# -*- coding: utf-8 -*-
"""S08 回忆影响情感的隔离实验（emotion 轨，路线 §4.3 / §9，manual_review）。

S06 的 LIMITATIONS 明确「记忆→情绪隔离实验属 S08」；本模块接住它：在隔离
条件下对照「无回忆」与「有回忆（R06 证据场景作为输入）」两条情感轨迹，对
**已记录的事件与评估**做确定性重放，产出逐 tick 差异与来源归因，打包成可供
人工评审的隔离实验包。

**真实结论需真实样本与人工评审；本契约只证明可执行；不证明主观体验。**
契约可执行 ≠ 真实实验已做：本模块不自称已证明「回忆影响情感」，只回答
「在冻结规则下，加入/撤掉回忆输入会不会让重放状态出现差异、差异来自哪条
回忆、来源引用是否完整」。organic=not_observed、authorization=manual_review
（与 V02 同口径），真实流量观测与结论评审都在本模块之外。

实验设计（纯函数、确定性、隔离可测）：
  计划   plan_experiment：固定事件序列 + R06 渲染输出 → 两臂共享同一冻结
         参数与初始状态；对照臂=原序列，回忆臂=原序列+回忆事件。now 必填，
         experiment_id 内容寻址（同输入同 ID，篡改计划会被 run_experiment 拒绝）。
  回忆事件化 recall_events：R06 场景条目 → tool_result(ok=True) 形状＝
         「检索成功/查到」，绝不伪装成新的亲历消息（message）：认识论分层
         不混，回忆是「查到/想起」不是新亲历，冻结规则下回忆不直接驱动效价。
         每条事件携带 object_id/scene_id/verdict_refs/store/epistemic_kind 等
         来源引用（逐字深拷贝自 R06，不补造）。事件 ID 只按 object_id 内容
         寻址：同一记忆反复检索/跨场景重复出现 → 同 ID，由内核去重口径
         「同一事件只更新一次」，不反复加分。invalidated 条目绝不事件化。
  重放   run_experiment：两臂各跑一遍 emotion_revision.build_base_run（真实
         冻结重放，不自带第二套动力学）；撤回走 dag.correction +
         rev.plan_revision/commit_revision（S03/M05 口径），撤源的回忆零冲击、
         不得继续驱动差异。max_error 口径对齐 S06：冲击对账 0 误差、恢复
         连续性 ≤1e-10。
  差异   逐事件（tick）状态 diff + 汇总：哪些维度变了、由哪条回忆引起、
         来源引用；稳定排序。无差异如实报 no_effect，不夸大。
  评审包 review_package：结构化 JSON（计划+两臂轨迹摘要+diff+限制声明）。

硬边界（与 S03/S06/V02 同一范式）：
- 只复用冻结原语：动力学/恢复/界/排序/去重全部来自 emotion_core(shadow-v1)
  与 emotion_dynamics(shadow-v2)（经 emotion_revision）；撤回语义来自
  invalidation_dag。本模块不自带 exp/tau，不写第二套顺序键，不调 LLM 评估
  （评估输入必须是已记录形状，合成夹具除外），不读写存储、不解析路径、
  不建库建表建目录、不发网络。
- 不写生产 emotion_shadow 账本，不接管表达：expression_control 恒 False，
  counts_for_live 由 S01 契约盖章不计在线；账本是调用方持有的内存对象。
- 确定性：所有时间戳取计划里的 now/recall_at，墙上时钟不进状态/ID/报告；
  同输入同输出。合成夹具的时间是构造顺序，不能解释成真实情绪持续时间。
"""
import copy
import hashlib
import json

import emotion_core as legacy
import emotion_revision as rev
import emotion_trajectories as trajectories
import invalidation_dag as dag

ALGORITHM_VERSION = 's08-1'
DEFAULT_KERNEL = rev.DEFAULT_KERNEL
KERNELS = tuple(rev.KERNELS)
DIMENSIONS = rev.DIMENSIONS
ARMS = ('control', 'recall')
# 回忆的认识论层：检索行为本身是「查到」，不是新亲历；来源对象自身的
# epistemic_kind 原样保留在 payload 里，绝不改写冒充亲历。
RECALL_LAYER = 'looked_up'
RECALL_SOURCE = 'recall'
RECALL_EVENT_KIND = 'tool_result'
# 分段指数恢复有浮点尾差（exp(-a)*exp(-b) ≠ exp(-(a+b)) 逐位）；与 S06 恢复
# 连续性容差同量级。小于它的差异是数值噪声，不许报成「回忆改变了情绪」。
EFFECT_EPSILON = 1e-10

