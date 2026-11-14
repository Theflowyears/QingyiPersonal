# -*- coding: utf-8 -*-
"""星座按人固定 + 摇卦算欠交货 的回归锁。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import member_memory as mm


class ZodiacTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zodiac_")
        self.addCleanup(self.tmp.cleanup)
        self._old = mm.PROFILE_FILE
        mm.PROFILE_FILE = os.path.join(self.tmp.name, "member_profiles.json")
        mm._cache.update({"data": None, "ts": 0})
        self.addCleanup(self._restore)

    def _restore(self):
        mm.PROFILE_FILE = self._old
        mm._cache.update({"data": None, "ts": 0})

    def test_set_and_read_zodiac(self):
        mm.set_zodiac("923", "9000000003", "天秤座")
        self.assertEqual(mm.zodiac_of("923", "9000000003"), "天秤座")

    def test_zodiac_shows_in_context_and_is_per_person(self):
        mm.set_zodiac("923", "9000000003", "天秤座")
        mm.set_zodiac("923", "9000000004", "狮子座")
        ctx_a = mm.context_for("923", "9000000003")
        ctx_f = mm.context_for("923", "9000000004")
        self.assertIn("天秤座", ctx_a)
        self.assertNotIn("狮子座", ctx_a)
        self.assertIn("狮子座", ctx_f)

    def test_clear_zodiac(self):
        mm.set_zodiac("923", "1", "天秤座")
        mm.set_zodiac("923", "1", "")
        self.assertEqual(mm.zodiac_of("923", "1"), "")


class OwedRegexTests(unittest.TestCase):
    def test_yao_gua_is_owed(self):
        import qq_openclaw_bridge as b
        self.assertTrue(b._OWED_TOOL.search("帮我摇一卦"))
        self.assertTrue(b._OWED_TOOL.search("重新摇一卦给你"))
        self.assertTrue(b._OWED_TOOL.search("抽三张塔罗牌"))


if __name__ == "__main__":
    unittest.main()