# -*- coding: utf-8 -*-
"""S08 回忆影响情感的隔离实验：可执行契约 + 人工评审包的回归锁（路线 §4.3 / §9）。

钉死的行为（全部对真实冻结契约做端到端，不打桩断言）：
  · 回忆事件化：R06 场景 → tool_result(ok=True)「检索成功」，绝不是新亲历消息；
    来源引用（object_id/scene_id/verdict_refs/store/epistemic_kind）逐字深拷贝；
    同一记忆同 ID，内核去重「同一事件只更新一次」；invalidated 绝不事件化；
  · 计划：now 必填、experiment_id 内容寻址、两臂共享冻结参数与初始状态、篡改拒绝；
  · 重放：两臂各跑一遍 emotion_revision 真实重放，确定性重跑逐位一致，
    max_error 口径对齐 S06（冲击对账 0 误差、恢复连续性 ≤1e-10）；
  · 差异：逐 tick diff + 归因（哪些维度/哪条回忆/来源引用），稳定排序；
    无差异如实 no_effect；浮点尾差不报成效应；回忆不直接驱动效价；
  · 撤回：S03 observation_invalid 撤源后零冲击、不再驱动差异，全撤回到对照态；
  · 评审包：形状稳定、JSON 可序列化、organic=not_observed、authorization=manual_review、
    限制声明如实（只证可执行，不证主观体验，不自称已证明回忆影响情感）；
  · 影子边界：expression_control 恒 False、counts_for_live 不计在线。
AST 锁钉死旁路契约：只 import 冻结原语+stdlib、无第二套动力学/顺序键、无写入、
不冻路径、不调 LLM/网络、不写生产 emotion_shadow 账本。
全部用隔离临时 DATA_DIR 与合成夹具，不读不写真实 data/，不发网络、不触发 QQ 发送。
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
import emotion_trajectories as traj      # noqa: E402
import invalidation_dag as dag           # noqa: E402
import recall_emotion as s08             # noqa: E402
import ripple_conflicts as rc            # noqa: E402
import ripple_evidence as re6            # noqa: E402

NOW, RECALL_AT = 6000.0, 4500.0
START, END = 900.0, 5000.0
V1, V2 = 'shadow-v1', 'shadow-v2'
PRAISE = '谢谢你帮我查清楚了'


def msg(eid, text, ts, addressed=True, **extra):
    return {'id': eid, 'kind': 'message', 'ts': ts, 'source': 'onebot',
            'payload': {'text': text, 'addressed_to_self': addressed, **extra}}


def tool(eid, ts, ok):
    return {'id': eid, 'kind': 'tool_result', 'ts': ts, 'source': 'onebot',
            'payload': {'ok': ok}}


def base_events():
    return [msg('p1', PRAISE, 1000), msg('n1', '今天天气不错', 2000, addressed=False),
            msg('r1', '你刚才说错了', 3000), tool('t1', 4000, False)]


def entry(oid, **over):
    """手工 R06 场景条目夹具（合成，不是自然样本）。"""
    row = {'object_id': oid, 'object_kind': 'belief', 'store': 'cognition',
           'epistemic_kind': 'reported', 'subject_ref': 'per_1',
           'root_source_ids': ['e1'], 'root_representative': 'e1',
           'occurred_at': 1000.0, 'validity': 'valid',
           'restatement': '合成记忆 ' + oid, 'missing_slots': [], 'verdict_refs': []}
    row.update(over)
    return row


def hand_rendered(entries, status='sufficient', scene_id='scene_hand', scene_key='e1'):
    return {'status': status, 'scenes': [
        {'scene_id': scene_id, 'scene_key': scene_key, 'root_representative': scene_key,
         'root_source_ids': [scene_key], 'entries': entries, 'entry_count': len(entries)}]}


def claim_item(oid, value, sources, **over):
    row = {'object_id': oid, 'object_kind': 'belief', 'store': 'cognition',
           'root_source_ids': list(sources), 'validity': 'valid',
           'subject_ref': 'per_1', 'restatement': value, 'occurred_at': 1000.0,
           'epistemic_kind': 'reported',
           'evidence_spans': [{'event_id': s, 'quote': value, 'subject_ref': 'per_1'}
                              for s in sources]}
    row.update(over)
    return row


def rendered_fixture():
    """真实 R05→R06 管线产出的证据场景（测试可 import 上游，被测模块不 import）。"""
    s = claim_item('m1', '主人说谢谢你帮我查清楚了', ['e1'])
    n = claim_item('m2', '主人喜欢茶', ['e2'])
    edge = {'edge_id': 'edge_m1_m2', 'type': 'shared_entity', 'a': 'm1',
            'a_kind': 'belief', 'b': 'm2', 'b_kind': 'belief',
            'basis': {'reason': 'shared_entity'}}
    resolved = rc.resolve_ripple([s], [edge], items=[s, n])
    return re6.render_evidence(resolved, items=[s, n], seeds=[s])


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def max_distance(a, b):
    return max(abs(a[k] - b[k]) for k in s08.DIMENSIONS)


class Base(unittest.TestCase):
    """隔离夹具：每个用例独立临时 DATA_DIR，路径调用时解析，不碰真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s08_', ignore_cleanup_errors=True)
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

    def plan(self, kernel=V2, events=None, rendered=None, **kw):
        args = {'now': NOW, 'recall_at': RECALL_AT, 'start_at': START, 'end_at': END,
                'kernel': kernel}
        args.update(kw)
        return s08.plan_experiment(base_events() if events is None else events,
                                   rendered_fixture() if rendered is None else rendered,
                                   **args)

    def package(self, kernel=V2, events=None, rendered=None, **kw):
        plan = self.plan(kernel=kernel, events=events, rendered=rendered, **kw)
        return s08.review_package(plan, s08.run_experiment(plan))

    def nothing_created(self):
        return not list(Path(self.root).iterdir())


