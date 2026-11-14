"""世界知识库：她自己搜到并记下来的东西。

存放 data/memory/world_knowledge.json：
{"topics": [{"key": "明日方舟", "title": "...", "summary": "...", "ts": 1730, "source": "bing"}]}

摘要直接来自搜索结果片段，不经模型改写，避免编造。
"""
import json
import os
import re
import time

_DEFAULT_FILE = os.path.join(os.getenv("DATA_DIR", "./data"), "memory", "world_knowledge.json")
_FILE = _DEFAULT_FILE

def _path():
    return _FILE if _FILE != _DEFAULT_FILE else os.path.join(
        os.getenv("DATA_DIR", "./data"), "memory", "world_knowledge.json")
SUMMARY_MAX = int(os.getenv("KNOWLEDGE_SUMMARY_MAX", "600"))
BLOCK_LIMIT = int(os.getenv("KNOWLEDGE_BLOCK_CHARS", "700"))   # 常驻注入砍半，够了

# 每类各占多少，而不是全局一个数。
# 以前是 MAX_TOPICS=30 全局封顶：搜了几条游戏设定就把 30 格占满，
# 「主人的生日」这类人物信息永远挤不进去 —— 她的知识面会单方面长歪。
DEFAULT_QUOTAS = {
    "作品设定": int(os.getenv("KB_QUOTA_作品设定", "8")),
    "人物": int(os.getenv("KB_QUOTA_人物", "6")),
    "工具做法": int(os.getenv("KB_QUOTA_工具做法", "6")),
    "时事": int(os.getenv("KB_QUOTA_时事", "5")),
    "其他": int(os.getenv("KB_QUOTA_其他", "5")),
}
MAX_TOPICS = sum(DEFAULT_QUOTAS.values())     # 兼容旧引用
# 归档不删，但也不能无限涨（决策：归档 + 仍可检索）
ARCHIVE_MAX = int(os.getenv("KB_ARCHIVE_MAX", "400"))
# 近似重复合并的门槛（词面 Jaccard）。0.62 是照现网那三条
# 「致命公司」的 key 差异定的：标点全半角不同、多两个字，其余一样。
NEAR_DUP = float(os.getenv("KB_NEAR_DUP_JACCARD", "0.62"))
# 一句话都算不上信息量的碎片不入库；但不该用 80 字这种硬门槛
MIN_SUMMARY = int(os.getenv("KB_MIN_SUMMARY_CHARS", "16"))


def _ensure():
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
    except Exception:
        pass


_CACHE = {"sig": None, "items": [], "tokens": {}}


def _file_sig():
    try:
        st = os.stat(_path())
        return (_path(), round(st.st_mtime, 3), st.st_size)
    except Exception:
        return None


def _tok(text):
    """系统唯一分词口径（见 text_tokens）。

    原来注释写着「跟群记忆那边保持一致」，实际只有汉字二元组、
    根本没有 jieba —— 于是专名（「主人」「提弗洛斯」）在知识库里只能碰运气，
    而写入用的 key 和检索用的词又不是同一套。现在三个库共用一份实现。
    """
    try:
        import text_tokens
        return text_tokens.tokens(text)
    except Exception:
        pass
    import re as _re
    t = str(text or "")
    out = set()
    for seg in _re.findall(r"[一-龥]+", t):
        if len(seg) == 1:
            out.add(seg)
        for k in range(len(seg) - 1):
            out.add(seg[k:k + 2])
    for w in _re.findall(r"[A-Za-z0-9]{2,}", t):
        out.add(w.lower())
    return out
