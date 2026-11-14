"""S04「采集、标注、回放、表达控制分离」回归锁。

四个关注点各钉一道 AST 静态锁 + 一道行为锁：
- 采集 collection（emotion_shadow.tick）：唯一可写连接落在实时影子账本，
  只写 shadow_* 表，实时路径绝不把候选标注当实时证据消费；
- 标注 annotation（valence_candidates）：唯一可写连接落在 valence_candidates.db，
  只写 candidate 表，绝不写实时/人格库；
- 回放 replay（emotion_replay）：没有任何数据库访问，唯一落盘写是独占创建新 JSON；
- 表达控制 expression control：全仓 expression_control 恒 False，任何路径不得开启；
  候选轨迹 counts_for_live=False 由 S01 契约盖章，不由生产者自报。

审计结论（S04 实施时）：四条边界均无真实泄漏。本任务只加了两处结构性守卫
（emotion_shadow._readonly 的 PRAGMA query_only、valence_candidates._conn 的
账本名钉死），把「只读/只写自己账本」从口头承诺变成运行时事实，不改 tick 路径。
"""
import ast
import asyncio
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import emotion_replay as replay  # noqa: E402
import emotion_shadow as sh  # noqa: E402
import emotion_trajectories as tr  # noqa: E402
import valence_candidates as vc  # noqa: E402


def _read(name):
    with io.open(BOT / name, encoding='utf-8') as f:
        return f.read()


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError('function %s not found' % name)


