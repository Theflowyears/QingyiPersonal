"""让视频模型真正看一遍 B 站视频的画面与音轨。

文本模型读字幕只能知道「说了什么」，不知道「画面里发生了什么」。这里把
可访问的视频文件交给 qwen3.8-omni-flash，要求它按时间点描述观察、把推测
单列，解析失败就如实返回 ok=False —— 绝不拿标题和简介冒充「看过」。
"""
import json
import math
import os
import re

DEFAULT_MODEL_NOTE = "qwen3.8-omni-flash"
MAX_SUBTITLE_CHARS = int(os.getenv("BILI_VISION_SUBTITLE_CHARS", "4000"))

# 每次视频理解最多烧多少 token（实测 qwen omni ≈ 260 token/帧，
# 之前按 37 估会严重超支）。10 万预算 ≈ 384 帧；长片动态降 fps。
VIDEO_TOKEN_BUDGET = int(os.getenv("VIDEO_TOKEN_BUDGET", "100000"))
VIDEO_TOKENS_PER_FRAME = float(os.getenv("VIDEO_TOKENS_PER_FRAME", "260"))
VIDEO_FPS_MAX = float(os.getenv("VIDEO_FPS_MAX", "2.0"))
VIDEO_FPS_MIN = float(os.getenv("VIDEO_FPS_MIN", "0.1"))
VIDEO_MAX_OUTPUT_TOKENS = int(os.getenv("VIDEO_MAX_OUTPUT_TOKENS", "8000"))