LIMITATIONS = (
    '真实结论需真实样本与人工评审；本契约只证明可执行，不证明主观体验，'
    '也不自称已证明「回忆影响情感」：organic=not_observed、authorization=manual_review。',
    '只证冻结重放在隔离合成夹具上的差异与归因，不证生产账本已合规；'
    '生产审计走 phase_audit 只读快照。',
    '动力学/恢复/界/排序/去重全部复用 emotion_core 与 emotion_revision 冻结原语；'
    '本模块不自带第二套 exp/tau 动力学，也不写第二套回放顺序键。',
    '回忆事件化采用 tool_result(ok=True)「检索成功」口径：回忆是查到/想起，'
    '不是新亲历消息；冻结规则下回忆不直接驱动效价，效价冲击只能来自真实消息证据。'
    '差异判定用 1e-10 容差（与 S06 恢复连续性同量级），分段恢复的浮点尾差不报成效应。',
    '不调 LLM 评估或重新解释原话；不写生产 emotion_shadow 账本、不接管表达'
    '（expression_control 恒 False），counts_for_live 由 S01 契约盖章不计在线。',
    '撤回的回忆（S03/M05 失效）零冲击、不再驱动差异；invalidated 条目绝不事件化，'
    '但本模块不做真实失效判定，失效输入由 R06/M05 上游负责。',
    '合成夹具的时间是构造顺序，不能解释成真实情绪持续时间；通过隔离实验只排除'
    '明显异常，不能替代真实流量观测。真实注入接线（C04）与表达接管未落地。',
)


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


# ── 回忆事件化：R06 场景 → 冻结内核可消费的「检索成功」事件 ────────────


def recall_events(rendered, *, recall_at, source=RECALL_SOURCE):
    """R06 render_evidence 输出 → 回忆事件列表 + 来源索引。纯函数，不改输入。

    每条场景条目事件化为 tool_result(ok=True)：回忆＝「查到/想起」的检索
    成功，不是新亲历消息。payload 逐字携带 object_id/scene_id/verdict_refs/
    store/epistemic_kind/根源/摘述等来源引用（深拷贝，不补造缺槽）。事件 ID
    只按 object_id 内容寻址：同一记忆跨场景/反复检索得到同 ID，交给内核
    「同一事件只更新一次」去重，不反复加分。invalidated 条目大声登记跳过，
    绝不事件化（失效回忆不得驱动差异）。

    返回 {events, index, skipped, duplicate_ids, source_status}：index 是
    event_id → 来源引用（首个出现为准，与内核同 ID 取首条口径一致）。
    """
    if not isinstance(rendered, dict) or not isinstance(rendered.get('scenes'), list):
        raise ValueError('recall input must be R06 render_evidence output with scenes')
    at = legacy._number(recall_at, 'recall_at')
    if at <= 0:
        raise ValueError('recall_at must be positive')
    source = _text(source)
    if not source:
        raise ValueError('recall source must be a nonempty string')
    if legacy._source_is(source, 'legacy'):
        raise ValueError('recall source must not masquerade as legacy evidence')
    events, index, skipped = [], {}, []
    scenes = sorted(rendered['scenes'], key=lambda s: _text(s.get('scene_key')))
    for scene in scenes:
        if not isinstance(scene, dict) or not isinstance(scene.get('entries'), list):
            raise ValueError('recall scene must carry an entries list')
        scene_id = _text(scene.get('scene_id'))
        if any(not isinstance(e, dict) for e in scene['entries']):
            raise ValueError('recall entry must be a dict')
        for entry in sorted(scene['entries'], key=lambda e: _text(e.get('object_id'))):
            oid = _text(entry.get('object_id'))
            if not oid:
                raise ValueError('recall entry requires object_id')
            if _text(entry.get('validity')) == 'invalidated':
                skipped.append({'object_id': oid, 'scene_id': scene_id,
                                'reason': 'invalidated_memory'})
                continue
            eid = 'recall_' + _sha(oid)
            refs = copy.deepcopy(entry.get('verdict_refs') or [])
            payload = {
                'ok': True,
                'object_id': oid,
                'scene_id': scene_id,
                'store': _text(entry.get('store')),
                'epistemic_kind': _text(entry.get('epistemic_kind')),
                'subject_ref': _text(entry.get('subject_ref')),
                'root_source_ids': list(entry.get('root_source_ids') or []),
                'root_representative': _text(entry.get('root_representative')),
                'restatement': _text(entry.get('restatement')),
                'occurred_at': entry.get('occurred_at'),
                'verdict_refs': refs,
                'missing_slots': copy.deepcopy(entry.get('missing_slots') or []),
                'recall_layer': RECALL_LAYER,
            }
            events.append({'id': eid, 'kind': RECALL_EVENT_KIND, 'ts': at,
                           'source': source, 'payload': payload})
            index.setdefault(eid, {
                'event_id': eid, 'object_id': oid, 'scene_id': scene_id,
                'store': payload['store'], 'epistemic_kind': payload['epistemic_kind'],
                'root_source_ids': list(payload['root_source_ids']),
                'restatement': payload['restatement'], 'verdict_refs': copy.deepcopy(refs),
            })
    counts = {}
    for event in events:
        counts[event['id']] = counts.get(event['id'], 0) + 1
    return {'events': events, 'index': index,
            'skipped': sorted(skipped, key=lambda s: (s['object_id'], s['scene_id'])),
            'duplicate_ids': sorted(eid for eid, n in counts.items() if n > 1),
            'source_status': _text(rendered.get('status'))}


