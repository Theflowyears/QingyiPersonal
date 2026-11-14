"""回归锁：首页热门是「她自己刷」的来源，匿名可用，且关掉时一条请求都不发。"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import bili        # noqa: E402

FEED = {
    "code": 0, "message": "OK",
    "data": {"list": [
        {"bvid": "BV1GJ411x7h7", "aid": 1, "cid": 2,
         "title": "【明日方舟】新角色抽卡实测", "duration": 613,
         "pubdate": 1794000000, "desc": "抽了八十发",
         "owner": {"name": "罗德岛人事部"},
         "stat": {"view": 123456, "like": 999},
         "tname": "手机游戏"},
        {"bvid": "", "title": "坏数据"},
    ]},
}


class FakeClient(object):
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return FakeResponse(FEED)


class FakeResponse(object):
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilifeed_")
        self._old = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "BILI_MODE", "BILI_COOKIE")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["BILI_MODE"] = "meta"
        os.environ.pop("BILI_COOKIE", None)
        bili.MODE = "meta"
        bili.COOKIE = ""
        bili._cache.clear()

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

    def test_parses_feed_into_watchable_items(self):
        c = FakeClient()
        got = asyncio.run(bili.fetch_popular(5, client=c))
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["items"]), 1)
        it = got["items"][0]
        self.assertEqual(it["bvid"], "BV1GJ411x7h7")
        self.assertEqual(it["category"], "手机游戏")
        self.assertEqual(it["duration"], 613)
        self.assertEqual(it["title"], "【明日方舟】新角色抽卡实测")
        self.assertEqual(c.calls[0][1].get("ps"), 5)

    def test_off_mode_sends_nothing(self):
        bili.MODE = "off"
        c = FakeClient()
        got = asyncio.run(bili.fetch_popular(5, client=c))
        self.assertFalse(got["ok"])
        self.assertEqual(c.calls, [])


if __name__ == "__main__":
    unittest.main()