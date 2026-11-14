# -*- coding: utf-8 -*-
"""发送前的自我纠正：她自己的话里混进内部机制时，就地修掉并记账。

真实事故（2026-11-11 19:24，群里）：有人只发了一张图，她把上下文里的
「[图片：对方想表达：流汗，尴尬，不知所措]」当成自己的话原样发了出来。
那串东西是**我们塞给模型的上下文标注**，不是任何人的发言 —— 她把日志念了。

三道边界，按代价从小到大排：

1. 纠正：能确定是机制残渣的，删掉；删完还有话就照常发。
2. 修复：整条都是残渣就不发 —— 宁可她这轮没说话，也不让她对群友复述日志。
   丢一条不说话的代价，远小于让她变成一台会打日志的机器。
3. 矫正：每次纠正都落账（data/self_correction.json）。同类残渣反复出现时
   能被看见、能进回归锁；不记账的「自我修复」只是把问题藏进日志里。

这里只管**发送前**的文本。上下文标注为什么要写成「（图：…）」这种旁注，
而不是看起来像发言的「[图片：…]」，见 qq_openclaw_bridge._cached_line：
格式本身就是诱因，模型会照着抄。
"""
import json
import os
import re
import time

import safe_store

# 入站媒体标注：她复述这些等于把机制说出口。含图片、表情、语音、视频、文件，
# 以及「图片数=N」这种"带过图但没读懂"的计数。
_ANNOTATION = re.compile(
    r"\[(?:图片|表情包|表情|动画表情|语音|视频|文件)(?:[:：][^\[\]\n]{0,240})?\]")
_COUNT_NOTE = re.compile(r"\[?图片数\s*=\s*\d+\]?")
# 改版后的旁注形态（「（图：对方想表达：…）」）。改格式只是降低被照抄的概率，
# 真正兜底的是这里：只要她自己能看见一种标注，她就有可能照着发一遍。
_SIDE_NOTE = re.compile(r"[（(]图[:：]对方想表达[:：][^）)\n]{0,240}[）)]")
# 单括号「回复/引用」元信息（[回复 1000000001 的消息: …]）。这是上下文里的
# 引用标注，不是她的话；她照抄发出去就是把机制念给群友（11-14 早安事故）。
_REPLY_MARK = re.compile(r"\[(?:回复|引用)[^\[\]\n]{0,240}\]")

# 删完只剩空白和标点，就说明整条都是残渣。
_ONLY_FILLER = re.compile(r"[\s\u3000。，、！？…~～·—\-*\"'“”‘’（）()\[\]【】<>《》]+")

PATTERNS = ("control_marker", "context_annotation", "media_count_note", "image_side_note",
            "reply_marker", "attribution_mismatch")


def audit(text):
    """返回 (可发文本, 命中的残渣类型列表)。clean 可能为空串。"""
    t = str(text or "")
    if not t:
        return "", []
    hits = []
    cleaned = t
    try:
        import outgoing_sanitize
        after = outgoing_sanitize.strip_control_markers(cleaned)
        if after != cleaned:
            hits.append("control_marker")
            cleaned = after
    except Exception:
        pass
    after = _ANNOTATION.sub("", cleaned)
    if after != cleaned:
        hits.append("context_annotation")
        cleaned = after
    after = _COUNT_NOTE.sub("", cleaned)
    if after != cleaned:
        hits.append("media_count_note")
        cleaned = after
    after = _SIDE_NOTE.sub("", cleaned)
    if after != cleaned:
        hits.append("image_side_note")
        cleaned = after
    after = _REPLY_MARK.sub("", cleaned)
    if after != cleaned:
        hits.append("reply_marker")
        cleaned = after
    if hits and not _ONLY_FILLER.sub("", cleaned).strip():
        # 整条都是机制，修不了就丢，不要留下半句残渣当她的发言。
        return "", hits
    return cleaned, hits


def recordable(text):
    """写进她的记忆之前过一道：机制残渣绝不许变成「她说过的话」。

    19:24 那条 [图片：…] 被当成她的发言记进了对话历史、日志和人格日记，
    之后每一轮都当作「她刚说过」重新喂回去 —— 记忆没幻觉，是脏东西被写进去了。
    返回 (该存什么, 是否整条丢弃)。
    """
    cleaned, hits = audit(text)
    if hits and not cleaned.strip():
        return "", True
    return (cleaned if hits else str(text or "")), False


def _path():
    """调用时解析 DATA_DIR：模块顶层冻路径会让测试写脏真实 data/。"""
    return os.path.join(os.getenv("DATA_DIR", "./data"), "self_correction.json")


def load_ledger():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(hits, text=""):
    """记一次纠正。永不抛异常：记账失败不能把回复链路弄崩。"""
    try:
        ledger = load_ledger()
        patterns = ledger.setdefault("patterns", {})
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for hit in hits:
            row = patterns.setdefault(hit, {"count": 0})
            row["count"] = int(row.get("count") or 0) + 1
            row["last"] = now
            row["sample"] = str(text or "")[:80]
        ledger["updated"] = now
        ledger["total"] = int(ledger.get("total") or 0) + 1
        return bool(safe_store.atomic_write_json(_path(), ledger))
    except Exception:
        return False


def stats():
    ledger = load_ledger()
    return {"total": int(ledger.get("total") or 0),
            "patterns": ledger.get("patterns") or {},
            "updated": ledger.get("updated", "")}