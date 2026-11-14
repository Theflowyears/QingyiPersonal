"""v1_reference 轨迹：用冻结的 shadow-v1 内核做隔离 synthetic 参考回放。

不连接 QQ、不写人格库、不计在线观察；产物只供对照，不能当实时证据。
三轨迹口径见 emotion_trajectories。
"""
import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import emotion_core as core
import emotion_trajectories as trajectories


def _number(value):
    try:
        n = float(value)
        return n if 0 < n < 1e12 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _payload(row):
    p = row.get('payload') or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def normalize(bundle):
    events = []

    def add(origin, ref, kind, ts, payload, quality='recorded', actor=''):
        encoded = json.dumps([origin, ref, payload], ensure_ascii=False,
                             sort_keys=True, allow_nan=False)
        eid = 'syn_' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]
        at = _number(ts)
        quality = quality if at is not None else 'inferred_order'
        events.append({'id': eid, 'kind': 'message' if kind in ('group_message', 'private_message') else kind,
                       'ts': at or float(len(events) + 1),
                       'source': 'synthetic', 'synthetic': True,
                       'time_quality': quality, 'origin': origin,
                       'actor_qq': actor, 'payload': {**payload, 'synthetic': True}})

    for i, row in enumerate(bundle.get('events') or []):
        add('cognition', row.get('id') or i, row.get('kind') or 'unknown',
            row.get('ts'), _payload(row), actor=str(row.get('actor_qq') or ''))
    for i, row in enumerate(bundle.get('history_log') or []):
        add('history_log', [row.get('key'), i], 'private_message' if
            row.get('role') == 'user' else 'outgoing_text', row.get('ts'),
            {'text': str(row.get('content') or ''),
             'addressed_to_self': str(row.get('key') or '').startswith('private-user-')})
    for key, session in (bundle.get('histories') or {}).items():
        if not isinstance(session, dict):
            continue
        for i, turn in enumerate(session.get('turns') or []):
            if not isinstance(turn, dict):
                continue
            # 快照 updated 是文件写入时间，不能拿来冒充每一轮发生的时刻。
            add('histories', [key, i], 'private_message' if
                turn.get('role') == 'user' else 'outgoing_text', turn.get('ts'),
                {'text': str(turn.get('content') or ''),
                 'addressed_to_self': str(key).startswith('private-user-')})
    groups = (bundle.get('group_facts') or {}).get('groups') or {}
    for gid, group in groups.items():
        entries = group.get('entries') or []
        if isinstance(entries, dict):
            entries = list(entries.values())
        for i, row in enumerate(entries):
            if not isinstance(row, dict):
                continue
            add('group_facts', [gid, row.get('id') or i], 'legacy_note',
                row.get('ts') or row.get('updated'), {'text': str(row.get('text') or ''),
                                                     'retrospective_summary': True})
    for i, row in enumerate(bundle.get('activity') or []):
        kind = str(row.get('kind') or '')
        out = str(row.get('out') or '')
        p = {'text': str(row.get('what') or ''), 'outcome': out,
             'recorded_by': 'activity_log'}
        event_kind = 'activity_note'
        if kind in ('search', 'watch'):
            event_kind = 'tool_result'
            if any(x in out for x in ('出错', '失败', '没结果', '对不上')):
                p['ok'] = False
            elif any(x in out for x in ('记住', '看懂', '看过', '看完')):
                p['ok'] = True
        elif kind == 'share':
            event_kind = 'action_result'
            p['submitted'] = True
        add('activity', [i, row.get('ref')], event_kind, row.get('ts'), p)
    unique = {e['id']: e for e in events}
    return list(unique.values())


def _distance(a, b):
    return max(abs(float(a[k]) - float(b[k])) for k in
               ('valence', 'arousal', 'control', 'attention'))


