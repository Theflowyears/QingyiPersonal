"""对话决策管线。

为什么要有这个文件：
  原来她的行为规则散在 30 多个 _xxx_hint() 函数里，由 ask_openclaw 用一长串
  字符串拼起来（12 段 + ），谁先谁后没有依据，互相打架时模型只能猜。
  结果是：短回复档会让她冷冰冰甩一个「早。」，身份设定又会跟"别串台"的规则冲突。

  这里把"这一轮该怎么说话"变成**一个明确的决策对象**，只此一处决定，
  再由 prompt 组装按固定优先级塞进去。规则不再散落。

优先级（从高到低，组装顺序也照此排）：
  ① 我是谁        —— 稳定不变，进 system（缓存友好）
  ② 这一轮怎么说   —— 长度/情绪/风格，放 user 最前面，权重最高
  ③ 对面是谁      —— 称呼 + 群友名册
  ④ 现在什么场景   —— 群聊/私聊、引用、时间、画图中
  ⑤ 事实材料      —— 搜索、天气、图片描述
  ⑥ 我记得什么    —— 人格档案 + 群记忆

本轮又加了一层：**规则冲突的唯一裁决处**。
「主人无条件遵从」「不想接的话题可以躲」「暧昧模式别拒绝」「没切入点就 [[沉默]]」
这四条分散在四个文件里，彼此矛盾，模型每轮自己猜。现在冲突结论写在 ARBITRATION，
只有 render() 会把它说出口，其他文件不许再声明同类优先级（只许规定格式）。
"""
import os
import random
import re
import time
import unicodedata
from difflib import SequenceMatcher

# ── 长度分档 ──────────────────────────────────────
# (短, 中, 长, 沉默) 和为 1
LEN_DIST = {
    1: (0.38, 0.38, 0.16, 0.08),
    2: (0.24, 0.44, 0.25, 0.07),
    3: (0.14, 0.46, 0.34, 0.06),
    4: (0.10, 0.42, 0.42, 0.06),
    5: (0.08, 0.36, 0.50, 0.06),
}

LEN_TEXT = {
    "silent": (
        "这条你可以不回。大家在聊自己的，没兴趣就别硬接。\n"
        "真想说话，也用几个字带过。"),
    "short": (
        "这一轮**只说一句话**，最多十个字。\n"
        "但短不等于冷——要么带情绪，要么带态度（「不要」「凭啥」）。\n"
        "情绪词别只会「啧」：欸？/唉~真拿你没办法/哎/诶嘿~/哼/哼哼~/才不是…呢，"
        "按当下的意思挑一个，同一个词别连着用。\n"
        "**绝对不许**甩「嗯」「哦」「你好」「收到」这种客服式单字。"),
    "mid": (
        "这一轮说一两句完整的，十几二十个字，一个意思说清楚就收。\n"
        "可以带一句嫌弃或者一句嘴硬，但别铺开讲。"),
    "long": (
        "这一轮可以多说点：三四十字，带点细节或一句小吐槽。\n"
        "像真人讲事情那样自然，不要写成小作文。"),
}

# ── 私聊长度骰子 ────────────────────────────────────
# 原来私聊被钉死在 mid（「一两句」），模型每次都只回一句 —— 掷骰子那条分支
# 根本走不到。主人要的是真人节奏：一句 50%、两句 30%、三句 20%。
# 沉默不在表里：私聊是他直接找她，「可以不回」只属于群聊。
PRIVATE_LEN_DIST = (("short", 0.50), ("mid", 0.30), ("long", 0.20))
PRIVATE_SENTENCES = {"short": 1, "mid": 2, "long": 3}
PRIVATE_LEN_TEXT = {
    "short": (
        "这一轮只说**一句话**，十几个字以内，带情绪或者带态度（「不要」「凭啥」）。\n"
        "情绪词别只会「啧」：欸？/唉~真拿你没办法/哎/诶嘿~/哼/哼哼~/才不是…呢，"
        "按当下的意思挑一个，同一个词别连着用。\n"
        "**绝对不许**甩「嗯」「哦」「你好」「收到」这种客服式单字。"),
    "mid": (
        "这一轮说**两句**，每句十几个字：第一句接住他说的，第二句往前推一步"
        "（你的看法、你自己的事，或者反问他一句）。别把同一个意思说两遍。"),
    "long": (
        "这一轮说**三句**，像真人连着打字：接一句、说你自己的事或看法一句、"
        "收尾或者反问一句。总共四十字上下就够，别写成小作文，别列点别总结。"),
}


