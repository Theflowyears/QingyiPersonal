# -*- coding: utf-8 -*-
"""C04 工作状态快照与三层注入（路线 §4.1 四种时间尺度 / §8.3 注入分三段）。

把「工作状态」这条秒—小时尺度的时间层落成**可执行契约**（不接进真实链路）：

  1. build_snapshot：从真实输入（事件/情绪状态/承诺/目标/等待中工具结果/关系信号）
     纯函数构建内容寻址快照。注意负荷、工具预算、等待项各有显式上限，超限大声
     登记 budget，绝不静默截断。now 必填，本模块不 import 时钟，决策路径可复现。
  2. render_injection：三层注入渲染。身份层稳定可缓存（缓存 key 走 M05 五分量，
     含 epoch 与修订版本，绝不按日期缓存）；状态层有界、携带 state_version 与
     时间戳，新承诺/目标/显著状态变化使 digest 变化、旧缓存立即失效；证据层只
     接受 R06 render_evidence 的输出形状，逐条带 verdict_refs，场景 ID 内容寻址
     可核对，绝不伪造聊天原文之外的经历。
  3. 失效纪律：任何注入层入口都过 M05 invalidation_dag.read_gate——invalidated/
     superseded/needs_reassessment/范围收紧/落后 epoch 的来源一律挡下并大声登记。
     缓存 key 复用 M05 cache_key（缺分量直接拒绝，不给默认值）。

硬边界（与 L01/R06 同一范式，如实声明）：
- 纯函数、零写入。不打开任何库、不写任何账本、不解析路径、不 import os/数据库驱动。
  states/epoch 由调用方经 M05 只读桥（read_invalidation_state）取得后传入；
  缺输入一律 absent/degraded 如实降级，绝不建库、绝不补造。
- 承诺/目标只消费 M06 cognition_writer.commit 已落账的行（self_model commitment/
  goal 表形状）；本模块零写入，新承诺只能经 M06 单一写入者进来。临时状态（state）
  按 M06 的 carve-out（_STATE_NOTE）不落认知账本，只进本快照。文本提取复用
  cw._text 的同一套口径，不另长第二套。
- 情绪只投影内核（emotion_dynamics shadow-v2）已算好的四维状态，按冻结 bounds
  裁剪；本模块不推进动力学、不调 LLM、不生成话术。接口失败不改情绪投影，
  「今天不想说」不改身份层——工作状态与人格/关系分层，互不连坐。
- 不接 qq_openclaw_bridge（那是 C05）、不接管表达、不新增第二套「什么时候行动」
  判断（行动裁决仍归统一仲裁）、不伪造身体感受。注入层是提示词上下文，不是聊天文本。
- 快路径只读已计算快照：render_injection 不回到原始事件重算；理解/巩固/规划在
  后台（本模块不做）。关系只携带已有好感单维信号，不冒充信任/亲密全部维度。
- 确定性：无墙钟无随机，snapshot_id/state_version 内容寻址（A03 digest 口径），
  同输入同输出、输入顺序无关；只读深拷贝，不改输入，缓存以新字典返回。
"""
import copy
import json
import math

import cognition_writer as cw
import emotion_dynamics as ed
import invalidation_dag as dag
import memory_manifest as mm
import ripple_evidence as re6

ALGORITHM_VERSION = 'c04-1'
# 三层注入的顺序契约（§8.3）：身份 → 状态 → 证据。
LAYERS = ('identity', 'state', 'evidence')
# 显式资源上限（不是质量分）：超限逐条大声登记 budget，绝不静默截断。
MAX_PENDING = 8
MAX_COMMITMENTS = 8
MAX_GOALS = 8
MAX_RELATIONS = 8
MAX_STATE_CHARS = 1200
# 与 memory_depth.IDENTITY_MAX_CHARS 的默认值同口径；不 import 它（它按天写快照、
# 读墙钟，正是本契约要替换的缓存方式）。
MAX_IDENTITY_CHARS = 420
# 注意负荷投影只看最近这段真实事件；窗口是显式常量，不是墙钟。
RECENT_EVENT_WINDOW = 300.0
# 证据层预算默认复用 R06 的上限，不另立第二套。
MAX_EVIDENCE_SCENES = re6.MAX_SCENES
MAX_EVIDENCE_ENTRIES = re6.MAX_ENTRIES

