"""清漪前置检测的单活动任务。只写诊断任务元数据与报告，不写认知/记忆账本。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
import time

RUN_ID = re.compile(r'^[0-9a-f]{32}$')
LEASE_SECONDS = 10
GRACE_SECONDS = 10
DEADLINE_SECONDS = {'evidence': 30, 'isolated': 120}
# 正常报告几十KB；上限防失控输出撑爆内存（统一方案 §23.3 要求输出有大小上限）。
# text 模式按字符计，8M 字符已是正常报告的百倍以上。
MAX_REPORT_CHARS = 8_000_000
MAX_STDERR_CHARS = 1_000_000


def root():
    return Path(os.getenv('DATA_DIR', './data')) / 'diagnostics' / 'qingyi'


def db_path():
    return root() / 'preflight_jobs.db'


def reports_dir():
    return root() / 'reports'


def _connect():
    reports_dir().mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path()), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA busy_timeout=5000')
    return connection


def _read_connect():
    """GET只读取已存在的诊断库；缺库时不创建表或目录。"""
    path = db_path()
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection
    except sqlite3.Error:
        return None


def _ensure(connection):
    connection.executescript('''
      CREATE TABLE IF NOT EXISTS jobs(
        run_id TEXT PRIMARY KEY, request_key TEXT NOT NULL, profile TEXT NOT NULL,
        scope TEXT NOT NULL, state TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL,
        deadline REAL NOT NULL, owner TEXT NOT NULL, fence INTEGER NOT NULL,
        finished REAL, report_path TEXT, error TEXT)
      ;CREATE TABLE IF NOT EXISTS active(
        id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL, owner TEXT NOT NULL,
        fence INTEGER NOT NULL, lease REAL NOT NULL, deadline REAL NOT NULL)
    ;''')


def _row(connection, run_id):
    row = connection.execute('SELECT * FROM jobs WHERE run_id=?', (run_id,)).fetchone()
    return dict(row) if row else None


def _public(row):
    if not row:
        return None
    return {'run_id': row['run_id'], 'profile': row['profile'], 'scope': row['scope'],
            'state': row['state'], 'started': row['started'], 'updated': row['updated'],
            'deadline': row['deadline'], 'finished': row.get('finished'),
            'fence': row['fence'], 'error': row.get('error') or ''}


def claim(profile, scope, request_key, now=None):
    if profile not in DEADLINE_SECONDS or not str(request_key or '').strip():
        raise ValueError('invalid_preflight_request')
    at = time.time() if now is None else float(now)
    reports_dir().mkdir(parents=True, exist_ok=True)
    with closing(_connect()) as connection:
        _ensure(connection)
        connection.execute('BEGIN IMMEDIATE')
        active = connection.execute('SELECT * FROM active WHERE id=1').fetchone()
        if active:
            old = _row(connection, active['run_id'])
            if old and old['state'] == 'running':
                if at <= float(active['lease']):
                    if old['request_key'] == request_key:
                        connection.rollback()
                        return dict(old, reused=True, state='running')
                    connection.rollback()
                    return {'state': 'conflict', 'existing_run_id': old['run_id'],
                            'profile': old['profile'], 'scope': old['scope']}
                replacement_at = float(active['deadline']) + GRACE_SECONDS
                if at <= replacement_at:
                    connection.rollback()
                    return {'state': 'recovering', 'existing_run_id': old['run_id'],
                            'profile': old['profile'], 'scope': old['scope']}
                connection.execute('UPDATE jobs SET state=?,finished=?,updated=?,error=? WHERE run_id=?',
                                   ('interrupted', at, at, 'owner_lease_expired', old['run_id']))
        connection.execute('DELETE FROM active WHERE id=1')
        run_id = uuid.uuid4().hex
        owner = uuid.uuid4().hex
        deadline = at + DEADLINE_SECONDS[profile]
        connection.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (run_id, request_key, profile, scope, 'running', at, at,
                            deadline, owner, 1, None, None, None))
        connection.execute('INSERT INTO active VALUES(?,?,?,?,?,?)',
                           (1, run_id, owner, 1, at + LEASE_SECONDS, deadline))
        connection.commit()
        return dict(_row(connection, run_id), reused=False, state='running')


def heartbeat(run_id, owner, fence, now=None):
    at = time.time() if now is None else float(now)
    with closing(_connect()) as connection:
        _ensure(connection)
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute(
            "UPDATE jobs SET updated=? WHERE run_id=? AND owner=? AND fence=? AND state='running'",
            (at, run_id, owner, int(fence))).rowcount
        if changed:
            connection.execute('UPDATE active SET lease=? WHERE id=1 AND run_id=? AND owner=? AND fence=?',
                               (at + LEASE_SECONDS, run_id, owner, int(fence)))
        connection.commit()
        return bool(changed)


def finish(run_id, owner, fence, state, report=None, error=None, now=None):
    at = time.time() if now is None else float(now)
    if state not in ('pass', 'blocked', 'unknown', 'timed_out', 'error'):
        raise ValueError('invalid_terminal_state')
    if not RUN_ID.match(str(run_id or '')):
        raise ValueError('invalid_run_id')
    reports_dir().mkdir(parents=True, exist_ok=True)
    report_path = reports_dir() / (run_id + '.json')
    if report is not None:
        temp = report_path.with_suffix('.%s.tmp' % uuid.uuid4().hex)
        temp.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        os.replace(temp, report_path)
    with closing(_connect()) as connection:
        _ensure(connection)
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute(
            'UPDATE jobs SET state=?,finished=?,updated=?,report_path=?,error=? '
            "WHERE run_id=? AND owner=? AND fence=? AND state='running'",
            (state, at, at, str(report_path) if report is not None else None,
             str(error or '')[:200], run_id, owner, int(fence))).rowcount
        if changed:
            connection.execute('DELETE FROM active WHERE id=1 AND run_id=? AND owner=? AND fence=?',
                               (run_id, owner, int(fence)))
        connection.commit()
        return bool(changed)


def get(run_id):
    if not RUN_ID.match(str(run_id or '')):
        return None
    connection = _read_connect()
    if connection is None:
        return None
    try:
        row = _row(connection, run_id)
        return _public(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def latest():
    connection = _read_connect()
    if connection is None:
        return None
    try:
        row = connection.execute('SELECT * FROM jobs ORDER BY started DESC LIMIT 1').fetchone()
        return _public(dict(row)) if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def report_path(run_id):
    if not RUN_ID.match(str(run_id or '')):
        raise ValueError('invalid_run_id')
    return root() / 'reports' / (run_id + '.json')


def read_report(run_id):
    try:
        path = report_path(run_id)
    except ValueError:
        return None
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def request_key(profile, scope, data_root):
    return hashlib.sha256(('%s|%s|%s' % (profile, scope, data_root)).encode()).hexdigest()


def _worker_environment():
    keep = ('PATH', 'SYSTEMROOT', 'COMSPEC', 'TEMP', 'TMP', 'PYTHONHOME')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update({
        'DATA_DIR': os.getenv('DATA_DIR', './data'),
        'PERSONA_DATA_DIR': os.getenv('PERSONA_DATA_DIR', ''),
        'QQ_DRY_RUN': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
    })
    return env


def extract_report(stdout):
    """worker可能夹带普通日志；只认最后一条带schema_version的JSON对象。"""
    for line in reversed((stdout or '').splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('schema_version') == 1 and 'checks' in value:
            return value
    raise ValueError('invalid_preflight_report')


def _drain_pipe(stream, cap, process, box, key):
    """抽干一根管道；超上限就杀掉子进程并丢弃余量。
    不能只读一半就停：子进程写满管道会永远阻塞，连 deadline 都等不到退出。"""
    kept = []
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                box[key + '_overflow'] = True
                try:
                    process.kill()
                except OSError:
                    pass
                while stream.read(65536):
                    pass
                break
            kept.append(chunk)
    except Exception:  # kill 后管道可能已关闭
        pass
    box[key] = ''.join(kept)


def _supervise(job):
    script = Path(__file__).resolve().parent.parent / 'bot' / 'qingyi_preflight.py'
    owner, fence = job['owner'], int(job['fence'])
    try:
        process = subprocess.Popen([sys.executable, '-B', str(script), '--profile', job['profile'], '--json'],
                                   cwd=str(script.parent.parent), env=_worker_environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding='utf-8', errors='replace')
    except Exception as exc:
        finish(job['run_id'], owner, fence, 'error', error=type(exc).__name__, now=time.time())
        return
    budget = max(1.0, float(job['deadline']) - time.time())
    # 报告可能有几十KB：必须边跑边抽干管道，否则子进程写满管道会永远等不到退出。
    # 同时在这个线程里续租，10秒租约才不会把健康的长任务误判成失联。
    box = {}

    def pump():
        # stderr 单独一根线程抽干：只读 stdout 时子进程可能先写满 stderr 管道。
        err_box = {}
        err_reader = threading.Thread(
            target=_drain_pipe, args=(process.stderr, MAX_STDERR_CHARS, process, err_box, 'stderr'),
            daemon=True, name='qingyi-preflight-stderr')
        err_reader.start()
        _drain_pipe(process.stdout, MAX_REPORT_CHARS, process, box, 'stdout')
        err_reader.join(5)
        box['stderr'] = err_box.get('stderr', '')
        box['stderr_overflow'] = err_box.get('stderr_overflow', False)

    reader = threading.Thread(target=pump, daemon=True, name='qingyi-preflight-pipes')
    reader.start()
    expires = time.monotonic() + budget
    while reader.is_alive() and time.monotonic() < expires:
        heartbeat(job['run_id'], owner, fence)
        time.sleep(min(1.0, max(0.05, expires - time.monotonic())))
    if reader.is_alive():
        process.kill()
        reader.join(5)
        finish(job['run_id'], owner, fence, 'timed_out', error='preflight_deadline', now=time.time())
        return
    reader.join()
    try:
        process.wait(timeout=5)
    except Exception:
        pass
    if box.get('stdout_overflow') or box.get('stderr_overflow'):
        finish(job['run_id'], owner, fence, 'error',
               error='report_too_large' if box.get('stdout_overflow') else 'stderr_too_large',
               now=time.time())
        return
    stdout, stderr = box.get('stdout') or '', box.get('stderr') or ''
    try:
        report = extract_report(stdout)
        report['run_id'] = job['run_id']
        state = {'pass': 'pass', 'blocked': 'blocked', 'not_observed': 'blocked',
                 'unknown': 'unknown'}.get(report.get('scope_status'), 'blocked')
        if stderr:
            report.setdefault('diagnostics', {})['worker_stderr_sha256'] = hashlib.sha256(
                stderr.encode('utf-8', 'replace')).hexdigest()
        # 0=pass,1=blocked,3=unknown,4=待人工评审；这些是检测结论，不是故障。
        code = process.returncode
        finish(job['run_id'], owner, fence, state, report=report,
               error=None if code in (0, 1, 3, 4) else 'worker_exit_%s' % code,
               now=time.time())
    except ValueError as exc:
        # extract_report 的 ValueError 是稳定 reason_code，可以直接留账。
        finish(job['run_id'], owner, fence, 'error', error=str(exc) or type(exc).__name__, now=time.time())
    except Exception as exc:
        finish(job['run_id'], owner, fence, 'error', error=type(exc).__name__, now=time.time())


def start(profile='evidence', scope='qingyi_all'):
    if profile not in DEADLINE_SECONDS or scope != 'qingyi_all':
        raise ValueError('invalid_preflight_request')
    claimed = claim(profile=profile, scope=scope,
                    request_key=request_key(profile, scope, os.getenv('DATA_DIR', './data')))
    state = claimed.get('state')
    if state != 'running':
        # conflict/recovering 只带run_id、profile、scope，没有owner。
        return dict(claimed)
    if claimed.get('reused'):
        return dict(_public(claimed), state='running', reused=True)
    thread = threading.Thread(target=_supervise, args=(claimed,), daemon=True,
                              name='qingyi-preflight')
    thread.start()
    return dict(_public(claimed), state='queued', reused=False)
