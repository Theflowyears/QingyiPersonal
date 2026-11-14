# -*- coding: utf-8 -*-
"""R08 可选离线向量边与受限二跳（ripple 轨 / R04/R05/R06 链路之上的隔离契约 + 人工评审包）。

R04 把联想扩散硬限一跳（MAX_HOPS=1），并注明「二跳是 R08，且需人工评审」；R02 的
边只从真实 root_source_ids/evidence_spans/subject_ref 派生。本模块接这条 carve-out，
把两个**可选扩展**做成隔离的可执行契约：
  (a) 离线预计算向量相似边（新边类型 vector_similarity，由调用方传入）；
  (b) 受限二跳扩散（第二跳种子只来自第一跳邻居，防环、跨跳同根源归并、全局预算）。
两者都需人工评审才能进真实链路——本模块**默认全关**（allow_vector_edges/
allow_second_hop 默认 False），真实路径（R04 一跳 + R05 裁决 + R06 渲染）一行不改，
只产出可供人工评审的对照包。R08 是 manual_review 任务：契约可执行 ≠ 真实实验已做。
真实实验需真实离线向量批次与真实样本，本契约只证明可执行；organic 口径 not_observed、
authorization 口径 manual_review（与 R07/V02 相同），judgement 永远留空给人判，
本模块不下「向量/二跳有用没用」的结论，expression_control 恒 False（不接管表达）。

设计理由与硬边界（与 R01/R02/R04/R05/R06/R07 同一范式）：
- 纯函数、真只读。不 import retrieval_objects/recall_scoring/association_edges
  （R01/R03 只观察静态锁禁止 bot 模块把它们接进链路），也不打开任何库：种子、边、
  向量边、items 池与 degradation 全部由调用方传入。无时钟无随机，now 是调用方必填
  的逻辑时间，只回显进计划供审计绑定，不参与任何判定（R07 同口径）。
- 向量边是调用方**离线预计算**的输入：本模块不算 embedding、不调模型（LLM）、
  不发网络、不验证向量真实性，只校验形状与离线出处（basis 必须带数值有限
  similarity、非空 model_id 与 batch_id，缺则大声拒绝该边）。similarity 只登记进
  basis 供追溯，**绝不用于排序/打分**：遍历顺序仍是 R04 的 type/edge_id/a/b 确定性
  排序，融合打分是 R03 的事。坏边逐条大声登记 dropped（稳定 reason_code），不静默丢；
  向量边与真实边 edge_id 冲突大声登记，真实边优先。
- 复用而非重造。一跳基线直接调 R05 resolve_ripple（内含 R04 受限一跳与 M04 裁决）
  与 R06 render_evidence，与 R07 逐字同链路；二跳只调两次 R04 expand_paths（第一跳
  原预算，第二跳 max_second_degree/max_second_expanded），合并后交 R05
  assess_expansion 与 R06 render_evidence，不复制平行遍历/裁决/渲染实现。
  冲突/降级/失效/缺槽语义完全承接 R04/R05/R06：不重新裁决、invalidated 绝不复活、
  unknown_neighbor 承接 R04「详情未知不扩散」语义、缺失不补造、degraded 不冒充充分。
- 二跳合并纪律。第二跳种子只从第一跳 paths 的邻居构造（池里没有详情的邻居不扩，
  大声登记）；第二跳路径指向种子/第一跳邻居的剔除（防环防重复，reason=revisit）；
  与种子/第一跳同根源的归并（同根源转述不算独立证据，§5.4，reason=same_root）；
  每条二跳路径带 via={first_hop_path_id, intermediate_id} 链式可追溯，path_id 由
  内容（含 via）决定；预算全局有界（max_expanded + max_second_expanded），超限大声
  dropped(reason=budget 等)，绝不静默截断。rp.MAX_HOPS=1 不被改写：二跳是本契约
  显式组合两次一跳，不是把 R04 的硬上限调大。
- 计划内容寻址。plan_id='rx_'+A03 digest(计划核心)，核心含 now/全部输入的内容绑定
  （排序归一后的 SHA-256）/开关/预算/变体定义；执行前按同一输入重建计划并核对
  plan_id，不一致大声拒绝，执行只认重建出的定义（R07 防篡改同口径）。
- 变体集合固定可枚举（VARIANT_IDS）：one_hop 基线恒执行；one_hop_vector 仅
  allow_vector_edges=True 执行；two_hop 仅 allow_second_hop=True 执行；
  two_hop_vector 需两者都开。未启用的变体如实登记 disabled，不假装跑过。
- 差异报告只做结构对照：以 one_hop 为基线逐执行变体对照 selected/excluded/dropped
  的 added/removed/changed，稳定排序、稳定 diff_reason、sha16 引用（R07 同口径，
  只记 ID/长度/哈希不落原文）。差异不解释因果、不打分。
- 评审包只带 ID/reason_code/sha16 引用与 R06 清单（A03 口径）；restatement/原句留在
  R06 场景里，不进本包。不伪造真实样本、不伪造结论字段、不生成聊天文本、不做真实
  注入接线（C04）。

已知边界（如实记录，不硬凑）：R04 已把失效邻居/未知端点挡在 paths 之外，R05/R06
只保留 dropped 聚合计数，本模块按聚合计数承接，不伪造逐对象明细；无根源的旧条目
无法按根源归并，沿用 R04/R05 的如实声明；向量边的语义质量（embedding 是否可信）
本契约无法核验，只能核对离线出处登记，真实性由人工评审判。
"""
import copy
import json
import math

