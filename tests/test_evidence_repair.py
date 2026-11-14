# -*- coding: utf-8 -*-
"""证据介入回归锁：只挂真实来源，找不到不补造。"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import evidence_repair as er


class EvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="evrepair_")
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "persona"))
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name,
                                           "PERSONA_DATA_DIR": os.path.join(self.tmp.name, "persona")})
        env.start()
        self.addCleanup(env.stop)
        import self_model
        self_model._db().close()  # 用真实 schema 建库，避免最小表与迁移冲突
        self.db = os.path.join(self.tmp.name, "persona", "cognition.db")
        c = sqlite3.connect(self.db)
        self.now = time.time()
        c.execute("INSERT INTO action (id,kind,state,scope,event_ids,created,updated) "
                  "VALUES ('a1','dm','accepted','private:1','[]',?,?)", (self.now, self.now))
        c.commit()
        c.close()

    def _add_event(self, eid, kind, session, source, dt):
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("INSERT INTO event (id,kind,session,source,created,ts) VALUES (?,?,?,?,?,?)",
                      (eid, kind, session, source, self.now + dt, self.now + dt))
            c.commit()

    def test_unlinked_detected(self):
        un = er.unlinked_actions()
        self.assertEqual([a["id"] for a in un], ["a1"])

    def test_repair_attaches_incoming_message(self):
        self._add_event("e_in", "private_message", "private:1", "onebot", -5)
        res = er.repair_action("a1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["event_id"], "e_in")
        self.assertEqual(er.unlinked_actions(), [])

    def test_repair_proactive_uses_generation_request(self):
        self._add_event("e_gen", "generation_request", "", "llm_router", -1)
        res = er.repair_action("a1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["event_id"], "e_gen")

    def test_repair_no_source_not_fabricated(self):
        res = er.repair_action("a1")
        self.assertFalse(res["ok"])
        self.assertEqual(er.unlinked_actions()[0]["refs"], [])

    def test_self_view_stats(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("INSERT INTO self_view_rev (id,ts,text) VALUES ('s1',?,'x')", (self.now - 3600,))
            c.commit()
        st = er.self_view_stats()
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["in_window"], 1)


if __name__ == "__main__":
    unittest.main()