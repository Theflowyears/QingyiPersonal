# -*- coding: utf-8 -*-
"""L01 仅从真实新增来源反思（路线 §5.3 自我认识只能修订 / §5.4 反思不得制造经历 /
§8.2 单一状态写入者；stage5/self，dev 前置 M08/S06）。

M08 把「有限巩固与晋升」做成可执行契约就停手（memory_consolidation LIMITATIONS：
「不做 L02 长期自我认识修订」）；S06 把「迟到/重启/同刻/撤回」回放连续性证完就停手
（emotion_replay_matrix：撤回单调、解释纠正洗不回来）。L01 接住这两处，把
「反思只能从真实新增来源产生」落成一条**可执行契约**（不接进真实链路）：

  1. 无新增真实证据 → 不得新增任何「发生过」：反思输出为空，plan 如实登记
     no_new_real_source，绝不现场编经历。
  2. 同一来源的重复转述不算多份独立证据：根源按 R04/R05 同口径归并
     （ripple_paths._roots 取根源 + ripple_conflicts._root_canon 共现归并），
     独立支持数按归并后的根源组计，转述不放大证据。
  3. 反思产物不得反过来充当证明自己的事实：解释层与原始事件分层。反思输出
     只落解释/信念候选层（OUTPUT_KINDS=claim/belief），绝不落事件层（episode）；
     上一轮反思的输出 ID 进入解释层登记，本轮任何候选引用它一律
     interpretation_not_evidence 拒绝（自证闭环）。
  4. 小说/角色扮演/塔罗等想象层不得混入现实人物事实：证据分层词表复用
     cognition_admission.EPISTEMIC_TYPES / FICTION_CONTEXTS 与 M08 的
     PROMOTABLE_TIERS/NON_PROMOTABLE_TIERS，inferred/imagined 与 fiction 上下文
     永不晋升为已核验信念，只留解释层。
  5. 撤回/失效来源单调不复活（接 M05/S06）：retracted/invalidated/superseded
     的源不参与反思，候选引用任一失效源即 source_invalidated 拒绝；已失效对象
     与 needs_reassessment 对象不盲目晋升。
  6. 晋升/落账只走 cognition_writer.commit 单一写入者：apply_reflection 复用
     M08.apply_consolidation（它本身被 AST 锁钉死只经 cognition_writer.commit
     与 self_model.write_invalidation_transaction），本模块不写一行事实 SQL、
     不开写连接、不调旧写入器，绝不写真实 data/（隔离提交由调用方备 DATA_DIR）。

硬不变量（全部复用冻结前置，不另长第二套）：
  · 确定性：无墙钟、无随机。now 必须显式传入（本模块不 import time/random），
    决策路径与 reflection_id 不含墙上时钟；内容寻址 ID，同输入同输出、输入顺序
    无关。
  · 只追加/版本化：内存账本只追加（new_ledger/commit_reflection，S03/M08 先例），
    旧记录一字节不动；重复 ID 与内容哈希不符一律拒绝；读函数纯读返回深拷贝。
  · 只读桥：read_reflection_inputs/freeze_window 完全委托 M08 的冻结只读桥
    （mode=ro + query_only，DATA_DIR 调用时解析，缺库报 absent/degraded，绝不
    建库建表建目录）。本模块自身不 import os/sqlite3、不解析路径、不开任何库。

边界（LIMITATIONS，如实声明没做的事）：不做 L02 信念/自我认识的版本修订、不做
L03 习惯与关系长期更新、不做真实 LLM 反思（候选由调用方传入，本模块只裁决来源
纪律，不调模型、不生成话术）、不接进真实链路（仅可执行契约，共享接线由主 agent
做）、不做 C04 表达注入。晋升的有限预算与依赖边注册复用 M08 事务（basis
m08.consolidation），本模块不另长预算/写入引擎。
"""
import copy
import hashlib
import json

import claim_conflicts as cc
import cognition_admission as ca
import memory_consolidation as mc
import ripple_conflicts as rc
import ripple_paths as rp

