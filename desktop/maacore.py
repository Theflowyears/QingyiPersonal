"""MaaCore.dll 的 ctypes 绑定（只暴露我们实测存在的导出）。

这台机器上的 MAA-v5.23.2 的 MaaCore.dll 导出的是 MAA 自己的 `Asst*` 接口，
不是 MaaFramework 的 `Maa*` 接口 —— 逐个符号 GetProcAddress 验过：
AsstLoadResource / AsstSetUserDir / AsstCreateEx / AsstSetInstanceOption /
AsstSetStaticOption / AsstSetConnectionExtras / AsstConnect / AsstAsyncConnect /
AsstAppendTask / AsstSetTaskParams / AsstStart / AsstStop / AsstRunning /
AsstGetVersion 都在；MaaMgr* / MaaControllerCreate 一个都没有。

不打包、不分发 MAA：加载的是用户本机已装的那一份，版本天然和 DLL 对齐。
"""
from __future__ import annotations

import ctypes
import json
import queue
import threading
import time
from pathlib import Path

# 回调消息号（MAA docs/回调消息.md，和 MAA 自带 Python 绑定的 Message 枚举一致）
MSG_INTERNAL_ERROR = 0
MSG_INIT_FAILED = 1
MSG_CONNECTION_INFO = 2
MSG_ALL_TASKS_COMPLETED = 3
MSG_ASYNC_CALL_INFO = 4
MSG_TASK_CHAIN_ERROR = 10000
MSG_TASK_CHAIN_START = 10001
MSG_TASK_CHAIN_COMPLETED = 10002
MSG_TASK_CHAIN_EXTRA_INFO = 10003
MSG_TASK_CHAIN_STOPPED = 10004
MSG_SUB_TASK_ERROR = 20000
MSG_SUB_TASK_EXTRA_INFO = 20003

_CHAIN = {
    MSG_TASK_CHAIN_ERROR: "error",
    MSG_TASK_CHAIN_START: "start",
    MSG_TASK_CHAIN_COMPLETED: "completed",
    MSG_TASK_CHAIN_EXTRA_INFO: "extra",
    MSG_TASK_CHAIN_STOPPED: "stopped",
}

# InstanceOptionType / StaticOptionType（MAA 自带 utils.py 里的取值）
OPT_TOUCH_TYPE = 2
OPT_DEPLOYMENT_WITH_PAUSE = 3
OPT_ADBLITE = 4
OPT_KILL_ON_ADB_EXIT = 5
STATIC_CPU_OCR = 1
STATIC_GPU_OCR = 2


def parse_event(msg: int, details: dict | None) -> dict:
    """把一条回调压成 runner 认得的结构；未知消息也照原样留痕，不吞。"""
    details = details or {}
    out = {"msg": int(msg), "kind": "other", "taskchain": details.get("taskchain") or ""}
    if msg in _CHAIN:
        out["kind"] = "chain"
        out["state"] = _CHAIN[msg]
        out["uuid"] = details.get("uuid") or ""
        out["finished"] = details.get("finished_tasks") or []
        out["raw"] = details
    elif msg == MSG_ALL_TASKS_COMPLETED:
        out["kind"] = "all_completed"
        out["raw"] = details
    elif msg == MSG_CONNECTION_INFO:
        out["kind"] = "connection"
        out["stage"] = details.get("stage") or ""
        out["code"] = details.get("code")
        out["raw"] = details
    elif msg == MSG_ASYNC_CALL_INFO:
        # AsstAsyncConnect 的返回值只说明「调用发出去了」，成没成看这条回执
        out["kind"] = "async_result"
        out["what"] = details.get("what") or ""
        out["details"] = details.get("details") or {}
        out["raw"] = details
    elif msg in (MSG_INTERNAL_ERROR, MSG_INIT_FAILED, MSG_SUB_TASK_ERROR):
        out["kind"] = "fault"
        out["what"] = details.get("what") or ""
        out["raw"] = details
    elif msg == MSG_SUB_TASK_EXTRA_INFO:
        # 计数只从这里来：FightTimes / SanityBeforeStage 等，别猜，读原值
        out["kind"] = "extra"
        out["what"] = details.get("what") or ""
        out["details"] = details.get("details") or {}
        out["raw"] = details
    else:
        out["raw"] = details
    return out


