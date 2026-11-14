"""S02「动力学与评估规则分别冻结」回归锁。

AST 静态锁：两把独立哈希函数存在、既有公开符号一个不少（纯增量）、
动力学锁不引用任何证据词表/口径版本、证据锁不引用任何恢复/步长参数、
线上 parameter_hash 仍对整个 PARAMETERS 计算、两把新锁只读不改参数。
行为锁：改词表→动力学哈希不动；改步长/恢复→证据哈希不动；线上
parameter_hash 与 v1 参考内核冻结值不变；appraise/advance/apply_event/
integrated_appraisal 行为不变。钉死值变了必须显式评审，不许顺手更新。
"""
import ast
import copy
import hashlib
import io
import json
import math
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, 'bot')
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import emotion_core as legacy
import emotion_dynamics as core

# 线上冻结口径（S02 之前就已固定）：整个 PARAMETERS（含词表）的 SHA256。
LIVE_PARAMETER_HASH = 'fddcdc0ec1fa48fe54d6a83749af04133eb2397fc3f0cdddbcdde42ca7d08a01'
V1_PARAMETER_HASH = 'ce7b83161e4baa1e4f738d6f1cecb50dcf876350ec848586f643634bdb49f796'
# S02 两把新锁的冻结值：动力学侧与证据侧各自独立评审、各自更新。
DYNAMICS_HASH = '9272cdbc3b38376c4215a016fd39bb8bf693ed963115b94d40d39154b625bc89'
APPRAISAL_HASH = '06a984d264621d0f7fd9bfb709cc8340a89428ebe3b2899d3685139c8c140777'

# S02 之前 emotion_dynamics.py 的全部公开符号，纯增量不许删改。
EXISTING_CONSTANTS = {
    'VERSION', 'VOCAB_VERSION', 'SEMANTIC_VERSION', 'AFFECTION_EVIDENCE',
    'REBUKE_EVIDENCE', 'PARAMETERS', 'DIMENSIONS', 'MIN_VALENCE_CONFIDENCE',
    'SEMANTIC_RULES',
}
EXISTING_FUNCTIONS = {
    'integrated_appraisal', 'parameter_hash', 'initial_state', 'advance',
    'appraise', 'apply_event',
}
# 证据侧的名字：动力学锁的函数体与白名单里都不许出现。
EVIDENCE_NAMES = {
    'AFFECTION_EVIDENCE', 'REBUKE_EVIDENCE', 'SEMANTIC_RULES', 'VOCAB_VERSION',
    'SEMANTIC_VERSION', 'MIN_VALENCE_CONFIDENCE',
    'affection_evidence', 'rebuke_evidence', 'semantic_rules', 'vocab_version',
    'semantic_version', 'min_valence_confidence', 'praise_evidence',
    'correction_evidence',
}
# v1 冻结参考参数（只读对照，钉死整份）。
V1_PARAMETERS = {
    'version': 'shadow-v1',
    'calibration': 'uncalibrated computational initial values; not psychometrics',
    'baseline': {'valence': 0.0, 'arousal': 0.25, 'control': 0.5, 'attention': 0.0},
    'tau_seconds': {'valence': 7200.0, 'arousal': 1800.0, 'control': 3600.0, 'attention': 600.0},
    'bounds': {'valence': [-1.0, 1.0], 'arousal': [0.0, 1.0],
               'control': [0.0, 1.0], 'attention': [0.0, 1.0]},
    'traits': {'curiosity': 0.6, 'sociability': 0.5, 'caution': 0.6,
               'playfulness': 0.6, 'sensitivity': 0.5, 'expressiveness': 0.5},
    'message_attention': 0.02,
    'praise_valence': 0.04,
    'praise_control': 0.015,
    'praise_evidence': ['谢谢你', '你帮大忙', '你好厉害'],
    'tool_control': 0.025,
    'tool_attention': 0.01,
}


def _read(name):
    with io.open(os.path.join(BOT, name), encoding='utf-8') as f:
        return f.read()


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('function %s not found' % name)


def _names_and_strings(node):
    names, strings = set(), set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            strings.add(child.value)
    return names, strings


def _module_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
    raise AssertionError('module constant %s not found' % name)


