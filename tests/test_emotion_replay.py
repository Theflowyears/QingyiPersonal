import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emotion_replay_')
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.assertIsNotNone(importlib.util.find_spec('emotion_replay'),
                             '隔离历史回放尚未实现')
        self.r = importlib.import_module('emotion_replay')

    def test_history_without_turn_time_is_explicitly_inferred_and_synthetic(self):
        bundle = {'histories': {'private-user-1': {'updated': 1790000000,
                    'turns': [{'role': 'user', 'content': '你好'},
                              {'role': 'assistant', 'content': '嗯'}]}}}
        events = self.r.normalize(bundle)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['synthetic'] for e in events))
        self.assertTrue(all(e['time_quality'] == 'inferred_order' for e in events))
        self.assertTrue(all(e['source'] == 'synthetic' for e in events))
        self.assertTrue(all(e['ts'] < 10000 for e in events))

    def test_group_facts_are_legacy_records_not_relived_messages(self):
        bundle = {'group_facts': {'groups': {'1': {'entries': [
            {'id': 'f1', 'text': '主人不喜欢苦瓜', 'ts': 1000}]}}}}
        events = self.r.normalize(bundle)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'legacy_note')
        self.assertTrue(events[0]['synthetic'])

    def test_activity_outcomes_are_observations_not_proof_of_read_receipt(self):
        bundle = {'activity': [{'kind': 'search', 'ts': 1000,
                               'what': '塔罗', 'out': '搜索出错'},
                              {'kind': 'share', 'ts': 1001,
                               'what': '私聊', 'out': '主动讲了'}]}
        events = self.r.normalize(bundle)
        self.assertEqual(events[0]['kind'], 'tool_result')
        self.assertIs(events[0]['payload']['ok'], False)
        self.assertEqual(events[1]['kind'], 'action_result')
        self.assertNotIn('seen', events[1]['payload'])

    def test_normalization_ids_stay_stable_across_runs(self):
        bundle = {'activity': [{'kind': 'watch', 'ts': 1000, 'what': '一张图'}]}
        self.assertEqual(self.r.normalize(bundle), self.r.normalize(bundle))

    def test_reference_run_does_not_create_persona_or_live_shadow_database(self):
        bundle = {'histories': {'private-user-1': {'turns': [
            {'role': 'user', 'content': '谢谢你'}]}}}
        result = self.r.reference_report(bundle)
        self.assertEqual(result['mode'], 'synthetic_reference')
        self.assertEqual(result['trajectory'], 'v1_reference')
        self.assertFalse(result['counts_for_live'])
        self.assertFalse(result['parameters_tuned'])
        self.assertFalse((Path(self.tmp.name) / 'persona').exists())

    def test_report_contains_replay_granularity_and_stress_diagnostics(self):
        result = self.r.reference_report({'activity': [
            {'kind': 'search', 'ts': 1000, 'out': '记住了', 'what': '原型'}]})
        self.assertTrue(result['checks']['deterministic_replay'])
        self.assertTrue(result['checks']['tick_invariance'])
        self.assertTrue(result['checks']['bounded_stress'])
        self.assertTrue(result['checks']['quiet_recovery'])
        self.assertIn('parameter_hash', result)
        self.assertIn('source_counts', result)
        self.assertIn('limitations', result)

    def test_no_chat_content_or_qq_ids_leak_into_report(self):
        secret = '测试私密文本-不应进入报告'
        result = self.r.reference_report({'histories': {'private-user-1234567': {
            'turns': [{'role': 'user', 'content': secret}]}}})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('1234567', serialized)

    def test_mixed_known_and_unknown_times_do_not_create_decades_of_fake_history(self):
        result = self.r.reference_report({'histories': {'private-user-1': {'turns': [
            {'role': 'user', 'content': '你好'},
            {'role': 'user', 'content': '谢谢你', 'ts': 1790000000}]}}})
        scenarios = list(result['scenarios'].values())
        self.assertEqual(len(scenarios), 2)
        self.assertTrue(result['checks']['tick_invariance'])

    def test_report_never_overwrites_existing_files_even_when_named_as_db(self):
        path = Path(self.tmp.name) / 'cognition.db'
        path.write_bytes(b'real-database-placeholder')
        self.assertTrue(callable(getattr(self.r, 'save_report', None)), '安全报告写入尚未实现')
        with self.assertRaises((FileExistsError, ValueError)):
            self.r.save_report({'mode': 'synthetic_reference'}, path)
        self.assertEqual(path.read_bytes(), b'real-database-placeholder')

    def test_report_writer_rejects_non_json_target(self):
        self.assertTrue(callable(getattr(self.r, 'save_report', None)))
        with self.assertRaises(ValueError):
            self.r.save_report({'mode': 'synthetic_reference'}, Path(self.tmp.name) / 'new.db')

    def test_synthetic_event_cannot_be_promoted_by_input_flag(self):
        bundle = {'events': [{'id': 'real-id', 'kind': 'private_message',
                   'ts': 1000, 'source': 'live', 'synthetic': False,
                   'payload': {'text': '谢谢你', 'addressed_to_self': True}}]}
        events = self.r.normalize(bundle)
        self.assertTrue(events[0]['synthetic'])
        self.assertEqual(events[0]['source'], 'synthetic')


if __name__ == '__main__':
    unittest.main()
