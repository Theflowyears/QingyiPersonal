"""回归锁：她「看不看得见图」这件事上的三处真实错位。

用户报的原话是两句：「她好像一开始没识别到这图是发给她看的」，
以及「让她算塔罗算卦没走云智能体技能」。查下来是三件独立的事：

  1. `[图片]` 被当成表情（sticker），于是别人发截图她只回「图是收到了」；
     实测文本模型 1 秒就能认出纯色图 —— 不是看不见，是压根没让她看。
  2. 图发在上一句、这一句只有「你看」两个字，于是这一轮她只拿到了两个字。
     图片继承没做，她就只能说收到。
  3. 「算下今天抽卡能出六星吗」这类口语问法进不了占卜的工具判定，
     被归成普通聊天，云端技能根本没被调用。

三条各配一条锁，另外钉住「陈述句不该被误判成问卜」——
放宽规则的代价必须一起量。
"""
import os
import sys
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b        # noqa: E402
import toolkit as tk                  # noqa: E402


class StickerVsImageTests(unittest.TestCase):
    def test_a_real_image_is_not_a_sticker(self):
        # 这就是那句「图是收到了」的源头
        self.assertFalse(b._is_sticker_only(u"[图片]", ["u1"]))
        self.assertFalse(b._is_sticker_only(u"[图]", ["u1"]))
        self.assertFalse(b._is_sticker_only(u"[图片][表情]", ["u1"]))

    def test_stickers_are_still_stickers(self):
        for t in (u"[表情]", u"[动画表情]", u"[表情包]", u"[表情][表情]"):
            self.assertTrue(b._is_sticker_only(t, ["u1"]), t)

    def test_voice_alone_is_still_nothing_to_read(self):
        self.assertTrue(b._is_sticker_only(u"[语音]", []))

    def test_text_accompanying_image_is_readable(self):
        self.assertFalse(b._is_sticker_only(u"[图片] 你看这个", ["u1"]))


class ImageInheritanceTests(unittest.TestCase):
    CACHE = b.group_message_cache
    PRIV = b.private_image_cache

    def setUp(self):
        self.gid = "888001"
        self.uid = "777001"
        b.group_message_cache[self.gid] = []
        b.private_image_cache[self.uid] = []

    def tearDown(self):
        b.group_message_cache.pop(self.gid, None)
        b.private_image_cache.pop(self.uid, None)

    def _msg(self, url, age_s=0):
        return {"text": u"[图片]", "image_urls": [url], "is_bot": False,
                "ts_epoch": time.time() - age_s}

    def test_short_reference_inherits_the_previous_image(self):
        b.group_message_cache[self.gid].append(self._msg("http://x/1.jpg"))
        self.assertTrue(b._refers_to_image(u"你看"))
        got = b.recent_image_for("", self.gid)
        self.assertEqual(got, "http://x/1.jpg")

    def test_stale_image_is_never_inherited(self):
        b.group_message_cache[self.gid].append(self._msg("http://x/old.jpg", 7200))
        self.assertEqual(b.recent_image_for("", self.gid), "",
                         "隔了两小时的图又拿来当现在的说，这是对旧图说事")

    def test_long_sentence_does_not_hijack_an_old_image(self):
        for t in (u"你刚才说的那个作家是哪本书的作者来着，我想去找来看看",
                  u"这个周末要不要一起去吃火锅啊，人多才热闹"):
            self.assertFalse(b._refers_to_image(t), t)

    def test_bot_messages_do_not_provide_the_inherited_image(self):
        b.group_message_cache[self.gid].append(
            {"text": u"[图片]", "image_urls": ["http://x/bot.jpg"],
             "is_bot": True, "ts_epoch": time.time()})
        self.assertEqual(b.recent_image_for("", self.gid), "")

    def test_private_chat_inherits_from_its_own_cache(self):
        b.remember_private_images(self.uid, ["http://x/p.jpg"])
        self.assertEqual(b.recent_image_for("private-user-" + self.uid, None),
                         "http://x/p.jpg")

    def test_ask_openclaw_actually_uses_the_inheritance(self):
        # 静态锁：调用点不能只定义函数不用它
        root = os.path.dirname(BOT)
        with open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("async def ask_openclaw"):]
        seg = seg[:seg.index("_recall_q = text")]
        self.assertIn("_refers_to_image(text)", seg)
        self.assertIn("recent_image_for(", seg)