def _norm_record(raw):
    """一条知识唯一的规范形状。load() 和 save() 都必须走这里。

    之前两处各自写了一份字段清单，结果 load() 那份漏了 first_seen /
    merged_from / merged_into：写入端辛辛苦苦记下的痕迹，读一次就没了，
    下一次落盘又按「读到的样子」写回去 —— 等于每次读写都在偷偷抹掉合并线索。
    更糟的是这种丢失完全静默，只有回头核对才发现。

    字段清单只留这一份，两边就不可能再各漏各的。
    """
    it = raw if isinstance(raw, dict) else {}
    key = str(it.get("key") or "").strip()
    summ = str(it.get("summary") or "").strip()
    if not key or not summ:
        return None
    ts = float(it.get("ts") or 0)
    st = str(it.get("status") or "").strip()
    ver = it.get("verified")
    return {
        "key": key,
        "title": str(it.get("title") or key)[:80],
        "summary": summ[:SUMMARY_MAX],
        "ts": ts,
        "source": str(it.get("source") or "")[:20],
        # 旧文件里没有这几个字段，读进来必须补齐，
        # 否则配额和归档判断会在老数据上直接 KeyError
        "category": _clean_category(it.get("category")),
        # quarantined 是新状态：隔离不是删除，原文仍留着可以追溯，
        # 但不再进提示词、也不参与检索（见 is_active）。
        "status": ("archive" if st == "archive"
                   else "quarantined" if st == "quarantined" else "active"),
        "provenance": str(it.get("provenance") or it.get("source") or "")[:20],
        # 证据化入库：这条知识是从哪个页面、哪段原文得出的，是否核验过。
        # 以前只存摘要，出了「卫戍协议被记成看门狗」根本说不清来源。
        "evidence_url": str(it.get("evidence_url") or "").strip()[:300],
        "evidence": str(it.get("evidence") or "").strip()[:SUMMARY_MAX],
        "verified": (ver if isinstance(ver, bool) else None),
        "quarantine_reason": str(it.get("quarantine_reason") or "")[:160],
        "quarantined_at": float(it.get("quarantined_at") or 0),
        # 近似重复合并留下的痕迹：这条最早是什么时候记下的、吸收了哪些说法、
        # 以及副本指向哪个正本。
        "first_seen": float(it.get("first_seen") or ts or time.time()),
        "merged_from": str(it.get("merged_from") or "")[:200],
        "merged_into": str(it.get("merged_into") or "")[:40],
    }


def load():
    # 文件没变就直接返回缓存，避免每次回复都重新解析 JSON + 重新分词
    sig = _file_sig()
    if sig is not None and _CACHE["sig"] == sig and _CACHE["items"]:
        return list(_CACHE["items"])
    _ensure()
    try:
        if os.path.isfile(_path()):
            with open(_path(), encoding="utf-8") as f:
                data = json.load(f) or {}
            items = data.get("topics")
            if isinstance(items, list):
                out = []
                for it in items:
                    rec = _norm_record(it)
                    if rec:
                        out.append(rec)
                _CACHE["items"] = out
                _CACHE["sig"] = sig
                _CACHE["tokens"] = {
                    i["key"]: _tok(i["key"]) | _tok(i["summary"]) for i in out}
                return list(out)
    except Exception as e:
        print("[知识库] 读取失败 " + str(e)[:80], flush=True)
        import safe_store
        safe_store.preserve_corrupt(_path())
    return []


def is_active(item):
    """只有 active 的条目才参与注入与检索。

    隔离（quarantined）与归档（archive）都不算 —— 区别只在后者是「过期」，
    前者是「压根不是同一个实体」。
    """
    return str((item or {}).get("status") or "active") == "active"


def active_items():
    return [t for t in load() if is_active(t)]


