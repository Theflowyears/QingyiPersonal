# -*- coding: utf-8 -*-
"""M08 有限巩固与晋升的回归锁（路线 §3.1 第8步 / §5.3 / §5.4 / §8.2）。

钉死的行为：
  · 巩固分类：亲历/听说/查到 + 可核验原句 → belief；episode → experience；
    推断/想象、无据、已失效、needs_reassessment、矛盾 → temporary，绝不冒充事实；
  · 有限预算：每次事务晋升有显式上限，按确定性优先级取前 N，其余 budget_exhausted；
    预算夹到硬上限，绝不无限；
  · 失效单调：invalidated/superseded 的对象与源永不晋升/复活；
  · 重评估修订：只接住 M05 的 needs_reassessment，剩余有效独立支持可核验 →
    belief_corrected 新版本另立、旧版 superseded、历史保留；否则暂缓/挡下；
  · 确定性：稳定 reason_code、plan_id/reassessment_id 内容寻址且不含墙上时钟、
    输入顺序无关；
  · 只追加/版本化：内存账本旧记录不动，重复 ID/篡改内容拒绝；
  · 单一写入者：落账只走 cognition_writer.commit 与 write_invalidation_transaction；
  · 只读桥：缺库报 absent/degraded，绝不建库建表建目录，DATA_DIR 调用时解析。
AST 锁钉死旁路契约：无写入 SQL、无旧写入器/直连、写入只走 M06/M05 既有入口、
复用冻结前置（M01/M02/M04/M05）、不自带第二套写入器、路径调用时解析。
"""
import ast
import copy
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

import memory_consolidation as mc   # noqa: E402
import cognition_writer as cw        # noqa: E402
import invalidation_dag as dag       # noqa: E402
import self_model as sm              # noqa: E402

NOW = 2000.0
TABLES = ("belief", "belief_evidence", "claim_span", "claim_candidate",
          "episode", "derivation_edge", "correction_record", "object_validity_state")


def source(event_id, text, **over):
    """手造冻结源记录（与 self_model.source_window 同一字段口径）。"""
    row = {"event_id": event_id, "kind": "private_message", "text": text,
           "source_revision": "rev_" + event_id, "occurred_at": 900.0,
           "session": "private:1001", "visibility": "private",
           "actor_ref": "per_1001", "actor_qq": "1001",
           "topic_ref": "", "topic_qq": "", "synthetic": False, "retracted": False}
    row.update(over)
    return row


def claim(oid, content, quote, eid, tier="reported", kind="claim", **over):
    row = {"object_kind": kind, "object_id": oid, "content": content,
           "occurred_at": 1000.0, "recorded_at": 1001.0, "source_event_ids": [eid],
           "epistemic_type": tier, "visibility": "private",
           "source_session": "private:1001", "subject_ref": "per_1001",
           "evidence": [{"event_id": eid, "quote": quote}]}
    row.update(over)
    return row


def window():
    return [source("evt_1", "我最喜欢喝冰可乐了"), source("evt_2", "冰可乐真好喝"),
            source("evt_3", "我今天好累")]


def pending_state(oid, remaining, independent=True, status="needs_reassessment"):
    return {oid: {"status": status, "remaining_supports": list(remaining),
                  "independent_support": independent,
                  "reasons": (["independent_support_remains"] if independent
                              else ["same_root_support_remains"])}}


def belief_record(oid, proposition, spans, tier="reported", **over):
    row = {"object_id": oid, "proposition": proposition, "epistemic_type": tier,
           "subject_ref": "per_1001", "visibility": "private",
           "source_session": "private:1001", "evidence": spans,
           "source_event_ids": [s["event_id"] for s in spans]}
    row.update(over)
    return row


def span(eid, quote):
    return {"event_id": eid, "quote": quote, "span_start": 0, "span_end": len(quote),
            "source_revision": "rev_" + eid, "match_kind": "lexical"}


