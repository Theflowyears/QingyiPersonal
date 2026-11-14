"""回归锁：面板/悄悄话能给她发图。

现状取证结论是「完全没有」：
  · website/ 里没有任何图片上传路由；
  · 愿望墙接口 /admin/api/bot/wishes 只收 text（ceylan_dashboard.py:916-958）；
  · 前端是 <input type="text">（console.html:219,227）；
  · bot/whisper.py 里 image/图片/upload/base64/vision 零命中 —— 纯文本。
所以她那条「拍张你桌上的书堆发给我」的愿望根本无从发生。

同时钉住一条边界：这些是主人私人照片（桌面、饭菜、药盒），
**永不进群聊上下文**，也不存原图之外的任何长期副本。
"""
import hashlib
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

import media_upload as mu       # noqa: E402
import vision_notes as vn       # noqa: E402
import whisper as wp            # noqa: E402


class WhisperImageQueueTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wimg_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        wp._FILE = os.path.join(self.dir, "persona", "whisper.json")
        wp._cache.update({"data": None, "ts": 0.0})

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        wp._FILE = os.path.join(self.dir, "unused.json")
        wp._cache.update({"data": None, "ts": 0.0})
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_add_image_lands_in_queue_unseen(self):
        rec = wp.add_image("/x/uploads/2026-11-06/abc.jpg", by="father", caption="我桌上")
        self.assertTrue(rec.get("id"))
        self.assertFalse(rec.get("seen"))
        snap = wp.snapshot()
        self.assertEqual(len(snap.get("images") or []), 1)

    def test_take_images_only_returns_unseen_and_marks_them(self):
        wp.add_image("/x/a.jpg")
        got = wp.take_images(limit=5)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(wp.take_images(limit=5)), 0, "同一条被取走两次")

    def test_queue_is_bounded(self):
        cap = wp.IMAGE_LIMIT
        for i in range(cap + 5):
            wp.add_image("/x/%d.jpg" % i)
        self.assertLessEqual(len(wp.snapshot().get("images") or []), cap)

    def test_missing_path_is_rejected(self):
        # 队列里存的是服务器上的路径；不存在的东西不该占位
        self.assertIsNone(wp.add_image(""))

    def test_old_images_are_pruned_not_forever(self):
        cap = wp.IMAGE_LIMIT
        for i in range(cap):
            wp.add_image("/x/%d.jpg" % i)
        snap = wp.snapshot()
        self.assertLessEqual(len(snap.get("images") or []), cap)


class UploadValidationTests(unittest.TestCase):
    """上传口是最容易出事的地方：类型、大小、文件名一律不信客户端。"""

    def test_allowed_types(self):
        for name in ("a.jpg", "b.png", "c.webp", "d.jpeg"):
            self.assertTrue(mu.allowed_name(name), name)

    def test_blocked_types(self):
        for name in ("a.html", "b.svg", "c.php", "d.py", "e.sh", "f.exe", "g.jsp"):
            self.assertFalse(mu.allowed_name(name), name)

    def test_blocked_even_with_double_extension(self):
        self.assertFalse(mu.allowed_name("photo.jpg.php"))
        self.assertFalse(mu.allowed_name("photo.php.jpg"))   # 内容不是图也不能收

    def test_size_cap_enforced(self):
        big = b"\xff" * (mu.MAX_BYTES + 1)
        ok, why = mu.validate(big, "a.jpg")
        self.assertFalse(ok, why)
        self.assertIn("大小", why)

    def test_real_jpeg_magic_passes(self):
        body = b"\xff\xd8\xff\xe0" + b"0" * 2000
        ok, why = mu.validate(body, "a.jpg")
        self.assertTrue(ok, why)

    def test_fake_extension_rejected_by_magic(self):
        body = b"<html><script>alert(1)</script></html>"
        ok, why = mu.validate(body, "a.jpg")
        self.assertFalse(ok, why)

    def test_saved_name_is_hash_derived_not_user_supplied(self):
        body = b"\x89PNG\r\n\x1a\n" + b"0" * 4000
        d = tempfile.mkdtemp()
        try:
            path = mu.save_upload(body, "我的 照片(1).png", storage_dir=d)
            base = os.path.basename(path)
            self.assertNotIn("照片", base)
            self.assertTrue(base.endswith(".png"))
            # 同一张图重复上传应落到同一个名字（内容寻址，不去重也没垃圾堆积）
            path2 = mu.save_upload(body, "另一轮名字.png", storage_dir=d)
            self.assertEqual(os.path.basename(path), os.path.basename(path2))
            # 文件名里不能出现路径分隔或遍历
            self.assertNotIn("..", path)
            self.assertNotIn("/", base)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class NoteInjectionTests(unittest.TestCase):
    """发进来的图要能在私聊里被她自己说出来，但不能漏进群。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vnl_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        vn._FILE = os.path.join(self.dir, "memory", "vision_notes.json")
        vn._reset_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        vn._reset_cache()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self):
        vn.save_note("/uploads/2026-11-06/desk.jpg",
                     {"objects": ["书堆", "马克杯", "充电线"], "text_in_image": "周四截止",
                      "scene": "书桌", "mood": "乱"}, source="panel", sensitive=True, session="whisper-owner")
        vn.save_note("/uploads/2026-11-06/food.jpg",
                     {"objects": ["米饭", "青菜"], "scene": "食堂", "mood": "普通"},
                     source="panel", sensitive=True, session="whisper-owner")

    def test_latest_notes_available_for_private_turn(self):
        self._seed()
        got = vn.latest_notes(scene="private", limit=2, session="whisper-owner")
        self.assertGreaterEqual(len(got), 1)
        blob = "".join(vn.render_for_prompt(n, scene="private") for n in got)
        self.assertTrue(("书堆" in blob) or ("米饭" in blob), blob[:200])

    def test_panel_photos_never_reach_group_scene(self):
        self._seed()
        # 群里这一轮不该拿到任何私人照片的笔记
        got = vn.latest_notes(scene="group", limit=5)
        for n in got:
            self.assertFalse(n.get("sensitive"), n)
        for n in got:
            self.assertEqual(vn.render_for_prompt(n, scene="group"), ""
                             if n.get("sensitive") else vn.render_for_prompt(n, "group"))

    def test_latest_notes_bounded_by_limit_and_budget(self):
        for i in range(9):
            vn.save_note("/uploads/x%d.jpg" % i, {"objects": ["物体%d" % i],
                                                  "scene": "书桌"}, sensitive=True)
        got = vn.latest_notes(scene="private", limit=2)
        self.assertLessEqual(len(got), 2)

    def test_non_sensitive_group_note_still_light_on_material(self):
        # 群里只给情绪/梗，不给能照着念的清单（这条是第 6 批定的规矩）
        vn.save_note("/uploads/g.jpg", {"objects": ["猫", "键盘"], "mood": "好笑",
                                        "meme_guess": "打工人现状"}, sensitive=False)
        n = vn.note_for("/uploads/g.jpg")
        out = vn.render_for_prompt(n, scene="group")
        self.assertIn("好笑", out)
        self.assertNotIn("键盘", out)


if __name__ == "__main__":
    unittest.main()
