# -*- coding: utf-8 -*-
"""L02 信念与自我认识的版本修订（路线 §5.1 Belief 修订历史 / SelfView 版本与
变更理由、§5.3 自我认识只能修订不能覆盖、§5.4 反思不得制造经历、§8.2 单一
状态写入者；stage5/self，前置 L01/M04/M05，参照 M08/M06）。

L01 把「反思只能从真实新增来源产生」证完就停手（self_reflection LIMITATIONS：
「不做 L02 信念/自我认识的版本修订」）；M05 把失效传播证完就停手（「重新评估
只能由后续显式修订事务（M08/L02）完成」）；M08 把有限巩固证完就停手（「不做
L02 长期自我认识修订」，且 self_model 目前没有 belief_revision/self_view_rev
的专用事务入口）。L02 接住这三处，把 §5.3 的流程落成一条**可执行契约**：

  提出修订 → 检索证据 → 检查反例 → 接受或暂缓 → 留版本。

  1. 修订提案只接受 L01 反思产物：候选信封**只从已提交的 L01 反思账本取**
     （sr.get_record），调用方自带的信封/证据一律忽略。目标层只允许
     belief/self_view（TARGET_KINDS），任何直接「发生过」（episode）断言
     一律 revision_not_event 拒绝；L01 产物（解释层）与既往修订版本不得
     充当证明自己的证据（interpretation_not_evidence，自证闭环）。
  2. 门序纯函数：身份证 → 输出层 → L01 出处 → 自证 → 证据只能来自反思
     落点 → 目标失效单调 → 无变化 → 想象层 → 证据检索（复用 M08.classify_fate
     内的 M01 准入 + M02 逐命题原句）→ 反例检查（M04 adjudicate 裁决 +
     显式反证事件，同根源转述不算独立反例）→ 接受或暂缓（held/rejected 必带
     稳定 reason_code）→ 版本记录。now 必填，revision_id/plan_id 内容寻址。
  3. 版本只增不覆盖：belief 与 self_view 的修订轨迹 append-only；每条修订带
     before/after、支撑证据引用（event_ids + 原句 spans）、反例处理结果、
     变更理由。what_changed 从修订记录生成「什么改变了你的想法」的证据链
     答案；没有记录/缺证据时如实说不知道，绝不现场编故事。
  4. 失效单调（接 M05）：被 M05 判失效/撤回的证据不得支撑「接受」；已接受
     的修订若支撑证据后来失效，reassessment_candidates 产出重评候选，
     绝不悄悄改历史（旧版本一字节不动）。
  5. 落账边界（二选一里选「纯计划输出 + 只读桥」，保持简单）：self_model
     尚无 belief_revision/self_view_rev 的专用单一写入事务入口，本模块
     **不开任何写入口**——只产出纯计划与 schema 对齐行（belief_revision_row /
     self_view_rev_row，字段与 self_model 两表逐列对齐），供主 agent 将来经
     唯一写入者落账。不写一行 SQL、不开写连接、不调旧写入器
     （revise_belief/note_self_view），绝不写真实 data/。
  6. 只读桥完全委托冻结前置：read_revision_inputs 走 L01→M08 只读桥 +
     M05.read_invalidation_state（mode=ro + query_only，DATA_DIR 调用时解析，
     缺库报 absent/degraded，绝不建库建表建目录）。self_view_rev 暂无冻结
     只读桥，基线由调用方传入，如实声明不补造。

硬不变量：确定性（无墙钟无随机，不 import time/random，now 显式传入，ID
内容寻址、输入顺序无关）；只追加（内存账本只追加，重复 ID/内容哈希不符/
版本断链一律拒绝，读函数纯读深拷贝）；原始事件不可改，解释分层可变。

边界（LIMITATIONS，如实声明没做的事）：不接真实定时反思 worker、不接生产
消费者（仅可执行契约，共享接线由主 agent 做）；不调 LLM 生成修订文本
（after/reason 由调用方传入，本模块只裁决与留版本）；不改原始事件；不做
L03 习惯与关系长期更新、不做 C04 表达注入。
"""
import copy
import hashlib
import json

