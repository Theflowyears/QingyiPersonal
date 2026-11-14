# -*- coding: utf-8 -*-
"""V06 长期消融与回退复查的回归锁（validation 轨，统一方案 §V 表 V06 行）。

「长期消融与回退复查：记忆选择、等待、履约、观点变化有证据，不能只看
『说得更像人』」。模块 bot/longterm_review.py 是纯编排（轴校验 + 委托 V03
引擎 + 覆盖审计）；真实执行在本文件的 runner 里：全部用内存纯函数契约，
真实调用 A03/G03/G02/M05/L01/L02 冻结前置（不打桩断言），不读不写真实
data/，不发网络。每个用例的 维度×证据类型×期望分型 显式登记在 CASE_PLAN，
覆盖审计钉死四个维度都同时有消融对照与回退复查两类真实用例。

契约可执行 ≠ 长期消融已做：judgement 恒 None、authorization 恒 manual_review，
「有用/没用/更像人」留给人判。发现契约缺陷只如实报告，不断言成正确行为。
"""
import ast
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import longterm_review as v06             # noqa: E402
import retraction_scope_matrix as v03     # noqa: E402
import memory_manifest as mm              # noqa: E402
import goal_draft_scheduler as gds        # noqa: E402
import goal_attention as ga               # noqa: E402
import action_proposal as ap              # noqa: E402
import self_revision as sv                # noqa: E402
import self_reflection as sr              # noqa: E402

NOW = 4000000000.0


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


# ── 合成夹具（内存里的测试数据，不是自然样本）─────────────────────


def memory_blocks():
    return [{"id": "mem_1", "text": "她喜欢冰可乐", "score": 0.9,
             "kind": "memory", "source": "persona"},
            {"id": "ctx_1", "text": "今天群里在聊动漫", "score": 0.5,
             "kind": "context", "source": "group"}]


def read_candidate():
    proposal = ap.build(
        "curiosity", "search", motive="求知", reason="想弄懂荒坂塔原型",
        event_ids=["evt_1"], topic="荒坂塔", visibility="internal",
        deadline=NOW + 86400.0,
        budget={"quota_key": "CURIOSITY_MAX_PER_DAY",
                "max_per_day": 3, "cooldown_seconds": 0},
        success="找到可追溯的资料", failure="查不到",
        abandon_when="连续 3 次查不到放弃", decision="act")
    return {"proposal": proposal, "recent_failures": 0, "attention_entry_id": ""}


def commitment(status="open"):
    return {"id": "cmt_1", "to_qq": "per_1001", "text": "帮查荒坂塔资料",
            "due_ts": NOW + 3600, "status": status, "event_ids": ["evt_1"]}


def source(event_id, text):
    return {"event_id": event_id, "kind": "private_message", "text": text,
            "source_revision": "rev_" + event_id, "occurred_at": NOW - 1100,
            "session": "private:1001", "visibility": "private",
            "actor_ref": "per_1001", "actor_qq": "1001",
            "topic_ref": "", "topic_qq": "", "synthetic": False, "retracted": False}


def revision_window():
    return [source("evt_1", "几次聊天我都先开了口"),
            source("evt_2", "那次也是我主动找的话题")]


def revision_claim():
    return {"object_kind": "claim", "object_id": "r1",
            "content": "有话题时我愿意主动找人",
            "occurred_at": NOW - 1000, "recorded_at": NOW - 999,
            "source_event_ids": ["evt_1", "evt_2"], "epistemic_type": "reported",
            "visibility": "private", "source_session": "private:1001",
            "subject_ref": "per_1001",
            "evidence": [{"event_id": "evt_1", "quote": "几次聊天我都先开了口"},
                         {"event_id": "evt_2", "quote": "那次也是我主动找的话题"}]}


def revision_proposal(reflection_id):
    return {"proposal_id": "p1", "reflection_id": reflection_id,
            "object_id": "r1", "target_kind": "belief", "target_id": "bel_x",
            "after": "有话题时我愿意主动找人",
            "reason": "几次真实互动里我都先开了口"}


