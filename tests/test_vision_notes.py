"""回归锁：看图笔记。

线上实测过两个真问题，都在这里锁住：
  1) `key_of` 把 QQ 图片地址按「最后一段路径」当身份 —— 而 QQ 的地址全是
     `.../download?appid=...&fileid=...`，最后一段永远是 `download`，
     于是**所有 QQ 图片共用一个 key**，笔记互相覆盖；
  2) 结构化识图要求六字段 JSON，模型返回被截断 / 夹着解释，解析一失败整条笔记
     就丢了（现网 `data/memory/vision_notes.json` 一直不存在）。识图本身是好的
     （同一张图退回短描述时给出了「无语」），坏的只是解析。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import vision_notes as vn       # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vn_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        vn._reset_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        vn._reset_cache()
        shutil.rmtree(self.dir, ignore_errors=True)


class KeyTests(Base):
    def test_two_qq_stickers_have_different_keys(self):
        a = ("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid="
             "EhRBs3CsUWi6dX49e-kGtLC0i6pueRi")
        b = ("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid="
             "EhSzA_fgIR_GFhvs7M49BpxbJ7AwNxi")
        self.assertNotEqual(vn.key_of(a), vn.key_of(b),
                            "两张不同的 QQ 图不能共用一个 key")

    def test_same_sticker_with_refreshed_token_keeps_its_identity(self):
        # QQ 的 fileid 不变、其它 query 会变（签名/时间戳）
        a = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=ABC&t=1"
        b = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=ABC&t=2"
        self.assertEqual(vn.key_of(a), vn.key_of(b))

    def test_plain_file_token_is_stable(self):
        self.assertEqual(vn.key_of("64FD7400FC77CCF0F77AB0E91C02A968.jpg"),
                         vn.key_of("64FD7400FC77CCF0F77AB0E91C02A968.jpg"))


class ParseTests(Base):
    def test_clean_json(self):
        got = vn.parse_note('{"mood":"困惑","meme_guess":"等答案",'
                            '"objects":["小人","加载圈"],"text_in_image":""}')
        self.assertEqual(got["mood"], "困惑")
        self.assertEqual(got["meme_guess"], "等答案")
        self.assertEqual(got["objects"], ["小人", "加载圈"])

    def test_fenced_json(self):
        got = vn.parse_note('好的：\n```json\n{"mood":"无语","meme_guess":"服了"}\n```')
        self.assertEqual(got["mood"], "无语")

    def test_truncated_json_is_repaired(self):
        # 线上真实形态：token 用完了，最后一段没闭合
        raw = ('{"objects":["蓝发小人","加载圈"],"text_in_image":"","scene":"表情包",'
               '"people_action":"头顶转圈","mood":"困惑","meme_gu')
        got = vn.parse_note(raw)
        self.assertEqual(got["mood"], "困惑", "截断前已经给出的字段要保住")
        self.assertEqual(got["objects"], ["蓝发小人", "加载圈"])

    def test_prose_instead_of_json_still_yields_a_note(self):
        got = vn.parse_note("这看起来是一个困惑的表情，头顶有个加载圈，像是在等答案。")
        self.assertTrue(got, "模型不听格式也要留下观察")
        self.assertIn("困惑", got["mood"] + got["meme_guess"])

    def test_garbage_is_none(self):
        self.assertIsNone(vn.parse_note(""))
        self.assertIsNone(vn.parse_note("   \n  "))

    def test_model_error_text_is_not_an_observation(self):
        # 模型不可用时返回的是这些东西，不能当成「看过了」
        for raw in ("unavailable", "ERROR", "failed", "timeout"):
            with self.subTest(raw=raw):
                self.assertIsNone(vn.parse_note(raw))


class ShortMeaningTests(Base):
    def test_compact_line_for_context(self):
        s = vn.short_meaning({"mood": "困惑", "meme_guess": "在等一个答案"})
        self.assertIn("困惑", s)
        self.assertNotIn("\n", s)
        self.assertLessEqual(len(s), 24)

    def test_empty_note_gives_empty(self):
        self.assertEqual(vn.short_meaning({}), "")
        self.assertEqual(vn.short_meaning({"mood": "", "meme_guess": ""}), "")


class SaveTests(Base):
    def test_note_round_trip(self):
        url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=XYZ"
        k = vn.save_note(url, {"mood": "困惑", "meme_guess": "等答案"})
        self.assertTrue(k)
        got = vn.note_for(url)
        self.assertEqual(got["mood"], "困惑")

    def test_same_bytes_under_a_new_url_reuse_the_note(self):
        a = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=AAA"
        b = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=BBB"
        vn.save_note(a, {"mood": "困惑", "meme_guess": "等答案"}, content_sha="deadbeef")
        self.assertTrue(vn.note_by_sha("deadbeef"), "同内容要能找到旧笔记")
        vn.save_note(b, vn.note_by_sha("deadbeef"), content_sha="deadbeef")
        self.assertEqual(vn.note_for(b)["mood"], "困惑",
                         "同一张贴纸换了新地址，不该重新看一遍")


if __name__ == "__main__":
    unittest.main()