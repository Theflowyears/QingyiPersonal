"""清漪后台检测API的权限、任务与报告边界。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, 'website'), os.path.join(ROOT, 'bot')):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def _app():
    from flask import Flask
    import ceylan_dashboard
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test-only'
    ceylan_dashboard.install(app, {})
    return app


class QingyiApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='qingyi_api_')
        self.addCleanup(__import__('shutil').rmtree, self.dir, True)
        env = patch.dict(os.environ, {'DATA_DIR': self.dir, 'PERSONA_DATA_DIR': os.path.join(self.dir, 'persona')})
        env.start()
        self.addCleanup(env.stop)
        self.client = _app().test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s['ceylan_admin'] = True
            s['bot_control_csrf'] = 'test-token'

    def test_capability_audit_is_admin_only_and_keeps_five_layers(self):
        self.assertEqual(401,self.client.get('/admin/api/bot/qingyi/capabilities').status_code)
        self.login()
        body=self.client.get('/admin/api/bot/qingyi/capabilities').get_json()
        self.assertTrue(body['ok'])
        self.assertEqual({'implemented','wired','exercised','evaluated','released'},
                         set(body['capabilities']['active_memory']))

    def test_capability_evaluation_endpoint_returns_real_counts(self):
        self.login()
        body=self.client.post('/admin/api/bot/qingyi/capabilities/evaluate',
                              headers={'X-CSRF-Token':'test-token'}).get_json()
        self.assertEqual(body['evaluation']['total'],body['evaluation']['passed']+body['evaluation']['failed'])

    def test_overview_requires_admin(self):
        self.assertEqual(401, self.client.get('/admin/api/bot/qingyi/overview').status_code)

    def test_stage3_signoff_never_enables_expression_and_gates_grant(self):
        self.login()
        h = {'X-CSRF-Token': 'test-token'}
        got = self.client.get('/admin/api/bot/qingyi/stage3-signoff').get_json()
        self.assertFalse(got['expression_control'])
        # 空账本下工程/自然门不全过 → 授予被拒
        r = self.client.post('/admin/api/bot/qingyi/stage3-signoff',
                             json={'granted': True}, headers=h)
        self.assertEqual(400, r.status_code)
        self.assertFalse(r.get_json()['expression_control'])
        # 撤回永远允许，且只记账不翻开关
        r2 = self.client.post('/admin/api/bot/qingyi/stage3-signoff',
                              json={'granted': False}, headers=h)
        self.assertEqual(200, r2.status_code)
        self.assertFalse(r2.get_json()['expression_control'])
        self.assertFalse(r2.get_json()['signoff']['granted'])

    def test_stage3_signoff_requires_csrf(self):
        self.login()
        r = self.client.post('/admin/api/bot/qingyi/stage3-signoff', json={'granted': False})
        self.assertEqual(403, r.status_code)

    def _write(self, rel, obj):
        path = os.path.join(self.dir, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_round_preflight_wins_over_the_old_job_report(self):
        """面板必须看得见本轮刚做完的活，不能还显示「拟议」。"""
        import qingyi_preflight, qingyi_preflight_jobs
        self.login()
        ident = qingyi_preflight.report_identity()
        stamp = {"catalog_version": qingyi_preflight.CATALOG_VERSION, **ident}
        self._write('ops/preflight.json', {
            'tasks': [{'id': 'R01', 'implementation': 'implemented'}],
            'stages': [], 'scope_status': 'pass', 'checked_at': '2026-11-11T23:46:12+0800',
            **stamp})
        with patch.object(qingyi_preflight_jobs, 'latest',
                          return_value={'run_id': 'b' * 32, 'state': 'pass'}), \
             patch.object(qingyi_preflight_jobs, 'read_report',
                          return_value={**stamp, 'tasks': [], 'scope_status': 'old'}):
            body = self.client.get('/admin/api/bot/qingyi/overview').get_json()
        self.assertEqual('round', body['report_source'])
        self.assertEqual('R01', body['report']['tasks'][0]['id'])

    def test_stale_round_preflight_is_ignored(self):
        import qingyi_preflight, qingyi_preflight_jobs
        self.login()
        ident = qingyi_preflight.report_identity()
        stamp = {"catalog_version": qingyi_preflight.CATALOG_VERSION, **ident}
        self._write('ops/preflight.json', {
            'tasks': [{'id': 'OLD1'}], 'stages': [],
            **{**stamp, 'binding_digest': 'stale'}})
        with patch.object(qingyi_preflight_jobs, 'latest',
                          return_value={'run_id': 'b' * 32, 'state': 'pass'}), \
             patch.object(qingyi_preflight_jobs, 'read_report',
                          return_value={**stamp, 'tasks': [{'id': 'JOB1'}]}):
            body = self.client.get('/admin/api/bot/qingyi/overview').get_json()
        self.assertEqual('job', body['report_source'])
        self.assertEqual('JOB1', body['report']['tasks'][0]['id'])

    def test_approval_moves_the_task_out_of_pending_immediately(self):
        """点了同意就要立刻换分组：账本比计划新，不能等下一轮巡检。"""
        self.login()
        self._write('ops/plan.json', {'at': '2026-11-11T20:00:00+0800',
                                      'frontier': [{'id': 'V02', 'kind': 'ask'},
                                                   {'id': 'A01', 'kind': 'auto'}]})
        self._write('ops/state.json', {'approvals': {}})
        body = self.client.get('/admin/api/bot/qingyi/overview').get_json()
        self.assertEqual(['A01'], body['next']['doable'])
        self.assertEqual(['V02'], body['next']['approval'])

        self._write('ops/state.json', {'approvals': {'V02': {'decision': 'approve'}}})
        body = self.client.get('/admin/api/bot/qingyi/overview').get_json()
        self.assertIn('V02', body['next']['doable'])
        self.assertEqual([], body['next']['approval'])
        self.assertEqual('approve', body['approvals']['V02'])

    def test_rejected_task_leaves_both_buckets(self):
        self.login()
        self._write('ops/plan.json', {'at': 't', 'frontier': [{'id': 'V02', 'kind': 'ask'}]})
        self._write('ops/state.json', {'approvals': {'V02': {'decision': 'reject'}}})
        body = self.client.get('/admin/api/bot/qingyi/overview').get_json()
        self.assertEqual([], body['next']['doable'])
        self.assertEqual([], body['next']['approval'])

    def test_decision_needs_csrf_and_a_known_task(self):
        self.login()
        url = '/admin/api/bot/qingyi/decision'
        self.assertEqual(403, self.client.post(
            url, json={'task_id': 'V02', 'decision': 'approve'}).status_code)
        resp = self.client.post(url, json={'task_id': 'ZZ99', 'decision': 'approve'},
                                headers={'X-CSRF-Token': 'test-token'})
        self.assertEqual(400, resp.status_code)
        self.assertIn('目录', resp.get_json()['message'])

    def test_overview_is_catalog_and_latest_report_only(self):
        import qingyi_roadmap, qingyi_preflight_jobs
        self.login()
        with patch.object(qingyi_roadmap, 'summary', return_value={'stages': [], 'tasks': [], 'validation': {}}), \
             patch.object(qingyi_preflight_jobs, 'latest', return_value={'run_id': 'a' * 32, 'state': 'pass'}), \
             patch.object(qingyi_preflight_jobs, 'read_report', return_value={'scope_status': 'pass'}):
            body = self.client.get('/admin/api/bot/qingyi/overview').get_json()
        self.assertEqual({'run_id': 'a' * 32, 'state': 'pass'}, body['latest_job'])
        self.assertEqual({'scope_status': 'pass'}, body['report'])
        self.assertFalse(body['authorization']['stage3'])

    def test_stale_report_from_old_catalog_is_not_shown_as_current(self):
        import qingyi_preflight_jobs, qingyi_preflight, qingyi_roadmap
        self.login()
        identity = qingyi_preflight.report_identity()
        fresh = {"catalog_version": qingyi_preflight.CATALOG_VERSION,
                 "scope_status": "blocked", "tasks": [{"id": "A03"}], **identity}
        stale = {"catalog_version": "2020-01-01", "scope_status": "blocked",
                 "tasks": [{"id": "OLD"}], **identity}
        with patch.object(qingyi_preflight_jobs, "latest",
                          return_value={"run_id": "a" * 32, "state": "blocked"}),              patch.object(qingyi_preflight_jobs, "read_report", return_value=stale),              patch.object(qingyi_roadmap, "summary",
                          return_value={"stages": [], "tasks": [{"id": "LIVE"}],
                                        "validation": {}}):
            body = self.client.get("/admin/api/bot/qingyi/overview").get_json()
        self.assertEqual([{"id": "LIVE"}], body["tasks"])
        with patch.object(qingyi_preflight_jobs, "latest",
                          return_value={"run_id": "a" * 32, "state": "blocked"}),              patch.object(qingyi_preflight_jobs, "read_report", return_value=fresh):
            body = self.client.get("/admin/api/bot/qingyi/overview").get_json()
        self.assertEqual([{"id": "A03"}], body["tasks"])

    def test_report_with_old_code_binding_is_stale_even_on_same_catalog(self):
        """目录版本没变但代码变了：旧报告的绿灯同样不能沿用（统一方案 §23.4）。"""
        import qingyi_preflight_jobs, qingyi_preflight, qingyi_roadmap
        self.login()
        identity = qingyi_preflight.report_identity()
        rebound = dict(identity, binding_digest="0" * 64)
        report = {"catalog_version": qingyi_preflight.CATALOG_VERSION,
                  "scope_status": "pass", "tasks": [{"id": "OLD"}], **rebound}
        with patch.object(qingyi_preflight_jobs, "latest",
                          return_value={"run_id": "a" * 32, "state": "pass"}), \
             patch.object(qingyi_preflight_jobs, "read_report", return_value=report), \
             patch.object(qingyi_roadmap, "summary",
                          return_value={"stages": [], "tasks": [{"id": "LIVE"}],
                                        "validation": {}}):
            body = self.client.get("/admin/api/bot/qingyi/overview").get_json()
        self.assertEqual([{"id": "LIVE"}], body["tasks"])

    def test_overview_exposes_current_identity_for_frontend_comparison(self):
        """前端不能只凭 binding_digest 存在就说「已绑定当前代码」：
        overview 必须给出当前身份，让前端逐字段比对（轮询换新报告后也成立）。"""
        import qingyi_preflight_jobs, qingyi_preflight
        self.login()
        identity = qingyi_preflight.report_identity()
        report = {"catalog_version": qingyi_preflight.CATALOG_VERSION,
                  "scope_status": "pass", "tasks": [], **identity}
        with patch.object(qingyi_preflight_jobs, "latest",
                          return_value={"run_id": "a" * 32, "state": "pass"}), \
             patch.object(qingyi_preflight_jobs, "read_report", return_value=report):
            body = self.client.get("/admin/api/bot/qingyi/overview").get_json()
        self.assertEqual(identity["binding_digest"], body["identity"]["binding_digest"])
        self.assertEqual(identity["data_identity_digest"], body["identity"]["data_identity_digest"])
        self.assertEqual(qingyi_preflight.CATALOG_VERSION, body["identity"]["catalog_version"])

    def test_check_meta_label_compares_identity_not_mere_presence(self):
        """静态锁：renderQingyiChecks 的「已绑定」文案必须由逐字段比对守卫。"""
        import re
        with open(os.path.join(ROOT, 'website', 'static', 'console.js'), encoding='utf-8') as f:
            js = f.read()
        match = re.search(r'function renderQingyiChecks\(\)\{(.*?)\n  \}', js, re.S)
        self.assertIsNotNone(match, 'renderQingyiChecks 不见了')
        body = match.group(1)
        for field in ('binding_digest', 'data_identity_digest', 'catalog_version'):
            self.assertIn('ident.' + field, body, '没有比对 ' + field)
        self.assertIn('QY.overview', body)
        self.assertNotRegex(body, r"QY\.report\.binding_digest \? '证据已绑定当前代码'",
                            '只凭存在就宣称绑定当前代码的旧写法回来了')

    def test_post_requires_csrf_and_fixed_profile(self):
        import qingyi_preflight_jobs
        self.login()
        self.assertEqual(403, self.client.post('/admin/api/bot/qingyi/preflight',
                                               json={'profile': 'evidence'}).status_code)
        with patch.object(qingyi_preflight_jobs, 'start', return_value={'state': 'queued', 'run_id': 'b' * 32}) as start:
            body = self.client.post('/admin/api/bot/qingyi/preflight',
                                    json={'profile': 'evidence'},
                                    headers={'X-CSRF-Token': 'test-token'}).get_json()
            start.assert_called_once()
        self.assertEqual('queued', body['state'])
        bad = self.client.post('/admin/api/bot/qingyi/preflight', json={'profile': 'shell'},
                               headers={'X-CSRF-Token': 'test-token'})
        self.assertEqual(400, bad.status_code)

    def test_post_rejects_non_object_json_without_a_server_error(self):
        import qingyi_preflight_jobs
        self.login()
        with patch.object(qingyi_preflight_jobs, 'start') as start:
            for payload in ([{'profile': 'evidence'}], 'evidence', 3, None):
                response = self.client.post('/admin/api/bot/qingyi/preflight', json=payload,
                                            headers={'X-CSRF-Token': 'test-token'})
                self.assertEqual(400, response.status_code, payload)
            start.assert_not_called()

    def test_overview_reads_do_not_create_diagnostics_store(self):
        self.login()
        self.client.get('/admin/api/bot/qingyi/overview')
        self.assertFalse((__import__('pathlib').Path(self.dir) / 'diagnostics').exists())

    def test_report_id_is_strictly_bounded(self):
        import qingyi_preflight_jobs
        self.login()
        self.assertIn(self.client.get('/admin/api/bot/qingyi/preflight/..%2Fsecret').status_code, (400, 404))
        with patch.object(qingyi_preflight_jobs, 'get', return_value=None):
            self.assertEqual(404, self.client.get('/admin/api/bot/qingyi/preflight/' + 'c' * 32).status_code)


if __name__ == '__main__':
    unittest.main()
