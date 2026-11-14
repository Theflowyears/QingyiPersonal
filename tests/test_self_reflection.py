# -*- coding: utf-8 -*-
"""L01 仅从真实新增来源反思的回归锁（路线 §5.3 / §5.4 / §8.2；stage5/self）。

钉死的行为（每条判据正反例）：
  · 无新增真实证据 → 反思输出为空、plan 如实登记 no_new_real_source，绝不新增「发生过」；
  · 同一来源的重复转述不算多份独立证据（根源按 R04/R05 归并，独立支持按组计）；
  · 反思产物不得自证：输出只落解释/信念候选层（claim/belief），绝不落事件层；
    引用上一轮反思输出一律 interpretation_not_evidence 拒绝；
  · 推断/想象与小说/角色扮演/塔罗永不晋升为现实人物事实（复用 M01/M08 分层）；
  · 撤回/失效来源单调不复活（retracted/invalidated/superseded 不参与反思）；
  · 晋升/落账只走 cognition_writer.commit 单一写入者（经 M08.apply_consolidation）；
  · 确定性：无墙钟无随机、reflection_id 内容寻址、同输入同输出、输入顺序无关；
  · 只追加账本：旧记录不动、重复 ID/篡改拒绝、读函数纯读深拷贝；
  · 只读桥委托 M08：缺库报 absent/degraded，绝不建库建表建目录，DATA_DIR 调用时解析。
AST 锁钉死「复用而非重造」：import 面只含冻结前置 +  stdlib；必须调用 M08/M04/R04/R05
冻结原语；不自带第二套证据分层词表/矛盾引擎/根源归并引擎；无写入 SQL/旧写入器/直连；
落账只经 M08.apply_consolidation；无墙钟无随机、now 必填；不解析路径不开库；输出层
常量钉死 claim/belief；LIMITATIONS 如实声明 L02/L03/真实 LLM 反思没做。

所有合成数据只出现在 tempfile 隔离目录或内存里，是测试夹具，不是自然样本；
绝不写真实 data/。
"""
import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import self_reflection as sr        # noqa: E402
import memory_consolidation as mc   # noqa: E402
import cognition_admission as ca    # noqa: E402
import cognition_writer as cw       # noqa: E402
import claim_conflicts as cc        # noqa: E402
import ripple_conflicts as rc       # noqa: E402
import ripple_paths as rp           # noqa: E402
import invalidation_dag as dag      # noqa: E402
import self_model as sm             # noqa: E402

NOW = 2000.0
TABLES = ("belief", "belief_evidence", "claim_span", "claim_candidate",
          "episode", "derivation_edge", "correction_record", "object_validity_state")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def snapshot(root):
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


def source(event_id, text, **over):
    """手造冻结源记录（与 self_model.source_window 同一字段口径）。"""
    row = {"event_id": event_id, "kind": "private_message", "text": text,
           "source_revision": "rev_" + event_id, "occurred_at": 900.0,
           "session": "private:1001", "visibility": "private",
           "actor_ref": "per_1001", "actor_qq": "1001",
           "topic_ref": "", "topic_qq": "", "synthetic": False, "retracted": False}
    row.update(over)
    return row


def claim(oid, content, quote, eids, tier="reported", kind="claim", **over):
    if isinstance(eids, str):
        eids = [eids]
    row = {"object_kind": kind, "object_id": oid, "content": content,
           "occurred_at": 1000.0, "recorded_at": 1001.0, "source_event_ids": list(eids),
           "epistemic_type": tier, "visibility": "private",
           "source_session": "private:1001", "subject_ref": "per_1001",
           "evidence": [{"event_id": e, "quote": quote} for e in eids]}
    row.update(over)
    return row


def window():
    return [source("evt_1", "我最喜欢喝冰可乐了"), source("evt_2", "冰可乐真好喝"),
            source("evt_3", "我今天好累")]


def span(eid, quote):
    return {"event_id": eid, "quote": quote, "span_start": 0, "span_end": len(quote),
            "source_revision": "rev_" + eid, "match_kind": "lexical"}


