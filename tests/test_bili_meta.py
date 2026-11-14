"""回归锁：刷 B 站这条链路（元数据层）。

三条不能松的线：
  1) 链接来自聊天 = 外部输入。只允许 bilibili.com / b23.tv，其余一律不去 fetch，
     否则谁都能在群里发一条 http://127.0.0.1:xxxx 让她替我们敲内网的门。
  2) 实测过：不登录时字幕列表和评论区都是「空」。空和拿不到是两件事 ——
     混起来她就会说「这视频居然没字幕」这种没依据的话。
  3) BILI_MODE=off（上线默认）时，一步都不许走，连请求都不发。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import media_link as ml     # noqa: E402
import bili                 # noqa: E402

REAL_VIEW = {
    "code": 0, "message": "OK",
    "data": {"bvid": "BV1GJ411x7h7", "aid": 80433022, "cid": 137649199,
             "title": "【官方 MV】Never Gonna Give You Up - Rick Astley",
             "duration": 213, "pubdate": 1577835803, "desc": "-",
             "rights": {"has_subtitle": None},
             "owner": {"mid": 486906719, "name": "索尼音乐中国"},
             "stat": {"view": 108120793, "like": 210000, "reply": 240125},
             "pages": [{"cid": 137649199, "page": 1, "part": "Never Gonna Give You Up"}],
             "subtitle": {"list": [{"lan_doc": "中文（中国）", "subtitle_url": ""}]}},
}


class ParserTests(unittest.TestCase):
    def test_bvid_in_plain_url(self):
        self.assertEqual(ml.video_refs(
            "https://www.bilibili.com/video/BV1GJ411x7h7?p=2"), ["BV1GJ411x7h7"])

    def test_mobile_subdomain_counts(self):
        self.assertTrue(ml.is_allowed("https://m.bilibili.com/video/BV1GJ411x7h7"))

    def test_bare_bvid_in_sentence(self):
        self.assertEqual(ml.video_refs("BV1GJ411x7h7 这个太搞笑了"), ["BV1GJ411x7h7"])

    def test_av_id(self):
        self.assertEqual(ml.video_refs(
            "https://www.bilibili.com/video/av80433022"), ["av80433022"])

    def test_truncated_bvid_is_not_a_video(self):
        # 认下来就要发一次请求拿 404，白跑一趟还让她以为视频坏了
        self.assertEqual(ml.video_refs("BV12 这个"), [])

    def test_lookalike_hosts_are_rejected(self):
        for u in ("https://evil.com/?x=bilibili.com",
                  "https://mybilibili.com/v/BV1GJ411x7h7",
                  "https://bilibili.com.evil.net/",
                  "http://[::1]:8080/admin"):
            self.assertFalse(ml.is_allowed(u), u)

    def test_localhost_and_intranet_are_never_fetched(self):
        refs = ml.video_refs("看看这个 http://127.0.0.1:3001/onebot "
                             "BV1GJ411x7h7")
        self.assertEqual(refs, ["BV1GJ411x7h7"], "内网地址不该被当成视频")

    def test_short_link_shape(self):
        self.assertEqual(ml.short_links("转发 https://b23.tv/aB3xKd 看看"),
                         ["https://b23.tv/aB3xKd"])

    def test_from_final_url(self):
        self.assertEqual(ml.from_final_url(
            "https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id=xxx"),
            "BV1GJ411x7h7")

    def test_duplicates_collapse(self):
        self.assertEqual(ml.video_refs(
            "BV1GJ411x7h7 https://b23.tv/aaa BV1GJ411x7h7"), ["BV1GJ411x7h7"])


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bili_")
        self._old = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "BILI_MODE", "BILI_COOKIE")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["BILI_MODE"] = "meta"
        os.environ.pop("BILI_COOKIE", None)
        bili.MODE = "meta"
        bili.COOKIE = ""
        bili._cache.clear()
        bili._notes.update({"sig": None, "items": None})

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bili.MODE = os.environ.get("BILI_MODE", "off")
        bili.COOKIE = os.environ.get("BILI_COOKIE", "")
        bili._cache.clear()
        shutil.rmtree(self.dir, ignore_errors=True)


class FakeClient(object):
    """只记录被请求了什么，好让「一条都没发」这件事可验证。"""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        key = "view" if "view" in url else ("player" if "player" in url else "other")
        body = self.payloads.get(key, {"code": -1, "message": "no fixture"})
        if hasattr(body, "headers"):      # 短链那种：要的是响应头不是 JSON
            return body
        return _Resp(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Resp(object):
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class MetaTests(_Base):
    def test_metadata_parses_what_the_api_actually_returns(self):
        c = FakeClient({"view": REAL_VIEW})
        meta = run(bili.fetch_meta("BV1GJ411x7h7", client=c))
        self.assertTrue(meta["ok"], meta)
        self.assertEqual(meta["owner"], "索尼音乐中国")
        self.assertEqual(meta["duration"], 213)
        self.assertEqual(meta["view"], 108120793)

    def test_mode_off_sends_no_request(self):
        bili.MODE = "off"
        c = FakeClient({"view": REAL_VIEW})
        meta = run(bili.fetch_meta("BV1GJ411x7h7", client=c))
        self.assertFalse(meta["ok"])
        self.assertEqual(c.calls, [], "BILI_MODE=off 还发请求就是没关")

    def test_api_error_is_reported_not_swallowed(self):
        c = FakeClient({"view": {"code": -404, "message": "啥都木有"}})
        meta = run(bili.fetch_meta("BV1GJ411x7h7", client=c))
        self.assertFalse(meta["ok"])
        self.assertIn("-404", meta["reason"])

    def test_second_fetch_is_cached(self):
        c = FakeClient({"view": REAL_VIEW})
        run(bili.fetch_meta("BV1GJ411x7h7", client=c))
        run(bili.fetch_meta("BV1GJ411x7h7", client=c))
        self.assertEqual(len(c.calls), 1, c.calls)


class SubtitleHonestyTests(_Base):
    def test_no_cookie_says_need_login_not_no_subtitle(self):
        # 「没有字幕」和「没登录所以看不到」必须分开，
        # 前者她会说出口，后者不该假装成结论
        meta = bili._ok_view(REAL_VIEW["data"])
        c = FakeClient({"player": {"code": 0, "data": {"subtitle": {"subtitles": []}}}})
        got = run(bili.fetch_subtitles(meta, client=c))
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "需要登录 cookie")
        self.assertEqual(c.calls, [], "没 cookie 就不该去敲接口的门")

    def test_with_cookie_empty_list_is_reported_as_no_cc(self):
        bili.COOKIE = "buvid3=fake"
        meta = bili._ok_view(REAL_VIEW["data"])
        c = FakeClient({"player": {"code": 0, "data": {"subtitle": {"subtitles": []}}}})
        got = run(bili.fetch_subtitles(meta, client=c))
        self.assertFalse(got["ok"])
        self.assertIn("CC", got["reason"])

    def test_subtitle_text_is_read(self):
        bili.COOKIE = "buvid3=fake"
        meta = bili._ok_view(REAL_VIEW["data"])
        c = FakeClient({
            "player": {"code": 0, "data": {"subtitle": {"subtitles": [
                {"lan_doc": "中文（中国）", "subtitle_url": "//other/x.json"}]}}},
            "other": {"body": [{"content": "今天来讲讲荒坂塔这栋楼到底是怎么来的"},
                               {"content": "它原本是荒坂公司在夜之城的总部大楼"},
                               {"content": "2077 年那场爆炸把它和公司广场一起抹平了"},
                               {"content": "后来重建成了现在这个纪念公园的样子"}]},
        })
        got = run(bili.fetch_subtitles(meta, client=c))
        self.assertTrue(got["ok"], got)
        self.assertIn("荒坂公司", got["text"])

    def test_tiny_subtitle_is_not_trusted(self):
        bili.COOKIE = "buvid3=fake"
        meta = bili._ok_view(REAL_VIEW["data"])
        c = FakeClient({
            "player": {"code": 0, "data": {"subtitle": {"subtitles": [
                {"lan_doc": "中文", "subtitle_url": "//other/x.json"}]}}},
            "other": {"body": [{"content": "嗯"}]},
        })
        got = run(bili.fetch_subtitles(meta, client=c))
        self.assertFalse(got["ok"])
        self.assertIn("太短", got["reason"])


class NoteTests(_Base):
    def test_note_keeps_no_subtitle_text(self):
        # 存的是「她看过、当时怎么想」，不是台词全文，也不是视频文件
        meta = bili._ok_view(REAL_VIEW["data"])
        rec = bili.save_note(meta, {"ok": False, "reason": "需要登录 cookie"},
                             reaction="这UP主标题起得真唬人", scene="group")
        self.assertNotIn("text", rec)
        self.assertEqual(rec["reaction"], "这UP主标题起得真唬人")
        self.assertFalse(rec["has_subtitle"])

    def test_same_video_updates_instead_of_duplicating(self):
        meta = bili._ok_view(REAL_VIEW["data"])
        bili.save_note(meta, reaction="第一次")
        bili.save_note(meta, reaction="第二次")
        self.assertEqual(len(bili.latest_notes(10)), 1)
        self.assertEqual(bili.note_for("BV1GJ411x7h7")["reaction"], "第二次")

    def test_seen_before_blocks_a_reshare(self):
        meta = bili._ok_view(REAL_VIEW["data"])
        self.assertFalse(bili.seen_before("BV1GJ411x7h7"))
        bili.save_note(meta)
        self.assertTrue(bili.seen_before("BV1GJ411x7h7"))

    def test_seen_before_is_case_sensitive(self):
        # BV 号大小写有意义：upper() 以后两个不同视频会被判成同一个，
        # 于是她「看过了」其实没看，也不会再去看第二条
        bili.save_note(bili._ok_view(REAL_VIEW["data"]))
        self.assertFalse(bili.seen_before("BV1GJ411X7H7"))

    def test_eviction_drops_stale_not_latest(self):
        meta = bili._ok_view(REAL_VIEW["data"])
        bili.MAX_NOTES_BAK = bili.MAX_NOTES
        bili.MAX_NOTES = 3
        try:
            for i in range(5):
                m = dict(meta)
                m["bvid"] = "BV1%09d" % i
                bili.save_note(m)
            keys = [x["bvid"] for x in bili.latest_notes(10)]
            self.assertEqual(len(keys), 3, keys)
            self.assertIn("BV1000000004", keys, "最新的反而被淘汰了")
        finally:
            bili.MAX_NOTES = bili.MAX_NOTES_BAK

    def test_render_mentions_subtitle_uncertainty(self):
        meta = bili._ok_view(REAL_VIEW["data"])
        rec = bili.save_note(meta, {"ok": False, "reason": "需要登录 cookie"})
        line = bili.render_for_prompt(rec)
        self.assertIn("字幕没看到", line)
        self.assertIn("需要登录", line)


class ShortLinkTests(_Base):
    def test_short_link_resolves_from_location(self):
        c = FakeClient({})
        c.payloads["other"] = _Loc(
            "https://www.bilibili.com/video/BV1GJ411x7h7/?vd_source=abc")
        self.assertEqual(run(bili.resolve_short("https://b23.tv/aB3xKd",
                                                client=c)), "BV1GJ411x7h7")

    def test_non_whitelisted_host_is_never_followed(self):
        c = FakeClient({})
        for u in ("http://127.0.0.1:3001/admin", "https://evil.com/b23.tv/x"):
            self.assertEqual(run(bili.resolve_short(u, client=c)), "", u)
        self.assertEqual(c.calls, [], "不该跟着她发的地址去敲门")


class _Loc(object):
    def __init__(self, loc):
        self.headers = {"location": loc}
        self.status_code = 302
        self.url = loc


if __name__ == "__main__":
    unittest.main()
