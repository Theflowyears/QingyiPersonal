"""她主动去私聊朋友：找谁、什么时候、为什么。

口径（用户定的）：
  · 只找「好感度 > 15」且确实是好友的人（好友列表为准，不是记忆里的名字）；
  · 每天 2~4 次，UTC+8 的 09:00–23:00 之间；
  · 具体时机尽量由**群聊内容**决定（谁刚在群里说话，就顺势去找谁）；
  · 必须有算法兜底：群聊没线索时按「好感高 + 最久没聊」挑人。

这份文件只做**决策**，不发消息、不调模型 —— 这样它能被测试，
而且「她今天该不该找谁」这件事有唯一答案，不会两处各判一次。
"""
import hashlib
import json
import os
import random
import time
import uuid

WINDOW_START_HOUR = int(os.getenv("PROACTIVE_DM_START_HOUR", "9"))
WINDOW_END_HOUR = int(os.getenv("PROACTIVE_DM_END_HOUR", "23"))
MIN_AFFECTION = float(os.getenv("PROACTIVE_DM_MIN_AFFECTION", "15"))
MIN_GAP_MINUTES = int(os.getenv("PROACTIVE_DM_MIN_GAP_MINUTES", "90"))
FRIEND_COOLDOWN_DAYS = float(os.getenv("PROACTIVE_DM_FRIEND_COOLDOWN_DAYS", "3"))
GROUP_SPEAK_WINDOW = int(os.getenv("PROACTIVE_DM_GROUP_WINDOW_SECONDS", "1800"))
MAX_PER_DAY = int(os.getenv("PROACTIVE_DM_MAX_PER_DAY", "4"))
MIN_PER_DAY = int(os.getenv("PROACTIVE_DM_MIN_PER_DAY", "2"))
ENABLED = os.getenv("PROACTIVE_DM_ENABLED", "1").strip() not in ("0", "off", "false")

# 清缓存不换 owner；只有新运行会话才能回收旧进程没有完成的预留。
_RUNTIME_OWNER = uuid.uuid4().hex
_cache = {"sig": None, "data": None}


def _file():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "persona",
                        "proactive_dm.json")


def _empty():
    return {"days": {}, "last": {}, "pending": None}


def _now(now=None):
    return float(time.time() if now is None else now)


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + 8 * 3600))


def _load():
    try:
        st = os.stat(_file())
        sig = (round(st.st_mtime, 3), st.st_size)
    except Exception:
        sig = None
    if sig is not None and _cache["sig"] == sig and _cache["data"] is not None:
        return _cache["data"]
    data = _empty()
    if sig is not None:
        try:
            with open(_file(), encoding="utf-8") as f:
                got = json.load(f) or {}
            if isinstance(got, dict):
                data["days"] = got.get("days") or {}
                data["last"] = got.get("last") or {}
                data["pending"] = got.get("pending")
        except Exception:
            pass
    _cache.update({"sig": sig, "data": data})
    pending = data.get("pending")
    if isinstance(pending, dict) and pending.get("owner") != _RUNTIME_OWNER:
        # 没有确认结果不等于成功；只取消预留，不扣额度/机会，也不回滚真实联系记录。
        data["pending"] = None
        _save(data)
        print("[主动私聊] 恢复旧进程预留：结果 unknown，释放机会，不计已发送", flush=True)
    return data


def _save(data=None):
    d = data if isinstance(data, dict) else _cache.get("data")
    if not isinstance(d, dict):
        return False
    try:
        os.makedirs(os.path.dirname(_file()), exist_ok=True)
        tmp = _file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _file())
        _cache.update({"sig": None, "data": d})
        return True
    except Exception as e:
        print("[主动私聊] 写盘失败 " + str(e)[:60], flush=True)
        return False


