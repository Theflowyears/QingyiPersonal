"""回归锁：私聊亲密模式只接亲近，不接性；并且她已明确成年。"""
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

BOT = Path(__file__).resolve().parents[1] / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import intimacy_mode as im  # noqa: E402
import whale_persona  # noqa: E402
import qq_openclaw_bridge as b  # noqa: E402


class AssessTests(unittest.TestCase):
    def test_romantic_cues_enter_intimate(self):
        for text in ("贴贴", "我好喜欢你跟我回家", "抱抱我", "想你了",
                     "想和你在一起", "亲一下"):
            got = im.assess(text, sex="female", is_private=True)
            self.assertEqual(got["mode"], "intimate", text)

    def test_sexual_cues_enter_boundary(self):
        for text in ("一起色色", "我想和你一起睡觉", "蹭蹭不进去",
                     "我想和你搞百合", "给我看看色图"):
            got = im.assess(text, sex="male", is_private=True)
            self.assertEqual(got["mode"], "boundary", text)

    def test_group_never_enters_private_mode(self):
        self.assertEqual(im.assess("贴贴", is_private=False)["mode"], "")
        self.assertEqual(im.assess("一起色色", is_private=False)["mode"], "")

    def test_plain_chat_does_not_trigger(self):
        for text in ("今天吃什么", "注意身体", "我想你帮我查一下",
                     "陪我打游戏", "有点心跳加速"):
            self.assertEqual(im.assess(text, is_private=True)["mode"], "", text)

    def test_tone_follows_gender_without_guessing(self):
        self.assertIn("姐姐", im.tone_hint("female"))
        self.assertIn("哥哥", im.tone_hint("male"))
        self.assertIn("不猜性别", im.tone_hint("unknown"))


class LinesTests(unittest.TestCase):
    def test_intimate_keeps_2_to_4_and_drops_explicit(self):
        answer = "\n".join([
            "1. 过来一点。",
            "2. 只准抱一下。",
            "3. 要不要一起色色？",
            "4. 别得寸进尺。",
            "5. 我要走了。",
        ])
        lines = im.prepare_lines(answer, "intimate")
        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(len(lines), 4)
        self.assertFalse(any("色色" in x for x in lines))
        self.assertTrue(all(len(x) <= 24 for x in lines))

    def test_boundary_caps_at_two(self):
        lines = im.prepare_lines("装没听懂\n再说下去要当真了\n今天吃什么",
                                 "boundary")
        self.assertLessEqual(len(lines), 2)

    def test_boundary_preserves_her_direct_refusal(self):
        lines = im.prepare_lines("打住\n这个不聊\n换个话题", "boundary")
        self.assertEqual(lines, ['打住', '这个不聊'])

    def test_fallback_never_contains_sex(self):
        lines = im.prepare_lines("色色", "boundary")
        self.assertTrue(lines)
        self.assertFalse(any("色色" in x for x in lines))

    def test_prompts_carry_redline_and_length(self):
        intimate = im.build_prompt("intimate", "对方是女性")
        boundary = im.build_prompt("boundary", "对方是男性")
        for text in (intimate, boundary):
            self.assertIn("零性器官", text)
            self.assertIn("零性行为", text)
            self.assertIn("成年人", text)
        self.assertIn("2 到 4 条", intimate)
        self.assertIn("10 到 20", intimate)
        self.assertIn("1 到 2 条", boundary)
        self.assertIn("模糊", boundary)
        self.assertIn("不确认也不否认", boundary)


class AdultPersonaTests(unittest.TestCase):
    def test_persona_is_explicitly_adult(self):
        block = whale_persona.build_persona_block()
        self.assertIn("已经成年（二十岁）", block)
        self.assertIn("不是未成年", block)
        self.assertEqual(whale_persona.PERSONA_META.get("age"), "20（成年）")


class BridgeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_romantic_cue_injects_safe_prompt_and_bursts_short(self):
        key = "private-user-889900"
        got = []

        async def fake_llm(messages, **kw):
            got.append(messages)
            return "过来一点。\n只准抱一下。\n别得寸进尺。"

        self.addCleanup(b.histories.pop, key, None)
        self.addCleanup(b.FLIRT_BURST.pop, key, None)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(b, "save_histories"), \
                patch.object(b, "_spawn", side_effect=lambda c: c.close()), \
                patch.object(b, "call_openclaw", fake_llm), \
                patch.object(b, "_novel_answer", AsyncMock(side_effect=lambda ans, *a, **k: ans)):
            b.histories[key] = deque(maxlen=24)
            b.FLIRT_BURST.pop(key, None)
            await b.ask_openclaw(key, "贴贴", speaker_id="889900",
                                 speaker_nick="阿丙", speaker_sex="female")
        self.assertTrue(got, "没走到模型")
        sys_text = "\n".join(str(m.get("content") or "") for m in got[0]
                             if isinstance(m, dict))
        self.assertIn("私聊亲密模式", sys_text)
        self.assertIn("零性器官", sys_text)
        burst = b.FLIRT_BURST.get(key) or {}
        self.assertEqual(burst.get("kind"), "intimate")
        self.assertGreaterEqual(len(burst.get("lines") or []), 2)
        self.assertLessEqual(len(burst.get("lines") or []), 4)
        self.assertTrue(all(len(x) <= 24 for x in burst.get("lines") or []))


class BridgeBoundaryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_sexual_cue_gets_boundary_prompt_and_at_most_two_lines(self):
        key = "private-user-889901"
        got = []

        async def fake_llm(messages, **kw):
            got.append(messages)
            return "你这话我没听懂。\n再说下去要当真了。\n今天吃什么"

        self.addCleanup(b.histories.pop, key, None)
        self.addCleanup(b.FLIRT_BURST.pop, key, None)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(b, "save_histories"), \
                patch.object(b, "_spawn", side_effect=lambda c: c.close()), \
                patch.object(b, "call_openclaw", fake_llm), \
                patch.object(b, "_novel_answer", AsyncMock(side_effect=lambda ans, *a, **k: ans)):
            b.histories[key] = deque(maxlen=24)
            b.FLIRT_BURST.pop(key, None)
            await b.ask_openclaw(key, "一起色色", speaker_id="889901",
                                 speaker_nick="阿丙", speaker_sex="male")
        sys_text = "\n".join(str(m.get("content") or "") for m in got[0]
                             if isinstance(m, dict))
        self.assertIn("私聊暧昧边界", sys_text)
        self.assertIn("零性器官", sys_text)
        burst = b.FLIRT_BURST.get(key) or {}
        self.assertLessEqual(len(burst.get("lines") or []), 2)


class BridgeWiringTests(unittest.TestCase):
    def test_bridge_has_private_intimacy_path(self):
        src = (Path(__file__).resolve().parents[1] / "bot"
               / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        self.assertIn("import intimacy_mode", src)
        self.assertIn("_intimate_mode", src)
        self.assertIn('"kind": "intimate"', src)
        self.assertIn('speaker_sex', src)


if __name__ == "__main__":
    unittest.main()
