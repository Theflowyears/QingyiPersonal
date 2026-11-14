"""读图的目的：先懂「这张图在说什么」，再回应那个意思。

用户原话（生产实测 23:46~23:49）：
    「读图是为了分析图片的含义和可能包含的情感，然后做出对背后含义的回复，
      而不是让你描述图片本身的情况！」

笔记本身没坏（data/memory/vision_notes.json 里 mood/meme_guess 都在），
坏的是「产出含义」这一步没被要求：老提示词让模型先列 3~6 个物体，
她拿到手就是一张画面清单，回出来的自然是「吃西瓜还带角，看着就热」。

这几条锁钉住四件事：
  1. 提示词的主字段换成 kind + intent + mood，表情包明令不列画面；
  2. 只写出 intent 的笔记也算「看懂了」（旧规则会把这种笔记整条丢掉）；
  3. 进上下文的一行摘要以 intent 打头，老笔记（只有 mood）不能退化；
  4. 表情包的「含义卡」不含物体清单，且照片/截图不发卡（那种要看原图）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot"))

import vision_notes as vn  # noqa: E402


MEME = {"kind": "表情包", "intent": "表面淡定其实已经崩了", "mood": "强撑",
        "text_in_image": "", "objects": ["卡通人物", "猫耳", "咖啡杯", "火焰"],
        "scene": "表情包", "meme_guess": "内心崩溃表面淡定"}

PHOTO = {"kind": "照片", "intent": "给你看看我买的", "mood": "得意",
         "text_in_image": "", "objects": ["塑料袋", "西瓜", "电动车"], "scene": "街边"}

OLD = {"mood": "呆萌治愈", "meme_guess": "", "objects": ["Q版白发女孩", "西瓜"],
       "scene": "二次元头像插画"}


class PromptShapeTests(unittest.TestCase):
    def test_prompt_leads_with_intent_not_object_list(self):
        p = vn.VISION_PROMPT
        self.assertIn('"intent"', p)
        self.assertIn('"kind"', p)
        self.assertIn('"mood"', p)
        # 「先列 3~6 个东西」是老提示词的主字段，含义优先以后不该再当第一要求
        self.assertNotIn('"objects":["3到6个具体的东西"]', p)
        self.assertIn("表情", p)          # 明令表情包别写画面清单
        self.assertIn("看不清", p)        # 不许编

    def test_prompt_still_keeps_ocr_field(self):
        # 表情包上的字就是内容本身，去掉就没法接「已思考（用时13秒）」这类梗
        self.assertIn('"text_in_image"', vn.VISION_PROMPT)


class ParseTests(unittest.TestCase):
    def test_intent_only_note_is_usable(self):
        note = vn.parse_note('{"kind":"表情包","intent":"想被哄一下","mood":"","'
                             'text_in_image":"","objects":[]}')
        self.assertIsNotNone(note)
        self.assertEqual(note.get("intent"), "想被哄一下")

    def test_parse_keeps_kind_and_intent(self):
        raw = ('{"kind":"表情包","intent":"等一个回答","mood":"困惑",'
               '"text_in_image":"","objects":[]}')
        note = vn.parse_note(raw)
        self.assertEqual(note.get("kind"), "表情包")
        self.assertEqual(note.get("intent"), "等一个回答")

    def test_truncated_json_still_recovers_intent(self):
        body = ('{"kind":"表情包","intent":"表面淡定其实已经崩了","mood":"强撑",'
                '"text_in_image":"","objects":["卡通人')
        note = vn.parse_note(body)
        self.assertIsNotNone(note)
        self.assertEqual(note.get("intent"), "表面淡定其实已经崩了")

    def test_old_note_without_new_fields_is_still_usable(self):
        self.assertTrue(vn._usable_note({"mood": "呆萌", "objects": []}))


class MeaningTests(unittest.TestCase):
    def test_short_meaning_leads_with_intent(self):
        self.assertTrue(vn.short_meaning(MEME).startswith("表面淡定其实已经崩了"))

    def test_short_meaning_old_note_falls_back_to_mood(self):
        self.assertEqual(vn.short_meaning(OLD), "呆萌治愈")

    def test_short_meaning_empty_note(self):
        self.assertEqual(vn.short_meaning({"kind": "照片"}), "")
        self.assertEqual(vn.short_meaning(None), "")


class MemeCardTests(unittest.TestCase):
    def test_card_carries_meaning_and_forbids_caption(self):
        card = vn.meme_card(MEME)
        self.assertIn("表面淡定其实已经崩了", card)
        self.assertIn("强撑", card)
        self.assertIn("不要描述画面", card)

    def test_card_never_lists_objects(self):
        card = vn.meme_card(MEME)
        for word in ("咖啡杯", "火焰", "猫耳"):
            self.assertNotIn(word, card)

    def test_card_keeps_text_written_on_the_image(self):
        note = dict(MEME, text_in_image="已思考（用时13秒）")
        self.assertIn("已思考", vn.meme_card(note))

    def test_photo_gets_no_card(self):
        # 照片/截图是信息载体，那种必须给她看原图，不能靠一句话糊弄
        self.assertEqual(vn.meme_card(PHOTO), "")

    def test_note_without_meaning_gets_no_card(self):
        self.assertEqual(vn.meme_card({"kind": "表情包"}), "")

    def test_legacy_note_is_symbol_only_when_scene_says_so(self):
        legacy = dict(scene="二次元表情包", mood="呆萌无语", meme_guess="敷衍回答", objects=[])
        self.assertTrue(vn.meme_card(legacy))
        self.assertEqual(vn.meme_card(OLD), "")


class CardLineTests(unittest.TestCase):
    def test_symbol_line_names_the_meaning_not_the_picture(self):
        line = vn.card_line(MEME)
        self.assertIn("表面淡定其实已经崩了", line)
        self.assertIn("对方", line)
        self.assertNotIn("咖啡杯", line)

    def test_legacy_note_without_kind_still_gets_a_line(self):
        # 老笔记没有 intent，只有情绪和梗；上下文不能因此退回空白「图片数=1」
        legacy = dict(scene="二次元表情包", mood="呆萌无语", meme_guess="敷衍回答", objects=[])
        self.assertIn("呆萌无语", vn.card_line(legacy))

    def test_line_stays_short_enough_for_group_context(self):
        self.assertLessEqual(len(vn.card_line(MEME)), 34)

    def test_record_line_keeps_the_content(self):
        # 照片/截图里装着对方要给她看的东西，那几个物体是后面接话的本钱
        line = vn.card_line(PHOTO)
        self.assertIn("西瓜", line)
        self.assertIn("给你看看我买的", line)

    def test_empty_note_gives_empty_line(self):
        self.assertEqual(vn.card_line({}), "")
        self.assertEqual(vn.card_line(None), "")


class GroupRenderTests(unittest.TestCase):
    def test_group_line_leads_with_intent(self):
        # 笔记没有 session 时按「不进群」处理（fail-closed），所以这条要给群的归属
        got = vn.render_for_prompt(dict(MEME, key="k1", visibility="group"), scene="group")
        self.assertIn("表面淡定其实已经崩了", got)
        self.assertNotIn("咖啡杯", got)

    def test_group_line_never_carries_an_unowned_note(self):
        self.assertEqual(vn.render_for_prompt(dict(MEME, key="k2"), scene="group"), "")


if __name__ == "__main__":
    unittest.main()
