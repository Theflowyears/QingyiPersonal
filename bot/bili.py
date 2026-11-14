"""刷 B 站：只取元数据，不下视频、不转写、不跑 ASR。

口径是用户定的「B 站走元数据 + 字幕（便宜、浅）」。落地前后各实测过一次：

    x/web-interface/view   不登录就有：标题 / UP主 / 时长 / 发布日 / 简介 / 分P / 播放量
    x/player/wbi/v2        未签名 subtitles=0
                           已签名(WBI mixin) + 匿名 buvid3/buvid4 仍旧 subtitles=0
    x/v2/reply/wbi/main    未签名 code=-403；已签名 + buvid 仍旧 -403

第二行值得写下来，因为它推翻过一个看起来很合理的假设：
我们原先只测了「匿名且未签名」，于是怀疑差的是 WBI 签名和 buvid3 指纹。
2026-11-07 把这两样都补上再测一遍 —— 指纹拿到了、mixin 算出来了、签名请求
code=0，字幕列表依然是空的，评论连 -403 都没解开。
所以卡点不是签名，是**账号登录态**：CC 字幕要带登录 cookie。
这条因此不进产品逻辑，只保留 BILI_COOKIE 这一个开关 —— 宁可她说「看不到字幕」，
也不能让她以为自己看过了。

同日带上登录 cookie 在服务器上复测（2026-11-07 15:4x，只读 GET）：
    x/web-interface/view        code=0（云服务器 IP 匿名时被风控成 -412）
    x/player/v2                 有 AI CC 字幕的视频能取到正文（实测 62 字 / 6000 字）
    x/v2/reply/main             code=0，返回 20 条评论
所以三层的差别只在 cookie；配了就用，没配就如实说拿不到。
评论是别人写的，只进「她当时的反应」，不进知识库笔记 —— 她的记忆里留
「刷到时看到几条」就够了，把陌生人的原话存成长期知识是另一类污染。

所以这里分成两层，而且**如实报告第二层拿不到**：
  · meta 层真的能用，默认开；
  · 字幕/评论层只在配了 BILI_COOKIE 时才试，没配就返回 need_login，
    绝不返回一个空列表假装「这个视频没有字幕」—— 空列表和拿不到是两件事，
    把它们混起来，她就会说出「这视频居然没字幕」这种没依据的话。

BILI_MODE=off 时整条链路一步都不走（上线默认）。
"""
import hashlib
import json
import os
import re
import time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYER = "https://api.bilibili.com/x/player/v2"
API_REPLY = "https://api.bilibili.com/x/v2/reply"
API_REPLY_MAIN = "https://api.bilibili.com/x/v2/reply/main"
API_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

MODE = os.getenv("BILI_MODE", "off").strip().lower()      # off | meta
# 看画面要先把可访问的视频文件交给视频模型；超过上限就不假装看过。
VIDEO_MAX_BYTES = int(os.getenv("BILI_VIDEO_MAX_BYTES", str(300 * 1024 * 1024)))
VIDEO_MAX_SECONDS = int(os.getenv("BILI_VISION_MAX_SECONDS", "3600"))
VIDEO_PUBLIC_TTL = int(os.getenv("BILI_VIDEO_PUBLIC_TTL", "900"))
VISION_MAX_PER_DAY = int(os.getenv("BILI_VISION_MAX_PER_DAY", "20"))


def _vision_day_file():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory",
                        "bili_vision_day.json")


def vision_budget():
    """今天还能不能真看视频。读一次小文件，不挡后台。"""
    try:
        with open(_vision_day_file(), encoding="utf-8") as f:
            got = json.load(f) or {}
    except Exception:
        got = {}
    day = time.strftime("%Y-%m-%d")
    used = int(got.get("count") or 0) if got.get("day") == day else 0
    return max(0, VISION_MAX_PER_DAY - used), used, day


