"""工具意图识别：画画 / 唱歌 / 语音 / 叫停。

为什么单独拎出来：
  这四类判定原来分散在三个模块（image_gen.detect_intent、tts.detect_sing、
  bridge 里的 _VOICE_ASK 和 _maybe_cancel_draw），主流程按固定顺序逐个试，
  谁也说不清为什么某句话被某个功能抢走。踩过的误判：
    ·「唱歌是不是很难」→ 被当成点歌
    ·「发个语音包」    → 被当成要她开口
    ·「画个饼给我看看」 → 被当成要画图（画饼是俗语）
    ·「唱歌的时候画张画」→ 被点歌抢走，其实是要画

  这里给一个单一入口：一次性判定四类，返回推荐动作 + 理由，
  既能保证优先级稳定，也能在日志里看清"为什么走了这条路"。
"""
import re

# 叫停：只在"正在画图"时才有效，由调用方传入 drawing 决定
_NEG_DRAW = re.compile(
    r"(别|不要|甭|不用|不必|先别|停|取消|算了)(画|生成|出图|图)|"
    r"停下|别画了|不画了|停止")

# 语音回复的策略条件放在 bridge 里，这里只做识别


def network_forbidden(text):
    return bool(re.search(r'(?:别|不要|不用|不必|无需|禁止)(?:再)?(?:联网|上网|搜索|搜|查资料)',
                          str(text or '')))


def request_text(text):
    raw = str(text or '').strip()
    clauses = re.split(r"([，,。！？!；;\n]+|(?<![A-Za-z0-9/])\?)", raw)
    kept = []
    removed = False
    for clause in clauses:
        if re.search(r"(?:别|不要|不用|不必|无需|先别|甭|取消|停止)(?:再|继续)?"
                     r"(?:查|搜|找|联网|上网|用|发|画|生成|唱|算|抽|占卜|塔罗|周易|语音|继续)", clause):
            removed = True
            continue
        if re.search(r"(?:没有|没带|没发|没传)(?:任何|这个|那个)?(?:表格|文件|视频|excel|xlsx|csv)", clause, re.I):
            removed = True
            continue
        kept.append(clause)
    return ''.join(kept).strip('，,。！？!?；;\n ') if removed else raw


def local_state_query(text):
    t = re.sub(r"\[CQ:at,[^\]]+\]|@\S+\s+", "", request_text(text)).strip()
    t = re.sub(r"[\s，,。！？!?~～]+", "", t)
    prefix = r"(?:请|麻烦|帮我|替我|给我)?(?:你)?(?:查一查|查一下|查查|查询|查看|查下|查|看看|看下|看一下|问问)?(?:一下)?"
    suffix = r"(?:是)?(?:多少|几分|几|有多少)?(?:分)?(?:呀|啊|呢|吗|啦|哦|嘛|宝宝|宝贝)*"
    return bool(
        re.fullmatch(prefix + r"(?:你对我的?|我的?|当前|现在)?(?:好感度?|亲密度)" + suffix, t)
        or re.fullmatch(prefix + r"(?:你)?(?:现在|今天)?(?:的)?心情(?:怎么样|如何|好不好)?" + suffix, t)
        or re.fullmatch(r"你(?:还)?记得我(?:叫什么|是谁)(?:吗|呀|啊)?|我是谁|你叫什么(?:呀|啊)?", t))


def detect(text, *, drawing=False):
    """识别这句话想要哪个工具。

    返回 dict：
      action : cancel_draw | draw | sing | voice | chat
      reason : 一句话说明为什么（写进日志）
      detail : 具体情况（画什么/唱什么/保密）
    """
    t = str(text or "").strip()
    out = {"action": "chat", "reason": "普通聊天", "detail": {}}
    if not t:
        return out

    # ① 叫停：正在画图时才认
    if drawing and _NEG_DRAW.search(t):
        out.update({"action": "cancel_draw", "reason": "正在画图，用户要停"})
        return out

    t = request_text(t)
    if not t:
        return out

    # ② 绘图
    try:
        import image_gen
        prompt = image_gen.detect_intent(t)
        if prompt:
            out.update({
                "action": "draw",
                "reason": "明确要求画图",
                "detail": {"prompt": prompt},
            })
            return out
    except Exception:
        pass

    # ③ 唱歌
    try:
        import tts
        sing, song = tts.detect_sing(t)
        if sing:
            out.update({
                "action": "sing",
                "reason": "点歌请求",
                "detail": {"song": song, "voice": tts.detect_voice(t) or "amiya"},
            })
            return out
    except Exception:
        pass

    # ④ 语音
    try:
        import qq_openclaw_bridge as _br
        if _br._VOICE_ASK.search(t) and not _br._VOICE_NOT.search(t):
            out.update({"action": "voice", "reason": "要她开口说话"})
            return out
    except Exception:
        pass

    return out


def summary(pl):
    """一行摘要，给日志用。"""
    a = pl.get("action")
    d = pl.get("detail") or {}
    extra = ""
    if a == "draw":
        extra = " prompt=%s" % str(d.get("prompt"))[:30]
    elif a == "sing":
        extra = " song=%s" % (d.get("song") or "即兴")
    return "action=%s (%s)%s" % (a, pl.get("reason") or "", extra)
