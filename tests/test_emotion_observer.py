import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / 'bot', ROOT / 'website'):
    if str(p) not in sys.path:
        sys.path.append(str(p))
from flask import Flask
import ceylan_dashboard
import emotion_shadow
import self_model


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shadow_observer_')
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name,
                                     'PERSONA_DATA_DIR': str(Path(self.tmp.name) / 'persona')})
        env.start()
        self.addCleanup(env.stop)
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        ceylan_dashboard.install(self.app, {})
        self.client = self.app.test_client()

    def test_shadow_api_is_admin_only(self):
        self.assertEqual(self.client.get('/admin/api/bot/emotion-shadow').status_code, 401)

    def test_reading_empty_shadow_is_read_only(self):
        with self.client.session_transaction() as s:
            s['ceylan_admin'] = True
        r = self.client.get('/admin/api/bot/emotion-shadow')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['mode'], 'shadow')
        self.assertFalse(d['expression_control'])
        self.assertFalse(emotion_shadow.shadow_path().exists())

    def test_observer_does_not_return_raw_chat_or_person_ids(self):
        secret = '测试隐私内容-不要出现在面板'
        self_model.record_event('group_message', ts=100, payload={'text': 'hello'}, dedupe='old')
        emotion_shadow.tick(now=1000)
        self_model.record_event('private_message', ts=1001, actor_qq='1234567',
                                 payload={'text': secret}, dedupe='new')
        with closing(self_model._db()) as c, c:
            c.execute('UPDATE event SET created=1001 WHERE dedupe=?', ('new',))
        emotion_shadow.tick(now=1002)
        with self.client.session_transaction() as s:
            s['ceylan_admin'] = True
        r = self.client.get('/admin/api/bot/emotion-shadow')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(secret, r.get_data(as_text=True))
        self.assertNotIn('1234567', r.get_data(as_text=True))
        self.assertEqual(r.get_json()['processed'], 1)

    def test_shadow_poll_is_nonblocking_and_awaitable_without_ws(self):
        import ceylan_runtime as runtime
        self.assertTrue(callable(getattr(runtime, 'poll_emotion_shadow', None)),
                        '无WS影子接线尚未实现')
        self_model.record_event('group_message', ts=100, payload={'text': 'hello'}, dedupe='old')
        with patch.object(runtime.time, 'time', return_value=1000):
            result = asyncio.run(runtime.poll_emotion_shadow())
        self.assertEqual(result['mode'], 'shadow')
        self.assertTrue(result['running'])


if __name__ == '__main__':
    unittest.main()
