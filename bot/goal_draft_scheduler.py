# -*- coding: utf-8 -*-
"""G03 低风险检索整理草稿调度（人格内核路线 §6.1–6.4 / §7，统一方案 §11.1）。

病灶：G01 统一了提案信封、G02 让承诺/目标进入注意，但「这一轮到底先做哪件
只读小事」仍散在各 worker：curiosity 有自己的配额、刷片有自己的间隔，谁先
谁后、谁把名额用光了、谁失败了一直重试，事后说不清。本模块把**只读行动
（search/watch/organize/draft）的统一调度**落成一条纯函数、零写入的可执行
契约：在既有门禁已经放行的候选里，按显式资源预算、公平性与注意优先级做
确定性选择，选不中的逐条大声登记合法等待。

它绝不新建第二套「什么时候行动」的判断（§6.1 机会≠命令）：

  1. 候选必须是 worker 补全的完整 G01 信封，逐份经 ap.validate 复核；
     硬约束/字段缺失/期限已过/worker 自己判 wait 的候选一律顺延，
     调度器不改写任何裁决，也不替 worker 补字段（先例 G02 不补全信封）。
  2. 只调度 READ_KINDS；对外发送（private_share/private_dm/signature/
     group_message）是 G04 的范围，逐条登记 send_kind_out_of_scope。
  3. 预算事实由调用方从权威配额账本只读取来传入（usage）；本模块不读不写
     任何账本。usage=None（读不到）≠ usage={}（明确没有用量）：读不到时
     全部候选顺延 usage_unavailable 并降级，绝不冒充零用量放行。
  4. 承诺可以提高优先级，但不能越过权限（§11.1）：G02 注意条目只影响
     排序档位，硬约束/预算/冷却/公平性一律先于优先级生效。
  5. 「没有值得做的事」是合法选择：空选择不是错误，allow_wait 恒 True，
     不为凑满名额硬选。兴趣查询静默（§11.1）：入选行动恒 visibility=
     internal、无收件人，本模块不产生任何对外交代文本。

硬边界（与 G01/G02 同一范式，如实声明）：
- 纯函数、零写入。不打开任何库、不写任何账本、不解析路径、不 import os/
  时钟/随机/数据库驱动/网络库/bridge/self_model/llm_router/检索器。
  now 必填显式传入，决策路径可复现。
- 失效纪律不重造：注意条目只消费 G02 build_attention 已过 M05 read_gate
  的输出；候选自身的失效/撤回由 worker 门禁与 G01 硬约束负责。
- 确定性：无墙钟无随机，candidate_id/schedule_id/schedule_version 内容
  寻址（A03 mm.digest 口径），同输入同输出、输入顺序无关；只读深拷贝，
  不改输入。schedule_version 只随入选集合变化，now 推进但选择未变则不变。
"""
import copy
import json
import math

import action_proposal as ap
import memory_manifest as mm

ALGORITHM_VERSION = 'g03-1'
# 显式资源上限（不是质量分）：每轮总名额与单 worker 名额，超限逐条大声登记。
MAX_SLOTS = 2
MAX_PER_WORKER = 1
# 连续失败退避阈值：worker 如实报数，达到就顺延，不无限重试也不静默丢弃。
FAILURE_BACKOFF_THRESHOLD = 3
# 注意不是命令（§6.1/§6.4）：空选择合法，调度不产生行动压力。
ALLOW_WAIT_NOTE = '没有值得做的事是合法选择；调度不为凑名额硬选。'
# 兴趣查询静默（§11.1）：只读行动不产生「查到了/没查到」的独立对外交代。
SILENT_NOTE = '兴趣查询静默运行；知识供未来普通接话，不自动产生独立交代。'

# 调度器自己的稳定 reason_code；ap.validate 的裁决码（missing_*/硬约束/
# deadline_expired/...）与 worker 的 gate_reasons 逐字透传，不另造同义码。
REASON_CODES = (
    'invalid_candidate', 'send_kind_out_of_scope', 'failure_backoff',
    'usage_unavailable', 'quota_exhausted', 'cooldown_active',
    'slot_budget', 'worker_fairness',
)

