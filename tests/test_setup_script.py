# -*- coding: utf-8 -*-
"""setup.py 一键引导的回归锁（隔离到临时目录，绝不碰真实 config.env）。"""
import argparse
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
setup = importlib.import_module('setup')


class GenConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='setup_gen_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.env.example').write_text(
            'WEB_SECRET_KEY=\nWEB_ADMIN_USER=admin\nWEB_ADMIN_PASSWORD=\n'
            'LLM_TEXT_API_KEY=\n# 注释保留\n', encoding='utf-8')
        self._p = patch.object(setup, 'ROOT', self.root)
        self._p.start(); self.addCleanup(self._p.stop)
        self.args = argparse.Namespace(admin_user='', admin_password='', port=5000)

    def test_generates_with_random_secret_and_keeps_blank_keys(self):
        cfg, created, creds = setup.gen_config(self.args)
        self.assertTrue(created)
        text = cfg.read_text(encoding='utf-8')
        self.assertIn('LLM_TEXT_API_KEY=', text)  # API key 留空交给面板
        self.assertIn('# 注释保留', text)
        self.assertTrue(creds['pass'])
        import re
        m = re.search(r'WEB_SECRET_KEY=([0-9a-f]+)', text)
        self.assertTrue(m and len(m.group(1)) >= 32)

    def test_does_not_overwrite_existing(self):
        (self.root / 'config.env').write_text('WEB_SECRET_KEY=existing\n', encoding='utf-8')
        cfg, created, creds = setup.gen_config(self.args)
        self.assertFalse(created)
        self.assertIsNone(creds)
        self.assertIn('existing', cfg.read_text(encoding='utf-8'))

    def test_explicit_credentials_respected(self):
        self.args.admin_user = 'boss'; self.args.admin_password = 'pw123'
        cfg, created, creds = setup.gen_config(self.args)
        self.assertEqual(creds['user'], 'boss')
        self.assertEqual(creds['pass'], 'pw123')


class NapcatMac(unittest.TestCase):
    def test_mac_persisted_and_stable(self):
        tmp = tempfile.TemporaryDirectory(prefix='setup_mac_')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); (root / 'data').mkdir()
        with patch.object(setup, 'ROOT', root):
            m1 = setup.napcat_mac()
            m2 = setup.napcat_mac()
        self.assertEqual(m1, m2)
        self.assertRegex(m1, r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$')
        self.assertTrue(m1.startswith('02:'))  # 本地管理位，避免与真实网卡冲突


if __name__ == '__main__':
    unittest.main()
