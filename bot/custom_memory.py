"""自定义记忆：主人在后台亲手写下的、要求她一定记住的事。

与自动记忆的区别：
- 自动记忆（member_profiles / group_memory）由对话提炼，会变动；
- 这里的内容是主人直接写的，优先级最高，原样注入提示词，不参与压缩。

文件：data/memory/custom_memory.json
    {"entries": [{"id": "ab12cd34", "text": "...", "ts": 1730...}]}
"""
import json
import os
import secrets
import time

_ROOT = os.getenv("DATA_DIR", "./data")
_FILE = os.path.join(_ROOT, "memory", "custom_memory.json")
MAX_ENTRIES = int(os.getenv("CUSTOM_MEMORY_MAX", "60"))
MAX_CHARS = int(os.getenv("CUSTOM_MEMORY_MAX_CHARS", "1200"))
BLOCK_LIMIT = int(os.getenv("CUSTOM_MEMORY_BLOCK_CHARS", "900"))


def _ensure():
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    except Exception:
        pass


def load():
    """返回条目列表 [{"id","text","ts"}]。"""
    _ensure()
    try:
        if os.path.isfile(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
            items = data.get("entries")
            if isinstance(items, list):
                out = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    txt = str(it.get("text") or "").strip()
                    if not txt:
                        continue
                    out.append({
                        "id": str(it.get("id") or secrets.token_hex(4)),
                        "text": txt[:300],
                        "ts": float(it.get("ts") or 0),
                    })
                return out
    except Exception as e:
        print("[自定义记忆] 读取失败 " + str(e)[:80], flush=True)
    return []


def save(entries):
    _ensure()
    clean = []
    for it in entries or []:
        txt = str((it or {}).get("text") or "").strip()
        if not txt:
            continue
        clean.append({
            "id": str((it or {}).get("id") or secrets.token_hex(4)),
            "text": txt[:300],
            "ts": float((it or {}).get("ts") or time.time()),
        })
    clean = clean[-MAX_ENTRIES:]
    try:
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": clean}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _FILE)
        return True
    except Exception as e:
        print("[自定义记忆] 保存失败 " + str(e)[:80], flush=True)
        return False


def add(text):
    txt = str(text or "").strip()
    if not txt:
        return None
    entries = load()
    rec = {"id": secrets.token_hex(4), "text": txt[:300], "ts": time.time()}
    entries.append(rec)
    save(entries)
    return rec


def remove(entry_id):
    entries = load()
    left = [x for x in entries if str(x.get("id")) != str(entry_id)]
    if len(left) == len(entries):
        return False
    save(left)
    return True


def build_block(max_chars=None):
    """注入提示词用。"""
    entries = load()
    if not entries:
        return ""
    limit = int(max_chars or BLOCK_LIMIT)
    lines = []
    used = 0
    for it in entries:
        row = "- " + str(it.get("text"))
        if used + len(row) > limit:
            break
        lines.append(row)
        used += len(row)
    if not lines:
        return ""
    return ("\n\n【主人亲口要你记住的事 · 最高优先级，必须遵守】\n"
            + "\n".join(lines)
            + "\n（这些是主人直接交代的，比你自己提炼的记忆更权威；自然遵守，别逐条背诵。）")
