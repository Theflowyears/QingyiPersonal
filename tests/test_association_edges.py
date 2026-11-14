"""R02 类型化稀疏联想边：在 R01 只读 item 之上派生类型化、稀疏、可追溯的边。

行为锁覆盖验收判据：边类型化（type + 两端 object_id/kind + basis）、稀疏上限
（有界度数/全局封顶）、basis 可追溯到真实来源、absent 库报 degraded、不凭空造边，
以及「只做边投影」——不遍历(R04)、不融合打分(R03)、不判矛盾(R05)。
AST 锁钉死「真只读纯投影」：不建库建表建目录、不在 import 时冻路径、不 import
retrieval_objects/各库（尊重 R01 的只观察静态锁）、不做遍历/打分/矛盾。

R02 是纯函数：调用方把 R01 已只读投影好的 items 与 degradation 传进来。这里既用
手工 item 精确核对派生规则，也接真实 R01 输出做端到端验证（测试可以 import R01，
被测模块 association_edges 本身不 import）。
"""
import ast
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

import association_edges as ae
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


def item(oid, kind="belief", store="cognition", sources=(), spans=(),
         subject=None, validity="valid", **over):
    row = {"object_id": oid, "object_kind": kind, "store": store,
           "root_source_ids": list(sources), "evidence_spans": list(spans),
           "validity": validity}
    if subject is not None:
        row["subject_ref"] = subject
    row.update(over)
    return row


def span(event_id="e1", quote="我喜欢冰可乐", subject=None, **over):
    row = {"event_id": event_id, "quote": quote, "span_start": 0,
           "span_end": len(quote), "source_revision": "rev1", "match_kind": "model_quote"}
    if subject is not None:
        row["subject_ref"] = subject
        row["subject_basis"] = "source_actor"
    row.update(over)
    return row


def degree_map(edges):
    deg = {}
    for edge in edges:
        deg[edge["a"]] = deg.get(edge["a"], 0) + 1
        deg[edge["b"]] = deg.get(edge["b"], 0) + 1
    return deg


# ── AST 静态锁：真只读、纯投影、不越界 ──────────────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "association_edges.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_projection_never_writes_or_creates_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "connect", "atomic_write_json",
                     "save", "add", "add_belief", "record_event", "write", "writelines",
                     "dump", "touch"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读边投影里出现写操作 %s" % node.func.attr)
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

    def test_it_does_not_import_r01_or_any_store(self):
        """R01 的只观察锁禁止 bot 模块 import retrieval_objects；R02 做成纯投影，
        连各库也不 import，彻底不碰文件系统。"""
        banned = {"retrieval_objects", "self_model", "persona_store", "group_memory_store",
                  "knowledge_base", "vision_notes"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             "R02 应是纯投影，不该 import %s" % sorted(names & banned))

    def test_it_does_not_call_the_r01_retrieval_entry_points(self):
        wanted = {"retrieve_evidence", "fetch_retrieval_items"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name, wanted, "R02 不该调用 R01 的检索入口 %s" % name)

    def test_no_traversal_scoring_or_contradiction_machinery(self):
        banned = {"traverse", "traversal", "walk", "bfs", "dfs", "expand", "expansion",
                  "hop", "spread", "diffuse", "diffusion", "score", "scoring", "rank",
                  "ranking", "fuse", "fusion", "contradict", "contradiction", "conflict"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name.lower(), banned,
                                 "出现了遍历/打分/矛盾函数 %s" % node.name)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name.lower(), banned,
                                 "调用了遍历/打分/矛盾函数 %s" % name)
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, "score_parts",
                                    "融合打分是 R03，R02 不许读 item.match.score_parts")

    def test_the_edge_types_and_statuses_are_the_contract_ones(self):
        self.assertEqual(("source_event", "source_url", "subject_ref",
                          "shared_source", "shared_entity"), ae.EDGE_TYPES)
        self.assertEqual(("sufficient", "partial", "not_found", "degraded"), ae.STATUSES)
        self.assertNotIn("conflicting", ae.STATUSES, "矛盾判定是 R05，不许冒充")


# ── 行为锁：纯函数派生规则（手工 item，精确核对）──────────────────────