# C04 自己的稳定 reason_code；read_gate 的挡下原因（invalidated/superseded/
# needs_reassessment/scope_revoked/stale_projection/...）逐字透传，不新造。
REASON_CODES = (
    'missing_object_id', 'missing_text', 'invalid_requested_at', 'future_request',
    'future_event', 'invalid_event_ts', 'commitment_not_open', 'goal_not_active',
    'budget', 'evidence_shape_invalid', 'scene_id_mismatch', 'missing_verdict_refs',
    'emotion_absent', 'emotion_unreadable', 'budget_absent', 'identity_absent',
    'identity_cache_unavailable', 'stale_snapshot',
)

LIMITATIONS = (
    'C04 只做工作状态快照与三层注入的可执行契约：不接 qq_openclaw_bridge、不接进'
    '真实回复链路（接线是 C05/主 agent 的范围）。',
    '不接管表达：注入层是提示词上下文，不是聊天话术；不新增第二套「什么时候行动」'
    '判断，行动裁决仍归统一仲裁。',
    '不伪造身体感受/现实经历/他人回应：缺输入一律 absent/degraded 如实降级，绝不补造；'
    '情绪只投影内核已算好的状态，不自己推进动力学，不调 LLM，接口失败不改情绪投影。',
    '快照零写入：不打开任何库、不写任何账本；承诺/目标只消费 M06 cognition_writer.commit '
    '已落账的行，临时状态按 M06 carve-out 只进本快照，不落认知账本。',
    '失效纪律复用 M05 invalidation_dag.read_gate/cache_key：失效/待重评/范围收紧/落后 '
    'epoch 的来源不进任何注入层；缓存 key 含 epoch 与修订版本，绝不按日期缓存。',
    '证据层只接受 R06 ripple_evidence.render_evidence 的输出形状，逐条带 verdict_refs，'
    '场景 ID 内容寻址可核对；不伪造聊天原文之外的经历，不重新裁决。',
    '确定性：无墙钟无随机，now 显式传入；snapshot_id/state_version 内容寻址，同输入同输出、'
    '输入顺序无关；缓存以新字典返回，不改输入。',
    '关系只携带已有好感单维信号，不冒充信任/亲密/依赖全部维度；注意负荷/工具预算/等待项'
    '有显式上限，超限大声登记 budget。',
    '快路径只读已计算快照：render_injection 不回到原始事件重算；理解/巩固/规划在后台'
    '（本模块不做）。',
)


# ── 小工具（与 L01/R06 同一口径，非引擎）────────────────────────────


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _require_now(now):
    """now 必须显式传入：本模块不读墙上时钟（无 time import），快照可复现。"""
    if not _number(now):
        raise ValueError('work state requires an explicit numeric now; no wall clock')
    return float(now)


def _oid(row):
    return _text(row.get('object_id') or row.get('id'))


def _clip(value, bounds):
    low, high = bounds
    return min(high, max(low, float(value)))


def _gate(entry, layer, states, epoch, scope, excluded):
    """单条过 M05 最终读门：失效/待重评/范围收紧/落后 epoch 一律挡下并大声登记。

    判定完全委托 dag.read_gate（不另长第二套失效引擎）；挡下原因逐字透传。
    """
    kept, dropped = dag.read_gate([entry], states, epoch, scope)
    if kept:
        return entry
    excluded.append({'object_id': _text(entry.get('object_id')), 'layer': layer,
                     'reason_code': sorted(dropped)[0]})
    return None


def _cap(rows, limit, layer, excluded):
    """显式上限截断：超限逐条大声登记 budget，绝不静默丢。"""
    kept = rows[:limit]
    for row in rows[limit:]:
        excluded.append({'object_id': _text(row.get('object_id')), 'layer': layer,
                         'reason_code': 'budget'})
    return kept


def _sorted_unique(rows):
    """按 object_id 排序去重（首见优先）：输入顺序无关，重复转述不产生二义。"""
    unique = {}
    for row in sorted(rows, key=lambda r: _text(r.get('object_id'))):
        unique.setdefault(_text(row.get('object_id')), row)
    return [unique[oid] for oid in sorted(unique)]


