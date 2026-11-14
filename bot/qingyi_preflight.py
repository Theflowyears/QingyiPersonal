"""清漪自动前置检测。默认只读取现有证据，不启动候选回放或任何外发链路。"""
import argparse
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import time

CATALOG_VERSION = '2026-11-14.3'
REPORT_SCHEMA_VERSION = 1
ORGANIC_GATE_KEYS = ('action_evidence', 'action_identity', 'action_results',
                     'belief_grounding', 'self_view_grounding')
ENGINEERING_GATE_KEYS = ('consent_enforcement', 'action_idempotency_outbox',
                         'evidence_retraction', 'layer_versioning', 'model_switch_behavior')


def _now(value=None):
    value = time.time() if value is None else float(value)
    return value if value > 0 else time.time()


def _data_root(persona_root):
    root = Path(persona_root)
    return root.parent if root.name == 'persona' else root


def _check(check_id, status, message, counts=None, source_refs=(), limitations=()):
    return {"id": check_id, "status": status, "message": message,
            "counts": counts or {}, "source_refs": list(source_refs),
            "limitations": list(limitations)}


def _ro(path):
    resolved = Path(path).resolve()
    connection = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


@contextmanager
def _readonly(path):
    """SQLite的with只管理事务；这里必须显式关闭句柄，否则Windows会短暂锁库。"""
    connection = _ro(path)
    try:
        yield connection
    finally:
        connection.close()


def _table_names(connection):
    return {str(row['name']) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _read_tables_with_reread(path):
    """跨库扫描可能撞上别的写入事务：锁冲突有限重读一次，并记录这个边界。
    暂时锁不是库损坏；重读仍失败按 unknown 分型，绝不冒充 pass，也不判永久损坏。"""
    try:
        with _readonly(path) as connection:
            return _table_names(connection), False
    except sqlite3.OperationalError as exc:
        if 'locked' not in str(exc) and 'busy' not in str(exc):
            raise
        time.sleep(0.25)
        with _readonly(path) as connection:
            return _table_names(connection), True


def _count(connection, sql, args=()):
    try:
        return int(connection.execute(sql, args).fetchone()[0])
    except sqlite3.Error:
        return None


def _source_identity(root):
    identity = {'binding_digest': '', 'code_root': '', 'reason': ''}
    try:
        import engineering_contracts
        binding = engineering_contracts.binding()
        identity['code_root'] = str(Path(engineering_contracts.__file__).resolve().parent.parent)
        identity['binding_digest'] = hashlib.sha256(json.dumps(
            binding, sort_keys=True, allow_nan=False).encode()).hexdigest()
    except Exception as exc:
        identity['reason'] = type(exc).__name__
    return identity


def report_identity(persona_root=None):
    """报告身份 = 代码/解释器 binding + 数据根。任一变化，旧报告就是 stale（统一方案 §23.4），
    不沿用绿灯。overview 与 run_preflight 共用同一口径。"""
    root = Path(persona_root) if persona_root else Path(os.getenv('PERSONA_DATA_DIR') or
                                                        Path(os.getenv('DATA_DIR', './data')) / 'persona')
    return {'binding_digest': _source_identity(root)['binding_digest'],
            'data_identity_digest': hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]}


def _schema_check(root):
    expected = {
        'cognition': (root / 'cognition.db', {
            'person', 'event', 'episode', 'belief', 'belief_evidence', 'belief_revision',
            'commitment', 'goal', 'action', 'self_view_rev', 'evidence_retraction',
            'cognition_invalidation', 'layer_revision'}),
        'shadow': (root / ('emotion_shadow_v2.db' if (root / 'emotion_shadow_v2.db').is_file()
                           else 'emotion_shadow.db'),
                   {'shadow_run', 'shadow_event', 'shadow_gap'}),
    }
    details, blocked = {}, False
    for name, (path, required) in expected.items():
        if not path.is_file():
            details[name] = {'status': 'missing_data', 'missing': sorted(required)}
            blocked = True
            continue
        try:
            present, reread = _read_tables_with_reread(path)
            missing = sorted(required - present)
            details[name] = {'status': 'blocked' if missing else 'pass', 'missing': missing}
            if reread:
                details[name]['reread_after_lock'] = True
            blocked = blocked or bool(missing)
        except sqlite3.Error:
            details[name] = {'status': 'unknown', 'missing': []}
            blocked = True
    return _check('file_schema', 'blocked' if blocked else 'pass',
                  '账本结构缺少必要对象，或存在账本暂时不可读' if blocked else '已发现账本具备必要结构',
                  details, ('bot/qingyi_preflight.py',),
                  ('表存在不等于对应能力完成；只证明schema可读。',))


