# -*- coding: utf-8 -*-
"""V05 发布前故障注入与回退矩阵（validation 轨，3a 预验收证据）。

统一方案 §V 表 V05 行：「3a发布前隔离压测和故障注入：重启/换模型/超时/库不可用/
多段回执/拒绝/撤回、表达接线与回退；无外发预验收后由C06单独授权真实灰度」。
本模块是该矩阵的编排与报告器，范式逐字跟随 V03 retraction_scope_matrix：
真实执行在测试的 runner 里（隔离临时账本 / 内存里的纯函数契约），本模块只做
轴校验、委托执行、覆盖审计与如实报告。

执行引擎复用 V03，不另长第二套：v03.run_matrix 负责确定性执行与
pass/fail/error 分型（runner 抛异常记 error 不吞、缺观察键不算默认为真、
空矩阵拒绝——没有用例的「全绿」是假绿灯）；v03.coverage 负责交叉点统计。
本模块只新增 V05 自己的轴（发布故障 × 表达管线层）与覆盖审计：每个声明的
故障/层都必须有真实用例命中，缺的大声列出，不假装覆盖完整。

硬边界（与 V03/D07 同一范式）：
- 纯编排、零副作用：不 import 任何库或上游契约模块、不读写账本、不解析路径、
  不发网络、不调模型、无时钟无随机。全部真实读写来自调用方传入的 runner——
  runner 只允许指向隔离临时账本，绝不指向生产 data/。
- 本矩阵只是预验收证据：expression_control 恒 False、outbound 恒 False、
  authorization 恒 manual_review、judgement 恒 None。真实灰度由 C06 另行
  授权，本模块永不授权、不接管表达、不产生任何外发。
- 真实发送链的幂等（unknown 不重发、多段回执不重复接受、崩溃边界不重试）
  由工程门 action_idempotency_outbox 的隔离探针验证；本矩阵在提案契约层
  验证裁决确定性/不累积/不改收件人，不冒充已测真实发送链。
"""
import retraction_scope_matrix as v03

# 算法版本：报告绑定它，口径变化必须改版本。
ALGORITHM_VERSION = 'v05-1'

# 矩阵的两根轴。层 = 表达管线/持久状态环节；故障 = 发布前注入的故障类型
# （§V 表 V05 行逐字：重启/换模型/超时/库不可用/多段回执/拒绝/撤回，
#  加 §15.3「被拒绝的发送和unknown结果不造成重复外发」的 unknown_result）。
LAYERS = ('state', 'snapshot', 'injection', 'expression_plan', 'delivery', 'rollback')
FAULTS = ('restart', 'model_switch', 'timeout', 'db_unavailable', 'multi_receipt',
          'rejected_send', 'unknown_result', 'retraction')

LIMITATIONS = (
    '只证契约在隔离临时账本/内存夹具上可执行，不证生产账本已合规；'
    '生产审计走 phase_audit 只读快照，本矩阵不是生产绿灯。',
    '本矩阵是 3a 发布前预验收证据：expression_control 恒 False、outbound 恒 '
    'False、authorization 恒 manual_review、judgement 恒 None；真实灰度由 '
    'C06 另行授权，本模块永不授权、不接管表达、不产生任何外发。',
    '真实发送链幂等（unknown 不重发、多段回执不重复接受、崩溃边界不重试）由'
    '工程门 action_idempotency_outbox 的隔离探针验证；本矩阵在提案契约层验证'
    '裁决确定性/不累积/不改收件人，不冒充已测真实发送链。',
    '首气泡 ≤2.5s 与灰度期 p50/p95/p99 需真实灰度实测（§15.3），本矩阵不做'
    '真实压测、不冒充延迟达标；「SQL 很快」推不出首气泡达标。',
    'runner 的全部副作用只许落在调用方准备好的隔离账本；故障分型沿用 '
    'D07/V03 口径：缺数据≠零、暂时锁≠损坏、旧投影不得当有效支持；'
    '发现契约缺陷只如实报告，不把缺陷断言成正确行为。',
    '覆盖审计只统计声明的 层×故障 交叉点是否真有用例命中，缺的大声列出；'
    '覆盖完整≠语义充分，语义判据来自每个用例显式登记的 expected。',
)


def case(case_id, layer, fault, expected, runner):
    """登记一个 V05 矩阵用例。层/故障必须在 V05 轴上，期望分型必须显式非空。

    expected 是「观察键 → 期望值」的显式声明：矩阵的可解释性来自这里，
    不允许空期望（那等于没有判据）。runner 是零参可调用，返回观察 dict；
    它的全部副作用必须落在调用方准备好的隔离账本里。返回形状与 v03.case
    逐字一致，执行/分型完全委托 v03.run_matrix。
    """
    cid = str(case_id or '').strip()
    if not cid:
        raise ValueError('case requires case_id')
    if layer not in LAYERS:
        raise ValueError('unknown matrix layer: %s' % layer)
    if fault not in FAULTS:
        raise ValueError('unknown matrix fault: %s' % fault)
    if not isinstance(expected, dict) or not expected:
        raise ValueError('case requires nonempty explicit expected classification')
    if not callable(runner):
        raise ValueError('case requires a callable runner')
    return {'case_id': cid, 'layer': layer, 'fault': fault,
            'expected': dict(expected), 'runner': runner}


def coverage(cases):
    """纯统计：已覆盖的 层×故障 交叉点计数（委托 v03.coverage，不另长口径）。"""
    return v03.coverage(cases)


def run_matrix(cases):
    """确定性执行一组 V05 用例，产出预验收报告。不读写任何账本。

    执行与 pass/fail/error 分型逐字委托 v03.run_matrix（空矩阵拒绝、异常记
    error 不吞、缺观察键不算默认为真）；本函数只包一层 V05 报告：覆盖审计
    （每个声明的故障/层都必须有真实用例命中，缺的大声列出）+ 恒定边界字段。
    status 只来自执行分型；coverage.complete 是独立的如实信号，不冒充 pass。
    """
    report = v03.run_matrix(cases)
    cov = v03.coverage(cases)
    hit_layers = {row['layer'] for row in report['cases']}
    hit_faults = {row['fault'] for row in report['cases']}
    missing_faults = sorted(set(FAULTS) - hit_faults)
    missing_layers = sorted(set(LAYERS) - hit_layers)
    return {
        'status': report['status'],
        'cases': report['cases'],
        'coverage': dict(cov,
                         missing_required_faults=missing_faults,
                         missing_required_layers=missing_layers,
                         complete=not missing_faults and not missing_layers),
        'expression_control': False,
        'outbound': False,
        'authorization': 'manual_review',
        'judgement': None,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'kernel': v03.ALGORITHM_VERSION,
            'kernel_provenance': report['provenance'],
            'required_faults': list(FAULTS),
            'required_layers': list(LAYERS),
            'llm_reinterpretation': False,
            'network': False,
            'production_ledger': False,
        },
        'limitations': list(LIMITATIONS),
    }