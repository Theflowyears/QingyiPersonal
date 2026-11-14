# -*- coding: utf-8 -*-
"""人工记忆标注：把「这条记忆到底从哪来」交还给主人，标完立刻生效。

两个门的真实口径（2026-11-12 实测）：
- memory_provenance：persona 1179 条只有 3 条带来源、group 1048 条 0 条。
  要 pass 必须**全部**绑定逐命题来源。标几条代表样本只能建立规则，
  剩下的靠 auto_link（只认逐字可定位的来源，绝不补造）。
- self_view_grounding：窗口内 0 条自我修订 → 只能等真实发生，标注造不出来。

所以这里只做三件能证明的事：
1. 挑代表性样本给主人标（近期、跨 kind/topic），附候选真实事件。
2. 标完立刻写进真实账本（persona_store / group_memory_store），并重跑门判定。
3. auto_link 批量把"逐字可定位"的来源绑上，绑不上的如实留着。

绝不把 legacy 背景补造成亲历；来源必须是真实事件号且文本逐字可定位。
"""
import json
import os
import re
import time

_CJK = re.compile(r"[一-鿿]{4,}")


def _dir():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "ops")


def _ledger_path():
    return os.path.join(_dir(), "annotations.json")


def _load_ledger():
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save_ledger(data):
    import safe_store
    return safe_store.atomic_write_json(_ledger_path(), data)