def reference_report(bundle):
    events = normalize(bundle)
    valid = [e for e in events if _number(e.get('ts'))]
    times = [e['ts'] for e in valid]
    start = min(times) if times else 1.0
    end = max(times) if times else start
    # 分源回放，避免把同一句的日志与快照当作独立证据反复强化。
    scenarios = {}
    deterministic = invariant = True
    maxima = {k: 0.0 for k in ('valence', 'arousal', 'control', 'attention')}
    groups = sorted({(e['origin'], e['time_quality']) for e in valid})
    for origin, quality in groups:
        subset = [e for e in valid if e['origin'] == origin and e['time_quality'] == quality]
        begin, finish = min(e['ts'] for e in subset), max(e['ts'] for e in subset)
        one = core.replay(subset, start_at=begin, end_at=finish)
        two = core.replay(copy.deepcopy(subset), start_at=begin, end_at=finish)
        ticked = core.replay(subset, start_at=begin, end_at=finish, tick_seconds=60)
        deterministic = deterministic and one == two
        diff = _distance(one['state'], ticked['state'])
        invariant = invariant and diff <= 1e-10
        name = origin if sum(o == origin for o, _ in groups) == 1 else origin + ':' + quality
        scenarios[name] = {'events': len(subset), 'final_state': one['state'],
                             'tick_max_error': diff,
                             'inferred_time': sum(e['time_quality'] != 'recorded' for e in subset)}
        for row in one['timeline']:
            for k in maxima:
                maxima[k] = max(maxima[k], abs(float(row['after'][k])))
    stress_events = [{'id': 'stress_' + str(i), 'kind': 'tool_result', 'ts': 1 + i,
                      'synthetic': True, 'source': 'synthetic',
                      'payload': {'ok': False}} for i in range(3000)]
    stress = core.replay(stress_events, start_at=1, end_at=3000)['state']
    quiet = core.advance(stress, 3000 + 86400 * 7)
    baseline = core.initial_state(quiet['at'])
    bounded = (-1 <= stress['valence'] <= 1 and
               all(0 <= stress[k] <= 1 for k in ('arousal', 'control', 'attention')))
    return trajectories.stamp({
            'mode': 'synthetic_reference',
            'parameters_tuned': False, 'calibration': 'uncalibrated',
            'parameter_hash': core.parameter_hash(), 'parameters': core.PARAMETERS,
            'source_counts': dict(Counter(e['origin'] for e in events)),
            'time_quality_counts': dict(Counter(e['time_quality'] for e in events)),
            'events': len(events), 'scenarios': scenarios, 'absolute_peaks': maxima,
            'checks': {'deterministic_replay': deterministic, 'tick_invariance': invariant,
                       'bounded_stress': bounded, 'quiet_recovery': _distance(quiet, baseline) < 1e-8},
            'limitations': ['所有回放均为synthetic，不改变她的真实状态或档案。',
                            '缺失轮次时间仅使用合成顺序，不能解释成真实情绪持续时间。',
                            '群事实是回顾摘要，不作为被重新亲历的消息。',
                            '不同来源可能重叠，分源观察，不把重复内容当独立证据。',
                            '通过回放只排除明显异常，不能替代真实流量观测或证明主观体验。'],
            'stage3_ready': False}, trajectories.V1_REFERENCE)


def save_report(report, path):
    # S04 回放边界：本模块没有任何数据库连接，唯一落盘写就是这里——
    # 只接受新 .json 路径（open 'x' 独占创建，绝不覆盖已有文件），
    # 拒绝生产数据文件名，实时/人格/候选账本一个字节都不碰。
    dest = Path(path).resolve()
    if dest.suffix.lower() != '.json':
        raise ValueError('reference reports must use a new JSON path')
    if dest.name.lower() in ('config.json', 'self.json', 'histories.json',
                              'world_knowledge.json', 'group_facts.json'):
        raise ValueError('refusing a production data filename')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('x', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    ap = argparse.ArgumentParser(description='v1参考轨迹：隔离synthetic回放，不连接QQ、不写人格库、不计在线')
    ap.add_argument('--bundle', required=True)
    ap.add_argument('--report', required=True)
    args = ap.parse_args()
    with open(args.bundle, encoding='utf-8') as f:
        bundle = json.load(f)
    report = reference_report(bundle)
    dest = Path(args.report)
    save_report(report, dest)
    print(json.dumps({'events': report['events'], 'checks': report['checks'],
                      'mode': report['mode'], 'report': str(dest)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
