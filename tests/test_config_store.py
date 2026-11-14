"""回归锁：config.env 的机制化写入（原子、归一重复键、可回滚）。

这条锁针对的都是已经发生过的真实故障形状，不是假想：
  · 面板 re.sub(count=1) 只改第一处，launcher 逐行赋值「后出现的赢」
    → 同一个键写过两次的话，改完重启就被旧值盖回去，用户看到的是「没生效」；
  · write_text 直接截断原文件，崩在中间 config.env 就成半截，下次起不来；
  · 回滚靠人记得先 cp，那是纪律不是机制。
"""
import io
import os
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import config_store as cs        # noqa: E402

SAMPLE = (u"# 她的配置\nBOT_TRIGGER_WORDS=清漪,我\nWEB_HOST=0.0.0.0\n"
          u"LLM_TEXT_API_KEY=\"abc\"\nX=1\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cfg_")
        self.p = os.path.join(self.dir, "config.env")
        io.open(self.p, "w", encoding="utf-8").write(SAMPLE)

    def text(self):
        return io.open(self.p, encoding="utf-8").read()


class WriteTests(Base):
    def test_replaces_in_place_and_keeps_comments(self):
        r = cs.set_values({"BOT_TRIGGER_WORDS": "小清"}, self.p)
        self.assertTrue(r["ok"], r)
        t = self.text()
        self.assertIn(u"# 她的配置", t, "注释被吃掉了")
        self.assertIn("WEB_HOST=0.0.0.0", t)
        self.assertIn("BOT_TRIGGER_WORDS=小清", t)

    def test_appends_missing_key(self):
        cs.set_values({"NEW_FLAG": "1"}, self.p)
        self.assertEqual(cs.read_values(self.p)["NEW_FLAG"], "1")

    def test_quoted_value_still_readable_by_launcher_semantics(self):
        cs.set_values({"TRIG": "a,b"}, self.p)
        self.assertEqual(cs.read_values(self.p)["TRIG"], "a,b")
        self.assertEqual(cs.read_values(self.p)["LLM_TEXT_API_KEY"], "abc")


class DuplicateKeyTests(Base):
    """这个类锁的就是「改了没生效」那个真 bug。"""

    def test_duplicate_keys_are_detected(self):
        io.open(self.p, "w", encoding="utf-8").write(
            SAMPLE + u"BOT_TRIGGER_WORDS=老值\n")
        self.assertEqual(cs.duplicate_keys(self.p), ["BOT_TRIGGER_WORDS"])

    def test_write_collapses_duplicates_so_change_survives_reload(self):
        io.open(self.p, "w", encoding="utf-8").write(SAMPLE + u"BOT_TRIGGER_WORDS=老值\n")
        cs.set_values({"BOT_TRIGGER_WORDS": "新值"}, self.p)
        lines = [l for l in self.text().splitlines()
                 if l.startswith("BOT_TRIGGER_WORDS=")]
        self.assertEqual(len(lines), 1, lines)
        # load_env 是「后出现的赢」，只剩一行就不可能再被盖回去
        self.assertEqual(cs.read_values(self.p)["BOT_TRIGGER_WORDS"], "新值")

    def test_other_keys_untouched_by_collapse(self):
        io.open(self.p, "w", encoding="utf-8").write(SAMPLE + u"X=2\n")
        cs.set_values({"BOT_TRIGGER_WORDS": "z"}, self.p)
        vals = cs.read_values(self.p)
        self.assertEqual(vals["X"], "2", "归并重复键时把别的键也动了")


