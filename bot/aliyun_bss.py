"""阿里云 BSS 账单拉取：自动从云端取「哪个模型 / 哪种 token / 多少量 / 多少钱」。

为什么能这么细：
  QueryInstanceBill 的 InstanceID 字段把一切都编码进去了 ——
      12345678;ws-xxxxxxxxxxxxxxxx;模型名;output_token;0
      └空间┘   └──── API Key ────┘ └─模型─┘ └─计费项─┘
  拆开就知道是哪个模型的哪一种 token，不用再手动粘贴控制台。

计费项 → 本地类目：
  input_token              未命中缓存      miss
  input_token_cache        隐式缓存命中    hit_implicit
  input_token_cache_read   显式缓存命中    hit_explicit
  input_token_cache_creation_5m  缓存创建  write
  output_token             输出            output
"""
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

_ENDPOINT = "https://business.aliyuncs.com/"
_VERSION = "2017-12-14"

AK = os.getenv("ALIYUN_ACCESS_KEY_ID", "").strip()
SK = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "").strip()

# 计费项 → 内部类目
_KINDS = {
    "input_token": "miss",
    "input_token_cache": "hit_implicit",
    "input_token_cache_read": "hit_explicit",
    "input_token_cache_creation_5m": "write",
    "input_token_cache_creation_1h": "write",
    "output_token": "output",
}


def configured():
    return bool(AK and SK)


def _pe(s):
    """RFC3986 编码，阿里云要求 ~ 不编码、空格转 %20。"""
    return quote(str(s), safe="~")


def _call(action, params, timeout=30):
    """RPC 风格签名（HMAC-SHA1），不依赖任何阿里云 SDK。"""
    if not configured():
        raise RuntimeError("阿里云 AK/SK 未配置")
    p = {
        "Action": action,
        "Format": "JSON",
        "Version": _VERSION,
        "AccessKeyId": AK,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    p.update(params or {})
    items = sorted(p.items())
    canon = "&".join("%s=%s" % (_pe(k), _pe(v)) for k, v in items)
    sts = "POST&%s&%s" % (_pe("/"), _pe(canon))
    h = hmac.new((SK + "&").encode("utf-8"), sts.encode("utf-8"), hashlib.sha1)
    p["Signature"] = base64.b64encode(h.digest()).decode("utf-8")

    import httpx
    with httpx.Client(timeout=timeout) as c:
        r = c.post(_ENDPOINT, data=p)
    r.raise_for_status()
    d = r.json()
    if not d.get("Success", True):
        raise RuntimeError(str(d.get("Message") or d.get("Code") or "接口报错")[:120])
    return d


def balance():
    """账户余额，顺手当连通性探针。"""
    d = _call("QueryAccountBalance", {})
    data = d.get("Data") or {}
    return {
        "available": float(data.get("AvailableAmount") or 0),
        "cash": float(data.get("AvailableCashAmount") or 0),
        "currency": data.get("Currency") or "CNY",
    }


def fetch_bill(day=None, cycle=None):
    """取某一天的账单，按「模型 + 计费项」聚合。

    day 形如 "2026-11-06"；默认取今天。
    返回 {"date":.., "models": {模型: {类目: [千token, 元]}}, "total":¥}
    """
    if not day:
        day = datetime.now().strftime("%Y-%m-%d")
    cycle = cycle or day[:7]

    props = {
        "BillingCycle": cycle,
        "BillingDate": day,
        "Granularity": "DAILY",
        "ProductCode": "sfm",
        "IsBillingItem": "true",
    }
    out = {"date": day, "models": {}, "total": 0.0, "items": []}

    page = 1
    while page <= 20:
        props["PageNum"] = str(page)
        props["PageSize"] = "300"
        d = _call("QueryInstanceBill", props)
        data = d.get("Data") or {}
        items = (data.get("Items") or {}).get("Item") or []
        if not items:
            break
        for it in items:
            seg = str(it.get("InstanceID") or "").split(";")
            if len(seg) < 4:
                continue
            model, kind = seg[2], seg[3]
            cat = _KINDS.get(kind)
            if not cat:
                continue
            qty = float(it.get("Usage") or 0)
            amt = float(it.get("PretaxAmount") or 0)
            if qty <= 0 and amt <= 0:
                continue
            mm = out["models"].setdefault(model, {})
            old = mm.get(cat) or [0.0, 0.0]
            mm[cat] = [old[0] + qty, old[1] + amt]
            out["total"] += amt
            out["items"].append({
                "model": model, "kind": kind, "cat": cat,
                "qty_k": round(qty, 3), "cost": round(amt, 6),
                "unit": round(amt / qty * 1000, 4) if qty else 0.0,
                "list_price": it.get("ListPrice"),
            })
        if len(items) < 300:
            break
        page += 1

    out["total"] = round(out["total"], 6)
    return out


def daily_trend(cycle=None, days=7):
    """最近几天各花多少（外层循环按天查，接口没有一次拿多天的小时级明细）。"""
    from datetime import timedelta
    if not cycle:
        cycle = datetime.now().strftime("%Y-%m")
    today = datetime.now()
    out = []
    for i in range(int(days)):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            b = fetch_bill(day=d)
            out.append({"date": d, "cost": b["total"]})
        except Exception:
            out.append({"date": d, "cost": None})
    return out