def vision_charge(day=None):
    day = day or time.strftime("%Y-%m-%d")
    path = _vision_day_file()
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f) or {}
    except Exception:
        got = {}
    used = int(got.get("count") or 0) if got.get("day") == day else 0
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"day": day, "count": used + 1}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass
TIMEOUT = float(os.getenv("BILI_TIMEOUT_SECONDS", "12"))
CACHE_TTL = float(os.getenv("BILI_CACHE_TTL_SECONDS", "86400"))
MAX_NOTES = int(os.getenv("BILI_MAX_NOTES", "80"))
DESC_MAX = int(os.getenv("BILI_DESC_MAX", "240"))
SUBTITLE_MIN_CHARS = int(os.getenv("BILI_SUBTITLE_MIN_CHARS", "60"))
COMMENT_MAX = int(os.getenv("BILI_COMMENT_MAX", "6"))
COMMENT_LEN = int(os.getenv("BILI_COMMENT_LEN", "60"))
COMMENT_MIN_CHARS = int(os.getenv("BILI_COMMENT_MIN_CHARS", "4"))

COOKIE = os.getenv("BILI_COOKIE", "").strip()

# 同一个 BV 今天已经吃过一次就别再吃：她刷到重发的那条不该变成两条记忆
_cache = {}
_notes = {"sig": None, "items": None}


def enabled():
    return MODE in ("meta", "on", "1", "true")


def has_login_cookie():
    return bool(COOKIE)


def _key(ref):
    return hashlib.sha1(str(ref or "").encode("utf-8", "ignore")).hexdigest()[:12]


async def resolve_short(url, client=None):
    """b23.tv 短链 → 真地址里的视频号。

    只跟随我们白名单上那个域名，而且只跳一次拿 Location：
    跟随任意 URL 等于让群里任何人指定我们下一步敲哪扇门。
    """
    import media_link as _ml
    surl = str(url or "").strip()
    if not _ml.is_allowed(surl):
        return ""
    own = client is None
    try:
        if own:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT,
                                         follow_redirects=False) as c:
                r = await c.get(surl, headers=_headers())
                loc = str(r.headers.get("location") or "")
                if not loc and r.status_code < 400:
                    loc = str(r.url)
                return _ml.from_final_url(loc)
        r = await client.get(surl)
        return _ml.from_final_url(str(r.headers.get("location") or "")
                                 or str(r.url))
    except Exception:
        return ""


def _headers():
    h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
         "Accept": "application/json, text/plain, */*"}
    if COOKIE:
        h["Cookie"] = COOKIE
    return h


def _cached(key):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]
    return None


def _put(key, val):
    _cache[key] = (time.time(), val)
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda x: _cache[x][0])[:80]:
            _cache.pop(k, None)


# ── 元数据 ────────────────────────────────────────
def _ok_view(data):
    aid = data.get("aid")
    cid = data.get("cid")
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    pages = data.get("pages") or []
    parts = []
    for p in pages[:12]:
        name = str(p.get("part") or "").strip()
        if name:
            parts.append(name[:40])
    return {
        "ok": True,
        "bvid": str(data.get("bvid") or ""),
        "aid": int(aid or 0),
        "cid": int(cid or 0),
        "title": str(data.get("title") or "").strip()[:120],
        "owner": str(owner.get("name") or "").strip()[:40],
        "duration": int(data.get("duration") or 0),
        "pubdate": int(data.get("pubdate") or 0),
        "desc": re.sub(r"\s+", " ", str(data.get("desc") or "")).strip()[:DESC_MAX],
        "view": int(stat.get("view") or 0),
        "like": int(stat.get("like") or 0),
        "pages": len(pages),
        "parts": parts,
    }


