# -*- coding: utf-8 -*-
"""M07 私聊原始轮次情景化（路线 §5 自传体记忆 / §5.4 反思不得制造经历 / §8.2 单一状态写入者）。

把真实发生过的私聊轮次（journal 里的 user/assistant 消息对）情景化为带
逐命题来源的 episode 候选，经 M06 单一写入事务落账。X04/X05 的病灶是
「整窗事件挂给每条结论」「无来源摘要冒充亲历」；这里的口径是：

  · 输入是调用方传入的原始轮次与冻结源窗口（self_model.source_window
    产出）。本模块不自己去翻生产库找「经历」：read_private_turns 只读
    journal 原始行，read_derivation_inputs 只用于幂等去重与降级报告。
  · 每条命题（对方说了什么 / 她回应了什么）用 claim_provenance 的同一套
    纯函数核对：quote 必须是真实源文本的连续逐字片段，offset 由代码定位。
    定位不到就不登记 quote/offset，绝不补造（M02）。
  · 来源分层如实登记：bound（事件级逐字可定位）/ legacy（只有 journal
    原文、没有事件引用——线上 journal 绝大多数行没有 events 字段）/
    unknown（有引用但核对不上）/ invalidated（源已撤回或失效）。
    没有任何 bound 命题的轮次不落 episode，不冒充亲历。
  · 她的回复是 action 不是 event（source_window 的既有口径）：引用里
    没有能逐字对上回复的事件时，assistant 命题按 legacy 登记，摘要里
    保留原文但绝不伪造事件级引文。
  · 原始记录不可改：journal 原行只读，情景化是派生层。撤回（A08 旧路径
    的 retracted 标记）与 M05 失效（invalidated/superseded）的源不得被
    复活为有效依据；episode 自身已被失效的，重复情景化只会被挡下
    （单调，绝不洗回 active）。
  · 落账只走 cognition_writer.commit（M06：准入门→事务）与
    self_model.write_invalidation_transaction（M05：只注册依赖边，
    不推进 epoch）。不新造第二套写入，不改线任何旧写入器/旧摘要路径。
  · LLM 文本只能当候选解释：interpretation 以 inferred claim 提交，
    M01 必然隔离（inferred_claim），绝不写成事实，也不进 episode 摘要。
  · 幂等：稳定 dedupe 键 = (session, ts, user, bot)，与解释文本无关；
    object_id → ledger ID 沿用 write_cognition_transaction 的派生口径，
    同一轮次重复情景化不产生重复 episode，依赖边重复注册幂等忽略。

纯函数核心：turn_identity / episodize 不读写账本、不发网络、不调模型。
只读桥 read_private_turns / freeze_window：路径调用时解析，缺文件报
absent/degraded，绝不建库建表建目录（X15）。

原子性口径：所有坏输入（结构非法的轮次、非法解释、成环依赖边）在任何
写入之前纯预检并整体拒绝；episode 落账与依赖边注册是两个既有事务入口，
各自原子回滚。边注册失败时 episode 已落账（其 event_ids 本身仍能被
M05 只读桥重建依赖），原样抛错出声，重跑同一轮次幂等修复。
"""
import hashlib
import json
import os
import time

import claim_provenance
import cognition_writer
import invalidation_dag
import self_model

ALGORITHM_VERSION = 'm07-1'
# ready=有逐字可定位的来源；degraded=只有 legacy/unknown 来源；
# refused=测试/合成/未来轮次；blocked=源或自身已失效；invalid=坏输入。
PLAN_STATUSES = ('ready', 'degraded', 'refused', 'blocked', 'invalid')
PROP_PROVENANCE = ('bound', 'legacy', 'unknown', 'invalidated', 'absent')
_PRIVATE_PREFIXES = ('private-user-', 'private:')
_TIME_SKEW_SECONDS = 1.0
# 摘要子句上限：ledger 的 summary 截断是 400，两条子句加模板必须留有余量，
# 截断必须出声（clause_truncated），不许静默切掉半句引文。
_CLAUSE_LIMIT = 150
_TRUTH_LOST = ('invalidated', 'superseded')
LIMITATIONS = (
    '不做巩固晋升：episode 落账即止，晋升为信念/自我认识是 M08 的范围',
    '不改写原始轮次：journal 原行只读，情景化只是派生层',
    '她的回复默认没有事件级来源（回复是 action 不是 event）：未提供回执事件时按 legacy 登记，不补造 quote/offset',
    'LLM 解释只作为 inferred 候选进隔离区，绝不写成事实',
    'episode 落账与依赖边注册是两个既有事务：边注册失败时原样抛错，重跑同一轮次幂等修复',
    '不改线旧写入器与旧摘要路径（persona_growth.consolidate / private_memory.json 保持原样）',
    '不做检索与注入接线（R01 仍在只观察阶段）',
)


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_private_session(session):
    return any(session.startswith(prefix) for prefix in _PRIVATE_PREFIXES)


