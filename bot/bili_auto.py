"""她自己去刷 B 站：从公开热门里挑一条，而不是等群里有人丢链接。

为什么要单独一份：bili_worker 原来的队列只有「入站链接」这一个来源，
于是「刷 B 站」实际上等价于「别人发什么她看什么」——她从来没有自己打开过 B 站。
这里补的是来源；看视频、生成反应、记笔记、主动分享仍然走已经验证过的那条链路。

口径：
  · 只从公开热门里挑（匿名可用），不搜索、不追踪任何个人；
  · 一天最多 BILI_AUTO_MAX_PER_DAY 条（默认 5），两条之间至少隔
    BILI_AUTO_MIN_GAP_HOURS（默认 2 小时）—— 9–23 点这个窗口装得下 5 次；
    以前是 2 条 / 3 小时间隔，一个下午就把当天的量用完了，不像过日子；
  · 只在 BILI_AUTO_START_HOUR–BILI_AUTO_END_HOUR 点（服务器本地，CST）刷；
  · 优先她真的关心过的题材（好奇度话题 / 人格 curious / 知识库标题），
    没命中就只在偏好分类里挑，时政新闻类永不入选；
  · 看过的不再看；状态落盘，重启后不会把同一批热门重新刷一遍。
"""
import json
import os
import random
import re
import autonomy_limits
import time

BLOCKED_CATEGORIES = {
    "资讯", "热点", "社会", "时事", "政治", "新闻", "军事",
}
DEFAULT_PREFERRED = ("动画,游戏,音乐,鬼畜,生活,美食,动物,科技,知识,"
                     "影视,纪录片,舞蹈,时尚,运动,数码,手工,绘画,搞笑")


