# -*- coding: utf-8 -*-
"""C06 灰度回归锁：默认关、人工签字+前置全过才开、可撤回、不翻影子静态锁。"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import expression_gate as eg
import expression_plan as ep

EMO = {"valence": 0.5, "arousal": 0.5, "control": 0.5, "attention": 0.1}


class GateTests(unittest.TestCase):
    def setUp(self):
        eg.invalidate()
        self.addCleanup(eg.invalidate)

    def _patch(self, granted, ready):
        return mock.patch.multiple(
            "ops_channel", stage3_signoff=lambda: {"granted": granted}), \
            mock.patch("phase_audit.audit_readiness",
                       return_value={"phase25": {"status": "review_ready" if ready else "blocked"}})

    def test_default_off_without_signoff(self):
        p1, p2 = self._patch(False, True)
        with p1, p2:
            g = eg.gate_status()
            self.assertFalse(g["active"])
            self.assertEqual(eg.tone_block(), "")

    def test_off_when_gates_not_ready(self):
        p1, p2 = self._patch(True, False)
        with p1, p2:
            self.assertFalse(eg.gate_status()["active"])
            self.assertEqual(eg.tone_block(), "")

    def test_on_only_with_signoff_and_ready(self):
        p1, p2 = self._patch(True, True)
        with p1, p2, mock.patch("emotion_shadow.status", return_value={"state": EMO}):
            g = eg.gate_status()
            self.assertTrue(g["active"])
            block = eg.tone_block()
            self.assertIn("灰度", block)
            self.assertIn("valence=high", block)
            self.assertLess(len(block), 200)  # 有界

    def test_revoke_turns_off(self):
        p1, p2 = self._patch(True, True)
        with p1, p2, mock.patch("emotion_shadow.status", return_value={"state": EMO}):
            self.assertTrue(eg.gate_status()["active"])
        eg.invalidate()
        p1, p2 = self._patch(False, True)
        with p1, p2:
            self.assertFalse(eg.gate_status()["active"])
            self.assertEqual(eg.tone_block(), "")


class ToneDescriptorTests(unittest.TestCase):
    def test_absent_emotion_unavailable(self):
        self.assertFalse(ep.tone_descriptor({}, True)["available"])
        self.assertFalse(ep.tone_descriptor(None, True)["available"])

    def test_valid_emotion_bounded(self):
        t = ep.tone_descriptor(EMO, True)
        self.assertTrue(t["available"])
        self.assertFalse(t["calibrated"])
        self.assertEqual(set(t["descriptors"]), {"valence", "arousal", "control"})

    def test_continuity_fail_unavailable(self):
        self.assertFalse(ep.tone_descriptor(EMO, False)["available"])


class ShadowLockTests(unittest.TestCase):
    def test_gate_never_flips_shadow_expression_control(self):
        import emotion_shadow
        p1, p2 = None, None
        eg.invalidate()
        with mock.patch("ops_channel.stage3_signoff", return_value={"granted": True}), \
             mock.patch("phase_audit.audit_readiness",
                        return_value={"phase25": {"status": "review_ready"}}):
            eg.gate_status()
            eg.tone_block()
        self.assertFalse(emotion_shadow.status(limit=1)["expression_control"])


if __name__ == "__main__":
    unittest.main()