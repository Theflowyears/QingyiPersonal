# -*- coding: utf-8 -*-
"""G01 统一行动提案契约（人格内核路线 §6.1–6.4 / §7，统一方案 §11.1）。

病灶：curiosity、B 站自主刷片、主动私聊、签名四条 worker 各有各的
「要不要做」记录格式，发起原因、话题主体、收件人、可见范围、预算、
成功/失败/放弃条件散在各处，事后说不清「她为什么做了 / 没做这件事」。
本模块把**提案的结构与硬校验**统一成一个信封（§7.1）：

    发起原因 / 动机类型 / 来源 event_ids / goal_id / 话题主体 topic_qq /
    对话参与者 / 收件人 recipient_qq / 可见范围 / 允许引用的证据 /
    期限 / 打扰预算（配额键 + 上限 + 冷却）/ 成功定义 / 失败定义 / 放弃条件

旁路建内核、逐步接管（§3.3，先例 cognition_writer/M06）：这里只统一
「提案长什么样、哪些字段不许缺、哪些硬约束先于动机」，**绝不新建第二套
『什么时候行动』的判断**。curiosity 配额、proactive_dm 时间窗/好感门槛、
autonomy_limits、consent/outbox、conversation_plan 仲裁保持权威；
发送路径一行不改。worker 把提案经由 observe() 登记并出声，门禁结论
仍由原函数给出。

硬口径：
  · 硬约束先行（§3.2 / §7.2 第 1 条）：权限/隐私/能力缺失登记在
    hard_blocks 里，直接 rejected，多高的 motivation_score 都抵消不了。
    motivation_score 只登记、不参与判定（先例：M02 的 candidate_confidence）。
  · 「等待/暂不行动」是合法提案结果（§6.4）：decision='wait' 不是错误，
    但等待也要把字段写明确 —— 记得「为什么没做」，不许静默补默认值。
  · 私聊收件人—话题主体一致性（§7.1 阿甲场景）：话题主体是甲时收件人
    必须是甲；甲不在好友列表、或结构化绑定拿不到（映射不确定）→
    判「放弃这条私聊」（abandon），**绝不退而发给主人**。校验依据是
    结构化 QQ 绑定（friends 列表 + topic_qq/recipient_qq），不是文本里
    有没有名字；名字冲突（addressee_conflict）只是兜底信号。
  · 字段缺失给稳定 reason_code（口径先例 claim_provenance.reasons），
    缺预算/成功/失败/放弃条件一律 rejected，不冒充「已明确」。

纯函数：build/validate/describe 不读写账本、不发网络、不调模型、不碰
路径（本模块没有 DATA_DIR，也没有任何 os 依赖）。observe() 是显式标注
的观测助手：只 print 一行日志并把最近一份提案留在模块内供检查，
契约自身出异常也必须出声，但不许拖垮既有门禁。
"""
import time

# 四类动机（§6.2）。不做「越没人理越孤独」的递增压力。
MOTIVES = ('求知', '联结', '履约', '创造表达')
# 只读行动与对外发送分开：只读提案没有受众（visibility=internal），
# 对外发送仍走原 consent/outbox 门禁，契约不替它们放行。
READ_KINDS = ('search', 'watch', 'organize', 'draft')
SEND_KINDS = ('private_share', 'private_dm', 'signature', 'group_message')
KINDS = READ_KINDS + SEND_KINDS
VISIBILITIES = ('internal', 'private', 'group', 'public')
# act=提案成立（交现有门禁裁决）；wait=合法等待；abandon=放弃这条行动
# （如私聊收件人绑定失败）；rejected=硬约束/字段缺失，提案不合格。
DECISIONS = ('act', 'wait', 'abandon', 'rejected')
# 硬约束的稳定码：权限/隐私/能力/同意，先于一切动机分（§7.2 第 1 条）。
HARD_BLOCK_CODES = ('permission_denied', 'privacy_blocked',
                    'capability_missing', 'consent_blocked')
_PRIVATE_KINDS = ('private_share', 'private_dm')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value):
    return str(value).strip() if value is not None else ''


