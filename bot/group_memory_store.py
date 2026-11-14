"""群长期记忆：只增不删的结构化条目。

设计原则（按主人的要求）：
  1. 写进去的东西不会消失，条目只增不减；
  2. 只有"和已有条目矛盾"的新信息才覆盖旧条目（旧内容另存 superseded，仍不丢）；
  3. 注入提示词时按相关性挑选，而不是把整段截断——所以条目再多也不会互相挤掉。

存储：data/memory/group_facts.json
{
  "groups": {
    "<gid>": {
      "entries": [
        {"id":"a1b2","topic":"阿甲","text":"...","ts":1730,"hits":3,"superseded":[]}
      ],
      "updated": 1730
    }
  }
}
"""
import json
import math
import os
import re
import secrets
import time

_DEFAULT_FILE = os.path.join(os.getenv("DATA_DIR", "./data"), "memory", "group_facts.json")
_FILE = _DEFAULT_FILE

def _path():
    return _FILE if _FILE != _DEFAULT_FILE else os.path.join(
        os.getenv("DATA_DIR", "./data"), "memory", "group_facts.json")
MAX_ENTRIES_PER_GROUP = int(os.getenv("GROUP_FACTS_MAX", "8000"))
CONTEXT_BUDGET = int(os.getenv("GROUP_FACTS_CONTEXT_CHARS", "1600"))
# 单次检索最多参与排序的候选条数，给最坏情况兜一个上界
CANDIDATE_CAP = int(os.getenv("GROUP_FACTS_CANDIDATE_CAP", "2000"))
# 索引待重建标记：写盘后不立刻重建（4000 条要 1.5 秒），
# 先打标记，由后台重建，避免卡在对话链路上。
_PENDING = {"need": False}

_cache = {"data": None, "ts": 0.0}
_TTL = 2.0


def _ensure():
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
    except Exception:
        pass


def _load(force=False):
    now = time.time()
    if (not force and _cache["data"] is not None and _cache.get("path") == _path()
            and (now - _cache["ts"]) < _TTL):
        return _cache["data"]
    data = {"groups": {}}
    try:
        _ensure()
        if os.path.isfile(_path()):
            with open(_path(), encoding="utf-8") as f:
                got = json.load(f) or {}
            if isinstance(got, dict) and isinstance(got.get("groups"), dict):
                data = got
    except Exception as e:
        print("[群事实库] 读取失败 " + str(e)[:80], flush=True)
        import safe_store
        safe_store.preserve_corrupt(_path())
    _cache["data"] = data
    _cache["path"] = _path()
    _cache["ts"] = now
    return data


def _save(data):
    import safe_store
    if safe_store.atomic_write_json(_path(), data):
        _cache["data"] = data
        _cache["path"] = _path()
        _cache["ts"] = time.time()
        return True
    return False


def _cut_clean(t, limit):
    """超长时退到最近的句号再截，避免留半截话。"""
    t = str(t or "")
    if len(t) <= limit:
        return t
    head = t[:limit]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
    if cut >= limit // 2:
        return head[:cut + 1]
    return head


def _norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def get_entries(group_id):
    g = (_load().get("groups") or {}).get(str(group_id)) or {}
    ents = g.get("entries")
    return list(ents) if isinstance(ents, list) else []


def unsourced_entries(limit=8):
    """还没绑逐命题来源的群事实，供人工标注面板挑样本（近期优先）。"""
    out = []
    for gid, group in (_load().get("groups") or {}).items():
        for entry in group.get("entries") or []:
            if not entry.get("source_event_ids"):
                out.append({"group_id": str(gid), "id": entry.get("id"),
                            "topic": entry.get("topic", ""), "text": entry.get("text", ""),
                            "ts": entry.get("ts")})
    out.sort(key=lambda r: -(r.get("ts") or 0))
    return out[:int(limit)]


def set_source(group_id, entry_id, event_ids):
    """人工/自动给一条群事实绑定真实事件号。只改来源，不动文本。"""
    data = _load(force=True)
    group = (data.get("groups") or {}).get(str(group_id)) or {}
    for entry in group.get("entries") or []:
        if entry.get("id") == entry_id:
            entry["source_event_ids"] = [str(x) for x in (event_ids or [])
                                         if str(x or "").strip()]
            return bool(_save(data))
    return False


def delete_entry(group_id, entry_id):
    """人工删除一条无法绑定来源的群事实。返回是否删到。"""
    data = _load(force=True)
    group = (data.get("groups") or {}).get(str(group_id)) or {}
    entries = group.get("entries") or []
    keep = [e for e in entries if e.get("id") != entry_id]
    if len(keep) == len(entries):
        return False
    group["entries"] = keep
    return bool(_save(data))


