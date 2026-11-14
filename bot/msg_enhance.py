"""
消息增强模块 - 表情包发送、撤回消息、@人解析
"""

import re
import json
import uuid
import asyncio
from typing import Optional


# ============================================================
# QQ 表情映射表
# AI 回复中包含这些关键词时，自动转为 QQ 表情
# ============================================================

EMOJI_MAP = {
    # 常用表情
    "[微笑]": {"type": "face", "data": {"id": "0"}},
    "[撇嘴]": {"type": "face", "data": {"id": "1"}},
    "[色]": {"type": "face", "data": {"id": "2"}},
    "[发呆]": {"type": "face", "data": {"id": "3"}},
    "[得意]": {"type": "face", "data": {"id": "4"}},
    "[流泪]": {"type": "face", "data": {"id": "5"}},
    "[害羞]": {"type": "face", "data": {"id": "6"}},
    "[闭嘴]": {"type": "face", "data": {"id": "7"}},
    "[睡]": {"type": "face", "data": {"id": "8"}},
    "[大哭]": {"type": "face", "data": {"id": "9"}},
    "[尴尬]": {"type": "face", "data": {"id": "10"}},
    "[发怒]": {"type": "face", "data": {"id": "11"}},
    "[调皮]": {"type": "face", "data": {"id": "12"}},
    "[呲牙]": {"type": "face", "data": {"id": "13"}},
    "[惊讶]": {"type": "face", "data": {"id": "14"}},
    "[难过]": {"type": "face", "data": {"id": "15"}},
    "[酷]": {"type": "face", "data": {"id": "16"}},
    "[冷汗]": {"type": "face", "data": {"id": "17"}},
    "[抓狂]": {"type": "face", "data": {"id": "18"}},
    "[吐]": {"type": "face", "data": {"id": "19"}},
    "[偷笑]": {"type": "face", "data": {"id": "20"}},
    "[可爱]": {"type": "face", "data": {"id": "21"}},
    "[白眼]": {"type": "face", "data": {"id": "22"}},
    "[傲慢]": {"type": "face", "data": {"id": "23"}},
    "[饥饿]": {"type": "face", "data": {"id": "24"}},
    "[困]": {"type": "face", "data": {"id": "25"}},
    "[惊恐]": {"type": "face", "data": {"id": "26"}},
    "[流汗]": {"type": "face", "data": {"id": "27"}},
    "[憨笑]": {"type": "face", "data": {"id": "28"}},
    "[大兵]": {"type": "face", "data": {"id": "29"}},
    "[奋斗]": {"type": "face", "data": {"id": "30"}},
    "[咒骂]": {"type": "face", "data": {"id": "31"}},
    "[疑问]": {"type": "face", "data": {"id": "32"}},
    "[嘘]": {"type": "face", "data": {"id": "33"}},
    "[晕]": {"type": "face", "data": {"id": "34"}},
    "[折磨]": {"type": "face", "data": {"id": "35"}},
    "[衰]": {"type": "face", "data": {"id": "36"}},
    "[骷髅]": {"type": "face", "data": {"id": "37"}},
    "[敲打]": {"type": "face", "data": {"id": "38"}},
    "[再见]": {"type": "face", "data": {"id": "39"}},
    "[擦汗]": {"type": "face", "data": {"id": "40"}},
    "[抠鼻]": {"type": "face", "data": {"id": "41"}},
    "[鼓掌]": {"type": "face", "data": {"id": "42"}},
    "[糗大了]": {"type": "face", "data": {"id": "43"}},
    "[坏笑]": {"type": "face", "data": {"id": "44"}},
    "[左哼哼]": {"type": "face", "data": {"id": "45"}},
    "[右哼哼]": {"type": "face", "data": {"id": "46"}},
    "[哈欠]": {"type": "face", "data": {"id": "47"}},
    "[鄙视]": {"type": "face", "data": {"id": "48"}},
    "[委屈]": {"type": "face", "data": {"id": "49"}},
    "[快哭了]": {"type": "face", "data": {"id": "50"}},
    "[阴险]": {"type": "face", "data": {"id": "51"}},
    "[亲亲]": {"type": "face", "data": {"id": "52"}},
    "[吓]": {"type": "face", "data": {"id": "53"}},
    "[可怜]": {"type": "face", "data": {"id": "54"}},
    "[菜刀]": {"type": "face", "data": {"id": "55"}},
    "[西瓜]": {"type": "face", "data": {"id": "56"}},
    "[啤酒]": {"type": "face", "data": {"id": "57"}},
    "[篮球]": {"type": "face", "data": {"id": "58"}},
    "[乒乓]": {"type": "face", "data": {"id": "59"}},
    "[咖啡]": {"type": "face", "data": {"id": "60"}},
    "[饭]": {"type": "face", "data": {"id": "61"}},
    "[猪头]": {"type": "face", "data": {"id": "62"}},
    "[玫瑰]": {"type": "face", "data": {"id": "63"}},
    "[凋谢]": {"type": "face", "data": {"id": "64"}},
    "[示爱]": {"type": "face", "data": {"id": "65"}},
    "[爱心]": {"type": "face", "data": {"id": "66"}},
    "[心碎]": {"type": "face", "data": {"id": "67"}},
    "[蛋糕]": {"type": "face", "data": {"id": "68"}},
    "[闪电]": {"type": "face", "data": {"id": "69"}},
    "[炸弹]": {"type": "face", "data": {"id": "70"}},
    "[刀]": {"type": "face", "data": {"id": "71"}},
    "[足球]": {"type": "face", "data": {"id": "72"}},
    "[瓢虫]": {"type": "face", "data": {"id": "73"}},
    "[便便]": {"type": "face", "data": {"id": "74"}},
    "[月亮]": {"type": "face", "data": {"id": "75"}},
    "[太阳]": {"type": "face", "data": {"id": "76"}},
    "[礼物]": {"type": "face", "data": {"id": "77"}},
    "[拥抱]": {"type": "face", "data": {"id": "78"}},
    "[强]": {"type": "face", "data": {"id": "79"}},
    "[弱]": {"type": "face", "data": {"id": "80"}},
    "[握手]": {"type": "face", "data": {"id": "81"}},
    "[胜利]": {"type": "face", "data": {"id": "82"}},
    "[抱拳]": {"type": "face", "data": {"id": "83"}},
    "[勾引]": {"type": "face", "data": {"id": "84"}},
    "[拳头]": {"type": "face", "data": {"id": "85"}},
    "[差劲]": {"type": "face", "data": {"id": "86"}},
    "[爱你]": {"type": "face", "data": {"id": "87"}},
    "[NO]": {"type": "face", "data": {"id": "88"}},
    "[OK]": {"type": "face", "data": {"id": "89"}},
    "[跳跳]": {"type": "face", "data": {"id": "92"}},
    "[发抖]": {"type": "face", "data": {"id": "94"}},
    "[怄火]": {"type": "face", "data": {"id": "95"}},
    "[转圈]": {"type": "face", "data": {"id": "96"}},
    "[嘿哈]": {"type": "face", "data": {"id": "100"}},
    "[捂脸]": {"type": "face", "data": {"id": "101"}},
    "[奸笑]": {"type": "face", "data": {"id": "102"}},
    "[机智]": {"type": "face", "data": {"id": "103"}},
    "[皱眉]": {"type": "face", "data": {"id": "104"}},
    "[耶]": {"type": "face", "data": {"id": "105"}},
    "[吃瓜]": {"type": "face", "data": {"id": "106"}},
    "[加油]": {"type": "face", "data": {"id": "107"}},
    "[汗]": {"type": "face", "data": {"id": "108"}},
    "[天啊]": {"type": "face", "data": {"id": "109"}},
    "[社会社会]": {"type": "face", "data": {"id": "110"}},
    "[旺柴]": {"type": "face", "data": {"id": "111"}},
    "[好的]": {"type": "face", "data": {"id": "112"}},
    "[哇]": {"type": "face", "data": {"id": "113"}},
    "[打脸]": {"type": "face", "data": {"id": "114"}},
    "[翻白眼]": {"type": "face", "data": {"id": "115"}},
    "[666]": {"type": "face", "data": {"id": "116"}},
    "[让我看看]": {"type": "face", "data": {"id": "117"}},
    "[叹气]": {"type": "face", "data": {"id": "118"}},
    "[苦涩]": {"type": "face", "data": {"id": "119"}},
    "[裂开]": {"type": "face", "data": {"id": "120"}},
}

