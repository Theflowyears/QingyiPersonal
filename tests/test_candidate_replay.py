# -*- coding: utf-8 -*-
"""S07 候选回放性能与脱敏读取的回归锁（路线 §9 / S01 candidate_counterfactual）。

钉死的行为：
  · 候选反事实：只复用冻结 integrated_appraisal/advance/clip，候选标签经源事件
    核对后才参与；不匹配隔离 source_mismatch；终态与独立冻结折叠逐位一致；
  · 性能预算：max_events 限制冲击数、max_steps 限制步进数，超预算事件记
    budget_excluded 零冲击审计行，超步进大声拒绝；性能元数据全是确定性计数，
    墙上时钟 ts 不影响 state/run_id/performance；
  · 确定性：同输入同报告、输入顺序无关、不同 tick 切分按经过时间一致；
  · 审计：无因跳变/篡改时间线被抓；读函数纯读，回放不写账本、不落盘；
  · 脱敏：白名单只读视图丢弃原话/QQ/会话/事件ID/标签明细，只回聚合与标志；
    纯函数深拷贝、残留 PII 扫描、reason_codes 主动出声；只接受候选轨迹报告；
  · 只读桥：缺库报 absent、绝不建库建表建目录、DATA_DIR 调用时解析。
AST 锁钉死旁路契约：无写入 SQL、无写文件/实时写入调用、无顶层冻路径、
不自带 exp/tau 恢复公式、expression_control 恒字面量 False、复用冻结原语。
"""
import ast
import asyncio
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import candidate_replay as cr  # noqa: E402
import emotion_core as legacy  # noqa: E402
import emotion_dynamics as dyn  # noqa: E402
import emotion_trajectories as tr  # noqa: E402

# 冻结口径（与 test_emotion_revision / test_emotion_rule_freeze 同值）。
LIVE_PARAMETER_HASH = 'fddcdc0ec1fa48fe54d6a83749af04133eb2397fc3f0cdddbcdde42ca7d08a01'
DYNAMICS_HASH = '9272cdbc3b38376c4215a016fd39bb8bf693ed963115b94d40d39154b625bc89'
APPRAISAL_HASH = '06a984d264621d0f7fd9bfb709cc8340a89428ebe3b2899d3685139c8c140777'

PRAISE = '谢谢你帮我查清楚了'
REBUKE = '你刚才说错了'
# 词表外文本：冻结词法不给效价，只有候选标注能给——正好检验反事实语义。
OUTSIDE_VOCAB = '这次处理得很靠谱'
START, END = 900.0, 5000.0
QQ = '284573916'


def msg(eid, text, ts, addressed=True, **extra):
    return {'id': eid, 'kind': 'message', 'ts': ts, 'source': 'onebot',
            'payload': {'text': text, 'addressed_to_self': addressed, **extra}}


def tool(eid, ts, ok):
    return {'id': eid, 'kind': 'tool_result', 'ts': ts, 'source': 'onebot',
            'payload': {'ok': ok}}


def label(eid, text, ts, stance='praise', confidence=0.95, state='done',
          kind='message', session='', address_basis='', reason='明确肯定', model='test-model'):
    return {'event_id': eid, 'kind': kind, 'session': session, 'at': ts, 'text': text,
            'address_basis': address_basis, 'stance': stance, 'confidence': confidence,
            'state': state, 'reason': reason, 'model': model}


def standard_events():
    return [msg('p1', PRAISE, 1000), msg('n1', '今天天气不错', 2000, addressed=False),
            msg('r1', REBUKE, 3000), tool('t1', 4000, False)]


def fold_candidate(events, labels, start=START, end=END):
    """用冻结 integrated_appraisal/advance/clip 独立折叠，作为反事实终态对照。"""
    state = dyn.initial_state(start)
    seen = set()
    for event in sorted(events, key=lambda e: (e.get('effective_ts', e['ts']), e['id'])):
        eid = event['id']
        if eid in seen:
            continue
        seen.add(eid)
        ts, eff = event['ts'], event.get('effective_ts', event['ts'])
        if ts <= 0 or eff <= 0 or not (start <= eff <= end):
            continue
        lab = labels.get(eid)
        if lab is not None and not cr._label_matches(event, lab):
            continue
        view = dict(event)
        if view['kind'] in ('group_message', 'private_message'):
            view['kind'] = 'message'
        assessment = dyn.integrated_appraisal(view, lab)
        state = dyn.advance(state, eff)
        for key in cr.DIMENSIONS:
            low, high = dyn.PARAMETERS['bounds'][key]
            state[key] = min(high, max(low, state[key] + assessment['delta'][key]))
        state['processed'] += 1
    return dyn.advance(state, end)


def max_distance(a, b):
    return max(abs(a[k] - b[k]) for k in cr.DIMENSIONS)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s07_", ignore_cleanup_errors=True)
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

    def run_replay(self, events=None, labels=None, **kw):
        kw.setdefault('start_at', START)
        kw.setdefault('end_at', END)
        kw.setdefault('ts', 5.0)
        return cr.replay_candidates(standard_events() if events is None else events,
                                    labels, **kw)


