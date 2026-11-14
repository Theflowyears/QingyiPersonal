# -*- coding: utf-8 -*-
"""自动驾驶决策内核：采集证据 → 算出下一步 → 分成"能自动做/要主人批/只能等"。

这一层是**确定性**的，不含模型判断：同样的证据必须给出同样的计划，
这样主人在 QQ 上看到的"待批准"和面板上显示的前置是同一套账。

安全边界（AGENTS.md 与统一方案 §21/§23.5 的口径）：
- 检测/UI/证据/验证这类基础设施，前置达标就自动推进，不用问。
- 记忆、情感、联想、表达、准入、目标、自我这些**算法**改动，一律先私聊主人，
  拿到 /ops approve 才进入自动队列 —— 授权只能由人给，检测器不自己授予。
- manual_review 任务永远不自动推进，哪怕主人批了单个任务也不放开整片开关。
- 任何推进前先备份；本地全量测试绿了才允许上云。
"""
import json
import os
import shutil
import sys
import time

import qingyi_roadmap

# 可以自动推进的轨道：都是基础设施，不改变她记住什么/怎么感受/说什么。
AUTO_TRACKS = frozenset({"detection", "ui", "evidence", "validation"})
# 必须主人批准的轨道：这些就是"算法改进"。
ASK_TRACKS = frozenset({"memory", "emotion", "ripple", "expression",
                        "admission", "goals", "self", "context", "knowledge"})
# 这些阶段即使轨道看起来安全也要问：它们直接接管她的对外表达。
ASK_STAGES = frozenset({"stage3a", "stage3b", "stage4", "stage5"})

def _local_root():
    return os.getenv("OPS_LOCAL_ROOT", os.getcwd())


def _backup_root():
    return os.getenv("OPS_LOCAL_BACKUP_ROOT", "_backup_autopilot")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _ops_dir():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "ops")


def max_tasks():
    """每轮任务上限。0（默认）= 不设上限，按前沿一次推完。

    主人要的是"能推就推到推不动"，不是每轮只动两项攒着。上限留着只是为了
    哪天要限速时能用一个环境变量收回去。
    """
    try:
        return max(0, int(os.getenv("OPS_MAX_TASKS_PER_ROUND", "0")))
    except ValueError:
        return 0


# ---------------------------------------------------------------- 前置图

def implemented_set(preflight):
    """以检测报告为准，不靠 grep 猜实现。"""
    out = set()
    for task in preflight.get("tasks") or []:
        if task.get("implementation") in ("implemented", "existing"):
            out.add(task["id"])
    return out


def dev_frontier(preflight):
    """尚未实现、但开发前置全部已实现的任务 —— 这就是"现在能开工"的集合。"""
    done = implemented_set(preflight)
    rows = []
    for task in qingyi_roadmap.catalog():
        if task["id"] in done:
            continue
        missing = [d for d in task["dev_requires"] if d not in done]
        if missing:
            continue
        rows.append(task)
    return rows


def classify_task(task, approvals):
    """返回 (类别, 原因)。类别 ∈ auto / ask / approved。"""
    tid = task["id"]
    decision = (approvals.get(tid) or {}).get("decision")
    if task["manual_review"]:
        if decision == "approve":
            return "approved", "人工标注/评审任务，主人已批准 %s" % tid
        if decision == "reject":
            return "rejected", "主人已否决 %s" % tid
        return "ask", "需要人工标注或评审，脚本不能替代"
    if task["stage"] in ASK_STAGES or task["track"] in ASK_TRACKS:
        if decision == "approve":
            return "approved", "算法改动，主人已批准 %s" % tid
        if decision == "reject":
            return "rejected", "主人已否决 %s" % tid
        return "ask", "属于算法改动（%s/%s），先私聊主人确认" % (task["stage"], task["track"])
    if decision == "reject":
        return "rejected", "主人已否决 %s" % tid
    if task["track"] in AUTO_TRACKS:
        return "auto", "基础设施轨道 %s，前置已达标" % task["track"]
    return "ask", "轨道 %s 不在自动推进白名单里" % task["track"]


