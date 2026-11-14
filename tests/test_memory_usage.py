"""记忆使用仪表的回归锁。

这批只做观测、不改行为，所以它最重要的性质是两条：
  1) 记的是「真的进了提示词」的条目，不是候选池；
  2) 记账坏掉绝不能影响回复（异常全吞）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import memory_usage        # noqa: E402

class Base(unittest.TestCase):
    """隔离方式：改模块上的路径常量，不改 sys.modules。

    以前这里每次 setUp 把 memory_usage 重导一遍 —— 那是个陷阱：
    test_memory_archive 顶部已经 `import memory_usage as mu` 拿到了旧对象，
    memory_archive 内部又走 `import memory_usage` 拿新对象，
    两边读写的不是同一份账，单独跑都过，合起来跑就假失败。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="memuse_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir   # 路径是调用时算的，改环境就隔离了
        memory_usage.ENABLED = True
        memory_usage._cache.update({"sig": None, "stats": {}})

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        memory_usage._cache.update({"sig": None, "stats": {}})
        shutil.rmtree(self.dir, ignore_errors=True)


class NoteTests(Base):
    def test_log_file_is_per_day_and_append_only(self):
        memory_usage.note("group_facts", ["g:aaa", "g:bbb"], query="赤合金",
                          injected_chars=210, candidates=40)
        memory_usage.note("world_knowledge", ["kb:zz"], query="赤合金",
                          injected_chars=90, candidates=0)
        p = memory_usage._log_path()
        self.assertTrue(os.path.isfile(p), p)
        with open(p, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f.read().splitlines() if x]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["store"], "group_facts")
        self.assertEqual(lines[0]["n"], 2)
        self.assertEqual(lines[0]["chars"], 210)

    def test_keys_are_the_selected_ones_not_candidates(self):
        # candidates 只用于算选中比例，绝不能被当成「用过」
        memory_usage.note("group_facts", ["g:only1"], query="q",
                          injected_chars=50, candidates=999)
        r = memory_usage.aggregate()
        per = memory_usage._load_stats()["keys"]
        self.assertEqual(list(per.keys()), ["g:only1"])
        self.assertEqual(per["g:only1"]["used"], 1)

    def test_disabled_switch_writes_nothing(self):
        memory_usage.ENABLED = False
        memory_usage.note("group_facts", ["g:x"], query="q", injected_chars=10)
        self.assertFalse(os.path.isfile(memory_usage._log_path()))

    def test_empty_selection_still_counted_as_round(self):
        memory_usage.note("world_knowledge", [], query="q", injected_chars=0,
                          candidates=12)
        memory_usage.aggregate()
        d = memory_usage.today()
        self.assertEqual(d["rounds"], 1)
        self.assertEqual(d["selected"], 0)
        self.assertEqual(d["empty_rounds"], 1)

    def test_fallback_flag_is_recorded_separately(self):
        # 「没检索命中、走兜底」是提取质量的关键指标，不能和正常命中混在一起
        memory_usage.note("group_facts", ["g:a"], query="q", injected_chars=40, fallback=True)
        memory_usage.note("group_facts", ["g:b"], query="q", injected_chars=40, fallback=False)
        memory_usage.aggregate()
        d = memory_usage.today()
        self.assertEqual(d["rounds"], 2)
        self.assertEqual(d["fallback_rounds"], 1)
        self.assertEqual(d["empty_rounds"], 0)