def plan_video_fps(duration_s):
    """按 token 预算动态定采样帧率：时长越长帧越稀，保证 frames*每帧token ≤ 预算。

    返回 (fps, 预计帧数)。100000 预算 / 260 → 最多约 384 帧：
    短视频仍用 2fps；30 分钟片自动降到 ~0.21fps；向下取整确保不超预算。
    """
    dur = int(duration_s or 0)
    if dur <= 0:
        dur = 600
    max_frames = max(1, int(VIDEO_TOKEN_BUDGET // max(1.0, VIDEO_TOKENS_PER_FRAME)))
    fps = max_frames / float(dur)
    fps = min(VIDEO_FPS_MAX, max(VIDEO_FPS_MIN, fps))
    fps = math.floor(fps * 100) / 100.0
    if fps <= 0:
        fps = VIDEO_FPS_MIN
    return fps, int(round(fps * dur))

_VISION_SYSTEM = (
    "你正在完整观看一段视频（画面与音轨）。只根据视频本身作答。\n"
    "要求：\n"
    "1. 先给一句总体概述；\n"
    "2. 再按时间点列 3 到 10 条关键内容（时间用 mm:ss，允许近似）；\n"
    "3. 把画面观察、人物对白/语音、你的推测分成不同字段，推测不得写成事实；\n"
    "4. 看不清、听不清、视频没加载或信息不足，就写进 uncertainties；\n"
    "5. 只输出 JSON，不要 markdown。\n"
    'JSON：{"overview":"", "timeline":[{"t":"00:12","what":""}], '
    '"key_points":[], "uncertainties":[], "emotional_beats":[]}'
)


def _extract_json(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    fence = chr(96) * 3
    raw = raw.replace(fence + "json", "").replace(fence, "").strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        got = json.loads(raw[a:b + 1])
    except (ValueError, RecursionError):
        return None
    return got if isinstance(got, dict) else None


def _list(value, limit=10):
    if isinstance(value, str):
        value = [value]
    return [str(x).strip()[:160] for x in (value or []) if str(x).strip()][:limit]


_TIME_HEAD = re.compile(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[-—:：|]?\s*(.+)$")


def _timeline(value, limit=12):
    """模型偶尔把时间线写成字符串或换字段名；能救就救，救不了就丢。"""
    if isinstance(value, str):
        value = [value]
    out = []
    for row in (value or [])[:limit]:
        if isinstance(row, dict):
            t = str(row.get("t") or row.get("time") or row.get("timestamp") or "").strip()[:12]
            what = str(row.get("what") or row.get("desc") or row.get("content")
                       or row.get("event") or "").strip()
        else:
            s = str(row or "").strip()
            m = _TIME_HEAD.match(s)
            t, what = (m.group(1), m.group(2)) if m else ("", s)
        if what:
            out.append({"t": t, "what": what[:200]})
    return out


async def understand(meta, sub, video_url, llm=None):
    """看一遍视频。llm(messages, ...) 可注入，默认走 llm_router.call_video。"""
    url = str(video_url or "").strip()
    if not url:
        return {"ok": False, "reason": "没有可读取的视频地址"}
    meta = meta or {}
    subtitle = ""
    if sub and sub.get("ok"):
        subtitle = str(sub.get("text") or "").strip()[:MAX_SUBTITLE_CHARS]
    known = ("标题：%s\nUP主：%s\n时长：%d 分 %d 秒\n简介：%s" % (
        str(meta.get("title") or "?")[:120], str(meta.get("owner") or "?")[:40],
        int(meta.get("duration") or 0) // 60, int(meta.get("duration") or 0) % 60,
        str(meta.get("desc") or "")[:300]))
    if subtitle:
        known += "\n字幕（只作辅助，以画面/音轨为准）：\n" + subtitle
    _fps, _est_frames = plan_video_fps(meta.get("duration"))
    print("[视频理解] 时长≈%ss fps=%s 预计帧≈%d 预计token≈%d（预算%d）"
          % (int(meta.get("duration") or 0), _fps, _est_frames,
             int(_est_frames * VIDEO_TOKENS_PER_FRAME), VIDEO_TOKEN_BUDGET), flush=True)
    content = [
        {"type": "text", "text": known + "\n\n下面是视频文件，请完整观看后按 JSON 回答。"},
        {"type": "video_url", "video_url": {"url": url}, "fps": _fps},
    ]
    messages = [{"role": "system", "content": _VISION_SYSTEM},
                {"role": "user", "content": content}]
    model_name = DEFAULT_MODEL_NOTE
    try:
        if llm is None:
            import llm_router
            try:
                model_name = llm_router.get_video_model().model
            except Exception:
                pass
            raw = await llm_router.call_video(messages, max_tokens=VIDEO_MAX_OUTPUT_TOKENS, timeout=300)
        else:
            raw = llm(messages, max_tokens=VIDEO_MAX_OUTPUT_TOKENS, timeout=300)
            if hasattr(raw, "__await__"):
                raw = await raw
    except Exception as exc:
        return {"ok": False, "reason": "调用失败:" + type(exc).__name__}
    got = _extract_json(raw)
    if not got:
        return {"ok": False, "reason": "返回不是 JSON", "raw": str(raw or "")[:300]}
    overview = str(got.get("overview") or "").strip()
    timeline = _timeline(got.get("timeline"))
    if not overview and not timeline:
        return {"ok": False, "reason": "内容为空"}
    return {"ok": True, "reason": "", "model": model_name,
            "overview": overview[:1200], "timeline": timeline,
            "key_points": _list(got.get("key_points"), 12),
            "uncertainties": _list(got.get("uncertainties"), 10),
            "emotional_beats": _list(got.get("emotional_beats"), 8)}


def to_prompt(result, budget=1600):
    """给「她自己的反应」那段提示词用的观察材料。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    parts = ["【你完整看过的视频 · 画面与音轨观察】"]
    if result.get("overview"):
        parts.append("概述：" + str(result["overview"])[:500])
    for row in (result.get("timeline") or [])[:8]:
        parts.append("- %s %s" % (row.get("t") or "?", str(row.get("what") or "")[:160]))
    if result.get("key_points"):
        parts.append("要点：" + "；".join(result["key_points"][:6]))
    if result.get("emotional_beats"):
        parts.append("情绪/气氛：" + "；".join(result["emotional_beats"][:4]))
    if result.get("uncertainties"):
        parts.append("没看清/没把握：" + "；".join(result["uncertainties"][:4]))
    parts.append("这是观察材料：可以接画面和声音里真实发生的事；"
                 "没看清的不要补，推测要说得像推测。")
    return "\n".join(parts)[:budget]


def summary_line(result):
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    bits = [str(result.get("overview") or "")[:200]]
    for row in (result.get("timeline") or [])[:3]:
        bits.append("%s %s" % (row.get("t") or "?", str(row.get("what") or "")[:80]))
    return "｜".join(x for x in bits if x)[:400]