def add_entry(group_id, topic, text, ts=None, aliases=None):
    """新增一条。完全相同的不重复加。"""
    gid = str(group_id)
    txt = str(text or "").strip()
    if not txt:
        return None
    data = _load()
    g = data.setdefault("groups", {}).setdefault(gid, {"entries": [], "updated": 0})
    ents = g.setdefault("entries", [])
    key = _norm(txt)[:80]
    for e in ents:
        if _norm(e.get("text"))[:80] == key:
            e["hits"] = int(e.get("hits", 1)) + 1
            e["ts"] = ts or time.time()
            # 同一件事被换了种说法再说一遍：把说法收进别名，别丢
            for _a in _clean_aliases(aliases):
                if _a not in (e.get("aliases") or []):
                    e.setdefault("aliases", []).append(_a)
            e["aliases"] = e["aliases"][:6]
            g["updated"] = time.time()
            _save(data)
            # 命中数变了但内容没变：把签名对齐，别让下一轮误判成"文件变了"去全量重建。
            # 4000 条重建要 1.5 秒，落在对话中途就是卡死。
            try:
                grp = (_INDEX.get("groups") or {}).get(gid)
                if grp and str(e.get("id")) in grp.get("entries", {}):
                    grp["entries"][str(e.get("id"))] = e
                _INDEX["sig"] = _file_sig()
            except Exception:
                _INDEX["sig"] = None
            return e
    _als = _clean_aliases(aliases)
    rec = {
        "id": secrets.token_hex(4),
        "topic": str(topic or "其他")[:24],
        "text": _cut_clean(txt, 400),
        "ts": float(ts or time.time()),
        "hits": 1,
        # 别名 = 用户自己怎么说这件事的。topic 是 LLM 起的名字，
        # 用户原话永远整词命中不了它，打分最重的那一项等于白设。
        "aliases": _als,
        "superseded": [],
    }
    ents.append(rec)
    if len(ents) > MAX_ENTRIES_PER_GROUP:
        # 只裁剪"重复次数最少且最老"的，且不裁剪带 superseded 的
        keep = sorted(
            range(len(ents)),
            key=lambda i: (int(ents[i].get("hits", 1)), float(ents[i].get("ts") or 0)),
            reverse=True,
        )[:MAX_ENTRIES_PER_GROUP]
        g["entries"] = [ents[i] for i in sorted(keep)]
    g["updated"] = time.time()
    _save(data)
    # 增量维护索引：只给这一条分词并挂进倒排，
    # 避免下一轮 _ensure_index 看到文件变了就全量重建（400 条要 50ms+）
    try:
        _index_add(gid, rec)
    except Exception:
        _INDEX["sig"] = None      # 出问题就退回全量重建，宁可慢也别错
    return rec


def supersede_entry(group_id, entry_id, new_text):
    """用新说法覆盖旧条目，但旧内容仍留在 superseded 里。"""
    gid = str(group_id)
    data = _load()
    g = data.setdefault("groups", {}).setdefault(gid, {"entries": [], "updated": 0})
    for e in g.get("entries") or []:
        if str(e.get("id")) != str(entry_id):
            continue
        old = str(e.get("text") or "")
        if old:
            e.setdefault("superseded", []).append({"text": old, "ts": time.time()})
            e["superseded"] = e["superseded"][-5:]
        e["text"] = str(new_text or old)[:400]
        e["ts"] = time.time()
        e["hits"] = int(e.get("hits", 1)) + 1
        g["updated"] = time.time()
        _save(data)
        return True
    return False


def migrate_summary(group_id, summary):
    """把旧的纯文本摘要迁移成条目（切成块，永不丢）。"""
    if not str(summary or "").strip():
        return 0
    if get_entries(group_id):
        return 0
    blocks = []
    cur = []
    for line in str(summary).splitlines():
        s = line.strip()
        if not s:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(s)
        if len("\n".join(cur)) > 260:
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    n = 0
    for b in blocks:
        if b.strip():
            add_entry(group_id, "历史", b)
            n += 1
    return n


def _sentences(t):
    return [x.strip() for x in re.split(r"[。；;\n]+", str(t or "")) if x.strip()]


def _merge_texts(longer, shorter):
    """把较短那条里「较长那条没有」的信息补上，保证信息量只增不减。"""
    from difflib import SequenceMatcher
    base = str(longer or "").strip()
    ls = _sentences(base)
    extra = []
    for s in _sentences(shorter):
        if not s or len(s) < 3:
            continue
        if s in base:
            continue
        if any(SequenceMatcher(None, s, x).ratio() > 0.80 for x in ls):
            continue
        if s in extra:
            continue
        extra.append(s)
    if not extra:
        return base
    tail = "；".join(extra)
    out = base.rstrip("。；; ") + "。" + tail
    # 兜底：绝不允许比原来短
    if len(out) < max(len(str(longer or "")), len(str(shorter or ""))):
        return base + "。" + tail
    return out


