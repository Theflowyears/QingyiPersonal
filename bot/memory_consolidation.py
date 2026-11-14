# -*- coding: utf-8 -*-
"""M08 有限巩固与晋升（路线 §3.1 第8步「巩固记忆」/ §5.3 自我认识只能修订 /
§5.4 反思不得制造经历 / §8.2 单一状态写入者）。

M07 把私聊轮次情景化成 episode 就停手（private_turn_episodes LIMITATIONS：
「不做巩固晋升……是 M08 的范围」）；M05 把失去部分支持的对象挂成
needs_reassessment 就停手（invalidation_dag：「重新评估只能由后续显式修订
事务（M08/L02）完成」）；M04 产出矛盾裁决记录供「M05 失效传播 / M08 巩固」
消费。本模块接住这三处 deferral，做**有限**的巩固与晋升：

  1. 巩固分类：对累积的 episode/claim 候选逐条判定归宿——
       experience  成为 lasting 经历（episode，亲历且有有效来源）；
       belief      晋升为信念/自我认识（命题，可核验原句 + 可晋升证据层）；
       temporary   只是临时信息（推断/想象、无据、已失效、矛盾或预算用尽）。
  2. 显式重评估修订事务：对 M05 留下的 needs_reassessment 对象，用剩余
     **有效独立**支持重新核验，产出一条 belief_corrected 修订（旧版本
     superseded、新版本另立），绝不把 invalidated/superseded 洗回 active。

「有限」= 每次事务有显式预算上限（budget），晋升按确定性优先级取前 N 条，
其余如实留 temporary（budget_exhausted），绝不无限晋升。

硬不变量（全部复用冻结前置，不另长第二套）：
  · 单一写入者：落账只走 cognition_writer.commit（M06）与
    self_model.write_invalidation_transaction（M05）；本模块不写一行事实 SQL，
    不开写连接，不调旧写入器（add_belief/revise_belief/note_self_view…）。
  · 证据分层：晋升必须带 epistemic_type 与逐命题原句来源；inferred/imagined
    永不晋升为已核验信念（M01 belief_layer_not_fact / M02 逐字核对复用）。
  · 失效单调：已撤回/失效/被取代的源与对象永不复活；晋升尊重 M05 的
    active/needs_reassessment/invalidated/superseded 状态（read_gate 口径）。
  · 确定性：稳定 reason_code、决策路径不读墙上时钟（now 显式传入）、
    内容寻址 ID（plan_id/reassessment_id 不含 now，同输入同 ID）。
  · 只追加/版本化：内存账本只追加，旧记录一字节不动；晋升/重评估是新版本，
    不是覆盖（belief_corrected 旧版 superseded、历史保留）。

账本是调用方传入的内存对象（new_ledger），沿用 S03 先例：plan_* 只算不写、
commit_* 拒绝重复 ID 与内容哈希不符、读函数纯读返回深拷贝。self_model 目前
没有 belief_revision/self_view_rev 的专用事务入口，本模块不建库建表建目录；
重评估以 belief_corrected（旧版 superseded + 新版 belief 另立）表达，专用
修订入口与 L02 长期自我修订留待主 agent 接线。只读桥 read_consolidation_inputs
沿用 S03/M05 先例（mode=ro + query_only，DATA_DIR 调用时解析，缺库报
absent/degraded），并复用 M04/M05 的冻结只读桥。

边界（LIMITATIONS）：不做 L02 长期自我认识修订、不做 C04 表达注入、不改线
任何旧写入器/旧摘要路径、不接检索（R01 仍只观察）。
"""
import copy
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import claim_conflicts
import claim_provenance
import cognition_admission
import cognition_writer
import invalidation_dag
import self_model

