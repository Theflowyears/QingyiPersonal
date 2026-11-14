# 本模块只负责「攒分、判该不该行动、限频率」，
# 真正去查、去记都由桥那边复用已经跑通的检索链路。
"""好奇度：让她自己想弄懂点什么，并且查完真的记住。

三条硬约束，都是用户定的口径：
  1. 种子必须是**她的** —— 只从她的人格档案 curious、她自己写的愿望、
     群里真实热度、以及「她说过要查却没能查到」这四个来源长出来。
     绝不随机造话题：那是「像人的技巧」，不是她的好奇。
  2. 行动产物默认只在私聊可见（CURIOSITY_ACT_SCOPE=private）。
  3. 主动跟主人讲一条是允许的，但必须限速：
     每日上限 + 最小间隔 + 同一话题永不重复讲，否则就是打扰。

上线口径：MAX_PER_DAY 默认 0 —— 先只攒不动，观察几天种子质量再放开。
"""
import hashlib
import json
import os
import time
import uuid

def _file():
    # 每次现算：DATA_DIR 在进程里被测试和 workers 改过，
    # 模块级常量会在 import 时冻结成旧路径，测试之间互相写脏。
    # 默认值也留在这里而不是模块常量 —— 留一份 `_ROOT = os.getenv(...)`
    # 在外面，等于又埋一颗同样的雷（tests.test_structure_guards 会抓这个）。
    return os.path.join(os.getenv("DATA_DIR", "./data"), "persona",
                        "curiosity.json")

# 权重：她自己问过的最重，愿望其次，被提到再次，群里热度最低
W_SELF_ASKED = float(os.getenv("CURIO_W_SELF_ASKED", "4.0"))
W_WISH = float(os.getenv("CURIO_W_WISH", "3.0"))
W_DEFERRED = float(os.getenv("CURIO_W_DEFERRED", "2.5"))
W_MENTION = float(os.getenv("CURIO_W_MENTION", "1.0"))
W_HEAT = float(os.getenv("CURIO_W_HEAT", "0.6"))

# 门槛必须**低于**单颗种子的权重，否则按半衰期一衰减就永远够不上：
# 曾经把 ACT_THRESHOLD 设成和 W_SELF_ASKED 一样的 4.0，结果是
# 「一条档案种子能撑 0.0 天」—— 种子写进去的那一秒就已经不合格，
# 配额给了也白给。3.0 对应一条新种子约 5.8 天的有效期，
# 而真正管着她别乱跑的是 CURIOSITY_MAX_PER_DAY，不是这个悬崖。
ACT_THRESHOLD = float(os.getenv("CURIOSITY_ACT_THRESHOLD", "3.0"))
DECAY_HALF_LIFE_DAYS = float(os.getenv("CURIOSITY_HALF_LIFE_DAYS", "14"))
SATURATION_DAYS = float(os.getenv("CURIOSITY_SATURATION_DAYS", "10"))
FADE_DAYS = float(os.getenv("CURIOSITY_FADE_DAYS", "45"))
MAX_TOPICS = int(os.getenv("CURIOSITY_MAX_TOPICS", "300"))
# 默认 0 = 只攒分不动，是新链路的上线闸门（先看几天种子质量）。
# 现网在 config.env 里给到 5 次/天 —— 以前是 2，上午一用完，
# 剩下大半天她什么也不会去查，「她自己会学习」就只剩一句说明。
# 主动跟主人讲的次数没跟着涨：那是打扰预算，不是她的活动预算（SHARE_MAX_PER_DAY）。
MAX_PER_DAY = int(os.getenv("CURIOSITY_MAX_PER_DAY", "0"))
MAX_CONCURRENT = int(os.getenv("CURIOSITY_MAX_CONCURRENT", "1"))
SHARE_MAX_PER_DAY = int(os.getenv("CURIOSITY_SHARE_MAX_PER_DAY", "1"))
SHARE_MIN_INTERVAL_HOURS = float(os.getenv("CURIOSITY_SHARE_MIN_INTERVAL_HOURS", "6"))


# ── 生效额度：面板能热改，不要再直接读上面那几个常量 ──────────
# 那几个常量是 import 时定死的，面板改完要重启才动 —— 而重启会断 QQ。
# autonomy_limits 持有「当前生效值」，控制层保存设置时推进来；
# 它没被设过时这里退回常量（单元测试补丁常量仍然有效）。
def max_per_day():
    import autonomy_limits
    return max(0, autonomy_limits.get_int("CURIOSITY_MAX_PER_DAY", MAX_PER_DAY))


def share_max_per_day():
    import autonomy_limits
    return max(0, autonomy_limits.get_int("CURIOSITY_SHARE_MAX_PER_DAY",
                                          SHARE_MAX_PER_DAY))


def share_min_interval_hours():
    import autonomy_limits
    return max(0.0, autonomy_limits.get_float("CURIOSITY_SHARE_MIN_INTERVAL_HOURS",
                                              SHARE_MIN_INTERVAL_HOURS))
