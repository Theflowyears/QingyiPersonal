#!/usr/bin/env python3
"""QQ 登录探针 —— 专门盯「掉线」这件事。

为什么单独写一个：
  掉线原因有五六种，日志里又混在一起。每次都要人工翻 docker logs 很蠢，
  而且事后查不出「到底几点掉的、掉了多久、为什么掉」。

这个脚本每分钟跑一次，做四件事：
  ① 记录当前状态（在线/离线/待扫码 + 持续多久）
  ② 采集掉线证据（KickedOffLine 原因、Chromium 崩溃、shm 用量）
  ③ 状态变化时写事件日志（掉线/恢复各一条，带原因）
  ④ 异常时推邮件（复用 watchdog 的通道）

数据落在 data/probe/ 下：
  state.json   当前状态快照
  events.jsonl 事件流水（掉线/恢复/崩溃，一行一条，永不覆盖）
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

# 自己加载 config.env（同 watchdog 的理由：systemd 定时器只传 DATA_DIR，
# BOT_QQ_UID 这类键不自己读就永远为空——检测静默失效比误报更难发现）。
# 必须在下面 DATA/STATE 等 import 期求值的常量之前执行。
def _load_env_files():
    for cand in ("/opt/qqbot/config.env",
                 str(Path(__file__).parent.parent / "config.env"),
                 "./config.env"):
        try:
            if not os.path.isfile(cand):
                continue
            with open(cand, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                        v = v[1:-1]
                    if k:
                        os.environ.setdefault(k, v)
            break
        except Exception:
            pass


_load_env_files()

DATA = Path(os.getenv("DATA_DIR", "/opt/qqbot/data"))
PROBE_DIR = DATA / "probe"
STATE = PROBE_DIR / "state.json"
EVENTS = PROBE_DIR / "events.jsonl"
CONTAINER = os.getenv("NAPCAT_CONTAINER", "napcat")

# 展示时效：面板只给近 72 小时内的事件。账本 events.jsonl 是流水永不删，
# 但误报（例如容器升级触发的 recreated）不该无限期赖在「最近事件」里。
RECENT_WINDOW_SECONDS = 72 * 3600


def _env(k, d=""):
    return os.getenv(k, d).strip()


def _sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def _load_events(n=200):
    if not EVENTS.is_file():
        return []
    out = []
    try:
        with open(EVENTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out[-n:]


def _push(ev):
    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_state():
    try:
        if STATE.is_file():
            return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(st):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    except Exception:
        pass


def _log_tail(kb=400):
    """取容器日志尾部。窗口要够大，跨过多次重启才看得到完整原因。"""
    out = _sh("docker logs --tail 3000 %s 2>&1" % CONTAINER, timeout=40)
    # 去掉 ANSI 颜色
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    return out


def _onebot_login_info(timeout=5):
    """直接问 OneBot：你是谁。这是唯一可靠的登录证据。

    日志里的「Adapter opened」是 debug 级，不一定每次都打；
    「适配器初始化完成」也可能是重连过程中的中间态。
    只有 <get_login_info> 返回真实 QQ 号，才能确定她在线。
    """
    try:
        import asyncio

        async def _go():
            import websockets
            async with websockets.connect(
                    "ws://127.0.0.1:%d" % int(os.getenv("ONEBOT_PORT", "3001")),
                    open_timeout=timeout) as ws:
                await ws.send('{"action":"get_login_info","params":{},"echo":"probe"}')
                # 连上后 NapCat 会先推一条 lifecycle 事件，不带 echo。
                # 必须一直读到带我们 echo 的那条，否则会把事件当成响应。
                deadline = timeout
                while deadline > 0:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline)
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if d.get("echo") != "probe":
                        continue        # 事件或别人的响应，跳过
                    if d.get("status") == "ok":
                        data = d.get("data") or {}
                        return {"ok": True,
                                "user_id": str(data.get("user_id") or ""),
                                "nickname": str(data.get("nickname") or "")}
                    return {"ok": False, "retcode": d.get("retcode")}
                return {"ok": False, "error": "timeout"}

        return asyncio.run(_go())
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def classify_kick(reason):
    """把 QQ 的掉线原话归类成可操作的原因。

    这几种在日志里长得差不多，但处置完全不同：
      · 另一台终端登录  → 别的地方登了同一个号（本地QQ/手机），要人工停掉
      · 登录已失效      → 会话过期或风控，等一会儿扫码即可
      · 账号异常/冻结    → 需要去 QQ 安全中心处理
    """
    r = str(reason or "")
    if "另一台终端" in r or "另一台设备" in r:
        return ("kicked_elsewhere",
                "同一个 QQ 号在别的设备登录了（可能是你本地/手机上的 QQ），把云端挤下线")
    if "登录已失效" in r or "身份已失效" in r:
        return ("session_expired", "登录会话失效（多半是频繁重登触发的风控）")
    if "冻结" in r or "异常" in r:
        return ("account_locked", "账号被风控/冻结，需要去 QQ 安全中心解封")
    if r:
        return ("kicked_other", "被服务器踢下线：" + r[:40])
    return ("unknown", "没有明确的掉线记录")


def check():
    st = _load_state()
    now = time.time()
    log = _log_tail()

    res = {
        "ts": now,
        "ts_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "container": "",
        "container_id": "",
        "container_recreated": False,
        "online": False,
        "state": "unknown",
        "reason": "",
        "last_online_ts": st.get("last_online_ts") or 0,
        "offline_since": st.get("offline_since") or 0,
        "shm_pct": 0.0,
        "shm_used": "",
        "crashes": 0,
        "kicks": [],
        "ws_ok": False,
        "extra_accounts": [],
    }

    # ① 容器状态
    res["container"] = _sh("docker inspect -f '{{.State.Status}}' %s" % CONTAINER).strip()

    # 容器 ID：一旦变化说明容器被重建过。
    # 重建 = QQ 眼中一台新设备 → 快速登录失效 → 必须重新扫码。
    # 这是「频繁扫码」的根因，必须盯住。
    try:
        cid = _sh("docker inspect -f '{{.Id}}' %s" % CONTAINER).strip()[:12]
        res["container_id"] = cid
        prev_cid = str(st.get("container_id") or "")
        if prev_cid and cid and prev_cid != cid:
            res["container_recreated"] = True
            _push({"ts": now, "type": "recreated",
                   "text": time.strftime("%H:%M:%S", time.localtime(now)),
                   "reason": "容器被重建（%s → %s），快速登录会失效，需重新扫码"
                             % (prev_cid, cid)})
    except Exception:
        pass

    # ② 登录判定：优先问 OneBot（可靠），日志只作补充
    _qb = _onebot_login_info()
    res["onebot_ok"] = bool(_qb.get("ok"))
    res["onebot_uid"] = _qb.get("user_id") or ""
    res["onebot_nick"] = _qb.get("nickname") or ""

    # ③ 从日志判断（仅用于补充掉线原因等）
    def _last_pos(pattern):
        m = None
        for mm in re.finditer(pattern, log):
            m = mm
        return m

    p_open = _last_pos(r"Adapter opened[^\n]*")
    p_off = _last_pos(r"账号状态变更为离线")
    p_qr = _last_pos(r"二维码已保存")
    p_kick = _last_pos(r"KickedOffLine\] \[下线通知\] ([^\n]+)")

    # ── 登录判定：OneBot 说了算 ──
    # 以前只看日志里有没有「Adapter opened」，但那是 debug 级日志，
    # 重新登录后不一定打出来，导致她明明在线、面板却一直显示「等扫码」。
    # 所以以 get_login_info 的返回为准，日志只用来解释「为什么掉」。
    if res.get("onebot_ok"):
        res["online"] = True
        res["state"] = "online"
        res["last_online_ts"] = now
        res["offline_since"] = 0
    else:
        # OneBot 问不到 → 确实不在线。区分「等扫码」和「离线」
        if p_qr and (not p_off or p_qr.start() > p_off.start()):
            res["state"] = "waiting_scan"
        elif p_off:
            res["state"] = "offline"
        else:
            res["state"] = "offline"
        if not res["offline_since"]:
            res["offline_since"] = now

    # 掉线原因（取最近 3 条不同的）
    if p_kick:
        res["reason"] = p_kick.group(1).strip()[:80]
        _code, _desc = classify_kick(res["reason"])
        res["reason_code"] = _code
        res["reason_desc"] = _desc
    # 掉线史：带时间戳，用来算"多久被踢一次"。
    # 只有时间和原因的序列能区分「偶发」和「周期性风控」——光看次数看不出来。
    kick_rows = []
    for m in re.finditer(
            r"(\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[^\n]*KickedOffLine\] \[下线通知\] ([^\n]+)", log):
        _ts, _txt, _reason = m.group(1), m.group(2), m.group(3)
        _code, _ = classify_kick(_reason)
        kick_rows.append({"date": _ts, "time": _txt, "reason": _reason.strip()[:80],
                          "code": _code})
    res["kick_history"] = kick_rows[-12:]
    res["kicks"] = [k["reason"] for k in kick_rows[-3:]]

    # 最近 1 小时被踢几次（>=2 就是风控/冲突的信号）
    _now_hm = None
    try:
        import datetime as _dt
        _now_hm = _dt.datetime.now().strftime("%H:%M:%S")
    except Exception:
        pass
    res["kicks_last_hour"] = 0
    if kick_rows and _now_hm:
        _latest = kick_rows[-1]
        try:
            _t0 = _dt.datetime.strptime(_latest["time"], "%H:%M:%S")
            _cnt = 0
            for k in kick_rows:
                _t = _dt.datetime.strptime(k["time"], "%H:%M:%S")
                if (_t0 - _t).total_seconds() <= 3600:
                    _cnt += 1
            res["kicks_last_hour"] = _cnt
        except Exception:
            pass

    # ③ Chromium 崩溃次数
    # 注意：这个数字只是「Worker 重启时顺带崩的」，不是掉线原因。
    # 真正的掉线原因在 KickedOffLine 那行里，两个要分开报，否则会误判。
    # 只统计「最近一次登录成功之后」的崩溃。
    # 否则这个数字会一直累加，明明早恢复了还显示"崩溃 3 次"，看着像还在坏。
    _last_ok = None
    for _m in re.finditer(r"Adapter opened|适配器初始化完成", log):
        _last_ok = _m
    _seg = log[_last_ok.start():] if _last_ok else log
    res["crashes"] = len(re.findall(r"Network service crashed", _seg))
    res["crashes_total"] = len(re.findall(r"Network service crashed", log))

    # ④ shm 用量（64MB 时撑不住 Chromium，这是一类根因）
    du = _sh("docker exec %s df -k /dev/shm 2>/dev/null | tail -1" % CONTAINER)
    parts = du.split()
    if len(parts) >= 4:
        try:
            used_pct = int(parts[4].rstrip("%")) if len(parts) > 4 else 0
        except Exception:
            used_pct = 0
        res["shm_pct"] = used_pct
        res["shm_used"] = "%s / %s (%s)" % (parts[2], parts[1], parts[4] if len(parts) > 4 else "")

    # ⑤ 多余账号（多号共存会互相牵连）
    cfg = _sh("ls /app/napcat/config/ 2>/dev/null; ls /opt/napcat/config/ 2>/dev/null")
    uins = set(re.findall(r"napcat_(\d{6,12})\.json", cfg))
    _bot_uid = os.getenv("BOT_QQ_UID", "").strip()
    extra = [u for u in uins if u != _bot_uid] if _bot_uid else []
    res["extra_accounts"] = sorted(extra)

    # ⑥ WS 端口（bot 能不能连上）
    res["ws_ok"] = ("LISTEN" in _sh("ss -tln 2>/dev/null | grep ':3001'"))
    if res["online"] and not res["ws_ok"]:
        res["state"] = "online_no_ws"

    # ── 状态变化 → 写事件 ──
    prev = st.get("state") or "unknown"
    if res["state"] != prev:
        if res["state"] == "online" and prev != "online":
            down_sec = int(now - (st.get("offline_since") or now)) if st.get("offline_since") else 0
            _push({"ts": now, "type": "recover",
                   "down_seconds": down_sec,
                   "text": time.strftime("%H:%M:%S", time.localtime(now)),
                   "reason": res["reason"]})
            res["offline_since"] = 0
        elif res["state"] in ("offline", "waiting_scan") and prev == "online":
            _push({"ts": now, "type": "offline",
                   "text": time.strftime("%H:%M:%S", time.localtime(now)),
                   "reason": res["reason"] or "(未知，无 KickedOffLine 记录)",
                   "crashes": res["crashes"]})
            res["offline_since"] = now
        st_base = dict(res)
        st_base["prev_state"] = res["state"]
        _save_state(st_base)
    else:
        _save_state(res)

    return res


def _session_stats(events):
    """从事件流算出每次会话活了多久。

    被踢的规律只有「登录多久后被踢」这个数才看得出来——
    光看次数分不清是随机风控还是有固定周期。
    """
    sessions = []
    up_at = None
    for e in events or []:
        if e.get("type") == "recover":
            up_at = float(e.get("ts") or 0)
        elif e.get("type") == "offline" and up_at:
            sessions.append(round(float(e.get("ts") or 0) - up_at))
            up_at = None
    current = round(time.time() - up_at) if up_at else 0
    return {"sessions": sessions[-10:], "current_seconds": current}


def status():
    st = _load_state()
    _cut = time.time() - RECENT_WINDOW_SECONDS
    ev = [e for e in _load_events(200) if float(e.get("ts") or 0) >= _cut]
    offline_sec = 0
    if st.get("state") in ("offline", "waiting_scan") and st.get("offline_since"):
        offline_sec = int(time.time() - float(st["offline_since"]))
    _ss = _session_stats(_load_events(200))
    return {
        "state": st.get("state", "unknown"),
        "online": bool(st.get("online")),
        "reason": st.get("reason") or "",
        "offline_seconds": offline_sec,
        "shm_pct": st.get("shm_pct"),
        "shm_used": st.get("shm_used"),
        "crashes": st.get("crashes") or 0,
        "crashes_total": st.get("crashes_total") or 0,
        "kick_history": st.get("kick_history") or [],
        "kicks_last_hour": st.get("kicks_last_hour") or 0,
        "sessions_seconds": _ss["sessions"],
        "current_seconds": _ss["current_seconds"],
        "ws_ok": st.get("ws_ok"),
        "extra_accounts": st.get("extra_accounts") or [],
        "container_id": st.get("container_id") or "",
        "onebot_uid": st.get("onebot_uid") or "",
        "onebot_nick": st.get("onebot_nick") or "",
        "container_recreated": bool(st.get("container_recreated")),
        "events": ev[-10:],
        "checked_at": st.get("ts_text"),
    }


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
    else:
        r = check()
        print(json.dumps(r, ensure_ascii=False, indent=1))
