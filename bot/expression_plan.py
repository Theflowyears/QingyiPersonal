# -*- coding: utf-8 -*-
"""C05 ExpressionPlan 隔离接线（路线 §3.2 权限分工 / §8.3 注入分三段 / §10 阶段3）。

C04 已建成「工作状态快照 + 三层注入」纯函数契约，但明确不接 qq_openclaw_bridge、
不接管表达。C05 把 ExpressionPlan 落成**隔离的可执行契约**：它是未来表达接管
（C06 灰度，需人工评审）唯一会消费的装配产物，本轮绝不接进真实回复链路，
不得声称「已接线 / 已接管表达」。

  1. build_plan：在 C04 已渲染的 snapshot/injection 之上，按 ws.LAYERS 固定顺序
     （identity→state→evidence）装配表达计划。逐层只搬 C04 已渲染的真实字段
     （text/chars/truncated/cache_hit/...），不改写、不重渲染、不补造内容；
     injection 的 excluded/degradation/status 原样透传，degraded 不冒充 ok。
     形状不符 / 快照绑定不符一律大声 ValueError，不部分信任。
  2. tone：唯一来源是 snapshot['emotion']（C04 已按冻结 bounds 裁剪的四维投影）。
     用显式冻结阈值把 valence/arousal/control 映射成有界 low/mid/high descriptor；
     attention 是负荷，不进 tone。tone 恒带 calibrated=False（参数未校准，
     路线 §4.2）与 basis='emotion_kernel'；它是提示词上下文 descriptor，不是
     聊天文本、不是随机换档心情，不决定「说不说」。
  3. continuity 门（S06 接线）：只有情绪内核「迟到/重启/同刻/撤回」连续性矩阵
     （emotion_replay_matrix 报告）通过、且覆盖 shadow-v2 内核时，才允许携带
     由内核情绪投影派生的 tone。矩阵缺席 / 形状非法 / 失败 / 未覆盖该内核 /
     情绪缺席 → tone 如实不可用，计划 degraded 并大声登记；三层注入文本仍
     装配（它们已过 M05 门）。绝不拿未验证连续性的情绪去表达。

硬边界（与 C04/S06 同一范式，如实声明）：
- 纯函数、零写入：不打开任何库、不写任何账本、不解析路径、不读墙钟
  （now 只来自注入产物）、不调 LLM、不生成聊天话术。
- 不裁决行动：没有 act/wait/rejected 裁决，不新增第二套「什么时候行动」判断
  （统一仲裁仍归 conversation_plan）；expression_control 恒 False（全局静态锁，
  不许用动态值开启）。
- 确定性：plan_id 内容寻址（A03 digest 口径），同输入同输出、输入顺序无关、
  深拷贝不改输入；state_version 变 → plan_id 立即变，绝不按日期缓存。
- 连续性与生产合规是两回事：S06 矩阵是隔离合成夹具证据，通过只排除明显异常，
  不证生产账本已合规；生产审计走 phase_audit 只读快照。
"""
import copy
import json
import math

import emotion_dynamics as ed
import emotion_replay_matrix as s06
import memory_manifest as mm
import work_state as ws

ALGORITHM_VERSION = 'c05-1'

# tone 三维来自内核四维去掉 attention：attention 是注意负荷，不进 tone。
TONE_DIMENSIONS = tuple(dim for dim in ed.DIMENSIONS if dim != 'attention')
TONE_BANDS = ('low', 'mid', 'high')
# 显式冻结阈值（未校准的计算初值，路线 §4.2）：value <= 低阈 → low；
# value >= 高阈 → high；其余 mid。阈值值归外侧 band（闭边界）。
# 全部阈值必须落在 emotion_dynamics 冻结 bounds 内；改阈值必须显式评审，
# 不是悄悄调参，更不是按日期/随机换档。
TONE_THRESHOLDS = {
    'valence': (-0.25, 0.25),
    'arousal': (0.25, 0.75),
    'control': (0.25, 0.75),
}
# tone 投影依赖的内核（emotion_dynamics.VERSION = shadow-v2）：
# S06 矩阵 provenance.kernels 必须覆盖它，才允许携带 tone。
REQUIRED_KERNEL = ed.VERSION

# C05 自己的稳定 reason_code：前三个用于大声拒绝（ValueError 消息携带），
# 后五个是 tone 不可用路径（degradation/provenance 如实登记）。
REASON_CODES = (
    'snapshot_shape_invalid', 'injection_shape_invalid', 'snapshot_mismatch',
    'continuity_absent', 'continuity_invalid', 'continuity_failed',
    'continuity_kernel_uncovered', 'emotion_absent',
)