ALGORITHM_VERSION = 'l01-1'
# 反思输出只落解释/信念候选层，绝不落事件层（§5.4 原始事件与解释分层）。
OUTPUT_KINDS = ('claim', 'belief')
# 证据分层与想象层词表全部复用冻结前置，不自带第二套（M01/M08 同口径）。
EPISTEMIC_TYPES = ca.EPISTEMIC_TYPES
PROMOTABLE_TIERS = mc.PROMOTABLE_TIERS
NON_PROMOTABLE_TIERS = mc.NON_PROMOTABLE_TIERS
FICTION_CONTEXTS = ca.FICTION_CONTEXTS
# 失效单调状态（复用 M08 冻结常量，不另立词表）。
_TRUTH_LOST = mc._TRUTH_LOST
# L01 特有的稳定 reason_code（其余 tier/verifiability/conflict 码复用 M08）。
REASON_CODES = (
    'missing_object_id',
    'reflection_not_event',
    'interpretation_not_evidence',
    'source_invalidated',
    'no_new_real_source',
    'fiction_not_fact',
)
# plan 级结论：有真实新增来源 / 无新增真实证据（反思输出为空）。
PLAN_REASONS = ('reflected_from_new_sources', 'no_new_real_source')

LIMITATIONS = (
    '不做 L02 信念与自我认识的版本修订：反思只产出解释/信念候选，旧版本 superseded '
    '的显式修订事务是 L02/M08 的范围。',
    '不做 L03 习惯与关系长期更新，不做 C04 表达注入，不接进真实链路（仅可执行契约，'
    '共享接线由主 agent 做）。',
    '不做真实 LLM 反思：候选由调用方传入，本模块只裁决来源纪律，不调模型、不生成话术、'
    '不现场编成长故事。',
    '无新增真实证据时反思输出为空并如实登记 no_new_real_source，绝不新增「发生过」。',
    '同一来源的重复转述不算多份独立证据：根源按 R04/R05 同口径归并后计独立支持。',
    '反思产物不得自证：输出只落解释/信念候选层（claim/belief），绝不落事件层；'
    '引用上一轮反思输出一律 interpretation_not_evidence 拒绝。',
    '推断/想象与小说/角色扮演/塔罗上下文永不晋升为现实人物事实（复用 M01/M08 分层）。',
    '撤回/失效来源单调不复活（接 M05/S06）：retracted/invalidated/superseded 不参与反思。',
    '落账只走 cognition_writer.commit 单一写入者（经 M08.apply_consolidation 复用），'
    '不写事实 SQL、不调旧写入器、绝不写真实 data/。',
    '确定性：无墙钟无随机，now 显式传入，reflection_id 内容寻址；只读桥委托 M08，'
    '缺库报 absent/degraded，绝不建库建表建目录。',
)


# ── 小工具（与 M08/R05 同一口径，非引擎）────────────────────────────


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
    """now 必须显式传入：本模块不读墙上时钟（无 time import），决策路径可复现。"""
    if not _number(now):
        raise ValueError('reflection requires an explicit numeric now; no wall clock')
    return float(now)


def _oid(candidate):
    return _text(candidate.get('object_id') or candidate.get('candidate_id')
                 or candidate.get('id'))


def _kind(candidate):
    return _text(candidate.get('object_kind'))


def _tier(candidate):
    return _text(candidate.get('epistemic_type') or candidate.get('epistemic_kind'))


def _sources(candidate):
    return [str(x).strip() for x in (candidate.get('source_event_ids') or [])
            if str(x or '').strip()]


def _cited(candidate):
    """候选引用的全部源 ID：source_event_ids + 逐命题 evidence 的 event_id。"""
    out = set(_sources(candidate))
    for item in (candidate.get('evidence') or []):
        if isinstance(item, dict) and _text(item.get('event_id')):
            out.add(_text(item['event_id']))
    return out


def _trim(candidate, grounding):
    """把候选裁剪到本轮新增真实来源：反思只从新增来源产生，旧源/转述不带入核验。

    核验（M08.classify_fate）与落账（M08.apply_consolidation）都用裁剪后的信封，
    保证「反思只能落在新增真实源上」在写入路径同样成立，不只停在计划层。
    """
    keep = set(grounding)
    out = dict(candidate)
    out['source_event_ids'] = list(grounding)
    out['evidence'] = [e for e in (candidate.get('evidence') or [])
                       if isinstance(e, dict) and _text(e.get('event_id')) in keep]
    return out