class ReplaySemanticsTests(Base):
    def test_state_matches_frozen_integrated_fold(self):
        events = standard_events()
        labels = {'p1': label('p1', PRAISE, 1000)}
        run = self.run_replay(events, labels)
        self.assertEqual(run['state'], fold_candidate(events, labels))
        self.assertTrue(run['checks']['ok'])

    def test_candidate_label_adds_valence_outside_lexical_vocab(self):
        events = [msg('c1', OUTSIDE_VOCAB, 1000)]
        with_label = self.run_replay(events, {'c1': label('c1', OUTSIDE_VOCAB, 1000, stance='praise')})
        without = self.run_replay(events, {})
        self.assertGreater(with_label['state']['valence'], without['state']['valence'])
        self.assertEqual(without['state']['valence'], 0.0)
        row = with_label['timeline'][0]
        self.assertEqual(row['rule'], 'integrated_praise')
        self.assertEqual(row['evidence_origin'], 'candidate')

    def test_rebuke_label_lowers_valence(self):
        events = [msg('c1', OUTSIDE_VOCAB, 1000)]
        run = self.run_replay(events, {'c1': label('c1', OUTSIDE_VOCAB, 1000, stance='rebuke')})
        self.assertLess(run['state']['valence'], 0.0)
        self.assertEqual(run['timeline'][0]['rule'], 'integrated_rebuke')

    def test_uncertain_label_suppresses_valence(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'c1': label('c1', PRAISE, 1000, stance='uncertain')})
        self.assertEqual(run['state']['valence'], 0.0)
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'label_uncertain')
        self.assertFalse(run['timeline'][0]['included'])

    def test_pending_label_suppresses_valence(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'c1': label('c1', PRAISE, 1000, state='pending')})
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'label_pending')
        self.assertEqual(run['state']['valence'], 0.0)

    def test_low_confidence_label_suppresses_valence(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'c1': label('c1', PRAISE, 1000, confidence=0.5)})
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'low_confidence')
        self.assertEqual(run['state']['valence'], 0.0)

    def test_no_label_uses_lexical_origin(self):
        run = self.run_replay([msg('p1', PRAISE, 1000)], {})
        row = run['timeline'][0]
        self.assertEqual(row['evidence_origin'], 'lexical')
        self.assertEqual(row['rule'], 'integrated_praise')

    def test_group_private_appraised_as_message(self):
        event = msg('g1', PRAISE, 1000)
        event['kind'] = 'group_message'
        run = self.run_replay([event], {'g1': label('g1', PRAISE, 1000, kind='group_message')})
        row = run['timeline'][0]
        self.assertEqual(row['kind'], 'group_message')
        self.assertEqual(row['rule'], 'integrated_praise')
        self.assertTrue(row['consumed'])

    def test_tool_result_uses_candidate_semantics(self):
        # integrated_appraisal 把 tool 的 control 归零（候选语义只认 message 效价），
        # 但保留 arousal/attention；终态必须与冻结 integrated 折叠一致。
        events = [tool('t1', 1000, True)]
        run = self.run_replay(events, {})
        self.assertEqual(run['state'], fold_candidate(events, {}))
        assessment = dyn.integrated_appraisal(events[0], None)
        self.assertEqual(assessment['delta']['control'], 0.0)
        self.assertGreater(assessment['delta']['arousal'], 0.0)

    def test_empty_events_recover_to_baseline(self):
        run = self.run_replay([], {})
        self.assertEqual(run['state'], dyn.advance(dyn.initial_state(START), END))
        self.assertEqual(run['state']['processed'], 0)
        self.assertTrue(run['checks']['ok'])

    def test_duplicate_event_consumed_once(self):
        events = standard_events() + [msg('p1', PRAISE, 4500)]
        run = self.run_replay(events, {})
        self.assertEqual(run['state']['processed'], 4)
        self.assertEqual(run['excluded'].get('duplicate_event'), 1)
        clean = self.run_replay(standard_events(), {})
        self.assertEqual(run['state'], clean['state'])

    def test_window_and_unknown_time_exclusions(self):
        events = standard_events() + [msg('bad', PRAISE, 0), msg('late', PRAISE, 6000)]
        run = self.run_replay(events, {})
        self.assertEqual(run['excluded'].get('unknown_timestamp'), 1)
        self.assertEqual(run['excluded'].get('outside_window'), 1)
        clean = self.run_replay(standard_events(), {})
        self.assertEqual(run['state'], clean['state'])

    def test_forged_recorded_appraisal_rejected(self):
        events = standard_events()
        forged = dyn.appraise(events[0])
        forged['delta']['valence'] += 0.1
        events[0]['appraisal'] = forged
        with self.assertRaises(ValueError):
            self.run_replay(events, {})

    def test_matching_recorded_appraisal_accepted(self):
        events = standard_events()
        events[0]['appraisal'] = dyn.appraise(events[0])
        run = self.run_replay(events, {})
        plain = self.run_replay(standard_events(), {})
        self.assertEqual(run['state'], plain['state'])


