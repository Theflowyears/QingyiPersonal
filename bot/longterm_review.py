# -*- coding: utf-8 -*-
"""V06 长期消融与回退复查（validation 轨，统一方案 §V 表 V06 行）。

「长期消融与回退复查：记忆选择、等待、履约、观点变化有证据，不能只看
『说得更像人』」。本模块是该复查的编排与报告器，范式逐字跟随 V05
release_fault_matrix：真实执行在测试的 runner 里（内存里的纯函数契约 /
隔离临时账本），本模块只做轴校验、委托执行、覆盖审计与如实报告。

执行引擎复用 V03，不另长第二套：v03.run_matrix 负责确定性执行与
pass/fail/error 分型（runner 抛异常记 error 不吞、缺观察键不算默认为真、
空矩阵拒绝——没有用例的「全绿」是假绿灯）；v03.coverage 负责交叉点统计。
本模块只新增 V06 自己的轴（长期观察维度 × 证据类型）与覆盖审计：四个维度
（记忆选择/等待/履约/观点变化）每一维都必须同时有消融对照与回退复查两类
真实用例命中，缺的大声列出，不假装覆盖完整。

硬边界（与 V03/V05/D07 同一范式）：
- 纯编排、零副作用：不 import 任何库或上游契约模块、不读写账本、不解析路径、
  不发网络、不调模型、无时钟无随机。全部真实读写来自调用方传入的 runner——
  runner 只允许指向隔离临时账本或内存夹具，绝不指向生产 data/。
- 契约可执行 ≠ 长期消融已做：真实结论需要真实长期样本，本矩阵只证明复查
  契约可执行；judgement 恒 None、authorization 恒 manual_review，
  「有用/没用/更像人」一律留给人判，检测器不下结论。
- 覆盖完整 ≠ 语义充分：语义判据来自每个用例显式登记的 expected；
  「说得更像人」不是判据，判据必须是可观察的选择/等待/履约/修订差异。
"""
import retraction_scope_matrix as v03

# 算法版本：报告绑定它，口径变化必须改版本。
ALGORITHM_VERSION = 'v06-1'

# 矩阵的两根轴。维度 = 必须拿证据的长期行为（§V 表 V06 行逐字：记忆选择、
# 等待、履约、观点变化）；证据类型 = 消融对照（去掉一环看选择是否真改变）
# 与回退复查（回退后读取路径恢复、历史不被篡改）。
DIMENSIONS = ('memory_selection', 'waiting', 'commitment_fulfillment',
              'belief_revision')
EVIDENCE_KINDS = ('ablation', 'rollback_recheck')

LIMITATIONS = (
    '只证复查契约在隔离临时账本/内存夹具上可执行，不证长期行为已达标；'
    '真实长期消融需要真实运行样本，organic 口径 not_observed，本矩阵不是生产绿灯。',
    'judgement 恒 None、authorization 恒 manual_review：「有用/没用/更像人」'
    '一律留给人判，检测器不下结论；不能只看「说得更像人」。',
    '覆盖审计要求每个维度同时有消融对照与回退复查两类真实用例命中，缺的大声列出；'
    '覆盖完整≠语义充分，语义判据来自每个用例显式登记的 expected。',
    'runner 的全部副作用只许落在调用方准备好的隔离账本或内存夹具；故障分型沿用 '
    'D07/V03 口径：缺数据≠零、暂时锁≠损坏、旧投影不得当有效支持；'
    '发现契约缺陷只如实报告，不把缺陷断言成正确行为。',
    '本模块零写入、不读生产账本、不发网络、不调模型；回退复查只验证读取路径'
    '恢复与历史不篡改，不执行真实回退、不删除任何账本。',
)


def case(case_id, dimension, evidence_kind, expected, runner):
    """登记一个 V06 复查用例。维度/证据类型必须在 V06 轴上，期望分型显式非空。

    expected 是「观察键 → 期望值」的显式声明：复查的可解释性来自这里，
    不允许空期望（那等于没有判据）。runner 是零参可调用，返回观察 dict；
    它的全部副作用必须落在调用方准备好的隔离账本/内存夹具里。返回形状与
    v03.case 逐字一致（dimension→layer、evidence_kind→fault），执行/分型
    完全委托 v03.run_matrix。
    """
    cid = str(case_id or '').strip()
    if not cid:
        raise ValueError('case requires case_id')
    if dimension not in DIMENSIONS:
        raise ValueError('unknown review dimension: %s' % dimension)
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError('unknown evidence kind: %s' % evidence_kind)
    if not isinstance(expected, dict) or not expected:
        raise ValueError('case requires nonempty explicit expected classification')
    if not callable(runner):
        raise ValueError('case requires a callable runner')
    return {'case_id': cid, 'layer': dimension, 'fault': evidence_kind,
            'expected': dict(expected), 'runner': runner}


def coverage(cases):
    """纯统计：已覆盖的 维度×证据类型 交叉点计数（委托 v03.coverage）。"""
    return v03.coverage(cases)


def run_matrix(cases):
    """确定性执行一组 V06 用例，产出长期复查报告。不读写任何账本。

    执行与 pass/fail/error 分型逐字委托 v03.run_matrix；本函数只包一层 V06
    报告：覆盖审计（每个维度都必须同时有消融与回退复查用例，缺的大声列出）
    + 恒定边界字段。status 只来自执行分型；coverage.complete 是独立的如实
    信号，不冒充 pass，更不冒充「长期行为已达标」。
    """
    report = v03.run_matrix(cases)
    cov = v03.coverage(cases)
    hit = set(cov['intersections'])
    required = {'%s/%s' % (d, k) for d in DIMENSIONS for k in EVIDENCE_KINDS}
    missing = sorted(required - hit)
    missing_dimensions = sorted({key.split('/')[0] for key in missing})
    missing_kinds = sorted({key.split('/')[1] for key in missing})
    return {
        'status': report['status'],
        'cases': report['cases'],
        'coverage': dict(cov,
                         missing_required_intersections=missing,
                         missing_required_dimensions=missing_dimensions,
                         missing_required_evidence_kinds=missing_kinds,
                         complete=not missing),
        'authorization': 'manual_review',
        'judgement': None,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'kernel': v03.ALGORITHM_VERSION,
            'kernel_provenance': report['provenance'],
            'required_dimensions': list(DIMENSIONS),
            'required_evidence_kinds': list(EVIDENCE_KINDS),
            'real_longterm_samples': False,
            'llm_reinterpretation': False,
            'network': False,
            'production_ledger': False,
        },
        'limitations': list(LIMITATIONS),
    }