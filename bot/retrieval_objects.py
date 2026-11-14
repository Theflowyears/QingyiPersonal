# -*- coding: utf-8 -*-
"""R01 多库统一只读检索对象（统一方案 §8.5 / §19.1–19.3 / §19.6）。

群、私聊档案、世界知识、看图笔记、认知信念各自持有原始资产；这里只把它们
投影成**同一种检索描述**：结构化对象、来源、有效性、范围、原文引用。
旧资产不迁移、不改写，旧检索路径继续可用（§19.5 只观察阶段）。

三条硬边界：
- 真只读。路径调用时解析；库文件不存在就报 absent，绝不建库建表建目录
  （X15：GET 辅助读路径可建表是缺陷，不是便利）。
- 范围分开。allowed_source_scopes 决定「能不能读这条来源」，recipient_scope
  决定「能不能转述给当前受众」；看见不等于可以转述，派生只能相同或更窄（X14）。
- 不装懂。源不可读返回 degraded，不声称「她没有记忆」；联想扩散是 R02/R04、
  多线索融合打分是 R03、矛盾判定是 R05，这里一律不做，也不冒充做了。
"""
import hashlib
import json
import os
import time

QUERY_FIELDS = ('logical_turn_id', 'request_attempt_id', 'as_of_time', 'query_text',
                'recent_referents', 'speaker_person_id', 'topic_person_ids',
                'recipient_scope', 'allowed_source_scopes', 'time_constraints',
                'active_commitment_ids', 'retrieval_budget', 'index_revision',
                'validity_epoch')
STATUSES = ('sufficient', 'partial', 'conflicting', 'not_found', 'degraded')
STORES = ('cognition', 'persona', 'group', 'knowledge', 'vision')
VISIBILITIES = ('private', 'group', 'public')
# 认识类型沿用 M01 的五层，不新造第六种。
EPISTEMIC_KINDS = ('experienced', 'reported', 'looked_up', 'inferred', 'imagined')
_TIME_SKEW_SECONDS = 1.0
_DEFAULT_BUDGET = 32


def _data_root():
    return os.getenv('DATA_DIR', './data')


def _persona_root():
    return os.getenv('PERSONA_DATA_DIR') or os.path.join(_data_root(), 'persona')


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def query_context(**over):
    """§19.2 的最小 QueryContext。字段一律摆出来，缺省值显式可见。"""
    ctx = {
        'logical_turn_id': '', 'request_attempt_id': '', 'as_of_time': time.time(),
        'query_text': '', 'recent_referents': [], 'speaker_person_id': '',
        'topic_person_ids': [], 'recipient_scope': '', 'allowed_source_scopes': [],
        'time_constraints': {}, 'active_commitment_ids': [],
        'retrieval_budget': _DEFAULT_BUDGET, 'index_revision': '', 'validity_epoch': '',
    }
    ctx.update({k: v for k, v in over.items() if k in QUERY_FIELDS})
    return ctx


def context_problems(ctx):
    """缺字段就出声，不让渲染层拿默认值把未知补成 verified（§19.3）。"""
    row = ctx if isinstance(ctx, dict) else {}
    problems = [ 'missing_' + field for field in QUERY_FIELDS if field not in row]
    as_of = row.get('as_of_time')
    if not isinstance(as_of, (int, float)) or isinstance(as_of, bool) or as_of <= 0:
        problems.append('invalid_as_of_time')
    if not str(row.get('recipient_scope') or '').strip():
        problems.append('missing_recipient_scope')
    if not row.get('allowed_source_scopes'):
        problems.append('no_allowed_source_scopes')
    return problems


def _budget(ctx):
    """0 是「这条都不要」，不是「没填」。用 or 兜底会把 0 悄悄变成 32。"""
    value = ctx.get('retrieval_budget')
    return _DEFAULT_BUDGET if value is None else int(value)


def _item(object_id, object_kind, store, restatement, **over):
    row = {
        'object_id': str(object_id), 'object_kind': object_kind, 'store': store,
        'revision': '', 'restatement': str(restatement or ''),
        'source_session': '', 'visibility': 'private', 'epistemic_kind': 'inferred',
        'occurred_at': None, 'recorded_at': None, 'root_source_ids': [],
        'evidence_spans': [], 'legacy': False, 'validity': 'unknown',
        'current': True, 'scope_basis': 'source_event',
        'match': {'clues': [], 'score_parts': {}},
    }
    row.update(over)
    return row


