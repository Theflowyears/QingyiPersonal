"""回归锁：群长期记忆面板。

用户报的是「群长期记忆怎么什么都不显示了？？没了吗？？」。数据没没 ——
坏的是本地文件桥那条回退路径：它无条件把 `memory`（文本）和 `cache`（数组）
换成了长度，前端拿数字去 `.slice()` 直接抛 TypeError，整块面板就空了。
这里钉三件事：字段口径（含 memory_updated / memory_revision 不能丢）、
首屏仍然只拿长度、以及两条路径必须共用同一个裁剪函数。
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "website")
if WEB not in sys.path:
    sys.path.append(WEB)

import ceylan_bot_control as ctl        # noqa: E402

GROUP = {"id": "900000001", "name": "测试群", "code": "c1",
         "memory": "这群最近在聊明日方舟的新活动，阿乙说想打高难。",
         "memory_complete": True, "memory_revision": "abc1234567",
         "memory_updated": 1794000000.0,
         "cache": [{"seq": 1, "time": "10:00", "nickname": "阿乙",
                    "text": "晚上开一把"}] * 3,
         "cache_total": 3, "cache_revision": "r2", "busy": False,
         "last_message": 1794000100.0}


class GroupRowsTests(unittest.TestCase):
    def test_full_mode_keeps_text_and_list(self):
        row = ctl._group_rows([GROUP], False)[0]
        self.assertIsInstance(row["memory"], str, "记忆正文被换成数字，面板就是空白")
        self.assertIsInstance(row["cache"], list)
        # 面板那一步就是 cache.slice(-14)：数字会直接抛 TypeError
        self.assertEqual(len(row["cache"][-14:]), 3)

    def test_update_time_and_revision_survive(self):
        row = ctl._group_rows([GROUP], False)[0]
        self.assertEqual(row["memory_updated"], 1794000000.0)
        self.assertEqual(row["memory_revision"], "abc1234567")
        self.assertEqual(row["cache_total"], 3)
        self.assertTrue(row["memory_complete"])

    def test_slim_mode_is_shorter_and_numeric(self):
        slim = ctl._group_rows([GROUP], True)[0]
        full = ctl._group_rows([GROUP], False)[0]
        self.assertIsInstance(slim["memory"], int)
        self.assertIsInstance(slim["cache"], int)
        self.assertLess(len(repr(slim)), len(repr(full)))

    def test_legacy_key_name_is_accepted(self):
        row = ctl._group_rows([dict(GROUP, memory_updated=None,
                                    memory_update=123.0)], False)[0]
        self.assertEqual(row["memory_updated"], 123.0)

    def test_garbage_rows_are_skipped_not_crashed(self):
        rows = ctl._group_rows([None, "x", GROUP], False)
        self.assertEqual(len(rows), 1)


class SharedPlumbingTests(unittest.TestCase):
    """两条路径必须共用同一个裁剪函数 —— 各写一套就是这次的成因。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(WEB, "ceylan_bot_control.py"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_both_branches_use_the_helper(self):
        self.assertEqual(self.src.count("state['groups'] = _group_rows"), 2,
                         "正常路径与本地文件桥回退各自裁一次，迟早又不一致")

    def test_fallback_no_longer_turns_text_into_lengths(self):
        start = self.src.index("面板首屏只要每群的摘要长度")
        blk = self.src[start:start + 900]
        self.assertNotIn("'memory': len(", blk)
        self.assertIn("request.args.get('slim')", blk)


if __name__ == "__main__":
    unittest.main()