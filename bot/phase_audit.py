"""阶段 0/1/2 的只读验收：直接读生产账本，不猜、不写。

为什么需要它：本地 `data/` 不是生产数据（`sync_to_cloud.py` 永不动 `data/`），
所以「本地 cognition.db 是空的」既不能证明线上没跑，也不能证明线上跑对了。
这个模块只读 `cognition.db` 与 `emotion_shadow.db`，输出：

  · 各表计数、事件类型、动作状态；
  · 阶段 0：动作是否有回执、是否带话题主体与证据；
  · 阶段 1：信念是否有证据/修订，自我认识修订是否带理由；
  · 阶段 2：影子是否可重放、有无无因跳变、有无未消费积压。

只输出计数、时间差与哈希，不输出聊天原文、QQ 号、payload、meta 值。
用法（本地或服务器）：
    python phase_audit.py --json
    python phase_audit.py --strict      # 有 fail 时退出码 1
"""
import argparse
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import emotion_core as core

TABLES = ("person", "event", "episode", "belief", "belief_evidence",
          "belief_revision", "commitment", "goal", "action", "self_view_rev",
          "meta")
DIMENSIONS = ("valence", "arousal", "control", "attention")
TERMINAL = ("accepted", "failed", "unknown")
STATUSES = ("pass", "partial", "not_started", "fail")


def root_dir(root=None):
    if root:
        return Path(root)
    return Path(os.getenv("PERSONA_DATA_DIR") or
                str(Path(os.getenv("DATA_DIR", "./data")) / "persona"))


def _ro(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                        timeout=2)
    c.row_factory = sqlite3.Row
    return c


def _one(c, sql, args=()):
    try:
        return c.execute(sql, args).fetchone()[0]
    except Exception:
        return None


def _cognition(path):
    out = {"present": path.is_file(), "counts": {}, "event_kinds": {},
           "checks": {}, "extra": {}}
    if not out["present"]:
        return out
    c = _ro(path)
    try:
        for t in TABLES:
            out["counts"][t] = int(_one(c, 'SELECT COUNT(*) FROM "%s"' % t) or 0)
        out["event_kinds"] = {
            str(r["kind"]): int(r["n"]) for r in
            c.execute("SELECT kind, COUNT(*) n FROM event GROUP BY kind")}
        out["extra"]["actions_by_state"] = {
            str(r["state"]): int(r["n"]) for r in
            c.execute("SELECT state, COUNT(*) n FROM action GROUP BY state")}
        out["extra"]["actions_by_kind"] = {
            str(r["kind"]): int(r["n"]) for r in
            c.execute("SELECT kind, COUNT(*) n FROM action GROUP BY kind")}
        out["extra"]["goals_by_status"] = {
            str(r["status"]): int(r["n"]) for r in
            c.execute("SELECT status, COUNT(*) n FROM goal GROUP BY status")}
        out["extra"]["commitments_by_status"] = {
            str(r["status"]): int(r["n"]) for r in
            c.execute("SELECT status, COUNT(*) n FROM commitment GROUP BY status")}
        out["extra"]["meta_keys"] = sorted(
            str(r["key"]) for r in c.execute("SELECT key FROM meta"))

        # 契约：事件发生时间不得晚于记录时间。
        # 秒级以上的倒挂才算违约；亚秒级（旧写入器留下的 1–5ms）单独报，
        # 不让两条历史记录把整个阶段永久判死，也不把它藏起来。
        row = c.execute("SELECT COUNT(*) n, COALESCE(MAX(ts-created),0) d "
                        "FROM event WHERE ts>created+1.0").fetchone()
        sub = c.execute("SELECT COUNT(*) n, COALESCE(MAX(ts-created),0) d "
                        "FROM event WHERE ts>created AND ts<=created+1.0").fetchone()
        out["checks"]["time_order"] = {
            "violations": int(row["n"] or 0),
            "max_delta_seconds": round(float(row["d"] or 0), 6),
            "subsecond_skew": int(sub["n"] or 0),
            "subsecond_max_seconds": round(float(sub["d"] or 0), 6)}

        out["checks"]["episode_without_events"] = int(_one(
            c, "SELECT COUNT(*) FROM episode WHERE is_legacy=0 "
               "AND event_ids IN ('[]','')") or 0)
        out["checks"]["belief_evidence_orphans"] = int(_one(
            c, "SELECT COUNT(*) FROM belief_evidence e "
               "LEFT JOIN event v ON v.id=e.event_id WHERE v.id IS NULL") or 0)
        out["checks"]["inbound_deduped"] = int(_one(
            c, "SELECT COUNT(*) FROM event WHERE kind IN "
               "('group_message','private_message') AND dedupe IS NOT NULL "
               "AND dedupe!=''") or 0)
        out["checks"]["tool_results"] = int(out["event_kinds"].get("tool_result", 0))
        out["checks"]["action_evidence"] = int(_one(
            c, "SELECT COUNT(*) FROM action WHERE event_ids NOT IN ('[]','')") or 0)
        out["checks"]["action_topic"] = int(_one(
            c, "SELECT COUNT(*) FROM action WHERE topic_qq!=''") or 0)
        out["checks"]["action_receipt_gap"] = int(_one(
            c, "SELECT COUNT(*) FROM action a WHERE a.state IN "
               "('accepted','failed','unknown') AND NOT EXISTS ("
               "SELECT 1 FROM event e WHERE e.kind='action_result' "
               "AND e.payload LIKE '%\"action_id\": \"' || a.id || '\"%')") or 0)
        out["checks"]["action_terminal"] = sum(
            v for k, v in out["extra"]["actions_by_state"].items()
            if k in TERMINAL)
        out["checks"]["action_receipts"] = max(
            0, out["checks"]["action_terminal"] - out["checks"]["action_receipt_gap"])
        out["checks"]["belief_revisions_with_evidence"] = int(_one(
            c, "SELECT COUNT(*) FROM belief_revision "
               "WHERE event_ids NOT IN ('[]','')") or 0)
        out["checks"]["legacy_beliefs_with_origin"] = int(_one(
            c, "SELECT COUNT(DISTINCT be.belief_id) FROM belief_evidence be "
               "JOIN belief b ON b.id=be.belief_id WHERE b.is_legacy=1 "
               "AND be.relation='origin'") or 0)
        out["checks"]["legacy_beliefs_without_evidence"] = int(_one(
            c, "SELECT COUNT(*) FROM belief b WHERE b.is_legacy=1 AND NOT EXISTS ("
               "SELECT 1 FROM belief_evidence be WHERE be.belief_id=b.id)") or 0)
        out["checks"]["beliefs_legacy"] = int(_one(
            c, "SELECT COUNT(*) FROM belief WHERE is_legacy=1") or 0)
        out["checks"]["self_view_with_reason"] = int(_one(
            c, "SELECT COUNT(*) FROM self_view_rev WHERE reason!=''") or 0)
    finally:
        c.close()
    out["extra"]["event_span"] = _span(path)
    return out


