"""清漪自动前置检测的安全合同。"""
import gc
import json
import os
from contextlib import closing
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# website 里有一份同名 project_config（只做面板展示）；先插 website、再把 bot
# 顶到 0，否则 bot 运行时会 import 到展示用的那一份。
for directory in (ROOT / 'website', ROOT / 'bot'):
    if str(directory) in sys.path:
        sys.path.remove(str(directory))
    sys.path.insert(0, str(directory))

import emotion_shadow
import qingyi_preflight


class PreflightTests(unittest.TestCase):
    def test_passing_engineering_with_unobserved_organic_is_not_blocked(self):
        import qingyi_preflight as pf
        self.assertEqual('not_observed',pf.aggregate_scope(['pass','not_observed','not_verified']))
        self.assertEqual('blocked',pf.aggregate_scope(['pass','blocked','not_observed']))

    def test_wired_runtime_checks_are_not_hardcoded_as_unimplemented(self):
        import qingyi_preflight as pf
        report=pf.run_preflight(self.root,profile='evidence',now=123)
        checks={x['id']:x for x in report['checks']}
        self.assertNotEqual('not_implemented',checks['memory_composition']['status'])
        self.assertNotEqual('not_implemented',checks['ripple']['status'])

    def test_one_failed_case_does_not_erase_other_independent_contract_evidence(self):
        import qingyi_preflight as pf
        got=pf._projection({'stage0_contracts':{'status':'blocked','counts':{'cases':{
            'conversation_state':{'status':'pass'},'worldbook_store':{'status':'fail'}}}}},'isolated',1000)
        self.assertEqual('pass',got['tasks']['Q01']['engineering'])
        self.assertEqual('blocked',got['tasks']['Q04']['engineering'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qingyi_preflight_')
        self.addCleanup(gc.collect)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'persona'
        self.root.mkdir()

    def _files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*') if p.is_file())

    def test_missing_data_is_reported_without_creating_any_ledger(self):
        before = self._files()
        report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        self.assertEqual(before, [])
        self.assertEqual(self._files(), before)
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['profile'], 'evidence')
        self.assertEqual(report['source_kind'], 'local_snapshot')
        self.assertIn(report['scope_status'], {'pass', 'blocked', 'not_observed', 'unknown'})
        self.assertFalse(report['authorization']['stage3'])
        self.assertFalse(report['authorization']['expression_control'])
        json.dumps(report, allow_nan=False)

    def test_evidence_profile_does_not_trigger_candidate_valence_replay(self):
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(True)
            return {'ok': True, 'rows': [], 'stats': {}}
        with patch.object(emotion_shadow, 'valence_samples', side_effect=forbidden):
            report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        self.assertFalse(calls)
        self.assertTrue(any(check['id'] == 'phase25' for check in report['checks']))

    def test_usage_probe_uses_only_temporary_data_and_detects_the_old_recount_bug(self):
        result = qingyi_preflight.usage_idempotency_probe()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['used_after_three_aggregations'], 1)
        self.assertEqual(result['daily_selected_after_three_aggregations'], 1)

    def test_stage0_contracts_are_isolated_only_and_project_onto_tasks(self):
        evidence = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        checks = {row['id']: row for row in evidence['checks']}
        self.assertEqual('not_verified', checks['stage0_contracts']['status'])
        tasks = {row['id']: row for row in evidence['tasks']}
        self.assertEqual('not_verified', tasks['A05']['engineering'])
        isolated = qingyi_preflight.run_preflight(self.root, profile='isolated', now=123.0)
        checks = {row['id']: row for row in isolated['checks']}
        self.assertEqual('pass', checks['stage0_contracts']['status'])
        tasks = {row['id']: row for row in isolated['tasks']}
        for task_id in ('A05', 'A06', 'A07', 'A08'):
            self.assertEqual('pass', tasks[task_id]['engineering'], task_id)
            self.assertEqual('implemented', tasks[task_id]['implementation'], task_id)

    def test_report_never_exports_chat_payload_or_qq(self):
        with closing(sqlite3.connect(str(self.root / 'cognition.db'))) as c:
            c.executescript('''CREATE TABLE event(id TEXT PRIMARY KEY,payload TEXT);
                              INSERT INTO event VALUES('evt-secret','{"qq":"123456789","text":"绝密私聊"}');''')
        report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('绝密私聊', text)
        self.assertNotIn('123456789', text)

    def test_unreadable_memory_ledger_is_unknown_not_missing(self):
        (self.root / 'persona.db').write_bytes(b'not a sqlite database at all')
        report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        check = {row['id']: row for row in report['checks']}['memory_provenance']
        self.assertEqual('unknown', check['status'])
        self.assertNotIn('未发现可检查的记忆账本', check['message'])

    def test_phase25_all_pending_blockers_stay_not_observed(self):
        audit = {'phase25': {'checks': [
            {'key': 'action_evidence', 'status': 'not_observed'},
            {'key': 'belief_grounding', 'status': 'not_observed'}],
            'blockers': ['action_evidence', 'belief_grounding']}}
        with patch('phase_audit.audit_readiness', return_value=audit):
            check = qingyi_preflight._phase25_check(self.root, 123.0)
        self.assertEqual('not_observed', check['status'])

    def test_phase25_mixed_blockers_are_blocked(self):
        audit = {'phase25': {'checks': [
            {'key': 'action_evidence', 'status': 'not_observed'},
            {'key': 'evidence_retraction', 'status': 'fail'}],
            'blockers': ['action_evidence', 'evidence_retraction']}}
        with patch('phase_audit.audit_readiness', return_value=audit):
            check = qingyi_preflight._phase25_check(self.root, 123.0)
        self.assertEqual('blocked', check['status'])


if __name__ == '__main__':
    unittest.main()