def _read_json(path):
    """只读取原始资产。不存在与读不动要分开报告，不能都当成空。"""
    if not os.path.isfile(path):
        return None, 'absent'
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle), None
    except Exception:
        return None, 'unreadable'


def _degradation(store, reason, detail=''):
    return {'store': store, 'reason': reason, 'detail': str(detail)[:120]}


# ── 各库适配器：原始资产仍由原模块持有，这里只做只读投影 ──────────


def _cognition_items(ctx, *, candidate_query=None):
    path = os.path.join(_persona_root(), 'cognition.db')
    if not os.path.isfile(path):
        return [], _degradation('cognition', 'absent')
    try:
        import self_model
        rows = self_model.retrieval_beliefs(
            limit=_budget(ctx), query='' if candidate_query is None else candidate_query, strict=True)
    except Exception as exc:
        return [], _degradation('cognition', 'unreadable', type(exc).__name__)
    items = []
    for row in rows:
        spans = row.get('evidence_spans') or []
        refs = row.get('evidence_event_ids') or []
        legacy = bool(row.get('is_legacy'))
        sessions = row.get('source_sessions') or []
        # legacy 保留自己的未知身份，不用新来源洗成已证实（§20.3）。
        epistemic = 'inferred' if legacy or not spans else 'reported'
        items.append(_item(
            row['id'], 'belief', 'cognition', row.get('proposition'),
            revision=_sha(row.get('proposition'), row.get('last_ts')),
            source_session=sessions[0] if sessions else 'cognition', source_sessions=list(sessions),
            scope_basis='source_event' if sessions else 'archive',
            visibility=row.get('visibility') if row.get('visibility') in VISIBILITIES else 'private',
            epistemic_kind=epistemic, occurred_at=row.get('first_ts'),
            recorded_at=row.get('last_ts'), root_source_ids=list(refs),
            evidence_spans=[{'event_id': s['event_id'], 'quote': s['quote'],
                             'span_start': s['span_start'], 'span_end': s['span_end'],
                             'source_revision': s['source_revision'],
                             'match_kind': s['match_kind']} for s in spans],
            legacy=legacy,
            validity='invalidated' if row.get('invalidated') else ('valid' if refs else 'unknown'),
            match={'clues': ['belief'], 'score_parts': {'grounded': 1.0 if spans else 0.0}}))
    return items, None


def _persona_items(ctx):
    path = os.path.join(_persona_root(), 'persona.db')
    if not os.path.isfile(path):
        return [], _degradation('persona', 'absent')
    try:
        import persona_store
        kinds = ('facts', 'experiences', 'likes', 'dislikes', 'curious')
        rows = []
        for kind in kinds:
            rows.extend((kind, entry) for entry in persona_store.sourced_items(kind))
    except Exception as exc:
        return [], _degradation('persona', 'unreadable', type(exc).__name__)
    # 档案条目不是「某个会话里的一句话」。有来源事件时范围由那些事件决定；
    # 没有来源的旧条目只能算档案，要显式授权才读得到（§7.5 显式回忆可恢复访问），
    # 而不是被当成 out_of_scope 静默抹掉——那等于声称她没有旧背景。
    wanted = sorted({str(x) for _kind, entry in rows
                     for x in (entry.get('source_event_ids') or []) if x})
    window = {}
    if wanted:
        try:
            import self_model
            window = {r['event_id']: r for r in self_model.source_window(wanted)}
        except Exception:
            window = {}
    items = []
    for kind, entry in rows:
        refs, sessions, visibilities, usable = [], [], [], []
        for eid in (entry.get('source_event_ids') or []):
            src = window.get(str(eid))
            if not src:
                continue
            refs.append(str(eid))
            sessions.append(str(src.get('session') or ''))
            visibilities.append(str(src.get('visibility') or 'private'))
            if not src.get('retracted') and not src.get('synthetic'):
                usable.append(str(eid))
        spans = [s for s in (entry.get('evidence_spans') or [])
                 if str(s.get('event_id')) in usable]
        if refs:
            validity = 'valid' if usable else 'invalidated'
            visibility = ('private' if 'private' in visibilities
                          else 'group' if 'group' in visibilities else 'private')
            session = sorted(set(s for s in sessions if s))[0] if any(sessions) else 'persona'
            basis = 'source_event' if session != 'persona' else 'archive'
        else:
            validity, visibility, session, basis = 'unknown', 'private', 'persona', 'archive'
        items.append(_item(
            'persona:%s:%s' % (kind, entry['id']), 'persona_' + kind, 'persona', entry['text'],
            revision=_sha(entry['text'], entry.get('ts')),
            source_session=session, source_sessions=sorted(set(sessions)), scope_basis=basis, visibility=visibility,
            epistemic_kind='reported' if usable else 'inferred',
            occurred_at=entry.get('ts'), recorded_at=entry.get('ts'),
            root_source_ids=usable, evidence_spans=spans,
            legacy=not usable, validity=validity,
            match={'clues': [kind], 'score_parts': {'sourced': 1.0 if usable else 0.0}}))
    return items, None