# ── 回忆事件化：检索成功形状、来源引用、去重、失效跳过 ────────────────────


class RecallEventTests(Base):
    def test_events_are_retrieval_success_not_messages(self):
        bundle = s08.recall_events(rendered_fixture(), recall_at=RECALL_AT)
        self.assertEqual(2, len(bundle['events']))
        for event in bundle['events']:
            self.assertEqual('tool_result', event['kind'])
            self.assertIs(True, event['payload']['ok'])
            self.assertNotEqual('message', event['kind'])
            self.assertEqual(RECALL_AT, event['ts'])

    def test_event_id_is_content_addressed_by_object(self):
        bundle = s08.recall_events(hand_rendered([entry('m1'), entry('m2')]),
                                   recall_at=RECALL_AT)
        ids = [e['id'] for e in bundle['events']]
        self.assertEqual(['recall_' + s08._sha('m1'), 'recall_' + s08._sha('m2')], ids)
        again = s08.recall_events(hand_rendered([entry('m1')]), recall_at=RECALL_AT + 10)
        self.assertEqual(ids[0], again['events'][0]['id'])

    def test_payload_carries_full_source_refs(self):
        refs = [{'left_id': 'm1', 'right_id': 'm9', 'verdict': 'disputed',
                 'reason_code': 'subject_mismatch'}]
        bundle = s08.recall_events(hand_rendered([entry('m1', verdict_refs=refs)]),
                                   recall_at=RECALL_AT)
        payload = bundle['events'][0]['payload']
        for field in ('object_id', 'scene_id', 'store', 'epistemic_kind', 'subject_ref',
                      'root_source_ids', 'root_representative', 'restatement',
                      'occurred_at', 'verdict_refs', 'missing_slots', 'recall_layer'):
            self.assertIn(field, payload)
        self.assertEqual('m1', payload['object_id'])
        self.assertEqual('scene_hand', payload['scene_id'])
        self.assertEqual(refs, payload['verdict_refs'])
        self.assertEqual(refs, bundle['index'][ids0(bundle)]['verdict_refs'])

    def test_verdict_refs_are_verbatim_deep_copies(self):
        refs = [{'left_id': 'm1', 'right_id': 'm9', 'verdict': 'disputed'}]
        rendered = hand_rendered([entry('m1', verdict_refs=refs)])
        bundle = s08.recall_events(rendered, recall_at=RECALL_AT)
        bundle['events'][0]['payload']['verdict_refs'][0]['verdict'] = 'tampered'
        self.assertEqual('disputed', refs[0]['verdict'])
        self.assertEqual('disputed',
                         bundle['index'][ids0(bundle)]['verdict_refs'][0]['verdict'])

    def test_recall_layer_is_looked_up_and_object_kind_preserved(self):
        bundle = s08.recall_events(hand_rendered([entry('m1', epistemic_kind='experienced')]),
                                   recall_at=RECALL_AT)
        payload = bundle['events'][0]['payload']
        self.assertEqual('looked_up', payload['recall_layer'])
        # 来源对象自身的认识论层原样保留，绝不改写；检索行为本身永远是「查到」。
        self.assertEqual('experienced', payload['epistemic_kind'])

    def test_praise_text_in_memory_never_becomes_new_praise(self):
        rendered = hand_rendered([entry('m1', restatement=PRAISE,
                                      epistemic_kind='experienced')])
        plan = self.plan(rendered=rendered)
        result = s08.run_experiment(plan)
        self.assertNotIn('valence', result['diff']['changed_dimensions'])
        for source in result['diff']['sources']:
            self.assertNotIn('valence', source['dimensions'])
        for event in plan['recall_events']:
            self.assertNotIn('addressed_to_self', event['payload'])
            self.assertNotIn('text', event['payload'])

    def test_payload_is_whitelisted_no_message_flags(self):
        rendered = hand_rendered([entry('m1', addressed_to_self=True, text=PRAISE)])
        bundle = s08.recall_events(rendered, recall_at=RECALL_AT)
        payload = bundle['events'][0]['payload']
        self.assertNotIn('addressed_to_self', payload)
        self.assertNotIn('text', payload)

    def test_invalidated_entries_are_skipped_loudly(self):
        rendered = hand_rendered([entry('m1', validity='invalidated'), entry('m2')])
        bundle = s08.recall_events(rendered, recall_at=RECALL_AT)
        self.assertEqual(1, len(bundle['events']))
        self.assertEqual([{'object_id': 'm1', 'scene_id': 'scene_hand',
                           'reason': 'invalidated_memory'}], bundle['skipped'])

    def test_missing_object_id_raises(self):
        with self.assertRaises(ValueError):
            s08.recall_events(hand_rendered([{'restatement': '无 ID'}]), recall_at=RECALL_AT)

    def test_bad_rendered_input_raises(self):
        for bad in (None, [], {}, {'scenes': 'not-a-list'},
                    {'scenes': [{'entries': 'not-a-list'}]},
                    {'scenes': [{'entries': ['not-a-dict']}]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    s08.recall_events(bad, recall_at=RECALL_AT)

    def test_recall_at_must_be_positive_finite(self):
        rendered = hand_rendered([entry('m1')])
        for bad in (0, -1, float('nan'), float('inf'), 'x', True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    s08.recall_events(rendered, recall_at=bad)

    def test_legacy_or_empty_source_rejected(self):
        rendered = hand_rendered([entry('m1')])
        for bad in ('legacy', 'legacy_note', 'legacy:x', ''):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    s08.recall_events(rendered, recall_at=RECALL_AT, source=bad)

    def test_duplicate_object_across_scenes_gets_same_id(self):
        rendered = {'status': 'sufficient', 'scenes': [
            {'scene_id': 'sc_a', 'scene_key': 'e1', 'entries': [entry('m1')]},
            {'scene_id': 'sc_b', 'scene_key': 'e2', 'entries': [entry('m1')]}]}
        bundle = s08.recall_events(rendered, recall_at=RECALL_AT)
        self.assertEqual(2, len(bundle['events']))
        self.assertEqual(1, len(bundle['index']))
        self.assertEqual([bundle['events'][0]['id']], bundle['duplicate_ids'])

    def test_order_is_deterministic_regardless_of_input_order(self):
        rendered = {'status': 'sufficient', 'scenes': [
            {'scene_id': 'sc_b', 'scene_key': 'e2', 'entries': [entry('m2'), entry('m3')]},
            {'scene_id': 'sc_a', 'scene_key': 'e1', 'entries': [entry('m1')]}]}
        one = s08.recall_events(rendered, recall_at=RECALL_AT)
        shuffled = copy.deepcopy(rendered)
        random.Random(7).shuffle(shuffled['scenes'])
        for scene in shuffled['scenes']:
            random.Random(9).shuffle(scene['entries'])
        two = s08.recall_events(shuffled, recall_at=RECALL_AT)
        self.assertEqual(canonical(one), canonical(two))
        self.assertEqual(['m1', 'm2', 'm3'],
                         [e['payload']['object_id'] for e in one['events']])

    def test_input_not_mutated(self):
        rendered = rendered_fixture()
        before = canonical(rendered)
        s08.recall_events(rendered, recall_at=RECALL_AT)
        self.assertEqual(before, canonical(rendered))

    def test_empty_scenes_produce_no_events(self):
        bundle = s08.recall_events({'status': 'not_found', 'scenes': []},
                                   recall_at=RECALL_AT)
        self.assertEqual([], bundle['events'])
        self.assertEqual({}, bundle['index'])
        self.assertEqual('not_found', bundle['source_status'])

    def test_synthetic_source_is_allowed(self):
        bundle = s08.recall_events(hand_rendered([entry('m1')]), recall_at=RECALL_AT,
                                   source='synthetic')
        self.assertEqual('synthetic', bundle['events'][0]['source'])


def ids0(bundle):
    return bundle['events'][0]['id']


# ── 实验计划：now 必填、内容寻址、两臂共享冻结参数与初始状态 ──────────────


class PlanTests(Base):
    def test_now_is_required_and_validated(self):
        with self.assertRaises(TypeError):
            s08.plan_experiment(base_events(), rendered_fixture())
        for bad in (None, 'x', float('nan'), 0, -5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.plan(now=bad)

    def test_experiment_id_is_content_addressed(self):
        one = self.plan()
        self.assertTrue(one['experiment_id'].startswith('s08_'))
        self.assertEqual(one['experiment_id'], self.plan()['experiment_id'])
        self.assertNotEqual(one['experiment_id'], self.plan(now=NOW + 1)['experiment_id'])
        self.assertNotEqual(one['experiment_id'],
                            self.plan(events=base_events()[:-1])['experiment_id'])
        self.assertNotEqual(one['experiment_id'],
                            self.plan(rendered=hand_rendered([entry('m9')]))['experiment_id'])
        self.assertNotEqual(one['experiment_id'], self.plan(kernel=V1)['experiment_id'])

    def test_arms_share_frozen_parameters_and_initial_state(self):
        for kernel in (V1, V2):
            with self.subTest(kernel=kernel):
                plan = self.plan(kernel=kernel)
                module = rev._kernel(kernel)
                hashes = rev._kernel_hashes(module)
                for key, value in hashes.items():
                    self.assertEqual(value, plan[key])
                self.assertEqual(module.initial_state(START), plan['initial_state'])
                self.assertEqual({'control': 4, 'recall': 6}, plan['arms'])

    def test_default_window_covers_events_and_recall(self):
        plan = s08.plan_experiment(base_events(), rendered_fixture(), now=RECALL_AT)
        self.assertEqual(0.0, plan['start_at'])
        self.assertEqual(RECALL_AT, plan['end_at'])
        self.assertEqual(RECALL_AT, plan['recall_at'])

    def test_recall_at_defaults_to_now(self):
        plan = s08.plan_experiment(base_events(), rendered_fixture(), now=NOW,
                                   start_at=START, end_at=NOW + 100)
        self.assertEqual(NOW, plan['recall_at'])

    def test_recall_at_outside_window_raises(self):
        with self.assertRaises(ValueError):
            self.plan(end_at=4000.0)
        with self.assertRaises(ValueError):
            self.plan(start_at=4600.0)

    def test_unknown_kernel_raises(self):
        with self.assertRaises(ValueError):
            self.plan(kernel='no-such-kernel')

    def test_base_event_id_collision_raises(self):
        clash = msg('recall_' + s08._sha('m1'), PRAISE, 1000)
        with self.assertRaises(ValueError):
            self.plan(events=base_events() + [clash])

    def test_plan_deep_copies_inputs(self):
        events, rendered = base_events(), rendered_fixture()
        plan = self.plan(events=events, rendered=rendered)
        snapshot = canonical(plan)
        events[0]['payload']['text'] = 'tampered'
        rendered['scenes'].clear()
        self.assertEqual(snapshot, canonical(plan))

    def test_bad_base_events_raise_loudly(self):
        with self.assertRaises(ValueError):
            self.plan(events=[{'kind': 'message', 'ts': 1000}])

    def test_plan_records_tick_and_shadow_boundary(self):
        plan = self.plan(tick_seconds=30)
        self.assertEqual(30.0, plan['tick_seconds'])
        self.assertIs(False, plan['expression_control'])
        self.assertEqual(s08.ALGORITHM_VERSION, plan['algorithm_version'])

    def test_recall_skipped_and_status_surfaced(self):
        rendered = hand_rendered([entry('m1', validity='invalidated')], status='partial')
        plan = self.plan(rendered=rendered)
        self.assertEqual('partial', plan['recall_source_status'])
        self.assertEqual(1, len(plan['recall_skipped']))
        self.assertEqual([], plan['recall_events'])

    def test_empty_base_events_allowed(self):
        plan = self.plan(events=[])
        result = s08.run_experiment(plan)
        self.assertEqual(0, result['arms']['control']['state']['processed'])
        self.assertEqual(2, result['diff']['processed_diff'])
        self.assertEqual('difference_observed', result['diff']['effect'])


# ── 确定性重放与差异：两臂、归因、no_effect、撤回、tick 不变性 ────────────


class RunExperimentTests(Base):
    def test_v2_difference_observed_with_correct_dimensions(self):
        result = s08.run_experiment(self.plan())
        diff = result['diff']
        self.assertEqual('difference_observed', diff['effect'])
        self.assertEqual(['arousal', 'control', 'attention'], diff['changed_dimensions'])
        self.assertNotIn('valence', diff['changed_dimensions'])
        self.assertLessEqual(abs(diff['final_diff']['valence']), s08.EFFECT_EPSILON)

    def test_v1_difference_observed_control_attention_only(self):
        result = s08.run_experiment(self.plan(kernel=V1))
        self.assertEqual(['control', 'attention'], result['diff']['changed_dimensions'])
        self.assertLessEqual(abs(result['diff']['final_diff']['valence']), s08.EFFECT_EPSILON)

    def test_control_arm_equals_plain_base_replay(self):
        plan = self.plan()
        result = s08.run_experiment(plan)
        plain = rev.build_base_run(base_events(), start_at=START, end_at=END,
                                   kernel=V2, ts=NOW)
        self.assertEqual(plain['state'], result['arms']['control']['state'])
        self.assertEqual(plain['revision_id'], result['arms']['control']['revision_id'])

    def test_processed_diff_counts_consumed_recalls(self):
        result = s08.run_experiment(self.plan())
        self.assertEqual(2, result['diff']['processed_diff'])
        self.assertEqual(6, result['arms']['recall']['state']['processed'])
        self.assertEqual(4, result['arms']['control']['state']['processed'])

    def test_deterministic_rerun_identical(self):
        plan = self.plan()
        one = s08.run_experiment(plan)
        two = s08.run_experiment(copy.deepcopy(plan))
        self.assertEqual(canonical(one), canonical(two))

    def test_tampered_plan_rejected(self):
        plan = self.plan()
        plan['base_events'][0]['payload']['text'] = 'tampered'
        with self.assertRaises(ValueError):
            s08.run_experiment(plan)
        with self.assertRaises(ValueError):
            s08.run_experiment({})
        with self.assertRaises(ValueError):
            s08.run_experiment(None)

    def test_max_error_aligned_with_s06(self):
        for kernel in (V1, V2):
            with self.subTest(kernel=kernel):
                checks = s08.run_experiment(self.plan(kernel=kernel))['checks']
                self.assertEqual(0.0, checks['max_impulse_error'])
                self.assertLessEqual(checks['max_recovery_error'], 1e-10)
                self.assertTrue(checks['max_error_ok'])
                self.assertTrue(checks['arms_share_parameters'])
                self.assertTrue(checks['arms_share_initial_state'])
                self.assertTrue(checks['deterministic_replay'])
                self.assertTrue(checks['control_checks_ok'])
                self.assertTrue(checks['recall_checks_ok'])
                self.assertTrue(checks['trajectory_is_shadow'])

    def test_diff_rows_before_recall_are_unchanged(self):
        result = s08.run_experiment(self.plan())
        rows = result['diff']['rows']
        shared = [r for r in rows if r['control_present']]
        self.assertEqual(4, len(shared))
        for row in shared:
            self.assertLess(row['at'], RECALL_AT)
            self.assertFalse(row['changed'])
            self.assertTrue(all(v == 0.0 for v in row['diff'].values()))
        recall_rows = [r for r in rows if r['is_recall']]
        self.assertEqual(2, len(recall_rows))
        for row in recall_rows:
            self.assertFalse(row['control_present'])
            self.assertTrue(row['consumed'])
            self.assertIn('object_id', row['recall_ref'])
            self.assertIn('verdict_refs', row['recall_ref'])

    def test_sources_carry_refs_and_dimensions(self):
        diff = s08.run_experiment(self.plan())['diff']
        self.assertEqual(2, len(diff['sources']))
        self.assertEqual(sorted(s['event_id'] for s in diff['sources']),
                         [s['event_id'] for s in diff['sources']])
        for source in diff['sources']:
            self.assertIn(source['object_id'], ('m1', 'm2'))
            self.assertIsInstance(source['verdict_refs'], list)
            self.assertEqual(['arousal', 'control', 'attention'], source['dimensions'])
            self.assertEqual('tool_success', source['rule'])
        self.assertEqual(['m1', 'm2'], diff['by_dimension']['control']['source_object_ids'])
        self.assertEqual([], diff['by_dimension']['valence']['source_object_ids'])

    def test_no_effect_when_no_recall_input(self):
        plan = self.plan(rendered={'status': 'not_found', 'scenes': []})
        result = s08.run_experiment(plan)
        diff = result['diff']
        self.assertEqual('no_effect', diff['effect'])
        self.assertTrue(diff['state_identical'])
        self.assertEqual([], diff['sources'])
        self.assertEqual([], diff['changed_dimensions'])
        self.assertEqual(0, diff['processed_diff'])
        self.assertEqual(result['arms']['control']['revision_id'],
                         result['arms']['recall']['revision_id'])

    def test_no_effect_when_all_memories_invalidated(self):
        plan = self.plan(rendered=hand_rendered(
            [entry('m1', validity='invalidated'), entry('m2', validity='invalidated')]))
        result = s08.run_experiment(plan)
        self.assertEqual('no_effect', result['diff']['effect'])
        self.assertTrue(result['diff']['state_identical'])
        self.assertEqual(2, len(plan['recall_skipped']))

    def test_duplicate_recall_updates_once(self):
        rendered = {'status': 'sufficient', 'scenes': [
            {'scene_id': 'sc_a', 'scene_key': 'e1', 'entries': [entry('m1')]},
            {'scene_id': 'sc_b', 'scene_key': 'e2', 'entries': [entry('m1')]}]}
        result = s08.run_experiment(self.plan(rendered=rendered))
        self.assertEqual(1, result['diff']['processed_diff'])
        self.assertEqual(1, len(result['diff']['sources']))
        rules = [r['rule'] for r in result['diff']['rows'] if r['is_recall']]
        self.assertEqual(['tool_success', 'duplicate_event'], rules)

    def test_retract_all_recalls_returns_to_control(self):
        plan = self.plan()
        result = s08.run_experiment(plan, retractions=[
            {'target': 'm1', 'reason': '主人撤回'}, {'target': 'm2', 'reason': '主人撤回'}])
        diff = result['diff']
        self.assertEqual('no_effect', diff['effect'])
        self.assertTrue(diff['state_identical'])
        self.assertEqual([], diff['sources'])
        self.assertEqual(2, len(diff['retracted_recalls']))
        self.assertTrue(result['checks']['retracted_recalls_zero_impulse'])
        self.assertTrue(result['checks']['retracted_not_attributed'])
        self.assertTrue(result['checks']['recall_checks_ok'])
        self.assertEqual('revision', result['arms']['recall']['branch'])
        retracted_rows = [r for r in diff['rows']
                          if r['is_recall'] and r['rule'] == 'retracted_source']
        self.assertEqual(2, len(retracted_rows))
        for row in retracted_rows:
            self.assertFalse(row['consumed'])
            self.assertTrue(all(v == 0.0 for v in row['diff'].values()))

    def test_partial_retraction_removes_only_that_source(self):
        result = s08.run_experiment(self.plan(),
                                    retractions=[{'target': 'm1', 'reason': '主人撤回'}])
        diff = result['diff']
        self.assertEqual(['m2'], [s['object_id'] for s in diff['sources']])
        self.assertEqual([{'event_id': 'recall_' + s08._sha('m1'), 'object_id': 'm1'}],
                         diff['retracted_recalls'])
        self.assertEqual('difference_observed', diff['effect'])
        self.assertEqual(1, len(result['arms']['recall']['corrections']))
        self.assertEqual(1, len(result['retractions']))
        self.assertEqual('m1', result['retractions'][0]['object_id'])

    def test_retraction_accepts_object_id_and_event_id(self):
        by_object = s08.run_experiment(self.plan(),
                                       retractions=[{'target': 'm1', 'reason': '撤回'}])
        by_event = s08.run_experiment(self.plan(), retractions=[
            {'target': 'recall_' + s08._sha('m1'), 'reason': '撤回'}])
        self.assertEqual(by_object['arms']['recall']['state'],
                         by_event['arms']['recall']['state'])
        self.assertEqual(by_object['arms']['recall']['revision_id'],
                         by_event['arms']['recall']['revision_id'])

    def test_retraction_bad_inputs_raise(self):
        plan = self.plan()
        with self.assertRaises(ValueError):
            s08.run_experiment(plan, retractions=[{'target': 'p1', 'reason': '不是回忆'}])
        with self.assertRaises(ValueError):
            s08.run_experiment(plan, retractions=[{'target': 'm1', 'reason': ''}])
        with self.assertRaises(ValueError):
            s08.run_experiment(plan, retractions=['not-a-dict'])

    def test_retraction_chain_two_steps(self):
        result = s08.run_experiment(self.plan(), retractions=[
            {'target': 'm1', 'reason': '撤回1'}, {'target': 'm2', 'reason': '撤回2'}])
        recall = result['arms']['recall']
        self.assertEqual(2, len(recall['corrections']))
        self.assertEqual(result['arms']['control']['state'], recall['state'])
        self.assertEqual('no_effect', result['diff']['effect'])
        self.assertTrue(recall['checks']['ok'])
        self.assertEqual(2, len(result['retractions']))

    def test_tick_invariance(self):
        plain = s08.run_experiment(self.plan())
        ticked = s08.run_experiment(self.plan(tick_seconds=30))
        for arm in s08.ARMS:
            self.assertLessEqual(max_distance(plain['arms'][arm]['state'],
                                              ticked['arms'][arm]['state']), 1e-10)
        self.assertTrue(ticked['checks']['max_error_ok'])
        self.assertTrue(ticked['checks']['recall_checks_ok'])
        self.assertEqual(plain['diff']['changed_dimensions'],
                         ticked['diff']['changed_dimensions'])

    def test_shadow_boundary_stamped(self):
        for kernel, trajectory in ((V1, traj.V1_REFERENCE), (V2, traj.HISTORICAL_REPLAY)):
            with self.subTest(kernel=kernel):
                result = s08.run_experiment(self.plan(kernel=kernel))
                self.assertIs(False, result['expression_control'])
                self.assertIs(False, result['counts_for_live'])
                self.assertEqual(trajectory, result['trajectory'])
                for arm in s08.ARMS:
                    self.assertIs(False, result['arms'][arm]['expression_control'])
                    self.assertIs(False, result['arms'][arm]['counts_for_live'])

    def test_interleaved_recall_lands_at_recall_time(self):
        # 回忆时刻插在基础事件之间：着陆时刻正确，之前的基础行不受影响。
        plan = self.plan(recall_at=2500.0)
        result = s08.run_experiment(plan)
        recall_rows = [r for r in result['diff']['rows'] if r['is_recall']]
        self.assertTrue(all(r['at'] == 2500.0 for r in recall_rows))
        early = [r for r in result['diff']['rows'] if r['control_present'] and r['at'] < 2500.0]
        self.assertTrue(all(not r['changed'] for r in early))
        self.assertTrue(result['checks']['max_error_ok'])


# ── 评审包：形状稳定、JSON、manual_review 口径、限制声明 ──────────────────


class ReviewPackageTests(Base):
    TOP_KEYS = {'experiment_id', 'algorithm_version', 'now', 'plan', 'arms', 'retractions',
                'diff', 'checks', 'expression_control', 'counts_for_live', 'trajectory',
                'organic', 'authorization', 'provenance', 'limitations'}
    PLAN_KEYS = {'kernel_version', 'start_at', 'end_at', 'tick_seconds', 'recall_at',
                 'base_event_ids', 'recall_event_ids', 'recall_index', 'recall_skipped',
                 'recall_source_status', 'duplicate_recall_ids', 'parameter_hash',
                 'dynamics_parameter_hash', 'appraisal_rule_hash', 'initial_state', 'arms'}
    ARM_KEYS = {'revision_id', 'parent_revision_id', 'branch', 'kernel_version',
                'parameter_hash', 'dynamics_parameter_hash', 'appraisal_rule_hash',
                'start_at', 'end_at', 'tick_seconds', 'state', 'event_count',
                'replayed_event_ids', 'excluded', 'correction_chain', 'checks',
                'expression_control', 'counts_for_live', 'trajectory'}
    DIFF_KEYS = {'rows', 'final_diff', 'changed_dimensions', 'effect', 'state_identical',
                 'processed_diff', 'sources', 'retracted_recalls', 'by_dimension'}
    PROV_KEYS = {'algorithm_version', 'method', 'kernel_version', 'recall_event_kind',
                 'recall_layer', 'llm_evaluation', 'llm_reinterpretation', 'network',
                 'production_ledger', 'expression_takeover'}

    def test_package_shape_is_stable(self):
        pkg = self.package()
        self.assertEqual(self.TOP_KEYS, set(pkg))
        self.assertEqual(self.PLAN_KEYS, set(pkg['plan']))
        self.assertEqual(self.DIFF_KEYS, set(pkg['diff']))
        self.assertEqual(self.PROV_KEYS, set(pkg['provenance']))
        self.assertEqual({'control', 'recall'}, set(pkg['arms']))
        for arm in s08.ARMS:
            self.assertEqual(self.ARM_KEYS, set(pkg['arms'][arm]))

    def test_package_json_roundtrip_stable(self):
        pkg = self.package()
        text = json.dumps(pkg, sort_keys=True, ensure_ascii=False)
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False))
        self.assertEqual(text, json.dumps(self.package(), sort_keys=True, ensure_ascii=False))

    def test_organic_and_authorization_gates(self):
        pkg = self.package()
        self.assertEqual('not_observed', pkg['organic'])
        self.assertEqual('manual_review', pkg['authorization'])

    def test_limitations_declare_honest_boundaries(self):
        limits = ' '.join(s08.LIMITATIONS)
        for token in ('人工评审', '主观体验', '可执行', '不证明', 'emotion_shadow',
                      '表达', 'LLM', 'manual_review', 'not_observed', 'C04'):
            self.assertIn(token, limits)
        self.assertIn('不证明主观体验', s08.__doc__)
        self.assertIn('人工评审', s08.__doc__)

    def test_provenance_flags_all_false(self):
        prov = self.package()['provenance']
        for key in ('llm_evaluation', 'llm_reinterpretation', 'network',
                    'production_ledger', 'expression_takeover'):
            self.assertIs(False, prov[key])
        self.assertEqual('isolated_recall_ablation_frozen_replay', prov['method'])
        self.assertEqual('tool_result', prov['recall_event_kind'])
        self.assertEqual('looked_up', prov['recall_layer'])

    def test_package_deterministic_across_runs(self):
        self.assertEqual(canonical(self.package()), canonical(self.package()))

    def test_arm_summaries_carry_audit_fields(self):
        pkg = self.package()
        for arm in s08.ARMS:
            summary = pkg['arms'][arm]
            self.assertTrue(summary['revision_id'])
            self.assertIn('valence', summary['state'])
            self.assertTrue(summary['checks']['ok'])
            self.assertTrue(summary['parameter_hash'])
            self.assertIsInstance(summary['replayed_event_ids'], list)
        self.assertEqual(['p1', 'n1', 'r1', 't1'], pkg['plan']['base_event_ids'])
        self.assertEqual(2, len(pkg['plan']['recall_event_ids']))

    def test_package_with_retractions_reflects_no_effect(self):
        plan = self.plan()
        result = s08.run_experiment(plan, retractions=[
            {'target': 'm1', 'reason': '撤回'}, {'target': 'm2', 'reason': '撤回'}])
        pkg = s08.review_package(plan, result)
        self.assertEqual('no_effect', pkg['diff']['effect'])
        self.assertEqual(2, len(pkg['retractions']))
        self.assertEqual(2, len(pkg['arms']['recall']['correction_chain']))
        self.assertIs(False, pkg['expression_control'])
        self.assertIs(False, pkg['counts_for_live'])

    def test_convenience_entry_matches_composition(self):
        events, rendered = base_events(), rendered_fixture()
        direct = s08.run_isolation_experiment(events, rendered, now=NOW,
                                              recall_at=RECALL_AT, start_at=START, end_at=END)
        plan = s08.plan_experiment(events, rendered, now=NOW, recall_at=RECALL_AT,
                                   start_at=START, end_at=END)
        composed = s08.review_package(plan, s08.run_experiment(plan))
        self.assertEqual(canonical(direct), canonical(composed))

    def test_review_package_rejects_mismatched_result(self):
        plan = self.plan()
        other = self.plan(now=NOW + 1)
        result = s08.run_experiment(other)
        with self.assertRaises(ValueError):
            s08.review_package(plan, result)
        with self.assertRaises(ValueError):
            s08.review_package(None, result)

    def test_package_recall_index_keeps_refs(self):
        pkg = self.package()
        index = pkg['plan']['recall_index']
        self.assertEqual(2, len(index))
        for ref in index.values():
            for field in ('event_id', 'object_id', 'scene_id', 'store',
                          'epistemic_kind', 'verdict_refs', 'root_source_ids'):
                self.assertIn(field, ref)

    def test_package_does_not_mutate_inputs(self):
        plan = self.plan()
        result = s08.run_experiment(plan)
        plan_snapshot, result_snapshot = canonical(plan), canonical(result)
        pkg = s08.review_package(plan, result)
        pkg['diff']['sources'].clear()
        pkg['plan']['recall_index'].clear()
        self.assertEqual(plan_snapshot, canonical(plan))
        self.assertEqual(result_snapshot, canonical(result))


# ── AST 静态锁：只复用冻结原语，无第二套动力学/顺序，无写入，影子恒 False ──


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(BOT, 'recall_emotion.py').read_text(encoding='utf-8')
        cls.tree = ast.parse(cls.source)

    def test_only_frozen_primitives_and_stdlib_are_imported(self):
        allowed = {'copy', 'hashlib', 'json', 'emotion_core', 'emotion_revision',
                   'emotion_trajectories', 'invalidation_dag'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or '').split('.')[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             'S08 import 了写入/网络/模型/动力学 API：%s' % sorted(names - allowed))

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
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, ('source_seq', 'input_seq'),
                                 '不许在代码里手搓第二套并列决序键')
            if isinstance(node, ast.FunctionDef):
                self.assertNotIn(node.name, ('_replay_key', '_order_key', '_sort_key',
                                             '_tie_break', '_replay', '_advance', '_impulse'),
                                 '不许定义第二套回放顺序/动力学函数')

    def test_reuses_frozen_primitives(self):
        for token in ('rev.build_base_run', 'rev.plan_revision', 'rev.commit_revision',
                      'rev.commit_base_run', 'rev.new_ledger', 'dag.correction',
                      'rev._kernel_hashes', 'legacy._event_header'):
            self.assertIn(token, self.source, 'S08 必须复用冻结原语 %s' % token)

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
                for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ',
                             'ALTER ', 'REPLACE '):
                    self.assertNotIn(verb, upper, 'S08 里出现写入 SQL：%s' % node.value[:60])
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else '')
                self.assertNotIn(name, forbidden, 'S08 里出现写操作 %s' % name)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ('getenv', 'join', 'abspath'),
                                     '模块级冻住了路径；S08 必须是纯编排')

    def test_no_llm_or_network_or_production_shadow(self):
        self.assertNotIn('import emotion_shadow', self.source)
        self.assertNotIn('emotion_shadow.', self.source)
        banned_calls = {'urlopen', 'urlretrieve', 'call_llm', 'chat_complete',
                        'evaluate_with_llm', 'send_message', 'post', 'request'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else '')
                self.assertNotIn(name.lower(), banned_calls, 'S08 调了 LLM/网络 %s' % name)
        self.assertIn("'llm_evaluation': False", self.source)
        self.assertIn("'production_ledger': False", self.source)

    def test_axes_and_constants(self):
        self.assertEqual(tuple(rev.KERNELS), s08.KERNELS)
        self.assertEqual(rev.DIMENSIONS, s08.DIMENSIONS)
        self.assertEqual(('control', 'recall'), s08.ARMS)
        self.assertEqual('tool_result', s08.RECALL_EVENT_KIND)
        self.assertEqual('looked_up', s08.RECALL_LAYER)
        self.assertEqual(1e-10, s08.EFFECT_EPSILON)
        self.assertEqual(rev.DEFAULT_KERNEL, s08.DEFAULT_KERNEL)

    def test_limitations_tuple_and_docstring_declares_manual_review(self):
        self.assertIsInstance(s08.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(s08.LIMITATIONS), 5)
        for token in ('真实结论需真实样本与人工评审', '只证明可执行', '不证明主观体验'):
            self.assertIn(token, s08.__doc__)

    def test_recall_event_kind_is_never_message(self):
        # 认识论分层的静态锁：回忆事件化只许 tool_result 形状，不许 message。
        self.assertIn("RECALL_EVENT_KIND = 'tool_result'", self.source)
        self.assertNotIn("RECALL_EVENT_KIND = 'message'", self.source)


