import importlib.util
import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / 'bot'
sys.path.insert(0, str(BOT))


class ShadowDynamicsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('emotion_dynamics'), 'versioned evidence appraisal is missing')
        import emotion_dynamics
        self.core = emotion_dynamics

    def event(self, text='', **payload):
        return {'id': 'one', 'kind': 'message', 'ts': 1001, 'source': 'onebot',
                'payload': {'text': text, **payload}}

    def test_directed_interaction_updates_activation_but_not_valence(self):
        result = self.core.appraise(self.event('请帮我看看这件事', addressed_to_self=True))
        self.assertGreater(result['delta']['arousal'], 0)
        self.assertEqual(result['delta']['valence'], 0)

    def test_third_party_emotion_does_not_become_her_emotion(self):
        result = self.core.appraise(self.event('我今天好难过', addressed_to_self=False))
        self.assertEqual(result['delta']['arousal'], 0)
        self.assertEqual(result['delta']['valence'], 0)

    def test_explicit_feedback_is_attributed_and_quote_safe(self):
        for text, want in [('谢谢你帮我查清楚了', 1), ('你刚才说错了', -1)]:
            direct = self.core.appraise(self.event(text, addressed_to_self=True))
            self.assertEqual(direct['delta']['valence'] > 0, want > 0)
            for flag in ('quoted', 'roleplay'):
                quoted = self.core.appraise(self.event(text, addressed_to_self=True, **{flag: True}))
                self.assertEqual(quoted['delta']['valence'], 0)

    def test_unknown_tool_outcome_does_not_boost_control(self):
        event = {'id': 'one', 'kind': 'tool_result', 'ts': 1001, 'payload': {'ok': None, 'outcome': 'unknown'}}
        result = self.core.appraise(event)
        self.assertEqual(result['delta']['control'], 0)
        self.assertTrue(result['ignored'])

    def test_tool_failure_affects_technical_control_not_social_valence(self):
        event = {'id': 'one', 'kind': 'tool_result', 'ts': 1001, 'payload': {'ok': False, 'outcome': 'failure'}}
        result = self.core.appraise(event)
        self.assertLess(result['delta']['control'], 0)
        self.assertEqual(result['delta']['valence'], 0)
        self.assertGreater(result['delta']['arousal'], 0)

    def test_legacy_parameters_are_not_mutated(self):
        import emotion_core
        self.assertEqual(emotion_core.VERSION, 'shadow-v1')
        self.assertEqual(emotion_core.parameter_hash(), 'ce7b83161e4baa1e4f738d6f1cecb50dcf876350ec848586f643634bdb49f796')
        self.assertNotEqual(self.core.parameter_hash(), emotion_core.parameter_hash())


if __name__ == '__main__':
    unittest.main()
