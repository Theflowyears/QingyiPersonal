"""本地状态：路径配置、已点头的套餐、别吵、每天主动次数。

只写 desktop/state/ 一个目录，删掉就等于重置，不碰用户的 MAA 配置。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULTS = {
    "maa_dir": "",
    "emulator_path": "",
    "adb_path": "",
    "address": "",
    "pet_size": 245,
    "pet_skin": "fish",
    "quiet_until": 0.0,
    "bubble_day": "",
    "bubble_count": 0,
    "approved": {},          # task -> params 快照
    "approved_fingerprint": "",
    "approved_at": 0.0,
    "bubble_last_at": 0.0,
}

BUBBLE_LIMIT = 6
BUBBLE_GAP = 1200.0  # 两次主动开口至少隔 20 分钟


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "config.json"
        self.data = dict(DEFAULTS)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> dict:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        return self.data

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def set(self, **kw) -> None:
        self.data.update(kw)
        self.save()

    def __getitem__(self, k):
        return self.data.get(k, DEFAULTS.get(k))

    # ---------- 套餐授权 ----------
    def fingerprint(self) -> str:
        return str(self.data.get("approved_fingerprint") or "")

    def approved(self) -> dict:
        return dict(self.data.get("approved") or {})

    def approve(self, intents) -> str:
        from plan import fingerprint
        fp = fingerprint(intents)
        self.data["approved"] = {i.task: dict(i.params) for i in intents}
        self.data["approved_fingerprint"] = fp
        self.data["approved_at"] = time.time()
        self.save()
        return fp

    # ---------- 别吵 / 气泡配额 ----------
    def quiet(self) -> bool:
        return float(self.data.get("quiet_until") or 0) > time.time()

    def mute_today(self) -> None:
        day = time.strftime("%Y-%m-%d")
        end = time.mktime(time.strptime(day + " 23:59:59", "%Y-%m-%d %H:%M:%S"))
        self.data["quiet_until"] = end
        self.save()

    def can_bubble(self, now: float | None = None, last_at: float | None = None) -> tuple[bool, str]:
        now = now if now is not None else time.time()
        if last_at is None:
            last_at = self.last_bubble_at()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.quiet():
            return False, "今天设了别吵"
        if int(self.data.get("bubble_count") or 0) >= BUBBLE_LIMIT and self.data.get("bubble_day") == day:
            return False, f"今天已经主动开口 {BUBBLE_LIMIT} 次"
        if now - float(last_at or 0) < BUBBLE_GAP:
            return False, f"离上次主动开口不到 {int(BUBBLE_GAP // 60)} 分钟"
        return True, ""

    def note_bubble(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.data.get("bubble_day") != day:
            self.data["bubble_day"], self.data["bubble_count"] = day, 0
        self.data["bubble_count"] = int(self.data.get("bubble_count") or 0) + 1
        self.data["bubble_last_at"] = now
        self.save()

    def last_bubble_at(self) -> float:
        return float(self.data.get("bubble_last_at") or 0)
