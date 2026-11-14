# -*- coding: utf-8 -*-
"""G02 承诺与目标进入注意（人格内核路线 §4.1 / §6.1–6.4 / §7.1 / §8.3）。

病灶：C04 已把承诺/目标渲染进状态层，但「哪些承诺/目标此刻值得注意、以什么
顺序、带什么行动身份字段」没有契约。本模块把「承诺与目标进入注意」落成一条
**纯函数、零写入**的可执行契约：从 M06 已落账的 commitment/goal 行产出有界、
确定性排序、内容寻址的注意条目，供工作状态/统一仲裁消费。

它绝不新建第二套「什么时候行动」判断（§6.1 机会≠命令；仲裁仍归 conversation_plan
与既有门禁），绝不把注意条目变成行动命令：

  1. build_attention：now 必填显式传入（无墙钟），承诺/目标行经 cw._text 提取文本、
     状态过滤（只有 open 承诺 / active 目标进注意）、逐条过 M05 dag.read_gate
     （失效承诺绝不驱动注意），算 urgency 三档（overdue/due_soon/open），按
     「承诺先于目标、承诺内 overdue>due_soon>open」确定性排序，内容寻址 entry_id。
     超出 max_entries 的条目逐条大声登记 budget，绝不静默截断。输出恒带
     allow_wait=True 与「注意不是命令」的 provenance 声明，没有任何 act/rejected
     裁决字段。attention_version 只随条目核心变化（§8.3 立即生效，不按日期缓存）。
  2. proposal_seed：把一条注意条目映射成 G01 信封的**诚实部分种子**——只给
     worker/motive/reason/event_ids/goal_id/topic_qq/recipient_qq/deadline 与
     decision='wait'；kind/visibility/budget/success/failure/abandon_when 一律留缺
     （这些是 worker 的事实，G02 不编造）。种子不是合格提案，worker 必须补全后经
     ap.observe 交既有门禁。

硬边界（与 C04/G01 同一范式，如实声明）：
- 纯函数、零写入。不打开任何库、不写任何账本、不解析路径、不 import os/时钟/随机/
  数据库驱动/网络库/bridge/self_model/llm_router/检索器。states/epoch/scope 由调用方
  经 M05 只读桥取得后传入；now 必填，决策路径可复现。
- 承诺/目标只消费 M06 cognition_writer.commit 已落账的行（self_model commitment/goal
  表形状）；文本提取复用 cw._text 同一套口径，不另长第二套。新承诺/目标只能经 M06
  单一写入者进来，本模块零写入。
- 失效纪律复用 M05 invalidation_dag.read_gate：invalidated/superseded/
  needs_reassessment/scope_revoked/stale_projection 的来源一律挡下并大声登记，
  挡下原因逐字透传，不另造同义码。
- 收件人绑定纪律（§7.1 阿甲场景）：承诺 to_qq → topic_qq=recipient_qq=to_qq；
  to_qq 缺 → 两者皆空并登记 unbound，**绝不默认主人**。目标 owner_qq → topic_qq，
  recipient_qq 恒空（目标不是给谁的消息）。event_ids 只取行里真实存在的来源事件，
  不补造。
- 动机：承诺恒 '履约'（ap.MOTIVES 之一）；目标取行里 motive 原文，
  motive_known = motive in ap.MOTIVES。未知动机如实登记，不拒绝真实目标、也不冒充
  四类之一。关系维度与 motivation_score 不在此模块。
- 确定性：无墙钟无随机，attention_id/attention_version/entry_id 内容寻址
  （A03 mm.digest 口径），同输入同输出、输入顺序无关；只读深拷贝，不改输入。
"""
import copy
import json
import math

import cognition_writer as cw
import invalidation_dag as dag
import memory_manifest as mm
import action_proposal as ap

