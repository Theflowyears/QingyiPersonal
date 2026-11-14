"""语音合成：把文字变成语音发到 QQ，支持指定声线。

用途：让她"唱"一首歌（本质是带语调朗读歌词），或指定某种声线说话。

API 返回 wav 的签名 URL，落盘后交给 NapCat 发 record 段。
"""
import hashlib
import os
import time
from datetime import datetime

import httpx

TTS_ENABLED = os.getenv("TTS_ENABLED", "1").strip() == "1"
TTS_URL = os.getenv(
    "TTS_URL",
    "https://cn-beijing.maas.qianwenaiapi.com/api/v1/services/audio/tts/SpeechSynthesizer",
).strip()
TTS_MODEL = os.getenv("TTS_MODEL", "qwen-audio-3.1-tts-next").strip()
# 输出格式：wav 兼容性最好（QQ 语音条直认）；mp3 体积仅 1/12 但偶有兼容问题
TTS_FORMAT = (os.getenv("TTS_FORMAT", "wav") or "wav").strip().lower()
TTS_KEY = (
    os.getenv("TTS_API_KEY", "").strip()
    or os.getenv("LLM_TEXT_API_KEY", "").strip()
)
TTS_TIMEOUT = float(os.getenv("TTS_TIMEOUT", "300"))
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "300"))

_DEFAULT_DATA = os.getenv("DATA_DIR", "./data")
TTS_DIR = os.getenv("TTS_DIR", os.path.join(_DEFAULT_DATA, "voice"))

# ── DashScope 直连（说话 / 唱歌两条独立通道）──
# 百炼签发的 key 形如 sk-ws-...，和 LLM_TEXT 那把 sk-w 不通用，所以单独一份。
TTS_KEY_BASE_URL = os.getenv(
    "TTS_KEY_BASE_URL",
    "https://cn-beijing.maas.qianwenaiapi.com/api/v1",
).strip()
# 唱歌走流式 WebSocket，端点不一样
TTS_WS_URL = os.getenv(
    "TTS_WS_URL",
    "wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/inference",
).strip()

# 说话：qwen3-tts-flash（同步 HTTP，最简单最稳）
SPEAK_MODEL = os.getenv("TTS_SPEAK_MODEL", "qwen3-tts-flash").strip()
SPEAK_VOICE = os.getenv("TTS_SPEAK_VOICE", "Cherry").strip()
# 唱歌：qwen-audio-3.0-tts-flash（流式 WebSocket）
SING_MODEL = os.getenv("TTS_SING_MODEL", "qwen-audio-3.0-tts-flash").strip()
SING_VOICE = os.getenv("TTS_SING_VOICE", "longanfengyue").strip()
# 唱歌第二备选：HTTP 同步接口，比流式稳
SING_NEXT_MODEL = os.getenv("TTS_SING_NEXT_MODEL", "qwen-audio-3.1-tts-next").strip()

# 成曲通道（中转站 suno-v5，按次计费）。默认关闭，绝不自作主张调用。
SING_SUNO_ENABLED = os.getenv("SING_SUNO_ENABLED", "0").strip() == "1"
SING_SUNO_MODEL = os.getenv("SING_SUNO_MODEL", "suno-v5").strip()


# 声线预设：key -> 给 TTS 的声线描述
VOICE_PRESETS = {
    "amiya": "用明日方舟阿米娅那种温柔清亮、有点稚气的少女声线",
    "whale": "用懒洋洋、带点傲娇的少女声线，像刚睡醒",
    "sweet": "用甜软的少女声线",
    "cold": "用清冷平静的少女声线",
    "loli": "用软萌娇小的萝莉声线",
    "mature": "用成熟温和的成年女性声线",
}
DEFAULT_VOICE = os.getenv("TTS_DEFAULT_VOICE", "whale").strip().lower()


def _ensure_dir():
    try:
        os.makedirs(TTS_DIR, exist_ok=True)
    except Exception:
        pass


