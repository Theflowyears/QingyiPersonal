# -*- coding: utf-8 -*-
"""C06 阶段3a 局部灰度：把情感内核的 tone 投影在人工授权后接进回复链路。

硬边界（守住红线，可回退）：
- 默认关闭。只有「主人本人签字（ops stage3_signoff.granted）」且「工程+自然
  门全过（phase25 review_ready）」同时满足才激活；任一不满足=observe-only，
  回复链路一个字不变。
- 只注入一个有界、带标签的 tone descriptor（提示词上下文），不生成话术、
  不裁决行动（act/wait 仍归 conversation_plan）、不替换人格。
- 不翻 emotion_shadow 的 expression_control 静态锁（影子仍 observe-only）；
  灰度是独立的人工授权注入路径，撤回签字立即关闭。
- tone 未校准（calibrated=False），是计算模型的有界提示，不是测得的心理量。
- 门状态缓存 60s，避免每条回复都读库；撤回签字最迟 60s 内生效。
"""
import time

_CACHE = {"at": 0.0, "status": None}
_TTL = 60


def gate_status(now=None):
    """返回 {active, signoff_granted, ready, reasons}。缓存 60s。"""
    at = time.time() if now is None else float(now)
    if _CACHE["status"] is not None and at - _CACHE["at"] < _TTL:
        return _CACHE["status"]
    reasons = []
    granted = False
    ready = False
    try:
        import ops_channel
        granted = bool((ops_channel.stage3_signoff() or {}).get("granted"))
    except Exception:
        reasons.append("signoff_unreadable")
    try:
        import phase_audit
        rep = phase_audit.audit_readiness()
        ready = (rep.get("phase25") or {}).get("status") == "review_ready"
    except Exception:
        reasons.append("readiness_unreadable")
    if not granted:
        reasons.append("no_human_signoff")
    if not ready:
        reasons.append("gates_not_ready")
    status = {"active": bool(granted and ready), "signoff_granted": granted,
              "ready": ready, "reasons": reasons}
    _CACHE.update({"at": at, "status": status})
    return status


def invalidate():
    _CACHE.update({"at": 0.0, "status": None})


def tone_block():
    """激活时返回有界带标签的 tone 上下文；否则空串（回复链路不变。"""
    g = gate_status()
    if not g["active"]:
        return ""
    try:
        import emotion_shadow
        import expression_plan
        st = emotion_shadow.status(limit=1)
        emo = st.get("state") or {}
        tone = expression_plan.tone_descriptor(emo, continuity_ok=g["ready"])
        if not tone.get("available") or not tone.get("descriptors"):
            return ""
        d = tone["descriptors"]
        return ("\n【内部状态·灰度（已人工授权，仅语气提示，不裁决说不说）】"
                "情绪tone：valence=%s arousal=%s control=%s（未校准，参考即可）"
                % (d.get("valence"), d.get("arousal"), d.get("control")))
    except Exception:
        return ""