def save(topics):
    """按类别配额落盘。

    淘汰规则有两条底线：
      1. active 的配额只在 active 内部淘汰，永不被归档条目挤掉；
      2. 归档不删（决策），只在超出 ARCHIVE_MAX 时丢最老的那些。
    """
    _ensure()
    clean = []
    seen = set()
    for it in topics or []:
        # 和 load() 共用一份字段清单（见 _norm_record）：
        # 以前两边各写各的，load() 漏了合并字段，读写一轮痕迹就被抹掉一次
        rec = _norm_record(it)
        if rec is None or rec["key"] in seen:
            continue
        seen.add(rec["key"])
        clean.append(rec)
    # 新的排前面，配额从前往后留
    clean.sort(key=lambda x: -float(x.get("ts") or 0))
    active, archive, quarantined = [], [], []
    kept = {c: 0 for c in DEFAULT_QUOTAS}
    for it in clean:
        if it["status"] == "archive":
            archive.append(it)
            continue
        if it["status"] == "quarantined":
            # 隔离条目不占 active 配额，也不降级成 archive：
            # 它的问题是「不是同一个实体」，不是「过期了」。
            quarantined.append(it)
            continue
        cap = DEFAULT_QUOTAS.get(it["category"], DEFAULT_QUOTAS["其他"])
        if kept.get(it["category"], 0) >= cap:
            # 超配额的降为归档，不丢。
            # 注意必须把 status 也改掉：以前只是放进了 archive 这个桶，
            # 写盘时它自己还写着 active —— 于是配额看起来有、实际完全不生效。
            demoted = dict(it)
            demoted["status"] = "archive"
            archive.append(demoted)
            continue
        kept[it["category"]] = kept.get(it["category"], 0) + 1
        active.append(it)
    if len(archive) > ARCHIVE_MAX:
        archive = sorted(archive, key=lambda x: -float(x.get("ts") or 0))[:ARCHIVE_MAX]
    final = active + archive + quarantined
    import safe_store
    return safe_store.atomic_write_json(_path(), {"topics": final})


# 知识库的键必须是「话题」，不能是群聊原话。
_CHAT_KEY = re.compile(
    r"^(在|你|我|他|她|它|这|那|哪|谁|什|怎么|为什么|嗯|啊|哦|呀|诶|哈|嘿)"
    r"|呢$|吗$|吧$|啊$|呀$|哦$|啦$|嘛$|捏$"
    r"|^(妈妈|爸爸|宝宝|主人|老婆|老公)$"
)


def _bad_topic(key):
    """判断这个键是不是群聊口水话，而不是一个话题。

    以前这里写的是「len(k) < 4 就算坏键」，于是「荒坂塔」这类三字专名被拒之门外 ——
    和「摘要必须满 80 字」是同一类毛病：拿长度当质量。
    长度只用来挡单字；噪声交给 _CHAT_KEY（口水开头）与 _JUNK_KEY（一两个字的碎词）。
    """
    k = str(key or "").strip()
    if len(k) < 2 or len(k) > 40:
        return True
    if _CHAT_KEY.search(k):
        return True
    if not re.search(r"[\w一-鿿]", k, re.UNICODE):
        return True
    return False


_STOP = set("还是 的 了 吗 呢 啊 呀 吧 我们 你们 他们 这个 那个 什么 怎么 可以".split())


def looks_relevant(query, title, summary):
    """这条结果真的跟她查的东西有关吗。

    搜索框经常把闲聊整句当查询词，"还是个傲娇" 会返回《说文解字》里"还是"的虚词解释。
    判断办法：把查询里的实词拿出来，要求标题或摘要里有重合。

    以前这里用 `[\\u4e00-\\u9fa5]{2,}` 把整串中文当成「一个词」，
    于是查询「荒坂塔原型」去匹配正文里的「荒坂塔」—— 整串不在正文，判成毫不相干，
    她查到的东西就这样被丢掉。现在走系统统一分词（词 + 二元组），
    子串重合算数，虚词也已被停用词表滤掉。
    """
    q = str(query or "").strip()
    if not q:
        return False
    body = (str(title or "") + " " + str(summary or ""))[:600]
    try:
        import text_tokens as _tt
        qt = _tt.tokens(q)
        bt = _tt.tokens(body)
        if qt and bt:
            return len(qt & bt) > 0
    except Exception:
        pass
    # 兜底：新模块出问题时退回旧写法，至少还能挡住明显的噪声
    words = [w for w in re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}", q)
             if w not in _STOP]
    if not words:
        return False
    return any(w in body for w in words)


def _content_set(text):
    """一条知识的「实词集合」，用来找近似重复。走统一分词器，不自己切。"""
    try:
        import text_tokens as _tt
        words = set(_tt.content_words(text))
        if words:
            return words
        return set(_tt.tokens(text))
    except Exception:
        return set()


