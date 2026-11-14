"""发送前硬闸：绝不让控制标记 / 注入的提示片段 / 聊天记录元信息被当成她的话发出去。

真实事故（2026-11-09）：她在群里连着发出
    [[查: 2026年11月9日 重庆 运势]]
——控制标记本该只触发动作，结果被当消息发了。以及注入用的提示片段被回显。
放在独立模块，便于单独回归（tests/test_outgoing_sanitize.py），发送函数统一调用。
"""
import re

# [[查: …]] / [[沉默]] / [[斗图]] / [[不查]] / [[不补]] / [[不说]] / [[无感]] …
_CONTROL_MARK = re.compile(r"\[\[[^\]]{0,60}?\]\]")

# 注入给模型的提示片段（本轮指令），她回显出来时一律剔掉，绝不外发。
# 只匹配带这些「明显是指令」的线索的方括号块，避免误伤正常的【…】。
_PROMPT_HINT = re.compile(
    r"【[^】]{0,60}(?:先别编|先别装懂|查到会再补|查完给你再答|现查才有准信|"
    r"系统查完|本轮先别|别报菜名|先接住|先自然接一句)[^】]{0,60}】"
)

_LEAD_WS = re.compile(r"[ \t]+")
_INNER_WS = re.compile(r"[ \t]{2,}")


def strip_control_markers(text):
    """去掉控制标记与注入提示；返回清理后的文本（可能为空串）。

    只压行内多余空格，**保留每行行首缩进**——长文/代码回复靠缩进，
    全局折叠空格会把「    return」压成「 return」破坏代码。
    """
    t = str(text or "")
    if not t:
        return ""
    cleaned = _PROMPT_HINT.sub("", _CONTROL_MARK.sub("", t))
    if cleaned == t:
        # 没有标记/提示片段：原样返回，一行空白都不碰（代码/长文靠空白）
        return t
    out = []
    for ln in cleaned.split("\n"):
        m = _LEAD_WS.match(ln)
        ind = m.group(0) if m else ""
        out.append(ind + _INNER_WS.sub(" ", ln[len(ind):]))
    return "\n".join(out).strip()