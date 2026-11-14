# -*- coding: utf-8 -*-
"""首次配置向导 + 一键部署的回归锁。

覆盖：
  · 恢复的三个模块可 import（否则公开白板面板起不来——本次修复的阻断 bug）
  · config.env 就地更新：保留注释/其它键、只改目标键、绝不回显密钥
  · 完成门控：.setup_complete 存在时写/测试/重启端点一律 409（生产惰性）
  · save-keys 白名单与 base_url 校验；未登录 401
  · test-model 不泄漏 key、错误如实返回
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'website'))

from flask import Flask
import setup_wizard as sw


def make_app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True
    sw.install(app)
    return app


class RestoredModules(unittest.TestCase):
    def test_three_modules_import_and_export(self):
        k = importlib.import_module('ceylan_keys_common')
        c = importlib.import_module('ceylan_control_core')
        f = importlib.import_module('ceylan_fun_core')
        self.assertTrue(callable(k.validate))
        self.assertTrue(callable(c.validate))
        self.assertTrue(callable(f.revision) and callable(f.validate_settings))


class EnvUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wiz_env_')
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / 'config.env'
        self.patcher = patch.object(sw, '_config_path', return_value=self.cfg)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def test_preserves_comments_and_other_keys(self):
        self.cfg.write_text('# 注释\nLLM_TEXT_MODEL=old\nOTHER=keep\n', encoding='utf-8')
        sw._update_env({'LLM_TEXT_MODEL': 'new'})
        text = self.cfg.read_text(encoding='utf-8')
        self.assertIn('# 注释', text)
        self.assertIn('OTHER=keep', text)
        self.assertIn('LLM_TEXT_MODEL=new', text)
        self.assertNotIn('LLM_TEXT_MODEL=old', text)

    def test_appends_missing_key(self):
        self.cfg.write_text('A=1\n', encoding='utf-8')
        sw._update_env({'LLM_TEXT_API_KEY': 'sk-x'})
        self.assertIn('LLM_TEXT_API_KEY=sk-x', self.cfg.read_text(encoding='utf-8'))

    def test_creates_file_when_absent(self):
        sw._update_env({'LLM_TEXT_MODEL': 'm'})
        self.assertTrue(self.cfg.exists())

    def test_channels_state_never_returns_key_value(self):
        self.cfg.write_text('LLM_TEXT_API_KEY=sk-secret\nLLM_TEXT_MODEL=m\n', encoding='utf-8')
        st = sw._channels_state()
        self.assertTrue(st['text']['has_key'])
        self.assertEqual(st['text']['model'], 'm')
        self.assertNotIn('sk-secret', json.dumps(st))


class WizardEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wiz_ep_')
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'; self.data.mkdir()
        self.cfg = Path(self.tmp.name) / 'config.env'
        self.cfg.write_text('LLM_TEXT_MODEL=old\n', encoding='utf-8')
        self._p1 = patch.object(sw, '_config_path', return_value=self.cfg)
        self._p2 = patch.dict(os.environ, {'DATA_DIR': str(self.data)})
        self._p1.start(); self._p2.start()
        self.addCleanup(self._p1.stop); self.addCleanup(self._p2.stop)
        self.app = make_app()
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s['ceylan_admin'] = True

    def test_state_requires_admin(self):
        r = self.client.get('/admin/api/setup/state')
        self.assertEqual(r.status_code, 401)

    def test_save_keys_whitelist_and_validation(self):
        self.login()
        r = self.client.post('/admin/api/setup/save-keys', json={'channels': {'bogus': {}}})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/admin/api/setup/save-keys',
                             json={'channels': {'text': {'base_url': 'ftp://x'}}})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/admin/api/setup/save-keys',
                             json={'channels': {'text': {'base_url': 'https://a/v1', 'model': 'm', 'api_key': 'sk-1'}}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['restart_needed'])
        text = self.cfg.read_text(encoding='utf-8')
        self.assertIn('LLM_TEXT_BASE_URL=https://a/v1', text)
        self.assertIn('LLM_TEXT_API_KEY=sk-1', text)

    def test_complete_then_write_endpoints_409(self):
        self.login()
        r = self.client.post('/admin/api/setup/complete')
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.data / '.setup_complete').exists())
        for ep in ['/admin/api/setup/save-keys', '/admin/api/setup/test-model',
                   '/admin/api/setup/restart', '/admin/api/setup/complete']:
            r = self.client.post(ep, json={'channels': {'text': {'model': 'm'}}})
            self.assertEqual(r.status_code, 409, ep)

    def test_setup_page_redirects_when_complete(self):
        self.login()
        self.client.post('/admin/api/setup/complete')
        r = self.client.get('/setup')
        self.assertEqual(r.status_code, 302)

    def test_test_model_no_key_leak_and_error(self):
        self.login()
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('u', 401, 'x', {}, None)):
            r = self.client.post('/admin/api/setup/test-model',
                                 json={'base_url': 'https://a/v1', 'model': 'm', 'api_key': 'sk-supersecret'})
        body = r.get_data(as_text=True)
        self.assertNotIn('sk-supersecret', body)
        self.assertFalse(r.get_json()['ok'])
        self.assertIn('401', r.get_json()['error'])

    def test_test_model_success(self):
        self.login()
        resp = MagicMock()
        resp.read.return_value = json.dumps({'id': 'x', 'choices': [{'message': {'content': 'pong'}}]}).encode()
        resp.__enter__ = lambda self_: resp
        resp.__exit__ = lambda self_, *a: False
        with patch('urllib.request.urlopen', return_value=resp):
            r = self.client.post('/admin/api/setup/test-model',
                                 json={'base_url': 'https://a/v1', 'model': 'm', 'api_key': 'sk-x'})
        d = r.get_json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['reply_preview'], 'pong')


if __name__ == '__main__':
    unittest.main()