# ── AST 静态锁：复用冻结前置，不长第二套引擎，真只读，无墙钟 ─────────
class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "self_reflection.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)
        cls.parents = {}
        for node in ast.walk(cls.tree):
            for child in ast.iter_child_nodes(node):
                cls.parents[child] = node

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

    def test_import_surface_is_frozen_predecessors_plus_stdlib(self):
        allowed = {"copy", "hashlib", "json", "claim_conflicts", "cognition_admission",
                   "memory_consolidation", "ripple_conflicts", "ripple_paths"}
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(set(), imported - allowed,
                         "L01 import 了写入/网络/模型/时钟 API：%s" % sorted(imported - allowed))
        self.assertLessEqual({"claim_conflicts", "cognition_admission", "memory_consolidation",
                              "ripple_conflicts", "ripple_paths"}, imported,
                             "L01 必须复用 M08/M04/M01/R04/R05 冻结前置")

    def test_it_reuses_the_frozen_predecessors(self):
        pairs = self.attr_pairs()
        wanted = {("mc", "classify_fate"), ("mc", "apply_consolidation"),
                  ("mc", "read_consolidation_inputs"), ("mc", "freeze_window"),
                  ("cc", "adjudicate"), ("rc", "_root_canon"), ("rp", "_roots")}
        self.assertEqual(set(), wanted - pairs,
                         "没有复用冻结前置：%s" % sorted(wanted - pairs))

    def test_no_second_evidence_tier_vocabulary(self):
        """证据分层词表只能来自 M01/M08：L01 不许自带五层词表常量。"""
        tiers = set(ca.EPISTEMIC_TYPES)
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            literals = {n.value for n in ast.walk(node.value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            self.assertFalse(tiers <= literals,
                             "L01 自带了第二套证据分层词表 %s" % sorted(tiers & literals))
        attrs = {(n.value.id, n.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for wanted in (("mc", "PROMOTABLE_TIERS"), ("mc", "NON_PROMOTABLE_TIERS"),
                       ("ca", "EPISTEMIC_TYPES"), ("ca", "FICTION_CONTEXTS")):
            self.assertIn(wanted, attrs, "证据分层/想象层词表必须复用 %s" % (wanted,))

    def test_no_second_conflict_or_root_engine(self):
        engine = {"classify_fate", "consolidation_plan", "plan_reassessment", "adjudicate",
                  "adjudicate_pair", "classify_predicate", "candidate_key",
                  "collect_conflict_pairs", "_root_canon", "_core_value", "_family_of",
                  "_view", "_value_of", "_root_of", "commit_plan", "apply_consolidation"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, engine, "重定义了冻结引擎函数 %s" % node.name)
                for word in ("classify", "adjudicate", "family", "verdict", "contradict"):
                    self.assertNotIn(word, node.name.lower(),
                                     "长出了第二套矛盾/分层机器 %s" % node.name)
        # 根源归并必须委托 R05，不许自带 union-find。
        self.assertNotIn("def _root_canon", self.source)
        self.assertIn(("rc", "_root_canon"), self.attr_pairs())

    def test_no_write_sql_anywhere(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper, "L01 里出现写入 SQL：%s" % node.value[:60])

    def test_no_legacy_writers_or_direct_connections(self):
        forbidden = {"add_belief", "record_episode", "quarantine_claim", "record_event",
                     "note_self_view", "revise_belief", "retract_evidence", "add_evidence",
                     "add_commitment", "add_goal", "record_action", "_db", "_transaction",
                     "executescript", "executemany", "rollback", "makedirs", "mkdir",
                     "write_text", "write_bytes", "remove", "unlink", "rmtree", "rename",
                     "write_cognition_transaction", "write_invalidation_transaction",
                     "connect", "execute", "commit", "open", "save", "dump", "touch"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), forbidden,
                             "L01 绕过既有入口写账/开库：%s" % self.call_name(call))

    def test_writes_go_only_through_m08_apply(self):
        """落账只经 M08.apply_consolidation（它本身被锁钉死只走 cognition_writer.commit）。"""
        self.assertIn(("mc", "apply_consolidation"), self.attr_pairs())
        # L01 不直接 import/调用 cognition_writer 或 self_model 写入器。
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"cognition_writer", "self_model",
                                                 "invalidation_dag"},
                                 "L01 应经 M08 落账，不直接接写入器")

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"time", "random", "datetime"},
                                 "L01 不许 import 时钟/随机")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic"),
                                 "L01 决策路径读了时钟/随机 %s" % node.func.attr)

    def test_now_is_a_required_keyword(self):
        for name in ("reflection_plan", "apply_reflection"):
            fn = next(n for n in ast.walk(self.tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            kw = [a.arg for a in fn.args.kwonlyargs]
            self.assertIn("now", kw, "%s 必须显式接收 now" % name)
            index = kw.index("now")
            self.assertIsNone(fn.args.kw_defaults[index],
                              "%s 的 now 不许有默认值（无墙钟）" % name)

    def test_no_path_resolution_or_db_open_in_module(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists", "connect"),
                             "L01 自身解析路径/开库了；只读桥必须委托 M08")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"os", "sqlite3", "pathlib"},
                                 "L01 不该 import os/sqlite3/pathlib")
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_output_layer_is_interpretation_only(self):
        self.assertEqual(("claim", "belief"), sr.OUTPUT_KINDS)
        self.assertNotIn("episode", sr.OUTPUT_KINDS)
        self.assertNotIn("state", sr.OUTPUT_KINDS)
        self.assertNotIn("commitment", sr.OUTPUT_KINDS)

    def test_the_contract_constants(self):
        self.assertEqual("l01-1", sr.ALGORITHM_VERSION)
        self.assertEqual(ca.EPISTEMIC_TYPES, sr.EPISTEMIC_TYPES)
        self.assertEqual(mc.PROMOTABLE_TIERS, sr.PROMOTABLE_TIERS)
        self.assertEqual(mc.NON_PROMOTABLE_TIERS, sr.NON_PROMOTABLE_TIERS)
        self.assertEqual(ca.FICTION_CONTEXTS, sr.FICTION_CONTEXTS)
        self.assertEqual(("reflected_from_new_sources", "no_new_real_source"), sr.PLAN_REASONS)
        for code in ("missing_object_id", "reflection_not_event", "interpretation_not_evidence",
                     "source_invalidated", "no_new_real_source", "fiction_not_fact"):
            self.assertIn(code, sr.REASON_CODES)

    def test_limitations_are_honest(self):
        self.assertIsInstance(sr.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(sr.LIMITATIONS), 8)
        text = " ".join(sr.LIMITATIONS)
        for undone in ("L02", "L03", "LLM", "cognition_writer.commit", "单调"):
            self.assertIn(undone, text, "limitations 必须如实声明 %s" % undone)
        self.assertTrue("新增" in text or "无新" in text)


