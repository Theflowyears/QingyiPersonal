# -*- coding: utf-8 -*-
"""L03 习惯和关系长期更新（统一方案 §L 表 L03 行 / §7.4 / §11.2 / §11.3；
stage5/self，前置 L02/G03，参照 L02 版本修订范式）。

病灶：L02 把「信念/自我认识」的版本修订证完就停手（self_revision LIMITATIONS：
「不做 L03 习惯与关系长期更新」）；G03 把只读行动的统一调度证完就停手（不裁决
行动结果如何沉淀成长期自我）。于是「反复得到结果的做法怎么变成可撤回的习惯
策略、长期关系怎么慢速更新」仍没有契约：容易从口癖/单次心情直接学人格，或者
只看支持证据不看反证（确认偏误）。本模块把 §7.4 的晋升依赖落成一条**纯函数、
零写入的可执行契约**：

  提出更新 → 查独立行为结果 → 检反例 → 查身份一致 → 接受或暂缓 → 留版本。

  1. 目标层只允许 habit/relationship（TARGET_KINDS）。habit=从反复有结果的
     做法形成的**可撤回策略**；relationship=长期关系更新。personality/trait/
     episode/belief/self_view 一律 habit_not_personality 拒绝——信念/自我认识
     是 L02 的范围，「发生过」只能由真实事件表达，人格特质不在未经验证时改写。
  2. 证据只能是**独立的行为/互动结果**（调用方从权威行动账本只读传入的投影，
     OUTCOME_KINDS）。口癖/反复 utterance、单次心情/情绪、模型切换事件一律不是
     证据（utterance_not_evidence / mood_not_evidence / model_switch_not_evidence）：
     §11.3「不从自己反复说某个口癖推导性格」、§11.2「单次情绪不能直接改人格、
     新模型接手不自动重设长期自我」。
  3. 晋升门（§7.4，多维同时成立，不是「出现三次就是真偏好」）：≥MIN_INDEPENDENT_ROOTS
     个独立根源（同根源转述不算独立，R04/R05 同口径）+ 时间跨度 ≥MIN_SPAN_SECONDS
     + ≥MIN_SUCCESS_OUTCOMES 个真实成功结果。阈值是**冻结的开发集常量**，不运行时
     自适应。unknown/rejected/waited/failure 绝不冒充成功，但作为「为什么没做」
     逐条保留进决策记录（§11.3「记得为什么没做」）。
  4. 防确认偏误（§11.3）：反例检查是**必经门**。counter_evidence=None（读不到）
     → held（counter_evidence_unavailable），绝不把读不到当成没有反例；=[]（明确
     查过没有）才可继续；有独立根源的反证 → held（counterexample_evidence）。
     注意/目标过滤不得移除反证：counter_evidence_filtered=True → 降级并 held。
  5. 身份一致性（§7.4）：identity 基线由调用方传入（只读投影，可来自 L02 版本
     轨迹）；与已有身份冲突 → held（identity_conflict），不悄悄改写人格特质；
     缺基线 → held（identity_unavailable）并降级，不补造。本模块不调模型做文本
     矛盾检测，冲突标记由调用方如实提供。
  6. 失效单调（接 M05）：被失效/撤回的结果不得支撑接受；已接受更新的支撑结果
     后来失效 → reassessment_candidates 产出重评候选，旧版本一字节不动。复用
     mc._TRUTH_LOST 冻结常量，不另造失效引擎。
  7. 落账边界（照 L02 §5「纯计划输出 + 只读桥」）：cognition.db 没有 habit/
     relationship 表，本模块**不开任何写入口**——只产出纯计划与自声明 schema
     对齐行（habit_row/relationship_row），供主 agent 将来经唯一写入者落账。
     不写一行 SQL、不开写连接、绝不写真实 data/。
  8. 只读桥 read_update_inputs 完全委托冻结前置 dag.read_invalidation_state
     （mode=ro + query_only，DATA_DIR 调用时解析，缺库报 absent/degraded，绝不
     建库建表建目录）。行为结果暂无冻结只读桥：基线由调用方传入，如实声明不补造。

硬不变量：确定性（无墙钟无随机，不 import time/random/os，now 显式传入，ID
内容寻址 mm.digest 口径、输入顺序无关）；只追加（内存账本只追加，重复 ID/内容
哈希不符/版本断链一律拒绝，读函数纯读深拷贝）；原始结果不可改，解释分层可变。

边界（LIMITATIONS，如实声明没做的事）：不接真实 worker/生产消费者（仅可执行
契约，共享接线由主 agent 做）；不建 habit/relationship 表；不调 LLM 生成策略
文本（after/reason 由调用方传入，本模块只裁决与留版本）；不做 L04 跨时间选择
连续性验收。
"""
import copy
import json

