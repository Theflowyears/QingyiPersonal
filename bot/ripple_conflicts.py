# -*- coding: utf-8 -*-
"""R05 缺失槽位与冲突判定（统一方案 ripple 轨 / R04 一跳输出与 M04 裁决之上的只读判定）。

前序明确把两件事留给 R05：
- association_edges::project_edges 与 ripple_paths::expand_paths 都声明「不判矛盾
  （R05，status 永远不会是 conflicting）」；retrieval_objects::retrieve_evidence 的
  conflicts 恒为空、status 不含 conflicting。本模块接这个班：把 R04 扩散到的
  证据对象（可用种子 + 一跳邻居）喂给 M04 claim_conflicts::adjudicate 的有限家族
  两段式裁决，**只有 M04 给出 contradiction 裁决才升级 conflicting**，绝不自己
  长第二套矛盾引擎：家族判定、剥壳归一、verdict/reason_code 全部来自 M04，
  未知家族一律 unclassified（M04 兜底），不新造家族。
- ripple_paths::expand_paths 声明「不判缺失槽位（R05）」。本模块对检索到的每个
  证据对象逐槽核对「依据是否可定位」：根源、原句、主体、发生时间、已核验出处。
  缺就大声登记 missing_support（稳定 reason_code），绝不补造填充，也绝不把
  「缺依据」静默丢掉。

四种口径严格分开（评审清单「失败、未知、拒绝如实区分」）：
  missing      检索到的对象缺某槽依据（本模块 missing_support，逐对象登记）；
  dropped      R04 按规则挡下的边/端点（预算、未知端点、归并），原样承接计数；
  invalidated  撤回/失效对象：R04 已挡，这里再守一道，绝不复活、绝不参与裁决；
  conflicting  M04 contradiction 裁决（本模块 conflicts，记录原样来自 M04）。

同根源口径（§5.4 / M04 同根守卫 / R04 根源去重）：M04 的守卫只看单一
root_event_id，而 R01 item 的 root_source_ids 是列表。这里先按「同一对象内共现」
把根源 ID 确定性归并成连通组（代表取组内字典序最小者），再把组代表交给 M04：
根源有交集的两条候选在 M04 眼里就是同根转述，不算独立反例。这是把 R04 的
「交集即归并」口径接到 M04 守卫上，不是新的矛盾判定；无根源的旧条目无法归并，
如实声明。归并映射登记在 provenance.root_canon，可核对。

硬边界（与 R01/R02/R04 同一范式）：
- 真只读、纯函数。不 import retrieval_objects / recall_scoring（R01/R03 的只观察
  静态锁禁止 bot 模块把它们接进链路），也不打开任何库：种子、边、items 池与
  degradation 全部由调用方传入。库不存在时 R01 报 absent/unreadable，这里原样
  承接并继续报 degraded，绝不建库建表建目录（X15），不在模块顶层冻路径。
- 只认 M04 裁决。semantic_hint 原样转交 M04 登记，绝不覆盖结构判定；disputed /
  deferred 等不确定裁决原样记进 provenance.verdicts，不冒充矛盾也不冒充已解决。
- 不越界。不做融合打分（R03，结果不带 score，也不读 item.match.score_parts）、
  不做证据场景渲染与清单（R06）、不做多跳扩散（R08，需人工评审）；范围硬过滤
  由调用方负责（与 R02/R04 相同范式）。落账走唯一写入 reducer（M06），这里不碰。
- 确定性。无时钟、无随机：候选按 object_id 排序，根源归并代表取最小，缺失条目
  按 (slot, object_id) 排序，verdicts 沿用 M04 的确定性排序。同输入同输出。

已知边界（如实记录，不硬凑）：R01 认知投影目前不把 belief.subject_qq / span 的
subject_ref 带进 item，这类候选在 M04 主体门如实 deferred（subject_missing），
本模块不替它编主体（实体归一是 M03）；多主体条目取排序后首个真实 span 主体，
不做语义消解。有效期只认 item 显式携带的 valid_from/valid_to，绝不拿
occurred_at/recorded_at（记录时间）冒充有效期（M04 口径）。
"""
import copy

import claim_conflicts as cc
import ripple_paths as rp

# 与 R01 retrieval_objects.STATUSES 同一契约：conflicting 在这里才真正产生。
STATUSES = ('sufficient', 'partial', 'conflicting', 'not_found', 'degraded')

