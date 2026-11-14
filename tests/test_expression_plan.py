# -*- coding: utf-8 -*-
"""C05 ExpressionPlan 隔离接线的回归锁（路线 §3.2 / §8.3 / §10 阶段3；S06 连续性门）。

钉死的行为（每条判据正反例）：
  · 形状拒绝：snapshot/injection 必须是 C04 真实产物，缺骨架大声 ValueError，
    不部分信任；注入必须绑定同一份快照（snapshot_id/state_version/now），
    别的快照的注入绝不装配进计划；
  · 三层保真：按 ws.LAYERS 固定顺序逐字搬运 C04 已渲染的 text/chars/truncated/
    cache_hit 等真实字段，不改写、不重渲染、不补造；excluded/degradation/status
    原样透传，degraded 不冒充 ok；
  · tone 门：continuity 缺席/形状非法/矩阵失败/未覆盖 shadow-v2/情绪缺席 →
    tone 如实不可用并大声登记（五种 reason），三层文本仍装配；全部通过才携带
    tone；tone 恒 calibrated=False、basis='emotion_kernel'，attention 不进 tone；
  · tone 阈值：显式冻结阈值的闭边界分档（low/mid/high），值来自 C04 已裁剪投影；
  · 计划不裁决行动：没有 act/wait/rejected，expression_control 恒 False；
  · plan_id 内容寻址：同输入同输出、输入顺序无关、深拷贝不改输入；
    state_version 变 → plan_id 立即变（不按日期缓存）。

AST 锁钉死「复用而非重造」：import 面只含 stdlib + C04/S06/A03/情感内核冻结
前置；必须复用 ws.ALGORITHM_VERSION/ws.LAYERS/mm.digest/s06.CONDITIONS/
s06.KERNELS/ed.DIMENSIONS；不 import bridge/self_model/os/时钟/随机/网络；
零写入、不开库、不解析路径；build_plan 不接收 now（now 只来自注入）；
expression_control 只能以 False 常量出现；LIMITATIONS 如实声明 C06/表达接管/
LLM 没做。

所有合成数据只出现在内存或 tempfile 隔离目录里，是测试夹具，不是自然样本；
绝不写真实 data/。端到端用例经真实 s06.run_condition_matrix 跑合成夹具产出
真报告（夹具构造参照 tests/test_emotion_replay_matrix.py），再经真实
ws.build_snapshot/render_injection 链路装配计划。
"""
import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import expression_plan as xp         # noqa: E402
import work_state as ws              # noqa: E402
import emotion_replay_matrix as s06  # noqa: E402
import emotion_dynamics as ed        # noqa: E402
import memory_manifest as mm         # noqa: E402
import ripple_conflicts as rc        # noqa: E402
import ripple_evidence as re6        # noqa: E402

