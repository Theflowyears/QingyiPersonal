# -*- coding: utf-8 -*-
"""C03 阶段3a发布人工评审包（admission 轨，3a 预评审证据聚合）。

统一方案 §C 表 C03 行：「3a发布人工评审包：既有12项、候选误判、Ripple效果、
新增范围安全、隔离延迟/回退证据；全pass不自动授权」。本模块把五路既有生产者
的证据汇成一个供人评审的包，范式跟随 V05/V03：纯编排、零副作用，真实执行
留在各生产者契约里，本模块只做形状校验、如实汇总与恒定边界。

五路证据（全部由调用方传入，本模块不生产任何证据）：
- existing_gates：phase_audit.audit_readiness 返回里的 phase25 子字典
  （既有12项自然与工程证据）。旧阶段定义不在这里重定义——生产者给什么门
  就报什么门；not_verified 是工程证据未绑定，如实归 not_observed。
- candidate_misjudgment：S05 valence_annotation 的评测报告（金标准只认
  human_annotation；missing_data/insufficient_data 如实 not_observed）。
- ripple_effect：R07 ripple_ablation.build_review_packet 的人工评审包
  （只带 R06 清单 ID/reason_code/sha16，不带场景原文）。
- scope_safety：V03 retraction_scope_matrix.run_matrix 报告（新增范围安全：
  无效源不驱动当前状态、撤回不复活）。fail 是安全阻断，不用平均分抵消。
- isolation_rollback：V04 mode_observation.sort_observations 分拣 +
  V05 release_fault_matrix.run_matrix 报告（隔离延迟/回退证据）。

硬边界（与 V03/V05/D07 同一范式）：
- 纯编排、零副作用：不读写账本、不解析路径、不发网络、不调模型、无时钟无
  随机（now 由调用方传入）。证据生产者的全部副作用必须落在调用方准备好的
  隔离账本或生产只读快照上，本模块不替它们背书。
- 全pass不自动授权：authorization 恒 manual_review、judgement 恒 None、
  expression_control 恒 False、outbound 恒 False。真实灰度由 C06 在评审后
  另行授权；人工评审必须由人签字，AI 不能自签（统一方案 §23）。
- 段状态只表示「证据在场且生产者报告无失败」，不表示语义充分；缺证据是
  missing，缺数据≠零，形状非法大声拒绝，不静默吞掉也不假装覆盖完整。
"""
import copy
import hashlib
import json

# 算法版本：包绑定它，口径变化必须改版本。
ALGORITHM_VERSION = 'c03-1'

# 评审包的五路证据，顺序即报告顺序（§C 表 C03 行逐字）。
SECTIONS = ('existing_gates', 'candidate_misjudgment', 'ripple_effect',
            'scope_safety', 'isolation_rollback')

# 段状态分型。missing=证据没给；error=生产者报告执行异常；blocked=安全阻断
# 或门被挡；unknown=生产者读不到账本；not_observed=缺真实样本/覆盖不全；
# pass=证据在场且生产者报告无失败（语义判断仍归人）。
SECTION_STATUSES = ('pass', 'not_observed', 'blocked', 'unknown', 'error', 'missing')

_GATE_STATUSES = ('pass', 'not_observed', 'blocked', 'unknown', 'fail', 'not_verified')
_MATRIX_STATUSES = ('pass', 'fail', 'error')
_V04_BUCKETS = ('live', 'observation_only', 'manual_review', 'rejected')

LIMITATIONS = (
    '本包是 3a 发布前人工评审证据聚合：authorization 恒 manual_review、'
    'judgement 恒 None、expression_control/outbound 恒 False；全pass不自动'
    '授权，真实灰度由 C06 在人工评审后另行授权。',
    '段状态只表示「证据在场且生产者报告无失败」，不表示效果/安全语义充分；'
    '语义判断由人签字，AI 不能自签，可评审≠已校准。',
    '既有12项沿用 phase25 生产者口径，本模块不重定义旧门；缺证据是 missing，'
    '缺数据≠零，暂时锁≠损坏，旧投影不得当有效支持。',
    '候选误判段金标准只认 human_annotation，候选 confidence 绝不当金标准；'
    'natural 与 isolated_eval 绝不混算（生产者口径，本模块只核对不重算）。',
    'Ripple效果段只带 R06 清单（ID/reason_code/sha16），不带场景原文；'
    '评审包不携带聊天载荷/QQ 号，脱敏由生产者负责，本模块不新增原文。',
    '首气泡 ≤2.5s 与灰度期 p50/p95/p99 需真实灰度实测，本包不做真实压测、'
    '不冒充延迟达标；隔离矩阵可执行≠生产账本已合规。',
    '安全阻断（范围矩阵 fail、门 blocked）逐项列出，不用平均分抵消；'
    '覆盖完整≠语义充分，缺的故障/层由 V05 覆盖审计大声列出。',
)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha16(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode('utf-8')).hexdigest()[:16]


