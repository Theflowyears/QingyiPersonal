# -*- coding: utf-8 -*-
"""R06 证据场景渲染与清单（统一方案 ripple 轨 / R05 裁决输出之上的只读渲染）。

R05 ripple_conflicts 的 docstring 明确「证据场景渲染与清单是 R06，本模块不渲染、
不生成话术」；本模块接住这条 carve-out：把 R05 已经裁决/登记好的证据对象组织成
**结构化证据场景**（按根源代表分组，每条带 object_id、来源层 store、根源代表、
verdict 引用），并产出本次渲染的**完整清单**（入选了什么/为什么、排除了什么、
降级如实呈现），供将来注入证据层（§8.3 第 3 段「本轮最相关的真实经历与资料」）使用。

设计理由与硬边界（与 R01/R02/R04/R05 同一范式）：
- 纯函数、真只读。不 import retrieval_objects/recall_scoring（R01/R03 只观察静态锁
  禁止 bot 模块把它们接进链路），不打开任何库：R05 结果、items 池、种子与清单上下文
  全部由调用方传入。不在模块顶层冻路径，无时钟无随机。范围硬过滤由调用方负责
  （与 R02/R04/R05 同范式），R06 不重做。
- 渲染是结构化数据，不是人设话术，不生成聊天文本。场景条目只搬 R01 item 的真实字段
  （object_id/store/epistemic_kind/根源/主体/发生时间/有效性/摘述）与 R05 的 verdict 引用、
  缺槽登记，绝不补造缺失依据（§5.4），绝不把结构化引用润色成自然语言（真实 LLM 渲染在下游）。
- 只认 R05/M04 的裁决。不重新裁决、不长第二套矛盾引擎、不伪造 verdict：conflicting
  对象由 provenance.verdicts 里 verdict=contradiction 的记录认定，verdict 引用逐字深拷贝
  自 R05；disputed/deferred 原样引用，不冒充已解决也不冒充矛盾。
- 四种排除口径与 R05 逐字一致：missing（R05 missing_support 的 reason_code，逐槽）、
  conflicting（M04 reason_code，逐对象）、dropped/invalidated（R04/R05 的 dropped 聚合
  计数原样承接，invalidated_* 归 invalidated 类、其余归 dropped 类）；R06 自己的预算
  超限沿用 A03 的 budget 口径（逐对象）。invalidated 绝不复活入场景；conflicting/degraded
  绝不冒充充分证据；缺失不补造。
- 不做融合打分（R03，场景/清单不带 score，也不读 item.match.score_parts）、不做多跳
  扩散（R08，只渲染 R05 已给出的对象，不扩边不遍历）、不做真实注入接线（C04）。
- 确定性。候选按 object_id 排序，场景按 scene_key 排序、条目按 object_id 排序，
  排除清单按 (category, object_id, reason_code) 排序；scene_id 与清单内容哈希由 A03
  digest 决定。同输入同输出，只读深拷贝，不改输入。
- 预算。场景数 max_scenes、条目数 max_entries 是显式资源上限（不是质量分）：先按条目
  上限截、再按场景上限截，超限逐条大声登记 dropped(reason=budget)，绝不静默截断。

status 口径：degraded > conflicting > not_found > partial > sufficient。降级/矛盾原样
承接 R05（status 与 degradation/conflicts 都认），绝不冒充充分；not_found 指「一个可支撑
对象都没渲染出来」——R05 的 not_found 描述的是扩散结果（没有独立一跳），种子本身仍是
真实证据，可被渲染成 sufficient 场景；缺槽、预算超限或 R05 partial 都让结果 partial。

候选集合与 R05 assess_expansion 同口径重建（可用种子 + 一跳邻居，失效单调取最严），
保证 R06 渲染的对象正是 R05 裁决过的那批，不多渲染池里 R05 没见过的对象。重建只做
「收集」，复用 R04 的 _index/_roots 与 R05 的 _subject_of，不重定义任何裁决函数。

已知边界（如实记录，不硬凑）：R04 已把失效邻居/未知端点挡在 paths 之外，R05 只保留
它们的 dropped 聚合计数；R06 拿不到这些对象的逐对象 ID，于是按 R05 dropped 计数如实
承接（invalidated_* 归 invalidated 类），绝不伪造逐对象明细，也绝不把它们复活入场景。
"""
import copy

import memory_manifest as mm
import ripple_conflicts as rc
import ripple_paths as rp

