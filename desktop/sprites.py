"""桌宠帧图：皮肤 → 动作 → 文件。

两套素材都在本机、都不分发：
- fish（默认，清漪）：帧抽自 shenliuyu18/dafeiyu-pet 的 VP9-alpha webm，
  12 个语义状态、全帧抽取（待机 241 帧），按源 24fps 原速放，
  播放节拍写在 assets/fish/manifest.json 里。
- maid：MIT 许可的 mdd111/deepseek-dafeiyu-pet 帧图（见 assets/LICENSE.deepseek-dafeiyu-pet）。
"""
from __future__ import annotations

import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
SKIN_ROOT = {"fish": ASSETS / "fish", "maid": ASSETS / "sprites" / "maid"}
SIZES = (122, 245, 306)
DEFAULT_SKIN = "fish"

# 清漪的状态目录名就是这些语义名（抽帧时按同一张表生成）
FISH_STATES = ("idle", "talk", "think", "working", "done", "fail", "file",
               "confirm", "browse", "sleep", "drag", "yawn")

# maid 那套用的是原项目的动作目录名，需要一次语义翻译
MAID_ACTION = {
    "idle": "idle", "talk": "start", "think": "tool_read", "browse": "tool_web",
    "working": "working", "done": "done", "fail": "error", "confirm": "approval",
    "file": "tool_edit", "sing": "sing", "sleep": "idle", "drag": "start",
    "yawn": "idle",
}


def _manifest() -> dict:
    p = SKIN_ROOT["fish"] / "manifest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("states", {})
    except (OSError, ValueError):
        return {}


def pick_size(px: int) -> int:
    return min(SIZES, key=lambda s: abs(s - px))


def frames(state: str, size: int | None = None, skin: str = DEFAULT_SKIN) -> list[Path]:
    root = SKIN_ROOT.get(skin) or SKIN_ROOT[DEFAULT_SKIN]
    if skin == "fish" or not (root / MAID_ACTION.get(state, "idle")).exists():
        d = root / (state if (root / state).exists() else "idle")
        return sorted(p for p in d.glob("*.png"))
    d = root / MAID_ACTION.get(state, "idle")
    px = size or 245
    out = sorted(d.glob(f"*_{px}.png"), key=lambda p: int(p.stem.split("_")[0]))
    if out:
        return out
    return sorted(d.glob(f"*_{pick_size(px)}.png"), key=lambda p: int(p.stem.split("_")[0]))


def delay_ms(state: str, skin: str = DEFAULT_SKIN) -> int:
    if skin != "fish":
        return 260
    entry = _manifest().get(state) or {}
    return int(entry.get("delay_ms") or 150)


def available(skin: str = DEFAULT_SKIN) -> dict[str, int]:
    root = SKIN_ROOT.get(skin) or SKIN_ROOT[DEFAULT_SKIN]
    if not root.exists():
        return {}
    if skin == "fish":
        return {p.name: len(list(p.glob("*.png"))) for p in sorted(root.iterdir())
                if p.is_dir()}
    return {p.name: len(list(p.glob("*.png"))) for p in sorted(root.iterdir()) if p.is_dir()}
