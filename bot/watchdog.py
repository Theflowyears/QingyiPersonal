"""掉线报警 + 自愈。

检测三个层面，每种故障的处置不一样：

  ① qqbot 服务挂了          → 自动重启（安全，不影响 QQ 登录）
  ② NapCat 3001 没监听      → **只报警，绝不自动重启**
     因为 docker restart napcat 会让 QQ 掉登录，必须人工扫码。
     盲目重启等于把"要扫码"变成"反复要扫码"。
  ③ bot 连着 NapCat 但 5 分钟没动静 → 只报警（可能是 NapCat 假死）

报警策略（防轰炸）：
  · 只在状态**变化**时推送（正常→异常、异常→正常各一次）
  · 持续异常每 30 分钟提醒一次，不重复刷屏
  · 恢复时发一条"已恢复"

报警渠道（都可以不配，不配就只写日志 + 面板可见）：
  · WATCHDOG_WEBHOOK_URL  企业微信/钉钉/飞书 机器人 webhook
  · WATCHDOG_EMAIL_TO     邮件（配合 SMTP 三项）
"""
import json
import os
import smtplib
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

# 自己加载 config.env。
# 不能指望 systemd 传进来：定时器只给了 DATA_DIR，SMTP 那几项读不到就会静默不发邮件
# ——"自愈成功了但你没收到通知"比不报警更糟。
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
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    # 不覆盖已经显式设置的环境变量
                    if k and k not in os.environ:
                        os.environ[k] = v
            return cand
        except Exception:
            continue
    return ""


_ENV_SOURCE = _load_env_files()
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "watchdog_state.json"
BOT_LOG = Path("/opt/qqbot/logs/Bot.log")
# bot 自己写的心跳状态（桥接是否在线、最近一次收到时间）
STATUS_FILE = DATA_DIR / "bot_status.json"
REMIND_EVERY = int(os.getenv("WATCHDOG_REMIND_SECONDS", "1800"))   # 30 分钟
SILENT_MINUTES = int(os.getenv("WATCHDOG_SILENT_MINUTES", "5"))


def _env(k, d=""):
    return os.getenv(k, d).strip()


def _load_state():
    try:
        if STATE_FILE.is_file():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"status": "ok", "issues": [], "since": 0, "last_push": 0, "log": []}


def _save_state(st):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception:
        pass


