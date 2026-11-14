"""S05「效价候选人工标注基准」回归锁。

钉死的口径（统一方案 §9.3 / 待办 S05）：
- 金标准只来自人工标注，候选 confidence 绝不当金标准；
- 枚举校验、文件名守卫（可写连接只许落在 valence_candidates.db）、
  只写 annotation 表不动 candidate 表；
- dev/holdout 按 event_id 哈希稳定划分，标注不接受调用者指定 split；
- 重复标注按修订处理：保留历史，评测取最新修订；
- 各指标用手算小夹具核对精确值；校准样本不足显式 insufficient_data；
- isolated_eval 构造样本绝不混入 natural 自然观察统计；
- 只读报告缺库/缺表返回 missing_data，不建目录不建表。

本文件所有合成数据都在隔离临时目录，且显式标 origin='isolated_eval'；
不伪造任何自然样本。
"""
import ast
import asyncio
import hashlib
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import valence_annotation as va  # noqa: E402
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


_WRITE_TARGET = re.compile(
    r'\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO\s+|REPLACE\s+INTO\s+|UPDATE\s+|DELETE\s+FROM\s+|'
    r'CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?|'
    r'DROP\s+(?:TABLE|INDEX)\s+(?:IF\s+EXISTS\s+)?|ALTER\s+TABLE\s+)'
    r'[`"\[]?(\w+)', re.I)


def _write_targets(tree):
    found = set()
    for text in _strings(tree):
        found.update(m.group(1).lower() for m in _WRITE_TARGET.finditer(text))
    return found


class StaticAnnotationLocks(unittest.TestCase):
    """AST 静态锁：标注边界在代码结构上成立（沿用 S04 风格）。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_read('valence_annotation.py'))

    def test_writes_only_annotation_table(self):
        allowed = {'annotation', 'idx_annotation_event_rev', 'idx_annotation_split'}
        self.assertLessEqual(_write_targets(self.tree), allowed,
                             '人工标注模块写到了 annotation 以外的目标')
        self.assertNotIn('candidate', _write_targets(self.tree),
                         '人工标注模块不许写 candidate 表')
        for text in _strings(self.tree):
            for banned in ('emotion_shadow.db', 'emotion_shadow_v2.db',
                           'shadow_run', 'shadow_event', 'shadow_gap',
                           'shadow_retrospective'):
                self.assertNotIn(banned, text, '引用了实时影子账本: %s' % banned)
        self.assertNotIn('emotion_shadow', _imports(self.tree))
        self.assertNotIn('emotion_replay', _imports(self.tree))

    def test_single_writable_connection_pinned_to_candidate_ledger(self):
        calls = _connect_calls(self.tree)
        self.assertTrue(calls)
        writable = [c for c in calls if not _is_readonly_uri(c)]
        self.assertEqual(len(writable), 1, '只许有一个可写连接（候选账本文件）')
        conn = _function(self.tree, '_conn')
        self.assertIn(writable[0], list(ast.walk(conn)), '可写连接必须在 _conn 里')
        arg = writable[0].args[0]
        self.assertTrue(isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                        and arg.func.id == 'str' and arg.args
                        and isinstance(arg.args[0], ast.Call)
                        and isinstance(arg.args[0].func, ast.Name)
                        and arg.args[0].func.id == '_path',
                        '可写连接必须钉死在 _path()（valence_candidates.db）上')
        guard_src = ast.get_source_segment(_read('valence_annotation.py'), conn)
        self.assertIn('valence_candidates.db', guard_src, '_conn 必须有文件名守卫')


class _Base(unittest.TestCase):
    """隔离底座：真实 SQLite 账本落在 Python tempfile 目录，不碰真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s05_annotation_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.persona = self.root / 'persona'
        self.persona.mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            'DATA_DIR': str(self.root), 'PERSONA_DATA_DIR': str(self.persona),
            'QQ_DRY_RUN': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = self.persona / 'valence_candidates.db'

    def annotate(self, eid, stance='praise', **over):
        kwargs = dict(slice_name='direct_praise', gold_stance=stance,
                      gold_target='self', annotator='test-human',
                      origin='isolated_eval')
        kwargs.update(over)
        return va.annotate(eid, **kwargs)

    def candidate(self, eid, text='谢谢你', stance='praise', confidence=0.95):
        vc.enqueue(eid, at=1000, session='private:1', kind='private_message',
                   text=text, address_basis='private')
        asyncio.run(vc.drain(limit=10, judge=lambda *_: dict(
            stance=stance, confidence=confidence, reason='测试', model='test-model')))


