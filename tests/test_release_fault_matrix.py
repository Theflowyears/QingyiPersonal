# -*- coding: utf-8 -*-
"""V05 发布前故障注入与回退矩阵的回归锁（validation 轨，3a 预验收证据）。

统一方案 §V 表 V05 行：「3a发布前隔离压测和故障注入：重启/换模型/超时/库不可用/
多段回执/拒绝/撤回、表达接线与回退；无外发预验收后由C06单独授权真实灰度」。
§15.3：持久状态重启/换模型保持，cursor 身份变更或规则漂移明确失败不静默重置；
失效源在旧缓存下也不能重新作为当前支持；被拒绝的发送和 unknown 结果不造成重复
外发；降级不伪造「她不想说」；回退恢复读取路径，不删除新账本。

模块 bot/release_fault_matrix.py 是纯编排（轴校验 + 委托 V03 引擎 + 覆盖审计）；
真实执行在本文件的 runner 里：全部用内存纯函数契约或 tempfile 隔离账本，真实
调用 C04/C05/G02/G01/M05/S06 冻结前置（不打桩断言），不读不写真实 data/，
不发网络。每个用例的 层×故障×期望分型 显式登记在 CASE_PLAN，覆盖审计钉死
每个声明的故障/层都真测到。发现契约缺陷只如实报告，不断言成正确行为。
"""
import ast
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import release_fault_matrix as rfm        # noqa: E402
import retraction_scope_matrix as v03     # noqa: E402
import work_state as ws                   # noqa: E402
import expression_plan as xp              # noqa: E402
import goal_attention as ga               # noqa: E402
import action_proposal as ap              # noqa: E402
import invalidation_dag as dag            # noqa: E402
import emotion_dynamics as ed             # noqa: E402
import ripple_conflicts as rc             # noqa: E402
import ripple_evidence as re6             # noqa: E402

NOW = 4000000000.0


# ── 合成夹具（内存里的测试数据，不是自然样本）─────────────────────


def emotion(at=NOW, **over):
    state = ed.initial_state(at)
    state.update(over)
    return state


def commitment(oid="cmt_1", **over):
    row = {"id": oid, "to_qq": "per_1001", "text": "帮查荒坂塔资料",
           "due_ts": NOW + 3600, "status": "open", "event_ids": ["evt_1"]}
    row.update(over)
    return row


def goal(oid="gol_1", **over):
    row = {"id": oid, "text": "弄清荒坂塔原型", "motive": "求知",
           "owner_qq": "per_1001", "status": "active"}
    row.update(over)
    return row


def identity(**over):
    row = {"text": "她是清漪，现代人语域，自称我。",
           "object_revision": "rev_1", "source_revision": "src_1"}
    row.update(over)
    return row


def snap(**kw):
    base = dict(now=NOW, emotion=emotion(), commitments=[commitment()],
                goals=[goal()],
                pending=[{"pending_id": "pend_1", "tool": "bili_search",
                          "requested_at": NOW - 120}],
                relations=[{"subject_ref": "per_1001", "affection": 5}],
                budget={"limit": 5, "used": 2},
                events=[{"id": "evt_1", "kind": "group_message", "ts": NOW - 60}],
                scope="private:1001")
    base.update(kw)
    return ws.build_snapshot(**base)


def claim_item(oid, value, sources=()):
    return {"object_id": oid, "object_kind": "belief", "store": "cognition",
            "root_source_ids": list(sources), "validity": "valid",
            "subject_ref": "per_1", "restatement": value, "occurred_at": 1000.0,
            "evidence_spans": [{"event_id": s, "quote": value, "subject_ref": "per_1"}
                               for s in sources]}


def edge(a, b):
    return {"edge_id": "edge_%s_%s" % (a, b), "type": "shared_entity",
            "a": a, "a_kind": "belief", "b": b, "b_kind": "belief",
            "basis": {"reason": "shared_entity"}}


def evidence_two_scenes():
    s = claim_item("s", "喜欢茶", ["e1"])
    n = claim_item("n", "喜欢咖啡", ["e2"])
    resolved = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
    return re6.render_evidence(resolved, items=[s, n], seeds=[s])


def render(snapshot=None, **kw):
    base = dict(identity=identity(), evidence=evidence_two_scenes())
    base.update(kw)
    return ws.render_injection(snapshot if snapshot is not None else snap(), **base)