def merge_duplicates(group_id, threshold=0.84):
    """把说的是同一件事的条目合并成一条。

    铁律（按主人的要求）：
      1. 只合并同一 topic 的条目，跨主题一律不动；
      2. 相似度不够高的不合并——宁可不合，也不许把不同的事揉在一起；
      3. 合并后必须包含两边全部信息，长度只能变长不能变短；
      4. 被合并掉的原文完整留在 superseded 里，随时可查。
    """
    from difflib import SequenceMatcher

    gid = str(group_id)
    data = _load()
    g = data.setdefault("groups", {}).setdefault(gid, {"entries": [], "updated": 0})
    ents = g.get("entries") or []
    if len(ents) < 2:
        return 0

    merged_count = 0
    used = set()
    out = []

    for i, a in enumerate(ents):
        if i in used:
            continue
        topic_a = str(a.get("topic") or "")
        ta = str(a.get("text") or "")
        best_j = -1
        best_r = 0.0
        for j in range(i + 1, len(ents)):
            if j in used:
                continue
            b = ents[j]
            if str(b.get("topic") or "") != topic_a:
                continue
            tb = str(b.get("text") or "")
            if not ta or not tb:
                continue
            na, nb = _norm(ta), _norm(tb)
            if na and nb and (na in nb or nb in na):
                r = 1.0          # 一条是另一条的子集，算完全重复
            else:
                r = SequenceMatcher(None, na, nb).ratio()
            if r > best_r:
                best_r = r
                best_j = j
        if best_j >= 0 and best_r >= threshold:
            b = ents[best_j]
            tb = str(b.get("text") or "")
            longer, shorter = (ta, tb) if len(ta) >= len(tb) else (tb, ta)
            merged = _merge_texts(longer, shorter)
            # 安全闸：信息量不许变少
            if len(merged) < max(len(ta), len(tb)):
                out.append(a)
                continue
            hist = list(a.get("superseded") or [])
            hist.append({"text": shorter, "ts": time.time(), "why": "merged"})
            a = dict(a)
            a["text"] = merged[:600]
            a["superseded"] = hist[-6:]
            a["hits"] = int(a.get("hits", 1)) + int(b.get("hits", 1))
            a["ts"] = max(float(a.get("ts") or 0), float(b.get("ts") or 0))
            used.add(best_j)
            merged_count += 1
        out.append(a)

    if merged_count:
        g["entries"] = out
        g["updated"] = time.time()
        _save(data)
        print("[群事实库] %s 合并了 %d 组重复条目，现 %d 条（只增不减）"
              % (gid, merged_count, len(out)), flush=True)
    return merged_count


_FOOD_WORDS = re.compile(r"虾|黄焖鸡|白饭|大米饭|干饭|开饭")
FOOD_SUMMARY = ("群里长期流行食物梗（虾、白饭、黄焖鸡、开饭），是一群人互相接的玩笑；"
                "清漪以前几乎句句都挂这个，主人已经要求她收敛，现在只偶尔提一次。")


_BULLET = re.compile(r"^\s*(?:[-•·]|\d+[.、)）]|[一二三四五六七八九十][、.])\s*")


def _end_punct(t):
    t = str(t or "").strip()
    if not t:
        return t
    if t[-1] in "。！？…":
        return t
    return t + "。"


def tidy_entries(group_id, min_len=12, max_len=70):
    """把记忆条目整理成「短、但完整」的句子。

    - 旧的整段摘要按句号切成一件事一条，不再是一坨；
    - 缺句号的补上，不留半截话；
    - 太碎的相邻句子拼起来，但不超长；
    - 原文全部留在 superseded 里，信息一个字都不丢。
    """
    gid = str(group_id)
    data = _load()
    g = data.setdefault("groups", {}).setdefault(gid, {"entries": [], "updated": 0})
    ents = g.get("entries") or []

    out = []
    changed = 0
    for e in ents:
        raw = str(e.get("text") or "").strip()
        if not raw:
            continue
        # 已经短且以标点收尾的，不动
        if len(raw) <= max_len and raw[-1] in "。！？…":
            out.append(e)
            continue

        parts = []
        for seg in re.split(r"[。；;\n]+", raw):
            seg = _BULLET.sub("", seg).strip(" ，,、")
            if not seg:
                continue
            # 只有逗号、没句号的超长堆砌，再按逗号细分一层
            if len(seg) > max_len * 1.5:
                for sub in re.split(r"[，,]+", seg):
                    sub = sub.strip(" ，,、")
                    if sub:
                        parts.append(sub)
            else:
                parts.append(seg)
        if not parts:
            out.append(e)
            continue

        merged = []
        buf = ""
        for seg in parts:
            cand = (buf + "。" + seg) if buf else seg
            if len(cand) <= max_len or len(buf) < min_len:
                buf = cand
            else:
                merged.append(buf)
                buf = seg
        if buf:
            merged.append(buf)

        if len(merged) == 1 and _end_punct(merged[0]) == raw:
            out.append(e)
            continue

        changed += 1
        for k, piece in enumerate(merged):
            txt = _end_punct(piece)[:400]
            if k == 0:
                e2 = dict(e)
                e2["text"] = txt
                e2.setdefault("superseded", []).append(
                    {"text": raw, "ts": time.time(), "why": "tidy-split"})
                e2["superseded"] = e2["superseded"][-5:]
                out.append(e2)
            else:
                out.append({
                    "id": secrets.token_hex(4),
                    "topic": str(e.get("topic") or "其他"),
                    "text": txt,
                    "ts": float(e.get("ts") or time.time()),
                    "hits": int(e.get("hits", 1)),
                    "superseded": [],
                })

    if changed:
        # 分出来的碎句可能重复，顺手去个重
        seen = set()
        deduped = []
        for e in out:
            k = _norm(e.get("text"))[:60]
            if k and k in seen:
                continue
            seen.add(k)
            deduped.append(e)
        g["entries"] = deduped
        g["updated"] = time.time()
        _save(data)
        print("[群事实库] %s 整理条目：%d -> %d 条（切分 %d 条）"
              % (gid, len(ents), len(deduped), changed), flush=True)
    return changed


