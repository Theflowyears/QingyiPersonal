"""M02 逐命题原句来源：一条命题只挂它自己那句原话。

行为测试覆盖统一方案 §20.3 的固定验证顺序与 §6.1 来源字段组；
AST 锁钉死两件事：契约保持纯函数，以及整理入口不许退回「整窗挂证据」（X04）。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import claim_provenance as cp
import self_model as sm

NOW = 2000.0


def source(event_id="evt_1", text="我最喜欢喝冰可乐了", **over):
    row = {"event_id": event_id, "text": text, "kind": "private_message",
           "source_revision": "rev_" + event_id, "occurred_at": 1000.0,
           "session": "private:1001", "visibility": "private",
           "actor_qq": "1001", "topic_qq": "", "actor_ref": "per_aaa",
           "topic_ref": "", "synthetic": False, "retracted": False}
    row.update(over)
    return row


def claim(restatement="喜欢冰可乐", evidence=None, **over):
    row = {"restatement": restatement,
           "evidence": [{"event_id": "evt_1", "quote": "我最喜欢喝冰可乐了"}]
           if evidence is None else evidence}
    row.update(over)
    return row


class QuoteVerificationTests(unittest.TestCase):
    """quote 必须逐字可定位；offset 由代码核对，不信模型数字。"""

    def test_a_verbatim_fragment_gets_real_offsets(self):
        got = cp.verify_quote("我最喜欢喝冰可乐了", "冰可乐")
        self.assertTrue(got["ok"])
        self.assertEqual(5, got["span_start"])
        self.assertEqual(8, got["span_end"])
        self.assertEqual("冰可乐", "我最喜欢喝冰可乐了"[got["span_start"]:got["span_end"]])

    def test_correct_offsets_are_accepted_as_given(self):
        got = cp.verify_quote("我最喜欢喝冰可乐了", "冰可乐", 5, 8)
        self.assertTrue(got["ok"])
        self.assertEqual("", got["reason"])

    def test_a_wrong_offset_is_corrected_only_at_the_unique_occurrence(self):
        got = cp.verify_quote("我最喜欢喝冰可乐了", "冰可乐", 0, 3)
        self.assertTrue(got["ok"])
        self.assertEqual("offset_corrected", got["reason"])
        self.assertEqual((5, 8), (got["span_start"], got["span_end"]))

    def test_an_ambiguous_offset_is_deferred_not_guessed(self):
        # 「可乐」出现两次，模型给的 offset 又对不上：不许猜是哪一处。
        got = cp.verify_quote("可乐可乐", "可乐", 1, 3)
        self.assertFalse(got["ok"])
        self.assertEqual("ambiguous_offset", got["reason"])

    def test_a_quote_without_offsets_needs_a_unique_occurrence(self):
        self.assertEqual("ambiguous_quote", cp.verify_quote("可乐可乐", "可乐")["reason"])
        self.assertTrue(cp.verify_quote("冰可乐", "可乐")["ok"])

    def test_a_paraphrase_is_not_a_verbatim_quote(self):
        got = cp.verify_quote("我最喜欢喝冰可乐了", "他爱喝冰可乐")
        self.assertFalse(got["ok"])
        self.assertEqual("quote_not_verbatim", got["reason"])

    def test_empty_and_out_of_range_offsets_are_refused(self):
        self.assertEqual("empty_quote", cp.verify_quote("原文", "  ")["reason"])
        self.assertEqual("invalid_offset", cp.verify_quote("原文", "原文", 0, 99)["reason"])
        self.assertEqual("invalid_offset", cp.verify_quote("原文", "原文", "0", "2")["reason"])


class SourceGateTests(unittest.TestCase):
    def test_unusable_sources_are_named_not_silently_dropped(self):
        cases = [(source(retracted=True), "source_retracted"),
                 (source(synthetic=True), "synthetic_source"),
                 (source(session="verify:stage0"), "test_polluted_source"),
                 (source(occurred_at=NOW + 60), "future_source"),
                 (source(text=""), "source_text_missing"),
                 (source(occurred_at=0), "invalid_source_time")]
        for row, code in cases:
            with self.subTest(code=code):
                self.assertIn(code, cp.source_reasons(row, NOW))

    def test_a_clean_live_source_has_no_problems(self):
        self.assertEqual([], cp.source_reasons(source(), NOW))


class BindOrderTests(unittest.TestCase):
    """§20.3 的固定验证顺序，每条都有稳定 reason_code。"""

    def bind(self, candidate, window=None):
        return cp.bind_claim(candidate, [source()] if window is None else window, NOW)

    def test_a_locatable_claim_is_accepted_with_a_real_span(self):
        got = self.bind(claim())
        self.assertEqual("accepted", got["decision"])
        self.assertEqual(1, len(got["spans"]))
        self.assertEqual("evt_1", got["spans"][0]["event_id"])
        self.assertEqual("rev_evt_1", got["spans"][0]["source_revision"])

    def test_no_evidence_span_goes_to_quarantine_not_to_verified_fact(self):
        got = self.bind(claim(evidence=[]))
        self.assertEqual("deferred", got["decision"])
        self.assertIn("no_evidence_span", got["reasons"])
        self.assertEqual([], got["spans"])

    def test_an_event_outside_the_frozen_window_is_rejected(self):
        got = self.bind(claim(evidence=[{"event_id": "evt_999", "quote": "冰可乐"}]))
        self.assertEqual("rejected", got["decision"])
        self.assertIn("event_outside_window", got["reasons"])

    def test_a_changed_source_revision_cannot_reuse_the_old_quote(self):
        got = self.bind(claim(evidence=[{"event_id": "evt_1", "quote": "冰可乐",
                                         "source_revision": "stale"}]))
        self.assertEqual("deferred", got["decision"])
        self.assertIn("source_revision_mismatch", got["reasons"])

    def test_a_retracted_source_is_rejected_even_with_a_perfect_quote(self):
        got = self.bind(claim(), [source(retracted=True)])
        self.assertEqual("rejected", got["decision"])
        self.assertIn("source_retracted", got["reasons"])

    def test_subject_must_come_from_a_real_participant(self):
        got = self.bind(claim(subject_ref="per_aaa"))
        self.assertEqual("accepted", got["decision"])
        self.assertEqual("source_actor", got["subject_basis"])

    def test_an_unresolvable_declared_subject_is_deferred(self):
        got = self.bind(claim(subject_ref="per_zzz"))
        self.assertEqual("deferred", got["decision"])
        self.assertIn("subject_unresolved", got["reasons"])

    def test_the_word_master_never_fills_in_a_person_by_default(self):
        # 「主人喜欢冰可乐」里的「主人」不是身份证据：窗口里没有可落的人就暂缓。
        got = self.bind(claim("主人喜欢冰可乐"),
                        [source(actor_qq="", actor_ref="", topic_qq="", topic_ref="")])
        self.assertEqual("deferred", got["decision"])
        self.assertIn("subject_unresolved", got["reasons"])

    def test_a_world_fact_without_a_person_subject_stays_unscoped(self):
        got = self.bind(claim("猫打呼噜是因为喉咙在震动",
                              evidence=[{"event_id": "evt_1", "quote": "猫为什么打呼噜"}]),
                        [source(text="猫为什么打呼噜")])
        self.assertEqual("accepted", got["decision"])
        self.assertEqual("unscoped", got["subject_basis"])

    def test_a_dropped_negation_flips_the_meaning_and_is_rejected(self):
        got = self.bind(claim("喜欢牛肉面",
                              evidence=[{"event_id": "evt_1", "quote": "我不喜欢牛肉面"}]),
                        [source(text="我不喜欢牛肉面")])
        self.assertEqual("rejected", got["decision"])
        self.assertIn("negation_dropped", got["reasons"])

    def test_a_dropped_qualifier_is_deferred_unless_declared(self):
        window = [source(text="我今天可能去教室")]
        evidence = [{"event_id": "evt_1", "quote": "我今天可能去教室"}]
        loose = self.bind(claim("去教室", evidence=evidence), window)
        self.assertEqual("deferred", loose["decision"])
        self.assertIn("qualifier_dropped", loose["reasons"])
        declared = self.bind(claim("去教室", evidence=evidence,
                                   qualifiers=["可能"], time_expression="今天"), window)
        self.assertEqual("accepted", declared["decision"])

    def test_tool_completion_needs_a_matching_execution_and_result(self):
        got = self.bind(claim("已经把视频发出去了", tool_claim="sent"))
        self.assertEqual("rejected", got["decision"])
        self.assertIn("tool_completion_needs_receipt", got["reasons"])
        ok = self.bind(claim("已经把视频发出去了", tool_claim="sent",
                             execution_id="run-1", result_status="accepted"))
        self.assertEqual("accepted", ok["decision"])

    def test_model_confidence_is_recorded_but_never_decides_admission(self):
        high = self.bind(claim(evidence=[], candidate_confidence=0.99))
        self.assertEqual("deferred", high["decision"])
        self.assertEqual(0.99, high["candidate_confidence"])
        low = self.bind(claim(candidate_confidence=0.01))
        self.assertEqual("accepted", low["decision"])
        self.assertEqual(0.01, low["candidate_confidence"])

    def test_lexical_matches_are_labelled_and_not_passed_off_as_semantic_proof(self):
        got = self.bind(claim(evidence=[{"event_id": "evt_1", "quote": "冰可乐",
                                         "match_kind": "lexical"}]))
        self.assertEqual("accepted", got["decision"])
        self.assertEqual("lexical", got["spans"][0]["match_kind"])
        bad = self.bind(claim(evidence=[{"event_id": "evt_1", "quote": "冰可乐",
                                         "match_kind": "semantic"}]))
        self.assertEqual("rejected", bad["decision"])
        self.assertIn("unknown_match_kind", bad["reasons"])


class LedgerIsolated(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m02_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def event(self, text, actor="22001", session="group:771177", dedupe=None):
        # 必须给 dedupe：A05 的身份哈希是 kind+时间+会话+说话人，
        # 同刻同会话的两条不同消息不给键会被当成同一件事幂等丢掉。
        return sm.record_event("group_message", session=session, actor_qq=actor,
                               visibility="group", payload={"text": text}, ts=1000.0,
                               dedupe=dedupe or ("m02:" + text))


class SpanStorageTests(LedgerIsolated):
    def test_spans_replace_the_whole_window_evidence_list(self):
        first = self.event("猫为什么打呼噜")
        second = self.event("今天天气不错")
        third = self.event("晚上吃什么")
        window = sm.source_window([first, second, third])
        bound = cp.bind_claim({"restatement": "猫打呼噜是因为喉咙在震动",
                               "evidence": [{"event_id": first, "quote": "猫为什么打呼噜",
                                             "match_kind": "lexical"}]}, window, NOW)
        self.assertEqual("accepted", bound["decision"])
        # 故意同时递整窗 evidence：有 span 时它必须被忽略，否则 X04 又回来了。
        belief = sm.add_belief("猫打呼噜是因为喉咙在震动", source="consolidation",
                               evidence=[first, second, third], spans=bound["spans"],
                               ts=1100.0)
        self.assertTrue(belief)
        with closing(sm._db()) as connection:
            refs = [r["event_id"] for r in connection.execute(
                "SELECT event_id FROM belief_evidence WHERE belief_id=?", (belief,))]
        self.assertEqual([first], refs, "整窗事件又被摊给这条结论了")
        spans = sm.belief_spans(belief)
        self.assertEqual(1, len(spans))
        self.assertEqual("猫为什么打呼噜", spans[0]["quote"])
        original = sm.get_event(first)["payload"]
        text = json.loads(original)["text"]
        self.assertEqual(spans[0]["quote"],
                         text[spans[0]["span_start"]:spans[0]["span_end"]],
                         "存下来的 quote 必须能在真实源文本里逐字复核")

    def test_source_window_is_read_only_and_does_not_create_people(self):
        event = self.event("猫为什么打呼噜")
        with closing(sm._db()) as connection:
            before = connection.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        window = sm.source_window([event, event, "missing-event"])
        with closing(sm._db()) as connection:
            after = connection.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        self.assertEqual(before, after, "只读窗口不许顺手建人物（X15）")
        self.assertEqual(1, len(window), "重复与不存在的 ID 不该进窗口")
        self.assertEqual("per_" + sm._sha("22001"), window[0]["actor_ref"])
        self.assertEqual("22001", window[0]["actor_qq"], "per_* 要能只读反查出 raw QQ")
        self.assertFalse(window[0]["retracted"])
        self.assertTrue(window[0]["source_revision"])

    def test_a_retracted_source_is_marked_in_the_window(self):
        event = self.event("猫为什么打呼噜")
        sm.retract_evidence(event, "misread")
        self.assertTrue(sm.source_window([event])[0]["retracted"])

    def test_quarantined_claims_are_kept_once_and_never_become_beliefs(self):
        first = sm.quarantine_claim("凭空断言的一条", kind="facts",
                                    reasons=["no_evidence_span"], ts=1100.0)
        again = sm.quarantine_claim("凭空断言的一条", kind="facts",
                                    reasons=["no_evidence_span"], ts=1200.0)
        self.assertTrue(first)
        self.assertEqual(first, again, "同一命题反复整理不该堆垃圾")
        pending = sm.pending_claims()
        self.assertEqual(1, len(pending))
        self.assertEqual("pending_review", pending[0]["state"])
        self.assertEqual(["no_evidence_span"], json.loads(pending[0]["reasons"]))
        with closing(sm._db()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM belief WHERE is_legacy=0").fetchone()[0])


class ConsolidationBindingTests(LedgerIsolated):
    """整理入口的端到端锁：每条结论只挂自己那句，无依据的进隔离候选。"""

    def absorb(self, items, texts, key="facts"):
        import persona_growth as pg
        events = [self.event(text) for text in texts]
        window = sm.source_window(events)
        profile = pg.load()
        added = pg._absorb_list(profile, key, items, window=window)
        return events, added

    def beliefs(self):
        with closing(sm._db()) as connection:
            return [dict(r) for r in connection.execute(
                "SELECT * FROM belief WHERE is_legacy=0").fetchall()]

    def refs(self, belief_id):
        with closing(sm._db()) as connection:
            return [r["event_id"] for r in connection.execute(
                "SELECT event_id FROM belief_evidence WHERE belief_id=?", (belief_id,))]

    def test_each_conclusion_only_carries_its_own_sentence(self):
        events, added = self.absorb(["猫打呼噜是因为喉咙在震动"],
                                    ["猫为什么打呼噜", "今天天气不错", "晚上吃什么"])
        self.assertEqual(3, len(set(events)), "窗口塌成一条事件的话这个锁就是假绿")
        self.assertEqual(["猫打呼噜是因为喉咙在震动"], added)
        beliefs = self.beliefs()
        self.assertEqual(1, len(beliefs))
        self.assertEqual([events[0]], self.refs(beliefs[0]["id"]),
                         "无关事件又被挂上来了")
        self.assertEqual(1, len(sm.belief_spans(beliefs[0]["id"])))

    def test_two_conclusions_in_one_window_do_not_share_evidence(self):
        events, _added = self.absorb(["猫打呼噜是因为喉咙在震动", "猫喜欢晒太阳"],
                                     ["猫为什么打呼噜", "猫喜欢晒太阳吗"])
        beliefs = self.beliefs()
        self.assertEqual(2, len(beliefs))
        by_text = {b["proposition"]: self.refs(b["id"]) for b in beliefs}
        self.assertEqual([events[0]], by_text["猫打呼噜是因为喉咙在震动"])
        self.assertEqual([events[1]], by_text["猫喜欢晒太阳"])

    def test_an_unlocatable_conclusion_is_quarantined_not_groundless(self):
        _events, added = self.absorb(["凭空想出来的一条事实"], ["猫为什么打呼噜"])
        self.assertEqual(["凭空想出来的一条事实"], added, "persona 旧背景仍保留（X05）")
        self.assertEqual([], self.beliefs(), "定位不到原句却写了有依据的信念")
        pending = sm.pending_claims()
        self.assertEqual(1, len(pending))
        self.assertIn("no_evidence_span", json.loads(pending[0]["reasons"]))

    def test_persona_rows_carry_their_own_sources_not_the_window(self):
        import persona_store as ps
        events, _added = self.absorb(["猫打呼噜是因为喉咙在震动", "凭空想出来的一条事实"],
                                     ["猫为什么打呼噜", "今天天气不错"])
        rows = {row["text"]: row for row in ps.sourced_items("facts")}
        self.assertEqual([events[0]], rows["猫打呼噜是因为喉咙在震动"]["source_event_ids"])
        self.assertEqual([], rows["凭空想出来的一条事实"]["source_event_ids"],
                         "无依据的条目不许被补造来源")
        spans = rows["猫打呼噜是因为喉咙在震动"]["evidence_spans"]
        self.assertEqual(1, len(spans))
        self.assertEqual("lexical", spans[0]["match_kind"])

    def test_no_window_means_no_grounded_belief(self):
        import persona_growth as pg
        profile = pg.load()
        added = pg._absorb_list(profile, "facts", ["猫打呼噜是因为喉咙在震动"])
        self.assertEqual(["猫打呼噜是因为喉咙在震动"], added)
        self.assertEqual([], self.beliefs())

    def test_legacy_rows_stay_unsourced_after_migration(self):
        import persona_store as ps
        ps.add("facts", "旧背景的一条")
        row = ps.sourced_items("facts")[0]
        self.assertEqual([], row["source_event_ids"])
        self.assertEqual([], row["evidence_spans"])


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "claim_provenance.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)
        with open(os.path.join(BOT, "persona_growth.py"), encoding="utf-8") as handle:
            cls.growth = ast.parse(handle.read())

    def test_contract_is_a_pure_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertEqual("time", alias.name.split(".")[0],
                                     "来源契约不许碰账本/网络/分词器")
            elif isinstance(node, ast.ImportFrom):
                self.fail("来源契约不许 from-import 外部能力: %s" % node.module)
        self.assertNotIn("cognition.db", self.source)
        self.assertNotIn("sqlite3", self.source)
        self.assertNotIn("tokenize", self.source)

    def test_decision_and_match_kind_sets_are_fixed(self):
        self.assertEqual(("accepted", "deferred", "rejected"), cp.DECISIONS)
        self.assertEqual(("model_quote", "lexical"), cp.MATCH_KINDS)

    def test_module_level_state_is_constant_only(self):
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self.assertTrue(isinstance(target, ast.Name) and target.id.isupper(),
                                    "契约模块不许有可变全局状态")

    def calls(self, name):
        return [node for node in ast.walk(self.growth)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == name]

    def test_consolidation_never_attaches_a_whole_window_again(self):
        """X04 回归锁：add_belief 必须走 spans，不许再递整窗 evidence。"""
        calls = self.calls("add_belief")
        self.assertTrue(calls, "整理入口不再写认知信念了")
        for call in calls:
            keywords = {kw.arg for kw in call.keywords}
            self.assertIn("spans", keywords, "add_belief 必须逐命题 span 落账")
            self.assertNotIn("evidence", keywords, "整窗 evidence 又挂回每条结论了")

    def test_persona_writes_carry_per_item_sources(self):
        for call in self.calls("add_many"):
            self.assertIn("sources", {kw.arg for kw in call.keywords},
                          "persona 批量写入必须带逐条来源")


if __name__ == "__main__":
    unittest.main()