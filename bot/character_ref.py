"""画指定角色前，先找参考图并提取形象特征。

为什么需要：
只给模型一句「画阿米娅」，它每次都按训练数据里模糊的印象画，
特征经常错（发色、兽耳、服装都对不上）。有了参考图提取的特征描述，
生成时就是「照着这个人画」，而不是「凭印象画」。

流程：
  1. 从请求里认出角色名
  2. 必应图片搜「<角色> 立绘」，拿图源
  3. 下载到本地（也留住，方便回看）
  4. 视觉模型看这张图，输出紧凑的特征描述
  5. 缓存起来，下次同角色直接复用
"""
import json
import os
import re
import time
import urllib.parse

import httpx

_DATA = os.getenv("DATA_DIR", "./data")
_REF_DIR = os.path.join(_DATA, "char_refs")
_CACHE_FILE = os.path.join(_DATA, "memory", "char_refs.json")
CACHE_TTL = float(os.getenv("CHAR_REF_TTL", str(30 * 86400)))

# 常见角色 → 出处，搜得更准
KNOWN = {
    "阿米娅": "明日方舟 Amiya Arknights",
    "提弗洛斯": "明日方舟 终末地 提弗洛斯",
    "小提丰": "明日方舟 终末地 提弗洛斯",
    "提丰": "明日方舟 终末地 提弗洛斯",
    "铃兰": "明日方舟 Suzuran Arknights",
    "莫斯提马": "明日方舟 Mostima Arknights",
    "安洁莉娜": "明日方舟 Angelina Arknights",
    "阿乙": "",
    "凯尔希": "明日方舟 Kal'tsit Arknights",
    "W": "明日方舟 W Arknights",
    "斯卡蒂": "明日方舟 Skadi Arknights",
    "能天使": "明日方舟 Exusiai Arknights",
    "德克萨斯": "明日方舟 Texas Arknights",
    "陈": "明日方舟 Ch'en Arknights",
    "史尔特尔": "明日方舟 Surtr Arknights",
    "泥岩": "明日方舟 Mudrock Arknights",
    "原神": "Genshin Impact",
    "可莉": "原神 Klee",
    "胡桃": "原神 Hu Tao",
    "雷电将军": "原神 Raiden Shogun",
    "初音": "初音未来 Hatsune Miku",
    "初音未来": "初音未来 Hatsune Miku",
    "雷姆": "Re:Zero 雷姆 Rem",
    "艾米莉亚": "Re:Zero 爱蜜莉雅",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_MURL_PATS = (
    r'murl&quot;:&quot;(.*?)&quot;',
    r'"murl":"(.*?)"',
    r'\\"murl\\":\\"(.*?)\\"',
    r'murl%22%3A%22(.*?)%22',
)


def _all_urls(body):
    """把页面里能看到的图源全捞出来，多模式合并去重。

    只用一个模式会漏——必应页面同时有转义和未转义两种写法，
    实测单一模式只出 2 条，全模式合并能出 24 条。
    """
    import html as _html
    out = []
    for pat in _MURL_PATS:
        for m in re.findall(pat, body or ""):
            u = _html.unescape(m).replace("\\/", "/").strip()
            if u.startswith("http") and u not in out:
                out.append(u)
    return out


_cache = {"data": None, "ts": 0.0}


def _load_cache(force=False):
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < 10:
        return _cache["data"]
    data = {}
    try:
        if os.path.isfile(_CACHE_FILE):
            with open(_CACHE_FILE, encoding="utf-8") as f:
                got = json.load(f) or {}
            if isinstance(got, dict):
                data = got
    except Exception:
        pass
    _cache["data"] = data
    _cache["ts"] = now
    return data


def _save_cache(data):
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _CACHE_FILE)
        _cache["data"] = data
        _cache["ts"] = time.time()
    except Exception as e:
        print("[角色参考] 缓存写入失败 " + str(e)[:60], flush=True)


def extract_characters(text):
    """从一句话里认出角色名。"""
    t = str(text or "")
    found = []
    for name in sorted(KNOWN, key=len, reverse=True):
        if name in t and name not in found:
            # 「小提丰」和「提丰」重名，长的优先，短的别重复加
            if any(name in f for f in found):
                continue
            found.append(name)
    return found[:2]


_PRTS_API = "https://prts.wiki/api.php"
_PRTS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://prts.wiki/",
}

# 明日方舟相关关键词：命中就直接走 PRTS
ARK_MARKS = ("明日方舟", "arknights", "终末地", "endfield", "罗德岛",
             "干员", "泰拉", "prts", "阿米娅", "提弗洛斯", "小提丰", "铃兰",
             "莫斯提马", "凯尔希", "斯卡蒂", "安洁莉娜")


