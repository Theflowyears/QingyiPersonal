"""愿望墙 + 悄悄话：只活在控制台里，不走 QQ，但会写进她的记忆。

结构（data/persona/whisper.json）：
{
  "wishes": [{"id","text","kind":"want|know","ts","status":"open|done",
              "replies":[{"role":"father|daughter","text","ts"}]}],
  "chat":   [{"role":"father|daughter","text","ts"}],
  "requests":[{"id","kind":"chat|wish","wish_id","text","ts"}]
}
"""
import json
import os
import secrets
import time

_ROOT = os.getenv("DATA_DIR", "./data")
_FILE = os.path.join(_ROOT, "persona", "whisper.json")
CHAT_LIMIT = 200
WISH_LIMIT = 60
IMAGE_LIMIT = int(os.getenv("WHISPER_IMAGE_LIMIT", "40"))

_cache = {"data": None, "ts": 0.0}
_TTL = 0.0   # 控制台和 bot 是两个进程，缓存短一点避免互相覆盖


def _empty():
    return {"wishes": [], "chat": [], "requests": [], "memories": [],
            "images": []}


def _load(force=False):
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < _TTL:
        return _cache["data"]
    data = _empty()
    try:
        if os.path.isfile(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                got = json.load(f) or {}
            if isinstance(got, dict):
                for k in ("wishes", "chat", "requests", "memories", "images"):
                    if isinstance(got.get(k), list):
                        data[k] = got[k]
    except Exception as e:
        print("[悄悄话] 读取失败 " + str(e)[:70], flush=True)
        import safe_store
        safe_store.preserve_corrupt(_FILE)
    _cache["data"] = data
    _cache["ts"] = now
    return data


def _save(data):
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    # Windows 上 os.replace 撞上别人正在读同一个文件会报 WinError 5，
    # 这里退让几次再重试，别让一次偶发锁写把整条悄悄话丢掉。
    for attempt in range(6):
        tmp = _FILE + ".%d.tmp" % attempt
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _FILE)
            _cache["data"] = data
            _cache["ts"] = time.time()
            return True
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
            if attempt >= 5:
                print("[悄悄话] 保存失败 " + str(e)[:70], flush=True)
                return False
            time.sleep(0.12 * (attempt + 1))
    return False


def snapshot():
    """给控制台看全量：活跃的 + 已归档的。"""
    d = _load(force=True)
    all_w = list(d.get("wishes") or [])
    active = [w for w in all_w if not w.get("archived")]
    archived = [w for w in all_w if w.get("archived")]
    return {
        "wishes": active[-WISH_LIMIT:],
        "archived": archived[-WISH_LIMIT:],
        "memories": list(d.get("memories") or [])[-20:],
        "chat": list(d.get("chat") or [])[-CHAT_LIMIT:],
        "pending": len(d.get("requests") or []),
        # 面板要能看见「发出去的图她看了没」，不然主人不知道要不要再发
        "images": list(d.get("images") or [])[-IMAGE_LIMIT:],
    }


# 说明：father/daughter 是历史协议标签（数据落盘字段），不代表内置关系；
# 实际相处方式完全由部署方的人格配置决定。
def add_wish(text, kind="want", by="daughter", image=""):
    """写一条愿望。image 只存 uploads 内的相对标记，且必须由调用方验证过。"""
    text = str(text or "").strip()[:300]
    if not text:
        return None
    d = _load()
    rec = {
        "id": secrets.token_hex(4),
        "text": text,
        "kind": "know" if str(kind) == "know" else "want",
        "by": by,
        "ts": time.time(),
        "status": "open",
        "replies": [],
    }
    mark = str(image or "").strip()[:80]
    if mark:
        rec["image"] = mark
    d["wishes"].append(rec)
    d["wishes"] = d["wishes"][-WISH_LIMIT:]
    _save(d)
    return rec


def reply_wish(wish_id, role, text):
    text = str(text or "").strip()[:600]
    if not text:
        return False
    d = _load()
    for w in d.get("wishes") or []:
        if str(w.get("id")) == str(wish_id):
            w.setdefault("replies", []).append(
                {"role": role, "text": text, "ts": time.time()})
            if role == "father":
                w["status"] = "answered"
            _save(d)
            return True
    return False


