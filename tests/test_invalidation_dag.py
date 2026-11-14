# -*- coding: utf-8 -*-
"""M05 派生来源失效传播的回归锁（统一方案 §20.5）。

钉死的行为：
  · 边方向 derived→source，建边前查环，原始观察不能当派生端；
  · 四类修正语义分开：撤源不连坐解释的原事件、解释纠正不连坐源、
    信念修订保留历史、范围收紧不改真假；
  · 失去支持但有其他有效支持 → needs_reassessment，绝不无脑恢复 active；
    没有支持 → 不能继续作当前已核验事实；同根源转述不算独立支持；
  · epoch 原子推进 + 快照发布；旧投影不得复活已撤回的源；
  · 缓存 key 五分量缺一不可，日历日期不足以控制失效；
  · 情感修正只产出新 revision 计划，读路径不启动写流程；
  · 只读桥缺库缺文件返回空/降级，绝不建库建表建目录（X15）。
AST 锁钉死纯函数契约：无写入 SQL、无顶层冻路径、只读连接 mode=ro。
"""
import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import invalidation_dag as dag  # noqa: E402
import self_model as sm  # noqa: E402


def edge(derived, source, dk="interpretation", sk="event"):
    return {"derived_id": derived, "source_id": source,
            "derived_kind": dk, "source_kind": sk, "basis": "test"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="m05_", ignore_cleanup_errors=True)
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

    def cognition_path(self):
        return Path(self.root) / "persona" / "cognition.db"

    def seed_event(self, dedupe, text="原话"):
        return sm.record_event("private_message", session="private:1001",
                               actor_qq="1001", payload={"text": text}, dedupe=dedupe)

    def seed_belief(self, object_id, event_id, proposition="喜欢茶"):
        return sm.write_cognition_transaction({
            "ts": 100.0,
            "belief": {"object_id": object_id, "proposition": proposition,
                       "subject_ref": "1001", "confidence": None,
                       "source": "test",
                       "spans": [{"event_id": event_id, "quote": "原话",
                                  "span_start": 0, "span_end": 2}]}})["belief"]

    def commit(self, edges, corrections, epoch=None, states=None):
        if epoch is None:
            epoch = dag.read_invalidation_state()["epoch"]
        plan = dag.propagate(edges, corrections, epoch=epoch, states=states, now=1000.0)
        result = sm.write_invalidation_transaction({
            "ts": 1000.0, "expected_epoch": plan["expected_epoch"],
            "edges": edges, "corrections": plan["corrections"],
            "propagation": plan})
        return plan, result


class GraphTests(unittest.TestCase):
    def test_cycle_is_rejected_before_the_edge_exists(self):
        edges = [edge("B", "A"), edge("C", "B")]
        self.assertFalse(dag.check_edge(edges, "A", "C")["ok"])
        self.assertEqual("cycle_rejected", dag.check_edge(edges, "A", "C")["reason"])

    def test_self_dependency_is_rejected(self):
        self.assertEqual("self_dependency", dag.check_edge([], "A", "A")["reason"])

    def test_raw_observation_cannot_depend_on_its_interpretation(self):
        checked = dag.check_edge([], "evt_1", "interp_1", derived_kind="event")
        self.assertFalse(checked["ok"])
        self.assertEqual("raw_observation_cannot_derive", checked["reason"])

    def test_duplicate_edge_is_idempotent(self):
        edges = [edge("B", "A")]
        self.assertTrue(dag.check_edge(edges, "B", "A")["ok"])
        self.assertEqual("edge_exists", dag.check_edge(edges, "B", "A")["reason"])

    def test_missing_ids_are_rejected(self):
        self.assertEqual("missing_object_id", dag.check_edge([], "", "A")["reason"])
        self.assertEqual("missing_object_id", dag.check_edge([], "B", " ")["reason"])

    def test_build_graph_dedupes_and_sorts_without_mutating_input(self):
        edges = [edge("B", "A"), edge("B", "A"), edge("C", "A")]
        snapshot = json.dumps(edges, sort_keys=True)
        graph = dag.build_graph(edges)
        self.assertEqual(["A"], graph["forward"]["B"])
        self.assertEqual(["B", "C"], graph["reverse"]["A"])
        self.assertEqual(snapshot, json.dumps(edges, sort_keys=True))