def _window_index(window):
    out = {}
    for row in (window or []):
        if isinstance(row, dict) and _text(row.get('event_id')):
            out[_text(row['event_id'])] = row
    return out


def _plan_hash(version, candidates, decisions, new_sources, reason_code):
    """reflection_id 内容寻址：候选/裁决/新增来源都排序后哈希，不含墙上时钟。"""
    rows = sorted((c for c in candidates if isinstance(c, dict)), key=_oid)
    summary = sorted((d['object_id'], d['layer'], d['fate'], d['reason_code'],
                      d['promotable']) for d in decisions)
    return 'l01_' + _sha(version, _canonical(rows), _canonical(summary),
                         _canonical(sorted(new_sources)), reason_code)


# ── 内存账本：只追加，读函数纯读（S03/M08 先例）────────────────────


def new_ledger():
    """空白反思账本。调用方持有；本模块不落盘、不解析路径。"""
    return {'records': {}, 'order': []}


def _check_ledger(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('records'), dict)
            or not isinstance(ledger.get('order'), list)):
        raise ValueError('ledger must come from new_ledger()')


def _watermark(ledger):
    """从已提交反思派生「已反思来源」与「解释层输出 ID」两个集合。

    已反思来源 → 本轮不算新增（无新证据不重复反思）；解释层输出 ID → 本轮
    任何候选引用它即自证，拒绝。两者都来自账本事实，不靠调用方自觉。
    """
    reflected, interpretations = set(), set()
    if ledger is None:
        return reflected, interpretations
    _check_ledger(ledger)
    for rid in ledger['order']:
        record = ledger['records'][rid]
        reflected.update(record.get('consumed_sources') or [])
        interpretations.update(record.get('output_ids') or [])
    return reflected, interpretations


# ── 根源归并：复用 R04 取根源 + R05 共现归并，不另长第二套 ──────────


def _window_canon(window):
    """把窗口里每个源的根本来源 ID 交给 R05 的共现归并（R04 同口径）。

    每个源一行：root_source_ids 缺省时以自身 event_id 为根源。转述/再提取
    携带相同根源 → 归并到同一组，不算独立证据。归并映射可核对。
    """
    rows = []
    for row in (window or []):
        if not isinstance(row, dict):
            continue
        eid = _text(row.get('event_id'))
        if not eid:
            continue
        rows.append({'object_id': eid, 'root_source_ids': rp._roots(row) or [eid]})
    return rc._root_canon(rows)


def _source_root_groups(window, canon):
    """源 ID → 归并后的根源组（排序去重）。独立支持数按组计，不按转述条数。"""
    out = {}
    for row in (window or []):
        if not isinstance(row, dict):
            continue
        eid = _text(row.get('event_id'))
        if not eid:
            continue
        roots = rp._roots(row) or [eid]
        out[eid] = sorted({canon.get(r, r) for r in roots})
    return out


# ── 纯预检：L01 来源纪律门 + 复用 M08 晋升裁决 ─────────────────────


