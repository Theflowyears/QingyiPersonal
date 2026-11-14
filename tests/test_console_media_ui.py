"""回归锁：面板媒体这条链路（收藏表情代理、愿望带图、图片回读）。

三条都是用户实际报过的问题：
  · QQ 收藏表情在面板里全是一排破图标 —— 浏览器直连 QQ CDN 会被 Referer 挡；
  · 愿望墙不能发图；
  · 回读图片的路由不能变成任意文件读取。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
WEB = os.path.join(ROOT, "website")
for p in (BOT, WEB):
    if p not in sys.path:
        sys.path.insert(0, p) if p == BOT else sys.path.append(p)

import sticker_store as ss          # noqa: E402
import media_upload as mu           # noqa: E402


def _make_app():
    from flask import Flask
    import ceylan_dashboard
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-only"
    ceylan_dashboard.install(app, {})
    return app


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="consolmedia_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "STICKER_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("STICKER_DIR", None)
        from pathlib import Path as _P
        self._sticker_dir = ss.STICKER_DIR
        ss.STICKER_DIR = _P(self.dir) / "stickers"
        ss._index_cache.update({"data": None, "ts": 0.0})
        self.app = _make_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["ceylan_admin"] = True

    def tearDown(self):
        ss.STICKER_DIR = self._sticker_dir
        ss._index_cache.update({"data": None, "ts": 0.0})
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class QqFaceProxyTests(Base):
    def test_needs_admin(self):
        plain = self.app.test_client()
        r = plain.get("/admin/api/bot/qq-face?i=0")
        self.assertEqual(r.status_code, 401)

    def test_index_out_of_range_is_404_not_arbitrary_url(self):
        old = ss.qq_face_urls
        ss.qq_face_urls = lambda: ["https://p.qpic.cn/qq_expression/a"]
        try:
            for bad in ("1", "-1", "abc", ""):
                r = self.client.get("/admin/api/bot/qq-face?i=" + bad)
                self.assertIn(r.status_code, (400, 404), bad)
        finally:
            ss.qq_face_urls = old

    def test_image_bytes_come_back_through_the_server(self):
        import asyncio
        old_urls, old_fetch = ss.qq_face_urls, ss.fetch_qq_face
        ss.qq_face_urls = lambda: ["https://p.qpic.cn/qq_expression/a"]

        async def fake_fetch(url):
            return b"GIF89aFAKE", "image/gif"
        ss.fetch_qq_face = fake_fetch
        try:
            r = self.client.get("/admin/api/bot/qq-face?i=0")
        finally:
            ss.qq_face_urls, ss.fetch_qq_face = old_urls, old_fetch
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"GIF89aFAKE")
        self.assertTrue(str(r.headers.get("content-type", "")).startswith("image/"))

    def test_upstream_failure_is_reported_not_blank_image(self):
        old_urls, old_fetch = ss.qq_face_urls, ss.fetch_qq_face
        ss.qq_face_urls = lambda: ["https://p.qpic.cn/qq_expression/a"]

        async def fail(url):
            return None, "HTTP403"
        ss.fetch_qq_face = fail
        try:
            r = self.client.get("/admin/api/bot/qq-face?i=0")
        finally:
            ss.qq_face_urls, ss.fetch_qq_face = old_urls, old_fetch
        self.assertEqual(r.status_code, 502)
        self.assertIn("403", r.get_json()["error"])


class FaceUrlSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="faceurls_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reads_only_http_urls(self):
        import json
        with io.open(os.path.join(self.dir, "qq_faces.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"urls": ["https://p.qpic.cn/a", "file:///etc/passwd",
                                          "/app/napcat/x.gif", None]}))
        self.assertEqual(ss.qq_face_urls(), ["https://p.qpic.cn/a"])

    def test_missing_file_is_empty_not_crash(self):
        self.assertEqual(ss.qq_face_urls(), [])


class WishImageTests(Base):
    def _upload(self, raw=b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"payload"):
        from io import BytesIO
        return self.client.post("/admin/api/bot/photo", data={
            "photo": (BytesIO(raw), "shot.png"), "caption": "给她看的"},
            content_type="multipart/form-data")

    def test_upload_returns_a_token_not_an_absolute_path(self):
        r = self._upload()
        d = r.get_json()
        self.assertTrue(d.get("ok"), d)
        self.assertNotIn("data/", str(d.get("token")))
        self.assertIn("/", d["token"])
        self.assertTrue(d["token"].split("/")[-1].endswith(".png"))

    def test_wish_can_carry_an_image_and_it_round_trips(self):
        token = self._upload().get_json()["token"]
        r = self.client.post("/admin/api/bot/wishes", json={
            "action": "add_wish", "text": "想看这张", "kind": "know", "image": token})
        d = r.get_json()
        self.assertTrue(d.get("ok"), d)
        self.assertEqual(d.get("image_error", ""), "")
        self.assertEqual(d["wish"]["image"], token)
        got = self.client.get("/admin/api/bot/wishes")
        self.assertEqual(got.get_json()["wishes"][-1]["image"], token)

    def test_forged_image_reference_is_dropped_but_the_wish_survives(self):
        r = self.client.post("/admin/api/bot/wishes", json={
            "action": "add_wish", "text": "假装我有图",
            "image": "../../config.env"})
        d = r.get_json()
        self.assertTrue(d.get("ok"))
        self.assertTrue(d.get("image_error"), "伪造的图必须说清楚，不能静默收下")
        self.assertNotIn("image", d["wish"])

    def test_photo_file_endpoint_refuses_traversal_and_absolute_paths(self):
        for bad in ("../../config.env", "/etc/passwd", "2026-11-07/..",
                    "2026-11-07/evil.php", "2026-11-07/zz.jpg"):
            r = self.client.get("/admin/api/bot/photo/file?name=" + bad)
            self.assertIn(r.status_code, (400, 404, 500), bad)

    def test_photo_file_serves_what_was_uploaded(self):
        token = self._upload().get_json()["token"]
        r = self.client.get("/admin/api/bot/photo/file?name=" + token)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(len(r.data) > 16)
        self.assertIn("max-age", str(r.headers.get("Cache-Control", "")))


class PromptSnapshotTests(Base):
    """「提示词预览」必须给她真的看到的，不是我们重新拼一份。"""

    def _snapshot(self, kind="group"):
        import qq_openclaw_bridge as b
        return b.remember_prompt_snapshot(
            [{"role": "system", "content": "你是清漪"},
             {"role": "user", "content": [
                 {"type": "text", "text": "群里最近聊了什么"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
            kind=kind, session="900000001", mode="chat")

    def test_writer_keeps_structure_and_drops_image_bytes(self):
        self.assertTrue(self._snapshot())
        path = os.path.join(self.dir, "prompt_snapshot.json")
        with io.open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("base64,AAAA", raw, "图片字节不该进快照")
        self.assertIn("[图片已省略]", raw)
        data = json.loads(raw)
        self.assertEqual(data["group"]["mode"], "chat")
        self.assertEqual(len(data["group"]["messages"]), 2)
        self.assertGreater(data["group"]["total_chars"], 0)

    def test_snapshot_file_is_owner_only(self):
        import stat
        self.assertTrue(self._snapshot())
        path = os.path.join(self.dir, "prompt_snapshot.json")
        # Windows 的 chmod 只管只读位，权限断言只在生产（POSIX）上有意义
        if os.name == "nt":
            self.skipTest("POSIX 权限位才有意义；生产是 Linux")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertFalse(mode & 0o077, "带着群聊原文的快照不能给同机其他用户读")

    def test_group_and_dm_keep_their_own_slots(self):
        self._snapshot("group")
        self._snapshot("dm")
        got = json.loads(io.open(os.path.join(self.dir, "prompt_snapshot.json"),
                                 encoding="utf-8").read())
        self.assertEqual(sorted(got), ["dm", "group"])

    def test_preview_returns_the_real_prompt(self):
        self._snapshot("group")
        r = self.client.post("/admin/api/bot/style/preview",
                             json={"mode": "chat", "scope": "group"})
        d = r.get_json()
        self.assertEqual(d["source"], "real")
        self.assertIn("你是清漪", d["prompt"])
        self.assertEqual(d["kind"], "group")

    def test_preview_without_snapshot_says_so(self):
        r = self.client.post("/admin/api/bot/style/preview",
                             json={"mode": "chat", "scope": "group"})
        d = r.get_json()
        self.assertEqual(d["source"], "style_block")
        self.assertIn("不是她真的看到的提示词", d["note"])

    def test_preview_requires_admin(self):
        plain = self.app.test_client()
        r = plain.post("/admin/api/bot/style/preview", json={"scope": "group"})
        self.assertEqual(r.status_code, 401)


class ModelMatrixVideoTests(Base):
    """视频理解必须出现在模型矩阵里，显示的还得是实际生效的那一份。"""

    def setUp(self):
        super().setUp()
        import llm_router as lr
        self.lr = lr
        lr._CONSOLE_VIDEO.update(sig=None, cfg=None)

    def test_video_role_shows_effective_omni_and_vision_channel(self):
        vision = self.lr.ModelConfig("vision", "https://vision.example.com",
                                     "sk-" + "k" * 40, "qwen3.8-flash")
        with patch.object(self.lr, "LLM_VISION", vision), \
                patch.object(self.lr, "LLM_VIDEO", None, create=True):
            d = self.client.get("/admin/api/bot/models-config").get_json()
        video = [m for m in d["models"] if m["role"] == "video"]
        self.assertEqual(len(video), 1, "模型矩阵里没有视频理解")
        self.assertEqual(video[0]["model"], "qwen3.8-omni-flash")
        self.assertEqual(video[0]["base_url"], "https://vision.example.com")
        self.assertTrue(video[0]["configured"])

    def test_video_role_can_be_saved(self):
        r = self.client.post("/admin/api/bot/models-config", json={
            "role": "video", "model": "qwen3.8-omni-flash",
            "base_url": "https://panel.example.com", "api_key": "sk-" + "v" * 32,
            "max_tokens": 3000, "timeout": 240})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json().get("ok"))
        with open(os.path.join(self.dir, "models_config.json"), encoding="utf-8") as f:
            saved = json.load(f)["video"]
        self.assertEqual(saved["model"], "qwen3.8-omni-flash")
        self.assertEqual(saved["max_tokens"], 3000)

    def test_edit_form_offers_video(self):
        with io.open(os.path.join(ROOT, "website", "console.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('<option value="video">视频理解 · Omni · VIDEO</option>', html)


class VisitsChartWiringTests(unittest.TestCase):
    """访客统计：口径要说人话，图不能是那种「一到峰值就压平」的柱子。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "website", "app.py"), encoding="utf-8") as f:
            cls.app = f.read()
        with io.open(os.path.join(root, "website", "static", "console.js"),
                     encoding="utf-8") as f:
            cls.js = f.read()
        with io.open(os.path.join(root, "website", "static", "console.css"),
                     encoding="utf-8") as f:
            cls.css = f.read()
        with io.open(os.path.join(root, "website", "console.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_api_states_what_a_visit_is(self):
        start = self.app.index('def api_visits')
        blk = self.app[start:start + 1800]
        self.assertIn('"counts"', blk)
        self.assertIn("retention_days", blk)
        self.assertIn("dedupe_seconds", blk)

    def test_chart_is_a_line_chart_with_a_honest_axis(self):
        start = self.js.index("function niceAxisMax")
        blk = self.js[start:self.js.index("/* ---------- visits ----------", start)]
        self.assertIn("createElementNS", blk, "SVG 得用安全 DOM 建，不能拼字符串")
        self.assertIn("lc-line", blk)
        self.assertIn("Math.ceil(v / unit) * unit", blk)

    def test_old_bar_formula_is_gone(self):
        # 那套 height:% + min-height:2px 的柱子就是「到顶变平」的根源
        self.assertNotIn(".trend .col i", self.css)
        self.assertNotIn("Math.max(2, Math.round(num(v) / peak * 100))", self.js)

    def test_view_explains_the_metrics(self):
        self.assertIn('id="vs-note"', self.html)
        self.assertIn('id="vs-trend-sum"', self.html)
        self.assertIn("不是人数", self.html)


if __name__ == "__main__":
    unittest.main()