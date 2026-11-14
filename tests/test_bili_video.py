import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import bili
import bili_vision


def meta(**kw):
    base = {"ok": True, "bvid": "BV1xx", "aid": 1, "cid": 2,
            "title": "测试视频", "owner": "UP", "duration": 120, "desc": "简介"}
    base.update(kw)
    return base


class FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, body):
        self.body = body

    async def get(self, *a, **kw):
        return FakeResp(self.body)


class PlayInfoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"BILI_COOKIE": "SESSDATA=x", "BILI_MODE": "meta"})
        environment.start()
        self.addCleanup(environment.stop)
        bili.MODE = "meta"
        bili.COOKIE = "SESSDATA=x"
        bili._cache.clear()

    async def test_durl_mp4_is_used(self):
        body = {"code": 0, "data": {"timelength": 120000,
                "quality": 64, "durl": [{"url": "https://cdn/x.mp4", "size": 1234}]}}
        got = await bili.fetch_playinfo(meta(), client=FakeClient(body))
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["url"], "https://cdn/x.mp4")
        self.assertEqual(got["seconds"], 120)

    async def test_dash_only_is_reported_not_faked(self):
        body = {"code": 0, "data": {"dash": {"video": [{"baseUrl": "https://cdn/v.m4s"}]}}}
        got = await bili.fetch_playinfo(meta(), client=FakeClient(body))
        self.assertFalse(got["ok"])
        self.assertIn("DASH", got["reason"])

    async def test_no_cookie_is_explicit(self):
        bili.COOKIE = ""
        got = await bili.fetch_playinfo(meta(), client=FakeClient({"code": 0}))
        self.assertFalse(got["ok"])
        self.assertIn("cookie", got["reason"])


class VisionPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_url_and_subtitles_are_sent(self):
        seen = {}

        async def llm(messages, **kw):
            seen["messages"] = messages
            return json.dumps({"overview": "两个人对线", "timeline": [{"t": "00:05", "what": "开团"}],
                               "key_points": ["对线"], "uncertainties": [], "emotional_beats": ["紧张"]})

        got = await bili_vision.understand(meta(), {"ok": True, "text": "字幕" * 3000},
                                           "https://media/x", llm=llm)
        self.assertTrue(got["ok"], got)
        content = seen["messages"][1]["content"]
        video = [x for x in content if x.get("type") == "video_url"][0]
        self.assertEqual(video["video_url"]["url"], "https://media/x")
        text = [x for x in content if x.get("type") == "text"][0]["text"]
        self.assertGreater(len(text), 500)
        self.assertIn("字幕", text)

    async def test_timeline_as_strings_or_other_keys_is_kept(self):
        async def llm(messages, **kw):
            return json.dumps({"overview": "看完了", "timeline": [
                "00:12 男主听见心声", {"time": "00:30", "desc": "千夏摇头"},
                {"t": "00:48", "what": "拥抱"}]})

        got = await bili_vision.understand(meta(), {"ok": False}, "https://media/x", llm=llm)
        self.assertTrue(got["ok"], got)
        self.assertEqual([r["t"] for r in got["timeline"]], ["00:12", "00:30", "00:48"])
        self.assertIn("千夏摇头", got["timeline"][1]["what"])

    async def test_non_json_is_not_a_success(self):
        async def llm(messages, **kw):
            return "我看完了"
        got = await bili_vision.understand(meta(), {"ok": False, "reason": "x"},
                                           "https://media/x", llm=llm)
        self.assertFalse(got["ok"])


class DownloadHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_range_and_cookie_are_sent_because_cdn_rejects_plain_get(self):
        import httpx
        captured = {}

        class Stream:
            status_code = 206
            headers = {"content-type": "video/mp4"}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False
            async def aiter_bytes(self, size):
                yield b"x" * 10

        class Client:
            def __init__(self, *a, **kw):
                captured.update(kw)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False
            def stream(self, *a, **kw):
                return Stream()

        temporary = tempfile.TemporaryDirectory(prefix="dl_")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            "DATA_DIR": temporary.name, "BILI_COOKIE": "SESSDATA=abc"})
        environment.start()
        self.addCleanup(environment.stop)
        with patch.object(httpx, "AsyncClient", Client):
            got = await bili.download_media("https://cdn/x.mp4")
        self.assertTrue(got["ok"], got)
        headers = captured.get("headers") or {}
        self.assertEqual("bytes=0-", headers.get("Range"))
        self.assertEqual("SESSDATA=abc", headers.get("Cookie"))
        self.assertEqual("https://www.bilibili.com/", headers.get("Referer"))
        self.assertTrue(os.path.exists(got["path"]))
        self.assertEqual(10, os.path.getsize(got["path"]))
        os.remove(got["path"])


class WatchVideoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bili_watch_")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {
            "DATA_DIR": self.root, "PUBLIC_MEDIA_BASE_URL": "https://site.example"})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(bili._cache.clear)

    async def test_too_long_never_claims_it_watched(self):
        with patch.object(bili, "fetch_playinfo") as play:
            got = await bili.watch_video(meta(duration=99999), {"ok": True, "text": "x"})
        self.assertFalse(got["ok"])
        self.assertEqual(got["watch_depth"], "仅字幕")
        play.assert_not_called()

    async def test_pipeline_downloads_publishes_and_cleans_up(self):
        fake = os.path.join(self.root, "v.mp4")
        open(fake, "wb").write(b"x" * 10)
        with patch.object(bili, "fetch_playinfo", new=AsyncMock(return_value={
                    "ok": True, "url": "https://cdn/x.mp4", "size": 10})), \
             patch.object(bili, "download_media", new=AsyncMock(return_value={
                    "ok": True, "path": fake, "bytes": 10})), \
             patch("media_public.publish", return_value={
                    "ok": True, "url": "https://site.example/media/qv/tok", "expires": 9999999999}), \
             patch("media_public.cleanup"), \
             patch.object(bili_vision, "understand", new=AsyncMock(return_value={
                    "ok": True, "overview": "看完了", "timeline": []})):
            got = await bili.watch_video(meta(), {"ok": True, "text": "字幕"})
        self.assertTrue(got["ok"])
        self.assertEqual(got["watch_depth"], "视频+字幕")
        self.assertFalse(os.path.exists(fake))


if __name__ == "__main__":
    unittest.main()