class AggregateTests(Base):
    def test_same_log_is_idempotent_when_aggregated_repeatedly(self):
        memory_usage.note("group_facts", ["g:stable"], query="q", injected_chars=30)
        for _ in range(3):
            memory_usage.aggregate()
            self.assertEqual(memory_usage.usage_of("g:stable")["used"], 1)
            self.assertEqual(memory_usage.today()["selected"], 1)
        memory_usage.note("group_facts", ["g:stable"], query="q", injected_chars=30)
        memory_usage.aggregate()
        self.assertEqual(memory_usage.usage_of("g:stable")["used"], 2)
        self.assertEqual(memory_usage.today()["selected"], 2)

    def test_repeat_across_days_accumulates_and_sets_last_used(self):
        memory_usage.note("group_facts", ["g:same"], query="q", injected_chars=30)
        memory_usage.aggregate()
        first = memory_usage.usage_of("g:same")
        self.assertEqual(first["used"], 1)
        self.assertGreater(first["last_used"], 0)

        # 同一轮里同一条被两个块选中，也应该各记一次
        memory_usage.note("persona:facts", ["p:facts:abc"], query="q", injected_chars=10)
        memory_usage.note("persona:facts", ["p:facts:abc"], query="q", injected_chars=10)
        memory_usage.aggregate()
        self.assertEqual(memory_usage.usage_of("p:facts:abc")["used"], 2)

    def test_unknown_key_reads_as_zero_not_crash(self):
        self.assertEqual(memory_usage.usage_of("g:nope")["used"], 0)
        self.assertEqual(memory_usage.usage_of("")["used"], 0)

    def test_corrupt_log_line_is_skipped(self):
        os.makedirs(memory_usage._dir(), exist_ok=True)
        with open(memory_usage._log_path(), "w", encoding="utf-8") as f:
            f.write("这不是 json\n")
            f.write(json.dumps({"ts": 1.0, "store": "group_facts", "n": 1,
                                "keys": ["g:ok"], "chars": 12, "empty": 0, "fb": 0}) + "\n")
        memory_usage.aggregate()
        self.assertEqual(memory_usage.usage_of("g:ok")["used"], 1)
        self.assertEqual(memory_usage.today()["rounds"], 1)


class KeyTests(Base):
    def test_group_key_prefers_entry_id_over_content(self):
        k = memory_usage.key_group("ab12cd34", "别的东西", "别的文本")
        self.assertEqual(k, "g:ab12cd34")

    def test_group_key_without_id_falls_back_to_content_hash(self):
        k1 = memory_usage.key_group("", "主人家在重庆", "文本A")
        k2 = memory_usage.key_group("", "主人家在重庆", "文本A")
        k3 = memory_usage.key_group("", "主人在上海", "文本B")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertTrue(k1.startswith("gt:"))

    def test_persona_key_ignores_punctuation_and_case(self):
        # 「同一条记忆」在库里常常只差标点，键必须一致，否则覆盖率会虚低
        a = memory_usage.key_persona("facts", "主人家在重庆。")
        b = memory_usage.key_persona("facts", "  主人家在重庆 ")
        c = memory_usage.key_persona("facts", "主人家在重庆")
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertNotEqual(a, memory_usage.key_persona("likes", "主人家在重庆"))

    def test_member_and_private_keys(self):
        self.assertEqual(memory_usage.key_member("923", "449"), "m:923|449")
        self.assertTrue(memory_usage.key_private("private-user-1").startswith("ps:"))


class LineTests(Base):
    def test_line_says_no_data_instead_of_inventing_zero(self):
        self.assertIn("还没有使用样本", memory_usage.line())

    def test_line_reports_percentages_not_raw_counts_only(self):
        memory_usage.note("group_facts", ["g:a"], query="q", injected_chars=100)
        memory_usage.note("group_facts", [], query="q", injected_chars=0, fallback=True)
        memory_usage.aggregate()
        s = memory_usage.line()
        self.assertIn("记忆 今天2轮", s)
        self.assertIn("选中1条", s)
        self.assertIn("空命中 50%", s)
        self.assertIn("兜底 50%", s)

    def test_report_shape(self):
        memory_usage.note("group_facts", ["g:a"], query="q", injected_chars=100)
        memory_usage.aggregate()
        r = memory_usage.report()
        self.assertIn("today", r)
        self.assertIn("week", r)
        self.assertEqual(r["today"]["rounds"], 1)
        self.assertGreaterEqual(r["distinct_used"], 1)


class NeverRaiseTests(Base):
    """仪表绝不能把回复带崩 —— 这是它唯一绝对不能破的合同。"""

    def test_note_survives_unwritable_dir(self):
        os.environ["DATA_DIR"] = "/proc/definitely-not-writable-xyz"
        memory_usage.note("group_facts", ["g:a"], query="q", injected_chars=10)  # 不抛

    def test_aggregate_survives_broken_stats_file(self):
        os.makedirs(memory_usage._dir(), exist_ok=True)
        with open(memory_usage._stats(), "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        memory_usage.aggregate()
        self.assertEqual(memory_usage.usage_of("g:a")["used"], 0)
        self.assertIn("记忆", memory_usage.line() + "记忆")


if __name__ == "__main__":
    unittest.main()
