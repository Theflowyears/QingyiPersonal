# -*- coding: utf-8 -*-
"""S06 迟到 × 重启 × 同刻 × 撤回 回放矩阵（emotion 轨，路线 §4.3 / §9）。

S03 把这两件事显式推迟给了本模块（emotion_revision.py 模块 docstring「迟到/
重启回放矩阵是 S06」、LIMITATIONS「迟到/重启/同刻回放矩阵属 S06」）。S06 接住
它们：对**已记录的事件与评估**，在冻结的 S03/emotion_core 重放之上，跨四类
条件做确定性的「前 → 施加条件 → 重跑」对账，回答「时间/重启/同刻/撤回会不会
让她的状态失去连续性」：

  迟到 late          有效时间(effective_ts)落在到达顺序之外的事件，必须按
                     effective_ts 而非到达 ts 着陆；时间线不倒退；输入顺序
                     无关；重跑逐位一致。
  重启 restart       检查点只存「事件 + 窗口 + 内核 + tick」，不存终态：重启
                     从事件全量重放，终态/时间线/内容寻址 ID 与重启前逐位一致
                     （指数恢复路径依赖，终态不能当重放初值，§4.3）。
  同刻 simultaneous  同一 effective_ts 的并列事件，按冻结键 source_seq >
                     input_seq > 输入索引 决序，再按 id 稳定排序；同 ID 只取
                     排序后首条。本模块不写第二套顺序规则，决序完全委托
                     emotion_core._replay_key。
  撤回 retraction    observation_invalid 撤源后终态与「该事件从未存在」逐位
                     一致、冲击对账 0 误差；原事件仍可审计；撤回单调，后续
                     解释纠正洗不回来。

硬边界（与 S03/V03 同一范式）：
- 只复用冻结原语：动力学/恢复/界全部来自 emotion_core(shadow-v1) 与
  emotion_dynamics(shadow-v2)（经 emotion_revision）；排序/去重/窗口口径来自
  emotion_core.replay。本模块不自带 exp/tau，不写第二套顺序键，不调 LLM 重新
  解释原话，不读写存储、不解析路径、不建库建表建目录、不发网络。
- 确定性：用例按 case_id 排序，provenance 不含时钟；同输入同报告。检查点是
  内存 JSON 串，重跑用深拷贝事件，墙上时钟不进状态/ID。
- 影子边界：expression_control 恒 False；counts_for_live 由 S01 契约盖章
  （v2→historical_replay、v1→v1_reference，均不计在线）；候选语义标签不参与
  重放，绝不驱动实时状态。
- 如实分型（D07/V03 口径）：runner 抛异常记 error，不冒充 pass；期望与观察
  不一致记 fail 并逐键列出 mismatch；缺观察键不算默认为真。limitations 如实
  声明没覆盖什么，不假装矩阵证明了生产合规或主观体验。

矩阵的真实执行在 tests/test_emotion_replay_matrix.py：每个用例对真实冻结契约
做「前→施加条件→重跑」。本模块提供可复用的编排（case/run_matrix/coverage）、
四个条件的确定性场景引擎（late_arrival/restart/simultaneous/retraction，全部
委托冻结原语），以及整张交叉矩阵入口 run_condition_matrix。
"""
import copy
import json

import emotion_core as legacy
import emotion_revision as rev
import emotion_trajectories as trajectories
import invalidation_dag as dag

ALGORITHM_VERSION = 's06-1'
# 矩阵的两根轴：条件 = 注入的时间/重启/同刻/撤回故障；内核 = 被对账的冻结轨迹。
CONDITIONS = ('late', 'restart', 'simultaneous', 'retraction')
KERNELS = ('shadow-v1', 'shadow-v2')
DEFAULT_KERNEL = rev.DEFAULT_KERNEL
DIMENSIONS = rev.DIMENSIONS