def _keywords(text, n=6):
    """取短语窗口而不是整句：记忆多为摘要，整句 LIKE 匹配不到原始事件。"""
    runs = sorted(_CJK.findall(str(text or "")), key=len, reverse=True)
    kws = []
    for run in runs[:2]:
        if len(run) <= 6:
            kws.append(run)
        else:
            for start in (0, max(0, len(run) // 2 - 2), max(0, len(run) - 5)):
                kws.append(run[start:start + 5])
    seen, out = set(), []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:n]


def _bigrams(text):
    out = set()
    for run in _CJK.findall(str(text or "")):
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


def _actor_label(session, actor, person_map=None, name_memo=None):
    """把发送人翻成可读称呼；翻不出就如实给 QQ，不猜。

    person_map / name_memo 由调用方一次性预加载，避免每条事件都开库查询
    （那是面板卡顿的根源：8 样本×800 事件×逐条查询）。
    """
    qq = str(actor or "")
    if not qq:
        return ""
    if qq.startswith("per_") and person_map:
        qq = str(person_map.get(qq) or qq)
    sess = str(session or "")
    if sess.startswith("group-"):
        key = (sess, qq)
        if name_memo is not None and key in name_memo:
            return name_memo[key]
        name = qq
        try:
            import member_memory
            got = member_memory.label(sess[6:], qq, None)
            if got and str(got) != qq:
                name = str(got)
        except Exception:
            pass
        if name_memo is not None:
            name_memo[key] = name
        return name
    return qq


_CAND_CACHE = {}
_CAND_TTL = 60


def candidate_events(text, ts=None, limit=6):
    """候选真实事件：只认真正文本重合，带发送人，图片给可看链接。

    性能：整段只开一次库；person 映射一次载入；称呼按 (群,QQ) 记忆化；
    结果按 (文本,ts) 缓存 60 秒，重复刷新不再重算。
    """
    import sqlite3
    path = os.path.join(os.getenv("DATA_DIR", "./data"), "persona", "cognition.db")
    key = (str(text), ts, path)
    now = time.time()
    hit = _CAND_CACHE.get(key)
    if hit and now - hit[0] < _CAND_TTL:
        return hit[1]
    if not os.path.isfile(path):
        return []
    grams = _bigrams(text)
    scored, images = {}, []
    try:
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        try:
            person_map = {r[0]: r[1] for r in c.execute("SELECT id, qq FROM person")}
        except sqlite3.Error:
            person_map = {}
        name_memo = {}
        # 内容优先、跨全部时间找来源；时间只作同分时的佐证。
        # 以前用硬时间窗，老记忆配不到它当时的聊天，只剩近期噪声。
        rows = c.execute("SELECT id, ts, payload, actor_qq, session FROM event "
                         "ORDER BY ts DESC LIMIT 4000")
        for eid, ets, payload, actor, session in rows:
            try:
                p = json.loads(payload or "{}")
            except ValueError:
                p = {}
            etext = str(p.get("text") or "")
            raw_imgs = p.get("images") or []
            if not isinstance(raw_imgs, (list, tuple)):
                raw_imgs = []
            imgs = [str(u) for u in raw_imgs if str(u or "").strip()]
            if etext.strip() and grams:
                score = sum(1 for g in grams if g in etext)
                if score > 0:
                    scored[eid] = {"id": eid, "ts": ets, "text": etext[:120],
                                   "score": score, "images": imgs[:2],
                                   "actor": _actor_label(session, actor, person_map, name_memo)}
            elif imgs and ts and (float(ts) - 6 * 3600) <= ets <= (float(ts) + 3600):
                if len(images) < 3:
                    images.append({"id": eid, "ts": ets, "text": "", "score": 0,
                                   "images": imgs[:2], "image_only": True,
                                   "actor": _actor_label(session, actor, person_map, name_memo)})
        c.close()
    except sqlite3.Error:
        return []

    def rank(r):
        dist = abs((r["ts"] or 0) - float(ts)) if ts else 0
        return (-r["score"], dist)

    out = sorted(scored.values(), key=rank)[:limit]
    result = out + images
    _CAND_CACHE[key] = (now, result)
    if len(_CAND_CACHE) > 200:
        _CAND_CACHE.clear()
    return result


def _reviewed_keys():
    """已人工处理（确认保留/已绑定）的条目，不再重复出现在待标注里。"""
    ledger = _load_ledger()
    keys = set()
    for r in ledger.get("reviewed") or []:
        keys.add((r.get("store"), str(r.get("id"))))
    for r in ledger.get("entries") or []:
        keys.add((r.get("store"), str(r.get("id"))))
    return keys


def samples(limit=8):
    """代表性样本：近期未绑来源、且还没人工处理过的，跨 kind/topic。"""
    import persona_store
    import group_memory_store
    reviewed = _reviewed_keys()
    out = []
    try:
        for row in persona_store.unsourced_items(limit=limit * 4):
            if ("persona", str(row["id"])) in reviewed:
                continue
            out.append({"store": "persona", "id": row["id"], "kind": row.get("kind", ""),
                        "text": row["text"], "ts": row.get("ts"),
                        "candidates": candidate_events(row["text"], ts=row.get("ts"))})
    except Exception:
        pass
    try:
        for row in group_memory_store.unsourced_entries(limit=limit * 4):
            if ("group", str(row["id"])) in reviewed:
                continue
            out.append({"store": "group", "id": row["id"], "group_id": row["group_id"],
                        "kind": row.get("topic", ""), "text": row["text"], "ts": row.get("ts"),
                        "candidates": candidate_events(row["text"], ts=row.get("ts"))})
    except Exception:
        pass
    # 好标的（候选重合度高）排前面，其次才是近期；避免把配不到来源的老记忆塞给用户。
    out.sort(key=lambda r: (-max([c.get("score", 0) for c in r["candidates"]] or [0]),
                            -(r.get("ts") or 0)))
    return out[:limit]


def confirm(store, item_id, note="", group_id=None, by="panel"):
    """人工确认这条记忆无误、保留，但**不补造来源**。

    主人知道是对的、只是找不到对应事件的记忆，可以跳过绑定直接保存。
    它会被记为已人工复核，不再出现在待标注里；记忆本身原样保留，
    来源字段不动（不伪造），provenance 门仍按真实绑定数计算。
    """
    ledger = _load_ledger()
    ledger.setdefault("reviewed", []).append(
        {"store": store, "id": str(item_id), "note": str(note)[:200], "by": by,
         "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    ledger["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ok = _save_ledger(ledger)
    return {"ok": bool(ok), "message": "已确认保留（未补造来源）" if ok else "保存失败"}


def submit(store, item_id, event_ids, note="", group_id=None, by="panel"):
    """把来源写进真实账本，立刻生效。返回 {ok, message}。"""
    ids = [str(x) for x in (event_ids or []) if str(x or "").strip()]
    if not ids:
        return {"ok": False, "message": "没有选任何来源事件，未写入"}
    try:
        if store == "persona":
            import persona_store
            ok = persona_store.set_sources(item_id, ids)
        elif store == "group":
            import group_memory_store
            ok = group_memory_store.set_source(group_id, item_id, ids)
        else:
            return {"ok": False, "message": "未知账本"}
    except Exception as e:
        return {"ok": False, "message": type(e).__name__}
    if not ok:
        return {"ok": False, "message": "找不到这条记忆，未写入"}
    ledger = _load_ledger()
    entries = ledger.setdefault("entries", [])
    entries.append({"store": store, "id": item_id, "event_ids": ids,
                    "note": str(note)[:200], "by": by,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    ledger["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save_ledger(ledger)
    return {"ok": True, "message": "已绑定 %d 个来源" % len(ids)}


def delete(store, item_id, group_id=None, by="panel"):
    """人工删除一条无法绑定来源的记忆（legacy 清理）。只删用户点的那条。

    删除是破坏性操作，前端有二次确认；这里再记一条审计，绝不批量删。
    """
    try:
        if store == "persona":
            import persona_store
            ok = persona_store.delete_by_id(item_id)
        elif store == "group":
            import group_memory_store
            ok = group_memory_store.delete_entry(group_id, item_id)
        else:
            return {"ok": False, "message": "未知账本"}
    except Exception as e:
        return {"ok": False, "message": type(e).__name__}
    if not ok:
        return {"ok": False, "message": "找不到这条记忆，未删除"}
    ledger = _load_ledger()
    ledger.setdefault("deletions", []).append(
        {"store": store, "id": item_id, "by": by,
         "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    ledger["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save_ledger(ledger)
    return {"ok": True, "message": "已删除这条记忆"}


def auto_link(limit=200):
    """批量绑定来源，三档置信度：

    - 逐字可定位（>=5 字片段原样出现）→ 直接绑；
    - 高二字词重合（>=6）→ 直接绑；
    - 人工标够 5 条后开启校准，中等重合（>=4）也绑。
    纯图片候选不参与自动绑定（机器看不出图意），只能人工确认。
    """
    manual = len(_load_ledger().get("entries") or [])
    calibrate = manual >= 5
    learned = learned_threshold() if calibrate else None
    cal_min = learned if learned is not None else 4
    linked = 0
    for sample in samples(limit=limit):
        text = sample["text"]
        best = None
        for cand in sample["candidates"]:
            if cand.get("image_only"):
                continue
            if any(len(run) >= 5 and run in cand["text"] for run in _CJK.findall(text)):
                best = cand
                break
            if cand.get("score", 0) >= 6:
                best = cand
                break
            if calibrate and cand.get("score", 0) >= cal_min:
                best = cand
                break
        if best:
            res = submit(sample["store"], sample["id"], [best["id"]],
                         note="auto_link " + ("学习校准" if learned is not None else "校准"),
                         group_id=sample.get("group_id"), by="auto")
            if res["ok"]:
                linked += 1
    return {"linked": linked, "calibrated": calibrate, "manual": manual,
            "learned_threshold": learned}


def _memory_text(store, item_id, group_id=None):
    try:
        if store == "persona":
            import persona_store
            row = persona_store.by_id(item_id)
            return (row or {}).get("text", ""), (row or {}).get("ts")
        import group_memory_store
        for e in group_memory_store.get_entries(group_id):
            if e.get("id") == item_id:
                return e.get("text", ""), e.get("ts")
    except Exception:
        pass
    return "", None


def _event_row(eid):
    import sqlite3
    path = os.path.join(os.getenv("DATA_DIR", "./data"), "persona", "cognition.db")
    try:
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        row = c.execute("SELECT ts, payload FROM event WHERE id=?", (eid,)).fetchone()
        c.close()
        if not row:
            return None
        p = json.loads(row[1] or "{}")
        return {"ts": row[0], "text": str(p.get("text") or "")}
    except Exception:
        return None


def learn():
    """从主人的人工标注里学特征：重合度分布、时间差。

    只统计真实人工绑定（entries），不拿自动绑定的当老师。
    返回 {n, median_score, p25_score, median_dt_hours}，供主动标注定阈值。
    """
    scores, dts = [], []
    for r in _load_ledger().get("entries") or []:
        mtext, mts = _memory_text(r.get("store"), r.get("id"), r.get("group_id"))
        if not mtext:
            continue
        grams = _bigrams(mtext)
        for eid in r.get("event_ids") or []:
            ev = _event_row(eid)
            if not ev or not ev["text"]:
                continue
            scores.append(sum(1 for g in grams if g in ev["text"]))
            if mts and ev["ts"]:
                dts.append(abs(mts - ev["ts"]) / 3600.0)
    if not scores:
        return {"n": 0}
    scores.sort()
    dts.sort()
    return {"n": len(scores),
            "median_score": scores[len(scores) // 2],
            "p25_score": scores[len(scores) // 4],
            "median_dt_hours": round(dts[len(dts) // 2], 1) if dts else None}


def learned_threshold():
    """主动标注的重合度下限：不低于人工标注的 25 分位，且至少 4。"""
    st = learn()
    if st.get("n", 0) < 5:
        return None
    return max(4, int(st.get("p25_score", 4)))


def status():
    """重跑两个门的判定，给面板实时进度。"""
    import qingyi_preflight
    root = os.path.join(os.getenv("DATA_DIR", "./data"), "persona")
    prov = qingyi_preflight._memory_provenance_check(__import__("pathlib").Path(root))
    counts = prov.get("counts") or {}
    total = counts.get("persona_total", 0) + counts.get("group_total", 0)
    sourced = counts.get("persona_sourced", 0) + counts.get("group_sourced", 0)
    ledger = _load_ledger()
    return {
        "memory_provenance": {"status": prov.get("status"), "total": total,
                              "sourced": sourced, "remaining": max(0, total - sourced)},
        "annotated": len(ledger.get("entries") or []),
        "reviewed": len(ledger.get("reviewed") or []),
        "learn": learn(),
        "updated": ledger.get("updated", ""),
    }