def _existing_gates(evidence):
    """phase25 检查清单：逐门如实转述，不重定义旧12项。"""
    _require(isinstance(evidence, dict), 'existing_gates must be a phase25 audit dict')
    checks = evidence.get('checks')
    _require(isinstance(checks, list) and checks,
             'existing_gates requires nonempty checks list; an empty green is a fake green')
    counts = {}
    non_pass = []
    for row in checks:
        _require(isinstance(row, dict) and str(row.get('key') or '').strip(),
                 'existing_gates check requires key')
        status = row.get('status')
        _require(status in _GATE_STATUSES,
                 'existing_gates unknown gate status: %r' % (status,))
        counts[status] = counts.get(status, 0) + 1
        if status != 'pass':
            non_pass.append({'key': row['key'], 'status': status})
    if counts.get('blocked') or counts.get('fail'):
        status = 'blocked'
    elif counts.get('unknown'):
        status = 'unknown'
    elif counts.get('not_observed') or counts.get('not_verified'):
        status = 'not_observed'
    else:
        status = 'pass'
    return {'status': status,
            'summary': {'total': len(checks), 'counts': counts,
                        'non_pass': non_pass},
            'evidence': evidence}


def _candidate_misjudgment(evidence):
    """S05 评测报告：金标准必须来自人工标注；缺数据如实 not_observed。"""
    _require(isinstance(evidence, dict),
             'candidate_misjudgment must be an S05 evaluation report dict')
    status = evidence.get('status')
    if evidence.get('ok') is False:
        _require(status == 'missing_data',
                 'candidate_misjudgment failed report must carry missing_data status')
        return {'status': 'not_observed',
                'summary': {'producer_status': status}, 'evidence': evidence}
    _require(evidence.get('ok') is True and status in ('ok', 'insufficient_data'),
             'candidate_misjudgment report must be ok with ok/insufficient_data status')
    _require(evidence.get('split') in ('dev', 'holdout'),
             'candidate_misjudgment report must carry explicit split')
    if status == 'ok':
        _require(evidence.get('origin') in ('natural', 'isolated_eval'),
                 'candidate_misjudgment report must carry explicit origin')
        _require(evidence.get('gold_source') == 'human_annotation',
                 'candidate_misjudgment gold must come from human annotation, never candidate confidence')
    summary = {'producer_status': status, 'split': evidence['split'],
               'origin': evidence['origin'],
               'n_joined': evidence.get('n_joined'),
               'macro_f1': evidence.get('macro_f1'),
               'praise_precision': evidence.get('praise_precision'),
               'rebuke_precision': evidence.get('rebuke_precision'),
               'target_error_rate': evidence.get('target_error_rate'),
               'abstention_rate': evidence.get('abstention_rate'),
               'lexical_relation': copy.deepcopy(evidence.get('lexical_relation'))}
    section_status = 'pass' if status == 'ok' else 'not_observed'
    return {'status': section_status, 'summary': summary, 'evidence': evidence}


def _ripple_effect(evidence):
    """R07 人工评审包：形状核对；机器判断必须留 None，效果归人评。"""
    _require(isinstance(evidence, dict),
             'ripple_effect must be an R07 review packet dict')
    for key in ('plan', 'variants', 'diffs', 'review', 'limitations'):
        _require(key in evidence, 'ripple_effect packet missing %s' % key)
    review = evidence['review']
    _require(isinstance(review, dict), 'ripple_effect review must be a dict')
    _require(review.get('authorization') == 'manual_review',
             'ripple_effect packet must stay manual_review')
    _require(review.get('judgement') is None,
             'ripple_effect packet must not carry a machine judgement')
    _require(isinstance(evidence['variants'], list) and evidence['variants'],
             'ripple_effect packet requires nonempty variants; an empty green is a fake green')
    return {'status': 'pass',
            'summary': {'variants': len(evidence['variants']),
                        'diffs': len(evidence['diffs']) if isinstance(evidence['diffs'], list) else None,
                        'organic': review.get('organic')},
            'evidence': evidence}


def _matrix_report(evidence, name):
    _require(isinstance(evidence, dict), '%s must be a matrix report dict' % name)
    status = evidence.get('status')
    _require(status in _MATRIX_STATUSES,
             '%s unknown matrix status: %r' % (name, status))
    cases = evidence.get('cases')
    _require(isinstance(cases, list) and cases,
             '%s requires nonempty cases; an empty green is a fake green' % name)
    failed = [{'case_id': row.get('case_id'), 'status': row.get('status'),
               'error': row.get('error', '')}
              for row in cases if row.get('status') in ('fail', 'error')]
    return status, cases, failed