class TypedEdgeTests(unittest.TestCase):
    def test_a_belief_and_its_source_event_form_a_typed_traceable_edge(self):
        row = item("b1", sources=["e1"], spans=[span("e1")])
        edges = ae.project_edges([row])["edges"]
        src = [e for e in edges if e["type"] == "source_event"]
        self.assertEqual(1, len(src))
        edge = src[0]
        self.assertEqual({"b1", "e1"}, {edge["a"], edge["b"]})
        self.assertEqual({"belief", "source_event"}, {edge["a_kind"], edge["b_kind"]})
        self.assertEqual("e1", edge["basis"]["event_id"])
        self.assertEqual("我喜欢冰可乐", edge["basis"]["quote"])
        self.assertEqual(0, edge["basis"]["span_start"])
        self.assertEqual(len("我喜欢冰可乐"), edge["basis"]["span_end"])

    def test_every_edge_carries_type_two_ids_kinds_and_basis(self):
        rows = [item("b1", sources=["e1"], spans=[span("e1", subject="per_1")]),
                item("b2", sources=["e1"], spans=[span("e1", subject="per_1")])]
        for edge in ae.project_edges(rows)["edges"]:
            self.assertIn(edge["type"], ae.EDGE_TYPES)
            self.assertTrue(edge["a"] and edge["b"])
            self.assertTrue(edge["a_kind"] and edge["b_kind"])
            self.assertIsInstance(edge["basis"], dict)
            self.assertTrue(edge["basis"], "basis 不能为空，必须说明为什么有这条边")
            self.assertNotIn("score", edge, "融合打分是 R03，边不许带 score")

    def test_person_belief_edge_comes_from_a_real_subject_ref(self):
        row = item("b1", sources=["e1"], spans=[span("e1", subject="per_1")])
        edges = ae.project_edges([row])["edges"]
        subj = [e for e in edges if e["type"] == "subject_ref"]
        self.assertEqual(1, len(subj))
        self.assertEqual({"b1", "per_1"}, {subj[0]["a"], subj[0]["b"]})
        self.assertEqual({"belief", "person"}, {subj[0]["a_kind"], subj[0]["b_kind"]})
        self.assertEqual("per_1", subj[0]["basis"]["subject_ref"])
        self.assertEqual("e1", subj[0]["basis"]["event_id"])

    def test_a_top_level_subject_ref_also_yields_a_person_edge(self):
        row = item("b1", sources=["e1"], subject="per_9")
        subj = [e for e in ae.project_edges([row])["edges"] if e["type"] == "subject_ref"]
        self.assertEqual(1, len(subj))
        self.assertEqual("per_9", subj[0]["basis"]["subject_ref"])

    def test_two_items_about_the_same_person_form_a_shared_entity_edge(self):
        rows = [item("b1", sources=["e1"], spans=[span("e1", subject="per_1")]),
                item("b2", sources=["e2"], spans=[span("e2", subject="per_1")])]
        shared = [e for e in ae.project_edges(rows)["edges"] if e["type"] == "shared_entity"]
        self.assertEqual(1, len(shared))
        self.assertEqual({"b1", "b2"}, {shared[0]["a"], shared[0]["b"]})
        self.assertEqual(["per_1"], shared[0]["basis"]["subject_refs"])

    def test_two_items_citing_the_same_event_form_a_shared_source_edge(self):
        rows = [item("b1", sources=["e1"]), item("b2", sources=["e1"])]
        shared = [e for e in ae.project_edges(rows)["edges"] if e["type"] == "shared_source"]
        self.assertEqual(1, len(shared))
        self.assertEqual({"b1", "b2"}, {shared[0]["a"], shared[0]["b"]})
        self.assertEqual(["e1"], shared[0]["basis"]["event_ids"])

    def test_multiple_shared_keys_merge_into_one_edge_with_full_basis(self):
        rows = [item("b1", sources=["e1", "e2"]), item("b2", sources=["e1", "e2"])]
        shared = [e for e in ae.project_edges(rows)["edges"] if e["type"] == "shared_source"]
        self.assertEqual(1, len(shared))
        self.assertEqual(["e1", "e2"], shared[0]["basis"]["event_ids"])

    def test_a_url_source_is_not_passed_off_as_an_event(self):
        row = item("k1", kind="world_knowledge", store="knowledge",
                   sources=["https://example.invalid/a"])
        edges = ae.project_edges([row])["edges"]
        self.assertEqual(["source_url"], [e["type"] for e in edges])
        self.assertEqual("source_url", edges[0]["b_kind"])
        self.assertEqual("https://example.invalid/a", edges[0]["basis"]["url"])
        self.assertEqual([], [e for e in edges if e["type"] == "source_event"])


