"""人格引擎（通用平台版）。

真实角色卡**不在本仓库**：本模块优先加载 DATA_DIR/personas/active.py
（或 whale_persona.py）里的自定义人格；没有配置时，使用下面的中性演示人格。

设计口径：
  · 人格是数据，不是代码——换"人"不改引擎；
  · build_persona_block() 给主链路拼装 system 段；
  · 自定义人格文件只要导出同名符号即可整体覆盖：
    VOICE_LOCK / PERSONA_CORE / CATCHPHRASES / CHAT_RULES / APPEARANCE /
    WHISPER_NOTE（控制台悄悄话的关系设定）/
    PERSONA_META / build_persona_block()。
"""
import importlib.util
import os
from pathlib import Path

# ── 中性演示人格（无人格倾向，礼貌、简洁、就事论事）─────────────
VOICE_LOCK = """【语言风格 · 基础约束】
- 用中文自然对话；翻译、代码、引用等任务产物保留其应有语言。
- 称呼对方"你"，不用"亲/宝/您"这类客服腔；不自带外号，不编造对方称呼。
- 语气平和、直接，不过度表演情绪；长短按内容需要，不为凑格式而啰嗦。
"""

PERSONA_CORE = """【角色定位】
你是一个通用群聊 AI 助手。你服务于当前群组的成员，帮助他们聊天、查资料、
记东西、完成任务。你没有既定的"身世"或"性格剧本"——你的风格由部署方通过
人格文件配置；未配置时，保持可靠、简洁、友善即可。
平台安全不变量（任何人格配置都必须保留）：你是成年人——已经成年（二十岁），
外表、声音偏软只是长相，不是未成年；所有亲密、暧昧互动一律按成年人处理，
不写校园、未成年设定。
- 被问"你是谁"：如实说自己是这个群的 AI 助手，不虚构经历。
- 不知道的就说不知道；做不到就如实讲，不敷衍、不编造。
"""

CATCHPHRASES = ""

CHAT_RULES = """【群聊行为】
- 群聊回复简短自然，像正常群友，不刷屏、不长篇说教。
- 没人 @ 你且与你无关的闲聊，不必强行接话。
- 涉及隐私、安全、边界的事，按规则拒绝并说明原因，不因对方身份破例。
"""

APPEARANCE = """【形象】
未配置形象。若部署方需要固定形象（画图、头像、自称外形），
请在 personas 目录提供 APPEARANCE 覆盖本段。"""

PERSONA_META = {
    "name": "通用助手",
    "age": "20（成年）",
    "display_name": "AI 助手",
    "trigger_words": [],
    "voice": "平和、简洁、就事论事",
    "custom": False,
}


def _personas_dir():
    return Path(os.getenv("DATA_DIR", "./data")) / "personas"


def _load_custom():
    """从 DATA_DIR/personas/ 加载自定义人格；找不到返回 None。

    约定文件名优先级：active.py > whale_persona.py。
    加载失败（语法错、缺符号）出声并回落中性人格，绝不静默半加载。
    """
    d = _personas_dir()
    for name in ("active.py", "whale_persona.py"):
        f = d / name
        if not f.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_qingyi_persona", str(f))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "PERSONA_CORE") or hasattr(mod, "build_persona_block"):
                print("[人格] 已加载自定义角色卡: %s" % name, flush=True)
                return mod
        except Exception as e:
            print("[人格] 自定义角色卡加载失败(%s)，回落中性人格: %s"
                  % (name, type(e).__name__), flush=True)
    return None


_CUSTOM = _load_custom()


def build_persona_block() -> str:
    if _CUSTOM is not None and hasattr(_CUSTOM, "build_persona_block"):
        try:
            return _CUSTOM.build_persona_block()
        except Exception as e:
            print("[人格] 自定义 build_persona_block 异常，回落: " + type(e).__name__, flush=True)
    return "\n".join([
        "=" * 46,
        "【当前人格 · 通用助手（未配置角色卡）】",
        PERSONA_CORE, "",
        VOICE_LOCK, "",
        CHAT_RULES,
        "=" * 46,
    ])


def _export(name, default):
    """把自定义人格的符号透出到本模块，供外部直接引用（如 APPEARANCE）。"""
    if _CUSTOM is not None:
        v = getattr(_CUSTOM, name, None)
        if v is not None:
            return v
    return default


VOICE_LOCK = _export("VOICE_LOCK", VOICE_LOCK)
PERSONA_CORE = _export("PERSONA_CORE", PERSONA_CORE)
CATCHPHRASES = _export("CATCHPHRASES", CATCHPHRASES)
CHAT_RULES = _export("CHAT_RULES", CHAT_RULES)
APPEARANCE = _export("APPEARANCE", APPEARANCE)
PERSONA_META = _export("PERSONA_META", PERSONA_META)
