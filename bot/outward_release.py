# -*- coding: utf-8 -*-
"""G04 对外行动灰度与结果确认（人格内核路线 §6.4 / §7，统一方案 §11.1 / §V）。

病灶：G01 统一了提案信封、G03 把只读小事纳入统一调度，但对外发送
（private_share/private_dm/signature/group_message）还没有统一的灰度授权
与结果确认契约：谁批准的、批的是哪一类、发出之后到底算不算「对方收到了」，
散在各 worker 的口头约定里。本模块把**对外行动的灰度门与结果确认**落成一条
纯函数、零写入的可执行契约（待办 G04：「对外分享/私聊/签名单独灰度授权，
保住 consent/outbox/unknown/源范围；不得为填样本或凑次数发言」）。

它绝不新建第二套「什么时候行动」的判断，也绝不替任何门禁放行：

  1. 灰度授权只认人工签字事实（signoff 由调用方从权威账本只读取来传入，
     先例 C06 expression_gate 读 ops stage3_signoff）：逐 kind 单独授权，
     缺签字/缺凭证/过期一律 observe-only。本模块不读不写任何账本，
     永不自己授权；authorization 各开关恒 False 的纪律不变。
  2. consent/outbox/unknown/源范围门禁保持权威：本契约只登记「这些门必须
     仍在发送路径上」，不替代、不绕过、不预判它们的结论。真实发送链的
     幂等（unknown 不重发、多段回执不重复接受）由工程门
     action_idempotency_outbox 的隔离探针验证，本模块不冒充已测。
  3. 结果确认（§2 行动真实性）：发出请求 ≠ 对方收到。retcode=0/ok=True
     最多记 sent；delivered 必须有显式送达确认，绝不从发送成功推断；
     异常/缺失/读不懂的回执一律 unknown，unknown 永不升级、永不触发重发；
     失败不得记完成。结果只降不升：delivered 之外的任何「升级」都拒绝。
  4. 不为填样本或凑次数发言：没有最小发送数，空灰度/空结果是合法状态，
     allow_wait 恒 True；收件人—话题主体绑定沿用 G01 校验，绝不改收件人。

硬边界（与 G01/G02/G03 同一范式，如实声明）：
- 纯函数、零写入。不打开任何库、不写任何账本、不解析路径、不 import os/
  时钟/随机/数据库驱动/网络库/bridge/self_model/llm_router/发送器。
  now 必填显式传入，决策路径可复现。
- 复用而非重造：信封校验调 G01 ap.validate，内容寻址用 A03 mm.digest；
  不另造提案契约、不另长失效引擎。
- 确定性：无墙钟无随机，release_id/result_id 内容寻址，同输入同输出、
  输入顺序无关；只读深拷贝，不改输入。
"""
import copy
import json
import math

import action_proposal as ap
import memory_manifest as mm

ALGORITHM_VERSION = 'g04-1'
# 对外四类逐 kind 单独灰度（§11.1：先只读后对外；对外每一类都要单独授权）。
OUTWARD_KINDS = ap.SEND_KINDS
# 结果词表：只降不升。sent=请求被接口接受；delivered=显式送达确认；
# unknown=回执缺失/异常/读不懂（永不重发、永不升级）；failed=明确失败；
# rejected=被 consent/权限/范围门挡下；abandoned=按 G01 放弃（如绑定失败）。
RESULTS = ('pending', 'sent', 'delivered', 'unknown', 'failed', 'rejected', 'abandoned')
# 灰度门的稳定 reason_code；G01 裁决码逐字透传，不另造同义码。
GATE_REASONS = (
    'no_human_signoff', 'signoff_unreadable', 'kind_not_granted',
    'signoff_expired', 'missing_signoff_evidence',
)
# 结果确认的稳定 reason_code。
RESULT_REASONS = (
    'invalid_record', 'not_outward_kind', 'unknown_receipt',
    'delivery_requires_confirmation', 'result_downgrade_blocked',
    'failure_recorded_as_done',
)
# 显式送达确认的唯一入口：回执里带 delivery_confirmed=True 才可能 delivered。
DELIVERY_FIELD = 'delivery_confirmed'
ALLOW_WAIT_NOTE = '没有值得发的就什么都不发；不为填样本或凑次数发言。'