# 系统原因（模型故障/答案被过滤）没讲成的次数上限。到顶就放弃，
# 免得一条永远生成不出来的话题天天占着唯一的队列位置。
MAX_SHARE_TRIES = int(os.getenv("CURIOSITY_SHARE_MAX_TRIES", "3"))
MAX_QUERIES_PER_ACT = int(os.getenv("CURIOSITY_MAX_QUERIES", "3"))
MAX_FAILS = int(os.getenv("CURIOSITY_MAX_FAILS", "3"))
ACT_SCOPE = os.getenv("CURIOSITY_ACT_SCOPE", "private")

STATE_SEED = "萌芽"
STATE_BUILD = "积累"
STATE_ACT = "行动"
STATE_SATISFIED = "满足"
STATE_FADED = "消退"

# 只在模块启动时生成；清缓存/文件变动不是进程重启，不能释放仍在执行的分享。
_RUNTIME_OWNER = uuid.uuid4().hex
_cache = {"sig": None, "data": None}


def _reset_cache():
    _cache.update({"sig": None, "data": None})


def _empty():
    return {"topics": {}, "actions": {}, "shares": [], "pending_share": None}


def _file_sig():
    try:
        st = os.stat(_file())
        return (round(st.st_mtime, 3), st.st_size)
    except Exception:
        return None


def _data():
    sig = _file_sig()
    if sig is None:
        if _cache["data"] is None:
            _cache["data"] = _empty()
        return _cache["data"]
    if _cache["sig"] == sig and _cache["data"] is not None:
        return _cache["data"]
    data = _empty()
    try:
        with open(_file(), encoding="utf-8") as f:
            got = json.load(f) or {}
        if isinstance(got, dict):
            for k in ("topics", "actions"):
                if isinstance(got.get(k), dict):
                    data[k] = got[k]
            for k in ("shares", "pending_share"):
                if k in got:
                    data[k] = got[k]
    except Exception:
        import safe_store
        safe_store.preserve_corrupt(_file())
        data = _empty()
    _cache.update({"sig": None, "data": data})
    recovered = 0
    for rec in data["topics"].values():
        if (rec.get("share_taken")
                and rec.get("share_owner") != _RUNTIME_OWNER):
            # Only pre-send generation reservations are safe to release. A crash
            # after the durable send boundary is ambiguous, never an automatic retry.
            state = rec.get('share_delivery_state')
            if state in ('submitted', 'unknown', 'accepted'):
                if state == 'submitted':
                    rec['share_delivery_state'] = 'unknown'
                continue
            rec["share_taken"] = False
            rec.pop("share_owner", None)
            recovered += 1
    if recovered:
        _save(data)
        print("[好奇度] 恢复 %d 条旧进程分享预留：结果 unknown，保留内容，不计已分享"
              % recovered, flush=True)
    return data


def _save(data=None):
    d = data if isinstance(data, dict) else _cache.get("data")
    if not isinstance(d, dict):
        return False
    import safe_store
    if safe_store.atomic_write_json(_file(), d):
        _cache["data"] = d
        _cache["sig"] = None
        return True
    return False


def _tid(topic):
    return hashlib.sha1(str(topic or "").strip().lower().encode("utf-8", "ignore"))\
        .hexdigest()[:12]


def _day(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(float(ts or time.time())))


def topic(name):
    d = _data()
    return (d.get("topics") or {}).get(_tid(name))


def all_topics():
    d = _data()
    out = list((d.get("topics") or {}).values())
    out.sort(key=lambda x: -float(x.get("last_seen") or 0))
    return out


