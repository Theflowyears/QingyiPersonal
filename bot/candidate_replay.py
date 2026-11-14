# -*- coding: utf-8 -*-
"""S07 候选回放性能与脱敏读取（路线 §9 情绪影子 / S01 candidate_counterfactual 轨迹）。

接住 S03 的两条显式延期：
  · emotion_revision.py 第 39 行「候选回放性能与脱敏读取是 S07」；
  · emotion_revision.py 第 69 行「候选语义标签（integrated_appraisal）不参与重算；
    候选反事实属 S05/S07 口径」。
本模块只做这两件事，不碰 S03 的撤回/纠正重算，也不碰 S06 的回放矩阵。

候选反事实回放（observe-only，红线「候选不驱动状态」）：
  · 只复用冻结 emotion_core(shadow-v1) / emotion_dynamics(shadow-v2) 的原语：
    排序/去重/窗口/头校验沿用 emotion_core.replay（_event_header/_replay_key），
    恢复用 dynamics.advance，冲击用 clip+processed（与冻结冲击原语逐位同式），
    候选语义评估用 dynamics.integrated_appraisal（S01 候选轨迹的评估契约版本）。
    不自带第二套 exp/tau 动力学，不调 LLM 重新解释原话。
  · 候选标签先经源事件核对（text/kind/session/at/address_basis，沿用
    emotion_shadow.valence_samples 口径）；不匹配即隔离 source_mismatch，
    零冲击、不消费。匹配后才交给 integrated_appraisal 做反事实评估。
  · 显式确定性预算：max_events 限制施加冲击的事件数，max_steps 限制恢复步进数
    （上限沿用 emotion_core._MAX_REPLAY_STEPS）。超预算的事件记 budget_excluded
    审计行（零冲击），超步进预算大声 ValueError，绝不悄悄跳过恢复冒充验证。
  · 性能元数据全部是确定性工作计数（事件数/评估数/步进数/预算），不含墙上时钟
    测量；决策路径无 time 非确定性。ts 只作记录时间戳，不影响 state/run_id/性能。
  · 产物是 candidate_counterfactual 分支报告：expression_control 恒 False，
    counts_for_live 由 S01 契约盖章（False），绝不写实时状态、不推进表达。

脱敏读取（S04 分离：读路径绝不喂表达控制）：
  · desensitize_report 是白名单只读视图：丢弃原话/QQ/会话/事件ID/标签明细，
    只回聚合计数、数值状态、性能/审计标志与稳定 reason_codes。纯函数、深拷贝，
    不改输入；构造后再做一次残留 PII 扫描，发现泄漏大声拒绝而不是静默输出。
  · reason_codes 在「施加了脱敏」「输入降级」「预算截断」「隔离/孤儿标签」
    「审计未过」时主动出声，不沉默。
  · 只读桥 read_candidate_inputs 沿用 S03 先例（mode=ro + query_only，DATA_DIR
    调用时解析，缺库报 absent/degraded，绝不建库建表建目录）。

账本是调用方传入的内存对象（new_ledger），只追加：读函数纯读、返回深拷贝；
replay_candidates 只算不写；commit_run 拒绝重复 ID、内容哈希不符与审计未过。
本模块不建库建表建目录、不落盘、不发网络、不调模型、不触发任何 QQ 发送。
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

ALGORITHM_VERSION = 's07-1'
DIMENSIONS = dynamics.DIMENSIONS
# 预算默认值与硬上限。max_steps 上限沿用冻结内核的验证步进上限，不另造一套。
DEFAULT_MAX_EVENTS = 200
MAX_EVENTS_CEILING = 2000
DEFAULT_MAX_STEPS = legacy._MAX_REPLAY_STEPS
# 与 valence_candidates.MAX_TEXT / valence_samples 历史桥 200 字截断同口径。
MAX_TEXT = 300
_BRIDGE_TRUNCATION = 200
# 脱敏扫描关注的输入字段（原话/身份/会话/事件ID/标签明细）。
_PII_KEYS = ('text', 'actor_qq', 'topic_qq', 'session', 'event_id', 'id',
             'reason', 'model', 'dedupe')
# 输出里的内容哈希字段是模块生成的十六进制，不是用户数据；残留扫描跳过它们，
# 避免 QQ 数字串与哈希/时间戳偶然子串碰撞造成误报。
_HASH_FIELDS = ('parameter_hash', 'dynamics_parameter_hash', 'appraisal_rule_hash',
                'run_id', 'input_hash')
LIMITATIONS = (
    '候选回放是 observe-only 反事实分支：不写实时状态、expression_control 恒 False、'
    'counts_for_live=False 由 S01 契约盖章；把候选接入实时状态需主人明确点头，本模块不做。',
    '候选标签经源事件核对后才参与反事实评估；不匹配即隔离 source_mismatch，绝不驱动实时状态。',
    '性能以确定性工作计数（事件数/评估数/步进数）与显式预算保证，不含墙上时钟测量；'
    '决策路径无 time 非确定性，ts 只作记录时间戳。',
    '脱敏读取是白名单只读视图：丢弃原话/QQ/会话/事件ID/标签明细，只回聚合与标志；'
    '纯函数深拷贝，绝不喂表达控制（S04 采集/标注/回放/表达分离）。',
    '迟到/重启/同刻回放矩阵属 S06，回忆影响情感的隔离实验属 S08，本模块不覆盖。',
    '只复用冻结 emotion_core/emotion_dynamics 原语与 integrated_appraisal 候选语义，'
    '不自带第二套动力学，不调 LLM 重新解释原话。',
    '账本为调用方传入的内存对象；只读桥缺库报 absent/degraded，绝不建库建表建目录，'
    'DATA_DIR 调用时解析。本模块不落盘、不发网络、不触发 QQ 发送。',
)


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _positive_int(value, name, ceiling):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        raise ValueError('%s must be an integer in [1, %d]' % (name, ceiling))
    return value


# ── 内存账本：只追加，读函数纯读 ────────────────────────────────────


def new_ledger():
    """空白候选回放账本。调用方持有；本模块不落盘、不解析路径。"""
    return {'runs': {}, 'order': []}


def _check_ledger(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('runs'), dict)
            or not isinstance(ledger.get('order'), list)):
        raise ValueError('ledger must come from new_ledger()')


def _kernel_hashes():
    return {'parameter_hash': dynamics.parameter_hash(),
            'dynamics_parameter_hash': dynamics.dynamics_parameter_hash(),
            'appraisal_rule_hash': dynamics.appraisal_rule_hash()}


# ── 冻结内核之上的候选反事实编排（动力学/评估全部来自内核）──────────


def _evaluated_view(event):
    # 与 emotion_shadow.tick / S03 同一口径：群聊/私聊消息按 message 评估。
    view = dict(event)
    if view.get('kind') in ('group_message', 'private_message'):
        view['kind'] = 'message'
    return view


def _label_matches(event, label):
    """候选标签必须对得上源事件，否则隔离。沿用 valence_samples 核对口径。"""
    if not isinstance(label, dict):
        return False
    payload = legacy._payload(event)
    source_text = str(payload.get('text') or '').strip()[:MAX_TEXT]
    label_text = str(label.get('text') or '')
    text_matches = label_text == source_text
    # 历史桥事件只保留了前 200 字，允许标签是它的延长。
    if len(source_text) == _BRIDGE_TRUNCATION and len(label_text) > _BRIDGE_TRUNCATION:
        text_matches = label_text.startswith(source_text)
    return bool(text_matches
                and label.get('kind') == event.get('kind')
                and str(label.get('session') or '') == str(event.get('session') or '')
                and label.get('at') == event.get('ts')
                and str(label.get('address_basis') or '') == str(payload.get('address_basis') or ''))


def _advance_to(state, target, tick, steps, ceiling):
    """与 emotion_core._replay_advance 同构的 tick 编排，advance 用冻结内核。

    steps 计入每一次真实 advance 调用（含 tick=None 的解析恢复），作为确定性
    工作量；超过 ceiling 大声拒绝，不悄悄跳过步进。
    """
    if tick is None or target <= state['at']:
        if steps >= ceiling:
            raise ValueError('replay step budget exceeded')
        return dynamics.advance(state, target), steps + 1
    origin = state['at']
    elapsed = target - origin
    if elapsed > tick and (origin + tick <= origin or target - tick >= target):
        raise ValueError('tick_seconds is below timestamp precision')
    ratio = elapsed / tick
    if not math.isfinite(ratio) or ratio > ceiling - steps:
        raise ValueError('replay step budget exceeded')
    count = max(1, math.ceil(ratio))
    if count > ceiling - steps:
        raise ValueError('replay step budget exceeded')
    for index in range(1, count + 1):
        # 从段起点计算，避免累计加法误差；最后一步精确落在事件/窗口终点。
        at = target if index == count else min(target, origin + index * tick)
        if at <= state['at']:
            raise ValueError('tick_seconds is below timestamp precision')
        state = dynamics.advance(state, at)
        steps += 1
        if at == target:
            break
    return state, steps


def _impulse(state, assessment):
    """clip(x+delta) 与 processed+1：与冻结内核的冲击原语逐位同式。"""
    result = dict(state)
    for key in DIMENSIONS:
        low, high = dynamics.PARAMETERS['bounds'][key]
        result[key] = min(high, max(low, result[key] + assessment['delta'][key]))
    result['processed'] += 1
    return result


def _excluded_assessment(rule, reason, event, exclusion=''):
    return {'rule': rule, 'delta': dict.fromkeys(DIMENSIONS, 0.0), 'ignored': True,
            'reason': reason, 'confidence': 0.0, 'synthetic': legacy._synthetic(event),
            'exclusion_reason': exclusion, 'evidence_origin': ''}


def _row(eid, kind, before, assessment, after, consumed, included, label):
    # 行里只留规则/数值/标志与标签的 stance/state 枚举，不留原话/标签理由明细。
    return {'event_id': eid, 'kind': kind, 'at': before['at'],
            'rule': assessment['rule'], 'delta': dict(assessment['delta']),
            'before': copy.deepcopy(before), 'after': copy.deepcopy(after),
            'ignored': assessment['ignored'], 'reason': assessment['reason'],
            'confidence': assessment['confidence'],
            'exclusion_reason': assessment.get('exclusion_reason', ''),
            'evidence_origin': assessment.get('evidence_origin', ''),
            'synthetic': assessment['synthetic'],
            'consumed': consumed, 'included': included,
            'stance': (label or {}).get('stance'),
            'label_state': (label or {}).get('state')}


def _replay(events, labels, start, end, tick, max_events, ceiling):
    try:
        ordered = []
        for index, value in enumerate(events):
            header = legacy._event_header(value)
            ordered.append((legacy._replay_key(header, value, index), header, value))
    except TypeError:
        raise ValueError('events must be an iterable of event dicts') from None
    ordered.sort(key=lambda item: item[0])
    event_ids = {header[0] for _, header, _ in ordered}
    state = dynamics.initial_state(start)
    steps = appraisals = consumed = 0
    timeline, seen, excluded = [], set(), {}
    stance_counts, state_counts = {}, {}
    included_count = synthetic_count = quarantined = 0
    for _, (eid, ts, effective), value in ordered:
        label = labels.get(eid)
        rule = reason = None
        if eid in seen:
            rule, reason = 'duplicate_event', 'Event ID was already encountered; no second impulse.'
        elif ts <= 0 or effective <= 0:
            rule, reason = 'unknown_timestamp', 'Nonpositive event time is unknown; excluded.'
        elif not (start <= effective <= end and
                  (('effective_ts' in value and ts <= end) or start <= ts <= end)):
            rule, reason = 'outside_window', 'Effective event time lies outside the replay window.'
        elif consumed >= max_events:
            rule, reason = 'budget_exceeded', 'Replay event budget reached; no further impulse.'
        seen.add(eid)
        if rule is not None:
            assessment = _excluded_assessment(rule, reason, value)
            timeline.append(_row(eid, value.get('kind'), state, assessment, state,
                                 False, False, label))
            excluded[rule] = excluded.get(rule, 0) + 1
            continue
        if label is not None and not _label_matches(value, label):
            assessment = _excluded_assessment(
                'quarantine', 'Candidate label does not match its source event.',
                value, 'source_mismatch')
            timeline.append(_row(eid, value.get('kind'), state, assessment, state,
                                 False, False, label))
            excluded['quarantine'] = excluded.get('quarantine', 0) + 1
            quarantined += 1
            continue
        view = _evaluated_view(value)
        assessment = dynamics.integrated_appraisal(view, label)
        appraisals += 1
        recorded = value.get('appraisal')
        if recorded is not None and recorded != dynamics.appraise(view):
            raise ValueError('recorded appraisal does not match frozen rules: %s' % eid)
        exclusion = assessment.get('exclusion_reason', '')
        before, steps = _advance_to(state, effective, tick, steps, ceiling)
        state = _impulse(before, assessment)
        consumed += 1
        included = not exclusion
        included_count += int(included)
        synthetic_count += int(bool(assessment.get('synthetic')))
        if label is not None:
            stance = str(label.get('stance') or 'uncertain')
            lstate = str(label.get('state') or 'pending')
            stance_counts[stance] = stance_counts.get(stance, 0) + 1
            state_counts[lstate] = state_counts.get(lstate, 0) + 1
        timeline.append(_row(eid, value.get('kind'), before, assessment, state,
                             True, included, label))
    state, steps = _advance_to(state, end, tick, steps, ceiling)
    rule_counts, exclusion_counts = {}, {}
    for row in timeline:
        rule_counts[row['rule']] = rule_counts.get(row['rule'], 0) + 1
        if row['exclusion_reason']:
            exclusion_counts[row['exclusion_reason']] = (
                exclusion_counts.get(row['exclusion_reason'], 0) + 1)
    orphan = sorted(eid for eid in labels if eid not in event_ids)
    stats = {'total': len(timeline), 'included': included_count,
             'excluded': len(timeline) - included_count, 'consumed': consumed,
             'synthetic': synthetic_count, 'quarantined': quarantined,
             'orphan_labels': len(orphan),
             'rule_counts': dict(sorted(rule_counts.items())),
             'exclusion_counts': dict(sorted(exclusion_counts.items())),
             'stance_counts': dict(sorted(stance_counts.items())),
             'label_state_counts': dict(sorted(state_counts.items()))}
    budget_excluded = excluded.get('budget_exceeded', 0)
    performance = {'max_events': max_events, 'max_steps': ceiling,
                   'events_considered': len(ordered), 'events_replayed': consumed,
                   'events_excluded': len(timeline) - consumed,
                   'budget_excluded': budget_excluded,
                   'advance_steps': steps, 'appraisals': appraisals,
                   'work_units': appraisals + steps,
                   'work_units_max': max_events + ceiling,
                   'truncated': budget_excluded > 0,
                   'bounded_work': appraisals <= max_events and steps <= ceiling,
                   'deterministic': True}
    return {'state': state, 'timeline': timeline, 'excluded': excluded,
            'stats': stats, 'performance': performance, 'orphan_labels': orphan}


# ── 审计：冲击对账与恢复连续性，抓无因跳变 ─────────────────────────


def verify_run(run):
    """纯读审计：从时间线复算每一步，回答「状态变化是否都有因」。

    冲击对账要求 0 误差（after == clip(before + delta) 逐位一致）；恢复
    连续性允许 1e-10 浮点尾差。结构损坏/越界状态如实报 ok=False，不抛异常冒充通过。
    """
    out = {'ok': False, 'impulse_accounting_error': None,
           'recovery_continuity_error': None, 'processed_matches': False,
           'no_unexplained_jump': False, 'reason': ''}
    try:
        previous = dynamics.initial_state(run['start_at'])
        impulse_error = continuity_error = 0.0
        consumed = 0
        for row in run.get('timeline') or []:
            expected_before = dynamics.advance(previous, row['at'])
            continuity_error = max(continuity_error, *(
                abs(expected_before[k] - row['before'][k]) for k in (*DIMENSIONS, 'at')))
            if row.get('consumed'):
                recomputed = _impulse(row['before'], {'delta': row['delta']})
                impulse_error = max(impulse_error, *(
                    abs(recomputed[k] - row['after'][k]) for k in (*DIMENSIONS, 'at', 'processed')))
                consumed += 1
            else:
                if row['before'] != row['after'] or any(row['delta'].values()):
                    impulse_error = max(impulse_error, 1.0)
            previous = row['after']
        final = dynamics.advance(previous, run['end_at'])
        continuity_error = max(continuity_error, *(
            abs(final[k] - run['state'][k]) for k in (*DIMENSIONS, 'at', 'processed')))
        matches = consumed == run['state']['processed']
        out.update(impulse_accounting_error=impulse_error,
                   recovery_continuity_error=continuity_error,
                   processed_matches=matches,
                   no_unexplained_jump=impulse_error == 0.0 and continuity_error <= 1e-10,
                   ok=impulse_error == 0.0 and continuity_error <= 1e-10 and matches)
    except (KeyError, TypeError, ValueError) as exc:
        out['reason'] = type(exc).__name__
    return out


# ── 候选反事实回放：纯函数，只算不写 ───────────────────────────────


def replay_candidates(events, labels=None, *, start_at, end_at=None,
                      max_events=DEFAULT_MAX_EVENTS, max_steps=DEFAULT_MAX_STEPS,
                      tick_seconds=None, ts=None):
    """纯函数：在显式确定性预算下，用冻结内核 + 候选语义重放反事实分支。

    end_at 缺省取事件最大有效时间；窗口/排序/去重口径与 emotion_core.replay 一致。
    labels 是 event_id → 候选行（valence_candidates.candidate 口径）；先经源事件
    核对再参与 integrated_appraisal。事件与标签原样存档供审计与内容哈希，但脱敏
    读取会把它们整体丢弃。不写任何东西，不推进表达，不计在线观察。
    """
    start = legacy._number(start_at, 'start_at')
    stored = copy.deepcopy(list(events))
    if labels is not None and not isinstance(labels, dict):
        raise ValueError('labels must be a dict of event_id -> candidate row')
    label_map = copy.deepcopy(dict(labels or {}))
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
    if start < 0 or end < start:
        raise ValueError('start_at/end_at must define a nonnegative ordered window')
    tick = None if tick_seconds is None else legacy._number(tick_seconds, 'tick_seconds')
    if tick is not None and tick <= 0:
        raise ValueError('tick_seconds must be positive')
    max_events = _positive_int(max_events, 'max_events', MAX_EVENTS_CEILING)
    ceiling = _positive_int(max_steps, 'max_steps', legacy._MAX_REPLAY_STEPS)
    if tick is not None and (end - start) / tick > ceiling:
        raise ValueError('tick_seconds requires too many replay steps')
    result = _replay(stored, label_map, start, end, tick, max_events, ceiling)
    again = _replay(copy.deepcopy(stored), copy.deepcopy(label_map), start, end,
                    tick, max_events, ceiling)
    at = time.time() if ts is None else float(ts)
    input_hash = _sha(_canonical(stored), _canonical(label_map))
    record = {
        'run_id': 'cand_' + _sha(dynamics.SEMANTIC_VERSION, dynamics.VERSION, start, end,
                                 tick, max_events, ceiling, input_hash),
        'branch': 'candidate', 'history': 'preserved', 'ts': at,
        'status': 'planned', 'expression_control': False,
        'algorithm_version': ALGORITHM_VERSION,
        'dynamics_version': dynamics.VERSION, 'semantic_version': dynamics.SEMANTIC_VERSION,
        **_kernel_hashes(),
        'start_at': start, 'end_at': end, 'tick_seconds': tick,
        'max_events': max_events, 'max_steps': ceiling, 'input_hash': input_hash,
        'events': stored, 'labels': label_map,
        'state': result['state'], 'timeline': result['timeline'],
        'excluded': result['excluded'], 'stats': result['stats'],
        'performance': result['performance'], 'orphan_labels': result['orphan_labels'],
        'provenance': {'method': 'deterministic_bounded_candidate_replay',
                       'kernel_version': dynamics.VERSION, 'algorithm_version': ALGORITHM_VERSION,
                       'llm_reinterpretation': False, 'input_hash': input_hash},
        'limitations': list(LIMITATIONS),
    }
    trajectories.stamp(record, trajectories.CANDIDATE_COUNTERFACTUAL)
    record['checks'] = verify_run(record)
    record['checks']['deterministic_replay'] = result == again
    record['checks']['ok'] = record['checks']['ok'] and result == again
    return record


def commit_run(ledger, run):
    """把候选回放追加进内存账本；旧 run 一个字节都不动。

    run_id 必须恰好等于内容哈希（事件+标签+窗口+预算），被篡改的内容在这里被
    拒绝；审计（冲击对账/恢复连续性）未过同样拒绝，不把无因跳变挂进账本。
    """
    _check_ledger(ledger)
    if not isinstance(run, dict) or run.get('branch') != 'candidate':
        raise ValueError('candidate run must come from replay_candidates')
    rid = _text(run.get('run_id'))
    if not rid:
        raise ValueError('candidate run requires run_id')
    if rid in ledger['runs']:
        raise ValueError('run already exists; history is append-only: %s' % rid)
    input_hash = _sha(_canonical(run.get('events') or []), _canonical(run.get('labels') or {}))
    expected = 'cand_' + _sha(run.get('semantic_version'), run.get('dynamics_version'),
                              run.get('start_at'), run.get('end_at'), run.get('tick_seconds'),
                              run.get('max_events'), run.get('max_steps'), input_hash)
    if rid != expected:
        raise ValueError('run_id does not match its content')
    if not verify_run(run)['ok']:
        raise ValueError('candidate run failed impulse/recovery audit; refusing to commit')
    record = copy.deepcopy(run)
    record['status'] = 'committed'
    ledger['runs'][rid] = record
    ledger['order'].append(rid)
    return copy.deepcopy(record)


def get_run(ledger, run_id):
    _check_ledger(ledger)
    record = ledger['runs'].get(_text(run_id))
    return copy.deepcopy(record) if record is not None else None


# ── 脱敏读取：白名单只读视图，绝不喂表达控制 ───────────────────────


def _scan_pii(node, pii, flags):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'text' and isinstance(value, str) and value.strip():
                flags['text'] = True
            if key in ('actor_qq', 'topic_qq') and _text(value):
                flags['actor'] = True
            if key == 'session' and _text(value):
                flags['session'] = True
            if key in ('event_id', 'id') and _text(value):
                flags['ids'] = True
            if key in ('candidate', 'labels', 'stance', 'label_state') and value:
                flags['labels'] = True
            if key in _PII_KEYS and isinstance(value, str) and len(value.strip()) >= 4:
                pii.add(value.strip())
            _scan_pii(value, pii, flags)
    elif isinstance(node, list):
        for item in node:
            _scan_pii(item, pii, flags)


def _string_values(node, acc):
    # 只收集输出的字符串值/键（模块枚举与静态文案），数值与哈希不参与残留扫描，
    # 避免 QQ 数字串与时间戳/哈希偶然子串碰撞误报。
    if isinstance(node, str):
        acc.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in _HASH_FIELDS:
                continue
            if isinstance(key, str):
                acc.append(key)
            _string_values(value, acc)
    elif isinstance(node, list):
        for item in node:
            _string_values(item, acc)


def _rows_of(report):
    rows = report.get('timeline')
    if rows is None:
        rows = report.get('rows')
    if rows is None:
        rows = report.get('recent')
    return rows or []


def desensitize_report(report):
    """纯读脱敏视图：只回聚合/数值/标志，丢弃原话/QQ/会话/事件ID/标签明细。

    只接受 candidate_counterfactual 轨迹报告（本模块的 run 或 valence_samples
    口径），从结构上保证读路径不碰实时/表达报告（S04 分离）。深拷贝、不改输入；
    白名单构造后再做残留 PII 扫描，发现泄漏大声 ValueError，绝不静默输出。
    """
    if not isinstance(report, dict):
        raise ValueError('desensitized read requires a candidate report dict')
    if report.get('trajectory') != trajectories.CANDIDATE_COUNTERFACTUAL:
        raise ValueError('desensitized read is only for candidate_counterfactual reports')
    pii, flags = set(), {}
    _scan_pii(report, pii, flags)
    rows = _rows_of(report)
    rule_counts, exclusion_counts, stance_counts, state_counts = {}, {}, {}, {}
    included = excluded = synthetic = quarantined = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        appraisal = row.get('appraisal') if isinstance(row.get('appraisal'), dict) else {}
        rule = row.get('rule') or appraisal.get('rule') or 'unknown'
        rule_counts[rule] = rule_counts.get(rule, 0) + 1
        exclusion = row.get('exclusion_reason') or appraisal.get('exclusion_reason') or ''
        if exclusion:
            exclusion_counts[exclusion] = exclusion_counts.get(exclusion, 0) + 1
            if exclusion == 'source_mismatch':
                quarantined += 1
        candidate = row.get('candidate') if isinstance(row.get('candidate'), dict) else {}
        stance = row.get('stance') or candidate.get('stance')
        if stance:
            stance_counts[stance] = stance_counts.get(stance, 0) + 1
        lstate = row.get('label_state') or candidate.get('state')
        if lstate:
            state_counts[lstate] = state_counts.get(lstate, 0) + 1
        if row.get('included'):
            included += 1
        else:
            excluded += 1
        if row.get('synthetic') or appraisal.get('synthetic'):
            synthetic += 1
    events = report.get('events') or []
    labels = report.get('labels') or {}
    base_stats = report.get('stats') if isinstance(report.get('stats'), dict) else {}
    total = base_stats.get('total', len(rows))
    stats = {'total': total,
             'included': base_stats.get('included', included),
             'excluded': base_stats.get('excluded', excluded),
             'synthetic': base_stats.get('synthetic', synthetic),
             'quarantined': base_stats.get('quarantined', quarantined),
             'orphan_labels': base_stats.get('orphan_labels', 0),
             'rule_counts': dict(sorted(rule_counts.items())),
             'exclusion_counts': dict(sorted(exclusion_counts.items())),
             'stance_counts': dict(sorted(stance_counts.items())),
             'label_state_counts': dict(sorted(state_counts.items()))}
    raw_state = report.get('state') if isinstance(report.get('state'), dict) else {}
    state = {k: raw_state[k] for k in (*DIMENSIONS, 'at', 'processed')
             if isinstance(raw_state.get(k), (int, float)) and not isinstance(raw_state.get(k), bool)}
    traits = raw_state.get('traits')
    if isinstance(traits, dict):
        state['traits'] = {k: v for k, v in traits.items()
                           if isinstance(v, (int, float)) and not isinstance(v, bool)}
    perf = report.get('performance')
    performance = ({k: v for k, v in perf.items() if isinstance(v, (int, float, bool))}
                   if isinstance(perf, dict) else None)
    checks = report.get('checks')
    safe_checks = ({k: v for k, v in checks.items() if isinstance(v, (int, float, bool))}
                   if isinstance(checks, dict) else None)
    codes = set()
    if flags.get('text'):
        codes.add('redacted_raw_text')
    if flags.get('actor'):
        codes.add('redacted_actor')
    if flags.get('session'):
        codes.add('redacted_session')
    if flags.get('ids'):
        codes.add('redacted_event_ids')
    if flags.get('labels'):
        codes.add('redacted_label_details')
    if not flags:
        codes.add('no_pii_detected')
    if isinstance(perf, dict) and perf.get('truncated'):
        codes.add('budget_truncated')
    degradation = report.get('degradation')
    if degradation:
        codes.add('input_degraded')
    if not labels and not flags.get('labels'):
        codes.add('no_candidate_labels')
    if stats['orphan_labels']:
        codes.add('orphan_labels')
    if quarantined or exclusion_counts.get('source_mismatch'):
        codes.add('quarantined_labels')
    if safe_checks is not None and not safe_checks.get('ok', True):
        codes.add('checks_failed')
    if total == 0 and not events:
        codes.add('empty_input')
    removed = sorted(k for k in ('events', 'labels', 'timeline', 'rows', 'recent',
                                 'corrections', 'parameters', 'orphan_labels')
                     if k in report)
    out = {
        'algorithm_version': report.get('algorithm_version', ALGORITHM_VERSION),
        'dynamics_version': report.get('dynamics_version', dynamics.VERSION),
        'semantic_version': report.get('semantic_version', dynamics.SEMANTIC_VERSION),
        'parameter_hash': report.get('parameter_hash'),
        'dynamics_parameter_hash': report.get('dynamics_parameter_hash'),
        'appraisal_rule_hash': report.get('appraisal_rule_hash'),
        'run_id': report.get('run_id'),
        'branch': report.get('branch', 'candidate'),
        'status': report.get('status'),
        'start_at': report.get('start_at'),
        'end_at': report.get('end_at'),
        'tick_seconds': report.get('tick_seconds'),
        'state': state, 'stats': stats, 'performance': performance, 'checks': safe_checks,
        'reason_codes': sorted(codes),
        'redaction': {'applied': True, 'removed_fields': removed, 'pii_scan_passed': True},
        'limitations': list(report.get('limitations') or LIMITATIONS),
        'expression_control': False,
    }
    if degradation is not None:
        out['degradation_count'] = len(degradation) if isinstance(degradation, (list, dict)) else 0
    trajectories.stamp(out, trajectories.CANDIDATE_COUNTERFACTUAL)
    # 残留扫描：输出字符串面（跳过哈希）不得包含任何已知输入 PII。
    acc = []
    _string_values(out, acc)
    surface = '\n'.join(acc)
    for secret in sorted(pii):
        if secret in surface:
            raise ValueError('desensitized output would leak redacted input; refusing')
    return out


def read_desensitized(ledger, run_id):
    """账本只读 + 脱敏：GET 不得启动任何写流程，缺 run 大声报错。"""
    record = get_run(ledger, run_id)
    if record is None:
        raise ValueError('run_not_found: %s' % run_id)
    return desensitize_report(record)


# ── 只读桥：缺库缺文件报 absent/degraded，绝不建库建表建目录 ────────


def _data_root():
    return os.getenv('DATA_DIR', './data')


def _persona_root():
    return os.getenv('PERSONA_DATA_DIR') or os.path.join(_data_root(), 'persona')


def _cognition_path():
    return os.path.join(_persona_root(), 'cognition.db')


def _candidates_path():
    return os.path.join(_persona_root(), 'valence_candidates.db')


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


def read_candidate_inputs():
    """只读收集候选回放输入：cognition 源事件 + valence_candidates 候选标签。

    事件保持原始观察口径；标签按 event_id 索引。缺库报 absent，读坏报 unreadable，
    绝不补造、绝不建库建表建目录。返回的是原始输入（供回放评估），脱敏发生在
    输出侧 desensitize_report，读路径本身只读、不喂表达。
    """
    out = {'events': [], 'labels': {}, 'degradation': []}
    connection = _ro(_cognition_path())
    if connection is None:
        out['degradation'].append({'store': 'cognition', 'reason': 'absent'})
    else:
        try:
            if 'event' in _tables(connection):
                for row in connection.execute(
                        'SELECT id,kind,ts,session,actor_qq,topic_qq,source,visibility,payload,created '
                        'FROM event ORDER BY rowid'):
                    event = dict(row)
                    try:
                        event['payload'] = json.loads(event.get('payload') or '{}')
                    except (ValueError, TypeError):
                        pass  # 保留原字符串，由冻结内核的 payload 门统一裁决
                    out['events'].append(event)
        except sqlite3.Error:
            out['degradation'].append({'store': 'cognition', 'reason': 'unreadable'})
        finally:
            connection.close()
    cand = _ro(_candidates_path())
    if cand is None:
        out['degradation'].append({'store': 'candidates', 'reason': 'absent'})
    else:
        try:
            if 'candidate' in _tables(cand):
                for row in cand.execute(
                        'SELECT event_id,at,created,session,kind,text,address_basis,lexical,'
                        'stance,confidence,reason,model,state,attempts,updated '
                        'FROM candidate ORDER BY event_id'):
                    label = dict(row)
                    out['labels'][label['event_id']] = label
        except sqlite3.Error:
            out['degradation'].append({'store': 'candidates', 'reason': 'unreadable'})
        finally:
            cand.close()
    return out


def desensitized_replay(*, start_at, end_at=None, max_events=DEFAULT_MAX_EVENTS,
                        max_steps=DEFAULT_MAX_STEPS, tick_seconds=None, ts=None):
    """一次性脱敏读取：只读桥 → 预算内候选回放 → 白名单脱敏视图。

    全程只读、不落盘、不喂表达；输入降级时 reason_codes 主动出声 input_degraded。
    """
    inputs = read_candidate_inputs()
    run = replay_candidates(inputs['events'], inputs['labels'], start_at=start_at,
                            end_at=end_at, max_events=max_events, max_steps=max_steps,
                            tick_seconds=tick_seconds, ts=ts)
    view = desensitize_report(run)
    if inputs['degradation']:
        view['reason_codes'] = sorted(set(view['reason_codes']) | {'input_degraded'})
        view['degradation_count'] = len(inputs['degradation'])
    return view