ALGORITHM_VERSION = 'm08-1'
# 巩固归宿：经历 / 信念 / 临时。顺序即默认优先级展示口径。
FATES = ('experience', 'belief', 'temporary')
# 可晋升为已核验信念的证据层（亲历/听说/查到）；推断/想象永不晋升。
PROMOTABLE_TIERS = ('experienced', 'reported', 'looked_up')
NON_PROMOTABLE_TIERS = ('inferred', 'imagined')
# 命题类候选（晋升为信念）与经历类候选（巩固为经历）。
_BELIEF_KINDS = ('claim', 'belief', 'preference')
_EXPERIENCE_KINDS = ('episode',)
# 有限预算：默认每次事务最多晋升 5 条，硬上限 20，绝不无限。
DEFAULT_BUDGET = 5
MAX_BUDGET = 20
# 重评估结论：修订（新版本另立）/ 暂缓（仍 pending）/ 挡下（已失效，单调）。
REASSESSMENT_OUTCOMES = ('revised', 'deferred', 'blocked')
# M04 裁决里禁止盲目晋升的 verdict：contradiction（明确矛盾）/ disputed
# （未决值冲突或同根源转述）→ 留临时，等修订。deferred 只是「无法比较」
# （主体缺失/家族未分类），不是冲突信号，绝不据此挡下正常晋升。
_BLOCKING_VERDICTS = ('contradiction', 'disputed')
_TRUTH_LOST = ('invalidated', 'superseded')
# 重评估只接住 M05 明确挂起的对象；active 无需重评，失效的永不复活。
_PENDING = 'needs_reassessment'
LIMITATIONS = (
    '有限巩固：每次事务有显式预算上限（DEFAULT_BUDGET/MAX_BUDGET），绝不无限晋升。',
    '推断/想象永不晋升为已核验信念；晋升必须带证据层与逐命题原句来源（复用 M01/M02）。',
    '重评估只接住 M05 的 needs_reassessment；invalidated/superseded 单调永不复活。',
    '落账只走 cognition_writer.commit 与 self_model.write_invalidation_transaction，不写事实 SQL、不调旧写入器。',
    '账本为调用方传入的内存对象；self_model 暂无 belief_revision/self_view_rev 专用事务入口，本模块不建库建表。',
    '不做 L02 长期自我认识修订、不做 C04 表达注入、不改线旧写入器与旧摘要路径、不接检索（R01 仍只观察）。',
)


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _without_clock(value, keys):
    """内容寻址哈希前剥掉墙上时钟字段：同内容跨 now 必同 ID（S03 口径）。"""
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k not in keys}


def _plan_hash(version, budget, candidates, decisions):
    """巩固计划的内容寻址 ID：候选与裁决都按 object_id 排序后哈希，
    输入顺序无关、不含墙上时钟。"""
    rows = sorted((c for c in candidates if isinstance(c, dict)), key=_oid)
    summary = sorted((d['object_id'], d['fate'], d['reason_code']) for d in decisions)
    return 'm08_' + _sha(version, budget, _canonical(rows), _canonical(summary))


def _text(value):
    return str(value or '').strip()


def _tier(candidate):
    return _text(candidate.get('epistemic_type') or candidate.get('epistemic_kind'))


def _kind(candidate):
    return _text(candidate.get('object_kind'))


def _oid(candidate):
    return _text(candidate.get('object_id') or candidate.get('candidate_id')
                 or candidate.get('id'))


def _sources(candidate):
    return [str(x).strip() for x in (candidate.get('source_event_ids') or [])
            if str(x or '').strip()]


def _ledger_id(object_id, fate):
    """M01 object_id → 账本稳定 ID（与 write_cognition_transaction 同口径）。

    派生边与失效传播都按账本 ID 定位对象，必须用 bel_/epi_ 前缀的派生 ID，
    不能用候选 object_id，否则 M05 传播会挂错对象。
    """
    prefix = 'epi_' if fate == 'experience' else 'bel_'
    return prefix + _sha(object_id)


# ── 内存账本：只追加，读函数纯读（S03 先例）────────────────────────


def new_ledger():
    """空白巩固账本。调用方持有；本模块不落盘、不解析路径。"""
    return {'records': {}, 'order': []}


def _check_ledger(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('records'), dict)
            or not isinstance(ledger.get('order'), list)):
        raise ValueError('ledger must come from new_ledger()')


def _budget_value(budget):
    """显式预算校验：非负整数，夹到硬上限。绝不接受「无限」。"""
    if budget is None:
        return DEFAULT_BUDGET, []
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ValueError('budget must be a nonnegative integer')
    if budget < 0:
        raise ValueError('budget must be nonnegative')
    if budget > MAX_BUDGET:
        return MAX_BUDGET, ['budget_clamped_to_max']
    return budget, []


# ── 冻结门之上的纯预检（证据分层 / 失效单调，全部复用前置）─────────


def _lost_states(states):
    return {oid for oid, st in (states or {}).items()
            if isinstance(st, dict) and st.get('status') in _TRUTH_LOST}


def _source_lost(source_id, lost, window_by_id):
    """源是否已失效：M05 状态失效，或冻结窗口里标记撤回。"""
    if source_id in lost:
        return True
    row = window_by_id.get(source_id)
    return bool(row and row.get('retracted'))


def _window_index(window):
    out = {}
    for row in (window or []):
        if isinstance(row, dict) and _text(row.get('event_id')):
            out[_text(row['event_id'])] = row
    return out


def _blocking_conflicts(conflicts):
    """M04 裁决 → 禁止盲目晋升的候选 id 集合（矛盾/存疑/暂缓）。"""
    blocked = {}
    for rec in (conflicts or []):
        if not isinstance(rec, dict):
            continue
        verdict = _text(rec.get('verdict'))
        if verdict in _BLOCKING_VERDICTS:
            for key in ('left_id', 'right_id'):
                oid = _text(rec.get(key))
                if oid:
                    blocked.setdefault(oid, verdict)
    return blocked