def _participant(session):
    for prefix in _PRIVATE_PREFIXES:
        if session.startswith(prefix):
            return session[len(prefix):]
    return session


def _is_probe_session(session):
    """与 M02/persona_growth 同一套测试会话标记：探针轮次不是她的经历。"""
    part = _participant(session)
    return any(session.startswith(marker) or part.startswith(marker)
               for marker in claim_provenance.TEST_SESSION_MARKERS)


def turn_identity(turn):
    """原始轮次的稳定身份（纯函数）。返回 {ok, key, object_id, problems}。

    身份只由 (session, ts, user, bot) 决定：同一轮次换一份 LLM 解释、
    补上事件引用，都不改变它是「同一段经历」的事实。缺时间/缺会话/
    空轮次不给默认值——没有稳定身份就不许落账。
    """
    row = turn if isinstance(turn, dict) else {}
    session = str(row.get('session') or '').strip()
    user_text = str(row.get('user') or '').strip()
    bot_text = str(row.get('bot') or '').strip()
    occurred = row.get('ts')
    problems = []
    if not isinstance(turn, dict):
        problems.append('turn_not_a_dict')
    if not session:
        problems.append('missing_session')
    elif not _is_private_session(session):
        problems.append('not_private_session')
    if not _number(occurred) or occurred <= 0:
        problems.append('missing_turn_time')
    if not user_text and not bot_text:
        problems.append('empty_turn')
    if problems:
        return {'ok': False, 'key': '', 'object_id': '', 'problems': problems}
    key = _sha(session, float(occurred), user_text, bot_text)
    return {'ok': True, 'key': key, 'object_id': 'ptep_' + key, 'problems': []}


def _bind_proposition(role, text, refs, sources, lost, at):
    """把一条命题绑到真实原句（M02 口径，纯函数）。

    只认调用方冻结窗口里、且被本轮次引用的事件；逐字核对用
    claim_provenance.verify_quote / source_reasons，不长第二套口径。
    多个事件都含同一句原话时不猜是哪一条（ambiguous_source_event）。
    引文来自原始轮次文本本身、由代码定位，match_kind 登记为 lexical：
    机器只能证明逐字可定位，不冒充语义核验。

    绑不上时按角色分层：user 消息本来就是事件，引用核对不上是可疑
    失配（unknown）；她的回复是 action 不是 event，引用里找不到匹配
    源是常态，journal 原文按 legacy 登记，不当可疑失配、也不补造引文。
    """
    prop = {'role': role, 'text': text, 'provenance': 'absent', 'event_id': '',
            'quote': '', 'span_start': None, 'span_end': None,
            'source_revision': '', 'match_kind': '', 'reasons': []}
    if not text:
        prop['reasons'].append('no_text')
        return prop
    if not refs:
        # journal 原文真实存在，但没有事件级来源：legacy，不补造 quote/offset。
        prop['provenance'] = 'legacy'
        prop['reasons'].append('no_event_reference')
        return prop
    matched, problems = [], []
    for eid in refs:
        row = sources.get(eid)
        if row is None:
            problems.append('event_outside_window')
            continue
        if eid in lost:
            problems.append('source_invalidated')
            continue
        codes = claim_provenance.source_reasons(row, at)
        if codes:
            problems.extend(codes)
            continue
        checked = claim_provenance.verify_quote(row.get('text'), text)
        if not checked['ok']:
            problems.append(checked['reason'])
            continue
        matched.append((eid, row, checked))
    if len(matched) == 1:
        eid, row, checked = matched[0]
        prop.update(provenance='bound', event_id=eid, quote=checked['quote'],
                    span_start=checked['span_start'], span_end=checked['span_end'],
                    source_revision=str(row.get('source_revision') or ''),
                    match_kind='lexical')
        if checked['reason']:
            prop['reasons'].append(checked['reason'])
        return prop
    if len(matched) > 1:
        prop['provenance'] = 'legacy' if role == 'assistant' else 'unknown'
        prop['reasons'].append('ambiguous_source_event')
        return prop
    invalidated = any(code in ('source_retracted', 'source_invalidated')
                      for code in problems)
    if invalidated:
        prop['provenance'] = 'invalidated'
    elif role == 'assistant':
        prop['provenance'] = 'legacy'
    else:
        prop['provenance'] = 'unknown'
    prop['reasons'].extend(dict.fromkeys(problems))
    return prop