def trim_food_noise(group_id, min_keep=15):
    """把食物梗从记忆里压下去。

    做法：按分句切（。；，、），只删掉含食物梗的那一小句，
    同一句话里别的内容全部保留。这样信息不会丢，只是不再句句带吃的。
    整条里一丁点非食物内容都不剩的，才并入一条汇总。
    """
    gid = str(group_id)
    data = _load()
    g = data.setdefault("groups", {}).setdefault(gid, {"entries": [], "updated": 0})
    ents = g.get("entries") or []

    kept = []
    dropped = 0
    dirty = False
    has_summary = False

    for e in ents:
        t = str(e.get("text") or "")
        if not _FOOD_WORDS.search(t):
            kept.append(e)
            continue
        if str(e.get("id")) == "foodsum" or t.startswith(FOOD_SUMMARY[:20]):
            has_summary = True
            kept.append(e)
            continue

        # 按分句切，只丢掉含食物梗的那几小句
        parts = [x.strip() for x in re.split(r"[。；;，,\n]+", t) if x.strip()]
        rest = "，".join([x for x in parts if not _FOOD_WORDS.search(x)]).strip("，。 ")
        if len(rest) >= min_keep:
            e2 = dict(e)
            e2["text"] = rest[:400]
            e2.setdefault("superseded", []).append(
                {"text": t, "ts": time.time(), "why": "food"})
            e2["superseded"] = e2["superseded"][-5:]
            kept.append(e2)
            dirty = True
        else:
            dropped += 1
            dirty = True

    if dropped and not has_summary:
        kept.append({
            "id": "foodsum",
            "topic": "群梗",
            "text": FOOD_SUMMARY,
            "ts": time.time(),
            "hits": dropped,
            "superseded": [],
        })
        dirty = True

    if dirty:
        g["entries"] = kept
        g["updated"] = time.time()
        _save(data)
        print("[群事实库] %s 压掉食物梗 %d 条，现有 %d 条"
              % (gid, dropped, len(kept)), flush=True)
    return 1 if dirty else 0


def split_long_entries(group_id, max_len=140):
    """把超长条目拆成可单独检索的小条，避免一条吃掉整个预算。"""
    gid = str(group_id)
    data = _load()
    g = data.setdefault("groups", {}).setdefault(gid, {"entries": [], "updated": 0})
    ents = g.get("entries") or []
    out = []
    changed = 0
    for e in ents:
        txt = str(e.get("text") or "")
        if len(txt) <= max_len:
            out.append(e)
            continue
        # 按句号/分号/换行切片，再合并到不超过 max_len
        parts = [x.strip() for x in re.split(r"[。；;\n]+", txt) if x.strip()]
        buf = ""
        idx = 0
        for pt in parts:
            if len(buf) + len(pt) + 1 > max_len and buf:
                idx += 1
                out.append({
                    "id": secrets.token_hex(4),
                    "topic": str(e.get("topic") or "其他"),
                    "text": buf.strip()[:400],
                    "ts": float(e.get("ts") or time.time()),
                    "hits": int(e.get("hits", 1)),
                    "superseded": [],
                })
                buf = pt
            else:
                buf = (buf + "。" + pt) if buf else pt
        if buf.strip():
            idx += 1
            out.append({
                "id": secrets.token_hex(4),
                "topic": str(e.get("topic") or "其他"),
                "text": buf.strip()[:400],
                "ts": float(e.get("ts") or time.time()),
                "hits": int(e.get("hits", 1)),
                "superseded": [],
            })
        changed += 1
    g["entries"] = out
    g["updated"] = time.time()
    _save(data)
    return changed

def render_summary(group_id):
    """完整文本视图（给后台面板看，不截断）。"""
    ents = get_entries(group_id)
    if not ents:
        return ""
    by_topic = {}
    for e in ents:
        by_topic.setdefault(str(e.get("topic") or "其他"), []).append(e)
    parts = []
    for topic, items in by_topic.items():
        lines = ["### " + topic]
        for e in items:
            lines.append("- " + str(e.get("text")))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ── 记忆检索索引 ─────────────────────────────────────────────
# 数据量会一直涨。如果每次回复都把所有条目逐个打分，几千条就会开始拖慢回复。
# 这里建一个倒排索引：文件一变就重建一次，之后查询只看命中的那几条。
# 效果：单次检索的耗时跟「总条目数」无关，只跟「命中数」有关。
_INDEX = {"sig": None, "groups": {}, "built": 0.0}


def _file_sig():
    """用 mtime+size 判断文件有没有变，比每次重新解析 JSON 便宜得多。"""
    try:
        st = os.stat(_path())
        return (round(st.st_mtime, 3), st.st_size)
    except Exception:
        return None


# ── 分词：jieba + 停用词 ───────────────────────────
# 原来用 CJK 二元组，会切出「主人叫铃」「主人很」这种跨词边界的碎片，
# 占索引、污染打分。改成真分词，并滤掉到处都出现的虚词。
_STOPWORDS = None
_JIEBA = {"mod": None, "tried": False}


def _clean_aliases(aliases, limit=6):
    """别名去空、去重、限长。太长就没法整词命中用户原话了。"""
    out = []
    for a in (aliases or []):
        a = str(a or "").strip()
        if not a or len(a) > 12:
            continue
        if a not in out:
            out.append(a)
        if len(out) >= limit:
            break
    return out


