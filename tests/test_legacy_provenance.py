"""旧信念补出处：只登记档案来源，不伪造经历或支持关系。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import self_model as sm
import persona_store


ROWS = [
    {"id": 11, "kind": "likes", "text": "主人喜欢喝冰可乐。", "ts": 900.0},
    {"id": 12, "kind": "facts", "text": "主人住在杭州。", "ts": 950.0},
    {"id": 13, "kind": "dislikes", "text": "她讨厌被当小孩哄。", "ts": 960.0},
]


class LegacyProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="legacy_prov_")
        self._old = os.environ.get("PERSONA_DATA_DIR"), os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp
        os.environ.pop("PERSONA_DATA_DIR", None)

    def tearDown(self):
        for key, value in zip(("PERSONA_DATA_DIR", "DATA_DIR"), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if persona_store._conn is not None:
            persona_store._conn.close()
            persona_store._conn = None
            persona_store._conn_path = ""
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def db(self):
        return sqlite3.connect(str(Path(self.tmp) / "persona" / "cognition.db"))

    def evidence(self, belief_id):
        with self.db() as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM belief_evidence WHERE belief_id=?", (belief_id,))]

    def test_dry_run_reports_matches_without_writing(self):
        belief = sm.add_belief("主人喜欢喝冰可乐", confidence=None,
                               source="legacy", is_legacy=True, ts=1000.0)
        result = sm.backfill_legacy_provenance(ROWS, apply=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["linked"])
        self.assertEqual([], self.evidence(belief))
        with self.db() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM event WHERE source='legacy_archive'").fetchone()[0])

    def test_origin_links_to_the_real_archive_row_without_claiming_support(self):
        belief = sm.add_belief("主人喜欢喝冰可乐", confidence=None,
                               source="legacy", is_legacy=True, ts=1000.0)
        result = sm.backfill_legacy_provenance(ROWS, apply=True)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["linked"])
        evidence = self.evidence(belief)
        self.assertEqual(1, len(evidence))
        self.assertEqual("origin", evidence[0]["relation"])
        with self.db() as connection:
            connection.row_factory = sqlite3.Row
            event = dict(connection.execute(
                "SELECT * FROM event WHERE id=?", (evidence[0]["event_id"],)).fetchone())
            belief_row = dict(connection.execute(
                "SELECT * FROM belief WHERE id=?", (belief,)).fetchone())
        self.assertEqual("legacy_note", event["kind"])
        self.assertEqual("legacy_archive", event["source"])
        self.assertEqual(900.0, event["ts"])
        payload = json.loads(event["payload"])
        self.assertEqual("persona.db#memory:11", payload["archive"])
        self.assertEqual("likes", payload["kind"])
        self.assertEqual("legacy", belief_row["source"], "不应改写原始迁移标记")
        self.assertEqual(1, belief_row["is_legacy"])
        self.assertIsNone(belief_row["confidence"])

    def test_unmatched_beliefs_stay_evidence_free_and_are_reported(self):
        unmatched = sm.add_belief("我对[1000000001]的好感度是 80", confidence=None,
                                  source="legacy", is_legacy=True, ts=1000.0)
        result = sm.backfill_legacy_provenance(ROWS, apply=True)
        self.assertEqual(0, result["linked"])
        self.assertIn(unmatched, result["unmatched"])
        self.assertEqual([], self.evidence(unmatched))

    def test_backfill_is_idempotent(self):
        belief = sm.add_belief("主人住在杭州。", confidence=None,
                               source="legacy", is_legacy=True, ts=1000.0)
        first = sm.backfill_legacy_provenance(ROWS, apply=True)
        second = sm.backfill_legacy_provenance(ROWS, apply=True)
        self.assertEqual(1, first["linked"])
        self.assertEqual(0, second["linked"])
        self.assertEqual(1, len(self.evidence(belief)))
        with self.db() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM event WHERE source='legacy_archive'").fetchone()[0])
            meta = connection.execute(
                "SELECT value FROM meta WHERE key='legacy_provenance_v1'").fetchone()
        self.assertIsNotNone(meta)
        marker = json.loads(meta[0])
        self.assertEqual(0, marker["linked_this_run"])
        self.assertEqual(1, marker["total_origin"], "重复执行不能把累计出处写丢")

    def test_matching_normalizes_whitespace_and_prefers_exact(self):
        belief = sm.add_belief("她讨厌被当小孩哄", confidence=None,
                               source="legacy", is_legacy=True, ts=1000.0)
        rows = ROWS + [{"id": 14, "kind": "facts",
                        "text": "她讨厌被当小孩哄。", "ts": 1200.0}]
        sm.backfill_legacy_provenance(rows, apply=True)
        evidence = self.evidence(belief)
        self.assertEqual(1, len(evidence))
        with self.db() as connection:
            connection.row_factory = sqlite3.Row
            payload = json.loads(connection.execute(
                "SELECT payload FROM event WHERE id=?",
                (evidence[0]["event_id"],)).fetchone()[0])
        self.assertEqual("persona.db#memory:13", payload["archive"],
                         "同文本多行时应按最早时间稳定选择")

    def test_audit_reports_how_many_legacy_beliefs_now_have_origin(self):
        sm.add_belief("主人喜欢喝冰可乐", confidence=None, source="legacy",
                      is_legacy=True, ts=1000.0)
        sm.add_belief("查无出处的旧说法", confidence=None, source="legacy",
                      is_legacy=True, ts=1000.0)
        import phase_audit
        report = phase_audit.audit(str(Path(self.tmp) / "persona"))
        checks = report["checks"]["cognition"]
        self.assertEqual(0, checks["legacy_beliefs_with_origin"])
        self.assertEqual(2, checks["legacy_beliefs_without_evidence"])
        sm.backfill_legacy_provenance(ROWS, apply=True)
        report = phase_audit.audit(str(Path(self.tmp) / "persona"))
        checks = report["checks"]["cognition"]
        self.assertEqual(1, checks["legacy_beliefs_with_origin"])
        self.assertEqual(1, checks["legacy_beliefs_without_evidence"])

    def test_non_legacy_beliefs_are_never_touched(self):
        modern = sm.add_belief("主人喜欢喝冰可乐", confidence=0.8,
                               source="consolidation", is_legacy=False, ts=1100.0)
        result = sm.backfill_legacy_provenance(ROWS, apply=True)
        self.assertEqual(0, result["linked"])
        self.assertEqual([], self.evidence(modern))

    def test_persona_store_exports_ids_for_audit_links(self):
        if persona_store._conn is not None:
            persona_store._conn.close()
        persona_store._conn = None
        persona_store._conn_path = ""
        persona_store.add("likes", "主人喜欢喝冰可乐。", ts=900.0)
        rows = persona_store.all_rows(with_ids=True)
        self.assertTrue(rows)
        self.assertIn("id", rows[0])
        self.assertIn("text", rows[0])
        without = persona_store.all_rows()
        self.assertNotIn("id", without[0], "默认导出保持原形状")


if __name__ == "__main__":
    unittest.main()