def continuity(status="pass", kernels=("shadow-v2",), conditions=("late",)):
    """S06 run_matrix 报告骨架（形状锁只认这个；轴外内核/条件一律非法）。"""
    return {"status": status, "cases": [], "expression_control": False,
            "counts_for_live": False, "trajectory": "historical_replay",
            "provenance": {"kernels": list(kernels), "conditions": list(conditions)}}


def proposal(**over):
    """一份字段齐全、绑定正确的私聊提案（阿甲=1001，主人=9999）。"""
    row = ap.build(
        "proactive_dm", "private_dm", motive="联结", reason="她想找朋友聊聊",
        event_ids=["evt_1"], goal_id="", topic="八抽出货", topic_qq="1001",
        participants=["1001"], recipient_qq="1001", visibility="private",
        scope={"origin": "friend_dm"}, evidence=["evt_1"],
        deadline=NOW + 3600,
        budget={"quota_key": "PROACTIVE_DM_MAX_PER_DAY",
                "max_per_day": 4, "cooldown_seconds": 5400},
        success="私聊真的发出去", failure="生成/发送失败",
        abandon_when="她自愿不说", decision="act")
    row.update(over)
    return row


# ── 真实 runner：每个故障×层都真实执行冻结前置契约 ─────────────────


def _st_restart():
    """重启：同输入重建快照，内容寻址 ID/版本逐字保持（持久状态不漂移）。"""
    a, b = snap(), snap()
    return {"stable_snapshot": a["snapshot_id"] == b["snapshot_id"],
            "stable_version": a["state_version"] == b["state_version"]}


def _st_model_drift():
    """换模型/规则漂移：M05 五分量 key 任一分量漂移都变 key；身份层缓存不静默命中。"""
    base = dag.cache_key("rev_1", "src_1", "0", "private:1001", "c04-1")
    drifts = [dag.cache_key("rev_2", "src_1", "0", "private:1001", "c04-1"),
              dag.cache_key("rev_1", "src_2", "0", "private:1001", "c04-1"),
              dag.cache_key("rev_1", "src_1", "1", "private:1001", "c04-1"),
              dag.cache_key("rev_1", "src_1", "0", "private:2002", "c04-1"),
              dag.cache_key("rev_1", "src_1", "0", "private:1001", "c04-2")]
    changes = all(k["ok"] and k["key"] != base["key"] for k in drifts)
    s = snap()
    r1 = ws.render_injection(s, identity=identity(), evidence=evidence_two_scenes())
    r2 = ws.render_injection(s, identity=identity(object_revision="rev_2"),
                             evidence=evidence_two_scenes(), cache=r1["cache"])
    return {"drift_changes_key": changes, "silent_hit": r2["identity"]["cache_hit"]}


def _st_db_unreadable():
    """库不可用：states 不可读 → G02 大声登记 unreadable 降级，不冒充正常。"""
    report = ga.build_attention(now=NOW, commitments=[commitment()],
                                states="corrupt-not-a-dict")
    reasons = [d.get("reason") for d in report["degradation"]]
    return {"status": report["status"], "unreadable_registered": "unreadable" in reasons}


def _sn_timeout():
    """超时：内核情绪结果缺席 → 快照如实 absent/degraded，不补造情绪。"""
    s = snap(emotion=None)
    return {"status": s["status"], "emotion_absent": s["emotion"] is None,
            "absent_registered": {"store": "emotion", "reason": "absent"} in s["degradation"]}


def _in_restart():
    """重启：身份层缓存命中；换新缓存（重启）重建内容逐字一致。"""
    s = snap()
    ev = evidence_two_scenes()
    r1 = ws.render_injection(s, identity=identity(), evidence=ev)
    r2 = ws.render_injection(s, identity=identity(), evidence=ev, cache=r1["cache"])
    r3 = ws.render_injection(s, identity=identity(), evidence=ev)
    return {"cache_hit": r2["identity"]["cache_hit"],
            "restart_same_content": r3["identity"]["text"] == r1["identity"]["text"]}


def _in_retraction():
    """撤回：失效源在注入时刻被 M05 读门挡下，大声登记，不进证据层。"""
    r = ws.render_injection(snap(), identity=identity(), evidence=evidence_two_scenes(),
                            states={"s": {"status": "invalidated"}})
    present = [e for scene in r["evidence"]["scenes"] for e in scene["entries"]
               if e.get("object_id") == "s"]
    codes = [x["reason_code"] for x in r["excluded"]]
    return {"invalidated_present": bool(present), "excluded_loud": "invalidated" in codes}


