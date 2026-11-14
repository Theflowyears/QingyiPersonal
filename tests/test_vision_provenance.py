import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import vision_notes as vn


class IdentityTests(unittest.TestCase):
    def test_long_file_ids_with_the_same_prefix_keep_distinct_notes(self):
        prefix = "https://multimedia.nt.qq.com.cn/download?fileid=" + "A" * 100
        self.assertNotEqual(vn.key_of(prefix + "1"), vn.key_of(prefix + "2"))

    def test_query_identifiers_are_namespaced_by_host(self):
        self.assertNotEqual(vn.key_of("https://one.example/download?fileid=ABC"),
                            vn.key_of("https://two.example/download?fileid=ABC"))

    def test_same_filename_on_different_hosts_or_paths_is_distinct(self):
        refs = ("https://one.example/albums/a/photo.jpg",
                "https://two.example/albums/a/photo.jpg",
                "https://one.example/albums/b/photo.jpg")
        self.assertEqual(len({vn.key_of(ref) for ref in refs}), 3)

    def test_ordinary_url_keeps_meaningful_query_parameters(self):
        a = "https://one.example/photo.jpg?variant=original"
        b = "https://one.example/photo.jpg?variant=thumbnail"
        self.assertNotEqual(vn.key_of(a), vn.key_of(b))

    def test_expiry_and_fragment_changes_do_not_change_identity(self):
        pairs = (
            ("https://one.example/download?fileid=ABC&t=1",
             "https://one.example/download?fileid=ABC&t=2"),
            ("https://one.example/a/photo.jpg?expires=1&signature=old#first",
             "https://one.example/a/photo.jpg?signature=new&expires=2#second"),
            ("https://one.example/a/photo.jpg#first",
             "https://one.example/a/photo.jpg#second"),
        )
        for a, b in pairs:
            with self.subTest(a=a):
                self.assertEqual(vn.key_of(a), vn.key_of(b))


