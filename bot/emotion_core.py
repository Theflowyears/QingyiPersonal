"""shadow-v1 冻结参考内核（三轨迹中的 v1_reference），标准库纯函数。

实时影子消费者是 emotion_dynamics（shadow-v2）；本内核的规则与参数已冻结，
只用于旧账本审计与合成参考回放（emotion_replay），不读写存储、不接管表达/行动。
旧 emotion_shadow.db 若是 v1 账本则原样保留作参考，不覆盖成 v2（见
emotion_shadow.shadow_path）。规则/参数改变须更新 VERSION 并另开评审账本。

这些状态/特质是未校准的计算初值，不是心理测量，也不证明主观体验。
六项 traits 固定不学习；本模块不建立或修改关系。confidence 是规则证据强度，
不是经过校准的心理概率。

幂等边界：state 只含约定字段，不能保存已处理 ID；apply_event 是单次转移，
调用者须去重。replay 用 effective_ts（未显式提供则 ts）排序；显式有效同刻按
source_seq > input_seq > 原输入索引取序号，再按 id 稳定排序，不拿 raw ts 重排。
无 effective_ts 的同刻仍按 id，混合同刻时这类事件在前。同 ID 只取排序后首条，
即使首条因时间无效/越界被排除也不重复消费。跨批次去重由外层唯一事件账本负责。
source_seq 应由外层保存为全局源序号，不能用批内索引替代；本模块不推断源顺序。
可在 event['appraisal'] 携带本版本的已记录评估；只接受与确定性规则相符的评估，
防止外部任意 delta 绕过证据门。timeline 仅输出白名单字段及独立状态快照。
"""
import os
import copy
import hashlib
import json
import math


VERSION = "shadow-v1"
PARAMETERS = {
    "version": VERSION,
    "calibration": "uncalibrated computational initial values; not psychometrics",
    "baseline": {"valence": 0.0, "arousal": 0.25, "control": 0.5, "attention": 0.0},
    "tau_seconds": {"valence": 7200.0, "arousal": 1800.0, "control": 3600.0, "attention": 600.0},
    "bounds": {"valence": [-1.0, 1.0], "arousal": [0.0, 1.0],
               "control": [0.0, 1.0], "attention": [0.0, 1.0]},
    "traits": {"curiosity": 0.6, "sociability": 0.5, "caution": 0.6,
               "playfulness": 0.6, "sensitivity": 0.5, "expressiveness": 0.5},
    "message_attention": 0.02,
    "praise_valence": 0.04,
    "praise_control": 0.015,
    "praise_evidence": ["谢谢你", "你帮大忙", "你好厉害"],
    "tool_control": 0.025,
    "tool_attention": 0.01,
}
_DIMENSIONS = ("valence", "arousal", "control", "attention")
# 仅限制验证路径的计算量，不属于情绪规则/校准参数。
_MAX_REPLAY_STEPS = 100_000


