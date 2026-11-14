"""
NapCat 主动接口辅助：通过已登录的 OneBot WebSocket 取 QQ 图片、执行需要登录态的操作。

QQ CDN 链接（multimedia.nt.qq.com.cn / gchat.qpic.cn 等）外部服务器抓不到，必须让
已登录的 NapCat 用 get_image 去取，返回 base64 再交给视觉模型。
"""
import json
import uuid
import asyncio
from typing import Optional


class OneBotChannel:
    """持有活跃 ws + send_lock，提供请求-响应式调用（靠 echo 匹配回包）。"""

    def __init__(self):
        self.ws = None
        self.lock = None          # 复用 bridge 的 send_lock
        self._pending = {}        # echo -> Future
        self._loop = None

    def bind(self, ws, send_lock=None):
        self.ws = ws
        self.lock = send_lock
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None

    def unbind(self, ws=None):
        if ws is None or ws is self.ws:
            self.ws = None
            # 唤醒所有等待者，避免悬挂
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_result(None)
            self._pending.clear()

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def call(self, action, params=None, timeout=15.0):
        """发送一个 action 并等待同 echo 的回包 dict；失败返回 None。"""
        if self.ws is None:
            return None
        echo = str(uuid.uuid4())
        payload = {"action": action, "params": params or {}, "echo": echo}
        fut = (self._loop or asyncio.get_event_loop()).create_future()
        self._pending[echo] = fut
        try:
            if self.lock:
                async with self.lock:
                    await self.ws.send(json.dumps(payload, ensure_ascii=False))
            else:
                await self.ws.send(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            print("[OneBotChannel.call 失败] %s %s" % (action, e), flush=True)
            return None
        finally:
            self._pending.pop(echo, None)

    def feed(self, msg) -> bool:
        """由接收循环调用：若该帧是某命令的回包，唤醒等待者并返回 True（不当普通事件处理）。"""
        echo = msg.get("echo") if isinstance(msg, dict) else None
        if echo and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(msg)
            return True
        return False


# 全局单例，bridge 连接时 bind，各处直接用
channel = OneBotChannel()


QQ_CDN_HOSTS = ("multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn",
                "p.qpic.cn", "snsimg.qpic.cn", "avatar")


def is_qq_cdn_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in QQ_CDN_HOSTS)


def _looks_like_image_b64(s) -> bool:
    """粗略判断字符串是否是有效图片 base64（长度够、能解码、带常见魔数）。"""
    if not isinstance(s, str):
        return False
    s2 = s.split(",", 1)[-1] if s.startswith("data:") else s
    if len(s2) < 128:
        return False
    try:
        import base64
        raw = base64.b64decode(s2[:len(s2) // 4 * 4], validate=False)
    except Exception:
        return False
    # 常见图片魔数：JPEG FFD8 / PNG 8950 / GIF 4749 / WEBP RIFF....WEBP / BMP 424D
    sigs = (b"\xff\xd8", b"\x89PNG", b"GIF8", b"BM")
    if any(raw.startswith(sg) for sg in sigs):
        return True
    if raw[0:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    return False


def _read_local_file_b64(path):
    """读取本地图片文件转 base64；返回 (b64 或 None, mime)。"""
    try:
        import base64
        from pathlib import Path as _P
        p = _P(path)
        if not p.is_file():
            return None, "image/jpeg"
        raw = p.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            return None, "image/jpeg"
        ext = p.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/jpeg")
        return base64.b64encode(raw).decode("ascii"), mime
    except Exception as e:
        print("[读本地图片失败] %s path=%s" % (e, str(path)[:120]), flush=True)
        return None, "image/jpeg"


async def fetch_image_as_dataurl(url: str, _allow_napcat: bool = True) -> str:
    """把图片标识变成 data:image/...;base64,... 。

    QQ 图片正确取法：事件 image segment 的 file 字段（如 xxx.jpg）传给 NapCat get_image，
    它返回一个本地磁盘路径（QQ 已下载好），直接读该文件转 base64。
    绕开 CDN url 需登录态/会过期的问题。CDN url 仅作最后兜底。
    """
    ref = (url or "").strip()
    if not ref:
        return ""
    if ref.startswith("data:image/"):
        return ref

    raw_b64 = None
    mime = "image/jpeg"

    # 首选：NapCat get_image -> 本地文件路径 -> 读盘
    if _allow_napcat and channel.connected:
        resp = await channel.call("get_image", {"file": ref}, timeout=25.0)
        data = (resp or {}).get("data")
        if isinstance(data, dict):
            b = data.get("base64")
            if b and _looks_like_image_b64(b):
                raw_b64 = b
                mime = data.get("mimetype") or data.get("mime_type") or "image/jpeg"
            else:
                local = data.get("file") or data.get("path") or ""
                if local and not str(local).startswith("http"):
                    raw_b64, mime = _read_local_file_b64(str(local))
                if not raw_b64:
                    fresh_url = data.get("url")
                    if fresh_url and str(fresh_url).startswith("http") and str(fresh_url) != ref:
                        return await fetch_image_as_dataurl(str(fresh_url), _allow_napcat=False)

    # 兜底：机器人自己下 http(s) url
    if not raw_b64 and ref.startswith("http"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(ref)
            if r.status_code < 400 and r.content:
                import base64
                raw_b64 = base64.b64encode(r.content).decode("ascii")
                ct = r.headers.get("content-type", "")
                if ct.startswith("image/"):
                    mime = ct.split(";")[0].strip()
        except Exception as e:
            print("[fetch_image 下载失败] %s ref=%s" % (e, ref[:120]), flush=True)

    if not raw_b64:
        return ""
    if raw_b64.startswith("data:"):
        return raw_b64
    return "data:%s;base64,%s" % (mime, raw_b64)