def l01_ledger():
    led = sr.new_ledger()
    plan = sr.reflection_plan([revision_claim()], revision_window(), now=NOW)
    record = sr.commit_reflection(led, plan)
    return led, record["reflection_id"]


# ── 八个真实 runner：四维度 × 消融/回退，全部内存纯函数 ───────────


def run_memory_selection_ablation():
    blocks = memory_blocks()
    with_mem, _, _ = mm.compose(blocks, 400)
    without_mem, _, _ = mm.compose(blocks[1:], 400)
    ids_with = [row["id"] for row in with_mem]
    ids_without = [row["id"] for row in without_mem]
    return {"with_memory": "mem_1" in ids_with,
            "without_memory": "mem_1" not in ids_without,
            "selection_changed": ids_with != ids_without}


def run_memory_selection_rollback():
    blocks = memory_blocks()
    baseline, _, _ = mm.compose(blocks[1:], 400)
    with_mem, _, _ = mm.compose(blocks, 400)
    restored, _, _ = mm.compose(blocks[1:], 400)
    return {"with_block_differs": canonical(with_mem) != canonical(baseline),
            "restored_equals_baseline": canonical(restored) == canonical(baseline)}


def run_waiting_ablation():
    rows = [read_candidate()]
    exhausted = gds.schedule(now=NOW, candidates=rows,
                             usage={"CURIOSITY_MAX_PER_DAY": {"used": 3, "last_ts": 0}})
    available = gds.schedule(now=NOW, candidates=rows, usage={})
    return {"exhausted_selected": len(exhausted["selected"]),
            "available_selected": len(available["selected"]),
            "allow_wait": bool(exhausted["allow_wait"]),
            "wait_reason": exhausted["deferred"][0]["reason_codes"][0]}


def run_waiting_rollback():
    rows = [read_candidate()]
    before = gds.schedule(now=NOW, candidates=rows,
                          usage={"CURIOSITY_MAX_PER_DAY": {"used": 3, "last_ts": 0}})
    after = gds.schedule(now=NOW, candidates=rows, usage={})
    return {"before_selected": len(before["selected"]),
            "after_restore_selected": len(after["selected"]),
            "reason_observed": "quota_exhausted" in before["deferred"][0]["reason_codes"]}


def run_fulfillment_ablation():
    open_att = ga.build_attention(now=NOW, commitments=[commitment("open")],
                                  scope="private:1001")
    done_att = ga.build_attention(now=NOW, commitments=[commitment("done")],
                                  scope="private:1001")
    return {"open_entries": len(open_att["entries"]),
            "done_entries": len(done_att["entries"]),
            "excluded_reason": done_att["excluded"][0]["reason_code"]}


def run_fulfillment_rollback():
    rows = [commitment("open")]
    blocked = ga.build_attention(now=NOW, commitments=rows,
                                 states={"cmt_1": {"status": "invalidated"}},
                                 scope="private:1001")
    restored = ga.build_attention(now=NOW, commitments=rows, states={},
                                  scope="private:1001")
    return {"blocked_entries": len(blocked["entries"]),
            "restored_entries": len(restored["entries"]),
            "blocked_reason": blocked["excluded"][0]["reason_code"]}


def run_belief_revision_ablation():
    led, rid = l01_ledger()
    with_ref = sv.revision_plan([revision_proposal(rid)], led,
                                revision_window(), now=NOW)
    without_ref = sv.revision_plan([revision_proposal(rid)], sr.new_ledger(),
                                   revision_window(), now=NOW)
    return {"with_reflection": with_ref["decisions"][0]["status"],
            "without_reflection": without_ref["decisions"][0]["status"],
            "reason_code": without_ref["decisions"][0]["reason_code"]}