# ── 快照构建：纯函数，零写入 ───────────────────────────────────────


def _project_emotion(emotion, degradation):
    """只投影内核已算好的四维状态（冻结 bounds 裁剪）；缺/坏输入如实降级，不补造。"""
    if emotion is None:
        degradation.append({'store': 'emotion', 'reason': 'absent'})
        return None
    if not isinstance(emotion, dict) or not all(
            _number(emotion.get(dim)) for dim in ed.DIMENSIONS):
        degradation.append({'store': 'emotion', 'reason': 'unreadable'})
        return None
    out = {dim: _clip(emotion[dim], ed.PARAMETERS['bounds'][dim])
           for dim in ed.DIMENSIONS}
    out['at'] = float(emotion['at']) if _number(emotion.get('at')) else None
    return out


def _project_budget(budget, degradation):
    if not isinstance(budget, dict) or not (_number(budget.get('limit'))
                                            and _number(budget.get('used'))):
        degradation.append({'store': 'budget', 'reason': 'absent'})
        return None
    limit = max(0, int(budget['limit']))
    used = max(0, int(budget['used']))
    remaining = max(0, limit - used)
    return {'limit': limit, 'used': used, 'remaining': remaining,
            'exhausted': used >= limit}


def _pending_rows(pending, at, states, epoch, scope, excluded):
    rows = []
    for raw in (pending or []):
        if not isinstance(raw, dict):
            continue
        oid = _text(raw.get('pending_id') or raw.get('object_id') or raw.get('id'))
        if not oid:
            excluded.append({'object_id': '', 'layer': 'pending',
                             'reason_code': 'missing_object_id'})
            continue
        requested = raw.get('requested_at')
        if not _number(requested):
            excluded.append({'object_id': oid, 'layer': 'pending',
                             'reason_code': 'invalid_requested_at'})
            continue
        if float(requested) > at:
            excluded.append({'object_id': oid, 'layer': 'pending',
                             'reason_code': 'future_request'})
            continue
        rows.append({'object_id': oid, 'tool': _text(raw.get('tool')),
                     'requested_at': float(requested),
                     'waiting_seconds': max(0.0, at - float(requested)),
                     'projection_epoch': epoch})
    gated = [_gate(r, 'pending', states, epoch, scope, excluded)
             for r in _sorted_unique(rows)]
    return _cap([r for r in gated if r is not None], MAX_PENDING, 'pending', excluded)


def _commitment_rows(commitments, at, states, epoch, scope, excluded):
    """承诺行必须是 M06/self_model 已落账形状；只有 open 的才是当前状态。"""
    rows = []
    for raw in (commitments or []):
        if not isinstance(raw, dict):
            continue
        oid = _oid(raw)
        if not oid:
            excluded.append({'object_id': '', 'layer': 'commitment',
                             'reason_code': 'missing_object_id'})
            continue
        text = cw._text(raw)
        if not text:
            excluded.append({'object_id': oid, 'layer': 'commitment',
                             'reason_code': 'missing_text'})
            continue
        status = _text(raw.get('status')) or 'open'
        if status != 'open':
            excluded.append({'object_id': oid, 'layer': 'commitment',
                             'reason_code': 'commitment_not_open'})
            continue
        due = float(raw['due_ts']) if _number(raw.get('due_ts')) else None
        rows.append({'object_id': oid, 'to_qq': _text(raw.get('to_qq')),
                     'text': text, 'due_ts': due,
                     'overdue': due is not None and due < at,
                     'projection_epoch': epoch})
    gated = [_gate(r, 'commitment', states, epoch, scope, excluded)
             for r in _sorted_unique(rows)]
    return _cap([r for r in gated if r is not None], MAX_COMMITMENTS, 'commitment', excluded)