def _summary(user_prop, bot_prop):
    """摘要只由 bound/legacy 子句确定性拼出（不调模型）。

    unknown 子句（引用了事件却核对不上）不进摘要：存疑文本不冒充引文。
    超长子句截断必须留痕，span 里的 quote 仍是全长原文。
    """
    reasons, parts = [], []
    for prop, template in ((user_prop, '私聊里对方说：「%s」'),
                           (bot_prop, '她回应：「%s」')):
        if prop['provenance'] not in ('bound', 'legacy'):
            if prop['provenance'] == 'unknown':
                reasons.append('clause_omitted_unverified')
            continue
        text = prop['text']
        if len(text) > _CLAUSE_LIMIT:
            text = text[:_CLAUSE_LIMIT] + '…'
            reasons.append('clause_truncated')
        parts.append(template % text)
    return '；'.join(parts), reasons


def episodize(turn, window, now=None, states=None, interpretation=None):
    """纯函数：把一个原始轮次情景化为 episode 候选计划，不写任何东西。

    turn:   journal 形态的原始轮次 {ts, session, user, bot, events[]}。
    window: 调用方冻结的源记录列表（self_model.source_window 产出）。
    states: M05 对象状态（invalidation_dag.read_invalidation_state 产出），
            已失效的源/已失效的 episode 一律挡下，绝不复活。
    interpretation: 可选 LLM 解释文本；只登记为候选，绝不进摘要或事实。

    返回 {status, object_id, dedupe_key, episode_id, session, candidate,
          propositions[], edges[], reasons[], interpretation, limitations}。
    candidate 是 cognition_writer.commit 的信封；status 非 ready 时为 None。
    """
    at = time.time() if now is None else float(now)
    row = turn if isinstance(turn, dict) else {}
    session = str(row.get('session') or '').strip()
    user_text = str(row.get('user') or '').strip()
    bot_text = str(row.get('bot') or '').strip()
    identity = turn_identity(turn)
    plan = {'status': 'invalid', 'object_id': identity['object_id'],
            'dedupe_key': identity['key'], 'episode_id': '', 'session': session,
            'candidate': None, 'propositions': [], 'edges': [],
            'reasons': list(identity['problems']), 'interpretation': None,
            'limitations': list(LIMITATIONS)}
    if interpretation is not None and not isinstance(interpretation, str):
        plan['reasons'].append('interpretation_not_text')
    if not identity['ok'] or plan['reasons']:
        return plan
    if _is_probe_session(session):
        plan['status'] = 'refused'
        plan['reasons'].append('test_session')
        return plan
    if row.get('synthetic') or row.get('evidence_origin') == 'verification':
        plan['status'] = 'refused'
        plan['reasons'].append('synthetic_turn')
        return plan
    occurred = float(row['ts'])
    if occurred > at + _TIME_SKEW_SECONDS:
        plan['status'] = 'refused'
        plan['reasons'].append('future_turn')
        return plan

    episode_id = 'epi_' + self_model._sha(identity['object_id'])
    plan['episode_id'] = episode_id
    prior = states if isinstance(states, dict) else {}
    lost = {oid for oid, st in prior.items()
            if isinstance(st, dict) and st.get('status') in _TRUTH_LOST}
    if episode_id in lost:
        # 失效是单调的：重复情景化不得把已失效的 episode 洗回有效。
        plan['status'] = 'blocked'
        plan['reasons'].append('prior_invalidation_preserved')
        return plan

    sources = {}
    for src in (window or []):
        if isinstance(src, dict) and str(src.get('event_id') or '').strip():
            sources[str(src['event_id'])] = src
    refs = list(dict.fromkeys(str(x).strip() for x in (row.get('events') or [])
                              if str(x or '').strip()))
    user_prop = _bind_proposition('user', user_text, refs, sources, lost, at)
    bot_prop = _bind_proposition('assistant', bot_text, refs, sources, lost, at)
    plan['propositions'] = [user_prop, bot_prop]

    text = str(interpretation or '').strip()
    if text:
        plan['interpretation'] = {'text': text, 'status': 'candidate_only',
                                  'object_id': 'pti_' + identity['key'],
                                  'reasons': ['llm_interpretation_not_fact']}

    bound = [p for p in (user_prop, bot_prop) if p['provenance'] == 'bound']
    if not bound:
        plan['status'] = ('blocked'
                          if any(p['provenance'] == 'invalidated'
                                 for p in (user_prop, bot_prop))
                          else 'degraded')
        plan['reasons'].append('no_bound_source_event')
        return plan

    summary, summary_reasons = _summary(user_prop, bot_prop)
    event_ids = list(dict.fromkeys(p['event_id'] for p in bound))
    plan['candidate'] = {
        'object_kind': 'episode', 'object_id': identity['object_id'],
        'content': summary, 'occurred_at': occurred, 'recorded_at': at,
        'epistemic_type': 'experienced', 'visibility': 'private',
        'source_session': session, 'source_event_ids': event_ids,
        'perspective': 'self',
    }
    basis_by_event = {}
    for prop in bound:
        basis_by_event.setdefault(prop['event_id'], set()).add(prop['role'])
    plan['edges'] = [
        {'derived_id': episode_id, 'source_id': eid,
         'derived_kind': 'episode', 'source_kind': 'event',
         'basis': ('private_turn.both' if len(roles) == 2
                   else 'private_turn.' + sorted(roles)[0])}
        for eid, roles in sorted(basis_by_event.items())]
    plan['status'] = 'ready'
    plan['reasons'].extend(summary_reasons)
    return plan


