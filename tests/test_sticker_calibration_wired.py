"""校准要真的改变她的行为，不是改一份没人读的文件。

主人要的是「校准她的表情包认知」。查下来 `meaning` 这个字段以前只有人写、没人读：
她发图走 `pick_random()`（随机抽），收图走当场视觉猜 ——
也就是说面板上把「这张是无语不是惊讶」改对了，她下一轮照样抽错、照样猜错。
这三条锁把「读」这一头接上：

  1. 她选图时能看到库里的清单（校准过的排前面并标明「你校准过」）；
  2. 她可以点名要哪一张（`[[斗图:短id]]`），点不到才随机；
  3. 别人甩来一张她库里的表情，命中内容指纹且有主人校准过的含义时，
     直接按主人定的意思理解，不再当场视觉猜一次。
"""
import asyncio
import base64
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import sticker_store as ss            # noqa: E402
import qq_openclaw_bridge as b        # noqa: E402
import vision_notes as vn             # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
       b"\x08\x06\x00\x00\x00\xea\x0d\x1a\x0c\x00\x00\x00\x19IDATx\x9cc\xfc\xcf"
       b"\xc0\x00\x04\x00\xff\x00\x8cx\xa3\x0e\x1c\x00\x00\x00\x00IEND\xaeB`\x82")


def run_coro(coro):
    """在独立线程的新事件循环里跑一个协程。

    IsolatedAsyncioTestCase 已经占用了当前线程的 loop，测试里再
    `new_event_loop().run_until_complete()` 会直接 RuntimeError。
    """
    box = {}

    def go():
        loop = asyncio.new_event_loop()
        try:
            box["v"] = loop.run_until_complete(coro)
        finally:
            loop.close()

    t = threading.Thread(target=go)
    t.start()
    t.join(30)
    if "v" not in box:
        raise AssertionError("协程没跑完（线程超时）")
    return box["v"]


class Fixture(object):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="calwired_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "STICKER_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("STICKER_DIR", None)
        ss.STICKER_DIR = Path(self.dir) / "stickers"
        ss._index_cache.update({"data": None, "ts": 0.0})
        vn._reset_cache()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ss._index_cache.update({"data": None, "ts": 0.0})
        vn._reset_cache()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, raw=PNG, meaning="", human=True, tags=("惊讶",)):
        with patch.object(ss, 'describe_meaning', new=AsyncMock(return_value="")):
            got = run_coro(ss.save_sticker_from_dataurl(
                "data:image/png;base64," + base64.b64encode(raw).decode(),
                tags=list(tags)))
        if meaning:
            ss.set_meaning(got["id"], meaning, source="human" if human else "vision")
        return got["id"]


class CatalogForHerTests(Fixture, unittest.TestCase):
    def test_picker_hint_lists_the_library_with_meanings(self):
        sid = self.put(meaning="无语凝噎，不是惊讶")
        hint = b.sticker_choice_hint()
        self.assertIn(sid[:8], hint)
        self.assertIn("无语凝噎", hint)
        self.assertIn("[[斗图:", hint)      # 教她可以点名

    def test_calibrated_entries_are_marked_and_first(self):
        a = self.put(PNG, meaning="机器猜的", human=False)
        calibrated = self.put(PNG + b"\x00", meaning="主人定的", human=True)
        hint = b.sticker_choice_hint()
        self.assertLess(hint.index(calibrated[:8]), hint.index(a[:8]),
                        "主人校准过的那张该排在前面，她先看可信的")
        self.assertIn("主人校准过", hint)

    def test_empty_library_does_not_add_a_hint(self):
        self.assertEqual(b.sticker_choice_hint(), "")


class PickByIdTests(Fixture, unittest.TestCase):
    def test_short_prefix_resolves(self):
        sid = self.put()
        got = ss.pick_by_sid(sid[:6])
        self.assertEqual((got or {}).get("id"), sid)

    def test_unknown_or_ambiguous_prefix_is_refused(self):
        self.put()
        self.assertIsNone(ss.pick_by_sid("zzzzzz"))
        self.assertIsNone(ss.pick_by_sid(""))


class MarkerWiringTests(Fixture, unittest.TestCase):
    def test_named_marker_is_parsed_without_leaving_it_in_the_text(self):
        got, want = b.parse_sticker_marker("哼，给你看这个。[[斗图:1a2b3c4d]]")
        self.assertEqual(want, "1a2b3c4d")
        self.assertNotIn("[[", got)
        self.assertIn("给你看这个", got)

    def test_plain_marker_still_works(self):
        got, want = b.parse_sticker_marker("笑死[[斗图]]")
        self.assertEqual((got, want), ("笑死", ""))

    def test_named_marker_sends_that_sticker(self):
        # A write to a socket is not a delivery receipt. Exercise the real outbox
        # while mocking only the external request/response transport.
        with patch.object(ss, 'pick_by_sid', return_value={"id": "1a2b3c4d", "abs": "/x/y.png"}), \
                patch.object(b, '_napcat_visible', return_value="/media/y.png"), \
                patch.object(b, '_chat_response', new=AsyncMock(return_value={'retcode': 0})) as transport, \
                patch.object(b, 'record_bot_group_message'), \
                patch.object(b, '_sticker_last_ts', {}), \
                patch.object(b, 'QQ_DRY_RUN', False):
            ok = run_coro(b._maybe_send_sticker(object(), group_id="9", want="1a2b3c4d"))
        self.assertTrue(ok)
        transport.assert_awaited_once()
        self.assertEqual('/media/y.png', transport.await_args.args[2]['message'][0]['data']['file'])

    def test_stale_named_id_falls_back_without_failing_silently(self):
        # 主人刚在面板删掉那张：她记着的短 id 失效了，要出声并退回随机，不能整轮没图
        # 换个群 id：上一条用例刚发过图，45 秒冷却会把这次直接挡在门外，
        # 那测到的就不是「点名失效」而是冷却本身。
        notes = []
        with patch.object(ss, 'pick_by_sid', return_value=None), \
                patch.object(ss, 'pick_random', return_value=None), \
                patch.object(b, '_napcat_visible', return_value=""), \
                patch("builtins.print", new=lambda msg, *a, **kw: notes.append(msg)):
            ok = run_coro(b._maybe_send_sticker(object(), group_id="99", want="deadbeef"))
        self.assertFalse(ok)
        self.assertTrue(any("不在库里" in x for x in notes), notes)


