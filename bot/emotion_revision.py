# -*- coding: utf-8 -*-
"""S03 撤回、纠正和版本化情感重算（路线 §4.3 状态转移 / §9 情绪因果·恢复·记忆纠错）。

M05 的 plan_emotion_revision 只产出新 revision 的分支计划；本模块接住它：
对**已记录的事件与评估**做确定性重放，应用撤回/纠正，把结果挂成父 revision
之下的新分支。旧 run、旧轨迹原样保留，绝不覆盖历史反应。

重算语义（§4.3 口径）：
  · 只复用 emotion_core(shadow-v1) / emotion_dynamics(shadow-v2) 的冻结
    appraise/advance/bounds，不自带第二套动力学；参数哈希与 S02 两把锁
    原样记进每个 revision，漂移由冻结内核当场拒绝。
  · 每个 revision 都是「父链继承的累计纠正链」下对基础窗口的完整重放。
    父 revision 的终态只作 before 对账基线，不作重放初值：指数恢复与
    clip 是路径依赖的，撤掉中途贡献无法用终态减 delta 等价实现。
  · 同一事件只更新一次：排序/去重/窗口口径完全沿用 emotion_core.replay
    （effective_ts 优先、source_seq > input_seq > 输入索引、同 ID 只取
    排序后首条），复用其头校验与排序键，不另写一套顺序规则。
  · 不调用 LLM 重新解释原话；随事件携带的已记录评估必须与冻结规则一致，
    不一致大声拒绝，防止外部任意 delta 绕过证据门。

两类纠正语义严格分开（M05 口径）：
  observation_invalid    撤源：移除该事件的评估贡献。事件仍留在审计时间线
                         （retracted_source：零冲击、不推进恢复、不计
                         processed），终态与「该事件从未存在」的重放逐位
                         一致；撤回单调，后续解释纠正不能把它洗回来。
  interpretation_revised 解释纠正：只允许白名单内的证据标志修正（引用/
                         扮演/ addressed_to_self 等），用冻结规则重新评估
                         并替换旧评估；原事件、原话、原时间保留，不连坐。
                         不许覆盖 text，不许注入任意 delta。

账本是调用方传入的内存对象（new_ledger），只追加：读函数纯读、返回深拷贝；
plan_revision 只算不写；commit 拒绝重复 ID 与内容哈希不符。self_model 目前
没有情感修订的持久化入口，本模块不建库建表建目录；只读桥
read_revision_inputs 沿用 M05 先例（mode=ro + query_only，DATA_DIR 调用时
解析，缺库报 absent/degraded）。

影子边界：expression_control 恒 False，counts_for_live 由 S01 契约盖章
（v2→historical_replay，v1→v1_reference），不改 emotion_shadow 的实时消费
路径。迟到/重启回放矩阵是 S06，候选回放性能与脱敏读取是 S07，都不在这里做。
"""
import copy
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

import emotion_core as legacy
import emotion_dynamics as dynamics
import emotion_trajectories as trajectories
import invalidation_dag as dag

ALGORITHM_VERSION = 's03-1'
DEFAULT_KERNEL = 'shadow-v2'
KERNELS = {legacy.VERSION: legacy, dynamics.VERSION: dynamics}
DIMENSIONS = dynamics.DIMENSIONS
# 只有这两类修正直接作用于情感证据；belief_corrected/scope_revoked 针对
# 信念与授权范围，不产生情感贡献变化，拒绝而不是假装重算。
EMOTION_CORRECTION_KINDS = ('observation_invalid', 'interpretation_revised')
# 解释纠正只许改「证据标志」：改 text 是改写原话，注入 delta 是绕过证据门。
EVIDENCE_OVERRIDE_KEYS = ('addressed_to_self', 'quoted', 'is_quote', 'is_quoted',
                          'quote', 'quoted_text', 'roleplay', 'is_roleplay', 'role_play')