def _decide(candidate, ctx):
    """纯函数：判定一条反思候选。返回解释层裁决（不含落账）。

    门序（稳定 reason_code）：身份 → 输出层（绝不事件）→ 自证 → 失效单调 →
    新增真实来源 → 想象层 → 复用 M08.classify_fate 做证据分层/原句/矛盾裁决。
    layer ∈ {interpretation, rejected}；fate 是 M08 兼容晋升归宿
    （belief/temporary），供 apply_reflection 经 M08 事务落账。
    """
    cand = candidate if isinstance(candidate, dict) else {}
    oid, kind, tier = _oid(cand), _kind(cand), _tier(cand)
    base = {'object_id': oid, 'kind': kind, 'tier': tier,
            'layer': 'rejected', 'fate': 'temporary', 'promotable': False,
            'reason_code': '', 'reasons': [], 'output_kind': '',
            'grounding_sources': [], 'root_groups': [], 'independent_sources': 0,
            'spans': [], 'ledger_id': ''}

    def out(layer, code, reasons=(), **extra):
        got = dict(base, layer=layer, reason_code=code, reasons=list(reasons))
        got.update(extra)
        return got

    if not oid:
        return out('rejected', 'missing_object_id')
    # 输出层：反思只落解释/信念候选，绝不制造经历（episode）或临时状态/承诺。
    if kind not in OUTPUT_KINDS:
        return out('rejected', 'reflection_not_event')
    cited = _cited(cand)
    # 自证闭环：引用上一轮反思输出（解释层）当作事实依据，拒绝。
    if cited & ctx['interpretation_ids']:
        return out('rejected', 'interpretation_not_evidence')
    # 失效单调：任一引用源已撤回/失效，绝不参与反思（M05/S06 同口径）。
    if any(s in ctx['lost'] or (ctx['window_by_id'].get(s) or {}).get('retracted')
           for s in cited):
        return out('rejected', 'source_invalidated')
    # 新增真实来源：反思只能落在本轮新增、有效、非解释层的真实源上。
    grounding = sorted(s for s in cited if s in ctx['new_sources'])
    if not grounding:
        return out('rejected', 'no_new_real_source')
    groups = sorted({g for s in grounding for g in ctx['root_groups'].get(s, [s])})
    accounting = {'grounding_sources': grounding, 'root_groups': groups,
                  'independent_sources': len(groups)}
    # 想象层：小说/角色扮演/塔罗上下文永不混入现实人物事实，只留解释层。
    if _text(cand.get('context_kind')) in FICTION_CONTEXTS:
        return out('interpretation', 'fiction_not_fact', output_kind=kind, **accounting)
    # 证据分层/逐命题原句/矛盾裁决全部复用 M08（不另长第二套晋升引擎）。
    # 核验只针对裁剪到新增来源的信封：旧源/转述不参与反思核验。
    fate = mc.classify_fate(_trim(cand, grounding), ctx['window'], ctx['states'],
                            ctx['conflicts'], now=ctx['now'])
    reasons = list(fate['reasons'])
    if fate['fate'] == 'belief' and fate['promotable']:
        return out('interpretation', 'promoted_belief', reasons, promotable=True,
                   fate='belief', output_kind='belief', spans=list(fate['spans']),
                   ledger_id=mc._ledger_id(oid, 'belief'), **accounting)
    code = fate['reason_code']
    # 不可核验/失效/挂起：反思必须可核验地落在真实源上，否则不产出解释。
    if code in ('evidence_not_verifiable', 'prior_invalidation_preserved',
                'needs_reassessment_pending', 'source_invalidated', 'no_source_events'):
        return out('rejected', code, reasons, **accounting)
    # 推断/想象/矛盾等：留解释层（promotable=False），绝不晋升为已核验事实。
    return out('interpretation', code, reasons, output_kind=kind, **accounting)


