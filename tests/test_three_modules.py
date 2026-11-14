"""2026-11-11 现场复盘：长文不触发、长文交付被丢、对象认错人。

生产日志（group=900000001）里的三处硬证据：
  1. `[工具判定] 宝宝写个拉普兰德和德克萨斯对女同文 | action=chat (普通聊天)`
     —— 写作请求被当成水群，max_tokens=150，她只能空口答应。
  2. `LLM_DEEP completion=2376` 之后紧跟 `[会话裁决] 生成期间话题继续，丢弃过时候选`
     —— 整篇小说写出来了，因为群里有人发了张图，被当成「话题过时」扔了；
     可她已经先说了「这段要认真写，给我点时间」。
  3. 上下文里她自己那句是 `[10:29] 清漪（你）: 啧，笨蛋主人……`，
     target_uid=1000000001 存着却从没进过提示词，于是阿甲问「什么时候怼我了」，
     她把自己对主人说的话记成了对阿甲说的。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import qq_openclaw_bridge as b             # noqa: E402
import conversation_plan as cp            # noqa: E402

# 现场原话 + 同义写法：都是「要一篇东西」，必须成篇输出。
WRITE_REQUESTS = [
    "宝宝写个拉普兰德和德克萨斯对女同文",
    "@清漪 宝宝写个拉普兰德和德克萨斯对女同文",
    "我现在急需一份德克萨斯和拉普兰德的女同文，救救我",
    "写个拉普兰德和德克萨斯的女同的故事",
    "帮我写一篇百合文",
    "写个小说",
    "写个函数",
]

# 抱怨、否定、水群：不是写作请求，不能被 3000 token 的深度链路吞掉。
NOT_REQUESTS = [
    "清清不写文啊",
    "奇怪",
    "随便写",
    "别写那种女同文",
    "整点同人图",
    "来张百合漫画",
    "你怎么都不写文",
    "什么时候怼我了",
    "今天开心吗",
]


class LongTriggerTests(unittest.TestCase):
    def test_write_requests_enter_long_mode(self):
        for t in WRITE_REQUESTS:
            with self.subTest(text=t):
                self.assertTrue(b._wants_long_mode(t), "这是要一篇东西，不能当水群")

    def test_complaints_and_chatter_do_not_enter_long_mode(self):
        for t in NOT_REQUESTS:
            with self.subTest(text=t):
                self.assertFalse(b._wants_long_mode(t), "抱怨/水群不该触发成篇输出")

    def test_shared_planner_agrees_on_genre_requests(self):
        # 桥接判成 long，conversation_plan 也认这个体裁，两套判据不能各说各话。
        for t in ("宝宝写个拉普兰德和德克萨斯对女同文",
                  "我现在急需一份德克萨斯和拉普兰德的女同文，救救我"):
            with self.subTest(text=t):
                self.assertTrue(cp.wants_long(t))
                self.assertEqual(cp.plan(t, long_mode=True)["length"], "long")

    def test_genre_requests_get_the_novel_style_block(self):
        for t in ("宝宝写个拉普兰德和德克萨斯对女同文",
                  "我现在急需一份德克萨斯和拉普兰德的女同文，救救我",
                  "帮我写一篇百合文"):
            with self.subTest(text=t):
                self.assertTrue(b._LONG_STYLE_RE.search(t),
                                "成篇的百合/女同题材也要拿到小说文风块")
        self.assertFalse(b._LONG_STYLE_RE.search("写个函数"))


class PromiseSurvivesTopicDriftTests(unittest.TestCase):
    """答应了的事不能因为群里又冒出一句就作废。"""

    def test_no_promise_keeps_the_old_drop_behaviour(self):
        self.assertFalse(b._keep_after_drift(None))

    def test_fresh_promise_is_delivered_even_if_topic_moved_on(self):
        self.assertTrue(b._keep_after_drift({"at": time.time(), "text": "稍等"}))

    def test_expired_promise_does_not_haunt_a_later_turn(self):
        self.assertFalse(b._keep_after_drift({"at": time.time() - 3600}))

    def test_a_sent_hold_records_the_obligation(self):
        """_hold_bubble 真发出去的那一刻，就是欠下一句话的那一刻。"""
        key = "group-900000001"
        b._PROMISED.clear()
        self.addCleanup(b._PROMISED.clear)
        sent = []

        async def fake_send(ws, gid, uid, text, **kw):
            sent.append(text)
            return True

        import asyncio
        async def go():
            # run_all 会设 QQ_DRY_RUN=1，而 _hold_bubble 在干跑时直接返回 False。
            # 这条测的是"真发出去才记账"，所以必须把干跑关掉（同 test_evidence_chain）。
            with mock.patch.object(b, "_BRIDGE_WS", {"ws": object()}), \
                    mock.patch.object(b, "QQ_DRY_RUN", False), \
                    mock.patch.object(b, "send_group_msg", new=fake_send):
                return await b._hold_bubble("900000001", "1000000001",
                                            b._LONG_HOLD, key)

        ok = asyncio.run(go())
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertIn(sent[0], b._LONG_HOLD)
        # 没有 turn context 时 seq=0；生产里由 _TURN_CONTEXT 带真实 seq。
        promised = b._PROMISED.get((key, 0))
        self.assertIsNotNone(promised)
        self.assertTrue(b._keep_after_drift(promised))


class AddresseeTests(unittest.TestCase):
    """她自己说的话要带上「这是对谁说的」，否则会被算到别人头上。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="addr_")
        import member_memory
        self.mm = member_memory
        self._old_file = member_memory.PROFILE_FILE
        member_memory.PROFILE_FILE = os.path.join(self.dir, "member_profiles.json")
        member_memory._cache = {"data": None, "ts": 0.0}
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(setattr, member_memory, "PROFILE_FILE", self._old_file)
        self.addCleanup(setattr, member_memory, "_cache", {"data": None, "ts": 0.0})
        member_memory.set_call("900000001", "1000000001", "主人")

    def test_bot_line_names_who_she_was_answering(self):
        m = {"time": "10:29:43", "user_id": "BOT", "nickname": b.BOT_SELF_NAME,
             "text": "行吧，就这一次啊。", "is_bot": True,
             "target_uid": "1000000001"}
        line = b._cached_line(m, "900000001")
        self.assertIn("（你 → 主人）", line, "她自己的话要写清是回给谁的")
        self.assertIn("行吧", line)

    def test_bot_line_without_a_target_stays_plain(self):
        m = {"time": "10:29:43", "user_id": "BOT", "nickname": b.BOT_SELF_NAME,
             "text": "水群一句。", "is_bot": True}
        line = b._cached_line(m, "900000001")
        self.assertNotIn("→", line, "没人可指的时候别硬加箭头")

    def test_unprofiled_recipient_falls_back_to_their_group_nickname(self):
        gid = "900000001"
        b.group_message_cache[gid] = [
            {"seq": 1, "time": "11:57:49", "user_id": "9000000003",
             "nickname": "还有3天接荒芜拉普兰德！", "text": "什么时候怼我了",
             "image_urls": [], "is_bot": False}]
        self.addCleanup(b.group_message_cache.pop, gid, None)
        m = {"time": "11:57:51", "user_id": "BOT", "nickname": b.BOT_SELF_NAME,
             "text": "我懒得跟你计较。", "is_bot": True,
             "target_uid": "9000000003"}
        self.assertIn("还有3天接荒芜拉普兰德！", b._cached_line(m, gid))

    def test_her_promise_is_attributed_to_the_master_not_the_next_asker(self):
        m = {"time": "10:29:43", "user_id": "BOT", "nickname": b.BOT_SELF_NAME,
             "text": "啧，笨蛋主人……刚怼完阿甲又来找我麻烦。",
             "is_bot": True, "target_uid": "1000000001"}
        line = b._cached_line(m, "900000001")
        # 现场：阿甲(9000000003) 问「什么时候怼我了」，她答成了「刚才那句笨蛋主人」。
        # 这句正文里本来就带「主人」二字，所以只认归因标记本身：
        # 必须写明这句是发给主人的，别让它在上下文里变成没有指代的一句话。
        self.assertIn("（你 → 主人）", line)
        self.assertNotIn("9000000003", line)


if __name__ == "__main__":
    unittest.main()