def _entry_tokens(rec):
    """一条记忆的检索词集：话题 + 正文 + 别名。

    别名以前根本不进索引，所以「用户怎么说」这条路是断的。
    """
    tk = _tokens(str(rec.get("topic") or "")) | _tokens(str(rec.get("text") or ""))
    for a in (rec.get("aliases") or []):
        a = str(a or "").strip().lower()
        if a:
            tk.add(a)
            for t in _tokens(a):
                tk.add(t)
    return tk


def _jieba():
    """惰性加载 jieba，并载入自定义词典（人名/社群术语）。

    没装就返回 None，上层自动退回二元组，不会因为缺库让检索失效。
    """
    if not _JIEBA["tried"]:
        _JIEBA["tried"] = True
        try:
            import jieba as _j
            # 优先 DATA_DIR/personas/（部署方自己的词表），回落仓库内样例
            _pd = os.path.join(os.getenv("DATA_DIR", "./data"), "personas", "persona_dict.txt")
            _dict = _pd if os.path.isfile(_pd) else os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "persona_dict.txt")
            if os.path.isfile(_dict):
                try:
                    _j.load_userdict(_dict)
                except Exception:
                    pass
            _JIEBA["mod"] = _j
        except Exception:
            _JIEBA["mod"] = None
    return _JIEBA["mod"]