ALGORITHM_VERSION = 'g02-1'
# 显式资源上限（不是质量分）：超限逐条大声登记 budget，绝不静默截断。
MAX_ENTRIES = 8
# 「快要到期」窗口是显式冻结常量，不是墙钟：due_ts<=now+窗口 → due_soon。
DUE_SOON_WINDOW = 86400.0
# 承诺的行动身份恒为履约（§6.2 四类动机之一，复用 ap.MOTIVES，不另造）。
COMMITMENT_MOTIVE = '履约'
# 注意不是命令（§6.1/§6.4）：种子恒以 wait 交既有门禁，绝不替仲裁裁决。
SEED_GATE_REASON = 'attention_is_not_command'

# 稳定 reason_code：复用 C04 同义码（逐字一致，不另造），加 G02 自己的
# unbound/invalid_due_ts/budget。read_gate 的挡下原因（invalidated/superseded/
# needs_reassessment/scope_revoked/stale_projection/...）逐字透传，不新造。
REASON_CODES = (
    'missing_object_id', 'missing_text', 'commitment_not_open', 'goal_not_active',
    'unbound', 'invalid_due_ts', 'budget',
)

# urgency 三档（承诺）与目标档；排序优先级 overdue>due_soon>open。
_URGENCY_RANK = {'overdue': 0, 'due_soon': 1, 'open': 2}

LIMITATIONS = (
    'G02 只做「承诺与目标进入注意」的可执行契约：不接 qq_openclaw_bridge、不接进'
    '真实回复链路（接线是 C05/主 agent 的范围）。',
    '不裁决行动：注意条目不是行动命令，「什么时候行动」仍归 conversation_plan 与'
    '既有门禁的统一仲裁；本模块不新增第二套行动判断（§6.1 机会≠命令 / §6.4）。',
    '不补全信封：proposal_seed 只给 G01 信封的诚实部分种子，kind/visibility/budget/'
    'success/failure/abandon_when 是 worker 的事实，G02 不编造；worker 必须补全后经 '
    'ap.observe 交既有门禁，对缺字段种子调 ap.validate 得 rejected+missing_* 是正确行为。',
    '注意不是命令：输出恒带 allow_wait=True，「等待/暂不行动」永远合法；没有任何 '
    'act/rejected 裁决字段。',
    '承诺/目标只消费 M06 cognition_writer.commit 已落账的行（self_model commitment/'
    'goal 表形状）；本模块零写入，新承诺/目标只能经 M06 单一写入者进来。',
    '失效纪律复用 M05 invalidation_dag.read_gate：invalidated/superseded/'
    'needs_reassessment/scope_revoked/stale_projection 的来源一律挡下，失效承诺绝不驱动注意。',
    '关系与动机分不在此：不携带好感/信任/亲密维度，不计算 motivation_score；未知动机'
    '如实登记 motive_known=False，不拒绝真实目标、也不冒充四类之一。',
    '确定性：无墙钟无随机零写入，now 显式传入；attention_id/attention_version/entry_id '
    '内容寻址，同输入同输出、输入顺序无关；只读深拷贝，不改输入。',
    '收件人绑定纪律（§7.1）：承诺 to_qq 缺则 topic_qq/recipient_qq 皆空并登记 unbound，'
    '绝不默认主人；目标 recipient_qq 恒空（目标不是给谁的消息）。',
)


# ── 小工具（与 C04 同一口径，非引擎）──────────────────────────────


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _require_now(now):
    """now 必须显式传入：本模块不读墙上时钟（无 time import），注意可复现。"""
    if not _number(now):
        raise ValueError('goal attention requires an explicit numeric now; no wall clock')
    return float(now)


def _oid(row):
    return _text(row.get('object_id') or row.get('id'))


def _event_ids(raw):
    """只取行里真实存在的来源事件（list 或 M06 落账的 JSON 字符串），不补造。"""
    value = copy.deepcopy(raw.get('event_ids') or [])
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _goal_event_ids(raw):
    """目标来源事件：event_ids 加上真实存在的 parent_event（goal 表的起源事件）。"""
    out = _event_ids(raw)
    parent = _text(raw.get('parent_event'))
    if parent and parent not in out:
        out.append(parent)
    return out