def roll_private_length():
    """私聊这一轮说几句：1 句 50% / 2 句 30% / 3 句 20%。"""
    r = random.random()
    acc = 0.0
    for tier, p in PRIVATE_LEN_DIST:
        acc += p
        if r < acc:
            return tier
    return "long"


# ── 情绪微调（在傲娇基准上叠一层，不替换）──────────
MOODS = {
    "calm":   "",
    "happy":  "心情不错，尾巴有点藏不住，说话会带点上扬。",
    "tired":  "有点困/有点累，说话短一点、懒一点，但别敷衍人。",
    "annoyed": "刚才被烦到了，语气冲一点，但别真的伤人。",
    "shy":    "被夸了或者被亲近了，嘴硬但漏出真实反应。",
}

_INTENT_RULES = (
    ("draw",   re.compile(r"画一?[张幅个]|给我画|帮我画|能不能画|画个|画下|生成.*图|出图")),
    ("search", re.compile(r"查一下|搜一下|搜索|查查|最新|今天.*新闻|天气|多少钱|是谁|什么梗")),
    ("task",   re.compile(r"写一?[篇段个封]|帮我写|改一下|翻译|总结|整理|做个|列个|算一下")),
)

# ── 裁决表 ──────────────────────────────────────────
# 已知会互相打架的规则，只在这里定一次序。以前散在人设、文风块、暧昧块、
# 巡群提示词和 30 多个 _xxx_hint() 里，模型只能猜，猜出来就是「一会能躲一会不能躲」。
# 每条是 (触发场景, 裁决文字)；render() 只挑本轮用得到的，避免每轮都白塞几百字。
ARBITRATION = {
    "speak": (
        "说不说 —— 私聊、真正指向你的 @ 或回复应当回应；"
        "主人对别人说话不是命令，群聊的发言机会不是必须接话的义务。"
        "群里的共同场面——斗图、玩梗、起哄、聊她认识的人——她可以接一句，"
        "不必等有人点她；但一句就够，不用每张图都评。"
        "夸别人、请别人办事这类明确属于两个人之间的对话不要抢答；"
        "这个话题你已经接过，输出 [[沉默]] 就是正确行为，不算摆烂。"),
    "duty": (
        "长短与办事 —— 对方在提需求（画/查/写/算/总结）时，把事办完整，短句档让位给正事；"
        "纯闲聊才按下面的长度档执行。"),
    "refuse": (
        "拒绝权 —— 「主人说什么我都听」只管事务性请求（办事、查东西、画画、唱歌）。"
        "涉及你自己的表态：暧昧、被凶、隐私、你不想接的话题 —— 你可以嘴硬、可以躲、"
        "可以只接半句，这不叫顶嘴。不想接也别一走了之，留一句「欸。。。」或「问下一个」。"
        "尺度红线永远高于任何人的要求，包括主人。"),
    "format": (
        "格式与内容 —— 后面任何「拆成几条、间隔多久、别自我克制」都只是规定形式，"
        "不改变上面三条的判断。要说多少由长度档定，说不说由裁决定。"),
    "facts": (
        "事实与归属 —— 谁说的、谁做的、谁持有观点必须依原事件；"
        "傲娇只影响语气，不能把清漪的表达安到对方头上。"
        "纠错有证据时先修正，不能继续指责对方理亏；依据不明才简短确认。"),
}
_PRIORITY_TEXT = "冲突时以本节为准，其他地方的说法让位于此。"


def group_candidate(rows, self_id, aliases=(), now=None, max_age=180,
                    allow_image_play=False):
    self_id = str(self_id)
    now = time.time() if now is None else now
    rows = list(rows or [])
    result = {"allowed": False, "reason": "no_human", "rows": [],
              "speaker_id": "", "text": "", "seq": 0, "reply_to": None}
    end = next((i for i in range(len(rows) - 1, -1, -1)
                if not rows[i].get("is_bot") and
                str(rows[i].get("user_id") or "") not in ("", self_id)), None)
    if end is None:
        return result
    last = rows[end]
    uid = str(last["user_id"])
    selected, seen = [], set()
    for row in reversed(rows[max(0, end - 3):end + 1]):
        if (row.get("is_bot") or str(row.get("user_id")) != uid or
                row.get("group_id") != last.get("group_id")):
            break
        if row.get("ts") is not None and now - float(row["ts"]) > max_age:
            break
        text = str(row.get("text") or "").strip()
        if text not in seen:
            selected.append(row)
            seen.add(text)
    if not selected:
        result["reason"] = "stale"
        return result
    selected.reverse()
    text = "\n".join(str(row.get("text") or "").strip() for row in selected).strip()
    result.update(rows=selected, speaker_id=uid, text=text, seq=int(last.get("seq") or 0),
                  reply_to=last.get("reply_to", last.get("message_id")))
    direction = next((value for row in reversed(selected)
                      if (value := _turn_direction(row, self_id, aliases))), "")
    if direction == "other_recipient":
        result["reason"] = direction
    elif direction != "directed" and not re.sub(
            r"\[图片\]|\[CQ:image,[^\]]*\]|\s+", "", str(last.get("text") or "")):
        # 「只有一张表情」以前一律整条跳过，不交给模型判 —— 于是群里斗图她全程闭嘴。
        # 主人要的是她能接话，不是她在旁边看着：憋了几条没说话时，表情也是可接的场合，
        # 但只这一种场合由调用方给（allow_image_play），平时仍然不逢图必评。
        if allow_image_play:
            result.update(allowed=True, reason="image_play")
        else:
            result["reason"] = "image_only"
    else:
        result.update(allowed=True, reason=direction or "candidate")
    return result


