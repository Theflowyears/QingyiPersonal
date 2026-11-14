# -*- coding: utf-8 -*-
"""R06 证据场景渲染与清单：R05 裁决输出之上的只读结构化渲染 + 完整清单。

行为锁覆盖验收判据：可支撑对象按根源/对象分组成结构化场景（每条带 object_id、
来源层 store、根源代表、verdict 引用）；conflicting/invalidated 绝不入场景、绝不
冒充充分证据；缺失逐字登记不补造；degraded 原样承接；四种排除口径（missing/
dropped/invalidated/conflicting）reason_code 与 R05 逐字一致；预算超限大声登记
dropped(reason=budget) 不静默截断；确定性排序、无时钟、不读 score_parts、输入不被
修改、输出深拷贝。

AST 锁钉死「复用而非重造」：import 面只许 copy + R05(ripple_conflicts) + R04
(ripple_paths) + A03(memory_manifest)；必须复用 R05 契约/R04 助手/A03 digest；
不许长第二套矛盾引擎、不许重新裁决、不许伪造 verdict（verdict 引用只能从源记录
拷贝）；conflicting 只走 M04 verdict=contradiction 通道；真只读、不 import
R01/R03/各库、无 SQL 写/建库、不冻路径、无打分机器、不调上游入口。

R06 是纯函数：调用方把 R05 结果、items 池、种子与清单上下文传进来。这里既用手工
item/edge 精确核对规则，也接真实 R01→R02→R03→R04→R05→R06 输出做端到端验证
（测试可以 import R01/R02/R03，被测模块 ripple_evidence 本身不 import）。
所有合成数据只出现在 tempfile 隔离目录或内存里，是测试夹具，不是自然样本。
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


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def resolve(seeds, edges, items=None, **kw):
    return rc.resolve_ripple(seeds, edges, items=items, **kw)


def render(resolved, items=None, seeds=None, **kw):
    return re6.render_evidence(resolved, items=items, seeds=seeds, **kw)


# ── AST 静态锁：复用 R05/R04/A03，不长第二套引擎，真只读 ───────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "ripple_evidence.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)
        cls.parents = {}
        for node in ast.walk(cls.tree):
            for child in ast.iter_child_nodes(node):
                cls.parents[child] = node

    def _ancestors(self, node):
        chain, cur = [], node
        while cur in self.parents:
            cur = self.parents[cur]
            chain.append(cur)
        return chain

    def test_the_import_surface_is_copy_plus_r05_r04_a03(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"copy", "ripple_conflicts", "ripple_paths",
                                        "memory_manifest"}, "多出来的 import 面要解释")
        self.assertLessEqual({"ripple_conflicts", "ripple_paths", "memory_manifest"},
                             imported, "R06 必须复用 R05 契约/R04 助手/A03 清单口径")

    def test_it_reuses_r05_contract_r04_helpers_and_a03_digest(self):
        calls = {(n.func.value.id, n.func.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name)}
        self.assertIn(("rp", "_index"), calls, "候选索引必须复用 R04 _index")
        self.assertIn(("rp", "_roots"), calls, "根源提取必须复用 R04 _roots")
        self.assertIn(("rc", "_subject_of"), calls, "主体提取必须复用 R05 _subject_of")
        self.assertIn(("mm", "digest"), calls, "清单/场景哈希必须复用 A03 digest")
        attrs = {(n.value.id, n.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        self.assertIn(("rc", "STATUSES"), attrs, "status 契约必须复用 R05")
        self.assertIn(("rc", "MISSING_REASONS"), attrs, "缺槽口径必须复用 R05")

    def test_it_does_not_import_r01_r03_or_any_store(self):
        banned = {"retrieval_objects", "recall_scoring", "association_edges",
                  "self_model", "persona_store", "group_memory_store",
                  "knowledge_base", "vision_notes", "sqlite3", "os", "time",
                  "random", "json", "claim_conflicts"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             "R06 应是纯函数，不该 import %s" % sorted(names & banned))

    def test_no_second_adjudication_engine(self):
        """家族词表/剥壳/裁决函数都不许在 R06 重定义：M04/R05 是唯一引擎。"""
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
                                 "R06 不许自带家族词表 %s" % node.value)
        for token in ("_FAMILY_MARKERS", "_NEGATION_PREFIXES", "_PREFERENCE_POSITIVE",
                      "_PREFERENCE_NEGATIVE", "_FAMILY_LEADERS"):
            self.assertNotIn(token, self.source, "出现了 M04 的剥壳词表 %s" % token)

    def test_no_verdict_record_is_fabricated(self):
        """verdict 只能从 R05 源记录拷贝引用：R06 不许构造带 verdict 字面量的字典。"""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "verdict":
                        self.assertNotIsInstance(value, ast.Constant,
                                                 "R06 自己伪造了裁决 verdict 字面量")
        verdict_constants = [n for n in ast.walk(self.tree)
                             if isinstance(n, ast.Constant) and isinstance(n.value, str)
                             and n.value in cc.VERDICTS]
        for node in verdict_constants:
            self.assertEqual("contradiction", node.value,
                             "除 contradiction 过滤外不许引用其它 verdict 字面量")
            self.assertIsInstance(self.parents.get(node), ast.Compare,
                                  "verdict 字面量只许用于比较过滤，不许用于赋值伪造")

    def test_conflicting_comes_only_through_the_m04_verdict_channel(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and node.value == "contradiction":
                self.assertIsInstance(self.parents.get(node), ast.Compare,
                                      "contradiction 只许用于比较过滤 M04 裁决")

    def test_the_module_never_writes_or_creates_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "connect", "atomic_write_json",
                     "save", "add", "add_belief", "record_event", "write", "writelines",
                     "dump", "touch"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读渲染里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "open", "纯函数不该打开任何文件")
        for token in ("INSERT INTO", "INSERT OR", "UPDATE ", "DELETE FROM",
                      "REPLACE INTO", "CREATE TABLE", "CREATE INDEX", "DROP TABLE",
                      "sqlite", "executescript", "PRAGMA", "cognition.db"):
            self.assertNotIn(token, self.source,
                             "只读渲染里出现建库/写库语句或库名 %s" % token)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_no_scoring_machinery(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("score", "score_parts", "weight"),
                                 "融合打分是 R03，R06 不许读分数")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("score", "scoring", "rank", "fuse", "fusion"):
                    self.assertNotIn(word, node.name.lower(), "出现了打分函数 %s" % node.name)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                for word in ("score", "scoring", "rank", "fuse", "fusion"):
                    self.assertNotIn(word, name.lower(), "调用了打分函数 %s" % name)

    def test_it_does_not_call_the_upstream_entry_points(self):
        wanted = {"retrieve_evidence", "fetch_retrieval_items", "project_edges",
                  "recall", "score_item", "read_all_candidates", "resolve_ripple",
                  "assess_expansion", "expand_paths", "adjudicate"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name, wanted,
                                 "R06 不该调用上游入口 %s，输入由调用方传入" % name)

    def test_no_retired_or_control_flags(self):
        for token in ("expression_control", "counts_as_live_observation"):
            self.assertNotIn(token, self.source, "出现了全局静态锁禁止的标记 %s" % token)

    def test_the_contract_constants(self):
        self.assertEqual(ro.STATUSES, re6.STATUSES, "status 词表必须与 R01/R05 契约一致")
        self.assertEqual(rc.STATUSES, re6.STATUSES)
        self.assertEqual(rc.MISSING_REASONS, re6.MISSING_REASONS, "缺槽口径必须与 R05 逐字一致")
        self.assertEqual(("missing", "dropped", "invalidated", "conflicting", "budget"),
                         re6.CATEGORIES)
        self.assertEqual(("grounded", "gapped"), re6.SELECT_REASONS)
        self.assertEqual(("invalidated_seed", "invalidated_neighbor", "invalidated_candidate"),
                         re6.INVALIDATED_REASONS)
        self.assertEqual("budget", re6.BUDGET_REASON)
        self.assertIsInstance(re6.MAX_SCENES, int)
        self.assertIsInstance(re6.MAX_ENTRIES, int)
        self.assertGreater(re6.MAX_SCENES, 0)
        self.assertGreater(re6.MAX_ENTRIES, 0)
        self.assertIsInstance(re6.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(re6.LIMITATIONS), 5)
        limits = " ".join(re6.LIMITATIONS)
        for undone in ("R03", "R08", "C04", "M04", "LLM"):
            self.assertIn(undone, limits, "limitations 必须如实声明没做的事 %s" % undone)


# ── 行为锁：结构化场景渲染 ───────────────────────────────────────────


class RenderTests(unittest.TestCase):
    def test_a_clean_expansion_renders_supportable_scenes(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        got = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        self.assertEqual("sufficient", got["status"])
        self.assertEqual(2, len(got["scenes"]))
        self.assertEqual(2, got["provenance"]["rendered"])
        self.assertEqual([("s", "grounded"), ("n", "grounded")],
                         [(x["object_id"], x["reason"]) for x in got["manifest"]["selected"]])
        self.assertEqual([], got["manifest"]["excluded"])

    def test_entries_carry_the_contract_fields(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = render(resolve([s], []), items=[s], seeds=[s])
        entry = got["scenes"][0]["entries"][0]
        for field in ("object_id", "store", "root_representative", "verdict_refs",
                      "epistemic_kind", "subject_ref", "root_source_ids", "missing_slots"):
            self.assertIn(field, entry)
        self.assertEqual("s", entry["object_id"])
        self.assertEqual("cognition", entry["store"])
        self.assertEqual("e1", entry["root_representative"])
        self.assertEqual("per_1", entry["subject_ref"])

    def test_objects_sharing_a_root_group_into_one_scene(self):
        s1 = claim_item("s1", "喜欢茶", sources=["e1"])
        s2 = claim_item("s2", "喜欢咖啡", sources=["e1"])
        got = render(resolve([s1, s2], [], items=[s1, s2]), items=[s1, s2], seeds=[s1, s2])
        self.assertEqual(1, len(got["scenes"]), "同根源代表归一个场景")
        self.assertEqual(2, got["scenes"][0]["entry_count"])
        self.assertEqual("e1", got["scenes"][0]["root_representative"])
        self.assertEqual(["s1", "s2"], [e["object_id"] for e in got["scenes"][0]["entries"]])

    def test_rootless_objects_each_form_their_own_scene(self):
        s1 = item("s1", sources=[], subject_ref="per_1", restatement="喜欢茶",
                  occurred_at=1000.0)
        s2 = item("s2", sources=[], subject_ref="per_1", restatement="喜欢咖啡",
                  occurred_at=1000.0)
        got = render(resolve([s1, s2], [], items=[s1, s2]), items=[s1, s2], seeds=[s1, s2])
        self.assertEqual(2, len(got["scenes"]), "无根源无法归并，各自成组（如实）")
        for scene in got["scenes"]:
            self.assertEqual("", scene["root_representative"])
            self.assertEqual([], scene["root_source_ids"])

    def test_verdict_refs_are_carried_verbatim_from_r05(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        resolved = resolve([s], [edge("s", "n")], items=[s, n])
        got = render(resolved, items=[s, n], seeds=[s])
        refs = [e["verdict_refs"] for sc in got["scenes"] for e in sc["entries"]]
        flat = [r for group in refs for r in group]
        self.assertTrue(flat, "coexists 裁决应当被引用")
        for record in flat:
            self.assertIn(record, resolved["provenance"]["verdicts"],
                          "verdict 引用必须逐字来自 R05，不添油加醋")
        self.assertEqual("coexists", flat[0]["verdict"])

    def test_scene_id_is_a_deterministic_content_hash(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = render(resolve([s], []), items=[s], seeds=[s])
        scene = got["scenes"][0]
        self.assertEqual("scene_" + mm.digest(scene["scene_key"])[:14], scene["scene_id"])

    def test_seed_without_expansion_still_renders(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        resolved = resolve([s], [])
        self.assertEqual("not_found", resolved["status"], "R05 的 not_found 描述扩散结果")
        got = render(resolved, items=[s], seeds=[s])
        self.assertEqual("sufficient", got["status"], "种子本身是真实证据，可渲染")
        self.assertEqual(1, len(got["scenes"]))

    def test_restatement_is_real_content_not_generated(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = render(resolve([s], []), items=[s], seeds=[s])
        self.assertEqual("喜欢茶", got["scenes"][0]["entries"][0]["restatement"])

    def test_scenes_are_structured_data_not_chat_text(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = render(resolve([s], []), items=[s], seeds=[s])
        self.assertIsInstance(got["scenes"], list)
        self.assertIsInstance(got["scenes"][0], dict)
        self.assertIsInstance(got["scenes"][0]["entries"], list)
        self.assertNotIsInstance(got["scenes"][0], str)


# ── 行为锁：排除口径与 R05 逐字一致，不复活不补造 ─────────────────────


class ExclusionTests(unittest.TestCase):
    def test_conflicting_objects_are_excluded_from_scenes(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        got = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        self.assertEqual("conflicting", got["status"])
        self.assertEqual([], got["scenes"], "矛盾对象绝不入场景冒充充分证据")
        excluded = got["manifest"]["excluded"]
        self.assertEqual({"s", "n"}, {x["object_id"] for x in excluded})
        self.assertEqual({"conflicting"}, {x["category"] for x in excluded})

    def test_conflicting_reason_codes_match_m04_verbatim(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        resolved = resolve([s], [edge("s", "n")], items=[s, n])
        got = render(resolved, items=[s, n], seeds=[s])
        codes = {x["reason_code"] for x in got["manifest"]["excluded"]}
        self.assertEqual({r["reason_code"] for r in resolved["conflicts"]}, codes)
        self.assertEqual({"explicit_negation_opposite"}, codes)

    def test_invalidated_seed_never_renders(self):
        s = claim_item("s", "喜欢茶", sources=["e1"], validity="invalidated")
        got = render(resolve([s], [], items=[s]), items=[s], seeds=[s])
        self.assertEqual([], got["scenes"])
        self.assertIn({"object_id": "", "category": "invalidated",
                       "reason_code": "invalidated_seed", "count": 1},
                      got["manifest"]["excluded"])

    def test_invalidated_neighbor_never_renders(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"], validity="invalidated")
        got = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        self.assertEqual(["s"], [e["object_id"] for sc in got["scenes"] for e in sc["entries"]])
        self.assertIn({"object_id": "", "category": "invalidated",
                       "reason_code": "invalidated_neighbor", "count": 1},
                      got["manifest"]["excluded"])

    def test_invalidated_pool_registration_beats_seed_copy(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        pool = [dict(s, validity="invalidated")]
        seeds = [dict(s, validity="valid")]
        got = render(resolve(seeds, [], items=pool), items=pool, seeds=seeds)
        self.assertEqual([], got["scenes"], "失效单调：池说失效就不复活")
        self.assertEqual("invalidated_seed", got["manifest"]["excluded"][0]["reason_code"])

    def test_missing_slots_are_registered_verbatim_not_fabricated(self):
        s = item("s", sources=[], subject_ref="per_1", restatement="喜欢茶",
                 occurred_at=1000.0)
        resolved = resolve([s], [])
        got = render(resolved, items=[s], seeds=[s])
        missing = [x for x in got["manifest"]["excluded"] if x["category"] == "missing"]
        self.assertIn("root_source_missing", [x["reason_code"] for x in missing])
        for entry in missing:
            self.assertIn(entry["reason_code"], rc.MISSING_REASONS)
        self.assertEqual(resolved["missing_support"], got["provenance"]["missing_support"])

    def test_gapped_object_still_renders_with_missing_slots(self):
        s = item("s", sources=["e1"], subject_ref="per_1", restatement="喜欢茶",
                 occurred_at=1000.0, validity="unknown")
        got = render(resolve([s], []), items=[s], seeds=[s])
        self.assertEqual(1, len(got["scenes"]), "有缺槽仍是真实证据，渲染但如实标缺")
        entry = got["scenes"][0]["entries"][0]
        self.assertTrue(entry["missing_slots"], "缺槽如实登记，不补造")
        self.assertEqual("gapped", got["manifest"]["selected"][0]["reason"])
        self.assertEqual("partial", got["status"])

    def test_unknown_endpoint_is_dropped_not_rendered(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = render(resolve([s], [edge("s", "x")], items=[s]), items=[s], seeds=[s])
        self.assertIn({"object_id": "", "category": "dropped",
                       "reason_code": "unknown_neighbor", "count": 1},
                      got["manifest"]["excluded"])
        self.assertEqual(["s"], [e["object_id"] for sc in got["scenes"] for e in sc["entries"]])

    def test_excluded_categories_are_the_contract_ones(self):
        s = item("s", subject_ref="per_1", restatement="喜欢冰可乐", occurred_at=1000.0,
                 evidence_spans=[{"event_id": "e1", "quote": "喜欢冰可乐"}])
        bad = claim_item("bad", "讨厌冰可乐", sources=["e2"], validity="invalidated")
        con = claim_item("con", "讨厌冰可乐", sources=["e3"])
        edges = [edge("s", "x"), edge("s", "bad"), edge("s", "con")]
        got = render(resolve([s], edges, items=[s, bad, con]), items=[s, bad, con], seeds=[s])
        for entry in got["manifest"]["excluded"]:
            self.assertIn(entry["category"], re6.CATEGORIES)

    def test_missing_dropped_invalidated_conflicting_are_distinct(self):
        s = item("s", subject_ref="per_1", restatement="喜欢冰可乐", occurred_at=1000.0,
                 evidence_spans=[{"event_id": "e1", "quote": "喜欢冰可乐"}])
        bad = claim_item("bad", "讨厌冰可乐", sources=["e2"], validity="invalidated")
        con = claim_item("con", "讨厌冰可乐", sources=["e3"])
        edges = [edge("s", "x"), edge("s", "bad"), edge("s", "con")]
        got = render(resolve([s], edges, items=[s, bad, con], required_slots=("root_source",)),
                     items=[s, bad, con], seeds=[s])
        cats = {x["category"] for x in got["manifest"]["excluded"]}
        self.assertLessEqual({"missing", "dropped", "invalidated", "conflicting"}, cats)

    def test_no_evidence_aggregate_gap_is_carried(self):
        got = render(resolve([], []))
        self.assertEqual("not_found", got["status"])
        self.assertIn({"object_id": "", "category": "missing",
                       "reason_code": "no_evidence", "slot": "evidence"},
                      got["manifest"]["excluded"])

    def test_invalidated_objects_are_not_slot_checked_into_scenes(self):
        s = item("s", validity="invalidated")
        got = render(resolve([s], []), items=[s], seeds=[s])
        self.assertEqual([], got["scenes"])
        self.assertNotIn("s", [e["object_id"] for sc in got["scenes"] for e in sc["entries"]])


# ── 行为锁：status 优先级与降级承接 ──────────────────────────────────


class StatusTests(unittest.TestCase):
    def test_degraded_beats_everything_but_keeps_available_scenes(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        got = render(resolve([s], [edge("s", "n")], items=[s, n],
                             degradation=[{"store": "group", "reason": "absent", "detail": ""}]),
                     items=[s, n], seeds=[s])
        self.assertEqual("degraded", got["status"])
        self.assertEqual(2, len(got["scenes"]), "降级不抹掉可用证据，只如实标状态")
        self.assertEqual([{"store": "group", "reason": "absent", "detail": ""}],
                         got["degradation"])
        self.assertEqual(got["degradation"], got["manifest"]["degradation"])

    def test_degraded_empty_is_not_claimed_as_no_evidence(self):
        got = render(resolve([], [], degradation=[
            {"store": "cognition", "reason": "absent", "detail": ""}]))
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["manifest"]["excluded"], "读不到不等于没有")

    def test_conflicting_beats_partial(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n1 = claim_item("n1", "讨厌冰可乐", sources=["e2"])
        n2 = claim_item("n2", "喜欢茶", sources=["e3"])
        got = render(resolve([s], [edge("s", "n1"), edge("s", "n2")],
                             items=[s, n1, n2], max_degree=1),
                     items=[s, n1, n2], seeds=[s])
        self.assertEqual("conflicting", got["status"])

    def test_missing_upgrades_sufficient_to_partial(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"], validity="unknown")
        got = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        self.assertEqual("partial", got["status"])

    def test_r05_partial_is_passed_through(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        neighbors = [claim_item("n%d" % i, "喜欢咖啡%d" % i, sources=["e%d" % (i + 2)])
                     for i in range(4)]
        edges = [edge("s", n["object_id"]) for n in neighbors]
        got = render(resolve([s], edges, items=[s] + neighbors, max_degree=2),
                     items=[s] + neighbors, seeds=[s])
        self.assertEqual("partial", got["status"])

    def test_a_clean_render_is_sufficient(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        got = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        self.assertEqual("sufficient", got["status"])

    def test_nothing_renderable_is_not_found(self):
        got = render(resolve([], []))
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["scenes"])

    def test_conflicting_and_degraded_never_become_sufficient(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        for kw in ({}, {"degradation": [{"store": "group", "reason": "absent", "detail": ""}]}):
            got = render(resolve([s], [edge("s", "n")], items=[s, n], **kw),
                         items=[s, n], seeds=[s])
            self.assertNotEqual("sufficient", got["status"])


# ── 行为锁：预算超限大声登记，不静默截断 ─────────────────────────────


class BudgetTests(unittest.TestCase):
    def fixture(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        neighbors = [claim_item("n%d" % i, "喜欢咖啡%d" % i, sources=["e%d" % (i + 2)])
                     for i in range(3)]
        edges = [edge("s", n["object_id"]) for n in neighbors]
        pool = [s] + neighbors
        return resolve([s], edges, items=pool), pool, [s]

    def test_entry_budget_drops_loudly(self):
        resolved, pool, seeds = self.fixture()
        got = render(resolved, items=pool, seeds=seeds, max_entries=1)
        self.assertEqual(1, got["provenance"]["rendered"])
        drops = [x for x in got["manifest"]["excluded"] if x["category"] == "budget"]
        self.assertEqual(3, len(drops), "4 个可支撑对象，留 1  drop 3，逐条登记")
        self.assertEqual({"budget"}, {x["reason_code"] for x in drops})

    def test_scene_budget_drops_loudly(self):
        resolved, pool, seeds = self.fixture()
        got = render(resolved, items=pool, seeds=seeds, max_scenes=1)
        self.assertEqual(1, len(got["scenes"]))
        drops = [x for x in got["manifest"]["excluded"] if x["category"] == "budget"]
        self.assertTrue(drops, "超场景上限的条目要大声登记")
        self.assertEqual({"budget"}, {x["reason_code"] for x in drops})

    def test_zero_entry_budget_drops_all_loudly(self):
        resolved, pool, seeds = self.fixture()
        got = render(resolved, items=pool, seeds=seeds, max_entries=0)
        self.assertEqual([], got["scenes"])
        self.assertEqual(4, len([x for x in got["manifest"]["excluded"]
                                 if x["category"] == "budget"]))
        self.assertEqual("partial", got["status"], "有证据但预算 0 全 drop，不是 not_found")

    def test_budget_drop_reason_is_a03_budget(self):
        resolved, pool, seeds = self.fixture()
        got = render(resolved, items=pool, seeds=seeds, max_entries=1)
        drops = [x for x in got["manifest"]["excluded"] if x["category"] == "budget"]
        self.assertTrue(drops)
        for entry in drops:
            self.assertEqual("budget", entry["reason_code"])

    def test_budget_accounts_for_every_renderable_object(self):
        resolved, pool, seeds = self.fixture()
        got = render(resolved, items=pool, seeds=seeds, max_entries=2, max_scenes=2)
        rendered = got["provenance"]["rendered"]
        budget_drops = len([x for x in got["manifest"]["excluded"] if x["category"] == "budget"])
        self.assertEqual(got["provenance"]["renderable"], rendered + budget_drops,
                         "每个可支撑对象要么渲染要么大声 drop，不静默丢")


# ── 行为锁：确定性与只读 ─────────────────────────────────────────────


class DeterminismTests(unittest.TestCase):
    def fixture(self):
        s1 = claim_item("s1", "喜欢冰可乐", sources=["e1"])
        s2 = claim_item("s2", "讨厌冰可乐", sources=["e2"])
        n1 = claim_item("n1", "喜欢茶", sources=["e3"])
        n2 = item("n2", sources=[])
        edges = [edge("s1", "n1"), edge("s1", "n2"), edge("s2", "n1"), edge("s1", "s2")]
        pool = [s1, s2, n1, n2]
        return resolve([s1, s2], edges, items=pool), pool, [s1, s2]

    def test_the_same_input_gives_the_same_output(self):
        resolved, pool, seeds = self.fixture()
        first = render(resolved, items=pool, seeds=seeds)
        second = render(resolved, items=pool, seeds=seeds)
        self.assertEqual(canonical(first), canonical(second))

    def test_input_order_does_not_change_the_output(self):
        resolved, pool, seeds = self.fixture()
        forward = render(resolved, items=pool, seeds=seeds)
        backward = render(resolved, items=list(reversed(pool)), seeds=list(reversed(seeds)))
        self.assertEqual(canonical(forward), canonical(backward))

    def test_the_inputs_are_not_mutated(self):
        resolved, pool, seeds = self.fixture()
        context = {"logical_turn_id": "t1"}
        before = canonical([resolved, pool, seeds, context])
        render(resolved, items=pool, seeds=seeds, context=context)
        self.assertEqual(before, canonical([resolved, pool, seeds, context]))

    def test_provenance_carries_no_clock(self):
        resolved, pool, seeds = self.fixture()
        got = render(resolved, items=pool, seeds=seeds)
        self.assertNotIn("seconds", got["provenance"])
        self.assertNotIn("ts", got["provenance"])

    def test_score_parts_do_not_affect_the_output(self):
        s = claim_item("s", "喜欢茶", sources=["e1"],
                       match={"clues": [], "score_parts": {"lexical": 0.9}})
        n = claim_item("n", "喜欢咖啡", sources=["e2"],
                       match={"clues": [], "score_parts": {"lexical": 0.1}})
        first = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        s["match"]["score_parts"]["lexical"] = 0.0
        n["match"]["score_parts"]["lexical"] = 99.0
        second = render(resolve([s], [edge("s", "n")], items=[s, n]), items=[s, n], seeds=[s])
        self.assertEqual(canonical(first), canonical(second),
                         "改 score_parts 影响了输出，说明 R06 偷读了 R03 的分数")
        for scene in first["scenes"]:
            for entry in scene["entries"]:
                self.assertNotIn("score", entry)

    def test_duplicate_rows_do_not_duplicate_scenes(self):
        resolved, pool, seeds = self.fixture()
        baseline = render(resolved, items=pool, seeds=seeds)
        got = render(resolved, items=pool + pool, seeds=seeds + seeds)
        self.assertEqual(canonical(baseline["scenes"]), canonical(got["scenes"]))
        self.assertEqual(canonical(baseline["manifest"]["selected"]),
                         canonical(got["manifest"]["selected"]))


# ── 行为锁：接口形状与深拷贝 ─────────────────────────────────────────


class InterfaceTests(unittest.TestCase):
    def test_the_result_shape_is_the_contract_one(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        got = render(resolve([s], []), items=[s], seeds=[s])
        self.assertEqual({"status", "scenes", "manifest", "degradation", "provenance"},
                         set(got))
        self.assertEqual({"status", "context", "selected", "excluded", "degradation",
                          "counts"}, set(got["manifest"]))
        prov = got["provenance"]
        for key in ("source_status", "adjudicated", "renderable", "rendered", "scenes",
                    "conflict_objects", "missing_support", "dropped", "conflicts",
                    "verdicts", "root_canon", "budget", "limitations"):
            self.assertIn(key, prov)
        self.assertEqual(prov["limitations"], re6.LIMITATIONS)
        self.assertEqual(prov["rendered"], got["manifest"]["counts"]["entries"])

    def test_the_results_are_deep_copies(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        resolved = resolve([s], [edge("s", "n")], items=[s, n])
        context = {"logical_turn_id": "t1"}
        before = canonical([resolved, s, n, context])
        got = render(resolved, items=[s, n], seeds=[s], context=context)
        got["scenes"][0]["entries"][0]["restatement"] = "mutated"
        got["scenes"][0]["entries"][0]["verdict_refs"][0]["verdict"] = "mutated"
        got["manifest"]["selected"][0]["reason"] = "mutated"
        got["manifest"]["excluded"].append({"object_id": "fake"})
        got["manifest"]["context"]["logical_turn_id"] = "mutated"
        got["provenance"]["verdicts"][0]["verdict"] = "mutated"
        got["provenance"]["root_canon"]["e1"] = "mutated"
        self.assertEqual(before, canonical([resolved, s, n, context]),
                         "改输出影响了输入，就不是深拷贝/只读")

    def test_none_and_non_dict_inputs_are_tolerated(self):
        got = re6.render_evidence(None)
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["scenes"])
        self.assertEqual([], got["degradation"])
        assessed = re6.render_evidence({"status": "degraded"})
        self.assertEqual("degraded", assessed["status"])

    def test_context_is_carried_verbatim(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        context = {"logical_turn_id": "t9", "request_attempt_id": "a1"}
        got = render(resolve([s], []), items=[s], seeds=[s], context=context)
        self.assertEqual(context, got["manifest"]["context"])

    def test_render_without_seeds_covers_path_seeds(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        resolved = resolve([s], [edge("s", "n")], items=[s, n])
        got = render(resolved, items=[s, n])
        ids = {e["object_id"] for sc in got["scenes"] for e in sc["entries"]}
        self.assertEqual({"s", "n"}, ids, "没给种子时从 paths 回查 seed 与邻居")

    def test_provenance_carries_r05_verbatim(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        resolved = resolve([s], [edge("s", "n")], items=[s, n])
        got = render(resolved, items=[s, n], seeds=[s])
        self.assertEqual(resolved["missing_support"], got["provenance"]["missing_support"])
        self.assertEqual(resolved["provenance"]["dropped"], got["provenance"]["dropped"])
        self.assertEqual(resolved["conflicts"], got["provenance"]["conflicts"])
        self.assertEqual(resolved["provenance"]["verdicts"], got["provenance"]["verdicts"])
        self.assertEqual(resolved["provenance"]["root_canon"], got["provenance"]["root_canon"])
        self.assertEqual(resolved["status"], got["provenance"]["source_status"])

    def test_render_equals_assess_then_render(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        edges = [edge("s", "n")]
        via_resolve = render(rc.resolve_ripple([s], edges, items=[s, n]), items=[s, n], seeds=[s])
        expanded = rp.expand_paths([s], edges, items=[s, n])
        assessed = rc.assess_expansion(expanded, seeds=[s], items=[s, n])
        via_assess = render(assessed, items=[s, n], seeds=[s])
        self.assertEqual(canonical(via_resolve), canonical(via_assess))


# ── 端到端：接真实 R01→R02→R03→R04→R05→R06（测试可 import，被测模块不 import）──


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r06_", ignore_cleanup_errors=True)
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

    def chain(self, ctx=None, budget=2, **render_kw):
        ctx = ctx if ctx is not None else self.ctx()
        fetched = ro.retrieve_evidence(ctx)
        edges = ae.project_edges(fetched["items"], fetched["degradation"])["edges"]
        recalled = rs.recall(fetched["items"], dict(ctx, retrieval_budget=budget))
        resolved = rc.resolve_ripple(recalled["items"], edges, items=fetched["items"],
                                     degradation=fetched["degradation"])
        got = re6.render_evidence(resolved, items=fetched["items"],
                                  seeds=recalled["items"], **render_kw)
        return got, resolved, fetched, recalled

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
        got, _resolved, _fetched, _recalled = self.chain()
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["scenes"])
        self.assertEqual([], got["manifest"]["excluded"], "读不到不等于没有")
        self.assertEqual({"absent"}, {d["reason"] for d in got["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "R01→…→R06 只读链路顺手建了库/目录，就不再是只读")

    def test_real_independent_group_facts_render_scenes(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1"),
                          self.fact("f2", "喜欢咖啡", "ge2", ts=1001.0)])
        got, resolved, _fetched, recalled = self.chain(budget=2)
        self.assertEqual(2, len(recalled["items"]))
        self.assertEqual([], resolved["conflicts"])
        self.assertEqual(2, got["provenance"]["rendered"], "两条独立真实群事实都入场景")
        self.assertEqual("degraded", got["status"], "其余库 absent → 整体 degraded，但证据照渲")
        ids = {e["object_id"] for sc in got["scenes"] for e in sc["entries"]}
        self.assertEqual({"group:g1:f1", "group:g1:f2"}, ids)

    def test_real_conflict_is_excluded_from_scenes(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1"),
                          self.fact("f2", "讨厌冰可乐", "ge2", ts=1001.0)])
        got, resolved, _fetched, _recalled = self.chain(budget=2)
        self.assertEqual(1, len(resolved["conflicts"]))
        self.assertEqual([], got["scenes"], "真实矛盾对象绝不入场景")
        conflicting = [x for x in got["manifest"]["excluded"] if x["category"] == "conflicting"]
        self.assertEqual({"group:g1:f1", "group:g1:f2"}, {x["object_id"] for x in conflicting})
        self.assertEqual({"explicit_negation_opposite"}, {x["reason_code"] for x in conflicting})

    def test_real_same_root_groups_into_one_scene(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1"),
                          self.fact("f2", "喜欢咖啡", "ge1", ts=1001.0)])
        got, _resolved, _fetched, _recalled = self.chain(budget=2)
        self.assertEqual(1, len(got["scenes"]), "同根源 ge1 归一个场景")
        self.assertEqual(2, got["scenes"][0]["entry_count"])
        self.assertEqual("ge1", got["scenes"][0]["root_representative"])

    def test_the_full_readonly_pipeline_rewrites_no_asset(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1")])
        self.belief("命题一", "我喜欢冰可乐", "r06:one")
        before = snapshot(self.root)
        self.chain()
        self.assertEqual(before, snapshot(self.root))

    def test_real_legacy_belief_missing_slots_are_carried_not_fabricated(self):
        bid = sm.add_belief("某个旧认识", subject_qq="1001", is_legacy=True, ts=900.0)
        ctx = self.ctx(allowed_source_scopes=["private:1001", "group:g1", "cognition"])
        got, resolved, _fetched, _recalled = self.chain(ctx=ctx)
        self.assertEqual(resolved["missing_support"], got["provenance"]["missing_support"])
        codes = {x["reason_code"] for x in got["manifest"]["excluded"]
                 if x["category"] == "missing" and x["object_id"] == bid}
        self.assertLessEqual({"root_source_missing", "original_quote_missing",
                              "provenance_unverified"}, codes, "legacy 缺依据大声登记，不补造")


if __name__ == "__main__":
    unittest.main()