# -*- coding: utf-8 -*-
"""QQ 运维通道：主人用 /ops 私聊直接指挥后台自动驾驶和 维护端。

这是**独立模块**，不是清漪的人格链路。三条隔离边界：

1. 拦截点在 `handle_event` 的私聊分支里、`handle_private_message` 之前，
   所以运维指令不会触发 `_record_private_observation`，不写 self_model /
   trigger_ledger / proactive_dm —— 她不会把「/ops pause」当成主人对她说的话。
2. 发话走 `napcat_channel.channel.call`，不走 `send_private_msg`：后者会做
   语气词裁剪、气泡拆分和好感自报，那是她的人格表达，运维报文不该被它改写。
3. 运维文本一律不进她的记忆/语料/知识库，只落 data/ops/ 自己的账。

沉默的 worker 视为故障：每道关闭的门都打印原因，outbox 送不出去会留账重投。
"""
import asyncio
import json
import os
import re
import time
import uuid

import safe_store

# 前缀必须显式，避免把正常私聊误判成运维指令。
PREFIXES = ("/ops", "#ops", "/运维")
CHUNK = 1000          # 单条 QQ 文本上限，长了拆条，别被网关截断
DRAIN_MAX = 6         # 每轮最多发几条，避免刷屏
DRAIN_GAP = 1.5       # 条间隔秒

# 任务号之间的分隔：主人手机上打逗号经常是中文全角，还可能是顿号/分号。
_ID_SPLIT = re.compile(r"[\s,，、;；/]+")
_ID_RE = re.compile(r"^[A-Za-z]{1,3}\d{1,3}$")


def split_ids(arg):
    """把「M01，S01 先做这两个」拆成 (['M01','S01'], '先做这两个')。

    第一版只按 ASCII 空格拆一次：`/ops approve M01，S01` 整串被当成一个
    不存在的任务号，主人的批准静默丢了；`/ops approve M01 S01` 更糟 ——
    S01 被当成备注，只批了 M01，回执还说成功。两种都只靠回车发现不了。
    """
    ids, rest = [], []
    for token in _ID_SPLIT.split(str(arg or "").strip()):
        if not token:
            continue
        if not rest and _ID_RE.match(token):
            ids.append(token.upper())
        else:
            rest.append(token)
    return ids, " ".join(rest)


def _one_id(value):
    """单个任务号的兜底归一化：远端调用可能带着尾随分隔符进来。"""
    tokens = [t for t in _ID_SPLIT.split(str(value or "").strip()) if t]
    return tokens[0].upper() if tokens else ""


_QUESTION = re.compile(r"(吗|呢|？|\?|是不是|有没有|了吗|在做|是否|多久|什么时候)")


def is_question(text):
    """问句只该被回答，不该被当成任务去做。

    主人问「现在是在做M05吗」是询问，不是下令开工；以前一律排队执行，
    维护端 可能真去动 M05。这里打个 kind=question 标记，分发器只读回答。
    """
    t = str(text or "").strip()
    return bool(t) and bool(_QUESTION.search(t))


def _dir():
    """调用时解析 DATA_DIR：模块顶层冻路径会让测试写脏真实 data/。"""
    return os.path.join(os.getenv("DATA_DIR", "./data"), "ops")


def _state_path():
    return os.path.join(_dir(), "state.json")


def _inbox_path():
    return os.path.join(_dir(), "inbox.jsonl")


def _outbox_path():
    return os.path.join(_dir(), "outbox.jsonl")


def _health_path():
    return os.path.join(_dir(), "health.json")


def rounds_dir():
    return os.path.join(_dir(), "rounds")


def enabled():
    return os.getenv("OPS_CHANNEL_ENABLED", "1").strip() != "0"


