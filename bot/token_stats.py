"""Token 用量统计（按天 + 按模型）。

为什么单独一个模块：
  llm_router 里只有一个全局累加器（_STATE），重启就清零，也分不出是哪个模型花的。
  这里按「日期 → 模型」两级记，落盘保留最近 30 天，面板能直接读出当天汇总。

数据结构（data/token_stats.json）：
  {
    "2026-11-05": {
      "qwen3.8-flash": {"calls": 120, "prompt": 560000, "cached": 320000, "completion": 9000},
      "deepseek-flash": {...},
      "gpt-6-luna": {...}
    },
    ...
  }
"""
import json
import os
import threading
import time

_DATA_DIR = os.getenv("DATA_DIR", "./data")
_FILE = os.path.join(_DATA_DIR, "token_stats.json")
KEEP_DAYS = int(os.getenv("TOKEN_STATS_KEEP_DAYS", "30"))

_lock = threading.Lock()
_cache = {"data": None, "mtime": 0.0}

# 单价（元/百万 token）。数值取自阿里云实际账单反推，不是官网宣传价。
#
# 2026-11-05 的真实账单：
#   未命中缓存   2126.151 千 token  ¥1.7009  → ¥0.80/百万
#   显式缓存命中  2124.703 千 token  ¥0.2125  → ¥0.10/百万
#   显式缓存创建   118.384 千 token  ¥0.1480  → ¥1.25/百万
# 所以「未命中」才是真正的成本大头，缓存命中便宜 8 倍。
# 2026-11-06 的账单（qwen3.8-flash，四类输入分开计价）：
#   隐式缓存命中    65.024 千 token  ¥0.0065024  → 0.10/百万
#   显式缓存命中   135.128 千 token  ¥0.0135128  → 0.10/百万
#   未命中缓存     191.267 千 token  ¥0.1530136  → 0.80/百万
#   显式缓存创建   104.456 千 token  ¥0.1305700  → 1.25/百万   ← 比未命中还贵
#   输出            24.331 千 token  ¥0.0656937  → 2.70/百万   ← 原来写 1.50 是错的
#
# 关键：缓存「创建」和缓存「命中」是两码事。创建要把整段 prompt 写进缓存，
# 单价 1.25 竟然是未命中的 1.56 倍。只有命中才便宜（0.10）。
# output 是绝对大头（2.70），之前低估了 44%。
PRICES = {
    "qwen3.8-flash":   {"input": 0.80,  "output": 2.70,  "cached": 0.10, "write": 1.25},
    "qwen-image-3.0":  {"input": 0.80,  "output": 2.70,  "cached": 0.10, "write": 1.25},
    "deepseek-flash":  {"input": 0.14,  "output": 0.28,  "cached": 0.014},
    "gpt-6-luna":      {"input": 0.105, "output": 0.525, "cached": 0.0105},
    "gpt-6.1-sol":     {"input": 0.30,  "output": 1.50,  "cached": 0.015, "write": 0.375},
    "claude-sonnet-5-5": {"input": 1.00, "output": 5.00, "cached": 0.10},
}

# 按张计费的（图像），不按 token
PER_IMAGE = {
    "qwen-image-3.0": 0.18,
    "gpt-image-2.5-flare": 0.05,
    "gpt-image-2.5-flare-b": 0.05,
}

# 按次计费的（语音/成曲），不按 token。数值是估算，不是账单实测。
PER_CALL = {
    "qwen3-tts-flash": 0.002,          # 说话，一句 0.002 元上下
    "qwen-audio-3.0-tts-flash": 0.003,  # 唱歌（流式）
    "qwen-audio-3.1-tts-next": 0.003,   # 唱歌（HTTP 备选）
    "suno-v5": 0.15,                    # 完整成曲，中转站按次，最贵
}


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


