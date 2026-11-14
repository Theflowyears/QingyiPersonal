"""千问 Managed Agent 客户端。

用途：把生图 / 语音这类活交给云端智能体做——它自带 bailian_image_gen、bailian_tts 等 MCP 工具。

流程（按官方 REST）：
  1. POST /sessions                     建会话（锁定 Agent 最新版本）
  2. POST /sessions/{id}/events         下发一条 message
  3. GET  /sessions/{id}/events         轮询事件，收助手消息与产物 URL

产物 URL 可能出现在：
  - 助手消息里的 markdown 图片 ![](https://...png)
  - mcp_call_output 的 data 里
所以直接对事件全文做 URL 扫描最稳。
"""
import json
import os
import re
import time

import httpx

_DEFAULT_BASE = "https://maas.qianwenaiapi.com/api/v1/agentstudio"

AGENT_ENABLED = os.getenv("QWEN_AGENT_ENABLED", "1").strip() == "1"
AGENT_ID = os.getenv("QWEN_AGENT_ID", "").strip()
AGENT_BASE = (os.getenv("QWEN_AGENT_BASE", "") or _DEFAULT_BASE).strip().rstrip("/")
AGENT_KEY = (
    os.getenv("QWEN_AGENT_API_KEY", "").strip()
    or os.getenv("LLM_TEXT_API_KEY", "").strip()
)
AGENT_TIMEOUT = float(os.getenv("QWEN_AGENT_TIMEOUT", "180"))
POLL_EVERY = float(os.getenv("QWEN_AGENT_POLL", "3"))
SESS_TTL = float(os.getenv("QWEN_AGENT_SESSION_TTL", "0"))  # >0 时复用会话

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
_AUD_EXT = (".wav", ".mp3", ".m4a", ".ogg", ".flac")
_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def _runtime_cfg():
    """后台面板保存的覆盖配置（改完即时生效）。"""
    import json
    try:
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return ((json.load(f) or {}).get("agent") or {})
    except Exception:
        pass
    return {}


def _agent_id():
    return str(_runtime_cfg().get("agent_id") or AGENT_ID).strip()


def _agent_key():
    return str(_runtime_cfg().get("api_key") or AGENT_KEY).strip()


def _agent_base():
    return str(_runtime_cfg().get("base") or AGENT_BASE).strip().rstrip("/")


def configured():
    return bool(AGENT_ENABLED and _agent_id() and _agent_key())


def status():
    """给面板显示的状态。"""
    return {
        "enabled": AGENT_ENABLED,
        "agent_id": _agent_id(),
        "base": _agent_base(),
        "has_key": bool(_agent_key()),
        "ready": configured(),
    }


# 尾巴上常见杂字符：反斜杠、右括号、引号、句号、中文标点
_URL_TAIL = "".join(chr(c) for c in (92, 47, 41, 93, 125, 34, 39, 96, 44, 46, 59, 12290, 65289, 12305, 12299))


def _clean_url(u):
    """去掉尾巴杂字符。模型输出常带反斜杠，会让 OSS 签名失效。"""
    t = str(u or "").strip()
    while t and t[-1] in _URL_TAIL:
        t = t[:-1]
    return t

def _extract_urls(obj, bucket):
    """递归扫出图片/音频 URL。"""
    if isinstance(obj, str):
        for u in _URL_RE.findall(obj):
            u = _clean_url(u)
            low = u.lower().split("?")[0]
            if low.endswith(_IMG_EXT):
                if u not in bucket["images"]:
                    bucket["images"].append(u)
            elif low.endswith(_AUD_EXT):
                if u not in bucket["audio"]:
                    bucket["audio"].append(u)
        return
    if isinstance(obj, list):
        for x in obj:
            _extract_urls(x, bucket)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _extract_urls(v, bucket)


