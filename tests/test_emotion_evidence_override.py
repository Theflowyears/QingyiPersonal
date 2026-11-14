# -*- coding: utf-8 -*-
"""emotion_dynamics._praise_override 的回归锁。

praise_evidence 进账本冻结哈希（parameter_hash）。词表是人格数据：
部署方用 DATA_DIR/personas/emotion_evidence.json 注入与账本一致的词表，
advance 的冻结守卫才放行、观察时钟不重置；缺文件回落仓库默认。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT))
import emotion_dynamics as ed


class PraiseOverride(unittest.TestCase):
    def _data(self, files):
        tmp = tempfile.TemporaryDirectory(prefix="ev_ovr_")
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "personas").mkdir()
        for name, body in files.items():
            (Path(tmp.name) / "personas" / name).write_text(body, encoding="utf-8")
        return tmp.name

    def test_no_file_returns_none(self):
        with patch.dict(os.environ, {"DATA_DIR": self._data({})}):
            self.assertIsNone(ed._praise_override())

    def test_valid_list_returned(self):
        body = json.dumps({"praise_evidence": ["甲谢", "乙夸"]}, ensure_ascii=False)
        d = self._data({"emotion_evidence.json": body})
        with patch.dict(os.environ, {"DATA_DIR": d}):
            self.assertEqual(ed._praise_override(), ["甲谢", "乙夸"])

    def test_bad_payloads_fall_back_none(self):
        bodies = [
            json.dumps({"praise_evidence": []}),
            json.dumps({"praise_evidence": "str"}),
            json.dumps({"praise_evidence": [1, 2]}),
            "not json at all",
        ]
        for body in bodies:
            d = self._data({"emotion_evidence.json": body})
            with patch.dict(os.environ, {"DATA_DIR": d}):
                self.assertIsNone(ed._praise_override(), body[:24])

    def test_module_params_are_builtin_without_data(self):
        # 套件运行环境没有 personas 文件 → 模块参数必须是仓库默认词表
        self.assertIn("帮我查清楚了", ed.PARAMETERS["praise_evidence"])


if __name__ == "__main__":
    unittest.main()