def _span(path):
    c = None
    try:
        c = _ro(path)
        row = c.execute("SELECT MIN(ts) a, MAX(ts) b FROM event").fetchone()
        return [float(row["a"] or 0), float(row["b"] or 0)]
    except Exception:
        return [0.0, 0.0]
    finally:
        if c is not None:
            c.close()


def _finite_number(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError("invalid_shadow_number")
    return float(value)


def _shadow_state(encoded, kernel):
    state = json.loads(encoded)
    if not isinstance(state, dict) or "at" not in state:
        raise ValueError("invalid_shadow_state")
    # advance 到同一时刻只做当前内核的完整 schema/bounds/traits 校验。
    return kernel.advance(state, state["at"])


def _shadow_appraisal(encoded, event_id, kernel):
    ap = json.loads(encoded)
    if (not isinstance(ap, dict) or set(ap) != {
            "rule", "confidence", "delta", "evidence_ids", "ignored",
            "reason", "version", "synthetic"}
            or not isinstance(ap["delta"], dict)
            or set(ap["delta"]) != set(DIMENSIONS)
            or type(ap["ignored"]) is not bool
            or type(ap["synthetic"]) is not bool
            or not isinstance(ap["rule"], str)
            or not isinstance(ap["reason"], str)
            or ap["version"] != kernel.VERSION
            or ap["evidence_ids"] != [event_id]
            or not 0 <= _finite_number(ap["confidence"]) <= 1):
        raise ValueError("invalid_shadow_appraisal")
    for delta in ap["delta"].values():
        _finite_number(delta)
    return ap


def _shadow(path, cognition_path, now=None):
    out = {"present": path.is_file(), "counts": {}, "checks": {},
           "replay": {}, "status": "not_started"}
    if not out["present"]:
        return out
    c = None
    try:
        c = _ro(path)
        run = c.execute("SELECT * FROM shadow_run WHERE id=1").fetchone()
        if run is None:
            out["status"] = "fail"
            out["checks"]["error"] = "shadow_run_missing"
            return out
        for key in ("processed", "real_events", "cursor"):
            if type(run[key]) is not int or run[key] < 0:
                raise ValueError("invalid_shadow_counter")
        for key in ("observed_seconds", "gap_seconds", "last_tick", "observation_started"):
            if _finite_number(run[key]) < 0:
                raise ValueError("invalid_shadow_time")
        import emotion_shadow
        # 消费器可升级，既有冻结账本仍按其内核审计；不依赖新增 status 接口。
        kernel = core if run["parameter_hash"] == core.parameter_hash() else emotion_shadow.core
        if run["parameter_hash"] != kernel.parameter_hash():
            raise ValueError("unknown_shadow_parameters")
        saved = _shadow_state(run["state"], kernel)
        if (saved["processed"] != run["processed"]
                or run["real_events"] > run["processed"]
                or run["last_tick"] < run["observation_started"]
                or not isinstance(run["cursor_id"], str)
                or json.loads(run["parameters"]) != kernel.PARAMETERS):
            raise ValueError("invalid_shadow_run")
        out["counts"].update({
            "processed": run["processed"],
            "real_events": run["real_events"],
            "shadow_events": c.execute("SELECT COUNT(*) FROM shadow_event").fetchone()[0],
            "cursor": run["cursor"],
            "observed_seconds": round(float(run["observed_seconds"]), 1),
            "gap_seconds": round(float(run["gap_seconds"]), 1),
            "last_tick": float(run["last_tick"]),
            "observation_started": float(run["observation_started"]),
        })
        out["checks"]["parameter_hash"] = str(run["parameter_hash"] or "")[:16]
        age = (time.time() if now is None else float(now)) - run['last_tick']
        out['checks']['heartbeat_fresh'] = 0 <= age < 90
        out['counts']['heartbeat_age_seconds'] = age

        mismatches = 0
        schema_errors = 0
        order_jumps = 0
        worst = 0.0
        for row in c.execute("SELECT * FROM shadow_event ORDER BY source_rowid"):
            try:
                ap = _shadow_appraisal(row["appraisal"], row["event_id"], kernel)
                before = _shadow_state(row["before_state"], kernel)
                after = _shadow_state(row["after_state"], kernel)
                at = _finite_number(row["at"])
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                schema_errors += 1
                mismatches += 1
                continue
            if after["at"] + 1e-12 < before["at"]:
                order_jumps += 1
            bad = (after["processed"] != before["processed"] + 1
                   or abs(before["at"] - at) > 1e-9
                   or abs(after["at"] - at) > 1e-9)
            for key in DIMENSIONS:
                lo, hi = kernel.PARAMETERS["bounds"][key]
                delta = ap["delta"][key]
                # ignored 也必须无冲击；不能靠一起篡改 delta/after 蒙混重放。
                expected = (before[key] if ap["ignored"] else
                            min(hi, max(lo, before[key] + delta)))
                diff = abs(after[key] - expected)
                worst = max(worst, diff)
                bad |= diff > 1e-9 or (ap["ignored"] and delta != 0)
            mismatches += int(bad)
        out["checks"]["schema_errors"] = schema_errors
        out["checks"]["delta_mismatches"] = mismatches
        out["checks"]["delta_max_error"] = worst
        out["checks"]["order_jumps"] = order_jumps

        try:
            if schema_errors:
                out["replay"] = {"ok": False, "reason": "invalid_shadow_schema"}
            else:
                out["replay"] = emotion_shadow.verify_replay(str(path))
        except Exception as e:
            out["replay"] = {"ok": False, "reason": type(e).__name__}
        out["checks"]["replay_ok"] = bool(out["replay"].get("ok"))

        backlog = None
        identity_ok = None
        if cognition_path.is_file():
            try:
                with closing(_ro(cognition_path)) as src:
                    # 同一只读快照检查游标身份和积压；rowid 可以有空洞。
                    src.execute("BEGIN")
                    backlog = int(src.execute("SELECT COUNT(*) FROM event WHERE rowid>?",
                                              (run["cursor"],)).fetchone()[0])
                    previous = src.execute("SELECT id FROM event WHERE rowid=?",
                                           (run["cursor"],)).fetchone()
                    identity_ok = (previous is not None and previous["id"] == run["cursor_id"]
                                   if run["cursor"] else run["cursor_id"] == "")
            except (sqlite3.Error, OSError):
                backlog = None
                identity_ok = None
        out["counts"]["backlog"] = backlog
        out["checks"]["cursor_identity_ok"] = identity_ok
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError,
            OverflowError, RecursionError) as e:
        out["status"] = "fail"
        out["checks"]["error"] = type(e).__name__
        return out
    finally:
        if c is not None:
            c.close()
    if (not out["checks"].get("replay_ok")
            or out["checks"].get("delta_mismatches")
            or out["checks"].get("order_jumps")
            or identity_ok is False
            or out["counts"]["processed"] != out["counts"]["shadow_events"]):
        out["status"] = "fail"
    elif not run["processed"] and not run["observed_seconds"] and backlog == 0:
        out["status"] = "not_started"
    elif backlog is None or backlog > 0 or not out['checks']['heartbeat_fresh']:
        out["status"] = "partial"
    elif not run["real_events"] or not run["observed_seconds"]:
        out["status"] = "partial"
    else:
        out["status"] = "pass"
    return out