def find_near_dup(key, title, items=None, threshold=None):
    """库里是不是已经有一条基本同一个问题。

    现网真实形状：「游戏《致命公司》(Lethal Company)怎么玩」和
    「游戏《致命公司》（Lethal Company）玩法」各占一条，来源还是同一个
    硬件检测网站 —— 她每次换个说法问，key 就换一个，同一条知识被记了三遍，
    配额于是被重复条目吃掉，真正的新东西反而挤不进来。

    判据是**词面**的 Jaccard，不是语义：宁可漏合，不可错合。
    漏合只是多占一格，错合会把两条不同的知识变成一条。
    """
    th = NEAR_DUP if threshold is None else float(threshold)
    a = _content_set("%s %s" % (key or "", title or ""))
    if len(a) < 2:
        return None          # 太短的一条没有词面可比，别拿它去合并别人
    best, best_j = None, 0.0
    for it in (items if items is not None else load()):
        k2 = str(it.get("key") or "")
        if not k2 or k2 == str(key or "").strip():
            continue
        if str(it.get("status") or "active") == "quarantined":
            continue        # 隔离条目不再参与「近似重复」配对
        b = _content_set("%s %s" % (k2, it.get("title") or ""))
        if len(b) < 2:
            continue
        shared = len(a & b)
        if shared < 2:
            continue
        j = shared / float(len(a | b) or 1)
        if j > best_j:
            best, best_j = it, j
    return best if best_j >= th else None


def merge_duplicates(dry_run=True, threshold=None):
    """把历史上那些「换个说法再问一次」的重复条目合起来。

    只做新写入的合并是不够的：现网已经攒了 3 条「致命公司」、3 条「主人」、
    2 条「赤合金」，配额被重复条目占着，注入时也反复说同一件事。

    口径跟这个项目的其它老化决定一致 —— **合并＝归档，不是删除**：
    留下最好的那条当正本，其余改成 archive 并写上 merged_into，
    仍然能被显式检索到、也能复活。直接删等于毁掉她真的记过的东西。

    正本怎么挑：active 优先 > 更新时间新 > 摘要更长。
    幂等：跑第二遍不会再把已经归档过的再动一次。
    """
    items = load()
    # 已经归档并标明「并进了哪条」的副本不再参与：
    # 否则每次重跑都会拿它们再配一次对，跑第二遍还能再 archive 一条，
    # 「幂等的回填」当场就不成立了
    dupes_already = set(
        str(it.get("key") or "") for it in items
        if str(it.get("status") or "active") == "archive"
        and str(it.get("merged_into") or ""))
    ranked = sorted([it for it in items
                     if str(it.get("key") or "") not in dupes_already
                     and str(it.get("status") or "active") != "quarantined"],
                    key=lambda x: (
                        0 if str(x.get("status") or "active") == "active" else 1,
                        -float(x.get("ts") or 0),
                        -len(str(x.get("summary") or "")),
                    ))
    keep, absorbed = [], {}
    for it in ranked:
        dup = find_near_dup(it.get("key"), it.get("title"), keep, threshold)
        if dup is None:
            keep.append(it)
            continue
        dk = str(dup.get("key") or "")
        if dk in absorbed:
            absorbed[dk].append(str(it.get("key") or ""))
            continue
        absorbed[dk] = [str(it.get("key") or "")]
        # 正本保留最早那次真的记下来的时间，合并痕迹记下被并掉的键
        keep = [x for x in keep if str(x.get("key")) != dk]
        best = dict(dup)
        best["first_seen"] = min(float(dup.get("first_seen") or dup.get("ts") or 0),
                                 float(it.get("first_seen") or it.get("ts") or 0))
        best["merged_from"] = ",".join(absorbed[dk])[:200]
        keep.append(best)
    plan = [{"keep": k, "absorbed": v} for k, v in absorbed.items()]
    if dry_run:
        return {"dry_run": True, "total": len(items), "kept": len(keep),
                "would_archive": sum(len(v) for v in absorbed.values()),
                "plan": plan}

    if not plan:
        return {"dry_run": False, "total": len(items), "kept": len(keep),
                "archived": 0, "plan": []}
    by_key = {str(it.get("key") or ""): it for it in items}
    # 只报这一轮真的被归档了几条。必须在改动之前先把「原本就归档的」记下来：
    # 下面那个循环改的就是 items 里同一批对象，事后再看两边集合是一样的，
    # 结果合并明明做了、计数却永远是 0（看起来像「回填没起作用」）。
    was_archived = set(
        str(it.get("key") or "") for it in items
        if str(it.get("status") or "active") == "archive")
    for k, v in absorbed.items():
        keeper = by_key.get(k)
        if not isinstance(keeper, dict):
            continue
        # 改的是 by_key 里那个对象本身（它就是待写盘的那条），
        # 之前写在一个 dict 副本上，合并痕迹根本没落到盘里
        earliest = min([float(keeper.get("first_seen") or keeper.get("ts") or 0)] +
                       [float((by_key.get(d) or {}).get("first_seen")
                              or (by_key.get(d) or {}).get("ts") or 0)
                        for d in v if d in by_key])
        if earliest:
            keeper["first_seen"] = earliest
        keeper["merged_from"] = ",".join(v)[:200]
        for dup_key in v:
            rec = by_key.get(dup_key)
            if isinstance(rec, dict):
                rec["status"] = "archive"
                rec["merged_into"] = str(k)[:40]
    # 这一轮真的新归档了几条 = 现在归档的 - 循环前就已经归档的。
    # was_archived 必须在改动之前取（见上面），这里只算「现在」这一边。
    now_archived = set(
        str(it.get("key") or "") for it in items
        if str(it.get("status") or "active") == "archive")
    newly = len(now_archived - was_archived)
    ok = save(items)
    return {"dry_run": False, "ok": ok, "total": len(items),
            "kept": len([x for x in items
                         if str(x.get("status") or "active") == "active"]),
            "archived": newly, "plan": plan}