def _memory_provenance_check(root):
    data_root = _data_root(root)
    persona_path = root / 'persona.db'
    group_path = data_root / 'memory' / 'group_facts.json'
    private_path = data_root / 'memory' / 'private_memory.json'
    counts = {'persona_total': 0, 'persona_sourced': 0, 'group_total': 0,
              'group_sourced': 0, 'private_summaries': 0, 'private_items': 0}
    if persona_path.is_file():
        try:
            with _readonly(persona_path) as connection:
                columns = {str(row[1]) for row in connection.execute('PRAGMA table_info(memory)')}
                counts['persona_total'] = _count(connection, 'SELECT COUNT(*) FROM memory') or 0
                if 'source_event_ids' in columns:
                    counts['persona_sourced'] = _count(
                        connection, "SELECT COUNT(*) FROM memory WHERE source_event_ids NOT IN ('','[]')") or 0
        except sqlite3.Error:
            counts['persona_status'] = 'unknown'
    try:
        if group_path.is_file():
            payload = json.loads(group_path.read_text(encoding='utf-8'))
            for group in (payload.get('groups') or {}).values():
                for entry in group.get('entries') or []:
                    counts['group_total'] += 1
                    if entry.get('source_event_ids'):
                        counts['group_sourced'] += 1
    except Exception:
        counts['group_status'] = 'unknown'
    try:
        if private_path.is_file():
            counts['private_summaries'] = len(json.loads(private_path.read_text(encoding='utf-8')))
    except Exception:
        counts['private_status'] = 'unknown'
    total = counts['persona_total'] + counts['group_total']
    sourced = counts['persona_sourced'] + counts['group_sourced']
    if counts.get('persona_status') == 'unknown' or counts.get('group_status') == 'unknown':
        return _check('memory_provenance', 'unknown',
                      '记忆账本存在但本次不可读，未给任何绿灯', counts,
                      limitations=('读不到不等于没有；不可读时不得当成0或通过。',))
    if not total and not counts['private_summaries']:
        return _check('memory_provenance', 'missing_data', '未发现可检查的记忆账本', counts)
    if sourced == total and counts['private_items'] == counts['private_summaries']:
        return _check('memory_provenance', 'pass', '当前可检查记忆均有命题级来源', counts)
    return _check('memory_provenance', 'not_observed',
                  '现有记忆尚未全部绑定逐命题来源；legacy背景不能被自动补造为亲历', counts,
                  limitations=('当前字段缺失是未实现证据，不是计数为零。',))


def _candidate_check(root):
    path = root / 'valence_candidates.db'
    if not path.is_file():
        return _check('candidate_evidence', 'missing_data', '效价候选账本尚未创建', {})
    try:
        with _readonly(path) as connection:
            if 'candidate' not in _table_names(connection):
                return _check('candidate_evidence', 'blocked', '候选账本缺少candidate表', {})
            rows = connection.execute('SELECT state,COUNT(*) n FROM candidate GROUP BY state').fetchall()
            counts = {str(row['state']): int(row['n']) for row in rows}
    except sqlite3.Error:
        return _check('candidate_evidence', 'unknown', '候选账本不可读', {})
    return _check('candidate_evidence', 'pass' if counts else 'not_observed',
                  '候选账本只读统计完成；标签不等于金标准', counts,
                  limitations=('本检测不标注、不调用模型、不启动候选回放。',))


def _phase25_check(root, at):
    try:
        import phase_audit
        audit = phase_audit.audit_readiness(str(root), now=at,
                                            include_candidate_replay=False)
    except Exception as exc:
        return _check('phase25', 'unknown', '阶段2.5判定不可读，未给任何绿灯', {'error_type': type(exc).__name__})
    value = audit.get('phase25') or {}
    checks = value.get('checks') or []
    statuses = {str(row.get('key')): str(row.get('status')) for row in checks}
    blockers = list(value.get('blockers') or [])
    observed = sum(1 for key in statuses if statuses[key] == 'pass')
    pending = sum(1 for key in statuses if statuses[key] == 'not_observed')
    pending_keys = {key for key, value in statuses.items() if value == 'not_observed'}
    if not blockers:
        status = 'pass'
    elif set(blockers) <= pending_keys:
        status = 'not_observed'
    else:
        status = 'blocked'
    return _check('phase25', status, '十二项准入逐条读取完成', {
        'observed': observed, 'total': len(statuses), 'pending': pending,
        'organic_pass': sum(statuses.get(k) == 'pass' for k in ORGANIC_GATE_KEYS),
        'engineering_pass': sum(statuses.get(k) == 'pass' for k in ENGINEERING_GATE_KEYS),
    }, source_refs=('bot/phase_audit.py::audit_readiness',),
        limitations=('pass只允许提交人工评审，不授权阶段3。',))


