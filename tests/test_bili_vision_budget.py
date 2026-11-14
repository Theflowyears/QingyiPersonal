import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import bili_vision as bv  # noqa: E402


class VideoBudgetTests(unittest.TestCase):
    def test_every_duration_stays_within_token_budget(self):
        # 事故：fps=2 × 1800s = 3600 帧 ≈ 13 万 token。改为动态 fps 后必须 ≤ 配置预算。
        for dur in (60, 300, 600, 900, 1200, 1500, 1800, 3600):
            fps, frames = bv.plan_video_fps(dur)
            self.assertGreater(fps, 0, dur)
            self.assertLessEqual(fps, 2.0, dur)
            self.assertLessEqual(frames * bv.VIDEO_TOKENS_PER_FRAME, bv.VIDEO_TOKEN_BUDGET,
                                 (dur, fps, frames))

    def test_short_video_keeps_two_fps(self):
        fps, frames = bv.plan_video_fps(60)
        self.assertEqual(fps, 2.0)
        self.assertEqual(frames, 120)

    def test_long_video_gets_sparse_fps(self):
        fps, frames = bv.plan_video_fps(1800)
        max_frames = bv.VIDEO_TOKEN_BUDGET // bv.VIDEO_TOKENS_PER_FRAME
        self.assertLessEqual(fps, max(0.25, max_frames / 1800.0))
        self.assertLess(fps, 2.0)          # 长片必须比短片稀
        self.assertGreaterEqual(fps, bv.VIDEO_FPS_MIN)
        self.assertLessEqual(frames * bv.VIDEO_TOKENS_PER_FRAME, bv.VIDEO_TOKEN_BUDGET)

    def test_unknown_duration_is_safe(self):
        fps, frames = bv.plan_video_fps(0)
        self.assertLessEqual(frames * bv.VIDEO_TOKENS_PER_FRAME, bv.VIDEO_TOKEN_BUDGET)


if __name__ == '__main__':
    unittest.main()