# -*- coding: utf-8 -*-
"""V04 四种运行模式观测隔离（路线 §9 验收 / §4.3 确定性重放，S01 口径之上的分拣契约）。

四种运行模式/轨迹已由 emotion_trajectories.CONTRACTS 固定：
- v1_reference：旧冻结内核与合成参考回放，历史保留，不计在线；
- v2_live：当前严格规则与动力学观察，唯一计入在线有效证据的轨迹；
- candidate_counterfactual：候选反事实回放，不计在线、不回写实时状态；
- historical_replay：v2 源账本的只读追溯视图，不计在线。

本模块回答「观测隔离」：口径字段 trajectory/kernel_version/counts_for_live 由
S01 契约盖章（stamp），不由报告生产者自报；非在线产物不得写入或冒充在线观察。
三层纯函数：
  certify(report, trajectory)   按契约给报告盖章（深拷贝，未知轨迹大声拒绝）。
  classify(item)                识别真实生产者的产物形状并锁口径：候选回放 run/
                                脱敏视图/valence_samples、实时 shadow status、
                                历史 retrospective、S06 回放矩阵、S03 修订重放、
                                v1 合成参考、R07 消融评审包、S08 隔离实验计划/
                                结果/评审包。识别出的产物按形状锁校验，任何
                                counts_for_live=True / expression_control=True
                                的冒充形状一律拒绝；未识别但带 trajectory 的
                                普通报告按契约重盖（自报在线被覆盖），缺字段不
                                补造、未知轨迹大声拒绝。
  sort_observations(items)      跨模式污染检查：混合报告/产物稳定分拣为
                                live（可计入在线）/ observation_only（仅观测）/
                                manual_review（待人工评审）/ rejected（形状违规）
                                四类，稳定 reason_code，保持输入顺序，不改输入。

硬边界（明确不做）：
- 纯契约：不接真实生产消费者，不读写任何账本/存储，不解析路径，不建库建表建目录；
  无墙上时钟、无随机、不发网络、不调 LLM、不触发任何 QQ 发送。
- 不接管表达、不改任何现有模块的行为、不新增第二套动力学；口径唯一来源是
  emotion_trajectories.CONTRACTS，本模块只复用它的 stamp，不另写口径表。
- 不读生产账本：真实产物由调用方传入（测试用隔离临时目录与合成夹具）。
- 分拣只是形状/口径隔离判定，不证明报告内容真实、不证明生产合规、不证明主观
  体验；R07/S08 是 manual_review 材料，契约可执行 ≠ 真实消融/实验已做，organic
  结论永远留给人工评审（authorization=manual_review、organic=not_observed）。
- 缺字段不补造、unknown 不冒充 pass：无法识别的输入一律 rejected 并出声。
"""
import copy

import emotion_trajectories as trajectories

V1_REFERENCE = trajectories.V1_REFERENCE
V2_LIVE = trajectories.V2_LIVE
CANDIDATE_COUNTERFACTUAL = trajectories.CANDIDATE_COUNTERFACTUAL
HISTORICAL_REPLAY = trajectories.HISTORICAL_REPLAY
TRAJECTORIES = (V1_REFERENCE, V2_LIVE, CANDIDATE_COUNTERFACTUAL, HISTORICAL_REPLAY)

# 分拣四类：可计入在线 / 仅观测 / 待人工评审 / 拒绝（形状违规）。
BUCKETS = ('live', 'observation_only', 'manual_review', 'rejected')
# 评审口径与 V02/R07/S08 相同：manual_review 任务，organic 留 not_observed。
REVIEW_AUTHORIZATION = 'manual_review'
REVIEW_ORGANIC = 'not_observed'
# 历史/参考类产物允许的轨迹（S03/S08 按内核分型：v1→参考，v2→历史追溯）。
SHADOW_TRAJECTORIES = (V1_REFERENCE, HISTORICAL_REPLAY)