# 与 R01/R05 同一 status 契约（直接复用 R05，保证逐字一致）。
STATUSES = rc.STATUSES
# 缺槽 reason_code 直接复用 R05，保证与 R05 口径逐字一致。
MISSING_REASONS = rc.MISSING_REASONS
# 排除口径分类（missing/dropped/invalidated/conflicting 与 R05 一致，budget 沿用 A03）。
CATEGORIES = ('missing', 'dropped', 'invalidated', 'conflicting', 'budget')
# 入选理由（R06 自己的「为什么入选」词表，描述支撑度，不是排除码也不是分数）：
#   grounded 五槽齐全；gapped 有缺槽但仍渲染（缺槽如实登记，不补造）。
SELECT_REASONS = ('grounded', 'gapped')
# 失效 reason_code 逐字沿用 R04/R05（不新造）；dropped 聚合里命中这些的归 invalidated 类。
INVALIDATED_REASONS = ('invalidated_seed', 'invalidated_neighbor', 'invalidated_candidate')
# 预算超限沿用 A03 memory_manifest 的 budget 口径。
BUDGET_REASON = 'budget'
# 显式资源上限（不是质量分）：场景数与条目数封顶，超限大声登记 dropped。
MAX_SCENES = 8
MAX_ENTRIES = 32

LIMITATIONS = (
    'R06 只在 R05 输出之上做证据场景渲染与清单：渲染是结构化数据，不是人设话术，'
    '不生成聊天文本；真实注入接线是 C04，本模块不接进回复链路。',
    '融合打分是 R03：场景与清单不带 score，也不读 item.match.score_parts，'
    '不排序打分、不长任何打分机器。',
    '多跳扩散是 R08（需人工评审）：本模块不扩边、不遍历，只渲染 R05 已给出的'
    '证据对象（可用种子 + 一跳邻居）。',
    '矛盾裁决完全复用 R05/M04 的结果：不重新裁决、不长第二套矛盾引擎、不伪造 '
    'verdict；verdict 引用逐字深拷贝自 provenance.verdicts，disputed/deferred 原样'
    '引用，不冒充已解决也不冒充矛盾。',
    'conflicting 对象不入场景（不冒充充分证据）；invalidated 绝不复活；degraded '
    '原样承接、绝不冒充充分；缺槽逐字登记、绝不补造填充（§5.4）。失效邻居/未知端点'
    '已被 R04 挡在 paths 之外，R06 按 R05 dropped 聚合计数如实承接，不伪造逐对象明细。',
    '真实 LLM 渲染（把结构化场景生成自然语言）不在本模块：场景是供将来证据层'
    '使用的结构化引用，文本生成属于下游。',
    '纯函数只读：不 import retrieval_objects/recall_scoring，不打开任何库；范围硬'
    '过滤由调用方负责（与 R02/R04/R05 同范式）；预算超限大声登记 dropped'
    '(reason=budget)，绝不静默截断。',
)


def _text(value):
    return str(value or '').strip()


def _scene_key(entry):
    """场景分组键：有根源按根源代表归组；无根源的旧条目无法归并，各自成组（如实）。"""
    rep = entry['root_representative']
    return rep if rep else 'object:' + entry['object_id']


def _collect(resolved, items, seeds):
    """与 R05 assess_expansion 同口径重建可用候选集合（只做收集，不做裁决）。

    复用 R04 _index（排序去重）与失效单调（池/种子两处登记不一致取最严）。返回按
    object_id 排序的可用 rows：可用种子 + 一跳邻居，失效对象绝不复活。失效邻居/未知
    端点已被 R04 挡在 paths 之外，这里看不到也不伪造，由 R05 dropped 聚合计数承接。
    """
    paths = resolved.get('paths') or []
    pool = rp._index(items)
    seed_rows = rp._index(seeds)
    known = dict(pool)
    for oid, row in seed_rows.items():
        prior = known.get(oid)
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
        for path in paths:
            sid = _text(path.get('seed_id'))
            if not sid or sid in rows:
                continue
            row = known.get(sid)
            if row is not None and row.get('validity') != 'invalidated':
                rows[sid] = row
    for path in paths:
        nid = _text(path.get('object_id'))
        if not nid or nid in rows:
            continue
        row = known.get(nid)
        if row is not None and row.get('validity') != 'invalidated':
            rows[nid] = row
    return [rows[oid] for oid in sorted(rows)]