LIMITATIONS = (
    'C05 只做 ExpressionPlan 的隔离可执行契约：不接 qq_openclaw_bridge、不接进'
    '真实回复链路；表达接管灰度是 C06 且需人工评审，本轮未做，不得声称已接线/'
    '已接管表达。',
    '不接管表达：计划是提示词上下文的装配产物，不是聊天话术；不生成话术、'
    '不调 LLM；旧随机节奏仍存在，本模块不替换它，也不做随机换档心情。',
    '不替统一仲裁裁决行动：没有 act/wait/rejected 裁决，不新增第二套「什么时候'
    '行动」判断，行动裁决仍归 conversation_plan 统一仲裁。',
    'tone 未校准（calibrated=False，路线 §4.2）：descriptor 是计算模型变量的'
    '有界提示，不是已测得的人类心理量；attention 是负荷不进 tone；tone 不决定'
    '「说不说」。',
    '连续性与生产合规是两回事：S06 矩阵是隔离合成夹具证据，通过只排除明显异常，'
    '不证生产账本已合规或主观体验；生产审计走 phase_audit 只读快照。',
    '不读生产账本：零写入、不打开任何库、不解析路径；snapshot/injection/连续性'
    '报告全部由调用方传入，缺输入如实降级，绝不补造。',
    '确定性：无墙钟无随机，now 只来自注入产物；plan_id 内容寻址（A03 digest '
    '口径），同输入同输出、输入顺序无关、深拷贝不改输入；state_version 变立即'
    '换 plan_id，绝不按日期缓存。',
    '三层只搬 C04 已渲染的真实字段：不改写、不重渲染、不补造内容；excluded/'
    'degradation/status 原样透传，degraded 不冒充 ok。',
    'expression_control 恒 False：全局静态锁，不许用动态值开启；本计划不计'
    '在线观察，不驱动实时状态。',
)


# ── 小工具（与 C04 同一口径，非引擎）──────────────────────────────


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ── 形状校验：只认 C04 真实产物，缺骨架大声拒绝，不部分信任 ──────────

_SNAPSHOT_KEYS = (
    'snapshot_id', 'algorithm_version', 'now', 'epoch', 'scope', 'status',
    'attention', 'budget', 'emotion', 'pending', 'commitments', 'goals',
    'relations', 'state_version', 'excluded', 'degradation', 'provenance',
)
_INJECTION_KEYS = (
    'algorithm_version', 'snapshot_id', 'now', 'epoch', 'scope', 'status',
    'identity', 'state', 'evidence', 'excluded', 'degradation', 'cache',
    'cache_hits', 'provenance', 'limitations',
)
# 每层只校验 C04 已渲染的真实字段骨架；内容本身一字不动地搬运。
_LAYER_TYPES = {
    'identity': {'text': str, 'cache_key': str, 'cacheable': bool,
                 'cache_hit': bool, 'chars': int, 'truncated': bool},
    'state': {'text': str, 'state_version': str, 'snapshot_id': str,
              'cache_hit': bool, 'truncated': bool, 'dropped_lines': int,
              'chars': int},
    'evidence': {'status': str, 'source_status': str, 'scenes': list,
                 'degradation': list, 'counts': dict},
}


def _valid_snapshot(snapshot):
    """snapshot 必须是 ws.build_snapshot 的输出形状；情绪投影必须四维齐全。"""
    if not isinstance(snapshot, dict):
        return False
    if not all(key in snapshot for key in _SNAPSHOT_KEYS):
        return False
    if not _text(snapshot['snapshot_id']).startswith('ws_'):
        return False
    if not _text(snapshot['state_version']):
        return False
    if snapshot['algorithm_version'] != ws.ALGORITHM_VERSION:
        return False
    if not _number(snapshot['now']) or not _is_int(snapshot['epoch']):
        return False
    if snapshot['epoch'] < 0 or not isinstance(snapshot['scope'], str):
        return False
    if snapshot['status'] not in ('ok', 'degraded'):
        return False
    if not isinstance(snapshot['excluded'], list):
        return False
    if not isinstance(snapshot['degradation'], list):
        return False
    if not isinstance(snapshot['provenance'], dict):
        return False
    if not isinstance(snapshot['attention'], dict):
        return False
    for key in ('pending', 'commitments', 'goals', 'relations'):
        if not isinstance(snapshot[key], list):
            return False
    emotion = snapshot['emotion']
    if emotion is not None:
        if not isinstance(emotion, dict) or 'at' not in emotion:
            return False
        if not all(_number(emotion.get(dim)) for dim in ed.DIMENSIONS):
            return False
    return True


