import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import image_identity as ii  # noqa: E402


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name, 'PERSONA_DATA_DIR': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_recall_by_name(self):
        # 事故：用户说「这也是祀呀，换个画风就不认识了？」
        names = [n for _s, n, _e in ii.candidates("这也是祀呀")]
        self.assertIn("祀", names)

    def test_recall_by_features_across_style(self):
        # 新画风的祀：绿发 / 头上带角 / 蓝眼 —— 与已存特征重合
        names = [n for _s, n, _e in ii.candidates("绿色头发 头上带角 蓝色眼睛 的干员")]
        self.assertIn("祀", names)

    def test_unrelated_is_not_matched(self):
        self.assertEqual(ii.candidates("今天重庆天气怎么样"), [])

    def test_hint_mentions_character(self):
        self.assertIn("祀", ii.hint_for("这是祀嘛"))

    def test_learns_character_from_named_image(self):
        ii.learn_from_message("这也是阿丙呀，换皮了", True)
        names = [n for _s, n, _e in ii.candidates("阿丙")]
        self.assertIn("阿丙", names)

    def test_does_not_learn_without_image(self):
        ii.learn_from_message("这是阿甲呀", False)
        self.assertEqual(ii.candidates("阿甲"), [])


if __name__ == '__main__':
    unittest.main()