# ── 行为锁：无新增真实证据 → 反思输出为空 ───────────────────────────
class SourcingTests(unittest.TestCase):
    def test_empty_window_yields_empty_reflection_with_honest_reason(self):
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  [], now=NOW)
        self.assertEqual("no_new_real_source", plan["reason_code"])
        self.assertEqual([], plan["reflections"])
        self.assertEqual([], plan["promotions"])
        self.assertEqual([], plan["new_sources"])
        self.assertEqual("no_new_real_source", plan["decisions"][0]["reason_code"])

    def test_no_candidates_yields_empty_reflection(self):
        plan = sr.reflection_plan([], window(), now=NOW)
        self.assertEqual([], plan["reflections"])
        self.assertEqual("reflected_from_new_sources", plan["reason_code"])
        self.assertEqual(3, len(plan["new_sources"]))

    def test_new_real_source_grounds_a_reflection(self):
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  window(), now=NOW)
        self.assertEqual("reflected_from_new_sources", plan["reason_code"])
        self.assertEqual(["evt_1", "evt_2", "evt_3"], plan["new_sources"])
        self.assertEqual(1, len(plan["reflections"]))
        self.assertEqual(1, len(plan["promotions"]))
        self.assertEqual("promoted_belief", plan["decisions"][0]["reason_code"])

    def test_already_reflected_source_is_not_new(self):
        ledger = sr.new_ledger()
        w = [source("evt_1", "我最喜欢喝冰可乐了")]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")
        first = sr.reflection_plan([cand], w, now=NOW)
        sr.commit_reflection(ledger, first)
        second = sr.reflection_plan([cand], w, ledger=ledger, now=NOW)
        self.assertEqual("no_new_real_source", second["reason_code"])
        self.assertEqual([], second["reflections"])
        self.assertIn({"event_id": "evt_1", "reason_code": "already_reflected"},
                      second["excluded_sources"])

    def test_reflection_grounds_only_on_new_sources_not_old(self):
        ledger = sr.new_ledger()
        first = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                   window(), now=NOW)
        sr.commit_reflection(ledger, first)
        # 新候选引用旧源 evt_1 + 新源 evt_2：只能落在新源 evt_2 上。
        mixed = claim("r2", "冰可乐真好喝", "冰可乐真好喝", ["evt_1", "evt_2"])
        plan = sr.reflection_plan([mixed], window(), ledger=ledger, now=NOW)
        decision = plan["decisions"][0]
        self.assertEqual(["evt_2"], decision["grounding_sources"])
        self.assertEqual("interpretation", decision["layer"])

    def test_synthetic_source_is_not_real_evidence(self):
        w = [source("evt_1", "我最喜欢喝冰可乐了", synthetic=True)]
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  w, now=NOW)
        self.assertEqual("no_new_real_source", plan["reason_code"])
        self.assertEqual([], plan["reflections"])
        self.assertIn({"event_id": "evt_1", "reason_code": "synthetic_source"},
                      plan["excluded_sources"])

    def test_missing_object_id_is_rejected(self):
        plan = sr.reflection_plan([claim("", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  window(), now=NOW)
        self.assertEqual("missing_object_id", plan["decisions"][0]["reason_code"])
        self.assertEqual("rejected", plan["decisions"][0]["layer"])


# ── 行为锁：同根源转述不算独立证据（R04/R05 同口径）─────────────────
class RootIndependenceTests(unittest.TestCase):
    def test_same_root_restatement_counts_as_one_independent_source(self):
        w = [source("e1", "我最喜欢喝冰可乐了"),
             source("e2", "我最喜欢喝冰可乐了", root_source_ids=["e1"])]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", ["e1", "e2"])
        plan = sr.reflection_plan([cand], w, now=NOW)
        decision = plan["decisions"][0]
        self.assertEqual(1, decision["independent_sources"],
                         "同根源转述被当成了多份独立证据")
        self.assertEqual(["e1"], decision["root_groups"])
        self.assertEqual(["e1", "e2"], decision["grounding_sources"])

    def test_distinct_roots_count_as_independent_sources(self):
        w = [source("e1", "我最喜欢喝冰可乐了"), source("e2", "冰可乐真好喝")]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", ["e1"],
                     source_event_ids=["e1", "e2"],
                     evidence=[{"event_id": "e1", "quote": "我最喜欢喝冰可乐了"},
                               {"event_id": "e2", "quote": "冰可乐真好喝"}])
        plan = sr.reflection_plan([cand], w, now=NOW)
        self.assertEqual(2, plan["decisions"][0]["independent_sources"])

    def test_intersecting_multi_roots_are_merged(self):
        w = [source("e1", "原句一", root_source_ids=["r1", "r2"]),
             source("e2", "原句二", root_source_ids=["r2", "r3"])]
        cand = claim("r1", "命题", "原句一", ["e1"],
                     source_event_ids=["e1", "e2"],
                     evidence=[{"event_id": "e1", "quote": "原句一"},
                               {"event_id": "e2", "quote": "原句二"}])
        plan = sr.reflection_plan([cand], w, now=NOW)
        self.assertEqual(1, plan["decisions"][0]["independent_sources"],
                         "根源有交集就不是独立证据（R04/R05 同口径）")
        self.assertEqual({"r1": "r1", "r2": "r1", "r3": "r1"},
                         plan["provenance"]["root_canon"])

    def test_two_candidates_sharing_a_root_are_not_independent(self):
        w = [source("e1", "我最喜欢喝冰可乐了")]
        a = claim("a", "喜欢冰可乐", "我最喜欢喝冰可乐了", "e1")
        b = claim("b", "喜欢冰可乐", "我最喜欢喝冰可乐了", "e1")
        plan = sr.reflection_plan([a, b], w, now=NOW)
        groups = {d["object_id"]: d["root_groups"] for d in plan["decisions"]}
        self.assertEqual(groups["a"], groups["b"], "同根候选共享根源组，不算彼此独立")
        self.assertEqual(1, plan["independent_support"]["a"])

    def test_root_canon_reuses_r05(self):
        w = [source("e1", "x", root_source_ids=["r1"]),
             source("e2", "y", root_source_ids=["r1"])]
        plan = sr.reflection_plan([claim("r1", "命题", "x", "e1")], w, now=NOW)
        rows = [{"object_id": r["event_id"],
                 "root_source_ids": rp._roots(r) or [r["event_id"]]} for r in w]
        self.assertEqual(rc._root_canon(rows), plan["provenance"]["root_canon"])


# ── 行为锁：解释层与事件层分层，反思不得自证 ───────────────────────
class LayerTests(unittest.TestCase):
    def test_episode_candidate_is_never_a_reflection_output(self):
        cand = claim("r1", "她今天说累了", "我今天好累", "evt_3",
                     tier="experienced", kind="episode")
        plan = sr.reflection_plan([cand], window(), now=NOW)
        decision = plan["decisions"][0]
        self.assertEqual("rejected", decision["layer"])
        self.assertEqual("reflection_not_event", decision["reason_code"])
        self.assertEqual([], plan["reflections"])

    def test_state_and_commitment_are_not_reflection_outputs(self):
        for kind in ("state", "commitment", "vibe"):
            cand = claim("r1", "x", "我今天好累", "evt_3", kind=kind)
            plan = sr.reflection_plan([cand], window(), now=NOW)
            self.assertEqual("reflection_not_event", plan["decisions"][0]["reason_code"], kind)

    def test_reflection_output_lands_only_in_belief_layer(self):
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  window(), now=NOW)
        for decision in plan["reflections"]:
            self.assertIn(decision["output_kind"], sr.OUTPUT_KINDS)
        for decision in plan["promotions"]:
            self.assertEqual("belief", decision["output_kind"])
            self.assertEqual("belief", decision["fate"])
            self.assertTrue(decision["ledger_id"].startswith("bel_"))

    def test_citing_a_prior_reflection_output_is_self_proof_and_refused(self):
        ledger = sr.new_ledger()
        first = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                   window(), now=NOW)
        sr.commit_reflection(ledger, first)
        output_id = first["reflections"][0]["object_id"]
        ledger_id = first["reflections"][0]["ledger_id"]
        for cited in (output_id, ledger_id):
            self_cand = claim("r2", "自证", "我最喜欢喝冰可乐了", cited)
            plan = sr.reflection_plan([self_cand], window(), ledger=ledger, now=NOW)
            self.assertEqual("interpretation_not_evidence",
                             plan["decisions"][0]["reason_code"], cited)
            self.assertEqual([], plan["reflections"])

    def test_interpretation_outputs_are_registered_in_ledger(self):
        ledger = sr.new_ledger()
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  window(), now=NOW)
        record = sr.commit_reflection(ledger, plan)
        self.assertIn("r1", record["output_ids"])
        self.assertTrue(any(oid.startswith("bel_") for oid in record["output_ids"]))
        self.assertEqual(["evt_1"], record["consumed_sources"])


