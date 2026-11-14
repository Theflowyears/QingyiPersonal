# -*- coding: utf-8 -*-
"""本地 维护端 ↔ 港机生产环境的只读采集与运维账本传输。

自动驾驶跑在主人的 Windows 上（维护端 定时任务），生产数据在港机。这一层只做两件事：
把云端证据取回来，把运维报文/状态送过去。**不采集聊天正文**：日志只统计错误
类型和条数，不回传原句，符合前置检测报告的脱敏要求。

连接参数与 _deploy_qingyi_release.py 一致（paramiko + RejectPolicy），
失败一律返回结构化 error，不抛到调用方脸上 —— 采集不到就如实说采集不到。
"""
import json
import os
import shlex
import time

# 服务器地址/私钥一律走环境变量（OPS_SSH_HOST/OPS_SSH_USER/OPS_SSH_KEY），
# 公开仓库不落真实主机信息；缺配时连接前明确报错，不静默连到别处。
HOST = os.getenv("OPS_SSH_HOST", "")
USER = os.getenv("OPS_SSH_USER", "root")
KEY = os.getenv("OPS_SSH_KEY", "")
REMOTE = os.getenv("OPS_REMOTE_ROOT", "/opt/qqbot")
BACKUP_ROOT = os.getenv("OPS_REMOTE_BACKUP_ROOT", "/opt/qqbot_backups")

# 远端只允许调这些运维账本函数，杜绝把 QQ 文本变成任意代码执行。
ALLOWED_REMOTE_FUNCS = (
    "load_state", "save_state", "load_health", "save_health", "save_round",
    "pending_inbox", "claim_inbox", "finish_inbox", "enqueue_outbox",
    "pending_outbox", "record_decision", "mark_waiting", "clear_waiting",
    "set_autopilot_enabled", "autopilot_enabled",
    "create_challenge", "confirm_challenge", "pending_challenges",
)


def _py():
    return REMOTE + "/venv/bin/python"


def _connect(timeout=25):
    import paramiko
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    if not HOST or not KEY:
        raise RuntimeError("OPS_SSH_HOST/OPS_SSH_KEY 未配置，运维通道拒绝匿名连接")
    client.connect(HOST, username=USER, key_filename=KEY, timeout=timeout,
                   allow_agent=False, look_for_keys=False)
    return client


class Remote:
    """一次连接内跑多条命令。用 with 保证句柄关掉。"""

    def __init__(self, timeout=25):
        self._timeout = timeout
        self._client = None

    def __enter__(self):
        self._client = _connect(self._timeout)
        return self

    def __exit__(self, *exc):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        return False

    def run(self, command, timeout=120):
        """返回 (exit_code, text)。不抛异常：采集失败要能被如实报告。"""
        try:
            _, out, _err = self._client.exec_command(command, timeout=timeout)
            out.channel.set_combine_stderr(True)
            text = out.read().decode("utf-8", "replace")
            return out.channel.recv_exit_status(), text
        except Exception as e:
            return -1, "%s: %s" % (type(e).__name__, str(e)[:300])

    def python(self, code, timeout=180, stdin=None):
        cmd = "cd " + shlex.quote(REMOTE) + " && DATA_DIR=" + shlex.quote(REMOTE + "/data") + \
              " PYTHONUTF8=1 " + _py() + " -c " + shlex.quote(code)
        try:
            inp, out, _err = self._client.exec_command(cmd, timeout=timeout)
            out.channel.set_combine_stderr(True)
            if stdin is not None:
                inp.write(stdin)
                inp.flush()
                inp.channel.shutdown_write()
            text = out.read().decode("utf-8", "replace")
            return out.channel.recv_exit_status(), text
        except Exception as e:
            return -1, "%s: %s" % (type(e).__name__, str(e)[:300])

    def call_ops(self, func, payload=None, timeout=90):
        """在远端调 ops_channel 的同一个函数，状态语义两边完全一致。"""
        if func not in ALLOWED_REMOTE_FUNCS:
            return {"ok": False, "error": "func_not_allowed", "func": func}
        code = (
            "import sys, json\n"
            "sys.path.insert(0, %r)\n" % (REMOTE + "/bot")
            + "import ops_channel\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps(ops_channel.%s(**payload), ensure_ascii=False, default=str))\n" % func
        )
        rc, text = self.python(code, timeout=timeout,
                               stdin=json.dumps(payload or {}, ensure_ascii=False))
        if rc != 0:
            return {"ok": False, "error": "remote_failed", "exit": rc, "detail": text[-400:]}
        try:
            return {"ok": True, "value": json.loads(text.strip().splitlines()[-1])}
        except (ValueError, IndexError):
            return {"ok": False, "error": "bad_json", "detail": text[-400:]}

    def read_json(self, relpath, timeout=60):
        code = (
            "import json, os, sys\n"
            "p = os.path.join(%r, %r)\n" % (REMOTE + "/data", relpath)
            + "if not os.path.isfile(p):\n"
            "    print('null'); sys.exit(0)\n"
            "print(open(p, encoding='utf-8').read())\n"
        )
        rc, text = self.python(code, timeout=timeout)
        if rc != 0:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def write_json(self, relpath, obj, timeout=60):
        """远端原子落盘：tmp → fsync → rename，别留半个文件。"""
        code = (
            "import json, os, sys\n"
            "p = os.path.join(%r, %r)\n" % (REMOTE + "/data", relpath)
            + "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "data = json.load(sys.stdin)\n"
            "tmp = p + '.tmp'\n"
            "with open(tmp, 'w', encoding='utf-8') as f:\n"
            "    json.dump(data, f, ensure_ascii=False, indent=1)\n"
            "    f.flush(); os.fsync(f.fileno())\n"
            "os.replace(tmp, p)\n"
            "print('ok')\n"
        )
        rc, text = self.python(code, timeout=timeout, stdin=json.dumps(obj, ensure_ascii=False))
        return rc == 0 and text.strip().endswith("ok")