def _env_int(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return int(default)


def _env_float(name, default):
    try:
        return float(str(os.getenv(name, default)).strip())
    except Exception:
        return float(default)


def _file():
    # 每次现算：测试与 workers 会改 DATA_DIR，模块常量会冻结成旧路径
    return os.path.join(os.getenv("DATA_DIR", "./data"), "persona",
                        "bili_auto.json")


def _empty():
    return {"days": {}, "recent": [], "last_ts": 0.0}


def _prune(d, now):
    days = d.get("days") if isinstance(d.get("days"), dict) else {}
    cutoff = time.strftime("%Y-%m-%d", time.localtime(float(now) - 14 * 86400))
    for key in [k for k in days if str(k) < cutoff]:
        days.pop(key, None)
    d["days"] = days
    recent = [x for x in (d.get("recent") or []) if isinstance(x, dict)]
    recent.sort(key=lambda x: float(x.get("at") or 0))
    d["recent"] = recent[-80:]
    return d


def _load():
    data = _empty()
    try:
        with open(_file(), encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            for k in ("days", "recent"):
                if isinstance(got.get(k), dict if k == "days" else list):
                    data[k] = got[k]
            data["last_ts"] = float(got.get("last_ts") or 0)
    except Exception:
        pass
    return data


def _save(d):
    try:
        path = _file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("[B站] 自主状态写盘失败 " + str(e)[:60], flush=True)
        return False


def _day(now):
    return time.strftime("%Y-%m-%d", time.localtime(float(now)))


def enabled():
    return str(os.getenv("BILI_AUTO", "1")).strip().lower() not in (
        "0", "off", "false", "no")


def block_reason(now=None):
    """现在该不该去刷；能刷返回空串，不能刷返回一句人话。"""
    now = time.time() if now is None else float(now)
    if not enabled():
        return "自主刷 B 站关着（BILI_AUTO=0）"
    start = _env_int("BILI_AUTO_START_HOUR", 9)
    end = _env_int("BILI_AUTO_END_HOUR", 23)
    hour = time.localtime(now).tm_hour
    if not (start <= hour < end):
        return "现在 %d 点，不在 %d–%d 点的活动时间" % (hour, start, end)
    d = _load()
    used = int((d["days"].get(_day(now)) or {}).get("n") or 0)
    # 额度先看面板（autonomy_limits，改完下一轮就生效），再看 config.env，最后是这里的默认值。
    cap = max(0, autonomy_limits.get_int(
        "BILI_AUTO_MAX_PER_DAY", _env_int("BILI_AUTO_MAX_PER_DAY", 5)))
    if used >= cap:
        return "当天已经刷够 %d 条了" % cap
    gap = max(0.0, autonomy_limits.get_float(
        "BILI_AUTO_MIN_GAP_HOURS", _env_float("BILI_AUTO_MIN_GAP_HOURS", 2.0))) * 3600.0
    last = float(d.get("last_ts") or 0)
    if last and (now - last) < gap:
        return "离上次刷只过了 %.1f 小时，间隔没到" % ((now - last) / 3600.0)
    return ""


def _tokens(text):
    t = str(text or "").strip()
    if not t:
        return set()
    out = set()
    try:
        import text_tokens
        out.update(str(w).lower() for w in text_tokens.content_words(t))
    except Exception:
        pass
    # 兜底：中文二元组。分词器不可用时也不至于把整句当零分。
    for run in re.findall(r"[一-鿿]{2,}", t):
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    for w in re.findall(r"[A-Za-z0-9]{2,}", t):
        out.add(w.lower())
    return out


def _preferred():
    raw = os.getenv("BILI_AUTO_CATEGORIES", DEFAULT_PREFERRED)
    return {x.strip() for x in str(raw or "").split(",") if x.strip()}


def _in_categories(cat, pool):
    cat = str(cat or "").strip()
    return bool(cat) and any(p and (p in cat or cat in p) for p in pool)


def _watchable(it):
    if not isinstance(it, dict):
        return False
    if not str(it.get("bvid") or "").strip():
        return False
    if not str(it.get("title") or "").strip():
        return False
    cat = str(it.get("category") or "").strip()
    if cat in BLOCKED_CATEGORIES:
        return False
    dur = int(it.get("duration") or 0)
    lo = max(0, _env_int("BILI_AUTO_MIN_SECONDS", 20))
    hi = max(lo, _env_int("BILI_AUTO_MAX_SECONDS", 7200))
    return lo <= dur <= hi


def choose(items, interests, seen=None, now=None, rng=None):
    """从热门里挑一条。看到过的、太短太长的、时政新闻都不进候选。

    interests 是「她关心过的东西」的原始句子列表；按分词重叠打分。
    一条都不沾边时退回偏好分类，仍然不碰新闻 —— 宁可今天不刷，也不替她
    把首页塞给她的东西当成她的兴趣。
    """
    now = time.time() if now is None else float(now)
    rng = rng or random.Random()
    is_seen = seen if callable(seen) else (lambda b: b in (seen or ()))

    want = set()
    for text in (interests or []):
        want |= _tokens(text)

    pref = _preferred()
    scored = []
    fallback = []
    for it in (items or []):
        if not _watchable(it):
            continue
        bvid = str(it.get("bvid") or "")
        if is_seen(bvid):
            continue
        cat = str(it.get("category") or "").strip()
        score = 0
        if want:
            score = len(_tokens(str(it.get("title") or "") + " "
                                + str(it.get("desc") or "")) & want) * 3
        if _in_categories(cat, pref):
            score += 2
        rec = (score, int(it.get("view") or 0), bvid, it)
        if score > 0:
            scored.append(rec)
        elif _in_categories(cat, pref):
            fallback.append(rec)

    pool = scored or fallback
    if not pool:
        return None
    pool.sort(key=lambda x: (-x[0], -x[1], x[2]))
    # 只在同分的候选里随机：不然「沾一点边」和「真的很想看」会被抽成一样。
    best = pool[0][0]
    top = [x for x in pool if x[0] == best]
    return rng.choice(top[:5])[3]


def claim(item, now=None):
    """记下「这条她今天要看了」。重复的 BV 不重复占名额。

    G01：决策点经统一契约登记一份只读提案（kind=watch，无受众），
    与对外发送提案区分开。额度/间隔门禁仍由 block_reason 与调用方裁决；
    「看过了」登记为合法等待，不是错误。
    """
    now = time.time() if now is None else float(now)
    bvid = str((item or {}).get("bvid") or "").strip()
    if not bvid:
        return False
    d = _load()
    seen = any(str(x.get("bvid") or "") == bvid for x in (d.get("recent") or []))
    import action_proposal as ap
    cap = max(0, autonomy_limits.get_int(
        "BILI_AUTO_MAX_PER_DAY", _env_int("BILI_AUTO_MAX_PER_DAY", 5)))
    gap = max(0.0, autonomy_limits.get_float(
        "BILI_AUTO_MIN_GAP_HOURS", _env_float("BILI_AUTO_MIN_GAP_HOURS", 2.0))) * 3600.0
    ap.observe(ap.build(
        "bili_auto", "watch", motive="求知",
        reason="她自己在公开热门里挑了这条，想看看",
        event_ids=[], goal_id="", topic=str((item or {}).get("title") or "")[:120],
        topic_qq="", participants=[], recipient_qq="", visibility="internal",
        scope={"bvid": bvid, "category": str((item or {}).get("category") or "")[:20]},
        evidence=[], deadline=now + 86400.0,
        budget={"quota_key": "BILI_AUTO_MAX_PER_DAY",
                "max_per_day": cap, "cooldown_seconds": gap},
        success="真看完并留下笔记/反应（不是只发了请求）",
        failure="字幕/画面拿不到，或工具失败",
        abandon_when="看过的不再看；当天额度用完即止",
        decision="wait" if seen else "act",
        gate_reasons=["already_watched"] if seen else []), now=now)
    if seen:
        return False
    day = d["days"].setdefault(_day(now), {"n": 0, "bvids": []})
    day["n"] = int(day.get("n") or 0) + 1
    day["bvids"] = (day.get("bvids") or [])[-20:] + [bvid]
    d["recent"].append({
        "bvid": bvid,
        "title": str((item or {}).get("title") or "")[:120],
        "category": str((item or {}).get("category") or "")[:20],
        "at": now,
    })
    d["last_ts"] = max(float(d.get("last_ts") or 0), now)
    _prune(d, now)
    return _save(d)


def stats(now=None):
    now = time.time() if now is None else float(now)
    d = _load()
    return {
        "today": int((d["days"].get(_day(now)) or {}).get("n") or 0),
        "last_ts": float(d.get("last_ts") or 0),
        "watched": [str(x.get("bvid") or "") for x in (d.get("recent") or [])][-20:],
        "enabled": enabled(),
        "blocked": block_reason(now),
    }