def _touch(name, seed_from="", weight=0.0, now=None, cap=64, reason="", tag="",
           event_ids=()):
    """加一次「她对这件事有兴趣」的证据。相同话题合并，不重复建条。

    tag：给「每次整点都重灌一遍」的种子用的幂等标记。同一个 tag 只计分一次，
    否则每轮 +4 分，攒几轮什么废话都能越过行动门槛 —— 那就不是好奇，是计数器。

    event_ids：是哪几件事让她好奇的。不记这个，她自己去查、自己去讲的时候
    就说不清起因（线上账本里查证动作的证据一直是空的，就是这么来的）。
    """
    now = float(now or time.time())
    d = _data()
    tps = d.setdefault("topics", {})
    tid = _tid(name)
    rec = tps.get(tid)
    if not isinstance(rec, dict):
        rec = {"topic": str(name or "")[:cap], "tid": tid, "points": [],
               "state": STATE_SEED, "seed_from": seed_from or "unknown",
               "first_seen": now, "last_seen": now, "mentions": 0,
               "last_satisfied": 0.0, "shared": False, "linked_keys": [],
               "seeded": []}
        if len(tps) >= MAX_TOPICS:
            # 满了就先淘汰「最久没被提起且没满足过」的，别把新的挤不出去
            try:
                cold = sorted([v for k, v in tps.items()
                               if str(v.get("state") or "") != STATE_SATISFIED],
                              key=lambda v: float(v.get("last_seen") or 0))
                for drop in cold[:len(tps) - MAX_TOPICS + 1]:
                    tps.pop(drop.get("tid"), None)
            except Exception:
                pass
        tps[tid] = rec
    if tag:
        seeded = rec.setdefault("seeded", [])
        if tag in seeded:
            weight = 0.0          # 这颗种子早就种过了，只续命不加分
            counted = False
        else:
            seeded.append(tag)
            rec["seeded"] = seeded[-24:]
            counted = True
    else:
        counted = True
    if counted:
        rec["mentions"] = int(rec.get("mentions") or 0) + 1
    if weight > 0:
        rec.setdefault("points", []).append([round(now, 1), float(weight)])
        rec["points"] = rec["points"][-40:]
    rec["last_seen"] = max(float(rec.get("last_seen") or 0), now)
    _ev = [str(x) for x in (event_ids or []) if x]
    if _ev:
        keep = [str(x) for x in (rec.get("origin_events") or []) if x]
        for eid in _ev:
            if eid not in keep:
                keep.append(eid)
        # Keep original causal identity across retries instead of replacing it
        # with the latest mentions. Historical references are not rewritten.
        rec["origin_events"] = keep[:12]
    if seed_from:
        # 来源取最强那个：她自己的愿望不该被群里热度盖过去
        order = {"unknown": 0, "group_heat": 1, "mention": 2, "deferred": 3,
                 "wish": 4, "persona_curious": 5, "self_asked": 6}
        cur = str(rec.get("seed_from") or "unknown")
        if order.get(seed_from, 0) >= order.get(cur, 0):
            rec["seed_from"] = seed_from
    rec["state"] = STATE_BUILD if str(rec.get("state") or "") in (STATE_SEED,
                                                                 STATE_FADED) \
        else rec.get("state")
    if reason:
        # 一次性写好再落盘：分两步 save 会踩到 _data() 重新加载后的对象不是同一个
        rs = rec.setdefault("deferred_reasons", [])
        rs.append(str(reason)[:40])
        rec["deferred_reasons"] = rs[-5:]
    _save(d)
    return rec


def score_at(name, now=None):
    """按半衰期衰减的累计兴趣分。"""
    now = float(now or time.time())
    rec = topic(name)
    if not rec:
        return 0.0
    hl = max(1.0, DECAY_HALF_LIFE_DAYS) * 86400.0
    total = 0.0
    for ts, w in (rec.get("points") or []):
        age = max(0.0, now - float(ts))
        total += float(w) * (0.5 ** (age / hl))
    return round(total, 4)


def note_mention(name, weight=W_MENTION, now=None, seed_from="mention", event_ids=()):
    return _touch(name, seed_from=seed_from, weight=weight, now=now, event_ids=event_ids)


def note_self_asked(name, now=None, event_ids=()):
    """她自己开口问的 —— 这是最强的一类好奇。

    event_ids 是「当时哪句话让她想问」：没有它，事后只剩一个光秃秃的话题名。
    """
    return _touch(name, seed_from="self_asked", weight=W_SELF_ASKED, now=now,
                  event_ids=event_ids)


def record_deferred(name, reason="", now=None, event_ids=()):
    """她说过「等我翻一下」却没能查到 —— 以前这个信号直接被丢掉。

    这其实是最自然的好奇来源：她自己承认不知道的事。
    """
    return _touch(name, seed_from="deferred", weight=W_DEFERRED,
                  now=now, reason=reason, event_ids=event_ids)


def record_failed(name, reason="", now=None):
    """自己去查了没查到 —— 只加失败计数，不加兴趣分。

    和 record_deferred 分开是有意的：那句是「她自己承认不知道」，是好奇的来源；
    这条是「行动落空」，只该用来关掉这个行动的循环。
    """
    rec = topic(name) or _touch(name, seed_from="unknown", weight=0.0, now=now)
    d = _data()
    rec = (d.get("topics") or {}).get(_tid(name)) or rec
    rec["fails"] = int(rec.get("fails") or 0) + 1
    rec["state"] = STATE_FADED if rec["fails"] >= MAX_FAILS else STATE_BUILD
    rec.pop("acted_at", None)
    if reason:
        rec["last_fail"] = str(reason)[:40]
    _save(d)
    return rec


def seeds_from_persona(persona):
    """从她人格档案里的 curious 长种子。

    传 dict（persona_growth.load() 的原样）或直接传列表都接：
    只按列表迭代的话，喂进 dict 会安静地拿到「curious」这个键当话题，
    那种种子最难查，所以宁可在入口分一下。
    """
    items = persona
    if isinstance(persona, dict):
        items = persona.get("curious") or []
    out = []
    for x in (items or []):
        t = str(x or "").strip()
        if not t:
            continue
        out.append({"topic": t[:64], "seed_from": "persona_curious",
                    "weight": W_SELF_ASKED})
    return out


