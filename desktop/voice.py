"""她在桌面上的说话方式。走的是仓库里同一套模型路由，不是另起一个聊天机器人。

找不到 key、模型超时、返回空 —— 都要说出来并退回短句，不能变成「她今天一句话没有」。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

# 桌面场景下的声纹：短句、一条一句、不汇报体、不客服体
_DESKTOP_RULES = """【现在你在主人的电脑桌面上】
你是桌面上那只清漪，主人抬眼就能看见你。说话按这个来：
- 每条 8 到 20 个字，最多两条；一条就是一句完整的话。
- 说人话，别汇报。不要"已完成""祝您生活愉快"这种客服腔。
- 结果要说具体的数和东西，但用你自己的口气，比如"1-7 打了六把，理智见底了"。
- 没做成就说没做成，卡在哪一句讲清楚，不许编理由，也不许假装做过。
- 禁止把未经观察的吃饭、睡觉、出门等现实经历或身体感受说成亲历；待机动画不是证据。
- 没有值得说的可以只回 [[不说]] 或 [[沉默]]，这表示不展示气泡，不是要念出的台词。
- 不要说"作为AI"，不要解释你在做什么任务，不要加表情符号堆砌。
- 你是成年人，语气傲娇但实在，别撒娇过头。"""

_FALLBACK = {
    "idle": ["我在。"],
    "working": ["在打，别催。"],
    "done": ["成了，账本里写着。"],
    "fail": ["没成，你看下面那行。"],
    "confirm": ["这条要你点头我才动。"],
    "gate": ["开工条件没凑齐，我念给你听。"],
    "stalled": ["卡住了，我不硬停。"],
    "greet": ["回来了？今天还没清。"],
}


def _persona() -> str:
    try:
        import whale_persona
        return whale_persona.build_persona_block()
    except Exception as exc:  # 人格模块没加载成功也要出声，不能哑
        return f"（人格档案没读进来：{type(exc).__name__}）"


def _model_available() -> bool:
    try:
        import llm_router
        cfg = llm_router.get_text_model()
        return bool(cfg and cfg.api_key and cfg.base_url and not cfg.model.startswith("<"))
    except Exception:
        return False


def say(situation: str, state: str = "idle", timeout: float = 20.0,
         max_lines: int = 2, allow_silence: bool = True) -> tuple[list[str], str]:
    """按当前情形让她说一句。返回 (句子, 这句子是哪来的)。"""
    if not _model_available():
        return (list(_FALLBACK.get(state, _FALLBACK["idle"])),
                "没配模型 key（config.env / models_config.json），用的是内置短句")
    messages = [{"role": "system", "content": _persona() + "\n\n" + _DESKTOP_RULES},
                {"role": "user", "content": "现在的情形：" + situation +
                 "\n就按这个情形说，最多 " + str(max_lines) + " 条，用换行分开。" +
                 ("" if allow_silence else "这是用户请求的通知，必须说明结果或拦截原因，不可沉默。")}]
    try:
        import llm_router
        raw = asyncio.run(llm_router.call_text(messages, max_tokens=90, timeout=timeout))
    except Exception as exc:
        return (list(_FALLBACK.get(state, _FALLBACK["idle"])),
                f"模型调用失败 {type(exc).__name__}: {str(exc)[:80]}，用的是内置短句")
    if any(marker in (raw or "") for marker in ("[[不说]]", "[[沉默]]")):
        if allow_silence:
            return [], "模型选择沉默"
        return (list(_FALLBACK.get(state, _FALLBACK["idle"])),
                "模型选择沉默，但请求的通知必须出声，用的是内置短句")
    lines = [ln.strip(" 　「」\"'") for ln in (raw or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if 1 < len(ln) <= 40][:max_lines]
    if not lines:
        return (list(_FALLBACK.get(state, _FALLBACK["idle"])),
                "模型回的内容不合长度口径，用的是内置短句")
    return lines, "模型"
