"""回归锁：归档不删除 + 可复活检索 + 知识库旧条目类别回填。

定下的口径是「老化 = 归档，不是删除；归档仍可被显式查到」。
这条锁同时盯着两件实际会发生的事：
  · 现网 world_knowledge 里 16 条旧记录既没有 category 也没有 status，
    读的时候全靠默认值兜住，但它们会全被算进「其他」这一类，
    新类别一涨起来它们就被优先降级 —— 老记忆被无声挤掉；
  · 归档判据必须用「多久没被用上」（memory_usage 的 last_used），
    不能用写入时间，否则刚写下的重点记忆会因为「还没被用过」被归档。
"""
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

import group_memory_store as gms        # noqa: E402
import knowledge_base as kb             # noqa: E402
import memory_archive as ma             # noqa: E402
import memory_usage as mu               # noqa: E402


class GroupArchiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="archg_")
        self._f = gms._FILE
        gms._FILE = os.path.join(self.dir, "memory", "group_facts.json")
        gms._cache["data"] = None
        gms._cache["ts"] = 0.0
        gms._INDEX["sig"] = None
        # 使用账必须一起隔离：归档判据读的就是它。
        # memory_usage 现在按 DATA_DIR 在调用时解析路径，所以改环境就够；
        # 留在这里不隔离的话，测试会往真 data/ 写一份假 last_used。
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        mu._cache.update({"sig": None, "stats": {}})

    def tearDown(self):
        gms._FILE = self._f
        gms._cache["data"] = None
        gms._cache["ts"] = 0.0
        gms._INDEX["sig"] = None
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        mu._cache.update({"sig": None, "stats": {}})
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self, n=6, old_days=400):
        recs = []
        base = time.time() - old_days * 86400
        for i in range(n):
            r = gms.add_entry("7001", "话题%d" % i, "这条记忆的内容编号 %d，够长一点。" % i,
                              ts=base + i)
            recs.append(r)
        return recs

    def test_archive_never_deletes_rows(self):
        recs = self._seed()
        n_before = len(gms.get_entries("7001"))
        res = ma.archive_stale(store="group_facts", group_id="7001",
                              unused_days=30, now=time.time())
        entries = gms.get_entries("7001")
        self.assertEqual(len(entries), n_before, "归档把条目删了")
        self.assertGreater(res["archived"], 0, res)

    def test_archived_entries_excluded_from_default_context(self):
        self._seed()
        ma.archive_stale(store="group_facts", group_id="7001", unused_days=30,
                         now=time.time())
        ctx = gms.build_context("7001", query="编号 0 的那条")
        for t in gms.get_entries("7001"):
            if t.get("status") == "archived":
                self.assertNotIn(str(t.get("text"))[:12], ctx)

    def test_recently_used_old_entry_is_not_archived(self):
        # 判据必须是 last_used，不是写入时间 —— 不然刚被频繁用到的老记忆会先被清
        recs = self._seed(old_days=400)
        k = mu.key_group(recs[0].get("id"))
        now = time.time()
        mu._save_stats({"keys": {k: {"used": 5, "last_used": now - 3600,
                                     "first_seen": now - 400 * 86400}}, "daily": {}})
        ma.archive_stale(store="group_facts", group_id="7001", unused_days=30, now=now)
        self.assertNotEqual((gms.get_entries("7001")[0] or {}).get("status"), "archived",
                            "40 天前写的但一直在用，照样被归档了")

    def test_explicit_search_can_revive_archived(self):
        recs = self._seed()
        ma.archive_stale(store="group_facts", group_id="7001", unused_days=30,
                         now=time.time())
        hit = gms.build_context("7001", query="编号 0 的那条", include_archived=True)
        self.assertIn("编号 0", hit)

    def test_revive_marks_active_and_keeps_history(self):
        recs = self._seed()
        ma.archive_stale(store="group_facts", group_id="7001", unused_days=30,
                         now=time.time())
        e = gms.get_entries("7001")[0]
        self.assertEqual(e.get("status"), "archived", e)
        before = e.get("archived_at")
        ma.revive(store="group_facts", group_id="7001", key=mu.key_group(e.get("id")))
        after = [x for x in gms.get_entries("7001") if x.get("id") == e.get("id")][0]
        self.assertEqual(after.get("status"), "active")
        self.assertEqual(after.get("archived_at"), before, "复活不该抹掉归档痕迹")
        self.assertGreaterEqual(int(after.get("revived") or 0), 1)


class KbBackfillTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="archkb_")
        self._f = kb._FILE
        kb._FILE = os.path.join(self.dir, "memory", "world_knowledge.json")
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})

    def tearDown(self):
        kb._FILE = self._f
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})
        shutil.rmtree(self.dir, ignore_errors=True)

    def _legacy(self):
        os.makedirs(os.path.dirname(kb._FILE), exist_ok=True)
        legacy = {"topics": [{"key": "荒坂塔", "title": "赛博朋克2077 荒坂塔",
                              "summary": "荒坂塔是荒坂公司的总部，在夜之城中心。",
                              "ts": time.time(), "source": "bing"}]}
        with open(kb._FILE, "w", encoding="utf-8") as f:
            json.dump(legacy, f, ensure_ascii=False)
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})

    def test_legacy_entries_get_real_categories_not_其他_forever(self):
        self._legacy()
        async def llm(prompt):
            return json.dumps({"keep": True, "category": "作品设定"}, ensure_ascii=False)
        r = ma.backfill_categories(kb.load(), llm=llm)
        self.assertGreaterEqual(r["done"], 1, r)
        it = kb.get("荒坂塔")
        self.assertEqual(it.get("category"), "作品设定", it)

    def test_backfill_is_idempotent(self):
        self._legacy()
        async def llm(prompt):
            return json.dumps({"keep": True, "category": "作品设定"}, ensure_ascii=False)
        ma.backfill_categories(kb.load(), llm=llm)
        first = kb.get("荒坂塔")["category"]
        r2 = ma.backfill_categories(kb.load(), llm=llm)
        self.assertEqual(r2["skipped"], r2["total"], r2)
        self.assertEqual(kb.get("荒坂塔")["category"], first)

    def test_broken_llm_does_not_storm_the_writes(self):
        self._legacy()
        async def bad(prompt):
            raise RuntimeError("模型不可用")
        r = ma.backfill_categories(kb.load(), llm=bad)
        self.assertEqual(r["failed"], 1, r)
        self.assertEqual(kb.get("荒坂塔")["category"], "其他",
                         "判定失败时不该改类别，也不该抛")

    def test_backfill_does_not_touch_content(self):
        self._legacy()
        before = kb.get("荒坂塔")
        async def llm(prompt):
            return json.dumps({"keep": True, "category": "人物"}, ensure_ascii=False)
        ma.backfill_categories(kb.load(), llm=llm)
        after = kb.get("荒坂塔")
        self.assertEqual(before["summary"], after["summary"])
        self.assertEqual(before["ts"], after["ts"])


if __name__ == "__main__":
    unittest.main()
