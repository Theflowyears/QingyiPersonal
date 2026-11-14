"""S01 三轨迹口径回归锁（统一方案 §9.1/§9.7）。

行为测试钉死：四类报告（v1参考/v2实时/候选反事实/历史追溯）都带统一的
trajectory/kernel_version/counts_for_live，且只有 v2 实时计在线；候选回放
不碰实时账本一个字节，迟到标签可重建；旧存储报告读取时重盖口径。
AST 锁钉死：口径由契约模块盖章、生产者不许自报在线标志、候选回放函数
只许 SELECT、实时消费者不许 import 候选账本、退役标志不许复活。
"""
import ast
import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, 'bot')
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import emotion_core as legacy
import emotion_dynamics as core
import emotion_replay as replay
import emotion_shadow as sh
import emotion_trajectories as tr
import qingyi_roadmap
import valence_candidates as candidates


def _read(name):
    with io.open(os.path.join(BOT, name), encoding='utf-8') as f:
        return f.read()


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('function %s not found' % name)


def _stamp_trajectories(func):
    found = []
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'stamp'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'trajectories'
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Attribute)
                and isinstance(node.args[1].value, ast.Name)
                and node.args[1].value.id == 'trajectories'):
            found.append(node.args[1].attr)
    return found


class ContractTests(unittest.TestCase):
    def test_only_v2_live_counts_as_online(self):
        self.assertEqual(set(tr.CONTRACTS),
                         {tr.V1_REFERENCE, tr.V2_LIVE,
                          tr.CANDIDATE_COUNTERFACTUAL, tr.HISTORICAL_REPLAY})
        live = [name for name, (_, counts) in tr.CONTRACTS.items() if counts]
        self.assertEqual(live, [tr.V2_LIVE])
        self.assertEqual(tr.CONTRACTS[tr.V1_REFERENCE][0], legacy.VERSION)
        self.assertEqual(tr.CONTRACTS[tr.V2_LIVE][0], core.VERSION)
        self.assertEqual(tr.CONTRACTS[tr.CANDIDATE_COUNTERFACTUAL][0], core.SEMANTIC_VERSION)

    def test_stamp_overrides_self_reported_live_flag(self):
        report = tr.stamp({'counts_for_live': True}, tr.CANDIDATE_COUNTERFACTUAL)
        self.assertFalse(report['counts_for_live'])
        self.assertEqual(report['trajectory'], tr.CANDIDATE_COUNTERFACTUAL)
        self.assertEqual(report['kernel_version'], core.SEMANTIC_VERSION)

    def test_catalog_marks_s01_implemented_with_satisfied_dependencies(self):
        task = qingyi_roadmap.project_task('S01')
        self.assertEqual(task['implementation'], 'implemented')
        self.assertEqual(task['blocked_by'], [])
        self.assertTrue(task['dev_ready'])


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shadow_tree = ast.parse(_read('emotion_shadow.py'))
        cls.replay_tree = ast.parse(_read('emotion_replay.py'))
        cls.contract_source = _read('emotion_trajectories.py')
        cls.contract_tree = ast.parse(cls.contract_source)

    def test_contract_module_is_pure(self):
        for node in self.contract_tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, ('emotion_core', 'emotion_dynamics'),
                                  '轨迹契约不许碰存储/时钟/网络')
            elif isinstance(node, ast.ImportFrom):
                self.fail('轨迹契约不许 from-import: %s' % node.module)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self.assertTrue(isinstance(target, ast.Name) and target.id.isupper(),
                                    '契约模块只许常量')
            else:
                self.assertIsInstance(node, (ast.FunctionDef, ast.Expr),
                                      '契约模块不许有副作用语句')
        for banned in ('sqlite3', 'os.', 'time.', 'open('):
            self.assertNotIn(banned, self.contract_source)

    def test_producers_stamp_the_contract(self):
        expected = {
            ('emotion_replay.py', 'reference_report'): {'V1_REFERENCE'},
            ('emotion_shadow.py', 'status'): {'V2_LIVE', 'HISTORICAL_REPLAY'},
            ('emotion_shadow.py', 'valence_samples'): {'CANDIDATE_COUNTERFACTUAL'},
            ('emotion_shadow.py', '_backfill_day'): {'HISTORICAL_REPLAY'},
        }
        trees = {'emotion_replay.py': self.replay_tree, 'emotion_shadow.py': self.shadow_tree}
        for (filename, funcname), want in expected.items():
            found = _stamp_trajectories(_function(trees[filename], funcname))
            self.assertTrue(found, '%s::%s 不再盖轨迹口径了' % (filename, funcname))
            self.assertEqual(set(found), want, '%s::%s 口径盖错' % (filename, funcname))

    def test_producers_do_not_self_report_the_live_flag(self):
        for tree, names in ((self.replay_tree, ('reference_report',)),
                            (self.shadow_tree, ('status', 'valence_samples', '_backfill_day'))):
            for name in names:
                for node in ast.walk(_function(tree, name)):
                    if isinstance(node, ast.Dict):
                        for key in node.keys:
                            self.assertFalse(isinstance(key, ast.Constant)
                                             and key.value == 'counts_for_live',
                                             '%s 自报在线标志了；必须由契约盖章' % name)

    def test_candidate_replay_only_reads_sql(self):
        func = _function(self.shadow_tree, 'valence_samples')
        calls = [node for node in ast.walk(func)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in ('execute', 'executescript')]
        self.assertTrue(calls, '候选回放不再读源账本了')
        for call in calls:
            self.assertTrue(call.args and isinstance(call.args[0], ast.Constant)
                            and str(call.args[0].value).lstrip().upper().startswith('SELECT'),
                            '候选反事实轨迹不许写任何账本')

    def test_live_consumer_never_imports_candidate_ledger(self):
        for node in self.shadow_tree.body:
            if isinstance(node, ast.Import):
                self.assertNotIn('valence_candidates', [a.name for a in node.names])
        tick = _function(self.shadow_tree, 'tick')
        for node in ast.walk(tick):
            if isinstance(node, ast.Import):
                self.assertNotIn('valence_candidates', [a.name for a in node.names],
                                 '实时轨迹不许消费候选标注')

    def test_retired_live_flag_stays_retired(self):
        for name in sorted(os.listdir(BOT)):
            # 契约模块的 docstring 记录退役这件事本身，不算复活。
            if name.endswith('.py') and name != 'emotion_trajectories.py':
                self.assertNotIn('counts_as_live_observation', _read(name),
                                 '%s 复活了退役口径名' % name)


class TrajectoryReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='s01_trajectories_')
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        environment = patch.dict(os.environ, {'PERSONA_DATA_DIR': self.tmp,
                                              'EMOTION_SHADOW_ENABLED': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = time.time()
        self.source = Path(self.tmp) / 'cognition.db'
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,session TEXT,'
                      'actor_qq TEXT,topic_qq TEXT,source TEXT,visibility TEXT,payload TEXT,'
                      'dedupe TEXT,created REAL)')

    def add(self, eid, at, text='谢谢你', session='private:one'):
        payload = {'text': text, 'addressed_to_self': True, 'address_basis': 'private'}
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (eid, 'private_message', at, session, '', '', 'onebot', 'private',
                       json.dumps(payload), eid, at))

    def label(self, eid, at, text='谢谢你', stance='praise'):
        candidates.enqueue(eid, at=at, session='private:one', kind='private_message',
                           text=text, address_basis='private')
        asyncio.run(candidates.drain(limit=10, judge=lambda *_: dict(
            stance=stance, confidence=0.95, reason='明确肯定', model='test-model')))

    def test_live_status_carries_v2_contract(self):
        self.add('live', self.now - 1)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        report = sh.status(now=self.now + 2, include_valence=False)
        self.assertEqual(report['trajectory'], tr.V2_LIVE)
        self.assertEqual(report['kernel_version'], core.VERSION)
        self.assertTrue(report['counts_for_live'])
        self.assertEqual(report['mode'], 'shadow')
        self.assertEqual(report['parameter_hash'], core.parameter_hash())

    def test_candidate_replay_contract_and_live_ledger_untouched(self):
        self.add('msg', self.now - 1)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        self.label('msg', self.now - 1)
        ledger = sh.shadow_path()
        before = ledger.read_bytes()
        run_before = sh.status(now=self.now + 2, include_valence=False)
        report = sh.valence_samples(now=self.now + 2)
        self.assertEqual(report['trajectory'], tr.CANDIDATE_COUNTERFACTUAL)
        self.assertEqual(report['kernel_version'], core.SEMANTIC_VERSION)
        self.assertFalse(report['counts_for_live'])
        self.assertFalse(report['expression_control'])
        self.assertEqual(report['stats']['included'], 1)
        self.assertEqual(ledger.read_bytes(), before)
        run_after = sh.status(now=self.now + 2, include_valence=False)
        for key in ('processed', 'cursor', 'observed_seconds', 'state',
                    'diagnostics', 'calibration_detail'):
            self.assertEqual(run_after[key], run_before[key], key)
        with_valence = sh.status(now=self.now + 2, include_valence=True)
        self.assertEqual(with_valence['diagnostics'], run_before['diagnostics'])
        self.assertEqual(with_valence['calibration_detail'], run_before['calibration_detail'])

    def test_late_label_rebuilds_candidate_without_touching_live(self):
        # 词表本身不带效价的文本：标注前候选回放无纳入，迟到标签才可重建。
        text = '这次处理得很靠谱'
        self.add('msg', self.now - 1, text=text)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        ledger = sh.shadow_path()
        before = ledger.read_bytes()
        self.assertEqual(sh.valence_samples(now=self.now + 2)['stats']['included'], 0)
        self.label('msg', self.now - 1, text=text)
        report = sh.valence_samples(now=self.now + 3)
        self.assertEqual(report['stats']['included'], 1)
        self.assertFalse(report['counts_for_live'])
        self.assertEqual(ledger.read_bytes(), before)

    def test_retrospective_is_historical_and_never_live(self):
        tm = time.localtime(self.now)
        midnight = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))
        self.add('today', midnight + 1)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        history = sh.status(now=self.now + 2)['retrospective']
        self.assertEqual(history['trajectory'], tr.HISTORICAL_REPLAY)
        self.assertEqual(history['kernel_version'], core.VERSION)
        self.assertFalse(history['counts_for_live'])
        self.assertEqual(history['mode'], 'retrospective')

    def test_legacy_stored_retrospective_is_restamped_on_read(self):
        self.add('today', self.now - 1)
        sh.tick(now=self.now)
        sh.tick(now=self.now + 1)
        old_format = dict(mode='retrospective', counts_for_live=False, events=1,
                          effective_events=1, excluded_events=0,
                          rule_version=core.VERSION,
                          vocab_version=getattr(core, 'VOCAB_VERSION', ''))
        with closing(sqlite3.connect(sh.shadow_path())) as c, c:
            c.execute('INSERT OR REPLACE INTO shadow_retrospective VALUES(1,?)',
                      (json.dumps(old_format, ensure_ascii=False),))
        history = sh.status(now=self.now + 2)['retrospective']
        self.assertEqual(history['trajectory'], tr.HISTORICAL_REPLAY)
        self.assertEqual(history['kernel_version'], core.VERSION)
        self.assertFalse(history['counts_for_live'])
        self.assertEqual(history['events'], 1)

    def test_v1_frozen_ledger_is_kept_as_reference_when_v2_starts(self):
        legacy_path = Path(self.tmp) / 'emotion_shadow.db'
        with closing(sqlite3.connect(legacy_path)) as c, c:
            c.executescript(sh._SCHEMA)
            at = self.now - 3600
            c.execute('INSERT INTO shadow_run(id,parameter_hash,parameters,observation_started,'
                      'last_tick,cursor,state) VALUES(1,?,?,?,?,?,?)',
                      (legacy.parameter_hash(), json.dumps(legacy.PARAMETERS), at, at, 0,
                       json.dumps(legacy.initial_state(at))))
        before = legacy_path.read_bytes()
        self.add('live', self.now - 1)
        sh.tick(now=self.now)
        self.assertEqual(sh.shadow_path().name, 'emotion_shadow_v2.db')
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_v1_reference_report_carries_frozen_contract(self):
        report = replay.reference_report({'histories': {'private-user-1': {'turns': [
            {'role': 'user', 'content': '谢谢你', 'ts': 100}]}}})
        self.assertEqual(report['trajectory'], tr.V1_REFERENCE)
        self.assertEqual(report['kernel_version'], legacy.VERSION)
        self.assertFalse(report['counts_for_live'])
        self.assertEqual(report['mode'], 'synthetic_reference')
        self.assertEqual(report['parameter_hash'], legacy.parameter_hash())
        self.assertNotEqual(report['parameter_hash'], core.parameter_hash())


if __name__ == '__main__':
    unittest.main()