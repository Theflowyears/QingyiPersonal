"""回归锁：私聊亲密短连发不该挂「这个得写一会儿…整段一块儿发你」的长文接住。

主人报的现场：睡前撒娇闲聊里冒出一句
    「我在憋了，稍等，整段一块儿发你。」
那是 `_LONG_HOLD`，本来只给真要走慢思考长文的路径用（实测 110~125 秒）。

根因：长文接住的判定把 `_intimate_mode` 也算了进去。但亲密模式走的是快车道 ——
`long_output = _explicit_long or _flirt_now`，亲密不在其中，`chat_fast=True`、
按预算秒回、拆成 2~4 条短句连发。它根本不慢，也不会「整段一块儿发」，
先甩一句「得写一会儿」是错配：既骗了人，又把好好的撒娇气氛打断。

所以接住只留给真慢的路径：明确长文（_explicit_long）、群聊暧昧成篇（_flirt_now）、
认真答（deep）。亲密短连发不接。
"""
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

import qq_openclaw_bridge as b  # noqa: E402


class IntimateNoLongHoldTests(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, key, text, *, deep=False, reply="过来一点。\n只准抱一下。\n别得寸进尺。"):
        """跑一遍 ask_openclaw，记录长文接住有没有被触发。"""
        hold = AsyncMock(return_value=True)
        got = []

        async def fake_llm(messages, **kw):
            got.append(messages)
            return reply

        self.addCleanup(b.histories.pop, key, None)
        self.addCleanup(b.FLIRT_BURST.pop, key, None)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root, "SLOW_LOOKUPS": "after_reply"}), \
                patch.object(b, "save_histories"), \
                patch.object(b, "_spawn", side_effect=lambda c: c.close()), \
                patch.object(b, "call_openclaw", fake_llm), \
                patch.object(b, "_hold_bubble", hold), \
                patch.object(b, "_novel_answer", AsyncMock(side_effect=lambda ans, *a, **k: ans)):
            b.histories[key] = deque(maxlen=24)
            b.FLIRT_BURST.pop(key, None)
            await b.ask_openclaw(key, text, speaker_id=key.replace("private-user-", ""),
                                 speaker_nick="阿丙", speaker_sex="female", deep=deep)
        return hold, got

    async def test_intimate_short_burst_does_not_fire_the_long_hold(self):
        key = "private-user-889910"
        hold, got = await self._drive(key, "贴贴")
        self.assertTrue(got, "没走到模型，用例没跑起来")
        # 确认真的进了亲密模式（否则这条用例锁不到东西）
        burst = b.FLIRT_BURST.get(key) or {}
        self.assertEqual(burst.get("kind"), "intimate")
        hold.assert_not_called()

    async def test_boundary_short_burst_does_not_fire_the_long_hold(self):
        key = "private-user-889911"
        hold, got = await self._drive(
            key, "一起色色", reply="你这话我没听懂。\n再说下去要当真了。")
        self.assertTrue(got, "没走到模型，用例没跑起来")
        hold.assert_not_called()

    async def test_deep_answer_still_fires_the_long_hold(self):
        # 阳性对照：认真答是真慢路径，接住必须还在，别把闸门一起废了
        key = "private-user-889912"
        hold, got = await self._drive(key, "在吗", deep=True, reply="嗯，我在呢。")
        self.assertTrue(got, "没走到模型，用例没跑起来")
        hold.assert_called_once()
        self.assertIs(hold.call_args.args[2], b._LONG_HOLD)


if __name__ == "__main__":
    unittest.main()