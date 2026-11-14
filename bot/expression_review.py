# -*- coding: utf-8 -*-
"""发送前的表达复核：只改「把揣测说成事实」这一种错，别的一概不碰。

病灶（2026-11-11 19:24）：主人发了个「流汗/尴尬」的表情，她回
「笨蛋主人，这点小场面就慌成这样？」—— 图意是猜的，她当成他的真实状态说了。
这类断言代码判不了对错，所以多跑一次快速模型，只问一个问题：
**这句话里有没有把没发生的事说成发生了？**

三条自我约束：
1. 只改事实性断言，不改语气、不改长度、不改人设；拿不准就不改。
2. 复核结果不可信（空、过长、答非所问）就退回原文 —— 复核是为了少说错话，
   不是为了让她不敢说话。
3. 一次调用、固定超时；失败静默退回原文，绝不挡发送。
"""
import os
import re
import time

_META = re.compile(r"^(无需修改|不用改|无问题|没有需要修改|原文不改|无。)")
_QUOTE = re.compile(r"[「『“\"]")

# 什么话值得多花一次复核 —— 这套打分以后由情感模块接管，所以只吃
# 「这句话本身 + 上下文」，不依赖人格、不依赖时间，纯函数。
STATE_WORDS = ("慌", "怕", "急", "生气", "气死", "难过", "委屈", "紧张", "不高兴",
               "开心", "失落", "尴尬", "心虚", "不安", "郁闷")
PAST_WORDS = ("刚才", "刚刚", "之前", "上次", "昨天", "前天", "说过", "讲过")
ASSERT_VERBS = ("说", "讲", "答应", "明明", "还在", "一直", "总是", "从来不")
_BUDGET = {}

SYSTEM = (
    "你是发言前的复核员。只处理一种错误：说话人把**没有发生过的事**当成发生了——"
    "凭空断定别人的情绪、别人的原话、别人答应过的事、别人的动作。\n"
    "改法：把那一句改成不确定、或者改成问句、或者删掉那半句；"
    "其余每个字都不许动，包括语气词、自称、长短和排版。\n"
    "只输出最终发言本身，不要解释、不要加引号。若确实没问题，就原样输出原文。"
)


def _names_in(context):
    try:
        import claim_check
        return {name for name, _said in claim_check.parse_context(context)}
    except Exception:
        return set()


def score(draft, context):
    """给"这句话有多可能在断言别人的事"打分。返回 (分数, 理由)。"""
    text = str(draft or "")
    if len(text) < 6:
        return 0, []
    total, why = 0, []
    names = _names_in(context) | {"主人"}
    if any(name and name in text for name in names):
        total += 2
        why.append("指名")
    if any(word in text for word in STATE_WORDS):
        total += 2
        why.append("断言情绪")
    if any(word in text for word in PAST_WORDS):
        total += 1
        why.append("引用过去")
    if _QUOTE.search(text):
        total += 1
        why.append("转述")
    for name in names:
        at = text.find(name)
        if at < 0:
            continue
        tail = text[at + len(name): at + len(name) + 10]
        if any(verb in tail for verb in ASSERT_VERBS):
            total += 2
            why.append("断言动作")
            break
    return total, why


def needs_review(draft, context, threshold=2):
    """分数到线才复核。纯反应、短句、没提人一律不花这次调用。"""
    total, why = score(draft, context)
    return (total >= threshold), total, why


def budget_ok(cap=None):
    """每小时最多复核多少次。复核是为了少说错话，不是把成本放开。"""
    try:
        cap = int(cap or os.getenv("OPS_REVIEW_PER_HOUR", "40"))
    except (TypeError, ValueError):
        cap = 40
    hour = time.strftime("%Y%m%d%H")
    used = int(_BUDGET.get(hour) or 0)
    if used >= cap:
        return False
    _BUDGET.clear()
    _BUDGET[hour] = used + 1
    return True


def build_messages(draft, context, budget=1600):
    ctx = str(context or "").strip()
    if len(ctx) > budget:
        ctx = ctx[-budget:]
    user = (
        "【群里的真实对话（唯一事实来源）】\n" + (ctx or "（无）") + "\n\n"
        "【她准备发出去的话】\n" + str(draft or "") + "\n\n"
        "对照上面的事实，只改凭空断定别人的那半句；没有就原样返回。"
    )
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


def accept(original, revised):
    """复核结果能不能用。宁可退回原文，也不要让复核把话说坏。"""
    src = str(original or "")
    out = str(revised or "").strip()
    if not out or out == src:
        return None
    if _META.match(out):
        return None
    if len(out) < max(2, int(len(src) * 0.45)) or len(out) > int(len(src) * 1.8) + 8:
        return None
    if out.startswith(("“", "\"", "「")) and out.endswith(("”", "\"", "」")):
        out = out[1:-1].strip()
    return out or None


async def review(draft, context, call, force=False):
    """返回改过的文本；不需要改或不可信时返回 None（调用方保留原文）。"""
    text = str(draft or "").strip()
    if len(text) < 6:
        return None
    if not force and not needs_review(text, context)[0]:
        return None
    if not budget_ok():
        print("[表达复核] 本小时额度用完，按原文发", flush=True)
        return None
    try:
        raw = await call(build_messages(text, context))
    except Exception:
        return None
    return accept(text, raw)