class ProvenanceTests(unittest.TestCase):
    OWNER = "private-user-101"
    OTHER = "private-user-202"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vision_provenance_")
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        vn._reset_cache()

    def tearDown(self):
        vn._reset_cache()
        self.env.stop()
        self.tmp.cleanup()

    def test_owner_and_visibility_survive_reload(self):
        vn.save_note("private.jpg", {"mood": "平静"}, session=self.OWNER)
        vn.save_note("group.jpg", {"mood": "好笑"}, session="group:1")
        vn._reset_cache()
        private = vn.note_for("private.jpg")
        self.assertEqual(private.get("session"), self.OWNER)
        self.assertEqual(private.get("visibility"), "private")
        self.assertFalse(private["sensitive"])
        self.assertEqual(vn.note_for("group.jpg").get("visibility"), "group")

    def test_ownerless_new_public_notes_remain_group_renderable(self):
        vn.save_note("public.jpg", {"mood": "好笑", "objects": ["键盘"]}, sensitive=False)
        got = vn.note_for("public.jpg")
        self.assertEqual(got.get("visibility"), "group")
        self.assertIn("好笑", vn.render_for_prompt(got, scene="group"))
        self.assertNotIn("键盘", vn.render_for_prompt(got, scene="group"))

    def test_copy_between_conflicting_private_owners_stays_quarantined(self):
        vn.save_note("source.jpg", {"mood": "原会话"}, session=self.OWNER, sensitive=True)
        vn.save_note("target.jpg", {"mood": "目标会话"}, session=self.OTHER, sensitive=True)
        vn.save_note("target.jpg", vn.note_for("source.jpg"), session=self.OTHER,
                     sensitive=False)
        got = vn.note_for("target.jpg", scene="private", session=self.OTHER)
        self.assertTrue(got is None or got["mood"] == "目标会话")

    def test_private_recency_only_returns_the_explicit_session(self):
        vn.save_note("own.jpg", {"mood": "平静"}, session=self.OWNER, sensitive=True)
        vn.save_note("other.jpg", {"mood": "开心"}, session=self.OTHER, sensitive=True)
        vn.save_note("group.jpg", {"mood": "好笑"}, session="group:1")
        got = vn.latest_notes(scene="private", limit=10, session=self.OWNER)
        self.assertEqual([n["mood"] for n in got], ["平静"])
        self.assertEqual(vn.latest_notes(scene="private", session="private-user-303"), [])

    def test_private_recency_without_session_fails_closed(self):
        vn.save_note("sensitive.jpg", {"mood": "私人照片"}, session=self.OWNER,
                     sensitive=True)
        vn.save_note("private.jpg", {"mood": "私聊图片"}, session=self.OTHER)
        self.assertEqual(vn.latest_notes(scene="private"), [])

    def test_scoped_ref_and_sha_lookups_isolate_private_notes(self):
        for sensitive in (False, True):
            with self.subTest(sensitive=sensitive):
                vn.save_note("private.jpg", {"mood": "安静"}, session=self.OWNER,
                             sensitive=sensitive, content_sha="private-sha")
                for lookup, value in ((vn.note_for, "private.jpg"),
                                      (vn.note_by_sha, "private-sha")):
                    self.assertEqual(lookup(value)["mood"], "安静")
                    self.assertEqual(lookup(value, scene="private", session=self.OWNER)["mood"],
                                     "安静")
                    self.assertIsNone(lookup(value, scene="private", session=self.OTHER))
                    self.assertIsNone(lookup(value, scene="private", session=""))
                    self.assertIsNone(lookup(value, scene="group", session=self.OWNER))
                self.assertEqual(vn.latest_notes(scene="group"), [])

    def test_public_notes_remain_reusable_across_conversations(self):
        vn.save_note("sticker.jpg", {"mood": "好笑"}, session="group:1",
                     content_sha="public-sha")
        for lookup, value in ((vn.note_for, "sticker.jpg"),
                              (vn.note_by_sha, "public-sha")):
            self.assertEqual(lookup(value, scene="group", session="group:2")["mood"], "好笑")
            self.assertEqual(lookup(value, scene="private", session=self.OTHER)["mood"], "好笑")

    def test_sha_lookup_skips_inaccessible_matches(self):
        vn.save_note("secret.jpg", {"mood": "私聊"}, session=self.OWNER,
                     sensitive=True, content_sha="same-sha")
        vn.save_note("public.jpg", {"mood": "公开"}, session="group:1",
                     content_sha="same-sha")
        self.assertEqual(vn.note_by_sha("same-sha", scene="group")["mood"], "公开")

    def test_sensitive_cannot_be_downgraded_on_the_same_key(self):
        vn.save_note("secret.jpg", {"mood": "私人照片"}, session=self.OWNER, sensitive=True)
        vn.save_note("secret.jpg", {"mood": "新笔记", "sensitive": False},
                     session=self.OTHER, sensitive=False)
        got = vn.note_for("secret.jpg")
        self.assertTrue(got["sensitive"])
        self.assertEqual(got.get("session"), self.OWNER)
        self.assertEqual(got.get("visibility"), "private")
        self.assertEqual(vn.render_for_prompt(got, scene="group"), "")

    def test_cross_session_overwrite_cannot_replace_the_owners_observation(self):
        vn.save_note("private.jpg", {"mood": "原会话观察"}, session=self.OWNER, sensitive=True)
        vn.save_note("private.jpg", {"mood": "其它会话观察"}, session=self.OTHER,
                     sensitive=False)
        got = vn.note_for("private.jpg", scene="private", session=self.OWNER)
        self.assertEqual(got["mood"], "原会话观察")

    def test_sensitive_same_owner_update_keeps_classification(self):
        vn.save_note("secret.jpg", {"mood": "旧观察"}, session=self.OWNER, sensitive=True)
        vn.save_note("secret.jpg", {"mood": "新观察"}, session=self.OWNER, sensitive=False)
        got = vn.note_for("secret.jpg", scene="private", session=self.OWNER)
        self.assertEqual(got["mood"], "新观察")
        self.assertTrue(got["sensitive"])

    def test_explicit_false_does_not_declassify_sensitive_input(self):
        vn.save_note("secret.jpg", {"mood": "私人照片", "sensitive": True},
                     session=self.OWNER, sensitive=False)
        self.assertTrue(vn.note_for("secret.jpg")["sensitive"])

    def test_copy_cannot_declassify_or_transfer_private_ownership(self):
        for sensitive in (False, True):
            with self.subTest(sensitive=sensitive):
                vn.save_note("source.jpg", {"mood": "私人照片"}, session=self.OWNER,
                             sensitive=sensitive, content_sha="source-sha")
                copied = vn.note_by_sha("source-sha")
                vn.save_note("copy.jpg", copied, session=self.OTHER, sensitive=False,
                             content_sha="copy-sha")
                got = vn.note_for("copy.jpg")
                self.assertEqual(got.get("session"), self.OWNER)
                self.assertEqual(got.get("visibility"), "private")
                self.assertEqual(got["sensitive"], sensitive)
                self.assertEqual(vn.render_for_prompt(got, scene="group"), "")
                self.assertIsNone(vn.note_by_sha("copy-sha", scene="private", session=self.OTHER))
                self.assertIsNone(vn.note_for("copy.jpg", scene="group"))

    def test_legacy_ownerless_notes_are_quarantined_not_deleted(self):
        for sensitive in (False, True):
            with self.subTest(sensitive=sensitive):
                key = vn.save_note("legacy.jpg", {"mood": "旧笔记"},
                                   sensitive=sensitive, content_sha="legacy-sha")
                data = vn.all_notes()
                data[key].pop("session", None)
                data[key].pop("visibility", None)
                vn._save(data)
                self.assertEqual(vn.note_for("legacy.jpg")["mood"], "旧笔记")
                self.assertEqual(vn.note_by_sha("legacy-sha")["mood"], "旧笔记")
                self.assertEqual(vn.latest_notes(scene="private", session=self.OWNER), [])
                self.assertEqual(vn.latest_notes(scene="group"), [])
                self.assertIsNone(vn.note_for("legacy.jpg", scene="private", session=self.OWNER))
                self.assertIsNone(vn.note_by_sha("legacy-sha", scene="group"))
                vn.save_note("legacy-copy.jpg", vn.note_for("legacy.jpg"),
                             session=self.OTHER, sensitive=False)
                self.assertIsNone(vn.note_for("legacy-copy.jpg", scene="private", session=self.OTHER))
                self.assertIn(key, vn.all_notes())

    def test_ownerless_sensitive_note_copy_cannot_acquire_a_new_owner(self):
        vn.save_note("source.jpg", {"mood": "旧照片", "sensitive": True})
        vn.save_note("copy.jpg", vn.note_for("source.jpg"),
                     session=self.OTHER, sensitive=False)
        got = vn.note_for("copy.jpg")
        self.assertTrue(got["sensitive"])
        self.assertEqual(got.get("session"), "")
        self.assertIsNone(vn.note_for("copy.jpg", scene="private", session=self.OTHER))

    def test_explicit_private_visibility_is_preserved_when_saving(self):
        vn.save_note("private.jpg", {"mood": "安静", "visibility": "private",
                                     "session": self.OWNER}, session="group:1")
        got = vn.note_for("private.jpg")
        self.assertEqual(got.get("visibility"), "private")
        self.assertEqual(got.get("session"), self.OWNER)
        self.assertIsNone(vn.note_for("private.jpg", scene="group"))

    def test_new_private_observation_uses_the_explicit_session(self):
        vn.save_note("private.jpg", {"mood": "安静", "visibility": "private"},
                     session=self.OWNER)
        got = vn.note_for("private.jpg")
        self.assertEqual(got.get("session"), self.OWNER)
        self.assertEqual(got.get("visibility"), "private")

    def test_private_recency_preserves_age_order_and_limit(self):
        now = time.time()
        for ref, age, mood in (("old.jpg", 8 * 86400, "过期"),
                               ("first.jpg", 20, "较早"),
                               ("second.jpg", 10, "最新")):
            vn.save_note(ref, {"mood": mood}, session=self.OWNER, ts=now - age)
        got = vn.latest_notes("private", 1, 7, self.OWNER)
        self.assertEqual([n["mood"] for n in got], ["最新"])


