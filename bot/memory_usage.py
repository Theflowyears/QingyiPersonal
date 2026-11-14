"""记忆使用仪表：她「用上了」多少记忆，第一次有数。

为什么要这个模块：注入面有十来个块，每轮都在往提示词里塞东西，
但**没有任何一处知道被选中的条目后来有没有真的被她说出来**。
所以之前所有的「记忆优化」都是凭感觉改 —— 改了也证明不了有效。

口径（重要）：
  - 只记「真的进了提示词」的条目，不记候选池。候选没选上不算用过。
  - 写入侧的 `hits`（重复入库计数）不动，语义完全不同；
    这里新增的是检索侧的 `used / last_used / first_used_lag_days`。
  - 纯记账，任何失败都吞掉，绝不拖慢回复（一次 append < 0.1ms）。

落盘：
  data/memory/usage_log-YYYY-MM-DD.jsonl   每天一个文件，聚合后按天清理
  data/memory/usage_stats.json             聚合结果（按 store 分组）
"""
import hashlib
import json
import os
import threading
import time

def _dir():
    """DATA_DIR 每次现算。

    以前这三个是模块级常量：import 时就冻住了，之后改环境变量不生效，
    测试只能靠 patch 模块常量来隔离 —— 而「忘了 patch」的代价是
    直接往她真实的 data/ 里写脏账（这轮真的发生过，跨模块合跑假失败就是它）。
    """
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory")


def _stats():
    return os.path.join(_dir(), "usage_stats.json")
_LOCK = threading.Lock()

ENABLED = os.getenv("MEMORY_USAGE_LOG", "1").strip() == "1"
KEEP_DAYS = int(os.getenv("MEMORY_USAGE_KEEP_DAYS", "30"))

# 每个 store 的总条目数由各自模块报，这里只缓存最后一次统计
_cache = {"sig": None, "stats": {}}


def _today():
    return time.strftime("%Y-%m-%d")


def _log_path(day=None):
    return os.path.join(_dir(), "usage_log-%s.jsonl" % (day or _today()))


# ── 键：稳定身份，跨进程可比 ────────────────────────────
def key_group(entry_id, topic="", text=""):
    """群长期记忆：优先用条目 id；没有 id（旧数据/退回路径）才用内容指纹。"""
    eid = str(entry_id or "").strip()
    if eid:
        return "g:" + eid
    return "gt:" + _sha(str(topic or "") + "|" + str(text or "")[:80])


def key_kb(topic_key):
    return "kb:" + _sha(str(topic_key or ""))


def key_persona(kind, text):
    return "p:%s:%s" % (kind, _sha(_norm_text(text)))


def key_member(group_id, user_id):
    return "m:%s|%s" % (group_id, user_id)


def key_private(user_key):
    return "ps:" + str(user_key or "")[:40]


def _sha(s):
    return hashlib.sha1(str(s).encode("utf-8", "ignore")).hexdigest()[:12]


def _norm_text(s):
    t = str(s or "").strip()
    for ch in " \t\n，。、；;：:！!？?“”\"'（）()【】":
        t = t.replace(ch, "")
    return t.lower()[:80]


# ── 记一笔 ──────────────────────────────────────────
def note(store, keys, query="", injected_chars=0, scene="", candidates=0,
         fallback=False):
    """一轮里某个 store 实际注入了哪些条目。

    keys          进了提示词的稳定键
    candidates    候选池大小（算「选中的比例」用，可传 0）
    fallback      True = 没检索命中、走的是兜底（最近+重要）
    """
    if not ENABLED:
        return
    try:
        ks = [str(k) for k in (keys or []) if k]
        rec = {
            "ts": round(time.time(), 2),
            "store": str(store or ""),
            "n": len(ks),
            "keys": ks[:24],
            "cand": int(candidates or 0),
            "chars": int(injected_chars or 0),
            "empty": 1 if not ks else 0,
            "fb": 1 if fallback else 0,
            "q": str(query or "")[:24],
            "scene": str(scene or ""),
        }
        with _LOCK:
            os.makedirs(_dir(), exist_ok=True)
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 聚合 ────────────────────────────────────────────
def _load_stats():
    try:
        st = os.stat(_stats())
        sig = (round(st.st_mtime, 3), st.st_size)
        if _cache["sig"] == sig and _cache["stats"]:
            return _cache["stats"]
    except Exception:
        sig = None
    data = {}
    try:
        if os.path.isfile(_stats()):
            with open(_stats(), encoding="utf-8") as f:
                got = json.load(f) or {}
            if isinstance(got, dict):
                data = got
    except Exception:
        data = {}
    _cache["sig"] = sig
    _cache["stats"] = data
    return data


def _save_stats(data):
    try:
        os.makedirs(_dir(), exist_ok=True)
        tmp = _stats() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _stats())
        _cache["stats"] = data
        try:
            _cache["sig"] = None
        except Exception:
            pass
    except Exception:
        pass


