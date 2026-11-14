"""她自己对世界做过什么：查了什么、刷到什么、记住了什么、主动讲了什么。

为什么要单独一份：她想弄懂什么、最后搞懂了没有，本来散在四五个模块的日志里，
面板上完全看不见 —— 于是「让她自己学习」这句话没法检查，只能信。
这份流水就是把那句话变成能看的东西。

只记后台自主动作，不记她回给群里的每一句（那些已经在聊天记录里）。
所有失败一律吞掉：记账本不该把她的回复拖慢或者弄崩。
"""
import json
import os
import threading
import time

_LOCK = threading.Lock()
KINDS = ("search", "watch", "remember", "share", "curious")
KEEP_DAYS = int(os.getenv("ACTIVITY_KEEP_DAYS", "30"))
MAX_LINE = 400


def _dir():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory")


def _path(day=None):
    return os.path.join(_dir(), "activity-%s.jsonl" % (
        day or time.strftime("%Y-%m-%d")))


def note(kind, what, outcome="", detail="", scene="", ref=""):
    """记一笔。kind 必须在 KINDS 里，不然这条会被丢掉而不是混进统计。"""
    try:
        k = str(kind or "")
        if k not in KINDS:
            return False
        rec = {
            "ts": round(time.time(), 2),
            "kind": k,
            "what": str(what or "")[:120],
            "out": str(outcome or "")[:40],
            "detail": str(detail or "")[:MAX_LINE],
            "scene": str(scene or "")[:12],
            "ref": str(ref or "")[:60],
        }
        with _LOCK:
            os.makedirs(_dir(), exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _day_files(limit_days=3):
    try:
        names = [n for n in os.listdir(_dir())
                 if n.startswith("activity-") and n.endswith(".jsonl")]
    except Exception:
        return []
    names.sort(reverse=True)
    return [os.path.join(_dir(), n) for n in names[:max(1, int(limit_days))]]


def recent(limit=40, kinds=None):
    """最近几笔，新的在前。跨今天/昨天读，够面板用。"""
    want = set(str(k) for k in (kinds or [])) or None
    out = []
    for path in _day_files():
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if want and rec.get("kind") not in want:
                continue
            out.append(rec)
            if len(out) >= limit:
                out.sort(key=lambda x: -float(x.get("ts") or 0))
                return out
    out.sort(key=lambda x: -float(x.get("ts") or 0))
    return out


def stats():
    """按类型点一下总数，面板用来显示「这周她自己查了 N 次」。"""
    recs = recent(limit=2000)
    by = {}
    for r in recs:
        k = str(r.get("kind") or "?")
        by[k] = by.get(k, 0) + 1
    return {"total": len(recs), "by_kind": by,
            "keep_days": KEEP_DAYS, "files": len(_day_files(limit_days=999))}


def prune(now=None):
    """删掉超过 KEEP_DAYS 的流水文件。这是运行日志，不是她的记忆。"""
    cutoff = float(now or time.time()) - KEEP_DAYS * 86400
    removed = 0
    try:
        names = os.listdir(_dir())
    except Exception:
        return 0
    for n in names:
        if not (n.startswith("activity-") and n.endswith(".jsonl")):
            continue
        path = os.path.join(_dir(), n)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            continue
    return removed
