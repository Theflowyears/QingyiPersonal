"""
Windows Launcher — 静默启动 bot + web console，单实例。

设计要点：
  1. 全程不开控制台窗口：子进程用 CREATE_NO_WINDOW，自己用 pythonw 启动。
     以前每个子进程都会弹一个 cmd 窗口，关掉窗口就等于杀掉进程组，
     所以"一关进程就掉"。
  2. 单实例：启动前用锁文件记 PID，发现旧实例还活着就直接退出，
     避免两个机器人连着同一个 NapCat 各回一遍消息。
  3. 日志写文件（追加），不再依赖控制台重定向。
     重定向是覆盖模式，一重启历史就没了，出问题根本没法追溯。
"""
import atexit
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Windows: 子进程不创建控制台窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def load_env(env_file):
    path = Path(env_file)
    if not path.exists():
        return False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value
    return True


def _pid_alive(pid):
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        pass
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                                 capture_output=True, text=True,
                                 creationflags=CREATE_NO_WINDOW)
            return str(pid) in (out.stdout or "")
    except Exception:
        pass
    return False


def acquire_lock(data_dir):
    """单实例锁。返回 (ok, 旧pid)。"""
    lock = Path(data_dir) / "launcher.lock"
    old = 0
    try:
        if lock.exists():
            old = int((lock.read_text(encoding="utf-8") or "0").strip() or 0)
    except Exception:
        old = 0
    if old and old != os.getpid() and _pid_alive(old):
        return False, old
    try:
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    return True, old


def release_lock(data_dir):
    try:
        lock = Path(data_dir) / "launcher.lock"
        if lock.exists():
            got = int((lock.read_text(encoding="utf-8") or "0").strip() or 0)
            if got == os.getpid():
                lock.unlink()
    except Exception:
        pass


class Logger:
    """追加式日志。不写控制台（pythonw 下没有控制台）。"""

    def __init__(self, path):
        self.path = str(path)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except Exception:
            pass

    def __call__(self, msg=""):
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass


def main():
    base_dir = Path(__file__).parent.resolve()
    py = sys.executable

    # pythonw 下 sys.executable 可能指向 pythonw.exe，子进程用普通 python.exe 更稳
    py_exe = str(Path(sys.executable).parent / "python.exe")
    if not os.path.exists(py_exe):
        py_exe = sys.executable

    if not load_env(str(base_dir / "config.env")):
        return 1

    data_dir = base_dir / os.getenv("DATA_DIR", "./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "share").mkdir(exist_ok=True)
    os.environ["DATA_DIR"] = str(data_dir)

    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    # 合并成一份主日志，便于排查
    log = Logger(str(data_dir / "bot_out.log"))
    log("")
    log("=" * 60)
    log("[启动] " + time.strftime("%Y-%m-%d %H:%M:%S") + "  launcher pid=" + str(os.getpid()))

    # ── 单实例 ──
    ok, old = acquire_lock(data_dir)
    if not ok:
        log("[启动] 已有实例在跑 (pid=%d)，本次退出，避免两个机器人同时回复。" % old)
        return 0
    atexit.register(lambda: release_lock(data_dir))

    os.environ.setdefault("CEYLAN_SECRET_KEY",
                          os.getenv("WEB_SECRET_KEY", "dev-secret-key-change-me"))
    os.environ.setdefault("CEYLAN_ADMIN_USER", os.getenv("WEB_ADMIN_USER", "admin"))
    os.environ.setdefault("CEYLAN_DB_PATH", str(data_dir / "visitors.db"))
    os.environ["CEYLAN_BOT_STATUS_FILE"] = str(data_dir / "bot_status.json")
    os.environ["CEYLAN_CONTROL_DIR"] = str(data_dir / "control-v4")
    os.environ["CEYLAN_FUN_DIR"] = str(data_dir / "fun-v6")
    os.environ["CEYLAN_KEYS_DIR"] = str(data_dir / "keys-v5")
    os.environ["CEYLAN_CONTROL_DB"] = str(data_dir / "bot_control.db")
    os.environ["CEYLAN_FUN_DB"] = str(data_dir / "bot_fun.db")
    os.environ["CEYLAN_TELEMETRY_DB"] = str(data_dir / "bot_telemetry.db")
    os.environ["CEYLAN_OBSERVER_FILE"] = str(data_dir / "observer-status.json")
    if not os.getenv("CEYLAN_ADMIN_HASH", ""):
        try:
            from werkzeug.security import generate_password_hash
            os.environ["CEYLAN_ADMIN_HASH"] = generate_password_hash(
                os.getenv("WEB_ADMIN_PASSWORD", "admin123"))
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    children = []

    def spawn(name, script_dir, module):
        # 关键：stdout/stderr 直接重定向到文件，且不创建窗口
        f = open(str(logs_dir / (name + ".log")), "a", encoding="utf-8", errors="replace")
        p = subprocess.Popen(
            [py_exe, "-u", module],
            cwd=str(script_dir),
            env=os.environ.copy(),
            creationflags=CREATE_NO_WINDOW,
            stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        children.append((name, p, f))
        log("[启动] %s pid=%d cwd=%s" % (name, p.pid, script_dir))
        return p

    spawn("Bot", base_dir / "bot", "qq_openclaw_bridge.py")
    time.sleep(1)
    spawn("Web", base_dir / "website", "run_web.py")
    log("[启动] 两个服务已拉起，端口 5000 稍后可用")

    stopping = {"flag": False}

    def shutdown(*_):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        log("[停止] 正在关闭子进程…")
        for name, p, f in children:
            try:
                p.terminate()
            except Exception:
                pass
        for name, p, f in children:
            try:
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                f.close()
            except Exception:
                pass
        release_lock(data_dir)
        log("[停止] 已退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while not stopping["flag"]:
        for name, p, f in children:
            rc = p.poll()
            if rc is not None:
                log("[警告] %s 退出 code=%s，全部停止" % (name, rc))
                shutdown()
                return 0
        time.sleep(1.5)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