def voice_desc(voice):
    """把声线别名转成描述；没匹配就当自由描述。"""
    v = str(voice or "").strip()
    if not v:
        return VOICE_PRESETS.get(DEFAULT_VOICE, VOICE_PRESETS["whale"])
    low = v.lower()
    for key, desc in VOICE_PRESETS.items():
        if key in low:
            return desc
    return v if v else VOICE_PRESETS["whale"]


def _runtime_cfg():
    """后台面板保存的覆盖配置优先。"""
    import json
    try:
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return (json.load(f) or {}).get("tts") or {}
    except Exception:
        pass
    return {}

# ---- 生成通道：builtin（直连模型）/ agent（云端智能体） ----
def _provider(role, default="builtin"):
    """读后台面板保存的通道设置，改完即时生效。"""
    import json
    try:
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f) or {}
            p = str(((cfg.get("providers") or {}).get(role)) or "").strip().lower()
            if p in ("builtin", "agent"):
                return p
    except Exception:
        pass
    return str(os.getenv(role.upper() + "_PROVIDER", default)).strip().lower() or default


_ensure_dir()


def _save_bytes(raw, ext):
    """把音频字节落盘，返回绝对路径。"""
    _ensure_dir()
    digest = hashlib.md5(raw).hexdigest()[:10]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(TTS_DIR, "v_%s_%s.%s" % (stamp, digest, ext))
    with open(path, "wb") as f:
        f.write(raw)
    return os.path.abspath(path)


async def _speak_dashscope(text):
    """说话通道：qwen3-tts-flash（同步 HTTP）。

    dashscope 的同步接口是阻塞的，这里放到线程池跑，别卡住整个事件循环。
    """
    import asyncio as _aio
    import dashscope
    from dashscope.audio.qwen_tts import SpeechSynthesizer

    key = TTS_KEY
    if not key:
        return {"ok": False, "kind": "config", "error": "语音密钥未配置"}

    def _go():
        dashscope.base_http_api_url = TTS_KEY_BASE_URL
        r = SpeechSynthesizer.call(
            model=SPEAK_MODEL,
            api_key=key,
            text=text[:TTS_MAX_CHARS],
            voice=SPEAK_VOICE,
        )
        return r

    try:
        r = await _aio.get_running_loop().run_in_executor(None, _go)
    except Exception as e:
        print("[语音] DashScope 说话异常 " + type(e).__name__ + ": " + str(e)[:120], flush=True)
        return {"ok": False, "kind": "network", "error": type(e).__name__}

    # 标准返回：output.audio.url / output.audio.data
    try:
        out = (r.output if hasattr(r, "output") else (r or {}).get("output")) or {}
        audio = out.get("audio") if isinstance(out, dict) else None
    except Exception:
        audio = None
    if not audio:
        print("[语音] 说话无音频：%s" % (str(r)[:160]), flush=True)
        return {"ok": False, "kind": "api", "error": "没有音频"}

    url = audio.get("url") or ""
    b64 = audio.get("data") or ""
    t0 = time.time()
    try:
        if url:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                ar = await c.get(url)
            ar.raise_for_status()
            raw = ar.content
        elif b64:
            import base64 as _b64
            raw = _b64.b64decode(b64)
        else:
            return {"ok": False, "kind": "api", "error": "音频无地址"}
        if not raw:
            return {"ok": False, "kind": "download", "error": "音频为空"}
        path = _save_bytes(raw, TTS_FORMAT)
        print("[语音] 说话完成 %s (%d bytes, %.1fs)" % (path, len(raw), time.time() - t0), flush=True)
        return {"ok": True, "path": path, "url": url, "seconds": round(time.time() - t0, 1),
                "provider": "qwen3-tts-flash"}
    except Exception as e:
        print("[语音] 说话下载异常 " + type(e).__name__, flush=True)
        return {"ok": False, "kind": "download", "error": type(e).__name__}