class SourceVerificationTests(Base):
    def test_mismatched_text_quarantined(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'c1': label('c1', '完全不同的原话', 1000)})
        row = run['timeline'][0]
        self.assertEqual(row['rule'], 'quarantine')
        self.assertEqual(row['exclusion_reason'], 'source_mismatch')
        self.assertFalse(row['consumed'])
        self.assertEqual(row['before'], row['after'])
        self.assertEqual(run['state']['valence'], 0.0)

    def test_mismatched_kind_quarantined(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'c1': label('c1', PRAISE, 1000, kind='private_message')})
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'source_mismatch')

    def test_mismatched_session_quarantined(self):
        event = msg('c1', PRAISE, 1000)
        event['session'] = 'private:1'
        run = self.run_replay([event], {'c1': label('c1', PRAISE, 1000, session='private:2')})
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'source_mismatch')

    def test_mismatched_at_quarantined(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'c1': label('c1', PRAISE, 1001)})
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'source_mismatch')

    def test_mismatched_address_basis_quarantined(self):
        events = [msg('c1', PRAISE, 1000, address_basis='private')]
        run = self.run_replay(events, {'c1': label('c1', PRAISE, 1000, address_basis='at_self')})
        self.assertEqual(run['timeline'][0]['exclusion_reason'], 'source_mismatch')

    def test_bridge_truncation_tolerance(self):
        source = '字' * 200
        event = msg('c1', source, 1000)
        run = self.run_replay([event], {'c1': label('c1', source + '延长部分', 1000)})
        self.assertNotEqual(run['timeline'][0]['rule'], 'quarantine')
        self.assertTrue(run['timeline'][0]['consumed'])

    def test_orphan_labels_counted_not_replayed(self):
        events = [msg('c1', PRAISE, 1000)]
        run = self.run_replay(events, {'ghost': label('ghost', PRAISE, 1000)})
        self.assertEqual(run['stats']['orphan_labels'], 1)
        self.assertEqual(run['orphan_labels'], ['ghost'])
        self.assertEqual(run['stats']['total'], 1)


