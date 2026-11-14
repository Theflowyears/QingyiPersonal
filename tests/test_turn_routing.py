import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = str(Path(__file__).resolve().parents[1] / "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import conversation_plan as cp

SELF = "9"
AYAN = "9000000003"
FOX = "9000000004"
MASTER = "1000000001"
ALIASES = {"阿甲": AYAN, "阿戊": FOX, "主人": MASTER, "清漪": SELF}


def human(seq, uid, text="", **extra):
    return {"seq": seq, "user_id": uid, "text": text, "is_bot": False,
            "ts": 1000, "group_id": "900000001", **extra}


class GroupCandidateTests(unittest.TestCase):
    def setUp(self):
        self.route = getattr(cp, "group_candidate", None)
        self.assertTrue(callable(self.route), "group_candidate is not implemented")

    def candidate(self, rows, **kwargs):
        return self.route(rows, SELF, aliases=ALIASES, now=1000, **kwargs)

    def test_empty_or_bot_only_has_no_candidate(self):
        for rows in ([], [human(1, SELF, "刚才说过了", is_bot=True)],
                     [human(1, SELF, "自己的消息但没打标")]):
            with self.subTest(rows=rows):
                result = self.candidate(rows)
                self.assertEqual(result, {
                    "allowed": False, "reason": "no_human", "rows": [],
                    "speaker_id": "", "text": "", "seq": 0, "reply_to": None,
                })

    def test_last_human_owns_candidate_and_original_group_id(self):
        rows = [human(1, MASTER, "帮我写个总结"),
                human("2", int(AYAN), "今晚月亮好亮", nickname="阿戊",
                      message_id="message-2")]
        result = self.candidate(rows)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["speaker_id"], AYAN)
        self.assertEqual(result["seq"], 2)
        self.assertEqual(result["text"], "今晚月亮好亮")
        self.assertEqual(result["reply_to"], "message-2")
        self.assertEqual(result["rows"], [rows[1]])
        self.assertEqual(result["rows"][0]["group_id"], "900000001")

    def test_plain_text_is_an_opportunity_not_a_forced_relevance_decision(self):
        result = self.candidate([human(1, FOX, "今天看到了好多云")])
        self.assertTrue(result["allowed"])
        self.assertTrue(result["reason"])

    def test_stale_humans_are_excluded_but_missing_timestamps_are_kept(self):
        rows = [human(1, FOX, "旧消息", ts=819),
                human(2, AYAN, "刚好在窗口内", ts=820)]
        self.assertEqual(self.candidate(rows)["rows"], [rows[1]])
        self.assertFalse(self.candidate([rows[0]])["allowed"])
        untimed = human(3, AYAN, "没有时间戳")
        untimed.pop("ts")
        self.assertTrue(self.candidate([untimed])["allowed"])
        self.assertFalse(self.candidate([rows[1]], max_age=60)["allowed"])

    def test_explicit_other_user_targets_exclude_even_master_requests(self):
        for metadata in ({"at_targets": [int(FOX)]},
                         {"at_targets": [FOX, AYAN]},
                         {"reply_to_user": int(AYAN)}):
            with self.subTest(metadata=metadata):
                row = human(1, MASTER, "把这几条逐条转发给我一下呗", **metadata)
                result = self.candidate([row])
                self.assertFalse(result["allowed"])
                self.assertTrue(result["reason"])
                self.assertEqual(result["speaker_id"], MASTER)

    def test_leading_third_party_praise_and_requests_are_not_for_bot(self):
        for text in ("阿戊棒棒！", "阿甲私聊把这几条逐条转发给我一下呗",
                     "阿戊，真棒！", "@阿戊 真厉害"):
            with self.subTest(text=text):
                result = self.candidate([human(1, MASTER, text)])
                self.assertFalse(result["allowed"])
                self.assertTrue(result["reason"])

    def test_aliases_are_configuration_not_guessed_from_names(self):
        row = human(1, MASTER, "阿戊棒棒！")
        self.assertTrue(self.route([row], SELF, now=1000)["allowed"])
        result = self.route([row], SELF, aliases={"阿戊": SELF}, now=1000)
        self.assertTrue(result["allowed"])

    def test_mentions_of_third_party_inside_a_sentence_do_not_exclude(self):
        for text in ("我觉得阿戊棒棒！", "你觉得阿甲最近怎么样？", "阿戊今天上线了"):
            with self.subTest(text=text):
                self.assertTrue(self.candidate([human(1, MASTER, text)])["allowed"])

    def test_actual_bot_direction_allows_questions_about_fox(self):
        for text, metadata in (
            ("@清漪 阿戊怎么样？", {"at_targets": [int(SELF)]}),
            ("阿戊最近怎么样？", {"reply_to_user": int(SELF)}),
            ("清漪，阿戊棒不棒？", {}),
            ("阿戊棒棒！@清漪 你怎么看？", {}),
            ("[CQ:at,qq=9] 阿戊怎么样？", {}),
            ("阿戊棒棒！", {"at_targets": [FOX, SELF]}),
        ):
            with self.subTest(text=text, metadata=metadata):
                self.assertTrue(self.candidate([human(1, MASTER, text, **metadata)])["allowed"])

    def test_pure_image_last_human_requires_explicit_direction(self):
        for text in ("", "[图片]", "[图片][图片]", "[CQ:image,file=cat.jpg]"):
            with self.subTest(text=text):
                row = human(2, FOX, text, image_urls=["cat.jpg"])
                self.assertFalse(self.candidate([human(1, FOX, "看这个"), row])["allowed"])
                self.assertTrue(self.candidate([{**row, "at_targets": [SELF]}])["allowed"])
                self.assertTrue(self.candidate([{**row, "reply_to_user": SELF}])["allowed"])
        self.assertTrue(self.candidate([human(3, FOX, "清漪，[图片]")])["allowed"])

    def test_consecutive_same_author_fragments_are_joined_once(self):
        rows = [human(1, AYAN, "先看这几条"), human(2, AYAN, "第二条"),
                human(3, AYAN, "第二条"), human(4, AYAN, "最后一条", reply_to="quoted-4")]
        before = copy.deepcopy(rows)
        result = self.candidate(rows)
        self.assertEqual(result["text"], "先看这几条\n第二条\n最后一条")
        self.assertEqual([row["seq"] for row in result["rows"]], [1, 3, 4])
        self.assertEqual(result["seq"], 4)
        self.assertEqual(result["reply_to"], "quoted-4")
        self.assertEqual(rows, before)

    def test_fragments_stop_at_bot_other_human_or_stale_row(self):
        for barrier in (human(2, SELF, "已接话", is_bot=True),
                        human(2, MASTER, "另一人的话"),
                        human(2, AYAN, "过期的片段", ts=800)):
            with self.subTest(barrier=barrier):
                rows = [human(1, AYAN, "不能接上"), barrier, human(3, AYAN, "新的一轮")]
                self.assertEqual(self.candidate(rows)["rows"], [rows[2]])

    def test_only_last_four_fragments_are_considered(self):
        rows = [human(i, AYAN, "片段%d" % i) for i in range(1, 7)]
        self.assertEqual([row["seq"] for row in self.candidate(rows)["rows"]], [3, 4, 5, 6])

    def test_last_directed_fragment_overrides_earlier_direction(self):
        for last in (human(2, MASTER, "阿戊棒棒！"),
                     human(2, MASTER, "把材料发给我", at_targets=[FOX]),
                     human(2, MASTER, "私聊发给我", reply_to_user=AYAN)):
            with self.subTest(last=last):
                rows = [human(1, MASTER, "清漪，看看这些", at_targets=[SELF]), last]
                self.assertFalse(self.candidate(rows)["allowed"])
        rows = [human(1, MASTER, "阿戊棒棒！"), human(2, MASTER, "清漪，你怎么看？")]
        self.assertTrue(self.candidate(rows)["allowed"])

    def test_inline_explicit_at_alias_is_addressing_not_plain_name_mention(self):
        self.assertFalse(self.candidate([human(1, MASTER, "请 @阿戊 私聊发给我")])["allowed"])

    def test_group_ids_are_never_inferred_or_crossed(self):
        rows = [human(1, AYAN, "另一个群", group_id="111"), human(2, AYAN, "当前群")]
        self.assertEqual(self.candidate(rows)["rows"], [rows[1]])
        row = human(3, AYAN, "没有群号")
        row.pop("group_id")
        self.assertNotIn("group_id", self.candidate([row])["rows"][0])


