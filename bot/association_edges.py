# -*- coding: utf-8 -*-
"""R02 类型化稀疏联想边（统一方案 ripple 轨 / R01 之上的只读边投影）。

R01 把各库投影成统一只读检索对象（item 带 object_id/object_kind/store/
root_source_ids/evidence_spans 等），并在 retrieve_evidence() 里把 paths 留空，
注明「边投影是 R02/R04」。本模块就在这些 item 之上派生**类型化、稀疏**的联想边：
每条边带 type、两端 object_id（及各自 kind）、basis（为什么有这条边，可追溯到
真实来源）。边只从 item 自己的 root_source_ids / evidence_spans / subject_ref
派生，绝不凭空造边。

四条硬边界（与 R01/M02 同一范式）：
- 真只读、纯投影。本模块**不 import retrieval_objects，也不打开任何库**：R01 的
  只观察静态锁（test_retrieval_objects::test_the_adapter_is_not_wired_into_any_reply_path_yet）
  禁止任何 bot 模块 import retrieval_objects / 调 retrieve_evidence /
  fetch_retrieval_items。为了既「在 R01 对象之上」又不撞那道锁，R02 做成 M02 那样
  的纯函数：调用方把 R01 已经「调用时解析路径、只读投影」好的 items 与 degradation
  传进来。库文件不存在时 R01 报 absent/degraded，R02 原样承接并继续报 degraded，
  绝不建库建表建目录（X15），也绝不在模块顶层冻路径。
- 类型化。边的关系种类见 EDGE_TYPES；两端 kind 让 belief↔source_event、
  person↔belief、episode↔event、shared_entity 这些关系都能被明确表达与核对。
- 稀疏。有界度数（MAX_DEGREE）与全局上限（MAX_EDGES），按共享键分组派生，
  不构建全图、不做全对全连接；被上限挡下的边记进 provenance.dropped，可解释。
- 只做边投影。**不做遍历**（受限一跳是 R04，所以这里只产 edges、不产 paths）、
  **不做融合打分**（R03，边不带任何 score，也不读 item.match.score_parts）、
  **不判矛盾**（R05，status 永远不会是 conflicting）。不冒充做了这些。

认识论口径沿用 M02：basis 里的 quote/offset 只在 item 真的带了 evidence_span 时
才登记，lexical 命中不冒充语义核验；subject_ref 只取 item/spans 里真实存在的值，
R01 认知投影未携带 subject_ref 时就不造 person 边（实体归一是 M03，不替它编 ID）。
"""
import hashlib

# 关系种类。两端 kind 另行携带，便于表达 belief↔source_event / person↔belief 等。
EDGE_TYPES = ('source_event', 'source_url', 'subject_ref', 'shared_source', 'shared_entity')
# 不含 conflicting：矛盾判定是 R05，这里不冒充。
STATUSES = ('sufficient', 'partial', 'not_found', 'degraded')
# 稀疏上限：单个对象（含来源事件/人物这类 hub）最多挂这么多条边；全局边数封顶。
MAX_DEGREE = 8
MAX_EDGES = 256
# 知识库存的是 URL 来源，不是事件；别把 URL 冒充成 source_event。
_URL_STORES = frozenset(('knowledge',))


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _root_sources(item):
    """item 自己登记的根本来源 ID（事件号或 URL）。只取真实存在的，不补造。"""
    out, seen = [], {}
    for value in (item.get('root_source_ids') or []):
        key = str(value or '').strip()
        if key and key not in seen:
            seen[key] = True
            out.append(key)
    return out


def _subject_refs(item):
    """item/spans 里真实存在的主体引用（M02 口径）。

    R01 认知投影没有把 subject_ref 带进 item，这里就读不到，于是也不造 person 边；
    persona/group 的 spans 若带了 subject_ref 才派生。绝不替 M03 编一个实体 ID。
    返回 {subject_ref: basis}，basis 指向真实 span 的 event_id / subject_basis。
    """
    refs = {}
    top = str(item.get('subject_ref') or '').strip()
    if top:
        refs[top] = {'subject_ref': top,
                     'subject_basis': str(item.get('subject_basis') or ''),
                     'event_id': ''}
    for span in (item.get('evidence_spans') or []):
        if not isinstance(span, dict):
            continue
        ref = str(span.get('subject_ref') or '').strip()
        if ref and ref not in refs:
            refs[ref] = {'subject_ref': ref,
                         'subject_basis': str(span.get('subject_basis') or ''),
                         'event_id': str(span.get('event_id') or '')}
    return refs