def reflection_plan(candidates, window=None, *, ledger=None, states=None,
                    conflicts=None, now):
    """纯函数：仅从真实新增来源产出反思计划，不写任何东西。

    candidates: 反思候选（M01 信封：object_kind∈claim/belief、epistemic_type、
                evidence[] 逐命题原句、source_event_ids、subject_ref、可选
                context_kind）。候选由调用方（未来接 LLM）传入，本模块不生成。
    window:     调用方冻结的源记录（self_model.source_window 产出）。
    ledger:     既往反思账本（new_ledger）；提供已反思来源与解释层输出登记。
    states:     M05 对象状态（invalidation_dag.read_invalidation_state）。
    conflicts:  M04 裁决记录；缺省时用 claim_conflicts.adjudicate 现算批内矛盾。
    now:        显式时间（必填；决策路径与 reflection_id 不含墙上时钟）。

    返回 {reflection_id, algorithm_version, status, reason_code, new_sources,
          excluded_sources, decisions, reflections, promotions, rejected,
          counts, independent_support, candidates, provenance, limitations}。
    无新增真实证据时 reflections/promotions 为空、reason_code=no_new_real_source。
    """
    at = _require_now(now)
    rows = sorted((c for c in (candidates or []) if isinstance(c, dict)), key=_oid)
    # 同 object_id 去重（排序后首见优先），重复转述不产生二义。
    unique = {}
    for cand in rows:
        unique.setdefault(_oid(cand), cand)
    rows = [unique[oid] for oid in sorted(unique)]
    frozen = list(window or [])
    prior = states if isinstance(states, dict) else {}
    window_by_id = _window_index(frozen)
    lost = {oid for oid, st in prior.items()
            if isinstance(st, dict) and st.get('status') in _TRUTH_LOST}
    reflected, interpretation_ids = _watermark(ledger)

    new_sources, excluded = [], []
    for eid in sorted(window_by_id):
        row = window_by_id[eid]
        if row.get('retracted'):
            excluded.append({'event_id': eid, 'reason_code': 'source_retracted'})
        elif eid in lost:
            excluded.append({'event_id': eid, 'reason_code': 'source_invalidated'})
        elif row.get('synthetic'):
            excluded.append({'event_id': eid, 'reason_code': 'synthetic_source'})
        elif eid in interpretation_ids:
            excluded.append({'event_id': eid, 'reason_code': 'interpretation_not_evidence'})
        elif eid in reflected:
            excluded.append({'event_id': eid, 'reason_code': 'already_reflected'})
        else:
            new_sources.append(eid)

    if conflicts is None:
        conflicts = cc.adjudicate(rows)
    canon = _window_canon(frozen)
    ctx = {'window': frozen, 'window_by_id': window_by_id, 'states': prior,
           'conflicts': conflicts, 'now': at, 'lost': lost,
           'new_sources': set(new_sources), 'interpretation_ids': interpretation_ids,
           'root_groups': _source_root_groups(frozen, canon)}
    decisions = [_decide(cand, ctx) for cand in rows]
    # 落账信封裁剪到新增来源（解释层）；rejected 保留原样（不会被提交）。
    candidates_out = [_trim(cand, d['grounding_sources']) if d['layer'] == 'interpretation'
                      else cand for cand, d in zip(rows, decisions)]

    reflections = [d for d in decisions if d['layer'] == 'interpretation']
    promotions = [d for d in reflections if d['promotable']]
    rejected = [d for d in decisions if d['layer'] == 'rejected']
    reason_code = 'reflected_from_new_sources' if new_sources else 'no_new_real_source'
    counts = {'interpretation': len(reflections), 'promotable': len(promotions),
              'rejected': len(rejected)}
    independent_support = {d['object_id']: d['independent_sources'] for d in reflections}
    reflection_id = _plan_hash(ALGORITHM_VERSION, candidates_out, decisions,
                               new_sources, reason_code)
    return {
        'reflection_id': reflection_id, 'algorithm_version': ALGORITHM_VERSION,
        'status': 'planned', 'reason_code': reason_code,
        'new_sources': new_sources, 'excluded_sources': excluded,
        'decisions': decisions, 'reflections': reflections, 'promotions': promotions,
        'rejected': rejected, 'counts': counts,
        'independent_support': independent_support,
        'candidates': copy.deepcopy(candidates_out),
        'provenance': {'method': 'reflection_from_new_real_sources_only',
                       'algorithm_version': ALGORITHM_VERSION,
                       'llm_reflection': False, 'window_size': len(frozen),
                       'new_source_count': len(new_sources),
                       'conflict_records': len(conflicts or []),
                       'conflicts': copy.deepcopy(conflicts or []),
                       'root_canon': canon},
        'limitations': list(LIMITATIONS),
    }


def commit_reflection(ledger, plan):
    """把反思计划追加进内存账本。重复 ID / 内容哈希不符一律拒绝（S03/M08 口径）。

    登记 consumed_sources（本轮真正用到的新增源）与 output_ids（解释层输出，
    含候选 object_id 与派生账本 ID），供下一轮反思判「新增」与「自证」。
    """
    _check_ledger(ledger)
    if not isinstance(plan, dict) or plan.get('status') not in ('planned', 'committed'):
        raise ValueError('reflection plan must come from reflection_plan')
    rid = _text(plan.get('reflection_id'))
    if not rid:
        raise ValueError('reflection plan requires reflection_id')
    if rid in ledger['records']:
        raise ValueError('reflection already exists; history is append-only: %s' % rid)
    expected = _plan_hash(plan.get('algorithm_version'), plan.get('candidates') or [],
                          plan.get('decisions') or [], plan.get('new_sources') or [],
                          plan.get('reason_code'))
    if rid != expected:
        raise ValueError('reflection_id does not match its content')
    record = copy.deepcopy(plan)
    record['status'] = 'committed'
    consumed, outputs = set(), set()
    for decision in record['reflections']:
        consumed.update(decision['grounding_sources'])
        outputs.add(decision['object_id'])
        if decision.get('ledger_id'):
            outputs.add(decision['ledger_id'])
    record['consumed_sources'] = sorted(consumed)
    record['output_ids'] = sorted(outputs)
    ledger['records'][rid] = record
    ledger['order'].append(rid)
    return copy.deepcopy(record)