LIMITATIONS = (
    'G04 只做「对外行动灰度授权与结果确认」的可执行契约：不接 qq_openclaw_bridge、'
    '不接进真实发送路径（接线是主 agent 的范围），不代替任何 worker 发送。',
    '永不自己授权：灰度只认调用方传入的人工签字事实（先例 C06 读 ops stage3_signoff）；'
    '本模块零写入，不读不写任何账本，authorization 各开关恒 False 的纪律不变。',
    'consent/outbox/unknown/源范围门禁保持权威：本契约只登记它们必须仍在发送路径上，'
    '不替代、不绕过、不预判结论；真实发送链幂等由工程门 action_idempotency_outbox '
    '的隔离探针验证，本模块不冒充已测真实发送链。',
    '发出请求≠对方收到（§2 行动真实性）：retcode=0/ok=True 最多记 sent；delivered '
    '必须有显式送达确认；异常/缺失/读不懂的回执一律 unknown，unknown 永不升级、'
    '永不触发重发；失败不得记完成。',
    '结果只降不升：任何把 unknown/failed 升级成 sent/delivered 的输入都被拒绝并大声登记；'
    '收件人—话题主体绑定沿用 G01，绝不改收件人（§7.1 阿甲场景）。',
    '不为填样本或凑次数发言：没有最小发送数，空灰度/空结果合法，allow_wait 恒 True。',
    '确定性：无墙钟无随机零写入，now 显式传入；release_id/result_id 内容寻址，'
    '同输入同输出、输入顺序无关；只读深拷贝，不改输入。',
)


# ── 小工具（与 G02/G03 同一口径，非引擎）─────────────────────────


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _require_now(now):
    """now 必须显式传入：本模块不读墙上时钟（无 time import），灰度可复现。"""
    if not _number(now):
        raise ValueError('outward release requires an explicit numeric now; no wall clock')
    return float(now)


# ── 灰度门：逐 kind 单独人工授权，缺凭证一律 observe-only ────────


def _signoff_facts(signoff, degradation):
    if signoff is None:
        return None
    if not isinstance(signoff, dict):
        degradation.append({'store': 'signoff', 'reason': 'unreadable'})
        return None
    return signoff