class ReplySelectionTests(unittest.TestCase):
    def setUp(self):
        self.select = getattr(cp, "select_reply", None)
        self.assertTrue(callable(self.select), "select_reply is not implemented")
        self.rows = [human(11, AYAN, "第一条"), human(12, AYAN, "第二条")]

    def test_valid_json_proposal_selects_the_actual_human_row(self):
        self.assertIs(self.select({"reply": True, "target_seq": 11}, self.rows), self.rows[0])

    def test_json_integer_selects_matching_cached_string_sequence(self):
        row = human("14", FOX, "缓存中的序号是字符串")
        self.assertIs(self.select({"reply": True, "target_seq": 14}, [row]), row)

    def test_decline_or_invented_sequence_has_no_fallback(self):
        for proposal in ({"reply": False, "target_seq": 11},
                         {"reply": True, "target_seq": 99},
                         {"reply": True}, {}, None):
            with self.subTest(proposal=proposal):
                self.assertIsNone(self.select(proposal, self.rows))

    def test_json_boolean_and_sequence_types_are_validated(self):
        for proposal in ({"reply": "false", "target_seq": 11},
                         {"reply": 1, "target_seq": 11},
                         {"reply": True, "target_seq": True},
                         {"reply": True, "target_seq": 11.5},
                         {"reply": True, "target_seq": "11"}):
            with self.subTest(proposal=proposal):
                self.assertIsNone(self.select(proposal, self.rows))

    def test_bot_rows_and_rows_outside_selection_cannot_be_targets(self):
        rows = [human(13, SELF, "自己的话", is_bot=True)]
        self.assertIsNone(self.select({"reply": True, "target_seq": 13}, rows))
        self.assertIsNone(self.select({"reply": True, "target_seq": 11}, self.rows[1:]))