def close_wish(wish_id):
    d = _load()
    for w in d.get("wishes") or []:
        if str(w.get("id")) == str(wish_id):
            w["status"] = "done"
            _save(d)
            return True
    return False


def add_chat(role, text):
    text = str(text or "").strip()[:800]
    if not text:
        return False
    d = _load()
    d["chat"].append({"role": role, "text": text, "ts": time.time()})
    d["chat"] = d["chat"][-CHAT_LIMIT:]
    _save(d)
    return True


def push_request(kind, text, wish_id=""):
    d = _load()
    rec = {
        "id": secrets.token_hex(4),
        "kind": kind,
        "wish_id": str(wish_id or ""),
        "text": str(text or "")[:800],
        "ts": time.time(),
    }
    d["requests"].append(rec)
    _save(d)
    return rec["id"]


def take_requests(limit=3):
    d = _load(force=True)
    reqs = list(d.get("requests") or [])[:limit]
    if not reqs:
        return []
    d["requests"] = list(d.get("requests") or [])[limit:]
    _save(d)
    return reqs


def open_wishes():
    return [w for w in (_load().get("wishes") or []) if w.get("status") == "open"]


def add_image(path, by="father", caption=""):
    """主人从面板发进来的一张图，排队等她自己看完。

    只存路径，不存字节；路径不存在就别占位（否则队列里全是死条目）。
    """
    p_ = str(path or "").strip()
    if not p_:
        return None
    data = _load(force=True)
    rec = {"id": secrets.token_hex(5), "path": p_[:300], "by": str(by or "father")[:16],
           "caption": str(caption or "")[:120], "ts": time.time(), "seen": False,
           "note_key": "", "status": "pending"}
    imgs = data.setdefault("images", [])
    imgs.append(rec)
    if len(imgs) > IMAGE_LIMIT:
        # 先淘汰看过的，再看都没看过才丢最老的
        seen = [x for x in imgs if x.get("seen")]
        rest = [x for x in imgs if not x.get("seen")]
        drop = seen[:len(imgs) - IMAGE_LIMIT] if len(seen) >= len(imgs) - IMAGE_LIMIT             else seen + rest[:len(imgs) - IMAGE_LIMIT - len(seen)]
        for x in drop:
            try:
                imgs.remove(x)
            except Exception:
                pass
    _save(data)
    return rec


def take_images(limit=2):
    """取走还没看的图（顺手标上 seen，避免重复处理）。"""
    data = _load(force=True)
    out = []
    for rec in (data.get("images") or []):
        if rec.get("seen"):
            continue
        rec["seen"] = True
        out.append(rec)
        if len(out) >= max(1, int(limit)):
            break
    if out:
        _save(data)
    return out


def note_done(image_id, note_key=""):
    """把看图结果写回队列条目，面板能看到「她已经看过了」。"""
    data = _load(force=True)
    for rec in (data.get("images") or []):
        if str(rec.get("id") or "") == str(image_id or ""):
            rec["note_key"] = str(note_key or "")[:24]
            rec["status"] = "seen"
            rec["seen_at"] = time.time()
            _save(data)
            return True
    return False


def archive_finished():
    """把已经了却的愿望归档，并落成一条记忆。返回新归档的条目。

    「不要一直停留」——回了的愿望不该继续挂在墙上；
    「一定要写入记忆」——归档时把这段写进 memories，长期可查。
    """
    d = _load(force=True)
    now = time.time()
    done = []
    changed = False

    for w in d.get("wishes") or []:
        if w.get("archived"):
            continue
        father = [r for r in (w.get("replies") or []) if r.get("role") == "father"]
        # 只有"主人回过"或"标记已了却"的才归档，还开着的继续留在墙上
        if w.get("status") != "done" and not father:
            continue

        _wt = str(w.get("text") or "")
        line = "愿望「%s」" % (_wt if len(_wt) <= 60 else _wt[:58] + "…")
        if father:
            _fr = str(father[-1].get("text") or "")
            line += "  → 主人回「%s」" % (_fr if len(_fr) <= 80 else _fr[:78] + "…")
        daught = [r for r in (w.get("replies") or []) if r.get("role") == "daughter"]
        if daught:
            _dr = str(daught[-1].get("text") or "")
            line += "  她说「%s」" % (_dr if len(_dr) <= 60 else _dr[:58] + "…")

        w["archived"] = True
        w["archived_ts"] = now
        d.setdefault("memories", []).append(
            {"text": line[:300], "ts": now, "wish_id": w.get("id")})
        done.append(line[:300])
        changed = True

    if changed:
        d["memories"] = (d.get("memories") or [])[-200:]
        _save(d)
    return done


