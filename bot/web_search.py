"""联网搜索接口：让她能自己查世界上的事。

实现：抓 Bing 的结果页（不需要 API key）。
备用源按顺序尝试，任何一个成功就返回。

返回结构统一为 [{"title", "url", "snippet"}]。
"""
import html
import inspect
import json
import os
import re
import time

import httpx

SEARCH_ENABLED = os.getenv("SEARCH_ENABLED", "1").strip() == "1"
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "15"))
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
SEARCH_MAX_CHARS = int(os.getenv("SEARCH_MAX_CHARS", "700"))

# 博查 AI 开放平台的 web-search。key 只写在服务器 config.env，不进仓库。
BOCHA_URL = "https://api.bocha.cn/v1/web-search"
BOCHA_API_KEY = os.getenv("BOCHA_API_KEY", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_cache = {}
_CACHE_TTL = 600.0


def _strip_tags(fragment):
    text = re.sub(r"<[^>]+>", "", fragment or "")
    return html.unescape(text).strip()


def _parse_bing(text, limit):
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', text, re.S):
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = m.group(1).strip()
        title = _strip_tags(m.group(2))
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _strip_tags(p.group(1)) if p else ""
        if not url or not title:
            continue
        out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= limit:
            break
    return out


# 疑问词：搜的时候去掉，否则搜索引擎会按字面匹配
_QUESTION_WORDS = [
    "请问", "帮我查", "帮我搜", "查一下", "搜一下", "告诉我",
    "是谁拿的", "是谁", "是什么", "是啥", "怎么样", "怎么", "如何", "为什么",
    "多少钱", "多少", "哪一个", "哪个", "哪", "吗", "呢", "来着", "呀", "啊",
    "拿的", "得的",
]

# 时间词 -> 替换成具体日期/年份，搜索引擎才认
_TIME_WORDS = {
    "今天": "%Y-%m-%d",
    "昨天": "%Y-%m-%d",
    "明天": "%Y-%m-%d",
    "今年": "%Y",
    "去年": "%Y",
    "最新": "%Y%m",
    "最近": "%Y%m",
}

_QUOTE_CHARS = (chr(34), chr(39), chr(8220), chr(8221))


def build_query(text):
    """把一句问话改成搜索引擎友好的关键词。

    "今年诺贝尔文学奖是谁拿的" -> "诺贝尔文学奖 2026"
    "今天北京天气"           -> "北京天气 2026-11-03"
    "这个英雄怎么出装"       -> "英雄 出装"

    以前是拿整句去掉疑问词去查，句首剩个「这」字，Bing 就返回「这」的字典页，
    她会在群里说「刚翻了下，这字确实是七画」。现在一律从 query_anchor 起词：
    代词、虚词、疑问框架都进不了查询串。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    now = time.localtime()
    suffix = ""
    for word, fmt in _TIME_WORDS.items():
        if word in t:
            suffix = time.strftime(fmt, now)
            break
    for ch in _QUOTE_CHARS:
        t = t.replace(ch, " ")
    query = query_anchor(t)
    if not query:
        # 整句都是代词/寒暄时至少别把空串丢给搜索引擎
        query = _PUNCT_RE.sub(" ", t).strip()[:40]
    query = " ".join(query.split())[:40]
    if suffix:
        query = (query + " " + suffix).strip()
    return query[:50]

def _norm_bocha(data, limit):
    """博查的响应剥成本地统一形状：[{title,url,snippet,site,date}]。

    date 只给提示词看（她要说「11月2日」得知道这是几号的价），
    相关性判定不看它，否则日期会假命中。
    """
    out = []
    try:
        pages = ((data or {}).get("data") or {}).get("webPages") or {}
        vals = pages.get("value") or []
    except Exception:
        return out
    for it in vals[:max(1, int(limit))]:
        title = str(it.get("name") or "").strip()
        url = str(it.get("url") or "").strip()
        if not title or not url:
            continue
        snip = str(it.get("summary") or "").strip() or str(it.get("snippet") or "").strip()
        date = str(it.get("datePublished") or it.get("dateLastCrawled") or "")[:10]
        out.append({"title": title, "url": url, "snippet": snip,
                    "site": str(it.get("siteName") or "").strip(), "date": date})
    return out


async def _search_bocha(client, q, limit):
    body = {"query": q, "freshness": "noLimit", "summary": True,
            "count": max(1, min(int(limit), 15))}
    r = await client.post(BOCHA_URL, json=body, headers={
        "Authorization": "Bearer " + BOCHA_API_KEY,
        "Content-Type": "application/json"})
    if r.status_code >= 400:
        return {"ok": False, "error": "HTTP%d" % r.status_code, "results": []}
    items = _norm_bocha(r.json(), limit)
    if not items:
        return {"ok": False, "error": "空结果", "results": []}
    return {"ok": True, "error": "", "results": items, "source": "bocha"}


async def _search_bing(client, q, limit):
    r = await client.get("https://www.bing.com/search",
                         params={"q": q, "setlang": "zh-CN", "ensearch": "0"},
                         headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    if r.status_code >= 400:
        return {"ok": False, "error": "HTTP%d" % r.status_code, "results": []}
    items = _parse_bing(r.text, limit)
    return {"ok": bool(items), "error": "" if items else "没解析出结果",
            "results": items, "source": "bing"}


async def search(query, limit=None, *, session="", visibility="private"):
    """搜一条。返回 {ok, results, source, error}。

    主源博查（api.bocha.cn，要 BOCHA_API_KEY），备用源是抓 Bing 结果页。
    以前只有抓 Bing 那一条：问「特斯拉股价现在多少」还回来的是「特」的字典页，
    她的「实时信息」基本等于噪音。没配 key 时行为退回旧样。
    """
    q = str(query or "").strip()
    limit = int(limit or SEARCH_MAX_RESULTS)
    if not SEARCH_ENABLED:
        return {"ok": False, "error": "搜索未开启", "results": []}
    if not q:
        return {"ok": False, "error": "没有关键词", "results": []}

    key = q.lower() + "|" + str(limit)
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]

    result = {"ok": False, "error": "没有结果", "results": [], "source": ""}
    attempts = ([("bocha", _search_bocha)] if BOCHA_API_KEY else []) + [("bing", _search_bing)]

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as c:
            for _, fn in attempts:
                try:
                    got = await fn(c, q, limit)
                except Exception as e:
                    got = {"ok": False, "error": type(e).__name__, "results": []}
                if got.get("ok"):
                    result = got
                    break
                result = got
    except Exception as e:
        result["error"] = type(e).__name__

    import uuid
    result = dict(result, run_id=uuid.uuid4().hex)
    try:
        import self_model
        self_model.record_tool_result(
            "search", self_model._sha(q), bool(result.get("ok")),
            session=session, visibility=visibility, run_id=result["run_id"],
            reason=str(result.get("error") or "")[:80])
    except Exception as exc:
        print("[认知] 搜索结果记账失败 " + type(exc).__name__, flush=True)
    _cache[key] = (time.time(), result)
    if len(_cache) > 200:
        for k in list(_cache)[:100]:
            _cache.pop(k, None)
    return result


# ── 该不该查 · 查回来的值不值得说 ─────────────────────
# 搜索是内部动作。这两道门的意义都在那两起事故里（见 tests/test_search_precision.py）：
# 把「刚翻了下」「查了半天全是屠洪刚的《你》」这种中间态发到群里，
# 比不查更像机器，也更不像她。
_PRONOUN_RE = re.compile(
    r"你们|我们|他们|她们|咱们|大家|人家|自己|我|你|您|我|咱|他|她|它"
    r"|谁|啥|哪位|哪些|哪个|哪里|哪儿"
    # 指示代词必须一起剥掉：留着句首的「这」，Bing 就还你一张「这」的字典页
    r"|这个|那个|这些|那些|这样|那样|这么|那么|这里|那里|这|那"
)
# 疑问框架与心理动词：去掉这些还剩东西，才说明问的是世界，不是人
_ASK_FRAME_RE = re.compile(
    r"能不能|可不可以|是不是|行不行|要不要|有没有|"
    r"觉得|知道|以为|认为|希望|猜|告诉|记得|忘了|打算|准备|喜欢|讨厌|需要|"
    r"感觉|感受|体验|体会|"
    r"怎么|怎样|如何|为什么|为何|多少|什么|咋|吗|呢|吧|啊|呀|哦|嗯|的|了|是|很|太|就|都|还|又|点|儿|样"
)
_PUNCT_RE = re.compile(r"[\s，。！？?、,.!;；:：《》「」\"'“”‘’()（）【】~～·\-—]")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9]{2,}")


def _runes(text):
    """把一句话拆成可能当搜索锚的片段：中文串按二字组，英数串整块。"""
    t = str(text or "")
    out = set()
    for run in _CJK_RUN_RE.findall(t):
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
        if len(run) == 2:
            out.add(run)
    for run in _LATIN_RUN_RE.findall(t):
        out.add(run.lower())
    return out


def query_anchor(text):
    """这句里真正要查的东西；只剩代词和疑问框架时返回空串。

    "你呢" -> ""        "你觉得我怎么样" -> ""
    "你cos谁" -> "cos"  （词法判不出该不该查，交给 relevance 兜）
    "2026年诺贝尔文学奖得主是谁" -> "2026 诺贝尔文学奖得主"
    """
    t = str(text or "").strip()
    if not t:
        return ""
    for word in _TIME_WORDS:
        t = t.replace(word, " ")
    for word in _QUESTION_WORDS:
        t = t.replace(word, " ")
    t = _PRONOUN_RE.sub(" ", t)
    t = _ASK_FRAME_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t).strip()
    # 锚至少得是个二字组或一整块英数；只剩「穿」「样」这种单字说明没东西可查
    return t if _runes(t) else ""


def _anchor_tokens(text):
    return _runes(query_anchor(text))


def looks_searchable(text):
    """这句里有没有「两个以上互相印证的实词」—— 够不够定位到一件具体的事。

    只剩一个片段（"cos"、"出装"）多半是动作而不是实体，网上查回来的是字典页；
    代词寒暄（"你呢"、"你觉得我怎么样"）连锚都没有。
    这句只管「有没有实体」，是不是在提问由桥那边判。
    """
    return len(_anchor_tokens(text)) >= 2


# 她自己能去查的话题，和「一问就能问到人」的话题，得分开。
_ADDRESSSEE_WORDS = [w.strip() for w in os.getenv(
    "SEARCH_ADDRESSEE_WORDS",
    "主人,爸爸,妈妈,主人,清漪,我,你自己").split(",") if w.strip()]


def topic_block_reason(text):
    """这个话题为什么不该自己联网查；该查就返回空串。

    分成两种是必须的：「主人刚才是不是偷偷笑了」是要问人，
    「小狗是谁」是只剩一个泛称、查回来必然是字典页。
    她这两件事的感受完全不同，日志里混成同一句就是我们在替她编原因。
    """
    t = str(text or "").strip()
    if not t:
        return u"没东西可查"
    for w in _ADDRESSSEE_WORDS:
        if w and w in t:
            return u"问的是熟人，该问人不该联网"
    if not looks_searchable(t):
        return u"只剩一个泛称，查回来是字典页"
    return ""


def searchable_topic(text):
    """这个话题该不该自己去联网查 —— 见 topic_block_reason。"""
    return not topic_block_reason(text)


def relevance(question, results, min_ratio=0.2, min_shared=2):
    """搜回来的东西有没有真的在回答这个问题。

    判据是「问题自己的实词有没有出现在结果里」：
      问 cos，回来的是屠洪刚《你》 -> 0 个重合 -> 不相干
      问赤合金，回来的是「赤七画」 -> 二字组一个都不重合 -> 不相干
    只用二字组/整块英数，不用单字，否则「的」「赤」这种会假命中。
    """
    qtok = _anchor_tokens(question)
    if not qtok or not results:
        return False
    shared = 0
    best = 0.0
    for it in results:
        body = str(it.get("title") or "") + " " + str(it.get("snippet") or "")
        rtok = _runes(body)
        if not rtok:
            continue
        hit = len(qtok & rtok)
        best = max(best, float(hit))
        if hit:
            shared = max(shared, hit)
    need = min(min_shared, len(qtok))
    return shared >= need and (shared / float(len(qtok))) >= min_ratio


# ── 实体约束 · 逐条判定 · 证据化入库 ───────────────────
# 起因（2026-11-08 取证）：「卫戍协议」被扩写成《卫戍协议》（Garrison Protocol），
# 查询降格成「卫戍」；回来的一批结果里《看门狗：军团》只因同时出现
# 「协议 / 多人 / 支持 / 模式 / 联机」这些泛词就过了旧 relevance，整批 active 入库。
# 根子是旧口径只看「问题实词和结果有没有重叠」，从不问「这页讲的是不是同一个东西」。
# 这里把实体显式写进查询计划，再逐条判实体 / 领域 / 回答要点；
# 判不准一律不通过 —— 宁可少一条知识，也不要把别人的游戏记成她的设定。

# 句首的疑问/人称框架：只削开头，不动专名内部（「我的世界」不能被削成「世界」）
_LEADIN = tuple(sorted((
    "请问", "帮我查", "帮我搜", "查一下", "搜一下", "告诉我",
    "我想知道", "我想搞懂", "我想了解", "想知道", "想搞懂", "了解一下",
    "我觉得", "我认为", "我感觉", "你知道", "你觉得",
    "这个", "那个", "这些", "那些", "这", "那",
    "今天", "昨天", "明天", "今年", "去年",
), key=len, reverse=True))
# 句尾的疑问框架
_TAIL_ASK = tuple(sorted((
    "是谁拿的", "是谁", "是什么", "是啥", "怎么样", "怎么玩", "怎么",
    "如何", "为什么", "为何", "多少钱", "多少", "哪一个", "哪个", "在哪",
    "吗", "呢", "来着", "呀", "啊",
), key=len, reverse=True))
# 这些是「问哪个方面」，不是实体本身；出现在句子中段时用它切开实体。
# 分两档是实测逼出来的：
#   · 硬领域（联机/股价/天气…）必须有证据，缺了就是另一件事，不许放行；
#   · 软方面（原型/剧情/攻略…）只用来切词和优化查询，不当放行门槛 ——
#     否则「荒坂塔的建筑原型」会把一页只讲「荒坂塔」的正文误杀。
_DOMAIN_TERMS = tuple(sorted((
    "多人联机", "联机", "多人", "玩法", "攻略", "评测", "测评",
    "股价", "汇率", "比分", "赛程", "票房", "天气", "气温", "热搜",
    "价格", "售价", "配置", "平台", "发售", "上线", "更新",
    "剧情", "设定", "原型", "出装", "强度", "阵容", "下载", "官网",
), key=len, reverse=True))
_STRICT_DOMAINS = frozenset((
    "多人联机", "联机", "多人", "股价", "汇率", "比分", "赛程", "票房",
    "天气", "气温", "热搜", "价格", "售价", "配置", "平台", "发售",
    "上线", "更新", "出装", "阵容", "下载", "官网",
))
# 实体末尾的角色词，不属于专名
_TAIL_ROLE = tuple(sorted(("得主", "获得者", "冠军", "赢家"), key=len, reverse=True))
_EDGE_CHARS = " \t，。！？?、,.!;；:：\"'“”‘’()（）【】《》「」~～·-—"
_TAIL_PARTICLES = "的里中上下内和与及"

_PLAN_PROMPT = (
    "从下面这句话里拆出「她要查什么」。只输出一个 JSON，不要解释：\n"
    "{\"entity\": \"...\", \"domain\": \"...\", \"question\": \"...\", "
    "\"query\": \"...\", \"alternates\": [\"...\"]}\n"
    "规则：\n"
    "- entity 必须是**完整的专名**，逐字照原话写；不许缩写、不许拆成单字或泛词，"
    "也不许补原话里没有的英文名/外文名；\n"
    "- domain 只在原话或上下文里**有证据**时填写（例如「多人联机」「出装」「股价」），"
    "没证据就填空串；\n"
    "- query 是 entity 加上 domain，必须包含完整 entity，不许只留其中两个字；\n"
    "- alternates 是查不到时的备选，每条都必须包含完整 entity，最多两条。\n"
    "原话：%s\n上下文：%s"
)


def _squash(text):
    """只留字母、数字与汉字，用来做逐字比对（空格/标点不参与）。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


def _strip_question(text):
    """削掉句首框架与句尾疑问词，专名内部一个字都不动。"""
    t = str(text or "").strip().strip(_EDGE_CHARS)
    changed = True
    while changed and t:
        changed = False
        for p in _LEADIN:
            if t.startswith(p):
                t = t[len(p):].strip(_EDGE_CHARS)
                changed = True
        for s in _TAIL_ASK:
            if t.endswith(s):
                t = t[:-len(s)].strip(_EDGE_CHARS)
                changed = True
    return t.strip(_EDGE_CHARS)


def _clean_entity(ent):
    e = " ".join(str(ent or "").split()).strip(_EDGE_CHARS)
    e = e.strip(_TAIL_PARTICLES + _EDGE_CHARS)
    # 「荒坂塔的建筑」->「荒坂塔」：方面词前面的所属修饰不属于专名
    e = re.sub(r"的[0-9A-Za-z\u4e00-\u9fff]{1,3}$", "", e)
    for role in _TAIL_ROLE:
        if e.endswith(role):
            e = e[:-len(role)].strip(_TAIL_PARTICLES + _EDGE_CHARS)
    return " ".join(e.split())


def _compose_query(entity, domain):
    q = str(entity or "").strip()
    d = str(domain or "").strip()
    if d and _squash(d) not in _squash(q):
        q = (q + " " + d).strip()
    return " ".join(q.split())[:50]


def plan_heuristic(text, context=""):
    """没有模型时的保守拆解：只顺着原话切出实体和领域，不补任何别名。

    「卫戍协议多人联机怎么样」 -> entity=卫戍协议 domain=多人联机
    「赛博朋克 2077 荒坂塔」    -> entity 保持整串，不拆
    拿不准就整句当实体：查询会把完整专名一起带出去，不会降格成「卫戍」。
    """
    raw = str(text or "").strip()
    body = _strip_question(raw)
    entity = _clean_entity(body)
    domain = ""
    aspect = ""
    # 方面词必须来自原话本身（上下文只给模型看，启发式不替她补）
    if body:
        best = None
        for term in _DOMAIN_TERMS:
            idx = body.find(term)
            if idx <= 0:
                continue
            if best is None or idx < best[0]:
                best = (idx, term)
        if best:
            entity = _clean_entity(_strip_question(body[:best[0]]))
            aspect = best[1]
            # 软方面只优化查询，不当硬门：没证据也不能因此判她查错了
            domain = aspect if aspect in _STRICT_DOMAINS else ""
    if len(_squash(entity)) < 2:
        entity = _clean_entity(_strip_question(body)) or " ".join(raw.split())
    query = _compose_query(entity, domain or aspect)
    alternates = [entity] if (domain or aspect) and entity and entity != query else []
    return {"entity": entity[:40], "domain": domain, "aspect": aspect,
            "question": (" ".join(raw.split())[:120]),
            "query": query, "alternates": alternates[:2], "source": "heuristic"}


def _parse_plan_json(raw):
    text = str(raw or "").strip()
    fence = chr(96) * 3
    text = text.replace(fence + "json", "").replace(fence, "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        got = json.loads(text[a:b + 1])
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _merge_llm_plan(base, got, text, context=""):
    """模型给的计划也要过同一道实体约束。

    模型最容易犯的两种错都在这里挡掉：
      · 把完整专名截短（卫戍协议 -> 卫戍）；
      · 把原话里没有的外文名补进来（Garrison Protocol）。
    """
    evidence = _squash(text) + _squash(context)
    ent = _clean_entity(got.get("entity"))
    base_ent = base.get("entity") or ""
    if not _squash(ent) or _squash(ent) not in _squash(text):
        ent = base_ent
    elif (_squash(base_ent).startswith(_squash(ent))
          and len(_squash(base_ent)) > len(_squash(ent))):
        # 模型把专名截短了：退回原话里的完整写法
        ent = base_ent
    if not ent:
        ent = base_ent
    raw_dom = _clean_entity(got.get("domain"))
    dom = (raw_dom if raw_dom and _squash(raw_dom) in evidence
           else (base.get("domain") or ""))
    aspect = str(base.get("aspect") or "")
    if dom and dom in _DOMAIN_TERMS and dom not in _STRICT_DOMAINS:
        # 模型把「原型/剧情」这类软方面填进了 domain：降为查询用方面，
        # 不当硬门，否则正文只讲实体本身的好页面会被误杀
        aspect, dom = dom, ""
    question = " ".join(str(got.get("question") or text or "").split())[:120]
    q = " ".join(str(got.get("query") or "").split())
    if not _squash(ent) or _squash(ent) not in _squash(q):
        q = _compose_query(ent, dom or aspect)
    alts = []
    for a in (got.get("alternates") or []):
        s = " ".join(str(a or "").split())[:40]
        if not s or s == q or _squash(ent) not in _squash(s):
            continue
        if s not in alts:
            alts.append(s)
    if not alts:
        alts = list(base.get("alternates") or [])
    return {"entity": ent[:40], "domain": dom, "aspect": aspect,
            "question": question or base.get("question"),
            "query": q[:50], "alternates": alts[:2], "source": "llm"}


async def plan(text, context="", llm=None):
    """把一句好奇拆成 {entity, domain, question, query, alternates}。

    给了 llm 就让它填，但结果必须过实体校验（见 _merge_llm_plan）；
    没给、调用失败、输出不成形，一律退回保守启发式 —— 拆不出来也不能降格。
    """
    base = plan_heuristic(text, context)
    if llm is None:
        return base
    try:
        raw = llm(_PLAN_PROMPT % (str(text or "")[:200], str(context or "")[:400]))
        if inspect.isawaitable(raw):
            raw = await raw
    except Exception as e:
        print("[搜索] 查询计划生成失败 " + type(e).__name__ + "，退回启发式", flush=True)
        return dict(base, note="llm_failed")
    got = _parse_plan_json(raw)
    if not got:
        return dict(base, note="llm_unparsed")
    return _merge_llm_plan(base, got, text, context)


def _plan_fields(plan_obj):
    if isinstance(plan_obj, dict):
        p = dict(plan_obj)
    else:
        p = plan_heuristic(str(plan_obj or ""))
    p.setdefault("entity", "")
    p.setdefault("domain", "")
    p.setdefault("question", "")
    p.setdefault("query", _compose_query(p.get("entity"), p.get("domain")))
    p.setdefault("alternates", [])
    return p


def query_keeps_entity(plan_obj, query):
    """这条查询有没有把完整实体带出去（「卫戍」≠「卫戍协议」）。"""
    ent = str(_plan_fields(plan_obj).get("entity") or "")
    e, q = _squash(ent), _squash(query)
    return bool(e) and e in q


def _entity_tokens(text):
    try:
        import text_tokens as _tt
        words = _tt.content_words(text)
    except Exception:
        words = re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", str(text or ""))
    out = []
    for w in words:
        s = _squash(w)
        if len(s) >= 2 and s not in out:
            out.append(s)
    return out


def _match_entity(entity, body):
    """完整实体逐字命中，或「全部词都在、顺序一致、彼此挨得近」的强匹配。

    只出现了「卫戍」没有「协议」不算命中；泛词重叠更不算。
    四字专名（卫戍协议）在这个口径下等于要求整串，拼不出别的可能。
    """
    e = _squash(entity)
    b = _squash(body)
    if not e:
        return False, "no_entity"
    if e in b:
        return True, "exact"
    toks = _entity_tokens(entity)
    if len(toks) < 2:
        return False, "missing"
    cursor, first, last = 0, None, None
    for tok in toks:
        i = b.find(tok, cursor)
        if i < 0:
            return False, "missing"
        if first is None:
            first = i
        cursor = i + len(tok)
        last = cursor
    span = int(last or 0) - int(first or 0)
    if span <= max(24, len(e) * 3):
        return True, "strong"
    return False, "too_far"


def _match_domain(domain, body):
    d = _squash(domain)
    b = _squash(body)
    if not d:
        return True, "unspecified"      # 没提领域就没有这道门
    if d in b:
        return True, "exact"
    parts = _entity_tokens(domain)
    if len(parts) >= 2 and all(p in b for p in parts):
        return True, "parts"
    return False, "no_evidence"


_GENERIC_TERMS = set((
    "支持 模式 玩法 游戏 内容 相关 介绍 可以 一个 这个 那个 时候 现在 官方"
    " 玩家 什么 怎么 如何 为什么 多少 哪个 哪些 是否 是谁 在哪 以及 还有"
    " 知道 告诉 觉得 认为 希望 需要 值得 情况 方面 东西 问题 今天 昨天 明天"
    " 怎么样 怎么玩 怎么办 是啥 啥 来着 一下 这种 那种"
    " 得主 获得者 赢家 冠军 结果 名单 答案"
).split())


def _question_keys(question, entity, domain):
    drop = set(_entity_tokens(entity)) | set(_entity_tokens(domain)) | _GENERIC_TERMS
    keys = []
    for w in _entity_tokens(question) + re.findall(r"\d{4,}", str(question or "")):
        s = _squash(w)
        if len(s) < 2 or s in drop or s in keys:
            continue
        keys.append(s)
    return keys


def _match_answer(question, entity, domain, body, entity_exact=False):
    keys = _question_keys(question, entity, domain)
    if not keys:
        return True, "no_extra_key"
    if not domain and entity_exact:
        # 没提硬领域时，这句话问的就是这个实体本身；修饰语（建筑/来头）
        # 不该反过来把讲对实体的页判死 —— 不确定性已经由实体门兜住了。
        return True, "entity_only"
    b = _squash(body)
    hit = [k for k in keys if k in b]
    need = max(1, (len(keys) + 1) // 2)
    if len(hit) >= need:
        return True, "hit:%d/%d" % (len(hit), len(keys))
    return False, "missing:%s" % ",".join(k for k in keys if k not in b)[:40]


def score_result(plan_obj, item, min_score=0.8):
    """这一条结果到底能不能用：实体、领域、回答要点逐项判，逐项记理由。

    返回 {accepted, entity_match, domain_match, answer_support, score, reasons}。
    判不准 = 不通过；调用方不得把 accepted=False 的条目混进结果。
    """
    p = _plan_fields(plan_obj)
    it = item if isinstance(item, dict) else {}
    title = str(it.get("title") or "")
    body = title + " " + str(it.get("snippet") or it.get("summary") or "")
    entity = p.get("entity") or ""
    domain = p.get("domain") or ""
    question = p.get("question") or p.get("query") or entity
    entity_match, entity_basis = _match_entity(entity, body)
    domain_match, domain_basis = _match_domain(domain, body)
    answer_support, answer_basis = _match_answer(
        question, entity, domain, body, entity_exact=(entity_basis == "exact"))
    score = 0.0
    if entity_match:
        score += 0.5
    if domain_match:
        score += 0.3
    if answer_support:
        score += 0.2
    temporal_match = True
    from datetime import datetime, timedelta, timezone
    today = datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8))).date()
    explicit_years = re.findall(r'(?<!\d)(20\d{2})\s*年?', question)
    historical = bool(explicit_years and all(int(y) < today.year for y in explicit_years))
    current_year = bool(re.search(r'今年|本年', question))
    dynamic = bool(re.search(r'股价|汇率|油价|现价|天气|气温|比分', question))
    current = not historical and bool(current_year or dynamic or re.search(
        r'今天|今日|明天|现在|目前|最新|实时|最近|近期', question))
    if current:
        raw_date = str(it.get('date') or it.get('datePublished') or '')
        dates = re.findall(r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})', raw_date or body)
        if dates:
            try:
                dated = datetime(*map(int, dates[-1])).date()
                age = (today - dated).days
                max_age = 2 if dynamic or re.search(r'今天|今日|明天|现在|实时', question) else 31
                temporal_match = (dated.year == today.year if current_year else -2 <= age <= max_age)
            except ValueError:
                temporal_match = False
        else:
            temporal_match = False
    accepted = bool(entity_match and domain_match and answer_support and temporal_match
                    and score >= float(min_score))
    reasons = []
    if not temporal_match:
        reasons.append('时效未证实：无可核验日期或材料已过期')
    if entity_match:
        reasons.append("实体命中（%s）" % ("逐字" if entity_basis == "exact" else "强匹配"))
    else:
        reasons.append("实体不符：正文没有完整的「%s」" % (entity or "?"))
    if not domain_match:
        reasons.append("领域无证据：未出现「%s」" % domain)
    if not answer_support:
        reasons.append("回答要点未覆盖：%s" % answer_basis)
    if not accepted and not reasons:
        reasons.append("证据不足")
    return {"accepted": accepted, "entity_match": bool(entity_match),
            "domain_match": bool(domain_match),
            "answer_support": bool(answer_support), "temporal_match": temporal_match,
            "score": round(score, 3), "reasons": reasons,
            "entity_basis": entity_basis, "domain_basis": domain_basis,
            "answer_basis": answer_basis}