def _in_timeout():
    """超时：身份层输入缺席 → 如实 absent/degraded，不伪造身份文本。"""
    r = ws.render_injection(snap(), identity=None, evidence=evidence_two_scenes())
    return {"status": r["status"], "identity_absent": r["identity"]["text"] == "",
            "absent_registered": {"store": "identity", "reason": "absent"} in r["degradation"]}


def _xp_model_switch():
    """换模型：continuity 未覆盖 tone 依赖内核 → tone 不可用且 degraded，不静默放行。"""
    plan = xp.build_plan(snap(), render(), continuity=continuity(kernels=["shadow-v1"]))
    return {"tone_available": plan["tone"]["available"], "reason": plan["tone"]["reason"],
            "status": plan["status"]}


def _xp_retraction():
    """撤回/篡改：snapshot 与 injection 绑定不一致大声拒绝；一致时计划内容寻址稳定。"""
    s1 = snap()
    s2 = snap(commitments=[commitment("cmt_2", text="另一件事")])
    raised = False
    try:
        xp.build_plan(s1, render(s2), continuity=continuity())
    except ValueError:
        raised = True
    p1 = xp.build_plan(s1, render(s1), continuity=continuity())
    p2 = xp.build_plan(s1, render(s1), continuity=continuity())
    return {"mismatch_raised": raised, "match_stable": p1["plan_id"] == p2["plan_id"]}


def _xp_timeout():
    """超时：continuity 矩阵未通过 → tone 不可用（continuity_failed），不补造语气。"""
    plan = xp.build_plan(snap(), render(), continuity=continuity(status="fail"))
    return {"tone_available": plan["tone"]["available"], "reason": plan["tone"]["reason"]}


def _dl_multi_receipt():
    """多段回执：同一提案重复 observe 裁决逐字一致、单槽投影不累积、出声不沉默。"""
    ap._LAST = None
    buf = io.StringIO()
    p = proposal()
    with contextlib.redirect_stdout(buf):
        r1 = ap.observe(p, friends=("1001", "9999"), now=NOW)
        r2 = ap.observe(p, friends=("1001", "9999"), now=NOW)
    last = ap.last()
    return {"same_verdict": (r1["decision"], r1["reasons"]) == (r2["decision"], r2["reasons"]),
            "single_projection": last is r2,
            "logged": "[行动提案]" in buf.getvalue()}


def _dl_rejected():
    """拒绝：缺字段提案 rejected + 稳定 reason_code；收件人绝不被改写成主人。"""
    p = ap.build("w", "private_dm")
    before = p.get("recipient_qq")
    r = ap.validate(p, friends=("1001", "9999"), now=NOW)
    return {"decision": r["decision"], "has_reasons": bool(r["reasons"]),
            "retargeted": p.get("recipient_qq") != before}


def _dl_unknown():
    """unknown 结果：wait 是合法裁决，不冒充 act、不触发外发。"""
    p = proposal(decision="wait", gate_reasons=["budget_closed"])
    r = ap.validate(p, friends=("1001", "9999"), now=NOW)
    return {"decision": r["decision"], "acted": r["decision"] == "act"}


def _rb_retraction():
    """回退：切回旧读取路径仍可产出注入；expression_control 恒 False；账本字节不变；
    检索灰度与情感接管是两个独立开关（计划里不存在检索开关键）。"""
    with tempfile.TemporaryDirectory(prefix="v05_rb_") as tmp:
        ledger = Path(tmp) / "ledger.json"
        ledger.write_text('{"rows": [1, 2, 3]}', encoding="utf-8")
        before = ledger.read_bytes()
        s = snap()
        inj = render(s)
        plan_on = xp.build_plan(s, inj, continuity=continuity())
        plan_off = xp.build_plan(s, inj, continuity=continuity(status="fail"))
        old = ws.render_injection(s, identity=identity(), evidence=evidence_two_scenes())
        after = ledger.read_bytes()
    return {"old_path_readable": old["snapshot_id"] == s["snapshot_id"]
            and old["identity"]["text"] != "",
            "expression_control": plan_on["expression_control"] or plan_off["expression_control"],
            "ledger_unchanged": before == after,
            "switches_independent": "retrieval_gray" not in plan_on and "retrieval" not in plan_on}