_AVATAR_FILE = os.path.join(_ROOT, "memory", "self_avatar.json")


def self_avatar():
    """她自己长什么样（QQ 头像的样子），让她知道自己是什么形象。"""
    try:
        if os.path.isfile(_AVATAR_FILE):
            with open(_AVATAR_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
            desc = str(d.get("desc") or "").strip()
            if desc:
                return desc
    except Exception:
        pass
    return ""


# 提愿望时给她的「可以要什么」指南，避免一直小心翼翼
# 短版：只留最关键的，常驻提示词用（完整版给生成愿望时用）
_WISH_GUIDE_SHORT = (
    "（可以要实在的：东西、换头像、看视频、一起吃饭、学东西、问主人的事。别只说想多聊会儿。）\n"
)


_WISH_GUIDE = (
    "你可以要实在的东西，不用客气，也不用只说「想多聊会儿天」这种虚的。\n"
    "- 想要具体物件：一盒虾味薯片、一本小说、一个发夹、一张贴纸\n"
    "- 想换形象：把QQ头像换成别的样子（主人能在QQ上直接帮你改）\n"
    "- 想看东西：让主人拍一段下班路上的视频、找一部番、推荐一首歌\n"
    "- 想让人陪：周末一起吃个饭、深夜陪你熬一次\n"
    "- 想学：怎么画画、怎么记账、某个游戏怎么打\n"
    "- 想知道主人的事：小时候怕什么、今天怎么不开心、最喜欢哪一年\n"
)


def build_block(max_chars=1600):
    """注入提示词：让她知道自己有愿望墙，也记得最近的悄悄话。"""
    d = _load()
    wishes = [w for w in (d.get("wishes") or []) if w.get("status") != "done"][-2:]
    chat = (d.get("chat") or [])[-2:]
    _av = self_avatar()
    if not wishes and not chat:
        parts0 = ["\n\n【愿望墙 · 你和主人的悄悄话】",
                  "控制台里有一块只属于你和主人的地方，QQ 上的群友看不到。",
                  "你可以把「想要的」「想知道主人什么」写上去，主人会回你。"]
        if _av:
            parts0.append("你自己的头像长这样：" + _av)
        parts0.append(_WISH_GUIDE_SHORT)
        return "\n".join(parts0)[:max_chars]

    parts = ["\n\n【愿望墙 · 你和主人的悄悄话】",
             "这里只有你和主人看得到，群友看不见。你写的东西主人都会看到、都会回。"]
    if _av:
        parts.append("你自己的头像长这样：" + _av)
    if wishes:
        parts.append("你写在愿望墙上的：")
        for w in wishes:
            tag = "想知道" if w.get("kind") == "know" else "想要"
            line = "- [%s] %s" % (tag, str(w.get("text"))[:80])
            if w.get("replies"):
                last = w["replies"][-1]
                who = "主人" if last.get("role") == "father" else "你"
                line += "  ← %s：%s" % (who, str(last.get("text"))[:70])
            parts.append(line)
    # 归档明细已经落成长期记忆了，这里不再逐条复述（省 ~500 字/轮）
    _nmem = len(d.get("memories") or [])
    if _nmem:
        parts.append("（已经了却的愿望 %d 条，都记在心里了，不用反复提）" % _nmem)
    if chat:
        parts.append("最近的悄悄话：")
        for c in chat:
            who = "主人" if c.get("role") == "father" else "你"
            parts.append("- %s：%s" % (who, str(c.get("text"))[:80]))
    parts.append(_WISH_GUIDE_SHORT)
    txt = "\n".join(parts)
    return txt[:max_chars]