# ── 只读桥：缺文件报 absent/degraded，绝不建库建表建目录（X15）──────


def _data_root():
    return os.getenv('DATA_DIR', './data')


def _journal_path():
    # 与 persona_growth.journal_file 同一路径口径，但只读、不 _ensure()。
    return os.path.join(_data_root(), 'persona', 'journal.jsonl')


def read_private_turns(since_ts=0.0, limit=0):
    """只读 journal 里的私聊原始轮次，原行原样返回（不改写、不补字段）。

    群聊/探针会话不进情景化；坏行跳过并计数。缺文件报 absent，
    读不动报 unreadable——不声称「她没有说过话」。limit<=0 表示不限。
    """
    out = {'turns': [], 'degradation': [], 'scanned': 0, 'skipped': 0}
    path = _journal_path()
    if not os.path.isfile(path):
        out['degradation'].append({'store': 'journal', 'reason': 'absent'})
        return out
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                out['scanned'] += 1
                try:
                    row = json.loads(line)
                except Exception:
                    out['skipped'] += 1
                    continue
                if not isinstance(row, dict):
                    out['skipped'] += 1
                    continue
                session = str(row.get('session') or '')
                if not _is_private_session(session):
                    continue
                if _is_probe_session(session):
                    out['skipped'] += 1
                    continue
                try:
                    ts = float(row.get('ts') or 0)
                except (TypeError, ValueError):
                    out['skipped'] += 1
                    continue
                if ts <= float(since_ts or 0):
                    continue
                out['turns'].append(row)
                if limit and len(out['turns']) >= int(limit):
                    break
    except (OSError, UnicodeDecodeError):
        out['degradation'].append({'store': 'journal', 'reason': 'unreadable'})
    return out


def freeze_window(turns):
    """只读冻结这批轮次引用的真实事件（走 self_model.source_window）。

    缺库/缺事件返回空窗口：命题自然绑不上，按 legacy/unknown 降级，
    绝不为了「有依据」去补造事件。
    """
    refs = []
    for row in (turns or []):
        if not isinstance(row, dict):
            continue
        for eid in (row.get('events') or []):
            key = str(eid or '').strip()
            if key and key not in refs:
                refs.append(key)
    return self_model.source_window(refs)


# ── 落账：只走 M06/M05 的既有事务入口，不造第二套写入 ───────────────


def _interpretation_candidate(plan, at):
    """LLM 解释 → inferred claim 信封。M01 对 inferred 必然隔离，
    这里不替它争取 accept：候选解释永远不是事实。"""
    bound = [p['event_id'] for p in plan['propositions']
             if p['provenance'] == 'bound']
    return {'object_kind': 'claim',
            'object_id': plan['interpretation']['object_id'],
            'content': plan['interpretation']['text'],
            'occurred_at': float(plan['candidate']['occurred_at'])
            if plan['candidate'] else None,
            'recorded_at': at, 'epistemic_type': 'inferred',
            'visibility': 'private', 'source_session': plan['session'],
            'source_event_ids': list(dict.fromkeys(bound)), 'evidence': []}


