"""M01 类型化准入契约：六类对象各走各的门，不混写。

行为测试覆盖统一方案 §7.2 的每条准入规则；AST 锁保证契约保持纯函数
（不碰账本、不发网络），防止有人把「判定」顺手改成「直接写入」。
"""
import ast
import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import cognition_admission as ca

NOW = 2000.0


def candidate(kind='belief', **over):
    base = {'object_kind': kind, 'object_id': 'obj_1',
            'content': '对方明确自述：我喜欢牛肉面',
            'occurred_at': 1000.0, 'recorded_at': 1001.0,
            'source_event_ids': ['evt_1'], 'epistemic_type': 'experienced',
            'visibility': 'private', 'source_session': 'private:1001'}
    base.update(over)
    return base


class EnvelopeTests(unittest.TestCase):
    def test_unknown_kind_is_rejected(self):
        for bad in ({}, None, candidate(object_kind='vibe'), candidate(object_kind='')):
            verdict = ca.admit(bad, now=NOW)
            self.assertEqual('reject', verdict['decision'])
            self.assertIn('unknown_kind', verdict['reasons'])

    def test_missing_identity_content_or_record_time_is_rejected(self):
        for key, code in (('object_id', 'missing_object_id'),
                          ('content', 'missing_content'),
                          ('recorded_at', 'missing_recorded_at')):
            verdict = ca.admit(candidate(**{key: '' if key != 'recorded_at' else None}), now=NOW)
            self.assertEqual('reject', verdict['decision'], key)
            self.assertIn(code, verdict['reasons'])

    def test_unknown_occurrence_time_is_kept_unknown_not_backfilled(self):
        verdict = ca.admit(candidate(occurred_at=None), now=NOW)
        self.assertEqual('accept', verdict['decision'])

    def test_future_or_after_record_occurrence_is_rejected(self):
        future = ca.admit(candidate(occurred_at=NOW + 3600), now=NOW)
        self.assertIn('future_occurrence', future['reasons'])
        self.assertEqual('reject', future['decision'])
        fabricated = ca.admit(candidate(occurred_at=1500.0, recorded_at=1001.0), now=NOW)
        self.assertIn('occurrence_after_record', fabricated['reasons'])
        self.assertEqual('reject', fabricated['decision'])

    def test_synthetic_or_verification_source_never_enters(self):
        for over in ({'synthetic': True}, {'evidence_origin': 'verification'}):
            verdict = ca.admit(candidate(**over), now=NOW)
            self.assertEqual('reject', verdict['decision'])
            self.assertIn('synthetic_source', verdict['reasons'])

    def test_missing_scope_or_epistemic_type_is_rejected(self):
        no_scope = ca.admit(candidate(visibility=''), now=NOW)
        self.assertIn('no_scope', no_scope['reasons'])
        no_layer = ca.admit(candidate(epistemic_type='fact'), now=NOW)
        self.assertIn('unknown_epistemic_type', no_layer['reasons'])

    def test_factual_kinds_need_real_source_events(self):
        for kind in ('episode', 'belief', 'state', 'preference', 'commitment'):
            verdict = ca.admit(candidate(kind, source_event_ids=[]), now=NOW)
            self.assertEqual('reject', verdict['decision'], kind)
            self.assertIn('no_source_events', verdict['reasons'], kind)


class EpisodeClaimTests(unittest.TestCase):
    def test_experienced_episode_with_sources_is_accepted(self):
        verdict = ca.admit(candidate('episode'), now=NOW)
        self.assertEqual('accept', verdict['decision'])
        self.assertTrue(verdict['current'])

    def test_other_peoples_experience_is_not_hers(self):
        verdict = ca.admit(candidate('episode', perspective='self',
                                     epistemic_type='reported'), now=NOW)
        self.assertEqual('reject', verdict['decision'])
        self.assertIn('not_her_experience', verdict['reasons'])
        other = ca.admit(candidate('episode', perspective='other',
                                   epistemic_type='reported'), now=NOW)
        self.assertEqual('accept', other['decision'])

    def test_unsupported_claim_is_quarantined_not_fact(self):
        verdict = ca.admit(candidate('claim', source_event_ids=[]), now=NOW)
        self.assertEqual('quarantine', verdict['decision'])
        self.assertIn('unsupported_claim', verdict['reasons'])
        self.assertFalse(verdict['current'])

    def test_inferred_claim_stays_candidate(self):
        verdict = ca.admit(candidate('claim', epistemic_type='inferred'), now=NOW)
        self.assertEqual('quarantine', verdict['decision'])
        self.assertIn('inferred_claim', verdict['reasons'])

    def test_fiction_chat_is_an_episode_but_never_a_real_person_fact(self):
        episode = ca.admit(candidate('episode', context_kind='tarot'), now=NOW)
        self.assertEqual('accept', episode['decision'])
        for kind in ('belief', 'preference', 'commitment'):
            verdict = ca.admit(candidate(kind, context_kind='roleplay'), now=NOW)
            self.assertEqual('reject', verdict['decision'], kind)
            self.assertIn('fiction_not_fact', verdict['reasons'], kind)
        claim = ca.admit(candidate('claim', context_kind='fiction'), now=NOW)
        self.assertEqual('quarantine', claim['decision'])
        self.assertIn('fiction_claim', claim['reasons'])


