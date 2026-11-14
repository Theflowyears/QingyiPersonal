"""一键前置检测任务所有权与并发合同。"""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / 'website', ROOT / 'bot'):
    if str(directory) in sys.path:
        sys.path.remove(str(directory))
    sys.path.insert(0, str(directory))

import qingyi_preflight_jobs as jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qingyi_jobs_')
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict('os.environ', {'DATA_DIR': self.temp.name, 'PERSONA_DATA_DIR': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 1000.0

    def _claim(self, key='same', profile='evidence'):
        return jobs.claim(profile=profile, scope='qingyi_all', request_key=key,
                          now=self.now)

    def test_same_request_returns_one_active_job(self):
        first = self._claim()
        second = self._claim()
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertTrue(second['reused'])

    def test_different_profile_conflicts_and_starts_nothing(self):
        first = self._claim()
        busy = jobs.claim(profile='isolated', scope='qingyi_all', request_key='other', now=self.now)
        self.assertEqual('conflict', busy['state'])
        self.assertEqual(first['run_id'], busy['existing_run_id'])

    def test_only_owner_with_current_fence_can_finish(self):
        job = self._claim()
        self.assertFalse(jobs.finish(job['run_id'], 'wrong-owner', 99, 'pass', {}))
        self.assertTrue(jobs.heartbeat(job['run_id'], job['owner'], job['fence'], self.now + 1))
        self.assertTrue(jobs.finish(job['run_id'], job['owner'], job['fence'], 'pass',
                                   {'scope_status': 'pass'}, now=self.now + 2))

    def test_old_owner_cannot_replace_expired_job_before_hard_deadline(self):
        job = self._claim()
        self.now += 20
        recovering = self._claim()
        self.assertEqual('recovering', recovering['state'])
        self.assertEqual(job['run_id'], recovering['existing_run_id'])
        self.now += 30
        replacement = self._claim(key='same')
        self.assertNotEqual(job['run_id'], replacement['run_id'])
        old = jobs.get(job['run_id'])
        self.assertEqual('interrupted', old['state'])

    def test_report_writer_and_reader_are_run_id_bounded(self):
        job = self._claim()
        path = jobs.report_path(job['run_id'])
        Path(path).write_text(json.dumps({'ok': True}), encoding='utf-8')
        self.assertEqual({'ok': True}, jobs.read_report(job['run_id']))
        self.assertIsNone(jobs.read_report('../evil'))

    def test_public_view_hides_owner_capability_and_keeps_failure_reason(self):
        job = self._claim()
        jobs.finish(job['run_id'], job['owner'], job['fence'], 'timed_out',
                    error='preflight_deadline', now=self.now + 1)
        view = jobs.get(job['run_id'])
        self.assertNotIn('owner', view)
        self.assertEqual('timed_out', view['state'])
        self.assertEqual('preflight_deadline', view['error'])

    def test_finish_rejects_unsafe_run_id_before_writing_anywhere(self):
        with self.assertRaises(ValueError):
            jobs.finish('../escape', 'owner', 1, 'pass', {'scope_status': 'pass'})
        self.assertFalse((jobs.root() / 'escape.json').exists())

    def test_worker_stdout_with_trailing_log_lines_still_yields_report(self):
        report = jobs.extract_report(
            'noise from an imported module\n'
            + json.dumps({'schema_version': 1, 'checks': [], 'scope_status': 'blocked'})
            + '\n[flush] done\n')
        self.assertEqual('blocked', report['scope_status'])
        with self.assertRaises(ValueError):
            jobs.extract_report('only logs\nno json here\n')

    def test_long_run_renews_lease_while_the_worker_is_alive(self):
        class SlowStream:
            def __init__(self, text, delay):
                self._text, self._delay, self._sent = text, delay, False
            def read(self, size=-1):
                if self._sent:
                    return ''
                time.sleep(self._delay)
                self._sent = True
                return self._text

        class FakeProcess:
            returncode = 0
            def __init__(self):
                import io
                self.stdout = SlowStream(json.dumps({'schema_version': 1, 'checks': [],
                                                     'scope_status': 'blocked'}), 1.6)
                self.stderr = io.StringIO('')
            def kill(self):
                pass
            def wait(self, timeout=None):
                return 0

        job = jobs.claim(profile='evidence', scope='qingyi_all',
                         request_key='lease', now=time.time())
        with patch.object(jobs.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(jobs, 'heartbeat', return_value=True) as beat:
            jobs._supervise(job)
        self.assertGreaterEqual(beat.call_count, 2, '长任务等待期间没有续租')
        self.assertEqual('blocked', jobs.get(job['run_id'])['state'])

    def test_large_report_is_drained_while_the_worker_runs(self):
        import subprocess as real_subprocess
        real_popen = real_subprocess.Popen
        code = ('import json; print(json.dumps({"schema_version": 1, "checks": [],'
                ' "scope_status": "blocked", "pad": "x" * 200000}))')

        def fake_popen(args, **kwargs):
            return real_popen([sys.executable, '-c', code], **kwargs)

        job = self._claim()
        with patch.object(jobs.subprocess, 'Popen', side_effect=fake_popen):
            jobs._supervise(job)
        row = jobs.get(job['run_id'])
        self.assertEqual('blocked', row['state'], row)
        self.assertIsNotNone(jobs.read_report(job['run_id']))

    def _supervise_fake_exit(self, code):
        class FakeProcess:
            returncode = code
            def __init__(self):
                import io
                self.stdout = io.StringIO(json.dumps({'schema_version': 1, 'checks': [],
                                                      'scope_status': 'blocked'}))
                self.stderr = io.StringIO('')
            def kill(self):
                pass
            def wait(self, timeout=None):
                return self.returncode

        job = jobs.claim(profile='evidence', scope='qingyi_all',
                         request_key='exit-%s' % code, now=time.time())
        with patch.object(jobs.subprocess, 'Popen', return_value=FakeProcess()):
            jobs._supervise(job)
        return jobs.get(job['run_id'])

    def test_worker_exit_codes_are_verdicts_not_failures(self):
        self.assertEqual('', self._supervise_fake_exit(1)['error'])
        self.assertEqual('worker_exit_7', self._supervise_fake_exit(7)['error'])


if __name__ == '__main__':
    unittest.main()
