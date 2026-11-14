# -*- coding: utf-8 -*-
"""C03 阶段3a发布人工评审包的回归锁（admission 轨，3a 预评审证据聚合）。

统一方案 §C 表 C03 行：「3a发布人工评审包：既有12项、候选误判、Ripple效果、
新增范围安全、隔离延迟/回退证据；全pass不自动授权」。§23：人工样本/人工评审
由人明确看过后签字，可评审≠已校准，AI 不能自签。

模块 bot/stage3a_review.py 是纯编排（形状校验 + 如实汇总 + 恒定边界）；
证据全部来自真实生产者：S05 evaluate 纯计算、R07 build_review_packet 隔离
夹具、V03/V05 run_matrix 内存 runner、V04 sort_observations、phase_audit
只读空目录。不读不写真实 data/，不发网络。发现契约缺陷只如实报告。
"""
import ast
import copy
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

import stage3a_review as c03              # noqa: E402
import retraction_scope_matrix as v03     # noqa: E402
import release_fault_matrix as v05        # noqa: E402
import mode_observation as mo             # noqa: E402
import valence_annotation as va           # noqa: E402
import ripple_ablation as ra              # noqa: E402
import memory_retention as mr             # noqa: E402
import phase_audit                        # noqa: E402

NOW = 4000000000.0


# ── 合成夹具（内存里的测试数据，不是自然样本）─────────────────────


def gate(key, status="pass"):
    return {"key": key, "status": status, "observed": {}, "reason": "fixture"}


def gates(*rows):
    return {"checks": list(rows) or [gate("action_evidence")]}


def s05_report(status="ok", **over):
    golds = [{"event_id": "e1", "gold_stance": "praise", "gold_target": "self",
              "origin": "natural"}]
    preds = [{"event_id": "e1", "stance": "praise", "confidence": 0.9,
              "lexical": "none"}]
    if status == "insufficient_data":
        golds, preds = [], []
    metrics = va.evaluate(golds, preds)
    report = {"ok": True, "status": status, "split": "dev", **metrics}
    report.update(over)
    return report


def claim_item(oid, value, sources=()):
    return {"object_id": oid, "object_kind": "belief", "store": "cognition",
            "root_source_ids": list(sources), "validity": "valid",
            "subject_ref": "per_1", "restatement": value, "occurred_at": 1000.0,
            "evidence_spans": [{"event_id": s, "quote": value, "subject_ref": "per_1"}
                               for s in sources]}


def r07_packet():
    s = claim_item("s", "喜欢茶", sources=["e1"])
    n1 = claim_item("n1", "喜欢咖啡", sources=["e2"])
    rows = [s, n1]
    retention = mr.retention_set(rows, ["e1", "e2"], mode="keep")
    edges = [{"edge_id": "edge_s_n1", "type": "shared_entity",
              "a": "s", "a_kind": "belief", "b": "n1", "b_kind": "belief",
              "basis": {"reason": "shared_entity"}}]
    return ra.build_review_packet(retention, seeds=[s], edges=edges, now=NOW)


def v03_report(fail=False, error=False):
    def runner():
        if error:
            raise RuntimeError("fixture runner crash")
        return {"ok": not fail}
    cases = [v03.case("case_%s" % ("fail" if fail else "pass"),
                      "cognition", "retract_source", {"ok": True}, runner)]
    return v03.run_matrix(cases)


def v05_report(complete=True, fail=False, error=False):
    def runner():
        if error:
            raise RuntimeError("fixture runner crash")
        return {"ok": not fail}
    faults = v05.FAULTS if complete else v05.FAULTS[:2]
    cases = [v05.case("case_%d" % i, v05.LAYERS[i % len(v05.LAYERS)],
                      fault, {"ok": True}, runner)
             for i, fault in enumerate(faults)]
    return v05.run_matrix(cases)


def isolation(complete=True, fail=False, error=False):
    return {"mode_observation": mo.sort_observations([]),
            "fault_matrix": v05_report(complete=complete, fail=fail, error=error)}


def full_evidence():
    return {"existing_gates": gates(),
            "candidate_misjudgment": s05_report(),
            "ripple_effect": r07_packet(),
            "scope_safety": v03_report(),
            "isolation_rollback": isolation()}


