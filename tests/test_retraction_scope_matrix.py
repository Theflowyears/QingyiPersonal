# -*- coding: utf-8 -*-
"""V03 分层撤回 × 范围故障矩阵（validation 轨，贯穿 A08/M05/S03/R01–R04/M07）。

路线 §5.2 认识论分层 / §5.4 反思不得制造经历 / §9「记忆纠错」行的贯穿验证：
源事件撤回后，逐层真实执行「撤回前→撤回后→重跑」，断言分型正确——

  认知层（A08）   belief/episode/commitment/self_view/action/goal 失效，
                  原始事件文本一个字节不改；撤回必须带理由。
  派生层（M05）   传递失效、独立支持 needs_reassessment 不无脑恢复、
                  同根源转述不算独立、四类修正语义分开、epoch 读门挡旧投影。
  情感层（S03）   撤源重算与「事件从未存在」逐位一致（0 误差）、解释纠正
                  白名单、撤回单调洗不回、版本分支只追加。
  检索层（R01–R04）撤回后旧投影不复活、invalidated 不作种子/邻居、根源去重
                  仍成立、详情未知端点不扩散。
  情景层（M07）   撤回/失效源不复活为 episode 依据、已失效 episode 单调。

范围故障（X14）：看见≠可转述（recipient_scope 硬过滤）、派生只能相同或
更窄（scope_revoked 沿闭包传播）、范围收紧不改命题真假、跨范围泄漏必须
被挡下并出声（稳定原因码）。故障分型沿用 D07 口径：缺数据≠零（absent/
degraded 出声）、暂时锁≠损坏（unreadable/locked，释放后可读回）、撤回后
的旧投影不得继续当有效支持（epoch 读门）。

全部用隔离临时 DATA_DIR 与合成事件，真实执行被测契约（不打桩断言），
不读不写真实 data/，不发网络。执行器/报告器是 bot/retraction_scope_matrix.py
（纯编排）；每个用例的 层×故障×期望分型 显式登记在 CASE_PLAN，覆盖审计
钉死每个声明的交叉点都真测到。发现的现有契约缺陷只报告，不在矩阵里把
缺陷断言成正确行为（见 ret_lock_recovery / der_lock_unreadable 的注释）。
"""
import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import association_edges as ae            # noqa: E402
import emotion_revision as rev            # noqa: E402
import invalidation_dag as dag            # noqa: E402
import private_turn_episodes as pte       # noqa: E402
import recall_scoring as rs               # noqa: E402
import retraction_scope_matrix as rsm     # noqa: E402
import retrieval_objects as ro            # noqa: E402
import ripple_paths as rp                 # noqa: E402
import self_model as sm                   # noqa: E402

NOW = 4000.0          # 检索 as_of：晚于全部合成事件，避开未来证据门
START, END = 900.0, 5000.0
PRAISE = '谢谢你帮我查清楚了'
REBUKE = '你刚才说错了'
TIRED = '我今天好累'

# 声明的交叉点：每个 (层, 故障) 都必须有真实用例命中（CoverageTests 钉死）。
INTERSECTIONS = {
    ('cognition', 'retract_source'), ('cognition', 'missing_data'),
    ('cognition', 'transient_lock'), ('cognition', 'scope_revoked'),
    ('derivation', 'retract_source'), ('derivation', 'same_root'),
    ('derivation', 'interpretation_revised'), ('derivation', 'belief_corrected'),
    ('derivation', 'scope_revoked'), ('derivation', 'stale_projection'),
    ('derivation', 'missing_data'), ('derivation', 'transient_lock'),
    ('emotion', 'retract_source'), ('emotion', 'stale_projection'),
    ('emotion', 'interpretation_revised'), ('emotion', 'belief_corrected'),
    ('emotion', 'missing_data'),
    ('retrieval', 'retract_source'), ('retrieval', 'stale_projection'),
    ('retrieval', 'same_root'), ('retrieval', 'missing_data'),
    ('retrieval', 'cross_scope_leak'), ('retrieval', 'scope_revoked'),
    ('retrieval', 'transient_lock'),
    ('episode', 'retract_source'), ('episode', 'stale_projection'),
    ('episode', 'missing_data'), ('episode', 'interpretation_revised'),
}

# 用例登记表：case_id → (层, 故障)。测试方法名约定为 test_<case_id>。
CASE_PLAN = {
    'cog_retract_event_preserved': ('cognition', 'retract_source'),
    'cog_retract_derived_objects': ('cognition', 'retract_source'),
    'cog_retract_requires_reason': ('cognition', 'retract_source'),
    'cog_missing_ledger': ('cognition', 'missing_data'),
    'cog_lock_not_invalid': ('cognition', 'transient_lock'),
    'cog_scope_truth_unchanged': ('cognition', 'scope_revoked'),
    'der_retract_single_support': ('derivation', 'retract_source'),
    'der_retract_multi_hop': ('derivation', 'retract_source'),
    'der_independent_support': ('derivation', 'retract_source'),
    'der_monotonic_rerun': ('derivation', 'retract_source'),
    'der_same_root_not_independent': ('derivation', 'same_root'),
    'der_interpretation_preserves_event': ('derivation', 'interpretation_revised'),
    'der_belief_corrected': ('derivation', 'belief_corrected'),
    'der_scope_narrowed_closure': ('derivation', 'scope_revoked'),
    'der_stale_projection_gate': ('derivation', 'stale_projection'),
    'der_missing_bridge': ('derivation', 'missing_data'),
    'der_lock_unreadable': ('derivation', 'transient_lock'),
    'emo_retract_zero_error': ('emotion', 'retract_source'),
    'emo_retract_after_replacement': ('emotion', 'retract_source'),
    'emo_retract_not_naive_delta': ('emotion', 'stale_projection'),
    'emo_append_only': ('emotion', 'stale_projection'),
    'emo_interpretation_whitelist': ('emotion', 'interpretation_revised'),
    'emo_retract_monotonic': ('emotion', 'interpretation_revised'),
    'emo_wrong_kind_rejected': ('emotion', 'belief_corrected'),
    'emo_missing_bridge': ('emotion', 'missing_data'),
    'ret_retract_belief_gone': ('retrieval', 'retract_source'),
    'ret_retract_partial_support': ('retrieval', 'retract_source'),
    'ret_m05_invalidated_no_resurrect': ('retrieval', 'retract_source'),
    'ret_pool_seed_strictest': ('retrieval', 'stale_projection'),
    'ret_same_root_dedup': ('retrieval', 'same_root'),
    'ret_unknown_neighbor': ('retrieval', 'missing_data'),
    'ret_private_to_group_blocked': ('retrieval', 'cross_scope_leak'),
    'ret_scope_narrowing': ('retrieval', 'scope_revoked'),
    'ret_missing_degraded': ('retrieval', 'missing_data'),
    'ret_archive_grant': ('retrieval', 'cross_scope_leak'),
    'ret_r04_scope_caller': ('retrieval', 'cross_scope_leak'),
    'ret_lock_recovery': ('retrieval', 'transient_lock'),
    'epi_retract_blocked_rerun': ('episode', 'retract_source'),
    'epi_m05_source_blocked': ('episode', 'retract_source'),
    'epi_self_invalidation_monotonic': ('episode', 'stale_projection'),
    'epi_missing_journal': ('episode', 'missing_data'),
    'epi_interpretation_candidate_only': ('episode', 'interpretation_revised'),
}


def edge(derived, source, dk='interpretation', sk='event'):
    return {'derived_id': derived, 'source_id': source,
            'derived_kind': dk, 'source_kind': sk, 'basis': 'v03'}