async def run(prompt, timeout=None, title="QQ Bot Task"):
    """驱动一次智能体回合。返回结构化结果。"""
    if not configured():
        return {"ok": False, "kind": "config", "error": "Agent 未配置"}

    prompt = str(prompt or "").strip()
    if not prompt:
        return {"ok": False, "kind": "config", "error": "没有内容"}

    limit = float(timeout or AGENT_TIMEOUT)
    headers = {
        "Authorization": "Bearer " + _agent_key(),
        "Content-Type": "application/json",
    }
    t0 = time.time()
    sid = ""

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            # 1) 建会话
            r = await c.post(
                _agent_base() + "/sessions",
                headers=headers,
                json={"agent": _agent_id(), "title": str(title)[:60]},
            )
            if r.status_code >= 400:
                return {"ok": False, "kind": "auth" if r.status_code in (401, 403) else "api",
                        "error": "HTTP %s %s" % (r.status_code, (r.text or "")[:160])}
            sid = str((r.json() or {}).get("id") or "")
            if not sid:
                return {"ok": False, "kind": "api", "error": "建会话没返回 id"}

            # 2) 下发消息
            payload = {
                "input": [{
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": prompt}],
                }]
            }
            r2 = await c.post(_agent_base() + "/sessions/" + sid + "/events",
                              headers=headers, json=payload)
            if r2.status_code >= 400:
                return {"ok": False, "kind": "api",
                        "error": "下发失败 HTTP %s %s" % (r2.status_code, (r2.text or "")[:160])}

            # 3) 轮询事件
            seen = set()
            texts = []
            bucket = {"images": [], "audio": []}
            idle_rounds = 0
            while time.time() - t0 < limit:
                await _sleep(POLL_EVERY)
                try:
                    g = await c.get(AGENT_BASE + "/sessions/" + sid + "/events",
                                    headers=headers, params={"limit": 100})
                except Exception:
                    continue
                if g.status_code >= 400:
                    continue
                try:
                    items = (g.json() or {}).get("data") or []
                except Exception:
                    continue
                fresh = 0
                for ev in items:
                    eid = str(ev.get("id") or "")
                    if eid in seen:
                        continue
                    seen.add(eid)
                    fresh += 1
                    if ev.get("type") == "message" and ev.get("role") == "assistant":
                        cont = ev.get("content") or []
                        chunk = " ".join(
                            str(x.get("text") or "") for x in cont if isinstance(x, dict)
                        ).strip()
                        if chunk:
                            texts.append(chunk)
                    _extract_urls(ev, bucket)

                if fresh == 0:
                    idle_rounds += 1
                else:
                    idle_rounds = 0

                # 拿到产物就收工
                if bucket["images"] or bucket["audio"]:
                    if idle_rounds >= 1:
                        break
                elif texts and idle_rounds >= 2:
                    break

        text = "\n".join(texts).strip()
        secs = round(time.time() - t0, 1)
        ok = bool(bucket["images"] or bucket["audio"] or text)
        print("[Agent] %s 用时 %.1fs 图 %d 音频 %d 文本 %d 字"
              % (sid[:18], secs, len(bucket["images"]), len(bucket["audio"]), len(text)), flush=True)
        return {
            "ok": ok,
            "kind": "" if ok else "empty",
            "error": "" if ok else "智能体没有产出",
            "session": sid,
            "text": text,
            "images": bucket["images"],
            "audio": bucket["audio"],
            "seconds": secs,
        }
    except Exception as e:
        print("[Agent] 异常 " + type(e).__name__ + ": " + str(e)[:100], flush=True)
        return {"ok": False, "kind": "network", "error": type(e).__name__}


async def _sleep(sec):
    import asyncio
    await asyncio.sleep(sec)


async def download(url, dest_dir, max_bytes=24 * 1024 * 1024):
    """下载产物到本地，返回 (path, size)。"""
    url = _clean_url(url)
    import hashlib
    from datetime import datetime

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass

    last = ""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code >= 400:
                last = "HTTP %s" % r.status_code
                print("[Agent] 下载 HTTP %s" % r.status_code, flush=True)
                break
            raw = r.content or b""
            if not raw:
                last = "内容为空"
                break
            if len(raw) > max_bytes:
                last = "过大 %d" % len(raw)
                break
            low = url.lower().split("?")[0]
            ext = ".png"
            for e in _IMG_EXT + _AUD_EXT:
                if low.endswith(e):
                    ext = e
                    break
            digest = hashlib.md5(raw).hexdigest()[:10]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(dest_dir, "ag_%s_%s%s" % (stamp, digest, ext))
            with open(path, "wb") as f:
                f.write(raw)
            return os.path.abspath(path), len(raw)
        except Exception as e:
            last = type(e).__name__ + ":" + str(e)[:80]
            print("[Agent] 下载失败(第%d次) %s" % (attempt + 1, last), flush=True)
            import asyncio as _a
            await _a.sleep(1.0)
    return None, 0
