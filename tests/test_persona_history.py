# -*- coding: utf-8 -*-
"""历史回顾自我修订回归锁：用真实历史、不推进整理游标、不补造。"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import persona_growth as pg


class ReflectHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pghist_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "DATA_DIR": self.tmp.name,
            "PERSONA_DATA_DIR": os.path.join(self.tmp.name, "persona")})
        env.start()
        self.addCleanup(env.stop)
        import persona_store

        def _close_conn():
            if persona_store._conn is not None:
                persona_store._conn.close()
                persona_store._conn = None
                persona_store._conn_path = ""
        self.addCleanup(_close_conn)
        os.makedirs(os.path.join(self.tmp.name, "persona"), exist_ok=True)
        now = time.time()
        with open(pg.journal_file(), "w", encoding="utf-8") as f:
            for i in range(6):
                f.write(json.dumps({"session": "group:1", "ts": now - (i + 1) * 3600,
                                    "user": "问%d" % i, "bot": "答%d" % i,
                                    "events": ["evt_h%d" % i]}) + "\n")

    def _llm(self, payload):
        async def call(prompt):
            return json.dumps(payload, ensure_ascii=False)
        return call

    def test_history_writes_revision_without_moving_cursor(self):
        before = pg.load().get("last_consolidated")
        res = asyncio.run(pg.reflect_history(self._llm({"self_view": "更沉稳了"}), days=14))
        self.assertTrue(res["ok"])
        self.assertTrue(res["changed"])
        self.assertEqual(pg.load().get("last_consolidated"), before)  # 游标不动
        import evidence_repair as er
        self.assertGreaterEqual(er.self_view_stats()["in_window"], 1)

    def test_history_insufficient_rows(self):
        os.remove(pg.journal_file())
        res = asyncio.run(pg.reflect_history(self._llm({"self_view": "x"}), days=14))
        self.assertFalse(res["ok"])

    def test_no_change_no_revision(self):
        res = asyncio.run(pg.reflect_history(self._llm({"facts": []}), days=14))
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])


if __name__ == "__main__":
    unittest.main()