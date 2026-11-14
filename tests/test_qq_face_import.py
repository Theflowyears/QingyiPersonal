"""回归锁：QQ 收藏并进她的斗图库 —— 一个库、一套去重、一个发送池。

用户的话是「QQ 收藏表情和她的斗图库应该都是她发表情的内容，没有必要分开」。
合并之前确实是两套，而且排序相反：`/斗图` 优先发 QQ 收藏，自动补发优先发本地库，
于是「她发的这张到底算哪来的」没人答得上，面板也分成两张表。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import sticker_store as ss        # noqa: E402


def gif(w, h, seed=b"a"):
    return (b"GIF89a" + bytes([w & 255, w >> 8, h & 255, h >> 8]) + b"\x00\x00\x00"
            + b"\x21\xf9\x04" + seed * 6000)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qqfaceimport_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "STICKER_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("STICKER_DIR", None)
        ss.STICKER_DIR = Path(self.dir) / "stickers"
        ss._index_cache.update({"data": None, "ts": 0.0})
        self._meaning = ss.describe_meaning
        self._fetch = ss.fetch_qq_face
        self.fetched = []

        async def no_meaning(path):
            return ""

        ss.describe_meaning = no_meaning

    def tearDown(self):
        ss.describe_meaning = self._meaning
        ss.fetch_qq_face = self._fetch
        ss._index_cache.update({"data": None, "ts": 0.0})
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def serve(self, mapping):
        """假装 QQ CDN：按地址给字节，取不到的报原因。"""
        def counter():
            return len(self.fetched)

        async def fake(url):
            self.fetched.append(url)
            return mapping.get(url, (None, "HTTP403"))

        ss.fetch_qq_face = fake


class ImportTests(Base):
    def test_faces_land_in_the_same_library(self):
        a, b = "https://p.qpic.cn/a.gif", "https://p.qpic.cn/b.gif"
        self.serve({a: (gif(120, 120, b"a"), "image/gif"),
                    b: (gif(90, 90, b"b"), "image/gif")})
        out = asyncio.run(ss.import_qq_faces([a, b]))
        self.assertEqual(out["new"], 2, out)
        idx = ss._load_index(ttl=0.0)
        self.assertEqual(len(idx), 2)
        for rec in idx.values():
            self.assertIn(ss.QQ_FACE_TAG, rec["tags"])
            self.assertTrue(rec.get("src"), "要记住是从哪个地址搬来的")
            self.assertTrue((ss.STICKER_DIR / rec["file"]).is_file())

    def test_same_face_is_not_downloaded_twice(self):
        a = "https://p.qpic.cn/c.gif"
        self.serve({a: (gif(100, 100, b"c"), "image/gif")})
        asyncio.run(ss.import_qq_faces([a]))
        before = len(self.fetched)
        out = asyncio.run(ss.import_qq_faces([a]))
        self.assertEqual(out["checked"], 0, "src 已经记过就别再下一遍")
        self.assertEqual(len(self.fetched), before)
        self.assertEqual(len(ss._load_index(ttl=0.0)), 1)

    def test_identical_bytes_from_two_sources_stay_one_record(self):
        raw = gif(110, 110, b"d")
        a = "https://p.qpic.cn/d.gif"
        self.serve({a: (raw, "image/gif")})
        asyncio.run(ss.save_sticker_from_dataurl(
            "data:image/gif;base64," + _b64(raw), tags=["群里"], note="群友发的"))
        out = asyncio.run(ss.import_qq_faces([a]))
        self.assertEqual(out["dup"], 1, out)
        self.assertEqual(len(ss._load_index(ttl=0.0)), 1, "同一张图只能有一条")

    def test_unreachable_face_is_reported_not_swallowed(self):
        self.serve({})                    # 什么都没给 → 全部 HTTP403
        out = asyncio.run(ss.import_qq_faces(["https://p.qpic.cn/x.gif"]))
        self.assertEqual(out["failed"], 1, out)
        self.assertEqual(ss._load_index(ttl=0.0), {})

    def test_imported_faces_are_pickable(self):
        a = "https://p.qpic.cn/e.gif"
        self.serve({a: (gif(80, 80, b"e"), "image/gif")})
        asyncio.run(ss.import_qq_faces([a]))
        self.assertIsNotNone(ss.pick_random(), "并池以后她得能抽到这张")


class PoolWiringTests(Base):
    """发送只认一个池子；面板也不再是两张表。"""

    @classmethod
    def setUpClass(cls):
        with io_open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            cls.bridge = f.read()
        web = os.path.join(ROOT, "website")
        with io_open(os.path.join(web, "static", "console.js"), encoding="utf-8") as f:
            cls.js = f.read()
        with io_open(os.path.join(web, "console.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_sticker_command_uses_the_local_pool_first(self):
        start = self.bridge.index('if kind == "send":')
        blk = self.bridge[start:start + 700]
        self.assertNotIn("pick_qq_native_face", blk,
                         "/斗图 不能再优先 QQ 收藏地址：那是另一个池子")
        self.assertIn("pick_random", blk)

    def test_something_syncs_the_faces_without_being_asked(self):
        # 光有函数不算合并：得有个东西定时去叫它，而且拿到列表后真的搬图。
        self.assertIn("async def qq_face_sync_worker", self.bridge)
        with io_open(os.path.join(BOT, "ceylan_runtime.py"), encoding="utf-8") as f:
            self.assertIn("qq_face_sync_worker", f.read(), "worker 没被起过等于没写")
        with io_open(os.path.join(BOT, "sticker_store.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("async def refresh_qq_native_faces")
        end = src.index("async def get_qq_native_faces")
        self.assertIn("import_qq_faces(", src[start:end],
                      "同步到地址列表却只写 JSON，库还是空的")

    def test_panel_has_one_library_grid_not_two(self):
        self.assertNotIn('id="em-faces"', self.html, "两张表就是用户说的「没必要分开」")
        self.assertIn('id="em-stickers"', self.html)
        self.assertNotIn("/admin/api/bot/qq-face?i=", self.js)

    def test_both_senders_translate_the_path_for_the_container(self):
        """NapCat 在 docker 里，看不见 /opt/qqbot/data/stickers。

        并池以后她发的全是本地文件，宿主机绝对路径直接塞过去是静默失败：
        日志写「已补发一张」，群里什么都没有，`used` 也永远涨不上去。
        两个发送口都必须先过 `_napcat_visible`。
        """
        start = self.bridge.index("async def _maybe_send_sticker")
        auto = self.bridge[start:start + 2000]
        cmd_start = self.bridge.index('if kind == "send":')
        command = self.bridge[cmd_start:cmd_start + 1400]
        for name, blk in (("自动补发", auto), ("/斗图", command)):
            self.assertIn("_napcat_visible(", blk, "%s 还在发宿主机路径" % name)


def io_open(*a, **kw):
    import io
    return io.open(*a, **kw)


def _b64(raw):
    import base64
    return base64.b64encode(raw).decode()


class AuditAndFacesTests(Base):
    """并池以后，历史体检不能反过来把她自己收藏的表情赶走。

    `audit_library` 的门是「超过 512px 就当照片移走」。QQ 收藏里确实有大图
    （现网最大 2048×2048），那些是她主动收的表情，不是混进来的手机照片。
    """

    def test_imported_faces_survive_the_photo_gate(self):
        raw = gif(1200, 1200)                    # 边长远超 512px 的门
        a = "https://p.qpic.cn/big.gif"
        self.serve({a: (raw, "image/gif")})
        asyncio.run(ss.import_qq_faces([a]))
        report = ss.audit_library(dry_run=False)
        self.assertEqual(report["hide"], 0, report)
        self.assertEqual(len(ss._load_index(ttl=0.0)), 1, "收藏来的大图被体检赶走了")

    def test_group_photos_still_get_moved_out(self):
        big = jpeg(3000, 2000, body=b"t" * 800000)
        asyncio.run(ss.save_sticker_from_dataurl(_b64url(big), tags=["群里"],
                                                 note="手滑存的照片", force=True))
        report = ss.audit_library(dry_run=False)
        self.assertEqual(report["hide"], 1, report)
        self.assertEqual(ss._load_index(ttl=0.0), {})


def _b64url(raw):
    import base64
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


def jpeg(w, h, body=b""):
    sof = (b"\xff\xc0\x00\x11\x08" + bytes([h >> 8, h & 255, w >> 8, w & 255]) + b"\x03")
    return b"\xff\xd8" + sof + (body or b"\x00" * 16)


if __name__ == "__main__":
    unittest.main()
