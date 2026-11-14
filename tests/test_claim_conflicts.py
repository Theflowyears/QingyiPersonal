# -*- coding: utf-8 -*-
"""M04 全库候选矛盾裁决：两段式检索 + 稳定 verdict/reason_code，绝不覆盖旧信念。

行为测试覆盖统一方案 §20.4（候选键、两段式、语义提示不覆盖结构规则）与
§7.3 七种情况表；字面相似陷阱（喜欢/不喜欢必须判矛盾）、多值偏好并存、
同根源转述不算独立反例、unclassified 不乱归。只读桥在隔离临时库上验证
「超过 40 条也能扫到早期条目」「缺库返回空且不建文件」「裁决不修改输入」。
AST 锁钉死纯函数契约：无写入 SQL、无顶层冻路径、只读连接 mode=ro。

所有合成数据只出现在 tempfile 隔离目录里，是测试夹具，不是自然样本。
"""
import ast
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import claim_conflicts as cc


def cand(value, subject="per_aaa111", **over):
    """M02 口径的合成候选夹具（不是自然样本）。"""
    row = {"candidate_id": "cand_" + value, "subject_ref": subject,
           "object_value": value, "negation": "", "qualifiers": [],
           "epistemic_kind": "experienced", "root_event_id": "evt_" + value,
           "evidence": [{"event_id": "evt_" + value, "quote": value}]}
    row.update(over)
    return row


def snapshot(root):
    """目录里有什么（含空目录），证明只读桥没顺手建任何东西。"""
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


def file_hash(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# 与 self_model._SCHEMA 中本模块读到的列保持一致的最小夹具 schema。
_FIXTURE_SCHEMA = """
CREATE TABLE belief (
    id TEXT PRIMARY KEY, proposition TEXT NOT NULL,
    subject_qq TEXT NOT NULL DEFAULT '', confidence REAL,
    status TEXT NOT NULL DEFAULT 'active', source TEXT NOT NULL DEFAULT '',
    first_ts REAL NOT NULL, last_ts REAL NOT NULL,
    is_legacy INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE belief_evidence (
    belief_id TEXT NOT NULL, event_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'support', weight REAL NOT NULL DEFAULT 1.0,
    ts REAL NOT NULL, PRIMARY KEY (belief_id, event_id, relation)
);
CREATE TABLE claim_candidate (
    id TEXT PRIMARY KEY, restatement TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '', subject_ref TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]', source_session TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private', candidate_confidence REAL,
    state TEXT NOT NULL DEFAULT 'pending_review', created REAL NOT NULL,
    dedupe TEXT UNIQUE
);
"""


def seed_db(root, beliefs=(), candidates=()):
    """在隔离临时目录里建夹具库。beliefs: (id, proposition, subject, ts)。"""
    path = os.path.join(root, "cognition.db")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_FIXTURE_SCHEMA)
        for bid, proposition, subject, ts in beliefs:
            connection.execute(
                "INSERT INTO belief(id,proposition,subject_qq,first_ts,last_ts) "
                "VALUES(?,?,?,?,?)", (bid, proposition, subject, ts, ts))
            connection.execute(
                "INSERT INTO belief_evidence(belief_id,event_id,relation,ts) "
                "VALUES(?,?,?,?)", (bid, "evt_" + bid, "support", ts))
        for cid, restatement, subject, ts in candidates:
            connection.execute(
                "INSERT INTO claim_candidate(id,restatement,subject_ref,decision,"
                "evidence,created) VALUES(?,?,?,?,?,?)",
                (cid, restatement, subject, "deferred",
                 json.dumps([{"event_id": "evt_" + cid, "quote": restatement}],
                            ensure_ascii=False), ts))
        connection.commit()
    finally:
        connection.close()
    return path