class CorrectionRecordTests(unittest.TestCase):
    def test_reason_is_required(self):
        with self.assertRaises(ValueError):
            dag.correction("observation_invalid", "evt_1", "  ")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            dag.correction("delete_everything", "evt_1", "why")

    def test_scope_revoked_requires_scope(self):
        with self.assertRaises(ValueError):
            dag.correction("scope_revoked", "obj", "why")

    def test_belief_corrected_requires_new_revision(self):
        with self.assertRaises(ValueError):
            dag.correction("belief_corrected", "bel_1", "why")

    def test_same_content_is_idempotent_across_time(self):
        a = dag.correction("observation_invalid", "evt_1", "误读", ts=1.0)
        b = dag.correction("observation_invalid", "evt_1", "误读", ts=2.0)
        self.assertEqual(a["correction_id"], b["correction_id"])


class PropagationTests(unittest.TestCase):
    def test_single_support_loss_invalidates_the_derived_object(self):
        edges = [edge("bel_1", "evt_1", "belief")]
        rec = dag.correction("observation_invalid", "evt_1", "误读", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        by_id = {e["object_id"]: e for e in plan["affected"]}
        self.assertEqual("invalidated", by_id["evt_1"]["status"])
        self.assertEqual("invalidated", by_id["bel_1"]["status"])
        self.assertIn("no_valid_support", by_id["bel_1"]["reasons"])
        self.assertEqual([], by_id["bel_1"]["remaining_supports"])

    def test_multi_hop_propagation_visits_each_object_once(self):
        edges = [edge("epi_1", "evt_1", "episode"), edge("sum_1", "epi_1", "summary"),
                 edge("sum_2", "epi_1", "summary"), edge("cache_1", "sum_1", "identity_cache")]
        rec = dag.correction("observation_invalid", "evt_1", "误读", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=3, now=10.0)
        ids = [e["object_id"] for e in plan["affected"]]
        self.assertEqual(len(ids), len(set(ids)), "同一对象被评估了多次")
        self.assertEqual({"evt_1", "epi_1", "sum_1", "sum_2", "cache_1"}, set(ids))
        self.assertEqual(4, plan["epoch"])
        self.assertEqual(3, plan["expected_epoch"])
        self.assertEqual(sorted(ids), plan["pending_recompute"])
        self.assertEqual(4, plan["snapshot"]["epoch"])
        self.assertIn("cache_1", plan["snapshot"]["pending_recompute"])

    def test_independent_remaining_support_goes_to_reassessment_not_active(self):
        edges = [edge("bel_1", "evt_1", "belief"), edge("bel_1", "evt_2", "belief")]
        rec = dag.correction("observation_invalid", "evt_1", "误读", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        entry = {e["object_id"]: e for e in plan["affected"]}["bel_1"]
        self.assertEqual("needs_reassessment", entry["status"])
        self.assertTrue(entry["independent_support"])
        self.assertEqual(["evt_2"], entry["remaining_supports"])
        self.assertIn("independent_support_remains", entry["reasons"])

    def test_losing_all_support_invalidates(self):
        edges = [edge("bel_1", "evt_1", "belief"), edge("bel_1", "evt_2", "belief")]
        recs = [dag.correction("observation_invalid", "evt_1", "误读", ts=1.0),
                dag.correction("observation_invalid", "evt_2", "也误读", ts=1.0)]
        plan = dag.propagate(edges, recs, epoch=0, now=10.0)
        entry = {e["object_id"]: e for e in plan["affected"]}["bel_1"]
        self.assertEqual("invalidated", entry["status"])

    def test_same_root_restatement_is_not_independent_support(self):
        edges = [edge("interp_1", "evt_1"), edge("bel_1", "evt_1", "belief"),
                 edge("bel_1", "interp_1", "belief")]
        rec = dag.correction("observation_invalid", "evt_1", "误读", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        entry = {e["object_id"]: e for e in plan["affected"]}["bel_1"]
        # interp_1 也失效了，bel_1 没有任何有效支持。
        self.assertEqual("invalidated", entry["status"])
        # 换一种修正：只纠正解释，剩余支持 evt_1 与失效解释同根源，
        # 登记但不算独立支持。
        rec2 = dag.correction("interpretation_revised", "interp_1", "解释错了", ts=1.0)
        plan2 = dag.propagate(edges, [rec2], epoch=0, now=10.0)
        entry2 = {e["object_id"]: e for e in plan2["affected"]}["bel_1"]
        self.assertEqual("needs_reassessment", entry2["status"])
        self.assertFalse(entry2["independent_support"])
        self.assertIn("same_root_support_remains", entry2["reasons"])

    def test_interpretation_revised_preserves_the_original_event(self):
        edges = [edge("interp_1", "evt_1"), edge("bel_1", "interp_1", "belief"),
                 edge("bel_1", "evt_1", "belief")]
        rec = dag.correction("interpretation_revised", "interp_1", "解释错了", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        by_id = {e["object_id"]: e for e in plan["affected"]}
        self.assertEqual("invalidated", by_id["interp_1"]["status"])
        self.assertIn("original_event_preserved", by_id["interp_1"]["reasons"])
        self.assertNotIn("evt_1", by_id, "真实原事件被解释纠正连坐了")
        self.assertEqual("needs_reassessment", by_id["bel_1"]["status"])
        self.assertEqual(["evt_1"], by_id["bel_1"]["remaining_supports"])

    def test_belief_corrected_supersedes_old_version_and_keeps_history(self):
        edges = [edge("bel_1", "evt_1", "belief"), edge("sum_1", "bel_1", "summary")]
        rec = dag.correction("belief_corrected", "bel_1", "口味变了",
                             new_revision_id="bel_2", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        by_id = {e["object_id"]: e for e in plan["affected"]}
        self.assertEqual("superseded", by_id["bel_1"]["status"])
        self.assertIn("history_preserved", by_id["bel_1"]["reasons"])
        self.assertEqual("needs_reassessment", by_id["sum_1"]["status"])
        self.assertIn("support_superseded_by_revision", by_id["sum_1"]["reasons"])
        self.assertNotIn("bel_2", by_id, "新修订对象不该被旧版本的修正波及")

    def test_scope_revoked_narrows_scope_without_touching_truth(self):
        edges = [edge("sum_1", "bel_1", "summary"), edge("cache_1", "sum_1", "retrieval_cache")]
        rec = dag.correction("scope_revoked", "bel_1", "主人撤回授权",
                             scope="group:55", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        by_id = {e["object_id"]: e for e in plan["affected"]}
        for oid in ("bel_1", "sum_1", "cache_1"):
            self.assertEqual("scope_narrowed", by_id[oid]["status"])
            self.assertIn("scope_revoked_no_truth_change", by_id[oid]["reasons"])
            self.assertEqual(["group:55"], by_id[oid]["revoked_scopes"])

    def test_invalidation_is_monotonic_and_never_returns_active(self):
        edges = [edge("bel_1", "evt_1", "belief"), edge("bel_1", "evt_2", "belief")]
        rec = dag.correction("observation_invalid", "evt_1", "误读", ts=1.0)
        prior = {"bel_1": {"status": "invalidated"}}
        plan = dag.propagate(edges, [rec], epoch=1, states=prior, now=10.0)
        entry = {e["object_id"]: e for e in plan["affected"]}["bel_1"]
        self.assertEqual("invalidated", entry["status"])
        self.assertIn("prior_invalidation_preserved", entry["reasons"])
        for item in plan["affected"]:
            self.assertNotEqual("active", item["status"])

    def test_prior_retracted_source_still_counts_as_invalid_support(self):
        edges = [edge("bel_1", "evt_1", "belief"), edge("bel_1", "evt_2", "belief")]
        rec = dag.correction("observation_invalid", "evt_2", "也误读", ts=1.0)
        prior = {"evt_1": {"status": "invalidated"}}
        plan = dag.propagate(edges, [rec], epoch=1, states=prior, now=10.0)
        entry = {e["object_id"]: e for e in plan["affected"]}["bel_1"]
        self.assertEqual("invalidated", entry["status"])

    def test_no_corrections_does_not_advance_epoch(self):
        plan = dag.propagate([edge("B", "A")], [], epoch=7, now=10.0)
        self.assertEqual(7, plan["epoch"])
        self.assertEqual([], plan["affected"])

    def test_bad_records_are_rejected_loudly(self):
        with self.assertRaises(ValueError):
            dag.propagate([], [{"kind": "nope", "target_id": "x", "reason": "y"}])
        with self.assertRaises(ValueError):
            dag.propagate([], [{"kind": "observation_invalid", "target_id": "x", "reason": ""}])


class TransactionTests(Base):
    def test_full_commit_publishes_epoch_state_and_snapshot_atomically(self):
        evt = self.seed_event("m05:1")
        bel = self.seed_belief("b1", evt)
        edges = [edge(bel, evt, "belief")]
        rec = dag.correction("observation_invalid", evt, "误读", ts=1.0)
        plan, result = self.commit(edges, [rec])
        self.assertEqual(1, result["epoch"])
        state = dag.read_invalidation_state()
        self.assertEqual(1, state["epoch"])
        self.assertEqual("invalidated", state["states"][bel]["status"])
        self.assertEqual(1, len(state["corrections"]))
        self.assertEqual(1, len(state["snapshots"]))
        self.assertEqual(plan["snapshot"]["snapshot_id"], state["snapshots"][0]["snapshot_id"])
        self.assertEqual(1, len(state["edges"]))

    def test_epoch_conflict_rolls_back_everything(self):
        evt = self.seed_event("m05:2")
        edges = [edge("bel_x", evt, "belief")]
        rec = dag.correction("observation_invalid", evt, "误读", ts=1.0)
        plan = dag.propagate(edges, [rec], epoch=0, now=10.0)
        # 先把账本推进到 epoch 1，再拿 epoch 0 的旧计划提交。
        self.commit([], [dag.correction("observation_invalid", evt, "先撤一次", ts=1.0)])
        with self.assertRaises(ValueError):
            sm.write_invalidation_transaction({
                "ts": 2.0, "expected_epoch": 0, "edges": edges,
                "corrections": plan["corrections"], "propagation": plan})
        state = dag.read_invalidation_state()
        self.assertEqual(1, state["epoch"], "冲突事务推进了 epoch")
        self.assertEqual([], state["edges"], "冲突事务留下了半截边")
        self.assertEqual(1, len(state["corrections"]))

    def test_cycle_edge_rolls_back_the_whole_plan(self):
        evt = self.seed_event("m05:3")
        edges = [edge("A", evt), edge("B", "A"), edge(evt, "B")]
        rec = dag.correction("observation_invalid", evt, "误读", ts=1.0)
        plan = dag.propagate(edges[:2], [rec], epoch=0, now=10.0)
        with self.assertRaises(ValueError):
            sm.write_invalidation_transaction({
                "ts": 1.0, "expected_epoch": 0, "edges": edges,
                "corrections": plan["corrections"], "propagation": plan})
        state = dag.read_invalidation_state()
        self.assertEqual(0, state["epoch"])
        self.assertEqual([], state["edges"])
        self.assertEqual([], state["corrections"])
        self.assertEqual({}, state["states"])

    def test_unknown_correction_kind_rolls_back(self):
        evt = self.seed_event("m05:4")
        rec = dag.correction("observation_invalid", evt, "误读", ts=1.0)
        plan = dag.propagate([], [rec], epoch=0, now=10.0)
        plan["corrections"][0]["kind"] = "drop_table"
        with self.assertRaises(ValueError):
            sm.write_invalidation_transaction({
                "ts": 1.0, "expected_epoch": 0, "edges": [],
                "corrections": plan["corrections"], "propagation": plan})
        self.assertEqual(0, dag.read_invalidation_state()["epoch"])

    def test_propagation_epoch_mismatch_rolls_back(self):
        evt = self.seed_event("m05:5")
        rec = dag.correction("observation_invalid", evt, "误读", ts=1.0)
        plan = dag.propagate([], [rec], epoch=0, now=10.0)
        plan["epoch"] = 99
        with self.assertRaises(ValueError):
            sm.write_invalidation_transaction({
                "ts": 1.0, "expected_epoch": 0, "edges": [],
                "corrections": plan["corrections"], "propagation": plan})
        self.assertEqual(0, dag.read_invalidation_state()["epoch"])

    def test_same_correction_resubmitted_is_idempotent(self):
        evt = self.seed_event("m05:6")
        rec = dag.correction("observation_invalid", evt, "误读", ts=1.0)
        self.commit([], [rec])
        self.commit([], [rec])
        state = dag.read_invalidation_state()
        self.assertEqual(1, len(state["corrections"]), "同一修正被写成了两条")
        self.assertEqual(2, state["epoch"])

    def test_edges_only_plan_does_not_advance_epoch(self):
        evt = self.seed_event("m05:7")
        sm.write_invalidation_transaction({
            "ts": 1.0, "expected_epoch": 0,
            "edges": [edge("cache_x", evt, "retrieval_cache")],
            "corrections": [], "propagation": None})
        state = dag.read_invalidation_state()
        self.assertEqual(0, state["epoch"])
        self.assertEqual(1, len(state["edges"]))

    def test_unknown_plan_key_is_rejected(self):
        with self.assertRaises(ValueError):
            sm.write_invalidation_transaction({"ts": 1.0, "surprise": True})

    def test_second_correction_builds_on_persisted_state(self):
        evt1 = self.seed_event("m05:8a")
        evt2 = self.seed_event("m05:8b")
        bel = self.seed_belief("b8", evt1)
        sm.write_cognition_transaction({
            "ts": 100.0, "belief": {"object_id": "b8", "proposition": "喜欢茶",
                                    "subject_ref": "1001", "confidence": None,
                                    "source": "test",
                                    "spans": [{"event_id": evt2, "quote": "原话",
                                               "span_start": 0, "span_end": 2}]}})
        edges = [edge(bel, evt1, "belief"), edge(bel, evt2, "belief")]
        self.commit(edges, [dag.correction("observation_invalid", evt1, "误读", ts=1.0)])
        state = dag.read_invalidation_state()
        self.assertEqual("needs_reassessment", state["states"][bel]["status"])
        plan, _ = self.commit(edges, [dag.correction("observation_invalid", evt2, "也误读", ts=2.0)],
                              states=state["states"])
        entry = {e["object_id"]: e for e in plan["affected"]}[bel]
        self.assertEqual("invalidated", entry["status"])
        self.assertEqual("invalidated",
                         dag.read_invalidation_state()["states"][bel]["status"])


class ReadGateTests(unittest.TestCase):
    def test_stale_projection_cannot_resurrect_a_retracted_source(self):
        states = {"bel_1": {"status": "invalidated"}}
        old_projection = [{"object_id": "bel_1", "projection_epoch": 0, "text": "旧结论"}]
        kept, dropped = dag.read_gate(old_projection, states, current_epoch=1)
        self.assertEqual([], kept)
        self.assertEqual({"invalidated": 1}, dropped)
        # 即使投影自称是当前 epoch，失效状态本身也挡下它。
        fresh = [{"object_id": "bel_1", "projection_epoch": 1}]
        kept, dropped = dag.read_gate(fresh, states, current_epoch=1)
        self.assertEqual([], kept)

    def test_projection_without_epoch_declaration_is_dropped(self):
        kept, dropped = dag.read_gate([{"object_id": "bel_1"}], {}, current_epoch=0)
        self.assertEqual([], kept)
        self.assertEqual({"missing_projection_epoch": 1}, dropped)

    def test_old_epoch_projection_is_dropped_even_if_state_is_clean(self):
        items = [{"object_id": "sum_1", "projection_epoch": 2}]
        kept, dropped = dag.read_gate(items, {}, current_epoch=3)
        self.assertEqual([], kept)
        self.assertEqual({"stale_projection": 1}, dropped)

    def test_needs_reassessment_is_not_a_current_verified_fact(self):
        states = {"bel_1": {"status": "needs_reassessment"}}
        kept, dropped = dag.read_gate([{"object_id": "bel_1", "projection_epoch": 1}],
                                      states, current_epoch=1)
        self.assertEqual([], kept)
        self.assertEqual({"needs_reassessment": 1}, dropped)

    def test_revoked_scope_blocks_only_that_scope(self):
        states = {"sum_1": {"status": "scope_narrowed", "revoked_scopes": ["group:55"]}}
        items = [{"object_id": "sum_1", "projection_epoch": 1}]
        kept, _ = dag.read_gate(items, states, current_epoch=1, scope="private:1001")
        self.assertEqual(items, kept)
        kept, dropped = dag.read_gate(items, states, current_epoch=1, scope="group:55")
        self.assertEqual([], kept)
        self.assertEqual({"scope_revoked": 1}, dropped)

    def test_current_clean_projection_passes(self):
        items = [{"object_id": "bel_1", "projection_epoch": 4}]
        kept, dropped = dag.read_gate(items, {}, current_epoch=4)
        self.assertEqual(items, kept)
        self.assertEqual({}, dropped)


class CacheKeyTests(unittest.TestCase):
    def test_all_five_components_are_required(self):
        for missing in ("object_revision", "source_revision", "validity_epoch", "scope"):
            parts = {"object_revision": "r1", "source_revision": "s1",
                     "validity_epoch": 1, "scope": "private:1001"}
            parts[missing] = "" if missing != "validity_epoch" else None
            result = dag.cache_key(**parts)
            self.assertFalse(result["ok"], missing)
            self.assertIn("missing_" + missing, result["problems"])

    def test_calendar_date_alone_cannot_build_a_key(self):
        result = dag.cache_key("2026-11-12", "", None, "")
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(len(result["problems"]), 3)

    def test_epoch_change_changes_the_key_within_the_same_day(self):
        a = dag.cache_key("r1", "s1", 1, "private:1001")
        b = dag.cache_key("r1", "s1", 2, "private:1001")
        self.assertTrue(a["ok"] and b["ok"])
        self.assertNotEqual(a["key"], b["key"])

    def test_source_revision_and_scope_change_the_key(self):
        base = dag.cache_key("r1", "s1", 1, "private:1001")
        self.assertNotEqual(base["key"], dag.cache_key("r1", "s2", 1, "private:1001")["key"])
        self.assertNotEqual(base["key"], dag.cache_key("r1", "s1", 1, "group:55")["key"])
        self.assertNotEqual(base["key"], dag.cache_key("r2", "s1", 1, "private:1001")["key"])

    def test_key_is_stable_for_identical_inputs(self):
        a = dag.cache_key("r1", "s1", 1, "private:1001")
        b = dag.cache_key("r1", "s1", "1", "private:1001")
        self.assertEqual(a["key"], b["key"])

    def test_negative_epoch_is_rejected(self):
        self.assertFalse(dag.cache_key("r1", "s1", -1, "private:1001")["ok"])


class EmotionRevisionTests(Base):
    def test_revision_plan_branches_without_touching_history(self):
        plan = dag.plan_emotion_revision("emo_old", "cor_1", {"valence": -0.2}, ts=5.0)
        self.assertEqual("emo_old", plan["parent_revision_id"])
        self.assertEqual("revision", plan["branch"])
        self.assertEqual("preserved", plan["history"])
        self.assertTrue(plan["new_revision_id"].startswith("emo_"))
        self.assertNotEqual("emo_old", plan["new_revision_id"])
        self.assertFalse(self.cognition_path().exists(), "纯计划函数建了库")

    def test_revision_plan_requires_parent_and_trigger(self):
        with self.assertRaises(ValueError):
            dag.plan_emotion_revision("", "cor_1", {})
        with self.assertRaises(ValueError):
            dag.plan_emotion_revision("emo_old", "", {})


class BridgeTests(Base):
    def test_absent_ledger_returns_defaults_without_creating_anything(self):
        state = dag.read_invalidation_state()
        self.assertEqual(0, state["epoch"])
        self.assertEqual([], state["edges"])
        inputs = dag.read_derivation_inputs()
        self.assertEqual([], inputs["edges"])
        self.assertTrue(inputs["degradation"])
        self.assertFalse(Path(self.root, "persona").exists(), "只读桥建了目录")
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_cognition_edges_cover_belief_episode_commitment_self_view_goal(self):
        evt = self.seed_event("m05:b1")
        bel = self.seed_belief("bb", evt)
        epi = sm.record_episode("一段经历", event_ids=[evt], ts=100.0)
        cmt = sm.add_commitment("1001", "答应的事", evidence=[evt], ts=100.0)
        view = sm.note_self_view("我是谁", evidence=[evt], ts=100.0)
        goal = sm.add_goal("目标", parent_event=evt, ts=100.0)
        inputs = dag.read_derivation_inputs()
        pairs = {(e["derived_id"], e["source_id"]) for e in inputs["edges"]}
        self.assertIn((bel, evt), pairs)
        self.assertIn((epi, evt), pairs)
        self.assertIn((cmt, evt), pairs)
        self.assertIn((view, evt), pairs)
        self.assertIn((goal, evt), pairs)

    def test_persona_group_knowledge_edges_and_summary_unknown_support(self):
        import persona_store
        evt = self.seed_event("m05:b2")
        persona_store.add("facts", "主人喜欢茶", ts=100.0, source_event_ids=[evt])
        memory = Path(self.root, "memory")
        memory.mkdir(exist_ok=True)
        memory.joinpath("group_facts.json").write_text(json.dumps({
            "groups": {"55": {"entries": [{"id": "g1", "text": "群规",
                                           "source_event_ids": [evt]}]}}}), encoding="utf-8")
        memory.joinpath("world_knowledge.json").write_text(json.dumps({
            "topics": [{"key": "k1", "summary": "知识", "evidence_url": "https://example.com",
                        "ts": 100.0}]}), encoding="utf-8")
        memory.joinpath("private_memory.json").write_text(json.dumps(
            {"private:1001": "一段摘要"}), encoding="utf-8")
        inputs = dag.read_derivation_inputs()
        pairs = {(e["derived_id"], e["source_id"]) for e in inputs["edges"]}
        self.assertTrue(any(d.startswith("persona:facts:") and s == evt for d, s in pairs))
        self.assertIn(("group:55:g1", evt), pairs)
        self.assertIn(("knowledge:k1", "https://example.com"), pairs)
        objects = {o["object_id"]: o for o in inputs["objects"]}
        self.assertEqual("unknown", objects["summary:private:1001"]["support"])
        self.assertFalse(any(d.startswith("summary:") for d, _ in pairs),
                         "摘要没有来源字段，不许补造来源边")

    def test_registered_cache_edges_round_trip(self):
        evt = self.seed_event("m05:b3")
        sm.write_invalidation_transaction({
            "ts": 1.0, "expected_epoch": 0,
            "edges": [edge("cache:identity", evt, "identity_cache"),
                      edge("cache:retrieval", evt, "retrieval_cache")],
            "corrections": [], "propagation": None})
        inputs = dag.read_derivation_inputs()
        pairs = {(e["derived_id"], e["source_id"]) for e in inputs["edges"]}
        self.assertIn(("cache:identity", evt), pairs)
        self.assertIn(("cache:retrieval", evt), pairs)

    def test_paths_resolve_at_call_time(self):
        evt = self.seed_event("m05:b4")
        self.commit([edge("cache_x", evt, "retrieval_cache")],
                    [dag.correction("observation_invalid", evt, "误读", ts=1.0)])
        self.assertEqual(1, len(dag.read_invalidation_state()["edges"]))
        with tempfile.TemporaryDirectory(prefix="m05_other_") as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                self.assertEqual([], dag.read_derivation_inputs()["edges"])
                self.assertEqual(0, dag.read_invalidation_state()["epoch"])
        self.assertEqual(1, len(dag.read_invalidation_state()["edges"]),
                         "路径若在 import 时冻住，这里会读到别人的库")

    def test_reads_never_modify_the_ledger(self):
        evt = self.seed_event("m05:b5")
        self.seed_belief("bb5", evt)
        self.commit([edge("bb5", evt, "belief")],
                    [dag.correction("observation_invalid", evt, "误读", ts=1.0)])
        path = self.cognition_path()
        before = path.read_bytes()
        dag.read_invalidation_state()
        dag.read_derivation_inputs()
        dag.read_gate([{"object_id": "bb5", "projection_epoch": 0}],
                      dag.read_invalidation_state()["states"], current_epoch=1)
        self.assertEqual(before, path.read_bytes(), "GET/读路径写了账本")

    def test_end_to_end_retraction_survives_restart_and_blocks_old_projection(self):
        evt = self.seed_event("m05:e2e")
        bel = self.seed_belief("be2e", evt)
        edges = [edge(bel, evt, "belief")]
        projection = [{"object_id": bel, "projection_epoch": 0, "text": "喜欢茶"}]
        kept, _ = dag.read_gate(projection, dag.read_invalidation_state()["states"],
                                current_epoch=0)
        self.assertEqual(projection, kept)
        self.commit(edges, [dag.correction("observation_invalid", evt, "主人纠正", ts=1.0)])
        # 模拟重启：只从账本只读恢复，不靠内存状态。
        state = dag.read_invalidation_state()
        kept, dropped = dag.read_gate(projection, state["states"],
                                      current_epoch=state["epoch"])
        self.assertEqual([], kept, "撤回后旧投影复活了")
        self.assertTrue(dropped)
        self.assertTrue(sm.get_event(evt), "撤回删除了原始观察")
        self.assertEqual("invalidated", state["states"][evt]["status"])
        # 旁路边界：A08 旧撤回路径不由 M05 改线，旧表保持原样（接管是 M07/M08）。
        self.assertTrue(sm.evidence_valid("event", evt))


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "invalidation_dag.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_only_pure_or_readonly_modules_are_imported(self):
        allowed = {"hashlib", "json", "os", "sqlite3", "time", "pathlib"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             "失效契约 import 了写入/网络/模型 API：%s" % sorted(names - allowed))

    def test_no_write_sql_anywhere_in_the_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper,
                                     "失效契约里出现写入 SQL：%s" % node.value[:60])

    def test_no_write_or_file_calls(self):
        forbidden = {"commit", "executescript", "executemany", "rollback",
                     "mkdir", "makedirs", "remove", "unlink", "rmtree", "rename",
                     "write_text", "write_bytes",
                     "add_belief", "revise_belief", "retract_evidence",
                     "write_cognition_transaction", "write_invalidation_transaction"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "失效契约里出现写操作 %s" % node.func.attr)
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
        bridge = {"_data_root", "_persona_root", "_cognition_path", "_ro", "_read_json",
                  "read_invalidation_state", "read_derivation_inputs"}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in bridge:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "isfile"),
                                     "%s 里解析了路径；核心传播必须是纯函数" % node.name)

    def test_the_correction_and_status_sets_are_fixed(self):
        self.assertEqual(("observation_invalid", "interpretation_revised",
                          "belief_corrected", "scope_revoked"), dag.CORRECTION_KINDS)
        self.assertEqual(("active", "invalidated", "needs_reassessment",
                          "scope_narrowed", "superseded"), dag.OBJECT_STATUSES)


if __name__ == "__main__":
    unittest.main()