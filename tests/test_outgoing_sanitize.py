import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import outgoing_sanitize as s  # noqa: E402


class StripControlMarkersTests(unittest.TestCase):
    def test_pure_marker_becomes_empty(self):
        # 事故：[[查: 2026年11月9日 重庆 运势]] 被当成消息发到群里
        self.assertEqual(s.strip_control_markers("[[查: 2026年11月9日 重庆 运势]]"), "")

    def test_marker_after_normal_text_is_removed(self):
        self.assertEqual(s.strip_control_markers("哼，扣一分。[[查: x]]"), "哼，扣一分。")

    def test_plain_text_unchanged(self):
        self.assertEqual(s.strip_control_markers("牌阵给你看：权杖三逆位、魔术师"),
                         "牌阵给你看：权杖三逆位、魔术师")

    def test_injected_prompt_hint_is_removed(self):
        # 注入的提示片段绝不能被当成她的话发出去
        self.assertEqual(s.strip_control_markers("【这句可能要查才知道 · 本轮先别编】"), "")
        self.assertEqual(s.strip_control_markers("先接住一句【查到会再补一条】后再说"), "先接住一句后再说")

    def test_empty_and_none(self):
        self.assertEqual(s.strip_control_markers(""), "")
        self.assertEqual(s.strip_control_markers(None), "")

    def test_collapses_whitespace_left_by_marker(self):
        self.assertEqual(s.strip_control_markers("行吧   [[斗图]]   走起"), "行吧 走起")


if __name__ == '__main__':
    unittest.main()