def seeds_from_wishes(wishes):
    """愿望墙里只有「想知道」那一类是好奇。

    「想要」是请主人办事（换头像、拍张照），把它当好奇去联网查，
    查回来的必然对不上 —— 现网 29 条愿望里 16 条是 want，全都不该进这个账。
    """
    out = []
    for w in (wishes or []):
        if str((w or {}).get("kind") or "") != "know":
            continue
        t = str((w or {}).get("text") or "").strip()
        if not t:
            continue
        spec = {"topic": t[:64], "seed_from": "wish", "weight": W_WISH,
                "wish_id": str((w or {}).get("id") or "")}
        for key in ("event_ids", "origin_events"):
            if w.get(key):
                spec[key] = w[key]
        out.append(spec)
    return out


def should_act(name, now=None):
    """够不够格行动：分够了，而且不在「刚查明白」的饱和期里。"""
    now = float(now or time.time())
    rec = topic(name)
    if not rec:
        return False
    if rec.get("state") == STATE_ACT:
        return False
    if int(rec.get("fails") or 0) >= MAX_FAILS:
        # 连着几次自己查也没查到的话题就放过它：
        # 否则失败给它加分，加分让它再失败，变成永远排第一的死循环
        return False
    if score_at(name, now) < ACT_THRESHOLD:
        return False
    ls = float(rec.get("last_satisfied") or 0)
    if ls and (now - ls) < SATURATION_DAYS * 86400.0:
        return False
    return True


def quota_left(now=None):
    now = float(now or time.time())
    d = _data()
    used = int(((d.get("actions") or {}).get(_day(now)) or {}).get("n") or 0)
    return max(0, max_per_day() - used)


def claim_action(name, now=None):
    """占一个行动名额。配额用完、或还不够格，就不该真去查。

    G01：决策点同时经统一契约登记一份行动提案（只读 search）。
    门禁结论仍由 should_act/quota_left 给出，契约只旁路观测；
    「不够格/配额用完」登记为合法等待（decision=wait），不是错误。
    """
    now = float(now or time.time())
    eligible = should_act(name, now)
    left = quota_left(now)
    gate = []
    if not eligible:
        gate.append("not_eligible")
    if left <= 0:
        gate.append("quota_exhausted")
    rec = topic(name) or {}
    origin = [str(x) for x in (rec.get("origin_events") or []) if x]
    import action_proposal as ap
    ap.observe(ap.build(
        "curiosity", "search", motive="求知",
        reason="她自己攒够了好奇分，想弄懂《%s》" % str(name or "")[:40],
        event_ids=origin, goal_id="", topic=str(name or "")[:64],
        topic_qq="", participants=[], recipient_qq="", visibility="internal",
        scope={"act_scope": ACT_SCOPE}, evidence=origin,
        deadline=now + 86400.0,
        budget={"quota_key": "CURIOSITY_MAX_PER_DAY",
                "max_per_day": max_per_day(),
                "cooldown_seconds": SATURATION_DAYS * 86400.0},
        success="找到可追溯的资料，或明确记下「还没证实」",
        failure="搜索/阅读失败，或没有可用结果",
        abandon_when="连续 %d 次自己查也查不到就放弃这条" % MAX_FAILS,
        decision="wait" if gate else "act", gate_reasons=gate,
        motivation_score=score_at(name, now)), now=now)
    if not eligible:
        return False
    if left <= 0:
        return False
    d = _data()
    day = d.setdefault("actions", {}).setdefault(_day(now), {"n": 0, "topics": []})
    day["n"] = int(day.get("n") or 0) + 1
    day["topics"] = (day.get("topics") or [])[-20:] + [_tid(name)]
    rec = d["topics"].get(_tid(name))
    if isinstance(rec, dict):
        rec["state"] = STATE_ACT
        rec["acted_at"] = now
    _save(d)
    return True


def mark_satisfied(name, linked_keys=None, now=None, *, origin_session="",
                   visibility="private", requester_qq="", recipient_qq="",
                   event_ids=()):
    now = float(now or time.time())
    if not topic(name) or event_ids:
        # 她自己开口问的那种好奇也可能第一次就查到答案，先补一条空账
        _touch(name, seed_from="self_asked", weight=0.0, now=now, event_ids=event_ids)
    # _touch 里落过盘，之后 _data() 可能已经把文件重新读成一个新对象；
    # 拿旧引用改就等于改在一个已经和磁盘脱钩的 dict 上，条目会静默丢掉
    d = _data()
    rec = (d.get("topics") or {}).get(_tid(name))
    if not isinstance(rec, dict):
        return None
    rec["state"] = STATE_SATISFIED
    rec["last_satisfied"] = now
    rec["fails"] = 0     # 这次真查明白了，以前几次落空不该继续压着它
    rec["linked_keys"] = [str(k)[:40] for k in (linked_keys or [])][:8]
    _save(d)
    # 查明白的事也走同一个分享队列：以前这里自己写了一套排期逻辑，
    # 于是「刷到视频」和「查懂知识」两条路各有各的门禁，谁也看不见谁
    push_share(name, linked_keys=rec.get("linked_keys") or [], kind="know",
               now=now, origin_session=origin_session, visibility=visibility,
               requester_qq=requester_qq, recipient_qq=recipient_qq,
               event_ids=event_ids or rec.get("origin_events") or [])
    return rec