# ── 行为锁：证据分层，想象层永不混入现实人物事实 ───────────────────
class EvidenceTierTests(unittest.TestCase):
    def test_promotable_tiers_reflect_to_belief(self):
        for tier in ("experienced", "reported", "looked_up"):
            cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1", tier=tier)
            plan = sr.reflection_plan([cand], window(), now=NOW)
            self.assertTrue(plan["decisions"][0]["promotable"], tier)
            self.assertEqual("promoted_belief", plan["decisions"][0]["reason_code"])

    def test_inferred_never_promotes_to_fact(self):
        cand = claim("r1", "推断她累了", "我今天好累", "evt_3", tier="inferred")
        plan = sr.reflection_plan([cand], window(), now=NOW)
        decision = plan["decisions"][0]
        self.assertEqual("interpretation", decision["layer"])
        self.assertFalse(decision["promotable"])
        self.assertEqual("inferred_not_promotable", decision["reason_code"])
        self.assertEqual([], plan["promotions"])

    def test_imagined_never_promotes_to_fact(self):
        cand = claim("r1", "想象她累了", "我今天好累", "evt_3", tier="imagined")
        plan = sr.reflection_plan([cand], window(), now=NOW)
        decision = plan["decisions"][0]
        self.assertFalse(decision["promotable"])
        self.assertEqual("imagined_not_promotable", decision["reason_code"])

    def test_unknown_tier_is_not_promotable(self):
        cand = claim("r1", "x", "我今天好累", "evt_3", tier="mystery")
        plan = sr.reflection_plan([cand], window(), now=NOW)
        self.assertFalse(plan["decisions"][0]["promotable"])
        self.assertEqual("unknown_epistemic_type", plan["decisions"][0]["reason_code"])

    def test_fiction_contexts_never_become_real_person_facts(self):
        for context in ca.FICTION_CONTEXTS:
            cand = claim("r1", "塔罗说她累了", "我今天好累", "evt_3", context_kind=context)
            plan = sr.reflection_plan([cand], window(), now=NOW)
            decision = plan["decisions"][0]
            self.assertEqual("fiction_not_fact", decision["reason_code"], context)
            self.assertFalse(decision["promotable"], context)
            self.assertEqual([], plan["promotions"])

    def test_quote_not_verbatim_is_not_grounded(self):
        cand = claim("r1", "喜欢冰可乐", "完全对不上的话", "evt_1")
        plan = sr.reflection_plan([cand], window(), now=NOW)
        decision = plan["decisions"][0]
        self.assertEqual("rejected", decision["layer"])
        self.assertEqual("evidence_not_verifiable", decision["reason_code"])

    def test_m04_contradiction_blocks_blind_promotion(self):
        conflicts = [{"verdict": "contradiction", "left_id": "r1", "right_id": "rX"}]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")
        plan = sr.reflection_plan([cand], window(), conflicts=conflicts, now=NOW)
        decision = plan["decisions"][0]
        self.assertFalse(decision["promotable"])
        self.assertEqual("conflict_contradiction", decision["reason_code"])