import claim_conflicts as cc
import cognition_admission as ca
import invalidation_dag as dag
import memory_consolidation as mc
import self_reflection as sr

ALGORITHM_VERSION = 'l02-1'
# 修订目标层：信念与自我认识。绝不接受事件层（「发生过」只能由真实事件表达）。
TARGET_KINDS = ('belief', 'self_view')
# 候选形状复用 L01 输出层常量，不自带第二套。
OUTPUT_KINDS = sr.OUTPUT_KINDS
EPISTEMIC_TYPES = ca.EPISTEMIC_TYPES
PROMOTABLE_TIERS = mc.PROMOTABLE_TIERS
NON_PROMOTABLE_TIERS = mc.NON_PROMOTABLE_TIERS
FICTION_CONTEXTS = ca.FICTION_CONTEXTS
# 失效单调与反例阻断词表复用 M08/M05 冻结常量，不另立。
_TRUTH_LOST = mc._TRUTH_LOST
_BLOCKING_VERDICTS = mc._BLOCKING_VERDICTS
# 裁决三态：接受（进版本轨迹）/ 暂缓（等更多证据）/ 拒绝（门违规）。
DECISION_STATUSES = ('accepted', 'held', 'rejected')
# L02 稳定 reason_code（证据分层/失效/挂起等透传码复用 M08，不另造）。
REASON_CODES = (
    'missing_proposal_id',
    'missing_object_id',
    'revision_not_event',
    'missing_after_text',
    'missing_change_reason',
    'reflection_not_found',
    'reflection_candidate_rejected',
    'candidate_not_in_reflection',
    'interpretation_not_evidence',
    'evidence_not_from_reflection',
    'prior_invalidation_preserved',
    'no_change_proposed',
    'fiction_not_fact',
    'no_valid_evidence',
    'source_invalidated',
    'evidence_not_verifiable',
    'needs_reassessment_pending',
    'counterexample_conflict',
    'counterexample_evidence',
    'revision_accepted',
)

LIMITATIONS = (
    '不接真实定时反思 worker、不接生产消费者：仅可执行契约，共享接线由主 agent 做。',
    '不调 LLM 生成修订文本：after/reason 由调用方传入，本模块只裁决来源纪律与留版本，'
    '不现场编成长故事。',
    '不改原始事件：原始记录不可改，解释分层可变；修订只落 belief/self_view 版本轨迹。',
    '落账为纯计划输出 + 只读桥：self_model 尚无 belief_revision/self_view_rev 专用'
    '单一写入事务入口，本模块只产出 schema 对齐行供主 agent 经唯一写入者'
    '（cognition_writer.commit 路径）落账，自身不写 SQL、不开写连接、不调旧写入器。',
    '修订候选唯一来源是已提交的 L01 反思账本；直接「发生过」断言与自证一律拒绝。',
    '同一来源的重复转述不算独立证据也不算独立反例（根源按 R04/R05 同口径归并）。',
    '失效单调（接 M05）：失效/撤回证据不得支撑接受；已接受修订的支撑失效后产出'
    '重评候选，绝不篡改历史。',
    '版本只增不覆盖：append-only，重复 ID/内容哈希不符/版本断链拒绝；'
    '「什么改变了你的想法」只从修订记录回答，缺证据如实说不知道。',
    '确定性：无墙钟无随机，now 显式必填，revision_id/plan_id 内容寻址；'
    '只读桥委托 L01/M08/M05，缺库报 absent/degraded，绝不建库建表建目录。',
    '不做 L03 习惯与关系长期更新，不做 C04 表达注入。',
)


# ── 小工具（与 L01/M08 同一口径，非引擎）────────────────────────────


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_now(now):
    """now 必须显式传入：本模块不读墙上时钟（无 time import），决策可复现。"""
    if not _number(now):
        raise ValueError('revision requires an explicit numeric now; no wall clock')
    return float(now)


def _oid(candidate):
    return _text(candidate.get('object_id') or candidate.get('candidate_id')
                 or candidate.get('id'))


def _window_index(window):
    out = {}
    for row in (window or []):
        if isinstance(row, dict) and _text(row.get('event_id')):
            out[_text(row['event_id'])] = row
    return out