async def fetch_playinfo(meta, client=None):
    """取可下载的视频文件地址（优先 durl 直出 mp4，含音轨）。

    播放页不是视频文件；DASH 是音视频分开的分片，没有 ffmpeg 合流时
    不能交给模型当完整视频 —— 返回明确原因，不冒充。
    """
    if not enabled() or not meta or not meta.get("ok"):
        return {"ok": False, "reason": "没有可用元数据"}
    if not has_login_cookie():
        return {"ok": False, "reason": "需要登录 cookie"}
    params = {"bvid": meta.get("bvid") or "", "cid": meta.get("cid") or 0,
              "fnval": 1, "fnver": 0, "fourk": 1, "qn": 64}
    if not params["bvid"] or not params["cid"]:
        return {"ok": False, "reason": "缺少 bvid/cid"}
    key = "play:" + _key(params["bvid"])
    hit = _cached(key)
    if hit:
        return hit
    body = None
    try:
        if client is None:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as c:
                r = await c.get(API_PLAYURL, params=params)
                body = r.json()
        else:
            r = await client.get(API_PLAYURL, params=params)
            body = r.json()
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__}
    if int((body or {}).get("code") or 0) != 0:
        return {"ok": False, "reason": "B站说 code=%s" % (body or {}).get("code")}
    data = (body or {}).get("data") or {}
    dur = int(data.get("timelength") or 0) // 1000
    durl = data.get("durl") or []
    if durl:
        first = durl[0] or {}
        url = str(first.get("url") or "")
        if url:
            res = {"ok": True, "url": url, "size": int(first.get("size") or 0),
                   "seconds": dur, "quality": int(data.get("quality") or 0),
                   "reason": "", "kind": "durl"}
            _put(key, res)
            return res
    return {"ok": False, "reason": "只有 DASH 分片流，需要合流"}