# ── 实验计划：两臂共享冻结参数与初始状态，experiment_id 内容寻址 ────────


def _experiment_id(kernel_version, start, end, tick, now, recall_at, base_events, recalls):
    return 's08_' + _sha(ALGORITHM_VERSION, kernel_version, start, end, tick, now,
                         recall_at, _canonical(base_events), _canonical(recalls))


def plan_experiment(events, rendered, *, now, recall_at=None, start_at=None,
                    end_at=None, kernel=DEFAULT_KERNEL, tick_seconds=None):
    """纯函数：固定事件序列 + R06 证据场景 → 隔离实验计划。不写任何东西。

    now 必填（正有限数）：实验时间戳，代替墙上时钟进入全部记录与 ID。
    recall_at 缺省取 now。窗口缺省 start_at=0、end_at=覆盖全部事件与回忆时刻。
    两臂共享同一冻结内核参数哈希与 initial_state；对照臂=原序列，回忆臂=
    原序列+回忆事件。experiment_id 由全部内容寻址，run_experiment 会复核。
    """
    module = rev._kernel(kernel)
    at = legacy._number(now, 'now')
    if at <= 0:
        raise ValueError('now must be a positive finite number')
    recall_at = at if recall_at is None else legacy._number(recall_at, 'recall_at')
    base = copy.deepcopy(list(events or []))
    for value in base:
        legacy._event_header(value)
    bundle = recall_events(rendered, recall_at=recall_at)
    start = 0.0 if start_at is None else legacy._number(start_at, 'start_at')
    if end_at is None:
        times = [recall_at]
        for value in base:
            _, ts_value, effective = legacy._event_header(value)
            if ts_value > 0 and effective > 0:
                times.append(effective)
        end = max(times)
    else:
        end = legacy._number(end_at, 'end_at')
    if start < 0 or end < start:
        raise ValueError('start_at/end_at must define a nonnegative ordered window')
    if not start <= recall_at <= end:
        raise ValueError('recall_at lies outside the replay window')
    tick = None if tick_seconds is None else legacy._number(tick_seconds, 'tick_seconds')
    recall_ids = {e['id'] for e in bundle['events']}
    collision = sorted(recall_ids & {_text(e.get('id')) for e in base})
    if collision:
        raise ValueError('recall event id collides with base event: %s' % collision[0])
    hashes = rev._kernel_hashes(module)
    return {
        'experiment_id': _experiment_id(module.VERSION, start, end, tick, at,
                                        recall_at, base, bundle['events']),
        'algorithm_version': ALGORITHM_VERSION,
        'now': at, 'kernel_version': module.VERSION,
        'start_at': start, 'end_at': end, 'tick_seconds': tick, 'recall_at': recall_at,
        'base_events': base, 'recall_events': bundle['events'],
        'recall_index': bundle['index'], 'recall_skipped': bundle['skipped'],
        'recall_source_status': bundle['source_status'],
        'duplicate_recall_ids': bundle['duplicate_ids'],
        'initial_state': module.initial_state(start),
        'arms': {'control': len(base), 'recall': len(base) + len(bundle['events'])},
        'expression_control': False,
        **hashes,
    }