def _evidence_check(candidate, window, now):
    """复用 M01 准入 + M02 逐命题来源做纯预检，不重实现写入器的门组合。

    返回 (ok, reasons, spans)。命题类必须准入 accept 且原句 accepted；
    经历类必须准入 accept（来源有效性另由 _source_lost 核对）。这只是
    巩固前的保守预检；落账时 cognition_writer.commit 仍会权威重跑全门。
    """
    admission = cognition_admission.admit(candidate, now=now)
    reasons = list(admission['reasons'])
    if admission['decision'] != 'accept':
        return False, reasons, []
    if _kind(candidate) in _BELIEF_KINDS:
        bound = claim_provenance.bind_claim(candidate, window or [], now=now)
        reasons.extend(bound['reasons'])
        if bound['decision'] != 'accepted':
            return False, reasons, []
        return True, reasons, list(bound['spans'])
    return True, reasons, []


def _priority_key(candidate, spans, window_by_id, lost):
    """确定性优先级：有效来源越多越先晋升，同数按 object_id 字典序。

    不读墙上时钟、不调模型；同输入必同序（预算分配可复现）。
    """
    if spans:
        valid = len({s['event_id'] for s in spans
                     if not _source_lost(s['event_id'], lost, window_by_id)})
    else:
        valid = len([s for s in _sources(candidate)
                     if not _source_lost(s, lost, window_by_id)])
    return (-valid, _oid(candidate))


def classify_fate(candidate, window=None, states=None, conflicts=None, now=None):
    """纯函数：判定一条候选的巩固归宿（不含预算分配）。

    返回 {object_id, kind, fate, reason_code, reasons, tier, promotable,
          spans}。fate ∈ FATES；reason_code 稳定可留账。推断/想象、已失效、
    needs_reassessment、矛盾、无据都留 temporary，绝不冒充已核验事实。
    """
    at = time.time() if now is None else float(now)
    cand = candidate if isinstance(candidate, dict) else {}
    oid, kind, tier = _oid(cand), _kind(cand), _tier(cand)
    window_by_id = _window_index(window)
    lost = _lost_states(states)
    blocked = _blocking_conflicts(conflicts)
    base = {'object_id': oid, 'kind': kind, 'fate': 'temporary',
            'reason_code': '', 'reasons': [], 'tier': tier,
            'promotable': False, 'spans': []}

    def out(fate, code, reasons=(), promotable=False, spans=()):
        return dict(base, fate=fate, reason_code=code,
                    reasons=list(reasons), promotable=promotable,
                    spans=list(spans))

    if not oid:
        return out('temporary', 'missing_object_id')
    if kind not in _BELIEF_KINDS + _EXPERIENCE_KINDS:
        return out('temporary', 'unknown_kind')
    # 失效单调：对象自身已失效/被取代，永不晋升（尊重 M05 状态）。
    status = _text((states or {}).get(oid, {}).get('status')) if isinstance(
        (states or {}).get(oid), dict) else ''
    if status in _TRUTH_LOST:
        return out('temporary', 'prior_invalidation_preserved')
    if status == _PENDING:
        # M05 挂起的对象不是已核验事实：交给显式重评估，不在巩固里盲目晋升。
        return out('temporary', 'needs_reassessment_pending')
    # 证据分层：推断/想象永不晋升为已核验信念。
    if tier in NON_PROMOTABLE_TIERS:
        return out('temporary', tier + '_not_promotable')
    if tier not in PROMOTABLE_TIERS:
        return out('temporary', 'unknown_epistemic_type')
    # 源失效：任一来源已撤回/失效就不能晋升（不复活失效依据）。
    sources = _sources(cand)
    if any(_source_lost(s, lost, window_by_id) for s in sources):
        return out('temporary', 'source_invalidated')
    # M04 矛盾裁决：矛盾/存疑/暂缓的候选不盲目晋升，留待修订。
    if oid in blocked:
        return out('temporary', 'conflict_' + blocked[oid])
    # 冻结门预检（M01 准入 + M02 原句）。
    ok, reasons, spans = _evidence_check(cand, window, at)
    if not ok:
        return out('temporary', 'evidence_not_verifiable', reasons)
    if kind in _EXPERIENCE_KINDS:
        if not sources:
            return out('temporary', 'no_source_events', reasons)
        return out('experience', 'promoted_experience', reasons, True, spans)
    return out('belief', 'promoted_belief', reasons, True, spans)