class BeliefStateTests(unittest.TestCase):
    def test_reported_belief_keeps_its_layer(self):
        verdict = ca.admit(candidate('belief', epistemic_type='reported'), now=NOW)
        self.assertEqual('accept', verdict['decision'])

    def test_inferred_or_imagined_belief_is_rejected(self):
        for layer in ('inferred', 'imagined'):
            verdict = ca.admit(candidate('belief', epistemic_type=layer), now=NOW)
            self.assertEqual('reject', verdict['decision'], layer)
            self.assertIn('belief_layer_not_fact', verdict['reasons'])

    def test_state_needs_an_expiry(self):
        verdict = ca.admit(candidate('state'), now=NOW)
        self.assertEqual('reject', verdict['decision'])
        self.assertIn('state_needs_expiry', verdict['reasons'])

    def test_expired_state_is_history_not_now(self):
        verdict = ca.admit(candidate('state', valid_until=NOW - 1), now=NOW)
        self.assertEqual('accept', verdict['decision'])
        self.assertFalse(verdict['current'], '过期状态不能再当「现在」注入')
        self.assertIn('expired_state', verdict['reasons'])
        live = ca.admit(candidate('state', valid_until=NOW + 3600), now=NOW)
        self.assertEqual('accept', live['decision'])
        self.assertTrue(live['current'])


class PreferenceCommitmentTests(unittest.TestCase):
    def test_explicit_self_report_is_accepted_without_generalization(self):
        verdict = ca.admit(candidate('preference', explicit_self_report=True,
                                     actor_is_subject=True), now=NOW)
        self.assertEqual('accept', verdict['decision'])
        self.assertIn('statement_not_generalized', verdict['reasons'])

    def test_inferred_or_single_emotion_preference_is_quarantined(self):
        # 「真讨厌」这种一时表达没有明确自述，不晋升永久偏好。
        verdict = ca.admit(candidate('preference'), now=NOW)
        self.assertEqual('quarantine', verdict['decision'])
        self.assertIn('preference_needs_self_report', verdict['reasons'])
        not_subject = ca.admit(candidate('preference', explicit_self_report=True,
                                         actor_is_subject=False), now=NOW)
        self.assertEqual('quarantine', not_subject['decision'])

    def _commitment(self, **over):
        base = dict(to_qq='1001', accepted_by_self=True,
                    completion_criteria='周五前把图发给他')
        base.update(over)
        return candidate('commitment', **base)

    def test_accepted_commitment_with_object_and_criteria_is_admitted(self):
        verdict = ca.admit(self._commitment(), now=NOW)
        self.assertEqual('accept', verdict['decision'])

    def test_commitment_without_acceptance_object_or_criteria_is_rejected(self):
        for over, code in (({'accepted_by_self': False}, 'commitment_needs_acceptance'),
                           ({'to_qq': ''}, 'commitment_needs_object'),
                           ({'completion_criteria': ''}, 'commitment_needs_criteria')):
            verdict = ca.admit(self._commitment(**over), now=NOW)
            self.assertEqual('reject', verdict['decision'], code)
            self.assertIn(code, verdict['reasons'])

    def test_affection_promise_is_not_a_general_commitment(self):
        for over in ({'source': 'affection.parse_promise'}, {'affection_promise': True}):
            verdict = ca.admit(self._commitment(**over), now=NOW)
            self.assertEqual('reject', verdict['decision'])
            self.assertIn('affection_promise_not_commitment', verdict['reasons'])


class ToolCompletionTests(unittest.TestCase):
    def test_generated_needs_artifact_and_delivery_needs_receipt(self):
        no_artifact = ca.admit(candidate('claim', tool_claim='generated'), now=NOW)
        self.assertIn('tool_generation_needs_artifact', no_artifact['reasons'])
        self.assertEqual('reject', no_artifact['decision'])
        for claim in ('sent', 'delivered', 'read', 'accepted'):
            verdict = ca.admit(candidate('claim', tool_claim=claim), now=NOW)
            self.assertEqual('reject', verdict['decision'], claim)
            self.assertIn('tool_completion_needs_receipt', verdict['reasons'])

    def test_tool_claims_with_real_evidence_are_admitted(self):
        generated = ca.admit(candidate('claim', tool_claim='generated',
                                       artifact_ref='artifact:42'), now=NOW)
        self.assertEqual('accept', generated['decision'])
        read = ca.admit(candidate('claim', tool_claim='read',
                                  receipt_event_ids=['evt_receipt']), now=NOW)
        self.assertEqual('accept', read['decision'])

    def test_unknown_tool_claim_is_rejected(self):
        verdict = ca.admit(candidate('claim', tool_claim='mind_read'), now=NOW)
        self.assertEqual('reject', verdict['decision'])
        self.assertIn('unknown_tool_claim', verdict['reasons'])


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, 'cognition_admission.py'), encoding='utf-8') as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_contract_is_a_pure_module(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertEqual('time', alias.name.split('.')[0],
                                     '准入契约不许碰账本/网络/子进程')
            elif isinstance(node, ast.ImportFrom):
                self.fail('准入契约不许 from-import 外部能力: %s' % node.module)
        self.assertNotIn('cognition.db', self.source)
        self.assertNotIn('sqlite3', self.source)

    def test_kind_and_decision_sets_are_fixed(self):
        self.assertEqual(('episode', 'claim', 'belief', 'state', 'preference', 'commitment'),
                         ca.KINDS)
        self.assertEqual(('accept', 'quarantine', 'reject'), ca.DECISIONS)

    def test_module_level_state_is_constant_only(self):
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self.assertTrue(isinstance(target, ast.Name) and target.id.isupper(),
                                    '契约模块不许有可变全局状态')


if __name__ == '__main__':
    unittest.main()