import invalidation_dag as dag
import memory_consolidation as mc
import memory_manifest as mm

ALGORITHM_VERSION = 'l03-1'
# 目标层：习惯（可撤回策略）与长期关系。绝不接受人格特质/事件/信念/自我认识。
TARGET_KINDS = ('habit', 'relationship')
# 证据只能是独立行为/互动结果；口癖/心情/模型切换不是证据。
OUTCOME_KINDS = ('action_outcome', 'interaction_outcome')
NON_EVIDENCE_KINDS = ('utterance', 'mood', 'model_switch')
# 结果状态如实区分：只有 success 计入晋升支撑，其余作为「为什么没做」保留。
RESULT_SUCCESS = 'success'
NON_SUCCESS_RESULTS = ('failure', 'unknown', 'rejected', 'waited')
# 失效单调复用 M05/M08 冻结常量，不另立。
_TRUTH_LOST = mc._TRUTH_LOST
# 裁决三态：接受（进版本轨迹）/ 暂缓（等更多证据/不可核验）/ 拒绝（门违规）。
DECISION_STATUSES = ('accepted', 'held', 'rejected')

# 冻结的开发集阈值（§7.4）：晋升需独立根源 + 时间跨度 + 真实成功结果 + 反例检查
# + 身份一致性**多维同时成立**，不是「出现三次就是真偏好」。同根源的反复出现
# 不增加独立根源数；单次心情/口癖根本不是证据。阈值不运行时自适应。
MIN_INDEPENDENT_ROOTS = 3
MIN_SUCCESS_OUTCOMES = 3
MIN_SPAN_SECONDS = 7 * 24 * 3600

# L03 稳定 reason_code（失效/分层透传码复用 M05/M08，不另造同义码）。
REASON_CODES = (
    'missing_proposal_id',
    'missing_object_id',
    'habit_not_personality',
    'missing_after_text',
    'missing_change_reason',
    'no_change_proposed',
    'utterance_not_evidence',
    'mood_not_evidence',
    'model_switch_not_evidence',
    'no_valid_evidence',
    'evidence_invalidated',
    'insufficient_independent_roots',
    'insufficient_time_span',
    'insufficient_success_outcomes',
    'prior_invalidation_preserved',
    'counter_evidence_unavailable',
    'counter_evidence_filtered',
    'counterexample_evidence',
    'identity_unavailable',
    'identity_conflict',
    'update_accepted',
)