def consolidation_plan(candidates, window=None, *, budget=DEFAULT_BUDGET,
                       states=None, conflicts=None, now=None):
    """纯函数：有限巩固计划。逐条判归宿 + 按预算取前 N 条晋升，不写任何东西。

    candidates: 累积的 M01 信封候选（episode/claim/belief/preference）。
    window:     调用方冻结的源记录（self_model.source_window 产出）。
    budget:     本次事务晋升上限（非负整数，夹到 MAX_BUDGET），绝不无限。
    states:     M05 对象状态（invalidation_dag.read_invalidation_state）。
    conflicts:  M04 裁决记录；缺省时用 claim_conflicts.adjudicate 现算批内矛盾。
    now:        显式时间（决策路径不读墙上时钟；plan_id 不含 now）。

    返回 {plan_id, algorithm_version, budget, budget_notes, decisions[],
          promotions[], temporary[], reassessment_needed[], counts,
          candidates, status, provenance, limitations}。内容寻址 plan_id。
    """
    at = time.time() if now is None else float(now)
    effective_budget, budget_notes = _budget_value(budget)
    rows = [c for c in (candidates or []) if isinstance(c, dict)]
    frozen = list(window or [])
    prior = states if isinstance(states, dict) else {}
    if conflicts is None:
        conflicts = claim_conflicts.adjudicate(rows)
    decisions = [classify_fate(c, frozen, prior, conflicts, now=at) for c in rows]

    # 预算分配：可晋升候选按确定性优先级排序，取前 N 条；其余留临时。
    # decisions[j] 与 rows[j] 同序，用显式索引配对，不靠 dict 相等反查。
    window_by_id = _window_index(frozen)
    lost = _lost_states(prior)
    promotable = [(j, d) for j, d in enumerate(decisions) if d['promotable']]
    promotable.sort(key=lambda jd: _priority_key(rows[jd[0]], jd[1]['spans'],
                                                 window_by_id, lost))
    chosen = {id(d) for _, d in promotable[:effective_budget]}
    for d in decisions:
        if d['promotable'] and id(d) not in chosen:
            d['fate'] = 'temporary'
            d['promotable'] = False
            d['reason_code'] = 'budget_exhausted'
            d['reasons'] = list(d['reasons']) + ['budget_exhausted']

    promotions = [d for d in decisions if d['fate'] in ('experience', 'belief')]
    for d in promotions:
        d['ledger_id'] = _ledger_id(d['object_id'], d['fate'])
    temporary = [d for d in decisions if d['fate'] == 'temporary']
    reassessment_needed = sorted(
        {d['object_id'] for d in decisions
         if d['reason_code'] == 'needs_reassessment_pending'})
    plan_id = _plan_hash(ALGORITHM_VERSION, effective_budget, rows, decisions)
    counts = {fate: sum(1 for d in decisions if d['fate'] == fate) for fate in FATES}
    return {
        'plan_id': plan_id, 'algorithm_version': ALGORITHM_VERSION,
        'budget': effective_budget, 'budget_notes': budget_notes,
        'decisions': decisions, 'promotions': promotions, 'temporary': temporary,
        'reassessment_needed': reassessment_needed, 'counts': counts,
        'candidates': copy.deepcopy(rows), 'status': 'planned',
        'provenance': {'method': 'bounded_consolidation_of_recorded_candidates',
                       'algorithm_version': ALGORITHM_VERSION,
                       'llm_promotion': False, 'window_size': len(frozen),
                       'conflict_records': len(conflicts or [])},
        'limitations': list(LIMITATIONS),
    }


def commit_plan(ledger, plan):
    """把巩固计划追加进内存账本。重复 ID / 内容哈希不符一律拒绝（S03 口径）。"""
    _check_ledger(ledger)
    if not isinstance(plan, dict) or plan.get('status') not in ('planned', 'committed'):
        raise ValueError('consolidation plan must come from consolidation_plan')
    pid = _text(plan.get('plan_id'))
    if not pid:
        raise ValueError('consolidation plan requires plan_id')
    if pid in ledger['records']:
        raise ValueError('plan already exists; history is append-only: %s' % pid)
    expected = _plan_hash(plan.get('algorithm_version'), plan.get('budget'),
                          plan.get('candidates') or [], plan.get('decisions') or [])
    if pid != expected:
        raise ValueError('plan_id does not match its content')
    record = copy.deepcopy(plan)
    record['status'] = 'committed'
    ledger['records'][pid] = record
    ledger['order'].append(pid)
    return copy.deepcopy(record)


# ── 显式重评估修订事务（接住 M05 的 needs_reassessment）─────────────


def _valid_remaining(object_id, states, window_by_id):
    """M05 剩余支持里当前仍有效的部分（再过滤后续失效/撤回）。

    独立性优先取 propagate 输出的 independent_support；从只读桥
    read_invalidation_state 拿到的状态没有该列（M05 把它写进 reasons），
    回落到 'independent_support_remains' 标志——复用 M05 自己的判定，
    不另长第二套根源独立性算法。
    """
    entry = (states or {}).get(object_id) or {}
    lost = _lost_states(states)
    remaining = [str(s) for s in (entry.get('remaining_supports') or [])]
    valid = [s for s in remaining if not _source_lost(s, lost, window_by_id)]
    independent = entry.get('independent_support')
    if independent is None:
        independent = 'independent_support_remains' in (entry.get('reasons') or [])
    return valid, bool(independent)