def build(evidence=None, now=NOW):
    return c03.build_review_package(full_evidence() if evidence is None else evidence,
                                    now=now)


# ── 形状与常量 ─────────────────────────────────────────────────────


class ShapeTests(unittest.TestCase):
    def test_sections_match_plan_doc_order(self):
        self.assertEqual(c03.SECTIONS,
                         ("existing_gates", "candidate_misjudgment", "ripple_effect",
                          "scope_safety", "isolation_rollback"))

    def test_algorithm_version_bound(self):
        self.assertEqual(c03.ALGORITHM_VERSION, "c03-1")
        self.assertEqual(build()["provenance"]["algorithm_version"], "c03-1")

    def test_limitations_are_honest_and_nonempty(self):
        self.assertTrue(len(c03.LIMITATIONS) >= 5)
        joined = " ".join(c03.LIMITATIONS)
        for phrase in ("全pass不自动授权", "AI 不能自签", "缺数据≠零", "真实灰度实测"):
            self.assertIn(phrase, joined)

    def test_report_json_serializable_without_nan(self):
        json.dumps(build(), ensure_ascii=False, allow_nan=False)

    def test_provenance_flags_never_claim_production_or_network(self):
        prov = build()["provenance"]
        self.assertFalse(prov["llm_reinterpretation"])
        self.assertFalse(prov["network"])
        self.assertFalse(prov["production_ledger"])
        self.assertEqual(set(prov["producers"]), set(c03.SECTIONS))


class BoundaryTests(unittest.TestCase):
    def test_all_pass_never_authorizes(self):
        package = build()
        self.assertTrue(package["all_pass"])
        self.assertEqual(package["authorization"], "manual_review")
        self.assertIsNone(package["judgement"])
        self.assertFalse(package["expression_control"])
        self.assertFalse(package["outbound"])

    def test_empty_evidence_never_authorizes(self):
        package = build({})
        self.assertFalse(package["all_pass"])
        self.assertEqual(package["authorization"], "manual_review")
        self.assertIsNone(package["judgement"])
        self.assertFalse(package["expression_control"])
        self.assertFalse(package["outbound"])

    def test_safety_block_never_authorizes(self):
        evidence = full_evidence()
        evidence["scope_safety"] = v03_report(fail=True)
        package = build(evidence)
        self.assertFalse(package["all_pass"])
        self.assertEqual(package["authorization"], "manual_review")
        self.assertIsNone(package["judgement"])


class MissingEvidenceTests(unittest.TestCase):
    def test_empty_evidence_lists_every_section_missing(self):
        package = build({})
        self.assertEqual(package["missing_sections"], list(c03.SECTIONS))
        for name in c03.SECTIONS:
            self.assertEqual(package["sections"][name]["status"], "missing")
            self.assertIsNone(package["sections"][name]["evidence"])

    def test_partial_evidence_lists_only_absent_sections(self):
        evidence = full_evidence()
        del evidence["ripple_effect"]
        del evidence["scope_safety"]
        package = build(evidence)
        self.assertEqual(package["missing_sections"], ["ripple_effect", "scope_safety"])
        self.assertFalse(package["all_pass"])

    def test_none_payload_is_missing_not_error(self):
        evidence = full_evidence()
        evidence["candidate_misjudgment"] = None
        package = build(evidence)
        self.assertEqual(package["sections"]["candidate_misjudgment"]["status"], "missing")

    def test_unknown_section_rejected_loudly(self):
        with self.assertRaises(ValueError):
            build({"surprise": {}})

    def test_non_dict_evidence_rejected(self):
        with self.assertRaises(ValueError):
            build([])

    def test_bad_now_rejected(self):
        for bad in (None, "yesterday", 0, -1):
            with self.assertRaises(ValueError):
                build(now=bad)