LIMITATIONS = (
    '不接真实 worker、不接生产消费者：仅可执行契约，共享接线由主 agent 做。',
    '不调 LLM 生成策略/关系文本：after/reason 由调用方传入，本模块只裁决独立结果'
    '纪律与留版本，不现场编成长故事。',
    '证据只能是独立行为/互动结果（OUTCOME_KINDS）；口癖/单次心情/模型切换不是证据，'
    '不从反复 utterance 推导性格，单次情绪不改人格，模型切换不重设长期自我。',
    '晋升是冻结开发集阈值的多维门（独立根源+时间跨度+真实成功结果+反例+身份一致），'
    '不是「出现三次就是真偏好」；同根源转述不算独立（R04/R05 同口径）。',
    '防确认偏误：反例检查必经，读不到反证（None）暂缓而非当成没有；注意/目标过滤'
    '移除反证则降级暂缓。unknown/rejected/waited/failure 不冒充成功，但保留为什么没做。',
    '失效单调（接 M05）：失效/撤回结果不得支撑接受；已接受更新的支撑失效后产出'
    '重评候选，绝不篡改历史。',
    '版本只增不覆盖：append-only，重复 ID/内容哈希不符/版本断链拒绝；'
    '「什么改变了/为什么没做」只从记录回答，缺证据如实说不知道。',
    '落账为纯计划输出 + 只读桥：cognition.db 尚无 habit/relationship 表，本模块只产出'
    '自声明 schema 对齐行供主 agent 经唯一写入者落账，自身不写 SQL、不开写连接、不建表。',
    '身份一致性由调用方传入的基线冲突标记判定，本模块不调模型做文本矛盾检测；'
    '缺基线如实降级暂缓，不补造。',
    '确定性：无墙钟无随机，now 显式必填，update_id/plan_id 内容寻址（mm.digest 口径）；'
    '只读桥委托 M05，缺库报 absent/degraded，绝不建库建表建目录。',
    '不做 L04 跨时间选择连续性验收，不做 C04 表达注入。',
)


# ── 小工具（与 L02/G03 同一口径，非引擎）────────────────────────────


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_now(now):
    """now 必须显式传入：本模块不读墙上时钟（无 time import），决策可复现。"""
    if not _number(now):
        raise ValueError('habit/relationship plan requires an explicit numeric now; no wall clock')
    return float(now)


def _lost_ids(states):
    return {oid for oid, st in (states or {}).items()
            if isinstance(st, dict) and st.get('status') in _TRUTH_LOST}


def _outcome_lost(outcome, lost):
    oid = _text(outcome.get('outcome_id'))
    return bool(outcome.get('retracted')) or oid in lost


def _update_id(decision):
    """update_id 内容寻址：不含 ts/now，同内容跨时钟必同 ID（mm.digest 口径）。"""
    core = {k: v for k, v in decision.items() if k not in ('ts', 'update_id')}
    return 'l03_' + mm.digest(_canonical(core))[:14]


def _plan_id(decisions):
    rows = sorted(({k: v for k, v in d.items() if k != 'ts'} for d in decisions),
                  key=lambda d: d['update_id'])
    return 'l03p_' + mm.digest(_canonical(rows))[:14]


# ── 内存账本：只追加，读函数纯读（L02/L01/M08 先例）────────────────


def new_ledger():
    """空白更新账本。调用方持有；本模块不落盘、不解析路径。"""
    return {'records': {}, 'order': []}


def _check_ledger(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('records'), dict)
            or not isinstance(ledger.get('order'), list)):
        raise ValueError('ledger must come from new_ledger()')


def _trail(ledger, target_kind, target_id):
    """某目标的已接受版本轨迹（按提交序）。只数 accepted，held/rejected 不进版本。"""
    out = []
    if ledger is None:
        return out
    _check_ledger(ledger)
    for uid in ledger['order']:
        record = ledger['records'][uid]
        if (record.get('status') == 'accepted'
                and record.get('target_kind') == target_kind
                and record.get('target_id') == target_id):
            out.append(record)
    return out


def _baseline(target_kind, target_id, habits, relationships):
    """版本 1 的 before 基线：取调用方传入的当前策略/关系描述。
    找不到基线就是空串（首次形成），绝不补造旧习惯。"""
    rows = habits if target_kind == 'habit' else relationships
    for row in (rows or []):
        if isinstance(row, dict) and _text(row.get('target_id') or row.get('id')) == target_id:
            return _text(row.get('text') or row.get('description'))
    return ''


# ── 证据解析：行为结果投影，逐字段如实 ─────────────────────────────


def _outcome_index(outcomes):
    out = {}
    for row in (outcomes or []):
        if isinstance(row, dict) and _text(row.get('outcome_id')):
            out[_text(row['outcome_id'])] = row
    return out


