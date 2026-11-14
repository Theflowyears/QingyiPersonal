"""回复延迟仪表：把每一轮回复的耗时拆开记账。

为什么要单独一个模块：「2.5 秒内回」这个要求光靠调参证明不了，
必须知道时间到底花在哪 —— 预查询（搜索/天气/云端工具）、模型本身、
还是答案已经生成完之后的收尾（图片描述、长期记忆压缩）。

写 data/latency_stats.json，按天分桶，只留最近 N 天。
纯记账，任何失败都吞掉，绝不拖慢或影响回复本身。
"""
import json
import os
import threading
import time

_ROOT = os.getenv("DATA_DIR", "./data")
_FILE = os.path.join(_ROOT, "latency_stats.json")
_LOCK = threading.Lock()
_KEEP_DAYS = int(os.getenv("LATENCY_KEEP_DAYS", "14"))

_cache = {"sig": None, "data": {}}


def budget_seconds():
    try:
        return max(0.2, float(os.getenv("REPLY_BUDGET_MS", "2500")) / 1000.0)
    except Exception:
        return 2.5


def _today():
    return time.strftime("%Y-%m-%d")


def _load():
    try:
        st = os.stat(_FILE)
        sig = (round(st.st_mtime, 3), st.st_size)
        if _cache["sig"] == sig and _cache["data"]:
            return _cache["data"]
    except Exception:
        sig = None
    data = {}
    try:
        if os.path.isfile(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                got = json.load(f) or {}
            if isinstance(got, dict):
                data = got
    except Exception:
        data = {}
    _cache["sig"] = sig
    _cache["data"] = data
    return data


def record(total_s, kind="chat", pre_s=0.0, model_s=0.0, post_s=0.0,
           over_budget=False, cached=None, scene="", first_s=None):
    """记一笔。

    total_s = 「收到消息 → 整段答案就绪」。
    first_s = 「收到消息 → 第一条气泡发出去」；超了预算但先应了一声时，
              这俩差得很远，SLO 看的是 first_s，所以分开存。
    样本池 samples 存的是 first_s（人真正等的那段）。
    """
    try:
        with _LOCK:
            data = _load()
            day = data.setdefault(_today(), {})
            b = day.setdefault(kind, {"n": 0, "sum": 0.0, "max": 0.0,
                                      "over": 0, "pre": 0.0, "model": 0.0,
                                      "post": 0.0, "samples": [],
                                      "full_sum": 0.0, "full_max": 0.0})
            t = max(0.0, float(total_s or 0))
            f = t if first_s is None else max(0.0, float(first_s))
            b["n"] += 1
            b["sum"] += f
            b["max"] = max(b["max"], f)
            b["full_sum"] += t
            b["full_max"] = max(b["full_max"], t)
            b["pre"] += max(0.0, float(pre_s or 0))
            b["model"] += max(0.0, float(model_s or 0))
            b["post"] += max(0.0, float(post_s or 0))
            if over_budget:
                b["over"] += 1
            if scene:
                b["scene"] = scene
            b["samples"] = (b["samples"] + [round(f, 3)])[-200:]
            days = sorted(data.keys(), reverse=True)[_KEEP_DAYS:]
            for k in days:
                data.pop(k, None)
            tmp = _FILE + ".tmp"
            os.makedirs(os.path.dirname(_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f2:
                json.dump(data, f2, ensure_ascii=False, indent=1)
            os.replace(tmp, _FILE)
            _cache["data"] = data
            _cache["sig"] = None
    except Exception:
        pass


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def summary(day=None):
    """给 /power 和面板用的汇总：各口径的 n / 均值 / p50 / p95 / 超预算数。"""
    try:
        data = _load()
        day = day or _today()
        kinds = data.get(day) or {}
        out = {"day": day, "budget_ms": int(budget_seconds() * 1000), "kinds": {}}
        tot_n = tot_over = 0
        all_samples = []
        for kind, b in kinds.items():
            n = int(b.get("n") or 0)
            if not n:
                continue
            smp = list(b.get("samples") or [])
            tot_n += n
            tot_over += int(b.get("over") or 0)
            all_samples += smp
            out["kinds"][kind] = {
                "n": n,
                "avg": round(b["sum"] / n, 2),
                "p50": round(_pct(smp, 50), 2),
                "p95": round(_pct(smp, 95), 2),
                "max": round(float(b.get("max") or 0), 2),
                "over": int(b.get("over") or 0),
                "full_avg": round(b.get("full_sum", 0) / n, 2),
                "full_max": round(float(b.get("full_max") or 0), 2),
                "avg_pre": round(b["pre"] / n, 2),
                "avg_model": round(b["model"] / n, 2),
                "avg_post": round(b["post"] / n, 2),
            }
        out["total_n"] = tot_n
        out["total_over"] = tot_over
        out["overall_p50"] = round(_pct(all_samples, 50), 2)
        out["overall_p95"] = round(_pct(all_samples, 95), 2)
        out["overall_max"] = round(max(all_samples), 2) if all_samples else 0.0
        out["days_available"] = sorted(data.keys(), reverse=True)[:_KEEP_DAYS]
        return out
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:80]}


def line():
    """一行话，给体检报告用。口径是「首气泡」，不是整段就绪。"""
    s = summary()
    if not s.get("kinds"):
        return "延迟 今天还没样本"
    return ("首气泡 今天%d 次｜p50 %.2fs p95 %.2fs 最长 %.2fs｜超 %.2fs 预算 %d 次"
            % (s.get("total_n", 0), s.get("overall_p50", 0), s.get("overall_p95", 0),
               s.get("overall_max", 0), s.get("budget_ms", 2500) / 1000.0,
               s.get("total_over", 0)))