class ExistingGatesTests(unittest.TestCase):
    def test_all_pass(self):
        section = build()["sections"]["existing_gates"]
        self.assertEqual(section["status"], "pass")
        self.assertEqual(section["summary"]["counts"], {"pass": 1})

    def test_not_observed_gate_stays_not_observed(self):
        package = build({"existing_gates": gates(gate("self_view_grounding", "not_observed"))})
        section = package["sections"]["existing_gates"]
        self.assertEqual(section["status"], "not_observed")
        self.assertEqual(section["summary"]["non_pass"],
                         [{"key": "self_view_grounding", "status": "not_observed"}])
        self.assertFalse(package["all_pass"])

    def test_blocked_gate_is_safety_block_not_averaged(self):
        package = build({"existing_gates": gates(
            gate("action_evidence"), gate("evidence_retraction", "blocked"))})
        self.assertEqual(package["sections"]["existing_gates"]["status"], "blocked")
        self.assertIn({"section": "existing_gates", "ref": "evidence_retraction",
                       "reason": "gate_blocked"}, package["safety_blocks"])

    def test_fail_gate_maps_to_blocked(self):
        package = build({"existing_gates": gates(gate("consent_enforcement", "fail"))})
        self.assertEqual(package["sections"]["existing_gates"]["status"], "blocked")

    def test_unknown_gate_stays_unknown(self):
        package = build({"existing_gates": gates(gate("cognition_evidence", "unknown"))})
        self.assertEqual(package["sections"]["existing_gates"]["status"], "unknown")

    def test_not_verified_engineering_gate_is_not_observed(self):
        package = build({"existing_gates": gates(
            gate("consent_enforcement", "not_verified"))})
        section = package["sections"]["existing_gates"]
        self.assertEqual(section["status"], "not_observed")
        self.assertEqual(section["summary"]["non_pass"],
                         [{"key": "consent_enforcement", "status": "not_verified"}])

    def test_blocked_outranks_unknown_and_not_observed(self):
        package = build({"existing_gates": gates(
            gate("a", "not_observed"), gate("b", "unknown"), gate("c", "blocked"))})
        self.assertEqual(package["sections"]["existing_gates"]["status"], "blocked")

    def test_empty_checks_rejected(self):
        with self.assertRaises(ValueError):
            build({"existing_gates": {"checks": []}})

    def test_unknown_gate_status_rejected(self):
        with self.assertRaises(ValueError):
            build({"existing_gates": gates(gate("a", "great"))})

    def test_gate_without_key_rejected(self):
        with self.assertRaises(ValueError):
            build({"existing_gates": {"checks": [{"status": "pass"}]}})

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            build({"existing_gates": [gate("a")]})


class CandidateMisjudgmentTests(unittest.TestCase):
    def test_real_evaluate_ok_is_pass_with_metrics(self):
        section = build()["sections"]["candidate_misjudgment"]
        self.assertEqual(section["status"], "pass")
        self.assertEqual(section["summary"]["n_joined"], 1)
        self.assertEqual(section["summary"]["split"], "dev")
        self.assertEqual(section["summary"]["origin"], "natural")
        self.assertEqual(section["evidence"]["gold_source"], "human_annotation")

    def test_insufficient_data_is_not_observed_not_zero(self):
        package = build({"candidate_misjudgment": s05_report("insufficient_data")})
        section = package["sections"]["candidate_misjudgment"]
        self.assertEqual(section["status"], "not_observed")
        self.assertEqual(section["summary"]["n_joined"], 0)
        self.assertFalse(package["all_pass"])

    def test_missing_data_report_is_not_observed(self):
        package = build({"candidate_misjudgment": {"ok": False, "status": "missing_data"}})
        self.assertEqual(package["sections"]["candidate_misjudgment"]["status"],
                         "not_observed")

    def test_failed_report_with_wrong_status_rejected(self):
        with self.assertRaises(ValueError):
            build({"candidate_misjudgment": {"ok": False, "status": "ok"}})

    def test_non_human_gold_rejected_loudly(self):
        with self.assertRaises(ValueError):
            build({"candidate_misjudgment": s05_report(gold_source="candidate_confidence")})

    def test_missing_split_rejected(self):
        report = s05_report()
        del report["split"]
        with self.assertRaises(ValueError):
            build({"candidate_misjudgment": report})

    def test_bad_origin_rejected(self):
        with self.assertRaises(ValueError):
            build({"candidate_misjudgment": s05_report(origin="mixed")})

    def test_lexical_disagreement_visible(self):
        section = build()["sections"]["candidate_misjudgment"]
        self.assertEqual(set(section["summary"]["lexical_relation"]),
                         {"hit", "missed", "mismatch", "neutral"})


