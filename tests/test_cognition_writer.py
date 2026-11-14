# -*- coding: utf-8 -*-
"""M06 单一认知写入与投影事务：回归锁。

AST 静态锁钉死四件事：
  · 准入门（M01）与来源门（M02）在任何落账之前跑；
  · 多对象写入全部在一个 _transaction() 里，写入器不绕开它开连接；
  · self_model 现有函数的签名与旧写入路径原样保留（只新增，不改线）；
  · 不 import retrieval_objects（R01 仍在只观察阶段）。

行为锁钉死：accept 写 belief+spans+evidence、quarantine 写候选、reject
不写已核验事实、中途失败全回滚无残留、投影正确、同一候选重复提交幂等。
"""
import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import cognition_writer as cw   # noqa: E402
import self_model as sm         # noqa: E402

NOW = 2000.0


# ── AST 静态锁 ──────────────────────────────────────────────
class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "cognition_writer.py"), encoding="utf-8") as handle:
            cls.writer_source = handle.read()
        cls.writer = ast.parse(cls.writer_source)
        with open(os.path.join(BOT, "self_model.py"), encoding="utf-8") as handle:
            cls.ledger_source = handle.read()
        cls.ledger = ast.parse(cls.ledger_source)

    def function(self, tree, name):
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail("缺少函数 %s" % name)

    def calls(self, node):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)]

    def call_name(self, call):
        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return ""

    def test_admission_and_provenance_gates_run_before_any_write(self):
        commit = self.function(self.writer, "commit")
        calls = self.calls(commit)
        admit = [c for c in calls if self.call_name(c) == "admit"]
        bind = [c for c in calls if self.call_name(c) == "bind_claim"]
        writes = [c for c in calls if self.call_name(c) == "write_cognition_transaction"]
        self.assertTrue(admit, "统一入口不再跑准入门了")
        self.assertTrue(bind, "统一入口不再跑来源门了")
        self.assertTrue(writes, "统一入口不再落账了")
        first_write = min(c.lineno for c in writes)
        self.assertLess(max(c.lineno for c in admit), first_write,
                        "准入门必须在任何写入之前")
        self.assertLess(max(c.lineno for c in bind), first_write,
                        "来源门必须在任何写入之前")
        self.assertLess(max(c.lineno for c in admit), min(c.lineno for c in bind),
                        "先准入、后来源的固定顺序不能反")

    def test_writer_never_calls_legacy_writers_or_opens_connections(self):
        forbidden = {"add_belief", "record_episode", "quarantine_claim", "add_commitment",
                     "add_evidence", "record_event", "record_action", "note_self_view",
                     "add_goal", "revise_belief", "_db", "_transaction"}
        for call in self.calls(self.writer):
            self.assertNotIn(self.call_name(call), forbidden,
                             "写入必须只走 write_cognition_transaction，不许旁路")
        self.assertNotIn("sqlite3", self.writer_source)

    def test_writer_imports_only_the_contracts_and_the_ledger(self):
        allowed = {"time", "cognition_admission", "claim_provenance", "self_model"}
        for node in ast.walk(self.writer):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], allowed)

    def test_writer_does_not_import_retrieval_objects(self):
        wanted = {"retrieval_objects", "retrieve_evidence", "fetch_retrieval_items"}
        for node in ast.walk(self.writer):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            elif isinstance(node, ast.Call):
                names = {self.call_name(node)}
            else:
                continue
            self.assertFalse(names & wanted,
                             "R01 还在只观察阶段，M06 不许接入检索对象")

    def test_transaction_helper_writes_every_insert_inside_one_transaction(self):
        helper = self.function(self.ledger, "write_cognition_transaction")
        transactions = [w for w in ast.walk(helper) if isinstance(w, ast.With)
                        and any(isinstance(item.context_expr, ast.Call)
                                and self.call_name(item.context_expr) == "_transaction"
                                for item in w.items)]
        self.assertEqual(1, len(transactions), "必须恰好一个 _transaction() 包住全部写入")
        inside = {id(n) for n in ast.walk(transactions[0])}
        inserts = [c for c in self.calls(helper)
                   if self.call_name(c) == "execute" and c.args
                   and isinstance(c.args[0], ast.Constant)
                   and "INSERT" in str(c.args[0].value)]
        self.assertTrue(inserts)
        for call in inserts:
            self.assertIn(id(call), inside, "有 INSERT 落在事务外面")
        for call in self.calls(helper):
            # 这些函数各自开连接：在 BEGIN IMMEDIATE 里调它们会锁死或破坏原子性。
            self.assertNotIn(self.call_name(call),
                             {"_db", "person_id", "add_belief", "record_episode",
                              "quarantine_claim", "add_commitment", "add_evidence",
                              "record_event", "commit", "rollback"})

    def test_existing_signatures_are_pinned(self):
        # M06 只许新增函数：线上 bot 在用的旧 API 签名一个都不许动。
        expected = {
            "person_id": (["qq", "alias"], [], 1),
            "record_event": (["kind", "ts", "session", "actor_qq", "topic_qq",
                              "source", "visibility", "payload", "dedupe"], [], 8),
            "record_tool_result": (["tool", "ref", "ok"],
                                   ["session", "visibility", "reason", "ts",
                                    "run_id", "outcome", "evidence_origin"], 1),
            "record_episode": (["summary", "event_ids", "ts", "perspective", "is_legacy"], [], 4),
            "add_belief": (["proposition", "subject_qq", "confidence", "source",
                            "evidence", "ts", "is_legacy", "spans"], [], 7),
            "quarantine_claim": (["restatement", "kind", "reasons", "evidence", "subject_ref",
                                  "source_session", "visibility", "candidate_confidence",
                                  "decision", "ts"], [], 9),
            "add_evidence": (["belief_id", "event_id", "relation", "weight", "ts"], [], 3),
            "revise_belief": (["belief_id", "new_text", "reason", "evidence",
                               "confidence", "ts"], [], 4),
            "add_commitment": (["to_qq", "text", "due_ts", "evidence", "ts"], [], 3),
            "add_goal": (["text", "owner_qq", "motive", "origin", "success",
                          "parent_event", "ts"], [], 6),
            "record_action": (["kind", "target_qq", "topic_qq", "visibility", "scope",
                               "text", "reason", "goal_id", "evidence", "state", "ts"], [], 10),
            "mark_action": (["action_id", "state", "result", "source"], [], 2),
            "note_self_view": (["text", "reason", "evidence", "ts"], [], 3),
            "retract_evidence": (["event_id", "reason"], [], 0),
            "source_window": (["event_ids", "now"], [], 1),
            "_transaction": ([], [], 0),
        }
        for name, (args, kwonly, defaults) in expected.items():
            fn = self.function(self.ledger, name)
            self.assertEqual(args, [a.arg for a in fn.args.args], name)
            self.assertEqual(kwonly, [a.arg for a in fn.args.kwonlyargs], name)
            self.assertEqual(defaults, len(fn.args.defaults), name)

    def test_existing_writers_are_not_rewired(self):
        # 旧写入器不许被改线到新事务（那是 M07/M08），也不许反过来调新函数。
        for node in self.ledger.body:
            if isinstance(node, ast.FunctionDef) and node.name != "write_cognition_transaction":
                for call in self.calls(node):
                    self.assertNotIn(self.call_name(call), {"write_cognition_transaction"},
                                     "旧函数 %s 被改线了" % node.name)
        for name in ("add_belief", "record_episode", "quarantine_claim"):
            fn = self.function(self.ledger, name)
            names = {self.call_name(c) for c in self.calls(fn)}
            self.assertIn("_db", names, "%s 不再用自己的连接了" % name)
            self.assertNotIn("_transaction", names)
        tx = self.function(self.ledger, "_transaction")
        segment = ast.get_source_segment(self.ledger_source, tx)
        self.assertIn("BEGIN IMMEDIATE", segment)
        self.assertIn("rollback", segment)
        self.assertIn("close", segment)