def sweep(now=None):
    """长期没人再提、也没被查明白的话题进入消退。记录不删。"""
    now = float(now or time.time())
    d = _data()
    faded = 0
    for rec in (d.get("topics") or {}).values():
        if str(rec.get("state") or "") in (STATE_SATISFIED, STATE_FADED):
            continue
        last = float(rec.get("last_seen") or 0)
        if last and (now - last) > FADE_DAYS * 86400.0:
            rec["state"] = STATE_FADED
            rec["faded_at"] = now
            faded += 1
    if faded:
        _save(d)
    return {"faded": faded, "topics": len(d.get("topics") or {})}


def shares_today(now=None):
    now = float(now or time.time())
    d = _data()
    day = _day(now)
    return len([s for s in (d.get("shares") or []) if _day(s.get("ts")) == day])


def last_share_ts(now=None):
    d = _data()
    ts = [float(s.get("ts") or 0) for s in (d.get("shares") or [])]
    return max(ts) if ts else 0.0


def _share_scope(origin_session="", visibility="private", requester_qq="",
                 recipient_qq=""):
    return {"origin_session": str(origin_session or ""),
            "visibility": str(visibility or "private"),
            "requester_qq": str(requester_qq or ""),
            "recipient_qq": str(recipient_qq or "")}


def _share_allowed(p, recipient_qq=None):
    """旧的无来源条目兼容；带私聊来源的内容只能回到来源人。"""
    target = str(recipient_qq or "")
    recipient = str(p.get("recipient_qq") or "")
    if recipient and target != recipient:
        return False
    session = str(p.get("origin_session") or "")
    if session.startswith("private-user-"):
        if target != session[len("private-user-"):] or not target:
            return False
    elif p.get("visibility", "private") != "public":
        requester = str(p.get("requester_qq") or "")
        if requester and target != requester:
            return False
        if session and not requester and not recipient:
            return False       # 来源可见范围不明，不能猜成可以给主人看
    return True


def _already_shared(d, name):
    tid = _tid(name)
    rec = (d.get("topics") or {}).get(tid) or {}
    return bool(rec.get("shared") or any(s.get("tid") == tid
                for s in (d.get("shares") or [])))


def share_candidate(now=None, recipient_qq=None):
    """还没主动讲过、而且手里真有内容的那条（含队列溢出的原始内容）。

    没有 share_payload 且 linked_keys 为空 = 她查到了但判「不值得记」，脑子里没留下具体说法。
    那种不能当候选：实测里它会把刚真正搞懂的「荒坂塔」从队列里顶掉，
    结果是两条都没讲成 —— 分享得有东西可讲，不只是「查过」。
    """
    now = float(now or time.time())
    d = _data()
    cands = [v for v in (d.get("topics") or {}).values()
             if not _already_shared(d, v.get("topic"))
             and not v.get("share_taken")
             and v.get('share_delivery_state') not in ('submitted', 'unknown', 'accepted')
             and _share_allowed(v.get("share_payload") or {}, recipient_qq)
             and (v.get("share_payload") or
                  (str(v.get("state") or "") == STATE_SATISFIED
                   and (v.get("linked_keys") or [])
                   and float(v.get("last_satisfied") or 0) > 0))]
    if not cands:
        return None
    cands.sort(key=lambda v: -float((v.get("share_payload") or {}).get("ts")
                                    or v.get("last_satisfied") or 0))
    return cands[0]


def share_block_reason(now=None, recipient_qq=None):
    """分享排不上队的原因；能排就返回空串。

    单独写一个函数是因为「她没主动讲话」这件事必须有答案。
    以前每个门禁都只 `return False`，线上出现「pending_share 一直排着、
    日志却说没分享」时，没人能看出是哪一道门关掉了 —— 观测不到的限速
    等于随机行为。

    「有东西可讲」有两种：队列里已经排着一条（刷到视频、看图这种自带内容，
    不必先挤进知识库），或者有一条真查明白的知识。只看后者会把前者永远堵死。
    """
    now = float(now or time.time())
    if shares_today(now) >= share_max_per_day():
        return "今天已经讲过一条了"
    last = last_share_ts(now)
    if last and (now - last) < share_min_interval_hours() * 3600.0:
        return "离上次讲只过了 %.1f 小时，没到最小间隔" % ((now - last) / 3600.0)
    d = _data()
    p = d.get("pending_share")
    if isinstance(p, dict) and p.get("topic") and _share_allowed(p, recipient_qq):
        return ""
    if share_candidate(now, recipient_qq=recipient_qq):
        return ""
    if isinstance(p, dict) and p.get("topic"):
        return "来源内容不对这个收件人可见"
    return "手里没有可讲的新东西"


