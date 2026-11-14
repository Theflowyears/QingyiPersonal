"""Exercise installed model wrappers without loading the live bridge."""
import asyncio
from collections import Counter
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
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


PROVIDERS = ("call_openclaw", "call_mimo_vision")
CHAT_ENTRIES = (
    ("handle_group_message", "chat", (None, {"group_id": "123"})),
    ("handle_private_message", "chat", (None, {"user_id": "456"})),
    ("periodic_group_review", "patrol", (None,)),
)
JSON_REPLY = " \n" + json.dumps({
    "summary": "保留完整的背景图像观察，不要改写为群聊。" * 12,
    "mood": "困惑",
    "details": ["等待回答", "文字与图像分开记录", "保留结尾字段"],
}, ensure_ascii=False, indent=2) + "\n "


class InternalModelScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="model_scope_")
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        runtime = patch.multiple(
            ctl, ROOT=Path(root), OPTIONS={
                **ctl.DEFAULTS, "reply_tokens": 80, "reply_chars": 30,
                "bubbles": 2, "style_note": "自然一点",
            }, BUSY=Counter(), LEDGER={}, BATCH_LOCKS={},
        )
        runtime.start()
        self.addCleanup(runtime.stop)
        token = ctl.SCOPE.set("other")
        self.addCleanup(ctl.SCOPE.reset, token)
        self.calls = []
        self.hooks = {}

        async def handle_group_message(ws, event):
            return await self.work()

        async def handle_private_message(ws, event):
            return await self.work()

        async def periodic_group_review(ws):
            return await self.work()

        async def compact_group_memory_once(ws, group_id):
            return await self.work()

        async def run_agent_task(ws, event):
            return await self.work()

        async def provider(name, messages, max_tokens, timeout, extra):
            self.calls.append({
                "name": name, "messages": messages, "max_tokens": max_tokens,
                "timeout": timeout, "extra": dict(extra), "scope": ctl.SCOPE.get(),
            })
            if name in self.hooks:
                return await self.hooks[name]()
            return JSON_REPLY

        async def call_openclaw(messages, max_tokens=800, timeout=300, **extra):
            return await provider("call_openclaw", messages, max_tokens, timeout, extra)

        async def call_mimo_vision(messages, max_tokens=800, timeout=300, **extra):
            return await provider("call_mimo_vision", messages, max_tokens, timeout, extra)

        async def send_group_msg(ws, group_id, text, mention=False):
            return None

        async def send_private_msg(ws, user_id, text):
            return None

        def build_group_recent_text(group_id):
            return ("", 0, 0, 0, [])

        self.ns = {
            "handle_group_message": handle_group_message,
            "handle_private_message": handle_private_message,
            "periodic_group_review": periodic_group_review,
            "compact_group_memory_once": compact_group_memory_once,
            "run_agent_task": run_agent_task,
            "call_openclaw": call_openclaw,
            "call_mimo_vision": call_mimo_vision,
            "send_group_msg": send_group_msg,
            "send_private_msg": send_private_msg,
            "build_group_recent_text": build_group_recent_text,
        }
        self.rt = SimpleNamespace(blocked=lambda group_id: False, snapshot=lambda: None)
        with redirect_stdout(io.StringIO()):
            ctl.install(self.ns, self.rt)

    async def test_internal_700_token_budget_preserves_json_and_arguments(self):
        for name in PROVIDERS:
            for entry, scope, args in CHAT_ENTRIES:
                for long_output in (False, True):
                    with self.subTest(provider=name, entry=entry, long_output=long_output):
                        messages = [
                            {"role": "system", "content": "Return only JSON."},
                            {"role": "user", "content": [
                                {"type": "text", "text": "Describe the image."},
                                {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
                            ]},
                        ]
                        original = deepcopy(messages)
                        extra = {"internal": True, "chat_fast": True}
                        if long_output:
                            extra["long_output"] = True

                        async def work():
                            self.assertEqual(ctl.SCOPE.get(), scope)
                            answer = await self.ns[name](messages, max_tokens=700, timeout=17, **extra)
                            self.assertEqual(ctl.SCOPE.get(), scope)
                            return answer

                        self.work = work
                        answer = await self.ns[entry](*args)
                        call = self.calls[-1]
                        self.assertEqual(answer.encode("utf-8"), JSON_REPLY.encode("utf-8"))
                        self.assertEqual(json.loads(answer), json.loads(JSON_REPLY))
                        self.assertIs(call["messages"], messages)
                        self.assertEqual(messages, original)
                        self.assertEqual(call["max_tokens"], 700)
                        self.assertEqual(call["timeout"], 17)
                        self.assertEqual(call["extra"], {k: v for k, v in extra.items() if k != "internal"})
                        self.assertEqual(call["scope"], "internal")
                        self.assertEqual(ctl.SCOPE.get(), "other")

    async def test_external_chat_still_copies_clamps_and_chunks(self):
        for name in PROVIDERS:
            for entry, scope, args in CHAT_ENTRIES:
                for system in (True, False):
                    for extra in ({}, {"internal": False}):
                        with self.subTest(provider=name, entry=entry, system=system, extra=extra):
                            messages = [{"role": "user", "content": "Return JSON."}]
                            if system:
                                messages.insert(0, {"role": "system", "content": "Original instructions."})
                            original = deepcopy(messages)

                            async def work():
                                return await self.ns[name](messages, max_tokens=700, timeout=19, chat_fast=True, **extra)

                            self.work = work
                            answer = await self.ns[entry](*args)
                            call = self.calls[-1]
                            self.assertEqual(answer, "\n".join(ctl.chunks(JSON_REPLY)))
                            self.assertNotEqual(answer, JSON_REPLY)
                            self.assertLess(len(answer), len(JSON_REPLY))
                            self.assertEqual(call["max_tokens"], 80)
                            self.assertEqual(call["timeout"], 19)
                            self.assertEqual(call["scope"], scope)
                            self.assertEqual(call["extra"], {"chat_fast": True})
                            self.assertIsNot(call["messages"], messages)
                            offset = 0 if system else 1
                            for source, copied in zip(messages, call["messages"][offset:]):
                                self.assertIsNot(source, copied)
                            self.assertIn("水群式回复", call["messages"][0]["content"])
                            self.assertIn("自然一点", call["messages"][0]["content"])
                            if system:
                                self.assertTrue(call["messages"][0]["content"].startswith(original[0]["content"]))
                            self.assertEqual(messages, original)
                            self.assertEqual(ctl.SCOPE.get(), "other")

    async def test_chat_token_clamp_does_not_raise_smaller_requests(self):
        for name in PROVIDERS:
            for entry, scope, args in CHAT_ENTRIES:
                with self.subTest(provider=name, entry=entry):
                    async def work():
                        return await self.ns[name]([], max_tokens=40)

                    self.work = work
                    await self.ns[entry](*args)
                    self.assertEqual(self.calls[-1]["max_tokens"], 40)

    async def test_unmarked_nonchat_calls_remain_unchanged(self):
        entries = (
            ("compact_group_memory_once", "memory", (None, "123")),
            ("run_agent_task", "task", (None, {})),
            (None, "other", ()),
        )
        for name in PROVIDERS:
            for entry, scope, args in entries:
                with self.subTest(provider=name, scope=scope):
                    messages = [{"role": "user", "content": "Return JSON."}]

                    async def work():
                        return await self.ns[name](messages, max_tokens=700, chat_fast=True)

                    self.work = work
                    answer = await self.ns[entry](*args) if entry else await work()
                    call = self.calls[-1]
                    self.assertEqual(answer, JSON_REPLY)
                    self.assertIs(call["messages"], messages)
                    self.assertEqual(call["max_tokens"], 700)
                    self.assertEqual(call["scope"], scope)
                    self.assertEqual(call["extra"], {"chat_fast": True})
                    self.assertEqual(ctl.SCOPE.get(), "other")

    async def test_existing_long_output_behavior_is_unchanged(self):
        for name in PROVIDERS:
            for entry, scope, args in CHAT_ENTRIES:
                for requested in (700, 3500):
                    with self.subTest(provider=name, entry=entry, requested=requested):
                        messages = [{"role": "user", "content": "Write a story."}]

                        async def work():
                            return await self.ns[name](messages, max_tokens=requested, long_output=True)

                        self.work = work
                        self.assertEqual(await self.ns[entry](*args), JSON_REPLY)
                        call = self.calls[-1]
                        self.assertIs(call["messages"], messages)
                        self.assertEqual(call["max_tokens"], max(requested, 3000))
                        self.assertEqual(call["extra"], {"long_output": True})
                        self.assertEqual(call["scope"], scope)

    async def test_nested_unmarked_subcalls_inherit_internal_scope(self):
        for outer, inner in (PROVIDERS, tuple(reversed(PROVIDERS))):
            for entry, scope, args in CHAT_ENTRIES:
                for spawned in (False, True):
                    with self.subTest(outer=outer, entry=entry, spawned=spawned):
                        self.calls.clear()
                        self.hooks.clear()
                        messages = [{"role": "user", "content": "Classify the search."}]
                        nested_messages = [{"role": "user", "content": "Summarize the evidence."}]

                        async def nested():
                            call = self.ns[inner](nested_messages, max_tokens=700, timeout=23)
                            answer = await asyncio.create_task(call) if spawned else await call
                            self.assertEqual(ctl.SCOPE.get(), "internal")
                            return answer

                        self.hooks[outer] = nested

                        async def work():
                            answer = await self.ns[outer](messages, max_tokens=700, internal=True)
                            self.assertEqual(ctl.SCOPE.get(), scope)
                            return answer

                        self.work = work
                        self.assertEqual(await self.ns[entry](*args), JSON_REPLY)
                        self.assertEqual([call["name"] for call in self.calls], [outer, inner])
                        for call, expected in zip(self.calls, (messages, nested_messages)):
                            self.assertEqual(call["scope"], "internal")
                            self.assertEqual(call["max_tokens"], 700)
                            self.assertIs(call["messages"], expected)
                            self.assertNotIn("internal", call["extra"])
                        self.assertEqual(ctl.SCOPE.get(), "other")

    async def test_scope_is_restored_when_provider_raises(self):
        for name in PROVIDERS:
            for entry, scope, args in CHAT_ENTRIES:
                with self.subTest(provider=name, entry=entry):
                    self.hooks.clear()
                    error = RuntimeError("provider failed")

                    async def fail():
                        raise error

                    self.hooks[name] = fail

                    async def work():
                        with self.assertRaises(RuntimeError) as caught:
                            await self.ns[name]([], max_tokens=700, internal=True)
                        self.assertIs(caught.exception, error)
                        self.assertEqual(ctl.SCOPE.get(), scope)

                    self.work = work
                    await self.ns[entry](*args)
                    self.assertEqual(self.calls[-1]["scope"], "internal")
                    self.assertNotIn("internal", self.calls[-1]["extra"])
                    self.assertEqual(ctl.SCOPE.get(), "other")

    async def test_scope_is_restored_when_provider_is_cancelled(self):
        for name in PROVIDERS:
            for entry, scope, args in CHAT_ENTRIES:
                with self.subTest(provider=name, entry=entry):
                    self.hooks.clear()
                    entered = asyncio.Event()
                    provider_scopes = []
                    restored_scopes = []

                    async def wait_forever():
                        entered.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            provider_scopes.append(ctl.SCOPE.get())

                    self.hooks[name] = wait_forever

                    async def invoke():
                        try:
                            await self.ns[name]([], max_tokens=700, internal=True)
                        except asyncio.CancelledError:
                            restored_scopes.append(ctl.SCOPE.get())
                            raise

                    async def work():
                        task = asyncio.create_task(invoke())
                        try:
                            await asyncio.wait_for(entered.wait(), timeout=2)
                        finally:
                            task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertEqual(ctl.SCOPE.get(), scope)

                    self.work = work
                    await self.ns[entry](*args)
                    self.assertEqual(restored_scopes, [scope])
                    self.assertEqual(provider_scopes, ["internal"])
                    self.assertNotIn("internal", self.calls[-1]["extra"])
                    self.assertEqual(ctl.SCOPE.get(), "other")


if __name__ == "__main__":
    unittest.main()