async def _sing_dashscope(text):
    """唱歌通道：qwen-audio-3.0-tts-flash（流式 WebSocket）。

    SpeechSynthesizer 内部起流，返回 mp3 字节。同样放线程池里跑。
    """
    import asyncio as _aio
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer

    key = TTS_KEY
    if not key:
        return {"ok": False, "kind": "config", "error": "语音密钥未配置"}

    def _go():
        dashscope.api_key = key
        dashscope.base_websocket_api_url = TTS_WS_URL
        synth_ = SpeechSynthesizer(model=SING_MODEL, voice=SING_VOICE)
        return synth_.call(text[:TTS_MAX_CHARS])

    try:
        audio = await _aio.get_running_loop().run_in_executor(None, _go)
    except Exception as e:
        print("[语音] DashScope 唱歌异常 " + type(e).__name__ + ": " + str(e)[:120], flush=True)
        return {"ok": False, "kind": "network", "error": type(e).__name__}

    if not audio:
        return {"ok": False, "kind": "api", "error": "没有音频"}
    try:
        path = _save_bytes(audio, "mp3")
        print("[语音] 唱歌完成 %s (%d bytes)" % (path, len(audio)), flush=True)
        return {"ok": True, "path": path, "url": "", "seconds": 0,
                "provider": "qwen-audio-3.0-tts-flash"}
    except Exception as e:
        print("[语音] 唱歌落盘异常 " + type(e).__name__, flush=True)
        return {"ok": False, "kind": "download", "error": type(e).__name__}


async def _sing_next(text):
    """唱歌第二备选（异步包装）。失败返回结构体，不抛异常。"""
    import asyncio as _aio
    key = TTS_KEY
    if not key:
        return {"ok": False, "kind": "config", "error": "语音密钥未配置"}

    def _go():
        body = {
            "model": SING_NEXT_MODEL,
            "input": {"text_prompt": text[:TTS_MAX_CHARS], "format": "wav"},
        }
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        with httpx.Client(timeout=TTS_TIMEOUT, follow_redirects=True) as c:
            r = c.post(TTS_URL, headers=h, json=body)
            r.raise_for_status()
            url = (((r.json() or {}).get("output") or {}).get("audio") or {}).get("url") or ""
            if not url:
                raise RuntimeError("没有音频地址")
            ar = c.get(url, timeout=60)
            ar.raise_for_status()
            return ar.content

    try:
        raw = await _aio.get_running_loop().run_in_executor(None, _go)
    except Exception as e:
        print("[语音] TTS-Next 唱歌异常 " + type(e).__name__ + ": " + str(e)[:120], flush=True)
        return {"ok": False, "kind": "network", "error": type(e).__name__}

    if not raw:
        return {"ok": False, "kind": "api", "error": "音频为空"}
    try:
        path = _save_bytes(raw, "wav")
        print("[语音] 唱歌完成（Next）%s (%d bytes)" % (path, len(raw)), flush=True)
        return {"ok": True, "path": path, "url": "", "seconds": 0,
                "provider": SING_NEXT_MODEL}
    except Exception as e:
        print("[语音] TTS-Next 落盘异常 " + type(e).__name__, flush=True)
        return {"ok": False, "kind": "download", "error": type(e).__name__}


async def synth(text, voice="", sing=False, *, session="", visibility="private", run_id=None,
                evidence_origin=None):
    """一次合成（含内部回退）只记一次结果，成功以非空本地音频为证。"""
    from toolkit import _record_tool_execution
    result = {"ok": False, "kind": "network", "error": "合成未完成"}
    reason = ""
    try:
        result = await _synth_impl(text, voice=voice, sing=sing)
    except Exception as exc:
        reason = "exception:" + type(exc).__name__
        result = {"ok": False, "kind": "network", "error": type(exc).__name__}
    finally:
        _record_tool_execution("tts", text, result, evidence="artifact", session=session,
                               visibility=visibility, run_id=run_id,
                               evidence_origin=evidence_origin, reason=reason)
    return result