def _priority(task):
    """统一方案 §21.2：先 D/U 交付，再按目录顺序。"""
    if task["track"] in ("detection", "ui"):
        return 0
    return 1


def frontier_rows(preflight, approvals):
    """完整前沿：每个「没做完但前置齐」的任务，连同它属于自动还是待批。

    面板的「当前可做/待批准」和 QQ 通知都从这里取，这样两边不会再各说各话。
    """
    rows = []
    for task in dev_frontier(preflight):
        kind, why = classify_task(task, approvals)
        rows.append({"id": task["id"], "title": task["title"], "stage": task["stage"],
                     "track": task["track"], "manual_review": task["manual_review"],
                     "dev_requires": task["dev_requires"], "kind": kind, "why": why})
    rows.sort(key=lambda r: (_priority(r), r["id"]))
    return rows


def next_actions(preflight, approvals, limit=None):
    """算出本轮建议顺序。auto/approved 排前面，ask 单列，不混成一个百分比。"""
    limit = max_tasks() if limit is None else limit
    auto, ask, rejected = [], [], []
    for row in frontier_rows(preflight, approvals):
        kind = row["kind"]
        if kind in ("auto", "approved"):
            auto.append(row)
        elif kind == "ask":
            ask.append(row)
        else:
            rejected.append(row)
    capped = auto[:limit] if limit and limit > 0 else auto
    rest = auto[limit:] if limit and limit > 0 else []
    return {"auto_advance": capped, "auto_queue_rest": rest,
            "needs_approval": ask, "rejected": rejected}


# ---------------------------------------------------------------- 阻塞分类

# 这五项读的是 persona/engineering_contracts.json，绑定当前部署代码。
# 没有产物就是 not_verified —— 这是机械可修的，不是算法问题。
ENGINEERING_GATES = ("consent_enforcement", "action_idempotency_outbox",
                     "evidence_retraction", "layer_versioning", "model_switch_behavior")


def classify_blockers(readiness, preflight_evidence, preflight_isolated, health):
    """把阻塞分成"能机械修"和"只能等数据"，别把等待当成故障去修。"""
    fixes, waiting, blocked = [], [], []
    phase25 = (readiness.get("phase25") or {})
    checks = {c.get("key"): c for c in phase25.get("checks") or []}

    missing_eng = [k for k in ENGINEERING_GATES
                   if (checks.get(k) or {}).get("status") != "pass"]
    if missing_eng:
        fixes.append({
            "id": "engineering_evidence",
            "action": "run_engineering_contracts",
            "why": "阶段2.5 的 %d 项工程门没有绑定当前代码的隔离证据：%s"
                   % (len(missing_eng), ", ".join(missing_eng)),
            "gates": missing_eng})

    for key, check in checks.items():
        if key in ENGINEERING_GATES or check.get("status") == "pass":
            continue
        observed = check.get("observed") or {}
        if key == "action_evidence" and isinstance(observed, dict):
            missing = observed.get("missing")
            if isinstance(missing, int) and missing > 0:
                waiting.append({
                    "id": "action_evidence",
                    "need": "她还需要 %d 次带真实证据的行动（当前 %s/%s 已关联）"
                            % (missing, observed.get("linked"), observed.get("total")),
                    "why": "自然样本不足，脚本不能替她造行动"})
                continue
        waiting.append({"id": key, "need": "需要真实运行样本或人工标注",
                        "why": str(check.get("reason") or "")[:160]})

    by_id = {c.get("id"): c for c in preflight_evidence.get("checks") or []}
    prov = by_id.get("memory_provenance") or {}
    if prov.get("status") in ("not_observed", "missing_data"):
        waiting.append({"id": "memory_provenance",
                        "need": "需要更多带逐命题来源的真实记忆写入",
                        "why": "只读审计里可定位来源的样本还不够"})

    iso = {c.get("id"): c for c in (preflight_isolated or {}).get("checks") or []}
    for key in ("usage_idempotency", "stage0_contracts"):
        row = iso.get(key)
        if row is None:
            fixes.append({"id": key, "action": "run_isolated_preflight",
                          "why": "隔离探针没跑过，工程验证是 not_verified"})
        elif row.get("status") != "pass":
            blocked.append({"id": key, "why": "隔离探针跑过但没通过，需要人看代码",
                            "status": row.get("status")})

    if not health.get("ok", False):
        for issue in health.get("issues") or []:
            if "qqbot" in issue:
                fixes.append({"id": "qqbot_down", "action": "restart_qqbot", "why": issue})
            else:
                blocked.append({"id": "server", "why": issue})
    return {"safe_fixes": fixes, "waiting_on": waiting, "blocked": blocked}


