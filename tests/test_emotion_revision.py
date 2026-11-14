# -*- coding: utf-8 -*-
"""S03 撤回、纠正和版本化情感重算的回归锁（路线 §4.3 / §9）。

钉死的行为：
  · 撤源重算：贡献移除后终态与「该事件从未存在」的重放逐位一致，
    冲击对账 0 误差；原事件仍在审计时间线，旧 run 原样保留；
  · 解释纠正：白名单证据标志修正 → 冻结规则重评替换旧评估，原事件保留、
    不连坐其他事件；不许改原话、不许注入任意 delta；
  · 版本链：parent/correction 缺失拒绝、只追加不覆盖、内容哈希防篡改、
    撤回单调（解释纠正洗不回已撤源）；
  · 确定性：同输入同状态、不同 tick 切分按经过时间一致、输入顺序无关、
    墙上时钟不影响状态；重复事件不重复计分；
  · 冻结：参数哈希与 S02 两把锁一致，v1 分支与 emotion_core.replay 逐位一致；
  · 审计：无因跳变/篡改时间线被抓；读函数纯读，计划不写账本；
  · 只读桥：缺库报 absent、绝不建库建表建目录、DATA_DIR 调用时解析。
AST 锁钉死旁路契约：无写入 SQL、无写文件调用、无顶层冻路径、不自带恢复公式。
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
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import emotion_core as legacy  # noqa: E402
import emotion_dynamics as core  # noqa: E402
import emotion_revision as rev  # noqa: E402
import invalidation_dag as dag  # noqa: E402
import self_model as sm  # noqa: E402

# 冻结口径（与 test_emotion_rule_freeze 同值）：变了必须显式评审。
LIVE_PARAMETER_HASH = 'fddcdc0ec1fa48fe54d6a83749af04133eb2397fc3f0cdddbcdde42ca7d08a01'
V1_PARAMETER_HASH = 'ce7b83161e4baa1e4f738d6f1cecb50dcf876350ec848586f643634bdb49f796'
DYNAMICS_HASH = '9272cdbc3b38376c4215a016fd39bb8bf693ed963115b94d40d39154b625bc89'
APPRAISAL_HASH = '06a984d264621d0f7fd9bfb709cc8340a89428ebe3b2899d3685139c8c140777'

PRAISE = '谢谢你帮我查清楚了'
REBUKE = '你刚才说错了'
START, END = 900.0, 5000.0


def msg(eid, text, ts, addressed=True, **extra):
    return {'id': eid, 'kind': 'message', 'ts': ts, 'source': 'onebot',
            'payload': {'text': text, 'addressed_to_self': addressed, **extra}}


def tool(eid, ts, ok):
    return {'id': eid, 'kind': 'tool_result', 'ts': ts, 'source': 'onebot',
            'payload': {'ok': ok}}


def standard_events():
    return [msg('p1', PRAISE, 1000), msg('n1', '今天天气不错', 2000, addressed=False),
            msg('r1', REBUKE, 3000), tool('t1', 4000, False)]


def fold(events, kernel, start=START, end=END):
    """用冻结 apply_event 原语独立折叠，作为重算状态的对照。"""
    state = kernel.initial_state(start)
    for event in sorted(events, key=lambda e: e['ts']):
        view = dict(event)
        if view['kind'] in ('group_message', 'private_message'):
            view['kind'] = 'message'
        state = kernel.apply_event(state, view)
    return kernel.advance(state, end)


def max_distance(a, b):
    return max(abs(a[k] - b[k]) for k in ('valence', 'arousal', 'control', 'attention'))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s03_", ignore_cleanup_errors=True)
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

    def ledger_with_base(self, events=None, kernel=rev.DEFAULT_KERNEL, tick=None):
        ledger = rev.new_ledger()
        base = rev.build_base_run(standard_events() if events is None else events,
                                  start_at=START, end_at=END, kernel=kernel,
                                  tick_seconds=tick, ts=5.0)
        rev.commit_base_run(ledger, base)
        return ledger, base

    def retract(self, ledger, parent_id, target, reason='主人撤回', ts=10.0):
        rec = dag.correction('observation_invalid', target, reason, ts=ts)
        plan = rev.plan_revision(ledger, parent_id, rec, ts=ts)
        return rec, rev.commit_revision(ledger, plan)


class BaseRunTests(Base):
    def test_base_state_matches_frozen_apply_event_fold(self):
        _, base = self.ledger_with_base()
        self.assertEqual(base['state'], fold(standard_events(), core))
        self.assertEqual(base['state']['processed'], 4)
        self.assertTrue(base['checks']['ok'])

    def test_v1_base_matches_frozen_core_replay(self):
        _, base = self.ledger_with_base(kernel='shadow-v1')
        reference = legacy.replay(standard_events(), start_at=START, end_at=END)
        self.assertEqual(base['state'], reference['state'])
        self.assertEqual(base['parameter_hash'], V1_PARAMETER_HASH)
        self.assertEqual(base['parameter_hash'], reference['parameter_hash'])

    def test_parameter_hashes_match_frozen_locks(self):
        _, v2 = self.ledger_with_base()
        self.assertEqual(v2['parameter_hash'], LIVE_PARAMETER_HASH)
        self.assertEqual(v2['dynamics_parameter_hash'], DYNAMICS_HASH)
        self.assertEqual(v2['appraisal_rule_hash'], APPRAISAL_HASH)
        _, v1 = self.ledger_with_base(kernel='shadow-v1')
        self.assertEqual(v1['parameter_hash'], V1_PARAMETER_HASH)
        self.assertIsNone(v1['dynamics_parameter_hash'])
        self.assertIsNone(v1['appraisal_rule_hash'])

    def test_duplicate_event_is_consumed_once(self):
        events = standard_events()
        events.append(msg('p1', PRAISE, 4500))
        _, base = self.ledger_with_base(events)
        self.assertEqual(base['state']['processed'], 4)
        self.assertEqual(base['excluded'].get('duplicate_event'), 1)
        _, clean = self.ledger_with_base()
        self.assertEqual(base['state'], clean['state'])
        dup = [r for r in base['timeline'] if r['rule'] == 'duplicate_event']
        self.assertEqual(len(dup), 1)
        self.assertFalse(dup[0]['consumed'])
        self.assertEqual(dup[0]['before'], dup[0]['after'])

    def test_build_is_deterministic(self):
        one = rev.build_base_run(standard_events(), start_at=START, end_at=END, ts=5.0)
        two = rev.build_base_run(standard_events(), start_at=START, end_at=END, ts=5.0)
        self.assertEqual(one, two)
        self.assertTrue(one['checks']['deterministic_replay'])

    def test_tick_split_invariance(self):
        for kernel in ('shadow-v2', 'shadow-v1'):
            with self.subTest(kernel=kernel):
                _, plain = self.ledger_with_base(kernel=kernel)
                _, ticked = self.ledger_with_base(kernel=kernel, tick=60)
                _, odd = self.ledger_with_base(kernel=kernel, tick=7)
                self.assertLessEqual(max_distance(plain['state'], ticked['state']), 1e-10)
                self.assertLessEqual(max_distance(plain['state'], odd['state']), 1e-10)
                self.assertTrue(ticked['checks']['ok'])

    def test_window_and_unknown_time_exclusions(self):
        events = standard_events()
        events.append(msg('bad_time', PRAISE, 0))
        events.append(msg('too_late', PRAISE, 6000))
        late = msg('late_eff', PRAISE, 1000)
        late['effective_ts'] = 5500
        events.append(late)
        _, base = self.ledger_with_base(events)
        self.assertEqual(base['excluded'].get('unknown_timestamp'), 1)
        self.assertEqual(base['excluded'].get('outside_window'), 2)
        _, clean = self.ledger_with_base()
        self.assertEqual(base['state'], clean['state'])
        for row in base['timeline']:
            if row['event_id'] in ('bad_time', 'too_late', 'late_eff'):
                self.assertFalse(row['consumed'])
                self.assertEqual(row['before'], row['after'])

    def test_empty_events_recover_to_baseline(self):
        _, base = self.ledger_with_base([])
        expected = core.advance(core.initial_state(START), END)
        self.assertEqual(base['state'], expected)
        self.assertEqual(base['state']['processed'], 0)
        self.assertTrue(base['checks']['ok'])

    def test_forged_recorded_appraisal_is_rejected(self):
        events = standard_events()
        forged = core.appraise(events[0])
        forged['delta']['valence'] += 0.1
        events[0]['appraisal'] = forged
        with self.assertRaises(ValueError):
            rev.build_base_run(events, start_at=START, end_at=END)

    def test_matching_recorded_appraisal_is_accepted(self):
        events = standard_events()
        events[0]['appraisal'] = core.appraise(events[0])
        _, base = self.ledger_with_base(events)
        _, plain = self.ledger_with_base()
        self.assertEqual(base['state'], plain['state'])

    def test_build_creates_no_files(self):
        rev.build_base_run(standard_events(), start_at=START, end_at=END)
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_shadow_boundary_stamps(self):
        _, v2 = self.ledger_with_base()
        self.assertEqual(v2['trajectory'], 'historical_replay')
        self.assertEqual(v2['kernel_version'], 'shadow-v2')
        self.assertFalse(v2['counts_for_live'])
        self.assertFalse(v2['expression_control'])
        _, v1 = self.ledger_with_base(kernel='shadow-v1')
        self.assertEqual(v1['trajectory'], 'v1_reference')
        self.assertFalse(v1['counts_for_live'])
        self.assertFalse(v1['expression_control'])
        self.assertTrue(v2['limitations'])
        self.assertFalse(v2['provenance']['llm_reinterpretation'])


class RetractionTests(Base):
    def test_retracted_state_equals_replay_without_event(self):
        for kernel in ('shadow-v2', 'shadow-v1'):
            with self.subTest(kernel=kernel):
                ledger, base = self.ledger_with_base(kernel=kernel)
                _, revision = self.retract(ledger, base['revision_id'], 'p1')
                without = rev.build_base_run(
                    [e for e in standard_events() if e['id'] != 'p1'],
                    start_at=START, end_at=END, kernel=kernel)
                self.assertEqual(revision['state'], without['state'])

    def test_retraction_delta_accounting_zero_error(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.retract(ledger, base['revision_id'], 'p1')
        self.assertEqual(revision['checks']['impulse_accounting_error'], 0.0)
        self.assertTrue(revision['checks']['no_unexplained_jump'])
        self.assertTrue(revision['checks']['ok'])
        for key in rev.DIMENSIONS:
            self.assertAlmostEqual(revision['delta'][key],
                                   revision['state'][key] - base['state'][key], delta=0.0)

    def test_old_revision_is_preserved(self):
        ledger, base = self.ledger_with_base()
        snapshot = rev.get_revision(ledger, base['revision_id'])
        _, revision = self.retract(ledger, base['revision_id'], 'p1')
        self.assertEqual(rev.get_revision(ledger, base['revision_id']), snapshot)
        self.assertEqual(ledger['order'], [base['revision_id'], revision['revision_id']])
        self.assertEqual(revision['parent_revision_id'], base['revision_id'])
        self.assertEqual(revision['history'], 'preserved')
        self.assertNotEqual(revision['revision_id'], base['revision_id'])

    def test_retracted_event_stays_auditable(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.retract(ledger, base['revision_id'], 'p1')
        row = next(r for r in revision['timeline'] if r['event_id'] == 'p1')
        self.assertEqual(row['rule'], 'retracted_source')
        self.assertEqual(row['revision_action'], 'retracted')
        self.assertFalse(row['consumed'])
        self.assertEqual(row['before'], row['after'])
        self.assertEqual(row['delta'], dict.fromkeys(rev.DIMENSIONS, 0.0))
        stored = next(e for e in rev.get_revision(ledger, base['revision_id'])['events']
                      if e['id'] == 'p1')
        self.assertEqual(stored['payload']['text'], PRAISE)

    def test_retraction_is_monotonic_across_chain(self):
        ledger, base = self.ledger_with_base()
        _, first = self.retract(ledger, base['revision_id'], 'p1')
        _, second = self.retract(ledger, first['revision_id'], 'r1', reason='也撤回')
        row = next(r for r in second['timeline'] if r['event_id'] == 'p1')
        self.assertEqual(row['rule'], 'retracted_source')
        without = rev.build_base_run(
            [e for e in standard_events() if e['id'] not in ('p1', 'r1')],
            start_at=START, end_at=END)
        self.assertEqual(second['state'], without['state'])

    def test_retract_all_returns_baseline(self):
        ledger, base = self.ledger_with_base()
        parent = base['revision_id']
        for eid in ('p1', 'n1', 'r1', 't1'):
            _, revision = self.retract(ledger, parent, eid)
            parent = revision['revision_id']
        expected = core.advance(core.initial_state(START), END)
        self.assertEqual(revision['state'], expected)
        self.assertEqual(revision['state']['processed'], 0)

    def test_retracting_zero_contribution_event_changes_nothing(self):
        events = standard_events() + [msg('e0', '', 1500)]
        ledger, base = self.ledger_with_base(events)
        _, revision = self.retract(ledger, base['revision_id'], 'e0')
        self.assertEqual(revision['delta'], dict.fromkeys(rev.DIMENSIONS, 0.0))
        for key in rev.DIMENSIONS:
            self.assertEqual(revision['state'][key], base['state'][key])


class InterpretationTests(Base):
    def correct(self, ledger, parent_id, target='r1', overrides=None, ts=10.0):
        rec = dag.correction('interpretation_revised', target, '那是引用，不是骂她', ts=ts)
        plan = rev.plan_revision(ledger, parent_id, rec,
                                 evidence_overrides={'quoted': True} if overrides is None else overrides,
                                 ts=ts)
        return rec, rev.commit_revision(ledger, plan)

    def test_correction_replaces_appraisal_and_preserves_event(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.correct(ledger, base['revision_id'])
        row = next(r for r in revision['timeline'] if r['event_id'] == 'r1')
        self.assertTrue(row['consumed'])
        self.assertEqual(row['revision_action'], 'replaced')
        self.assertEqual(row['rule'], 'message_attention')
        self.assertEqual(row['delta']['valence'], 0.0)
        self.assertEqual(row['original_appraisal']['rule'], 'direct_rebuke')
        self.assertIn('r1', revision['replayed_event_ids'])
        self.assertGreater(revision['state']['valence'], base['state']['valence'])
        self.assertTrue(revision['checks']['ok'])

    def test_original_text_cannot_be_overridden(self):
        ledger, base = self.ledger_with_base()
        with self.assertRaises(ValueError):
            self.correct(ledger, base['revision_id'], overrides={'text': PRAISE})

    def test_non_whitelist_override_rejected(self):
        ledger, base = self.ledger_with_base()
        for overrides in ({'delta': {'valence': 0.5}}, {'ok': True}, {'payload': {}}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.correct(ledger, base['revision_id'], overrides=overrides)

    def test_empty_overrides_rejected(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('interpretation_revised', 'r1', '那是引用', ts=1.0)
        for overrides in ({}, None):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    rev.plan_revision(ledger, base['revision_id'], rec,
                                      evidence_overrides=overrides)

    def test_corrected_appraisal_comes_from_frozen_rules(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.correct(ledger, base['revision_id'])
        row = next(r for r in revision['timeline'] if r['event_id'] == 'r1')
        corrected = msg('r1', REBUKE, 3000, quoted=True)
        self.assertEqual(row['delta'], core.appraise(corrected)['delta'])
        events = standard_events()
        events[2] = corrected
        without = rev.build_base_run(events, start_at=START, end_at=END)
        self.assertEqual(revision['state'], without['state'])

    def test_other_events_not_punished(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.correct(ledger, base['revision_id'])
        entries = {e['event_id']: e for e in revision['event_reconciliation']}
        self.assertTrue(entries['r1']['changed'])
        for eid in ('p1', 'n1', 't1'):
            self.assertFalse(entries[eid]['changed'])
            self.assertEqual(entries[eid]['parent_delta'], entries[eid]['revision_delta'])

    def test_overrides_rejected_for_retraction(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'r1', '撤回', ts=1.0)
        with self.assertRaises(ValueError):
            rev.plan_revision(ledger, base['revision_id'], rec,
                              evidence_overrides={'quoted': True})

    def test_addressed_override_also_works(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.correct(ledger, base['revision_id'],
                                   overrides={'addressed_to_self': False})
        row = next(r for r in revision['timeline'] if r['event_id'] == 'r1')
        self.assertEqual(row['rule'], 'message_attention')
        self.assertEqual(row['delta']['valence'], 0.0)
        self.assertEqual(row['delta']['arousal'], 0.0)


class VersionChainTests(Base):
    def test_missing_parent_rejected(self):
        ledger, _ = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        with self.assertRaises(ValueError):
            rev.plan_revision(ledger, 'emo_missing', rec)

    def test_missing_or_invalid_correction_rejected(self):
        ledger, base = self.ledger_with_base()
        bad = [None, {}, {'kind': 'observation_invalid', 'target_id': 'p1', 'reason': 'x'},
               {'kind': 'observation_invalid', 'correction_id': 'c1', 'target_id': 'p1'},
               {'kind': 'observation_invalid', 'correction_id': 'c1', 'reason': 'x'}]
        for correction in bad:
            with self.subTest(correction=correction):
                with self.assertRaises(ValueError):
                    rev.plan_revision(ledger, base['revision_id'], correction)

    def test_non_emotion_kinds_rejected(self):
        ledger, base = self.ledger_with_base()
        for rec in (dag.correction('belief_corrected', 'p1', '信念修订',
                                   new_revision_id='bel_2', ts=1.0),
                    dag.correction('scope_revoked', 'p1', '收紧范围', scope='group:55', ts=1.0)):
            with self.subTest(kind=rec['kind']):
                with self.assertRaises(ValueError):
                    rev.plan_revision(ledger, base['revision_id'], rec)

    def test_unknown_target_rejected(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'evt_not_recorded', '撤回', ts=1.0)
        with self.assertRaises(ValueError):
            rev.plan_revision(ledger, base['revision_id'], rec)

    def test_duplicate_correction_rejected(self):
        ledger, base = self.ledger_with_base()
        rec, first = self.retract(ledger, base['revision_id'], 'p1')
        with self.assertRaises(ValueError):
            rev.plan_revision(ledger, first['revision_id'], rec)

    def test_interpretation_after_retraction_rejected(self):
        ledger, base = self.ledger_with_base()
        _, first = self.retract(ledger, base['revision_id'], 'r1')
        rec = dag.correction('interpretation_revised', 'r1', '想重新解释', ts=2.0)
        with self.assertRaises(ValueError):
            rev.plan_revision(ledger, first['revision_id'], rec,
                              evidence_overrides={'quoted': True})

    def test_lineage_and_provenance_traceable(self):
        ledger, base = self.ledger_with_base()
        rec1, first = self.retract(ledger, base['revision_id'], 'p1')
        rec2 = dag.correction('interpretation_revised', 'r1', '引用', ts=2.0)
        second = rev.commit_revision(ledger, rev.plan_revision(
            ledger, first['revision_id'], rec2, evidence_overrides={'quoted': True}, ts=2.0))
        self.assertEqual(rev.revision_lineage(ledger, second['revision_id']),
                         [base['revision_id'], first['revision_id'], second['revision_id']])
        self.assertEqual(second['provenance']['correction_chain'],
                         [rec1['correction_id'], rec2['correction_id']])
        self.assertEqual(second['provenance']['base_revision_id'], base['revision_id'])
        self.assertEqual(second['correction_id'], rec2['correction_id'])
        self.assertTrue(second['replayed_event_ids'])
        self.assertEqual(second['base_revision_id'], base['revision_id'])

    def test_commit_is_append_only(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        plan = rev.plan_revision(ledger, base['revision_id'], rec, ts=1.0)
        rev.commit_revision(ledger, plan)
        with self.assertRaises(ValueError):
            rev.commit_revision(ledger, plan)
        self.assertEqual(len(ledger['order']), 2)

    def test_tampered_state_rejected_at_commit(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        plan = rev.plan_revision(ledger, base['revision_id'], rec, ts=1.0)
        plan['state']['valence'] += 0.1
        with self.assertRaises(ValueError):
            rev.commit_revision(ledger, plan)
        self.assertEqual(len(ledger['order']), 1)

    def test_plan_does_not_write_ledger(self):
        ledger, base = self.ledger_with_base()
        snapshot = json.dumps(ledger, sort_keys=True, default=str)
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        plan = rev.plan_revision(ledger, base['revision_id'], rec, ts=1.0)
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(json.dumps(ledger, sort_keys=True, default=str), snapshot)

    def test_chained_corrections_accumulate(self):
        ledger, base = self.ledger_with_base()
        _, first = self.retract(ledger, base['revision_id'], 'p1')
        rec = dag.correction('interpretation_revised', 'r1', '引用', ts=2.0)
        second = rev.commit_revision(ledger, rev.plan_revision(
            ledger, first['revision_id'], rec, evidence_overrides={'quoted': True}, ts=2.0))
        rows = {r['event_id']: r for r in second['timeline']}
        self.assertEqual(rows['p1']['rule'], 'retracted_source')
        self.assertEqual(rows['r1']['revision_action'], 'replaced')
        events = [e for e in standard_events() if e['id'] != 'p1']
        events[1] = msg('r1', REBUKE, 3000, quoted=True)
        without = rev.build_base_run(events, start_at=START, end_at=END)
        self.assertEqual(second['state'], without['state'])

    def test_retraction_wins_over_prior_interpretation(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('interpretation_revised', 'r1', '引用', ts=1.0)
        first = rev.commit_revision(ledger, rev.plan_revision(
            ledger, base['revision_id'], rec, evidence_overrides={'quoted': True}, ts=1.0))
        _, second = self.retract(ledger, first['revision_id'], 'r1', reason='整条撤回', ts=2.0)
        row = next(r for r in second['timeline'] if r['event_id'] == 'r1')
        self.assertEqual(row['rule'], 'retracted_source')
        without = rev.build_base_run(
            [e for e in standard_events() if e['id'] != 'r1'], start_at=START, end_at=END)
        self.assertEqual(second['state'], without['state'])


class DeterminismTests(Base):
    def test_same_inputs_same_revision(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        one = rev.plan_revision(ledger, base['revision_id'], rec, ts=7.0)
        two = rev.plan_revision(ledger, base['revision_id'], rec, ts=7.0)
        self.assertEqual(one, two)
        self.assertTrue(one['checks']['deterministic_replay'])

    def test_wall_clock_ts_does_not_change_state_or_id(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        one = rev.plan_revision(ledger, base['revision_id'], rec, ts=1.0)
        two = rev.plan_revision(ledger, base['revision_id'], rec, ts=999.0)
        self.assertEqual(one['revision_id'], two['revision_id'])
        self.assertEqual(one['state'], two['state'])
        self.assertEqual(one['timeline'], two['timeline'])

    def test_input_order_does_not_change_result(self):
        events = standard_events()
        shuffled = list(events)
        random.Random(42).shuffle(shuffled)
        one = rev.build_base_run(events, start_at=START, end_at=END, ts=5.0)
        two = rev.build_base_run(shuffled, start_at=START, end_at=END, ts=5.0)
        self.assertEqual(one['state'], two['state'])
        self.assertEqual(one['timeline'], two['timeline'])

    def test_effective_time_ordering_no_backward_state(self):
        a = msg('A', PRAISE, 2000)
        b = msg('B', '嗯', 1000, addressed=False)
        b['effective_ts'], b['source_seq'] = 3000, 0
        c = msg('C', '哦', 500, addressed=False)
        c['effective_ts'], c['source_seq'] = 2500, 1
        base = rev.build_base_run([b, c, a], start_at=400, end_at=4000, ts=5.0)
        self.assertEqual([r['event_id'] for r in base['timeline']], ['A', 'C', 'B'])
        times = [r['at'] for r in base['timeline']]
        self.assertEqual(times, sorted(times))
        self.assertEqual(base['state']['at'], 4000.0)

    def test_tick_invariance_for_revision(self):
        ledger, base = self.ledger_with_base()
        rec = dag.correction('observation_invalid', 'p1', '撤回', ts=1.0)
        one = rev.plan_revision(ledger, base['revision_id'], rec, tick_seconds=30, ts=7.0)
        two = rev.plan_revision(ledger, base['revision_id'], rec, tick_seconds=60, ts=7.0)
        self.assertLessEqual(max_distance(one['state'], two['state']), 1e-10)
        self.assertTrue(one['checks']['ok'])
        self.assertTrue(two['checks']['ok'])


class AuditTests(Base):
    def revision(self):
        ledger, base = self.ledger_with_base()
        _, revision = self.retract(ledger, base['revision_id'], 'p1')
        return ledger, base, revision

    def test_verify_passes_on_clean_revision(self):
        _, _, revision = self.revision()
        checked = rev.verify_revision(revision)
        self.assertTrue(checked['ok'])
        self.assertEqual(checked['impulse_accounting_error'], 0.0)
        self.assertLessEqual(checked['recovery_continuity_error'], 1e-10)
        self.assertTrue(checked['processed_matches'])

    def test_tampered_after_state_detected(self):
        _, _, revision = self.revision()
        tampered = copy.deepcopy(revision)
        row = next(r for r in tampered['timeline'] if r['consumed'])
        row['after']['valence'] += 0.05
        checked = rev.verify_revision(tampered)
        self.assertFalse(checked['ok'])
        self.assertGreater(checked['impulse_accounting_error'], 0.0)

    def test_tampered_delta_detected(self):
        _, _, revision = self.revision()
        tampered = copy.deepcopy(revision)
        row = next(r for r in tampered['timeline'] if r['consumed'])
        row['delta']['valence'] += 0.01
        self.assertFalse(rev.verify_revision(tampered)['ok'])

    def test_unexplained_jump_detected(self):
        _, _, revision = self.revision()
        tampered = copy.deepcopy(revision)
        consumed = [r for r in tampered['timeline'] if r['consumed']]
        consumed[1]['before']['valence'] += 0.2
        checked = rev.verify_revision(tampered)
        self.assertFalse(checked['ok'])
        self.assertFalse(checked['no_unexplained_jump'])

    def test_excluded_row_cannot_carry_delta(self):
        _, _, revision = self.revision()
        tampered = copy.deepcopy(revision)
        row = next(r for r in tampered['timeline'] if r['rule'] == 'retracted_source')
        row['delta']['valence'] = 0.01
        self.assertFalse(rev.verify_revision(tampered)['ok'])

    def test_explain_answers_why(self):
        ledger, base, revision = self.revision()
        explanation = rev.explain_revision(ledger, revision['revision_id'])
        self.assertEqual(explanation['correction_id'], revision['correction_id'])
        self.assertEqual(explanation['reasons'], ['主人撤回'])
        self.assertEqual(explanation['final_delta'], revision['delta'])
        changed = {e['event_id'] for e in explanation['changed_events']}
        self.assertIn('p1', changed)
        self.assertNotIn('t1', changed)
        self.assertEqual(explanation['parent_revision_id'], base['revision_id'])

    def test_read_functions_return_copies_and_never_write(self):
        ledger, base, revision = self.revision()
        snapshot = json.dumps(ledger, sort_keys=True, default=str)
        got = rev.get_revision(ledger, revision['revision_id'])
        got['state']['valence'] = 0.9
        got['timeline'].clear()
        rev.revision_lineage(ledger, revision['revision_id'])
        rev.explain_revision(ledger, revision['revision_id'])
        rev.verify_revision(rev.get_revision(ledger, revision['revision_id']))
        self.assertEqual(json.dumps(ledger, sort_keys=True, default=str), snapshot)
        self.assertIsNone(rev.get_revision(ledger, 'emo_missing'))


class BridgeTests(Base):
    def seed_event(self, dedupe, text=PRAISE, kind='private_message', ts=1000.0):
        return sm.record_event(kind, ts=ts, session='private:1001', actor_qq='1001',
                               payload={'text': text, 'addressed_to_self': True},
                               dedupe=dedupe)

    def test_absent_ledger_degrades_without_creating(self):
        inputs = rev.read_revision_inputs()
        self.assertEqual(inputs['events'], [])
        self.assertEqual(inputs['degradation'], [{'store': 'cognition', 'reason': 'absent'}])
        self.assertEqual([], list(Path(self.root).iterdir()))

    def test_reads_events_corrections_retractions(self):
        evt1 = self.seed_event('s03:b1')
        evt2 = self.seed_event('s03:b2', text='今天天气不错', ts=2000.0)
        sm.retract_evidence(evt1, '主人撤回')
        rec = dag.correction('observation_invalid', evt2, '误读', ts=1.0)
        sm.write_invalidation_transaction({
            'ts': 1.0, 'expected_epoch': 0, 'edges': [], 'corrections': [rec],
            'propagation': dag.propagate([], [rec], epoch=0, now=1.0)})
        inputs = rev.read_revision_inputs()
        self.assertEqual([e['id'] for e in inputs['events']], [evt1, evt2])
        self.assertEqual(inputs['events'][0]['payload']['text'], PRAISE)
        self.assertEqual(inputs['retractions'][0]['event_id'], evt1)
        self.assertEqual(inputs['retractions'][0]['reason'], '主人撤回')
        self.assertEqual(inputs['corrections'][0]['correction_id'], rec['correction_id'])
        self.assertEqual(inputs['degradation'], [])

    def test_reads_never_modify_the_ledger(self):
        evt = self.seed_event('s03:b3')
        sm.retract_evidence(evt, '主人撤回')
        path = Path(self.root) / 'persona' / 'cognition.db'
        before = path.read_bytes()
        rev.read_revision_inputs()
        rev.read_revision_inputs()
        self.assertEqual(before, path.read_bytes())

    def test_paths_resolve_at_call_time(self):
        self.seed_event('s03:b4')
        self.assertEqual(len(rev.read_revision_inputs()['events']), 1)
        with tempfile.TemporaryDirectory(prefix='s03_other_') as other:
            with patch.dict(os.environ, {'DATA_DIR': other, 'PERSONA_DATA_DIR': other}):
                self.assertEqual(rev.read_revision_inputs()['events'], [])
        self.assertEqual(len(rev.read_revision_inputs()['events']), 1)

    def test_retraction_corrections_stable_ids(self):
        rows = [{'event_id': 'evt_x', 'reason': '主人撤回', 'ts': 3.0}]
        one = rev.retraction_corrections(rows)
        two = rev.retraction_corrections(rows)
        self.assertEqual(one, two)
        self.assertEqual(one[0]['kind'], 'observation_invalid')
        self.assertEqual(one[0]['target_id'], 'evt_x')
        self.assertEqual(one[0]['reason'], '主人撤回')

    def test_end_to_end_retraction_from_bridge(self):
        evt1 = self.seed_event('s03:e1')
        self.seed_event('s03:e2', text='今天天气不错', ts=2000.0)
        sm.retract_evidence(evt1, '主人撤回')
        inputs = rev.read_revision_inputs()
        ledger = rev.new_ledger()
        base = rev.build_base_run(inputs['events'], start_at=START, end_at=3000, ts=5.0)
        rev.commit_base_run(ledger, base)
        correction = rev.retraction_corrections(inputs['retractions'])[0]
        revision = rev.commit_revision(ledger, rev.plan_revision(
            ledger, base['revision_id'], correction, ts=10.0))
        without = rev.build_base_run(
            [e for e in inputs['events'] if e['id'] != evt1],
            start_at=START, end_at=3000, ts=5.0)
        self.assertEqual(revision['state'], without['state'])
        self.assertTrue(revision['checks']['ok'])
        self.assertTrue(sm.get_event(evt1), '撤回删除了原始观察')

    def test_group_private_kinds_are_appraised_as_messages(self):
        self.seed_event('s03:g1', kind='group_message')
        inputs = rev.read_revision_inputs()
        base = rev.build_base_run(inputs['events'], start_at=START, end_at=3000, ts=5.0)
        row = base['timeline'][0]
        self.assertEqual(row['kind'], 'group_message')
        self.assertEqual(row['rule'], 'direct_affection')
        self.assertTrue(row['consumed'])


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, 'emotion_revision.py'), encoding='utf-8') as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_only_pure_or_readonly_modules_are_imported(self):
        allowed = {'copy', 'hashlib', 'json', 'math', 'os', 'sqlite3', 'time', 'pathlib',
                   'emotion_core', 'emotion_dynamics', 'emotion_trajectories', 'invalidation_dag'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or '').split('.')[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             '情感修订契约 import 了写入/网络/模型 API：%s' % sorted(names - allowed))

    def test_no_write_sql_anywhere_in_the_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ',
                             'ALTER ', 'REPLACE '):
                    self.assertNotIn(verb, upper,
                                     '情感修订契约里出现写入 SQL：%s' % node.value[:60])

    def test_no_write_or_file_calls(self):
        forbidden = {'commit', 'executescript', 'executemany', 'rollback',
                     'mkdir', 'makedirs', 'remove', 'unlink', 'rmtree', 'rename',
                     'write_text', 'write_bytes', 'tick', 'record_event',
                     'retract_evidence', 'write_cognition_transaction',
                     'write_invalidation_transaction'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 '情感修订契约里出现写操作 %s' % node.func.attr)
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
        bridge = {'_data_root', '_persona_root', '_cognition_path', '_ro', 'read_revision_inputs'}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name in bridge:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ('getenv', 'isfile'),
                                     '%s 里解析了路径；重算核心必须是纯函数' % node.name)

    def test_recovery_is_never_reimplemented(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, 'exp',
                                    '恢复公式必须来自冻结内核 advance，不许自带第二套动力学')
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotEqual(node.value, 'tau_seconds',
                                    '恢复时间尺度必须取自内核 PARAMETERS')

    def test_correction_kinds_and_override_whitelist_are_fixed(self):
        self.assertEqual(rev.EMOTION_CORRECTION_KINDS,
                         ('observation_invalid', 'interpretation_revised'))
        self.assertNotIn('text', rev.EVIDENCE_OVERRIDE_KEYS)
        self.assertNotIn('delta', rev.EVIDENCE_OVERRIDE_KEYS)
        self.assertIn('quoted', rev.EVIDENCE_OVERRIDE_KEYS)
        self.assertIn('addressed_to_self', rev.EVIDENCE_OVERRIDE_KEYS)


if __name__ == '__main__':
    unittest.main()