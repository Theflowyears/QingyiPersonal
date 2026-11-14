"""她自己上网时「打开网页读正文」这一层。

搜索接口只给摘要；摘要够用来判断值不值得说，但不够让她真的知道页面上写了什么。
这里补的是读正文，同时把「搜索结果页给的任意 URL」挡在私网之外：

  · 只允许 http/https，拒绝 IP 字面量里的私网/回环/链路本地地址；
  · 域名每次请求前解析一遍，解析到私网就不去；
  · 跳转手动跟，最多 3 次，每一跳重新过同一道门；
  · 只读 text/html / xhtml / text/plain，最多 WEB_READ_MAX_BYTES 字节；
  · 正文去掉脚本、样式、导航，截到 WEB_READ_MAX_CHARS 字。

调用方是后台的好奇度 worker，不在 2.5 秒回复预算里。
"""
import asyncio
import html as _html
import ipaddress
import os
import re
import socket
from urllib.parse import urljoin, urlsplit

MAX_BYTES = int(os.getenv("WEB_READ_MAX_BYTES", "600000"))
MAX_CHARS = int(os.getenv("WEB_READ_MAX_CHARS", "2500"))
TIMEOUT = float(os.getenv("WEB_READ_TIMEOUT", "12"))
MIN_CHARS = int(os.getenv("WEB_READ_MIN_CHARS", "20"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_BLOCKED_HOSTS = ("localhost",)
_BLOCKED_SUFFIX = (".local", ".internal", ".localhost", ".home.arpa")
_REMOVE = re.compile(
    r"<(script|style|noscript|svg|template|nav|header|footer|aside)\b[^>]*>"
    r".*?</\1>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|br|li|h[1-6]|tr|section|article|table|ul|ol|blockquote)\b[^>]*>",
    re.I)
_TAG = re.compile(r"<[^>]+>", re.S)


def _headers():
    return {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,"
                                       "text/plain;q=0.9,*/*;q=0.1",
            "Accept-Language": "zh-CN,zh;q=0.9"}


def safe_url(url):
    """语法层白名单：只放行公网 http/https。解析私网的事在 _download 里做。"""
    try:
        p = urlsplit(str(url or "").strip())
    except Exception:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    if p.username or p.password:
        return ""
    host = p.hostname.strip().lower()
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIX):
        return ""
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return ""
    except ValueError:
        pass
    return p.geturl()


def _public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _decode(body, content_type):
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    encs = [m.group(1)] if m else []
    encs += ["utf-8", "gb18030"]
    for enc in encs:
        try:
            return bytes(body).decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return bytes(body).decode("utf-8", "ignore")


def extract(markup):
    """返回 (标题, 正文)。只做结构化抽取，不执行任何东西。"""
    raw = str(markup or "")
    mt = _TITLE.search(raw)
    title = _clean(mt.group(1)) if mt else ""
    body = _COMMENT.sub(" ", raw)
    body = _REMOVE.sub(" ", body)
    body = _BLOCK_TAG.sub("\n", body)
    body = _TAG.sub(" ", body)
    body = _html.unescape(body)
    lines = []
    for line in body.splitlines():
        line = " ".join(line.split())
        if line:
            lines.append(line)
    text = "\n".join(lines).strip()[:MAX_CHARS]
    return title[:120], text


def _clean(fragment):
    return " ".join(_html.unescape(_TAG.sub(" ", str(fragment or ""))).split())


def _is_html(content_type):
    ct = str(content_type or "").lower()
    return any(x in ct for x in ("text/html", "application/xhtml", "text/plain"))


async def _download(url, client=None):
    """按跳转读回字节；每一跳都重新做地址检查。"""
    import httpx
    own = client is None
    c = (httpx.AsyncClient(timeout=TIMEOUT, headers=_headers(),
                           follow_redirects=False) if own else client)
    try:
        current = url
        for _ in range(4):
            p = urlsplit(current)
            if not p.hostname or not await asyncio.to_thread(_public_host, p.hostname):
                return {"ok": False, "reason": "私网或解析失败的地址"}
            async with c.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = str(r.headers.get("location") or "").strip()
                    nxt = safe_url(urljoin(current, loc)) if loc else ""
                    if not nxt:
                        return {"ok": False, "reason": "跳转到了不安全的地址"}
                    current = nxt
                    continue
                if r.status_code >= 400:
                    return {"ok": False, "reason": "HTTP%d" % r.status_code}
                ct = str(r.headers.get("content-type") or "").lower()
                if not _is_html(ct):
                    return {"ok": False,
                            "reason": "不是网页（%s）" % (ct or "未知类型")}
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= MAX_BYTES:
                        del body[MAX_BYTES:]
                        break
                return {"ok": True, "body": bytes(body), "content_type": ct,
                        "final_url": current}
        return {"ok": False, "reason": "跳转次数太多"}
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__}
    finally:
        if own:
            await c.aclose()


async def fetch_page(url, client=None, *, session="", visibility="private", run_id=None,
                     evidence_origin=None):
    """一次页面读取只在正文抽取完成后提交一次结果，包括地址门禁失败。"""
    from toolkit import _record_tool_execution
    result = {"ok": False, "reason": "读取未完成"}
    reason = ""
    try:
        result = await _fetch_page_impl(url, client)
        # 只记固定门名，不把 URL / 页面正文写入 reason。
        if result.get("ok") is False:
            reason = {"不安全的地址": "unsafe_url", "正文太短": "short_body",
                      "不是网页": "unsupported_content"}.get(result.get("reason"), "download_failed")
    except Exception as exc:
        reason = "exception:" + type(exc).__name__
        result = {"ok": False, "reason": type(exc).__name__, "url": str(url or "")}
    finally:
        _record_tool_execution("web_read", url, result, evidence="text", session=session,
                               visibility=visibility, run_id=run_id,
                               evidence_origin=evidence_origin, reason=reason)
    return result


async def _fetch_page_impl(url, client=None):
    """打开一个搜索结果页。拿不到就如实返回原因，不返回空正文。"""
    clean = safe_url(url)
    if not clean:
        return {"ok": False, "reason": "不安全的地址", "url": str(url or "")}
    got = await _download(clean, client)
    if not got.get("ok"):
        return {"ok": False, "reason": got.get("reason") or "没打开", "url": clean}
    if not _is_html(got.get("content_type")):
        return {"ok": False, "reason": "不是网页", "url": clean}
    title, text = extract(_decode(got.get("body") or b"", got.get("content_type")))
    if len(text) < MIN_CHARS:
        return {"ok": False, "reason": "正文太短", "url": clean, "title": title}
    return {"ok": True, "url": got.get("final_url") or clean, "title": title,
            "text": text, "chars": len(text)}