def push_share(topic, detail="", linked_keys=(), kind="know", now=None,
               tries=0, *, origin_session="", visibility="private",
               requester_qq="", recipient_qq="", event_ids=()):
    """排进唯一待发位置；满位/日额度满返回 False，但内容留作候选，不顶掉旧条目。

    origin_session / visibility 是内容来源；requester_qq / recipient_qq 绑定隐私边界。
    失败重试仍用本函数，保留原参数并递增 tries，不调用 record_share。
    kind 决定她怎么说：know=查明白的知识，watch=刷到的视频，photo=看图看到的。
    detail 直接带内容，这样刷视频/看图这两类不用先挤进知识库才配说话。

    tries 是「因为系统原因没讲成」的次数。这条必须和「她说了 [[不说]]」分开：
    前者是模型故障或答案被过滤，话题不该算她讲过；后者是她自己的决定，
    讲过一次就不再提。混成一条路的后果是 —— 一次系统故障就替她永久闭嘴。
    """
    now = float(now or time.time())
    t = str(topic or "").strip()[:64]
    if not t:
        return False
    # 必须真带得出内容：要么排队时就给了 detail（刷视频、看图），
    # 要么有知识库的键可以回查（查明白的知识）。
    # 两者都没有却排进队的话，她那天唯一一条机会就被一个「讲不出所以然」的
    # 话题占掉了 —— 而队列只有一个位置。
    keys = [str(k)[:40] for k in (linked_keys or []) if str(k or "").strip()]
    if not str(detail or "").strip() and not keys:
        return False
    d = _data()
    if _already_shared(d, t):
        return False           # 暂无新内容 ID 契约，沿用同话题永不重复的口径
    if not isinstance((d.get("topics") or {}).get(_tid(t)), dict):
        _touch(t, now=now)
        d = _data()
    rec = d["topics"][_tid(t)]
    scope = _share_scope(origin_session, visibility, requester_qq, recipient_qq)
    old_payload = rec.get("share_payload") or {}
    if old_payload:
        old_scope = {key: old_payload.get(key, default)
                     for key, default in _share_scope().items()}
        for key, value in scope.items():
            # 无身份参数的旧调用以 private 为默认值，不代表要改掉原来的 public。
            if (key == "visibility" and value == "private"
                    and not any((origin_session, requester_qq, recipient_qq))):
                continue
            # 来源/收件人是整包身份；也不接受给旧包另绑一个原先没有的收件人。
            if value and value != old_scope[key]:
                print("[好奇度] 分享来源冲突（%s），保留原内容与边界" % key, flush=True)
                return False
        scope = old_scope
    # G01：排进分享队列前，经统一契约登记一份对外私聊提案。
    # 来源/收件人边界仍由 _share_allowed 与发送门裁决，契约只旁路观测；
    # 收件人此刻未绑定的，如实登记 recipient_bound_at_send_gate。
    import action_proposal as ap
    ap.observe(ap.build(
        "curiosity", "private_share", motive="联结",
        reason="查明白/刷到了值得跟相关的人讲一句的内容",
        event_ids=event_ids, goal_id="", topic=t, topic_qq="",
        participants=[], recipient_qq=scope.get("recipient_qq") or "",
        visibility=scope.get("visibility") or "private", scope=dict(scope),
        evidence=keys, deadline=now + 86400.0,
        budget={"quota_key": "CURIOSITY_SHARE_MAX_PER_DAY",
                "max_per_day": share_max_per_day(),
                "cooldown_seconds": share_min_interval_hours() * 3600.0},
        success="私聊里讲成一条（发送回执 accepted）",
        failure="生成失败或发送失败，名额退回",
        abandon_when="她自己决定不说，或连续 %d 次系统失败" % MAX_SHARE_TRIES,
        decision="act"), now=now)
    payload = {
        "topic": t, "tid": _tid(t), "ts": now, "kind": str(kind or "know")[:12],
        "detail": str(detail or "")[:400],
        "linked_keys": keys[:8],
        "event_ids": [str(x) for x in (event_ids or []) if x][:3],
        "tries": max(0, int(tries or 0)),
        **scope,
    }
    # Once created, intent/provenance/content cannot be replaced by a retry.
    if rec.get('share_delivery_state') in ('submitted', 'unknown', 'accepted'):
        return False
    if old_payload:
        payload = {**old_payload, 'tries': max(int(old_payload.get('tries') or 0),
                                             int(tries or 0))}
    payload.setdefault('intent_id', uuid.uuid4().hex)
    rec["share_payload"] = payload
    rec["share_taken"] = False
    rec.pop("share_owner", None)
    old = d.get("pending_share")
    if ((isinstance(old, dict) and old.get("topic"))
            or shares_today(now) >= share_max_per_day()):
        _save(d)               # 位置满/额度满仍保留内容，空队列时用 share_candidate 补位
        return False
    d["pending_share"] = payload
    return _save(d)


