"""找到本机那套 MAA + 模拟器，并把它已有的配置读出来。

MAA 自更新会把目录名里的版本号换掉（MAA-v5.23.2 → MAA-v6.x），所以不能写死路径：
先按用户指过的路径认，认不出再扫各盘根目录找带 MaaCore.dll 的 MAA* 目录。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import string
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAA_DIR_RE = re.compile(r"^MAA[\w.\-]*$", re.I)
VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)", re.I)


def _version_key(path: Path):
    m = VERSION_RE.search(path.name)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_maa_install(preferred: str = "", roots=None) -> Path | None:
    """返回装着 MaaCore.dll 的 MAA 目录；preferred 命中就先用它。"""
    if preferred:
        p = Path(preferred)
        if (p / "MaaCore.dll").exists():
            return p
    roots = [Path(r) for r in roots] if roots else _default_roots()
    best = None
    for root in roots:
        try:
            entries = [e for e in root.iterdir() if e.is_dir() and MAA_DIR_RE.match(e.name)]
        except (OSError, PermissionError):
            continue
        for e in entries:
            if not (e / "MaaCore.dll").exists():
                continue
            if best is None or _version_key(e) > _version_key(best):
                best = e
    return best


def _default_roots():
    out = []
    for letter in string.ascii_uppercase:
        p = Path(letter + ":\\")
        if p.exists():
            out.append(p)
    home = Path.home()
    out += [home / "Downloads", home / "Desktop", home / "Program Files"]
    return out


@dataclass
class Connection:
    """连接一台模拟器需要的全部参数，默认全部来自用户自己的 MAA 配置。"""

    adb_path: str = ""
    address: str = "127.0.0.1:16384"
    config: str = "MuMuEmulator12"
    touch_mode: str = "MumuExtras"
    client_type: str = "Official"
    extras: dict = field(default_factory=dict)
    kill_adb_on_exit: bool = False
    adblite: bool = False
    source: str = "内置默认"

    def as_dict(self) -> dict:
        return {
            "adb_path": self.adb_path, "address": self.address, "config": self.config,
            "touch_mode": self.touch_mode, "client_type": self.client_type,
            "extras": self.extras, "kill_adb_on_exit": self.kill_adb_on_exit,
            "adblite": self.adblite, "source": self.source,
        }


def _dig(node, path):
    cur = node
    for key in path.split("/"):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def read_connection(gui_cfg: dict) -> Connection:
    """gui.new.json → Connection。缺键就退回上一级默认，并记下这条参数的来源。"""
    base = "Configurations/Default/Gui/ConnectSettings"
    conn = Connection()
    got = []
    adb = _dig(gui_cfg, base + "/AdbPath")
    if adb:
        conn.adb_path = str(adb)
        got.append("AdbPath")
    addr = _dig(gui_cfg, base + "/Address")
    if addr:
        conn.address = str(addr)
        got.append("Address")
    cfgname = _dig(gui_cfg, base + "/Config")
    if cfgname:
        conn.config = str(cfgname)
        got.append("Config")
    touch = _dig(gui_cfg, base + "/TouchMode")
    if touch:
        conn.touch_mode = str(touch)
        got.append("TouchMode")
    client = _dig(gui_cfg, "Configurations/Default/Gui/RuntimeSettings/ClientType")
    if client:
        conn.client_type = str(client)
        got.append("ClientType")
    extras = _dig(gui_cfg, base + "/Extras") or {}
    if isinstance(extras, dict):
        # AsstSetConnectionExtras 收的是 {"MuMuEmulator12": {...}}，内层必须是原样的 dict
        conn.extras = {k: v for k, v in extras.items()
                       if isinstance(v, (dict, str, int, float, bool))}
        if conn.extras:
            got.append("Extras")
    conn.kill_adb_on_exit = bool(_dig(gui_cfg, base + "/KillAdbOnExit") or False)
    conn.adblite = bool(_dig(gui_cfg, base + "/EnableAdbLite") or False)
    conn.source = "你的 MAA 配置：" + "、".join(got) if got else "内置默认（没读到 MAA 配置）"
    if conn.adb_path and not Path(conn.adb_path).exists():
        found = shutil.which("adb") or shutil.which("adb.exe")
        if found:
            conn.adb_path = found
            conn.source += "；配置里的 adb 不在，改用 PATH 里的"
    return conn


def read_task_queue(gui_cfg: dict) -> list[dict]:
    q = _dig(gui_cfg, "Configurations/Default/TaskQueue")
    return list(q) if isinstance(q, list) else []


def read_copilot_tasks(gui_cfg: dict) -> list[dict]:
    q = _dig(gui_cfg, "Configurations/Default/Copilot/TaskList")
    return list(q) if isinstance(q, list) else []


def gui_config_path(maa_root: Path) -> Path | None:
    """新版写 gui.new.json，老版只有 gui.json —— 两个都在时以 .new 为准（MAA 自己读它）。"""
    for name in ("gui.new.json", "gui.json"):
        p = maa_root / "config" / name
        if p.exists():
            return p
    return None


def load_gui_config(maa_root: Path) -> dict:
    p = gui_config_path(maa_root)
    if not p:
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}


def _child_env() -> dict:
    """给子进程清洗环境：MuMuManager 自己也是 Qt5 程序，
    继承 QT_QPA_PLATFORM / QT_PLUGIN_PATH 会让它找不到插件而弹错误框。"""
    env = dict(os.environ)
    for key in [k for k in env if k.upper().startswith("QT_")]:
        env.pop(key, None)
    return env


def _runquiet(cmd, **kwargs):
    kwargs.setdefault("env", _child_env())
    kwargs.setdefault("timeout", 15)
    return subprocess.run(cmd, **kwargs)


def mumu_manager(emulator_path: str) -> Path | None:
    if not emulator_path:
        return None
    p = Path(emulator_path) / "shell" / "MuMuManager.exe"
    return p if p.exists() else None


def emulator_running(emulator_path: str, index: int = 0, run=None) -> tuple[bool, str]:
    """问 MuMuManager 这台虚拟机在不在跑。字段名是实测的 is_android_started / is_process_started。"""
    exe = mumu_manager(emulator_path)
    if exe is None:
        return False, f"{emulator_path or '（没配模拟器路径）'} 下没找到 MuMuManager.exe"
    run = run or _runquiet
    try:
        out = run([str(exe), "info", "-v", str(index)], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"MuMuManager 调用失败 {type(exc).__name__}"
    text = (out.stdout or b"").decode("utf-8", "replace").strip()
    try:
        info = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, IndexError):
        return False, "MuMuManager 输出不是 JSON：" + text[:120].replace("\n", " ")
    if _flag(info.get("is_android_started")):
        return True, "安卓系统已起来"
    if _flag(info.get("is_process_started")):
        return False, "虚拟机进程在跑，但安卓系统还没起来"
    name = info.get("name") or f"第 {index} 台"
    return False, f"{name} 没开着"


def _flag(value) -> bool:
    return str(value).lower() in ("true", "1", "yes")


def adb_reachable(adb_path: str, address: str, run=None) -> tuple[bool, str]:
    """真正要紧的判据：MAA 用的那台 adb 能不能见到这个地址。

    注意别加 `-host` —— MuMu 自带的是 adb 34（1.0.41），不认这个选项，
    加了只会让每条理由都变成「adb 列表里没有」，把真原因盖掉。
    """
    if not adb_path or not Path(adb_path).exists():
        return False, f"adb 不在（{adb_path or '没配'}）"
    run = run or _runquiet
    try:
        got = run([adb_path, "connect", address], capture_output=True, timeout=15)
        listed = run([adb_path, "devices"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"adb 调用失败 {type(exc).__name__}"
    for line in (listed.stdout or b"").decode("utf-8", "replace").splitlines():
        if line.startswith(address):
            state = line[len(address):].strip()
            return (state == "device", state or "状态不明")
    text = ((got.stdout or b"") + (got.stderr or b"")).decode("utf-8", "replace")
    first = " ".join(x.strip() for x in text.splitlines() if x.strip())[:120]
    return False, f"连不上 {address}" + (f"：{first}" if first else "")


def device_state(conn, emulator_path: str, run=None) -> tuple[bool, str]:
    """设备闸门的口径：adb 通 = 能开工；不通就把 MuMu 和 adb 各自的情况都念出来。"""
    ok, why = adb_reachable(conn.adb_path, conn.address, run=run)
    if ok:
        return True, why
    _, mu = emulator_running(emulator_path, run=run)
    return False, f"{why}；MuMu：{mu}"