class RippleEffectTests(unittest.TestCase):
    def test_real_r07_packet_is_pass(self):
        section = build()["sections"]["ripple_effect"]
        self.assertEqual(section["status"], "pass")
        self.assertGreaterEqual(section["summary"]["variants"], 2)
        self.assertEqual(section["summary"]["organic"], "not_observed")

    def test_machine_judgement_rejected_loudly(self):
        packet = r07_packet()
        packet["review"]["judgement"] = "useful"
        with self.assertRaises(ValueError):
            build({"ripple_effect": packet})

    def test_self_granted_authorization_rejected(self):
        packet = r07_packet()
        packet["review"]["authorization"] = "pass"
        with self.assertRaises(ValueError):
            build({"ripple_effect": packet})

    def test_empty_variants_rejected(self):
        packet = r07_packet()
        packet["variants"] = []
        with self.assertRaises(ValueError):
            build({"ripple_effect": packet})

    def test_missing_packet_key_rejected(self):
        packet = r07_packet()
        del packet["diffs"]
        with self.assertRaises(ValueError):
            build({"ripple_effect": packet})


class ScopeSafetyTests(unittest.TestCase):
    def test_real_v03_pass(self):
        section = build()["sections"]["scope_safety"]
        self.assertEqual(section["status"], "pass")
        self.assertEqual(section["summary"]["failed"], [])

    def test_fail_is_blocked_and_listed_not_averaged(self):
        package = build({"scope_safety": v03_report(fail=True)})
        section = package["sections"]["scope_safety"]
        self.assertEqual(section["status"], "blocked")
        self.assertEqual(section["summary"]["failed"][0]["case_id"], "case_fail")
        self.assertIn({"section": "scope_safety", "ref": "case_fail",
                       "reason": "matrix_fail"}, package["safety_blocks"])
        self.assertFalse(package["all_pass"])

    def test_runner_error_is_error_not_swallowed(self):
        package = build({"scope_safety": v03_report(error=True)})
        section = package["sections"]["scope_safety"]
        self.assertEqual(section["status"], "error")
        self.assertIn("RuntimeError", section["summary"]["failed"][0]["error"])

    def test_empty_matrix_rejected_by_kernel(self):
        with self.assertRaises(ValueError):
            build({"scope_safety": {"status": "pass", "cases": []}})

    def test_unknown_matrix_status_rejected(self):
        with self.assertRaises(ValueError):
            build({"scope_safety": {"status": "fine", "cases": [{"case_id": "x"}]}})


class IsolationRollbackTests(unittest.TestCase):
    def test_real_v04_v05_complete_is_pass(self):
        section = build()["sections"]["isolation_rollback"]
        self.assertEqual(section["status"], "pass")
        self.assertTrue(section["summary"]["coverage_complete"])
        self.assertEqual(section["summary"]["mode_counts"],
                         {"live": 0, "observation_only": 0, "manual_review": 0,
                          "rejected": 0})

    def test_incomplete_coverage_is_not_observed_with_missing_listed(self):
        package = build({"isolation_rollback": isolation(complete=False)})
        section = package["sections"]["isolation_rollback"]
        self.assertEqual(section["status"], "not_observed")
        self.assertFalse(section["summary"]["coverage_complete"])
        self.assertTrue(section["summary"]["missing_required_faults"])
        self.assertFalse(package["all_pass"])

    def test_fault_fail_is_blocked_and_listed(self):
        package = build({"isolation_rollback": isolation(fail=True)})
        section = package["sections"]["isolation_rollback"]
        self.assertEqual(section["status"], "blocked")
        self.assertTrue(any(block["section"] == "isolation_rollback"
                            for block in package["safety_blocks"]))

    def test_fault_error_is_error(self):
        package = build({"isolation_rollback": isolation(error=True)})
        self.assertEqual(package["sections"]["isolation_rollback"]["status"], "error")

    def test_missing_mode_bucket_rejected(self):
        payload = isolation()
        del payload["mode_observation"]["rejected"]
        with self.assertRaises(ValueError):
            build({"isolation_rollback": payload})

    def test_missing_coverage_audit_rejected(self):
        payload = isolation()
        del payload["fault_matrix"]["coverage"]
        with self.assertRaises(ValueError):
            build({"isolation_rollback": payload})

    def test_missing_fault_matrix_rejected(self):
        with self.assertRaises(ValueError):
            build({"isolation_rollback": {"mode_observation": mo.sort_observations([])}})