LIMITATIONS = (
    'G03 只做「只读检索/整理/草稿的统一调度」可执行契约：不接 qq_openclaw_bridge、'
    '不接进真实 worker 循环（接线是主 agent 的范围），不代替任何 worker 发起行动。',
    '不裁决行动：候选的 act/wait/硬约束结论仍归 worker 既有门禁与 G01 契约；'
    '本模块只在已放行的候选里做预算/公平/优先级选择，不新增第二套行动判断'
    '（§6.1 机会≠命令 / §6.4）。',
    '只调度 READ_KINDS（search/watch/organize/draft）；对外发送是 G04 的范围，'
    '逐条登记 send_kind_out_of_scope，绝不代发。',
    '预算事实由调用方从权威配额账本只读传入；本模块零写入，不读不写任何账本。'
    'usage=None（不可读）时全部顺延并降级，绝不把读不到当成零用量。',
    '承诺只提高排序优先级，不能越过权限/预算/冷却/公平性（§11.1）；注意条目'
    '只消费 G02 已过 M05 read_gate 的输出，不重造失效引擎。',
    '兴趣查询静默（§11.1）：入选行动恒 internal、无收件人；本模块不产生任何'
    '对外交代文本，也不调模型、不发网络。',
    '失败纪律是退避不是裁决：连续失败达阈值顺延 failure_backoff，放弃条件'
    '（abandon_when）仍由 worker 按 G01 信封执行；失败计数由 worker 如实提供。',
    '确定性：无墙钟无随机零写入，now 显式传入；candidate_id/schedule_id/'
    'schedule_version 内容寻址，同输入同输出、输入顺序无关；只读深拷贝，不改输入。',
)


# ── 小工具（与 G02 同一口径，非引擎）──────────────────────────────


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_now(now):
    """now 必须显式传入：本模块不读墙上时钟（无 time import），调度可复现。"""
    if not _number(now):
        raise ValueError('goal draft scheduler requires an explicit numeric now; no wall clock')
    return float(now)


def _seal(core):
    """内容寻址 candidate_id：'gd_' + digest(候选核心)[:14]（A03 mm.digest 口径）。"""
    return dict(core, candidate_id='gd_' + mm.digest(_canonical(core))[:14])


def _deferred(row, reasons):
    seen, ordered = set(), []
    for code in reasons:
        code = _text(code)
        if code and code not in seen:
            seen.add(code)
            ordered.append(code)
    return {'candidate_id': row.get('candidate_id', ''),
            'worker': row.get('worker', ''), 'kind': row.get('kind', ''),
            'reason_codes': ordered}


# ── 候选解析：worker 事实 + 调度事实，逐字段如实 ─────────────────


def _parse_candidate(raw, flags):
    """把一份候选规范成 (核心, 提案, 调度事实)；缺字段不补造，交给 ap.validate。"""
    data = raw if isinstance(raw, dict) else {}
    proposal = data.get('proposal')
    proposal = copy.deepcopy(proposal) if isinstance(proposal, dict) else {}
    failures = data.get('recent_failures')
    if failures is None:
        failures = 0
    elif not _int(failures) or failures < 0:
        flags.append({'reason': 'invalid_recent_failures',
                      'worker': _text(proposal.get('worker'))})
        failures = 0
    link = _text(data.get('attention_entry_id'))
    core = {'proposal': proposal, 'recent_failures': failures,
            'attention_entry_id': link}
    return _seal(core), proposal, failures, link


def _attention_entries(attention, degradation):
    """只消费 G02 输出的 entries；形状不可读如实降级，不冒充空注意。"""
    if attention is None:
        return {}
    if not isinstance(attention, dict) or not isinstance(attention.get('entries'), list):
        degradation.append({'store': 'attention', 'reason': 'unreadable'})
        return {}
    out = {}
    for entry in attention['entries']:
        if isinstance(entry, dict) and _text(entry.get('entry_id')):
            out[_text(entry['entry_id'])] = entry
    return out


def _priority_tier(link, entries, flags, worker):
    """承诺提高优先级（§11.1）：overdue=0 / due_soon=1 / 其余注意=2 / 无注意=3。

    链接缺失只降回基础档并如实登记，不编造优先级。
    """
    if not link:
        return 3
    entry = entries.get(link)
    if entry is None:
        flags.append({'reason': 'attention_link_missing', 'worker': worker})
        return 3
    if entry.get('source_kind') == 'commitment':
        urgency = entry.get('urgency')
        if urgency == 'overdue':
            return 0
        if urgency == 'due_soon':
            return 1
    return 2


def _usage_facts(usage, quota_key):
    rec = (usage or {}).get(quota_key)
    if not isinstance(rec, dict):
        return 0, 0.0
    used = rec.get('used')
    last = rec.get('last_ts')
    return (used if _int(used) and used >= 0 else 0,
            float(last) if _number(last) and last >= 0 else 0.0)


# ── 统一调度：纯函数，零写入 ─────────────────────────────────────


