"""面板改完到真的生效，中间到底隔了多久。

主人报的是「更改设置以后好像设置并不会立即热加载生效了，是不是路径有问题」。
先按他说的查路径，结论是路径没断（生产 23:43 那次保存）：

    ledger 最后一条 status=done「已执行并保存」
    data/control-v4/settings.json 与运行中 live 值逐键相等
    inbox 已排空、CEYLAN_CONTROL_DIR 两边同一个目录（web 进程 env 已确认）

真正错位的是**节奏**，而且面板文案在撒谎：

  1. inbox 只在 20 秒一圈的快照循环里被顺带排空，面板却写「通常 1~2 秒内生效」。
     保存完立刻回头看，参数当然还是旧的。
  2. `await asyncio.sleep(PERIODIC_REVIEW_INTERVAL_SECONDS)` 把睡意锁死在改数之前：
     默认 3 小时的巡群间隔，就算面板改成 30 秒，这一次也照旧睡满 3 小时。
     这条是真的「保存了不生效」，不是错觉。
  3. 202 只代表「已排队」，回执却从来没被等过 —— 面板直接报「已保存并生效」。

三条都要有锁：收件单独跑起来、长觉分段睡、面板说实话并等回执。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import ceylan_control_runtime as cr          # noqa: E402
import qq_openclaw_bridge as b               # noqa: E402


class Rt:
    def __init__(self, blocked=()):
        self.blocked_ids = set(str(x) for x in blocked)

    def blocked(self, gid):
        return str(gid) in self.blocked_ids

    def snapshot(self):
        # install() 会把快照包一层（poll 挂在里面），没有这个属性就装不上
        return None


def _ns():
    async def _noop(*a, **kw):
        return None

    return {"group_memory_summary": {}, "group_memory_last_seq": {},
            "group_memory_last_update_ts": {}, "group_message_cache": {},
            "group_message_seq": {}, "group_last_periodic_seq": {},
            "periodic_answer_fingerprints": {}, "histories": {},
            "GROUP_MEMORY_STATE_FILE": "/tmp/none-memory.json",
            "PERIODIC_STATE_FILE": "/tmp/none-periodic.json",
            "GROUP_AUTO_REPLY_COOLDOWN": 12, "GROUP_AUTO_REPLY_EVERY": 5,
            "reply_chars": 45, "reply_tokens": 150, "bubbles": 3, "delay_ms": 700,
            "patrol_enabled": True, "memory_enabled": True, "style_note": "",
            # install() 会把这些包一层，缺一个就 KeyError，测不到收件任务本身
            "handle_group_message": _noop, "handle_private_message": _noop,
            "periodic_group_review": _noop, "compact_group_memory_once": _noop,
            "run_agent_task": _noop, "call_openclaw": _noop, "call_mimo_vision": _noop,
            "send_group_msg": _noop, "send_private_msg": _noop,
            "build_group_recent_text": lambda *a, **kw: ("", 0, 0, 0, [])}


def _command(ns, values):
    return {"action": "settings", "values": values,
            "revision": cr.revision(cr.settings(ns)),
            "id": "a" * 32, "expires": time.time() + 600}


class DrainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self._p = patch.object(cr, "ROOT", self.root)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        # LEDGER / OPTIONS 是模块级的：上一条用例用过的命令 id 会被当成「已经执行过」，
        # 于是这一条直接被跳过，测出来的永远是旧值。每条用例都要拿到干净的一份。
        self._ledger = dict(cr.LEDGER)
        cr.LEDGER.clear()
        self.addCleanup(lambda: (cr.LEDGER.clear(), cr.LEDGER.update(self._ledger)))
        self._task = cr.INBOX_TASK
        cr.INBOX_TASK = None
        self.addCleanup(lambda: setattr(cr, "INBOX_TASK", self._task))

    def _drop(self, cmd):
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / (cmd["id"] + ".json")).write_text(json.dumps(cmd), encoding="utf-8")
        return inbox

    def test_inbox_can_be_drained_without_state_snapshot(self):
        """收件必须能单独跑：不该为了取一条命令就把 70KB 状态整份重算一遍。"""
        self.assertTrue(hasattr(cr, "drain"), "收件还埋在 poll 里，没法单独高频跑")
        ns, rt = _ns(), Rt()
        inbox = self._drop(_command(ns, {"GROUP_AUTO_REPLY_COOLDOWN": 6}))
        cr.drain(ns, rt)
        self.assertEqual(ns["GROUP_AUTO_REPLY_COOLDOWN"], 6)
        self.assertEqual(list(inbox.glob("*.json")), [])
        self.assertFalse((self.root / "state.json").exists())
        self.assertTrue((self.root / "settings.json").is_file())

    def test_drain_pushes_autonomy_quotas_to_the_running_module(self):
        import autonomy_limits
        self.addCleanup(autonomy_limits.reset)
        autonomy_limits.reset()
        ns, rt = _ns(), Rt()
        self._drop(_command(ns, {"BILI_AUTO_MAX_PER_DAY": 9}))
        cr.drain(ns, rt)
        self.assertEqual(autonomy_limits.get_int("BILI_AUTO_MAX_PER_DAY", 0), 9)

    async def test_install_registers_the_inbox_task(self):
        """保存要按秒级被接走，不能陪着 20 秒的快照循环一起转。"""
        ns, rt = _ns(), Rt()
        self.assertFalse(getattr(cr, "INBOX_TASK", None), "install 之前不该有收件任务")
        cr.install(ns, rt)
        self.addCleanup(lambda: getattr(cr, "INBOX_TASK", None)
                        and cr.INBOX_TASK.cancel())
        task = getattr(cr, "INBOX_TASK", None)
        self.assertIsNotNone(task, "install 没启动收件循环 —— 面板保存只能等 20 秒快照")
        self.assertFalse(task.done())

    async def test_inbox_loop_period_is_seconds_not_twenty(self):
        """节奏看它请求了多少秒，不看墙钟。

        原来这条是「0.15 秒内数到 4 圈」：全量跑的时候事件循环被别的用例拖住，
        数出 3 圈就红 —— 那是测试自己在抖，不是收件循环坏了。
        """
        box = {"n": 0, "slept": []}

        class _Stop(Exception):
            pass

        def _drain(ns, rt):
            box["n"] += 1

        async def _sleep(secs):
            box["slept"].append(secs)
            if len(box["slept"]) >= 5:
                raise _Stop

        with patch.object(cr, "drain", new=_drain), \
                patch.object(cr.asyncio, "sleep", new=_sleep):
            with self.assertRaises(_Stop):
                await cr._inbox_loop({}, Rt(), period=0.02)
        self.assertEqual(box["slept"], [0.02] * 5, "收件周期不是面板承诺的那几秒")
        self.assertEqual(box["n"], 5)

    async def test_inbox_loop_speaks_when_a_drain_fails(self):
        # 沉默的 worker 视为故障：收件炸了必须在日志里出声
        seen = {}

        def boom(ns, rt):
            raise OSError("disk gone")

        def fake_print(msg, *a, **kw):
            seen["line"] = msg

        with patch.object(cr, "drain", new=boom), patch("builtins.print", new=fake_print):
            task = asyncio.create_task(cr._inbox_loop({}, Rt(), period=0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertIn("控制收件", seen.get("line", ""))


class ReArmingSleepTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_sleep_wakes_when_the_value_changes(self):
        """巡群间隔从 3 小时改成 30 秒，不该等旧的那一觉睡完。"""
        clock = {"now": 0.0}
        box = {"seconds": 10800}

        async def fake_sleep(sec):
            self.assertLessEqual(sec, 5.0)
            clock["now"] += sec
            if clock["now"] >= 5.0:
                box["seconds"] = 30          # 面板刚好在这一次里改了数

        with patch.object(b.asyncio, "sleep", new=fake_sleep):
            await b._config_aware_sleep(lambda: box["seconds"])
        self.assertLess(clock["now"], 60, "改小间隔以后还在睡旧的那一觉")

    async def test_unchanged_value_is_slept_in_full(self):
        total = 0.0

        async def fake_sleep(sec):
            nonlocal total
            self.assertLessEqual(sec, 5.0)
            total += sec

        with patch.object(b.asyncio, "sleep", new=fake_sleep):
            await b._config_aware_sleep(lambda: 60)
        self.assertAlmostEqual(total, 60.0, places=2)

    async def test_zero_interval_does_not_spin(self):
        total = 0.0

        async def fake_sleep(sec):
            nonlocal total
            total += sec

        with patch.object(b.asyncio, "sleep", new=fake_sleep):
            await b._config_aware_sleep(lambda: 0)
        self.assertEqual(total, 0.0)

    def test_patrol_and_compact_loops_use_the_chunked_sleep(self):
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        self.assertIn("await _config_aware_sleep(lambda: PERIODIC_REVIEW_INTERVAL_SECONDS)", src)
        self.assertIn("await _config_aware_sleep(lambda: GROUP_MEMORY_COMPACT_INTERVAL_SECONDS)", src)


class PanelHonestyTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "website" / "console.html").read_text(encoding="utf-8")
        self.js = (ROOT / "website" / "static" / "console.js").read_text(encoding="utf-8")

    def test_no_mythical_one_or_two_second_promise(self):
        for text, name in ((self.html, "console.html"), (self.js, "console.js")):
            self.assertNotIn("1~2 秒", text, name + " 还在承诺做不到的秒数")
            self.assertNotIn("立刻生效", text, name + " 还在说立刻生效")

    def test_panel_states_the_real_wait(self):
        self.assertIn("2 秒", self.html)
        self.assertIn("2 秒", self.js)

    def test_save_waits_for_the_bots_receipt(self):
        # 202 只是排队；没等到机器人回执就没有资格报成功
        self.assertIn("waitAck(", self.js)
        self.assertNotIn("高级设置已保存并生效", self.js)

    def test_ingest_note_is_shared_by_group_and_private(self):
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        head = src.index("async def _handle_private_message")
        block = src[head:head + 9000]
        self.assertIn("_note_at_ingest", block, "私聊入库还是不看图，第一张表情只能靠原图硬猜")


if __name__ == "__main__":
    unittest.main()