LIMITATIONS = (
    '只证冻结重放在隔离合成夹具上的时间/重启/同刻/撤回连续性，不证生产账本已合规；'
    '生产审计走 phase_audit 只读快照。',
    '动力学/恢复/界/排序全部复用 emotion_core 与 emotion_revision 冻结原语；'
    '本模块不自带第二套 exp/tau 动力学，也不写第二套回放顺序键。',
    '不调 LLM 重新解释原话；随事件携带的已记录评估必须与冻结规则一致，否则大声拒绝。',
    '影子边界：expression_control 恒 False，counts_for_live 由 S01 契约盖章不计在线；'
    '候选语义标签(integrated_appraisal)不参与重放，绝不驱动实时状态。',
    '候选回放性能与脱敏读取属 S07，记忆→情绪隔离实验属 S08，表达接管(C04)未落地，'
    '都不在本矩阵。',
    '检查点是调用方持有的内存对象；重启持久化需要 self_model 新增事务入口，本模块不建库建表。',
    '合成夹具的时间是构造顺序，不能解释成真实情绪持续时间；通过回放只排除明显异常，'
    '不能替代真实流量观测或证明主观体验。',
)


# ── 冻结顺序的唯一复用点：决序完全委托 emotion_core._replay_key ──────────


def _frozen_order(events):
    """按冻结键给事件决序（复用 emotion_core._replay_key，不另写顺序规则）。

    仅用于把「按有效时间 chronological 重放」作为迟到/同刻的对照参考；
    真正的状态重放仍由 emotion_revision/emotion_core 完成。
    """
    keyed = []
    for index, value in enumerate(events):
        header = legacy._event_header(value)
        keyed.append((legacy._replay_key(header, value, index), value))
    keyed.sort(key=lambda item: item[0])
    return [value for _, value in keyed]


def _boundary(record):
    """从冻结记录读取 S01 影子口径；本模块不改写它，只如实 surfaced。"""
    # 比较式回读：expression_control 全局静态锁不许用动态值开启表达控制。
    return {'expression_control': record['expression_control'] is True,
            'counts_for_live': record['counts_for_live'],
            'trajectory': record['trajectory']}


def _base(events, start_at, end_at, kernel, tick_seconds=None, ts=None):
    return rev.build_base_run(events, start_at=start_at, end_at=end_at,
                              kernel=kernel, tick_seconds=tick_seconds, ts=ts)


# ── 四个条件的确定性场景引擎（前 → 施加条件 → 重跑，全部委托冻结原语）──


def late_arrival(spec, *, start_at, end_at, kernel, ts=None):
    """迟到：effective_ts 落在到达顺序之外。返回观察分型。

    前 = 按 effective_ts 的冻结重放；重跑 = 深拷贝再放（幂等）；对照 = 按冻结键
    chronological 决序后重放。断言迟到事件按 effective_ts 着陆、时间线不倒退、
    输入顺序无关、终态与 chronological 对照逐位一致。
    """
    events = spec['events']
    late_id = spec['late_id']
    after = _base(events, start_at, end_at, kernel, ts=ts)
    rerun = _base(copy.deepcopy(events), start_at, end_at, kernel, ts=ts)
    reference = _base(_frozen_order(events), start_at, end_at, kernel, ts=ts)
    reversed_run = _base(list(reversed(events)), start_at, end_at, kernel, ts=ts)
    late_event = next(e for e in events if e['id'] == late_id)
    effective = late_event.get('effective_ts', late_event['ts'])
    row = next(r for r in after['timeline'] if r['event_id'] == late_id)
    times = [r['at'] for r in after['timeline']]
    out = {
        'timeline_order': [r['event_id'] for r in after['timeline']],
        'state_equals_chronological': after['state'] == reference['state'],
        'late_lands_at_effective_ts': row['at'] == effective,
        'late_not_at_arrival_ts': row['at'] != late_event['ts'],
        'timeline_non_decreasing': times == sorted(times),
        'rerun_identical': after['state'] == rerun['state'] and after['timeline'] == rerun['timeline'],
        'input_order_independent': after['state'] == reversed_run['state'],
        'checks_ok': after['checks']['ok'],
        'impulse_error': after['checks']['impulse_accounting_error'],
    }
    out.update(_boundary(after))
    return out


