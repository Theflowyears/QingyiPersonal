# -*- coding: utf-8 -*-
"""R04 受限一跳与根源去重：在 R03 种子与 R02 边上做一跳扩散，按根源归并支持。

行为锁覆盖验收判据：一跳即止（二跳可达也不扩）、度数/全局预算、根源去重
（同根多派生只算一份、不同根不合并、转述不算独立证据）、invalidated 不复活、
basis 可追溯到真实边与来源、空输入/缺边/degraded 原样承接、确定性排序、
path 无 score、status 无 conflicting。
AST 锁钉死「真只读纯投影」：不 import retrieval_objects/association_edges/
recall_scoring/各库、无 sqlite 写/建库语句、不建目录、不在 import 时冻路径、
无打分/矛盾机器，且结构上只有两层循环（没有队列/递归/环游，走不出第二跳）。

R04 是纯函数：调用方把种子、边、items 池与 degradation 传进来。这里既用手工
item/edge 精确核对规则，也接真实 R01→R02→R03 输出做端到端验证（测试可以
import R01/R02/R03，被测模块 ripple_paths 本身不 import）。R02「只产边不产路径」
的锁在这里一并复核：paths 只由 R04 产生。
"""
import ast
import hashlib
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

import ripple_paths as rp
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


def edge(a, b, etype="shared_source", basis=None, a_kind="belief", b_kind="belief",
         edge_id=None):
    if basis is None:
        basis = {"reason": etype}
    return {"edge_id": edge_id or ("edge_%s_%s_%s" % (etype, a, b)),
            "type": etype, "a": a, "a_kind": a_kind, "b": b, "b_kind": b_kind,
            "basis": dict(basis)}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# ── AST 静态锁：真只读、纯函数、一跳结构、不越界 ─────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "ripple_paths.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_expansion_never_writes_or_creates_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "connect", "atomic_write_json",
                     "save", "add", "add_belief", "record_event", "write", "writelines",
                     "dump", "touch"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读扩散里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else None
                    for keyword in node.keywords:
                        if keyword.arg == "mode":
                            mode = keyword.value
                    if isinstance(mode, ast.Constant):
                        self.assertNotIn("w", str(mode.value))
                        self.assertNotIn("a", str(mode.value))

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径，换 DATA_DIR 的测试会读到别人的库")

    def test_it_does_not_import_r01_r02_r03_or_any_store(self):
        """R01/R03 的只观察锁禁止 bot 模块 import 它们；R04 做成纯函数，
        连 R02 与各库也不 import，输入全部由调用方传入。"""
        banned = {"retrieval_objects", "association_edges", "recall_scoring",
                  "self_model", "persona_store", "group_memory_store",
                  "knowledge_base", "vision_notes", "sqlite3", "os", "time"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             "R04 应是纯函数，不该 import %s" % sorted(names & banned))

    def test_the_import_surface_is_hashlib_only(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"hashlib"},
                             "纯投影只需要 hashlib，多出来的 import 面要解释")

    def test_it_does_not_call_the_upstream_entry_points(self):
        wanted = {"retrieve_evidence", "fetch_retrieval_items", "project_edges",
                  "recall", "score_item"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name, wanted, "R04 不该调用上游入口 %s，输入由调用方传入" % name)

    def test_no_scoring_or_contradiction_machinery(self):
        banned = {"score", "scoring", "rank", "ranking", "fuse", "fusion",
                  "contradict", "contradiction", "conflict"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in banned:
                    self.assertNotIn(word, node.name.lower(),
                                     "出现了打分/矛盾函数 %s" % node.name)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                for word in banned:
                    self.assertNotIn(word, name.lower(),
                                     "调用了打分/矛盾函数 %s" % name)
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, "score_parts",
                                    "融合打分是 R03，R04 不许读 item.match.score_parts")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotEqual("conflicting", node.value,
                                    "矛盾判定是 R05，status 不许出现 conflicting")

    def test_no_sql_write_or_database_creation_statements(self):
        for token in ("INSERT INTO", "INSERT OR", "UPDATE ", "DELETE FROM",
                      "REPLACE INTO", "CREATE TABLE", "CREATE INDEX", "DROP TABLE",
                      "sqlite", "executescript", "PRAGMA"):
            self.assertNotIn(token, self.source,
                             "只读扩散里出现建库/写库语句 %s" % token)

    def test_the_structure_cannot_walk_a_second_hop(self):
        """一跳即止的结构锁：没有 while 环游、没有递归、没有遍历机器。
        实现只允许「种子×边」的有限 for 循环，邻居永远只当终点。"""
        for node in ast.walk(self.tree):
            self.assertNotIsInstance(node, ast.While,
                                     "出现 while 环游，一跳即止不再由结构保证")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("bfs", "dfs", "walk", "traverse", "traversal",
                             "two_hop", "second_hop", "multi_hop"):
                    self.assertNotIn(word, node.name.lower(),
                                     "出现多跳遍历机器 %s" % node.name)
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                        self.assertNotEqual(inner.func.id, node.name,
                                            "函数 %s 递归调用自己，可能走出第二跳" % node.name)

    def test_the_contract_constants(self):
        self.assertEqual(("sufficient", "partial", "not_found", "degraded"), rp.STATUSES)
        self.assertNotIn("conflicting", rp.STATUSES, "矛盾判定是 R05，不许冒充")
        self.assertEqual(1, rp.MAX_HOPS, "二跳是 R08 且需人工评审，一跳是硬上限")
        self.assertGreater(rp.MAX_DEGREE, 0)
        self.assertGreater(rp.MAX_EXPANDED, 0)


