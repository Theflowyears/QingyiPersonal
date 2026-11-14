"""看图笔记：把「看懂了」和「说不说」拆开。

取证结论：她识图差不是模型的锅，是规则的锅 ——
  · `_img_reaction_hint` 明写「别解释图、别拆画面」，轮换风格里还有一条
    「一个字都不许提图和画」；
  · `_describe_image` 只要「不超过18个字说明这张图是什么表情包/什么情绪」，
    max_tokens=40 —— 那是给梗图分类，不是识图；
  · `_scrub_img_opening/_scrub_img_explain/_scrub_img_lazy` 是一整面历史失败话术黑名单
    （没看懂/图太小/糊成一团/懒得看），她连「这张我看不清」都说不出口。

所以这里存一份**结构化**看图笔记（物体 / 图上文字 / 场景 / 动作 / 情绪 / 可能的梗），
上层再按场合决定说多少：
  · 群聊、梗图：只给「这是什么情绪/梗」，不给物体清单 —— 有清单她就会照着念；
  · 私聊、被明确要求「看看这是什么」：给全，允许推断，也允许说看不清；
  · sensitive（主人桌面、饭菜这类私人照片）：永不进群聊上下文。

按已定口径：**不存原图字节**，只存笔记。代价如实说明：NapCat 的文件 token 会过期，
所以笔记必须在看到的那一刻生成；事后无法回看原图。
"""
import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

def _file():
    """同 memory_usage：路径必须调用时算，别在 import 时冻住。"""
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory",
                        "vision_notes.json")
_LOCK_PATH_NOTE = "笔记数量上限，超了按最久没用淘汰（笔记很短，2000 条也就几百 KB）"
MAX_NOTES = int(os.getenv("VISION_NOTES_MAX", "2000"))

_cache = {"sig": None, "data": {}}

FIELDS = ("objects", "text_in_image", "scene", "people_action", "mood", "meme_guess")

# 结构化看图提示词。要点：
#   1) 要结构，不要「一句话描述」—— 一句话描述只能拿来分类梗图；
#   2) 允许说「看不清」并说清哪儿看不清 —— 硬编比承认看不清更不像人；
#   3) 不再有「不许提图」这类禁令，那是发送阶段的社交规则，不该污染看的过程。
#   4) 线上实测：六字段 + 长描述经常被截断（max_tokens 400 用完），或被模型
#      加上解释 —— 解析一失败整条笔记就没了。所以格式压到"一行一个短句"，
#      并允许截断修复（见 parse_note）。
VISION_PROMPT = (
    "看这张图，只输出一行 JSON，不要解释、不要 markdown、不要道歉：\n"
    '{"kind":"表情包/动画表情/照片/截图/插画/其他 里挑一个",'
    '"intent":"发图的人想表达什么、想要什么回应，一句话",'
    '"mood":"发图人的情绪，一两个词",'
    '"text_in_image":"图上的字，没有就空串",'
    '"objects":"只有照片、截图才列 2 到 5 个东西，表情包留空数组"}\n'
    "要写的是「这张图在说什么」，不是「图里画了什么」：\n"
    "没人想在聊天里听人解说一张表情包，所以表情包别填 objects。"
    "intent 看不清就留空串，不要编。"
)


_SYMBOL_WORDS = ("表情包", "动画表情", "梗图", "表情", "meme", "sticker", "emoji")
_RECORD_WORDS = ("照片", "截图", "插画", "实拍", "photo", "screenshot", "artwork")


def _is_symbol_image(note):
    """这张图是「情绪符号」还是「信息载体」。

    符号（表情包）只要读懂意思就能接，传原图反而勾着她解说画面；
    载体（照片/截图/插画）里装着对方要给她看的内容，不传原图就是让她瞎猜。
    kind 是新生成的笔记才有的字段，老笔记退回 scene 与 meme_guess 判。
    """
    if not isinstance(note, dict):
        return False
    kind = str(note.get("kind") or "").strip().lower()
    if kind:
        if any(w.lower() in kind for w in _SYMBOL_WORDS):
            return True
        if any(w.lower() in kind for w in _RECORD_WORDS):
            return False
    scene = str(note.get("scene") or "")
    if any(w in scene for w in _SYMBOL_WORDS):
        return True
    return bool(str(note.get("meme_guess") or "").strip())


MEANING_RULE = ("回应对方发这张图的意思和情绪就好："
                "不要描述画面、不要列图里有什么、不要拿画面对比自己，也不要说「这张图」。")