def _strings(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _connect_calls(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'connect' and isinstance(n.func.value, ast.Name)
            and n.func.value.id == 'sqlite3']


def _is_readonly_uri(call):
    return (any(k.arg == 'uri' and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in call.keywords)
            and any('?mode=ro' in s for s in _strings(call)))


def _imports(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# 写语句的目标表提取：INSERT/REPLACE/UPDATE/DELETE/CREATE/DROP/ALTER。
# 每个动词后必须跟 whitespace，避免把 updated 之类的列名误判成写目标。
_WRITE_TARGET = re.compile(
    r'\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO\s+|REPLACE\s+INTO\s+|UPDATE\s+|DELETE\s+FROM\s+|'
    r'CREATE\s+(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?|'
    r'DROP\s+(?:TABLE|INDEX)\s+(?:IF\s+EXISTS\s+)?|ALTER\s+TABLE\s+)'
    r'[`"\[]?(\w+)', re.I)


def _write_targets(tree):
    found = set()
    for text in _strings(tree):
        found.update(m.group(1).lower() for m in _WRITE_TARGET.finditer(text))
    return found


class StaticSeparationLocks(unittest.TestCase):
    """AST 静态锁：边界在代码结构上成立，不依赖运行时自觉。"""

    @classmethod
    def setUpClass(cls):
        cls.shadow_tree = ast.parse(_read('emotion_shadow.py'))
        cls.candidate_tree = ast.parse(_read('valence_candidates.py'))
        cls.replay_tree = ast.parse(_read('emotion_replay.py'))

    # ---- 标注：只写自己的候选账本 ----

    def test_annotation_sql_writes_only_candidate_table(self):
        allowed = {'candidate', 'idx_candidate_at', 'idx_candidate_state'}
        self.assertLessEqual(_write_targets(self.candidate_tree), allowed,
                             '标注模块写到了候选表以外的目标')
        for text in _strings(self.candidate_tree):
            for banned in ('emotion_shadow.db', 'emotion_shadow_v2.db',
                           'shadow_run', 'shadow_event', 'shadow_gap', 'shadow_retrospective'):
                self.assertNotIn(banned, text, '标注模块引用了实时影子账本: %s' % banned)
        self.assertNotIn('emotion_shadow', _imports(self.candidate_tree))
        self.assertNotIn('emotion_replay', _imports(self.candidate_tree))

    def test_annotation_connections_are_candidate_ledger_or_readonly(self):
        calls = _connect_calls(self.candidate_tree)
        self.assertTrue(calls)
        writable = [c for c in calls if not _is_readonly_uri(c)]
        self.assertEqual(len(writable), 1, '标注模块只许有一个可写连接（候选账本）')
        conn = _function(self.candidate_tree, '_conn')
        self.assertIn(writable[0], list(ast.walk(conn)), '可写连接必须在 _conn 里')
        arg = writable[0].args[0]
        self.assertTrue(isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                        and arg.func.id == 'str' and arg.args
                        and isinstance(arg.args[0], ast.Call)
                        and isinstance(arg.args[0].func, ast.Name)
                        and arg.args[0].func.id == '_path',
                        '可写连接必须钉死在 _path()（valence_candidates.db）上')

    # ---- 回放：无数据库访问，只写新 JSON ----

    def test_replay_has_no_database_access(self):
        imports = _imports(self.replay_tree)
        for banned in ('sqlite3', 'os', 'emotion_shadow', 'valence_candidates'):
            self.assertNotIn(banned, imports, '回放模块不许碰 %s' % banned)
        for text in _strings(self.replay_tree):
            self.assertNotIn('.db', text, '回放模块引用了数据库文件名: %s' % text)
        self.assertFalse(_connect_calls(self.replay_tree))

    def test_replay_only_writes_exclusive_new_json(self):
        save = _function(self.replay_tree, 'save_report')
        save_nodes = list(ast.walk(save))
        opens = [n for n in ast.walk(self.replay_tree)
                 if isinstance(n, ast.Call)
                 and ((isinstance(n.func, ast.Name) and n.func.id == 'open')
                      or (isinstance(n.func, ast.Attribute) and n.func.attr == 'open'))]
        self.assertTrue(opens)
        exclusive = 0
        for call in opens:
            # Path.open 的第一个位置参数就是 mode；内建 open 的 mode 在第二位。
            positional = call.args[0] if isinstance(call.func, ast.Attribute) else (
                call.args[1] if len(call.args) >= 2 else None)
            mode = positional if positional is not None else next(
                (k.value for k in call.keywords if k.arg == 'mode'), None)
            if mode is None:  # 默认只读
                continue
            self.assertIsInstance(mode, ast.Constant, '打开模式必须是字面量')
            if mode.value == 'x':
                exclusive += 1
                self.assertIn(call, save_nodes, '独占创建只许出现在 save_report')
            else:
                self.assertIn(mode.value, ('r', 'rb'), '回放模块不许以写模式打开文件')
        self.assertEqual(exclusive, 1, 'save_report 必须用 open(x) 独占创建新报告')
        self.assertIn('.json', _strings(save), 'save_report 必须限定 .json 后缀')
        self.assertTrue(any(isinstance(n, ast.Raise) for n in save_nodes),
                        'save_report 必须保留拒绝路径')

    # ---- 采集：只写实时账本，实时路径不消费候选 ----

    def test_collection_writes_only_shadow_ledger(self):
        allowed = {'shadow_run', 'shadow_event', 'shadow_gap', 'shadow_retrospective'}
        self.assertLessEqual(_write_targets(self.shadow_tree), allowed,
                             '影子模块写到了实时账本以外的表')
        calls = _connect_calls(self.shadow_tree)
        writable = [c for c in calls if not _is_readonly_uri(c)]
        self.assertEqual(len(writable), 1, '影子模块只许有一个可写连接（实时账本）')
        tick = _function(self.shadow_tree, 'tick')
        self.assertIn(writable[0], list(ast.walk(tick)), '可写连接必须在 tick 里')
        arg = writable[0].args[0]
        self.assertTrue(isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                        and arg.func.id == 'shadow_path',
                        '采集的可写连接必须钉死在 shadow_path() 上')

    def test_live_path_never_consumes_candidates(self):
        for node in self.shadow_tree.body:
            if isinstance(node, ast.Import):
                self.assertNotIn('valence_candidates', [a.name for a in node.names])
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, 'valence_candidates')
        live_funcs = ('tick', '_evaluation', '_source_evaluation', '_backfill_day',
                      '_diagnostics', '_replay_cached', '_calibration', 'verify_replay', 'status')
        for name in live_funcs:
            func = _function(self.shadow_tree, name)
            for node in ast.walk(func):
                if isinstance(node, ast.Import):
                    self.assertNotIn('valence_candidates', [a.name for a in node.names],
                                     '%s 不许消费候选标注' % name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, 'valence_candidates',
                                        '%s 不许消费候选标注' % name)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotEqual(node.func.attr, 'integrated_appraisal',
                                        '%s 不许用候选整合评估' % name)
                elif isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, 'candidates', '%s 不许引用候选账本' % name)
        # 全模块唯一的候选整合入口必须是 valence_samples（只读反事实轨迹）。
        integrated = [n for n in ast.walk(self.shadow_tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == 'integrated_appraisal']
        self.assertEqual(len(integrated), 1)
        self.assertIn(integrated[0], list(ast.walk(_function(self.shadow_tree, 'valence_samples'))))

    def test_readonly_helper_enforces_query_only(self):
        func = _function(self.shadow_tree, '_readonly')
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == 'execute' and n.args
                            and isinstance(n.args[0], ast.Constant)
                            and n.args[0].value == 'PRAGMA query_only=ON'
                            for n in ast.walk(func)),
                        '跨账本只读助手必须结构性拒绝写（S04 守卫）')

    # ---- 表达控制：任何模块不得开启 ----

    def test_expression_control_never_enabled_anywhere(self):
        files = [p for p in BOT.rglob('*.py') if '__pycache__' not in p.parts]
        files += [p for p in (ROOT / 'website').rglob('*.py') if '__pycache__' not in p.parts]
        files.append(ROOT / 'launcher.py')
        emotion_modules = {'emotion_shadow.py', 'valence_candidates.py', 'emotion_replay.py'}
        for path in files:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            values = []
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == 'expression_control':
                    values.append(node.value)
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == 'expression_control':
                            values.append(value)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if ((isinstance(target, ast.Name) and target.id == 'expression_control')
                                or (isinstance(target, ast.Attribute) and target.attr == 'expression_control')
                                or (isinstance(target, ast.Subscript)
                                    and isinstance(target.slice, ast.Constant)
                                    and target.slice.value == 'expression_control')):
                            values.append(node.value)
            for value in values:
                if path.name in emotion_modules:
                    self.assertTrue(isinstance(value, ast.Constant) and value.value is False,
                                    '%s 的 expression_control 必须是字面量 False' % path.name)
                else:
                    # 审计模块允许用比较式回读输入（如 info.get(...) is True），
                    # 但任何模块都不得用 True 字面量或动态值开启表达控制。
                    self.assertTrue((isinstance(value, ast.Constant) and value.value is False)
                                    or isinstance(value, ast.Compare),
                                    '%s 出现了可开启 expression_control 的赋值' % path.name)


