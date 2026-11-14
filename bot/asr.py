"""语音识别（ASR）：把语音条转成文字。

用阿里云 fun-asr-flash-8k-realtime，走 WebSocket 实时接口。
为什么要它：群里有人发语音，她原来只能干看着；转成文字后她就能正常接话。
"""
import os
import time

ASR_ENABLED = os.getenv("ASR_ENABLED", "1").strip() == "1"
ASR_MODEL = os.getenv("ASR_MODEL", "fun-asr-flash-8k-realtime").strip()
ASR_WS_URL = os.getenv(
    "ASR_WS_URL",
    "wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/inference",
).strip()
ASR_KEY = (
    os.getenv("ASR_API_KEY", "").strip()
    or os.getenv("IMAGE_GEN_API_KEY", "").strip()
    or os.getenv("TTS_API_KEY", "").strip()
)
ASR_TIMEOUT = float(os.getenv("ASR_TIMEOUT", "60"))

_DONE = {"ready": False}


def _runtime_cfg():
    """面板里保存的 ASR 覆盖配置（改完即时生效）。"""
    try:
        import json
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return ((json.load(f) or {}).get("asr") or {})
    except Exception:
        pass
    return {}


def _model():
    return str(_runtime_cfg().get("model") or ASR_MODEL).strip()


def _key():
    return str(_runtime_cfg().get("api_key") or ASR_KEY).strip()


def _ws_url():
    return str(_runtime_cfg().get("base_url") or ASR_WS_URL).strip()


def _configure():
    """给 dashscope 配好 key 和自定义 WebSocket 端点。"""
    if _DONE["ready"]:
        return True
    try:
        import dashscope
    except Exception as e:
        print("[语音识别] dashscope 未安装: " + str(e)[:60], flush=True)
        return False
    try:
        dashscope.api_key = _key()
        dashscope.base_websocket_api_url = _ws_url()
        _DONE["ready"] = True
        return True
    except Exception as e:
        print("[语音识别] 配置失败 " + type(e).__name__, flush=True)
        return False


def configured():
    return bool(ASR_ENABLED and _key())


def status():
    return {
        "enabled": ASR_ENABLED,
        "model": _model(),
        "url": _ws_url(),
        "has_key": bool(_key()),
        "ready": configured(),
    }


