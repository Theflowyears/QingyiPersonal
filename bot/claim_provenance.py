# -*- coding: utf-8 -*-
"""M02 逐命题原句来源契约（统一方案 §6.1 来源字段组 / §20.3 固定验证顺序）。

X04 的病灶是「有事件号就算有依据」：一轮整理读到 8 个事件，就把这 8 个事件
整窗挂给每一条结论。本模块把依据粒度降到**一条命题一句原话**：quote 必须是
真实源文本里的连续逐字片段，offset 由代码核对，主体从真实 actor/topic 拿，
模型自报 confidence 只登记、不参与准入。

纯函数：不读写账本、不发网络、不调模型、不自带分词器。定位候选原句由调用方
用系统唯一分词口径（text_tokens / persona_store）做，这里只负责核对，
避免长出第二套词法口径。落账走 self_model 的显式 API；接进唯一写入 reducer
是 M06 的范围。

边界（§22.4）：机器只能证明「原句逐字可定位」，不能证明「原句语义支持命题」。
后者是自然证据，所以每条 span 都带 match_kind，lexical 命中不许冒充语义核验。
"""
import time

DECISIONS = ('accepted', 'deferred', 'rejected')
MATCH_KINDS = ('model_quote', 'lexical')
# 否定词在摘述里消失＝意思反转，直接拒；条件/时态限定词消失＝降级暂缓，
# 允许调用方把限定词显式登记在 qualifiers/time_expression 里而不是塞进摘述。
NEGATION_MARKERS = ('不', '没', '别', '未', '无', '非', '讨厌', '拒绝')
QUALIFIER_MARKERS = ('可能', '也许', '大概', '应该', '听说', '好像', '似乎', '暂时',
                     '今天', '昨天', '明天', '最近', '以前', '现在', '有时', '偶尔',
                     '经常', '总是', '如果', '要是', '除非', '只有', '打算', '想要')
# 这些称呼一旦出现就必须能落到真实参与者，否则就是「按称呼把别人信息归给主人」。
PERSON_MARKERS = ('主人', '爸爸', '妈妈', '对方')
TEST_SESSION_MARKERS = ('verify:', 'probe:', 'test:', 'p-', 't-')
_TIME_SKEW_SECONDS = 1.0
# 源本身不合格就不该继续核对 quote：这些是硬拒，其余源问题留暂缓。
_SOURCE_REJECT = frozenset(('source_missing_id', 'source_retracted', 'synthetic_source',
                            'test_polluted_source', 'future_source', 'invalid_source_time'))


def _text(claim):
    for key in ('restatement', 'content', 'proposition', 'text'):
        value = str(claim.get(key) or '').strip()
        if value:
            return value
    return ''


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _occurrences(source, quote):
    """非重叠出现位置。多处出现时不许猜是哪一处。"""
    out, at = [], source.find(quote)
    while at >= 0:
        out.append(at)
        at = source.find(quote, at + len(quote))
    return out


def verify_quote(source_text, quote, start=None, end=None):
    """核对 quote 是源文本的连续逐字片段，并给出可信 offset。

    返回 {ok, quote, span_start, span_end, reason}。reason 是稳定码：
    offset_corrected 表示模型给的数字错了、但 quote 在原文只出现一次，
    按唯一位置修正并留痕；ambiguous_* 表示多处出现，暂缓而不是猜。
    """
    source = str(source_text or '')
    wanted = str(quote or '')
    blank = {'ok': False, 'quote': '', 'span_start': None, 'span_end': None}
    if not wanted.strip():
        return dict(blank, reason='empty_quote')
    if wanted not in source:
        return dict(blank, reason='quote_not_verbatim')
    hits = _occurrences(source, wanted)
    if start is not None or end is not None:
        if not (_number(start) and _number(end)) or start < 0 or end > len(source) or start >= end:
            return dict(blank, reason='invalid_offset')
        if source[int(start):int(end)] == wanted:
            return {'ok': True, 'quote': wanted, 'span_start': int(start),
                    'span_end': int(end), 'reason': ''}
        if len(hits) == 1:
            at = hits[0]
            return {'ok': True, 'quote': wanted, 'span_start': at,
                    'span_end': at + len(wanted), 'reason': 'offset_corrected'}
        return dict(blank, reason='ambiguous_offset')
    if len(hits) == 1:
        at = hits[0]
        return {'ok': True, 'quote': wanted, 'span_start': at,
                'span_end': at + len(wanted), 'reason': ''}
    return dict(blank, reason='ambiguous_quote')


