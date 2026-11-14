# -*- coding: utf-8 -*-
"""L02 信念与自我认识版本修订的回归锁（路线 §5.1 / §5.3 / §5.4 / §8.2；stage5/self）。

钉死的行为（每条判据正反例）：
  · 修订提案只接受 L01 反思产物：信封只从已提交 L01 账本取，自带信封忽略；
    直接「发生过」（episode/state）断言一律 revision_not_event 拒绝；
  · 自证拦截：L01 解释层产物与既往修订版本不得充当证据；
  · 门序：证据检索（M01/M02 经 M08.classify_fate）→ 反例检查（M04）→
    接受或暂缓（held/rejected 必带稳定 reason_code）→ 版本记录；
  · 同一来源重复转述不算独立证据，也不算独立反例（R04/R05 同口径）；
  · 版本 append-only：before/after/证据/反例结果/理由齐全，重复 ID/篡改/
    版本断链拒绝，旧记录一字节不动；
  · 失效单调：M05 失效/撤回证据不得支撑接受；支撑后来失效 → 重评候选，
    历史不篡改；
  · 「什么改变了你的想法」只从修订记录回答，缺证据如实说不知道，不编故事；
  · 落账为纯计划 + schema 对齐行（belief_revision/self_view_rev 逐列对齐），
    本模块不开写入口；只读桥委托 L01/M08/M05，缺库报 absent，绝不建库。
AST 锁钉死「复用而非重造」：import 面只含冻结前置 + stdlib；必须调用
L01/M08/M04/M05 冻结原语；无写入 SQL/旧写入器/直连/第二写入口；无墙钟无随机、
now 必填；不解析路径不开库；目标层常量钉死 belief/self_view。

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

import self_revision as sv          # noqa: E402
import self_reflection as sr        # noqa: E402
import memory_consolidation as mc   # noqa: E402
import cognition_admission as ca    # noqa: E402
import cognition_writer as cw       # noqa: E402
import claim_conflicts as cc        # noqa: E402
import invalidation_dag as dag      # noqa: E402
import self_model as sm             # noqa: E402

NOW = 2000.0
AFTER = "有话题时我愿意主动找人"
REASON = "几次真实互动里我都先开了口"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


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


def default_claim():
    return claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", "evt_1")


def l01_fixture(cands=None, w=None):
    """已提交的 L01 反思账本 + reflection_id（修订候选的唯一来源）。"""
    led = sr.new_ledger()
    plan = sr.reflection_plan(cands if cands is not None else [default_claim()],
                              w if w is not None else window(), now=NOW)
    record = sr.commit_reflection(led, plan)
    return led, record["reflection_id"]


def prop(pid, rid, oid="r1", after=AFTER, reason=REASON, **over):
    row = {"proposal_id": pid, "reflection_id": rid, "object_id": oid,
           "target_kind": "belief", "target_id": "bel_x", "after": after,
           "reason": reason}
    row.update(over)
    return row


def accepted_fixture(**over):
    """一条已接受的 belief 修订（v1，支撑 evt_1）。返回 (ledger, plan, record)。"""
    rl, rid = l01_fixture()
    plan = sv.revision_plan([prop("p1", rid, **over)], rl, window(), now=NOW)
    led = sv.new_ledger()
    records = sv.commit_revision(led, plan)
    return led, plan, records[0]


# ── AST 静态锁：复用冻结前置，不长第二套引擎，无写入口，无墙钟 ─────
class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "self_revision.py"), encoding="utf-8") as handle:
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

    def test_import_surface_is_frozen_predecessors_plus_stdlib(self):
        allowed = {"copy", "hashlib", "json", "claim_conflicts", "cognition_admission",
                   "invalidation_dag", "memory_consolidation", "self_reflection"}
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(set(), imported - allowed,
                         "L02 import 了写入/网络/模型/时钟 API：%s" % sorted(imported - allowed))
        self.assertLessEqual({"claim_conflicts", "invalidation_dag",
                              "memory_consolidation", "self_reflection"}, imported,
                             "L02 必须复用 L01/M04/M05/M08 冻结前置")

    def test_it_reuses_the_frozen_predecessors(self):
        pairs = self.attr_pairs()
        wanted = {("sr", "get_record"), ("sr", "_watermark"), ("sr", "_trim"),
                  ("sr", "_window_canon"), ("sr", "_source_root_groups"),
                  ("sr", "read_reflection_inputs"), ("sr", "freeze_window"),
                  ("mc", "classify_fate"), ("cc", "adjudicate"),
                  ("dag", "read_invalidation_state")}
        self.assertEqual(set(), wanted - pairs,
                         "没有复用冻结前置：%s" % sorted(wanted - pairs))

    def test_no_second_evidence_tier_vocabulary(self):
        tiers = set(ca.EPISTEMIC_TYPES)
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            literals = {n.value for n in ast.walk(node.value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            self.assertFalse(tiers <= literals,
                             "L02 自带了第二套证据分层词表 %s" % sorted(tiers & literals))
        attrs = {(n.value.id, n.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for wanted in (("mc", "PROMOTABLE_TIERS"), ("mc", "NON_PROMOTABLE_TIERS"),
                       ("ca", "EPISTEMIC_TYPES"), ("ca", "FICTION_CONTEXTS"),
                       ("sr", "OUTPUT_KINDS")):
            self.assertIn(wanted, attrs, "分层/输出层词表必须复用 %s" % (wanted,))

    def test_no_second_conflict_root_or_invalidation_engine(self):
        engine = {"classify_fate", "consolidation_plan", "plan_reassessment", "adjudicate",
                  "adjudicate_pair", "classify_predicate", "candidate_key",
                  "collect_conflict_pairs", "_root_canon", "_core_value", "_family_of",
                  "_view", "_value_of", "_root_of", "commit_plan", "apply_consolidation",
                  "propagate", "correction", "reflection_plan", "commit_reflection",
                  "read_invalidation_state", "read_consolidation_inputs"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, engine, "重定义了冻结引擎函数 %s" % node.name)
                for word in ("classify", "adjudicate", "family", "verdict", "contradict"):
                    self.assertNotIn(word, node.name.lower(),
                                     "长出了第二套矛盾/分层机器 %s" % node.name)
        self.assertNotIn("def _root_canon", self.source)
        self.assertIn(("sr", "_window_canon"), self.attr_pairs())

    def test_no_write_sql_anywhere(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper, "L02 里出现写入 SQL：%s" % node.value[:60])

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
                             "L02 绕过既有入口写账/开库：%s" % self.call_name(call))

    def test_no_second_write_entry_at_all(self):
        """本模块选「纯计划 + 只读桥」：不落账、不写 L01 账本、不直接碰写入器。"""
        pairs = self.attr_pairs()
        for forbidden in (("mc", "apply_consolidation"), ("mc", "apply_reassessment"),
                          ("sr", "commit_reflection"), ("dag", "propagate"),
                          ("dag", "correction")):
            self.assertNotIn(forbidden, pairs, "L02 开了第二写入口 %s" % (forbidden,))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"cognition_writer", "self_model"},
                                 "L02 是纯计划输出，不直接接写入器/账本")

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"time", "random", "datetime"},
                                 "L02 不许 import 时钟/随机")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic"),
                                 "L02 决策路径读了时钟/随机 %s" % node.func.attr)

    def test_now_is_a_required_keyword(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "revision_plan")
        kw = [a.arg for a in fn.args.kwonlyargs]
        self.assertIn("now", kw, "revision_plan 必须显式接收 now")
        self.assertIsNone(fn.args.kw_defaults[kw.index("now")],
                          "revision_plan 的 now 不许有默认值（无墙钟）")

    def test_no_path_resolution_or_db_open_in_module(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists", "connect"),
                             "L02 自身解析路径/开库了；只读桥必须委托冻结前置")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"os", "sqlite3", "pathlib"},
                                 "L02 不该 import os/sqlite3/pathlib")
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_target_layer_is_belief_and_self_view_only(self):
        self.assertEqual(("belief", "self_view"), sv.TARGET_KINDS)
        self.assertEqual(sr.OUTPUT_KINDS, sv.OUTPUT_KINDS)
        self.assertNotIn("episode", sv.TARGET_KINDS)
        self.assertNotIn("episode", sv.OUTPUT_KINDS)
        self.assertNotIn("state", sv.TARGET_KINDS)

    def test_the_contract_constants(self):
        self.assertEqual("l02-1", sv.ALGORITHM_VERSION)
        self.assertEqual(ca.EPISTEMIC_TYPES, sv.EPISTEMIC_TYPES)
        self.assertEqual(mc.PROMOTABLE_TIERS, sv.PROMOTABLE_TIERS)
        self.assertEqual(mc.NON_PROMOTABLE_TIERS, sv.NON_PROMOTABLE_TIERS)
        self.assertEqual(ca.FICTION_CONTEXTS, sv.FICTION_CONTEXTS)
        self.assertEqual(("accepted", "held", "rejected"), sv.DECISION_STATUSES)
        for code in ("revision_not_event", "interpretation_not_evidence",
                     "evidence_not_from_reflection", "counterexample_conflict",
                     "counterexample_evidence", "prior_invalidation_preserved",
                     "no_change_proposed", "revision_accepted", "no_valid_evidence"):
            self.assertIn(code, sv.REASON_CODES)

    def test_limitations_are_honest(self):
        self.assertIsInstance(sv.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(sv.LIMITATIONS), 8)
        text = " ".join(sv.LIMITATIONS)
        for undone in ("LLM", "worker", "belief_revision", "self_view_rev",
                       "cognition_writer.commit", "单调", "原始事件", "L03"):
            self.assertIn(undone, text, "limitations 必须如实声明 %s" % undone)

    def test_docstring_declares_the_flow_and_boundaries(self):
        doc = ast.get_docstring(self.tree) or ""
        for phrase in ("提出修订", "检索证据", "检查反例", "接受或暂缓", "留版本",
                       "不接真实定时反思 worker", "不调 LLM", "不改原始事件"):
            self.assertIn(phrase, doc, "docstring 必须如实声明 %s" % phrase)


# ── 行为锁：门序各道门拒绝路径 ─────────────────────────────────────
class GateTests(unittest.TestCase):
    def setUp(self):
        self.rl, self.rid = l01_fixture()

    def decide(self, proposal, **kw):
        plan = sv.revision_plan([proposal], self.rl, kw.pop("w", window()), now=NOW, **kw)
        return plan["decisions"][0]

    def test_missing_proposal_id_is_rejected(self):
        d = self.decide(prop("", self.rid))
        self.assertEqual("rejected", d["status"])
        self.assertEqual("missing_proposal_id", d["reason_code"])

    def test_missing_object_id_is_rejected(self):
        d = self.decide(prop("p1", self.rid, oid=""))
        self.assertEqual("missing_object_id", d["reason_code"])

    def test_missing_target_id_is_rejected(self):
        d = self.decide(prop("p1", self.rid, target_id=""))
        self.assertEqual("missing_object_id", d["reason_code"])

    def test_episode_assertion_is_never_a_revision(self):
        d = self.decide(prop("p1", self.rid, target_kind="episode"))
        self.assertEqual("rejected", d["status"])
        self.assertEqual("revision_not_event", d["reason_code"])

    def test_state_and_commitment_are_not_revision_targets(self):
        for kind in ("state", "commitment", "experience", "vibe"):
            d = self.decide(prop("p1", self.rid, target_kind=kind))
            self.assertEqual("revision_not_event", d["reason_code"], kind)

    def test_missing_after_text_is_rejected(self):
        d = self.decide(prop("p1", self.rid, after="  "))
        self.assertEqual("missing_after_text", d["reason_code"])

    def test_missing_change_reason_is_rejected(self):
        d = self.decide(prop("p1", self.rid, reason=""))
        self.assertEqual("missing_change_reason", d["reason_code"])

    def test_unknown_reflection_is_rejected(self):
        d = self.decide(prop("p1", "l01_nope"))
        self.assertEqual("reflection_not_found", d["reason_code"])

    def test_missing_reflection_ledger_is_rejected(self):
        plan = sv.revision_plan([prop("p1", self.rid)], None, window(), now=NOW)
        self.assertEqual("reflection_not_found", plan["decisions"][0]["reason_code"])

    def test_l01_rejected_candidate_cannot_ground_a_revision(self):
        # 原句对不上 → L01 已拒绝；修订不能拿被拒候选当出处。
        rl, rid = l01_fixture([claim("r1", "喜欢冰可乐", "完全对不上的话", "evt_1")])
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        self.assertEqual("reflection_candidate_rejected", plan["decisions"][0]["reason_code"])

    def test_candidate_not_in_reflection_is_rejected(self):
        d = self.decide(prop("p1", self.rid, oid="rX"))
        self.assertEqual("candidate_not_in_reflection", d["reason_code"])

    def test_evidence_outside_reflection_grounding_is_rejected(self):
        d = self.decide(prop("p1", self.rid, support_event_ids=["evt_2"]))
        self.assertEqual("evidence_not_from_reflection", d["reason_code"])

    def test_caller_supplied_envelope_is_ignored(self):
        # 自带信封/证据一律不用：出处只能是 L01 账本里的落点。
        forged = prop("p1", self.rid, candidate={"content": "编造的"},
                      evidence=[{"event_id": "evt_9", "quote": "编造的"}])
        d = self.decide(forged)
        self.assertEqual("accepted", d["status"])
        self.assertEqual(["evt_1"], d["event_ids"])

    def test_invalidated_target_cannot_be_revived(self):
        for status in ("invalidated", "superseded"):
            d = self.decide(prop("p1", self.rid),
                            states={"bel_x": {"status": status}})
            self.assertEqual("held", d["status"], status)
            self.assertEqual("prior_invalidation_preserved", d["reason_code"], status)

    def test_no_change_proposed_is_rejected(self):
        beliefs = [{"object_id": "bel_x", "proposition": AFTER,
                    "subject_ref": "per_1001"}]
        d = self.decide(prop("p1", self.rid), beliefs=beliefs)
        self.assertEqual("no_change_proposed", d["reason_code"])

    def test_fiction_contexts_never_revise_real_facts(self):
        for context in ca.FICTION_CONTEXTS:
            rl, rid = l01_fixture([claim("r1", "塔罗说她累了", "我今天好累", "evt_3",
                                         context_kind=context)])
            plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
            d = plan["decisions"][0]
            self.assertEqual("held", d["status"], context)
            self.assertEqual("fiction_not_fact", d["reason_code"], context)

    def test_empty_support_is_held_with_reason(self):
        d = self.decide(prop("p1", self.rid, support_event_ids=[]))
        self.assertEqual("held", d["status"])
        self.assertEqual("no_valid_evidence", d["reason_code"])

    def test_missing_window_degrades_honestly(self):
        d = self.decide(prop("p1", self.rid), w=[])
        self.assertEqual("held", d["status"])
        self.assertEqual("evidence_not_verifiable", d["reason_code"])

    def test_inferred_l01_output_cannot_ground_acceptance(self):
        rl, rid = l01_fixture([claim("r1", "推断她累了", "我今天好累", "evt_3",
                                     tier="inferred")])
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("inferred_not_promotable", d["reason_code"])

    def test_imagined_l01_output_cannot_ground_acceptance(self):
        rl, rid = l01_fixture([claim("r1", "想象她累了", "我今天好累", "evt_3",
                                     tier="imagined")])
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        self.assertEqual("imagined_not_promotable", plan["decisions"][0]["reason_code"])

    def test_needs_reassessment_object_is_held(self):
        d = self.decide(prop("p1", self.rid), states={"r1": {"status": "needs_reassessment"}})
        self.assertEqual("held", d["status"])
        self.assertEqual("needs_reassessment_pending", d["reason_code"])

    def test_acceptance_carries_full_version_record(self):
        d = self.decide(prop("p1", self.rid))
        self.assertEqual("accepted", d["status"])
        self.assertEqual("revision_accepted", d["reason_code"])
        self.assertEqual(1, d["version"])
        self.assertEqual("", d["before"])
        self.assertEqual(AFTER, d["after"])
        self.assertEqual(REASON, d["reason"])
        self.assertEqual(["evt_1"], d["event_ids"])
        self.assertTrue(d["evidence"], "修订必须带逐命题原句支撑")
        self.assertEqual(self.rid, d["reflection_id"])
        self.assertEqual("r1", d["source_object_id"])
        self.assertTrue(d["revision_id"].startswith("l02_"))

    def test_before_baseline_comes_from_current_belief(self):
        beliefs = [{"object_id": "bel_x", "proposition": "我不太会主动找朋友",
                    "subject_ref": "per_1001"}]
        d = self.decide(prop("p1", self.rid), beliefs=beliefs)
        self.assertEqual("accepted", d["status"])
        self.assertEqual("我不太会主动找朋友", d["before"])

    def test_self_view_revision_uses_latest_view_as_baseline(self):
        views = [{"id": "sv1", "ts": 100.0, "text": "我不太会主动找朋友",
                  "reason": "旧印象", "event_ids": []},
                 {"id": "sv2", "ts": 200.0, "text": "我有点慢热",
                  "reason": "整点整理", "event_ids": []}]
        d = self.decide(prop("p1", self.rid, target_kind="self_view", target_id="self"),
                        self_views=views)
        self.assertEqual("accepted", d["status"])
        self.assertEqual("self_view", d["target_kind"])
        self.assertEqual("我有点慢热", d["before"])

    def test_every_held_or_rejected_decision_has_a_reason_code(self):
        cases = [prop("p1", self.rid, target_kind="episode"),
                 prop("p2", self.rid, support_event_ids=[]),
                 prop("p3", self.rid, reason=""),
                 prop("p4", "l01_nope")]
        plan = sv.revision_plan(cases, self.rl, window(), now=NOW)
        for d in plan["held"] + plan["rejected"]:
            self.assertTrue(d["reason_code"], "held/rejected 必须带 reason_code")
            self.assertEqual(0, d["version"], "非接受不得进版本轨迹")


# ── 行为锁：自证拦截（L01 产物与既往修订不得充当证据）─────────────
class SelfProofTests(unittest.TestCase):
    def test_citing_l01_output_as_support_is_self_proof(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid, support_event_ids=["r1"])],
                                rl, window(), now=NOW)
        self.assertEqual("interpretation_not_evidence", plan["decisions"][0]["reason_code"])
        self.assertEqual([], plan["accepted"])

    def test_citing_l01_ledger_id_as_support_is_self_proof(self):
        rl, rid = l01_fixture()
        record = sr.get_record(rl, rid)
        ledger_id = record["reflections"][0]["ledger_id"]
        plan = sv.revision_plan([prop("p1", rid, support_event_ids=[ledger_id])],
                                rl, window(), now=NOW)
        self.assertEqual("interpretation_not_evidence", plan["decisions"][0]["reason_code"])

    def test_citing_l01_output_as_counterexample_is_also_refused(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan(
            [prop("p1", rid, counter_evidence=[{"event_id": "r1", "quote": "x"}])],
            rl, window(), now=NOW)
        self.assertEqual("interpretation_not_evidence", plan["decisions"][0]["reason_code"])

    def test_citing_a_prior_revision_as_evidence_is_self_proof(self):
        led, _, record = accepted_fixture()
        rl, rid = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        plan = sv.revision_plan(
            [prop("p2", rid, oid="r2", support_event_ids=[record["revision_id"]])],
            rl, window(), ledger=led, now=NOW)
        self.assertEqual("interpretation_not_evidence", plan["decisions"][0]["reason_code"])

    def test_revision_never_writes_the_l01_ledger(self):
        rl, rid = l01_fixture()
        before = canonical(rl)
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        self.assertEqual(before, canonical(rl), "修订改写了 L01 反思账本")


# ── 行为锁：同根源转述不算独立证据/独立反例 ────────────────────────
class RootIndependenceTests(unittest.TestCase):
    def test_same_root_restatement_counts_as_one_independent_source(self):
        w = [source("e1", "我最喜欢喝冰可乐了"),
             source("e2", "我最喜欢喝冰可乐了", root_source_ids=["e1"])]
        rl, rid = l01_fixture([claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", ["e1", "e2"])], w)
        plan = sv.revision_plan([prop("p1", rid)], rl, w, now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual(1, d["independent_sources"], "同根源转述被当成了多份独立证据")
        self.assertEqual(["e1"], d["root_groups"])
        self.assertEqual(["e1", "e2"], d["event_ids"])

    def test_distinct_roots_count_as_independent_sources(self):
        w = [source("e1", "我最喜欢喝冰可乐了"), source("e2", "冰可乐真好喝")]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", ["e1", "e2"],
                     evidence=[{"event_id": "e1", "quote": "我最喜欢喝冰可乐了"},
                               {"event_id": "e2", "quote": "冰可乐真好喝"}])
        rl, rid = l01_fixture([cand], w)
        plan = sv.revision_plan([prop("p1", rid)], rl, w, now=NOW)
        self.assertEqual(2, plan["decisions"][0]["independent_sources"])

    def test_same_root_counterexample_is_dismissed_not_blocking(self):
        w = window() + [source("evt_4", "转述", root_source_ids=["evt_1"])]
        rl, rid = l01_fixture()
        plan = sv.revision_plan(
            [prop("p1", rid, counter_evidence=[{"event_id": "evt_4", "quote": "转述"}])],
            rl, w, now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("dismissed_only", d["counterexample_result"])
        self.assertEqual([{"event_id": "evt_4", "disposition": "dismissed_same_root"}],
                         d["counterexamples"])

    def test_root_canon_reuses_l01_r04_r05(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        self.assertEqual(sr._window_canon(window()), plan["provenance"]["root_canon"])


# ── 行为锁：反例检查（M04 裁决 + 显式反证）─────────────────────────
class CounterexampleTests(unittest.TestCase):
    def setUp(self):
        self.rl, self.rid = l01_fixture()

    def test_contradicting_other_belief_holds_the_revision(self):
        beliefs = [{"object_id": "bel_other", "proposition": "讨厌主动找朋友聊天",
                    "subject_ref": "per_1001"}]
        plan = sv.revision_plan(
            [prop("p1", self.rid, after="喜欢主动找朋友聊天")],
            self.rl, window(), beliefs=beliefs, now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("counterexample_conflict", d["reason_code"])
        self.assertEqual("blocked_conflict", d["counterexample_result"])

    def test_the_target_being_revoked_is_not_its_own_counterexample(self):
        # 旧观点反对新观点正是修订要表达的变化，不能据此挡死每次修订。
        beliefs = [{"object_id": "bel_x", "proposition": "讨厌主动找朋友聊天",
                    "subject_ref": "per_1001"}]
        plan = sv.revision_plan(
            [prop("p1", self.rid, after="喜欢主动找朋友聊天")],
            self.rl, window(), beliefs=beliefs, now=NOW)
        self.assertEqual("accepted", plan["decisions"][0]["status"])

    def test_multi_valued_preferences_coexist_not_block(self):
        beliefs = [{"object_id": "bel_other", "proposition": "喜欢喝茶",
                    "subject_ref": "per_1001"}]
        plan = sv.revision_plan(
            [prop("p1", self.rid, after="喜欢主动找朋友聊天")],
            self.rl, window(), beliefs=beliefs, now=NOW)
        self.assertEqual("accepted", plan["decisions"][0]["status"])

    def test_independent_counter_evidence_holds_the_revision(self):
        plan = sv.revision_plan(
            [prop("p1", self.rid,
                  counter_evidence=[{"event_id": "evt_3", "quote": "我今天好累"}])],
            self.rl, window(), now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("counterexample_evidence", d["reason_code"])
        self.assertEqual("blocked_evidence", d["counterexample_result"])
        self.assertEqual([{"event_id": "evt_3", "disposition": "independent_counterexample"}],
                         d["counterexamples"])

    def test_invalidated_counter_evidence_cannot_block(self):
        plan = sv.revision_plan(
            [prop("p1", self.rid,
                  counter_evidence=[{"event_id": "evt_3", "quote": "我今天好累"}])],
            self.rl, window(), states={"evt_3": {"status": "invalidated"}}, now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("dismissed_invalidated", d["counterexamples"][0]["disposition"])

    def test_retracted_counter_evidence_cannot_block(self):
        w = [source("evt_1", "我最喜欢喝冰可乐了"),
             source("evt_3", "我今天好累", retracted=True)]
        plan = sv.revision_plan(
            [prop("p1", self.rid,
                  counter_evidence=[{"event_id": "evt_3", "quote": "我今天好累"}])],
            self.rl, w, now=NOW)
        self.assertEqual("accepted", plan["decisions"][0]["status"])

    def test_unverifiable_counter_is_recorded_not_silently_dropped(self):
        plan = sv.revision_plan(
            [prop("p1", self.rid,
                  counter_evidence=[{"event_id": "evt_9", "quote": "不在窗口"}])],
            self.rl, window(), now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("unverified_not_in_window", d["counterexamples"][0]["disposition"])

    def test_caller_supplied_m04_records_are_respected(self):
        conflicts = [{"verdict": "contradiction", "reason_code": "explicit_negation_opposite",
                      "left_id": "p1", "right_id": "bel_other"}]
        plan = sv.revision_plan([prop("p1", self.rid)], self.rl, window(),
                                conflicts=conflicts, now=NOW)
        self.assertEqual("counterexample_conflict", plan["decisions"][0]["reason_code"])

    def test_same_root_dispute_record_does_not_block(self):
        conflicts = [{"verdict": "disputed", "reason_code": "same_root_not_independent",
                      "left_id": "p1", "right_id": "bel_other"}]
        plan = sv.revision_plan([prop("p1", self.rid)], self.rl, window(),
                                conflicts=conflicts, now=NOW)
        self.assertEqual("accepted", plan["decisions"][0]["status"])

    def test_no_counterexamples_records_none_found(self):
        plan = sv.revision_plan([prop("p1", self.rid)], self.rl, window(), now=NOW)
        self.assertEqual("none_found", plan["decisions"][0]["counterexample_result"])


# ── 行为锁：失效单调（M05 失效证据不得支撑接受）────────────────────
class MonotonicTests(unittest.TestCase):
    def test_m05_invalidated_support_cannot_ground_acceptance(self):
        rl, rid = l01_fixture()
        for status in ("invalidated", "superseded"):
            plan = sv.revision_plan([prop("p1", rid)], rl, window(),
                                    states={"evt_1": {"status": status}}, now=NOW)
            d = plan["decisions"][0]
            self.assertEqual("held", d["status"], status)
            self.assertEqual("source_invalidated", d["reason_code"], status)
            self.assertEqual([], plan["accepted"])

    def test_retracted_support_cannot_ground_acceptance(self):
        rl, rid = l01_fixture()
        w = [source("evt_1", "我最喜欢喝冰可乐了", retracted=True)]
        plan = sv.revision_plan([prop("p1", rid)], rl, w, now=NOW)
        self.assertEqual("source_invalidated", plan["decisions"][0]["reason_code"])

    def test_partial_support_loss_narrows_to_valid_sources(self):
        # 两个独立源，其一失效：只能收窄到仍有效的源（调用方显式收窄）。
        w = [source("e1", "我最喜欢喝冰可乐了"), source("e2", "冰可乐真好喝")]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", ["e1", "e2"],
                     evidence=[{"event_id": "e1", "quote": "我最喜欢喝冰可乐了"},
                               {"event_id": "e2", "quote": "冰可乐真好喝"}])
        rl, rid = l01_fixture([cand], w)
        plan = sv.revision_plan([prop("p1", rid, support_event_ids=["e2"])], rl, w,
                                states={"e1": {"status": "invalidated"}}, now=NOW)
        d = plan["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual(["e2"], d["event_ids"])

    def test_invalidated_l01_candidate_object_is_held(self):
        rl, rid = l01_fixture()
        for status in ("invalidated", "superseded"):
            plan = sv.revision_plan([prop("p1", rid)], rl, window(),
                                    states={"r1": {"status": status}}, now=NOW)
            self.assertEqual("prior_invalidation_preserved",
                             plan["decisions"][0]["reason_code"], status)


# ── 行为锁：版本 append-only 与账本完整性 ──────────────────────────
class LedgerTests(unittest.TestCase):
    def test_commit_appends_and_marks_committed(self):
        led, plan, record = accepted_fixture()
        self.assertTrue(record["committed"])
        self.assertEqual(plan["plan_id"], record["plan_id"])
        self.assertEqual([record["revision_id"]], sv.list_records(led))

    def test_duplicate_revision_rejected(self):
        led, plan, _ = accepted_fixture()
        with self.assertRaises(ValueError):
            sv.commit_revision(led, plan)
        self.assertEqual(1, len(sv.list_records(led)))

    def test_tampered_plan_id_rejected(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        plan["plan_id"] = "l02p_forged"
        with self.assertRaises(ValueError):
            sv.commit_revision(sv.new_ledger(), plan)

    def test_tampered_decision_rejected(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        plan["decisions"][0]["after"] = "偷偷改写"
        with self.assertRaises(ValueError):
            sv.commit_revision(sv.new_ledger(), plan)

    def test_tampered_revision_id_rejected(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        plan["decisions"][0]["revision_id"] = "l02_forged"
        with self.assertRaises(ValueError):
            sv.commit_revision(sv.new_ledger(), plan)

    def test_plan_does_not_write_ledger(self):
        rl, rid = l01_fixture()
        led = sv.new_ledger()
        before = canonical(led)
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), ledger=led, now=NOW)
        self.assertEqual("planned", plan["status"])
        self.assertEqual(before, canonical(led))

    def test_second_revision_appends_version_two_with_before_after_chain(self):
        led, _, first = accepted_fixture()
        rl, rid = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        plan = sv.revision_plan([prop("p2", rid, oid="r2", after="我不喜欢硬聊")],
                                rl, window(), ledger=led, now=NOW)
        records = sv.commit_revision(led, plan)
        second = records[0]
        self.assertEqual(2, second["version"])
        self.assertEqual(first["after"], second["before"])
        trail = sv.version_trail(led, "belief", "bel_x")
        self.assertEqual([1, 2], [r["version"] for r in trail])

    def test_old_records_are_byte_preserved_on_append(self):
        led, _, first = accepted_fixture()
        before = canonical(first)
        rl, rid = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        plan = sv.revision_plan([prop("p2", rid, oid="r2", after="我不喜欢硬聊")],
                                rl, window(), ledger=led, now=NOW)
        sv.commit_revision(led, plan)
        self.assertEqual(before, canonical(sv.get_record(led, first["revision_id"])))

    def test_stale_plan_breaking_version_trail_is_rejected(self):
        led, _, _ = accepted_fixture()
        # 不带 ledger 重新算的计划 version 仍是 1：提交必须拒绝版本断链。
        rl, rid = l01_fixture()
        stale = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        with self.assertRaises(ValueError):
            sv.commit_revision(led, stale)
        self.assertEqual(1, len(sv.list_records(led)))

    def test_two_proposals_same_target_chain_within_one_plan(self):
        rl, rid = l01_fixture()
        rl2, rid2 = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        # 合并两个 L01 输出到同一账本：直接构造两轮反思。
        led01 = sr.new_ledger()
        p1 = sr.reflection_plan([default_claim()], window(), now=NOW)
        sr.commit_reflection(led01, p1)
        p2 = sr.reflection_plan([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")],
                                window(), ledger=led01, now=NOW)
        r2 = sr.commit_reflection(led01, p2)
        plan = sv.revision_plan([prop("p1", p1["reflection_id"]),
                                 prop("p2", r2["reflection_id"], oid="r2", after="我不喜欢硬聊")],
                                led01, window(), now=NOW)
        self.assertEqual([1, 2], [d["version"] for d in plan["accepted"]])
        self.assertEqual(AFTER, plan["accepted"][1]["before"])
        del rl, rid, rl2, rid2

    def test_held_and_rejected_are_kept_for_audit_but_not_versioned(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid),
                                 prop("p2", rid, target_kind="episode")],
                                rl, window(), now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        self.assertEqual(2, len(sv.list_records(led)))
        self.assertEqual(1, len(sv.version_trail(led, "belief", "bel_x")))

    def test_get_record_returns_copy_and_never_writes(self):
        led, _, record = accepted_fixture()
        before = canonical(led)
        got = sv.get_record(led, record["revision_id"])
        got["event_ids"].clear()
        sv.version_trail(led, "belief", "bel_x")
        sv.what_changed(led, "belief", "bel_x")
        self.assertEqual(before, canonical(led))
        self.assertIsNone(sv.get_record(led, "l02_missing"))

    def test_bad_ledger_rejected(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        for bad in (None, {}, {"records": [], "order": []}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sv.commit_revision(bad, plan)

    def test_bad_plan_rejected(self):
        with self.assertRaises(ValueError):
            sv.commit_revision(sv.new_ledger(), {"nope": 1})
        with self.assertRaises(ValueError):
            sv.commit_revision(sv.new_ledger(), {"status": "planned", "plan_id": "x"})

    def test_version_trail_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            sv.version_trail(sv.new_ledger(), "episode", "bel_x")


# ── 行为锁：「什么改变了你的想法」只来自记录 ───────────────────────
class WhatChangedTests(unittest.TestCase):
    def test_no_record_answers_honestly_i_dont_know(self):
        got = sv.what_changed(sv.new_ledger(), "belief", "bel_x")
        self.assertFalse(got["known"])
        self.assertEqual("no_revision_record", got["reason_code"])
        self.assertIn("不知道", got["answer"])
        self.assertEqual([], got["changes"])

    def test_answer_is_assembled_only_from_recorded_fields(self):
        led, _, record = accepted_fixture()
        got = sv.what_changed(led, "belief", "bel_x")
        self.assertTrue(got["known"])
        self.assertEqual("answered_from_records", got["reason_code"])
        expected = "第1版把「」改成「%s」，理由：%s；依据事件：evt_1" % (AFTER, REASON)
        self.assertEqual(expected, got["answer"])
        self.assertEqual(["evt_1"], got["changes"][0]["event_ids"])
        self.assertEqual(record["reflection_id"], got["changes"][0]["reflection_id"])

    def test_answer_tracks_the_full_version_chain(self):
        led, _, first = accepted_fixture()
        rl, rid = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        plan = sv.revision_plan([prop("p2", rid, oid="r2", after="我不喜欢硬聊",
                                      reason="硬聊那次双方都尴尬")],
                                rl, window(), ledger=led, now=NOW)
        sv.commit_revision(led, plan)
        got = sv.what_changed(led, "belief", "bel_x")
        self.assertEqual(2, len(got["changes"]))
        self.assertIn("第2版", got["answer"])
        self.assertIn("硬聊那次双方都尴尬", got["answer"])
        self.assertIn("evt_2", got["answer"])
        self.assertEqual(first["after"], got["changes"][1]["before"])

    def test_held_only_history_is_not_a_change_story(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid, support_event_ids=[])],
                                rl, window(), now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        got = sv.what_changed(led, "belief", "bel_x")
        self.assertFalse(got["known"])
        self.assertEqual("no_revision_record", got["reason_code"])

    def test_unknown_target_answers_honestly(self):
        led, _, _ = accepted_fixture()
        got = sv.what_changed(led, "belief", "bel_other")
        self.assertFalse(got["known"])
        self.assertIn("不知道", got["answer"])

    def test_record_without_evidence_degrades_honestly(self):
        # 直接构造缺证据的旧记录（legacy 形态）：绝不替它编支撑经历。
        led = sv.new_ledger()
        led["records"]["l02_legacy"] = {
            "revision_id": "l02_legacy", "status": "accepted", "target_kind": "belief",
            "target_id": "bel_x", "version": 1, "before": "旧", "after": "新",
            "reason": "", "event_ids": [], "reflection_id": "", "counterexamples": []}
        led["order"].append("l02_legacy")
        got = sv.what_changed(led, "belief", "bel_x")
        self.assertFalse(got["known"])
        self.assertEqual("no_evidence_recorded", got["reason_code"])
        self.assertIn("不编造", got["answer"])

    def test_invalidated_evidence_is_flagged_without_rewriting_history(self):
        led, _, record = accepted_fixture()
        before = canonical(led)
        got = sv.what_changed(led, "belief", "bel_x",
                              states={"evt_1": {"status": "invalidated"}})
        self.assertTrue(got["known"], "历史修订仍然真实，只是证据待重评")
        self.assertEqual(["evt_1"], got["changes"][0]["invalidated_sources"])
        self.assertIn("失效", got["answer"])
        self.assertIn("历史不改", got["answer"])
        self.assertEqual(before, canonical(led))

    def test_unknown_target_kind_raises(self):
        with self.assertRaises(ValueError):
            sv.what_changed(sv.new_ledger(), "episode", "bel_x")

    def test_self_view_change_is_answerable(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan(
            [prop("p1", rid, target_kind="self_view", target_id="self")],
            rl, window(), now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        got = sv.what_changed(led, "self_view", "self")
        self.assertTrue(got["known"])
        self.assertIn(AFTER, got["answer"])


# ── 行为锁：失效后重评候选，不篡改历史 ─────────────────────────────
class ReassessmentTests(unittest.TestCase):
    def test_no_invalidation_yields_no_candidates(self):
        led, _, _ = accepted_fixture()
        self.assertEqual([], sv.reassessment_candidates(led))
        self.assertEqual([], sv.reassessment_candidates(led, {}, window()))

    def test_invalidated_support_yields_reassessment_candidate(self):
        led, _, record = accepted_fixture()
        before = canonical(led)
        got = sv.reassessment_candidates(led, {"evt_1": {"status": "invalidated"}})
        self.assertEqual(1, len(got))
        self.assertEqual(record["revision_id"], got[0]["revision_id"])
        self.assertEqual(["evt_1"], got[0]["lost_sources"])
        self.assertEqual([], got[0]["remaining_sources"])
        self.assertEqual("support_invalidated", got[0]["reason_code"])
        self.assertEqual("preserved", got[0]["history"])
        self.assertEqual(before, canonical(led), "重评候选篡改了历史")

    def test_retracted_window_support_yields_candidate(self):
        led, _, _ = accepted_fixture()
        w = [source("evt_1", "我最喜欢喝冰可乐了", retracted=True)]
        got = sv.reassessment_candidates(led, {}, w)
        self.assertEqual(["evt_1"], got[0]["lost_sources"])

    def test_partial_loss_keeps_remaining_sources(self):
        w = [source("e1", "我最喜欢喝冰可乐了"), source("e2", "冰可乐真好喝")]
        cand = claim("r1", "喜欢冰可乐", "我最喜欢喝冰可乐了", ["e1", "e2"],
                     evidence=[{"event_id": "e1", "quote": "我最喜欢喝冰可乐了"},
                               {"event_id": "e2", "quote": "冰可乐真好喝"}])
        rl, rid = l01_fixture([cand], w)
        plan = sv.revision_plan([prop("p1", rid)], rl, w, now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        got = sv.reassessment_candidates(led, {"e1": {"status": "invalidated"}}, w)
        self.assertEqual(["e1"], got[0]["lost_sources"])
        self.assertEqual(["e2"], got[0]["remaining_sources"])

    def test_held_records_are_not_reassessed(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid, support_event_ids=[])],
                                rl, window(), now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        self.assertEqual([], sv.reassessment_candidates(
            led, {"evt_1": {"status": "invalidated"}}))

    def test_reassessment_does_not_revive_or_rewrite_versions(self):
        led, _, record = accepted_fixture()
        sv.reassessment_candidates(led, {"evt_1": {"status": "superseded"}})
        trail = sv.version_trail(led, "belief", "bel_x")
        self.assertEqual(1, len(trail))
        self.assertEqual(record["after"], trail[0]["after"])
        self.assertEqual(["evt_1"], trail[0]["event_ids"], "历史支撑引用被悄悄改掉了")

    def test_candidates_are_deterministically_sorted(self):
        led, _, _ = accepted_fixture()
        rl, rid = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        plan = sv.revision_plan([prop("p2", rid, oid="r2", target_id="bel_a",
                                      after="另一条认识")], rl, window(), now=NOW)
        sv.commit_revision(led, plan)
        states = {"evt_1": {"status": "invalidated"}, "evt_2": {"status": "invalidated"}}
        got = sv.reassessment_candidates(led, states)
        self.assertEqual([("belief", "bel_a"), ("belief", "bel_x")],
                         [(c["target_kind"], c["target_id"]) for c in got])


# ── 行为锁：确定性（无墙钟、内容寻址、顺序无关、不改输入）──────────
class DeterminismTests(unittest.TestCase):
    def fixture(self):
        rl, rid = l01_fixture()
        props = [prop("p1", rid), prop("p2", rid, target_id="bel_y", after="另一种认识")]
        return rl, props

    def test_same_inputs_same_output(self):
        rl, props = self.fixture()
        one = sv.revision_plan(props, rl, window(), now=NOW)
        two = sv.revision_plan(props, rl, window(), now=NOW)
        self.assertEqual(canonical(one), canonical(two))

    def test_wall_clock_does_not_change_ids_or_decisions(self):
        rl, props = self.fixture()
        one = sv.revision_plan(props, rl, window(), now=2000.0)
        two = sv.revision_plan(props, rl, window(), now=99999.0)
        self.assertEqual(one["plan_id"], two["plan_id"])
        strip = lambda ds: [{k: v for k, v in d.items() if k != "ts"} for d in ds]
        self.assertEqual(strip(one["decisions"]), strip(two["decisions"]))
        self.assertEqual([d["revision_id"] for d in one["decisions"]],
                         [d["revision_id"] for d in two["decisions"]])

    def test_input_order_does_not_change_output(self):
        rl, props = self.fixture()
        forward = sv.revision_plan(props, rl, window(), now=NOW)
        backward = sv.revision_plan(list(reversed(props)), rl, list(reversed(window())),
                                    now=NOW)
        self.assertEqual(forward["plan_id"], backward["plan_id"])

    def test_inputs_are_not_mutated(self):
        rl, props = self.fixture()
        w = window()
        before = canonical([props, w, rl])
        sv.revision_plan(props, rl, w, now=NOW)
        self.assertEqual(before, canonical([props, w, rl]))

    def test_duplicate_proposals_do_not_duplicate_decisions(self):
        rl, props = self.fixture()
        baseline = sv.revision_plan(props, rl, window(), now=NOW)
        doubled = sv.revision_plan(props + props, rl, window() + window(), now=NOW)
        self.assertEqual(baseline["plan_id"], doubled["plan_id"])

    def test_missing_now_is_refused_loudly(self):
        rl, props = self.fixture()
        with self.assertRaises(ValueError):
            sv.revision_plan(props, rl, window(), now=None)
        with self.assertRaises(TypeError):
            sv.revision_plan(props, rl, window())

    def test_provenance_carries_no_clock_and_no_llm(self):
        rl, props = self.fixture()
        plan = sv.revision_plan(props, rl, window(), now=NOW)
        self.assertNotIn("ts", plan["provenance"])
        self.assertNotIn("now", plan["provenance"])
        self.assertFalse(plan["provenance"]["llm_revision"])

    def test_empty_proposals_yield_empty_plan(self):
        plan = sv.revision_plan([], None, [], now=NOW)
        self.assertEqual([], plan["decisions"])
        self.assertEqual({"accepted": 0, "held": 0, "rejected": 0}, plan["counts"])


# ── 行为锁：schema 对齐行（供将来单一写入者落账，本模块不写）───────
class SchemaRowTests(unittest.TestCase):
    def test_belief_revision_row_matches_schema_columns(self):
        led, _, record = accepted_fixture()
        row = sv.belief_revision_row(record)
        self.assertEqual({"id", "belief_id", "old_text", "new_text", "reason",
                          "event_ids", "ts"}, set(row))
        self.assertEqual(record["revision_id"], row["id"])
        self.assertEqual("bel_x", row["belief_id"])
        self.assertEqual(record["before"], row["old_text"])
        self.assertEqual(AFTER, row["new_text"])
        self.assertEqual(["evt_1"], row["event_ids"])
        self.assertEqual(NOW, row["ts"])
        del led

    def test_self_view_rev_row_matches_schema_columns(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan(
            [prop("p1", rid, target_kind="self_view", target_id="self")],
            rl, window(), now=NOW)
        record = sv.commit_revision(sv.new_ledger(), plan)[0]
        row = sv.self_view_rev_row(record)
        self.assertEqual({"id", "ts", "text", "reason", "event_ids"}, set(row))
        self.assertEqual(AFTER, row["text"])
        self.assertEqual(REASON, row["reason"])

    def test_held_record_cannot_become_a_schema_row(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid, support_event_ids=[])],
                                rl, window(), now=NOW)
        record = sv.commit_revision(sv.new_ledger(), plan)[0]
        with self.assertRaises(ValueError):
            sv.belief_revision_row(record)
        with self.assertRaises(ValueError):
            sv.self_view_rev_row(record)

    def test_wrong_kind_row_is_refused(self):
        led, _, record = accepted_fixture()
        with self.assertRaises(ValueError):
            sv.self_view_rev_row(record)
        del led

    def test_ledger_rows_only_accepted_in_trail_order(self):
        led, _, first = accepted_fixture()
        rl, rid = l01_fixture([claim("r2", "冰可乐真好喝", "冰可乐真好喝", "evt_2")])
        plan = sv.revision_plan([prop("p2", rid, oid="r2", after="我不喜欢硬聊"),
                                 prop("p3", rid, oid="r2", target_kind="episode")],
                                rl, window(), ledger=led, now=NOW)
        sv.commit_revision(led, plan)
        rows = sv.ledger_rows(led)
        self.assertEqual([1, 2], [r["old_text"] == "" and 1 or 2 for r in rows["belief_revision"]])
        self.assertEqual(first["after"], rows["belief_revision"][1]["old_text"])
        self.assertEqual([], rows["self_view_rev"])

    def test_ledger_rows_are_pure_read(self):
        led, _, _ = accepted_fixture()
        before = canonical(led)
        rows = sv.ledger_rows(led)
        rows["belief_revision"][0]["new_text"] = "篡改"
        self.assertEqual(before, canonical(led))


# ── 隔离账本夹具（DATA_DIR 调用时解析，绝不写真实 data/）──────────
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="l02_", ignore_cleanup_errors=True)
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


class BridgeTests(Base):
    def test_absent_ledger_degrades_without_creating(self):
        got = sv.read_revision_inputs()
        self.assertEqual([], got["candidates"])
        self.assertEqual([], got["beliefs"])
        self.assertEqual([{"store": "cognition", "reason": "absent"}], got["degradation"])
        self.assertEqual([], got["self_views"])
        self.assertEqual([], list(Path(self.root).iterdir()), "只读桥建了目录")

    def test_freeze_window_absent_returns_empty_and_creates_nothing(self):
        self.assertEqual([], sv.freeze_window(["evt_x"]))
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_reads_real_beliefs_through_frozen_bridges(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l02:b1")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        got = sv.read_revision_inputs()
        self.assertEqual([], got["degradation"])
        self.assertEqual(1, len(got["beliefs"]))
        self.assertTrue(got["beliefs"][0]["evidence"], "belief 原句 span 没重建")
        self.assertEqual({}, got["states"])

    def test_reads_never_modify_the_ledger(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l02:b2")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        path = Path(self.root, "persona", "cognition.db")
        before = path.read_bytes()
        sv.read_revision_inputs()
        sv.read_revision_inputs()
        self.assertEqual(before, path.read_bytes())

    def test_paths_resolve_at_call_time(self):
        eid = self.seed("我最喜欢喝冰可乐了", "l02:b3")
        w = sm.source_window([eid])
        cw.commit(self.real_claim("o1", "喜欢冰可乐", eid, "我最喜欢喝冰可乐了"), w, now=NOW)
        self.assertEqual(1, len(sv.read_revision_inputs()["beliefs"]))
        with tempfile.TemporaryDirectory(prefix="l02_other_") as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                self.assertEqual([], sv.read_revision_inputs()["beliefs"])
        self.assertEqual(1, len(sv.read_revision_inputs()["beliefs"]))

    def test_freeze_window_reads_real_events(self):
        eid = self.seed("我今天好累", "l02:b4")
        w = sv.freeze_window([eid, eid])
        self.assertEqual(1, len(w))
        self.assertEqual("我今天好累", w[0]["text"])

    def test_full_pure_flow_creates_no_files(self):
        # 纯计划 + 内存账本全流程不碰磁盘：隔离目录始终为空。
        eid = self.seed("我最喜欢喝冰可乐了", "l02:b5")
        w = sm.source_window([eid])
        rl = sr.new_ledger()
        plan01 = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid,
                                                     "我最喜欢喝冰可乐了")], w, now=NOW)
        rec = sr.commit_reflection(rl, plan01)
        plan = sv.revision_plan([prop("p1", rec["reflection_id"])], rl, w, now=NOW)
        led = sv.new_ledger()
        sv.commit_revision(led, plan)
        sv.what_changed(led, "belief", "bel_x")
        sv.reassessment_candidates(led)
        sv.ledger_rows(led)
        self.assertEqual(["persona"], [p.name for p in Path(self.root).iterdir()])
        with closing(sm._db()) as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM belief_revision").fetchone()[0])
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM self_view_rev").fetchone()[0])

    def test_m05_states_flow_into_revision_gates(self):
        # 真实 M05 失效状态经只读桥进入修订门：失效证据不得支撑接受。
        eid = self.seed("我最喜欢喝冰可乐了", "l02:b6")
        w = sm.source_window([eid])
        rl = sr.new_ledger()
        plan01 = sr.reflection_plan([self.real_claim("r1", "喜欢冰可乐", eid,
                                                     "我最喜欢喝冰可乐了")], w, now=NOW)
        rec = sr.commit_reflection(rl, plan01)
        st = dag.read_invalidation_state()
        correction = dag.correction("observation_invalid", eid, "撤回", ts=2600.0)
        prop05 = dag.propagate(st["edges"], [correction], epoch=st["epoch"],
                               states=st["states"], now=2600.0)
        sm.write_invalidation_transaction({"ts": 2600.0, "expected_epoch": st["epoch"],
                                           "edges": [], "corrections": [correction],
                                           "propagation": prop05})
        states = sv.read_revision_inputs()["states"]
        plan = sv.revision_plan([prop("p1", rec["reflection_id"])], rl, w,
                                states=states, now=NOW)
        self.assertEqual("held", plan["decisions"][0]["status"])
        self.assertEqual("source_invalidated", plan["decisions"][0]["reason_code"])


# ── LIMITATIONS 内容锁（如实声明没做的事）──────────────────────────
class LimitationsTests(unittest.TestCase):
    def test_limitations_declare_undone_work(self):
        text = " ".join(sv.LIMITATIONS)
        self.assertIn("worker", text)
        self.assertIn("LLM", text)
        self.assertIn("不接生产消费者", text)
        self.assertIn("单调", text)
        self.assertIn("belief_revision", text)
        self.assertIn("self_view_rev", text)
        self.assertIn("不改原始事件", text)

    def test_plan_carries_limitations(self):
        rl, rid = l01_fixture()
        plan = sv.revision_plan([prop("p1", rid)], rl, window(), now=NOW)
        self.assertEqual(list(sv.LIMITATIONS), plan["limitations"])


if __name__ == "__main__":
    unittest.main()