LIMITATIONS = (
    '纯契约：不接真实生产消费者，不读写账本/存储，不解析路径，无时钟无随机，'
    '不发网络、不调 LLM；真实产物由调用方传入，本模块不读生产账本。',
    '不接管表达、不改任何现有模块的行为、不新增第二套动力学；口径唯一来源是 '
    'emotion_trajectories.CONTRACTS，counts_for_live 由契约盖章不由生产者自报。',
    '分拣只是形状/口径隔离判定：不证明报告内容真实、不证明生产账本已合规、'
    '不证明主观体验；生产审计走 phase_audit 只读快照。',
    'R07/S08 评审包是 manual_review 材料：契约可执行 ≠ 真实消融/实验已做；'
    'organic=not_observed、authorization=manual_review，结论永远留给人工评审。',
    '缺字段不补造、unknown 不冒充 pass：缺 trajectory、未知轨迹、无法识别的'
    '输入一律 rejected 并给稳定 reason_code，绝不静默放行。',
    '识别出的生产者产物按形状锁校验（候选/历史/参考/评审包不得携带 '
    'counts_for_live=True 或 expression_control=True）；普通轨迹报告按契约重盖，'
    '自报在线被覆盖。两条路径都不允许非在线产物计入在线。',
)


def _text(value):
    return str(value or '').strip()


# ── 盖章：口径由契约决定，深拷贝不改输入 ─────────────────────────────


def certify(report, trajectory):
    """按 S01 契约给报告盖统一口径，返回深拷贝；未知轨迹/非 dict 大声拒绝。

    counts_for_live/kernel_version 以 CONTRACTS 为准，覆盖调用方自带的值。
    本函数不判断报告内容真假，只保证口径字段出自契约而非生产者自报。
    """
    if not isinstance(report, dict):
        raise ValueError('report must be a dict')
    if trajectory not in trajectories.CONTRACTS:
        raise ValueError('unknown_trajectory: %r' % (trajectory,))
    return trajectories.stamp(copy.deepcopy(report), trajectory)


# ── 产物形状锁：识别真实生产者的输出并校验口径 ───────────────────────

# 每种产物的必备字段；缺字段给稳定 missing_<field> 码，不补造。
REQUIRED = {
    'candidate_run': ('run_id', 'branch', 'trajectory', 'kernel_version',
                      'counts_for_live', 'expression_control', 'state',
                      'timeline', 'stats', 'checks'),
    'candidate_view': ('trajectory', 'kernel_version', 'counts_for_live',
                       'expression_control', 'redaction', 'reason_codes', 'stats'),
    'valence_samples': ('mode', 'trajectory', 'kernel_version', 'counts_for_live',
                        'expression_control', 'stats', 'rows', 'state'),
    'shadow_status': ('mode', 'trajectory', 'kernel_version', 'counts_for_live',
                      'expression_control', 'timeline', 'parameter_hash'),
    'retrospective': ('mode', 'trajectory', 'kernel_version', 'counts_for_live'),
    'synthetic_reference': ('mode', 'trajectory', 'kernel_version',
                            'counts_for_live', 'parameter_hash'),
    'matrix_report': ('status', 'cases', 'trajectory', 'counts_for_live',
                      'expression_control', 'provenance', 'limitations'),
    'revision_run': ('revision_id', 'branch', 'trajectory', 'kernel_version',
                     'counts_for_live', 'expression_control', 'state', 'timeline'),
    'r07_review_packet': ('plan', 'variants', 'diffs', 'review', 'limitations'),
    's08_review_package': ('experiment_id', 'plan', 'arms', 'diff', 'checks',
                           'organic', 'authorization', 'provenance', 'limitations',
                           'trajectory', 'counts_for_live', 'expression_control'),
    's08_experiment_result': ('experiment_id', 'arms', 'diff', 'checks',
                              'trajectory', 'counts_for_live', 'expression_control'),
    's08_plan': ('experiment_id', 'base_events', 'recall_events', 'recall_index',
                 'initial_state', 'arms', 'expression_control', 'now'),
}