def _group_items(ctx):
    path = os.path.join(_data_root(), 'memory', 'group_facts.json')
    data, problem = _read_json(path)
    if problem:
        return [], _degradation('group', problem)
    items = []
    for gid, group in ((data or {}).get('groups') or {}).items():
        for entry in (group or {}).get('entries') or []:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get('text') or '').strip()
            if not text:
                continue
            refs = [str(x) for x in (entry.get('source_event_ids') or []) if x]
            # 群事实库的归档字段是 status='archived'，不是 archived 布尔位。
            archived = str(entry.get('status') or 'active') == 'archived'
            items.append(_item(
                'group:%s:%s' % (gid, entry.get('id') or _sha(text)), 'group_fact', 'group', text,
                revision=_sha(text, entry.get('ts')),
                source_session='group:%s' % gid, visibility='group',
                scope_basis='store_owner',
                epistemic_kind='reported' if refs else 'inferred',
                occurred_at=entry.get('ts'), recorded_at=entry.get('ts'),
                root_source_ids=refs, evidence_spans=list(entry.get('evidence_spans') or []),
                legacy=not refs, validity='valid' if refs else 'unknown',
                # 归档不等于假（X16）：只降当前检索权重，历史仍可回顾。
                current=not archived,
                match={'clues': ['group', str(gid)],
                     'score_parts': {'sourced': 1.0 if refs else 0.0,
                                     'archived': 1.0 if archived else 0.0}}))
    return items, None


def _knowledge_items(ctx):
    path = os.path.join(_data_root(), 'memory', 'world_knowledge.json')
    data, problem = _read_json(path)
    if problem:
        return [], _degradation('knowledge', problem)
    try:
        import knowledge_base
        records = [knowledge_base._norm_record(x) for x in (data or {}).get('topics') or []]
    except Exception as exc:
        return [], _degradation('knowledge', 'unreadable', type(exc).__name__)
    items = []
    for record in records:
        if not record or not knowledge_base.is_active(record):
            continue
        url = str(record.get('evidence_url') or '')
        items.append(_item(
            'knowledge:%s' % record['key'], 'world_knowledge', 'knowledge',
            record.get('summary'), revision=_sha(record.get('summary'), record.get('ts')),
            source_session='public:knowledge', visibility='public',
            scope_basis='public',
            epistemic_kind='looked_up', occurred_at=record.get('ts'),
            recorded_at=record.get('ts'), root_source_ids=[url] if url else [],
            evidence_spans=[], legacy=not url,
            validity='valid' if url else 'unknown',
            match={'clues': ['knowledge', str(record.get('category') or '')],
                   'score_parts': {'verified': 1.0 if record.get('verified') else 0.0}}))
    return items, None


def _vision_text(note):
    """看图笔记的真实字段是 intent/scene/objects/text_in_image，没有 meaning。

    照想象里的字段名去读，会让每条笔记都被当成空跳过：检索于是静默地声称
    「她没看过任何图」。读不到内容要报 degraded，不是报没有。
    """
    parts = []
    for field in ('intent', 'scene', 'people_action', 'mood', 'meme_guess'):
        value = str(note.get(field) or '').strip()
        if value:
            parts.append(value)
    objects = [str(x).strip() for x in (note.get('objects') or []) if str(x or '').strip()]
    if objects:
        parts.append('画面里有' + '、'.join(objects[:6]))
    written = str(note.get('text_in_image') or '').strip()
    if written:
        parts.append('图上写着「' + written[:60] + '」')
    return '；'.join(parts)


