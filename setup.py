#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QingyiPersonal 一键部署引导（公开白板版）。

目标：新克隆的仓库，一条命令把"能打开面板"这件事备齐——
  1. 建 venv 并装依赖
  2. 生成 config.env（随机面板密钥 + 管理员凭据，API key 留空交给面板填）
  3. 可选：拉起 NapCat 容器（固定 MAC，设备指纹稳定；绝不重启已存在的容器）
  4. 打印面板地址 / 管理员凭据 / 下一步

设计边界（与 AGENTS.md 红线一致）：
  · 只在本仓库目录内动作；不碰任何远端生产机。
  · config.env 已存在时绝不覆盖（先备份原则），只提示。
  · NapCat 容器已存在时绝不 restart/recreate（QQ 登录态高于一切），只报告。
  · 本脚本只用标准库——它在 venv 建立之前就要能跑。

用法：
  python setup.py                 # 交互式：生成配置并提示启动
  python setup.py --napcat        # 顺带创建 NapCat 容器（需要 docker）
  python setup.py --start         # 生成后直接后台拉起 launcher
  python setup.py --non-interactive  # 全自动：随机管理员密码并打印一次
"""
import argparse
import os
import platform
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"


def say(msg=""):
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def py_in_venv():
    return str(ROOT / "venv" / ("Scripts/python.exe" if IS_WIN else "bin/python"))


def ensure_python():
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10+，当前 %s" % platform.python_version())


def ensure_venv(skip):
    if skip:
        say("[1/4] 跳过 venv（--no-venv），用当前解释器装依赖")
        pip = [sys.executable, "-m", "pip"]
    else:
        if not (ROOT / "venv").exists():
            say("[1/4] 创建 venv …")
            r = run([sys.executable, "-m", "venv", str(ROOT / "venv")])
            if r.returncode != 0:
                raise SystemExit("venv 创建失败：%s" % (r.stderr or r.stdout)[-300:])
        else:
            say("[1/4] venv 已存在，复用")
        pip = [py_in_venv(), "-m", "pip"]
    say("      安装依赖（requirements.txt，可能需 1–2 分钟）…")
    r = run(pip + ["install", "-r", str(ROOT / "requirements.txt")])
    if r.returncode != 0:
        raise SystemExit("依赖安装失败：%s" % (r.stderr or r.stdout)[-400:])
    say("      依赖就绪")


def gen_config(args):
    """从 .env.example 生成 config.env；已存在则绝不覆盖。返回 (path, created, creds)。"""
    cfg = ROOT / "config.env"
    example = ROOT / ".env.example"
    if cfg.exists():
        say("[2/4] config.env 已存在——不覆盖（改配置请编辑它或用面板）")
        return cfg, False, None
    if not example.exists():
        raise SystemExit("缺少 .env.example，无法生成配置")
    text = example.read_text(encoding="utf-8")
    secret = secrets.token_hex(24)
    admin_user = args.admin_user or "admin"
    admin_pass = args.admin_password or secrets.token_urlsafe(12)
    overrides = {
        "WEB_SECRET_KEY": secret,
        "WEB_ADMIN_USER": admin_user,
        "WEB_ADMIN_PASSWORD": admin_pass,
        "WEB_HOST": "127.0.0.1",
        "WEB_PORT": str(args.port),
        "DATA_DIR": "./data",
        "ONEBOT_WS": "ws://127.0.0.1:3001",
    }
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in overrides:
                lines.append("%s=%s" % (key, overrides[key]))
                continue
        lines.append(line)
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(cfg, 0o600)
    except OSError:
        pass
    say("[2/4] 已生成 config.env（随机面板密钥；API key 留空，稍后在面板填）")
    return cfg, True, {"user": admin_user, "pass": admin_pass}


def ensure_data():
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "share").mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)


def napcat_mac():
    """固定 MAC：首次生成后落盘，之后复用——设备指纹稳定，QQ 不易风控。"""
    f = ROOT / "data" / "napcat_mac.txt"
    if f.exists():
        mac = f.read_text(encoding="utf-8").strip()
        if mac:
            return mac
    import random
    octets = [0x02] + [random.randint(0, 255) for _ in range(5)]
    mac = ":".join("%02x" % b for b in octets)
    f.write_text(mac, encoding="utf-8")
    return mac


def ensure_napcat():
    say("[3/4] NapCat（--napcat）")
    if not shutil.which("docker"):
        say("      未找到 docker，跳过。手动部署见 README「QQ 接入」。")
        return
    name = "napcat"
    exists = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    names = (exists.stdout or "").split()
    if name in names:
        state = run(["docker", "inspect", "-f", "{{.State.Running}}", name]).stdout.strip()
        say("      容器已存在（running=%s）——绝不重启/重建，保护 QQ 登录态。" % state)
        say("      如需扫码：打开面板「QQ 登录」，或 docker logs %s" % name)
        return
    mac = napcat_mac()
    say("      创建容器（固定 MAC %s）…" % mac)
    cmd = ["docker", "run", "-d", "--name", name, "--mac-address", mac,
           "-p", "3001:3001", "-p", "6099:6099",
           "-e", "NAPCAT_UID=1000", "-e", "NAPCAT_GID=1000",
           "--restart", "unless-stopped",
           "mlikiowa/napcat-docker:latest"]
    r = run(cmd)
    if r.returncode != 0:
        say("      创建失败：%s" % (r.stderr or r.stdout)[-300:])
        say("      可手动执行：%s" % " ".join(cmd))
    else:
        say("      已创建。首次登录需扫码：面板「QQ 登录」或 docker logs napcat")


def start_stack():
    say("[4/4] 启动 launcher（后台）")
    py = py_in_venv() if (ROOT / "venv").exists() else sys.executable
    logf = open(str(ROOT / "logs" / "setup_start.log"), "a", encoding="utf-8")
    kw = {"stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL, "cwd": str(ROOT)}
    if IS_WIN:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([py, str(ROOT / "launcher.py")], **kw)
    say("      已拉起。日志：logs/ 与 data/bot_out.log")


def main():
    ap = argparse.ArgumentParser(description="QingyiPersonal 一键部署引导")
    ap.add_argument("--napcat", action="store_true", help="创建 NapCat 容器（固定 MAC，需 docker）")
    ap.add_argument("--no-venv", action="store_true", help="不建 venv，用当前解释器")
    ap.add_argument("--start", action="store_true", help="生成后后台启动 launcher")
    ap.add_argument("--port", type=int, default=5000, help="面板端口（默认 5000）")
    ap.add_argument("--admin-user", default="", help="面板管理员用户名（默认 admin）")
    ap.add_argument("--admin-password", default="", help="面板管理员密码（默认随机生成）")
    ap.add_argument("--non-interactive", action="store_true", help="等价于默认随机密码，不提问")
    ap.add_argument("--mark-complete", action="store_true", help="直接标记初始化完成（生产式部署跳过向导，向导置惰性）")
    args = ap.parse_args()

    say("=" * 56)
    say("QingyiPersonal 部署引导（公开白板版 · 不含任何预设人格）")
    say("=" * 56)
    ensure_python()
    ensure_venv(args.no_venv)
    cfg, created, creds = gen_config(args)
    ensure_data()
    if args.mark_complete:
        marker = ROOT / "data" / ".setup_complete"
        marker.write_text("marked by setup.py", encoding="utf-8")
        say("      已标记初始化完成：/setup 向导将惰性（生产式部署）")
    if args.napcat:
        ensure_napcat()
    else:
        say("[3/4] 跳过 NapCat（未加 --napcat）。QQ 接入见 README。")
    if args.start:
        start_stack()
    else:
        say("[4/4] 未自动启动（加 --start 可后台拉起）")

    say("")
    say("─" * 56)
    say("下一步：")
    if not args.start:
        say("  1. 启动：  %s launcher.py" % (py_in_venv() if (ROOT/'venv').exists() else sys.executable))
    say("  2. 打开面板：http://127.0.0.1:%d/admin/login" % args.port)
    if created and creds:
        say("  3. 管理员登录（仅此一次打印，请记好）：")
        say("       用户名：%s" % creds["user"])
        say("       密码：  %s" % creds["pass"])
    else:
        say("  3. 用 config.env 里的 WEB_ADMIN_USER / WEB_ADMIN_PASSWORD 登录")
    say("  4. 登录后进入「快速配置向导 /setup」：填 API → 扫码登录 QQ → 看后台状态")
    say("─" * 56)


if __name__ == "__main__":
    main()
