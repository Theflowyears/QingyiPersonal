"""她对外的社交动作：好友列表、改签名。以及「哪些做不到」。

能力是**先探测再写代码**的（2026-11-07 对 NapCat v4.15.19 用空参数调用探过）：

    get_friend_list        retcode=0，正常返回 6 个好友
    get_login_info         retcode=0
    set_qq_profile         认识（只报缺 nickname）→ 签名可以改
    set_friend_add_request 认识（只报缺 flag）→ 别人加她，能处理
    add_friend             1404 不支持的API  → **主动加好友做不到**
    send_friend_request    1404 不支持的API
    send_qzone_msg         1404 不支持的API  → **QQ空间说说做不到**
    get_qzone_msg_list     1404 不支持的API

所以这里只提供真能用的两个：好友列表、改签名；
另外两个在 CAPABILITIES 里如实标成 False，谁问都能得到一句真话，
而不是调用失败后让人以为「可能是我参数写错了」。
"""
import time

# 探针结论，别改成 True —— 改了也不会工作，只会让上游以为能力存在
CAPABILITIES = {
    "friend_list": True,
    "set_signature": True,
    "handle_friend_request": True,
    "add_friend": False,      # OneBot 没这个动作，NapCat 这一版也没扩展
    "qzone_publish": False,   # 没有 qzone API；要做得上 NapCat 的 qzone 插件
    "qzone_read": False,
}

FRIEND_TTL = float(__import__("os").getenv("FRIEND_LIST_TTL_SECONDS", "21600"))
_friends = {"ts": 0.0, "items": []}


def _chan():
    import napcat_channel
    ch = getattr(napcat_channel, "channel", None)
    return ch


async def _call(action, params=None, timeout=15.0):
    ch = _chan()
    if ch is None:
        return {"retcode": -1, "message": "通道未就绪"}
    try:
        got = await ch.call(action, params or {}, timeout=timeout)
    except Exception as e:
        return {"retcode": -1, "message": type(e).__name__ + ": " + str(e)[:60]}
    return got if isinstance(got, dict) else {"retcode": -1, "message": "无回包"}


async def get_friends(force=False):
    """好友列表（带缓存）。返回 [{user_id, nickname, remark}]。"""
    now = time.time()
    if not force and _friends["items"] and (now - _friends["ts"]) < FRIEND_TTL:
        return _friends["items"]
    got = await _call("get_friend_list", {}, timeout=20.0)
    if got.get("retcode") not in (0, None):
        print("[社交] 取好友列表失败: %s" % str(got.get("message"))[:60], flush=True)
        return _friends["items"]
    data = got.get("data")
    items = []
    for f in (data if isinstance(data, list) else []):
        if not isinstance(f, dict):
            continue
        uid = str(f.get("user_id") or "").strip()
        if not uid.isdigit():
            continue
        items.append({"user_id": uid,
                      "nickname": str(f.get("nickname") or "")[:40],
                      "remark": str(f.get("remark") or "")[:40]})
    _friends.update({"ts": now, "items": items})
    return items


def friend_uids(friends=None):
    return [f["user_id"] for f in (friends if friends is not None else _friends["items"])]


async def get_profile():
    got = await _call("get_login_info", {})
    d = got.get("data") if isinstance(got.get("data"), dict) else {}
    return {"user_id": str(d.get("user_id") or ""),
            "nickname": str(d.get("nickname") or "")}


async def set_signature(text):
    """改个性签名。

    NapCat 的 set_qq_profile 要 nickname（空参数探测时报的就是缺 nickname），
    所以先把当前昵称读出来，连签名一起提交 —— 只传签名会被判缺参。
    签名参数的键名各版本不一致（personal_note / signature），两个都试一次，
    以回包为准，不靠猜。
    """
    text = str(text or "").strip()
    # G01：改签名决策点经统一契约登记一份对外公开提案（创造表达）。
    # 能力缺失登记为硬约束（hard_blocks），契约只旁路观测，不改发送行为；
    # 空签名登记为合法等待。一天一次/概率/时间窗仍由 signature_worker 裁决。
    import action_proposal as ap
    ap.observe(ap.build(
        "social", "signature", motive="创造表达",
        reason="她想给自己换一句签名",
        event_ids=[], goal_id="", topic=text[:60], topic_qq="",
        participants=[], recipient_qq="", visibility="public",
        scope={"channel": "qq_profile"}, evidence=[],
        deadline=time.time() + 3600.0,
        budget={"quota_key": "SIGNATURE_MIN_HOURS", "max_per_day": 1,
                "cooldown_seconds":
                    float(__import__("os").getenv("SIGNATURE_MIN_HOURS", "20")) * 3600.0},
        success="set_qq_profile 回包 retcode=0",
        failure="回包非 0，或拿不到她的昵称",
        abandon_when="空签名或拿不到昵称就放弃，不反复重试",
        hard_blocks=[] if CAPABILITIES.get("set_signature") else ["capability_missing"],
        decision="act" if text else "wait",
        gate_reasons=[] if text else ["empty_signature"]))
    if not text:
        return {"ok": False, "error": "签名为空"}
    if len(text) > 60:
        text = text[:60]
    prof = await get_profile()
    nick = prof.get("nickname") or ""
    if not nick:
        return {"ok": False, "error": "拿不到她的昵称，改不了签名"}
    last_err = ""
    for key in ("personal_note", "signature"):
        got = await _call("set_qq_profile",
                          {"nickname": nick, key: text}, timeout=20.0)
        rc = got.get("retcode")
        if rc == 0:
            print("[社交] 签名已改: %s" % text[:40], flush=True)
            return {"ok": True, "signature": text, "key": key}
        last_err = str(got.get("message") or got.get("wording") or rc)[:80]
    print("[社交] 改签名失败: " + last_err, flush=True)
    return {"ok": False, "error": last_err}


def capability_report():
    return {"capabilities": dict(CAPABILITIES),
            "friends_cached": len(_friends["items"]),
            "friends_cache_age_s": (round(time.time() - _friends["ts"], 1)
                                    if _friends["ts"] else None)}