async def _synth_impl(text, voice="", sing=False):
    """合成语音，返回 {ok, path, url, seconds} 或 {ok:False, kind, error}。"""
    text = str(text or "").strip()
    if not TTS_ENABLED:
        return {"ok": False, "kind": "disabled", "error": "语音功能未开启"}
    if not text:
        return {"ok": False, "kind": "config", "error": "没有内容"}
    _cfg = _runtime_cfg()
    _model = str(_cfg.get("model") or TTS_MODEL)
    _url = str(_cfg.get("base_url") or TTS_URL)
    _key = str(_cfg.get("api_key") or TTS_KEY)

    # ── 第一级：DashScope 专用通道（说话 qwen3-tts-flash / 唱歌 qwen-audio-3.0）──
    # 这两个是百炼直连，声线固定（Cherry / longanfengyue），效果比中转站稳定。
    # 失败再往下走旧链路（云端智能体 → 直连模型），不会因为一条挂掉就没声音。
    if TTS_KEY:
        if sing:
            # 唱歌：① 3.0 流式（快）→ ② 3.1-next 同步（稳）→ ③ Suno（默认关）
            _r1 = await _sing_dashscope(text)
            if _r1.get("ok"):
                return _r1
            print("[语音] 唱歌 3.0 未成（%s），换 3.1-next" % str(_r1.get("error"))[:40], flush=True)
            _r2 = await _sing_next(text)
            if _r2.get("ok"):
                return _r2
            print("[语音] 唱歌 3.1-next 也未成（%s）" % str(_r2.get("error"))[:40], flush=True)
            if SING_SUNO_ENABLED:
                return {"ok": False, "kind": "suno", "error": "suno 未接入"}
            return {"ok": False, "kind": "api", "error": "三条唱歌通道都没成"}
        _r1 = await _speak_dashscope(text)
        if _r1.get("ok"):
            return _r1
        print("[语音] 说话未成（%s），降级" % str(_r1.get("error"))[:40], flush=True)

    # ── 第二级：agent 优先，失败自动回退直连模型 ──
    # 唱歌走 TTSNEXT（直连模型，音准更好）；说话交给云端智能体
    _route = _provider("tts_sing") if sing else _provider("tts")
    if _route == "agent":
        try:
            import qwen_agent
            # 提示词越短，智能体内部推理越少。实测：长句 25.6s → 直给 16.8s（快 35%）。
            if sing:
                _p = "TTS 唱：" + text[:160] + "。只给音频。"
            else:
                _p = "TTS 朗读：" + text[:200] + "。只给音频。"
            _ar = await qwen_agent.run(_p, timeout=170, title="tts")
            if _ar.get("ok") and _ar.get("audio"):
                _path, _size = await qwen_agent.download(_ar["audio"][0], TTS_DIR)
                if _path:
                    print("[语音] 走 Agent 通道 %s (%d bytes, %.1fs)"
                          % (_path, _size, _ar.get("seconds") or 0), flush=True)
                    return {"ok": True, "path": _path, "url": _ar["audio"][0],
                            "seconds": _ar.get("seconds") or 0, "provider": "agent"}
            print("[语音] Agent 无产出，回退直连: " + str(_ar.get("error"))[:60], flush=True)
        except Exception as _ae:
            print("[语音] Agent 异常，回退直连 " + type(_ae).__name__, flush=True)

    if not _key:
        return {"ok": False, "kind": "config", "error": "语音密钥未配置"}
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]

    desc = voice_desc(voice)
    if sing:
        prompt = (desc + "，唱两句就收，八秒以内，直接开口唱，不要前奏尾奏：" + text)
    else:
        prompt = desc + "，用自然口语说：" + text

    payload = {
        "model": _model,
        # wav 是 QQ 语音条最稳的格式，体积大一些但不会出现发不出去的情况。
        "input": {"text_prompt": prompt, "format": TTS_FORMAT},
    }
    headers = {
        "Authorization": "Bearer " + _key,
        "Content-Type": "application/json",
    }

    def _mk_prompt(body):
        if sing:
            return desc + "，唱两句就收，八秒以内，直接开口唱，不要前奏尾奏：" + body
        return desc + "，用自然口语说：" + body

    def _variants(body):
        """内容审核偶发误判时的降级候选：先原句，再截短。"""
        out = [body]
        # 取前两句
        one = re.split(r"[\n，,。；;]+", body)
        one = [x.strip() for x in one if x.strip()]
        if len(one) > 1:
            out.append(" ".join(one[:1]))
        if len(one) > 2:
            out.append(" ".join(one[:2]))
        # 兜底：只说一句安全的话
        out.append("啦啦啦，我随便哼两句。" if sing else "唔，我在呢。")
        seen, uniq = set(), []
        for v in out:
            k = v.strip()
            if k and k not in seen:
                seen.add(k)
                uniq.append(k)
        return uniq

    t0 = time.time()
    print("[语音] 合成中 sing=%s voice=%s len=%d" % (sing, voice or DEFAULT_VOICE, len(text)), flush=True)
    r = None
    _tried = []
    for _body in _variants(text):
        _tried.append(_body[:20])
        payload["input"]["text_prompt"] = _mk_prompt(_body)
        try:
            async with httpx.AsyncClient(timeout=TTS_TIMEOUT, follow_redirects=True) as c:
                _resp = await c.post(_url, headers=headers, json=payload)
        except Exception as e:
            print("[语音] 请求异常 " + type(e).__name__ + ": " + str(e)[:100], flush=True)
            return {"ok": False, "kind": "network", "error": type(e).__name__}
        if _resp.status_code < 400:
            r = _resp
            break
        # 只有内容审核拦了才降级重试；其它错误直接返回
        _code = ""
        try:
            _code = str((_resp.json() or {}).get("code") or "")
        except Exception:
            pass
        print("[语音] HTTP %s %s code=%s" % (_resp.status_code, (_resp.text or "")[:160], _code), flush=True)
        if "DataInspection" not in _code and "inappropriate" not in (_resp.text or "").lower():
            return {"ok": False, "kind": "api", "error": "HTTP %s" % _resp.status_code}
        print("[语音] 审核误判，降级重试：" + _body[:20], flush=True)

    if r is None:
        print("[语音] 降级后仍失败，试过：" + " / ".join(_tried), flush=True)
        return {"ok": False, "kind": "api", "error": "内容审核拦截"}

    try:
        data = r.json()
        url = ((data.get("output") or {}).get("audio") or {}).get("url") or ""
    except Exception:
        url = ""
    if not url:
        print("[语音] 返回里没有音频 url", flush=True)
        return {"ok": False, "kind": "api", "error": "没有音频"}

    _ensure_dir()
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            ar = await c.get(url)
        if ar.status_code >= 400 or not ar.content:
            return {"ok": False, "kind": "download", "error": "音频下载失败"}
        digest = hashlib.md5(ar.content).hexdigest()[:10]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(TTS_DIR, "v_%s_%s.%s" % (stamp, digest, TTS_FORMAT))
        with open(path, "wb") as f:
            f.write(ar.content)
        secs = round(time.time() - t0, 1)
        print("[语音] 完成 %s (%d bytes, %.1fs)" % (path, len(ar.content), secs), flush=True)
        return {"ok": True, "path": os.path.abspath(path), "url": url, "seconds": secs}
    except Exception as e:
        print("[语音] 下载异常 " + type(e).__name__ + ": " + str(e)[:100], flush=True)
        return {"ok": False, "kind": "download", "error": type(e).__name__}


