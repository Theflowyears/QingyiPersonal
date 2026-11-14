"""她说不出话，和面板回读不到新值：同一批改动留下的两个坑。

主人两句话：
    「她现在不主动接话说话的问题你还没修好呢！接着修！」
    「高级面板那里调了报成功之后立即重新抓取数据还是没有修改！」

先说数据（生产实测，21:31 新裁决上线以来）：
    会话裁决 reply=False 16 次、skip=image_only 2 次、skip=other_recipient 7 次
    reply=True 0 次
模型给的理由是「属于群体起哄和玩梗，非针对清漪的提问或求助，无需插话」——
它把「大家在玩梗」当成了不接的理由，而那恰恰是该接的场合。
但真正让参数失灵的是这一条：面板上写着「连续多少条没说话，就强制说一句」，
代码里 `GROUP_AUTO_REPLY_FORCE` 只拿去调概率，模型一说 false 就直接 return。
所谓「强制」从来不存在。

第二个坑：收件改成 2 秒一轮以后，`drain()` 只应用参数、不写状态快照
（快照还是 20 秒一次的 poll 在写）。于是保存成功、回执也 done，
面板回头再抓一次数据读到的还是**旧快照**，看起来就是「改了没生效」。
不是没生效，是我把生效的证据留在下一个 20 秒里。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import ceylan_control_runtime as cr        # noqa: E402
import qq_openclaw_bridge as b             # noqa: E402
import ceylan_control_runtime as ctl       # noqa: E402

SELF = "9"


def msg(seq, uid, text, nickname="烤苕皮！", is_bot=False, at=None):
    return {"seq": seq, "ts": time.time(), "time": "01:0%d:00" % (seq % 10),
            "user_id": uid, "nickname": nickname, "text": text, "is_bot": is_bot,
            "image_urls": [], "at_targets": at or [], "self_id": SELF}


class ForceActuallyForcesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gid = "771155"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for d in (b.group_message_cache, b._GROUP_SPEAK, b.group_last_periodic_seq,
                  b._auto_reply_state):
            self.addCleanup(d.pop, self.gid, None)
        b._GROUP_SPEAK.pop(self.gid, None)
        b.group_last_periodic_seq.pop(self.gid, None)
        self.patchers = [
            patch.dict(os.environ, {"DATA_DIR": self.tmp.name}),
            patch.object(b, "save_periodic_state", lambda: None),
            patch.object(b, "GROUP_AUTO_REPLY_ENABLED", True),
            patch.object(b, "GROUP_AUTO_REPLY_DEBOUNCE", 0),
            patch.object(b, "GROUP_AUTO_REPLY_FORCE", 5),
            patch.dict(ctl.OPTIONS, {"patrol_enabled": True}),
            patch("draw_flow.has_pending", return_value=False),
            patch.object(b, "_group_aliases", return_value={"阿戊": "9000000004"}),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def cache(self, trailing_human):
        rows = [msg(1, "BOT", "我刚说过一句。", "清漪", is_bot=True)]
        for i in range(trailing_human):
            rows.append(msg(2 + i, "9000000003", "小情侣真好啊"))
        b.group_message_cache[self.gid] = deque(rows, maxlen=200)

    async def run_turn(self, proposal, sent=None):
        model = AsyncMock(return_value=json.dumps(proposal, ensure_ascii=False))
        ws = type("Wire", (), {"send": sent or AsyncMock()})()
        with patch.object(b, "call_openclaw", new=model), \
                patch.object(b, "ask_openclaw",
                             new=AsyncMock(return_value="哈哈哈哈你们够了")), \
                patch.object(b, "send_group_msg", new=AsyncMock(return_value=True)) as post, \
                patch.object(b, "_submit_chat", new=AsyncMock(return_value=True)):
            got = await b._evaluate_group_turn(ws, self.gid, source="auto")
        return got, post, model

    async def test_quiet_long_enough_ignores_one_veto(self):
        """「连续 N 条没说话就强制说一句」必须真的强制。

        以前模型一句 false 就 return，FORCE 只是拿去算了概率 ——
        于是参数调到天上她也不接话，面板那句话成了假的。
        """
        self.cache(6)                      # 她憋了 6 条 > FORCE=5
        got, post, model = await self.run_turn(
            {"reply": False, "target_seq": 7, "reason": "群体起哄，无需插话"})
        self.assertTrue(got, "憋够 FORCE 条还是一次也没开口")
        self.assertEqual(model.await_count, 1)
        post.assert_awaited()

    async def test_recent_speaker_still_respects_veto(self):
        self.cache(1)                      # 才 1 条，没到 FORCE
        got, post, model = await self.run_turn(
            {"reply": False, "target_seq": 2, "reason": "别人之间的玩笑"})
        self.assertFalse(got)
        post.assert_not_awaited()

    async def test_third_party_is_never_forced(self):
        """憋再久也不许抢别人之间的对话 —— 那是上一轮主人骂出来的反例。"""
        rows = [msg(1, "9000000003", "清清你去哄哄阿戊")]
        rows[0]["at_targets"] = ["9000000004"]
        b.group_message_cache[self.gid] = deque(rows, maxlen=200)
        b.group_message_cache[self.gid][0]["seq"] = 9
        got, post, model = await self.run_turn(
            {"reply": True, "target_seq": 9, "reason": "想接"})
        self.assertFalse(got)
        self.assertEqual(model.await_count, 0)
        post.assert_not_awaited()


class ArbiterFramingTests(unittest.TestCase):
    ANCHOR = '"她是这个群的群友，不是审核员。'

    def setUp(self):
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        i = src.index(self.ANCHOR)
        self.prompt = src[i:i + 1100]

    def test_prompt_does_not_ask_for_permission(self):
        # 问「有没有必要」，模型永远回答没必要 —— 16 次判定 0 次放行就是这么来的
        self.assertNotIn("是否有必要开口", self.prompt)
        self.assertIn("群友", self.prompt)
        self.assertIn("默认该接", self.prompt)

    def test_prompt_says_playing_along_is_a_reason_to_join(self):
        self.assertIn("起哄", self.prompt)
        self.assertIn("接一句", self.prompt)
        # 但「不是她的对话」这条底线不能松
        self.assertIn("不要抢", self.prompt)

    def test_invalid_target_seq_does_not_eat_the_yes(self):
        # 模型说 true、seq 却指到窗口外：以前整票作废，她一句也不说
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        self.assertIn("改接最新一条", src)


class SnapshotAfterSaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self._p = patch.object(cr, "ROOT", self.root)
        self._p.start()
        self.addCleanup(self._p.stop)
        import shutil
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._ledger = dict(cr.LEDGER)
        cr.LEDGER.clear()
        self.addCleanup(lambda: (cr.LEDGER.clear(), cr.LEDGER.update(self._ledger)))

    @staticmethod
    def ns():
        return {"group_memory_summary": {}, "group_memory_last_seq": {},
                "group_memory_last_update_ts": {}, "group_message_cache": {},
                "group_message_seq": {}, "group_last_periodic_seq": {},
                "periodic_answer_fingerprints": {}, "histories": {},
                "GROUP_MEMORY_STATE_FILE": str(Path("/tmp") / "none.json"),
                "PERIODIC_STATE_FILE": str(Path("/tmp") / "none2.json"),
                "GROUP_AUTO_REPLY_COOLDOWN": 12, "reply_chars": 45, "bubbles": 3,
                "delay_ms": 700, "reply_tokens": 150, "patrol_enabled": True,
                "memory_enabled": True, "style_note": ""}

    def _drop(self, cmd):
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / (cmd["id"] + ".json")).write_text(json.dumps(cmd), encoding="utf-8")

    def test_drain_reports_that_it_applied_something(self):
        ns, root = self.ns(), self.root
        cmd = {"action": "settings", "values": {"GROUP_AUTO_REPLY_COOLDOWN": 7},
               "revision": cr.revision(cr.settings(ns)), "id": "c" * 32,
               "expires": time.time() + 600}
        self._drop(cmd)
        applied = cr.drain(ns, _Rt())
        self.assertEqual(applied, 1, "drain 要报告这次真的改了参数")
        self.assertEqual(ns["GROUP_AUTO_REPLY_COOLDOWN"], 7)

    def test_empty_drain_reports_nothing(self):
        self.assertEqual(cr.drain(self.ns(), _Rt()), 0)

    def test_save_refreshes_the_snapshot_immediately(self):
        """保存后面板立刻重新抓取，读到的必须已经是新值。

        快照本来是 20 秒一写；收件改成 2 秒以后，如果 drain 不顺手刷一次，
        面板那一页就还在读 20 秒前的旧档 —— 回执写着成功、数值却还是旧的。
        """
        ns = self.ns()
        cmd = {"action": "settings", "values": {"GROUP_AUTO_REPLY_COOLDOWN": 9},
               "revision": cr.revision(cr.settings(ns)), "id": "d" * 32,
               "expires": time.time() + 600}
        self._drop(cmd)
        applied = cr.drain(ns, _Rt())
        cr.write_state(ns, _Rt())
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["settings"]["GROUP_AUTO_REPLY_COOLDOWN"], 9)
        self.assertEqual(state["settings_revision"], cr.revision(cr.settings(ns)))

    async def test_inbox_loop_refreshes_only_when_something_landed(self):
        hits = {"drain": 0, "state": 0}

        def _drain(ns, rt):
            hits["drain"] += 1
            return 1 if hits["drain"] == 1 else 0

        with patch.object(cr, "drain", new=_drain), \
                patch.object(cr, "write_state",
                             new=lambda ns, rt: hits.__setitem__("state", hits["state"] + 1)):
            task = asyncio.create_task(cr._inbox_loop({}, _Rt(), period=0.01))
            await asyncio.sleep(0.12)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertGreaterEqual(hits["drain"], 3)
        self.assertEqual(hits["state"], 1, "没改动也刷快照，等于白写一份状态文件")


class _Rt:
    def blocked(self, gid):
        return False

    def snapshot(self):
        return None


if __name__ == "__main__":
    unittest.main()