def _lost_ids(states):
    return {oid for oid, st in (states or {}).items()
            if isinstance(st, dict) and st.get('status') in _TRUTH_LOST}


def _source_lost(source_id, lost, window_by_id):
    if source_id in lost:
        return True
    row = window_by_id.get(source_id)
    return bool(row and row.get('retracted'))


def _belief_text(row):
    return _text(row.get('proposition') or row.get('text') or row.get('content'))


def _revision_id(decision):
    """revision_id 内容寻址：不含 ts/now，同内容跨时钟必同 ID。"""
    return 'l02_' + _sha(
        ALGORITHM_VERSION, decision['proposal_id'], decision['target_kind'],
        decision['target_id'], decision['version'], decision['status'],
        decision['reason_code'], decision['before'], decision['after'],
        decision['reason'], _canonical(decision['event_ids']),
        _canonical(decision['counterexamples']), decision['reflection_id'],
        decision['source_object_id'], decision['independent_sources'],
        _canonical(decision['evidence']))


def _plan_id(decisions):
    rows = sorted((_without_ts(d) for d in decisions),
                  key=lambda d: d['revision_id'])
    return 'l02p_' + _sha(ALGORITHM_VERSION, _canonical(rows))


def _without_ts(decision):
    return {k: v for k, v in decision.items() if k != 'ts'}


# ── 内存账本：只追加，读函数纯读（L01/S03/M08 先例）────────────────


def new_ledger():
    """空白修订账本。调用方持有；本模块不落盘、不解析路径。"""
    return {'records': {}, 'order': []}


def _check_ledger(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('records'), dict)
            or not isinstance(ledger.get('order'), list)):
        raise ValueError('ledger must come from new_ledger()')


def _trail(ledger, target_kind, target_id):
    """某目标的已接受版本轨迹（按提交序）。只数 accepted，held/rejected 不进版本。"""
    out = []
    if ledger is None:
        return out
    _check_ledger(ledger)
    for rid in ledger['order']:
        record = ledger['records'][rid]
        if (record.get('status') == 'accepted'
                and record.get('target_kind') == target_kind
                and record.get('target_id') == target_id):
            out.append(record)
    return out


def _baseline(target_kind, target_id, beliefs, self_views):
    """版本 1 的 before 基线：belief 取当前命题，self_view 取最近一版文本。
    找不到基线就是空串（首次认识），绝不补造旧观点。"""
    if target_kind == 'belief':
        for row in (beliefs or []):
            if isinstance(row, dict) and _oid(row) == target_id:
                return _belief_text(row)
        return ''
    rows = [r for r in (self_views or []) if isinstance(r, dict) and _text(r.get('text'))]
    if not rows:
        return ''
    latest = sorted(rows, key=lambda r: (float(r.get('ts') or 0), _text(r.get('id'))))[-1]
    return _text(latest.get('text'))


# ── L01 出处：候选信封只从已提交反思账本取（唯一来源）──────────────


def _reflection_output(reflection_ledger, reflection_id, object_id):
    """返回 (decision, envelope, error_code)。调用方自带信封一律不用。"""
    if reflection_ledger is None:
        return None, None, 'reflection_not_found'
    record = sr.get_record(reflection_ledger, reflection_id)
    if record is None:
        return None, None, 'reflection_not_found'
    for decision in record.get('rejected') or []:
        if decision.get('object_id') == object_id:
            return None, None, 'reflection_candidate_rejected'
    decision = next((d for d in record.get('reflections') or []
                     if d.get('object_id') == object_id), None)
    if decision is None:
        return None, None, 'candidate_not_in_reflection'
    envelope = next((c for c in record.get('candidates') or []
                     if _oid(c) == object_id), None)
    if envelope is None:
        return None, None, 'candidate_not_in_reflection'
    return decision, envelope, ''


def _comparison_candidate(proposal, envelope, support):
    """after 命题的 M04 比较形态：主体来自 L01 信封，根源取首个支撑源。"""
    return {'candidate_id': proposal['proposal_id'],
            'subject_ref': _text(envelope.get('subject_ref')),
            'value': proposal['after'],
            'root_event_id': support[0] if support else '',
            'evidence': [{'event_id': s} for s in support]}