def is_arknights(text):
    """这句话是不是在说明日方舟。"""
    t = str(text or "").lower()
    return any(m.lower() in t for m in ARK_MARKS)


def _pick_portrait(names, skin_kw=""):
    """从一堆文件名里挑最合适的立绘。

    指定皮肤时（skin_kw 非空），名字里带该词的最优先——
    PRTS 的皮肤立绘一般叫「立绘_<角色>_skin1.png」或带皮肤名。
    """
    _kw = str(skin_kw or "").strip()

    def score(n):
        base = str(n)
        if "\t" in base:
            base = base.split("\t")[0]
        base = base.replace("文件:", "").rsplit("/", 1)[-1]
        s = 0
        if _kw and _kw in base:
            s += 300      # 明确点名的皮肤，直接顶到最前
        elif _kw and "skin" in base.lower():
            s += 40       # 其它皮肤次之
        # 职业分支变体（医疗/近卫/术师…）优先级压低，基础形态最像她本人
        if re.search(r"[（(]\S{1,4}[)）]", base):
            s -= 120
        if base.startswith("立绘_"):
            s += 100
        if "_1.png" in base or "_1+.png" in base:
            s += 60
        elif "_2.png" in base:
            s += 40
        elif "_2b" in base or "skin" in base.lower():
            s += 10
        if "头像" in base or "图标" in base or "技能" in base or "道具" in base:
            s -= 200
        if base.lower().endswith(".png"):
            s += 5
        return s
    return sorted(names, key=score, reverse=True)


async def prts_images(name, limit=8, skin_kw=""):
    """从 PRTS 抓角色立绘直链。

    PRTS 是明日方舟官方资料站（MediaWiki），立绘命名规律是
    「立绘_<角色>_1.png」，直接查文件前缀比全网搜图准，也不会拿错人。
    """
    out = []
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers=_PRTS_HEADERS) as c:
            # ① 按前缀直接列文件（最准）
            for prefix in ("立绘_" + name, "立绘_" + name + "(", "立绘_" + name + "_"):
                r = await c.get(_PRTS_API, params={
                    "action": "query", "format": "json", "list": "allimages",
                    "aiprefix": prefix, "ailimit": "40",
                    "aiprop": "url|name|size",
                })
                try:
                    d = r.json()
                except Exception:
                    continue
                for it in (d.get("query", {}).get("allimages") or []):
                    u = it.get("url") or ""
                    n = it.get("name") or ""
                    if u and n and u not in out:
                        out.append(n + "\t" + u)
            # ② 前缀没抓到就退到文件命名空间搜索
            if not out:
                r = await c.get(_PRTS_API, params={
                    "action": "query", "format": "json", "list": "search",
                    "srsearch": name + " 立绘", "srnamespace": "6", "srlimit": "20",
                })
                try:
                    d = r.json()
                except Exception:
                    d = {}
                titles = [it.get("title", "") for it in
                          (d.get("query", {}).get("search") or [])]
                titles = _pick_portrait(titles)
                if titles:
                    r2 = await c.get(_PRTS_API, params={
                        "action": "query", "format": "json",
                        "titles": "|".join(titles[:8]), "prop": "imageinfo",
                        "iiprop": "url",
                    })
                    try:
                        d2 = r2.json()
                    except Exception:
                        d2 = {}
                    for pg in (d2.get("query", {}).get("pages") or {}).values():
                        for ii in (pg.get("imageinfo") or []):
                            u = ii.get("url") or ""
                            if u and u not in out:
                                out.append(u)
        if out:
            print("[角色参考] PRTS 命中 %d 张立绘" % len(out), flush=True)
    except Exception as e:
        print("[角色参考] PRTS 抓图失败 " + type(e).__name__ + ": " + str(e)[:60], flush=True)
    if out and all("\t" in x for x in out):
        ranked = _pick_portrait([x.split("\t")[0] + "\t" + x.split("\t")[1] for x in out], skin_kw)
        out = [x.split("\t")[1] for x in ranked]
    return out[:limit]


async def _search_images(name, limit=10):
    """必应图片搜索，拿图源直链。多试几个查询词，尽量凑够。"""
    key = KNOWN.get(name) or name
    queries = [key + " 立绘", key + " 全身", key + " 官方设定"]
    out = []
    for q in queries:
        if len(out) >= limit:
            break
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(
                    "https://www.bing.com/images/async",
                    params={"q": q, "first": 0, "count": 35, "mkt": "zh-CN", "adlt": "off"},
                    headers={"User-Agent": _UA,
                             "Accept-Language": "zh-CN,zh;q=0.9",
                             "Referer": "https://www.bing.com/"},
                )
            for u in _all_urls(r.text or ""):
                if u not in out:
                    out.append(u)
        except Exception as e:
            print("[角色参考] 搜图失败 " + type(e).__name__ + ": " + str(e)[:60], flush=True)
    return out[:limit]


