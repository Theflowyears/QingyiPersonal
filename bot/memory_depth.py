"""分层记忆：让她分得清「刚刚」和「一直都是」。

原来的问题：每轮都把人格档案整块塞进去，等于把一辈子的记忆当成眼前的对话，
又重又糊——她既记不住刚说的话，也显不出那些真正长久的东西。

现在分三层：
  浅层 shallow —— 刚刚发生了什么（最近几轮、今天的情绪和话题），随对话快速滑动
  中层 middle  —— 最近这段时间的经历和事实，几周到几个月
  深层 deep    —— 她是谁（性格、说话调子、真正在意的人和事），几乎不变

按场合挑层：闲聊只要浅层，走心的话才把深层翻出来。
"""
import hashlib
import os
import re
import time


import json


IDENTITY_MAX_CHARS = int(os.getenv("IDENTITY_MAX_CHARS", "420"))

# 身份层进 system[0] 的唯一前提是「当天字节稳定」——
# 档案每小时整理都会变，直接读就会每轮把前缀缓存打断一次。
# 所以按天快照一次，当天恒定，隔天自然刷新。
_IDENT_SNAP = {"day": None, "text": None}


def _snap_path():
    root = os.getenv("PERSONA_DATA_DIR") or os.path.join(os.getenv("DATA_DIR", "./data"), "persona")
    return os.path.join(root, "identity_snapshot.json")


def identity_block():
    """她是谁：自我认知、说话调子、真正在意的人与事、关于主人的固定事实。

    这层以前只在消息命中「你是谁/还记得/喜欢 you」这类深层信号时才出现，
    实测 1306 条真实消息里 94.9% 落进浅层 —— 也就是说绝大多数轮次
    她的档案根本没进提示词（仪表里那一项叫 persona:starved）。
    档案是「她一直是什么样」，本来就不该看场合才想起来，所以挪到这里常驻。
    """
    day = time.strftime("%Y-%m-%d")
    path_ = _snap_path()
    import self_model
    token = self_model.cognition_cache_token()
    if (_IDENT_SNAP.get("day") == day and _IDENT_SNAP.get("path") == path_
            and _IDENT_SNAP.get('cognition_token') == token
            and _IDENT_SNAP.get("text") is not None):
        return _IDENT_SNAP["text"]
    try:
        if os.path.isfile(path_):
            with open(path_, encoding="utf-8") as f:
                data = json.load(f)
            if str(data.get("day") or "") == day and data.get('cognition_token') == token:
                txt = str(data.get("text") or "")
                _IDENT_SNAP.update({"day": day, "text": txt, "path": path_, 'cognition_token': token})
                return txt
    except Exception:
        pass
    txt = _deep_block_raw().strip()
    import self_model
    self_model.revise_layer('identity', {'text': txt}, 'identity snapshot boundary')
    if len(txt) > IDENTITY_MAX_CHARS:
        txt = txt[:IDENTITY_MAX_CHARS].rstrip() + "…"
    try:
        os.makedirs(os.path.dirname(path_), exist_ok=True)
        tmp = path_ + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"day": day, "text": txt, "saved": time.time(), 'cognition_token': token},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, path_)
    except Exception as e:
        print("[身份层] 快照写失败 " + str(e)[:60], flush=True)
    _IDENT_SNAP.update({"day": day, "text": txt, "path": path_, 'cognition_token': token})
    try:
        import memory_usage as _mu
        _mu.note("identity_system", [_mu.key_persona("self_view", txt[:60])],
                 query="", injected_chars=len(txt), scene="system0")
    except Exception:
        pass
    return txt


def _persona():
    try:
        import persona_growth
        return persona_growth.load()
    except Exception:
        return {}


def _journal_recent(n=8, session_key=""):
    """只取当前会话的真实对话；缺失不是跨会话补满的理由。"""
    try:
        import persona_growth
        import conversation_state
        rows = persona_growth.read_new_journal(0)
        if session_key:
            scope = conversation_state.normalize_session(session_key)
            mine = [r for r in rows
                    if conversation_state.normalize_session(r.get("session")) == scope]
            return mine[-n:]
        return rows[-n:]
    except Exception:
        return []


# ── 判断这一轮要挖多深 ──────────────────────────────
_DEEP_SIGNS = re.compile(
    r"你是谁|你是什么|你记得|还记得|以前|从前|小时候|那次|上次"
    r"|喜欢你|爱你|想你|难过|难受|不开心|开心|害怕|孤独|一个人"
    r"|为什么活着|有意义|意义|本质|感觉自己|你是谁啊"
    r"|生日|纪念|答应|承诺|一直|永远|以后|将来"
    r"|你妈|你爸|家人|女儿|爸爸|妈妈"
    r"|存在|什么样的|怎么想|心里|感受|真的吗|说实话|认真的"
)
_MID_SIGNS = re.compile(
    r"最近|这段时间|前几天|前天|昨天|这周|上周|最近在|还在|又在"
)


def depth_for(text):
    """给这句话定深度：shallow / middle / deep。"""
    t = str(text or "").strip()
    if not t:
        return "shallow"
    if _DEEP_SIGNS.search(t):
        return "deep"
    if _MID_SIGNS.search(t):
        return "middle"
    # 长句、带情绪的，也比纯水群深一层
    if len(t) >= 40:
        return "middle"
    return "shallow"


# ── 三层各自的取料 ──────────────────────────────────
def _note(kind, items, query, chars, scene="any"):
    """把「真的进了提示词」的人格条目记给仪表。坏了不能影响回复。"""
    try:
        import memory_usage as _mu
        _mu.note("persona:" + kind, [_mu.key_persona(kind, x) for x in (items or [])],
                 query=query, injected_chars=chars,
                 candidates=len(items or []), scene=scene)
    except Exception:
        pass