class NoFabricationTests(unittest.TestCase):
    def test_an_item_without_any_basis_yields_no_edge(self):
        got = ae.project_edges([item("b3")])
        self.assertEqual([], got["edges"])
        self.assertEqual("not_found", got["status"])

    def test_blank_source_ids_are_not_turned_into_edges(self):
        got = ae.project_edges([item("b1", sources=["", "   "])])
        self.assertEqual([], got["edges"])

    def test_a_legacy_item_without_sources_is_not_given_a_source(self):
        row = item("b1", sources=[], validity="unknown", legacy=True)
        self.assertEqual([], ae.project_edges([row])["edges"])

    def test_an_invalidated_item_is_skipped_not_resurrected(self):
        row = item("b1", sources=["e1"], validity="invalidated")
        got = ae.project_edges([row])
        self.assertEqual([], got["edges"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated"))

    def test_cognition_item_without_subject_ref_gets_no_person_edge(self):
        """R01 认知投影没带 subject_ref，R02 不替 M03 编一个实体 ID。"""
        row = item("b1", sources=["e1"], spans=[span("e1")])  # span 无 subject_ref
        edges = ae.project_edges([row])["edges"]
        self.assertEqual([], [e for e in edges if e["type"] == "subject_ref"])
        self.assertEqual([], [e for e in edges if e["type"] == "shared_entity"])

    def test_items_with_different_sources_and_subjects_are_not_linked(self):
        rows = [item("b1", sources=["e1"], spans=[span("e1", subject="per_1")]),
                item("b2", sources=["e2"], spans=[span("e2", subject="per_2")])]
        edges = ae.project_edges(rows)["edges"]
        self.assertEqual([], [e for e in edges if e["type"] in ("shared_source", "shared_entity")])

    def test_basis_only_references_ids_the_item_really_carries(self):
        row = item("b1", sources=["e1"], spans=[span("e1", subject="per_1")])
        for edge in ae.project_edges([row])["edges"]:
            basis = edge["basis"]
            if "event_id" in basis and basis.get("reason") == "root_source_id":
                self.assertIn(basis["event_id"], row["root_source_ids"])
            if "subject_ref" in basis:
                self.assertEqual("per_1", basis["subject_ref"])

    def test_a_source_edge_without_a_span_has_no_invented_quote(self):
        row = item("b1", sources=["e1"])  # 无 evidence_span
        edge = [e for e in ae.project_edges([row])["edges"] if e["type"] == "source_event"][0]
        self.assertEqual("e1", edge["basis"]["event_id"])
        self.assertNotIn("quote", edge["basis"], "没有逐字 span 就不许补造原句")


class SparsityTests(unittest.TestCase):
    def test_a_hub_event_degree_is_bounded(self):
        rows = [item("m%02d" % i, sources=["hub"]) for i in range(30)]
        got = ae.project_edges(rows)
        deg = degree_map(got["edges"])
        self.assertLessEqual(deg.get("hub", 0), ae.MAX_DEGREE)
        for node, count in deg.items():
            self.assertLessEqual(count, ae.MAX_DEGREE, "节点 %s 度数超界" % node)

    def test_the_global_edge_count_is_bounded(self):
        rows = [item("m%02d" % i, sources=["hub"]) for i in range(40)]
        got = ae.project_edges(rows, max_edges=20)
        self.assertLessEqual(len(got["edges"]), 20)
        self.assertGreaterEqual(got["provenance"]["dropped"].get("edge_cap", 0), 1)

    def test_degree_cap_drops_are_explainable(self):
        rows = [item("m%02d" % i, sources=["hub"]) for i in range(30)]
        got = ae.project_edges(rows)
        self.assertGreaterEqual(got["provenance"]["dropped"].get("degree_cap", 0), 1)
        self.assertEqual("partial", got["status"])

    def test_a_zero_cap_yields_no_edges(self):
        rows = [item("b1", sources=["e1"]), item("b2", sources=["e1"])]
        self.assertEqual([], ae.project_edges(rows, max_edges=0)["edges"])
        self.assertEqual([], ae.project_edges(rows, max_degree=0)["edges"])

    def test_it_does_not_build_the_full_clique(self):
        """共享同一来源的 n 个 item，边数必须远小于全对全 n*(n-1)/2。"""
        n = 20
        rows = [item("m%02d" % i, sources=["hub"]) for i in range(n)]
        got = ae.project_edges(rows)
        self.assertLess(len(got["edges"]), n * (n - 1) // 2)


class DeterminismTests(unittest.TestCase):
    def test_the_same_input_gives_the_same_edges(self):
        rows = [item("b1", sources=["e1"], spans=[span("e1", subject="per_1")]),
                item("b2", sources=["e1"], spans=[span("e1", subject="per_1")])]
        first = ae.project_edges(rows)["edges"]
        second = ae.project_edges(rows)["edges"]
        self.assertEqual([e["edge_id"] for e in first], [e["edge_id"] for e in second])

    def test_input_order_does_not_change_the_projection(self):
        rows = [item("b1", sources=["e1"]), item("b2", sources=["e1"]), item("b3", sources=["e2"])]
        forward = ae.project_edges(rows)["edges"]
        backward = ae.project_edges(list(reversed(rows)))["edges"]
        self.assertEqual(sorted(e["edge_id"] for e in forward),
                         sorted(e["edge_id"] for e in backward))

    def test_duplicate_items_do_not_duplicate_edges(self):
        row = item("b1", sources=["e1"], spans=[span("e1")])
        edges = ae.project_edges([row, row])["edges"]
        ids = [e["edge_id"] for e in edges]
        self.assertEqual(len(ids), len(set(ids)))


class ScopeBoundaryTests(unittest.TestCase):
    """只做边投影：不遍历(R04)、不打分(R03)、不判矛盾(R05)。"""

    def test_it_returns_edges_not_paths(self):
        got = ae.project_edges([item("b1", sources=["e1"])])
        self.assertIn("edges", got)
        self.assertNotIn("paths", got, "受限一跳遍历是 R04，R02 只产边不产路径")

    def test_no_edge_carries_a_fusion_score(self):
        rows = [item("b1", sources=["e1"]), item("b2", sources=["e1"])]
        for edge in ae.project_edges(rows)["edges"]:
            self.assertNotIn("score", edge)
            self.assertNotIn("weight", edge)

    def test_contradictory_looking_items_are_not_judged(self):
        rows = [item("b1", sources=["e1"], spans=[span("e1", quote="我喜欢冰可乐")]),
                item("b2", sources=["e2"], spans=[span("e2", quote="我不喜欢冰可乐")])]
        got = ae.project_edges(rows)
        self.assertNotEqual("conflicting", got["status"])
        self.assertNotIn("conflicts", got)

    def test_the_provenance_names_what_is_not_done(self):
        limits = " ".join(ae.project_edges([item("b1", sources=["e1"])])["provenance"]["limitations"])
        for undone in ("R03", "R04", "R05"):
            self.assertIn(undone, limits)

    def test_it_does_not_mutate_the_input_items(self):
        row = item("b1", sources=["e1"], spans=[span("e1", subject="per_1")])
        before = json.dumps(row, sort_keys=True, ensure_ascii=False)
        ae.project_edges([row])
        self.assertEqual(before, json.dumps(row, sort_keys=True, ensure_ascii=False))


class DegradationTests(unittest.TestCase):
    def test_absent_store_degradation_is_propagated_as_degraded(self):
        degradation = [{"store": "cognition", "reason": "absent", "detail": ""}]
        got = ae.project_edges([], degradation)
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["edges"])
        self.assertEqual(degradation, got["degradation"])

    def test_degraded_still_projects_edges_from_available_items(self):
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        got = ae.project_edges([item("b1", sources=["e1"])], degradation)
        self.assertEqual("degraded", got["status"])
        self.assertGreaterEqual(len(got["edges"]), 1, "可用 item 的边仍要投影，不能整体装没有")

    def test_unreadable_is_not_the_same_as_absent(self):
        got = ae.project_edges([], [{"store": "group", "reason": "unreadable", "detail": "X"}])
        self.assertEqual("degraded", got["status"])
        self.assertEqual("unreadable", got["degradation"][0]["reason"])


# ── 端到端：接真实 R01 输出（测试可 import R01，被测模块不 import）──────


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r02_", ignore_cleanup_errors=True)
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

    def belief(self, quote="我喜欢冰可乐", text="对方明确自述：我喜欢冰可乐",
               session="private:1001", actor="1001", ts=1000.0):
        eid = sm.record_event("private_message", session=session, actor_qq=actor,
                              visibility="private", payload={"text": quote}, ts=ts,
                              dedupe="r02:" + session + ":" + quote)
        bid = sm.add_belief(text, subject_qq=actor, source="explicit_self_report",
                            ts=ts, spans=[{"event_id": eid, "quote": quote,
                                           "span_start": 0, "span_end": len(quote),
                                           "match_kind": "model_quote"}])
        return eid, bid

    def project(self, ctx=None):
        fetched = ro.retrieve_evidence(ctx if ctx is not None else self.ctx())
        return ae.project_edges(fetched["items"], fetched["degradation"]), fetched


class IntegrationTests(StoreFixture):
    def test_an_empty_data_dir_reports_degraded_and_creates_nothing(self):
        before = snapshot(self.root)
        got, fetched = self.project()
        self.assertEqual("degraded", fetched["status"])
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["edges"])
        self.assertEqual({"absent"}, {d["reason"] for d in got["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "R01+R02 只读链路顺手建了库/目录，就不再是只读")

    def test_a_real_belief_projects_a_traceable_source_event_edge(self):
        eid, bid = self.belief()
        got, _fetched = self.project()
        src = [e for e in got["edges"] if e["type"] == "source_event"]
        self.assertEqual(1, len(src))
        self.assertEqual({bid, eid}, {src[0]["a"], src[0]["b"]})
        self.assertEqual(eid, src[0]["basis"]["event_id"])
        self.assertEqual("我喜欢冰可乐", src[0]["basis"]["quote"])

    def test_two_real_beliefs_sharing_an_event_get_a_shared_source_edge(self):
        eid = sm.record_event("private_message", session="private:1001", actor_qq="1001",
                              visibility="private", payload={"text": "我喜欢冰可乐"},
                              ts=1000.0, dedupe="r02:shared")
        b1 = sm.add_belief("命题一", subject_qq="1001", ts=1000.0,
                           spans=[{"event_id": eid, "quote": "我喜欢冰可乐",
                                   "span_start": 0, "span_end": 6, "match_kind": "model_quote"}])
        b2 = sm.add_belief("命题二", subject_qq="1001", ts=1000.0,
                           spans=[{"event_id": eid, "quote": "我喜欢冰可乐",
                                   "span_start": 0, "span_end": 6, "match_kind": "model_quote"}])
        got, _fetched = self.project()
        shared = [e for e in got["edges"] if e["type"] == "shared_source"]
        self.assertEqual(1, len(shared))
        self.assertEqual({b1, b2}, {shared[0]["a"], shared[0]["b"]})
        self.assertEqual([eid], shared[0]["basis"]["event_ids"])

    def test_an_unreadable_store_propagates_degraded(self):
        path = os.path.join(self.root, "memory", "group_facts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        got, _fetched = self.project()
        self.assertEqual("degraded", got["status"])
        self.assertIn("unreadable", {d["reason"] for d in got["degradation"]})

    def test_the_full_readonly_pipeline_rewrites_no_asset(self):
        self.belief()
        before = snapshot(self.root)
        self.project()
        self.assertEqual(before, snapshot(self.root))

    def test_real_edges_are_typed_sparse_and_scoreless(self):
        self.belief()
        got, _fetched = self.project()
        for edge in got["edges"]:
            self.assertIn(edge["type"], ae.EDGE_TYPES)
            self.assertTrue(edge["a"] and edge["b"] and edge["basis"])
            self.assertNotIn("score", edge)
        deg = degree_map(got["edges"])
        for count in deg.values():
            self.assertLessEqual(count, ae.MAX_DEGREE)


if __name__ == "__main__":
    unittest.main()