# 自然语言 -> 表情映射（AI 可能输出的自然表达）
NATURAL_EMOJI_MAP = {
    "哈哈": "[呲牙]",
    "哈哈哈": "[呲牙]",
    "嘻嘻": "[偷笑]",
    "呜呜": "[流泪]",
    "呜呜呜": "[大哭]",
    "啊啊啊": "[抓狂]",
    "嗯嗯": "[OK]",
    "好的": "[好的]",
    "ok": "[OK]",
    "OK": "[OK]",
    "666": "[666]",
    "牛": "[强]",
    "厉害": "[强]",
    "谢谢": "[抱拳]",
    "感谢": "[抱拳]",
    "拜拜": "[再见]",
    "再见": "[再见]",
    "晚安": "[月亮]",
    "早安": "[太阳]",
    "爱你": "[爱你]",
    "比心": "[爱心]",
    "抱抱": "[拥抱]",
    "生气": "[发怒]",
    "无语": "[流汗]",
    "尴尬": "[尴尬]",
    "害羞": "[害羞]",
    "开心": "[可爱]",
    "难过": "[难过]",
    "惊讶": "[惊讶]",
    "震惊": "[惊恐]",
    "疑惑": "[疑问]",
    "思考": "[疑问]",
    "点赞": "[强]",
    "鼓掌": "[鼓掌]",
    "加油": "[加油]",
    "吃瓜": "[吃瓜]",
    "看戏": "[吃瓜]",
    "doge": "[旺柴]",
    "狗头": "[旺柴]",
    "捂脸": "[捂脸]",
    "笑哭": "[捂脸]",
    "裂开": "[裂开]",
    "社死": "[裂开]",
}