def _resample_wav(src, target_rate=8000):
    """把 wav 转成目标采样率，返回新文件路径（失败返回 None）。

    没有 ffmpeg，就自己算：整倍降采样每隔 N 点取一个，
    非整倍用线性插值。语音识别用足够了。
    """
    import array
    import wave
    try:
        w = wave.open(src, "rb")
        rate = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
        w.close()
    except Exception:
        return None
    if sw != 2:
        return None

    a = array.array("h")
    a.frombytes(raw)
    if ch > 1:
        a = array.array("h", a[::ch])

    if rate == target_rate:
        pass
    elif rate % target_rate == 0:
        a = a[::int(rate // target_rate)]
    else:
        ratio = rate / float(target_rate)
        out_n = int(len(a) / ratio)
        out = array.array("h", bytes(out_n * 2))
        for i in range(out_n):
            pos = i * ratio
            i0 = int(pos)
            if i0 + 1 >= len(a):
                break
            frac = pos - i0
            out[i] = int(a[i0] * (1.0 - frac) + a[i0 + 1] * frac)
        a = out

    dst = src + ".asr.wav"
    try:
        w2 = wave.open(dst, "wb")
        w2.setnchannels(1)
        w2.setsampwidth(2)
        w2.setframerate(target_rate)
        w2.writeframes(a.tobytes())
        w2.close()
        return dst
    except Exception:
        return None


def _sentence_text(sent):
    """把 Recognition 返回的句子结构拼成纯文本。

    它可能是：str / dict（含 text 与 words）/ list（多个句子）。
    不同版本返回结构不一样，这里都兜住。
    """
    def one(item):
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            t = str(item.get("text") or "").strip()
            if t:
                return t
            words = item.get("words") or []
            parts = []
            for w in words:
                if not isinstance(w, dict):
                    continue
                parts.append(str(w.get("text") or ""))
                parts.append(str(w.get("punctuation") or ""))
            return "".join(parts)
        return ""

    if sent is None:
        return ""
    if isinstance(sent, (list, tuple)):
        parts = [one(x).strip() for x in sent]
        return "".join(p for p in parts if p).strip()
    return one(sent).strip()


def transcribe(path, *, session="", visibility="private", run_id=None, evidence_origin=None):
    """同步识别 I/O 收口；异步包装不另记事件，识别原文不进入事件账本。"""
    from toolkit import _record_tool_execution
    result = {"ok": False, "error": "识别未完成"}
    reason = ""
    try:
        result = _transcribe_impl(path)
        if result.get("ok") is False:
            reason = {"语音识别未配置": "not_configured", "音频文件不存在": "missing_audio",
                      "dashscope 不可用": "sdk_unavailable", "配置失败": "configuration_failed",
                      "没听到内容": "empty_transcript"}.get(result.get("error"), "recognition_failed")
    except Exception as exc:
        reason = "exception:" + type(exc).__name__
        result = {"ok": False, "error": type(exc).__name__}
    finally:
        _record_tool_execution("asr", path, result, evidence="text", session=session,
                               visibility=visibility, run_id=run_id,
                               evidence_origin=evidence_origin, reason=reason)
    return result


def _transcribe_impl(path):
    """把本地音频文件转成文字。返回 {ok, text, seconds, error}。

    dashscope 的 Recognition 是同步阻塞调用，必须放到线程里跑，
    否则会卡住整个事件循环（她整轮就不说话了）。
    """
    if not configured():
        return {"ok": False, "error": "语音识别未配置"}
    p = str(path or "").strip()
    if not p or not os.path.isfile(p):
        return {"ok": False, "error": "音频文件不存在"}
    if not _configure():
        return {"ok": False, "error": "dashscope 不可用"}
    # 配置变了要重新配一次
    _DONE["ready"] = False
    if not _configure():
        return {"ok": False, "error": "配置失败"}

    t0 = time.time()
    try:
        from dashscope.audio.asr import Recognition
        rate = 8000 if "8k" in ASR_MODEL else 16000
        # 模型是 8k 的，先重采样，不然识别质量很差
        fixed = _resample_wav(p, rate)
        use = fixed or p
        rec = Recognition(model=ASR_MODEL, format="wav",
                          sample_rate=rate, callback=None)
        result = rec.call(use)
    except Exception as e:
        return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:80],
                "seconds": round(time.time() - t0, 1)}

    secs = round(time.time() - t0, 1)
    try:
        code = getattr(result, "status_code", None)
        if code != 200:
            msg = str(getattr(result, "message", "") or "")[:80]
            print("[语音识别] 失败 %s %s" % (code, msg), flush=True)
            return {"ok": False, "error": "HTTP %s %s" % (code, msg), "seconds": secs}
        text = _sentence_text(result.get_sentence())
    except Exception as e:
        return {"ok": False, "error": "解析结果失败 " + type(e).__name__, "seconds": secs}

    if not text:
        return {"ok": False, "error": "没听到内容", "seconds": secs}
    print("[语音识别] %s -> %s" % (os.path.basename(p), text[:50]), flush=True)
    return {"ok": True, "text": text, "seconds": secs}


async def transcribe_async(path, *, session="", visibility="private", run_id=None,
                           evidence_origin=None):
    """to_thread 传播 ContextVar，验证请求不会在线程池中变成真实证据。"""
    import asyncio
    return await asyncio.to_thread(transcribe, path, session=session, visibility=visibility,
                                   run_id=run_id, evidence_origin=evidence_origin)