# -*- coding: utf-8 -*-
"""M06 单一认知写入入口：准入门 → 逐命题来源门 → 一个事务。

《人格内核路线》§8.2「单一状态写入者」与 §8.1「同一事务提交」在这里
落成一条可执行的写入路径：

  1. cognition_admission.admit（M01）先决定这个候选能不能以这种身份
     进入认知层：accept / quarantine / reject；
  2. 命题类对象（claim/belief/preference）再过 claim_provenance.bind_claim
     （M02）：依据必须逐命题逐字可定位，主体必须来自真实参与者。
     来源门只能收紧准入的结论，绝不放宽（reject > quarantine > accept）；
  3. 所有落账都走 self_model.write_cognition_transaction：多对象同一
     连接、同一 BEGIN IMMEDIATE 事务，要么全写、要么全回滚，绝不留下
     半条信念/半套证据。

旁路建内核、逐步接管（§3.3）：本模块是新建的统一入口，不改线任何旧
写入器——record_episode/add_belief/quarantine_claim 等旧路径保持原样，
把它们接过来是 M07/M08 的范围。因此这里只 import 两个契约与 self_model，
也不碰 retrieval_objects（R01 仍在只观察阶段，禁止接入）。

临时状态（state）在认知账本里没有对应对象表：它属于情感/工作状态层
（S 轨/C04）。准入即使通过，本入口也不替它造表、更不把它塞进长期事实，
只在投影里登记原因码，等状态层落地后再接。

模型自报 candidate_confidence 只登记在投影与隔离候选里，绝不参与准入，
也绝不写进已核验信念的 confidence（M02 §20.3 第6条）。
"""
import time

import claim_provenance
import cognition_admission
import self_model

# 命题类对象：它们的「依据」必须逐命题原句可定位（M02）。
_CLAIM_KINDS = ('claim', 'belief', 'preference')
_STATE_NOTE = 'state_layer_not_in_cognition_ledger'
_RANK = {'accept': 0, 'quarantine': 1, 'reject': 2}
_PROVENANCE_TO_DECISION = {'accepted': 'accept', 'deferred': 'quarantine', 'rejected': 'reject'}


def _text(candidate):
    """与两个契约同一套文本口径：content/restatement/text/proposition。"""
    for key in ('content', 'restatement', 'text', 'proposition'):
        value = str(candidate.get(key) or '').strip()
        if value:
            return value
    return ''


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _candidate_plan(candidate, kind, text, verdict, reasons, subject_ref, confidence):
    """隔离/拒绝候选的落账计划，字段口径与 quarantine_claim 完全一致。"""
    return {
        'restatement': text,
        'kind': kind,
        'subject_ref': subject_ref,
        'decision': verdict,
        'reasons': reasons,
        'evidence': [e for e in (candidate.get('evidence') or []) if isinstance(e, dict)],
        'source_session': str(candidate.get('source_session') or ''),
        'visibility': candidate.get('visibility'),
        'candidate_confidence': confidence,
    }


