"""2026-11-11 13:39 私聊现场：替他编行为 + 把「被冤枉」安到自己头上。

原话链：
    主人：又要去上课了吗呜呜呜
    她  ：啧，又逃课？我都替你记着呢。          ← 他没说过逃课
    主人：我没说要逃课呀，在去上课的路上
    她  ：哎呀，冤枉我了……路上注意安全呀。      ← 错的是她，她却写成自己被冤枉

这里锁两件事：
  1. 对方只是在抱怨日常时，要给提示「只按他说的接」，别替他补逃课/旷课。
  2. 对方在纠正她时，「冤枉我/冤枉我」必须掰成「冤枉你了」。
"""
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import qq_openclaw_bridge as b             # noqa: E402


class AttributionHintTests(unittest.TestCase):
    def test_correction_tells_her_the_fault_is_hers(self):
        hint = b._attribution_hint("我没说要逃课呀，在去上课的路上")
        self.assertIn("错的人是你", hint)
        self.assertIn("冤枉你了", hint)
        self.assertIn("不许说「冤枉我了」", hint)

    def test_other_ways_of_correcting_her_are_recognised(self):
        for t in ("我没说那个", "我什么时候说要逃课了", "你别乱说",
                  "你冤枉我", "我不是那个意思", "我明明只是在路上"):
            with self.subTest(text=t):
                self.assertIn("错的人是你", b._attribution_hint(t))

    def test_routine_complaint_blocks_the_skipping_invention(self):
        hint = b._attribution_hint("又要去上课了吗呜呜呜")
        self.assertIn("没说自己逃课", hint)
        self.assertIn("别问", hint)

    def test_routine_hint_does_not_fire_when_he_really_said_it(self):
        for t in ("哎呀我今天逃课了", "又要上班了，想请假", "今天天气怎么样",
                  "帮我查查明日方舟Mon3tr是什么"):
            with self.subTest(text=t):
                self.assertEqual(b._attribution_hint(t), "")


class SelfOtherInversionTests(unittest.TestCase):
    CORRECTION = "我没说要逃课呀，在去上课的路上"

    def test_wronged_subject_is_flipped_back(self):
        out = b._fix_self_other_inversion(
            "哎呀，冤枉我了……路上注意安全呀。", self.CORRECTION)
        self.assertEqual(out, "哎呀，冤枉你了……路上注意安全呀。")

    def test_wrong_verb_is_kept(self):
        self.assertEqual(
            b._fix_self_other_inversion("是我错怪我了。", self.CORRECTION),
            "是我错怪你了。")

    def test_no_rewrite_without_a_correction_context(self):
        text = "哎呀，冤枉我了……"
        self.assertEqual(b._fix_self_other_inversion(text, "今天吃什么"), text)

    def test_explicit_you_as_subject_is_left_alone(self):
        # 「你冤枉我了」是她在把矛头指回去；硬改会变成「你冤枉你了」。
        text = "你冤枉我了。"
        self.assertEqual(b._fix_self_other_inversion(text, self.CORRECTION), text)

    def test_correct_wording_is_untouched_and_idempotent(self):
        self.assertEqual(
            b._fix_self_other_inversion("冤枉你了，是我不对。", self.CORRECTION),
            "冤枉你了，是我不对。")
        once = b._fix_self_other_inversion("冤枉我了。", self.CORRECTION)
        self.assertEqual(b._fix_self_other_inversion(once, self.CORRECTION), once)

    def test_none_and_empty_are_safe(self):
        self.assertEqual(b._fix_self_other_inversion("", self.CORRECTION), "")
        self.assertIsNone(b._fix_self_other_inversion(None, self.CORRECTION))


class WiredIntoAskTests(unittest.IsolatedAsyncioTestCase):
    """两处护栏必须真的接进 ask_openclaw，而不只是躺在模块里。"""

    async def drive(self, text, reply):
        key = "private-user-1000000001"
        seen = []

        def spawn(coro):
            coro.close()

        async def model(messages, **kw):
            seen.append(messages)
            return reply

        for d in (b.histories, b.FLIRT_BURST, b.STICKER_WISH, b._PENDING_DELIVERIES):
            self.addCleanup(d.pop, key, None)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(b, "save_histories"), \
                patch.object(b, "_mark_talk"), \
                patch.object(b, "_fatigue_touch"), \
                patch.object(b, "_spawn", side_effect=spawn), \
                patch.object(b, "STICKER_PROB", 0), \
                patch.object(b, "get_persona_context", return_value=""), \
                patch.object(b, "call_openclaw", side_effect=model), \
                patch.object(b, "_hold_bubble", new=AsyncMock(return_value=False)), \
                patch.object(b, "_novel_answer",
                             new=AsyncMock(side_effect=lambda ans, *a, **k: ans)):
            b.histories[key] = deque(maxlen=24)
            answer = await b.ask_openclaw(key, text, speaker_id="1000000001",
                                          speaker_nick="主人")
        return answer, seen

    async def test_correction_hint_reaches_the_prompt(self):
        _, seen = await self.drive(
            "我没说要逃课呀，在去上课的路上", "行吧，我知道了。")
        blob = "\n".join(str(m.get("content")) for m in seen[0])
        self.assertIn('本轮事实与纠错',blob)
        self.assertIn('纠正所指尚不明确',blob)
        self.assertNotIn('错的人是你，不是他',blob)

    async def test_inverted_wronged_subject_never_leaves_the_bridge(self):
        answer, _ = await self.drive(
            "我没说要逃课呀，在去上课的路上",
            "哎呀，冤枉我了……路上注意安全呀。")
        self.assertEqual(answer, "哎呀，冤枉你了……路上注意安全呀。")

    async def test_skipping_hint_reaches_the_prompt(self):
        _, seen = await self.drive("又要去上课了吗呜呜呜", "唉，去吧去吧。")
        blob = "\n".join(str(m.get("content")) for m in seen[0])
        self.assertIn("只按他说的接", blob)


if __name__ == "__main__":
    unittest.main()