# 唱歌意图：必须是要她"唱"，普通说话不触发
import re


_SING_PATTERNS = [
    r"唱一?首", r"唱一?个", r"来一?首", r"唱给", r"唱歌",
    r"唱一?段", r"唱两?句", r"唱一?曲", r"来一?段",
    r"/唱", r"唱吧", r"哼一?首", r"唱支", r"唱[《「“\"]",
    r"唱[一二三几]?遍", r"再唱", r"唱个",
]

# ── 否定 / 评价 / 复述：这些是在说「唱歌」这件事，不是在点歌 ──
# 例：「不是唱歌」「好像不会唱歌」「唱给姐姐的」「你刚才唱得不错」
_SING_NEGATED = re.compile(
    r"(不是|不算|并不|没有|没在|别|不要|不用|不想|不爱|不会|不能|没法"
    r"|像不|不太)\s*(在)?\s*唱")
_SING_COMMENT = re.compile(
    r"唱\s*(得|完|过|了|成)"                       # 唱得/唱完/唱过/唱了
    r"|唱给[^，。！？,.!?]{0,10}的"                  # 唱给姐姐的
    r"|(去|在|刚)s*唱歌"                            # 去唱歌了（陈述自己）
    r"|唱歌了"                                      # 已经唱过了
    r"|唱[的]?[^，。！？,.!?]{0,6}(好听|难听|跑调|破音|还行|不错|可以)"  # 评价
    # 「唱歌」这件事本身在讨论，不是在点歌：
    #   「唱歌是不是很难」「唱歌难不难」「唱歌好学吗」「唱歌有什么诀窍」
    r"|(唱歌|唱)[^，。！？,.!?]{0,8}(是不是|难不难|好不好学|有什么用|什么诀窍|怎么练|怎么学)"
    r"|(会不会|是不是|难不难|好不好)\s*(唱歌|唱)"
)
_SING_PAST = re.compile(r"(刚才|刚刚|之前|上次|上一?首|前一首|你唱)")
_SING_ASK = re.compile(r"(会不会|能不能|可以|会|想听|要听?)唱")