def restart(spec, *, start_at, end_at, kernel, ts=None):
    """重启：检查点只存事件+窗口+内核+tick，从事件全量重放。返回观察分型。

    前 = 提交的基础 run；检查点 = 内存 JSON（不含终态）；重启 = 新账本从检查点
    事件重建。断言终态/时间线/内容寻址 ID 逐位一致、重跑幂等；若给了 retract_id，
    再断言重启后重放撤回修订仍逐位一致（修订链可跨重启重建）。
    """
    events = spec['events']
    tick_seconds = spec.get('tick_seconds')
    ledger = rev.new_ledger()
    before = _base(events, start_at, end_at, kernel, tick_seconds=tick_seconds, ts=ts)
    rev.commit_base_run(ledger, before)
    checkpoint = json.dumps(
        {'events': before['events'], 'start_at': before['start_at'],
         'end_at': before['end_at'], 'kernel_version': before['kernel_version'],
         'tick_seconds': before['tick_seconds']},
        sort_keys=True, ensure_ascii=False, default=str)
    restored = json.loads(checkpoint)
    fresh = rev.new_ledger()
    rebuilt = _base(restored['events'], restored['start_at'], restored['end_at'],
                    restored['kernel_version'], tick_seconds=restored['tick_seconds'], ts=ts)
    rev.commit_base_run(fresh, rebuilt)
    again = _base(copy.deepcopy(restored['events']), restored['start_at'], restored['end_at'],
                  restored['kernel_version'], tick_seconds=restored['tick_seconds'], ts=ts)
    out = {
        'checkpoint_stores_events_not_state': 'state' not in restored and 'events' in restored,
        'terminal_bit_identical': rebuilt['state'] == before['state'],
        'timeline_bit_identical': rebuilt['timeline'] == before['timeline'],
        'revision_id_identical': rebuilt['revision_id'] == before['revision_id'],
        'rerun_idempotent': again['state'] == rebuilt['state'] and again['timeline'] == rebuilt['timeline'],
        'checks_ok': rebuilt['checks']['ok'],
    }
    target = spec.get('retract_id')
    if target:
        reason = spec.get('retract_reason', '重启前撤回')
        rec = dag.correction('observation_invalid', target, reason, ts=ts)
        committed = rev.commit_revision(ledger, rev.plan_revision(
            ledger, before['revision_id'], rec, ts=ts))
        rec2 = dag.correction('observation_invalid', target, reason, ts=ts)
        committed2 = rev.commit_revision(fresh, rev.plan_revision(
            fresh, rebuilt['revision_id'], rec2, ts=ts))
        out['revision_terminal_bit_identical'] = committed2['state'] == committed['state']
        out['revision_id_identical_after_restart'] = committed2['revision_id'] == committed['revision_id']
        out['revision_checks_ok'] = committed2['checks']['ok']
    out.update(_boundary(rebuilt))
    return out


def simultaneous(spec, *, start_at, end_at, kernel, ts=None):
    """同刻：同一 effective_ts 的并列事件按冻结键决序。返回观察分型。

    断言时间线顺序与冻结键(_frozen_order)一致、与显式期望一致、重跑逐位一致。
    决序完全来自 emotion_core._replay_key，本引擎不写第二套顺序。
    """
    events = spec['events']
    run = _base(events, start_at, end_at, kernel, ts=ts)
    rerun = _base(copy.deepcopy(events), start_at, end_at, kernel, ts=ts)
    order = [r['event_id'] for r in run['timeline']]
    frozen_order = [e['id'] for e in _frozen_order(events)]
    out = {
        'timeline_order': order,
        'matches_frozen_key': order == frozen_order,
        'matches_expected': order == spec['expected_order'],
        'processed': run['state']['processed'],
        'rerun_identical': run['state'] == rerun['state'] and run['timeline'] == rerun['timeline'],
        'checks_ok': run['checks']['ok'],
    }
    out.update(_boundary(run))
    return out