LIMITATIONS = (
    '重算只复用冻结规则，不调用 LLM 重新解释原话；解释纠正仅接受证据标志修正。',
    '修订是历史重放分支：不计在线观察、不接管表达（expression_control=False）。',
    '迟到/重启/同刻回放矩阵属 S06，候选回放性能与脱敏读取属 S07，本模块不覆盖。',
    '候选语义标签（integrated_appraisal）不参与重算；候选反事实属 S05/S07 口径。',
    '账本为调用方传入的内存对象；持久化需要 self_model 新增修订事务入口，本模块不建库建表。',
    '只读桥不附影子账本的已记录评估；未附评估的事件由冻结规则重算，附了的必须与规则一致。',
)


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


# ── 内存账本：只追加，读函数纯读 ────────────────────────────────────


def new_ledger():
    """空白修订账本。调用方持有；本模块不落盘、不解析路径。"""
    return {'revisions': {}, 'order': []}


def _check_ledger(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('revisions'), dict)
            or not isinstance(ledger.get('order'), list)):
        raise ValueError('ledger must come from new_ledger()')


def _kernel(name):
    kernel = KERNELS.get(_text(name))
    if kernel is None:
        raise ValueError('unknown frozen kernel: %s' % name)
    return kernel


def _kernel_hashes(kernel):
    if kernel is dynamics:
        return {'parameter_hash': dynamics.parameter_hash(),
                'dynamics_parameter_hash': dynamics.dynamics_parameter_hash(),
                'appraisal_rule_hash': dynamics.appraisal_rule_hash()}
    # v1 冻结内核没有 S02 的分锁，如实记 None，不伪造第二把锁。
    return {'parameter_hash': legacy.parameter_hash(),
            'dynamics_parameter_hash': None, 'appraisal_rule_hash': None}


# ── 冻结内核之上的重放编排（动力学本身全部来自内核）─────────────────


def _evaluated_view(event):
    # 与 emotion_shadow.tick 同一口径：群聊/私聊消息按 message 评估。
    view = dict(event)
    if view.get('kind') in ('group_message', 'private_message'):
        view['kind'] = 'message'
    return view


def _corrected_view(event, overrides):
    view = _evaluated_view(event)
    view['payload'] = {**legacy._payload(event), **overrides}
    # 旧记录评估属于旧证据口径，不能混进纠正后的评估。
    view.pop('appraisal', None)
    return view


def _checked_overrides(overrides):
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError('interpretation_revised requires nonempty evidence_overrides')
    for key in overrides:
        if key not in EVIDENCE_OVERRIDE_KEYS:
            raise ValueError('evidence override key not allowed: %s' % key)
    legacy._json_values(overrides)
    return dict(overrides)


def _resolve_chain(chain):
    """累计纠正链 → (被撤源事件, 解释替换)。撤回单调：撤源压过一切解释纠正。"""
    retracted, replacements = set(), {}
    for entry in chain:
        target = entry['target_id']
        if entry['kind'] == 'observation_invalid':
            retracted.add(target)
            replacements.pop(target, None)
        else:
            if target in retracted:
                raise ValueError('target_already_retracted: %s' % target)
            replacements[target] = entry
    return retracted, replacements


def _advance_to(state, target, tick, steps, kernel):
    """与 emotion_core._replay_advance 同构的 tick 编排，advance 用所选内核。"""
    if tick is None or target <= state['at']:
        return kernel.advance(state, target), steps
    origin = state['at']
    elapsed = target - origin
    if elapsed > tick and (origin + tick <= origin or target - tick >= target):
        raise ValueError('tick_seconds is below timestamp precision')
    ratio = elapsed / tick
    if not math.isfinite(ratio) or ratio > legacy._MAX_REPLAY_STEPS - steps:
        raise ValueError('tick_seconds requires too many replay steps')
    count = max(1, math.ceil(ratio))
    if count > legacy._MAX_REPLAY_STEPS - steps:
        raise ValueError('tick_seconds requires too many replay steps')
    for index in range(1, count + 1):
        # 从段起点计算，避免累计加法误差；最后一步精确落在事件/窗口终点。
        at = target if index == count else min(target, origin + index * tick)
        if at <= state['at']:
            raise ValueError('tick_seconds is below timestamp precision')
        state = kernel.advance(state, at)
        steps += 1
        if at == target:
            break
    return state, steps


def _impulse(state, assessment, kernel):
    """clip(x+delta) 与 processed+1：与两个冻结内核的冲击原语逐位同式。"""
    result = dict(state)
    for key in DIMENSIONS:
        low, high = kernel.PARAMETERS['bounds'][key]
        result[key] = min(high, max(low, result[key] + assessment['delta'][key]))
    result['processed'] += 1
    return result


