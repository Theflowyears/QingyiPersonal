"""
表情包(斗图)收藏库 - 图片表情包的收藏、检索、随机发送。

与 QQ 内置小表情(msg_enhance.EMOJI_MAP)不同，这里是"图片表情包"：
- 用户发图 + /收藏表情  → 把该图存进本地图库并打标签
- /斗图 [关键词]        → 按关键词随机抽一张发出去对线
- /表情包列表           → 看有哪些标签
图库目录默认 data/stickers，索引 data/stickers/index.json。
"""
import os
import re
import json
import shutil
import time
import uuid
import random
import threading
from pathlib import Path
from typing import Optional


STICKER_DIR = Path(os.getenv("STICKER_DIR", str(Path(os.getenv("DATA_DIR", "./data")) / "stickers")))
MAX_STICKERS = int(os.getenv("STICKER_MAX", "500"))
# 人工筛选删掉的那些留在回收里的上限（见 delete_sticker）。
TRASH_KEEP = int(os.getenv("STICKER_TRASH_KEEP", "200"))
_index_cache = {"data": None, "ts": 0.0}
# 索引是「整包读、整包写」的，收一张图中间还要 await 视觉识别（可达几十秒）。
# 没有这把锁，两次并发收藏会后写的那份会直接覆盖前一份，被覆盖的那张图就变成
# 磁盘上有、索引里没有的孤儿 —— 线上 2026-11-07 实测到 2 个。
_INDEX_LOCK = threading.RLock()
IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _index_path() -> Path:
    return STICKER_DIR / "index.json"


def _load_index(ttl=3.0) -> dict:
    now = time.time()
    if _index_cache["data"] is not None and (now - _index_cache["ts"]) < ttl:
        return _index_cache["data"]
    p = _index_path()
    data = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            data = {}
    _index_cache["data"] = data
    _index_cache["ts"] = now
    return data


def _save_index(data: dict):
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _index_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_index_path())
    _index_cache["data"] = data
    _index_cache["ts"] = time.time()


def _update_index(mutate):
    """锁内「重读磁盘 → mutate(idx) → 写回」。

    凡是中间 await 过（比如识别含义那几十秒）再写索引的地方都必须走这里。
    把自己手上那份 idx 直接整包写回去，会冲掉 await 期间别人新增的记录 ——
    那张图留在磁盘上但索引里没有，她就永远不会再发它，库里也看不出少了东西。
    mutate 返回 None 表示啥也没改，就不费这一次盘。
    """
    with _INDEX_LOCK:
        idx = _load_index(ttl=0.0)
        out = mutate(idx)
        if out is not None:
            _save_index(idx)
        return out


def _norm_tags(tags) -> list:
    if isinstance(tags, str):
        parts = re.split(r"[,，\s]+", tags)
    elif isinstance(tags, (list, tuple)):
        parts = list(tags)
    else:
        parts = []
    out = []
    for t in parts:
        t = str(t).strip()
        if t and t not in out:
            out.append(t[:24])
    return out[:12]


# ── 这是表情还是照片 ─────────────────────────────────
# 以前这段一个判定都没有：群里来一张图就收，手机照片和长截图因此全混进斗图库。
# 判定只看文件头（本仓库没有 Pillow，也不为此加依赖）：格式 + 最长边 + 字节数。
# 认不出尺寸就当不是表情 —— 宁可少收一张梗图，也不要把照片当成她的表情包。
def _gate_limits():
    px = max(64, int(os.getenv("STICKER_STEAL_MAX_PX", "512")))
    kb = max(8192, int(os.getenv("STICKER_STEAL_MAX_BYTES", str(1200 * 1024))))
    # JPEG 绝大多数是手机照片，单独收紧
    return {"gif": (px, kb), "png": (min(px, 480), min(kb, 400 * 1024)),
            "webp": (min(px, 480), min(kb, 400 * 1024)),
            "jpg": (min(px, 512), min(kb, 300 * 1024))}


def _jpeg_dims(b):
    i = 2
    n = len(b)
    while i + 9 < n:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0x01,) or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = (b[i + 5] << 8) | b[i + 6]
            w = (b[i + 7] << 8) | b[i + 8]
            return (w, h, "jpg")
        seg = (b[i + 2] << 8) | b[i + 3]
        if seg < 2:
            break
        i += 2 + seg
    return (0, 0, "jpg")


def _webp_dims(b):
    i = 12
    n = len(b)
    while i + 8 <= n:
        fourcc = b[i:i + 4]
        size = int.from_bytes(b[i + 4:i + 8], "little")
        payload = i + 8
        if fourcc == b"VP8X" and payload + 10 <= n:
            w = int.from_bytes(b[payload + 4:payload + 7], "little") + 1
            h = int.from_bytes(b[payload + 7:payload + 10], "little") + 1
            return (w, h, "webp")
        if fourcc == b"VP8 " and payload + 10 <= n:
            w = (b[payload + 6] | b[payload + 7] << 8) & 0x3FFF
            h = (b[payload + 8] | b[payload + 9] << 8) & 0x3FFF
            return (w, h, "webp")
        if size <= 0:
            break
        i = payload + size + (size & 1)
    return (0, 0, "webp")