def _belief_comparison(row):
    evidence = [e for e in (row.get('evidence') or []) if isinstance(e, dict)]
    root = _text(row.get('root_event_id'))
    if not root and evidence:
        root = _text(evidence[0].get('event_id'))
    return {'candidate_id': _oid(row),
            'subject_ref': _text(row.get('subject_ref') or row.get('subject_qq')),
            'value': _belief_text(row), 'root_event_id': root, 'evidence': evidence}


# ── 纯预检：门序裁决（不含落账）────────────────────────────────────


def _decide(proposal, ctx):
    """纯函数：判定一条修订提案。门序见模块 docstring；held/rejected 必带
    稳定 reason_code。version/before 由调用方按版本轨迹顺序传入。"""
    prop = proposal if isinstance(proposal, dict) else {}
    pid = _text(prop.get('proposal_id'))
    oid = _text(prop.get('object_id'))
    kind = _text(prop.get('target_kind'))
    target = _text(prop.get('target_id'))
    after = _text(prop.get('after'))
    reason = _text(prop.get('reason'))
    reflection_id = _text(prop.get('reflection_id'))
    base = {'proposal_id': pid, 'reflection_id': reflection_id,
            'source_object_id': oid, 'target_kind': kind, 'target_id': target,
            'version': 0, 'status': 'rejected', 'reason_code': '', 'reasons': [],
            'before': ctx['before'], 'after': after, 'reason': reason,
            'event_ids': [], 'evidence': [], 'counterexamples': [],
            'counterexample_result': 'not_checked', 'independent_sources': 0,
            'root_groups': [], 'revision_id': '', 'ts': ctx['now']}

    def out(status, code, reasons=(), **extra):
        got = dict(base, status=status, reason_code=code, reasons=list(reasons))
        got.update(extra)
        got['revision_id'] = _revision_id(got)
        return got

    # ── 身份证 ──
    if not pid:
        return out('rejected', 'missing_proposal_id')
    if not oid or not target:
        return out('rejected', 'missing_object_id')
    # ── 输出层：修订只落 belief/self_view，绝不制造「发生过」──
    if kind not in TARGET_KINDS:
        return out('rejected', 'revision_not_event')
    if not after:
        return out('rejected', 'missing_after_text')
    if not reason:
        return out('rejected', 'missing_change_reason')
    # ── L01 出处：信封只从已提交反思账本取 ──
    decision, envelope, err = _reflection_output(
        ctx['reflection_ledger'], reflection_id, oid)
    if err:
        return out('rejected', err)
    grounding = list(decision.get('grounding_sources') or [])
    # 支撑源：缺省用 L01 落点；调用方只能收窄，不能另找证据。
    if 'support_event_ids' in prop:
        support = sorted({_text(s) for s in (prop.get('support_event_ids') or [])
                          if _text(s)})
    else:
        support = list(grounding)
    counters = [c for c in (prop.get('counter_evidence') or []) if isinstance(c, dict)]
    cited = set(support) | {_text(c.get('event_id')) for c in counters if _text(c.get('event_id'))}
    # ── 自证闭环：L01 解释层产物与既往修订版本不得充当证据 ──
    if cited & ctx['interpretation_ids']:
        return out('rejected', 'interpretation_not_evidence')
    if not set(support) <= set(grounding):
        return out('rejected', 'evidence_not_from_reflection')
    # ── 目标失效单调：已失效/被取代的对象绝不借修订复活 ──
    if target in ctx['lost']:
        return out('held', 'prior_invalidation_preserved')
    if after == ctx['before']:
        return out('rejected', 'no_change_proposed')
    # ── 想象层：小说/角色扮演/塔罗永不修订成现实人物事实 ──
    if _text(envelope.get('context_kind')) in FICTION_CONTEXTS:
        return out('held', 'fiction_not_fact')
    # ── 证据检索：逐命题来源，复用 M08.classify_fate（M01 准入 + M02 原句）──
    if not support:
        return out('held', 'no_valid_evidence')
    trimmed = sr._trim(envelope, support)
    fate = mc.classify_fate(trimmed, ctx['window'], ctx['states'], [], now=ctx['now'])
    if not (fate['fate'] == 'belief' and fate['promotable']):
        return out('held', fate['reason_code'], fate['reasons'])
    groups = sorted({g for s in support for g in ctx['root_groups'].get(s, [s])})
    accounting = {'event_ids': list(support), 'evidence': list(fate['spans']),
                  'root_groups': groups, 'independent_sources': len(groups)}
    # ── 反例检查：M04 裁决（与其它信念矛盾）+ 显式反证事件 ──
    blocking = [r for r in ctx['conflicts']
                if pid in (r.get('left_id'), r.get('right_id'))
                and r.get('verdict') in _BLOCKING_VERDICTS
                and r.get('reason_code') != 'same_root_not_independent']
    examined = []
    for counter in counters:
        eid = _text(counter.get('event_id'))
        if not eid:
            continue
        if _source_lost(eid, ctx['lost'], ctx['window_by_id']):
            disposition = 'dismissed_invalidated'
        elif eid not in ctx['window_by_id']:
            disposition = 'unverified_not_in_window'
        elif set(ctx['root_groups'].get(eid, [eid])) & set(groups):
            disposition = 'dismissed_same_root'
        else:
            disposition = 'independent_counterexample'
        examined.append({'event_id': eid, 'disposition': disposition})
    independent_counters = [c for c in examined
                            if c['disposition'] == 'independent_counterexample']
    examined.sort(key=lambda c: c['event_id'])
    counter_result = 'none_found'
    if blocking:
        counter_result = 'blocked_conflict'
    elif independent_counters:
        counter_result = 'blocked_evidence'
    elif examined:
        counter_result = 'dismissed_only'
    extra = dict(accounting, counterexamples=examined,
                 counterexample_result=counter_result)
    if blocking:
        return out('held', 'counterexample_conflict',
                   [r.get('reason_code', '') for r in blocking], **extra)
    if independent_counters:
        return out('held', 'counterexample_evidence',
                   [c['event_id'] for c in independent_counters], **extra)
    # ── 接受：进版本轨迹（版本号由调用方按轨迹顺序给出）──
    return out('accepted', 'revision_accepted', fate['reasons'],
               version=ctx['version'], **extra)


