# -*- coding: utf-8 -*-
"""V03 分层撤回 × 范围故障矩阵的确定性执行器与报告器（validation 轨）。

矩阵的真实执行在 tests/test_retraction_scope_matrix.py：每个用例在隔离
DATA_DIR 里对真实契约（A08 撤回 / M05 失效DAG / S03 情感重算 / R01 统一检索 /
R02 边 / R03 初召回 / R04 扩散 / M07 情景化）做「撤回前→撤回后→重跑」。
本模块只提供可复用的编排与报告，回答「哪个层 × 哪类故障 × 期望什么分型」：

  case()       登记一个用例：层、故障类型、期望分型（显式键值）、runner。
  run_matrix() 按 case_id 确定性顺序执行，产出 {status, cases, provenance,
               limitations}；每个用例的 expected/observed/mismatches 都摆出来。
  coverage()   统计已覆盖的 层×故障 交叉点，供覆盖审计。

硬边界（与 M05/R04 同一范式）：
- 纯编排、零副作用：本模块不 import 任何库、不读写账本、不解析路径、
  不建库建表建目录、不发网络、不调模型。全部真实读写来自调用方传入的
  runner——runner 只允许指向隔离临时账本，绝不指向生产 data/。
- 确定性：用例按 case_id 排序，provenance 不含时钟；同输入同报告。
- 如实分型（D07 口径）：runner 抛异常记 error，不冒充 pass 也不吞掉；
  期望与观察不一致记 fail 并逐键列出 mismatch；缺观察键不算默认为真。
  limitations 如实声明没覆盖什么，不假装矩阵证明了生产合规。
"""

ALGORITHM_VERSION = 'v03-1'

# 矩阵的两根轴。层 = 被验证的状态层；故障 = 注入的修正/故障类型。
LAYERS = ('cognition', 'derivation', 'emotion', 'retrieval', 'episode')
FAULTS = ('retract_source', 'interpretation_revised', 'belief_corrected',
          'scope_revoked', 'stale_projection', 'missing_data', 'transient_lock',
          'same_root', 'cross_scope_leak')

LIMITATIONS = (
    '不覆盖 ExpressionPlan/表达接管（C04 未落地）：撤回后「能不能说、怎么说」的门不在本矩阵。',
    '只证契约在隔离临时账本上可执行，不证生产账本已合规；生产审计走 phase_audit 只读快照。',
    '不发网络、不调 LLM；情感重算只复用冻结内核，不重新解释原话。',
    '范围硬过滤由调用方负责（R04 不重做）：矩阵验证的是调用方按契约接线后的组合行为。',
    '迟到/重启回放矩阵属 S06，候选回放性能与脱敏读取属 S07，不在本矩阵。',
    'A08 旧撤回路径与 M05 失效账本仍是两套（旁路边界）：矩阵分别验证，不假装已合并。',
    '故障分型沿用 D07 口径：缺数据≠零、暂时锁≠损坏、旧投影不得当有效支持；'
    '发现契约缺陷只如实报告，不把缺陷断言成正确行为。',
)


def case(case_id, layer, fault, expected, runner):
    """登记一个矩阵用例。层/故障必须在轴上，期望分型必须显式非空。

    expected 是「观察键 → 期望值」的显式声明：矩阵的可解释性来自这里，
    不允许空期望（那等于没有判据）。runner 是零参可调用，返回观察 dict；
    它的全部副作用必须落在调用方准备好的隔离账本里。
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
    """纯统计：已覆盖的 层×故障 交叉点计数（'layer/fault' → n）。"""
    counts = {}
    for spec in cases or []:
        key = '%s/%s' % (spec['layer'], spec['fault'])
        counts[key] = counts.get(key, 0) + 1
    return {'cases': len(cases or []), 'intersections': counts}


def run_matrix(cases):
    """确定性执行一组用例，产出可解释报告。不读写任何账本。

    分型口径：pass=全部分型命中；fail=有期望/观察不一致；error=有 runner
    抛异常（异常类型与消息如实记录，不吞）。空矩阵直接拒绝——没有用例的
    「全绿」是假绿灯。provenance 不含时钟，同输入同输出。
    """
    specs = list(cases or [])
    if not specs:
        raise ValueError('matrix carries no case; an empty green is a fake green')
    seen = set()
    for spec in specs:
        if not isinstance(spec, dict) or not {'case_id', 'layer', 'fault',
                                              'expected', 'runner'} <= set(spec):
            raise ValueError('matrix case must come from case()')
        if spec['case_id'] in seen:
            raise ValueError('duplicate case_id: %s' % spec['case_id'])
        seen.add(spec['case_id'])
    results = []
    for spec in sorted(specs, key=lambda s: s['case_id']):
        entry = {'case_id': spec['case_id'], 'layer': spec['layer'],
                 'fault': spec['fault'], 'status': 'pass',
                 'expected': dict(spec['expected']), 'observed': {},
                 'mismatches': [], 'error': ''}
        try:
            observed = spec['runner']()
        except Exception as exc:
            entry['status'] = 'error'
            entry['error'] = '%s: %s' % (type(exc).__name__, exc)
            results.append(entry)
            continue
        if not isinstance(observed, dict):
            entry['status'] = 'error'
            entry['error'] = 'runner must return an observation dict'
            results.append(entry)
            continue
        entry['observed'] = observed
        for key in sorted(spec['expected']):
            want = spec['expected'][key]
            if key not in observed:
                entry['mismatches'].append({'key': key, 'expected': want,
                                            'observed': None, 'present': False})
            elif observed[key] != want:
                entry['mismatches'].append({'key': key, 'expected': want,
                                            'observed': observed[key], 'present': True})
        if entry['mismatches']:
            entry['status'] = 'fail'
        results.append(entry)
    statuses = [r['status'] for r in results]
    status = ('error' if 'error' in statuses
              else 'fail' if 'fail' in statuses else 'pass')
    return {
        'status': status,
        'cases': results,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'method': 'isolated_fixture_contract_execution',
            'cases': len(results),
            'passed': statuses.count('pass'),
            'failed': statuses.count('fail'),
            'errored': statuses.count('error'),
            'layers': sorted({r['layer'] for r in results}),
            'faults': sorted({r['fault'] for r in results}),
            'llm_reinterpretation': False,
            'network': False,
            'production_ledger': False,
        },
        'limitations': list(LIMITATIONS),
    }