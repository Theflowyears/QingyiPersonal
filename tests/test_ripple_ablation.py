# -*- coding: utf-8 -*-
"""R07 关联记忆保留集消融：固定保留集上的可执行对照契约 + 人工评审包。

行为锁覆盖验收判据：变体集合固定可枚举且只用 R04/R05/R06 真实开关；计划内容
寻址（now 必填）；逐变体复用 R05 resolve_ripple 与 R06 render_evidence（不复制
平行实现）；差异报告稳定排序、稳定 diff_reason、带 sha16 引用；冲突/降级/失效
语义完全承接 R05/R06（invalidated 不复活、缺失不补造、degraded 不冒充充分）；
预算超限大声 dropped(reason=budget)；评审包 manual_review/not_observed 口径、
不下结论、不落聊天原文；空保留集降级；确定性重跑一致。

AST 锁钉死「复用而非重造」：import 面只许 copy/json + R05/R04/R06/A03；必须调
R05 resolve_ripple 与 R06 render_evidence；不 import R01/R02/R03/V02/各库（输入
由调用方传入），无写操作、无时钟随机、无打分机器、无第二套矛盾引擎。

R07 是 manual_review：契约可执行 ≠ 真实消融已做。所有合成数据只出现在 tempfile
隔离目录或内存里，是测试夹具，不是自然样本；端到端接真实 R01→R02→R03→V02→R07
（测试可以 import R01/R02/R03/V02，被测模块 ripple_ablation 本身不 import）。
"""
import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import ripple_ablation as ra
import ripple_evidence as re6
import ripple_conflicts as rc
import ripple_paths as rp
import claim_conflicts as cc
import memory_manifest as mm
import memory_retention as mr
import association_edges as ae
import recall_scoring as rs
import retrieval_objects as ro
import self_model as sm

NOW = 4000000000.0


def snapshot(root):
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


def item(oid, kind="belief", store="cognition", sources=(), validity="valid", **over):
    row = {"object_id": oid, "object_kind": kind, "store": store,
           "root_source_ids": list(sources), "evidence_spans": [],
           "validity": validity}
    row.update(over)
    return row


def claim_item(oid, value, subject="per_1", sources=(), **over):
    """带主体/原句/发生时间的合成候选夹具（不是自然样本）。"""
    row = item(oid, sources=sources, subject_ref=subject, restatement=value,
               occurred_at=1000.0,
               evidence_spans=[{"event_id": s, "quote": value, "subject_ref": subject}
                               for s in sources])
    row.update(over)
    return row


def edge(a, b, etype="shared_entity", basis=None, a_kind="belief", b_kind="belief",
         edge_id=None):
    if basis is None:
        basis = {"reason": etype}
    return {"edge_id": edge_id or ("edge_%s_%s_%s" % (etype, a, b)),
            "type": etype, "a": a, "a_kind": a_kind, "b": b, "b_kind": b_kind,
            "basis": dict(basis)}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def base_fixture():
    """s←e1 种子，n1←e2 / n2←e3 一跳邻居，dead←e4 已失效（V02 不洗层）。"""
    s = claim_item("s", "喜欢茶", sources=["e1"])
    n1 = claim_item("n1", "喜欢咖啡", sources=["e2"])
    n2 = claim_item("n2", "喜欢可可", sources=["e3"])
    dead = claim_item("dead", "喜欢果汁", sources=["e4"], validity="invalidated")
    rows = [s, n1, n2, dead]
    retention = mr.retention_set(rows, ["e1", "e2", "e3", "e4"], mode="keep")
    seeds = [s]
    edges = [edge("s", "n1"), edge("s", "n2"), edge("s", "dead")]
    return retention, seeds, edges


def packet(retention, seeds, edges, **kw):
    kw.setdefault("now", NOW)
    return ra.build_review_packet(retention, seeds=seeds, edges=edges, **kw)


def plan(retention, seeds, edges, **kw):
    kw.setdefault("now", NOW)
    return ra.build_plan(retention, seeds=seeds, edges=edges, **kw)


def by_id(runs, variant_id):
    return next(r for r in runs if r["variant_id"] == variant_id)


def selected_ids(rendered):
    return [x["object_id"] for x in rendered["manifest"]["selected"]]


def excluded_of(rendered, category):
    return [x for x in rendered["manifest"]["excluded"] if x["category"] == category]


def walk_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from walk_keys(inner)


def render_stub(selected=(), excluded=(), status="sufficient", counts=None):
    """diff 单测用的最小 R06 形状夹具（合成，不是模块伪造的输出）。"""
    return {"status": status,
            "manifest": {"selected": list(selected), "excluded": list(excluded),
                         "counts": counts or {}, "context": {}, "degradation": []}}


def selected_row(oid, reason="grounded", sha16=None, missing=()):
    return {"object_id": oid, "scene_id": "scene_" + oid, "reason": reason,
            "store": "cognition", "root_representative": "e_" + oid,
            "missing_slots": list(missing), "sha16": sha16 or ("a" * 16)}