def retraction(spec, *, start_at, end_at, kernel, ts=None):
    """撤回：observation_invalid 撤源。返回观察分型。

    前 = 提交的基础 run；施加条件 = 撤源修正 → 修订重放；重跑 = 「该事件从未
    存在」的基础重放。断言终态逐位一致、冲击对账 0 误差、原事件仍可审计、
    撤回单调（后续解释纠正被 target_already_retracted 拒绝）。
    """
    events = spec['events']
    target = spec['target_id']
    ledger = rev.new_ledger()
    before = _base(events, start_at, end_at, kernel, ts=ts)
    rev.commit_base_run(ledger, before)
    rec = dag.correction('observation_invalid', target, spec.get('reason', '主人撤回'), ts=ts)
    committed = rev.commit_revision(ledger, rev.plan_revision(
        ledger, before['revision_id'], rec, ts=ts))
    never = _base([e for e in events if e['id'] != target], start_at, end_at, kernel, ts=ts)
    row = next(r for r in committed['timeline'] if r['event_id'] == target)
    archived = next(e for e in before['events'] if e['id'] == target)
    original = next(e for e in events if e['id'] == target)
    wash = ''
    try:
        rec2 = dag.correction('interpretation_revised', target, '想洗回来', ts=ts)
        rev.plan_revision(ledger, committed['revision_id'], rec2,
                          evidence_overrides={'quoted': False}, ts=ts)
    except ValueError as exc:
        wash = str(exc)
    out = {
        'state_equals_never_existed': committed['state'] == never['state'],
        'impulse_error': committed['checks']['impulse_accounting_error'],
        'checks_ok': committed['checks']['ok'],
        'retracted_rule': row['rule'],
        'retracted_zero_delta': not any(row['delta'].values()),
        'retracted_not_consumed': row['consumed'] is False,
        'original_event_archived': archived == original,
        'wash_back_rejected': 'target_already_retracted' in wash,
        'parent_state_preserved': rev.get_revision(ledger, before['revision_id'])['state'] == before['state'],
    }
    out.update(_boundary(committed))
    return out


ENGINES = {'late': late_arrival, 'restart': restart,
           'simultaneous': simultaneous, 'retraction': retraction}


# ── 纯编排：登记 / 执行 / 覆盖统计（与 V03 同范式，轴换成 条件×内核）────


def case(case_id, condition, kernel, expected, runner):
    """登记一个矩阵用例。条件/内核必须在轴上，期望分型必须显式非空。

    expected 是「观察键 → 期望值」的显式声明；runner 是零参可调用，返回观察
    dict，其全部副作用必须落在调用方准备好的隔离内存账本里，绝不指向生产 data/。
    """
    cid = str(case_id or '').strip()
    if not cid:
        raise ValueError('case requires case_id')
    if condition not in CONDITIONS:
        raise ValueError('unknown matrix condition: %s' % condition)
    if kernel not in KERNELS:
        raise ValueError('unknown matrix kernel: %s' % kernel)
    if not isinstance(expected, dict) or not expected:
        raise ValueError('case requires nonempty explicit expected classification')
    if not callable(runner):
        raise ValueError('case requires a callable runner')
    return {'case_id': cid, 'condition': condition, 'kernel': kernel,
            'expected': dict(expected), 'runner': runner}


def coverage(cases):
    """纯统计：已覆盖的 条件×内核 交叉点计数（'condition/kernel' → n）。"""
    counts = {}
    for spec in cases or []:
        key = '%s/%s' % (spec['condition'], spec['kernel'])
        counts[key] = counts.get(key, 0) + 1
    return {'cases': len(cases or []), 'intersections': counts}