async def _download_ref(url, name):
    """下载参考图到本地，返回 (path, data_url)。"""
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": _UA, "Referer": "https://www.bing.com/"})
        raw = r.content or b""
        if r.status_code >= 400 or len(raw) < 8000 or len(raw) > 12 * 1024 * 1024:
            print("[角色参考] 跳过 %s (%s, %d 字节)" % (url[:40], r.status_code, len(raw)), flush=True)
            return None, ""
        ctype = str(r.headers.get("content-type") or "").lower()
        if "image" not in ctype and not raw[:4].startswith(b"\x89PNG") and not raw[:3] == b"\xff\xd8\xff":
            return None, ""
        os.makedirs(_REF_DIR, exist_ok=True)
        ext = ".png" if raw[:4].startswith(b"\x89PNG") else ".jpg"
        safe = re.sub(r"[^\w\u4e00-\u9fa5]", "_", name)[:20]
        path = os.path.join(_REF_DIR, "%s_%d%s" % (safe, int(time.time()), ext))
        with open(path, "wb") as f:
            f.write(raw)
        import base64
        b64 = base64.b64encode(raw).decode("ascii")
        return path, "data:image/%s;base64,%s" % ("png" if ext == ".png" else "jpeg", b64)
    except Exception as e:
        print("[角色参考] 下载失败 " + type(e).__name__, flush=True)
        return None, ""


_VIS_PROMPT = (
    "你在给画师做参考。看这张图里的人物，用 100 字以内、逗号分隔的短句，"
    "只描述**外观特征**，供另一次生图照着画，不要写画风、不要写画师、不要写构图：\n"
    "发色与发型（长度、刘海、呆毛、辫子）、瞳色、是否有兽耳/精灵耳/角、"
    "服装主色与款式（制服/外套/裙）、标志性配饰（发饰、领结、徽章、武器）、"
    "大致年龄感与体态。\n"
    "直接输出特征串，不要「这张图」开头，不要解释。"
)


async def _describe(data_url):
    """视觉模型提取外观特征。"""
    try:
        import llm_router
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": _VIS_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}]
        d = await llm_router.call_vision(msgs, max_tokens=220, timeout=90)
        d = str(d or "").strip()
        d = re.sub(r"^(这张图|图中|图片)[^，,]{0,6}[，,]?", "", d).strip()
        d = d.strip("“”\"'。.，, \n")
        return d[:400]
    except Exception as e:
        print("[角色参考] 视觉提取失败 " + type(e).__name__, flush=True)
        return ""


async def get_character_ref(name, force=False, _ark_hint=False):
    """取某个角色的外观特征。返回 {ok, name, desc, image, source, seconds}。"""
    t0 = time.time()
    cache = _load_cache()
    rec = cache.get(name) or {}
    if not force and rec.get("desc") and (time.time() - float(rec.get("ts") or 0)) < CACHE_TTL:
        return {"ok": True, "name": name, "desc": rec["desc"], "cached": True,
                "image": rec.get("image") or "", "seconds": 0.0}

    # 明日方舟的角色直接走 PRTS，比全网搜图准得多
    urls = []
    if _ark_hint:
        urls = await prts_images(name)
    if not urls:
        urls = await _search_images(name)
    if not urls:
        return {"ok": False, "name": name, "error": "没搜到参考图", "seconds": round(time.time() - t0, 1)}

    desc = ""
    used = ""
    local = ""
    for u in urls:
        local, data_url = await _download_ref(u, name)
        if not data_url:
            continue
        desc = await _describe(data_url)
        if desc and len(desc) >= 20:
            used = u
            break

    if not desc:
        return {"ok": False, "name": name, "error": "特征提取失败", "seconds": round(time.time() - t0, 1)}

    cache[name] = {"desc": desc, "ts": time.time(), "image": local or "", "source": used}
    _save_cache(cache)
    secs = round(time.time() - t0, 1)
    print("[角色参考] %s 特征 %d 字，用时 %.1fs" % (name, len(desc), secs), flush=True)
    return {"ok": True, "name": name, "desc": desc, "image": local, "source": used, "seconds": secs}


# ── 画风样本：从真实画作提取风格，存成档案 ──
STYLE_DIR = os.path.join(_DATA, "style_refs")
STYLE_FILE = os.path.join(_DATA, "memory", "art_style.json")
CAPTURE_QQ = (os.getenv("STYLE_CAPTURE_QQ", "") or "").strip()  # 部署方指定采集对象 QQ