def _missing(item, kind):
    return ['missing_' + field for field in REQUIRED[kind] if field not in item]


def _caliber(item, trajectory, live, expression, kernel=True):
    """单一轨迹产物的口径校验：字段必须恰好等于契约值（缺/错都出声）。"""
    codes = []
    if item.get('trajectory') != trajectory:
        codes.append('trajectory_mismatch')
    elif kernel and item.get('kernel_version') != trajectories.CONTRACTS[trajectory][0]:
        codes.append('kernel_version_mismatch')
    if item.get('counts_for_live') is not live:
        codes.append('counts_for_live_mismatch')
    if expression is False:
        if item.get('expression_control') is not False:
            codes.append('expression_control_mismatch')
    elif expression is None and 'expression_control' in item:
        if item['expression_control'] is not False:
            codes.append('expression_control_mismatch')
    return codes


def _shadow_caliber(item, kernel=True):
    """历史/参考类产物：轨迹必须在影子集合内，且内核版本与轨迹契约一致。

    kernel=False 用于顶层不带 kernel_version 的产物（S08 结果/评审包，内核
    版本在 arms/plan 内），只锁轨迹集合与在线/表达口径。
    """
    codes = []
    trajectory = item.get('trajectory')
    if trajectory not in SHADOW_TRAJECTORIES:
        codes.append('trajectory_mismatch')
    elif kernel and item.get('kernel_version') != trajectories.CONTRACTS[trajectory][0]:
        codes.append('kernel_version_mismatch')
    if item.get('counts_for_live') is not False:
        codes.append('counts_for_live_mismatch')
    if item.get('expression_control') is not False:
        codes.append('expression_control_mismatch')
    return codes


def _live_claims(node):
    """递归扫描：非在线产物任何层级都不得携带在线/表达控制自报。"""
    codes = set()
    if isinstance(node, dict):
        if node.get('counts_for_live') is True:
            codes.add('counts_for_live_true_present')
        if node.get('expression_control') is True:
            codes.add('expression_control_true_present')
        for value in node.values():
            codes |= _live_claims(value)
    elif isinstance(node, list):
        for value in node:
            codes |= _live_claims(value)
    return codes


def _validate_candidate_run(item):
    codes = _missing(item, 'candidate_run')
    codes += _caliber(item, CANDIDATE_COUNTERFACTUAL, False, False)
    if not _text(item.get('run_id')).startswith('cand_'):
        codes.append('run_id_malformed')
    return codes + sorted(_live_claims(item))


def _validate_candidate_view(item):
    codes = _missing(item, 'candidate_view')
    codes += _caliber(item, CANDIDATE_COUNTERFACTUAL, False, False)
    redaction = item.get('redaction')
    if not isinstance(redaction, dict) or redaction.get('applied') is not True:
        codes.append('redaction_malformed')
    if not isinstance(item.get('reason_codes'), list):
        codes.append('reason_codes_malformed')
    return codes + sorted(_live_claims(item))


def _validate_valence(item):
    codes = _missing(item, 'valence_samples')
    codes += _caliber(item, CANDIDATE_COUNTERFACTUAL, False, False)
    return codes + sorted(_live_claims(item))


def _validate_status(item):
    # 唯一在线产物：顶层 counts_for_live=True 是契约事实；嵌套的历史/候选
    # 报告必须各自守住非在线口径，不得借实时报告冒充在线。
    codes = _missing(item, 'shadow_status')
    codes += _caliber(item, V2_LIVE, True, False)
    retro = item.get('retrospective')
    if retro is not None:
        if (not isinstance(retro, dict)
                or retro.get('trajectory') != HISTORICAL_REPLAY
                or retro.get('counts_for_live') is not False
                or retro.get('kernel_version') != trajectories.CONTRACTS[HISTORICAL_REPLAY][0]):
            codes.append('nested_retrospective_mismatch')
    valence = item.get('valence_samples')
    if valence is not None:
        if (not isinstance(valence, dict)
                or valence.get('trajectory') != CANDIDATE_COUNTERFACTUAL
                or valence.get('counts_for_live') is not False
                or valence.get('expression_control') is not False):
            codes.append('nested_valence_mismatch')
    return codes


