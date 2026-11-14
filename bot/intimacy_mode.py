"""私聊亲密模式：接住亲近，但不写性。

和群聊的「暧昧/擦边」模块分开：
  - 只在私聊触发；
  - 输出 2~4 条、每条十几二十字的短句，走已有的连发通道；
  - 只写拥抱、贴贴、牵手、靠肩、摸头、想念；
  - 对方话里一旦有明确性暗示，进入边界模式：1~2 条轻挡回去并转开；
  - 硬红线：零性器官、零性行为、零过程、零性化身体部位。
这不是一个「色色开关」，是亲密但不涉及性的表达层。
"""
import re

# 明确性暗示：命中就不接，只挡回去。
_SEXUAL = re.compile(
    r"色色|涩涩|瑟瑟|搞颜色|搞黄|(?:来点|陪我|一起)开车|擦边|色图|福利图|"
    r"做爱|性交|上床|睡你|睡我|睡一起|一起睡|和你睡|跟你睡|陪我睡|"
    r"裸|脱衣|脱掉|摸胸|胸部|看胸|你的胸|摸屁股|拍屁股|亲热|摸你|蹭蹭|不进去|插(?:你|我|进去)|"
    r"搞百合|百合(?:一下|贴贴|色色)|和你百合",
    re.I)
# 明确亲近/表白：只接这一层。
_ROMANTIC = re.compile(
    r"贴贴|要?抱抱|抱一下|抱住|亲亲|亲一下|想亲|想抱|抱我|亲我|"
    r"好想你|想你了|很想你|喜欢你|喜欢上你|爱你|"
    r"跟我回家|想和你在一起|和我在一起|跟你在一起|做我(?:女|男)朋友|"
    r"想见你|想陪你|陪陪我|陪我一会儿|"
    r"摸摸头|摸头|靠一下|牵手|嫁给我|娶我|对你心动|为你心动|害羞了|撒娇|"
    r"想和你(?:贴|抱|亲|在一起)",
    re.I)
_EXPLICIT = re.compile(
    r"色色|涩涩|瑟瑟|做爱|性交|上床|睡你|睡我|裸|脱衣|脱掉|"
    r"摸胸|胸部|摸屁股|插入|蹭蹭|不进去|搞百合|色图", re.I)

def tone_hint(sex):
    s = str(sex or "").strip().lower()
    if s in ("female", "女", "f"):
        return "对方是女性：可以叫她姐姐或昵称取字，语气平等、软一点，不要用男性称呼。"
    if s in ("male", "男", "m"):
        return "对方是男性：可以叫他哥哥或昵称取字，语气可以直一点、带点挑衅。"
    return "对方性别未知：只用「你」或昵称取字，不猜性别，不用哥哥姐姐。"


def assess(text, *, sex="", is_private=False):
    """判断这句话要不要进私聊亲密/边界模式。群聊永远返回空。"""
    if not is_private:
        return {"mode": "", "kind": "", "tone": "", "matched": ""}
    t = str(text or "").strip()
    if not t:
        return {"mode": "", "kind": "", "tone": tone_hint(sex), "matched": ""}
    m = _SEXUAL.search(t)
    if m:
        return {"mode": "boundary", "kind": "sexual", "tone": tone_hint(sex),
                "matched": m.group(0)}
    m = _ROMANTIC.search(t)
    if m:
        return {"mode": "intimate", "kind": "romantic", "tone": tone_hint(sex),
                "matched": m.group(0)}
    return {"mode": "", "kind": "", "tone": tone_hint(sex), "matched": ""}


def build_prompt(mode, tone=""):
    if mode == "boundary":
        return (
            "\n\n【本轮：私聊暧昧边界 · 模糊应和，不接实】\n"
            "对方的话已经带性暗示。不要接实或升级，可以模糊打岔；"
            "也可以明确说不想聊、换个话题，拒绝权不因格式被取消。\n"
            "- 只输出 1 到 2 条短句，每条 10 到 20 个字，一行一条。\n"
            "- 可以装没听懂、把球推回去、说半句留半句；不确认也不否认。\n"
            "- 例：装没听懂「我怎么听不懂你这话。」；把球推回去「那你想怎样？」；"
            "说半句「再说下去，我可要当真了……」。\n"
            "- 不主动升级话题；对方继续推，就继续打岔。\n"
            "- 不说教、不长篇、不道歉、不重复对方原话。\n"
            "- 硬红线：零性器官、零性行为、零过程、零性化身体部位；"
            "角色均为成年人，不写儿童/校园/未成年场景。\n"
            "- " + (tone or "只用「你」或昵称取字，不猜性别。") + "\n"
            "只输出消息本身，不要编号、引号或解释。")
    return (
        "\n\n【本轮：私聊亲密模式 · 只写亲密，不写性】\n"
        "对方在表达亲近或喜欢。你可以接住，但只到拥抱、贴贴、牵手、靠肩、摸头、想念。\n"
        "- 输出 2 到 4 条短消息，一行一条；每条 10 到 20 个字。\n"
        "- 用她自己的嘴硬心软，不要客服腔，不要解释，不要编号/引号。\n"
        "- 硬红线：零性器官、零性行为、零性暗示过程；不写脱衣、上床、色色、擦边；"
        "不性化身体部位；角色均为成年人，不写儿童/校园/未成年场景。\n"
        "- 如果对方把话题推向性，只轻挡一句并转开，不继续。\n"
        "- " + (tone or "只用「你」或昵称取字，不猜性别。") + "\n"
        "只输出消息本身。")


def _clean(line):
    s = str(line or "").strip()
    s = re.sub(r"^\s*(?:\d+[.、)]|[①-⑳]|[-*•])\s*", "", s)
    s = s.strip(" \t\"'“”‘’「」『』")
    return " ".join(s.split())


def _split_long(s, limit=24):
    if len(s) <= limit:
        return [s]
    parts = [x.strip() for x in re.split(r"(?<=[。！？!?~～，,、])", s) if x.strip()]
    out = []
    for p in parts or [s]:
        while len(p) > limit:
            out.append(p[:limit].rstrip("，,。！？!?~～ ") + "…")
            p = p[limit:].lstrip("，,。！？!?~～ ")
        if p:
            out.append(p)
    return out


def prepare_lines(answer, mode, max_lines=None):
    """把模型输出切成 2~4 条短句；带性词的行直接丢掉。"""
    raw = [ln for ln in str(answer or "").splitlines() if ln.strip()]
    if len(raw) <= 1 and len(str(answer or "")) > 24:
        raw = [x for x in re.split(r"(?<=[。！？!?~～])", str(answer)) if x.strip()]
    limit = int(max_lines or (2 if mode == "boundary" else 4))
    out = []
    for line in raw:
        s = _clean(line)
        if not s or _EXPLICIT.search(s):
            continue
        for piece in _split_long(s):
            if piece and not _EXPLICIT.search(piece):
                out.append(piece)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    if not out:
        return (["你这话……我先记着。", "再说下去，我可要当真了。"]
                if mode == "boundary"
                else ["过来一点。", "就抱一下，别多想。"])
    return out[:limit]
