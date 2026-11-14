"""回归锁：斗图库只收「表情」，不收照片，且同一张图不会存两遍。

用户报的三件事都在这里锁住：
  1) 群里发的照片/截图被当成表情包载入 —— 以前**没有任何**判定，
     唯一门槛是 8MB 与每日 20 张，所以来者不拒；
  2) 重复条目 —— 去重函数只被一个生产零调用的入口用过，
     在用的那条路每次都是新 uuid、也不写哈希；
  3) 收藏表情图标损坏 —— 见 test_qq_face_proxy，图片必须走服务端代理，
     不能让浏览器直连 QQ CDN（防盗链 + 面板是 https 隧道）。
"""
import asyncio
import base64
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import sticker_store as ss        # noqa: E402


def png(w, h, body=b""):
    import struct
    head = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00")
    return head + body


def gif(w, h, frames=2, body=b""):
    data = (b"GIF89a" + bytes([w & 255, w >> 8, h & 255, h >> 8])
            + b"\x00\x00\x00")
    # 真 GIF 头部不止 12 字节；填到 16 以上，_dims 才不会当成「太短」
    return data + (body or b"\x21\xf9\x04") + b"\x00" * 8


def jpeg(w, h, body=b""):
    # SOI + SOF0（长度 0x0011、精度 8、高 2 字节、宽 2 字节）
    sof = (b"\xff\xc0\x00\x11\x08" + bytes([h >> 8, h & 255, w >> 8, w & 255])
           + b"\x03")
    # 真 JPEG 远不止 12 字节，补一段假数据，免得被「太短」先挡掉
    return b"\xff\xd8" + sof + (body or b"\x00" * 16)