def _span_for(item, event_id):
    """找到指向某来源事件的逐字 span（若有），让 basis 能追到原句。"""
    for span in (item.get('evidence_spans') or []):
        if isinstance(span, dict) and str(span.get('event_id') or '') == event_id:
            return span
    return None


def _edge(edge_type, a, a_kind, b, b_kind, basis):
    """一条类型化边。basis_key 仅用于去重，不对外暴露成打分。"""
    basis_key = basis.get('event_id') or basis.get('url') or basis.get('subject_ref') \
        or ','.join(basis.get('event_ids') or []) or ','.join(basis.get('subject_refs') or [])
    return {
        'edge_id': 'edge_' + _sha(edge_type, a, b, basis_key),
        'type': edge_type,
        'a': a, 'a_kind': a_kind,
        'b': b, 'b_kind': b_kind,
        'basis': basis,
        '_basis_key': basis_key,
    }


def _source_edges(item):
    """item ↔ 其根本来源。事件来源走 source_event，URL 来源走 source_url。"""
    out = []
    oid = str(item.get('object_id') or '')
    kind = str(item.get('object_kind') or '')
    is_url = str(item.get('store') or '') in _URL_STORES
    for source in _root_sources(item):
        if is_url:
            basis = {'reason': 'root_source_id', 'url': source}
            out.append(_edge('source_url', oid, kind, source, 'source_url', basis))
        else:
            basis = {'reason': 'root_source_id', 'event_id': source}
            span = _span_for(item, source)
            if span is not None:
                # 有逐字 span 就登记原句与 offset（M02 口径），可追溯到真实源文本。
                basis.update({'quote': str(span.get('quote') or ''),
                              'span_start': span.get('span_start'),
                              'span_end': span.get('span_end'),
                              'source_revision': str(span.get('source_revision') or ''),
                              'match_kind': str(span.get('match_kind') or '')})
            out.append(_edge('source_event', oid, kind, source, 'source_event', basis))
    return out


def _subject_edges(item):
    """item ↔ 其主体（person↔belief 等）。只在 subject_ref 真实存在时派生。"""
    out = []
    oid = str(item.get('object_id') or '')
    kind = str(item.get('object_kind') or '')
    for ref, basis in sorted(_subject_refs(item).items()):
        out.append(_edge('subject_ref', oid, kind, ref, 'person', dict(basis)))
    return out


def _shared_groups(items):
    """按共享键分组：shared_source 按根本来源 ID，shared_entity 按 subject_ref。

    返回 {(edge_type, basis_field, key): [item, ...]}。只把真的共享同一键的 item
    放进同一组，组内再两两派生——这是稀疏的「按键分组」，不是全图。
    """
    groups = {}
    for item in items:
        for source in _root_sources(item):
            groups.setdefault(('shared_source', 'event_ids', source), []).append(item)
        for ref in _subject_refs(item):
            groups.setdefault(('shared_entity', 'subject_refs', ref), []).append(item)
    return groups


def _shared_edges(groups, window):
    """把共享键分组聚合成「每对 item 一条边」，basis 列全共享键。稀疏、可追溯。

    跨组合并：b1、b2 同时共享 e1 与 e2 时只产一条 shared_source 边，
    basis.event_ids=[e1,e2]，而不是每个键各产一条。
    每组只取排序后的前 window 个 item 参与配对：度数反正被 max_degree 封顶，
    取更多只会物化全对全团（违背「不构建全图」）。window=max_degree+1 足以让
    每个入选项至少有机会挂一条边，其余 item 仍保留自己的来源边，遍历留给 R04。
    """
    pairs = {}
    for (edge_type, basis_field, key), group in groups.items():
        ordered = sorted(group, key=lambda x: str(x.get('object_id') or ''))[:max(1, window)]
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                aid, bid = str(a.get('object_id') or ''), str(b.get('object_id') or '')
                if aid == bid:
                    continue
                pk = (edge_type, basis_field, aid, bid,
                      str(a.get('object_kind') or ''), str(b.get('object_kind') or ''))
                pairs.setdefault(pk, {})[key] = True
    out = []
    for (edge_type, basis_field, aid, bid, a_kind, b_kind), keys in sorted(pairs.items()):
        basis = {'reason': edge_type, basis_field: sorted(keys)}
        out.append(_edge(edge_type, aid, a_kind, bid, b_kind, basis))
    return out