def _day_stats(path, now):
    """把一天日志汇成可整体替换的贡献；重复读取同一天不会累加两次。"""
    d = {"rounds": 0, "chars": 0, "selected": 0, "empty_rounds": 0,
         "fallback_rounds": 0, "stores": {}, "keys": {}}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                st = r.get("store") or "?"
                ts = float(r.get("ts") or 0)
                d["rounds"] += 1
                d["chars"] += int(r.get("chars") or 0)
                d["selected"] += int(r.get("n") or 0)
                if r.get("empty"):
                    d["empty_rounds"] += 1
                if r.get("fb"):
                    d["fallback_rounds"] += 1
                sd = d["stores"].setdefault(st, {"rounds": 0, "selected": 0,
                                                 "empty": 0, "fb": 0, "chars": 0})
                sd["rounds"] += 1
                sd["selected"] += int(r.get("n") or 0)
                sd["empty"] += 1 if r.get("empty") else 0
                sd["fb"] += 1 if r.get("fb") else 0
                sd["chars"] += int(r.get("chars") or 0)
                for k in (r.get("keys") or []):
                    e = d["keys"].setdefault(k, {"used": 0, "first_seen": ts or now,
                                                "last_used": 0.0, "stores": st})
                    e["used"] = int(e.get("used", 0)) + 1
                    e["last_used"] = max(float(e.get("last_used") or 0), ts)
                    e["first_seen"] = min(float(e.get("first_seen") or ts or now), ts or now)
                    stores = {x for x in str(e.get("stores") or "").split(",") if x}
                    stores.add(st)
                    e["stores"] = ",".join(sorted(stores))
    except Exception:
        return None
    return d


def aggregate(days=KEEP_DAYS):
    """按日贡献重建窗口累计；同一日志反复聚合结果恒定。"""
    old = _load_stats()
    daily = {}
    now = time.time()
    touched = 0
    for i in range(days):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        p = _log_path(day)
        if not os.path.isfile(p):
            continue
        value = _day_stats(p, now)
        if value is not None:
            daily[day] = value
            touched += sum(int(x.get("used") or 0) for x in value.get("keys", {}).values())

    per = {}
    for value in daily.values():
        for k, e in (value.get("keys") or {}).items():
            out = per.setdefault(k, {"used": 0, "first_seen": e.get("first_seen") or now,
                                    "last_used": e.get("last_used") or 0.0,
                                    "stores": e.get("stores") or ""})
            out["used"] += int(e.get("used") or 0)
            out["last_used"] = max(float(out.get("last_used") or 0), float(e.get("last_used") or 0))
            out["first_seen"] = min(float(out.get("first_seen") or now), float(e.get("first_seen") or now))
            stores = {x for x in str(out.get("stores") or "").split(",") if x}
            stores.update(x for x in str(e.get("stores") or "").split(",") if x)
            out["stores"] = ",".join(sorted(stores))

    legacy = old.get("legacy_untrusted")
    if legacy is None and (old.get("keys") or {}):
        legacy = {"untrusted_keys": len(old.get("keys") or {}),
                  "reason": "v1累计受重复聚合污染，不用于新排名"}
    out = {"updated": now, "version": 2, "keys": per, "daily": daily,
           "legacy_untrusted": legacy or {}}
    with _LOCK:
        _save_stats(out)
    return {"ok": True, "touched": touched, "keys": len(per), "days": len(daily)}


# ── 读取（第 4 批的打分要用） ──────────────────────────
def usage_of(key):
    """返回 {used, last_used, first_seen}；没见过就是 0。"""
    if not key:
        return {"used": 0, "last_used": 0.0, "first_seen": 0.0}
    try:
        e = (_load_stats().get("keys") or {}).get(str(key)) or {}
        return {"used": int(e.get("used") or 0),
                "last_used": float(e.get("last_used") or 0),
                "first_seen": float(e.get("first_seen") or 0)}
    except Exception:
        return {"used": 0, "last_used": 0.0, "first_seen": 0.0}


def used_count(key):
    return usage_of(key)["used"]


def today():
    return (_load_stats().get("daily") or {}).get(_today()) or {}


def report():
    """给面板/仪表用的汇总：覆盖率、注入效率、空结果率。

    total_known 由调用方（或 _sizes()）报，用于算「从未被用过的条目数」。
    """
    st = _load_stats()
    per = st.get("keys") or {}
    d = today()
    days = sorted(st.get("daily") or {})[-7:]
    day_rows = [st["daily"][x] for x in days]
    rounds = sum(int(r.get("rounds") or 0) for r in day_rows)
    sel = sum(int(r.get("selected") or 0) for r in day_rows)
    empty = sum(int(r.get("empty_rounds") or 0) for r in day_rows)
    fb = sum(int(r.get("fallback_rounds") or 0) for r in day_rows)
    chars = sum(int(r.get("chars") or 0) for r in day_rows)
    used_keys = len(per)
    never = max(0, int(os.getenv("MEMORY_TOTAL_KNOWN", "0"))) - used_keys
    return {
        "today": {"rounds": d.get("rounds", 0), "chars": d.get("chars", 0),
                  "selected": d.get("selected", 0),
                  "empty_rounds": d.get("empty_rounds", 0),
                  "fallback_rounds": d.get("fallback_rounds", 0),
                  "stores": d.get("stores") or {}},
        "week": {"rounds": rounds, "selected": sel, "chars": chars,
                 "empty_rate": round(empty / rounds * 100, 1) if rounds else 0.0,
                 "fallback_rate": round(fb / rounds * 100, 1) if rounds else 0.0,
                 "avg_selected": round(sel / rounds, 2) if rounds else 0.0},
        "distinct_used": used_keys,
        "never_used_estimate": never if os.getenv("MEMORY_TOTAL_KNOWN") else None,
    }


def line():
    """一行话，接进 /power。没数据就说没数据，别装。"""
    d = today()
    if not d.get("rounds"):
        return "记忆 今天还没有使用样本"
    rounds = int(d.get("rounds") or 0)
    empty = int(d.get("empty_rounds") or 0)
    fb = int(d.get("fallback_rounds") or 0)
    per = _load_stats().get("keys") or {}
    return ("记忆 今天%d轮｜选中%d条｜注入%d字｜空命中 %.0f%%｜兜底 %.0f%%｜累计用过 %d 条"
            % (rounds, int(d.get("selected") or 0), int(d.get("chars") or 0),
               empty / rounds * 100, fb / rounds * 100, len(per)))