def wait_estimates():
    """给"等真实数据"的门估一个大体时间范围，估不出就如实说估不出。

    只认真实账本里的发生速率，不拍脑袋。self_view_grounding 要等下一次
    自我修订，用最近 7 天的修订速率推；没有历史就明说无法估计。
    """
    import sqlite3
    path = os.path.join(os.getenv("DATA_DIR", "./data"), "persona", "cognition.db")
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        now = time.time()
        week = now - 7 * 86400
        rev = c.execute("SELECT COUNT(*) FROM self_view_rev WHERE ts>=?", (week,)).fetchone()[0]
        if rev > 0:
            per_day = rev / 7.0
            days = 1.0 / per_day
            lo, hi = max(1, int(days * 0.5)), max(2, int(days * 2) + 1)
            out["self_view_grounding"] = "约 %d–%d 天（近7天 %d 次自我修订）" % (lo, hi, rev)
        else:
            out["self_view_grounding"] = "无法估计：近7天还没有自我修订，得等她真实发生"
        c.close()
    except sqlite3.Error:
        pass
    return out


def build_plan(evidence):
    approvals = (evidence.get("state") or {}).get("approvals") or {}
    actions = next_actions(evidence["preflight_evidence"], approvals)
    frontier = frontier_rows(evidence["preflight_evidence"], approvals)
    blockers = classify_blockers(evidence.get("readiness") or {},
                                 evidence["preflight_evidence"],
                                 evidence.get("preflight_isolated"),
                                 evidence.get("health") or {})
    waiting = list(blockers["waiting_on"])
    for tid, info in ((evidence.get("state") or {}).get("waiting") or {}).items():
        waiting.append({"id": tid, "need": info.get("what", ""), "why": "主人标记为等待数据"})
    plan = {
        "at": _now(),
        "health": evidence.get("health") or {},
        "scope_status": evidence["preflight_evidence"].get("scope_status"),
        "isolated_scope_status": (evidence.get("preflight_isolated") or {}).get("scope_status"),
        "auto_advance": actions["auto_advance"],
        "auto_queue_rest": actions["auto_queue_rest"],
        "needs_approval": actions["needs_approval"],
        "rejected": actions["rejected"],
        "frontier": frontier,
        "safe_fixes": blockers["safe_fixes"],
        "waiting_on": waiting,
        "blocked": blockers["blocked"],
        "wait_estimates": wait_estimates(),
        "max_tasks_this_round": max_tasks(),
        "authorization": evidence["preflight_evidence"].get("authorization"),
    }
    plan["notifications"] = build_notifications(plan, evidence.get("state") or {})
    return plan


# 重复通知的冷却期。巡检按小时跑，等待/空转每轮都发就是一天 24 条，
# 那不是在看护，是在刷屏。内容变了立刻发，没变就等冷却。
WAITING_COOLDOWN_HOURS = 12
BLOCKED_COOLDOWN_HOURS = 6
IDLE_COOLDOWN_HOURS = 6


def notify_interval_hours():
    """两次 QQ 推送的最小间隔。巡检每小时跑，但消息不必每小时响一次。"""
    try:
        return max(1, int(os.getenv("OPS_NOTIFY_INTERVAL_HOURS", "3")))
    except ValueError:
        return 3


def _push_window_open(proposed):
    last = proposed.get("last_push")
    if not isinstance(last, dict):
        return True
    try:
        return time.time() - float(last.get("at") or 0) >= notify_interval_hours() * 3600
    except (TypeError, ValueError):
        return True


def _sig(items):
    return ",".join(sorted(str(i) for i in items))