def _is_singing_request(t):
    """判断这是不是一条「让她唱歌」的请求，而不是在聊唱歌这件事。"""
    if not t:
        return False
    # ① 否定 → 不是点歌
    if _SING_NEGATED.search(t):
        return False
    # ② 评价/复述 → 不是点歌
    if _SING_COMMENT.search(t):
        return False
    # ③ 纯提问："你会唱歌吗""唱歌好听么""能不能唱一首"
    if _SING_ASK.search(t) and not re.search(
            r"唱\s*(一|首|个|段|句|支|曲|遍)|唱[《「“\"]|来一?[首段]|点歌", t):
        return False
    # ④ 提到过去某次但不是新请求（"你刚才唱的那首""唱过的"）
    if _SING_PAST.search(t) and not re.search(r"(再|再来|现在|接下来|下一?首)唱", t):
        # 除非明确带点歌量词 + 祈使
        if not re.search(r"(唱|来)\s*(一|首|个|段|句|支|曲)", t):
            return False
    return True


def detect_sing(text):
    """判断是不是要她唱歌。返回 (要不要唱, 歌名或空串)。"""
    import re
    t = str(text or "").strip()
    if not t:
        return False, ""
    if not _is_singing_request(t):
        return False, ""

    hit = False
    for pat in _SING_PATTERNS:
        if re.search(pat, t):
            hit = True
            break
    if not hit:
        return False, ""

    # ① 书名号/引号里的，一定是歌名
    m = re.search(r"[《「“\"]([^》」”\"]{1,40})[》」”\"]", t)
    if m:
        return True, m.group(1).strip()

    # ② 去掉声线描述和"一/两/几"量词后再看有没有真歌名
    cand = t
    for junk in ("用阿米娅的声音", "用阿米亚的声音", "阿米娅的声音", "阿米亚的声音",
                 "用你的声音", "换个声线", "换声线"):
        cand = cand.replace(junk, "")
    m = re.search(r"唱(?:一?首|一?个|一?段|两?句|一?曲|支|给)?\s*([^\s，。！？!?]{2,40})", cand)
    if m:
        name = m.group(1).strip()
        if re.fullmatch(r"[一两几]?[首歌句段支曲个遍]", name):
            return True, ""
        # 「唱给我听」这类，人称开头不是歌名
        if name[:1] in "我你他她它们":
            return True, ""
        junk_prefix = ("首歌", "个歌", "句歌", "段歌", "支歌")
        if name.startswith(junk_prefix):
            return True, ""
        return True, name

    # 命中了唱歌意图但没听出歌名：交给即兴
    return True, ""


def detect_voice(text):
    """从话里挑声线。返回声线别名或空串。"""
    t = str(text or "").lower()
    keys = {
        "amiya": ("阿米娅", "阿米亚", "amiya"),
        "whale": ("清漪", "我", "你自己的"),
        "sweet": ("甜", "可爱"),
        "cold": ("冷", "高冷"),
        "loli": ("萝莉", "软"),
        "mature": ("御姐", "成熟"),
    }
    for key, words in keys.items():
        if any(w.lower() in t for w in words):
            return key
    return ""