def should_share(now=None, recipient_qq=None):
    """限速三件事：每日上限、最小间隔、有没得可讲。

    主动开话头只在私聊发生（ACT_SCOPE=private），群里这一版不做。
    """
    return not share_block_reason(now, recipient_qq=recipient_qq)


def record_share(name, now=None, sent=True):
    """这条话题算「已经讲过了」。

    sent=False 是她自己决定不说：话题照样从分享队列里划掉（不然会一直排着），
    但不占日上限 —— 没发出去的那条不构成打扰，扣名额反而是替她做决定。
    """
    now = float(now or time.time())
    if not topic(name):
        _touch(name, now=now)
    d = _data()
    if sent:
        d.setdefault("shares", []).append({"tid": _tid(name),
                                           "topic": str(name or "")[:64],
                                           "ts": now})
        d["shares"] = d["shares"][-60:]
    rec = d.get("topics", {}).get(_tid(name))
    if isinstance(rec, dict):
        rec["shared"] = True
        rec["shared_at"] = now
        rec.pop("share_payload", None)
        rec.pop("share_taken", None)
        rec.pop("share_owner", None)
    pending = d.get("pending_share")
    if isinstance(pending, dict) and _tid(pending.get("topic")) == _tid(name):
        d["pending_share"] = None
    _save(d)
    return True


def take_pending_share(recipient_qq=None, now=None):
    """预留可见内容；不可见队首留给来源人。失败须用 push_share 原样放回。"""
    d = _data()
    p = d.get("pending_share")
    from_pending = (isinstance(p, dict) and bool(p.get("topic"))
                    and _share_allowed(p, recipient_qq))
    if not from_pending:
        rec = share_candidate(now, recipient_qq=recipient_qq)
        if not rec:
            return None
        p = rec.get("share_payload") or {
            "topic": rec["topic"], "tid": rec["tid"],
            "ts": rec["last_satisfied"], "kind": "know", "detail": "",
            "linked_keys": rec.get("linked_keys") or [], "tries": 0}
    p = {**_share_scope(), **p}
    tid = _tid(p["topic"])
    # 旧队列可能还没有 topic 记录；也必须持久保留 payload，不能取走后重启就丢内容。
    rec = d.setdefault("topics", {}).setdefault(tid, {"topic": p["topic"], "tid": tid})
    if rec.get("share_taken") or rec.get('share_delivery_state') in ('submitted', 'unknown', 'accepted'):
        return None
    p.setdefault('intent_id', uuid.uuid4().hex)
    rec["share_payload"] = p
    rec["share_taken"] = True
    rec["share_owner"] = _RUNTIME_OWNER
    if from_pending:
        d["pending_share"] = None
    if not _save(d):
        return None
    return p


def freeze_share_delivery(topic, text, recipient_qq):
    """Persist rendered intent before any I/O; return an immutable attempt key."""
    d = _data()
    rec = d['topics'][_tid(topic)]
    p = rec['share_payload']
    if rec.get('share_delivery_state') in ('submitted', 'unknown', 'accepted'):
        return None
    p.setdefault('rendered_text', str(text))
    p.setdefault('delivery_recipient', str(recipient_qq))
    if p['rendered_text'] != str(text) or p['delivery_recipient'] != str(recipient_qq):
        raise ValueError('share delivery intent conflict')
    attempt = int(p.get('delivery_attempt') or 0)
    key = 'share:%s:%d' % (p['intent_id'], attempt)
    rec['share_delivery_state'] = 'submitted'
    if not _save(d):
        return None
    return key


def finish_share_delivery(topic, state):
    """Only a definite failed receipt permits retry; unknown stays quarantined."""
    if state not in ('failed', 'unknown', 'accepted'):
        raise ValueError('invalid share delivery state')
    d = _data()
    rec = d['topics'][_tid(topic)]
    rec['share_delivery_state'] = state
    if state == 'failed':
        rec['share_payload']['delivery_attempt'] = int(rec['share_payload'].get('delivery_attempt') or 0) + 1
        rec['share_taken'] = False
        rec.pop('share_owner', None)
        if not d.get('pending_share'):
            d['pending_share'] = dict(rec['share_payload'])
    return _save(d)


def _squash(text):
    import re
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


