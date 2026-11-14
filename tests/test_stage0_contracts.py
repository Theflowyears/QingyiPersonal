"""阶段0契约执行器本身：白名单、隔离与可复现。"""
import json
import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import stage0_contracts


class Stage0RunnerTests(unittest.TestCase):
    def test_cases_reference_real_test_classes(self):
        for key, tests in stage0_contracts.CASES.items():
            for name in tests:
                loaded = unittest.defaultTestLoader.loadTestsFromName(name)
                self.assertGreater(loaded.countTestCases(), 0, "%s -> %s 为空" % (key, name))

    def test_runner_passes_every_case_in_isolation(self):
        report = stage0_contracts.run()
        self.assertTrue(report['ok'], json.dumps(report, ensure_ascii=False))
        self.assertTrue(report['isolated'])
        self.assertEqual(set(stage0_contracts.CASES), set(report['cases']))
        for key, case in report['cases'].items():
            self.assertEqual('pass', case['status'], key)

    def test_runner_never_writes_test_owned_protected_assets(self):
        from pathlib import Path
        protected = Path(os.path.dirname(BOT)) / 'data' / '_contract_probe_test'
        def snap():
            return sorted((p, p.stat().st_mtime) for p in protected.rglob('*') if p.is_file()) if protected.is_dir() else []
        before = snap()
        stage0_contracts.run()
        self.assertEqual(before, snap(), "契约执行器写到了测试专属保护资产")


if __name__ == '__main__':
    unittest.main()