def _goal_rows(goals, states, epoch, scope, excluded):
    rows = []
    for raw in (goals or []):
        if not isinstance(raw, dict):
            continue
        oid = _oid(raw)
        if not oid:
            excluded.append({'object_id': '', 'layer': 'goal',
                             'reason_code': 'missing_object_id'})
            continue
        text = cw._text(raw)
        if not text:
            excluded.append({'object_id': oid, 'layer': 'goal',
                             'reason_code': 'missing_text'})
            continue
        status = _text(raw.get('status')) or 'active'
        if status != 'active':
            excluded.append({'object_id': oid, 'layer': 'goal',
                             'reason_code': 'goal_not_active'})
            continue
        rows.append({'object_id': oid, 'text': text,
                     'motive': _text(raw.get('motive')),
                     'owner_qq': _text(raw.get('owner_qq')),
                     'projection_epoch': epoch})
    gated = [_gate(r, 'goal', states, epoch, scope, excluded)
             for r in _sorted_unique(rows)]
    return _cap([r for r in gated if r is not None], MAX_GOALS, 'goal', excluded)


def _relation_rows(relations, states, epoch, scope, excluded):
    """关系只带已有好感单维信号（迁移底线）；数值缺省如实留 None，不补造。"""
    rows = []
    for raw in (relations or []):
        if not isinstance(raw, dict):
            continue
        subject = _text(raw.get('subject_ref'))
        if not subject:
            excluded.append({'object_id': '', 'layer': 'relation',
                             'reason_code': 'missing_object_id'})
            continue
        rows.append({'object_id': subject, 'subject_ref': subject,
                     'affection_signal': float(raw['affection'])
                     if _number(raw.get('affection')) else None,
                     'projection_epoch': epoch})
    gated = [_gate(r, 'relation', states, epoch, scope, excluded)
             for r in _sorted_unique(rows)]
    return _cap([r for r in gated if r is not None], MAX_RELATIONS, 'relation', excluded)


def _recent_events(events, at, excluded):
    """最近窗口内的真实事件计数（注意负荷的旁证）；未来/坏时间戳大声登记。"""
    count = 0
    for raw in (events or []):
        if not isinstance(raw, dict):
            continue
        eid = _text(raw.get('event_id') or raw.get('id'))
        ts = raw.get('ts')
        if not _number(ts):
            excluded.append({'object_id': eid, 'layer': 'attention',
                             'reason_code': 'invalid_event_ts'})
            continue
        if float(ts) > at:
            excluded.append({'object_id': eid, 'layer': 'attention',
                             'reason_code': 'future_event'})
            continue
        if at - float(ts) <= RECENT_EVENT_WINDOW:
            count += 1
    return count


def build_snapshot(*, now, events=None, emotion=None, commitments=None, goals=None,
                   pending=None, relations=None, budget=None,
                   states=None, epoch=0, scope=''):
    """纯函数：从真实输入构建工作状态快照。不写任何账本、不打开任何库。

    now:         必填显式时间（无墙钟）。
    events:      真实事件行（self_model event 形状），只用于最近窗口计数。
    emotion:     内核已算好的情绪状态（emotion_dynamics 四维形状）；缺/坏如实降级。
    commitments: M06/self_model 已落账的承诺行；只有 open 的进当前状态。
    goals:       M06/self_model 已落账的目标行；只有 active 的进当前状态。
    pending:     等待中的工具结果 {pending_id, tool, requested_at}。
    relations:   关系信号 {subject_ref, affection}（单维，不冒充全维度）。
    budget:      工具预算 {limit, used}；缺输入如实 absent，不伪造额度。
    states/epoch/scope: 调用方经 M05 只读桥取得的失效状态/epoch/范围；
                 所有条目过 dag.read_gate，失效来源绝不进快照。

    返回内容寻址快照：{snapshot_id, algorithm_version, now, epoch, scope, status,
    attention, budget, emotion, pending, commitments, goals, relations,
    state_version, excluded, degradation, provenance}。
    """
    at = _require_now(now)
    epoch = int(epoch)
    if epoch < 0:
        raise ValueError('epoch must be nonnegative')
    scope = _text(scope)
    prior = states if isinstance(states, dict) else {}
    excluded, degradation = [], []

    emotion_out = _project_emotion(emotion, degradation)
    budget_out = _project_budget(budget, degradation)
    pending_out = _pending_rows(pending, at, prior, epoch, scope, excluded)
    commitment_out = _commitment_rows(commitments, at, prior, epoch, scope, excluded)
    goal_out = _goal_rows(goals, prior, epoch, scope, excluded)
    relation_out = _relation_rows(relations, prior, epoch, scope, excluded)
    recent = _recent_events(events, at, excluded)

    attention = {'load': emotion_out['attention'] if emotion_out else None,
                 'basis': 'emotion_kernel' if emotion_out else 'absent',
                 'recent_events': recent, 'pending_count': len(pending_out)}
    state_content = {'attention': attention, 'budget': budget_out,
                     'emotion': emotion_out, 'pending': pending_out,
                     'commitments': commitment_out, 'goals': goal_out,
                     'relations': relation_out}
    state_version = mm.digest(_canonical(state_content))[:16]
    excluded.sort(key=lambda x: (x['layer'], x['object_id'], x['reason_code']))
    payload = {
        'algorithm_version': ALGORITHM_VERSION, 'now': at, 'epoch': epoch,
        'scope': scope, 'status': 'degraded' if degradation else 'ok',
        'attention': attention, 'budget': budget_out, 'emotion': emotion_out,
        'pending': pending_out, 'commitments': commitment_out, 'goals': goal_out,
        'relations': relation_out, 'state_version': state_version,
        'excluded': excluded, 'degradation': degradation,
        'provenance': {'algorithm_version': ALGORITHM_VERSION,
                       'state_layer_note': cw._STATE_NOTE,
                       'event_window_seconds': RECENT_EVENT_WINDOW,
                       'counts': {'pending': len(pending_out),
                                  'commitments': len(commitment_out),
                                  'goals': len(goal_out),
                                  'relations': len(relation_out),
                                  'excluded': len(excluded)},
                       'limitations': list(LIMITATIONS)},
    }
    payload['snapshot_id'] = 'ws_' + mm.digest(_canonical(payload))[:14]
    return payload