class RepeatReplyTests(unittest.TestCase):
    def setUp(self):
        self.repeat = getattr(cp, "repeat_reply", None)
        self.assertTrue(callable(self.repeat), "repeat_reply is not implemented")

    def test_whitespace_and_punctuation_only_variations_are_duplicates(self):
        self.assertIs(self.repeat("  阿戊，棒棒！\n", ["阿戊棒棒。"]), True)
        self.assertIs(self.repeat("收到！", "收到。"), True)

    def test_each_previous_bubble_and_whole_output_are_compared(self):
        previous = ["先把资料整理好。\n然后逐条发给阿甲。", "我先去看看"]
        self.assertTrue(self.repeat("然后逐条发给阿甲！", previous))
        self.assertTrue(self.repeat("先把资料整理好！然后逐条发给阿甲", previous))

    def test_long_near_identical_reply_with_meaningful_prefix_is_duplicate(self):
        self.assertTrue(self.repeat("这些资料我已经整理好了呀", ["这些资料我已经整理好了哦"]))
        self.assertFalse(self.repeat("呀这些资料我已经整理好了", ["哦这些资料我已经整理好了"]))

    def test_short_near_matches_are_not_suppressed(self):
        self.assertFalse(self.repeat("阿戊棒棒呀", ["阿戊棒棒哦"]))
        self.assertFalse(self.repeat("abcdefgh", ["abcdefgi"]))

    def test_similar_replies_with_changed_numbers_are_distinct(self):
        for answer, previous in (
            ("阿甲要的材料一共是12条，已经整理好了", "阿甲要的材料一共是13条，已经整理好了"),
            ("明天的室外温度大概是20.5度，记得加衣服", "明天的室外温度大概是2.05度，记得加衣服"),
            ("这次计算得到的最终数值是-12，先记下来", "这次计算得到的最终数值是12，先记下来"),
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.repeat(answer, [previous]))

    def test_similar_replies_with_changed_facts_are_distinct(self):
        for answer, previous in (
            ("阿戊今天已经拿到限定角色了，我替他高兴", "阿戊今天还没拿到限定角色呢，我替他高兴"),
            ("明天下午三点去图书馆门口集合，别迟到了", "明天下午四点去图书馆门口集合，别迟到了"),
            ("我刚刚查到阿戊喜欢红色，先记住这个偏好", "我刚刚查到阿戊喜欢蓝色，先记住这个偏好"),
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.repeat(answer, [previous]))

    def test_added_casual_tail_does_not_evade_near_duplicate_detection(self):
        self.assertTrue(self.repeat("这些资料我已经整理好了，放心哦", ["这些资料我已经整理好了"]))
        self.assertTrue(self.repeat("我记得你上次说过那件事情呢", ["我还记得你上次说过那件事情呢"]))

    def test_first_common_characters_must_not_be_only_symbols(self):
        self.assertFalse(self.repeat("++++呀这些资料我已经整理好了", ["++++哦这些资料我已经整理好了"]))

    def test_empty_answer_or_empty_history_is_not_duplicate(self):
        for answer, previous in (("", ["旧回复"]), ("！", ["？"]),
                                 (None, ["旧回复"]), ("新回复", []), ("新回复", None)):
            with self.subTest(answer=answer, previous=previous):
                self.assertIs(self.repeat(answer, previous), False)