def _seq(value):
    if isinstance(value, str):
        value = [value]
    out = []
    for item in (value or []):
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _envelope(row):
    """把任意输入规范成完整信封：每个字段都显式存在。

    缺失的必填字段留 None/''，由 validate 给 reason_code ——
    这里不补任何「看起来已明确」的默认值。
    """
    data = row if isinstance(row, dict) else {}
    budget = data.get('budget')
    score = data.get('motivation_score')
    return {
        'worker': _text(data.get('worker')),
        'kind': _text(data.get('kind')),
        'motive': _text(data.get('motive')),
        'reason': _text(data.get('reason')),
        'event_ids': _seq(data.get('event_ids')),
        'goal_id': _text(data.get('goal_id')),          # 可空：不是每个行动都挂目标
        'topic': _text(data.get('topic')),
        'topic_qq': _text(data.get('topic_qq')),
        'participants': _seq(data.get('participants')),
        'recipient_qq': _text(data.get('recipient_qq')),
        'visibility': _text(data.get('visibility')),
        'scope': dict(data.get('scope') or {}) if isinstance(data.get('scope'), dict) else {},
        'evidence': _seq(data.get('evidence')),
        'deadline': data.get('deadline'),
        'budget': dict(budget) if isinstance(budget, dict) else None,
        'success': _text(data.get('success')),
        'failure': _text(data.get('failure')),
        'abandon_when': _text(data.get('abandon_when')),
        'decision': _text(data.get('decision')),
        'gate_reasons': _seq(data.get('gate_reasons')),
        'hard_blocks': _seq(data.get('hard_blocks')),
        'addressee_conflict': _text(data.get('addressee_conflict')),
        # 动机分只登记，绝不参与准入（§3.2：硬约束不入负分、不可抵消）。
        'motivation_score': float(score) if _number(score) else None,
    }


def build(worker, kind, *, motive=None, reason=None, event_ids=(), goal_id=None,
          topic=None, topic_qq=None, participants=(), recipient_qq=None,
          visibility=None, scope=None, evidence=(), deadline=None, budget=None,
          success=None, failure=None, abandon_when=None, decision='act',
          gate_reasons=(), hard_blocks=(), addressee_conflict=None,
          motivation_score=None):
    """构造一份字段显式的行动提案信封。

    没给的必填字段保持缺失（None/''），validate 会逐条给 reason_code；
    「等待」用 decision='wait' + gate_reasons 说明哪道门关了，是合法提案。
    """
    return _envelope({
        'worker': worker, 'kind': kind, 'motive': motive, 'reason': reason,
        'event_ids': event_ids, 'goal_id': goal_id, 'topic': topic,
        'topic_qq': topic_qq, 'participants': participants,
        'recipient_qq': recipient_qq, 'visibility': visibility, 'scope': scope,
        'evidence': evidence, 'deadline': deadline, 'budget': budget,
        'success': success, 'failure': failure, 'abandon_when': abandon_when,
        'decision': decision, 'gate_reasons': gate_reasons,
        'hard_blocks': hard_blocks, 'addressee_conflict': addressee_conflict,
        'motivation_score': motivation_score,
    })


def _result(decision, reasons, env, hard_blocked):
    seen, ordered = set(), []
    for code in reasons:
        if code and code not in seen:
            seen.add(code)
            ordered.append(code)
    return {'decision': decision, 'reasons': ordered,
            'hard_blocked': bool(hard_blocked),
            'worker': env['worker'], 'kind': env['kind'], 'proposal': env}