async def download_media(url, referer="https://www.bilibili.com/",
                         max_bytes=None, timeout=None):
    """把视频流落到本地临时文件，超过上限就中止并清理。"""
    import uuid
    limit = int(max_bytes or VIDEO_MAX_BYTES)
    root = os.path.join(os.getenv("DATA_DIR", "./data"), "media_cache")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, uuid.uuid4().hex + ".mp4")
    t0 = time.time()
    total = 0
    try:
        import httpx
        # B站 CDN 对不带 Range 的整段 GET 会回 567（风控页）。
        # 用 bytes=0- 走标准分段读取，同时带上登录 cookie。
        headers = {"User-Agent": UA, "Referer": referer, "Range": "bytes=0-"}
        cookie = str(os.getenv("BILI_COOKIE") or COOKIE or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        async with httpx.AsyncClient(timeout=timeout or 120, follow_redirects=True,
                                     headers=headers) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    return {"ok": False, "reason": "HTTP %s" % r.status_code}
                ctype = str(r.headers.get("content-type") or "").split(";")[0].lower()
                if ctype and not (ctype.startswith("video/") or ctype in
                                  ("application/octet-stream", "binary/octet-stream")):
                    return {"ok": False, "reason": "不是视频：" + ctype}
                with open(path, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        total += len(chunk)
                        if total > limit:
                            raise ValueError("too_large")
                        f.write(chunk)
    except ValueError:
        try:
            os.remove(path)
        except OSError:
            pass
        return {"ok": False, "reason": "视频超过 %d MB" % (limit // (1024 * 1024))}
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        return {"ok": False, "reason": type(e).__name__}
    return {"ok": True, "path": path, "bytes": total,
            "seconds": round(time.time() - t0, 1)}


def _cleanup_media(path):
    try:
        os.remove(str(path))
    except OSError:
        pass


async def watch_video(meta, sub, ttl=None):
    """看完一条 B 站视频：取流 → 临时公开 → Omni 看画面/音轨 → 清理。"""
    if not meta or not meta.get("ok"):
        return {"ok": False, "reason": "没有可用元数据", "watch_depth": "仅简介"}
    duration = int(meta.get("duration") or 0)
    if duration > VIDEO_MAX_SECONDS:
        return {"ok": False, "reason": "视频太长（>%d 分钟），只看字幕"
                % (VIDEO_MAX_SECONDS // 60), "watch_depth": "仅字幕"}
    try:
        import media_public
        import bili_vision
    except Exception as e:
        return {"ok": False, "reason": "模块不可用:" + type(e).__name__,
                "watch_depth": "仅字幕"}
    if not media_public.base_url():
        return {"ok": False, "reason": "没有配置公网媒体地址", "watch_depth": "仅字幕"}
    play = await fetch_playinfo(meta)
    if not play.get("ok"):
        print("[B站] 取视频流失败：%s" % play.get("reason"), flush=True)
        return {"ok": False, "reason": str(play.get("reason") or "取流失败"),
                "watch_depth": "仅字幕"}
    dl = await download_media(play.get("url"))
    if not dl.get("ok"):
        print("[B站] 下载视频失败：%s" % dl.get("reason"), flush=True)
        return {"ok": False, "reason": str(dl.get("reason") or "下载失败"),
                "watch_depth": "仅字幕"}
    pub = media_public.publish(dl.get("path"), ttl=ttl or VIDEO_PUBLIC_TTL)
    if not pub.get("ok"):
        _cleanup_media(dl.get("path"))
        return {"ok": False, "reason": str(pub.get("reason") or "发布失败"),
                "watch_depth": "仅字幕"}
    try:
        print("[B站] 交给视频模型看画面（%d MB，TTL %ds）"
              % (int(dl.get("bytes") or 0) // (1024 * 1024),
                 int(pub.get("expires") or 0) - int(time.time())), flush=True)
        vision = await bili_vision.understand(meta, sub, pub.get("url"))
    finally:
        _cleanup_media(dl.get("path"))
        try:
            media_public.cleanup()
        except Exception:
            pass
    vision["watch_depth"] = "视频+字幕" if (sub and sub.get("ok")) else "视频画面"
    print("[B站] 视频理解 %s（%s）" % (
        "成功" if vision.get("ok") else "失败：" + str(vision.get("reason")),
        str(vision.get("model") or "?")), flush=True)
    return vision


async def fetch_meta(ref, client=None):
    """按 BV/av 取元数据。ref 必须已经过 media_link 白名单。"""
    if not enabled():
        return {"ok": False, "reason": "BILI_MODE=off"}
    sref = str(ref or "").strip()
    if not sref:
        return {"ok": False, "reason": "没有视频号"}
    key = "meta:" + _key(sref)
    hit = _cached(key)
    if hit:
        return hit
    params = {"bvid": sref} if sref.upper().startswith("BV") else {"aid": sref[2:]}
    own = client is None
    res = {"ok": False, "reason": "没取到"}
    try:
        if own:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as c:
                r = await c.get(API_VIEW, params=params)
                body = r.json()
        else:
            r = await client.get(API_VIEW, params=params)
            body = r.json()
        if int(body.get("code") or 0) != 0:
            res = {"ok": False,
                   "reason": "B站说 code=%s %s" % (body.get("code"),
                                                   str(body.get("message") or "")[:24])}
        else:
            res = _ok_view(body.get("data") or {})
    except Exception as e:
        res = {"ok": False, "reason": type(e).__name__}
    if res.get("ok"):
        _put(key, res)
    return res


def _feed_item(it):
    """热门列表里的一条 → 和 fetch_meta 同形的候选。

    缺 bvid 或标题的直接丢：宁可少一条候选，也不要拿空标题去挑。
    """
    if not isinstance(it, dict):
        return None
    bvid = str(it.get("bvid") or "").strip()
    title = str(it.get("title") or "").strip()
    if not bvid or not title:
        return None
    owner = it.get("owner") or {}
    stat = it.get("stat") or {}
    return {
        "ok": True,
        "bvid": bvid,
        "aid": int(it.get("aid") or 0),
        "cid": int(it.get("cid") or 0),
        "title": title[:120],
        "owner": str(owner.get("name") or "").strip()[:40],
        "duration": int(it.get("duration") or 0),
        "pubdate": int(it.get("pubdate") or 0),
        "desc": re.sub(r"\s+", " ", str(it.get("desc") or "")).strip()[:DESC_MAX],
        "view": int(stat.get("view") or 0),
        "like": int(stat.get("like") or 0),
        "pages": int(it.get("videos") or 1),
        "parts": [],
        "category": str(it.get("tname") or "").strip()[:20],
        "url": "https://www.bilibili.com/video/" + bvid,
    }


async def fetch_popular(limit=20, client=None):
    """首页热门：她「自己刷」时从哪儿拿片子。

    实测（2026-11-07）匿名可用，code=0，不需要 cookie。
    这条只负责给候选，挑哪条、一天刷几条由 bili_auto 决定。
    """
    if not enabled():
        return {"ok": False, "reason": "BILI_MODE=off", "items": []}
    size = max(1, min(int(limit or 20), 50))
    try:
        if client is None:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as c:
                r = await c.get(API_POPULAR, params={"ps": size, "pn": 1})
                body = r.json()
        else:
            r = await client.get(API_POPULAR, params={"ps": size, "pn": 1})
            body = r.json()
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__, "items": []}
    if int(body.get("code") or 0) != 0:
        return {"ok": False,
                "reason": "B站说 code=%s %s" % (body.get("code"),
                                                str(body.get("message") or "")[:24]),
                "items": []}
    items = []
    for raw in ((body.get("data") or {}).get("list") or []):
        got = _feed_item(raw)
        if got:
            items.append(got)
    return {"ok": bool(items), "reason": "" if items else "热门列表为空",
            "items": items}


# ── 字幕：拿不到就说拿不到 ────────────────────────
async def fetch_subtitles(meta, client=None):
    """只有挂了 cookie 才真的去取。

    实测不登录时这里永远返回空列表，而「空列表」会被下游读成
    「这个视频没有字幕」。所以没 cookie 直接给 need_login，不猜。
    """
    if not has_login_cookie():
        return {"ok": False, "reason": "需要登录 cookie", "text": ""}
    if not meta or not meta.get("aid"):
        return {"ok": False, "reason": "没有 aid", "text": ""}
    key = "sub:" + _key(meta.get("bvid") or meta.get("aid"))
    hit = _cached(key)
    if hit:
        return hit
    res = {"ok": False, "reason": "没有字幕", "text": ""}

    async def _json(getter):
        r = await getter
        return r.json()

    try:
        if client is None:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT,
                                         follow_redirects=True,
                                         headers=_headers()) as c:
                body = await _json(c.get(API_PLAYER, params={
                    "aid": meta["aid"], "cid": meta.get("cid")}))
                res = await _parse_subtitles(body, c)
        else:
            body = await _json(client.get(API_PLAYER, params={
                "aid": meta["aid"], "cid": meta.get("cid")}))
            res = await _parse_subtitles(body, client)
    except Exception as e:
        res = {"ok": False, "reason": type(e).__name__, "text": ""}
    _put(key, res)
    return res


async def _parse_subtitles(player_body, client):
    subs = (((player_body or {}).get("data") or {}).get("subtitle") or {})
    lst = subs.get("subtitles") or []
    if not lst:
        return {"ok": False, "reason": "这条视频没有 CC 字幕", "text": ""}
    url = str(lst[0].get("subtitle_url") or "")
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        return {"ok": False, "reason": "字幕没有可读地址", "text": ""}
    r = await client.get(url)
    sb = r.json()
    lines = [str(x.get("content") or "").strip()
             for x in ((sb or {}).get("body") or [])]
    text = " ".join([x for x in lines if x])
    if len(text) < SUBTITLE_MIN_CHARS:
        # 十几分钟的词表拼起来就一句话，那不是字幕是打轴失败
        return {"ok": False, "reason": "字幕太短，不可信", "text": ""}
    return {"ok": True, "reason": "", "text": text[:6000],
            "lan": str(lst[0].get("lan_doc") or "")[:20]}


# ── 评论区：只当「听说的」，不进笔记原文 ──────────
def _clean_comment(text):
    t = re.sub(r"https?://\S+", " ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:COMMENT_LEN]


def _reply_text(reply):
    """评论正文。

    2026-11-07 实测 x/v2/reply/main 把文字放在 content.message，
    而老的 x/v2/reply 放在 content.content —— 只认一种的话，
    20 条评论会被解析成「评论区是空的」。
    """
    content = (reply or {}).get("content") or {}
    return str(content.get("message") or content.get("content") or "")


def _ok_comments(body, n):
    if int((body or {}).get("code") or 0) != 0:
        return {"ok": False, "items": [],
                "reason": "B站说 code=%s" % (body or {}).get("code")}
    data = (body or {}).get("data") or {}
    out = []
    for it in ((data.get("top_replies") or []) + (data.get("replies") or [])):
        line = _clean_comment(_reply_text(it))
        if len(line) >= COMMENT_MIN_CHARS:
            out.append(line)
        if len(out) >= n:
            break
    if not out:
        # 「没人评论」和「没取到」是两件事，别混过去
        return {"ok": False, "items": [], "reason": "评论区是空的"}
    return {"ok": True, "items": out, "reason": ""}


async def fetch_comments(aid, limit=None, client=None):
    """她刷到这个视频时，评论区能看见的那几条。

    不登录时这里必须是「需要登录 cookie」，不能是空列表 —— 2026-11-07 复测：
    匿名 -403，带 cookie code=0 且返回 20 条。把 -403 说成「没人评论」，
    她就会说出「这视频居然没人讨论」这种没依据的话。
    """
    if not has_login_cookie():
        return {"ok": False, "reason": "需要登录 cookie", "items": []}
    try:
        oid = int(aid or 0)
    except (TypeError, ValueError):
        oid = 0
    if oid <= 0:
        return {"ok": False, "reason": "没有 aid", "items": []}
    key = "reply:" + _key(oid)
    hit = _cached(key)
    if hit:
        return hit
    n = max(1, min(int(limit or COMMENT_MAX), 20))
    params = {"type": 1, "oid": oid, "mode": 3, "next": 0}
    res = {"ok": False, "reason": "没取到", "items": []}
    try:
        if client is None:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as c:
                r = await c.get(API_REPLY_MAIN, params=params)
                res = _ok_comments(r.json(), n)
        else:
            r = await client.get(API_REPLY_MAIN, params=params)
            res = _ok_comments(r.json(), n)
    except Exception as e:
        res = {"ok": False, "reason": type(e).__name__, "items": []}
    _put(key, res)
    return res


# ── 看过的笔记：只存结构化那条，不存视频本身 ───────
def _notes_path():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory",
                        "bili_notes.json")


def _load_notes():
    path = _notes_path()
    try:
        st = os.stat(path)
        sig = (round(st.st_mtime, 3), st.st_size)
    except Exception:
        sig = None
    if sig is None:
        if _notes["items"] is None:
            _notes["items"] = []
        return _notes["items"]
    if _notes["sig"] == sig and _notes["items"] is not None:
        return _notes["items"]
    items = []
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, list):
            items = [x for x in got if isinstance(x, dict)]
    except Exception:
        items = []
    _notes.update({"sig": None, "items": items})
    return items


def _save_notes(items):
    path = _notes_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        _notes["items"] = items
        _notes["sig"] = None
        return True
    except Exception as e:
        print("[B站] 笔记写盘失败 " + str(e)[:60], flush=True)
        return False


def save_note(meta, sub=None, reaction="", scene="", *, session="", run_id=None,
              comments=None, vision=None):
    """一条「她看过这个视频」的笔记。

    存的是她看到的东西和当时的想法，不是视频文件也不是原始字幕全文 ——
    那些占地方、有版权，而且她自己也不会逐字记住台词。
    评论只留条数：陌生人的原话不该变成她的长期记忆。
    """
    if not meta or not meta.get("ok"):
        return None
    items = list(_load_notes())
    bvid = str(meta.get("bvid") or "")
    now = time.time()
    rec = {
        "key": bvid or _key(json.dumps(meta, ensure_ascii=False)[:80]),
        "bvid": bvid,
        "title": str(meta.get("title") or "")[:120],
        "owner": str(meta.get("owner") or "")[:40],
        "duration": int(meta.get("duration") or 0),
        "pubdate": int(meta.get("pubdate") or 0),
        "view": int(meta.get("view") or 0),
        "like": int(meta.get("like") or 0),
        "pages": int(meta.get("pages") or 0),
        "desc": str(meta.get("desc") or "")[:DESC_MAX],
        "has_subtitle": bool(sub and sub.get("ok")),
        "subtitle_reason": "" if (sub and sub.get("ok")) else str(
            (sub or {}).get("reason") or "没取字幕")[:24],
        "reaction": str(reaction or "")[:200],
        "comments_seen": len((comments or {}).get("items") or [])
                         if (comments or {}).get("ok") else 0,
        "scene": str(scene or "")[:12],
        # 观看深度要如实写：只读简介、只读字幕、真看了画面，不能混成一句「看完了」
        "watch_depth": str((vision or {}).get("watch_depth")
                           or ("仅字幕" if (sub and sub.get("ok")) else "仅简介"))[:12],
        "vision_ok": bool((vision or {}).get("ok")),
        "vision_reason": str((vision or {}).get("reason") or "")[:80],
        "vision_model": str((vision or {}).get("model") or "")[:40],
        "vision_overview": str((vision or {}).get("overview") or "")[:500],
        "vision_timeline": (vision or {}).get("timeline") or [],
        "vision_key_points": (vision or {}).get("key_points") or [],
        "vision_uncertainties": (vision or {}).get("uncertainties") or [],
        "evidence": {"video": str(meta.get("bvid") or "")[:24],
                     "subtitle": bool(sub and sub.get("ok"))},
        "ts": now,
        "last_seen": now,
    }
    out = [x for x in items if str(x.get("key")) != str(rec["key"])]
    out.append(rec)
    if len(out) > MAX_NOTES:
        # 满了淘汰最久没再看到的，而不是最新加进来的那条
        out.sort(key=lambda x: -float(x.get("last_seen") or 0))
        out = out[:MAX_NOTES]
    saved = _save_notes(out)
    if saved:
        try:
            import self_model
            rec['event_id'] = self_model.record_tool_result(
                "bili", bvid or rec["key"], True, session=session,
                visibility='public' if scene == '自主' else 'group' if scene == 'group' else 'private',
                run_id=run_id)
            # Persist the exact execution event, not a fabricated lookup ID.
            if not _save_notes(out):
                print('[认知] 视频事件引用写盘失败', flush=True)
        except Exception as exc:
            print("[认知] 视频结果记账失败 " + type(exc).__name__, flush=True)
    return rec


def note_for(bvid):
    for x in _load_notes():
        if str(x.get("bvid") or "") == str(bvid or ""):
            return x
    return None


def latest_notes(limit=3):
    items = list(_load_notes())
    items.sort(key=lambda x: -float(x.get("ts") or 0))
    return items[:max(0, int(limit))]


def seen_before(ref):
    """这条是不是已经吃过了 —— 群里重复转发很常见。

    BV 号大小写是有意义的（BV1GJ411x7h7 和 BV1GJ411X7H7 是两个视频），
    所以这里逐字比，别手滑 upper() 成「认不出但撞车」的那种错。
    """
    b = str(ref or "").strip()
    if not b.startswith("BV"):
        return False
    return any(str(x.get("bvid") or "") == b for x in _load_notes())


def render_for_prompt(rec):
    """把笔记压成提示词里的一小段。"""
    if not isinstance(rec, dict):
        return ""
    dur = int(rec.get("duration") or 0)
    line = "- 《%s》 UP主「%s」，%d分%02d秒" % (
        rec.get("title") or "?", rec.get("owner") or "?",
        dur // 60, dur % 60)
    view = int(rec.get("view") or 0)
    if view:
        line += "，播放 %s" % ("%.1f万" % (view / 10000.0) if view >= 10000
                              else str(view))
    if rec.get("reaction"):
        line += "｜她当时的想法：" + str(rec["reaction"])[:120]
    # 观看深度要带进记忆：只读了字幕的人不该说得像亲眼看过画面
    if rec.get("watch_depth"):
        line += "｜观看：" + str(rec["watch_depth"])[:12]
    if rec.get("vision_ok") and rec.get("vision_overview"):
        line += "｜画面：" + str(rec["vision_overview"])[:120]
    elif rec.get("vision_reason"):
        line += "｜画面没看成：" + str(rec["vision_reason"])[:40]
    if not rec.get("has_subtitle"):
        line += "｜（字幕没看到：%s）" % str(rec.get("subtitle_reason") or "?")[:20]
    return line


def stats():
    items = _load_notes()
    return {"mode": MODE, "enabled": enabled(),
            "login_cookie": has_login_cookie(),
            "notes": len(items),
            "with_subtitle": sum(1 for x in items if x.get("has_subtitle")),
            "cache": len(_cache)}
