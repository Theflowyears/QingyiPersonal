import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
sys.path.insert(0, str(BOT))
import emotion_dynamics as core


def event(text, **payload):
    return {'id': 'e1', 'kind': 'message', 'ts': 1001, 'source': 'onebot',
            'payload': {'text': text, **payload}}


class ValenceEvidenceTests(unittest.TestCase):
    def test_explicit_affection_moves_valence_up(self):
        for text in ('最喜欢你啦宝宝', '爱你我', '辛苦了宝宝', '谢谢你帮我查清楚了'):
            got = core.appraise(event(text, addressed_to_self=True))
            self.assertGreater(got['delta']['valence'], 0, text)

    def test_explicit_rebuke_moves_valence_down(self):
        for text in ('你刚才说错了', '别烦我', '你真笨', '闭嘴吧'):
            got = core.appraise(event(text, addressed_to_self=True))
            self.assertLess(got['delta']['valence'], 0, text)

    def test_third_party_or_quoted_warmth_does_not_move_her(self):
        for payload in ({'addressed_to_self': False}, {'addressed_to_self': True, 'quoted': True},
                        {'addressed_to_self': True, 'is_roleplay': True}):
            got = core.appraise(event('最喜欢你啦宝宝', **payload))
            self.assertEqual(got['delta']['valence'], 0, payload)

    def test_vocabulary_is_not_part_of_frozen_dynamics(self):
        before = core.parameter_hash()
        self.assertTrue(core.AFFECTION_EVIDENCE)
        self.assertEqual(core.parameter_hash(), before)


if __name__ == '__main__':
    unittest.main()
