"""老化 = 归档，不是删除；归档仍可被显式查到，被查到就复活。

为什么不用写入时间判旧：档案里「40 天前记下、但最近天天被用到」的记忆很多，
按 ts 归档会先把这些最该留的清掉。判据统一用 memory_usage 的 last_used ——
那才是「她到底还在不在用它」。

三条底线：
  1. 不 DELETE：条目只加 status/archived_at，原文一直在库里；
  2. 默认注入不带 archived（预算留给真正在用的东西）；
  3. 显式检索（include_archived=True）能查到，查到就复活并记一次 revived。
"""
import asyncio
import json
import os
import time

ARCHIVED = "archived"
ACTIVE = "active"


def _run(coro):
    """在没有事件循环的地方（面板、脚本、worker 外层）把协程跑完。"""
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        loop = None
    if loop is not None and loop.is_running():
        # 已经在循环里：调用方自己 await，这里不该阻塞
        raise RuntimeError("在事件循环里请直接 await")
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _usage(store_key):
    try:
        import memory_usage as _mu
        return _mu.usage_of(store_key) or {}
    except Exception:
        return {}


def archive_stale(store="group_facts", group_id="", unused_days=30, min_age_days=30,
                  now=None, limit=200):
    """把「很久没被用上」的条目标成归档。"""
    now = float(now or time.time())
    cutoff = now - max(1.0, float(unused_days)) * 86400.0
    born_cut = now - max(1.0, float(min_age_days)) * 86400.0
    archived = scanned = 0
    try:
        if store != "group_facts":
            return {"ok": False, "reason": "这一版只管群事实：%s" % store,
                    "archived": 0, "scanned": 0}
        import group_memory_store as gms
        import memory_usage as _mu
        data = gms._load(force=True)
        g = (data.get("groups") or {}).get(str(group_id)) or {}
        ents = g.get("entries") or []
        for e in ents:
            scanned += 1
            if str(e.get("status") or ACTIVE) == ARCHIVED:
                continue
            ts = float(e.get("ts") or 0)
            if ts > born_cut:
                continue                     # 刚写下的不动
            u = _usage(_mu.key_group(e.get("id"), e.get("topic"), e.get("text")))
            last = float(u.get("last_used") or 0)
            if last and last > cutoff:
                continue                     # 最近用过 —— 这就是不删的理由
            e["status"] = ARCHIVED
            e["archived_at"] = now
            e["archive_reason"] = ("never_used" if not last
                                   else "stale_%dd" % int((now - (last or ts)) / 86400))
            archived += 1
            if archived >= limit:
                break
        if archived:
            g["updated"] = now
            gms._save(data)
            gms._INDEX["sig"] = None         # 让索引按新状态重建
        return {"ok": True, "archived": archived, "scanned": scanned,
                "group_id": str(group_id)}
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__ + ": " + str(e)[:80],
                "archived": archived, "scanned": scanned}


def revive(store="group_facts", group_id="", key=""):
    """归档条目被显式查到 → 复活，但保留归档痕迹与次数。"""
    if not key:
        return False
    try:
        if store == "group_facts":
            import group_memory_store as gms
            data = gms._load(force=True)
            g = (data.get("groups") or {}).get(str(group_id)) or {}
            eid = str(key).split(":", 1)[-1]
            for e in (g.get("entries") or []):
                if str(e.get("id") or "") != eid:
                    continue
                if str(e.get("status") or ACTIVE) != ARCHIVED:
                    return False
                e["status"] = ACTIVE
                e["revived"] = int(e.get("revived") or 0) + 1
                e["revived_at"] = time.time()
                gms._save(data)
                gms._INDEX["sig"] = None
                print("[归档] 复活群事实 %s（第 %d 次）" % (eid, e["revived"]), flush=True)
                return True
        elif store == "world_knowledge":
            import knowledge_base as kb
            k = str(key).split(":", 1)[-1]
            items = kb.raw_items()
            for it in items:
                if str(it.get("key") or "").strip() == k:
                    if str(it.get("status") or "active") != "archive":
                        return False
                    return kb.update_meta(k, status="active",
                                          extra={"revived": int(it.get("revived") or 0) + 1})
    except Exception as e:
        print("[归档] 复活失败 " + type(e).__name__, flush=True)
    return False


async def _backfill_one(item, llm):
    import knowledge_base as kb
    try:
        raw = await llm("这条你记下来的东西该算哪一类？只回答一个词："
                        "作品设定 / 人物 / 工具做法 / 时事 / 其他\n\n"
                        "关键词：%s\n标题：%s\n内容：%s"
                        % (str(item.get("key") or "")[:24],
                           str(item.get("title") or "")[:40],
                           str(item.get("summary") or "")[:200]))
    except Exception:
        return None, "llm_failed"
    text = str(raw or "").strip()
    for cat in ("作品设定", "人物", "工具做法", "时事", "其他"):
        if cat in text:
            return cat, ""
    return None, "unrecognized"


def backfill_categories(items, llm=None):
    """给没有类别的老条目补上类别。

    现网 world_knowledge 里 16 条旧记录既没 category 也没 status，
    读的时候全靠默认值兜成「其他」，于是配额一涨它们就成批被降级 ——
    老记忆被无声挤掉，而且再也看不出是被谁挤的。
    只填「原始文件里确实缺 category」的那些，填过的跳过，所以幂等。
    """
    total = len(items or [])
    done = skipped = failed = 0
    try:
        import knowledge_base as kb
        raw = {str(x.get("key") or "").strip(): x for x in kb.raw_items()}
    except Exception as e:
        return {"total": total, "done": 0, "skipped": 0, "failed": total,
                "reason": type(e).__name__}
    for it in (items or []):
        key = str(it.get("key") or "").strip()
        origin = raw.get(key) or {}
        if str(origin.get("category") or "").strip():
            skipped += 1
            continue
        if llm is None:
            skipped += 1
            continue
        try:
            cat, why = _run(_backfill_one(it, llm))
        except Exception:
            cat, why = None, "runner_failed"
        if not cat:
            failed += 1
            continue
        if kb.update_meta(key, category=cat):
            done += 1
        else:
            failed += 1
    return {"total": total, "done": done, "skipped": skipped, "failed": failed}


def stats():
    """归档规模，给面板/仪表看。"""
    out = {"group_facts": {}, "world_knowledge": {}}
    try:
        import group_memory_store as gms
        data = gms._load()
        for gid, g in (data.get("groups") or {}).items():
            ents = g.get("entries") or []
            out["group_facts"][gid] = {
                "total": len(ents),
                "archived": len([e for e in ents
                                 if str(e.get("status") or "active") == ARCHIVED]),
                "revived": sum(int(e.get("revived") or 0) for e in ents),
            }
    except Exception:
        pass
    try:
        import knowledge_base as kb
        raw = kb.raw_items()
        out["world_knowledge"] = {
            "total": len(raw),
            "archive": len([x for x in raw if str(x.get("status") or "") == "archive"]),
            "no_category": len([x for x in raw
                                if not str(x.get("category") or "").strip()]),
        }
    except Exception:
        pass
    return out