# ── 确定性重放：两臂各跑一遍真实冻结重放，撤回走 S03 修订链 ─────────────


def _run_arm(events, plan):
    return rev.build_base_run(events, start_at=plan['start_at'], end_at=plan['end_at'],
                              kernel=plan['kernel_version'],
                              tick_seconds=plan['tick_seconds'], ts=plan['now'])


def _resolve_recall_target(plan, target):
    target = _text(target)
    if target in plan['recall_index']:
        return target
    for eid, ref in plan['recall_index'].items():
        if ref['object_id'] == target:
            return eid
    raise ValueError('retraction_target_not_recall: %s' % target)


def _diff(control, recall, index):
    """逐 tick（事件）状态 diff + 汇总归因。稳定排序，无差异如实 no_effect。"""
    control_rows = {}
    for row in control['timeline']:
        control_rows.setdefault(row['event_id'], row)
    rows = []
    for row in recall['timeline']:
        ref = index.get(row['event_id'])
        prior = control_rows.get(row['event_id'])
        if prior is None:
            delta, present = dict(row['delta']), False
        else:
            delta = {k: row['after'][k] - prior['after'][k] for k in DIMENSIONS}
            present = True
        rows.append({'event_id': row['event_id'], 'at': row['at'], 'rule': row['rule'],
                     'consumed': row['consumed'], 'is_recall': ref is not None,
                     'control_present': present,
                     'recall_ref': copy.deepcopy(ref) if ref is not None else None,
                     'diff': delta,
                     'changed': any(abs(v) > EFFECT_EPSILON for v in delta.values())})
    final_diff = {k: recall['state'][k] - control['state'][k] for k in DIMENSIONS}
    changed = [k for k in DIMENSIONS if abs(final_diff[k]) > EFFECT_EPSILON]
    sources, retracted = [], []
    for row in recall['timeline']:
        ref = index.get(row['event_id'])
        if ref is None:
            continue
        if row['rule'] == 'retracted_source':
            retracted.append({'event_id': row['event_id'], 'object_id': ref['object_id']})
            continue
        if row['consumed'] and any(row['delta'].values()):
            sources.append({'event_id': row['event_id'], 'object_id': ref['object_id'],
                            'scene_id': ref['scene_id'], 'store': ref['store'],
                            'epistemic_kind': ref['epistemic_kind'],
                            'verdict_refs': copy.deepcopy(ref['verdict_refs']),
                            'rule': row['rule'], 'delta': dict(row['delta']),
                            'dimensions': [k for k in DIMENSIONS if row['delta'][k] != 0.0]})
    sources.sort(key=lambda s: s['event_id'])
    retracted.sort(key=lambda r: r['event_id'])
    by_dimension = {k: {'final_diff': final_diff[k],
                        'source_object_ids': sorted({s['object_id'] for s in sources
                                                     if k in s['dimensions']})}
                    for k in DIMENSIONS}
    return {'rows': rows, 'final_diff': final_diff, 'changed_dimensions': changed,
            'effect': 'difference_observed' if changed else 'no_effect',
            'state_identical': control['state'] == recall['state'],
            'processed_diff': recall['state']['processed'] - control['state']['processed'],
            'sources': sources, 'retracted_recalls': retracted,
            'by_dimension': by_dimension}