def schedule(*, now, candidates=None, usage=None, attention=None,
             max_slots=MAX_SLOTS, max_per_worker=MAX_PER_WORKER):
    """纯函数：在既有门禁放行的只读候选里做确定性预算/公平/优先级选择。

    now:          必填显式时间（无墙钟）。
    candidates:   worker 补全的完整 G01 信封 + 调度事实，形如
                  {'proposal': <ap.build 信封>, 'recent_failures': int,
                   'attention_entry_id': str}。缺字段/硬约束/wait 候选逐条
                  顺延并透传原因，绝不改写裁决。
    usage:        权威配额账本的只读投影 {quota_key: {'used': int, 'last_ts': float}}。
                  None=读不到（全部顺延 usage_unavailable 并降级）；
                  {}=明确没有用量记录。本模块不读不写任何账本。
    attention:    G02 build_attention 的输出（或 None）；只用于优先级排序。
    max_slots:    本轮总名额（显式上限，默认 MAX_SLOTS）。
    max_per_worker: 单 worker 名额（公平性，默认 MAX_PER_WORKER）。

    返回 {schedule_id, schedule_version, algorithm_version, now, status,
    selected, deferred, budget, fairness, failures, allow_wait, silent,
    degradation, provenance, limitations}。selected 空是合法结果；
    allow_wait 恒 True、silent 恒 True，没有任何对外发送路径。
    """
    at = _require_now(now)
    max_slots = max(0, int(max_slots))
    max_per_worker = max(0, int(max_per_worker))
    degradation = []
    entries = _attention_entries(attention, degradation)
    if not isinstance(usage, dict):
        # None=读不到，非 dict=形状坏了：都不是「明确没有用量」，绝不冒充零。
        degradation.append({'store': 'usage', 'reason': 'unreadable'})
        usage = None

    # ── 逐份候选：先过 G01 契约与硬纪律，再谈排序 ────────────────
    parsed, deferred = {}, []
    for raw in (candidates or []):
        if not isinstance(raw, dict):
            deferred.append(_deferred({'worker': '', 'kind': ''}, ['invalid_candidate']))
            continue
        flags = []
        core, proposal, failures, link = _parse_candidate(raw, flags)
        cid = core['candidate_id']
        if cid in parsed:
            continue                    # 内容寻址去重：重复转述不产生二义
        parsed[cid] = (core, proposal, failures, link, flags)

    eligible = []
    quota_caps = {}
    quota_facts = {}
    for cid in sorted(parsed):
        core, proposal, failures, link, flags = parsed[cid]
        worker, kind = _text(proposal.get('worker')), _text(proposal.get('kind'))
        row = {'candidate_id': cid, 'worker': worker, 'kind': kind}
        if kind and kind not in ap.READ_KINDS:
            # 对外发送是 G04 的范围：不进 G01 复核，也不代发。
            deferred.append(_deferred(row, ['send_kind_out_of_scope']))
            continue
        verdict = ap.validate(proposal, now=at)
        if verdict['decision'] in ('rejected', 'abandon'):
            deferred.append(_deferred(row, verdict['reasons']))
            continue
        if verdict['decision'] == 'wait':
            deferred.append(_deferred(row, verdict['reasons'] or ['worker_wait']))
            continue
        if failures >= FAILURE_BACKOFF_THRESHOLD:
            deferred.append(_deferred(row, ['failure_backoff']))
            continue
        budget = proposal.get('budget') or {}
        quota_key = _text(budget.get('quota_key'))
        cap = budget.get('max_per_day')
        if quota_key:
            if quota_key in quota_caps and quota_caps[quota_key] != cap:
                # 同键不同上限是调度级事实：保守取最小，无论谁入选都要看得见。
                degradation.append({'reason': 'quota_cap_conflict',
                                    'quota_key': quota_key, 'worker': worker})
                cap = min(quota_caps[quota_key], cap)
            quota_caps[quota_key] = cap
        used, last_ts = _usage_facts(usage, quota_key)
        cooldown = budget.get('cooldown_seconds')
        if quota_key and quota_key not in quota_facts:
            # 预算可观测覆盖到被挡下的候选：用尽/冷却中的配额恰恰最要看得见。
            quota_facts[quota_key] = {'used': used, 'last_ts': last_ts,
                                      'cooldown': cooldown}
        if usage is None:
            deferred.append(_deferred(row, ['usage_unavailable']))
            continue
        if _int(cap) and used >= cap:
            deferred.append(_deferred(row, ['quota_exhausted']))
            continue
        if _number(cooldown) and last_ts > 0 and at - last_ts < cooldown:
            deferred.append(_deferred(row, ['cooldown_active']))
            continue
        tier = _priority_tier(link, entries, flags, worker)
        deadline = proposal.get('deadline')
        eligible.append({'core': core, 'proposal': proposal, 'tier': tier,
                         'deadline': float(deadline) if _number(deadline) else None,
                         'worker': worker, 'kind': kind, 'quota_key': quota_key,
                         'cap': cap, 'used': used, 'cooldown': cooldown,
                         'last_ts': last_ts, 'flags': flags,
                         'attention_entry_id': link})

    # ── 确定性排序：优先级档 → 期限（None 最后）→ worker → 内容寻址 ──
    eligible.sort(key=lambda c: (c['tier'],
                                 (1, 0.0) if c['deadline'] is None else (0, c['deadline']),
                                 c['worker'], c['core']['candidate_id']))

    # ── 贪心选择：总名额 / 单 worker 公平 / 共享配额逐次扣减 ──────
    selected, per_worker, remaining = [], {}, {}
    for cand in eligible:
        worker, quota_key = cand['worker'], cand['quota_key']
        row = {'candidate_id': cand['core']['candidate_id'],
               'worker': worker, 'kind': cand['kind']}
        if len(selected) >= max_slots:
            deferred.append(_deferred(row, ['slot_budget']))
            continue
        if per_worker.get(worker, 0) >= max_per_worker:
            deferred.append(_deferred(row, ['worker_fairness']))
            continue
        if quota_key:
            if quota_key not in remaining:
                cap = quota_caps.get(quota_key, cand['cap'])
                remaining[quota_key] = max(0, int(cap) - cand['used'])
            if remaining[quota_key] <= 0:
                deferred.append(_deferred(row, ['quota_exhausted']))
                continue
            remaining[quota_key] -= 1
        per_worker[worker] = per_worker.get(worker, 0) + 1
        degradation.extend(cand['flags'])
        selected.append({
            'candidate_id': cand['core']['candidate_id'],
            'worker': worker, 'kind': cand['kind'],
            'priority_tier': cand['tier'],
            'attention_entry_id': cand['attention_entry_id'],
            'quota_key': quota_key,
            'proposal': cand['proposal'],
        })

    # ── 可观测投影：预算/公平/失败/等待，全部如实 ─────────────────
    budget_view = {}
    for key in sorted(quota_facts):
        facts = quota_facts[key]
        used, last_ts = facts['used'], facts['last_ts']
        cap = quota_caps.get(key)
        cooldown = facts['cooldown']
        budget_view[key] = {
            'used': used if usage is not None else None,
            'max_per_day': cap,
            'remaining': (max(0, int(cap) - used) if usage is not None and _int(cap)
                          else None),
            'cooldown_seconds': cooldown if _number(cooldown) else None,
            'last_ts': last_ts if usage is not None else None,
        }
    deferred.sort(key=lambda x: (x['worker'], x['candidate_id'],
                                 ','.join(x['reason_codes'])))
    degradation.sort(key=lambda x: _canonical(x))
    selected_cores = [{'candidate_id': s['candidate_id'], 'worker': s['worker'],
                       'kind': s['kind'], 'priority_tier': s['priority_tier'],
                       'quota_key': s['quota_key']} for s in selected]
    payload = {
        'algorithm_version': ALGORITHM_VERSION,
        'now': at, 'status': 'degraded' if degradation else 'ok',
        'selected': selected, 'deferred': deferred,
        'budget': budget_view,
        'fairness': {'per_worker': dict(sorted(per_worker.items())),
                     'max_per_worker': max_per_worker, 'max_slots': max_slots},
        'failures': {'backoff_threshold': FAILURE_BACKOFF_THRESHOLD,
                     'deferred_for_failures': sum(
                         1 for d in deferred if 'failure_backoff' in d['reason_codes'])},
        'allow_wait': True, 'silent': True,
        'degradation': degradation,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'attention_is_not_command': True,
            'note': ALLOW_WAIT_NOTE, 'silent_note': SILENT_NOTE,
            'counts': {'selected': len(selected), 'deferred': len(deferred),
                       'degradation': len(degradation)},
            'limitations': list(LIMITATIONS),
        },
        'limitations': list(LIMITATIONS),
    }
    # schedule_version 只随入选集合变化：now 推进但选择未变时版本不变。
    payload['schedule_version'] = mm.digest(_canonical(selected_cores))[:16]
    payload['schedule_id'] = 'gd_' + mm.digest(_canonical(payload))[:14]
    return payload