def plan_reassessment(record, states, edges, window=None, *, epoch=0,
                      now=None, reason='重新评估剩余有效支持'):
    """纯函数：对一个 M05 挂起对象产出显式重评估修订计划，不写任何东西。

    record: 待重评估对象（ belief 形态：object_id/proposition 或 content/
            epistemic_type/subject_ref/evidence[span]/source_event_ids）。
    states: M05 对象状态；edges: 派生依赖边；window: 冻结源记录。
    epoch:  当前 validity_epoch（供 belief_corrected 传播计划）。

    只有 needs_reassessment 且剩余**有效独立**支持可逐字核验、证据层可晋升
    时，才产出 belief_corrected 修订（旧版 superseded、新版另立）；否则
    暂缓（仍 pending）或挡下（已失效，单调永不复活）。

    返回 {reassessment_id, object_id, outcome, reason_code, reasons,
          correction, new_candidate, new_edges, propagation,
          remaining_supports, independent_support, status, provenance,
          limitations}。内容寻址 reassessment_id（不含 now）。
    """
    at = time.time() if now is None else float(now)
    rec = record if isinstance(record, dict) else {}
    oid = _oid(rec)
    prior = states if isinstance(states, dict) else {}
    window_by_id = _window_index(window)
    base = {'reassessment_id': '', 'object_id': oid, 'outcome': 'deferred',
            'reason_code': '', 'reasons': [], 'correction': None,
            'new_candidate': None, 'new_edges': [], 'propagation': None,
            'remaining_supports': [], 'independent_support': False,
            'status': 'planned',
            'provenance': {'method': 'explicit_reassessment_revision',
                           'algorithm_version': ALGORITHM_VERSION,
                           'llm_reinterpretation': False},
            'limitations': list(LIMITATIONS)}

    def finish(outcome, code, reasons=(), **extra):
        out = dict(base, outcome=outcome, reason_code=code, reasons=list(reasons))
        out.update(extra)
        out['reassessment_id'] = 'm08ra_' + _sha(
            ALGORITHM_VERSION, oid, outcome, code,
            _canonical(_without_clock(out['correction'], ('ts',))),
            _canonical(_without_clock(out['new_candidate'], ('recorded_at',))))
        return out

    if not oid:
        return finish('deferred', 'missing_object_id')
    status = _text((prior.get(oid) or {}).get('status')) if isinstance(
        prior.get(oid), dict) else ''
    # 失效单调：已失效/被取代的对象永不重评估复活。
    if status in _TRUTH_LOST:
        return finish('blocked', 'prior_invalidation_preserved')
    if status != _PENDING:
        return finish('deferred', 'not_pending_reassessment')

    remaining, independent = _valid_remaining(oid, prior, window_by_id)
    if not remaining:
        return finish('deferred', 'no_valid_support_remains',
                      remaining_supports=remaining, independent_support=independent)
    if not independent:
        # 同根源转述不算独立支持（M04/M05 同一口径），只登记不升级。
        return finish('deferred', 'same_root_support_only',
                      remaining_supports=remaining, independent_support=independent)
    tier = _tier(rec)
    if tier in NON_PROMOTABLE_TIERS or tier not in PROMOTABLE_TIERS:
        return finish('deferred', 'tier_not_promotable',
                      remaining_supports=remaining, independent_support=independent)

    # 用剩余有效支持重建新版本候选：原命题不变，依据只挂仍有效的原句。
    proposition = _text(rec.get('proposition') or rec.get('content')
                        or rec.get('restatement'))
    spans = [s for s in (rec.get('evidence') or [])
             if isinstance(s, dict) and _text(s.get('event_id')) in remaining]
    new_oid = 'm08rev_' + _sha(oid, proposition, _canonical(spans))
    # 新版本 belief 的账本 ID：派生边与 belief_corrected 的 new_revision_id
    # 都按它定位，未来对剩余源的失效才能正确传播到这条新信念。
    new_ledger_id = _ledger_id(new_oid, 'belief')
    candidate = {
        'object_kind': 'belief', 'object_id': new_oid, 'content': proposition,
        'epistemic_type': tier, 'visibility': _text(rec.get('visibility')) or 'private',
        'source_session': _text(rec.get('source_session')) or 'private:reassessment',
        'subject_ref': _text(rec.get('subject_ref')),
        'source_event_ids': list(remaining), 'evidence': spans,
        'recorded_at': at,
    }
    ok, reasons, bound = _evidence_check(candidate, window, at)
    if not ok or not bound:
        return finish('deferred', 'evidence_not_verifiable', reasons,
                      remaining_supports=remaining, independent_support=independent)
    candidate['evidence'] = [
        {'event_id': s['event_id'], 'quote': s['quote'],
         'start': s['span_start'], 'end': s['span_end'],
         'source_revision': s.get('source_revision', ''),
         'match_kind': s.get('match_kind', 'lexical')} for s in bound]

    correction = invalidation_dag.correction(
        'belief_corrected', oid, _text(reason) or '重新评估剩余有效支持',
        ts=at, new_revision_id=new_ledger_id)
    new_edges = [{'derived_id': new_ledger_id, 'source_id': s, 'derived_kind': 'belief',
                  'source_kind': 'event', 'basis': 'm08.reassessment'}
                 for s in sorted(set(remaining))]
    combined = [dict(e) for e in (edges or [])] + new_edges
    propagation = invalidation_dag.propagate(combined, [correction], epoch=epoch,
                                             states=prior, now=at)
    return finish('revised', 'independent_valid_support_reverified', reasons,
                  correction=correction, new_candidate=candidate,
                  new_edges=new_edges, propagation=propagation,
                  remaining_supports=remaining, independent_support=independent)