def release_gate(*, now, signoff=None, kinds=OUTWARD_KINDS):
    """纯函数：对外四类的灰度授权投影。只认人工签字，绝不自己授权。

    signoff: 权威账本的人工签字只读投影，形如
             {'granted': {kind: {'by': str, 'at': ts, 'expires_ts': ts?}},
              'revoked': [kind, ...]}。
             None=没读到（全部 no_human_signoff）；形状坏了=降级+全关。
    kinds:   要投影的对外类别（默认四类全集；只读 kind 不在灰度范围）。

    返回 {algorithm_version, now, active, per_kind, reasons, allow_wait,
    silent, degradation, provenance, limitations, release_id}。
    active 恒等于「至少一类被人工授权且未过期未撤回」；没有任何自动放行路径。
    """
    at = _require_now(now)
    degradation = []
    facts = _signoff_facts(signoff, degradation)
    granted = facts.get('granted') if isinstance(facts, dict) else None
    revoked = facts.get('revoked') if isinstance(facts, dict) else None
    if not isinstance(granted, dict):
        if facts is not None:
            degradation.append({'store': 'signoff', 'reason': 'unreadable'})
        granted = {}
    revoked_set = {_text(k) for k in (revoked or []) if _text(k)}

    per_kind = {}
    for kind in sorted({_text(k) for k in (kinds or []) if _text(k)}):
        reasons = []
        if kind not in OUTWARD_KINDS:
            reasons.append('not_outward_kind')
        entry = granted.get(kind)
        if kind in revoked_set:
            reasons.append('no_human_signoff')
        elif not isinstance(entry, dict):
            reasons.append('no_human_signoff' if facts is None or not granted
                           else 'kind_not_granted')
        else:
            if not _text(entry.get('by')) or not _number(entry.get('at')):
                reasons.append('missing_signoff_evidence')
            expires = entry.get('expires_ts')
            if expires is not None and (not _number(expires) or expires <= at):
                reasons.append('signoff_expired')
        ok = not reasons
        per_kind[kind] = {'granted': ok, 'reasons': reasons}
    active = any(row['granted'] for row in per_kind.values())
    payload = {
        'algorithm_version': ALGORITHM_VERSION,
        'now': at, 'active': bool(active), 'per_kind': per_kind,
        'allow_wait': True, 'silent': False,
        'degradation': degradation,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'human_only_authorization': True,
            'note': ALLOW_WAIT_NOTE,
            'downstream_gates': ['consent', 'outbox', 'unknown_no_resend',
                                 'source_scope'],
            'counts': {'granted': sum(1 for r in per_kind.values() if r['granted']),
                       'kinds': len(per_kind)},
            'limitations': list(LIMITATIONS),
        },
        'limitations': list(LIMITATIONS),
    }
    payload['release_id'] = 'g4_' + mm.digest(_canonical(payload))[:14]
    return payload


# ── 结果确认：发出≠收到，unknown 永不升级，失败不记完成 ──────────


def _receipt_result(receipt):
    """把一份回执映射成结果词表；读不懂一律 unknown，绝不推断送达。

    异常（error=True）是 unknown 不是 failed：线上抛错时对方收没收到不知道
    （先例 runtime outbox test_exception_is_unknown_receipt）；明确失败只认
    非零 retcode 或显式 failed 标记。
    """
    if not isinstance(receipt, dict):
        return 'unknown', ['unknown_receipt']
    if receipt.get('rejected'):
        return 'rejected', []
    if receipt.get('abandoned'):
        return 'abandoned', []
    if receipt.get('error') is True:
        return 'unknown', ['unknown_receipt']
    if receipt.get('failed') is True or (
            _number(receipt.get('retcode')) and receipt['retcode'] != 0):
        return 'failed', []
    if receipt.get(DELIVERY_FIELD) is True:
        return 'delivered', []
    if receipt.get('retcode') == 0 or receipt.get('ok') is True:
        return 'sent', []
    return 'unknown', ['unknown_receipt']


# 结果只降不升的显式规则（不是全序打分）：明确失败/拒绝/放弃是终态事实，
# 之后任何「成功」回执都不能改写它；delivered 是终态，不许降回更悲观。
_TERMINAL_NEGATIVE = ('failed', 'rejected', 'abandoned')


def _upgrade_blocked(prior, result):
    if prior in _TERMINAL_NEGATIVE and result in ('sent', 'delivered', 'pending', 'unknown'):
        return True
    if prior == 'delivered' and result != 'delivered':
        return True
    if prior == 'sent' and result in ('pending', 'unknown'):
        return True
    return False