class AnnotationWriteLocks(_Base):

    def test_invalid_enums_refused_before_any_write(self):
        bad = [dict(slice_name='not_a_slice'), dict(gold_stance='angry'),
               dict(gold_target='everyone'), dict(origin='synthetic'),
               dict(annotator='  '), dict(slice_name=None)]
        for over in bad:
            with self.assertRaises(ValueError, msg=repr(over)):
                self.annotate('bad1', **over)
        with self.assertRaises(ValueError):
            self.annotate('   ')
        self.assertFalse(self.db.exists(), '非法标注不许落库/建库')

    def test_filename_guard_rejects_other_ledgers(self):
        for name in ('emotion_shadow_v2.db', 'emotion_shadow.db', 'cognition.db'):
            with patch.object(va, '_path', return_value=self.persona / name):
                with self.assertRaises(ValueError, msg=name):
                    va._conn()
                with self.assertRaises(ValueError, msg=name):
                    self.annotate('guard1')
            self.assertFalse((self.persona / name).exists(), name)
        with closing(va._conn()) as c:
            self.assertEqual(c.execute('SELECT 1').fetchone()[0], 1)

    def test_split_is_stable_hash_and_not_caller_chosen(self):
        for i in range(200):
            eid = 'evt_%d' % i
            expected = ('holdout' if hashlib.sha256(eid.encode('utf-8')).digest()[0] < 51
                        else 'dev')
            self.assertEqual(va.stable_split(eid), expected)
            self.assertEqual(va.stable_split(eid), va.stable_split(eid))
        splits = {va.stable_split('evt_%d' % i) for i in range(200)}
        self.assertEqual(splits, {'dev', 'holdout'})
        holdout = sum(1 for i in range(200)
                      if va.stable_split('evt_%d' % i) == 'holdout')
        self.assertTrue(10 < holdout < 70, 'holdout 比例应约 20%%，实际 %d/200' % holdout)
        got = self.annotate('evt_7')
        self.assertEqual(got['split'], va.stable_split('evt_7'))
        with self.assertRaises(TypeError):
            va.annotate('evt_8', slice_name='direct_praise', gold_stance='praise',
                        gold_target='self', annotator='h', origin='isolated_eval',
                        split='dev')

    def test_repeat_annotation_keeps_history_latest_wins(self):
        first = self.annotate('rev1', stance='praise')
        second = self.annotate('rev1', stance='rebuke', slice_name='direct_rebuke',
                               note='看错上下文，修订')
        self.assertEqual((first['rev'], second['rev']), (1, 2))
        self.assertEqual(first['split'], second['split'], '修订不许改变 split')
        latest = va.query_annotations(first['split'])
        rows = [r for r in latest['rows'] if r['event_id'] == 'rev1']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['gold_stance'], 'rebuke')
        self.assertEqual(rows[0]['rev'], 2)
        history = va.query_annotations(first['split'], include_history=True)
        rows = [r for r in history['rows'] if r['event_id'] == 'rev1']
        self.assertEqual([r['rev'] for r in rows], [1, 2])
        self.assertEqual([r['gold_stance'] for r in rows], ['praise', 'rebuke'])

    def test_query_requires_explicit_valid_split(self):
        self.annotate('q1')
        with self.assertRaises(TypeError):
            va.query_annotations()
        for bad in ('all', 'train', '', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                va.query_annotations(bad)
        with self.assertRaises(ValueError):
            va.query_annotations('dev', origin='mixed')
        with self.assertRaises(ValueError):
            va.evaluation_report('all')


class WorklistLocks(_Base):

    def test_worklist_is_readonly_with_hints_not_gold(self):
        vc.enqueue('w1', at=1, text='谢谢你', address_basis='private')
        vc.enqueue('w2', at=2, text='你说错了', address_basis='private')
        vc.enqueue('w3', at=3, text='「你真棒」', address_basis='private')
        vc.enqueue('w4', at=4, text='今天天气不错', address_basis='ambient')
        before = self.db.read_bytes()
        got = va.annotation_worklist(limit=10)
        self.assertTrue(got['ok'])
        self.assertEqual(self.db.read_bytes(), before, '工作清单写了候选账本')
        hints = {r['event_id']: r['slice_hint'] for r in got['rows']}
        self.assertEqual(hints, {'w1': 'direct_praise', 'w2': 'direct_rebuke',
                                 'w3': 'quotation', 'w4': 'third_person'})
        for row in got['rows']:
            self.assertEqual(row['state'], 'pending', '工作清单改了候选状态')
            self.assertEqual(row['split'], va.stable_split(row['event_id']))
            self.assertNotIn('gold_stance', row, '清单不许预填金标准')
        self.annotate('w1')
        got = va.annotation_worklist(limit=10)
        self.assertEqual({r['event_id'] for r in got['rows']}, {'w2', 'w3', 'w4'})

    def test_worklist_missing_db_creates_nothing(self):
        absent = self.root / 'absent' / 'persona'
        with patch.dict(os.environ, {'PERSONA_DATA_DIR': str(absent)}):
            got = va.annotation_worklist()
            self.assertEqual(got['status'], 'missing_data')
            self.assertFalse(absent.exists(), '只读清单建了目录')


def _gold(eid, stance, target='self'):
    return {'event_id': eid, 'gold_stance': stance, 'gold_target': target,
            'slice': 'direct_praise', 'origin': 'isolated_eval'}


def _pred(eid, stance, conf, lexical='none', target='self'):
    return {'event_id': eid, 'stance': stance, 'confidence': conf,
            'lexical': lexical, 'target': target}


# 手算夹具（10 条，全部 isolated_eval）：
#  gold -> pred:  praise->praise x2, praise->rebuke, rebuke->rebuke,
#  rebuke->uncertain, neutral->neutral, neutral->praise,
#  uncertain->uncertain, rebuke->praise(对象 other->self), praise->neutral
#  praise precision = 2/4 = 0.5；rebuke precision = 1/2 = 0.5
#  F1: praise 2TP/2FP/2FN=0.5；rebuke 1TP/1FP/2FN=0.4；neutral 1TP/1FP/1FN=0.5
#  macro-F1 = (0.5+0.4+0.5)/3 = 7/15；拒判覆盖 = 2/10 = 0.2
#  对象错误 = 1/10 = 0.1；词法 hit=3 missed=1 mismatch=3 neutral=3
_FIXTURE = [
    (_gold('e1', 'praise'), _pred('e1', 'praise', 0.9, 'positive')),
    (_gold('e2', 'praise'), _pred('e2', 'praise', 0.8, 'positive')),
    (_gold('e3', 'praise'), _pred('e3', 'rebuke', 0.7, 'positive')),
    (_gold('e4', 'rebuke'), _pred('e4', 'rebuke', 0.9, 'negative')),
    (_gold('e5', 'rebuke'), _pred('e5', 'uncertain', 0.4)),
    (_gold('e6', 'neutral', 'none'), _pred('e6', 'neutral', 0.6, target='none')),
    (_gold('e7', 'neutral'), _pred('e7', 'praise', 0.55)),
    (_gold('e8', 'uncertain', 'none'), _pred('e8', 'uncertain', 0.3, target='none')),
    (_gold('e9', 'rebuke', 'other'), _pred('e9', 'praise', 0.85, 'negative')),
    (_gold('e10', 'praise'), _pred('e10', 'neutral', 0.5, 'negative')),
]


class EvaluateMetricLocks(unittest.TestCase):
    """纯计算指标：手算小夹具核对精确值。"""

    def test_hand_computed_precision_macro_f1_and_slices(self):
        got = va.evaluate([g for g, _ in _FIXTURE], [p for _, p in _FIXTURE])
        self.assertEqual(got['n_joined'], 10)
        self.assertEqual(got['origin'], 'isolated_eval')
        self.assertEqual(got['praise_precision'], 0.5)
        self.assertEqual((got['praise_predicted'], got['praise_correct']), (4, 2))
        self.assertEqual(got['rebuke_precision'], 0.5)
        self.assertEqual((got['rebuke_predicted'], got['rebuke_correct']), (2, 1))
        self.assertAlmostEqual(got['per_class']['praise']['f1'], 0.5)
        self.assertAlmostEqual(got['per_class']['rebuke']['f1'], 0.4)
        self.assertAlmostEqual(got['per_class']['neutral']['f1'], 0.5)
        self.assertAlmostEqual(got['macro_f1'], 7 / 15)
        self.assertEqual(got['abstention_rate'], 0.2)
        self.assertEqual(got['target_error_rate'], 0.1)
        self.assertEqual((got['target_checked'], got['target_errors']), (10, 1))
        self.assertEqual(got['lexical_relation'],
                         {'hit': 3, 'missed': 1, 'mismatch': 3, 'neutral': 3})
        self.assertEqual(got['gold_source'], 'human_annotation')

    def test_calibration_insufficient_data_is_explicit(self):
        got = va.evaluate([g for g, _ in _FIXTURE], [p for _, p in _FIXTURE])
        self.assertEqual(got['calibration']['status'], 'insufficient_data')
        self.assertEqual(got['calibration']['n'], 8)  # 两条 uncertain 不参与
        self.assertNotIn('ece', got['calibration'], '样本不足不许硬算 ECE')

    def test_calibration_ece_hand_computed_single_bin(self):
        golds = [_gold('c%d' % i, 'praise') for i in range(40)]
        preds = [_pred('c%d' % i, 'praise', 0.8) for i in range(40)]
        got = va.evaluate(golds, preds)
        cal = got['calibration']
        self.assertEqual(cal['status'], 'ok')
        self.assertEqual(cal['n'], 40)
        self.assertAlmostEqual(cal['ece'], 0.2)  # |acc 1.0 - conf 0.8|
        self.assertEqual(len(cal['bins']), 1)
        self.assertAlmostEqual(cal['bins'][0]['accuracy'], 1.0)

    def test_calibration_ece_hand_computed_two_bins(self):
        golds = [_gold('a%d' % i, 'praise') for i in range(30)]
        golds += [_gold('b%d' % i, 'rebuke') for i in range(10)]
        preds = [_pred('a%d' % i, 'praise', 0.9) for i in range(30)]
        preds += [_pred('b%d' % i, 'praise', 0.2) for i in range(10)]
        cal = va.evaluate(golds, preds)['calibration']
        # 30/40*|1-0.9| + 10/40*|0-0.2| = 0.075 + 0.05 = 0.125
        self.assertAlmostEqual(cal['ece'], 0.125)

    def test_candidate_high_confidence_never_becomes_gold(self):
        golds = [_gold('h1', 'rebuke')]
        preds = [_pred('h1', 'praise', 0.99, 'positive')]
        got = va.evaluate(golds, preds)
        self.assertEqual(got['praise_precision'], 0.0, '高置信错误预测必须按金标准计错')
        self.assertEqual(got['macro_f1'], 0.0)
        self.assertEqual(golds[0]['gold_stance'], 'rebuke', '评测不许改写金标准')
        # 反向：低置信预测对了就是对了，置信度不影响 precision/F1。
        got = va.evaluate([_gold('h2', 'praise')], [_pred('h2', 'praise', 0.01)])
        self.assertEqual(got['praise_precision'], 1.0)
        self.assertAlmostEqual(got['per_class']['praise']['f1'], 1.0)

    def test_mixed_origin_and_duplicate_gold_refused(self):
        natural = dict(_gold('n1', 'praise'), origin='natural')
        with self.assertRaises(ValueError):
            va.evaluate([natural, _gold('i1', 'praise')], [])
        with self.assertRaises(ValueError):
            va.evaluate([_gold('d1', 'praise'), _gold('d1', 'rebuke')], [])
        with self.assertRaises(ValueError):
            va.evaluate([{'event_id': 'x', 'gold_stance': 'praise'}], [])

    def test_empty_join_reports_none_not_fake_numbers(self):
        got = va.evaluate([_gold('solo', 'praise')], [])
        self.assertEqual(got['n_joined'], 0)
        self.assertIsNone(got['macro_f1'])
        self.assertIsNone(got['praise_precision'])
        self.assertIsNone(got['abstention_rate'])
        self.assertIsNone(got['target_error_rate'])
        self.assertEqual(got['calibration']['status'], 'insufficient_data')


class ReportLocks(_Base):

    def _same_split_ids(self, prefix, n):
        out, i = [], 0
        while len(out) < n:
            eid = '%s%d' % (prefix, i)
            i += 1
            if not out or va.stable_split(eid) == va.stable_split(out[0]):
                out.append(eid)
        return out

    def test_report_missing_db_creates_nothing(self):
        absent = self.root / 'absent' / 'persona'
        with patch.dict(os.environ, {'PERSONA_DATA_DIR': str(absent)}):
            for got in (va.evaluation_report('dev'), va.query_annotations('dev')):
                self.assertFalse(got['ok'])
                self.assertEqual(got['status'], 'missing_data')
            self.assertFalse(absent.exists(), '只读报告建了目录')
            self.assertFalse((absent / 'valence_candidates.db').exists())

    def test_report_missing_annotation_table_no_writes(self):
        vc.enqueue('only-candidate', at=1, text='谢谢你')
        before = self.db.read_bytes()
        got = va.evaluation_report('dev')
        self.assertEqual(got['status'], 'missing_data')
        self.assertEqual(va.query_annotations('dev')['status'], 'missing_data')
        self.assertEqual(self.db.read_bytes(), before, '缺表报告写了账本')

    def test_report_separates_natural_and_isolated_eval(self):
        a, b, c = self._same_split_ids('nat', 3)
        split = va.stable_split(a)
        for eid in (a, b, c):
            self.candidate(eid, text='谢谢你', stance='praise', confidence=0.95)
        self.annotate(a, stance='praise', origin='natural')
        self.annotate(b, stance='rebuke', origin='natural',
                      slice_name='irony_joke')
        self.annotate(c, stance='praise', origin='isolated_eval')
        before = self.db.read_bytes()
        natural = va.evaluation_report(split)  # 默认 origin='natural'
        self.assertTrue(natural['ok'])
        self.assertEqual(natural['origin'], 'natural')
        self.assertEqual(natural['n_gold'], 2, 'isolated_eval 混进了自然统计')
        self.assertEqual(natural['praise_precision'], 0.5)
        isolated = va.evaluation_report(split, origin='isolated_eval')
        self.assertEqual(isolated['n_gold'], 1)
        self.assertEqual(isolated['praise_precision'], 1.0)
        rows = va.query_annotations(split, origin='natural')['rows']
        self.assertEqual({r['event_id'] for r in rows}, {a, b})
        self.assertEqual(self.db.read_bytes(), before, '只读报告/查询写了账本')
        # 候选高置信（0.95 praise）不改变人工金标准：b 的人工 rebuke 仍计错。
        self.assertEqual(natural['praise_correct'], 1)

    def test_report_uses_latest_revision(self):
        a, b = self._same_split_ids('rev', 2)
        split = va.stable_split(a)
        for eid in (a, b):
            self.candidate(eid)
        self.annotate(a, stance='praise', origin='natural')
        self.annotate(b, stance='rebuke', origin='natural')
        self.assertEqual(va.evaluation_report(split)['praise_precision'], 0.5)
        self.annotate(b, stance='praise', origin='natural', note='修订：确实是夸')
        got = va.evaluation_report(split)
        self.assertEqual(got['praise_precision'], 1.0, '报告没用最新修订')
        history = va.query_annotations(split, origin='natural',
                                       include_history=True)['rows']
        self.assertEqual(len(history), 3, '修订丢了历史')

    def test_no_human_annotations_yet_is_missing_data_not_fake_metrics(self):
        # 真实上线初期状态：只有候选账本、没有任何人工标注 → 如实 missing_data，
        # 不建 annotation 表、不产出任何假指标。
        self.candidate('no-gold')
        before = self.db.read_bytes()
        got = va.evaluation_report(va.stable_split('no-gold'))
        self.assertFalse(got['ok'])
        self.assertEqual(got['status'], 'missing_data')
        self.assertEqual(self.db.read_bytes(), before)
        # 一旦有了第一条人工标注，空 join 也如实报 insufficient_data。
        other = 'empty-split-probe'
        self.annotate(other, origin='isolated_eval')
        split = va.stable_split(other)
        got = va.evaluation_report(split, origin='isolated_eval')
        self.assertTrue(got['ok'])
        self.assertEqual(got['n_gold'], 1)
        self.assertEqual(got['n_joined'], 0)
        self.assertEqual(got['status'], 'insufficient_data')
        self.assertIsNone(got['macro_f1'])


if __name__ == '__main__':
    unittest.main()