"""人格自进化：让她从所有对话里长出自己的说话方式、喜好和经历。

与 whale_persona.py 的分工：
- whale_persona.py 是"出生设定"，固定不变。
- 本模块是"成长档案"，由对话持续塑造，写在 data/persona/self.json。

数据流：
  每次回复 -> note_exchange() 追加到 journal.jsonl
  每小时   -> consolidate() 取新对话，交给 LLM 合并进 self.json
  每次开口 -> build_block() 把成长档案注入 system prompt
"""
import json
import re
import os
import time

# 路径在调用时解析，不在 import 时冻结。
# 冻过一次：一个测试进程里两个用例各带自己的 DATA_DIR，第二个用例
# 却还在往第一个（已经被删掉）的档案上写，于是「她变了没有」永远比出「没变」——
# 自我认识修订记录就这样悄悄不写了。
def _dir():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "persona")


def self_file():
    return os.path.join(_dir(), "self.json")


def journal_file():
    return os.path.join(_dir(), "journal.jsonl")

# 【不设上限】她的人格档案是她的回忆，一条都不该丢。
# 历史上这里曾有 MAX_* 上限，超出就 FIFO 挤掉最老的——那等于删记忆，已取消。
# 唯一的约束在「进提示词」那一步（BLOCK_MAX_CHARS），存储端永久保留。
ARCHIVE_LIMIT = 0            # 0 = 无上限；给统一语义用
JOURNAL_KEEP = 0             # 0 = journal 不裁剪
BLOCK_MAX_CHARS = int(os.getenv("PERSONA_BLOCK_MAX_CHARS", "900"))
CONSOLIDATE_MIN_ITEMS = int(os.getenv("PERSONA_MIN_ITEMS", "3"))

_DEFAULTS = {
    "version": 1,
    "revision": 0,
    "updated": 0,
    "last_consolidated": 0,
    "voice": {"口癖": [], "句式": [], "语气": ""},
    "likes": [],
    "dislikes": [],
    "curious": [],
    "experiences": [],
    "facts": [],
    "self_view": "",
    "stats": {"consolidations": 0, "exchanges": 0},
}


def _ensure():
    try:
        os.makedirs(_dir(), exist_ok=True)
    except Exception:
        pass


def load():
    """读档案。存储已迁到 SQLite（persona.db），JSON 只当备份/导出。"""
    data = dict(_DEFAULTS)
    try:
        import persona_store as _ps
        snap = _ps.snapshot()
        for k, v in snap.items():
            if k.startswith("_"):
                continue
            data[k] = v
    except Exception as e:
        print("[人格] 读库失败，退回 JSON: " + str(e)[:70], flush=True)
        try:
            if os.path.isfile(self_file()):
                with open(self_file(), encoding="utf-8") as f:
                    got = json.load(f) or {}
                if isinstance(got, dict):
                    for k, v in got.items():
                        data[k] = v
        except Exception:
            pass
    import self_model
    data['self_view'] = self_model.consumable_self_view(data.get('self_view'))
    return data

def save(data):
    """写档案。列表类由 record_* 增量写库，这里只同步 meta + 导出一份 JSON。"""
    _ensure()
    import self_model
    data = dict(data)
    if 'self_view' in data:
        data['self_view'] = self_model.consumable_self_view(data['self_view'])
    ok = False
    try:
        import persona_store as _ps
        for k in ("self_view", "voice", "stats", "last_consolidated",
                  "revision", "updated"):
            if k in data:
                _ps.set_meta(k, data[k])
        import self_model
        self_model.revise_layer('personality',
                                {k: data[k] for k in ('self_view', 'voice') if k in data},
                                'persona save')
        ok = True
    except Exception as e:
        print("[人格] 写库失败 " + str(e)[:70], flush=True)
    try:
        tmp = self_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self_file())
    except Exception:
        pass
    return ok


def _clean_list(items, limit=0):
    """清洗列表。limit<=0 表示不限条数（存储端永不裁剪）。"""
    out = []
    for it in items or []:
        s = str(it or "").strip().strip("-·• ").strip()
        if not s or len(s) > 120:
            continue
        if s in out:
            continue
        out.append(s)
        if limit and limit > 0 and len(out) >= limit:
            break
    return out