def _dims(raw):
    """从文件头读宽高与格式；读不出来返回 (0, 0, "")。"""
    b = bytes(raw or b"")
    if len(b) < 16:
        return (0, 0, "")
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return (b[6] | (b[7] << 8), b[8] | (b[9] << 8), "gif")
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        try:
            w, h = struct.unpack(">II", b[16:24])
            return (int(w), int(h), "png")
        except struct.error:
            return (0, 0, "png")
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return _webp_dims(b)
    if b[:3] == b"\xff\xd8\xff":
        return _jpeg_dims(b)
    return (0, 0, "")


def looks_like_sticker(raw):
    """(该不该收, 为什么)。照片走看图链路，不进表情包库。"""
    w, h, kind = _dims(raw)
    if not kind:
        return False, "认不出图片格式"
    if not w or not h:
        return False, "%s 认不出尺寸" % kind
    px, cap = _gate_limits().get(kind, (0, 0))
    if len(raw) > cap:
        return False, "%s %dKB 超过表情包上限 %dKB" % (kind, len(raw) // 1024, cap // 1024)
    if max(w, h) > px:
        return False, "%s %dx%d 太大，像是照片或截图" % (kind, w, h)
    return True, ""


def _content_hash(raw):
    import hashlib
    return hashlib.sha1(bytes(raw or b"")).hexdigest()


def _is_collected_face(rec):
    """这张是她（或主人）明确收藏的表情，不是群里顺手混进来的图。

    「超过 512px 就当照片」这道门管的是自动收图；QQ 收藏里确实有 600–2048px 的
    大图（现网实测最大 2048×2048），那是她自己收的表情，体检不能反过来把它们赶走。
    """
    return bool(str(rec.get("src") or "")) or QQ_FACE_TAG in (rec.get("tags") or [])


def audit_library(dry_run=True):
    """把库里已经混进来的照片移出去（不是删除），并回写真索引。

    默认试运行只报数。`dry_run=False` 时把不合规矩的图移到
    `excluded-<日期>/` 并在 `excluded.json` 留一条 —— 随时能拿回来。
    顺带三件旧账：给没有哈希的条目补上哈希（不然下次还是会重收一遍），
    把内容完全相同的重复条目并成一条，以及把「文件在、索引里没有」的
    孤儿找回来（并发覆盖留下的，见 _update_index）。
    """
    idx = _load_index(ttl=0.0)
    folder = "excluded-%s" % time.strftime("%Y%m%d")
    dest = STICKER_DIR / folder
    ledger_path = STICKER_DIR / "excluded.json"
    ledger = []
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8")) or []
        except Exception:
            ledger = []
    keep = hide = dup = 0
    touched = False
    kept = {}
    for sid, rec in list(idx.items()):
        path = STICKER_DIR / str(rec.get("file") or "")
        if not path.is_file():
            hide += 1
            if not dry_run:
                idx.pop(sid, None)
            continue
        try:
            raw = path.read_bytes()
        except Exception:
            continue
        digest = _content_hash(raw)
        if not dry_run and rec.get("hash") != digest:
            rec["hash"] = digest
            w, h, kind = _dims(raw)
            rec["dims"] = [w, h]
            rec["kind"] = kind
            touched = True
        ok, why = looks_like_sticker(raw)
        if not ok and _is_collected_face(rec):
            ok, why = True, ""
        if ok and rec.get("hash") and kept.get(digest):
            ok, why = False, "和 %s 是同一张图" % kept[digest]
            dup += 1
        if ok:
            keep += 1
            kept.setdefault(digest, sid)
            continue
        hide += 1
        if dry_run:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(dest / path.name))
            ledger.append({"id": sid, "file": path.name, "reason": why[:60],
                           "bytes": len(raw), "moved": int(time.time()),
                           "note": str(rec.get("note") or "")[:60]})
            idx.pop(sid, None)
        except Exception:
            continue
    # 磁盘上有、索引里没有的图：并发收藏互相覆盖留下的孤儿（见 _update_index）。
    # 能过门的找回来继续用，过不了门的和照片一起移走 —— 都是可逆的。
    known = {str(v.get("file") or "") for v in idx.values()}
    orphans = adopt = 0
    if STICKER_DIR.is_dir():
        for path in sorted(STICKER_DIR.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                continue
            if path.name in known:
                continue
            try:
                raw = path.read_bytes()
            except Exception:
                continue
            orphans += 1
            digest = _content_hash(raw)
            ok, why = looks_like_sticker(raw)
            if ok and kept.get(digest):
                ok, why = False, "和 %s 是同一张图" % kept[digest]
                dup += 1
            if ok:
                if not dry_run:
                    w, h, kind = _dims(raw)
                    new_id = path.stem
                    idx[new_id] = {"id": new_id, "file": path.name,
                                   "abs": str(path.resolve()), "tags": [],
                                   "note": "回填：索引里丢的那张",
                                   "mime": "image/jpeg" if kind == "jpg" else "image/" + kind,
                                   "bytes": len(raw), "hash": digest,
                                   "dims": [w, h], "kind": kind,
                                   "added": int(path.stat().st_mtime),
                                   "seen": 1, "used": 0}
                    known.add(path.name)
                    kept.setdefault(digest, new_id)
                    adopt += 1
                continue
            hide += 1
            if dry_run:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(dest / path.name))
                ledger.append({"id": "", "file": path.name,
                               "reason": ("没入库的那张：" + why)[:60],
                               "bytes": len(raw), "moved": int(time.time()),
                               "note": "回填：索引里丢的那张"})
            except Exception:
                continue
    if not dry_run and (hide or touched or adopt):
        _save_index(idx)
        try:
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        except Exception:
            pass
    return {"checked": keep + hide, "keep": keep, "hide": hide,
            "duplicates": dup, "orphans": orphans, "adopted": adopt,
            "folder": folder, "dry_run": bool(dry_run)}


_EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/webp": ".webp", "image/bmp": ".bmp"}


async def _save_raw(raw, mime="image/png", tags=None, note="", force=False,
                    src="", extra=None, with_meaning=True):
    """落盘一张表情包并回写索引。

    群里偷的、指令存的、QQ 收藏导入的 —— 三个入口都汇到这里，
    去重、门槛、并发合并只写一遍，省得每条路各漏一点。
    force=True 只用于明确「收藏这张」和导入她自己的收藏；
    自动收图必须过 `looks_like_sticker`，否则群里的照片会堆进表情包库。
    """
    raw = bytes(raw or b"")
    if not raw or len(raw) > 8 * 1024 * 1024:
        return None
    ext = _EXT_BY_MIME.get(str(mime or "").lower(), ".jpg")
    w, h, kind = _dims(raw)
    if not force:
        ok, why = looks_like_sticker(raw)
        if not ok:
            print("[表情] 这张不收：%s" % why, flush=True)
            return None
    digest = _content_hash(raw)

    # 去重写在这里，不在某个入口里：同一张图被群友重发、被「教含义」再收一次，
    # 都只该是一条记录。以前哈希只被生产零调用的 steal_from_url 用过，于是库全是重的。
    def _seen_before(cur):
        for old_id in sorted(cur, key=lambda k: -(cur[k].get("used") or 0)):
            hit = cur[old_id]
            if hit.get("hash") == digest:
                hit["seen"] = int(hit.get("seen") or 1) + 1
                hit["last_seen"] = int(time.time())
                if src and not hit.get("src"):
                    # 同一张图从 QQ 收藏又来了了一遍：记下地址，下次不用再下载。
                    hit["src"] = str(src)[:300]
                out = dict(hit)
                out["deduped"] = True
                return out
        return None

    repeated = _update_index(_seen_before)
    if repeated:
        return repeated
    sid = uuid.uuid4().hex[:12]
    fname = f"{sid}{ext}"
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    (STICKER_DIR / fname).write_bytes(raw)

    rec = {
        "id": sid,
        "file": fname,
        "abs": str((STICKER_DIR / fname).resolve()),
        "tags": _norm_tags(tags),
        "note": (note or "")[:80],
        "mime": mime,
        "bytes": len(raw),
        "hash": digest,
        "dims": [w, h],
        "kind": kind,
        "added": int(time.time()),
        "seen": 1,
        "used": 0,
    }
    if src:
        rec["src"] = str(src)[:300]
    if extra:
        rec.update(extra)
    # 顺手看一眼这张图是什么意思（失败不影响收藏）
    if with_meaning:
        try:
            _m = await describe_meaning(str((STICKER_DIR / fname).resolve()))
            if _m:
                rec["meaning"] = _m
                rec["meaning_src"] = "vision"
                rec["meaning_ts"] = time.time()
                print("[表情] 自动识别含义: " + _m[:40], flush=True)
        except Exception:
            pass

    # await 之后必须重新读一遍再合并，不能用手上那份 idx 整包写回去：
    # 识别含义那几十秒里另一张图可能已经存好了，覆盖写会让它变成
    # 「文件在、索引里没有」的孤儿（线上 2026-11-07 实测到 2 个）。
    def _commit(cur):
        # 上面那次去重和这里之间隔着一次视觉识别的 await：同一张图被同时收两次
        # 时，两次都会走到这儿，所以必须再判一遍，否则库里又多一条重复。
        for old_id, hit in sorted(cur.items()):
            if old_id != sid and hit.get("hash") == digest:
                try:
                    (STICKER_DIR / fname).unlink(missing_ok=True)
                except Exception:
                    pass
                hit["seen"] = int(hit.get("seen") or 1) + 1
                hit["last_seen"] = int(time.time())
                out = dict(hit)
                out["deduped"] = True
                return out
        cur[sid] = rec
        if len(cur) > MAX_STICKERS:
            old = sorted((r for r in cur.values() if r.get("id") != sid),
                         key=lambda r: (r.get("used", 0), r.get("added", 0)))
            drop = old[0]
            try:
                (STICKER_DIR / drop["file"]).unlink(missing_ok=True)
            except Exception:
                pass
            cur.pop(drop["id"], None)
        return rec

    return _update_index(_commit)