class Base(unittest.TestCase):
    """隔离夹具：每个用例独立临时 DATA_DIR，路径调用时解析，不碰真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v03_', ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._old = os.environ.get('DATA_DIR'), os.environ.get('PERSONA_DATA_DIR')
        os.environ['DATA_DIR'] = self.root
        os.environ.pop('PERSONA_DATA_DIR', None)

    def tearDown(self):
        for key, value in zip(('DATA_DIR', 'PERSONA_DATA_DIR'), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── 合成事件与派生对象（全部走真实写入 API）──────────────────────

    def seed_event(self, text, dedupe, session='private:1001', visibility='private',
                   ts=1000.0, addressed=True, kind='private_message'):
        return sm.record_event(kind, session=session, actor_qq='1001',
                               visibility=visibility, ts=ts, dedupe=dedupe,
                               payload={'text': text, 'addressed_to_self': addressed})

    def seed_belief(self, proposition, sources, dedupe_ts=1000.0):
        """sources: [(event_id, quote)]；逐命题 span 走真实 add_belief。"""
        spans = [{'event_id': eid, 'quote': quote, 'span_start': 0,
                  'span_end': len(quote), 'match_kind': 'model_quote'}
                 for eid, quote in sources]
        return sm.add_belief(proposition, subject_qq='1001', ts=dedupe_ts, spans=spans)

    def commit(self, edges, corrections, states=None):
        state = dag.read_invalidation_state()
        plan = dag.propagate(edges, corrections, epoch=state['epoch'],
                             states=state['states'] if states is None else states,
                             now=1000.0)
        result = sm.write_invalidation_transaction({
            'ts': 1000.0, 'expected_epoch': plan['expected_epoch'],
            'edges': edges, 'corrections': plan['corrections'], 'propagation': plan})
        return plan, result

    def rows(self, table):
        connection = sm._ro_db()
        if connection is None:
            return []
        with closing(connection) as c:
            return [dict(r) for r in c.execute('SELECT * FROM %s' % table)]

    def nothing_created(self):
        return not list(Path(self.root).iterdir())

    # ── 检索链路与范围上下文 ─────────────────────────────────────────

    def ctx(self, **over):
        base = {'recipient_scope': 'private:1001',
                'allowed_source_scopes': ['private:1001'],
                'as_of_time': NOW, 'query_text': '她喜欢什么'}
        base.update(over)
        return ro.query_context(**base)

    def chain(self, ctx=None, recall_ctx=None, map_validity=False):
        """真实 R01→R02→R03→R04 链路。map_validity=True 时调用方按 M05
        账本状态收紧 items 的 validity（R04 契约：有效性登记由调用方负责）。"""
        fetched = ro.retrieve_evidence(ctx if ctx is not None else self.ctx())
        items = fetched['items']
        if map_validity:
            items = self.with_m05_validity(items)
        edges = ae.project_edges(items, fetched['degradation'])['edges']
        recalled = rs.recall(items, recall_ctx if recall_ctx is not None
                             else (ctx if ctx is not None else self.ctx()))
        expanded = rp.expand_paths(recalled['items'], edges, items=items,
                                   degradation=fetched['degradation'])
        return expanded, fetched, recalled

    def with_m05_validity(self, items):
        states = dag.read_invalidation_state()['states']
        out = []
        for item in items:
            row = dict(item)
            if (states.get(row['object_id']) or {}).get('status') in ('invalidated', 'superseded'):
                row['validity'] = 'invalidated'
            out.append(row)
        return out

    def seed_group_facts(self, entries, gid='g1'):
        path = Path(self.root, 'memory', 'group_facts.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'groups': {gid: {'entries': entries}}},
                                   ensure_ascii=False), encoding='utf-8')

    # ── 暂时锁夹具：WAL 库的写锁不挡读者，转回 DELETE 才能模拟 D07 的锁 ──

    def hold_exclusive_lock(self):
        path = Path(self.root, 'persona', 'cognition.db')
        with closing(sqlite3.connect(str(path))) as c:
            c.execute('PRAGMA journal_mode=DELETE')
        holder = sqlite3.connect(str(path), timeout=1)
        holder.execute('BEGIN EXCLUSIVE')
        self.addCleanup(holder.close)
        return holder

    def fast_sqlite(self):
        """把连接超时压到 0.2s：锁分型不变，只是不用干等 8s（D07 打桩同范式）。"""
        real = sqlite3.connect

        def fast(*args, **kwargs):
            kwargs['timeout'] = 0.2
            return real(*args, **kwargs)
        return patch.object(sqlite3, 'connect', side_effect=fast)

    # ── 情感账本（真实事件 → 只读桥 → 冻结重放）─────────────────────

    def emotion_ledger(self):
        e1 = self.seed_event(PRAISE, 'v03:emo:praise', ts=1000.0)
        e2 = self.seed_event(REBUKE, 'v03:emo:rebuke', ts=3000.0)
        inputs = rev.read_revision_inputs()
        ledger = rev.new_ledger()
        base = rev.build_base_run(inputs['events'], start_at=START, end_at=END, ts=5.0)
        rev.commit_base_run(ledger, base)
        return ledger, base, inputs, e1, e2

    def retract_emotion(self, ledger, parent_id, target, reason='主人撤回', ts=10.0):
        rec = dag.correction('observation_invalid', target, reason, ts=ts)
        plan = rev.plan_revision(ledger, parent_id, rec, ts=ts)
        return rec, rev.commit_revision(ledger, plan)

    # ── 矩阵用例执行：真实 runner → 执行器报告 → 断言分型命中 ────────

    def run_case(self, case_id, expected, runner):
        layer, fault = CASE_PLAN[case_id]
        report = rsm.run_matrix([rsm.case(case_id, layer, fault, expected, runner)])
        entry = report['cases'][0]
        self.assertEqual('pass', report['status'],
                         '%s 分型未命中: %s' % (case_id, json.dumps(
                             entry['mismatches'] or entry['error'], ensure_ascii=False)))
        return entry['observed']


# ── 认知层（A08）：撤回不改写原始观察，派生对象全部失效 ──────────────


class CognitionLayerTests(Base):
    def test_cog_retract_event_preserved(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:cog:1')
            before = sm.evidence_valid('event', eid)
            payload_before = sm.get_event(eid)['payload']
            sm.retract_evidence(eid, '主人纠正：那是引用')
            after = sm.evidence_valid('event', eid)
            row = sm.get_event(eid)
            again = sm.retract_evidence(eid, '主人纠正：那是引用')
            return {'before_valid': before, 'after_valid': after,
                    'event_row_preserved': row is not None,
                    'payload_unchanged': row['payload'] == payload_before,
                    'rerun_returns_true': again,
                    'retraction_rows': len(self.rows('evidence_retraction'))}
        self.run_case('cog_retract_event_preserved', {
            'before_valid': True, 'after_valid': False, 'event_row_preserved': True,
            'payload_unchanged': True, 'rerun_returns_true': True, 'retraction_rows': 1},
            runner)

    def test_cog_retract_derived_objects(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:cog:2')
            bel = self.seed_belief('她今天累了', [(eid, TIRED)])
            epi = sm.record_episode('一段经历', event_ids=[eid], ts=100.0)
            cmt = sm.add_commitment('1001', '答应的事', evidence=[eid], ts=100.0)
            view = sm.note_self_view('我是谁', evidence=[eid], ts=100.0)
            goal = sm.add_goal('目标', parent_event=eid, ts=100.0)
            act = sm.record_action('message', target_qq='1001', evidence=[eid], ts=100.0)
            objects = [('belief', bel), ('episode', epi), ('commitment', cmt),
                       ('self_view_rev', view), ('goal', goal), ('action', act)]
            before = all(sm.evidence_valid(k, oid) for k, oid in objects)
            sm.retract_evidence(eid, '主人撤回')
            after = all(not sm.evidence_valid(k, oid) for k, oid in objects)
            sm.retract_evidence(eid, '主人撤回')
            rerun = all(not sm.evidence_valid(k, oid) for k, oid in objects)
            belief_row = self.rows('belief')[0]
            goal_row = self.rows('goal')[0]
            return {'before_all_valid': before, 'after_all_invalid': after,
                    'rerun_all_invalid': rerun, 'belief_status': belief_row['status'],
                    'goal_status': goal_row['status']}
        self.run_case('cog_retract_derived_objects', {
            'before_all_valid': True, 'after_all_invalid': True,
            'rerun_all_invalid': True, 'belief_status': 'invalidated',
            'goal_status': 'invalidated'}, runner)

    def test_cog_retract_requires_reason(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:cog:3')
            try:
                sm.retract_evidence(eid, '   ')
                rejected = False
            except ValueError:
                rejected = True
            unknown = sm.retract_evidence('evt_never_existed', '理由')
            return {'empty_reason_rejected': rejected,
                    'unknown_event_returns_false': unknown is False,
                    'event_still_valid': sm.evidence_valid('event', eid)}
        self.run_case('cog_retract_requires_reason', {
            'empty_reason_rejected': True, 'unknown_event_returns_false': True,
            'event_still_valid': True}, runner)

    def test_cog_missing_ledger(self):
        def runner():
            # 缺库时保守判「无有效证据」，但绝不建库，也不冒充「她没有记忆」：
            # 缺席的出声报告由 der_missing_bridge / ret_missing_degraded 钉死。
            valid = sm.evidence_valid('event', 'evt_x')
            window = sm.source_window(['evt_x'])
            beliefs = sm.retrieval_beliefs()
            return {'evidence_valid_false': valid is False, 'window_empty': window == [],
                    'beliefs_empty': beliefs == [], 'nothing_created': self.nothing_created()}
        self.run_case('cog_missing_ledger', {
            'evidence_valid_false': True, 'window_empty': True,
            'beliefs_empty': True, 'nothing_created': True}, runner)

    def test_cog_lock_not_invalid(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:cog:lock')
            holder = self.hold_exclusive_lock()
            with self.fast_sqlite():
                try:
                    sm.evidence_valid('event', eid)
                    raised, message = '', ''
                except sqlite3.OperationalError as exc:
                    raised, message = type(exc).__name__, str(exc)
            holder.rollback()
            return {'under_lock_raises': raised,
                    'message_has_locked': 'locked' in message,
                    'after_release_valid': sm.evidence_valid('event', eid)}
        # 暂时锁不是损坏：必须显式撞锁出声，绝不能静默判成「证据无效」。
        self.run_case('cog_lock_not_invalid', {
            'under_lock_raises': 'OperationalError', 'message_has_locked': True,
            'after_release_valid': True}, runner)

    def test_cog_scope_truth_unchanged(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:cog:scope')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            before = sm.evidence_valid('belief', bel)
            proposition_before = self.rows('belief')[0]['proposition']
            self.commit([edge(bel, eid, 'belief')],
                        [dag.correction('scope_revoked', bel, '主人撤回授权',
                                        scope='group:55', ts=1.0)])
            row = self.rows('belief')[0]
            state = dag.read_invalidation_state()['states'][bel]
            return {'before_valid': before,
                    'after_valid': sm.evidence_valid('belief', bel),
                    'ledger_status': row['status'], 'm05_status': state['status'],
                    'proposition_unchanged': row['proposition'] == proposition_before}
        # 范围收紧只收授权，不改命题真假（M05 口径）：A08 有效性不动。
        self.run_case('cog_scope_truth_unchanged', {
            'before_valid': True, 'after_valid': True, 'ledger_status': 'active',
            'm05_status': 'scope_narrowed', 'proposition_unchanged': True}, runner)


# ── 派生层（M05）：传递失效、独立支持、四类修正、epoch 读门 ──────────


class DerivationLayerTests(Base):
    def test_der_retract_single_support(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:der:1')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            edges = [edge(bel, eid, 'belief')]
            projection = [{'object_id': bel, 'projection_epoch': 0}]
            state = dag.read_invalidation_state()
            kept_before, _ = dag.read_gate(projection, state['states'], state['epoch'])
            self.commit(edges, [dag.correction('observation_invalid', eid, '误读', ts=1.0)])
            state = dag.read_invalidation_state()
            entry = state['states'][bel]
            kept_after, dropped = dag.read_gate(projection, state['states'], state['epoch'])
            plan, _ = self.commit(edges, [dag.correction('observation_invalid', eid, '误读', ts=1.0)],
                                  states=state['states'])
            rerun = {e['object_id']: e['status'] for e in plan['affected']}
            return {'before_gate_kept': len(kept_before), 'after_status': entry['status'],
                    'reason_has_no_valid_support': 'no_valid_support' in entry['reasons'],
                    'event_preserved': sm.get_event(eid) is not None,
                    'after_gate_kept': len(kept_after), 'dropped_reason': sorted(dropped)[0],
                    'rerun_status': rerun[bel], 'epoch_advanced': state['epoch'] == 1}
        self.run_case('der_retract_single_support', {
            'before_gate_kept': 1, 'after_status': 'invalidated',
            'reason_has_no_valid_support': True, 'event_preserved': True,
            'after_gate_kept': 0, 'dropped_reason': 'invalidated',
            'rerun_status': 'invalidated', 'epoch_advanced': True}, runner)

    def test_der_retract_multi_hop(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:der:hop')
            bel = self.seed_belief('她累了', [(eid, TIRED)])
            edges = [edge(bel, eid, 'belief'), edge('sum_x', bel, 'summary'),
                     edge('cache:id', 'sum_x', 'identity_cache'),
                     edge('cache:ret', 'sum_x', 'retrieval_cache')]
            self.commit(edges, [dag.correction('observation_invalid', eid, '误读', ts=1.0)])
            state = dag.read_invalidation_state()
            statuses = {oid: state['states'][oid]['status']
                        for oid in (bel, 'sum_x', 'cache:id', 'cache:ret')}
            projection = [{'object_id': oid, 'projection_epoch': 0} for oid in statuses]
            kept, _ = dag.read_gate(projection, state['states'], state['epoch'])
            return {'all_invalidated': all(s == 'invalidated' for s in statuses.values()),
                    'affected_count': len(statuses), 'epoch': state['epoch'],
                    'pending_matches_states': (state['snapshots'][0]['payload']['pending_recompute']
                                               == sorted(state['states'])),
                    'old_projection_kept': len(kept)}
        self.run_case('der_retract_multi_hop', {
            'all_invalidated': True, 'affected_count': 4, 'epoch': 1,
            'pending_matches_states': True, 'old_projection_kept': 0}, runner)

    def test_der_independent_support(self):
        def runner():
            e1 = self.seed_event('我喜欢冰可乐', 'v03:der:ind1')
            e2 = self.seed_event('我常买冷饮', 'v03:der:ind2')
            bel = self.seed_belief('她喜欢冷饮', [(e1, '我喜欢冰可乐'), (e2, '我常买冷饮')])
            edges = [edge(bel, e1, 'belief'), edge(bel, e2, 'belief')]
            plan, _ = self.commit(edges, [dag.correction('observation_invalid', e1, '误读', ts=1.0)])
            entry = {e['object_id']: e for e in plan['affected']}[bel]
            state = dag.read_invalidation_state()
            kept, dropped = dag.read_gate([{'object_id': bel, 'projection_epoch': state['epoch']}],
                                          state['states'], state['epoch'])
            self.commit(edges, [dag.correction('observation_invalid', e2, '也误读', ts=2.0)],
                        states=state['states'])
            second = dag.read_invalidation_state()['states'][bel]['status']
            return {'status_after_first': entry['status'],
                    'independent': entry['independent_support'],
                    'remaining_is_e2': entry['remaining_supports'] == [e2],
                    'gate_kept': len(kept), 'gate_reason': sorted(dropped)[0],
                    'status_after_second': second}
        # 独立支持仍在 → needs_reassessment，绝不无脑恢复 active，也不得当已核验事实输出。
        self.run_case('der_independent_support', {
            'status_after_first': 'needs_reassessment', 'independent': True,
            'remaining_is_e2': True, 'gate_kept': 0, 'gate_reason': 'needs_reassessment',
            'status_after_second': 'invalidated'}, runner)

    def test_der_monotonic_rerun(self):
        def runner():
            e1 = self.seed_event('我喜欢冰可乐', 'v03:der:mono1')
            e2 = self.seed_event('我常买冷饮', 'v03:der:mono2')
            bel = self.seed_belief('她喜欢冷饮', [(e1, '我喜欢冰可乐'), (e2, '我常买冷饮')])
            edges = [edge(bel, e1, 'belief'), edge(bel, e2, 'belief')]
            self.commit(edges, [dag.correction('observation_invalid', e1, '误读', ts=1.0)])
            first = dag.read_invalidation_state()['states'][bel]['status']
            state = dag.read_invalidation_state()
            self.commit(edges, [dag.correction('observation_invalid', e2, '也误读', ts=2.0)],
                        states=state['states'])
            second = dag.read_invalidation_state()['states'][bel]['status']
            state = dag.read_invalidation_state()
            plan, _ = self.commit(edges, [dag.correction('observation_invalid', e2, '也误读', ts=2.0)],
                                  states=state['states'])
            rerun = {e['object_id']: e['status'] for e in plan['affected']}[bel]
            never_active = all(e['status'] != 'active' for e in plan['affected'])
            return {'first': first, 'second': second, 'rerun': rerun,
                    'never_active': never_active}
        self.run_case('der_monotonic_rerun', {
            'first': 'needs_reassessment', 'second': 'invalidated',
            'rerun': 'invalidated', 'never_active': True}, runner)

    def test_der_same_root_not_independent(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:der:root')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            edges = [edge('interp_1', eid), edge(bel, eid, 'belief'),
                     edge(bel, 'interp_1', 'belief')]
            plan, _ = self.commit(edges, [dag.correction('interpretation_revised', 'interp_1',
                                                         '解释错了', ts=1.0)])
            entry = {e['object_id']: e for e in plan['affected']}[bel]
            state = dag.read_invalidation_state()['states']
            return {'bel_status': entry['status'], 'independent': entry['independent_support'],
                    'reason': entry['reasons'][0], 'event_not_affected': eid not in state}
        # 同根源转述不算独立支持（§5.4）：剩余支持与失效解释同根，只登记不升级。
        self.run_case('der_same_root_not_independent', {
            'bel_status': 'needs_reassessment', 'independent': False,
            'reason': 'same_root_support_remains', 'event_not_affected': True}, runner)

    def test_der_interpretation_preserves_event(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:der:interp')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            edges = [edge('interp_1', eid), edge(bel, 'interp_1', 'belief'),
                     edge(bel, eid, 'belief')]
            self.commit(edges, [dag.correction('interpretation_revised', 'interp_1',
                                               '解释错了', ts=1.0)])
            state = dag.read_invalidation_state()['states']
            return {'interp_status': state['interp_1']['status'],
                    'interp_reason': 'original_event_preserved' in state['interp_1']['reasons'],
                    'event_in_affected': eid in state,
                    'remaining_is_real_event': state[bel]['remaining_supports'] == [eid],
                    'event_still_valid': sm.evidence_valid('event', eid)}
        self.run_case('der_interpretation_preserves_event', {
            'interp_status': 'invalidated', 'interp_reason': True,
            'event_in_affected': False, 'remaining_is_real_event': True,
            'event_still_valid': True}, runner)

    def test_der_belief_corrected(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:der:correct')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            edges = [edge(bel, eid, 'belief'), edge('sum_x', bel, 'summary')]
            self.commit(edges, [dag.correction('belief_corrected', bel, '口味变了',
                                               new_revision_id='bel_v2', ts=1.0)])
            state = dag.read_invalidation_state()['states']
            return {'old_status': state[bel]['status'],
                    'history_reason': 'history_preserved' in state[bel]['reasons'],
                    'summary_status': state['sum_x']['status'],
                    'summary_reason': state['sum_x']['reasons'][0],
                    'history_row_exists': any(r['id'] == bel for r in self.rows('belief')),
                    'new_revision_untouched': 'bel_v2' not in state}
        self.run_case('der_belief_corrected', {
            'old_status': 'superseded', 'history_reason': True,
            'summary_status': 'needs_reassessment',
            'summary_reason': 'support_superseded_by_revision',
            'history_row_exists': True, 'new_revision_untouched': True}, runner)

    def test_der_scope_narrowed_closure(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:der:scope')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            edges = [edge(bel, eid, 'belief'), edge('sum_x', bel, 'summary'),
                     edge('cache:ret', 'sum_x', 'retrieval_cache')]
            self.commit(edges, [dag.correction('scope_revoked', bel, '主人撤回授权',
                                               scope='group:55', ts=1.0)])
            state = dag.read_invalidation_state()
            oids = [bel, 'sum_x', 'cache:ret']
            statuses = {oid: state['states'][oid]['status'] for oid in oids}
            revoked = {oid: state['states'][oid]['revoked_scopes'] for oid in oids}
            projection = [{'object_id': oid, 'projection_epoch': state['epoch']} for oid in oids]
            kept_group, dropped_group = dag.read_gate(projection, state['states'],
                                                      state['epoch'], scope='group:55')
            kept_private, _ = dag.read_gate(projection, state['states'],
                                            state['epoch'], scope='private:1001')
            return {'statuses': statuses, 'revoked_scopes': revoked,
                    'gate_group_kept': len(kept_group),
                    'gate_group_reason': sorted(dropped_group)[0],
                    'gate_private_kept': len(kept_private),
                    'no_truth_lost': all(s not in ('invalidated', 'superseded')
                                         for s in statuses.values())}
        # X14：派生只能相同或更窄——整条闭包都记下被撤范围；真假不动。
        self.run_case('der_scope_narrowed_closure', {
            'gate_group_kept': 0, 'gate_group_reason': 'scope_revoked',
            'gate_private_kept': 3, 'no_truth_lost': True}, runner)
        state = dag.read_invalidation_state()['states']
        for entry in state.values():
            self.assertEqual('scope_narrowed', entry['status'])
            self.assertEqual(['group:55'], entry['revoked_scopes'])

    def test_der_stale_projection_gate(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:der:gate')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            projection = [{'object_id': bel, 'projection_epoch': 0}]
            state = dag.read_invalidation_state()
            kept_before, _ = dag.read_gate(projection, state['states'], state['epoch'])
            self.commit([edge(bel, eid, 'belief')],
                        [dag.correction('observation_invalid', eid, '误读', ts=1.0)])
            state = dag.read_invalidation_state()
            _, dropped_retracted = dag.read_gate(projection, state['states'], state['epoch'])
            _, dropped_stale = dag.read_gate([{'object_id': 'clean', 'projection_epoch': 0}],
                                             state['states'], state['epoch'])
            _, dropped_missing = dag.read_gate([{'object_id': 'clean'}],
                                               state['states'], state['epoch'])
            kept_fresh, _ = dag.read_gate([{'object_id': 'clean', 'projection_epoch': state['epoch']}],
                                          state['states'], state['epoch'])
            # epoch 0 会被 cache_key 当成缺分量拒绝（现有契约的真实边界缺陷，
            # 只报告不在此断言）；这里用两个合法 epoch 验证「同日推进 epoch 必须换 key」。
            key0 = dag.cache_key('r1', 's1', 1, 'private:1001')
            key1 = dag.cache_key('r1', 's1', 2, 'private:1001')
            return {'before_kept': len(kept_before),
                    'retracted_dropped': sorted(dropped_retracted)[0],
                    'stale_dropped': sorted(dropped_stale)[0],
                    'missing_epoch_dropped': sorted(dropped_missing)[0],
                    'fresh_kept': len(kept_fresh),
                    'cache_key_changed': key0['ok'] and key1['ok'] and key0['key'] != key1['key']}
        self.run_case('der_stale_projection_gate', {
            'before_kept': 1, 'retracted_dropped': 'invalidated',
            'stale_dropped': 'stale_projection', 'missing_epoch_dropped': 'missing_projection_epoch',
            'fresh_kept': 1, 'cache_key_changed': True}, runner)

    def test_der_missing_bridge(self):
        def runner():
            state = dag.read_invalidation_state()
            inputs = dag.read_derivation_inputs()
            reasons = {d['store']: d['reason'] for d in inputs['degradation']}
            return {'epoch_default': state['epoch'], 'edges_default': state['edges'],
                    'cognition_degradation': reasons.get('cognition'),
                    'degradation_reported': bool(inputs['degradation']),
                    'nothing_created': self.nothing_created()}
        # 缺数据≠零：缺席必须出声（absent），不能冒充「她没有记忆」。
        self.run_case('der_missing_bridge', {
            'epoch_default': 0, 'edges_default': [], 'cognition_degradation': 'absent',
            'degradation_reported': True, 'nothing_created': True}, runner)

    def test_der_lock_unreadable(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:der:lock')
            sm.write_invalidation_transaction({
                'ts': 1.0, 'expected_epoch': 0,
                'edges': [edge('cache_x', eid, 'retrieval_cache')],
                'corrections': [], 'propagation': None})
            holder = self.hold_exclusive_lock()
            with self.fast_sqlite():
                inputs = dag.read_derivation_inputs()
                reasons = {d['store']: d['reason'] for d in inputs['degradation']}
                try:
                    dag.read_invalidation_state()
                    crashed = False
                except Exception:
                    crashed = True
            holder.rollback()
            after = dag.read_derivation_inputs()
            after_reasons = {d['store']: d['reason'] for d in after['degradation']}
            return {'locked_reason': reasons.get('cognition'),
                    'not_absent': reasons.get('cognition') != 'absent',
                    'state_read_no_crash': not crashed,
                    'after_release_edges': len(after['edges']),
                    'after_release_readable': 'cognition' not in after_reasons}
        # 暂时锁≠损坏：读不动报 unreadable（不是 absent），释放后必须读回。
        # 注意：read_invalidation_state 撞锁时静默返回默认值、不带降级标记，
        # 这是现有契约的真实缺陷（只报告，不在此断言其为正确行为）。
        self.run_case('der_lock_unreadable', {
            'locked_reason': 'unreadable', 'not_absent': True,
            'state_read_no_crash': True, 'after_release_edges': 1,
            'after_release_readable': True}, runner)


# ── 情感层（S03）：撤源重算 0 误差、白名单、撤回单调、只追加 ─────────


class EmotionLayerTests(Base):
    def test_emo_retract_zero_error(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            before_processed = base['state']['processed']
            _, committed = self.retract_emotion(ledger, base['revision_id'], e1)
            never = rev.build_base_run([e for e in inputs['events'] if e['id'] != e1],
                                       start_at=START, end_at=END, ts=5.0)
            row = [r for r in committed['timeline'] if r['event_id'] == e1][0]
            archived = [e for e in base['events'] if e['id'] == e1][0]
            try:
                rec = dag.correction('observation_invalid', e1, '主人撤回', ts=10.0)
                rev.plan_revision(ledger, committed['revision_id'], rec, ts=12.0)
                rerun = ''
            except ValueError as exc:
                rerun = str(exc)
            return {'before_processed': before_processed,
                    'state_equals_never_existed': committed['state'] == never['state'],
                    'impulse_error': committed['checks']['impulse_accounting_error'],
                    'checks_ok': committed['checks']['ok'],
                    'retracted_rule': row['rule'],
                    'retracted_row_zero_delta': not any(row['delta'].values()),
                    'retracted_row_not_consumed': row['consumed'] is False,
                    'original_event_archived': archived['payload']['text'] == PRAISE,
                    'rerun_rejected': 'correction_already_applied' in rerun}
        self.run_case('emo_retract_zero_error', {
            'before_processed': 2, 'state_equals_never_existed': True,
            'impulse_error': 0.0, 'checks_ok': True, 'retracted_rule': 'retracted_source',
            'retracted_row_zero_delta': True, 'retracted_row_not_consumed': True,
            'original_event_archived': True, 'rerun_rejected': True}, runner)

    def test_emo_retract_after_replacement(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            rec = dag.correction('interpretation_revised', e1, '其实是引用', ts=9.0)
            plan = rev.plan_revision(ledger, base['revision_id'], rec,
                                     evidence_overrides={'quoted': True}, ts=9.0)
            replaced = rev.commit_revision(ledger, plan)
            _, committed = self.retract_emotion(ledger, replaced['revision_id'], e1, ts=10.0)
            never = rev.build_base_run([e for e in inputs['events'] if e['id'] != e1],
                                       start_at=START, end_at=END, ts=5.0)
            return {'state_equals_never_existed': committed['state'] == never['state'],
                    'chain_length': len(committed['corrections']),
                    'checks_ok': committed['checks']['ok']}
        # 撤源压过一切解释纠正：先替换后撤回，替换必须被弹出，终态仍逐位一致。
        self.run_case('emo_retract_after_replacement', {
            'state_equals_never_existed': True, 'chain_length': 2, 'checks_ok': True}, runner)

    def test_emo_retract_not_naive_delta(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            parent_state = dict(base['state'])
            row = [r for r in base['timeline'] if r['event_id'] == e1 and r['consumed']][0]
            naive_valence = parent_state['valence'] - row['delta']['valence']
            _, committed = self.retract_emotion(ledger, base['revision_id'], e1)
            never = rev.build_base_run([e for e in inputs['events'] if e['id'] != e1],
                                       start_at=START, end_at=END, ts=5.0)
            preserved = rev.get_revision(ledger, base['revision_id'])['state']
            return {'naive_subtraction_differs': committed['state']['valence'] != naive_valence,
                    'full_replay_matches_never': committed['state'] == never['state'],
                    'parent_state_preserved': preserved == parent_state}
        # 指数恢复是路径依赖的：父终态减 delta 不等于撤源重算，旧终态不得当重放初值。
        self.run_case('emo_retract_not_naive_delta', {
            'naive_subtraction_differs': True, 'full_replay_matches_never': True,
            'parent_state_preserved': True}, runner)

    def test_emo_append_only(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            rec, committed = self.retract_emotion(ledger, base['revision_id'], e1)
            parent_snapshot = rev.get_revision(ledger, base['revision_id'])
            try:
                rev.commit_revision(ledger, committed)
                duplicate = False
            except ValueError:
                duplicate = True
            tampered = json.loads(json.dumps(committed))
            tampered['state']['valence'] += 0.5
            try:
                rev.commit_revision(ledger, tampered)
                tamper = False
            except ValueError:
                tamper = True
            lineage = rev.revision_lineage(ledger, committed['revision_id'])
            return {'duplicate_rejected': duplicate, 'tamper_rejected': tamper,
                    'parent_unchanged': rev.get_revision(ledger, base['revision_id']) == parent_snapshot,
                    'lineage_correct': lineage == [base['revision_id'], committed['revision_id']]}
        self.run_case('emo_append_only', {
            'duplicate_rejected': True, 'tamper_rejected': True,
            'parent_unchanged': True, 'lineage_correct': True}, runner)

    def test_emo_interpretation_whitelist(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            rec = dag.correction('interpretation_revised', e1, '其实是引用', ts=9.0)
            plan = rev.plan_revision(ledger, base['revision_id'], rec,
                                     evidence_overrides={'quoted': True}, ts=9.0)
            committed = rev.commit_revision(ledger, plan)
            row = [r for r in committed['timeline'] if r['event_id'] == e1][0]
            archived = [e for e in base['events'] if e['id'] == e1][0]
            rejected = {}
            for name, overrides, kind in (
                    ('text', {'text': '改写原话'}, 'interpretation_revised'),
                    ('delta', {'valence': 0.9}, 'interpretation_revised'),
                    ('retract_with_overrides', {'quoted': True}, 'observation_invalid')):
                try:
                    bad = dag.correction(kind, e1, '坏纠正', ts=9.0)
                    rev.plan_revision(ledger, base['revision_id'], bad,
                                      evidence_overrides=overrides, ts=9.0)
                    rejected[name] = False
                except ValueError:
                    rejected[name] = True
            return {'replaced_valence_delta': row['delta']['valence'],
                    'action': row['revision_action'],
                    'original_appraisal_recorded': 'original_appraisal' in row,
                    'text_unchanged': archived['payload']['text'] == PRAISE,
                    'text_override_rejected': rejected['text'],
                    'delta_injection_rejected': rejected['delta'],
                    'retract_with_overrides_rejected': rejected['retract_with_overrides'],
                    'checks_ok': committed['checks']['ok']}
        self.run_case('emo_interpretation_whitelist', {
            'replaced_valence_delta': 0.0, 'action': 'replaced',
            'original_appraisal_recorded': True, 'text_unchanged': True,
            'text_override_rejected': True, 'delta_injection_rejected': True,
            'retract_with_overrides_rejected': True, 'checks_ok': True}, runner)

    def test_emo_retract_monotonic(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            _, committed = self.retract_emotion(ledger, base['revision_id'], e1)
            order_before = list(ledger['order'])
            try:
                rec = dag.correction('interpretation_revised', e1, '想洗回来', ts=11.0)
                rev.plan_revision(ledger, committed['revision_id'], rec,
                                  evidence_overrides={'quoted': False}, ts=11.0)
                message = ''
            except ValueError as exc:
                message = str(exc)
            return {'wash_back_rejected': 'target_already_retracted' in message,
                    'ledger_unchanged': ledger['order'] == order_before}
        # 撤回单调：解释纠正不能把已撤源洗回来（§5.4 反思不得制造经历）。
        self.run_case('emo_retract_monotonic', {
            'wash_back_rejected': True, 'ledger_unchanged': True}, runner)

    def test_emo_wrong_kind_rejected(self):
        def runner():
            ledger, base, inputs, e1, e2 = self.emotion_ledger()
            rejected = {}
            for kind, extra in (('belief_corrected', {'new_revision_id': 'bel_v2'}),
                                ('scope_revoked', {'scope': 'group:55'})):
                try:
                    rec = dag.correction(kind, e1, '不针对情感证据', ts=9.0, **extra)
                    rev.plan_revision(ledger, base['revision_id'], rec, ts=9.0)
                    rejected[kind] = False
                except ValueError:
                    rejected[kind] = True
            return {'belief_corrected_rejected': rejected['belief_corrected'],
                    'scope_revoked_rejected': rejected['scope_revoked']}
        self.run_case('emo_wrong_kind_rejected', {
            'belief_corrected_rejected': True, 'scope_revoked_rejected': True}, runner)

    def test_emo_missing_bridge(self):
        def runner():
            inputs = rev.read_revision_inputs()
            reasons = {d['store']: d['reason'] for d in inputs['degradation']}
            return {'events_empty': inputs['events'] == [],
                    'degradation': reasons.get('cognition'),
                    'nothing_created': self.nothing_created()}
        self.run_case('emo_missing_bridge', {
            'events_empty': True, 'degradation': 'absent', 'nothing_created': True}, runner)


# ── 检索层（R01–R04）：不复活、根源去重、范围硬过滤出声 ──────────────


class RetrievalLayerTests(Base):
    def test_ret_retract_belief_gone(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:1')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            payload_before = sm.get_event(eid)['payload']
            before = ro.retrieve_evidence(self.ctx())
            present_before = any(i['object_id'] == bel for i in before['items'])
            sm.retract_evidence(eid, '主人撤回')
            after = ro.retrieve_evidence(self.ctx())
            rerun = ro.retrieve_evidence(self.ctx())
            return {'before_present': present_before,
                    'after_present': any(i['object_id'] == bel for i in after['items']),
                    'rerun_present': any(i['object_id'] == bel for i in rerun['items']),
                    'event_text_unchanged': sm.get_event(eid)['payload'] == payload_before}
        self.run_case('ret_retract_belief_gone', {
            'before_present': True, 'after_present': False,
            'rerun_present': False, 'event_text_unchanged': True}, runner)

    def test_ret_retract_partial_support(self):
        def runner():
            e1 = self.seed_event('我喜欢冰可乐', 'v03:ret:p1')
            e2 = self.seed_event('我常买冷饮', 'v03:ret:p2')
            bel = self.seed_belief('她喜欢冷饮', [(e1, '我喜欢冰可乐'), (e2, '我常买冷饮')])
            before = ro.retrieve_evidence(self.ctx())
            roots_before = [i['root_source_ids'] for i in before['items']
                            if i['object_id'] == bel][0]
            sm.retract_evidence(e1, '主人撤回')
            after = ro.retrieve_evidence(self.ctx())
            belief_row = [r for r in self.rows('belief') if r['id'] == bel][0]
            return {'before_roots': len(roots_before),
                    'after_present': any(i['object_id'] == bel for i in after['items']),
                    'belief_status': belief_row['status'],
                    'other_event_still_valid': sm.evidence_valid('event', e2)}
        # A08 是保守口径：撤掉任一支持即整条信念失效（独立支持的细判在 M05
        # der_independent_support 验证）；检索层不得继续输出旧信念，另一源不连坐。
        self.run_case('ret_retract_partial_support', {
            'before_roots': 2, 'after_present': False,
            'belief_status': 'invalidated', 'other_event_still_valid': True}, runner)

    def test_ret_m05_invalidated_no_resurrect(self):
        def runner():
            ge1 = self.seed_event('我喜欢冰可乐', 'v03:ret:g1', session='group:g1',
                                  visibility='group', ts=1000.0, kind='group_message')
            ge2 = self.seed_event('又买冷饮', 'v03:ret:g2', session='group:g1',
                                  visibility='group', ts=999.0, kind='group_message')
            self.seed_group_facts([
                {'id': 'f1', 'text': '阿甲喜欢冰可乐', 'ts': 1001.0,
                 'source_event_ids': [ge1], 'status': 'active',
                 'evidence_spans': [{'event_id': ge1, 'quote': '我喜欢冰可乐',
                                     'subject_ref': 'per_1', 'subject_basis': 'source_actor'}]},
                {'id': 'f2', 'text': '阿甲常买冷饮', 'ts': 1000.0,
                 'source_event_ids': [ge2], 'status': 'active',
                 'evidence_spans': [{'event_id': ge2, 'quote': '又买冷饮',
                                     'subject_ref': 'per_1', 'subject_basis': 'source_actor'}]}])
            ctx = self.ctx(recipient_scope='group:g1', allowed_source_scopes=['group:g1'],
                           recent_referents=['阿甲'], query_text='阿甲喜欢什么')
            before, before_fetched, _ = self.chain(
                ctx=ctx, recall_ctx=dict(ctx, retrieval_budget=1))
            before_edges = ae.project_edges(before_fetched['items'],
                                            before_fetched['degradation'])['edges']
            # 依赖边来自真实只读桥（group.source_event_ids），不是手造图。
            edges = dag.read_derivation_inputs()['edges']
            self.commit(edges, [dag.correction('observation_invalid', ge2, '误读', ts=1.0)])
            # 调用方按 M05 账本收紧 validity 后重跑：R04 自己的失效防御必须挡住
            # 旧边指向的已失效邻居（R02 重投影也会挡，两层都验）。
            after_fetched = ro.retrieve_evidence(ctx)
            mapped = self.with_m05_validity(after_fetched['items'])
            recalled = rs.recall(mapped, dict(ctx, retrieval_budget=1))
            after = rp.expand_paths(recalled['items'], before_edges, items=mapped,
                                    degradation=after_fetched['degradation'])
            rerun = rp.expand_paths(recalled['items'], before_edges, items=mapped,
                                    degradation=after_fetched['degradation'])
            reprojection = ae.project_edges(mapped, after_fetched['degradation'])['edges']
            dropped = after['provenance']['dropped']
            f2 = 'group:g1:f2'
            return {'before_paths': len(before['paths']), 'after_paths': len(after['paths']),
                    'invalidated_drop': bool(dropped.get('invalidated_neighbor')
                                             or dropped.get('invalidated_seed')),
                    'r02_also_drops': not any(f2 in (e['a'], e['b']) for e in reprojection),
                    'f2_state': dag.read_invalidation_state()['states'][f2]['status'],
                    'rerun_paths': len(rerun['paths'])}
        self.run_case('ret_m05_invalidated_no_resurrect', {
            'before_paths': 1, 'after_paths': 0, 'invalidated_drop': True,
            'r02_also_drops': True, 'f2_state': 'invalidated', 'rerun_paths': 0}, runner)

    def test_ret_pool_seed_strictest(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:strict')
            self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            fetched = ro.retrieve_evidence(self.ctx())
            item = fetched['items'][0]
            seed_claims_valid = rp.expand_paths([dict(item, validity='valid')], [],
                                                items=[dict(item, validity='invalidated')])
            seed_claims_invalid = rp.expand_paths([dict(item, validity='invalidated')], [],
                                                  items=[dict(item, validity='valid')])
            return {'pool_stricter_wins': seed_claims_valid['provenance']['dropped'].get('invalidated_seed') == 1,
                    'seed_stricter_wins': seed_claims_invalid['provenance']['dropped'].get('invalidated_seed') == 1,
                    'paths': len(seed_claims_valid['paths']) + len(seed_claims_invalid['paths'])}
        # 两处登记不一致取最严：不给撤回对象留复活缝。
        self.run_case('ret_pool_seed_strictest', {
            'pool_stricter_wins': True, 'seed_stricter_wins': True, 'paths': 0}, runner)

    def test_ret_same_root_dedup(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:shared')
            b1 = self.seed_belief('命题一', [(eid, '我喜欢冰可乐')], dedupe_ts=1000.0)
            b2 = self.seed_belief('命题二', [(eid, '我喜欢冰可乐')], dedupe_ts=1001.0)
            before, _, recalled = self.chain(recall_ctx=self.ctx(retrieval_budget=1))
            seed_id = recalled['items'][0]['object_id']
            merged = [d for d in before['provenance']['deduped'] if d['reason'] == 'same_root']
            edges = dag.read_derivation_inputs()['edges']
            self.commit(edges, [dag.correction('observation_invalid', eid, '误读', ts=1.0)])
            after, _, _ = self.chain(recall_ctx=self.ctx(retrieval_budget=1), map_validity=True)
            return {'before_paths': len(before['paths']),
                    'dedup_reason': merged[0]['reason'] if merged else '',
                    'dedup_roots_is_eid': bool(merged) and merged[0]['root_source_ids'] == [eid],
                    'merged_into_seed': bool(merged) and merged[0]['merged_into'] == seed_id,
                    'after_candidates': after['provenance']['candidates'],
                    'after_paths': len(after['paths'])}
        # 同根源转述不算独立证据；根源撤回后两条派生一起失效，去重口径不塌。
        self.run_case('ret_same_root_dedup', {
            'before_paths': 0, 'dedup_reason': 'same_root', 'dedup_roots_is_eid': True,
            'merged_into_seed': True, 'after_candidates': 0, 'after_paths': 0}, runner)

    def test_ret_unknown_neighbor(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:unknown')
            self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            fetched = ro.retrieve_evidence(self.ctx())
            edges = ae.project_edges(fetched['items'], fetched['degradation'])['edges']
            # source_event 边的端点是原始事件，不在 R01 items 池里：详情未知。
            got = rp.expand_paths(fetched['items'], edges, items=fetched['items'])
            return {'paths': len(got['paths']),
                    'dropped': 'unknown_neighbor' in got['provenance']['dropped'],
                    'not_sufficient': got['status'] != 'sufficient'}
        # 缺数据≠零：核对不了有效性/根源的端点不扩散，原因码必须出声。
        # （零扩散时 status 落 not_found 而非 partial：原因码仍在 dropped 里，
        # 状态优先级是实现口径，这里只钉「不扩散+出声」，不钉状态优先级。）
        self.run_case('ret_unknown_neighbor', {
            'paths': 0, 'dropped': True, 'not_sufficient': True}, runner)

    def test_ret_private_to_group_blocked(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:leak')
            self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            allowed = ['private:1001']
            private = ro.retrieve_evidence(self.ctx(allowed_source_scopes=allowed))
            group = ro.retrieve_evidence(self.ctx(allowed_source_scopes=allowed,
                                                  recipient_scope='group:55'))
            return {'private_kept': len(private['items']), 'group_kept': len(group['items']),
                    'group_drop_reason': sorted(group['provenance']['filtered'])[0],
                    'allowed_unchanged': allowed == ['private:1001']}
        # 看见≠可转述：来源范围没变，只换受众就必须挡下并记原因码。
        self.run_case('ret_private_to_group_blocked', {
            'private_kept': 1, 'group_kept': 0, 'group_drop_reason': 'out_of_scope',
            'allowed_unchanged': True}, runner)

    def test_ret_scope_narrowing(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:narrow')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            wide = ro.retrieve_evidence(self.ctx(
                allowed_source_scopes=['private:1001', 'group:g1']))
            narrow = ro.retrieve_evidence(self.ctx(allowed_source_scopes=['group:g1']))
            return {'wide_kept': len(wide['items']), 'narrow_kept': len(narrow['items']),
                    'narrow_drop_reason': sorted(narrow['provenance']['filtered'])[0],
                    'truth_unchanged': sm.evidence_valid('belief', bel)}
        # 范围收紧不改命题真假：信念仍有效，只是读不到了。
        self.run_case('ret_scope_narrowing', {
            'wide_kept': 1, 'narrow_kept': 0, 'narrow_drop_reason': 'out_of_scope',
            'truth_unchanged': True}, runner)

    def test_ret_missing_degraded(self):
        def runner():
            got = ro.retrieve_evidence(self.ctx())
            reasons = {d['reason'] for d in got['degradation']}
            return {'status': got['status'], 'not_not_found': got['status'] != 'not_found',
                    'absent_reported': 'absent' in reasons,
                    'nothing_created': self.nothing_created()}
        self.run_case('ret_missing_degraded', {
            'status': 'degraded', 'not_not_found': True,
            'absent_reported': True, 'nothing_created': True}, runner)

    def test_ret_archive_grant(self):
        def runner():
            sm.add_belief('旧背景', subject_qq='1001', ts=900.0, is_legacy=True)
            ungranted = ro.retrieve_evidence(self.ctx())
            granted_private = ro.retrieve_evidence(self.ctx(
                allowed_source_scopes=['private:1001', 'cognition']))
            granted_group = ro.retrieve_evidence(self.ctx(
                allowed_source_scopes=['private:1001', 'cognition'],
                recipient_scope='group:55'))
            return {'ungranted_drop': sorted(ungranted['provenance']['filtered'])[0],
                    'granted_private_kept': len(granted_private['items']),
                    'granted_group_kept': len(granted_group['items']),
                    'group_drop': sorted(granted_group['provenance']['filtered'])[0]}
        # 档案显式授权可恢复访问（§7.5），但永远不给群受众。
        self.run_case('ret_archive_grant', {
            'ungranted_drop': 'archive_not_granted', 'granted_private_kept': 1,
            'granted_group_kept': 0, 'group_drop': 'archive_not_granted'}, runner)

    def test_ret_r04_scope_caller(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:r04scope')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            ge1 = self.seed_event('群里的话', 'v03:ret:r04g', session='group:g1',
                                  visibility='group', ts=1000.0, kind='group_message')
            self.seed_group_facts([
                {'id': 'f1', 'text': '阿甲喜欢冰可乐', 'ts': 1001.0,
                 'source_event_ids': [ge1], 'status': 'active',
                 'evidence_spans': [{'event_id': ge1, 'quote': '群里的话',
                                     'subject_ref': 'per_1', 'subject_basis': 'source_actor'}]}])
            wide = ro.retrieve_evidence(self.ctx(
                allowed_source_scopes=['private:1001', 'group:g1']))
            narrow_ctx = self.ctx(recipient_scope='group:g1',
                                  allowed_source_scopes=['group:g1'],
                                  recent_referents=['阿甲'], query_text='阿甲喜欢什么')
            narrow = ro.retrieve_evidence(narrow_ctx)
            edges = ae.project_edges(narrow['items'], narrow['degradation'])['edges']
            recalled = rs.recall(narrow['items'], narrow_ctx)
            got = rp.expand_paths(recalled['items'], edges, items=narrow['items'],
                                  degradation=narrow['degradation'])
            leaked = [p for p in got['paths'] if p['object_id'] == bel]
            seeds_leaked = [s for s in recalled['items'] if s['object_id'] == bel]
            return {'wide_items': len(wide['items']), 'narrow_kept': len(narrow['items']),
                    'leaked_paths': len(leaked), 'seeds_leaked': len(seeds_leaked)}
        # R04 不重做范围过滤（契约明示）：调用方先硬过滤，泄漏必须在进 R04 前被挡下。
        self.run_case('ret_r04_scope_caller', {
            'wide_items': 2, 'narrow_kept': 1, 'leaked_paths': 0, 'seeds_leaked': 0}, runner)

    def test_ret_lock_recovery(self):
        def runner():
            eid = self.seed_event('我喜欢冰可乐', 'v03:ret:lock')
            bel = self.seed_belief('她喜欢冰可乐', [(eid, '我喜欢冰可乐')])
            holder = self.hold_exclusive_lock()
            with self.fast_sqlite():
                try:
                    locked = ro.retrieve_evidence(self.ctx())
                    crashed, status = False, locked['status']
                except Exception:
                    crashed, status = True, ''
            holder.rollback()
            after = ro.retrieve_evidence(self.ctx())
            return {'under_lock_no_crash': not crashed,
                    'under_lock_not_sufficient': status != 'sufficient',
                    'after_release_present': any(i['object_id'] == bel for i in after['items'])}
        # 暂时锁≠损坏：撞锁不许冒充绿灯，释放后必须读回。
        # 注意：retrieval_beliefs 撞锁时静默返回空、R01 不给 cognition 降级标记，
        # 这是现有契约的真实缺陷（只报告，不在此断言其为正确行为）。
        self.run_case('ret_lock_recovery', {
            'under_lock_no_crash': True, 'under_lock_not_sufficient': True,
            'after_release_present': True}, runner)


# ── 情景层（M07）：撤回/失效源不复活，已失效 episode 单调 ────────────


class EpisodeLayerTests(Base):
    def turn(self, eid, text=TIRED):
        return {'ts': 1000.0, 'session': 'private-user-1001',
                'user': text, 'bot': '辛苦啦，早点休息', 'events': [eid]}

    def test_epi_retract_blocked_rerun(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:epi:1')
            first = pte.commit_turns([self.turn(eid)], sm.source_window([eid]), now=2000.0)
            epid = first['episodes'][0]['episode_id']
            before_valid = sm.evidence_valid('episode', epid)
            sm.retract_evidence(eid, '主人撤回')
            refrozen = sm.source_window([eid])
            again = pte.commit_turns([self.turn(eid)], refrozen, now=2500.0)
            return {'before_committed': first['committed'], 'before_valid': before_valid,
                    'refrozen_retracted': refrozen[0]['retracted'],
                    'after_blocked': again['blocked'], 'after_committed': again['committed'],
                    'reason': again['episodes'][0]['propositions'][0]['reasons'][0],
                    'episode_count': len(self.rows('episode')),
                    'event_preserved': sm.get_event(eid) is not None}
        self.run_case('epi_retract_blocked_rerun', {
            'before_committed': 1, 'before_valid': True, 'refrozen_retracted': True,
            'after_blocked': 1, 'after_committed': 0, 'reason': 'source_retracted',
            'episode_count': 1, 'event_preserved': True}, runner)

    def test_epi_m05_source_blocked(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:epi:m05')
            self.commit([], [dag.correction('observation_invalid', eid, '误读', ts=1.0)])
            got = pte.commit_turns([self.turn(eid)], sm.source_window([eid]), now=2000.0)
            prop = got['episodes'][0]['propositions'][0]
            return {'blocked': got['blocked'], 'committed': got['committed'],
                    'prop_provenance': prop['provenance'],
                    'prop_reason': 'source_invalidated' in prop['reasons'],
                    'episode_rows': len(self.rows('episode'))}
        self.run_case('epi_m05_source_blocked', {
            'blocked': 1, 'committed': 0, 'prop_provenance': 'invalidated',
            'prop_reason': True, 'episode_rows': 0}, runner)

    def test_epi_self_invalidation_monotonic(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:epi:mono')
            first = pte.commit_turns([self.turn(eid)], sm.source_window([eid]), now=2000.0)
            epid = first['episodes'][0]['episode_id']
            edges = dag.read_invalidation_state()['edges']
            self.commit(edges, [dag.correction('observation_invalid', eid, '误读', ts=1.0)])
            again = pte.commit_turns([self.turn(eid)], sm.source_window([eid]), now=2500.0)
            state = dag.read_invalidation_state()['states']
            return {'rerun_blocked': again['blocked'],
                    'reason': again['episodes'][0]['reasons'][0],
                    'episode_count': len(self.rows('episode')),
                    'episode_state': state[epid]['status']}
        # 源撤回沿依赖边失效 episode 自身；重复情景化只会被单调挡下。
        self.run_case('epi_self_invalidation_monotonic', {
            'rerun_blocked': 1, 'reason': 'prior_invalidation_preserved',
            'episode_count': 1, 'episode_state': 'invalidated'}, runner)

    def test_epi_missing_journal(self):
        def runner():
            got = pte.read_private_turns()
            reasons = {d['store']: d['reason'] for d in got['degradation']}
            window = pte.freeze_window([self.turn('evt_x')])
            return {'degradation': reasons.get('journal'), 'turns_empty': got['turns'] == [],
                    'window_empty': window == [], 'nothing_created': self.nothing_created()}
        self.run_case('epi_missing_journal', {
            'degradation': 'absent', 'turns_empty': True,
            'window_empty': True, 'nothing_created': True}, runner)

    def test_epi_interpretation_candidate_only(self):
        def runner():
            eid = self.seed_event(TIRED, 'v03:epi:interp')
            raw = self.turn(eid)
            ident = pte.turn_identity(raw)['object_id']
            inter = {ident: '她可能累了，需要安慰'}
            first = pte.commit_turns([raw], sm.source_window([eid]), now=2000.0,
                                     interpretations=inter)
            entry = first['episodes'][0]
            summary = self.rows('episode')[0]['summary']
            candidates_before = len(self.rows('claim_candidate'))
            sm.retract_evidence(eid, '主人撤回')
            again = pte.commit_turns([raw], sm.source_window([eid]), now=2500.0,
                                     interpretations=inter)
            return {'interpretation_decision': entry['interpretation']['decision'],
                    'summary_excludes_interpretation': '需要安慰' not in summary,
                    'belief_rows': len(self.rows('belief')),
                    'rerun_blocked': again['blocked'],
                    'candidate_count_unchanged': len(self.rows('claim_candidate')) == candidates_before}
        # LLM 解释只是候选：不写事实、不进摘要；撤回后重跑也不复活。
        self.run_case('epi_interpretation_candidate_only', {
            'interpretation_decision': 'quarantine', 'summary_excludes_interpretation': True,
            'belief_rows': 0, 'rerun_blocked': 1, 'candidate_count_unchanged': True}, runner)


# ── 执行器自身：分型口径、确定性、如实报告 ───────────────────────────


class ExecutorTests(unittest.TestCase):
    def spec(self, case_id='c1', expected=None, runner=None):
        return rsm.case(case_id, 'cognition', 'retract_source',
                        {'ok': True} if expected is None else expected,
                        (lambda: {'ok': True}) if runner is None else runner)

    def test_report_shape_and_pass(self):
        report = rsm.run_matrix([self.spec()])
        self.assertEqual('pass', report['status'])
        self.assertEqual({'status', 'cases', 'provenance', 'limitations'}, set(report))
        entry = report['cases'][0]
        self.assertEqual({'case_id', 'layer', 'fault', 'status', 'expected',
                          'observed', 'mismatches', 'error'}, set(entry))
        for key in ('algorithm_version', 'method', 'cases', 'passed', 'failed',
                    'errored', 'layers', 'faults', 'llm_reinterpretation',
                    'network', 'production_ledger'):
            self.assertIn(key, report['provenance'])
        self.assertFalse(report['provenance']['production_ledger'])
        self.assertTrue(report['limitations'])

    def test_fail_reports_mismatch_not_pass(self):
        report = rsm.run_matrix([self.spec(expected={'ok': False})])
        self.assertEqual('fail', report['status'])
        mismatch = report['cases'][0]['mismatches'][0]
        self.assertEqual('ok', mismatch['key'])
        self.assertIs(False, mismatch['expected'])
        self.assertIs(True, mismatch['observed'])
        self.assertTrue(mismatch['present'])

    def test_missing_observation_key_is_fail_not_default_true(self):
        report = rsm.run_matrix([self.spec(runner=lambda: {})])
        self.assertEqual('fail', report['status'])
        self.assertFalse(report['cases'][0]['mismatches'][0]['present'])

    def test_runner_exception_is_error_not_pass(self):
        def boom():
            raise RuntimeError('契约撞锁了')
        report = rsm.run_matrix([self.spec(runner=boom)])
        self.assertEqual('error', report['status'])
        self.assertIn('RuntimeError', report['cases'][0]['error'])
        self.assertIn('契约撞锁了', report['cases'][0]['error'])

    def test_non_dict_observation_is_error(self):
        report = rsm.run_matrix([self.spec(runner=lambda: ['不是观察'])])
        self.assertEqual('error', report['status'])

    def test_deterministic_order_and_no_clock(self):
        specs = [self.spec('z_case'), self.spec('a_case')]
        one = rsm.run_matrix(specs)
        two = rsm.run_matrix(list(reversed(specs)))
        self.assertEqual(json.dumps(one, sort_keys=True, ensure_ascii=False),
                         json.dumps(two, sort_keys=True, ensure_ascii=False))
        self.assertEqual(['a_case', 'z_case'], [c['case_id'] for c in one['cases']])
        self.assertNotIn('ts', one['provenance'])
        self.assertNotIn('seconds', one['provenance'])

    def test_bad_specs_are_rejected_loudly(self):
        with self.assertRaises(ValueError):
            rsm.run_matrix([])
        with self.assertRaises(ValueError):
            rsm.run_matrix([self.spec('dup'), self.spec('dup')])
        with self.assertRaises(ValueError):
            rsm.case('c', 'no_such_layer', 'retract_source', {'ok': True}, lambda: {})
        with self.assertRaises(ValueError):
            rsm.case('c', 'cognition', 'no_such_fault', {'ok': True}, lambda: {})
        with self.assertRaises(ValueError):
            rsm.case('c', 'cognition', 'retract_source', {}, lambda: {})
        with self.assertRaises(ValueError):
            rsm.case('c', 'cognition', 'retract_source', {'ok': True}, '不是 callable')

    def test_limitations_declare_what_is_not_covered(self):
        limits = ' '.join(rsm.LIMITATIONS)
        self.assertIn('C04', limits)
        self.assertIn('ExpressionPlan', limits)
        self.assertIn('生产', limits)
        self.assertIn('S06', limits)

    def test_coverage_counts_intersections(self):
        got = rsm.coverage([self.spec('a'), self.spec('b')])
        self.assertEqual(2, got['cases'])
        self.assertEqual({'cognition/retract_source': 2}, got['intersections'])


class ExecutorStaticLockTests(unittest.TestCase):
    """AST 锁：执行器必须纯编排——不 import、不读写、不冻路径。"""

    @classmethod
    def setUpClass(cls):
        cls.source = Path(BOT, 'retraction_scope_matrix.py').read_text(encoding='utf-8')
        cls.tree = ast.parse(cls.source)

    def test_it_imports_nothing(self):
        for node in ast.walk(self.tree):
            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom),
                                     '执行器 import 了库；它必须纯编排，副作用全在 runner')

    def test_no_write_sql_or_file_calls(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ', 'ALTER '):
                    self.assertNotIn(verb, upper)
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else '')
                self.assertNotIn(name, ('open', 'connect', 'getenv', 'mkdir', 'makedirs',
                                        'write_text', 'write_bytes', 'commit', 'remove'))

    def test_axes_are_fixed(self):
        self.assertEqual(('cognition', 'derivation', 'emotion', 'retrieval', 'episode'),
                         rsm.LAYERS)
        self.assertIn('transient_lock', rsm.FAULTS)
        self.assertIn('cross_scope_leak', rsm.FAULTS)


class ZCoverageTests(unittest.TestCase):
    """覆盖审计：声明的每个 层×故障 交叉点都有真实用例，且用例都真执行过。"""

    def test_matrix_covers_every_declared_intersection(self):
        self.assertGreaterEqual(len(CASE_PLAN), 35)
        self.assertEqual(INTERSECTIONS, set(CASE_PLAN.values()),
                         '声明的交叉点与用例登记不一致')

    def test_every_planned_case_has_a_real_test_method(self):
        test_classes = [obj for _, obj in globals().items()
                        if isinstance(obj, type) and issubclass(obj, unittest.TestCase)]
        methods = {name for cls in test_classes for name in dir(cls)
                   if name.startswith('test_')}
        for case_id in CASE_PLAN:
            self.assertIn('test_' + case_id, methods, '用例 %s 没有真实测试' % case_id)

    def test_case_ids_are_unique_and_on_the_axes(self):
        self.assertEqual(len(CASE_PLAN), len(set(CASE_PLAN)))
        for case_id, (layer, fault) in CASE_PLAN.items():
            self.assertIn(layer, rsm.LAYERS, case_id)
            self.assertIn(fault, rsm.FAULTS, case_id)


if __name__ == '__main__':
    unittest.main()