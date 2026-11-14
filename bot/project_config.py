"""Public, environment-driven branding and endpoint configuration."""
import os


def csv_set(name, default=""):
    return {item.strip() for item in os.getenv(name, default).split(",") if item.strip()}


BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "QQ AI 助手").strip() or "QQ AI 助手"
BOT_PERSONA_NAME = os.getenv("BOT_PERSONA_NAME", BOT_DISPLAY_NAME).strip() or BOT_DISPLAY_NAME
BOT_PERSONA = os.getenv(
    "BOT_PERSONA",
    "你是群聊里的 AI 助手。说话自然、友善、简洁，不暴露内部实现。",
).strip()
BOT_TRIGGER_WORDS = csv_set("BOT_TRIGGER_WORDS", BOT_DISPLAY_NAME)

# ── 身份统一 ────────────────────────────────────────
# 机器人可能有三个名字：QQ 昵称、显示名、人设名（BOT_SELF_NAME/DISPLAY/PERSONA_NAME）——
# 三个都是同一个人。以前各处各写一个，结果她在上下文里认不出自己
# （看到"清漪: ..."当成别人说的，还反过来否认一遍）。
# 以后只认这一处：主名 BOT_SELF_NAME，别名全在 BOT_ALL_NAMES 里。
BOT_SELF_NAME = (os.getenv("BOT_SELF_NAME", "清漪").strip() or "清漪")


def bot_names():
    """她所有的名字，主名排第一。判"这句话是不是她自己说的"用它。"""
    names = [BOT_SELF_NAME, BOT_DISPLAY_NAME, BOT_PERSONA_NAME, "我", "清漪"]
    names.extend(sorted(BOT_TRIGGER_WORDS))
    seen, out = set(), []
    for n in names:
        t = str(n or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


BOT_ALL_NAMES = bot_names()

# ── 触发词热更新 ────────────────────────────────────
# 面板里改触发词曾经完全无效：读的是 whale_persona 里硬编码的那份，
# 写的是 data/persona.json（没人读），真正生效的是 config.env 里的
# BOT_TRIGGER_WORDS。三份数据互不相通，所以改完一刷新就"变回去了"。
#
# 现在统一到这条链：
#   面板保存 → data/persona_policy.json（triggers 字段）+ config.env
#            → refresh_from_policy() 把新词灌进 BOT_TRIGGER_WORDS / BOT_ALL_NAMES
#            → 正在跑的 bot 进程不用重启就能认新名字

_POLICY_PATH = None


def _policy_path():
    global _POLICY_PATH
    if _POLICY_PATH is None:
        base = os.getenv("DATA_DIR", "./data")
        _POLICY_PATH = os.path.join(base, "persona_policy.json")
    return _POLICY_PATH


def refresh_from_policy() -> bool:
    """从 persona_policy.json 重新读触发词。返回是否真的变了。"""
    global BOT_TRIGGER_WORDS, BOT_ALL_NAMES
    try:
        import json
        p = _policy_path()
        if not os.path.isfile(p):
            return False
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return False

    raw = d.get("triggers")
    if raw is None:
        return False
    if isinstance(raw, str):
        words = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    elif isinstance(raw, (list, tuple)):
        # 有人会用中文逗号写在一格里，这里也拆开
        words = []
        for x in raw:
            for t in str(x).replace("，", ",").split(","):
                t = t.strip()
                if t:
                    words.append(t)
    else:
        return False
    words = [w for w in words if w]
    if not words:
        return False

    new_set = set(words)
    if new_set == BOT_TRIGGER_WORDS:
        return False        # 没变化，别白改

    BOT_TRIGGER_WORDS = new_set
    BOT_ALL_NAMES = bot_names()
    # 同步给其他模块（有些地方直接 import 了这两个常量）
    try:
        import whale_persona
        whale_persona.PERSONA_META["trigger_words"] = bot_names()
    except Exception:
        pass
    print("[身份] 触发词已更新: %s" % ",".join(w for w in bot_names()), flush=True)
    return True


BLOCKED_GROUP_IDS = csv_set("BLOCKED_GROUP_IDS")
PUBLIC_BASE_URL = os.getenv("BOT_CONTROL_BASE_URL", "").strip().rstrip("/")


def api_url(path, explicit_env):
    explicit = os.getenv(explicit_env, "").strip()
    if explicit:
        return explicit
    if not PUBLIC_BASE_URL:
        raise RuntimeError(f"Set {explicit_env} or BOT_CONTROL_BASE_URL")
    return PUBLIC_BASE_URL + path