class AtomicTests(Base):
    def test_no_temp_file_left_behind(self):
        cs.set_values({"X": "9"}, self.p)
        leftovers = [f for f in os.listdir(self.dir) if ".tmp" in f]
        self.assertEqual(leftovers, [], leftovers)

    @unittest.skipUnless(os.name == "posix",
                         "Windows 的 os.chmod 只能切只读位，0600 在这台机器上量不出来")
    def test_file_mode_is_600(self):
        os.chmod(self.p, 0o644)     # 模拟被人放宽过
        cs.set_values({"X": "9"}, self.p)
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), "0o600",
                         "这文件里全是密钥")

    def test_chmod_failure_does_not_break_the_write(self):
        # 本机量不出 0600，但「写完仍然是完整可读的」这件事必须成立
        r = cs.set_values({"X": "9"}, self.p)
        self.assertTrue(r["ok"], r)
        self.assertEqual(cs.read_values(self.p)["X"], "9")


class RollbackTests(Base):
    def test_every_change_leaves_a_generation(self):
        cs.set_values({"X": "2"}, self.p)
        cs.set_values({"X": "3"}, self.p)
        self.assertEqual(len(cs.generations(self.p)), 2)

    def test_restore_goes_back_to_previous_value(self):
        cs.set_values({"BOT_TRIGGER_WORDS": "第一次"}, self.p)
        cs.set_values({"BOT_TRIGGER_WORDS": "第二次"}, self.p)
        r = cs.restore(self.p, which=-1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(cs.read_values(self.p)["BOT_TRIGGER_WORDS"], "第一次",
                         "which=-1 应该回到最后一次改动之前")

    def test_rollback_is_itself_reversible(self):
        # 回错了还能再回来 —— 回滚不留档的话，第二步就无路可走
        cs.set_values({"BOT_TRIGGER_WORDS": "第一次"}, self.p)
        cs.set_values({"BOT_TRIGGER_WORDS": "第二次"}, self.p)
        cs.restore(self.p, which=-1)
        self.assertEqual(cs.read_values(self.p)["BOT_TRIGGER_WORDS"], "第一次")
        cs.restore(self.p, which=-1)
        self.assertEqual(cs.read_values(self.p)["BOT_TRIGGER_WORDS"], "第二次",
                         "回滚前的现场没留住，回错了就只能手改配置文件")

    def test_restore_does_not_snapshot_the_current_file_as_history(self):
        # 这一条盯着 restore() 里「先算目标还是先留档」的顺序：
        # 顺序反了的话 which=-1 会指向刚留的那份当前文件，回滚变成原地复制却报成功
        cs.set_values({"BOT_TRIGGER_WORDS": "唯一一次改动"}, self.p)
        cur = self.text()
        cs.restore(self.p, which=-1)
        self.assertNotEqual(self.text(), cur,
                            "restore(which=-1) 原地复制了当前文件还返回 ok")

    def test_generations_are_bounded(self):
        old = cs.GENERATIONS
        cs.GENERATIONS = 3
        try:
            for i in range(7):
                cs.set_values({"X": str(i)}, self.p)
            self.assertLessEqual(len(cs.generations(self.p)), 3)
        finally:
            cs.GENERATIONS = old

    def test_noop_write_does_not_bloat_generations(self):
        cs.set_values({"X": "1"}, self.p)
        n = len(cs.generations(self.p))
        cs.set_values({"X": "1"}, self.p)      # 值没变
        self.assertEqual(len(cs.generations(self.p)), n, "重复写同一个值也存代次")

    def test_missing_file_reports_instead_of_crashing(self):
        missing = os.path.join(self.dir, "nope.env")
        r = cs.set_values({"A": "1"}, missing)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"], "失败了却不说明原因")
        self.assertIn("nope.env", r["error"], r["error"])
        self.assertFalse(os.path.exists(missing), "报错了却顺手建了个空文件")


class ReadTests(Base):
    def test_read_values_matches_file(self):
        v = cs.read_values(self.p)
        self.assertEqual(v["WEB_HOST"], "0.0.0.0")

    def test_empty_file_is_not_an_error(self):
        io.open(self.p, "w", encoding="utf-8").write("")
        self.assertEqual(cs.read_values(self.p), {})
        self.assertEqual(cs.duplicate_keys(self.p), [])


if __name__ == "__main__":
    unittest.main()