# 声明的交叉点：每个 (层, 故障) 都必须有真实用例命中（CoverageTests 钉死）。
CASE_PLAN = (
    ("st_restart_snapshot_stable", "state", "restart",
     {"stable_snapshot": True, "stable_version": True}, _st_restart),
    ("st_model_drift_loud", "state", "model_switch",
     {"drift_changes_key": True, "silent_hit": False}, _st_model_drift),
    ("st_db_unreadable_degraded", "state", "db_unavailable",
     {"status": "degraded", "unreadable_registered": True}, _st_db_unreadable),
    ("sn_timeout_emotion_absent", "snapshot", "timeout",
     {"status": "degraded", "emotion_absent": True, "absent_registered": True}, _sn_timeout),
    ("in_restart_identity_cache", "injection", "restart",
     {"cache_hit": True, "restart_same_content": True}, _in_restart),
    ("in_retract_gates_evidence", "injection", "retraction",
     {"invalidated_present": False, "excluded_loud": True}, _in_retraction),
    ("in_timeout_identity_absent", "injection", "timeout",
     {"status": "degraded", "identity_absent": True, "absent_registered": True}, _in_timeout),
    ("xp_kernel_uncovered", "expression_plan", "model_switch",
     {"tone_available": False, "reason": "continuity_kernel_uncovered",
      "status": "degraded"}, _xp_model_switch),
    ("xp_binding_mismatch_loud", "expression_plan", "retraction",
     {"mismatch_raised": True, "match_stable": True}, _xp_retraction),
    ("xp_continuity_failed", "expression_plan", "timeout",
     {"tone_available": False, "reason": "continuity_failed"}, _xp_timeout),
    ("dl_multi_observe_idempotent", "delivery", "multi_receipt",
     {"same_verdict": True, "single_projection": True, "logged": True}, _dl_multi_receipt),
    ("dl_rejected_no_retarget", "delivery", "rejected_send",
     {"decision": "rejected", "has_reasons": True, "retargeted": False}, _dl_rejected),
    ("dl_wait_is_legal", "delivery", "unknown_result",
     {"decision": "wait", "acted": False}, _dl_unknown),
    ("rb_old_path_survives", "rollback", "retraction",
     {"old_path_readable": True, "expression_control": False,
      "ledger_unchanged": True, "switches_independent": True}, _rb_retraction),
)


def build_cases(plan=CASE_PLAN):
    return [rfm.case(*spec) for spec in plan]


# ── 用例登记：轴校验与形状 ─────────────────────────────────────────


class CaseRegistrationTests(unittest.TestCase):
    def test_unknown_layer_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            rfm.case("x", "not_a_layer", "restart", {"k": 1}, lambda: {})

    def test_unknown_fault_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            rfm.case("x", "state", "not_a_fault", {"k": 1}, lambda: {})

    def test_empty_case_id_is_rejected(self):
        with self.assertRaises(ValueError):
            rfm.case("  ", "state", "restart", {"k": 1}, lambda: {})

    def test_empty_expected_is_rejected(self):
        with self.assertRaises(ValueError):
            rfm.case("x", "state", "restart", {}, lambda: {})

    def test_non_dict_expected_is_rejected(self):
        with self.assertRaises(ValueError):
            rfm.case("x", "state", "restart", [("k", 1)], lambda: {})

    def test_non_callable_runner_is_rejected(self):
        with self.assertRaises(ValueError):
            rfm.case("x", "state", "restart", {"k": 1}, None)

    def test_registration_does_not_call_the_runner(self):
        def boom():
            raise AssertionError("runner must not run at registration")
        spec = rfm.case("x", "state", "restart", {"k": 1}, boom)
        self.assertEqual(spec["case_id"], "x")

    def test_expected_is_copied_not_aliased(self):
        expected = {"k": 1}
        spec = rfm.case("x", "state", "restart", expected, lambda: {})
        spec["expected"]["k"] = 2
        self.assertEqual(expected["k"], 1)

    def test_case_shape_matches_v03(self):
        spec = rfm.case("x", "state", "restart", {"k": 1}, lambda: {})
        self.assertEqual(set(spec), {"case_id", "layer", "fault", "expected", "runner"})


# ── 执行与分型：逐字继承 V03 口径 ──────────────────────────────────