def _validate_retrospective(item):
    codes = _missing(item, 'retrospective')
    codes += _caliber(item, HISTORICAL_REPLAY, False, None)
    return codes + sorted(_live_claims(item))


def _validate_reference(item):
    codes = _missing(item, 'synthetic_reference')
    codes += _caliber(item, V1_REFERENCE, False, None)
    return codes + sorted(_live_claims(item))


def _validate_matrix(item):
    codes = _missing(item, 'matrix_report')
    codes += _caliber(item, HISTORICAL_REPLAY, False, False, kernel=False)
    provenance = item.get('provenance')
    if (not isinstance(provenance, dict)
            or any(provenance.get(key) is not False
                   for key in ('network', 'production_ledger', 'llm_reinterpretation'))):
        codes.append('provenance_mismatch')
    return codes + sorted(_live_claims(item))


def _validate_revision(item):
    codes = _missing(item, 'revision_run')
    codes += _shadow_caliber(item)
    if not _text(item.get('revision_id')).startswith('emo_'):
        codes.append('revision_id_malformed')
    return codes + sorted(_live_claims(item))


def _validate_r07(item):
    codes = _missing(item, 'r07_review_packet')
    plan = item.get('plan')
    if not isinstance(plan, dict) or not _text(plan.get('plan_id')).startswith('plan_'):
        codes.append('plan_malformed')
    if not isinstance(item.get('variants'), list):
        codes.append('variants_malformed')
    if not isinstance(item.get('diffs'), list):
        codes.append('diffs_malformed')
    review = item.get('review')
    if not isinstance(review, dict):
        codes.append('review_malformed')
    else:
        if review.get('authorization') != REVIEW_AUTHORIZATION:
            codes.append('review_authorization_mismatch')
        if review.get('organic') != REVIEW_ORGANIC:
            codes.append('review_organic_mismatch')
        if review.get('judgement') is not None:
            codes.append('review_judgement_not_none')
    return codes + sorted(_live_claims(item))


def _validate_s08_package(item):
    codes = _missing(item, 's08_review_package')
    codes += _shadow_caliber(item, kernel=False)
    if not _text(item.get('experiment_id')).startswith('s08_'):
        codes.append('experiment_id_malformed')
    if item.get('organic') != REVIEW_ORGANIC:
        codes.append('organic_mismatch')
    if item.get('authorization') != REVIEW_AUTHORIZATION:
        codes.append('authorization_mismatch')
    provenance = item.get('provenance')
    if (not isinstance(provenance, dict)
            or any(provenance.get(key) is not False
                   for key in ('production_ledger', 'network',
                               'llm_evaluation', 'expression_takeover'))):
        codes.append('provenance_mismatch')
    arms = item.get('arms')
    if isinstance(arms, dict):
        for arm in ('control', 'recall'):
            row = arms.get(arm)
            if (not isinstance(row, dict)
                    or row.get('counts_for_live') is not False
                    or row.get('expression_control') is not False
                    or row.get('trajectory') not in SHADOW_TRAJECTORIES):
                codes.append('arm_caliber_mismatch')
    else:
        codes.append('arms_malformed')
    return codes + sorted(_live_claims(item))


def _validate_s08_result(item):
    codes = _missing(item, 's08_experiment_result')
    codes += _shadow_caliber(item, kernel=False)
    if not _text(item.get('experiment_id')).startswith('s08_'):
        codes.append('experiment_id_malformed')
    checks = item.get('checks')
    if not isinstance(checks, dict):
        codes.append('checks_malformed')
    elif checks.get('trajectory_is_shadow') is not True:
        codes.append('shadow_check_failed')
    return codes + sorted(_live_claims(item))