def act_plan(text, answered=None):
    """把一句好奇拆成几条她会自己敲进搜索框的短词。

    原话排第一个 —— 话题本来就是模型挑出来的具体说法，先照她的话去查。
    备选必须**包含完整实体**：以前这里用 content_words 拆出「卫戍」「协议」
    这种二字词去搜，实体被降格，搜回来的《看门狗：军团》只靠泛词重叠就过了门。
    现在拆词交给 web_search.plan_heuristic（实体 + 领域），这里只做候选整理。
    真正执行仍由桥复用检索链路，不去碰网络也不碰模型。
    """
    t = " ".join(str(text or "").split())
    queries = []
    if t:
        queries.append(t[:40])
    try:
        import web_search as _ws
        p = _ws.plan_heuristic(t) if hasattr(_ws, "plan_heuristic") else {}
    except Exception:
        p = {}
    entity = str((p or {}).get("entity") or "").strip()
    ent_s = _squash(entity)
    for cand in [p.get("query")] + list(p.get("alternates") or []):
        q = " ".join(str(cand or "").split())
        if not q:
            continue
        # 完整实体不许被拆成碎片：比实体短、又被实体包含的候选一律不要
        qs = _squash(q)
        if ent_s and qs != ent_s and qs in ent_s:
            continue
        q = q[:30]
        if q and q not in queries:
            queries.append(q)
    if not queries:
        queries = [t[:30]]
    uniq = []
    for q in queries:
        q = " ".join(str(q).split())
        if q and q not in uniq:
            uniq.append(q)
    return {"topic": t[:64], "queries": uniq[:MAX_QUERIES_PER_ACT],
            "answered": list(answered or [])}


def _seed_event_ids(spec, topic, seed_from):
    """Observe stored motivation at ingestion, never invent its original cause."""
    supplied = []
    for key in ("event_ids", "origin_events"):
        refs = spec.get(key) or []
        if isinstance(refs, str):
            refs = [refs]
        for ref in refs:
            if ref and str(ref) not in supplied:
                supplied.append(str(ref))
    source = {"persona_curious": "persona_state", "wish": "wish_state"}.get(seed_from)
    if not supplied and not source:
        return []
    try:
        import self_model as sm
        refs = supplied
        if not refs:
            wish_id = str(spec.get("wish_id") or "")
            # Exact seed identity, not a normalized topic ID, clock tick or search
            # attempt. The immutable ledger dedupes across cache loss/restarts too.
            identity = json.dumps([source, topic, wish_id], ensure_ascii=False,
                                  separators=(",", ":"))
            dedupe = "interest_seed:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            eid = sm.record_event(
                "interest_seed", source=source, visibility="private",
                payload={"topic": topic, "seed_from": seed_from, "wish_id": wish_id,
                         "epistemic": "internal_motivation"},
                dedupe=dedupe)
            # No ts from apply_seeds(now=...) or the wish: we observed the stored
            # desire NOW, not when it began. This is not external factual evidence.
            if not eid:
                print("[好奇度] 种子依据写入失败（%s），不补造事件 ID" % source, flush=True)
                return []
            refs = [eid]
        valid = []
        for eid in refs:
            if sm.evidence_valid("event", eid):
                valid.append(eid)
            else:
                # Dedupe may return a withdrawn observation. Never mint a new key
                # to revive it, or replace withdrawn supplied causes with a motive.
                print("[好奇度] 种子依据无效或已撤回，不复用 %s" % eid, flush=True)
        return valid
    except Exception as e:
        print("[好奇度] 种子依据记录/校验失败 " + type(e).__name__, flush=True)
        return []


def apply_seeds(specs, now=None):
    """落种并记录当下观察到的内部动机；幂等 tag 保证重复灌不再加分。"""
    n = 0
    for sp in (specs or []):
        t = str((sp or {}).get("topic") or "").strip()
        if not t:
            continue
        src = str(sp.get("seed_from") or "unknown")
        tag = src + ":" + str(sp.get("wish_id") or sp.get("src") or "")
        event_ids = _seed_event_ids(sp, t, src)
        _touch(t[:64], seed_from=src, weight=float(sp.get("weight") or 0.0),
               now=now, tag=tag, event_ids=event_ids)
        n += 1
    return n


def top_candidates(now=None, limit=3):
    """按当前兴趣分排出「她最想弄懂的这几条」，已经排上进行动的不重复占位。"""
    now = float(now or time.time())
    out = []
    for rec in list((_data().get("topics") or {}).values()):
        t = str(rec.get("topic") or "").strip()
        if not t or str(rec.get("state") or "") == STATE_ACT:
            continue
        if not should_act(t, now):
            continue
        out.append({"topic": t, "tid": rec.get("tid"),
                    "score": score_at(t, now),
                    "seed_from": rec.get("seed_from") or "unknown"})
    out.sort(key=lambda x: -x["score"])
    return out[:max(0, int(limit))]


def stats():
    d = _data()
    tps = list((d.get("topics") or {}).values())
    by = {}
    for r in tps:
        by[str(r.get("state") or "?")] = by.get(str(r.get("state") or "?"), 0) + 1
    return {"topics": len(tps), "states": by,
            "max_per_day": max_per_day(), "quota_left": quota_left(),
            "shares_today": shares_today(), "threshold": ACT_THRESHOLD,
            "pending_share": bool(d.get("pending_share")),
            "scope": ACT_SCOPE}