def _shadow_check(root, at):
    try:
        import phase_audit
        audit = phase_audit.audit(str(root), now=at)
        phase2 = ((audit.get('phases') or {}).get('phase2') or {}).get('status', 'unknown')
        counts = ((audit.get('counts') or {}).get('shadow') or {})
    except Exception as exc:
        return _check('shadow_run', 'unknown', '影子账本不可读', {'error_type': type(exc).__name__})
    status = {'pass': 'pass', 'partial': 'blocked', 'fail': 'blocked',
              'not_started': 'missing_data'}.get(phase2, 'unknown')
    return _check('shadow_run', status, '既有实时影子轨迹检查完成；未运行候选反事实', counts,
                  source_refs=('bot/phase_audit.py::_shadow',),
                  limitations=('可重放不等于心理校准或可接管表达。',))


def _projection(checks_by_id, profile, at):
    def pass_id(check_id):
        return 'pass' if checks_by_id.get(check_id, {}).get('status') == 'pass' else 'blocked'

    tasks = {}
    import qingyi_roadmap
    implementation = {task['id']: 'implemented' if task['implementation'] else 'planned'
                      for task in qingyi_roadmap.catalog()}
    for task_id, engineering in (
            ('D01', 'catalog'), ('D02', 'file_schema'), ('D03', 'phase25'),
            ('D04', 'usage_idempotency'), ('D05', 'file_schema'),
            ('D06', 'file_schema'), ('A02', 'usage_idempotency')):
        tasks[task_id] = {'implementation': implementation[task_id],
                          'engineering': pass_id(engineering),
                          'organic': 'not_required', 'authorization': 'not_required'}
    tasks['U01'] = dict(tasks['D01'])
    stage0_check = checks_by_id.get('stage0_contracts', {})
    stage0_cases = (stage0_check.get('counts') or {}).get('cases') or {}

    def stage0_engineering(*keys):
        if stage0_check.get('status') == 'not_verified':
            return 'not_verified'
        if all((stage0_cases.get(key) or {}).get('status') == 'pass' for key in keys):
            return 'pass'
        return 'blocked'

    for task_id, keys in (('A01', ('audit_baseline',)),
                          ('A03', ('memory_manifest',)),
                          ('A04', ('memory_manifest',)),
                          ('A05', ('stable_identity', 'time_scope')),
                          ('A06', ('write_inventory',)),
                          ('A07', ('isolated_fixture',)),
                          ('A08', ('retraction_observation',))):
        tasks[task_id] = {'implementation': implementation[task_id],
                          'engineering': stage0_engineering(*keys),
                          'organic': 'not_required', 'authorization': 'not_required'}
    # D07 的故障矩阵与 M01 的准入契约都是可执行契约：工程状态绑定隔离探针里的
    # 固定用例，evidence profile 不跑探针，保持 not_verified，不冒充已验证。
    tasks['D07'] = {'implementation': implementation['D07'],
                    'engineering': stage0_engineering('detector_fault_matrix'),
                    'organic': 'not_required', 'authorization': 'not_required'}
    tasks['M01'] = {'implementation': implementation['M01'],
                    'engineering': stage0_engineering('cognition_admission'),
                    'organic': 'not_required', 'authorization': 'not_required'}
    # M02/R01 同样是可执行契约：工程状态绑定隔离探针里的固定用例。
    # R01 的验收前置含 M05；M05 已建设，release 仍由工程/有机门把关。
    tasks['M02'] = {'implementation': implementation['M02'],
                    'engineering': stage0_engineering('claim_provenance'),
                    'organic': 'not_required', 'authorization': 'not_required'}
    tasks['R01'] = {'implementation': implementation['R01'],
                    'engineering': stage0_engineering('retrieval_objects'),
                    'organic': 'not_required', 'authorization': 'not_required'}
    # M05 派生失效 DAG：工程状态绑定隔离探针固定用例；M07/R04/S03 已按同一
    # 旁路范式建成可执行契约，但接进真实消费者（摘要/检索/表达）仍是
    # M08/C04 等的范围，不由此冒充已接线。
    tasks['M05'] = {'implementation': implementation['M05'],
                    'engineering': stage0_engineering('invalidation_dag'),
                    'organic': 'not_required', 'authorization': 'not_required'}
    # M03/M06/R02/R03/S02/S04/M07/R04/S03/V03/V02/M08/R05/S06/S07/L01/R06/C04/L02/V04/C05/G02/G03/V05/L03
    # 同样是可执行契约：工程状态绑定隔离探针固定用例。它们的 release 前置（如 R02 含 M06、
    # V02 需人工消融）仍由目录 acceptance/organic 门把关。C04 只建快照与三层注入契约，
    # C05 只做 ExpressionPlan 隔离装配（expression_control 恒 False），G02 只产出注意
    # 与诚实部分种子，G03 只做只读检索/整理/草稿的统一调度契约（对外发送是 G04），
    # V05 只是发布前预验收证据（真实灰度由 C06 另行授权）——
    # 接进真实表达/行动链路仍是后续任务的范围，不由此冒充已接线。
    for task_id, case in (('M03', 'entity_normalization'), ('M06', 'cognition_writer'),
                          ('R02', 'association_edges'), ('R03', 'recall_scoring'),
                          ('S02', 'emotion_rule_freeze'), ('S04', 'emotion_separation'),
                          ('M07', 'private_turn_episodes'), ('R04', 'ripple_paths'),
                          ('S03', 'emotion_revision'), ('V03', 'retraction_scope_matrix'),
                          ('M08', 'memory_consolidation'), ('R05', 'ripple_conflicts'),
                          ('S06', 'emotion_replay_matrix'), ('S07', 'candidate_replay'),
                          ('S05', 'valence_annotation'), ('S01', 'emotion_trajectories'),
                          ('M04', 'claim_conflicts'), ('G01', 'action_proposal'),
                          ('V01', 'core_contract_regression'), ('U03', 'qingyi_panel'),
                          ('L01', 'self_reflection'), ('R06', 'ripple_evidence'),
                          ('C04', 'work_state'), ('L02', 'self_revision'),
                          ('V04', 'mode_observation'), ('C05', 'expression_plan'),
                          ('G02', 'goal_attention'),
                          ('G03', 'goal_draft_scheduler'), ('V05', 'release_fault_matrix'),
                          ('L03', 'habit_relationship'),
                          ('Q01', 'conversation_state'), ('Q02', 'memory_runtime'),
                          ('Q03', 'expression_priority'), ('Q04', 'worldbook_store'),
                          ('Q05', 'contract_sandbox'), ('Q06', 'capability_audit'),
                          ('Q07', 'capability_audit')):
        tasks[task_id] = {'implementation': implementation[task_id],
                          'engineering': stage0_engineering(case),
                          'organic': 'not_required', 'authorization': 'not_required'}
    # V02 是人工标注/评审的验证夹具：契约可执行≠真实消融已做，organic 留待真实样本。
    tasks['V02'] = {'implementation': implementation['V02'],
                    'engineering': stage0_engineering('memory_retention'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    # R07/S08/R08 同 V02 口径：消融/隔离实验契约可执行≠真实实验已做，
    # 结论必须由人工评审给出，检测器不替人下「有用/没用」的判断。
    tasks['R07'] = {'implementation': implementation['R07'],
                    'engineering': stage0_engineering('ripple_ablation'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    tasks['S08'] = {'implementation': implementation['S08'],
                    'engineering': stage0_engineering('recall_emotion'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    tasks['R08'] = {'implementation': implementation['R08'],
                    'engineering': stage0_engineering('ripple_extension'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    # G04 对外灰度与结果确认契约已建成（outward_release）：可执行≠已灰度。
    # 真实对外灰度要主人逐 kind 签字 + 真实发送链证据，organic/authorization
    # 留待人工，AI 不能自签（统一方案 §G 表 G04 行 / §11.1）。
    tasks['G04'] = {'implementation': implementation['G04'],
                    'engineering': stage0_engineering('outward_release'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    # V06 长期消融与回退复查契约已建成（longterm_review）：可执行≠长期消融已做。
    # 真实结论需要真实长期样本，organic 留待观测；judgement 恒 None 留给人判。
    tasks['V06'] = {'implementation': implementation['V06'],
                    'engineering': stage0_engineering('longterm_review'),
                    'organic': 'not_observed', 'authorization': 'not_required'}
    # L04 跨时间连续性验收契约已建成（continuity_acceptance）：可执行≠连续性已证明。
    # 真实结论需要真实跨时段样本，organic 留待观测；judgement 恒 None 留给人判，
    # AI 不能自签（统一方案 §L 表 L04 行 / §25 原阶段5行）。
    tasks['L04'] = {'implementation': implementation['L04'],
                    'engineering': stage0_engineering('continuity_acceptance'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    phase25 = checks_by_id.get('phase25', {}).get('counts') or {}
    tasks['C01'] = {'implementation': 'existing',
                    'engineering': 'not_required',
                    'organic': ('pass' if phase25.get('organic_pass') == len(ORGANIC_GATE_KEYS)
                                else 'not_observed' if phase25 else 'unknown'),
                    'authorization': 'manual_review'}
    tasks['C02'] = {'implementation': 'existing',
                    'engineering': ('pass' if phase25.get('engineering_pass') == len(ENGINEERING_GATE_KEYS)
                                    else 'blocked' if phase25 else 'unknown'),
                    'organic': 'not_required', 'authorization': 'manual_review'}
    # C03 评审包聚合器已建成（stage3a_review）：可执行≠已评审。五路证据汇入
    # 人工评审包，全pass不自动授权；organic/authorization 留待主人看过签字，
    # AI 不能自签（统一方案 §C 表 C03 行 / §23）。
    tasks['C03'] = {'implementation': 'existing',
                    'engineering': stage0_engineering('stage3a_review'),
                    'organic': 'not_observed', 'authorization': 'manual_review'}
    if profile != 'isolated':
        tasks['A02']['engineering'] = 'not_verified'
    return {'generated_at': at, 'tasks': tasks}


def usage_idempotency_probe():
    bot_dir = str(Path(__file__).resolve().parent)
    code = '''
import json, os, sys, tempfile
sys.path.insert(0, %r)
with tempfile.TemporaryDirectory(prefix="qingyi_usage_probe_") as tmp:
    os.environ.update(DATA_DIR=tmp, QQ_DRY_RUN="1", MEMORY_USAGE_LOG="1")
    import memory_usage as m
    m.note("group_facts", ["g:probe"], injected_chars=1)
    used = daily = None
    for _ in range(3):
        m.aggregate(days=1)
        used = m.usage_of("g:probe")["used"]
        daily = m.today()["selected"]
    print(json.dumps({"used": used, "daily": daily}, allow_nan=False))
''' % bot_dir
    try:
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                                timeout=30, text=True, encoding='utf-8', errors='replace')
        payload = json.loads((result.stdout or '').strip().splitlines()[-1])
        ok = result.returncode == 0 and payload.get('used') == 1 and payload.get('daily') == 1
        return {'ok': bool(ok), 'used_after_three_aggregations': payload.get('used'),
                'daily_selected_after_three_aggregations': payload.get('daily'),
                'stderr': (result.stderr or '')[-200:]}
    except Exception as exc:
        return {'ok': False, 'reason': type(exc).__name__,
                'used_after_three_aggregations': None,
                'daily_selected_after_three_aggregations': None}


def stage0_contracts_probe(*, online=None):
    """阶段0证据契约的固定白名单执行；只在隔离 profile 里跑。"""
    try:
        import stage0_contracts
        if online is None:
            online = (Path(__file__).resolve().parents[1] / 'data').resolve() == Path(
                os.getenv('DATA_DIR', './data')).resolve()
        return stage0_contracts.run(timeout=90, online=online)
    except Exception as exc:
        return {'ok': False, 'cases': {}, 'reason': type(exc).__name__,
                'isolated': True, 'seconds': 0}


def aggregate_scope(statuses):
    if any(s in ('error','unknown') for s in statuses):
        return 'unknown'
    if any(s in ('blocked','not_implemented') for s in statuses):
        return 'blocked'
    if any(s in ('missing_data','not_observed') for s in statuses):
        return 'not_observed'
    if all(s in ('pass','not_applicable','not_verified','not_required') for s in statuses):
        return 'pass'
    return 'blocked'


def run_preflight(root=None, profile='evidence', now=None):
    if profile not in ('evidence', 'isolated'):
        raise ValueError('unsupported_profile')
    at = _now(now)
    persona_root = Path(root) if root else Path(os.getenv('PERSONA_DATA_DIR') or
                                                Path(os.getenv('DATA_DIR', './data')) / 'persona')
    checks = [_check_catalog(), _schema_check(persona_root),
              _memory_provenance_check(persona_root), _shadow_check(persona_root, at),
              _candidate_check(persona_root), _phase25_check(persona_root, at)]
    if profile == 'isolated':
        probe = usage_idempotency_probe()
        checks.append(_check('usage_idempotency', 'pass' if probe['ok'] else 'blocked',
                             '计数重复聚合隔离探针完成', probe))
        contracts = stage0_contracts_probe(online=root is None)
        checks.append(_check(
            'stage0_contracts', ('pass' if contracts.get('ok') else
                                 'not_verified' if contracts.get('status') == 'not_verified' else 'blocked'),
            '阶段0证据契约隔离验证完成' if contracts.get('ok')
            else '阶段0证据契约存在失败项，未给绿灯',
            contracts, source_refs=('bot/stage0_contracts.py',),
            limitations=('只证明契约可执行；生产账本是否合规仍看只读审计。',)))
    else:
        checks.append(_check('usage_idempotency', 'not_verified',
                             '默认证据检测不执行代码探针；请明确选择隔离检测', {}))
        checks.append(_check('stage0_contracts', 'not_verified',
                             '默认证据检测不执行契约测试；请选择隔离检测', {}))
    import capability_audit
    capability=capability_audit.status()
    active=capability['capabilities']['active_memory']
    checks.extend([
        _check('memory_composition', 'pass' if active['wired'] else 'not_implemented',
               '最终请求清单绑定真实入选材料与请求哈希', capability),
        _check('ripple', 'pass' if active['wired'] else 'not_implemented',
               '统一只读检索、排序、受限关联与证据渲染已接入运行编排', capability),
        _check('expression', 'not_observed', '局部灰度按人工签字和前置生效；完整统一表达接管仍需后续授权'),
        _check('permissions', 'pass', '检测入口需管理员会话和CSRF；本函数本身不授予权限'),
    ])
    statuses = [row['status'] for row in checks]
    scope_status = aggregate_scope(statuses)
    checks_by_id = {row['id']: row for row in checks}
    projection = _projection(checks_by_id, profile, at)
    roadmap = _roadmap_summary(projection)
    identity = report_identity(persona_root)
    return {
        'schema_version': REPORT_SCHEMA_VERSION,
        'catalog_version': CATALOG_VERSION,
        'run_id': '',
        'profile': profile,
        'requested_scope': 'qingyi_all',
        'source_kind': 'local_snapshot',
        'checked_at': at,
        'binding_digest': identity['binding_digest'],
        'data_identity_digest': identity['data_identity_digest'],
        'scope_status': scope_status,
        'checks': checks,
        **roadmap,
        'authorization': {'stage3': False, 'expression_control': False,
                          'deployment': False, 'automatic_progress': False},
    }


def _check_catalog():
    import qingyi_roadmap
    result = qingyi_roadmap.validate_catalog()
    return _check('catalog', 'pass' if result['ok'] else 'blocked',
                  '阶段目录与依赖完整' if result['ok'] else '阶段目录存在缺失或循环依赖', result)


def _roadmap_summary(projection):
    import qingyi_roadmap
    return qingyi_roadmap.summary(projection=projection)


def exit_code(report):
    statuses = {row['status'] for row in report['checks']}
    if 'error' in statuses or 'unknown' in statuses:
        return 3
    if report['scope_status'] == 'blocked':
        return 1
    if report['scope_status'] == 'not_observed':
        return 3
    if report['scope_status'] == 'pass':
        return 4 if any(task.get('release_ready') is False and task.get('authorization') == 'manual_review'
                        for task in report['tasks']) else 0
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='清漪阶段前置检测')
    parser.add_argument('--profile', choices=('evidence', 'isolated'), default='evidence')
    parser.add_argument('--root', default='')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = run_preflight(args.root or None, profile=args.profile)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return exit_code(report)


if __name__ == '__main__':
    raise SystemExit(main())