def remember(key, title, summary, source="", category="", status=None,
             evidence_url="", evidence="", verified=None):
    """写入或更新一条知识（同 key 覆盖）。

    门禁只留「机械可判」的那几条：口水话题、词典/下载页噪声、查询与结果毫不相干。
    原来那条 `len(summary) < 80` 是拍脑袋的硬门槛，把「荒坂塔是什么」这种
    四十来字的正经知识全挡在外面 —— 换成 MIN_SUMMARY 的碎片下限。
    时效性内容（天气/股价）不再丢弃，改成归档：不进提示词，但仍在库里查得到。

    evidence_url / evidence / verified：证据化入库。哪一页、哪段原文、核没核过，
    跟着条目一起落盘；出了「卫戍协议被记成看门狗」才追得回来源。
    """
    if not key or not summary:
        return False
    # 质量门禁：不像话题的不进库
    if _bad_topic(key):
        return False
    if _JUNK_KEY.match(str(key or "")):
        return False
    # 查询词和结果完全不搭（搜索框把闲聊当成了查询）——不入库
    if not looks_relevant(key, title, summary):
        return False
    if _JUNK_TEXT.search(str(summary or "")[:200]):
        return False
    summ = str(summary or "").strip()
    if len(summ) < MIN_SUMMARY:
        return False
    _transient = _is_transient({"key": key, "title": title})
    if _transient:
        _st = "archive"
    elif status in ("active", "archive"):
        _st = status
    else:
        _st = "active"
    _cat = _clean_category(category)
    _all = load()
    # 同一 key 再写一次时，没带新证据不要把旧证据抹掉：
    # 判定通道/兜底路径调用 remember 时不传 evidence，抹掉就再也追不回来源。
    _prev = next((t for t in _all
                  if t.get("key") == str(key).strip()), None) or {}
    # 同一条知识换个说法再问一次，不该再多占一格配额（见 find_near_dup）
    _dupe = find_near_dup(key, title, _all)
    topics = [t for t in _all
              if t.get("key") != key
              and not (_dupe is not None and t.get("key") == _dupe.get("key"))]
    _first_ts = (float(_dupe.get("first_seen") or _dupe.get("ts") or 0)
                 if _dupe else 0.0) or time.time()
    topics.append({
        "key": str(key).strip(),
        "title": str(title or key),
        "summary": summ[:SUMMARY_MAX],
        "ts": time.time(),
        "source": str(source or "")[:20],
        "category": _cat,
        "status": _st,
        "provenance": str(source or "")[:20],
        "evidence_url": str(evidence_url or _prev.get("evidence_url") or "").strip()[:300],
        "evidence": str(evidence or _prev.get("evidence") or "").strip()[:SUMMARY_MAX],
        "verified": (verified if isinstance(verified, bool)
                     else _prev.get("verified")),
        "first_seen": _first_ts,
        "merged_from": str(_dupe.get("key") or "")[:40] if _dupe else "",
    })
    return save(topics)


