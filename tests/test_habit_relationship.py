# -*- coding: utf-8 -*-
"""L03 习惯和关系长期更新的回归锁（统一方案 §L 表 L03 / §7.4 / §11.2 / §11.3；
stage5/self）。

钉死的行为（每条判据正反例）：
  · 目标层只允许 habit/relationship；人格特质/事件/信念/自我认识一律拒绝；
  · 证据只能是独立行为/互动结果；口癖/单次心情/模型切换不是证据；
  · 晋升多维门：独立根源 + 时间跨度 + 真实成功结果（冻结开发集阈值，
    不是「出现三次就是真偏好」）；unknown/rejected/waited/failure 不冒充成功，
    但保留「为什么没做」；
  · 防确认偏误：反例检查必经，None（读不到）暂缓而非当成没有；注意/目标
    过滤移除反证则降级暂缓；独立反证挡下接受；
  · 身份一致性：缺基线降级暂缓，冲突暂缓，不悄悄改人格；
  · 失效单调：失效/撤回结果不得支撑接受；支撑后来失效 → 重评候选，历史不改；
  · 版本 append-only：重复 ID/篡改/版本断链拒绝，旧记录一字节不动；
  · 「什么改变了/为什么没做」只从记录回答，缺证据如实说不知道；
  · 落账为纯计划 + 自声明 schema 对齐行，本模块不开写入口、不建表；
    只读桥委托 M05，缺库报 absent，绝不建库。
AST 锁钉死「复用而非重造」：import 面只含冻结前置 + stdlib；必须调用
mm.digest / dag.read_invalidation_state 并复用 mc._TRUTH_LOST；无写入 SQL/
旧写入器/直连/第二写入口；无墙钟无随机、now 必填；不解析路径不开库；
目标层常量钉死 habit/relationship。

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
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import habit_relationship as hr     # noqa: E402
import memory_consolidation as mc   # noqa: E402
import memory_manifest as mm        # noqa: E402
import invalidation_dag as dag      # noqa: E402

NOW = 2_000_000.0
BASE = 1_000_000.0
DAY = 24 * 3600
AFTER = "有话题时我愿意主动找人"
REASON = "几次真实互动里我都先开了口，且对方回应积极"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def outcome(oid, root, ts, kind="action_outcome", result="success", **over):
    row = {"outcome_id": oid, "root_id": root, "ts": ts, "kind": kind,
           "result": result, "retracted": False}
    row.update(over)
    return row


def good_outcomes():
    """3 个独立根源 + 8 天跨度 + 3 个真实成功结果（满足多维门）。"""
    return [outcome("o1", "root_a", BASE),
            outcome("o2", "root_b", BASE + 4 * DAY),
            outcome("o3", "root_c", BASE + 8 * DAY)]


def prop(pid="p1", kind="habit", target="hab_x", after=AFTER, reason=REASON, **over):
    row = {"proposal_id": pid, "target_kind": kind, "target_id": target,
           "after": after, "reason": reason,
           "outcome_ids": ["o1", "o2", "o3"], "counter_evidence": []}
    row.update(over)
    return row


def identity(**over):
    row = {"statements": ["我是一个愿意主动的人"], "conflict_targets": [],
           "conflict_after": []}
    row.update(over)
    return row


def plan_for(props=None, outcomes=None, **kw):
    kw.setdefault("identity", identity())
    kw.setdefault("now", NOW)
    return hr.update_plan(props if props is not None else [prop()],
                          outcomes if outcomes is not None else good_outcomes(), **kw)


def accepted_fixture(**over):
    """一条已接受的 habit 更新（v1）。返回 (ledger, plan, record)。"""
    plan = plan_for([prop(**over)])
    led = hr.new_ledger()
    records = hr.commit_update(led, plan)
    return led, plan, records[0]


# ── AST 静态锁：复用冻结前置，不长第二套引擎，无写入口，无墙钟 ─────
class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "habit_relationship.py"), encoding="utf-8") as handle:
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
        allowed = {"copy", "json", "invalidation_dag", "memory_consolidation",
                   "memory_manifest"}
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(set(), imported - allowed,
                         "L03 import 了写入/网络/模型/时钟 API：%s" % sorted(imported - allowed))
        self.assertLessEqual({"invalidation_dag", "memory_consolidation",
                              "memory_manifest"}, imported,
                             "L03 必须复用 M05/M08/A03 冻结前置")

    def test_it_reuses_the_frozen_predecessors(self):
        pairs = self.attr_pairs()
        wanted = {("mm", "digest"), ("dag", "read_invalidation_state")}
        self.assertEqual(set(), wanted - pairs,
                         "没有复用冻结前置：%s" % sorted(wanted - pairs))
        attrs = {(n.value.id, n.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        self.assertIn(("mc", "_TRUTH_LOST"), attrs, "失效状态必须复用 M05/M08 冻结常量")

    def test_no_write_sql_anywhere(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE "):
                    self.assertNotIn(verb, upper, "L03 里出现写入 SQL：%s" % node.value[:60])

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
                             "L03 绕过既有入口写账/开库：%s" % self.call_name(call))

    def test_no_second_write_entry_at_all(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"cognition_writer", "self_model"},
                                 "L03 是纯计划输出，不直接接写入器/账本")
        pairs = self.attr_pairs()
        for forbidden in (("dag", "propagate"), ("dag", "correction"),
                          ("mc", "apply_consolidation")):
            self.assertNotIn(forbidden, pairs, "L03 开了第二写入口 %s" % (forbidden,))

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"time", "random", "datetime"},
                                 "L03 不许 import 时钟/随机")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic"),
                                 "L03 决策路径读了时钟/随机 %s" % node.func.attr)

    def test_now_is_a_required_keyword(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "update_plan")
        kw = [a.arg for a in fn.args.kwonlyargs]
        self.assertIn("now", kw, "update_plan 必须显式接收 now")
        self.assertIsNone(fn.args.kw_defaults[kw.index("now")],
                          "update_plan 的 now 不许有默认值（无墙钟）")

    def test_no_path_resolution_or_db_open_in_module(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists", "connect"),
                             "L03 自身解析路径/开库了；只读桥必须委托冻结前置")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
                self.assertEqual(set(), names & {"os", "sqlite3", "pathlib"},
                                 "L03 不该 import os/sqlite3/pathlib")

    def test_target_layer_is_habit_and_relationship_only(self):
        self.assertEqual(("habit", "relationship"), hr.TARGET_KINDS)
        for forbidden in ("episode", "personality", "trait", "belief", "self_view", "state"):
            self.assertNotIn(forbidden, hr.TARGET_KINDS)

    def test_the_contract_constants(self):
        self.assertEqual("l03-1", hr.ALGORITHM_VERSION)
        self.assertEqual(("accepted", "held", "rejected"), hr.DECISION_STATUSES)
        self.assertEqual(mc._TRUTH_LOST, hr._TRUTH_LOST)
        self.assertEqual(("action_outcome", "interaction_outcome"), hr.OUTCOME_KINDS)
        self.assertEqual(("utterance", "mood", "model_switch"), hr.NON_EVIDENCE_KINDS)
        for code in ("habit_not_personality", "utterance_not_evidence",
                     "mood_not_evidence", "model_switch_not_evidence",
                     "insufficient_independent_roots", "insufficient_time_span",
                     "insufficient_success_outcomes", "counter_evidence_unavailable",
                     "counter_evidence_filtered", "counterexample_evidence",
                     "identity_conflict", "identity_unavailable",
                     "prior_invalidation_preserved", "update_accepted"):
            self.assertIn(code, hr.REASON_CODES)

    def test_thresholds_are_frozen_dev_set_constants(self):
        self.assertIsInstance(hr.MIN_INDEPENDENT_ROOTS, int)
        self.assertIsInstance(hr.MIN_SUCCESS_OUTCOMES, int)
        self.assertIsInstance(hr.MIN_SPAN_SECONDS, int)
        self.assertGreaterEqual(hr.MIN_INDEPENDENT_ROOTS, 2)
        self.assertGreaterEqual(hr.MIN_SPAN_SECONDS, DAY)

    def test_limitations_are_honest(self):
        self.assertIsInstance(hr.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(hr.LIMITATIONS), 8)
        text = " ".join(hr.LIMITATIONS)
        for undone in ("LLM", "worker", "habit", "relationship", "L04"):
            self.assertIn(undone, text)


# ── 身份证 / 目标层 / 文本门 ───────────────────────────────────────
class IdentityGateTests(unittest.TestCase):
    def test_missing_proposal_id_rejected(self):
        d = plan_for([prop(pid="")])["decisions"][0]
        self.assertEqual("rejected", d["status"])
        self.assertEqual("missing_proposal_id", d["reason_code"])

    def test_missing_target_id_rejected(self):
        d = plan_for([prop(target="")])["decisions"][0]
        self.assertEqual("missing_object_id", d["reason_code"])

    def test_personality_target_rejected(self):
        for kind in ("personality", "trait", "episode", "belief", "self_view"):
            d = plan_for([prop(kind=kind)])["decisions"][0]
            self.assertEqual("rejected", d["status"])
            self.assertEqual("habit_not_personality", d["reason_code"])

    def test_missing_after_rejected(self):
        d = plan_for([prop(after="")])["decisions"][0]
        self.assertEqual("missing_after_text", d["reason_code"])

    def test_missing_reason_rejected(self):
        d = plan_for([prop(reason="")])["decisions"][0]
        self.assertEqual("missing_change_reason", d["reason_code"])

    def test_no_change_rejected(self):
        plan = plan_for([prop()], habits=[{"target_id": "hab_x", "text": AFTER}])
        d = plan["decisions"][0]
        self.assertEqual("rejected", d["status"])
        self.assertEqual("no_change_proposed", d["reason_code"])

    def test_valid_habit_accepted(self):
        d = plan_for()["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("update_accepted", d["reason_code"])
        self.assertEqual(1, d["version"])
        self.assertEqual(3, d["independent_sources"])
        self.assertEqual(3, d["success_count"])

    def test_valid_relationship_accepted(self):
        d = plan_for([prop(kind="relationship", target="rel_y")])["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("relationship", d["target_kind"])


# ── 证据类型门：口癖/心情/模型切换不是证据 ─────────────────────────
class EvidenceKindTests(unittest.TestCase):
    def test_utterance_only_rejected(self):
        outs = [outcome("u1", "r", BASE, kind="utterance")]
        d = plan_for([prop(outcome_ids=["u1"])], outs)["decisions"][0]
        self.assertEqual("rejected", d["status"])
        self.assertEqual("utterance_not_evidence", d["reason_code"])

    def test_mood_only_rejected(self):
        outs = [outcome("m1", "r", BASE, kind="mood")]
        d = plan_for([prop(outcome_ids=["m1"])], outs)["decisions"][0]
        self.assertEqual("mood_not_evidence", d["reason_code"])

    def test_model_switch_only_rejected(self):
        outs = [outcome("s1", "r", BASE, kind="model_switch")]
        d = plan_for([prop(outcome_ids=["s1"])], outs)["decisions"][0]
        self.assertEqual("model_switch_not_evidence", d["reason_code"])

    def test_no_evidence_at_all_held(self):
        d = plan_for([prop(outcome_ids=[])], [])["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("no_valid_evidence", d["reason_code"])

    def test_mixed_filters_non_evidence_and_accepts(self):
        outs = good_outcomes() + [outcome("u1", "root_a", BASE, kind="utterance")]
        d = plan_for([prop(outcome_ids=["o1", "o2", "o3", "u1"])], outs)["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertNotIn("u1", d["outcome_ids"])
        self.assertEqual(3, d["independent_sources"])

    def test_single_mood_cannot_change_personality(self):
        # 单次心情即便配上足够行为结果，心情本身也不计入支撑。
        outs = good_outcomes() + [outcome("m1", "root_m", BASE, kind="mood")]
        d = plan_for([prop(outcome_ids=["o1", "o2", "o3", "m1"])], outs)["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertNotIn("m1", d["outcome_ids"])


# ── 多维晋升门：独立根源 / 时间跨度 / 真实成功结果 ─────────────────
class SufficiencyTests(unittest.TestCase):
    def test_insufficient_independent_roots_held(self):
        outs = [outcome("o1", "root_a", BASE), outcome("o2", "root_a", BASE + 4 * DAY),
                outcome("o3", "root_b", BASE + 8 * DAY)]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("insufficient_independent_roots", d["reason_code"])
        self.assertEqual(2, d["independent_sources"])

    def test_same_root_repetition_is_not_independent(self):
        # 同根源出现三次≠真偏好：独立根源只有 1。
        outs = [outcome("o%d" % i, "root_a", BASE + i * 4 * DAY) for i in range(3)]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("insufficient_independent_roots", d["reason_code"])
        self.assertEqual(1, d["independent_sources"])

    def test_insufficient_time_span_held(self):
        outs = [outcome("o1", "root_a", BASE), outcome("o2", "root_b", BASE + DAY),
                outcome("o3", "root_c", BASE + 2 * DAY)]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("insufficient_time_span", d["reason_code"])

    def test_span_boundary_exactly_min_accepted(self):
        outs = [outcome("o1", "root_a", BASE), outcome("o2", "root_b", BASE + DAY),
                outcome("o3", "root_c", BASE + hr.MIN_SPAN_SECONDS)]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("accepted", d["status"])

    def test_insufficient_success_outcomes_held(self):
        outs = [outcome("o1", "root_a", BASE), outcome("o2", "root_b", BASE + 4 * DAY),
                outcome("o3", "root_c", BASE + 8 * DAY, result="unknown")]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("insufficient_success_outcomes", d["reason_code"])
        self.assertEqual(2, d["success_count"])

    def test_non_success_preserved_as_why_not_done(self):
        outs = good_outcomes() + [outcome("o4", "root_d", BASE + 9 * DAY, result="waited"),
                                  outcome("o5", "root_e", BASE + 10 * DAY, result="rejected")]
        d = plan_for([prop(outcome_ids=["o1", "o2", "o3", "o4", "o5"])], outs)["decisions"][0]
        self.assertEqual("accepted", d["status"])
        kept = {r["outcome_id"]: r["result"] for r in d["non_success_outcomes"]}
        self.assertEqual("waited", kept["o4"])
        self.assertEqual("rejected", kept["o5"])
        self.assertEqual(3, d["success_count"])

    def test_unknown_never_counts_as_success(self):
        outs = [outcome("o1", "root_a", BASE, result="unknown"),
                outcome("o2", "root_b", BASE + 4 * DAY, result="unknown"),
                outcome("o3", "root_c", BASE + 8 * DAY, result="unknown")]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("insufficient_success_outcomes", d["reason_code"])
        self.assertEqual(0, d["success_count"])


# ── 失效单调 ───────────────────────────────────────────────────────
class InvalidationTests(unittest.TestCase):
    def test_retracted_support_cannot_accept(self):
        outs = [dict(o, retracted=True) for o in good_outcomes()]
        d = plan_for(outcomes=outs)["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("evidence_invalidated", d["reason_code"])

    def test_states_invalidated_support_cannot_accept(self):
        states = {"o1": {"status": "invalidated"}, "o2": {"status": "superseded"},
                  "o3": {"status": "invalidated"}}
        d = plan_for(states=states)["decisions"][0]
        self.assertEqual("evidence_invalidated", d["reason_code"])

    def test_partial_invalidation_filters_and_holds(self):
        states = {"o3": {"status": "invalidated"}}
        d = plan_for(states=states)["decisions"][0]
        # 只剩 2 个有效根源 → 独立根源不足。
        self.assertEqual("insufficient_independent_roots", d["reason_code"])
        self.assertNotIn("o3", d["outcome_ids"])

    def test_invalidated_target_not_revived(self):
        states = {"hab_x": {"status": "invalidated"}}
        d = plan_for(states=states)["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("prior_invalidation_preserved", d["reason_code"])

    def test_reassessment_after_support_invalidated(self):
        led, plan, rec = accepted_fixture()
        before = copy.deepcopy(led)
        cands = hr.reassessment_candidates(led, {"o1": {"status": "invalidated"}})
        self.assertEqual(1, len(cands))
        self.assertEqual(rec["update_id"], cands[0]["update_id"])
        self.assertEqual(["o1"], cands[0]["lost_sources"])
        self.assertEqual("preserved", cands[0]["history"])
        self.assertEqual(before, led, "重评候选不得改账本")

    def test_reassessment_none_when_support_valid(self):
        led, _, _ = accepted_fixture()
        self.assertEqual([], hr.reassessment_candidates(led, {}))


# ── 反例门：防确认偏误 ─────────────────────────────────────────────
class CounterEvidenceTests(unittest.TestCase):
    def test_counter_none_held_not_assumed_absent(self):
        d = plan_for([prop(counter_evidence=None)])["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("counter_evidence_unavailable", d["reason_code"])

    def test_counter_empty_list_accepted(self):
        d = plan_for([prop(counter_evidence=[])])["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("none_found", d["counterexample_result"])

    def test_independent_counter_blocks(self):
        counters = [outcome("c1", "root_d", BASE + DAY, result="failure")]
        d = plan_for([prop(counter_evidence=counters)])["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("counterexample_evidence", d["reason_code"])
        self.assertEqual("blocked_evidence", d["counterexample_result"])

    def test_same_root_counter_dismissed(self):
        counters = [outcome("c1", "root_a", BASE + DAY, result="failure")]
        d = plan_for([prop(counter_evidence=counters)])["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("dismissed_only", d["counterexample_result"])
        self.assertEqual("dismissed_same_root", d["counterexamples"][0]["disposition"])

    def test_invalidated_counter_dismissed(self):
        counters = [outcome("c1", "root_d", BASE + DAY, retracted=True)]
        d = plan_for([prop(counter_evidence=counters)])["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("dismissed_invalidated", d["counterexamples"][0]["disposition"])

    def test_filtered_counter_degrades_and_holds(self):
        plan = plan_for([prop(counter_evidence=[], counter_evidence_filtered=True)])
        d = plan["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("counter_evidence_filtered", d["reason_code"])

    def test_counter_check_is_mandatory_even_with_strong_evidence(self):
        # 证据再强，读不到反证也不能跳过反例门。
        d = plan_for([prop(counter_evidence=None)])["decisions"][0]
        self.assertEqual(3, d["independent_sources"])
        self.assertEqual("counter_evidence_unavailable", d["reason_code"])


# ── 身份一致性 ─────────────────────────────────────────────────────
class IdentityConsistencyTests(unittest.TestCase):
    def test_identity_none_held_and_degraded(self):
        plan = plan_for(identity=None)
        d = plan["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("identity_unavailable", d["reason_code"])
        self.assertEqual("degraded", plan["status"])

    def test_identity_conflict_target_held(self):
        plan = plan_for(identity=identity(conflict_targets=["hab_x"]))
        d = plan["decisions"][0]
        self.assertEqual("held", d["status"])
        self.assertEqual("identity_conflict", d["reason_code"])
        self.assertEqual("conflict", d["identity_result"])

    def test_identity_conflict_after_held(self):
        plan = plan_for(identity=identity(conflict_after=[AFTER]))
        d = plan["decisions"][0]
        self.assertEqual("identity_conflict", d["reason_code"])

    def test_identity_consistent_accepted(self):
        d = plan_for(identity=identity())["decisions"][0]
        self.assertEqual("accepted", d["status"])
        self.assertEqual("consistent", d["identity_result"])


# ── 账本 append-only ───────────────────────────────────────────────
class LedgerTests(unittest.TestCase):
    def test_commit_records_all_statuses(self):
        props = [prop("p1"), prop("p2", target="hab_y", counter_evidence=None)]
        plan = plan_for(props)
        led = hr.new_ledger()
        records = hr.commit_update(led, plan)
        self.assertEqual(2, len(records))
        self.assertEqual(2, len(hr.list_records(led)))

    def test_duplicate_commit_rejected(self):
        led, plan, _ = accepted_fixture()
        with self.assertRaises(ValueError):
            hr.commit_update(led, plan)

    def test_tampered_decision_rejected(self):
        plan = plan_for()
        plan["decisions"][0]["after"] = "被篡改的策略"
        led = hr.new_ledger()
        with self.assertRaises(ValueError):
            hr.commit_update(led, plan)

    def test_tampered_plan_id_rejected(self):
        plan = plan_for()
        plan["plan_id"] = "l03p_forged"
        led = hr.new_ledger()
        with self.assertRaises(ValueError):
            hr.commit_update(led, plan)

    def test_version_break_rejected(self):
        plan = plan_for()
        plan["decisions"][0]["version"] = 5
        # 重算 update_id 使内容哈希自洽，但版本断链仍须拒绝。
        plan["decisions"][0]["update_id"] = hr._update_id(plan["decisions"][0])
        plan["plan_id"] = hr._plan_id(plan["decisions"])
        led = hr.new_ledger()
        with self.assertRaises(ValueError):
            hr.commit_update(led, plan)

    def test_before_mismatch_rejected(self):
        led, _, rec = accepted_fixture()
        plan2 = plan_for([prop("p2", after="第二版策略")], ledger=led)
        plan2["decisions"][0]["before"] = "错误的基线"
        plan2["decisions"][0]["update_id"] = hr._update_id(plan2["decisions"][0])
        plan2["plan_id"] = hr._plan_id(plan2["decisions"])
        with self.assertRaises(ValueError):
            hr.commit_update(led, plan2)

    def test_second_version_chains_before(self):
        led, _, rec = accepted_fixture()
        plan2 = plan_for([prop("p2", after="第二版策略")], ledger=led)
        rec2 = hr.commit_update(led, plan2)[0]
        self.assertEqual(2, rec2["version"])
        self.assertEqual(AFTER, rec2["before"])
        trail = hr.version_trail(led, "habit", "hab_x")
        self.assertEqual([1, 2], [r["version"] for r in trail])

    def test_old_record_untouched_after_new_commit(self):
        led, _, rec = accepted_fixture()
        snapshot = copy.deepcopy(hr.get_record(led, rec["update_id"]))
        hr.commit_update(led, plan_for([prop("p2", after="第二版策略")], ledger=led))
        self.assertEqual(snapshot, hr.get_record(led, rec["update_id"]))

    def test_bad_ledger_rejected(self):
        with self.assertRaises(ValueError):
            hr.commit_update({}, plan_for())

    def test_bad_plan_rejected(self):
        led = hr.new_ledger()
        with self.assertRaises(ValueError):
            hr.commit_update(led, {"status": "bogus"})


# ── 纯读视图 ───────────────────────────────────────────────────────
class ViewTests(unittest.TestCase):
    def test_version_trail_only_accepted(self):
        led, _, _ = accepted_fixture()
        hr.commit_update(led, plan_for([prop("p2", target="hab_z", counter_evidence=None)]))
        self.assertEqual([], hr.version_trail(led, "habit", "hab_z"))
        self.assertEqual(1, len(hr.version_trail(led, "habit", "hab_x")))

    def test_version_trail_unknown_kind_raises(self):
        led = hr.new_ledger()
        with self.assertRaises(ValueError):
            hr.version_trail(led, "personality", "x")

    def test_what_changed_no_record_honest_unknown(self):
        led = hr.new_ledger()
        got = hr.what_changed(led, "habit", "hab_x")
        self.assertFalse(got["known"])
        self.assertEqual("no_update_record", got["reason_code"])

    def test_what_changed_answered_from_records(self):
        led, _, rec = accepted_fixture()
        got = hr.what_changed(led, "habit", "hab_x")
        self.assertTrue(got["known"])
        self.assertEqual("answered_from_records", got["reason_code"])
        self.assertIn(AFTER, got["answer"])
        self.assertIn("o1", got["answer"])

    def test_what_changed_marks_invalidated_support(self):
        led, _, _ = accepted_fixture()
        got = hr.what_changed(led, "habit", "hab_x", states={"o1": {"status": "invalidated"}})
        self.assertIn("待重评", got["answer"])
        self.assertEqual(["o1"], got["changes"][0]["invalidated_sources"])

    def test_what_changed_missing_evidence_honest_unknown(self):
        led = hr.new_ledger()
        led["records"]["x"] = {"update_id": "x", "status": "accepted",
                               "target_kind": "habit", "target_id": "hab_x",
                               "version": 1, "before": "", "after": "a",
                               "reason": "", "outcome_ids": []}
        led["order"].append("x")
        got = hr.what_changed(led, "habit", "hab_x")
        self.assertFalse(got["known"])
        self.assertEqual("no_evidence_recorded", got["reason_code"])

    def test_why_not_done_preserves_held_and_rejected(self):
        props = [prop("p1", target="hab_x", counter_evidence=None),
                 prop("p2", target="hab_x", kind="personality")]
        led = hr.new_ledger()
        hr.commit_update(led, plan_for(props))
        reasons = hr.why_not_done(led, "habit", "hab_x")
        codes = {r["reason_code"] for r in reasons}
        self.assertIn("counter_evidence_unavailable", codes)
        self.assertEqual(1, len(reasons))  # personality 那条 target_kind 不是 habit

    def test_why_not_done_empty_for_accepted_only(self):
        led, _, _ = accepted_fixture()
        self.assertEqual([], hr.why_not_done(led, "habit", "hab_x"))

    def test_get_record_deep_copy(self):
        led, _, rec = accepted_fixture()
        got = hr.get_record(led, rec["update_id"])
        got["after"] = "改坏了"
        self.assertNotEqual(got["after"], hr.get_record(led, rec["update_id"])["after"])


# ── 确定性 / 输入不可变 ────────────────────────────────────────────
class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        a = plan_for()
        b = plan_for()
        self.assertEqual(a["plan_id"], b["plan_id"])
        self.assertEqual(canonical(a["decisions"]), canonical(b["decisions"]))

    def test_order_independent(self):
        props = [prop("p1"), prop("p2", target="hab_y"), prop("p3", target="hab_z")]
        outs = good_outcomes()
        a = plan_for(props, outs)
        b = plan_for(list(reversed(props)), list(reversed(outs)))
        self.assertEqual(a["plan_id"], b["plan_id"])

    def test_inputs_not_mutated(self):
        props = [prop()]
        outs = good_outcomes()
        ident = identity()
        props_snap, outs_snap, ident_snap = copy.deepcopy(props), copy.deepcopy(outs), copy.deepcopy(ident)
        plan_for(props, outs, identity=ident)
        self.assertEqual(props_snap, props)
        self.assertEqual(outs_snap, outs)
        self.assertEqual(ident_snap, ident)

    def test_now_required_keyword(self):
        with self.assertRaises(TypeError):
            hr.update_plan([prop()], good_outcomes(), identity=identity())

    def test_now_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            hr.update_plan([prop()], good_outcomes(), identity=identity(), now="soon")

    def test_update_id_content_addressed_no_clock(self):
        a = plan_for(now=NOW)["decisions"][0]
        b = plan_for(now=NOW + 999)["decisions"][0]
        self.assertEqual(a["update_id"], b["update_id"])
        self.assertNotEqual(a["ts"], b["ts"])

    def test_duplicate_proposal_id_deduped(self):
        plan = plan_for([prop("p1"), prop("p1")])
        self.assertEqual(1, len(plan["decisions"]))


# ── 只读桥 / schema 行 ─────────────────────────────────────────────
class ReadOnlyBridgeTests(unittest.TestCase):
    def test_absent_db_returns_empty_and_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="l03_bridge_") as tmp:
            with patch.dict(os.environ, {"DATA_DIR": tmp, "PERSONA_DATA_DIR": ""}):
                got = hr.read_update_inputs()
            self.assertEqual([], got["outcomes"])
            self.assertEqual({}, got["states"])
            self.assertIn("outcome_note", got)
            self.assertFalse(os.path.exists(os.path.join(tmp, "persona", "cognition.db")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "cognition.db")))

    def test_bridge_delegates_to_frozen_m05(self):
        with patch.object(dag, "read_invalidation_state",
                          return_value={"states": {"o1": {"status": "invalidated"}},
                                        "edges": [], "epoch": 3}) as mock:
            got = hr.read_update_inputs()
        mock.assert_called_once()
        self.assertEqual({"o1": {"status": "invalidated"}}, got["states"])
        self.assertEqual(3, got["epoch"])


class SchemaRowTests(unittest.TestCase):
    def test_habit_row(self):
        led, _, rec = accepted_fixture()
        row = hr.habit_row(rec)
        self.assertEqual(rec["update_id"], row["id"])
        self.assertEqual("hab_x", row["target_id"])
        self.assertEqual(AFTER, row["new_text"])
        self.assertEqual(["o1", "o2", "o3"], row["outcome_ids"])

    def test_relationship_row(self):
        plan = plan_for([prop(kind="relationship", target="rel_y")])
        led = hr.new_ledger()
        rec = hr.commit_update(led, plan)[0]
        row = hr.relationship_row(rec)
        self.assertEqual("rel_y", row["target_id"])

    def test_habit_row_wrong_kind_raises(self):
        led, _, rec = accepted_fixture()
        with self.assertRaises(ValueError):
            hr.relationship_row(rec)

    def test_ledger_rows_separates_kinds(self):
        led = hr.new_ledger()
        hr.commit_update(led, plan_for([prop("p1"), prop("p2", kind="relationship", target="rel_y")]))
        rows = hr.ledger_rows(led)
        self.assertEqual(1, len(rows["habit"]))
        self.assertEqual(1, len(rows["relationship"]))

    def test_ledger_rows_excludes_non_accepted(self):
        led = hr.new_ledger()
        hr.commit_update(led, plan_for([prop("p1", counter_evidence=None)]))
        rows = hr.ledger_rows(led)
        self.assertEqual([], rows["habit"])


if __name__ == "__main__":
    unittest.main()