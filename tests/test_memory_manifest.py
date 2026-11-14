"""A03/A04：最终请求记忆清单、按实际入选集合去重与预算。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import memory_manifest
import memory_depth


class ComposeTests(unittest.TestCase):
    def test_duplicate_id_and_text_are_removed_before_budget(self):
        blocks = [
            {"id": "a", "text": "主人喜欢冰可乐", "score": 5},
            {"id": "a", "text": "主人喜欢冰可乐（重复ID）", "score": 9},
            {"id": "b", "text": "主人喜欢冰可乐", "score": 4},
            {"id": "c", "text": "主人住在杭州", "score": 3},
        ]
        selected, rendered, dropped = memory_manifest.compose(blocks, 1000)
        self.assertEqual(["a", "c"], [row["id"] for row in selected])
        self.assertEqual({"duplicate_id", "duplicate_text"},
                         {row["reason"] for row in dropped})
        self.assertEqual(2, rendered.count("主人"))

    def test_text_already_in_system_is_not_repeated(self):
        selected, rendered, dropped = memory_manifest.compose(
            [{"id": "p", "text": "她一直很怕被丢下", "score": 5}], 1000,
            system_text="人格设定：她一直很怕被丢下。")
        self.assertEqual([], selected)
        self.assertEqual("already_in_system", dropped[0]["reason"])

    def test_budget_drops_whole_low_score_block_instead_of_cutting_it(self):
        blocks = [
            {"id": "high", "text": "高优先" * 10, "score": 9},
            {"id": "low", "text": "低优先" * 40, "score": 1},
        ]
        selected, rendered, dropped = memory_manifest.compose(blocks, 60)
        self.assertEqual(["high"], [row["id"] for row in selected])
        self.assertNotIn("低优", rendered)
        self.assertIn({"id": "low", "reason": "budget"}, dropped)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="manifest_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_binds_request_hash_and_never_stores_raw_text(self):
        selected = [{"id": "persona:ctx", "kind": "persona",
                     "text": "她记得主人怕冷", "score": 5, "source": "memory_depth"}]
        messages = [{"role": "user", "content": "背景：她记得主人怕冷\n用户消息：冷吗"}]
        manifest = memory_manifest.record("dm", "private-user-1", messages,
                                          selected, [{"id": "x", "reason": "budget"}])
        self.assertEqual(64, len(manifest["request_sha256"]))
        self.assertTrue(manifest["selected"][0]["present"])
        self.assertEqual(len("她记得主人怕冷"), manifest["selected"][0]["chars"])
        self.assertEqual("budget", manifest["dropped"][0]["reason"])
        raw = memory_manifest._manifest_path().read_text(encoding="utf-8")
        self.assertNotIn("她记得主人怕冷", raw, "manifest 不落原文")
        self.assertEqual(manifest["request_sha256"],
                         json.loads(raw.splitlines()[-1])["request_sha256"])

    def test_manifest_marks_missing_render(self):
        selected = [{"id": "p", "kind": "persona", "text": "这句没有进请求",
                     "score": 1, "source": "memory_depth"}]
        manifest = memory_manifest.record("group", "group:1",
                                          [{"role": "user", "content": "别的内容"}],
                                          selected, [])
        self.assertFalse(manifest["selected"][0]["present"])


class MemoryDepthComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="depth_manifest_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_records_selected_blocks_and_dedupes_layers(self):
        manifest = {}
        with patch.object(memory_depth, "_deep_block", return_value="同一段记忆"), \
             patch.object(memory_depth, "_middle_block", return_value="同一段记忆"), \
             patch.object(memory_depth, "_shallow_block", return_value="刚刚发生的事"):
            text = memory_depth.build("你是谁", depth="deep", max_chars=900,
                                      manifest=manifest)
        self.assertIn("刚刚发生的事", text)
        self.assertEqual(1, text.count("同一段记忆"))
        self.assertEqual(2, len(manifest.get("selected") or []))
        self.assertEqual(["duplicate_text"],
                         [row["reason"] for row in manifest.get("dropped") or []])

    def test_build_drops_whole_block_over_budget(self):
        manifest = {}
        with patch.object(memory_depth, "_deep_block", return_value="深" * 80), \
             patch.object(memory_depth, "_middle_block", return_value="中" * 80), \
             patch.object(memory_depth, "_shallow_block", return_value="浅" * 80):
            text = memory_depth.build("你是谁", depth="deep", max_chars=100,
                                      manifest=manifest)
        self.assertEqual(1, len(manifest["selected"]))
        self.assertEqual(2, len(manifest["dropped"]))
        self.assertNotIn("中" * 80, text)


class RuntimeMetadataTests(unittest.TestCase):
    def test_runtime_manifest_preserves_scope_and_query_without_raw_text(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'DATA_DIR':root}):
            got=memory_manifest.record('group','group:7',[{'role':'user','content':'冰可乐'}],
                          [{'id':'m1','kind':'reported','text':'冰可乐','source':'memory_runtime',
                            'source_session':'group:7','visibility':'group','root_source_ids':['e1']}],[],
                          runtime={'queries':[{'kind':'original','text':'私密查询'}],
                                   'status':'partial','filtered':{'out_of_scope':1},'correction':'ambiguous'})
        self.assertEqual('group',got['selected'][0]['visibility'])
        self.assertEqual(['e1'],got['selected'][0]['root_source_ids'])
        self.assertEqual('partial',got['runtime']['status'])
        self.assertNotIn('私密查询',str(got))


if __name__ == "__main__":
    unittest.main()