def _validate_s08_plan(item):
    # 实验输入：回忆事件必须是「检索成功」tool_result，不得伪装成新亲历消息，
    # 不得携带在线自报；计划本身不回写 live 账本（纯内存，由调用方持有）。
    codes = _missing(item, 's08_plan')
    if item.get('expression_control') is not False:
        codes.append('expression_control_mismatch')
    if not _text(item.get('experiment_id')).startswith('s08_'):
        codes.append('experiment_id_malformed')
    recalls = item.get('recall_events')
    if not isinstance(recalls, list):
        codes.append('recall_events_malformed')
    else:
        for event in recalls:
            if not isinstance(event, dict) or event.get('kind') != 'tool_result':
                codes.append('recall_event_not_retrieval')
                continue
            payload = event.get('payload')
            if not isinstance(payload, dict) or payload.get('recall_layer') != 'looked_up':
                codes.append('recall_layer_mismatch')
            elif payload.get('ok') is not True:
                codes.append('recall_ok_mismatch')
            if _text(event.get('source')).casefold().startswith('legacy'):
                codes.append('recall_source_legacy')
    return codes + sorted(_live_claims(item))


# 识别顺序即优先级：最具体的形状在前，避免产物落进普通报告的重盖路径。
_REGISTRY = (
    ('s08_review_package',
     lambda x: _text(x.get('experiment_id')).startswith('s08_')
     and 'organic' in x and 'plan' in x, _validate_s08_package, 'manual_review'),
    ('s08_experiment_result',
     lambda x: _text(x.get('experiment_id')).startswith('s08_')
     and 'arms' in x and 'checks' in x, _validate_s08_result, 'manual_review'),
    ('s08_plan',
     lambda x: _text(x.get('experiment_id')).startswith('s08_')
     and 'base_events' in x and 'recall_events' in x, _validate_s08_plan, 'manual_review'),
    ('r07_review_packet',
     lambda x: isinstance(x.get('plan'), dict)
     and _text(x['plan'].get('plan_id')).startswith('plan_')
     and 'variants' in x and 'diffs' in x, _validate_r07, 'manual_review'),
    ('candidate_view',
     lambda x: 'redaction' in x
     and x.get('trajectory') == CANDIDATE_COUNTERFACTUAL, _validate_candidate_view,
     'observation_only'),
    ('candidate_run',
     lambda x: x.get('branch') == 'candidate'
     and _text(x.get('run_id')).startswith('cand_')
     and 'timeline' in x, _validate_candidate_run, 'observation_only'),
    ('valence_samples',
     lambda x: x.get('mode') == 'candidate_integrated_replay', _validate_valence,
     'observation_only'),
    ('shadow_status',
     lambda x: x.get('mode') == 'shadow' and 'timeline' in x, _validate_status, 'live'),
    ('retrospective',
     lambda x: x.get('mode') == 'retrospective', _validate_retrospective,
     'observation_only'),
    ('synthetic_reference',
     lambda x: x.get('mode') == 'synthetic_reference', _validate_reference,
     'observation_only'),
    ('matrix_report',
     lambda x: isinstance(x.get('provenance'), dict)
     and x['provenance'].get('method') == 'isolated_fixture_frozen_replay'
     and 'cases' in x, _validate_matrix, 'observation_only'),
    ('revision_run',
     lambda x: _text(x.get('revision_id')).startswith('emo_')
     and 'branch' in x and 'state' in x, _validate_revision, 'observation_only'),
)

_OK_CODE = {'live': 'live_contract_ok', 'observation_only': 'observation_contract_ok',
            'manual_review': 'manual_review_ok'}