def commit_reassessment(ledger, plan):
    """把重评估计划追加进内存账本；旧记录一字节不动（S03 口径）。"""
    _check_ledger(ledger)
    if not isinstance(plan, dict) or plan.get('outcome') not in REASSESSMENT_OUTCOMES:
        raise ValueError('reassessment plan must come from plan_reassessment')
    rid = _text(plan.get('reassessment_id'))
    oid = _text(plan.get('object_id'))
    if not rid or not oid:
        raise ValueError('reassessment requires reassessment_id and object_id')
    if rid in ledger['records']:
        raise ValueError('reassessment already exists; history is append-only: %s' % rid)
    expected = 'm08ra_' + _sha(plan.get('provenance', {}).get('algorithm_version'),
                               oid, plan.get('outcome'), plan.get('reason_code'),
                               _canonical(_without_clock(plan.get('correction'), ('ts',))),
                               _canonical(_without_clock(plan.get('new_candidate'),
                                                         ('recorded_at',))))
    if rid != expected:
        raise ValueError('reassessment_id does not match its content')
    record = copy.deepcopy(plan)
    record['status'] = 'committed'
    ledger['records'][rid] = record
    ledger['order'].append(rid)
    return copy.deepcopy(record)


# ── 纯读视图：GET 不得启动任何写流程（S03 先例）────────────────────


def get_record(ledger, record_id):
    _check_ledger(ledger)
    record = ledger['records'].get(_text(record_id))
    return copy.deepcopy(record) if record is not None else None


def list_records(ledger):
    _check_ledger(ledger)
    return list(ledger['order'])


def explain_plan(ledger, plan_id):
    """回答「这次巩固为什么这样分」：逐条归宿 + 稳定 reason_code，不编叙事。"""
    record = get_record(ledger, plan_id)
    if record is None:
        raise ValueError('plan_not_found: %s' % plan_id)
    return {'plan_id': record['plan_id'], 'budget': record['budget'],
            'counts': dict(record['counts']),
            'decisions': [{'object_id': d['object_id'], 'fate': d['fate'],
                           'reason_code': d['reason_code'], 'tier': d['tier']}
                          for d in record['decisions']],
            'reassessment_needed': list(record['reassessment_needed']),
            'promoted': [d['object_id'] for d in record['promotions']],
            'reasons': [d['reason_code'] for d in record['decisions']]}


def explain_reassessment(ledger, reassessment_id):
    """回答「这个挂起对象为什么被修订/暂缓/挡下」：全部来自账本事实记录。"""
    record = get_record(ledger, reassessment_id)
    if record is None:
        raise ValueError('reassessment_not_found: %s' % reassessment_id)
    return {'reassessment_id': record['reassessment_id'],
            'object_id': record['object_id'], 'outcome': record['outcome'],
            'reason_code': record['reason_code'],
            'reasons': list(record['reasons']),
            'remaining_supports': list(record['remaining_supports']),
            'independent_support': record['independent_support'],
            'new_revision_id': (record.get('correction') or {}).get('new_revision_id', ''),
            'correction_id': (record.get('correction') or {}).get('correction_id', '')}


# ── 只读桥：缺库缺文件报 absent/degraded，绝不建库建表建目录（X15）──


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


def _json_list(raw):
    try:
        got = json.loads(raw or '[]')
        return got if isinstance(got, list) else []
    except Exception:
        return []