def raw_items():
    """直接读文件里的原始条目（不补默认值）。

    回填类别要靠它：load() 会把缺失的 category 补成「其他」，
    于是「本来就没类别的老条目」和「她判过确实是其他的」看起来一样，
    回填就会每轮重判一遍。
    """
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
        items = data.get("topics")
        return [x for x in (items or []) if isinstance(x, dict)]
    except Exception:
        return []


def update_meta(key, category=None, status=None, extra=None):
    """只改元信息：类别、状态、附加字段。内容、时间戳、来源一律不动。

    回填走这里而不是 remember —— remember 会重跑门禁、刷新 ts，
    那等于把「她什么时候查到的」这条信息抹掉了。
    """
    items = raw_items()
    hit = False
    for it in items:
        if str(it.get("key") or "").strip() != str(key or "").strip():
            continue
        if category:
            it["category"] = _clean_category(category)
        if status in ("active", "archive"):
            it["status"] = status
        if isinstance(extra, dict):
            for k, v in extra.items():
                it[k] = v
        hit = True
        break
    if not hit:
        return False
    return save(items)


def quarantine(key, reason=""):
    """把一条知识隔离：不进提示词、不进检索，但原文留着可追溯。

    为什么不是删除：她确实查到过那些字，删了就再也说不清
    「为什么当时会把《看门狗：军团》记成卫戍协议」。隔离只让它不再参与判断。
    返回 True 表示盘上确实有这样一条并已改状态。
    """
    k = str(key or "").strip()
    if not k:
        return False
    items = raw_items()
    hit = False
    for it in items:
        if str(it.get("key") or "").strip() != k:
            continue
        it["status"] = "quarantined"
        it["quarantine_reason"] = str(reason or "")[:160]
        it["quarantined_at"] = time.time()
        hit = True
    if not hit:
        return False
    return save(items)


_GARRISON_HINT = re.compile(r"garrison\s*protocol|看门狗", re.I)


def migrate_quarantine_garrison(dry_run=True):
    """把「卫戍协议被写成了看门狗军团」的历史记录隔离。

    判据两条同时成立才动：
      · key 含「卫戍」；
      · 标题/摘要/证据里出现「Garrison Protocol」或「看门狗」。
    默认 dry_run=True，且**不会自动执行** —— 由主线程/维护者看完报告再决定。
    幂等：已经 quarantined 的不再计数。
    """
    plan_rows = []
    for it in raw_items():
        key = str(it.get("key") or "")
        if "卫戍" not in key:
            continue
        blob = " ".join(str(it.get(f) or "") for f in
                        ("title", "summary", "evidence", "evidence_url",
                         "source", "provenance"))
        if not _GARRISON_HINT.search(blob):
            continue
        if str(it.get("status") or "active") == "quarantined":
            continue
        plan_rows.append({"key": key,
                          "title": str(it.get("title") or "")[:60],
                          "status": str(it.get("status") or "active")})
    if dry_run:
        return {"dry_run": True, "matched": len(plan_rows),
                "plan": plan_rows, "quarantined": 0}
    n = 0
    for row in plan_rows:
        if quarantine(row["key"], reason="实体不符：卫戍协议≠看门狗军团"):
            n += 1
    return {"dry_run": False, "matched": len(plan_rows),
            "plan": plan_rows, "quarantined": n}


