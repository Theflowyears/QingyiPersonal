# -*- coding: utf-8 -*-
"""R07 关联记忆保留集消融（ripple 轨 / V02 保留集 + R04/R05/R06 链路之上的只读对照）。

§9 消融对照要求「若内核只多输出装饰性文字则不算数」：关联记忆是否真的改变了
选择，必须能被对照实验核对。本模块在**固定的 V02 保留集**上，对照「完整关联」
与「去掉某一环」的证据场景差异，逐条给出稳定 reason_code 与可追溯 ID，产出
**可供人工评审的消融实验包**。R07 是 manual_review 任务：契约可执行 ≠ 真实
消融已做。真实消融需真实保留集与真实样本，本契约只证明可执行；organic 口径
not_observed、authorization 口径 manual_review（与 V02 相同），本模块不下
「关联有用/没用」的结论，judgement 永远留空给人判。

设计理由与硬边界（与 R01/R02/R04/R05/R06/V02 同一范式）：
- 纯函数、真只读。不 import retrieval_objects/recall_scoring/association_edges
  （R01/R03 只观察静态锁禁止 bot 模块把它们接进链路），不打开任何库：V02 保留集、
  种子（R03 初召回结果）、边（R02 投影结果）与降级全部由调用方传入。无时钟无随机，
  now 是调用方必填的逻辑时间，只回显进计划供审计绑定，不参与任何判定。
- 复用而非重造。逐变体执行直接调 R05 resolve_ripple（内含 R04 受限一跳与 M04
  裁决）与 R06 render_evidence，不复制平行实现；冲突/降级/失效/缺槽语义完全承接
  R05/R06：不重新裁决、不长第二套矛盾引擎、invalidated 绝不复活、缺失不补造、
  degraded 不冒充充分。预算纪律沿用 R04/R06：超限大声 dropped(reason=budget 等)，
  绝不静默截断。
- 变体集合固定可枚举（VARIANT_IDS），只用真实存在的开关：R04 的 max_expanded/
  max_degree、R05 的 required_slots、R06 的 max_scenes/max_entries、以及「边置空」。
  每个变体带明确定义（changes 改了什么、keeps 没改什么）。R02 的边投影预算变体
  需要重跑 R02，本契约把 edges 当给定输入，不重投影；R03 初召回不重跑，种子由
  调用方给定——不发明不存在的开关。
- 固定保留集口径。items 池 = V02 retained_items（原样深拷贝，legacy/invalidated
  不洗层）；种子不在保留集的大声登记 seed_not_retained 后剔除；指向已剔除记忆的
  边大声登记 endpoint_not_retained 后剔除；其余端点（来源事件/人物/池外对象）
  交给 R04 的真实 unknown_neighbor 语义，不冒充认识也不补造。
- 计划内容寻址。plan_id 由计划核心（now/保留集绑定/预算/变体定义/过滤登记等）
  的 A03 digest 决定；执行前按同一输入重建计划并核对 plan_id，不一致大声拒绝，
  执行只认重建出的定义，篡改计划字段不影响结果。
- 差异报告只做结构对照：selected/excluded/dropped 逐条 added/removed/changed，
  稳定排序、稳定 diff_reason、带 sha16 引用（selected 沿用 R06 的 restatement
  sha16，excluded/dropped 用条目内容哈希）。excluded 段装 missing/conflicting，
  dropped 段装 dropped/invalidated/budget（R04/R05 聚合与 R06 预算），不重不漏。
  差异不解释因果、不打分——「关联是否改变选择」由人工评审判。
- 评审包只带 ID/reason_code/sha16 引用与 R06 清单（A03 口径：只记 ID、长度和
  哈希，不落原文）；restatement/原句留在 R06 场景里，不进本包。不伪造真实样本，
  不伪造结论字段。不做多跳扩散（R08）、不做真实注入接线（C04）、不调 LLM。

已知边界（如实记录，不硬凑）：R04 已把失效邻居/未知端点挡在 paths 之外，R05/R06
只保留 dropped 聚合计数，本模块的差异报告按聚合计数承接（count 变化记 changed），
不伪造逐对象明细；无根源的旧条目无法按根源归并，沿用 R04/R05 的如实声明。
"""
import copy
import json

import memory_manifest as mm
import ripple_conflicts as rc
import ripple_evidence as re6
import ripple_paths as rp