def _dedup_due(proposed, kind, sig, hours):
    prev = proposed.get(kind)
    if not isinstance(prev, dict) or prev.get("sig") != sig:
        return True
    try:
        return time.time() - float(prev.get("at") or 0) >= hours * 3600
    except (TypeError, ValueError):
        return True


def build_notifications(plan, state):
    """要私聊主人什么。同一件事别每轮都重复轰炸（靠 state.proposals 去重）。"""
    out = []
    proposed = state.get("proposals") or {}
    pending_ids = [t["id"] for t in plan["needs_approval"]]
    fresh = [i for i in pending_ids if i not in proposed]
    if fresh:
        lines = ["【运维】自动驾驶跑到算法边界了，这几项前置已达标但要你点头："]
        for task in plan["needs_approval"]:
            if task["id"] in fresh:
                lines.append("%s %s — %s" % (task["id"], task["title"], task["why"]))
        lines.append("批准：/ops approve 任务号；否决：/ops reject 任务号")
        out.append({"kind": "approval", "ids": fresh, "text": "\n".join(lines)})

    if plan["waiting_on"]:
        ids = [i["id"] for i in plan["waiting_on"]]
        sig = _sig(ids)
        if _dedup_due(proposed, "waiting", sig, WAITING_COOLDOWN_HOURS):
            lines = ["【运维】这轮推进不动了，在等真实数据，不是故障："]
            estimates = plan.get("wait_estimates") or {}
            for item in plan["waiting_on"][:6]:
                line = "%s：%s" % (item["id"], item["need"])
                if estimates.get(item["id"]):
                    line += "（%s）" % estimates[item["id"]]
                lines.append(line)
            lines.append("下一轮到点自动再看；想立刻跑一轮就说 /ops 现在跑一轮")
            out.append({"kind": "waiting", "ids": ids, "text": "\n".join(lines),
                        "dedup": {"kind": "waiting", "sig": sig}})

    if plan["blocked"]:
        ids = [b["id"] for b in plan["blocked"]]
        sig = _sig(ids)
        if _dedup_due(proposed, "blocked", sig, BLOCKED_COOLDOWN_HOURS):
            out.append({"kind": "blocked", "ids": ids,
                        "text": "【运维】有自动修不了的阻塞：\n" + "\n".join(
                            "%s：%s" % (b["id"], b["why"]) for b in plan["blocked"][:6]),
                        "dedup": {"kind": "blocked", "sig": sig}})

    if plan["auto_advance"]:
        out.append({"kind": "advance", "ids": [t["id"] for t in plan["auto_advance"]],
                    "text": "【运维】前置达标，本轮自动推进：" + ", ".join(
                        "%s %s" % (t["id"], t["title"]) for t in plan["auto_advance"])})

    quiet = not (fresh or plan["waiting_on"] or plan["blocked"] or plan["auto_advance"]
                 or plan["needs_approval"])
    if not out and quiet and _dedup_due(proposed, "idle", "idle", IDLE_COOLDOWN_HOURS):
        out.append({"kind": "idle", "ids": [],
                    "text": "【运维】这轮没有可推进项，也没有新阻塞，云端正常，继续等下一轮",
                    "dedup": {"kind": "idle", "sig": "idle"}})
    if not _push_window_open(proposed):
        # 每 3 小时一次：等待/阻塞/推进/空转都攒着，别每小时震一次手机。
        # 只有新批准例外 —— 那是唯一会卡住推进、越早看到越好的一条。
        return [note for note in out if note["kind"] == "approval"]
    return out


def mark_proposed(state, ids):
    proposed = state.setdefault("proposals", {})
    for tid in ids:
        proposed[tid] = _now()
    return state


# ---------------------------------------------------------------- 备份

