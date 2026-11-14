"""回归锁：面板上「她自己看过什么」那一块。

这块的唯一价值是让人能检查「让她自己学习」这句话，所以两件事必须钉住：
  1) 它得在 admin session 后面 —— 流水里带着她查到的标题和关键词，
     挂在没人管的地方等于把她的行为日志公开；
  2) 前端渲染必须转义。那些文本全部来自外部：群友说的话、搜索引擎回的标题、
     B站视频的标题。把这种内容直接拼进 innerHTML，就是给她自己开的
     存储型 XSS 入口 —— 她在群里查到一个标题就能在自己的后台里执行脚本。
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
WEB = os.path.join(ROOT, "website")
for p in (BOT, WEB):
    if p not in sys.path:
        # app.py 里写明了这个顺序是有讲究的，别把它换成 insert(0, WEB)
        if p == BOT:
            sys.path.insert(0, p)
        else:
            sys.path.append(p)

import activity_log as al        # noqa: E402


def _make_app():
    from flask import Flask
    import ceylan_dashboard
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-only"
    ceylan_dashboard.install(app, {})
    return app


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="actpanel_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        self.app = _make_app()
        self.client = self.app.test_client()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def _login(self):
        with self.client.session_transaction() as s:
            s["ceylan_admin"] = True

    def test_requires_admin(self):
        r = self.client.get("/admin/api/bot/activity")
        self.assertEqual(r.status_code, 401, r.data[:120])

    def test_returns_the_three_views(self):
        al.note("search", "荒坂塔 建筑原型", "记住了", scene="自主")
        al.note("share", "荒坂塔", "主动讲了", detail="那栋楼挺硬的")
        self._login()
        d = json.loads(self.client.get("/admin/api/bot/activity").data.decode("utf-8"))
        self.assertTrue(d["ok"], d)
        kinds = [e["kind"] for e in d["events"]]
        self.assertIn("search", kinds)
        self.assertIn("share", kinds)
        self.assertIn("curiosity", d)
        self.assertIn("bili", d)

    def test_counts_match_events(self):
        for i in range(3):
            al.note("search", "话题%d" % i, "记住了")
        self._login()
        d = json.loads(self.client.get("/admin/api/bot/activity").data.decode("utf-8"))
        self.assertEqual(d["counts"]["by_kind"].get("search"), 3, d["counts"])

    def test_hostile_query_survives_as_text_not_html(self):
        # 服务端原样返回是对的（它是 JSON）；安全靠前端转义，见下面那条测试。
        nasty = "<img src=x onerror=alert(1)>"
        al.note("search", nasty, "查到的对不上")
        self._login()
        d = json.loads(self.client.get("/admin/api/bot/activity").data.decode("utf-8"))
        self.assertEqual(d["events"][0]["what"], nasty)

    def test_endpoint_still_answers_when_a_module_breaks(self):
        # 三个数据源各自独立：curiosity 读不到也不该把整块面板变成 500，
        # 否则「她最近看了什么」会因为一个小故障完全看不见
        real = sys.modules.get("curiosity")
        sys.modules["curiosity"] = None
        try:
            self._login()
            r = self.client.get("/admin/api/bot/activity")
            self.assertEqual(r.status_code, 200)
            d = json.loads(r.data.decode("utf-8"))
            self.assertTrue(d["ok"])
            self.assertTrue(d["errors"], "模块坏了却没有任何交代")
        finally:
            if real is not None:
                sys.modules["curiosity"] = real


class FrontendWiringTests(unittest.TestCase):
    """面板接线。UI 逻辑一律在 console.js —— 带 src 的 <script> 元素内容
    浏览器不会执行，这段代码曾经就栽在这里：「她自己看过什么」永远停在加载中。
    """

    @classmethod
    def setUpClass(cls):
        with io.open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
            cls.html = f.read()
        with io.open(os.path.join(WEB, "static", "console.js"), encoding="utf-8") as f:
            cls.js = f.read()

    def test_card_exists_in_persona_view(self):
        self.assertIn('id="view-persona"', self.html)
        for i in ("ex-refresh", "ex-search", "ex-watch", "ex-topics", "ex-sum"):
            self.assertIn('id="%s"' % i, self.html, i)
        self.assertIn("她自己看过什么", self.html)

    def test_no_logic_inside_external_script_tag(self):
        for m in re.finditer(r"<script[^>]*\bsrc=[^>]*>", self.html):
            head = m.group(0)
            tail = self.html[m.end():m.end() + 40]
            self.assertTrue(tail.lstrip().startswith("</script>"),
                            "带 src 的 script 里塞了内容，浏览器不会执行：%s" % head)

    def test_fetches_the_admin_endpoint(self):
        self.assertIn("/admin/api/bot/activity", self.js)

    def test_exploring_is_refreshed_with_the_persona_view(self):
        start = self.js.index("async function loadPersona")
        blk = self.js[start:start + 900]
        self.assertIn("loadExploring()", blk, "切到人格页不会刷新的面板等于坏面板")

    def test_external_text_is_escaped_before_innerhtml(self):
        start = self.js.index("她自己查过什么、刷到什么")
        blk = self.js[start:self.js.index("var pSend", start)]
        self.assertIn("esc(", blk, "渲染外部文本的块里没有转义函数")
        for field in ("e.what", "e.out", "e.ref", "e.detail",
                      "n.title", "n.owner", "n.reaction",
                      "n.subtitle_reason", "t.topic", "t.state", "t.seed_from"):
            pat = r"esc\(\s*(?:String\(\s*)?" + re.escape(field) + r"\b"
            self.assertTrue(re.search(pat, blk), "%s 没被转义就进了 innerHTML" % field)

    def test_no_raw_interpolation_of_event_fields(self):
        start = self.js.index("她自己查过什么、刷到什么")
        blk = self.js[start:self.js.index("var pSend", start)]
        for bad in ("+ e.what +", "+ n.title +", "+ t.topic +"):
            self.assertNotIn(bad, blk, bad)


class PruneTests(unittest.TestCase):
    """KEEP_DAYS 不是装饰：prune 没人调的话，流水只会一直涨。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="actprune_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        os.makedirs(os.path.join(self.dir, "memory"), exist_ok=True)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def _touch(self, name, age_days):
        p = os.path.join(self.dir, "memory", name)
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(u"{}\n")
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
        return p

    def test_old_files_gone_recent_kept(self):
        old = self._touch("activity-2026-01-01.jsonl", 60)
        fresh = self._touch("activity-today.jsonl", 1)
        self.assertEqual(al.prune(), 1)
        self.assertFalse(os.path.exists(old), "过期文件没删")
        self.assertTrue(os.path.exists(fresh), "没过期的一起删了就是丢数据")

    def test_prune_is_quiet_when_there_is_nothing(self):
        self.assertEqual(al.prune(), 0)

    def test_rollup_worker_calls_prune(self):
        with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"),
                     encoding="utf-8") as f:
            src = f.read()
        blk = src[src.index("async def periodic_memory_usage_rollup"):]
        blk = blk[:blk.index("async def periodic_memory_archive")]
        self.assertIn("_al.prune()", blk,
                      "prune 又没人调了 —— 这次就是这么攒出垃圾的")


