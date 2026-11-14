import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gateway_watchdog', ROOT / 'bot' / 'watchdog.py')
watchdog = importlib.util.module_from_spec(spec)
with patch('os.path.isfile', return_value=False):
    spec.loader.exec_module(watchdog)


class WebHealthTests(unittest.TestCase):
    def setUp(self):
        self.responses = {
            '/local': (200, '<html>login</html>'),
            '/public': (502, 'error code: 502'),
        }
        responses = self.responses

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = responses[self.path]
                raw = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
                self.send_response(status)
                self.send_header('CF-Ray', 'test-edge-LAX')
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        base = 'http://127.0.0.1:%d' % self.server.server_port
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'DATA_DIR': self.tmp.name,
            'WATCHDOG_LOCAL_URL': base + '/local',
            'WATCHDOG_PUBLIC_URL': base + '/public',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, value in (('DATA_DIR', Path(self.tmp.name)),
                            ('STATE_FILE', Path(self.tmp.name) / 'watchdog.json')):
            p = patch.object(watchdog, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (('_service_active', True), ('_port_open', True),
                            ('_bot_connected_recently', (True, 0)), ('_probe_extra', {})):
            p = patch.object(watchdog, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        if hasattr(watchdog, '_WEB_HEALTH_CACHE'):
            watchdog._WEB_HEALTH_CACHE.clear()

    def test_slow_body_does_not_hold_up_a_status_probe(self):
        original = self.server.RequestHandlerClass.do_GET

        def slow(handler):
            if handler.path != '/slow':
                return original(handler)
            handler.send_response(200)
            handler.end_headers()
            for _ in range(6):
                try:
                    handler.wfile.write(b'x')
                    handler.wfile.flush()
                except OSError:
                    return
                time.sleep(.2)

        self.server.RequestHandlerClass.do_GET = slow
        started = time.monotonic()
        result = watchdog._web_probe('http://127.0.0.1:%d/slow' % self.server.server_port)
        self.assertLess(time.monotonic() - started, .4, '探测只需HTTP状态，不该读慢响应体')
        self.assertEqual(result['status'], 200)

    def test_public_502_is_reported_without_restarting_healthy_origin(self):
        issues = watchdog.collect_issues()
        issue = next((i for i in issues if i[1] == 'public_gateway'), None)
        self.assertIsNotNone(issue, '公网502不能继续显示健康')
        self.assertIn('502', issue[2])
        self.assertFalse(issue[3], '边缘故障不能盲目重启本机服务')
        status = watchdog.status()
        self.assertEqual(status['web']['public']['status'], 502)
        self.assertTrue(status['web']['local']['ok'])

    def test_self_heal_rechecks_origin_instead_of_using_cached_failure(self):
        self.responses['/local'] = (503, 'not ready')
        self.responses['/public'] = (200, '<html>login</html>')
        watchdog._web_health()

        def restore(_):
            self.responses['/local'] = (200, '<html>login</html>')
            return ['重启 qqbot']

        with patch.object(watchdog, '_service_active', side_effect=lambda name: self.responses['/local'][0] == 200 if name == 'qqbot' else True), \
                patch.object(watchdog, 'heal', side_effect=restore), \
                patch.object(watchdog, 'STATE_FILE', Path(self.tmp.name) / 'status.json'), \
                patch.object(watchdog.time, 'sleep'), patch.object(watchdog, '_notify', return_value=False), \
                patch.object(watchdog, 'DATA_DIR', Path(self.tmp.name)):
            watchdog.run_once()
        self.assertEqual(json.loads((Path(self.tmp.name) / 'status.json').read_text(encoding='utf-8'))['status'], 'ok')

    def test_recent_probe_result_is_reused_without_refetching_public_site(self):
        first = watchdog._web_health()
        self.responses['/public'] = (200, '<html>login</html>')
        self.assertEqual(watchdog._web_health()['public']['status'], 502)
        self.assertEqual(first['scope'], 'server_network_only')
        with patch.object(watchdog.time, 'monotonic', return_value=10 ** 12):
            self.assertEqual(watchdog._web_health()['public']['status'], 200)

    def test_status_uses_one_probe_snapshot_for_issues_and_checklist(self):
        failed = {'local': {'ok': True, 'status': 200},
                  'public': {'ok': False, 'status': 502}}
        recovered = {'local': {'ok': True, 'status': 200},
                     'public': {'ok': True, 'status': 200}}
        with patch.object(watchdog, '_web_health', side_effect=[failed, recovered]):
            status = watchdog.status()
        self.assertEqual(status['web']['public']['status'], 502)
        self.assertIn('public_gateway', {i['code'] for i in status['issues']})

    def test_health_checklist_has_no_cloudflare_tunnel_row(self):
        # 已迁到港机直连，不再有 Cloudflare Tunnel，健康检查不应再列出该行。
        names = {row['name'] for row in watchdog.status()['checklist']}
        self.assertFalse(any('Cloudflare' in n or 'Tunnel' in n for n in names), names)

    def test_local_failure_is_not_reported_as_public_gateway_failure(self):
        self.responses['/local'] = (503, 'not ready')
        codes = {i[1] for i in watchdog.collect_issues()}
        self.assertIn('web_unhealthy', codes)
        self.assertNotIn('public_gateway', codes)


if __name__ == '__main__':
    unittest.main()