def local_code_backup(label="pre-autopilot", keep=20):
    """只备份代码，不备份 data/ 与 config.env：人格数据在云端另有 SQLite 在线快照，
    而 config.env 自动驾驶从不改，多抄一份只是让密钥多一个落点。

    keep 只清理本功能自己产生的 _backup_autopilot/ 目录，不碰用户既有的
    _backup_* 快照 —— 那些是用户的资产，删它们要先问。
    """
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    root = _local_root()
    backup_root = _backup_root()
    dest = os.path.join(backup_root, stamp + "_" + label)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", "*.bak",
                                    "*.localbak", "*.db", "venv", "data", "logs", ".git")
    os.makedirs(dest, exist_ok=True)
    copied = []
    for folder in ("bot", "website", "tests"):
        src = os.path.join(root, folder)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, folder), ignore=ignore, dirs_exist_ok=True)
            copied.append(folder)
    for name in ("launcher.py", "requirements.txt"):
        src = os.path.join(root, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
            copied.append(name)
    pruned = []
    try:
        entries = sorted(os.listdir(backup_root))
        for old in entries[:-keep] if keep > 0 else []:
            path = os.path.join(backup_root, old)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                pruned.append(old)
    except OSError:
        pass
    return {"ok": True, "dir": dest, "copied": copied, "pruned_own_backups": pruned}


def backup_before_fixes(plan):
    """要动手修就先留底：本地代码快照 + 云端 persona 在线快照。

    备份失败就不修 —— 没有回滚点的写操作宁可不做，如实报阻塞。
    没有修复动作时只做本地代码快照，不每轮都堆一份 persona 快照。
    """
    out = {"local": local_code_backup(), "persona": None, "ok": False}
    if not (plan.get("safe_fixes") or []):
        out["ok"] = bool(out["local"].get("ok"))
        out["skipped"] = "no_fixes"
        return out
    import ops_remote
    try:
        with ops_remote.Remote() as remote:
            out["persona"] = ops_remote.backup_persona(remote, label="ops-autopilot")
    except Exception as e:
        out["persona"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    out["ok"] = bool(out["local"].get("ok")) and bool((out["persona"] or {}).get("ok"))
    return out


# ---------------------------------------------------------------- 轮次锁

def _lock_path():
    return os.path.join(_ops_dir(), ".round.lock")


def _lock_ttl_minutes():
    try:
        return max(5, int(os.getenv("OPS_ROUND_LOCK_MINUTES", "110")))
    except ValueError:
        return 110


# 同一轮里重复确认锁是合法的。两个真正并发的轮次不可能相差 90 秒
# （排程是每天 5 次），所以这个宽限期只会让"我自己再确认一次"变成功，
# 不会让第二个轮次混进来。22:00 那轮就是死在重复加锁上：拿到锁两分钟后
# 又检查了一次，看到 holder 是自己，按规程把自己关在了门外。
_LOCK_GRACE_SECONDS = 90


def _read_lock():
    try:
        with open(_lock_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def acquire_round_lock():
    """同一时刻只许一轮巡检动手。返回 (拿到没有, 说明)。

    按小时排的巡检可能和上一轮重叠：一轮要改代码、跑全量测试、再上云，
    可能超过一小时。两轮同时改同一棵树、同时上云，会把对方改到一半的
    东西一起发出去。锁超过 TTL 视为上一轮已经崩了，允许接管 ——
    否则一次异常退出就会让巡检永久停摆，那比撞车更难发现。
    """
    path = _lock_path()
    try:
        os.makedirs(_ops_dir(), exist_ok=True)
    except OSError as e:
        return False, {"error": type(e).__name__}
    payload = json.dumps({"at": time.time(), "holder": os.getenv("OPS_ROUND_HOLDER", "cron")})
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            prev = _read_lock()
            age = time.time() - float(prev.get("at") or 0)
            if prev and age < _lock_ttl_minutes() * 60:
                if age <= _LOCK_GRACE_SECONDS:
                    # 自己刚才拿的锁：续期放行，别把同一轮关在门外。
                    try:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(payload)
                    except OSError:
                        pass
                    return True, {"renewed": True}
                return False, {"holder": prev.get("holder"),
                               "age_minutes": round(age / 60.0, 1)}
            try:
                os.remove(path)
            except OSError:
                return False, {"error": "stale_lock_unremovable"}
            continue
        except OSError as e:
            return False, {"error": type(e).__name__}
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        return True, {}
    return False, {"error": "lock_contended"}


def release_round_lock():
    try:
        os.remove(_lock_path())
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 采集

def collect(with_isolated=True):
    import ops_remote
    evidence = {"at": _now(), "errors": [], "warnings": []}
    try:
        with ops_remote.Remote() as remote:
            evidence["health"] = ops_remote.health(remote)
            evidence["preflight_evidence"] = ops_remote.preflight(remote, "evidence")
            if with_isolated:
                evidence["preflight_isolated"] = ops_remote.preflight(remote, "isolated", timeout=400)
            evidence["readiness"] = ops_remote.readiness(remote)
            state = remote.call_ops("load_state")
            evidence["state"] = state.get("value") if state.get("ok") else {}
            if not state.get("ok"):
                # 运维模块还没上云时读不到状态，这不是采集失败：计划照样能算，
                # 但必须出声，否则"没有授权记录"会被误当成"主人没批过"。
                evidence["warnings"].append("state_unavailable:" + str(state.get("error")))
            evidence["remote"] = ops_remote.REMOTE
    except Exception as e:
        evidence["errors"].append("connect:%s:%s" % (type(e).__name__, str(e)[:200]))
    for key in ("health", "preflight_evidence", "readiness"):
        value = evidence.get(key)
        if not isinstance(value, dict) or not value.get("ok"):
            evidence["errors"].append(key + ":" + str((value or {}).get("error", "missing")))
    if not (evidence.get("preflight_evidence") or {}).get("tasks"):
        evidence["errors"].append("preflight_no_tasks")
    return evidence


def apply_safe_fixes(plan, evidence, dry_run=False):
    """只做机械修复：重生成工程证据、跑隔离探针、服务挂了才重启。

    绝不在这一步改算法、改配置、动她的人格数据。
    """
    import ops_remote
    results = []
    fixes = plan.get("safe_fixes") or []
    if not fixes:
        return results
    if dry_run:
        return [{"id": f["id"], "action": f["action"], "ok": None, "dry_run": True} for f in fixes]
    try:
        with ops_remote.Remote() as remote:
            for fix in fixes:
                action = fix.get("action")
                if action == "restart_qqbot":
                    res = ops_remote.restart_qqbot(remote)
                elif action == "run_engineering_contracts":
                    rc, text = remote.run(
                        "cd " + ops_remote.REMOTE + " && DATA_DIR=" + ops_remote.REMOTE + "/data"
                        " PERSONA_DATA_DIR=" + ops_remote.REMOTE + "/data/persona"
                        " QQ_DRY_RUN=1 PYTHONUTF8=1 " + ops_remote.REMOTE + "/venv/bin/python"
                        " -B bot/engineering_contracts.py", timeout=420)
                    res = {"ok": rc == 0, "exit": rc, "tail": text[-300:]}
                elif action == "run_isolated_preflight":
                    res = ops_remote.preflight(remote, "isolated", timeout=400)
                    res = {"ok": res.get("ok") and res.get("scope_status") != "unknown",
                           "scope_status": res.get("scope_status")}
                else:
                    res = {"ok": False, "error": "unknown_action"}
                results.append(dict(fix, result=res))
                print("[自动驾驶] 修复 %s -> %s" % (fix["id"], res.get("ok")), flush=True)
    except Exception as e:
        results.append({"id": "connect", "ok": False,
                        "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
    return results


def _organic_snapshot(evidence):
    """自然样本累积快照：每个有机门的当前读数，供面板看"有没有在涨"。"""
    rd = (evidence.get("readiness") or {}).get("phase25") or {}
    gates = {}
    for c in rd.get("checks") or []:
        gates[c.get("key")] = {"status": c.get("status"), "observed": c.get("observed")}
    return {"at": _now(), "gates": gates}


def push_plan(plan, evidence):
    """把 plan/health/检测快照写到云端，/ops plan 和 /ops status 才有东西可读。"""
    import ops_remote
    out = {"ok": True}
    try:
        with ops_remote.Remote() as remote:
            out["plan"] = remote.write_json("ops/plan.json", plan)
            # 推**隔离**报告：evidence 不跑探针，工程状态会永远停在 not_verified；
            # 隔离报告才带真实工程验证结果。没有隔离时退回 evidence。
            report = evidence.get("preflight_isolated") or evidence.get("preflight_evidence") or {}
            out["preflight"] = remote.write_json("ops/preflight.json", report)
            # 自然样本累积历史：每轮追加一条，面板据此画"有没有在累积"。
            hist = remote.read_json("ops/organic_history.json") or []
            if not isinstance(hist, list):
                hist = []
            hist.append(_organic_snapshot(evidence))
            out["organic_history"] = remote.write_json("ops/organic_history.json", hist[-200:])
            out["health"] = remote.call_ops("save_health", {"snapshot": plan.get("health") or {}}).get("ok")
            state = evidence.get("state") or {}
            # 注意：这里**不**标记「已提议」。没真发出去就不能记账，
            # 否则一次只 --push 不 --notify 的运行会把待批悄悄吞掉。
            out["state"] = remote.call_ops("save_state", {"state": state}).get("ok")
    except Exception as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    return out


def push_notifications(plan):
    import ops_remote
    sent = []
    try:
        with ops_remote.Remote() as remote:
            muted = False
            approval_ids = []
            for note in plan.get("notifications") or []:
                res = remote.call_ops("enqueue_outbox", {"text": note["text"], "kind": note["kind"]})
                sent.append({"kind": note["kind"], "ok": res.get("ok"),
                             "id": res.get("value") if res.get("ok") else res.get("error")})
                if res.get("ok") and note["kind"] == "approval":
                    approval_ids.extend(note.get("ids") or [])
                if res.get("ok") and note["kind"] != "approval":
                    muted = True
            marks = [n["dedup"] for n in plan.get("notifications") or [] if n.get("dedup")]
            if marks or muted or approval_ids:
                st = remote.call_ops("load_state")
                if st.get("ok"):
                    state = st.get("value") or {}
                    proposals = state.setdefault("proposals", {})
                    for mark in marks:
                        proposals[mark["kind"]] = {"sig": mark["sig"], "at": time.time()}
                    for tid in approval_ids:
                        # 真发出去了才记「已提议」，下一轮才不会重复问同一件事。
                        proposals[str(tid)] = _now()
                    if muted:
                        # 只有真的推出去了才开冷却，推送失败不能被记成"已经说过"。
                        proposals["last_push"] = {"at": time.time()}
                    remote.call_ops("save_state", {"state": state})
    except Exception as e:
        sent.append({"kind": "connect", "ok": False, "error": str(e)[:200]})
    return sent


def round_digest(report):
    """把轮次报告压成一条"做完了"的消息。没事发生就返回空串。"""
    advanced = [str(x) for x in (report.get("advanced") or [])]
    deploy = report.get("deploy") or {}
    failed = [str(x) for x in (report.get("attempted_but_failed") or [])]
    if not advanced and not deploy.get("deployed"):
        return ""
    lines = ["【运维】这轮做完了："]
    if advanced:
        lines.append("推进 " + "、".join(advanced))
    if deploy.get("deployed"):
        lines.append("已上云 %s 个文件，服务 %s" %
                     (deploy.get("remote_hash_ok") or "?", deploy.get("service") or "active"))
        if deploy.get("backup"):
            lines.append("回滚点 " + str(deploy["backup"]))
    if failed:
        lines.append("没做成：" + "、".join(failed[:4]))
    wait = [str(w) for w in (report.get("waiting_on") or [])]
    need = [str(n) for n in (report.get("needs_approval") or [])]
    if wait:
        lines.append("在等数据：" + "、".join(wait[:4]))
    if need:
        lines.append("等你点头：" + "、".join(need[:6]))
    return "\n".join(lines)


def save_round(report):
    """轮次结论同时落本地和云端，并给主人发一条「做完了」。

    `/ops report` 能翻到细节，但主人不该为了知道结果去翻命令 ——
    每轮收尾主动说一声，这才是"完成之后告诉我"。
    """
    import ops_channel
    import ops_remote
    rid = ops_channel.save_round(report)
    remote_ok = False
    digest = round_digest(report)
    notified = None
    try:
        with ops_remote.Remote() as remote:
            remote_ok = remote.call_ops("save_round", {"report": report}).get("ok", False)
            if digest:
                res = remote.call_ops("enqueue_outbox", {"text": digest, "kind": "round"})
                notified = res.get("value") if res.get("ok") else None
                if not res.get("ok"):
                    print("[自动驾驶] 收尾消息入队失败 " + str(res.get("error")), flush=True)
    except Exception as e:
        print("[自动驾驶] 轮次报告上云失败 " + type(e).__name__, flush=True)
    return {"id": rid, "local": True, "remote": remote_ok, "notified": notified}


def proactive_annotate(limit=40):
    """在云端主动做记忆来源标注：学主人的人工标注特征，按学习阈值自动绑定。

    记忆库在生产，本地没有，所以必须远程执行。只绑高置信/学习校准档，
    纯图片不自动绑。失败如实返回 error，不挡巡检。
    """
    import ops_remote
    code = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n" % (ops_remote.REMOTE + "/bot")
        + "import annotation_queue as aq\n"
        + "print(json.dumps(aq.auto_link(limit=%d), ensure_ascii=False))\n" % int(limit)
    )
    try:
        with ops_remote.Remote() as remote:
            rc, text = remote.python(code, timeout=180)
        if rc != 0:
            return {"ok": False, "error": "remote_failed", "detail": text[-200:]}
        return {"ok": True, **json.loads(text.strip().splitlines()[-1])}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "--plan"
    if mode == "--lock":
        ok, info = acquire_round_lock()
        print(json.dumps(dict(info, ok=ok), ensure_ascii=False))
        return 0 if ok else 6
    if mode == "--unlock":
        print(json.dumps({"ok": release_round_lock()}, ensure_ascii=False))
        return 0
    if mode == "--backup":
        print(json.dumps(local_code_backup(), ensure_ascii=False))
        return 0
    if mode == "--collect":
        print(json.dumps(collect(), ensure_ascii=False, default=str))
        return 0
    if mode == "--save-round":
        path = argv[1] if len(argv) > 1 else ""
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
        print(json.dumps(save_round(report), ensure_ascii=False))
        return 0
    if mode == "--notify":
        text = argv[1] if len(argv) > 1 else ""
        import ops_remote
        with ops_remote.Remote() as remote:
            print(json.dumps(remote.call_ops("enqueue_outbox", {"text": text, "kind": "manual"}),
                             ensure_ascii=False))
        return 0

    evidence = collect()
    if evidence["errors"]:
        print(json.dumps({"ok": False, "stage": "collect", "errors": evidence["errors"]},
                         ensure_ascii=False))
        return 3
    plan = build_plan(evidence)
    os.makedirs(_ops_dir(), exist_ok=True)
    import safe_store
    safe_store.atomic_write_json(os.path.join(_ops_dir(), "plan.json"), plan)
    if "--fix" in argv:
        guard = backup_before_fixes(plan)
        plan["backup"] = guard
        if guard["ok"]:
            plan["fix_results"] = apply_safe_fixes(plan, evidence)
            # 主动记忆标注：学主人的人工特征，在云端按学习阈值自动绑来源。
            plan["auto_annotate"] = proactive_annotate(limit=40)
        else:
            # 没留底就不动手，而且必须出声：静默跳过会被读成"修好了"。
            plan["fix_results"] = [{"id": "backup", "ok": False, "skipped": True,
                                    "error": (guard.get("persona") or {}).get("error", "backup_failed")}]
            plan["blocked"].append({"id": "backup", "why": "修复前备份失败，本轮没有动手"})
            plan["notifications"].append({
                "kind": "blocked", "ids": ["backup"],
                "text": "【运维】想自动修但备份没做成，这轮什么都没动：%s"
                        % str((guard.get("persona") or {}).get("error", ""))[:160]})
    if "--push" in argv:
        plan["push"] = push_plan(plan, evidence)
    if "--notify" in argv:
        plan["notified"] = push_notifications(plan)
    print(json.dumps(plan, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())