def project_edges(items, degradation=None, *, max_degree=MAX_DEGREE, max_edges=MAX_EDGES):
    """在 R01 的只读 item 之上派生类型化稀疏联想边。纯函数，不读库、不写任何资产。

    items: R01 retrieve_evidence()/fetch_retrieval_items() 产出的 item 列表
           （调用方负责传入已按范围硬过滤的 items；R02 不重做范围过滤，那需要
           import R01，会撞 R01 的只观察锁）。
    degradation: R01 报的降级列表（absent/unreadable 等）。R02 原样承接：库不存在
           就继续报 degraded，绝不把「读不到」冒充成「她没有这段记忆」。

    返回 {status, edges, degradation, provenance}。只产 edges，不产 paths（遍历是
    R04）；边不带 score（融合打分是 R03）；status 不含 conflicting（矛盾是 R05）。
    """
    max_degree = max(0, int(max_degree))
    max_edges = max(0, int(max_edges))
    rows = [it for it in (items or []) if isinstance(it, dict)]
    # 防御性跳过已失效（撤回）item：R01 hard_filter 通常已挡，这里不复活撤回的依据。
    usable = [it for it in rows if it.get('validity') != 'invalidated']
    invalidated = len(rows) - len(usable)
    degradation = [dict(d) for d in (degradation or []) if isinstance(d, dict)]

    edges, degree, seen = [], {}, {}
    dropped = {}

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    def try_add(edge):
        if len(edges) >= max_edges:
            drop('edge_cap')
            return
        a, b = edge['a'], edge['b']
        if degree.get(a, 0) >= max_degree or degree.get(b, 0) >= max_degree:
            drop('degree_cap')
            return
        key = (edge['type'], a, b, edge['_basis_key'])
        if key in seen:
            return
        seen[key] = True
        edges.append(edge)
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    # 确定性顺序：先按 object_id 排序 item，逐 item 派生「自身来源/主体」边，
    # 再按共享键分组派生 item↔item 边。上限在 try_add 里增量执行，从不物化全图。
    ordered = sorted(usable, key=lambda x: str(x.get('object_id') or ''))
    no_basis = 0
    for item in ordered:
        candidates = _source_edges(item) + _subject_edges(item)
        if not candidates:
            no_basis += 1
        for edge in candidates:
            try_add(edge)
    for edge in _shared_edges(_shared_groups(ordered), max_degree + 1):
        try_add(edge)

    for edge in edges:
        edge.pop('_basis_key', None)

    if invalidated:
        dropped['invalidated'] = invalidated
    if degradation:
        status = 'degraded'
    elif not edges:
        status = 'not_found'
    elif no_basis or dropped.get('degree_cap') or dropped.get('edge_cap'):
        status = 'partial'
    else:
        status = 'sufficient'

    return {
        'status': status,
        'edges': edges,
        'degradation': degradation,
        'provenance': {
            'candidates': len(usable),
            'edges': len(edges),
            'dropped': dropped,
            'limitations': (
                'R02 只做类型化稀疏边投影：不做受限一跳遍历(R04，故只产 edges 不产 paths)、'
                '不做多线索融合打分(R03，边不带 score)、不判矛盾(R05，status 无 conflicting)；'
                '边只从 R01 item 的 root_source_ids/evidence_spans/subject_ref 派生，不凭空造边；'
                'basis 可追溯到真实来源，但 lexical 命中不冒充语义核验。',),
        },
    }