def _excluded_assessment(rule, reason, event):
    return {'rule': rule, 'delta': dict.fromkeys(DIMENSIONS, 0.0), 'ignored': True,
            'reason': reason, 'confidence': 0.0, 'synthetic': legacy._synthetic(event)}


def _row(eid, kind, before, assessment, after, kernel, consumed, action,
         correction_id='', original=None):
    row = {'event_id': eid, 'kind': kind, 'at': before['at'],
           'rule': assessment['rule'], 'delta': dict(assessment['delta']),
           'before': copy.deepcopy(before), 'after': copy.deepcopy(after),
           'ignored': assessment['ignored'], 'reason': assessment['reason'],
           'confidence': assessment['confidence'], 'version': kernel.VERSION,
           'synthetic': assessment['synthetic'], 'consumed': consumed,
           'revision_action': action, 'correction_id': correction_id}
    if original is not None:
        row['original_appraisal'] = copy.deepcopy(original)
    return row


def _replay(events, chain, kernel, start_at, end_at, tick_seconds):
    start = legacy._number(start_at, 'start_at')
    end = legacy._number(end_at, 'end_at')
    if start < 0 or end < start:
        raise ValueError('start_at/end_at must define a nonnegative ordered window')
    tick = None if tick_seconds is None else legacy._number(tick_seconds, 'tick_seconds')
    if tick is not None:
        if tick <= 0:
            raise ValueError('tick_seconds must be positive')
        if (end - start) / tick > legacy._MAX_REPLAY_STEPS:
            raise ValueError('tick_seconds requires too many replay steps')
    retracted, replacements = _resolve_chain(chain)
    try:
        ordered = []
        for index, value in enumerate(events):
            header = legacy._event_header(value)
            ordered.append((legacy._replay_key(header, value, index), header, value))
    except TypeError:
        raise ValueError('events must be an iterable of event dicts') from None
    ordered.sort(key=lambda item: item[0])
    state = kernel.initial_state(start)
    steps, timeline, seen, excluded = 0, [], set(), {}
    for _, (eid, ts, effective), value in ordered:
        rule = reason = None
        if eid in seen:
            rule, reason = 'duplicate_event', 'Event ID was already encountered; no second impulse.'
        elif ts <= 0 or effective <= 0:
            rule, reason = 'unknown_timestamp', 'Nonpositive event time is unknown; excluded.'
        elif not (start <= effective <= end and
                  (('effective_ts' in value and ts <= end) or start <= ts <= end)):
            rule, reason = 'outside_window', 'Effective event time lies outside the replay window.'
        elif eid in retracted:
            rule = 'retracted_source'
            reason = 'Retracted evidence contributes no impulse; the original observation stays auditable.'
        seen.add(eid)
        if rule is not None:
            assessment = _excluded_assessment(rule, reason, value)
            action = 'retracted' if rule == 'retracted_source' else 'excluded'
            timeline.append(_row(eid, value.get('kind'), state, assessment, state,
                                 kernel, False, action))
            excluded[rule] = excluded.get(rule, 0) + 1
            continue
        entry = replacements.get(eid)
        if entry is not None:
            assessment = kernel.appraise(_corrected_view(value, entry['evidence_overrides']))
            original = kernel.appraise(_evaluated_view(value))
            action, correction_id = 'replaced', entry['correction_id']
        else:
            assessment = kernel.appraise(_evaluated_view(value))
            recorded = value.get('appraisal')
            if recorded is not None and recorded != assessment:
                raise ValueError('recorded appraisal does not match frozen rules: %s' % eid)
            action, correction_id, original = 'recorded', '', None
        before, steps = _advance_to(state, effective, tick, steps, kernel)
        state = _impulse(before, assessment, kernel)
        timeline.append(_row(eid, value.get('kind'), before, assessment, state, kernel,
                             True, action, correction_id, original))
    state, _ = _advance_to(state, end, tick, steps, kernel)
    return {'state': state, 'timeline': timeline, 'excluded': excluded,
            'replayed_event_ids': [r['event_id'] for r in timeline if r['consumed']]}