# 有限缺失槽位（沿用 R01 _missing_support 的缺口口径 + 联想所需的主体槽）。
SLOTS = ('root_source', 'original_quote', 'subject_ref', 'occurrence_time',
         'verified_provenance')
# 稳定 reason_code：缺什么就喊什么，不补造、不静默。no_evidence 是「一条证据
# 都没检索到」的聚合缺口（仅在无降级时成立；读不到不等于没有）。
MISSING_REASONS = ('no_evidence', 'root_source_missing', 'original_quote_missing',
                   'subject_ref_missing', 'occurrence_time_missing',
                   'provenance_unverified')
_SLOT_REASONS = {
    'root_source': 'root_source_missing',
    'original_quote': 'original_quote_missing',
    'subject_ref': 'subject_ref_missing',
    'occurrence_time': 'occurrence_time_missing',
    'verified_provenance': 'provenance_unverified',
}

LIMITATIONS = (
    'R05 只在 R04 一跳输出与种子上做矛盾裁决与缺失槽位登记：多跳扩散是 R08'
    '（需人工评审），本模块不扩边、不遍历。',
    '矛盾判定完全复用 M04 claim_conflicts 的有限家族两段式裁决，不自带第二套'
    '矛盾引擎；未知家族一律 unclassified（M04 兜底），不新造家族。',
    '只有 M04 verdict=contradiction 才升级 conflicting；disputed/deferred 等'
    '不确定裁决原样登记在 provenance.verdicts，不冒充矛盾也不冒充已解决。',
    '同根源转述不算独立反例：根源按共现归并后交给 M04 同根守卫，与 R04 根源'
    '去重同口径；无根源的旧条目无法归并，如实声明。',
    '融合打分是 R03：结果不带 score，也不读 item.match.score_parts；证据场景'
    '渲染与清单是 R06，本模块不渲染、不生成话术。',
    '缺失槽位只登记「检索到的对象缺什么依据」，不补造填充；R04 的 dropped'
    '（预算/未知端点/失效/归并）原样承接，不改判成 missing。',
    'invalidated 种子与邻居绝不复活，也不参与裁决；范围硬过滤由调用方负责'
    '（与 R02/R04 同范式），R05 不重做。',
    'absent/unreadable 降级原样承接：读不到继续报 degraded，绝不冒充「她没有'
    '这段记忆」；semantic_hint 只登记不覆盖结构判定（M04 口径）。',
    'R01 认知投影未携带 subject_ref 时，M04 主体门如实 deferred，不替它编主体'
    '（M03 范围）；多主体条目取排序后首个真实 span 主体，不做语义消解。',
    '纯函数只读：不 import retrieval_objects/recall_scoring，不打开任何库；'
    '落账走唯一写入 reducer（M06），这里不碰。',
)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _spans(item):
    return [s for s in (item.get('evidence_spans') or []) if isinstance(s, dict)]


def _subject_of(item):
    """主体只取 item/spans 里真实存在的引用（M02 口径），绝不编造。

    顶层 subject_ref 优先；否则取排序后首个 span 主体（多主体不做语义消解，
    那是 M03 的范围）。一个都没有就留空，让 M04 主体门如实 deferred。
    """
    top = _text(item.get('subject_ref'))
    if top:
        return top
    refs = sorted({_text(s.get('subject_ref')) for s in _spans(item)
                   if _text(s.get('subject_ref'))})
    return refs[0] if refs else ''


def _root_canon(rows):
    """根源 ID 按「同一对象内共现」归并成连通组，代表取组内字典序最小者。

    这是把 R04 的「交集即归并」接到 M04 单根守卫上的确定性结构归一：
    根源有交集的两条候选拿到同一个 root_event_id，M04 便按同根转述处理，
    不算独立反例。不判矛盾、不打分；无根源的对象不参与归并。
    """
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for row in rows:
        roots = sorted(set(rp._roots(row)))
        for root in roots:
            parent.setdefault(root, root)
        for root in roots[1:]:
            a, b = find(roots[0]), find(root)
            if a != b:
                if b < a:
                    a, b = b, a
                parent[b] = a
    return {root: find(root) for root in sorted(parent)}


