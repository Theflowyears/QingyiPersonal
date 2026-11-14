"""阶段0 证据契约的固定隔离执行器。

只跑一组白名单测试，每个 case 在独立的临时 DATA_DIR 里启动子进程；
不读生产账本、不写任何账本、不发起网络请求。它证明的是契约可执行，
不是「生产数据已经合规」——后者由 phase_audit 只读快照负责。
白名单也承载后续阶段的可执行契约（D07 检测器故障矩阵、M01 认知准入），
新增 case 必须同样是纯隔离测试，不许夹带生产读写。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'audit_baseline': ['test_audit_baseline.AuditBaselineTests'],
    'memory_manifest': ['test_memory_manifest'],
    'stable_identity': ['test_stage0_evidence_contracts.StableIdentityTests'],
    'time_scope': ['test_stage0_evidence_contracts.TimeScopeTests'],
    'write_inventory': ['test_stage0_evidence_contracts.WriteInventoryTests'],
    'retraction_observation': ['test_stage0_evidence_contracts.RetractionObservationTests'],
    'isolated_fixture': ['test_stage0_evidence_contracts.IsolationFixtureTests'],
    'detector_fault_matrix': ['test_qingyi_fault_matrix'],
    'cognition_admission': ['test_cognition_admission'],
    'claim_provenance': ['test_claim_provenance'],
    'retrieval_objects': ['test_retrieval_objects'],
    'entity_normalization': ['test_entity_normalization'],
    'cognition_writer': ['test_cognition_writer'],
    'association_edges': ['test_association_edges'],
    'recall_scoring': ['test_recall_scoring'],
    'emotion_rule_freeze': ['test_emotion_rule_freeze'],
    'emotion_separation': ['test_emotion_separation'],
    'memory_retention': ['test_memory_retention'],
    'claim_conflicts': ['test_claim_conflicts'],
    'invalidation_dag': ['test_invalidation_dag'],
    'private_turn_episodes': ['test_private_turn_episodes'],
    'ripple_paths': ['test_ripple_paths'],
    'emotion_revision': ['test_emotion_revision'],
    'retraction_scope_matrix': ['test_retraction_scope_matrix'],
    'memory_consolidation': ['test_memory_consolidation'],
    'ripple_conflicts': ['test_ripple_conflicts'],
    'emotion_replay_matrix': ['test_emotion_replay_matrix'],
    'candidate_replay': ['test_candidate_replay'],
    'self_reflection': ['test_self_reflection'],
    'ripple_evidence': ['test_ripple_evidence'],
    'work_state': ['test_work_state'],
    'self_revision': ['test_self_revision'],
    'habit_relationship': ['test_habit_relationship'],
    'ripple_ablation': ['test_ripple_ablation'],
    'recall_emotion': ['test_recall_emotion'],
    'mode_observation': ['test_mode_observation'],
    'action_proposal': ['test_action_proposal'],
    'valence_annotation': ['test_valence_annotation'],
    'core_contract_regression': ['test_core_contract_regression'],
    'emotion_trajectories': ['test_emotion_trajectories'],
    'qingyi_panel': ['test_qingyi_panel'],
    'expression_plan': ['test_expression_plan'],
    'goal_attention': ['test_goal_attention'],
    'goal_draft_scheduler': ['test_goal_draft_scheduler'],
    'outward_release': ['test_outward_release'],
    'longterm_review': ['test_longterm_review'],
    'continuity_acceptance': ['test_continuity_acceptance'],
    'ripple_extension': ['test_ripple_extension'],
    'release_fault_matrix': ['test_release_fault_matrix'],
    'stage3a_review': ['test_stage3a_review'],
    'conversation_state': ['test_conversation_state'],
    'memory_runtime': ['test_memory_runtime'],
    'worldbook_store': ['test_worldbook_store'],
    'contract_sandbox': ['test_contract_sandbox'],
    'capability_audit': ['test_capability_audit'],
    'expression_priority': ['test_expression_priority'],
    'dialogue_semantics': ['test_dialogue_semantics'],
    'memory_governance': ['test_memory_governance'],
}


def _environment(tmp):
    return dict(os.environ, DATA_DIR=tmp, PERSONA_DATA_DIR='', QQ_DRY_RUN='1',
                PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                PYTHONPATH=os.pathsep.join([str(ROOT / 'tests'),
                                            str(ROOT / 'bot'), str(ROOT)]))


def run(timeout=120, *, online=False):
    started = time.time()
    results = {}
    if online:
        import contract_sandbox
        boundary = contract_sandbox.verify_boundary()
        if boundary.get('status') != 'pass':
            return {'ok':False,'cases':{},'seconds':round(time.time()-started,3),
                    'isolated':True,'mode':'online','status':'not_verified',
                    'reason':'isolation_unavailable','boundary':boundary}
        for key, tests in CASES.items():
            begin=time.time()
            result=contract_sandbox.run(tests,online=True,timeout=timeout)
            result["tests"]=tests;result["seconds"]=round(time.time()-begin,3)
            result.pop("output",None)
            results[key]=result
        return {"ok":all(case["status"]=="pass" for case in results.values()),
                "cases":results,"seconds":round(time.time()-started,3),
                "isolated":True,"mode":"online"}
    for key, tests in CASES.items():
        begin = time.time()
        with tempfile.TemporaryDirectory(prefix='stage0-contract-') as tmp:
            try:
                process = subprocess.run(
                    [sys.executable, '-B', '-m', 'unittest', *tests],
                    cwd=str(ROOT / 'tests'), env=_environment(tmp),
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=timeout)
                output = (process.stdout or '') + (process.stderr or '')
                status = 'pass' if process.returncode == 0 else 'fail'
            except subprocess.TimeoutExpired:
                output = 'timeout'
                status = 'fail'
            except Exception as exc:
                output = type(exc).__name__
                status = 'fail'
        results[key] = {'status': status, 'tests': tests,
                        'output_sha256': hashlib.sha256(output.encode('utf-8')).hexdigest(),
                        'seconds': round(time.time() - begin, 3)}
    return {'ok': all(case['status'] == 'pass' for case in results.values()),
            'cases': results, 'seconds': round(time.time() - started, 3),
            'isolated': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='阶段0证据契约隔离验证')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--online', action='store_true')
    args = parser.parse_args(argv)
    report = run(online=args.online)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        for key, case in report['cases'].items():
            print('%-24s %s %.2fs' % (key, case['status'], case['seconds']))
        print('OK' if report['ok'] else 'FAILED')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())