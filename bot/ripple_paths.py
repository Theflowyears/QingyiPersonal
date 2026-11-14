# -*- coding: utf-8 -*-
"""R04 受限一跳与根源去重（统一方案 ripple 轨 / R02 边与 R03 初召回之上的只读扩散）。

R02 在 R01 统一对象上派生类型化稀疏边时明确「只产 edges、不产 paths，受限一跳
是 R04」；R03 初召回选中种子 items 时也注明「不做联想遍历」。本模块接这个班：
在调用方传入的种子与边上做**一跳**扩散，并按 root_source_ids 做**根源去重**，
产出可追溯的 paths。每条 path 的 basis 登记经过哪条边（edge type、两端
object_id/kind）、为什么扩到它（共享来源/共享主体等真实键），绝不凭空造路径。

硬边界（与 R01/R02/R03 同一范式）：
- 真只读、纯函数。不 import retrieval_objects / association_edges / recall_scoring，
  也不打开任何库：R01/R03 的只观察静态锁禁止 bot 模块把它们接进链路。种子、边、
  全量 items 池与 degradation 全部由调用方传入；库不存在时 R01 报 absent/unreadable，
  这里原样承接并继续报 degraded，绝不建库建表建目录（X15），不在模块顶层冻路径。
- 一跳即止。种子→邻居就是终点，绝不从邻居继续扩（二跳是 R08，且需人工评审）；
  实现上只有「种子×边」两层循环，没有队列/递归/环游，结构上就走不出第二跳。
- 根源去重。同根源转述不算独立证据（M05/§5.4 同一口径）：种子的根源先登记为
  已支持，邻居只要与已支持根源有交集就归并，不产生独立 path；谁被并掉、因为
  哪个根源、并到谁，都记进 provenance.deduped。无根源的旧条目无法归并，如实声明。
- 失效防御。validity=invalidated 的对象不得作为种子或邻居复活（M05 失效单调）；
  种子与池两处登记不一致时取最严。详情未知的端点（不在池里的来源事件/URL/人物
  引用）不扩散：无法核对有效性与根源，宁可少扩也不复活撤回对象，记入 dropped。
- 不越界。不做融合打分（R03，path 不带 score，也不读 item.match.score_parts）、
  不判矛盾与缺失槽位（R05，status 永远不会是 conflicting）。范围硬过滤由调用方
  负责（与 R02 相同范式），R04 不重做，并在 limitations 里如实声明。

输出 {status, paths, degradation, provenance}；同输入同输出：种子、边、池全部
排序后处理，path_id 由内容决定，provenance 不含时钟，可回归。
"""
import hashlib

# 不含 conflicting：矛盾与缺失槽位判定是 R05，这里不冒充。
STATUSES = ('sufficient', 'partial', 'not_found', 'degraded')
# 一跳是硬上限，不是可调参数：二跳属于 R08，要先过人工评审。
MAX_HOPS = 1
# 预算是资源上限不是质量分：每个种子最多扩散这么多邻居，全局扩散数封顶。
MAX_DEGREE = 4
MAX_EXPANDED = 32


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _text(value):
    return str(value or '').strip()


def _roots(item):
    """item 自己登记的根本来源 ID。只取真实存在的，不补造（与 R02 同口径）。"""
    out, seen = [], {}
    for value in (item.get('root_source_ids') or []):
        key = _text(value)
        if key and key not in seen:
            seen[key] = True
            out.append(key)
    return out


def _index(rows):
    """按 object_id 建索引：排序后首见优先，重复 ID 不产生二义。

    排序键带上 revision/restatement，让同 ID 不同内容的重复行也与输入顺序无关。
    """
    out = {}
    ordered = sorted((r for r in (rows or []) if isinstance(r, dict)),
                     key=lambda r: (_text(r.get('object_id')),
                                    _text(r.get('revision')),
                                    _text(r.get('restatement'))))
    for row in ordered:
        oid = _text(row.get('object_id'))
        if oid:
            out.setdefault(oid, row)
    return out