def _scope_safety(evidence):
    """V03 范围故障矩阵：fail 是安全阻断，逐项列出不用平均分抵消。"""
    status, cases, failed = _matrix_report(evidence, 'scope_safety')
    section_status = {'pass': 'pass', 'fail': 'blocked', 'error': 'error'}[status]
    return {'status': section_status,
            'summary': {'cases': len(cases), 'failed': failed},
            'evidence': evidence}


def _isolation_rollback(evidence):
    """V04 分拣 + V05 故障矩阵：覆盖不全如实 not_observed，不冒充完整。"""
    _require(isinstance(evidence, dict),
             'isolation_rollback must carry mode_observation and fault_matrix')
    modes = evidence.get('mode_observation')
    _require(isinstance(modes, dict),
             'isolation_rollback.mode_observation must be a V04 sort dict')
    for bucket in _V04_BUCKETS:
        _require(isinstance(modes.get(bucket), list),
                 'isolation_rollback.mode_observation missing bucket %s' % bucket)
    counts = {bucket: len(modes[bucket]) for bucket in _V04_BUCKETS}
    matrix = evidence.get('fault_matrix')
    status, cases, failed = _matrix_report(matrix, 'isolation_rollback.fault_matrix')
    coverage = matrix.get('coverage')
    _require(isinstance(coverage, dict),
             'isolation_rollback.fault_matrix requires V05 coverage audit')
    complete = coverage.get('complete') is True
    if status == 'error':
        section_status = 'error'
    elif status == 'fail':
        section_status = 'blocked'
    elif not complete:
        section_status = 'not_observed'
    else:
        section_status = 'pass'
    return {'status': section_status,
            'summary': {'mode_counts': counts, 'cases': len(cases),
                        'failed': failed, 'coverage_complete': complete,
                        'missing_required_faults': list(coverage.get('missing_required_faults') or []),
                        'missing_required_layers': list(coverage.get('missing_required_layers') or [])},
            'evidence': evidence}


_VALIDATORS = {
    'existing_gates': _existing_gates,
    'candidate_misjudgment': _candidate_misjudgment,
    'ripple_effect': _ripple_effect,
    'scope_safety': _scope_safety,
    'isolation_rollback': _isolation_rollback,
}


def build_review_package(evidence, *, now):
    """把五路生产者证据汇成一个人工评审包。不读写任何账本。

    evidence 缺某一路 → 该段 missing 并大声列出（缺证据≠零）；给了但形状
    非法 → ValueError 大声拒绝。包 ID 内容寻址（只绑证据与算法版本，不绑
    时钟），同证据同 ID。全pass不自动授权：authorization/judgement/
    expression_control/outbound 是恒定边界，不随证据变化。
    """
    _require(isinstance(evidence, dict), 'evidence must be a dict of sections')
    unknown = sorted(set(evidence) - set(SECTIONS))
    _require(not unknown, 'unknown review sections: %s' % ', '.join(unknown))
    try:
        now = float(now)
    except (TypeError, ValueError):
        raise ValueError('now must be a timestamp')
    _require(now > 0, 'now must be positive')

    sections = {}
    missing = []
    for name in SECTIONS:
        payload = evidence.get(name)
        if payload is None:
            sections[name] = {'status': 'missing', 'summary': {}, 'evidence': None}
            missing.append(name)
            continue
        sections[name] = _VALIDATORS[name](copy.deepcopy(payload))

    safety_blocks = []
    gates = sections['existing_gates']
    for row in gates['summary'].get('non_pass') or []:
        if row['status'] in ('blocked', 'fail'):
            safety_blocks.append({'section': 'existing_gates',
                                  'ref': row['key'], 'reason': 'gate_blocked'})
    for name in ('scope_safety', 'isolation_rollback'):
        for row in sections[name]['summary'].get('failed') or []:
            safety_blocks.append({'section': name, 'ref': row['case_id'],
                                  'reason': 'matrix_' + str(row['status'])})

    statuses = [sections[name]['status'] for name in SECTIONS]
    all_pass = all(status == 'pass' for status in statuses)
    producers = {
        'existing_gates': 'phase_audit.audit_readiness()["phase25"]',
        'candidate_misjudgment': 'valence_annotation.evaluation_report',
        'ripple_effect': 'ripple_ablation.build_review_packet',
        'scope_safety': 'retraction_scope_matrix.run_matrix',
        'isolation_rollback': 'mode_observation.sort_observations + release_fault_matrix.run_matrix',
    }
    return {
        'sections': sections,
        'missing_sections': missing,
        'safety_blocks': safety_blocks,
        'all_pass': all_pass,
        'expression_control': False,
        'outbound': False,
        'authorization': 'manual_review',
        'judgement': None,
        'package_id': _sha16({'algorithm_version': ALGORITHM_VERSION,
                              'sections': sections}),
        'generated_at': now,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'producers': producers,
            'llm_reinterpretation': False,
            'network': False,
            'production_ledger': False,
        },
        'limitations': list(LIMITATIONS),
    }