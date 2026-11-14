"""表情包那一轮，她拿到的是「含义」还是「像素」。

生产实测（23:46~23:49 私聊）：看图笔记已经写对了
（`mood=平静中带疯狂`、`meme_guess=内心崩溃表面淡定`），可回复请求里还是
原样附了 `data:image/... len=7293790` 的像素，于是她张口就是画面解说
「吃西瓜还带角，看着就热」「这眼神……我可没这么呆」。

用户要的是机制，不是再叮嘱一句：含义说得清的纯表情就不传像素，
照片/截图/看不清的照旧传像素——不能为了不回错就让她变成瞎子。
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot"))

import qq_openclaw_bridge as b  # noqa: E402
import vision_notes as vn  # noqa: E402

MEME_NOTE = {"key": "meme1", "ts": time.time(), "kind": "表情包",
             "intent": "表面淡定其实已经崩了", "mood": "强撑",
             "text_in_image": "", "objects": ["咖啡杯", "火焰"],
             "visibility": "public", "session": "private-user-9001"}

PHOTO_NOTE = {"key": "photo1", "ts": time.time(), "kind": "照片",
              "intent": "给你看看我买的", "mood": "得意",
              "text_in_image": "", "objects": ["西瓜"],
              "visibility": "public", "session": "private-user-9001"}


def _no_look():
    """「当场看一眼」这条支路关掉：单元测试不该为此真去取图、真去调视觉模型。"""
    return patch.object(b, "_look_now", new=AsyncMock(return_value=None))


class MaterialTests(unittest.IsolatedAsyncioTestCase):
    async def test_sticker_with_meaning_sends_no_pixels(self):
        async def fake_ready(ref, scene, session, wait=1.5):
            return {"kind": "表情包", "intent": "表面淡定其实已经崩了", "mood": "强撑",
                    "objects": ["猫耳", "咖啡杯", "火焰"], "text_in_image": ""}

        with patch.object(b, "_note_ready", new=AsyncMock(side_effect=fake_ready)):
            urls, card = await b._image_reply_material("[图片]", ["a.gif"],
                                                       scene="private", session="user-9001")
        self.assertEqual(urls, [])
        self.assertIn("表面淡定其实已经崩了", card)
        self.assertIn("不要描述画面", card)

    async def test_real_photo_still_sends_pixels(self):
        async def fake_ready(ref, scene, session, wait=1.5):
            return PHOTO_NOTE

        with patch.object(b, "_note_ready", new=AsyncMock(side_effect=fake_ready)):
            urls, card = await b._image_reply_material("[图片]", ["p.jpg"],
                                                       scene="private", session="user-9001")
        self.assertEqual(urls, ["p.jpg"])
        self.assertEqual(card, "")

    async def test_unknown_image_still_sends_pixels(self):
        # 没有笔记 = 还没看懂。这时候不传像素就是让她瞎猜，比解说画面更糟。
        with patch.object(b, "_note_ready", new=AsyncMock(return_value=None)), _no_look():
            urls, card = await b._image_reply_material("[图片]", ["x.jpg"],
                                                       scene="private", session="user-9001")
        self.assertEqual(urls, ["x.jpg"])
        self.assertEqual(card, "")

    async def test_mixed_batch_keeps_pixels(self):
        async def fake_ready(ref, scene, session, wait=1.5):
            return {"kind": "表情包", "intent": "想被哄", "mood": "撒娇"} if ref == "a.gif" else PHOTO_NOTE

        with patch.object(b, "_note_ready", new=AsyncMock(side_effect=fake_ready)):
            urls, card = await b._image_reply_material("[图片]", ["a.gif", "p.jpg"],
                                                       scene="private", session="user-9001")
        self.assertEqual(urls, ["a.gif", "p.jpg"])
        self.assertEqual(card, "")

    async def test_asking_about_the_picture_keeps_pixels(self):
        # 「这图里写的什么」就是要看内容，含义卡回答不了
        with patch.object(b, "_note_ready", new=AsyncMock(
                return_value={"kind": "表情包", "intent": "想被哄", "mood": "撒娇"})):
            urls, card = await b._image_reply_material("这图里写的什么", ["a.gif"],
                                                       scene="private", session="user-9001")
        self.assertEqual(urls, ["a.gif"])

    async def test_no_images_no_material(self):
        self.assertEqual(await b._image_reply_material("晚上吃啥", [], scene="private",
                                                      session="user-9001"), ([], ""))

    async def test_batch_waits_only_the_budget(self):
        """等笔记是有界的：后台那次看不出来，也不能把人一直晾着。"""
        waits = []

        async def slow_ready(ref, scene, session, wait=1.5):
            waits.append(wait)
            return None

        with patch.object(b, "_note_ready", new=AsyncMock(side_effect=slow_ready)), _no_look():
            _urls, _card = await b._image_reply_material("[图片]", ["a.gif"], scene="private",
                                                          session="user-9001", wait=1.5)
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], 1.5)


class NoteWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_flight_look_is_awaited_then_used(self):
        # 入库那次看图正在跑：等它一下，这一轮就能用含义卡，而不是带着原图去解说
        notes = {}

        async def look():
            await asyncio.sleep(0.02)
            notes["a.gif"] = {"kind": "表情包", "intent": "想被夸", "mood": "得意"}

        task = asyncio.create_task(look())
        b._VISION_INFLIGHT[("user-9001", vn.key_of("a.gif"))] = task
        self.addCleanup(task.cancel)
        with patch.object(vn, "note_for", side_effect=lambda ref, **kw: notes.get(str(ref))):
            got = await b._note_ready("a.gif", "private", "user-9001", 1.0)
        self.assertIsNotNone(got)
        self.assertEqual(got.get("intent"), "想被夸")

    async def test_wait_is_bounded_when_the_look_hangs(self):
        async def hang():
            await asyncio.sleep(30)

        task = asyncio.create_task(hang())
        self.addCleanup(task.cancel)
        b._VISION_INFLIGHT[("user-9002", vn.key_of("z.gif"))] = task
        t0 = time.time()
        with patch.object(vn, "note_for", return_value=None):
            got = await b._note_ready("z.gif", "private", "user-9002", 0.2)
        self.assertIsNone(got)
        self.assertLess(time.time() - t0, 1.0)

    async def test_no_task_no_wait(self):
        t0 = time.time()
        with patch.object(vn, "note_for", return_value=None):
            got = await b._note_ready("nothing.gif", "group", "group:1", 1.5)
        self.assertIsNone(got)
        self.assertLess(time.time() - t0, 0.2)


def _capture_async(seen):
    """假 _note_images_async：把参数记下来，返回一个能被立刻关掉的协程。"""
    def fake(urls, session="", sensitive=False):
        seen["urls"] = list(urls)
        seen["session"] = session
        seen["sensitive"] = sensitive

        async def _done():
            return None
        return _done()
    return fake


class LookNowTests(unittest.IsolatedAsyncioTestCase):
    """第一张表情也得先懂含义。

    「入库即看图」是后台任务，回复往往比它先跑到；私聊那条以前干脆没排。
    只等不等得到就算了的话，主人看到的仍然是「吃西瓜还带角」——
    所以看不懂时当场看一次，看完再回；看不成（额度用完/取图失败）才退回原图。
    """

    async def test_missing_note_triggers_a_look_before_replying(self):
        seen = {}
        notes = {}

        async def note_imgs(urls, session="", sensitive=False):
            seen["call"] = (list(urls), session, sensitive)
            notes["a.gif"] = {"kind": "表情包", "intent": "想被夸", "mood": "得意"}

        with patch.object(vn, "note_for", side_effect=lambda ref, **kw: notes.get(str(ref))), \
                patch.object(b, "_note_images_async", new=note_imgs):
            urls, card = await b._image_reply_material("[图片]", ["a.gif"], scene="private",
                                                       session="private-user-9001")
        self.assertEqual(seen.get("call"), (["a.gif"], "private-user-9001", True))
        self.assertEqual(urls, [])
        self.assertIn("想被夸", card)

    async def test_group_note_look_is_not_marked_sensitive(self):
        seen = {}
        notes = {}

        async def note_imgs(urls, session="", sensitive=False):
            seen["call"] = (list(urls), session, sensitive)
            notes["g.gif"] = {"kind": "表情包", "intent": "起哄", "mood": "热闹"}

        with patch.object(vn, "note_for", side_effect=lambda ref, **kw: notes.get(str(ref))), \
                patch.object(b, "_note_images_async", new=note_imgs):
            await b._image_reply_material("[图片]", ["g.gif"], scene="group", session="group:123")
        self.assertEqual(seen.get("call"), (["g.gif"], "group:123", False))

    async def test_failed_look_falls_back_to_pixels(self):
        # 当场看不成（额度、取图失败）不能让她对着不存在的图编
        async def note_imgs(urls, session="", sensitive=False):
            return None

        with patch.object(vn, "note_for", return_value=None), \
                patch.object(b, "_note_images_async", new=note_imgs):
            urls, card = await b._image_reply_material("[图片]", ["a.gif"], scene="private",
                                                       session="private-user-9002")
        self.assertEqual(urls, ["a.gif"])
        self.assertEqual(card, "")

    async def test_hanging_look_does_not_stall_the_reply(self):
        async def slow(urls, session="", sensitive=False):
            await asyncio.sleep(5)

        with patch.object(vn, "note_for", return_value=None), \
                patch.object(b, "_note_images_async", new=slow):
            t0 = time.time()
            urls, card = await b._image_reply_material("[图片]", ["a.gif"], scene="private",
                                                       session="private-user-9003", wait=0.3)
        spent = time.time() - t0
        self.assertEqual(urls, ["a.gif"])
        self.assertLess(spent, 2.0, "当场看图不该把人晾在那里")


class PlaceholderAnswerTests(unittest.TestCase):
    """她不能把「[图片]」当成一句话发出去。

    实测（服务器上一次性别 DATA_DIR 的端到端跑）：取图被媒体目录白名单挡掉时，
    提示词里只剩一句「这轮图片没有取到」，模型直接把用户那句 `[图片]` 抄回来当回答。
    这种回复发出去，主人看到的就是她的名字下面挂着两个字「[图片]」。
    """

    def test_bare_placeholder_is_not_an_answer(self):
        self.assertTrue(b._is_placeholder_answer("[图片]"))
        self.assertTrue(b._is_placeholder_answer("[动画表情] "))
        self.assertTrue(b._is_placeholder_answer("[表情][表情]"))
        self.assertTrue(b._is_placeholder_answer(""))

    def test_real_replies_are_not_flags(self):
        for got in ("啧，这表情挺应景。", "[图片] 收到了，我看看", "哈哈这不自闭吗"):
            self.assertFalse(b._is_placeholder_answer(got), got)


class IngestSchedulingTests(unittest.TestCase):
    def test_helper_passes_session_and_sensitivity(self):
        """私聊的笔记必须落在 private-user-<qq> 这个会话键上。

        键不一致 = 回复那一轮 `note_for(session=...)` 查不到 = 又回去解说画面。
        这个错位以前只发生在私聊（群里做了，私聊忘了）。
        """
        seen = {}

        def fake_spawn(coro):
            seen["coro"] = coro
            coro.close()

        with patch.object(b, "_spawn", new=fake_spawn), \
                patch.object(b, "_note_images_async", new=_capture_async(seen)) as _m:
            b._note_at_ingest(["a.gif"], "private-user-9001", sensitive=True)
        self.assertEqual(seen.get("session"), "private-user-9001")
        self.assertEqual(seen.get("sensitive"), True)
        self.assertEqual(seen.get("urls"), ["a.gif"])

    def test_helper_is_quiet_when_there_is_nothing_to_look_at(self):
        with patch.object(b, "_spawn") as spawn:
            b._note_at_ingest([], "private-user-9001")
        spawn.assert_not_called()

    def test_helper_speaks_when_scheduling_fails(self):
        # 调度失败也必须出声：沉默的 worker 视为故障
        lines = []
        with patch.object(b, "_spawn", side_effect=RuntimeError("loop closed")), \
                patch("builtins.print", new=lambda msg, *a, **kw: lines.append(msg)):
            b._note_at_ingest(["a.gif"], "group:123")
        self.assertTrue(any("看图笔记" in x for x in lines), lines)


class HintWordingTests(unittest.TestCase):
    def test_reaction_hint_is_meaning_first(self):
        hint = b._img_reaction_hint("")
        self.assertIn("含义", hint)
        self.assertIn("不要描述画面", hint)

    def test_hint_no_longer_invites_a_visual_pass(self):
        # 老话术「先读画面」是她解说画面的源头之一
        self.assertNotIn("先读画面", b._img_reaction_hint(""))


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def _ask(self, notes, text="[图片]", refs=("a.gif",)):
        key = "private-user-9001"
        captured = {}

        async def fake_build(user_text, image_urls=None):
            captured["urls"] = list(image_urls or [])
            return user_text

        async def fake_call(messages, **kw):
            captured["prompt"] = messages[-1]["content"]
            return "哦哟，你这是撑不住了。"

        self.addCleanup(b.histories.pop, key, None)
        b.histories[key] = deque(maxlen=24)
        async def fake_vision(*a, **kw):
            captured["vision"] = captured.get("vision", 0) + 1
            return "（现场看图占位）"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(vn, "note_for", side_effect=lambda ref, **kw: notes.get(str(ref))), \
                patch.object(b, "build_user_content", new=fake_build), \
                patch.object(b, "call_mimo_vision", new=AsyncMock(side_effect=fake_vision)), \
                patch.object(b, "call_openclaw", new=AsyncMock(side_effect=fake_call)), \
                patch.object(b, "save_histories"), \
                patch.object(b, "_spawn", side_effect=lambda coro: coro.close()):
            got = await b.ask_openclaw(key, text, image_urls=list(refs), speaker_id="9001")
        return got, captured

    async def test_sticker_turn_is_generated_without_pixels(self):
        got, cap = await self._ask({"a.gif": MEME_NOTE})
        self.assertEqual(cap["urls"], [])
        self.assertIn("表面淡定其实已经崩了", cap["prompt"])

    async def test_photo_turn_still_attaches_pixels(self):
        _got, cap = await self._ask({"p.jpg": PHOTO_NOTE}, refs=("p.jpg",))
        self.assertEqual(cap["urls"], ["p.jpg"])

    async def test_meaning_card_is_not_recorded_as_visible_objects(self):
        # 不给像素就不能假装看见了画面细节
        _got, cap = await self._ask({"a.gif": MEME_NOTE})
        self.assertNotIn("咖啡杯", cap["prompt"])


class IngestTests(unittest.IsolatedAsyncioTestCase):
    """入库那一步也不该再去看像素。

    「入库即看图」以前每张图都要 NapCat 取一次原图再喂视觉模型一次；
    回复那轮既然已经读过笔记，就等于看过了 —— 再取一次只是把同一个表情
    看两遍，还把 7MB 的 base64 搬了两趟。
    """

    async def test_seen_sticker_is_not_fetched_again(self):
        hits = {"ref": 0, "vision": 0}

        async def fake_ref(url):
            hits["ref"] += 1
            return "data:image/gif;base64,eA=="

        async def fake_vision(*a, **kw):
            hits["vision"] += 1
            return '{"kind":"表情包","intent":"想被夸","mood":"得意",' \
                   '"text_in_image":"","objects":[]}'

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(vn, "note_for", return_value=MEME_NOTE), \
                patch.object(b, "image_url_to_openai_ref", new=fake_ref), \
                patch.object(b, "call_mimo_vision", new=AsyncMock(side_effect=fake_vision)):
            await b._note_images_async(["a.gif"], sensitive=False, session="user-9001")
        self.assertEqual(hits["ref"], 0)
        self.assertEqual(hits["vision"], 0)

    async def test_unseen_sticker_still_gets_looked_at(self):
        hits = {"vision": 0}

        async def fake_ref(url):
            return "data:image/gif;base64,eA=="

        async def fake_vision(*a, **kw):
            hits["vision"] += 1
            return '{"kind":"表情包","intent":"想被夸","mood":"得意",' \
                   '"text_in_image":"","objects":[]}'

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(vn, "note_for", return_value=None), \
                patch.object(vn, "note_by_sha", return_value=None), \
                patch.object(b, "image_url_to_openai_ref", new=fake_ref), \
                patch.object(b, "call_mimo_vision", new=AsyncMock(side_effect=fake_vision)):
            await b._note_images_async(["new.gif"], sensitive=False, session="user-9001")
        self.assertGreaterEqual(hits["vision"], 1)


class NoFakeBlindnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_dropping_pixels_does_not_make_her_claim_she_cannot_see(self):
        # 「我看不太清这张图」以前是她接到表情包时的一句真话；现在不给像素，
        # 就不能再让「视觉状态」那条兜底句把她带回看不见。
        key = "private-user-9002"
        captured = {}

        async def fake_build(user_text, image_urls=None):
            captured["urls"] = list(image_urls or [])
            return user_text

        async def fake_call(messages, **kw):
            captured["prompt"] = str(messages[-1].get("content", ""))
            return "哦哟，你这是撑不住了。"

        self.addCleanup(b.histories.pop, key, None)
        b.histories[key] = deque(maxlen=24)
        note = dict(MEME_NOTE)
        note["session"] = "private-user-9002"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {"DATA_DIR": root}), \
                patch.object(vn, "note_for", return_value=note), \
                patch.object(b, "build_user_content", new=fake_build), \
                patch.object(b, "call_mimo_vision", new=AsyncMock(side_effect=lambda *a, **k: "x")), \
                patch.object(b, "call_openclaw", new=AsyncMock(side_effect=fake_call)), \
                patch.object(b, "save_histories"), \
                patch.object(b, "_spawn", side_effect=lambda coro: coro.close()):
            await b.ask_openclaw(key, "[图片]", image_urls=["a.gif"], speaker_id="9002")
        self.assertEqual(captured["urls"], [])
        self.assertNotIn("不能声称看见", captured.get("prompt", ""))
        self.assertIn("对方想表达", captured.get("prompt", ""))


if __name__ == "__main__":
    unittest.main()