def webp(w, h, body=b""):
    import struct
    payload = (b"VP8X\x0a\x00\x00\x00\x00\x00\x00\x00"
               + struct.pack("<I", (w - 1) & 0xffffff)[:3]
               + struct.pack("<I", (h - 1) & 0xffffff)[:3])
    return b"RIFF\x00\x00\x00\x00WEBP" + payload + body


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="stickergate_")
        self._old_env = {k: os.environ.get(k) for k in
                         ("DATA_DIR", "STICKER_DIR", "STICKER_STEAL_MAX_PX",
                          "STICKER_STEAL_MAX_BYTES", "STICKER_STEAL")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("STICKER_DIR", None)
        for k in ("STICKER_STEAL_MAX_PX", "STICKER_STEAL_MAX_BYTES", "STICKER_STEAL"):
            os.environ.pop(k, None)
        ss.STICKER_DIR = Path(self.dir) / "stickers"
        ss._index_cache.update({"data": None, "ts": 0.0})

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ss.STICKER_DIR = Path(self._old_env.get("STICKER_DIR")
                              or Path(os.environ.get("DATA_DIR", "./data")) / "stickers")
        ss._index_cache.update({"data": None, "ts": 0.0})
        shutil.rmtree(self.dir, ignore_errors=True)

    def dataurl(self, raw, mime="image/png"):
        return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


class DimensionTests(Base):
    def test_reads_size_from_headers_without_pillow(self):
        self.assertEqual(ss._dims(png(120, 80)), (120, 80, "png"))
        self.assertEqual(ss._dims(gif(64, 48)), (64, 48, "gif"))
        self.assertEqual(ss._dims(jpeg(800, 600)), (800, 600, "jpg"))
        self.assertEqual(ss._dims(webp(300, 200)), (300, 200, "webp"))

    def test_unreadable_bytes_are_not_a_sticker(self):
        self.assertEqual(ss._dims(b""), (0, 0, ""))
        self.assertEqual(ss._dims(b"GARBAGE" * 10), (0, 0, ""))


class GateTests(Base):
    def test_small_gif_png_are_stickers(self):
        ok, why = ss.looks_like_sticker(gif(200, 200, body=b"x" * 20000))
        self.assertTrue(ok, why)
        ok, why = ss.looks_like_sticker(png(240, 180, body=b"x" * 30000))
        self.assertTrue(ok, why)

    def test_photo_and_screenshot_are_refused(self):
        cases = [("手机照片 jpeg", jpeg(4000, 3000, body=b"y" * 900000)),
                 ("长截图 png", png(1080, 2400, body=b"y" * 300000)),
                 ("大 webp", webp(1200, 900, body=b"y" * 400000))]
        for name, raw in cases:
            with self.subTest(name):
                ok, why = ss.looks_like_sticker(raw)
                self.assertFalse(ok, name)
                self.assertTrue(why)

    def test_unknown_bytes_refused_with_reason(self):
        ok, why = ss.looks_like_sticker(b"\x00" * 5000)
        self.assertFalse(ok)
        self.assertIn("认不出", why)

    def test_thresholds_can_be_tightened_by_env(self):
        os.environ["STICKER_STEAL_MAX_PX"] = "100"
        ok, _ = ss.looks_like_sticker(png(240, 180, body=b"x" * 30000))
        self.assertFalse(ok)


class StoreTests(Base):
    def test_non_sticker_bytes_are_never_saved(self):
        rec = asyncio.run(ss.save_sticker_from_dataurl(
            self.dataurl(jpeg(3000, 2000, body=b"z" * 800000))))
        self.assertIsNone(rec)
        self.assertEqual(ss._load_index(), {})

    def test_same_image_is_not_stored_twice(self):
        raw = gif(200, 200, body=b"a" * 40000)
        first = asyncio.run(ss.save_sticker_from_dataurl(
            self.dataurl(raw, "image/gif")))
        second = asyncio.run(ss.save_sticker_from_dataurl(
            self.dataurl(raw, "image/gif")))
        self.assertIsNotNone(first)
        self.assertTrue(second.get("deduped"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(ss._load_index()), 1)
        self.assertEqual(ss._load_index()[first["id"]]["seen"], 2)

    def test_index_records_hash_and_gate(self):
        raw = png(180, 120, body=b"b" * 25000)
        rec = asyncio.run(ss.save_sticker_from_dataurl(self.dataurl(raw)))
        self.assertTrue(rec.get("hash"))
        self.assertEqual(rec["dims"], [180, 120])
        self.assertTrue((ss.STICKER_DIR / rec["file"]).is_file())


class StealWiringTests(Base):
    """谁自动收图必须过门，谁说「收藏这张」才可以绕过门。"""

    @classmethod
    def setUpClass(cls):
        import inspect
        import qq_openclaw_bridge as b
        cls.auto = inspect.getsource(b.remember_group_images)
        with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_auto_collect_never_bypasses_the_gate(self):
        self.assertNotIn("force=True", self.auto)
        self.assertIn("save_sticker_from_dataurl", self.auto)

    def test_explicit_save_command_forces(self):
        start = self.src.index('if kind == "save":')
        blk = self.src[start:start + 900]
        self.assertIn("force=True", blk, "人工说「收藏这张」不该被尺寸门槛挡住")

    def test_taught_meaning_forces(self):
        start = self.src.index("含义是别人教的")
        blk = self.src[start - 200:start + 200]
        self.assertIn("force=True", blk)


class AuditTests(Base):
    def _seed_mixed(self):
        idx = {}
        for name, raw, mime in (("sticker.gif", gif(200, 200, body=b"c" * 40000), "image/gif"),
                                ("photo.jpg", jpeg(3000, 2000, body=b"d" * 700000), "image/jpeg")):
            rec = {"id": name.split(".")[0], "file": name,
                   "abs": str(ss.STICKER_DIR / name), "tags": [], "note": "",
                   "mime": mime, "bytes": len(raw), "added": 1, "used": 0}
            ss.STICKER_DIR.mkdir(parents=True, exist_ok=True)
            (ss.STICKER_DIR / name).write_bytes(raw)
            idx[rec["id"]] = rec
        ss._save_index(idx)

    def test_dry_run_only_counts(self):
        self._seed_mixed()
        report = ss.audit_library()
        self.assertEqual(report["keep"], 1)
        self.assertEqual(report["hide"], 1)
        self.assertEqual(len(ss._load_index()), 2, "试运行不许动库")

    def test_hide_moves_files_aside_instead_of_deleting(self):
        self._seed_mixed()
        report = ss.audit_library(dry_run=False)
        idx = ss._load_index()
        self.assertEqual(list(idx), ["sticker"])
        self.assertFalse((ss.STICKER_DIR / "photo.jpg").exists())
        moved = list((ss.STICKER_DIR / report["folder"]).glob("*"))
        self.assertEqual([p.name for p in moved], ["photo.jpg"], "移走不是删除")
        ledger = json.loads((ss.STICKER_DIR / "excluded.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger[0]["file"], "photo.jpg")


class LostUpdateTests(Base):
    """两次收藏同时在跑，不能把对方那条从索引里抹掉。

    线上实测到 2 个「文件在、索引里没有」的孤儿（2026-11-07）。原因是
    `save_sticker_from_dataurl` 在 `await describe_meaning()`（一次几十秒的
    视觉调用）之前就把 idx 读进来了，回来直接整包覆盖写盘 ——
    后完成的那次连前一次的记录一起冲掉。
    """

    def setUp(self):
        super().setUp()
        self._real_meaning = ss.describe_meaning

        async def slow_meaning(path):
            await asyncio.sleep(0)      # 真的让出控制权，复现网络等待
            await asyncio.sleep(0)
            return ""

        ss.describe_meaning = slow_meaning

    def tearDown(self):
        ss.describe_meaning = self._real_meaning
        super().tearDown()

    def test_two_saves_at_once_keep_both_records(self):
        a, b = gif(200, 200, body=b"u" * 40000), png(180, 120, body=b"v" * 25000)

        async def both():
            return await asyncio.gather(
                ss.save_sticker_from_dataurl(self.dataurl(a, "image/gif")),
                ss.save_sticker_from_dataurl(self.dataurl(b, "image/png")))

        got = asyncio.run(both())
        idx = ss._load_index(ttl=0.0)
        self.assertEqual(len(idx), 2, "并发收藏丢了一条：%s" % [g and g["file"] for g in got])
        for rec in got:
            self.assertIn(rec["file"], {v["file"] for v in idx.values()})

    def test_no_file_is_left_without_an_index_record(self):
        a, b = gif(200, 200, body=b"w" * 40000), png(160, 160, body=b"x" * 25000)

        async def both():
            return await asyncio.gather(
                ss.save_sticker_from_dataurl(self.dataurl(a, "image/gif")),
                ss.save_sticker_from_dataurl(self.dataurl(b, "image/png")))

        asyncio.run(both())
        on_disk = {p.name for p in ss.STICKER_DIR.iterdir() if p.suffix.lower() in
                   (".gif", ".png", ".jpg", ".jpeg", ".webp")}
        indexed = {v["file"] for v in ss._load_index(ttl=0.0).values()}
        self.assertEqual(on_disk - indexed, set(), "落盘了却没进索引 = 她永远不会用到这张")


class OrphanTests(Base):
    """索引和磁盘对不上时，audit 要把能用的那张找回来，而不是当它不存在。"""

    def _seed_orphans(self):
        ss.STICKER_DIR.mkdir(parents=True, exist_ok=True)
        good = png(150, 130, body=b"o" * 25000)
        photo = jpeg(3000, 2000, body=b"p" * 700000)
        (ss.STICKER_DIR / "good.png").write_bytes(good)
        (ss.STICKER_DIR / "photo.jpg").write_bytes(photo)
        ss._save_index({})

    def test_dry_run_reports_orphans_without_touching_them(self):
        self._seed_orphans()
        report = ss.audit_library()
        self.assertEqual(report["orphans"], 2)
        self.assertEqual(len(list(ss.STICKER_DIR.glob("*.png"))), 1)
        self.assertEqual(ss._load_index(ttl=0.0), {})

    def test_orphans_passing_the_gate_come_back(self):
        self._seed_orphans()
        report = ss.audit_library(dry_run=False)
        idx = ss._load_index(ttl=0.0)
        self.assertEqual([v["file"] for v in idx.values()], ["good.png"])
        self.assertTrue(report["adopted"])
        self.assertFalse((ss.STICKER_DIR / "photo.jpg").exists())
        self.assertTrue((ss.STICKER_DIR / report["folder"] / "photo.jpg").is_file())


class OneFingerprintTests(Base):
    """一条库只能有一种指纹。

    用户报「有重复的」，根因不只是没去重：库里同时存在两套哈希
    （sha1 全字节 与 md5(前 4KB+长度)），比来比去永远对不上，
    于是去重形同不存在，`find_by_hash` 也永远是 None。
    """

    def test_record_hash_is_the_content_hash(self):
        raw = gif(200, 200, body=b"q" * 40000)
        rec = asyncio.run(ss.save_sticker_from_dataurl(
            self.dataurl(raw, "image/gif")))
        self.assertEqual(rec["hash"], ss._content_hash(raw))

    def test_find_by_hash_actually_finds_it(self):
        raw = png(180, 140, body=b"r" * 25000)
        rec = asyncio.run(ss.save_sticker_from_dataurl(self.dataurl(raw)))
        path = str(ss.STICKER_DIR / rec["file"])
        found = ss.find_by_hash(path)
        self.assertIsNotNone(found, "按内容认图必须认得出来")
        self.assertEqual(found["id"], rec["id"])

    def test_legacy_hash_helpers_are_gone(self):
        # 留着它们，下一个入口就会又写一种指纹
        self.assertFalse(hasattr(ss, "_norm_hash"))
        self.assertFalse(hasattr(ss, "_existing_hashes"))

    def test_every_writer_goes_through_save_raw(self):
        """所有会往库里写的路都得汇到 _save_raw，不然修一处漏一处。"""
        import inspect
        for name in ("save_sticker_from_dataurl", "steal_from_url", "import_qq_faces"):
            src = inspect.getsource(getattr(ss, name))
            self.assertIn("_save_raw", src, "%s 还在自己写库" % name)
        body = inspect.getsource(ss.import_qq_faces)
        self.assertIn("force=True", body, "收藏是她自己挑的，不该被照片门挡住")


if __name__ == "__main__":
    unittest.main()