def _phase0(cog):
    if cog["checks"].get("error"):
        return "partial"
    if not cog["present"] or cog["counts"].get("event", 0) == 0:
        return "not_started"
    if cog["checks"].get("time_order", {}).get("violations"):
        return "fail"
    receipts = cog["checks"].get("action_receipts", 0)
    actions = cog["counts"].get("action", 0)
    if actions == 0:
        # 只有事件、还没有对外动作：证据层在，但「能否追踪一次行动」没被验证
        return "partial"
    if receipts == 0:
        return "partial"
    if actions and (cog["checks"].get("action_evidence", 0) == 0
                    or cog["checks"].get("action_topic", 0) == 0):
        return "partial"
    return "pass"


def _phase1(cog):
    if cog["checks"].get("error"):
        return "partial"
    if not cog["present"]:
        return "not_started"
    counts = cog["counts"]
    checks = cog["checks"]
    if (checks.get("episode_without_events")
            or checks.get("belief_evidence_orphans")):
        return "fail"
    if (counts.get("belief", 0)
            and (counts.get("belief_evidence", 0)
                 or checks.get("belief_revisions_with_evidence", 0))
            and checks.get("self_view_with_reason", 0)):
        return "pass"
    if any(counts.get(k, 0) for k in
           ("belief", "episode", "belief_revision", "self_view_rev")):
        return "partial"
    return "not_started"