class BudgetPerformanceTests(Base):
    def many_events(self, n):
        return [msg('e%d' % i, OUTSIDE_VOCAB, 1000 + i) for i in range(n)]

    def test_max_events_bounds_impulses(self):
        run = self.run_replay(self.many_events(10), {}, max_events=4)
        perf = run['performance']
        self.assertEqual(perf['events_replayed'], 4)
        self.assertEqual(perf['budget_excluded'], 6)
        self.assertTrue(perf['truncated'])
        self.assertEqual(run['state']['processed'], 4)
        self.assertEqual(perf['events_considered'], 10)

    def test_budget_excluded_rows_zero_impulse(self):
        run = self.run_replay(self.many_events(6), {}, max_events=2)
        budget_rows = [r for r in run['timeline'] if r['rule'] == 'budget_exceeded']
        self.assertEqual(len(budget_rows), 4)
        for row in budget_rows:
            self.assertFalse(row['consumed'])
            self.assertEqual(row['before'], row['after'])
            self.assertEqual(row['delta'], dict.fromkeys(cr.DIMENSIONS, 0.0))

    def test_performance_is_wall_clock_independent(self):
        one = self.run_replay(self.many_events(5), {}, ts=1.0, max_events=3)
        two = self.run_replay(self.many_events(5), {}, ts=99999.0, max_events=3)
        self.assertEqual(one['performance'], two['performance'])
        self.assertEqual(one['state'], two['state'])
        self.assertEqual(one['run_id'], two['run_id'])

    def test_work_units_bounded(self):
        run = self.run_replay(self.many_events(8), {}, max_events=5, max_steps=50)
        perf = run['performance']
        self.assertLessEqual(perf['appraisals'], perf['max_events'])
        self.assertLessEqual(perf['advance_steps'], perf['max_steps'])
        self.assertLessEqual(perf['work_units'], perf['work_units_max'])
        self.assertTrue(perf['bounded_work'])
        self.assertTrue(perf['deterministic'])

    def test_max_steps_upfront_window_check_raises(self):
        with self.assertRaises(ValueError):
            self.run_replay(self.many_events(3), {}, start_at=0, end_at=100000,
                            tick_seconds=0.001)

    def test_max_steps_small_with_tick_raises(self):
        with self.assertRaises(ValueError):
            self.run_replay(self.many_events(3), {}, start_at=0, end_at=100,
                            tick_seconds=1, max_steps=5)

    def test_max_steps_small_without_tick_raises(self):
        with self.assertRaises(ValueError):
            self.run_replay(self.many_events(10), {}, max_steps=3)

    def test_invalid_max_events_rejected(self):
        for bad in (0, -1, True, cr.MAX_EVENTS_CEILING + 1, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.run_replay(standard_events(), {}, max_events=bad)

    def test_invalid_max_steps_rejected(self):
        for bad in (0, -1, False, legacy._MAX_REPLAY_STEPS + 1, 2.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.run_replay(standard_events(), {}, max_steps=bad)

    def test_default_budgets(self):
        run = self.run_replay(standard_events(), {})
        self.assertEqual(run['performance']['max_events'], cr.DEFAULT_MAX_EVENTS)
        self.assertEqual(run['performance']['max_steps'], legacy._MAX_REPLAY_STEPS)
        self.assertFalse(run['performance']['truncated'])

    def test_budget_selection_deterministic(self):
        events = self.many_events(10)
        one = self.run_replay(events, {}, max_events=4)
        two = self.run_replay(list(reversed(events)), {}, max_events=4)
        self.assertEqual([r['event_id'] for r in one['timeline'] if r['consumed']],
                         [r['event_id'] for r in two['timeline'] if r['consumed']])
        self.assertEqual(one['state'], two['state'])

    def test_tick_split_invariance(self):
        events = standard_events()
        labels = {'p1': label('p1', PRAISE, 1000)}
        plain = self.run_replay(events, labels)
        ticked = self.run_replay(events, labels, tick_seconds=60)
        odd = self.run_replay(events, labels, tick_seconds=7)
        self.assertLessEqual(max_distance(plain['state'], ticked['state']), 1e-10)
        self.assertLessEqual(max_distance(plain['state'], odd['state']), 1e-10)
        self.assertTrue(ticked['checks']['ok'])


class DeterminismTests(Base):
    def test_same_inputs_same_run(self):
        labels = {'p1': label('p1', PRAISE, 1000)}
        one = self.run_replay(standard_events(), labels)
        two = self.run_replay(standard_events(), labels)
        self.assertEqual(one, two)
        self.assertTrue(one['checks']['deterministic_replay'])

    def test_input_order_independent_state_and_timeline(self):
        import random
        events = standard_events()
        shuffled = list(events)
        random.Random(42).shuffle(shuffled)
        one = self.run_replay(events, {})
        two = self.run_replay(shuffled, {})
        self.assertEqual(one['state'], two['state'])
        self.assertEqual(one['timeline'], two['timeline'])

    def test_effective_time_ordering_no_backward_state(self):
        a = msg('A', PRAISE, 2000)
        b = msg('B', '嗯', 1000, addressed=False)
        b['effective_ts'], b['source_seq'] = 3000, 0
        c = msg('C', '哦', 500, addressed=False)
        c['effective_ts'], c['source_seq'] = 2500, 1
        run = self.run_replay([b, c, a], {}, start_at=400, end_at=4000)
        self.assertEqual([r['event_id'] for r in run['timeline']], ['A', 'C', 'B'])
        times = [r['at'] for r in run['timeline']]
        self.assertEqual(times, sorted(times))
        self.assertEqual(run['state']['at'], 4000.0)

    def test_replay_creates_no_files(self):
        self.run_replay(standard_events(), {'p1': label('p1', PRAISE, 1000)})
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_replay_does_not_write_ledger(self):
        ledger = cr.new_ledger()
        snapshot = json.dumps(ledger, sort_keys=True)
        self.run_replay(standard_events(), {})
        self.assertEqual(json.dumps(ledger, sort_keys=True), snapshot)
        self.assertEqual(ledger['order'], [])


class AuditTests(Base):
    def test_verify_passes_clean(self):
        run = self.run_replay(standard_events(), {'p1': label('p1', PRAISE, 1000)})
        checked = cr.verify_run(run)
        self.assertTrue(checked['ok'])
        self.assertEqual(checked['impulse_accounting_error'], 0.0)
        self.assertLessEqual(checked['recovery_continuity_error'], 1e-10)
        self.assertTrue(checked['processed_matches'])
        self.assertTrue(checked['no_unexplained_jump'])

    def test_tampered_after_state_detected(self):
        run = self.run_replay()
        row = next(r for r in run['timeline'] if r['consumed'])
        row['after']['valence'] += 0.05
        checked = cr.verify_run(run)
        self.assertFalse(checked['ok'])
        self.assertGreater(checked['impulse_accounting_error'], 0.0)

    def test_tampered_delta_detected(self):
        run = self.run_replay()
        row = next(r for r in run['timeline'] if r['consumed'])
        row['delta']['valence'] += 0.01
        self.assertFalse(cr.verify_run(run)['ok'])

    def test_unexplained_jump_detected(self):
        run = self.run_replay()
        consumed = [r for r in run['timeline'] if r['consumed']]
        consumed[1]['before']['valence'] += 0.2
        checked = cr.verify_run(run)
        self.assertFalse(checked['ok'])
        self.assertFalse(checked['no_unexplained_jump'])

    def test_excluded_row_cannot_carry_delta(self):
        events = standard_events() + [msg('bad', PRAISE, 0)]
        run = self.run_replay(events, {})
        row = next(r for r in run['timeline'] if r['rule'] == 'unknown_timestamp')
        row['delta']['valence'] = 0.01
        self.assertFalse(cr.verify_run(run)['ok'])

    def test_verify_reports_structure_damage_without_raising(self):
        checked = cr.verify_run({'start_at': 0, 'end_at': 1, 'timeline': None, 'state': {}})
        self.assertFalse(checked['ok'])
        self.assertTrue(checked['reason'])


class LedgerTests(Base):
    def test_commit_append_only(self):
        ledger = cr.new_ledger()
        run = self.run_replay()
        committed = cr.commit_run(ledger, run)
        self.assertEqual(committed['status'], 'committed')
        self.assertEqual(ledger['order'], [run['run_id']])
        with self.assertRaises(ValueError):
            cr.commit_run(ledger, run)
        self.assertEqual(len(ledger['order']), 1)

    def test_tampered_content_rejected(self):
        ledger = cr.new_ledger()
        run = self.run_replay()
        run['events'][0]['payload']['text'] = '被篡改的原话'
        with self.assertRaises(ValueError):
            cr.commit_run(ledger, run)
        self.assertEqual(ledger['order'], [])

    def test_tampered_state_rejected_by_audit(self):
        ledger = cr.new_ledger()
        run = self.run_replay()
        run['state']['valence'] += 0.1
        with self.assertRaises(ValueError):
            cr.commit_run(ledger, run)

    def test_get_run_returns_deep_copy(self):
        ledger = cr.new_ledger()
        run = cr.commit_run(ledger, self.run_replay())
        got = cr.get_run(ledger, run['run_id'])
        got['state']['valence'] = 0.9
        got['timeline'].clear()
        self.assertEqual(cr.get_run(ledger, run['run_id']), run)
        self.assertIsNone(cr.get_run(ledger, 'cand_missing'))

    def test_bad_ledger_rejected(self):
        for bad in (None, {}, {'runs': [], 'order': []}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cr.get_run(bad, 'x')

    def test_non_candidate_run_rejected(self):
        ledger = cr.new_ledger()
        with self.assertRaises(ValueError):
            cr.commit_run(ledger, {'branch': 'base', 'run_id': 'x'})

    def test_shadow_boundary_stamps(self):
        run = self.run_replay()
        self.assertEqual(run['trajectory'], tr.CANDIDATE_COUNTERFACTUAL)
        self.assertEqual(run['kernel_version'], dyn.SEMANTIC_VERSION)
        self.assertFalse(run['counts_for_live'])
        self.assertIs(run['expression_control'], False)
        self.assertEqual(run['parameter_hash'], LIVE_PARAMETER_HASH)
        self.assertEqual(run['dynamics_parameter_hash'], DYNAMICS_HASH)
        self.assertEqual(run['appraisal_rule_hash'], APPRAISAL_HASH)
        self.assertTrue(run['limitations'])
        self.assertFalse(run['provenance']['llm_reinterpretation'])


class DesensitizationTests(Base):
    def rich_run(self):
        event = msg('evt_secret_1', OUTSIDE_VOCAB, 1000, address_basis='private')
        event['actor_qq'] = QQ
        event['topic_qq'] = QQ
        event['session'] = 'private:' + QQ
        labels = {'evt_secret_1': label('evt_secret_1', OUTSIDE_VOCAB, 1000, session='private:' + QQ,
                                      address_basis='private', reason='主人私下夸她', model='secret-model')}
        return self.run_replay([event], labels)

    def test_no_raw_text_qq_session_ids_in_view(self):
        run = self.rich_run()
        view = cr.desensitize_report(run)
        encoded = json.dumps(view, ensure_ascii=False)
        for secret in (OUTSIDE_VOCAB, QQ, 'private:' + QQ, 'evt_secret_1', '主人私下夸她', 'secret-model'):
            self.assertNotIn(secret, encoded, '脱敏视图泄漏了 %s' % secret)

    def test_aggregates_and_numeric_state_present(self):
        view = cr.desensitize_report(self.rich_run())
        self.assertEqual(view['stats']['total'], 1)
        self.assertEqual(view['stats']['stance_counts'], {'praise': 1})
        self.assertEqual(view['stats']['label_state_counts'], {'done': 1})
        for key in (*cr.DIMENSIONS, 'at', 'processed'):
            self.assertIsInstance(view['state'][key], (int, float))
        self.assertNotIn('events', view)
        self.assertNotIn('labels', view)
        self.assertNotIn('timeline', view)

    def test_reason_codes_speak_up(self):
        view = cr.desensitize_report(self.rich_run())
        for code in ('redacted_raw_text', 'redacted_actor', 'redacted_session',
                     'redacted_event_ids', 'redacted_label_details'):
            self.assertIn(code, view['reason_codes'])
        self.assertTrue(view['redaction']['applied'])
        self.assertTrue(view['redaction']['pii_scan_passed'])
        self.assertIn('events', view['redaction']['removed_fields'])

    def test_budget_truncated_reason_code(self):
        events = [msg('e%d' % i, OUTSIDE_VOCAB, 1000 + i) for i in range(6)]
        view = cr.desensitize_report(self.run_replay(events, {}, max_events=2))
        self.assertIn('budget_truncated', view['reason_codes'])
        self.assertTrue(view['performance']['truncated'])

    def test_quarantine_and_orphan_reason_codes(self):
        events = [msg('c1', PRAISE, 1000)]
        labels = {'c1': label('c1', '不同原话', 1000), 'ghost': label('ghost', PRAISE, 1000)}
        view = cr.desensitize_report(self.run_replay(events, labels))
        self.assertIn('quarantined_labels', view['reason_codes'])
        self.assertIn('orphan_labels', view['reason_codes'])

    def test_checks_failed_reason_code(self):
        run = self.run_replay()
        run['checks']['ok'] = False
        view = cr.desensitize_report(run)
        self.assertIn('checks_failed', view['reason_codes'])

    def test_empty_input_reason_code(self):
        view = cr.desensitize_report(self.run_replay([], {}))
        self.assertIn('empty_input', view['reason_codes'])
        self.assertIn('no_candidate_labels', view['reason_codes'])

    def test_pure_and_input_unchanged(self):
        run = self.rich_run()
        snapshot = json.dumps(run, sort_keys=True, default=str)
        view = cr.desensitize_report(run)
        view['state']['valence'] = 0.9
        view['stats']['rule_counts'].clear()
        self.assertEqual(json.dumps(run, sort_keys=True, default=str), snapshot)

    def test_non_candidate_report_rejected(self):
        live = tr.stamp({'state': dyn.initial_state(1), 'expression_control': False}, tr.V2_LIVE)
        with self.assertRaises(ValueError):
            cr.desensitize_report(live)
        with self.assertRaises(ValueError):
            cr.desensitize_report(None)

    def test_expression_and_live_contract_on_view(self):
        view = cr.desensitize_report(self.rich_run())
        self.assertIs(view['expression_control'], False)
        self.assertFalse(view['counts_for_live'])
        self.assertEqual(view['trajectory'], tr.CANDIDATE_COUNTERFACTUAL)

    def test_valence_samples_style_report_desensitized(self):
        # 兼容 emotion_shadow.valence_samples 口径（rows 里带原话/候选明细）。
        report = tr.stamp({
            'mode': 'candidate_integrated_replay', 'expression_control': False,
            'state': dyn.initial_state(1000),
            'stats': {'total': 2, 'included': 1, 'excluded': 1},
            'rows': [
                {'event_id': 'evt_1', 'at': 1000, 'text': '谢谢你', 'actor_qq': QQ,
                 'session': 'private:' + QQ, 'included': True, 'exclusion_reason': '',
                 'appraisal': {'rule': 'integrated_praise', 'exclusion_reason': '', 'synthetic': False},
                 'candidate': {'stance': 'praise', 'state': 'done', 'text': '谢谢你',
                               'reason': '明确夸她', 'model': 'm'}},
                {'event_id': 'evt_2', 'at': 1001, 'text': '私密原话', 'included': False,
                 'exclusion_reason': 'source_mismatch',
                 'candidate': {'stance': 'rebuke', 'state': 'done', 'text': '私密原话',
                               'reason': 'x', 'model': 'm'}}],
        }, tr.CANDIDATE_COUNTERFACTUAL)
        view = cr.desensitize_report(report)
        encoded = json.dumps(view, ensure_ascii=False)
        for secret in ('谢谢你', '私密原话', QQ, 'private:' + QQ, 'evt_1', 'evt_2', '明确夸她'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(view['stats']['total'], 2)
        self.assertEqual(view['stats']['quarantined'], 1)
        self.assertEqual(view['stats']['stance_counts'], {'praise': 1, 'rebuke': 1})
        self.assertIn('quarantined_labels', view['reason_codes'])

    def test_string_values_scan_skips_hash_fields(self):
        acc = []
        cr._string_values({'parameter_hash': QQ, 'run_id': QQ, 'reason_codes': ['ok'],
                           'state': {'valence': 0.5}}, acc)
        self.assertNotIn(QQ, acc)
        self.assertIn('ok', acc)
        self.assertIn('reason_codes', acc)

    def test_scan_pii_collects_known_secrets(self):
        pii, flags = set(), {}
        cr._scan_pii({'payload': {'text': '私密原话'}, 'actor_qq': QQ,
                      'session': 'private:1', 'event_id': 'evt_1'}, pii, flags)
        self.assertIn('私密原话', pii)
        self.assertIn(QQ, pii)
        self.assertTrue(flags['text'])
        self.assertTrue(flags['actor'])
        self.assertTrue(flags['session'])
        self.assertTrue(flags['ids'])

    def test_read_desensitized_from_ledger(self):
        ledger = cr.new_ledger()
        run = cr.commit_run(ledger, self.rich_run())
        view = cr.read_desensitized(ledger, run['run_id'])
        self.assertNotIn(QQ, json.dumps(view, ensure_ascii=False))
        with self.assertRaises(ValueError):
            cr.read_desensitized(ledger, 'cand_missing')


class BridgeTests(Base):
    def seed(self, events=(), labels=()):
        persona = Path(self.root) / 'persona'
        persona.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(persona / 'cognition.db')) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,session TEXT,'
                      'actor_qq TEXT,topic_qq TEXT,source TEXT,visibility TEXT,payload TEXT,'
                      'dedupe TEXT,created REAL)')
            for e in events:
                c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (e['id'], e['kind'], e['ts'], e.get('session', ''), e.get('actor_qq', ''),
                           e.get('topic_qq', ''), e.get('source', 'onebot'), 'private',
                           json.dumps(e['payload'], ensure_ascii=False), e['id'], e['ts']))
        with closing(sqlite3.connect(persona / 'valence_candidates.db')) as c, c:
            c.execute('CREATE TABLE candidate(event_id TEXT PRIMARY KEY,at REAL,created REAL,'
                      'session TEXT,kind TEXT,text TEXT,address_basis TEXT,lexical TEXT,'
                      'stance TEXT,confidence REAL,reason TEXT,model TEXT,state TEXT,'
                      'attempts INTEGER,updated REAL)')
            for lab in labels:
                c.execute('INSERT INTO candidate VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (lab['event_id'], lab['at'], lab['at'], lab.get('session', ''),
                           lab['kind'], lab['text'], lab.get('address_basis', ''), 'none',
                           lab['stance'], lab['confidence'], lab.get('reason', ''),
                           lab.get('model', ''), lab['state'], 1, lab['at']))
        return persona

    def test_absent_ledgers_degrade_without_creating(self):
        inputs = cr.read_candidate_inputs()
        self.assertEqual(inputs['events'], [])
        self.assertEqual(inputs['labels'], {})
        self.assertEqual(inputs['degradation'],
                         [{'store': 'cognition', 'reason': 'absent'},
                          {'store': 'candidates', 'reason': 'absent'}])
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_reads_events_and_labels(self):
        event = msg('c1', OUTSIDE_VOCAB, 1000, address_basis='private')
        event['actor_qq'] = QQ
        event['session'] = 'private:' + QQ
        self.seed([event], [label('c1', OUTSIDE_VOCAB, 1000, session='private:' + QQ,
                                  address_basis='private')])
        inputs = cr.read_candidate_inputs()
        self.assertEqual([e['id'] for e in inputs['events']], ['c1'])
        self.assertEqual(inputs['events'][0]['payload']['text'], OUTSIDE_VOCAB)
        self.assertIn('c1', inputs['labels'])
        self.assertEqual(inputs['labels']['c1']['stance'], 'praise')
        self.assertEqual(inputs['degradation'], [])

    def test_reads_never_modify_bytes(self):
        event = msg('c1', PRAISE, 1000)
        persona = self.seed([event], [label('c1', PRAISE, 1000)])
        before = {p: p.read_bytes() for p in persona.iterdir()}
        cr.read_candidate_inputs()
        cr.desensitized_replay(start_at=START, end_at=END, ts=5.0)
        after = {p: p.read_bytes() for p in persona.iterdir()}
        self.assertEqual(before, after)

    def test_paths_resolve_at_call_time(self):
        self.seed([msg('c1', PRAISE, 1000)], [])
        self.assertEqual(len(cr.read_candidate_inputs()['events']), 1)
        with tempfile.TemporaryDirectory(prefix='s07_other_') as other:
            with patch.dict(os.environ, {'DATA_DIR': other, 'PERSONA_DATA_DIR': other}):
                self.assertEqual(cr.read_candidate_inputs()['events'], [])
        self.assertEqual(len(cr.read_candidate_inputs()['events']), 1)

    def test_desensitized_replay_end_to_end(self):
        event = msg('evt_bridge_1', OUTSIDE_VOCAB, 1000, address_basis='private')
        event['actor_qq'] = QQ
        event['session'] = 'private:' + QQ
        self.seed([event], [label('evt_bridge_1', OUTSIDE_VOCAB, 1000, session='private:' + QQ,
                                  address_basis='private', reason='主人私下夸她')])
        view = cr.desensitized_replay(start_at=START, end_at=END, ts=5.0)
        encoded = json.dumps(view, ensure_ascii=False)
        for secret in (OUTSIDE_VOCAB, QQ, 'private:' + QQ, 'evt_bridge_1', '主人私下夸她'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(view['stats']['total'], 1)
        self.assertGreater(view['state']['valence'], 0.0)
        self.assertIs(view['expression_control'], False)
        self.assertFalse(view['counts_for_live'])
        self.assertEqual([], [p for p in Path(self.root).rglob('*')
                              if p.is_file() and p.suffix not in ('.db',)])

    def test_desensitized_replay_degraded_reason_code(self):
        view = cr.desensitized_replay(start_at=START, end_at=END, ts=5.0)
        self.assertIn('input_degraded', view['reason_codes'])
        self.assertEqual(view['degradation_count'], 2)
        self.assertIn('empty_input', view['reason_codes'])

    def test_readonly_helper_structurally_rejects_writes(self):
        persona = self.seed([msg('c1', PRAISE, 1000)], [])
        for path in (persona / 'cognition.db', persona / 'valence_candidates.db'):
            with closing(cr._ro(path)) as c:
                with self.assertRaises(sqlite3.Error):
                    c.execute('CREATE TABLE s07_probe(x)')
                self.assertTrue(c.execute('SELECT name FROM sqlite_master').fetchall())

    def test_unreadable_degradation_reported(self):
        self.seed([msg('c1', PRAISE, 1000)], [])
        broken = MagicMock()
        broken.execute.side_effect = sqlite3.Error('boom')
        with patch.object(cr, '_ro', return_value=broken):
            inputs = cr.read_candidate_inputs()
        self.assertEqual(inputs['degradation'],
                         [{'store': 'cognition', 'reason': 'unreadable'},
                          {'store': 'candidates', 'reason': 'unreadable'}])
        self.assertEqual(inputs['events'], [])

    def test_corrupt_file_degrades_gracefully(self):
        persona = Path(self.root) / 'persona'
        persona.mkdir(parents=True)
        (persona / 'cognition.db').write_bytes(b'not a sqlite database at all')
        inputs = cr.read_candidate_inputs()
        reasons = {d['store']: d['reason'] for d in inputs['degradation']}
        self.assertIn(reasons.get('cognition'), ('absent', 'unreadable'))
        self.assertEqual(inputs['events'], [])


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, 'candidate_replay.py'), encoding='utf-8') as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_only_pure_or_readonly_modules_are_imported(self):
        allowed = {'copy', 'hashlib', 'json', 'math', 'os', 'sqlite3', 'time', 'pathlib',
                   'emotion_core', 'emotion_dynamics', 'emotion_trajectories'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or '').split('.')[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             '候选回放契约 import 了写入/网络/模型 API：%s' % sorted(names - allowed))

    def test_no_live_state_writer_modules_imported(self):
        banned = {'emotion_shadow', 'self_model', 'valence_candidates',
                  'qq_openclaw_bridge', 'llm_router', 'cognition_writer'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or '').split('.')[0]}
            else:
                continue
            self.assertEqual(set(), names & banned,
                             '候选回放契约 import 了实时写入模块：%s' % sorted(names & banned))

    def test_no_write_sql_anywhere_in_the_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ',
                             'ALTER ', 'REPLACE '):
                    self.assertNotIn(verb, upper,
                                     '候选回放契约里出现写入 SQL：%s' % node.value[:60])

    def test_no_write_or_file_calls(self):
        forbidden = {'commit', 'executescript', 'executemany', 'rollback',
                     'mkdir', 'makedirs', 'remove', 'unlink', 'rmtree', 'rename',
                     'write_text', 'write_bytes', 'tick', 'record_event',
                     'retract_evidence', 'enqueue', 'drain', 'apply_event',
                     'write_cognition_transaction', 'write_invalidation_transaction'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 '候选回放契约里出现写操作 %s' % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual('open', node.func.id, '契约不直接开文件')

    def test_the_connection_is_readonly(self):
        self.assertIn('mode=ro', self.source)
        self.assertIn('query_only', self.source)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ('getenv', 'join', 'abspath'),
                                     '模块级冻住了路径，换 DATA_DIR 的测试会读到别人的库')

    def test_path_resolution_lives_only_inside_the_read_bridge(self):
        bridge = {'_data_root', '_persona_root', '_cognition_path', '_candidates_path',
                  '_ro', 'read_candidate_inputs'}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name in bridge:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ('getenv', 'isfile'),
                                     '%s 里解析了路径；回放核心必须是纯函数' % node.name)

    def test_recovery_is_never_reimplemented(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, 'exp',
                                    '恢复公式必须来自冻结内核 advance，不许自带第二套动力学')
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotEqual(node.value, 'tau_seconds',
                                    '恢复时间尺度必须取自内核 PARAMETERS')

    def test_reuses_frozen_primitives(self):
        called = set()
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)):
                called.add((node.func.value.id, node.func.attr))
        for required in (('dynamics', 'advance'), ('dynamics', 'initial_state'),
                         ('dynamics', 'integrated_appraisal'), ('legacy', '_event_header'),
                         ('legacy', '_replay_key')):
            self.assertIn(required, called, '没有复用冻结原语 %s' % (required,))

    def test_expression_control_is_literal_false(self):
        values = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.keyword) and node.arg == 'expression_control':
                values.append(node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'expression_control':
                        values.append(value)
        self.assertTrue(values)
        for value in values:
            self.assertTrue(isinstance(value, ast.Constant) and value.value is False,
                            'expression_control 必须是字面量 False')

    def test_limitations_carve_out_scope(self):
        joined = ' '.join(cr.LIMITATIONS)
        for token in ('S06', 'S08', 'expression_control', 'counts_for_live',
                      'observe-only', '脱敏', '预算'):
            self.assertIn(token, joined)

    def test_no_wall_clock_in_performance_path(self):
        # time.time 只允许出现在 replay_candidates 的记录时间戳，不得进 _replay/性能。
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ('_replay', '_advance_to',
                                                                   'verify_run', 'desensitize_report'):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr == 'time'):
                        self.fail('%s 里出现了墙上时钟调用' % node.name)


if __name__ == '__main__':
    unittest.main()