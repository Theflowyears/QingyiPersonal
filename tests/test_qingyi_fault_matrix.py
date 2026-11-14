"""D07 检测器全模式验收：故障/性能/真实资产保护的端到端分型矩阵。

统一方案 §25.1 验收口径：缺数据/坏库/SQL超时/旧binding/跨库变化/报告过时
必须正确分型——暂时锁不是损坏，缺数据不是零，旧证据不是绿灯，
失控 worker 不能拖死面板。全部用临时根与真实子进程，不碰生产账本。
"""
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / 'website', ROOT / 'bot'):
    if str(directory) in sys.path:
        sys.path.remove(str(directory))
    sys.path.insert(0, str(directory))

import engineering_contracts
import phase_audit
import qingyi_preflight
import qingyi_preflight_jobs as jobs

COGNITION_TABLES = ('person', 'event', 'episode', 'belief', 'belief_evidence',
                    'belief_revision', 'commitment', 'goal', 'action', 'self_view_rev',
                    'evidence_retraction', 'cognition_invalidation', 'layer_revision')
SHADOW_TABLES = ('shadow_run', 'shadow_event', 'shadow_gap')


def _make_ledger(path, tables):
    connection = sqlite3.connect(str(path))
    try:
        for table in tables:
            connection.execute('CREATE TABLE IF NOT EXISTS %s(id TEXT)' % table)
        connection.commit()
    finally:
        connection.close()


