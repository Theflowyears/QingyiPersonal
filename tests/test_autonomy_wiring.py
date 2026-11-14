"""回归锁：自主链路的接线。

「有函数」不等于「她会做」：B 站要由 worker 在队列空的时候自己去热门里挑，
上网要真的打开结果网页而不是只看摘要。这两条都在这里走真调用链验一次。
"""
import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b     # noqa: E402
import bili                        # noqa: E402
import bili_auto as ba             # noqa: E402
import web_read                    # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


FEED = [{"bvid": "BV1GJ411x7h7", "title": "【明日方舟】新角色抽卡实测",
         "owner": "罗德岛人事部", "category": "手机游戏", "duration": 613,
         "view": 123456, "desc": "", "aid": 1, "cid": 2, "pubdate": 0},
        {"bvid": "BVNEWS", "title": "今日要闻", "owner": "新闻",
         "category": "资讯", "duration": 300, "view": 999,
         "desc": "", "aid": 3, "cid": 4, "pubdate": 0}]


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autowire_")
        self._old = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "BILI_MODE", "BILI_AUTO")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["BILI_MODE"] = "meta"
        os.environ["BILI_AUTO"] = "1"
        bili.MODE = "meta"
        bili._cache.clear()
        b._BILI_QUEUE[:] = []
        b._BILI_AUTO_NOTE["ts"] = 0.0
        b._BILI_AUTO_BACKOFF["until"] = 0.0
        self.feed_calls = []

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bili.MODE = os.environ.get("BILI_MODE", "off")
        b._BILI_QUEUE[:] = []
        bili._cache.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_seed_queues_one_video_from_public_feed(self):
        async def fake_feed(limit=20, client=None):
            self.feed_calls.append(limit)
            return {"ok": True, "reason": "", "items": FEED}
        with patch.object(bili, "fetch_popular", fake_feed), \
                patch.object(b, "_bili_interests", lambda: ["明日方舟", "抽卡"]), \
                patch.object(ba, "block_reason", lambda now=None: ""):
            n = run(b._bili_auto_seed(bili))
        self.assertEqual(n, 1)
        self.assertEqual(len(b._BILI_QUEUE), 1)
        got = b._BILI_QUEUE[0]
        self.assertEqual(got["ref"], "BV1GJ411x7h7")
        self.assertEqual(got["scene"], "自主")
        self.assertEqual(ba.stats()["today"], 1)

    def test_seed_does_not_fetch_when_not_due(self):
        async def fake_feed(limit=20, client=None):
            self.feed_calls.append(limit)
            return {"ok": True, "items": FEED}
        with patch.object(bili, "fetch_popular", fake_feed), \
                patch.object(ba, "block_reason", lambda now=None: "夜里不刷"):
            n = run(b._bili_auto_seed(bili))
        self.assertEqual(n, 0)
        self.assertEqual(self.feed_calls, [])
        self.assertEqual(b._BILI_QUEUE, [])

    def test_worker_seeds_only_when_queue_is_empty(self):
        src = inspect.getsource(b.bili_worker)
        self.assertIn("_bili_auto_seed", src)
        self.assertIn("if not _BILI_QUEUE", src)

    def test_autonomy_is_logged_as_her_own_action(self):
        async def fake_feed(limit=20, client=None):
            return {"ok": True, "items": FEED}
        with patch.object(bili, "fetch_popular", fake_feed), \
                patch.object(b, "_bili_interests", lambda: ["明日方舟"]), \
                patch.object(ba, "block_reason", lambda now=None: ""):
            run(b._bili_auto_seed(bili))
        import activity_log
        rows = activity_log.recent(limit=5, kinds=["watch"])
        self.assertTrue(rows)
        self.assertEqual(rows[0]["scene"], "自主")
        self.assertEqual(rows[0]["ref"], "BV1GJ411x7h7")


class FeedDrainTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autodrain_")
        self._old = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "BILI_MODE", "BILI_COOKIE")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["BILI_MODE"] = "meta"
        os.environ.pop("BILI_COOKIE", None)
        bili.MODE = "meta"
        bili.COOKIE = ""
        bili._cache.clear()
        bili._notes.update({"sig": None, "items": None})
        b._BILI_QUEUE[:] = []

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bili.MODE = os.environ.get("BILI_MODE", "off")
        bili.COOKIE = os.environ.get("BILI_COOKIE", "")
        bili._cache.clear()
        bili._notes.update({"sig": None, "items": None})
        b._BILI_QUEUE[:] = []
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_feed_item_does_not_call_blocked_view_api(self):
        item = dict(FEED[0], ok=True)
        b._BILI_QUEUE.append({"kind": "feed", "ref": item["bvid"],
                              "scene": "自主", "session": "autonomy",
                              "run_id": "r1", "meta": item})

        async def forbidden(*a, **kw):
            raise AssertionError("feed 条目不该再请求 view 接口")

        async def fake_sub(meta):
            return {"ok": False, "reason": "需要登录 cookie", "text": ""}

        async def no_reaction(meta, sub, com=None, vision=None):
            return ""

        async def fake_kb(*a, **kw):
            return {"saved": False, "reason": ""}

        with patch.object(bili, "fetch_meta", forbidden), \
                patch.object(bili, "fetch_subtitles", fake_sub), \
                patch.object(b, "_bili_reaction", no_reaction), \
                patch.object(b, "_kb_judge", fake_kb):
            n = run(b._bili_drain(bili))

        self.assertEqual(n, 1)
        notes = bili.latest_notes(5)
        self.assertEqual(notes[0]["bvid"], item["bvid"])
        self.assertEqual(notes[0]["title"], item["title"])
        self.assertFalse(notes[0]["has_subtitle"])


class CuriosityReadsPageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autoread_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR",)}
        os.environ["DATA_DIR"] = self.dir
        self.summaries = []
        self.pages = []

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_lookup_opens_top_result_and_judges_real_text(self):
        import web_search

        async def fake_plan(topic, **kw):
            return web_search.plan_heuristic(topic)

        async def fake_search(q, limit=None, **kw):
            return {"ok": True, "source": "bocha", "run_id": "r1",
                    "results": [{"title": "明日方舟新角色机制", "url":
                                 "https://example.com/wiki",
                                 "snippet": "明日方舟新角色机制的详细说明，用来通过摘录长度门槛。" * 3}]}

        async def fake_page(url, client=None, **scope):
            self.pages.append(url)
            return {"ok": True, "title": "明日方舟 wiki", "url": url,
                    "chars": 800,
                    "text": "明日方舟新角色机制 " + "正文" * 300}

        async def fake_judge(_kb, key, title, summary, source=""):
            self.summaries.append(summary)
            return {"saved": True, "reason": ""}

        with patch.object(web_search, "searchable_topic", lambda t: True), \
                patch.object(b, "_search_plan", fake_plan), \
                patch.object(web_search, "search", fake_search), \
                patch.object(web_read, "fetch_page", fake_page), \
                patch.object(b, "_kb_judge", fake_judge):
            found, keys = run(b._curiosity_lookup("明日方舟新角色机制"))

        self.assertTrue(found)
        self.assertEqual(self.pages, ["https://example.com/wiki"])
        self.assertTrue(self.summaries)
        self.assertIn("正文", self.summaries[0])
        self.assertTrue(keys)

    def test_lookup_falls_back_to_snippet_when_page_fails(self):
        import web_search

        async def fake_plan(topic, **kw):
            return web_search.plan_heuristic(topic)

        async def fake_search(q, limit=None, **kw):
            return {"ok": True, "source": "bocha", "run_id": "r2",
                    "results": [{"title": "明日方舟新角色机制", "url":
                                 "https://example.com/wiki",
                                 "snippet": "明日方舟新角色机制的摘要内容，用来兜底。" * 4}]}

        async def fake_page(url, client=None, **scope):
            return {"ok": False, "reason": "超时", "text": ""}

        async def fake_judge(_kb, key, title, summary, source=""):
            self.summaries.append(summary)
            return {"saved": True, "reason": ""}

        with patch.object(web_search, "searchable_topic", lambda t: True), \
                patch.object(b, "_search_plan", fake_plan), \
                patch.object(web_search, "search", fake_search), \
                patch.object(web_read, "fetch_page", fake_page), \
                patch.object(b, "_kb_judge", fake_judge):
            found, _ = run(b._curiosity_lookup("明日方舟新角色机制"))
        self.assertTrue(found)
        self.assertIn("摘要内容", self.summaries[0])


if __name__ == "__main__":
    unittest.main()