# 校准过的含义是「让你听懂」，不是「让你照着念」。主人原话：校准含义是为了
# 读懂表情包再按背后的意思回话，不是拿这段描述当台词背出去。
MEANING_RULE_V2 = (MEANING_RULE + "下面这段是给你听懂用的：不要照念、不要复述，"
                   "用你自己的话接住它的意思就行。")


def calibrated_card(meaning, budget=160):
    text = str(meaning or "").strip()
    if not text:
        return ""
    return "【这张图的意思】" + text[:budget] + "\n" + MEANING_RULE_V2


def meme_card(note, budget=160):
    """表情包的「含义卡」：看得懂意思时，这就是她在这一轮看到的全部。

    主人的原话：「读图是为了分析图片的含义和可能包含的情感，然后做出对背后
    含义的回复，而不是让你描述图片本身的情况。」只要原图进了请求，模型就忍不住
    报菜名（「吃西瓜还带角，看着就热」），所以能靠含义接的那一轮干脆不给像素，
    顺带省掉一张几 MB 的 base64。
    看不懂（没有 intent/mood）或不是符号图 → 返回空串，交回原图。
    """
    if not isinstance(note, dict) or not _is_symbol_image(note):
        return ""
    intent = str(note.get("intent") or "").strip() or str(note.get("meme_guess") or "").strip()
    mood = str(note.get("mood") or "").strip()
    if intent and not meaning_like(intent):
        # 记下的只是画面描述。不要把它当意思喂回去，否则她一定照着念。
        intent = ""
    if mood and not meaning_like(mood):
        mood = ""
    if not (intent or mood):
        original = str(note.get("intent") or note.get("meme_guess") or "").strip()
        if not original:
            return ""
        return ("【这张图】对方发来一张表情包，但你没把握它具体是什么梗。\n"
                "不要描述画面、不要猜图里有什么；用一句短话接住气氛，"
                "或者直接问对方「这表情啥意思」。" + "\n" + MEANING_RULE_V2)
    bits = []
    if intent:
        bits.append("对方想表达：" + intent)
    if mood:
        bits.append("对方情绪：" + mood)
    text = str(note.get("text_in_image") or "").strip()
    if text:
        bits.append("图上的字：" + text)
    # 只截对方那部分；规矩那句是我要她读到的，砍掉就等于没说
    return "【这张图的意思】" + "；".join(bits)[:budget] + "\n" + MEANING_RULE_V2



def _usable_note(note):
    if not isinstance(note, dict):
        return False
    def text(field):
        value = note.get(field)
        return isinstance(value, str) and bool(value.strip())
    objects = note.get("objects")
    if isinstance(objects, str):
        objects = [objects]
    has_objects = isinstance(objects, list) and any(
        isinstance(value, str) and value.strip() for value in objects)
    return text("mood") or text("intent") or text("meme_guess") or (has_objects and text("scene"))