import memory_manifest as mm
import ripple_conflicts as rc
import ripple_evidence as re6
import ripple_paths as rp

# 算法版本：评审包与计划都绑定它，口径变化必须改版本。
ALGORITHM_VERSION = 'r08-1'
# 新边类型：离线预计算向量相似边。只由调用方传入，本模块不产边（R02 才产真实边）。
VECTOR_EDGE_TYPE = 'vector_similarity'
# 变体集合固定可枚举：one_hop 是基线，其余是可选扩展。顺序即执行与差异报告顺序。
VARIANT_IDS = ('one_hop', 'one_hop_vector', 'two_hop', 'two_hop_vector')
# 变体定义里「改了什么/没改什么」的开关全集（两个扩展开关 + 四个真实预算）。
KNOBS = ('vector_edges', 'second_hop', 'max_expanded', 'max_degree',
         'max_second_degree', 'max_second_expanded')
# 差异条目自身的稳定结构码（与 R07 逐字同口径：描述结构变化，不是因果结论）。
DIFF_REASONS = ('selected_added', 'selected_removed', 'selected_changed',
                'excluded_added', 'excluded_removed', 'excluded_changed',
                'dropped_added', 'dropped_removed', 'dropped_changed')
# 向量边校验的稳定 reason_code：缺什么就喊什么，逐条大声登记，不静默丢。
VECTOR_REASONS = ('malformed_vector_edge', 'vector_type_mismatch', 'self_loop',
                  'vector_edge_id_missing', 'vector_provenance_missing',
                  'vector_edge_id_conflict')
# 二跳合并的稳定 reason_code：revisit=防环防重复；same_root=跨跳同根源归并（§5.4）；
# second_hop_seed_unavailable=第一跳邻居在池里没有详情，不扩；budget=全局预算超限。
MERGE_REASONS = ('revisit', 'same_root', 'second_hop_seed_unavailable', 'budget')
# 评审口径与 R07/V02 相同：manual_review 任务，organic 留 not_observed。
REVIEW_AUTHORIZATION = 'manual_review'
REVIEW_ORGANIC = 'not_observed'
# 二跳预算默认值（显式资源上限，不是质量分）：第二跳每种子度数与全局扩散数封顶。
MAX_SECOND_DEGREE = 2
MAX_SECOND_EXPANDED = 8
# dropped 差异段承接的排除分类（R04/R05 聚合 + R06 预算）；其余归 excluded 段。
_DROP_CATEGORIES = ('dropped', 'invalidated', 'budget')

LIMITATIONS = (
    'R08 是 manual_review 的隔离契约：两个扩展开关默认全关（allow_vector_edges/'
    'allow_second_hop 默认 False），真实路径（R04 一跳 + R05 + R06）一行未改；'
    '契约可执行不等于真实实验已做，真实实验需真实离线向量批次与真实样本；'
    'organic 口径 not_observed，authorization 口径 manual_review，judgement 留空'
    '给人判，expression_control 恒 False，不接管表达、不接进回复链路（C04）。',
    '向量边是调用方离线预计算的输入：本模块不算 embedding、不调模型（LLM）、'
    '不发网络、不验证向量真实性，只校验形状与离线出处（model_id/batch_id/'
    'similarity 登记）；向量语义是否可信由人工评审判，本契约不冒充已核验。',
    'similarity 只登记进 basis 供追溯，绝不用于排序/打分：遍历顺序仍是 R04 的 '
    'type/edge_id/a/b 确定性排序，融合打分是 R03 的事，本模块不长任何打分机器。',
    '二跳是需人工评审的可选扩展：第二跳种子只来自第一跳邻居，防环（revisit）、'
    '跨跳同根源归并（same_root，同根源转述不算独立证据 §5.4）、全局预算有界；'
    'rp.MAX_HOPS=1 不被改写，二跳是显式组合两次 R04 一跳，不是调大 R04 硬上限。',
    '冲突/降级/失效/缺槽语义完全承接 R04/R05/R06（M04 裁决）：不重新裁决、不长'
    '第二套矛盾引擎、不伪造 verdict；invalidated 绝不复活、unknown_neighbor 承接 '
    'R04 详情未知不扩散语义、缺失不补造、degraded 不冒充充分；失效邻居/未知端点'
    '按 R05 dropped 聚合计数承接，不伪造逐对象明细。',
    '纯函数只读：不 import retrieval_objects/recall_scoring/association_edges，'
    '不打开任何库、无时钟无随机；now 由调用方必填（逻辑时间，只回显供审计绑定，'
    '不参与判定）；预算超限大声登记 dropped(reason=budget 等)，绝不静默截断。',
    '评审包只带 ID/reason_code/sha16 引用与 R06 清单（A03 口径：只记 ID、长度和'
    '哈希，不落原文）；restatement/原句留在 R06 场景，不进本包；差异报告以 '
    'one_hop 为基线逐执行变体对照，只做结构对照，不解释因果、不打分；未启用的'
    '变体如实登记 disabled，不假装跑过。',
)