def source_reasons(source, now=None):
    """§20.3 第4条：源本身可用、非测试污染、时序有效。"""
    at = time.time() if now is None else float(now)
    row = source if isinstance(source, dict) else {}
    out = []
    if not str(row.get('event_id') or '').strip():
        out.append('source_missing_id')
    if row.get('retracted'):
        out.append('source_retracted')
    if row.get('synthetic') or row.get('evidence_origin') == 'verification':
        out.append('synthetic_source')
    session = str(row.get('session') or '')
    if any(session.startswith(marker) for marker in TEST_SESSION_MARKERS):
        out.append('test_polluted_source')
    occurred = row.get('occurred_at')
    if occurred is not None:
        if not _number(occurred) or occurred <= 0:
            out.append('invalid_source_time')
        elif occurred > at + _TIME_SKEW_SECONDS:
            out.append('future_source')
    if not str(row.get('text') or ''):
        out.append('source_text_missing')
    return out


def resolve_subject(claim, sources):
    """主体只能来自真实 actor/topic 或已确认映射（§20.3 / X13）。

    返回 (subject_ref, basis, reasons)。没有声明人物主体的世界知识/实体命题
    留 basis='unscoped'：实体归一是 M03 的范围，这里不替它编一个实体 ID，
    但只要摘述里出现「主人/对方」这类称呼，就必须能落到真实参与者。
    """
    declared = str(claim.get('subject_ref') or claim.get('subject_qq') or '').strip()
    actors, topics = {}, {}
    for row in sources:
        for key, ref_key, table in (('actor_qq', 'actor_ref', actors),
                                    ('topic_qq', 'topic_ref', topics)):
            raw = str(row.get(key) or '').strip()
            stable = str(row.get(ref_key) or '').strip()
            for form in (raw, stable):
                if form:
                    table[form] = stable or raw
    if declared:
        if declared in actors:
            return actors[declared], 'source_actor', []
        if declared in topics:
            return topics[declared], 'source_topic', []
        return '', '', ['subject_unresolved']
    restatement = _text(claim)
    if any(marker in restatement for marker in PERSON_MARKERS):
        return '', '', ['subject_unresolved']
    return '', 'unscoped', []


def modifiers_dropped(restatement, quote, declared=()):
    """摘述里不许丢掉原句的否定与限定（§20.3 第5条）。

    限定词可以显式登记在 qualifiers/time_expression/negation 字段里，
    不要求逐字塞进摘述；否定词丢了就是意思反转，没有补登记一说。
    """
    kept = str(restatement or '')
    extra = ' '.join(str(x or '') for x in (declared or ()))
    dropped = []
    for marker in NEGATION_MARKERS:
        if marker in quote and marker not in kept:
            dropped.append(marker)
    for marker in QUALIFIER_MARKERS:
        if marker in quote and marker not in kept and marker not in extra:
            dropped.append(marker)
    return dropped


def _result(decision, reject, hold, notes, spans, subject, basis, claim):
    confidence = claim.get('candidate_confidence')
    return {
        'decision': decision,
        'reasons': reject + hold + notes,
        'spans': spans,
        'subject_ref': subject,
        'subject_basis': basis,
        # 模型自报置信度只登记，绝不参与准入判定（§20.3 第6条）。
        'candidate_confidence': float(confidence) if _number(confidence) else None,
    }