# ── 行为锁：一跳即止 ─────────────────────────────────────────────────


class OneHopTests(unittest.TestCase):
    def test_a_chain_stops_after_one_hop(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e2"])]
        edges = [edge("s", "n1"), edge("n1", "n2")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(["n1"], [p["object_id"] for p in got["paths"]])
        self.assertNotIn("n2", [p["object_id"] for p in got["paths"]],
                         "n2 二跳可达，R04 绝不扩（二跳是 R08）")

    def test_a_cycle_does_not_walk_back_or_forever(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e2"])]
        edges = [edge("s", "n1"), edge("n1", "s", edge_id="edge_back"),
                 edge("n1", "n2")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(["n1"], [p["object_id"] for p in got["paths"]])

    def test_a_diamond_two_hop_target_is_not_expanded(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("a", sources=["e1"]), item("b", sources=["e2"]),
                        item("c", sources=["e3"])]
        edges = [edge("s", "a"), edge("s", "b"), edge("a", "c"), edge("b", "c")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual({"a", "b"}, {p["object_id"] for p in got["paths"]})
        self.assertNotIn("c", [p["object_id"] for p in got["paths"]])

    def test_every_path_is_exactly_one_hop(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e2"])]
        edges = [edge("s", "n1"), edge("s", "n2")]
        for path in rp.expand_paths(seeds, edges, items=pool)["paths"]:
            self.assertEqual(1, path["hops"])
            self.assertEqual(rp.MAX_HOPS, path["hops"])

    def test_the_neighbors_own_edges_are_never_consulted(self):
        seeds = [item("s", sources=["e0"])]
        star = [item("x%d" % i, sources=["ex%d" % i]) for i in range(5)]
        pool = seeds + [item("n1", sources=["e1"])] + star
        edges = [edge("s", "n1")] + [edge("n1", x["object_id"]) for x in star]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(["n1"], [p["object_id"] for p in got["paths"]])

    def test_an_edge_between_two_seeds_is_not_an_expansion(self):
        seeds = [item("s1", sources=["e1"]), item("s2", sources=["e2"])]
        got = rp.expand_paths(seeds, [edge("s1", "s2")], items=seeds)
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("seed_overlap"))
        self.assertTrue(any(d["reason"] == "seed_overlap" for d in got["provenance"]["deduped"]))


# ── 行为锁：basis 可追溯、不凭空造路径 ───────────────────────────────


class BasisTests(unittest.TestCase):
    def test_path_basis_carries_the_edge_identity_and_reason(self):
        seeds = [item("s", sources=["e0"])]
        neighbor = item("n1", sources=["e1"])
        via = edge("s", "n1", basis={"reason": "shared_source", "event_ids": ["e1"]})
        got = rp.expand_paths(seeds, [via], items=seeds + [neighbor])
        basis = got["paths"][0]["basis"]
        self.assertEqual("shared_source", basis["edge_type"])
        self.assertEqual(via["edge_id"], basis["edge_id"])
        self.assertEqual({"s", "n1"}, {basis["a"], basis["b"]})
        self.assertEqual("belief", basis["a_kind"])
        self.assertEqual("belief", basis["b_kind"])
        self.assertEqual(["e1"], basis["event_ids"])
        self.assertEqual("shared_source", basis["reason"])

    def test_path_identity_comes_from_the_real_neighbor_item(self):
        seeds = [item("s", sources=["e0"])]
        neighbor = item("n1", kind="group_fact", store="group", sources=["e1"])
        got = rp.expand_paths(seeds, [edge("s", "n1")], items=seeds + [neighbor])
        path = got["paths"][0]
        self.assertEqual("n1", path["object_id"])
        self.assertEqual("group_fact", path["object_kind"])
        self.assertEqual("group", path["store"])
        self.assertEqual("s", path["seed_id"])
        self.assertEqual(["e1"], path["root_source_ids"])

    def test_an_empty_edge_basis_gets_no_invented_quote_or_source(self):
        seeds = [item("s", sources=["e0"])]
        neighbor = item("n1", sources=["e1"])
        via = edge("s", "n1", basis={})
        basis = rp.expand_paths(seeds, [via], items=seeds + [neighbor])["paths"][0]["basis"]
        self.assertEqual("shared_source", basis["reason"])
        self.assertNotIn("quote", basis, "没有真实 span 就不许补造原句")
        self.assertNotIn("event_ids", basis)

    def test_path_ids_are_stable_and_content_derived(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e2"])]
        edges = [edge("s", "n1"), edge("s", "n2")]
        first = rp.expand_paths(seeds, edges, items=pool)["paths"]
        second = rp.expand_paths(seeds, edges, items=pool)["paths"]
        self.assertEqual([p["path_id"] for p in first], [p["path_id"] for p in second])
        self.assertEqual(2, len({p["path_id"] for p in first}))
        for path in first:
            self.assertTrue(path["path_id"].startswith("path_"))

    def test_paths_carry_no_score_or_weight(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"])]
        for path in rp.expand_paths(seeds, [edge("s", "n1")], items=pool)["paths"]:
            self.assertNotIn("score", path)
            self.assertNotIn("weight", path)
            self.assertNotIn("score", path["basis"])

    def test_an_unknown_endpoint_is_not_fabricated_into_a_path(self):
        """指向池外端点（来源事件/人物引用）的边不产路径：详情与有效性核对不了。"""
        seeds = [item("s", sources=["e0"])]
        via = edge("s", "e0", etype="source_event", b_kind="source_event")
        got = rp.expand_paths(seeds, [via], items=seeds)
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("unknown_neighbor"))

    def test_without_a_pool_only_seeds_can_serve_as_neighbor_details(self):
        seeds = [item("s1", sources=["e1"]), item("s2", sources=["e2"])]
        got = rp.expand_paths(seeds, [edge("s1", "s2")])
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("seed_overlap"))


# ── 行为锁：根源去重（同根源转述不算独立证据）────────────────────────


class RootDedupTests(unittest.TestCase):
    def test_same_root_neighbors_count_as_one_diffusion(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e1"])]
        edges = [edge("s", "n1"), edge("s", "n2")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(["n1"], [p["object_id"] for p in got["paths"]])
        merged = [d for d in got["provenance"]["deduped"] if d["reason"] == "same_root"]
        self.assertEqual(1, len(merged))
        self.assertEqual("n2", merged[0]["object_id"])
        self.assertEqual("n1", merged[0]["merged_into"])
        self.assertEqual(["e1"], merged[0]["root_source_ids"])
        self.assertEqual(1, got["provenance"]["dropped"].get("same_root"))

    def test_a_neighbor_repeating_a_seed_root_is_merged_into_the_seed(self):
        seeds = [item("s", sources=["e1"])]
        pool = seeds + [item("n", sources=["e1"])]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool)
        self.assertEqual([], got["paths"])
        merged = got["provenance"]["deduped"]
        self.assertEqual(1, len(merged))
        self.assertEqual({"object_id": "n", "merged_into": "s",
                          "reason": "same_root", "root_source_ids": ["e1"]}, merged[0])

    def test_different_roots_are_not_merged(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e2"])]
        got = rp.expand_paths(seeds, [edge("s", "n1"), edge("s", "n2")], items=pool)
        self.assertEqual({"n1", "n2"}, {p["object_id"] for p in got["paths"]})
        self.assertEqual([], [d for d in got["provenance"]["deduped"]
                              if d["reason"] == "same_root"])

    def test_partial_root_overlap_still_counts_as_restatement(self):
        """交集即归并（M05 独立支持口径）：{e1,e2} 对上已支持的 {e1} 不算独立。"""
        seeds = [item("s", sources=["e1"])]
        pool = seeds + [item("n", sources=["e1", "e2"])]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool)
        self.assertEqual([], got["paths"])
        self.assertEqual(["e1"], got["provenance"]["deduped"][0]["root_source_ids"])

    def test_rootless_legacy_neighbors_are_not_merged(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=[]), item("n2", sources=[])]
        got = rp.expand_paths(seeds, [edge("s", "n1"), edge("s", "n2")], items=pool)
        self.assertEqual({"n1", "n2"}, {p["object_id"] for p in got["paths"]})
        limits = " ".join(got["provenance"]["limitations"])
        self.assertIn("无根源", limits)

    def test_the_same_neighbor_from_two_seeds_is_one_support(self):
        seeds = [item("s1", sources=["e1"]), item("s2", sources=["e2"])]
        pool = seeds + [item("n", sources=["e3"])]
        got = rp.expand_paths(seeds, [edge("s1", "n"), edge("s2", "n")], items=pool)
        self.assertEqual(1, len(got["paths"]))
        self.assertEqual("s1", got["paths"][0]["seed_id"])
        self.assertTrue(any(d["reason"] == "already_expanded" and d["object_id"] == "n"
                            for d in got["provenance"]["deduped"]))

    def test_dedup_does_not_consume_the_expansion_budget(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e1"]),
                        item("n3", sources=["e2"])]
        edges = [edge("s", "n1"), edge("s", "n2"), edge("s", "n3")]
        got = rp.expand_paths(seeds, edges, items=pool, max_expanded=1)
        self.assertEqual(["n1"], [p["object_id"] for p in got["paths"]])
        self.assertEqual(1, got["provenance"]["dropped"].get("same_root"))
        self.assertEqual(1, got["provenance"]["dropped"].get("expansion_cap"))

    def test_a_deduped_neighbor_does_not_cover_roots_for_later_neighbors(self):
        """被归并的对象不登记新根源：n2 与 n1 同根被并，n3 的新根仍算独立。"""
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"]), item("n2", sources=["e1"]),
                        item("n3", sources=["e2"])]
        edges = [edge("s", "n1"), edge("s", "n2"), edge("s", "n3")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual({"n1", "n3"}, {p["object_id"] for p in got["paths"]})


# ── 行为锁：失效防御（invalidated 不得复活）──────────────────────────


class InvalidationTests(unittest.TestCase):
    def test_an_invalidated_seed_is_not_used(self):
        seeds = [item("s", sources=["e0"], validity="invalidated")]
        pool = seeds + [item("n", sources=["e1"])]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool)
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_seed"))
        self.assertEqual(0, got["provenance"]["candidates"])

    def test_an_invalidated_neighbor_is_blocked_and_explainable(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n", sources=["e1"], validity="invalidated")]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool)
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_neighbor"))

    def test_an_invalidated_neighbor_does_not_cover_its_roots(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n1", sources=["e1"], validity="invalidated"),
                        item("n2", sources=["e1"])]
        edges = [edge("s", "n1"), edge("s", "n2")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(["n2"], [p["object_id"] for p in got["paths"]],
                         "失效对象挡在前面，也不能把同根的有效对象洗成转述")

    def test_pool_invalidation_overrides_a_valid_seed_copy(self):
        """失效单调（M05）：池里登记失效，种子副本说有效也不复活。"""
        seeds = [item("s", sources=["e0"], validity="valid")]
        pool = [item("s", sources=["e0"], validity="invalidated"),
                item("n", sources=["e1"])]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool)
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_seed"))

    def test_all_seeds_invalidated_reports_not_found(self):
        seeds = [item("s1", validity="invalidated"), item("s2", validity="invalidated")]
        got = rp.expand_paths(seeds, [], items=seeds)
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["paths"])
        self.assertEqual(2, got["provenance"]["dropped"].get("invalidated_seed"))


# ── 行为锁：度数与全局预算 ───────────────────────────────────────────


class BudgetTests(unittest.TestCase):
    def test_per_seed_degree_is_bounded(self):
        seeds = [item("s", sources=["e0"])]
        neighbors = [item("n%d" % i, sources=["e%d" % (i + 1)]) for i in range(6)]
        edges = [edge("s", n["object_id"]) for n in neighbors]
        got = rp.expand_paths(seeds, edges, items=seeds + neighbors, max_degree=3)
        self.assertEqual(3, len(got["paths"]))
        self.assertEqual(3, got["provenance"]["dropped"].get("degree_cap"))
        self.assertEqual("partial", got["status"])

    def test_the_global_expansion_count_is_bounded(self):
        seeds = [item("s1", sources=["e1"]), item("s2", sources=["e2"])]
        neighbors = [item("n%d" % i, sources=["e%d" % (i + 3)]) for i in range(6)]
        edges = ([edge("s1", n["object_id"]) for n in neighbors[:3]]
                 + [edge("s2", n["object_id"]) for n in neighbors[3:]])
        got = rp.expand_paths(seeds, edges, items=seeds + neighbors, max_expanded=4)
        self.assertEqual(4, len(got["paths"]))
        self.assertGreaterEqual(got["provenance"]["dropped"].get("expansion_cap"), 1)
        self.assertEqual("partial", got["status"])

    def test_zero_budgets_yield_no_paths(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n", sources=["e1"])]
        edges = [edge("s", "n")]
        self.assertEqual([], rp.expand_paths(seeds, edges, items=pool, max_expanded=0)["paths"])
        self.assertEqual([], rp.expand_paths(seeds, edges, items=pool, max_degree=0)["paths"])

    def test_the_effective_budget_is_reported(self):
        got = rp.expand_paths([], [], max_expanded=7, max_degree=2)
        self.assertEqual({"max_expanded": 7, "max_degree": 2}, got["provenance"]["budget"])

    def test_negative_budgets_are_clamped_not_crashed(self):
        got = rp.expand_paths([], [], max_expanded=-5, max_degree=-1)
        self.assertEqual({"max_expanded": 0, "max_degree": 0}, got["provenance"]["budget"])


# ── 行为锁：空输入、缺边、degraded 承接 ──────────────────────────────


class EmptyAndDegradationTests(unittest.TestCase):
    def test_empty_seeds_yield_not_found(self):
        got = rp.expand_paths([], [edge("a", "b")])
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["paths"])

    def test_seeds_without_edges_yield_not_found(self):
        got = rp.expand_paths([item("s", sources=["e0"])], [])
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["paths"])

    def test_absent_degradation_is_propagated_verbatim(self):
        degradation = [{"store": "cognition", "reason": "absent", "detail": ""}]
        got = rp.expand_paths([], [], degradation=degradation)
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["paths"])
        self.assertEqual(degradation, got["degradation"])

    def test_unreadable_is_not_the_same_as_absent(self):
        got = rp.expand_paths([], [], degradation=[
            {"store": "group", "reason": "unreadable", "detail": "X"}])
        self.assertEqual("degraded", got["status"])
        self.assertEqual("unreadable", got["degradation"][0]["reason"])

    def test_degraded_still_expands_available_seeds(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n", sources=["e1"])]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool,
                              degradation=[{"store": "group", "reason": "absent", "detail": ""}])
        self.assertEqual("degraded", got["status"])
        self.assertEqual(1, len(got["paths"]), "可用种子的扩散仍要做，不能整体装没有")

    def test_none_inputs_are_tolerated(self):
        got = rp.expand_paths(None, None)
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["paths"])
        self.assertEqual([], got["degradation"])

    def test_non_dict_rows_are_skipped(self):
        seeds = [None, 42, item("s", sources=["e0"])]
        pool = seeds + [item("n", sources=["e1"])]
        edges = [None, "x", edge("s", "n")]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(["n"], [p["object_id"] for p in got["paths"]])
        self.assertEqual(2, got["provenance"]["dropped"].get("malformed_edge"))

    def test_malformed_edges_are_dropped_explainably(self):
        seeds = [item("s", sources=["e0"])]
        pool = seeds + [item("n", sources=["e1"])]
        edges = [edge("s", "n"), {"edge_id": "bad1", "type": "shared_source", "a": "s"},
                 {"edge_id": "bad2", "a": "s", "b": "n"}]
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(1, len(got["paths"]))
        self.assertEqual(2, got["provenance"]["dropped"].get("malformed_edge"))
        self.assertEqual("partial", got["status"])

    def test_self_loop_edges_are_dropped(self):
        seeds = [item("s", sources=["e0"])]
        got = rp.expand_paths(seeds, [edge("s", "s")], items=seeds)
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("self_loop"))