def _load():
    try:
        if os.path.isfile(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _save(d):
    try:
        os.makedirs(os.path.dirname(_FILE) or ".", exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _FILE)
    except Exception as e:
        print("[用量] 存盘失败 " + str(e)[:60], flush=True)


def record(model, prompt=0, cached=0, completion=0, role="", write=0):
    """记一次调用。model 用实际模型名（qwen3.8-flash 这种）。

    write = 显式缓存创建消耗的 token 数（单价 1.25，比未命中还贵，必须单独记）。
    按次/按张计费的模型（语音、成曲、图像）没有 token 概念，prompt 当次数用。
    """
    name = str(model or "unknown").strip() or "unknown"
    try:
        prompt = int(prompt or 0)
        cached = int(cached or 0)
        completion = int(completion or 0)
        write = int(write or 0)
    except Exception:
        return
    # 按次计费的也要记（它们没有 token）
    if prompt <= 0 and completion <= 0 and name not in PER_CALL and name not in PER_IMAGE:
        return

    with _lock:
        d = _load()
        day = _today()
        bucket = d.setdefault(day, {})
        m = bucket.setdefault(name, {"calls": 0, "prompt": 0,
                                     "cached": 0, "completion": 0, "roles": {}})
        m["calls"] = int(m.get("calls") or 0) + 1
        # 按次/按张的模型，这里的 prompt 当「次数」用，累加起来就是总次数
        if name in PER_CALL or name in PER_IMAGE:
            prompt = 1
        m["prompt"] = int(m.get("prompt") or 0) + prompt
        m["cached"] = int(m.get("cached") or 0) + cached
        m["completion"] = int(m.get("completion") or 0) + completion
        if write:
            m["write"] = int(m.get("write") or 0) + write
        if role:
            rs = m.setdefault("roles", {})
            rs[role] = int(rs.get(role) or 0) + 1

        # 只留最近 KEEP_DAYS 天
        if len(d) > KEEP_DAYS:
            keep = sorted(d.keys(), reverse=True)[:KEEP_DAYS]
            d = {k: d[k] for k in keep}
        _save(d)


def _cost_of(name, prompt, cached, completion, write=0):
    """按账单的四类口径算钱。

    prompt_tokens 是「输入总承包」，里面同时含 cached（命中）和 write（创建）。
    所以：
      未命中部分 = prompt - cached - write
    三块各按各的价：
      未命中 0.80 ｜ 命中 0.10 ｜ 创建 1.25 ｜ 输出 2.70
    """
    # 按次计费的（语音/成曲）：单价 × 调用次数
    if name in PER_CALL:
        return float(PER_CALL[name]) * max(1, int(prompt or 0) or 1)
    # 按张计费的（图像）：单价 × 张数
    if name in PER_IMAGE:
        return float(PER_IMAGE[name]) * max(1, int(prompt or 0) or 1)
    p = PRICES.get(name) or {"input": 0.3, "output": 1.5, "cached": 0.015}
    prompt = int(prompt or 0)
    cached = int(cached or 0)
    write = int(write or 0)
    completion = int(completion or 0)
    # 兜底：write 不能超过 prompt（老数据可能没有这个字段）
    write = max(0, min(write, prompt - cached)) if write else 0
    fresh = max(0, prompt - cached - write)
    write_price = p.get("write", p["input"])
    return (fresh * p["input"]
            + cached * p["cached"]
            + write * write_price
            + completion * p["output"]) / 1_000_000.0


def price_table():
    """所有已知模型的单价，给面板展示（含 linlong 中转站的模型）。"""
    rows = []
    for name, p in PRICES.items():
        rows.append({
            "model": name,
            "kind": "token",
            "input": p.get("input"),
            "output": p.get("output"),
            "cached": p.get("cached"),
            "write": p.get("write"),
        })
    for name, v in PER_CALL.items():
        rows.append({"model": name, "kind": "call", "per_call": v})
    for name, v in PER_IMAGE.items():
        rows.append({"model": name, "kind": "image", "per_image": v})
    return rows


def today():
    """返回当天汇总，含每个模型的明细和估算花费。"""
    d = _load()
    bucket = d.get(_today()) or {}
    rows = []
    total = {"calls": 0, "prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}
    for name, m in bucket.items():
        if not isinstance(m, dict):
            continue
        c = _cost_of(name, m.get("prompt", 0), m.get("cached", 0),
                     m.get("completion", 0), m.get("write", 0))
        pct = (float(m.get("cached", 0)) / float(m.get("prompt") or 1)) * 100
        rows.append({
            "model": name,
            "calls": int(m.get("calls") or 0),
            "prompt": int(m.get("prompt") or 0),
            "cached": int(m.get("cached") or 0),
            "completion": int(m.get("completion") or 0),
            "write": int(m.get("write") or 0),
            "cached_pct": round(pct, 1),
            "cost": round(c, 4),
            "roles": m.get("roles") or {},
        })
        for k in ("calls", "prompt", "cached", "completion"):
            total[k] += rows[-1][k]
        total["cost"] += c
    rows.sort(key=lambda x: -x["cost"])
    total["cost"] = round(total["cost"], 4)
    total["cached_pct"] = round(
        (float(total["cached"]) / float(total["prompt"] or 1)) * 100, 1)
    return {"date": _today(), "models": rows, "total": total}


def recent(days=7):
    """最近几天的每日汇总（用于趋势）。"""
    d = _load()
    out = []
    keys = sorted(d.keys(), reverse=True)[:int(days)]
    for day in reversed(keys):
        bucket = d.get(day) or {}
        tot = {"prompt": 0, "cached": 0, "completion": 0, "cost": 0.0, "calls": 0}
        for name, m in bucket.items():
            if not isinstance(m, dict):
                continue
            tot["prompt"] += int(m.get("prompt") or 0)
            tot["cached"] += int(m.get("cached") or 0)
            tot["completion"] += int(m.get("completion") or 0)
            tot["calls"] += int(m.get("calls") or 0)
            tot["cost"] += _cost_of(name, m.get("prompt", 0),
                                     m.get("cached", 0), m.get("completion", 0),
                                     m.get("write", 0))
        tot["cost"] = round(tot["cost"], 4)
        tot["date"] = day
        out.append(tot)
    return out