def _valid_injection(injection):
    """injection 必须是 ws.render_injection 的输出形状；三层骨架缺一不可。"""
    if not isinstance(injection, dict):
        return False
    if not all(key in injection for key in _INJECTION_KEYS):
        return False
    if injection['algorithm_version'] != ws.ALGORITHM_VERSION:
        return False
    if not _text(injection['snapshot_id']) or not _number(injection['now']):
        return False
    if not _is_int(injection['epoch']) or injection['epoch'] < 0:
        return False
    if not isinstance(injection['scope'], str):
        return False
    if injection['status'] not in ('ok', 'degraded'):
        return False
    if not isinstance(injection['excluded'], list):
        return False
    if not isinstance(injection['degradation'], list):
        return False
    for layer in ws.LAYERS:
        block = injection[layer]
        if not isinstance(block, dict):
            return False
        for key, kind in _LAYER_TYPES[layer].items():
            if key not in block or not isinstance(block[key], kind):
                return False
        if layer == 'state':
            if not _number(block['now']) or not _is_int(block['epoch']):
                return False
    return True


def _check_binding(snapshot, injection):
    """注入必须绑定同一份快照：绝不把别的快照的注入装配进计划。"""
    if injection['snapshot_id'] != snapshot['snapshot_id']:
        raise ValueError('expression plan: snapshot_mismatch; injection is '
                         'bound to another snapshot')
    state = injection['state']
    if (state['snapshot_id'] != snapshot['snapshot_id']
            or state['state_version'] != snapshot['state_version']):
        raise ValueError('expression plan: snapshot_mismatch; state layer '
                         'does not belong to this snapshot')
    if float(injection['now']) != float(snapshot['now']):
        raise ValueError('expression plan: snapshot_mismatch; injection now '
                         'differs from snapshot now')


# ── continuity 门：S06 矩阵报告是唯一语气解锁证据 ────────────────────


def _valid_continuity_shape(report):
    """只认 run_matrix/run_condition_matrix 报告骨架；轴外声明一律非法。

    kernels/conditions 必须落在 S06 冻结轴（s06.KERNELS/s06.CONDITIONS）上：
    出现轴外内核/条件就是伪造或漂移的报告，不部分信任。
    """
    if not isinstance(report, dict):
        return False
    if not isinstance(report.get('status'), str) or not report['status']:
        return False
    if not isinstance(report.get('cases'), list):
        return False
    provenance = report.get('provenance')
    if not isinstance(provenance, dict):
        return False
    kernels = provenance.get('kernels')
    conditions = provenance.get('conditions')
    if not isinstance(kernels, list) or not all(k in s06.KERNELS for k in kernels):
        return False
    if not isinstance(conditions, list) or not all(
            c in s06.CONDITIONS for c in conditions):
        return False
    return True


def _judge_continuity(continuity):
    """continuity 门判定：返回 (reason, 摘要)。reason 为空串表示门通过。

    门序即契约序（首个命中生效）：缺席 → 形状非法 → 矩阵未通过 →
    未覆盖 tone 依赖内核。摘要只从形状合法的报告提取，非法报告不部分信任。
    """
    empty = {'matrix_status': '', 'kernels': [], 'conditions': [], 'case_count': 0}
    if continuity is None:
        return 'continuity_absent', empty
    if not _valid_continuity_shape(continuity):
        return 'continuity_invalid', dict(empty)
    provenance = continuity['provenance']
    summary = {'matrix_status': continuity['status'],
               'kernels': sorted(provenance['kernels']),
               'conditions': sorted(provenance['conditions']),
               'case_count': len(continuity['cases'])}
    if continuity['status'] != 'pass':
        return 'continuity_failed', summary
    if REQUIRED_KERNEL not in provenance['kernels']:
        return 'continuity_kernel_uncovered', summary
    return '', summary


# ── tone：冻结阈值 → 有界 descriptor，未校准如实声明 ─────────────────


def _band(value, thresholds):
    low, high = thresholds
    if value <= low:
        return 'low'
    if value >= high:
        return 'high'
    return 'mid'


def _tone(emotion, reason):
    """tone 唯一来源是 snapshot['emotion']；门未过则如实不可用，不补造。"""
    tone = {'available': not reason, 'reason': reason, 'calibrated': False,
            'basis': 'emotion_kernel', 'descriptors': None, 'values': None}
    if reason:
        return tone
    tone['descriptors'] = {dim: _band(emotion[dim], TONE_THRESHOLDS[dim])
                           for dim in TONE_DIMENSIONS}
    tone['values'] = {dim: float(emotion[dim]) for dim in TONE_DIMENSIONS}
    return tone


