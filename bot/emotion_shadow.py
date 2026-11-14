"""情感影子消费者：v2_live 实时轨迹的唯一在线写入者。

三轨迹口径见 emotion_trajectories：本模块的 tick/status 是 v2 实时观察；
valence_samples 是候选反事实回放（只读重建，不计在线、不回写实时状态）；
shadow_retrospective 是历史追溯视图（不计在线）。v1 冻结账本原样保留作参考。
"""
import copy
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import emotion_core as legacy
import emotion_dynamics as core
import emotion_trajectories as trajectories

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS shadow_run (
 id INTEGER PRIMARY KEY CHECK(id=1), parameter_hash TEXT NOT NULL,
 parameters TEXT NOT NULL, observation_started REAL NOT NULL,
 last_tick REAL NOT NULL, observed_seconds REAL NOT NULL DEFAULT 0,
 gap_seconds REAL NOT NULL DEFAULT 0, cursor INTEGER NOT NULL,
 cursor_id TEXT NOT NULL DEFAULT '', processed INTEGER NOT NULL DEFAULT 0,
 real_events INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_event (
 source_rowid INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
 at REAL NOT NULL, kind TEXT NOT NULL, appraisal TEXT NOT NULL,
 before_state TEXT NOT NULL, after_state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_gap (
 started REAL NOT NULL, ended REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_retrospective (
 id INTEGER PRIMARY KEY CHECK(id=1), report TEXT NOT NULL
);
'''


def _root():
    return Path(os.getenv('PERSONA_DATA_DIR') or
                str(Path(os.getenv('DATA_DIR', './data')) / 'persona'))


def shadow_path():
    # v1 冻结账本是参考轨迹，保留历史不覆盖成 v2；v2 实时观察另开独立库。
    path = _root() / 'emotion_shadow.db'
    versioned = _root() / 'emotion_shadow_v2.db'
    if versioned.is_file():
        return versioned
    if path.is_file():
        with closing(_readonly(path)) as c:
            run = c.execute('SELECT parameter_hash FROM shadow_run WHERE id=1').fetchone()
        if run is not None and run['parameter_hash'] == legacy.parameter_hash():
            return versioned
    return path


def _source_path():
    return _root() / 'cognition.db'


def _readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True,
                        timeout=2)
    c.row_factory = sqlite3.Row
    # S04 分离边界：本模块所有跨账本读取（cognition 源、候选账本、v1 参考、
    # 实时账本的状态/复核）都经由此助手。mode=ro 挡住开库即写，query_only
    # 再把「只读」变成结构性事实——将来即使误加写语句也会当场被拒绝。
    # 本模块唯一可写连接是 tick 的实时账本（shadow_path()），不经过这里。
    c.execute('PRAGMA query_only=ON')
    return c


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _reject_nonfinite(_value):
    raise ValueError('nonfinite value in shadow ledger')


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('overflowing number in shadow ledger')
    return result


def _load(encoded):
    return json.loads(encoded, parse_constant=_reject_nonfinite, parse_float=_finite_float)


def _ignored(event, reason):
    return {'rule': 'quarantine', 'confidence': 0.0, 'ignored': True,
            'reason': reason, 'version': core.VERSION,
            'synthetic': False, 'evidence_ids': [event['id']],
            'delta': {k: 0.0 for k in ('valence', 'arousal', 'control', 'attention')}}


def _excluded(event, payload):
    source = str(event.get('source') or '').casefold()
    session = str(event.get('session') or '').casefold()
    markers = ('legacy', 'synthetic', 'verify', 'verification', 'test', 'probe')
    return bool(payload.get('synthetic') or event.get('synthetic') or
                payload.get('evidence_origin') == 'verification' or
                any(value == marker or value.startswith(tuple(marker + sep for sep in '/:_-'))
                    for value in (source, session) for marker in markers))


def _evaluation(event, now):
    return _source_evaluation(event, now)


def _source_evaluation(event, now):
    try:
        payload = _load(event.get('payload') or '{}')
        if not isinstance(payload, dict):
            raise ValueError('payload_not_object')
    except (ValueError, TypeError, RecursionError):
        return _ignored(event, 'invalid_payload')
    event['payload'] = payload
    if _excluded(event, payload):
        return _ignored(event, 'not_live_evidence')
    try:
        ts = float(event.get('ts') or 0)
        recorded = float(event.get('created') or 0)
        if (not math.isfinite(ts) or not math.isfinite(recorded)
                or not 0 < ts <= recorded):
            return _ignored(event, 'invalid_event_time')
        evaluated = dict(event)
        if evaluated['kind'] in ('group_message', 'private_message'):
            evaluated['kind'] = 'message'
        return core.appraise(evaluated)
    except (ValueError, TypeError, OverflowError):
        return _ignored(event, 'invalid_appraisal_input')


def _backfill_day(source, dest, run):
    stored = dest.execute('SELECT report FROM shadow_retrospective WHERE id=1').fetchone()
    if stored:
        try:
            old = _load(stored['report'])
        except (ValueError, TypeError):
            old = {}
        if (old.get('rule_version') == core.VERSION
                and old.get('vocab_version') == getattr(core, 'VOCAB_VERSION', '')):
            return
    end = run['observation_started']
    local = time.localtime(end)
    try:
        start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    except (OverflowError, OSError):
        start = max(0, end - local.tm_hour * 3600 - local.tm_min * 60 - local.tm_sec - end % 1)
    prior = _root() / 'emotion_shadow.db'
    if prior != shadow_path() and prior.is_file():
        with closing(_readonly(prior)) as old:
            origin = old.execute('SELECT observation_started FROM shadow_run WHERE id=1').fetchone()
        if origin is not None:
            origin_at = origin['observation_started']
            origin_day = time.localtime(origin_at)
            try:
                origin_start = time.mktime((origin_day.tm_year, origin_day.tm_mon, origin_day.tm_mday,
                                            0, 0, 0, 0, 0, -1))
            except (OverflowError, OSError):
                origin_start = max(0, origin_at - origin_day.tm_hour * 3600 - origin_day.tm_min * 60
                                   - origin_day.tm_sec - origin_at % 1)
            start = min(start, origin_start)
    state = core.initial_state(start)
    dimensions = {k: {'implemented': True, 'observed': False, 'nonzero_events': 0,
                      'positive': 0, 'negative': 0, 'min': None, 'max': None} for k in _DIMENSIONS}
    effective = excluded = events = 0
    for row in source.execute('SELECT * FROM event WHERE created>=? AND created<=? ORDER BY rowid', (start, end)):
        event = dict(row)
        events += 1
        appraisal = _evaluation(event, end)
        if appraisal['rule'] == 'quarantine':
            excluded += 1
            continue
        event['effective_ts'] = max(state['at'], event['ts'], event['created'])
        evaluated = dict(event)
        if evaluated['kind'] in ('group_message', 'private_message'):
            evaluated['kind'] = 'message'
        state = core.apply_event(state, evaluated, appraisal)
        drives = not appraisal['ignored'] and any(appraisal['delta'].values())
        effective += bool(drives)
        for key in _DIMENSIONS:
            dim = dimensions[key]
            delta = appraisal['delta'][key]
            dim['nonzero_events'] += delta != 0
            dim['observed'] = dim['nonzero_events'] > 0
            dim['positive'] += delta > 0
            dim['negative'] += delta < 0
            dim['min'] = min(dim['min'], state[key]) if dim['min'] is not None else state[key]
            dim['max'] = max(dim['max'], state[key]) if dim['max'] is not None else state[key]
    report = trajectories.stamp(dict(
        mode='retrospective', day=time.strftime('%Y-%m-%d', local),
        source_start_day=time.strftime('%Y-%m-%d', time.localtime(start)),
        source_end_day=time.strftime('%Y-%m-%d', time.localtime(end)),
        events=events, effective_events=effective, excluded_events=excluded,
        source_start=start, source_end=end, rule_version=core.VERSION,
        vocab_version=getattr(core, 'VOCAB_VERSION', ''),
        dimensions=dimensions, state=core.advance(state, end),
        note='仅按既有账本追溯；不计实时样本、心跳时长或评审进度'),
        trajectories.HISTORICAL_REPLAY)
    dest.execute('INSERT OR REPLACE INTO shadow_retrospective VALUES(1,?)', (_dump(report),))


def tick(now=None, batch_size=100):
    if os.getenv('EMOTION_SHADOW_ENABLED', '1').lower() in ('0', 'off', 'false'):
        return {'running': False, 'reason': 'disabled', 'mode': 'shadow'}
    now = time.time() if now is None else float(now)
    if not _source_path().is_file():
        return {'running': False, 'reason': 'waiting_for_cognition', 'mode': 'shadow'}
    size = min(500, max(1, int(batch_size)))
    _root().mkdir(parents=True, exist_ok=True)
    with closing(_readonly(_source_path())) as source, closing(sqlite3.connect(
            shadow_path(), timeout=3)) as dest:
        dest.row_factory = sqlite3.Row
        dest.executescript(_SCHEMA)
        try:
            dest.execute('BEGIN IMMEDIATE')
            run = dest.execute('SELECT * FROM shadow_run WHERE id=1').fetchone()
            if run is None:
                latest = source.execute('SELECT rowid,id FROM event ORDER BY rowid DESC LIMIT 1').fetchone()
                cursor, cursor_id = (latest['rowid'], latest['id']) if latest else (0, '')
                dest.execute('INSERT INTO shadow_run '
                             '(id,parameter_hash,parameters,observation_started,last_tick,cursor,cursor_id,state) '
                             'VALUES(1,?,?,?,?,?,?,?)',
                             (core.parameter_hash(), _dump(core.PARAMETERS), now, now,
                              cursor, cursor_id, _dump(core.initial_state(now))))
                dest.commit()
                return {'running': True, 'mode': 'shadow', 'processed': 0,
                        'cursor': cursor, 'initialized': True}
            if run['parameter_hash'] != core.parameter_hash():
                raise ValueError('frozen shadow parameters changed; start a separately reviewed run')
            dest.execute('SAVEPOINT retrospective')
            try:
                _backfill_day(source, dest, run)
            except (sqlite3.Error, ValueError, TypeError, OSError, OverflowError, RecursionError) as exc:
                dest.execute('ROLLBACK TO retrospective')
                print('[影子历史回放] 失败，不阻断实时观测 ' + type(exc).__name__, flush=True)
            finally:
                dest.execute('RELEASE retrospective')
            if run['cursor']:
                previous = source.execute('SELECT id FROM event WHERE rowid=?',
                                          (run['cursor'],)).fetchone()
                if previous is None or previous['id'] != run['cursor_id']:
                    raise ValueError('event source changed or was truncated; cursor not reset')
            rows = source.execute('SELECT rowid AS source_rowid,* FROM event '
                                  'WHERE rowid>? ORDER BY rowid LIMIT ?',
                                  (run['cursor'], size)).fetchall()
            state = _load(run['state'])
            processed, real_events = run['processed'], run['real_events']
            cursor, cursor_id = run['cursor'], run['cursor_id']
            consumed = 0
            deferred = 0
            for row in rows:
                event = dict(row)
                try:
                    recorded = float(event.get('created') or 0)
                except (ValueError, TypeError, OverflowError):
                    recorded = 0.0
                if math.isfinite(recorded) and recorded > now:
                    deferred = 1
                    break
                appraisal = _evaluation(event, now)
                if appraisal['rule'] == 'quarantine':
                    effective = float(state['at'])
                else:
                    effective = max(float(state['at']), float(event['ts']), recorded)
                event['effective_ts'] = effective
                before = core.advance(state, effective)
                if appraisal['rule'] == 'quarantine':
                    state = core.advance(state, event['effective_ts'])
                    state['processed'] += 1
                else:
                    evaluated = dict(event)
                    if evaluated['kind'] in ('group_message', 'private_message'):
                        evaluated['kind'] = 'message'
                    state = core.apply_event(state, evaluated, appraisal)
                dest.execute('INSERT INTO shadow_event VALUES(?,?,?,?,?,?,?)',
                             (event['source_rowid'], event['id'], state['at'],
                              event['kind'], _dump(appraisal), _dump(before), _dump(state)))
                processed += 1
                consumed += 1
                if (appraisal['rule'] != 'quarantine' and
                        event.get('kind') in ('message', 'group_message', 'private_message',
                                              'tool_result', 'action_result')):
                    real_events += 1
                cursor, cursor_id = event['source_rowid'], event['id']
            dt = max(0, now - run['last_tick'])
            # 超过两轮的间隙不冒充连续观测，事件仍按游标补读。
            covered = dt if dt <= 60 else 0.0
            if dt > 60:
                dest.execute('INSERT INTO shadow_gap VALUES(?,?)', (run['last_tick'], now))
            dest.execute('UPDATE shadow_run SET last_tick=?,observed_seconds=?,gap_seconds=?,'
                         'cursor=?,cursor_id=?,processed=?,real_events=?,state=? WHERE id=1',
                         (max(now, run['last_tick']), run['observed_seconds'] + covered,
                          run['gap_seconds'] + dt - covered, cursor, cursor_id,
                          processed, real_events, _dump(state)))
            dest.commit()
            return {'running': True, 'mode': 'shadow', 'processed': processed,
                    'cursor': cursor, 'batch': consumed, 'deferred': deferred}
        except Exception:
            dest.rollback()
            raise


def _run_kernel(run):
    if run['parameter_hash'] == legacy.parameter_hash():
        return legacy
    if run['parameter_hash'] == core.parameter_hash():
        return core
    raise ValueError('unknown frozen shadow parameters')


def verify_replay(path=None):
    """按已记录的事件与评估重放；path 只给只读审计用，默认仍是生产影子库。"""
    db = Path(path) if path else shadow_path()
    if not db.is_file():
        return {'ok': False, 'reason': 'no_shadow_run'}
    with closing(_readonly(db)) as c:
        run = c.execute('SELECT * FROM shadow_run WHERE id=1').fetchone()
        if run is None:
            return {'ok': False, 'reason': 'parameters_or_run_unavailable'}
        try:
            kernel = _run_kernel(run)
        except ValueError:
            return {'ok': False, 'reason': 'parameters_or_run_unavailable'}
        state = kernel.initial_state(run['observation_started'])
        count = 0
        error = 0.0
        for row in c.execute('SELECT * FROM shadow_event ORDER BY source_rowid'):
            expected = _load(row['after_state'])
            appraisal = _load(row['appraisal'])
            state = kernel.advance(state, row['at'])
            for key, delta in appraisal['delta'].items():
                low, high = kernel.PARAMETERS['bounds'][key]
                state[key] = min(high, max(low, state[key] + delta))
            state['processed'] += 1
            error = max(error, *(abs(state[k] - expected[k]) for k in
                                  ('valence', 'arousal', 'control', 'attention', 'at', 'processed')))
            count += 1
        saved = _load(run['state'])
        error = max(error, *(abs(state[k] - saved[k]) for k in
                              ('valence', 'arousal', 'control', 'attention', 'at', 'processed')))
    return {'ok': error <= 1e-10, 'events': count, 'max_error': error,
            'parameter_hash': run['parameter_hash'], 'mode': 'shadow'}


_CALIB_CACHE = {'key': None, 'ts': 0.0, 'replay': None}
_DIMENSIONS = ('valence', 'arousal', 'control', 'attention')
_DIAGNOSTIC_CACHE = {'key': None, 'at': 0.0, 'value': None}


def _ledger_fingerprint(path):
    return tuple((p.stat().st_size, p.stat().st_mtime_ns) if p.is_file() else None
                 for p in (Path(path), Path(str(path) + '-wal')))


def _replay_cached(c, run, now, rows):
    key = (str(shadow_path().resolve()), run['parameter_hash'], run['state'],
           hash(tuple(tuple(row) for row in rows)))
    if _CALIB_CACHE['key'] == key and 0 <= now - _CALIB_CACHE['ts'] < 300:
        return _CALIB_CACHE['replay']
    kernel = _run_kernel(run)
    state = kernel.initial_state(run['observation_started'])
    error = 0.0
    for row in rows:
        expected = _load(row['after_state'])
        before = _load(row['before_state'])
        appraisal = _load(row['appraisal'])
        state = kernel.advance(state, row['at'])
        error = max(error, *(abs(state[k] - before[k]) for k in _DIMENSIONS))
        for item, delta in appraisal['delta'].items():
            low, high = kernel.PARAMETERS['bounds'][item]
            state[item] = min(high, max(low, state[item] + delta))
        state['processed'] += 1
        error = max(error, *(abs(state[k] - expected[k]) for k in (*_DIMENSIONS, 'at', 'processed')))
    saved = _load(run['state'])
    error = max(error, *(abs(state[k] - saved[k]) for k in (*_DIMENSIONS, 'at', 'processed')))
    result = {'ok': bool(rows) and error <= 1e-10, 'events': len(rows), 'max_error': error}
    _CALIB_CACHE.update(key=key, ts=now, replay=result)
    return result


def _diagnostics(c, run, now, rows):
    cache_key = (str(shadow_path().resolve()), _ledger_fingerprint(_source_path()),
                 run['parameter_hash'], run['processed'], run['state'],
                 hash(tuple(tuple(row) for row in rows)))
    cached = (_DIAGNOSTIC_CACHE['key'] == cache_key and
              0 <= now - _DIAGNOSTIC_CACHE['at'] < 300)
    by_kind, by_rule, tools = {}, {}, {}
    dims = {k: {'implemented': _run_kernel(run) is core or k != 'arousal',
                'nonzero_events': 0, 'positive': 0, 'negative': 0,
                'min': None, 'max': None, 'observed': False} for k in _DIMENSIONS}
    source_rows = None
    backlog = {'known': False, 'pending': None, 'oldest_age_seconds': None,
               'ledger_consistent': False}
    evidence = {'ok': None, 'checked_events': 0, 'max_error': None,
                'source_missing': 0, 'appraisal_mismatches': 0}
    try:
        with closing(_readonly(_source_path())) as src:
            src.execute('BEGIN')
            start = min((r['source_rowid'] for r in rows), default=run['cursor'] + 1)
            source_rows = (None if cached else {
                r['source_rowid']: dict(r) for r in src.execute(
                    'SELECT rowid AS source_rowid,* FROM event WHERE rowid>=? AND rowid<=?',
                    (start, run['cursor']))})
            pending = src.execute('SELECT COUNT(*),MIN(created) FROM event WHERE rowid>?',
                                  (run['cursor'],)).fetchone()
            last = src.execute('SELECT id FROM event WHERE rowid=?', (run['cursor'],)).fetchone()
            ids = {r['source_rowid'] for r in rows}
            first = min(ids) if ids else run['cursor'] + 1
            consistent = ((not run['cursor'] or (last is not None and last['id'] == run['cursor_id']))
                          and (cached or all(i in ids for i in source_rows if first <= i <= run['cursor']))
                          and len(rows) == run['processed'])
            backlog.update(known=True, pending=pending[0], ledger_consistent=consistent,
                           oldest_age_seconds=max(0, now - pending[1]) if pending[1] is not None else 0.0)
    except (sqlite3.Error, OSError, ValueError):
        pass
    effective = ignored = excluded = quarantined = live = 0
    independent = _run_kernel(run).initial_state(run['observation_started'])
    worst = 0.0
    for row in ([] if cached else rows):
        appraisal = _load(row['appraisal'])
        before, after = _load(row['before_state']), _load(row['after_state'])
        event = source_rows.get(row['source_rowid']) if source_rows is not None else None
        payload = {}
        if event is not None:
            try:
                payload = _load(event.get('payload') or '{}')
                if not isinstance(payload, dict):
                    payload = {}
            except (ValueError, TypeError):
                payload = {}
        is_excluded = _excluded(event, payload) if event is not None else appraisal.get('reason') == 'not_live_evidence'
        quarantine = appraisal['rule'] == 'quarantine' and not is_excluded
        supported = row['kind'] in ('message', 'group_message', 'private_message', 'tool_result', 'action_result')
        is_live = not is_excluded and not quarantine and supported
        drives = is_live and not appraisal['ignored'] and any(appraisal['delta'].values())
        ignored += not is_excluded and not quarantine and not drives
        effective += bool(drives)
        live += bool(is_live)
        excluded += bool(is_excluded)
        quarantined += bool(quarantine)
        kind = by_kind.setdefault(row['kind'], dict(received=0, live=0, effective=0, ignored=0, excluded=0))
        kind['received'] += 1
        kind['live'] += bool(is_live)
        kind['effective'] += bool(drives)
        kind['ignored'] += not is_excluded and not quarantine and not drives
        kind['excluded'] += bool(is_excluded or quarantine)
        by_rule[appraisal['rule']] = by_rule.get(appraisal['rule'], 0) + 1
        for key in _DIMENSIONS:
            d = dims[key]
            d['min'] = min(d['min'], after[key]) if d['min'] is not None else after[key]
            d['max'] = max(d['max'], after[key]) if d['max'] is not None else after[key]
            delta = appraisal['delta'][key] if drives else 0
            d['nonzero_events'] += delta != 0
            d['positive'] += delta > 0
            d['negative'] += delta < 0
            d['observed'] = d['nonzero_events'] > 0
        if row['kind'] == 'tool_result' and not is_excluded and not quarantine:
            name = str(payload.get('tool') or 'unknown')
            outcome = payload.get('outcome')
            if outcome not in ('success', 'failure', 'unknown'):
                outcome = 'success' if payload.get('ok') is True else 'failure' if payload.get('ok') is False else 'unknown'
            tool = tools.setdefault(name, dict(success=0, failure=0, unknown=0))
            tool[outcome] += 1
        if source_rows is not None:
            if event is None or event['id'] != row['event_id']:
                evidence['source_missing'] += 1
                continue
            fresh = _evaluation(dict(event), now)
            evidence['checked_events'] += 1
            if fresh != appraisal:
                evidence['appraisal_mismatches'] += 1
            target = independent['at'] if fresh['rule'] == 'quarantine' else max(
                independent['at'], float(event['ts']), float(event['created']))
            independent = core.advance(independent, target)
            for key in _DIMENSIONS:
                low, high = core.PARAMETERS['bounds'][key]
                independent[key] = min(high, max(low, independent[key] + fresh['delta'][key]))
                worst = max(worst, abs(independent[key] - after[key]))
            independent['processed'] += 1
    if source_rows is not None:
        evidence.update(max_error=worst, ok=bool(rows) and backlog['ledger_consistent']
                        and not evidence['source_missing'] and not evidence['appraisal_mismatches'] and worst <= 1e-10)
    heartbeat_age = max(0, now - run['last_tick'])
    open_gap = heartbeat_age if heartbeat_age > 60 else 0.0
    observed = float(run['observed_seconds'])
    gap = float(run['gap_seconds']) + open_gap
    total = observed + gap
    last_gap = c.execute('SELECT MAX(ended) FROM shadow_gap').fetchone()[0]
    streak_start = last_gap if last_gap is not None else run['observation_started']
    result = {
        'received_events': len(rows), 'live_events': live, 'effective_events': effective,
        'ignored_events': ignored, 'excluded_events': excluded, 'quarantined_events': quarantined,
        'by_kind': by_kind, 'by_rule': by_rule, 'tools': tools, 'dimensions': dims,
        'freshness': {'heartbeat_age_seconds': heartbeat_age, 'stale': heartbeat_age >= 90},
        'backlog': backlog, 'evidence_replay': evidence,
        'continuity': {'observed_seconds': observed, 'gap_seconds': gap,
                       'open_gap_seconds': open_gap, 'gap_ratio': gap / total if total else None,
                       'current_streak_seconds': max(0, run['last_tick'] - streak_start) if not open_gap else 0.0},
        'legacy_excluded_events': excluded, 'run_version': core.VERSION,
    }
    if cached:
        for key, value in _DIAGNOSTIC_CACHE['value'].items():
            result[key] = copy.deepcopy(value)
    else:
        stable = {k: copy.deepcopy(v) for k, v in result.items()
                  if k not in ('freshness', 'backlog', 'continuity')}
        _DIAGNOSTIC_CACHE.update(key=cache_key, at=now, value=stable)
    return result


def _calibration(c, run, now, rows, diagnostics):
    def number(name, default):
        try:
            value = float(os.getenv(name, default))
            return value if math.isfinite(value) and value >= 0 else float(default)
        except (TypeError, ValueError):
            return float(default)
    hours, events, ratio = (number('EMOTION_CALIB_MIN_HOURS', 24),
                             number('EMOTION_CALIB_MIN_EVENTS', 120),
                             number('EMOTION_CALIB_MAX_GAP_RATIO', 0.25))
    replay = _replay_cached(c, run, now, rows)
    continuity = diagnostics['continuity']
    backlog = diagnostics['backlog']
    evidence = diagnostics['evidence_replay']
    kinds = {k for k, v in diagnostics['by_kind'].items() if v['effective'] > 0}
    checks = []
    def add(key, label, need, current, passed, state=None):
        checks.append(dict(key=key, label=label, need=need, now=current,
                           ok=bool(passed), status=state or ('pass' if passed else 'fail')))
    add('window', '有效心跳累计时长', '≥ %.0f 小时（非连续在线保证）' % hours,
        '%.1f 小时' % (continuity['observed_seconds'] / 3600), continuity['observed_seconds'] >= hours * 3600)
    add('events', '有效驱动事件', '≥ %.0f 条（不含回执/隔离）' % events,
        '%d 条' % diagnostics['effective_events'], diagnostics['effective_events'] >= events)
    add('kinds', '有效来源覆盖', '聊天与工具结果均有非零驱动',
        '、'.join(sorted(kinds)) or '尚未观测',
        bool(kinds & {'message', 'group_message', 'private_message'}) and 'tool_result' in kinds)
    add('replay', '存储轨迹可复算', '非空轨迹，误差 ≤ 1e-10；不证明规则有效',
        '最大误差 %.1e / %d 条' % (replay['max_error'], replay['events']), replay['ok'],
        'not_observed' if not rows else None)
    gap_ratio = continuity['gap_ratio']
    add('continuity', '心跳断档占比', '≤ %.0f%%，含当前停机区间' % (ratio * 100),
        '%.1f%% / 当前断档 %.0f 秒' % ((gap_ratio or 0) * 100, continuity['open_gap_seconds']) if gap_ratio is not None else '未知',
        gap_ratio is not None and gap_ratio <= ratio and not diagnostics['freshness']['stale'],
        'unknown' if gap_ratio is None else None)
    add('backlog', '源账本实际积压', '源库可读、待消费 0 条、游标身份一致',
        '%d 条 / 最久 %.0f 秒' % (backlog['pending'], backlog['oldest_age_seconds']) if backlog['known'] else '源库不可读，未知',
        backlog['known'] and backlog['pending'] == 0 and backlog['ledger_consistent'],
        'unknown' if not backlog['known'] else None)
    missing = [k for k, v in diagnostics['dimensions'].items() if not v['implemented'] or not v['observed']]
    add('dimensions', '四维真实驱动覆盖', '每维均有真实非零驱动，未实现不得通过',
        '未覆盖：' + '、'.join(missing) if missing else '四维均有真实驱动', not missing,
        'not_observed' if missing else None)
    add('evidence', '源事件独立核对', '来源身份、重新评估与轨迹一致',
        '核对 %d 条 / 缺失 %d / 评估差异 %d' % (evidence['checked_events'], evidence['source_missing'], evidence['appraisal_mismatches']),
        evidence['ok'] is True, 'unknown' if evidence['ok'] is None else 'not_observed' if not rows else None)
    add('freshness', '消费者当前存活', '心跳小于 90 秒',
        '距上次 %.1f 秒' % diagnostics['freshness']['heartbeat_age_seconds'], not diagnostics['freshness']['stale'])
    tools = diagnostics['tools']
    successes = sum(v['success'] for v in tools.values())
    failures = sum(v['failure'] for v in tools.values())
    uncertain = sum(v['unknown'] for v in tools.values())
    confirmed_channels = sum(v['success'] + v['failure'] > 0 for v in tools.values())
    add('outcomes', '工具结果与通道覆盖', '至少两种工具且含真实成功和失败；未知单列不充数',
        '%d 种已确认 / 成功 %d / 失败 %d / 未知 %d' % (confirmed_channels, successes, failures, uncertain),
        confirmed_channels >= 2 and successes > 0 and failures > 0)
    done = sum(x['ok'] for x in checks)
    return dict(checks=checks, passed=done, total=len(checks), progress=round(done / len(checks), 3),
                thresholds=dict(min_hours=hours, min_events=events, max_gap_ratio=ratio), replay=replay)


def valence_samples(now=None, limit=50):
    """candidate_counterfactual 轨迹：只读全历史反事实，独立于 cognition 游标。

    Rebuilt from source snapshots on each request so late labels cannot be lost.
    No historical impulse is ever applied to shadow_run or shadow_event.
    """
    import valence_candidates as candidates
    now = time.time() if now is None else float(now)
    report = trajectories.stamp(dict(
        ok=True, mode='candidate_integrated_replay',
        expression_control=False, rule_version=core.SEMANTIC_VERSION,
        parameter_hash=core.parameter_hash(), rows=[],
        stats=dict(total=0, included=0, excluded=0), state=core.initial_state(now)),
        trajectories.CANDIDATE_COUNTERFACTUAL)
    labels = {}
    try:
        if candidates._path().is_file():
            with closing(_readonly(candidates._path())) as c:
                labels = {r['event_id']: dict(r) for r in c.execute('SELECT * FROM candidate')}
        if not _source_path().is_file():
            report.update(ok=False, error='no_cognition')
            return report
        with closing(_readonly(_source_path())) as c:
            events = [dict(r) for r in c.execute('SELECT * FROM event ORDER BY created,id')]
    except (sqlite3.Error, OSError, ValueError) as exc:
        print('[效价整合回放] 读取失败 ' + type(exc).__name__, flush=True)
        report.update(ok=False, error=type(exc).__name__)
        return report
    state = None
    rows = []
    seen = set()
    for event in events:
        eid = event['id']
        if eid in seen:
            continue
        seen.add(eid)
        label = labels.pop(eid, None)
        if event['kind'] not in ('message', 'private_message', 'group_message') and label is None:
            continue
        baseline = _source_evaluation(event, now)
        payload = event.get('payload')
        error = baseline['reason'] if baseline['rule'] == 'quarantine' else ''
        if not error and event['created'] > now:
            error = 'future_event'
        if label is not None and not error:
            source_text = str(payload.get('text') or '').strip()[:candidates.MAX_TEXT]
            text_matches = label['text'] == source_text
            # Historical bridge events retained only the first 200 characters.
            if len(source_text) == 200 and len(label['text']) > 200:
                text_matches = label['text'].startswith(source_text)
            if (not text_matches
                    or label['kind'] != event['kind'] or label['session'] != event['session']
                    or label['at'] != event['ts']
                    or label['address_basis'] != str(payload.get('address_basis') or '')):
                error = 'source_mismatch'
        evaluated = dict(event)
        evaluated['kind'] = 'message' if event['kind'] in ('private_message', 'group_message') else event['kind']
        assessment = core.integrated_appraisal(evaluated, label) if not error else dict(
            rule='quarantine', delta=dict.fromkeys(_DIMENSIONS, 0.0), exclusion_reason=error)
        included = not error and not assessment['exclusion_reason']
        if not error:
            effective = max(event['ts'], event['created'])
            if state is None:
                state = core.initial_state(effective)
            state = core.advance(state, effective)
            for key, delta in assessment['delta'].items():
                low, high = core.PARAMETERS['bounds'][key]
                state[key] = min(high, max(low, state[key] + delta))
            state['processed'] += 1
        rows.append(dict(event_id=eid, at=event['ts'], text=str(payload.get('text') or '')[:160]
                         if isinstance(payload, dict) else '', included=included,
                         exclusion_reason=error or assessment['exclusion_reason'],
                         rule_appraisal=baseline, appraisal=assessment,
                         candidate=label, after=copy.deepcopy(state)))
    for eid, label in labels.items():
        rows.append(dict(event_id=eid, at=label['at'], text=label['text'], included=False,
                         exclusion_reason='source_missing', candidate=label))
    def order(row):
        try:
            at = float(row['at'])
            if not math.isfinite(at):
                at = 0.0
        except (ValueError, TypeError, OverflowError):
            at = 0.0
        return at, row['event_id']
    rows.sort(key=order)
    report['stats'] = dict(total=len(rows), included=sum(r['included'] for r in rows),
                           excluded=sum(not r['included'] for r in rows))
    report['state'] = core.advance(state, now) if state else core.initial_state(now)
    report['rows'] = rows[-max(1, min(100, int(limit))):][::-1]
    report['recent'] = report['rows']
    return report


def status(now=None, limit=50, include_valence=True):
    """Return live shadow evidence. Candidate replay is opt-in: it scans all history."""
    now = time.time() if now is None else float(now)
    out = trajectories.stamp(dict(
        ok=True, mode='shadow', expression_control=False, calibration='uncalibrated',
        running=False, timeline=[], parameter_hash=core.parameter_hash(),
        stage3_ready=False, ready_for_review=False,
        valence_samples=(valence_samples(now=now, limit=limit) if include_valence else None)),
        trajectories.V2_LIVE)
    if not shadow_path().is_file():
        return out
    with closing(_readonly(shadow_path())) as c:
        c.execute('BEGIN')
        run = c.execute('SELECT * FROM shadow_run WHERE id=1').fetchone()
        if run is None:
            return out
        if run['parameter_hash'] != core.parameter_hash():
            raise ValueError('frozen parameters do not match the loaded kernel')
        rows = c.execute('SELECT * FROM shadow_event ORDER BY source_rowid').fetchall()
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        history = c.execute('SELECT report FROM shadow_retrospective WHERE id=1').fetchone() if 'shadow_retrospective' in tables else None
        # 旧存储报告可能早于口径统一，读取时按契约重盖，不做数据迁移。
        out['retrospective'] = trajectories.stamp(
            _load(history['report']) if history else dict(
                mode='retrospective', events=0, effective_events=0,
                excluded_events=0, error='历史回放等待后台生成'),
            trajectories.HISTORICAL_REPLAY)
        out.update({k: run[k] for k in ('observation_started', 'last_tick', 'cursor', 'processed',
                                      'observed_seconds', 'gap_seconds', 'parameter_hash')})
        diagnostics = _diagnostics(c, run, now, rows)
        out.update(diagnostics=diagnostics, parameters=_load(run['parameters']),
                   real_events=diagnostics['live_events'], running=not diagnostics['freshness']['stale'],
                   state=core.advance(_load(run['state']), now))
        out['calibration_detail'] = _calibration(c, run, now, rows, diagnostics)
        detail = out['calibration_detail']
        out['ready_for_review'] = detail['passed'] == detail['total']
        out['calibration'] = 'review_ready' if out['ready_for_review'] else 'collecting'
        for row in rows[-min(100, max(1, int(limit))):]:
            out['timeline'].append(dict(event_id=row['event_id'], at=row['at'], kind=row['kind'],
                                        **_load(row['appraisal']), before=_load(row['before_state']),
                                        after=_load(row['after_state'])))
    return out
