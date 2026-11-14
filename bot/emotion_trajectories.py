"""S01 三轨迹与版本口径（统一方案 §9.1/§9.7，目录题「实时、历史、候选」）。

三条可对照的轨迹，不是三个有权同时决定表达的内核：
- v1_reference：旧冻结内核与合成参考回放；历史保留，不覆盖成 v2。
- v2_live：当前严格规则与动力学观察，唯一计入在线有效证据的轨迹。
- candidate_counterfactual：对已发生消息的另一种解释实验；迟到标签可重建，
  不计在线观察、不回写实时状态。
historical_replay 是 v2 源账本的只读追溯视图（面板五口径之一），同样不计在线；
它不是第四条决策轨迹，单列是为了不让历史回放冒充实时样本。

口径字段统一为 trajectory / kernel_version / counts_for_live；
counts_for_live 由契约决定，不由报告生产者自报。旧名 counts_as_live_observation 退役。
本模块是纯契约：不碰存储、不解析路径、不做评估。
"""
import emotion_core as v1
import emotion_dynamics as v2

V1_REFERENCE = 'v1_reference'
V2_LIVE = 'v2_live'
CANDIDATE_COUNTERFACTUAL = 'candidate_counterfactual'
HISTORICAL_REPLAY = 'historical_replay'

# trajectory -> (评估契约版本, 是否计入在线观察)
CONTRACTS = {
    V1_REFERENCE: (v1.VERSION, False),
    V2_LIVE: (v2.VERSION, True),
    CANDIDATE_COUNTERFACTUAL: (v2.SEMANTIC_VERSION, False),
    HISTORICAL_REPLAY: (v2.VERSION, False),
}


def stamp(report, trajectory):
    """给轨迹报告盖统一口径；在线标志以契约为准，覆盖调用方自带的值。"""
    version, live = CONTRACTS[trajectory]
    report['trajectory'] = trajectory
    report['kernel_version'] = version
    report['counts_for_live'] = live
    return report