# ── 三层注入渲染：只读已计算快照 ───────────────────────────────────


def _identity_layer(identity, epoch, scope, cache, excluded, degradation):
    """身份层：日内稳定可缓存，但缓存 key 走 M05 五分量（epoch/修订/范围/版本）。

    绝不按日期缓存：同一天里 epoch 推进、源修订都必须让 key 变化。缺分量时
    cache_key 直接拒绝，本层如实降级为不可缓存，不悄悄按天缓存。
    """
    text = _text(identity.get('text')) if isinstance(identity, dict) else ''
    if not text:
        degradation.append({'store': 'identity', 'reason': 'absent'})
        return {'text': '', 'cache_key': '', 'cacheable': False,
                'cache_hit': False, 'chars': 0, 'truncated': False}
    truncated = len(text) > MAX_IDENTITY_CHARS
    if truncated:
        text = text[:MAX_IDENTITY_CHARS].rstrip() + '…'
    # cache_key 显式接受 int|str epoch，但其 _text 口径把 int 0 当缺分量；
    # 用 str 编码保留初始 epoch 0，不绕开契约也不给默认值。
    key = dag.cache_key(_text(identity.get('object_revision')),
                        _text(identity.get('source_revision')),
                        str(epoch), scope, ALGORITHM_VERSION)
    if not key['ok']:
        degradation.append({'store': 'identity', 'reason': 'cache_key_unavailable'})
        excluded.append({'object_id': 'identity', 'layer': 'identity',
                         'reason_code': 'identity_cache_unavailable'})
        return {'text': text, 'cache_key': '', 'cacheable': False,
                'cache_hit': False, 'chars': len(text), 'truncated': truncated}
    hit = key['key'] in cache['identity']
    if hit:
        text = cache['identity'][key['key']]['text']
    else:
        cache['identity'][key['key']] = {'text': text}
    return {'text': text, 'cache_key': key['key'], 'cacheable': True,
            'cache_hit': hit, 'chars': len(text), 'truncated': truncated}