def verify_page(plan_obj, title, text):
    """打开网页正文以后再判一次（口径与 score_result 完全一致）。"""
    return score_result(plan_obj, {"title": title or "", "snippet": text or ""})


def _search_kwargs(fn, session, visibility):
    full = {}
    if session:
        full["session"] = session
    if visibility:
        full["visibility"] = visibility
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        return full
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return full
    return {k: v for k, v in full.items() if k in params}


async def verified_search(query_plan, limit=None, *, search_fn=None,
                          session="", visibility="private", min_score=0.8):
    """搜一条，但只把「实体、领域、回答要点都过」的条目交出去。

    返回 {ok, results, plan, reason, queries, checked, source}。
    reason：entity_mismatch=回来的没有一条是同一个实体；
            no_answer=实体对上了但没有回答要点的证据（或压根没结果）。
    第一条查询全被拒时，用 alternates 里**最多一条**完整实体备选再试一次；
    绝不拿单字/泛词重试，也绝不把被拒条目混进 results。
    """
    p = _plan_fields(query_plan if isinstance(query_plan, dict) else await plan(str(query_plan or "")))
    q = str(p.get("query") or "").strip() or _compose_query(p.get("entity"), p.get("domain"))
    limit = int(limit or SEARCH_MAX_RESULTS)
    fn = search_fn or search
    kwargs = _search_kwargs(fn, session, visibility)
    tried, checked, entity_seen, source = [], 0, False, ""

    async def _attempt(query):
        nonlocal checked, entity_seen, source
        tried.append(query)
        try:
            got = fn(query, limit=limit, **kwargs)
            if inspect.isawaitable(got):
                got = await got
        except Exception as e:
            print("[搜索] 查询失败 " + type(e).__name__ + "：" + query[:30], flush=True)
            return []
        if not isinstance(got, dict) or not got.get("ok"):
            return []
        source = str(got.get("source") or "") or source
        kept = []
        for item in (got.get("results") or []):
            verdict = score_result(p, item, min_score=min_score)
            checked += 1
            entity_seen = entity_seen or bool(verdict.get("entity_match"))
            if verdict.get("accepted"):
                row = dict(item)
                row["verification"] = verdict
                kept.append(row)
        kept.sort(key=lambda x: -float((x.get("verification") or {}).get("score") or 0))
        return kept

    results = await _attempt(q) if q else []
    if not results:
        alternates = [a for a in (p.get("alternates") or []) if a and a != q]
        if alternates:
            results = await _attempt(str(alternates[0]))
    if results:
        return {"ok": True, "results": results, "plan": p, "reason": "",
                "queries": tried, "checked": checked, "source": source}
    reason = "no_answer" if (checked == 0 or entity_seen) else "entity_mismatch"
    return {"ok": False, "results": [], "plan": p, "reason": reason,
            "queries": tried, "checked": checked, "source": source}


def build_search_block(results):
    """把搜索结果压成给模型读的短文本。"""
    lines = []
    used = 0
    for i, it in enumerate(results or [], 1):
        title = str(it.get("title") or "").strip()
        snip = str(it.get("snippet") or "").strip()
        # 站点与日期让她能说「11月2日的价」而不是「有个网页说」
        meta = " ".join(x for x in (str(it.get("site") or "").strip(),
                                    str(it.get("date") or "").strip()) if x)
        head = "%d. %s" % (i, title)
        if meta:
            head += "（%s）" % meta
        row = "%s — %s" % (head, snip) if snip else head
        if used + len(row) > SEARCH_MAX_CHARS:
            break
        lines.append(row)
        used += len(row)
    return "\n".join(lines)


# 模型想查东西时的标记：[[查: 关键词]]
_SEARCH_RE = re.compile(r"\[\[\s*查\s*[:：]\s*([^\]\n]{1,60})\s*\]\]")


def extract_search(text):
    """抠出搜索标记。返回 (关键词 or "", 去掉标记的文本)。"""
    t = str(text or "")
    m = _SEARCH_RE.search(t)
    if not m:
        return "", t
    query = m.group(1).strip()
    return query, _SEARCH_RE.sub("", t).strip()