# ── 隔离与端到端：真实 R05→R06 管线、临时目录零写入、环境无关 ──────────────


class IsolationTests(Base):
    def test_end_to_end_creates_no_files(self):
        plan = self.plan()
        s08.run_experiment(plan)
        s08.run_experiment(plan, retractions=[{'target': 'm1', 'reason': '撤回'}])
        s08.review_package(plan, s08.run_experiment(plan))
        s08.run_isolation_experiment(base_events(), rendered_fixture(), now=NOW,
                                     recall_at=RECALL_AT, start_at=START, end_at=END)
        self.assertTrue(self.nothing_created())

    def test_real_r05_r06_pipeline_feeds_experiment(self):
        rendered = rendered_fixture()
        self.assertEqual('sufficient', rendered['status'])
        self.assertEqual(2, len(rendered['scenes']))
        pkg = self.package(rendered=rendered)
        self.assertEqual('difference_observed', pkg['diff']['effect'])
        self.assertEqual(['m1', 'm2'],
                         sorted(s['object_id'] for s in pkg['diff']['sources']))
        self.assertTrue(self.nothing_created())

    def test_invalidated_seed_never_recalled(self):
        seed = claim_item('m1', '主人说谢谢你', ['e1'], validity='invalidated')
        resolved = rc.resolve_ripple([seed], [], items=[seed])
        rendered = re6.render_evidence(resolved, items=[seed], seeds=[seed])
        self.assertEqual([], rendered['scenes'])
        pkg = self.package(rendered=rendered)
        self.assertEqual('no_effect', pkg['diff']['effect'])
        self.assertEqual([], pkg['plan']['recall_event_ids'])

    def test_environment_independence(self):
        one = self.package()
        other = tempfile.TemporaryDirectory(prefix='s08_else_', ignore_cleanup_errors=True)
        self.addCleanup(other.cleanup)
        os.environ['DATA_DIR'] = other.name
        try:
            two = self.package()
        finally:
            os.environ['DATA_DIR'] = self.root
        self.assertEqual(canonical(one), canonical(two))
        self.assertTrue(self.nothing_created())


if __name__ == '__main__':
    unittest.main()