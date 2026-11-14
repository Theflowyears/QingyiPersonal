"""Isolated executable engineering evidence, never organic/live evidence."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'consent_enforcement': [
        'test_runtime_outbox.RuntimeOutboxTests.test_deny_blocks_effect',
        'test_review_enforcement.ReviewEnforcement.test_media_denial_blocks_real_entrypoints',
        'test_review_enforcement.ReviewEnforcement.test_other_media_entrypoints_enforce_denial_and_unknown',
        'test_review_enforcement.ReviewEnforcement.test_denied_friend_dm_releases_real_reservation'],
    'action_idempotency_outbox': [
        'test_runtime_outbox.RuntimeOutboxTests.test_unknown_never_resends',
        'test_runtime_outbox.RuntimeOutboxTests.test_multipart_action_not_accepted_after_first_chunk',
        'test_review_enforcement.ReviewEnforcement.test_media_unknown_receipts_do_not_claim_delivery_or_resend',
        'test_review_enforcement.ReviewEnforcement.test_unknown_share_never_requeues_even_after_restart',
        'test_review_enforcement.ReviewEnforcement.test_crash_after_send_boundary_is_not_a_generation_retry',
        'test_review_enforcement.ReviewEnforcement.test_definite_share_failure_retries_frozen_intent_and_evidence',
        'test_review_enforcement.ReviewEnforcement.test_generation_retry_preserves_provenance',
        'test_review_enforcement.ReviewEnforcement.test_bili_share_carries_exact_tool_event_and_private_recipient',
        'test_review_enforcement.ReviewEnforcement.test_autonomous_public_video_stays_shareable_to_bound_master',
        'test_curiosity_seed_evidence.SeedEvidenceTests.test_persona_ingestion_records_exact_internal_motivation_now',
        'test_curiosity_seed_evidence.SeedEvidenceTests.test_retracted_seed_observation_cannot_be_revived_by_reingestion'],
    'evidence_retraction': [
        'test_cognition_controls.CognitionControls.test_retraction_preserves_event_and_invalidates_dependents',
        'test_review_enforcement.ReviewEnforcement.test_retraction_suppresses_all_consumers_and_persisted_identity_cache',
        'test_review_enforcement.ReviewEnforcement.test_audit_does_not_count_withdrawn_or_invalidated_grounding'],
    'layer_versioning': ['test_contract_continuity.Continuity.test_persona_save_noop'],
    'model_switch_behavior': ['test_contract_continuity.Continuity.test_model_switch_preserves_cognition'],
}


def binding():
    files = sorted(p for folder in ('bot', 'website', 'tests')
                   for p in (ROOT / folder).rglob('*')
                   if p.is_file() and p.suffix in ('.py', '.html', '.js', '.css')
                   and '__pycache__' not in p.parts)
    files.extend(p for p in (ROOT / 'launcher.py', ROOT / 'qq_openclaw_bridge.py',
                              ROOT / 'requirements.txt') if p.is_file())
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in files}
    return {'files': hashes, 'python': sys.version, 'platform': platform.platform(),
            'executable_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}


def read_evidence(root):
    try:
        data = json.loads((Path(root) / 'engineering_contracts.json').read_text(encoding='utf-8'))
        unsigned = dict(data)
        digest = unsigned.pop('sha256')
        if digest != hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest():
            raise ValueError('artifact hash mismatch')
        if data['binding'] != binding() or not 0 < data['ts'] <= time.time():
            raise ValueError('stale code/runtime evidence')
        if data['origin'] != 'isolated_mock_transport' or set(data['cases']) != set(CASES):
            raise ValueError('invalid contract coverage')
        if any(not isinstance(v, dict) or v.get('status') != 'pass'
               or v.get('tests') != CASES[key]
               or not _executed_successfully(v.get('execution'), len(CASES[key]))
               or not isinstance(v.get('output_sha256'), str)
               or len(v['output_sha256']) != 64
               or any(c not in '0123456789abcdef' for c in v['output_sha256'])
               for key, v in data['cases'].items()):
            raise ValueError('failed or invalid contracts')
        return data['cases'], ''
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, str(exc)


def _executed_successfully(execution, expected):
    return (isinstance(execution, dict)
            and type(execution.get('run')) is int and execution['run'] == expected
            and all(type(execution.get(k)) is int and execution[k] == 0
                    for k in ('skipped', 'failures', 'errors', 'expected_failures', 'unexpected_successes')))


def _execute_tests(tests):
    import unittest
    sys.path.insert(0, str(ROOT / 'tests'))
    suite = unittest.defaultTestLoader.loadTestsFromNames(tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    execution = {'run': result.testsRun, 'skipped': len(result.skipped),
                 'failures': len(result.failures), 'errors': len(result.errors),
                 'expected_failures': len(result.expectedFailures),
                 'unexpected_successes': len(result.unexpectedSuccesses)}
    print('CONTRACT_EXECUTION=' + json.dumps(execution, sort_keys=True), flush=True)
    return 0 if result.wasSuccessful() and _executed_successfully(execution, len(tests)) else 1


def run():
    destination = Path(os.getenv('PERSONA_DATA_DIR') or Path(os.getenv('DATA_DIR', './data')) / 'persona')
    artifact = destination / 'engineering_contracts.json'
    # Invalidate before binding/spawn: timeouts, spawn errors and interruption must
    # never leave yesterday's pass available while this attempt is incomplete.
    artifact.unlink(missing_ok=True)
    before = binding()
    results = {}
    for key, tests in CASES.items():
        execution = None
        passed = False
        output = b''
        try:
            with tempfile.TemporaryDirectory(prefix='cognition-contract-') as tmp:
                env = dict(os.environ, DATA_DIR=tmp, PERSONA_DATA_DIR='', QQ_DRY_RUN='1',
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--execute', *tests],
                                      cwd=ROOT / 'tests', env=dict(env, PYTHONPATH=str(ROOT)),
                                      capture_output=True, timeout=180)
                output = proc.stdout + proc.stderr
                records = [line.split('=', 1)[1] for line in proc.stdout.decode('utf-8').splitlines()
                           if line.startswith('CONTRACT_EXECUTION=')]
                if len(records) == 1:
                    execution = json.loads(records[0])
                passed = proc.returncode == 0 and _executed_successfully(execution, len(tests))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            output = ('contract execution failed: ' + repr(exc)).encode('utf-8')
        results[key] = {'status': 'pass' if passed else 'fail', 'execution': execution,
                        'tests': tests, 'output_sha256': hashlib.sha256(output).hexdigest()}
        print('%s: %s %s' % (key, results[key]['status'], json.dumps(execution, sort_keys=True)), flush=True)
        if not passed:
            sys.stderr.write(output.decode('utf-8', errors='replace'))
    if any(v['status'] != 'pass' for v in results.values()) or binding() != before:
        # A previous success must not survive a failed verification run.
        artifact.unlink(missing_ok=True)
        return 1
    data = {'origin': 'isolated_mock_transport', 'ts': time.time(),
            'binding': before, 'cases': results}
    data['sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    temp = artifact.with_suffix('.tmp')
    temp.write_text(json.dumps(data, sort_keys=True), encoding='utf-8')
    os.replace(temp, artifact)
    return 0


if __name__ == '__main__':
    raise SystemExit(_execute_tests(sys.argv[2:]) if sys.argv[1:2] == ['--execute'] else run())