def revision_plan(proposals, reflection_ledger, window=None, *, ledger=None,
                  beliefs=None, self_views=None, states=None, conflicts=None, now):
    """纯函数：产出修订计划，不写任何东西。

    proposals:         修订提案 {proposal_id, reflection_id, object_id（L01 反思
                       输出 ID）, target_kind∈belief/self_view, target_id, after,
                       reason, 可选 support_event_ids（只能收窄 L01 落点）、
                       counter_evidence[]}。after/reason 由调用方传入，本模块不调 LLM。
    reflection_ledger: 已提交的 L01 反思账本（候选信封的唯一来源）。
    window:            调用方冻结的源记录（self_model.source_window 产出）。
    ledger:            既往修订账本（new_ledger）；提供版本轨迹与自证登记。
    beliefs:           当前信念行（M08 只读桥 beliefs 形态）：before 基线 + 反例比较。
    self_views:        self_view_rev 形态行（id/ts/text/reason/event_ids）：before 基线。
    states:            M05 对象状态（invalidation_dag.read_invalidation_state）。
    conflicts:         M04 裁决记录；缺省时用 claim_conflicts.adjudicate 现算
                       （after 命题 vs 其它信念；被修订目标本身不参与反例）。
    now:               显式时间（必填；决策路径与 ID 不含墙上时钟）。

    返回 {plan_id, algorithm_version, status, decisions, accepted, held,
          rejected, counts, provenance, limitations}。
    """
    at = _require_now(now)
    rows = sorted((p for p in (proposals or []) if isinstance(p, dict)),
                  key=lambda p: _text(p.get('proposal_id')))
    unique = {}
    for prop in rows:
        unique.setdefault(_text(prop.get('proposal_id')), prop)
    rows = [unique[pid] for pid in sorted(unique)]
    frozen = list(window or [])
    prior = states if isinstance(states, dict) else {}
    window_by_id = _window_index(frozen)
    lost = _lost_ids(prior)
    _, l01_interpretations = sr._watermark(reflection_ledger)
    interpretation_ids = set(l01_interpretations)
    if ledger is not None:
        _check_ledger(ledger)
        # 既往修订版本也是解释层：引用它们当证据同样是自证。
        interpretation_ids |= set(ledger['order'])
    canon = sr._window_canon(frozen)
    root_groups = sr._source_root_groups(frozen, canon)

    # 反例比较集：after 命题 + 其它信念（本批被修订的目标不参与，否则每次
    # 修订都会被「旧观点反对新观点」挡死——那正是修订要表达的变化）。
    targets = {_text(p.get('target_id')) for p in rows}
    comparison = []
    for prop in rows:
        decision, envelope, err = _reflection_output(
            reflection_ledger, _text(prop.get('reflection_id')),
            _text(prop.get('object_id')))
        if err or not _text(prop.get('after')):
            continue
        if 'support_event_ids' in prop:
            support = sorted({_text(s) for s in (prop.get('support_event_ids') or [])
                              if _text(s)})
        else:
            support = list(decision.get('grounding_sources') or [])
        comparison.append(_comparison_candidate(
            {'proposal_id': _text(prop.get('proposal_id')), 'after': _text(prop.get('after'))},
            envelope, support))
    for row in (beliefs or []):
        if isinstance(row, dict) and _oid(row) not in targets:
            comparison.append(_belief_comparison(row))
    if conflicts is None:
        conflicts = cc.adjudicate(comparison)

    # 版本轨迹按提案顺序推进：同一目标多条提案时，后一条的 before 是前一条
    # 接受后的 after（计划内也保持 append-only 语义）。
    working = {}
    decisions = []
    for prop in rows:
        kind, target = _text(prop.get('target_kind')), _text(prop.get('target_id'))
        key = (kind, target)
        if key not in working:
            trail = _trail(ledger, kind, target)
            before = trail[-1]['after'] if trail else _baseline(
                kind, target, beliefs, self_views)
            working[key] = {'before': before, 'version': len(trail) + 1}
        ctx = {'reflection_ledger': reflection_ledger, 'window': frozen,
               'window_by_id': window_by_id, 'states': prior, 'lost': lost,
               'interpretation_ids': interpretation_ids, 'root_groups': root_groups,
               'conflicts': conflicts or [], 'now': at,
               'before': working[key]['before'], 'version': working[key]['version']}
        decision = _decide(prop, ctx)
        decisions.append(decision)
        if decision['status'] == 'accepted':
            working[key] = {'before': decision['after'],
                            'version': working[key]['version'] + 1}

    accepted = [d for d in decisions if d['status'] == 'accepted']
    held = [d for d in decisions if d['status'] == 'held']
    rejected = [d for d in decisions if d['status'] == 'rejected']
    return {'plan_id': _plan_id(decisions), 'algorithm_version': ALGORITHM_VERSION,
            'status': 'planned', 'decisions': decisions, 'accepted': accepted,
            'held': held, 'rejected': rejected,
            'counts': {'accepted': len(accepted), 'held': len(held),
                       'rejected': len(rejected)},
            'provenance': {'method': 'versioned_revision_from_l01_reflection_only',
                           'algorithm_version': ALGORITHM_VERSION,
                           'llm_revision': False, 'window_size': len(frozen),
                           'conflict_records': len(conflicts or []),
                           'conflicts': copy.deepcopy(conflicts or []),
                           'root_canon': canon},
            'limitations': list(LIMITATIONS)}