def parameter_hash() -> str:
    """Canonical UTF-8 JSON (sorted keys, compact separators) 的 SHA256。"""
    encoded = json.dumps(PARAMETERS, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def initial_state(at: float) -> dict:
    """建立无自发低气压的基线；每次返回独立 traits 字典。"""
    return {"at": _number(at, "at"), **PARAMETERS["baseline"],
            "processed": 0, "traits": dict(PARAMETERS["traits"])}


def _state_copy(state):
    if not isinstance(state, dict) or set(state) != {
        "at", "processed", "traits", *_DIMENSIONS,
    }:
        raise ValueError("state must have exactly the shadow-v1 state keys")
    result = {"at": _number(state["at"], "state.at")}
    for key in _DIMENSIONS:
        result[key] = _number(state[key], f"state.{key}")
        low, high = PARAMETERS["bounds"][key]
        if not low <= result[key] <= high:
            raise ValueError(f"state.{key} is outside bounds")
    count = state["processed"]
    if type(count) is not int or count < 0:
        raise ValueError("state.processed must be a nonnegative integer")
    traits = state["traits"]
    if not isinstance(traits, dict) or set(traits) != set(PARAMETERS["traits"]):
        raise ValueError("state.traits must contain the six fixed shadow traits")
    for key, expected in PARAMETERS["traits"].items():
        if _number(traits[key], f"state.traits.{key}") != expected:
            raise ValueError("state.traits are fixed, uncalibrated initial values")
    result.update(processed=count, traits=dict(PARAMETERS["traits"]))
    return result


def advance(state: dict, at: float) -> dict:
    """精确指数恢复；迟到时间不倒退；输入状态从不原地修改。"""
    result = _state_copy(state)
    target = max(result["at"], _number(at, "at"))
    elapsed = target - result["at"]
    if elapsed == 0:
        return result
    for key in _DIMENSIONS:
        base = PARAMETERS["baseline"][key]
        result[key] = base + (result[key] - base) * math.exp(
            -elapsed / PARAMETERS["tau_seconds"][key])
    result["at"] = target
    return result


def _event_header(event):
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    eid = event.get("id")
    if not isinstance(eid, str) or not eid.strip():
        raise ValueError("event.id must be a nonempty string")
    if not isinstance(event.get("kind"), str):
        raise ValueError("event.kind must be a string")
    if not isinstance(event.get("source", ""), str):
        raise ValueError("event.source must be a string")
    ts = _number(event.get("ts"), "event.ts")
    effective = _number(event.get("effective_ts", ts), "event.effective_ts")
    return eid, ts, effective


def _json_values(value):
    """拒绝非 JSON 对象与任何深度的 NaN/inf，不把原始值写进异常。"""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        _number(value, "payload number")
    elif isinstance(value, list):
        for item in value:
            _json_values(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("payload object keys must be strings")
            _json_values(item)
    else:
        raise ValueError("payload must contain JSON-compatible values")


def _reject_constant(_value):
    raise ValueError("payload numbers must be finite")


def _payload(event):
    value = event.get("payload", {})
    try:
        if isinstance(value, str):
            value = json.loads(value, parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError("event.payload must be a JSON object or dict")
        _json_values(value)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("event.payload must be a finite, nonrecursive JSON object") from None
    if "text" in value and not isinstance(value["text"], str):
        raise ValueError("payload.text must be a string")
    return value


def _source_is(source, prefix):
    source = source.casefold()
    return source == prefix or source.startswith(tuple(prefix + sep for sep in ("/", ":", "_", "-")))


def _synthetic(event, payload=None):
    if payload is None:
        # 审计被排除/重复的事件不做 appraisal，也不因其坏 payload 再失败。
        payload = event.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, RecursionError):
                payload = {}
    return (event.get("synthetic") is True
            or _source_is(event.get("source", ""), "synthetic")
            or (isinstance(payload, dict) and payload.get("synthetic") is True))


def _indirect(payload, text):
    # 无可靠引用 span 时宁可漏掉夸奖，不将引用/扮演中的话当作对她的反馈。
    flags = ("quoted", "is_quote", "is_quoted", "quote", "quoted_text",
             "roleplay", "is_roleplay", "role_play")
    if any(payload.get(key) for key in flags):
        return True
    markers = ('"', "'", "`", "“", "”", "‘", "’", "「", "」", "『", "』", ">",
               "引用", "转述", "原话", "他说", "她说", "别人说", "对方说", "扮演",
               "roleplay", "role-play")
    return any(marker in text.casefold() for marker in markers)


def appraise(event: dict) -> dict:
    """仅评估有证据的微小冲击；不猜测他人原话隐含的她的情绪。"""
    eid, ts, effective = _event_header(event)
    payload = _payload(event)
    result = {
        "rule": "unsupported_event", "confidence": 0.0,
        "delta": dict.fromkeys(_DIMENSIONS, 0.0), "evidence_ids": [eid],
        "ignored": True, "reason": "No supported direct evidence for a state impulse.",
        "version": VERSION, "synthetic": _synthetic(event, payload),
    }
    if ts <= 0 or effective <= 0:
        result.update(rule="unknown_timestamp", reason="Nonpositive event time is unknown.")
        return result
    if _source_is(event.get("source", ""), "legacy") or event["kind"] == "legacy_note":
        result.update(rule="legacy_evidence", reason="Legacy evidence cannot establish current emotion.")
        return result
    kind = event["kind"]
    if kind == "action_result":
        result.update(rule="technical_ack", reason="Action acceptance is not a social response or read receipt.")
    elif kind == "tool_result":
        ok = payload.get("ok")
        if type(ok) is bool:
            result.update(rule="tool_success" if ok else "tool_failure", confidence=1.0,
                          ignored=False, reason="Explicit tool status affects technical control only.")
            result["delta"]["control"] = PARAMETERS["tool_control"] * (1 if ok else -1)
            result["delta"]["attention"] = PARAMETERS["tool_attention"]
        else:
            result.update(rule="tool_status_unknown", reason="Tool status is not an explicit boolean.")
    elif kind == "message":
        text = payload.get("text", "").strip()
        if not text:
            result.update(rule="empty_message", reason="No message text; no inferred mood or load.")
            return result
        result.update(rule="message_attention", confidence=0.5, ignored=False,
                      reason="Observed message adds a small attention load, not the speaker's mood.")
        result["delta"]["attention"] = PARAMETERS["message_attention"]
        if (payload.get("addressed_to_self") is True and not _indirect(payload, text)
                and any(phrase in text for phrase in list(PARAMETERS["praise_evidence"]) + [x for x in os.getenv("EMOTION_EVIDENCE_EXTRA", "").split(",") if x.strip()])):
            result.update(rule="direct_praise", confidence=0.8,
                          reason="Explicit self-addressed appreciation, outside quotation or roleplay.")
            result["delta"]["valence"] = PARAMETERS["praise_valence"]
            result["delta"]["control"] = PARAMETERS["praise_control"]
    return result


def _checked_appraisal(event, appraisal):
    expected = appraise(event)
    if appraisal is None:
        return expected
    if not isinstance(appraisal, dict):
        raise ValueError("appraisal must be a shadow-v1 dict")
    if appraisal.get("evidence_ids") != expected["evidence_ids"]:
        raise ValueError("appraisal evidence_ids must match event.id")
    if appraisal.get("version") != VERSION:
        raise ValueError("appraisal version must match shadow-v1")
    confidence = _number(appraisal.get("confidence"), "appraisal.confidence")
    delta = appraisal.get("delta")
    if not isinstance(delta, dict) or set(delta) != set(_DIMENSIONS):
        raise ValueError("appraisal.delta must have all four dimensions")
    for key in _DIMENSIONS:
        _number(delta[key], f"appraisal.delta.{key}")
    if not 0 <= confidence <= 1 or type(appraisal.get("ignored")) is not bool:
        raise ValueError("appraisal confidence/ignored are invalid")
    if (set(appraisal) != set(expected)
            or any(appraisal[key] != expected[key] for key in expected)
            or type(appraisal.get("synthetic")) is not bool):
        raise ValueError("appraisal must match the deterministic shadow-v1 evidence rules")
    # 输出 canonical 内容而不传播外部 rule/reason 或嵌套引用。
    return expected


def _impulse(state, assessment):
    result = _state_copy(state)
    for key in _DIMENSIONS:
        low, high = PARAMETERS["bounds"][key]
        result[key] = min(high, max(low, result[key] + assessment["delta"][key]))
    result["processed"] += 1
    return result


def apply_event(state: dict, event: dict, appraisal=None) -> dict:
    """在 max(state.at, effective_ts 或 ts) 恢复后施加冲击，processed +1。

    ignored 事件也计作一次消费但冲击为零。此原语不去重；请用 replay 或外层唯一 ID。
    """
    _, _, effective = _event_header(event)
    assessment = _checked_appraisal(event, appraisal)
    return _impulse(advance(state, effective), assessment)


def _timeline_row(eid, state, assessment, after):
    return {
        "event_id": eid, "at": state["at"], "rule": assessment["rule"],
        "delta": dict(assessment["delta"]), "before": copy.deepcopy(state),
        "after": copy.deepcopy(after), "ignored": assessment["ignored"],
        "reason": assessment["reason"], "confidence": assessment["confidence"],
        "version": VERSION, "synthetic": assessment["synthetic"],
    }


def _replay_key(header, event, index):
    eid, ts, effective = header
    if "effective_ts" not in event:
        return ts, 0, 0, eid
    for key in ("source_seq", "input_seq"):
        if key in event and (type(event[key]) is not int or event[key] < 0):
            raise ValueError(f"event.{key} must be a nonnegative integer")
    sequence = event.get("source_seq", event.get("input_seq", index))
    # 序号保留 Python int 精度；例如 SQLite rowid 超过 2**53 也不合并。
    return effective, 1, sequence, eid


def _replay_advance(state, target, tick, steps):
    if tick is None or target <= state["at"]:
        return advance(state, target), steps
    origin = state["at"]
    elapsed = target - origin
    if elapsed > tick and (origin + tick <= origin or target - tick >= target):
        raise ValueError("tick_seconds is below timestamp precision")
    ratio = elapsed / tick
    if not math.isfinite(ratio) or ratio > _MAX_REPLAY_STEPS - steps:
        raise ValueError("tick_seconds requires too many replay steps")
    count = max(1, math.ceil(ratio))
    if count > _MAX_REPLAY_STEPS - steps:
        raise ValueError("tick_seconds requires too many replay steps")
    for index in range(1, count + 1):
        # 从段起点计算，避免累计加法误差；最后一步精确落在事件/窗口终点。
        at = target if index == count else min(target, origin + index * tick)
        if at <= state["at"]:
            raise ValueError("tick_seconds is below timestamp precision")
        state = advance(state, at)
        steps += 1
        if at == target:
            break
    return state, steps


def replay(events, *, start_at: float, end_at: float, tick_seconds=None) -> dict:
    """从 start_at 的基线重放闭区间内事件；原始/有效时间均不得越界。

    排序见模块契约；source_seq/input_seq 若提供须为非负整数，仅显式有效时间
    使用它们。无有效时间的事件保留 (ts, id) 次序。重复/无效/越界事件不恢复状态。
    ts<=0（含有效时间）排除并审计；NaN/inf/坏结构明确 ValueError。
    before 为本次事件时刻、冲击前的已恢复状态。排除项 before==after，at 为当前
    状态时间。tick_seconds=None 直接解析恢复；否则从每段当前状态起按正有限 tick
    真实调用 advance，余步到事件有效时间，末段到 end_at；不为 tick 生成 timeline。
    一次调用最多 100000 次正时长步进（含余步），超限或 tick 小于时间戳浮点精度
    明确 ValueError，不跳过步进冒充验证。初值/规则/恢复公式及 VERSION 均未改变。
    """
    start = _number(start_at, "start_at")
    end = _number(end_at, "end_at")
    if start < 0 or end < start:
        raise ValueError("start_at/end_at must define a nonnegative ordered window")
    tick = None if tick_seconds is None else _number(tick_seconds, "tick_seconds")
    if tick is not None:
        if tick <= 0:
            raise ValueError("tick_seconds must be positive")
        # 先做全窗下界检查，极小 tick/巨大窗口不进入循环或分配 tick 数组。
        if (end - start) / tick > _MAX_REPLAY_STEPS:
            raise ValueError("tick_seconds requires too many replay steps")
    try:
        ordered = []
        for index, value in enumerate(events):
            header = _event_header(value)
            ordered.append((_replay_key(header, value, index), header, value))
    except TypeError:
        raise ValueError("events must be an iterable of event dicts") from None
    ordered.sort(key=lambda item: item[0])
    state = initial_state(start)
    steps = 0
    timeline = []
    seen = set()
    for _, (eid, ts, effective), value in ordered:
        rule = None
        if eid in seen:
            rule, reason = "duplicate_event", "Event ID was already encountered; no second impulse."
        elif ts <= 0 or effective <= 0:
            rule, reason = "unknown_timestamp", "Nonpositive event time is unknown; excluded."
        elif not (start <= effective <= end and
                  (("effective_ts" in value and ts <= end) or
                   start <= ts <= end)):
            rule, reason = "outside_window", "Effective event time lies outside the replay window."
        seen.add(eid)
        if rule is not None:
            assessment = {"rule": rule, "delta": dict.fromkeys(_DIMENSIONS, 0.0),
                          "ignored": True, "reason": reason, "confidence": 0.0,
                          "synthetic": _synthetic(value)}
            timeline.append(_timeline_row(eid, state, assessment, state))
            continue
        assessment = _checked_appraisal(value, value.get("appraisal"))
        before, steps = _replay_advance(state, effective, tick, steps)
        state = _impulse(before, assessment)
        timeline.append(_timeline_row(eid, before, assessment, state))
    state, _ = _replay_advance(state, end, tick, steps)
    return {"state": state, "timeline": timeline, "parameter_hash": parameter_hash()}