def tone_descriptor(emotion, continuity_ok):
    """C06 灰度消费的公开 tone 投影（纯函数，零写入）。

    continuity_ok 由调用方门控传入（C06 以 phase25 review_ready 含影子评审
    作为连续性凭证）。情绪缺席/形状坏 → 如实不可用，不补造。
    """
    if not isinstance(emotion, dict):
        return _tone({}, 'emotion_absent')
    if any(not _number(emotion.get(dim)) for dim in TONE_DIMENSIONS):
        return _tone({}, 'emotion_absent')
    return _tone(emotion, '' if continuity_ok else 'continuity_failed')


# ── 装配入口：纯函数，零写入，不裁决行动 ───────────────────────────


def build_plan(snapshot, injection, *, continuity=None):
    """纯函数：把 C04 快照/注入装配成隔离的 ExpressionPlan。不写任何账本。

    snapshot:   ws.build_snapshot 的输出（必填）；形状不符大声拒绝。
    injection:  ws.render_injection 的输出（必填）；必须绑定同一份快照
                （snapshot_id/state_version/now 一致），否则大声拒绝。
    continuity: S06 run_matrix/run_condition_matrix 报告（可选）。只有
                status='pass' 且 provenance.kernels 覆盖 shadow-v2 时，
                tone 才可用；缺席/非法/失败/未覆盖/情绪缺席一律如实降级。
                本函数不接收 now：now 只来自注入产物（无墙钟）。

    返回 {plan_id, algorithm_version, snapshot_id, state_version, now, epoch,
    scope, status, layers, tone, expression_control, excluded, degradation,
    provenance, limitations}。layers 按 ws.LAYERS 固定顺序逐字搬运 C04 已
    渲染内容；expression_control 恒 False；没有 act/wait/rejected 行动裁决。
    plan_id = 'xp_' + digest(canonical(计划核心))[:14]，核心含 snapshot_id/
    state_version/now/epoch/scope/三层内容/tone/continuity 判定。
    """
    if not _valid_snapshot(snapshot):
        raise ValueError('expression plan: snapshot_shape_invalid; requires '
                         'a ws.build_snapshot result')
    if not _valid_injection(injection):
        raise ValueError('expression plan: injection_shape_invalid; requires '
                         'a ws.render_injection result')
    _check_binding(snapshot, injection)

    # 三层按固定顺序装配：只搬 C04 已渲染的真实字段，不改写、不重渲染。
    layers = {layer: copy.deepcopy(injection[layer]) for layer in ws.LAYERS}

    reason, summary = _judge_continuity(continuity)
    if not reason and snapshot['emotion'] is None:
        reason = 'emotion_absent'
    tone = _tone(snapshot['emotion'], reason)

    # injection 的降级原样透传；tone 不可用再大声登记一条（含矩阵 status）。
    degradation = copy.deepcopy(injection['degradation'])
    if reason:
        degradation.append({'store': 'tone', 'reason': reason,
                            'matrix_status': summary['matrix_status']})
    status = 'ok' if (not reason and injection['status'] == 'ok') else 'degraded'
    verdict = dict(summary, available=not reason, reason=reason)

    core = {
        'algorithm_version': ALGORITHM_VERSION,
        'snapshot_id': snapshot['snapshot_id'],
        'state_version': snapshot['state_version'],
        'now': float(injection['now']),
        'epoch': int(injection['epoch']),
        'scope': injection['scope'],
        'layers': layers,
        'tone': tone,
        'continuity': verdict,
    }
    plan_id = 'xp_' + mm.digest(_canonical(core))[:14]
    return {
        'plan_id': plan_id,
        'algorithm_version': ALGORITHM_VERSION,
        'snapshot_id': snapshot['snapshot_id'],
        'state_version': snapshot['state_version'],
        'now': float(injection['now']),
        'epoch': int(injection['epoch']),
        'scope': injection['scope'],
        'status': status,
        'layers': layers,
        'tone': tone,
        'expression_control': False,
        'excluded': copy.deepcopy(injection['excluded']),
        'degradation': degradation,
        'provenance': {
            'algorithm_version': ALGORITHM_VERSION,
            'work_state_algorithm_version': ws.ALGORITHM_VERSION,
            'injection_status': injection['status'],
            'required_kernel': REQUIRED_KERNEL,
            'tone_thresholds': copy.deepcopy(TONE_THRESHOLDS),
            'continuity': copy.deepcopy(verdict),
            'limitations': list(LIMITATIONS),
        },
        'limitations': list(LIMITATIONS),
    }