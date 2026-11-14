# -*- coding: utf-8 -*-
"""M07 私聊原始轮次情景化的回归锁（路线 §5 / §5.4 / §8.2）。

钉死的行为：
  · 正常情景化：user 命题逐字绑定真实事件（quote/offset 可复核），
    episode 经 M06 单一事务落账，只挂 bound 事件，不写信念；
  · 无来源轮次如实降级：legacy/unknown 分层登记，不冒充亲历，
    没有 bound 命题就不落 episode；
  · 重复幂等：稳定 dedupe 键与解释无关，同一轮次不产生重复 episode，
    epoch 不推进；
  · 失效源不复活：旧路径撤回与 M05 失效都挡下，episode 自身已失效的
    重复情景化被单调挡下；
  · 事务原子性：坏输入在任何写入之前整体拒绝；注入故障整体回滚；
    边注册失败出声且重跑幂等修复；
  · LLM 解释只进隔离候选（inferred claim），绝不写成事实；
  · 只读桥缺文件报 absent/degraded，绝不建库建表建目录（X15）。
AST 锁钉死：无写入 SQL、无旧写入器/直连、写入只走 M06/M05 既有入口、
open 只读、路径调用时解析。
"""
import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import private_turn_episodes as pte  # noqa: E402
import cognition_writer as cw        # noqa: E402
import invalidation_dag as dag       # noqa: E402
import self_model as sm              # noqa: E402

NOW = 2000.0
TABLES = ("belief", "belief_evidence", "claim_span", "claim_candidate",
          "episode", "commitment", "derivation_edge")


def source(event_id, text, **over):
    """手造冻结源记录（与 self_model.source_window 同一字段口径）。"""
    row = {"event_id": event_id, "kind": "private_message", "text": text,
           "source_revision": "rev_" + event_id, "occurred_at": 900.0,
           "session": "private:1001", "visibility": "private",
           "actor_ref": "per_1001", "actor_qq": "1001",
           "topic_ref": "", "topic_qq": "",
           "synthetic": False, "retracted": False}
    row.update(over)
    return row


def turn(**over):
    row = {"ts": 1000.0, "session": "private-user-1001",
           "user": "我今天好累", "bot": "辛苦啦，早点休息",
           "events": ["evt_u1"]}
    row.update(over)
    return row


class IdentityTests(unittest.TestCase):
    def test_same_turn_has_stable_identity(self):
        a = pte.turn_identity(turn())
        b = pte.turn_identity({"bot": "辛苦啦，早点休息", "events": ["evt_u1"],
                               "user": "我今天好累", "session": "private-user-1001",
                               "ts": 1000.0})
        self.assertTrue(a["ok"])
        self.assertEqual(a, b)
        self.assertTrue(a["object_id"].startswith("ptep_"))

    def test_identity_changes_with_each_component(self):
        base = pte.turn_identity(turn())["key"]
        for over in ({"session": "private-user-2002"}, {"ts": 1001.0},
                     {"user": "别的话"}, {"bot": "别的回应"}):
            self.assertNotEqual(base, pte.turn_identity(turn(**over))["key"], over)

    def test_events_and_interpretation_do_not_change_identity(self):
        a = pte.turn_identity(turn())
        self.assertEqual(a, pte.turn_identity(turn(events=[])))
        self.assertEqual(a, pte.turn_identity(turn(events=["evt_x", "evt_y"])))
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累")],
                             now=NOW, interpretation="她可能累了")
        self.assertEqual(a["object_id"], plan["object_id"])

    def test_missing_or_invalid_ts_is_invalid(self):
        for bad in (None, 0, -5, "1000", True):
            got = pte.turn_identity(turn(ts=bad))
            self.assertFalse(got["ok"], bad)
            self.assertIn("missing_turn_time", got["problems"])

    def test_non_private_session_is_invalid(self):
        got = pte.turn_identity(turn(session="group-55"))
        self.assertFalse(got["ok"])
        self.assertIn("not_private_session", got["problems"])
        self.assertIn("missing_session", pte.turn_identity(turn(session=" "))["problems"])

    def test_empty_turn_is_invalid(self):
        got = pte.turn_identity(turn(user="", bot="  "))
        self.assertFalse(got["ok"])
        self.assertIn("empty_turn", got["problems"])

    def test_non_dict_is_invalid(self):
        self.assertFalse(pte.turn_identity(None)["ok"])
        self.assertFalse(pte.turn_identity("一轮对话")["ok"])