# ── 审计：冲击对账与恢复连续性，抓无因跳变 ─────────────────────────


def verify_revision(revision):
    """纯读审计：从时间线复算每一步，回答「状态变化是否都有因」。

    冲击对账要求 0 误差（after == clip(before + delta) 逐位一致）；恢复
    连续性允许 1e-10 浮点尾差（不同 tick 切分按同一经过时间合成）。
    结构损坏/越界状态如实报 ok=False，不抛异常冒充通过。
    """
    out = {'ok': False, 'impulse_accounting_error': None,
           'recovery_continuity_error': None, 'processed_matches': False,
           'no_unexplained_jump': False, 'reason': ''}
    try:
        kernel = _kernel(revision.get('kernel_version'))
        previous = kernel.initial_state(revision['start_at'])
        impulse_error = continuity_error = 0.0
        consumed = 0
        for row in revision.get('timeline') or []:
            expected_before = kernel.advance(previous, row['at'])
            continuity_error = max(continuity_error, *(
                abs(expected_before[k] - row['before'][k]) for k in (*DIMENSIONS, 'at')))
            if row.get('consumed'):
                recomputed = _impulse(row['before'], {'delta': row['delta']}, kernel)
                impulse_error = max(impulse_error, *(
                    abs(recomputed[k] - row['after'][k]) for k in (*DIMENSIONS, 'at', 'processed')))
                consumed += 1
            else:
                if row['before'] != row['after'] or any(row['delta'].values()):
                    impulse_error = max(impulse_error, 1.0)
            previous = row['after']
        final = kernel.advance(previous, revision['end_at'])
        continuity_error = max(continuity_error, *(
            abs(final[k] - revision['state'][k]) for k in (*DIMENSIONS, 'at', 'processed')))
        matches = consumed == revision['state']['processed']
        out.update(impulse_accounting_error=impulse_error,
                   recovery_continuity_error=continuity_error,
                   processed_matches=matches,
                   no_unexplained_jump=impulse_error == 0.0 and continuity_error <= 1e-10,
                   ok=impulse_error == 0.0 and continuity_error <= 1e-10 and matches)
    except (KeyError, TypeError, ValueError) as exc:
        out['reason'] = type(exc).__name__
    return out


def _reconcile(parent_timeline, timeline):
    old = {row['event_id']: row for row in parent_timeline}
    entries = []
    for row in timeline:
        prior = old.get(row['event_id'])
        entries.append({
            'event_id': row['event_id'],
            'parent_rule': prior['rule'] if prior else None,
            'revision_rule': row['rule'],
            'parent_delta': dict(prior['delta']) if prior else dict.fromkeys(DIMENSIONS, 0.0),
            'revision_delta': dict(row['delta']),
            'changed': bool(prior is None or prior['rule'] != row['rule']
                            or prior['delta'] != row['delta'])})
    return entries


def _base_of(ledger, record):
    current, seen = record, set()
    while current.get('branch') != 'base':
        pid = _text(current.get('parent_revision_id'))
        if not pid or pid in seen or pid not in ledger['revisions']:
            raise ValueError('revision lineage broken at %s' % current.get('revision_id'))
        seen.add(pid)
        current = ledger['revisions'][pid]
    return current


def _provenance(kernel, base_id, chain):
    return {'method': 'deterministic_replay_of_recorded_events',
            'kernel_version': kernel.VERSION, 'algorithm_version': ALGORITHM_VERSION,
            'llm_reinterpretation': False,
            'correction_chain': [entry['correction_id'] for entry in chain],
            'base_revision_id': base_id}


def _stamp(record, kernel):
    record['checks'] = verify_revision(record)
    trajectories.stamp(record, trajectories.V1_REFERENCE if kernel is legacy
                       else trajectories.HISTORICAL_REPLAY)
    return record


# ── 基础 run 与修订计划 ─────────────────────────────────────────────