def event(text='', **payload):
    return {'id': 'one', 'kind': 'message', 'ts': 1001, 'source': 'onebot',
            'payload': {'text': text, **payload}}


class StaticFreezeLocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _read('emotion_dynamics.py')
        cls.tree = ast.parse(cls.source)

    def test_both_independent_freeze_functions_exist(self):
        for name in ('dynamics_parameter_hash', 'appraisal_rule_hash'):
            node = _function(self.tree, name)
            self.assertEqual([a.arg for a in node.args.args], [],
                             '%s 必须是无参冻结锁' % name)
            self.assertTrue(callable(getattr(core, name, None)), name)

    def test_all_existing_public_symbols_are_preserved(self):
        assigned = set()
        defined = set()
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                assigned.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.FunctionDef):
                defined.add(node.name)
        self.assertTrue(EXISTING_CONSTANTS <= assigned,
                        '纯增量不许删既有常量: %s' % (EXISTING_CONSTANTS - assigned))
        self.assertTrue(EXISTING_FUNCTIONS <= defined,
                        '纯增量不许删既有函数: %s' % (EXISTING_FUNCTIONS - defined))
        imports = [alias.asname or alias.name
                   for node in self.tree.body if isinstance(node, ast.Import)
                   for alias in node.names]
        self.assertIn('legacy', imports, 'v1 参考内核依赖不许断')

    def test_dynamics_lock_never_references_evidence(self):
        names, strings = _names_and_strings(_function(self.tree, 'dynamics_parameter_hash'))
        whitelist = _module_assignment(self.tree, '_DYNAMICS_PARAMETER_KEYS')
        keys = {elt.value for elt in whitelist.value.elts}
        self.assertFalse((names | strings | keys) & EVIDENCE_NAMES,
                         '动力学锁混进了证据口径: %s' % ((names | strings | keys) & EVIDENCE_NAMES))

    def test_appraisal_lock_never_references_dynamics_parameters(self):
        names, strings = _names_and_strings(_function(self.tree, 'appraisal_rule_hash'))
        dynamics_keys = set(core._DYNAMICS_PARAMETER_KEYS)
        self.assertFalse((names | strings) & dynamics_keys,
                         '证据锁混进了动力学参数: %s' % ((names | strings) & dynamics_keys))

    def test_live_parameter_hash_still_covers_whole_parameters(self):
        names, _ = _names_and_strings(_function(self.tree, 'parameter_hash'))
        self.assertIn('PARAMETERS', names, '线上冻结哈希必须仍对整个 PARAMETERS 计算')
        self.assertFalse(names & {'_DYNAMICS_PARAMETER_KEYS', '_EVIDENCE_PARAMETER_KEYS',
                                  'dynamics_parameter_hash', 'appraisal_rule_hash',
                                  '_canonical_sha256'},
                         '线上 parameter_hash 的计算口径不许被 S02 重构')

    def test_new_locks_are_read_only(self):
        for name in ('dynamics_parameter_hash', 'appraisal_rule_hash'):
            for node in ast.walk(_function(self.tree, name)):
                self.assertNotIsInstance(node, (ast.Assign, ast.AugAssign, ast.Global,
                                                ast.Nonlocal, ast.Delete),
                                         '%s 不许改写任何参数' % name)

    def test_whitelists_partition_parameters_exactly(self):
        dynamics = list(core._DYNAMICS_PARAMETER_KEYS)
        evidence = list(core._EVIDENCE_PARAMETER_KEYS)
        self.assertEqual(len(dynamics), len(set(dynamics)))
        self.assertEqual(len(evidence), len(set(evidence)))
        self.assertFalse(set(dynamics) & set(evidence), '两把锁的键集必须互斥')
        self.assertEqual(set(dynamics) | set(evidence), set(core.PARAMETERS),
                         '每个 PARAMETERS 键必须显式归边，不许漏冻')
        self.assertEqual(set(evidence), {'praise_evidence', 'correction_evidence'})


