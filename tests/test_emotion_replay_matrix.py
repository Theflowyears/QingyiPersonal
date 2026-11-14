# -*- coding: utf-8 -*-
"""S06 迟到 × 重启 × 同刻 × 撤回 回放矩阵的回归锁（路线 §4.3 / §9）。

钉死的行为（全部对真实冻结契约做「前 → 施加条件 → 重跑」，不打桩断言）：
  · 迟到：effective_ts 落在到达顺序之外的事件按 effective_ts 着陆（非到达 ts），
    时间线不倒退、输入顺序无关、终态与 chronological 对照逐位一致、重跑幂等；
  · 重启：检查点只存事件+窗口+内核+tick（不存终态），从事件全量重放，终态/
    时间线/内容寻址 ID 逐位一致；撤回修订链可跨重启重建（指数恢复路径依赖）；
  · 同刻：同一 effective_ts 按冻结键 source_seq > input_seq > 输入索引 决序，
    再按 id 稳定排序；同 ID 只取排序后首条；混合同刻 ts-only 事件在前；
  · 撤回：observation_invalid 终态与「事件从未存在」逐位一致、冲击对账 0 误差、
    原事件仍可审计、撤回单调（解释纠正洗不回）、只追加不覆盖；
  · 影子边界：expression_control 恒 False、counts_for_live 由 S01 契约盖章不计在线；
  · 冻结：动力学/恢复/排序全部复用 emotion_core/emotion_revision，无第二套 exp/tau
    或顺序键；矩阵纯编排、确定性、provenance 不含时钟。
AST 锁钉死旁路契约：只 import 冻结原语+stdlib、无写入 SQL/写文件、无顶层冻路径、
不自带恢复公式、不写第二套回放顺序、expression_control 恒 False。
全部用隔离临时 DATA_DIR 与合成事件，不读不写真实 data/，不发网络、不触发 QQ 发送。
"""
import ast
import copy
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import emotion_core as legacy            # noqa: E402
import emotion_dynamics as core          # noqa: E402
import emotion_revision as rev           # noqa: E402
import emotion_replay_matrix as erm      # noqa: E402
import emotion_trajectories as traj      # noqa: E402
import invalidation_dag as dag           # noqa: E402

PRAISE = '谢谢你帮我查清楚了'
REBUKE = '你刚才说错了'
START, END = 900.0, 5000.0
V1, V2 = 'shadow-v1', 'shadow-v2'


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
    """到达顺序 [B,C,A]，有效时间 A(2000)<C(2500)<B(3000)：B/C 迟到。"""
    a = msg('A', PRAISE, 2000)
    a['effective_ts'], a['source_seq'] = 2000, 0
    c = msg('C', '哦', 500, addressed=False)
    c['effective_ts'], c['source_seq'] = 2500, 1
    b = msg('B', '嗯', 1000, addressed=False)
    b['effective_ts'], b['source_seq'] = 3000, 2
    return [b, c, a]


def simultaneous_events():
    """同一 effective_ts=2000，source_seq 决序 → [s1(0), s0(1), s2(2)]。"""
    s0 = msg('s0', PRAISE, 2000)
    s0['effective_ts'], s0['source_seq'] = 2000, 1
    s1 = msg('s1', PRAISE, 2000)
    s1['effective_ts'], s1['source_seq'] = 2000, 0
    s2 = msg('s2', '嗨', 2000, addressed=False)
    s2['effective_ts'], s2['source_seq'] = 2000, 2
    return [s0, s1, s2]


def max_distance(a, b):
    return max(abs(a[k] - b[k]) for k in erm.DIMENSIONS)