def validate(proposal, friends=None, now=None):
    """按 §7.2 的优先顺序校验一份提案，返回结构化裁决 + 稳定 reason_code。

    friends：结构化好友 QQ 列表（不是名字表）。None 表示拿不到绑定依据。
    返回 {decision, reasons, hard_blocked, worker, kind, proposal}。
    decision ∈ DECISIONS；wait 是合法结果，abandon 用于「放弃这条私聊」，
    任何情况下都不会改写 recipient_qq —— 没有「退而发给主人」这条路径。
    """
    at = time.time() if now is None else float(now)
    env = _envelope(proposal)
    fatal, notes = [], []

    # ── 字段完整性：缺什么说什么，多条缺失一次给全 ──────────────
    if not env['worker']:
        fatal.append('missing_worker')
    if not env['kind']:
        fatal.append('missing_kind')
    elif env['kind'] not in KINDS:
        fatal.append('unknown_kind')
    if not env['motive']:
        fatal.append('missing_motive')
    elif env['motive'] not in MOTIVES:
        fatal.append('unknown_motive')
    if not env['reason']:
        fatal.append('missing_reason')
    if not env['visibility']:
        fatal.append('missing_visibility')
    elif env['visibility'] not in VISIBILITIES:
        fatal.append('unknown_visibility')
    if env['deadline'] is None:
        fatal.append('missing_deadline')
    elif not _number(env['deadline']) or env['deadline'] <= 0:
        fatal.append('invalid_deadline')
    budget = env['budget']
    if not isinstance(budget, dict):
        fatal.append('missing_budget')
    else:
        if not _text(budget.get('quota_key')):
            fatal.append('missing_quota_key')
        if not _int(budget.get('max_per_day')) or budget['max_per_day'] < 0:
            fatal.append('invalid_budget_max_per_day')
        if not _number(budget.get('cooldown_seconds')) or budget['cooldown_seconds'] < 0:
            fatal.append('invalid_budget_cooldown')
    if not env['success']:
        fatal.append('missing_success_definition')
    if not env['failure']:
        fatal.append('missing_failure_definition')
    if not env['abandon_when']:
        fatal.append('missing_abandon_condition')
    decision = env['decision']
    if decision not in ('act', 'wait'):
        fatal.append('missing_decision' if not decision else 'unknown_decision')
    if not env['event_ids']:
        # 来源事件暂时拿不到（如刷片在 claim 时还没有笔记事件）：
        # 如实登记「没有来源事件」，不补造事件 ID（先例：好奇度种子依据）。
        notes.append('no_source_events')

    kind = env['kind']
    if kind in KINDS:
        if kind in READ_KINDS:
            if env['visibility'] != 'internal':
                fatal.append('visibility_kind_mismatch')
            if env['recipient_qq']:
                fatal.append('read_kind_has_recipient')
        else:
            if env['visibility'] == 'internal':
                fatal.append('visibility_kind_mismatch')
            if kind == 'private_dm' and decision == 'act' and not env['recipient_qq']:
                fatal.append('missing_recipient')
            if kind == 'private_share' and not env['recipient_qq']:
                # 分享队列的收件人在发送门（freeze_share_delivery）才最终
                # 绑定；提案如实登记「尚未绑定」，不假装已明确。
                notes.append('recipient_bound_at_send_gate')
            if kind in ('signature', 'group_message') and env['recipient_qq']:
                fatal.append('unexpected_recipient')

    # ── 第 1 优先：硬约束。动机分再高也抵消不了（§3.2 / §7.2）──
    hard = list(env['hard_blocks'])
    if hard:
        return _result('rejected', hard + fatal + notes, env, True)
    if fatal:
        return _result('rejected', fatal + notes, env, False)

    if _number(env['deadline']) and env['deadline'] <= at:
        return _result('abandon', ['deadline_expired'] + notes, env, False)

    # ── 第 3 优先：收件人—话题主体—场景一致性（仅对外私聊、仅 act）──
    if decision == 'act' and kind in _PRIVATE_KINDS:
        binding = []
        topic_qq, recipient = env['topic_qq'], env['recipient_qq']
        friend_set = None if friends is None else {_text(x) for x in friends}
        if topic_qq:
            # 话题主体是甲 → 收件人必须是甲；绑定依据缺失/不在好友 →
            # 放弃这条私聊。绝不把收件人改写成主人或任何别人。
            if recipient != topic_qq:
                binding.append('recipient_topic_mismatch')
            elif friend_set is None:
                binding.append('friend_binding_unverified')
            elif topic_qq not in friend_set:
                binding.append('topic_not_in_friends')
        elif friend_set is not None and recipient and recipient not in friend_set:
            binding.append('recipient_not_in_friends')
        elif kind == 'private_dm' and friend_set is None:
            # 机会预留阶段拿不到好友表：登记「绑定未核验」，最终绑定
            # 在 decide（带好友表）与发送门完成。
            notes.append('friend_binding_unverified')
        if env['addressee_conflict']:
            # 名字检查只是兜底：结构化绑定已经判过，这里补一刀文本信号。
            binding.append('addressee_name_conflict')
        if binding:
            return _result('abandon', binding + notes, env, False)

    if decision == 'wait':
        return _result('wait', env['gate_reasons'] + notes, env, False)
    return _result('act', env['gate_reasons'] + notes, env, False)


def describe(result):
    """一行日志摘要（纯函数）：哪条 worker、哪道门、为什么。"""
    env = result.get('proposal') or {}
    return ('worker=%s kind=%s decision=%s reasons=%s recipient=%s topic_qq=%s'
            % (result.get('worker') or '?', result.get('kind') or '?',
               result.get('decision') or '?',
               ','.join(result.get('reasons') or []) or '-',
               env.get('recipient_qq') or '-', env.get('topic_qq') or '-'))


# ── 观测助手（唯一有副作用的部分，显式标注）──────────────────────
# 只 print + 留最近一份提案供 last() 检查；不读写账本、不发网络。
_LAST = None


def observe(proposal, friends=None, now=None):
    """worker 接线入口：校验 + 出声 + 留痕。返回 validate 的结果。

    每份提案都落一行日志 —— 沉默的 worker 视为故障（AGENTS §4）。
    契约自身异常也要出声，但绝不把异常抛回既有门禁：门禁结论仍由
    原函数给出，契约只是旁路观测（M06 先例）。
    """
    global _LAST
    try:
        result = validate(proposal, friends=friends, now=now)
    except Exception as exc:
        print('[行动提案] 契约校验异常 %s: %s'
              % (type(exc).__name__, str(exc)[:80]), flush=True)
        return None
    _LAST = result
    print('[行动提案] ' + describe(result), flush=True)
    return result


def last():
    """最近一份经 observe 登记的提案裁决（只读投影，可能为 None）。"""
    return _LAST