async def save_sticker_from_dataurl(data_url: str, tags=None, note="",
                                    force=False) -> Optional[dict]:
    """把 data:image;base64 落盘成一张图片表情包，返回记录。"""
    import base64
    data_url = (data_url or "").strip()
    m = re.match(r"data:(image/[a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.S)
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return None
    return await _save_raw(raw, m.group(1), tags=tags, note=note, force=force)


# ── 从群里「偷」表情包 ──────────────────────────────────
# 群友发的梗图往往比自带的更对味。看到合适的就存一份，用的时候拿自己的。
STEAL_ENABLED = os.getenv("STICKER_STEAL", "1").strip() != "0"
STEAL_MAX_BYTES = int(os.getenv("STICKER_STEAL_MAX_BYTES", str(4 * 1024 * 1024)))
STEAL_MIN_BYTES = int(os.getenv("STICKER_STEAL_MIN_BYTES", "6000"))
STEAL_DAILY_CAP = int(os.getenv("STICKER_STEAL_DAILY", "20"))
_STEAL_LOG = {"day": "", "n": 0}


def _steal_quota_ok():
    import datetime
    today = datetime.date.today().isoformat()
    if _STEAL_LOG["day"] != today:
        _STEAL_LOG["day"] = today
        _STEAL_LOG["n"] = 0
    return _STEAL_LOG["n"] < STEAL_DAILY_CAP


def steal_ok():
    """对外问一句「今天还能不能再收一张」。"""
    return bool(STEAL_ENABLED) and _steal_quota_ok()


def note_stolen(n=1):
    """收下一张后记数。

    新链路（token → data URL → save_sticker_from_dataurl）不走 steal_from_url，
    所以那边的计数不会动。不补这一下，日上限就是摆设，一天能把库塞满。
    """
    global _STEAL_LOG
    import datetime
    today = datetime.date.today().isoformat()
    if _STEAL_LOG["day"] != today:
        _STEAL_LOG["day"] = today
        _STEAL_LOG["n"] = 0
    _STEAL_LOG["n"] = int(_STEAL_LOG["n"]) + max(0, int(n))
    return _STEAL_LOG["n"]


async def steal_from_url(url, tags=None, note="", sender=""):
    """从群友发的一张图里偷表情包。成功返回记录。"""
    import httpx
    if not STEAL_ENABLED:
        return None
    if not _steal_quota_ok():
        print("[偷图] 今日 %d 张已满" % STEAL_DAILY_CAP, flush=True)
        return None
    url = str(url or "").strip()
    if not url.startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36")})
        raw = r.content or b""
    except Exception as e:
        print("[偷图] 下载失败 " + type(e).__name__, flush=True)
        return None

    if r.status_code >= 400 or not raw:
        return None
    if len(raw) < STEAL_MIN_BYTES or len(raw) > STEAL_MAX_BYTES:
        return None
    ctype = str(r.headers.get("content-type") or "").lower()
    if "gif" in ctype:
        mime = "image/gif"
    elif raw[:4].startswith(b"\x89PNG"):
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None

    # 落盘、去重、并发合并全部交给 _save_raw —— 这里以前自己算一种
    # 「md5(前 4KB + 长度)」的哈希写进同一个 hash 字段，和生产用的 sha1 对不上，
    # 于是去重形同不存在，库里全是重复。一条库只能有一种指纹。
    rec = await _save_raw(raw, mime, tags=tags, note=note,
                          extra={"stolen": True, "from": str(sender or "")[:20]})
    if rec is None or rec.get("deduped"):
        return None          # 库里已经有了，今天的额度不该为一张重复图花掉
    _STEAL_LOG["n"] += 1
    print("[偷图] 收下一张表情包 %s（%d KB，今日第 %d 张）"
          % (rec.get("file"), max(1, len(raw) // 1024), _STEAL_LOG["n"]), flush=True)
    return rec


# ── 表情包含义：让她真的懂这张图在说什么 ──────────────
# 之前只存 tags 和 note，她对图的意思一无所知，选图全凭运气。
_MEAN_PROMPT = (
    "这是一张聊天表情包。只回答它传达的情绪或态度，用一个词或极短短语。"
    "比如：无语、嘲讽、服了、求饶、装可怜、得意、敷衍、凑热闹、震惊、嫌弃。"
    "不要描述画面里有什么，不要写句子，不要解释。只给那一个词。"
)


_MOOD_WORDS = ("无语", "嘲讽", "服了", "求饶", "装可怜", "得意", "敷衍", "凑热闹",
               "震惊", "嫌弃", "开心", "难过", "生气", "害羞", "尴尬", "委屈",
               "疑问", "赞同", "拒绝", "鼓励", "挑衅", "撒娇", "摆烂", "期待")


def _to_mood(text):
    """把模型可能给出的描述句，压成一个情绪词。"""
    t = str(text or "").strip()
    if not t:
        return ""
    # 先找已知情绪词，出现得越早越可能是主线
    for w in _MOOD_WORDS:
        if w in t:
            return w
    # 再找「表示/表达/意思是/是在」后面的那段
    m = re.search(r"(?:表示|表达|意思是|是在|代表)\s*([^，,。；;、\s]{2,8})", t)
    if m:
        return m.group(1)
    # 最后退回最短分句
    for seg in re.split(r"[，,。；;、\s]+", t):
        seg = seg.strip()
        if 2 <= len(seg) <= 8:
            return seg
    return t[:8]


async def describe_meaning(image_path):
    """让视觉模型看一张表情包，给出一句含义。"""
    try:
        import base64
        import llm_router
        p = str(image_path or "")
        if not os.path.isfile(p):
            return ""
        with open(p, "rb") as _f:
            raw = _f.read()
        if len(raw) < 2000 or len(raw) > 8 * 1024 * 1024:
            return ""
        low = p.lower()
        ext = "png" if low.endswith(".png") else ("gif" if low.endswith(".gif") else "jpeg")
        durl = "data:image/" + ext + ";base64," + base64.b64encode(raw).decode("ascii")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": _MEAN_PROMPT},
            {"type": "image_url", "image_url": {"url": durl}},
        ]}]
        got = await llm_router.call_vision(msgs, max_tokens=90, timeout=60)
        t = str(got or "").strip()
        for ch in ("“", "”", "。", ".", "、", ",", " "):
            t = t.strip(ch)
        return _to_mood(t)
    except Exception as e:
        print("[表情] 含义识别失败 " + type(e).__name__, flush=True)
        return ""


_MEANING_RANK = {"human": 3, "chat": 2, "vision": 1, "": 0}


def set_meaning(sid, meaning, source="vision"):
    """给某张表情包写入含义。出处有三档，优先级是定死的：

        human（主人在面板校准的）> chat（群友教她的）> vision（她自己看图猜的）

    以前群里有人甩一句「这表情是XX」也写 source="human"，于是任何群友都能
    把主人亲手校准过的那条盖掉 —— 「谁教的」和「谁定的」必须分成两档。
    空含义也算一次人工判断：「这张没有固定意思」比留着一个错的总结有用。
    """
    idx = _load_index()
    rec = idx.get(str(sid))
    if not rec:
        return False
    m = str(meaning or "").strip()[:80]
    if not m and source != "human":
        return False
    old = str(rec.get("meaning_src") or "")
    if _MEANING_RANK.get(old, 0) > _MEANING_RANK.get(source, 0):
        return False
    rec["meaning"] = m
    rec["meaning_src"] = source
    rec["meaning_ts"] = time.time()
    _save_index(idx)
    who = {"human": "主人校准", "chat": "群友教的", "vision": "她自己看的"}.get(source, source)
    print("[表情] 含义已记 %s = %s（%s）" % (str(sid)[:12], m or "（判定：无固定含义）", who),
          flush=True)
    return True


def catalog_for_prompt(limit=24, chars=560):
    """她选表情包时看到的那份清单：每张一行「短 id + 什么意思 + 谁定的」。

    以前 `meaning` 只有人写、没人读 —— 她发图走的是 `pick_random()`，
    主人就算把含义改成对的，她下一次照样随机抽一张。校准要有用，
    就得让她在挑的时候真的读到这份表。
    主人校准过的那批排在前面，并且标「你校准过」，让她知道哪条可信。
    """
    idx = _load_index()
    rows = []
    labeled = 0
    rank = _MEANING_RANK
    for sid, rec in idx.items():
        if not isinstance(rec, dict):
            continue
        m = str(rec.get("meaning") or "").strip()
        if not m:
            continue
        labeled += 1
        src = str(rec.get("meaning_src") or "")
        # 主人校准的排最前，别人教过的其次，她自己猜的最后 —— 可信度是分层的
        rows.append((-rank.get(src, 0), -int(rec.get("used") or 0), sid, m,
                     "、".join(str(t) for t in (rec.get("tags") or [])[:2]), src))
    rows.sort()
    who = {"human": "（主人校准过，可信）", "chat": "（有人教过她）"}
    lines, used = [], 0
    for _r, _u, sid, m, tags, src in rows[:max(1, int(limit))]:
        line = "- %s：%s%s%s" % (sid[:8], m, who.get(src, ""), "｜" + tags if tags else "")
        if used + len(line) > chars:
            break
        lines.append(line)
        used += len(line) + 1
    return {"text": "\n".join(lines), "labeled": labeled,
            "shown": len(lines), "total": len(idx)}


def meaning_for_bytes(raw):
    """别人甩来一张图：命中她库里某张的内容指纹，就用那条含义（人工优先）。

    她没有「这张我存过、主人告诉我它是什么意思」的联想能力 ——
    不看指纹就只能当场再猜一次，猜错了还要主人再校准一遍。
    """
    try:
        h = _content_hash(raw)
    except Exception:
        return {}
    for rec in (_load_index() or {}).values():
        if not isinstance(rec, dict) or rec.get("hash") != h:
            continue
        m = str(rec.get("meaning") or "").strip()
        if not m:
            return {}
        return {"id": str(rec.get("id") or ""), "meaning": m,
                "src": str(rec.get("meaning_src") or "")}
    return {}


def meaning_for_src(src):
    """按图片地址找库里那张的含义 —— 回复时不该再去下载一遍算指纹。"""
    url = str(src or '').strip()
    if not url:
        return {}
    for rec in (_load_index() or {}).values():
        if not isinstance(rec, dict):
            continue
        if str(rec.get('src') or '').strip() != url:
            continue
        meaning = str(rec.get('meaning') or '').strip()
        if not meaning:
            return {}
        return {'id': str(rec.get('id') or ''), 'meaning': meaning,
                'src': str(rec.get('meaning_src') or '')}
    return {}


def meaning_for_sha(sha):
    """按内容指纹找含义 —— QQ 每次给的新地址也能对上同一张图。"""
    value = str(sha or '').strip()
    if not value:
        return {}
    for rec in (_load_index() or {}).values():
        if not isinstance(rec, dict) or str(rec.get('hash') or '') != value:
            continue
        meaning = str(rec.get('meaning') or '').strip()
        if not meaning:
            return {}
        return {'id': str(rec.get('id') or ''), 'meaning': meaning,
                'src': str(rec.get('meaning_src') or '')}
    return {}


def meaning_for_id(sid):
    rec = (_load_index() or {}).get(str(sid or '').strip())
    if not isinstance(rec, dict):
        return {}
    meaning = str(rec.get('meaning') or '').strip()
    return ({'id': str(rec.get('id') or ''), 'meaning': meaning,
             'src': str(rec.get('meaning_src') or '')} if meaning else {})


def meaning_of(rec):
    """取一张表情包的含义（没有就返回空）。"""
    if not isinstance(rec, dict):
        return ""
    return str(rec.get("meaning") or "").strip()


def describe_hint(rec):
    """给她看的提示：这张图是什么意思。"""
    m = meaning_of(rec)
    if not m:
        return ""
    return "这张表情包的意思是：" + m + "。"


def find_by_hash(raw_or_url_path):
    """按内容找已收藏的表情包（用于认出「这张是她库里的」）。

    指纹必须是 `_content_hash`：库里每条记录的 hash 都是它（sha1 全字节）。
    以前这里比的是另一套「md5(前 4KB + 长度)」，两边永远对不上，函数看起来有返回值
    实际永远是 None —— 一条库不能有两种指纹。
    """
    try:
        p = str(raw_or_url_path or "")
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            h = _content_hash(f.read())
    except Exception:
        return None
    for rec in (_load_index() or {}).values():
        if rec.get("hash") == h:
            return rec
    return None


def library_meanings(limit=12):
    """返回库里带含义的表情包清单，供她挑选时参考。"""
    out = []
    for rec in (_load_index() or {}).values():
        m = str(rec.get("meaning") or "").strip()
        if m:
            out.append({"id": rec.get("id"), "meaning": m[:40],
                        "tags": rec.get("tags") or []})
    return out[:limit]


def search(tag_kw=None) -> list:
    idx = _load_index()
    items = list(idx.values())
    if tag_kw:
        kw = str(tag_kw).strip()
        if kw:
            items = [r for r in items
                     if any(kw in t for t in r.get("tags", [])) or kw in (r.get("note", ""))]
    return items


def pick_by_sid(prefix) -> Optional[dict]:
    """按主人/她给的短 id 取一张（前缀唯一才算）。

    面板和提示词里露出的是 id 前 8 位，所以这里必须按前缀解析；
    真有撞车的短前缀就当没找到 —— 宁可随机，也不要发错一张她其实不想发的图。
    """
    want = re.sub(r"[^0-9a-fA-F]", "", str(prefix or ""))[:12].lower()
    if not want:
        return None
    idx = _load_index()
    hits = [rec for sid, rec in idx.items() if str(sid).lower().startswith(want)]
    if len(hits) != 1:
        return None
    return dict(hits[0])


def pick_random(tag_kw=None) -> Optional[dict]:
    items = search(tag_kw)
    if not items:
        return None
    return random.choice(items)


def bump_used(sid: str):
    idx = _load_index()
    if sid in idx:
        idx[sid]["used"] = idx[sid].get("used", 0) + 1
        _save_index(idx)


def delete_sticker(sid: str, to_trash=True) -> dict:
    """人工筛选掉一张表情：从索引里摘掉，文件移进回收而不是直接删。

    主人要的是「我要人工筛选」，不是「给我一把不留后路的刀」：
    看走眼删掉一张常用的梗图，成本应该是能捞回来，而不是让她从此少一张斗图库。
    所以她下次抽不到（索引没了）就够了，文件先进 `data/stickers_trash`。
    只认索引里存在的 id，并且要确认文件真的还在库目录内 ——
    面板传进来的 id 不该有机会删到库外的东西。
    """
    idx = _load_index()
    rec = idx.pop(str(sid), None)
    if not isinstance(rec, dict):
        return {"ok": False, "reason": "库里没有这个 id"}
    name = str(rec.get("file") or "")
    src = (STICKER_DIR / name) if name else None
    moved = ""
    try:
        root = STICKER_DIR.resolve()
        if src is not None and src.is_file() and src.resolve().is_relative_to(root):
            if to_trash:
                trash = _trash_dir()
                dst = trash / ("%d_%s" % (int(time.time()), src.name))
                shutil.move(str(src), str(dst))
                moved = str(dst)
                _trim_trash(trash)
            else:
                src.unlink(missing_ok=True)
    except Exception as e:
        print("[表情] 删除时文件没处理好 %s: %s" % (type(e).__name__, str(e)[:60]), flush=True)
    _save_index(idx)
    print("[表情] 人工删掉 %s%s" % (str(sid)[:12],
                                    "（已移进回收）" if moved else ""), flush=True)
    return {"ok": True, "id": str(sid), "trashed": moved,
            "file": name if not moved and src is not None else ""}


def _trash_dir() -> Path:
    trash = Path(STICKER_DIR).parent / "stickers_trash"
    trash.mkdir(parents=True, exist_ok=True)
    return trash


def _trim_trash(trash: Path):
    """回收也要有上限：人工筛选一天点几十次，盘不该无限吃图。"""
    try:
        files = sorted((p for p in trash.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime)
        for old in files[:max(0, len(files) - TRASH_KEEP)]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def all_stickers(limit=300) -> list:
    """面板用：列出她的斗图库（不返回绝对路径，按「用得多的」在前）。

    合并以后这一个库就是全部来源，所以把 `src` 带上：
    有 src 的是从 QQ 收藏搬来的，没有的是群里收的或她/主人手动存的。
    `meaning_src` 是给校准面板看的：`human` = 主人改过，`vision` = 她自己看的。
    """
    idx = _load_index()
    out = []
    for sid, r in idx.items():
        if not isinstance(r, dict):
            continue
        out.append({"id": sid, "tags": r.get("tags") or [],
                    "meaning": r.get("meaning") or "",
                    "meaning_src": str(r.get("meaning_src") or ""),
                    "meaning_ts": float(r.get("meaning_ts") or 0),
                    "note": r.get("note") or "",
                    "used": int(r.get("used") or 0),
                    "added": int(r.get("added") or 0),
                    "bytes": int(r.get("bytes") or 0),
                    "dims": r.get("dims") or [],
                    "kind": r.get("kind") or "",
                    "src": str(r.get("src") or "")})
    out.sort(key=lambda x: (-x["used"], -x["added"]))
    return out[:max(1, int(limit))]


def sticker_path(sid) -> str:
    """面板取图用。只认索引里存在的 id，并确认路径在库目录内。

    直接拼 `STICKER_DIR / 用户传来的名字` 就是一个路径穿越洞
    （../../config.env）。所以先查索引，再对 resolved 路径做前缀校验。
    """
    idx = _load_index()
    rec = idx.get(str(sid))
    if not isinstance(rec, dict):
        return ""
    try:
        p = Path(str(rec.get("abs") or "")).resolve()
        root = STICKER_DIR.resolve()
        if not str(p).startswith(str(root)):
            return ""
        if not p.is_file():
            return ""
        return str(p)
    except Exception:
        return ""


def stats() -> dict:
    idx = _load_index()
    all_tags = {}
    for r in idx.values():
        for t in r.get("tags", []):
            all_tags[t] = all_tags.get(t, 0) + 1
    trash = Path(STICKER_DIR).parent / "stickers_trash"
    return {
        "dir": str(STICKER_DIR.resolve()),
        "exists": STICKER_DIR.exists(),
        "count": len(idx),
        "max": MAX_STICKERS,
        "tags": all_tags,
        # 面板要说清口径：库里多少张、其中主人校准过几张、回收里还留着几张
        "calibrated": len([r for r in idx.values()
                           if isinstance(r, dict)
                           and str(r.get("meaning_src") or "") == "human"]),
        "meaning_known": len([r for r in idx.values()
                              if isinstance(r, dict) and str(r.get("meaning") or "").strip()]),
        "trash": len([p for p in trash.iterdir() if p.is_file()]) if trash.is_dir() else 0,
        "trash_keep": TRASH_KEEP,
    }




def qq_face_urls():
    """读 bot 同步下来的 QQ 收藏表情地址列表（web 侧只读文件，不开 WS）。"""
    p = Path(os.getenv('DATA_DIR', './data')) / 'qq_faces.json'
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []
    urls = data.get('urls') if isinstance(data, dict) else None
    return [str(u) for u in (urls or []) if isinstance(u, str) and u.startswith('http')]


_FACE_CACHE = {}
_FACE_CACHE_MAX = 24


async def fetch_qq_face(url):
    """替浏览器去取一张收藏表情。

    p.qpic.cn 对带 Referer 的跨站请求会拒，而且面板走 https 隧道，
    直连 QQ CDN 在浏览器里就是一个个坏图。所以图片一律由服务端取回来再转发。
    只做 GET，不发送任何 QQ 凭证。
    """
    u = str(url or "").strip()
    if not u.startswith("http"):
        return None, "地址不对"
    hit = _FACE_CACHE.get(u)
    if hit:
        return hit[0], hit[1]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                     headers={"User-Agent":
                                              "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as c:
            r = await c.get(u)
        if r.status_code != 200:
            return None, "HTTP%d" % r.status_code
        body = r.content[:4 * 1024 * 1024]
        ctype = str(r.headers.get("content-type") or "image/gif").split(";")[0]
        if not ctype.startswith("image/"):
            return None, "不是图片（%s）" % ctype
        if len(_FACE_CACHE) >= _FACE_CACHE_MAX:
            _FACE_CACHE.pop(next(iter(_FACE_CACHE)), None)
        _FACE_CACHE[u] = (body, ctype)
        return body, ctype
    except Exception as e:
        return None, type(e).__name__


# ============================================================
# QQ 原生收藏表情（用户截图那种）：从 NapCat fetch_custom_face 拉取并缓存
# ============================================================

_qqface_cache = {"urls": [], "ts": 0.0}
QQFACE_TTL = float(os.getenv("QQFACE_CACHE_TTL", "600"))


async def refresh_qq_native_faces(count=40):
    """调用 NapCat fetch_custom_face 拉取用户 QQ 收藏的表情包 URL 列表，缓存。"""
    try:
        import napcat_channel
        if not napcat_channel.channel.connected:
            return []
        resp = await napcat_channel.channel.call("fetch_custom_face", {"count": int(count)}, timeout=20.0)
        urls = (resp or {}).get("data") or []
        urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        _qqface_cache["urls"] = urls
        _qqface_cache["ts"] = time.time()
        # 落盘给 Web 面板读取
        try:
            cache_path = Path(os.getenv('DATA_DIR', './data')) / 'qq_faces.json'
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'urls': urls, 'ts': time.time()}, ensure_ascii=False), encoding='utf-8')
            tmp.replace(cache_path)
        except Exception:
            pass
        print("[斗图] 同步到 %d 张 QQ 收藏表情" % len(urls), flush=True)
        # 同步完顺手并进斗图库 —— 合并必须是自动的，否则两边各发各的照旧。
        # 一次最多搬 12 张，别让这一步把发送链路堵住的太久。
        try:
            await import_qq_faces(urls)
        except Exception as e:
            print("[斗图] 收藏并入库失败 %s" % type(e).__name__, flush=True)
        return urls
    except Exception as e:
        print("[斗图] fetch_custom_face 失败: %s" % e, flush=True)
        return []


