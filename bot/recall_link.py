"""检索链路：跨库去重、带使用反馈的打分、可检索性判定、查不到时的降级渲染。

放在一个模块里的理由：这四件事本来就是一件事 —— 「这一轮到底该给她带哪些记忆」。
以前它散在四个库里各写一套，谁都不去看别的库存了什么，
于是同一句「主人家在重庆」能占四份预算，而查不到时又各库自己兜底塞旧的。
"""
import math
import os
import time

ENABLED = os.getenv("RECALL_MODE", "v2").strip().lower() == "v2"

# 优先级：她自己查证过的 > 她自己的档案 > 她记得的某个人 > 群里的事 > 压缩摘要。
# 越靠前越是「她的东西」，重复时留下它。
PRIORITY = ["world_knowledge",
            "persona:facts", "persona:experiences", "persona:likes",
            "persona:dislikes", "persona:self_view", "persona:curious",
            "member", "group_facts", "private_summary"]

# 相似度阈值：两条讲的基本是同一件事，就只留优先级高的那份
SIM_THRESHOLD = float(os.getenv("RECALL_DEDUPE_SIM", "0.55"))

NO_MEMORY_MARK = "（这一轮没有查到相关的记忆，别硬凑，按你本来知道的说就行。）"

# 时间衰减半衰期（天）。太陡会把老而重要的事压死，太平又会让陈旧内容常驻。
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECALL_RECENCY_HALF_LIFE_DAYS", "30"))
RECENCY_WEIGHT = float(os.getenv("RECALL_RECENCY_WEIGHT", "3.0"))
USAGE_WEIGHT = float(os.getenv("RECALL_USAGE_WEIGHT", "1.5"))
IMPORTANCE_WEIGHT = float(os.getenv("RECALL_IMPORTANCE_WEIGHT", "0.6"))


def priority_of(store):
    s = str(store or "")
    if s in PRIORITY:
        return PRIORITY.index(s)
    # persona:xxx 之类的没列全的写法，按前缀归到同档
    for i, p in enumerate(PRIORITY):
        if s.startswith(p.split(":")[0]) and p.startswith("persona"):
            return i
    return len(PRIORITY)


def _sim(a, b):
    """两条文本的实词重合度（Jaccard）。口径走系统统一分词。"""
    try:
        import text_tokens as _tt
        ta = _tt.content_words(a)
        tb = _tt.content_words(b)
    except Exception:
        ta = [w for w in str(a or "").split() if len(w) > 1]
        tb = [w for w in str(b or "").split() if len(w) > 1]
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    inter = len(sa & sb)
    if not inter:
        return 0.0
    return inter / float(len(sa | sb))


def dedupe(cands):
    """跨库去重：同一件事只留优先级最高那份。"""
    items = [c for c in (cands or []) if isinstance(c, dict)]
    if len(items) <= 1:
        return list(items)
    items.sort(key=lambda c: priority_of(c.get("store")))
    kept = []
    for c in items:
        txt = str(c.get("text") or "")
        dup = False
        for k in kept:
            if _sim(txt, str(k.get("text") or "")) >= SIM_THRESHOLD:
                dup = True
                k.setdefault("dupes", []).append(c.get("store"))
                break
        if not dup:
            kept.append(dict(c))
    return kept


def final_score(entry):
    """相关度 + 重要度 + 使用反馈 + 时间衰减。缺字段一律当 0，不许抛。"""
    try:
        e = entry or {}
        rel = float(e.get("relevance") or 0.0)
        imp = float(e.get("importance") or 0.0)
        used = int(e.get("used") or 0)
        ts = float(e.get("ts") or 0.0)
        s = rel + imp * IMPORTANCE_WEIGHT
        if used > 0:
            # 用过的确实更该带上，但对数封顶：别让一条被反复命中的旧条目永居榜首
            s += math.log1p(used) * USAGE_WEIGHT * 0.5
        if ts > 0:
            age_days = max(0.0, (time.time() - ts) / 86400.0)
            s += RECENCY_WEIGHT * math.pow(0.5, age_days / max(1.0, RECENCY_HALF_LIFE_DAYS))
        else:
            s += RECENCY_WEIGHT * 0.25     # 不知道日期的，别当成最新，也别一票否决
        return float(s)
    except Exception:
        return 0.0


def query_searchable(text):
    """这句有没有足够的实词去检索。

    实测 1306 条真实消息平均只有 2.21 个实词、8.8% 一个都没有；
    「笑死我了」这类句子拿去做词法检索，结果必然是空，
    然后落进各库的兜底 —— 那比不检索更糟。
    """
    try:
        import text_tokens as _tt
        words = _tt.content_words(text)
    except Exception:
        words = [w for w in str(text or "").split() if len(w) > 1]
    if not words:
        return False
    if any(len(w) >= 3 for w in words):
        return True
    return len(words) >= 2


def render(cands, budget=600):
    """把选中的条目拼成注入文本，超预算就少给，不硬塞。

    一条都没有时给一句明确标记，让她知道「这轮没有相关记忆」，
    而不是像以前那样被兜底塞一串不相关的旧条目、再靠提示词喊「别报菜名」。
    """
    items = [c for c in (cands or []) if isinstance(c, dict) and str(c.get("text") or "").strip()]
    if not items:
        return NO_MEMORY_MARK if budget >= len(NO_MEMORY_MARK) else ""
    items.sort(key=lambda c: -final_score(dict(c.get("_meta") or {})))
    limit = max(80, int(budget))
    lines, used = [], 0
    for c in items:
        row = "- [" + str(c.get("store") or "?")[:14] + "] " + str(c.get("text"))[:160]
        if used + len(row) > limit:
            continue
        lines.append(row)
        used += len(row)
    if not lines:
        return NO_MEMORY_MARK
    return "【这一轮相关的记忆】\n" + "\n".join(lines)


def dedupe_lines(block, covered, budget=None):
    """从已渲染的注入块里剔掉「别处已经讲过」的行。

    优先级写在 PRIORITY 里：她自己查证的 > 她自己的档案 > 群里的事。
    四个库以前各自拼块、互相看不见，同一句「主人家在重庆」能占三份预算
    —— 而预算就那么大，多出来的都是挤掉别的有用记忆换来的。
    """
    text = str(block or "")
    if not text.strip():
        return ""
    cov = [str(c or "").strip() for c in (covered or []) if str(c or "").strip()]
    if not cov:
        return text if budget is None or len(text) <= budget else text[:budget]
    head, _, body = text.partition(chr(10))
    lines = [l for l in body.split(chr(10)) if l.strip()]
    kept = []
    used = len(head) + 1
    limit = int(budget) if budget else 10 ** 9
    for l in lines:
        content = l.lstrip("- ").strip()
        # 去掉 [话题] 前缀再比，否则话题名会把相似度整体拉低
        core = content
        if core.startswith("[") and "]" in core:
            core = core.split("]", 1)[1].strip()
        dup = any(_sim(core, c) >= SIM_THRESHOLD for c in cov)
        if dup:
            continue
        if used + len(l) + 1 > limit:
            continue
        kept.append(l)
        used += len(l) + 1
    if not kept:
        return NO_MEMORY_MARK
    return head + chr(10) + chr(10).join(kept)


def stats():
    return {"enabled": ENABLED, "priority": PRIORITY,
            "sim_threshold": SIM_THRESHOLD,
            "half_life_days": RECENCY_HALF_LIFE_DAYS}
