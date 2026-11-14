"""命令行探针：把「她到底能不能摸到这台机器上的 MAA」测出来给人看。

    venv\\Scripts\\python.exe desktop\\probe_maa.py            # 只装载 + 连接，不动游戏
    venv\\Scripts\\python.exe desktop\\probe_maa.py --plan     # 顺便打印继承下来的套餐
    venv\\Scripts\\python.exe desktop\\probe_maa.py --run Award # 明确点名才真跑一条任务

不 --run 就绝不排队：连接本身不会点击屏幕。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from discovery import (Connection, adb_reachable, device_state, emulator_running,  # noqa: E402
                       find_maa_install, load_gui_config, read_connection, read_task_queue)
from maacore import MaaCore  # noqa: E402
from plan import build_intents, fingerprint  # noqa: E402
from store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="打印从 MAA 配置继承的套餐")
    ap.add_argument("--run", default="", help="真跑一条任务（StartUp/Award/Mall/…）")
    ap.add_argument("--seconds", type=float, default=90.0, help="等回调的秒数")
    args = ap.parse_args()

    store = Store(HERE / "state")
    root = find_maa_install(preferred=str(store["maa_dir"] or ""))
    print("MAA 目录:", root or "没找到")
    if root is None:
        return 2
    cfg = load_gui_config(root)
    conn = read_connection(cfg)
    print("连接配置来源:", conn.source)
    print("  adb    =", conn.adb_path)
    print("  address=", conn.address, " config=", conn.config, " touch=", conn.touch_mode)

    emu = str(store["emulator_path"] or
              (conn.extras.get("MuMuEmulator12", {}) or {}).get("EmulatorPath") or "")
    print("MuMu:", emulator_running(emu))
    print("adb :", adb_reachable(conn.adb_path, conn.address))
    print("设备闸门:", device_state(conn, emu))

    if args.plan:
        intents = build_intents(conn, cfg)
        print("\n套餐指纹:", fingerprint(intents))
        for i in intents:
            print(f"  {i.task:9s} tier={i.tier:7s} 队列里={'开' if i.enabled_in_maa else '关'} "
                  f"参数={json.dumps(i.params, ensure_ascii=False)[:150]}")
            print(f"  {'':9s} 来源={ {k: v for k, v in i.prov.items()} }  {i.note}")

    core = MaaCore(dll_dir=root, user_dir=HERE / "state" / "probe-user")
    print("\nMaaCore 版本:", core.load())
    core.set_instance_option(2, conn.touch_mode)
    for name, val in (conn.extras or {}).items():
        try:
            core.set_connection_extras(name, val)
            print("连接附加项:", name, "已设置")
        except Exception as exc:
            print("连接附加项:", name, "失败", type(exc).__name__)
    t0 = time.time()
    accepted = core.connect(conn.adb_path, conn.address, conn.config)
    print(f"连接调用已发出: {accepted}（{time.time() - t0:.1f}s）")
    real, why = core.connect_result(timeout=25.0)
    print(f"连接真实结果: {real} {why}")
    for ev in core.events():
        print("  回调:", json.dumps(ev, ensure_ascii=False)[:200])
    if not real or not args.run:
        print("（没排队任何任务，屏幕不会被动）")
        return 0 if real else 3

    intents = {i.task: i for i in build_intents(conn, cfg)}
    intent = intents.get(args.run)
    if intent is None:
        print("白名单里没有", args.run, "；可选:", "、".join(intents))
        return 4
    print(f"\n排队 {intent.task} 参数={json.dumps(intent.params, ensure_ascii=False)}")
    print("task id:", core.append_task(intent.task, intent.params), "start:", core.start())
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        for ev in core.events():
            keep = {k: v for k, v in ev.items() if k in ("kind", "taskchain", "state", "what")}
            print("  ", json.dumps(keep, ensure_ascii=False),
                  json.dumps(ev.get("details") or {}, ensure_ascii=False)[:120])
        if not core.running():
            break
        time.sleep(1.0)
    core.stop()
    print("running after stop:", core.running())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
