"""会话流水：每一轮自带时间，append-only。

为什么要单独一份日志而不是只改 histories.json：
    histories.json 是「全量重写」的快照，而轮次里以前只存 role/content，
    根本没有时间戳。于是「上次聊过」这个数只能取**写盘的时刻**，
    而写盘会被任何一次回复、巡群压缩、清死群触发 ——
    结果一段搁了五天的对话也显示「3 分钟前」，而且每次点进去都不一样。

只要时间还挂在「文件什么时候被写过」上，这类错就迟早再来一次。
把它挂在轮次本身上，就没法写错了：一行记录 = 一句话 + 它发生的时刻。

顺带白送两样：
  · replay(key) —— 这个会话按时间顺序发生过什么，能完整重放；
  · last_ts(key) —— 会话时间唯一的真相来源，面板和压缩判断都用它。

histories.json 仍然是她对话记忆的快照（一条都不删的口径不变）；
本文件是**流水**，按体积轮转，所以删它的旧段不算删记忆。
"""
import json
import os
import threading
import time

_LOCK = threading.Lock()
MAX_BYTES = int(os.getenv("HISTORY_LOG_MAX_BYTES", "8000000"))
MAX_SEGMENTS = int(os.getenv("HISTORY_LOG_MAX_SEGMENTS", "5"))
ROLE_KEEP = 2000


def _dir():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory")


def _path():
    return os.path.join(_dir(), "history_log.jsonl")


def _rotate_if_needed():
    """按体积切一刀，旧段逐级移位保留，最多留 MAX_SEGMENTS 段。

    这是运行流水不是她的记忆，滚动是允许的；但只做「搬走」不做「删掉」。
    原来直接 os.replace(当前, .1)：第二次轮转会把上一段 .1 静默覆盖掉，
    于是刚轮转出去的那批话立刻没了。现在先把 .1→.2、.2→.3 … 逐级移位
    （超出上限的最老一段才让位），再把当前段搬成 .1，旧段一段都不丢。
    """
    try:
        if not (os.path.isfile(_path()) and os.path.getsize(_path()) > MAX_BYTES):
            return
        for i in range(MAX_SEGMENTS - 1, 0, -1):
            src = _path() + ".%d" % i
            if os.path.isfile(src):
                os.replace(src, _path() + ".%d" % (i + 1))
        os.replace(_path(), _path() + ".1")
    except Exception as e:
        print("[会话流水] 轮转失败 " + type(e).__name__, flush=True)


def append(key, role, content, ts=None):
    """记一轮。失败静默：这是记账，不该把她的回复拖慢或者弄崩。"""
    try:
        k = str(key or "").strip()
        if not k:
            return False
        rec = {"key": k[:80], "role": str(role or "")[:12],
               "content": str(content or "")[:2000],
               "ts": round(float(ts or time.time()), 3)}
        with _LOCK:
            os.makedirs(_dir(), exist_ok=True)
            _rotate_if_needed()
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print("[会话流水] 写入失败 " + type(e).__name__, flush=True)
        return False


def _lines():
    out = []
    segs = [_path() + ".%d" % i for i in range(MAX_SEGMENTS, 0, -1)]
    for p in segs + [_path()]:     # 先最老段再当前段，时间序才对
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict) and rec.get("key"):
                        out.append(rec)
        except Exception:
            continue
    return out


def last_ts(key):
    """这个会话最后一次真有过去/来话的时刻。没有记录就返回 0（未知）。

    返回 0 而不是 now 是有意的：「不知道」和「刚刚」是两件事，
    拿 now 兜底就是那个 bug 本来的样子。
    """
    k = str(key or "")
    best = 0.0
    for rec in _lines():
        if rec.get("key") == k:
            t = float(rec.get("ts") or 0)
            if t > best:
                best = t
    return best


def tail(key, n=12):
    k = str(key or "")
    rows = [r for r in _lines() if r.get("key") == k][-max(1, int(n)):]
    return rows


def replay(key, n=ROLE_KEEP):
    """按时间顺序重放一个会话（默认给全量上限内的所有轮次）。"""
    return tail(key, n)


def stats():
    try:
        size = os.path.getsize(_path())
    except Exception:
        size = 0
    keys = set()
    n = 0
    for rec in _lines():
        keys.add(rec.get("key"))
        n += 1
    return {"lines": n, "sessions": len(keys), "bytes": size,
            "max_bytes": MAX_BYTES}


def backfill_histories(store_path=None, dry_run=False):
    """把 histories.json 里那批「写盘时刻」换成真实证据；没有证据就标未知。

    这些老值是上一版代码留下的：`updated` 直接取了 `time.time()`，
    所以它们既不表示对话时间，也解释不了「搁了五天还显示三分钟前」。
    现在流水能提供真相的会话，用流水的最后一个时间；提供不了的，
    把值清成 0 并标 `updated_source="unknown"` —— 面板显示 `--`。
    「不知道」比一个编出来的「刚刚」诚实，而且不会每次刷新都变。

    幂等：已经有 updated_source 的会话不再动。
    """
    import json as _json
    import shutil as _shutil
    p = store_path or os.path.join(os.getenv("DATA_DIR", "./data"),
                                   "memory", "histories.json")
    if not os.path.isfile(p):
        return {"ok": False, "reason": "没有 " + p}
    try:
        with open(p, encoding="utf-8") as f:
            data = _json.load(f) or {}
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__ + ": " + str(e)[:80]}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "格式不是对象"}

    from_log = unknown = kept = 0
    plan = []
    for key, rec in data.items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("updated_source") or ""):
            kept += 1
            continue
        ts = last_ts(key)
        if ts:
            rec["updated"] = ts
            rec["updated_source"] = "log"
            from_log += 1
        else:
            rec["updated"] = 0.0
            rec["updated_source"] = "unknown"
            unknown += 1
        plan.append((key, rec["updated_source"], rec["updated"]))
    res = {"ok": True, "dry_run": bool(dry_run), "from_log": from_log,
           "unknown": unknown, "already": kept, "plan": plan}
    if dry_run or not plan:
        return res
    try:
        _shutil.copy2(p, p + ".bak_pre-migrate")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        res["ok"] = False
        res["reason"] = type(e).__name__ + ": " + str(e)[:80]
    return res