def run_matrix(cases):
    """确定性执行一组用例，产出可解释报告。不读写任何账本。

    分型口径：pass=全部分型命中；fail=有期望/观察不一致；error=有 runner 抛
    异常（类型与消息如实记录，不吞）。空矩阵直接拒绝——没有用例的「全绿」是
    假绿灯。provenance 不含时钟，同输入同输出。报告整体盖影子口径：
    expression_control=False、counts_for_live=False、trajectory=historical_replay。
    """
    specs = list(cases or [])
    if not specs:
        raise ValueError('matrix carries no case; an empty green is a fake green')
    seen = set()
    for spec in specs:
        if not isinstance(spec, dict) or not {'case_id', 'condition', 'kernel',
                                              'expected', 'runner'} <= set(spec):
            raise ValueError('matrix case must come from case()')
        if spec['case_id'] in seen:
            raise ValueError('duplicate case_id: %s' % spec['case_id'])
        seen.add(spec['case_id'])
    results = []
    for spec in sorted(specs, key=lambda s: s['case_id']):
        entry = {'case_id': spec['case_id'], 'condition': spec['condition'],
                 'kernel': spec['kernel'], 'status': 'pass',
                 'expected': dict(spec['expected']), 'observed': {},
                 'mismatches': [], 'error': ''}
        try:
            observed = spec['runner']()
        except Exception as exc:
            entry['status'] = 'error'
            entry['error'] = '%s: %s' % (type(exc).__name__, exc)
            results.append(entry)
            continue
        if not isinstance(observed, dict):
            entry['status'] = 'error'
            entry['error'] = 'runner must return an observation dict'
            results.append(entry)
            continue
        entry['observed'] = observed
        for key in sorted(spec['expected']):
            want = spec['expected'][key]
            if key not in observed:
                entry['mismatches'].append({'key': key, 'expected': want,
                                            'observed': None, 'present': False})
            elif observed[key] != want:
                entry['mismatches'].append({'key': key, 'expected': want,
                                            'observed': observed[key], 'present': True})
        if entry['mismatches']:
            entry['status'] = 'fail'
        results.append(entry)
    statuses = [r['status'] for r in results]
    status = ('error' if 'error' in statuses
              else 'fail' if 'fail' in statuses else 'pass')
    return {
        'status': status,
        'cases': results,
        'expression_control': False,
        'counts_for_live': False,
        'trajectory': trajectories.HISTORICAL_REPLAY,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'method': 'isolated_fixture_frozen_replay',
            'cases': len(results),
            'passed': statuses.count('pass'),
            'failed': statuses.count('fail'),
            'errored': statuses.count('error'),
            'conditions': sorted({r['condition'] for r in results}),
            'kernels': sorted({r['kernel'] for r in results}),
            'llm_reinterpretation': False,
            'network': False,
            'production_ledger': False,
        },
        'limitations': list(LIMITATIONS),
    }


def run_condition_matrix(fixture, *, start_at, end_at, kernels=KERNELS, ts=None):
    """整张交叉矩阵入口：对每个 内核×条件 用对应引擎跑「前→条件→重跑」。

    fixture 是 {condition: spec} 字典，每个 spec 含引擎所需参数与显式 'expected'
    分型。返回 run_matrix 报告（确定性、盖影子口径）。夹具与期望由调用方构造，
    本函数只编排，不构造任何自然样本、不碰生产账本。
    """
    cases = []
    for kernel in kernels:
        for condition in CONDITIONS:
            spec = fixture[condition]
            engine = ENGINES[condition]
            cid = '%s__%s' % (condition, kernel)
            cases.append(case(cid, condition, kernel, spec['expected'],
                              lambda s=spec, e=engine, k=kernel: e(
                                  s, start_at=start_at, end_at=end_at, kernel=k, ts=ts)))
    return run_matrix(cases)