def _candidate(item, canon):
    """R01 只读 item → M04 兼容候选。只搬真实字段，绝不补造有效期/认识类型。

    值字段原样透传，由 M04 _value_of 决定口径（不长第二套剥壳）；家族由 M04
    classify_predicate 判定（未知→unclassified）。occurred_at/recorded_at 是
    记录时间，不当 valid_from/valid_to 用（M04 口径）。
    """
    roots = rp._roots(item)
    oid = _text(item.get('object_id'))
    cand = {
        'candidate_id': oid,
        'object_id': oid,
        'subject_ref': _subject_of(item),
        'negation': item.get('negation', ''),
        'qualifiers': list(item.get('qualifiers') or []),
        'valid_from': item.get('valid_from'),
        'valid_to': item.get('valid_to'),
        'time_expression': _text(item.get('time_expression')),
        'epistemic_kind': _text(item.get('epistemic_kind')),
        # 组代表交给 M04 同根守卫；evidence 仍列全真实根源，可追溯。
        'root_event_id': canon.get(roots[0], '') if roots else '',
        'evidence': [{'event_id': root} for root in roots],
        'validity': _text(item.get('validity')),
    }
    for key in ('object_value', 'value', 'restatement', 'proposition', 'content',
                'text', 'predicate_family', 'kind', 'predicate'):
        if key in item:
            cand[key] = item[key]
    return cand


def _missing_for(item, required):
    """逐槽核对「依据是否可定位」，缺就登记稳定 reason_code，不补造填充。"""
    oid = _text(item.get('object_id'))
    spans = _spans(item)
    occurred = item.get('occurred_at')
    located = {
        'root_source': bool(rp._roots(item)),
        'original_quote': any(_text(s.get('quote')) for s in spans),
        'subject_ref': bool(_subject_of(item)),
        'occurrence_time': _number(occurred) and occurred > 0,
        'verified_provenance': item.get('validity') == 'valid',
    }
    return [{'slot': slot, 'reason_code': _SLOT_REASONS[slot], 'object_id': oid}
            for slot in sorted(required) if not located[slot]]


def _resolve_status(base, degradation, conflicts, missing):
    """status 优先级：degraded > conflicting > not_found > partial > sufficient。

    降级是能力问题，压过一切（但 conflicts/missing 仍逐条列出，不静默）；
    conflicting 是 M04 给出的确定性认识论发现，压过 not_found/partial；
    not_found 沿用 R04「一条独立扩散都没有」的口径；缺失槽位与 R04 的
    partial（预算/未知端点）都让结果 partial。
    """
    if degradation:
        return 'degraded'
    if conflicts:
        return 'conflicting'
    if base == 'not_found':
        return 'not_found'
    if missing or base == 'partial':
        return 'partial'
    return 'sufficient'