class EpisodizeTests(unittest.TestCase):
    """纯函数核心：不碰账本，window/states 全部显式传入。"""

    def test_user_proposition_binds_verbatim_quote_with_offsets(self):
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累")], now=NOW)
        self.assertEqual("ready", plan["status"])
        user = plan["propositions"][0]
        self.assertEqual("bound", user["provenance"])
        self.assertEqual("我今天好累", user["quote"])
        self.assertEqual((0, 5), (user["span_start"], user["span_end"]))
        self.assertEqual("lexical", user["match_kind"])
        self.assertEqual("rev_evt_u1", user["source_revision"])
        cand = plan["candidate"]
        self.assertEqual("episode", cand["object_kind"])
        self.assertEqual("experienced", cand["epistemic_type"])
        self.assertEqual("private", cand["visibility"])
        self.assertEqual(["evt_u1"], cand["source_event_ids"])
        self.assertEqual(1000.0, cand["occurred_at"])
        self.assertIn("我今天好累", cand["content"])

    def test_assistant_without_event_source_is_legacy_not_fabricated(self):
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累")], now=NOW)
        bot = plan["propositions"][1]
        self.assertEqual("legacy", bot["provenance"])
        self.assertEqual("", bot["quote"])
        self.assertIsNone(bot["span_start"])
        self.assertIsNone(bot["span_end"])
        self.assertIn("quote_not_verbatim", bot["reasons"])
        self.assertIn("辛苦啦，早点休息", plan["candidate"]["content"])

    def test_turn_without_references_is_degraded_not_experienced(self):
        plan = pte.episodize(turn(events=[]), [], now=NOW)
        self.assertEqual("degraded", plan["status"])
        self.assertIsNone(plan["candidate"])
        self.assertIn("no_bound_source_event", plan["reasons"])
        self.assertEqual(["legacy", "legacy"],
                         [p["provenance"] for p in plan["propositions"]])

    def test_quote_not_verbatim_is_unknown(self):
        plan = pte.episodize(turn(), [source("evt_u1", "今天天气不错")], now=NOW)
        self.assertEqual("degraded", plan["status"])
        user = plan["propositions"][0]
        self.assertEqual("unknown", user["provenance"])
        self.assertIn("quote_not_verbatim", user["reasons"])
        self.assertEqual("", user["quote"])

    def test_ambiguous_source_event_is_not_guessed(self):
        window = [source("evt_u1", "我今天好累"), source("evt_u2", "我今天好累")]
        plan = pte.episodize(turn(events=["evt_u1", "evt_u2"]), window, now=NOW)
        self.assertEqual("degraded", plan["status"])
        self.assertIn("ambiguous_source_event", plan["propositions"][0]["reasons"])

    def test_retracted_source_is_blocked_not_resurrected(self):
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累", retracted=True)],
                             now=NOW)
        self.assertEqual("blocked", plan["status"])
        self.assertEqual("invalidated", plan["propositions"][0]["provenance"])
        self.assertIn("source_retracted", plan["propositions"][0]["reasons"])
        self.assertIsNone(plan["candidate"])

    def test_m05_invalidated_source_is_blocked(self):
        for status in ("invalidated", "superseded"):
            plan = pte.episodize(turn(), [source("evt_u1", "我今天好累")],
                                 now=NOW, states={"evt_u1": {"status": status}})
            self.assertEqual("blocked", plan["status"], status)
            self.assertIn("source_invalidated", plan["propositions"][0]["reasons"])

    def test_prior_episode_invalidation_is_monotonic(self):
        first = pte.episodize(turn(), [source("evt_u1", "我今天好累")], now=NOW)
        again = pte.episodize(turn(), [source("evt_u1", "我今天好累")], now=NOW,
                              states={first["episode_id"]: {"status": "invalidated"}})
        self.assertEqual("blocked", again["status"])
        self.assertIn("prior_invalidation_preserved", again["reasons"])
        self.assertIsNone(again["candidate"])

    def test_synthetic_turn_is_refused(self):
        plan = pte.episodize(turn(synthetic=True), [], now=NOW)
        self.assertEqual("refused", plan["status"])
        self.assertIn("synthetic_turn", plan["reasons"])

    def test_future_turn_is_refused(self):
        plan = pte.episodize(turn(ts=NOW + 10), [], now=NOW)
        self.assertEqual("refused", plan["status"])
        self.assertIn("future_turn", plan["reasons"])

    def test_probe_participant_is_refused(self):
        plan = pte.episodize(turn(session="private-user-t-999"), [], now=NOW)
        self.assertEqual("refused", plan["status"])
        self.assertIn("test_session", plan["reasons"])

    def test_synthetic_event_source_is_not_bound(self):
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累", synthetic=True)],
                             now=NOW)
        self.assertEqual("degraded", plan["status"])
        self.assertIn("synthetic_source", plan["propositions"][0]["reasons"])

    def test_event_outside_window_is_unknown(self):
        plan = pte.episodize(turn(events=["evt_missing"]), [], now=NOW)
        self.assertEqual("degraded", plan["status"])
        self.assertIn("event_outside_window", plan["propositions"][0]["reasons"])

    def test_summary_omits_unverified_clause(self):
        window = [source("evt_u1", "完全不同的话"),
                  source("evt_b1", "辛苦啦，早点休息")]
        plan = pte.episodize(turn(events=["evt_u1", "evt_b1"]), window, now=NOW)
        self.assertEqual("ready", plan["status"])
        self.assertEqual("她回应：「辛苦啦，早点休息」", plan["candidate"]["content"])
        self.assertIn("clause_omitted_unverified", plan["reasons"])
        self.assertEqual(["evt_b1"], plan["candidate"]["source_event_ids"])

    def test_long_clause_is_truncated_loudly_and_quote_stays_full(self):
        long_text = "累" * 200
        plan = pte.episodize(turn(user=long_text), [source("evt_u1", long_text)], now=NOW)
        self.assertEqual("ready", plan["status"])
        self.assertIn("clause_truncated", plan["reasons"])
        self.assertIn("累" * 150 + "…", plan["candidate"]["content"])
        self.assertNotIn("累" * 151, plan["candidate"]["content"])
        self.assertEqual(long_text, plan["propositions"][0]["quote"])

    def test_interpretation_is_candidate_only(self):
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累")], now=NOW,
                             interpretation="她可能累了，需要安慰")
        self.assertEqual("candidate_only", plan["interpretation"]["status"])
        self.assertNotIn("需要安慰", plan["candidate"]["content"])
        self.assertEqual("ready", plan["status"])

    def test_non_text_interpretation_is_invalid(self):
        plan = pte.episodize(turn(), [source("evt_u1", "我今天好累")],
                             now=NOW, interpretation=123)
        self.assertEqual("invalid", plan["status"])
        self.assertIn("interpretation_not_text", plan["reasons"])

    def test_plan_does_not_mutate_inputs(self):
        raw, window = turn(), [source("evt_u1", "我今天好累")]
        before = json.dumps([raw, window], sort_keys=True, ensure_ascii=False)
        pte.episodize(raw, window, now=NOW, interpretation="解释")
        self.assertEqual(before, json.dumps([raw, window], sort_keys=True, ensure_ascii=False))

    def test_only_bound_events_enter_source_event_ids(self):
        window = [source("evt_u1", "我今天好累"), source("evt_x", "整窗里别的事")]
        plan = pte.episodize(turn(), window, now=NOW)
        self.assertEqual(["evt_u1"], plan["candidate"]["source_event_ids"])
        self.assertEqual([("evt_u1", "private_turn.user")],
                         [(e["source_id"], e["basis"]) for e in plan["edges"]])

    def test_both_propositions_can_bind_one_echo_event(self):
        window = [source("evt_echo", "我今天好累 辛苦啦，早点休息")]
        plan = pte.episodize(turn(events=["evt_echo"]), window, now=NOW)
        self.assertEqual("ready", plan["status"])
        self.assertEqual(["bound", "bound"],
                         [p["provenance"] for p in plan["propositions"]])
        self.assertEqual(1, len(plan["edges"]))
        self.assertEqual("private_turn.both", plan["edges"][0]["basis"])

    def test_absent_text_proposition_is_absent(self):
        window = [source("evt_b1", "辛苦啦，早点休息")]
        plan = pte.episodize(turn(user="", events=["evt_b1"]), window, now=NOW)
        self.assertEqual("ready", plan["status"])
        self.assertEqual("absent", plan["propositions"][0]["provenance"])
        self.assertEqual("她回应：「辛苦啦，早点休息」", plan["candidate"]["content"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="m07_", ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._old = os.environ.get("DATA_DIR"), os.environ.get("PERSONA_DATA_DIR")
        os.environ["DATA_DIR"] = self.root
        os.environ.pop("PERSONA_DATA_DIR", None)

    def tearDown(self):
        for key, value in zip(("DATA_DIR", "PERSONA_DATA_DIR"), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_journal(self, rows):
        path = Path(self.root, "persona", "journal.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def counts(self):
        if not Path(self.root, "persona", "cognition.db").exists():
            return {t: 0 for t in TABLES}
        with closing(sm._db()) as connection:
            return {t: connection.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                    for t in TABLES}

    def rows(self, table):
        if not Path(self.root, "persona", "cognition.db").exists():
            return []
        with closing(sm._db()) as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM %s" % table)]


class BridgeTests(Base):
    def test_absent_journal_reports_absent_and_creates_nothing(self):
        got = pte.read_private_turns()
        self.assertEqual([], got["turns"])
        self.assertEqual([{"store": "journal", "reason": "absent"}], got["degradation"])
        self.assertEqual([], list(Path(self.root).iterdir()), "只读桥建了目录")

    def test_reads_private_turns_and_skips_group_and_probe(self):
        self.write_journal([
            {"ts": 100, "session": "private-user-1001", "user": "a", "bot": "b"},
            {"ts": 101, "session": "group-55", "user": "c", "bot": "d"},
            {"ts": 102, "session": "p-pool", "user": "e", "bot": "f"},
            {"ts": 103, "session": "private-user-t-9", "user": "g", "bot": "h"},
            {"ts": 104, "session": "private:1002", "user": "i", "bot": "j"},
        ])
        got = pte.read_private_turns()
        self.assertEqual(["private-user-1001", "private:1002"],
                         [r["session"] for r in got["turns"]])
        self.assertEqual(5, got["scanned"])
        self.assertEqual(1, got["skipped"])
        self.assertEqual([], got["degradation"])

    def test_since_ts_and_limit(self):
        self.write_journal([{"ts": t, "session": "private-user-1001",
                             "user": "u%d" % t, "bot": "b"} for t in (100, 200, 300)])
        self.assertEqual([200.0, 300.0],
                         [r["ts"] for r in pte.read_private_turns(since_ts=100)["turns"]])
        self.assertEqual(1, len(pte.read_private_turns(limit=1)["turns"]))

    def test_malformed_lines_are_skipped_not_fatal(self):
        path = Path(self.root, "persona", "journal.jsonl")
        path.parent.mkdir(parents=True)
        path.write_text('not json\n[1,2]\n' + json.dumps(
            {"ts": 100, "session": "private-user-1001", "user": "a", "bot": "b"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        got = pte.read_private_turns()
        self.assertEqual(1, len(got["turns"]))
        self.assertEqual(3, got["scanned"])
        self.assertEqual(2, got["skipped"])

    def test_unreadable_journal_degrades(self):
        path = Path(self.root, "persona", "journal.jsonl")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe\x00bad")
        got = pte.read_private_turns()
        self.assertEqual([{"store": "journal", "reason": "unreadable"}], got["degradation"])

    def test_bridge_never_modifies_journal(self):
        path = self.write_journal([{"ts": 100, "session": "private-user-1001",
                                    "user": "a", "bot": "b"}])
        before = path.read_bytes()
        pte.read_private_turns()
        pte.read_private_turns(since_ts=50, limit=1)
        self.assertEqual(before, path.read_bytes(), "只读桥改写了原始轮次")

    def test_freeze_window_absent_ledger_returns_empty_and_creates_nothing(self):
        self.assertEqual([], pte.freeze_window([turn()]))
        self.assertEqual([], list(Path(self.root).iterdir()), "只读冻结建了库")

    def test_freeze_window_reads_real_events(self):
        eid = sm.record_event("private_message", session="private:1001",
                              actor_qq="1001", payload={"text": "我今天好累"},
                              ts=1000.0, dedupe="m07:freeze")
        window = pte.freeze_window([turn(events=[eid]), turn(events=[eid])])
        self.assertEqual(1, len(window))
        self.assertEqual("我今天好累", window[0]["text"])
        self.assertFalse(window[0]["retracted"])

    def test_paths_resolve_at_call_time(self):
        self.write_journal([{"ts": 100, "session": "private-user-1001",
                             "user": "a", "bot": "b"}])
        self.assertEqual(1, len(pte.read_private_turns()["turns"]))
        with tempfile.TemporaryDirectory(prefix="m07_other_") as other:
            with patch.dict(os.environ, {"DATA_DIR": other}):
                self.assertEqual([], pte.read_private_turns()["turns"])
                self.assertTrue(pte.read_private_turns()["degradation"])
        self.assertEqual(1, len(pte.read_private_turns()["turns"]),
                         "路径若在 import 时冻住，这里会读到别人的库")


class CommitTests(Base):
    def seed(self, text, dedupe, ts=1000.0):
        return sm.record_event("private_message", session="private:1001",
                               actor_qq="1001", visibility="private",
                               payload={"text": text}, ts=ts, dedupe=dedupe)

    def test_commit_writes_episode_through_m06(self):
        eid = self.seed("我今天好累", "m07:c1")
        window = sm.source_window([eid])
        got = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        self.assertEqual("ok", got["status"])
        self.assertEqual(1, got["committed"])
        entry = got["episodes"][0]
        self.assertEqual("committed", entry["status"])
        self.assertEqual("accept", entry["decision"])
        episodes = self.rows("episode")
        self.assertEqual(1, len(episodes))
        self.assertEqual([eid], json.loads(episodes[0]["event_ids"]))
        self.assertEqual(1000.0, episodes[0]["ts"])
        self.assertEqual(0, episodes[0]["is_legacy"])
        self.assertEqual("self", episodes[0]["perspective"])
        self.assertIn("我今天好累", episodes[0]["summary"])
        self.assertEqual(entry["episode_id"], episodes[0]["id"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(0, self.counts()["claim_span"])

    def test_degraded_turn_writes_nothing(self):
        got = pte.commit_turns([turn(events=[])], [], now=NOW)
        self.assertEqual(1, got["degraded"])
        self.assertEqual(0, got["committed"])
        self.assertFalse(Path(self.root, "persona").exists(), "降级轮次不该触发任何写入")

    def test_repeat_commit_is_idempotent(self):
        eid = self.seed("我今天好累", "m07:c2")
        window = sm.source_window([eid])
        first = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        again = pte.commit_turns([turn(events=[eid])], window, now=NOW + 500)
        self.assertEqual(1, again["duplicates"])
        self.assertEqual(0, again["committed"])
        self.assertEqual(first["episodes"][0]["episode_id"],
                         again["episodes"][0]["episode_id"])
        self.assertEqual(1, self.counts()["episode"], "同一轮次变成了两条经历")

    def test_repeat_does_not_advance_epoch(self):
        eid = self.seed("我今天好累", "m07:c3")
        window = sm.source_window([eid])
        pte.commit_turns([turn(events=[eid])], window, now=NOW)
        pte.commit_turns([turn(events=[eid])], window, now=NOW + 500)
        state = dag.read_invalidation_state()
        self.assertEqual(0, state["epoch"], "情景化不是管理修正，不许推进 epoch")
        self.assertEqual(1, len(state["edges"]))

    def test_edges_registered_with_provenance_basis(self):
        eid = self.seed("我今天好累", "m07:c4")
        window = sm.source_window([eid])
        got = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        state = dag.read_invalidation_state()
        self.assertEqual(1, len(state["edges"]))
        edge = state["edges"][0]
        self.assertEqual(got["episodes"][0]["episode_id"], edge["derived_id"])
        self.assertEqual(eid, edge["source_id"])
        self.assertEqual("episode", edge["derived_kind"])
        self.assertEqual("private_turn.user", edge["basis"])
        self.assertEqual(1, got["edges_added"])

    def test_retracted_source_is_not_resurrected(self):
        eid = self.seed("我今天好累", "m07:c5")
        window = sm.source_window([eid])
        first = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        epid = first["episodes"][0]["episode_id"]
        sm.retract_evidence(eid, "主人撤回")
        refrozen = sm.source_window([eid])
        self.assertTrue(refrozen[0]["retracted"])
        again = pte.commit_turns([turn(events=[eid])], refrozen, now=NOW + 500)
        self.assertEqual(1, again["blocked"])
        self.assertEqual(0, again["committed"])
        self.assertIn("source_retracted", again["episodes"][0]["propositions"][0]["reasons"])
        self.assertEqual(1, self.counts()["episode"], "撤回后重复情景化堆了新经历")
        self.assertTrue(sm.get_event(eid), "撤回删除了原始观察")
        self.assertFalse(sm.evidence_valid("episode", epid), "撤回的源仍是有效依据")

    def test_m05_invalidation_blocks_recommit(self):
        eid = self.seed("我今天好累", "m07:c6")
        window = sm.source_window([eid])
        pte.commit_turns([turn(events=[eid])], window, now=NOW)
        state = dag.read_invalidation_state()
        rec = dag.correction("observation_invalid", eid, "误读", ts=1.0)
        plan = dag.propagate(state["edges"], [rec], epoch=state["epoch"],
                             states=state["states"], now=NOW)
        sm.write_invalidation_transaction({
            "ts": NOW, "expected_epoch": plan["expected_epoch"],
            "edges": state["edges"], "corrections": plan["corrections"],
            "propagation": plan})
        again = pte.commit_turns([turn(events=[eid])], sm.source_window([eid]), now=NOW + 500)
        self.assertEqual(1, again["blocked"])
        self.assertEqual("invalidated",
                         dag.read_invalidation_state()["states"][eid]["status"])
        self.assertEqual(1, self.counts()["episode"])

    def test_interpretation_goes_to_quarantine_not_fact(self):
        eid = self.seed("我今天好累", "m07:c7")
        window = sm.source_window([eid])
        raw = turn(events=[eid])
        ident = pte.turn_identity(raw)["object_id"]
        got = pte.commit_turns([raw], window, now=NOW,
                               interpretations={ident: "她可能累了，需要安慰"})
        entry = got["episodes"][0]
        self.assertEqual("quarantine", entry["interpretation"]["decision"])
        candidates = self.rows("claim_candidate")
        self.assertEqual(1, len(candidates))
        self.assertEqual("deferred", candidates[0]["decision"])
        self.assertEqual("pending_review", candidates[0]["state"])
        self.assertIn("inferred_claim", json.loads(candidates[0]["reasons"]))
        self.assertEqual(0, self.counts()["belief"], "LLM 解释写成了事实")
        self.assertNotIn("需要安慰", self.rows("episode")[0]["summary"])

    def test_interpretation_repeat_is_idempotent(self):
        eid = self.seed("我今天好累", "m07:c8")
        window = sm.source_window([eid])
        raw = turn(events=[eid])
        ident = pte.turn_identity(raw)["object_id"]
        inter = {ident: "她可能累了"}
        pte.commit_turns([raw], window, now=NOW, interpretations=inter)
        pte.commit_turns([raw], window, now=NOW + 500, interpretations=inter)
        self.assertEqual(1, self.counts()["claim_candidate"])
        self.assertEqual(1, self.counts()["episode"])

    def test_interpretation_for_degraded_turn_is_still_candidate(self):
        raw = turn(events=[])
        ident = pte.turn_identity(raw)["object_id"]
        got = pte.commit_turns([raw], [], now=NOW,
                               interpretations={ident: "她可能累了"})
        self.assertEqual(1, got["degraded"])
        self.assertEqual("quarantine", got["episodes"][0]["interpretation"]["decision"])
        self.assertEqual(1, self.counts()["claim_candidate"])
        self.assertEqual(0, self.counts()["episode"])

    def test_bad_turn_rolls_back_whole_batch(self):
        eid = self.seed("我今天好累", "m07:c9")
        window = sm.source_window([eid])
        bad = {"session": "private-user-1001", "user": "x", "bot": "y"}  # 缺 ts
        with self.assertRaises(ValueError):
            pte.commit_turns([turn(events=[eid]), bad], window, now=NOW)
        self.assertEqual({t: 0 for t in TABLES}, self.counts(), "坏输入留下了半截账")

    def test_group_turn_in_batch_is_rejected(self):
        eid = self.seed("我今天好累", "m07:c10")
        window = sm.source_window([eid])
        with self.assertRaises(ValueError):
            pte.commit_turns([turn(events=[eid]), turn(session="group-55")], window, now=NOW)
        self.assertEqual(0, self.counts()["episode"])

    def test_bad_interpretation_rolls_back_whole_batch(self):
        eid = self.seed("我今天好累", "m07:c11")
        window = sm.source_window([eid])
        raw = turn(events=[eid])
        ident = pte.turn_identity(raw)["object_id"]
        with self.assertRaises(ValueError):
            pte.commit_turns([raw], window, now=NOW, interpretations={ident: 123})
        with self.assertRaises(ValueError):
            pte.commit_turns([raw], window, now=NOW, interpretations="不是字典")
        self.assertEqual({t: 0 for t in TABLES}, self.counts())

    def test_injected_episode_failure_rolls_back_and_retry_works(self):
        eid = self.seed("我今天好累", "m07:c12")
        window = sm.source_window([eid])
        real_db = sm._db

        class FailingConnection:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if "INTO episode" in sql:
                    raise sqlite3.OperationalError("injected disk failure")
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch.object(sm, "_db", lambda: FailingConnection(real_db())):
            with self.assertRaises(sqlite3.OperationalError):
                pte.commit_turns([turn(events=[eid])], window, now=NOW)
        self.assertEqual(0, self.counts()["episode"], "回滚后残留了半条经历")
        self.assertEqual(0, self.counts()["derivation_edge"])
        got = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        self.assertEqual(1, got["committed"])
        self.assertEqual(1, self.counts()["episode"])

    def test_edge_failure_is_loud_and_rerun_repairs(self):
        eid = self.seed("我今天好累", "m07:c13")
        window = sm.source_window([eid])
        with patch.object(sm, "write_invalidation_transaction",
                          side_effect=RuntimeError("injected edge failure")):
            with self.assertRaises(RuntimeError):
                pte.commit_turns([turn(events=[eid])], window, now=NOW)
        self.assertEqual(1, self.counts()["episode"], "episode 事务本身应已原子落账")
        self.assertEqual(0, self.counts()["derivation_edge"])
        again = pte.commit_turns([turn(events=[eid])], window, now=NOW + 500)
        self.assertEqual(1, again["duplicates"])
        self.assertEqual(1, again["edges_added"], "重跑没有修复缺失的依赖边")
        self.assertEqual(1, self.counts()["episode"])

    def test_gate_demotion_registers_no_edges(self):
        eid = self.seed("我今天好累", "m07:c14")
        window = sm.source_window([eid])
        real_commit = cw.commit

        def demote(candidate, win, now=None):
            if candidate.get("object_kind") == "episode":
                return {"decision": "quarantine", "reasons": ["injected_demotion"],
                        "object_ids": {}}
            return real_commit(candidate, win, now=now)

        with patch.object(cw, "commit", demote):
            got = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        self.assertEqual(1, got["deferred"])
        self.assertEqual(0, got["edges_added"], "给不存在的 episode 注册了依赖边")
        self.assertEqual(0, self.counts()["episode"])
        self.assertEqual(0, self.counts()["derivation_edge"])

    def test_projection_carries_status_provenance_limitations(self):
        eid = self.seed("我今天好累", "m07:c15")
        window = sm.source_window([eid])
        got = pte.commit_turns([turn(events=[eid])], window, now=NOW)
        self.assertEqual({"status", "committed", "duplicates", "degraded", "refused",
                          "blocked", "deferred", "rejected", "episodes", "edges_added",
                          "epoch", "provenance", "limitations"}, set(got))
        self.assertEqual("m07-1", got["provenance"]["algorithm_version"])
        self.assertEqual(1, got["provenance"]["window_size"])
        self.assertTrue(any("M08" in line for line in got["limitations"]))
        self.assertTrue(any("legacy" in line for line in got["limitations"]))
        self.assertTrue(any("两个既有事务" in line for line in got["limitations"]))

    def test_empty_batch_writes_nothing(self):
        got = pte.commit_turns([], [], now=NOW)
        self.assertEqual("ok", got["status"])
        self.assertEqual(0, got["committed"])
        self.assertEqual([], got["episodes"])
        self.assertFalse(Path(self.root, "persona").exists())

    def test_end_to_end_bridge_freeze_commit(self):
        eid = self.seed("我今天好累", "m07:e2e")
        path = self.write_journal([
            {"ts": 1000.0, "session": "private-user-1001",
             "user": "我今天好累", "bot": "辛苦啦，早点休息", "events": [eid]}])
        before = path.read_bytes()
        turns = pte.read_private_turns()["turns"]
        window = pte.freeze_window(turns)
        got = pte.commit_turns(turns, window, now=NOW)
        self.assertEqual(1, got["committed"])
        self.assertIn("我今天好累", self.rows("episode")[0]["summary"])
        self.assertEqual(before, path.read_bytes(), "情景化改写了原始轮次")


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "private_turn_episodes.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def calls(self, tree):
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def call_name(self, call):
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return ""

    def test_imports_only_contracts_and_ledger(self):
        allowed = {"hashlib", "json", "os", "time",
                   "claim_provenance", "cognition_writer", "invalidation_dag", "self_model"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             "情景化模块 import 了写入/网络/模型 API：%s" % sorted(names - allowed))

    def test_no_write_sql_anywhere_in_the_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper,
                                     "情景化模块里出现写入 SQL：%s" % node.value[:60])

    def test_no_legacy_writers_or_direct_connections(self):
        forbidden = {"add_belief", "record_episode", "quarantine_claim", "record_event",
                     "note_self_view", "revise_belief", "retract_evidence", "add_evidence",
                     "add_commitment", "add_goal", "record_action", "_db", "_transaction",
                     "executescript", "executemany", "rollback", "makedirs", "mkdir",
                     "write_text", "write_bytes", "remove", "unlink", "rmtree", "rename"}
        for call in self.calls(self.tree):
            self.assertNotIn(self.call_name(call), forbidden,
                             "情景化模块绕过既有入口写账：%s" % self.call_name(call))
        self.assertNotIn("sqlite3", self.source)

    def test_writes_go_only_through_m06_and_m05_entries(self):
        commits, edge_writes = [], []
        for call in self.calls(self.tree):
            name = self.call_name(call)
            if name == "commit":
                self.assertIsInstance(call.func, ast.Attribute)
                self.assertIsInstance(call.func.value, ast.Name)
                self.assertEqual("cognition_writer", call.func.value.id,
                                 "commit 只能调 cognition_writer 的单一入口")
                commits.append(call)
            if name == "write_invalidation_transaction":
                self.assertIsInstance(call.func, ast.Attribute)
                self.assertEqual("self_model", call.func.value.id)
                edge_writes.append(call)
        self.assertTrue(commits, "不再经 M06 落账了")
        self.assertTrue(edge_writes, "不再经 M05 注册依赖边了")

    def test_open_is_readonly(self):
        for call in self.calls(self.tree):
            if self.call_name(call) != "open":
                continue
            if len(call.args) > 1:
                self.assertIsInstance(call.args[1], ast.Constant)
                self.assertIn(call.args[1].value, ("r", "rt", "rb"))
            for keyword in call.keywords:
                if keyword.arg == "mode":
                    self.assertIn(keyword.value.value, ("r", "rt", "rb"))

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径，换 DATA_DIR 的测试会读到别人的库")

    def test_path_resolution_lives_only_inside_the_read_bridge(self):
        bridge = {"_data_root", "_journal_path", "read_private_turns"}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name in bridge:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "isfile"),
                                     "%s 里解析了路径；核心情景化必须是纯函数" % node.name)

    def test_status_and_provenance_sets_are_fixed(self):
        self.assertEqual(("ready", "degraded", "refused", "blocked", "invalid"),
                         pte.PLAN_STATUSES)
        self.assertEqual(("bound", "legacy", "unknown", "invalidated", "absent"),
                         pte.PROP_PROVENANCE)

    def test_limitations_are_honest(self):
        text = " ".join(pte.LIMITATIONS)
        self.assertIn("M08", text)
        self.assertIn("不改线", text)
        self.assertIn("不补造", text)


if __name__ == "__main__":
    unittest.main()