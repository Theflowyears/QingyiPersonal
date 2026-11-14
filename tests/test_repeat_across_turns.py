"""同一句术隔两三分钟再说一遍。

生产实测私聊：
    23:46:44  笨蛋主人，大半夜不睡觉发这个？
    23:49:01  笨蛋主人，大半夜不睡觉就为了逗我？
主人当场指出「怎么又重复回答同一句了」。去重不是没做——`repeat_reply` 就在那儿，
但它只认「几乎逐字相同」：这两句归一化以后公共前缀 10 个字，后半各说各话，
`SequenceMatcher` 只有 0.71，卡在 0.86 以下放行了。

所以加一条前缀规则（开头一模一样就是复读模板），并且把「已经说过的开头」
明确列进重写请求——只说「重复了」不给材料，她能换三个字再说回同一句。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import conversation_plan as cp          # noqa: E402
import qq_openclaw_bridge as b          # noqa: E402

ECHOED = "笨蛋主人，大半夜不睡觉发这个？"
AGAIN = "笨蛋主人，大半夜不睡觉就为了逗我？"


class NearRepeatTests(unittest.TestCase):
    def test_same_opener_different_tail_is_a_repeat(self):
        self.assertTrue(cp.repeat_reply(AGAIN, [ECHOED]))

    def test_sticker_captions_reused_across_turns(self):
        old = ["啧，这表情挺应景。", "笨蛋主人，大半夜不睡觉发这个？"]
        self.assertTrue(cp.repeat_reply("笨蛋主人，大半夜不睡觉就为了逗我？", old))

    def test_different_opener_is_not_a_repeat(self):
        for got in ("哼，理直气壮是吧。", "那我陪你熬会儿，就这一次啊。",
                    "才不是傲娇……"):
            self.assertFalse(cp.repeat_reply(got, [ECHOED]), got)

    def test_short_common_start_is_not_a_repeat(self):
        # 「笨蛋主人」是她的常用称呼，只撞前缀几个字不算复读
        self.assertFalse(cp.repeat_reply("笨蛋主人，晚安啦。", [ECHOED]))
        self.assertFalse(cp.repeat_reply("笨蛋主人早点睡。", ["笨蛋主人记得喝水。"]))

    def test_numbers_still_matter(self):
        self.assertFalse(cp.repeat_reply("就加一分吧，八十五分。", ["就加一分吧，八十四分。"]))

    def test_exact_repeat_still_caught(self):
        self.assertTrue(cp.repeat_reply(ECHOED, [ECHOED]))

    def test_bubble_inside_a_multi_bubble_reply_caught(self):
        old = ["哼，我才不稀罕当呢。"]
        self.assertTrue(cp.repeat_reply("啧，谁要当小孩子……\n哼，我才不稀罕当呢。", old))


class RewriteRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_delivered_opener_is_removed_without_losing_new_content(self):
        answer = "唉~真拿你没办法。刚才还哭唧唧说没vvna，现在又喊好玩。"
        with patch.object(b, "call_openclaw", new=AsyncMock()) as model:
            got = await b._novel_answer(answer, "大变好玩", ["唉~真拿你没办法"], [])
        self.assertEqual(got, "刚才还哭唧唧说没vvna，现在又喊好玩。")
        model.assert_not_awaited()

    async def test_programming_symbols_are_part_of_new_fact(self):
        answer = "我支持C#开发。工具链也换了。"
        got = await b._novel_answer(answer, "换语言", ["我支持C++开发。"], [])
        self.assertEqual(got, answer)

    async def test_partial_salutation_is_not_removed_from_new_sentence(self):
        answer = "笨蛋主人，今晚记得早点睡。"
        got = await b._novel_answer(answer, "晚安", ["笨蛋主人，今天记得喝水。"], [])
        self.assertEqual(got, answer)

    async def test_new_numeric_fact_is_not_trimmed_as_a_repeated_opener(self):
        answer = "今天是99分。比昨天高了一分。"
        got = await b._novel_answer(answer, "好感多少", ["今天是98分。"], [])
        self.assertEqual(got, answer)

    async def test_rewrite_is_told_which_openers_are_used(self):
        seen = {}

        async def fake(messages, **kw):
            seen["prompt"] = messages
            return "哦哟，那你现在是真闲。"

        with patch.object(b, "call_openclaw", new=AsyncMock(side_effect=fake)):
            got = await b._novel_answer(AGAIN, "[图片]", [ECHOED],
                                        [{"role": "system", "content": "人设"}])
        self.assertEqual(got, "哦哟，那你现在是真闲。")
        tail = seen["prompt"][-1]["content"]
        self.assertIn("笨蛋主人", tail)      # 说清楚哪句已经说过
        self.assertIn("不能再发", tail)

    async def test_still_repeating_becomes_silence(self):
        with patch.object(b, "call_openclaw", new=AsyncMock(return_value=AGAIN)):
            self.assertEqual(await b._novel_answer(AGAIN, "[图片]", [ECHOED], []), "[[沉默]]")


if __name__ == "__main__":
    unittest.main()