class FamilyClassificationTests(unittest.TestCase):
    """predicate_family 是有限可解释类型；未知归 unclassified，不许乱归。"""

    def test_each_family_has_an_interpretable_keyword_route(self):
        cases = [("我喜欢喝茶", "self_report_preference"),
                 ("今天正在上课", "current_activity"),
                 ("住在北京", "residence"),
                 ("我们是朋友", "relationship_statement"),
                 ("承诺明天交稿", "commitment")]
        for text, family in cases:
            with self.subTest(text=text):
                self.assertEqual(family, cc.classify_predicate(text))

    def test_an_explicit_kind_is_honored(self):
        self.assertEqual("commitment", cc.classify_predicate("随便什么文本", "commitment"))

    def test_unknown_text_stays_unclassified_and_is_never_guessed(self):
        for text in ("宇宙在膨胀", "某个没见过的关系", ""):
            with self.subTest(text=text):
                self.assertEqual("unclassified", cc.classify_predicate(text))

    def test_the_family_vocabulary_is_fixed(self):
        self.assertEqual(("self_report_preference", "current_activity", "residence",
                          "relationship_statement", "commitment"), cc.PREDICATE_FAMILIES)
        self.assertEqual("unclassified", cc.UNCLASSIFIED)

    def test_a_declared_family_is_used_but_a_bogus_one_is_not(self):
        declared = cand("随便文本", predicate_family="residence")
        self.assertEqual(("per_aaa111", "residence"), cc.candidate_key(declared))
        bogus = cand("住在北京", predicate_family="vibe")
        self.assertEqual("residence", cc.candidate_key(bogus)[1],
                         "不在有限集合里的家族声明不许采信")

    def test_unclassified_candidates_are_deferred_not_force_fitted(self):
        left = cand("星云在旋转", candidate_id="u1")
        right = cand("星云不在旋转", candidate_id="u2")
        got = cc.adjudicate_pair(left, right)
        self.assertEqual("deferred", got["verdict"])
        self.assertEqual("family_unclassified", got["reason_code"])
        self.assertNotEqual("contradiction", got["verdict"],
                            "未知家族不许硬判矛盾（§20.4）")