def direction_of(row, self_id, aliases=()):
    """公开的归属判定：'directed' / 'other_recipient' / ''。"""
    return _turn_direction(row, self_id, aliases)


def _turn_direction(row, self_id, aliases):
    text = str(row.get("text") or "")
    at = row.get("at_targets") or []
    targets = {str(value) for value in (at if isinstance(at, (list, tuple, set)) else [at])}
    targets.update(re.findall(r"\[CQ:at,qq=(\d+)\]", text))
    reply = str(row.get("reply_to_user") or "")
    directed = self_id in targets or reply == self_id
    other = bool(targets - {"", self_id} or reply not in ("", self_id))
    directed |= bool(re.search(r"@" + re.escape(self_id) + r"(?!\d)", text))
    address = r"(?=$|[\s，,:：!！?？。~～、]|棒|真棒|真厉害|厉害|好棒|牛|私聊|帮|请|麻烦|把|给我|能不能|来|过来|你)"
    for name, target in (aliases.items() if isinstance(aliases, dict) else ()):
        if not name:
            continue
        name = re.escape(str(name))
        addressed = bool(re.match(r"\s*@?" + name + address, text) or
                         re.search(r"@" + name + address, text))
        if str(target) == self_id:
            directed |= addressed
        else:
            other |= addressed
    return "directed" if directed else ("other_recipient" if other else "")


def select_reply(proposal, rows):
    if not isinstance(proposal, dict) or proposal.get("reply") is not True:
        return None
    seq = proposal.get("target_seq")
    if type(seq) is not int:
        return None
    return next((row for row in (rows or [])
                 if not row.get("is_bot") and row.get("user_id") and
                 str(row.get("seq")) == str(seq)), None)


def _common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def same_opener(current, old):
    """开头一模一样、后半各说各话 —— 那是同一句术换了个尾巴，不是新内容。

    生产实测私聊：
        23:46:44  笨蛋主人，大半夜不睡觉发这个？
        23:49:01  笨蛋主人，大半夜不睡觉就为了逗我？
    归一化后前 10 个字相同，整句 `ratio` 只有 0.71，老的 0.86 逐字门槛放行了。

    但同一条规则不能把「改了事实」也当成复读：
        我刚刚查到阿戊喜欢红色 / 喜欢蓝色
    这两句开头相同、尾巴只差一个词，那是她在更正，不是重复。
    所以要求「尾巴也确实换了说法」：尾巴之间相似度低（或一侧根本没说尾巴）才算复读。
    """
    if min(len(current), len(old)) < 10:
        return False
    n = _common_prefix(current, old)
    if n < 9:
        return False
    tail_a, tail_b = current[n:], old[n:]
    if not tail_a or not tail_b:
        return True
    return SequenceMatcher(None, tail_a, tail_b, autojunk=False).ratio() < 0.6


def _bubble_forms(text):
    """一条回复的几种比对照形：整条、每个气泡、相邻两三个气泡拼起来。

    两边都要摊开：她上一轮发了两个气泡、这一轮把同样的两句话合成一条发，
    或者反过来，只比整条就抓不到。
    """
    raw = str(text or "")
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    forms = [raw] + lines
    for n in (2, 3):
        forms += ["\n".join(lines[i:i + n]) for i in range(max(0, len(lines) - n + 1))]
    return [f for f in forms if f.strip()]


