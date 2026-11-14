"""阶段 2 影子内核：真实规则、解析恢复、去重和证据边界的回归锁。"""
import copy
import hashlib
import importlib
import json
import math
import unittest
from unittest.mock import patch

try:
    core = importlib.import_module("bot.emotion_core")
except ModuleNotFoundError as exc:
    if exc.name != "bot.emotion_core":
        raise
    core = None


DIMENSIONS = ("valence", "arousal", "control", "attention")
TRAITS = {
    "curiosity": 0.6, "sociability": 0.5, "caution": 0.6,
    "playfulness": 0.6, "sensitivity": 0.5, "expressiveness": 0.5,
}


def event(eid="e1", ts=100.0, kind="message", payload=None, **fields):
    """完整复现已核对的 event shape，不接触 self_model 或数据库。"""
    result = {
        "id": eid, "kind": kind, "ts": ts, "session": "test-session",
        "actor_qq": "1001", "topic_qq": "1002", "source": "onebot",
        "visibility": "private", "payload": {} if payload is None else payload,
        "dedupe": "fixture:" + eid, "created": 1000.0,
    }
    result.update(fields)
    return result


def praise(eid="p1", ts=100.0, **fields):
    return event(eid, ts, payload={"text": "谢谢你，你帮大忙了", "addressed_to_self": True},
                 **fields)


class EmotionCoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(core, "阶段 2 功能尚未实现：缺少 bot.emotion_core")

    def assert_state_close(self, first, second, tolerance=1e-10):
        self.assertEqual(set(first), set(second))
        for key in ("at",) + DIMENSIONS:
            self.assertAlmostEqual(first[key], second[key], delta=tolerance)
        self.assertEqual(first["processed"], second["processed"])
        self.assertEqual(first["traits"], second["traits"])

    def test_initial_state_has_fixed_unlearned_traits_and_no_relationships(self):
        state = core.initial_state(100)
        self.assertEqual(state, {
            "at": 100.0, "valence": 0.0, "arousal": 0.25,
            "control": 0.5, "attention": 0.0, "processed": 0,
            "traits": TRAITS,
        })
        self.assertIsInstance(state["at"], float)
        state["traits"]["curiosity"] = 0
        self.assertEqual(core.initial_state(100)["traits"], TRAITS)

    def test_parameter_hash_is_canonical_json_sha256_and_stable(self):
        self.assertEqual(core.VERSION, "shadow-v1")
        self.assertIsInstance(core.PARAMETERS, dict)
        encoded = json.dumps(core.PARAMETERS, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        expected = hashlib.sha256(encoded).hexdigest()
        self.assertEqual(core.parameter_hash(), expected)
        self.assertEqual(core.replay([], start_at=0, end_at=10)["parameter_hash"], expected)

    def test_advance_matches_independent_exponential_solution_in_all_dimensions(self):
        state = core.initial_state(100)
        state.update(valence=-0.8, arousal=0.9, control=0.1, attention=0.7)
        before = copy.deepcopy(state)
        result = core.advance(state, 1900)
        expected = {
            "valence": -0.8 * math.exp(-1800 / 7200),
            "arousal": 0.25 + 0.65 * math.exp(-1),
            "control": 0.5 - 0.4 * math.exp(-0.5),
            "attention": 0.7 * math.exp(-3),
        }
        for key, value in expected.items():
            self.assertAlmostEqual(result[key], value, delta=1e-12)
        self.assertEqual(state, before)
        self.assertIsNot(result, state)
        self.assertIsNot(result["traits"], state["traits"])

    def test_segmented_recovery_matches_one_analytic_advance(self):
        state = core.initial_state(100)
        state.update(valence=0.7, arousal=0.95, control=0.05, attention=0.9)
        segmented = state
        for at in range(101, 7301, 7):
            segmented = core.advance(segmented, at)
        segmented = core.advance(segmented, 7300)
        self.assert_state_close(segmented, core.advance(state, 7300))

    def test_zero_or_negative_elapsed_time_preserves_values_exactly(self):
        state = core.initial_state(500)
        state.update(valence=-0.8, arousal=0.1, control=0.1, attention=0.01)
        for at in (500, 100):
            with self.subTest(at=at):
                self.assertEqual(core.advance(state, at), state)

    def test_explicit_quote_flag_and_code_quotes_cannot_supply_praise(self):
        for payload in ({"text": "谢谢你", "is_quoted": True},
                        {"text": "`谢谢你，你好厉害`"},
                        {"text": "【引用】谢谢你"}):
            with self.subTest(payload=payload):
                payload["addressed_to_self"] = True
                assessment = core.appraise(event(payload=payload))
                self.assertEqual(assessment["delta"]["valence"], 0)
                self.assertEqual(assessment["delta"]["control"], 0)

    def test_negative_elapsed_time_does_not_reverse_state(self):
        state = core.apply_event(core.initial_state(200), praise(ts=200))
        result = core.advance(state, 100)
        self.assertEqual(result, state)
        self.assertIsNot(result, state)

    def test_no_events_or_long_offline_period_do_not_invent_low_mood(self):
        for end in (0, 3600, 86400 * 365):
            with self.subTest(end=end):
                result = core.replay([], start_at=0, end_at=end)
                self.assertEqual(result["state"], core.initial_state(end))
                self.assertEqual(result["timeline"], [])

    def test_normal_message_only_adds_small_attention_load(self):
        assessment = core.appraise(event(payload={"text": "我很伤心，今天很倒霉"}))
        self.assertFalse(assessment["ignored"])
        self.assertEqual(assessment["delta"]["valence"], 0)
        self.assertEqual(assessment["delta"]["arousal"], 0)
        self.assertEqual(assessment["delta"]["control"], 0)
        self.assertGreaterEqual(assessment["delta"]["attention"], 0.01)
        self.assertLessEqual(assessment["delta"]["attention"], 0.03)
        self.assertEqual(assessment["evidence_ids"], ["e1"])
        self.assertEqual(assessment["version"], "shadow-v1")
        self.assertTrue(0 <= assessment["confidence"] <= 1)
        self.assertIsInstance(assessment["rule"], str)
        self.assertIsInstance(assessment["reason"], str)

    def test_only_explicit_self_addressed_evidenced_praise_is_positive(self):
        for text in ("谢谢你", "你帮大忙了", "你好厉害"):
            with self.subTest(text=text):
                assessment = core.appraise(event(payload={
                    "text": text, "addressed_to_self": True,
                }))
                self.assertGreater(assessment["delta"]["valence"], 0)
                self.assertLessEqual(assessment["delta"]["valence"], 0.06)
                self.assertGreater(assessment["delta"]["control"], 0)
        for addressed in (False, None, "true", 1):
            with self.subTest(addressed=addressed):
                assessment = core.appraise(event(payload={
                    "text": "谢谢你，你好厉害", "addressed_to_self": addressed,
                }))
                self.assertEqual(assessment["delta"]["valence"], 0)
        ordinary = core.appraise(event(payload={"text": "明天见", "addressed_to_self": True}))
        self.assertEqual(ordinary["delta"]["valence"], 0)

    def test_quotes_and_roleplay_do_not_supply_personal_emotion_evidence(self):
        examples = [
            {"quoted": True, "text": "谢谢你"},
            {"quote": "谢谢你", "text": "谢谢你"},
            {"quoted_text": "谢谢你", "text": "谢谢你"},
            {"roleplay": True, "text": "你好厉害"},
            {"is_roleplay": True, "text": "你好厉害"},
            {"text": "他说：“谢谢你，你好厉害”"},
            {"text": "角色扮演：谢谢你"},
        ]
        for payload in examples:
            with self.subTest(payload=payload):
                payload["addressed_to_self"] = True
                assessment = core.appraise(event(payload=payload))
                self.assertEqual(assessment["delta"]["valence"], 0)
                self.assertEqual(assessment["delta"]["control"], 0)

    def test_banter_and_other_peoples_sadness_never_become_negative_mood(self):
        events = [event(str(i), 100, payload={"text": text, "addressed_to_self": True})
                  for i, text in enumerate(("笨蛋", "啧", "哈哈", "我很伤心", "接口又坏了") * 20)]
        state = core.replay(events, start_at=100, end_at=100)["state"]
        self.assertEqual(state["valence"], 0)
        self.assertEqual(state["arousal"], 0.25)
        self.assertEqual(state["control"], 0.5)
        self.assertEqual(state["traits"], TRAITS)
        self.assertLessEqual(state["attention"], 1)

    def test_legacy_consolidation_and_unknown_events_are_zero_impact(self):
        examples = [praise(source="legacy"), praise(source="legacy/import"),
                    praise(kind="legacy_note"), praise(kind="consolidation"),
                    praise(kind="unrecognized")]
        for value in examples:
            with self.subTest(kind=value["kind"], source=value["source"]):
                assessment = core.appraise(value)
                self.assertTrue(assessment["ignored"])
                self.assertEqual(assessment["delta"], dict.fromkeys(DIMENSIONS, 0.0))
                state = core.apply_event(core.initial_state(100), value)
                self.assertEqual(state["processed"], 1)
                self.assertEqual(state["valence"], 0)

    def test_json_payload_and_dict_payload_have_identical_appraisal(self):
        value = praise()
        as_json = copy.deepcopy(value)
        as_json["payload"] = json.dumps(value["payload"], ensure_ascii=False)
        self.assertEqual(core.appraise(value), core.appraise(as_json))

    def test_tool_ok_requires_actual_bool_and_changes_only_control_attention(self):
        for ok, control_delta in ((True, 0.025), (False, -0.025)):
            with self.subTest(ok=ok):
                assessment = core.appraise(event(kind="tool_result", payload={"ok": ok}))
                self.assertFalse(assessment["ignored"])
                self.assertEqual(assessment["delta"], {
                    "valence": 0.0, "arousal": 0.0,
                    "control": control_delta, "attention": 0.01,
                })
        for ok in (None, 0, 1, "false", "true"):
            with self.subTest(ok=ok):
                assessment = core.appraise(event(kind="tool_result", payload={"ok": ok}))
                self.assertTrue(assessment["ignored"])
                self.assertEqual(assessment["delta"], dict.fromkeys(DIMENSIONS, 0.0))

    def test_many_system_failures_never_change_relationships_or_personality(self):
        events = [event(str(i), 100, kind="tool_result", payload={"ok": False})
                  for i in range(100)]
        state = core.replay(events, start_at=100, end_at=100)["state"]
        self.assertEqual(state["valence"], 0)
        self.assertEqual(state["arousal"], 0.25)
        self.assertEqual(state["traits"], TRAITS)
        self.assertNotIn("relationships", state)
        self.assertNotIn("affection", state)
        self.assertEqual(state["control"], 0)

    def test_action_acceptance_is_technical_ack_not_a_social_response(self):
        for payload in ({"accepted": True}, {"ok": True, "status": "accepted"},
                        {"text": "谢谢你", "addressed_to_self": True, "ok": True}):
            with self.subTest(payload=payload):
                assessment = core.appraise(event(kind="action_result", payload=payload))
                self.assertTrue(assessment["ignored"])
                self.assertEqual(assessment["delta"], dict.fromkeys(DIMENSIONS, 0.0))

    def test_apply_event_recovers_before_impulse_and_is_pure(self):
        state = core.initial_state(100)
        state.update(valence=0.8, control=0.8, attention=0.8)
        value = praise(ts=1900)
        assessment = core.appraise(value)
        originals = copy.deepcopy((state, value, assessment))
        result = core.apply_event(state, value, assessment)
        self.assertAlmostEqual(result["valence"], 0.8 * math.exp(-0.25) + assessment["delta"]["valence"])
        self.assertAlmostEqual(result["control"], 0.5 + 0.3 * math.exp(-0.5) + assessment["delta"]["control"])
        self.assertEqual(result["at"], 1900.0)
        self.assertEqual(result["processed"], 1)
        self.assertEqual((state, value, assessment), originals)
        result["traits"]["curiosity"] = 0
        self.assertEqual(state["traits"], TRAITS)

    def test_late_event_never_rolls_back_and_effective_ts_is_respected(self):
        state = core.initial_state(500)
        late = core.apply_event(state, praise(ts=100))
        self.assertEqual(late["at"], 500)
        effective = core.apply_event(state, praise(ts=100, effective_ts=800))
        self.assertEqual(effective["at"], 800)

    def test_accumulated_impulses_clip_each_state_dimension(self):
        events = [praise(str(i), 100) for i in range(200)]
        state = core.replay(events, start_at=100, end_at=100)["state"]
        self.assertEqual(state["valence"], 1)
        self.assertEqual(state["control"], 1)
        self.assertEqual(state["attention"], 1)
        self.assertEqual(state["arousal"], 0.25)
        self.assertEqual(state["traits"], TRAITS)

    def test_replay_deduplicates_event_and_recorded_appraisal_by_id(self):
        value = praise()
        value["appraisal"] = core.appraise(value)
        duplicate = copy.deepcopy(value)
        duplicate["payload"] = "malformed duplicate must not be reappraised"
        duplicate["appraisal"]["delta"]["valence"] = float("nan")
        once = core.replay([value], start_at=100, end_at=200)
        repeated = core.replay([value, duplicate, value], start_at=100, end_at=200)
        self.assertEqual(repeated["state"], once["state"])
        self.assertEqual(repeated["state"]["processed"], 1)
        self.assertEqual(sum(row["rule"] == "duplicate_event" for row in repeated["timeline"]), 2)

    def test_replay_sorts_stably_by_ts_then_id_and_matches_manual_application(self):
        a = praise("a", 100)
        b = event("b", 100, kind="tool_result", payload={"ok": False})
        c = event("c", 400, payload={"text": "我很伤心"})
        events = [c, b, a]
        original = copy.deepcopy(events)
        result = core.replay(events, start_at=50, end_at=1000)
        manual = core.initial_state(50)
        for value in (a, b, c):
            manual = core.apply_event(manual, value)
        manual = core.advance(manual, 1000)
        self.assertEqual([row["event_id"] for row in result["timeline"]], ["a", "b", "c"])
        self.assert_state_close(result["state"], manual)
        self.assertEqual(core.replay([a, c, b], start_at=50, end_at=1000), result)
        self.assertEqual(events, original)

    def test_replay_effective_time_ties_preserve_online_clipping_order(self):
        values = [praise(f"p{i:02}", 100, effective_ts=200) for i in range(40)]
        values.append(event("a-late-failure", 50, effective_ts=200,
                            kind="tool_result", payload={"ok": False}))
        original = copy.deepcopy(values)
        online = core.initial_state(50)
        for value in values:
            online = core.apply_event(online, value)
        self.assertEqual(online["control"], 0.975)
        result = core.replay(values, start_at=50, end_at=200)
        self.assertEqual(result["state"]["control"], 0.975)
        self.assert_state_close(result["state"], online)
        self.assertEqual([row["event_id"] for row in result["timeline"]],
                         [value["id"] for value in values])
        self.assertEqual(values, original)

    def test_replay_explicit_input_sequence_survives_input_reordering(self):
        values = [praise(f"p{i:02}", 100, effective_ts=200, input_seq=i)
                  for i in range(40)]
        values.append(event("a-late-failure", 50, effective_ts=200, input_seq=40,
                            kind="tool_result", payload={"ok": False}))
        result = core.replay(reversed(values), start_at=50, end_at=200)
        self.assertEqual(result["state"]["control"], 0.975)
        self.assertEqual([row["event_id"] for row in result["timeline"]],
                         [value["id"] for value in values])

    def test_replay_saved_source_sequence_overrides_batch_local_input_sequence(self):
        values = [praise(f"p{i:02}", 100, effective_ts=200) for i in range(40)]
        values.append(event("a-late-failure", 50, effective_ts=200,
                            kind="tool_result", payload={"ok": False}))
        reference = None
        for batch_size in (1, 7, 41):
            with self.subTest(batch_size=batch_size):
                saved = [dict(value, source_seq=2**60 + i, input_seq=i % batch_size)
                         for i, value in enumerate(values)]
                original = copy.deepcopy(saved)
                result = core.replay(saved[::2] + saved[1::2], start_at=50, end_at=200)
                self.assertEqual(result["state"]["control"], 0.975)
                self.assertEqual([row["event_id"] for row in result["timeline"]],
                                 [value["id"] for value in values])
                if reference is not None:
                    self.assertEqual(result, reference)
                reference = result
                self.assertEqual(saved, original)

    def test_replay_orders_mixed_events_by_effective_time_or_raw_time(self):
        values = [praise("p", 300, effective_ts=100),
                  event("f", 100, effective_ts=300, kind="tool_result", payload={"ok": False}),
                  event("n", 200, payload={"text": "普通消息"})]
        result = core.replay(values, start_at=50, end_at=400)
        self.assertEqual([row["event_id"] for row in result["timeline"]], ["p", "n", "f"])
        expected_control = 0.5 + 0.015 * math.exp(-300 / 3600) - 0.025 * math.exp(-100 / 3600)
        self.assertAlmostEqual(result["state"]["control"], expected_control, delta=1e-12)

    def test_replay_effective_sequence_ties_use_id_without_changing_raw_ties(self):
        # 混合同刻：原始时间事件先按 ID，再是显式有效时间事件按序号/ID。
        values = [praise("0-explicit", 50, effective_ts=100, input_seq=0),
                  praise("b-raw", 100), praise("a-raw", 100),
                  praise("a-explicit", 70, effective_ts=100, input_seq=0)]
        result = core.replay(values, start_at=50, end_at=100)
        self.assertEqual([row["event_id"] for row in result["timeline"]],
                         ["a-raw", "b-raw", "0-explicit", "a-explicit"])

    def test_replay_invalid_explicit_sequence_metadata_raises_value_error(self):
        for key in ("source_seq", "input_seq"):
            for bad in (-1, True, 1.5, "1", None, float("nan"), float("inf")):
                with self.subTest(key=key, bad=bad):
                    value = praise(effective_ts=100, **{key: bad})
                    with self.assertRaisesRegex(ValueError, r"seq.*integer"):
                        core.replay([value], start_at=50, end_at=100)

    def test_replay_tick_hint_does_not_change_final_state(self):
        # 旧名称保留；tick 现在必须真实步进。超预算的极小 tick 另测明确拒绝。
        events = [praise(ts=100), event("t", 300, kind="tool_result", payload={"ok": False})]
        reference = core.replay(events, start_at=10, end_at=86400)
        for tick in (1, 7, 60, 100000):
            with self.subTest(tick=tick):
                actual = core.replay(events, start_at=10, end_at=86400, tick_seconds=tick)
                self.assert_state_close(actual["state"], reference["state"])
                self.assertEqual(len(actual["timeline"]), len(reference["timeline"]))
                for actual_row, reference_row in zip(actual["timeline"], reference["timeline"]):
                    for key in ("before", "after"):
                        self.assert_state_close(actual_row[key], reference_row[key])
                    self.assertEqual({k: v for k, v in actual_row.items() if k not in ("before", "after")},
                                     {k: v for k, v in reference_row.items() if k not in ("before", "after")})

    def test_replay_tick_really_steps_to_events_and_recovers_at_the_end(self):
        values = [praise("p", 70, effective_ts=100),
                  event("f", 150, effective_ts=250, kind="tool_result", payload={"ok": False})]
        # 观察真实 advance，而不是替身结果；去掉 tick 循环会直接被调用轨迹抓到。
        with patch.object(core, "advance", wraps=core.advance) as observed:
            result = core.replay(values, start_at=10, end_at=380, tick_seconds=60)
        self.assertEqual([call.args[1] for call in observed.call_args_list],
                         [70, 100, 160, 220, 250, 310, 370, 380])
        expected = {
            "valence": 0.04 * math.exp(-280 / 7200),
            "arousal": 0.25,
            "control": 0.5 + 0.015 * math.exp(-280 / 3600) - 0.025 * math.exp(-130 / 3600),
            "attention": 0.02 * math.exp(-280 / 600) + 0.01 * math.exp(-130 / 600),
        }
        for key, value in expected.items():
            self.assertAlmostEqual(result["state"][key], value, delta=1e-12)
        self.assertEqual(result["state"]["at"], 380)
        self.assertEqual(result["state"]["processed"], 2)
        self.assertEqual(len(result["timeline"]), 2)
        self.assertAlmostEqual(result["timeline"][1]["before"]["control"],
                               0.5 + 0.015 * math.exp(-150 / 3600), delta=1e-12)

    def test_replay_tick_segments_empty_window_without_inventing_events(self):
        with patch.object(core, "advance", wraps=core.advance) as observed:
            result = core.replay([], start_at=10, end_at=145, tick_seconds=60)
        self.assertEqual([call.args[1] for call in observed.call_args_list], [70, 130, 145])
        self.assertEqual(result["state"], core.initial_state(145))
        self.assertEqual(result["timeline"], [])

    def test_replay_tick_clipping_matches_online_state_at_every_event(self):
        values = [praise(f"p{i:02}", 100, effective_ts=200) for i in range(40)]
        values.append(event("a-late-failure", 50, effective_ts=200,
                            kind="tool_result", payload={"ok": False}))
        reference = core.replay(values, start_at=50, end_at=500)
        for tick in (0.01, 7, 60, 1000):
            with self.subTest(tick=tick):
                result = core.replay(values, start_at=50, end_at=500, tick_seconds=tick)
                self.assertEqual(result["timeline"][-1]["after"]["control"], 0.975)
                self.assert_state_close(result["state"], reference["state"])
                for row, other in zip(result["timeline"], reference["timeline"]):
                    self.assert_state_close(row["before"], other["before"])
                    self.assert_state_close(row["after"], other["after"])

    def test_replay_tick_excessive_work_is_rejected_before_iterating(self):
        for end, tick in ((100001, 1), (86400, 0.001), (1e308, 1e-308), (1, 5e-324)):
            with self.subTest(end=end, tick=tick):
                with patch.object(core, "advance", wraps=core.advance) as observed:
                    with self.assertRaisesRegex(ValueError, r"tick.*steps"):
                        core.replay([], start_at=0, end_at=end, tick_seconds=tick)
                self.assertEqual(observed.call_count, 0)

    def test_replay_tick_step_budget_is_shared_across_events_and_tail(self):
        values = [praise("p", 80), event("n", 150)]
        with patch.object(core, "_MAX_REPLAY_STEPS", 4, create=True):
            # 全窗 ceil(210/60)=4，但三段各需两步，不能每段重置预算。
            with self.assertRaisesRegex(ValueError, r"tick.*steps"):
                core.replay(values, start_at=10, end_at=220, tick_seconds=60)
            result = core.replay(values, start_at=10, end_at=150, tick_seconds=60)
        self.assertEqual(result["state"]["at"], 150)
        self.assertEqual(result["state"]["processed"], 2)

    def test_replay_tick_budget_counts_remainders_even_when_ratio_underflows(self):
        tiny = math.nextafter(0.0, math.inf)
        with patch.object(core, "_MAX_REPLAY_STEPS", 1):
            with self.assertRaisesRegex(ValueError, r"tick.*steps"):
                core.replay([praise(ts=tiny)], start_at=0, end_at=2 * tiny, tick_seconds=1e308)

    def test_replay_tick_below_timestamp_precision_is_rejected(self):
        start = 1e20
        with self.assertRaisesRegex(ValueError, r"tick.*precision"):
            core.replay([], start_at=start, end_at=math.nextafter(start, math.inf), tick_seconds=1)

    def test_replay_tiny_tick_with_zero_span_needs_no_work(self):
        self.assertEqual(core.replay([], start_at=100, end_at=100, tick_seconds=5e-324)["state"],
                         core.initial_state(100))

    def test_replay_huge_tick_still_advances_tiny_positive_span(self):
        end = math.nextafter(0.0, math.inf)
        self.assertEqual(core.replay([], start_at=0, end_at=end, tick_seconds=1e308)["state"]["at"], end)

    def test_explicit_effective_time_replays_a_late_pre_observation_event(self):
        value = event('late', 50, effective_ts=150, source_seq=1,
                      kind='tool_result', payload={'ok': False})
        online = core.apply_event(core.initial_state(100), value)
        replayed = core.replay([value], start_at=100, end_at=150)
        self.assertEqual(replayed['state']['processed'], 1)
        self.assert_state_close(replayed['state'], online)

    def test_replay_inclusive_window_excludes_unknown_and_out_of_window_times(self):
        values = [praise("unknown", 0), praise("negative", -1), praise("old", 99),
                  praise("start", 100), praise("end", 200), praise("future", 201),
                  praise("effective-future", 150, effective_ts=201)]
        result = core.replay(values, start_at=100, end_at=200)
        self.assertEqual(result["state"]["processed"], 2)
        self.assertEqual(result["state"]["at"], 200)
        by_id = {row["event_id"]: row for row in result["timeline"]}
        for eid in ("unknown", "negative"):
            self.assertEqual(by_id[eid]["rule"], "unknown_timestamp")
            self.assertTrue(by_id[eid]["reason"])
            self.assertEqual(by_id[eid]["before"], by_id[eid]["after"])
        for eid in ("old", "future", "effective-future"):
            self.assertEqual(by_id[eid]["rule"], "outside_window")

    def test_timeline_is_sanitized_independent_snapshot_with_recovery_before_event(self):
        secret = "PRIVATE-CHAT-TEXT-DO-NOT-COPY"
        first = praise("p", 100)
        second = event("n", 1900, payload={"text": secret})
        result = core.replay([first, second], start_at=100, end_at=3000)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("actor_qq", encoded)
        self.assertNotIn("payload", encoded)
        row = result["timeline"][1]
        self.assertTrue({"event_id", "at", "rule", "delta", "before", "after"} <= row.keys())
        self.assertEqual(row["before"]["at"], 1900)
        self.assertEqual(row["after"]["at"], 1900)
        self.assertAlmostEqual(row["before"]["valence"],
                               result["timeline"][0]["after"]["valence"] * math.exp(-0.25))
        row["after"]["traits"]["curiosity"] = 0
        self.assertEqual(result["state"]["traits"], TRAITS)
        self.assertEqual(result["timeline"][0]["after"]["traits"], TRAITS)

    def test_synthetic_appraisals_and_timeline_keep_explicit_provenance(self):
        for value in (praise(source="synthetic"), praise(synthetic=True),
                      event(payload={"text": "你好厉害", "addressed_to_self": True, "synthetic": True})):
            with self.subTest(value=value):
                assessment = core.appraise(value)
                self.assertIs(assessment["synthetic"], True)
                result = core.replay([value], start_at=100, end_at=100)
                self.assertIs(result["timeline"][0]["synthetic"], True)
                self.assertGreater(result["state"]["valence"], 0)

    def test_invalid_event_structure_and_payload_raise_clear_value_errors(self):
        values = [None, [], {"id": "", "kind": "message", "ts": 100, "payload": {}},
                  event(payload="{"), event(payload="[]"), event(payload=[]),
                  event(payload={"text": 123}), event(payload={"ok": float("nan")}),
                  event(payload='{"ok": NaN}'), event(payload={"nested": {"x": float("inf")}})]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r"event|payload|id|text|finite"):
                    core.appraise(value)

    def test_invalid_numeric_times_are_rejected_without_polluting_state(self):
        for bad in (float("nan"), float("inf"), -float("inf"), "100", None, True):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, r"at|finite|number"):
                    core.initial_state(bad)
                with self.assertRaisesRegex(ValueError, r"at|finite|number"):
                    core.advance(core.initial_state(100), bad)
                with self.assertRaisesRegex(ValueError, r"ts|finite|number"):
                    core.appraise(praise(ts=bad))
                with self.assertRaisesRegex(ValueError, r"ts|finite|number"):
                    core.apply_event(core.initial_state(100), praise(effective_ts=bad))

    def test_invalid_state_values_traits_and_counts_are_rejected(self):
        cases = [("valence", float("nan")), ("arousal", float("inf")),
                 ("control", -0.1), ("attention", 1.1),
                 ("processed", -1), ("processed", 1.5), ("processed", True),
                 ("traits", dict(TRAITS, curiosity=0.9))]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                state = core.initial_state(100)
                state[key] = value
                with self.assertRaisesRegex(ValueError, r"state|traits|finite"):
                    core.advance(state, 200)

    def test_invalid_replay_windows_ticks_and_event_times_fail_explicitly(self):
        for kwargs in ({"start_at": 200, "end_at": 100},
                       {"start_at": -1, "end_at": 100},
                       {"start_at": 0, "end_at": float("inf")},
                       {"start_at": 0, "end_at": 100, "tick_seconds": 0},
                       {"start_at": 0, "end_at": 100, "tick_seconds": -1},
                       {"start_at": 0, "end_at": 100, "tick_seconds": float("nan")},
                       {"start_at": 0, "end_at": 100, "tick_seconds": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, r"start|end|tick|finite"):
                    core.replay([], **kwargs)
        with self.assertRaisesRegex(ValueError, r"ts|finite"):
            core.replay([praise(ts=float("nan"))], start_at=0, end_at=100)

    def test_recorded_appraisal_requires_matching_evidence_version_and_finite_deltas(self):
        value = praise()
        valid = core.appraise(value)
        self.assertEqual(core.apply_event(core.initial_state(100), value, valid),
                         core.apply_event(core.initial_state(100), value))
        for key, change in (("evidence_ids", ["wrong-event"]), ("version", "unknown"),
                            ("confidence", float("inf")), ("ignored", "false"),
                            ("delta", dict.fromkeys(DIMENSIONS, float("nan"))),
                            ("delta", dict.fromkeys(DIMENSIONS, -0.9))):
            with self.subTest(key=key):
                assessment = copy.deepcopy(valid)
                assessment[key] = change
                with self.assertRaisesRegex(ValueError, r"appraisal|evidence|version|finite"):
                    core.apply_event(core.initial_state(100), value, assessment)

    def test_forged_assessment_cannot_turn_legacy_into_real_praise(self):
        value = praise(source="legacy")
        forged = core.appraise(praise())
        with self.assertRaisesRegex(ValueError, r"appraisal"):
            core.apply_event(core.initial_state(100), value, forged)


if __name__ == "__main__":
    unittest.main()
