# -*- coding: utf-8 -*-
"""L04 跨时间选择连续性验收的回归锁（stage5/self，统一方案 §L 表 L04 行）。

「用跨时段的选择变化证明连续性：能解释什么改变了看法、未做什么、为何等待；
无效则收缩机制」。模块 bot/continuity_acceptance.py 是纯编排（轴校验 + 委托
V03 引擎 + 覆盖审计）；真实执行在本文件的 runner 里：全部用内存纯函数契约，
真实调用 L02/L03/G03 冻结前置（不打桩断言），不读不写真实 data/，不发网络。
每个用例的 维度×证据类型×期望分型 显式登记在 CASE_PLAN，覆盖审计钉死三个
维度都同时有跨时段对照与收缩复查两类真实用例。

契约可执行 ≠ 连续性已证明：judgement 恒 None、authorization 恒 manual_review，
「连续/不连续/更像人」留给人判。发现契约缺陷只如实报告，不断言成正确行为。
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

import continuity_acceptance as l04         # noqa: E402
import retraction_scope_matrix as v03       # noqa: E402
import self_revision as sv                  # noqa: E402
import self_reflection as sr                # noqa: E402
import habit_relationship as hr             # noqa: E402
import goal_draft_scheduler as gds          # noqa: E402
import action_proposal as ap                # noqa: E402

NOW = 4000000000.0
DAY = 24 * 3600
BASE = NOW - 20 * DAY
AFTER = "有话题时我愿意主动找人"
REASON = "几次真实互动里我都先开了口"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


# ── 合成夹具（内存里的测试数据，不是自然样本）─────────────────────


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
            "content": AFTER, "occurred_at": NOW - 1000, "recorded_at": NOW - 999,
            "source_event_ids": ["evt_1", "evt_2"], "epistemic_type": "reported",
            "visibility": "private", "source_session": "private:1001",
            "subject_ref": "per_1001",
            "evidence": [{"event_id": "evt_1", "quote": "几次聊天我都先开了口"},
                         {"event_id": "evt_2", "quote": "那次也是我主动找的话题"}]}


def revision_proposal(reflection_id):
    return {"proposal_id": "p1", "reflection_id": reflection_id,
            "object_id": "r1", "target_kind": "belief", "target_id": "bel_x",
            "after": AFTER, "reason": REASON}


def l01_ledger():
    led = sr.new_ledger()
    plan = sr.reflection_plan([revision_claim()], revision_window(), now=NOW)
    record = sr.commit_reflection(led, plan)
    return led, record["reflection_id"]


def accepted_revision_store():
    led, rid = l01_ledger()
    plan = sv.revision_plan([revision_proposal(rid)], led, revision_window(), now=NOW)
    store = sv.new_ledger()
    sv.commit_revision(store, plan)
    return store


def outcome(oid, root, ts, result="success"):
    return {"outcome_id": oid, "root_id": root, "ts": ts,
            "kind": "action_outcome", "result": result, "retracted": False}


def good_outcomes():
    return [outcome("o1", "root_a", BASE),
            outcome("o2", "root_b", BASE + 4 * DAY),
            outcome("o3", "root_c", BASE + 8 * DAY)]


def habit_prop(pid="p1", **over):
    row = {"proposal_id": pid, "target_kind": "habit", "target_id": "hab_x",
           "after": "先开口再等回应", "reason": REASON,
           "outcome_ids": ["o1", "o2", "o3"], "counter_evidence": []}
    row.update(over)
    return row


def identity():
    return {"statements": ["我愿意主动"], "conflict_targets": [], "conflict_after": []}


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


# ── 六个真实 runner：三维度 × 跨时段/收缩，全部内存纯函数 ─────────


def run_view_change_cross_time():
    store = accepted_revision_store()
    got = sv.what_changed(store, "belief", "bel_x")
    trail = sv.version_trail(store, "belief", "bel_x")
    return {"explanation_known": got["known"], "versions": len(trail),
            "before_after_differ": trail[0]["before"] != trail[0]["after"]}


def run_view_change_shrink():
    store = accepted_revision_store()
    before = canonical(sv.list_records(store))
    cands = sv.reassessment_candidates(store, {"evt_1": {"status": "invalidated"}})
    after = canonical(sv.list_records(store))
    return {"reassess_candidate": len(cands) > 0, "history_preserved": before == after,
            "action": cands[0]["action"] if cands else ""}


def run_undone_cross_time():
    plan = hr.update_plan([habit_prop(counter_evidence=None)], good_outcomes(),
                          identity=identity(), now=NOW)
    store = hr.new_ledger()
    hr.commit_update(store, plan)
    reasons = hr.why_not_done(store, "habit", "hab_x")
    return {"undone_recorded": len(reasons) > 0,
            "reason_known": reasons[0]["reason_code"] != "",
            "status": reasons[0]["status"]}


def run_undone_shrink():
    plan = hr.update_plan([habit_prop()], good_outcomes(), identity=identity(), now=NOW)
    store = hr.new_ledger()
    hr.commit_update(store, plan)
    before = canonical(hr.list_records(store))
    cands = hr.reassessment_candidates(store, {"o1": {"status": "invalidated"}})
    after = canonical(hr.list_records(store))
    return {"reassess_candidate": len(cands) > 0, "history_preserved": before == after,
            "action": cands[0]["action"] if cands else ""}


def run_waiting_cross_time():
    rows = [read_candidate()]
    waited = gds.schedule(now=NOW, candidates=rows,
                          usage={"CURIOSITY_MAX_PER_DAY": {"used": 3, "last_ts": 0}})
    acted = gds.schedule(now=NOW + 3600, candidates=rows, usage={})
    return {"waited_selected": len(waited["selected"]),
            "acted_selected": len(acted["selected"]),
            "wait_reason": waited["deferred"][0]["reason_codes"][0]}


def run_waiting_shrink():
    empty = gds.schedule(now=NOW, candidates=[], usage={})
    exhausted = gds.schedule(now=NOW, candidates=[read_candidate()],
                               usage={"CURIOSITY_MAX_PER_DAY": {"used": 3, "last_ts": 0}})
    return {"allow_wait": bool(empty["allow_wait"]),
            "empty_selected": len(empty["selected"]),
            "no_force_when_exhausted": len(exhausted["selected"]) == 0}


CASE_PLAN = (
    ("l04_view_cross", "view_change", "cross_time",
     {"explanation_known": True, "versions": 1, "before_after_differ": True},
     run_view_change_cross_time),
    ("l04_view_shrink", "view_change", "shrink_recheck",
     {"reassess_candidate": True, "history_preserved": True, "action": "reassess"},
     run_view_change_shrink),
    ("l04_undone_cross", "undone", "cross_time",
     {"undone_recorded": True, "reason_known": True, "status": "held"},
     run_undone_cross_time),
    ("l04_undone_shrink", "undone", "shrink_recheck",
     {"reassess_candidate": True, "history_preserved": True, "action": "reassess"},
     run_undone_shrink),
    ("l04_wait_cross", "waiting", "cross_time",
     {"waited_selected": 0, "acted_selected": 1, "wait_reason": "quota_exhausted"},
     run_waiting_cross_time),
    ("l04_wait_shrink", "waiting", "shrink_recheck",
     {"allow_wait": True, "empty_selected": 0, "no_force_when_exhausted": True},
     run_waiting_shrink),
)


def all_cases():
    return [l04.case(cid, dim, kind, expected, runner)
            for cid, dim, kind, expected, runner in CASE_PLAN]


# ── AST 静态锁：纯编排，只 import V03 引擎 ───────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "continuity_acceptance.py"), encoding="utf-8") as handle:
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
        text = " ".join(l04.LIMITATIONS)
        for token in ("not_observed", "manual_review", "不能只看", "零写入"):
            self.assertIn(token, text)


# ── 轴校验与用例登记 ─────────────────────────────────────────────


class AxisTests(unittest.TestCase):
    def test_axes_match_the_todo_row(self):
        self.assertEqual(l04.DIMENSIONS, ("view_change", "undone", "waiting"))
        self.assertEqual(l04.EVIDENCE_KINDS, ("cross_time", "shrink_recheck"))

    def test_unknown_axis_is_rejected(self):
        with self.assertRaises(ValueError):
            l04.case("x", "说得更像人", "cross_time", {"k": 1}, lambda: {})
        with self.assertRaises(ValueError):
            l04.case("x", "waiting", "decorative", {"k": 1}, lambda: {})

    def test_empty_expected_or_missing_runner_is_rejected(self):
        with self.assertRaises(ValueError):
            l04.case("x", "waiting", "cross_time", {}, lambda: {})
        with self.assertRaises(ValueError):
            l04.case("x", "waiting", "cross_time", {"k": 1}, None)
        with self.assertRaises(ValueError):
            l04.case("", "waiting", "cross_time", {"k": 1}, lambda: {})

    def test_case_shape_delegates_to_v03(self):
        spec = l04.case("x", "waiting", "cross_time", {"k": 1}, lambda: {"k": 1})
        self.assertEqual({"case_id", "layer", "fault", "expected", "runner"}, set(spec))
        self.assertEqual(spec["layer"], "waiting")
        self.assertEqual(spec["fault"], "cross_time")


# ── 执行与覆盖：六格全命中，分型如实 ─────────────────────────────


class MatrixTests(unittest.TestCase):
    def test_full_matrix_passes_with_complete_coverage(self):
        report = l04.run_matrix(all_cases())
        self.assertEqual(report["status"], "pass", report)
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_required_intersections"], [])
        self.assertEqual(len(report["coverage"]["intersections"]), 6)
        self.assertIsNone(report["judgement"])
        self.assertEqual(report["authorization"], "manual_review")
        self.assertFalse(report["provenance"]["real_cross_time_samples"])
        json.dumps(report, allow_nan=False)

    def test_missing_evidence_kind_is_listed_loudly(self):
        cases = [spec for spec in all_cases() if spec["fault"] != "shrink_recheck"]
        report = l04.run_matrix(cases)
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_required_evidence_kinds"],
                         ["shrink_recheck"])
        self.assertEqual(len(report["coverage"]["missing_required_intersections"]), 3)

    def test_missing_dimension_is_listed_loudly(self):
        cases = [spec for spec in all_cases() if spec["layer"] != "waiting"]
        report = l04.run_matrix(cases)
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_required_dimensions"], ["waiting"])

    def test_empty_matrix_is_fake_green_and_rejected(self):
        with self.assertRaises(ValueError):
            l04.run_matrix([])

    def test_runner_exception_is_error_not_swallowed(self):
        def boom():
            raise OSError("隔离账本坏了")
        cases = all_cases() + [l04.case("l04_boom", "waiting", "cross_time",
                                        {"k": 1}, boom)]
        report = l04.run_matrix(cases)
        self.assertEqual(report["status"], "error")
        row = next(c for c in report["cases"] if c["case_id"] == "l04_boom")
        self.assertEqual(row["status"], "error")

    def test_wrong_observation_is_fail_not_pass(self):
        cases = all_cases()[:-1] + [l04.case(
            "l04_wait_shrink", "waiting", "shrink_recheck",
            {"allow_wait": False, "empty_selected": 0, "no_force_when_exhausted": True},
            run_waiting_shrink)]
        report = l04.run_matrix(cases)
        self.assertEqual(report["status"], "fail")

    def test_report_is_deterministic(self):
        a = l04.run_matrix(all_cases())
        b = l04.run_matrix(all_cases())
        self.assertEqual(canonical(a), canonical(b))

    def test_coverage_delegates_to_v03(self):
        cases = all_cases()
        self.assertEqual(l04.coverage(cases), v03.coverage(cases))


if __name__ == "__main__":
    unittest.main()