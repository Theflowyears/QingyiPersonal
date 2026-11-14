"""回归锁：效价候选账本只观测，不推进影子状态。

用户问「效价为什么一直没数据」后确认的路线：严格规则不动，另开一层
把发给她的消息交给小模型判 praise/rebuke/neutral/uncertain，记录原文、
置信度、规则线索与漏判/误判，等人工看过再决定是否接入。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import valence_candidates as vc  # noqa: E402


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="valcand_")
        self.env = patch.dict(os.environ, {
            "DATA_DIR": self.dir, "PERSONA_DATA_DIR": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class LedgerTests(Base):
    def test_enqueue_dedupes_by_event_id(self):
        self.assertTrue(vc.enqueue("evt_1", at=100, session="private:1",
                                   kind="private_message", text="谢谢你",
                                   address_basis="private"))
        self.assertFalse(vc.enqueue("evt_1", at=100, session="private:1",
                                    kind="private_message", text="谢谢你",
                                    address_basis="private"))
        got = vc.recent()
        self.assertEqual(got["stats"]["total"], 1)
        self.assertEqual(got["rows"][0]["lexical"], "positive")

    def test_empty_text_or_id_is_ignored(self):
        self.assertFalse(vc.enqueue("", text="谢谢你"))
        self.assertFalse(vc.enqueue("evt_2", text="   "))

    async def test_drain_judges_and_keeps_rule_comparison(self):
        vc.enqueue("evt_praise", at=100, text="好棒", address_basis="private")
        vc.enqueue("evt_rebuke", at=101, text="你真笨", address_basis="at_self")

        async def judge(text, basis):
            if "棒" in text:
                return {"stance": "praise", "confidence": 0.91,
                        "reason": "明确夸她", "model": "fake"}
            return {"stance": "rebuke", "confidence": 0.84,
                    "reason": "明确责备", "model": "fake"}

        r = await vc.drain(limit=5, judge=judge)
        self.assertEqual(r["judged"], 2)
        got = vc.recent()
        self.assertEqual(got["stats"]["praise"], 1)
        self.assertEqual(got["stats"]["rebuke"], 1)
        self.assertEqual(got["stats"]["missed"], 1, "好棒不在词表里，应记为规则漏掉")
        self.assertEqual(got["stats"]["hit"], 1)

    async def test_bad_label_is_uncertain_not_crash(self):
        vc.enqueue("evt_x", at=100, text="嗯", address_basis="private")

        async def judge(text, basis):
            return {"stance": "great", "confidence": 5, "reason": "?"}

        r = await vc.drain(limit=1, judge=judge)
        self.assertEqual(r["judged"], 1)
        row = vc.recent()["rows"][0]
        self.assertEqual(row["stance"], "uncertain")
        self.assertEqual(row["confidence"], 1.0)

    async def test_failure_retries_then_marks_error(self):
        vc.enqueue("evt_bad", at=100, text="你好", address_basis="private")

        async def broken(text, basis):
            raise RuntimeError("model down")

        for _ in range(vc.MAX_ATTEMPTS):
            await vc.drain(limit=1, judge=broken)
        row = vc.recent()["rows"][0]
        self.assertEqual(row["state"], "error")
        self.assertEqual(row["attempts"], vc.MAX_ATTEMPTS)

    def test_backfill_only_takes_addressed_messages(self):
        os.makedirs(os.path.join(self.dir, "persona"), exist_ok=True)
        c = sqlite3.connect(os.path.join(self.dir, "persona", "cognition.db"))
        c.execute("CREATE TABLE event (id TEXT, kind TEXT, ts REAL, created REAL,"
                  " session TEXT, payload TEXT)")
        now = __import__("time").time()
        c.execute("INSERT INTO event VALUES (?,?,?,?,?,?)",
                  ("evt_a", "private_message", now - 30, now - 20, "private:1",
                   json.dumps({"text": "抱抱", "addressed_to_self": True})))
        c.execute("INSERT INTO event VALUES (?,?,?,?,?,?)",
                  ("evt_b", "group_message", now - 10, now - 9, "group:1",
                   json.dumps({"text": "喜欢你", "addressed_to_self": False})))
        c.execute("INSERT INTO event VALUES (?,?,?,?,?,?)",
                  ("evt_c", "tool_result", now - 5, now - 4, "group:1",
                   json.dumps({"ok": True})))
        c.commit()
        c.close()
        r = vc.backfill(days=7, limit=20)
        self.assertEqual(r["added"], 1)
        self.assertEqual(vc.recent()["stats"]["total"], 1)

    def test_backfill_limit_counts_addressed_not_scanned_rows(self):
        os.makedirs(os.path.join(self.dir, "persona"), exist_ok=True)
        c = sqlite3.connect(os.path.join(self.dir, "persona", "cognition.db"))
        c.execute("CREATE TABLE event (id TEXT, kind TEXT, ts REAL, created REAL,"
                  " session TEXT, payload TEXT)")
        now = __import__("time").time()
        for i in range(20):
            c.execute("INSERT INTO event VALUES (?,?,?,?,?,?)",
                      ("amb_%d" % i, "group_message", now - i, now - i, "group:1",
                       json.dumps({"text": "闲聊", "addressed_to_self": False})))
        for i in range(3):
            c.execute("INSERT INTO event VALUES (?,?,?,?,?,?)",
                      ("dir_%d" % i, "private_message", now - 30 - i, now - 30 - i,
                       "private:1",
                       json.dumps({"text": "抱抱", "addressed_to_self": True})))
        c.commit()
        c.close()
        r = vc.backfill(days=7, limit=2)
        self.assertEqual(r["added"], 2)
        self.assertEqual(vc.recent()["stats"]["total"], 2)


class WiringTests(unittest.TestCase):
    def test_bridge_notes_only_addressed_evidence(self):
        import qq_openclaw_bridge as b
        with patch("valence_candidates.enqueue") as enq:
            b._note_valence_candidate("evt_1", "group_message", "group:1",
                                      "喜欢你", {"addressed_to_self": False})
            enq.assert_not_called()
            b._note_valence_candidate("evt_2", "private_message", "private:1",
                                      "最喜欢你啦", {"addressed_to_self": True,
                                                    "address_basis": "private"})
            enq.assert_called_once()

    def test_panel_and_api_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "website" / "console.html").read_text(encoding="utf-8")
        js = (root / "website" / "static" / "console.js").read_text(encoding="utf-8")
        api = (root / "website" / "ceylan_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('id="valence-samples-list"', html)
        self.assertIn('id="valence-samples-brief"', html)
        self.assertIn("renderValenceSamples", js)
        self.assertIn("d.valence_samples", js)
        self.assertIn("emotion_shadow.status(include_valence=candidate_detail)", api)
        self.assertIn("valence_candidates.recent_readonly(20)", api)
        self.assertNotIn('id="valence-cand-list"', html)


if __name__ == "__main__":
    unittest.main()