def _state_lines(snapshot, commitments, goals, pending, relations):
    """状态层行序即截断优先级：承诺/目标最先（显著变化必须立即生效），关系最后。"""
    lines = []
    for row in commitments:
        due = ''
        if row['due_ts'] is not None:
            due = '，期限 %.0f%s' % (row['due_ts'], '（已逾期）' if row['overdue'] else '')
        lines.append('- 承诺：对 %s「%s」%s' % (row['to_qq'] or '未指定', row['text'], due))
    for row in goals:
        motive = '（动机：%s）' % row['motive'] if row['motive'] else ''
        lines.append('- 目标：「%s」%s' % (row['text'], motive))
    emotion = snapshot['emotion']
    if emotion is not None:
        lines.append('- 情绪（内核投影，未校准）：效价 %.2f 激活 %.2f 掌控 %.2f 注意 %.2f'
                     % (emotion['valence'], emotion['arousal'],
                        emotion['control'], emotion['attention']))
    else:
        lines.append('- 情绪：缺输入，不编造（不是心情不好）')
    attention = snapshot['attention']
    load = '%.2f' % attention['load'] if attention['load'] is not None else '未知'
    lines.append('- 注意负荷：%s；近 %.0f 秒事件 %d 条；等待中 %d 项'
                 % (load, RECENT_EVENT_WINDOW, attention['recent_events'],
                    attention['pending_count']))
    budget = snapshot['budget']
    if budget is not None:
        lines.append('- 工具预算：剩 %d/%d%s'
                     % (budget['remaining'], budget['limit'],
                        '（已用尽）' if budget['exhausted'] else ''))
    else:
        lines.append('- 工具预算：缺输入，未知')
    for row in pending:
        lines.append('- 等待：%s 已等 %.0f 秒（结果未回，不算完成）'
                     % (row['tool'] or row['object_id'], row['waiting_seconds']))
    for row in relations:
        signal = ('%+.0f' % row['affection_signal']
                  if row['affection_signal'] is not None else '未知')
        lines.append('- 关系信号：%s 好感 %s（单维信号，不是信任/亲密全维度）'
                     % (row['subject_ref'], signal))
    return lines


def _state_layer(snapshot, commitments, goals, pending, relations, epoch, scope,
                 cache, max_state_chars):
    """状态层：有界、携带版本/时间戳；digest 对比缓存，显著变化立即生效。"""
    version = snapshot['state_version']
    prior = cache.get('state')
    if (isinstance(prior, dict) and prior.get('version') == version
            and prior.get('max_chars') == max_state_chars
            and prior.get('epoch') == epoch and prior.get('scope') == scope):
        return {'text': prior['text'], 'state_version': version,
                'snapshot_id': snapshot['snapshot_id'], 'now': snapshot['now'],
                'epoch': epoch, 'cache_hit': True,
                'truncated': prior['truncated'], 'dropped_lines': prior['dropped_lines'],
                'chars': len(prior['text'])}
    lines = _state_lines(snapshot, commitments, goals, pending, relations)
    header = '【当前状态】'
    kept, dropped = [], 0
    used = len(header)
    for line in lines:
        if used + 1 + len(line) <= max_state_chars:
            kept.append(line)
            used += 1 + len(line)
        else:
            dropped += 1
    text = '\n'.join([header] + kept)
    truncated = dropped > 0
    if len(text) > max_state_chars:
        # 预算连标题都放不下：硬截断并大声标记，绝不静默。
        text = text[:max(0, max_state_chars)]
        truncated = True
    cache['state'] = {'version': version, 'max_chars': max_state_chars,
                      'epoch': epoch, 'scope': scope, 'text': text,
                      'truncated': truncated, 'dropped_lines': dropped}
    return {'text': text, 'state_version': version,
            'snapshot_id': snapshot['snapshot_id'], 'now': snapshot['now'],
            'epoch': epoch, 'cache_hit': False, 'truncated': truncated,
            'dropped_lines': dropped, 'chars': len(text)}


def _valid_evidence_shape(evidence):
    """只认 R06 render_evidence 的输出形状；缺骨架一律拒绝，不部分信任。"""
    if not isinstance(evidence, dict):
        return False
    if not all(key in evidence for key in ('status', 'scenes', 'manifest',
                                           'degradation', 'provenance')):
        return False
    return evidence['status'] in re6.STATUSES and isinstance(evidence['scenes'], list)