def parse_emojis(text: str) -> list[dict]:
    """
    解析文本中的表情标记，返回 OneBot v11 消息段列表
    
    支持：
    1. [微笑] 格式的直接表情标记
    2. 自然语言表情（如 "哈哈" -> [呲牙]）
    """
    segments = []
    
    # 先处理直接表情标记 [xxx]
    pattern = r"\[(?:[^\]]+)\]"
    parts = re.split(f"({pattern})", text)
    
    for part in parts:
        if not part:
            continue
        
        if part in EMOJI_MAP:
            segments.append(EMOJI_MAP[part])
        else:
            # 检查是否包含自然语言表情
            remaining = part
            found_natural = False
            for natural, emoji_tag in sorted(NATURAL_EMOJI_MAP.items(), key=lambda x: -len(x[0])):
                if natural in remaining:
                    # 分割并插入表情
                    idx = remaining.index(natural)
                    before = remaining[:idx]
                    after = remaining[idx + len(natural):]
                    
                    if before:
                        segments.append({"type": "text", "data": {"text": before}})
                    segments.append(EMOJI_MAP[emoji_tag])
                    remaining = after
                    found_natural = True
            
            if remaining:
                segments.append({"type": "text", "data": {"text": remaining}})
    
    # 如果没有解析出任何内容，返回纯文本
    if not segments:
        segments.append({"type": "text", "data": {"text": text}})
    
    return segments


