"""群友个人档案：给每个群里的每个人建一份记忆，保证称呼与历史一致。

结构（data/memory/member_profiles.json）：
{
  "<group_id>": {
    "<user_id>": {
      "nick": "最近一次群昵称",
      "call": "已确定的称呼（优先使用）",
      "notes": "由 LLM 提炼的长期档案（简短）",
      "recent": ["最近说过的话", ...],   # 滚动保留，用于给模型即时上下文
      "count": 累计消息数,
      "last_ts": 最后活跃时间,
      "updated": 档案最后更新时间
    }
  }
}

设计取舍：
- 原文 recent 不做即时压缩（省 token），只在累计到阈值时调一次 LLM 提炼成 notes。
- call 一旦确定就固定，避免同一个人被叫成好几个称呼。
"""
import json
import os
import re
import time
from collections import deque

PROFILE_FILE = os.path.join(
    os.getenv("DATA_DIR", "./data"), "memory", "member_profiles.json"
)
RECENT_MAX = int(os.getenv("MEMBER_RECENT_MAX", "8"))
COMPACT_EVERY = int(os.getenv("MEMBER_COMPACT_EVERY", "15"))
NOTES_MAX = int(os.getenv("MEMBER_NOTES_MAX", "300"))
PROFILE_TTL = 3.0

_cache = {"data": None, "ts": 0.0}


