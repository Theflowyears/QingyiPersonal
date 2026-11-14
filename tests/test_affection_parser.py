import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import affection as af  # noqa: E402


class AbstractDeltaTests(unittest.TestCase):
    def test_leaf_forever_phrases(self):
        # 用户点名要能识别的抽象表述（没数字）
        for txt, want in [("加一点", 1), ("减一点", -1),
                          ("好啦给你加回去", 1), ("哼，生气了，减一点", -1),
                          ("那就加一些吧", 1)]:
            got = af.parse_abstract_delta(txt)
            self.assertIsNotNone(got, txt)
            self.assertEqual(got[0], want, txt)
            self.assertTrue(got[1], txt)

    def test_unrelated_add_is_not_affection(self):
        # 「这道菜加一点盐」不是好感
        self.assertIsNone(af.parse_abstract_delta("这道菜加一点盐再翻炒一下出锅"))


class NoStrayNumberTests(unittest.TestCase):
    def test_tarot_reading_does_not_become_score(self):
        # 事故：100→3，牌面「权杖三逆位」的三被当成好感分
        for allow in (False, True):
            self.assertIsNone(af.parse_self_report("牌阵给你看：权杖三逆位、魔术师逆位", allow_bare=allow))
            self.assertIsNone(af.parse_bare_number("牌阵给你看：权杖三逆位、魔术师逆位"))

    def test_explicit_still_works(self):
        self.assertEqual(af.parse_self_report("好感度给你涨到八十")[0], 80)
        self.assertEqual(af.parse_self_report("好感度-20")[0], -20)


if __name__ == '__main__':
    unittest.main()