# ── AST 静态锁：复用 R05/R06/R04/A03，不长第二套引擎，真只读 ───────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "ripple_ablation.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_import_surface_is_copy_json_plus_r05_r04_r06_a03(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"copy", "json", "ripple_conflicts",
                                        "ripple_paths", "ripple_evidence",
                                        "memory_manifest"}, "多出来的 import 面要解释")
        self.assertLessEqual({"ripple_conflicts", "ripple_paths", "ripple_evidence",
                              "memory_manifest"}, imported,
                             "R07 必须复用 R05/R04/R06/A03，不复制平行实现")

    def test_it_reuses_the_real_r05_r06_entry_points_and_a03_digest(self):
        calls = {(n.func.value.id, n.func.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name)}
        self.assertIn(("rc", "resolve_ripple"), calls, "逐变体执行必须走 R05 真实入口")
        self.assertIn(("re6", "render_evidence"), calls, "证据场景必须走 R06 真实渲染")
        self.assertIn(("mm", "digest"), calls, "内容寻址必须复用 A03 digest")
        self.assertIn(("rp", "_index"), calls, "种子索引必须复用 R04 _index")

    def test_it_does_not_import_r01_r02_r03_v02_or_any_store(self):
        banned = {"retrieval_objects", "recall_scoring", "association_edges",
                  "memory_retention", "self_model", "persona_store",
                  "group_memory_store", "knowledge_base", "vision_notes",
                  "sqlite3", "os", "time", "random", "claim_conflicts"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             "R07 应是纯函数，不该 import %s" % sorted(names & banned))

    def test_no_second_adjudication_engine(self):
        engine = {"classify_predicate", "adjudicate_pair", "candidate_key",
                  "collect_conflict_pairs", "adjudicate", "_core_value",
                  "_family_of", "_view", "_value_of", "_root_of"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, engine, "重定义了 M04 引擎函数 %s" % node.name)
                for word in ("classify", "adjudicate", "family", "verdict", "contradict"):
                    self.assertNotIn(word, node.name.lower(),
                                     "长出了第二套矛盾机器 %s" % node.name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, cc.PREDICATE_FAMILIES,
                                 "R07 不许自带家族词表 %s" % node.value)
        for token in ("_FAMILY_MARKERS", "_NEGATION_PREFIXES", "_PREFERENCE_POSITIVE",
                      "_PREFERENCE_NEGATIVE", "_FAMILY_LEADERS"):
            self.assertNotIn(token, self.source, "出现了 M04 的剥壳词表 %s" % token)

    def test_no_verdict_literal_is_fabricated(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, cc.VERDICTS,
                                 "R07 不裁决，不许出现 verdict 字面量 %s" % node.value)

    def test_the_module_never_writes_or_creates_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "connect", "atomic_write_json",
                     "save", "add", "add_belief", "record_event", "write", "writelines",
                     "dump", "touch"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读契约里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "open", "纯函数不该打开任何文件")
        for token in ("INSERT INTO", "INSERT OR", "UPDATE ", "DELETE FROM",
                      "REPLACE INTO", "CREATE TABLE", "CREATE INDEX", "DROP TABLE",
                      "sqlite", "executescript", "PRAGMA", "cognition.db"):
            self.assertNotIn(token, self.source,
                             "只读契约里出现建库/写库语句或库名 %s" % token)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_no_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "monotonic", "now", "today", "random",
                                  "randint", "choice", "shuffle", "uuid4", "urandom"),
                                 "确定性契约引入了时钟/随机：%s" % node.func.attr)

    def test_no_scoring_machinery(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("score", "score_parts", "weight"),
                                 "融合打分是 R03，R07 不许读分数")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("score", "scoring", "rank", "fuse", "fusion"):
                    self.assertNotIn(word, node.name.lower(), "出现了打分函数 %s" % node.name)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                for word in ("score", "scoring", "rank", "fuse", "fusion"):
                    self.assertNotIn(word, name.lower(), "调用了打分函数 %s" % name)

    def test_it_does_not_call_the_other_upstream_entry_points(self):
        """只许走 R05 resolve_ripple 与 R06 render_evidence；其余上游由调用方给定。"""
        wanted = {"retrieve_evidence", "fetch_retrieval_items", "project_edges",
                  "recall", "score_item", "read_all_candidates", "adjudicate",
                  "expand_paths", "assess_expansion", "retention_set", "split_by_root"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name, wanted,
                                 "R07 不该调用上游入口 %s，输入由调用方传入" % name)

    def test_no_retired_or_control_flags(self):
        for token in ("expression_control", "counts_as_live_observation"):
            self.assertNotIn(token, self.source, "出现了全局静态锁禁止的标记 %s" % token)

    def test_the_variant_contract_constants(self):
        self.assertEqual(("full", "no_edges", "no_expansion", "half_expansion",
                          "half_degree", "half_render", "no_slot_check"), ra.VARIANT_IDS)
        self.assertEqual("full", ra.VARIANT_IDS[0], "full 必须是基线且排第一")
        self.assertEqual(len(set(ra.VARIANT_IDS)), len(ra.VARIANT_IDS))
        for knob in ("edges", "max_expanded", "max_degree", "max_scenes",
                     "max_entries", "required_slots"):
            self.assertIn(knob, ra.KNOBS)

    def test_the_diff_and_filter_reason_codes_are_fixed(self):
        self.assertEqual(("selected_added", "selected_removed", "selected_changed",
                          "excluded_added", "excluded_removed", "excluded_changed",
                          "dropped_added", "dropped_removed", "dropped_changed"),
                         ra.DIFF_REASONS)
        self.assertEqual(("seed_not_retained", "seed_malformed", "endpoint_not_retained"),
                         ra.FILTER_REASONS)

    def test_the_review_caliber_matches_v02(self):
        self.assertEqual("manual_review", ra.REVIEW_AUTHORIZATION)
        self.assertEqual("not_observed", ra.REVIEW_ORGANIC)

    def test_the_limitations_declare_the_undone_honestly(self):
        self.assertIsInstance(ra.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(ra.LIMITATIONS), 5)
        limits = " ".join(ra.LIMITATIONS)
        for undone in ("R02", "R03", "R08", "C04", "M04", "LLM",
                       "manual_review", "not_observed"):
            self.assertIn(undone, limits, "limitations 必须如实声明没做的事 %s" % undone)

    def test_the_docstring_declares_executable_not_real_ablation(self):
        self.assertIn("真实消融需真实保留集", ra.__doc__)
        self.assertIn("只证明可执行", ra.__doc__)
        self.assertIn("manual_review", ra.__doc__)


# ── 行为锁：消融计划（内容寻址、变体定义、保留集绑定）─────────────────


