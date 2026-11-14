import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import self_model as sm
import persona_growth as pg


class Continuity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, DATA_DIR=self.tmp.name, PERSONA_DATA_DIR='', QQ_DRY_RUN='1')
        env.start()
        self.addCleanup(env.stop)
        def close_store():
            import persona_store
            if persona_store._conn is not None:
                persona_store._conn.close()
                persona_store._conn = None
        self.addCleanup(close_store)

    def test_persona_save_noop(self):
        self.assertTrue(pg.save({'self_view': 'continuous', 'voice': 'modern'}))
        first = sm.layer_history('personality')
        self.assertTrue(pg.save({'voice': 'modern', 'self_view': 'continuous'}))
        self.assertEqual(first, sm.layer_history('personality'))
        self.assertTrue(pg.save({'self_view': 'revised', 'voice': 'modern'}))
        self.assertEqual(2, len(sm.layer_history('personality')))

    def test_ingest_rejects_unaddressed_quoted_roleplay_and_retracted(self):
        for extra in ({'addressed_to_self': False}, {'quoted': True}, {'roleplay': True}):
            payload = {'text': '我喜欢蓝色', 'addressed_to_self': True, **extra}
            event = sm.record_event('private_message', actor_qq='19', payload=payload)
            self.assertEqual('', sm.ingest_explicit_belief(event))
        for text in ('他说我喜欢蓝色', '我喜欢蓝色？', '“我喜欢蓝色”', '我可能喜欢蓝色'):
            event = sm.record_event('private_message', actor_qq='19',
                                    payload={'text': text, 'addressed_to_self': True})
            self.assertEqual('', sm.ingest_explicit_belief(event))
        event = sm.record_event('private_message', actor_qq='19',
                                payload={'text': '我喜欢蓝色', 'addressed_to_self': True})
        belief = sm.ingest_explicit_belief(event)
        self.assertTrue(belief)
        self.assertEqual(belief, sm.ingest_explicit_belief(event))
        sm.retract_evidence(event, 'withdrawn')
        self.assertEqual('', sm.ingest_explicit_belief(event))

    def test_artifact_validation_rejects_tampering_staleness_and_failed_cases(self):
        import hashlib
        import json
        import time
        import engineering_contracts as contracts
        path = Path(self.tmp.name) / 'engineering_contracts.json'
        binding = {'files': {'tests/test_contract_continuity.py': 'original'}}
        valid = {'origin': 'isolated_mock_transport', 'ts': time.time(),
                 'binding': binding, 'cases': {key: {'status': 'pass', 'tests': tests,
                     'output_sha256': 'a' * 64,
                     'execution': {'run': len(tests), 'skipped': 0, 'failures': 0, 'errors': 0,
                                   'expected_failures': 0, 'unexpected_successes': 0}}
                     for key, tests in contracts.CASES.items()}}
        def write(data):
            data = dict(data)
            data['sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            path.write_text(json.dumps(data), encoding='utf-8')
        with patch.object(contracts, 'binding', return_value=binding):
            write(valid)
            self.assertTrue(contracts.read_evidence(self.tmp.name)[0])
            write({**valid, 'ts': time.time() - 3 * 86400})
            self.assertTrue(contracts.read_evidence(self.tmp.name)[0])
            for changes in ({'ts': 0}, {'origin': 'live'}, {'cases': {}},
                            {'binding': {'files': {'tests/test_contract_continuity.py': 'changed'}}},
                            {'cases': {key: {'status': 'fail'} for key in contracts.CASES}}):
                write({**valid, **changes})
                self.assertFalse(contracts.read_evidence(self.tmp.name)[0])
            write(valid)
            data = json.loads(path.read_text(encoding='utf-8'))
            data['ts'] = 0
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertIn('hash mismatch', contracts.read_evidence(self.tmp.name)[1])
        actual = contracts.binding()['files']
        self.assertIn('tests/test_contract_continuity.py', actual)
        self.assertIn('bot/llm_router.py', actual)

    def test_model_switch_preserves_cognition(self):
        event = sm.record_event('private_message', actor_qq='19', payload={'text': 'explicit'})
        belief = sm.add_belief('explicit', evidence=[event])
        layer = sm.revise_layer('identity', {'name': '清漪'}, 'initial')
        original = sm.explain_belief(belief)
        import llm_router
        with patch.dict(llm_router._runtime_overrides, clear=True):
            for model in ('provider-a', 'provider-b'):
                llm_router.set_model_override('text', 'https://example.invalid/v1',
                                              'isolated-test-key', model)
                self.assertEqual(model, llm_router.get_effective_model('text').model)
                self.assertEqual(original, sm.explain_belief(belief))
                self.assertEqual(layer, sm.layer_history('identity')[0]['content_hash'])
                self.assertTrue(sm.evidence_valid('event', event))
        self.assertTrue(sm.retract_evidence(event, 'corrected'))
        self.assertFalse(sm.evidence_valid('event', event))
        self.assertFalse(sm.evidence_valid('event', 'missing'))