def _vision_items(ctx):
    try:
        import vision_notes
        path = vision_notes._file()
    except Exception as exc:
        return [], _degradation('vision', 'unreadable', type(exc).__name__)
    data, problem = _read_json(path)
    if problem:
        return [], _degradation('vision', problem)
    # 笔记资产本身不存来源 ID；依据是「哪次看图执行看到了它」，回执在认知库。
    try:
        import self_model
        receipts = self_model.tool_receipts('vision')
    except Exception:
        receipts = {}
    items = []
    for key, note in (data or {}).items():
        if not isinstance(note, dict):
            continue
        meaning = _vision_text(note)
        if not meaning:
            continue
        visibility = vision_notes._visibility(note)
        session = str(note.get('session') or '')
        refs = [str(x) for x in (receipts.get(str(note.get('key') or key)) or []) if x]
        items.append(_item(
            'vision:%s' % key, 'vision_note', 'vision', meaning,
            revision=_sha(meaning, note.get('ts')),
            source_session=session or ('private:vision' if visibility == 'private' else 'group:vision'),
            visibility=visibility if visibility in VISIBILITIES else 'private',
            scope_basis='store_owner',
            # 看图是工具查到的含义，不是她亲历的现实经历（§8.5）。
            epistemic_kind='looked_up', occurred_at=note.get('ts'),
            recorded_at=note.get('ts'), root_source_ids=refs,
            evidence_spans=[], legacy=not refs,
            validity='valid' if refs else 'unknown',
            match={'clues': ['vision', visibility],
                   'score_parts': {'tool_sourced': 1.0 if refs else 0.0}}))
    return items, None


_ADAPTERS = {'cognition': _cognition_items, 'persona': _persona_items,
             'group': _group_items, 'knowledge': _knowledge_items,
             'vision': _vision_items}


def fetch_retrieval_items(ctx, *, candidate_query=None):
    """§19.1 fetch_retrieval_items：各库的统一只读描述，不初始化任何库。"""
    items, degradation = [], []
    for store in STORES:
        try:
            adapter = _ADAPTERS[store]
            rows, problem = (adapter(ctx, candidate_query=candidate_query)
                             if store == 'cognition' and candidate_query is not None
                             else adapter(ctx))
        except Exception as exc:
            rows, problem = [], _degradation(store, 'unreadable', type(exc).__name__)
        items.extend(rows)
        if problem:
            degradation.append(problem)
    return {'items': items, 'degradation': degradation}


# ── 硬过滤：失效、不可见、来源无效、未来信息、过期当前状态（§8.3 第2步）──


def scope_allows(item, ctx):
    """来源范围与受众范围分开校验；派生只能相同或更窄（X14）。

    scope_basis='archive' 的旧条目没有可核对的会话来源，只有调用方把档案本身
    显式列进 allowed_source_scopes 才读得到，且永远不给群受众：显式回忆可以
    恢复访问（§7.5），但「读不到范围」不等于「可以放宽范围」。
    """
    allowed = [str(x) for x in (ctx.get('allowed_source_scopes') or []) if str(x or '').strip()]
    session = str(item.get('source_session') or '')
    visibility = str(item.get('visibility') or 'private')
    if visibility == 'public':
        return True
    if not allowed or not session:
        return False
    sessions = item.get('source_sessions') or [session]
    if not all(any(source == scope or source.startswith(scope.rstrip(':') + ':')
                   for scope in allowed) for source in sessions):
        return False
    recipient = str(ctx.get('recipient_scope') or '')
    # 私聊来源不能因为被摘成「公开风格」就转述给群受众。
    if visibility == 'private' and recipient.startswith('group:'):
        return False
    if item.get('scope_basis') == 'archive' and not recipient.startswith('private:'):
        return False
    if visibility == 'group' and recipient.startswith('private:'):
        return session == recipient or recipient in allowed
    return True