class PlanTests(unittest.TestCase):
    def test_plan_id_is_a_stable_content_address(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        self.assertRegex(got["plan_id"], r"^plan_[0-9a-f]{14}$")
        self.assertEqual(got["plan_id"], plan(retention, seeds, edges)["plan_id"])

    def test_now_is_required_and_loud(self):
        retention, seeds, edges = base_fixture()
        with self.assertRaises(ValueError):
            ra.build_plan(retention, seeds=seeds, edges=edges, now=None)
        with self.assertRaises(ValueError):
            ra.build_plan(retention, seeds=seeds, edges=edges, now="昨天")
        with self.assertRaises(ValueError):
            ra.build_plan(retention, seeds=seeds, edges=edges, now=True)

    def test_now_zero_is_a_valid_logical_time(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges, now=0)
        self.assertEqual(0, got["now"])

    def test_plan_id_changes_with_now(self):
        retention, seeds, edges = base_fixture()
        self.assertNotEqual(plan(retention, seeds, edges, now=1)["plan_id"],
                            plan(retention, seeds, edges, now=2)["plan_id"])

    def test_plan_id_changes_with_the_retention_condition(self):
        retention, seeds, edges = base_fixture()
        rows = retention["retained_items"]
        other = mr.retention_set(rows, ["e4"], mode="ablate")
        self.assertNotEqual(plan(retention, seeds, edges)["plan_id"],
                            plan(other, seeds, edges)["plan_id"])

    def test_plan_id_changes_with_the_budgets(self):
        retention, seeds, edges = base_fixture()
        self.assertNotEqual(plan(retention, seeds, edges)["plan_id"],
                            plan(retention, seeds, edges, max_expanded=3)["plan_id"])

    def test_the_variant_set_is_fixed_enumerable_and_defined(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        self.assertEqual(list(ra.VARIANT_IDS), [v["variant_id"] for v in got["variants"]])
        for variant in got["variants"]:
            self.assertEqual({"variant_id", "changes", "keeps", "definition"},
                             set(variant))
            self.assertIsInstance(variant["definition"], str)
            self.assertTrue(variant["definition"])
            self.assertEqual([k for k in ra.KNOBS if k not in variant["changes"]],
                             variant["keeps"], "keeps 必须如实列出没改的开关")
        self.assertEqual({}, got["variants"][0]["changes"], "full 基线不改任何开关")

    def test_variant_definitions_are_stable_across_builds(self):
        retention, seeds, edges = base_fixture()
        first = plan(retention, seeds, edges, max_expanded=3, max_degree=3,
                     max_scenes=3, max_entries=3)
        second = plan(retention, seeds, edges, max_expanded=3, max_degree=3,
                      max_scenes=3, max_entries=3)
        self.assertEqual(canonical(first["variants"]), canonical(second["variants"]))

    def test_half_variants_floor_the_real_budgets(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges, max_expanded=3, max_degree=3,
                   max_scenes=3, max_entries=3)
        changes = {v["variant_id"]: v["changes"] for v in got["variants"]}
        self.assertEqual({"max_expanded": 1}, changes["half_expansion"])
        self.assertEqual({"max_degree": 1}, changes["half_degree"])
        self.assertEqual({"max_scenes": 1, "max_entries": 1}, changes["half_render"])
        self.assertEqual({"max_expanded": 0}, changes["no_expansion"])
        self.assertEqual({"edges": []}, changes["no_edges"])
        self.assertEqual({"required_slots": []}, changes["no_slot_check"])

    def test_zero_budgets_stay_zero_not_negative(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges, max_expanded=0, max_degree=0,
                   max_scenes=0, max_entries=0)
        for variant in got["variants"]:
            for value in variant["changes"].values():
                if isinstance(value, int):
                    self.assertGreaterEqual(value, 0)

    def test_the_retention_binding_is_echoed_for_review(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        binding = got["retention"]
        self.assertEqual(retention["fingerprint"], binding["fingerprint"])
        self.assertEqual(retention["retained"], binding["retained"])
        self.assertEqual(retention["removed"], binding["removed"])
        self.assertEqual(retention["mode"], binding["mode"])
        self.assertEqual(retention["policy"], binding["policy"])
        self.assertNotIn("retained_items", binding, "计划只绑定 ID 与指纹，不搬原文")

    def test_seeds_outside_the_retention_set_are_registered_loudly(self):
        retention, seeds, edges = base_fixture()
        outsider = claim_item("x", "喜欢奶茶", sources=["e9"])
        got = plan(retention, seeds + [outsider], edges)
        self.assertIn({"object_id": "x", "reason_code": "seed_not_retained"},
                      got["seed_filter"])
        self.assertEqual(["s"], got["seed_ids"])

    def test_malformed_seeds_are_registered_not_silently_dropped(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, [None, 42] + seeds, edges)
        self.assertEqual([{"object_id": "", "reason_code": "seed_malformed"}] * 2,
                         got["seed_filter"])

    def test_edges_touching_removed_memories_are_registered_loudly(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n1 = claim_item("n1", "喜欢咖啡", sources=["e2"])
        n2 = claim_item("n2", "喜欢可可", sources=["e3"])
        retention = mr.retention_set([s, n1, n2], ["e2"], mode="ablate")
        got = plan(retention, [s], [edge("s", "n1"), edge("s", "n2")])
        self.assertEqual([{"edge_id": "edge_shared_entity_s_n1",
                           "reason_code": "endpoint_not_retained",
                           "endpoints": ["n1"]}], got["edge_filter"])
        self.assertEqual(["edge_shared_entity_s_n2"], got["edge_ids"])

    def test_filter_reason_codes_are_the_contract_ones(self):
        retention, seeds, edges = base_fixture()
        outsider = claim_item("x", "喜欢奶茶", sources=["e9"])
        got = plan(retention, [None] + seeds + [outsider], edges)
        for entry in got["seed_filter"] + got["edge_filter"]:
            self.assertIn(entry["reason_code"], ra.FILTER_REASONS)

    def test_unknown_required_slots_are_rejected_loudly(self):
        retention, seeds, edges = base_fixture()
        with self.assertRaises(ValueError):
            plan(retention, seeds, edges, required_slots=("vibes",))

    def test_empty_required_slots_means_no_slot_check(self):
        retention, seeds, edges = base_fixture()
        for slots in ((), None):
            got = plan(retention, seeds, edges, required_slots=slots)
            self.assertEqual([], got["required_slots"])

    def test_empty_retention_still_builds_a_valid_plan(self):
        empty = mr.retention_set([], [], mode="keep")
        got = plan(empty, [], [])
        self.assertEqual([], got["pool_ids"])
        self.assertEqual([], got["seed_ids"])
        self.assertRegex(got["plan_id"], r"^plan_[0-9a-f]{14}$")

    def test_non_dict_retention_is_tolerated_as_empty(self):
        got = plan(None, [], [], now=NOW)
        self.assertEqual([], got["pool_ids"])

    def test_input_order_does_not_change_the_plan(self):
        retention, seeds, edges = base_fixture()
        s, n1 = retention["retained_items"][0], retention["retained_items"][1]
        baseline = plan(retention, [s, n1], edges)
        shuffled = plan(retention, [n1, s], list(reversed(edges)))
        self.assertEqual(baseline["plan_id"], shuffled["plan_id"])
        self.assertEqual(canonical(baseline), canonical(shuffled))

    def test_the_inputs_are_not_mutated_by_planning(self):
        retention, seeds, edges = base_fixture()
        context = {"logical_turn_id": "t1"}
        before = canonical([retention, seeds, edges, context])
        plan(retention, seeds, edges, context=context)
        self.assertEqual(before, canonical([retention, seeds, edges, context]))

    def test_context_and_degradation_are_carried_verbatim(self):
        retention, seeds, edges = base_fixture()
        context = {"logical_turn_id": "t9"}
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        got = plan(retention, seeds, edges, context=context, degradation=degradation)
        self.assertEqual(context, got["context"])
        self.assertEqual(degradation, got["degradation"])

    def test_semantic_hints_are_registered_deterministically(self):
        retention, seeds, edges = base_fixture()
        hints = {("s", "n1"): {"relation": "same", "source": "unit_test"}}
        got = plan(retention, seeds, edges, semantic_hints=hints)
        self.assertEqual([["s", "n1", {"relation": "same", "source": "unit_test"}]],
                         got["semantic_hints"])

    def test_the_plan_carries_no_chat_text(self):
        retention, seeds, edges = base_fixture()
        text = canonical(plan(retention, seeds, edges))
        for raw in ("喜欢茶", "喜欢咖啡", "喜欢可可", "喜欢果汁"):
            self.assertNotIn(raw, text, "计划只登记 ID，不落聊天原文")


# ── 行为锁：逐变体执行（复用真实链路，语义承接 R05/R06）────────────────


class ExecutionTests(unittest.TestCase):
    def test_full_variant_equals_the_real_r05_r06_pipeline(self):
        retention, seeds, edges = base_fixture()
        context = {"logical_turn_id": "t1"}
        hints = {("s", "n1"): {"relation": "same", "source": "unit_test"}}
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        got = plan(retention, seeds, edges, context=context, semantic_hints=hints,
                   degradation=degradation)
        run = ra.execute_variant(got, "full", retention, seeds=seeds, edges=edges)
        pool = retention["retained_items"]
        resolved = rc.resolve_ripple(seeds, edges, items=pool, degradation=degradation,
                                     required_slots=rc.SLOTS, semantic_hints=hints)
        rendered = re6.render_evidence(resolved, items=pool, seeds=seeds, context=context)
        self.assertEqual(canonical(resolved), canonical(run["resolved"]),
                         "full 变体必须就是真实 R05 输出，不是平行实现")
        self.assertEqual(canonical(rendered), canonical(run["rendered"]),
                         "full 变体必须就是真实 R06 渲染，不是平行实现")

    def test_execute_plan_runs_every_variant_in_contract_order(self):
        retention, seeds, edges = base_fixture()
        runs = ra.execute_plan(plan(retention, seeds, edges), retention,
                               seeds=seeds, edges=edges)
        self.assertEqual(list(ra.VARIANT_IDS), [r["variant_id"] for r in runs])
        for run in runs:
            self.assertEqual({"variant_id", "definition", "resolved", "rendered"}, set(run))

    def test_execute_variant_matches_the_plan_run(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        runs = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        single = ra.execute_variant(got, "no_edges", retention, seeds=seeds, edges=edges)
        self.assertEqual(canonical(by_id(runs, "no_edges")), canonical(single))

    def test_unknown_variant_is_rejected_loudly(self):
        retention, seeds, edges = base_fixture()
        with self.assertRaises(ValueError):
            ra.execute_variant(plan(retention, seeds, edges), "no_memory",
                               retention, seeds=seeds, edges=edges)

    def test_invalid_plans_are_rejected_loudly(self):
        retention, seeds, edges = base_fixture()
        for bad in (None, {}, {"plan_id": "plan_x"}):
            with self.assertRaises(ValueError):
                ra.execute_plan(bad, retention, seeds=seeds, edges=edges)

    def test_inputs_that_drift_from_the_plan_are_rejected(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        with self.assertRaises(ValueError):
            ra.execute_plan(got, retention, seeds=[], edges=edges)
        with self.assertRaises(ValueError):
            ra.execute_plan(got, retention, seeds=seeds, edges=[])

    def test_tampered_budgets_break_the_content_address(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        got["budgets"]["max_expanded"] = 999
        with self.assertRaises(ValueError):
            ra.execute_plan(got, retention, seeds=seeds, edges=edges)

    def test_tampered_variant_definitions_do_not_change_execution(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        baseline = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        tampered = copy.deepcopy(got)
        tampered["variants"][2]["changes"]["max_expanded"] = 999
        runs = ra.execute_plan(tampered, retention, seeds=seeds, edges=edges)
        self.assertEqual(canonical(baseline), canonical(runs),
                         "执行只认内容地址重建出的定义，不认被篡改的字段")

    def test_no_edges_keeps_only_the_seed_as_evidence(self):
        retention, seeds, edges = base_fixture()
        runs = ra.execute_plan(plan(retention, seeds, edges), retention,
                               seeds=seeds, edges=edges)
        full = by_id(runs, "full")["rendered"]
        ablated = by_id(runs, "no_edges")["rendered"]
        self.assertEqual(["s", "n1", "n2"], selected_ids(full))
        self.assertEqual(["s"], selected_ids(ablated), "去掉联想边只剩种子自身")
        self.assertEqual([], excluded_of(ablated, "invalidated"),
                         "无边可走时失效邻居计数也如实消失")

    def test_no_expansion_drops_every_attempt_loudly(self):
        retention, seeds, edges = base_fixture()
        runs = ra.execute_plan(plan(retention, seeds, edges), retention,
                               seeds=seeds, edges=edges)
        ablated = by_id(runs, "no_expansion")["rendered"]
        self.assertEqual(["s"], selected_ids(ablated))
        dropped = {x["reason_code"]: x["count"] for x in excluded_of(ablated, "dropped")}
        self.assertEqual(2, dropped.get("expansion_cap"), "禁一跳需要大声计数，不静默")
        self.assertEqual(1, {x["reason_code"]: x["count"]
                             for x in excluded_of(ablated, "invalidated")}["invalidated_neighbor"])

    def test_half_expansion_halves_the_real_r04_budget(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges, max_expanded=1)
        runs = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        full = by_id(runs, "full")["rendered"]
        half = by_id(runs, "half_expansion")["rendered"]
        self.assertEqual(["s", "n1"], selected_ids(full))
        self.assertEqual(["s"], selected_ids(half))
        full_cap = {x["reason_code"]: x["count"] for x in excluded_of(full, "dropped")}
        half_cap = {x["reason_code"]: x["count"] for x in excluded_of(half, "dropped")}
        self.assertEqual(1, full_cap["expansion_cap"])
        self.assertEqual(2, half_cap["expansion_cap"])

    def test_half_degree_halves_the_real_r04_degree_cap(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges, max_degree=1)
        runs = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        full = by_id(runs, "full")["rendered"]
        half = by_id(runs, "half_degree")["rendered"]
        self.assertEqual(["s", "n1"], selected_ids(full))
        self.assertEqual(["s"], selected_ids(half))
        half_cap = {x["reason_code"]: x["count"] for x in excluded_of(half, "dropped")}
        self.assertEqual(2, half_cap["degree_cap"])

    def test_half_render_drops_over_budget_loudly_with_budget_reason(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges, max_entries=2)
        runs = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        full = by_id(runs, "full")["rendered"]
        half = by_id(runs, "half_render")["rendered"]
        self.assertEqual(["s", "n1"], selected_ids(full))
        self.assertEqual(["s"], selected_ids(half))
        drops = excluded_of(half, "budget")
        self.assertEqual(["n1", "n2"], [x["object_id"] for x in drops])
        self.assertEqual({"budget"}, {x["reason_code"] for x in drops})

    def test_no_slot_check_stops_registering_missing_but_keeps_selection(self):
        g = item("g", sources=[], subject_ref="per_1", restatement="喜欢茶",
                 occurred_at=1000.0)
        retention = mr.retention_set([g], [], mode="ablate")
        got = plan(retention, [g], [])
        runs = ra.execute_plan(got, retention, seeds=[g], edges=[])
        full = by_id(runs, "full")["rendered"]
        unchecked = by_id(runs, "no_slot_check")["rendered"]
        self.assertEqual("partial", full["status"])
        self.assertEqual("gapped", full["manifest"]["selected"][0]["reason"])
        self.assertEqual(2, len(excluded_of(full, "missing")))
        self.assertEqual("sufficient", unchecked["status"])
        self.assertEqual("grounded", unchecked["manifest"]["selected"][0]["reason"])
        self.assertEqual([], excluded_of(unchecked, "missing"))
        self.assertEqual(["g"], selected_ids(unchecked), "关槽位核对不改变选择本身")

    def test_invalidated_objects_never_resurrect_in_any_variant(self):
        retention, seeds, edges = base_fixture()
        runs = ra.execute_plan(plan(retention, seeds, edges), retention,
                               seeds=seeds, edges=edges)
        for run in runs:
            self.assertNotIn("dead", selected_ids(run["rendered"]),
                             "invalidated 绝不复活：%s" % run["variant_id"])
            for scene in run["rendered"]["scenes"]:
                self.assertNotIn("dead", [e["object_id"] for e in scene["entries"]])

    def test_invalidated_pool_registration_beats_the_seed_copy(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        retention = mr.retention_set([dict(s, validity="invalidated")], ["e1"], mode="keep")
        got = plan(retention, [s], [])
        runs = ra.execute_plan(got, retention, seeds=[s], edges=[])
        for run in runs:
            self.assertEqual([], selected_ids(run["rendered"]), "失效单调：池说失效就不复活")
            self.assertEqual("invalidated_seed",
                             excluded_of(run["rendered"], "invalidated")[0]["reason_code"])

    def test_conflicting_objects_stay_excluded_and_degraded_stays_degraded(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        retention = mr.retention_set([s, n], ["e1", "e2"], mode="keep")
        edges = [edge("s", "n")]
        got = plan(retention, [s], edges)
        runs = ra.execute_plan(got, retention, seeds=[s], edges=edges)
        full = by_id(runs, "full")["rendered"]
        self.assertEqual("conflicting", full["status"])
        self.assertEqual([], selected_ids(full), "矛盾对象绝不入场景冒充充分证据")
        self.assertEqual({"explicit_negation_opposite"},
                         {x["reason_code"] for x in excluded_of(full, "conflicting")})
        ablated = by_id(runs, "no_edges")["rendered"]
        self.assertEqual("sufficient", ablated["status"], "去掉边后矛盾不再被裁决到")
        self.assertEqual(["s"], selected_ids(ablated))

    def test_degradation_is_carried_by_every_variant(self):
        retention, seeds, edges = base_fixture()
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        got = plan(retention, seeds, edges, degradation=degradation)
        runs = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        for run in runs:
            self.assertEqual("degraded", run["rendered"]["status"])
            self.assertEqual(degradation, run["rendered"]["degradation"])
        self.assertEqual(["s", "n1", "n2"], selected_ids(by_id(runs, "full")["rendered"]),
                         "降级不抹掉可用证据，只如实标状态")

    def test_empty_retention_degrades_to_not_found_everywhere(self):
        empty = mr.retention_set([], [], mode="keep")
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = plan(empty, [s], [edge("s", "n1")])
        runs = ra.execute_plan(got, empty, seeds=[s], edges=[edge("s", "n1")])
        for run in runs:
            self.assertEqual("not_found", run["rendered"]["status"])
            self.assertEqual([], selected_ids(run["rendered"]))
        self.assertIn({"object_id": "s", "reason_code": "seed_not_retained"},
                      got["seed_filter"])

    def test_seeds_outside_the_retention_set_do_not_run(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n1 = claim_item("n1", "喜欢咖啡", sources=["e2"])
        n2 = claim_item("n2", "喜欢可可", sources=["e3"])
        retention = mr.retention_set([s, n1, n2], ["e1"], mode="ablate")
        edges = [edge("s", "n1"), edge("s", "n2")]
        got = plan(retention, [s, n1], edges)
        runs = ra.execute_plan(got, retention, seeds=[s, n1], edges=edges)
        full = by_id(runs, "full")["rendered"]
        self.assertEqual(["n1"], selected_ids(full), "被消融的种子不许再跑")
        self.assertEqual(2, len(got["edge_filter"]), "指向被消融记忆的边一并登记剔除")

    def test_score_parts_do_not_affect_execution(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        first = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        for row in retention["retained_items"] + seeds:
            row["match"] = {"clues": [], "score_parts": {"lexical": 99.0}}
        second = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        self.assertEqual(canonical(first), canonical(second),
                         "改 score_parts 影响了输出，说明 R07 偷读了 R03 的分数")

    def test_the_inputs_are_not_mutated_by_execution(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        before = canonical([retention, seeds, edges, got])
        ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        self.assertEqual(before, canonical([retention, seeds, edges, got]))

    def test_execution_is_deterministic_across_reruns(self):
        retention, seeds, edges = base_fixture()
        got = plan(retention, seeds, edges)
        first = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        second = ra.execute_plan(got, retention, seeds=seeds, edges=edges)
        self.assertEqual(canonical(first), canonical(second))

    def test_every_status_stays_in_the_r05_contract(self):
        retention, seeds, edges = base_fixture()
        runs = ra.execute_plan(plan(retention, seeds, edges), retention,
                               seeds=seeds, edges=edges)
        for run in runs:
            self.assertIn(run["rendered"]["status"], rc.STATUSES)
            self.assertIn(run["resolved"]["status"], rc.STATUSES)


# ── 行为锁：差异报告（稳定排序、稳定 reason_code、sha16 引用）──────────


class DiffTests(unittest.TestCase):
    def test_identical_renders_have_empty_diffs(self):
        stub = render_stub([selected_row("a")], status="sufficient",
                           counts={"selected": 1})
        got = ra.diff_manifests(stub, stub)
        for section in ("selected", "excluded", "dropped"):
            self.assertEqual({"added": [], "removed": [], "changed": []}, got[section])
        self.assertEqual({"full": "sufficient", "variant": "sufficient"}, got["status"])
        self.assertEqual({"full": {"selected": 1}, "variant": {"selected": 1}},
                         got["counts"])

    def test_removed_selected_entries_carry_sha16_and_reason(self):
        full = render_stub([selected_row("a"), selected_row("b", sha16="b" * 16)])
        variant = render_stub([selected_row("a")])
        got = ra.diff_manifests(full, variant)
        self.assertEqual([], got["selected"]["added"])
        entry = got["selected"]["removed"][0]
        self.assertEqual("selected_removed", entry["diff_reason"])
        self.assertEqual("b", entry["object_id"])
        self.assertEqual("grounded", entry["reason_code"])
        self.assertEqual("b" * 16, entry["sha16"])
        self.assertEqual(selected_row("b", sha16="b" * 16), entry["entry"])

    def test_added_selected_entries_are_reported(self):
        full = render_stub([selected_row("a")])
        variant = render_stub([selected_row("a"), selected_row("c")])
        got = ra.diff_manifests(full, variant)
        self.assertEqual(["c"], [x["object_id"] for x in got["selected"]["added"]])
        self.assertEqual("selected_added", got["selected"]["added"][0]["diff_reason"])

    def test_changed_selected_entries_list_the_changed_fields(self):
        full = render_stub([selected_row("a", reason="gapped",
                                         missing=[{"slot": "root_source",
                                                   "reason_code": "root_source_missing"}])])
        variant = render_stub([selected_row("a", reason="grounded")])
        got = ra.diff_manifests(full, variant)
        entry = got["selected"]["changed"][0]
        self.assertEqual("selected_changed", entry["diff_reason"])
        self.assertEqual(["missing_slots", "reason"], entry["changed_fields"])
        self.assertEqual("gapped", entry["full"]["reason"])
        self.assertEqual("grounded", entry["variant"]["reason"])

    def test_excluded_diff_entries_carry_content_hash_references(self):
        gap = {"object_id": "a", "category": "missing",
               "reason_code": "root_source_missing", "slot": "root_source"}
        full = render_stub(excluded=[gap])
        variant = render_stub()
        got = ra.diff_manifests(full, variant)
        entry = got["excluded"]["removed"][0]
        self.assertEqual("excluded_removed", entry["diff_reason"])
        self.assertEqual("root_source_missing", entry["reason_code"])
        self.assertEqual(gap, entry["entry"])
        self.assertRegex(entry["ref_sha16"], r"^[0-9a-f]{16}$")
        compact = json.dumps(gap, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(mm.digest(compact)[:16], entry["ref_sha16"])

    def test_aggregate_count_changes_are_changed_not_removed_plus_added(self):
        full = render_stub(excluded=[{"object_id": "", "category": "invalidated",
                                      "reason_code": "invalidated_neighbor", "count": 2}])
        variant = render_stub(excluded=[{"object_id": "", "category": "invalidated",
                                         "reason_code": "invalidated_neighbor", "count": 1}])
        got = ra.diff_manifests(full, variant)
        self.assertEqual([], got["dropped"]["added"])
        self.assertEqual([], got["dropped"]["removed"])
        entry = got["dropped"]["changed"][0]
        self.assertEqual("dropped_changed", entry["diff_reason"])
        self.assertEqual(["count"], entry["changed_fields"])
        self.assertEqual(2, entry["full"]["count"])
        self.assertEqual(1, entry["variant"]["count"])
        self.assertRegex(entry["full_ref_sha16"], r"^[0-9a-f]{16}$")
        self.assertRegex(entry["variant_ref_sha16"], r"^[0-9a-f]{16}$")

    def test_the_sections_partition_the_exclusion_categories(self):
        rows = [{"object_id": "a", "category": "missing",
                 "reason_code": "root_source_missing", "slot": "root_source"},
                {"object_id": "b", "category": "conflicting",
                 "reason_code": "explicit_negation_opposite", "verdict": "contradiction",
                 "left_id": "b", "right_id": "c"},
                {"object_id": "", "category": "dropped",
                 "reason_code": "unknown_neighbor", "count": 1},
                {"object_id": "", "category": "invalidated",
                 "reason_code": "invalidated_seed", "count": 1},
                {"object_id": "d", "category": "budget",
                 "reason_code": "budget", "detail": "max_entries"}]
        got = ra.diff_manifests(render_stub(excluded=rows), render_stub())
        self.assertEqual({"missing", "conflicting"},
                         {x["category"] for x in got["excluded"]["removed"]})
        self.assertEqual({"dropped", "invalidated", "budget"},
                         {x["category"] for x in got["dropped"]["removed"]})
        self.assertEqual(5, len(got["excluded"]["removed"]) + len(got["dropped"]["removed"]),
                         "不重不漏：每条排除恰好落进一个段")

    def test_diff_order_is_stable_under_input_shuffle(self):
        full = render_stub([selected_row("a"), selected_row("b")],
                           excluded=[{"object_id": "a", "category": "missing",
                                      "reason_code": "root_source_missing",
                                      "slot": "root_source"},
                                     {"object_id": "", "category": "dropped",
                                      "reason_code": "unknown_neighbor", "count": 1}])
        variant = render_stub([selected_row("c"), selected_row("d")],
                              excluded=[{"object_id": "b", "category": "missing",
                                         "reason_code": "subject_ref_missing",
                                         "slot": "subject_ref"},
                                        {"object_id": "c", "category": "budget",
                                         "reason_code": "budget", "detail": "max_entries"}])
        baseline = ra.diff_manifests(full, variant)
        shuffled = copy.deepcopy(variant)
        shuffled["manifest"]["selected"].reverse()
        shuffled["manifest"]["excluded"].reverse()
        self.assertEqual(canonical(baseline), canonical(ra.diff_manifests(full, shuffled)))

    def test_diff_reason_codes_are_the_contract_ones(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges, max_entries=2)
        for diff in got["diffs"]:
            for section in ("selected", "excluded", "dropped"):
                for kind in ("added", "removed", "changed"):
                    for entry in diff[section][kind]:
                        self.assertIn(entry["diff_reason"], ra.DIFF_REASONS)

    def test_real_diffs_carry_the_upstream_reason_codes_verbatim(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges)
        no_edges = next(d for d in got["diffs"] if d["variant_id"] == "no_edges")
        self.assertEqual(["n1", "n2"],
                         [x["object_id"] for x in no_edges["selected"]["removed"]])
        invalidated = no_edges["dropped"]["removed"]
        self.assertEqual(["invalidated_neighbor"],
                         [x["reason_code"] for x in invalidated])
        no_expansion = next(d for d in got["diffs"] if d["variant_id"] == "no_expansion")
        self.assertEqual(["expansion_cap"],
                         [x["reason_code"] for x in no_expansion["dropped"]["added"]])

    def test_diff_is_deterministic_and_read_only(self):
        retention, seeds, edges = base_fixture()
        runs = ra.execute_plan(plan(retention, seeds, edges), retention,
                               seeds=seeds, edges=edges)
        full, variant = runs[0]["rendered"], runs[1]["rendered"]
        before = canonical([full, variant])
        first = ra.diff_manifests(full, variant)
        second = ra.diff_manifests(full, variant)
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(before, canonical([full, variant]))
        first["selected"]["removed"].append({"object_id": "fake"})
        self.assertEqual(before, canonical([full, variant]), "diff 输出必须深拷贝")

    def test_non_dict_renders_are_tolerated(self):
        got = ra.diff_manifests(None, None)
        self.assertEqual({"full": "", "variant": ""}, got["status"])
        for section in ("selected", "excluded", "dropped"):
            self.assertEqual({"added": [], "removed": [], "changed": []}, got[section])


# ── 行为锁：评审包（manual_review 口径、不打分、不落原文）──────────────


class PacketTests(unittest.TestCase):
    def test_the_packet_shape_is_the_contract_one(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges)
        self.assertEqual({"plan", "variants", "diffs", "review", "limitations"}, set(got))
        self.assertEqual(list(ra.VARIANT_IDS), [v["variant_id"] for v in got["variants"]])
        self.assertEqual(list(ra.VARIANT_IDS[1:]), [d["variant_id"] for d in got["diffs"]])
        for diff in got["diffs"]:
            self.assertEqual("full", diff["baseline"])
        self.assertEqual(ra.LIMITATIONS, got["limitations"])

    def test_the_review_block_keeps_the_v02_caliber(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges)
        self.assertEqual({"authorization": "manual_review", "organic": "not_observed",
                          "judgement": None, "notes": None}, got["review"])

    def test_variants_carry_manifests_not_scenes(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges)
        for variant in got["variants"]:
            self.assertEqual({"variant_id", "definition", "status", "manifest"},
                             set(variant))
            self.assertEqual({"status", "context", "selected", "excluded",
                              "degradation", "counts"}, set(variant["manifest"]))

    def test_the_packet_leaks_no_chat_text(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges, context={"logical_turn_id": "t1"})
        text = json.dumps(got, ensure_ascii=False)
        for raw in ("喜欢茶", "喜欢咖啡", "喜欢可可", "喜欢果汁"):
            self.assertNotIn(raw, text, "评审包只带 ID/sha16，不落聊天原文")
        self.assertEqual(set(), set(walk_keys(got)) & {"restatement", "quote"})
        # counts 里的 scenes/entries 是整数计数，不是场景正文；除此之外不许出现。
        for variant in got["variants"]:
            self.assertNotIn("scenes", variant)
            for key in ("scenes", "entries"):
                self.assertIsInstance(variant["manifest"]["counts"][key], int)

    def test_the_packet_fabricates_no_conclusion_or_score_fields(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges)
        self.assertEqual(set(), set(walk_keys(got)) &
                         {"score", "useful", "conclusion", "verdicts"})
        self.assertIsNone(got["review"]["judgement"], "结论永远留空给人判")

    def test_the_packet_is_json_serializable(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges,
                     semantic_hints={("s", "n1"): {"relation": "same", "source": "t"}})
        dumped = json.dumps(got, ensure_ascii=False)
        reloaded = json.loads(dumped)
        self.assertEqual(dumped, json.dumps(reloaded, ensure_ascii=False),
                         "评审包必须能无损 JSON 往返（limitations 元组落成列表）")
        self.assertEqual(list(ra.LIMITATIONS), reloaded["limitations"])

    def test_the_packet_is_deterministic_across_reruns(self):
        retention, seeds, edges = base_fixture()
        first = packet(retention, seeds, edges)
        second = packet(retention, seeds, edges)
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(first["plan"]["plan_id"], second["plan"]["plan_id"])

    def test_the_packet_is_isolated_from_inputs_and_mutation(self):
        retention, seeds, edges = base_fixture()
        context = {"logical_turn_id": "t1"}
        before = canonical([retention, seeds, edges, context])
        got = packet(retention, seeds, edges, context=context)
        got["plan"]["seed_ids"].append("fake")
        got["variants"][0]["manifest"]["selected"].append({"object_id": "fake"})
        got["diffs"][0]["selected"]["removed"].append({"object_id": "fake"})
        got["review"]["judgement"] = "关联有用"
        self.assertEqual(before, canonical([retention, seeds, edges, context]))
        fresh = packet(retention, seeds, edges, context=context)
        self.assertIsNone(fresh["review"]["judgement"])
        self.assertEqual([], [x for x in fresh["plan"]["seed_ids"] if x == "fake"])

    def test_empty_retention_packet_degrades_without_fabricating(self):
        empty = mr.retention_set([], [], mode="keep")
        got = packet(empty, [], [])
        for variant in got["variants"]:
            self.assertEqual("not_found", variant["status"])
            self.assertEqual([], variant["manifest"]["selected"])
        for diff in got["diffs"]:
            for section in ("selected", "excluded", "dropped"):
                self.assertEqual({"added": [], "removed": [], "changed": []},
                                 diff[section])
        self.assertEqual("manual_review", got["review"]["authorization"])
        self.assertEqual("not_observed", got["review"]["organic"])

    def test_degraded_empty_is_not_claimed_as_no_evidence(self):
        empty = mr.retention_set([], [], mode="keep")
        got = packet(empty, [], [],
                     degradation=[{"store": "cognition", "reason": "absent", "detail": ""}])
        for variant in got["variants"]:
            self.assertEqual("degraded", variant["status"])
            self.assertEqual([], variant["manifest"]["excluded"], "读不到不等于没有")

    def test_budget_drops_surface_in_the_packet_variant_manifest(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges, max_entries=2)
        half = next(v for v in got["variants"] if v["variant_id"] == "half_render")
        drops = [x for x in half["manifest"]["excluded"] if x["category"] == "budget"]
        self.assertEqual({"budget"}, {x["reason_code"] for x in drops})
        self.assertEqual(2, len(drops), "减半预算的超限要逐条大声登记")

    def test_the_packet_binds_the_retention_fingerprint(self):
        retention, seeds, edges = base_fixture()
        got = packet(retention, seeds, edges)
        self.assertEqual(retention["fingerprint"], got["plan"]["retention"]["fingerprint"])
        self.assertEqual(retention["retained"], got["plan"]["pool_ids"])


# ── 端到端：接真实 R01→R02→R03→V02→R07（测试可 import，被测模块不 import）──


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r07_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def ctx(self, **over):
        base = {"recipient_scope": "private:1001",
                "allowed_source_scopes": ["private:1001", "group:g1"],
                "as_of_time": NOW, "query_text": "阿甲喜欢什么",
                "recent_referents": ["阿甲"]}
        base.update(over)
        return ro.query_context(**base)

    def fact(self, fid, text, eid, ts=1000.0):
        return {"id": fid, "text": text, "ts": ts, "source_event_ids": [eid],
                "status": "active",
                "evidence_spans": [{"event_id": eid, "quote": text,
                                    "subject_ref": "per_1",
                                    "subject_basis": "source_actor"}]}

    def group_facts(self, entries):
        path = os.path.join(self.root, "memory", "group_facts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"groups": {"g1": {"entries": entries}}}, handle,
                      ensure_ascii=False)

    def chain(self, ctx=None, roots=(), mode="ablate", budget=2, **packet_kw):
        ctx = ctx if ctx is not None else self.ctx()
        fetched = ro.retrieve_evidence(ctx)
        edges = ae.project_edges(fetched["items"], fetched["degradation"])["edges"]
        recalled = rs.recall(fetched["items"], dict(ctx, retrieval_budget=budget))
        retention = mr.retention_set(fetched["items"], roots, mode=mode)
        got = ra.build_review_packet(retention, seeds=recalled["items"], edges=edges,
                                     now=NOW, degradation=fetched["degradation"],
                                     **packet_kw)
        return got, retention, fetched, recalled, edges

    def belief(self, proposition, quote, dedupe, ts=1000.0):
        eid = sm.record_event("private_message", session="private:1001",
                              actor_qq="1001", visibility="private",
                              payload={"text": quote}, ts=ts, dedupe=dedupe)
        bid = sm.add_belief(proposition, subject_qq="1001",
                            source="explicit_self_report", ts=ts,
                            spans=[{"event_id": eid, "quote": quote,
                                    "span_start": 0, "span_end": len(quote),
                                    "match_kind": "model_quote"}])
        return eid, bid


class IntegrationTests(StoreFixture):
    def test_an_empty_data_dir_reports_degraded_and_creates_nothing(self):
        before = snapshot(self.root)
        got, _retention, _fetched, _recalled, _edges = self.chain()
        for variant in got["variants"]:
            self.assertEqual("degraded", variant["status"])
            self.assertEqual([], variant["manifest"]["excluded"], "读不到不等于没有")
        self.assertEqual({"absent"}, {d["reason"] for d in got["plan"]["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "R01→…→V02→R07 只读链路顺手建了库/目录，就不再是只读")

    def test_real_group_facts_run_through_every_variant(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1"),
                          self.fact("f2", "喜欢咖啡", "ge2", ts=1001.0)])
        got, retention, fetched, _recalled, _edges = self.chain()
        full = got["variants"][0]
        self.assertEqual("full", full["variant_id"])
        self.assertEqual({"group:g1:f1", "group:g1:f2"},
                         {x["object_id"] for x in full["manifest"]["selected"]})
        self.assertEqual(sorted(x["object_id"] for x in fetched["items"]),
                         got["plan"]["pool_ids"])
        self.assertEqual(retention["fingerprint"], got["plan"]["retention"]["fingerprint"])
        self.assertNotIn("喜欢茶", json.dumps(got, ensure_ascii=False))

    def test_real_root_ablation_removes_the_derived_memory_traceably(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1"),
                          self.fact("f2", "喜欢咖啡", "ge2", ts=1001.0)])
        got, retention, _fetched, _recalled, _edges = self.chain(roots=["ge2"])
        self.assertEqual(["group:g1:f2"], retention["removed"])
        self.assertEqual("root_ablated", retention["removed_reasons"]["group:g1:f2"])
        full = got["variants"][0]
        self.assertEqual(["group:g1:f1"],
                         [x["object_id"] for x in full["manifest"]["selected"]])
        self.assertIn({"object_id": "group:g1:f2", "reason_code": "seed_not_retained"},
                      got["plan"]["seed_filter"])
        self.assertTrue(got["plan"]["edge_filter"], "指向被消融记忆的边要大声登记")
        for entry in got["plan"]["edge_filter"]:
            self.assertEqual("endpoint_not_retained", entry["reason_code"])
            self.assertIn("group:g1:f2", entry["endpoints"])

    def test_real_conflict_is_excluded_and_the_no_edges_variant_differs(self):
        # 只召回 f1 作种子（f2 更新但词法分低），矛盾是种子×邻居：去掉边才裁不到。
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1", ts=1001.0),
                          self.fact("f2", "讨厌冰可乐", "ge2")])
        got, _retention, _fetched, recalled, _edges = self.chain(budget=1)
        self.assertEqual(["group:g1:f1"], [x["object_id"] for x in recalled["items"]])
        full = got["variants"][0]
        # 其余库 absent → degraded 压过 conflicting（R05/R06 口径），但矛盾逐条列出。
        self.assertEqual("degraded", full["status"])
        self.assertEqual([], full["manifest"]["selected"])
        self.assertEqual({"explicit_negation_opposite"},
                         {x["reason_code"] for x in full["manifest"]["excluded"]
                          if x["category"] == "conflicting"})
        no_edges = next(v for v in got["variants"] if v["variant_id"] == "no_edges")
        self.assertEqual(["group:g1:f1"],
                         [x["object_id"] for x in no_edges["manifest"]["selected"]])
        diff = next(d for d in got["diffs"] if d["variant_id"] == "no_edges")
        self.assertEqual({"explicit_negation_opposite"},
                         {x["reason_code"] for x in diff["excluded"]["removed"]})
        self.assertEqual(["group:g1:f1"],
                         [x["object_id"] for x in diff["selected"]["added"]])

    def test_the_full_readonly_pipeline_rewrites_no_asset(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1")])
        self.belief("命题一", "我喜欢冰可乐", "r07:one")
        before = snapshot(self.root)
        self.chain()
        self.assertEqual(before, snapshot(self.root))

    def test_real_legacy_missing_slots_are_carried_then_ablated_by_no_slot_check(self):
        bid = sm.add_belief("某个旧认识", subject_qq="1001", is_legacy=True, ts=900.0)
        ctx = self.ctx(allowed_source_scopes=["private:1001", "group:g1", "cognition"])
        got, _retention, _fetched, _recalled, _edges = self.chain(ctx=ctx)
        full = got["variants"][0]
        codes = {x["reason_code"] for x in full["manifest"]["excluded"]
                 if x["category"] == "missing" and x["object_id"] == bid}
        self.assertLessEqual({"root_source_missing", "original_quote_missing",
                              "provenance_unverified"}, codes, "legacy 缺依据大声登记，不补造")
        diff = next(d for d in got["diffs"] if d["variant_id"] == "no_slot_check")
        removed = {(x["object_id"], x["reason_code"])
                   for x in diff["excluded"]["removed"]}
        self.assertLessEqual({(bid, "root_source_missing"),
                              (bid, "original_quote_missing")}, removed)

    def test_real_chain_is_deterministic_and_score_blind(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1"),
                          self.fact("f2", "喜欢咖啡", "ge2", ts=1001.0)])
        got, retention, fetched, recalled, edges = self.chain()
        again, _r2, _f2, _rec2, _e2 = self.chain()
        self.assertEqual(canonical(got), canonical(again))
        baseline = ra.build_review_packet(retention, seeds=recalled["items"], edges=edges,
                                          now=NOW, degradation=fetched["degradation"])
        self.assertEqual(canonical(got), canonical(baseline))
        for row in retention["retained_items"] + recalled["items"]:
            row.setdefault("match", {})["score_parts"] = {"lexical": 99.0}
        mutated = ra.build_review_packet(retention, seeds=recalled["items"], edges=edges,
                                         now=NOW, degradation=fetched["degradation"])
        self.assertEqual(canonical(mutated), canonical(baseline),
                         "改 score_parts 影响了评审包，说明 R07 偷读了 R03 的分数")
        self.assertEqual(got["plan"]["plan_id"], mutated["plan"]["plan_id"],
                         "分数不进内容地址：R07 不读 R03 的分数")


if __name__ == "__main__":
    unittest.main()