def build_base_run(events, *, start_at, end_at=None, kernel=DEFAULT_KERNEL,
                   tick_seconds=None, ts=None):
    """纯函数：无纠正的确定性重放，产出版本链的根（branch=base）。

    end_at 缺省取事件最大有效时间；窗口/排序/去重口径与 emotion_core.replay
    一致。事件原样存档，供后续 revision 全量重放与审计。不写任何东西。
    """
    module = _kernel(kernel)
    start = legacy._number(start_at, 'start_at')
    stored = copy.deepcopy(list(events))
    if end_at is None:
        times = []
        for value in stored:
            try:
                _, ts_value, effective = legacy._event_header(value)
            except ValueError:
                continue
            if ts_value > 0 and effective > 0:
                times.append(effective)
        end = max(times) if times else start
    else:
        end = legacy._number(end_at, 'end_at')
    tick = None if tick_seconds is None else legacy._number(tick_seconds, 'tick_seconds')
    result = _replay(stored, [], module, start, end, tick)
    again = _replay(copy.deepcopy(stored), [], module, start, end, tick)
    record = {
        'revision_id': 'emo_base_' + _sha(module.VERSION, start, end, tick, _canonical(stored)),
        'parent_revision_id': '', 'correction_id': '', 'branch': 'base',
        'history': 'preserved', 'ts': time.time() if ts is None else float(ts),
        'kernel_version': module.VERSION, **_kernel_hashes(module),
        'start_at': start, 'end_at': end, 'tick_seconds': tick,
        'events': stored, 'corrections': [],
        'state': result['state'], 'timeline': result['timeline'],
        'replayed_event_ids': result['replayed_event_ids'],
        'excluded': result['excluded'], 'event_count': len(result['timeline']),
        'status': 'planned', 'expression_control': False,
        'algorithm_version': ALGORITHM_VERSION,
        'provenance': _provenance(module, '', []), 'limitations': list(LIMITATIONS),
    }
    record['provenance']['base_revision_id'] = record['revision_id']
    _stamp(record, module)
    record['checks']['deterministic_replay'] = result == again
    record['checks']['ok'] = record['checks']['ok'] and result == again
    return record


def commit_base_run(ledger, run):
    """把基础 run 追加进内存账本。重复 ID / 内容哈希不符一律拒绝。"""
    _check_ledger(ledger)
    if not isinstance(run, dict) or run.get('branch') != 'base':
        raise ValueError('base run must be a base record from build_base_run')
    rid = _text(run.get('revision_id'))
    if not rid:
        raise ValueError('base run requires revision_id')
    if rid in ledger['revisions']:
        raise ValueError('revision already exists; history is append-only: %s' % rid)
    expected = 'emo_base_' + _sha(run.get('kernel_version'), run.get('start_at'),
                                  run.get('end_at'), run.get('tick_seconds'),
                                  _canonical(run.get('events') or []))
    if rid != expected:
        raise ValueError('base revision_id does not match its content')
    record = copy.deepcopy(run)
    record['status'] = 'committed'
    ledger['revisions'][rid] = record
    ledger['order'].append(rid)
    return copy.deepcopy(record)