# ── 行为锁：确定性与只读 ─────────────────────────────────────────────


class DeterminismTests(unittest.TestCase):
    def fixture(self):
        seeds = [item("s1", sources=["e1"]), item("s2", sources=["e2"])]
        pool = seeds + [item("n1", sources=["e3"]), item("n2", sources=["e3"]),
                        item("n3", sources=["e4"])]
        edges = [edge("s1", "n1"), edge("s1", "n2"), edge("s2", "n3"),
                 edge("s1", "n3", etype="shared_entity",
                      basis={"reason": "shared_entity", "subject_refs": ["per_1"]})]
        return seeds, edges, pool

    def test_the_same_input_gives_the_same_output(self):
        seeds, edges, pool = self.fixture()
        first = rp.expand_paths(seeds, edges, items=pool)
        second = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual(canonical(first), canonical(second))

    def test_input_order_does_not_change_the_output(self):
        seeds, edges, pool = self.fixture()
        forward = rp.expand_paths(seeds, edges, items=pool)
        backward = rp.expand_paths(list(reversed(seeds)), list(reversed(edges)),
                                   items=list(reversed(pool)))
        self.assertEqual(canonical(forward), canonical(backward))

    def test_duplicate_seeds_edges_and_pool_rows_do_not_duplicate_paths(self):
        seeds, edges, pool = self.fixture()
        got = rp.expand_paths(seeds + seeds, edges + edges, items=pool + pool)
        ids = [p["path_id"] for p in got["paths"]]
        self.assertEqual(len(ids), len(set(ids)))
        baseline = rp.expand_paths(seeds, edges, items=pool)
        self.assertEqual({p["object_id"] for p in baseline["paths"]},
                         {p["object_id"] for p in got["paths"]})

    def test_the_inputs_are_not_mutated(self):
        seeds, edges, pool = self.fixture()
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        before = canonical([seeds, edges, pool, degradation])
        rp.expand_paths(seeds, edges, items=pool, degradation=degradation)
        self.assertEqual(before, canonical([seeds, edges, pool, degradation]))

    def test_provenance_carries_no_clock(self):
        seeds, edges, pool = self.fixture()
        got = rp.expand_paths(seeds, edges, items=pool)
        self.assertNotIn("seconds", got["provenance"])
        self.assertNotIn("ts", got["provenance"])