# ── 纯函数核心：巩固分类（不碰账本）──────────────────────────────
class FateTests(unittest.TestCase):
    def test_reported_claim_with_verbatim_quote_promotes_to_belief(self):
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                             window(), now=NOW)
        self.assertEqual("belief", d["fate"])
        self.assertEqual("promoted_belief", d["reason_code"])
        self.assertTrue(d["promotable"])
        self.assertEqual("reported", d["tier"])
        self.assertTrue(d["spans"])

    def test_experienced_and_looked_up_tiers_are_promotable(self):
        for tier in ("experienced", "looked_up"):
            d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1",
                                       tier=tier), window(), now=NOW)
            self.assertEqual("belief", d["fate"], tier)

    def test_episode_candidate_consolidates_to_experience(self):
        d = mc.classify_fate(claim("o3", "她今天说累了", "我今天好累", "evt_3",
                                   tier="experienced", kind="episode"), window(), now=NOW)
        self.assertEqual("experience", d["fate"])
        self.assertEqual("promoted_experience", d["reason_code"])

    def test_inferred_never_promotes_to_belief(self):
        d = mc.classify_fate(claim("o2", "推断她累了", "我今天好累", "evt_3",
                                   tier="inferred"), window(), now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("inferred_not_promotable", d["reason_code"])
        self.assertFalse(d["promotable"])

    def test_imagined_never_promotes_to_belief(self):
        d = mc.classify_fate(claim("o2", "想象她累了", "我今天好累", "evt_3",
                                   tier="imagined"), window(), now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("imagined_not_promotable", d["reason_code"])

    def test_unknown_tier_is_temporary(self):
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1",
                                   tier="mystery"), window(), now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("unknown_epistemic_type", d["reason_code"])

    def test_missing_object_id_is_temporary(self):
        d = mc.classify_fate(claim("", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                             window(), now=NOW)
        self.assertEqual("missing_object_id", d["reason_code"])

    def test_unknown_kind_is_temporary(self):
        d = mc.classify_fate(claim("o1", "x", "我最喜欢喝冰可乐了", "evt_1", kind="vibe"),
                             window(), now=NOW)
        self.assertEqual("unknown_kind", d["reason_code"])

    def test_invalidated_object_is_not_promoted(self):
        for status in ("invalidated", "superseded"):
            d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                                 window(), states={"o1": {"status": status}}, now=NOW)
            self.assertEqual("temporary", d["fate"], status)
            self.assertEqual("prior_invalidation_preserved", d["reason_code"])

    def test_needs_reassessment_object_routes_to_reassessment(self):
        plan = mc.consolidation_plan(
            [claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")], window(),
            states=pending_state("o1", ["evt_1"]), now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("needs_reassessment_pending", d["reason_code"])
        self.assertEqual(["o1"], plan["reassessment_needed"])

    def test_retracted_source_blocks_promotion(self):
        w = [source("evt_1", "我最喜欢喝冰可乐了", retracted=True)]
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                             w, now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("source_invalidated", d["reason_code"])

    def test_m05_invalidated_source_blocks_promotion(self):
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                             window(), states={"evt_1": {"status": "invalidated"}}, now=NOW)
        self.assertEqual("source_invalidated", d["reason_code"])

    def test_quote_not_verbatim_is_temporary(self):
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "完全对不上的话", "evt_1"),
                             window(), now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("evidence_not_verifiable", d["reason_code"])

    def test_no_evidence_span_is_temporary(self):
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1",
                                   evidence=[]), window(), now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("evidence_not_verifiable", d["reason_code"])

    def test_m04_contradiction_blocks_blind_promotion(self):
        conflicts = [{"verdict": "contradiction", "left_id": "o1", "right_id": "oX"}]
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                             window(), conflicts=conflicts, now=NOW)
        self.assertEqual("temporary", d["fate"])
        self.assertEqual("conflict_contradiction", d["reason_code"])

    def test_m04_deferred_is_not_a_conflict_and_does_not_block(self):
        # deferred = 无法比较（家族未分类），不是冲突信号，绝不据此挡下正常晋升。
        conflicts = [{"verdict": "deferred", "left_id": "o1", "right_id": "oX"}]
        d = mc.classify_fate(claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                             window(), conflicts=conflicts, now=NOW)
        self.assertEqual("belief", d["fate"])

    def test_auto_adjudication_detects_intra_batch_contradiction(self):
        cands = [claim("a", "喜欢茶", "喜欢茶", "evt_1", subject_ref="per_1001"),
                 claim("b", "不喜欢茶", "不喜欢茶", "evt_1", subject_ref="per_1001")]
        w = [source("evt_1", "喜欢茶 不喜欢茶")]
        plan = mc.consolidation_plan(cands, w, now=NOW)
        codes = {d["object_id"]: d["reason_code"] for d in plan["decisions"]}
        self.assertTrue(any(c.startswith("conflict_") for c in codes.values()), codes)


class BudgetTests(unittest.TestCase):
    def promotable_batch(self, n):
        w = [source("e%d" % i, "原句%d" % i) for i in range(n)]
        cands = [claim("o%d" % i, "命题%d" % i, "原句%d" % i, "e%d" % i) for i in range(n)]
        return cands, w

    def test_budget_zero_promotes_nothing(self):
        cands, w = self.promotable_batch(3)
        plan = mc.consolidation_plan(cands, w, budget=0, now=NOW)
        self.assertEqual(0, plan["counts"]["belief"])
        self.assertEqual(3, plan["counts"]["temporary"])
        self.assertTrue(all(d["reason_code"] == "budget_exhausted" for d in plan["decisions"]))

    def test_budget_limits_promotions_and_marks_rest_exhausted(self):
        cands, w = self.promotable_batch(5)
        plan = mc.consolidation_plan(cands, w, budget=2, now=NOW)
        self.assertEqual(2, plan["counts"]["belief"])
        self.assertEqual(3, plan["counts"]["temporary"])
        self.assertEqual(3, sum(1 for d in plan["decisions"]
                                if d["reason_code"] == "budget_exhausted"))

    def test_budget_clamped_to_hard_max(self):
        cands, w = self.promotable_batch(3)
        plan = mc.consolidation_plan(cands, w, budget=10 ** 6, now=NOW)
        self.assertEqual(mc.MAX_BUDGET, plan["budget"])
        self.assertIn("budget_clamped_to_max", plan["budget_notes"])
        self.assertEqual(3, plan["counts"]["belief"])

    def test_default_budget_is_bounded(self):
        cands, w = self.promotable_batch(mc.DEFAULT_BUDGET + 4)
        plan = mc.consolidation_plan(cands, w, now=NOW)
        self.assertEqual(mc.DEFAULT_BUDGET, plan["budget"])
        self.assertEqual(mc.DEFAULT_BUDGET, plan["counts"]["belief"])

    def test_negative_or_non_integer_budget_rejected(self):
        for bad in (-1, 1.5, "3", True):
            with self.subTest(budget=bad):
                with self.assertRaises(ValueError):
                    mc.consolidation_plan([], [], budget=bad, now=NOW)

    def test_priority_is_deterministic_more_valid_sources_first(self):
        w = [source("e1", "原句一"), source("e2", "原句二")]
        single = claim("single", "命题单", "原句一", "e1")
        double = claim("double", "命题双", "原句一", "e1",
                       source_event_ids=["e1", "e2"],
                       evidence=[{"event_id": "e1", "quote": "原句一"},
                                 {"event_id": "e2", "quote": "原句二"}])
        plan = mc.consolidation_plan([single, double], w, budget=1, now=NOW)
        promoted = [d["object_id"] for d in plan["promotions"]]
        self.assertEqual(["double"], promoted)


