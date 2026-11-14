"""画图工作流：先查资料，再画。

流程：
  1. 认出要画什么（人物 / 场景）
  2. 强制检索：明日方舟走 PRTS，其余走网络搜索；拿参考图 + 文字设定
  3. 对参考图做「非常详细」的外观提取，多图综合成一份可照着还原的描述
  4. 先把查到的东西简短回给用户（让她知道我在照着什么画）
  5. 再进生图；这期间用户若说「错了 / 别画了」，直接终止并撒娇认错
"""
import asyncio
import os
import re
import time

_DATA = os.getenv("DATA_DIR", "./data")


# ── 消极反馈：用户否掉就停 ──────────────────────────────
# 明确要求停下的词，出现在句子里任何位置都算
_NEG_STRONG = re.compile(
    # 「别画」两个字是最自然的喊停方式，原来只认「别画了」，导致喊停没反应。
    r"别画|不要画|甭画|不用画|先别画|别搞了|别弄了|别继续|不要继续"
    r"|停止|停下|停一停|打住|取消|撤销|不用了|算了|重来|换个画|换一个"
    # 排除「别停」「不要停」这种反过来催的
    r"(?<!别)(?<!不要)停(?!不下来)"
)
# 否定判断一般出现在句首，避免「报错了」这种误伤
_NEG_START = re.compile(
    r"^(错了|不对|不是这个|不是他|不是她|不是它|搞错了|认错了|弄错了"
    r"|完全不像|不像|差太多|差远了|停|等等|等一下)"
)

def is_negative(text):
    """这句是不是在否掉当前的事。"""
    t = str(text or "").strip()
    if not t:
        return False
    # 太长的一般是在说别的事，不当成否定指令
    if len(t) > 30:
        return False
    if _NEG_STRONG.search(t):
        return True
    return bool(_NEG_START.search(t))


# ── 待处理任务登记（可中断）────────────────────────────
PENDING = {}          # key -> {"ts":..., "subject":..., "stage":..., "cancel": False}


def register(key, subject):
    PENDING[str(key)] = {
        "ts": time.time(), "subject": str(subject)[:60],
        "stage": "search", "cancel": False,
    }


def stage(key, name):
    it = PENDING.get(str(key))
    if it:
        it["stage"] = name


def cancelled(key):
    it = PENDING.get(str(key))
    return bool(it and it.get("cancel"))


def mark_cancel(key):
    it = PENDING.get(str(key))
    if it:
        it["cancel"] = True
        return it.get("subject") or ""
    return ""


def clear(key):
    PENDING.pop(str(key), None)


def has_pending(key):
    return str(key) in PENDING


def gc(max_age=600):
    """清掉太久没动的登记，避免卡住。"""
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - float(v.get("ts") or 0) > max_age]:
        PENDING.pop(k, None)


# ── 参考图分析：要非常详细 ─────────────────────────────
_DEEP_PROMPT = (
    "你在给画师做严谨的参考。仔细看这张图里的人物，尽可能完整地描述外观，"
    "供另一次生图高度还原。按下面顺序写，用逗号分隔的短句，不要分点编号：\n"
    "1. 头部：发色（含渐变/挑染）、发长、发型（刘海、卷直、扎法、呆毛）、发饰；\n"
    "2. 面部：瞳色与眼型、眉毛、肤色、有无兽耳/精灵耳/角/尾巴等非人特征；\n"
    "3. 上装：款式、主色、领口、袖型、层次、纽扣或拉链位置、纹样；\n"
    "4. 下装与鞋：裙/裤的款式长度颜色、袜子、鞋型与颜色；\n"
    "5. 配饰与装备：首饰、腰带、包、徽章、武器、披风、手套；\n"
    "6. 体态与气质：身高体态、站姿、表情基调、整体印象。\n"
    "只写看得见的外观，不要写画风、构图、背景故事，不要「这张图」开头。"
)