def _resolve_support(prop, index):
    """支撑结果：缺省用提案引用的全部结果；调用方只能收窄，不能另找证据。
    返回 (behavioral, non_evidence)：行为结果 vs 口癖/心情/模型切换。"""
    if 'support_outcome_ids' in prop:
        ids = sorted({_text(s) for s in (prop.get('support_outcome_ids') or []) if _text(s)})
    else:
        ids = sorted({_text(s) for s in (prop.get('outcome_ids') or []) if _text(s)})
    behavioral, non_evidence = [], []
    for oid in ids:
        row = index.get(oid)
        if row is None:
            continue
        kind = _text(row.get('kind'))
        if kind in NON_EVIDENCE_KINDS:
            non_evidence.append(row)
        elif kind in OUTCOME_KINDS:
            behavioral.append(row)
    return behavioral, non_evidence


# ── 纯预检：门序裁决（不含落账）────────────────────────────────────


def _decide(prop, ctx):
    """纯函数：判定一条习惯/关系更新提案。门序见模块 docstring；held/rejected
    必带稳定 reason_code。version/before 由调用方按版本轨迹顺序传入。"""
    p = prop if isinstance(prop, dict) else {}
    pid = _text(p.get('proposal_id'))
    kind = _text(p.get('target_kind'))
    target = _text(p.get('target_id'))
    after = _text(p.get('after'))
    reason = _text(p.get('reason'))
    base = {'proposal_id': pid, 'target_kind': kind, 'target_id': target,
            'version': 0, 'status': 'rejected', 'reason_code': '', 'reasons': [],
            'before': ctx['before'], 'after': after, 'reason': reason,
            'outcome_ids': [], 'evidence': [], 'root_groups': [],
            'independent_sources': 0, 'success_count': 0, 'span_seconds': 0.0,
            'non_success_outcomes': [], 'counterexamples': [],
            'counterexample_result': 'not_checked', 'identity_result': 'not_checked',
            'update_id': '', 'ts': ctx['now']}

    def out(status, code, reasons=(), **extra):
        got = dict(base, status=status, reason_code=code, reasons=list(reasons))
        got.update(extra)
        got['update_id'] = _update_id(got)
        return got

    # ── 身份证 ──
    if not pid:
        return out('rejected', 'missing_proposal_id')
    if not target:
        return out('rejected', 'missing_object_id')
    # ── 目标层：习惯/关系 only，绝不学人格特质/制造「发生过」──
    if kind not in TARGET_KINDS:
        return out('rejected', 'habit_not_personality')
    if not after:
        return out('rejected', 'missing_after_text')
    if not reason:
        return out('rejected', 'missing_change_reason')

    # ── 证据类型门：口癖/单次心情/模型切换不是证据 ──
    behavioral, non_evidence = _resolve_support(p, ctx['index'])
    if not behavioral:
        if non_evidence:
            first = _text(non_evidence[0].get('kind'))
            code = {'utterance': 'utterance_not_evidence',
                    'mood': 'mood_not_evidence',
                    'model_switch': 'model_switch_not_evidence'}.get(first, 'no_valid_evidence')
            return out('rejected', code)
        return out('held', 'no_valid_evidence')

    # ── 失效单调：失效/撤回的结果不得支撑（复用 M05 口径）──
    valid = [r for r in behavioral if not _outcome_lost(r, ctx['lost'])]
    if not valid:
        return out('held', 'evidence_invalidated')

    # ── 独立根源 + 时间跨度 + 真实成功结果（多维门，非出现次数）──
    roots = sorted({_text(r.get('root_id')) or _text(r.get('outcome_id')) for r in valid})
    times = [float(r.get('ts')) for r in valid if _number(r.get('ts'))]
    span = (max(times) - min(times)) if len(times) >= 2 else 0.0
    success = [r for r in valid if _text(r.get('result')) == RESULT_SUCCESS]
    non_success = [{'outcome_id': _text(r.get('outcome_id')),
                    'result': _text(r.get('result')), 'root_id': _text(r.get('root_id'))}
                   for r in valid if _text(r.get('result')) != RESULT_SUCCESS]
    non_success.sort(key=lambda r: (r['outcome_id'], r['result']))
    accounting = {'outcome_ids': sorted(_text(r.get('outcome_id')) for r in valid),
                  'evidence': copy.deepcopy(valid), 'root_groups': roots,
                  'independent_sources': len(roots), 'success_count': len(success),
                  'span_seconds': span, 'non_success_outcomes': non_success}
    if len(roots) < MIN_INDEPENDENT_ROOTS:
        return out('held', 'insufficient_independent_roots', **accounting)
    if span < MIN_SPAN_SECONDS:
        return out('held', 'insufficient_time_span', **accounting)
    if len(success) < MIN_SUCCESS_OUTCOMES:
        return out('held', 'insufficient_success_outcomes', **accounting)

    # ── 目标失效单调：已失效的对象绝不借更新复活 ──
    if target in ctx['lost']:
        return out('held', 'prior_invalidation_preserved', **accounting)
    if after == ctx['before']:
        return out('rejected', 'no_change_proposed', **accounting)

    # ── 反例检查（必经门，防确认偏误）──
    counters = p.get('counter_evidence')
    if p.get('counter_evidence_filtered'):
        return out('held', 'counter_evidence_filtered', **accounting)
    if counters is None:
        return out('held', 'counter_evidence_unavailable', **accounting)
    support_roots = set(roots)
    examined = []
    for counter in counters:
        if not isinstance(counter, dict):
            continue
        cid = _text(counter.get('outcome_id'))
        if not cid:
            continue
        croot = _text(counter.get('root_id')) or cid
        if _outcome_lost(counter, ctx['lost']):
            disposition = 'dismissed_invalidated'
        elif croot in support_roots:
            disposition = 'dismissed_same_root'
        else:
            disposition = 'independent_counterexample'
        examined.append({'outcome_id': cid, 'disposition': disposition})
    examined.sort(key=lambda c: c['outcome_id'])
    independent = [c for c in examined if c['disposition'] == 'independent_counterexample']
    counter_result = 'none_found'
    if independent:
        counter_result = 'blocked_evidence'
    elif examined:
        counter_result = 'dismissed_only'
    extra = dict(accounting, counterexamples=examined, counterexample_result=counter_result)
    if independent:
        return out('held', 'counterexample_evidence',
                   [c['outcome_id'] for c in independent], **extra)

    # ── 身份一致性（§7.4）：缺基线降级暂缓，冲突暂缓，不悄悄改人格 ──
    identity = ctx['identity']
    if identity is None:
        return out('held', 'identity_unavailable',
                   counterexample_result=counter_result, counterexamples=examined,
                   identity_result='unavailable', **accounting)
    conflict_targets = {_text(t) for t in (identity.get('conflict_targets') or [])}
    conflict_after = {_text(t) for t in (identity.get('conflict_after') or [])}
    if target in conflict_targets or after in conflict_after:
        return out('held', 'identity_conflict',
                   counterexample_result=counter_result, counterexamples=examined,
                   identity_result='conflict', **accounting)
    extra = dict(accounting, counterexamples=examined,
                 counterexample_result=counter_result, identity_result='consistent')
    # ── 接受：进版本轨迹（版本号由调用方按轨迹顺序给出）──
    return out('accepted', 'update_accepted', version=ctx['version'], **extra)