def assess_expansion(expanded, *, seeds=None, items=None, required_slots=SLOTS,
                     semantic_hints=None):
    """在 R04 expand_paths() 输出之上做矛盾裁决与缺失槽位登记。纯函数，不读库。

    expanded: R04 的输出 {status, paths, degradation, provenance}。paths 与
              degradation 原样承接（深拷贝），R04 的 deduped/dropped/budget 记进
              provenance，不改判。
    seeds:    传给 R04 的同一批种子 items。给了才能裁决「种子×种子」的矛盾；
              不给时只能从 paths 的 seed_id 回查 items 池，没扩出路径的种子
              覆盖不到（如实声明，不冒充全量）。
    items:    全量 items 池，提供种子/邻居的详情（主体/值/根源/有效性）。
    required_slots: 要核对的槽位（SLOTS 的子集）；None/() 表示不做槽位核对。
              未知槽位大声拒绝（ValueError），不静默忽略。
    semantic_hints: 原样转交 M04（{(left_id, right_id): hint}），只登记不覆盖。

    返回 {status, paths, conflicts, missing_support, degradation, provenance}。
    conflicts 只含 M04 verdict=contradiction 的记录（原样，不添油加醋）；全部
    裁决记在 provenance.verdicts。missing_support 逐对象登记缺槽；一条证据都
    没有且无降级时登记聚合缺口 no_evidence（读不到≠没有，降级时不这么喊）。
    """
    expanded = expanded if isinstance(expanded, dict) else {}
    prov = expanded.get('provenance') or {}
    base = _text(expanded.get('status')) or 'not_found'
    paths = copy.deepcopy(expanded.get('paths') or [])
    degradation = copy.deepcopy(expanded.get('degradation') or [])
    deduped = copy.deepcopy(prov.get('deduped') or [])
    dropped = dict(prov.get('dropped') or {})

    required = tuple(required_slots or ())
    for slot in required:
        if slot not in _SLOT_REASONS:
            raise ValueError('unknown required slot: %s' % slot)

    pool = rp._index(items)
    seed_rows = rp._index(seeds)
    known = dict(pool)
    for oid, row in seed_rows.items():
        prior = known.get(oid)
        # 失效单调（M05/R04 同口径）：池与种子两处登记不一致时取最严。
        if prior is not None and prior.get('validity') == 'invalidated' \
                and row.get('validity') != 'invalidated':
            row = dict(row)
            row['validity'] = 'invalidated'
        known[oid] = row

    rows = {}
    for oid in sorted(seed_rows):
        if known[oid].get('validity') != 'invalidated':
            rows[oid] = known[oid]
    if seeds is None:
        # 没给种子时从 paths 回查 seed 详情；查不到的大声计数，不静默丢。
        for path in paths:
            sid = _text(path.get('seed_id'))
            if not sid or sid in rows:
                continue
            row = known.get(sid)
            if row is None:
                dropped['unknown_candidate'] = dropped.get('unknown_candidate', 0) + 1
            elif row.get('validity') != 'invalidated':
                rows[sid] = row
    for path in paths:
        nid = _text(path.get('object_id'))
        if not nid or nid in rows:
            continue
        row = known.get(nid)
        if row is None:
            dropped['unknown_candidate'] = dropped.get('unknown_candidate', 0) + 1
            continue
        if row.get('validity') == 'invalidated':
            # R04 已挡失效邻居；这里再守一道，绝不复活、绝不参与裁决。
            dropped['invalidated_candidate'] = dropped.get('invalidated_candidate', 0) + 1
            continue
        rows[nid] = row

    ordered = [rows[oid] for oid in sorted(rows)]
    canon = _root_canon(ordered)
    candidates = [_candidate(row, canon) for row in ordered]
    # 矛盾判定全部来自 M04：家族、剥壳、verdict/reason_code 都不在这里长。
    records = copy.deepcopy(cc.adjudicate(candidates, semantic_hints))
    conflicts = [r for r in records if r.get('verdict') == 'contradiction']
    verdict_counts = {verdict: 0 for verdict in cc.VERDICTS}
    for record in records:
        verdict_counts[record['verdict']] += 1

    missing = []
    for oid in sorted(rows):
        missing.extend(_missing_for(rows[oid], required))
    if not rows and not degradation:
        missing.append({'slot': 'evidence', 'reason_code': 'no_evidence',
                        'object_id': ''})
    missing.sort(key=lambda m: (m['slot'], m['object_id'], m['reason_code']))

    return {
        'status': _resolve_status(base, degradation, conflicts, missing),
        'paths': paths,
        'conflicts': conflicts,
        'missing_support': missing,
        'degradation': degradation,
        'provenance': {
            'candidates': prov.get('candidates', 0),
            'expanded': len(paths),
            'deduped': deduped,
            'dropped': dropped,
            'budget': copy.deepcopy(prov.get('budget') or {}),
            'pool_size': prov.get('pool_size', len(pool)),
            'edge_count': prov.get('edge_count', 0),
            'adjudicated': len(candidates),
            'pairs': len(records),
            'verdicts': records,
            'verdict_counts': verdict_counts,
            'missing_count': len(missing),
            'root_canon': canon,
            'limitations': LIMITATIONS,
        },
    }


def resolve_ripple(seeds, edges, *, items=None, degradation=None,
                   required_slots=SLOTS, semantic_hints=None,
                   max_expanded=rp.MAX_EXPANDED, max_degree=rp.MAX_DEGREE):
    """R04 受限一跳 + R05 矛盾裁决与缺失槽位，一次调用。纯函数，不读库不写资产。

    seeds/edges/items/degradation 与 R04 expand_paths 同口径（调用方负责范围
    硬过滤）；预算原样转交 R04。返回与 assess_expansion 相同的结构。
    """
    expanded = rp.expand_paths(seeds, edges, items=items, degradation=degradation,
                               max_expanded=max_expanded, max_degree=max_degree)
    return assess_expansion(expanded, seeds=seeds, items=items,
                            required_slots=required_slots,
                            semantic_hints=semantic_hints)