# ---------------------------------------------------------------- 采集

# 采集全部走 argv 列表或 /proc，不拼 shell：这些命令跑在生产 root 下，
# 任何字符串拼接都是多余的注入面。日志只统计错误类型和条数，绝不回传原句。
HEALTH_SNIPPET = r"""
import collections, json, os, re, subprocess, time, urllib.request

def argv(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or p.stderr or '').strip()
    except Exception as e:
        return 'error:' + type(e).__name__

def listening(port):
    want = '%04X' % port
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[3] == '0A' and parts[1].endswith(':' + want):
                        return True
        except OSError:
            continue
    return False

out = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
out['qqbot'] = argv(['systemctl', 'is-active', 'qqbot.service'])
out['napcat'] = argv(['docker', 'inspect', '-f', '{{.State.Status}}', 'napcat'])
out['onebot_port'] = listening(3001)
try:
    st = os.statvfs('/opt')
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    out['disk_used_pct'] = round((total - free) * 100.0 / total, 1) if total else None
    out['disk_free_gb'] = round(free / 1073741824.0, 2)
except OSError:
    out['disk_used_pct'] = None
mem = {}
try:
    with open('/proc/meminfo') as f:
        for line in f:
            k, _, v = line.partition(':')
            mem[k] = int(v.strip().split()[0])
    out['mem_used_mb'] = (mem.get('MemTotal', 0) - mem.get('MemAvailable', 0)) // 1024
    out['mem_total_mb'] = mem.get('MemTotal', 0) // 1024
except (OSError, ValueError):
    pass
try:
    out['load'] = [round(x, 2) for x in os.getloadavg()]
except OSError:
    pass
try:
    with urllib.request.urlopen('http://127.0.0.1:5000/admin/login', timeout=8) as r:
        out['web'] = r.status
except Exception as e:
    out['web'] = type(e).__name__

counts = collections.Counter()
try:
    with open('/opt/qqbot/logs/Bot.log', encoding='utf-8', errors='replace') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400000))
        lines = f.read().splitlines()[-1200:]
    for line in lines:
        for tag in ('连接断开', '消息处理失败', 'Traceback', '发送失败', '写盘失败',
                    '模型调用失败', 'OneBotChannel.call 失败', '运维'):
            if tag in line:
                counts[tag] += 1
        m = re.search(r'([A-Za-z_]*(?:Error|Exception))', line)
        if m:
            counts['exc:' + m.group(1)] += 1
    out['log_lines_scanned'] = len(lines)
except OSError as e:
    out['log_error'] = type(e).__name__
out['log_tags'] = dict(counts)
print(json.dumps(out, ensure_ascii=False))
"""


def health(remote):
    rc, text = remote.python(HEALTH_SNIPPET, timeout=90)
    if rc != 0:
        return {"ok": False, "error": "health_failed", "detail": text[-300:]}
    try:
        data = json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "bad_json", "detail": text[-300:]}
    issues = []
    if data.get("qqbot") != "active":
        issues.append("qqbot 服务不是 active（%s）" % data.get("qqbot"))
    if data.get("napcat") != "running":
        issues.append("NapCat 容器不是 running（%s）" % data.get("napcat"))
    if not data.get("onebot_port"):
        issues.append("OneBot 3001 端口没在监听")
    if data.get("web") not in (200, 302):
        issues.append("Web 面板返回 %s" % data.get("web"))
    used = data.get("disk_used_pct")
    if isinstance(used, (int, float)) and used >= 90:
        issues.append("磁盘已用 %s%%" % used)
    data["ok"] = not issues
    data["issues"] = issues
    return data


