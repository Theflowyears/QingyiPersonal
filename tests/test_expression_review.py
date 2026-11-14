# -*- coding: utf-8 -*-
"""表达复核的回归锁：只改「把揣测说成事实」，且改不动就退回原文。"""
import asyncio
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
sys.path.insert(0, BOT)

import expression_review as er

DRAFT = "笨蛋主人，这点小场面就慌成这样？"
CONTEXT = "[19:24:07] 主人: [图片]\n[19:24:19] 主人: 阿甲提前五分钟叫我们都来了"


class MessageTests(unittest.TestCase):
    def test_prompt_carries_context_and_draft(self):
        msgs = er.build_messages(DRAFT, CONTEXT)
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn(DRAFT, msgs[1]["content"])
        self.assertIn("阿甲提前五分钟", msgs[1]["content"])

    def test_long_context_is_trimmed_from_the_front(self):
        ctx = "旧" * 3000 + "最近的这一句"
        content = er.build_messages(DRAFT, ctx)[1]["content"]
        self.assertIn("最近的这一句", content)
        self.assertLess(len(content), 2400)

    def test_missing_context_is_stated_not_omitted(self):
        self.assertIn("（无）", er.build_messages(DRAFT, "")[1]["content"])


class AcceptTests(unittest.TestCase):
    def test_unchanged_is_not_a_rewrite(self):
        self.assertIsNone(er.accept(DRAFT, DRAFT))

    def test_refusal_markers_are_not_a_rewrite(self):
        for raw in ("无需修改", "没有需要修改的地方", "无。"):
            with self.subTest(raw=raw):
                self.assertIsNone(er.accept(DRAFT, raw))

    def test_empty_or_runaway_output_is_rejected(self):
        self.assertIsNone(er.accept(DRAFT, ""))
        self.assertIsNone(er.accept(DRAFT, "好。"))
        self.assertIsNone(er.accept(DRAFT, "嗯" * 400))

    def test_a_real_rewrite_is_taken_and_unquoted(self):
        fixed = "“笨蛋主人，这是被什么事吓到了吗？”"
        self.assertEqual(er.accept(DRAFT, fixed), "笨蛋主人，这是被什么事吓到了吗？")

    def test_wrapping_quotes_are_stripped(self):
        self.assertEqual(er.accept(DRAFT, "“笨蛋主人，还好吧？”"), "笨蛋主人，还好吧？")


class ScoreTests(unittest.TestCase):
    """什么话才值得多花一次调用。纯反应不该被复核。"""

    def test_pure_reaction_is_not_reviewed(self):
        need, score, _why = er.needs_review("哈哈这也太好笑了吧", CONTEXT)
        self.assertFalse(need)
        self.assertEqual(score, 0)

    def test_asserting_someones_state_is_reviewed(self):
        need, score, why = er.needs_review(DRAFT, CONTEXT)
        self.assertTrue(need)
        self.assertGreaterEqual(score, 2)
        self.assertIn("断言情绪", why)

    def test_naming_someone_with_an_action_is_reviewed(self):
        ctx = CONTEXT + "\n[19:25:21] 阿甲: 我还在路上"
        need, _score, why = er.needs_review("阿甲刚才说他不来了", ctx)
        self.assertTrue(need)
        self.assertIn("指名", why)

    def test_budget_caps_the_extra_calls(self):
        er._BUDGET.clear()
        self.addCleanup(er._BUDGET.clear)
        for _ in range(3):
            self.assertTrue(er.budget_ok(cap=3))
        self.assertFalse(er.budget_ok(cap=3))


class ReviewTests(unittest.TestCase):
    def _run(self, draft, reply_text, boom=False):
        async def call(_msgs):
            if boom:
                raise TimeoutError("slow")
            return reply_text
        return asyncio.run(er.review(draft, CONTEXT, call))

    def test_returns_the_reviewed_text(self):
        self.assertEqual(self._run(DRAFT, "笨蛋主人，还好吧？"), "笨蛋主人，还好吧？")

    def test_failure_keeps_the_original(self):
        self.assertIsNone(self._run(DRAFT, "", boom=True))

    def test_very_short_draft_skips_the_call(self):
        async def call(_msgs):
            raise AssertionError("不该为两个字开一次复核")
        self.assertIsNone(asyncio.run(er.review("嗯", CONTEXT, call)))


class WiringLockTests(unittest.TestCase):
    def test_group_reply_runs_the_review(self):
        with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("expression_review", src)
        self.assertIn("_er.review(", src)

    def test_module_has_no_path_or_network_coupling(self):
        """它只读环境里的额度配置，不许把任何路径冻在模块顶层。"""
        with io.open(os.path.join(BOT, "expression_review.py"), encoding="utf-8") as f:
            src = f.read()
        for line in src.splitlines():
            if line.startswith(("_FILE", "_DIR", "_PATH", "_ROOT")) and "=" in line:
                self.fail("模块顶层冻了路径：%s" % line)
        self.assertNotIn("import requests", src)


if __name__ == "__main__":
    unittest.main()