def read_consolidation_inputs():
    """只读收集巩固/重评估输入：episode、belief（含原句 span）、隔离候选、
    M05 状态/边/epoch。复用 M04/M05 冻结只读桥，不另长第二套读取口径。

    缺库报 absent，读坏报 unreadable，绝不补造。belief 的 evidence 由
    claim_span 逐命题原句重建（供重评估只挂仍有效的原句），绝不整窗冒充。
    """
    out = {'episodes': [], 'beliefs': [], 'candidates': [], 'states': {},
           'edges': [], 'epoch': 0, 'degradation': []}
    connection = _ro(_cognition_path())
    if connection is None:
        out['degradation'].append({'store': 'cognition', 'reason': 'absent'})
        return out
    try:
        tables = _tables(connection)
        if 'episode' in tables:
            for row in connection.execute(
                    'SELECT id,ts,summary,event_ids,perspective,is_legacy '
                    'FROM episode ORDER BY id'):
                out['episodes'].append({
                    'object_id': row['id'], 'summary': row['summary'],
                    'occurred_at': row['ts'], 'perspective': row['perspective'],
                    'is_legacy': row['is_legacy'],
                    'source_event_ids': _json_list(row['event_ids'])})
        spans_by_belief = {}
        if 'claim_span' in tables:
            for row in connection.execute(
                    'SELECT belief_id,event_id,quote,span_start,span_end,'
                    'source_revision,match_kind,subject_ref,subject_basis '
                    'FROM claim_span ORDER BY belief_id,event_id,span_start'):
                spans_by_belief.setdefault(row['belief_id'], []).append({
                    'event_id': row['event_id'], 'quote': row['quote'],
                    'span_start': row['span_start'], 'span_end': row['span_end'],
                    'source_revision': row['source_revision'],
                    'match_kind': row['match_kind'],
                    'subject_ref': row['subject_ref'],
                    'subject_basis': row['subject_basis']})
        if 'belief' in tables:
            for row in connection.execute(
                    'SELECT id,proposition,subject_qq,confidence,status,source,'
                    'first_ts,last_ts,is_legacy FROM belief ORDER BY id'):
                spans = spans_by_belief.get(row['id'], [])
                out['beliefs'].append({
                    'object_id': row['id'], 'proposition': row['proposition'],
                    'subject_ref': row['subject_qq'], 'status': row['status'],
                    'is_legacy': row['is_legacy'], 'confidence': row['confidence'],
                    'source_event_ids': [s['event_id'] for s in spans],
                    'evidence': spans})
        if 'claim_candidate' in tables:
            for row in connection.execute(
                    'SELECT id,restatement,kind,subject_ref,decision,reasons,'
                    'evidence,source_session,visibility,state,created '
                    'FROM claim_candidate ORDER BY id'):
                out['candidates'].append({
                    'object_id': row['id'], 'content': row['restatement'],
                    'object_kind': row['kind'] or 'claim',
                    'subject_ref': row['subject_ref'], 'decision': row['decision'],
                    'state': row['state'], 'reasons': _json_list(row['reasons']),
                    'evidence': _json_list(row['evidence']),
                    'source_session': row['source_session'],
                    'visibility': row['visibility'], 'recorded_at': row['created']})
    except sqlite3.Error:
        out['degradation'].append({'store': 'cognition', 'reason': 'unreadable'})
    finally:
        connection.close()
    # M05 状态/边/epoch 复用冻结只读桥（同一 mode=ro + query_only 口径）。
    state = invalidation_dag.read_invalidation_state()
    out['states'] = state['states']
    out['edges'] = state['edges']
    out['epoch'] = state['epoch']
    return out


def freeze_window(event_ids):
    """只读冻结这批对象引用的真实事件（走 self_model.source_window）。

    缺库/缺事件返回空窗口：原句自然绑不上，按降级处理，绝不补造事件。
    """
    refs = []
    for eid in (event_ids or []):
        key = str(eid or '').strip()
        if key and key not in refs:
            refs.append(key)
    return self_model.source_window(refs)


# ── 落账：只走 M06/M05 既有事务入口，不造第二套写入（M07 先例）──────


def _promotion_edges(decision, candidate):
    """晋升对象的派生→来源边（belief/episode → 有效事件），按账本 ID 定位。"""
    kind = 'episode' if decision['fate'] == 'experience' else 'belief'
    derived_id = decision.get('ledger_id') or _ledger_id(_oid(candidate), decision['fate'])
    if decision['spans']:
        sources = sorted({s['event_id'] for s in decision['spans']})
    else:
        sources = sorted(set(_sources(candidate)))
    return [{'derived_id': derived_id, 'source_id': s, 'derived_kind': kind,
             'source_kind': 'event', 'basis': 'm08.consolidation'} for s in sources]