# 变体集合固定可枚举：full 是基线，其余各去掉一环。顺序即执行与差异报告顺序。
VARIANT_IDS = ('full', 'no_edges', 'no_expansion', 'half_expansion',
               'half_degree', 'half_render', 'no_slot_check')
# 变体定义里「改了什么/没改什么」的开关全集（都是 R04/R05/R06 真实存在的开关）。
KNOBS = ('edges', 'seeds', 'pool', 'max_expanded', 'max_degree',
         'max_scenes', 'max_entries', 'required_slots', 'degradation',
         'semantic_hints')
# 差异条目自身的稳定结构码（描述结构变化，不是因果结论，更不是分数）。
DIFF_REASONS = ('selected_added', 'selected_removed', 'selected_changed',
                'excluded_added', 'excluded_removed', 'excluded_changed',
                'dropped_added', 'dropped_removed', 'dropped_changed')
# 保留集过滤的稳定 reason_code：缺什么就喊什么，不静默丢。
FILTER_REASONS = ('seed_not_retained', 'seed_malformed', 'endpoint_not_retained')
# 评审口径与 V02 相同：manual_review 任务，organic 留 not_observed。
REVIEW_AUTHORIZATION = 'manual_review'
REVIEW_ORGANIC = 'not_observed'
# dropped 差异段承接的排除分类（R04/R05 聚合 + R06 预算）；其余归 excluded 段。
_DROP_CATEGORIES = ('dropped', 'invalidated', 'budget')

LIMITATIONS = (
    'R07 是 manual_review 的可执行契约：本模块只产出可供人工评审的消融实验包，'
    '真实消融需真实保留集与真实样本，契约可执行不等于真实消融已做；organic 口径 '
    'not_observed，authorization 口径 manual_review，与 V02 相同。',
    '不下「关联有用/没用」的结论、不打分：差异只是 selected/excluded/dropped 的'
    '结构对照（added/removed/changed），judgement 留空，因果与价值由人工评审判。',
    '变体只用 R04/R05/R06 真实开关（边置空/max_expanded/max_degree/required_slots/'
    'max_scenes/max_entries）：R02 边投影预算变体需重跑 R02，本契约把 edges 当给定'
    '输入不重投影；R03 初召回不重跑，种子由调用方给定；不发明不存在的开关。',
    '冲突/降级/失效语义完全承接 R05/R06（M04 裁决）：不重新裁决、不长第二套矛盾'
    '引擎、不伪造 verdict；invalidated 绝不复活、缺失不补造、degraded 不冒充充分；'
    '失效邻居/未知端点按 R05 dropped 聚合计数承接，不伪造逐对象明细。',
    '多跳扩散是 R08（需人工评审），真实注入接线是 C04，真实 LLM 渲染在下游：'
    '本模块不扩边、不遍历、不生成聊天文本、不接进回复链路。',
    '纯函数只读：不 import retrieval_objects/recall_scoring/association_edges，'
    '不打开任何库；now 由调用方必填（逻辑时间，只回显不判定）；预算超限沿用 '
    'R04/R06 大声登记 dropped(reason=budget 等)，绝不静默截断。',
    '评审包只带 ID/reason_code/sha16 引用与 R06 清单（A03 口径：只记 ID、长度和'
    '哈希，不落原文）；restatement/原句留在 R06 场景，不进本包；差异报告以 full '
    '为基线逐变体对照，不做变体间两两对照。',
)


def _text(value):
    return str(value or '').strip()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _ref(entry):
    """排除/预算条目的内容哈希引用：清单不落原文，评审仍能逐条对账。"""
    return mm.digest(_canonical(entry))[:16]


def _hints_of(plan):
    return {(left, right): hint
            for left, right, hint in (plan.get('semantic_hints') or [])}