def _entry(row, root_canon, verdicts, missing_by_oid):
    """R01 只读 item → 结构化场景条目。只搬真实字段，绝不补造缺槽/有效期/主体。

    根源代表取 R05 provenance.root_canon（不重算归并）；verdict 引用逐字深拷贝自
    R05 provenance.verdicts（涉及本对象的那些）；缺槽取 R05 missing_support 里本对象
    的登记（不补造）。主体复用 R05 _subject_of（M02 口径，不替 M03 编实体）。
    """
    oid = _text(row.get('object_id'))
    roots = rp._roots(row)
    rep = root_canon.get(roots[0], roots[0]) if roots else ''
    refs = [copy.deepcopy(v) for v in verdicts
            if _text(v.get('left_id')) == oid or _text(v.get('right_id')) == oid]
    slots = [{'slot': m.get('slot'), 'reason_code': m.get('reason_code')}
             for m in missing_by_oid.get(oid, [])]
    return {
        'object_id': oid,
        'object_kind': _text(row.get('object_kind')),
        'store': _text(row.get('store')),
        'epistemic_kind': _text(row.get('epistemic_kind')),
        'subject_ref': rc._subject_of(row),
        'root_source_ids': roots,
        'root_representative': rep,
        'occurred_at': row.get('occurred_at'),
        'validity': _text(row.get('validity')),
        'restatement': _text(row.get('restatement')),
        'missing_slots': slots,
        'verdict_refs': refs,
    }


def _status(degradation, conflict_records, scenes, missing, budget_drops, status_in):
    """degraded > conflicting > not_found > partial > sufficient（承接 R05，不冒充）。"""
    if degradation or status_in == 'degraded':
        return 'degraded'
    if conflict_records or status_in == 'conflicting':
        return 'conflicting'
    if not scenes and not budget_drops:
        return 'not_found'
    if missing or budget_drops or status_in == 'partial':
        return 'partial'
    return 'sufficient'