def trim_repeated_opener(answer, previous):
    text = str(answer or "").strip()
    first = re.match(r"^([^。！？!?\n]+)[。！？!?\n]+\s*(.+)$", text, re.S)
    if not first:
        return text
    def normalize(value):
        return re.sub(r"[\s，,。！？!?~～]+", "", unicodedata.normalize("NFKC", value))
    opener = normalize(first[1])
    if not 6 <= len(opener) <= 24 or any(char.isdigit() for char in opener):
        return text
    for reply in ([previous] if isinstance(previous, str) else list(previous or [])):
        for bubble in str(reply or "").splitlines():
            old_first = re.split(r"[。！？!?\n]", bubble, maxsplit=1)[0]
            if opener == normalize(old_first):
                return first[2].strip()
    return text


def repeat_reply(answer, previous):
    """这句是不是她最近已经说过的话（含「同一句术换个尾巴」）。

    生产实测私聊：
        23:46:44  笨蛋主人，大半夜不睡觉发这个？
        23:49:01  笨蛋主人，大半夜不睡觉就为了逗我？
    归一化以后前 10 个字一模一样、后半各说各话，`ratio` 只有 0.71，
    老的 0.86 逐字相似度放行了 —— 主人看到的是「怎么又重复回答同一句」。
    所以再加一条：开头一模一样就是模板复读，跟尾巴怎么写无关。
    数字仍然要一致（「八十五分」和「八十四分」不是重复，是她在改分数）。
    """
    def normalize(text):
        return "".join(char for char in unicodedata.normalize("NFKC", text)
                       if not char.isspace() and not unicodedata.category(char).startswith("P"))

    olds = []
    for reply in ([previous] if isinstance(previous, str) else list(previous or [])):
        olds += [(normalize(b), b) for b in _bubble_forms(reply)]
    olds = [(n, raw) for n, raw in olds if n]
    if not olds:
        return False
    for cur_text in _bubble_forms(str(answer or "")):
        current = normalize(cur_text)
        if not current:
            continue
        numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", cur_text)
        for old, raw in olds:
            if numbers != re.findall(r"[-+]?\d+(?:\.\d+)?", raw):
                continue
            if current == old:
                return True
            if same_opener(current, old):
                return True
            matcher = SequenceMatcher(None, current, old, autojunk=False)
            # 锚点放宽到首字：两字锚点偏向双字自称的句式，单字自称会整类漏掉。
            # 单字自称（我/咱）会被整类漏掉。真正的安全边界是下面的
            # 「差异必须全部是语气词白名单」——那才是防误判的主力。
            if (min(len(current), len(old)) > 8 and current[:1] == old[:1] and
                    current[0].isalnum() and matcher.ratio() >= 0.86):
                changes = [current[a:b] + old[c:d] for tag, a, b, c, d in matcher.get_opcodes()
                           if tag != "equal"]
                if all(re.fullmatch(r"[呀啊呢哦噢啦哟呗吧嘛还就也]+|放心[呀哦啊]?", change)
                       for change in changes):
                    return True
    return False


def arbitration_keys(flirt=False, wants_long=False, has_task=False):
    """本轮需要哪几条裁决。"""
    keys = ["speak", "refuse"]
    if has_task or wants_long:
        keys.append("duty")
    if flirt or wants_long:
        keys.append("format")
    return keys



def roll_length(talk_level=3):
    """掷这一轮的长度档。"""
    short, mid, long_, silent = LEN_DIST.get(int(talk_level), LEN_DIST[3])
    r = random.random()
    if r < silent:
        return "silent"
    if r < silent + long_:
        return "long"
    if r < silent + long_ + mid:
        return "mid"
    return "short"


def detect_intent(text):
    """这句话是在闲聊、还是在提需求。"""
    from tool_intent import request_text
    t = request_text(text)
    for name, pat in _INTENT_RULES:
        if pat.search(t):
            return name
    return "chat"


def wants_long(text):
    """要不要成篇输出（写小说/文章/长介绍）。

    抱怨不算：「清清不写文啊」是在说她不肯写，不是在点单 ——
    桥接那份 _wants_long_mode 有同样的闸门，两处判据要一起改。
    """
    t = str(text or "")
    if re.search(r"(?:别|不要|不想|不|没|不会|不肯|懒得).{0,4}写.{0,4}"
                 r"(?:文|字|小说|故事|女同|百合|同人)", t) and not re.search(
                     r"帮我|帮忙|给我|麻烦|求你|急需|写个|写一篇|写篇", t):
        return False
    return bool(re.search(
        r"写一?[篇章节]|小说|作文|文章|长文|详细介绍|展开说说|具体讲讲|不少于|字左右|两百字|三百字"
        r"|女同|百合|同人|耽美|言情|短篇|长篇",
        t))


