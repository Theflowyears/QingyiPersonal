# -*- coding: utf-8 -*-
"""V02 按根源切分的记忆保留集（validation 轨，人工评审夹具，manual_review）。

§9 消融对照（只有提示词→加记忆→加持续状态→加动机与目标）要求
「去掉某个根源的所有派生记忆」这种消融可被**确定性执行与复核**。
本模块把调用方喂进来的 R01 投影 items 按 root_source_ids 切分，并在
给定保留/剔除的根源集合时算出确定的保留集，附指纹供人工评审对账。

硬边界（与 R01 的只读纪律、M02 的纯契约风格一致）：
- 真只读、纯函数：不建库建表建目录、不写账本、不网络、不调模型、
  不读时钟、不掷骰子。同一输入恒同一输出，且与 items 传入顺序无关。
- 不 import retrieval_objects：R01 仍在只观察阶段（§19.5 静态锁禁止
  bot 模块接它），items 由调用方投影后传入，这里只做切分与选择。
- 不补造生平（迁移底线）：无根源的 legacy/旧档案进显式 'unrooted' 桶，
  绝不给它们编造根源；'unrooted' 是桶名，不是可保留/可消融的根源 ID，
  把它混进 roots 会被点名拒绝，而不是悄悄当成某个真实来源。
- 不混层：legacy 保持 legacy、invalidated 保持 invalidated、unknown 保持
  unknown。保留集只回答「哪些对象属于这次消融条件」，不做有效性过滤——
  把失效/撤回记忆挡在回复外是 R01 hard_filter 的职责，这里不越权，
  也绝不把失效记忆洗成有效再交出去。

多根源口径（显式声明，policy 参数，结果里原样回显）：
- 'any'（默认）：keep 模式下**任一根源被保留则对象保留**；ablate 模式下
  任一根源被剔除则对象剔除——这是「去掉某根源的所有派生记忆」的字面
  语义，多根源对象会随任一根源一起消失。
- 'all'：keep 模式下所有根源都被保留才保留；ablate 模式下所有根源都被
  剔除才剔除（只要还有一个根源活着，对象就留着）。
两种口径都确定性；评审材料必须写明用了哪一种。默认取 'any'，因为它与
「按根源消融」的直觉一致；需要保守消融时显式传 'all'。
"""
import copy
import hashlib
import json

UNROOTED = 'unrooted'
MODES = ('keep', 'ablate')
POLICIES = ('any', 'all')


def _roots_of(item):
    """根源 ID 只做规范化（str/去空白/去空/去重/排序），绝不新增。

    缺字段、空列表、全空白都归一为空元组＝无根源，交给 unrooted 桶。
    单个字符串不是列表，不许拆成字符冒充多个根源。
    """
    raw = item.get('root_source_ids')
    if raw is None:
        values = []
    elif isinstance(raw, (str, bytes)):
        values = [raw]
    else:
        try:
            values = list(raw)
        except TypeError:
            values = [raw]
    out = []
    for value in values:
        text = '' if value is None else str(value).strip()
        if text and text not in out:
            out.append(text)
    return tuple(sorted(out))


def _normalize(items):
    """统一整理成按 object_id 排序的条目；坏条目点名报告，不静默吞。

    排序让输出与传入顺序无关（确定性不止「同输入同输出」，还要
    「同集合合同输出」，评审重排样本不该改变保留集）。
    重复 object_id 保留首次出现并报告：同一 ID 两份描述是数据问题，
    这里不猜哪份是真的。
    """
    entries, problems, seen = [], [], set()
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            problems.append({'index': index, 'reason': 'item_not_dict'})
            continue
        object_id = str(item.get('object_id') or '').strip()
        if not object_id:
            problems.append({'index': index, 'reason': 'missing_object_id'})
            continue
        if object_id in seen:
            problems.append({'index': index, 'reason': 'duplicate_object_id',
                             'object_id': object_id})
            continue
        seen.add(object_id)
        entries.append({'object_id': object_id, 'roots': _roots_of(item), 'item': item})
    entries.sort(key=lambda entry: entry['object_id'])
    return entries, problems


