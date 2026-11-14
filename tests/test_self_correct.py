# -*- coding: utf-8 -*-
"""自我纠正的回归锁：她用内部标注当回复的那次事故不许重演。

事故原文（2026-11-11 19:24 群里，同一条群聊记录里可以看到）：
对方只发了一张图，她回了一条
    [图片：对方想表达：流汗，尴尬，不知所措]
那不是任何人的发言，是我们塞给模型的上下文标注。格式本身是诱因 ——
「[图片：…]」和她能说的话长得一模一样，模型照着抄了一遍。
所以锁两层：发送前必须拦，上下文里必须不再长成这样。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, BOT)

import self_correct as sc

INCIDENT = "[图片：对方想表达：流汗，尴尬，不知所措]"


def _bridge_src():
    with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
        return f.read()


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="self_correct_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_incident_text_is_dropped_entirely(self):
        cleaned, hits = sc.audit(INCIDENT)
        self.assertEqual(cleaned, "")
        self.assertIn("context_annotation", hits)

    def test_real_words_survive_while_the_annotation_goes(self):
        cleaned, hits = sc.audit("笨蛋主人，这点小场面就慌成这样？" + INCIDENT)
        self.assertEqual(cleaned.strip(), "笨蛋主人，这点小场面就慌成这样？")
        self.assertIn("context_annotation", hits)

    def test_all_media_annotations_are_stripped(self):
        for token in ("[图片]", "[表情]", "[表情包]", "[动画表情]", "[语音]",
                      "[视频]", "[文件]", "图片数=2", "[图片数=1]"):
            with self.subTest(token=token):
                cleaned, hits = sc.audit("看到了" + token)
                self.assertEqual(cleaned.strip(), "看到了")
                self.assertTrue(hits)

    def test_reply_marker_is_dropped(self):
        # 11-14 早安事故：她把上下文引用标注当回复发出去。
        cleaned, hits = sc.audit("[回复 1000000001 的消息:   /早安]")
        self.assertEqual(cleaned, "")
        self.assertIn("reply_marker", hits)
        cleaned2, hits2 = sc.audit("早安呐[回复 123 的消息: 早]")
        self.assertEqual(cleaned2.strip(), "早安呐")
        self.assertIn("reply_marker", hits2)

    def test_control_markers_still_die(self):
        cleaned, hits = sc.audit("[[查: 2026年11月9日 重庆 运势]]")
        self.assertEqual(cleaned, "")
        self.assertIn("control_marker", hits)

    def test_new_side_note_form_is_also_caught(self):
        """改格式只是降低被抄的概率，抄了照样要拦住。"""
        cleaned, hits = sc.audit("笨蛋主人（图：对方想表达：流汗，尴尬）")
        self.assertEqual(cleaned.strip(), "笨蛋主人")
        self.assertIn("image_side_note", hits)

    def test_ordinary_talk_is_not_touched(self):
        """误伤比漏拦更糟：这两条是她正常会说的话，一个字都不许改。"""
        for text in ("这张图挺好笑", "我才不接这茬。", "图片数不多了，省着点用"):
            with self.subTest(text=text):
                self.assertEqual(sc.audit(text), (text, []))

    def test_ledger_counts_and_keeps_a_sample(self):
        sc.audit(INCIDENT)
        sc.record(["context_annotation"], INCIDENT)
        sc.record(["context_annotation"], INCIDENT)
        stats = sc.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["patterns"]["context_annotation"]["count"], 2)
        self.assertIn("对方想表达", stats["patterns"]["context_annotation"]["sample"])

    def test_record_never_raises_when_the_ledger_cannot_be_written(self):
        # 把一个普通文件当成目录用：落盘必然失败，但不能把回复链路弄崩。
        with mock.patch.dict(os.environ, {"DATA_DIR": os.path.join(ROOT, "requirements.txt")}):
            self.assertFalse(sc.record(["context_annotation"], "x"))


class RecordableTests(unittest.TestCase):
    """写进记忆的那一层：脏东西一旦记成「她说过的话」，就会天天复读。"""

    def test_incident_is_not_recorded(self):
        self.assertEqual(sc.recordable(INCIDENT), ("", True))

    def test_sticker_marker_is_not_recorded_as_speech(self):
        self.assertEqual(sc.recordable("[表情包]"), ("", True))

    def test_mixed_line_keeps_only_her_words(self):
        text, dropped = sc.recordable("哼，我才不接这茬。" + INCIDENT)
        self.assertFalse(dropped)
        self.assertEqual(text, "哼，我才不接这茬。")

    def test_normal_line_is_untouched(self):
        self.assertEqual(sc.recordable("我才不接这茬。"), ("我才不接这茬。", False))


class ContextFormatLockTests(unittest.TestCase):
    """上下文标注不许再长得像一句发言。"""

    def test_bridge_never_puts_image_meaning_into_the_utterance(self):
        src = _bridge_src()
        self.assertNotIn('"[图片：%s]"', src,
                         "图片意思又被塞进「他说的那句话」里了，模型会照着抄")

    def test_context_prompt_teaches_the_parenthetical_form(self):
        src = _bridge_src()
        self.assertIn("「（图：…）」是那张图的意思", src)

    def test_record_path_refuses_machinery(self):
        """发送端拦住了还不够：记忆写入端也必须拒绝，否则它会绕一圈再回来。"""
        src = _bridge_src()
        self.assertIn("_sc.recordable(", src)

    def test_every_send_path_runs_the_self_correction(self):
        """群聊、私聊、语音三条出口都必须过。少一条就是一条静默的漏。"""
        src = _bridge_src()
        self.assertEqual(src.count("_sc.audit("), 2, "群聊/私聊两条发送口没接")
        self.assertEqual(src.count("_scv.audit("), 1, "语音出口没接")

    def test_cached_line_puts_the_meaning_in_a_side_note(self):
        import qq_openclaw_bridge as b
        import vision_notes
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"DATA_DIR": tmp}), \
                mock.patch.object(vision_notes, "note_for", return_value={"intent": "x"}), \
                mock.patch.object(vision_notes, "card_line",
                                  return_value="对方想表达：流汗，尴尬"):
            line = b._cached_line({"time": "19:24:33", "user_id": "1", "nickname": "主人",
                                   "text": "[图片]", "image_urls": ["http://x/1.jpg"]}, "g1")
        self.assertNotIn("[图片：", line)
        self.assertIn("（图：对方想表达：流汗，尴尬）", line)


if __name__ == "__main__":
    unittest.main()