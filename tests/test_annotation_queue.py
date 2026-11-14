# -*- coding: utf-8 -*-
"""人工标注回归锁：标完立刻写真实账本、来源必须真实、绝不补造。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import annotation_queue as aq


class KeywordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_kw_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_keywords_are_short_phrase_windows(self):
        kws = aq._keywords("我喜欢吃烤苕皮和糖炒栗子")
        self.assertTrue(kws)
        self.assertTrue(all(len(k) <= 6 for k in kws))
        self.assertTrue(any("烤苕皮" in k for k in kws))

    def test_candidate_scoring_requires_overlap(self):
        # 没有四字重合就不该给候选
        self.assertEqual(aq.candidate_events("完全无关的一句话"), [])


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_no_source_is_not_written(self):
        res = aq.submit("persona", "m1", [], note="x")
        self.assertFalse(res["ok"])

    def test_persona_submit_writes_and_logs(self):
        with mock.patch("persona_store.set_sources", return_value=True) as setter:
            res = aq.submit("persona", "m1", ["e1", "e2"], note="确认", by="panel")
        self.assertTrue(res["ok"])
        setter.assert_called_once_with("m1", ["e1", "e2"])
        ledger = aq._load_ledger()
        self.assertEqual(ledger["entries"][0]["event_ids"], ["e1", "e2"])
        self.assertEqual(ledger["entries"][0]["by"], "panel")

    def test_group_submit_routes_to_group_store(self):
        with mock.patch("group_memory_store.set_source", return_value=True) as setter:
            res = aq.submit("group", "g1", ["e9"], group_id="923")
        self.assertTrue(res["ok"])
        setter.assert_called_once_with("923", "g1", ["e9"])

    def test_missing_row_is_not_written(self):
        with mock.patch("persona_store.set_sources", return_value=False):
            self.assertFalse(aq.submit("persona", "nope", ["e1"])["ok"])


class CandidateRelevanceTests(unittest.TestCase):
    """候选必须真相关：塔罗记忆不该配到「奶茶」，图片要能看。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_cand_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        import sqlite3
        os.makedirs(os.path.join(self.tmp.name, "persona"), exist_ok=True)
        c = sqlite3.connect(os.path.join(self.tmp.name, "persona", "cognition.db"))
        c.execute("CREATE TABLE event (id TEXT, ts REAL, payload TEXT, actor_qq TEXT, session TEXT)")
        now = 1794420000.0
        c.execute("INSERT INTO event VALUES (?,?,?,?,?)",
                  ("e_tarot", now - 3600, json.dumps({"text": "帮我抽三张塔罗牌 分析一下今日运势"}),
                   "9000000003", "group-9001"))
        c.execute("INSERT INTO event VALUES (?,?,?,?,?)",
                  ("e_milk", now - 60, json.dumps({"text": "好想喝奶茶"}), "1", "group-9001"))
        c.execute("INSERT INTO event VALUES (?,?,?,?,?)",
                  ("e_img", now - 120, json.dumps({"text": "", "images": ["https://x/1.jpg"]}),
                   "2", "group-9001"))
        c.execute("INSERT INTO event VALUES (?,?,?,?,?)",
                  ("e_old", now - 20 * 86400, json.dumps({"text": "帮我抽三张塔罗牌 看看运势"}),
                   "9000000003", "group-9001"))
        c.commit()
        c.close()

    def test_tarot_memory_matches_tarot_not_milk(self):
        cands = aq.candidate_events("清漪给阿甲算塔罗，抽出过去世界逆位", ts=1794420000.0)
        ids = [c["id"] for c in cands]
        self.assertIn("e_tarot", ids)
        self.assertNotIn("e_milk", ids)

    def test_candidates_carry_the_sender(self):
        cands = aq.candidate_events("清漪给阿甲算塔罗，抽出过去世界逆位", ts=1794420000.0)
        tarot = [c for c in cands if c["id"] == "e_tarot"][0]
        self.assertTrue(tarot["actor"])

    def test_old_source_found_by_content_not_window(self):
        # 记忆是"现在"写的，但真实来源是 20 天前的聊天；内容优先必须能找到。
        cands = aq.candidate_events("清漪给阿甲算塔罗，抽出过去世界逆位", ts=1794420000.0)
        self.assertIn("e_old", [c["id"] for c in cands])

    def test_image_candidate_is_viewable_and_flagged(self):
        cands = aq.candidate_events("清漪给阿甲算塔罗，抽出过去世界逆位", ts=1794420000.0)
        img = [c for c in cands if c["id"] == "e_img"]
        self.assertTrue(img)
        self.assertTrue(img[0]["image_only"])
        self.assertEqual(img[0]["images"], ["https://x/1.jpg"])


class AutoLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_auto_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_only_verbatim_sources_are_linked(self):
        sample = {"store": "persona", "id": "m1", "text": "我喜欢吃烤苕皮",
                  "candidates": [{"id": "e1", "text": "他说我喜欢吃烤苕皮", "score": 3}]}
        with mock.patch.object(aq, "samples", return_value=[sample]), \
             mock.patch("persona_store.set_sources", return_value=True):
            res = aq.auto_link(limit=10)
        self.assertEqual(res["linked"], 1)

    def test_non_verbatim_is_left_alone(self):
        sample = {"store": "persona", "id": "m1", "text": "我喜欢吃烤苕皮",
                  "candidates": [{"id": "e1", "text": "今天天气不错", "score": 0}]}
        with mock.patch.object(aq, "samples", return_value=[sample]), \
             mock.patch("persona_store.set_sources", return_value=True) as setter:
            res = aq.auto_link(limit=10)
        self.assertEqual(res["linked"], 0)
        setter.assert_not_called()

    def test_calibration_needs_enough_manual_labels(self):
        sample = {"store": "persona", "id": "m1", "text": "我喜欢吃烤苕皮",
                  "candidates": [{"id": "e1", "text": "烤苕皮好吃", "score": 4}]}
        with mock.patch.object(aq, "samples", return_value=[sample]), \
             mock.patch("persona_store.set_sources", return_value=True):
            few = aq.auto_link(limit=10)   # 0 条人工 → 中等重合不校准
            self.assertEqual(few["linked"], 0)
            self.assertFalse(few["calibrated"])
        ledger = {"entries": [{"id": i} for i in range(5)]}
        with mock.patch.object(aq, "samples", return_value=[sample]), \
             mock.patch.object(aq, "_load_ledger", return_value=ledger), \
             mock.patch("persona_store.set_sources", return_value=True):
            many = aq.auto_link(limit=10)  # 5 条人工 → 允许校准
            self.assertEqual(many["linked"], 1)
            self.assertTrue(many["calibrated"])

    def test_high_overlap_links_without_manual(self):
        sample = {"store": "persona", "id": "m1", "text": "我喜欢吃烤苕皮",
                  "candidates": [{"id": "e1", "text": "烤苕皮好吃", "score": 8}]}
        with mock.patch.object(aq, "samples", return_value=[sample]), \
             mock.patch("persona_store.set_sources", return_value=True):
            res = aq.auto_link(limit=10)
        self.assertEqual(res["linked"], 1)

    def test_image_only_never_auto_linked(self):
        sample = {"store": "persona", "id": "m1", "text": "我喜欢吃烤苕皮",
                  "candidates": [{"id": "e1", "text": "", "score": 0,
                                  "image_only": True, "images": ["u"]}]}
        ledger = {"entries": [{"id": i} for i in range(10)]}
        with mock.patch.object(aq, "samples", return_value=[sample]), \
             mock.patch.object(aq, "_load_ledger", return_value=ledger), \
             mock.patch("persona_store.set_sources", return_value=True) as setter:
            res = aq.auto_link(limit=10)
        self.assertEqual(res["linked"], 0)
        setter.assert_not_called()


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_status_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_status_reports_real_counts(self):
        fake = {"status": "not_observed",
                "counts": {"persona_total": 10, "persona_sourced": 3,
                           "group_total": 5, "group_sourced": 0}}
        with mock.patch("qingyi_preflight._memory_provenance_check", return_value=fake):
            st = aq.status()
        mp = st["memory_provenance"]
        self.assertEqual(mp["total"], 15)
        self.assertEqual(mp["sourced"], 3)
        self.assertEqual(mp["remaining"], 12)


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_group_")
        self.addCleanup(self.tmp.cleanup)
        import group_memory_store as gms
        self.gms = gms
        self._old = gms._FILE
        gms._FILE = os.path.join(self.tmp.name, "group_facts.json")
        gms._cache.update({"data": None, "ts": 0})
        self.addCleanup(self._restore)

    def _restore(self):
        self.gms._FILE = self._old
        self.gms._cache.update({"data": None, "ts": 0})

    def test_set_source_and_unsourced(self):
        self.gms.add_entry("923", "烤苕皮", "阿甲喜欢吃烤苕皮")
        entries = self.gms.unsourced_entries(limit=5)
        self.assertEqual(len(entries), 1)
        eid = entries[0]["id"]
        self.assertTrue(self.gms.set_source("923", eid, ["e1"]))
        self.assertEqual(self.gms.unsourced_entries(limit=5), [])


