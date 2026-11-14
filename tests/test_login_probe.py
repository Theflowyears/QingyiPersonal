# -*- coding: utf-8 -*-
"""login_probe.status() 的展示时效回归锁。

起因：面板「最近事件」按条数取最后几条、无时效过滤，
11-09 的 container_recreated 误报连着几天挂在"昨晚"的位置上；
前端还把 reason 硬截 18 字符，括号被斩成半截（((未知，无 KickedOffLin）。
账本 events.jsonl 是流水，永不删；过期只在展示层滤。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT))
import login_probe as lp


class RecentEventsWindow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="probe_win_")
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "probe").mkdir()
        self.events = d / "probe" / "events.jsonl"
        self.state = d / "probe" / "state.json"
        patcher_events = patch.object(lp, "EVENTS", self.events)
        patcher_state = patch.object(lp, "STATE", self.state)
        patcher_events.start()
        patcher_state.start()
        self.addCleanup(patcher_events.stop)
        self.addCleanup(patcher_state.stop)
        self.state.write_text(json.dumps({
            "state": "online", "online": True, "ts_text": "2026-11-15 01:00:00",
            "last_online_ts": time.time(), "offline_since": 0,
        }), encoding="utf-8")

    def _write(self, evs):
        self.events.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs) + "\n",
            encoding="utf-8")

    def test_events_older_than_window_are_not_returned(self):
        now = time.time()
        self._write([
            {"ts": now - 100 * 3600, "type": "recreated",
             "reason": "容器被重建（0cbf6b18f7ec → f94ebd16b4ef），快速登录会失效，需重新扫码"},
            {"ts": now - 96 * 3600, "type": "offline", "reason": "(未知，无 KickedOffLine 记录)"},
            {"ts": now - 3600, "type": "recover", "reason": "ok"},
        ])
        evs = lp.status()["events"]
        types = [e["type"] for e in evs]
        self.assertNotIn("recreated", types, "100 小时前的容器事件超窗，不该再上展示")
        self.assertNotIn("offline", types, "96 小时前的掉线超 72h 窗，不该再上展示")
        self.assertIn("recover", types, "1 小时内的正常事件必须还在")

    def test_boundary_exactly_window_kept(self):
        now = time.time()
        self._write([
            {"ts": now - lp.RECENT_WINDOW_SECONDS + 5, "type": "offline", "reason": "x"},
            {"ts": now - lp.RECENT_WINDOW_SECONDS - 5, "type": "offline", "reason": "y"},
        ])
        evs = lp.status()["events"]
        self.assertEqual(len(evs), 1, "只留窗口内的，边界外一条不得漏进")
        self.assertEqual(evs[0]["reason"], "x")

    def test_no_events_file_is_ok(self):
        st = lp.status()
        self.assertEqual(st["events"], [])
        self.assertTrue(st["online"])

    def test_count_cap_still_applies_inside_window(self):
        now = time.time()
        self._write([{"ts": now - i * 60, "type": "crash", "reason": "c%d" % i}
                     for i in range(30, 0, -1)])
        evs = lp.status()["events"]
        self.assertLessEqual(len(evs), 10, "时效之外仍保留条数上限")


if __name__ == "__main__":
    unittest.main()