async def get_qq_native_faces(force=False):
    """取缓存的 QQ 收藏表情；过期或空则刷新。"""
    now = time.time()
    if force or not _qqface_cache["urls"] or (now - _qqface_cache["ts"]) > QQFACE_TTL:
        await refresh_qq_native_faces()
    return _qqface_cache["urls"]


# ── QQ 收藏 → 她的斗图库 ────────────────────────────────
# 用户的话：「QQ 收藏表情和她的斗图库都是她发表情的内容，没有必要分开」。
# 以前是两套：库里的是本地文件，收藏的是 CDN 地址，两个入口排序还相反
# （/斗图 优先收藏、自动补发优先本地），于是「她发的表情」到底从哪来没人说得清。
# 现在收藏被当普通来源收进同一个库：同一份去重、同一份用量统计、同一个发送池。
# 每张都记 src，下次刷新只补没见过的，不会反复重下。
QQ_FACE_TAG = "QQ收藏"


async def import_qq_faces(urls=None, limit=12, with_meaning=False):
    """把 QQ 收藏表情收进本地斗图库。返回 {checked,new,dup,failed}。"""
    if not urls:
        urls = qq_face_urls()
    urls = [str(u) for u in (urls or []) if str(u).startswith("http")][:max(0, int(limit))]
    if not urls:
        return {"checked": 0, "new": 0, "dup": 0, "failed": 0}
    have = {str(r.get("src") or "") for r in _load_index(ttl=0.0).values()}
    out = {"checked": 0, "new": 0, "dup": 0, "failed": 0}
    for url in urls:
        if url in have:
            continue
        out["checked"] += 1
        raw, err = await fetch_qq_face(url)
        if not raw:
            out["failed"] += 1
            print("[斗图] 收藏这张取不回来 %s：%s" % (url[:40], err), flush=True)
            continue
        # 收藏是她自己挑的，本来就是表情，所以不过「像不像照片」那道门（那道门管的是
        # 群里来路不明的图）。含义这里不顺手识别：一次刷新十几张，每张一个视觉请求
        # 太贵，等她真的用到或有人教再说。
        rec = await _save_raw(raw, "image/" + (_dims(raw)[2] or "gif"),
                              tags=[QQ_FACE_TAG], note="从 QQ 收藏搬来的",
                              force=True, src=url, with_meaning=with_meaning)
        if rec is None:
            out["failed"] += 1
        elif rec.get("deduped"):
            out["dup"] += 1
        else:
            out["new"] += 1
    if out["checked"]:
        print("[斗图] 收藏并入库：新增 %d，已有 %d，失败 %d"
              % (out["new"], out["dup"], out["failed"]), flush=True)
    return out


async def pick_qq_native_face(keyword=None):
    """随机挑一张 QQ 收藏表情 URL。"""
    urls = await get_qq_native_faces()
    if not urls:
        return None
    return random.choice(urls)

# —— 聊天指令解析 ——
RE_SAVE = re.compile(r"/收藏表情|/存表情|加斗图", re.I)
RE_SEND = re.compile(r"/斗图(?:\s+(.*))?", re.I)
RE_LIST = re.compile(r"/表情包列表|/斗图列表", re.I)
RE_DEL = re.compile(r"/删表情\s+(\S+)", re.I)


def parse_command(text: str):
    """返回 (kind, arg)。kind in {save,send,list,del,None}。"""
    t = (text or "").strip()
    if RE_SAVE.search(t):
        # 标签可跟在命令后：/收藏表情 猫 惊讶
        after = RE_SAVE.sub("", t, count=1).strip()
        return ("save", after)
    if RE_LIST.search(t):
        return ("list", "")
    m = RE_DEL.search(t)
    if m:
        return ("del", m.group(1))
    m = RE_SEND.search(t)
    if m:
        return ("send", (m.group(1) or "").strip())
    return (None, None)