NOW = 4000000000.0
# 矩阵夹具的时间基（与 tests/test_emotion_replay_matrix.py 同口径）。
START, END = 900.0, 5000.0
PRAISE = '谢谢你帮我查清楚了'
REBUKE = '你刚才说错了'
_DEFAULT = object()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def reordered(value):
    """递归反转 dict 键序（列表顺序是语义，不动）：验证输入顺序无关。"""
    if isinstance(value, dict):
        return {k: reordered(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reordered(v) for v in value]
    return value


def snapshot_tree(root):
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


# ── 合成夹具（内存里的测试数据，不是自然样本；参照 test_work_state.py）──


def emotion(at=NOW, **over):
    state = ed.initial_state(at)
    state.update(over)
    return state


def commitment(oid="cmt_1", **over):
    row = {"id": oid, "to_qq": "per_1001", "text": "帮查荒坂塔资料",
           "due_ts": NOW + 3600, "status": "open", "event_ids": ["evt_1"]}
    row.update(over)
    return row


def goal(oid="gol_1", **over):
    row = {"id": oid, "text": "弄清荒坂塔原型", "motive": "求知",
           "owner_qq": "per_1001", "status": "active"}
    row.update(over)
    return row


def waiting(oid="pend_1", **over):
    row = {"pending_id": oid, "tool": "bili_search", "requested_at": NOW - 120}
    row.update(over)
    return row


def relation(subject="per_1001", affection=5):
    return {"subject_ref": subject, "affection": affection}


def identity(**over):
    row = {"text": "她是清漪，现代人语域，自称我。",
           "object_revision": "rev_1", "source_revision": "src_1"}
    row.update(over)
    return row


def snap(**kw):
    base = dict(now=NOW, emotion=emotion(), commitments=[commitment()],
                goals=[goal()], pending=[waiting()], relations=[relation()],
                budget={"limit": 5, "used": 2},
                events=[{"id": "evt_1", "kind": "group_message", "ts": NOW - 60}],
                scope="private:1001")
    base.update(kw)
    return ws.build_snapshot(**base)


def claim_item(oid, value, sources=(), validity="valid"):
    return {"object_id": oid, "object_kind": "belief", "store": "cognition",
            "root_source_ids": list(sources), "validity": validity,
            "subject_ref": "per_1", "restatement": value, "occurred_at": 1000.0,
            "evidence_spans": [{"event_id": s, "quote": value, "subject_ref": "per_1"}
                               for s in sources]}


def edge(a, b):
    return {"edge_id": "edge_%s_%s" % (a, b), "type": "shared_entity",
            "a": a, "a_kind": "belief", "b": b, "b_kind": "belief",
            "basis": {"reason": "shared_entity"}}


def evidence_two_scenes():
    s = claim_item("s", "喜欢茶", ["e1"])
    n = claim_item("n", "喜欢咖啡", ["e2"])
    resolved = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
    return re6.render_evidence(resolved, items=[s, n], seeds=[s])


def render(snapshot=None, **kw):
    base = dict(identity=identity(), evidence=evidence_two_scenes())
    base.update(kw)
    return ws.render_injection(snapshot if snapshot is not None else snap(), **base)


def matrix_report(status="pass", kernels=s06.KERNELS, conditions=s06.CONDITIONS):
    """合成 S06 报告（形状与 run_matrix 输出一致）；端到端用例用真报告。"""
    cases = [{"case_id": "%s__%s" % (c, k), "condition": c, "kernel": k,
              "status": "pass", "expected": {"ok": True}, "observed": {"ok": True},
              "mismatches": [], "error": ""}
             for c in conditions for k in kernels]
    return {"status": status, "cases": cases, "expression_control": False,
            "counts_for_live": False, "trajectory": "historical_replay",
            "provenance": {"algorithm_version": s06.ALGORITHM_VERSION,
                           "method": "isolated_fixture_frozen_replay",
                           "cases": len(cases), "passed": len(cases),
                           "failed": 0, "errored": 0,
                           "conditions": sorted(conditions),
                           "kernels": sorted(kernels),
                           "llm_reinterpretation": False, "network": False,
                           "production_ledger": False},
            "limitations": list(s06.LIMITATIONS)}


def make_plan(snapshot=None, injection=None, continuity=_DEFAULT):
    base = snapshot if snapshot is not None else snap()
    rendered = injection if injection is not None else render(base)
    report = matrix_report() if continuity is _DEFAULT else continuity
    return xp.build_plan(base, rendered, continuity=report)


# ── 矩阵端到端夹具（参照 tests/test_emotion_replay_matrix.py）─────────


def msg(eid, text, ts, addressed=True, **extra):
    return {'id': eid, 'kind': 'message', 'ts': ts, 'source': 'onebot',
            'payload': {'text': text, 'addressed_to_self': addressed, **extra}}


def tool(eid, ts, ok):
    return {'id': eid, 'kind': 'tool_result', 'ts': ts, 'source': 'onebot',
            'payload': {'ok': ok}}


def standard_events():
    return [msg('p1', PRAISE, 1000), msg('n1', '今天天气不错', 2000, addressed=False),
            msg('r1', REBUKE, 3000), tool('t1', 4000, False)]


def late_events():
    a = msg('A', PRAISE, 2000)
    a['effective_ts'], a['source_seq'] = 2000, 0
    c = msg('C', '哦', 500, addressed=False)
    c['effective_ts'], c['source_seq'] = 2500, 1
    b = msg('B', '嗯', 1000, addressed=False)
    b['effective_ts'], b['source_seq'] = 3000, 2
    return [b, c, a]


def simultaneous_events():
    s0 = msg('s0', PRAISE, 2000)
    s0['effective_ts'], s0['source_seq'] = 2000, 1
    s1 = msg('s1', PRAISE, 2000)
    s1['effective_ts'], s1['source_seq'] = 2000, 0
    s2 = msg('s2', '嗨', 2000, addressed=False)
    s2['effective_ts'], s2['source_seq'] = 2000, 2
    return [s0, s1, s2]


LATE_EXPECTED = {
    'timeline_order': ['A', 'C', 'B'], 'state_equals_chronological': True,
    'late_lands_at_effective_ts': True, 'late_not_at_arrival_ts': True,
    'timeline_non_decreasing': True, 'rerun_identical': True,
    'input_order_independent': True, 'checks_ok': True, 'impulse_error': 0.0,
    'expression_control': False, 'counts_for_live': False,
}
RESTART_EXPECTED = {
    'checkpoint_stores_events_not_state': True, 'terminal_bit_identical': True,
    'timeline_bit_identical': True, 'revision_id_identical': True,
    'rerun_idempotent': True, 'checks_ok': True,
    'revision_terminal_bit_identical': True, 'revision_id_identical_after_restart': True,
    'revision_checks_ok': True, 'expression_control': False, 'counts_for_live': False,
}
RETRACT_EXPECTED = {
    'state_equals_never_existed': True, 'impulse_error': 0.0, 'checks_ok': True,
    'retracted_rule': 'retracted_source', 'retracted_zero_delta': True,
    'retracted_not_consumed': True, 'original_event_archived': True,
    'wash_back_rejected': True, 'parent_state_preserved': True,
    'expression_control': False, 'counts_for_live': False,
}


def sim_expected(order, processed):
    return {'timeline_order': order, 'matches_frozen_key': True, 'matches_expected': True,
            'processed': processed, 'rerun_identical': True, 'checks_ok': True,
            'expression_control': False, 'counts_for_live': False}


def matrix_fixture():
    return {
        'late': {'events': late_events(), 'late_id': 'B', 'expected': dict(LATE_EXPECTED)},
        'restart': {'events': standard_events(), 'retract_id': 'p1',
                    'expected': dict(RESTART_EXPECTED)},
        'simultaneous': {'events': simultaneous_events(), 'expected_order': ['s1', 's0', 's2'],
                         'expected': sim_expected(['s1', 's0', 's2'], 3)},
        'retraction': {'events': standard_events(), 'target_id': 'p1',
                       'expected': dict(RETRACT_EXPECTED)},
    }


# ── AST 静态锁：复用冻结前置，零写入，无墙钟，不接 bridge ──────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "expression_plan.py"), encoding="utf-8") as handle:
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

    def attrs(self):
        return {(n.value.id, n.attr) for n in ast.walk(self.tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}

    def imported(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        return names

    def test_import_surface_is_frozen_predecessors_plus_stdlib(self):
        allowed = {"copy", "json", "math", "work_state", "emotion_replay_matrix",
                   "emotion_dynamics", "memory_manifest"}
        self.assertEqual(allowed, self.imported(),
                         "import 面变了：多出来的要解释，缺少的说明没复用前置")

    def test_it_reuses_c04_s06_a03_and_kernel_constants(self):
        pairs = self.attr_pairs()
        self.assertIn(("mm", "digest"), pairs, "plan_id 必须复用 A03 digest 口径")
        attrs = self.attrs()
        for wanted in (("ws", "ALGORITHM_VERSION"), ("ws", "LAYERS"),
                       ("s06", "CONDITIONS"), ("s06", "KERNELS"), ("ed", "DIMENSIONS")):
            self.assertIn(wanted, attrs, "必须复用冻结前置 %s，不另长第二套" % (wanted,))

    def test_no_banned_imports(self):
        banned = {"os", "time", "random", "datetime", "sqlite3", "pathlib", "socket",
                  "urllib", "requests", "http", "qq_openclaw_bridge", "memory_depth",
                  "persona_growth", "self_model", "llm_router", "emotion_shadow",
                  "history_log", "napcat_channel", "web_read", "bili",
                  "retrieval_objects", "recall_scoring", "association_edges",
                  "conversation_plan", "expression_review"}
        self.assertEqual(set(), self.imported() & banned,
                         "C05 是隔离纯契约，不该 import %s" % sorted(self.imported() & banned))

    def test_no_writer_or_db_calls(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "executemany", "commit", "connect",
                     "open", "save", "add", "record_event", "record_action",
                     "write", "writelines", "dump", "touch", "write_text", "write_bytes",
                     "rollback", "close"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), forbidden,
                             "只读装配里出现写操作/开库 %s" % self.call_name(call))

    def test_no_upstream_entry_calls(self):
        """输入由调用方传入：不自己建快照/渲染注入/跑矩阵/推进动力学/落账。"""
        wanted = {"build_snapshot", "render_injection", "run_matrix", "run_condition_matrix",
                  "late_arrival", "restart", "simultaneous", "retraction", "case", "coverage",
                  "advance", "apply_event", "appraise", "integrated_appraisal",
                  "render_evidence", "resolve_ripple", "retrieve_evidence", "recall",
                  "propagate", "correction", "read_invalidation_state", "commit"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), wanted,
                             "C05 不该调用上游入口 %s" % self.call_name(call))

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic", "strftime",
                                  "localtime", "gmtime", "perf_counter"),
                                 "C05 决策路径读了时钟/随机 %s" % node.func.attr)
        for token in ("time.time", "fromtimestamp", "isoformat"):
            self.assertNotIn(token, self.source, "出现了墙钟取时 %s" % token)

    def test_no_sql_db_names_or_data_dir(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE ", "PRAGMA"):
                    self.assertNotIn(verb, upper, "C05 里出现 SQL：%s" % node.value[:60])
        for token in ("cognition.db", "emotion_shadow.db", "DATA_DIR", "sqlite"):
            self.assertNotIn(token, self.source, "纯输入契约不该碰库名/路径变量 %s" % token)

    def test_no_path_resolution_or_freezing(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists"),
                             "C05 自身解析路径了；输入全部由调用方传入")
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_build_plan_takes_no_now_parameter(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "build_plan")
        names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
        self.assertEqual(["snapshot", "injection"], [a.arg for a in fn.args.args])
        self.assertNotIn("now", names, "now 只来自注入产物，build_plan 不接收 now")
        kw = [a.arg for a in fn.args.kwonlyargs]
        self.assertEqual(["continuity"], kw)
        default = fn.args.kw_defaults[kw.index("continuity")]
        self.assertIsInstance(default, ast.Constant)
        self.assertIsNone(default.value, "continuity 默认 None（缺席即锁语气）")

    def test_expression_control_is_a_false_constant_only(self):
        self.assertIn("'expression_control': False", self.source)
        self.assertNotIn("'expression_control': True", self.source)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "expression_control":
                        self.assertIsInstance(value, ast.Constant,
                                              "expression_control 不许用动态值")
                        self.assertIs(False, value.value,
                                      "expression_control 只能以 False 常量出现")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self.assertFalse(isinstance(target, ast.Name)
                                     and target.id == "expression_control",
                                     "expression_control 不许被赋值开启")

    def test_no_second_action_timing_engine(self):
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("should_act", "decide", "arbitrate", "schedule",
                             "when_to", "act_now", "appraise", "score", "rank"):
                    self.assertNotIn(word, node.name.lower(),
                                     "长出了第二套行动/打分判断 %s" % node.name)
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        self.assertNotIn(key.value, ("act", "wait", "rejected"),
                                         "计划不裁决行动，出现行动裁决键 %s" % key.value)

    def test_no_date_based_caching_or_moods(self):
        for token in ("strftime", "%Y-%m-%d", '"day"', "'day'", "_MOODS"):
            self.assertNotIn(token, self.source, "出现了按日期缓存/随机换档口径 %s" % token)

    def test_the_contract_constants(self):
        self.assertEqual("c05-1", xp.ALGORITHM_VERSION)
        for code in ("snapshot_shape_invalid", "injection_shape_invalid", "snapshot_mismatch",
                     "continuity_absent", "continuity_invalid", "continuity_failed",
                     "continuity_kernel_uncovered", "emotion_absent"):
            self.assertIn(code, xp.REASON_CODES)
        self.assertIsInstance(xp.REASON_CODES, tuple)
        self.assertEqual(("low", "mid", "high"), xp.TONE_BANDS)
        self.assertEqual(("valence", "arousal", "control"), xp.TONE_DIMENSIONS)
        self.assertEqual(set(xp.TONE_DIMENSIONS) | {"attention"}, set(ed.DIMENSIONS))
        self.assertNotIn("attention", xp.TONE_DIMENSIONS)
        self.assertEqual("shadow-v2", xp.REQUIRED_KERNEL)
        self.assertIn(xp.REQUIRED_KERNEL, s06.KERNELS)
        for dim, (low, high) in xp.TONE_THRESHOLDS.items():
            bound_low, bound_high = ed.PARAMETERS["bounds"][dim]
            self.assertLessEqual(bound_low, low, "%s 低阈越出冻结界" % dim)
            self.assertLess(low, high, "%s 阈值倒挂" % dim)
            self.assertLessEqual(high, bound_high, "%s 高阈越出冻结界" % dim)

    def test_limitations_are_honest(self):
        self.assertIsInstance(xp.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(xp.LIMITATIONS), 8)
        text = " ".join(xp.LIMITATIONS)
        for undone in ("C06", "qq_openclaw_bridge", "表达接管", "LLM", "话术",
                       "未校准", "统一仲裁", "生产", "墙钟", "随机", "日期"):
            self.assertIn(undone, text, "limitations 必须如实声明 %s" % undone)


# ── 行为锁：形状拒绝（只认 C04 真实产物，不部分信任）─────────────────


class ShapeTests(unittest.TestCase):
    def test_real_c04_outputs_are_accepted(self):
        plan = make_plan()
        self.assertEqual("ok", plan["status"])
        self.assertTrue(plan["plan_id"].startswith("xp_"))

    def test_bad_snapshot_is_refused_loudly(self):
        good = snap()
        variants = [None, {}, [], "snapshot", 42]
        for key in ("snapshot_id", "state_version", "algorithm_version", "emotion",
                    "attention", "excluded", "degradation", "provenance", "now"):
            broken = dict(good)
            del broken[key]
            variants.append(broken)
        variants.append(dict(good, algorithm_version="c04-999"))
        variants.append(dict(good, snapshot_id="forged"))
        variants.append(dict(good, state_version=""))
        variants.append(dict(good, epoch=-1))
        variants.append(dict(good, status="bogus"))
        variants.append(dict(good, now="yesterday"))
        for bad in variants:
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(ValueError):
                    xp.build_plan(bad, render(good), continuity=matrix_report())

    def test_snapshot_emotion_must_be_the_four_dim_projection(self):
        good = snap()
        for dim in ed.DIMENSIONS:
            broken = copy.deepcopy(good)
            del broken["emotion"][dim]
            with self.subTest(dim=dim):
                with self.assertRaises(ValueError):
                    xp.build_plan(broken, render(good), continuity=matrix_report())
        broken = copy.deepcopy(good)
        broken["emotion"]["valence"] = "x"
        with self.assertRaises(ValueError):
            xp.build_plan(broken, render(good), continuity=matrix_report())

    def test_bad_injection_is_refused_loudly(self):
        good = render()
        variants = [None, {}, [], "injection"]
        for key in ("snapshot_id", "algorithm_version", "identity", "state",
                    "evidence", "excluded", "degradation", "cache", "now"):
            broken = dict(good)
            del broken[key]
            variants.append(broken)
        variants.append(dict(good, algorithm_version="c04-999"))
        variants.append(dict(good, status="bogus"))
        for layer, key in (("identity", "text"), ("identity", "cache_hit"),
                           ("state", "state_version"), ("state", "chars"),
                           ("evidence", "scenes"), ("evidence", "counts")):
            broken = copy.deepcopy(good)
            del broken[layer][key]
            variants.append(broken)
        broken = copy.deepcopy(good)
        broken["identity"]["text"] = 123
        variants.append(broken)
        for bad in variants:
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(ValueError):
                    xp.build_plan(snap(), bad, continuity=matrix_report())

    def test_plan_shape_is_the_contract_one(self):
        plan = make_plan()
        self.assertEqual({"plan_id", "algorithm_version", "snapshot_id", "state_version",
                          "now", "epoch", "scope", "status", "layers", "tone",
                          "expression_control", "excluded", "degradation",
                          "provenance", "limitations"}, set(plan))
        self.assertEqual("c05-1", plan["algorithm_version"])
        self.assertIs(False, plan["expression_control"])
        self.assertEqual(list(xp.LIMITATIONS), plan["limitations"])
        self.assertEqual(list(xp.LIMITATIONS), plan["provenance"]["limitations"])
        self.assertEqual(ws.ALGORITHM_VERSION,
                         plan["provenance"]["work_state_algorithm_version"])
        for key in ("act", "wait", "rejected"):
            self.assertNotIn(key, plan, "计划不裁决行动")

    def test_plan_carries_the_injection_moment_not_a_clock(self):
        shared = snap()
        plan = make_plan(shared, render(shared, epoch=1, scope="group:g1"))
        self.assertEqual(shared["now"], plan["now"])
        self.assertEqual(1, plan["epoch"])
        self.assertEqual("group:g1", plan["scope"])


# ── 行为锁：快照绑定（绝不装配别的快照的注入）──────────────────────


class BindingTests(unittest.TestCase):
    def test_mismatched_snapshot_id_is_refused_loudly(self):
        injection = render()
        other = snap(commitments=[commitment(text="别的快照的承诺")])
        self.assertNotEqual(other["snapshot_id"], injection["snapshot_id"])
        with self.assertRaisesRegex(ValueError, "snapshot_mismatch"):
            xp.build_plan(other, injection, continuity=matrix_report())

    def test_state_layer_binding_is_checked(self):
        injection = render()
        forged = copy.deepcopy(injection)
        forged["state"]["snapshot_id"] = "ws_forged"
        with self.assertRaisesRegex(ValueError, "snapshot_mismatch"):
            xp.build_plan(snap(), forged, continuity=matrix_report())
        forged = copy.deepcopy(injection)
        forged["state"]["state_version"] = "forged_version"
        with self.assertRaisesRegex(ValueError, "snapshot_mismatch"):
            xp.build_plan(snap(), forged, continuity=matrix_report())

    def test_now_binding_is_checked(self):
        injection = render()
        forged = copy.deepcopy(injection)
        forged["now"] = injection["now"] + 1
        with self.assertRaisesRegex(ValueError, "snapshot_mismatch"):
            xp.build_plan(snap(), forged, continuity=matrix_report())

    def test_matching_pair_passes_and_is_bound(self):
        shared = snap()
        plan = make_plan(shared, render(shared))
        self.assertEqual(shared["snapshot_id"], plan["snapshot_id"])
        self.assertEqual(shared["state_version"], plan["state_version"])
        self.assertEqual(shared["state_version"], plan["layers"]["state"]["state_version"])


# ── 行为锁：三层保真（只搬 C04 已渲染内容，不改写不补造）─────────────


class LayerTests(unittest.TestCase):
    def test_layers_follow_the_frozen_order(self):
        plan = make_plan()
        self.assertEqual(ws.LAYERS, tuple(plan["layers"]))
        self.assertEqual(("identity", "state", "evidence"), tuple(plan["layers"]))

    def test_layers_are_verbatim_c04_renderings(self):
        shared = snap()
        injection = render(shared)
        plan = make_plan(shared, injection)
        for layer in ws.LAYERS:
            self.assertEqual(injection[layer], plan["layers"][layer],
                             "%s 层必须逐字搬运，不改写不重渲染" % layer)

    def test_identity_text_and_flags_are_carried(self):
        injection = render()
        plan = make_plan(injection=injection)
        got = plan["layers"]["identity"]
        self.assertEqual(identity()["text"], got["text"])
        self.assertTrue(got["cacheable"])
        self.assertFalse(got["cache_hit"])
        self.assertEqual(len(got["text"]), got["chars"])
        self.assertFalse(got["truncated"])

    def test_truncated_identity_stays_truncated_not_repaired(self):
        injection = render(identity=identity(text="长" * 600))
        plan = make_plan(injection=injection)
        got = plan["layers"]["identity"]
        self.assertTrue(got["truncated"])
        self.assertEqual(injection["identity"]["text"], got["text"])
        self.assertLessEqual(len(got["text"]), ws.MAX_IDENTITY_CHARS + 1)

    def test_state_text_carries_commitment_verbatim(self):
        injection = render()
        plan = make_plan(injection=injection)
        got = plan["layers"]["state"]
        self.assertIn("帮查荒坂塔资料", got["text"])
        self.assertEqual(injection["state"]["text"], got["text"])
        self.assertEqual(NOW, got["now"])
        self.assertFalse(got["truncated"])

    def test_evidence_scenes_and_verdict_refs_are_verbatim(self):
        injection = render()
        plan = make_plan(injection=injection)
        got = plan["layers"]["evidence"]
        self.assertEqual(injection["evidence"]["scenes"], got["scenes"])
        self.assertEqual(2, len(got["scenes"]))
        for scene in got["scenes"]:
            for entry in scene["entries"]:
                self.assertIsInstance(entry["verdict_refs"], list)
                self.assertTrue(entry["verdict_refs"])

    def test_absent_identity_stays_empty_not_fabricated(self):
        injection = render(identity=None)
        plan = make_plan(injection=injection)
        self.assertEqual("", plan["layers"]["identity"]["text"])
        self.assertEqual(injection["identity"], plan["layers"]["identity"])

    def test_excluded_and_degradation_pass_through(self):
        shared = snap()
        injection = render(shared, identity=None,
                           states={"cmt_1": {"status": "invalidated"}})
        plan = make_plan(shared, injection)
        self.assertEqual(injection["excluded"], plan["excluded"])
        for entry in injection["degradation"]:
            self.assertIn(entry, plan["degradation"])
        self.assertIn({"object_id": "cmt_1", "layer": "commitment",
                       "reason_code": "invalidated"}, plan["excluded"])

    def test_degraded_injection_never_becomes_ok(self):
        injection = render(identity=None)
        self.assertEqual("degraded", injection["status"])
        plan = make_plan(injection=injection)
        self.assertEqual("degraded", plan["status"], "degraded 透传，不冒充 ok")
        self.assertEqual("degraded", plan["provenance"]["injection_status"])

    def test_layers_are_deep_copies(self):
        shared = snap()
        injection = render(shared)
        before = canonical(injection)
        plan = make_plan(shared, injection)
        plan["layers"]["state"]["text"] = "mutated"
        plan["layers"]["evidence"]["scenes"].clear()
        plan["layers"]["identity"]["cache_hit"] = True
        self.assertEqual(before, canonical(injection))


# ── 行为锁：tone 门（五种不可用路径 + 全通过路径）───────────────────


class ToneGateTests(unittest.TestCase):
    def assert_tone_locked(self, plan, reason, matrix_status=""):
        tone = plan["tone"]
        self.assertIs(False, tone["available"])
        self.assertEqual(reason, tone["reason"])
        self.assertIsNone(tone["descriptors"])
        self.assertIsNone(tone["values"])
        self.assertIs(False, tone["calibrated"])
        self.assertEqual("emotion_kernel", tone["basis"])
        self.assertEqual("degraded", plan["status"])
        self.assertIn({"store": "tone", "reason": reason,
                       "matrix_status": matrix_status}, plan["degradation"])
        verdict = plan["provenance"]["continuity"]
        self.assertIs(False, verdict["available"])
        self.assertEqual(reason, verdict["reason"])
        self.assertEqual(matrix_status, verdict["matrix_status"])
        # 三层注入文本仍装配（它们已过 M05 门）。
        self.assertTrue(plan["layers"]["state"]["text"])
        self.assertEqual(ws.LAYERS, tuple(plan["layers"]))

    def test_absent_continuity_locks_tone(self):
        self.assert_tone_locked(make_plan(continuity=None), "continuity_absent")

    def test_invalid_continuity_shape_locks_tone(self):
        good = matrix_report()
        variants = [[], "pass", 42, {}]
        for key in ("status", "cases", "provenance"):
            broken = dict(good)
            del broken[key]
            variants.append(broken)
        variants.append(dict(good, status=123))
        variants.append(dict(good, cases={}))
        broken = copy.deepcopy(good)
        broken["provenance"]["kernels"] = "shadow-v2"
        variants.append(broken)
        broken = copy.deepcopy(good)
        broken["provenance"]["kernels"] = ["shadow-v9"]
        variants.append(broken)
        broken = copy.deepcopy(good)
        broken["provenance"]["conditions"] = ["bogus_condition"]
        variants.append(broken)
        for bad in variants:
            with self.subTest(bad=str(bad)[:60]):
                self.assert_tone_locked(make_plan(continuity=bad), "continuity_invalid")

    def test_failed_matrix_locks_tone_and_registers_status(self):
        for status in ("fail", "error"):
            with self.subTest(status=status):
                plan = make_plan(continuity=matrix_report(status=status))
                self.assert_tone_locked(plan, "continuity_failed", matrix_status=status)

    def test_uncovered_kernel_locks_tone(self):
        plan = make_plan(continuity=matrix_report(kernels=("shadow-v1",)))
        self.assert_tone_locked(plan, "continuity_kernel_uncovered", matrix_status="pass")
        self.assertEqual(["shadow-v1"], plan["provenance"]["continuity"]["kernels"])

    def test_absent_emotion_locks_tone(self):
        shared = snap(emotion=None)
        plan = make_plan(shared, render(shared))
        self.assert_tone_locked(plan, "emotion_absent", matrix_status="pass")

    def test_gate_order_is_the_contract_order(self):
        shared = snap(emotion=None)
        injection = render(shared)
        plan = xp.build_plan(shared, injection, continuity=None)
        self.assertEqual("continuity_absent", plan["tone"]["reason"])
        plan = xp.build_plan(shared, injection, continuity=matrix_report(status="fail"))
        self.assertEqual("continuity_failed", plan["tone"]["reason"])

    def test_all_gates_pass_unlocks_tone(self):
        shared = snap()
        plan = make_plan(shared, render(shared))
        tone = plan["tone"]
        self.assertIs(True, tone["available"])
        self.assertEqual("", tone["reason"])
        self.assertIs(False, tone["calibrated"])
        self.assertEqual("emotion_kernel", tone["basis"])
        self.assertEqual(set(xp.TONE_DIMENSIONS), set(tone["descriptors"]))
        self.assertEqual({dim: shared["emotion"][dim] for dim in xp.TONE_DIMENSIONS},
                         tone["values"])
        self.assertEqual("ok", plan["status"])
        self.assertNotIn("tone", [d["store"] for d in plan["degradation"]])
        verdict = plan["provenance"]["continuity"]
        self.assertIs(True, verdict["available"])
        self.assertEqual("pass", verdict["matrix_status"])
        self.assertEqual(sorted(s06.KERNELS), verdict["kernels"])
        self.assertEqual(sorted(s06.CONDITIONS), verdict["conditions"])
        self.assertEqual(8, verdict["case_count"])

    def test_unavailable_tone_never_fabricates_descriptors(self):
        for continuity in (None, matrix_report(status="fail"),
                           matrix_report(kernels=("shadow-v1",))):
            plan = make_plan(continuity=continuity)
            self.assertIsNone(plan["tone"]["descriptors"])
            self.assertIsNone(plan["tone"]["values"])

    def test_provenance_carries_required_kernel_and_thresholds(self):
        plan = make_plan()
        self.assertEqual("shadow-v2", plan["provenance"]["required_kernel"])
        self.assertEqual(xp.TONE_THRESHOLDS, plan["provenance"]["tone_thresholds"])


# ── 行为锁：tone 阈值（冻结闭边界分档，attention 不进 tone）───────────


class ToneThresholdTests(unittest.TestCase):
    def tone_for(self, **over):
        shared = snap(emotion=emotion(**over))
        return make_plan(shared, render(shared))["tone"]

    def test_valence_boundaries(self):
        for value, band in ((-1.0, "low"), (-0.25, "low"), (-0.249999, "mid"),
                            (0.0, "mid"), (0.249999, "mid"), (0.25, "high"),
                            (1.0, "high")):
            with self.subTest(value=value):
                self.assertEqual(band, self.tone_for(valence=value)["descriptors"]["valence"])

    def test_arousal_boundaries(self):
        for value, band in ((0.0, "low"), (0.25, "low"), (0.250001, "mid"),
                            (0.5, "mid"), (0.749999, "mid"), (0.75, "high"),
                            (1.0, "high")):
            with self.subTest(value=value):
                self.assertEqual(band, self.tone_for(arousal=value)["descriptors"]["arousal"])

    def test_control_boundaries(self):
        for value, band in ((0.0, "low"), (0.25, "low"), (0.250001, "mid"),
                            (0.5, "mid"), (0.749999, "mid"), (0.75, "high"),
                            (1.0, "high")):
            with self.subTest(value=value):
                self.assertEqual(band, self.tone_for(control=value)["descriptors"]["control"])

    def test_values_are_the_clipped_c04_projection(self):
        tone = self.tone_for(valence=5.0, arousal=-2.0, control=9.0)
        self.assertEqual({"valence": 1.0, "arousal": 0.0, "control": 1.0}, tone["values"])
        self.assertEqual({"valence": "high", "arousal": "low", "control": "high"},
                         tone["descriptors"])

    def test_attention_is_load_not_tone(self):
        low = self.tone_for(attention=0.0)
        high = self.tone_for(attention=0.95)
        self.assertEqual(low, high, "attention 是负荷，不进 tone")
        plan_low = make_plan(snap(emotion=emotion(attention=0.0)))
        plan_high = make_plan(snap(emotion=emotion(attention=0.95)))
        self.assertNotEqual(plan_low["plan_id"], plan_high["plan_id"],
                            "负荷变化仍须改变状态版本与计划 ID")

    def test_default_kernel_state_bands(self):
        tone = self.tone_for()
        self.assertEqual({"valence": "mid", "arousal": "low", "control": "mid"},
                         tone["descriptors"])

    def test_bands_stay_bounded_across_the_whole_range(self):
        for dim in xp.TONE_DIMENSIONS:
            low, high = ed.PARAMETERS["bounds"][dim]
            for step in range(21):
                value = low + (high - low) * step / 20.0
                tone = self.tone_for(**{dim: value})
                self.assertIn(tone["descriptors"][dim], xp.TONE_BANDS)

    def test_calibrated_is_always_false(self):
        self.assertIs(False, self.tone_for()["calibrated"])
        self.assertIs(False, make_plan(continuity=None)["tone"]["calibrated"])


# ── 行为锁：plan_id 内容寻址 / 确定性 / 不改输入 ────────────────────


class PlanIdTests(unittest.TestCase):
    def test_plan_id_is_content_addressed(self):
        plan = make_plan()
        core = {"algorithm_version": plan["algorithm_version"],
                "snapshot_id": plan["snapshot_id"],
                "state_version": plan["state_version"],
                "now": plan["now"], "epoch": plan["epoch"], "scope": plan["scope"],
                "layers": plan["layers"], "tone": plan["tone"],
                "continuity": plan["provenance"]["continuity"]}
        self.assertEqual("xp_" + mm.digest(canonical(core))[:14], plan["plan_id"])
        self.assertEqual(17, len(plan["plan_id"]))

    def test_same_input_gives_the_same_plan(self):
        self.assertEqual(canonical(make_plan()), canonical(make_plan()))

    def test_dict_key_order_does_not_change_the_plan(self):
        shared = snap()
        injection = render(shared)
        report = matrix_report()
        forward = xp.build_plan(shared, injection, continuity=report)
        backward = xp.build_plan(reordered(shared), reordered(injection),
                                 continuity=reordered(report))
        self.assertEqual(forward["plan_id"], backward["plan_id"])
        self.assertEqual(canonical(forward), canonical(backward))

    def test_inputs_are_not_mutated(self):
        shared = snap()
        injection = render(shared)
        report = matrix_report()
        before = (canonical(shared), canonical(injection), canonical(report))
        xp.build_plan(shared, injection, continuity=report)
        self.assertEqual(before, (canonical(shared), canonical(injection), canonical(report)))

    def test_output_is_a_deep_copy(self):
        shared = snap()
        injection = render(shared)
        report = matrix_report()
        plan = xp.build_plan(shared, injection, continuity=report)
        plan["layers"]["state"]["text"] = "mutated"
        plan["tone"]["descriptors"]["valence"] = "mutated"
        plan["degradation"].append({"store": "mutated"})
        plan["provenance"]["continuity"]["kernels"].clear()
        again = xp.build_plan(shared, injection, continuity=report)
        self.assertEqual("xp_" + mm.digest(canonical({
            "algorithm_version": again["algorithm_version"],
            "snapshot_id": again["snapshot_id"], "state_version": again["state_version"],
            "now": again["now"], "epoch": again["epoch"], "scope": again["scope"],
            "layers": again["layers"], "tone": again["tone"],
            "continuity": again["provenance"]["continuity"]}))[:14], again["plan_id"])
        self.assertEqual(injection["state"]["text"], again["layers"]["state"]["text"])

    def test_state_version_change_changes_plan_id_immediately(self):
        first = make_plan()
        second = make_plan(snap(commitments=[commitment(text="周五前把图发给他")]))
        self.assertNotEqual(first["state_version"], second["state_version"])
        self.assertNotEqual(first["plan_id"], second["plan_id"],
                            "state_version 变 → plan_id 立即变，不按日期缓存")

    def test_same_state_version_different_now_changes_plan_id(self):
        rows = dict(commitments=[], goals=[], pending=[], relations=[], events=[],
                    emotion=emotion(at=123.0), budget={"limit": 5, "used": 2},
                    scope="private:1001")
        first = snap(now=NOW, **rows)
        second = snap(now=NOW + 1000, **rows)
        self.assertEqual(first["state_version"], second["state_version"])
        plan_first = make_plan(first, render(first))
        plan_second = make_plan(second, render(second))
        self.assertNotEqual(plan_first["plan_id"], plan_second["plan_id"])

    def test_continuity_verdict_changes_plan_id(self):
        shared = snap()
        injection = render(shared)
        locked = xp.build_plan(shared, injection, continuity=None)
        unlocked = xp.build_plan(shared, injection, continuity=matrix_report())
        self.assertNotEqual(locked["plan_id"], unlocked["plan_id"])

    def test_tone_change_changes_plan_id(self):
        first = make_plan(snap(emotion=emotion(valence=0.8)))
        second = make_plan(snap(emotion=emotion(valence=-0.8)))
        self.assertNotEqual(first["plan_id"], second["plan_id"])


# ── 端到端：真实 s06.run_condition_matrix 报告经真实 C04 链路 ────────


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="c05_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def kernel_emotion(self):
        """情绪来自真实冻结内核推进（不是手填数值），再交给 C04 投影。"""
        state = ed.initial_state(START)
        for event in standard_events():
            state = ed.apply_event(state, event)
        return ed.advance(state, END)

    def e2e_snapshot(self):
        return ws.build_snapshot(
            now=END, emotion=self.kernel_emotion(),
            commitments=[commitment(due_ts=END + 3600)], goals=[goal()],
            pending=[waiting(requested_at=END - 120)], relations=[relation()],
            budget={"limit": 5, "used": 2},
            events=[{"id": "evt_1", "kind": "group_message", "ts": END - 60}],
            scope="private:1001")

    def test_real_matrix_pass_unlocks_tone(self):
        report = s06.run_condition_matrix(matrix_fixture(), start_at=START, end_at=END, ts=5.0)
        self.assertEqual("pass", report["status"], canonical(report["cases"]))
        shared = self.e2e_snapshot()
        injection = render(shared)
        plan = xp.build_plan(shared, injection, continuity=report)
        self.assertEqual("ok", plan["status"])
        self.assertIs(True, plan["tone"]["available"])
        self.assertIs(False, plan["tone"]["calibrated"])
        self.assertEqual("emotion_kernel", plan["tone"]["basis"])
        self.assertEqual({dim: shared["emotion"][dim] for dim in xp.TONE_DIMENSIONS},
                         plan["tone"]["values"])
        for band in plan["tone"]["descriptors"].values():
            self.assertIn(band, xp.TONE_BANDS)
        self.assertIs(False, plan["expression_control"])
        self.assertEqual(sorted(s06.KERNELS), plan["provenance"]["continuity"]["kernels"])
        self.assertEqual(sorted(s06.CONDITIONS), plan["provenance"]["continuity"]["conditions"])
        self.assertEqual(8, plan["provenance"]["continuity"]["case_count"])
        for layer in ws.LAYERS:
            self.assertEqual(injection[layer], plan["layers"][layer])

    def test_real_matrix_is_deterministic_through_the_plan(self):
        shared = self.e2e_snapshot()
        injection = render(shared)
        first = s06.run_condition_matrix(matrix_fixture(), start_at=START, end_at=END, ts=5.0)
        second = s06.run_condition_matrix(matrix_fixture(), start_at=START, end_at=END, ts=5.0)
        self.assertEqual(xp.build_plan(shared, injection, continuity=first)["plan_id"],
                         xp.build_plan(shared, injection, continuity=second)["plan_id"])

    def test_real_matrix_fail_locks_tone(self):
        fixture = matrix_fixture()
        fixture["simultaneous"]["expected"]["processed"] = 99
        report = s06.run_condition_matrix(fixture, start_at=START, end_at=END, ts=5.0)
        self.assertEqual("fail", report["status"])
        shared = self.e2e_snapshot()
        injection = render(shared)
        plan = xp.build_plan(shared, injection, continuity=report)
        self.assertEqual("degraded", plan["status"])
        self.assertIs(False, plan["tone"]["available"])
        self.assertEqual("continuity_failed", plan["tone"]["reason"])
        self.assertEqual("fail", plan["provenance"]["continuity"]["matrix_status"])
        self.assertIn({"store": "tone", "reason": "continuity_failed",
                       "matrix_status": "fail"}, plan["degradation"])
        self.assertEqual(injection["state"]["text"], plan["layers"]["state"]["text"],
                         "矩阵失败只锁语气，三层注入文本仍装配")

    def test_real_v1_only_matrix_does_not_unlock_v2_tone(self):
        report = s06.run_condition_matrix(matrix_fixture(), start_at=START, end_at=END,
                                          kernels=("shadow-v1",), ts=5.0)
        self.assertEqual("pass", report["status"])
        shared = self.e2e_snapshot()
        plan = xp.build_plan(shared, render(shared), continuity=report)
        self.assertEqual("degraded", plan["status"])
        self.assertEqual("continuity_kernel_uncovered", plan["tone"]["reason"])
        self.assertIsNone(plan["tone"]["descriptors"])

    def test_pure_contract_creates_nothing_in_an_empty_data_dir(self):
        before = snapshot_tree(self.root)
        report = s06.run_condition_matrix(matrix_fixture(), start_at=START, end_at=END, ts=5.0)
        shared = self.e2e_snapshot()
        plan = xp.build_plan(shared, render(shared), continuity=report)
        self.assertEqual("ok", plan["status"])
        self.assertEqual(before, snapshot_tree(self.root))
        self.assertEqual([], list(Path(self.root).iterdir()), "纯契约顺手建了目录")


if __name__ == "__main__":
    unittest.main()