def parse_note(txt):
    """有限修复结构化观察；失败或信息不足返回 None，保留诚实的不确定描述。"""
    raw = str(txt or "")[:4096].strip()
    if not raw:
        return None
    if (re.search(r"\bunavailable\b", raw, re.I)
            or any(marker in raw for marker in
                   ("[空响应]", "无法读取图片", "图片下载失败", "模型暂不可用"))):
        return None
    fence = chr(96) * 3
    raw = raw.replace(fence + "json", "").replace(fence, "").strip()
    a = raw.find("{")
    if a >= 0:
        body = raw[a:]
        if re.search(r'"error"\s*:', body):
            return None
        try:
            got, _end = json.JSONDecoder().raw_decode(body)
        except (ValueError, RecursionError):
            got = None
        else:
            return got if _usable_note(got) else None
        got = {}
        for field in ("intent", "kind", "mood", "meme_guess", "scene",
                      "people_action", "text_in_image"):
            m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.){1,220})(?:"|$)' % field, body)
            if m:
                try:
                    got[field] = json.loads('"' + m.group(1) + '"').strip()
                except ValueError:
                    pass
        m = re.search(r'"objects"\s*:\s*(\[[^\]]{0,360}\])', body)
        if m:
            try:
                got["objects"] = json.loads(m.group(1))
            except ValueError:
                pass
        return got if _usable_note(got) else None
    text = re.sub(r"\s+", " ", raw).strip("。！？.!? ")
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        return None
    if re.match(r"^(?:error|failed|timeout)\b", text, re.I):
        return None
    return {"mood": text[:24], "meme_guess": "", "objects": []}


# 「小葱吃西瓜还带角」这种是画面描述，不是含义。把它当意思喂回去，
# 她就照着念一遍；宁可说「没把握这个梗」，也不把物体清单当语义。
_DESCRIPTIVE_WORDS = re.compile(
    r"小葱|西瓜|角|吃|喝|拿|戴|穿|站|坐|躺|举|抱|颜色|背景|画面|图片|"
    r"手|头|脸|猫|狗|兔|熊|帽|衣|桌|杯|手机|字|表情包上")


def meaning_like(text):
    value = str(text or "").strip()
    if not value or len(value) > 18:
        return False
    # 「吃瓜」是含义（看戏），不是「在吃西瓜」；先放行再按描述词判。
    if "吃瓜" in value:
        return True
    return not _DESCRIPTIVE_WORDS.search(value)


def short_meaning(note, budget=20):
    """一行能塞进群上下文的意思，例如「想被哄·撒娇」。

    打头的永远是「对方想表达什么」——写成物体清单她就会照着念一遍画面。
    老笔记没有 intent，退回情绪和梗，不能因此变成空白。
    """
    if not isinstance(note, dict):
        return ""
    intent = str(note.get("intent") or "").strip() or str(note.get("meme_guess") or "").strip()
    mood = str(note.get("mood") or "").strip()
    # 描述性 intent 不算含义；退到情绪，情绪也不行就明说没懂。
    bits = [x for x in (intent, mood) if x and meaning_like(x)]
    if not bits:
        return ""
    out = "·".join(bits)
    return out[:budget]


def card_line(note, budget=34):
    """聊天上下文里的那一行：先写「这张图在说什么」，再写内容。

    符号图（表情包）只留含义 —— 把物体清单搬进上下文，她就会照着念一遍画面，
    那正是主人指着的那句「不是让你描述图片本身的情况」。
    载体图（照片/截图）里装着对方要给她看的东西，那几个物体是后面接话的本钱，
    不写就等于让她忘记自己看过什么。
    """
    if not isinstance(note, dict) or not note:
        return ""
    meaning = short_meaning(note, budget=24)
    if not meaning:
        return ""
    head = "对方想表达：" + meaning
    if _is_symbol_image(note):
        return head[:budget]
    objs = "、".join(str(x).strip() for x in (note.get("objects") or [])[:2]
                     if str(x).strip())
    return (head + ("｜" + objs if objs else ""))[:budget]


def _reset_cache():
    _cache.update({"sig": None, "data": {}})


def _file_sig():
    try:
        st = os.stat(_file())
        return (round(st.st_mtime, 3), st.st_size)
    except Exception:
        return None


def _load():
    sig = _file_sig()
    if sig is None:
        return {}
    if _cache["sig"] == sig and _cache["data"]:
        return _cache["data"]
    data = {}
    try:
        with open(_file(), encoding="utf-8") as f:
            got = json.load(f) or {}
        if isinstance(got, dict):
            data = got
    except Exception:
        data = {}
    _cache.update({"sig": sig, "data": data})
    return data


def _save(data):
    try:
        os.makedirs(os.path.dirname(_file()), exist_ok=True)
        path = _file()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        _cache.update({"sig": None, "data": data})
        return True
    except Exception as e:
        print("[看图笔记] 写盘失败 " + str(e)[:60], flush=True)
        return False


def _identity(ref):
    """一张图的稳定身份（人类可读的那一段，不哈希）。

    以前的 `key_of` 取「最后一段路径」当身份，而 QQ 图片地址永远是
    `.../download?appid=1407&fileid=...`，最后一段全是 `download` ——
    **所有 QQ 图共用同一个 key**，笔记互相覆盖，等于没有笔记。
    现在优先认 fileid/file/md5 这类真正标识图像本身的参数。
    """
    r = str(ref or "").strip()
    if not r:
        return ""
    if r.startswith("data:image/"):
        return "sha:" + hashlib.sha1(r.encode("utf-8", "ignore")).hexdigest()[:16]
    if r.startswith(("http://", "https://")):
        try:
            parts = urlsplit(r)
        except Exception:
            parts = None
        if parts is not None:
            q = parse_qs(parts.query)
            host = parts.netloc.lower()
            for name in ("fileid", "file_id", "file", "md5", "hash", "fname", "filename"):
                if q.get(name):
                    return "q:%s:%s:%s" % (host, name, q[name][0])
            volatile = {"t", "ts", "timestamp", "expires", "expire", "expiry",
                        "signature", "sig", "sign", "token", "auth_key"}
            query = urlencode(sorted((name, value) for name, value in
                                     parse_qsl(parts.query, keep_blank_values=True)
                                     if name.lower() not in volatile))
            return "url:" + host + parts.path + ("?" + query if query else "")
        return r.split("#", 1)[0]
    return r.strip("/").split("/")[-1]


def key_of(ref):
    """图的稳定身份（哈希后，用作笔记的 key）。"""
    ident = _identity(ref)
    if not ident:
        return ""
    return hashlib.sha1(ident.encode("utf-8", "ignore")).hexdigest()[:16]


def all_notes():
    return _load()


def _visibility(note):
    if note.get("sensitive") or note.get("visibility") == "private":
        return "private"
    session = str(note.get("session") or "").strip()
    if session:
        return "group" if session.startswith("group") else "private"
    return "group" if note.get("visibility") == "group" else "private"


def _visible(note, *, session=None, scene=None):
    if not isinstance(note, dict):
        return False
    if scene is None and session is None:
        return True
    if _visibility(note) == "group":
        return True
    if scene == "group":
        return False
    owner = str(note.get("session") or "").strip()
    return bool(owner and session and owner == str(session).strip())


def note_for(ref, *, session=None, scene=None):
    k = key_of(ref)
    if not k:
        return None
    n = _load().get(k)
    return dict(n) if _visible(n, session=session, scene=scene) else None


def _clean(note):
    out = {}
    obj = (note or {}).get("objects")
    if isinstance(obj, str):
        obj = [x for x in re.split(r"[,，、;；\n]+", obj) if x.strip()]
    # 截断修补捞回来的 objects 会混进 `"scene\":\"二次元头像背景"` 这种字段碎片
    # （生产 21:29 那条笔记就有三片）。带着它进上下文，她念出来的就是半句 JSON。
    def _ok(x):
        s = str(x).strip()
        return bool(s) and not any(c in s for c in '{}":：')
    out["objects"] = [x.strip()[:20] for x in (obj or []) if _ok(x)][:8]
    for f in ("text_in_image", "scene", "people_action", "mood", "meme_guess"):
        out[f] = str((note or {}).get(f) or "").strip()[:220 if f == "text_in_image" else 60]
    # 含义优先的两个字段：`_clean` 以前不认识它们，新提示词辛苦问出来的
    # intent/kind 会在落盘那一刻被丢掉（笔记里就只剩画面清单了）。
    out["intent"] = str((note or {}).get("intent") or "").strip()[:60]
    out["kind"] = str((note or {}).get("kind") or "").strip()[:20]
    out["sensitive"] = bool((note or {}).get("sensitive"))
    return out


def note_by_sha(content_sha, *, session=None, scene=None):
    """按图片内容的 sha1 找旧笔记。

    同一张贴纸被转发表情时 QQ 会给一个新的 fileid，但字节是一样的 ——
    没有这一层就要把同一张图反复送去看一遍。
    """
    sha = str(content_sha or "").strip()
    if not sha:
        return None
    for rec in (_load() or {}).values():
        if (isinstance(rec, dict) and str(rec.get("sha") or "") == sha
                and _visible(rec, session=session, scene=scene)):
            return dict(rec)
    return None


def save_note(ref, note, source="", ts=None, sensitive=None, *, session="",
              run_id=None, content_sha=""):
    """写一条看图笔记。同 key 更新而不是追加。"""
    k = key_of(ref)
    if not k:
        return ""
    data = _load()
    old = data.get(k) or {}
    note = note or {}
    rec = _clean(note)
    rec["key"] = k
    rec["source"] = str(source or "")[:20]
    rec["ts"] = float(ts or time.time())
    rec["ref"] = str(ref or "")[-60:]
    if content_sha:
        rec["sha"] = str(content_sha)[:64]
    rec["sensitive"] = bool(sensitive or rec["sensitive"] or old.get("sensitive"))
    rec["session"] = str(session or "").strip()
    rec["visibility"] = "private" if note.get("visibility") == "private" else "group"
    rec["visibility"] = _visibility(rec)
    provenance = old if old and _visibility(old) == "private" else None
    if any(field in note for field in ("key", "session")):
        if _visibility(note) == "private":
            if (provenance is not None
                    and str(provenance.get("session") or "").strip()
                    != str(note.get("session") or "").strip()):
                return ""
            provenance = note
    if provenance is not None:
        owner = str(provenance.get("session") or "").strip()
        if provenance is old and rec["session"] and rec["session"] != owner:
            return ""
        rec["session"] = owner
        rec["visibility"] = "private"
    rec["used"] = int(old.get("used") or 0)
    rec["last_used"] = float(old.get("last_used") or 0.0)
    data[k] = rec
    # 淘汰按「最久没用」，不是「最旧写的」——常被提起的图不该先掉
    if len(data) > MAX_NOTES:
        order = sorted(data.items(),
                       key=lambda kv: (float(kv[1].get("last_used") or 0.0),
                                       float(kv[1].get("ts") or 0.0)))
        for kk, _v in order[:len(data) - MAX_NOTES]:
            data.pop(kk, None)
    saved = _save(data)
    if saved:
        try:
            import self_model
            self_model.record_tool_result(
                "vision", k, True, session=session, run_id=run_id,
                visibility=rec["visibility"])
        except Exception as exc:
            print("[认知] 看图结果记账失败 " + type(exc).__name__, flush=True)
    return k


def _bump_used(key):
    try:
        data = _load()
        rec = data.get(key)
        if not isinstance(rec, dict):
            return
        rec["used"] = int(rec.get("used") or 0) + 1
        rec["last_used"] = time.time()
        _save(data)
    except Exception:
        pass


def render_for_prompt(note, scene="private", budget=420):
    """按场合决定把笔记的哪一部分交给她。

    两种图分两条路（`meme_card`）：表情包只给含义，照片/截图才给画面细节。
    scene=group  → 只给「对方想表达什么 + 情绪」，不给物体清单与图上文字：
                    给了清单她就会照着念一遍，那正是群里最违和的解说腔；
    scene=private→ 符号图同上，载体图给全，允许她说看不清；
    sensitive    → 永不进 group（主人的桌面、药盒、饭菜不该在群里被提起）。
    """
    if not isinstance(note, dict) or not note:
        return ""
    k = str(note.get("key") or "")
    symbol = _is_symbol_image(note)
    if scene == "group":
        if _visibility(note) == "private":
            return ""
        line = short_meaning(note, budget=60)
        if not line:
            return ""
        if k:
            _bump_used(k)
        return ("（这张图你看过，它的意思是：" + line +
                "。这段只是让你听懂，别照念、别描述画面，用自己的话接。）")
    card = meme_card(note) if symbol else ""
    if card:
        if k:
            _bump_used(k)
        try:
            import memory_usage as _mu
            _mu.note("vision_note", ["vn:" + (k or "?")], query="",
                     injected_chars=len(card), scene=scene)
        except Exception:
            pass
        return card
    lines = []
    intent = str(note.get("intent") or "").strip()
    if intent:
        lines.append("- 对方想表达：" + intent)
    if note.get("objects") and not symbol:
        lines.append("- 图里的东西：" + "、".join(note["objects"]))
    if str(note.get("text_in_image") or "").strip():
        lines.append("- 图上的字：" + str(note.get("text_in_image")).strip())
    if str(note.get("scene") or "").strip():
        lines.append("- 场合：" + str(note.get("scene")).strip())
    if str(note.get("people_action") or "").strip():
        lines.append("- 有人在做什么：" + str(note.get("people_action")).strip())
    if str(note.get("mood") or "").strip():
        lines.append("- 感觉：" + str(note.get("mood")).strip())
    if str(note.get("meme_guess") or "").strip():
        lines.append("- 像是什么梗：" + str(note.get("meme_guess")).strip())
    if not lines:
        return ""
    if k:
        _bump_used(k)
    out = "\n".join(lines)[:budget]
    try:
        import memory_usage as _mu
        _mu.note("vision_note", ["vn:" + (k or "?")], query="",
                 injected_chars=len(out), scene=scene)
    except Exception:
        pass
    return ("【你看过的这张图】\n" + out +
            "\n（先想对方发这张图是什么意思、什么情绪，回应那个；"
            "画面细节只在对方真问起时才说，不要开场就报一遍。看不清就承认看不清。）")


def latest_notes(scene="private", limit=2, within_days=7, session=""):
    """私聊仅检索明确的同会话笔记；群聊排除私人和无归属的旧笔记。"""
    session = str(session or "").strip()
    if scene == "private" and not session:
        return []
    data = _load()
    if not data:
        return []
    cut = time.time() - max(1, int(within_days)) * 86400.0
    items = [v for v in data.values()
             if isinstance(v, dict) and float(v.get("ts") or 0) >= cut
             and _visible(v, session=session, scene=scene)]
    if scene == "private":
        items = [v for v in items if str(v.get("session") or "").strip() == session]
    items.sort(key=lambda v: -float(v.get("ts") or 0))
    return items[:max(1, int(limit))]


def stats():
    data = _load()
    n = list(data.values())
    return {"total": len(n),
            "with_text": len([x for x in n if str(x.get("text_in_image") or "").strip()]),
            "sensitive": len([x for x in n if x.get("sensitive")]),
            "never_used": len([x for x in n if int(x.get("used") or 0) == 0]),
            "max": MAX_NOTES}
