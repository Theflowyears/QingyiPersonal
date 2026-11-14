"""清漪 · 桌面日常（P1，纯本地）。

启动：
    agent-qq-sandbox-bot\\venv\\Scripts\\python.exe desktop\\qingyi_desktop.py

她在这台机器上只做三件事：看着你的 MAA 配置把日常套餐摆出来、经你点头后操控
MaaCore 清明日方舟日常、把真实结果用她自己的话说给你听。云端链路是 P2，这里不碰。

生命周期：关面板只是收起，托盘和清漪还在后台跑；清漪能单独隐藏/留在前台，
真正退出走托盘或右键菜单的「退出」。设备探测在后台线程做，面板刷新只读缓存，
不再每 1.5 秒卡在 adb/MuMuManager 的子进程上。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO / "bot")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("DATA_DIR", str(REPO / "data"))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

import brain as BR  # noqa: E402
import discovery as DS  # noqa: E402
import plan as PL  # noqa: E402
import runner as RN  # noqa: E402
import sprites as SP  # noqa: E402
import voice as VOICE  # noqa: E402
from maacore import MaaCore  # noqa: E402
from panel_window import PanelWindow  # noqa: E402
from pet_window import PetWindow  # noqa: E402
from store import Store  # noqa: E402

STATE = HERE / "state"
LEDGER = STATE / "ledger.jsonl"
PROBE_EVERY_MS = 8000  # 设备闸门后台多久查一次；面板只读这份缓存


class _Speak(QtCore.QObject):
    done = QtCore.Signal(list, str, str, bool)


class _SpeakJob(QtCore.QRunnable):
    """说话走线程池：模型 20 秒不回也不能把桌面卡住。"""

    def __init__(self, sink, situation, state, proactive):
        super().__init__()
        self.sink = sink
        self.situation = situation
        self.state = state
        self.proactive = proactive

    def run(self):
        try:
            lines, source = VOICE.say(self.situation, self.state, allow_silence=self.proactive)
        except Exception as exc:
            lines, source = ([f"我刚才想说话，结果{type(exc).__name__}了"], "兜底")
        self.sink.done.emit(lines, self.state, source, self.proactive)


class _Probe(QtCore.QObject):
    result = QtCore.Signal(object)


class _ProbeJob(QtCore.QRunnable):
    """设备探测会跑 adb/MuMuManager 子进程，绝不能压在 UI 线程上。"""

    def __init__(self, ctrl, store, conn, todo, emulator_path):
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self.conn = conn
        self.todo = todo
        self.emulator_path = emulator_path

    def run(self):
        try:
            res = RN.check_gates(
                self.store, self.conn, self.todo,
                device_probe=lambda: DS.device_state(self.conn, self.emulator_path))
        except Exception as exc:
            res = (False, [f"设备探测崩了 {type(exc).__name__}: {exc}"])
        self.ctrl._probe.result.emit(res)


class Controller:
    def __init__(self, app: QtWidgets.QApplication):
        self.app = app
        self.store = Store(STATE)
        self.logs: list[str] = []
        self.maa_root: Path | None = None
        self.gui_cfg: dict = {}
        self.conn = DS.read_connection({})
        self.intents: list[PL.Intent] = []
        self.last_say = ("", "", "")
        self.pet = None
        self.brain = None
        self.pool = QtCore.QThreadPool()
        self.pool.setMaxThreadCount(2)
        self.sink = _Speak()
        self.sink.done.connect(self._on_said)
        self.runner = RN.Runner(MaaCore(), LEDGER, log=self.log, store=self.store)
        self.worker: threading.Thread | None = None
        # 后台探测：结果缓存下来，gates()/status() 只读，不阻塞
        self._probe = _Probe()
        self._probe.result.connect(self._on_probe)
        self._probe_pool = QtCore.QThreadPool()
        self._probe_pool.setMaxThreadCount(1)
        self._probing = False
        self._probe_result: tuple[bool, list[str]] = (False, ["还没查"])
        self._probe_at = 0.0
        self.rescan()
        for rid in self.runner.recover_stale():
            self.log(f"[账本] 上一单 {rid} 没收尾，已标中断")
        self._probe_timer = QtCore.QTimer(self.app)
        self._probe_timer.timeout.connect(self._dispatch_probe)
        self._probe_timer.start(PROBE_EVERY_MS)
        self._dispatch_probe()

    # ---------- 基础设施 ----------
    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"{stamp} {line}")
        if len(self.logs) > 1200:
            del self.logs[:-1200]
        print(line, flush=True)

    def rescan(self) -> None:
        root = DS.find_maa_install(preferred=str(self.store["maa_dir"] or ""))
        if root is None:
            self.conn = DS.read_connection({})
            self.intents = PL.build_intents(self.conn, {})
            self.log("[发现] 没找到装好的 MAA（目录里没有 MaaCore.dll）；"
                     "在面板底部指一次路径，或先装 MAA")
            return
        self.maa_root = root
        self.store.set(maa_dir=str(root))
        self.gui_cfg = DS.load_gui_config(root)
        self.conn = DS.read_connection(self.gui_cfg)
        if self.store["adb_path"]:
            self.conn.adb_path = str(self.store["adb_path"])
            self.conn.source += "；adb 用面板里指的"
        if self.store["address"]:
            self.conn.address = str(self.store["address"])
            self.conn.source += "；地址用面板里指的"
        if self.store["emulator_path"]:
            self.conn.extras = {"MuMuEmulator12": {
                "IsEnabled": True, "EmulatorPath": str(self.store["emulator_path"]),
                "InstanceIndex": 0}}
        self.intents = PL.build_intents(self.conn, self.gui_cfg)
        self.log(f"[发现] MAA {root.name}；连接：{self.conn.address} "
                 f"adb={self.conn.adb_path or '未找到'} 触控={self.conn.touch_mode}")
        self.log("[套餐] " + "、".join(f"{i.task}({i.tier})" for i in self.intents))

    def save_settings(self, values: dict) -> None:
        self.store.set(**{k: v for k, v in values.items() if v})
        self.rescan()
        self._dispatch_probe()

    # ---------- 设备探测（后台） ----------
    def _dispatch_probe(self) -> None:
        if self._probing:
            return
        self._probing = True
        todo = PL.daily_plan(self.intents)
        self._probe_pool.start(_ProbeJob(self, self.store, self.conn, todo,
                                         self.emulator_path()))

    def _on_probe(self, res) -> None:
        self._probe_result = (bool(res[0]), list(res[1]))
        self._probe_at = time.time()
        self._probing = False

    def gates(self) -> tuple[bool, list[str]]:
        """只回后台最近一次探测的结果，不跑子进程，面板随便刷都不卡。"""
        return self._probe_result

    def shutdown(self) -> None:
        self._probe_timer.stop()
        self._probe_pool.waitForDone(1500)
        if self.runner.run is not None and self.runner.run.state in ("running", "stalled"):
            self.runner.stop()

    # ---------- 授权与开工 ----------
    def approve(self) -> str:
        todo = PL.daily_plan(self.intents)
        fp = self.store.approve(todo)
        self.log(f"[授权] 套餐 {fp} 已确认：" + "、".join(i.task for i in todo))
        return fp

    def mute_today(self) -> None:
        self.store.mute_today()
        self.log("[别吵] 今天不再主动开口；跑单结果和闸门仍然照说")

    def set_skin(self, skin: str) -> None:
        self.store.set(pet_skin=skin)
        if self.pet is not None:
            self.pet.reload_skin(skin)
        frames = len(self.pet._frames) if self.pet is not None else 0
        self.log(f"[皮肤] 换成 {skin}，待机序列 {frames} 帧")

    def preview_state(self, state: str) -> None:
        """面板上点一下就能看她做这个动作 —— 素材对不对，眼睛验，不靠声称。"""
        if self.pet is None:
            return
        self.pet.set_state(state)
        self.log(f"[预览] {state}：{len(self.pet._frames)} 帧，"
                f"{SP.delay_ms(state, self.pet.skin)}ms/帧")

    def emulator_path(self) -> str:
        if self.store["emulator_path"]:
            return str(self.store["emulator_path"])
        extra = (self.conn.extras or {}).get("MuMuEmulator12") or {}
        return str(extra.get("EmulatorPath") or "")

    def start_run(self) -> None:
        if self.worker and self.worker.is_alive():
            self.log("[跑单] 已经有一单在跑，先停掉再说")
            return
        todo = PL.daily_plan(self.intents)
        fp = PL.fingerprint(todo)
        self.worker = threading.Thread(target=self._run_worker, args=(todo, fp), daemon=True)
        self.worker.start()

    def _run_worker(self, todo, fp) -> None:
        # 闸门在工作线程里现查一遍（会跑子进程），不卡 UI，也不拿旧缓存误事
        ok, blocked = RN.check_gates(
            self.store, self.conn, todo,
            device_probe=lambda: DS.device_state(self.conn, self.emulator_path()))
        if not ok:
            self.log("[闸门] " + "；".join(blocked))
            self.ask(BR.Utterance("confirm", "闸门拦住了：" + "；".join(blocked) +
                                  "。告诉他每一条。", "；".join(blocked), False))
            return
        core = MaaCore(dll_dir=self.maa_root, user_dir=STATE / "maa-user")
        self.runner.core = core
        try:
            self.runner.start(todo, self.conn, fp)
            while self.runner.run.state in ("running", "stalled"):
                self.runner.pump()
                time.sleep(1.0)
        except Exception as exc:
            self.log(f"[跑单] 崩了 {type(exc).__name__}: {exc}")
        finally:
            snap = self.runner.snapshot()
            state = {"done": "done"}.get(snap.get("state"), "fail")
            self.ask(BR.Utterance(state, BR.summarize_run(snap), "跑单结束", False))

    def stop_run(self) -> None:
        if self.runner.run is None:
            self.log("[停止] 现在没有正在跑的单")
            return
        self.runner.stop()

    # ---------- 说话 ----------
    def ask(self, ut: BR.Utterance) -> None:
        if ut.proactive:
            ok, why = self.store.can_bubble()
            if not ok:
                self.log(f"[主动] 不开口：{why}")
                if self.brain is not None:
                    self.brain.last_reason = why
                return
        self.log(f"[她说·{ut.reason}] {ut.situation[:60]}")
        self.pool.start(_SpeakJob(self.sink, ut.situation, ut.state, ut.proactive))

    def _on_said(self, lines: list, state: str, source: str, proactive: bool) -> None:
        if not lines:
            self.log(f"[气泡/{source}] 不开口")
            return
        if proactive:
            # 模型在后台生成时可能刚点了「别吵」，也可能另一条已用掉配额。
            ok, why = self.store.can_bubble()
            if not ok:
                self.log(f"[主动] 不展示：{why}")
                if self.brain is not None:
                    self.brain.last_reason = why
                return
            self.store.note_bubble()
        self.last_say = (lines, state, source)
        if self.pet is not None:
            self.pet.say(lines, state)
        self.log(f"[气泡/{source}] {' / '.join(lines)}")

    # ---------- 面板数据 ----------
    def status(self) -> dict:
        todo = PL.daily_plan(self.intents)
        approved = self.store.approved()
        blocked = self.gates()[1]
        return {
            "headline": self._headline(),
            "env": [f"MAA：{(self.maa_root.name if self.maa_root else '没找到')}",
                    f"连接来源：{self.conn.source}",
                    f"套餐指纹：{PL.fingerprint(todo)}（已确认 {self.store.fingerprint() or '—'}）"],
            "intents": [i.as_dict() for i in self.intents],
            "changed": PL.changed_keys(todo, approved),
            "fp": PL.fingerprint(todo),
            "blocked": blocked,
            "probe_at": self._probe_at,
            "run": self.runner.snapshot(),
            "ledger": RN.recent_ledger(LEDGER, 14),
            "logs": list(self.logs),
        }

    def _headline(self) -> str:
        snap = self.runner.snapshot()
        state = snap.get("state")
        if state in ("running", "stalled"):
            done = sum(1 for c in snap.get("chains", []) if c.get("state") == "done")
            return f"正在清日常：{done}/{len(snap.get('chains') or [])} 条完成"
        if state == "done":
            return "上一单跑完了，账本里有。"
        ok, blocked = self.gates()
        if blocked:
            return "还没开工：" + "；".join(blocked)
        return "可以开工。要不要现在清？"


class App(QtWidgets.QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setApplicationName("清漪·桌面日常")
        # 关面板/藏鱼都不该退出程序：后台静默运行，退出只走显式「退出」
        self.setQuitOnLastWindowClosed(False)
        self.controller = Controller(self)
        self.pet = PetWindow(self.controller.store)
        self.panel = PanelWindow(self.controller)
        self.controller.pet = self.pet
        self.pet.open_panel.connect(self._show_panel)
        self.pet.run_requested.connect(self.controller.start_run)
        self.pet.clicked.connect(self._greet_once)
        self.brain = BR.Brain(self.controller.store, [])
        self.controller.brain = self.brain
        self._build_tray()
        self.aboutToQuit.connect(self.controller.shutdown)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60_000)
        self._greeted = False
        self.pet.show()
        if os.environ.get("DESKTOP_PANEL") == "1":
            self.panel.show()

    # ---------- 托盘 ----------
    def _tray_icon(self) -> QtGui.QIcon:
        frames = SP.frames("idle", skin=str(self.controller.store["pet_skin"] or "fish"))
        if frames:
            return QtGui.QIcon(str(frames[0]))
        return self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)

    def _build_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(self._tray_icon(), self)
        self.tray.setToolTip("清漪 · 桌面日常（后台运行中）")
        menu = QtWidgets.QMenu()
        menu.addAction("打开面板", self._show_panel)
        self.act_pet = menu.addAction("隐藏清漪", self._toggle_pet)
        menu.addAction("现在清日常", self.controller.start_run)
        menu.addAction("今天别吵", self.controller.mute_today)
        menu.addSeparator()
        menu.addAction("退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason in (QtWidgets.QSystemTrayIcon.DoubleClick,
                      QtWidgets.QSystemTrayIcon.Trigger):
            self._show_panel()

    def _show_panel(self):
        self.panel.showNormal()
        self.panel.raise_()
        self.panel.activateWindow()

    def _toggle_pet(self):
        if self.pet.pet_visible():
            self.pet.hide_pet()
            self.act_pet.setText("显示清漪")
            self.tray.setToolTip("清漪 · 桌面日常（清漪已隐藏，后台仍在跑）")
        else:
            self.pet.show_pet()
            self.act_pet.setText("隐藏清漪")
            self.tray.setToolTip("清漪 · 桌面日常（后台运行中）")

    def _quit(self):
        self.controller.shutdown()
        self.quit()

    def _greet_once(self):
        if not self._greeted:
            self._greeted = True
            self.controller.ask(self.controller.brain.greet())
        else:
            self.controller.pet.say(self.controller._headline(), "talk")

    def _tick(self):
        self.brain.ledger_rows = self.controller.runner.read()
        ut = self.brain.tick()
        if ut:
            self.controller.ask(ut)


def main() -> int:
    app = App(sys.argv)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