def split_by_root(items):
    """按真实根源切分：每个根源的派生记忆归一组，多根源对象归入每个相关组。

    返回 {groups, unrooted, multi_root_items, item_count, problems}。
    groups 的键与成员都排序；unrooted 是显式桶，装所有无根源对象
    （legacy/旧档案），绝不替它们补造根源。
    """
    entries, problems = _normalize(items)
    groups = {}
    for entry in entries:
        for root in entry['roots']:
            groups.setdefault(root, []).append(entry['object_id'])
    return {
        'groups': {root: sorted(groups[root]) for root in sorted(groups)},
        'unrooted': sorted(e['object_id'] for e in entries if not e['roots']),
        'multi_root_items': sorted(e['object_id'] for e in entries if len(e['roots']) > 1),
        'item_count': len(entries),
        'problems': problems,
    }


def _fingerprint(core):
    """选择结果的稳定指纹：人工评审复核两次消融是否同一口径同一结果。"""
    raw = json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:14]


def retention_set(items, roots, mode='keep', policy='any', keep_unrooted=True):
    """给定要保留（keep）或剔除（ablate）的根源集合，确定性算出保留集。

    roots: 根源 ID 集合（可迭代；单个字符串按一个 ID 处理）。
    mode: 'keep'＝只保留这些根源的派生记忆；'ablate'＝剔除这些根源的
          派生记忆，其余保留。
    policy: 多根源口径，见模块 docstring；'any'（默认）/ 'all'。
    keep_unrooted: 无根源对象（legacy/旧档案）是否保留。它们不属于任何
          根源，两种模式下都只由这个开关决定，默认保留——消融某个根源
          不该顺手抹掉她的旧背景；要连旧档案一起消融必须显式传 False。

    返回 dict：retained/removed（排序的 object_id）、removed_reasons、
    retained_items（深拷贝，字段原样：legacy/validity 一律不洗）、
    requested_roots、unknown_roots（请求里不存在于任何对象的根源，
    点名报告而不是静默无操作）、problems、fingerprint。
    坏条目（非 dict/缺 ID/重复 ID）不进保留集，在 problems 里点名。
    非法 mode/policy 是调用方编程错误，直接 ValueError 出声。
    """
    if mode not in MODES:
        raise ValueError('unknown_mode: %r (allowed: %s)' % (mode, ', '.join(MODES)))
    if policy not in POLICIES:
        raise ValueError('unknown_policy: %r (allowed: %s)' % (policy, ', '.join(POLICIES)))
    entries, problems = _normalize(items)
    known = {root for entry in entries for root in entry['roots']}
    if isinstance(roots, (str, bytes)):
        roots = [roots]
    wanted, seen = set(), set()
    for value in (roots or []):
        text = '' if value is None else str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if text == UNROOTED:
            # 桶名不是根源：拒绝把它当成可消融的真实来源（迁移底线）。
            problems.append({'reason': 'unrooted_not_a_root'})
            continue
        wanted.add(text)

    retained, removed, reasons, kept_items = [], [], {}, []
    for entry in entries:
        object_id, item_roots = entry['object_id'], entry['roots']
        if not item_roots:
            keep = bool(keep_unrooted)
            reason = '' if keep else 'unrooted_excluded'
        else:
            touched = bool(wanted.intersection(item_roots))
            covered = wanted.issuperset(item_roots)
            if mode == 'keep':
                keep = touched if policy == 'any' else covered
            else:
                keep = (not touched) if policy == 'any' else (not covered)
            reason = '' if keep else ('root_not_kept' if mode == 'keep' else 'root_ablated')
        if keep:
            retained.append(object_id)
            # 深拷贝后原样交出：不修改任何字段，legacy/invalidated 保持原层。
            kept_items.append(copy.deepcopy(entry['item']))
        else:
            removed.append(object_id)
            reasons[object_id] = reason

    core = {'mode': mode, 'policy': policy, 'keep_unrooted': bool(keep_unrooted),
            'requested_roots': sorted(wanted), 'retained': retained, 'removed': removed}
    return dict(core,
                unknown_roots=sorted(wanted - known),
                removed_reasons=reasons,
                retained_items=kept_items,
                retained_count=len(retained),
                removed_count=len(removed),
                problems=problems,
                fingerprint=_fingerprint(core))