async def deep_describe(image_ref):
    """对一张参考图做详细外观提取。image_ref 可以是 data_url 或本地路径。"""
    try:
        import base64
        import llm_router

        if str(image_ref).startswith("data:"):
            data_url = image_ref
        else:
            p = str(image_ref)
            if not os.path.isfile(p):
                return ""
            raw = open(p, "rb").read()
            if len(raw) < 3000:
                return ""
            ext = "png" if p.lower().endswith(".png") else "jpeg"
            data_url = "data:image/%s;base64,%s" % (
                ext, base64.b64encode(raw).decode("ascii"))

        msgs = [{"role": "user", "content": [
            {"type": "text", "text": _DEEP_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}]
        d = await llm_router.call_vision(msgs, max_tokens=520, timeout=16)
        d = str(d or "").strip()
        d = re.sub(r"^(这张图|图中|图片|画面)[^，,]{0,8}[，,]", "", d).strip()
        d = d.strip("“”\"'。.，, \n")
        # 去掉可能出现的编号
        d = re.sub(r"(^|\s)[1-6][\.、]\s*", " ", d).strip()
        return d[:900]
    except Exception as e:
        print("[画图] 详细提取失败 " + type(e).__name__, flush=True)
        return ""


_DESC_LABEL = re.compile(
    r"(?:^|，|,|、|；|;)\s*(头部|面部|脸部|上装|上衣|下装|裙装|配饰|装备|体态|气质|外表|外观)\s*[：:]?\s*")


def _clean_desc(d):
    """清洗合并结果：去结构词、去重复分句、限长。"""
    t = str(d or "").strip()
    if not t:
        return ""
    t = _DESC_LABEL.sub("，", t)
    t = re.sub(r"^(头部|面部|上装|下装|配饰|体态|气质)[：:，,]\s*", "", t)
    # 句号也当分隔符，否则句号后的重复分句去不掉
    t = re.sub(r"[；;。\n]+", "，", t)
    t = re.sub(r"，{2,}", "，", t)
    parts = [x.strip() for x in t.split("，") if x.strip()]
    seen = set()
    out = []
    for x in parts:
        k = x.replace(" ", "")
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    t = "，".join(out).strip("，。. ")
    if t and not t.endswith("。"):
        t = t + "。"
    return t[:480]


async def merge_descriptions(pieces, subject="", extra=""):
    """多张图的描述合成一份更完整、去重后的最终描述。"""
    pieces = [p for p in (pieces or []) if p and len(p) >= 20]
    if not pieces:
        return ""
    if len(pieces) == 1 and not extra:
        return pieces[0]

    try:
        import llm_router

        body = "\n\n".join("【参考图 %d】%s" % (i + 1, p) for i, p in enumerate(pieces))
        if extra:
            body += "\n\n【文字资料】" + str(extra)[:800]
        prompt = (
            "下面是同一个对象的多份外观描述（可能来自不同参考图），以及一些文字资料。\n"
            "请合并成一份**完整、去重、可直接照着复原**的外观描述，供生图使用。\n"
            "要求：\n"
            "- 只保留稳定的、一致的特征；某张图独有的细节若与设定相符也保留；\n"
            "- 按 头部/面部/上装/下装与鞋/配饰装备/体态气质 的顺序写，逗号分隔短句；\n"
            "- 具体到颜色和款式，不要用「漂亮」「精致」这类空话；\n"
            "- 不要提「参考图」「根据图片」，直接描述对象本身。\n"
            "- 整段 200~420 字。\n\n"
            "对象：%s\n\n%s" % (subject or "画面主角", body)
        )
        d = await llm_router.call_text([{"role": "user", "content": prompt}],
                                      max_tokens=700, timeout=16)
        d = str(d or "").strip()
        d = re.sub(r"^(合并|综合|最终|描述)[^\n]{0,12}[:：]\s*", "", d).strip()
        return _clean_desc(d)
    except Exception as e:
        print("[画图] 合并失败 " + type(e).__name__, flush=True)
        return "；".join(pieces)[:1200]


# ── 简短报告：让用户知道我在照着什么画 ────────────────
def build_report(subject, desc, source_note=""):
    """给用户看的短报告：只说一句查到了什么，多一个字都不加。"""
    s = str(subject or "").strip()
    d = str(desc or "").strip()
    if not d:
        return "没找着%s的准谱儿，我凭印象画。" % (s or "这个")

    # 从描述里挑几个关键点，压成一句
    bits = [x.strip() for x in re.split(r"[，,；;、]", d) if len(x.strip()) >= 2]
    key = "、".join(bits[:4]) if bits else d[:40]
    return ("%s大概是：%s。" % (s or "画面", key[:60]))[:120]

# ── 检索：先查清楚再画 ──────────────────────────────────
async def search_text(query, limit=3):
    """搜一段文字资料（官方设定、外观描述）。"""
    try:
        import web_search
        r = await web_search.search(query, limit=limit)
        if not r.get("ok"):
            return ""
        rows = []
        for it in (r.get("results") or [])[:limit]:
            ti = str(it.get("title") or "").strip()
            sn = str(it.get("snippet") or "").strip()
            if sn:
                rows.append((ti + "：" + sn)[:220])
        return "\n".join(rows)
    except Exception as e:
        print("[画图] 文字检索失败 " + type(e).__name__, flush=True)
        return ""


# A4：详细描述缓存 —— 同一个角色画第二遍不该再分析一次
DESC_CACHE = os.path.join(_DATA, "memory", "draw_desc_cache.json")
DESC_TTL = float(os.getenv("DRAW_DESC_TTL", str(30 * 86400)))


def _load_desc_cache():
    try:
        import json
        if os.path.isfile(DESC_CACHE):
            with open(DESC_CACHE, encoding="utf-8") as f:
                d = json.load(f) or {}
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _save_desc_cache(d):
    try:
        import json
        os.makedirs(os.path.dirname(DESC_CACHE), exist_ok=True)
        tmp = DESC_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, DESC_CACHE)
    except Exception:
        pass


def get_cached_desc(subject):
    """取缓存的外观描述（命中返回 desc，否则空）。"""
    if not subject:
        return ""
    rec = _load_desc_cache().get(str(subject)) or {}
    desc = str(rec.get("desc") or "")
    if desc and len(desc) >= 40 and (time.time() - float(rec.get("ts") or 0)) < DESC_TTL:
        return desc
    return ""


def put_cached_desc(subject, desc, source=""):
    if not subject or not desc:
        return
    d = _load_desc_cache()
    d[str(subject)] = {"desc": str(desc)[:600], "ts": time.time(),
                       "source": str(source)[:30]}
    # 只留最近 200 个角色
    if len(d) > 200:
        old = sorted(d.items(), key=lambda x: x[1].get("ts", 0))[:50]
        for k, _ in old:
            d.pop(k, None)
    _save_desc_cache(d)


async def gather(subject, full_text="", ark=False, max_images=2, budget=0, skin_kw=""):
    """查参考图和文字资料。

    返回 {ok, images, texts, desc, source}
    """
    import character_ref as cref

    t0 = time.time()

    # A4：先看缓存，命中就直接用（同一个角色画第二遍省 ~30s）
    _hit = get_cached_desc(subject)
    if _hit:
        print("[画图] %s 命中描述缓存（省掉参考图分析）" % (subject or "未命名"), flush=True)
        return {"ok": True, "desc": _hit, "images": [], "texts": [],
                "source": "缓存", "pieces": 0, "seconds": round(time.time() - t0, 1),
                "cached": True}

    # A5：明日方舟走快路——PRTS 上干员页面就有立绘，直接提特征最快。
    # 其它题材才按「文字优先于图片」来。
    async def _images():
        u = []
        src = ""
        if ark:
            try:
                u = await cref.prts_images(subject, limit=6, skin_kw=skin_kw)
                if u:
                    src = "PRTS 明日方舟wiki"
            except Exception as e:
                print("[画图] PRTS 失败 " + type(e).__name__, flush=True)
        if not u:
            try:
                u = await cref._search_images(subject, limit=10)
                if u:
                    src = "网络图片搜索"
            except Exception as e:
                print("[画图] 搜图失败 " + type(e).__name__, flush=True)
        return u, src

    async def _texts():
        try:
            q = ("%s 外观 外貌 设定 服装" % subject) if subject else str(full_text)[:30]
            t = await search_text(q, limit=2)
            return [t] if t else []
        except Exception:
            return []

    texts = []
    urls = []
    source = ""
    if ark:
        # 明日方舟：立绘就是官方设定，比文字准也比文字快
        print("[画图] %s 是明日方舟，直接取 PRTS 立绘%s"
              % (subject or "角色", ("·" + skin_kw) if skin_kw else ""), flush=True)
        urls, source = await _images()
    else:
        texts = await _texts()
        if texts:
            _t = "\n".join(str(x) for x in texts if x)[:900]
            if len(_t) >= 40:
                secs = round(time.time() - t0, 1)
                print("[画图] %s 用文字资料，跳过参考图（%.1fs）"
                      % (subject or "未命名", secs), flush=True)
                return {"ok": True, "desc": _t, "images": [], "texts": texts,
                        "source": "文字资料", "pieces": 1, "seconds": secs}
        print("[画图] 没查到文字资料，改用参考图", flush=True)
        urls, source = await _images()

    pieces = []
    local_imgs = []
    for u in urls:
        if len(pieces) >= max_images:
            break
        try:
            local, data_url = await cref._download_ref(u, subject or "ref")
        except Exception:
            continue
        if not data_url:
            continue
        d = await deep_describe(data_url)
        if d and len(d) >= 40:
            pieces.append(d)
            if local:
                local_imgs.append(local)

    secs = round(time.time() - t0, 1)
    if not pieces:
        return {"ok": False, "reason": "没拿到可用的参考", "source": source,
                "seconds": secs, "images": [], "desc": "", "texts": texts}

    desc = await merge_descriptions(pieces, subject=subject,
                                    extra="\n".join(texts)[:400])
    put_cached_desc(subject, desc, source)

    print("[画图] %s 参考 %d 张，描述 %d 字，用时 %.1fs"
          % (subject or "未命名", len(pieces), len(desc), secs), flush=True)
    return {
        "ok": bool(desc),
        "desc": desc,
        "images": local_imgs,
        "texts": texts,
        "source": source,
        "pieces": len(pieces),
        "seconds": secs,
    }

# ── 画图记录：控制台能看见她查了什么、画了什么 ────────
DRAW_LOG_FILE = os.path.join(_DATA, "draw_log.json")
DRAW_LOG_MAX = 60


def _load_log():
    try:
        if os.path.isfile(DRAW_LOG_FILE):
            import json
            with open(DRAW_LOG_FILE, encoding="utf-8") as f:
                got = json.load(f) or []
            if isinstance(got, list):
                return got
    except Exception:
        pass
    return []


def log_draw(subject="", report="", desc="", source="", images=0,
             ok=True, cancelled=False, seconds=0, error=""):
    import json
    rows = _load_log()
    rows.append({
        "ts": time.time(),
        "time": time.strftime("%m-%d %H:%M:%S"),
        "subject": str(subject)[:40],
        "report": str(report)[:120],
        "desc": str(desc)[:400],
        "desc_len": len(str(desc)),
        "source": str(source)[:30],
        "images": int(images or 0),
        "ok": bool(ok),
        "cancelled": bool(cancelled),
        "seconds": round(float(seconds or 0), 1),
        "error": str(error)[:80],
    })
    try:
        os.makedirs(os.path.dirname(DRAW_LOG_FILE), exist_ok=True)
        tmp = DRAW_LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-DRAW_LOG_MAX:], f, ensure_ascii=False)
        os.replace(tmp, DRAW_LOG_FILE)
    except Exception:
        pass


def recent_draws(limit=20):
    return _load_log()[-int(limit):][::-1]


def draw_stats():
    rows = _load_log()
    if not rows:
        return {"total": 0, "ok": 0, "cancel": 0, "avg_desc": 0, "avg_seconds": 0}
    ok = sum(1 for r in rows if r.get("ok") and not r.get("cancelled"))
    cancel = sum(1 for r in rows if r.get("cancelled"))
    dl = [int(r.get("desc_len") or 0) for r in rows if r.get("desc_len")]
    return {
        "total": len(rows),
        "ok": ok,
        "cancel": cancel,
        "avg_desc": round(sum(dl) / len(dl), 0) if dl else 0,
        "avg_seconds": round(sum(float(r.get("seconds") or 0) for r in rows) / len(rows), 1),
    }


def pending_list():
    """当前挂着的画图任务，面板上要能看见。"""
    now = time.time()
    return [
        {"key": k, "subject": v.get("subject"), "stage": v.get("stage"),
         "seconds": round(now - float(v.get("ts") or now), 1),
         "cancel": bool(v.get("cancel"))}
        for k, v in PENDING.items()
    ]