def _evidence_layer(evidence, states, epoch, scope, excluded, degradation,
                    max_scenes, max_entries):
    """证据层：只搬 R06 场景的真实字段与 verdict_refs，逐条过 M05 读门。

    场景 ID 必须等于 'scene_' + digest(scene_key)[:14]（R06 同一配方）：
    对不上就是伪造/篡改的场景，整块大声拒绝。条目缺 verdict_refs 不得入选。
    """
    if evidence is None:
        degradation.append({'store': 'evidence', 'reason': 'absent'})
        return {'status': 'degraded', 'source_status': '', 'scenes': [],
                'degradation': [], 'counts': {'scenes': 0, 'entries': 0, 'dropped': 0}}
    if not _valid_evidence_shape(evidence):
        degradation.append({'store': 'evidence', 'reason': 'invalid_shape'})
        excluded.append({'object_id': '', 'layer': 'evidence',
                         'reason_code': 'evidence_shape_invalid'})
        return {'status': 'degraded', 'source_status': '', 'scenes': [],
                'degradation': [], 'counts': {'scenes': 0, 'entries': 0, 'dropped': 0}}

    source_status = evidence['status']
    dropped_before = len(excluded)
    scenes = []
    ordered = sorted((s for s in evidence['scenes'] if isinstance(s, dict)),
                     key=lambda s: _text(s.get('scene_key')))
    for scene in ordered:
        key = _text(scene.get('scene_key'))
        entries = scene.get('entries')
        if (scene.get('scene_id') != 'scene_' + mm.digest(key)[:14]
                or not isinstance(entries, list)
                or scene.get('entry_count') != len(entries)):
            excluded.append({'object_id': _text(scene.get('scene_id')),
                             'layer': 'evidence', 'reason_code': 'scene_id_mismatch'})
            continue
        kept_entries = []
        for entry in sorted((e for e in entries if isinstance(e, dict)),
                            key=lambda e: _text(e.get('object_id'))):
            oid = _text(entry.get('object_id'))
            if not oid:
                excluded.append({'object_id': '', 'layer': 'evidence',
                                 'reason_code': 'missing_object_id'})
                continue
            if not isinstance(entry.get('verdict_refs'), list):
                excluded.append({'object_id': oid, 'layer': 'evidence',
                                 'reason_code': 'missing_verdict_refs'})
                continue
            gated = _gate(dict(entry, projection_epoch=epoch), 'evidence',
                          states, epoch, scope, excluded)
            if gated is not None:
                kept_entries.append(copy.deepcopy(entry))
        if kept_entries:
            scenes.append({'scene_id': scene['scene_id'], 'scene_key': key,
                           'root_representative': scene.get('root_representative', ''),
                           'root_source_ids': copy.deepcopy(scene.get('root_source_ids') or []),
                           'entries': kept_entries, 'entry_count': len(kept_entries)})

    # 预算：先按条目上限截、再按场景上限截（与 R06 同序），超限大声登记。
    total = 0
    for scene in scenes:
        room = max(0, max_entries - total)
        overflow = scene['entries'][room:]
        scene['entries'] = scene['entries'][:room]
        scene['entry_count'] = len(scene['entries'])
        total += len(scene['entries'])
        for entry in overflow:
            excluded.append({'object_id': entry['object_id'], 'layer': 'evidence',
                             'reason_code': 'budget'})
    # 条目被截空/挡空的场景不再输出（其条目已逐条登记）。
    final = [s for s in scenes[:max_scenes] if s['entries']]
    for scene in scenes[max_scenes:]:
        for entry in scene['entries']:
            excluded.append({'object_id': entry['object_id'], 'layer': 'evidence',
                             'reason_code': 'budget'})

    drops = len(excluded) - dropped_before
    r06_degradation = copy.deepcopy(evidence.get('degradation') or [])
    if r06_degradation or source_status == 'degraded':
        status = 'degraded'
    elif source_status == 'conflicting':
        status = 'conflicting'
    elif not final:
        status = 'not_found'
    elif drops or source_status == 'partial':
        status = 'partial'
    else:
        status = 'sufficient'
    return {'status': status, 'source_status': source_status, 'scenes': final,
            'degradation': r06_degradation,
            'counts': {'scenes': len(final),
                       'entries': sum(s['entry_count'] for s in final),
                       'dropped': drops}}