def hard_filter(items, ctx):
    """返回 (留下的条目, 被挡下的原因计数)。挡下什么必须能解释。"""
    as_of = float(ctx.get('as_of_time') or 0)
    constraints = ctx.get('time_constraints') or {}
    not_before = constraints.get('not_before')
    not_after = constraints.get('not_after')
    kept, dropped = [], {}

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    for item in items:
        if item.get('validity') == 'invalidated':
            drop('invalidated')
            continue
        if not scope_allows(item, ctx):
            drop('archive_not_granted' if item.get('scope_basis') == 'archive'
                 else 'out_of_scope')
            continue
        occurred = item.get('occurred_at')
        if isinstance(occurred, (int, float)) and not isinstance(occurred, bool):
            if occurred > as_of + _TIME_SKEW_SECONDS:
                drop('future_evidence')
                continue
            if isinstance(not_before, (int, float)) and occurred < not_before:
                drop('outside_time_constraints')
                continue
            if isinstance(not_after, (int, float)) and occurred > not_after:
                drop('outside_time_constraints')
                continue
        # 过期/归档的当前状态不再当「现在」，但历史仍可回顾（X16）。
        item = dict(item)
        item['current'] = bool(item.get('current', True))
        kept.append(item)
    return kept, dropped


def _missing_support(items, ctx):
    """缺口要可见，不能用空数组冒充全部已确认（§19.3）。"""
    gaps = []
    if not items:
        return ['no_evidence']
    if not any(item.get('evidence_spans') for item in items):
        gaps.append('original_quote')
    if not any(item.get('root_source_ids') for item in items):
        gaps.append('root_source')
    if any(item.get('legacy') for item in items) and not any(
            item.get('validity') == 'valid' for item in items):
        gaps.append('verified_provenance')
    if not any(item.get('occurred_at') for item in items):
        gaps.append('occurrence_time')
    if str(ctx.get('query_text') or '').strip() and not any(
            item.get('match', {}).get('clues') for item in items):
        gaps.append('match_clues')
    return gaps


def retrieve_evidence(ctx):
    """R01 的只读检索结果。不做联想扩散、不做融合打分、不判矛盾。

    status 只会是 sufficient/partial/not_found/degraded：conflicting 需要
    R05 的矛盾判定，这里不冒充。paths 恒为空，因为边投影是 R02/R04。
    """
    started = time.time()
    problems = context_problems(ctx)
    if problems:
        return {'status': 'degraded', 'items': [], 'paths': [],
                'missing_support': ['query_context'], 'conflicts': [],
                'degradation': [_degradation('query', 'invalid_context', ','.join(problems))],
                'provenance': {'query_hash': _sha(ctx.get('query_text')),
                               'index_revision': ctx.get('index_revision', ''),
                               'validity_epoch': ctx.get('validity_epoch', ''),
                               'seconds': round(time.time() - started, 4),
                               'candidates': 0, 'expanded': 0, 'filtered': {},
                               'limitations': ('QueryContext 不完整，未给任何绿灯',)}}
    fetched = fetch_retrieval_items(ctx)
    kept, dropped = hard_filter(fetched['items'], ctx)
    budget = _budget(ctx)
    selected = kept[:max(0, budget)]
    gaps = _missing_support(selected, ctx)
    degradation = list(fetched['degradation'])
    # 正常的范围/失效/时间过滤不是「能力降级」，把它记进 degradation 会让
    # status 永远 degraded。挡下了什么放在 provenance.filtered 里，一样能解释。
    if dropped.get('archive_not_granted'):
        degradation.append(_degradation('archive', 'not_granted',
                                        str(dropped['archive_not_granted'])))
    if len(kept) > len(selected):
        degradation.append(_degradation('budget', 'over_budget',
                                        '%d/%d' % (len(selected), len(kept))))
    if degradation:
        status = 'degraded'
    elif not selected:
        status = 'not_found'
    elif gaps:
        status = 'partial'
    else:
        status = 'sufficient'
    return {
        'status': status, 'items': selected, 'paths': [],
        'missing_support': gaps, 'conflicts': [], 'degradation': degradation,
        'provenance': {
            'query_hash': _sha(ctx.get('query_text'), ctx.get('recipient_scope'),
                               ','.join(sorted(str(x) for x in ctx.get('allowed_source_scopes') or []))),
            'index_revision': ctx.get('index_revision', ''),
            'validity_epoch': ctx.get('validity_epoch', ''),
            'seconds': round(time.time() - started, 4),
            'candidates': len(fetched['items']), 'expanded': 0, 'filtered': dropped,
            'limitations': ('R01 只做统一只读对象与硬过滤：无联想边(R02/R04)、'
                            '无多线索融合打分(R03)、无矛盾判定(R05)；'
                            '分数只是检索排序线索，不是命题真实性百分比。',),
        },
    }