def _clean_category(category):
    """类别只认表里那几个，别让她或模型随口造的词把配额打散。"""
    c = str(category or "").strip()
    if c in DEFAULT_QUOTAS:
        return c
    return "其他"


_JUDGE_PROMPT = (
    "你查到了下面这条东西。判一下它值不值得长期记住。\n\n"
    "查询：%s\n标题：%s\n内容：%s\n\n"
    "只输出一个 JSON，不要解释：\n"
    "{\"keep\": true 或 false, \"category\": \"作品设定|人物|工具做法|时事|其他\" 里挑一个}\n"
    "规则：\n"
    "- 作品里的设定、人物、地名、机制，某个人做过什么、喜欢什么，怎么做到某件事："
    "这些都是**以后还会用上的**，keep=true，别因为「跟今天的聊天没关系」就说不值得记；\n"
    "- 她自己也好奇、主动去查的那些（查询词像是个专业名词或作品名）：一定 keep=true；\n"
    "- 只在今天成立的事（天气、股价、比分、今天几号）：category 用「时事」，"
    "keep 可以 true，它会进归档而不是常驻提示词；\n"
    "- 词典释义、下载页、跟查询毫不相干的句子、什么都没有的空话：keep=false；\n"
    "- 判不准的时候偏向 true —— 少一条知识比多一条噪音更难补回来。"
)


async def judge_and_remember(key, title, summary, source="", llm=None):
    """值不值得记、算哪一类 —— 让她判一次，而不是靠字数门槛猜。

    判不了也必须有个结果：没给 llm、llm 抛异常、输出不是 JSON，
    全都退回「按机械门禁该记就记、类别归其他」。
    静默不写是最坑的：库会一直空着，还看不出为什么空。
    """
    _t = str(title or "")
    _s = str(summary or "")
    if llm is None:
        return {"saved": remember(key, _t, _s, source=source), "keep": None,
                "category": "", "reason": "没有判定通道，按机械门禁写"}
    try:
        raw = await llm(_JUDGE_PROMPT % (str(key or "")[:40], _t[:60], _s[:400]))
    except Exception as e:
        return {"saved": remember(key, _t, _s, source=source), "keep": None,
                "category": "", "reason": "判定调用失败 " + type(e).__name__}
    text = str(raw or "").strip()
    # 她拒绝的写法沿用全项目那套标记（[[沉默]] / [[不补]] / [[不查]]），
    # 只认 JSON 的话，她说「不记」会被当成解析失败、于是照记不误
    if "[[不记]]" in text or text in ("不记", "不用记", "别记"):
        return {"saved": False, "keep": False, "category": "", "reason": "她认为不值得记"}
    fence = chr(96) * 3
    text = text.replace(fence + "json", "").replace(fence, "").strip()
    a, b = text.find("{"), text.rfind("}")
    got = {}
    if a >= 0 and b > a:
        try:
            got = json.loads(text[a:b + 1])
        except Exception:
            got = {}
    if not isinstance(got, dict) or "keep" not in got:
        # 输出不成形：宁可按机械门禁写下去，也不要因为判不回来就不记
        return {"saved": remember(key, _t, _s, source=source), "keep": None,
                "category": "", "reason": "判定输出无法解析，按机械门禁写"}
    if not got.get("keep"):
        return {"saved": False, "keep": False, "category": "", "reason": "她认为不值得记"}
    cat = _clean_category(got.get("category"))
    ok = remember(key, _t, _s, source=source, category=cat)
    return {"saved": bool(ok), "keep": True, "category": cat, "reason": ""}


def get(key, include_quarantined=False):
    for t in load():
        if t.get("key") != key:
            continue
        if not include_quarantined and str(t.get("status") or "active") == "quarantined":
            continue
        return t
    return None


