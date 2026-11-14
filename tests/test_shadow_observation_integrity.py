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
sys.path.insert(0, str(BOT))
import emotion_shadow as sh


class ObservationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shadow_integrity_')
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {
            'DATA_DIR': self.tmp.name, 'PERSONA_DATA_DIR': self.tmp.name,
            'EMOTION_SHADOW_ENABLED': '1', 'EMOTION_CALIB_MIN_HOURS': '0',
            'EMOTION_CALIB_MIN_EVENTS': '1',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.source = Path(self.tmp.name) / 'cognition.db'
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,session TEXT,actor_qq TEXT,topic_qq TEXT,source TEXT,visibility TEXT,payload TEXT,dedupe TEXT,created REAL)')
        sh._CALIB_CACHE.update(key=None, ts=0.0, replay=None)
        sh.tick(now=1000)

    def add(self, ident, kind='private_message', at=1001, payload=None, session='private:one'):
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (ident, kind, at, session, '', '', 'onebot', 'private',
                       json.dumps(payload or {'text': 'hello', 'addressed_to_self': True}), ident, at))

    def check(self, report, key):
        return next(x for x in report['calibration_detail']['checks'] if x['key'] == key)

    def test_pending_source_records_cannot_pass_as_no_backlog(self):
        for i in range(3):
            self.add(str(i))
        report = sh.status(now=1005)
        self.assertFalse(self.check(report, 'backlog')['ok'])
        self.assertEqual(report['diagnostics']['backlog']['pending'], 3)

    def test_missing_source_is_unknown_not_zero(self):
        self.source.unlink()
        report = sh.status(now=1005)
        self.assertFalse(self.check(report, 'backlog')['ok'])
        self.assertEqual(self.check(report, 'backlog')['status'], 'unknown')

    def test_live_open_gap_is_counted_before_worker_returns(self):
        sh.tick(now=1005)
        report = sh.status(now=10000)
        self.assertFalse(self.check(report, 'continuity')['ok'])
        self.assertGreater(report['diagnostics']['continuity']['open_gap_seconds'], 8900)
        self.assertFalse(report['ready_for_review'])

    def test_ack_and_unsupported_records_do_not_become_emotional_drivers(self):
        self.add('msg')
        self.add('ack', 'action_result', payload={'state': 'accepted'})
        self.add('cons', 'consolidation', payload={'text': 'summary'})
        sh.tick(now=1005)
        report = sh.status(now=1006)
        self.assertFalse(self.check(report, 'kinds')['ok'])
        self.assertEqual(report['diagnostics']['effective_events'], 1)
        self.assertEqual(report['diagnostics']['ignored_events'], 2)
        self.assertEqual(report['diagnostics']['by_kind']['action_result']['effective'], 0)

    def test_verification_session_is_not_real_observation(self):
        self.add('verify', 'tool_result', payload={'tool': 'vision', 'ok': True}, session='verify:probe')
        sh.tick(now=1005)
        report = sh.status(now=1006)
        self.assertEqual(report['real_events'], 0)
        self.assertEqual(report['diagnostics']['excluded_events'], 1)
        self.assertEqual(report['state']['control'], 0.5)

    def test_empty_replay_is_not_evidence_of_readiness(self):
        report = sh.status(now=1005)
        self.assertFalse(self.check(report, 'replay')['ok'])
        self.assertEqual(self.check(report, 'replay')['status'], 'not_observed')

    def test_source_payload_tampering_is_caught_independently_of_stored_deltas(self):
        self.add('tool', 'tool_result', payload={'tool': 'search', 'ok': True})
        sh.tick(now=1005)
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('UPDATE event SET payload=? WHERE id=?',
                      (json.dumps({'tool': 'search', 'ok': False}), 'tool'))
        sh._CALIB_CACHE.update(key=None, ts=0.0, replay=None)
        report = sh.status(now=1006)
        self.assertFalse(report['diagnostics']['evidence_replay']['ok'])
        self.assertEqual(report['diagnostics']['evidence_replay']['appraisal_mismatches'], 1)
        self.assertFalse(report['ready_for_review'])

    def test_all_four_dimensions_need_observed_drivers(self):
        self.add('msg')
        self.add('tool', 'tool_result', payload={'tool': 'search', 'ok': True})
        sh.tick(now=1005)
        report = sh.status(now=1006)
        self.assertFalse(report['ready_for_review'])
        self.assertFalse(self.check(report, 'dimensions')['ok'])
        self.assertFalse(report['diagnostics']['dimensions']['valence']['observed'])
        self.assertFalse(report['expression_control'])
        self.assertFalse(report['stage3_ready'])

    def test_new_run_preserves_legacy_history_and_starts_separate_observation(self):
        import emotion_core as legacy
        with closing(sqlite3.connect(sh.shadow_path())) as c, c:
            c.execute('UPDATE shadow_run SET parameter_hash=?,parameters=?',
                      (legacy.parameter_hash(), json.dumps(legacy.PARAMETERS)))
        old = Path(self.tmp.name) / 'emotion_shadow.db'
        before = old.read_bytes()
        self.assertFalse(sh.status(now=1005)['running'])
        self.assertEqual(sh.shadow_path().name, 'emotion_shadow_v2.db')
        self.assertTrue(sh.tick(now=1005)['initialized'])
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(sh.verify_replay(old)['ok'])

    def test_one_successful_tool_is_not_diverse_outcome_coverage(self):
        self.add('msg', payload={'text': '谢谢你', 'addressed_to_self': True})
        self.add('tool', 'tool_result', payload={'tool': 'search', 'ok': True})
        sh.tick(now=1005)
        report = sh.status(now=1006)
        self.assertFalse(report['ready_for_review'])
        self.assertFalse(self.check(report, 'outcomes')['ok'])

    def test_read_snapshot_does_not_load_history_before_the_observation_start(self):
        statements = []
        readonly = sh._readonly
        def traced(path):
            connection = readonly(path)
            connection.set_trace_callback(statements.append)
            return connection
        self.add('msg')
        sh.tick(now=1005)
        with patch.object(sh, '_readonly', side_effect=traced):
            sh.status(now=1006)
        payload_queries = [sql.lower() for sql in statements if 'select rowid as source_rowid,* from event' in sql.lower()]
        self.assertTrue(payload_queries)
        self.assertTrue(all('rowid>=' in sql for sql in payload_queries), payload_queries)

    def test_unchanged_ledger_refresh_does_not_reassess_every_event(self):
        self.add('msg')
        sh.tick(now=1005)
        evaluator = sh._evaluation
        with patch.object(sh, '_evaluation', wraps=evaluator) as observed:
            first = sh.status(now=1006)
            second = sh.status(now=1007)
        self.assertEqual(observed.call_count, 1)
        self.assertGreater(second['diagnostics']['freshness']['heartbeat_age_seconds'],
                           first['diagnostics']['freshness']['heartbeat_age_seconds'])

    def test_nonfinite_stored_state_never_receives_a_green_replay(self):
        self.add('msg', payload={'text': '谢谢你', 'addressed_to_self': True})
        self.add('search', 'tool_result', payload={'tool': 'search', 'ok': True})
        self.add('vision', 'tool_result', payload={'tool': 'vision', 'ok': False})
        sh.tick(now=1005)
        self.assertTrue(sh.status(now=1006)['ready_for_review'])
        for number in (float('nan'), float('inf')):
            with closing(sqlite3.connect(sh.shadow_path())) as c, c:
                row = c.execute('SELECT after_state FROM shadow_event ORDER BY source_rowid DESC LIMIT 1').fetchone()
                state = json.loads(row[0]); state['attention'] = number
                c.execute('UPDATE shadow_event SET after_state=? WHERE source_rowid=(SELECT MAX(source_rowid) FROM shadow_event)',
                          (json.dumps(state),))
            with self.assertRaises(ValueError):
                sh.status(now=1007)

    def test_overflowing_json_float_does_not_bypass_finite_validation(self):
        self.add('msg')
        sh.tick(now=1005)
        with closing(sqlite3.connect(sh.shadow_path())) as c, c:
            row = c.execute('SELECT after_state FROM shadow_event').fetchone()
            state = json.loads(row[0]); state['attention'] = float('inf')
            encoded = json.dumps(state).replace('Infinity', '1e999')
            c.execute('UPDATE shadow_event SET after_state=?', (encoded,))
        with self.assertRaises(ValueError):
            sh.status(now=1006)

    def test_nonfinite_delta_is_not_hidden_by_error_aggregation(self):
        self.add('msg')
        sh.tick(now=1005)
        with closing(sqlite3.connect(sh.shadow_path())) as c, c:
            appraisal = json.loads(c.execute('SELECT appraisal FROM shadow_event').fetchone()[0])
            appraisal['delta']['attention'] = float('nan')
            c.execute('UPDATE shadow_event SET appraisal=?', (json.dumps(appraisal),))
        with self.assertRaises(ValueError):
            sh.status(now=1006)

    def test_unknown_only_tool_does_not_fill_a_missing_confirmed_channel(self):
        self.add('msg', payload={'text': '谢谢你', 'addressed_to_self': True})
        self.add('search-ok', 'tool_result', payload={'tool': 'search', 'ok': True})
        self.add('search-fail', 'tool_result', payload={'tool': 'search', 'ok': False})
        self.add('tarot-unknown', 'tool_result', payload={'tool': 'tarot', 'ok': None, 'outcome': 'unknown'})
        sh.tick(now=1005)
        report = sh.status(now=1006)
        self.assertFalse(self.check(report, 'outcomes')['ok'])

    def test_replay_cache_is_not_reused_between_databases(self):
        self.add('msg')
        sh.tick(now=1005)
        sh.status(now=1006)
        with closing(sqlite3.connect(sh.shadow_path())) as c, c:
            row = c.execute('SELECT after_state FROM shadow_event LIMIT 1').fetchone()
            state = json.loads(row[0]); state['attention'] += 0.1
            c.execute('UPDATE shadow_event SET after_state=?', (json.dumps(state),))
        report = sh.status(now=1007)
        self.assertFalse(self.check(report, 'replay')['ok'])


if __name__ == '__main__':
    unittest.main()
