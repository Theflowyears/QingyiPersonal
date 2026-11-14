"""回归锁：会话流水 append-only、每轮自带时间。

这份流水存在的唯一理由，是把「上次聊过」从**文件的写盘时刻**换成**那句话发生的时刻**。
以前的形状是：轮次只存 role/content，没有时间，于是 updated 只能取写盘时间；
而写盘会被任何一次回复、巡群压缩、清死群触发 ——
所以一段搁了五天的对话也会显示「3 分钟前」，而且每次点进去数值都不一样。

因此这里最重要的一条是：时间没有记录时，宁可返回 0（面板显示 --），
绝对不许拿 now 兜底。用 now 兜底就是那个 bug 本来的样子。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import history_log as hl        # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hlog_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)


class AppendTests(Base):
    def test_each_turn_carries_its_own_time(self):
        t0 = time.time() - 5 * 86400
        hl.append("private-user-1", "user", u"几天前问的一句", t0)
        hl.append("private-user-1", "assistant", u"几天前答的一句", t0 + 1)
        rows = hl.replay("private-user-1")
        self.assertEqual(len(rows), 2, rows)
        self.assertAlmostEqual(rows[0]["ts"], t0, delta=1)
        self.assertEqual(rows[0]["role"], "user")

    def test_last_ts_is_the_newest_turn_not_the_write_time(self):
        old = time.time() - 86400 * 4
        hl.append("k1", "user", u"四天前", old)
        hl.append("k2", "user", u"刚刚", time.time())
        self.assertLess(hl.last_ts("k1"), time.time() - 86400 * 3,
                        "别的会话写盘把这段的时间带走了")
        self.assertAlmostEqual(hl.last_ts("k2"), time.time(), delta=5)

    def test_unknown_session_returns_zero_not_now(self):
        # 「不知道」和「刚刚」是两件事。用 now 兜底就是这个 bug 的原样。
        self.assertEqual(hl.last_ts("从没有过的会话"), 0.0)

    def test_append_never_raises_on_bad_input(self):
        self.assertFalse(hl.append("", "user", u"没有 key 不该记"))
        self.assertTrue(hl.append("k", "user", None))

    def test_lines_are_valid_json_and_one_per_line(self):
        for i in range(3):
            hl.append("kk", "user", u"第%d句" % i)
        with io.open(hl._path(), encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        for l in lines:
            json.loads(l)

    def test_long_content_is_capped(self):
        hl.append("cap", "user", u"啊" * 5000)
        self.assertLessEqual(len(hl.replay("cap")[0]["content"]), 2000)


class ReplayTests(Base):
    def test_replay_is_in_time_order_across_rotation(self):
        for i in range(4):
            hl.append("rot", "user", u"第%d句" % i, time.time() + i)
        old_seg = hl._path() + ".1"
        os.makedirs(os.path.dirname(old_seg), exist_ok=True)
        # 手工制造一次轮转：旧段在前，当前段在后，时间序必须接得上
        with io.open(old_seg, "w", encoding="utf-8") as f:
            f.write(json.dumps({"key": "rot", "role": "user",
                                "content": u"很早的一句", "ts": 1000.0},
                               ensure_ascii=False) + "\n")
        rows = hl.replay("rot")
        self.assertEqual(rows[0]["content"], u"很早的一句", rows)
        self.assertEqual(rows[-1]["content"], u"第3句", rows)

    def test_rotation_keeps_data_instead_of_deleting(self):
        small = int(os.getenv("HISTORY_LOG_MAX_BYTES", "8000000"))
        old = hl.MAX_BYTES
        hl.MAX_BYTES = 200
        try:
            for i in range(30):
                hl.append("big", "user", u"很长的一句话用来把文件撑大" * 3)
            self.assertTrue(os.path.isfile(hl._path() + ".1"),
                            "轮转应该把旧段搬走，不是删掉")
            self.assertGreater(len(hl.replay("big", n=1000)), 0)
        finally:
            hl.MAX_BYTES = old
        del small

    def test_corrupt_line_is_skipped_not_fatal(self):
        os.makedirs(hl._dir(), exist_ok=True)
        with io.open(hl._path(), "w", encoding="utf-8") as f:
            f.write(u"{不是 json\n")
            f.write(json.dumps({"key": "ok", "role": "user",
                                "content": u"好的", "ts": time.time()}) + "\n")
        self.assertEqual(len(hl.replay("ok")), 1)
        self.assertEqual(hl.last_ts("ok") > 0, True)


class BridgeWiringTests(Base):
    def test_assistant_is_logged_when_delivered_not_when_generated(self):
        import qq_openclaw_bridge as bridge
        from unittest.mock import patch
        key = "private-user-history-log-test"
        bridge.histories.pop(key, None)
        self.addCleanup(bridge.histories.pop, key, None)
        with patch.object(bridge, "save_histories"):
            bridge._record_delivered(key, "刷到什么有意思的了？")
            bridge._record_delivered(key, "刷到什么有意思的了？")
        replies = [r for r in hl.tail(key) if r["role"] == "assistant"]
        self.assertEqual([r["content"] for r in replies], ["刷到什么有意思的了？"])

    def test_save_histories_reads_time_from_the_log_first(self):
        with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"),
                     encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("def save_histories"):]
        seg = seg[:seg.index("def load_histories")]
        self.assertIn("_hl.last_ts(key)", seg,
                      "会话时间还是从写盘时刻来的话，这个 bug 就还在")
        self.assertNotIn('"updated": time.time()', seg,
                         "又用写盘时刻当会话时间了")


class BackfillTests(Base):
    """把历史里写坏的时间迁成「有证据的真实值」或「未知」。

    那批值来自上一版直接取 time.time()，既不是对话时间、又每次刷新都变。
    这里钉的是：有流水就用流水；没有就清成 0 标 unknown（面板显示 --），
    绝不编一个「刚刚」。
    """

    def setUp(self):
        super().setUp()
        self.store = os.path.join(self.dir, "memory", "histories.json")
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        with io.open(self.store, "w", encoding="utf-8") as f:
            json.dump({
                "private-user-1": {"kind": "private", "user_id": "1",
                                   "turns": [{"role": "user", "content": u"旧话"}],
                                   "updated": time.time()},        # 写坏的假值
                "whisper": {"kind": "other", "turns": [],
                            "updated": time.time()},
            }, f)

    def _read(self):
        with io.open(self.store, encoding="utf-8") as f:
            return json.load(f)

    def test_session_with_log_gets_real_time(self):
        t0 = time.time() - 6 * 86400
        hl.append("private-user-1", "user", u"六天前那句", t0)
        res = hl.backfill_histories(self.store)
        self.assertTrue(res["ok"], res)
        d = self._read()
        self.assertAlmostEqual(d["private-user-1"]["updated"], t0, delta=2)
        self.assertEqual(d["private-user-1"]["updated_source"], "log")

    def test_session_without_log_becomes_unknown_not_now(self):
        res = hl.backfill_histories(self.store)
        self.assertTrue(res["ok"], res)
        d = self._read()
        self.assertEqual(d["whisper"]["updated"], 0.0,
                         "没证据却留了个「刚刚」")
        self.assertEqual(d["whisper"]["updated_source"], "unknown")
        self.assertEqual(res["unknown"], 2)

    def test_backfill_leaves_a_backup(self):
        hl.backfill_histories(self.store)
        self.assertTrue(os.path.isfile(self.store + ".bak_pre-migrate"))

    def test_backfill_is_idempotent(self):
        hl.append("private-user-1", "assistant", u"六天前那句", time.time() - 86400 * 6)
        first = hl.backfill_histories(self.store)
        second = hl.backfill_histories(self.store)
        self.assertGreater(first["from_log"] + first["unknown"], 0)
        self.assertEqual(second["from_log"], 0)
        self.assertEqual(second["unknown"], 0)
        self.assertGreaterEqual(second["already"], 2)

    def test_dry_run_does_not_write(self):
        before = self._read()["whisper"]["updated"]
        res = hl.backfill_histories(self.store, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(self._read()["whisper"]["updated"], before)

    def test_missing_file_reports(self):
        res = hl.backfill_histories(os.path.join(self.dir, "nope.json"))
        self.assertFalse(res["ok"])


class RotationPreservationTests(Base):
    """轮转只能「移位+上限」，不许第二次轮转把上一段静默覆盖掉。"""

    def _seg(self, suffix):
        with io.open(hl._path() + suffix, encoding="utf-8") as f:
            return json.loads(f.readline())["content"]

    def test_a_second_rotation_shifts_old_segments_instead_of_overwriting(self):
        from unittest.mock import patch
        os.makedirs(hl._dir(), exist_ok=True)
        with io.open(hl._path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({"key": "k", "role": "user", "content": u"当前", "ts": 3000.0}) + "\n")
        with io.open(hl._path() + ".1", "w", encoding="utf-8") as f:
            f.write(json.dumps({"key": "k", "role": "user", "content": u"较旧", "ts": 2000.0}) + "\n")
        with io.open(hl._path() + ".2", "w", encoding="utf-8") as f:
            f.write(json.dumps({"key": "k", "role": "user", "content": u"最旧", "ts": 1000.0}) + "\n")
        with patch.object(hl, "MAX_BYTES", 10), patch.object(hl, "MAX_SEGMENTS", 5):
            hl._rotate_if_needed()
            self.assertEqual(self._seg(".3"), u"最旧", "最旧段被覆盖了")
            self.assertEqual(self._seg(".2"), u"较旧", "较旧段被覆盖了")
            self.assertEqual(self._seg(".1"), u"当前")
            rows = hl.replay("k", n=100)
        self.assertEqual([r["content"] for r in rows], [u"最旧", u"较旧", u"当前"],
                         "重放必须按时间序接上所有保留段")

    def test_append_failure_is_not_silent(self):
        import builtins
        from contextlib import redirect_stdout
        from unittest.mock import patch
        buf = io.StringIO()
        with patch.object(builtins, "open", side_effect=OSError("disk full")), \
                redirect_stdout(buf):
            self.assertFalse(hl.append("k", "user", u"记不进"))
        self.assertIn(u"流水", buf.getvalue(), "写盘失败却一声不吭，沉默的 worker 视为故障")


if __name__ == "__main__":
    unittest.main()
