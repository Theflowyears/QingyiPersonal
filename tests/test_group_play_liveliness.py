"""她在群里几乎不说话了——把今晚收紧的闸门松回可用的位置。

主人问：「她最近不怎么在群里说话，是我的错觉吗？我还是喜欢她以前那种经常能接话的。」

不是错觉。账：

    data/persona/journal.jsonl（她每次开口一条）
      11-04 群 328 条   11-05 群 294 条   11-06 群 241 条   11-07 群 92 条   11-08（到 01:05）3 条

    logs/Bot.log 里 [会话裁决]（21:31 那批上线以来，共 30 次判定）
      reply=True  0 次
      reply=False 15 次
      skip=image_only 6 次、skip=无新问题/其它 9 次

两个原因都是我今晚造成的：
  ① `group_candidate` 把「只有一张表情」整条判成 image_only 直接跳过，不交给模型判 ——
     于是群里斗图她全程闭嘴（主人的原话是喜欢她接话，不是喜欢她在旁边看着）；
  ② 裁决提示词被写成「确有新内容值得补充才回」，模型顺着把玩梗、共同话题也判成不相干。

修的方向不是「什么都接」：第三人不相关的对话仍然不抢（那是主人昨天骂的反例），
但斗图、玩梗、聊她认识的人这种「群里的共同场面」要能接一句。
并且憋了几条没说话时，表情也算可接的场合。
"""
import os
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import conversation_plan as cp        # noqa: E402

SELF = "800000001"
OTHER = "1000000001"


def human(text, seq=1, images=None, uid=OTHER, at_targets=None, reply_to=None):
    return {"user_id": uid, "nickname": "烤苕皮", "text": text, "seq": seq,
            "is_bot": False, "ts": time.time(), "group_id": "900000001",
            "image_urls": images or [], "at_targets": at_targets or [],
            "reply_to": reply_to}


def bot(text, seq=0):
    return {"user_id": SELF, "nickname": "清漪", "text": text, "seq": seq,
            "is_bot": True, "ts": time.time(), "group_id": "900000001",
            "image_urls": [], "at_targets": [], "reply_to": None}


class StickerTurnTests(unittest.TestCase):
    def test_sticker_only_is_still_not_a_default_topic(self):
        """默认仍然不接：一条表情不等于开了新话题（这是昨天修的反例）。"""
        got = cp.group_candidate([human("[图片]", images=["a.gif"])], SELF)
        self.assertFalse(got["allowed"])
        self.assertEqual(got["reason"], "image_only")

    def test_sticker_turn_is_offerable_when_she_has_been_quiet(self):
        """憋久了，斗图就是可接的场合 —— 交给模型判，而不是一律跳过。"""
        got = cp.group_candidate([human("[图片]", images=["a.gif"])], SELF,
                                 allow_image_play=True)
        self.assertTrue(got["allowed"], "表情轮被硬跳过，她整晚没法参与斗图")
        self.assertEqual(got["reason"], "image_play")

    def test_offered_sticker_turn_still_respects_third_party_turns(self):
        rows = [human("清清你快去哄哄主人", at_targets=["999"])]
        got = cp.group_candidate(rows, SELF, allow_image_play=True)
        self.assertFalse(got["allowed"])
        self.assertEqual(got["reason"], "other_recipient")


class ArbiterWordingTests(unittest.TestCase):
    def setUp(self):
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        # 锚点用「她是群友」那句：以前锚在「你只判断是否有必要开口」上，
        # 而那句话本身在 11-08 被换掉了（问「有没有必要」，模型永远答没必要）。
        i = src.index('"她是这个群的群友')
        self.prompt = src[i:i + 1100]

    def test_prompt_allows_group_play(self):
        # 老话术「确有新内容值得补充才回」把玩梗也判成不相干：30 次判定 0 次开口
        self.assertIn("斗图", self.prompt)
        self.assertIn("玩梗", self.prompt)
        self.assertNotIn("确有新内容值得补充才回", self.prompt)

    def test_prompt_still_blocks_third_party_exchanges(self):
        # 昨天主人骂的就是这个：别人之间的对话她不许抢
        self.assertIn("两个人", self.prompt)

    def test_arbitration_speak_key_is_not_a_blanket_duty_to_stay_quiet(self):
        text = cp.ARBITRATION["speak"]
        self.assertIn("斗图", text)
        self.assertIn("不是", text)          # 「机会不是义务」这条要留着


class CallerWiringTests(unittest.TestCase):
    def test_evaluation_passes_the_image_play_flag(self):
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        i = src.index("candidate = cp.group_candidate(")
        block = src[i - 1200:i + 700]
        self.assertIn("allow_image_play", block, "调用方没把「憋久了可以接表情」传进去")

    def test_play_gate_is_visible_in_the_log(self):
        # 沉默的闸门必须能看见：跳过了要说为什么，接了要说走的是哪条
        src = (ROOT / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        self.assertIn("skip=%s", src)


if __name__ == "__main__":
    unittest.main()
