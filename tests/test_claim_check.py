# -*- coding: utf-8 -*-
"""交叉核对回归锁：只收口不封口。

19:26 那次：阿戊说「还有一个每次都一键跟随的小朋友」，阿甲说「我还在路上」，
她把阿戊的谜面安到阿甲头上。事实全在上下文里，是拼错了人。
这里的口径是**降级指名**而不是禁言——把名字换成「某位」，句子照发。
证据不唯一（两个人都说过同一句）就不动，因为宁可漏收一次，也不要冤枉她。
"""
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
sys.path.insert(0, BOT)

import claim_check as cc

CONTEXT = """[19:25:05] 阿戊: [图片]
[19:25:08] 阿戊: 哎呀呀
[19:25:18] 阿甲: 可惜
[19:25:21] 阿甲: 我还在路上
[19:25:27] 阿戊: 还有一个每次都一键跟随的小朋友
[19:25:34] 阿戊: 我跟他关系特别特别好呀
[19:25:38] 阿戊: 是谁呢
[19:26:14] 清漪（你 → 阿戊）: 哎呀，阿戊姐姐这是在搞猜谜大会呢？"""

INCIDENT = "不过你这一问一答加发图的节奏，怎么听着像是在cue阿甲那个“一键跟随”的小朋友呀？"


class ParseTests(unittest.TestCase):
    def test_bot_claims_are_available_but_not_human_ownership_evidence(self):
        rows = cc.parse_context(CONTEXT)
        self.assertEqual({name for name, _ in rows if "（你" not in name}, {"阿戊", "阿甲"})
        self.assertNotIn("清漪（你 → 阿戊）", cc.ownership(rows).values())

    def test_ownership_skips_ambiguous_phrases(self):
        rows = [("阿戊", "我也在吃饭"), ("阿甲", "我也在吃饭"), ("阿丁", "我还在路上")]
        owned = cc.ownership(rows)
        self.assertNotIn("我也在吃", owned)
        self.assertEqual(owned.get("我还在路"), "阿丁")


class MismatchTests(unittest.TestCase):
    def test_incident_is_softened_not_silenced(self):
        out = cc.cross_check(INCIDENT, CONTEXT)
        self.assertEqual(out["softened"], ["阿甲"])
        self.assertIn("某位", out["text"])
        self.assertNotIn("阿甲", out["text"])
        # 语气和句式必须原样保留
        self.assertTrue(out["text"].startswith("不过你这一问一答加发图的节奏"))
        self.assertTrue(out["text"].endswith("小朋友呀？"))

    def test_correct_attribution_is_untouched(self):
        for reply in ("阿戊姐姐说的那个一键跟随的小朋友，你俩关系真好啊。",
                      "阿甲你还在路上吗，人卫那边等你。",
                      "我才不接这茬。"):
            with self.subTest(reply=reply):
                out = cc.cross_check(reply, CONTEXT)
                self.assertEqual(out["text"], reply)
                self.assertEqual(out["mismatches"], [])

    def test_ambiguous_phrase_never_rewrites(self):
        ctx = "[19:20:01] 阿戊: 我也在吃饭\n[19:20:05] 阿甲: 我也在吃饭"
        reply = "阿甲说他也在吃饭"
        self.assertEqual(cc.cross_check(reply, ctx)["text"], reply)

    def test_short_or_empty_context_changes_nothing(self):
        for ctx in ("", "[19:20:01] 阿戊: 嗯"):
            with self.subTest(ctx=ctx):
                self.assertEqual(cc.cross_check(INCIDENT, ctx)["text"], INCIDENT)

    def test_no_crash_on_junk(self):
        self.assertEqual(cc.cross_check(None, None)["text"], "")
        self.assertEqual(cc.cross_check(INCIDENT, "乱码没有时间戳")["text"], INCIDENT)


class WiringLockTests(unittest.TestCase):
    def test_group_send_runs_cross_check(self):
        with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("claim_check", src)
        self.assertIn("cross_check(", src)

    def test_module_resolves_no_paths_at_import(self):
        # 纯函数模块：不落盘、不读环境，也就不存在路径冻结问题。
        with io.open(os.path.join(BOT, "claim_check.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("os.getenv", src)


if __name__ == "__main__":
    unittest.main()