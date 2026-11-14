# -*- coding: utf-8 -*-
"""回归锁：触发账本必须落盘，重启不能丢光「她刚才看到了什么」。

线上活跃丢失：trigger_ledger 原来纯内存，进程一重启 _SCENES 清空，
发话时取不到任何触发证据 —— 动作没依据、整理取不到来源事件，
belief/self_view/action 三个准入门跟着一起空。这里锁住「重启后还在」。
"""
import os
import sys
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import trigger_ledger as tl        # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trig_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        tl.reset()

    def tearDown(self):
        tl.reset()
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def _restart(self):
        # 模拟进程重启：内存清空、尚未从盘里读回。
        tl._SCENES.clear()
        tl._loaded = False
        tl._INPUT_EVIDENCE.set(None)


class PersistenceTests(Base):
    def test_note_survives_restart(self):
        now = time.time()
        self.assertTrue(tl.note("group:1", "evt_a", ts=now))
        self._restart()
        self.assertEqual(tl.pending("group:1", now=now), ["evt_a"],
                         "重启后触发证据丢了，发话又说不出为什么")

    def test_latest_survives_restart(self):
        now = time.time()
        tl.note("private:9", "evt_old", ts=now - 5)
        tl.note("private:9", "evt_new", ts=now)
        self._restart()
        self.assertEqual(tl.latest("private:9"), "evt_new")

    def test_order_and_dedupe_survive_restart(self):
        now = time.time()
        tl.note("group:1", "evt_a", ts=now - 2)
        tl.note("group:1", "evt_b", ts=now - 1)
        tl.note("group:1", "evt_a", ts=now)   # 重记推到最新，不重复占位
        self._restart()
        self.assertEqual(tl.pending("group:1", now=now), ["evt_b", "evt_a"])

    def test_ttl_still_applies_after_restart(self):
        now = time.time()
        tl.note("group:1", "evt_stale", ts=now - 100000)
        tl.note("group:1", "evt_fresh", ts=now)
        self._restart()
        self.assertEqual(tl.pending("group:1", now=now), ["evt_fresh"],
                         "过期事件不该因为落盘就复活")

    def test_reset_clears_disk_too(self):
        now = time.time()
        tl.note("group:1", "evt_a", ts=now)
        tl.reset()
        self._restart()
        self.assertEqual(tl.pending("group:1", now=now), [])

    def test_corrupt_file_is_preserved_not_silently_lost(self):
        path = tl._file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        tl._loaded = False
        now = time.time()
        tl.note("group:1", "evt_a", ts=now)
        self.assertTrue(os.path.exists(path + ".corrupt"),
                        "损坏的旧账本要先留底，不能直接当空覆盖掉")
        self.assertEqual(tl.pending("group:1", now=now), ["evt_a"])


if __name__ == "__main__":
    unittest.main()