"""群聊回复要按 1~3 个气泡发，而不是一句一条 / 一句不动。

生产现场（主人报的）：群聊里她永远只发一条，哪怕有三句话也挤成一坨。
用户要的是「1~3 条随机，根据实际情况决策」，以后条数交给情感模块。
这里锁住拆分契约：按句子边界、条数随内容加权、内容一条都不能丢。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import ceylan_control_runtime as rt  # noqa: E402


def _norm(s):
    return re.sub(r"\s+", "", str(s or ""))


class BubbleSplitTests(unittest.TestCase):
    def test_empty_returns_no_bubbles(self):
        self.assertEqual(rt.split_bubbles(""), [])
        self.assertEqual(rt.split_bubbles("   "), [])

    def test_single_sentence_stays_one_bubble(self):
        self.assertEqual(rt.split_bubbles("才不是呢"), ["才不是呢"])
        self.assertEqual(rt.split_bubbles("我才没有。"), ["我才没有。"])

    def test_content_is_never_lost(self):
        for text in (
            "才不是呢。都是你自己瞎想的！",
            "唔……刚才在打游戏。别吵。等一下再说吧。",
            "你好\n再见\n欢迎再来",
        ):
            with self.subTest(text=text):
                out = rt.split_bubbles(text, rng=__import__("random").Random(1))
                self.assertEqual(_norm("".join(out)), _norm(text))

    def test_two_sentences_yield_one_or_two(self):
        text = "才不是呢。都是你自己瞎想的！"
        for seed in range(20):
            import random
            out = rt.split_bubbles(text, rng=random.Random(seed))
            self.assertIn(len(out), (1, 2), out)

    def test_many_sentences_yield_one_to_three(self):
        text = "第一句。第二句！第三句。第四句？"
        seen = set()
        for seed in range(60):
            import random
            out = rt.split_bubbles(text, rng=random.Random(seed))
            self.assertIn(len(out), (1, 2, 3), out)
            seen.add(len(out))
        # 随机得真的会落在 1、2、3 上，而不是永远卡在一条
        self.assertEqual(seen, {1, 2, 3}, seen)

    def test_explicit_target_is_honoured(self):
        text = "第一句。第二句！第三句。第四句？"
        out = rt.split_bubbles(text, target=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(_norm("".join(out)), _norm(text))

    def test_target_clamped_to_available_sentences(self):
        out = rt.split_bubbles("只有一句。", target=3)
        self.assertEqual(out, ["只有一句。"])


if __name__ == "__main__":
    unittest.main()
