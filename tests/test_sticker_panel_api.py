"""表情库面板接口：人工筛选删除 + 认知校准。

主人的要求原话：
    「加一个能够手动删除她添加的表情包的功能，我要人工筛选，
      然后再增加一个可以校准她的表情包认知（列出她对表情包的总结，然后由我修改，
      通过点击对应的表情包展示悬浮窗，不要直接在界面展示）」

接口这几条锁的是「危险动作的边界」，不是渲染：
  · 只有登录管理员能删（401）；
  · 还要带 CSRF（403）—— 删除是写操作，光靠 cookie 就能触发就是 CSRF 面；
  · id 只认索引里那张，路径必须还在库目录内（不能让一个 id 删到 config.env）；
  · 删除走回收，不是 unlink；
  · 校准写入 `meaning_src=human`，机器再猜也不许覆盖。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for d in (ROOT / "bot", ROOT / "website"):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from flask import Flask, jsonify, session  # noqa: E402

import ceylan_dashboard                    # noqa: E402
import sticker_store as ss                 # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
       b"\x08\x06\x00\x00\x00\xea\x0d\x1a\x0c\x00\x00\x00\x19IDATx\x9cc\xfc\xcf"
       b"\xc0\x00\x04\x00\xff\x00\x8cx\xa3\x0e\x1c\x00\x00\x00\x00IEND\xaeB`\x82")


class Base(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.dir = tempfile.mkdtemp(prefix="stkapi_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "STICKER_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("STICKER_DIR", None)
        ss.STICKER_DIR = Path(self.dir) / "stickers"
        ss._index_cache.update({"data": None, "ts": 0.0})
        self.csrf = "test-csrf-token"

        async def blind(messages, **kw):
            return ""
        self._dm = ss.describe_meaning
        ss.describe_meaning = blind
        loop = asyncio.new_event_loop()
        self.sid = loop.run_until_complete(ss.save_sticker_from_dataurl(
            "data:image/png;base64," + base64.b64encode(PNG).decode(),
            tags=["群里", "惊讶"]))["id"]
        loop.close()

        app = Flask(__name__)
        app.secret_key = "t"

        @app.get("/login")
        def _login():
            session["ceylan_admin"] = True
            session["bot_control_csrf"] = self.csrf
            return "ok"

        @app.get("/login-no-csrf")
        def _login2():
            session["ceylan_admin"] = True
            return "ok"

        ceylan_dashboard.install(app, {})
        self.app = app
        self.client = app.test_client()
        self.client.get("/login")

    def tearDown(self):
        ss.describe_meaning = self._dm
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ss._index_cache.update({"data": None, "ts": 0.0})
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, payload, csrf=..., use_client=True):
        headers = {}
        token = self.csrf if csrf is ... else csrf
        if token:
            headers["X-CSRF-Token"] = token
        client = self.client if use_client else self.app.test_client()
        return client.post(path, json=payload, headers=headers)


class MeaningEndpointTests(Base):
    def test_calibration_is_saved_as_human(self):
        r = self.post("/admin/api/bot/sticker/%s/meaning" % self.sid,
                      {"meaning": "这张是无语，不是惊讶"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        rec = ss._load_index()[self.sid]
        self.assertEqual(rec["meaning"], "这张是无语，不是惊讶")
        self.assertEqual(rec["meaning_src"], "human")
        self.assertEqual(json.loads(r.get_data())["stats"]["calibrated"], 1)

    def test_requires_login(self):
        fresh = self.app.test_client()      # 没走 /login，没有管理员 session
        r = self.post("/admin/api/bot/sticker/%s/meaning" % self.sid,
                      {"meaning": "x"}, use_client=False) if False else \
            fresh.post("/admin/api/bot/sticker/%s/meaning" % self.sid, json={"meaning": "x"})
        self.assertEqual(r.status_code, 401)

    def test_requires_csrf(self):
        r = self.post("/admin/api/bot/sticker/%s/meaning" % self.sid,
                      {"meaning": "x"}, csrf="")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(ss._load_index()[self.sid].get("meaning"))

    def test_rejects_wrong_token(self):
        r = self.post("/admin/api/bot/sticker/%s/meaning" % self.sid,
                      {"meaning": "x"}, csrf="not-the-token")
        self.assertEqual(r.status_code, 403)

    def test_unknown_id_is_404_not_a_write(self):
        r = self.post("/admin/api/bot/sticker/deadbeef/meaning", {"meaning": "x"})
        self.assertEqual(r.status_code, 404)

    def test_empty_meaning_is_an_explicit_judgement(self):
        r = self.post("/admin/api/bot/sticker/%s/meaning" % self.sid, {"meaning": "  "})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        rec = ss._load_index()[self.sid]
        self.assertEqual(rec["meaning"], "")
        self.assertEqual(rec["meaning_src"], "human")

    def test_overlong_meaning_is_refused_not_silently_cut(self):
        r = self.post("/admin/api/bot/sticker/%s/meaning" % self.sid,
                      {"meaning": "长" * 120})
        self.assertEqual(r.status_code, 400)


class DeleteEndpointTests(Base):
    def test_delete_moves_file_to_trash_and_drops_index(self):
        abs_ = ss.sticker_path(self.sid)
        self.assertTrue(os.path.isfile(abs_))
        r = self.post("/admin/api/bot/sticker/%s/delete" % self.sid, {})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        self.assertNotIn(self.sid, ss._load_index())
        self.assertFalse(os.path.isfile(abs_), "文件还在库里，她还会抽到这张")
        body = json.loads(r.get_data())
        self.assertTrue(body.get("trashed"), "删除没留回收，点错就找不回来")
        self.assertTrue(os.path.isfile(body["trashed"]))
        self.assertEqual(body["stats"]["count"], 0)

    def test_delete_requires_csrf(self):
        r = self.post("/admin/api/bot/sticker/%s/delete" % self.sid, {}, csrf="")
        self.assertEqual(r.status_code, 403)
        self.assertIn(self.sid, ss._load_index())

    def test_delete_unknown_id_is_404(self):
        r = self.post("/admin/api/bot/sticker/deadbeef/delete", {})
        self.assertEqual(r.status_code, 404)

    def test_delete_cannot_escape_the_library(self):
        """索引里那条的 abs 若指向库外，删除只能不动它。

        面板传进来的只是 id，但索引本身可能被旧数据或别的写入路径污染过，
        一个 `../../config.env` 级别的 filename 不该有机会被删。
        """
        idx = ss._load_index()
        victim = Path(self.dir) / "outside.png"
        victim.write_bytes(PNG)
        idx[self.sid]["file"] = "../outside.png"
        idx[self.sid]["abs"] = str(victim)
        ss._save_index(idx)
        r = self.post("/admin/api/bot/sticker/%s/delete" % self.sid, {})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(victim.is_file(), "删库里的表情却删掉了库外的文件")


class ListingTests(Base):
    def test_list_carries_the_meaning_source_for_the_popup(self):
        ss.set_meaning(self.sid, "无语", source="human")
        got = self.client.get("/admin/api/bot/emojis").get_json()
        row = [s for s in got["stickers"] if s["id"] == self.sid][0]
        self.assertEqual(row["meaning"], "无语")
        self.assertEqual(row["meaning_src"], "human")
        self.assertEqual(got["stats"]["calibrated"], 1)
        self.assertIn("trash", got["stats"])


if __name__ == "__main__":
    unittest.main()