def audit(root=None, now=None):
    base = root_dir(root)
    try:
        cog = _cognition(base / "cognition.db")
    except (sqlite3.Error, OSError) as e:
        # 源不可读不是「零条事件」；仍继续影子审计，保留 unknown/partial。
        cog = {"present": (base / "cognition.db").is_file(), "counts": {},
               "checks": {"error": type(e).__name__}, "extra": {}}
    active = base / 'emotion_shadow_v2.db'
    sh = _shadow(active if active.is_file() else base / 'emotion_shadow.db', base / 'cognition.db', now=now)
    phases = {
        "phase0": {"name": "统一证据", "status": _phase0(cog)},
        "phase1": {"name": "自传体记忆", "status": _phase1(cog)},
        "phase2": {"name": "情感内核影子", "status": sh["status"]},
    }
    rep = {
        "ok": all(p["status"] != "fail" for p in phases.values()),
        "checked_at": float(now or time.time()),
        "root": str(base),
        "phases": phases,
        "counts": {"cognition": cog["counts"], "shadow": sh["counts"]},
        "checks": {"cognition": cog["checks"], "shadow": sh["checks"]},
        "extra": cog["extra"],
        "shadow_replay": sh["replay"],
    }
    for p in rep["phases"].values():
        if p["status"] not in STATUSES:
            p["status"] = "fail"
    return rep


def _observation_release(base, at):
    """Only an explicit deployment marker bound to this exact code/runtime counts.

    This reader never creates the marker, resets a clock, or rewrites evidence.
    Missing/stale/malformed metadata leaves the original admission scope in place.
    """
    out = {"valid": False, "deployed_at": None, "reason": "missing"}
    path = base / "observation_release.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid_metadata")
        deployed = _finite_number(data.get("deployed_at"))
        if not 0 < deployed <= at:
            raise ValueError("invalid_deployed_at")
        import engineering_contracts
        if not isinstance(data.get("binding"), dict) or data["binding"] != engineering_contracts.binding():
            raise ValueError("binding_mismatch")
        out.update(valid=True, deployed_at=deployed, reason="validated")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        # Never return marker contents, file hashes, or arbitrary exception text.
        out["reason"] = (str(exc) if isinstance(exc, ValueError) and str(exc) in
                         {"invalid_metadata", "invalid_deployed_at", "binding_mismatch"}
                         else type(exc).__name__)
    return out