def update_plan(proposals, outcomes=None, *, ledger=None, habits=None,
                relationships=None, states=None, identity=None, now):
    """纯函数：产出习惯/关系更新计划，不写任何东西。

    proposals:     更新提案 {proposal_id, target_kind∈habit/relationship, target_id,
                   after, reason, 可选 support_outcome_ids（只能收窄引用）、
                   counter_evidence[]|None、counter_evidence_filtered}。after/reason
                   由调用方传入，本模块不调 LLM。
    outcomes:      权威行动账本的只读结果投影 [{outcome_id, root_id, ts, kind, result,
                   retracted}]。None/缺省=没有结果（不是读不到账本的借口）。
    ledger:        既往更新账本（new_ledger）；提供版本轨迹。
    habits/relationships: 当前策略/关系行（target_id/text）：before 基线。
    states:        M05 对象状态（invalidation_dag.read_invalidation_state）。
    identity:      身份基线只读投影 {statements, conflict_targets, conflict_after}；
                   None=读不到（暂缓，不补造）。
    now:           显式时间（必填；决策路径与 ID 不含墙上时钟）。

    返回 {plan_id, algorithm_version, status, decisions, accepted, held,
          rejected, counts, provenance, limitations}。
    """
    at = _require_now(now)
    rows = sorted((p for p in (proposals or []) if isinstance(p, dict)),
                  key=lambda p: _text(p.get('proposal_id')))
    unique = {}
    for prop in rows:
        unique.setdefault(_text(prop.get('proposal_id')), prop)
    rows = [unique[pid] for pid in sorted(unique)]
    index = _outcome_index(outcomes)
    prior = states if isinstance(states, dict) else {}
    lost = _lost_ids(prior)
    degradation = []
    if identity is None:
        degradation.append({'store': 'identity', 'reason': 'unavailable'})

    # 版本轨迹按提案顺序推进：同一目标多条提案时，后一条的 before 是前一条
    # 接受后的 after（计划内也保持 append-only 语义）。
    working = {}
    decisions = []
    for prop in rows:
        kind, target = _text(prop.get('target_kind')), _text(prop.get('target_id'))
        key = (kind, target)
        if key not in working:
            trail = _trail(ledger, kind, target)
            before = trail[-1]['after'] if trail else _baseline(
                kind, target, habits, relationships)
            working[key] = {'before': before, 'version': len(trail) + 1}
        ctx = {'index': index, 'lost': lost, 'identity': identity, 'now': at,
               'before': working[key]['before'], 'version': working[key]['version']}
        decision = _decide(prop, ctx)
        decisions.append(decision)
        if decision['status'] == 'accepted':
            working[key] = {'before': decision['after'],
                            'version': working[key]['version'] + 1}

    accepted = [d for d in decisions if d['status'] == 'accepted']
    held = [d for d in decisions if d['status'] == 'held']
    rejected = [d for d in decisions if d['status'] == 'rejected']
    return {'plan_id': _plan_id(decisions), 'algorithm_version': ALGORITHM_VERSION,
            'status': 'degraded' if degradation else 'planned',
            'decisions': decisions, 'accepted': accepted, 'held': held,
            'rejected': rejected,
            'counts': {'accepted': len(accepted), 'held': len(held),
                       'rejected': len(rejected)},
            'provenance': {'method': 'versioned_update_from_independent_outcomes_only',
                           'algorithm_version': ALGORITHM_VERSION,
                           'llm_update': False, 'outcome_count': len(index),
                           'min_independent_roots': MIN_INDEPENDENT_ROOTS,
                           'min_success_outcomes': MIN_SUCCESS_OUTCOMES,
                           'min_span_seconds': MIN_SPAN_SECONDS,
                           'degradation': degradation},
            'limitations': list(LIMITATIONS)}