class MatrixExecutionTests(unittest.TestCase):
    def test_empty_matrix_is_rejected(self):
        with self.assertRaises(ValueError):
            rfm.run_matrix([])

    def test_duplicate_case_id_is_rejected(self):
        cases = [rfm.case("x", "state", "restart", {"k": 1}, lambda: {"k": 1}),
                 rfm.case("x", "snapshot", "timeout", {"k": 1}, lambda: {"k": 1})]
        with self.assertRaises(ValueError):
            rfm.run_matrix(cases)

    def test_runner_exception_is_error_not_swallowed(self):
        def boom():
            raise RuntimeError("isolated fixture blew up")
        report = rfm.run_matrix([rfm.case("x", "state", "restart", {"k": 1}, boom)])
        self.assertEqual(report["status"], "error")
        row = report["cases"][0]
        self.assertEqual(row["status"], "error")
        self.assertIn("RuntimeError", row["error"])
        self.assertIn("isolated fixture blew up", row["error"])

    def test_non_dict_observation_is_error(self):
        report = rfm.run_matrix([rfm.case("x", "state", "restart", {"k": 1}, lambda: None)])
        self.assertEqual(report["cases"][0]["status"], "error")

    def test_missing_observation_key_is_fail_not_default_true(self):
        report = rfm.run_matrix([rfm.case("x", "state", "restart", {"k": 1}, lambda: {})])
        row = report["cases"][0]
        self.assertEqual(row["status"], "fail")
        self.assertEqual(row["mismatches"],
                         [{"key": "k", "expected": 1, "observed": None, "present": False}])

    def test_mismatch_is_fail_with_both_values(self):
        report = rfm.run_matrix([rfm.case("x", "state", "restart", {"k": 1}, lambda: {"k": 2})])
        row = report["cases"][0]
        self.assertEqual(row["status"], "fail")
        self.assertEqual(row["mismatches"][0]["expected"], 1)
        self.assertEqual(row["mismatches"][0]["observed"], 2)

    def test_all_hit_is_pass(self):
        report = rfm.run_matrix([rfm.case("x", "state", "restart", {"k": 1}, lambda: {"k": 1})])
        self.assertEqual(report["status"], "pass")

    def test_one_error_does_not_stop_other_cases(self):
        def boom():
            raise RuntimeError("x")
        cases = [rfm.case("a", "state", "restart", {"k": 1}, boom),
                 rfm.case("b", "snapshot", "timeout", {"k": 1}, lambda: {"k": 1})]
        report = rfm.run_matrix(cases)
        self.assertEqual(report["status"], "error")
        self.assertEqual([r["status"] for r in report["cases"]], ["error", "pass"])

    def test_cases_are_sorted_by_case_id(self):
        cases = [rfm.case("z", "state", "restart", {"k": 1}, lambda: {"k": 1}),
                 rfm.case("a", "snapshot", "timeout", {"k": 1}, lambda: {"k": 1})]
        report = rfm.run_matrix(cases)
        self.assertEqual([r["case_id"] for r in report["cases"]], ["a", "z"])

    def test_same_input_same_report(self):
        r1 = rfm.run_matrix(build_cases())
        r2 = rfm.run_matrix(build_cases())
        self.assertEqual(r1, r2)

    def test_input_order_does_not_matter(self):
        r1 = rfm.run_matrix(build_cases())
        r2 = rfm.run_matrix(build_cases(tuple(reversed(CASE_PLAN))))
        self.assertEqual(r1, r2)

    def test_input_cases_are_not_mutated(self):
        cases = build_cases()
        before = copy.deepcopy([c["expected"] for c in cases])
        rfm.run_matrix(cases)
        self.assertEqual([c["expected"] for c in cases], before)

    def test_report_is_json_serializable(self):
        json.dumps(rfm.run_matrix(build_cases()), ensure_ascii=False, allow_nan=False)


# ── 覆盖审计：缺的大声列出，不假装覆盖完整 ─────────────────────────