class SevenCasesTests(unittest.TestCase):
    """§7.3 矛盾不是一律覆盖：七种情况各走各的门。"""

    def test_case1_different_subjects_are_not_compared(self):
        got = cc.adjudicate_pair(cand("喜欢茶", subject="per_aaa111"),
                                 cand("不喜欢茶", subject="per_bbb222"))
        self.assertEqual("not_comparable", got["verdict"])
        self.assertEqual("different_subject", got["reason_code"])

    def test_case2_disjoint_times_keep_history_instead_of_overwriting(self):
        got = cc.adjudicate_pair(
            cand("住在北京", valid_from=100.0, valid_to=200.0, candidate_id="old"),
            cand("住在上海", valid_from=300.0, valid_to=400.0, candidate_id="new"))
        self.assertEqual("historical_change", got["verdict"])
        self.assertEqual("disjoint_validity", got["reason_code"])

    def test_case3_opposite_claims_build_a_contradiction_candidate(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("不喜欢茶", candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"])
        self.assertEqual("explicit_negation_opposite", got["reason_code"])

    def test_case4_reported_vs_self_report_keeps_the_epistemic_difference(self):
        got = cc.adjudicate_pair(
            cand("喜欢茶", candidate_id="a", epistemic_kind="experienced",
                 root_event_id="evt_1"),
            cand("喜欢茶", candidate_id="b", epistemic_kind="reported",
                 root_event_id="evt_2"))
        self.assertEqual("equivalent", got["verdict"])
        self.assertIn("epistemic_kind_differs", got["reasons"])
        self.assertEqual("experienced", got["left_epistemic_kind"])
        self.assertEqual("reported", got["right_epistemic_kind"],
                         "认识类型差异必须留痕，不许按更近覆盖（§7.3）")

    def test_case5_multi_value_preferences_coexist(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢咖啡", candidate_id="b"))
        self.assertEqual("coexists", got["verdict"])
        self.assertEqual("multi_value_coexists", got["reason_code"])

    def test_case6_new_self_knowledge_keeps_the_old_version(self):
        old = cand("喜欢独处", valid_from=0.0, valid_to=100.0, candidate_id="old")
        new = cand("喜欢热闹", valid_from=200.0, valid_to=300.0, candidate_id="new")
        before = json.dumps([old, new], sort_keys=True, ensure_ascii=False)
        got = cc.adjudicate_pair(old, new)
        self.assertEqual("historical_change", got["verdict"])
        self.assertEqual("喜欢独处", got["left_value"])
        self.assertEqual("喜欢热闹", got["right_value"])
        self.assertEqual(before, json.dumps([old, new], sort_keys=True, ensure_ascii=False),
                         "新旧自我认识都保留，禁止整块最新摘要覆盖旧人生（§7.3）")

    def test_case7_unresolvable_pairs_stay_disputed(self):
        got = cc.adjudicate_pair(
            cand("喜欢茶", candidate_id="a"),
            cand("讨厌咖啡", candidate_id="b"))
        self.assertEqual("disputed", got["verdict"])
        self.assertEqual("value_conflict_unresolved", got["reason_code"])


class NegationTrapTests(unittest.TestCase):
    """字面相似陷阱：喜欢/不喜欢字面极像，却必须判矛盾（§7.3 末段）。"""

    def test_like_and_dislike_are_a_contradiction_not_a_near_duplicate(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("不喜欢茶", candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"])
        self.assertNotIn(got["verdict"], ("equivalent", "duplicate", "coexists"))

    def test_an_explicit_negation_field_also_flips_the_polarity(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢茶", candidate_id="b", negation=True,
                                      root_event_id="evt_other"))
        self.assertEqual("contradiction", got["verdict"])
        self.assertEqual("explicit_negation_opposite", got["reason_code"])

    def test_the_m02_negation_marker_string_is_understood(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢茶", candidate_id="b", negation="不",
                                      root_event_id="evt_other"))
        self.assertEqual("contradiction", got["verdict"])

    def test_dislike_verb_and_like_verb_on_the_same_object_contradict(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("讨厌茶", candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"])
        self.assertEqual("explicit_negation_opposite", got["reason_code"])

    def test_double_negation_returns_to_the_positive_polarity(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("没有不喜欢茶", candidate_id="b"))
        self.assertEqual("equivalent", got["verdict"])

    def test_negation_in_other_families_also_contradicts(self):
        got = cc.adjudicate_pair(cand("住在北京", candidate_id="a"),
                                 cand("不住在北京", candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"])


class MultiValueAndCardinalityTests(unittest.TestCase):
    def test_tea_and_coffee_are_not_a_contradiction(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢咖啡", candidate_id="b"))
        self.assertEqual("coexists", got["verdict"])

    def test_two_commitments_coexist(self):
        got = cc.adjudicate_pair(cand("承诺周一交稿", candidate_id="a"),
                                 cand("承诺周五交画", candidate_id="b"))
        self.assertEqual("coexists", got["verdict"])

    def test_single_valued_residence_conflicts_in_the_same_period(self):
        got = cc.adjudicate_pair(
            cand("住在北京", valid_from=100.0, valid_to=300.0, candidate_id="a"),
            cand("住在上海", valid_from=200.0, valid_to=400.0, candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"])
        self.assertEqual("single_valued_value_conflict", got["reason_code"])

    def test_two_negative_residences_can_both_hold(self):
        got = cc.adjudicate_pair(cand("不住在北京", candidate_id="a"),
                                 cand("不住在上海", candidate_id="b"))
        self.assertNotEqual("contradiction", got["verdict"],
                            "同时不住两个地方并不矛盾")
        self.assertEqual("disputed", got["verdict"])

    def test_a_more_specific_value_is_a_supplement_not_a_conflict(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢热茶", candidate_id="b"))
        self.assertEqual("supplement", got["verdict"])
        self.assertEqual("value_extends", got["reason_code"])

    def test_extra_qualifiers_are_a_supplement(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢茶", candidate_id="b", qualifiers=["夏天"],
                                      root_event_id="evt_other"))
        self.assertEqual("supplement", got["verdict"])
        self.assertEqual("qualifier_extends", got["reason_code"])


class SameRootTests(unittest.TestCase):
    """同一根源＝转述重复，不算独立反例（§20.4）。"""

    def test_a_restatement_of_the_same_event_is_a_duplicate(self):
        got = cc.adjudicate_pair(
            cand("喜欢茶", candidate_id="a", root_event_id="evt_1"),
            cand("爱茶", candidate_id="b", root_event_id="evt_1",
                 predicate_family="self_report_preference"))
        self.assertEqual("duplicate", got["verdict"])
        self.assertEqual("same_root_restatement", got["reason_code"])

    def test_same_root_divergence_is_never_an_independent_contradiction(self):
        got = cc.adjudicate_pair(
            cand("喜欢茶", candidate_id="a", root_event_id="evt_1"),
            cand("不喜欢茶", candidate_id="b", root_event_id="evt_1"))
        self.assertEqual("disputed", got["verdict"])
        self.assertEqual("same_root_not_independent", got["reason_code"])
        self.assertNotEqual("contradiction", got["verdict"],
                            "一个事件的两条提取不许互相当反例")

    def test_independent_roots_with_the_same_proposition_are_equivalent(self):
        got = cc.adjudicate_pair(
            cand("喜欢茶", candidate_id="a", root_event_id="evt_1"),
            cand("喜欢茶", candidate_id="b", root_event_id="evt_2"))
        self.assertEqual("equivalent", got["verdict"])
        self.assertEqual("equivalent_independent", got["reason_code"])

    def test_the_root_can_come_from_the_first_evidence_event(self):
        left = cand("喜欢茶", candidate_id="a", root_event_id="")
        right = cand("爱茶", candidate_id="b", root_event_id="",
                     predicate_family="self_report_preference",
                     evidence=[{"event_id": "evt_9", "quote": "爱茶"}])
        left["evidence"] = [{"event_id": "evt_9", "quote": "喜欢茶"}]
        got = cc.adjudicate_pair(left, right)
        self.assertEqual("duplicate", got["verdict"])


class TimeTests(unittest.TestCase):
    def test_distinct_time_expressions_are_history_for_changeable_families(self):
        got = cc.adjudicate_pair(
            cand("正在上课", time_expression="昨天", candidate_id="a"),
            cand("正在上班", time_expression="今天", candidate_id="b"))
        self.assertEqual("historical_change", got["verdict"])
        self.assertEqual("distinct_time_expression", got["reason_code"])

    def test_open_intervals_are_not_proven_disjoint(self):
        got = cc.adjudicate_pair(
            cand("住在北京", valid_from=100.0, candidate_id="a"),
            cand("住在上海", valid_from=300.0, candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"],
                         "开口有效期证明不了不相交，不许洗成历史变化")

    def test_commitments_with_different_due_windows_coexist(self):
        got = cc.adjudicate_pair(
            cand("承诺交稿", valid_from=0.0, valid_to=100.0, candidate_id="a"),
            cand("承诺交画", valid_from=200.0, valid_to=300.0, candidate_id="b"))
        self.assertEqual("coexists", got["verdict"])

    def test_record_timestamps_are_not_validity(self):
        # recorded_at/first_ts 是记录时间：只有它不同时不许判历史变化。
        got = cc.adjudicate_pair(
            cand("住在北京", recorded_at=100.0, candidate_id="a"),
            cand("住在上海", recorded_at=900.0, candidate_id="b"))
        self.assertEqual("contradiction", got["verdict"])


class GateTests(unittest.TestCase):
    def test_a_missing_subject_is_deferred_not_waved_through(self):
        got = cc.adjudicate_pair(cand("喜欢茶", subject=""),
                                 cand("不喜欢茶"))
        self.assertEqual("deferred", got["verdict"])
        self.assertEqual("subject_missing", got["reason_code"])

    def test_different_families_are_not_compared(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("住在北京", candidate_id="b"))
        self.assertEqual("not_comparable", got["verdict"])
        self.assertEqual("different_family", got["reason_code"])

    def test_a_missing_value_is_deferred(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("", candidate_id="b"))
        self.assertEqual("deferred", got["verdict"])
        self.assertEqual("missing_value", got["reason_code"])

    def test_every_verdict_and_reason_is_stable_and_nonempty(self):
        rows = [cand("喜欢茶", candidate_id="a"), cand("不喜欢茶", candidate_id="b"),
                cand("喜欢咖啡", candidate_id="c"), cand("住在北京", candidate_id="d"),
                cand("宇宙膨胀", candidate_id="e"), cand("喜欢茶", subject="", candidate_id="f")]
        for left in rows:
            for right in rows:
                got = cc.adjudicate_pair(left, right)
                self.assertIn(got["verdict"], cc.VERDICTS)
                self.assertTrue(got["reason_code"])
                self.assertIn(got["reason_code"], got["reasons"])


class SemanticHintTests(unittest.TestCase):
    """语义 LLM 只能提出 same/extends/contradicts/unknown 及来源，不覆盖。"""

    def test_a_hint_cannot_override_a_structural_contradiction(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("不喜欢茶", candidate_id="b"),
                                 semantic_hint={"relation": "same", "source": "llm:x"})
        self.assertEqual("contradiction", got["verdict"])
        self.assertEqual({"relation": "same", "source": "llm:x"}, got["semantic_hint"])
        self.assertIn("semantic_hint_recorded", got["reasons"])

    def test_a_hint_cannot_turn_multi_value_into_a_contradiction(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("喜欢咖啡", candidate_id="b"),
                                 semantic_hint={"relation": "contradicts", "source": "llm:x"})
        self.assertEqual("coexists", got["verdict"])

    def test_a_hint_cannot_rescue_a_same_root_pair_into_independence(self):
        got = cc.adjudicate_pair(
            cand("喜欢茶", candidate_id="a", root_event_id="evt_1"),
            cand("不喜欢茶", candidate_id="b", root_event_id="evt_1"),
            semantic_hint={"relation": "contradicts", "source": "llm:x"})
        self.assertEqual("disputed", got["verdict"])
        self.assertEqual("same_root_not_independent", got["reason_code"])

    def test_an_unsourced_hint_is_ignored_and_marked(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("讨厌咖啡", candidate_id="b"),
                                 semantic_hint={"relation": "contradicts"})
        self.assertEqual("disputed", got["verdict"])
        self.assertIsNone(got["semantic_hint"])
        self.assertIn("semantic_hint_unsourced", got["reasons"])

    def test_an_unknown_relation_is_ignored_and_marked(self):
        got = cc.adjudicate_pair(cand("喜欢茶", candidate_id="a"),
                                 cand("讨厌咖啡", candidate_id="b"),
                                 semantic_hint={"relation": "vibes", "source": "llm:x"})
        self.assertIsNone(got["semantic_hint"])
        self.assertIn("semantic_hint_unknown_relation", got["reasons"])

    def test_the_relation_vocabulary_is_fixed(self):
        self.assertEqual(("same", "extends", "contradicts", "unknown"),
                         cc.SEMANTIC_RELATIONS)

    def test_bulk_hints_are_matched_by_sorted_id_pair(self):
        rows = [cand("喜欢茶", candidate_id="a"), cand("讨厌咖啡", candidate_id="b")]
        got = cc.adjudicate(rows, semantic_hints={("b", "a"): {"relation": "unknown",
                                                              "source": "llm:x"}})
        self.assertEqual(1, len(got))
        self.assertEqual({"relation": "unknown", "source": "llm:x"}, got[0]["semantic_hint"])


class StageOneTests(unittest.TestCase):
    def test_only_same_key_candidates_are_paired(self):
        rows = [cand("喜欢茶", subject="per_aaa111", candidate_id="a"),
                cand("喜欢咖啡", subject="per_aaa111", candidate_id="b"),
                cand("喜欢茶", subject="per_bbb222", candidate_id="c"),
                cand("住在北京", subject="per_aaa111", candidate_id="d")]
        pairs = cc.collect_conflict_pairs(rows)
        ids = {tuple(sorted((l["candidate_id"], r["candidate_id"])))
               for l, r in pairs}
        self.assertEqual({("a", "b")}, ids,
                         "不同主体/不同家族不许配对，同键内全量配对")

    def test_a_key_group_is_compared_in_full_not_just_the_tail(self):
        rows = [cand("喜欢茶%d" % i, candidate_id="c%02d" % i) for i in range(5)]
        self.assertEqual(10, len(cc.collect_conflict_pairs(rows)),
                         "5 条同键候选必须产生 C(5,2)=10 对，不许截断")

    def test_bulk_adjudication_is_deterministic(self):
        rows = [cand("喜欢茶", candidate_id="a"), cand("不喜欢茶", candidate_id="b"),
                cand("喜欢咖啡", candidate_id="c")]
        first = cc.adjudicate(rows)
        second = cc.adjudicate(list(reversed(rows)))
        self.assertEqual(first, second)
        self.assertEqual(3, len(first))


class PurityTests(unittest.TestCase):
    def test_adjudication_never_mutates_the_input_candidates(self):
        rows = [cand("喜欢茶", candidate_id="a"),
                cand("不喜欢茶", candidate_id="b", qualifiers=["最近"]),
                cand("喜欢咖啡", candidate_id="c", valid_from=1.0, valid_to=2.0),
                cand("宇宙膨胀", candidate_id="d")]
        before = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        cc.adjudicate(rows)
        cc.collect_conflict_pairs(rows)
        for left in rows:
            for right in rows:
                cc.adjudicate_pair(left, right)
        self.assertEqual(before, json.dumps(rows, sort_keys=True, ensure_ascii=False))

    def test_records_are_plain_data_not_live_references(self):
        rows = [cand("喜欢茶", candidate_id="a"), cand("不喜欢茶", candidate_id="b")]
        records = cc.adjudicate(rows)
        records[0]["verdict"] = "tampered"
        self.assertEqual("contradiction", cc.adjudicate(rows)[0]["verdict"])


class ReadBridgeTests(unittest.TestCase):
    """只读桥：全库收集、缺库返回空、绝不建文件、路径调用时解析。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m04_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_an_absent_ledger_returns_empty_and_creates_nothing(self):
        before = snapshot(self.root)
        self.assertEqual([], cc.read_all_candidates())
        self.assertEqual(before, snapshot(self.root),
                         "只读桥顺手建了库/目录（X15）")

    def test_a_tableless_db_is_left_alone(self):
        path = os.path.join(self.root, "cognition.db")
        connection = sqlite3.connect(path)
        connection.close()
        before = file_hash(path)
        self.assertEqual([], cc.read_all_candidates())
        self.assertEqual(before, file_hash(path), "不许给别人的库补表")

    def test_more_than_forty_rows_still_reaches_the_earliest_entries(self):
        # 早期矛盾对故意取排序靠后的 id + 最旧的 ts：任何「末尾 20/40 条」
        # 的扫描都会漏掉它们。其余 58 条是不同主体的噪声夹具。
        beliefs = [("zz_old_0", "喜欢茶", "1001", 10.0),
                   ("zz_old_1", "不喜欢茶", "1001", 20.0)]
        beliefs += [("b_%03d" % i, "未知事情第%d件" % i, str(2000 + i), 100.0 + i)
                    for i in range(58)]
        candidates = [("clc_0", "喜欢咖啡", "1001", 30.0),
                      ("clc_1", "承诺明天交稿", "1001", 40.0)]
        path = seed_db(self.root, beliefs, candidates)
        before = file_hash(path)
        rows = cc.read_all_candidates()
        self.assertEqual(62, len(rows), "全库收集不许 LIMIT 截断")
        ids = {row["candidate_id"] for row in rows}
        self.assertIn("zz_old_0", ids)
        self.assertIn("zz_old_1", ids)
        self.assertIn("clc_0", ids)
        self.assertEqual(before, file_hash(path), "只读收集改写了库文件")
        self.assertEqual(sorted(os.listdir(self.root)), ["cognition.db"],
                         "只读收集建了多余文件")

    def test_the_early_contradiction_is_actually_adjudicated(self):
        beliefs = [("zz_old_0", "喜欢茶", "1001", 10.0),
                   ("zz_old_1", "不喜欢茶", "1001", 20.0)]
        beliefs += [("b_%03d" % i, "未知事情第%d件" % i, str(2000 + i), 100.0 + i)
                    for i in range(58)]
        seed_db(self.root, beliefs)
        records = cc.adjudicate(cc.read_all_candidates())
        hits = [r for r in records if r["verdict"] == "contradiction"]
        self.assertEqual(1, len(hits))
        self.assertEqual({"zz_old_0", "zz_old_1"},
                         {hits[0]["left_id"], hits[0]["right_id"]})
        self.assertEqual("explicit_negation_opposite", hits[0]["reason_code"])

    def test_collected_rows_keep_the_m02_field_shape_without_fabrication(self):
        seed_db(self.root, [("b1", "喜欢茶", "1001", 10.0)])
        row = cc.read_all_candidates()[0]
        for key in ("subject_ref", "predicate_family", "value", "object_value",
                    "negation", "qualifiers", "valid_from", "valid_to",
                    "time_expression", "epistemic_kind", "root_event_id", "evidence"):
            self.assertIn(key, row)
        self.assertEqual("1001", row["subject_ref"])
        self.assertEqual("self_report_preference", row["predicate_family"])
        self.assertEqual("evt_b1", row["root_event_id"])
        self.assertIsNone(row["valid_from"], "first_ts 是记录时间，不许冒充有效期")
        self.assertIsNone(row["valid_to"])
        self.assertEqual("", row["epistemic_kind"], "真实表没有认识类型列，不许补造")

    def test_the_data_dir_is_resolved_at_call_time_not_import_time(self):
        seed_db(self.root, [("b1", "喜欢茶", "1001", 10.0)])
        self.assertEqual(1, len(cc.read_all_candidates()))
        with tempfile.TemporaryDirectory(prefix="m04_other_",
                                         ignore_cleanup_errors=True) as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                self.assertEqual([], cc.read_all_candidates(),
                                 "路径若在 import 时冻住，这里会读到别人的库")
        self.assertEqual(1, len(cc.read_all_candidates()))

    def test_invalidated_beliefs_are_collected_with_their_status(self):
        path = seed_db(self.root, [("b1", "喜欢茶", "1001", 10.0)])
        connection = sqlite3.connect(path)
        connection.execute("UPDATE belief SET status='invalidated' WHERE id='b1'")
        connection.commit()
        connection.close()
        rows = cc.read_all_candidates()
        self.assertEqual(1, len(rows))
        self.assertEqual("invalidated", rows[0]["status"],
                         "全库收集带上状态，失效与否由消费方决定")


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "claim_conflicts.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_only_pure_or_readonly_modules_are_imported(self):
        allowed = {"json", "os", "sqlite3", "pathlib"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             "矛盾契约 import 了写入/网络/模型 API：%s" % sorted(names - allowed))

    def test_no_write_sql_anywhere_in_the_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper,
                                     "矛盾契约里出现写入 SQL：%s" % node.value[:60])

    def test_no_write_or_file_calls(self):
        forbidden = {"commit", "executescript", "executemany", "rollback",
                     "mkdir", "makedirs", "remove", "unlink", "rmtree", "rename",
                     "add_belief", "revise_belief", "quarantine_claim", "person_id"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "矛盾契约里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual("open", node.func.id, "契约不直接开文件")

    def test_the_connection_is_readonly(self):
        self.assertIn("mode=ro", self.source)
        self.assertIn("query_only", self.source)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径，换 DATA_DIR 的测试会读到别人的库")

    def test_path_resolution_lives_only_inside_the_read_bridge(self):
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in ("_cognition_path", "read_all_candidates"):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "isfile"),
                                     "%s 里解析了路径；核心裁决必须是纯函数" % node.name)

    def test_no_similarity_scoring_is_used_for_contradiction(self):
        for banned in ("jaccard", "cosine", "difflib", "SequenceMatcher",
                       "embedding", "vector"):
            self.assertNotIn(banned, self.source.lower(),
                             "不许用字面/向量相似度判矛盾（§7.3）")

    def test_the_verdict_set_is_fixed(self):
        self.assertEqual(("not_comparable", "equivalent", "supplement",
                          "historical_change", "coexists", "contradiction",
                          "duplicate", "disputed", "deferred"), cc.VERDICTS)


if __name__ == "__main__":
    unittest.main()