def run_belief_revision_rollback():
    led, rid = l01_ledger()
    plan = sv.revision_plan([revision_proposal(rid)], led,
                            revision_window(), now=NOW)
    store = sv.new_ledger()
    sv.commit_revision(store, plan)
    before = canonical(sv.list_records(store))
    duplicate_rejected = False
    try:
        sv.commit_revision(store, plan)
    except ValueError:
        duplicate_rejected = True
    after = canonical(sv.list_records(store))
    return {"records_before": len(sv.list_records(store)),
            "duplicate_rejected": duplicate_rejected,
            "history_untouched": before == after}


CASE_PLAN = (
    ("v06_mem_ablation", "memory_selection", "ablation",
     {"with_memory": True, "without_memory": True, "selection_changed": True},
     run_memory_selection_ablation),
    ("v06_mem_rollback", "memory_selection", "rollback_recheck",
     {"with_block_differs": True, "restored_equals_baseline": True},
     run_memory_selection_rollback),
    ("v06_wait_ablation", "waiting", "ablation",
     {"exhausted_selected": 0, "available_selected": 1,
      "allow_wait": True, "wait_reason": "quota_exhausted"},
     run_waiting_ablation),
    ("v06_wait_rollback", "waiting", "rollback_recheck",
     {"before_selected": 0, "after_restore_selected": 1, "reason_observed": True},
     run_waiting_rollback),
    ("v06_fulfill_ablation", "commitment_fulfillment", "ablation",
     {"open_entries": 1, "done_entries": 0,
      "excluded_reason": "commitment_not_open"},
     run_fulfillment_ablation),
    ("v06_fulfill_rollback", "commitment_fulfillment", "rollback_recheck",
     {"blocked_entries": 0, "restored_entries": 1,
      "blocked_reason": "invalidated"},
     run_fulfillment_rollback),
    ("v06_belief_ablation", "belief_revision", "ablation",
     {"with_reflection": "accepted", "without_reflection": "rejected",
      "reason_code": "reflection_not_found"},
     run_belief_revision_ablation),
    ("v06_belief_rollback", "belief_revision", "rollback_recheck",
     {"records_before": 1, "duplicate_rejected": True, "history_untouched": True},
     run_belief_revision_rollback),
)


def all_cases():
    return [v06.case(cid, dim, kind, expected, runner)
            for cid, dim, kind, expected, runner in CASE_PLAN]


