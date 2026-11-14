"""人工筛选与认知校准：删掉不该用的那张，把她对表情包的总结改对。

主人的原话：
    「加一个能够手动删除她添加的表情包的功能，我要人工筛选；
      再增加一个可以校准她的表情包认知的（列出她对表情包的总结，然后由我修改，
      通过点击对应的表情包展示悬浮窗，不要直接在界面展示）」

这一轮先把「写进去的东西真的会被用」钉住 —— 以前 `meaning` 字段只有人写、没人读，
面板改了也只是改一份文件，她发表情还是 `pick_random()` 瞎抽：

  1. 删除必须能撤销回来一点（移进回收而不是直接 unlink），而且只认索引里的 id，
     路径要确认还在库目录内 —— 不能让人一个点击把库外的文件删掉。
  2. 人工校准优先于机器描述（`meaning_src=human` 的不能被 vision 覆盖）。
  3. 她选图时要看见「库里每张图是什么意思」，校准过的那张要按校准的说。
  4. 别人甩来一张她库里的表情，命中内容指纹时直接用校准过的含义，不再当场猜。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import sticker_store as ss        # noqa: E402


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
       b"\x08\x06\x00\x00\x00\xea\x0d\x1a\x0c\x00\x00\x00\x19IDATx\x9cc\xfc\xcf"
       b"\xc0\x00\x04\x00\xff\x00\x8cx\xa3\x0e\x1c\x00\x00\x00\x00IEND\xaeB`\x82")
PNG2 = PNG + b"\x00" * 16


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="stkcog_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "STICKER_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("STICKER_DIR", None)
        ss.STICKER_DIR = Path(self.dir) / "stickers"
        ss._index_cache.update({"data": None, "ts": 0.0})
        self._dm = ss.describe_meaning

        async def blind(messages, **kw):
            return ""
        ss.describe_meaning = blind

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

    def add(self, raw, sid=None, meaning="", tags=("惊讶",)):
        import asyncio
        got = asyncio.new_event_loop().run_until_complete(
            ss.save_sticker_from_dataurl("data:image/png;base64," +
                                         __import__("base64").b64encode(raw).decode(),
                                         tags=list(tags)))
        self.assertTrue(got.get("id"), "存不进去")
        if meaning:
            idx = ss._load_index()
            rec = idx[got["id"]]
            rec["meaning"] = meaning
            rec["meaning_src"] = "vision"
            ss._save_index(idx)
        return got["id"]


class DeleteTests(Base):
    def test_delete_moves_to_trash_instead_of_erasing(self):
        """人工筛选会误点。文件进回收目录，索引立刻消失，她下次抽不到就够。"""
        sid = self.add(PNG)
        abs_ = ss.sticker_path(sid)
        self.assertTrue(os.path.isfile(abs_))
        got = ss.delete_sticker(sid)
        self.assertTrue(got.get("ok"), got)
        self.assertFalse(os.path.isfile(abs_), "文件还在，她还会抽到这张")
        self.assertFalse(ss.sticker_path(sid), "索引还认得这张，她就还能发出去")
        trash = Path(self.dir) / "stickers_trash"
        self.assertTrue(trash.is_dir() and any(trash.iterdir()),
                        "删除没留回收，误点就找不回来")

    def test_delete_rejects_unknown_id(self):
        got = ss.delete_sticker("deadbeef")
        self.assertFalse(got.get("ok"))

    def test_trash_is_bounded(self):
        for i in range(30):
            sid = self.add(PNG if i % 2 else PNG2)
            ss.delete_sticker(sid)
        trash = list((Path(self.dir) / "stickers_trash").iterdir())
        self.assertLessEqual(len(trash), ss.TRASH_KEEP,
                             "回收目录不设上限，早晚会撑爆盘")


class MeaningTests(Base):
    def test_human_calibration_wins_over_vision(self):
        sid = self.add(PNG, meaning="她以为是惊讶，其实是无语")
        idx = ss._load_index()
        idx[sid]["meaning_src"] = "human"
        idx[sid]["meaning"] = "无语，不是惊讶"
        ss._save_index(idx)
        self.assertFalse(ss.set_meaning(sid, "惊讶", source="vision"),
                         "机器描述把人工校准盖掉了")
        self.assertEqual(ss._load_index()[sid]["meaning"], "无语，不是惊讶")

    def test_human_can_overwrite_vision(self):
        sid = self.add(PNG, meaning="惊讶")
        self.assertTrue(ss.set_meaning(sid, "其实是吓一跳", source="human"))
        rec = ss._load_index()[sid]
        self.assertEqual(rec["meaning_src"], "human")

    def test_clearing_a_meaning_is_an_explicit_human_choice(self):
        """「这张没有固定含义」也是校准结果，要留住，不能被机器再填一遍。"""
        sid = self.add(PNG, meaning="惊讶")
        self.assertTrue(ss.set_meaning(sid, "", source="human"))
        rec = ss._load_index()[sid]
        self.assertEqual(rec.get("meaning"), "")
        self.assertEqual(rec.get("meaning_src"), "human")
        self.assertFalse(ss.set_meaning(sid, "惊讶", source="vision"))


class CatalogTests(Base):
    def test_catalog_is_what_she_sees_when_choosing(self):
        sid = self.add(PNG)
        ss.set_meaning(sid, "无语凝噎", source="human")
        cat = ss.catalog_for_prompt()
        self.assertIn("无语凝噎", cat["text"])
        self.assertIn(sid[:8], cat["text"])
        self.assertEqual(cat["labeled"], 1)

    def test_catalog_marks_calibrated_ones(self):
        sid = self.add(PNG)
        ss.set_meaning(sid, "无语", source="human")
        self.assertIn("校准", ss.catalog_for_prompt()["text"])

    def test_catalog_skips_meaningless_entries(self):
        self.add(PNG)
        cat = ss.catalog_for_prompt()
        self.assertEqual(cat["labeled"], 0)
        self.assertEqual(cat["text"], "")


class MeaningForImageTests(Base):
    def test_received_sticker_uses_calibrated_meaning(self):
        """别人甩她一张她库里的表情：命中内容指纹就按校准过的意思回。"""
        sid = self.add(PNG)
        ss.set_meaning(sid, "这是无语，不是惊讶", source="human")
        got = ss.meaning_for_bytes(PNG)
        self.assertEqual(got.get("meaning"), "这是无语，不是惊讶")
        self.assertEqual(got.get("id"), sid)

    def test_unknown_image_has_no_forced_meaning(self):
        self.add(PNG)
        self.assertEqual(ss.meaning_for_bytes(b"GIF89a" + os.urandom(64)), {})


class TeachingRankTests(Base):
    """可信度分三档：主人校准 > 群友教的 > 她自己看的。

    以前群里任何人甩一句「这表情是XX」也写 source="human"，
    跟主人在面板上点出来的校准同级 —— 谁最后说话谁覆盖。
    """

    def test_chat_cannot_overwrite_human(self):
        sid = self.add(PNG)
        ss.set_meaning(sid, "主人定的：无语", source="human")
        self.assertFalse(ss.set_meaning(sid, "群友说的：惊讶", source="chat"))
        self.assertEqual(ss._load_index()[sid]["meaning"], "主人定的：无语")

    def test_vision_cannot_overwrite_chat(self):
        sid = self.add(PNG)
        ss.set_meaning(sid, "有人教过：无奈", source="chat")
        self.assertFalse(ss.set_meaning(sid, "她自己猜：开心", source="vision"))
        self.assertEqual(ss._load_index()[sid]["meaning_src"], "chat")

    def test_chat_may_refine_chat(self):
        sid = self.add(PNG)
        ss.set_meaning(sid, "第一次教的", source="chat")
        self.assertTrue(ss.set_meaning(sid, "后来又说清了", source="chat"))
        self.assertEqual(ss._load_index()[sid]["meaning"], "后来又说清了")

    def test_tiers_are_marked_apart_in_her_list(self):
        human_sid = self.add(PNG)
        chat_sid = self.add(PNG2)
        ss.set_meaning(human_sid, "主人定的", source="human")
        ss.set_meaning(chat_sid, "别人教的", source="chat")
        text = ss.catalog_for_prompt()["text"]
        self.assertIn("主人校准过", text)
        self.assertIn("有人教过她", text)
        self.assertLess(text.index(human_sid[:8]), text.index(chat_sid[:8]))


if __name__ == "__main__":
    unittest.main()