def _readiness_cognition(path, at, window_seconds, since=None):
    if not path.is_file():
        return {"known": False, "reason": "cognition_missing"}
    with closing(_ro(path)) as c:
        c.execute("BEGIN")
        events = {r["id"]: r for r in c.execute(
            "SELECT id,kind,source,session,payload,created FROM event")}
        event_ids = set(events)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        retracted = ({r[0] for r in c.execute('SELECT event_id FROM evidence_retraction')}
                     if 'evidence_retraction' in tables else set())
        invalidated = ({(r[0], r[1]) for r in c.execute(
            'SELECT kind,object_id FROM cognition_invalidation')}
                       if 'cognition_invalidation' in tables else set())
        markers = ("legacy", "synthetic", "verify", "verification", "test", "probe")

        def excluded_source(*labels):
            return any(
                value == marker or value.startswith(tuple(marker + sep for sep in "/:_-"))
                for value in (str(label or "").casefold() for label in labels)
                for marker in markers)

        def usable(eid):
            row = events.get(eid)
            if (row is None or eid in retracted
                    or (since is not None and not since <= row["created"] <= at)):
                return False
            payload = json.loads(row["payload"] or "{}")
            if not isinstance(payload, dict) or payload.get("synthetic") or payload.get("evidence_origin") == "verification":
                return False
            return row["kind"] != "legacy_note" and not excluded_source(row["source"], row["session"])
        if since is None:
            # Historic rolling coverage includes old proposals executed today.
            actions = c.execute(
                "SELECT * FROM action WHERE (created>=? AND created<=?) "
                "OR (updated>=? AND updated<=?)",
                (at - window_seconds, at, at - window_seconds, at)).fetchall()
        else:
            actions = c.execute("SELECT * FROM action WHERE created>=? AND created<=?",
                                (since, at)).fetchall()
        linked = dangling = unusable = 0
        for row in actions:
            refs = json.loads(row["event_ids"] or "[]")
            if not isinstance(refs, list) or any(not isinstance(e, str) for e in refs):
                raise ValueError("invalid_action_evidence")
            missing = sum(e not in event_ids for e in refs)
            invalid = sum(e in event_ids and not usable(e) for e in refs)
            dangling += missing
            unusable += invalid
            linked += (bool(refs) and not missing and not invalid
                       and ('action', row['id']) not in invalidated)
        chat_actions = [r for r in actions if r["kind"] in ("group", "dm")]

        def recipient_bound(row):
            if row["kind"] == "dm":
                # 私聊必须认得出具体的人：话题主体 + 收件人 + private 可见性。
                return (bool(row["topic_qq"]) and bool(row["target_qq"])
                        and row["visibility"] == "private")
            # 群发言的收件方就是群本身（scope group:<数码> + group 可见性）。
            # 不硬要一个人物话题：主动群发言没有单一话题人物，旧口径因此把
            # 真实群发言全判未绑定（线上 586 条聊天动作里 359 条 topic 为空）。
            scope = str(row["scope"] or "")
            return (scope.startswith("group:") and scope[6:].isdigit()
                    and row["visibility"] == "group")
        results = set()
        result_sql = "SELECT id,payload FROM event WHERE kind='action_result'"
        for row in c.execute(
                result_sql if since is None else result_sql + " AND created>=? AND created<=?",
                () if since is None else (since, at)):
            payload = json.loads(row["payload"] or "{}")
            if isinstance(payload, dict) and usable(row["id"]):
                results.add((str(payload.get("action_id") or ""),
                             str(payload.get("state") or "")))
        terminal = [r for r in actions if r["state"] in TERMINAL]
        receipted = sum((r["id"], r["state"]) in results for r in terminal)
        belief_sql = "SELECT id,source,status FROM belief WHERE is_legacy=0"
        nonlegacy = c.execute(
            belief_sql if since is None else belief_sql + " AND first_ts>=? AND first_ts<=?",
            () if since is None else (since, at)).fetchall()
        organic_beliefs = [r for r in nonlegacy if not excluded_source(r["source"])]
        # An intentional retraction is not an active unsupported assertion.
        # Exclude only the explicit status; an ACTIVE row with an invalidation
        # marker is still inconsistent and must fail grounding below.
        live_beliefs = [r for r in organic_beliefs if r["status"] != "invalidated"]
        belief_statuses = {}
        for row in nonlegacy:
            belief_statuses[row["status"]] = belief_statuses.get(row["status"], 0) + 1
        grounded_beliefs = 0
        for row in live_beliefs:
            refs = [r["event_id"] for r in c.execute(
                "SELECT event_id FROM belief_evidence WHERE belief_id=?", (row["id"],))]
            grounded_beliefs += (row['status'] == 'active' and bool(refs)
                                  and all(usable(e) for e in refs)
                                  and ('belief', row['id']) not in invalidated)
        revisions = c.execute("SELECT id,event_ids FROM self_view_rev WHERE ts>=? AND ts<=?", 
                              (at - window_seconds if since is None else since, at)).fetchall()
        grounded_revisions = aggregate_only = orphan_revisions = 0
        direct_kinds = {"message", "group_message", "private_message", "tool_result"}
        for row in revisions:
            refs = json.loads(row["event_ids"] or "[]")
            if not isinstance(refs, list) or any(not isinstance(e, str) for e in refs):
                raise ValueError("invalid_self_view_evidence")
            kinds = []
            for eid in refs:
                event = c.execute("SELECT kind FROM event WHERE id=?", (eid,)).fetchone()
                if event:
                    kinds.append(event["kind"])
                else:
                    orphan_revisions += 1
            grounded_revisions += (bool(set(kinds) & direct_kinds) and all(usable(e) for e in refs)
                                   and ('self_view_rev', row['id']) not in invalidated)
            aggregate_only += bool(kinds) and set(kinds) == {"consolidation"}
        integrity = None
        if since is not None:
            # Keep full-ledger integrity in the baseline/history report. A release
            # may separate old row defects, never a current SQL/read failure or
            # a shadow replay failure (the shadow ledger is not reset here).
            integrity = {
                "time_order_violations": c.execute(
                    "SELECT COUNT(*) FROM event WHERE created>=? AND created<=? "
                    "AND ts>created+1.0", (since, at)).fetchone()[0],
                "episode_without_events": c.execute(
                    "SELECT COUNT(*) FROM episode WHERE ts>=? AND ts<=? "
                    "AND is_legacy=0 AND event_ids IN ('[]','')", (since, at)).fetchone()[0],
                "belief_evidence_orphans": c.execute(
                    "SELECT COUNT(*) FROM belief_evidence e "
                    "LEFT JOIN belief b ON b.id=e.belief_id "
                    "LEFT JOIN event v ON v.id=e.event_id WHERE v.id IS NULL "
                    "AND ((b.first_ts>=? AND b.first_ts<=?) OR "
                    "(b.id IS NULL AND e.ts>=? AND e.ts<=?))",
                    (since, at, since, at)).fetchone()[0],
            }
        return {
            "known": True,
            "ledger_integrity": integrity,
            "action_evidence": {"total": len(actions), "linked": linked,
                                "missing": len(actions) - linked, "dangling_refs": dangling,
                                "unusable_refs": unusable},
            "action_identity": {"total": len(chat_actions), "bound": sum(
                recipient_bound(r) for r in chat_actions)},
            "action_results": {"terminal": len(terminal), "receipted": receipted,
                               "missing": len(terminal) - receipted},
            "belief_grounding": {"nonlegacy": len(live_beliefs),
                                 "with_existing_evidence": grounded_beliefs,
                                 "excluded": len(nonlegacy) - len(live_beliefs),
                                 "excluded_source": len(nonlegacy) - len(organic_beliefs),
                                 "excluded_invalidated": len(organic_beliefs) - len(live_beliefs),
                                 "statuses": belief_statuses},
            "self_view_grounding": {"total": len(revisions), "with_direct_sources": grounded_revisions,
                                    "aggregate_only": aggregate_only, "dangling_refs": orphan_revisions},
        }