def _prepare(retention, seeds, edges):
    """固定保留集口径的输入整理（只收集/过滤，不做任何裁决）。

    池 = retained_items 原样深拷贝（V02 不洗层，这里也不洗）；种子复用 R04 _index
    排序去重；指向已剔除记忆的边剔除并大声登记，其余端点交给 R04 真实语义。
    """
    retention = retention if isinstance(retention, dict) else {}
    pool = copy.deepcopy([r for r in (retention.get('retained_items') or [])
                          if isinstance(r, dict)])
    pool.sort(key=lambda r: _text(r.get('object_id')))
    retained = {_text(r.get('object_id')) for r in pool}
    removed = {_text(x) for x in (retention.get('removed') or []) if _text(x)}

    candidates, seed_filter = [], []
    for row in (seeds or []):
        if not isinstance(row, dict):
            seed_filter.append({'object_id': '', 'reason_code': 'seed_malformed'})
            continue
        oid = _text(row.get('object_id'))
        if oid and oid in retained:
            candidates.append(row)
        else:
            seed_filter.append({'object_id': oid, 'reason_code': 'seed_not_retained'})
    index = rp._index(candidates)
    kept_seeds = [copy.deepcopy(index[oid]) for oid in sorted(index)]
    seed_filter.sort(key=lambda x: (x['reason_code'], x['object_id']))

    kept_edges, edge_filter = [], []
    for row in (edges or []):
        if not isinstance(row, dict):
            # 坏边不在这里静默丢：交给 R04 大声登记 malformed_edge。
            kept_edges.append(row)
            continue
        a, b = _text(row.get('a')), _text(row.get('b'))
        hit = sorted(x for x in {a, b} if x and x in removed)
        if hit:
            edge_filter.append({'edge_id': _text(row.get('edge_id')),
                                'reason_code': 'endpoint_not_retained',
                                'endpoints': hit})
        else:
            kept_edges.append(copy.deepcopy(row))
    kept_edges.sort(key=lambda e: (_text(e.get('type')) if isinstance(e, dict) else '',
                                   _text(e.get('edge_id')) if isinstance(e, dict) else '',
                                   _text(e.get('a')) if isinstance(e, dict) else '',
                                   _text(e.get('b')) if isinstance(e, dict) else ''))
    edge_filter.sort(key=lambda x: (x['reason_code'], x['edge_id'], tuple(x['endpoints'])))
    return pool, kept_seeds, kept_edges, seed_filter, edge_filter