def bind_claim(claim, window, now=None):
    """按 §20.3 的固定顺序核对一条命题候选。

    claim: restatement、可选 subject_ref/subject_qq、evidence[]（每项 event_id、
           quote、可选 start/end/source_revision/match_kind）、可选 qualifiers、
           time_expression、negation、tool_claim、execution_id、result_status、
           candidate_confidence。
    window: 本次冻结的源记录列表（self_model.source_window 产出）。
    返回 {decision, reasons, spans, subject_ref, subject_basis, candidate_confidence}。
    """
    at = time.time() if now is None else float(now)
    cand = claim if isinstance(claim, dict) else {}
    restatement = _text(cand)
    sources = {}
    for row in (window or []):
        if isinstance(row, dict) and str(row.get('event_id') or '').strip():
            sources[str(row['event_id'])] = row
    evidence = [x for x in (cand.get('evidence') or []) if isinstance(x, dict)]
    if not restatement:
        return _result('rejected', ['missing_restatement'], [], [], [], '', '', cand)
    if not evidence:
        # 无依据项进隔离候选，不是「先写了再说」。
        return _result('deferred', ['no_evidence_span'], [], [], [], '', '', cand)

    reject, hold, notes, spans, bound = [], [], [], [], []
    declared = list(cand.get('qualifiers') or [])
    if cand.get('time_expression'):
        declared.append(cand['time_expression'])
    if cand.get('negation'):
        declared.append(cand['negation'])
    for item in evidence:
        event_id = str(item.get('event_id') or '').strip()
        # 1 窗口成员：禁止引用本次冻结窗口之外的未知 ID。
        if event_id not in sources:
            reject.append('event_outside_window')
            continue
        row = sources[event_id]
        # 2 源可用性
        problems = source_reasons(row, at)
        reject.extend(code for code in problems if code in _SOURCE_REJECT)
        hold.extend(code for code in problems if code not in _SOURCE_REJECT)
        if problems:
            continue
        # 3 源版本：源文本变了就不能拿旧 quote 当依据。
        revision = str(row.get('source_revision') or '')
        if item.get('source_revision') and str(item['source_revision']) != revision:
            hold.append('source_revision_mismatch')
            continue
        # 4 逐字核对 + offset
        checked = verify_quote(row.get('text'), item.get('quote'),
                               item.get('start'), item.get('end'))
        if not checked['ok']:
            if checked['reason'] in ('quote_not_verbatim', 'empty_quote', 'invalid_offset'):
                reject.append(checked['reason'])
            else:
                hold.append(checked['reason'])
            continue
        if checked['reason']:
            notes.append(checked['reason'])
        match_kind = str(item.get('match_kind') or 'model_quote')
        if match_kind not in MATCH_KINDS:
            reject.append('unknown_match_kind')
            continue
        spans.append({'event_id': event_id, 'quote': checked['quote'],
                      'span_start': checked['span_start'], 'span_end': checked['span_end'],
                      'source_revision': revision, 'match_kind': match_kind})
        bound.append(row)

    if not spans:
        decision = 'rejected' if reject else 'deferred'
        return _result(decision, reject, hold, notes, [], '', '', cand)

    # 5 主体绑定
    subject, basis, subject_problems = resolve_subject(cand, bound)
    hold.extend(subject_problems)
    # 6 否定/限定不许在摘述里消失
    dropped = []
    for span in spans:
        dropped.extend(modifiers_dropped(restatement, span['quote'], declared))
    if any(marker in dropped for marker in NEGATION_MARKERS):
        reject.append('negation_dropped')
    elif dropped:
        hold.append('qualifier_dropped')
    # 7 工具完成类命题必须有匹配执行 ID 与结果状态
    tool_claim = cand.get('tool_claim')
    if tool_claim in ('sent', 'delivered', 'read', 'accepted', 'completed'):
        if not str(cand.get('execution_id') or '').strip() or not str(cand.get('result_status') or '').strip():
            reject.append('tool_completion_needs_receipt')

    for span in spans:
        span['subject_ref'] = subject
        span['subject_basis'] = basis
    if reject:
        decision = 'rejected'
    elif hold:
        decision = 'deferred'
    else:
        decision = 'accepted'
    return _result(decision, reject, hold, notes, spans, subject, basis, cand)