# 显式期望分型（与探针确认的冻结行为一致；变了必须显式评审）。
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
RESTART_PLAIN_EXPECTED = {
    'checkpoint_stores_events_not_state': True, 'terminal_bit_identical': True,
    'timeline_bit_identical': True, 'revision_id_identical': True,
    'rerun_idempotent': True, 'checks_ok': True,
    'expression_control': False, 'counts_for_live': False,
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


# 声明的交叉点：每个 (条件, 内核) 都必须有真实用例命中（ZCoverageTests 钉死）。
INTERSECTIONS = {(c, k) for c in erm.CONDITIONS for k in erm.KERNELS}

# 用例登记表：case_id → (条件, 内核)。测试方法名约定为 test_<case_id>。
CASE_PLAN = {
    'late__shadow_v1': ('late', V1),
    'late__shadow_v2': ('late', V2),
    'late_no_backward__shadow_v1': ('late', V1),
    'restart__shadow_v1': ('restart', V1),
    'restart__shadow_v2': ('restart', V2),
    'restart_after_simultaneous__shadow_v2': ('restart', V2),
    'late_restart_idempotent__shadow_v1': ('restart', V1),
    'restart_revision_chain__shadow_v1': ('restart', V1),
    'simultaneous__shadow_v1': ('simultaneous', V1),
    'simultaneous__shadow_v2': ('simultaneous', V2),
    'simultaneous_source_seq__shadow_v1': ('simultaneous', V1),
    'simultaneous_input_seq__shadow_v2': ('simultaneous', V2),
    'simultaneous_index__shadow_v2': ('simultaneous', V2),
    'simultaneous_mixed_ts_only__shadow_v2': ('simultaneous', V2),
    'simultaneous_same_id__shadow_v2': ('simultaneous', V2),
    'retraction__shadow_v1': ('retraction', V1),
    'retraction__shadow_v2': ('retraction', V2),
    'late_then_retract__shadow_v2': ('retraction', V2),
    'simultaneous_then_retract__shadow_v2': ('retraction', V2),
    'retract_all_baseline__shadow_v2': ('retraction', V2),
}


class Base(unittest.TestCase):
    """隔离夹具：每个用例独立临时 DATA_DIR，路径调用时解析，不碰真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s06_', ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._old = os.environ.get('DATA_DIR'), os.environ.get('PERSONA_DATA_DIR')
        os.environ['DATA_DIR'] = self.root
        os.environ.pop('PERSONA_DATA_DIR', None)

    def tearDown(self):
        for key, value in zip(('DATA_DIR', 'PERSONA_DATA_DIR'), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_case(self, case_id, expected, runner):
        condition, kernel = CASE_PLAN[case_id]
        report = erm.run_matrix([erm.case(case_id, condition, kernel, expected, runner)])
        entry = report['cases'][0]
        self.assertEqual('pass', report['status'],
                         '%s 分型未命中: %s' % (case_id, json.dumps(
                             entry['mismatches'] or entry['error'], ensure_ascii=False)))
        return entry['observed']

    def nothing_created(self):
        return not list(Path(self.root).iterdir())


# ── 迟到：effective_ts 着陆、时间线不倒退、输入顺序无关 ──────────────────


class LateArrivalTests(Base):
    def test_late__shadow_v1(self):
        obs = self.run_case('late__shadow_v1', LATE_EXPECTED,
                            lambda: erm.late_arrival({'events': late_events(), 'late_id': 'B'},
                                                     start_at=START, end_at=END, kernel=V1, ts=5.0))
        self.assertEqual(['A', 'C', 'B'], obs['timeline_order'])

    def test_late__shadow_v2(self):
        self.run_case('late__shadow_v2', LATE_EXPECTED,
                      lambda: erm.late_arrival({'events': late_events(), 'late_id': 'B'},
                                               start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_late_no_backward__shadow_v1(self):
        # 迟到事件有效时间早于到达时间，时间线仍按有效时间单调不倒退。
        expected = {'timeline_non_decreasing': True, 'late_lands_at_effective_ts': True,
                    'state_equals_chronological': True, 'checks_ok': True,
                    'expression_control': False, 'counts_for_live': False}
        self.run_case('late_no_backward__shadow_v1', expected,
                      lambda: erm.late_arrival({'events': late_events(), 'late_id': 'C'},
                                               start_at=START, end_at=END, kernel=V1, ts=5.0))


# ── 重启：检查点只存事件，全量重放逐位一致，修订链可跨重启重建 ────────────


class RestartTests(Base):
    def test_restart__shadow_v1(self):
        self.run_case('restart__shadow_v1', RESTART_EXPECTED,
                      lambda: erm.restart({'events': standard_events(), 'retract_id': 'p1'},
                                          start_at=START, end_at=END, kernel=V1, ts=5.0))

    def test_restart__shadow_v2(self):
        self.run_case('restart__shadow_v2', RESTART_EXPECTED,
                      lambda: erm.restart({'events': standard_events(), 'retract_id': 'p1'},
                                          start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_restart_after_simultaneous__shadow_v2(self):
        # 重启 × 同刻：同刻并列事件跨重启仍逐位一致，撤回修订也可重建。
        self.run_case('restart_after_simultaneous__shadow_v2', RESTART_EXPECTED,
                      lambda: erm.restart({'events': simultaneous_events(), 'retract_id': 's1'},
                                          start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_late_restart_idempotent__shadow_v1(self):
        # 重启 × 迟到：迟到事件跨重启重放仍按 effective_ts 着陆，终态逐位一致。
        self.run_case('late_restart_idempotent__shadow_v1', RESTART_PLAIN_EXPECTED,
                      lambda: erm.restart({'events': late_events()},
                                          start_at=START, end_at=END, kernel=V1, ts=5.0))

    def test_restart_revision_chain__shadow_v1(self):
        def runner():
            events = standard_events()
            led = rev.new_ledger()
            base = rev.build_base_run(events, start_at=START, end_at=END, kernel=V1, ts=5.0)
            rev.commit_base_run(led, base)
            rec1 = dag.correction('observation_invalid', 'p1', '撤回1', ts=10.0)
            c1 = rev.commit_revision(led, rev.plan_revision(led, base['revision_id'], rec1, ts=10.0))
            rec2 = dag.correction('observation_invalid', 'r1', '撤回2', ts=11.0)
            c2 = rev.commit_revision(led, rev.plan_revision(led, c1['revision_id'], rec2, ts=11.0))
            # 重启：新账本从事件全量重建整条纠正链。
            fresh = rev.new_ledger()
            rb = rev.build_base_run(events, start_at=START, end_at=END, kernel=V1, ts=5.0)
            rev.commit_base_run(fresh, rb)
            r1b = dag.correction('observation_invalid', 'p1', '撤回1', ts=10.0)
            d1 = rev.commit_revision(fresh, rev.plan_revision(fresh, rb['revision_id'], r1b, ts=10.0))
            r2b = dag.correction('observation_invalid', 'r1', '撤回2', ts=11.0)
            d2 = rev.commit_revision(fresh, rev.plan_revision(fresh, d1['revision_id'], r2b, ts=11.0))
            return {'chain_terminal_identical': d2['state'] == c2['state'],
                    'chain_id_identical': d2['revision_id'] == c2['revision_id'],
                    'lineage_len': len(rev.revision_lineage(fresh, d2['revision_id'])),
                    'checks_ok': d2['checks']['ok'],
                    'expression_control': d2['expression_control'],
                    'counts_for_live': d2['counts_for_live']}
        self.run_case('restart_revision_chain__shadow_v1', {
            'chain_terminal_identical': True, 'chain_id_identical': True, 'lineage_len': 3,
            'checks_ok': True, 'expression_control': False, 'counts_for_live': False}, runner)


# ── 同刻：冻结键 source_seq > input_seq > 索引，同 ID 取首条 ──────────────


class SimultaneousTests(Base):
    def test_simultaneous__shadow_v1(self):
        self.run_case('simultaneous__shadow_v1', sim_expected(['s1', 's0', 's2'], 3),
                      lambda: erm.simultaneous({'events': simultaneous_events(),
                                                'expected_order': ['s1', 's0', 's2']},
                                               start_at=START, end_at=END, kernel=V1, ts=5.0))

    def test_simultaneous__shadow_v2(self):
        self.run_case('simultaneous__shadow_v2', sim_expected(['s1', 's0', 's2'], 3),
                      lambda: erm.simultaneous({'events': simultaneous_events(),
                                                'expected_order': ['s1', 's0', 's2']},
                                               start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_simultaneous_source_seq__shadow_v1(self):
        hi = msg('e_hi', PRAISE, 2000)
        hi['effective_ts'], hi['source_seq'] = 2000, 1
        lo = msg('e_lo', PRAISE, 2000)
        lo['effective_ts'], lo['source_seq'] = 2000, 0
        self.run_case('simultaneous_source_seq__shadow_v1', sim_expected(['e_lo', 'e_hi'], 2),
                      lambda: erm.simultaneous({'events': [hi, lo], 'expected_order': ['e_lo', 'e_hi']},
                                               start_at=START, end_at=END, kernel=V1, ts=5.0))

    def test_simultaneous_input_seq__shadow_v2(self):
        a = msg('a', PRAISE, 2000)
        a['effective_ts'], a['input_seq'] = 2000, 5
        b = msg('b', PRAISE, 2000)
        b['effective_ts'], b['input_seq'] = 2000, 2
        self.run_case('simultaneous_input_seq__shadow_v2', sim_expected(['b', 'a'], 2),
                      lambda: erm.simultaneous({'events': [a, b], 'expected_order': ['b', 'a']},
                                               start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_simultaneous_index__shadow_v2(self):
        c = msg('c', PRAISE, 2000)
        c['effective_ts'] = 2000
        d = msg('d', PRAISE, 2000)
        d['effective_ts'] = 2000
        self.run_case('simultaneous_index__shadow_v2', sim_expected(['c', 'd'], 2),
                      lambda: erm.simultaneous({'events': [c, d], 'expected_order': ['c', 'd']},
                                               start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_simultaneous_mixed_ts_only__shadow_v2(self):
        # 混合同刻：无 effective_ts 的 ts-only 事件 (ts,0,0,id) 在显式有效 (eff,1,seq,id) 之前。
        e = msg('e', PRAISE, 2000)
        f = msg('f', PRAISE, 2000)
        f['effective_ts'], f['source_seq'] = 2000, 0
        self.run_case('simultaneous_mixed_ts_only__shadow_v2', sim_expected(['e', 'f'], 2),
                      lambda: erm.simultaneous({'events': [f, e], 'expected_order': ['e', 'f']},
                                               start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_simultaneous_same_id__shadow_v2(self):
        # 同 ID 只取排序后首条：effective 1500 的首条消费，3000 的重复条 duplicate_event。
        g = msg('g', PRAISE, 1000)
        g['effective_ts'], g['source_seq'] = 1500, 0
        h = msg('g', '再说一次', 3000)
        h['effective_ts'], h['source_seq'] = 3000, 1
        obs = self.run_case('simultaneous_same_id__shadow_v2', sim_expected(['g', 'g'], 1),
                            lambda: erm.simultaneous({'events': [h, g], 'expected_order': ['g', 'g']},
                                                     start_at=START, end_at=END, kernel=V2, ts=5.0))
        self.assertEqual(1, obs['processed'])


# ── 撤回：终态等于「从未存在」、单调洗不回、只追加 ────────────────────────


class RetractionTests(Base):
    def test_retraction__shadow_v1(self):
        self.run_case('retraction__shadow_v1', RETRACT_EXPECTED,
                      lambda: erm.retraction({'events': standard_events(), 'target_id': 'p1'},
                                             start_at=START, end_at=END, kernel=V1, ts=5.0))

    def test_retraction__shadow_v2(self):
        self.run_case('retraction__shadow_v2', RETRACT_EXPECTED,
                      lambda: erm.retraction({'events': standard_events(), 'target_id': 'p1'},
                                             start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_late_then_retract__shadow_v2(self):
        # 撤回 × 迟到：撤回迟到事件后终态等于「该迟到事件从未存在」。
        self.run_case('late_then_retract__shadow_v2', RETRACT_EXPECTED,
                      lambda: erm.retraction({'events': late_events(), 'target_id': 'B'},
                                             start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_simultaneous_then_retract__shadow_v2(self):
        # 撤回 × 同刻：撤回同刻并列事件之一，其余决序与贡献不变。
        self.run_case('simultaneous_then_retract__shadow_v2', RETRACT_EXPECTED,
                      lambda: erm.retraction({'events': simultaneous_events(), 'target_id': 's0'},
                                             start_at=START, end_at=END, kernel=V2, ts=5.0))

    def test_retract_all_baseline__shadow_v2(self):
        def runner():
            led = rev.new_ledger()
            base = rev.build_base_run(standard_events(), start_at=START, end_at=END, kernel=V2, ts=5.0)
            rev.commit_base_run(led, base)
            parent = base['revision_id']
            committed = None
            for eid in ('p1', 'n1', 'r1', 't1'):
                rec = dag.correction('observation_invalid', eid, '撤回', ts=10.0)
                committed = rev.commit_revision(led, rev.plan_revision(led, parent, rec, ts=10.0))
                parent = committed['revision_id']
            baseline = core.advance(core.initial_state(START), END)
            return {'final_processed': committed['state']['processed'],
                    'state_equals_baseline': committed['state'] == baseline,
                    'checks_ok': committed['checks']['ok'],
                    'expression_control': committed['expression_control'],
                    'counts_for_live': committed['counts_for_live']}
        self.run_case('retract_all_baseline__shadow_v2', {
            'final_processed': 0, 'state_equals_baseline': True, 'checks_ok': True,
            'expression_control': False, 'counts_for_live': False}, runner)


# ── 场景引擎与整张交叉矩阵：形状、确定性、影子口径、冻结键复用 ────────────


class ScenarioEngineTests(Base):
    def fixture(self):
        return {
            'late': {'events': late_events(), 'late_id': 'B', 'expected': dict(LATE_EXPECTED)},
            'restart': {'events': standard_events(), 'retract_id': 'p1', 'expected': dict(RESTART_EXPECTED)},
            'simultaneous': {'events': simultaneous_events(), 'expected_order': ['s1', 's0', 's2'],
                             'expected': sim_expected(['s1', 's0', 's2'], 3)},
            'retraction': {'events': standard_events(), 'target_id': 'p1', 'expected': dict(RETRACT_EXPECTED)},
        }

    def test_run_condition_matrix_full_cross_product(self):
        report = erm.run_condition_matrix(self.fixture(), start_at=START, end_at=END, ts=5.0)
        self.assertEqual('pass', report['status'], json.dumps(report['cases'], ensure_ascii=False))
        self.assertEqual(8, report['provenance']['cases'])
        self.assertEqual(8, report['provenance']['passed'])
        self.assertEqual(sorted(erm.CONDITIONS), report['provenance']['conditions'])
        self.assertEqual(sorted(erm.KERNELS), report['provenance']['kernels'])
        self.assertEqual(8, len(report['cases']))

    def test_run_condition_matrix_detects_deviation(self):
        fixture = self.fixture()
        fixture['simultaneous']['expected']['processed'] = 99
        report = erm.run_condition_matrix(fixture, start_at=START, end_at=END, ts=5.0)
        self.assertEqual('fail', report['status'])
        failed = [c for c in report['cases'] if c['status'] == 'fail']
        self.assertTrue(all(c['condition'] == 'simultaneous' for c in failed))
        self.assertEqual(2, len(failed))

    def test_engines_stamp_shadow_boundary(self):
        specs = {'late': {'events': late_events(), 'late_id': 'B'},
                 'restart': {'events': standard_events(), 'retract_id': 'p1'},
                 'simultaneous': {'events': simultaneous_events(), 'expected_order': ['s1', 's0', 's2']},
                 'retraction': {'events': standard_events(), 'target_id': 'p1'}}
        for condition, engine in erm.ENGINES.items():
            for kernel in erm.KERNELS:
                with self.subTest(condition=condition, kernel=kernel):
                    obs = engine(specs[condition], start_at=START, end_at=END, kernel=kernel, ts=5.0)
                    self.assertIs(False, obs['expression_control'])
                    self.assertIs(False, obs['counts_for_live'])
                    self.assertIn(obs['trajectory'], (traj.V1_REFERENCE, traj.HISTORICAL_REPLAY))

    def test_engines_are_deterministic(self):
        specs = {'late': {'events': late_events(), 'late_id': 'B'},
                 'restart': {'events': standard_events(), 'retract_id': 'p1'},
                 'simultaneous': {'events': simultaneous_events(), 'expected_order': ['s1', 's0', 's2']},
                 'retraction': {'events': standard_events(), 'target_id': 'p1'}}
        for condition, engine in erm.ENGINES.items():
            with self.subTest(condition=condition):
                one = engine(specs[condition], start_at=START, end_at=END, kernel=V2, ts=5.0)
                two = engine(copy.deepcopy(specs[condition]), start_at=START, end_at=END, kernel=V2, ts=5.0)
                self.assertEqual(one, two)

    def test_frozen_order_reuses_core_key(self):
        events = late_events()
        reference = legacy.replay(events, start_at=START, end_at=END)
        self.assertEqual([r['event_id'] for r in reference['timeline']],
                         [e['id'] for e in erm._frozen_order(events)])

    def test_restart_checkpoint_roundtrip_preserves_events(self):
        obs = erm.restart({'events': standard_events(), 'retract_id': 'p1'},
                          start_at=START, end_at=END, kernel=V2, ts=5.0)
        self.assertTrue(obs['checkpoint_stores_events_not_state'])
        self.assertTrue(obs['terminal_bit_identical'])
        self.assertTrue(obs['revision_terminal_bit_identical'])

    def test_engines_create_no_files(self):
        erm.late_arrival({'events': late_events(), 'late_id': 'B'},
                         start_at=START, end_at=END, kernel=V2, ts=5.0)
        erm.restart({'events': standard_events(), 'retract_id': 'p1'},
                    start_at=START, end_at=END, kernel=V2, ts=5.0)
        erm.simultaneous({'events': simultaneous_events(), 'expected_order': ['s1', 's0', 's2']},
                         start_at=START, end_at=END, kernel=V2, ts=5.0)
        erm.retraction({'events': standard_events(), 'target_id': 'p1'},
                       start_at=START, end_at=END, kernel=V2, ts=5.0)
        self.assertTrue(self.nothing_created())


# ── 矩阵性质：tick 不变性、单调链、排列无关、越界排除 ──────────────────────


class MatrixPropertyTests(Base):
    def test_late_tick_invariance(self):
        for kernel in erm.KERNELS:
            with self.subTest(kernel=kernel):
                plain = rev.build_base_run(late_events(), start_at=START, end_at=END, kernel=kernel, ts=5.0)
                for tick in (60, 7):
                    ticked = rev.build_base_run(late_events(), start_at=START, end_at=END,
                                                kernel=kernel, tick_seconds=tick, ts=5.0)
                    self.assertLessEqual(max_distance(plain['state'], ticked['state']), 1e-10)
                    self.assertTrue(ticked['checks']['ok'])

    def test_restart_tick_invariance(self):
        for kernel in erm.KERNELS:
            with self.subTest(kernel=kernel):
                plain = erm.restart({'events': standard_events(), 'retract_id': 'p1'},
                                    start_at=START, end_at=END, kernel=kernel, ts=5.0)
                ticked = erm.restart({'events': standard_events(), 'retract_id': 'p1', 'tick_seconds': 30},
                                     start_at=START, end_at=END, kernel=kernel, ts=5.0)
                self.assertTrue(plain['terminal_bit_identical'])
                self.assertTrue(ticked['terminal_bit_identical'])
                self.assertTrue(ticked['revision_terminal_bit_identical'])

    def test_retraction_monotonic_across_chain(self):
        for kernel in erm.KERNELS:
            with self.subTest(kernel=kernel):
                led = rev.new_ledger()
                base = rev.build_base_run(standard_events(), start_at=START, end_at=END, kernel=kernel, ts=5.0)
                rev.commit_base_run(led, base)
                rec1 = dag.correction('observation_invalid', 'p1', '撤回1', ts=10.0)
                c1 = rev.commit_revision(led, rev.plan_revision(led, base['revision_id'], rec1, ts=10.0))
                rec2 = dag.correction('observation_invalid', 'r1', '撤回2', ts=11.0)
                c2 = rev.commit_revision(led, rev.plan_revision(led, c1['revision_id'], rec2, ts=11.0))
                never = rev.build_base_run([e for e in standard_events() if e['id'] not in ('p1', 'r1')],
                                           start_at=START, end_at=END, kernel=kernel, ts=5.0)
                self.assertEqual(c2['state'], never['state'])
                row = next(r for r in c2['timeline'] if r['event_id'] == 'p1')
                self.assertEqual('retracted_source', row['rule'])

    def test_simultaneous_permutation_invariance(self):
        events = simultaneous_events()
        shuffled = list(events)
        random.Random(42).shuffle(shuffled)
        for kernel in erm.KERNELS:
            with self.subTest(kernel=kernel):
                one = rev.build_base_run(events, start_at=START, end_at=END, kernel=kernel, ts=5.0)
                two = rev.build_base_run(shuffled, start_at=START, end_at=END, kernel=kernel, ts=5.0)
                self.assertEqual(one['state'], two['state'])
                self.assertEqual([r['event_id'] for r in one['timeline']],
                                 [r['event_id'] for r in two['timeline']])

    def test_late_event_outside_window_excluded(self):
        events = late_events()
        events[0]['effective_ts'] = 6000  # B 有效时间越界
        for kernel in erm.KERNELS:
            with self.subTest(kernel=kernel):
                run = rev.build_base_run(events, start_at=START, end_at=END, kernel=kernel, ts=5.0)
                clean = rev.build_base_run([e for e in late_events() if e['id'] != 'B'],
                                           start_at=START, end_at=END, kernel=kernel, ts=5.0)
                self.assertEqual(run['state'], clean['state'])
                self.assertEqual(1, run['excluded'].get('outside_window'))

    def test_retraction_not_naive_delta(self):
        # 指数恢复路径依赖：父终态减 delta ≠ 撤源重算（重启/撤回都不得拿终态当初值）。
        led = rev.new_ledger()
        base = rev.build_base_run(standard_events(), start_at=START, end_at=END, kernel=V2, ts=5.0)
        rev.commit_base_run(led, base)
        row = next(r for r in base['timeline'] if r['event_id'] == 'p1' and r['consumed'])
        naive = base['state']['valence'] - row['delta']['valence']
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=10.0)
        committed = rev.commit_revision(led, rev.plan_revision(led, base['revision_id'], rec, ts=10.0))
        self.assertNotEqual(committed['state']['valence'], naive)
        never = rev.build_base_run([e for e in standard_events() if e['id'] != 'p1'],
                                   start_at=START, end_at=END, kernel=V2, ts=5.0)
        self.assertEqual(committed['state'], never['state'])


# ── 执行器：纯编排、确定性、如实分型（与 V03 同范式）──────────────────────


class ExecutorTests(unittest.TestCase):
    def spec(self, case_id='c1', expected=None, runner=None):
        return erm.case(case_id, 'late', V2, {'ok': True} if expected is None else expected,
                        (lambda: {'ok': True}) if runner is None else runner)

    def test_report_shape_and_pass(self):
        report = erm.run_matrix([self.spec()])
        self.assertEqual('pass', report['status'])
        self.assertEqual({'status', 'cases', 'expression_control', 'counts_for_live',
                          'trajectory', 'provenance', 'limitations'}, set(report))
        entry = report['cases'][0]
        self.assertEqual({'case_id', 'condition', 'kernel', 'status', 'expected',
                          'observed', 'mismatches', 'error'}, set(entry))
        for key in ('algorithm_version', 'method', 'cases', 'passed', 'failed',
                    'errored', 'conditions', 'kernels', 'llm_reinterpretation',
                    'network', 'production_ledger'):
            self.assertIn(key, report['provenance'])
        self.assertFalse(report['provenance']['production_ledger'])
        self.assertTrue(report['limitations'])

    def test_report_shadow_boundary(self):
        report = erm.run_matrix([self.spec()])
        self.assertIs(False, report['expression_control'])
        self.assertIs(False, report['counts_for_live'])
        self.assertEqual(traj.HISTORICAL_REPLAY, report['trajectory'])

    def test_fail_reports_mismatch_not_pass(self):
        report = erm.run_matrix([self.spec(expected={'ok': False})])
        self.assertEqual('fail', report['status'])
        mismatch = report['cases'][0]['mismatches'][0]
        self.assertEqual('ok', mismatch['key'])
        self.assertIs(False, mismatch['expected'])
        self.assertIs(True, mismatch['observed'])
        self.assertTrue(mismatch['present'])

    def test_missing_observation_key_is_fail_not_default_true(self):
        report = erm.run_matrix([self.spec(runner=lambda: {})])
        self.assertEqual('fail', report['status'])
        self.assertFalse(report['cases'][0]['mismatches'][0]['present'])

    def test_runner_exception_is_error_not_pass(self):
        def boom():
            raise RuntimeError('契约撞锁了')
        report = erm.run_matrix([self.spec(runner=boom)])
        self.assertEqual('error', report['status'])
        self.assertIn('RuntimeError', report['cases'][0]['error'])
        self.assertIn('契约撞锁了', report['cases'][0]['error'])

    def test_non_dict_observation_is_error(self):
        report = erm.run_matrix([self.spec(runner=lambda: ['不是观察'])])
        self.assertEqual('error', report['status'])

    def test_deterministic_order_and_no_clock(self):
        specs = [self.spec('z_case'), self.spec('a_case')]
        one = erm.run_matrix(specs)
        two = erm.run_matrix(list(reversed(specs)))
        self.assertEqual(json.dumps(one, sort_keys=True, ensure_ascii=False),
                         json.dumps(two, sort_keys=True, ensure_ascii=False))
        self.assertEqual(['a_case', 'z_case'], [c['case_id'] for c in one['cases']])
        self.assertNotIn('ts', one['provenance'])
        self.assertNotIn('seconds', one['provenance'])

    def test_bad_specs_are_rejected_loudly(self):
        with self.assertRaises(ValueError):
            erm.run_matrix([])
        with self.assertRaises(ValueError):
            erm.run_matrix([self.spec('dup'), self.spec('dup')])
        with self.assertRaises(ValueError):
            erm.case('c', 'no_such_condition', V2, {'ok': True}, lambda: {})
        with self.assertRaises(ValueError):
            erm.case('c', 'late', 'no_such_kernel', {'ok': True}, lambda: {})
        with self.assertRaises(ValueError):
            erm.case('c', 'late', V2, {}, lambda: {})
        with self.assertRaises(ValueError):
            erm.case('c', 'late', V2, {'ok': True}, '不是 callable')
        with self.assertRaises(ValueError):
            erm.case('', 'late', V2, {'ok': True}, lambda: {})

    def test_limitations_declare_what_is_not_covered(self):
        limits = ' '.join(erm.LIMITATIONS)
        for token in ('S07', 'S08', 'C04', 'expression_control', '生产', '候选'):
            self.assertIn(token, limits)

    def test_coverage_counts_intersections(self):
        got = erm.coverage([self.spec('a'), self.spec('b')])
        self.assertEqual(2, got['cases'])
        self.assertEqual({'late/shadow-v2': 2}, got['intersections'])


# ── AST 锁：只复用冻结原语，无第二套动力学/顺序，无写入，影子恒 False ──────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(BOT, 'emotion_replay_matrix.py').read_text(encoding='utf-8')
        cls.tree = ast.parse(cls.source)

    def test_only_frozen_primitives_and_stdlib_are_imported(self):
        allowed = {'copy', 'json', 'emotion_core', 'emotion_revision',
                   'emotion_trajectories', 'invalidation_dag'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or '').split('.')[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             'S06 矩阵 import 了写入/网络/模型/动力学 API：%s' % sorted(names - allowed))

    def test_no_self_contained_dynamics(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual('exp', node.func.attr,
                                    '恢复公式必须来自冻结内核 advance，不许自带第二套动力学')
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotEqual('tau_seconds', node.value,
                                    '恢复时间尺度必须取自内核 PARAMETERS')
        self.assertNotIn('import math', self.source)

    def test_no_second_replay_ordering(self):
        # 决序完全委托 emotion_core._replay_key：代码里不得手搓 source_seq/input_seq
        # 顺序键（docstring 描述冻结规则不算；只查代码中的字符串字面量与键函数）。
        self.assertIn('legacy._replay_key', self.source)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, ('source_seq', 'input_seq'),
                                 '不许在代码里手搓第二套并列决序键')
            if isinstance(node, ast.FunctionDef):
                self.assertNotIn(node.name, ('_replay_key', '_order_key', '_sort_key', '_tie_break'),
                                 '不许定义第二套回放顺序键函数')

    def test_reuses_frozen_primitives(self):
        for token in ('rev.build_base_run', 'rev.plan_revision', 'rev.commit_revision',
                      'dag.correction', 'legacy._replay_key', 'legacy._event_header'):
            self.assertIn(token, self.source, 'S06 必须复用冻结原语 %s' % token)

    def test_expression_control_is_false(self):
        self.assertIn("'expression_control': False", self.source)
        self.assertNotIn("'expression_control': True", self.source)
        self.assertIn("'counts_for_live': False", self.source)
        self.assertNotIn("'counts_for_live': True", self.source)

    def test_no_write_sql_or_file_calls(self):
        forbidden = {'open', 'connect', 'getenv', 'mkdir', 'makedirs', 'remove', 'unlink',
                     'rmtree', 'rename', 'write_text', 'write_bytes', 'commit', 'executescript',
                     'executemany', 'rollback', 'tick', 'record_event', 'retract_evidence',
                     'write_cognition_transaction', 'write_invalidation_transaction'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ', 'ALTER ', 'REPLACE '):
                    self.assertNotIn(verb, upper, 'S06 矩阵里出现写入 SQL：%s' % node.value[:60])
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else '')
                self.assertNotIn(name, forbidden, 'S06 矩阵里出现写操作 %s' % name)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ('getenv', 'join', 'abspath'),
                                     '模块级冻住了路径；S06 矩阵必须是纯编排')

    def test_axes_are_fixed(self):
        self.assertEqual(('late', 'restart', 'simultaneous', 'retraction'), erm.CONDITIONS)
        self.assertEqual(('shadow-v1', 'shadow-v2'), erm.KERNELS)
        self.assertEqual(set(erm.KERNELS), set(rev.KERNELS))
        self.assertEqual(set(erm.CONDITIONS), set(erm.ENGINES))

    def test_no_llm_reinterpretation_claim(self):
        self.assertIn('llm_reinterpretation', self.source)
        self.assertFalse(erm.run_matrix([erm.case('c', 'late', V2, {'ok': True},
                                                  lambda: {'ok': True})])['provenance']['llm_reinterpretation'])


# ── 覆盖审计：声明的每个 条件×内核 交叉点都有真实用例且真执行过 ────────────


class ZCoverageTests(unittest.TestCase):
    def test_matrix_covers_every_declared_intersection(self):
        self.assertGreaterEqual(len(CASE_PLAN), 20)
        self.assertEqual(INTERSECTIONS, set(CASE_PLAN.values()),
                         '声明的交叉点与用例登记不一致')

    def test_every_planned_case_has_a_real_test_method(self):
        test_classes = [obj for _, obj in globals().items()
                        if isinstance(obj, type) and issubclass(obj, unittest.TestCase)]
        methods = {name for cls in test_classes for name in dir(cls) if name.startswith('test_')}
        for case_id in CASE_PLAN:
            self.assertIn('test_' + case_id, methods, '用例 %s 没有真实测试' % case_id)

    def test_case_ids_are_unique_and_on_the_axes(self):
        self.assertEqual(len(CASE_PLAN), len(set(CASE_PLAN)))
        for case_id, (condition, kernel) in CASE_PLAN.items():
            self.assertIn(condition, erm.CONDITIONS, case_id)
            self.assertIn(kernel, erm.KERNELS, case_id)


if __name__ == '__main__':
    unittest.main()