class DivinationRoutingTests(unittest.TestCase):
    """真实说的那两句必须能进云端占卜，而陈述句不能。"""

    REAL = [
        u"快帮阿甲算下今天她抽卡可以出六星吗",
        u"所以你算出来我今天能单抽出六星了吗",
    ]

    def test_the_two_real_phrasings_route_to_fortune(self):
        for t in self.REAL:
            self.assertEqual(tk.pick_agent_tool(t), "fortune", t)

    def test_they_are_explicit_enough_to_go_cloud_in_a_group(self):
        for t in self.REAL:
            plan = b._tool_plan(t, speaker_id="9000000003",
                                group_id="900000001", mentioned=True)
            self.assertTrue(plan.get("cloud"), (t, plan))

    def test_a_statement_is_not_a_question(self):
        # 放宽规则的代价：别人只是在炫耀抽卡，不该触发问卜
        for t in (u"我单抽抽出来了六星", u"这次十连运气真差", u"抽卡真费钱"):
            self.assertEqual(tk.pick_agent_tool(t), "", t)
            self.assertFalse(b._tool_plan(t, speaker_id="9000000003",
                                          group_id="900000001").get("cloud"))

    def test_explicit_tarot_still_works(self):
        self.assertEqual(tk.pick_agent_tool(u"帮我抽个塔罗看看"), "tarot")

    def test_ordinary_chat_stays_chat(self):
        for t in (u"今天吃啥", u"你在干嘛", u"这游戏好难"):
            self.assertEqual(tk.pick_agent_tool(t), "", t)


class ColdImageTests(unittest.TestCase):
    """把 `[图片]` 从表情里拿出来之后，不能反过来变成「见图就开讲」。

    当年那场毛病（认角色、纠正叫法、像自言自语）就是巡群对着图展开分析。
    区别只在上下文：她刚在这个群说过话，图就是对话的一部分；否则是随手丢的图。
    """

    GID = "888002"

    def setUp(self):
        b._auto_reply_state.pop(self.GID, None)

    def tearDown(self):
        b._auto_reply_state.pop(self.GID, None)

    def _spoke_at(self, ts):
        b._auto_reply_state[self.GID] = {"task": None, "last_reply": ts}

    def test_cold_group_image_stays_quiet(self):
        self.assertTrue(b._cold_image(self.GID, u"[图片]", ["u"]))

    def test_image_right_after_she_spoke_is_looked_at(self):
        self._spoke_at(time.time() - 30)
        self.assertFalse(b._cold_image(self.GID, u"[图片]", ["u"]),
                         "她刚说「截图拿来」，图来了反而装看不见")

    def test_image_after_a_long_silence_is_cold_again(self):
        self._spoke_at(time.time() - (b._IMAGE_FOLLOWUP_WINDOW + 60))
        self.assertTrue(b._cold_image(self.GID, u"[图片]", ["u"]))

    def test_stickers_and_voice_are_always_quiet(self):
        self._spoke_at(time.time())
        for t in (u"[表情]", u"[语音]", u"[表情包]"):
            self.assertTrue(b._cold_image(self.GID, t, ["u"]), t)

    def test_text_with_image_is_never_cold(self):
        self.assertTrue(b._is_sticker_only(u"[表情]", []) is True)
        self.assertFalse(b._cold_image(self.GID, u"[图片] 你看这个", ["u"]))


if __name__ == "__main__":
    unittest.main()
