"""回归锁：这一轮从真实聊天里暴露出来的几件事。

用户贴了一段群聊，指出四个问题，这里锁住其中三个（好感承诺另有文件）：
  1) 别人发的表情她读不懂 —— 根因是「图只有在她要回复时才被看」，
     群上下文里只剩「图片数=1」；
  2) 她接不住自己刚问出去的话；
  3) 傲娇起手只剩「啧」。
"""
import io
import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b      # noqa: E402


def _read(name):
    with io.open(os.path.join(BOT, name), encoding="utf-8") as f:
        return f.read()


class ImageInContextTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="dlgfix_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        import vision_notes as vn
        vn._reset_cache()
        self.vn = vn
        self._cleanup = shutil.rmtree

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        self.vn._reset_cache()
        self._cleanup(self.dir, ignore_errors=True)

    def test_context_line_carries_the_meaning(self):
        url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=MON3TR"
        self.vn.save_note(url, {"mood": "困惑", "meme_guess": "在等一个答案"})
        m = {"time": "20:26:54", "user_id": "10086", "nickname": "烤苕皮！",
             "text": "[图片]", "image_urls": [url]}
        line = b._cached_line(m, "900000001")
        self.assertIn("困惑", line, "上下文里要带图的意思，不能只有「图片」")
        self.assertNotIn("图片数=1", line)

    def test_meaning_rides_along_when_there_is_also_text(self):
        url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=MON3TR2"
        self.vn.save_note(url, {"mood": "困惑", "meme_guess": "在等一个答案"})
        m = {"time": "20:26:59", "user_id": "10086", "nickname": "烤苕皮！",
             "text": "睡这么早吗", "image_urls": [url]}
        line = b._cached_line(m, "900000001")
        self.assertIn("睡这么早吗", line)
        self.assertIn("困惑", line)

    def test_unknown_image_falls_back_to_a_count(self):
        m = {"time": "20:26:54", "user_id": "10086", "nickname": "烤苕皮！",
             "text": "[图片]", "image_urls": ["https://x/none"]}
        self.assertIn("图片数=1", b._cached_line(m, "900000001"))

    def test_ingest_spawns_a_background_look(self):
        """消息一进来就要看图，不能等她回复完。

        2026-11-08：这条链路收进了 `_note_at_ingest`（群里做了、私聊漏了，
        私聊第一张表情因此永远只能带原图去回）。锁跟着改到两处：
        入库点必须调用这个共用入口，而这个入口必须真的把看图任务排出去。
        """
        src = _read("qq_openclaw_bridge.py")
        i = src.index("def record_group_message(")
        self.assertIn("_note_at_ingest", src[i:i + 2600], "群入库没走共用看图入口")
        j = src.index("def _note_at_ingest(")
        helper = src[j:j + 1200]
        self.assertIn("_note_images_async", helper, "共用入口没安排看图")
        self.assertIn("_spawn(", helper, "共用入口没把任务发出去")

    def test_describe_uses_the_repairing_parser(self):
        src = _read("qq_openclaw_bridge.py")
        i = src.index("async def _describe_image")
        block = src[i:i + 3200]
        self.assertIn("parse_note", block, "还用老的正则解析，截断就全丢")
        self.assertNotIn("json.loads(txt[a:b + 1])", block)

    def test_vision_gets_enough_tokens_to_finish_json(self):
        src = _read("qq_openclaw_bridge.py")
        i = src.index("async def _describe_image")
        # 函数体这几年长了一截（校准命中、复用旧笔记都在前面），
        # 再按固定 2000 字取窗口就会把真正那行视觉调用切掉。取到下一个函数头为止。
        j = src.find("\nasync def ", i + 10)
        block = src[i:(j if j > i else i + 4000)]
        self.assertIn("max_tokens=700", block, "400 个 token 会把 JSON 从中间截断")


class SelfContinuityTests(unittest.TestCase):
    def test_context_tells_her_to_continue_her_own_thread(self):
        src = _read("qq_openclaw_bridge.py")
        self.assertIn("接着自己的话往下说", src)
        self.assertIn("别否认自己刚说过", src)


class InterjectionTests(unittest.TestCase):
    def test_runtime_offers_a_palette(self):
        # 调色板是平台机制（防口癖复读），住在 bridge；人格包可另给覆盖。
        src = _read("qq_openclaw_bridge.py")
        for word in ("欸？", "唉~", "哎", "诶嘿~", "哼", "哼哼~"):
            self.assertIn(word, src, "runtime 调色板缺「%s」" % word)

    def test_runtime_rotates_when_she_repeats_one(self):
        old = list(b._OPEN_HISTORY)
        try:
            b._OPEN_HISTORY.clear()
            for _ in range(3):
                b._OPEN_HISTORY.append("啧，")
            hint = b._open_hint()
            self.assertIn("啧", hint)
            self.assertIn("换一个", hint)
            self.assertIn("欸", hint, "要给出可替换的词，否则她只会换标点")
        finally:
            b._OPEN_HISTORY.clear()
            b._OPEN_HISTORY.extend(old)

    def test_open_record_ignores_punctuation(self):
        old = list(b._OPEN_HISTORY)
        try:
            b._OPEN_HISTORY.clear()
            b._open_record("啧，笨蛋主人")
            b._open_record("啧。才不是")
            self.assertTrue(b._OPEN_HISTORY)
            self.assertTrue(all("，" not in x and "。" not in x
                                for x in b._OPEN_HISTORY))
        finally:
            b._OPEN_HISTORY.clear()
            b._OPEN_HISTORY.extend(old)


class VisionBudgetTests(unittest.TestCase):
    """入库即看图以后节奏变成「来一张看一张」，得有个小时额度兜着。"""

    def setUp(self):
        b._VISION_HOUR.update({"hour": -1, "n": 0})

    def tearDown(self):
        b._VISION_HOUR.update({"hour": -1, "n": 0})

    def test_stops_after_the_limit(self):
        from unittest.mock import patch
        with patch.object(b.time, "time", return_value=1000.0):
            take = b._vision_budget_ok(limit=2)
            self.assertTrue(take())
            self.assertTrue(take())
            self.assertFalse(take())

    def test_next_hour_gets_a_fresh_budget(self):
        from unittest.mock import patch
        with patch.object(b.time, "time", return_value=1000.0):
            take = b._vision_budget_ok(limit=1)
            self.assertTrue(take())
            self.assertFalse(take())
        with patch.object(b.time, "time", return_value=1000.0 + 3600):
            take2 = b._vision_budget_ok(limit=1)
            self.assertTrue(take2(), "换了一个小时要重新有额度")


class StaleMemoryTests(unittest.TestCase):
    def test_old_affection_numbers_are_not_fed_back(self):
        text = ("- 她喜欢糖炒栗子\n"
                "- [主人与好感度] 清漪查看好感度时称主人为六十五分\n"
                "- [好感度] 群里好感度分数：主人70分最高，阿戊18分\n"
                "- 阿甲最近在玩明日方舟")
        kept = b._drop_stale_affection(text)
        self.assertIn("糖炒栗子", kept)
        self.assertIn("明日方舟", kept)
        self.assertNotIn("六十五分", kept)
        self.assertNotIn("70分", kept)

    def test_ordinary_notes_survive(self):
        text = "- 她说三分钟就到\n- 好感是慢慢养出来的"
        self.assertEqual(b._drop_stale_affection(text), text)


if __name__ == "__main__":
    unittest.main()