# 稳定 reason_code 全集（含按必备字段生成的 missing_<field>）。
_BASE_REASONS = (
    'not_a_dict', 'missing_trajectory', 'unknown_trajectory', 'expression_control_true',
    'self_reported_live_overridden', 'caliber_restamped',
    'live_contract_ok', 'observation_contract_ok', 'manual_review_ok',
    'trajectory_mismatch', 'kernel_version_mismatch', 'counts_for_live_mismatch',
    'expression_control_mismatch', 'counts_for_live_true_present',
    'expression_control_true_present', 'run_id_malformed', 'redaction_malformed',
    'reason_codes_malformed', 'nested_retrospective_mismatch', 'nested_valence_mismatch',
    'provenance_mismatch', 'revision_id_malformed', 'plan_malformed', 'variants_malformed',
    'diffs_malformed', 'review_malformed', 'review_authorization_mismatch',
    'review_organic_mismatch', 'review_judgement_not_none', 'experiment_id_malformed',
    'organic_mismatch', 'authorization_mismatch', 'arm_caliber_mismatch', 'arms_malformed',
    'checks_malformed', 'shadow_check_failed', 'recall_events_malformed',
    'recall_event_not_retrieval', 'recall_layer_mismatch', 'recall_ok_mismatch',
    'recall_source_legacy',
)
REASON_CODES = tuple(sorted(set(_BASE_REASONS)
                            | {'missing_' + field
                               for fields in REQUIRED.values() for field in fields}))


def _entry(kind, bucket, codes, caliber):
    return {'kind': kind, 'bucket': bucket, 'reason_codes': list(codes),
            'caliber': caliber}


def _caliber_of(item):
    if 'trajectory' not in item:
        return None
    return {key: item.get(key)
            for key in ('trajectory', 'kernel_version', 'counts_for_live')}


def _classify_generic(item):
    """未识别为生产者产物的普通报告：按契约重盖，自报在线被覆盖。"""
    if 'trajectory' not in item:
        return _entry('generic_report', 'rejected', ['missing_trajectory'], None)
    trajectory = item['trajectory']
    if not isinstance(trajectory, str) or trajectory not in trajectories.CONTRACTS:
        return _entry('generic_report', 'rejected', ['unknown_trajectory'], None)
    if item.get('expression_control') is True:
        return _entry('generic_report', 'rejected', ['expression_control_true'], None)
    version, live = trajectories.CONTRACTS[trajectory]
    codes = []
    if item.get('counts_for_live') is True and not live:
        codes.append('self_reported_live_overridden')
    elif (('counts_for_live' in item and item['counts_for_live'] is not live)
          or ('kernel_version' in item and item['kernel_version'] != version)):
        codes.append('caliber_restamped')
    certified = trajectories.stamp(copy.deepcopy(item), trajectory)
    codes.append(_OK_CODE['live' if live else 'observation_only'])
    return _entry('generic_report', 'live' if live else 'observation_only',
                  sorted(set(codes)),
                  {key: certified[key]
                   for key in ('trajectory', 'kernel_version', 'counts_for_live')})


def classify(item):
    """纯函数分拣单个报告/产物：返回 kind/bucket/reason_codes/caliber。

    识别出的生产者产物走形状锁：口径违规一律 rejected，不重盖放行（锁住任何
    试图把非在线产物计为在线的输入形状）。普通轨迹报告走契约重盖。缺字段不
    补造、unknown 不冒充 pass。不改输入。
    """
    if not isinstance(item, dict):
        return _entry('unknown', 'rejected', ['not_a_dict'], None)
    for kind, detect, validate, bucket in _REGISTRY:
        if detect(item):
            codes = sorted(set(validate(item)))
            if codes:
                return _entry(kind, 'rejected', codes, None)
            return _entry(kind, bucket, [_OK_CODE[bucket]], _caliber_of(item))
    return _classify_generic(item)


def sort_observations(items):
    """跨模式污染检查：混合报告/产物稳定分拣为四类，保持输入顺序，不改输入。

    返回 {live, observation_only, manual_review, rejected, counts}；每个条目
    带 index/kind/bucket/reason_codes/caliber。同输入同输出；空输入给四个空桶。
    """
    buckets = {bucket: [] for bucket in BUCKETS}
    for index, item in enumerate(items or []):
        entry = classify(item)
        entry['index'] = index
        buckets[entry['bucket']].append(entry)
    return dict(buckets, counts={bucket: len(buckets[bucket]) for bucket in BUCKETS})