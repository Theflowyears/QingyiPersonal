# -*- coding: utf-8 -*-
"""本地占卜回归锁：真随机、一次给完、不走云端；搜索等云端链路不变。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import local_divination as ld
import toolkit


class LocalDivinationTests(unittest.TestCase):
    def test_tarot_complete_one_shot(self):
        r = ld.tarot("过去现在未来")
        self.assertTrue(r["ok"])
        for pos in ("过去", "现在", "未来"):
            self.assertIn(pos, r["text"])
        self.assertIn("正位", r["text"] + "逆位")  # 至少含 orientation 词
        self.assertTrue(("正位" in r["text"]) or ("逆位" in r["text"]))

    def test_tarot_spreads(self):
        self.assertIn("选项A", ld.tarot("二选一选哪个")["text"])
        self.assertIn("当前建议", ld.tarot("单张建议")["text"])

    def test_deck_is_78_and_draws_distinct(self):
        self.assertEqual(len(ld._deck()), 78)
        cards = ld._draw(3)
        self.assertEqual(len({c["name"] for c in cards}), 3)

    def test_zhouyi_hexagram(self):
        r = ld.zhouyi("问事业")
        self.assertTrue(r["ok"])
        self.assertIn("得卦", r["text"])
        self.assertIn("动爻", r["text"])

    def test_fortune_four_dims(self):
        r = ld.fortune("今天运势")
        for d in ("事业", "感情", "财运", "健康"):
            self.assertIn(d, r["text"])

    def test_unknown_key_not_ok(self):
        self.assertFalse(ld.run_local("nope")["ok"])


class ToolkitRoutingTests(unittest.TestCase):
    def test_divination_is_local(self):
        for k in ("tarot", "zhouyi", "fortune"):
            self.assertEqual(toolkit.TOOLS[k]["kind"], "local")

    def test_search_stays_cloud(self):
        # 真正依赖云端的链路不能被顺手改成本地。
        for k, cfg in toolkit.TOOLS.items():
            if k in ("tarot", "zhouyi", "fortune"):
                continue
            self.assertNotEqual(cfg.get("kind"), "local", k)

    def test_local_dispatch_returns_text(self):
        import asyncio
        r = asyncio.run(toolkit.run_agent_tool("tarot", "抽三张"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["text"].strip())
        self.assertEqual(r["outcome"], "success")


if __name__ == "__main__":
    unittest.main()