def note_exchange(session_key, user_text, bot_text, events=(), *, speaker_id="",
                  speaker_name="", target_uid="", message_event_id=""):
    """记一条真实发生的对话，供每小时整理时学习。

    events 是那几句对话对应的事件号。整理时她会说「我酸的时候会直接说出口」，
    要回答「这话是从哪儿来的」，靠的就是这几个号 ——
    只挂一条「整点整理过」的事件，等于什么都没记（线上 6/6 条修订都是这样）。
    """
    try:
        u = str(user_text or "").strip()[:300]
        b = str(bot_text or "").strip()[:300]
        if not u and not b:
            return
        _ensure()
        row = {"ts": time.time(), "session": str(session_key or "")[:40], "user": u, "bot": b}
        for key, value in (("speaker_id", speaker_id), ("speaker_name", speaker_name),
                           ("target_uid", target_uid), ("message_event_id", message_event_id)):
            if value:
                row[key] = str(value)[:80]
        ev = [str(x) for x in (events or []) if str(x)]
        if ev:
            row["events"] = ev[:3]
        with open(journal_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _trim_journal()
    except Exception as e:
        print("[人格] 记对话失败 " + str(e)[:60], flush=True)


def _trim_journal():
    try:
        if JOURNAL_KEEP <= 0:
            return          # 0 = 不裁剪。以前少了这一句：lines[-0:] 等于全量，
                            # 于是每条回复都把整本 journal 读出来重抄一遍，还在发话路径上。
        if not os.path.isfile(journal_file()):
            return
        with open(journal_file(), encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= JOURNAL_KEEP:
            return
        with open(journal_file(), "w", encoding="utf-8") as f:
            f.writelines(lines[-JOURNAL_KEEP:])
    except Exception:
        pass


# 测试探针会话：自动化测试留下的对话不是她的经历，不能吃进档案。
# 踩过的坑：我用 "某群号" 当会话 key 测语音，输入了「我今天面试挂了」，
# 结果被归纳成「主人今天面试挂了」——凭空给她编了一段主人的经历。
_PROBE_SESSION = re.compile(r"^(p-|probe|p\d+$|t-|test)")


def read_new_journal(since_ts):
    """取 last_consolidated 之后的新对话。"""
    rows = []
    try:
        if not os.path.isfile(journal_file()):
            return rows
        with open(journal_file(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if str(r.get("session") or "") and _PROBE_SESSION.match(str(r.get("session"))):
                    continue        # 测试会话，跳过
                if float(r.get("ts") or 0) > float(since_ts or 0):
                    rows.append(r)
    except Exception as e:
        print("[人格] 读日志失败 " + str(e)[:60], flush=True)
    return rows


def _pick_relevant(items, query, keep=5):
    """按当前话题挑最相关的几条。走 SQLite 倒排，不再线性扫字符串。"""
    arr = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not arr:
        return []
    q = str(query or "").strip()
    if not q:
        return arr[-keep:]
    try:
        import persona_store as _ps
        picked = _ps.search_within(arr, q, keep)
        if picked:
            return picked[:keep]
    except Exception as e:
        print("[人格] 检索失败，退回末尾取: " + str(e)[:60], flush=True)
    return arr[-keep:]


def build_block(max_chars=None, query=""):
    """把成长档案压成可注入的短文本。存储不限量，这里只挑相关的进提示词。"""
    d = load()
    limit = int(max_chars or BLOCK_MAX_CHARS)
    parts = []

    # voice 保留在档案供回看，不把历史输出再次变成表达指令（口癖自强化）。

    likes = _clean_list(d.get("likes"), 10)
    dislikes = _clean_list(d.get("dislikes"), 6)
    curious = _clean_list(d.get("curious"), 8)
    if likes:
        parts.append("- 你自己喜欢：" + "、".join(likes))
    if dislikes:
        parts.append("- 你自己不喜欢：" + "、".join(dislikes))
    if curious:
        parts.append("- 你最近在意：" + "、".join(curious))

    # 愿望派生的条目已经在愿望墙里了，这里剔掉避免重复（省 ~500 字/轮）
    def _not_wish(x):
        t = str(x or "")
        return "愿望「" not in t and "愿望墙" not in t and "→ 主人回" not in t

    exp = [x for x in _clean_list(d.get("experiences"), 0) if _not_wish(x)]
    if exp:
        parts.append("- 你经历过：" + "；".join(_pick_relevant(exp, query, 5)))

    facts = [x for x in _clean_list(d.get("facts"), 0) if _not_wish(x)]
    if facts:
        parts.append("- 你查到/记住的事：" + "；".join(_pick_relevant(facts, query, 5)))

    self_view = str(d.get("self_view") or "").strip()
    if self_view:
        parts.append("- 你怎么看自己：" + self_view[:150])

    if not parts:
        return ""

    head = "\n\n【成长记忆背景 · 旧记录仅作背景，不覆盖当前声纹与边界；缺证据的旧事不视为已核验事实】\n"
    text = head + "\n".join(parts)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text + "\n（这些是你自己的偏好和经历，可以自然流露，但别逐条背出来。）"


# ── 语料库隔离：书里的设定不许进人格档案 ──
# 她读写小说只是取材，不是变成书里的人。这些词一旦出现在「经历/事实」里，
# 说明那轮对话是创作或扮演，不该沉淀成她的人生。
_BOOK_TAINT = re.compile(
    r"公主|殿下|陛下|臣妾|本座|师尊|师父|师兄|师姐|师弟|师妹"
    r"|仙门|仙尊|灵根|筑基|金丹|元婴|江湖|门派|武林"
    r"|王朝|皇帝|太子|王爷|朝代|年号|科举|将军|丞相"
    r"|异世界|魔法|精灵|魔族|半兽人|女仆长"
)
_BOOK_ALLOW = re.compile("群里|主人"
    + ("|" + "|".join(re.escape(x) for x in os.getenv("PERSON_BOOK_TOKENS", "").split(",") if x.strip())
       if os.getenv("PERSON_BOOK_TOKENS", "").strip() else ""))


# 愿望派生的条目已经在愿望墙里了，不该再占人格档案的配额
_WISH_JUNK = re.compile(
    r"愿望「|愿望墙|→ 主人回|她说「|往愿望墙上写|关于愿望"
)


# ── 临时事件过滤 ───────────────────────────────────────
# 「阿乙正在尝试使用夸克网盘」这种记下来毫无用处。
# 她该记的是谁跟谁玩、她喜欢跟谁玩、对谁示了好感——
# 能塑造人格的东西，不是某人在用哪个软件。
_TOOL_WORD = re.compile(
    r"网盘|界面|软件|插件|客户端|浏览器|登录|部署|安装|更新|版本"
    r"|报错|崩溃|卡顿|下载|上传|接口|脚本|代码|服务器|配置|设置|功能"
)
_ONGOING = re.compile(r"正在|还在|刚刚在|今天在|尝试使用|尝试用")
_WARMTH = re.compile(
    r"喜欢|讨厌|害怕|心疼|开心|难过|委屈|生气|撒娇|护着|护短"
    r"|夸(?!克)|抱|亲|陪|答应|承诺|约定|想他|想她|舍不得|依赖|信任"
)


def _is_transient_event(text):
    """这条是不是「临时事件 / 技术琐事」，不该进她的人格。"""
    t = str(text or "").strip()
    if not t:
        return True
    # 带感情色彩的一律留住——哪怕提到工具，那也是她和某人之间的事
    if _WARMTH.search(t):
        return False
    if _TOOL_WORD.search(t):
        return True
    if _ONGOING.search(t):
        return True
    return False


def _is_wish_junk(text):
    """这条是不是从愿望墙派生的垃圾。"""
    return bool(_WISH_JUNK.search(str(text or "")))


def _book_tainted(text):
    """判断这条内容是不是被书里设定污染了。"""
    t = str(text or "")
    if not t:
        return False
    if not _BOOK_TAINT.search(t):
        return False
    # 同时提到真实群友的，多半是在拿角色开玩笑，放行
    if _BOOK_ALLOW.search(t):
        return False
    return True


def record_fact(text):
    """记一条通过搜索学到的事实。"""
    s = str(text or "").strip()
    if not s:
        return
    # 语料库隔离：书里的设定不进她的人生
    if _book_tainted(s):
        print("[人格] 拦下一条疑似书本设定: " + s[:40], flush=True)
        return
    # 愿望残渣：愿望墙那边已经有了，别再占配额
    if _is_wish_junk(s):
        print("[人格] 拦下一条愿望残渣: " + s[:40], flush=True)
        return
    # 临时事件 / 技术琐事：记下来对人格没帮助
    if _is_transient_event(s):
        print("[人格] 拦下一条临时事件: " + s[:40], flush=True)
        return
    # 增量写库：只插这一条，不再全量读写
    try:
        import persona_store as _ps
        _ps.add("facts", s[:120])
        _ps.set_meta("updated", time.time())
        return True
    except Exception as e:
        print("[人格] 写事实失败 " + str(e)[:60], flush=True)


def record_experience(text):
    """记一条经历（例如给主人唱了歌）。"""
    s = str(text or "").strip()
    if not s:
        return
    # 语料库隔离：书里的设定不进她的人生
    if _book_tainted(s):
        print("[人格] 拦下一条疑似书本设定: " + s[:40], flush=True)
        return
    # 愿望残渣：愿望墙那边已经有了，别再占配额
    if _is_wish_junk(s):
        print("[人格] 拦下一条愿望残渣: " + s[:40], flush=True)
        return
    # 临时事件 / 技术琐事：记下来对人格没帮助
    if _is_transient_event(s):
        print("[人格] 拦下一条临时事件: " + s[:40], flush=True)
        return
    try:
        import persona_store as _ps
        _ps.add("experiences", s[:120])
        _ps.set_meta("updated", time.time())
        return True
    except Exception as e:
        print("[人格] 写经历失败 " + str(e)[:60], flush=True)


_BAD_SELFVIEW = re.compile("工具人|AI助手|人工智能|语言模型|大模型|机器人|程序|代码|助手|工具")


# 每小时整理允许吸收哪些字段、每轮最多吸收几条。
# 口癖/句式/语气走 voice（save 会写进 meta）；其余是列表类，**必须走 persona_store**——
# save() 只同步 meta 和导出 JSON，列表不写库就会在下次 load 时凭空消失。
_MERGE_LIST_CAPS = {"likes": 3, "dislikes": 3, "curious": 3, "experiences": 5, "facts": 6}
_MERGE_NORM = re.compile(r"[\s，。、；;：:！!？?“”\"'（）()【】.·~～]")


def _norm_mem(x):
    return _MERGE_NORM.sub("", str(x or "")).lower()


_SENTENCE_SPLIT = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]?")
# 定位原句至少要两个共同词。实测：相关命题（「猫打呼噜是因为喉咙在震动」对
# 「猫为什么打呼噜」）重叠 3 词，无关命题重叠 0 词。不能用 search_within——
# 它无命中时退回末尾几条，拿那个当来源就是给无关命题硬塞依据。
_SPAN_MIN_OVERLAP = 2
_SPAN_KEEP = 2


def _sentences(text):
    """切句但保留原文 offset，quote 才能逐字复核。"""
    for match in _SENTENCE_SPLIT.finditer(str(text or "")):
        raw = match.group(0)
        sentence = raw.strip()
        if not sentence:
            continue
        start = match.start() + (len(raw) - len(raw.lstrip()))
        yield sentence, start, start + len(sentence)


def _locate_spans(text, window, keep=_SPAN_KEEP):
    """在冻结窗口里定位**这一条**命题自己的原句，用系统唯一分词口径。

    每个源事件只取重叠最高的那一句，最多 keep 个事件。命中只证明「原句可
    定位」，不证明语义支持——所以 match_kind 一律 lexical，语义核验是自然
    证据（§22.4 event_integrity），不由这里冒充。
    """
    import persona_store as _ps
    wanted = set(_ps.tokenize(text))
    if not wanted:
        return []
    scored = []
    for row in window or []:
        best = None
        for sentence, start, end in _sentences(row.get("text")):
            common = wanted & set(_ps.tokenize(sentence))
            if len(common) < _SPAN_MIN_OVERLAP:
                continue
            if best is None or len(common) > best[0]:
                best = (len(common), sentence, start, end)
        if best:
            scored.append((best[0], row, best[1], best[2], best[3]))
    scored.sort(key=lambda item: -item[0])
    return [{"event_id": row["event_id"], "quote": sentence,
             "start": start, "end": end,
             "source_revision": row.get("source_revision"),
             "match_kind": "lexical"}
            for _n, row, sentence, start, end in scored[:keep]]


def _absorb_list(d, key, raw, evidence=(), window=()):
    """清洗 + 门禁 + 去重 + 落库，返回真正新增的条目。

    window 是这轮整理冻结的真实源记录。M02/X04：每条新结论只挂**它自己**
    定位到的原句与事件，不再把整窗 8 个事件摊给每条结论；定位不到原句或
    主体/否定核不过的进隔离候选，persona 仍保留为旧背景（X05），但绝不
    写成有依据的认知信念 —— 凭空断言是编生平。
    """
    if not isinstance(raw, list) or not raw:
        return []
    cur = list(d.get(key) or [])
    known = {_norm_mem(x) for x in cur}
    cap = _MERGE_LIST_CAPS.get(key, 5)
    added = []
    for s in _clean_list(raw):
        if len(added) >= cap:
            break
        n = _norm_mem(s)
        if not n or n in known:
            continue
        # 语料库隔离：书里的设定不进她的人生（与 record_fact 同一道门）
        if _book_tainted(s):
            print("[人格] 整理时拦下书本设定(%s): %s" % (key, s[:36]), flush=True)
            continue
        if key in ("facts", "experiences") and (_is_wish_junk(s) or _is_transient_event(s)):
            continue
        if key in ("likes", "dislikes") and len(s) > 40:
            s = s[:40]
        known.add(n)
        added.append(s)
    if not added:
        return []
    grounded, sources = _bind_added(added, key, evidence, window)
    try:
        import persona_store as _ps
        _ps.add_many(key, added, sources=sources)
        _ps.set_meta("updated", time.time())
    except Exception as e:
        print("[人格] 吸收 %s 写库失败 %s" % (key, str(e)[:60]), flush=True)
        return []
    d[key] = _clean_list(cur + added)
    if grounded:
        try:
            import self_model as _sm
            for s, spans in grounded.items():
                _sm.add_belief(s, source="consolidation", spans=spans)
        except Exception as e:
            print("[人格] 信念记账失败 " + type(e).__name__, flush=True)
    return added


def _bind_added(added, key, evidence, window):
    """逐命题绑定原句。返回 (可写信念的 span 映射, persona 逐条来源映射)。

    失败要出声：绑定链路断了就退回「全部无来源」，绝不悄悄改回整窗挂证据。
    """
    if key not in ("facts", "experiences", "likes", "dislikes"):
        return {}, {}
    try:
        import claim_provenance as _cp
        import self_model as _sm
        if not window and evidence:
            window = _sm.source_window(evidence)
        grounded, sources = {}, {}
        for s in added:
            located = _locate_spans(s, window)
            bound = _cp.bind_claim({"restatement": s, "evidence": located}, window)
            if bound["decision"] == "accepted" and bound["spans"]:
                grounded[s] = bound["spans"]
                sources[s] = {"source_event_ids": [p["event_id"] for p in bound["spans"]],
                              "evidence_spans": bound["spans"]}
                continue
            _sm.quarantine_claim(s, kind=key, reasons=bound["reasons"],
                                 evidence=located, subject_ref=bound["subject_ref"],
                                 decision=bound["decision"])
            print("[人格] 逐命题来源未过(%s/%s): %s"
                  % (key, ",".join(bound["reasons"])[:60], s[:36]), flush=True)
        return grounded, sources
    except Exception as e:
        print("[人格] 逐命题来源绑定失败 " + type(e).__name__ + ": " + str(e)[:80], flush=True)
        return {}, {}


def _merge(d, got, evidence=(), window=()):
    """把 LLM 给的增量合并进档案，只接受认识的键。"""
    changed = False
    # 口癖里混进"宝宝""乖女儿"这种称呼，会直接改掉她怎么叫主人，必须拦住
    _BAD_TIC = set()   # 亲昵称呼现在允许了（主人要求可以被叫宝宝/宝贝）

    if isinstance(got.get("voice"), dict):
        v = got["voice"]
        cur = dict(d.get("voice") or {})
        for k, lim in (("口癖", 0), ("句式", 0)):
            if isinstance(v.get(k), list):
                fresh = [x for x in (v.get(k) or []) if str(x).strip() not in _BAD_TIC]
                merged = _clean_list(list(cur.get(k) or []) + fresh, lim)
                if merged != list(cur.get(k) or []):
                    cur[k] = merged
                    changed = True
        if isinstance(v.get("语气"), str) and v["语气"].strip():
            cur["语气"] = v["语气"].strip()[:120]
            changed = True
        cur["口癖"] = [x for x in (cur.get("口癖") or []) if str(x).strip() not in _BAD_TIC]
        d["voice"] = cur

    absorbed = {}
    for key in ("facts", "experiences", "likes", "dislikes", "curious"):
        got_more = _absorb_list(d, key, got.get(key), evidence=evidence, window=window)
        if got_more:
            absorbed[key] = len(got_more)
            changed = True

    sv = got.get("self_view")
    if isinstance(sv, str) and sv.strip():
        nv = sv.strip()[:40]
        if nv and nv != str(d.get("self_view") or "") and not _book_tainted(nv):
            d["self_view"] = nv
            changed = True

    if absorbed:
        print("[人格] 本轮吸收 " + " ".join("%s+%d" % (k, v) for k, v in absorbed.items()),
              flush=True)
    return changed


# 整理时给模型看的档案规模：每类只带最近若干条。
# 去重不依赖这里（_absorb_list 是拿完整列表比对的），它只负责让模型别把同一件事再说一遍。
# 全量喂进去有两个代价：prompt 随记忆线性膨胀，而且档案每小时都在变 → 前缀缓存永不命中。
_VIEW_CAPS = {
    "facts": int(os.getenv("PERSONA_VIEW_FACTS", "40")),
    "experiences": int(os.getenv("PERSONA_VIEW_EXPERIENCES", "40")),
    "likes": int(os.getenv("PERSONA_VIEW_LIKES", "20")),
    "dislikes": int(os.getenv("PERSONA_VIEW_DISLIKES", "20")),
    "curious": int(os.getenv("PERSONA_VIEW_CURIOUS", "20")),
}


def _archive_view(d):
    """整理器用的档案视图（近期样本 + 真实总量）。"""
    view, totals = {}, {}
    for k, cap in _VIEW_CAPS.items():
        arr = list(d.get(k) or [])
        totals[k] = len(arr)
        view[k] = arr[-cap:] if cap > 0 else arr
    view["voice"] = d.get("voice")
    view["self_view"] = d.get("self_view")
    return view, totals


def _build_prompt(d, rows):
    lines = []
    for r in rows:
        u = str(r.get("user") or "").strip()
        b = str(r.get("bot") or "").strip()
        if u or b:
            lines.append(json.dumps({"会话来源": str(r.get("session") or "unknown"),
                                     "时间": r.get("ts"), "用户陈述": u,
                                     "助手回复（非工具完成证据）": b}, ensure_ascii=False))
    # 游标覆盖 rows 全部，prompt 也必须覆盖同一窗口，不能静默截掉后半段。
    dialogue = "\n\n".join(lines)

    _roster = ""
    try:
        import member_memory as _mm
        _roster = _mm.all_rosters_text()
    except Exception:
        _roster = ""

    cur, totals = _archive_view(d)
    cur_txt = json.dumps(cur, ensure_ascii=False, indent=1)
    _totals_line = ("（档案现有总量："
                    + " / ".join("%s %d 条" % (k, v) for k, v in totals.items())
                    + "；下面只列每类最近的部分条目做参考，重复的意思别再写一遍。）")

    return (
        "你是一个 AI 角色的「人格档案整理器」。她的名字叫清漪。\n"
        "下面是她的现有档案，以及最近发生的一批真实对话。\n"
        "请从对话里总结她应该新增的东西，只输出一个 JSON 对象，不要任何解释。\n\n"
        "输出格式（每一项都是数组，可以为空数组）：\n"
        '{"voice": {"口癖": [], "句式": [], "语气": ""},'
        ' "likes": [], "dislikes": [], "curious": [],'
        ' "experiences": [], "facts": [], "self_view": ""}\n\n'
        "规则：\n"
        "1. voice 只可观察，不是命令；重复语气词不是需要学习的口癖，不因重复生成新规则。无可靠变化就留空。\n"
        "2. 每行带独立会话来源，群聊与私聊参与者不得合并，不明身份不要套主人姓名。\n"
        "3. 用户陈述只能记为该会话用户自述；助手猜测、承诺、计划不是事实。"
        "助手说做完不等于工具完成证据，只有独立工具结果确认才可记完成；无证据保留未知。\n"
        "4. likes/dislikes：从她自己的表达和对话里推出来的偏好，每条 8 字内。\n"
        "4b. 【voice.句式 要跟着她变】只写她近期真在用的说法。如果现有句式里"
        "有和最近对话对不上的（比如总是套同一句吐槽、或早就换了的梗），"
        "就别再输出那种，用新的替换掉。口癖也同理，只留真在用的。\n"
        "5. curious：她表现出想知道的事，最多 3 条。\n"
        "6. experiences：具体发生过的事，写成短句，如「给主人唱了孤勇者」，最多 5 条。\n"
        "7. facts：关于人或世界的确定事实，如「主人叫主人」，最多 6 条。不要把猜测写成事实。\n"
        "8. self_view：她怎么评价自己，一句话，不超过 40 字。\n"
        "9. 只写对话里能支持的，没证据就留空数组。\n"
        "9b. 【记长期有用的，一次别灌满】只要对话里确实发生过就记下来："
        "谁说了什么、她做了什么、当时什么气氛、主人当时什么状态。宁可细碎，也别漏。"
        "每条控制在 30 字内。存进去的永不裁剪、不会挤掉旧的，所以**不必一次记完**——"
        "本轮每类按上面写的条数上限给，挑最重要、最能塑造她的几条；"
        "整点还会再整理，剩下的下一轮再补。一次灌几百条会把输出撑爆、反而一条都存不下。\n"
        "9c. 【只记能塑造她的东西】优先记这些：\n"
        "    · 谁和谁在一起玩、聊什么、关系怎么样\n"
        "    · 她喜欢跟谁玩、跟谁更亲近、对谁示过好\n"
        "    · 谁对她好、她怎么回应、有没有闹别扭又和好\n"
        "    · 她自己的小偏好、小脾气、在意的点\n"
        "    【不要记】临时事件和技术琐事：谁正在用什么软件、界面好不好看、"
        "网盘登不登录、插件报没报错、谁在部署什么。这些过两天就没意义了，"
        "记下来只会占地方。\n"
        "10. 写人名时必须用下面的固定称呼，不要用群名片，也不要自己造简称。\n"
        "11. 【最重要 · 语料库隔离】她平时会读写小说、古风、异世界之类的文本，"
        "那些是「素材」，不是她的身世。绝对不许把书里的人物、门派、朝代、"
        "称谓、世界观、剧情当成她的经历或事实。\n"
        "    如果对话里出现「公主/殿下/师父/师兄/仙门/江湖/王朝/陛下/臣妾/本座」这类，"
        "那是在写小说或扮演，不是她本人。这类内容一律不准进 experiences / facts / self_view。\n"
        "12. 她是一个独立的现代人（网名清漪，一个群聊里的 AI 角色），有自己的喜好和日常。"
        "只记录她本人和真实群友之间发生的事。\n"
        + _roster + "\n"
        "现有档案：\n" + _totals_line + "\n" + cur_txt + "\n\n"
        "最近对话：\n" + (dialogue if dialogue else "（无）")
    )


async def consolidate(call_llm):
    """每小时执行：读新对话 -> 请 LLM 提炼 -> 合并进档案。"""
    d = load()
    since = float(d.get("last_consolidated") or 0)
    rows = read_new_journal(since)
    if len(rows) < CONSOLIDATE_MIN_ITEMS:
        return {"ok": False, "reason": "新对话不足 %d 条" % CONSOLIDATE_MIN_ITEMS, "count": len(rows)}

    # 限制本轮完整行的窗口而非截断字符串；相同时间戳必须一起处理，
    # 否则仅有时间戳的既有游标会跳过同时间的剩余行。
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    used, selected, cutoff = 0, [], None
    for row in rows:
        ts = float(row.get("ts") or 0)
        size = len(json.dumps(row, ensure_ascii=False))
        if selected and used + size > 4000 and ts != cutoff:
            break
        selected.append(row)
        used += size
        cutoff = ts
    rows = selected
    prompt = _build_prompt(d, rows)
    try:
        raw = await call_llm(prompt)
    except Exception as e:
        print("[人格] 整理调用失败 " + type(e).__name__ + ": " + str(e)[:80], flush=True)
        return {"ok": False, "reason": "调用失败"}

    text = str(raw or "").strip()
    fence = chr(96) * 3
    text = text.replace(fence + "json", "").replace(fence, "").strip()
    start = text.find("{")
    end = text.rfind("}")
    got = {}
    if start >= 0 and end > start:
        try:
            got = json.loads(text[start:end + 1])
        except Exception:
            got = {}

    if not isinstance(got, dict):
        got = {}

    # 解析不出来（多半是输出被 max_tokens 拦腰截断）就**不要**前移 last_consolidated，
    # 否则这一窗口的对话被记成"已整理"，学习机会永久丢失，而且日志只会打"变化=无"。
    if not got:
        print("[人格] 整理输出无法解析（疑似截断），本轮窗口保留待重试 len=%d tail=%r"
              % (len(text), text[-40:]), flush=True)
        return {"ok": False, "reason": "输出解析失败", "count": len(rows)}

    # 来源事件：她这轮整理读到的真实对话。冻结成只读源记录后逐命题定位原句
    # （M02）；取不到真实来源就不写认知信念（凭空断言＝编生平）。
    _src = []
    for _r in rows:
        for _e in (_r.get("events") or []):
            if _e and _e not in _src:
                _src.append(_e)
    _window = []
    try:
        import self_model as _smv
        _window = _smv.source_window(_src[:8])
    except Exception as _ve:
        print("[人格] 来源事件核验失败 " + type(_ve).__name__, flush=True)
    _sv_before = str(d.get("self_view") or "")
    changed = _merge(d, got, window=_window) if got else False
    # 阶段1：自我认识变了要留一条修订记录 + 这次整理的证据事件。
    # 不这么做，自我认识只会变成「最新一次摘要」，永远回答不了「什么改变了你」。
    try:
        _sv_after = str(d.get("self_view") or "")
        if _sv_after and _sv_after != _sv_before:
            import self_model as _sm
            _eid = _sm.record_event(
                "consolidation", session="persona_growth",
                payload={"rows": len(rows), "since": since, "source_events": _src[:8]},
                source="journal",
                dedupe="consol:%s:%d" % (int(float(d["last_consolidated"]) if d.get("last_consolidated") else 0),
                                         len(rows)))
            # 整理这件事本身也挂进去：它是「她为什么这么看自己」这条链上的一环，
            # 但只有它就不够了 —— 前面那些对话才是依据。
            _sm.note_self_view(_sv_after, reason="整点整理",
                               evidence=([_eid] if _eid else []) + _src[:8])
    except Exception as _cse:
        print("[人格] 自我认识修订记账失败 " + type(_cse).__name__, flush=True)
    d["last_consolidated"] = max([float(r.get("ts") or 0) for r in rows] + [since])
    d["updated"] = time.time()
    stats = dict(d.get("stats") or {})
    stats["consolidations"] = int(stats.get("consolidations", 0)) + 1
    stats["exchanges"] = int(stats.get("exchanges", 0)) + len(rows)
    d["stats"] = stats
    if changed:
        d["revision"] = int(d.get("revision", 0)) + 1
    save(d)

    print("[人格] 整理完成 对话=%d 变化=%s rev=%d"
          % (len(rows), "有" if changed else "无", int(d.get("revision", 0))), flush=True)
    return {"ok": True, "count": len(rows), "changed": changed, "revision": int(d.get("revision", 0))}


def snapshot():
    """给面板看的完整档案。"""
    d = load()
    d["_file"] = self_file()
    d["_journal"] = journal_file()
    return d


async def reflect_history(call_llm, days=14, max_rows=40):
    """从历史对话（近 N 天）做自我修订，不推进 last_consolidated。

    新对话攒得慢时，用真实历史给自我认识提供材料；证据是历史真实事件，
    不补造。只有自我认识真实变化才写修订。与整点整理互不干扰。
    """
    d = load()
    now = time.time()
    rows = read_new_journal(now - days * 86400)
    if len(rows) < 3:
        return {"ok": False, "reason": "历史对话不足 3 条", "count": len(rows)}
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    if len(rows) > max_rows:
        step = len(rows) / float(max_rows)
        rows = [rows[int(i * step)] for i in range(max_rows)]
    prompt = _build_prompt(d, rows)
    try:
        raw = await call_llm(prompt)
    except Exception as e:
        return {"ok": False, "reason": "调用失败:" + type(e).__name__}
    text = str(raw or "").strip()
    text = text.replace(chr(96) * 3 + "json", "").replace(chr(96) * 3, "").strip()
    s, e = text.find("{"), text.rfind("}")
    got = {}
    if s >= 0 and e > s:
        try:
            got = json.loads(text[s:e + 1])
        except Exception:
            got = {}
    if not isinstance(got, dict) or not got:
        return {"ok": False, "reason": "输出解析失败", "count": len(rows)}
    _src = []
    for r in rows:
        for ev in (r.get("events") or []):
            if ev and ev not in _src:
                _src.append(ev)
    try:
        import self_model as _smv
        _window = _smv.source_window(_src[:8])
    except Exception:
        _window = []
    _before = str(d.get("self_view") or "")
    changed = _merge(d, got, window=_window)
    _after = str(d.get("self_view") or "")
    if _after and _after != _before:
        try:
            import self_model as _sm
            _eid = _sm.record_event(
                "consolidation", session="persona_growth",
                payload={"rows": len(rows), "historical_days": days,
                         "source_events": _src[:8]},
                source="journal", dedupe="hist:%d:%d" % (int(now), len(rows)))
            _sm.note_self_view(_after, reason="历史回顾",
                               evidence=([_eid] if _eid else []) + _src[:8])
        except Exception as e:
            print("[人格] 历史修订记账失败 " + type(e).__name__, flush=True)
    if changed:
        d["revision"] = int(d.get("revision", 0)) + 1
    save(d)  # 不推进 last_consolidated，新对话整理不受影响
    return {"ok": True, "count": len(rows), "changed": changed,
            "revision": int(d.get("revision", 0))}