class ReceivedStickerUsesCalibration(Fixture, unittest.TestCase):
    def test_calibrated_library_image_skips_the_vision_guess(self):
        sid = self.put(meaning="这是在无语，不是在惊讶")
        data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        calls = {"vision": 0}

        async def fake_vision(messages, **kw):
            calls["vision"] += 1
            return '{"kind":"表情包","intent":"惊讶","mood":"惊讶"}'

        with patch.object(b, 'image_url_to_openai_ref', new=AsyncMock(return_value=data_url)), \
                patch.object(b, 'call_mimo_vision', new=AsyncMock(side_effect=fake_vision)), \
                patch.object(b, 'remember_image_desc'):
            got = run_coro(b._describe_image("", "", image_ref="https://x/y.png",
                                             sensitive=True, session="private-user-7"))
        self.assertEqual(calls["vision"], 0, "命中校准库还要再视觉猜一遍")
        self.assertIn("无语", str(got))
        note = vn.note_for("https://x/y.png", scene="private", session="private-user-7")
        self.assertEqual((note or {}).get("intent"), "这是在无语，不是在惊讶")

    def test_taught_meaning_also_skips_the_vision_guess(self):
        """群友教过的同样算已有定论：再猜一次只会把对的猜成错的。"""
        sid = self.put()
        ss.set_meaning(sid, "别人教的：无奈", source="chat")
        data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        calls = {"vision": 0}

        async def fake_vision(messages, **kw):
            calls["vision"] += 1
            return '{"kind":"表情包","intent":"开心","mood":"开心"}'

        with patch.object(b, 'image_url_to_openai_ref', new=AsyncMock(return_value=data_url)), \
                patch.object(b, 'call_mimo_vision', new=AsyncMock(side_effect=fake_vision)), \
                patch.object(b, 'remember_image_desc'):
            got = run_coro(b._describe_image("", "", image_ref="https://x/chat.png",
                                             sensitive=True, session="private-user-9"))
        self.assertEqual(calls["vision"], 0)
        self.assertIn("无奈", str(got))

    def test_unknown_image_still_goes_through_vision(self):
        self.put(meaning="库里那张的含义")
        other = b"GIF89a" + os.urandom(200)
        data_url = "data:image/gif;base64," + base64.b64encode(other).decode()
        calls = {"vision": 0}

        async def fake_vision(messages, **kw):
            calls["vision"] += 1
            return '{"kind":"表情包","intent":"想被夸","mood":"得意","objects":[]}'

        with patch.object(b, 'image_url_to_openai_ref', new=AsyncMock(return_value=data_url)), \
                patch.object(b, 'call_mimo_vision', new=AsyncMock(side_effect=fake_vision)), \
                patch.object(b, 'remember_image_desc'):
            run_coro(b._describe_image("", "", image_ref="https://x/other.gif",
                                       sensitive=True, session="private-user-8"))
        self.assertEqual(calls["vision"], 1)


class PromptInjectionTests(Fixture, unittest.TestCase):
    """清单必须真的拼进她那一轮，不然校准还是只改了个文件。"""

    def test_sticker_hint_uses_the_catalog(self):
        with open(os.path.join(ROOT, "bot", "qq_openclaw_bridge.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_sticker_hint = sticker_choice_hint() or (", src)

    def test_choice_hint_lists_meanings_with_their_author(self):
        sid = self.put(meaning="无语凝噎", human=True)
        got = b.sticker_choice_hint()
        self.assertIn(sid[:8], got)
        self.assertIn("无语凝噎", got)
        self.assertIn("主人校准过", got)
        self.assertIn("[[斗图:", got)

    def test_taught_meanings_are_marked_as_second_class(self):
        """群友教的要让她看见「这是别人说的」，可信度和主人校准的不是一档。"""
        taught = self.put(PNG, meaning="好像是无奈", human=False)
        idx = ss._load_index()
        idx[taught]["meaning_src"] = "chat"
        ss._save_index(idx)
        got = b.sticker_choice_hint()
        self.assertIn("有人教过她", got)

    def test_human_outranks_chat_in_the_list(self):
        chat = self.put(PNG, meaning="别人教的", human=False)
        idx = ss._load_index()
        idx[chat]["meaning_src"] = "chat"
        ss._save_index(idx)
        boss = self.put(PNG + b"\x01", meaning="主人定的", human=True)
        got = b.sticker_choice_hint()
        self.assertLess(got.index(boss[:8]), got.index(chat[:8]))

    def test_empty_library_gives_no_hint(self):
        self.assertEqual(b.sticker_choice_hint(), "")


class TeachingSourceTests(unittest.TestCase):
    """群友教她含义时，记的必须是 chat，不能冒充主人的校准。"""

    def test_learn_path_uses_chat_source(self):
        src = (Path(ROOT) / "bot" / "qq_openclaw_bridge.py").read_text(encoding="utf-8")
        i = src.index("async def _learn_sticker_meaning")
        block = src[i:i + 4000]
        self.assertIn('source="chat"', block)
        self.assertNotIn('source="human"', block)


if __name__ == "__main__":
    unittest.main()