# 瞬时信息不该沉淀成长期知识：天气会过期，放进就是污染 prompt
# 关键词太短的搜索结果基本都是垃圾。
# 搜「清漪」会匹配到「清」字 → 明清史；搜「干嘛好」会匹配到「干」→ 干戈的解释。
# 这类东西进了库就会污染她的自我认知（她一度以为"清漪"是隔壁群的别人）。
_JUNK_KEY = re.compile(r"^\s*[^\s]{1,2}\s*$")
_JUNK_TEXT = re.compile(
    # 技术问答 / 下载页
    r"Win11|Windows|内核隔离|注册表|驱动程序|安装包|破解|下载地址|下载链接"
    r"|点击下载|官网下载|安卓版|ios|apk"
    # 汉字词典释义：搜「晚上吃什么」会返回「晚上」的词性解释，纯噪声。
    # 这些不是她想查的东西，进了提示词只会挤掉她自己的人格。
    r"|此字始见于|据《说文解字》|汉语词语|汉语常用字|拼音为|注音|部首是|笔画|本义是|引申有"
    r"|甲骨文|金文|形声字|会意字|读作|一级字|二级字|繁体|简体字|古籍解释|康熙字典"
)

_TRANSIENT = re.compile(
    r"天气|气温|下雨|带伞|几度|多少度|降温|升温|台风|晴|阴|多云"
    r"|股价|汇率|比分|赛程|热搜|票房|实时"
)


def _is_transient(item):
    t = str(item.get("key") or "") + " " + str(item.get("title") or "")
    return bool(_TRANSIENT.search(t))


def build_block(max_chars=None, query=""):
    """注入提示词用。

    query 非空时优先放相关的条目（按关键词命中）。
    """
    topics = [t for t in load() if is_active(t) and not _is_transient(t)]
    if not topics:
        # 库是空的 —— 和「有库但没查中」是两回事，必须分开记，
        # 否则「空命中率」会把写入门禁的问题掩盖成检索的问题。
        try:
            import memory_usage as _mu
            _mu.note("world_knowledge:store_empty", [], query=query,
                     injected_chars=0, candidates=0, scene="any")
        except Exception:
            pass
        return ""
    limit = int(max_chars or BLOCK_LIMIT)

    q = str(query or "")
    if q:
        qtok = _tok(q)

        def score(t):
            key = str(t.get("key") or "")
            if key and key in q:
                return 99            # 话题名整词命中，直接排最前
            tk = _CACHE["tokens"].get(key) or _tok(key) | _tok(t.get("title") or "")
            return len(qtok & tk) * 10

        topics = sorted(topics, key=lambda t: (-score(t), -float(t.get("ts") or 0)))
    else:
        topics = sorted(topics, key=lambda t: -float(t.get("ts") or 0))

    lines = []
    used = 0
    _picked = []
    _fb = False
    for t in topics:
        if q and score(t) <= 0:
            continue
        # 每条只截 90 字——搜索结果原文动辄几百字，
        # 全塞进去会把她自己的话挤到 prompt 末尾，反而被忽略。
        body = " ".join(str(t.get("summary") or "").split())[:90]
        key = str(t.get("key") or "").strip()
        # 正文和标题都空就别占位了——之前会注入一条只有「- 」的空条目
        if not body and not key:
            continue
        row = "- " + (("【" + key[:16] + "】") if key else "") + body
        if len(row) < 4:
            continue
        if used + len(row) > limit:
            break
        lines.append(row)
        _picked.append(key or body[:24])
        used += len(row)
        if len(lines) >= 2:      # 最多两条，够用就行
            break
    if not lines:
        try:
            import memory_usage as _mu
            _mu.note("world_knowledge", [], query=query, injected_chars=0,
                     candidates=len(topics), fallback=False, scene="any")
        except Exception:
            pass
        return ""
    try:
        import memory_usage as _mu
        _mu.note("world_knowledge", [_mu.key_kb(k) for k in _picked],
                 query=query, injected_chars=used,
                 candidates=len(topics), fallback=_fb, scene="any")
    except Exception:
        pass
    verified = all(t.get("verified") is True for t in topics[:len(lines)])
    heading = "【你自己查过并记住的事】" if verified else "【知识参考 · 含未核验条目】"
    note = "已核验条目可据实使用；未核验内容保留来源边界，不确定的细节别硬编。"
    return "\n\n" + heading + "\n" + "\n".join(lines) + "\n（" + note + "）"