# ── 行为锁：隔离账本夹具 ─────────────────────────────────────
class LedgerIsolated(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m06_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def event(self, text, actor="1001", session="private:1001"):
        return sm.record_event("private_message", session=session, actor_qq=actor,
                               visibility="private", payload={"text": text}, ts=1000.0,
                               dedupe="m06:" + text)

    def counts(self):
        with closing(sm._db()) as connection:
            return {table: connection.execute(
                "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                for table in ("belief", "belief_evidence", "claim_span",
                              "claim_candidate", "episode", "commitment")}

    def rows(self, table):
        with closing(sm._db()) as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM %s" % table)]


class WriterFixture(LedgerIsolated):
    def setUp(self):
        super().setUp()
        self.eid = self.event("我最喜欢喝冰可乐了")
        self.window = sm.source_window([self.eid])
        self.actor_ref = self.window[0]["actor_ref"]

    def claim(self, **over):
        base = {"object_kind": "claim", "object_id": "obj_1",
                "content": "喜欢冰可乐",
                "occurred_at": 1000.0, "recorded_at": 1001.0,
                "source_event_ids": [self.eid], "epistemic_type": "reported",
                "visibility": "private", "source_session": "private:1001",
                "subject_ref": self.actor_ref,
                "evidence": [{"event_id": self.eid, "quote": "我最喜欢喝冰可乐了"}]}
        base.update(over)
        return base


class AcceptTests(WriterFixture):
    def test_accept_writes_belief_spans_and_evidence_atomically(self):
        got = cw.commit(self.claim(), self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertEqual("accepted", got["provenance_decision"])
        bid = got["object_ids"].get("belief")
        self.assertTrue(bid)
        beliefs = self.rows("belief")
        self.assertEqual(1, len(beliefs))
        self.assertEqual("喜欢冰可乐", beliefs[0]["proposition"])
        self.assertEqual(self.actor_ref, beliefs[0]["subject_qq"])
        self.assertEqual(0, beliefs[0]["is_legacy"])
        spans = sm.belief_spans(bid)
        self.assertEqual(1, len(spans))
        self.assertEqual("我最喜欢喝冰可乐了", spans[0]["quote"])
        text = json.loads(sm.get_event(self.eid)["payload"])["text"]
        self.assertEqual(spans[0]["quote"],
                         text[spans[0]["span_start"]:spans[0]["span_end"]],
                         "存下来的 quote 必须能在真实源文本里逐字复核")
        self.assertEqual(self.actor_ref, spans[0]["subject_ref"])
        self.assertEqual("source_actor", spans[0]["subject_basis"])
        self.assertEqual([self.eid], [r["event_id"] for r in self.rows("belief_evidence")])
        self.assertEqual(0, self.counts()["claim_candidate"])

    def test_whole_window_events_are_not_spread_over_the_belief(self):
        # X04：候选信封里带整窗事件号，信念的依据也只能是它自己那句原话。
        second = self.event("今天天气不错")
        third = self.event("晚上吃什么")
        got = cw.commit(self.claim(source_event_ids=[self.eid, second, third]),
                        self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertEqual([self.eid], [r["event_id"] for r in self.rows("belief_evidence")])

    def test_belief_kind_goes_through_the_same_gate(self):
        got = cw.commit(self.claim(object_kind="belief"), self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertEqual(1, self.counts()["belief"])
        self.assertEqual(1, self.counts()["claim_span"])

    def test_preference_self_report_is_recorded_without_generalization(self):
        got = cw.commit(self.claim(object_kind="preference",
                                   content="对方明确自述：喜欢冰可乐",
                                   explicit_self_report=True, actor_is_subject=True),
                        self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertIn("statement_not_generalized", got["reasons"])
        beliefs = self.rows("belief")
        self.assertEqual(1, len(beliefs))
        self.assertEqual("对方明确自述：喜欢冰可乐", beliefs[0]["proposition"],
                         "只收自述命题本身，不许擅自推广")

    def test_episode_accept_writes_episode_not_belief(self):
        got = cw.commit(self.claim(object_kind="episode", epistemic_type="experienced",
                                   content="她看到朋友晒出了抽卡结果"),
                        self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertTrue(got["object_ids"].get("episode"))
        episodes = self.rows("episode")
        self.assertEqual(1, len(episodes))
        self.assertEqual([self.eid], json.loads(episodes[0]["event_ids"]))
        self.assertEqual(1000.0, episodes[0]["ts"])
        self.assertEqual(0, self.counts()["belief"])

    def test_commitment_accept_writes_commitment_with_stable_person(self):
        got = cw.commit(self.claim(object_kind="commitment", epistemic_type="experienced",
                                   content="周五前把图发给他", to_qq="1001",
                                   accepted_by_self=True,
                                   completion_criteria="周五前把图发给他"),
                        self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        commitments = self.rows("commitment")
        self.assertEqual(1, len(commitments))
        self.assertEqual("per_" + sm._sha("1001"), commitments[0]["to_qq"])
        self.assertEqual("open", commitments[0]["status"])
        self.assertEqual([self.eid], json.loads(commitments[0]["event_ids"]))
        self.assertEqual(0, self.counts()["belief"])

    def test_state_accept_persists_no_cognition_object(self):
        # 临时状态属于情感/工作状态层：认知账本不替它造表，也不塞进长期事实。
        live = cw.commit(self.claim(object_kind="state", epistemic_type="experienced",
                                    content="现在有点困", valid_until=NOW + 3600),
                         self.window, now=NOW)
        self.assertEqual("accept", live["decision"])
        self.assertTrue(live["current"])
        self.assertEqual({}, live["object_ids"])
        self.assertIn("state_layer_not_in_cognition_ledger", live["reasons"])
        expired = cw.commit(self.claim(object_kind="state", epistemic_type="experienced",
                                       content="现在有点困", valid_until=NOW - 1),
                            self.window, now=NOW)
        self.assertEqual("accept", expired["decision"])
        self.assertFalse(expired["current"], "过期状态不能再当「现在」注入")
        self.assertEqual({t: 0 for t in self.counts()}, self.counts())

    def test_model_confidence_is_registered_but_never_decides(self):
        high = cw.commit(self.claim(candidate_confidence=0.99), self.window, now=NOW)
        self.assertEqual("accept", high["decision"])
        self.assertEqual(0.99, high["candidate_confidence"])
        self.assertIsNone(self.rows("belief")[0]["confidence"],
                          "自报置信度不许写进已核验信念")
        groundless = cw.commit(self.claim(object_id="obj_2", evidence=[],
                                          candidate_confidence=0.99),
                               self.window, now=NOW)
        self.assertEqual("quarantine", groundless["decision"],
                         "自报置信度再高也不能替代依据")
        self.assertEqual(0.99, groundless["candidate_confidence"])


class QuarantineTests(WriterFixture):
    def test_no_evidence_span_is_quarantined_not_fact(self):
        got = cw.commit(self.claim(evidence=[]), self.window, now=NOW)
        self.assertEqual("quarantine", got["decision"])
        self.assertFalse(got["current"])
        self.assertIn("no_evidence_span", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])
        candidates = self.rows("claim_candidate")
        self.assertEqual(1, len(candidates))
        self.assertEqual("pending_review", candidates[0]["state"])
        self.assertEqual("deferred", candidates[0]["decision"])
        self.assertIn("no_evidence_span", json.loads(candidates[0]["reasons"]))
        self.assertEqual(got["object_ids"].get("candidate"), candidates[0]["id"])

    def test_inferred_claim_stays_a_candidate(self):
        got = cw.commit(self.claim(epistemic_type="inferred"), self.window, now=NOW)
        self.assertEqual("quarantine", got["decision"])
        self.assertIn("inferred_claim", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(1, self.counts()["claim_candidate"])

    def test_preference_without_self_report_is_quarantined(self):
        got = cw.commit(self.claim(object_kind="preference"), self.window, now=NOW)
        self.assertEqual("quarantine", got["decision"])
        self.assertIn("preference_needs_self_report", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])

    def test_provenance_deferral_demotes_an_admitted_claim(self):
        # 准入通过、来源暂缓：两道门取更严的，绝不「先写了再说」。
        event = self.event("我今天可能去教室")
        window = sm.source_window([event])
        got = cw.commit(self.claim(content="去教室", source_event_ids=[event],
                                   evidence=[{"event_id": event, "quote": "我今天可能去教室"}],
                                   subject_ref=window[0]["actor_ref"]),
                        window, now=NOW)
        self.assertEqual("quarantine", got["decision"])
        self.assertIn("qualifier_dropped", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(1, self.counts()["claim_candidate"])

    def test_quarantine_is_idempotent(self):
        first = cw.commit(self.claim(evidence=[]), self.window, now=NOW)
        again = cw.commit(self.claim(evidence=[]), self.window, now=NOW + 500)
        self.assertEqual(first["object_ids"], again["object_ids"])
        self.assertEqual(1, self.counts()["claim_candidate"], "同一候选反复整理堆了垃圾")


class RejectTests(WriterFixture):
    def test_fiction_belief_is_rejected_and_leaves_no_fact(self):
        got = cw.commit(self.claim(object_kind="belief", context_kind="roleplay"),
                        self.window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("fiction_not_fact", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])
        candidates = self.rows("claim_candidate")
        self.assertEqual(1, len(candidates), "拒绝要留 rejected 候选供审计")
        self.assertEqual("rejected", candidates[0]["decision"])

    def test_negation_dropped_is_rejected(self):
        event = self.event("我不喜欢牛肉面")
        window = sm.source_window([event])
        got = cw.commit(self.claim(content="喜欢牛肉面", source_event_ids=[event],
                                   evidence=[{"event_id": event, "quote": "我不喜欢牛肉面"}],
                                   subject_ref=window[0]["actor_ref"]),
                        window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("negation_dropped", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual("rejected", self.rows("claim_candidate")[0]["decision"])

    def test_event_outside_the_frozen_window_is_rejected(self):
        got = cw.commit(self.claim(evidence=[{"event_id": "evt_999", "quote": "冰可乐"}]),
                        self.window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("event_outside_window", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])

    def test_empty_window_rejects_the_claim(self):
        got = cw.commit(self.claim(), [], now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertEqual(0, self.counts()["belief"])

    def test_rejected_episode_leaves_no_candidate(self):
        got = cw.commit(self.claim(object_kind="episode", epistemic_type="reported"),
                        self.window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("not_her_experience", got["reasons"])
        self.assertEqual({t: 0 for t in self.counts()}, self.counts(),
                         "拒绝的经历不许留下任何账")

    def test_synthetic_source_is_rejected(self):
        got = cw.commit(self.claim(synthetic=True), self.window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("synthetic_source", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])

    def test_reject_is_idempotent(self):
        first = cw.commit(self.claim(object_kind="belief", context_kind="roleplay"),
                          self.window, now=NOW)
        again = cw.commit(self.claim(object_kind="belief", context_kind="roleplay"),
                          self.window, now=NOW + 500)
        self.assertEqual(first["object_ids"], again["object_ids"])
        self.assertEqual(1, self.counts()["claim_candidate"])


class AtomicityTests(WriterFixture):
    def test_helper_rolls_back_all_objects_on_mid_transaction_failure(self):
        # belief 与 evidence 已插入、span 插入中途失败：必须全部回滚。
        plan = {"ts": NOW, "belief": {
            "object_id": "obj_bad", "proposition": "命题", "subject_ref": "",
            "confidence": None, "source": "test",
            "spans": [{"event_id": self.eid, "quote": "冰可乐",
                       "span_start": "bad", "span_end": 3}]}}
        with self.assertRaises(ValueError):
            sm.write_cognition_transaction(plan)
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(0, self.counts()["belief_evidence"])
        self.assertEqual(0, self.counts()["claim_span"])

    def test_commit_rolls_back_when_a_span_insert_fails(self):
        real_db = sm._db

        class FailingConnection:
            """真实连接代理：只在 claim_span 插入时注入故障。"""
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if "INTO claim_span" in sql:
                    raise sqlite3.OperationalError("injected disk failure")
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch.object(sm, "_db", lambda: FailingConnection(real_db())):
            with self.assertRaises(sqlite3.OperationalError):
                cw.commit(self.claim(), self.window, now=NOW)
        self.assertEqual(0, self.counts()["belief"], "回滚后残留了半条信念")
        self.assertEqual(0, self.counts()["belief_evidence"], "回滚后残留了半套证据")
        self.assertEqual(0, self.counts()["claim_span"])
        # 故障之后连接已释放：同一候选重新提交必须能完整落账。
        got = cw.commit(self.claim(), self.window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertEqual(1, self.counts()["belief"])

    def test_helper_refuses_empty_or_unknown_plans(self):
        with self.assertRaises(ValueError):
            sm.write_cognition_transaction({})
        with self.assertRaises(ValueError):
            sm.write_cognition_transaction({"ts": NOW, "vibe": {}})
        self.assertEqual({t: 0 for t in self.counts()}, self.counts())

    def test_helper_refuses_a_belief_without_spans(self):
        with self.assertRaises(ValueError):
            sm.write_cognition_transaction({"ts": NOW, "belief": {
                "object_id": "obj_x", "proposition": "无依据的命题", "spans": []}})
        self.assertEqual(0, self.counts()["belief"])


class IdempotencyTests(WriterFixture):
    def test_same_candidate_twice_keeps_one_belief_and_one_span_set(self):
        first = cw.commit(self.claim(), self.window, now=NOW)
        again = cw.commit(self.claim(), self.window, now=NOW + 999)
        self.assertEqual(first["object_ids"], again["object_ids"],
                         "同一候选重复提交变成了两条信念")
        self.assertEqual(1, self.counts()["belief"])
        self.assertEqual(1, self.counts()["belief_evidence"])
        self.assertEqual(1, self.counts()["claim_span"])

    def test_same_episode_twice_keeps_one_episode(self):
        candidate = self.claim(object_kind="episode", epistemic_type="experienced",
                               content="她看到朋友晒出了抽卡结果")
        first = cw.commit(candidate, self.window, now=NOW)
        again = cw.commit(candidate, self.window, now=NOW + 999)
        self.assertEqual(first["object_ids"], again["object_ids"])
        self.assertEqual(1, self.counts()["episode"])


class ProjectionTests(WriterFixture):
    def test_projection_carries_the_full_contract(self):
        got = cw.commit(self.claim(candidate_confidence=0.4), self.window, now=NOW)
        self.assertEqual({"decision", "kind", "reasons", "current", "object_ids",
                          "spans", "subject_ref", "subject_basis",
                          "candidate_confidence", "provenance_decision"}, set(got))
        self.assertEqual("claim", got["kind"])
        self.assertTrue(got["current"])
        self.assertIsInstance(got["reasons"], list)
        self.assertEqual(self.actor_ref, got["subject_ref"])
        self.assertEqual("source_actor", got["subject_basis"])
        self.assertEqual(0.4, got["candidate_confidence"])
        self.assertEqual(1, len(got["spans"]))
        self.assertEqual(self.eid, got["spans"][0]["event_id"])

    def test_projection_is_a_copy_not_a_ledger_handle(self):
        got = cw.commit(self.claim(), self.window, now=NOW)
        bid = got["object_ids"]["belief"]
        got["spans"][0]["quote"] = "被篡改的引文"
        got["reasons"].append("injected")
        self.assertEqual("我最喜欢喝冰可乐了", sm.belief_spans(bid)[0]["quote"],
                         "投影必须是只读副本，改它不许碰到账本")


if __name__ == "__main__":
    unittest.main()