class PackageIdentityTests(unittest.TestCase):
    def test_same_evidence_same_id_across_clocks(self):
        self.assertEqual(build(now=NOW)["package_id"], build(now=NOW + 999)["package_id"])

    def test_different_evidence_different_id(self):
        a = build()["package_id"]
        evidence = full_evidence()
        evidence["existing_gates"] = gates(gate("action_evidence", "not_observed"))
        self.assertNotEqual(a, build(evidence)["package_id"])

    def test_generated_at_recorded_but_not_in_id(self):
        package = build(now=NOW + 5)
        self.assertEqual(package["generated_at"], NOW + 5)

    def test_input_evidence_not_mutated(self):
        evidence = full_evidence()
        before = copy.deepcopy(evidence)
        build(evidence)
        self.assertEqual(evidence, before)

    def test_safety_blocks_empty_when_all_pass(self):
        self.assertEqual(build()["safety_blocks"], [])


class RealPhaseAuditTests(unittest.TestCase):
    def test_empty_ledger_root_never_passes_and_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="c03_audit_") as tmp:
            before = sorted(Path(tmp).rglob("*"))
            audit = phase_audit.audit_readiness(root=tmp, now=NOW)
            self.assertEqual(sorted(Path(tmp).rglob("*")), before)
            package = build({"existing_gates": audit["phase25"]})
            section = package["sections"]["existing_gates"]
            self.assertIn(section["status"], ("not_observed", "unknown", "blocked"))
            self.assertNotEqual(section["status"], "pass")
            self.assertFalse(package["all_pass"])


class AstLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(BOT, "stage3a_review.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_only_stdlib_pure_imports(self):
        allowed = {"copy", "hashlib", "json"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], allowed)

    def test_no_io_or_exec_calls(self):
        forbidden = {"open", "exec", "eval", "compile", "__import__", "input"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden)

    def test_no_clock_random_or_path_resolution(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 {"time", "random", "resolve", "connect", "urlopen",
                                  "system", "popen", "write_text", "read_text"})

    def test_boundary_constants_are_literals(self):
        found = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.setdefault(node.value, 0)
                found[node.value] += 1
        self.assertIn("manual_review", found)
        negatives = [n for n in ast.walk(self.tree)
                     if isinstance(n, ast.Constant) and n.value is False]
        self.assertGreaterEqual(len(negatives), 4)

    def test_build_signature_requires_keyword_now(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "build_review_package")
        self.assertEqual([a.arg for a in fn.args.kwonlyargs], ["now"])
        self.assertFalse(fn.args.defaults)

    def test_no_authorization_assignment_other_than_constant(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assertNotIn(target.id,
                                         {"authorization", "judgement",
                                          "expression_control", "outbound"})

    def test_sections_and_validators_cover_each_other(self):
        validators = next(n for n in ast.walk(self.tree)
                          if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "_VALIDATORS"
                                  for t in n.targets))
        keys = {k.value for k in validators.value.keys}
        self.assertEqual(keys, set(c03.SECTIONS))

    def test_missing_never_counts_as_pass(self):
        self.assertIn("missing", c03.SECTION_STATUSES)
        source = self.source
        self.assertIn("all_pass = all(status == 'pass' for status in statuses)", source)

    def test_no_production_data_dir_reference(self):
        self.assertNotIn("DATA_DIR", self.source)
        self.assertNotIn("config.env", self.source)

    def test_package_id_is_content_addressed_without_clock(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "build_review_package")
        segment = ast.get_source_segment(self.source, fn)
        id_line = next(line for line in segment.splitlines() if "'package_id'" in line)
        self.assertNotIn("now", id_line)
        self.assertNotIn("generated_at", id_line)


if __name__ == "__main__":
    unittest.main()