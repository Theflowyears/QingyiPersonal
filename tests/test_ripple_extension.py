# -*- coding: utf-8 -*-
"""R08 可选离线向量边与受限二跳：隔离契约 + 人工评审包的行为锁与 AST 静态锁。

行为锁覆盖验收判据：两个扩展开关默认全关（未启用变体如实 disabled，不假装跑过）；
计划内容寻址 plan_id='rx_'+digest[:14]（now 必填、全部输入内容绑定）；执行前重建
核对、篡改/漂移大声拒绝、执行只认重建定义；向量边逐条校验（缺 model_id/batch_id/
similarity 非数值有限/自环/缺 edge_id/类型不符/edge_id 冲突逐条大声拒绝）；similarity
只登记不排序不打分（改 similarity 路径顺序不变）；二跳链式 via 可追溯、防环 revisit、
跨跳同根源归并 same_root、invalidated 不复活、unknown_neighbor 承接 R04、预算大声
登记；二跳确实比一跳多扩出独立根源邻居且全部经真实 R04/R05/R06；差异报告稳定排序、
稳定 diff_reason、sha16 引用；评审包 judgement 恒 None、authorization=manual_review、
organic=not_observed、expression_control=False；同输入同输出、输入顺序无关、不改输入。

AST 锁钉死「复用而非重造」：import 面只许 copy/json/math + R04/R05/R06/A03；必须调
rp.expand_paths / rc.assess_expansion / rc.resolve_ripple / re6.render_evidence /
mm.digest；不 import R01/R02/R03/各库/bridge/self_model/llm_router/os/time/random 等；
无写操作、无开库、无 SQL、无路径解析、无墙钟随机、无打分机器、无第二套矛盾引擎；
rp.MAX_HOPS=1 不被改写；LIMITATIONS 如实声明默认全关与人工评审口径。

R08 是 manual_review：契约可执行 ≠ 真实实验已做。所有合成数据只出现在 tempfile
隔离目录或内存里，是测试夹具，不是自然样本；端到端接真实 R01→R02→R03→R08
（测试可以 import R01/R02/R03，被测模块 ripple_extension 本身不 import）。
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

import ripple_extension as rx
import ripple_evidence as re6
import ripple_conflicts as rc
import ripple_paths as rp
import claim_conflicts as cc
import memory_manifest as mm
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


def vector_edge(a, b, similarity=0.5, edge_id=None, model_id="vec_model_v1",
                batch_id="batch_0001", **over):
    """调用方离线预计算向量边的合成夹具（不是真实 embedding，本模块不算向量）。"""
    row = {"edge_id": edge_id or ("vec_%s_%s" % (a, b)), "type": "vector_similarity",
           "a": a, "a_kind": "belief", "b": b, "b_kind": "belief",
           "basis": {"reason": "vector_similarity", "similarity": similarity,
                     "model_id": model_id, "batch_id": batch_id}}
    row.update(over)
    return row


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def compact(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def chain_fixture():
    """s←e1 种子，n1←e2 一跳邻居，n2←e3 二跳独立根源邻居，dead←e4 已失效，
    ghost 池外端点。边 s-n1 / n1-n2 / n1-dead / n1-ghost：一跳只到 n1，二跳才到 n2。"""
    s = claim_item("s", "喜欢茶", sources=["e1"])
    n1 = claim_item("n1", "喜欢咖啡", sources=["e2"])
    n2 = claim_item("n2", "喜欢可可", sources=["e3"])
    dead = claim_item("dead", "喜欢果汁", sources=["e4"], validity="invalidated")
    pool = [s, n1, n2, dead]
    seeds = [s]
    edges = [edge("s", "n1"), edge("n1", "n2"), edge("n1", "dead"), edge("n1", "ghost")]
    return pool, seeds, edges


def root_fixture():
    """在 chain 之上加 n4←e1（与种子同根源）+ 边 n1-n4：二跳跨跳同根源归并夹具。"""
    pool, seeds, edges = chain_fixture()
    n4 = claim_item("n4", "茶转述", sources=["e1"])
    return pool + [n4], seeds, edges + [edge("n1", "n4")]


def plan(seeds, edges, pool, **kw):
    kw.setdefault("now", NOW)
    return rx.build_plan(seeds=seeds, edges=edges, items=pool, **kw)


def packet(seeds, edges, pool, **kw):
    kw.setdefault("now", NOW)
    return rx.build_review_packet(seeds=seeds, edges=edges, items=pool, **kw)


def run_by_id(runs, variant_id):
    return next(r for r in runs if r["variant_id"] == variant_id)


def variant_by_id(pkt, variant_id):
    return next(v for v in pkt["variants"] if v["variant_id"] == variant_id)


def diff_by_id(pkt, variant_id):
    return next(d for d in pkt["diffs"] if d["variant_id"] == variant_id)


def selected_ids(rendered):
    return [x["object_id"] for x in rendered["manifest"]["selected"]]


def excluded_of(rendered, category):
    return [x for x in rendered["manifest"]["excluded"] if x["category"] == category]


def dropped_counts(rendered):
    return {x["reason_code"]: x["count"] for x in excluded_of(rendered, "dropped")}


def path_of(resolved, oid):
    return next(p for p in resolved["paths"] if p["object_id"] == oid)


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


# ── AST 静态锁：复用 R04/R05/R06/A03，不长第二套引擎，真只读，MAX_HOPS 不改写 ──


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "ripple_extension.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_import_surface_is_copy_json_math_plus_r04_r05_r06_a03(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"copy", "json", "math", "ripple_conflicts",
                                        "ripple_paths", "ripple_evidence",
                                        "memory_manifest"}, "多出来的 import 面要解释")
        self.assertLessEqual({"ripple_conflicts", "ripple_paths", "ripple_evidence",
                              "memory_manifest"}, imported,
                             "R08 必须复用 R04/R05/R06/A03，不复制平行实现")

    def test_it_reuses_the_real_entry_points_and_a03_digest(self):
        calls = {(n.func.value.id, n.func.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name)}
        self.assertIn(("rp", "expand_paths"), calls, "二跳必须走 R04 真实扩散，不复制遍历")
        self.assertIn(("rc", "assess_expansion"), calls, "二跳合并后必须走 R05 真实裁决")
        self.assertIn(("rc", "resolve_ripple"), calls, "一跳基线必须走 R05 真实入口")
        self.assertIn(("re6", "render_evidence"), calls, "证据场景必须走 R06 真实渲染")
        self.assertIn(("mm", "digest"), calls, "内容寻址必须复用 A03 digest")
        self.assertIn(("rp", "_index"), calls, "池/种子索引必须复用 R04 _index")
        self.assertIn(("rp", "_roots"), calls, "根源读取必须复用 R04 _roots")

    def test_it_does_not_import_r01_r02_r03_or_any_store_or_bridge(self):
        banned = {"retrieval_objects", "recall_scoring", "association_edges",
                  "memory_retention", "self_model", "persona_store",
                  "group_memory_store", "knowledge_base", "vision_notes",
                  "sqlite3", "os", "time", "random", "datetime", "pathlib",
                  "claim_conflicts", "qq_openclaw_bridge", "llm_router",
                  "socket", "urllib", "requests", "http", "asyncio", "subprocess", "sys"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             "R08 应是纯函数，不该 import %s" % sorted(names & banned))

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
                                 "R08 不许自带家族词表 %s" % node.value)
        for token in ("_FAMILY_MARKERS", "_NEGATION_PREFIXES", "_PREFERENCE_POSITIVE",
                      "_PREFERENCE_NEGATIVE", "_FAMILY_LEADERS"):
            self.assertNotIn(token, self.source, "出现了 M04 的剥壳词表 %s" % token)

    def test_no_verdict_literal_is_fabricated(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, cc.VERDICTS,
                                 "R08 不裁决，不许出现 verdict 字面量 %s" % node.value)

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
                                 "融合打分是 R03，R08 不许读分数")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("score", "scoring", "rank", "fuse", "fusion"):
                    self.assertNotIn(word, node.name.lower(), "出现了打分函数 %s" % node.name)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                for word in ("score", "scoring", "rank", "fuse", "fusion"):
                    self.assertNotIn(word, name.lower(), "调用了打分函数 %s" % name)

    def test_it_does_not_call_the_other_upstream_entry_points(self):
        """只许走 R04 expand_paths / R05 assess_expansion+resolve_ripple / R06 render_evidence；
        其余上游（R01/R02/R03/V02/M04）由调用方给定，不重跑。"""
        wanted = {"retrieve_evidence", "fetch_retrieval_items", "project_edges",
                  "recall", "score_item", "read_all_candidates", "adjudicate",
                  "retention_set", "split_by_root"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name, wanted,
                                 "R08 不该调用上游入口 %s，输入由调用方传入" % name)

    def test_max_hops_is_not_rewritten(self):
        self.assertEqual(1, rp.MAX_HOPS, "R04 一跳硬上限不许被 R08 改写")
        self.assertNotIn("MAX_HOPS =", self.source, "不许给 MAX_HOPS 赋值")
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.assertNotEqual("MAX_HOPS", target.id)
                    if isinstance(target, ast.Attribute):
                        self.assertNotEqual("MAX_HOPS", target.attr)

    def test_no_expression_control_true_or_live_observation_flag(self):
        self.assertNotIn("'expression_control': True", self.source,
                         "隔离契约不许冒充接管表达")
        self.assertNotIn("counts_as_live_observation", self.source,
                         "出现了全局静态锁禁止的标记")

    def test_the_variant_and_type_constants(self):
        self.assertEqual(("one_hop", "one_hop_vector", "two_hop", "two_hop_vector"),
                         rx.VARIANT_IDS)
        self.assertEqual("one_hop", rx.VARIANT_IDS[0], "one_hop 必须是基线且排第一")
        self.assertEqual(len(set(rx.VARIANT_IDS)), len(rx.VARIANT_IDS))
        self.assertEqual("vector_similarity", rx.VECTOR_EDGE_TYPE)
        self.assertEqual("r08-1", rx.ALGORITHM_VERSION)
        self.assertEqual(2, rx.MAX_SECOND_DEGREE)
        self.assertEqual(8, rx.MAX_SECOND_EXPANDED)
        for knob in ("vector_edges", "second_hop", "max_expanded", "max_degree",
                     "max_second_degree", "max_second_expanded"):
            self.assertIn(knob, rx.KNOBS)

    def test_the_diff_vector_and_merge_reason_codes_are_fixed(self):
        self.assertEqual(("selected_added", "selected_removed", "selected_changed",
                          "excluded_added", "excluded_removed", "excluded_changed",
                          "dropped_added", "dropped_removed", "dropped_changed"),
                         rx.DIFF_REASONS)
        self.assertEqual(("malformed_vector_edge", "vector_type_mismatch", "self_loop",
                          "vector_edge_id_missing", "vector_provenance_missing",
                          "vector_edge_id_conflict"), rx.VECTOR_REASONS)
        self.assertEqual(("revisit", "same_root", "second_hop_seed_unavailable", "budget"),
                         rx.MERGE_REASONS)

    def test_the_review_caliber_matches_r07_v02(self):
        self.assertEqual("manual_review", rx.REVIEW_AUTHORIZATION)
        self.assertEqual("not_observed", rx.REVIEW_ORGANIC)

    def test_the_limitations_declare_the_undone_honestly(self):
        self.assertIsInstance(rx.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(rx.LIMITATIONS), 5)
        limits = " ".join(rx.LIMITATIONS)
        for undone in ("默认全关", "manual_review", "not_observed", "judgement",
                       "embedding", "LLM", "R03", "C04", "M04", "similarity",
                       "人工评审", "纯函数", "R04", "R05", "R06", "MAX_HOPS",
                       "expression_control"):
            self.assertIn(undone, limits, "limitations 必须如实声明 %s" % undone)

    def test_the_docstring_declares_executable_not_real_experiment(self):
        self.assertIn("默认全关", rx.__doc__)
        self.assertIn("契约可执行 ≠ 真实实验已做", rx.__doc__)
        self.assertIn("manual_review", rx.__doc__)
        self.assertIn("人工评审", rx.__doc__)
        self.assertIn("不算 embedding", rx.__doc__)
        self.assertIn("不发网络", rx.__doc__)


# ── 行为锁：扩展计划（内容寻址、默认全关、变体定义、内容绑定）──────────────


class PlanTests(unittest.TestCase):
    def test_plan_id_is_a_stable_content_address(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        self.assertRegex(got["plan_id"], r"^rx_[0-9a-f]{14}$")
        self.assertEqual(got["plan_id"], plan(seeds, edges, pool)["plan_id"])

    def test_plan_id_is_recomputable_from_the_core(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        core = {k: v for k, v in got.items() if k != "plan_id"}
        self.assertEqual("rx_" + mm.digest(compact(core))[:14], got["plan_id"])

    def test_now_is_required_and_loud(self):
        pool, seeds, edges = chain_fixture()
        for bad in (None, "昨天", True):
            with self.assertRaises(ValueError):
                plan(seeds, edges, pool, now=bad)

    def test_now_zero_is_a_valid_logical_time(self):
        pool, seeds, edges = chain_fixture()
        self.assertEqual(0, plan(seeds, edges, pool, now=0)["now"])

    def test_plan_id_changes_with_now(self):
        pool, seeds, edges = chain_fixture()
        self.assertNotEqual(plan(seeds, edges, pool, now=1)["plan_id"],
                            plan(seeds, edges, pool, now=2)["plan_id"])

    def test_plan_id_changes_with_input_content(self):
        pool, seeds, edges = chain_fixture()
        base = plan(seeds, edges, pool)["plan_id"]
        other_seed = [claim_item("s", "喜欢奶茶", sources=["e1"])]
        self.assertNotEqual(base, plan(other_seed, edges, pool)["plan_id"])
        other_pool = pool + [claim_item("extra", "喜欢奶茶", sources=["e9"])]
        self.assertNotEqual(base, plan(seeds, edges, other_pool)["plan_id"])
        self.assertNotEqual(base, plan(seeds, edges + [edge("s", "n2")], pool)["plan_id"])

    def test_plan_id_changes_with_vector_similarity_content(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2", similarity=0.9)]
        other = [vector_edge("s", "n2", similarity=0.1)]
        self.assertNotEqual(plan(seeds, edges, pool, vector_edges=vec)["plan_id"],
                            plan(seeds, edges, pool, vector_edges=other)["plan_id"])

    def test_plan_id_changes_with_switches_and_budgets(self):
        pool, seeds, edges = chain_fixture()
        base = plan(seeds, edges, pool)["plan_id"]
        self.assertNotEqual(base, plan(seeds, edges, pool, allow_vector_edges=True)["plan_id"])
        self.assertNotEqual(base, plan(seeds, edges, pool, allow_second_hop=True)["plan_id"])
        for knob in ("max_expanded", "max_degree", "max_second_degree", "max_second_expanded"):
            self.assertNotEqual(base, plan(seeds, edges, pool, **{knob: 1})["plan_id"])

    def test_allow_flags_must_be_real_bools(self):
        pool, seeds, edges = chain_fixture()
        for bad in (1, 0, "yes", None):
            with self.assertRaises(ValueError):
                plan(seeds, edges, pool, allow_vector_edges=bad)
            with self.assertRaises(ValueError):
                plan(seeds, edges, pool, allow_second_hop=bad)

    def test_the_variant_set_is_fixed_enumerable_and_defined(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        self.assertEqual(list(rx.VARIANT_IDS), [v["variant_id"] for v in got["variants"]])
        for variant in got["variants"]:
            self.assertEqual({"variant_id", "changes", "keeps", "enabled", "definition"},
                             set(variant))
            self.assertIsInstance(variant["definition"], str)
            self.assertTrue(variant["definition"])
            self.assertEqual([k for k in rx.KNOBS if k not in variant["changes"]],
                             variant["keeps"])
        self.assertEqual({}, got["variants"][0]["changes"], "one_hop 基线不改任何开关")
        self.assertTrue(got["variants"][0]["enabled"], "one_hop 基线恒执行")

    def test_disabled_variants_follow_the_switches(self):
        pool, seeds, edges = chain_fixture()
        cases = ((False, False, [True, False, False, False]),
                 (True, False, [True, True, False, False]),
                 (False, True, [True, False, True, False]),
                 (True, True, [True, True, True, True]))
        for av, ah, expected in cases:
            got = plan(seeds, edges, pool, allow_vector_edges=av, allow_second_hop=ah)
            self.assertEqual(expected, [v["enabled"] for v in got["variants"]],
                             "av=%s ah=%s" % (av, ah))

    def test_budgets_are_non_negative_and_echoed(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool, max_expanded=-3, max_degree=-1,
                   max_second_degree=-2, max_second_expanded=-5)
        self.assertEqual({"max_expanded": 0, "max_degree": 0,
                          "max_second_degree": 0, "max_second_expanded": 0}, got["budgets"])
        default = plan(seeds, edges, pool)["budgets"]
        self.assertEqual(rp.MAX_EXPANDED, default["max_expanded"])
        self.assertEqual(rp.MAX_DEGREE, default["max_degree"])
        self.assertEqual(2, default["max_second_degree"])
        self.assertEqual(8, default["max_second_expanded"])

    def test_unknown_required_slots_are_rejected_loudly(self):
        pool, seeds, edges = chain_fixture()
        with self.assertRaises(ValueError):
            plan(seeds, edges, pool, required_slots=("vibes",))

    def test_empty_required_slots_means_no_slot_check(self):
        pool, seeds, edges = chain_fixture()
        for slots in ((), None):
            self.assertEqual([], plan(seeds, edges, pool, required_slots=slots)["required_slots"])

    def test_vector_edges_are_bound_and_filtered_in_the_plan(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2"), vector_edge("s", "s")]
        got = plan(seeds, edges, pool, vector_edges=vec)
        self.assertEqual(["vec_s_n2"], got["vector_edge_ids"])
        self.assertEqual([{"edge_id": "vec_s_s", "reason_code": "self_loop"}],
                         got["vector_filter"])

    def test_bindings_are_content_hashes_of_normalized_inputs(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        index = rp._index(pool)
        normalized = [index[oid] for oid in sorted(index)]
        self.assertEqual(mm.digest(compact(normalized)), got["bindings"]["pool"])
        seed_index = rp._index(seeds)
        self.assertEqual(mm.digest(compact([seed_index[o] for o in sorted(seed_index)])),
                         got["bindings"]["seeds"])
        for value in got["bindings"].values():
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_input_order_does_not_change_the_plan(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2"), vector_edge("s", "n4")]
        base = plan(seeds, edges, pool, vector_edges=vec)
        shuffled = plan(list(reversed(seeds)), list(reversed(edges)), list(reversed(pool)),
                        vector_edges=list(reversed(vec)))
        self.assertEqual(base["plan_id"], shuffled["plan_id"])
        self.assertEqual(canonical(base), canonical(shuffled))

    def test_the_inputs_are_not_mutated_by_planning(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        before = canonical([pool, seeds, edges, vec])
        plan(seeds, edges, pool, vector_edges=vec)
        self.assertEqual(before, canonical([pool, seeds, edges, vec]))

    def test_the_plan_carries_no_chat_text(self):
        pool, seeds, edges = chain_fixture()
        text = canonical(plan(seeds, edges, pool, vector_edges=[vector_edge("s", "n2")]))
        for raw in ("喜欢茶", "喜欢咖啡", "喜欢可可", "喜欢果汁"):
            self.assertNotIn(raw, text, "计划只登记 ID 与哈希，不落聊天原文")

    def test_degradation_and_hints_are_carried_deterministically(self):
        pool, seeds, edges = chain_fixture()
        degradation = [{"store": "group", "reason": "absent", "detail": ""},
                       {"store": "cognition", "reason": "unreadable", "detail": ""}]
        hints = {("s", "n1"): {"relation": "same", "source": "unit_test"}}
        got = plan(seeds, edges, pool, degradation=degradation, semantic_hints=hints)
        self.assertEqual(sorted(degradation, key=compact), got["degradation"])
        self.assertEqual([["s", "n1", {"relation": "same", "source": "unit_test"}]],
                         got["semantic_hints"])
        shuffled = plan(seeds, edges, pool, degradation=list(reversed(degradation)),
                        semantic_hints=hints)
        self.assertEqual(got["plan_id"], shuffled["plan_id"])

    def test_empty_inputs_still_build_a_valid_plan(self):
        got = plan([], [], [])
        self.assertEqual([], got["pool_ids"])
        self.assertEqual([], got["seed_ids"])
        self.assertRegex(got["plan_id"], r"^rx_[0-9a-f]{14}$")

    def test_non_dict_rows_are_tolerated_as_empty(self):
        got = plan([None, 42], [None], [None, 42])
        self.assertEqual([], got["pool_ids"])
        self.assertEqual([], got["seed_ids"])


# ── 行为锁：向量边校验（逐条大声拒绝，similarity 只登记不排序）──────────────


class VectorValidationTests(unittest.TestCase):
    def codes(self, vec, **kw):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool, vector_edges=vec, **kw)
        return got["vector_filter"], got["vector_edge_ids"]

    def test_a_valid_vector_edge_is_kept(self):
        filt, ids = self.codes([vector_edge("s", "n2")])
        self.assertEqual([], filt)
        self.assertEqual(["vec_s_n2"], ids)

    def test_missing_model_id_is_rejected_loudly(self):
        filt, ids = self.codes([vector_edge("s", "n2", model_id="")])
        self.assertEqual([{"edge_id": "vec_s_n2", "reason_code": "vector_provenance_missing"}], filt)
        self.assertEqual([], ids)

    def test_missing_batch_id_is_rejected_loudly(self):
        filt, _ = self.codes([vector_edge("s", "n2", batch_id="")])
        self.assertEqual("vector_provenance_missing", filt[0]["reason_code"])

    def test_non_numeric_or_non_finite_similarity_is_rejected(self):
        for bad in ("0.9", None, float("nan"), float("inf"), float("-inf"), True, 10 ** 400):
            filt, ids = self.codes([vector_edge("s", "n2", similarity=bad)])
            self.assertEqual("vector_provenance_missing", filt[0]["reason_code"], repr(bad))
            self.assertEqual([], ids)

    def test_missing_basis_is_rejected(self):
        vec = vector_edge("s", "n2")
        del vec["basis"]
        filt, _ = self.codes([vec])
        self.assertEqual("vector_provenance_missing", filt[0]["reason_code"])

    def test_self_loop_is_rejected_loudly(self):
        filt, _ = self.codes([vector_edge("s", "s")])
        self.assertEqual([{"edge_id": "vec_s_s", "reason_code": "self_loop"}], filt)

    def test_missing_edge_id_is_rejected_loudly(self):
        vec = vector_edge("s", "n2")
        vec["edge_id"] = ""
        filt, _ = self.codes([vec])
        self.assertEqual([{"edge_id": "", "reason_code": "vector_edge_id_missing"}], filt)

    def test_non_dict_vector_edges_are_rejected_loudly(self):
        filt, _ = self.codes([None, 42])
        self.assertEqual([{"edge_id": "", "reason_code": "malformed_vector_edge"}] * 2, filt)

    def test_missing_endpoint_is_rejected_loudly(self):
        filt, _ = self.codes([vector_edge("", "n2")])
        self.assertEqual("malformed_vector_edge", filt[0]["reason_code"])

    def test_wrong_type_is_rejected_loudly(self):
        filt, _ = self.codes([vector_edge("s", "n2", type="shared_entity")])
        self.assertEqual([{"edge_id": "vec_s_n2", "reason_code": "vector_type_mismatch"}], filt)

    def test_edge_id_conflict_with_a_real_edge_is_rejected(self):
        pool, seeds, edges = chain_fixture()
        real_id = edges[0]["edge_id"]
        filt, ids = self.codes([vector_edge("s", "n2", edge_id=real_id)])
        self.assertEqual([{"edge_id": real_id, "reason_code": "vector_edge_id_conflict"}], filt)
        self.assertEqual([], ids)

    def test_duplicate_vector_edge_id_keeps_one_and_registers_the_other(self):
        filt, ids = self.codes([vector_edge("s", "n2", similarity=0.9),
                                vector_edge("s", "n2", similarity=0.1)])
        self.assertEqual(["vec_s_n2"], ids)
        self.assertEqual([{"edge_id": "vec_s_n2", "reason_code": "vector_edge_id_conflict"}], filt)

    def test_all_filter_codes_are_the_contract_ones(self):
        pool, seeds, edges = chain_fixture()
        vec = [None, vector_edge("s", "s"), vector_edge("s", "n2", model_id=""),
               vector_edge("s", "n2", edge_id=""), vector_edge("s", "n2", type="x")]
        got = plan(seeds, edges, pool, vector_edges=vec)
        for entry in got["vector_filter"]:
            self.assertIn(entry["reason_code"], rx.VECTOR_REASONS)

    def test_bad_vector_edges_never_reach_traversal(self):
        pool, seeds, edges = chain_fixture()
        bad = [vector_edge("s", "n2", model_id=""), vector_edge("s", "s")]
        base = rx.execute(plan(seeds, edges, pool), seeds=seeds, edges=edges, items=pool)
        got = rx.execute(plan(seeds, edges, pool, vector_edges=bad, allow_vector_edges=True),
                         seeds=seeds, edges=edges, items=pool, vector_edges=bad)
        self.assertEqual(canonical(run_by_id(base, "one_hop")["resolved"]),
                         canonical(run_by_id(got, "one_hop_vector")["resolved"]),
                         "坏向量边被剔除后，向量变体应与一跳基线完全一致")

    def test_similarity_does_not_affect_path_order(self):
        pool, seeds, edges = chain_fixture()
        n5 = claim_item("n5", "喜欢奶茶", sources=["e5"])
        pool = pool + [n5]
        high = [vector_edge("s", "n2", similarity=0.9), vector_edge("s", "n5", similarity=0.1)]
        low = [vector_edge("s", "n2", similarity=0.1), vector_edge("s", "n5", similarity=0.9)]
        runs_a = rx.execute(plan(seeds, edges, pool, vector_edges=high, allow_vector_edges=True),
                            seeds=seeds, edges=edges, items=pool, vector_edges=high)
        runs_b = rx.execute(plan(seeds, edges, pool, vector_edges=low, allow_vector_edges=True),
                            seeds=seeds, edges=edges, items=pool, vector_edges=low)
        pa = run_by_id(runs_a, "one_hop_vector")["resolved"]["paths"]
        pb = run_by_id(runs_b, "one_hop_vector")["resolved"]["paths"]
        self.assertEqual([p["object_id"] for p in pa], [p["object_id"] for p in pb])
        self.assertEqual([p["path_id"] for p in pa], [p["path_id"] for p in pb],
                         "改 similarity 改变了路径顺序/ID，说明 similarity 被拿去排序了")

    def test_similarity_is_registered_in_basis_for_traceability(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2", similarity=0.87, model_id="m9", batch_id="b9")]
        runs = rx.execute(plan(seeds, edges, pool, vector_edges=vec, allow_vector_edges=True),
                          seeds=seeds, edges=edges, items=pool, vector_edges=vec)
        basis = path_of(run_by_id(runs, "one_hop_vector")["resolved"], "n2")["basis"]
        self.assertEqual(0.87, basis["similarity"])
        self.assertEqual("m9", basis["model_id"])
        self.assertEqual("b9", basis["batch_id"])

    def test_validation_runs_even_when_the_switch_is_off(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2"), vector_edge("s", "s")]
        got = plan(seeds, edges, pool, vector_edges=vec)
        self.assertEqual(["vec_s_n2"], got["vector_edge_ids"])
        self.assertEqual(1, len(got["vector_filter"]))
        self.assertFalse(got["variants"][1]["enabled"], "开关关着向量变体仍 disabled")


# ── 行为锁：执行（默认全关、重建核对、篡改拒绝、真实链路）──────────────────


class ExecutionTests(unittest.TestCase):
    def test_one_hop_baseline_equals_the_real_r05_r06_pipeline(self):
        pool, seeds, edges = chain_fixture()
        hints = {("s", "n1"): {"relation": "same", "source": "unit_test"}}
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        got = plan(seeds, edges, pool, semantic_hints=hints, degradation=degradation)
        runs = rx.execute(got, seeds=seeds, edges=edges, items=pool,
                          degradation=degradation, semantic_hints=hints)
        run = run_by_id(runs, "one_hop")
        resolved = rc.resolve_ripple(seeds, edges, items=pool, degradation=degradation,
                                     required_slots=rc.SLOTS, semantic_hints=hints)
        rendered = re6.render_evidence(resolved, items=pool, seeds=seeds,
                                       context={"plan_id": got["plan_id"],
                                                "variant_id": "one_hop", "now": NOW})
        self.assertEqual(canonical(resolved), canonical(run["resolved"]))
        self.assertEqual(canonical(rendered), canonical(run["rendered"]))

    def test_one_hop_vector_equals_the_real_pipeline_with_merged_edges(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        got = plan(seeds, edges, pool, vector_edges=vec, allow_vector_edges=True)
        runs = rx.execute(got, seeds=seeds, edges=edges, items=pool, vector_edges=vec)
        run = run_by_id(runs, "one_hop_vector")
        resolved = rc.resolve_ripple(seeds, edges + vec, items=pool, required_slots=rc.SLOTS)
        self.assertEqual(canonical(resolved), canonical(run["resolved"]))
        self.assertEqual(["s", "n1", "n2"], selected_ids(run["rendered"]))

    def test_default_switches_execute_only_the_baseline(self):
        pool, seeds, edges = chain_fixture()
        runs = rx.execute(plan(seeds, edges, pool), seeds=seeds, edges=edges, items=pool)
        states = {r["variant_id"]: r["state"] for r in runs}
        self.assertEqual({"one_hop": "executed", "one_hop_vector": "disabled",
                          "two_hop": "disabled", "two_hop_vector": "disabled"}, states)
        for r in runs[1:]:
            self.assertNotIn("resolved", r, "disabled 变体不许假装跑过")
            self.assertNotIn("rendered", r)

    def test_switches_select_which_variants_execute(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        cases = ((True, False, {"one_hop", "one_hop_vector"}),
                 (False, True, {"one_hop", "two_hop"}),
                 (True, True, set(rx.VARIANT_IDS)))
        for av, ah, expected in cases:
            runs = rx.execute(plan(seeds, edges, pool, vector_edges=vec,
                                   allow_vector_edges=av, allow_second_hop=ah),
                              seeds=seeds, edges=edges, items=pool, vector_edges=vec)
            self.assertEqual(expected, {r["variant_id"] for r in runs if r["state"] == "executed"})

    def test_execute_runs_every_variant_in_contract_order(self):
        pool, seeds, edges = chain_fixture()
        runs = rx.execute(plan(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True),
                          seeds=seeds, edges=edges, items=pool)
        self.assertEqual(list(rx.VARIANT_IDS), [r["variant_id"] for r in runs])

    def test_invalid_plans_are_rejected_loudly(self):
        pool, seeds, edges = chain_fixture()
        for bad in (None, {}, {"plan_id": "rx_x"}):
            with self.assertRaises(ValueError):
                rx.execute(bad, seeds=seeds, edges=edges, items=pool)

    def test_tampered_plan_id_is_rejected(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        got["plan_id"] = "rx_00000000000000"
        with self.assertRaises(ValueError):
            rx.execute(got, seeds=seeds, edges=edges, items=pool)

    def test_tampered_budgets_break_the_content_address(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        got["budgets"]["max_second_expanded"] = 999
        with self.assertRaises(ValueError):
            rx.execute(got, seeds=seeds, edges=edges, items=pool)

    def test_tampered_budget_keys_are_rejected(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        got["budgets"]["extra"] = 1
        with self.assertRaises(ValueError):
            rx.execute(got, seeds=seeds, edges=edges, items=pool)

    def test_tampered_switch_is_rejected(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool)
        got["allow_second_hop"] = True
        with self.assertRaises(ValueError):
            rx.execute(got, seeds=seeds, edges=edges, items=pool)

    def test_inputs_that_drift_from_the_plan_are_rejected(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        got = plan(seeds, edges, pool, vector_edges=vec)
        kwargs = dict(seeds=seeds, edges=edges, items=pool, vector_edges=vec)
        for drift in ({"seeds": []}, {"edges": []}, {"items": []},
                      {"vector_edges": None},
                      {"degradation": [{"store": "x", "reason": "absent"}]},
                      {"semantic_hints": {("s", "n1"): {"relation": "same"}}}):
            bad = dict(kwargs)
            bad.update(drift)
            with self.assertRaises(ValueError):
                rx.execute(got, **bad)

    def test_tampered_variant_definitions_do_not_change_execution(self):
        pool, seeds, edges = chain_fixture()
        got = plan(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True)
        kwargs = dict(seeds=seeds, edges=edges, items=pool)
        baseline = rx.execute(got, **kwargs)
        tampered = copy.deepcopy(got)
        tampered["variants"][2]["changes"]["second_hop"] = False
        tampered["variants"][1]["enabled"] = False
        tampered["variants"][0]["definition"] = "hacked"
        runs = rx.execute(tampered, **kwargs)
        self.assertEqual(canonical(baseline), canonical(runs),
                         "执行只认内容地址重建出的定义，不认被篡改的字段")

    def test_execution_is_deterministic_and_order_independent(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        kw = dict(allow_vector_edges=True, allow_second_hop=True, vector_edges=vec)
        got = plan(seeds, edges, pool, **kw)
        first = rx.execute(got, seeds=seeds, edges=edges, items=pool, vector_edges=vec)
        second = rx.execute(got, seeds=seeds, edges=edges, items=pool, vector_edges=vec)
        self.assertEqual(canonical(first), canonical(second))
        shuffled = rx.execute(got, seeds=list(reversed(seeds)), edges=list(reversed(edges)),
                              items=list(reversed(pool)), vector_edges=list(reversed(vec)))
        self.assertEqual(canonical(first), canonical(shuffled))

    def test_the_inputs_are_not_mutated_by_execution(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        got = plan(seeds, edges, pool, vector_edges=vec, allow_vector_edges=True,
                   allow_second_hop=True)
        before = canonical([pool, seeds, edges, vec, got])
        rx.execute(got, seeds=seeds, edges=edges, items=pool, vector_edges=vec)
        self.assertEqual(before, canonical([pool, seeds, edges, vec, got]))

    def test_degradation_is_carried_by_every_executed_variant(self):
        pool, seeds, edges = chain_fixture()
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        got = plan(seeds, edges, pool, degradation=degradation, allow_second_hop=True)
        runs = rx.execute(got, seeds=seeds, edges=edges, items=pool, degradation=degradation)
        for run in runs:
            if run["state"] != "executed":
                continue
            self.assertEqual("degraded", run["rendered"]["status"])
            self.assertEqual(degradation, run["rendered"]["degradation"])

    def test_empty_inputs_degrade_to_not_found_without_crashing(self):
        got = plan([], [], [], allow_second_hop=True, allow_vector_edges=True)
        runs = rx.execute(got, seeds=[], edges=[], items=[])
        for run in runs:
            self.assertEqual("not_found", run["rendered"]["status"])
            self.assertEqual([], selected_ids(run["rendered"]))

    def test_score_parts_do_not_affect_selection(self):
        pool, seeds, edges = chain_fixture()
        kw = dict(allow_second_hop=True)
        base = rx.execute(plan(seeds, edges, pool, **kw), seeds=seeds, edges=edges, items=pool)
        for row in pool + seeds:
            row["match"] = {"clues": [], "score_parts": {"lexical": 99.0}}
        mutated_plan = plan(seeds, edges, pool, **kw)
        mutated = rx.execute(mutated_plan, seeds=seeds, edges=edges, items=pool)
        for vid in ("one_hop", "two_hop"):
            self.assertEqual(canonical(run_by_id(base, vid)["resolved"]),
                             canonical(run_by_id(mutated, vid)["resolved"]),
                             "改 score_parts 影响了裁决输出，说明 R08 偷读了 R03 的分数")
            self.assertEqual(selected_ids(run_by_id(base, vid)["rendered"]),
                             selected_ids(run_by_id(mutated, vid)["rendered"]))

    def test_every_status_stays_in_the_r05_contract(self):
        pool, seeds, edges = chain_fixture()
        runs = rx.execute(plan(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True),
                          seeds=seeds, edges=edges, items=pool)
        for run in runs:
            if run["state"] == "executed":
                self.assertIn(run["rendered"]["status"], rc.STATUSES)
                self.assertIn(run["resolved"]["status"], rc.STATUSES)

    def test_conflicting_objects_stay_excluded_in_every_variant(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n1 = claim_item("n1", "讨厌冰可乐", sources=["e2"])
        pool, seeds, edges = [s, n1], [s], [edge("s", "n1")]
        runs = rx.execute(plan(seeds, edges, pool, allow_second_hop=True),
                          seeds=seeds, edges=edges, items=pool)
        for run in runs:
            if run["state"] != "executed":
                continue
            self.assertEqual("conflicting", run["rendered"]["status"])
            self.assertEqual([], selected_ids(run["rendered"]))
            self.assertEqual({"explicit_negation_opposite"},
                             {x["reason_code"] for x in excluded_of(run["rendered"], "conflicting")})

    def test_invalidated_objects_never_resurrect_in_any_variant(self):
        pool, seeds, edges = chain_fixture()
        runs = rx.execute(plan(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True,
                               vector_edges=[vector_edge("n1", "dead")]),
                          seeds=seeds, edges=edges, items=pool,
                          vector_edges=[vector_edge("n1", "dead")])
        for run in runs:
            if run["state"] != "executed":
                continue
            self.assertNotIn("dead", selected_ids(run["rendered"]))
            self.assertNotIn("dead", [p["object_id"] for p in run["resolved"]["paths"]])
            for scene in run["rendered"]["scenes"]:
                self.assertNotIn("dead", [e["object_id"] for e in scene["entries"]])

    def test_invalidated_pool_registration_beats_the_seed_copy(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        pool, seeds, edges = [dict(s, validity="invalidated")], [s], []
        runs = rx.execute(plan(seeds, edges, pool), seeds=seeds, edges=edges, items=pool)
        run = run_by_id(runs, "one_hop")
        self.assertEqual([], selected_ids(run["rendered"]))
        self.assertEqual("invalidated_seed",
                         excluded_of(run["rendered"], "invalidated")[0]["reason_code"])


# ── 行为锁：受限二跳（via 可追溯、防环、跨跳去重、预算、真实 R04/R05/R06）────


class TwoHopTests(unittest.TestCase):
    def runs(self, pool, seeds, edges, **kw):
        kw.setdefault("allow_second_hop", True)
        got = plan(seeds, edges, pool, **kw)
        return rx.execute(got, seeds=seeds, edges=edges, items=pool,
                          vector_edges=kw.get("vector_edges"))

    def test_two_hop_expands_an_independent_root_neighbor_beyond_one_hop(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        one = run_by_id(runs, "one_hop")["rendered"]
        two = run_by_id(runs, "two_hop")["rendered"]
        self.assertEqual(["s", "n1"], selected_ids(one))
        self.assertEqual(["s", "n1", "n2"], selected_ids(two),
                         "二跳应多扩出独立根源 e3 的邻居 n2")
        self.assertEqual(["e3"], path_of(run_by_id(runs, "two_hop")["resolved"], "n2")["root_source_ids"])

    def test_second_hop_paths_carry_traceable_via_and_hops(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        first = rp.expand_paths(seeds, edges, items=pool)
        first_pid = path_of(first, "n1")["path_id"]
        p2 = path_of(resolved, "n2")
        self.assertEqual(2, p2["hops"])
        self.assertEqual({"first_hop_path_id": first_pid, "intermediate_id": "n1"}, p2["via"])
        self.assertEqual(1, path_of(resolved, "n1")["hops"], "第一跳路径仍是 hops=1")

    def test_second_hop_path_id_is_content_addressed_including_via(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        first = rp.expand_paths(seeds, edges, items=pool)
        first_pid = path_of(first, "n1")["path_id"]
        second = rp.expand_paths([rp._index(pool)["n1"]], edges, items=pool,
                                 max_degree=rx.MAX_SECOND_DEGREE,
                                 max_expanded=rx.MAX_SECOND_EXPANDED)
        base_pid = path_of(second, "n2")["path_id"]
        expected = "path_" + mm.digest(compact([base_pid, first_pid, "n1"]))[:14]
        self.assertEqual(expected, path_of(resolved, "n2")["path_id"])

    def test_revisit_prevents_loops_back_to_seeds_and_first_hop(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        second_oids = [p["object_id"] for p in resolved["paths"] if p["hops"] == 2]
        self.assertNotIn("s", second_oids, "二跳不许把种子再扩一遍（防环）")
        self.assertNotIn("n1", second_oids, "二跳不许把第一跳邻居再扩一遍")
        self.assertGreaterEqual(resolved["provenance"]["dropped"].get("revisit", 0), 1)
        revisit = [d for d in resolved["provenance"]["deduped"] if d["reason"] == "revisit"]
        self.assertTrue(revisit)
        self.assertIn("via", revisit[0], "revisit 登记也要带链式出处")

    def test_cross_hop_same_root_is_merged_not_independent_evidence(self):
        pool, seeds, edges = root_fixture()
        runs = self.runs(pool, seeds, edges)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        self.assertNotIn("n4", [p["object_id"] for p in resolved["paths"]],
                         "与种子同根源 e1 的 n4 不算独立证据（§5.4）")
        self.assertNotIn("n4", selected_ids(run_by_id(runs, "two_hop")["rendered"]))
        merged = [d for d in resolved["provenance"]["deduped"]
                  if d["reason"] == "same_root" and d["object_id"] == "n4"]
        self.assertTrue(merged)
        self.assertEqual("s", merged[0]["merged_into"])
        self.assertEqual(["e1"], merged[0]["root_source_ids"])
        self.assertIn("via", merged[0])

    def test_invalidated_second_hop_neighbor_never_resurrects(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        self.assertNotIn("dead", [p["object_id"] for p in resolved["paths"]])
        self.assertGreaterEqual(resolved["provenance"]["dropped"].get("invalidated_neighbor", 0), 1)

    def test_unknown_neighbor_semantics_are_carried_from_r04(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        run = run_by_id(runs, "two_hop")
        self.assertGreaterEqual(run["resolved"]["provenance"]["dropped"].get("unknown_neighbor", 0), 1)
        self.assertEqual("partial", run["rendered"]["status"])
        self.assertIn("unknown_neighbor", dropped_counts(run["rendered"]))

    def test_second_hop_seed_without_pool_detail_is_registered(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        first = {"status": "sufficient",
                 "paths": [{"path_id": "path_fake", "object_id": "ghost", "seed_id": "s",
                            "root_source_ids": ["e9"]}],
                 "degradation": [],
                 "provenance": {"candidates": 1, "deduped": [], "dropped": {},
                                "budget": {}, "pool_size": 1, "edge_count": 1}}
        budgets = {"max_expanded": 32, "max_degree": 4,
                   "max_second_degree": 2, "max_second_expanded": 8}
        merged = rx._merge_second_hop(first, [s], [], [s], budgets)
        self.assertEqual(1, merged["provenance"]["dropped"]["second_hop_seed_unavailable"])
        self.assertEqual(["ghost"], merged["provenance"]["second_hop"]["unavailable"])
        self.assertEqual([], merged["provenance"]["second_hop"]["seed_ids"])

    def test_second_hop_budget_is_loud(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges, max_second_expanded=1)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        self.assertGreaterEqual(resolved["provenance"]["dropped"].get("expansion_cap", 0), 1)
        self.assertEqual(["s", "n1", "n2"], selected_ids(run_by_id(runs, "two_hop")["rendered"]))

    def test_zero_second_hop_budget_keeps_only_the_first_hop(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges, max_second_expanded=0)
        one = run_by_id(runs, "one_hop")["rendered"]
        two = run_by_id(runs, "two_hop")["rendered"]
        self.assertEqual(selected_ids(one), selected_ids(two))
        self.assertGreaterEqual(run_by_id(runs, "two_hop")["resolved"]["provenance"]["dropped"].get("expansion_cap", 0), 1)

    def test_second_hop_degree_cap_is_loud(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges, max_second_degree=0)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        self.assertGreaterEqual(resolved["provenance"]["dropped"].get("degree_cap", 0), 1)

    def test_global_budget_is_echoed_and_bounded(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges, max_expanded=5, max_second_expanded=3)
        budget = run_by_id(runs, "two_hop")["resolved"]["provenance"]["budget"]
        self.assertEqual(8, budget["global_expanded"])
        self.assertEqual(5, budget["max_expanded"])
        self.assertEqual(3, budget["max_second_expanded"])

    def test_two_hop_vector_combines_both_extensions(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        runs = self.runs(pool, seeds, edges, allow_vector_edges=True, vector_edges=vec)
        run = run_by_id(runs, "two_hop_vector")
        self.assertEqual("executed", run["state"])
        self.assertIn("n2", selected_ids(run["rendered"]))

    def test_multiple_first_hop_neighbors_keep_correct_via_chains(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        a = claim_item("a", "喜欢咖啡", sources=["e2"])
        b = claim_item("b", "喜欢可可", sources=["e3"])
        na = claim_item("na", "喜欢奶茶", sources=["e4"])
        nb = claim_item("nb", "喜欢果汁", sources=["e5"])
        pool, seeds = [s, a, b, na, nb], [s]
        edges = [edge("s", "a"), edge("s", "b"), edge("a", "na"), edge("b", "nb")]
        runs = self.runs(pool, seeds, edges)
        resolved = run_by_id(runs, "two_hop")["resolved"]
        first = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual("a", path_of(resolved, "na")["via"]["intermediate_id"])
        self.assertEqual(path_of(first, "a")["path_id"], path_of(resolved, "na")["via"]["first_hop_path_id"])
        self.assertEqual("b", path_of(resolved, "nb")["via"]["intermediate_id"])
        self.assertEqual(path_of(first, "b")["path_id"], path_of(resolved, "nb")["via"]["first_hop_path_id"])

    def test_two_hop_reuses_real_r05_and_r06(self):
        pool, seeds, edges = chain_fixture()
        runs = self.runs(pool, seeds, edges)
        run = run_by_id(runs, "two_hop")
        self.assertIn("verdict_counts", run["resolved"]["provenance"], "必须经 R05 真实裁决")
        self.assertIn("root_canon", run["resolved"]["provenance"])
        self.assertIn("counts", run["rendered"]["manifest"], "必须经 R06 真实渲染")
        self.assertIn("scenes", run["rendered"])

    def test_max_hops_stays_one_after_two_hop_execution(self):
        pool, seeds, edges = chain_fixture()
        self.runs(pool, seeds, edges)
        self.assertEqual(1, rp.MAX_HOPS, "二跳是组合两次一跳，不许改写 R04 硬上限")


# ── 行为锁：差异报告（稳定排序、稳定 reason_code、sha16 引用）──────────────


class DiffTests(unittest.TestCase):
    def test_identical_renders_have_empty_diffs(self):
        stub = render_stub([selected_row("a")], counts={"selected": 1})
        got = rx.diff_manifests(stub, stub)
        for section in ("selected", "excluded", "dropped"):
            self.assertEqual({"added": [], "removed": [], "changed": []}, got[section])
        self.assertEqual({"full": "sufficient", "variant": "sufficient"}, got["status"])

    def test_removed_selected_entries_carry_sha16_and_reason(self):
        full = render_stub([selected_row("a"), selected_row("b", sha16="b" * 16)])
        variant = render_stub([selected_row("a")])
        got = rx.diff_manifests(full, variant)
        entry = got["selected"]["removed"][0]
        self.assertEqual("selected_removed", entry["diff_reason"])
        self.assertEqual("b", entry["object_id"])
        self.assertEqual("b" * 16, entry["sha16"])

    def test_added_selected_entries_are_reported(self):
        full = render_stub([selected_row("a")])
        variant = render_stub([selected_row("a"), selected_row("c")])
        got = rx.diff_manifests(full, variant)
        self.assertEqual(["c"], [x["object_id"] for x in got["selected"]["added"]])
        self.assertEqual("selected_added", got["selected"]["added"][0]["diff_reason"])

    def test_changed_selected_entries_list_the_changed_fields(self):
        full = render_stub([selected_row("a", reason="gapped",
                                         missing=[{"slot": "root_source",
                                                   "reason_code": "root_source_missing"}])])
        variant = render_stub([selected_row("a", reason="grounded")])
        entry = rx.diff_manifests(full, variant)["selected"]["changed"][0]
        self.assertEqual(["missing_slots", "reason"], entry["changed_fields"])

    def test_excluded_diff_entries_carry_content_hash_references(self):
        gap = {"object_id": "a", "category": "missing",
               "reason_code": "root_source_missing", "slot": "root_source"}
        got = rx.diff_manifests(render_stub(excluded=[gap]), render_stub())
        entry = got["excluded"]["removed"][0]
        self.assertEqual("excluded_removed", entry["diff_reason"])
        self.assertEqual(mm.digest(compact(gap))[:16], entry["ref_sha16"])

    def test_aggregate_count_changes_are_changed_not_removed_plus_added(self):
        full = render_stub(excluded=[{"object_id": "", "category": "dropped",
                                      "reason_code": "revisit", "count": 2}])
        variant = render_stub(excluded=[{"object_id": "", "category": "dropped",
                                         "reason_code": "revisit", "count": 1}])
        got = rx.diff_manifests(full, variant)
        self.assertEqual([], got["dropped"]["added"])
        self.assertEqual([], got["dropped"]["removed"])
        entry = got["dropped"]["changed"][0]
        self.assertEqual("dropped_changed", entry["diff_reason"])
        self.assertEqual(["count"], entry["changed_fields"])

    def test_the_sections_partition_the_exclusion_categories(self):
        rows = [{"object_id": "a", "category": "missing",
                 "reason_code": "root_source_missing", "slot": "root_source"},
                {"object_id": "b", "category": "conflicting",
                 "reason_code": "explicit_negation_opposite", "verdict": "contradiction",
                 "left_id": "b", "right_id": "c"},
                {"object_id": "", "category": "dropped", "reason_code": "revisit", "count": 1},
                {"object_id": "", "category": "invalidated",
                 "reason_code": "invalidated_neighbor", "count": 1},
                {"object_id": "d", "category": "budget", "reason_code": "budget",
                 "detail": "max_entries"}]
        got = rx.diff_manifests(render_stub(excluded=rows), render_stub())
        self.assertEqual({"missing", "conflicting"},
                         {x["category"] for x in got["excluded"]["removed"]})
        self.assertEqual({"dropped", "invalidated", "budget"},
                         {x["category"] for x in got["dropped"]["removed"]})
        self.assertEqual(5, len(got["excluded"]["removed"]) + len(got["dropped"]["removed"]))

    def test_diff_order_is_stable_under_input_shuffle(self):
        full = render_stub([selected_row("a"), selected_row("b")],
                           excluded=[{"object_id": "a", "category": "missing",
                                      "reason_code": "root_source_missing", "slot": "root_source"}])
        variant = render_stub([selected_row("c"), selected_row("d")],
                              excluded=[{"object_id": "b", "category": "missing",
                                         "reason_code": "subject_ref_missing", "slot": "subject_ref"}])
        baseline = rx.diff_manifests(full, variant)
        shuffled = copy.deepcopy(variant)
        shuffled["manifest"]["selected"].reverse()
        shuffled["manifest"]["excluded"].reverse()
        self.assertEqual(canonical(baseline), canonical(rx.diff_manifests(full, shuffled)))

    def test_real_two_hop_diff_adds_the_independent_neighbor(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_second_hop=True)
        diff = diff_by_id(got, "two_hop")
        self.assertEqual("one_hop", diff["baseline"])
        self.assertEqual(["n2"], [x["object_id"] for x in diff["selected"]["added"]])
        added_codes = {x["reason_code"] for x in diff["dropped"]["added"]}
        self.assertLessEqual({"revisit", "unknown_neighbor", "invalidated_neighbor"}, added_codes)

    def test_diff_reason_codes_are_the_contract_ones(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True,
                     vector_edges=[vector_edge("s", "n2")])
        for diff in got["diffs"]:
            for section in ("selected", "excluded", "dropped"):
                for kind in ("added", "removed", "changed"):
                    for entry in diff[section][kind]:
                        self.assertIn(entry["diff_reason"], rx.DIFF_REASONS)

    def test_diff_is_deterministic_and_read_only(self):
        pool, seeds, edges = chain_fixture()
        runs = rx.execute(plan(seeds, edges, pool, allow_second_hop=True),
                          seeds=seeds, edges=edges, items=pool)
        full = run_by_id(runs, "one_hop")["rendered"]
        variant = run_by_id(runs, "two_hop")["rendered"]
        before = canonical([full, variant])
        first = rx.diff_manifests(full, variant)
        self.assertEqual(canonical(first), canonical(rx.diff_manifests(full, variant)))
        self.assertEqual(before, canonical([full, variant]))
        first["selected"]["added"].append({"object_id": "fake"})
        self.assertEqual(before, canonical([full, variant]), "diff 输出必须深拷贝")

    def test_non_dict_renders_are_tolerated(self):
        got = rx.diff_manifests(None, None)
        self.assertEqual({"full": "", "variant": ""}, got["status"])
        for section in ("selected", "excluded", "dropped"):
            self.assertEqual({"added": [], "removed": [], "changed": []}, got[section])


# ── 行为锁：评审包（manual_review 口径、不打分、不落原文、默认全关）──────────


class PacketTests(unittest.TestCase):
    def test_the_packet_shape_is_the_contract_one(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool)
        self.assertEqual({"plan_id", "algorithm_version", "now", "variants", "diffs",
                          "judgement", "authorization", "organic", "expression_control",
                          "provenance", "limitations"}, set(got))
        self.assertEqual("r08-1", got["algorithm_version"])
        self.assertEqual(NOW, got["now"])
        self.assertEqual(rx.LIMITATIONS, got["limitations"])
        self.assertEqual(list(rx.VARIANT_IDS), [v["variant_id"] for v in got["variants"]])

    def test_the_review_caliber_is_manual_review_and_never_concludes(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool)
        self.assertIsNone(got["judgement"], "结论永远留空给人判")
        self.assertEqual("manual_review", got["authorization"])
        self.assertEqual("not_observed", got["organic"])
        self.assertIs(False, got["expression_control"], "隔离契约不接管表达")

    def test_default_off_packet_disables_extensions_and_has_no_diffs(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, vector_edges=[vector_edge("s", "n2")])
        self.assertEqual([], got["diffs"])
        self.assertEqual(["one_hop"], got["provenance"]["executed"])
        self.assertEqual(["one_hop_vector", "two_hop", "two_hop_vector"],
                         got["provenance"]["disabled"])
        for v in got["variants"][1:]:
            self.assertEqual("disabled", v["state"])
            self.assertIsNone(v["manifest"])
            self.assertIsNone(v["status"])
            self.assertEqual({}, v["counts"])

    def test_both_on_packet_runs_all_and_diffs_against_one_hop(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True,
                     vector_edges=[vector_edge("s", "n2")])
        self.assertEqual(list(rx.VARIANT_IDS), got["provenance"]["executed"])
        self.assertEqual([], got["provenance"]["disabled"])
        self.assertEqual(list(rx.VARIANT_IDS[1:]), [d["variant_id"] for d in got["diffs"]])
        for diff in got["diffs"]:
            self.assertEqual("one_hop", diff["baseline"])

    def test_executed_variants_carry_manifests_not_scenes(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_second_hop=True)
        for v in got["variants"]:
            if v["state"] != "executed":
                continue
            self.assertEqual({"variant_id", "definition", "state", "status", "counts", "manifest"},
                             set(v))
            self.assertEqual({"status", "context", "selected", "excluded",
                              "degradation", "counts"}, set(v["manifest"]))
            self.assertNotIn("scenes", v)

    def test_the_packet_plan_id_is_recomputable(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool)
        core = {k: v for k, v in got["provenance"]["plan"].items() if k != "plan_id"}
        self.assertEqual("rx_" + mm.digest(compact(core))[:14], got["plan_id"])
        self.assertEqual(got["provenance"]["plan"]["plan_id"], got["plan_id"])

    def test_the_packet_leaks_no_chat_text(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_vector_edges=True, allow_second_hop=True,
                     vector_edges=[vector_edge("s", "n2")])
        text = json.dumps(got, ensure_ascii=False)
        for raw in ("喜欢茶", "喜欢咖啡", "喜欢可可", "喜欢果汁", "茶转述"):
            self.assertNotIn(raw, text, "评审包只带 ID/sha16，不落聊天原文")
        self.assertEqual(set(), set(walk_keys(got)) & {"restatement", "quote"})

    def test_the_packet_fabricates_no_conclusion_or_score_fields(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_second_hop=True)
        self.assertEqual(set(), set(walk_keys(got)) &
                         {"score", "useful", "conclusion", "verdicts"})
        self.assertIsNone(got["judgement"])

    def test_the_packet_is_json_serializable(self):
        pool, seeds, edges = chain_fixture()
        got = packet(seeds, edges, pool, allow_second_hop=True,
                     semantic_hints={("s", "n1"): {"relation": "same", "source": "t"}})
        dumped = json.dumps(got, ensure_ascii=False)
        reloaded = json.loads(dumped)
        self.assertEqual(dumped, json.dumps(reloaded, ensure_ascii=False))
        self.assertEqual(list(rx.LIMITATIONS), reloaded["limitations"])

    def test_the_packet_is_deterministic_across_reruns(self):
        pool, seeds, edges = chain_fixture()
        kw = dict(allow_vector_edges=True, allow_second_hop=True,
                  vector_edges=[vector_edge("s", "n2")])
        first = packet(seeds, edges, pool, **kw)
        second = packet(seeds, edges, pool, **kw)
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(first["plan_id"], second["plan_id"])

    def test_the_packet_is_isolated_from_inputs_and_mutation(self):
        pool, seeds, edges = chain_fixture()
        vec = [vector_edge("s", "n2")]
        before = canonical([pool, seeds, edges, vec])
        got = packet(seeds, edges, pool, vector_edges=vec, allow_vector_edges=True,
                     allow_second_hop=True)
        got["provenance"]["plan"]["seed_ids"].append("fake")
        got["variants"][0]["manifest"]["selected"].append({"object_id": "fake"})
        got["diffs"][0]["selected"]["added"].append({"object_id": "fake"})
        got["judgement"] = "二跳有用"
        self.assertEqual(before, canonical([pool, seeds, edges, vec]))
        fresh = packet(seeds, edges, pool, vector_edges=vec, allow_vector_edges=True,
                       allow_second_hop=True)
        self.assertIsNone(fresh["judgement"])
        self.assertEqual([], [x for x in fresh["provenance"]["plan"]["seed_ids"] if x == "fake"])

    def test_empty_packet_degrades_without_fabricating(self):
        got = packet([], [], [], allow_second_hop=True, allow_vector_edges=True)
        for v in got["variants"]:
            self.assertEqual("not_found", v["status"])
            self.assertEqual([], v["manifest"]["selected"])
        for diff in got["diffs"]:
            for section in ("selected", "excluded", "dropped"):
                self.assertEqual({"added": [], "removed": [], "changed": []}, diff[section])
        self.assertEqual("manual_review", got["authorization"])
        self.assertEqual("not_observed", got["organic"])

    def test_degraded_empty_is_not_claimed_as_no_evidence(self):
        got = packet([], [], [], degradation=[{"store": "cognition", "reason": "absent",
                                               "detail": ""}])
        for v in got["variants"]:
            if v["state"] != "executed":
                continue
            self.assertEqual("degraded", v["status"])
            self.assertEqual([], v["manifest"]["excluded"], "读不到不等于没有")


# ── 端到端：接真实 R01→R02→R03→R08（测试可 import，被测模块不 import）────────


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r08_", ignore_cleanup_errors=True)
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
                "as_of_time": NOW, "query_text": "喜欢茶",
                "recent_referents": ["阿甲"]}
        base.update(over)
        return ro.query_context(**base)

    def fact(self, fid, text, eid, subjects=("per_1",), ts=1000.0):
        return {"id": fid, "text": text, "ts": ts, "source_event_ids": [eid],
                "status": "active",
                "evidence_spans": [{"event_id": eid, "quote": text, "subject_ref": s,
                                    "subject_basis": "source_actor"} for s in subjects]}

    def group_facts(self, entries):
        path = os.path.join(self.root, "memory", "group_facts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"groups": {"g1": {"entries": entries}}}, handle, ensure_ascii=False)

    def chain(self, ctx=None, budget=1, **packet_kw):
        ctx = ctx if ctx is not None else self.ctx()
        fetched = ro.retrieve_evidence(ctx)
        edges = ae.project_edges(fetched["items"], fetched["degradation"])["edges"]
        recalled = rs.recall(fetched["items"], dict(ctx, retrieval_budget=budget))
        got = rx.build_review_packet(seeds=recalled["items"], edges=edges,
                                     items=fetched["items"], now=NOW,
                                     degradation=fetched["degradation"], **packet_kw)
        return got, fetched, recalled, edges

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
        got, _fetched, _recalled, _edges = self.chain()
        for v in got["variants"]:
            if v["state"] == "executed":
                self.assertEqual("degraded", v["status"])
                self.assertEqual([], v["manifest"]["excluded"], "读不到不等于没有")
        self.assertEqual({"absent"}, {d["reason"] for d in got["provenance"]["plan"]["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "R01→R02→R03→R08 只读链路顺手建了库/目录，就不再是只读")

    def test_real_two_hop_reaches_a_neighbor_one_hop_cannot(self):
        # f1(per_1)─f2(per_1,per_2)─f3(per_2)：真实 shared_entity 边链，一跳只到 f2。
        self.group_facts([self.fact("f1", "喜欢茶", "ge1", subjects=("per_1",), ts=1000.0),
                          self.fact("f2", "喜欢咖啡", "ge2", subjects=("per_1", "per_2"), ts=1001.0),
                          self.fact("f3", "喜欢可可", "ge3", subjects=("per_2",), ts=1002.0)])
        got, _fetched, recalled, _edges = self.chain(budget=1, allow_second_hop=True)
        self.assertEqual(["group:g1:f1"], [x["object_id"] for x in recalled["items"]])
        one = variant_by_id(got, "one_hop")
        two = variant_by_id(got, "two_hop")
        self.assertEqual({"group:g1:f1", "group:g1:f2"},
                         {x["object_id"] for x in one["manifest"]["selected"]})
        self.assertIn("group:g1:f3", {x["object_id"] for x in two["manifest"]["selected"]},
                      "真实二跳应扩出一跳到不了的 f3")
        diff = diff_by_id(got, "two_hop")
        self.assertIn("group:g1:f3", [x["object_id"] for x in diff["selected"]["added"]])
        self.assertNotIn("喜欢茶", json.dumps(got, ensure_ascii=False))

    def test_real_offline_vector_edge_expands_a_neighbor(self):
        # 稀疏主体链 f1(per_1)─f2(per_1,per_2)─f3(per_2)：真实边一跳到不了 f3，
        # 离线向量边 f1-f3 才把它扩出来（向量边是调用方离线预计算输入，合成夹具）。
        self.group_facts([self.fact("f1", "喜欢茶", "ge1", subjects=("per_1",), ts=1000.0),
                          self.fact("f2", "喜欢咖啡", "ge2", subjects=("per_1", "per_2"), ts=1001.0),
                          self.fact("f3", "喜欢可可", "ge3", subjects=("per_2",), ts=1002.0)])
        vec = [{"edge_id": "vec_f1_f3", "type": "vector_similarity",
                "a": "group:g1:f1", "a_kind": "fact", "b": "group:g1:f3", "b_kind": "fact",
                "basis": {"similarity": 0.8, "model_id": "offline_model", "batch_id": "batch_1"}}]
        got, _fetched, _recalled, _edges = self.chain(budget=1, allow_vector_edges=True,
                                                      vector_edges=vec)
        one = variant_by_id(got, "one_hop")
        vecv = variant_by_id(got, "one_hop_vector")
        self.assertNotIn("group:g1:f3", {x["object_id"] for x in one["manifest"]["selected"]})
        self.assertIn("group:g1:f3", {x["object_id"] for x in vecv["manifest"]["selected"]})
        self.assertEqual(["vec_f1_f3"], got["provenance"]["plan"]["vector_edge_ids"])
        self.assertEqual("manual_review", got["authorization"])
        self.assertIsNone(got["judgement"])

    def test_the_full_readonly_pipeline_rewrites_no_asset(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1")])
        self.belief("命题一", "我喜欢冰可乐", "r08:one")
        before = snapshot(self.root)
        self.chain(allow_second_hop=True, allow_vector_edges=True)
        self.assertEqual(before, snapshot(self.root))

    def test_real_legacy_missing_slots_are_carried_by_the_baseline(self):
        bid = sm.add_belief("某个旧认识", subject_qq="1001", is_legacy=True, ts=900.0)
        ctx = self.ctx(allowed_source_scopes=["private:1001", "group:g1", "cognition"])
        got, _fetched, _recalled, _edges = self.chain(ctx=ctx, budget=8)
        one = variant_by_id(got, "one_hop")
        codes = {x["reason_code"] for x in one["manifest"]["excluded"]
                 if x["category"] == "missing" and x["object_id"] == bid}
        self.assertLessEqual({"root_source_missing", "original_quote_missing",
                              "provenance_unverified"}, codes, "legacy 缺依据大声登记，不补造")

    def test_real_chain_is_deterministic_and_score_blind(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1", ts=1000.0),
                          self.fact("f2", "喜欢咖啡", "ge2", subjects=("per_1", "per_2"), ts=1001.0),
                          self.fact("f3", "喜欢可可", "ge3", subjects=("per_2",), ts=1002.0)])
        got, fetched, recalled, edges = self.chain(budget=1, allow_second_hop=True)
        again, _f2, _r2, _e2 = self.chain(budget=1, allow_second_hop=True)
        self.assertEqual(canonical(got), canonical(again))
        for row in fetched["items"] + recalled["items"]:
            row.setdefault("match", {})["score_parts"] = {"lexical": 99.0}
        mutated = rx.build_review_packet(seeds=recalled["items"], edges=edges,
                                         items=fetched["items"], now=NOW,
                                         degradation=fetched["degradation"], allow_second_hop=True)
        self.assertEqual(canonical(got["diffs"]), canonical(mutated["diffs"]),
                         "改 score_parts 影响了差异，说明 R08 偷读了 R03 的分数")
        for vid in rx.VARIANT_IDS:
            va, vb = variant_by_id(got, vid), variant_by_id(mutated, vid)
            if va["state"] != "executed":
                continue
            self.assertEqual([x["object_id"] for x in va["manifest"]["selected"]],
                             [x["object_id"] for x in vb["manifest"]["selected"]])


if __name__ == "__main__":
    unittest.main()