def confirm_results(*, now, records=None, gate=None, friends=None):
    """纯函数：逐条确认对外行动的真实结果，产出可审计的结果清单。

    records: [{'proposal': <G01 对外信封>, 'receipt': <回执 dict>,
               'prior_result': str?}]。prior_result 是已落账的旧结果：
              新结果只允许保持或更悲观，往乐观方向改写一律拒绝。
    gate:    release_gate 的输出（或 None）。未授权 kind 的行动结果照常
             确认（事实就是事实），但登记 kind_not_granted 供审计。
    friends: 结构化好友 QQ 列表（G01 绑定校验依据）；None=拿不到绑定依据，
             私聊按 G01 纪律判 abandon，绝不退而发给主人。

    返回 {algorithm_version, now, results, counts, allow_wait, degradation,
    provenance, limitations}。results 逐条带 result_id/reasons；
    unknown 永不带 resend 建议，delivered 必来自显式确认。
    """
    at = _require_now(now)
    degradation = []
    gate_kinds = {}
    if gate is not None:
        if isinstance(gate, dict) and isinstance(gate.get('per_kind'), dict):
            gate_kinds = gate['per_kind']
        else:
            degradation.append({'store': 'gate', 'reason': 'unreadable'})

    results, seen = [], set()
    for raw in (records or []):
        if not isinstance(raw, dict):
            degradation.append({'store': 'record', 'reason': 'invalid_record'})
            continue
        proposal = raw.get('proposal')
        proposal = copy.deepcopy(proposal) if isinstance(proposal, dict) else {}
        receipt = raw.get('receipt')
        worker, kind = _text(proposal.get('worker')), _text(proposal.get('kind'))
        core = {'proposal': proposal, 'receipt': receipt if isinstance(receipt, dict) else None,
                'prior_result': _text(raw.get('prior_result'))}
        rid = 'g4r_' + mm.digest(_canonical(core))[:14]
        if rid in seen:
            continue
        seen.add(rid)
        reasons = []
        if kind not in OUTWARD_KINDS:
            result = 'unknown'
            reasons.append('not_outward_kind')
        else:
            verdict = ap.validate(proposal, friends=friends, now=at)
            if verdict['decision'] == 'rejected':
                result, reasons = 'rejected', list(verdict['reasons'])
            elif verdict['decision'] == 'abandon':
                result, reasons = 'abandoned', list(verdict['reasons'])
            else:
                result, reasons = _receipt_result(receipt)
                if verdict['decision'] == 'wait':
                    reasons = list(verdict['reasons']) + reasons
        if result == 'delivered' and not (
                isinstance(receipt, dict) and receipt.get(DELIVERY_FIELD) is True):
            reasons.append('delivery_requires_confirmation')
            result = 'unknown'
        if result in ('sent', 'delivered') and isinstance(receipt, dict) and (
                receipt.get('failed') is True or (
                    _number(receipt.get('retcode')) and receipt['retcode'] != 0)):
            reasons.append('failure_recorded_as_done')
            result = 'failed'
        prior = core['prior_result']
        if prior in RESULTS and _upgrade_blocked(prior, result):
            # 终态事实优先：恢复旧结果，撤掉中间态产生的误报原因。
            result = prior
            reasons = [code for code in reasons
                       if code not in ('delivery_requires_confirmation',
                                       'failure_recorded_as_done', 'unknown_receipt')]
            reasons.append('result_downgrade_blocked')
        gate_row = gate_kinds.get(kind)
        if gate_row is not None and not gate_row.get('granted'):
            reasons.append('kind_not_granted')
        seen_order = sorted(set(reasons))
        results.append({'result_id': rid, 'worker': worker, 'kind': kind,
                        'result': result, 'reasons': seen_order,
                        'recipient_qq': _text(proposal.get('recipient_qq')),
                        'resend': False})

    results.sort(key=lambda r: (r['worker'], r['result_id']))
    degradation.sort(key=lambda x: _canonical(x))
    counts = {name: sum(1 for r in results if r['result'] == name) for name in RESULTS}
    payload = {
        'algorithm_version': ALGORITHM_VERSION,
        'now': at, 'results': results, 'counts': counts,
        'allow_wait': True,
        'degradation': degradation,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'sent_is_not_delivered': True,
            'unknown_never_resends': True,
            'note': ALLOW_WAIT_NOTE,
            'counts': dict(counts, total=len(results)),
            'limitations': list(LIMITATIONS),
        },
        'limitations': list(LIMITATIONS),
    }
    return payload