def commit(candidate, window, now=None):
    """单一认知写入入口。返回只读投影，绝不返回账本句柄。

    candidate: M01 信封（object_kind/object_id/content/recorded_at/
               epistemic_type/visibility/source_session/source_event_ids…）
               加 M02 的 evidence[]/subject_ref/qualifiers 等字段；
    window:    本次冻结的源记录列表（self_model.source_window 产出）。
               空窗口意味着命题拿不到可核对的原句，自然被来源门挡下。

    投影: {decision, kind, reasons, current, object_ids, spans,
           subject_ref, subject_basis, candidate_confidence,
           provenance_decision}。reasons 是稳定 reason_code，可直接留账。

    事务失败原样抛异常（此时已整体回滚）：沉默的写入路径视为故障。
    幂等：belief/episode/commitment 按 object_id 派生稳定 ID，候选沿用
    quarantine_claim 的文本+类型+判定 dedupe 键，重复提交不堆垃圾。
    """
    at = time.time() if now is None else float(now)
    cand = candidate if isinstance(candidate, dict) else {}

    # ── 门 1：类型化准入（M01）。任何落账之前先过这道门。 ──
    admission = cognition_admission.admit(cand, now=at)
    kind = admission['kind']
    decision = admission['decision']
    reasons = list(admission['reasons'])
    current = admission['current']

    # ── 门 2：逐命题来源（M02）。准入已拒的不必再核原句。 ──
    provenance = None
    if kind in _CLAIM_KINDS and decision != 'reject':
        provenance = claim_provenance.bind_claim(cand, window or [], now=at)
        reasons.extend(provenance['reasons'])
        # 两道门取更严的那个：来源门只能收紧，绝不放宽准入结论。
        mapped = _PROVENANCE_TO_DECISION[provenance['decision']]
        if _RANK[mapped] > _RANK[decision]:
            decision = mapped
    if decision != 'accept':
        current = False

    text = _text(cand)
    spans = list(provenance['spans']) if provenance else []
    subject_ref = provenance['subject_ref'] if provenance else str(cand.get('subject_ref') or '')
    subject_basis = provenance['subject_basis'] if provenance else ''
    if provenance is not None:
        confidence = provenance['candidate_confidence']
    else:
        raw = cand.get('candidate_confidence')
        confidence = float(raw) if _number(raw) else None

    projection = {
        'decision': decision, 'kind': kind, 'reasons': reasons, 'current': current,
        'object_ids': {}, 'spans': spans, 'subject_ref': subject_ref,
        'subject_basis': subject_basis, 'candidate_confidence': confidence,
        'provenance_decision': provenance['decision'] if provenance else None,
    }

    if decision in ('quarantine', 'reject'):
        # 隔离/拒绝绝不写已核验事实。命题类留一条候选供审计：deferred 与
        # rejected 如实区分，dedupe 沿用现有口径，重复整理不堆垃圾。
        if decision == 'quarantine' or (kind in _CLAIM_KINDS and text):
            verdict = 'deferred' if decision == 'quarantine' else 'rejected'
            projection['object_ids'] = self_model.write_cognition_transaction({
                'ts': at,
                'candidate': _candidate_plan(cand, kind, text, verdict, reasons,
                                             subject_ref, confidence),
            })
        return projection

    # ── accept：按类型写对应对象，全部落在同一个事务里。 ──
    if kind == 'state':
        # 认知账本没有临时状态对象：不造表、不塞进长期事实，登记原因。
        projection['reasons'] = reasons + [_STATE_NOTE]
        return projection
    object_id = str(cand.get('object_id') or '')
    if kind in _CLAIM_KINDS:
        plan = {'ts': at, 'belief': {
            'object_id': object_id, 'proposition': text, 'subject_ref': subject_ref,
            # 自报置信度只登记在投影/候选，不写进信念 confidence。
            'confidence': None,
            'source': str(cand.get('source') or 'cognition_writer'),
            'spans': spans,
        }}
    elif kind == 'episode':
        occurred = cand.get('occurred_at')
        plan = {'ts': at, 'episode': {
            'object_id': object_id, 'summary': text,
            'event_ids': [str(x) for x in (cand.get('source_event_ids') or [])
                          if str(x or '').strip()],
            'perspective': str(cand.get('perspective') or 'self'),
            # 发生时间未知就用记录时间，不补造生平（与 record_episode 同口径）。
            'ts': float(occurred) if _number(occurred) and occurred > 0 else at,
        }}
    elif kind == 'commitment':
        due = cand.get('due_ts')
        plan = {'ts': at, 'commitment': {
            'object_id': object_id, 'to_qq': str(cand.get('to_qq') or ''), 'text': text,
            'due_ts': float(due) if _number(due) else None,
            'event_ids': [str(x) for x in (cand.get('source_event_ids') or [])
                          if str(x or '').strip()],
        }}
    else:
        # unknown_kind 到不了 accept；防御性出声，而不是静默丢弃。
        raise ValueError('unhandled cognition kind: %s' % kind)
    projection['object_ids'] = self_model.write_cognition_transaction(plan)
    return projection