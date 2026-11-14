# -*- coding: utf-8 -*-
"""M05 派生来源失效传播（统一方案 §20.5 派生失效DAG / §7.3 撤回不改写历史）。

X06 的病灶是「撤回了源，旧投影还活着」：self_view 被抑制了，但摘要、检索
缓存、身份缓存仍按旧口径复活同一段话。本模块把「谁依赖谁」显式建成 DAG
（边方向统一 derived_object→source_object），管理修正提交时沿反向依赖队列
传播失效，原子推进 validity_epoch，让最终读门按 epoch 即时过滤。

四条修正类型语义严格分开（§20.5 第3条）：
  observation_invalid    源观察无效：移出当前有效支持集合，原始观察仍可审计；
  interpretation_revised 只使指定解释失效，真实原事件保留，绝不连坐源；
  belief_corrected       旧信念版本被新修订取代（superseded），历史不删；
  scope_revoked          只收紧授权范围，不凭空改变事实真假。

传播判定（§20.5 第4条）：失去支持后仍有其他有效支持的对象进入
needs_reassessment，**绝不无脑恢复 active**；没有任何有效支持的对象不能
继续作当前已核验事实。同根源转述不算独立支持（与 M04 同一口径），只登记
不升级。失效是单调的：本模块任何路径都不会把 invalidated/superseded 洗回
active，重新评估只能由后续显式修订事务（M08/L02）完成。

纯函数核心：check_edge / build_graph / correction / propagate / read_gate /
cache_key / plan_emotion_revision 不读写账本、不发网络、不调模型。唯一例外
是显式只读桥 read_invalidation_state() / read_derivation_inputs()：沿用
claim_conflicts.read_all_candidates 的先例——sqlite mode=ro + query_only，
缺库缺文件返回空/降级，绝不建库建表建目录（X15）；路径调用时解析。
落账走 self_model.write_invalidation_transaction（唯一写入者），这里不碰。

缓存 key 必须同时含 object revision、source revision、validity epoch、scope、
算法版本（§20.5 末段）：只有日历日期不足以控制失效，「写了失效表、旧 prompt
缓存仍活着」是假修复。cache_key 对缺分量直接拒绝，不给默认值。

情感修正（§20.5 第6条）只产出**新 revision 计划**（plan_emotion_revision），
保留旧历史反应，不直接写轨迹；把它接进情感读取与版本化重算是 S03 的范围。
所有读函数（read_gate/两个只读桥）都是纯读，GET 不得启动任何写流程。

旁路建内核（§3.3）：本模块不改线任何旧消费者。A08 的 retract_evidence /
cognition_invalidation 旧路径保持原样；把摘要、检索、ExpressionPlan 接过来
是 M07/M08/S03/C04/R04 的范围。摘要库（private_memory.json）当前没有来源
字段，只读桥如实登记为 unknown 支持，绝不补造来源。
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

CORRECTION_KINDS = ('observation_invalid', 'interpretation_revised',
                    'belief_corrected', 'scope_revoked')
# active 只在读门里表示「未受任何修正影响」；propagate 绝不产出 active。
OBJECT_STATUSES = ('active', 'invalidated', 'needs_reassessment',
                   'scope_narrowed', 'superseded')
# 原始观察不能依赖其派生解释（§20.5）：event 只能当源，不能当派生端。
RAW_KINDS = ('event',)
OBJECT_KINDS = ('event', 'belief', 'episode', 'commitment', 'self_view_rev',
                'goal', 'interpretation', 'persona_fact', 'group_fact',
                'world_knowledge', 'summary', 'vision_note', 'identity_cache',
                'retrieval_cache', 'emotion_state', 'url_source')
ALGORITHM_VERSION = 'm05-1'
_truth_lost = ('invalidated', 'superseded')


def _sha(*parts):
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:14]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value):
    return str(value or '').strip()


# ── DAG：边方向 derived→source，建边前查环 ─────────────────────────


def check_edge(edges, derived_id, source_id, derived_kind='', source_kind=''):
    """建边前的纯检查。返回 {ok, reason}，reason 是稳定码。

    环检查用带 visited 的 DFS：从 source 沿既有 forward 边走，若能到达
    derived 就会成环（§20.5：小规模不必第一天引入图数据库）。
    重复边幂等放行（edge_exists），不堆垃圾。
    """
    derived, source = _text(derived_id), _text(source_id)
    if not derived or not source:
        return {'ok': False, 'reason': 'missing_object_id'}
    if derived == source:
        return {'ok': False, 'reason': 'self_dependency'}
    if _text(derived_kind) in RAW_KINDS:
        return {'ok': False, 'reason': 'raw_observation_cannot_derive'}
    forward = {}
    for edge in (edges or []):
        if not isinstance(edge, dict):
            continue
        a, b = _text(edge.get('derived_id')), _text(edge.get('source_id'))
        if not a or not b:
            continue
        if a == derived and b == source:
            return {'ok': True, 'reason': 'edge_exists'}
        forward.setdefault(a, set()).add(b)
    seen, stack = set(), [source]
    while stack:
        node = stack.pop()
        if node == derived:
            return {'ok': False, 'reason': 'cycle_rejected'}
        if node in seen:
            continue
        seen.add(node)
        stack.extend(forward.get(node, ()))
    return {'ok': True, 'reason': ''}


def build_graph(edges):
    """边列表 → 邻接表。去重、确定性排序；不修改输入。"""
    forward, reverse, kinds = {}, {}, {}
    for edge in (edges or []):
        if not isinstance(edge, dict):
            continue
        a, b = _text(edge.get('derived_id')), _text(edge.get('source_id'))
        if not a or not b or a == b:
            continue
        forward.setdefault(a, set()).add(b)
        reverse.setdefault(b, set()).add(a)
        if _text(edge.get('derived_kind')):
            kinds[a] = _text(edge['derived_kind'])
        if _text(edge.get('source_kind')):
            kinds[b] = _text(edge['source_kind'])
    return {'forward': {k: sorted(v) for k, v in forward.items()},
            'reverse': {k: sorted(v) for k, v in reverse.items()},
            'kinds': kinds}


def _roots(graph, object_id):
    """对象的根源集合：沿 forward 边能到达的无源对象（原始观察/外部来源）。

    同根源转述不算独立支持（§20.4/§7.3 同一口径）。带 visited 防旧数据成环。
    """
    seen, stack, roots = set(), [object_id], set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        sources = graph['forward'].get(node) or []
        if not sources:
            roots.add(node)
        else:
            stack.extend(sources)
    return roots


# ── 修正记录：不可变，只追加 ───────────────────────────────────────


def correction(kind, target_id, reason, ts=None, scope='', new_revision_id=''):
    """构造一条不可变修正记录。稳定 ID 由内容决定：同内容重复提交幂等。

    reason 必填（A08：撤回必须带理由）；scope_revoked 必须给收紧的范围；
    belief_corrected 必须给新修订对象 ID（旧版本保留，历史不删）。
    """
    if kind not in CORRECTION_KINDS:
        raise ValueError('unknown correction kind: %s' % kind)
    target, why = _text(target_id), _text(reason)
    if not target:
        raise ValueError('correction requires target_id')
    if not why:
        raise ValueError('correction requires reason')
    scope = _text(scope)
    new_revision_id = _text(new_revision_id)
    if kind == 'scope_revoked' and not scope:
        raise ValueError('scope_revoked requires scope')
    if kind == 'belief_corrected' and not new_revision_id:
        raise ValueError('belief_corrected requires new_revision_id')
    at = time.time() if ts is None else float(ts)
    return {
        'correction_id': 'cor_' + _sha(kind, target, why, scope, new_revision_id),
        'kind': kind, 'target_id': target, 'reason': why,
        'scope': scope, 'new_revision_id': new_revision_id, 'ts': at,
    }


def _closure(graph, targets):
    """反向依赖闭包：BFS + visited，同对象只进一次队列（§20.5 第2条）。"""
    seen, queue, order = set(), sorted(set(targets)), []
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        order.append(node)
        for dependent in graph['reverse'].get(node, []):
            if dependent not in seen:
                queue.append(dependent)
    return order


def propagate(edges, corrections, epoch=0, states=None, now=None):
    """纯函数：算出一次管理修正提交的完整失效计划，不写任何东西。

    edges:      derived→source 边列表（含本次新注册的边）。
    corrections: correction() 产出的记录列表；空列表不推进 epoch。
    epoch:      当前 validity_epoch（调用方从只读桥拿）。
    states:     既有对象状态 {object_id: {status,...}}；已失效的源继续按
                无效支持计，已失效的对象不会被洗回 active（单调）。
    返回 {expected_epoch, epoch, corrections, affected[], pending_recompute[],
          snapshot}。affected 每项 {object_id, status, reasons[],
          remaining_supports[], independent_support, revoked_scopes[]}。
    """
    at = time.time() if now is None else float(now)
    graph = build_graph(edges)
    prior = states if isinstance(states, dict) else {}
    records = sorted((c for c in (corrections or []) if isinstance(c, dict)),
                     key=lambda c: _text(c.get('correction_id')))
    for rec in records:
        if rec.get('kind') not in CORRECTION_KINDS:
            raise ValueError('unknown correction kind: %s' % rec.get('kind'))
        if not _text(rec.get('target_id')) or not _text(rec.get('reason')):
            raise ValueError('correction requires target_id and reason')
        if rec['kind'] == 'scope_revoked' and not _text(rec.get('scope')):
            raise ValueError('scope_revoked requires scope')
        if rec['kind'] == 'belief_corrected' and not _text(rec.get('new_revision_id')):
            raise ValueError('belief_corrected requires new_revision_id')

    base = int(epoch)
    if not records:
        return {'expected_epoch': base, 'epoch': base, 'corrections': [],
                'affected': [], 'pending_recompute': [],
                'snapshot': {'snapshot_id': '', 'epoch': base, 'correction_ids': [],
                             'affected': [], 'pending_recompute': [], 'ts': at}}

    # 既有失效先入账：上一轮撤掉的源这一轮仍是无效支持。
    invalid = {oid for oid, st in prior.items()
               if isinstance(st, dict) and st.get('status') in _truth_lost}
    superseded, direct, revoked = set(), {}, {}
    truth_targets, scope_targets = [], []
    for rec in records:
        target, kind = rec['target_id'], rec['kind']
        if kind == 'observation_invalid':
            invalid.add(target)
            direct[target] = ('invalidated', ['observation_invalid',
                                              'removed_from_valid_support'])
            truth_targets.append(target)
        elif kind == 'interpretation_revised':
            # 只失效这条解释；它的源（真实原事件）不在 invalid 里，不连坐。
            invalid.add(target)
            direct[target] = ('invalidated', ['interpretation_revised',
                                              'original_event_preserved'])
            truth_targets.append(target)
        elif kind == 'belief_corrected':
            invalid.add(target)
            superseded.add(target)
            direct[target] = ('superseded', ['belief_corrected', 'history_preserved',
                                             'new_revision_registered'])
            truth_targets.append(target)
        else:
            revoked.setdefault(target, set()).add(_text(rec['scope']))
            scope_targets.append(target)

    truth_order = _closure(graph, truth_targets)
    scope_order = _closure(graph, scope_targets)
    # 范围收紧沿派生链传播：派生范围只能相同或更窄（X14），所以整条闭包
    # 都记下被撤的 scope；事实真假不动。
    scope_by_target = {}
    for rec in records:
        if rec['kind'] == 'scope_revoked':
            scope_by_target.setdefault(rec['target_id'], set()).add(_text(rec['scope']))
    for target, scopes in scope_by_target.items():
        for oid in _closure(graph, [target]):
            revoked.setdefault(oid, set()).update(scopes)

    truth_set = set(truth_order)
    affected_set = truth_set | set(scope_order)
    # 拓扑序评估：源先于派生。被传递失效的对象需要立即进 invalid 集合，
    # 否则它的下游会把「已失效的中间派生」当成仍然有效的支持。
    indeg = {oid: len([s for s in graph['forward'].get(oid, []) if s in affected_set])
             for oid in affected_set}
    ready = sorted(oid for oid in affected_set if indeg[oid] == 0)
    topo = []
    while ready:
        oid = ready.pop(0)
        topo.append(oid)
        for dependent in graph['reverse'].get(oid, []):
            if dependent in indeg:
                indeg[dependent] -= 1
                if indeg[dependent] == 0:
                    ready.append(dependent)
                    ready.sort()
    # 旧数据若带环（建边锁之前的遗留），剩余对象按字典序兜底评估，不静默丢。
    topo.extend(sorted(affected_set - set(topo)))

    entries = {}
    for oid in topo:
        supports = graph['forward'].get(oid, [])
        remaining = [s for s in supports if s not in invalid]
        lost = [s for s in supports if s in invalid]
        if oid in direct:
            status, reasons = direct[oid]
            independent = False
        elif oid in truth_set:
            if not remaining:
                if lost and all(s in superseded for s in lost):
                    # 支持只是被新修订取代，不是被撤回：等新版本重评，
                    # 不直接判死，但也绝不允许继续冒充已核验事实。
                    status, reasons = 'needs_reassessment', ['support_superseded_by_revision']
                else:
                    status, reasons = 'invalidated', ['no_valid_support']
                independent = False
            else:
                lost_roots = set()
                for s in lost:
                    lost_roots |= _roots(graph, s)
                independent = any(not (_roots(graph, s) & lost_roots) for s in remaining)
                status = 'needs_reassessment'
                reasons = (['independent_support_remains'] if independent
                           else ['same_root_support_remains'])
        else:
            # 只被范围修正波及：真假不动，只收紧授权。
            status, reasons, independent = 'scope_narrowed', ['scope_revoked_no_truth_change'], False
        reasons = list(reasons)
        scopes = sorted(revoked.get(oid, ()))
        if scopes and status != 'scope_narrowed':
            reasons.append('scope_also_revoked')
        prior_status = (prior.get(oid) or {}).get('status') if isinstance(prior.get(oid), dict) else None
        if prior_status in _truth_lost and status not in _truth_lost:
            status = prior_status
            reasons.append('prior_invalidation_preserved')
        if status in _truth_lost:
            invalid.add(oid)
        if status == 'superseded':
            superseded.add(oid)
        entries[oid] = {'object_id': oid, 'status': status, 'reasons': reasons,
                        'remaining_supports': remaining,
                        'independent_support': bool(independent),
                        'revoked_scopes': scopes}

    affected = [entries[oid] for oid in sorted(entries)]
    pending = sorted(entries)
    correction_ids = [r['correction_id'] for r in records]
    snapshot = {
        'snapshot_id': 'inv_' + _sha(base + 1, _canonical(correction_ids),
                                     _canonical([(e['object_id'], e['status']) for e in affected])),
        'epoch': base + 1, 'correction_ids': correction_ids,
        'affected': [{'object_id': e['object_id'], 'status': e['status']} for e in affected],
        'pending_recompute': pending, 'ts': at,
    }
    return {'expected_epoch': base, 'epoch': base + 1, 'corrections': records,
            'affected': affected, 'pending_recompute': pending, 'snapshot': snapshot}


# ── 最终读门：按 epoch 即时过滤，旧投影不得复活已撤回的源 ──────────


def read_gate(items, states, current_epoch, scope=''):
    """纯读过滤。返回 (留下的条目, 被挡原因计数)。

    每条 item 必须声明 projection_epoch（它是在哪个 epoch 下构建的投影）：
    缺声明或落后于当前 epoch 一律挡下——「写了失效表、旧投影仍活着」正是
    M05 要消灭的假修复（§20.5 第5条）。needs_reassessment 不是当前已核验
    事实，同样不得作为当前支持输出；历史审计走只读桥，不走这道门。
    """
    kept, dropped = [], {}
    current = int(current_epoch)
    wanted = _text(scope)
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        oid = _text(item.get('object_id'))
        state = (states or {}).get(oid) or {}
        status = state.get('status') or 'active'
        reason = None
        if status in _truth_lost:
            reason = status
        elif status == 'needs_reassessment':
            reason = 'needs_reassessment'
        elif wanted and wanted in (state.get('revoked_scopes') or []):
            reason = 'scope_revoked'
        else:
            built = item.get('projection_epoch')
            if built is None:
                reason = 'missing_projection_epoch'
            else:
                try:
                    if int(built) < current:
                        reason = 'stale_projection'
                except (TypeError, ValueError):
                    reason = 'invalid_projection_epoch'
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(item)
    return kept, dropped


def cache_key(object_revision, source_revision, validity_epoch, scope,
              algorithm_version=ALGORITHM_VERSION):
    """五分量缓存 key（§20.5 末段）。缺分量直接拒绝，绝不给默认值。

    只有日历日期不足以控制失效：同一天里 epoch 推进、源修订、范围收紧都
    必须让 key 变化，否则旧 prompt/检索缓存会复活已撤回的依据。
    """
    parts = {'object_revision': _text(object_revision),
             'source_revision': _text(source_revision),
             'validity_epoch': validity_epoch,
             'scope': _text(scope),
             'algorithm_version': _text(algorithm_version)}
    problems = []
    for name in ('object_revision', 'source_revision', 'scope', 'algorithm_version'):
        if not parts[name]:
            problems.append('missing_' + name)
    epoch = parts['validity_epoch']
    if isinstance(epoch, bool) or not isinstance(epoch, (int, str)) or _text(epoch) == '':
        problems.append('missing_validity_epoch')
    else:
        try:
            parts['validity_epoch'] = int(epoch)
            if parts['validity_epoch'] < 0:
                problems.append('invalid_validity_epoch')
        except (TypeError, ValueError):
            problems.append('invalid_validity_epoch')
    if problems:
        return {'ok': False, 'key': '', 'problems': problems}
    return {'ok': True, 'problems': [],
            'key': hashlib.sha256(_canonical(parts).encode('utf-8')).hexdigest()}


def plan_emotion_revision(parent_revision_id, correction_id, new_state, ts=None):
    """情感修正只产出新 revision/轨迹分支计划（§20.5 第6条），不写轨迹。

    旧历史反应原样保留：新 revision 挂 parent_revision_id 形成分支，
    绝不覆盖旧 run。接进情感读取与版本化重算是 S03；GET/读路径不得调用。
    """
    parent, trigger = _text(parent_revision_id), _text(correction_id)
    if not parent or not trigger:
        raise ValueError('emotion revision requires parent_revision_id and correction_id')
    at = time.time() if ts is None else float(ts)
    return {
        'new_revision_id': 'emo_' + _sha(parent, trigger, _canonical(new_state)),
        'parent_revision_id': parent, 'correction_id': trigger,
        'state': new_state, 'branch': 'revision', 'history': 'preserved', 'ts': at,
    }


# ── 只读桥：缺库缺文件返回空/降级，绝不建库建表建目录（X15）────────


def _data_root():
    return os.getenv('DATA_DIR', './data')


def _persona_root():
    return os.getenv('PERSONA_DATA_DIR') or os.path.join(_data_root(), 'persona')


def _cognition_path():
    return os.path.join(_persona_root(), 'cognition.db')


def _ro(path):
    if not os.path.isfile(path):
        return None
    try:
        connection = sqlite3.connect(
            Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection
    except sqlite3.Error:
        return None


def _tables(connection):
    return {str(r[0]) for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _json_list(raw):
    try:
        got = json.loads(raw or '[]')
        return got if isinstance(got, list) else []
    except Exception:
        return []


def _read_json(path):
    if not os.path.isfile(path):
        return None, 'absent'
    try:
        return json.loads(Path(path).read_text(encoding='utf-8')), None
    except Exception:
        return None, 'unreadable'


def read_invalidation_state():
    """只读当前失效账本：epoch、边、修正记录、对象状态、快照。"""
    out = {'epoch': 0, 'edges': [], 'corrections': [], 'states': {}, 'snapshots': []}
    connection = _ro(_cognition_path())
    if connection is None:
        return out
    try:
        tables = _tables(connection)
        if 'meta' in tables:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='validity_epoch'").fetchone()
            if row:
                try:
                    out['epoch'] = int(row['value'])
                except (TypeError, ValueError):
                    out['epoch'] = 0
        if 'derivation_edge' in tables:
            out['edges'] = [dict(r) for r in connection.execute(
                'SELECT derived_id,source_id,derived_kind,source_kind,basis,ts '
                'FROM derivation_edge ORDER BY derived_id, source_id')]
        if 'correction_record' in tables:
            out['corrections'] = [dict(r) for r in connection.execute(
                'SELECT id,kind,target_id,reason,scope,new_revision_id,ts '
                'FROM correction_record ORDER BY id')]
        if 'object_validity_state' in tables:
            for row in connection.execute(
                    'SELECT object_id,status,reasons,remaining_supports,revoked_scopes,epoch '
                    'FROM object_validity_state ORDER BY object_id'):
                out['states'][row['object_id']] = {
                    'status': row['status'], 'reasons': _json_list(row['reasons']),
                    'remaining_supports': _json_list(row['remaining_supports']),
                    'revoked_scopes': _json_list(row['revoked_scopes']),
                    'epoch': row['epoch']}
        if 'invalidation_snapshot' in tables:
            out['snapshots'] = [
                {'epoch': r['epoch'], 'snapshot_id': r['snapshot_id'],
                 'correction_ids': _json_list(r['correction_ids']),
                 'payload': json.loads(r['payload'] or '{}'), 'ts': r['ts']}
                for r in connection.execute(
                    'SELECT epoch,snapshot_id,correction_ids,payload,ts '
                    'FROM invalidation_snapshot ORDER BY epoch, snapshot_id')]
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return out


def _edge(derived_id, source_id, derived_kind, source_kind, basis):
    return {'derived_id': derived_id, 'source_id': source_id,
            'derived_kind': derived_kind, 'source_kind': source_kind,
            'basis': basis}


def read_derivation_inputs():
    """只读收集各库现成的派生→来源关系，供建图与传播。

    覆盖 cognition（belief_evidence/episode/commitment/self_view_rev/goal）、
    persona 档案、群事实、世界知识；私聊摘要当前无来源字段，如实登记
    unknown 支持，绝不补造。身份/检索缓存的依赖边由构建方显式注册
    （derivation_edge 表），这里原样读出。缺库缺文件报 degradation，
    不声称「她没有记忆」。
    """
    edges, objects, degradation = [], [], []
    seen_objects = {}

    def note(object_id, kind, store, support):
        if object_id not in seen_objects:
            seen_objects[object_id] = {'object_id': object_id, 'kind': kind,
                                       'store': store, 'support': support}

    connection = _ro(_cognition_path())
    if connection is None:
        degradation.append({'store': 'cognition', 'reason': 'absent'})
    else:
        try:
            tables = _tables(connection)
            if 'belief_evidence' in tables:
                for row in connection.execute(
                        "SELECT belief_id,event_id FROM belief_evidence "
                        "WHERE relation='support' ORDER BY belief_id,event_id"):
                    edges.append(_edge(row['belief_id'], row['event_id'],
                                       'belief', 'event', 'belief_evidence'))
                    note(row['belief_id'], 'belief', 'cognition', 'known')
                    note(row['event_id'], 'event', 'cognition', 'raw')
            for table, kind, field in (('episode', 'episode', 'event_ids'),
                                       ('commitment', 'commitment', 'event_ids'),
                                       ('self_view_rev', 'self_view_rev', 'event_ids')):
                if table not in tables:
                    continue
                for row in connection.execute(
                        'SELECT id,%s FROM %s ORDER BY id' % (field, table)):
                    refs = [str(x) for x in _json_list(row[field]) if str(x or '').strip()]
                    note(row['id'], kind, 'cognition', 'known' if refs else 'unknown')
                    for eid in refs:
                        edges.append(_edge(row['id'], eid, kind, 'event', table + '.' + field))
                        note(eid, 'event', 'cognition', 'raw')
            if 'goal' in tables:
                for row in connection.execute(
                        "SELECT id,parent_event FROM goal WHERE parent_event!='' ORDER BY id"):
                    edges.append(_edge(row['id'], row['parent_event'],
                                       'goal', 'event', 'goal.parent_event'))
                    note(row['id'], 'goal', 'cognition', 'known')
                    note(row['parent_event'], 'event', 'cognition', 'raw')
            if 'derivation_edge' in tables:
                for row in connection.execute(
                        'SELECT derived_id,source_id,derived_kind,source_kind,basis '
                        'FROM derivation_edge ORDER BY derived_id,source_id'):
                    edges.append(dict(row))
                    note(row['derived_id'], row['derived_kind'] or 'interpretation',
                         'cognition', 'known')
                    note(row['source_id'], row['source_kind'] or 'event', 'cognition', 'raw')
        except sqlite3.Error:
            degradation.append({'store': 'cognition', 'reason': 'unreadable'})
        finally:
            connection.close()

    persona = _ro(os.path.join(_persona_root(), 'persona.db'))
    if persona is None:
        degradation.append({'store': 'persona', 'reason': 'absent'})
    else:
        try:
            if 'memory' in _tables(persona):
                for row in persona.execute(
                        'SELECT id,kind,source_event_ids FROM memory ORDER BY id'):
                    refs = [str(x) for x in _json_list(row['source_event_ids'])
                            if str(x or '').strip()]
                    oid = 'persona:%s:%s' % (row['kind'], row['id'])
                    note(oid, 'persona_fact', 'persona', 'known' if refs else 'unknown')
                    for eid in refs:
                        edges.append(_edge(oid, eid, 'persona_fact', 'event',
                                           'persona.source_event_ids'))
                        note(eid, 'event', 'cognition', 'raw')
        except sqlite3.Error:
            degradation.append({'store': 'persona', 'reason': 'unreadable'})
        finally:
            persona.close()

    data, problem = _read_json(os.path.join(_data_root(), 'memory', 'group_facts.json'))
    if problem:
        degradation.append({'store': 'group', 'reason': problem})
    else:
        for gid, group in ((data or {}).get('groups') or {}).items():
            for entry in (group or {}).get('entries') or []:
                if not isinstance(entry, dict):
                    continue
                oid = 'group:%s:%s' % (gid, entry.get('id') or _sha(entry.get('text')))
                refs = [str(x) for x in (entry.get('source_event_ids') or [])
                        if str(x or '').strip()]
                note(oid, 'group_fact', 'group', 'known' if refs else 'unknown')
                for eid in refs:
                    edges.append(_edge(oid, eid, 'group_fact', 'event',
                                       'group.source_event_ids'))
                    note(eid, 'event', 'cognition', 'raw')

    data, problem = _read_json(os.path.join(_data_root(), 'memory', 'world_knowledge.json'))
    if problem:
        degradation.append({'store': 'knowledge', 'reason': problem})
    else:
        for record in (data or {}).get('topics') or []:
            if not isinstance(record, dict) or not _text(record.get('key')):
                continue
            oid = 'knowledge:%s' % record['key']
            url = _text(record.get('evidence_url'))
            note(oid, 'world_knowledge', 'knowledge', 'known' if url else 'unknown')
            if url:
                edges.append(_edge(oid, url, 'world_knowledge', 'url_source',
                                   'knowledge.evidence_url'))
                note(url, 'url_source', 'knowledge', 'raw')

    data, problem = _read_json(os.path.join(_data_root(), 'memory', 'private_memory.json'))
    if problem:
        degradation.append({'store': 'summary', 'reason': problem})
    else:
        for session in sorted((data or {})):
            # 摘要库没有来源字段：只登记对象，unknown 支持，绝不补造来源。
            note('summary:%s' % session, 'summary', 'summary', 'unknown')

    objects = [seen_objects[oid] for oid in sorted(seen_objects)]
    return {'edges': edges, 'objects': objects, 'degradation': degradation}