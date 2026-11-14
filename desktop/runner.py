"""跑单执行器：闸门 → 连接 → 排队 → 读回调 → 落账本。

三条硬要求决定了这里的写法：
- 进度只认 MaaCore 回调（TaskChain* / SubTaskExtraInfo），局数和理智都是它报的原值；
- 每一道「什么都不做」的门禁都要说出是哪一道，沉默等于故障；
- 进程中途没了，账本里那单必须标成中断，不能留在 running 里冒充成功。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from maacore import MaaCore
from plan import CONFIRM, MANUAL

STALE_AFTER = 240.0  # 这么久没有任何回调 = 卡住；只报不自动停，中途硬停会丢一整局
TERMINAL = ("run_finished", "run_interrupted")


def _now() -> float:
    return time.time()


@dataclass
class ChainProgress:
    task: str
    state: str = "queued"
    started: float = 0.0
    ended: float = 0.0
    times_finished: int = 0
    sanity_before: int = -1
    sanity_after: int = -1
    error: str = ""

    def as_dict(self) -> dict:
        return {"task": self.task, "state": self.state,
                "times_finished": self.times_finished,
                "sanity_before": None if self.sanity_before < 0 else self.sanity_before,
                "sanity_after": None if self.sanity_after < 0 else self.sanity_after,
                "seconds": round(self.ended - self.started) if self.started and self.ended else 0,
                "error": self.error}


@dataclass
class Run:
    rid: str
    started: float
    fp: str
    chains: list[ChainProgress] = field(default_factory=list)
    state: str = "running"
    last_event: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"id": self.rid, "started": self.started, "fingerprint": self.fp,
                "state": self.state, "notes": list(self.notes),
                "chains": [c.as_dict() for c in self.chains]}


def maa_gui_running(probe=None) -> tuple[bool, str]:
    """MAA 自己的 GUI 在跑就不能抢设备 —— 两边同时点同一台模拟器只会互相踩。"""
    if probe is None:
        try:
            import psutil
        except ImportError:
            return False, "没装 psutil，查不到 MAA 是否在跑（按不拦处理）"

        def probe():
            return [p.info["name"] for p in psutil.process_iter(["name"])]

    try:
        names = probe()
    except Exception as exc:
        return False, f"进程列表读失败 {type(exc).__name__}（按不拦处理）"
    hit = [n for n in names if n and n.lower().startswith("maa.exe")]
    return (bool(hit), "MAA 界面正在占用这台模拟器，先关掉它" if hit else "MAA 界面没在跑")


def check_gates(store, conn, intents, device_probe=None, gui_probe=None,
                run_checks=True) -> tuple[bool, list[str]]:
    """返回 (能不能开工, 拦路的每一道)。每道都要有名有姓。"""
    blocked = []
    todo = [i for i in intents if i.tier != MANUAL]
    if not todo:
        return False, ["套餐里一条任务都没有，没什么可跑的"]
    need_confirm = [i.task for i in todo if i.tier == CONFIRM]
    if need_confirm and store.fingerprint() != store_fingerprint_of(intents):
        blocked.append("有会花资源的任务（" + "、".join(need_confirm) +
                       "）参数和你上次点头的不一样，要你在面板确认")
    ok, why = maa_gui_running(gui_probe)
    if ok:
        blocked.append(why)
    if run_checks and device_probe is not None:
        ok, why = device_probe()
        if not ok:
            blocked.append("设备：" + why)
    if not conn.adb_path:
        blocked.append("没找到 adb.exe，连不上模拟器")
    return (not blocked), blocked


def store_fingerprint_of(intents) -> str:
    from plan import fingerprint
    return fingerprint(intents)


class Runner:
    def __init__(self, core: MaaCore, ledger_path: str | Path, log=None, store=None):
        self.core = core
        self.store = store
        self.ledger_path = Path(ledger_path)
        self.log = log or (lambda s: print(s, flush=True))
        self.run: Run | None = None
        self._by_task: dict[str, ChainProgress] = {}

    # ---------- 账本 ----------
    def _append(self, rec: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        rows = []
        for ln in self.ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
        return rows

    def recover_stale(self) -> list[str]:
        """上次进程没了，账本里还开着的单一律补一条中断记录。"""
        rows = self.read()
        closed = {r.get("id") for r in rows if r.get("event") in TERMINAL}
        fixed = []
        for r in rows:
            if r.get("event") == "run_started" and r.get("id") not in closed:
                self._append({"event": "run_interrupted", "id": r.get("id"), "at": _now(),
                              "why": "进程在跑单中途没了，这单没有完成证据"})
                fixed.append(str(r.get("id")))
        if fixed:
            self.log("[账本] 补标中断：" + "、".join(fixed))
        return fixed

    # ---------- 跑 ----------
    def start(self, intents, conn, fp: str) -> Run:
        self.run = Run(rid=time.strftime("%Y%m%d-%H%M%S"), started=_now(), fp=fp)
        self._by_task = {}
        for i in intents:
            c = ChainProgress(i.task)
            self._by_task[i.task] = c
            self.run.chains.append(c)
        self._append({"event": "run_started", "id": self.run.rid, "at": self.run.started,
                      "fingerprint": fp, "tasks": [i.task for i in intents]})
        self.log("[跑单 " + self.run.rid + "] 排队 " + str(len(intents)) + " 条：" +
                 "、".join(i.task for i in intents))
        spending = any(i.spends for i in intents)
        if spending and (self.store is None or
                         self.store.fingerprint() != store_fingerprint_of(intents)):
            return self._finish("fault", "会花资源的套餐还没确认，或本次授权已经用过，要重新确认")
        try:
            self.log("[MaaCore] " + self.core.load() + " 资源加载完成")
        except Exception as exc:
            return self._finish("fault", f"MaaCore 装载失败 {type(exc).__name__}: {exc}")
        try:
            self.core.set_instance_option(2, conn.touch_mode)
            for name, val in (conn.extras or {}).items():
                try:
                    self.core.set_connection_extras(name, val)
                except Exception as exc:
                    self.log(f"[连接附加项] {name} 没设上（{type(exc).__name__}），继续")
            accepted = self.core.connect(conn.adb_path, conn.address, conn.config)
            if not accepted:
                return self._finish("fault",
                                    f"连接调用没发出去 {conn.address}（adb={conn.adb_path or '未配置'}）")
            ok, why = self.core.connect_result()
            if not ok:
                return self._finish(
                    "fault", f"连不上 {conn.address}：{why or 'MaaCore 报失败'}"
                             f"（adb={conn.adb_path or '未配置'}）")
            self.log(f"[连接] {conn.address} 通了（{why or 'MaaCore 回执 ret=true'}）")
        except Exception as exc:
            return self._finish("fault", f"连接异常 {type(exc).__name__}: {exc}")
        for i in intents:
            if not self.core.append_task(i.task, i.params):
                return self._finish("fault", f"{i.task} 排队失败（MaaCore 没接受这组参数）")
        if spending:
            # 先落盘再开工：崩溃最多丢授权，不能留下已开工却可复用的授权。
            # 参数快照仍供面板比较；接受开工后不再写授权，以免保存异常丢掉监控。
            saved_approval = self.store.fingerprint()
            try:
                self.store.set(approved_fingerprint="")
            except Exception as exc:
                self._restore_approval(saved_approval)
                return self._finish("fault", f"授权消费保存失败，未开工 {type(exc).__name__}: {exc}")
        if not self.core.start():
            if spending:
                self._restore_approval(saved_approval)
            return self._finish("fault", "AsstStart 返回失败")
        self.run.last_event = _now()
        return self.run

    def _restore_approval(self, fp: str) -> None:
        """只在尚未开工时尽力恢复；保存结果不确定时，内存仍按无授权处理。"""
        try:
            self.store.set(approved_fingerprint=fp)
        except Exception as exc:
            # Store.set 先改内存才保存，失败不能留下看似已恢复的可用授权。
            self.store.data["approved_fingerprint"] = ""
            self.log(f"[授权] 恢复失败 {type(exc).__name__}: {exc}；未开工，请重新确认")

    def pump(self, limit: int = 200) -> list[dict]:
        """吃干净回调队列并更新进度；UI 侧轮询这个。"""
        if self.run is None:
            return []
        seen = [ev for ev in self.core.events(limit) if self._apply(ev)]
        if self.run.state == "running":
            if seen:
                self.run.last_event = _now()
            elif _now() - self.run.last_event > STALE_AFTER:
                self.run.state = "stalled"
                self.log(f"[跑单 {self.run.rid}] {int(_now() - self.run.last_event)} 秒没有任何回调，"
                         "卡住了。我不替你硬停（会丢一局），要停在面板上点")
        return seen

    def _apply(self, ev: dict) -> bool:
        kind = ev.get("kind")
        chain = self._by_task.get(ev.get("taskchain") or "")
        if kind == "chain" and chain is not None:
            state = ev.get("state")
            if state in ("start", "completed", "stopped") and chain.error:
                return True  # 同一条链后续回调不抹掉已确认的失败
            if state == "start":
                chain.state, chain.started = "running", _now()
                self.log(f"[{chain.task}] 开始")
            elif state == "completed":
                chain.state, chain.ended = "done", _now()
                self.log(f"[{chain.task}] 完成 局数={chain.times_finished} "
                         f"理智 {chain.sanity_before}→{chain.sanity_after}")
            elif state == "error":
                chain.state, chain.ended, chain.error = "error", _now(), "TaskChainError"
                self.run.notes.append(f"{chain.task}：TaskChainError")
                self.log(f"[{chain.task}] 出错：MaaCore 报了 TaskChainError，细节看它的日志")
            elif state == "stopped":
                chain.state, chain.ended = "stopped", _now()
                self.log(f"[{chain.task}] 被停止")
        elif kind == "extra" and chain is not None:
            what, details = ev.get("what") or "", ev.get("details") or {}
            if what == "FightTimes":
                chain.times_finished = int(details.get("times_finished")
                                           or chain.times_finished)
            elif what == "SanityBeforeStage":
                cur = details.get("current_sanity")
                if isinstance(cur, (int, float)):
                    if chain.sanity_before < 0:
                        chain.sanity_before = int(cur)
                    chain.sanity_after = int(cur)
        elif kind == "all_completed":
            # 队列收尾不等于每条都成功；保留部分完成和之前回调提供的失败依据。
            failed = self.run.notes or [f"{c.task}：{c.state}" for c in self.run.chains
                                        if c.state in ("error", "stopped")]
            self._finish("fault" if failed else "done",
                         "；".join([*failed, "AllTasksCompleted"]))
        elif kind == "fault":
            self.run.notes.append(str(ev.get("what") or ev.get("msg")))
            self.log("[MaaCore 故障] " + json.dumps(ev.get("raw") or {}, ensure_ascii=False)[:200])
        elif kind == "connection":
            self.log("[连接] " + str(ev.get("stage") or "") + " code=" + str(ev.get("code")))
        return True

    def _finish(self, state: str, why: str) -> Run:
        if self.run is None:
            raise RuntimeError("没有正在跑的单")
        if self.run.state in TERMINAL_STATES:
            return self.run
        self.run.state = state
        self.run.notes.append(why)
        self._append({"event": "run_finished", "id": self.run.rid, "at": _now(),
                      "state": state, "why": why, "notes": list(self.run.notes),
                      "chains": [c.as_dict() for c in self.run.chains]})
        self.log(f"[跑单 {self.run.rid}] {state}：{why}")
        return self.run

    def stop(self) -> bool:
        ok = self.core.stop()
        if self.run is not None and self.run.state in ("running", "stalled"):
            self._finish("stopped", "你在面板点了停止")
        return ok

    def snapshot(self) -> dict:
        return self.run.as_dict() if self.run is not None else {"state": "idle"}


TERMINAL_STATES = ("done", "fault", "stopped", "interrupted")


def recent_ledger(path: str | Path, limit: int = 30) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    return list(reversed(rows[-limit:]))
