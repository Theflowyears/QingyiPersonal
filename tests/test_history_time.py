"""回归锁：会话列表上那个时间，必须是「上次真的聊过」而不是「上次写盘」。

用户报的现象很具体：「每次点进去都是 1~13 分钟随机，即使实际已经过去几天了」。
根因是 `save_histories()` 给**每一个**会话都盖 `time.time()`，
而写盘会被别人的对话、巡群压缩、清死群触发 —— 于是所有会话一起变成「刚刚」，
显示出来的分钟数其实就是「距离上一次有人聊天过了多久」，跟这段会话无关。

这个锁要钉的是三件事：
  1. 只有真的被追加了一轮的会话，时间才前进；
  2. 压缩 / 清群这类「不是新话」的写盘，不许动任何人的时间；
  3. 重启以后（load → 再 save）时间仍然是真的，不能悄悄归零或跳到当下。
"""
import os
import sys
import tempfile
import time
import unittest
import json

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b        # noqa: E402


def _read():
    with open(b.HISTORY_STORE, encoding="utf-8") as f:
        return json.load(f)


class HistoryTimeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="histime_")
        self._store = b.HISTORY_STORE
        b.HISTORY_STORE = os.path.join(self.dir, "memory", "histories.json")
        self._hist = dict(b.histories)
        self._ts = dict(b._history_last_ts)
        b.histories.clear()
        b._history_last_ts.clear()

    def tearDown(self):
        b.HISTORY_STORE = self._store
        b.histories.clear()
        b.histories.update(self._hist)
        b._history_last_ts.clear()
        b._history_last_ts.update(self._ts)

    def test_only_the_session_that_talked_moves_forward(self):
        old = time.time() - 5 * 86400
        b.histories["private-user-1001"].append(("user", "几天前那句话"))
        b.histories["private-user-2002"].append(("user", "刚刚那句"))
        b._history_last_ts["private-user-1001"] = old
        now = time.time()
        b.touch_history("private-user-2002", now)
        b.save_histories()
        d = _read()
        self.assertAlmostEqual(d["private-user-2002"]["updated"], now, delta=2)
        self.assertLess(d["private-user-1001"]["updated"], old + 5,
                        "没聊过的那段被刷成刚刚了")

    def test_compaction_style_write_does_not_touch_anyone(self):
        # 巡群压缩、清死群这些调用 save_histories() 的地方没有新话，
        # 谁的时间都不该动 —— 以前正是这些调用把全部会话刷成「刚刚」
        old = time.time() - 3 * 86400
        for k in ("group-1|9001", "private-user-3003"):
            b.histories[k].append(("user", "旧话"))
            b._history_last_ts[k] = old
        b.save_histories()
        b.save_histories()
        d = _read()
        for k in ("group-1|9001", "private-user-3003"):
            self.assertLess(d[k]["updated"], old + 5, "%s 被写盘刷成新时间了" % k)

    def test_restart_keeps_the_real_times(self):
        old = time.time() - 8 * 86400
        b.histories["private-user-4004"].append(("user", "上周说的"))
        b._history_last_ts["private-user-4004"] = old
        b.save_histories()
        # 模拟重启：清空内存再从盘上恢复
        b.histories.clear()
        b._history_last_ts.clear()
        b.load_histories()
        self.assertLess(float(b._history_last_ts.get("private-user-4004") or 0),
                        old + 5, "重启把真实时间丢了")
        b.save_histories()
        d = _read()
        self.assertLess(d["private-user-4004"]["updated"], time.time() - 7 * 86400,
                        "重启后第一次写盘就变成刚刚")

    def test_legacy_record_without_time_is_not_claimed_as_now(self):
        # 老数据里没有可靠时间时，宁可显示未知，也不许编一个「刚刚」
        os.makedirs(os.path.dirname(b.HISTORY_STORE), exist_ok=True)
        with open(b.HISTORY_STORE, "w", encoding="utf-8") as f:
            json.dump({"private-user-5005": {"kind": "private", "user_id": "5005",
                                             "turns": [{"role": "user",
                                                        "content": "旧对话"}]}}, f)
        b.histories.clear()
        b._history_last_ts.clear()
        b.load_histories()
        b.save_histories()
        d = _read()
        self.assertEqual(float(d["private-user-5005"].get("updated") or 0), 0.0,
                         "没有时间依据却报了个当下")

    def test_touch_history_survives_bad_input(self):
        b.touch_history("private-user-6006", "不是数字")   # 不该抛
        b.touch_history(None)
        self.assertTrue(True)


class DashboardPassthroughTests(unittest.TestCase):
    def test_memories_route_passes_updated_through(self):
        with open(os.path.join(BOT, os.pardir, "website", "ceylan_dashboard.py"),
                  encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("def api_memories"):]
        seg = seg[:seg.index("def api_memory_detail")]
        self.assertIn('rec.get("updated", 0)', seg,
                      "路由里要是又算了时间，写盘的修复就白做了")


if __name__ == "__main__":
    unittest.main()