def owner_ids():
    raw = os.getenv("MASTER_QQ_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def round_hours():
    """巡检轮次的小时点，只用来告诉主人「什么时候会被处理」。

    维护端 没有无头 CLI，只能靠定时任务去取队列。为了不为了等一条指令
    单独开轮询会话烧 token，指令搭巡检的车 —— 这个字符串就是主人看到的
    等待上限。改巡检时间时同步 OPS_ROUND_HOURS（或这里的默认值），
    否则回执会对着一个已经不存在的班次许诺。
    """
    raw = os.getenv("OPS_ROUND_HOURS", "1,4,8,14,18,22").strip()
    if raw.lower() in ("hourly", "每小时", "*", "all"):
        return list(range(24))
    hours = []
    for piece in raw.replace("，", ",").split(","):
        piece = piece.strip()
        if piece.isdigit() and 0 <= int(piece) <= 23:
            hours.append(int(piece))
    return sorted(set(hours)) or [1, 4, 8, 14, 18, 22]


def round_when_text():
    hours = round_hours()
    if len(hours) == 24:
        return "每小时整点"
    return "每天 %s 点" % "/".join(str(h) for h in hours)


def extract_text(event):
    """只取纯文本，不依赖 bridge 的 parse_message（那会把图片/语音也带进来）。"""
    msg = event.get("message")
    if isinstance(msg, str):
        return (event.get("raw_message") or msg).strip()
    parts = []
    for seg in msg or []:
        if isinstance(seg, dict) and seg.get("type") == "text":
            parts.append(str((seg.get("data") or {}).get("text") or ""))
    return "".join(parts).strip()


def match_command(event):
    """返回 (命令词, 余下参数) 或 None。只认主人的私聊显式前缀。"""
    if not enabled():
        return None
    if event.get("post_type") != "message" or event.get("message_type") != "private":
        return None
    if str(event.get("user_id", "")) not in owner_ids():
        return None
    text = extract_text(event)
    low = text.lower()
    for prefix in PREFIXES:
        if low == prefix:
            return ("help", "")
        if low.startswith(prefix + " ") or low.startswith(prefix + "\n"):
            rest = text[len(prefix):].strip()
            head, _, tail = rest.partition(" ")
            return (head.lower(), tail.strip())
    return None


# ---------------------------------------------------------------- 账本

def _read_jsonl(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return rows
    return rows


def _write_jsonl(path, rows):
    """整表重写：这些账很小（几十行），原子替换比追加更能保证不半行。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        print("[运维] 账写盘失败 " + type(e).__name__, flush=True)
        return False


def default_state():
    return {
        "enabled": True,
        "paused_reason": "",
        "paused_at": "",
        "approvals": {},
        "waiting": {},
        "last_round": {},
        "counters": {"inbound": 0, "outbound": 0, "maint_jobs": 0},
    }


def load_state():
    path = _state_path()
    if not os.path.isfile(path):
        return default_state()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        safe_store.preserve_corrupt(path)
        return default_state()
    if not isinstance(data, dict):
        safe_store.preserve_corrupt(path)
        return default_state()
    base = default_state()
    base.update(data)
    for key in ("approvals", "waiting", "counters", "last_round"):
        if not isinstance(base.get(key), dict):
            base[key] = {}
    return base


def save_state(state):
    return safe_store.atomic_write_json(_state_path(), state)


def autopilot_enabled():
    """自动驾驶开关。暂停只停推进，不停观测和通知。"""
    return bool(load_state().get("enabled", True))


def set_autopilot_enabled(flag, reason="", by=""):
    state = load_state()
    state["enabled"] = bool(flag)
    state["paused_reason"] = "" if flag else str(reason)[:200]
    state["paused_at"] = "" if flag else _now()
    state["paused_by"] = "" if flag else str(by)
    save_state(state)
    return state


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def enqueue_inbox(text, source="qq", meta=None):
    """需要 维护端 判断的指令进 inbox，由本地分发任务取走。"""
    row = {"id": uuid.uuid4().hex[:12], "at": _now(), "text": str(text)[:4000],
           "source": source, "state": "pending", "meta": meta or {}}
    rows = _read_jsonl(_inbox_path())
    rows.append(row)
    if _write_jsonl(_inbox_path(), rows):
        state = load_state()
        state["counters"]["maint_jobs"] = int(state["counters"].get("maint_jobs", 0)) + 1
        save_state(state)
        return row["id"]
    return ""


def pending_inbox():
    return [r for r in _read_jsonl(_inbox_path()) if r.get("state") == "pending"]


def claim_inbox(ids, owner):
    rows = _read_jsonl(_inbox_path())
    claimed = []
    wanted = set(ids)
    for row in rows:
        if row.get("id") in wanted and row.get("state") == "pending":
            row["state"] = "claimed"
            row["claimed_by"] = owner
            row["claimed_at"] = _now()
            claimed.append(row["id"])
    _write_jsonl(_inbox_path(), rows)
    return claimed


def finish_inbox(ids, result="done", note=""):
    rows = _read_jsonl(_inbox_path())
    wanted = set(ids)
    for row in rows:
        if row.get("id") in wanted:
            row["state"] = result
            row["finished_at"] = _now()
            row["note"] = str(note)[:500]
    _write_jsonl(_inbox_path(), rows)


def enqueue_outbox(text, kind="notify"):
    """自动驾驶在本地/云端写这里，bot 的 worker 负责真的发出去。

    这样 维护端 侧不需要持有 ws 句柄 —— 主动开口的场景恰恰是没人找她的时候。
    """
    text = str(text).strip()
    if not text:
        return ""
    row = {"id": uuid.uuid4().hex[:12], "at": _now(), "text": text[:8000],
           "kind": kind, "state": "pending", "tries": 0}
    rows = _read_jsonl(_outbox_path())
    rows.append(row)
    if _write_jsonl(_outbox_path(), rows):
        return row["id"]
    return ""


def pending_outbox():
    return [r for r in _read_jsonl(_outbox_path()) if r.get("state") == "pending"]


def _mark_outbox(ids, state, note=""):
    rows = _read_jsonl(_outbox_path())
    wanted = set(ids)
    for row in rows:
        if row.get("id") in wanted:
            row["state"] = state
            row["tries"] = int(row.get("tries", 0)) + 1
            row["last_try"] = _now()
            if note:
                row["note"] = str(note)[:200]
    _write_jsonl(_outbox_path(), rows)


def record_decision(task_id, decision, note="", by=""):
    """人工授权只按任务粒度记，绝不放开 stage3/表达接管这种整片开关。"""
    import qingyi_roadmap
    known = {t["id"]: t for t in qingyi_roadmap.catalog()}
    task_id = _one_id(task_id)
    if task_id not in known:
        return False, "目录里没有这个任务号：%s" % task_id[:20]
    if decision not in ("approve", "reject"):
        return False, "只认 approve / reject"
    state = load_state()
    state["approvals"][task_id] = {"decision": decision, "note": str(note)[:300],
                                  "at": _now(), "by": str(by)}
    save_state(state)
    return True, "%s 已记为 %s" % (task_id, decision)


def mark_waiting(task_id, what, by=""):
    state = load_state()
    state["waiting"][_one_id(task_id)] = {
        "what": str(what)[:300], "since": _now(), "by": str(by)}
    save_state(state)


def stage3_signoff():
    """主人对阶段3/表达接管的人工签字记录（只读）。"""
    return load_state().get("stage3_signoff") or {}


def record_stage3_signoff(granted, by="", note=""):
    """记录主人本人对灰度接管的签字。只记账，不翻 expression_control。

    表达接管链路（C06）尚未接线，静态锁 expression_control 恒 False；
    此签字是将来 C06 接通后必需的人工授权凭证，AI 不得代写。
    """
    state = load_state()
    state["stage3_signoff"] = {"granted": bool(granted), "by": str(by),
                               "note": str(note)[:300], "at": _now()}
    save_state(state)
    try:  # 签字/撤回立即生效，不等灰度门缓存过期
        import expression_gate
        expression_gate.invalidate()
    except Exception:
        pass
    return True, ("已记录你的接管授权签字" if granted else "已撤回接管授权签字")


def clear_waiting(task_id):
    state = load_state()
    state["waiting"].pop(_one_id(task_id), None)
    save_state(state)


def create_challenge(description, ttl_seconds=900):
    """破坏性操作先挂一个令牌，主人 /ops confirm 令牌 才放行。

    自动驾驶本身永远不做破坏性操作；这道门是给主人临时下达的指令用的，
    避免一句歧义的话就把数据删了。令牌一次性，过期作废。
    """
    token = uuid.uuid4().hex[:8]
    state = load_state()
    challenges = state.setdefault("challenges", {})
    now = time.time()
    for old in [k for k, v in challenges.items() if v.get("expires", 0) < now]:
        challenges.pop(old, None)
    challenges[token] = {"description": str(description)[:300], "created": _now(),
                         "expires": now + ttl_seconds, "used": False}
    save_state(state)
    return token


def confirm_challenge(token):
    state = load_state()
    challenges = state.get("challenges") or {}
    row = challenges.get(str(token).strip())
    if not row:
        return False, "没有这个令牌，或它已过期"
    if row.get("used"):
        return False, "这个令牌已经用过了"
    if row.get("expires", 0) < time.time():
        challenges.pop(str(token).strip(), None)
        save_state(state)
        return False, "令牌过期了，重新下指令"
    row["used"] = True
    row["used_at"] = _now()
    save_state(state)
    return True, row.get("description", "")


def pending_challenges():
    now = time.time()
    return {k: v for k, v in (load_state().get("challenges") or {}).items()
            if not v.get("used") and v.get("expires", 0) >= now}


def save_round(report):
    """自动驾驶每轮的结论落盘，/ops report 直接读它，不用重新扫云端。"""
    rid = str(report.get("id") or uuid.uuid4().hex[:12])
    os.makedirs(rounds_dir(), exist_ok=True)
    safe_store.atomic_write_json(os.path.join(rounds_dir(), rid + ".json"), report)
    state = load_state()
    state["last_round"] = {"id": rid, "at": report.get("at") or _now(),
                           "summary": str(report.get("summary", ""))[:600],
                           "advanced": list(report.get("advanced", []))[:20],
                           "waiting_on": list(report.get("waiting_on", []))[:20],
                           "needs_approval": list(report.get("needs_approval", []))[:20]}
    save_state(state)
    return rid


def save_health(snapshot):
    safe_store.atomic_write_json(_health_path(), snapshot)


def load_health():
    path = _health_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- 发话

async def send_text(ws, text, user_id=None):
    """裸 OneBot 发送。返回 True/False，失败出声。"""
    import napcat_channel
    target = str(user_id or os.getenv("OPS_OWNER_QQ", "") or "").strip()
    if not target:
        owners = sorted(owner_ids())
        target = owners[0] if owners else ""
    if not target:
        print("[运维] 没有主人 QQ 号，报文丢弃", flush=True)
        return False
    if not napcat_channel.channel.connected:
        print("[运维] OneBot 通道未绑定，报文留账重投", flush=True)
        return False
    ok = True
    for i in range(0, len(text), CHUNK):
        piece = text[i:i + CHUNK]
        resp = await napcat_channel.channel.call(
            "send_private_msg", {"user_id": int(target), "message": piece}, timeout=20.0)
        if not isinstance(resp, dict) or resp.get("status") not in ("ok", "async"):
            print("[运维] 发送失败 status=%s" % str((resp or {}).get("status"))[:40], flush=True)
            ok = False
            break
        if i + CHUNK < len(text):
            await asyncio.sleep(DRAIN_GAP)
    return ok


async def outbox_worker(ws=None):
    """把 outbox 里待发的运维报文真的送出去。独立于任何来话。"""
    interval = float(os.getenv("OPS_OUTBOX_INTERVAL", "20"))
    print("[运维] outbox worker 已启动 interval=%.0fs" % interval, flush=True)
    while True:
        try:
            rows = pending_outbox()
            if not rows:
                await asyncio.sleep(interval)
                continue
            sent, failed = [], []
            for row in rows[:DRAIN_MAX]:
                ok = await send_text(ws, row.get("text", ""))
                (sent if ok else failed).append(row["id"])
                if ok:
                    await asyncio.sleep(DRAIN_GAP)
            if sent:
                _mark_outbox(sent, "sent")
                state = load_state()
                state["counters"]["outbound"] = int(state["counters"].get("outbound", 0)) + len(sent)
                save_state(state)
            if failed:
                # 送不出去就留账下一轮重投；超过 5 次标死信，别无限刷。
                rows_now = {r["id"]: r for r in _read_jsonl(_outbox_path())}
                dead = [i for i in failed if int(rows_now.get(i, {}).get("tries", 0)) >= 5]
                retry = [i for i in failed if i not in dead]
                if retry:
                    _mark_outbox(retry, "pending", "retry")
                if dead:
                    _mark_outbox(dead, "dead", "gave_up")
                    print("[运维] %d 条报文重投 5 次仍失败，标死信" % len(dead), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[运维] outbox worker 异常 " + type(e).__name__ + ": " + str(e)[:120], flush=True)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- 命令

HELP = (
    "【运维】可用指令（只认主人私聊）\n"
    "/ops status 服务器与自动驾驶现状\n"
    "/ops plan 当前可推进/待批/等待的任务\n"
    "/ops report 上一轮自动驾驶结论\n"
    "/ops pause 原因 / /ops resume 暂停或恢复自动推进\n"
    "/ops approve 任务号 备注 / /ops reject 任务号 备注\n"
    "/ops wait 任务号 缺什么数据\n"
    "/ops inbox 待 维护端 处理的指令\n"
    "/ops cancel 指令号 撤回一条排队的指令（/ops inbox 看号）\n"
    "/ops confirm 令牌 放行一次破坏性操作\n"
    "问句（…吗/？/是不是）只会被回答，不会当成任务去做；\n"
    "其它内容直接交给 维护端 执行，例如 /ops 把 M01 的准入契约写了"
)


def _fmt_status():
    state = load_state()
    health = load_health()
    lines = ["【运维】自动驾驶：%s" % ("运行中" if state.get("enabled") else "已暂停")]
    if not state.get("enabled"):
        lines.append("暂停原因：" + str(state.get("paused_reason") or "未填"))
    last = state.get("last_round") or {}
    if last:
        lines.append("上一轮 %s：%s" % (last.get("at", "")[:16], last.get("summary", "")[:200]))
        if last.get("advanced"):
            lines.append("已推进：" + ", ".join(last["advanced"]))
    else:
        lines.append("还没有跑过任何一轮")
    if health:
        lines.append("云端 %s：qqbot=%s napcat=%s 磁盘余量=%sGB" % (
            str(health.get("at", ""))[:16], health.get("qqbot", "?"),
            health.get("napcat", "?"), health.get("disk_free_gb", "?")))
        if health.get("issues"):
            lines.append("异常：" + "; ".join(str(x) for x in health["issues"][:4]))
    else:
        lines.append("云端健康快照还没被自动驾驶写过")
    lines.append("待 维护端 指令 %d 条，待发报文 %d 条" % (len(pending_inbox()), len(pending_outbox())))
    if state.get("waiting"):
        lines.append("等待中：" + ", ".join(sorted(state["waiting"])))
    return "\n".join(lines)


def plan_view(plan=None, state=None):
    """一份计划快照 + 当前授权账本 → 三端共用的分组视图。

    快照里的 auto_advance / needs_approval 是**算出来那一刻**的分桶。
    主人批完之后如果不重算，面板说"已可推进"、QQ 还列在"待批准"、
    维护端 看到的又是第三份 —— 三端各说各话。所以这个函数只做一件事：
    用最新的 approvals 把 frontier 重新分一遍。面板、/ops plan、
    分发器全部走这里，不许再各写一套。
    """
    if plan is None:
        try:
            with open(os.path.join(_dir(), "plan.json"), encoding="utf-8") as f:
                plan = json.load(f) or {}
        except (OSError, ValueError):
            plan = {}
    if state is None:
        state = load_state()
    approvals = state.get("approvals") or {}
    frontier = plan.get("frontier") or []
    doable, approval, rejected = [], [], []
    for row in frontier:
        tid = str(row.get("id") or "")
        if not tid:
            continue
        decision = (approvals.get(tid) or {}).get("decision")
        if decision == "reject":
            rejected.append(tid)
        elif row.get("kind") in ("auto", "approved") or decision == "approve":
            doable.append(tid)
        else:
            approval.append(tid)
    waiting = []
    for item in plan.get("waiting_on") or []:
        waiting.append("%s：%s" % (item.get("id", "?"), str(item.get("need", ""))[:60]))
    return {
        "at": plan.get("at", ""),
        "doable": doable,
        "approval": approval,
        "rejected": rejected,
        "waiting": waiting,
        "blocked": [b.get("id") for b in plan.get("blocked") or []],
        "title": {str(r.get("id")): r.get("title", "") for r in frontier},
        "why": {str(r.get("id")): str(r.get("why", ""))[:90] for r in frontier},
    }


def _fmt_plan():
    """读最近一次 plan 快照，但按**当前**授权账本重新分桶再说话。"""
    path = os.path.join(_dir(), "plan.json")
    if not os.path.isfile(path):
        return "【运维】还没有 plan 快照，等下一轮自动驾驶，或 /ops 让它现在跑一轮"
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError):
        return "【运维】plan 快照读不动，等下一轮重写"
    view = plan_view(plan)
    lines = ["【运维】推进计划 %s" % str(view["at"])[:16]]
    if view["doable"]:
        lines.append("可推进：")
        for tid in view["doable"][:8]:
            lines.append("  %s %s" % (tid, view["title"].get(tid, "")))
    if view["approval"]:
        lines.append("待你批准（说 /ops approve 任务号）：")
        for tid in view["approval"][:8]:
            lines.append("  %s %s — %s" % (tid, view["title"].get(tid, ""),
                                          view["why"].get(tid, "")))
    if view["waiting"]:
        lines.append("等待数据：")
        for item in view["waiting"][:6]:
            lines.append("  " + item)
    if view["blocked"]:
        lines.append("被阻塞：" + "、".join(view["blocked"][:6]))
    if len(lines) == 1:
        lines.append("现在没有可推进项，也没有待批；前沿是空的")
    return "\n".join(lines)


def _fmt_report():
    state = load_state()
    last = state.get("last_round") or {}
    if not last.get("id"):
        return "【运维】还没有轮次报告"
    path = os.path.join(rounds_dir(), last["id"] + ".json")
    if not os.path.isfile(path):
        return "【运维】上一轮 %s：%s" % (last.get("at", "")[:16], last.get("summary", ""))
    try:
        with open(path, encoding="utf-8") as f:
            rep = json.load(f)
    except (OSError, ValueError):
        return "【运维】报告文件读不动：%s" % last["id"]
    lines = ["【运维】轮次 %s %s" % (rep.get("id", ""), str(rep.get("at", ""))[:16])]
    lines.append(str(rep.get("summary", ""))[:400])
    for key, label in (("advanced", "本轮推进"), ("fixes", "修掉的阻塞"),
                       ("waiting_on", "等待数据"), ("needs_approval", "待批准"),
                       ("failures", "失败")):
        items = rep.get(key) or []
        if items:
            lines.append(label + "：" + ", ".join(str(x) for x in items[:10]))
    return "\n".join(lines)


def handle_local(cmd, arg, user_id):
    """能就地答的就地答。返回 (回复文本, 是否已处理)。"""
    if cmd in ("help", "", "?", "帮助"):
        return HELP, True
    if cmd in ("status", "状态"):
        return _fmt_status(), True
    if cmd in ("plan", "计划"):
        return _fmt_plan(), True
    if cmd in ("report", "报告"):
        return _fmt_report(), True
    if cmd in ("pause", "暂停"):
        set_autopilot_enabled(False, arg or "主人手动暂停", by=user_id)
        return "【运维】自动推进已暂停，观测和通知照常。恢复：/ops resume", True
    if cmd in ("resume", "恢复"):
        set_autopilot_enabled(True, by=user_id)
        return "【运维】自动推进已恢复，下一轮按前置继续", True
    if cmd in ("approve", "批准", "reject", "否决"):
        decision = "approve" if cmd in ("approve", "批准") else "reject"
        ids, note = split_ids(arg)
        if not ids:
            return "【运维】用法：/ops approve M01，S02 备注（一次可以批多个）", True
        lines = []
        for tid in ids:
            _, msg = record_decision(tid, decision, note, by=user_id)
            lines.append(msg)
        return "【运维】" + "\n".join(lines), True
    if cmd in ("wait", "等待"):
        ids, reason = split_ids(arg)
        if not ids:
            return "【运维】用法：/ops wait 任务号 缺什么数据", True
        for tid in ids:
            mark_waiting(tid, reason or "未说明", by=user_id)
        return "【运维】已记 %s 为等待数据，自动驾驶不再反复催它" % "、".join(ids), True
    if cmd in ("confirm", "确认"):
        tokens = [t for t in _ID_SPLIT.split(str(arg or "").strip()) if t]
        token = tokens[0] if tokens else ""
        ok, msg = confirm_challenge(token)
        if ok:
            return "【运维】令牌已放行：%s\n维护端 下一轮取指令时会执行它" % msg, True
        return "【运维】" + msg, True
    if cmd in ("inbox", "队列"):
        rows = pending_inbox()
        if not rows:
            return "【运维】没有待 维护端 处理的指令", True
        return "【运维】待处理 %d 条：\n" % len(rows) + "\n".join(
            "%s %s" % (r["id"], r.get("text", "")[:80]) for r in rows[:8]), True
    if cmd in ("cancel", "撤回", "撤销", "revoke"):
        tokens = [t for t in _ID_SPLIT.split(str(arg or "").strip()) if t]
        if not tokens:
            return "【运维】用法：/ops cancel 指令号（先 /ops inbox 看号）", True
        finish_inbox(tokens, "cancelled", "主人撤回")
        return "【运维】已撤回：%s" % "、".join(tokens), True
    return "", False


async def intercept(ws, event):
    """在人格链路之前截住运维指令。返回 True 表示已消费，调用方必须直接 return。

    任何异常都不许冒泡到 handle_event：宁可回一句失败，也不能把运维指令
    漏进她的私聊记忆里去。
    """
    try:
        matched = match_command(event)
    except Exception as e:
        print("[运维] 指令识别异常 " + type(e).__name__, flush=True)
        return False
    if not matched:
        return False
    cmd, arg = matched
    user_id = str(event.get("user_id", ""))
    try:
        state = load_state()
        state["counters"]["inbound"] = int(state["counters"].get("inbound", 0)) + 1
        save_state(state)
        reply, done = handle_local(cmd, arg, user_id)
        if not done:
            # 交给 维护端：本地分发任务会取走并执行，结果走 outbox 回来。
            # 问句只回答不执行，避免把「是在做M05吗」当成开工指令。
            raw = extract_text(event)
            kind = "question" if is_question(raw) else "task"
            job = enqueue_inbox(raw, source="qq", meta={"cmd": cmd, "kind": kind})
            if job:
                if kind == "question":
                    reply = ("【运维】收到，这是个问题，我只回答不会动手。任务号 %s，"
                             "下一轮巡检时回你（%s）" % (job, round_when_text()))
                else:
                    reply = ("【运维】已排队，任务号 %s。维护端 下一次巡检时处理（%s），"
                             "处理完私聊回你；只看现状可以先 /ops status"
                             % (job, round_when_text()))
            else:
                reply = "【运维】指令落盘失败，这条没排上队，请重发"
        await send_text(ws, reply, user_id=user_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[运维] 指令处理异常 " + type(e).__name__ + ": " + str(e)[:160], flush=True)
        try:
            await send_text(ws, "【运维】这条指令处理失败了（%s），没动任何数据" % type(e).__name__,
                            user_id=user_id)
        except Exception:
            pass
    return True