def parse_at_mentions(text: str, group_members: dict[str, str] = None) -> list[dict]:
    """
    解析文本中的 @提及，返回 OneBot v11 消息段列表
    
    支持格式：
    - @QQ号
    - @昵称
    """
    segments = []
    # 匹配 @后面跟数字或中文/英文昵称
    pattern = r"@([\d]+|[^\s@]{1,20})"
    parts = re.split(f"({pattern})", text)
    
    i = 0
    while i < len(parts):
        part = parts[i]
        
        if part.startswith("@") and i + 1 < len(parts):
            target = parts[i + 1]
            i += 2
            
            # 如果是纯数字，当作 QQ 号
            if target.isdigit():
                segments.append({"type": "at", "data": {"qq": target}})
            # 如果有群成员映射，尝试按昵称查找
            elif group_members and target in group_members:
                segments.append({"type": "at", "data": {"qq": group_members[target]}})
            else:
                # 无法识别，保留原文
                segments.append({"type": "text", "data": {"text": f"@{target}"}})
        else:
            if part:
                segments.append({"type": "text", "data": {"text": part}})
            i += 1
    
    if not segments:
        segments.append({"type": "text", "data": {"text": text}})
    
    return segments


async def send_enhanced_msg(ws, action: str, params: dict, text: str, 
                             group_members: dict = None, send_lock=None):
    """
    发送增强消息：自动解析表情和 @提及
    
    Args:
        ws: WebSocket 连接
        action: OneBot action (send_group_msg / send_private_msg)
        params: 基础参数 (group_id / user_id)
        text: 要发送的文本
        group_members: 群成员昵称映射 {昵称: QQ号}
        send_lock: 发送锁
    """
    # 先解析 @提及
    at_segments = parse_at_mentions(text, group_members)
    
    # 对每个文本段再解析表情
    final_segments = []
    for seg in at_segments:
        if seg["type"] == "text":
            emoji_segments = parse_emojis(seg["data"]["text"])
            final_segments.extend(emoji_segments)
        else:
            final_segments.append(seg)
    
    payload = {
        "action": action,
        "params": {
            **params,
            "message": final_segments,
            "auto_escape": False,
        },
        "echo": str(uuid.uuid4()),
    }
    
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))


async def delete_message(ws, message_id: int, send_lock=None) -> bool:
    """
    撤回消息
    
    Args:
        ws: WebSocket 连接
        message_id: 要撤回的消息 ID
        send_lock: 发送锁
    
    Returns:
        是否成功
    """
    payload = {
        "action": "delete_msg",
        "params": {
            "message_id": int(message_id),
        },
        "echo": str(uuid.uuid4()),
    }
    
    try:
        if send_lock:
            async with send_lock:
                await ws.send(json.dumps(payload, ensure_ascii=False))
        else:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as e:
        print(f"[撤回失败] message_id={message_id} error={e}", flush=True)
        return False


def get_emoji_list() -> list[dict]:
    """获取所有可用表情列表（供 Web UI 展示）"""
    result = []
    for tag, segment in EMOJI_MAP.items():
        result.append({
            "tag": tag,
            "face_id": int(segment["data"]["id"]),
        })
    return result


# ============================================================
# 主动接口：引用回复 / 收藏(精华) / 读取消息 / 表情包 / 点赞
# 这些通过 OneBot v11 action 直接调用 NapCat（已确认其支持）。
# ============================================================

async def call_action(ws, action, params=None, send_lock=None, timeout=15.0):
    """通用请求-响应：发送 action 并等待带相同 echo 的回包。

    需要调用方在同一 ws 上不会并发抢读；机器人主链路里事件读取由 runtime 循环负责，
    因此这里用一个独立的 echo 匹配 + 超时保护，尽量只在"发命令拿结果"时用。
    """
    import asyncio
    echo = str(uuid.uuid4())
    payload = {
        "action": action,
        "params": params or {},
        "echo": echo,
    }
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    return echo


