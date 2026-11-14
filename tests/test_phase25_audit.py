import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import phase_audit
import self_model as sm


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='phase25_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'persona'
        self.env = patch.dict(os.environ, {
            'DATA_DIR': self.temp.name, 'PERSONA_DATA_DIR': str(self.root),
            'QQ_DRY_RUN': '1',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_integrity_ok_without_evidence_is_not_permission_for_stage_three(self):
        report = phase_audit.audit_readiness(self.root, now=1000)
        self.assertTrue(report['ok'])
        self.assertEqual(report['phase25']['status'], 'blocked')
        self.assertFalse(report['stage3']['authorized'])
        self.assertFalse(report['stage3']['expression_control'])
        self.assertEqual(report['stage3']['stage3a'], 'blocked')
        self.assertTrue(report['phase25']['blockers'])

    def test_all_recent_actions_need_existing_evidence_not_just_one_good_example(self):
        event = sm.record_event('private_message', ts=900, actor_qq='12345',
                                topic_qq='12345', dedupe='source:1')
        sm.record_action('dm', target_qq='12345', topic_qq='12345',
                         evidence=[event], state='submitted', ts=950)
        sm.record_action('dm', target_qq='12345', topic_qq='12345',
                         evidence=[], state='submitted', ts=951)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_evidence')
        self.assertEqual(check['status'], 'blocked')
        self.assertEqual(check['observed'], {'total': 2, 'linked': 1,
                                            'missing': 1, 'dangling_refs': 0,
                                            'unusable_refs': 0})

    def test_test_events_cannot_make_a_live_action_look_grounded(self):
        event = sm.record_event('private_message', ts=900, actor_qq='12345',
                                topic_qq='12345', source='synthetic:test',
                                payload={'synthetic': True}, dedupe='test:1')
        sm.record_action('dm', target_qq='12345', topic_qq='12345',
                         evidence=[event], state='submitted', ts=950)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_evidence')
        self.assertEqual(check['status'], 'blocked')
        self.assertEqual(check['observed']['unusable_refs'], 1)

    def test_shadow_review_readiness_does_not_authorize_expression(self):
        import emotion_shadow
        status = {
            'ready_for_review': True, 'expression_control': False,
            'calibration_detail': {'passed': 10, 'total': 10},
            'diagnostics': {'dimensions': {
                'valence': {'positive': 6, 'negative': 0},
            }, 'evidence_replay': {'ok': True}},
        }
        with patch.object(emotion_shadow, 'status', return_value=status):
            report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'shadow_review')
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(report['phase25']['shadow_review']['calibration_passed'], 10)
        self.assertEqual(report['phase25']['shadow_review']['valence_negative'], 0)
        self.assertFalse(report['stage3']['authorized'])

    def test_dangling_references_and_aggregate_self_views_cannot_pass(self):
        sm.record_action('dm', target_qq='12345', topic_qq='12345',
                         evidence=['missing-event'], state='submitted', ts=950)
        consolidation = sm.record_event('consolidation', ts=900, source='journal')
        sm.note_self_view('我更喜欢先弄清楚再开口', reason='整理',
                          evidence=[consolidation], ts=960)
        report = phase_audit.audit_readiness(self.root, now=1000)
        checks = {c['key']: c for c in report['phase25']['checks']}
        self.assertEqual(checks['action_evidence']['observed']['dangling_refs'], 1)
        self.assertEqual(checks['self_view_grounding']['status'], 'blocked')
        self.assertEqual(checks['self_view_grounding']['observed']['aggregate_only'], 1)

    def test_audit_does_not_write_or_export_private_contents(self):
        import hashlib
        import json
        secret = '仅供私聊的测试内容，不应进入报告'
        source = sm.record_event('private_message', ts=900, actor_qq='918273645',
                                 payload={'text': secret}, dedupe='source:private')
        sm.record_action('dm', target_qq='918273645', topic_qq='918273645',
                         evidence=[source], state='submitted', ts=950)
        # Read-only WAL readers may create lock sidecars, not change logical database contents.
        files = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.iterdir() if p.suffix == '.db'}
        report = phase_audit.audit_readiness(self.root, now=1000)
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn('918273645', encoded)
        self.assertEqual(files, {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in self.root.iterdir() if p.suffix == '.db'})
        from contextlib import closing
        with closing(phase_audit._ro(self.root / 'cognition.db')) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM event').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM action').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM belief').fetchone()[0], 0)

    def test_synthetic_belief_and_self_view_refs_are_not_live_grounding(self):
        event = sm.record_event('private_message', ts=900, source='synthetic:probe',
                                payload={'synthetic': True}, dedupe='synthetic:1')
        sm.add_belief('她更愿意先核对资料', evidence=[event], ts=950)
        sm.note_self_view('她更愿意先核对资料', reason='测试解释', evidence=[event], ts=960)
        report = phase_audit.audit_readiness(self.root, now=1000)
        checks = {c['key']: c for c in report['phase25']['checks']}
        self.assertEqual(checks['belief_grounding']['status'], 'blocked')
        self.assertEqual(checks['self_view_grounding']['status'], 'blocked')

    def test_readiness_cli_strict_returns_nonzero_for_a_blocked_gate(self):
        import io
        import json
        from contextlib import redirect_stdout
        output = io.StringIO()
        with redirect_stdout(output):
            code = phase_audit.main(['--readiness', '--strict', '--json',
                                     '--root', str(self.root)])
        self.assertEqual(code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report['phase25']['status'], 'blocked')
        self.assertFalse(report['stage3']['authorized'])

    def test_synthetic_belief_origin_is_not_live_even_with_a_real_event(self):
        event = sm.record_event('private_message', ts=900, source='qq', dedupe='source:1')
        sm.add_belief('验证样本不属于真实生活', source='synthetic:probe',
                      evidence=[event], ts=950)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'belief_grounding')
        self.assertEqual(check['status'], 'not_observed')
        self.assertEqual(check['observed']['excluded'], 1)

    def test_verification_receipt_does_not_confirm_a_real_action(self):
        action = sm.record_action('dm', target_qq='12345', state='accepted', ts=950)
        sm.record_event('action_result', ts=960, source='synthetic:receipt',
                        payload={'action_id': action, 'state': 'accepted',
                                 'evidence_origin': 'verification'}, dedupe='receipt:verify')
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_results')
        self.assertEqual(check['status'], 'blocked')
        self.assertEqual(check['observed']['receipted'], 0)

    def test_old_proposal_executed_today_is_in_the_recent_action_window(self):
        from contextlib import closing
        action = sm.record_action('dm', target_qq='12345', state='submitted', ts=1)
        with closing(sm._db()) as connection, connection:
            connection.execute('UPDATE action SET updated=? WHERE id=?', (99990, action))
        report = phase_audit.audit_readiness(self.root, now=100000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_evidence')
        self.assertEqual(check['observed']['total'], 1)
        self.assertEqual(check['status'], 'blocked')

    def test_status_errors_cannot_corrupt_the_machine_readable_report(self):
        import emotion_shadow
        import io
        import json
        from contextlib import redirect_stdout

        def broken_status(**kwargs):
            print('optional candidate diagnostics failed')
            raise KeyError('bad_candidate_shape')

        output = io.StringIO()
        with patch.object(emotion_shadow, 'status', side_effect=broken_status), redirect_stdout(output):
            code = phase_audit.main(['--readiness', '--strict', '--json',
                                     '--root', str(self.root)])
        self.assertEqual(code, 1)
        report = json.loads(output.getvalue())
        check = next(c for c in report['phase25']['checks'] if c['key'] == 'shadow_review')
        self.assertEqual(check['status'], 'unknown')
        self.assertFalse(report['stage3']['authorized'])

    def test_group_recipient_is_the_group_scope_not_a_required_private_target(self):
        sm.record_action('group', topic_qq='12345', scope='group:67890',
                         visibility='group', state='submitted', ts=950)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_identity')
        self.assertEqual(check['observed'], {'total': 1, 'bound': 1})
        self.assertEqual(check['status'], 'pass')

    def test_proactive_group_action_is_bound_by_its_group_scope(self):
        # 主动群发言没有单一话题人物（topic_qq 空），但收件方就是群本身。
        # 旧口径硬要一个人物 topic，于是 359 条真实主动群发言全被判未绑定。
        sm.record_action('group', topic_qq='', scope='group:67890',
                         visibility='group', state='submitted', ts=950)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_identity')
        self.assertEqual(check['observed'], {'total': 1, 'bound': 1})
        self.assertEqual(check['status'], 'pass')

    def test_group_action_without_a_valid_group_scope_is_not_bound(self):
        sm.record_action('group', topic_qq='12345', scope='not-a-group',
                         visibility='group', state='submitted', ts=950)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_identity')
        self.assertEqual(check['observed']['bound'], 0)
        self.assertEqual(check['status'], 'blocked')

    def test_dm_still_requires_a_private_recipient_and_topic(self):
        sm.record_action('dm', topic_qq='12345', target_qq='',
                         visibility='private', state='submitted', ts=950)
        report = phase_audit.audit_readiness(self.root, now=1000)
        check = next(c for c in report['phase25']['checks']
                     if c['key'] == 'action_identity')
        self.assertEqual(check['observed']['bound'], 0)

    def test_malformed_status_containers_are_unknown_not_an_audit_crash(self):
        import emotion_shadow
        for status in ({'calibration_detail': [1]}, {'diagnostics': [1]},
                       {'diagnostics': {'dimensions': [1]}}):
            with self.subTest(status=status), patch.object(emotion_shadow, 'status', return_value=status):
                report = phase_audit.audit_readiness(self.root, now=1000)
                check = next(c for c in report['phase25']['checks'] if c['key'] == 'shadow_review')
                self.assertEqual(check['status'], 'unknown')
                self.assertFalse(report['stage3']['authorized'])


if __name__ == '__main__':
    unittest.main()