def commit_update(ledger, plan):
    """把更新计划追加进内存账本（accepted/held/rejected 全留审计，版本轨迹
    只数 accepted）。重复 ID / 内容哈希不符 / 版本断链一律拒绝，先整体校验
    再追加，绝不落半条计划。"""
    _check_ledger(ledger)
    if not isinstance(plan, dict) or plan.get('status') not in ('planned', 'committed', 'degraded'):
        raise ValueError('plan must come from update_plan')
    decisions = plan.get('decisions')
    if not isinstance(decisions, list):
        raise ValueError('plan requires decisions')
    pid = _text(plan.get('plan_id'))
    if not pid or pid != _plan_id(decisions):
        raise ValueError('plan_id does not match its content')
    seen = set()
    trails = {}
    for decision in decisions:
        uid = _text(decision.get('update_id'))
        if not uid or uid != _update_id(decision):
            raise ValueError('update_id does not match its content')
        if uid in ledger['records'] or uid in seen:
            raise ValueError('record already exists; history is append-only: %s' % uid)
        seen.add(uid)
        if decision.get('status') == 'accepted':
            key = (decision.get('target_kind'), decision.get('target_id'))
            if key not in trails:
                trails[key] = _trail(ledger, *key)
            trail = trails[key]
            expected_version = len(trail) + 1
            if decision.get('version') != expected_version:
                raise ValueError('version breaks the append-only trail')
            if trail and decision.get('before') != trail[-1]['after']:
                raise ValueError('before-text does not match version trail')
            trail.append(decision)
    records = []
    for decision in decisions:
        record = copy.deepcopy(decision)
        record['plan_id'] = pid
        record['committed'] = True
        ledger['records'][record['update_id']] = record
        ledger['order'].append(record['update_id'])
        records.append(copy.deepcopy(record))
    return records