def plan_revision(ledger, parent_revision_id, correction, *, evidence_overrides=None,
                  tick_seconds=None, ts=None):
    """纯函数：消费 M05 修正记录，重算出新 revision 计划，不写账本。

    correction 必须是 invalidation_dag.correction() 的记录（observation_invalid
    或 interpretation_revised）。新状态由「父链累计纠正 + 本次纠正」下的
    完整确定性重放得出，再经 dag.plan_emotion_revision 生成内容寻址的分支
    ID；调用方给出的任何状态都不被直接采信。tick_seconds=None 继承父 run。
    """
    _check_ledger(ledger)
    parent_id = _text(parent_revision_id)
    parent = ledger['revisions'].get(parent_id)
    if parent is None:
        raise ValueError('parent_revision_not_found: %s' % parent_id)
    if not isinstance(correction, dict):
        raise ValueError('correction must be a record from invalidation_dag.correction')
    kind = correction.get('kind')
    if kind not in EMOTION_CORRECTION_KINDS:
        raise ValueError('correction kind does not target emotion evidence: %s' % kind)
    target, why = _text(correction.get('target_id')), _text(correction.get('reason'))
    cid = _text(correction.get('correction_id'))
    if not target or not why or not cid:
        raise ValueError('correction requires correction_id, target_id and reason')
    base = _base_of(ledger, parent)
    if target not in {_text(e.get('id')) for e in base['events']}:
        raise ValueError('correction_target_not_in_events: %s' % target)
    chain = list(parent.get('corrections') or [])
    if any(entry.get('correction_id') == cid for entry in chain):
        raise ValueError('correction_already_applied: %s' % cid)
    overrides = None
    if kind == 'interpretation_revised':
        overrides = _checked_overrides(evidence_overrides)
    elif evidence_overrides:
        raise ValueError('observation_invalid does not reinterpret evidence')
    chain.append({'correction_id': cid, 'kind': kind, 'target_id': target,
                  'reason': why, 'evidence_overrides': overrides})
    _resolve_chain(chain)
    kernel = _kernel(parent['kernel_version'])
    tick = parent['tick_seconds'] if tick_seconds is None else tick_seconds
    at = time.time() if ts is None else float(ts)
    result = _replay(base['events'], chain, kernel, parent['start_at'], parent['end_at'], tick)
    again = _replay(copy.deepcopy(base['events']), chain, kernel,
                    parent['start_at'], parent['end_at'], tick)
    branch = dag.plan_emotion_revision(parent_id, cid, result['state'], ts=at)
    record = {
        'revision_id': branch['new_revision_id'],
        'parent_revision_id': parent_id, 'correction_id': cid,
        'branch': branch['branch'], 'history': branch['history'], 'ts': at,
        'kernel_version': kernel.VERSION, **_kernel_hashes(kernel),
        'start_at': parent['start_at'], 'end_at': parent['end_at'], 'tick_seconds': tick,
        'base_revision_id': base['revision_id'], 'corrections': chain,
        'state': result['state'], 'timeline': result['timeline'],
        'replayed_event_ids': result['replayed_event_ids'],
        'excluded': result['excluded'], 'event_count': len(result['timeline']),
        'parent_state': copy.deepcopy(parent['state']),
        'delta': {k: result['state'][k] - parent['state'][k] for k in DIMENSIONS},
        'event_reconciliation': _reconcile(parent.get('timeline') or [], result['timeline']),
        'status': 'planned', 'expression_control': False,
        'algorithm_version': ALGORITHM_VERSION,
        'provenance': _provenance(kernel, base['revision_id'], chain),
        'limitations': list(LIMITATIONS),
    }
    _stamp(record, kernel)
    record['checks']['deterministic_replay'] = result == again
    record['checks']['ok'] = record['checks']['ok'] and result == again
    return record


def commit_revision(ledger, plan):
    """把 revision 计划追加进内存账本；旧 revision 一个字节都不动。

    分支 ID 必须恰好等于 parent+correction+state 的内容哈希（M05 计划口径），
    被篡改的状态在这里被拒绝；父 revision 缺失同样拒绝。
    """
    _check_ledger(ledger)
    if not isinstance(plan, dict) or plan.get('branch') != 'revision':
        raise ValueError('revision plan must come from plan_revision')
    rid = _text(plan.get('revision_id'))
    parent_id = _text(plan.get('parent_revision_id'))
    cid = _text(plan.get('correction_id'))
    if not rid or not parent_id or not cid:
        raise ValueError('revision requires revision_id, parent_revision_id and correction_id')
    if parent_id not in ledger['revisions']:
        raise ValueError('parent_revision_not_found: %s' % parent_id)
    if rid in ledger['revisions']:
        raise ValueError('revision already exists; history is append-only: %s' % rid)
    expected = dag.plan_emotion_revision(parent_id, cid, plan.get('state'),
                                         ts=plan.get('ts'))['new_revision_id']
    if rid != expected:
        raise ValueError('revision_id does not match parent/correction/state content')
    record = copy.deepcopy(plan)
    record['status'] = 'committed'
    ledger['revisions'][rid] = record
    ledger['order'].append(rid)
    return copy.deepcopy(record)


# ── 纯读视图：GET 不得启动任何写流程 ───────────────────────────────


def get_revision(ledger, revision_id):
    _check_ledger(ledger)
    record = ledger['revisions'].get(_text(revision_id))
    return copy.deepcopy(record) if record is not None else None