async def reply_group_msg(ws, group_id, user_id, text, quote_message_id=None,
                          mention=True, send_lock=None):
    """群聊回复，可带"引用回复"(reply 段)。

    Args:
        quote_message_id: 被引用消息的 message_id；None 则不引用。
        其余同 send_enhanced_msg。
    """
    segments = []
    if quote_message_id is not None:
        # OneBot v11 reply 段：id 为被引用消息 id
        segments.append({"type": "reply", "data": {"id": str(quote_message_id)}})
    if mention and user_id:
        segments.append({"type": "at", "data": {"qq": str(user_id)}})
        segments.append({"type": "text", "data": {"text": "\n"}})
    # 解析表情与 @
    at_segments = parse_at_mentions(text)
    for seg in at_segments:
        if seg["type"] == "text":
            segments.extend(parse_emojis(seg["data"]["text"]))
        else:
            segments.append(seg)

    payload = {
        "action": "send_group_msg",
        "params": {
            "group_id": int(group_id),
            "message": segments,
            "auto_escape": False,
        },
        "echo": str(uuid.uuid4()),
    }
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    return True


async def set_essence(ws, message_id, send_lock=None):
    """把某条消息设为群精华（收藏）。仅群主/管理员账号可成功。"""
    payload = {
        "action": "set_essence_msg",
        "params": {"message_id": int(message_id)},
        "echo": str(uuid.uuid4()),
    }
    try:
        if send_lock:
            async with send_lock:
                await ws.send(json.dumps(payload, ensure_ascii=False))
        else:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as e:
        print(f"[收藏失败] message_id={message_id} error={e}", flush=True)
        return False


async def unset_essence(ws, message_id, send_lock=None):
    """取消群精华（取消收藏）。"""
    payload = {
        "action": "del_essence_msg",
        "params": {"message_id": int(message_id)},
        "echo": str(uuid.uuid4()),
    }
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    return True


async def like_message(ws, message_id, face_name="爱心", count=1, send_lock=None):
    """给某条消息点赞（表情回应）。face_name 走 QQ 内置回应表情名。"""
    payload = {
        "action": "set_msg_emoji_like",
        "params": {
            "message_id": int(message_id),
            "emojiType": "1",
            "emojiId": str(_EMOJI_NAME_TO_ID.get(face_name, "66")),
            "set": True,
        },
        "echo": str(uuid.uuid4()),
    }
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    return True


# 常见回应表情名 -> emojiId（NapCat/QQ 内置）
_EMOJI_NAME_TO_ID = {
    "爱心": "66", "点赞": "79", "强": "79", "OK": "89",
    "流泪": "5", "大笑": "13", "捂脸": "101", "抱拳": "83",
}


async def send_face(ws, chat_type, target_id, face_id, send_lock=None):
    """发送 QQ 内置小表情（face 元素），face_id 见 EMOJI_MAP。
    chat_type: "group" | "private"
    """
    action = "send_group_msg" if chat_type == "group" else "send_private_msg"
    key = "group_id" if chat_type == "group" else "user_id"
    payload = {
        "action": action,
        "params": {
            key: int(target_id),
            "message": [{"type": "face", "data": {"face_id": str(int(face_id))}}],
        },
        "echo": str(uuid.uuid4()),
    }
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    return True


async def send_sticker_image(ws, chat_type, target_id, image, text="", send_lock=None):
    """发送表情包/图片。image 可为 URL、本地路径或 base64://xxx。
    可选附带一句文字。
    """
    action = "send_group_msg" if chat_type == "group" else "send_private_msg"
    key = "group_id" if chat_type == "group" else "user_id"
    segments = []
    if text:
        segments.extend(parse_emojis(text))
    img_data = {"file": image}
    segments.append({"type": "image", "data": img_data})
    payload = {
        "action": action,
        "params": {key: int(target_id), "message": segments},
        "echo": str(uuid.uuid4()),
    }
    if send_lock:
        async with send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
    else:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    return True


def build_reply_payload(group_id, user_id, text, quote_message_id=None):
    """纯函数版：构造引用回复 payload（便于单测，不发送）。"""
    segments = []
    if quote_message_id is not None:
        segments.append({"type": "reply", "data": {"id": str(quote_message_id)}})
    if user_id:
        segments.append({"type": "at", "data": {"qq": str(user_id)}})
    segments.extend(parse_emojis(text))
    return {
        "action": "send_group_msg",
        "params": {"group_id": int(group_id), "message": segments},
    }