def apply_consolidation(plan, window=None, *, now=None):
    """把巩固计划里的晋升经 M06 单一写入事务落账，依赖边走 M05 事务注册。

    只执行 plan 里 fate∈{experience,belief} 的晋升；temporary 不写。每条
    晋升各自走 cognition_writer.commit 的原子事务（权威重跑 M01/M02 门）；
    依赖边在一个 write_invalidation_transaction 里注册（只加边，不推进
    epoch）。坏输入/门降级如实登记，绝不把推断/想象写成事实。

    返回只读投影 {status, promoted, deferred, rejected, duplicates,
    edges_added, epoch, promotions[], provenance, limitations}。
    """
    at = time.time() if now is None else float(now)
    if not isinstance(plan, dict) or 'promotions' not in plan:
        raise ValueError('apply_consolidation requires a consolidation_plan')
    frozen = list(window or [])
    state = invalidation_dag.read_invalidation_state()
    inputs = invalidation_dag.read_derivation_inputs()
    existing = {o['object_id'] for o in inputs['objects']}
    by_oid = {_oid(c): c for c in (plan.get('candidates') or [])}

    entries, write_edges = [], []
    for decision in plan['promotions']:
        oid = decision['object_id']
        ledger_oid = decision.get('ledger_id') or _ledger_id(oid, decision['fate'])
        candidate = by_oid.get(oid)
        entry = {'object_id': oid, 'fate': decision['fate'],
                 'reason_code': decision['reason_code'], 'decision': None,
                 'reasons': list(decision['reasons']), 'ledger_id': ''}
        if candidate is None:
            entry['decision'] = 'reject'
            entry['reasons'].append('candidate_not_in_plan')
            entries.append(entry)
            continue
        if ledger_oid in existing:
            entry['decision'] = 'duplicate'
            entry['ledger_id'] = ledger_oid
            entries.append(entry)
            write_edges.extend(_promotion_edges(decision, candidate))
            continue
        projection = cognition_writer.commit(candidate, frozen, now=at)
        entry['decision'] = projection['decision']
        entry['reasons'].extend(projection['reasons'])
        entry['ledger_id'] = projection['object_ids'].get(
            'episode' if decision['fate'] == 'experience' else 'belief', '')
        if projection['decision'] == 'accept':
            write_edges.extend(_promotion_edges(decision, candidate))
        entries.append(entry)

    edges_added = 0
    if write_edges:
        unique = list({(e['derived_id'], e['source_id']): e for e in write_edges}.values())
        result = self_model.write_invalidation_transaction({
            'ts': at, 'expected_epoch': state['epoch'],
            'edges': unique, 'corrections': [], 'propagation': None})
        edges_added = result['edges_added']

    promoted = sum(1 for e in entries if e['decision'] == 'accept')
    duplicates = sum(1 for e in entries if e['decision'] == 'duplicate')
    deferred = sum(1 for e in entries if e['decision'] == 'quarantine')
    rejected = sum(1 for e in entries if e['decision'] == 'reject')
    return {'status': 'ok', 'promoted': promoted, 'duplicates': duplicates,
            'deferred': deferred, 'rejected': rejected, 'edges_added': edges_added,
            'epoch': state['epoch'], 'promotions': entries,
            'provenance': {'algorithm_version': ALGORITHM_VERSION,
                           'window_size': len(frozen), 'budget': plan.get('budget'),
                           'invalidation_epoch': state['epoch'],
                           'degradation': inputs['degradation']},
            'limitations': list(LIMITATIONS)}


def apply_reassessment(plan, window=None, *, now=None):
    """把重评估修订经 M06/M05 既有事务落账：新版 belief 另立 + 旧版 superseded。

    只有 outcome=='revised' 才写：先 cognition_writer.commit 写新版本 belief
    （原子），再 write_invalidation_transaction 注册新边 + belief_corrected
    修正 + 传播（推进 epoch，旧版 superseded、历史保留）。deferred/blocked
    不写任何东西（单调：绝不把失效对象洗回 active）。

    乐观并发：apply 时重读当前 epoch/state 重算传播，撞车由 M05 事务拒绝。
    返回只读投影 {status, outcome, reason_code, new_belief_id, correction_id,
    epoch, edges_added, decision, reasons, provenance, limitations}。
    """
    at = time.time() if now is None else float(now)
    if not isinstance(plan, dict) or plan.get('outcome') not in REASSESSMENT_OUTCOMES:
        raise ValueError('apply_reassessment requires a plan_reassessment record')
    out = {'status': 'ok', 'outcome': plan['outcome'],
           'reason_code': plan['reason_code'], 'new_belief_id': '',
           'correction_id': '', 'epoch': 0, 'edges_added': 0, 'decision': None,
           'reasons': list(plan.get('reasons') or []),
           'provenance': {'algorithm_version': ALGORITHM_VERSION},
           'limitations': list(LIMITATIONS)}
    if plan['outcome'] != 'revised':
        return out
    frozen = list(window or [])
    state = invalidation_dag.read_invalidation_state()
    candidate = plan['new_candidate']
    projection = cognition_writer.commit(candidate, frozen, now=at)
    out['decision'] = projection['decision']
    out['reasons'].extend(projection['reasons'])
    out['new_belief_id'] = projection['object_ids'].get('belief', '')
    if projection['decision'] != 'accept':
        # 门收紧：新版本没落成已核验信念，就不推进旧版 superseded（出声）。
        out['outcome'] = 'deferred'
        out['reason_code'] = 'revision_gate_demoted'
        return out
    correction = plan['correction']
    combined = [dict(e) for e in state['edges']] + [dict(e) for e in plan['new_edges']]
    propagation = invalidation_dag.propagate(combined, [correction],
                                             epoch=state['epoch'],
                                             states=state['states'], now=at)
    result = self_model.write_invalidation_transaction({
        'ts': at, 'expected_epoch': state['epoch'], 'edges': plan['new_edges'],
        'corrections': [correction], 'propagation': propagation})
    out['correction_id'] = correction['correction_id']
    out['epoch'] = result['epoch']
    out['edges_added'] = result['edges_added']
    return out