# ── AST 静态锁：纯编排，只 import V03 引擎 ───────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "longterm_review.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def attr_pairs(self):
        return {(n.func.value.id, n.func.attr) for n in ast.walk(self.tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)}

    def imported(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        return names

    def test_import_surface_is_only_the_v03_kernel(self):
        self.assertEqual({"retraction_scope_matrix"}, self.imported())

    def test_it_delegates_execution_and_coverage_to_v03(self):
        pairs = self.attr_pairs()
        for wanted in (("v03", "run_matrix"), ("v03", "coverage")):
            self.assertIn(wanted, pairs)

    def test_no_banned_imports_or_side_effects(self):
        banned = {"os", "time", "random", "datetime", "sqlite3", "pathlib",
                  "socket", "urllib", "requests", "http", "qq_openclaw_bridge",
                  "self_model", "llm_router", "emotion_shadow", "napcat_channel",
                  "ops_remote", "paramiko"}
        self.assertEqual(set(), self.imported() & banned)
        for token in ("open(", "DATA_DIR", "time.time(", "random.",
                      "send_private_msg", "execute("):
            self.assertNotIn(token, self.source)

    def test_boundaries_are_constant(self):
        self.assertIn("judgement", self.source)
        text = " ".join(v06.LIMITATIONS)
        for token in ("not_observed", "manual_review", "不能只看", "零写入"):
            self.assertIn(token, text)


# ── 轴校验与用例登记 ─────────────────────────────────────────────


class AxisTests(unittest.TestCase):
    def test_axes_match_the_todo_row(self):
        self.assertEqual(v06.DIMENSIONS,
                         ("memory_selection", "waiting",
                          "commitment_fulfillment", "belief_revision"))
        self.assertEqual(v06.EVIDENCE_KINDS, ("ablation", "rollback_recheck"))

    def test_unknown_axis_is_rejected(self):
        with self.assertRaises(ValueError):
            v06.case("x", "说得更像人", "ablation", {"k": 1}, lambda: {})
        with self.assertRaises(ValueError):
            v06.case("x", "waiting", "decorative", {"k": 1}, lambda: {})

    def test_empty_expected_or_missing_runner_is_rejected(self):
        with self.assertRaises(ValueError):
            v06.case("x", "waiting", "ablation", {}, lambda: {})
        with self.assertRaises(ValueError):
            v06.case("x", "waiting", "ablation", {"k": 1}, None)
        with self.assertRaises(ValueError):
            v06.case("", "waiting", "ablation", {"k": 1}, lambda: {})

    def test_case_shape_delegates_to_v03(self):
        spec = v06.case("x", "waiting", "ablation", {"k": 1}, lambda: {"k": 1})
        self.assertEqual({"case_id", "layer", "fault", "expected", "runner"},
                         set(spec))
        self.assertEqual(spec["layer"], "waiting")
        self.assertEqual(spec["fault"], "ablation")


# ── 执行与覆盖：八格全命中，分型如实 ─────────────────────────────


class MatrixTests(unittest.TestCase):
    def test_full_matrix_passes_with_complete_coverage(self):
        report = v06.run_matrix(all_cases())
        self.assertEqual(report["status"], "pass", report)
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_required_intersections"], [])
        self.assertEqual(len(report["coverage"]["intersections"]), 8)
        self.assertIsNone(report["judgement"])
        self.assertEqual(report["authorization"], "manual_review")
        self.assertFalse(report["provenance"]["real_longterm_samples"])
        json.dumps(report, allow_nan=False)

    def test_missing_evidence_kind_is_listed_loudly(self):
        cases = [spec for spec in all_cases() if spec["fault"] != "rollback_recheck"]
        report = v06.run_matrix(cases)
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_required_evidence_kinds"],
                         ["rollback_recheck"])
        self.assertEqual(len(report["coverage"]["missing_required_intersections"]), 4)

    def test_missing_dimension_is_listed_loudly(self):
        cases = [spec for spec in all_cases() if spec["layer"] != "belief_revision"]
        report = v06.run_matrix(cases)
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_required_dimensions"],
                         ["belief_revision"])

    def test_empty_matrix_is_fake_green_and_rejected(self):
        with self.assertRaises(ValueError):
            v06.run_matrix([])

    def test_runner_exception_is_error_not_swallowed(self):
        def boom():
            raise OSError("隔离账本坏了")
        cases = all_cases() + [v06.case("v06_boom", "waiting", "ablation",
                                        {"k": 1}, boom)]
        report = v06.run_matrix(cases)
        self.assertEqual(report["status"], "error")
        row = next(c for c in report["cases"] if c["case_id"] == "v06_boom")
        self.assertEqual(row["status"], "error")

    def test_wrong_observation_is_fail_not_pass(self):
        cases = all_cases()[:-1] + [v06.case(
            "v06_belief_rollback", "belief_revision", "rollback_recheck",
            {"records_before": 99, "duplicate_rejected": True,
             "history_untouched": True}, run_belief_revision_rollback)]
        report = v06.run_matrix(cases)
        self.assertEqual(report["status"], "fail")

    def test_report_is_deterministic(self):
        a = v06.run_matrix(all_cases())
        b = v06.run_matrix(all_cases())
        self.assertEqual(canonical(a), canonical(b))

    def test_coverage_delegates_to_v03(self):
        cases = all_cases()
        self.assertEqual(v06.coverage(cases), v03.coverage(cases))


if __name__ == "__main__":
    unittest.main()