class PrivatePlanTests(unittest.TestCase):
    """私聊长度：1 句 50% / 2 句 30% / 3 句 20%，永不掷出沉默。"""

    def roll(self, value):
        """把长度骰子钉死成某个值，只看 plan() 怎么选。"""
        return patch.object(cp, "roll_private_length", return_value=value)

    def test_private_rolls_its_own_dice_not_the_group_one(self):
        for scene in ("private", "whisper"):
            for group_roll in ("silent", "short", "long"):
                with self.subTest(scene=scene, group_roll=group_roll), \
                        patch.object(cp, "roll_length", return_value=group_roll), \
                        self.roll("mid"):
                    self.assertEqual(cp.plan("今天有点累", scene=scene)["length"], "mid")

    def test_private_dice_never_returns_silent(self):
        seen = {cp.roll_private_length() for _ in range(400)}
        self.assertTrue(seen <= {"short", "mid", "long"}, sorted(seen))
        self.assertEqual(seen, {"short", "mid", "long"})

    def test_private_distribution_is_50_30_20(self):
        counts = {"short": 0, "mid": 0, "long": 0}
        for value in (0.0, 0.49, 0.50, 0.79, 0.80, 0.99):
            with patch.object(cp.random, "random", return_value=value):
                counts[cp.roll_private_length()] += 1
        self.assertEqual(counts, {"short": 2, "mid": 2, "long": 2})
        total = 20000
        rolled = [cp.roll_private_length() for _ in range(total)]
        share = {k: rolled.count(k) / total for k in counts}
        self.assertAlmostEqual(share["short"], 0.50, delta=0.03)
        self.assertAlmostEqual(share["mid"], 0.30, delta=0.03)
        self.assertAlmostEqual(share["long"], 0.20, delta=0.03)

    def test_private_length_text_names_the_sentence_count(self):
        for tier, phrase in (("short", "一句话"), ("mid", "两句"), ("long", "三句")):
            with self.subTest(tier=tier), self.roll(tier):
                pl = cp.plan("今天有点累", scene="private")
                self.assertIn(phrase, pl["length_text"])
                self.assertEqual(pl["sentences"], {"short": 1, "mid": 2, "long": 3}[tier])

    def test_image_only_private_floors_at_two_sentences(self):
        # 只发图时一句话打发＝没看图，和「先懂含义再回应」冲突。
        for tier in ("short", "mid", "long"):
            with self.subTest(tier=tier), self.roll(tier):
                self.assertGreaterEqual(
                    cp.plan("", scene="private", has_image=True)["sentences"], 2)

    def test_real_tasks_and_explicit_long_requests_still_take_precedence(self):
        for text, kwargs in (("帮我写个总结", {}), ("详细介绍这本书", {}),
                             ("接着讲", {"long_mode": True})):
            with self.subTest(text=text):
                self.assertEqual(cp.plan(text, scene="private", **kwargs)["length"], "long")

    def test_intimate_and_flirt_profiles_keep_their_own_format(self):
        for kwargs in ({"intimate": "soft"}, {"flirt": True}):
            with self.subTest(kwargs=kwargs), self.roll("short"):
                pl = cp.plan("想你了", scene="private", **kwargs)
                self.assertNotIn("一句话", pl["length_text"])
                self.assertIn("专用短消息格式", pl["length_text"])

    def test_group_length_still_uses_existing_roll(self):
        with patch.object(cp, "roll_length", return_value="short"):
            self.assertEqual(cp.plan("今晚月亮真亮", scene="group")["length"], "short")

    def test_rendered_arbitration_does_not_force_reply_when_master_addresses_others(self):
        prompt = cp.render(cp.plan("阿戊棒棒！", scene="group"))
        self.assertIn("私聊", prompt)
        self.assertIn("真正", prompt)
        self.assertIn("机会", prompt)
        self.assertIn("不是命令", prompt)
        self.assertNotIn("主人在说话时必须接", prompt)


if __name__ == "__main__":
    unittest.main()