def _gate(oid, source_kind, states, epoch, scope, excluded):
    """单条过 M05 最终读门：失效/待重评/范围收紧/落后 epoch 一律挡下并大声登记。

    判定完全委托 dag.read_gate（不另长第二套失效引擎）；挡下原因逐字透传。
    """
    kept, dropped = dag.read_gate([{'object_id': oid, 'projection_epoch': epoch}],
                                  states, epoch, scope)
    if kept:
        return True
    excluded.append({'object_id': oid, 'source_kind': source_kind,
                     'reason_code': sorted(dropped)[0]})
    return False


def _seal(core):
    """内容寻址 entry_id：'ga_' + digest(条目核心)[:14]（A03 mm.digest 口径）。"""
    return dict(core, entry_id='ga_' + mm.digest(_canonical(core))[:14])


def _unique_by_oid(parsed):
    """按 object_id 排序去重（首见优先）：输入顺序无关，重复转述不产生二义。"""
    unique = {}
    for item in sorted(parsed, key=lambda t: t[0]):
        unique.setdefault(item[0], item)
    return [unique[oid] for oid in sorted(unique)]


# ── 注意条目构建：纯函数，零写入 ─────────────────────────────────


def _commitment_entry(oid, text, raw, at, epoch):
    """承诺条目：恒履约动机；urgency 三档；to_qq 绑定纪律（缺则 unbound，绝不默认主人）。

    返回 (entry, flags)：flags 是「保留但如实登记」的不完整信号（unbound/invalid_due_ts），
    只有最终入选（未被 budget 截掉）的条目才把 flags 计入 degradation。
    """
    flags = []
    due_raw = raw.get('due_ts')
    if due_raw is None:
        due = None                      # 无期限是正常状态，不是错误
    elif _number(due_raw):
        due = float(due_raw)
    else:
        due = None                      # 非数值期限：如实登记，不补造期限
        flags.append({'store': 'commitment', 'object_id': oid,
                      'reason': 'invalid_due_ts'})
    if due is None:
        urgency = 'open'
    elif due < at:
        urgency = 'overdue'
    elif due <= at + DUE_SOON_WINDOW:
        urgency = 'due_soon'
    else:
        urgency = 'open'

    to_qq = _text(raw.get('to_qq'))
    if to_qq:
        topic_qq = recipient_qq = to_qq
    else:
        topic_qq = recipient_qq = ''    # 绝不默认主人（§7.1 阿甲场景）
        flags.append({'store': 'commitment', 'object_id': oid, 'reason': 'unbound'})

    core = {
        'source_kind': 'commitment', 'object_id': oid, 'text': text,
        'motive': COMMITMENT_MOTIVE,
        'motive_known': COMMITMENT_MOTIVE in ap.MOTIVES,
        'urgency': urgency, 'due_ts': due,
        'topic_qq': topic_qq, 'recipient_qq': recipient_qq,
        'goal_id': '', 'event_ids': _event_ids(raw), 'projection_epoch': epoch,
    }
    return _seal(core), flags


def _goal_entry(oid, text, raw, epoch):
    """目标条目：motive 取原文（未知如实登记）；owner_qq→topic_qq，recipient 恒空。"""
    motive = _text(raw.get('motive'))
    core = {
        'source_kind': 'goal', 'object_id': oid, 'text': text,
        'motive': motive, 'motive_known': motive in ap.MOTIVES,
        'urgency': 'active', 'due_ts': None,
        'topic_qq': _text(raw.get('owner_qq')), 'recipient_qq': '',
        'goal_id': oid, 'event_ids': _goal_event_ids(raw), 'projection_epoch': epoch,
    }
    return _seal(core), []