# ── 纯读视图：GET 不得启动任何写流程（L02/L01/M08 先例）────────────


def get_record(ledger, update_id):
    _check_ledger(ledger)
    record = ledger['records'].get(_text(update_id))
    return copy.deepcopy(record) if record is not None else None


def list_records(ledger):
    _check_ledger(ledger)
    return list(ledger['order'])


def version_trail(ledger, target_kind, target_id):
    """某目标的已接受版本轨迹（深拷贝，按提交序）。held/rejected 不进轨迹。"""
    if target_kind not in TARGET_KINDS:
        raise ValueError('unknown target kind: %s' % target_kind)
    return copy.deepcopy(_trail(ledger, target_kind, _text(target_id)))


def what_changed(ledger, target_kind, target_id, *, states=None):
    """回答「什么改变了你的习惯/关系」：答案只来自更新记录与支撑结果引用。

    没有记录 / 记录缺支撑证据时如实说不知道（known=False），绝不现场编故事。
    支撑结果后来失效的，如实标注待重评，历史不改。"""
    if target_kind not in TARGET_KINDS:
        raise ValueError('unknown target kind: %s' % target_kind)
    target = _text(target_id)
    trail = _trail(ledger, target_kind, target)
    lost = _lost_ids(states)
    changes = []
    for record in trail:
        invalidated = sorted(set(record.get('outcome_ids') or []) & lost)
        changes.append({'version': record['version'], 'before': record['before'],
                        'after': record['after'], 'reason': record['reason'],
                        'outcome_ids': list(record.get('outcome_ids') or []),
                        'update_id': record['update_id'],
                        'counterexample_result': record.get('counterexample_result', ''),
                        'invalidated_sources': invalidated})
    base = {'known': False, 'reason_code': '', 'target_kind': target_kind,
            'target_id': target, 'changes': changes, 'answer': ''}
    if not changes:
        return dict(base, reason_code='no_update_record',
                    answer='没有更新记录，不知道是什么改变了习惯或关系，不编造。')
    if any(not c['outcome_ids'] or not c['reason'] for c in changes):
        return dict(base, reason_code='no_evidence_recorded',
                    answer='更新记录缺支撑结果或理由，不知道是什么改变了，不编造。')
    lines = []
    for change in changes:
        line = '第%d版把「%s」改成「%s」，理由：%s；依据结果：%s' % (
            change['version'], change['before'], change['after'],
            change['reason'], '、'.join(change['outcome_ids']))
        if change['invalidated_sources']:
            line += '（支撑结果 %s 后来失效，待重评，历史不改）' % '、'.join(
                change['invalidated_sources'])
        lines.append(line)
    return dict(base, known=True, reason_code='answered_from_records',
                answer='\n'.join(lines))


def why_not_done(ledger, target_kind, target_id):
    """记得「为什么没做」（§11.3）：返回某目标 held/rejected 的决策与稳定原因，
    保留拒绝、等待、未知结果与放弃原因，避免重启后重复尝试不该做的行动。纯读。"""
    if target_kind not in TARGET_KINDS:
        raise ValueError('unknown target kind: %s' % target_kind)
    target = _text(target_id)
    _check_ledger(ledger)
    out = []
    for uid in ledger['order']:
        record = ledger['records'][uid]
        if (record.get('target_kind') == target_kind
                and record.get('target_id') == target
                and record.get('status') in ('held', 'rejected')):
            out.append({'update_id': uid, 'status': record['status'],
                        'reason_code': record.get('reason_code', ''),
                        'reasons': list(record.get('reasons') or []),
                        'non_success_outcomes': copy.deepcopy(
                            record.get('non_success_outcomes') or [])})
    return out


