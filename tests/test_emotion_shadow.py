import importlib
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


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shadow_test_')
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name,
                                           'PERSONA_DATA_DIR': str(Path(self.tmp.name) / 'persona')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.assertIsNotNone(importlib.util.find_spec('emotion_shadow'),
                             '影子消费器尚未实现')
        self.s = importlib.import_module('emotion_shadow')
        self.source = Path(self.tmp.name) / 'persona' / 'cognition.db'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,session TEXT,actor_qq TEXT,topic_qq TEXT,source TEXT,visibility TEXT,payload TEXT,dedupe TEXT,created REAL)')

    def add_event(self, eid='e1', ts=1001, kind='private_message', source='', payload=None):
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (eid, kind, ts, 'private:1', 'per_1', '', source, 'private',
                       json.dumps(payload or {'text': '你好'}), eid, ts))

    def test_read_api_does_not_create_database(self):
        info = self.s.status(now=1000)
        self.assertFalse(info['running'])
        self.assertFalse(Path(self.s.shadow_path()).exists())

    def test_first_tick_skips_historical_rows_and_starts_observation_clock(self):
        self.add_event('old', ts=50)
        result = self.s.tick(now=1000)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['cursor'], 1)
        self.assertEqual(self.s.status(now=1000)['observation_started'], 1000)
        self.add_event()
        self.assertEqual(self.s.tick(now=1002)['processed'], 1)

    def test_duplicate_ticks_are_idempotent_and_restart_retains_state(self):
        self.s.tick(now=1000)
        self.add_event()
        self.s.tick(now=1002)
        self.s.tick(now=1002)
        info = self.s.status(now=1002)
        self.assertEqual(info['processed'], 1)
        self.assertEqual(len(info['timeline']), 1)
        self.assertEqual(self.s.tick(now=1002)['processed'], 1)

    def test_live_consumer_refuses_synthetic_input(self):
        self.s.tick(now=1000)
        self.add_event('synth', source='synthetic', payload={'text': '谢谢你', 'addressed_to_self': True})
        self.s.tick(now=1002)
        info = self.s.status(now=1002)
        self.assertEqual(info['state']['valence'], 0)
        self.assertTrue(info['timeline'][0]['ignored'])
        self.assertEqual(info['real_events'], 0)

    def test_late_event_is_not_lost_due_to_timestamp_cursor(self):
        self.s.tick(now=1000)
        self.add_event('late', ts=500)
        self.s.tick(now=1002)
        info = self.s.status(now=1002)
        self.assertEqual(info['processed'], 1)
        self.assertEqual(info['timeline'][0]['event_id'], 'late')
        self.assertGreaterEqual(info['state']['at'], 1000)

    def test_batch_bounds_do_not_reset_cursor(self):
        self.s.tick(now=1000)
        for i in range(5):
            self.add_event('batch' + str(i), ts=1001 + i)
        self.assertEqual(self.s.tick(now=1006, batch_size=2)['processed'], 2)
        self.assertEqual(self.s.tick(now=1006, batch_size=2)['processed'], 4)
        self.assertEqual(self.s.tick(now=1006, batch_size=2)['processed'], 5)

    def test_bad_payload_is_quarantined_without_blocking_following_event(self):
        self.s.tick(now=1000)
        self.add_event('bad')
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('UPDATE event SET payload=? WHERE id=?', ('{bad', 'bad'))
        self.add_event('good', ts=1002)
        self.s.tick(now=1003)
        info = self.s.status(now=1003)
        self.assertEqual(info['processed'], 2)
        self.assertTrue(info['timeline'][0]['ignored'])
        self.assertEqual(info['timeline'][1]['event_id'], 'good')

    def test_rollback_keeps_cursor_and_state_together(self):
        self.s.tick(now=1000)
        self.add_event()
        real_apply = self.s.core.apply_event
        with patch.object(self.s.core, 'apply_event', side_effect=RuntimeError('test crash')):
            with self.assertRaises(RuntimeError):
                self.s.tick(now=1002)
        self.assertEqual(self.s.status(now=1002)['cursor'], 0)
        self.assertEqual(self.s.status(now=1002)['processed'], 0)
        with patch.object(self.s.core, 'apply_event', real_apply):
            self.assertEqual(self.s.tick(now=1002)['processed'], 1)

    def test_source_is_read_only_and_no_new_facts_or_actions_are_written(self):
        self.s.tick(now=1000)
        self.add_event()
        before = self.source.read_bytes()
        self.s.tick(now=1002)
        self.s.status(now=1002)
        self.assertEqual(self.source.read_bytes(), before)
        with closing(sqlite3.connect(self.source)) as c, c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'event'})

    def test_frozen_parameter_mismatch_blocks_without_reset(self):
        self.s.tick(now=1000)
        with patch.object(self.s.core, 'parameter_hash', return_value='different'):
            with self.assertRaises(ValueError):
                self.s.tick(now=1001)
        self.assertEqual(self.s.status(now=1001)['processed'], 0)

    def test_synthetic_source_prefix_never_updates_live_emotion(self):
        self.s.tick(now=1000)
        self.add_event('synth_prefix', source='synthetic/import',
                       payload={'text': '谢谢你', 'addressed_to_self': True})
        self.s.tick(now=1002)
        info = self.s.status(now=1002)
        self.assertEqual(info['real_events'], 0)
        self.assertEqual(info['state']['valence'], 0)
        self.assertEqual(info['timeline'][0]['reason'], 'not_live_evidence')

    def test_future_event_never_advances_or_changes_state(self):
        self.s.tick(now=1000)
        self.add_event('future', ts=1050, kind='tool_result', payload={'ok': True})
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('UPDATE event SET created=1001 WHERE id=?', ('future',))
        self.s.tick(now=1002)
        info = self.s.status(now=1002)
        self.assertEqual(info['state']['control'], 0.5)
        self.assertTrue(info['timeline'][0]['ignored'])
        self.assertEqual(info['real_events'], 0)

    def test_future_classification_is_based_on_recorded_intake_not_poll_time(self):
        self.s.tick(now=1000)
        self.add_event('first', ts=1001)
        self.add_event('future', ts=1040, kind='tool_result', payload={'ok': True})
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('UPDATE event SET created=1001 WHERE id=?', ('future',))
        self.s.tick(now=1002, batch_size=1)
        self.s.tick(now=1062, batch_size=1)
        info = self.s.status(now=1062)
        self.assertEqual(info['state']['control'], 0.5)
        self.assertEqual(info['timeline'][1]['reason'], 'invalid_event_time')

    def test_source_with_future_recording_clock_waits_without_consuming(self):
        self.s.tick(now=1000)
        self.add_event('not-yet-recorded', ts=1050)
        result = self.s.tick(now=1002)
        self.assertEqual(result.get('deferred'), 1)
        self.assertEqual(self.s.status(now=1002)['processed'], 0)
        self.s.tick(now=1062)
        self.assertEqual(self.s.status(now=1062)['processed'], 1)

    def test_bad_timestamp_is_quarantined_and_next_event_is_processed(self):
        self.s.tick(now=1000)
        self.add_event('bad_ts', ts='nonsense')
        self.add_event('good', ts=1001)
        self.s.tick(now=1002)
        info = self.s.status(now=1002)
        self.assertEqual(info['processed'], 2)
        self.assertTrue(info['timeline'][0]['ignored'])
        self.assertEqual(info['timeline'][1]['rule'], 'message_attention')

    def test_offline_gap_counts_zero_observation_seconds(self):
        self.s.tick(now=1000)
        self.s.tick(now=1000 + 86400)
        info = self.s.status(now=1000 + 86400)
        self.assertEqual(info['observed_seconds'], 0)
        self.assertEqual(info['gap_seconds'], 86400)

    def test_parameter_mismatch_is_not_rendered_with_current_dynamics(self):
        self.s.tick(now=1000)
        with patch.object(self.s.core, 'parameter_hash', return_value='different'):
            with self.assertRaises(ValueError):
                self.s.status(now=1001)

    def test_persisted_trajectory_replays_after_late_events_and_quarantine(self):
        self.s.tick(now=1000)
        self.add_event('first', ts=1001, kind='tool_result', payload={'ok': True})
        self.s.tick(now=1002)
        self.add_event('late', ts=500, kind='tool_result', payload={'ok': False})
        self.add_event('bad', ts='not-a-time')
        self.s.tick(now=1003)
        self.assertTrue(callable(getattr(self.s, 'verify_replay', None)),
                        '已持久轨迹还不能复核重放')
        check = self.s.verify_replay()
        self.assertTrue(check['ok'], check)
        self.assertEqual(check['events'], 3)
        self.assertLessEqual(check['max_error'], 1e-10)

    def test_no_events_recovers_without_llm_or_state_personality_drift(self):
        self.s.tick(now=1000)
        self.s.tick(now=36000)
        info = self.s.status(now=36000)
        self.assertEqual(info['state']['valence'], 0)
        self.assertEqual(info['processed'], 0)
        self.assertEqual(info['mode'], 'shadow')
        self.assertFalse(info['expression_control'])
        # 校准现在是有条有据的状态，不是恒定的「未校准」标签：
        # 一条真实事件都没有时它是 collecting / 未达评审，绝不能是 review_ready。
        self.assertIn(info['calibration'], ('uncalibrated', 'collecting'))
        self.assertFalse(info['ready_for_review'])
        # 一条真实事件都没有：可重放天然成立，但事件量与驱动来源必须没过
        detail = info['calibration_detail']
        self.assertEqual(detail['total'], 10)
        self.assertLess(detail['passed'], detail['total'])
        passed = set(c['key'] for c in detail['checks'] if c['ok'])
        self.assertNotIn('replay', passed)
        self.assertFalse(passed & {'events', 'kinds', 'window'})


if __name__ == '__main__':
    unittest.main()