_STYLE_PROMPT = (
    "你在帮画师归纳画风。看这张画，用 120 字以内、逗号分隔的短句，只描述**画风**：\n"
    "线条（粗细、是否勾线、干净或随性）、上色（赛璐璐/厚涂/水彩/平涂、是否有渐变、"
    "阴影软硬）、配色倾向（高明度/低饱和/暖冷）、眼睛与皮肤的处理方式、"
    "整体质感（插画感/漫画分格感/写实质感）、画面情绪。\n"
    "不要描述画的是什么角色、什么场景。直接输出风格串，不要「这张画」开头。"
)


def _style_ready():
    try:
        if os.path.isfile(STYLE_FILE):
            with open(STYLE_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
            return str(d.get("desc") or "").strip()
    except Exception:
        pass
    return ""


async def extract_style_from_file(path, note=""):
    """从一张本地图片提取画风，存成档案。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return {"ok": False, "error": "读不到文件 " + str(e)[:60]}
    if len(raw) < 5000:
        return {"ok": False, "error": "文件太小，不是图"}
    import base64
    ext = "png" if str(path).lower().endswith(".png") else "jpeg"
    data_url = "data:image/%s;base64,%s" % (ext, base64.b64encode(raw).decode("ascii"))
    try:
        import llm_router
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": _STYLE_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}]
        desc = str(await llm_router.call_vision(msgs, max_tokens=260, timeout=90) or "").strip()
        desc = re.sub(r"^(这张画|图中|画面)[^，,]{0,6}[，,]?", "", desc).strip()
        desc = desc.strip("“”\"'。.，, \n")
    except Exception as e:
        return {"ok": False, "error": "视觉提取失败 " + type(e).__name__}
    if len(desc) < 15:
        return {"ok": False, "error": "提取内容太短"}
    try:
        os.makedirs(os.path.dirname(STYLE_FILE), exist_ok=True)
        with open(STYLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"desc": desc, "ts": time.time(), "source": path,
                       "note": str(note)[:80]}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        return {"ok": False, "error": "保存失败 " + str(e)[:60]}
    print("[画风] 已提取 %d 字" % len(desc), flush=True)
    return {"ok": True, "desc": desc}


async def _fetch_image_bytes(url):
    """取图片原始字节。QQ 的图要先过 NapCat，直接 httpx 会 UnsupportedProtocol。"""
    u = str(url or "").strip()
    if not u:
        return b""
    # ① QQ CDN 链接 / 文件 token → 交给 NapCat 取 base64
    try:
        import napcat_channel
        is_qq = napcat_channel.is_qq_cdn_url(u) or not u.startswith("http")
        if is_qq and napcat_channel.channel.connected:
            du = await napcat_channel.fetch_image_as_dataurl(u)
            if du and "," in du:
                import base64
                return base64.b64decode(du.split(",", 1)[1])
    except Exception:
        pass
    # ② 普通外链直接下
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
            r = await c.get(u, headers={"User-Agent": _UA})
        if r.status_code >= 400:
            return b""
        return r.content or b""
    except Exception:
        return b""


async def capture_style_sample(url, who=""):
    """把别人发的图存成画风样本（不做提取，等主人确认再用）。"""
    try:
        os.makedirs(STYLE_DIR, exist_ok=True)
        raw = await _fetch_image_bytes(url)
        if len(raw) < 20000:
            return None
        ext = ".png" if raw[:4].startswith(b"\x89PNG") else ".jpg"
        path = os.path.join(STYLE_DIR, "style_%d%s" % (int(time.time()), ext))
        with open(path, "wb") as f:
            f.write(raw)
        print("[画风] 已存样本 %s (%d 字节)" % (path, len(raw)), flush=True)
        return path
    except Exception as e:
        print("[画风] 存样本失败 " + type(e).__name__, flush=True)
        return None


async def build_character_block(text):
    """把请求里提到的角色，转成一段「照着画」的描述。"""
    names = extract_characters(text)
    if not names:
        return "", []
    _ark = is_arknights(text)
    parts = []
    meta = []
    for name in names:
        r = await get_character_ref(name, _ark_hint=_ark)
        if r.get("ok"):
            parts.append("【%s 的形象特征 · 必须照此画】%s" % (name, r["desc"]))
            meta.append({"name": name, "seconds": r.get("seconds"), "cached": r.get("cached")})
        else:
            meta.append({"name": name, "error": r.get("error")})
    if not parts:
        return "", meta
    return ("\n".join(parts) + "\n以上特征要严格对应，不要换成别的角色。", meta)