# ── 行为锁：撤回/失效单调不复活（M05/S06 同口径）───────────────────
class MonotonicTests(unittest.TestCase):
    def test_retracted_source_does_not_participate(self):
        w = [source("evt_1", "我最喜欢喝冰可乐了", retracted=True)]
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  w, now=NOW)
        self.assertEqual("source_invalidated", plan["decisions"][0]["reason_code"])
        self.assertEqual([], plan["reflections"])
        self.assertEqual("no_new_real_source", plan["reason_code"])

    def test_m05_invalidated_source_blocks_reflection(self):
        for status in ("invalidated", "superseded"):
            states = {"evt_1": {"status": status}}
            plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                      window(), states=states, now=NOW)
            self.assertEqual("source_invalidated", plan["decisions"][0]["reason_code"], status)
            self.assertEqual([], plan["reflections"])

    def test_invalidated_object_is_not_reflected(self):
        for status in ("invalidated", "superseded"):
            states = {"r1": {"status": status}}
            plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                      window(), states=states, now=NOW)
            self.assertEqual("prior_invalidation_preserved",
                             plan["decisions"][0]["reason_code"], status)
            self.assertEqual("rejected", plan["decisions"][0]["layer"])

    def test_needs_reassessment_object_is_not_blindly_promoted(self):
        states = {"r1": {"status": "needs_reassessment"}}
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  window(), states=states, now=NOW)
        self.assertEqual("needs_reassessment_pending", plan["decisions"][0]["reason_code"])
        self.assertFalse(plan["decisions"][0]["promotable"])

    def test_retraction_after_reflection_does_not_revive_in_next_round(self):
        ledger = sr.new_ledger()
        first = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                   window(), now=NOW)
        sr.commit_reflection(ledger, first)
        # 源被撤回：即便账本曾反思过它，新一轮也不复活、不再参与。
        w = [source("evt_1", "我最喜欢喝冰可乐了", retracted=True),
             source("evt_2", "冰可乐真好喝")]
        again = sr.reflection_plan([claim("r2", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                   w, ledger=ledger, now=NOW)
        self.assertEqual("source_invalidated", again["decisions"][0]["reason_code"])
        self.assertEqual([], again["reflections"])


# ── 行为锁：确定性（无墙钟、内容寻址、顺序无关、不改输入）──────────
class DeterminismTests(unittest.TestCase):
    def fixture(self):
        cands = [claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1"),
                 claim("r2", "喜欢茶", "冰可乐真好喝", "evt_2")]
        return cands, window()

    def test_same_inputs_same_output(self):
        cands, w = self.fixture()
        one = sr.reflection_plan(cands, w, now=NOW)
        two = sr.reflection_plan(cands, w, now=NOW)
        self.assertEqual(canonical(one), canonical(two))

    def test_wall_clock_does_not_change_id_or_decisions(self):
        cands, w = self.fixture()
        one = sr.reflection_plan(cands, w, now=2000.0)
        two = sr.reflection_plan(cands, w, now=99999.0)
        self.assertEqual(one["reflection_id"], two["reflection_id"])
        self.assertEqual(one["decisions"], two["decisions"])

    def test_input_order_does_not_change_output(self):
        cands, w = self.fixture()
        forward = sr.reflection_plan(cands, w, now=NOW)
        backward = sr.reflection_plan(list(reversed(cands)), list(reversed(w)), now=NOW)
        self.assertEqual(forward["reflection_id"], backward["reflection_id"])
        self.assertEqual(canonical(forward["decisions"]), canonical(backward["decisions"]))

    def test_inputs_are_not_mutated(self):
        cands, w = self.fixture()
        before = canonical([cands, w])
        sr.reflection_plan(cands, w, now=NOW)
        self.assertEqual(before, canonical([cands, w]))

    def test_duplicate_rows_do_not_duplicate_reflections(self):
        cands, w = self.fixture()
        baseline = sr.reflection_plan(cands, w, now=NOW)
        doubled = sr.reflection_plan(cands + cands, w + w, now=NOW)
        self.assertEqual(baseline["reflection_id"], doubled["reflection_id"])
        self.assertEqual(canonical(baseline["decisions"]), canonical(doubled["decisions"]))

    def test_provenance_carries_no_clock(self):
        cands, w = self.fixture()
        plan = sr.reflection_plan(cands, w, now=NOW)
        self.assertNotIn("ts", plan["provenance"])
        self.assertNotIn("now", plan["provenance"])
        self.assertNotIn("seconds", plan["provenance"])
        self.assertFalse(plan["provenance"]["llm_reflection"])

    def test_missing_now_is_refused_loudly(self):
        cands, w = self.fixture()
        with self.assertRaises(ValueError):
            sr.reflection_plan(cands, w, now=None)
        with self.assertRaises(TypeError):
            sr.reflection_plan(cands, w)


# ── 行为锁：只追加账本、内容寻址、纯读 ─────────────────────────────
class LedgerTests(unittest.TestCase):
    def plan(self):
        return sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  [source("evt_1", "我最喜欢喝冰可乐了")], now=NOW)

    def test_commit_appends_and_marks_committed(self):
        ledger = sr.new_ledger()
        record = sr.commit_reflection(ledger, self.plan())
        self.assertEqual("committed", record["status"])
        self.assertEqual([record["reflection_id"]], sr.list_records(ledger))

    def test_duplicate_reflection_id_rejected(self):
        ledger = sr.new_ledger()
        plan = self.plan()
        sr.commit_reflection(ledger, plan)
        with self.assertRaises(ValueError):
            sr.commit_reflection(ledger, plan)
        self.assertEqual(1, len(sr.list_records(ledger)))

    def test_tampered_reflection_id_rejected(self):
        ledger = sr.new_ledger()
        plan = self.plan()
        plan["reflection_id"] = "l01_forged"
        with self.assertRaises(ValueError):
            sr.commit_reflection(ledger, plan)
        self.assertEqual([], sr.list_records(ledger))

    def test_tampered_decisions_rejected_at_commit(self):
        ledger = sr.new_ledger()
        plan = self.plan()
        plan["decisions"][0]["layer"] = "rejected"
        plan["decisions"][0]["reason_code"] = "no_new_real_source"
        with self.assertRaises(ValueError):
            sr.commit_reflection(ledger, plan)

    def test_plan_does_not_write_ledger(self):
        ledger = sr.new_ledger()
        before = canonical(ledger)
        plan = self.plan()
        self.assertEqual("planned", plan["status"])
        self.assertEqual(before, canonical(ledger))

    def test_get_record_returns_copy_and_never_writes(self):
        ledger = sr.new_ledger()
        record = sr.commit_reflection(ledger, self.plan())
        before = canonical(ledger)
        got = sr.get_record(ledger, record["reflection_id"])
        got["decisions"].clear()
        sr.explain_reflection(ledger, record["reflection_id"])
        self.assertEqual(before, canonical(ledger))
        self.assertIsNone(sr.get_record(ledger, "l01_missing"))

    def test_explain_reflection_answers_why(self):
        ledger = sr.new_ledger()
        record = sr.commit_reflection(ledger, self.plan())
        ex = sr.explain_reflection(ledger, record["reflection_id"])
        self.assertEqual(["r1"], ex["promoted"])
        self.assertEqual("reflected_from_new_sources", ex["reason_code"])
        self.assertEqual(["evt_1"], ex["new_sources"])
        self.assertEqual(1, ex["reflections"][0]["independent_sources"])
        self.assertEqual(["evt_1"], ex["consumed_sources"])

    def test_explain_missing_reflection_raises(self):
        with self.assertRaises(ValueError):
            sr.explain_reflection(sr.new_ledger(), "l01_nope")

    def test_bad_ledger_rejected(self):
        for bad in (None, {}, {"records": [], "order": []}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sr.commit_reflection(bad, self.plan())

    def test_old_records_are_byte_preserved_on_append(self):
        ledger = sr.new_ledger()
        first = sr.commit_reflection(ledger, self.plan())
        before = canonical(first)
        second_plan = sr.reflection_plan([claim("r2", "喜欢茶", "冰可乐真好喝", "evt_2")],
                                         window(), now=NOW)
        sr.commit_reflection(ledger, second_plan)
        self.assertEqual(before, canonical(sr.get_record(ledger, first["reflection_id"])))
        self.assertEqual(2, len(sr.list_records(ledger)))


# ── 隔离账本夹具（DATA_DIR 调用时解析，绝不写真实 data/）──────────
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="l01_", ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._old = os.environ.get("DATA_DIR"), os.environ.get("PERSONA_DATA_DIR")
        os.environ["DATA_DIR"] = self.root
        os.environ.pop("PERSONA_DATA_DIR", None)

    def tearDown(self):
        for key, value in zip(("DATA_DIR", "PERSONA_DATA_DIR"), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def seed(self, text, dedupe, ts=1000.0):
        return sm.record_event("private_message", session="private:1001",
                               actor_qq="1001", visibility="private",
                               payload={"text": text}, ts=ts, dedupe=dedupe)

    def real_claim(self, oid, content, eid, quote, tier="reported", kind="claim", **over):
        w = sm.source_window([eid])
        row = {"object_kind": kind, "object_id": oid, "content": content,
               "occurred_at": 1000.0, "recorded_at": 1001.0, "source_event_ids": [eid],
               "epistemic_type": tier, "visibility": "private",
               "source_session": "private:1001", "subject_ref": w[0]["actor_ref"],
               "evidence": [{"event_id": eid, "quote": quote}]}
        row.update(over)
        return row

    def counts(self):
        if not Path(self.root, "persona", "cognition.db").exists():
            return {t: 0 for t in TABLES}
        with closing(sm._db()) as c:
            return {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in TABLES}


class BridgeTests(Base):
    def test_absent_ledger_degrades_without_creating(self):
        got = sr.read_reflection_inputs()
        self.assertEqual([], got["candidates"])
        self.assertEqual([{"store": "cognition", "reason": "absent"}], got["degradation"])
        self.assertEqual([], list(Path(self.root).iterdir()), "只读桥建了目录")

    def test_freeze_window_absent_returns_empty_and_creates_nothing(self):
        self.assertEqual([], sr.freeze_window(["evt_x"]))
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_reads_real_candidates_and_beliefs(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:b1")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        cw.commit(self.real_claim("o2", "推断", eid, "我最喜欢喝冰可乐了", tier="inferred"), w, now=NOW)
        got = sr.read_reflection_inputs()
        self.assertEqual([], got["degradation"])
        self.assertEqual(1, len(got["beliefs"]))
        self.assertTrue(got["candidates"])
        self.assertTrue(got["beliefs"][0]["evidence"], "belief 原句 span 没重建")

    def test_reads_never_modify_the_ledger(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:b2")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        path = Path(self.root, "persona", "cognition.db")
        before = path.read_bytes()
        sr.read_reflection_inputs()
        sr.read_reflection_inputs()
        self.assertEqual(before, path.read_bytes())

    def test_paths_resolve_at_call_time(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:b3")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        self.assertEqual(1, len(sr.read_reflection_inputs()["beliefs"]))
        with tempfile.TemporaryDirectory(prefix="l01_other_") as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                self.assertEqual([], sr.read_reflection_inputs()["beliefs"])
        self.assertEqual(1, len(sr.read_reflection_inputs()["beliefs"]))

    def test_freeze_window_reads_real_events(self):
        eid = self.seed("我今天好累", "l01:b4")
        w = sr.freeze_window([eid, eid])
        self.assertEqual(1, len(w))
        self.assertEqual("我今天好累", w[0]["text"])
        self.assertFalse(w[0]["retracted"])


class ApplyTests(Base):
    def test_reflection_writes_belief_through_single_writer(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:a1")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                  w, now=NOW)
        calls = []
        real = cw.commit

        def spy(candidate, win, now=None):
            calls.append(candidate.get("object_kind"))
            return real(candidate, win, now=now)

        with patch.object(cw, "commit", spy):
            got = sr.apply_reflection(plan, w, now=NOW)
        self.assertEqual("ok", got["status"])
        self.assertEqual(1, got["promoted"])
        self.assertTrue(calls, "落账没有经过 cognition_writer.commit 单一写入者")
        self.assertEqual(1, self.counts()["belief"])
        self.assertEqual(0, self.counts()["episode"], "反思落到了事件层")
        self.assertEqual(1, got["edges_added"])
        self.assertEqual(0, got["epoch"], "反思不是管理修正，不许推进 epoch")

    def test_reflection_never_writes_episode_even_if_candidate_is_episode(self):
        eid = self.seed("我今天好累", "l01:a2")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "她今天说累了", eid, "我今天好累",
                                                   tier="experienced", kind="episode")], w, now=NOW)
        self.assertEqual([], plan["promotions"])
        got = sr.apply_reflection(plan, w, now=NOW)
        self.assertEqual(0, got["promoted"])
        self.assertEqual(0, self.counts()["episode"])
        self.assertEqual(0, self.counts()["belief"])

    def test_inferred_is_never_written_as_fact(self):
        eid = self.seed("我今天好累", "l01:a3")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "推断她累了", eid, "我今天好累",
                                                   tier="inferred")], w, now=NOW)
        self.assertEqual([], plan["promotions"])
        got = sr.apply_reflection(plan, w, now=NOW)
        self.assertEqual(0, got["promoted"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(0, self.counts()["claim_candidate"])

    def test_repeat_apply_is_idempotent(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:a4")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                  w, now=NOW)
        sr.apply_reflection(plan, w, now=NOW)
        again = sr.apply_reflection(plan, w, now=NOW + 500)
        self.assertEqual(1, again["duplicates"])
        self.assertEqual(0, again["promoted"])
        self.assertEqual(1, self.counts()["belief"])

    def test_non_belief_promotion_is_refused_loudly(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:a5")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                  w, now=NOW)
        plan["promotions"] = [dict(plan["promotions"][0], fate="experience")]
        with self.assertRaises(ValueError):
            sr.apply_reflection(plan, w, now=NOW)
        self.assertEqual(0, self.counts()["episode"])

    def test_empty_plan_writes_nothing(self):
        plan = sr.reflection_plan([], [], now=NOW)
        got = sr.apply_reflection(plan, [], now=NOW)
        self.assertEqual(0, got["promoted"])
        self.assertFalse(Path(self.root, "persona").exists())

    def test_gate_demotion_registers_no_edges(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:a6")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                  w, now=NOW)

        def demote(candidate, win, now=None):
            return {"decision": "quarantine", "reasons": ["injected_demotion"], "object_ids": {}}

        with patch.object(cw, "commit", demote):
            got = sr.apply_reflection(plan, w, now=NOW)
        self.assertEqual(1, got["deferred"])
        self.assertEqual(0, got["edges_added"])
        self.assertEqual(0, self.counts()["belief"])

    def test_bad_plan_rejected(self):
        with self.assertRaises(ValueError):
            sr.apply_reflection({"nope": 1}, [], now=NOW)
        with self.assertRaises(ValueError):
            sr.apply_reflection(sr.reflection_plan([], [], now=NOW), [], now=None)

    def test_projection_carries_provenance_and_limitations(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:a7")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                  w, now=NOW)
        got = sr.apply_reflection(plan, w, now=NOW)
        self.assertEqual("l01-1", got["provenance"]["reflection_algorithm_version"])
        self.assertTrue(any("L02" in line for line in got["limitations"]))
        self.assertTrue(any("cognition_writer.commit" in line for line in got["limitations"]))

    def test_derivation_edge_propagates_invalidation_to_reflection(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l01:a8")
        w = sm.source_window([eid])
        plan = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了")],
                                  w, now=NOW)
        got = sr.apply_reflection(plan, w, now=NOW)
        bid = got["promotions"][0]["ledger_id"]
        st = dag.read_invalidation_state()
        self.assertEqual(bid, st["edges"][0]["derived_id"])
        rec = dag.correction("observation_invalid", eid, "撤回", ts=2600.0)
        prop = dag.propagate(st["edges"], [rec], epoch=st["epoch"], states=st["states"], now=2600.0)
        sm.write_invalidation_transaction({"ts": 2600.0, "expected_epoch": st["epoch"],
                                           "edges": [], "corrections": [rec], "propagation": prop})
        self.assertEqual("invalidated", dag.read_invalidation_state()["states"][bid]["status"])


# ── LIMITATIONS 内容锁（如实声明没做的事）──────────────────────────
class LimitationsTests(unittest.TestCase):
    def test_limitations_declare_undone_work(self):
        text = " ".join(sr.LIMITATIONS)
        self.assertIn("L02", text)
        self.assertIn("L03", text)
        self.assertIn("LLM", text)
        self.assertIn("不接进真实链路", text)
        self.assertIn("单调", text)
        self.assertIn("cognition_writer.commit", text)

    def test_plan_carries_limitations(self):
        plan = sr.reflection_plan([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")],
                                  window(), now=NOW)
        self.assertEqual(list(sr.LIMITATIONS), plan["limitations"])


if __name__ == "__main__":
    unittest.main()