def commit_revision(ledger, plan):
    """把修订计划追加进内存账本（accepted/held/rejected 全留审计，版本轨迹
    只数 accepted）。重复 ID / 内容哈希不符 / 版本断链一律拒绝，先整体校验
    再追加，绝不落半条计划。"""
    _check_ledger(ledger)
    if not isinstance(plan, dict) or plan.get('status') not in ('planned', 'committed'):
        raise ValueError('revision plan must come from revision_plan')
    decisions = plan.get('decisions')
    if not isinstance(decisions, list):
        raise ValueError('revision plan requires decisions')
    pid = _text(plan.get('plan_id'))
    if not pid or pid != _plan_id(decisions):
        raise ValueError('plan_id does not match its content')
    seen = set()
    trails = {}
    for decision in decisions:
        rid = _text(decision.get('revision_id'))
        if not rid or rid != _revision_id(decision):
            raise ValueError('revision_id does not match its content')
        if rid in ledger['records'] or rid in seen:
            raise ValueError('revision already exists; history is append-only: %s' % rid)
        seen.add(rid)
        if decision.get('status') == 'accepted':
            key = (decision.get('target_kind'), decision.get('target_id'))
            if key not in trails:
                trails[key] = _trail(ledger, *key)
            trail = trails[key]
            expected_version = len(trail) + 1
            if decision.get('version') != expected_version:
                raise ValueError('revision version breaks the append-only trail')
            if trail and decision.get('before') != trail[-1]['after']:
                raise ValueError('revision before-text does not match version trail')
            trail.append(decision)
    records = []
    for decision in decisions:
        record = copy.deepcopy(decision)
        record['plan_id'] = pid
        record['committed'] = True
        ledger['records'][record['revision_id']] = record
        ledger['order'].append(record['revision_id'])
        records.append(copy.deepcopy(record))
    return records


