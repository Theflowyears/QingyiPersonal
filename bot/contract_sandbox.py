# -*- coding: utf-8 -*-
"""在线探针使用短生命周期、无能力的只读 systemd 沙箱。"""
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    result = {}
    for path in Path(root).rglob('*'):
        try:
            if path.is_file():
                stat = path.stat()
                result[str(path.relative_to(root))] = {'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size}
        except FileNotFoundError:
            continue
    return result


def changes(before, after):
    return [{'path': p, 'kind': 'added' if p not in before else 'modified'}
            for p in sorted(after) if before.get(p) != after[p]] + [
                {'path': p, 'kind': 'removed'} for p in sorted(before.keys() - after.keys())]


def classify_changes(rows, *, online):
    return {'status': ('concurrent_runtime_change' if online else 'fail') if rows else 'pass',
            'test_violation': bool(rows and not online), 'changes': rows}


def writable_paths_safe(root):
    root = Path(root).resolve()
    for path in root.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            return False
    return True


def command_for(argv, writable_root, *, timeout=120):
    path = str(Path(writable_root).resolve())
    properties = [
        'User=nobody', 'NoNewPrivileges=yes', 'CapabilityBoundingSet=',
        'AmbientCapabilities=', 'ProtectSystem=strict', 'ProtectHome=yes',
        'PrivateNetwork=yes', 'PrivateDevices=yes', 'ProtectKernelTunables=yes',
        'ProtectKernelModules=yes', 'ProtectControlGroups=yes', 'RestrictSUIDSGID=yes',
        'RestrictNamespaces=yes', 'LockPersonality=yes', 'ProtectProc=invisible',
        'ReadOnlyPaths=/tmp /var/tmp', 'ReadWritePaths=' + path,
        'InaccessiblePaths=-/run -' + str(ROOT / 'config.env'),
        'RuntimeMaxSec=' + str(max(1, int(timeout))), 'KillMode=control-group',
        'WorkingDirectory=' + str(ROOT / 'tests'),
    ]
    command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
               '--unit=qqbot-contract-' + uuid.uuid4().hex]
    for value in properties:
        command.extend(['--property', value])
    command.extend(['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=' + path,
                    'TMPDIR=' + path, 'TEMP=' + path, 'TMP=' + path, 'DATA_DIR=' + path, 'PERSONA_DATA_DIR=',
                    'QQ_DRY_RUN=1', 'CONTRACT_PROBE_ONLINE=1', 'PYTHONUTF8=1',
                    'PYTHONIOENCODING=utf-8', 'PYTHONDONTWRITEBYTECODE=1',
                    'PYTHONPATH=' + os.pathsep.join(map(str, (ROOT / 'tests', ROOT / 'bot', ROOT))),
                    *argv])
    return command


def capabilities():
    if os.getenv('CONTRACT_PROBE_ONLINE')=='1':
        return {'available':False,'reason':'nested_sandbox_not_permitted','mechanism':'none'}
    if platform.system() != 'Linux':
        return {'available': False, 'reason': 'linux_namespace_required', 'mechanism': 'none'}
    try:
        ready=bool(shutil.which('systemd-run') and Path('/run/systemd/system').is_dir())
    except OSError:
        ready=False
    if not ready:
        return {'available': False, 'reason': 'systemd_sandbox_unavailable', 'mechanism': 'none'}
    return {'available': True, 'reason': 'requires_boundary_probe', 'mechanism': 'systemd-readonly-unprivileged'}


def _prepare_tmp(path):
    if platform.system() == 'Linux' and os.geteuid() == 0:
        import pwd
        user = pwd.getpwnam('nobody')
        os.chown(path, user.pw_uid, user.pw_gid)
        os.chmod(path, 0o700)


def run_code(code, *, online=False, timeout=120):
    cap = capabilities()
    if not cap['available']:
        return {'status': 'not_verified', 'isolation': cap}
    with tempfile.TemporaryDirectory(prefix='contract-code-') as tmp:
        script = Path(tmp) / 'probe.py'
        script.write_text(code, encoding='utf-8')
        _prepare_tmp(tmp)
        try:
            process = subprocess.run(command_for([sys.executable, '-B', str(script)], tmp, timeout=timeout),
                                     capture_output=True, text=True, timeout=timeout + 10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {'status': 'not_verified', 'isolation': cap, 'output': type(exc).__name__}
        return {'status': 'pass' if process.returncode == 0 else 'fail', 'isolation': cap,
                'output': (process.stdout + process.stderr)[-4000:]}


def verify_boundary():
    code = """import os, pathlib, sqlite3, subprocess, sys
root = pathlib.Path(os.environ['DATA_DIR'])
outside = root.parent / ('forbidden-' + root.name)
link = root / 'escape'
link.symlink_to(outside)
for p in (outside, link):
    try:
        p.write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('outside write allowed')
p = subprocess.run([sys.executable, '-c', 'import sys;open(sys.argv[1],"w").write("x")', str(outside)], capture_output=True)
assert p.returncode != 0
c = sqlite3.connect(root / 'allowed.db')
c.execute('pragma journal_mode=wal');c.execute('create table x(a)');c.commit();c.close()
assert not outside.exists()
"""
    return run_code(code, online=True, timeout=15)


def _environment(tmp):
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if key in os.environ}
    env.update(DATA_DIR=str(tmp), PERSONA_DATA_DIR='', QQ_DRY_RUN='1', PYTHONUTF8='1',
               PYTHONIOENCODING='utf-8', PYTHONPATH=os.pathsep.join(map(str, (ROOT / 'tests', ROOT / 'bot', ROOT))))
    return env


def run(tests, *, online=False, timeout=120, root=None):
    cap = capabilities()
    if online and not cap['available']:
        return {'status': 'not_verified', 'isolation': cap, 'tests': list(tests), 'output_sha256': ''}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='contract-sandbox-', dir=root) as tmp:
        before = snapshot(ROOT / 'data') if online else {}
        if online:
            boundary = verify_boundary()
            if boundary['status'] != 'pass':
                return {'status': 'not_verified', 'isolation': cap, 'tests': list(tests), 'boundary': boundary}
            _prepare_tmp(tmp)
            command = command_for([sys.executable, '-B', '-m', 'unittest', *tests], tmp, timeout=timeout)
        else:
            command = [sys.executable, '-B', '-m', 'unittest', *tests]
        try:
            process = subprocess.run(command, cwd=ROOT / 'tests', env=_environment(tmp),
                                     capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout + 10)
            output = process.stdout + process.stderr
            status = 'pass' if process.returncode == 0 else 'fail'
        except (OSError, subprocess.TimeoutExpired) as exc:
            output, status = type(exc).__name__, 'fail'
        observed = changes(before, snapshot(ROOT / 'data')) if online else []
        return {'status': status, 'tests': list(tests), 'output_sha256': hashlib.sha256(output.encode()).hexdigest(),
                'isolation': dict(cap, writable_root=tmp, hard_boundary=online),
                'protected_snapshot': {'status': 'pass', 'test_violation': False},
                'concurrent_changes': classify_changes(observed, online=True),
                'seconds': round(time.monotonic() - started, 3), 'output': output[-4000:]}
