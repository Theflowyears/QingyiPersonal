# -*- coding: utf-8 -*-
"""发送前的交叉验证：她把甲的事安到乙头上时，收口而不是封口。

病灶（2026-11-11 19:26）：群里阿戊说「还有一个每次都一键跟随的小朋友」，
阿甲说「我还在路上」。她回：「像是在cue阿甲那个『一键跟随』的小朋友」——
把阿戊的谜面安到了阿甲身上。事实全在上下文里，是拼错了人。

口径不搞"禁止推断"（那会把她的话削平），只做一件能证明的事：
**同一句话里，某个只属于 Q 的说法紧跟在名字 P 后面，而 P≠Q。**
这时把那个名字降级成「某位」——句子、语气、疑问句式全保留，
只是不再指名道姓地冤枉人。证据不唯一（多人说过同一句）就不动，只记账。

宁可不改，也不乱改：不用分词器、不用模型、不做同义推断，
只认上下文里真实出现过的四字片段。
"""
import re

# 上下文行：[19:25:27] 阿戊: 还有一个每次都一键跟随的小朋友
_LINE = re.compile(r"^\[\d{0,2}:?\d{0,2}(?::\d{2})?\]\s*([^:：]{1,80})[:：]\s?(.*)$")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{4,}")
_BOT_MARK = "（你"
# 名字紧跟着「说法的片段」才算归错；超过这个距离就当两句不相干的话。
_ATTRIBUTION_WINDOW = 12
_VAGUE = "某位"


def parse_context(context_text):
    """把上下文拆成 [(名字, 那句原话)]，跳过她自己说的话。"""
    rows = []
    for line in str(context_text or "").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        name, said = m.group(1).strip(), m.group(2)
        if not said.strip():
            continue
        rows.append((name, said))
    return rows


def _quadgrams(text):
    out = set()
    for run in _CJK_RUN.findall(str(text or "")):
        for i in range(len(run) - 3):
            out.add(run[i:i + 4])
    return out


def ownership(rows):
    """四字片段 -> 说过它的唯一那个人。多人说过的不算数。"""
    seen = {}
    for name, said in rows:
        if _BOT_MARK in name:
            continue
        for gram in _quadgrams(said):
            seen.setdefault(gram, set()).add(name)
    return {gram: next(iter(names)) for gram, names in seen.items() if len(names) == 1}


def audit(reply, context_text):
    """返回 [(被安错的名字, 真正说过的人, 片段, 位置)]。"""
    rows = parse_context(context_text)
    names = {name for name, _ in rows if _BOT_MARK not in name}
    owned = ownership(rows)
    text = str(reply or "")
    found = []
    for name in names:
        start = text.find(name)
        while start >= 0:
            window = text[start + len(name):start + len(name) + _ATTRIBUTION_WINDOW + 4]
            for gram, owner in owned.items():
                if owner != name and gram in window:
                    found.append((name, owner, gram, start))
                    break
            start = text.find(name, start + 1)
    return found


def soften(reply, mismatches):
    """把被安错的名字降级成「某位」，其余一个字不动。"""
    text = str(reply or "")
    wrong = []
    for name, _owner, _gram, pos in mismatches:
        if name not in wrong:
            wrong.append(name)
    if not wrong:
        return text, []
    for name in sorted(wrong, key=len, reverse=True):
        text = text.replace(name, _VAGUE)
    return text, wrong


def cross_check(reply, context_text, state=None):
    """收口错误的指名；上下文构建器返回元组时只取其文本部分。"""
    if isinstance(context_text, tuple):
        context_text = context_text[0] if context_text else ""
    mismatches = audit(reply, context_text)
    text, softened = soften(reply, mismatches) if mismatches else (str(reply or ""), [])
    issues = ["speaker_swap"] if mismatches else []
    if (state and (state.get('correction') or {}).get('status') in
            ('supported_correction','ambiguous') and
            re.search(r'(?:你|主人)(?:[^，,。！？!?\n]{0,24})(?:理亏|晒糊涂|嫌热|嫌晒)|理亏还',text)):
        issues.append('unsupported_claim')
        text=re.sub(r'[^。！？!?\n]*(?:理亏|晒糊涂|嫌热|嫌晒)[^。！？!?\n]*[。！？!?]?', '', text).strip()
    rows = parse_context(context_text)
    humans = [(name, said) for name, said in rows if "（你" not in name]
    bot_said = " ".join(said for name, said in rows if "（你" in name)
    self_admission = bool(re.search(r'(?:我|我)[^，,。！？!?\n]{0,24}(?:嫌热|嫌晒|热得离谱)', str(reply or '')))
    if humans and not self_admission and re.search(r"嫌热|嫌晒|热得离谱", str(reply or "")):
        human_said = " ".join(said for _, said in humans)
        if re.search(r"热得离谱|嫌热|好热|太热", bot_said) and not re.search(
                r"热得离谱|嫌热|好热|太热", human_said):
            if "unsupported_claim" not in issues:
                issues.append("unsupported_claim")
            text = re.sub(r"[^。！？!?\n]*(?:嫌热|嫌晒|热得离谱)[^。！？!?\n]*[。！？!?]?", "", text).strip()
            text = re.sub(r"^(?:[，,、]\s*)+", "", text)
    return {"text": text, "mismatches": mismatches, "softened": softened, "issues": issues}