def render_injection(snapshot, *, identity=None, evidence=None, states=None,
                     epoch=None, scope=None, cache=None,
                     max_state_chars=MAX_STATE_CHARS,
                     max_scenes=MAX_EVIDENCE_SCENES, max_entries=MAX_EVIDENCE_ENTRIES):
    """纯函数：在已计算快照上渲染三层注入。快路径只读快照，不回原始事件重算。

    snapshot:  build_snapshot 的输出（必填）。
    identity:  {text, object_revision, source_revision}；缺输入如实 absent。
    evidence:  R06 render_evidence 的输出；其它形状一律大声拒绝，绝不伪造经历。
    states/epoch/scope: 注入时刻的 M05 失效状态（缺省沿用快照构建时的 epoch/scope）。
               epoch 前进后旧快照条目按 stale_projection 挡下——必须重算快照，
               绝不让落后投影复活已撤回的依据。
    cache:     调用方持有的缓存字典（{'identity': {...}, 'state': {...}}）；
               本函数只读它，更新后的新缓存随结果返回（不改输入）。
    max_state_chars/max_scenes/max_entries: 显式资源上限，超限大声登记。

    返回 {algorithm_version, snapshot_id, now, epoch, scope, status, identity,
    state, evidence, excluded, degradation, cache, cache_hits, provenance,
    limitations}。身份层稳定可缓存；状态层携带 state_version/时间戳，显著变化
    （新承诺/目标/状态变化）使 digest 变化、旧缓存立即失效，不等第二天。
    """
    if (not isinstance(snapshot, dict) or not _text(snapshot.get('snapshot_id'))
            or not _text(snapshot.get('state_version'))
            or not _number(snapshot.get('now'))):
        raise ValueError('render_injection requires a build_snapshot result')
    at = float(snapshot['now'])
    epoch = int(snapshot['epoch']) if epoch is None else int(epoch)
    if epoch < 0:
        raise ValueError('epoch must be nonnegative')
    scope = _text(snapshot.get('scope')) if scope is None else _text(scope)
    prior = states if isinstance(states, dict) else {}
    max_state_chars = max(0, int(max_state_chars))
    max_scenes = max(0, int(max_scenes))
    max_entries = max(0, int(max_entries))
    new_cache = copy.deepcopy(cache) if isinstance(cache, dict) else {}
    new_cache.setdefault('identity', {})
    new_cache.setdefault('state', None)

    excluded, degradation = [], []
    degradation.extend(copy.deepcopy(snapshot.get('degradation') or []))
    if epoch > int(snapshot['epoch']):
        degradation.append({'store': 'work_state', 'reason': 'stale_snapshot'})

    # 快照条目在注入时刻再过一遍 M05 读门：构建后新发生的失效立即生效。
    commitments, goals, pending, relations = [], [], [], []
    for key, bucket, layer in (('commitments', commitments, 'commitment'),
                               ('goals', goals, 'goal'),
                               ('pending', pending, 'pending'),
                               ('relations', relations, 'relation')):
        for row in sorted(snapshot.get(key) or [],
                          key=lambda r: _text(r.get('object_id'))):
            gated = _gate(dict(row), layer, prior, epoch, scope, excluded)
            if gated is not None:
                bucket.append(gated)

    identity_out = _identity_layer(identity, epoch, scope, new_cache,
                                   excluded, degradation)
    state_out = _state_layer(snapshot, commitments, goals, pending, relations,
                             epoch, scope, new_cache, max_state_chars)
    evidence_out = _evidence_layer(evidence, prior, epoch, scope, excluded,
                                   degradation, max_scenes, max_entries)

    excluded.sort(key=lambda x: (x['layer'], x['object_id'], x['reason_code']))
    return {
        'algorithm_version': ALGORITHM_VERSION,
        'snapshot_id': snapshot['snapshot_id'], 'now': at, 'epoch': epoch,
        'scope': scope, 'status': 'degraded' if degradation else 'ok',
        'identity': identity_out, 'state': state_out, 'evidence': evidence_out,
        'excluded': excluded, 'degradation': degradation, 'cache': new_cache,
        'cache_hits': {'identity': identity_out['cache_hit'],
                       'state': state_out['cache_hit']},
        'provenance': {'algorithm_version': ALGORITHM_VERSION,
                       'state_layer_note': cw._STATE_NOTE,
                       'snapshot_state_version': snapshot['state_version'],
                       'limitations': list(LIMITATIONS)},
        'limitations': list(LIMITATIONS),
    }