# ── 行为锁：不越界（R03 打分 / R05 矛盾与缺失槽位）───────────────────


class ScopeBoundaryTests(unittest.TestCase):
    def test_r02_still_returns_edges_not_paths(self):
        """R02 的回归锁不被 R04 破坏：paths 只由 R04 产生。"""
        rows = [item("b1", sources=["e1"]), item("b2", sources=["e1"])]
        projected = ae.project_edges(rows)
        self.assertIn("edges", projected)
        self.assertNotIn("paths", projected, "R02 只产边不产路径，R04 不改 R02")
        expanded = rp.expand_paths(rows[:1], projected["edges"], items=rows)
        self.assertIn("paths", expanded)

    def test_score_parts_are_not_read(self):
        seeds = [item("s", sources=["e0"], match={"clues": [], "score_parts": {"lexical": 0.9}})]
        neighbor = item("n", sources=["e1"], match={"clues": [], "score_parts": {"lexical": 0.1}})
        edges = [edge("s", "n")]
        first = rp.expand_paths(seeds, edges, items=seeds + [neighbor])
        seeds[0]["match"]["score_parts"]["lexical"] = 0.0
        neighbor["match"]["score_parts"]["lexical"] = 99.0
        second = rp.expand_paths(seeds, edges, items=seeds + [neighbor])
        self.assertEqual(canonical(first), canonical(second),
                         "改 score_parts 影响了输出，说明 R04 偷读了 R03 的分数")
        for path in first["paths"]:
            self.assertNotIn("score", path)

    def test_contradictory_looking_items_are_not_judged(self):
        seeds = [item("s", sources=["e0"], restatement="我喜欢冰可乐")]
        pool = seeds + [item("n", sources=["e1"], restatement="我不喜欢冰可乐")]
        got = rp.expand_paths(seeds, [edge("s", "n")], items=pool)
        self.assertNotEqual("conflicting", got["status"])
        self.assertNotIn("conflicts", got)
        self.assertEqual(1, len(got["paths"]), "矛盾判定是 R05，R04 不拦也不判")

    def test_missing_slot_judgment_is_absent(self):
        got = rp.expand_paths([item("s")], [])
        self.assertNotIn("missing_support", got, "缺失槽位判定是 R05/R01，R04 不冒充")

    def test_the_limitations_name_what_is_not_done(self):
        limits = " ".join(rp.expand_paths([], [])["provenance"]["limitations"])
        for undone in ("R03", "R05", "R08"):
            self.assertIn(undone, limits)
        self.assertIn("范围", limits, "范围硬过滤由调用方负责必须如实声明")

    def test_the_provenance_shape_is_the_contract_one(self):
        got = rp.expand_paths([item("s", sources=["e0"])], [])
        prov = got["provenance"]
        for key in ("candidates", "expanded", "deduped", "dropped", "budget", "limitations"):
            self.assertIn(key, prov)
        self.assertEqual(1, prov["candidates"])
        self.assertEqual(0, prov["expanded"])
        self.assertIsInstance(prov["deduped"], list)
        self.assertIsInstance(prov["dropped"], dict)