def commit_turns(turns, window, now=None, interpretations=None):
    """把一批原始轮次情景化并经 M06 单一写入事务落账。

    turns:          原始轮次列表（journal 形态）。
    window:         调用方冻结的源窗口（freeze_window / source_window）。
    interpretations: 可选 {object_id: LLM 解释文本}，只进隔离候选。

    坏输入整体拒绝：任何结构非法的轮次/解释/依赖边都在第一次写入之前
    抛 ValueError，账本一个字节都不动。每个 episode 各自走
    cognition_writer.commit 的原子事务；依赖边在一个
    write_invalidation_transaction 里注册（只加边，不推进 epoch）。

    返回只读投影 {status, committed, duplicates, degraded, refused,
    blocked, deferred, rejected, episodes[], edges_added, epoch,
    provenance, limitations}，绝不返回账本句柄。
    """
    at = time.time() if now is None else float(now)
    if interpretations is not None and not isinstance(interpretations, dict):
        raise ValueError('interpretations must be a dict keyed by turn object_id')
    inter = interpretations or {}
    rows = list(turns or [])
    frozen = list(window or [])

    state = invalidation_dag.read_invalidation_state()
    inputs = invalidation_dag.read_derivation_inputs()
    existing_episodes = {o['object_id'] for o in inputs['objects']
                         if o.get('kind') == 'episode'}

    # ── 纯预检：全部计划与依赖边先算完，坏输入在任何写入之前整体拒绝。 ──
    plans = [episodize(row, frozen, now=at, states=state['states'],
                       interpretation=inter.get(turn_identity(row)['object_id']))
             for row in rows]
    invalid = [p for p in plans if p['status'] == 'invalid']
    if invalid:
        raise ValueError('private turn batch rejected: %s'
                         % sorted({r for p in invalid for r in p['reasons']}))
    combined = [dict(e) for e in state['edges']]
    seen_pairs = set()
    for plan in plans:
        for edge in plan['edges']:
            pair = (edge['derived_id'], edge['source_id'])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            checked = invalidation_dag.check_edge(
                combined, edge['derived_id'], edge['source_id'],
                edge['derived_kind'], edge['source_kind'])
            if not checked['ok']:
                raise ValueError('derivation edge rejected before write: %s (%s -> %s)'
                                 % (checked['reason'], edge['derived_id'], edge['source_id']))
            combined.append(edge)

    # ── 写入：episode 各自原子落账；门只能收紧，降级如实登记。 ──
    episodes, write_edges = [], []
    for plan in plans:
        entry = {'object_id': plan['object_id'], 'episode_id': plan['episode_id'],
                 'status': plan['status'], 'decision': None,
                 'reasons': list(plan['reasons']),
                 'summary': (plan['candidate'] or {}).get('content', ''),
                 'propositions': plan['propositions'],
                 'interpretation': plan['interpretation']}
        if plan['status'] == 'ready':
            if plan['episode_id'] in existing_episodes:
                # 幂等：同一轮次不重复落账，但依赖边仍补注册（修复中断）。
                entry['status'] = 'duplicate'
            else:
                projection = cognition_writer.commit(plan['candidate'], frozen, now=at)
                entry['decision'] = projection['decision']
                entry['reasons'].extend(projection['reasons'])
                entry['episode_id'] = projection['object_ids'].get('episode',
                                                                   plan['episode_id'])
                entry['status'] = {'accept': 'committed',
                                   'quarantine': 'deferred',
                                   'reject': 'rejected'}[projection['decision']]
        if entry['status'] in ('committed', 'duplicate'):
            write_edges.extend(plan['edges'])
        if plan['interpretation'] and entry['status'] in ('committed', 'duplicate',
                                                          'degraded'):
            projection = cognition_writer.commit(
                _interpretation_candidate(plan, at), frozen, now=at)
            entry['interpretation'] = dict(plan['interpretation'])
            entry['interpretation']['decision'] = projection['decision']
            entry['interpretation']['candidate_id'] = projection['object_ids'].get('candidate', '')
            entry['reasons'].extend(projection['reasons'])
        episodes.append(entry)

    edges_added = 0
    if write_edges:
        unique = list({(e['derived_id'], e['source_id']): e for e in write_edges}.values())
        result = self_model.write_invalidation_transaction({
            'ts': at, 'expected_epoch': state['epoch'],
            'edges': unique, 'corrections': [], 'propagation': None})
        edges_added = result['edges_added']

    counter_names = {'committed': 'committed', 'duplicate': 'duplicates',
                     'degraded': 'degraded', 'refused': 'refused',
                     'blocked': 'blocked', 'deferred': 'deferred',
                     'rejected': 'rejected'}
    counts = {name: sum(1 for e in episodes if e['status'] == status)
              for status, name in counter_names.items()}
    return {'status': 'ok', **counts, 'episodes': episodes,
            'edges_added': edges_added, 'epoch': state['epoch'],
            'provenance': {'algorithm_version': ALGORITHM_VERSION,
                           'window_size': len(frozen),
                           'window_source': 'caller_frozen',
                           'invalidation_epoch': state['epoch'],
                           'degradation': inputs['degradation']},
            'limitations': list(LIMITATIONS)}