def plan_day(day):
    """今天的 2~4 个时间点（UTC+8 窗口内）。

    按日期做种子：同一天无论重启几次，计划都是同一套。
    每次重启重新抽签的话，她会在重启窗口里反复「刚好该发了」。
    """
    seed = int(hashlib.sha1(str(day).encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    n = rng.randint(max(1, MIN_PER_DAY), max(MIN_PER_DAY, MAX_PER_DAY))
    span = (WINDOW_END_HOUR - WINDOW_START_HOUR) * 3600
    offs = sorted(rng.uniform(0, span) for _ in range(n))
    return [WINDOW_START_HOUR * 3600 + int(o) for o in offs]


def _day_state(day):
    d = _load()
    st = (d.get("days") or {}).get(day)
    if not isinstance(st, dict):
        st = {"slots": plan_day(day), "used": [], "declined": [], "n": 0}
        d.setdefault("days", {})[day] = st
        _save(d)
    return st


def _in_window(now):
    t = time.gmtime(now + 8 * 3600)
    return WINDOW_START_HOUR <= t.tm_hour < WINDOW_END_HOUR


def _friend_block_reason(now, st, scores, friends):
    if not ENABLED:
        return "开关关着"
    if not _in_window(now):
        return "不在 9–23 点"
    if st.get("n", 0) >= MAX_PER_DAY:
        return "今天已经聊够了"
    if isinstance(_load().get("pending"), dict):
        return "上一条私聊还在处理中"
    # 到点了吗：拒绝也是用过一次机会，但不扣已发送预算。
    slots = list(st.get("slots") or [])
    used = set(st.get("used") or []) | set(st.get("declined") or [])
    pending_slots = [i for i in range(len(slots)) if i not in used]
    if not pending_slots:
        return "今天排的时间点都用完了"
    now_s = _local_seconds(now)
    due = [i for i in pending_slots if slots[i] <= now_s]
    if not due:
        return "还没到下一个时间点"
    gap = MIN_GAP_MINUTES * 60
    last_any = max([float(v or 0) for v in (_load().get("last") or {}).values()] or [0])
    if last_any and (now - last_any) < gap:
        return "离上次私聊不到 %d 分钟" % MIN_GAP_MINUTES
    if not friends:
        return "拿不到好友列表"
    cands = []
    for uid in friends:
        uid = str(uid)
        if float(scores.get(uid, -999)) < MIN_AFFECTION:
            continue
        last = float((_load().get("last") or {}).get(uid) or 0)
        if last and (now - last) < FRIEND_COOLDOWN_DAYS * 86400:
            continue
        cands.append(uid)
    if not cands:
        return "没有够格又好几天没聊的朋友"
    return ""


def _local_seconds(now):
    t = time.gmtime(now + 8 * 3600)
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def _observe_decision(got, now, friends):
    """G01：把 decide 的结论经统一契约登记成私聊提案（旁路观测）。

    门禁结论仍由 _decide 给出；这里只把「找谁/为什么/没找成卡在哪」
    写成字段显式的提案。好友表是结构化 QQ 绑定，随提案一起交给契约，
    收件人—话题主体一致性由契约按 §7.1 校验（名字检查只是兜底）。
    """
    import action_proposal as ap
    ok = bool(got.get("ok"))
    uid = str(got.get("uid") or "")
    window_left = max(0, WINDOW_END_HOUR * 3600 - _local_seconds(now))
    ap.observe(ap.build(
        "proactive_dm", "private_dm", motive="联结",
        reason=str(got.get("reason") or "主动私聊决策"),
        event_ids=[], goal_id="", topic="", topic_qq=uid if ok else "",
        participants=[uid] if ok else [], recipient_qq=uid,
        visibility="private", scope={"origin": "friend_dm"}, evidence=[],
        deadline=now + max(3600, window_left),
        budget={"quota_key": "PROACTIVE_DM_MAX_PER_DAY",
                "max_per_day": MAX_PER_DAY,
                "cooldown_seconds": MIN_GAP_MINUTES * 60,
                "friend_cooldown_seconds": FRIEND_COOLDOWN_DAYS * 86400},
        success="私聊真的发出去（done 提交回执）",
        failure="生成/发送失败，release 退回机会",
        abandon_when="她自愿不说（decline），或冷却期内不再找同一个人",
        decision="act" if ok else "wait",
        gate_reasons=[] if ok else ["opportunity_gate_closed"],
        motivation_score=float(got.get("score") or 0) if ok else None),
        friends=[str(u) for u in (friends or [])], now=now)


def decide(now, friends, scores, recent_speakers=None, exclude=None):
    """现在该不该找谁。返回 dict 或 None；None 时 reason 说明卡在哪。

    exclude：这条链路不找的人。默认由调用方把主人排除掉 ——
    主人已经有「好奇度主动分享」那条线，这里再发一次就成了重复打扰，
    而且线上真出现过「把别人的抽卡八卦发给主人」这种错位。

    G01：结论同时经统一契约登记成行动提案；返回形状与门禁行为不变。
    """
    got = _decide(now, friends, scores, recent_speakers, exclude)
    _observe_decision(got, _now(now), friends)
    return got


def _decide(now, friends, scores, recent_speakers=None, exclude=None):
    now = _now(now)
    day = _day(now)
    st = _day_state(day)
    skip = {str(u) for u in (exclude or [])}
    friends = [u for u in (friends or []) if str(u) not in skip]
    why = _friend_block_reason(now, st, scores, friends)
    if why:
        return {"ok": False, "reason": why}

    cands = [str(u) for u in friends
             if float((scores or {}).get(str(u), -999)) >= MIN_AFFECTION]
    last_map = _load().get("last") or {}
    cands = [u for u in cands
             if not (float(last_map.get(u) or 0)
                     and now - float(last_map.get(u) or 0)
                     < FRIEND_COOLDOWN_DAYS * 86400)]

    # ① 群聊驱动：刚在群里说话的人，最有话头
    spoken = [str(u) for u in (recent_speakers or {}) if str(u) in cands]
    if spoken:
        spoken.sort(key=lambda u: -float((recent_speakers or {}).get(u) or 0))
        return {"ok": True, "uid": spoken[0], "reason": "刚在群里说话",
                "score": float((scores or {}).get(spoken[0], 0))}

    # ② 算法兜底：好感高 + 最久没联系
    def rank(u):
        last = float(last_map.get(u) or 0)
        return (-float((scores or {}).get(u, 0)), last or 0)
    cands.sort(key=rank)
    if not cands:
        return {"ok": False, "reason": "没有够格的朋友"}
    return {"ok": True, "uid": cands[0], "reason": "好久没聊了",
            "score": float((scores or {}).get(cands[0], 0))}


def mark_contacted(uid, ts=None):
    """记下「刚跟这个人聊过」。

    主动找完要记，她自己本来就聊过的也该记 —— 否则「几天内不重复找」形同虚设。
    走这个函数而不是让调用方直接改 _load() 的返回值：那个返回值可能是
    重新读出来的副本，改了不落盘，下次判断就白算了。
    """
    d = _load()
    last = d.setdefault("last", {})
    last[str(uid)] = max(float(last.get(str(uid)) or 0), _now(ts))
    return _save(d)


def addressee_conflict(text, target_aliases, name_map):
    """这句话里有没有喊到**别的人**。

    线上真实事故：消息发给了主人，正文却是「阿甲，八抽出货这运气是开挂了」——
    收件人 A、开口喊 B。这不是措辞问题，是把该跟 B 说的话发给了 A。

    做法：先把收件人自己的名字从文本里抹掉，再看还剩不剩别人的名字。
    名字只认「群名片/备注」这类具体到人的两字以上称呼，
    避免把「鱼」「月」这种单字当成名字误杀。
    返回冲突的名字；没有冲突返回空串。
    """
    t = str(text or "")
    if not t:
        return ""
    mine = {str(x).strip() for x in (target_aliases or []) if str(x).strip()}
    for a in sorted(mine, key=len, reverse=True):
        if a:
            t = t.replace(a, " ")
    for name in (name_map or {}):
        n = str(name or "").strip()
        if len(n) < 2 or n in mine:
            continue
        if n in t:
            return n
    return ""


def pick_reason(decision):
    """给日志/提示词用的一句人话。"""
    if not decision:
        return ""
    return str(decision.get("reason") or "")


def claim(decision, now=None):
    """预留一次到点的机会；done 才扣已发送额度，失败 release，自愿不说 decline。

    G01：预留结论同时经统一契约登记成提案；返回形状与门禁行为不变。
    预留阶段只登记收件人，话题主体绑定在 decide（带好友表）与发送门
    完成，契约如实登记 friend_binding_unverified，不假装已核验。
    """
    got = _claim(decision, now)
    import action_proposal as ap
    at = _now(now)
    uid = str((decision or {}).get("uid") or "")
    window_left = max(0, WINDOW_END_HOUR * 3600 - _local_seconds(at))
    ap.observe(ap.build(
        "proactive_dm", "private_dm", motive="联结",
        reason="预留一次到点的私聊机会：%s" % str((decision or {}).get("reason") or ""),
        event_ids=[], goal_id="", topic="", topic_qq="",
        participants=[uid] if uid else [], recipient_qq=uid,
        visibility="private", scope={"origin": "friend_dm", "stage": "reserve"},
        evidence=[], deadline=at + max(3600, window_left),
        budget={"quota_key": "PROACTIVE_DM_MAX_PER_DAY",
                "max_per_day": MAX_PER_DAY,
                "cooldown_seconds": MIN_GAP_MINUTES * 60,
                "friend_cooldown_seconds": FRIEND_COOLDOWN_DAYS * 86400},
        success="私聊真的发出去（done 提交回执）",
        failure="生成/发送失败，release 退回机会",
        abandon_when="她自愿不说（decline），或冷却期内不再找同一个人",
        decision="act" if got else "wait",
        gate_reasons=[] if got else ["reservation_gate_closed"]), now=at)
    return got


def _claim(decision, now=None):
    now = _now(now)
    if not decision or not decision.get("ok") or not decision.get("uid"):
        return None
    if not ENABLED or not _in_window(now):
        return None
    day = _day(now)
    _day_state(day)
    d = _load()
    if isinstance(d.get("pending"), dict):
        return None
    st = d["days"][day]
    if int(st.get("n") or 0) >= MAX_PER_DAY:
        return None
    used = set(st.get("used") or []) | set(st.get("declined") or [])
    slots = list(st.get("slots") or [])
    idx = next((i for i in range(len(slots))
                if i not in used and slots[i] <= _local_seconds(now)), None)
    if idx is None:
        return None
    d["pending"] = {"uid": str(decision["uid"]), "reason": decision.get("reason"),
                    "ts": now, "day": day, "slot": idx, "owner": _RUNTIME_OWNER}
    return d["pending"] if _save(d) else None


def release(now=None):
    """模型/发送失败只取消预留，不能删除期间收到的真实联系记录。"""
    d = _load()
    if not isinstance(d.get("pending"), dict):
        return False
    d["pending"] = None
    return _save(d)


def decline(now=None):
    """她自愿不说：消耗这次机会，既不算发送，也不算联系过这个人。"""
    d = _load()
    p = d.get("pending")
    if not isinstance(p, dict) or "slot" not in p:
        return False
    st = (d.get("days") or {}).get(p["day"])
    if not isinstance(st, dict):
        return False
    st["declined"] = sorted(set(st.get("declined") or []) | {p["slot"]})
    d["pending"] = None
    return _save(d)


def done(now=None):
    """确认发成后提交一次发送；重复回调不重复计数。"""
    d = _load()
    p = d.get("pending")
    if not isinstance(p, dict):
        return False
    if "slot" in p:
        st = (d.get("days") or {}).get(p["day"])
        if not isinstance(st, dict):
            return False
        st["used"] = sorted(set(st.get("used") or []) | {p["slot"]})
        st["n"] = int(st.get("n") or 0) + 1
        uid = str(p["uid"])
        last = d.setdefault("last", {})
        last[uid] = max(float(last.get(uid) or 0), _now(now))
    # 旧版 pending 已在 claim 时记过预算，不再扣第二次。
    d["pending"] = None
    return _save(d)


def stats(now=None):
    now = _now(now)
    day = _day(now)
    st = _day_state(day)
    return {"enabled": ENABLED, "day": day, "n": st.get("n", 0),
            "slots": ["%02d:%02d" % (s // 3600, s % 3600 // 60)
                      for s in (st.get("slots") or [])],
            "used": len(st.get("used") or []),
            "declined": len(st.get("declined") or []),
            "min_affection": MIN_AFFECTION,
            "window": "%02d:00–%02d:00" % (WINDOW_START_HOUR, WINDOW_END_HOUR),
            "last": _load().get("last") or {}}