# ── 端到端：接真实 R01→R02→R03 输出（测试可 import，被测模块不 import）──


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r04_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def ctx(self, **over):
        base = {"recipient_scope": "private:1001",
                "allowed_source_scopes": ["private:1001"],
                "as_of_time": NOW, "query_text": "她喜欢什么"}
        base.update(over)
        return ro.query_context(**base)

    def chain(self, ctx=None, recall_ctx=None):
        fetched = ro.retrieve_evidence(ctx if ctx is not None else self.ctx())
        edges = ae.project_edges(fetched["items"], fetched["degradation"])["edges"]
        recalled = rs.recall(fetched["items"], recall_ctx if recall_ctx is not None else self.ctx())
        got = rp.expand_paths(recalled["items"], edges, items=fetched["items"],
                              degradation=fetched["degradation"])
        return got, fetched, recalled

    def belief(self, proposition, quote="我喜欢冰可乐", dedupe="r04:one", ts=1000.0):
        eid = sm.record_event("private_message", session="private:1001", actor_qq="1001",
                              visibility="private", payload={"text": quote}, ts=ts,
                              dedupe=dedupe)
        bid = sm.add_belief(proposition, subject_qq="1001", source="explicit_self_report",
                            ts=ts, spans=[{"event_id": eid, "quote": quote,
                                           "span_start": 0, "span_end": len(quote),
                                           "match_kind": "model_quote"}])
        return eid, bid


