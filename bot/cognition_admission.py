# -*- coding: utf-8 -*-
"""M01 类型化准入契约：情景、状态、信念与承诺各走各的门。

统一方案 §6.2/§7.2 的可执行定义：Episode/Claim/Belief/State/Preference/Commitment
不是同一种「记忆」；临时状态、一时想法、工具完成不混写进长期事实。

本模块是纯函数契约：不读写任何账本、不发网络、不调模型。它只回答
「这个候选能不能以这种身份进入认知层」：
  accept     —— 可以按该类型接收（落账仍走 self_model 的显式 API）；
  quarantine —— 留作隔离候选/pending_review，绝不当前已核验事实消费；
  reject     —— 不写。
把契约接进唯一写入路径是 M06 的范围；这里先把门本身定死，让
「喜欢牛肉面≠喜欢所有面食」「已生成≠对方已读」「玩笑≠现实人物事实」
有机器可判的边界，而不是靠提取器自觉。
"""
import time

KINDS = ('episode', 'claim', 'belief', 'state', 'preference', 'commitment')
EPISTEMIC_TYPES = ('experienced', 'reported', 'looked_up', 'inferred', 'imagined')
VISIBILITIES = ('private', 'group', 'public')
FICTION_CONTEXTS = ('fiction', 'roleplay', 'tarot', 'joke')
DECISIONS = ('accept', 'quarantine', 'reject')
# 发生时间不可能晚于记录时间；只容忍秒级时钟 skew，不容忍补造生平。
_TIME_SKEW_SECONDS = 1.0
_FACTUAL_KINDS = ('episode', 'belief', 'state', 'preference', 'commitment')
_TOOL_RECEIPT_CLAIMS = ('sent', 'delivered', 'read', 'accepted')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _sources(candidate):
    return [str(x) for x in (candidate.get('source_event_ids') or []) if str(x or '').strip()]


def _text(candidate):
    for key in ('content', 'restatement', 'text', 'proposition'):
        value = str(candidate.get(key) or '').strip()
        if value:
            return value
    return ''


def admit(candidate, now=None):
    """判定一个认知候选的准入。返回 {kind, decision, reasons, current}。

    reasons 是稳定 reason_code，可直接留账/展示；current=False 表示
    即使接收也只能当历史，不得作为「现在」注入（例如已过期的临时状态）。
    """
    at = time.time() if now is None else float(now)
    cand = candidate if isinstance(candidate, dict) else {}
    kind = str(cand.get('object_kind') or '')
    reject, hold, notes = [], [], []
    current = True

    if kind not in KINDS:
        return {'kind': kind, 'decision': 'reject', 'reasons': ['unknown_kind'], 'current': False}

    # ── 共同信封（§6.1 / A05）：身份、时间、范围、认识类型、来源 ──
    if not str(cand.get('object_id') or '').strip():
        reject.append('missing_object_id')
    if not _text(cand):
        reject.append('missing_content')
    recorded = cand.get('recorded_at')
    if not _number(recorded):
        reject.append('missing_recorded_at')
    occurred = cand.get('occurred_at')
    if occurred is not None:
        if not _number(occurred):
            reject.append('invalid_occurred_at')
        else:
            if occurred > at + _TIME_SKEW_SECONDS:
                reject.append('future_occurrence')
            if _number(recorded) and occurred > recorded + _TIME_SKEW_SECONDS:
                reject.append('occurrence_after_record')
    if cand.get('synthetic') or cand.get('evidence_origin') == 'verification':
        reject.append('synthetic_source')
    epistemic = str(cand.get('epistemic_type') or '')
    if epistemic not in EPISTEMIC_TYPES:
        reject.append('unknown_epistemic_type')
    if cand.get('visibility') not in VISIBILITIES or not str(cand.get('source_session') or '').strip():
        reject.append('no_scope')
    sources = _sources(cand)
    if not sources:
        if kind == 'claim':
            hold.append('unsupported_claim')
        elif kind in _FACTUAL_KINDS:
            reject.append('no_source_events')

    # ── 工具完成分型（§7.2）：生成≠发送≠送达≠已读，后两者没有回执不代填 ──
    tool_claim = cand.get('tool_claim')
    if tool_claim is not None:
        if tool_claim == 'generated':
            if not str(cand.get('artifact_ref') or '').strip():
                reject.append('tool_generation_needs_artifact')
        elif tool_claim in _TOOL_RECEIPT_CLAIMS:
            if not [str(x) for x in (cand.get('receipt_event_ids') or []) if str(x or '').strip()]:
                reject.append('tool_completion_needs_receipt')
        else:
            reject.append('unknown_tool_claim')

    # ── 玩笑/小说/塔罗：可以当聊天情景保留，不能晋升为现实人物事实（§7.2）──
    fiction = cand.get('context_kind') in FICTION_CONTEXTS
    if fiction and kind in ('belief', 'preference', 'commitment'):
        reject.append('fiction_not_fact')
    elif fiction and kind == 'claim':
        hold.append('fiction_claim')

    # ── 类型专属门 ──
    if kind == 'episode':
        perspective = str(cand.get('perspective') or 'self')
        if perspective == 'self' and epistemic == 'reported':
            # 同段里别人的经历不能自动称为她亲历（§6.2）。
            reject.append('not_her_experience')
    elif kind == 'claim':
        if epistemic == 'inferred':
            hold.append('inferred_claim')
    elif kind == 'belief':
        if epistemic in ('inferred', 'imagined'):
            reject.append('belief_layer_not_fact')
    elif kind == 'state':
        valid_until = cand.get('valid_until')
        if not _number(valid_until):
            reject.append('state_needs_expiry')
        elif valid_until <= at:
            # 过期状态停止当「现在」注入，历史仍在（§7.2）。
            current = False
            notes.append('expired_state')
    elif kind == 'preference':
        if cand.get('explicit_self_report') is True and cand.get('actor_is_subject') is True:
            # 只收「该人明确自述」这个命题本身；是否稳定偏好由后续独立证据决定，
            # 不擅自推广（牛肉面≠所有面食），也不从单次情绪表达晋升。
            notes.append('statement_not_generalized')
        else:
            hold.append('preference_needs_self_report')
    elif kind == 'commitment':
        if cand.get('source') == 'affection.parse_promise' or cand.get('affection_promise'):
            # 「答应加分」是专用语义，不因名称 promise 自动映射为一般承诺（§6.2）。
            reject.append('affection_promise_not_commitment')
        if not str(cand.get('to_qq') or '').strip():
            reject.append('commitment_needs_object')
        if cand.get('accepted_by_self') is not True:
            reject.append('commitment_needs_acceptance')
        if not str(cand.get('completion_criteria') or '').strip():
            reject.append('commitment_needs_criteria')

    if reject:
        decision = 'reject'
    elif hold:
        decision = 'quarantine'
    else:
        decision = 'accept'
    if decision != 'accept':
        current = False
    return {'kind': kind, 'decision': decision,
            'reasons': reject + hold + notes, 'current': current}