class CoverageAuditTests(unittest.TestCase):
    def test_coverage_delegates_to_v03(self):
        cases = build_cases()
        self.assertEqual(rfm.coverage(cases), v03.coverage(cases))

    def test_full_plan_covers_every_required_fault_and_layer(self):
        report = rfm.run_matrix(build_cases())
        cov = report["coverage"]
        self.assertTrue(cov["complete"], cov)
        self.assertEqual(cov["missing_required_faults"], [])
        self.assertEqual(cov["missing_required_layers"], [])

    def test_partial_coverage_is_listed_loudly(self):
        report = rfm.run_matrix(build_cases(CASE_PLAN[:1]))
        cov = report["coverage"]
        self.assertFalse(cov["complete"])
        self.assertIn("model_switch", cov["missing_required_faults"])
        self.assertIn("rollback", cov["missing_required_layers"])

    def test_incomplete_coverage_does_not_fake_a_fail_or_pass(self):
        report = rfm.run_matrix(build_cases(CASE_PLAN[:1]))
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["coverage"]["complete"])

    def test_every_declared_intersection_is_really_hit(self):
        report = rfm.run_matrix(build_cases())
        hit = {(r["layer"], r["fault"]) for r in report["cases"]}
        for spec in CASE_PLAN:
            self.assertIn((spec[1], spec[2]), hit, spec[0])
        self.assertEqual(report["coverage"]["cases"], len(CASE_PLAN))


# ── 恒定边界：预验收证据永不授权 ───────────────────────────────────


class ConstantBoundaryTests(unittest.TestCase):
    def test_constants_hold_even_when_all_pass(self):
        report = rfm.run_matrix(build_cases())
        self.assertEqual(report["status"], "pass")
        self.assertIs(report["expression_control"], False)
        self.assertIs(report["outbound"], False)
        self.assertEqual(report["authorization"], "manual_review")
        self.assertIsNone(report["judgement"])

    def test_constants_hold_on_error_too(self):
        def boom():
            raise RuntimeError("x")
        report = rfm.run_matrix([rfm.case("x", "state", "restart", {"k": 1}, boom)])
        self.assertIs(report["expression_control"], False)
        self.assertIs(report["outbound"], False)
        self.assertEqual(report["authorization"], "manual_review")

    def test_provenance_binds_the_v03_kernel(self):
        report = rfm.run_matrix(build_cases())
        prov = report["provenance"]
        self.assertEqual(prov["algorithm_version"], rfm.ALGORITHM_VERSION)
        self.assertEqual(prov["kernel"], v03.ALGORITHM_VERSION)
        self.assertIs(prov["network"], False)
        self.assertIs(prov["production_ledger"], False)
        self.assertIs(prov["llm_reinterpretation"], False)
        self.assertEqual(prov["required_faults"], list(rfm.FAULTS))
        self.assertEqual(prov["required_layers"], list(rfm.LAYERS))

    def test_limitations_declare_what_is_not_done(self):
        self.assertIsInstance(rfm.LIMITATIONS, tuple)
        limits = " ".join(rfm.LIMITATIONS)
        for token in ("C06", "manual_review", "action_idempotency_outbox",
                      "p50/p95/p99", "不冒充"):
            self.assertIn(token, limits)


# ── 全矩阵真实执行：每个故障×层都真测到 ────────────────────────────


class FaultMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = rfm.run_matrix(build_cases())

    def test_whole_matrix_passes(self):
        for row in self.report["cases"]:
            self.assertEqual(row["status"], "pass",
                             "%s: %s %s" % (row["case_id"], row["mismatches"], row["error"]))
        self.assertEqual(self.report["status"], "pass")

    def test_restart_keeps_persistent_state(self):
        row = next(r for r in self.report["cases"] if r["case_id"] == "st_restart_snapshot_stable")
        self.assertTrue(row["observed"]["stable_snapshot"])
        self.assertTrue(row["observed"]["stable_version"])

    def test_model_drift_never_silently_resets(self):
        row = next(r for r in self.report["cases"] if r["case_id"] == "st_model_drift_loud")
        self.assertTrue(row["observed"]["drift_changes_key"])
        self.assertFalse(row["observed"]["silent_hit"])

    def test_retracted_source_never_re_enters_injection(self):
        row = next(r for r in self.report["cases"] if r["case_id"] == "in_retract_gates_evidence")
        self.assertFalse(row["observed"]["invalidated_present"])
        self.assertTrue(row["observed"]["excluded_loud"])

    def test_rejected_and_unknown_never_act(self):
        rows = {r["case_id"]: r for r in self.report["cases"]}
        self.assertEqual(rows["dl_rejected_no_retarget"]["observed"]["decision"], "rejected")
        self.assertFalse(rows["dl_rejected_no_retarget"]["observed"]["retargeted"])
        self.assertEqual(rows["dl_wait_is_legal"]["observed"]["decision"], "wait")
        self.assertFalse(rows["dl_wait_is_legal"]["observed"]["acted"])

    def test_rollback_restores_read_path_without_deleting_ledger(self):
        row = next(r for r in self.report["cases"] if r["case_id"] == "rb_old_path_survives")
        self.assertTrue(row["observed"]["old_path_readable"])
        self.assertTrue(row["observed"]["ledger_unchanged"])
        self.assertFalse(row["observed"]["expression_control"])
        self.assertTrue(row["observed"]["switches_independent"])


