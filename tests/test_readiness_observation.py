"""Release observations never repair history or manufacture live samples."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import engineering_contracts
import phase_audit
import self_model as sm


class ReadinessObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='readiness-observation-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'persona'
        env = patch.dict(os.environ, DATA_DIR=temp.name,
                         PERSONA_DATA_DIR=str(self.root), QQ_DRY_RUN='1')
        env.start()
        self.addCleanup(env.stop)
        # Environment is isolated before any database is opened. Python 3.10
        # supports addCleanup, unlike TestCase.enterContext.
        with closing(sm._db()):
            pass
        self.at = 100000
        self.release_at = 99900

    def event(self, kind, ts, **kwargs):
        with patch.object(sm, '_now', return_value=ts):
            return sm.record_event(kind, ts=ts, **kwargs)

    def release(self, **overrides):
        metadata = dict(deployed_at=self.release_at,
                        binding=engineering_contracts.binding())
        metadata.update(overrides)
        (self.root / 'observation_release.json').write_text(
            json.dumps(metadata), encoding='utf-8')

    def report(self, **kwargs):
        return phase_audit.audit_readiness(self.root, now=self.at, **kwargs)

    def checks(self, report, history=False):
        return {c['key']: c for c in report['phase25'][
            'history_checks' if history else 'checks']}

    def deficient_rows(self, at=99800):
        event = self.event('consolidation', ts=at, source='journal')
        sm.record_action('dm', state='accepted', ts=at)
        sm.add_belief('old unsupported belief', ts=at)
        sm.note_self_view('old aggregate revision', reason='reflection',
                          evidence=[event], ts=at)

    def snapshot(self):
        with closing(sqlite3.connect(str(self.root / 'cognition.db'))) as c:
            return '\n'.join(c.iterdump())

    def test_immutable_old_defects_stay_in_history_and_do_not_become_samples(self):
        self.deficient_rows()
        self.release()
        before = self.snapshot()
        digest = hashlib.sha256((self.root / 'cognition.db').read_bytes()).hexdigest()
        report = self.report()
        current, history = self.checks(report), self.checks(report, history=True)
        for key in ('action_evidence', 'action_identity', 'action_results',
                    'belief_grounding', 'self_view_grounding'):
            self.assertEqual('not_observed', current[key]['status'], key)
            self.assertEqual('blocked', history[key]['status'], key)
        self.assertEqual('not_observed', report['phase25']['observation']['status'])
        self.assertEqual(self.release_at, report['phase25']['observation']['boundary_at'])
        self.assertIn('histor', report['phase25']['history_label'].lower())
        self.assertEqual(self.at - 86400, report['phase25']['history_boundary_at'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(digest, hashlib.sha256((self.root / 'cognition.db').read_bytes()).hexdigest())
        self.assertFalse(report['stage3']['authorized'])
        self.assertFalse(report['stage3']['expression_control'])
        self.assertEqual('blocked', report['stage3']['stage3b'])

    def test_new_bad_rows_block_instead_of_waiting_or_repairing_old_rows(self):
        self.deficient_rows(at=99950)
        self.release()
        report = self.report()
        for key in ('action_evidence', 'action_identity', 'action_results',
                    'belief_grounding', 'self_view_grounding'):
            self.assertEqual('blocked', self.checks(report)[key]['status'])
        self.assertEqual('blocked', report['phase25']['observation']['status'])

    def test_release_boundary_requires_new_events_not_old_refs_or_late_receipts(self):
        old = self.event('private_message', ts=99800, source='qq')
        action = sm.record_action('dm', target_qq='19', topic_qq='19',
                                  evidence=[old], state='accepted', ts=99950)
        self.event('action_result', ts=99810, source='onebot',
                        payload={'action_id': action, 'state': 'accepted'})
        sm.add_belief('new claim with old evidence', evidence=[old], ts=99950)
        sm.note_self_view('new revision with old evidence', evidence=[old], ts=99950)
        self.release()
        checks = self.checks(self.report())
        self.assertEqual(1, checks['action_evidence']['observed']['unusable_refs'])
        self.assertEqual('blocked', checks['action_results']['status'])
        self.assertEqual('blocked', checks['belief_grounding']['status'])
        self.assertEqual('blocked', checks['self_view_grounding']['status'])

    def test_created_not_updated_defines_the_release_population(self):
        action = sm.record_action('dm', ts=99800)
        belief = sm.add_belief('old belief later edited', ts=99800)
        with closing(sm._db()) as c, c:
            c.execute('UPDATE action SET updated=? WHERE id=?', (99950, action))
            c.execute('UPDATE belief SET last_ts=? WHERE id=?', (99950, belief))
        self.release()
        report = self.report()
        self.assertEqual('not_observed', self.checks(report)['action_evidence']['status'])
        self.assertEqual('not_observed', self.checks(report)['belief_grounding']['status'])
        self.assertEqual('blocked', self.checks(report, True)['action_evidence']['status'])
        self.assertEqual('blocked', self.checks(report, True)['belief_grounding']['status'])

    def test_rolling_start_wins_over_an_older_release(self):
        self.deficient_rows(at=1000)
        self.release(deployed_at=500)
        report = self.report(window_seconds=3600)
        self.assertEqual(96400, report['phase25']['observation']['boundary_at'])
        self.assertEqual('not_observed', self.checks(report)['belief_grounding']['status'])
        self.assertEqual('blocked', self.checks(report, True)['belief_grounding']['status'])

    def test_invalidated_beliefs_are_excluded_but_active_defects_still_block(self):
        event = self.event('private_message', ts=99920, source='qq')
        withdrawn = sm.add_belief('withdrawn claim', evidence=[event], ts=99930)
        sm.retract_evidence(event, 'explicit withdrawal')
        self.release()
        report = self.report()
        for checks in (self.checks(report), self.checks(report, True)):
            b = checks['belief_grounding']
            self.assertEqual('not_observed', b['status'])
            self.assertEqual(0, b['observed']['nonlegacy'])
            self.assertEqual(1, b['observed']['excluded_invalidated'])
            self.assertEqual(1, b['observed']['excluded'])
            self.assertEqual({'invalidated': 1}, b['observed']['statuses'])
        sm.add_belief('active unsupported claim', ts=99940)
        report = self.report()
        self.assertEqual('blocked', self.checks(report)['belief_grounding']['status'])
        with closing(phase_audit._ro(self.root / 'cognition.db')) as c:
            self.assertEqual('invalidated', c.execute(
                'SELECT status FROM belief WHERE id=?', (withdrawn,)).fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM evidence_retraction').fetchone()[0])

    def test_active_belief_with_invalidation_marker_is_not_silently_excluded(self):
        event = self.event('private_message', ts=99920, source='qq')
        belief = sm.add_belief('active inconsistent claim', evidence=[event], ts=99930)
        with closing(sm._db()) as c, c:
            c.execute('INSERT INTO cognition_invalidation VALUES(?,?,?)',
                      ('belief', belief, event))
        self.release()
        check = self.checks(self.report())['belief_grounding']
        self.assertEqual('blocked', check['status'])
        self.assertEqual(0, check['observed']['excluded_invalidated'])

    def test_invalid_metadata_retains_original_population_and_fails_closed(self):
        self.deficient_rows()
        original = self.checks(self.report())
        bad = [dict(deployed_at=True), dict(deployed_at=0), dict(deployed_at=-1),
               dict(deployed_at=self.at + 1), dict(deployed_at='99900'),
               dict(deployed_at=float('nan')), dict(deployed_at=float('inf')),
               dict(binding={}), dict(binding=None)]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                self.release(**overrides)
                report = self.report()
                self.assertFalse(report['phase25']['observation_release']['valid'])
                self.assertEqual(original, self.checks(report))
                self.assertEqual('blocked', report['phase25']['status'])
        for text in ('{}', '[]', '{broken', '{"deployed_at": 99900}'):
            with self.subTest(text=text):
                (self.root / 'observation_release.json').write_text(text, encoding='utf-8')
                report = self.report()
                self.assertFalse(report['phase25']['observation_release']['valid'])
                self.assertEqual(original, self.checks(report))

    def test_changed_code_binding_invalidates_previously_valid_release(self):
        self.deficient_rows()
        self.release()
        changed = dict(engineering_contracts.binding())
        changed['files'] = dict(changed['files'], **{'bot/phase_audit.py': 'changed'})
        with patch.object(engineering_contracts, 'binding', return_value=changed):
            report = self.report()
        self.assertFalse(report['phase25']['observation_release']['valid'])
        self.assertEqual('binding_mismatch', report['phase25']['observation_release']['reason'])
        self.assertEqual('blocked', self.checks(report)['belief_grounding']['status'])

    def test_synthetic_samples_cannot_satisfy_new_live_grounding(self):
        event = self.event('private_message', ts=99920, source='synthetic:probe',
                                payload={'synthetic': True})
        sm.record_action('dm', target_qq='19', topic_qq='19', evidence=[event], ts=99930)
        sm.add_belief('claim based on synthetic input', evidence=[event], ts=99930)
        sm.note_self_view('view based on synthetic input', evidence=[event], ts=99930)
        self.release()
        report = self.report()
        for key in ('action_evidence', 'belief_grounding', 'self_view_grounding'):
            self.assertEqual('blocked', self.checks(report)[key]['status'])
        self.assertEqual('blocked', report['phase25']['observation']['status'])

    def test_real_new_samples_can_be_observed_but_never_authorize_stage_three(self):
        event = self.event('private_message', ts=99920, source='qq')
        action = sm.record_action('dm', target_qq='19', topic_qq='19', evidence=[event],
                                  state='accepted', ts=99930)
        self.event('action_result', ts=99940, source='onebot',
                        payload={'action_id': action, 'state': 'accepted'})
        sm.add_belief('grounded claim', evidence=[event], ts=99930)
        sm.note_self_view('grounded view', evidence=[event], reason='real input', ts=99930)
        self.release()
        report = self.report()
        for key in ('action_evidence', 'action_identity', 'action_results',
                    'belief_grounding', 'self_view_grounding'):
            self.assertEqual('pass', self.checks(report)[key]['status'])
        self.assertEqual('observed', report['phase25']['observation']['status'])
        self.assertFalse(report['stage3']['authorized'])
        self.assertFalse(report['stage3']['expression_control'])

    def test_absent_release_keeps_old_checks_and_never_creates_metadata(self):
        self.deficient_rows()
        report = self.report()
        self.assertFalse((self.root / 'observation_release.json').exists())
        self.assertEqual('missing', report['phase25']['observation_release']['reason'])
        self.assertEqual('not_configured', report['phase25']['observation']['status'])
        self.assertEqual(report['phase25']['checks'], report['phase25']['history_checks'])
        self.assertEqual('blocked', self.checks(report)['belief_grounding']['status'])

    def test_event_creation_time_not_occurrence_time_defines_new_evidence(self):
        with patch.object(sm, '_now', return_value=99920):
            event = sm.record_event('private_message', ts=99800, source='qq')
        sm.add_belief('newly recorded evidence', evidence=[event], ts=99930)
        self.release()
        self.assertEqual('pass', self.checks(self.report())['belief_grounding']['status'])

    def test_old_corrupt_receipt_remains_visible_in_history_not_new_samples(self):
        event = self.event('action_result', ts=99800, source='onebot')
        with closing(sm._db()) as c, c:
            c.execute("UPDATE event SET payload='not-json' WHERE id=?", (event,))
        self.release()
        report = self.report()
        self.assertEqual('unknown', self.checks(report, True)['cognition_evidence']['status'])
        self.assertEqual('not_observed', self.checks(report)['action_results']['status'])
        self.assertEqual('not_observed', report['phase25']['observation']['status'])

    def test_unreadable_marker_encoding_fails_closed(self):
        self.deficient_rows()
        (self.root / 'observation_release.json').write_bytes(b'\xff')
        report = self.report()
        self.assertFalse(report['phase25']['observation_release']['valid'])
        self.assertEqual('blocked', self.checks(report)['belief_grounding']['status'])

    def test_current_ledger_time_violation_is_not_hidden_by_a_valid_boundary(self):
        self.event('private_message', ts=99930, source='qq')
        with closing(sm._db()) as c, c:
            c.execute('UPDATE event SET ts=created+5')
        self.release()
        report = self.report()
        self.assertEqual('blocked', self.checks(report)['ledger_integrity']['status'])
        self.assertEqual('blocked', report['phase25']['observation']['status'])

    def test_old_integrity_violation_stays_history_without_poisoning_new_scope(self):
        self.event('private_message', ts=99800, source='qq')
        with closing(sm._db()) as c, c:
            c.execute('UPDATE event SET ts=created+5')
        self.release()
        report = self.report()
        self.assertFalse(report['ok'])
        self.assertEqual('blocked', self.checks(report, True)['ledger_integrity']['status'])
        self.assertEqual('pass', self.checks(report)['ledger_integrity']['status'])
        self.assertEqual('not_observed', report['phase25']['observation']['status'])

    def test_corrupt_current_evidence_is_unknown_not_an_empty_success(self):
        sm.record_action('dm', ts=99930)
        with closing(sm._db()) as c, c:
            c.execute("UPDATE action SET event_ids='not-json'")
        self.release()
        report = self.report()
        self.assertEqual('unknown', self.checks(report)['cognition_evidence']['status'])
        self.assertEqual('blocked', report['phase25']['observation']['status'])


if __name__ == '__main__':
    unittest.main()