# ── 纯读视图：GET 不得启动任何写流程（S03/M08 先例）────────────────


def get_record(ledger, record_id):
    _check_ledger(ledger)
    record = ledger['records'].get(_text(record_id))
    return copy.deepcopy(record) if record is not None else None


def list_records(ledger):
    _check_ledger(ledger)
    return list(ledger['order'])


def explain_reflection(ledger, reflection_id):
    """回答「这次反思为什么这样产」：逐条解释层归宿 + 稳定 reason_code + 独立支持，
    全部来自账本事实记录，不编叙事。"""
    record = get_record(ledger, reflection_id)
    if record is None:
        raise ValueError('reflection_not_found: %s' % reflection_id)
    return {'reflection_id': record['reflection_id'],
            'reason_code': record['reason_code'],
            'new_sources': list(record['new_sources']),
            'counts': dict(record['counts']),
            'reflections': [{'object_id': d['object_id'], 'output_kind': d['output_kind'],
                             'promotable': d['promotable'], 'reason_code': d['reason_code'],
                             'independent_sources': d['independent_sources'],
                             'root_groups': list(d['root_groups'])}
                            for d in record['reflections']],
            'promoted': [d['object_id'] for d in record['promotions']],
            'rejected': [{'object_id': d['object_id'], 'reason_code': d['reason_code']}
                         for d in record['rejected']],
            'consumed_sources': list(record.get('consumed_sources') or []),
            'output_ids': list(record.get('output_ids') or [])}


# ── 只读桥：完全委托 M08 冻结只读桥，缺库报 absent/degraded，绝不建库 ──


def read_reflection_inputs():
    """只读收集反思输入：隔离候选、信念（含原句 span）、M05 状态/边/epoch。

    完全复用 M08.read_consolidation_inputs（mode=ro + query_only，DATA_DIR 调用时
    解析，缺库报 absent，读坏报 unreadable，绝不补造）。本模块自身不开库、不解析
    路径。反思候选取隔离候选层（claim_candidate），绝不把已核验信念当新证据。
    """
    inputs = mc.read_consolidation_inputs()
    return {'candidates': inputs['candidates'], 'beliefs': inputs['beliefs'],
            'states': inputs['states'], 'edges': inputs['edges'],
            'epoch': inputs['epoch'], 'degradation': inputs['degradation']}


def freeze_window(event_ids):
    """只读冻结这批对象引用的真实事件（走 M08.freeze_window → self_model.source_window）。

    缺库/缺事件返回空窗口：原句自然绑不上，按降级处理，绝不补造事件。
    """
    return mc.freeze_window(event_ids)


# ── 落账：只经 M08.apply_consolidation → cognition_writer.commit 单一写入者 ──


def apply_reflection(plan, window=None, *, now):
    """把反思计划里的晋升经 M08 事务落账（M08 本身只走 cognition_writer.commit）。

    只执行 promotable 的 belief 层晋升；解释层非晋升候选与 rejected 不写。反思
    绝不落事件层：任何 promotion 的 fate 不是 belief 一律大声拒绝。依赖边注册
    复用 M08 事务（basis m08.consolidation，只加边不推进 epoch）。坏输入/门降级
    如实登记，绝不把推断/想象写成事实，绝不写真实 data/（隔离由调用方备 DATA_DIR）。

    返回 M08 只读投影 {status, promoted, deferred, rejected, duplicates,
    edges_added, epoch, promotions[], provenance, limitations}，附 L01 版本与边界。
    """
    at = _require_now(now)
    if not isinstance(plan, dict) or 'promotions' not in plan:
        raise ValueError('apply_reflection requires a reflection_plan')
    for decision in plan['promotions']:
        if decision.get('fate') != 'belief':
            raise ValueError('reflection promotion must be belief-layer, never event')
    result = mc.apply_consolidation(plan, list(window or []), now=at)
    result['provenance']['reflection_algorithm_version'] = ALGORITHM_VERSION
    result['limitations'] = list(LIMITATIONS)
    return result