def expand_paths(seeds, edges, *, items=None, degradation=None,
                 max_expanded=MAX_EXPANDED, max_degree=MAX_DEGREE):
    """在 R03 种子与 R02 边上做受限一跳扩散，按根源去重。纯函数，不读库不写资产。

    seeds:  R03 recall() 选中的 items（调用方负责已按范围硬过滤；R04 不重做）。
    edges:  R02 project_edges() 产出的类型化边列表。
    items:  可选的全量 items 池，提供一跳邻居的详情（有效性/根源）。不给池时
            只有种子自身可作邻居详情；指向池外端点的边记 dropped.unknown_neighbor。
    degradation: R01 报的降级列表（absent/unreadable 等），原样承接：读不到就
            继续报 degraded，绝不把「读不到」冒充成「她没有这段记忆」。

    返回 {status, paths, degradation, provenance}。status 描述的是扩散结果而非
    种子召回结果：一条独立扩散都没有就是 not_found。path 不带 score（R03），
    status 不含 conflicting（R05）。provenance.deduped 逐条登记归并细节。
    """
    max_expanded = max(0, int(max_expanded))
    max_degree = max(0, int(max_degree))
    degradation = [dict(d) for d in (degradation or []) if isinstance(d, dict)]
    dropped = {}

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    pool = _index(items)
    seed_rows = _index(seeds)
    known = dict(pool)
    for oid, row in seed_rows.items():
        prior = known.get(oid)
        # 失效单调（M05）：池与种子两处登记不一致时取最严，不给复活留缝。
        if prior is not None and prior.get('validity') == 'invalidated' \
                and row.get('validity') != 'invalidated':
            row = dict(row)
            row['validity'] = 'invalidated'
        known[oid] = row

    usable_seeds = []
    for oid in sorted(seed_rows):
        if known[oid].get('validity') == 'invalidated':
            drop('invalidated_seed')
            continue
        usable_seeds.append(known[oid])

    # 种子的根源先登记为已支持：初召回已经拿到的支持，转述不再算独立扩散。
    root_owner = {}
    for row in usable_seeds:
        for root in _roots(row):
            root_owner.setdefault(root, _text(row.get('object_id')))

    valid_edges = []
    for edge in (edges or []):
        if not isinstance(edge, dict):
            drop('malformed_edge')
            continue
        a, b = _text(edge.get('a')), _text(edge.get('b'))
        if not a or not b or not _text(edge.get('type')):
            drop('malformed_edge')
            continue
        if a == b:
            drop('self_loop')
            continue
        valid_edges.append(edge)
    ordered_edges = sorted(valid_edges, key=lambda e: (
        _text(e.get('type')), _text(e.get('edge_id')),
        _text(e.get('a')), _text(e.get('b'))))

    paths, deduped = [], []
    expanded_ids = {}
    per_seed = {}
    # 只有「种子×边」两层循环：邻居永远只当终点，结构上走不出第二跳（R08 才许）。
    for seed in usable_seeds:
        sid = _text(seed.get('object_id'))
        for edge in ordered_edges:
            a, b = _text(edge.get('a')), _text(edge.get('b'))
            if a == sid:
                nid = b
            elif b == sid:
                nid = a
            else:
                continue
            neighbor = known.get(nid)
            if neighbor is None:
                # 端点详情未知：无法核对有效性与根源，不扩散也不冒充认识它。
                drop('unknown_neighbor')
                continue
            if neighbor.get('validity') == 'invalidated':
                drop('invalidated_neighbor')
                continue
            nroots = _roots(neighbor)
            if nid in seed_rows:
                # 种子重叠是无序对的性质：只在字典序小的一侧记一次，
                # 免得两个方向各记一条归并、把同一件事数成两件事。
                if sid < nid:
                    deduped.append({'object_id': nid, 'merged_into': nid,
                                    'reason': 'seed_overlap',
                                    'root_source_ids': sorted(set(nroots) & set(_roots(seed)))})
                    drop('seed_overlap')
                continue
            if nid in expanded_ids:
                deduped.append({'object_id': nid, 'merged_into': nid,
                                'reason': 'already_expanded', 'root_source_ids': []})
                drop('already_expanded')
                continue
            shared = sorted(r for r in nroots if r in root_owner)
            if shared:
                deduped.append({'object_id': nid, 'merged_into': root_owner[shared[0]],
                                'reason': 'same_root', 'root_source_ids': shared})
                drop('same_root')
                continue
            if per_seed.get(sid, 0) >= max_degree:
                drop('degree_cap')
                continue
            if len(paths) >= max_expanded:
                drop('expansion_cap')
                continue
            basis = dict(edge.get('basis') or {}) if isinstance(edge.get('basis'), dict) else {}
            basis.setdefault('reason', _text(edge.get('type')))
            basis.update({'edge_id': _text(edge.get('edge_id')),
                          'edge_type': _text(edge.get('type')),
                          'a': a, 'a_kind': _text(edge.get('a_kind')),
                          'b': b, 'b_kind': _text(edge.get('b_kind'))})
            paths.append({
                'path_id': 'path_' + _sha(sid, nid, _text(edge.get('edge_id'))),
                'seed_id': sid,
                'seed_kind': _text(seed.get('object_kind')),
                'object_id': nid,
                'object_kind': _text(neighbor.get('object_kind')),
                'store': _text(neighbor.get('store')),
                'hops': MAX_HOPS,
                'root_source_ids': list(nroots),
                'basis': basis,
            })
            expanded_ids[nid] = True
            per_seed[sid] = per_seed.get(sid, 0) + 1
            for root in nroots:
                root_owner.setdefault(root, nid)

    if degradation:
        status = 'degraded'
    elif not paths:
        status = 'not_found'
    elif dropped.get('degree_cap') or dropped.get('expansion_cap') \
            or dropped.get('unknown_neighbor') or dropped.get('malformed_edge'):
        # 归并/失效/种子重叠是正常认识论行为，不是能力降级；只有预算与
        # 「想扩但核对不了」才让结果 partial（与 R01 对 filtered 的口径一致）。
        status = 'partial'
    else:
        status = 'sufficient'

    return {
        'status': status,
        'paths': paths,
        'degradation': degradation,
        'provenance': {
            'candidates': len(usable_seeds),
            'expanded': len(paths),
            'deduped': deduped,
            'dropped': dropped,
            'budget': {'max_expanded': max_expanded, 'max_degree': max_degree},
            'pool_size': len(pool),
            'edge_count': len(ordered_edges),
            'limitations': (
                'R04 只做受限一跳扩散与根源去重：一跳即止，二跳是 R08（需人工评审）；'
                '不做多线索融合打分(R03，path 不带 score)、不判矛盾与缺失槽位(R05，'
                'status 无 conflicting)；范围硬过滤由调用方负责（与 R02 同范式），'
                'R04 不重做范围过滤；basis 承接 R02 边的真实来源键，不新增语义核验；'
                '同根源转述不算独立证据，无根源的旧条目无法按根源归并；'
                '详情未知的端点不扩散，absent/unreadable 原样承接，'
                '绝不把「读不到」冒充成「她没有这段记忆」。',),
        },
    }