class ParseProvenanceTests(unittest.TestCase):
    def test_service_failures_are_not_chinese_observations(self):
        for raw in ("unavailable", "模型 unavailable，请稍后再试", "[空响应]",
                    "无法读取图片，请重试", "图片下载失败", "模型暂不可用",
                    '{"error":"无法读取图片"}', '{"error":{"message":"图片下载失败"}}',
                    '{"mood":"模型暂不可用"}', '{"mood":"[空响应]"}'):
            with self.subTest(raw=raw):
                self.assertIsNone(vn.parse_note(raw))

    def test_json_error_only_with_no_known_marker_is_not_prose(self):
        for raw in ('{"error":"请求被拒绝"}', '{"error":"请求被拒绝"',
                    '{"error":{"mood":"请求被拒绝"',
                    '{"error":"请求被拒绝","mood":"困惑"}'):
            with self.subTest(raw=raw):
                self.assertIsNone(vn.parse_note(raw))

    def test_partial_notes_without_meaning_are_not_successes(self):
        for raw in ('{"objects":["猫"]}', '{"objects":["猫"],"text_in_image":"你好"',
                    '{"scene":"房间"}', '{"people_action":"坐着"}',
                    '{"text_in_image":"你好"}', '{"mood":"  "}',
                    '{"mood":{},"scene":"房间"}', '{"objects":[""],"scene":"房间"}'):
            with self.subTest(raw=raw):
                self.assertIsNone(vn.parse_note(raw))

    def test_objects_and_scene_are_a_usable_observation(self):
        got = vn.parse_note('{"objects":["杯子"],"scene":"桌面"}')
        self.assertEqual(got["objects"], ["杯子"])
        self.assertEqual(got["scene"], "桌面")

    def test_truncated_note_with_mood_survives(self):
        got = vn.parse_note('{"objects":["蓝发小人"],"mood":"困惑","meme_gu')
        self.assertEqual(got["mood"], "困惑")
        self.assertEqual(got["objects"], ["蓝发小人"])

    def test_truncated_final_mood_string_survives(self):
        got = vn.parse_note('{"objects":["蓝发小人"],"mood":"困惑')
        self.assertIsNotNone(got)
        self.assertEqual(got["mood"], "困惑")

    def test_uncertain_honest_chinese_prose_is_preserved(self):
        for raw in ("看起来像是一只猫，背景有点模糊，不确定是什么品种。",
                    "看不清图上的小字，但能看到桌上有杯子。"):
            with self.subTest(raw=raw):
                got = vn.parse_note(raw)
                self.assertTrue(got)
                self.assertTrue(got["mood"])

    def test_repair_does_not_scan_unbounded_response_tails(self):
        raw = json.dumps({"objects": ["猫"], "text_in_image": "x" * 20000,
                          "mood": "困惑"}, ensure_ascii=False)
        self.assertIsNone(vn.parse_note(raw))


if __name__ == "__main__":
    unittest.main()