class ConfirmTests(unittest.TestCase):
    """确认保留：不补造来源、不删除，只是人工复核过、不再重复待标。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_keep_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_confirm_records_review_without_touching_memory(self):
        with mock.patch("persona_store.set_sources") as setter, \
             mock.patch("persona_store.delete_by_id") as deleter:
            res = aq.confirm("persona", "m1", note="我知道是对的")
        self.assertTrue(res["ok"])
        setter.assert_not_called()
        deleter.assert_not_called()
        self.assertEqual(aq._load_ledger()["reviewed"][0]["id"], "m1")

    def test_reviewed_item_not_reshown(self):
        aq.confirm("persona", "m1")
        row = {"id": "m1", "kind": "facts", "text": "某条记忆", "ts": 1}
        with mock.patch("persona_store.unsourced_items", return_value=[row]), \
             mock.patch("group_memory_store.unsourced_entries", return_value=[]):
            self.assertEqual(aq.samples(limit=8), [])

    def test_status_counts_reviewed(self):
        aq.confirm("persona", "m1")
        fake = {"status": "not_observed", "counts": {"persona_total": 1, "persona_sourced": 0,
                                                    "group_total": 0, "group_sourced": 0}}
        with mock.patch("qingyi_preflight._memory_provenance_check", return_value=fake):
            self.assertEqual(aq.status()["reviewed"], 1)


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_learn_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_learn_from_manual_only(self):
        ledger = {"entries": [{"store": "persona", "id": "m1", "event_ids": ["e1"]}]}
        with mock.patch.object(aq, "_load_ledger", return_value=ledger), \
             mock.patch.object(aq, "_memory_text", return_value=("我喜欢吃烤苕皮", 100.0)), \
             mock.patch.object(aq, "_event_row", return_value={"ts": 90.0, "text": "烤苕皮好吃"}):
            st = aq.learn()
        self.assertEqual(st["n"], 1)
        self.assertGreaterEqual(st["median_score"], 1)

    def test_threshold_needs_enough_samples(self):
        with mock.patch.object(aq, "learn", return_value={"n": 2}):
            self.assertIsNone(aq.learned_threshold())
        with mock.patch.object(aq, "learn", return_value={"n": 8, "p25_score": 7}):
            self.assertEqual(aq.learned_threshold(), 7)


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="anno_del_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_persona_delete_logs_audit(self):
        with mock.patch("persona_store.delete_by_id", return_value=True) as d:
            res = aq.delete("persona", "m1")
        self.assertTrue(res["ok"])
        d.assert_called_once_with("m1")
        self.assertEqual(aq._load_ledger()["deletions"][0]["id"], "m1")

    def test_group_delete_routes(self):
        with mock.patch("group_memory_store.delete_entry", return_value=True) as d:
            res = aq.delete("group", "g1", group_id="923")
        self.assertTrue(res["ok"])
        d.assert_called_once_with("923", "g1")

    def test_missing_row_not_deleted(self):
        with mock.patch("persona_store.delete_by_id", return_value=False):
            self.assertFalse(aq.delete("persona", "nope")["ok"])
        self.assertEqual(aq._load_ledger().get("deletions", []), [])


if __name__ == "__main__":
    unittest.main()