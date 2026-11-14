import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import emotion_shadow as sh


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='shadow_history_')
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        environment = patch.dict(os.environ, {'PERSONA_DATA_DIR': self.tmp,
                                              'EMOTION_SHADOW_ENABLED': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = time.time()
        tm = time.localtime(self.now)
        self.midnight = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))
        self.source = Path(self.tmp) / 'cognition.db'
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,session TEXT,actor_qq TEXT,topic_qq TEXT,source TEXT,visibility TEXT,payload TEXT,dedupe TEXT,created REAL)')

    def add(self, name, at, session='private:one', kind='private_message', payload=None):
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (name, kind, at, session, '', '', 'onebot', 'private',
                       json.dumps(payload or {'text': '谢谢你', 'addressed_to_self': True}), name, at))

    def test_today_is_replayed_but_never_becomes_live_observation(self):
        self.add('yesterday', self.midnight - 1)
        self.add('today', self.midnight + 1)
        self.add('test', self.midnight + 2, session='verify:fixture')
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        report = sh.status(now=self.now + 2)
        self.assertIn('retrospective', report)
        history = report['retrospective']
        self.assertFalse(history['counts_for_live'])
        self.assertEqual(history['mode'], 'retrospective')
        self.assertEqual(history['events'], 2)
        self.assertEqual(history['effective_events'], 1)
        self.assertEqual(history['excluded_events'], 1)
        self.assertEqual(history['source_start'], self.midnight)
        self.assertEqual(history['source_end'], self.now)
        self.assertEqual(report['processed'], 0)
        self.assertEqual(report['real_events'], 0)
        self.assertEqual(report['state']['valence'], 0)
        self.assertEqual(report['diagnostics']['effective_events'], 0)
        self.assertEqual(report['observed_seconds'], 1)

    def test_new_messages_remain_live_and_history_is_frozen_at_start(self):
        self.add('before', self.midnight + 1)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        first = sh.status(now=self.now + 1)['retrospective']
        self.add('after', self.now + 2)
        sh.tick(now=self.now + 3)
        second = sh.status(now=self.now + 3)
        self.assertEqual(second['retrospective'], first)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['diagnostics']['effective_events'], 1)

    def test_previous_night_in_legacy_window_is_backfilled_without_rewriting_it(self):
        import emotion_core as legacy
        legacy_path = Path(self.tmp) / 'emotion_shadow.db'
        with closing(sqlite3.connect(legacy_path)) as c, c:
            c.executescript(sh._SCHEMA)
            at = self.midnight - 3600
            c.execute('INSERT INTO shadow_run(id,parameter_hash,parameters,observation_started,last_tick,cursor,state) VALUES(1,?,?,?,?,?,?)',
                      (legacy.parameter_hash(), json.dumps(legacy.PARAMETERS), at, at, 0,
                       json.dumps(legacy.initial_state(at))))
        before = legacy_path.read_bytes()
        self.add('last-night', self.midnight - 1800)
        self.add('earlier-that-day', self.midnight - 7200)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        history = sh.status(now=self.now + 2)['retrospective']
        self.assertEqual(history['events'], 2)
        self.assertLess(history['source_start'], self.midnight)
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_history_failure_cannot_stop_real_time_event_consumption(self):
        sh.tick(now=self.now)
        self.add('live', self.now + 1)
        with patch.object(sh, '_backfill_day', side_effect=sqlite3.OperationalError('history unavailable')):
            result = sh.tick(now=self.now + 2)
        self.assertEqual(result['processed'], 1)
        report = sh.status(now=self.now + 3)
        self.assertEqual(report['processed'], 1)
        self.assertIn('error', report['retrospective'])

    def test_recursive_history_error_does_not_stop_live_consumer(self):
        sh.tick(now=self.now)
        self.add('live', self.now + 1)
        with patch.object(sh, '_backfill_day', side_effect=RecursionError('malformed history')):
            result = sh.tick(now=self.now + 2)
        self.assertEqual(result['processed'], 1)

    def test_deeply_nested_bad_history_cannot_block_new_live_events(self):
        self.add('bad', self.midnight + 1)
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('UPDATE event SET payload=? WHERE id=?',
                      ('{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}', 'bad'))
        sh.tick(now=self.now)
        self.add('live', self.now + 1)
        result = sh.tick(now=self.now + 2)
        self.assertEqual(result['processed'], 1)
        self.assertTrue(sh.status(now=self.now + 3)['running'])

    def test_read_only_status_does_not_generate_history_or_change_ledgers(self):
        self.add('before', self.midnight + 1)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        path = sh.shadow_path()
        old = path.read_bytes()
        for _ in range(2):
            sh.status(now=self.now + 2)
        self.assertEqual(path.read_bytes(), old)

    def test_history_contains_no_raw_messages_or_personal_identifiers(self):
        self.add('one', self.midnight + 1, session='private:secret-user',
                 payload={'text': 'secret-message', 'addressed_to_self': True})
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        text = json.dumps(sh.status(now=self.now + 2)['retrospective'])
        self.assertNotIn('secret-message', text)
        self.assertNotIn('secret-user', text)


if __name__ == '__main__':
    unittest.main()
