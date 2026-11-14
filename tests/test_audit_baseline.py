"""阶段0审计基线：绑定代码/解释器/数据身份，且只读。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import audit_baseline
import self_model


class AuditBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="baseline_")
        self._old = os.environ.get("PERSONA_DATA_DIR"), os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp
        os.environ.pop("PERSONA_DATA_DIR", None)

    def tearDown(self):
        for key, value in zip(("PERSONA_DATA_DIR", "DATA_DIR"), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_binds_code_and_data_without_writing_anything(self):
        self_model.record_event("observation", dedupe="baseline:1")
        root = Path(self.tmp) / "persona"
        before = sorted(p.name for p in root.iterdir())
        report = audit_baseline.snapshot(now=1234.5)
        after = sorted(p.name for p in root.iterdir())
        self.assertEqual(before, after, "快照不应写数据目录")
        self.assertEqual(1, report["schema_version"])
        self.assertEqual(1234.5, report["created_at"])
        self.assertEqual(64, len(report["code"]["binding_digest"]))
        self.assertTrue(report["data"]["cognition"]["present"])
        self.assertEqual(64, len(report["data"]["cognition"]["sha256"]))
        self.assertEqual(16, len(report["data"]["root_digest"]))

    def test_written_baseline_is_valid_json_and_reproducible(self):
        report = audit_baseline.snapshot(now=42.0)
        target = Path(self.tmp) / "audit_baseline.json"
        audit_baseline.write(report, target)
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(report["code"]["binding_digest"], loaded["code"]["binding_digest"])
        again = audit_baseline.snapshot(now=42.0)
        self.assertEqual(report["data"], again["data"])


if __name__ == "__main__":
    unittest.main()