def reassessment_candidates(ledger, states=None):
    """纯函数：已接受更新的支撑结果后来失效 → 产出重评候选，绝不改历史。

    失效判定复用 M05 同口径（states 失效 + 结果自带 retracted）。返回按
    (target_kind, target_id, version) 排序的候选列表；账本一字节不动。
    重评本身要走新一轮更新提案，不在这里悄悄复活或改写。"""
    _check_ledger(ledger)
    lost = _lost_ids(states)
    out = []
    for uid in ledger['order']:
        record = ledger['records'][uid]
        if record.get('status') != 'accepted':
            continue
        supports = list(record.get('outcome_ids') or [])
        evidence = record.get('evidence') or []
        retracted = {_text(r.get('outcome_id')) for r in evidence
                     if isinstance(r, dict) and r.get('retracted')}
        lost_sources = sorted(s for s in supports if s in lost or s in retracted)
        if not lost_sources:
            continue
        out.append({'update_id': uid, 'target_kind': record['target_kind'],
                    'target_id': record['target_id'], 'version': record['version'],
                    'lost_sources': lost_sources,
                    'remaining_sources': sorted(set(supports) - set(lost_sources)),
                    'reason_code': 'support_invalidated', 'action': 'reassess',
                    'history': 'preserved'})
    out.sort(key=lambda c: (c['target_kind'], c['target_id'], c['version']))
    return out


# ── schema 对齐行：供主 agent 将来经唯一写入者落账（本模块不写、不建表）──


def habit_row(record):
    """accepted habit 更新 → 自声明 schema 对齐行（cognition.db 尚无 habit 表）。"""
    if record.get('status') != 'accepted' or record.get('target_kind') != 'habit':
        raise ValueError('habit_row requires an accepted habit update')
    return {'id': record['update_id'], 'target_id': record['target_id'],
            'old_text': record['before'], 'new_text': record['after'],
            'reason': record['reason'], 'outcome_ids': list(record['outcome_ids']),
            'ts': record['ts']}


def relationship_row(record):
    """accepted relationship 更新 → 自声明 schema 对齐行（尚无 relationship 表）。"""
    if record.get('status') != 'accepted' or record.get('target_kind') != 'relationship':
        raise ValueError('relationship_row requires an accepted relationship update')
    return {'id': record['update_id'], 'target_id': record['target_id'],
            'old_text': record['before'], 'new_text': record['after'],
            'reason': record['reason'], 'outcome_ids': list(record['outcome_ids']),
            'ts': record['ts']}


def ledger_rows(ledger):
    """全部已接受更新的 schema 对齐行（按版本轨迹序）。纯读，不落账、不建表。"""
    _check_ledger(ledger)
    out = {'habit': [], 'relationship': []}
    for uid in ledger['order']:
        record = ledger['records'][uid]
        if record.get('status') != 'accepted':
            continue
        if record['target_kind'] == 'habit':
            out['habit'].append(habit_row(record))
        else:
            out['relationship'].append(relationship_row(record))
    return out


# ── 只读桥：完全委托冻结前置，缺库报 absent/degraded，绝不建库 ─────


def read_update_inputs():
    """只读收集更新输入：M05 状态/边/epoch。

    完全复用 M05.read_invalidation_state（mode=ro + query_only，DATA_DIR 调用时
    解析，缺库报 absent，绝不补造）。行为结果暂无冻结只读桥：基线由调用方传入，
    这里如实返回空并声明，不另开读取口。"""
    state = dag.read_invalidation_state()
    return {'outcomes': [], 'states': state['states'], 'edges': state['edges'],
            'epoch': state['epoch'], 'degradation': [],
            'outcome_note': '行为结果暂无冻结只读桥，基线由调用方从权威行动账本传入'}