class IntegrationTests(StoreFixture):
    def test_an_empty_data_dir_reports_degraded_and_creates_nothing(self):
        before = snapshot(self.root)
        got, fetched, recalled = self.chain()
        self.assertEqual("degraded", fetched["status"])
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["paths"])
        self.assertEqual([], recalled["items"])
        self.assertEqual({"absent"}, {d["reason"] for d in got["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "R01→R02→R03→R04 只读链路顺手建了库/目录，就不再是只读")

    def test_only_r04_produces_paths_in_the_whole_chain(self):
        self.belief("命题一")
        fetched = ro.retrieve_evidence(self.ctx())
        projected = ae.project_edges(fetched["items"], fetched["degradation"])
        recalled = rs.recall(fetched["items"], self.ctx())
        self.assertEqual([], fetched["paths"], "R01 的 paths 恒为空")
        self.assertNotIn("paths", projected, "R02 只产边不产路径")
        self.assertNotIn("paths", recalled, "R03 只打分不遍历")
        got = rp.expand_paths(recalled["items"], projected["edges"],
                              items=fetched["items"], degradation=fetched["degradation"])
        self.assertIn("paths", got)

    def test_a_real_same_root_restatement_is_deduped_not_expanded(self):
        eid = sm.record_event("private_message", session="private:1001", actor_qq="1001",
                              visibility="private", payload={"text": "我喜欢冰可乐"},
                              ts=1000.0, dedupe="r04:shared")
        b1 = sm.add_belief("命题一", subject_qq="1001", ts=1000.0,
                           spans=[{"event_id": eid, "quote": "我喜欢冰可乐",
                                   "span_start": 0, "span_end": 6, "match_kind": "model_quote"}])
        b2 = sm.add_belief("命题二", subject_qq="1001", ts=1000.0,
                           spans=[{"event_id": eid, "quote": "我喜欢冰可乐",
                                   "span_start": 0, "span_end": 6, "match_kind": "model_quote"}])
        got, _fetched, recalled = self.chain(recall_ctx=self.ctx(retrieval_budget=1))
        self.assertEqual(1, len(recalled["items"]))
        seed_id = recalled["items"][0]["object_id"]
        other = ({b1, b2} - {seed_id}).pop()
        self.assertEqual([], got["paths"], "同根源转述不算独立扩散")
        merged = [d for d in got["provenance"]["deduped"] if d["reason"] == "same_root"]
        self.assertEqual(1, len(merged))
        self.assertEqual(other, merged[0]["object_id"])
        self.assertEqual(seed_id, merged[0]["merged_into"])
        self.assertEqual([eid], merged[0]["root_source_ids"])

    def test_a_real_shared_entity_neighbor_expands_exactly_one_hop(self):
        path = os.path.join(self.root, "memory", "group_facts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        facts = {"groups": {"g1": {"entries": [
            {"id": "f1", "text": "阿甲喜欢冰可乐", "ts": 1000.0,
             "source_event_ids": ["ge1"], "status": "active",
             "evidence_spans": [{"event_id": "ge1", "quote": "我喜欢冰可乐",
                                 "subject_ref": "per_1", "subject_basis": "source_actor"}]},
            {"id": "f2", "text": "阿甲常买冷饮", "ts": 1001.0,
             "source_event_ids": ["ge2"], "status": "active",
             "evidence_spans": [{"event_id": "ge2", "quote": "又买冷饮",
                                 "subject_ref": "per_1", "subject_basis": "source_actor"}]}]}}}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(facts, handle, ensure_ascii=False)
        ctx = self.ctx(allowed_source_scopes=["private:1001", "group:g1"],
                       recent_referents=["阿甲"], query_text="阿甲喜欢什么")
        got, fetched, recalled = self.chain(ctx=ctx, recall_ctx=dict(ctx, retrieval_budget=1))
        self.assertEqual(1, len(recalled["items"]))
        seed_id = recalled["items"][0]["object_id"]
        self.assertEqual(1, len(got["paths"]))
        path_row = got["paths"][0]
        self.assertNotEqual(seed_id, path_row["object_id"])
        self.assertEqual(1, path_row["hops"])
        self.assertEqual("shared_entity", path_row["basis"]["edge_type"])
        self.assertEqual(["per_1"], path_row["basis"]["subject_refs"])
        self.assertEqual({seed_id, path_row["object_id"]},
                         {path_row["basis"]["a"], path_row["basis"]["b"]})
        self.assertTrue(path_row["root_source_ids"], "邻居的根源必须真实可追溯")
        self.assertNotIn("score", path_row)

    def test_the_full_readonly_pipeline_rewrites_no_asset(self):
        self.belief("命题一")
        before = snapshot(self.root)
        self.chain()
        self.assertEqual(before, snapshot(self.root))

    def test_real_paths_are_traceable_and_scoreless(self):
        self.belief("命题一")
        self.belief("命题二", quote="我喜欢冰可乐", dedupe="r04:two")
        got, _fetched, _recalled = self.chain()
        for path in got["paths"]:
            self.assertEqual(1, path["hops"])
            self.assertTrue(path["basis"]["edge_id"])
            self.assertIn(path["basis"]["edge_type"], ae.EDGE_TYPES)
            self.assertNotIn("score", path)
        self.assertNotEqual("conflicting", got["status"])


if __name__ == "__main__":
    unittest.main()