"""Purpose precedence through installed wrappers and stubbed bridge transport."""
import asyncio
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import inspect
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BOT = str(Path(__file__).resolve().parents[1] / "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import ceylan_control_runtime as ctl
import ceylan_fun_runtime as fun
import ceylan_keys_runtime as keys
import ceylan_observer as observer

PROVIDERS = ("call_openclaw", "call_mimo_vision")
REPORT = "甲" * 94 + "。" + "乙" * 119 + "。" + "丙" * 84 + "。"
JSON_REPLY = ' \n{"summary": "' + "观察" * 150 + '", "complete": true}\n '
TRANSLATION = "译文：" + "保留原文的缩进、逗号和分段，不要改成闲聊。\n" * 6


class ReportPackingTests(unittest.TestCase):
    def test_report_uses_spare_capacity_across_sentence_boundaries(self):
        got = ctl.chunks(REPORT, dict(ctl.DEFAULTS), profile="report")
        self.assertEqual("".join(got), REPORT)
        self.assertLessEqual(len(got), 2)
        self.assertTrue(all(len(x) <= 200 for x in got))

    def test_report_preserves_commas_spaces_and_newlines_when_splitting(self):
        text = "甲" * 89 + "， " + "乙" * 99 + "、\n" + "丙" * 105 + "。"
        got = ctl.chunks(text, dict(ctl.DEFAULTS), profile="report")
        self.assertEqual("".join(got), text)
        self.assertLessEqual(len(got), 2)
        self.assertTrue(all(len(x) <= 200 for x in got))

    def test_report_clip_uses_explicit_options_not_global_options(self):
        opts = {**ctl.DEFAULTS, "report_max_chars": 100}
        with patch.dict(ctl.OPTIONS, {"report_max_chars": 400}):
            got = ctl.chunks("牌" * 500, opts, profile="report")
        self.assertEqual("".join(got), "牌" * 99 + "…")

    def test_clip_report_accepts_its_own_options(self):
        self.assertEqual(ctl.clip_report("牌" * 500, {"report_max_chars": 100}),
                         "牌" * 99 + "…")

    def test_effective_report_capacity_covers_cap_without_mutating_knobs(self):
        for width, bubbles, cap, effective_width in ((50, 1, 300, 300),
                                                     (50, 3, 400, 134),
                                                     (100, 2, 301, 151)):
            with self.subTest(width=width, bubbles=bubbles, cap=cap):
                opts = {**ctl.DEFAULTS, "reply_chars_report": width,
                        "bubbles_report": bubbles, "report_max_chars": cap}
                before = dict(opts)
                text = "牌" * (cap - 1) + "。"
                got = ctl.chunks(text, opts, profile="report")
                self.assertEqual("".join(got), text)
                self.assertLessEqual(len(got), bubbles)
                self.assertTrue(all(len(x) <= effective_width for x in got))
                self.assertEqual(opts, before)


class InstalledWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rule_wrappers_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for patcher in (
                patch.dict(os.environ, {
                    "DATA_DIR": str(root), "PERSONA_DATA_DIR": str(root / "persona"),
                    "QQ_DRY_RUN": "1",
                }),
                patch.multiple(
                    ctl, ROOT=root / "control", OPTIONS={**ctl.DEFAULTS, "delay_ms": 0},
                    BUSY=Counter(), LEDGER={}, BATCH_LOCKS={}, INBOX_TASK=None),
                patch.multiple(
                    fun, ROOT=root / "fun", SETTINGS={**fun.DEFAULTS, "mode": "playful"},
                    STATE={"version": 1, "totals": {}, "days": {}, "groups": {}}, LEDGER={}),
                patch.object(keys, "ROOT", root / "keys"),
                patch.multiple(
                    observer, COUNTS=Counter(), EVENTS=observer.deque(maxlen=60),
                    LATENCIES=observer.deque(maxlen=100), HOURS={}, ACTIVE=0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        token = ctl.SCOPE.set("chat")
        self.addCleanup(ctl.SCOPE.reset, token)
        self.calls = []
        self.sent = []
        self.reply = JSON_REPLY
        self.receipts = []
        self.hook = None
        self.blocked = False
        self.rt = SimpleNamespace(blocked=lambda gid: self.blocked,
                                  snapshot=lambda: None, mark_patrol=lambda: None)
        self.ns = self.install_stack()

    def install_stack(self, *, send_kwargs=False, strict_provider=False, legacy_send=False,
                      transport=False):
        async def noop(*args, **kwargs):
            return None

        async def provider(messages, max_tokens=800, timeout=300, **extra):
            self.calls.append({"messages": messages, "max_tokens": max_tokens,
                               "timeout": timeout, "extra": extra,
                               "scope": ctl.SCOPE.get()})
            if self.hook:
                return await self.hook()
            return self.reply

        async def strict(messages, max_tokens=800, timeout=300, chat_fast=False,
                         long_output=False):
            return await provider(messages, max_tokens, timeout,
                                  chat_fast=chat_fast, long_output=long_output)

        async def deliver(group_id, user_id, text, mention, reason, extra):
            self.sent.append({"group_id": group_id, "user_id": user_id, "text": text,
                              "mention": mention, "reason": reason, "extra": extra})
            return self.receipts.pop(0) if self.receipts else True

        async def send_group_msg(ws, group_id, user_id, text, mention=True, reason="",
                                 report=False, reply_profile=None):
            return await deliver(group_id, user_id, text, mention, reason,
                                 {"report": report, "reply_profile": reply_profile})

        async def send_private_msg(ws, user_id, text, reason="", report=False,
                                   reply_profile=None):
            return await deliver(None, user_id, text, False, reason,
                                 {"report": report, "reply_profile": reply_profile})

        async def group_legacy(ws, group_id, user_id, text, mention=True, reason="", report=False):
            return await deliver(group_id, user_id, text, mention, reason, {"report": report})

        async def private_legacy(ws, user_id, text, reason="", report=False):
            return await deliver(None, user_id, text, False, reason, {"report": report})

        async def group_kwargs(ws, group_id, user_id, text, mention=True, reason="", **kwargs):
            return await deliver(group_id, user_id, text, mention, reason, kwargs)

        async def private_kwargs(ws, user_id, text, reason="", **kwargs):
            return await deliver(None, user_id, text, False, reason, kwargs)

        ns = {name: noop for name in (
            "handle_group_message", "handle_private_message", "periodic_group_review",
            "compact_group_memory_once", "run_agent_task", "handle_event",
            "send_group_image", "upload_group_file")}
        ns.update(call_openclaw=strict if strict_provider else provider,
                  call_mimo_vision=strict if strict_provider else provider,
                  send_group_msg=group_kwargs if send_kwargs else send_group_msg,
                  send_private_msg=private_kwargs if send_kwargs else send_private_msg,
                  build_group_recent_text=lambda *a: ("", 0, 0, 0, []),
                  is_ignored_sender=lambda event: False)
        if legacy_send:
            ns.update(send_group_msg=group_legacy, send_private_msg=private_legacy)
        if transport:
            import qq_openclaw_bridge as bridge
            ns.update(send_group_msg=bridge.send_group_msg, send_private_msg=bridge.send_private_msg)
        with redirect_stdout(io.StringIO()):
            keys.install(ns)
            ctl.install(ns, self.rt)
            observer.install(ns, self.rt)
            fun.install(ns, self.rt)
        if ctl.INBOX_TASK is not None:
            ctl.INBOX_TASK.cancel()
        return ns

    def gate(self, kind):
        ctl.SCOPE.set("patrol")
        fun.SETTINGS.update(quiet_enabled=kind == "quiet", quiet_start=0, quiet_end=0,
                            quiet_patrol=True, daily_patrol_model_limit=1 if kind == "quota" else 0)
        fun.bucket()["patrol_model_calls"] = 1 if kind == "quota" else 0

    async def send(self, ns=None, *, private=False, text=REPORT, **extra):
        ns = self.ns if ns is None else ns
        if private:
            return await ns["send_private_msg"](None, "456", text, **extra)
        return await ns["send_group_msg"](None, "123", "456", text, **extra)

    async def test_internal_bypasses_outer_fun_quiet_and_quota_without_style_changes(self):
        messages = [{"role": "system", "content": "Return only JSON."}]
        for name in PROVIDERS:
            for gate in ("quiet", "quota"):
                for profile in ("intimate", "task"):
                    with self.subTest(provider=name, gate=gate, profile=profile):
                        self.gate(gate)
                        before = deepcopy(messages)
                        start = len(self.calls)
                        answer = await self.ns[name](messages, max_tokens=700, timeout=17,
                                                     internal=True, report=True,
                                                     reply_profile=profile, chat_fast=True)
                        self.assertEqual(answer, JSON_REPLY)
                        self.assertEqual(len(self.calls), start + 1)
                        call = self.calls[-1]
                        self.assertIs(call["messages"], messages)
                        self.assertEqual(messages, before)
                        self.assertEqual(call["max_tokens"], 700)
                        self.assertEqual(call["timeout"], 17)
                        self.assertEqual(call["extra"], {"chat_fast": True})
                        self.assertEqual(call["scope"], "internal")
                        self.assertEqual(ctl.SCOPE.get(), "patrol")
                        self.assertEqual(fun.bucket()["patrol_model_calls"], 1 if gate == "quota" else 0)

    async def test_internal_stack_restores_scope_on_error_and_cancellation(self):
        for name in PROVIDERS:
            for error in (RuntimeError("provider failed"), asyncio.CancelledError()):
                with self.subTest(provider=name, error=type(error).__name__):
                    self.gate("quiet")

                    async def fail():
                        raise error

                    self.hook = fail
                    with self.assertRaises(type(error)):
                        await self.ns[name]([], internal=True)
                    self.assertEqual(self.calls[-1]["scope"], "internal")
                    self.assertEqual(ctl.SCOPE.get(), "patrol")
                    self.assertEqual(observer.ACTIVE, 0)
        self.hook = None

    async def test_internal_stack_without_quiet_gate_does_not_add_fun_mode_note(self):
        messages = [{"role": "system", "content": "Only JSON."}]
        for name in PROVIDERS:
            self.assertEqual(await self.ns[name](messages, max_tokens=700, internal=True), JSON_REPLY)
            self.assertIs(self.calls[-1]["messages"], messages)
            self.assertEqual(self.calls[-1]["extra"], {})

    async def test_inherited_internal_scope_wins_over_report_long_and_profile(self):
        ctl.SCOPE.set("internal")
        messages = [{"role": "user", "content": "Only JSON."}]
        for name in PROVIDERS:
            for extra in ({"report": True}, {"long_output": True},
                          {"reply_profile": "long"}, {"reply_profile": "task", "report": True}):
                with self.subTest(provider=name, extra=extra):
                    self.assertEqual(await self.ns[name](messages, max_tokens=700, **extra), JSON_REPLY)
                    self.assertEqual(self.calls[-1]["max_tokens"], 700)
                    self.assertIs(self.calls[-1]["messages"], messages)
                    self.assertNotIn("report", self.calls[-1]["extra"])
                    self.assertNotIn("reply_profile", self.calls[-1]["extra"])

    async def test_report_survives_quiet_and_quota_but_optional_calls_stay_gated(self):
        self.reply = REPORT
        for name in PROVIDERS:
            for gate, skipped in (("quiet", "patrol_skipped_quiet"),
                                  ("quota", "patrol_skipped_limit")):
                with self.subTest(provider=name, gate=gate):
                    self.gate(gate)
                    start = len(self.calls)
                    answer = await self.ns[name]([], max_tokens=700, report=True)
                    self.assertEqual(answer.replace("\n", ""), REPORT)
                    self.assertEqual(len(self.calls), start + 1)
                    self.assertEqual(self.calls[-1]["max_tokens"], 700)
                    self.assertNotIn("report", self.calls[-1]["extra"])
                    self.assertIn("办事汇报", self.calls[-1]["messages"][0]["content"])
                    self.assertEqual(fun.bucket()["patrol_model_calls"], 1 if gate == "quota" else 0)
                    self.assertEqual(await self.ns[name]([]), "SILENCE")
                    self.assertEqual(len(self.calls), start + 1)
                    self.assertGreater(fun.bucket().get(skipped, 0), 0)

    async def test_nested_internal_calls_keep_json_and_restore_scope(self):
        self.gate("quiet")
        inner_messages = [{"role": "user", "content": "Inner JSON."}]

        async def nested():
            self.hook = None
            return await asyncio.create_task(self.ns[PROVIDERS[1]](inner_messages, max_tokens=700))

        self.hook = nested
        self.assertEqual(await self.ns[PROVIDERS[0]]([], internal=True), JSON_REPLY)
        self.assertEqual([c["scope"] for c in self.calls], ["internal", "internal"])
        self.assertIs(self.calls[-1]["messages"], inner_messages)
        self.assertEqual(ctl.SCOPE.get(), "patrol")

    async def test_intimate_and_boundary_bypass_generic_formatting_without_long_promotion(self):
        messages = [{"role": "system", "content": "Dedicated bridge instructions."}]
        for name in PROVIDERS:
            for profile in ("intimate", "boundary"):
                with self.subTest(provider=name, profile=profile):
                    answer = await self.ns[name](messages, max_tokens=700, timeout=19,
                                                 chat_fast=True, reply_profile=profile)
                    self.assertEqual(answer, JSON_REPLY)
                    call = self.calls[-1]
                    self.assertEqual(call["messages"], messages)
                    self.assertEqual(call["max_tokens"], 700)
                    self.assertEqual(call["timeout"], 19)
                    self.assertEqual(call["extra"], {"chat_fast": True})
                    self.assertEqual(call["scope"], "chat")

    async def test_task_translation_round_trip_preserves_output_and_fast_model_contract(self):
        # Removing task's dedicated lane must lose text, not just change a marker.
        self.reply = TRANSLATION
        messages = [{"role": "system", "content": "Translate the whole passage verbatim."}]
        before = deepcopy(messages)
        ctl.OPTIONS["style_note"] = "只聊两句"
        self.assertGreater(len(self.reply), ctl.OPTIONS["reply_chars"] * ctl.OPTIONS["bubbles"])
        for scope in ("chat", "patrol"):
            ctl.SCOPE.set(scope)
            for name in PROVIDERS:
                for requested in (40, 700, 3500):
                    for private in (False, True):
                        with self.subTest(scope=scope, provider=name, tokens=requested, private=private):
                            self.sent.clear()
                            answer = await self.ns[name](messages, max_tokens=requested, timeout=19,
                                                         chat_fast=True, long_output=False,
                                                         reply_profile="task")
                            self.assertIs(await self.send(private=private, text=answer,
                                                          reply_profile="task"), True)
                            self.assertEqual("".join(x["text"] for x in self.sent), self.reply)
                            call = self.calls[-1]
                            self.assertIs(call["messages"], messages)
                            self.assertEqual(messages, before)
                            self.assertEqual(call["max_tokens"], requested)
                            self.assertEqual(call["timeout"], 19)
                            self.assertEqual(call["extra"], {"chat_fast": True, "long_output": False})

    async def test_explicit_long_model_profile_uses_existing_long_lane(self):
        for name in PROVIDERS:
            answer = await self.ns[name]([], max_tokens=700, reply_profile="long")
            self.assertEqual(answer, JSON_REPLY)
            self.assertEqual(self.calls[-1]["max_tokens"], 3000)
            self.assertEqual(self.calls[-1]["extra"], {"long_output": True})

    async def test_report_flag_still_wins_over_explicit_model_profile(self):
        self.reply = REPORT
        for name in PROVIDERS:
            for profile in ("intimate", "boundary", "long", "task"):
                with self.subTest(provider=name, profile=profile):
                    answer = await self.ns[name]([], max_tokens=700, report=True,
                                                 reply_profile=profile)
                    self.assertEqual(answer.replace("\n", ""), REPORT)
                    self.assertEqual(self.calls[-1]["max_tokens"], 700)
                    self.assertEqual(self.calls[-1]["extra"], {})
                    self.assertIn("办事汇报", self.calls[-1]["messages"][0]["content"])

    async def test_legacy_long_output_keeps_precedence_but_consumes_report(self):
        for name in PROVIDERS:
            answer = await self.ns[name]([], max_tokens=700, long_output=True,
                                         report=True, reply_profile="intimate")
            self.assertEqual(answer, JSON_REPLY)
            self.assertEqual(self.calls[-1]["max_tokens"], 3000)
            self.assertEqual(self.calls[-1]["extra"], {"long_output": True})
            self.assertNotIn("办事汇报", str(self.calls[-1]["messages"]))

    async def test_normal_chat_remains_token_and_bubble_limited(self):
        for name in PROVIDERS:
            answer = await self.ns[name]([], max_tokens=700, chat_fast=True, reply_profile="chat")
            self.assertLessEqual(len(answer.replace("\n", "")), 60)
            self.assertEqual(self.calls[-1]["max_tokens"], 80)
            self.assertEqual(self.calls[-1]["extra"], {"chat_fast": True})
            self.assertIn("水群式回复", self.calls[-1]["messages"][0]["content"])

    async def test_profile_is_consumed_before_strict_provider_signatures(self):
        ns = self.install_stack(strict_provider=True)
        for name in PROVIDERS:
            for profile in ("intimate", "task"):
                with self.subTest(provider=name, profile=profile):
                    self.assertEqual(await ns[name]([], reply_profile=profile, chat_fast=True), JSON_REPLY)
                    self.assertEqual(self.calls[-1]["extra"], {"chat_fast": True, "long_output": False})

    async def test_control_wrappers_explicitly_accept_reply_profile(self):
        # Inspect the actual control closures, not functools.wraps' advertised base signature.
        for name in (*PROVIDERS, "send_group_msg", "send_private_msg"):
            control = self.ns[name].__wrapped__.__wrapped__
            self.assertIn("reply_profile", inspect.signature(control, follow_wrapped=False).parameters)

    async def test_raw_silence_is_preserved_by_model_before_clipping_and_never_sent(self):
        ctl.OPTIONS.update(reply_chars=45, bubbles=3)
        for name in PROVIDERS:
            for marker in ("[[沉默]]", "SILENCE", "silence"):
                for extra in ({}, {"report": True}):
                    with self.subTest(provider=name, marker=marker, extra=extra):
                        self.reply = "牌" * 340 + marker
                        answer = await self.ns[name]([], **extra)
                        self.assertIn(marker, answer)
                        self.sent.clear()
                        self.assertIs(await self.send(text=answer, **extra), False)
                        self.assertEqual(self.sent, [])

    async def test_send_detects_raw_marker_beyond_chat_clip_before_any_delivery(self):
        ctl.OPTIONS.update(reply_chars=45, bubbles=3)
        for private in (False, True):
            self.assertIs(await self.send(private=private, text="牌" * 140 + "[[沉默]]"), False)
        self.assertEqual(self.sent, [])
        self.assertEqual(fun.STATE["totals"].get("sent_replies", 0), 0)

    async def test_send_profiles_bypass_generic_threshold_and_consume_profile(self):
        for private in (False, True):
            for profile in ("intimate", "boundary", "long", "task"):
                for length in (100, 200, 201):
                    with self.subTest(private=private, profile=profile, length=length):
                        self.sent.clear()
                        text = "话" * length
                        self.assertIs(await self.send(private=private, text=text,
                                                      reply_profile=profile, reason="existing reason"), True)
                        self.assertEqual([x["text"] for x in self.sent], [text])
                        self.assertEqual(self.sent[0]['extra']['reply_profile'],
                                         profile if profile in ('long', 'task') else None)
                        self.assertEqual(self.sent[0]["reason"], "existing reason")

    async def test_task_transport_retains_translation_and_code_bytes_with_explicit_profile(self):
        ns = self.install_stack(transport=True)
        import qq_openclaw_bridge as bridge
        pieces = []
        code = 'select * from users\n' + '    value = "唔唔"\n' * 230

        async def submit(ws, action, params, *args, **kwargs):
            pieces.extend(segment["data"]["text"] for segment in params["message"]
                          if segment["type"] == "text")
            return True

        # Keep the real sanitizers and transport packing; replace only IO/bookkeeping.
        for name, kwargs in (
                ("QQ_DRY_RUN", {"new": False}),
                ("_submit_chat", {"side_effect": submit}),
                ("_already_delivered", {"return_value": False}),
                ("_recent_human_texts", {"return_value": []}),
                ("_speaker_names", {"return_value": []}),
                ("_food_record", {}), ("_tail_record", {}), ("_mark_spoke", {}),
                ("record_bot_group_message", {}), ("_record_delivered", {})):
            patcher = patch.object(bridge, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch("self_model.record_action", return_value=""),
                        patch("proactive_dm.mark_contacted"),
                        patch("affection.apply_self", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for text in (TRANSLATION, code):
            self.reply = text
            for private in (False, True):
                with self.subTest(private=private, chars=len(text)):
                    pieces.clear()
                    answer = await ns["call_openclaw"]([], max_tokens=700, chat_fast=True,
                                                      reply_profile="task")
                    self.assertEqual(self.calls[-1]["extra"], {"chat_fast": True})
                    extra = {} if private else {"mention": False}
                    self.assertIs(await self.send(ns, private=private, text=answer,
                                                  reply_profile="task", **extra), True)
                    self.assertEqual(len(pieces), 2 if text == code else 1)
                    self.assertEqual("".join(pieces).encode("utf-8"), text.encode("utf-8"))

    async def test_legacy_long_send_heuristic_is_unchanged_without_profile(self):
        for length, want in ((200, 60), (201, 201)):
            self.sent.clear()
            self.assertIs(await self.send(text="话" * length), True)
            self.assertEqual(sum(len(x["text"]) for x in self.sent), want)

    async def test_report_model_to_send_round_trip_does_not_spend_cap_on_transport_newlines(self):
        self.reply = REPORT
        answer = await self.ns["call_openclaw"]([], max_tokens=700, report=True)
        self.assertIs(await self.send(text=answer, report=True), True)
        self.assertEqual("".join(x["text"] for x in self.sent), REPORT)

    async def test_report_flag_still_wins_over_send_profile_and_reaches_base(self):
        ctl.SCOPE.set("patrol")
        ctl.OPTIONS["patrol_enabled"] = False
        for send_kwargs in (False, True):
            ns = self.install_stack(send_kwargs=send_kwargs)
            for private in (False, True):
                for profile in ("intimate", "task"):
                    with self.subTest(kwargs=send_kwargs, private=private, profile=profile):
                        self.sent.clear()
                        self.assertIs(await self.send(ns, private=private, report=True,
                                                      reply_profile=profile), True)
                        self.assertEqual("".join(x["text"] for x in self.sent), REPORT)
                        self.assertLessEqual(len(self.sent), 2)
                        self.assertTrue(all(len(x["text"]) <= 200 for x in self.sent))
                        self.assertTrue(all(x["extra"]["report"] is True for x in self.sent))
                        self.assertTrue(all(not x["extra"].get("reply_profile") for x in self.sent))
                        if not private:
                            self.assertTrue(self.sent[0]["mention"])
                            self.assertFalse(self.sent[1]["mention"])

    async def test_special_send_profiles_do_not_bypass_optional_patrol_or_blocked_group(self):
        ctl.SCOPE.set("patrol")
        ctl.OPTIONS["patrol_enabled"] = False
        for profile in ("intimate", "boundary", "long", "task"):
            await self.send(reply_profile=profile)
        self.assertEqual(self.sent, [])
        self.blocked = True
        await self.send(report=True, reply_profile="long")
        self.assertEqual(self.sent, [])

    async def test_control_consumes_profile_before_legacy_strict_senders(self):
        ns = self.install_stack(legacy_send=True)
        for private in (False, True):
            for profile in ("long", "task"):
                with self.subTest(private=private, profile=profile):
                    self.sent.clear()
                    self.assertIs(await self.send(ns, private=private, text="话" * 100,
                                                  reply_profile=profile), True)
                    self.assertEqual([x["text"] for x in self.sent], ["话" * 100])
                    self.assertNotIn("reply_profile", self.sent[0]["extra"])

    async def test_kwargs_send_signatures_consume_profile_and_keep_report(self):
        ns = self.install_stack(send_kwargs=True)
        for private in (False, True):
            for profile in ("long", "task"):
                with self.subTest(private=private, profile=profile):
                    self.sent.clear()
                    self.assertIs(await self.send(ns, private=private, text="话" * 100,
                                                  reply_profile=profile), True)
                    self.assertEqual([x["text"] for x in self.sent], ["话" * 100])
                    self.assertNotIn("reply_profile", self.sent[0]["extra"])

    async def test_fun_stats_count_only_accepted_true_not_false_or_none(self):
        ctl.SCOPE.set("other")
        for private in (False, True):
            for receipt in (False, None, True):
                with self.subTest(private=private, receipt=receipt):
                    before = deepcopy(fun.STATE)
                    self.receipts = [receipt]
                    self.assertIs(await self.send(private=private, text="abc"), receipt)
                    increment = 1 if receipt is True else 0
                    self.assertEqual(fun.STATE["totals"].get("sent_replies", 0),
                                     before["totals"].get("sent_replies", 0) + increment)
                    self.assertEqual(fun.STATE["totals"].get("sent_chars", 0),
                                     before["totals"].get("sent_chars", 0) + 3 * increment)

    async def test_control_does_not_turn_unknown_receipt_into_success_for_fun(self):
        self.receipts = [None]
        self.assertIsNone(await self.send(text="一条回复"))
        self.assertEqual(fun.STATE["totals"].get("sent_replies", 0), 0)


if __name__ == "__main__":
    unittest.main()
