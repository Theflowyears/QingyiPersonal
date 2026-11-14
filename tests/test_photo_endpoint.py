"""上传接口的路由级测试：安全口径最怕只测了函数、没测路由。"""
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOT = os.path.join(ROOT, "bot")
WEB = os.path.join(ROOT, "website")
# 顺序很要紧：bot 必须在最前，website 只能 append。
# 两边各有一个同名的 project_config.py，而 ceylan_runtime 是
# 「from project_config import BLOCKED_GROUP_IDS」—— website 抢在前头就会
# ImportError（app.py 里那段注释说的是同一件事，别绕开它）。
if BOT not in sys.path:
    sys.path.insert(0, BOT)
if WEB not in sys.path:
    sys.path.append(WEB)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        import ceylan_dashboard
        import whisper as wp
        self.wp = wp
        self.dir = tempfile.mkdtemp(prefix="ep_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR",)}
        os.environ["DATA_DIR"] = self.dir
        wp._FILE = os.path.join(self.dir, "persona", "whisper.json")
        wp._cache.update({"data": None, "ts": 0.0})
        import vision_notes as vn
        vn._FILE = os.path.join(self.dir, "memory", "vision_notes.json")
        vn._reset_cache()
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test"
        ceylan_dashboard.install(app, {})
        self.client = app.test_client()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.wp._cache.update({"data": None, "ts": 0.0})
        shutil.rmtree(self.dir, ignore_errors=True)

    def login(self):
        with self.client.session_transaction() as ss:
            ss["ceylan_admin"] = True

    def jpeg(self):
        return bytes((0xff, 0xd8, 0xff, 0xe0)) + b"0" * 5000

    def post(self, body, name, extra=None):
        data = {"photo": (io.BytesIO(body), name)}
        if extra:
            data.update(extra)
        return self.client.post("/admin/api/bot/photo", data=data,
                                content_type="multipart/form-data")

    def test_requires_admin_session(self):
        r = self.post(self.jpeg(), "a.jpg")
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True)[:120])

    def test_no_file_is_400(self):
        self.login()
        r = self.client.post("/admin/api/bot/photo", data={"caption": "空的"},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:120])

    def test_accepts_real_jpeg_when_logged_in(self):
        self.login()
        r = self.post(self.jpeg(), "我的照片.jpg", {"caption": "我桌上"})
        d = r.get_json()
        self.assertTrue(d and d.get("ok"), str(d)[:200])
        up = os.path.join(self.dir, "uploads")
        self.assertTrue(os.path.isdir(up))
        names = [f for _d, _s, fs in os.walk(up) for f in fs]
        self.assertEqual(len(names), 1, names)
        # 落盘名必须来自内容哈希，客户端给的名字一点都不能带进来
        self.assertNotIn("我的照片", names[0])
        self.assertEqual(len((self.wp.snapshot() or {}).get("images") or []), 1)
        rec = (self.wp.snapshot() or {})["images"][0]
        self.assertEqual(rec["caption"], "我桌上")
        self.assertFalse(rec["seen"])

    def test_rejects_fake_extension_at_route(self):
        self.login()
        r = self.post(b"<html><script>alert(1)</script></html>", "evil.jpg")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:160])
        self.assertEqual(len((self.wp.snapshot() or {}).get("images") or []), 0)

    def test_rejects_svg_at_route(self):
        self.login()
        r = self.post(b"<svg onload=alert(1)>", "a.svg")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:160])

    def test_rejects_double_extension(self):
        self.login()
        r = self.post(self.jpeg(), "photo.jpg.php")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:160])

    def test_rejects_oversize(self):
        self.login()
        import media_upload as m
        r = self.post(self.jpeg() + b"0" * m.MAX_BYTES, "big.jpg")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:160])
        self.assertIn("大小", str(r.get_json()))

    def test_same_image_uploaded_twice_is_one_file_two_queue_rows(self):
        self.login()
        self.post(self.jpeg(), "a.jpg")
        self.post(self.jpeg(), "b.jpg")
        up = os.path.join(self.dir, "uploads")
        names = [f for _d, _s, fs in os.walk(up) for f in fs]
        self.assertEqual(len(names), 1, names)
        self.assertEqual(len((self.wp.snapshot() or {}).get("images") or []), 2)

    def test_get_lists_notes_and_queue(self):
        self.login()
        r = self.client.get("/admin/api/bot/photo")
        d = r.get_json()
        self.assertTrue(d and d.get("ok"), str(d)[:160])
        for k in ("notes", "queue", "stats"):
            self.assertIn(k, d)

    def test_get_requires_login(self):
        r = self.client.get("/admin/api/bot/photo")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
