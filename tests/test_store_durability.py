"""防丢回归锁：五个常驻 JSON 库读损坏时不许把原始字节覆盖成空。

线上「她似乎会丢失数据」的根因之一：这些库的 _load 一旦 json.load 抛错，
就退回空对象并缓存，下一次写盘把原本还能人工抢救的文件永久覆盖掉。
这里钉死一条底线：读不动的原始文件必须先搬进 .corrupt 留底，
而且后续写盘绝不能把这份留底再抹掉。顺带钉写盘要 fsync（断电耐久）。
"""
import glob
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

CORRUPT = '{ "groups": { "g1": { "entries": [ 这不是合法 JSON'.encode("utf-8")


class DurabilityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dur_")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _write_corrupt(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(CORRUPT)

    def _sidecars(self, path):
        return sorted(glob.glob(path + ".corrupt*"))

    def _assert_preserved(self, path):
        cars = self._sidecars(path)
        self.assertTrue(cars, "读损坏后没留底，原始字节会被下一次写盘永久覆盖")
        with open(cars[0], "rb") as f:
            self.assertEqual(f.read(), CORRUPT, "留底内容必须和原始损坏文件一字不差")
        return cars[0]


class GroupFactsDurability(DurabilityBase):
    def test_corrupt_read_is_preserved_and_survives_the_next_write(self):
        import group_memory_store as gms
        path = os.path.join(self.dir, "group_facts.json")
        with patch.object(gms, "_FILE", path):
            gms._cache["data"] = None
            self._write_corrupt(path)
            self.assertEqual(gms._load(force=True), {"groups": {}})
            car = self._assert_preserved(path)
            gms.add_entry("g1", "话题", "一条新记忆")
            self.assertTrue(os.path.isfile(car), "写盘把留底抹掉了")
            with open(car, "rb") as f:
                self.assertEqual(f.read(), CORRUPT)

    def test_a_normal_write_fsyncs_before_replace(self):
        import group_memory_store as gms
        path = os.path.join(self.dir, "group_facts.json")
        with patch.object(gms, "_FILE", path):
            gms._cache["data"] = None
            with patch("os.fsync", wraps=os.fsync) as fsync:
                self.assertTrue(gms._save({"groups": {}}))
            self.assertTrue(fsync.called, "写盘没 fsync，断电可能留个空壳")
            self.assertEqual(gms._load(force=True), {"groups": {}})


class CuriosityDurability(DurabilityBase):
    def test_corrupt_read_is_preserved_and_survives_the_next_write(self):
        import curiosity as cu
        with patch.dict(os.environ, {"DATA_DIR": self.dir}):
            cu._reset_cache()
            self.addCleanup(cu._reset_cache)
            path = cu._file()
            self._write_corrupt(path)
            cu._data()
            car = self._assert_preserved(path)
            cu._save({"topics": {}, "actions": {}, "shares": [], "pending_share": None})
            self.assertTrue(os.path.isfile(car))
            with open(car, "rb") as f:
                self.assertEqual(f.read(), CORRUPT)


class MemberMemoryDurability(DurabilityBase):
    def test_corrupt_read_is_preserved_and_survives_the_next_write(self):
        import member_memory as mm
        path = os.path.join(self.dir, "member_profiles.json")
        with patch.object(mm, "PROFILE_FILE", path):
            mm._cache["data"] = None
            self._write_corrupt(path)
            self.assertEqual(mm._load(force=True), {})
            car = self._assert_preserved(path)
            mm._save({"g1": {"22001": {"nickname": "阿甲"}}})
            self.assertTrue(os.path.isfile(car))
            with open(car, "rb") as f:
                self.assertEqual(f.read(), CORRUPT)


class WhisperDurability(DurabilityBase):
    def test_corrupt_read_is_preserved_and_survives_the_next_write(self):
        import whisper as wh
        path = os.path.join(self.dir, "whisper.json")
        with patch.object(wh, "_FILE", path):
            wh._cache["data"] = None
            self._write_corrupt(path)
            wh._load(force=True)
            car = self._assert_preserved(path)
            self.assertTrue(wh._save(wh._empty()))
            self.assertTrue(os.path.isfile(car))
            with open(car, "rb") as f:
                self.assertEqual(f.read(), CORRUPT)


class KnowledgeBaseDurability(DurabilityBase):
    def test_corrupt_read_is_preserved_and_survives_the_next_write(self):
        import knowledge_base as kb
        path = os.path.join(self.dir, "world_knowledge.json")
        with patch.object(kb, "_FILE", path):
            kb._CACHE.update({"sig": None, "items": [], "tokens": {}})
            self._write_corrupt(path)
            self.assertEqual(kb.load(), [])
            car = self._assert_preserved(path)
            self.assertTrue(kb.save([]))
            self.assertTrue(os.path.isfile(car))
            with open(car, "rb") as f:
                self.assertEqual(f.read(), CORRUPT)


class ConcurrentAtomicWrites(DurabilityBase):
    def test_concurrent_writers_never_share_a_tempfile(self):
        import json
        import safe_store
        import threading
        from concurrent.futures import ThreadPoolExecutor
        path = os.path.join(self.dir, "concurrent.json")
        barrier = threading.Barrier(8)
        real_dump = json.dump
        sources = []
        def synchronized_dump(data, handle, **kwargs):
            sources.append(os.fstat(handle.fileno()).st_ino)
            barrier.wait(timeout=10)
            return real_dump(data, handle, **kwargs)
        with patch("safe_store.json.dump", side_effect=synchronized_dump):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda i: safe_store.atomic_write_json(path, {"writer": i}), range(8)))
        self.assertEqual(8, len(set(sources)))
        self.assertTrue(all(results))
        with open(path, encoding="utf-8") as handle:
            self.assertIn(json.load(handle)["writer"], range(8))
        self.assertEqual([], glob.glob(os.path.join(self.dir,"*.tmp")))


if __name__ == "__main__":
    unittest.main()