class DeterminismTests(unittest.TestCase):
    def test_same_inputs_same_plan_id(self):
        cands = [claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")]
        one = mc.consolidation_plan(cands, window(), budget=3, now=NOW)
        two = mc.consolidation_plan(cands, window(), budget=3, now=NOW)
        self.assertEqual(one, two)

    def test_wall_clock_does_not_change_plan_id_or_fates(self):
        cands = [claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")]
        one = mc.consolidation_plan(cands, window(), budget=3, now=2000.0)
        two = mc.consolidation_plan(cands, window(), budget=3, now=99999.0)
        self.assertEqual(one["plan_id"], two["plan_id"])
        self.assertEqual(one["decisions"], two["decisions"])

    def test_input_order_does_not_change_plan_id(self):
        cands = [claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                 claim("o2", "喜欢茶", "冰可乐真好喝", "evt_2")]
        one = mc.consolidation_plan(cands, window(), budget=3, now=NOW)
        two = mc.consolidation_plan(list(reversed(cands)), window(), budget=3, now=NOW)
        self.assertEqual(one["plan_id"], two["plan_id"])
        self.assertEqual({d["object_id"]: d["fate"] for d in one["decisions"]},
                         {d["object_id"]: d["fate"] for d in two["decisions"]})

    def test_plan_does_not_mutate_inputs(self):
        cands = [claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")]
        w = window()
        before = json.dumps([cands, w], sort_keys=True, ensure_ascii=False)
        mc.consolidation_plan(cands, w, budget=3, now=NOW)
        self.assertEqual(before, json.dumps([cands, w], sort_keys=True, ensure_ascii=False))


# ── 内存账本：只追加、内容寻址、纯读（S03 先例）──────────────────
class LedgerTests(unittest.TestCase):
    def plan(self, budget=3):
        return mc.consolidation_plan(
            [claim("o1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
            window(), budget=budget, now=NOW)

    def test_commit_appends_and_marks_committed(self):
        ledger = mc.new_ledger()
        rec = mc.commit_plan(ledger, self.plan())
        self.assertEqual("committed", rec["status"])
        self.assertEqual([rec["plan_id"]], mc.list_records(ledger))

    def test_duplicate_plan_id_rejected(self):
        ledger = mc.new_ledger()
        plan = self.plan()
        mc.commit_plan(ledger, plan)
        with self.assertRaises(ValueError):
            mc.commit_plan(ledger, plan)
        self.assertEqual(1, len(mc.list_records(ledger)))

    def test_tampered_plan_id_rejected(self):
        ledger = mc.new_ledger()
        plan = self.plan()
        plan["plan_id"] = "m08_forged"
        with self.assertRaises(ValueError):
            mc.commit_plan(ledger, plan)
        self.assertEqual([], mc.list_records(ledger))

    def test_tampered_decisions_rejected_at_commit(self):
        ledger = mc.new_ledger()
        plan = self.plan()
        plan["decisions"][0]["fate"] = "temporary"
        plan["decisions"][0]["reason_code"] = "budget_exhausted"
        with self.assertRaises(ValueError):
            mc.commit_plan(ledger, plan)

    def test_plan_does_not_write_ledger(self):
        ledger = mc.new_ledger()
        snapshot = json.dumps(ledger, sort_keys=True)
        mc.commit_plan  # 计划本身纯算
        plan = self.plan()
        self.assertEqual("planned", plan["status"])
        self.assertEqual(snapshot, json.dumps(ledger, sort_keys=True))

    def test_get_record_returns_copy_and_never_writes(self):
        ledger = mc.new_ledger()
        rec = mc.commit_plan(ledger, self.plan())
        snapshot = json.dumps(ledger, sort_keys=True, default=str)
        got = mc.get_record(ledger, rec["plan_id"])
        got["decisions"].clear()
        mc.explain_plan(ledger, rec["plan_id"])
        self.assertEqual(snapshot, json.dumps(ledger, sort_keys=True, default=str))
        self.assertIsNone(mc.get_record(ledger, "m08_missing"))

    def test_explain_plan_answers_why(self):
        ledger = mc.new_ledger()
        rec = mc.commit_plan(ledger, self.plan())
        ex = mc.explain_plan(ledger, rec["plan_id"])
        self.assertEqual(["o1"], ex["promoted"])
        self.assertEqual(["promoted_belief"], ex["reasons"])
        self.assertEqual(3, ex["budget"])

    def test_bad_ledger_rejected(self):
        for bad in (None, {}, {"records": [], "order": []}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mc.commit_plan(bad, self.plan())

    def test_reassessment_commit_append_only_and_tamper_proof(self):
        ledger = mc.new_ledger()
        rec = belief_record("bel_1", "喜欢冰可乐", [span("evt_2", "冰可乐真好喝")])
        plan = mc.plan_reassessment(rec, pending_state("bel_1", ["evt_2"]), [],
                                    [source("evt_2", "冰可乐真好喝")], now=NOW)
        committed = mc.commit_reassessment(ledger, plan)
        self.assertEqual("committed", committed["status"])
        with self.assertRaises(ValueError):
            mc.commit_reassessment(ledger, plan)
        tampered = copy.deepcopy(plan)
        tampered["outcome"] = "blocked"
        with self.assertRaises(ValueError):
            mc.commit_reassessment(ledger, tampered)
        self.assertEqual(1, len(mc.list_records(ledger)))

    def test_explain_reassessment_answers_why(self):
        ledger = mc.new_ledger()
        rec = belief_record("bel_1", "喜欢冰可乐", [span("evt_2", "冰可乐真好喝")])
        plan = mc.plan_reassessment(rec, pending_state("bel_1", ["evt_2"]), [],
                                    [source("evt_2", "冰可乐真好喝")], now=NOW)
        committed = mc.commit_reassessment(ledger, plan)
        ex = mc.explain_reassessment(ledger, committed["reassessment_id"])
        self.assertEqual("revised", ex["outcome"])
        self.assertEqual("bel_1", ex["object_id"])
        self.assertTrue(ex["new_revision_id"])
        self.assertTrue(ex["correction_id"])


# ── 纯函数：显式重评估修订事务（接住 M05 needs_reassessment）──────
class ReassessmentTests(unittest.TestCase):
    def record(self, tier="reported"):
        return belief_record("bel_1", "喜欢冰可乐", [span("evt_2", "冰可乐真好喝")], tier=tier)

    def test_independent_valid_support_revises_to_new_version(self):
        plan = mc.plan_reassessment(self.record(), pending_state("bel_1", ["evt_2"]),
                                    [], [source("evt_2", "冰可乐真好喝")], now=NOW)
        self.assertEqual("revised", plan["outcome"])
        self.assertEqual("independent_valid_support_reverified", plan["reason_code"])
        self.assertEqual("belief_corrected", plan["correction"]["kind"])
        self.assertEqual("bel_1", plan["correction"]["target_id"])
        self.assertEqual("belief", plan["new_candidate"]["object_kind"])
        self.assertNotEqual("bel_1", plan["new_candidate"]["object_id"])
        self.assertTrue(plan["new_edges"])
        self.assertTrue(plan["propagation"])

    def test_invalidated_object_is_blocked_monotonic(self):
        for status in ("invalidated", "superseded"):
            plan = mc.plan_reassessment(self.record(),
                                        pending_state("bel_1", ["evt_2"], status=status),
                                        [], [source("evt_2", "冰可乐真好喝")], now=NOW)
            self.assertEqual("blocked", plan["outcome"], status)
            self.assertEqual("prior_invalidation_preserved", plan["reason_code"])
            self.assertIsNone(plan["correction"])
            self.assertIsNone(plan["new_candidate"])

    def test_active_object_is_not_reassessed(self):
        plan = mc.plan_reassessment(self.record(),
                                    {"bel_1": {"status": "active"}}, [],
                                    [source("evt_2", "冰可乐真好喝")], now=NOW)
        self.assertEqual("deferred", plan["outcome"])
        self.assertEqual("not_pending_reassessment", plan["reason_code"])

    def test_no_valid_support_remains_defers(self):
        states = pending_state("bel_1", ["evt_2"])
        states["evt_2"] = {"status": "invalidated"}
        plan = mc.plan_reassessment(self.record(), states, [],
                                    [source("evt_2", "冰可乐真好喝")], now=NOW)
        self.assertEqual("deferred", plan["outcome"])
        self.assertEqual("no_valid_support_remains", plan["reason_code"])

    def test_same_root_support_only_defers(self):
        plan = mc.plan_reassessment(self.record(),
                                    pending_state("bel_1", ["evt_2"], independent=False),
                                    [], [source("evt_2", "冰可乐真好喝")], now=NOW)
        self.assertEqual("deferred", plan["outcome"])
        self.assertEqual("same_root_support_only", plan["reason_code"])

    def test_non_promotable_tier_defers(self):
        for tier in ("inferred", "imagined"):
            plan = mc.plan_reassessment(self.record(tier=tier),
                                        pending_state("bel_1", ["evt_2"]), [],
                                        [source("evt_2", "冰可乐真好喝")], now=NOW)
            self.assertEqual("deferred", plan["outcome"], tier)
            self.assertEqual("tier_not_promotable", plan["reason_code"])

    def test_evidence_not_verifiable_defers(self):
        plan = mc.plan_reassessment(self.record(), pending_state("bel_1", ["evt_2"]),
                                    [], [source("evt_2", "完全不同的话")], now=NOW)
        self.assertEqual("deferred", plan["outcome"])
        self.assertEqual("evidence_not_verifiable", plan["reason_code"])

    def test_missing_object_id_defers(self):
        plan = mc.plan_reassessment({}, pending_state("bel_1", ["evt_2"]), [],
                                    [source("evt_2", "冰可乐真好喝")], now=NOW)
        self.assertEqual("deferred", plan["outcome"])
        self.assertEqual("missing_object_id", plan["reason_code"])

    def test_independence_read_from_reasons_when_flag_absent(self):
        # read_invalidation_state 不持久化 independent_support 列，只写 reasons。
        states = {"bel_1": {"status": "needs_reassessment", "remaining_supports": ["evt_2"],
                            "reasons": ["independent_support_remains"]}}
        plan = mc.plan_reassessment(self.record(), states, [],
                                    [source("evt_2", "冰可乐真好喝")], now=NOW)
        self.assertEqual("revised", plan["outcome"])
        self.assertTrue(plan["independent_support"])

    def test_remaining_refiltered_against_later_invalidation(self):
        states = pending_state("bel_1", ["evt_2", "evt_3"])
        states["evt_3"] = {"status": "invalidated"}
        rec = belief_record("bel_1", "喜欢冰可乐",
                            [span("evt_2", "冰可乐真好喝"), span("evt_3", "我今天好累")])
        plan = mc.plan_reassessment(rec, states, [],
                                    [source("evt_2", "冰可乐真好喝"),
                                     source("evt_3", "我今天好累")], now=NOW)
        self.assertEqual(["evt_2"], plan["remaining_supports"])
        self.assertEqual(["evt_2"], plan["new_candidate"]["source_event_ids"])

    def test_reassessment_id_is_content_addressed_and_clock_free(self):
        one = mc.plan_reassessment(self.record(), pending_state("bel_1", ["evt_2"]),
                                   [], [source("evt_2", "冰可乐真好喝")], now=NOW)
        two = mc.plan_reassessment(self.record(), pending_state("bel_1", ["evt_2"]),
                                   [], [source("evt_2", "冰可乐真好喝")], now=NOW + 99999)
        self.assertEqual(one["reassessment_id"], two["reassessment_id"])
        self.assertEqual(one["correction"]["correction_id"], two["correction"]["correction_id"])

    def test_new_revision_uses_ledger_id_for_edges_and_correction(self):
        plan = mc.plan_reassessment(self.record(), pending_state("bel_1", ["evt_2"]),
                                    [], [source("evt_2", "冰可乐真好喝")], now=NOW)
        new_oid = plan["new_candidate"]["object_id"]
        ledger_id = "bel_" + mc._sha(new_oid)
        self.assertEqual(ledger_id, plan["correction"]["new_revision_id"])
        self.assertTrue(all(e["derived_id"] == ledger_id for e in plan["new_edges"]))


# ── 隔离账本夹具（DATA_DIR 调用时解析，绝不写真实 data/）──────────
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="m08_", ignore_cleanup_errors=True)
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

    def seed(self, text, dedupe, ts=1000.0):
        return sm.record_event("private_message", session="private:1001",
                               actor_qq="1001", visibility="private",
                               payload={"text": text}, ts=ts, dedupe=dedupe)

    def real_claim(self, oid, content, eid, quote, tier="reported", kind="claim", **over):
        w = sm.source_window([eid])
        row = {"object_kind": kind, "object_id": oid, "content": content,
               "occurred_at": 1000.0, "recorded_at": 1001.0, "source_event_ids": [eid],
               "epistemic_type": tier, "visibility": "private",
               "source_session": "private:1001", "subject_ref": w[0]["actor_ref"],
               "evidence": [{"event_id": eid, "quote": quote}]}
        row.update(over)
        return row

    def counts(self):
        if not Path(self.root, "persona", "cognition.db").exists():
            return {t: 0 for t in TABLES}
        with closing(sm._db()) as c:
            return {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in TABLES}

    def rows(self, table):
        if not Path(self.root, "persona", "cognition.db").exists():
            return []
        with closing(sm._db()) as c:
            return [dict(r) for r in c.execute("SELECT * FROM %s" % table)]

    def seed_pending_belief(self):
        """建一条双支持信念，撤回其一 → M05 needs_reassessment（独立支持仍在）。"""
        e1 = self.seed("我最喜欢喝冰可乐了", "m08:ra1")
        e2 = self.seed("冰可乐真好喝", "m08:ra2", ts=1100.0)
        w = sm.source_window([e1, e2])
        aref = w[0]["actor_ref"]
        cand = {"object_kind": "claim", "object_id": "belobj", "content": "喜欢冰可乐",
                "occurred_at": 1000.0, "recorded_at": 1200.0, "source_event_ids": [e1, e2],
                "epistemic_type": "reported", "visibility": "private",
                "source_session": "private:1001", "subject_ref": aref,
                "evidence": [{"event_id": e1, "quote": "我最喜欢喝冰可乐了"},
                             {"event_id": e2, "quote": "冰可乐真好喝"}]}
        bid = cw.commit(cand, w, now=NOW)["object_ids"]["belief"]
        edges = [{"derived_id": bid, "source_id": e, "derived_kind": "belief",
                  "source_kind": "event", "basis": "test"} for e in (e1, e2)]
        sm.write_invalidation_transaction({"ts": NOW, "expected_epoch": 0, "edges": edges,
                                           "corrections": [], "propagation": None})
        st = dag.read_invalidation_state()
        rec = dag.correction("observation_invalid", e1, "误读", ts=2100.0)
        prop = dag.propagate(st["edges"], [rec], epoch=st["epoch"], states=st["states"], now=2100.0)
        sm.write_invalidation_transaction({"ts": 2100.0, "expected_epoch": st["epoch"],
                                           "edges": [], "corrections": [rec], "propagation": prop})
        return bid, e1, e2, sm.source_window([e1, e2])

    def reassessment_record(self, bid):
        inputs = mc.read_consolidation_inputs()
        rec = next(b for b in inputs["beliefs"] if b["object_id"] == bid)
        rec["epistemic_type"] = "reported"
        rec["source_session"] = "private:1001"
        rec["visibility"] = "private"
        return rec


class BridgeTests(Base):
    def test_absent_ledger_degrades_without_creating(self):
        got = mc.read_consolidation_inputs()
        self.assertEqual([], got["episodes"])
        self.assertEqual([{"store": "cognition", "reason": "absent"}], got["degradation"])
        self.assertEqual([], list(Path(self.root).iterdir()), "只读桥建了目录")

    def test_reads_episodes_beliefs_candidates_and_state(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:b1")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        cw.commit(self.real_claim("o3", "她今天说累了", eid, "我最喜欢喝冰可乐了",
                                  tier="experienced", kind="episode"), w, now=NOW)
        cw.commit(self.real_claim("o2", "推断", eid, "我最喜欢喝冰可乐了", tier="inferred"), w, now=NOW)
        got = mc.read_consolidation_inputs()
        self.assertEqual([], got["degradation"])
        self.assertEqual(1, len(got["beliefs"]))
        self.assertEqual(1, len(got["episodes"]))
        self.assertTrue(got["candidates"])
        self.assertEqual([eid], got["beliefs"][0]["source_event_ids"])
        self.assertTrue(got["beliefs"][0]["evidence"], "belief 原句 span 没重建")

    def test_reads_never_modify_the_ledger(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:b2")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        path = Path(self.root, "persona", "cognition.db")
        before = path.read_bytes()
        mc.read_consolidation_inputs()
        mc.read_consolidation_inputs()
        self.assertEqual(before, path.read_bytes())

    def test_paths_resolve_at_call_time(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:b3")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        self.assertEqual(1, len(mc.read_consolidation_inputs()["beliefs"]))
        with tempfile.TemporaryDirectory(prefix="m08_other_") as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                self.assertEqual([], mc.read_consolidation_inputs()["beliefs"])
        self.assertEqual(1, len(mc.read_consolidation_inputs()["beliefs"]))

    def test_freeze_window_absent_returns_empty_and_creates_nothing(self):
        self.assertEqual([], mc.freeze_window(["evt_x"]))
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_freeze_window_reads_real_events(self):
        eid = self.seed("我今天好累", "m08:b4")
        w = mc.freeze_window([eid, eid])
        self.assertEqual(1, len(w))
        self.assertEqual("我今天好累", w[0]["text"])
        self.assertFalse(w[0]["retracted"])


class ApplyTests(Base):
    def test_consolidation_writes_belief_through_m06(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a1")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        got = mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual("ok", got["status"])
        self.assertEqual(1, got["promoted"])
        self.assertEqual("accept", got["promotions"][0]["decision"])
        self.assertEqual(1, self.counts()["belief"])
        self.assertEqual(1, self.counts()["claim_span"])
        self.assertEqual(1, got["edges_added"])
        self.assertEqual(0, got["epoch"], "巩固不是管理修正，不许推进 epoch")

    def test_consolidation_writes_episode_not_belief(self):
        eid = self.seed("我今天好累", "m08:a2")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o3", "她今天说累了", eid, "我今天好累",
                                                      tier="experienced", kind="episode")],
                                     w, budget=3, now=NOW)
        got = mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual(1, got["promoted"])
        self.assertEqual(1, self.counts()["episode"])
        self.assertEqual(0, self.counts()["belief"])

    def test_inferred_is_never_written_as_fact(self):
        eid = self.seed("我今天好累", "m08:a3")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o2", "推断她累了", eid, "我今天好累",
                                                      tier="inferred")], w, budget=3, now=NOW)
        self.assertEqual(0, len(plan["promotions"]))
        got = mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual(0, got["promoted"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(0, self.counts()["claim_candidate"], "临时信息不该被写成事实或候选")

    def test_repeat_apply_is_idempotent(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a4")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        mc.apply_consolidation(plan, w, now=NOW)
        again = mc.apply_consolidation(plan, w, now=NOW + 500)
        self.assertEqual(1, again["duplicates"])
        self.assertEqual(0, again["promoted"])
        self.assertEqual(1, self.counts()["belief"])

    def test_derivation_edge_uses_ledger_id_and_propagation_targets_belief(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a5")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        got = mc.apply_consolidation(plan, w, now=NOW)
        bid = got["promotions"][0]["ledger_id"]
        st = dag.read_invalidation_state()
        self.assertEqual(bid, st["edges"][0]["derived_id"])
        rec = dag.correction("observation_invalid", eid, "撤回", ts=2600.0)
        prop = dag.propagate(st["edges"], [rec], epoch=st["epoch"], states=st["states"], now=2600.0)
        sm.write_invalidation_transaction({"ts": 2600.0, "expected_epoch": st["epoch"],
                                           "edges": [], "corrections": [rec], "propagation": prop})
        self.assertEqual("invalidated", dag.read_invalidation_state()["states"][bid]["status"])

    def test_retracted_source_is_not_promoted(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a6")
        sm.retract_evidence(eid, "主人撤回")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        self.assertEqual(0, len(plan["promotions"]))
        self.assertEqual("source_invalidated", plan["decisions"][0]["reason_code"])
        self.assertTrue(sm.get_event(eid), "撤回删除了原始观察")

    def test_gate_demotion_registers_no_edges(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a7")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        real_commit = cw.commit

        def demote(candidate, win, now=None):
            return {"decision": "quarantine", "reasons": ["injected_demotion"], "object_ids": {}}

        with patch.object(cw, "commit", demote):
            got = mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual(1, got["deferred"])
        self.assertEqual(0, got["edges_added"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(0, self.counts()["derivation_edge"])

    def test_empty_plan_writes_nothing(self):
        plan = mc.consolidation_plan([], [], budget=3, now=NOW)
        got = mc.apply_consolidation(plan, [], now=NOW)
        self.assertEqual(0, got["promoted"])
        self.assertFalse(Path(self.root, "persona").exists())

    def test_bad_plan_rejected(self):
        with self.assertRaises(ValueError):
            mc.apply_consolidation({"nope": 1}, [], now=NOW)
        with self.assertRaises(ValueError):
            mc.apply_reassessment({"outcome": "bogus"}, [], now=NOW)

    def test_reassessment_writes_new_version_and_supersedes_old(self):
        bid, e1, e2, w = self.seed_pending_belief()
        st = dag.read_invalidation_state()
        self.assertEqual("needs_reassessment", st["states"][bid]["status"])
        plan = mc.plan_reassessment(self.reassessment_record(bid), st["states"], st["edges"],
                                    w, epoch=st["epoch"], now=2200.0)
        got = mc.apply_reassessment(plan, w, now=2200.0)
        self.assertEqual("revised", got["outcome"])
        self.assertEqual("accept", got["decision"])
        self.assertTrue(got["new_belief_id"])
        self.assertNotEqual(bid, got["new_belief_id"])
        self.assertEqual(2, self.counts()["belief"], "新版本不是另立而是覆盖")
        self.assertEqual("superseded", dag.read_invalidation_state()["states"][bid]["status"])
        self.assertEqual(2, got["epoch"])
        self.assertTrue(got["correction_id"])
        old = next(b for b in self.rows("belief") if b["id"] == bid)
        self.assertEqual("喜欢冰可乐", old["proposition"], "旧信念文本被抹掉了")

    def test_reassessment_deferred_writes_nothing(self):
        bid, e1, e2, w = self.seed_pending_belief()
        st = dag.read_invalidation_state()
        rec = self.reassessment_record(bid)
        rec["epistemic_type"] = "inferred"
        plan = mc.plan_reassessment(rec, st["states"], st["edges"], w, epoch=st["epoch"], now=2200.0)
        self.assertEqual("deferred", plan["outcome"])
        before = self.counts()
        got = mc.apply_reassessment(plan, w, now=2200.0)
        self.assertEqual("deferred", got["outcome"])
        self.assertEqual(before, self.counts())
        self.assertEqual("needs_reassessment", dag.read_invalidation_state()["states"][bid]["status"])

    def test_reassessment_blocked_never_revives_invalidated(self):
        bid, e1, e2, w = self.seed_pending_belief()
        st = dag.read_invalidation_state()
        rec = dag.correction("observation_invalid", e2, "也撤回", ts=2150.0)
        prop = dag.propagate(st["edges"], [rec], epoch=st["epoch"], states=st["states"], now=2150.0)
        sm.write_invalidation_transaction({"ts": 2150.0, "expected_epoch": st["epoch"],
                                           "edges": [], "corrections": [rec], "propagation": prop})
        st = dag.read_invalidation_state()
        self.assertEqual("invalidated", st["states"][bid]["status"])
        plan = mc.plan_reassessment(self.reassessment_record(bid), st["states"], st["edges"],
                                    w, epoch=st["epoch"], now=2200.0)
        self.assertEqual("blocked", plan["outcome"])
        before = self.counts()
        got = mc.apply_reassessment(plan, w, now=2200.0)
        self.assertEqual("blocked", got["outcome"])
        self.assertEqual(before, self.counts())
        self.assertEqual("invalidated", dag.read_invalidation_state()["states"][bid]["status"])

    def test_reassessment_gate_demotion_does_not_supersede_old(self):
        bid, e1, e2, w = self.seed_pending_belief()
        st = dag.read_invalidation_state()
        plan = mc.plan_reassessment(self.reassessment_record(bid), st["states"], st["edges"],
                                    w, epoch=st["epoch"], now=2200.0)

        def demote(candidate, win, now=None):
            return {"decision": "quarantine", "reasons": ["injected"], "object_ids": {}}

        with patch.object(cw, "commit", demote):
            got = mc.apply_reassessment(plan, w, now=2200.0)
        self.assertEqual("deferred", got["outcome"])
        self.assertEqual("revision_gate_demoted", got["reason_code"])
        self.assertEqual("needs_reassessment", dag.read_invalidation_state()["states"][bid]["status"])
        self.assertEqual(1, self.counts()["belief"])

    def test_injected_failure_rolls_back_consolidation(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a8")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        real_db = sm._db

        class FailingConnection:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if "INTO belief" in sql:
                    raise sqlite3.OperationalError("injected disk failure")
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch.object(sm, "_db", lambda: FailingConnection(real_db())):
            with self.assertRaises(sqlite3.OperationalError):
                mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual(0, self.counts()["belief"])
        got = mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual(1, got["promoted"])

    def test_projection_carries_provenance_and_limitations(self):
        eid = self.seed("我最喜欢喝冰可乐了", "m08:a9")
        w = sm.source_window([eid])
        plan = mc.consolidation_plan([self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                     w, budget=3, now=NOW)
        got = mc.apply_consolidation(plan, w, now=NOW)
        self.assertEqual("m08-1", got["provenance"]["algorithm_version"])
        self.assertTrue(any("L02" in line for line in got["limitations"]))
        self.assertTrue(any("预算" in line for line in got["limitations"]))


# ── AST 静态锁：钉死旁路契约 ─────────────────────────────────────
class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "memory_consolidation.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def calls(self):
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)]

    def call_name(self, call):
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return ""

    def test_imports_only_contracts_ledger_and_stdlib(self):
        allowed = {"copy", "hashlib", "json", "os", "sqlite3", "time", "pathlib",
                   "claim_conflicts", "claim_provenance", "cognition_admission",
                   "cognition_writer", "invalidation_dag", "self_model"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             "巩固模块 import 了写入/网络/模型 API：%s" % sorted(names - allowed))

    def test_no_write_sql_anywhere_in_the_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper, "巩固模块里出现写入 SQL：%s" % node.value[:60])

    def test_no_legacy_writers_or_direct_connections(self):
        forbidden = {"add_belief", "record_episode", "quarantine_claim", "record_event",
                     "note_self_view", "revise_belief", "retract_evidence", "add_evidence",
                     "add_commitment", "add_goal", "record_action", "_db", "_transaction",
                     "executescript", "executemany", "rollback", "makedirs", "mkdir",
                     "write_text", "write_bytes", "remove", "unlink", "rmtree", "rename",
                     "write_cognition_transaction"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), forbidden,
                             "巩固模块绕过既有入口写账：%s" % self.call_name(call))

    def test_writes_go_only_through_m06_and_m05_entries(self):
        commits, edge_writes = [], []
        for call in self.calls():
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
        self.assertTrue(edge_writes, "不再经 M05 注册依赖边/修正了")

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
        bridge = {"_data_root", "_persona_root", "_cognition_path", "_ro",
                  "read_consolidation_inputs"}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name in bridge:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "isfile"),
                                     "%s 里解析了路径；巩固核心必须是纯函数" % node.name)

    def test_reuses_frozen_predecessors_not_a_second_engine(self):
        wanted = {("invalidation_dag", "propagate"), ("invalidation_dag", "correction"),
                  ("claim_conflicts", "adjudicate"), ("cognition_admission", "admit"),
                  ("claim_provenance", "bind_claim")}
        found = set()
        for call in self.calls():
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                found.add((call.func.value.id, call.func.attr))
        self.assertEqual(set(), wanted - found,
                         "没有复用冻结前置：%s" % sorted(wanted - found))

    def test_no_second_dynamics_or_recovery_formula(self):
        for call in self.calls():
            if isinstance(call.func, ast.Attribute):
                self.assertNotEqual(call.func.attr, "exp",
                                    "巩固模块不许自带第二套动力学/恢复公式")

    def test_fate_tier_and_budget_constants_are_fixed(self):
        self.assertEqual(("experience", "belief", "temporary"), mc.FATES)
        self.assertEqual(("experienced", "reported", "looked_up"), mc.PROMOTABLE_TIERS)
        self.assertEqual(("inferred", "imagined"), mc.NON_PROMOTABLE_TIERS)
        self.assertEqual(("revised", "deferred", "blocked"), mc.REASSESSMENT_OUTCOMES)
        self.assertGreater(mc.MAX_BUDGET, 0)
        self.assertGreaterEqual(mc.MAX_BUDGET, mc.DEFAULT_BUDGET)
        self.assertNotIn("deferred", mc._BLOCKING_VERDICTS)
        self.assertIn("contradiction", mc._BLOCKING_VERDICTS)

    def test_limitations_are_honest(self):
        text = " ".join(mc.LIMITATIONS)
        self.assertIn("L02", text)
        self.assertIn("C04", text)
        self.assertIn("预算", text)
        self.assertIn("单调", text)
        self.assertIn("cognition_writer.commit", text)


if __name__ == "__main__":
    unittest.main()