def _signed_artifact(binding):
    cases = {}
    for key, tests in engineering_contracts.CASES.items():
        cases[key] = {'status': 'pass', 'tests': tests,
                      'execution': {'run': len(tests), 'skipped': 0, 'failures': 0,
                                    'errors': 0, 'expected_failures': 0,
                                    'unexpected_successes': 0},
                      'output_sha256': '0' * 64}
    data = {'origin': 'isolated_mock_transport', 'ts': time.time(),
            'binding': binding, 'cases': cases}
    data['sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data


class FaultClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qingyi_fault_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'persona'
        self.root.mkdir()

    def _schema_details(self, report):
        check = {row['id']: row for row in report['checks']}['file_schema']
        return check, check['counts']

    def test_missing_data_stays_missing_and_creates_nothing(self):
        report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        check, details = self._schema_details(report)
        self.assertEqual('missing_data', details['cognition']['status'])
        self.assertEqual('missing_data', details['shadow']['status'])
        self.assertNotEqual('pass', check['status'])
        self.assertEqual([], list(self.root.iterdir()), '缺库检测不许建库')

    def test_corrupt_ledger_is_unknown_not_zero(self):
        (self.root / 'cognition.db').write_bytes(b'this is not a sqlite database')
        report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        check, details = self._schema_details(report)
        self.assertEqual('unknown', details['cognition']['status'])
        self.assertNotEqual('pass', check['status'])
        self.assertNotIn('pass', report['scope_status'])

    def test_sql_lock_is_unknown_not_corruption(self):
        _make_ledger(self.root / 'cognition.db', COGNITION_TABLES)
        _make_ledger(self.root / 'emotion_shadow.db', SHADOW_TABLES)
        holder = sqlite3.connect(str(self.root / 'cognition.db'))
        self.addCleanup(holder.close)
        holder.execute('BEGIN EXCLUSIVE')
        # 锁住期间审计 reader 也会撞锁；本用例只关心 schema 分型，审计打桩加速。
        with patch.object(qingyi_preflight.time, 'sleep'), \
             patch.object(phase_audit, 'audit_readiness',
                          return_value={'phase25': {'checks': [], 'blockers': []}}), \
             patch.object(phase_audit, 'audit', return_value={'phases': {}, 'counts': {}}):
            report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        check, details = self._schema_details(report)
        self.assertEqual('unknown', details['cognition']['status'])
        self.assertEqual('pass', details['shadow']['status'])
        self.assertNotEqual('pass', check['status'])
        holder.rollback()
        after = qingyi_preflight.run_preflight(self.root, profile='evidence', now=124.0)
        _, details_after = self._schema_details(after)
        self.assertEqual('pass', details_after['cognition']['status'],
                         '锁释放后必须能读回，暂时锁不是永久损坏')

    def test_transient_lock_gets_exactly_one_limited_reread(self):
        _make_ledger(self.root / 'cognition.db', COGNITION_TABLES)
        _make_ledger(self.root / 'emotion_shadow.db', SHADOW_TABLES)
        real = qingyi_preflight._readonly
        calls = []

        def flaky(path):
            calls.append(str(path))
            if len(calls) == 1:
                raise sqlite3.OperationalError('database is locked')
            return real(path)

        with patch.object(qingyi_preflight, '_readonly', side_effect=flaky), \
             patch.object(qingyi_preflight.time, 'sleep') as nap:
            report = qingyi_preflight.run_preflight(self.root, profile='evidence', now=123.0)
        check, details = self._schema_details(report)
        self.assertEqual('pass', check['status'])
        self.assertTrue(details['cognition'].get('reread_after_lock'),
                        '重读必须记录边界，不能悄悄发生')
        self.assertEqual(2, len([c for c in calls if c.endswith('cognition.db')]),
                         '锁冲突只允许有限重读一次')
        nap.assert_called()

    def test_stale_engineering_binding_never_stays_green(self):
        current = engineering_contracts.binding()
        stale = dict(current)
        stale['files'] = dict(current['files'])
        stale['files']['bot/self_model.py'] = '0' * 64
        artifact = self.root / 'engineering_contracts.json'
        artifact.write_text(json.dumps(_signed_artifact(stale)), encoding='utf-8')
        cases, error = engineering_contracts.read_evidence(self.root)
        self.assertEqual({}, cases)
        self.assertIn('stale', error)
        readiness = phase_audit.audit_readiness(str(self.root), now=time.time(),
                                                include_candidate_replay=False)
        checks = {c['key']: c for c in readiness['phase25']['checks']}
        for key in engineering_contracts.CASES:
            self.assertEqual('not_verified', checks[key]['status'], key)
        artifact.write_text(json.dumps(_signed_artifact(current)), encoding='utf-8')
        cases, error = engineering_contracts.read_evidence(self.root)
        self.assertEqual(set(engineering_contracts.CASES), set(cases), error)
        readiness = phase_audit.audit_readiness(str(self.root), now=time.time(),
                                                include_candidate_replay=False)
        checks = {c['key']: c for c in readiness['phase25']['checks']}
        for key in engineering_contracts.CASES:
            self.assertEqual('pass', checks[key]['status'], key)


class WorkerFaultTests(unittest.TestCase):
    """真实子进程：崩溃/僵尸/失控输出都必须落真实失败，不能冒充检测结论。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qingyi_worker_')
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict('os.environ', {'DATA_DIR': self.temp.name, 'PERSONA_DATA_DIR': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run_worker(self, code, deadline=None):
        import subprocess as real_subprocess
        real_popen = real_subprocess.Popen
        captured = {}

        def fake_popen(args, **kwargs):
            process = real_popen([sys.executable, '-c', code], **kwargs)
            captured['process'] = process
            return process

        if deadline:
            limits = patch.dict(jobs.DEADLINE_SECONDS, {'evidence': deadline})
            limits.start()
            self.addCleanup(limits.stop)
        job = jobs.claim(profile='evidence', scope='qingyi_all',
                         request_key='fault-%s' % time.time(), now=time.time())
        with patch.object(jobs.subprocess, 'Popen', side_effect=fake_popen):
            jobs._supervise(job)
        return jobs.get(job['run_id']), captured['process']

    def test_crashed_worker_without_report_is_error(self):
        row, process = self._run_worker('import sys; sys.exit(3)')
        self.assertEqual('error', row['state'])
        self.assertEqual('invalid_preflight_report', row['error'])
        self.assertIsNone(jobs.read_report(row['run_id']))

    def test_zombie_worker_is_killed_at_deadline(self):
        row, process = self._run_worker('import time; time.sleep(60)', deadline=2)
        self.assertEqual('timed_out', row['state'])
        self.assertEqual('preflight_deadline', row['error'])
        self.assertIsNotNone(process.poll(), '僵尸子进程必须被杀掉，不能留它过年')

    def test_oversized_stdout_is_rejected_not_buffered_forever(self):
        row, process = self._run_worker('print("x" * %d)' % (jobs.MAX_REPORT_CHARS + 1000))
        self.assertEqual('error', row['state'])
        self.assertEqual('report_too_large', row['error'])
        self.assertIsNotNone(process.poll())
        self.assertIsNone(jobs.read_report(row['run_id']))

    def test_oversized_stderr_is_rejected(self):
        row, process = self._run_worker(
            'import sys; sys.stderr.write("y" * %d)' % (jobs.MAX_STDERR_CHARS + 1000))
        self.assertEqual('error', row['state'])
        self.assertEqual('stderr_too_large', row['error'])
        self.assertIsNotNone(process.poll())


class StaticLockTests(unittest.TestCase):
    """AST 静态锁：保住矩阵依赖的实现结构，防止被顺手改回无界读取。"""

    def test_supervisor_drains_bounded_pipes_not_communicate(self):
        tree = ast.parse((ROOT / 'website' / 'qingyi_preflight_jobs.py').read_text(encoding='utf-8'))
        supervise = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == '_supervise')
        dumped = ast.dump(supervise)
        self.assertIn('MAX_REPORT_CHARS', dumped)
        self.assertIn('MAX_STDERR_CHARS', dumped)
        self.assertNotIn('communicate', dumped, '无界 communicate 会把失控输出全吞进内存')
        self.assertIn('report_too_large', dumped)

    def test_schema_check_uses_limited_reread(self):
        tree = ast.parse((ROOT / 'bot' / 'qingyi_preflight.py').read_text(encoding='utf-8'))
        schema = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_schema_check')
        self.assertIn('_read_tables_with_reread', ast.dump(schema))
        reread = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_read_tables_with_reread')
        self.assertIn('locked', ast.dump(reread))

    def test_report_identity_is_shared_by_preflight_and_overview(self):
        preflight = ast.parse((ROOT / 'bot' / 'qingyi_preflight.py').read_text(encoding='utf-8'))
        run = next(n for n in ast.walk(preflight)
                   if isinstance(n, ast.FunctionDef) and n.name == 'run_preflight')
        self.assertIn('report_identity', ast.dump(run))
        dashboard = (ROOT / 'website' / 'ceylan_dashboard.py').read_text(encoding='utf-8')
        tree = ast.parse(dashboard)
        overview = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == 'api_qingyi_overview')
        dumped = ast.dump(overview)
        self.assertIn('report_identity', dumped)
        self.assertIn('binding_digest', dumped)
        self.assertIn('data_identity_digest', dumped)


if __name__ == '__main__':
    unittest.main()