class _BehaviorBase(unittest.TestCase):
    """行为锁公共底座：隔离 DATA_DIR，真实 SQLite 账本，字节级对账。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s04_separation_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.persona = self.root / 'persona'
        self.persona.mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            'DATA_DIR': str(self.root), 'PERSONA_DATA_DIR': str(self.persona),
            'EMOTION_SHADOW_ENABLED': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source = self.persona / 'cognition.db'
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,session TEXT,'
                      'actor_qq TEXT,topic_qq TEXT,source TEXT,visibility TEXT,payload TEXT,'
                      'dedupe TEXT,created REAL)')

    def add(self, eid, ts=1001, text='这次处理得很靠谱'):
        # 词表外文本：实时规则不给效价，只有候选标注能给——正好检验边界。
        payload = {'text': text, 'addressed_to_self': True, 'address_basis': 'private'}
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (eid, 'private_message', ts, 'private:1', '', '', 'onebot', 'private',
                       json.dumps(payload), eid, ts))

    def label(self, eid, ts=1001, text='这次处理得很靠谱', stance='praise'):
        vc.enqueue(eid, at=ts, session='private:1', kind='private_message',
                   text=text, address_basis='private')
        asyncio.run(vc.drain(limit=10, judge=lambda *_: dict(
            stance=stance, confidence=0.95, reason='明确肯定', model='test-model')))

    def live_with_candidate(self):
        sh.tick(now=1000)
        self.add('m1')
        sh.tick(now=1002)
        self.label('m1')

    def ledger_bytes(self):
        return {p: p.read_bytes() for p in
                (sh.shadow_path(), self.persona / 'valence_candidates.db', self.source)
                if p.is_file()}


class SeparationBehaviorLocks(_BehaviorBase):

    def test_annotation_writes_stay_in_candidate_ledger(self):
        sh.tick(now=1000)
        self.add('m1')
        sh.tick(now=1002)
        shadow_db = sh.shadow_path()
        before_shadow = shadow_db.read_bytes()
        before_source = self.source.read_bytes()
        self.label('m1')
        self.assertEqual(shadow_db.read_bytes(), before_shadow, '标注动了实时账本字节')
        self.assertEqual(self.source.read_bytes(), before_source, '标注动了源账本字节')
        self.assertTrue((self.persona / 'valence_candidates.db').is_file())
        got = vc.recent_readonly()
        self.assertEqual(got['stats']['total'], 1)
        self.assertEqual(got['stats']['done'], 1)
        self.assertEqual(sorted(p.name for p in self.persona.iterdir()),
                         sorted(['cognition.db', shadow_db.name, 'valence_candidates.db']))

    def test_collection_never_consumes_candidate_labels(self):
        sh.tick(now=1000)
        self.add('m1')
        sh.tick(now=1002)
        before = sh.status(now=1002, include_valence=False)
        self.label('m1')
        cand = self.persona / 'valence_candidates.db'
        cand_before = cand.read_bytes()
        result = sh.tick(now=1003)
        self.assertEqual(result['processed'], before['processed'])
        self.assertEqual(cand.read_bytes(), cand_before, '采集动了候选账本字节')
        after = sh.status(now=1003, include_valence=False)
        self.assertEqual(after['state']['valence'], 0, '候选标注泄漏进了实时效价')
        self.assertEqual(after['timeline'], before['timeline'])
        self.assertEqual(after['real_events'], before['real_events'])
        self.assertTrue(all(not str(r['rule']).startswith('integrated_') for r in after['timeline']))
        self.assertTrue(after['diagnostics']['evidence_replay']['ok'])
        report = sh.valence_samples(now=1003)
        self.assertEqual(report['stats']['included'], 1)
        self.assertGreater(report['state']['valence'], 0)
        self.assertEqual(report['trajectory'], tr.CANDIDATE_COUNTERFACTUAL)
        self.assertFalse(report['counts_for_live'])

    def test_collection_creates_no_candidate_ledger(self):
        sh.tick(now=1000)
        self.add('m1')
        sh.tick(now=1002)
        self.assertFalse((self.persona / 'valence_candidates.db').exists())
        self.assertEqual(sorted(p.name for p in self.persona.iterdir()),
                         sorted(['cognition.db', sh.shadow_path().name]))

    def test_candidate_replay_and_status_touch_no_ledger_bytes(self):
        self.live_with_candidate()
        before = self.ledger_bytes()
        self.assertEqual(len(before), 3)
        report = sh.valence_samples(now=1004)
        full = sh.status(now=1004, include_valence=True)
        self.assertEqual(full['valence_samples'], report)
        self.assertEqual(self.ledger_bytes(), before, '候选回放/状态读取写了账本')
        self.assertIs(report['expression_control'], False)
        self.assertIs(full['expression_control'], False)

    def test_backfill_reads_source_without_writing(self):
        now = time.time()
        self.add('bf1', ts=now - 30)
        before = self.source.read_bytes()
        result = vc.backfill(days=7, limit=10)
        self.assertEqual(result['added'], 1)
        self.assertEqual(self.source.read_bytes(), before, 'backfill 写了源账本')
        self.assertFalse(sh.shadow_path().exists(), 'backfill 创建了实时账本')

    def test_replay_writes_only_a_new_json_report(self):
        self.live_with_candidate()
        before = self.ledger_bytes()
        dbs_before = sorted(str(p) for p in self.root.rglob('*.db'))
        bundle = {'histories': {'private-user-1': {'turns': [
            {'role': 'user', 'content': '谢谢你', 'ts': 1790000000}]}}}
        report = replay.reference_report(bundle)
        dest = self.root / 'reports' / 'reference.json'
        replay.save_report(report, dest)
        self.assertTrue(dest.is_file())
        saved = json.loads(dest.read_text(encoding='utf-8'))
        self.assertEqual(saved['trajectory'], tr.V1_REFERENCE)
        self.assertFalse(saved['counts_for_live'])
        self.assertEqual(self.ledger_bytes(), before, '回放写了实时/候选/源账本')
        self.assertEqual(sorted(str(p) for p in self.root.rglob('*.db')), dbs_before,
                         '回放创建或改动了数据库文件')
        self.assertEqual(sorted(p.name for p in self.persona.iterdir()),
                         sorted(['cognition.db', sh.shadow_path().name, 'valence_candidates.db']))

    def test_save_report_refuses_live_persona_and_existing_targets(self):
        report = {'mode': 'synthetic_reference'}
        for name in ('cognition.db', 'emotion_shadow.db', 'emotion_shadow_v2.db',
                     'valence_candidates.db', 'self.json', 'config.json', 'histories.json',
                     'world_knowledge.json', 'group_facts.json'):
            target = self.persona / name
            target.write_bytes(b'production-placeholder')
            with self.assertRaises((ValueError, FileExistsError), msg=name):
                replay.save_report(report, target)
            self.assertEqual(target.read_bytes(), b'production-placeholder', name)
        existing = self.root / 'already.json'
        existing.write_bytes(b'old-report')
        with self.assertRaises(FileExistsError):
            replay.save_report(report, existing)
        self.assertEqual(existing.read_bytes(), b'old-report')
        with self.assertRaises(ValueError):
            replay.save_report(report, self.root / 'report.db')

    def test_expression_control_and_live_contract_on_every_report(self):
        empty = sh.status(now=1000)
        self.assertIs(empty['expression_control'], False)
        self.assertIs(sh.valence_samples(now=1000)['expression_control'], False)
        self.live_with_candidate()
        for include in (False, True):
            status = sh.status(now=1002, include_valence=include)
            self.assertIs(status['expression_control'], False)
            self.assertEqual(status['trajectory'], tr.V2_LIVE)
            self.assertTrue(status['counts_for_live'])
        samples = sh.valence_samples(now=1002)
        self.assertIs(samples['expression_control'], False)
        self.assertFalse(samples['counts_for_live'])

    def test_readonly_helper_structurally_rejects_writes(self):
        self.live_with_candidate()
        for path in (self.source, self.persona / 'valence_candidates.db', sh.shadow_path()):
            with closing(sh._readonly(path)) as c:
                with self.assertRaises(sqlite3.Error, msg=str(path)):
                    c.execute('CREATE TABLE s04_probe(x)')
                self.assertTrue(c.execute('SELECT name FROM sqlite_master').fetchall())

    def test_annotation_writable_connection_is_pinned_to_candidate_ledger(self):
        for name in ('emotion_shadow_v2.db', 'emotion_shadow.db', 'cognition.db'):
            with patch.object(vc, '_path', return_value=self.persona / name):
                with self.assertRaises(ValueError, msg=name):
                    vc._conn()
        with closing(vc._conn()) as c:
            self.assertEqual(c.execute('SELECT 1').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()