def _text(value):
    return str(value or '').strip()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _ref(entry):
    """排除/预算条目的内容哈希引用：清单不落原文，评审仍能逐条对账（R07 同口径）。"""
    return mm.digest(_canonical(entry))[:16]


def _finite_number(value):
    """similarity 必须是数值且有限：bool/字符串/NaN/Inf 一律不算（大声拒绝该边）。
    大到无法转成 float 的整数同样拒绝（无法核验的数值不冒充有限 similarity）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _hints_of(plan):
    return {(left, right): hint
            for left, right, hint in (plan.get('semantic_hints') or [])}


def _edge_key(edge):
    """真实边的确定性排序键：type/edge_id/a/b（R04 同口径），canonical 只做并列 tie-break。"""
    if isinstance(edge, dict):
        return (_text(edge.get('type')), _text(edge.get('edge_id')),
                _text(edge.get('a')), _text(edge.get('b')), _canonical(edge))
    return ('', '', '', '', _canonical(edge))


def _vector_key(edge):
    """向量边校验顺序键：edge_id/a/b 确定性排序；canonical 只用于同 ID 重复登记的
    并列 tie-break（内容寻址取最小），与 similarity 大小无关，更不是打分。"""
    if isinstance(edge, dict):
        return (_text(edge.get('edge_id')), _text(edge.get('a')),
                _text(edge.get('b')), _canonical(edge))
    return ('', '', '', _canonical(edge))


def _validate_vector_edges(vector_edges, real_edges):
    """向量边校验（只收集/过滤，不裁决）：坏边逐条大声登记，好边原样深拷贝保留。

    固定检查顺序（一条边只报第一个命中的稳定码）：非 dict → 类型不是
    vector_similarity → 端点缺失 → 自环 → edge_id 缺失 → 离线出处缺失（basis 缺
    数值有限 similarity / 非空 model_id / 非空 batch_id）→ edge_id 与真实边或已收
    向量边冲突。similarity 只登记进 basis 供追溯，绝不参与排序/打分。
    """
    seen = {}
    for edge in real_edges:
        if isinstance(edge, dict) and _text(edge.get('edge_id')):
            seen[_text(edge.get('edge_id'))] = True
    kept, dropped = [], []
    for edge in sorted((e for e in (vector_edges or [])), key=_vector_key):
        if not isinstance(edge, dict):
            dropped.append({'edge_id': '', 'reason_code': 'malformed_vector_edge'})
            continue
        eid = _text(edge.get('edge_id'))
        a, b = _text(edge.get('a')), _text(edge.get('b'))
        if _text(edge.get('type')) != VECTOR_EDGE_TYPE:
            dropped.append({'edge_id': eid, 'reason_code': 'vector_type_mismatch'})
            continue
        if not a or not b:
            dropped.append({'edge_id': eid, 'reason_code': 'malformed_vector_edge'})
            continue
        if a == b:
            dropped.append({'edge_id': eid, 'reason_code': 'self_loop'})
            continue
        if not eid:
            dropped.append({'edge_id': '', 'reason_code': 'vector_edge_id_missing'})
            continue
        basis = edge.get('basis') if isinstance(edge.get('basis'), dict) else {}
        if not (_finite_number(basis.get('similarity'))
                and _text(basis.get('model_id')) and _text(basis.get('batch_id'))):
            dropped.append({'edge_id': eid, 'reason_code': 'vector_provenance_missing'})
            continue
        if eid in seen:
            dropped.append({'edge_id': eid, 'reason_code': 'vector_edge_id_conflict'})
            continue
        seen[eid] = True
        kept.append(copy.deepcopy(edge))
    dropped.sort(key=lambda x: (x['reason_code'], x['edge_id']))
    return kept, dropped


def _prepare(seeds, edges, vector_edges, items):
    """输入整理（只收集/过滤，不做任何裁决）：池/种子复用 R04 _index 排序去重，
    真实边原样保留（坏边交给 R04 大声登记 malformed_edge），向量边逐条校验。"""
    pool_index = rp._index(items)
    pool = [copy.deepcopy(pool_index[oid]) for oid in sorted(pool_index)]
    seed_index = rp._index(seeds)
    kept_seeds = [copy.deepcopy(seed_index[oid]) for oid in sorted(seed_index)]
    kept_edges = sorted((copy.deepcopy(e) if isinstance(e, dict) else e
                         for e in (edges or [])), key=_edge_key)
    kept_vector, vector_filter = _validate_vector_edges(vector_edges, kept_edges)
    return {'pool': pool, 'seeds': kept_seeds, 'edges': kept_edges,
            'vector_edges': kept_vector, 'vector_filter': vector_filter}


def _variant_definitions(allow_vector, allow_second):
    """固定变体集合：每个变体写清改了什么（changes）、没改什么（keeps）、是否启用。

    未启用的变体如实登记 enabled=False（执行时 disabled），不假装跑过。
    """
    specs = (
        ('one_hop', {}, True,
         '一跳基线：真实边 + R04 受限一跳 + R05 裁决 + R06 渲染，与 R07 逐字同链路，'
         '不改任何开关；向量边与二跳都不参与。'),
        ('one_hop_vector', {'vector_edges': True}, allow_vector,
         '一跳 + 离线向量边：真实边合并校验后的 vector_similarity 边，仍走 R04 一跳'
         '（MAX_HOPS=1 不变）；similarity 只登记不排序不打分。需 allow_vector_edges。'),
        ('two_hop', {'second_hop': True}, allow_second,
         '受限二跳（仅真实边）：第一跳 R04 原预算，第二跳种子只来自第一跳邻居，'
         'max_second_degree/max_second_expanded 封顶；防环 revisit、跨跳同根源归并 '
         'same_root、via 链式可追溯。需 allow_second_hop，进真实链路前需人工评审。'),
        ('two_hop_vector', {'vector_edges': True, 'second_hop': True},
         allow_vector and allow_second,
         '受限二跳 + 离线向量边：两个扩展同时启用，合并纪律与预算同 two_hop。'
         '需两个开关都开，进真实链路前需人工评审。'),
    )
    return [{'variant_id': vid, 'changes': copy.deepcopy(changes),
             'keeps': [k for k in KNOBS if k not in changes],
             'enabled': bool(enabled), 'definition': text}
            for vid, changes, enabled, text in specs]


def build_plan(*, now, seeds, edges, vector_edges=None, items=None, degradation=None,
               allow_vector_edges=False, allow_second_hop=False,
               max_expanded=rp.MAX_EXPANDED, max_degree=rp.MAX_DEGREE,
               max_second_degree=MAX_SECOND_DEGREE, max_second_expanded=MAX_SECOND_EXPANDED,
               required_slots=rc.SLOTS, semantic_hints=None):
    """扩展计划纯函数：默认全关 + 固定变体集合，内容寻址 plan_id='rx_'+digest[:14]。

    now:       必填的逻辑时间（调用方给定，无时钟），只回显进计划供审计绑定，不参与判定。
    seeds:     R03 初召回选中的 items（调用方给定，本契约不重跑 R03）。
    edges:     R02 project_edges() 的真实边（调用方给定，不重投影）。
    vector_edges: 调用方离线预计算的向量相似边（type 恒为 vector_similarity，basis
               必须带数值有限 similarity、非空 model_id/batch_id）；坏边逐条大声登记
               vector_filter，不静默丢。本模块不算 embedding、不调模型、不发网络。
    items:     全量 items 池（邻居详情/有效性/根源）；degradation 原样承接 R01。
    allow_vector_edges/allow_second_hop: 两个扩展开关，默认 False（默认全关是硬边界），
               只认真实布尔；未启用的变体如实登记 disabled。
    max_*:     预算（显式资源上限不是质量分）：一跳沿用 R04 的 max_expanded/max_degree，
               二跳第二跳用 max_second_degree/max_second_expanded，全局有界。
    required_slots/semantic_hints: 与 R05 同口径；未知槽位大声拒绝。

    返回计划 dict：核心含 now/全部输入的内容绑定（排序归一后的 SHA-256）/开关/预算/
    变体定义；不含任何聊天原文（只登记 ID 与哈希）。执行时按同一输入重建并核对 plan_id。
    """
    if now is None:
        raise ValueError('now_required: 逻辑时间必须由调用方给出')
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError('now_must_be_number: %r' % (now,))
    if not isinstance(allow_vector_edges, bool) or not isinstance(allow_second_hop, bool):
        raise ValueError('allow_flags_must_be_bool: 扩展开关只认真实布尔，默认全关')
    slots = tuple(required_slots or ())
    for slot in slots:
        if slot not in rc.SLOTS:
            raise ValueError('unknown required slot: %s' % slot)
    budgets = {'max_expanded': max(0, int(max_expanded)),
               'max_degree': max(0, int(max_degree)),
               'max_second_degree': max(0, int(max_second_degree)),
               'max_second_expanded': max(0, int(max_second_expanded))}
    prepared = _prepare(seeds, edges, vector_edges, items)
    pool, kept_seeds = prepared['pool'], prepared['seeds']
    kept_edges, kept_vector = prepared['edges'], prepared['vector_edges']

    hints = {}
    for (left, right), hint in (semantic_hints or {}).items():
        hints[(_text(left), _text(right))] = copy.deepcopy(hint)
    core = {
        'now': now,
        'algorithm_version': ALGORITHM_VERSION,
        'allow_vector_edges': allow_vector_edges,
        'allow_second_hop': allow_second_hop,
        'budgets': budgets,
        'required_slots': list(slots),
        'semantic_hints': [[left, right, hints[(left, right)]]
                           for left, right in sorted(hints)],
        'degradation': sorted((dict(d) for d in (degradation or []) if isinstance(d, dict)),
                              key=_canonical),
        'pool_ids': [_text(r.get('object_id')) for r in pool],
        'seed_ids': [_text(r.get('object_id')) for r in kept_seeds],
        'edge_ids': [_text(e.get('edge_id')) if isinstance(e, dict) else ''
                     for e in kept_edges],
        'vector_edge_ids': [_text(e.get('edge_id')) for e in kept_vector],
        'vector_filter': prepared['vector_filter'],
        # 全部输入的内容绑定：排序归一后的 SHA-256，执行前重建核对用（不落原文）。
        'bindings': {'seeds': mm.digest(_canonical(kept_seeds)),
                     'pool': mm.digest(_canonical(pool)),
                     'edges': mm.digest(_canonical(kept_edges)),
                     'vector_edges': mm.digest(_canonical(kept_vector))},
        'variants': _variant_definitions(allow_vector_edges, allow_second_hop),
    }
    return dict(core, plan_id='rx_' + mm.digest(_canonical(core))[:14])


def _rebuild(plan, seeds, edges, vector_edges, items, degradation, semantic_hints):
    """执行前按同一输入重建计划并核对内容地址：不一致大声拒绝，不悄悄跑偏（R07 同口径）。"""
    plan = plan if isinstance(plan, dict) else {}
    for key in ('plan_id', 'now', 'allow_vector_edges', 'allow_second_hop', 'budgets',
                'required_slots', 'semantic_hints', 'degradation', 'variants'):
        if key not in plan:
            raise ValueError('invalid_plan: missing %s' % key)
    if set(plan['budgets']) != {'max_expanded', 'max_degree',
                                'max_second_degree', 'max_second_expanded'}:
        raise ValueError('invalid_plan: budgets 键与契约不一致')
    rebuilt = build_plan(now=plan['now'], seeds=seeds, edges=edges,
                         vector_edges=vector_edges, items=items, degradation=degradation,
                         allow_vector_edges=plan['allow_vector_edges'],
                         allow_second_hop=plan['allow_second_hop'],
                         required_slots=plan['required_slots'],
                         semantic_hints=semantic_hints, **plan['budgets'])
    if rebuilt['plan_id'] != plan['plan_id']:
        raise ValueError('plan_mismatch: 输入与计划的内容地址不一致')
    # 只认重建出的计划：传入计划被篡改的字段（如 variants/开关）不参与执行。
    return rebuilt


def _merge_second_hop(first, seeds, edges, pool, budgets):
    """受限二跳：第一跳邻居作第二跳种子再走一次 R04，按合并纪律归并。纯函数。

    合并纪律（大声登记，不静默）：
    - 第二跳种子只从第一跳 paths 的邻居构造；池里没有详情的邻居不扩，登记
      second_hop_seed_unavailable（无法核对有效性与根源，宁可少扩）。
    - 第二跳路径指向种子/第一跳邻居（或已收二跳邻居）的剔除：防环防重复，revisit。
    - 与种子/第一跳同根源的归并：同根源转述不算独立证据（§5.4），same_root。
    - 每条保留的二跳路径带 via={first_hop_path_id, intermediate_id}，path_id 由内容
      （含 via）决定；hops=2。invalidated/unknown_neighbor 由两次 R04 真实承接。
    - 全局预算 max_expanded + max_second_expanded 有界，超限大声 dropped(budget)。
    降级只在第一跳承接一次（同一份 degradation 不重复计数），合并结果原样回显。
    """
    prov = first.get('provenance') or {}
    pool_index = rp._index(pool)
    first_paths = first.get('paths') or []
    dropped = dict(prov.get('dropped') or {})
    deduped = list(prov.get('deduped') or [])

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    # 第二跳种子：第一跳邻居 ∩ 池详情（排序确定；没有详情的不扩，大声登记）。
    neighbor_ids = sorted({_text(p.get('object_id')) for p in first_paths})
    first_path_by_neighbor = {_text(p.get('object_id')): _text(p.get('path_id'))
                              for p in first_paths}
    second_seeds, unavailable = [], []
    for nid in neighbor_ids:
        row = pool_index.get(nid)
        if row is None:
            drop('second_hop_seed_unavailable')
            unavailable.append(nid)
            continue
        second_seeds.append(row)
    second = rp.expand_paths(second_seeds, edges, items=pool, degradation=None,
                             max_degree=budgets['max_second_degree'],
                             max_expanded=budgets['max_second_expanded'])
    second_prov = second.get('provenance') or {}
    deduped.extend(copy.deepcopy(second_prov.get('deduped') or []))
    for reason, count in (second_prov.get('dropped') or {}).items():
        dropped[reason] = dropped.get(reason, 0) + count

    # 跨跳根源登记：可用种子 + 第一跳邻居（复用 R06 的同口径收集，失效单调取最严）。
    root_owner, visited = {}, {}
    for row in re6._collect(first, pool, seeds):
        oid = _text(row.get('object_id'))
        visited[oid] = True
        for root in rp._roots(row):
            root_owner.setdefault(root, oid)

    global_cap = budgets['max_expanded'] + budgets['max_second_expanded']
    kept_second = []
    kept_total = len(first_paths)
    for path in sorted(second.get('paths') or [],
                       key=lambda p: (_text(p.get('object_id')), _text(p.get('path_id')))):
        nid = _text(path.get('object_id'))
        sid = _text(path.get('seed_id'))
        via = {'first_hop_path_id': first_path_by_neighbor.get(sid, ''),
               'intermediate_id': sid}
        if not via['first_hop_path_id']:
            # 防御：第二跳种子必然来自第一跳邻居；查不到链式出处就不扩，大声登记。
            drop('second_hop_seed_unavailable')
            continue
        if nid in visited:
            deduped.append({'object_id': nid, 'merged_into': nid, 'reason': 'revisit',
                            'root_source_ids': [], 'via': copy.deepcopy(via)})
            drop('revisit')
            continue
        shared = sorted(r for r in rp._roots(path) if r in root_owner)
        if shared:
            deduped.append({'object_id': nid, 'merged_into': root_owner[shared[0]],
                            'reason': 'same_root', 'root_source_ids': shared,
                            'via': copy.deepcopy(via)})
            drop('same_root')
            continue
        if kept_total >= global_cap:
            drop('budget')
            continue
        merged_path = copy.deepcopy(path)
        merged_path['hops'] = 2
        merged_path['via'] = via
        # path_id 内容寻址含 via：同一条二跳路径永远同一个 ID，可回归对账。
        merged_path['path_id'] = 'path_' + mm.digest(_canonical(
            [path.get('path_id'), via['first_hop_path_id'], via['intermediate_id']]))[:14]
        kept_second.append(merged_path)
        kept_total += 1
        visited[nid] = True
        for root in rp._roots(merged_path):
            root_owner.setdefault(root, nid)

    all_paths = list(first_paths) + kept_second
    # status 口径沿用 R04：归并/失效是正常认识论行为，只有预算与「想扩但核对不了」
    # 才 partial；降级原样承接，绝不冒充充分。
    if first.get('degradation'):
        status = 'degraded'
    elif not all_paths:
        status = 'not_found'
    elif any(dropped.get(r) for r in ('degree_cap', 'expansion_cap', 'unknown_neighbor',
                                      'malformed_edge', 'budget',
                                      'second_hop_seed_unavailable')):
        status = 'partial'
    else:
        status = 'sufficient'
    return {
        'status': status,
        'paths': all_paths,
        'degradation': copy.deepcopy(first.get('degradation') or []),
        'provenance': {
            'candidates': prov.get('candidates', 0),
            'expanded': len(all_paths),
            'deduped': deduped,
            'dropped': dropped,
            'budget': dict(budgets, global_expanded=global_cap),
            'pool_size': prov.get('pool_size', len(pool_index)),
            'edge_count': prov.get('edge_count', 0),
            'hops': 2,
            'second_hop': {'seed_ids': [_text(r.get('object_id')) for r in second_seeds],
                           'unavailable': unavailable,
                           'expanded': len(second.get('paths') or []),
                           'kept': len(kept_second)},
            'limitations': LIMITATIONS,
        },
    }


def _run_variant(plan, variant_id, prepared):
    """逐变体执行：一跳走 R05 resolve_ripple（与 R07 逐字同链路），二跳走两次 R04
    expand_paths + 合并 + R05 assess_expansion；渲染一律走 R06 render_evidence。
    未启用的变体如实返回 disabled，不假装跑过。"""
    definition = next(v for v in plan['variants'] if v['variant_id'] == variant_id)
    if not definition['enabled']:
        return {'variant_id': variant_id, 'definition': copy.deepcopy(definition),
                'state': 'disabled'}
    pool, seeds = prepared['pool'], prepared['seeds']
    edges = prepared['edges']
    if definition['changes'].get('vector_edges'):
        # 真实边 + 校验后向量边合并；遍历排序仍由 R04 按 type/edge_id/a/b 决定。
        edges = edges + prepared['vector_edges']
    budgets = plan['budgets']
    slots = tuple(plan['required_slots'])
    hints = _hints_of(plan)
    if definition['changes'].get('second_hop'):
        first = rp.expand_paths(seeds, edges, items=pool, degradation=plan['degradation'],
                                max_expanded=budgets['max_expanded'],
                                max_degree=budgets['max_degree'])
        merged = _merge_second_hop(first, seeds, edges, pool, budgets)
        resolved = rc.assess_expansion(merged, seeds=seeds, items=pool,
                                       required_slots=slots, semantic_hints=hints)
    else:
        resolved = rc.resolve_ripple(seeds, edges, items=pool,
                                     degradation=plan['degradation'],
                                     required_slots=slots, semantic_hints=hints,
                                     max_expanded=budgets['max_expanded'],
                                     max_degree=budgets['max_degree'])
    # 清单上下文绑定 plan_id/variant_id/now：供审计对账，R06 不解释它、不用它判定。
    rendered = re6.render_evidence(resolved, items=pool, seeds=seeds,
                                   context={'plan_id': plan['plan_id'],
                                            'variant_id': variant_id, 'now': plan['now']})
    return {'variant_id': variant_id, 'definition': copy.deepcopy(definition),
            'state': 'executed', 'resolved': resolved, 'rendered': rendered}


def execute(plan, *, seeds, edges, vector_edges=None, items=None, degradation=None,
            semantic_hints=None):
    """按 VARIANT_IDS 顺序执行全部变体，返回逐变体结果列表。纯函数，不读库不写资产。

    执行前按同一输入重建计划并核对 plan_id，不一致大声拒绝；执行只认重建出的定义。
    启用的变体返回 {variant_id, definition, state='executed', resolved, rendered}
    （resolved/rendered 是 R05/R06 的真实输出，不改判）；未启用的变体返回
    {variant_id, definition, state='disabled'}，不带 resolved/rendered，不假装跑过。
    """
    rebuilt = _rebuild(plan, seeds, edges, vector_edges, items, degradation, semantic_hints)
    prepared = _prepare(seeds, edges, vector_edges, items)
    return [_run_variant(rebuilt, vid, prepared) for vid in VARIANT_IDS]


def _diff_selected(full_rows, variant_rows):
    full = {_text(r.get('object_id')): r for r in full_rows}
    variant = {_text(r.get('object_id')): r for r in variant_rows}
    added, removed, changed = [], [], []
    for oid in sorted(set(variant) - set(full)):
        entry = copy.deepcopy(variant[oid])
        added.append({'diff_reason': 'selected_added', 'object_id': oid,
                      'reason_code': _text(entry.get('reason')),
                      'sha16': _text(entry.get('sha16')), 'entry': entry})
    for oid in sorted(set(full) - set(variant)):
        entry = copy.deepcopy(full[oid])
        removed.append({'diff_reason': 'selected_removed', 'object_id': oid,
                        'reason_code': _text(entry.get('reason')),
                        'sha16': _text(entry.get('sha16')), 'entry': entry})
    for oid in sorted(set(full) & set(variant)):
        if _canonical(full[oid]) == _canonical(variant[oid]):
            continue
        fields = sorted(k for k in set(full[oid]) | set(variant[oid])
                        if _canonical(full[oid].get(k)) != _canonical(variant[oid].get(k)))
        changed.append({'diff_reason': 'selected_changed', 'object_id': oid,
                        'reason_code': _text(variant[oid].get('reason')),
                        'sha16': _text(variant[oid].get('sha16')),
                        'changed_fields': fields,
                        'full': copy.deepcopy(full[oid]),
                        'variant': copy.deepcopy(variant[oid])})
    return {'added': added, 'removed': removed, 'changed': changed}


def _identity(entry):
    """排除条目的稳定身份键（R07 同口径）：聚合计数条目 object_id 为空，靠
    reason_code 区分；count 不进键，计数变化才能被识别成 changed 而不是 removed+added。"""
    return tuple(_text(entry.get(k)) for k in
                 ('category', 'object_id', 'reason_code', 'slot',
                  'left_id', 'right_id', 'detail'))


def _diff_excluded(full_rows, variant_rows, prefix):
    full = {_identity(r): r for r in full_rows}
    variant = {_identity(r): r for r in variant_rows}

    def base(entry):
        return {'object_id': _text(entry.get('object_id')),
                'category': _text(entry.get('category')),
                'reason_code': _text(entry.get('reason_code'))}

    added, removed, changed = [], [], []
    for key in sorted(set(variant) - set(full)):
        entry = copy.deepcopy(variant[key])
        added.append(dict(base(entry), diff_reason=prefix + '_added',
                          entry=entry, ref_sha16=_ref(entry)))
    for key in sorted(set(full) - set(variant)):
        entry = copy.deepcopy(full[key])
        removed.append(dict(base(entry), diff_reason=prefix + '_removed',
                            entry=entry, ref_sha16=_ref(entry)))
    for key in sorted(set(full) & set(variant)):
        if _canonical(full[key]) == _canonical(variant[key]):
            continue
        fields = sorted(k for k in set(full[key]) | set(variant[key])
                        if _canonical(full[key].get(k)) != _canonical(variant[key].get(k)))
        changed.append(dict(base(variant[key]), diff_reason=prefix + '_changed',
                            changed_fields=fields,
                            full=copy.deepcopy(full[key]),
                            variant=copy.deepcopy(variant[key]),
                            full_ref_sha16=_ref(full[key]),
                            variant_ref_sha16=_ref(variant[key])))
    return {'added': added, 'removed': removed, 'changed': changed}


def diff_manifests(full, variant):
    """两份 R06 渲染结果的结构差异（full=one_hop 基线）。纯函数，不打分不下结论。

    与 R07 逐字同口径：selected 按 object_id 逐条对照（带 R06 的 sha16 引用）；
    excluded 段装 missing/conflicting，dropped 段装 dropped/invalidated/budget
    （R04/R05 聚合与 R06 预算），按稳定身份键对照，计数变化记 changed。所有条目
    稳定排序、带稳定 diff_reason；原因码逐字来自 R05/R06 清单，不新造因果解释。
    """
    full = full if isinstance(full, dict) else {}
    variant = variant if isinstance(variant, dict) else {}
    fm = full.get('manifest') or {}
    vm = variant.get('manifest') or {}
    f_ex = fm.get('excluded') or []
    v_ex = vm.get('excluded') or []
    f_drop = [r for r in f_ex if _text(r.get('category')) in _DROP_CATEGORIES]
    v_drop = [r for r in v_ex if _text(r.get('category')) in _DROP_CATEGORIES]
    f_other = [r for r in f_ex if _text(r.get('category')) not in _DROP_CATEGORIES]
    v_other = [r for r in v_ex if _text(r.get('category')) not in _DROP_CATEGORIES]
    return {
        'status': {'full': _text(full.get('status')),
                   'variant': _text(variant.get('status'))},
        'counts': {'full': copy.deepcopy(fm.get('counts') or {}),
                   'variant': copy.deepcopy(vm.get('counts') or {})},
        'selected': _diff_selected(fm.get('selected') or [], vm.get('selected') or []),
        'excluded': _diff_excluded(f_other, v_other, 'excluded'),
        'dropped': _diff_excluded(f_drop, v_drop, 'dropped'),
    }


def build_review_packet(*, now, seeds, edges, vector_edges=None, items=None,
                        degradation=None, allow_vector_edges=False, allow_second_hop=False,
                        max_expanded=rp.MAX_EXPANDED, max_degree=rp.MAX_DEGREE,
                        max_second_degree=MAX_SECOND_DEGREE,
                        max_second_expanded=MAX_SECOND_EXPANDED,
                        required_slots=rc.SLOTS, semantic_hints=None):
    """一次调用产出人工评审包：计划绑定 + 各变体清单 + 以 one_hop 为基线的差异 + 限制声明。

    输入与 build_plan 相同。评审包只带 R06 清单（ID/reason_code/sha16），不带场景
    原文；judgement 永远 None——「向量/二跳是否真的改变选择」由人工评审标注，本契约
    不打分、不下结论、不伪造真实样本。authorization=manual_review、organic=not_observed、
    expression_control=False：默认全关的隔离契约，不是真实链路实验。
    """
    plan = build_plan(now=now, seeds=seeds, edges=edges, vector_edges=vector_edges,
                      items=items, degradation=degradation,
                      allow_vector_edges=allow_vector_edges, allow_second_hop=allow_second_hop,
                      max_expanded=max_expanded, max_degree=max_degree,
                      max_second_degree=max_second_degree,
                      max_second_expanded=max_second_expanded,
                      required_slots=required_slots, semantic_hints=semantic_hints)
    runs = execute(plan, seeds=seeds, edges=edges, vector_edges=vector_edges, items=items,
                   degradation=degradation, semantic_hints=semantic_hints)
    baseline = next(r for r in runs if r['variant_id'] == 'one_hop')
    variants, diffs, executed, disabled = [], [], [], []
    for run in runs:
        if run['state'] == 'disabled':
            disabled.append(run['variant_id'])
            variants.append({'variant_id': run['variant_id'],
                             'definition': run['definition'], 'state': 'disabled',
                             'status': None, 'counts': {}, 'manifest': None})
            continue
        executed.append(run['variant_id'])
        rendered = run['rendered']
        variants.append({'variant_id': run['variant_id'], 'definition': run['definition'],
                         'state': 'executed', 'status': rendered['status'],
                         'counts': copy.deepcopy(rendered['manifest']['counts']),
                         'manifest': rendered['manifest']})
        if run['variant_id'] != 'one_hop':
            diffs.append(dict({'variant_id': run['variant_id'], 'baseline': 'one_hop'},
                              **diff_manifests(baseline['rendered'], rendered)))
    return {
        'plan_id': plan['plan_id'],
        'algorithm_version': ALGORITHM_VERSION,
        'now': plan['now'],
        'variants': variants,
        'diffs': diffs,
        'judgement': None,
        'authorization': REVIEW_AUTHORIZATION,
        'organic': REVIEW_ORGANIC,
        'expression_control': False,
        'provenance': {'plan': copy.deepcopy(plan),
                       'executed': executed, 'disabled': disabled},
        'limitations': LIMITATIONS,
    }