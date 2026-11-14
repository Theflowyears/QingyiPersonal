# -*- coding: utf-8 -*-
"""R05 缺失槽位与冲突判定：R04 一跳输出之上复用 M04 裁决，缺槽大声登记。

行为锁覆盖验收判据：只有 M04 contradiction 才升级 conflicting（喜欢/不喜欢
独立根源必须判矛盾、多值偏好并存、同根源转述不算独立反例、多根源交集归一、
不同主体不配对、unclassified 不乱归、不相交有效期是历史变化）；缺失槽位逐对象
登记稳定 reason_code，不补造填充；missing/dropped/invalidated/conflicting 四种
口径分开；invalidated 不复活；degraded 原样承接且压过 conflicting；确定性排序、
无时钟、不读 score_parts、输入不被修改、输出深拷贝。
AST 锁钉死「复用而非重造」：必须 import 并调用 M04 claim_conflicts 与 R04
ripple_paths；不许长第二套矛盾家族引擎（家族词表/剥壳/裁决函数都不许重定义）；
不许伪造 verdict 记录；conflicting 常量只出现在契约与 status 归结处；真只读、
不 import R01/R03/各库、无 SQL 写/建库、不冻路径、无打分机器。

R05 是纯函数：调用方把种子、边、items 池与 degradation 传进来。这里既用手工
item/edge 精确核对规则，也接真实 R01→R02→R03→R04→R05 输出做端到端验证
（测试可以 import R01/R02/R03，被测模块 ripple_conflicts 本身不 import）。
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

import ripple_conflicts as rc
import claim_conflicts as cc
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


# ── AST 静态锁：复用 M04/R04，不长第二套引擎，真只读 ─────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "ripple_conflicts.py"), encoding="utf-8") as handle:
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

    def test_the_import_surface_is_copy_plus_m04_and_r04(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"copy", "claim_conflicts", "ripple_paths"},
                             "多出来的 import 面要解释")
        self.assertLessEqual({"claim_conflicts", "ripple_paths"}, imported,
                             "R05 必须复用 M04 裁决与 R04 扩散，不许绕开")

    def test_it_reuses_m04_adjudication_and_r04_expansion(self):
        calls = {(n.func.value.id, n.func.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name)}
        self.assertIn(("cc", "adjudicate"), calls, "矛盾裁决必须走 M04 adjudicate")
        self.assertIn(("rp", "expand_paths"), calls, "扩散必须走 R04 expand_paths")

    def test_it_does_not_import_r01_r03_or_any_store(self):
        banned = {"retrieval_objects", "recall_scoring", "association_edges",
                  "self_model", "persona_store", "group_memory_store",
                  "knowledge_base", "vision_notes", "sqlite3", "os", "time",
                  "random", "json"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             "R05 应是纯函数，不该 import %s" % sorted(names & banned))

    def test_no_second_conflict_family_engine(self):
        """家族词表/剥壳/裁决函数都不许在 R05 重定义：M04 是唯一引擎。"""
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
                                 "R05 不许自带家族词表 %s" % node.value)
        for token in ("_FAMILY_MARKERS", "_NEGATION_PREFIXES", "_PREFERENCE_POSITIVE",
                      "_PREFERENCE_NEGATIVE", "_FAMILY_LEADERS"):
            self.assertNotIn(token, self.source, "出现了 M04 的剥壳词表 %s" % token)

    def test_no_verdict_record_is_fabricated(self):
        """verdict 记录只能来自 M04：R05 不许构造带 verdict 键的字典。"""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    self.assertFalse(isinstance(key, ast.Constant) and key.value == "verdict",
                                     "R05 自己伪造了裁决记录")
        verdict_constants = [n for n in ast.walk(self.tree)
                             if isinstance(n, ast.Constant) and isinstance(n.value, str)
                             and n.value in cc.VERDICTS]
        self.assertTrue(verdict_constants, "应当有一处按 M04 verdict 过滤 conflicts")
        for node in verdict_constants:
            self.assertEqual("contradiction", node.value,
                             "除 contradiction 过滤外不许引用其它 verdict 字面量")
            self.assertIsInstance(self.parents.get(node), ast.Compare,
                                  "verdict 字面量只许用于比较过滤，不许用于赋值伪造")

    def test_conflicting_constant_only_in_contract_and_status_resolution(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and node.value == "conflicting":
                ok = False
                for ancestor in self._ancestors(node):
                    if isinstance(ancestor, ast.FunctionDef) and ancestor.name == "_resolve_status":
                        ok = True
                    if isinstance(ancestor, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == "STATUSES"
                            for t in ancestor.targets):
                        ok = True
                self.assertTrue(ok, "conflicting 出现在了契约与 status 归结之外")

    def test_the_module_never_writes_or_creates_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "connect", "atomic_write_json",
                     "save", "add", "add_belief", "record_event", "write", "writelines",
                     "dump", "touch"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读判定里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "open", "纯函数不该打开任何文件")
        for token in ("INSERT INTO", "INSERT OR", "UPDATE ", "DELETE FROM",
                      "REPLACE INTO", "CREATE TABLE", "CREATE INDEX", "DROP TABLE",
                      "sqlite", "executescript", "PRAGMA"):
            self.assertNotIn(token, self.source,
                             "只读判定里出现建库/写库语句 %s" % token)

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
                                 "融合打分是 R03，R05 不许读分数")
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
                  "recall", "score_item", "read_all_candidates"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                self.assertNotIn(name, wanted,
                                 "R05 不该调用上游入口 %s，输入由调用方传入" % name)

    def test_the_contract_constants(self):
        self.assertEqual(ro.STATUSES, rc.STATUSES, "status 词表必须与 R01 契约一致")
        self.assertIn("conflicting", rc.STATUSES)
        self.assertEqual(("root_source", "original_quote", "subject_ref",
                          "occurrence_time", "verified_provenance"), rc.SLOTS)
        self.assertEqual(("no_evidence", "root_source_missing", "original_quote_missing",
                          "subject_ref_missing", "occurrence_time_missing",
                          "provenance_unverified"), rc.MISSING_REASONS)
        self.assertIsInstance(rc.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(rc.LIMITATIONS), 5)
        limits = " ".join(rc.LIMITATIONS)
        for undone in ("R03", "R06", "R08", "M04"):
            self.assertIn(undone, limits, "limitations 必须如实声明没做的事")


# ── 行为锁：矛盾判定完全来自 M04 ─────────────────────────────────────


class ConflictTests(unittest.TestCase):
    def test_opposite_preferences_from_independent_roots_conflict(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual("conflicting", got["status"])
        self.assertEqual(1, len(got["conflicts"]))
        record = got["conflicts"][0]
        self.assertEqual("contradiction", record["verdict"])
        self.assertEqual("explicit_negation_opposite", record["reason_code"])
        self.assertEqual({"s", "n"}, {record["left_id"], record["right_id"]})
        self.assertEqual("per_1", record["subject_ref"])
        self.assertEqual("self_report_preference", record["predicate_family"])

    def test_single_valued_residence_conflict(self):
        s = claim_item("s", "住在北京", sources=["e1"])
        n = claim_item("n", "住在上海", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual("conflicting", got["status"])
        self.assertEqual("single_valued_value_conflict", got["conflicts"][0]["reason_code"])

    def test_multi_valued_preferences_coexist_not_conflict(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertNotEqual("conflicting", got["status"])
        self.assertEqual([], got["conflicts"])
        self.assertEqual(1, got["provenance"]["verdict_counts"]["coexists"])

    def test_same_root_restatement_is_not_an_independent_counterexample(self):
        s1 = claim_item("s1", "喜欢冰可乐", sources=["e1"])
        s2 = claim_item("s2", "讨厌冰可乐", sources=["e1"])
        got = rc.resolve_ripple([s1, s2], [], items=[s1, s2])
        self.assertEqual([], got["conflicts"], "同根源转述不算独立反例，绝不判矛盾")
        self.assertEqual("not_found", got["status"])
        self.assertEqual(1, len(got["provenance"]["verdicts"]))
        self.assertEqual("disputed", got["provenance"]["verdicts"][0]["verdict"])
        self.assertEqual("same_root_not_independent",
                         got["provenance"]["verdicts"][0]["reason_code"])

    def test_intersecting_multi_roots_are_not_independent(self):
        # 种子×种子：R04 不去重种子间的根源交集，R05 的归一守卫在这里接住。
        s1 = claim_item("s1", "喜欢冰可乐", sources=["e1", "e2"])
        s2 = claim_item("s2", "讨厌冰可乐", sources=["e2", "e3"])
        got = rc.resolve_ripple([s1, s2], [], items=[s1, s2])
        self.assertEqual([], got["conflicts"], "根源有交集就不是独立反例（R04 同口径）")
        self.assertEqual("disputed", got["provenance"]["verdicts"][0]["verdict"])
        self.assertEqual("same_root_not_independent",
                         got["provenance"]["verdicts"][0]["reason_code"])
        self.assertEqual({"e1": "e1", "e2": "e1", "e3": "e1"},
                         got["provenance"]["root_canon"])
        # 种子→邻居：R04 的根源去重先把同根邻居并掉，R05 不重复判。
        n = claim_item("n", "讨厌冰可乐", sources=["e2", "e3"])
        via_path = rc.resolve_ripple([s1], [edge("s1", "n")], items=[s1, n])
        self.assertEqual([], via_path["paths"])
        self.assertEqual([], via_path["conflicts"])
        self.assertEqual(1, via_path["provenance"]["dropped"].get("same_root"))

    def test_different_subjects_are_never_paired(self):
        s = claim_item("s", "喜欢冰可乐", subject="per_1", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", subject="per_2", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual([], got["conflicts"])
        self.assertEqual(0, got["provenance"]["pairs"], "不同主体根本不配对（M04 第一段）")

    def test_unclassified_family_defers_and_is_never_guessed(self):
        s = claim_item("s", "宇宙在膨胀", sources=["e1"])
        n = claim_item("n", "宇宙在收缩", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual([], got["conflicts"])
        self.assertEqual("deferred", got["provenance"]["verdicts"][0]["verdict"])
        self.assertEqual("family_unclassified",
                         got["provenance"]["verdicts"][0]["reason_code"])

    def test_disjoint_validity_is_historical_change_not_conflict(self):
        s = claim_item("s", "住在北京", sources=["e1"], valid_from=1000.0, valid_to=2000.0)
        n = claim_item("n", "住在上海", sources=["e2"], valid_from=3000.0, valid_to=4000.0)
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual([], got["conflicts"])
        self.assertEqual("historical_change", got["provenance"]["verdicts"][0]["verdict"])
        self.assertEqual("disjoint_validity",
                         got["provenance"]["verdicts"][0]["reason_code"])

    def test_conflict_records_come_verbatim_from_m04(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        left = {"candidate_id": "s", "subject_ref": "per_1", "object_value": "喜欢冰可乐",
                "root_event_id": "e1", "evidence": [{"event_id": "e1"}]}
        right = {"candidate_id": "n", "subject_ref": "per_1", "object_value": "讨厌冰可乐",
                 "root_event_id": "e2", "evidence": [{"event_id": "e2"}]}
        self.assertEqual(cc.adjudicate([left, right]), got["provenance"]["verdicts"],
                         "R05 的裁决记录必须与 M04 逐字一致，不添油加醋")

    def test_seed_seed_conflict_without_any_path(self):
        s1 = claim_item("s1", "喜欢冰可乐", sources=["e1"])
        s2 = claim_item("s2", "讨厌冰可乐", sources=["e2"])
        got = rc.resolve_ripple([s1, s2], [edge("s1", "s2")], items=[s1, s2],
                                max_expanded=0)
        self.assertEqual([], got["paths"], "种子重叠不算扩散，预算 0 也不扩")
        self.assertEqual("conflicting", got["status"],
                         "矛盾判定不依赖扩散预算：种子之间也要裁决")
        self.assertEqual(1, len(got["conflicts"]))

    def test_disputed_and_deferred_are_recorded_not_silenced(self):
        s = claim_item("s", "宇宙在膨胀", sources=["e1"])
        n = claim_item("n", "宇宙在收缩", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual(set(cc.VERDICTS), set(got["provenance"]["verdict_counts"]))
        self.assertEqual(1, got["provenance"]["verdict_counts"]["deferred"])
        self.assertEqual(got["provenance"]["verdicts"],
                         [r for r in got["provenance"]["verdicts"]])
        self.assertEqual(1, got["provenance"]["pairs"])

    def test_semantic_hint_is_recorded_but_never_overrides(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        hints = {("s", "n"): {"relation": "same", "source": "test_fixture"}}
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n], semantic_hints=hints)
        self.assertEqual("conflicting", got["status"], "语义提示绝不覆盖结构判定")
        record = got["conflicts"][0]
        self.assertEqual({"relation": "same", "source": "test_fixture"},
                         record["semantic_hint"])
        self.assertIn("semantic_hint_recorded", record["reasons"])

    def test_an_invalidated_neighbor_cannot_create_a_conflict(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"], validity="invalidated")
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual([], got["conflicts"])
        self.assertEqual([], got["paths"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_neighbor"))

    def test_an_invalidated_seed_cannot_create_a_conflict(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"], validity="invalidated")
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        got = rc.resolve_ripple([s, n], [], items=[s, n])
        self.assertEqual([], got["conflicts"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_seed"))
        # 失效单调：池里登记失效，种子副本说有效也不复活、不参与裁决。
        pool = [dict(s, validity="invalidated"), n]
        seeds = [dict(s, validity="valid"), n]
        got = rc.resolve_ripple(seeds, [], items=pool)
        self.assertEqual([], got["conflicts"])
        self.assertEqual(1, got["provenance"]["adjudicated"])


# ── 行为锁：缺失槽位大声登记，不补造不静默 ───────────────────────────


class MissingSlotTests(unittest.TestCase):
    def test_a_rootless_item_reports_root_source_missing(self):
        got = rc.resolve_ripple([item("s")], [])
        self.assertIn({"slot": "root_source", "reason_code": "root_source_missing",
                       "object_id": "s"}, got["missing_support"])

    def test_no_verbatim_quote_reports_original_quote_missing(self):
        got = rc.resolve_ripple([item("s", sources=["e1"])], [])
        self.assertIn({"slot": "original_quote", "reason_code": "original_quote_missing",
                       "object_id": "s"}, got["missing_support"])
        self.assertNotIn("root_source_missing",
                         [m["reason_code"] for m in got["missing_support"]])

    def test_no_subject_reports_subject_ref_missing(self):
        got = rc.resolve_ripple([item("s", sources=["e1"])], [])
        self.assertIn("subject_ref_missing",
                      [m["reason_code"] for m in got["missing_support"]])

    def test_no_occurrence_time_reports_occurrence_time_missing(self):
        got = rc.resolve_ripple([item("s", sources=["e1"])], [])
        self.assertIn("occurrence_time_missing",
                      [m["reason_code"] for m in got["missing_support"]])
        grounded = rc.resolve_ripple([item("s", sources=["e1"], occurred_at=0)], [])
        self.assertIn("occurrence_time_missing",
                      [m["reason_code"] for m in grounded["missing_support"]],
                      "0/未知时间不算可定位的发生时间")

    def test_unknown_validity_reports_provenance_unverified(self):
        got = rc.resolve_ripple([item("s", sources=["e1"], validity="unknown")], [])
        self.assertIn("provenance_unverified",
                      [m["reason_code"] for m in got["missing_support"]])

    def test_fully_grounded_items_have_no_missing(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual([], got["missing_support"])
        self.assertEqual("sufficient", got["status"])

    def test_no_evidence_at_all_reports_aggregate_no_evidence(self):
        got = rc.resolve_ripple([], [])
        self.assertEqual([{"slot": "evidence", "reason_code": "no_evidence",
                           "object_id": ""}], got["missing_support"])
        self.assertEqual("not_found", got["status"])

    def test_degraded_empty_is_not_claimed_as_no_evidence(self):
        got = rc.resolve_ripple([], [], degradation=[
            {"store": "cognition", "reason": "absent", "detail": ""}])
        self.assertEqual([], got["missing_support"],
                         "读不到不等于没有：降级时不喊 no_evidence")
        self.assertEqual("degraded", got["status"])

    def test_required_slots_can_be_narrowed(self):
        got = rc.resolve_ripple([item("s")], [], required_slots=("root_source",))
        self.assertEqual([{"slot": "root_source", "reason_code": "root_source_missing",
                           "object_id": "s"}], got["missing_support"])

    def test_an_unknown_required_slot_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            rc.resolve_ripple([item("s")], [], required_slots=("made_up_slot",))

    def test_none_required_slots_disables_slot_checks(self):
        got = rc.resolve_ripple([item("s")], [], required_slots=None)
        self.assertEqual([], got["missing_support"])

    def test_missing_entries_are_sorted_and_stable(self):
        got = rc.resolve_ripple([item("s2"), item("s1")], [])
        keys = [(m["slot"], m["object_id"]) for m in got["missing_support"]]
        self.assertEqual(sorted(keys), keys)
        again = rc.resolve_ripple([item("s2"), item("s1")], [])
        self.assertEqual(canonical(got), canonical(again))

    def test_missing_dropped_invalidated_and_conflicting_are_distinct(self):
        s = item("s", subject_ref="per_1", restatement="喜欢冰可乐", occurred_at=1000.0,
                 evidence_spans=[{"event_id": "e1", "quote": "喜欢冰可乐"}])
        bad = claim_item("bad", "讨厌冰可乐", sources=["e2"], validity="invalidated")
        edges = [edge("s", "x"), edge("s", "bad")]
        got = rc.resolve_ripple([s], edges, items=[s, bad],
                                required_slots=("root_source",))
        # missing：检索到的 s 缺根源；dropped：未知端点 x 与失效邻居 bad（R04 口径）。
        self.assertEqual([{"slot": "root_source", "reason_code": "root_source_missing",
                           "object_id": "s"}], got["missing_support"])
        self.assertEqual(1, got["provenance"]["dropped"].get("unknown_neighbor"))
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_neighbor"))
        self.assertEqual([], got["conflicts"])
        for entry in got["missing_support"]:
            self.assertIn(entry["reason_code"], rc.MISSING_REASONS)

    def test_invalidated_objects_are_not_slot_checked(self):
        s = item("s", validity="invalidated")
        got = rc.resolve_ripple([s], [])
        self.assertEqual([{"slot": "evidence", "reason_code": "no_evidence",
                           "object_id": ""}], got["missing_support"])
        self.assertEqual(1, got["provenance"]["dropped"].get("invalidated_seed"))
        self.assertNotIn("s", [m["object_id"] for m in got["missing_support"]
                               if m["object_id"]])


# ── 行为锁：status 优先级与降级承接 ──────────────────────────────────


class StatusTests(unittest.TestCase):
    def test_degraded_beats_conflicting_but_keeps_the_conflicts(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n],
                                degradation=[{"store": "group", "reason": "absent",
                                              "detail": ""}])
        self.assertEqual("degraded", got["status"])
        self.assertEqual(1, len(got["conflicts"]), "降级压过 status，但矛盾不静默")
        self.assertEqual([{"store": "group", "reason": "absent", "detail": ""}],
                         got["degradation"])

    def test_conflicting_beats_partial(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n1 = claim_item("n1", "讨厌冰可乐", sources=["e2"])
        n2 = claim_item("n2", "喜欢茶", sources=["e3"])
        got = rc.resolve_ripple([s], [edge("s", "n1"), edge("s", "n2")],
                                items=[s, n1, n2], max_degree=1)
        self.assertEqual(1, got["provenance"]["dropped"].get("degree_cap"))
        self.assertEqual("conflicting", got["status"])

    def test_missing_slots_upgrade_sufficient_to_partial(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"], validity="unknown")
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual(1, len(got["paths"]))
        self.assertEqual("partial", got["status"])
        self.assertEqual("provenance_unverified", got["missing_support"][0]["reason_code"])

    def test_r04_partial_is_passed_through(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        neighbors = [claim_item("n%d" % i, "喜欢咖啡%d" % i, sources=["e%d" % (i + 2)])
                     for i in range(4)]
        edges = [edge("s", n["object_id"]) for n in neighbors]
        got = rc.resolve_ripple([s], edges, items=[s] + neighbors, max_degree=2)
        self.assertEqual("partial", got["status"])
        self.assertEqual(2, got["provenance"]["dropped"].get("degree_cap"))

    def test_nothing_found_without_degradation_is_not_found(self):
        got = rc.resolve_ripple([claim_item("s", "喜欢茶", sources=["e1"])], [])
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["paths"])

    def test_a_clean_expansion_is_sufficient(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual("sufficient", got["status"])

    def test_no_conflicting_without_an_m04_verdict(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        with patch.object(rc.cc, "adjudicate", return_value=[]):
            got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertNotEqual("conflicting", got["status"],
                            "没有 M04 裁决就不许声称 conflicting")
        self.assertEqual([], got["conflicts"])

    def test_conflicting_comes_only_through_the_m04_verdict_channel(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        record = {"verdict": "contradiction", "reason_code": "explicit_negation_opposite",
                  "left_id": "s", "right_id": "n", "subject_ref": "per_1",
                  "predicate_family": "self_report_preference"}
        with patch.object(rc.cc, "adjudicate", return_value=[record]):
            got = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual("conflicting", got["status"])
        self.assertEqual([record], got["conflicts"])


# ── 行为锁：确定性与只读 ─────────────────────────────────────────────


class DeterminismTests(unittest.TestCase):
    def fixture(self):
        s1 = claim_item("s1", "喜欢冰可乐", sources=["e1"])
        s2 = claim_item("s2", "讨厌冰可乐", sources=["e2"])
        n1 = claim_item("n1", "喜欢茶", sources=["e3"])
        n2 = item("n2", sources=[])
        edges = [edge("s1", "n1"), edge("s1", "n2"), edge("s2", "n1"),
                 edge("s1", "s2")]
        return [s1, s2], edges, [s1, s2, n1, n2]

    def test_the_same_input_gives_the_same_output(self):
        seeds, edges, pool = self.fixture()
        first = rc.resolve_ripple(seeds, edges, items=pool)
        second = rc.resolve_ripple(seeds, edges, items=pool)
        self.assertEqual(canonical(first), canonical(second))

    def test_input_order_does_not_change_the_output(self):
        seeds, edges, pool = self.fixture()
        forward = rc.resolve_ripple(seeds, edges, items=pool)
        backward = rc.resolve_ripple(list(reversed(seeds)), list(reversed(edges)),
                                     items=list(reversed(pool)))
        self.assertEqual(canonical(forward), canonical(backward))

    def test_the_inputs_are_not_mutated(self):
        seeds, edges, pool = self.fixture()
        degradation = [{"store": "group", "reason": "absent", "detail": ""}]
        before = canonical([seeds, edges, pool, degradation])
        rc.resolve_ripple(seeds, edges, items=pool, degradation=degradation)
        self.assertEqual(before, canonical([seeds, edges, pool, degradation]))

    def test_provenance_carries_no_clock(self):
        seeds, edges, pool = self.fixture()
        got = rc.resolve_ripple(seeds, edges, items=pool)
        self.assertNotIn("seconds", got["provenance"])
        self.assertNotIn("ts", got["provenance"])

    def test_duplicate_rows_do_not_duplicate_conflicts(self):
        seeds, edges, pool = self.fixture()
        baseline = rc.resolve_ripple(seeds, edges, items=pool)
        got = rc.resolve_ripple(seeds + seeds, edges + edges, items=pool + pool)
        # 裁决/缺失/状态/路径必须完全一致；R04 的 deduped 登记随重复边行增长
        # （前序口径，只报告不修），不在这里强求逐字一致。
        self.assertEqual(canonical(baseline["conflicts"]), canonical(got["conflicts"]))
        self.assertEqual(canonical(baseline["provenance"]["verdicts"]),
                         canonical(got["provenance"]["verdicts"]))
        self.assertEqual(canonical(baseline["missing_support"]),
                         canonical(got["missing_support"]))
        self.assertEqual(baseline["status"], got["status"])
        self.assertEqual({p["path_id"] for p in baseline["paths"]},
                         {p["path_id"] for p in got["paths"]})

    def test_score_parts_do_not_affect_the_output(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"],
                       match={"clues": [], "score_parts": {"lexical": 0.9}})
        n = claim_item("n", "讨厌冰可乐", sources=["e2"],
                       match={"clues": [], "score_parts": {"lexical": 0.1}})
        first = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        s["match"]["score_parts"]["lexical"] = 0.0
        n["match"]["score_parts"]["lexical"] = 99.0
        second = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        self.assertEqual(canonical(first), canonical(second),
                         "改 score_parts 影响了输出，说明 R05 偷读了 R03 的分数")
        for path in first["paths"]:
            self.assertNotIn("score", path)
            self.assertNotIn("score", path["basis"])
        for record in first["conflicts"]:
            self.assertNotIn("score", record)


# ── 行为锁：接口形状与深拷贝 ─────────────────────────────────────────


class InterfaceTests(unittest.TestCase):
    def test_resolve_ripple_equals_expand_then_assess(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        edges = [edge("s", "n")]
        expanded = rp.expand_paths([s], edges, items=[s, n])
        via_assess = rc.assess_expansion(expanded, seeds=[s], items=[s, n])
        via_resolve = rc.resolve_ripple([s], edges, items=[s, n])
        self.assertEqual(canonical(via_assess), canonical(via_resolve))

    def test_assess_expansion_accepts_r04_output_directly(self):
        s = claim_item("s", "喜欢茶", sources=["e1"])
        n = claim_item("n", "喜欢咖啡", sources=["e2"])
        expanded = rp.expand_paths([s], [edge("s", "n")], items=[s, n])
        got = rc.assess_expansion(expanded, seeds=[s], items=[s, n])
        self.assertEqual("sufficient", got["status"])
        self.assertEqual(expanded["paths"], got["paths"])
        self.assertEqual(1, got["provenance"]["expanded"])

    def test_assess_without_seeds_only_covers_path_seeds(self):
        s1 = claim_item("s1", "喜欢冰可乐", sources=["e1"])
        s2 = claim_item("s2", "讨厌冰可乐", sources=["e2"])
        expanded = rp.expand_paths([s1, s2], [], items=[s1, s2])
        without = rc.assess_expansion(expanded, items=[s1, s2])
        self.assertEqual([], without["conflicts"],
                         "没给种子又没扩出路径时覆盖不到种子×种子，如实不判")
        self.assertEqual(0, without["provenance"]["adjudicated"])
        with_seeds = rc.assess_expansion(expanded, seeds=[s1, s2], items=[s1, s2])
        self.assertEqual(1, len(with_seeds["conflicts"]))

    def test_none_and_non_dict_inputs_are_tolerated(self):
        got = rc.resolve_ripple(None, None)
        self.assertEqual("not_found", got["status"])
        self.assertEqual([], got["paths"])
        self.assertEqual([], got["degradation"])
        assessed = rc.assess_expansion(None)
        self.assertEqual("not_found", assessed["status"])
        self.assertEqual([{"slot": "evidence", "reason_code": "no_evidence",
                           "object_id": ""}], assessed["missing_support"])

    def test_the_result_shape_is_the_contract_one(self):
        got = rc.resolve_ripple([claim_item("s", "喜欢茶", sources=["e1"])], [])
        self.assertEqual({"status", "paths", "conflicts", "missing_support",
                          "degradation", "provenance"}, set(got))
        prov = got["provenance"]
        for key in ("candidates", "expanded", "deduped", "dropped", "budget",
                      "adjudicated", "pairs", "verdicts", "verdict_counts",
                      "missing_count", "root_canon", "limitations"):
            self.assertIn(key, prov)
        self.assertEqual(prov["missing_count"], len(got["missing_support"]))
        self.assertEqual(prov["limitations"], rc.LIMITATIONS)

    def test_the_results_are_deep_copies(self):
        s = claim_item("s", "喜欢冰可乐", sources=["e1"])
        n = claim_item("n", "讨厌冰可乐", sources=["e2"])
        expanded = rp.expand_paths([s], [edge("s", "n")], items=[s, n])
        before = canonical([expanded, s, n])
        got = rc.assess_expansion(expanded, seeds=[s], items=[s, n])
        got["paths"][0]["basis"]["edge_id"] = "mutated"
        got["conflicts"][0]["reason_code"] = "mutated"
        got["provenance"]["deduped"].append({"object_id": "fake"})
        got["provenance"]["verdicts"][0]["verdict"] = "mutated"
        got["missing_support"].append({"slot": "fake"})
        self.assertEqual(before, canonical([expanded, s, n]),
                         "改输出影响了输入，就不是深拷贝/只读")


# ── 端到端：接真实 R01→R02→R03→R04→R05（测试可 import，被测模块不 import）──


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r05_", ignore_cleanup_errors=True)
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

    def chain(self, ctx=None, budget=2):
        ctx = ctx if ctx is not None else self.ctx()
        fetched = ro.retrieve_evidence(ctx)
        edges = ae.project_edges(fetched["items"], fetched["degradation"])["edges"]
        recalled = rs.recall(fetched["items"], dict(ctx, retrieval_budget=budget))
        got = rc.resolve_ripple(recalled["items"], edges, items=fetched["items"],
                                degradation=fetched["degradation"])
        return got, fetched, recalled

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
        got, _fetched, _recalled = self.chain()
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["paths"])
        self.assertEqual([], got["conflicts"])
        self.assertEqual([], got["missing_support"],
                         "读不到不等于没有：降级时不喊 no_evidence")
        self.assertEqual({"absent"}, {d["reason"] for d in got["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "R01→R02→R03→R04→R05 只读链路顺手建了库/目录，就不再是只读")

    def test_real_independent_group_facts_conflict(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1"),
                          self.fact("f2", "讨厌冰可乐", "ge2", ts=1001.0)])
        got, _fetched, recalled = self.chain(budget=2)
        self.assertEqual(2, len(recalled["items"]))
        self.assertEqual(1, len(got["conflicts"]))
        record = got["conflicts"][0]
        self.assertEqual("contradiction", record["verdict"])
        self.assertEqual("explicit_negation_opposite", record["reason_code"])
        self.assertEqual({"group:g1:f1", "group:g1:f2"},
                         {record["left_id"], record["right_id"]})
        # 其余库 absent → 整体 degraded，但矛盾逐条列出，不静默。
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["missing_support"])

    def test_real_conflict_via_a_one_hop_path(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1"),
                          self.fact("f2", "讨厌冰可乐", "ge2", ts=1001.0)])
        got, _fetched, recalled = self.chain(budget=1)
        self.assertEqual(1, len(recalled["items"]))
        self.assertEqual(1, len(got["paths"]))
        self.assertEqual(1, got["paths"][0]["hops"])
        self.assertEqual(1, len(got["conflicts"]))
        self.assertEqual({"group:g1:f1", "group:g1:f2"},
                         {got["conflicts"][0]["left_id"], got["conflicts"][0]["right_id"]})

    def test_real_same_root_restatement_never_conflicts(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1"),
                          self.fact("f2", "讨厌冰可乐", "ge1", ts=1001.0)])
        got, _fetched, _recalled = self.chain(budget=2)
        self.assertEqual([], got["conflicts"], "同根源转述不算独立反例")
        self.assertEqual("disputed", got["provenance"]["verdicts"][0]["verdict"])
        self.assertEqual("same_root_not_independent",
                         got["provenance"]["verdicts"][0]["reason_code"])
        narrowed, _fetched, recalled = self.chain(budget=1)
        self.assertEqual(1, len(recalled["items"]))
        self.assertEqual([], narrowed["paths"], "R04 根源去重：同根邻居不扩散")
        self.assertEqual([], narrowed["conflicts"])

    def test_the_full_readonly_pipeline_rewrites_no_asset(self):
        self.group_facts([self.fact("f1", "喜欢冰可乐", "ge1")])
        self.belief("命题一", "我喜欢冰可乐", "r05:one")
        before = snapshot(self.root)
        self.chain()
        self.assertEqual(before, snapshot(self.root))

    def test_a_real_legacy_belief_reports_missing_slots(self):
        bid = sm.add_belief("某个旧认识", subject_qq="1001", is_legacy=True, ts=900.0)
        # legacy 无来源事件 → scope_basis='archive'：只有显式授权档案范围才读得到
        # （R01 §7.5 口径），否则被硬过滤挡下，轮不到 R05 登记缺槽。
        ctx = self.ctx(allowed_source_scopes=["private:1001", "group:g1", "cognition"])
        got, _fetched, _recalled = self.chain(ctx=ctx)
        codes = {m["reason_code"] for m in got["missing_support"]
                 if m["object_id"] == bid}
        self.assertLessEqual({"root_source_missing", "original_quote_missing",
                              "subject_ref_missing", "provenance_unverified"}, codes,
                             "legacy 信念缺依据要大声登记，不补造")
        self.assertEqual([], got["conflicts"])

    def test_cognition_beliefs_defer_without_a_projected_subject(self):
        """R01 认知投影未携带 subject_ref（前序缺陷，只报告不修）：M04 主体门
        如实 deferred，R05 不替它编主体，也不因此声称矛盾。"""
        self.belief("我喜欢冰可乐", "我喜欢冰可乐", "r05:a")
        self.belief("我不喜欢冰可乐", "我不喜欢冰可乐", "r05:b", ts=1001.0)
        got, _fetched, recalled = self.chain(budget=2)
        self.assertEqual(2, len(recalled["items"]))
        self.assertEqual([], got["conflicts"])
        self.assertEqual(1, len(got["provenance"]["verdicts"]))
        self.assertEqual("deferred", got["provenance"]["verdicts"][0]["verdict"])
        self.assertEqual("subject_missing",
                         got["provenance"]["verdicts"][0]["reason_code"])
        self.assertIn("subject_ref_missing",
                      [m["reason_code"] for m in got["missing_support"]])


if __name__ == "__main__":
    unittest.main()