class MaaCore:
    """一个 MaaCore 实例。`lib` 可以注入 —— 单测用假 DLL，真机用 MaaCore.dll。"""

    _CALLBACK_TYPE = ctypes.CFUNCTYPE(
        None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p
    )

    def __init__(self, dll_dir: str | Path | None = None, user_dir: str | Path | None = None,
                 lib=None):
        self.dll_dir = Path(dll_dir) if dll_dir else None
        self.user_dir = Path(user_dir) if user_dir else None
        self._lib = lib
        self._ptr = None
        self._cb = None  # 回调必须被引用住，否则被 GC 掉直接崩
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.RLock()

    # ---------- 装载 ----------
    def _load_lib(self):
        if self._lib is not None:
            return self._lib
        if self.dll_dir is None:
            raise RuntimeError("没给 MAA 目录，也不知道去哪找 MaaCore.dll")
        dll = self.dll_dir / "MaaCore.dll"
        if not dll.exists():
            raise RuntimeError(f"{dll} 不存在；MAA 可能升级换了目录，去面板里重新指一次")
        # MaaCore.dll 依赖同目录的 MaaUtils.dll 等，必须把目录本身加进 PATH
        import os

        os.environ["PATH"] = str(self.dll_dir) + os.pathsep + os.environ.get("PATH", "")
        return ctypes.WinDLL(str(dll))

    @staticmethod
    def _declare(lib) -> None:
        """只给真 DLL 声明签名；注入的假 lib 原样跳过。"""
        if not isinstance(lib, ctypes.WinDLL):
            return
        b, i, p = ctypes.c_bool, ctypes.c_int, ctypes.c_void_p
        cp = ctypes.c_char_p
        lib.AsstSetUserDir.restype = b
        lib.AsstSetUserDir.argtypes = (cp,)
        lib.AsstLoadResource.restype = b
        lib.AsstLoadResource.argtypes = (cp,)
        lib.AsstCreate.restype = p
        lib.AsstCreate.argtypes = ()
        lib.AsstCreateEx.restype = p
        lib.AsstCreateEx.argtypes = (MaaCore._CALLBACK_TYPE, p)
        lib.AsstDestroy.argtypes = (p,)
        lib.AsstSetInstanceOption.restype = b
        lib.AsstSetInstanceOption.argtypes = (p, i, cp)
        lib.AsstSetStaticOption.restype = b
        lib.AsstSetStaticOption.argtypes = (i, cp)
        lib.AsstSetConnectionExtras.restype = None
        lib.AsstSetConnectionExtras.argtypes = (cp, cp)
        lib.AsstConnect.restype = b
        lib.AsstConnect.argtypes = (p, cp, cp, cp)
        # 第一个参数是实例句柄：漏掉它，ctypes 会把 adb 路径当指针传进去，直接访问违例
        lib.AsstAsyncConnect.restype = i
        lib.AsstAsyncConnect.argtypes = (p, cp, cp, cp, b)
        lib.AsstAppendTask.restype = i
        lib.AsstAppendTask.argtypes = (p, cp, cp)
        lib.AsstSetTaskParams.restype = b
        lib.AsstSetTaskParams.argtypes = (p, i, cp)
        for name in ("AsstStart", "AsstStop", "AsstRunning"):
            getattr(lib, name).restype = b
            getattr(lib, name).argtypes = (p,)
        lib.AsstGetVersion.restype = cp

    def load(self, incremental_dir: str | Path | None = None) -> str:
        """加载 DLL + 资源，返回 MaaCore 版本串。"""
        with self._lock:
            lib = self._load_lib()
            self._declare(lib)
            self._lib = lib
            if self.user_dir is not None:
                self.user_dir.mkdir(parents=True, exist_ok=True)
                lib.AsstSetUserDir(str(self.user_dir).encode("utf-8"))
            if self.dll_dir is None or not lib.AsstLoadResource(str(self.dll_dir).encode("utf-8")):
                raise RuntimeError("MaaCore 资源加载失败（Resource 目录缺失或版本不匹配）")
            if incremental_dir:
                lib.AsstLoadResource(str(incremental_dir).encode("utf-8"))
            raw = lib.AsstGetVersion()
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")

    # ---------- 实例 ----------
    def create(self) -> None:
        with self._lock:
            if self._ptr is not None:
                return
            self._cb = MaaCore._CALLBACK_TYPE(self._on_callback)
            self._ptr = self._lib.AsstCreateEx(self._cb, None)
            if not self._ptr:
                self._cb = None
                raise RuntimeError("AsstCreateEx 返回空句柄")

    def destroy(self) -> None:
        with self._lock:
            if self._ptr is not None:
                try:
                    self._lib.AsstDestroy(self._ptr)
                finally:
                    self._ptr = None
                    self._cb = None

    def _on_callback(self, msg: int, details: bytes | None, _arg) -> None:
        # 这条跑在 MaaCore 的工作线程上：只做解析和入队，绝不在里面碰 UI 或阻塞
        try:
            obj = json.loads(details.decode("utf-8")) if details else {}
        except (ValueError, UnicodeDecodeError):
            obj = {"_undecodable": (details or b"")[:200].decode("utf-8", "replace")}
        self._events.put(parse_event(int(msg), obj))

    def events(self, limit: int = 200) -> list[dict]:
        """取走队列里已有的事件，不等待。"""
        out = []
        while len(out) < limit:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    # ---------- 连接 ----------
    def set_instance_option(self, opt: int, value: str) -> bool:
        with self._lock:
            self.create()
            return bool(self._lib.AsstSetInstanceOption(
                self._ptr, int(opt), str(value).encode("utf-8")))

    @staticmethod
    def set_static_option(lib, opt: int, value: str) -> bool:
        return bool(lib.AsstSetStaticOption(int(opt), str(value).encode("utf-8")))

    def set_connection_extras(self, name: str, extras: dict) -> None:
        with self._lock:
            self._lib.AsstSetConnectionExtras(
                name.encode("utf-8"), json.dumps(extras, ensure_ascii=False).encode("utf-8"))

    def connect(self, adb_path: str, address: str, config: str = "General",
                block: bool = True) -> bool:
        with self._lock:
            self.create()
            return bool(self._lib.AsstAsyncConnect(
                self._ptr, adb_path.encode("utf-8"), address.encode("utf-8"),
                config.encode("utf-8"), bool(block)))

    def connect_result(self, timeout: float = 30.0,
                       sleep=time.sleep) -> tuple[bool, str]:
        """等连接的真实回执。没回执就说没回执，不拿「调用已受理」冒充连上了。"""
        deadline = time.time() + timeout
        while time.time() <= deadline:
            for ev in self.events():
                if ev.get("kind") != "async_result" or ev.get("what") != "Connect":
                    continue
                details = ev.get("details") or {}
                cost = details.get("cost")
                return bool(details.get("ret")), f"耗时 {cost}ms" if cost is not None else ""
            sleep(0.2)
        return False, f"{timeout:.0f} 秒内没有连接回执"

    # ---------- 任务 ----------
    def append_task(self, name: str, params: dict | None = None) -> int:
        with self._lock:
            self.create()
            payload = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
            return int(self._lib.AsstAppendTask(self._ptr, name.encode("utf-8"), payload))

    def set_task_params(self, task_id: int, params: dict) -> bool:
        with self._lock:
            return bool(self._lib.AsstSetTaskParams(
                self._ptr, int(task_id),
                json.dumps(params, ensure_ascii=False).encode("utf-8")))

    def start(self) -> bool:
        with self._lock:
            return bool(self._lib.AsstStart(self._ptr))

    def stop(self) -> bool:
        with self._lock:
            return bool(self._lib.AsstStop(self._ptr)) if self._ptr else False

    def running(self) -> bool:
        with self._lock:
            return bool(self._lib.AsstRunning(self._ptr)) if self._ptr else False