# ── 纯读视图：GET 不得启动任何写流程（L01/S03/M08 先例）────────────


def get_record(ledger, revision_id):
    _check_ledger(ledger)
    record = ledger['records'].get(_text(revision_id))
    return copy.deepcopy(record) if record is not None else None


def list_records(ledger):
    _check_ledger(ledger)
    return list(ledger['order'])


def version_trail(ledger, target_kind, target_id):
    """某目标的已接受版本轨迹（深拷贝，按提交序）。held/rejected 不进轨迹。"""
    if target_kind not in TARGET_KINDS:
        raise ValueError('unknown revision target kind: %s' % target_kind)
    return copy.deepcopy(_trail(ledger, target_kind, _text(target_id)))


def what_changed(ledger, target_kind, target_id, *, states=None):
    """回答「什么改变了你的想法」：答案只来自修订记录与支撑经历引用。

    没有修订记录 / 记录缺支撑证据时如实说不知道（known=False），绝不现场
    编故事。支撑证据后来失效的，如实标注待重评，历史不改。"""
    if target_kind not in TARGET_KINDS:
        raise ValueError('unknown revision target kind: %s' % target_kind)
    target = _text(target_id)
    trail = _trail(ledger, target_kind, target)
    lost = _lost_ids(states)
    changes = []
    for record in trail:
        invalidated = sorted(set(record.get('event_ids') or []) & lost)
        changes.append({'version': record['version'], 'before': record['before'],
                        'after': record['after'], 'reason': record['reason'],
                        'event_ids': list(record.get('event_ids') or []),
                        'reflection_id': record['reflection_id'],
                        'revision_id': record['revision_id'],
                        'counterexample_result': record.get('counterexample_result', ''),
                        'invalidated_sources': invalidated})
    base = {'known': False, 'reason_code': '', 'target_kind': target_kind,
            'target_id': target, 'changes': changes, 'answer': ''}
    if not changes:
        return dict(base, reason_code='no_revision_record',
                    answer='没有修订记录，不知道是什么改变了想法，不编造。')
    if any(not c['event_ids'] or not c['reason'] for c in changes):
        return dict(base, reason_code='no_evidence_recorded',
                    answer='修订记录缺支撑证据或理由，不知道是什么改变了想法，不编造。')
    lines = []
    for change in changes:
        line = '第%d版把「%s」改成「%s」，理由：%s；依据事件：%s' % (
            change['version'], change['before'], change['after'],
            change['reason'], '、'.join(change['event_ids']))
        if change['invalidated_sources']:
            line += '（支撑证据 %s 后来失效，待重评，历史不改）' % '、'.join(
                change['invalidated_sources'])
        lines.append(line)
    return dict(base, known=True, reason_code='answered_from_records',
                answer='\n'.join(lines))