class FrozenValueLocks(unittest.TestCase):
    def test_live_parameter_hash_value_is_frozen(self):
        self.assertEqual(core.parameter_hash(), LIVE_PARAMETER_HASH)
        encoded = json.dumps(core.PARAMETERS, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()
        self.assertEqual(core.parameter_hash(), hashlib.sha256(encoded).hexdigest())

    def test_v1_reference_kernel_is_intact(self):
        self.assertEqual(legacy.VERSION, 'shadow-v1')
        self.assertEqual(legacy.PARAMETERS, V1_PARAMETERS)
        self.assertEqual(legacy.parameter_hash(), V1_PARAMETER_HASH)
        self.assertEqual(legacy.replay([], start_at=0, end_at=10)['parameter_hash'],
                         V1_PARAMETER_HASH)

    def test_new_lock_values_are_frozen_and_distinct(self):
        self.assertEqual(core.dynamics_parameter_hash(), DYNAMICS_HASH)
        self.assertEqual(core.appraisal_rule_hash(), APPRAISAL_HASH)
        self.assertEqual(len({core.parameter_hash(), core.dynamics_parameter_hash(),
                              core.appraisal_rule_hash()}), 3)

    def test_versions_are_unchanged(self):
        self.assertEqual(core.VERSION, 'shadow-v2')
        self.assertEqual(core.VOCAB_VERSION, 'v2.1')
        self.assertEqual(core.SEMANTIC_VERSION, 'valence-integration-v1')

    def test_hashes_are_deterministic_order_independent_and_non_mutating(self):
        snapshot = copy.deepcopy(core.PARAMETERS)
        first = (core.dynamics_parameter_hash(), core.appraisal_rule_hash())
        self.assertEqual(first, (core.dynamics_parameter_hash(), core.appraisal_rule_hash()))
        reordered = dict(reversed(list(core.PARAMETERS.items())))
        with patch.object(core, 'PARAMETERS', reordered):
            self.assertEqual(core.dynamics_parameter_hash(), first[0])
            self.assertEqual(core.appraisal_rule_hash(), first[1])
            self.assertEqual(core.parameter_hash(), LIVE_PARAMETER_HASH)
        self.assertEqual(core.PARAMETERS, snapshot, '算哈希不许动参数')


class IndependenceLocks(unittest.TestCase):
    def setUp(self):
        self.dynamics = core.dynamics_parameter_hash()
        self.appraisal = core.appraisal_rule_hash()
        self.live = core.parameter_hash()

    def assert_restored(self):
        self.assertEqual(core.dynamics_parameter_hash(), self.dynamics)
        self.assertEqual(core.appraisal_rule_hash(), self.appraisal)
        self.assertEqual(core.parameter_hash(), self.live)

    def test_vocab_changes_never_move_dynamics_lock(self):
        # (补丁, 是否动线上 parameter_hash)：PARAMETERS 内的词表属于线上口径，
        # 模块级识别词表/规则文本本就不在 PARAMETERS 里（VOCAB_VERSION 的既有分离）。
        mutations = [
            (lambda: patch.object(core, 'AFFECTION_EVIDENCE',
                                  core.AFFECTION_EVIDENCE + ('冻结测试词',)), False),
            (lambda: patch.object(core, 'REBUKE_EVIDENCE',
                                  core.REBUKE_EVIDENCE + ('冻结测试词',)), False),
            (lambda: patch.object(core, 'VOCAB_VERSION', 'v2.2-freeze-test'), False),
            (lambda: patch.object(core, 'SEMANTIC_RULES',
                                  core.SEMANTIC_RULES + '- freeze-test\n'), False),
            (lambda: patch.dict(core.PARAMETERS, {
                'praise_evidence': core.PARAMETERS['praise_evidence'] + ['冻结测试词']}), True),
            (lambda: patch.dict(core.PARAMETERS, {
                'correction_evidence': core.PARAMETERS['correction_evidence'] + ['冻结测试词']}), True),
        ]
        for make_patch, moves_live in mutations:
            with self.subTest(moves_live=moves_live):
                with make_patch():
                    self.assertEqual(core.dynamics_parameter_hash(), self.dynamics,
                                     '改证据词表不许动动力学锁')
                    self.assertNotEqual(core.appraisal_rule_hash(), self.appraisal,
                                        '改证据词表必须动证据锁')
                    if moves_live:
                        self.assertNotEqual(core.parameter_hash(), self.live)
                    else:
                        self.assertEqual(core.parameter_hash(), self.live)
                self.assert_restored()

    def test_dynamics_changes_never_move_appraisal_lock(self):
        scalar_steps = [
            ('directed_activation', 0.5), ('tool_activation', 0.5),
            ('correction_valence', -0.5), ('correction_control', -0.5),
            ('praise_valence', 0.5), ('praise_control', 0.5),
            ('message_attention', 0.5), ('tool_control', 0.5), ('tool_attention', 0.5),
        ]
        for key, value in scalar_steps:
            with self.subTest(key=key):
                with patch.dict(core.PARAMETERS, {key: value}):
                    self.assertEqual(core.appraisal_rule_hash(), self.appraisal,
                                     '改动力学步长不许动证据锁')
                    self.assertNotEqual(core.dynamics_parameter_hash(), self.dynamics,
                                        '改动力学步长必须动动力学锁')
                self.assert_restored()
        nested = [
            ('tau_seconds', {'valence': 60.0}),
            ('baseline', {'arousal': 0.3}),
            ('bounds', {'valence': [-0.9, 0.9]}),
        ]
        for key, change in nested:
            with self.subTest(key=key):
                with patch.dict(core.PARAMETERS[key], change):
                    self.assertEqual(core.appraisal_rule_hash(), self.appraisal,
                                     '改恢复参数不许动证据锁')
                    self.assertNotEqual(core.dynamics_parameter_hash(), self.dynamics,
                                        '改恢复参数必须动动力学锁')
                self.assert_restored()

    def test_semantic_confidence_gate_belongs_to_appraisal_side(self):
        with patch.object(core, 'MIN_VALENCE_CONFIDENCE', 0.5):
            self.assertEqual(core.dynamics_parameter_hash(), self.dynamics)
            self.assertNotEqual(core.appraisal_rule_hash(), self.appraisal)
        self.assert_restored()

    def test_v2_mutations_never_touch_v1_reference(self):
        with patch.dict(core.PARAMETERS, {'directed_activation': 9.9}):
            with patch.dict(core.PARAMETERS['tau_seconds'], {'valence': 1.0}):
                self.assertEqual(legacy.parameter_hash(), V1_PARAMETER_HASH)
                self.assertEqual(legacy.PARAMETERS, V1_PARAMETERS)
        self.assertEqual(legacy.parameter_hash(), V1_PARAMETER_HASH)


class BehaviorPreservationLocks(unittest.TestCase):
    def test_appraise_affection_and_rebuke_are_unchanged(self):
        praise = core.appraise(event('谢谢你帮我查清楚了', addressed_to_self=True))
        self.assertEqual(praise['rule'], 'direct_affection')
        self.assertEqual(praise['version'], 'shadow-v2')
        self.assertEqual(praise['confidence'], 0.8)
        self.assertEqual(praise['delta']['valence'], core.PARAMETERS['praise_valence'])
        self.assertEqual(praise['delta']['control'], core.PARAMETERS['praise_control'])
        self.assertEqual(praise['delta']['arousal'], core.PARAMETERS['directed_activation'])
        rebuke = core.appraise(event('你刚才说错了', addressed_to_self=True))
        self.assertEqual(rebuke['rule'], 'direct_rebuke')
        self.assertEqual(rebuke['delta']['valence'], core.PARAMETERS['correction_valence'])
        self.assertEqual(rebuke['delta']['control'], core.PARAMETERS['correction_control'])

    def test_appraise_evidence_boundaries_are_unchanged(self):
        for payload in ({'addressed_to_self': False},
                        {'addressed_to_self': True, 'quoted': True},
                        {'addressed_to_self': True, 'is_roleplay': True}):
            with self.subTest(payload=payload):
                got = core.appraise(event('最喜欢你啦宝宝', **payload))
                self.assertEqual(got['delta']['valence'], 0)
        third_party = core.appraise(event('我今天好难过', addressed_to_self=False))
        self.assertEqual(third_party['delta']['arousal'], 0)
        self.assertEqual(third_party['delta']['valence'], 0)
        self.assertTrue(core.appraise(event('', addressed_to_self=True))['ignored'])
        legacy_event = event('谢谢你', addressed_to_self=True)
        legacy_event['source'] = 'legacy'
        self.assertTrue(core.appraise(legacy_event)['ignored'])

    def test_tool_result_behavior_is_unchanged(self):
        failure = core.appraise({'id': 'one', 'kind': 'tool_result', 'ts': 1001,
                                 'payload': {'ok': False}})
        self.assertEqual(failure['delta']['valence'], 0)
        self.assertEqual(failure['delta']['control'], -core.PARAMETERS['tool_control'])
        self.assertEqual(failure['delta']['attention'], core.PARAMETERS['tool_attention'])
        self.assertEqual(failure['delta']['arousal'], core.PARAMETERS['tool_activation'])
        unknown = core.appraise({'id': 'one', 'kind': 'tool_result', 'ts': 1001,
                                 'payload': {'ok': None}})
        self.assertTrue(unknown['ignored'])
        self.assertEqual(unknown['delta'], dict.fromkeys(core.DIMENSIONS, 0.0))

    def test_advance_matches_independent_exponential_recovery(self):
        state = core.initial_state(100)
        state.update(valence=-0.8, arousal=0.9, control=0.1, attention=0.7)
        before = copy.deepcopy(state)
        result = core.advance(state, 1900)
        expected = {
            'valence': -0.8 * math.exp(-1800 / 7200),
            'arousal': 0.25 + 0.65 * math.exp(-1),
            'control': 0.5 - 0.4 * math.exp(-0.5),
            'attention': 0.7 * math.exp(-3),
        }
        for key, value in expected.items():
            self.assertAlmostEqual(result[key], value, delta=1e-12)
        self.assertEqual(state, before)
        # v2 的 advance 无零时长早退（既有行为，不许改）：exp(0) 乘法有浮点尾差，
        # 迟到时间只钉「不倒退 + 数值不变容差内」。
        late = core.advance(state, 50)
        self.assertEqual(late['at'], 100.0)
        for key in core.DIMENSIONS:
            self.assertAlmostEqual(late[key], state[key], delta=1e-12)

    def test_apply_event_recovers_then_impulses_and_checks_appraisal(self):
        state = core.initial_state(100)
        state['valence'] = 0.8
        value = event('谢谢你帮我查清楚了', addressed_to_self=True)
        value['id'] = 'p1'
        value['ts'] = 1900
        assessment = core.appraise(value)
        result = core.apply_event(state, value, assessment)
        self.assertAlmostEqual(result['valence'],
                               0.8 * math.exp(-0.25) + assessment['delta']['valence'], delta=1e-12)
        self.assertEqual(result['at'], 1900.0)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result, core.apply_event(state, value))
        forged = copy.deepcopy(assessment)
        forged['delta']['valence'] += 0.1
        with self.assertRaisesRegex(ValueError, 'appraisal'):
            core.apply_event(state, value, forged)
        clipped = core.initial_state(100)
        for index in range(200):
            impulse = event('谢谢你', addressed_to_self=True)
            impulse['id'] = 'p%d' % index
            impulse['ts'] = 100
            clipped = core.apply_event(clipped, impulse)
        self.assertEqual(clipped['valence'], 1)
        self.assertEqual(clipped['control'], 1)

    def test_initial_state_and_integrated_appraisal_are_unchanged(self):
        self.assertEqual(core.initial_state(100), legacy.initial_state(100))
        value = event('这次处理得很靠谱', addressed_to_self=True)
        candidate = {'event_id': 'one', 'state': 'done', 'stance': 'praise',
                     'confidence': 0.9, 'reason': '明确肯定', 'model': 'test-model'}
        accepted = core.integrated_appraisal(value, candidate)
        self.assertEqual(accepted['rule'], 'integrated_praise')
        self.assertEqual(accepted['semantic_version'], core.SEMANTIC_VERSION)
        self.assertEqual(accepted['delta']['valence'], core.PARAMETERS['praise_valence'])
        self.assertEqual(accepted['exclusion_reason'], '')
        low = core.integrated_appraisal(value, dict(candidate, confidence=0.5))
        self.assertEqual(low['exclusion_reason'], 'low_confidence')
        self.assertEqual(low['delta']['valence'], 0)
        pending = core.integrated_appraisal(value, dict(candidate, state='pending'))
        self.assertEqual(pending['exclusion_reason'], 'label_pending')
        self.assertEqual(pending['delta']['valence'], 0)


if __name__ == '__main__':
    unittest.main()