def revision_lineage(ledger, revision_id):
    """从根到该 revision 的版本链（含自身）；断链大声报错。"""
    _check_ledger(ledger)
    rid = _text(revision_id)
    if rid not in ledger['revisions']:
        raise ValueError('revision_not_found: %s' % rid)
    chain, seen = [], set()
    while rid:
        if rid in seen or rid not in ledger['revisions']:
            raise ValueError('revision lineage broken at %s' % rid)
        seen.add(rid)
        chain.append(rid)
        rid = _text(ledger['revisions'][rid].get('parent_revision_id'))
    return list(reversed(chain))


def explain_revision(ledger, revision_id):
    """回答「这次心情重算为什么变了」：纠正链 + 逐事件贡献差异 + 终态 delta。

    全部来自账本里的事实记录，不生成解释性叙事。
    """
    record = get_revision(ledger, revision_id)
    if record is None:
        raise ValueError('revision_not_found: %s' % revision_id)
    return {'revision_id': record['revision_id'],
            'parent_revision_id': record['parent_revision_id'],
            'correction_id': record['correction_id'],
            'corrections': copy.deepcopy(record.get('corrections') or []),
            'final_delta': dict(record.get('delta') or {}),
            'changed_events': [e for e in record.get('event_reconciliation') or []
                               if e['changed']],
            'replayed_events': len(record.get('replayed_event_ids') or []),
            'checks': copy.deepcopy(record.get('checks') or {}),
            'reasons': [entry['reason'] for entry in record.get('corrections') or []]}


# ── 只读桥：缺库缺文件报 absent/degraded，绝不建库建表建目录 ────────


def _data_root():
    return os.getenv('DATA_DIR', './data')


def _persona_root():
    return os.getenv('PERSONA_DATA_DIR') or os.path.join(_data_root(), 'persona')


def _cognition_path():
    return os.path.join(_persona_root(), 'cognition.db')


def _ro(path):
    if not os.path.isfile(path):
        return None
    try:
        connection = sqlite3.connect(
            Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection
    except sqlite3.Error:
        return None


def _tables(connection):
    return {str(r[0]) for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def read_revision_inputs():
    """只读收集重算输入：已记录事件、M05 修正记录、A08 撤回账本。

    事件保持原始观察口径（含撤回过的），撤回语义由纠正链表达，不改写源。
    不附影子账本的已记录评估：未附的由冻结规则重算，调用方附了的必须与
    冻结规则一致。缺库报 absent，读坏报 unreadable，绝不补造。
    """
    out = {'events': [], 'corrections': [], 'retractions': [], 'degradation': []}
    connection = _ro(_cognition_path())
    if connection is None:
        out['degradation'].append({'store': 'cognition', 'reason': 'absent'})
        return out
    try:
        tables = _tables(connection)
        if 'event' in tables:
            for row in connection.execute(
                    'SELECT id,kind,ts,session,actor_qq,topic_qq,source,visibility,payload,created '
                    'FROM event ORDER BY rowid'):
                event = dict(row)
                try:
                    event['payload'] = json.loads(event.get('payload') or '{}')
                except (ValueError, TypeError):
                    pass  # 保留原字符串，由冻结内核的 payload 门统一裁决
                out['events'].append(event)
        if 'correction_record' in tables:
            for row in connection.execute(
                    'SELECT id,kind,target_id,reason,scope,new_revision_id,ts '
                    'FROM correction_record ORDER BY id'):
                out['corrections'].append({
                    'correction_id': row['id'], 'kind': row['kind'],
                    'target_id': row['target_id'], 'reason': row['reason'],
                    'scope': row['scope'], 'new_revision_id': row['new_revision_id'],
                    'ts': row['ts']})
        if 'evidence_retraction' in tables:
            out['retractions'] = [dict(r) for r in connection.execute(
                'SELECT event_id,reason,ts FROM evidence_retraction ORDER BY event_id')]
    except sqlite3.Error:
        out['degradation'].append({'store': 'cognition', 'reason': 'unreadable'})
    finally:
        connection.close()
    return out


def retraction_corrections(retractions):
    """A08 撤回账本行 → M05 撤源修正记录。内容寻址 ID，同内容幂等。"""
    return [dag.correction('observation_invalid', row.get('event_id'),
                           row.get('reason'), ts=row.get('ts'))
            for row in (retractions or []) if isinstance(row, dict)]