def audit_readiness(root=None, now=None, window_seconds=86400,
                    include_candidate_replay=True):
    """Read-only admission evidence; unverified controls block and never grant authority."""
    at = time.time() if now is None else float(now)
    window = max(3600, min(30 * 86400, int(window_seconds or 86400)))
    baseline = audit(root=root, now=at)
    release = _observation_release(root_dir(root), at)
    boundary = max(at - window, release["deployed_at"]) if release["valid"] else None
    checks = []

    def add(key, passed, observed, reason, observed_count=1, target=None):
        status = "pass" if passed else "not_observed" if observed_count == 0 else "blocked"
        (checks if target is None else target).append(
            {"key": key, "status": status, "observed": observed, "reason": reason})

    add("ledger_integrity", baseline["ok"],
        {k: v["status"] for k, v in baseline["phases"].items()},
        "Integrity is distinct from readiness; partial stages never authorize expression.")
    scoped_integrity = None

    def cognition_checks(since=None):
        nonlocal scoped_integrity
        result = []
        try:
            evidence = _readiness_cognition(root_dir(root) / "cognition.db", at, window, since=since)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError,
                OverflowError, RecursionError) as exc:
            evidence = {"known": False, "reason": type(exc).__name__}
        if not evidence["known"]:
            return [{"key": "cognition_evidence", "status": "unknown",
                     "observed": None, "reason": evidence["reason"]}]
        if since is not None:
            scoped_integrity = evidence["ledger_integrity"]
        a = evidence["action_evidence"]
        add("action_evidence", a["total"] > 0 and a["missing"] == 0,
            a, "Every recent action needs existing evidence, not merely one linked action.", a["total"], result)
        a = evidence["action_identity"]
        add("action_identity", a["total"] > 0 and a["bound"] == a["total"],
            a, "Chat actions need topic identity and a private recipient or group scope matching visibility; this is structural coverage, not semantic authorization.", a["total"], result)
        a = evidence["action_results"]
        add("action_results", a["terminal"] > 0 and a["missing"] == 0,
            a, "Every terminal action needs a matching result observation.", a["terminal"], result)
        a = evidence["belief_grounding"]
        add("belief_grounding", a["nonlegacy"] > 0 and a["with_existing_evidence"] == a["nonlegacy"],
            a, "Legacy volume cannot establish grounded new beliefs; explicitly invalidated beliefs are excluded, not repaired.", a["nonlegacy"], result)
        a = evidence["self_view_grounding"]
        add("self_view_grounding", a["total"] > 0 and a["with_direct_sources"] == a["total"]
            and a["dangling_refs"] == 0,
            a, "A consolidation timestamp alone does not explain a self-view revision.", a["total"], result)
        return result

    historic_cognition = cognition_checks()
    current_cognition = historic_cognition if boundary is None else cognition_checks(boundary)
    historic_integrity = checks[0]
    if boundary is not None:
        readable = (scoped_integrity is not None
                    and not baseline["checks"]["cognition"].get("error"))
        checks[0] = {
            "key": "ledger_integrity",
            "status": ("unknown" if not readable else "pass"
                       if not any(scoped_integrity.values())
                       and baseline["phases"]["phase2"]["status"] != "fail" else "blocked"),
            "observed": {"current_cognition": scoped_integrity,
                         "shadow": baseline["phases"]["phase2"]["status"]},
            "reason": "Current cognition integrity plus unchanged full shadow integrity; historical cognition defects remain in history_checks and baseline.",
        }
    checks.extend(current_cognition)
    shadow_review = {}
    try:
        import emotion_shadow
        # Never mutate DATA_DIR in a running process to inspect a different ledger.
        if root_dir(root).resolve() == emotion_shadow._root().resolve():
            def mapping(value):
                if value is None:
                    return {}
                if not isinstance(value, dict):
                    raise ValueError("invalid_shadow_status_container")
                return value

            info = mapping(emotion_shadow.status(now=at, limit=1,
                                                include_valence=include_candidate_replay))
            detail = mapping(info.get("calibration_detail"))
            diagnostics = mapping(info.get("diagnostics"))
            dims = mapping(diagnostics.get("dimensions"))
            valence = mapping(dims.get("valence"))
            continuity = mapping(diagnostics.get("continuity"))
            replay = mapping(detail.get("replay"))
            shadow_review = {
                "ready_for_review": info.get("ready_for_review") is True,
                "expression_control": info.get("expression_control") is True,
                "calibration_passed": detail.get("passed", 0),
                "calibration_total": detail.get("total", 0),
                "valence_positive": valence.get("positive", 0),
                "valence_negative": valence.get("negative", 0),
                "effective_events": diagnostics.get("effective_events"),
                "observed_seconds": continuity.get("observed_seconds"),
                "gap_ratio": continuity.get("gap_ratio"),
                "replay_max_error": replay.get("max_error"),
            }
            add("shadow_review", shadow_review["ready_for_review"]
                and not shadow_review["expression_control"], shadow_review,
                "Shadow observation readiness is not calibrated expression behavior or permission to take control.")
        else:
            checks.append({"key": "shadow_review", "status": "unknown", "observed": None,
                           "reason": "Requested root differs from the shadow consumer root; no alternate ledger is inspected."})
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError,
            OverflowError, RecursionError) as exc:
        checks.append({"key": "shadow_review", "status": "unknown", "observed": None,
                       "reason": type(exc).__name__})
    import engineering_contracts
    contracts, contract_error = engineering_contracts.read_evidence(root_dir(root))
    for key in engineering_contracts.CASES:
        checks.append({"key": key, "status": "pass" if key in contracts else "not_verified",
                       "observed": contracts.get(key),
                       "reason": "Isolated engineering evidence only; not live organic evidence."
                       if key in contracts else "Independent contract evidence unavailable: " + contract_error})
    blockers = [c["key"] for c in checks if c["status"] != "pass"]
    history_checks = [historic_integrity] + historic_cognition + checks[1 + len(current_cognition):]
    if not release["valid"]:
        observation_status = "not_configured" if release["reason"] == "missing" else "invalid_boundary"
    elif (checks[0]["status"] != "pass"
          or any(c["status"] in ("blocked", "unknown") for c in current_cognition)):
        observation_status = "blocked"
    elif any(c["status"] == "not_observed" for c in current_cognition):
        observation_status = "not_observed"
    else:
        observation_status = "observed"
    observation = {
        "status": observation_status,
        "boundary_at": boundary,
        "label": ("Current organic observation since max(rolling window start, validated deployment)."
                  if release["valid"] else "Original admission scope; no validated release boundary."),
        "reason": "Wait for real organic samples; never backfill history or authorize stage 3.",
    }
    return {
        **baseline,
        "phase25": {"status": "blocked" if blockers else "review_ready",
                    "window_seconds": window, "blockers": blockers, "checks": checks,
                    "observation_release": release, "observation": observation,
                    "history_checks": history_checks,
                    "history_label": "Historical coverage: full rolling actions (created or updated) and self-view revisions; all-time nonlegacy beliefs. Not backfilled or repaired.",
                    "history_boundary_at": at - window,
                    "shadow_review": shadow_review},
        "stage3": {"authorized": False, "expression_control": False,
                   "stage3a": "blocked" if blockers else "review_required",
                   "stage3b": "blocked"},
    }