def reassessment_candidates(ledger, states=None, window=None):
    """纯函数：已接受修订的支撑证据后来失效 → 产出重评候选，绝不改历史。

    失效判定复用 M05/M08 同口径（states 失效 + 冻结窗口撤回）。返回按
    (target_kind, target_id, version) 排序的候选列表；账本一字节不动。
    重评本身要走新一轮 L01 反思 + 修订提案，不在这里悄悄复活或改写。"""
    _check_ledger(ledger)
    lost = _lost_ids(states)
    window_by_id = _window_index(window)
    out = []
    for rid in ledger['order']:
        record = ledger['records'][rid]
        if record.get('status') != 'accepted':
            continue
        supports = list(record.get('event_ids') or [])
        lost_sources = sorted(s for s in supports
                              if _source_lost(s, lost, window_by_id))
        if not lost_sources:
            continue
        out.append({'revision_id': rid, 'target_kind': record['target_kind'],
                    'target_id': record['target_id'], 'version': record['version'],
                    'lost_sources': lost_sources,
                    'remaining_sources': sorted(set(supports) - set(lost_sources)),
                    'reason_code': 'support_invalidated', 'action': 'reassess',
                    'history': 'preserved'})
    out.sort(key=lambda c: (c['target_kind'], c['target_id'], c['version']))
    return out


# ── schema 对齐行：供主 agent 将来经唯一写入者落账（本模块不写）────


def belief_revision_row(record):
    """accepted belief 修订 → self_model.belief_revision 逐列对齐行。"""
    if record.get('status') != 'accepted' or record.get('target_kind') != 'belief':
        raise ValueError('belief_revision_row requires an accepted belief revision')
    return {'id': record['revision_id'], 'belief_id': record['target_id'],
            'old_text': record['before'], 'new_text': record['after'],
            'reason': record['reason'], 'event_ids': list(record['event_ids']),
            'ts': record['ts']}


def self_view_rev_row(record):
    """accepted self_view 修订 → self_model.self_view_rev 逐列对齐行。"""
    if record.get('status') != 'accepted' or record.get('target_kind') != 'self_view':
        raise ValueError('self_view_rev_row requires an accepted self_view revision')
    return {'id': record['revision_id'], 'ts': record['ts'], 'text': record['after'],
            'reason': record['reason'], 'event_ids': list(record['event_ids'])}


def ledger_rows(ledger):
    """全部已接受修订的 schema 对齐行（按版本轨迹序）。纯读，不落账。"""
    _check_ledger(ledger)
    out = {'belief_revision': [], 'self_view_rev': []}
    for rid in ledger['order']:
        record = ledger['records'][rid]
        if record.get('status') != 'accepted':
            continue
        if record['target_kind'] == 'belief':
            out['belief_revision'].append(belief_revision_row(record))
        else:
            out['self_view_rev'].append(self_view_rev_row(record))
    return out


# ── 只读桥：完全委托冻结前置，缺库报 absent/degraded，绝不建库 ─────


def read_revision_inputs():
    """只读收集修订输入：L01 隔离候选、信念（含原句 span）、M05 状态/边/epoch。

    完全复用 L01→M08 冻结只读桥与 M05.read_invalidation_state（mode=ro +
    query_only，DATA_DIR 调用时解析，缺库报 absent，绝不补造）。self_view_rev
    暂无冻结只读桥：基线由调用方传入，这里如实返回空并声明，不另开读取口。"""
    inputs = sr.read_reflection_inputs()
    state = dag.read_invalidation_state()
    return {'candidates': inputs['candidates'], 'beliefs': inputs['beliefs'],
            'states': state['states'], 'edges': state['edges'],
            'epoch': state['epoch'], 'degradation': inputs['degradation'],
            'self_views': [],
            'self_view_note': 'self_view_rev 暂无冻结只读桥，基线由调用方传入'}


def freeze_window(event_ids):
    """只读冻结这批对象引用的真实事件（走 L01→M08→self_model.source_window）。

    缺库/缺事件返回空窗口：原句自然绑不上，按降级处理，绝不补造事件。"""
    return sr.freeze_window(event_ids)