def _variant_definitions(budgets):
    """固定变体集合：每个变体写清改了什么（changes）、没改什么（keeps）。

    减半一律向下取整（预算是资源上限不是质量分，0 是「这条都不要」）。
    """
    expanded, degree = budgets['max_expanded'], budgets['max_degree']
    scenes, entries = budgets['max_scenes'], budgets['max_entries']
    specs = (
        ('full', {},
         '完整关联基线：种子/边/保留集/预算/槽位核对全部原样，不改任何开关。'),
        ('no_edges', {'edges': []},
         '去掉全部联想边（edges 置空）：种子/保留集/预算/槽位核对不变，'
         'R04 无边可走，只剩种子自身作证据。'),
        ('no_expansion', {'max_expanded': 0},
         '禁一跳：边保留但全局扩散预算为 0（R04 max_expanded 真实开关），'
         '每次尝试扩边大声登记 expansion_cap，不静默。'),
        ('half_expansion', {'max_expanded': expanded // 2},
         '全局扩散预算减半（向下取整，R04 max_expanded），其余输入与开关不变。'),
        ('half_degree', {'max_degree': degree // 2},
         '每种子度数上限减半（向下取整，R04 max_degree），其余输入与开关不变。'),
        ('half_render', {'max_scenes': scenes // 2, 'max_entries': entries // 2},
         'R06 渲染预算减半（向下取整，max_scenes/max_entries）：超限逐条大声 '
         'dropped(reason=budget)，其余输入与开关不变。'),
        ('no_slot_check', {'required_slots': []},
         '关闭 R05 槽位核对（required_slots=()）：缺槽不再登记，'
         '选择与渲染口径不变，缺依据的对象不再被标 gapped。'),
    )
    return [{'variant_id': vid, 'changes': copy.deepcopy(changes),
             'keeps': [k for k in KNOBS if k not in changes], 'definition': text}
            for vid, changes, text in specs]


def build_plan(retention, *, seeds, edges, now, degradation=None, context=None,
               required_slots=rc.SLOTS, semantic_hints=None,
               max_expanded=rp.MAX_EXPANDED, max_degree=rp.MAX_DEGREE,
               max_scenes=re6.MAX_SCENES, max_entries=re6.MAX_ENTRIES):
    """消融计划纯函数：固定保留集 + 固定变体集合，内容寻址 plan_id。

    retention: V02 retention_set() 的输出（retained_items 是固定池；fingerprint/
               retained/removed 等绑定信息原样回显供评审核对）。
    seeds:     R03 初召回选中的 items（调用方给定，本契约不重跑 R03）；不在保留集
               的大声登记 seed_not_retained 后剔除。
    edges:     R02 project_edges() 的边（调用方给定，不重投影）；指向已剔除记忆
               的边大声登记 endpoint_not_retained 后剔除。
    now:       必填的逻辑时间（调用方给定，无时钟），只回显进计划供审计绑定。
    degradation/context/semantic_hints: 原样承接（深拷贝），所有变体共用，不是
               消融开关。required_slots 未知槽位大声拒绝（与 R05 同口径）。
    max_*:     基线预算，原样转交 R04/R06；减半变体由基线向下取整推出。

    返回计划 dict：plan_id 由核心内容的 A03 digest 决定；不含任何聊天原文
    （池/种子/边只登记 ID），执行时按同一输入重建并核对 plan_id。
    """
    if now is None:
        raise ValueError('now_required: 逻辑时间必须由调用方给出')
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError('now_must_be_number: %r' % (now,))
    slots = tuple(required_slots or ())
    for slot in slots:
        if slot not in rc.SLOTS:
            raise ValueError('unknown required slot: %s' % slot)
    budgets = {'max_expanded': max(0, int(max_expanded)),
               'max_degree': max(0, int(max_degree)),
               'max_scenes': max(0, int(max_scenes)),
               'max_entries': max(0, int(max_entries))}
    retention = retention if isinstance(retention, dict) else {}
    pool, kept_seeds, kept_edges, seed_filter, edge_filter = _prepare(retention, seeds, edges)

    hints = {}
    for (left, right), hint in (semantic_hints or {}).items():
        hints[(_text(left), _text(right))] = copy.deepcopy(hint)
    binding = {key: copy.deepcopy(retention.get(key))
               for key in ('fingerprint', 'mode', 'policy', 'keep_unrooted',
                           'requested_roots', 'retained', 'removed',
                           'removed_reasons', 'retained_count', 'removed_count',
                           'unknown_roots', 'problems')}
    core = {
        'now': now,
        'retention': binding,
        'pool_ids': [_text(r.get('object_id')) for r in pool],
        'seed_ids': [_text(r.get('object_id')) for r in kept_seeds],
        'edge_ids': [_text(e.get('edge_id')) if isinstance(e, dict) else ''
                     for e in kept_edges],
        'seed_filter': seed_filter,
        'edge_filter': edge_filter,
        'budgets': budgets,
        'required_slots': list(slots),
        'semantic_hints': [[left, right, hints[(left, right)]]
                           for left, right in sorted(hints)],
        'degradation': [dict(d) for d in (degradation or []) if isinstance(d, dict)],
        'context': copy.deepcopy(context) if isinstance(context, dict) else {},
        'variants': _variant_definitions(budgets),
    }
    return dict(core, plan_id='plan_' + mm.digest(_canonical(core))[:14])


def _rebuild(plan, retention, seeds, edges):
    """执行前按同一输入重建计划并核对内容地址：不一致大声拒绝，不悄悄跑偏。"""
    plan = plan if isinstance(plan, dict) else {}
    for key in ('plan_id', 'now', 'budgets', 'required_slots', 'semantic_hints',
                'degradation', 'context', 'variants'):
        if key not in plan:
            raise ValueError('invalid_plan: missing %s' % key)
    rebuilt = build_plan(retention, seeds=seeds, edges=edges, now=plan['now'],
                         degradation=plan['degradation'], context=plan['context'],
                         required_slots=plan['required_slots'],
                         semantic_hints=_hints_of(plan), **plan['budgets'])
    if rebuilt['plan_id'] != plan['plan_id']:
        raise ValueError('plan_mismatch: 输入与计划的内容地址不一致')
    # 只认重建出的计划：传入计划被篡改的字段（如 variants）不参与执行。
    return rebuilt


def _run_variant(plan, variant_id, prepared):
    """逐变体执行：复用 R05 resolve_ripple（含 R04/M04）与 R06 render_evidence。"""
    pool, kept_seeds, kept_edges = prepared[0], prepared[1], prepared[2]
    definition = next(v for v in plan['variants'] if v['variant_id'] == variant_id)
    changes = definition['changes']
    budgets = dict(plan['budgets'])
    budgets.update({k: v for k, v in changes.items() if k in budgets})
    slots = tuple(changes['required_slots']) if 'required_slots' in changes \
        else tuple(plan['required_slots'])
    edges_v = [] if 'edges' in changes else kept_edges
    resolved = rc.resolve_ripple(kept_seeds, edges_v, items=pool,
                                 degradation=plan['degradation'],
                                 required_slots=slots,
                                 semantic_hints=_hints_of(plan),
                                 max_expanded=budgets['max_expanded'],
                                 max_degree=budgets['max_degree'])
    rendered = re6.render_evidence(resolved, items=pool, seeds=kept_seeds,
                                   context=plan['context'],
                                   max_scenes=budgets['max_scenes'],
                                   max_entries=budgets['max_entries'])
    return {'variant_id': variant_id, 'definition': copy.deepcopy(definition),
            'resolved': resolved, 'rendered': rendered}


def execute_variant(plan, variant_id, retention, *, seeds, edges):
    """执行单个变体。plan 必须与输入内容地址一致；未知变体大声拒绝。纯函数。"""
    if variant_id not in VARIANT_IDS:
        raise ValueError('unknown_variant: %r (allowed: %s)'
                         % (variant_id, ', '.join(VARIANT_IDS)))
    rebuilt = _rebuild(plan, retention, seeds, edges)
    return _run_variant(rebuilt, variant_id, _prepare(retention, seeds, edges))


def execute_plan(plan, retention, *, seeds, edges):
    """按 VARIANT_IDS 顺序执行全部变体，返回逐变体 {variant_id, definition,
    resolved, rendered}。resolved/rendered 是 R05/R06 的真实输出，不改判。"""
    rebuilt = _rebuild(plan, retention, seeds, edges)
    prepared = _prepare(retention, seeds, edges)
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
    """排除条目的稳定身份键：聚合计数条目 object_id 为空，靠 reason_code 区分；
    count 不进键，计数变化才能被识别成 changed 而不是 removed+added。"""
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
    """两份 R06 渲染结果的结构差异（full 为基线）。纯函数，不打分不下结论。

    selected 按 object_id 逐条对照（带 R06 的 sha16 引用）；excluded 段装
    missing/conflicting，dropped 段装 dropped/invalidated/budget（R04/R05 聚合
    与 R06 预算），按稳定身份键对照，计数变化记 changed。所有条目稳定排序、
    带稳定 diff_reason；原因码逐字来自 R05/R06 清单，不新造因果解释。
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


def build_review_packet(retention, *, seeds, edges, now, degradation=None,
                        context=None, required_slots=rc.SLOTS, semantic_hints=None,
                        max_expanded=rp.MAX_EXPANDED, max_degree=rp.MAX_DEGREE,
                        max_scenes=re6.MAX_SCENES, max_entries=re6.MAX_ENTRIES):
    """一次调用产出人工评审包：计划 + 各变体清单 + 以 full 为基线的差异 + 限制声明。

    输入与 build_plan 相同。评审包只带 R06 清单（ID/reason_code/sha16），不带
    场景原文；review.judgement 永远 None——「关联是否真的改变选择」由人工评审
    标注，本契约不打分、不下结论、不伪造真实样本。
    """
    plan = build_plan(retention, seeds=seeds, edges=edges, now=now,
                      degradation=degradation, context=context,
                      required_slots=required_slots, semantic_hints=semantic_hints,
                      max_expanded=max_expanded, max_degree=max_degree,
                      max_scenes=max_scenes, max_entries=max_entries)
    runs = execute_plan(plan, retention, seeds=seeds, edges=edges)
    baseline = runs[0]['rendered']
    variants = [{'variant_id': run['variant_id'], 'definition': run['definition'],
                 'status': run['rendered']['status'],
                 'manifest': run['rendered']['manifest']} for run in runs]
    diffs = [dict({'variant_id': run['variant_id'], 'baseline': 'full'},
                  **diff_manifests(baseline, run['rendered'])) for run in runs[1:]]
    return {
        'plan': plan,
        'variants': variants,
        'diffs': diffs,
        'review': {'authorization': REVIEW_AUTHORIZATION,
                   'organic': REVIEW_ORGANIC, 'judgement': None, 'notes': None},
        'limitations': LIMITATIONS,
    }