def _service_active(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() == "active"
    except Exception:
        return False


def _port_open(port, host="127.0.0.1", timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _bot_connected_recently(minutes=SILENT_MINUTES):
    """bot 是否与 NapCat 连着。返回 (是否连着, 距上次心跳的秒数)。

    以前只翻日志末尾 400 行找"已连接 NapCat OneBot"，结果被聊天日志淹了：
    她那 629 行私聊/巡群/气泡记录把连接行挤出窗口，于是**明明连着却报断线**，
    邮件就一遍遍发。

    现在优先读 bot 自己写的 data/bot_status.json——带 received_at 时间戳，
    是权威状态；日志只作兜底，且窗口放大到 3000 行。
    """
    # ① 权威来源：状态文件
    try:
        if STATUS_FILE.is_file():
            st = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            rec = str(st.get("received_at") or "")
            bridge = str(st.get("bridge") or "").lower()
            if rec and bridge in ("online", "offline"):
                ts = datetime.fromisoformat(rec.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = int((datetime.now(timezone.utc) - ts).total_seconds())
                # 心跳新鲜才作数；bridge=offline 说明 bot 自己在重连
                if age <= minutes * 60:
                    return (bridge == "online"), age
    except Exception:
        pass

    # ② 兜底：日志（窗口放大，避免被闲聊挤掉）
    try:
        if not BOT_LOG.is_file():
            return False, -1
        size = BOT_LOG.stat().st_size
        with open(BOT_LOG, "rb") as f:
            f.seek(max(0, size - 1024 * 1024))
            tail = f.read().decode("utf-8", "replace")
        lines = [x for x in tail.splitlines() if x.strip()]
        for line in reversed(lines[-3000:]):
            if "已连接 NapCat OneBot" in line:
                return True, 0
            if "连接断开" in line:
                return False, 0
        return False, -1
    except Exception:
        return False, -1


_WEB_HEALTH_CACHE = {}
_WEB_HEALTH_LOCK = threading.Lock()


def _web_probe(url):
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                                  "User-Agent": "qqbot-health/1"})
        with urllib.request.urlopen(req, timeout=4) as response:
            return dict(ok=response.status == 200, status=response.status,
                        seconds=round(time.monotonic() - started, 3),
                        edge=response.headers.get("CF-Ray", ""))
    except urllib.error.HTTPError as exc:
        result = dict(ok=False, status=exc.code, seconds=round(time.monotonic() - started, 3),
                      edge=exc.headers.get("CF-Ray", ""))
        exc.close()
        return result
    except (OSError, ValueError) as exc:
        return dict(ok=False, status=0, error=type(exc).__name__,
                    seconds=round(time.monotonic() - started, 3))


def _web_health():
    local = _env("WATCHDOG_LOCAL_URL", "http://127.0.0.1:5000/admin/login")
    public = _env("WATCHDOG_PUBLIC_URL", "")
    key = (local, public)
    with _WEB_HEALTH_LOCK:
        cached = _WEB_HEALTH_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        # 未配置公网 URL 时跳过公网探测（公开部署缺配不该天天误报）
        pub = _web_probe(public) if public else {"ok": True, "status": 0, "skipped": True}
        report = dict(local=_web_probe(local), public=pub, checked=time.time(),
                      scope="server_network_only")
        _WEB_HEALTH_CACHE.clear()
        _WEB_HEALTH_CACHE[key] = (time.monotonic(), report)
        return report


def _probe_extra():
    """读探针结果，补充 watchdogs 自己看不见的细节。

    探针每跑一次会写 data/probe/state.json，里面记了：
    shm 用量、Chromium 崩溃次数、多余账号、掉线原因。
    这些正是「掉线」的真实根因，watchdog 自己从 docker inspect 拿不到。
    """
    try:
        import json as _j
        f = Path(os.getenv("DATA_DIR", "./data")) / "probe" / "state.json"
        if f.is_file():
            return _j.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def collect_issues(web=None):
    """返回 [(级别, 代码, 描述, 能否自愈)]"""
    issues = []

    if not _service_active("qqbot"):
        issues.append(("error", "qqbot_down", "qqbot 服务未运行", True))
    else:
        if not _port_open(3001):
            issues.append(("warn", "napcat_down",
                           "NapCat 的 3001 端口没监听（QQ 掉线或 NapCat 挂了）", False))
        else:
            ok, _ = _bot_connected_recently()
            if not ok:
                issues.append(("warn", "bot_disconnected",
                               "bot 与 NapCat 的连接断了（正在重连）", False))

    web = _web_health() if web is None else web
    if not web["local"]["ok"]:
        issues.append(("error", "web_unhealthy",
                       "后台源站不可用（HTTP %s），需核对 Web 进程" % web["local"]["status"], False))
    elif not web["public"]["ok"]:
        issues.append(("error", "public_gateway",
                       "源站正常但公网入口失败（HTTP %s，%s）；注册连接不等于公网可用"
                       % (web["public"]["status"], web["public"].get("edge") or "无边缘回执"), False))

    # ── 以下来自探针：这三样是「反复掉线」的真实根因 ──
    pr = _probe_extra()

    # ① shm 太小 → Chromium 网络服务会崩 → 登录被拽掉
    shm = str(pr.get("shm_used") or "")
    try:
        if "/" in shm:
            total_kb = int(shm.split("/")[1].strip().split()[0])
            if total_kb < 256 * 1024:
                issues.append(("error", "shm_small",
                               "容器 /dev/shm 只有 %d MB，Chromium 会崩（应 ≥512MB）"
                               % (total_kb // 1024), False))
    except Exception:
        pass

    # ② 多余账号 → 同进程多号，一个掉连累另一个
    extra = pr.get("extra_accounts") or []
    if extra:
        issues.append(("error", "extra_account",
                       "容器里登录了多余 QQ 号 %s，会互相顶号（只该有 bot 自己的号）"
                       % ",".join(extra), False))

    # ③ 真正的掉线原因（KickedOffLine 原话归类）
    # 以前把 Chromium 崩溃报成"掉线的直接原因"，那是错的：
    # 崩溃只发生在 Worker 重启的时候，是结果不是原因。真正的原因在 KickedOffLine。
    code = str(pr.get("reason_code") or "")
    desc = str(pr.get("reason_desc") or "")
    if code and code != "unknown" and str(pr.get("state") or "") in ("offline", "waiting_scan"):
        if code == "kicked_elsewhere":
            issues.append(("error", "kicked_elsewhere", desc, False))
        elif code == "session_expired":
            issues.append(("warn", "session_expired", desc, False))
        elif code == "account_locked":
            issues.append(("error", "account_locked", desc, False))

    # ③b 频繁掉线：一小时内被踢 ≥2 次 → 基本是风控或设备冲突，不是网络抖动
    try:
        kh = pr.get("kick_history") or []
        n1h = int(pr.get("kicks_last_hour") or 0)
        if n1h >= 2:
            _last = kh[-1] if kh else {}
            issues.append(("warn", "frequent_kicks",
                           "一小时内被踢 %d 次（最近 %s：%s）——像风控或设备冲突"
                           % (n1h, _last.get("time", "?"),
                              (_last.get("reason") or "")[:40]), False))
    except Exception:
        pass

    # ④ Chromium 崩溃：只在没被踢的情况下才当作可疑信号
    try:
        cr = int(pr.get("crashes") or 0)
        # 在线时崩溃次数只是历史累计（每次 Worker 重启都会 +1），不该报警
        if cr > 0 and code in ("", "unknown") and str(pr.get("state") or "") != "online":
            issues.append(("warn", "chromium_crash",
                           "Chromium 网络服务重启过 %d 次（通常是掉线后的副作用，不是原因）" % cr,
                           False))
    except Exception:
        pass

    # ⑤ 容器被重建（会导致快速登录失效，必须重新扫码）
    if pr.get("container_recreated"):
        issues.append(("error", "container_recreated",
                       "NapCat 容器被重建过，QQ 视为新设备，快速登录失效（用 napcat-restart 只重启不重建）",
                       False))

    # ⑥ 掉线时长（超过 10 分钟才报警，避免抖动刷屏）
    try:
        if str(pr.get("state") or "") in ("offline", "waiting_scan"):
            since = float(pr.get("offline_since") or 0)
            if since and (now_ts() - since) > 600:
                mins = int((now_ts() - since) / 60)
                issues.append(("error", "qq_offline",
                               "QQ 已离线 %d 分钟（%s）"
                               % (mins, pr.get("reason") or "需扫码登录"), False))
    except Exception:
        pass

    return issues


def now_ts():
    return time.time()


def _notify(title, body, detail=""):
    """发到所有已配置的渠道。返回是否至少发出去一个。

    自动识别 URL 类型，用对应格式：
      · sctapi.ftqq.com      Server酱（微信）
      · pushplus             PushPlus（微信）
      · qyapi.weixin.qq.com  企业微信群机器人
      · oapi.dingtalk.com    钉钉群机器人
      · open.feishu.cn       飞书群机器人
      · 其他                通用 JSON {"title":..., "content":...}
    """
    ok = False
    text = "%s\n\n%s\n\n%s" % (title, body, detail)

    url = _env("WATCHDOG_WEBHOOK_URL")
    if url:
        try:
            low = url.lower()
            headers = {"Content-Type": "application/json"}
            if "sctapi.ftqq.com" in low or "server.ftqq" in low:
                # Server酱：POST form
                import urllib.parse
                data = urllib.parse.urlencode({
                    "title": title, "desp": body + "\n\n" + detail}).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif "pushplus" in low:
                data = json.dumps({"token": _env("WATCHDOG_PUSHPLUS_TOKEN"),
                                   "title": title,
                                   "content": body + "<br><br>" + detail},
                                  ensure_ascii=False).encode()
            elif "qyapi.weixin" in low or "dingtalk" in low or "feishu" in low:
                # 企业微信/钉钉/飞书都用 msgtype+text.content；钉钉要 at 字段
                payload = {"msgtype": "text", "text": {"content": text}}
                if "dingtalk" in low:
                    payload["at"] = {"isAtAll": True}
                data = json.dumps(payload, ensure_ascii=False).encode()
            else:
                data = json.dumps({"title": title,
                                   "content": body + "\n\n" + detail},
                                  ensure_ascii=False).encode()
            req = urllib.request.Request(url, data=data, headers=headers)
            urllib.request.urlopen(req, timeout=15)
            ok = True
        except Exception as e:
            print("[watchdog] webhook 失败 %s: %s"
                  % (type(e).__name__, str(e)[:80]), flush=True)

    # ② 邮件
    to = _env("WATCHDOG_EMAIL_TO")
    host = _env("WATCHDOG_SMTP_HOST")
    user = _env("WATCHDOG_SMTP_USER")
    pwd = _env("WATCHDOG_SMTP_PASS")
    if to and host and user and pwd:
        try:
            port = int(_env("WATCHDOG_SMTP_PORT", "465"))
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = title
            msg["From"] = user
            msg["To"] = to
            if port == 465:
                srv = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                srv = smtplib.SMTP(host, port, timeout=20)
                srv.starttls()
            srv.login(user, pwd)
            srv.send_message(msg)
            srv.quit()
            ok = True
        except Exception as e:
            print("[watchdog] 邮件失败 %s: %s"
                  % (type(e).__name__, str(e)[:80]), flush=True)

    return ok



def heal(issues):
    """能自愈的就地修，返回修了哪些。"""
    fixed = []
    codes = {c for _l, c, _d, can in issues if can}
    if "qqbot_down" in codes:
        try:
            subprocess.run(["systemctl", "restart", "qqbot"], timeout=90)
            fixed.append("重启 qqbot")
        except Exception:
            pass
    return fixed


def run_once():
    st = _load_state()
    issues = collect_issues()
    now = time.time()
    cur = sorted({c for _l, c, _d, _f in issues})

    prev = st.get("issues") or []
    changed = cur != prev

    lines = []
    for lvl, code, desc, can in issues:
        lines.append("%s %s：%s%s" % (
            "✗" if lvl == "error" else "!", code, desc,
            "（可自动恢复）" if can else "（需人工处理）"))

    if issues and changed:
        # 新故障：先尝试自愈
        fixed = heal([i for i in issues if i[3]])
        if fixed:
            # 自愈完再看一遍，确认是否真的好了
            time.sleep(8)
            with _WEB_HEALTH_LOCK:
                _WEB_HEALTH_CACHE.clear()
            issues2 = collect_issues()
            if not issues2:
                _notify("【清漪】已自动恢复",
                        "检测到故障并自动修复：" + "、".join(fixed),
                        "修复后复查：一切正常")
                st.update({"status": "ok", "issues": [], "since": 0,
                           "last_push": now})
                st["log"] = (st.get("log") or [])[-50:] + [
                    {"ts": now, "event": "自愈成功", "detail": "、".join(fixed)}]
                _save_state(st)
                print("[watchdog] 自愈成功：%s" % "、".join(fixed), flush=True)
                return
            issues = issues2
            cur = sorted({c for _l, c, _d, _f in issues})

        body = "\n".join(lines)
        sent = _notify("【清漪】掉线了", "检测到 %d 个问题：" % len(issues), body)
        st.update({"status": "bad", "issues": cur, "since": now, "last_push": now})
        st["log"] = (st.get("log") or [])[-50:] + [
            {"ts": now, "event": "故障", "detail": "；".join(cur),
             "pushed": sent}]
        _save_state(st)
        print("[watchdog] 报警（%s）：%s" % ("已推送" if sent else "无渠道", "；".join(cur)),
              flush=True)
        return

    if not issues and prev:
        _notify("【清漪】恢复了", "之前的故障已消失，一切正常。",
                "之前的问题：" + "；".join(prev))
        st.update({"status": "ok", "issues": [], "since": 0, "last_push": now})
        st["log"] = (st.get("log") or [])[-50:] + [
            {"ts": now, "event": "恢复", "detail": "；".join(prev)}]
        _save_state(st)
        print("[watchdog] 已恢复", flush=True)
        return

    # 长期故障：定期提醒，别每轮都推
    if issues and now - float(st.get("last_push") or 0) > REMIND_EVERY:
        sent = _notify("【清漪】仍未恢复（已持续 %d 分钟）" %
                       int((now - float(st.get("since") or now)) / 60),
                       "问题还在：", "\n".join(lines))
        st["last_push"] = now
        st["log"] = (st.get("log") or [])[-50:] + [
            {"ts": now, "event": "持续提醒", "pushed": sent}]
        _save_state(st)
        print("[watchdog] 持续提醒已发", flush=True)
    else:
        _save_state(st)


def status():
    st = _load_state()
    web = _web_health()
    issues = collect_issues(web)
    return {
        "status": "ok" if not issues else "bad",
        "web": web,
        "issues": [{"level": l, "code": c, "desc": d,
                    "auto_fix": f} for l, c, d, f in issues],
        "checklist": [
            {"name": "qqbot 服务", "ok": _service_active("qqbot")},
            {"name": "NapCat 3001", "ok": _port_open(3001)},
            {"name": "bot ↔ NapCat", "ok": _bot_connected_recently()[0]},
            {"name": "后台源站 HTTP", "ok": web["local"]["ok"]},
            {"name": "公网入口（服务器线路）", "ok": web["public"]["ok"]},
        ],
        "since": st.get("since"),
        "last_push": st.get("last_push"),
        "history": (st.get("log") or [])[-10:],
        "env_source": _ENV_SOURCE,
        "channels": {
            "webhook": bool(_env("WATCHDOG_WEBHOOK_URL")),
            "email": bool(_env("WATCHDOG_EMAIL_TO") and _env("WATCHDOG_SMTP_HOST")),
        },
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
    else:
        run_once()