def preflight(remote, profile="evidence", timeout=200):
    """云端只读前置检测。profile 只认固定枚举，不接受任意参数。"""
    if profile not in ("evidence", "isolated"):
        return {"ok": False, "error": "bad_profile"}
    cmd = ("cd " + shlex.quote(REMOTE) + " && DATA_DIR=" + shlex.quote(REMOTE + "/data") +
           " PERSONA_DATA_DIR=" + shlex.quote(REMOTE + "/data/persona") +
           " QQ_DRY_RUN=1 PYTHONUTF8=1 " + _py() + " -B bot/qingyi_preflight.py --profile " +
           profile + " --json")
    rc, text = remote.run(cmd, timeout=timeout)
    # 退出码 0/1/3/4 都是结论不是故障，只有 2 和负数才是执行失败。
    if rc in (2, -1):
        return {"ok": False, "error": "preflight_failed", "exit": rc, "detail": text[-400:]}
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "bad_json", "exit": rc, "detail": text[-400:]}
    data["ok"] = True
    data["exit_code"] = rc
    return data


def readiness(remote, timeout=180):
    cmd = ("cd " + shlex.quote(REMOTE) + " && DATA_DIR=" + shlex.quote(REMOTE + "/data") +
           " PERSONA_DATA_DIR=" + shlex.quote(REMOTE + "/data/persona") +
           " QQ_DRY_RUN=1 PYTHONUTF8=1 " + _py() + " -B bot/phase_audit.py --readiness --json")
    rc, text = remote.run(cmd, timeout=timeout)
    if rc not in (0, 1):
        return {"ok": False, "error": "readiness_failed", "exit": rc, "detail": text[-400:]}
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "bad_json", "detail": text[-400:]}
    data["ok"] = True
    return data


def backup_persona(remote, label="ops", keep=0, timeout=300):
    """给 persona 的每个 SQLite 库做在线 .backup（不复制单个 .db 假装一致快照）。

    keep=0 表示不清理旧备份：删备份是不可逆操作，只由主人显式要求时才做。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = "%s/%s-%s" % (BACKUP_ROOT, label, stamp)
    code = (
        "import glob, json, os, sqlite3\n"
        "src = %r\n" % (REMOTE + "/data/persona")
        + "dst = %r\n" % dest
        + "os.makedirs(dst, exist_ok=True)\n"
        "done, failed = [], []\n"
        "for p in sorted(glob.glob(os.path.join(src, '*.db'))):\n"
        "    name = os.path.basename(p)\n"
        "    try:\n"
        "        s = sqlite3.connect('file:' + p + '?mode=ro', uri=True, timeout=15)\n"
        "        d = sqlite3.connect(os.path.join(dst, name))\n"
        "        s.backup(d); d.close(); s.close()\n"
        "        done.append(name)\n"
        "    except Exception as e:\n"
        "        failed.append(name + ':' + type(e).__name__)\n"
        "total = 0\n"
        "for root, _, files in os.walk(%r):\n" % BACKUP_ROOT
        + "    for f in files:\n"
        "        try: total += os.path.getsize(os.path.join(root, f))\n"
        "        except OSError: pass\n"
        "print(json.dumps({'dir': dst, 'backed_up': done, 'failed': failed,\n"
        "                  'backup_root_bytes': total,\n"
        "                  'backup_dirs': len(glob.glob(%r))}, ensure_ascii=False))\n"
        % (BACKUP_ROOT + "/*")
    )
    rc, text = remote.python(code, timeout=timeout)
    if rc != 0:
        return {"ok": False, "error": "backup_failed", "detail": text[-400:]}
    try:
        data = json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "bad_json", "detail": text[-400:]}
    data["ok"] = not data.get("failed")
    return data


def restart_qqbot(remote, timeout=120):
    """明显的机械阻塞才用：服务挂了才重启，且重启后必须复核。"""
    rc, _ = remote.run("systemctl restart qqbot.service", timeout=timeout)
    if rc != 0:
        return {"ok": False, "error": "restart_failed", "exit": rc}
    time.sleep(6)
    rc, text = remote.run("systemctl is-active qqbot.service", timeout=30)
    active = text.strip() == "active"
    return {"ok": active, "state": text.strip(), "error": "" if active else "not_active_after_restart"}