def _load_stopwords():
    global _STOPWORDS
    if _STOPWORDS is not None:
        return _STOPWORDS
    path_ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hit_stopwords.txt")
    words = set()
    try:
        with open(path_, encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if w and not w.startswith("#"):
                    words.add(w)
    except Exception:
        pass
    _STOPWORDS = words
    return words


def _tokens(text):
    """分词口径见 text_tokens（系统唯一实现）。

    口径依据的实测数据也挪过去了：二元组 R@1 80.0%、jieba 75.0%、
    两者混合 82.5% —— 三个库统一走混合那套，而不是各留一套互相打脸。
    """
    try:
        import text_tokens
        return text_tokens.tokens(text)
    except Exception:
        pass
    # 兜底：新模块出问题时退回本地这份旧实现，宁可慢也不能让检索整个失效
    t = str(text or "")
    if not t:
        return set()
    stop = _load_stopwords()
    out = set()

    # ① jieba 词（带她的专属词典）
    jieba = _jieba()
    if jieba is not None:
        for w in jieba.cut(t):
            w = w.strip()
            if not w or w in stop:
                continue
            if not re.search(r"[\w一-鿿]", w, re.UNICODE):
                continue
            # 单字基本是噪音，除非是数字或字母
            if len(w) == 1 and not re.match(r"[A-Za-z0-9]", w):
                continue
            out.add(w if re.match(r"[A-Za-z0-9]", w) else w.lower())

    # ② 汉字二元组（补子串重叠）
    for seg in re.findall(r"[一-鿿]+", t):
        if len(seg) == 1:
            out.add(seg)
        for k in range(len(seg) - 1):
            bg = seg[k:k + 2]
            if bg not in stop:
                out.add(bg)

    # ③ 英文/数字整词
    for w in re.findall(r"[A-Za-z0-9]{2,}", t):
        if w.lower() not in stop:
            out.add(w.lower())
    return out


# ── 分层压缩 ───────────────────────────────────────
# 人不会逐条记住上周每句话，但会记得"那阵子总在聊抽卡"。
# 所以旧条目不是删掉，而是并成一条摘要：保留话题和大概，丢掉逐字。
COMPRESS_DAYS = float(os.getenv("GROUP_COMPRESS_DAYS", "14"))     # 超过这个天数的开始压
COMPRESS_KEEP = int(os.getenv("GROUP_COMPRESS_KEEP", "2"))       # 每天最多留几条原文


def compress(group_id, days=None, keep=None):
    """把 N 天前的条目压成摘要。返回 {merged, before, after}。"""
    gid = str(group_id)
    days = float(days if days is not None else COMPRESS_DAYS)
    keep = int(keep if keep is not None else COMPRESS_KEEP)
    cutoff = time.time() - days * 86400

    data = _load()
    g = (data.get("groups") or {}).get(gid)
    if not g:
        return {"merged": 0}
    ents = g.get("entries") or []
    old = [e for e in ents if float(e.get("ts") or 0) < cutoff]
    if len(old) < 8:
        return {"merged": 0}      # 太少，压了没意义

    # 按天分桶
    buckets = {}
    for e in old:
        day = time.strftime("%Y-%m-%d", time.localtime(float(e.get("ts") or 0)))
        buckets.setdefault(day, []).append(e)

    keep_ids = set()
    new_entries = []
    merged = 0
    for day, rows in buckets.items():
        if len(rows) <= keep:
            continue
        # 每天留下最重要的 keep 条原文
        rows_sorted = sorted(rows, key=lambda r: -_importance(r))
        for r in rows_sorted[:keep]:
            keep_ids.add(str(r.get("id")))
        rest = rows_sorted[keep:]
        # 其余并成一条：取出现最多的话题 + 几条代表句
        topics = {}
        for r in rest:
            tp = str(r.get("topic") or "其他")
            topics[tp] = topics.get(tp, 0) + 1
        top_topics = sorted(topics.items(), key=lambda x: -x[1])[:5]
        sample = "；".join(str(r.get("text") or "")[:20] for r in rest[:3])
        txt = ("%s 那阵子主要在聊：%s。%s"
               % (day, "、".join(t for t, _ in top_topics), sample))[:380]
        rec = {
            "id": secrets.token_hex(4),
            "topic": "往期摘要",
            "text": txt,
            "ts": max(float(r.get("ts") or 0) for r in rest),
            "hits": 1,
            "compressed": True,
            "merged_from": len(rest),
            "superseded": [],
        }
        new_entries.append(rec)
        merged += len(rest)

    if not merged:
        return {"merged": 0}

    before = len(ents)
    remain = [e for e in ents if str(e.get("id")) in keep_ids
              or float(e.get("ts") or 0) >= cutoff]
    g["entries"] = remain + new_entries
    g["updated"] = time.time()
    _save(data)
    # 压缩后在这里就把索引重建好，别留到下一轮对话里再付这 1-2 秒
    try:
        _rebuild_index(data)
    except Exception:
        _INDEX["sig"] = None
    print("[群记忆] 压缩 %d 条 -> %d 条摘要（原文 %d 条保留 %d 条）"
          % (before, len(g["entries"]), merged, len(remain)), flush=True)
    return {"merged": merged, "before": before, "after": len(g["entries"])}


def _index_add(gid, rec):
    """往现有索引里挂一条，不重建整张表。"""
    grp = (_INDEX.get("groups") or {}).get(str(gid))
    if not grp:
        return False
    eid = str(rec.get("id") or "")
    if not eid:
        return False
    tk = _entry_tokens(rec)
    grp["entries"][eid] = rec
    grp["tokens"][eid] = tk
    for t in tk:
        grp["inv"].setdefault(t, set()).add(eid)
    df = grp.setdefault("df", {})
    for t in tk:
        df[t] = int(df.get(t, 0)) + 1
    grp["n"] = max(1, len(grp["entries"]))
    if eid not in grp["order"]:
        grp["order"].append(eid)
    # 文件签名同步成最新，避免下次误判成"变了"
    _INDEX["sig"] = _file_sig()
    return True


def _rebuild_index(data=None):
    """把全部条目建成倒排索引。只在文件变化时跑一次。"""
    data = data or _load(force=True)
    groups = {}
    for gid, g in (data.get("groups") or {}).items():
        ents = g.get("entries") or []
        tok_by_id = {}
        inv = {}
        for e in ents:
            eid = str(e.get("id") or "")
            if not eid:
                continue
            tk = _entry_tokens(e)
            tok_by_id[eid] = tk
            for t in tk:
                inv.setdefault(t, set()).add(eid)
        df = {}
        for t, idset in inv.items():
            df[t] = len(idset)
        groups[str(gid)] = {
            "entries": {str(e.get("id")): e for e in ents if e.get("id")},
            "tokens": tok_by_id,
            "inv": inv,
            "df": df,
            "n": max(1, len(tok_by_id)),
            "order": [str(e.get("id")) for e in ents if e.get("id")],
        }
    _INDEX["groups"] = groups
    _INDEX["sig"] = _file_sig()
    _INDEX["built"] = time.time()
    return groups


def _ensure_index():
    """取索引。

    【不阻塞对话】文件变了但索引还在时，先用旧的返回，
    把重建留给后台。4000 条重建要 1.5 秒，落在对话中途就是卡死。
    新条目由 _index_add 增量挂进索引，所以"旧索引"并不缺内容，
    只是 hits/superseded 这类字段稍旧，对检索没影响。
    """
    sig = _file_sig()
    if sig is None:
        return {}
    has = bool(_INDEX.get("groups"))
    if has and _INDEX.get("sig") == sig:
        return _INDEX["groups"]
    if has:
        # 标记待重建，本轮照常用旧索引
        _PENDING["need"] = True
        return _INDEX["groups"]
    # 进程刚起、索引为空：这时候付一次全量重建（重启时多等 1-2 秒可以接受）
    t0 = time.time()
    groups = _rebuild_index()
    print("[群事实库] 首建索引 %d 条，%.0f ms"
          % (sum(len(g["entries"]) for g in groups.values()), (time.time() - t0) * 1000),
          flush=True)
    return groups


def rebuild_if_pending():
    """后台调用：真有需要时才重建。"""
    if not _PENDING.get("need"):
        return False
    _PENDING["need"] = False
    try:
        t0 = time.time()
        _rebuild_index()
        print("[群事实库] 后台重建完成 %.0f ms" % ((time.time() - t0) * 1000), flush=True)
        return True
    except Exception as e:
        print("[群事实库] 后台重建失败 " + type(e).__name__, flush=True)
        _INDEX["sig"] = None
        return False



def _score(entry, query, qtok=None):
    """相关度：主题整词命中权重最高，其次正文二元组命中。

    qtok 传进来就复用，避免每次重新分词。
    """
    q = str(query or "")
    if not q:
        return 0.0
    topic = str(entry.get("topic") or "")
    text = str(entry.get("text") or "")
    s = 0.0
    if topic and topic in q:
        s += 8.0                       # 主题整词命中，最强信号
    for ch in topic:
        if ch in q:
            s += 1.5
    if qtok:
        et = _tokens(topic) | _tokens(text)
        overlap = len(qtok & et)
        s += overlap * 2.0
    return s


def _idf(idx, tok):
    """某个词的信息量：越常见越不值钱。

    换成分词后必须配 IDF——「阿丁」「主人」这种词命中几十条，
    如果和「提弗洛斯」一样算分，正确那条就会被稀释掉，
    召回率反而比二元组低（实测 70% vs 90%）。
    """
    try:
        df = (idx.get("df") or {}).get(tok, 0)
        n = float(idx.get("n") or 1)
        if df <= 0:
            return 0.0
        w = math.log(1.0 + n / float(df))
        return max(0.3, min(w, 4.0))
    except Exception:
        return 1.0


def _score_fast(entry, q, qtok, etok, idx=None):
    """用索引里预算好的词集打分，不再重复分词。"""
    s = 0.0
    topic = str(entry.get("topic") or "")
    if topic and topic in q:
        s += 8.0
    for ch in topic:
        if ch in q:
            s += 1.5
    # 别名是用户自己的说法：整词命中就给次高权重。
    # 没有这一项，LLM 起的话题名（「赤合金使用建议」）对上
    # 用户原话（「赤合金太贵了」）只能靠二元组那点残渣分。
    for a in (entry.get("aliases") or []):
        a = str(a or "").strip().lower()
        if a and a in q.lower():
            s += 5.0
    if qtok and etok:
        common = qtok & etok
        if idx is not None:
            s += sum(_idf(idx, t) for t in common) * 2.0
        else:
            s += len(common) * 2.0
    return s


# ── 重要性打分（参考斯坦福 Generative Agents 的三因子检索）────
# 它原来按「recency + importance + relevance」综合排序；我们之前只有
# relevance 和 ts，同分的条目里「她怕黑」「主人是重庆人」和
# 「某次随口说了句在吗」排在一起，重要的反而被淹。
# 这里用启发式给每条算重要性，不额外调模型。
_IMPORTANT_NAME = re.compile(
    ("主人|阿甲|阿乙|阿丙|阿丁|阿戊|阿己|清漪|爸爸|妈妈|女儿|家人"
 + ("|" + "|".join(re.escape(x) for x in os.getenv("PERSON_NAME_TOKENS", "").split(",") if x.strip()))
 if os.getenv("PERSON_NAME_TOKENS", "").strip() else "")
)
_EMOTION_WORD = re.compile(
    r"喜欢|讨厌|害怕|难过|开心|心疼|生气|委屈|撒娇|担心|想念|答应|承诺"
    r"|第一次|最重要|记得|不许|再也|永远|一直|以后|秘密"
)
_CHATTER = re.compile(
    r"^(在吗|在不在|哈哈+|嗯+|哦+|好的?|收到|早|晚安|早安|吃了吗|睡了)$"
)


def _importance(entry):
    """这条记忆对她有多重要（0~5）。纯启发式，不调模型。"""
    t = str(entry.get("text") or "") + " " + str(entry.get("topic") or "")
    if not t.strip():
        return 0.0
    s = 0.0
    if _IMPORTANT_NAME.search(t):
        s += 1.8          # 和她重要的人有关
    if _EMOTION_WORD.search(t):
        s += 1.6          # 带情绪或承诺
    if re.search(r"\d", t):
        s += 0.4          # 具体数字，信息量高
    if len(t) >= 24:
        s += 0.6          # 有内容的描述，不是碎语
    if _CHATTER.match(t.strip()):
        s -= 1.6          # 纯寒暄，没什么记的价值
    if len(t) <= 8:
        s -= 0.5          # 太短多半是残句
    return max(0.0, min(s, 5.0))


def _mix_recent_and_important(idx, entries, recent_n=40, imp_n=20):
    """兜底选条：最近 recent_n 条 + 高重要度 imp_n 条（去重）。

    只用「最近 N 条」的话，一个安静了两周的群会把她的自我设定、
    关键约定全挤出去；纯按时间又会让水群废话上位。混着给最稳。
    排序开销 O(n log n)，n≤几千，毫秒级，不影响响应速度。
    """
    order = idx.get("order") or []
    recent = [entries[eid] for eid in order[-recent_n:] if eid in entries]
    seen = {str(e.get("id") or "") for e in recent}
    rest = [e for e in entries.values() if str(e.get("id") or "") not in seen]
    try:
        rest.sort(key=lambda e: -_importance(e))
    except Exception:
        pass
    return rest[:imp_n] + recent


def _rank_key(relevance, entry):
    """相关度 + 重要度 + 使用反馈 + 时间衰减。口径统一在 recall_link.final_score。"""
    try:
        import recall_link as _rl
        import memory_usage as _mu
        if not _rl.ENABLED:
            raise RuntimeError("召回链路 v2 未开启")
        k = _mu.key_group(entry.get("id"), entry.get("topic"), entry.get("text"))
        u = _mu.usage_of(k)
        return _rl.final_score({
            "relevance": float(relevance or 0.0),
            "importance": _importance(entry),
            "ts": float(entry.get("ts") or 0.0),
            "used": int(u.get("used") or 0),
            "last_used": float(u.get("last_used") or 0.0),
        })
    except Exception:
        # 退回旧口径：相关度 → 重要度 → 时间
        return float(relevance or 0.0) * 1000.0 + _importance(entry) * 10.0             + float(entry.get("ts") or 0.0) / 1e9


def build_context(group_id, query="", budget=None, include_archived=False):
    """挑相关的条目注入提示词。

    走倒排索引：先找命中词条的候选，再只对候选打分排序。
    数据涨到几千条时也不会变慢，因为不参与匹配的条目根本不进入排序。
    """
    gid = str(group_id)
    idx = _ensure_index().get(gid)
    if not idx:
        # 索引里没有这个群，退回直接读（可能是刚写入还没重建）
        ents = get_entries(gid)
        if not ents:
            return ""
        idx = None

    limit = int(budget or CONTEXT_BUDGET)
    q = str(query or "").strip()
    _fb = False          # 这一轮是不是走的兜底（没检索命中）

    if idx is None:
        ranked = sorted(ents, key=lambda e: -float(e.get("ts") or 0))
        cands = ranked
        _fb = True
    else:
        entries = idx["entries"]
        q = q
        if not q:
            cands = _mix_recent_and_important(idx, entries)
            _fb = True
        else:
            qtok = _tokens(q)
            inv = idx["inv"]
            # 从「最小的词桶」取候选，并设上限。
            # 高频词（比如「阿甲」）的桶可能有几千条，全捞出来排序就会变慢；
            # 只取最稀有的那些词命中的条目，既快又更准。
            buckets = []
            for t in qtok:
                got = inv.get(t)
                if got:
                    buckets.append((len(got), t, got))
            buckets.sort(key=lambda x: x[0])
            hit = set()
            for size, _t, got in buckets:
                hit |= got
                if len(hit) >= CANDIDATE_CAP:
                    break
            if len(hit) > CANDIDATE_CAP:
                # 还是太大：只留最新的 CAP 条，保证上界
                order = idx["order"]
                recent = set(order[-CANDIDATE_CAP:])
                hit &= recent
            if hit:
                tokmap = idx["tokens"]
                scored = []
                for eid in hit:
                    e = entries.get(eid)
                    if not e:
                        continue
                    sc = _score_fast(e, q, qtok, tokmap.get(eid) or (), idx)
                    if sc <= 0:
                        continue        # 零相关不进排序，缩小候选
                    scored.append((sc, e))
                if scored:
                    # 排序：相关度优先，同分时重要的在前（参考 Generative Agents
                    # 的 importance 因子），再往后才看时间。
                    # 现在多带两个因子：这条记忆被用上过没有（used/last_used，
                    # 来自 memory_usage 的检索账）与时间衰减 —— 见 recall_link。
                    # 以前 used 完全没进打分，所以「从来没被引用过的条目」和
                    # 「常被用到的条目」在这里是平权的，档案越长越挤。
                    scored.sort(key=lambda x: -_rank_key(x[0], x[1]))
                    cands = [e for _s, e in scored]
                else:
                    cands = _mix_recent_and_important(idx, entries)
                    _fb = True
            else:
                # 一个字都没命中：最近 + 重要混着给，别什么都不给
                cands = _mix_recent_and_important(idx, entries)
                _fb = True

    # 标题也算进预算，否则「700 字预算」实际会吐出 729 字
    _HEAD = "\n【这个群的长期记忆 · 这里的东西只会新增，不会被删】\n"
    lines = []
    used = 0
    _picked = []
    limit = max(200, limit - len(_HEAD))
    if not include_archived:
        # 默认不端归档：预算要留给「她最近真的在用」的东西。
        # 但条目一条都没删，include_archived=True 就能原样查回来。
        cands = [e for e in cands
                 if str(e.get("status") or "active") != "archived"]
    for e in cands:
        row = "- [" + str(e.get("topic") or "其他") + "] " + str(e.get("text") or "")
        if used + len(row) > limit:
            if lines:
                break
            continue          # 单条就超预算，跳过它试下一条
        lines.append(row)
        _picked.append(e)
        used += len(row)
    if not lines:
        return ""
    _note_usage(_picked, q, used + len(_HEAD), _fb, len(cands))
    if include_archived:
        # 归档条目被显式查到 = 她还需要它 —— 复活，别让它一直躺在归档里
        _revived = [str(e.get("id") or "") for e in (_picked or [])
                    if str(e.get("status") or "active") == "archived"]
        if _revived:
            try:
                import memory_archive as _march
                for _eid in _revived:
                    _march.revive(store="group_facts", group_id=gid, key=_eid)
            except Exception:
                pass
    return _HEAD + "\n".join(lines)


def _note_usage(picked, query, chars, fallback, candidates):
    """给 memory_usage 记一笔：只记真的进了提示词的条目。
    仪表本身坏掉不能影响回复，所以整段吞异常。
    """
    try:
        import memory_usage as _mu
        _mu.note("group_facts",
                 [_mu.key_group(e.get("id"), e.get("topic"), e.get("text"))
                  for e in (picked or [])],
                 query=query, injected_chars=chars,
                 candidates=candidates, fallback=fallback, scene="group")
    except Exception:
        pass



