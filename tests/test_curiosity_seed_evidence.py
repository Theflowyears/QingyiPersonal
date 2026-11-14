"""Seed provenance observes stored motivation, not an independent external fact."""
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import curiosity as cu
import self_model as sm


class SeedEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Python 3.10 compatible; cognition connections are closed per operation.
        temporary = tempfile.TemporaryDirectory(prefix="curiosity_seed_evidence_")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            "DATA_DIR": temporary.name,
            "PERSONA_DATA_DIR": str(Path(temporary.name) / "persona"),
            "QQ_DRY_RUN": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        cu._reset_cache()
        self.addCleanup(cu._reset_cache)

    def events(self):
        with closing(sm._db()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM event ORDER BY rowid")]

    def test_seed_conversion_does_not_record_an_observation_until_ingestion(self):
        cu.seeds_from_persona({"curious": ["猫为什么打呼噜"]})
        cu.seeds_from_wishes([{"kind": "know", "id": "w1", "text": "月亮的颜色"}])
        self.assertEqual(self.events(), [])
        self.assertEqual(cu.all_topics(), [])

    def test_persona_ingestion_records_exact_internal_motivation_now(self):
        specs = cu.seeds_from_persona({"curious": ["猫为什么打呼噜"]})
        before = time.time()
        self.assertEqual(cu.apply_seeds(specs, now=1234), 1)
        after = time.time()
        event, = self.events()
        self.assertEqual(event["kind"], "interest_seed")
        self.assertEqual(event["source"], "persona_state")
        self.assertEqual(event["visibility"], "private")
        self.assertEqual(event["actor_qq"], "")
        self.assertEqual(event["topic_qq"], "")
        self.assertEqual(json.loads(event["payload"]), {
            "topic": "猫为什么打呼噜", "seed_from": "persona_curious",
            "wish_id": "", "epistemic": "internal_motivation",
        })
        self.assertLessEqual(before, event["ts"])
        self.assertLessEqual(event["ts"], event["created"])
        self.assertLessEqual(event["created"], after)
        self.assertEqual(cu.topic(specs[0]["topic"])["origin_events"], [event["id"]])

    def test_wish_observation_does_not_inherit_wish_creation_time(self):
        specs = cu.seeds_from_wishes([
            {"kind": "know", "id": "wish:精确", "text": "月亮的颜色", "ts": 100},
            {"kind": "want", "id": "w2", "text": "换头像"},
        ])
        before = time.time()
        cu.apply_seeds(specs, now=200)
        event, = self.events()
        self.assertEqual(event["source"], "wish_state")
        self.assertEqual(json.loads(event["payload"]), {
            "topic": "月亮的颜色", "seed_from": "wish", "wish_id": "wish:精确",
            "epistemic": "internal_motivation",
        })
        self.assertGreaterEqual(event["ts"], before)
        self.assertEqual(cu.topic("月亮的颜色")["origin_events"], [event["id"]])

    def test_reingestion_is_content_addressed_and_keeps_original_event_immutable(self):
        specs = cu.seeds_from_persona(["猫为什么打呼噜"])
        cu.apply_seeds(specs, now=1000)
        original = self.events()
        for now in (2000, 3000, 4000):
            cu._reset_cache()
            cu.apply_seeds(specs, now=now)
        self.assertEqual(self.events(), original)
        rec = cu.topic(specs[0]["topic"])
        self.assertEqual(rec["origin_events"], [original[0]["id"]])
        self.assertEqual(rec["mentions"], 1)
        self.assertEqual(len(rec["points"]), 1)
        # Losing the curiosity projection must not duplicate the stored observation.
        cu._save(cu._empty())
        cu.apply_seeds(specs)
        self.assertEqual(self.events(), original)

    def test_source_topic_and_wish_id_each_participate_in_identity(self):
        specs = cu.seeds_from_persona(["同一话题", "另一话题"])
        specs += cu.seeds_from_wishes([
            {"kind": "know", "id": "w1", "text": "同一话题"},
            {"kind": "know", "id": "w2", "text": "同一话题"},
            {"kind": "know", "id": "w1", "text": "改过的话题"},
        ])
        cu.apply_seeds(specs)
        original = self.events()
        cu.apply_seeds(list(reversed(specs)))
        self.assertEqual(len(original), 5)
        self.assertEqual(len({event["dedupe"] for event in original}), 5)
        self.assertEqual(self.events(), original)
        self.assertEqual(cu.topic("同一话题")["origin_events"],
                         [original[i]["id"] for i in (0, 2, 3)])

    def test_existing_source_history_is_appended_not_replaced(self):
        origin = sm.record_event("group_message", payload={"text": "真实来源"})
        cu.note_self_asked("猫为什么打呼噜", event_ids=[origin])
        cu.apply_seeds(cu.seeds_from_persona(["猫为什么打呼噜"]))
        self.assertEqual(cu.topic("猫为什么打呼噜")["origin_events"],
                         [origin, self.events()[-1]["id"]])
        self.assertEqual(self.events()[0]["kind"], "group_message")

    def test_supplied_source_ids_are_preserved_without_replacement_motivation(self):
        first = sm.record_event("group_message", dedupe="real:1")
        second = sm.record_event("private_message", dedupe="real:2")
        for key in ("event_ids", "origin_events"):
            with self.subTest(key=key):
                spec = {"topic": key, "seed_from": "wish", "wish_id": "w1",
                        "weight": cu.W_WISH, key: [first, second, first]}
                cu.apply_seeds([spec])
                self.assertEqual(cu.topic(key)["origin_events"], [first, second])
        self.assertEqual(len(self.events()), 2)

    def test_wish_conversion_preserves_explicit_evidence_from_stored_wish(self):
        origin = sm.record_event("private_message", dedupe="wish-origin")
        for key in ("event_ids", "origin_events"):
            with self.subTest(key=key):
                wishes = [{"kind": "know", "text": key, "id": "w1", key: [origin]}]
                cu.apply_seeds(cu.seeds_from_wishes(wishes))
                self.assertEqual(cu.topic(key)["origin_events"], [origin])
                self.assertEqual(wishes[0][key], [origin])
        self.assertEqual(len(self.events()), 1)

    def test_invalid_supplied_sources_do_not_get_attached_or_replaced(self):
        invalid = sm.record_event("group_message", dedupe="withdrawn")
        valid = sm.record_event("group_message", dedupe="valid")
        self.assertTrue(sm.retract_evidence(invalid, "来源已撤回"))
        for refs, expected in (([invalid, "evt_missing"], []),
                               ([invalid, valid], [valid])):
            with self.subTest(refs=refs):
                name = "话题%d" % len(expected)
                cu.apply_seeds([{"topic": name, "seed_from": "persona_curious",
                                 "event_ids": refs}])
                self.assertEqual(cu.topic(name).get("origin_events", []), expected)
        self.assertEqual(len(self.events()), 2)

    def test_retracted_seed_observation_cannot_be_revived_by_reingestion(self):
        specs = cu.seeds_from_persona(["撤回的话题"])
        cu.apply_seeds(specs)
        original = self.events()
        eid = original[0]["id"]
        self.assertTrue(sm.retract_evidence(eid, "不再作为依据"))
        cu._reset_cache()
        with patch.object(cu, "_touch", wraps=cu._touch) as touch:
            cu.apply_seeds(specs)
        self.assertEqual(list(touch.call_args.kwargs["event_ids"]), [])
        self.assertFalse(sm.evidence_valid("event", eid))
        self.assertEqual(self.events(), original)
        # Keep historical references for audit; do not silently rewrite them.
        self.assertEqual(cu.topic("撤回的话题")["origin_events"], [eid])
        cu._save(cu._empty())
        cu.apply_seeds(specs)
        self.assertEqual(cu.topic("撤回的话题").get("origin_events", []), [])
        self.assertEqual(self.events(), original)

    def test_failed_write_logs_without_manufacturing_an_event_id_then_can_retry(self):
        specs = cu.seeds_from_persona(["写盘失败的话题"])
        for failure in ("", OSError("unavailable")):
            with self.subTest(failure=failure):
                log = io.StringIO()
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {
                    "return_value": failure}
                with patch.object(sm, "record_event", **kwargs), redirect_stdout(log):
                    self.assertEqual(cu.apply_seeds(specs), 1)
                self.assertEqual(cu.topic(specs[0]["topic"]).get("origin_events", []), [])
                self.assertTrue(log.getvalue().strip(), "Evidence failure must be observable")
                self.assertEqual(self.events(), [])
        cu.apply_seeds(specs)
        event, = self.events()
        self.assertEqual(cu.topic(specs[0]["topic"])["origin_events"], [event["id"]])
        self.assertEqual(cu.topic(specs[0]["topic"])["mentions"], 1)

    def test_failed_validity_check_fails_closed_and_logs(self):
        log = io.StringIO()
        with patch.object(sm, "evidence_valid", side_effect=OSError("unavailable")), \
                redirect_stdout(log):
            cu.apply_seeds(cu.seeds_from_persona(["验证失败的话题"]))
        self.assertEqual(cu.topic("验证失败的话题").get("origin_events", []), [])
        self.assertTrue(log.getvalue().strip())

    def test_other_seed_kinds_and_empty_topics_do_not_invent_motivation_events(self):
        cu.apply_seeds([{"topic": "群里热度", "seed_from": "group_heat"},
                        {"topic": "来历不明"}, {"topic": "  "}])
        self.assertEqual(self.events(), [])
        self.assertEqual(cu.topic("群里热度").get("origin_events", []), [])

    def test_scoring_and_action_claim_do_not_create_posthoc_seed_evidence(self):
        specs = cu.seeds_from_persona(["猫为什么打呼噜"])
        cu.apply_seeds(specs)
        original = self.events()
        with patch.object(cu, "max_per_day", return_value=1):
            self.assertTrue(cu.should_act(specs[0]["topic"]))
            self.assertTrue(cu.claim_action(specs[0]["topic"]))
        self.assertEqual(self.events(), original)


if __name__ == "__main__":
    unittest.main()