def run_experiment(plan, *, retractions=None):
    """确定性执行计划：两臂重放 + 可选撤回重算 + 差异报告。不写任何存储。

    retractions 是 [{'target': object_id 或回忆事件 ID, 'reason': 必填理由}]，
    只允许撤回忆臂的回忆事件（S03 observation_invalid 口径）；撤源后完整重放，
    撤回的回忆零冲击、不再驱动差异。两臂重跑逐位一致才算确定性通过；
    max_error 口径对齐 S06（冲击对账 0 误差、恢复连续性 ≤1e-10）。
    """
    if not isinstance(plan, dict) or not _text(plan.get('experiment_id')).startswith('s08_'):
        raise ValueError('plan must come from plan_experiment')
    expected = _experiment_id(plan['kernel_version'], plan['start_at'], plan['end_at'],
                              plan['tick_seconds'], plan['now'], plan['recall_at'],
                              plan['base_events'], plan['recall_events'])
    if plan['experiment_id'] != expected:
        raise ValueError('plan content does not match experiment_id')
    control = _run_arm(plan['base_events'], plan)
    recall = _run_arm(plan['base_events'] + plan['recall_events'], plan)
    control_again = _run_arm(copy.deepcopy(plan['base_events']), plan)
    recall_again = _run_arm(copy.deepcopy(plan['base_events'] + plan['recall_events']), plan)
    deterministic = (control['state'] == control_again['state']
                     and control['timeline'] == control_again['timeline']
                     and control['revision_id'] == control_again['revision_id']
                     and recall['state'] == recall_again['state']
                     and recall['timeline'] == recall_again['timeline']
                     and recall['revision_id'] == recall_again['revision_id'])
    ledger = rev.new_ledger()
    rev.commit_base_run(ledger, recall)
    applied = []
    for entry in retractions or []:
        if not isinstance(entry, dict):
            raise ValueError('retraction must be a dict with target and reason')
        eid = _resolve_recall_target(plan, entry.get('target'))
        reason = _text(entry.get('reason'))
        rec = dag.correction('observation_invalid', eid, reason, ts=plan['now'])
        recall = rev.commit_revision(ledger, rev.plan_revision(
            ledger, recall['revision_id'], rec, ts=plan['now']))
        applied.append({'correction_id': rec['correction_id'], 'target_event_id': eid,
                        'object_id': plan['recall_index'][eid]['object_id'], 'reason': reason})
    diff = _diff(control, recall, plan['recall_index'])
    hashes = ('parameter_hash', 'dynamics_parameter_hash', 'appraisal_rule_hash')
    module = rev._kernel(plan['kernel_version'])
    retracted_rows = [r for r in recall['timeline']
                      if r['event_id'] in plan['recall_index'] and r['rule'] == 'retracted_source']
    checks = {
        'arms_share_parameters': all(control[k] == recall[k] == plan[k] for k in hashes),
        'arms_share_initial_state': (control['start_at'] == recall['start_at']
                                     and plan['initial_state'] == module.initial_state(plan['start_at'])),
        'deterministic_replay': deterministic,
        'control_checks_ok': control['checks']['ok'],
        'recall_checks_ok': recall['checks']['ok'],
        'max_impulse_error': max(control['checks']['impulse_accounting_error'],
                                 recall['checks']['impulse_accounting_error']),
        'max_recovery_error': max(control['checks']['recovery_continuity_error'],
                                  recall['checks']['recovery_continuity_error']),
        'retracted_recalls_zero_impulse': all(
            not r['consumed'] and not any(r['delta'].values()) for r in retracted_rows),
        'retracted_not_attributed': not ({r['event_id'] for r in retracted_rows}
                                         & {s['event_id'] for s in diff['sources']}),
        'trajectory_is_shadow': (control['trajectory'] == recall['trajectory']
                                 and control['trajectory'] in (trajectories.V1_REFERENCE,
                                                               trajectories.HISTORICAL_REPLAY)),
    }
    checks['max_error_ok'] = (checks['max_impulse_error'] == 0.0
                              and checks['max_recovery_error'] <= 1e-10)
    return {'experiment_id': plan['experiment_id'], 'now': plan['now'],
            'arms': {'control': control, 'recall': recall},
            'recall_base_revision_id': recall_again['revision_id'],
            'retractions': applied, 'diff': diff, 'checks': checks,
            'expression_control': False, 'counts_for_live': False,
            'trajectory': control['trajectory']}