def _load(force=False):
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < PROFILE_TTL:
        return _cache["data"]
    data = {}
    try:
        if os.path.isfile(PROFILE_FILE):
            with open(PROFILE_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception as e:
        print("[成员档案] 读取失败 " + str(e)[:80], flush=True)
        import safe_store
        safe_store.preserve_corrupt(PROFILE_FILE)
        data = {}
    _cache["data"] = data
    _cache["ts"] = now
    return data


def _save(data):
    import safe_store
    if safe_store.atomic_write_json(PROFILE_FILE, data):
        _cache["data"] = data
        _cache["ts"] = time.time()


def note(group_id, user_id, nickname="", text=""):
    """记录某人说的一句话。"""
    gid = str(group_id or "").strip()
    uid = str(user_id or "").strip()
    if not gid or not uid:
        return
    data = _load()
    g = data.setdefault(gid, {})
    p = g.setdefault(uid, {})
    if nickname:
        p["nick"] = str(nickname)[:40]
    txt = str(text or "").strip()
    if txt:
        recent = p.get("recent") or []
        recent.append(txt[:160])
        p["recent"] = recent[-RECENT_MAX:]
    p["count"] = int(p.get("count", 0)) + 1
    p["last_ts"] = time.time()
    _save(data)


def get(group_id, user_id):
    data = _load()
    return ((data.get(str(group_id)) or {}).get(str(user_id)) or {})


def set_call(group_id, user_id, call):
    """固定这个人的称呼。"""
    call = str(call or "").strip()[:20]
    if not call:
        return
    data = _load()
    g = data.setdefault(str(group_id), {})
    p = g.setdefault(str(user_id), {})
    p["call"] = call
    _save(data)


_NAME_LINE_RE = None


def scrub_notes(notes, others=None):
    """剔掉 notes 里的称呼污染。

    call 字段是唯一权威。notes 由 LLM 生成，可能：
      1. 直接写「称呼：阿甲」——和 call 打架；
      2. 引用别人的名字（「坏阿甲什么都发」）——把两个人搅在一起。
    两类都剔掉，宁少不混。
    """
    global _NAME_LINE_RE
    if _NAME_LINE_RE is None:
        _NAME_LINE_RE = re.compile(r"称呼|叫做|就叫|名字|外号|昵称|曾用名|又叫")
    bad_names = [str(x).strip() for x in (others or []) if str(x).strip()]
    out = []
    for line in str(notes or "").splitlines():
        if _NAME_LINE_RE.search(line):
            continue
        if any(n in line for n in bad_names):
            continue
        out.append(line)
    return "\n".join(out).strip()


def roster(group_id, limit=12):
    """返回该群已确定称呼的成员名册，用来钉死认知。"""
    data = _load().get(str(group_id)) or {}
    rows = []
    for uid, m in data.items():
        call = str((m or {}).get("call") or "").strip()
        if not call:
            continue
        nick = str((m or {}).get("nick") or "").strip()
        rows.append((call, uid, nick))
    rows.sort(key=lambda x: x[0])
    return rows[:limit]

def all_rosters_text(limit_groups=5):
    """所有群的固定称呼汇总，给跨群的任务（人格整理）用。"""
    data = _load()
    out = []
    for gid, mem in list(data.items())[:limit_groups]:
        rows = roster(gid, limit=20)
        if not rows:
            continue
        lines = []
        for _c, _u, _n in rows:
            ln = "- 「" + _c + "」"
            if _n:
                ln += "（群名片：" + _n + "）"
            lines.append(ln)
        out.append("群 " + str(gid) + "：\n" + "\n".join(lines))
    if not out:
        return ""
    return ("\n【群友固定称呼 · 写档案时一律用这些名字，不要用群名片】\n"
            + "\n".join(out) + "\n")

def set_relation(group_id, user_id, relation):
    """给某人标注特殊身份（如"主人的对象"），比称呼更强的约束。"""
    rel = str(relation or "").strip()[:120]
    data = _load()
    g = data.setdefault(str(group_id), {})
    p2 = g.setdefault(str(user_id), {})
    if rel:
        p2["relation"] = rel
    else:
        p2.pop("relation", None)
    _save(data)
    return rel


def relation_of(group_id, user_id):
    return str((get(group_id, user_id) or {}).get("relation") or "").strip()


def set_zodiac(group_id, user_id, zodiac):
    """固定某人的星座（本人说过的才记，不猜）。空值清除。"""
    z = str(zodiac or "").strip()[:12]
    data = _load()
    g = data.setdefault(str(group_id), {})
    p = g.setdefault(str(user_id), {})
    if z:
        p["zodiac"] = z
    else:
        p.pop("zodiac", None)
    _save(data)
    return z


def zodiac_of(group_id, user_id):
    return str((get(group_id, user_id) or {}).get("zodiac") or "").strip()


def label(group_id, user_id, nickname=""):
    """在提示词里显示某人的名字：有固定称呼就用固定称呼，否则用群昵称。

    群聊上下文原来直接塞群名片（「阿戊」），模型就会叫成「白子」。
    统一走这里，保证上下文里出现的名字和 call 一致。
    """
    call = str((get(group_id, user_id) or {}).get("call") or "").strip()
    if call:
        return call
    return str(nickname or "").strip() or str(user_id or "")

def context_for(group_id, user_id, max_chars=420):
    """给提示词用的该人档案摘要。"""
    p = get(group_id, user_id)
    if not p:
        return ""
    parts = []
    nick = str(p.get("nick") or "").strip()
    call = str(p.get("call") or "").strip()
    rel = str(p.get("relation") or "").strip()
    if rel:
        parts.append("【身份】" + rel)
    zodiac = str(p.get("zodiac") or "").strip()
    if zodiac:
        parts.append("【星座】" + zodiac + "（本人说过，别和别人的搞混）")
    if nick:
        parts.append("群昵称：" + nick)
    if call:
        parts.append("【唯一准确称呼】「" + call + "」。"
                     "除此之外任何地方出现的名字都不算数，不许叫成别的。")
    _others = [c for c, u, n in roster(group_id) if str(u) != str(user_id)]
    notes = scrub_notes(p.get("notes"), _others)
    if notes:
        parts.append("关于他的事（不含称呼）：\n" + notes)
    recent = p.get("recent") or []
    if recent:
        parts.append("他最近说过：" + " / ".join(recent[-4:]))
    if not parts:
        return ""
    out = "【你记得的这个群友】\n" + "\n".join(parts)
    out = out[:max_chars]
    # 档案按「人」记账：她记住了谁、这个人的档案被用过几次
    try:
        import memory_usage as _mu
        _mu.note("member", [_mu.key_member(group_id, user_id)],
                 query="", injected_chars=len(out), candidates=1,
                 fallback=not bool(notes), scene="group")
    except Exception:
        pass
    return out


def needs_compact(group_id, user_id):
    p = get(group_id, user_id)
    cnt = int(p.get("count", 0))
    last = float(p.get("updated", 0))
    return cnt > 0 and cnt % COMPACT_EVERY == 0 and last < time.time() - 60


def apply_notes(group_id, user_id, notes):
    data = _load()
    g = data.setdefault(str(group_id), {})
    p = g.setdefault(str(user_id), {})
    p["notes"] = str(notes or "").strip()[:NOTES_MAX]
    p["updated"] = time.time()
    _save(data)


def all_members(group_id):
    data = _load()
    return list((data.get(str(group_id)) or {}).keys())


async def compact_member(group_id, user_id, call_llm, bot_name="清漪"):
    """用一次 LLM 把该人的 recent + 旧 notes 提炼成档案。"""
    p = get(group_id, user_id)
    if not p:
        return False
    recent = " / ".join((p.get("recent") or [])[-RECENT_MAX:])
    old = str(p.get("notes") or "").strip()
    if not recent and not old:
        return False
    nick = str(p.get("nick") or "").strip()
    cur_call = str(p.get("call") or "").strip()
    prompt = (
        "你在给群友建个人档案。把旧档案和最近发言合并成一份更新后的档案。\n\n"
        "【第一行必须写成】称呼=xxx\n"
        "- 优先用别的群友实际叫他的那个名字（大家叫他白子、罐罐，就写白子、罐罐）。\n"
        "- 没有现成称呼时才从群昵称里取，不要自己编。\n"
        "- 如果已有固定称呼，保持不变。\n\n"
        "之后是档案正文：\n"
        "1. 只记长期有用的：身份、喜好、在做的事、约定、反复提到的梗。\n"
        "   ★ 绝对不要在正文里写称呼、名字、外号——称呼由系统的 call 字段单独管理，\n"
        "   你写进去会和真正的称呼打架，让人认错人。\n"
        "2. 删掉寒暄、情绪、重复。\n"
        "3. 不要编造。\n"
        "4. 中文，分条列，控制在 " + str(NOTES_MAX) + " 字内。\n"
        "5. 不要提模型、接口、档案器这些内部实现。\n\n"
        "他的群昵称：" + (nick if nick else "未知") + "\n"
        "当前已固定称呼：" + (cur_call if cur_call else "还没定") + "\n\n"
        "旧档案：\n" + (old if old else "暂无") + "\n\n"
        "最近发言：\n" + (recent if recent else "暂无") + "\n\n"
        "请输出："
    )
    try:
        res = await call_llm(prompt)
        res = (res or "").strip()
        if not res or len(res) < 4:
            return False
        # 把第一行 称呼=xxx 抓出来写进 call，正文进 notes
        call = ""
        body = res
        m = re.search(r"称呼\s*[=＝:：]\s*([^\n]{1,12})", res)
        if m:
            call = m.group(1).strip()
            for _ch in ("「", "」", "'", '"'):
                call = call.replace(_ch, "")
            call = call.strip()
            body = re.sub(r"^.*?称呼\s*[=＝:：][^\n]*\n?", "", res, count=1).strip()
        if call and not str(p.get("call") or "").strip():
            set_call(group_id, user_id, call)
            print("[成员档案] " + str(user_id) + " 称呼定为「" + call + "」", flush=True)
        apply_notes(group_id, user_id, body if body else res)
        print("[成员档案] " + str(user_id) + " 已更新 (" + str(len(res)) + " 字)", flush=True)
        return True
    except Exception as e:
        print("[成员档案] 提炼失败 " + type(e).__name__ + ": " + str(e)[:80], flush=True)
        return False