def _commitment_sort_key(entry):
    """承诺内排序：overdue>due_soon>open，再按 due_ts（None 排最后）、object_id。"""
    due = entry['due_ts']
    due_key = (1, 0.0) if due is None else (0, due)
    return (_URGENCY_RANK[entry['urgency']], due_key, entry['object_id'])


def _collect_commitments(commitments, at, states, epoch, scope, excluded):
    parsed = []
    for raw in (commitments or []):
        if not isinstance(raw, dict):
            continue
        oid = _oid(raw)
        if not oid:
            excluded.append({'object_id': '', 'source_kind': 'commitment',
                             'reason_code': 'missing_object_id'})
            continue
        text = cw._text(raw)
        if not text:
            excluded.append({'object_id': oid, 'source_kind': 'commitment',
                             'reason_code': 'missing_text'})
            continue
        status = _text(raw.get('status')) or 'open'
        if status != 'open':
            excluded.append({'object_id': oid, 'source_kind': 'commitment',
                             'reason_code': 'commitment_not_open'})
            continue
        parsed.append((oid, text, raw))
    built = []
    for oid, text, raw in _unique_by_oid(parsed):
        if not _gate(oid, 'commitment', states, epoch, scope, excluded):
            continue
        built.append(_commitment_entry(oid, text, raw, at, epoch))
    built.sort(key=lambda pair: _commitment_sort_key(pair[0]))
    return built


def _collect_goals(goals, states, epoch, scope, excluded):
    parsed = []
    for raw in (goals or []):
        if not isinstance(raw, dict):
            continue
        oid = _oid(raw)
        if not oid:
            excluded.append({'object_id': '', 'source_kind': 'goal',
                             'reason_code': 'missing_object_id'})
            continue
        text = cw._text(raw)
        if not text:
            excluded.append({'object_id': oid, 'source_kind': 'goal',
                             'reason_code': 'missing_text'})
            continue
        status = _text(raw.get('status')) or 'active'
        if status != 'active':
            excluded.append({'object_id': oid, 'source_kind': 'goal',
                             'reason_code': 'goal_not_active'})
            continue
        parsed.append((oid, text, raw))
    built = []
    for oid, text, raw in _unique_by_oid(parsed):
        if not _gate(oid, 'goal', states, epoch, scope, excluded):
            continue
        built.append(_goal_entry(oid, text, raw, epoch))
    built.sort(key=lambda pair: pair[0]['object_id'])
    return built