# ── 评审包：计划 + 两臂轨迹摘要 + diff + 限制声明（结构化 JSON）────────


def _arm_summary(record):
    return {'revision_id': record['revision_id'],
            'parent_revision_id': record['parent_revision_id'],
            'branch': record['branch'], 'kernel_version': record['kernel_version'],
            'parameter_hash': record['parameter_hash'],
            'dynamics_parameter_hash': record['dynamics_parameter_hash'],
            'appraisal_rule_hash': record['appraisal_rule_hash'],
            'start_at': record['start_at'], 'end_at': record['end_at'],
            'tick_seconds': record['tick_seconds'], 'state': copy.deepcopy(record['state']),
            'event_count': record['event_count'],
            'replayed_event_ids': list(record['replayed_event_ids']),
            'excluded': dict(record['excluded']),
            'correction_chain': [c['correction_id'] for c in record.get('corrections') or []],
            'checks': copy.deepcopy(record['checks']),
            'expression_control': record['expression_control'] is True,
            'counts_for_live': record['counts_for_live'],
            'trajectory': record['trajectory']}


def review_package(plan, result):
    """把计划与执行结果打包成可供人工评审的结构化 JSON。纯函数，不落盘。

    包是评审材料不是结论：organic=not_observed、authorization=manual_review，
    真实结论需真实样本与人工评审；本契约只证明可执行，不证明主观体验。
    """
    if not isinstance(plan, dict) or not isinstance(result, dict):
        raise ValueError('review_package requires plan and result dicts')
    if plan.get('experiment_id') != result.get('experiment_id'):
        raise ValueError('result does not belong to this plan')
    return {
        'experiment_id': plan['experiment_id'],
        'algorithm_version': ALGORITHM_VERSION,
        'now': plan['now'],
        'plan': {
            'kernel_version': plan['kernel_version'],
            'start_at': plan['start_at'], 'end_at': plan['end_at'],
            'tick_seconds': plan['tick_seconds'], 'recall_at': plan['recall_at'],
            'base_event_ids': [e['id'] for e in plan['base_events']],
            'recall_event_ids': [e['id'] for e in plan['recall_events']],
            'recall_index': copy.deepcopy(plan['recall_index']),
            'recall_skipped': copy.deepcopy(plan['recall_skipped']),
            'recall_source_status': plan['recall_source_status'],
            'duplicate_recall_ids': list(plan['duplicate_recall_ids']),
            'parameter_hash': plan['parameter_hash'],
            'dynamics_parameter_hash': plan['dynamics_parameter_hash'],
            'appraisal_rule_hash': plan['appraisal_rule_hash'],
            'initial_state': copy.deepcopy(plan['initial_state']),
            'arms': dict(plan['arms']),
        },
        'arms': {arm: _arm_summary(result['arms'][arm]) for arm in ARMS},
        'retractions': copy.deepcopy(result['retractions']),
        'diff': copy.deepcopy(result['diff']),
        'checks': copy.deepcopy(result['checks']),
        'expression_control': False,
        'counts_for_live': False,
        'trajectory': result['trajectory'],
        'organic': 'not_observed',
        'authorization': 'manual_review',
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'method': 'isolated_recall_ablation_frozen_replay',
            'kernel_version': plan['kernel_version'],
            'recall_event_kind': RECALL_EVENT_KIND,
            'recall_layer': RECALL_LAYER,
            'llm_evaluation': False,
            'llm_reinterpretation': False,
            'network': False,
            'production_ledger': False,
            'expression_takeover': False,
        },
        'limitations': list(LIMITATIONS),
    }


def run_isolation_experiment(events, rendered, *, now, recall_at=None, start_at=None,
                             end_at=None, kernel=DEFAULT_KERNEL, tick_seconds=None,
                             retractions=None):
    """端到端入口：计划 → 两臂确定性重放 → 评审包。隔离合成夹具专用。"""
    plan = plan_experiment(events, rendered, now=now, recall_at=recall_at,
                           start_at=start_at, end_at=end_at, kernel=kernel,
                           tick_seconds=tick_seconds)
    result = run_experiment(plan, retractions=retractions)
    return review_package(plan, result)