class EmojiRouteTests(unittest.TestCase):
    """表情库接口：内置表情 + 她自己的斗图库都要给得出来。

    以前这个接口只返回内置表情，所以「表情库」点进去只能看见一堆小黄脸，
    真正会发出去的梗图一张都看不到。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="emojiapi_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        self.app = _make_app()
        self.client = self.app.test_client()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def test_returns_builtin_and_stickers(self):
        d = json.loads(self.client.get("/admin/api/bot/emojis").data.decode("utf-8"))
        self.assertIn("emojis", d)
        self.assertIn("stickers", d)
        self.assertIsInstance(d["stickers"], list)
        self.assertIn("stats", d)
        self.assertGreater(len(d["emojis"]), 10, "内置表情没返回")

    def test_sticker_image_route_requires_admin(self):
        r = self.client.get("/admin/api/bot/sticker/whatever")
        self.assertEqual(r.status_code, 401)

    def test_unknown_sticker_is_404_not_a_traversal(self):
        with self.client.session_transaction() as s:
            s["ceylan_admin"] = True
        for bad in ("nope", "..%2f..%2fconfig.env", "index"):
            r = self.client.get("/admin/api/bot/sticker/" + bad)
            self.assertIn(r.status_code, (404, 400), bad)


if __name__ == "__main__":
    unittest.main()