def build_attention(*, now, commitments=None, goals=None, states=None,
                    epoch=0, scope='', max_entries=MAX_ENTRIES):
    """纯函数：从 M06 已落账的承诺/目标行构建有界、确定性、内容寻址的注意。

    now:         必填显式时间（无墙钟）。
    commitments: M06/self_model 已落账的承诺行；只有 open 的进注意。
    goals:       M06/self_model 已落账的目标行；只有 active 的进注意。
    states/epoch/scope: 调用方经 M05 只读桥取得的失效状态/epoch/范围；所有条目过
                 dag.read_gate，失效来源绝不进注意。states 非 dict 如实降级。
    max_entries: 显式上限（默认 MAX_ENTRIES）；超限逐条大声登记 budget。

    返回 {attention_id, attention_version, algorithm_version, now, epoch, scope,
    status, entries, allow_wait, excluded, degradation, provenance, limitations}。
    allow_wait 恒 True，没有任何 act/rejected 裁决字段——注意不是命令（§6.1/§6.4）。
    excluded 只装被丢弃的行（缺字段/状态不符/失效/budget）；degradation 装「保留但
    不完整」的如实信号（unbound/invalid_due_ts/状态不可读），有 degradation 则 degraded。
    """
    at = _require_now(now)
    epoch = int(epoch)
    if epoch < 0:
        raise ValueError('epoch must be nonnegative')
    scope = _text(scope)
    max_entries = max(0, int(max_entries))
    if states is None:
        prior = {}
    elif isinstance(states, dict):
        prior = states
    else:
        prior = {}
    excluded, degradation = [], []
    if states is not None and not isinstance(states, dict):
        degradation.append({'store': 'invalidation', 'reason': 'unreadable'})

    commitment_built = _collect_commitments(commitments, at, prior, epoch, scope, excluded)
    goal_built = _collect_goals(goals, prior, epoch, scope, excluded)

    # 承诺先于目标（§7.2 第2优先：真实待处理承诺）；合并后按显式上限截断。
    combined = commitment_built + goal_built
    kept, overflow = combined[:max_entries], combined[max_entries:]
    entries = [pair[0] for pair in kept]
    for _entry, flags in kept:
        degradation.extend(flags)
    for entry, _flags in overflow:
        excluded.append({'object_id': entry['object_id'],
                         'source_kind': entry['source_kind'], 'reason_code': 'budget'})

    excluded.sort(key=lambda x: (x['source_kind'], x['object_id'], x['reason_code']))
    degradation.sort(key=lambda x: _canonical(x))
    # attention_version 只随条目核心变化：任何承诺/目标/urgency 变化立即变（§8.3），
    # 不按日期缓存；now 推进但条目未变时版本不变（内容寻址，不是时钟）。
    attention_version = mm.digest(_canonical(entries))[:16]
    payload = {
        'algorithm_version': ALGORITHM_VERSION,
        'attention_version': attention_version,
        'now': at, 'epoch': epoch, 'scope': scope,
        'status': 'degraded' if degradation else 'ok',
        'entries': entries, 'allow_wait': True,
        'excluded': excluded, 'degradation': degradation,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'attention_is_not_command': True,
            'note': '注意不是命令：条目只表示此刻值得注意，行动裁决仍归统一仲裁（§6.1/§6.4）。',
            'state_layer_note': cw._STATE_NOTE,
            'due_soon_window': DUE_SOON_WINDOW,
            'max_entries': max_entries,
            'counts': {
                'entries': len(entries),
                'commitments': sum(1 for e in entries if e['source_kind'] == 'commitment'),
                'goals': sum(1 for e in entries if e['source_kind'] == 'goal'),
                'excluded': len(excluded), 'degradation': len(degradation),
            },
            'limitations': list(LIMITATIONS),
        },
        'limitations': list(LIMITATIONS),
    }
    payload['attention_id'] = 'ga_' + mm.digest(_canonical(payload))[:14]
    return payload


def proposal_seed(entry):
    """把一条注意条目映射成 G01 信封的**诚实部分种子**（纯函数，零写入）。

    种子不是合格提案：kind/visibility/budget/success/failure/abandon_when 一律留缺
    ——这些是 worker 的事实，G02 不编造。worker 必须补全这些字段后经 ap.observe 交
    既有门禁裁决；对缺字段种子调 ap.validate 会得到 rejected + missing_kind/
    missing_budget/missing_visibility/... 这是正确行为（证明 G02 不冒充完整提案）。

    motive 仅在 motive_known 时给（否则留空，不冒充四类之一）；decision 恒 'wait'
    （注意不是命令，§6.1/§6.4）；goal_id 目标条目=object_id、承诺条目=''；
    deadline=due_ts（无期限则 None，不补造）。本函数不调用 ap.validate/observe/build。
    """
    data = entry if isinstance(entry, dict) else {}
    motive_known = bool(data.get('motive_known'))
    is_goal = data.get('source_kind') == 'goal'
    return {
        'worker': 'goal_attention',
        'motive': _text(data.get('motive')) if motive_known else '',
        'reason': _text(data.get('text')),
        'event_ids': list(data.get('event_ids') or []),
        'goal_id': _text(data.get('object_id')) if is_goal else '',
        'topic_qq': _text(data.get('topic_qq')),
        'recipient_qq': _text(data.get('recipient_qq')),
        'deadline': data.get('due_ts'),
        'decision': 'wait',
        'gate_reasons': [SEED_GATE_REASON],
    }