# ── AST 静态锁：纯编排，复用 V03 引擎，零副作用 ────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "release_fault_matrix.py"), encoding="utf-8") as handle:
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

    def attr_pairs(self):
        return {(n.func.value.id, n.func.attr) for n in self.calls()
                if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}

    def imported(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        return names

    def test_import_surface_is_only_the_v03_kernel(self):
        self.assertEqual({"retraction_scope_matrix"}, self.imported(),
                         "V05 是纯编排：只许 import V03 引擎，多出来的要解释")

    def test_it_delegates_execution_and_coverage_to_v03(self):
        pairs = self.attr_pairs()
        for wanted in (("v03", "run_matrix"), ("v03", "coverage")):
            self.assertIn(wanted, pairs, "必须复用 V03 引擎 %s，不另长第二套" % (wanted,))

    def test_no_banned_imports(self):
        banned = {"os", "time", "random", "datetime", "sqlite3", "pathlib", "socket",
                  "urllib", "requests", "http", "qq_openclaw_bridge", "self_model",
                  "work_state", "expression_plan", "action_proposal", "goal_attention",
                  "invalidation_dag", "emotion_dynamics", "llm_router", "emotion_shadow",
                  "napcat_channel", "web_read", "bili", "ops_remote", "paramiko"}
        self.assertEqual(set(), self.imported() & banned,
                         "V05 不该 import %s（真实契约调用只在测试 runner 里）"
                         % sorted(self.imported() & banned))

    def test_no_writer_or_db_calls(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "executemany", "commit", "connect",
                     "open", "save", "add", "record_event", "record_action",
                     "write", "writelines", "dump", "touch", "write_text", "write_bytes",
                     "rollback", "observe", "validate"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), forbidden,
                             "纯编排里出现写操作/开库/上游裁决 %s" % self.call_name(call))

    def test_no_upstream_entry_calls(self):
        wanted = {"build_snapshot", "render_injection", "build_plan", "build_attention",
                  "proposal_seed", "read_gate", "cache_key", "correction", "propagate",
                  "resolve_ripple", "render_evidence", "expand_paths", "advance",
                  "apply_event", "appraise", "run_condition_matrix"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), wanted,
                             "V05 不该调用上游入口 %s（输入由调用方 runner 传入）"
                             % self.call_name(call))

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic", "strftime",
                                  "localtime", "gmtime", "perf_counter"),
                                 "V05 读了时钟/随机 %s" % node.func.attr)
        for token in ("time.time", "fromtimestamp", "isoformat"):
            self.assertNotIn(token, self.source, "出现了墙钟取时 %s" % token)

    def test_no_sql_db_names_or_data_dir(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE ", "PRAGMA"):
                    self.assertNotIn(verb, upper, "V05 里出现 SQL：%s" % node.value[:60])
        for token in ("cognition.db", "emotion_shadow.db", "DATA_DIR", "sqlite"):
            self.assertNotIn(token, self.source, "纯编排不该碰库名/路径变量 %s" % token)

    def test_no_path_resolution(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists"),
                             "V05 自身解析路径了；runner 由调用方传入")

    def test_public_functions_take_no_now(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("case", "coverage", "run_matrix"):
                names = ([a.arg for a in node.args.args]
                         + [a.arg for a in node.args.kwonlyargs])
                self.assertNotIn("now", names, "V05 无时钟，%s 不该接收 now" % node.name)

    def test_axes_are_frozen_tuples(self):
        self.assertEqual(rfm.LAYERS,
                         ("state", "snapshot", "injection", "expression_plan",
                          "delivery", "rollback"))
        self.assertEqual(rfm.FAULTS,
                         ("restart", "model_switch", "timeout", "db_unavailable",
                          "multi_receipt", "rejected_send", "unknown_result", "retraction"))


if __name__ == "__main__":
    unittest.main()