def render_evidence(resolved, *, items=None, seeds=None, context=None,
                    max_scenes=MAX_SCENES, max_entries=MAX_ENTRIES):
    """在 R05 resolve_ripple()/assess_expansion() 输出之上做证据场景渲染与清单。纯函数。

    resolved: R05 的输出 {status, paths, conflicts, missing_support, degradation,
              provenance}。降级/矛盾/缺槽/裁决/根源归并/dropped 全部原样承接（深拷贝），不改判。
    items:    全量 items 池（与传给 R05 的同一批），提供种子/邻居详情。不给池时指向
              池外端点无法核对，不渲染（R05 dropped 计数承接）。
    seeds:    传给 R05 的同一批种子。给了才能覆盖「没扩出路径的种子」；不给时从 paths
              的 seed_id 回查（与 R05 同口径，覆盖不到如实声明）。
    context:  调用方传入的清单上下文（如轮次/请求标识），原样深拷贝进清单供审计绑定；
              R06 不解释它、不用它做判定（无时钟无随机）。
    max_scenes/max_entries: 显式资源上限。先按条目上限截、再按场景上限截，超限逐条
              大声登记 dropped(reason=budget)，绝不静默截断。0 是「这条都不要」。

    返回 {status, scenes, manifest, degradation, provenance}。scenes 是按根源代表分组
    的结构化场景块；manifest 是完整清单（selected 入选+为什么、excluded 逐条稳定
    reason_code、degradation 如实、counts）；provenance 承接 R05 的缺槽/dropped/矛盾/
    裁决/根源归并并登记预算与 LIMITATIONS。conflicting/invalidated 不入场景，缺失不补造。
    """
    resolved = resolved if isinstance(resolved, dict) else {}
    max_scenes = max(0, int(max_scenes))
    max_entries = max(0, int(max_entries))
    context = copy.deepcopy(context) if isinstance(context, dict) else {}

    status_in = _text(resolved.get('status'))
    prov = resolved.get('provenance') or {}
    verdicts = prov.get('verdicts') or []
    root_canon = dict(prov.get('root_canon') or {})
    missing = copy.deepcopy(resolved.get('missing_support') or [])
    degradation = copy.deepcopy(resolved.get('degradation') or [])
    dropped_in = copy.deepcopy(prov.get('dropped') or {})
    conflicts_in = copy.deepcopy(resolved.get('conflicts') or [])

    # conflicting 只认 M04 verdict=contradiction（经 R05 provenance.verdicts），不自己判。
    conflict_records = [v for v in verdicts if v.get('verdict') == 'contradiction']
    conflict_ids = {_text(r.get(k)) for r in conflict_records for k in ('left_id', 'right_id')}
    conflict_ids.discard('')

    rows = _collect(resolved, items, seeds)

    missing_by_oid = {}
    for gap in missing:
        missing_by_oid.setdefault(_text(gap.get('object_id')), []).append(gap)

    renderable = [_entry(row, root_canon, verdicts, missing_by_oid)
                  for row in rows if _text(row.get('object_id')) not in conflict_ids]

    conflicting = []
    for record in conflict_records:
        left, right = _text(record.get('left_id')), _text(record.get('right_id'))
        for oid in (left, right):
            if oid:
                conflicting.append({'object_id': oid, 'category': 'conflicting',
                                    'reason_code': _text(record.get('reason_code')),
                                    'verdict': record.get('verdict'),
                                    'left_id': left, 'right_id': right})

    # 预算：先按条目上限截（按 scene_key, object_id 稳定序），再按场景上限截。
    ordered_entries = sorted(renderable, key=lambda e: (_scene_key(e), e['object_id']))
    kept = ordered_entries[:max_entries]
    budget_drops = [{'object_id': e['object_id'], 'category': 'budget',
                     'reason_code': BUDGET_REASON, 'detail': 'max_entries'}
                    for e in ordered_entries[max_entries:]]
    groups = {}
    for entry in kept:
        groups.setdefault(_scene_key(entry), []).append(entry)
    ordered_keys = sorted(groups)
    kept_keys = ordered_keys[:max_scenes]
    for key in ordered_keys[max_scenes:]:
        for entry in groups[key]:
            budget_drops.append({'object_id': entry['object_id'], 'category': 'budget',
                                 'reason_code': BUDGET_REASON, 'detail': 'max_scenes'})

    scenes, selected = [], []
    for key in kept_keys:
        entries = groups[key]
        rep = entries[0]['root_representative']
        roots = sorted({r for e in entries for r in e['root_source_ids']})
        scene_id = 'scene_' + mm.digest(key)[:14]
        scenes.append({'scene_id': scene_id, 'scene_key': key,
                       'root_representative': rep, 'root_source_ids': roots,
                       'entries': entries, 'entry_count': len(entries)})
        for entry in entries:
            selected.append({'object_id': entry['object_id'], 'scene_id': scene_id,
                             'reason': 'grounded' if not entry['missing_slots'] else 'gapped',
                             'store': entry['store'], 'root_representative': rep,
                             'missing_slots': copy.deepcopy(entry['missing_slots']),
                             'sha16': mm.digest(entry['restatement'])[:16]})

    # 排除清单：missing 逐槽、conflicting 逐对象、budget 逐对象（R06 自己），
    # dropped/invalidated 按 R05 聚合计数承接（invalidated_* 归 invalidated 类）。
    missing_excluded = [{'object_id': _text(g.get('object_id')), 'category': 'missing',
                         'reason_code': g.get('reason_code'), 'slot': g.get('slot')}
                        for g in missing]
    dropped_excluded = []
    for reason in sorted(dropped_in):
        count = dropped_in[reason]
        if not count:
            continue
        dropped_excluded.append({'object_id': '', 'reason_code': reason, 'count': count,
                                 'category': 'invalidated' if reason in INVALIDATED_REASONS
                                 else 'dropped'})
    excluded = missing_excluded + conflicting + budget_drops + dropped_excluded
    excluded.sort(key=lambda x: (x['category'], x['object_id'], x.get('reason_code', ''),
                                 x.get('left_id', ''), x.get('right_id', '')))

    status = _status(degradation, conflict_records, scenes, missing, budget_drops, status_in)
    rendered = sum(s['entry_count'] for s in scenes)
    counts = {'scenes': len(scenes), 'entries': rendered, 'selected': len(selected),
              'excluded': len(excluded), 'missing': len(missing_excluded),
              'conflicting': len(conflicting), 'budget_dropped': len(budget_drops),
              'invalidated': sum(1 for x in excluded if x['category'] == 'invalidated'),
              'dropped': sum(1 for x in excluded if x['category'] == 'dropped'),
              'degraded_stores': len(degradation)}

    return {
        'status': status,
        'scenes': scenes,
        'manifest': {
            'status': status,
            'context': context,
            'selected': selected,
            'excluded': excluded,
            'degradation': copy.deepcopy(degradation),
            'counts': counts,
        },
        'degradation': degradation,
        'provenance': {
            'source_status': status_in,
            'adjudicated': len(rows),
            'renderable': len(renderable),
            'rendered': rendered,
            'scenes': len(scenes),
            'conflict_objects': len(conflict_ids),
            'missing_support': missing,
            'dropped': dropped_in,
            'conflicts': conflicts_in,
            'verdicts': copy.deepcopy(verdicts),
            'root_canon': root_canon,
            'budget': {'max_scenes': max_scenes, 'max_entries': max_entries},
            'limitations': LIMITATIONS,
        },
    }