def render(rep):
    """一行一个阶段，给不方便读 JSON 的时候看。"""
    lines = []
    order = ("phase0", "phase1", "phase2")
    c = rep["counts"]["cognition"]
    s = rep["counts"]["shadow"]
    for key in order:
        p = rep["phases"][key]
        if key == "phase0":
            detail = ("events=%s receipts=%s evidence=%s topic=%s"
                      % (c.get("event"), rep["checks"]["cognition"].get("action_receipts"),
                         rep["checks"]["cognition"].get("action_evidence"),
                         rep["checks"]["cognition"].get("action_topic")))
        elif key == "phase1":
            detail = ("beliefs=%s/legacy=%s revisions=%s episodes=%s self_view=%s"
                      % (c.get("belief"),
                         rep["checks"]["cognition"].get("beliefs_legacy"),
                         rep["checks"]["cognition"].get("belief_revisions_with_evidence"),
                         c.get("episode"), rep["checks"]["cognition"].get("self_view_with_reason")))
        else:
            detail = ("processed=%s/%s replay=%s backlog=%s"
                      % (s.get("processed") if s else "—",
                         s.get("shadow_events") if s else "—",
                         "ok" if rep["checks"]["shadow"].get("replay_ok") else "fail",
                         s.get("backlog") if s else "—"))
        lines.append("%s %s: %s (%s)" % (key, p["name"], p["status"], detail))
    lines.append("ok=%s" % rep["ok"])
    if "phase25" in rep:
        lines.append("phase2.5: %s" % rep["phase25"]["status"])
        for check in rep["phase25"]["checks"]:
            lines.append("  %s: %s" % (check["key"], check["status"]))
        observation = rep["phase25"].get("observation", {})
        if observation:
            lines.append("observation=%s boundary_at=%s" % (
                observation["status"], observation["boundary_at"]))
            lines.append(rep["phase25"]["history_label"])
            for check in rep["phase25"]["history_checks"]:
                lines.append("  history %s: %s" % (check["key"], check["status"]))
        lines.append("stage3a=%s stage3b=%s authorized=false" % (
            rep["stage3"]["stage3a"], rep["stage3"]["stage3b"]))
    return "\n".join(lines)


def main(argv=None):
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="阶段 0/1/2 只读验收")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--root", default="", help="persona 数据目录")
    ap.add_argument("--strict", action="store_true", help="完整性失败或准入未通过时退出码 1")
    ap.add_argument("--readiness", action="store_true", help="阶段 2.5 只读准入评估，不开启表达接管")
    args = ap.parse_args(argv)
    import sys
    from contextlib import redirect_stdout
    with redirect_stdout(sys.stderr):
        rep = audit_readiness(args.root or None) if args.readiness else audit(args.root or None)
    print(json.dumps(rep, ensure_ascii=False, sort_keys=True, allow_nan=False)
          if args.json else render(rep))
    if args.strict and (not rep["ok"] or
                        (args.readiness and rep["phase25"]["status"] != "review_ready")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())