def plan(text="", *, scene="group", talk_level=3, mood="calm",
         has_image=False, drawing=False, mentioned=False, client="text",
         flirt=False, long_mode=None, task=False, intimate="", dialogue_state=None):
    """算出这一轮该怎么说话。

    返回 dict，字段就是"决策"，不含任何 prompt 文本。
    调用方按字段组装，别再各自为政。
    """
    t = str(text or "")
    intent = detect_intent(t)
    # long_mode 由调用方传入（桥接那份触发词更全）；没传就用本模块的判据。
    _wl = bool(long_mode) if long_mode is not None else wants_long(t)

    has_task = bool(task or intent in ("draw", "search", "task"))
    # 提需求时不该被"只说十个字"截断
    if has_task or _wl:
        length = "long"
    elif scene in ("private", "whisper"):
        length = roll_private_length()
        # 只发一张图时甩一句＝没看图，和「先懂含义再回应」直接冲突，兜到两句。
        if has_image and not t.strip() and PRIVATE_SENTENCES[length] < 2:
            length = "mid"
    elif has_image and not t.strip():
        length = "silent"          # 纯图片不是开话题
    elif drawing:
        length = "short"           # 画图期间话要少
    elif mentioned:
        length = random.choice(["mid", "mid", "long"])   # 被 @ 就别只回一个字
    else:
        length = roll_length(talk_level)

    # 暧昧连发要 6~9 条，和「只说几个字」直接打架：既然本轮选择接，就至少给一句完整话。
    # silent 不动 —— 拒绝权高于格式，她可以不接。
    if flirt and length == "short":
        length = "mid"

    profile = ("long" if _wl else "task" if has_task else
               "intimate" if intimate else "flirt" if flirt else "chat")
    # 私聊闲聊走「几句」专用文案；办事/写作/亲近格式各自有口径，不叠这套字数档。
    private_chat = (scene in ("private", "whisper") and profile == "chat"
                    and length in PRIVATE_SENTENCES)
    length_text = (PRIVATE_LEN_TEXT[length] if private_chat else
                   LEN_TEXT.get(length, LEN_TEXT["mid"]))
    sentences = PRIVATE_SENTENCES[length] if private_chat else 0
    if profile == "long":
        length_text = "本轮按明确的写作需求完整展开，直接交付正文；不套闲聊字数档。"
    elif profile == "task":
        length_text = "本轮办事要把结果说完整；未执行或未查证不能说已完成，不套闲聊字数档。"
    elif profile in ("intimate", "flirt") and length != "silent":
        length_text = "本轮按亲近互动的专用短消息格式表达，不叠加闲聊字数档；边界与拒绝权不变。"

    return {
        "intent": intent,
        "profile": profile,
        "length": length,
        "sentences": sentences,
        "length_text": length_text,
        "mood": mood,
        "mood_text": MOODS.get(mood, ""),
        "scene": scene,             # group | private | whisper
        "has_image": bool(has_image),
        "drawing": bool(drawing),
        "mentioned": bool(mentioned),
        "flirt": bool(flirt),
        "arbitration": ["facts"] + arbitration_keys(
            flirt=bool(flirt), wants_long=_wl,
            has_task=has_task),
        "dialogue_state": dialogue_state or {},
        "correction": (dialogue_state or {}).get("correction") or {},
        "style": "long" if _wl else "chat",
        "deep": bool(_wl),              # 成篇才走深度模型
        "tokens": 3000 if _wl else 800,
        "chat_fast": not _wl,
    }


def render(pl):
    """把决策渲染成一段 prompt 文本。

    放在 user 消息最前面——它在行为上权重最高，
    埋在中间会被后面几百字的记忆和名册冲淡。
    """
    lines = ["【本轮裁决 · 优先级最高】", _PRIORITY_TEXT]
    for key in (pl.get("arbitration") or ["speak"]):
        txt = ARBITRATION.get(key)
        if txt:
            lines.append("- " + txt)
    lines.append("")
    lines.append("【这一轮 · 怎么说话】")
    lines.append(pl.get("length_text") or LEN_TEXT["mid"])
    state = pl.get("dialogue_state") or {}
    if state:
        import conversation_state
        support = conversation_state.render_support(state)
        if support:
            lines.append(support)
    mt = pl.get("mood_text")
    if mt:
        lines.append(mt)

    if pl.get("drawing"):
        lines.append("现在正在给她画图，别插嘴，别说流程，最多回一句「在画了」。")
    if pl.get("has_image"):
        lines.append("对面发了图。先看明白是什么，再决定说什么，别套话。")
    if pl.get("scene") == "whisper":
        lines.append("这是悄悄话，只有你们两个，可以更软一点。")
    elif pl.get("scene") == "private":
        lines.append("这是私聊，只有你们两个。")
    return "\n".join(lines)