def _shallow_block(query="", session_key=""):
    rows = _journal_recent(6, session_key)
    if not rows:
        return ""
    lines = []
    for r in rows[-4:]:
        u = str(r.get("user") or "").strip()[:40]
        b = str(r.get("bot") or "").strip()[:40]
        if u:
            name = str(r.get("speaker_name") or r.get("speaker_id") or "对方")
            lines.append(name + "：" + u)
        if b:
            target = str(r.get("target_uid") or "")
            lines.append("你" + (" → " + target if target else "") + "：" + b)
    if not lines:
        return ""
    return "【刚刚聊过的】\n" + "\n".join(lines)


def _middle_block(query="", keep=4):
    d = _persona()
    exp = [str(x) for x in (d.get("experiences") or []) if str(x).strip()]
    facts = [str(x) for x in (d.get("facts") or []) if str(x).strip()]
    out = []
    _picked_exp, _picked_facts = [], []
    if exp:
        try:
            import persona_growth
            picked = persona_growth._pick_relevant(exp, query, keep)
        except Exception:
            picked = exp[-keep:]
        if picked:
            _picked_exp = list(picked)
            out.append("- 最近经历过：" + "；".join(picked))
    if facts:
        try:
            import persona_growth
            picked = persona_growth._pick_relevant(facts, query, keep)
        except Exception:
            picked = facts[-keep:]
        if picked:
            _picked_facts = list(picked)
            out.append("- 你知道的事：" + "；".join(picked))
    if not out:
        return ""
    _body = "【这段时间的】\n" + "\n".join(out)
    _note("experiences", _picked_exp, query, len(_body))
    _note("facts", _picked_facts, query, 0)
    return _body


def _deep_block():
    body = _deep_block_raw()
    return body


def _deep_block_raw():
    d = _persona()
    out = []
    sv = str(d.get("self_view") or "").strip()
    if sv:
        out.append("- 你心里怎么看自己：" + sv[:120])
    voice = d.get("voice") or {}
    tone = str(voice.get("语气") or "").strip()
    if tone:
        out.append("- 你骨子里的调子：" + tone[:100])
    def _clean(items, n):
        out2 = []
        for x in items:
            t = str(x or "").strip().rstrip("。．.，,；;、 ")
            if t:
                out2.append(t)
        return out2[:n]

    likes = _clean(d.get("likes") or [], 6)
    dislikes = _clean(d.get("dislikes") or [], 4)
    if likes:
        out.append("- 你真正喜欢的：" + "、".join(likes))
    if dislikes:
        out.append("- 你真心讨厌的：" + "、".join(dislikes))
    # facts 以前根本没被读过——"主人是大三学生"写进去了也永远不出现，
    # 所以她会一直问上下班。这里挑和主人相关的几条带上。
    facts = _clean(d.get("facts") or [], 12)
    owned = [f for f in facts if ("主人" in f or (os.getenv("MASTER_DISPLAY_NAME", "") in f if os.getenv("MASTER_DISPLAY_NAME", "") else False))]
    if owned:
        out.append("- 关于主人（必须记住）：" + "；".join(owned[:4]))

    if not out:
        return ""
    _body = "【一层不变的你】\n" + "\n".join(out)
    # 深层是「她一直的样子」，不参与检索但也确实在被用 —— 记下来才算完整账
    _note("likes", likes[:6], "", 0)
    _note("dislikes", dislikes[:4], "", 0)
    _note("facts", owned[:4], "", 0)
    if sv:
        _note("self_view", [sv], "", 0)
    return _body


def _memory_part(layer, text, score):
    body = str(text or "").strip()
    if not body:
        return None
    return {"id": layer + ":" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12],
            "text": body, "kind": layer, "score": score,
            "source": "memory_depth"}


def build(query="", depth=None, max_chars=900, session_key="", manifest=None):
    """按深度拼出这一轮该带的记忆；整块装入预算，不切半句话。

    manifest 传入 dict 时回填本轮实际入选/丢弃的块，供最终请求清单记录。
    """
    dep = depth or depth_for(query)
    if dep == "deep":
        rows = [_memory_part("deep", _deep_block(), 3),
                _memory_part("middle", _middle_block(query, 5), 2),
                _memory_part("shallow", _shallow_block(query, session_key), 1)]
    elif dep == "middle":
        rows = [_memory_part("middle", _middle_block(query, 4), 2),
                _memory_part("shallow", _shallow_block(query, session_key), 1)]
    else:
        rows = [_memory_part("shallow", _shallow_block(query, session_key), 1)]
    rows = [row for row in rows if row]
    import memory_manifest as _mm
    selected, body, dropped = _mm.compose(rows, max_chars)
    if manifest is not None:
        manifest["selected"] = selected
        manifest["dropped"] = dropped
    if not body:
        # 这一轮她一个字的人格记忆都没拿到 —— 绝大多数群聊消息都是这种。
        # 必须记下来：这是「档案写了但用不上」的直接证据，不是「检索没命中」。
        try:
            import memory_usage as _mu
            _mu.note("persona:starved", [], query=query, injected_chars=0,
                     candidates=0, fallback=True,
                     scene=("deep" if dep == "deep" else dep))
        except Exception:
            pass
        return ""
    return "\n\n" + body + "\n（浅层是刚发生的，深层是你一直的样子；别把它们念出来，自然带着就行。）"


def stats():
    d = _persona()
    return {
        "shallow_rows": len(_journal_recent(6)),
        "middle": len(d.get("experiences") or []) + len(d.get("facts") or []),
        "deep": bool(str(d.get("self_view") or "").strip()),
    }
