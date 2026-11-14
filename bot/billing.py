"""账单对账：把阿里云百炼控制台复制的账单文本解析成结构化数据，
再和本地 token_stats 的记录对比，算出「差价出在哪」。

为什么要这个：
  网关给出的 usage 只告诉你「这次输入了多少 token」，
  但真金白银是控制台按「未命中 / 命中 / 缓存创建 / 输出」四档分开算的。
  两边对不上时，光看 usage 是查不出原因的——必须拿账单做锚点。

支持的输入格式（从控制台直接 Ctrl+A 复制粘贴即可）：
    2026-11-06
    大模型推理
    qwen3.8-flash
    大模型文本消耗量
    输入（显式缓存命中）
    135.128
    千tokens
    ¥
    0.0135128
"""
import re

# 控制台里的类目名 → 内部键
_CATS = [
    ("输入（隐式缓存命中）", "hit_implicit"),
    ("输入（显式缓存命中）", "hit_explicit"),
    ("输入（未命中缓存）", "miss"),
    ("输入（显式缓存创建）", "write"),
    ("输出", "output"),
]

_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_MODEL = re.compile(r"[a-zA-Z][a-zA-Z0-9._-]{2,60}")
_QTY = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_MONEY = re.compile(r"^[0-9]*\.[0-9]{4,}$")


def _cat_key(line):
    t = line.strip()
    for name, key in _CATS:
        if t == name or t == "大模型文本消耗量" + name:
            return key
    return None


def parse(text):
    """把整段账单文本解析成 {date: {模型: {类目: (千token, 元)}}}。

    解析失败返回 {}，调用方自己决定是报错还是忽略。
    """
    lines = [x.strip() for x in str(text or "").splitlines()]
    lines = [x for x in lines if x]
    if not lines:
        return {}

    out = {}
    date = None
    model = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        m = _DATE.search(line)
        if m and len(line) <= 12:
            date = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
            i += 1
            continue

        if line in ("大模型推理", "大模型调用", "模型推理"):
            for j in range(i + 1, min(i + 4, n)):
                if _MODEL.match(lines[j]) and not _cat_key(lines[j]):
                    model = lines[j]
                    i = j + 1
                    break
            else:
                i += 1
            continue

        key = _cat_key(line)
        if key:
            qty = cost = None
            j = i + 1
            while j < n and j <= i + 6:
                t = lines[j]
                if t in ("千tokens", "千 tokens", "tokens", "千Token"):
                    j += 1
                    continue
                if t in ("¥", "￥", "元"):
                    j += 1
                    continue
                if qty is None and _QTY.match(t):
                    qty = float(t)
                    j += 1
                    continue
                if cost is None and _MONEY.match(t):
                    cost = float(t)
                    j += 1
                    continue
                break
            if qty is not None and cost is not None:
                d = out.setdefault(date or "unknown", {})
                mm = d.setdefault(model or "unknown", {})
                mm[key] = (qty, cost)
                i = j
                continue
        i += 1
    return out


def unit_prices(cat):
    """从一个模型的四类账单数据反推单价（元/百万 token）。"""
    def per_m(key):
        if key in cat:
            qty, cost = cat[key]
            if qty > 0:
                return round(cost / qty * 1000, 4)
        return None

    return {
        "hit_implicit": per_m("hit_implicit"),
        "hit_explicit": per_m("hit_explicit"),
        "miss": per_m("miss"),
        "write": per_m("write"),
        "output": per_m("output"),
    }


def summarize(cat):
    """一个模型当天的汇总：token 数 + 真实花费。"""
    keys = ("hit_implicit", "hit_explicit", "miss", "write", "output")
    qty = {k: cat.get(k, (0, 0))[0] for k in keys}
    cost = sum(cat.get(k, (0, 0))[1] for k in keys)
    total_in = qty["hit_implicit"] + qty["hit_explicit"] + qty["miss"] + qty["write"]
    return {
        "hit_implicit": int(round(qty["hit_implicit"] * 1000)),
        "hit_explicit": int(round(qty["hit_explicit"] * 1000)),
        "miss": int(round(qty["miss"] * 1000)),
        "write": int(round(qty["write"] * 1000)),
        "output": int(round(qty["output"] * 1000)),
        "input_total": int(round(total_in * 1000)),
        "cost": round(cost, 6),
    }


def compare(bill_date, bill_model, local):
    """本地记录 vs 账单，算差额并给出可能原因。"""
    b = summarize(bill_model)
    lp = int(local.get("prompt") or 0)
    lc = int(local.get("cached") or 0)
    lw = int(local.get("write") or 0)
    lo = int(local.get("completion") or 0)
    lcost = float(local.get("cost") or 0)

    bill_hit = b["hit_implicit"] + b["hit_explicit"]
    gaps = {
        "input": b["input_total"] - lp,
        "cached": bill_hit - lc,
        "write": b["write"] - lw,
        "output": b["output"] - lo,
        "cost": round(b["cost"] - lcost, 6),
    }

    reasons = []
    if abs(gaps["output"]) > max(500, lo * 0.2):
        reasons.append("输出差了 %d token：本地只统计走 llm_router 的调用，"
                       "网页实验台/智能体另算。" % gaps["output"])
    if b["hit_explicit"] and lw == 0:
        reasons.append("账单有「显式缓存命中 %d」而本地没有："
                       "说明同一个 key 下还有别的程序开了显式缓存。"
                       % b["hit_explicit"])
    if b["write"] and lw == 0:
        reasons.append("账单有「显式缓存创建 %d」而本地没有："
                       "创建缓存按 1.25/百万计费，本地记不到这笔。"
                       % b["write"])
    if not reasons and abs(gaps["cost"]) < 0.01:
        reasons.append("对得上，误差在四舍五入范围内。")

    return {
        "date": bill_date,
        "model": bill_model,
        "bill": b,
        "local": {"prompt": lp, "cached": lc, "write": lw,
                  "completion": lo, "cost": round(lcost, 6)},
        "gap": gaps,
        "prices": unit_prices(bill_model),
        "reasons": reasons,
    }
