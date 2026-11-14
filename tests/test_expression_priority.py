# -*- coding: utf-8 -*-
import os,sys,unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"bot"))
import qq_openclaw_bridge as b
import conversation_plan as cp

class ExpressionPriorityTests(unittest.TestCase):
    def test_active_emotion_suppresses_random_mood(self):
        with mock.patch("expression_gate.gate_status",return_value={"active":True}), \
             mock.patch.object(b.random,"random",return_value=0.0) as rnd:
            self.assertEqual(b._mood_hint(),"")
            rnd.assert_not_called()

    def test_inactive_emotion_keeps_existing_surface_rhythm(self):
        with mock.patch("expression_gate.gate_status",return_value={"active":False}):
            self.assertIn("表达节奏参考",b._mood_hint())

    def test_fact_arbitration_precedes_style(self):
        text=cp.render(cp.plan("那不是你在嫌热吗",scene="group"))
        self.assertLess(text.index("事实与归属"),text.index("这一轮 · 怎么说话"))

if __name__=="__main__":unittest.main()