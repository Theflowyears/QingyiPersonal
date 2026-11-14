"""工具完成证据：拦截外部 I/O，只核对隔离 SQLite 中的真实事件。"""
import asyncio
import io
import inspect
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BOT = Path(__file__).resolve().parents[1] / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import self_model as sm
import toolkit
import web_read
import asr
import tts
import image_gen
import qwen_agent as qa
import httpx


class IsolatedLedger:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tool_evidence_")
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        self.root = Path(root)
        for patcher in (
                patch.dict(os.environ, {
                    "DATA_DIR": root, "PERSONA_DATA_DIR": str(self.root / "persona"),
                    "QQ_DRY_RUN": "1",
                }),
                patch.object(toolkit, "TOOL_LOG_FILE", str(self.root / "tool_calls.json")),
                patch.object(tts, "TTS_DIR", str(self.root / "voice")),
                patch.object(image_gen, "IMAGE_GEN_DIR", str(self.root / "images")),
                patch.object(socket.socket, "connect", side_effect=AssertionError("real network forbidden")),
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("real DNS forbidden"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def events(self):
        with closing(sm._db()) as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM event WHERE kind='tool_result' ORDER BY rowid")]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def artifact(self, name="artifact.wav", raw=b"offline-fixture"):
        path = self.root / name
        path.write_bytes(raw)
        return str(path)

    def require_scope(self, function):
        parameters = inspect.signature(function).parameters
        for key in ("session", "visibility", "run_id", "evidence_origin"):
            self.assertIn(key, parameters, function.__name__ + " 缺少 " + key)

    def assert_result(self, tool, outcome, session="", visibility="private"):
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["payload"]["tool"], tool)
        self.assertEqual(row["payload"]["outcome"], outcome)
        self.assertIs(row["payload"]["ok"], {"success": True, "failure": False, "unknown": None}[outcome])
        self.assertTrue(row["payload"]["run_id"])
        self.assertEqual(row["session"], session)
        self.assertEqual(row["visibility"], visibility)
        return row


class PanelVerificationTests(IsolatedLedger, unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        super().setUp()

    def test_panel_test_route_marks_verification_not_live_experience(self):
        from flask import Flask
        sys.path.append(str(BOT.parent / 'website'))
        import ceylan_dashboard
        app = Flask(__name__)
        app.secret_key = 'isolated-fixture'
        ceylan_dashboard.install(app, {})
        with app.test_client() as client:
            with client.session_transaction() as session:
                session['ceylan_admin'] = True
            with patch.object(asyncio, 'new_event_loop', return_value=self.loop), \
                 patch.object(qa, 'configured', return_value=True), \
                 patch.object(qa, 'run', new=AsyncMock(return_value={'ok': True, 'text': 'done', 'outcome': 'success'})):
                response = client.post('/admin/api/bot/tools/test', json={'tool': 'tarot', 'query': 'fixture'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.events()[0]['payload'].get('synthetic'))

    def test_tool_statistics_do_not_present_legacy_text_as_completed_tasks(self):
        toolkit.log_call('tarot', True, 1, 'legacy text only')
        toolkit.log_call('tarot', True, 1, 'unverified', extra={'outcome': 'unknown'})
        toolkit.log_call('tarot', True, 1, 'completed', extra={'outcome': 'success'})
        toolkit.log_call('tarot', False, 1, 'failed', extra={'outcome': 'failure'})
        stats = toolkit.stats()
        self.assertEqual(stats['outcomes'], {'success': 1, 'failure': 1, 'unknown': 2})


class LedgerContractTests(IsolatedLedger, unittest.TestCase):
    # 删除 outcome、把 None 转 False、扩大 session 前缀都会被下列用例抓住。
    def test_legacy_boolean_call_gets_explicit_outcome(self):
        for ok, want in ((True, "success"), (False, "failure")):
            eid = sm.record_tool_result("legacy", "ref", ok)
            payload = json.loads(sm.get_event(eid)["payload"])
            self.assertEqual(payload.get("outcome"), want)
            self.assertIs(payload["ok"], ok)

    def test_unknown_preserves_null_and_execution_dedupe(self):
        try:
            a = sm.record_tool_result("agent", "first", None, outcome="unknown", run_id="same")
            b = sm.record_tool_result("agent", "second", outcome="unknown", run_id="same")
        except (TypeError, ValueError) as exc:
            self.fail("三态接口尚未实现: " + str(exc))
        self.assertEqual(a, b)
        self.assert_result("agent", "unknown")

    def test_outcome_only_derives_boolean(self):
        for outcome, want in (("success", True), ("failure", False), ("unknown", None)):
            try:
                eid = sm.record_tool_result("agent", "ref", outcome=outcome)
            except TypeError as exc:
                self.fail("outcome-only 接口缺失: " + str(exc))
            self.assertIs(json.loads(sm.get_event(eid)["payload"])["ok"], want)

    def test_invalid_or_conflicting_status_is_rejected(self):
        for kw in ({"ok": None}, {"ok": 1}, {"ok": "false"}, {"ok": True, "outcome": "failure"},
                   {"ok": False, "outcome": "unknown"}, {"ok": None, "outcome": "success"},
                   {"ok": True, "outcome": "done"}, {"ok": True, "evidence_origin": "guess"}):
            with self.subTest(kw=kw):
                try:
                    with self.assertRaises(ValueError):
                        sm.record_tool_result("agent", "ref", **kw)
                except TypeError as exc:
                    self.fail("新参数应验证而不是缺失: " + str(exc))
        self.assertEqual(self.events(), [])

    def test_new_executions_and_different_tools_are_independent(self):
        ids = [sm.record_tool_result("a", "same", True), sm.record_tool_result("a", "same", True),
               sm.record_tool_result("a", "same", True, run_id="shared"),
               sm.record_tool_result("b", "same", True, run_id="shared")]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all("outcome" in row["payload"] for row in self.events()))

    def test_verification_prefix_is_token_bounded(self):
        for session, synthetic in (("verify:one", True), ("test-one", True), ("probe/one", True),
                                   ("VERIFY_one", True), ("test", True), ("testimony", False),
                                   ("prober", False), ("verification", False), ("group:test", False)):
            eid = sm.record_tool_result("agent", "ref", True, session=session)
            row = sm.get_event(eid)
            payload = json.loads(row["payload"])
            self.assertEqual(row["source"].startswith("synthetic:"), synthetic, session)
            self.assertEqual(payload.get("synthetic", False), synthetic, session)

    def test_explicit_origin_overrides_context_and_session(self):
        context = getattr(sm, "tool_evidence_context", None)
        self.assertTrue(callable(context), "缺少验证 ContextVar 上下文接口")
        with context("verification"):
            a = sm.record_tool_result("agent", "ref", True, session="group:1")
            b = sm.record_tool_result("agent", "ref", True, session="test:1", evidence_origin="live")
        c = sm.record_tool_result("agent", "ref", True)
        self.assertTrue(sm.get_event(a)["source"].startswith("synthetic:"))
        self.assertFalse(sm.get_event(b)["source"].startswith("synthetic:"))
        self.assertFalse(sm.get_event(c)["source"].startswith("synthetic:"))

    def test_dedupe_does_not_alias_delimiters_in_tool_and_run_id(self):
        first = sm.record_tool_result("a|b", "ref", True, run_id="c")
        second = sm.record_tool_result("a", "ref", True, run_id="b|c")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.events()), 2)

    def test_verification_event_cannot_affect_live_shadow_state(self):
        import time
        import emotion_shadow as shadow
        with closing(sm._db()):
            pass
        shadow.tick(now=time.time())
        sm.record_tool_result("agent", "ref", True, session="probe:offline")
        shadow.tick(now=time.time())
        info = shadow.status(now=time.time())
        self.assertEqual(info["real_events"], 0)
        self.assertTrue(info["timeline"][0]["ignored"])
        self.assertEqual(info["state"]["control"], 0.5)
        self.assertEqual(info["state"]["attention"], 0.0)


class AgentEvidenceTests(IsolatedLedger, unittest.IsolatedAsyncioTestCase):
    async def call_agent(self, result, **kwargs):
        with patch.object(qa, "configured", return_value=True), patch.object(qa, "run", new=AsyncMock(return_value=result)):
            return await toolkit.run_agent_tool("excel", "private question must not enter ledger", "private context", **kwargs)

    async def test_text_is_unknown_not_task_success_and_legacy_ok_survives(self):
        for text in ("I'll activate the skill", "我来抽", "安装失败，改用随机源", "宝剑三逆位：完整牌面解读"):
            with self.subTest(text=text):
                await self.call_agent({"ok": True, "text": text, "images": [], "audio": []})
        rows = self.events()
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["payload"].get("outcome") for row in rows], ["unknown"] * 4)
        self.assertTrue(all(row["payload"]["ok"] is None for row in rows))
        result = await self.call_agent({"ok": True, "text": "我来抽"})
        self.assertTrue(result["ok"], "旧调用仍可消费 agent 文本")
        self.assertEqual(result["outcome"], "unknown")
        block = toolkit.build_block("excel", result)
        self.assertNotIn("办完了", block)
        self.assertNotIn("以下是真实结果", block)

    async def test_structured_completion_propagates_scope_and_hash_only_ref(self):
        self.require_scope(toolkit.run_agent_tool)
        result = await self.call_agent({"ok": True, "text": "done", "outcome": "success"},
                                       session="group:1", visibility="group", run_id="execution-one")
        self.assertEqual(result.get("outcome"), "success")
        row = self.assert_result("excel", "success", "group:1", "group")
        self.assertEqual(row["payload"]["run_id"], "execution-one")
        self.assertNotIn("private", json.dumps(row["payload"]))
        self.assertTrue(row["payload"]["ref"].startswith("sha256:"))

    async def test_agent_false_overrides_claimed_success(self):
        await self.call_agent({"ok": False, "text": "done", "outcome": "success"})
        self.assert_result("excel", "failure")

    async def test_structured_failure_with_text_is_failure(self):
        await self.call_agent({"ok": True, "text": "details", "outcome": "failure"})
        self.assert_result("excel", "failure")

    async def test_empty_output_is_failure(self):
        await self.call_agent({"ok": True, "text": "", "images": [], "audio": []})
        self.assert_result("excel", "failure")

    async def test_none_response_is_empty_failure(self):
        await self.call_agent(None)
        self.assert_result("excel", "failure")

    async def test_reobserving_execution_dedupes_but_retry_gets_new_event(self):
        response = {"ok": True, "text": "done", "outcome": "success"}
        await self.call_agent(response, run_id="same-execution")
        await self.call_agent(response, run_id="same-execution")
        await self.call_agent(response, run_id="retry-execution")
        self.assertEqual(len(self.events()), 2)

    async def test_cancellation_is_recorded_once_and_still_propagates(self):
        with patch.object(qa, "configured", return_value=True), patch.object(qa, "run", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await toolkit.run_agent_tool("excel", "private question")
        self.assert_result("excel", "failure")

    async def test_ledger_failure_does_not_erase_result_and_is_logged(self):
        with patch.object(sm, "record_tool_result", side_effect=OSError("offline")), self.assertLogs(level="ERROR"):
            result = await self.call_agent({"ok": True, "text": "done", "outcome": "success"})
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["text"], "done")

    async def test_remote_artifact_url_without_completion_evidence_is_unknown(self):
        await self.call_agent({"ok": True, "text": "", "images": ["https://invalid.example/result.png"]})
        self.assert_result("excel", "unknown")

    async def test_existing_nonempty_local_artifact_confirms_completion(self):
        await self.call_agent({"ok": True, "text": "", "images": [self.artifact("result.png")]})
        self.assert_result("excel", "success")

    async def test_missing_or_empty_local_artifact_is_not_success(self):
        await self.call_agent({"ok": True, "text": "done", "images": [self.artifact("empty.png", b"")]})
        self.assert_result("excel", "unknown")

    async def test_unconfigured_and_unknown_tool_are_each_recorded(self):
        with patch.object(qa, "configured", return_value=False):
            await toolkit.run_agent_tool("excel", "secret")
        await toolkit.run_agent_tool("missing", "secret")
        rows = self.events()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["payload"].get("outcome") for row in rows], ["failure", "failure"])

    async def test_exception_is_a_logged_failure_not_an_unobserved_raise(self):
        with patch.object(qa, "configured", return_value=True), patch.object(qa, "run", new=AsyncMock(side_effect=RuntimeError("private question"))):
            try:
                with self.assertLogs(level="WARNING"):
                    result = await toolkit.run_agent_tool("excel", "private question")
            except RuntimeError as exc:
                self.fail("异常缺少统一收口: " + str(exc))
        self.assertFalse(result["ok"])
        row = self.assert_result("excel", "failure")
        self.assertNotIn("private question", json.dumps(row["payload"]))

    async def test_contexts_do_not_leak_between_tasks(self):
        context = getattr(sm, "tool_evidence_context", None)
        self.assertTrue(callable(context), "缺少验证上下文接口")
        async def execute(origin):
            with context(origin):
                await asyncio.sleep(0)
                return await self.call_agent({"ok": True, "text": "done", "outcome": "success"})
        await asyncio.gather(execute("verification"), execute("live"))
        rows = self.events()
        self.assertEqual(sum(row["source"].startswith("synthetic:") for row in rows), 1)
        self.assertEqual(len({row["payload"]["run_id"] for row in rows}), 2)


class LocalIOEvidenceTests(IsolatedLedger, unittest.IsolatedAsyncioTestCase):
    async def test_web_read_emits_once_after_real_decode_and_extraction(self):
        self.require_scope(web_read.fetch_page)
        got = {"ok": True, "body": ("<p>" + "offline readable content " * 4 + "</p>").encode(),
               "content_type": "text/html", "final_url": "https://invalid.example/"}
        with patch.object(web_read, "_download", new=AsyncMock(return_value=got)):
            result = await web_read.fetch_page("https://invalid.example/?secret=query", session="group:2", visibility="group", run_id="page-one")
        self.assertTrue(result["ok"])
        row = self.assert_result("web_read", "success", "group:2", "group")
        self.assertNotIn("secret", json.dumps(row["payload"]))

    async def test_web_boundary_failure_is_logged_and_recorded(self):
        with self.assertLogs(level="WARNING") as logs:
            result = await web_read.fetch_page("http://127.0.0.1/private")
        self.assertIn("unsafe_url", " ".join(logs.output))
        self.assertFalse(result["ok"])
        self.assert_result("web_read", "failure")

    async def test_web_unexpected_exception_is_recorded(self):
        with patch.object(web_read, "_download", new=AsyncMock(side_effect=OSError("offline"))):
            try:
                result = await web_read.fetch_page("https://invalid.example/")
            except OSError:
                self.fail("网页异常没有收口")
        self.assertFalse(result["ok"])
        self.assert_result("web_read", "failure")

    async def test_asr_sync_and_async_record_once_per_execution(self):
        self.require_scope(asr.transcribe_async)
        self.require_scope(asr.transcribe)
        path = self.artifact("input.wav")
        response = SimpleNamespace(status_code=200, get_sentence=lambda: {"text": "offline transcript"})
        recognition = SimpleNamespace(call=lambda _: response)
        module = SimpleNamespace(Recognition=lambda **kw: recognition)
        with patch.object(asr, "configured", return_value=True), patch.object(asr, "_configure", return_value=True), \
                patch.object(asr, "_resample_wav", return_value=None), patch.dict(sys.modules, {"dashscope.audio.asr": module}):
            first = asr.transcribe(path)
            second = await asr.transcribe_async(path, session="test:asr", run_id="asr-two")
        self.assertTrue(first["ok"] and second["ok"])
        rows = self.events()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["payload"].get("outcome") for row in rows], ["success", "success"])
        self.assertTrue(rows[1]["source"].startswith("synthetic:"))
        self.assertNotIn("offline transcript", json.dumps(rows))

    async def test_asr_missing_configuration_is_a_logged_failure(self):
        with patch.object(asr, "configured", return_value=False), self.assertLogs(level="WARNING") as logs:
            self.assertFalse(asr.transcribe("missing.wav")["ok"])
        self.assertIn("not_configured", " ".join(logs.output))
        self.assert_result("asr", "failure")

    async def test_tts_records_downloaded_artifact_not_each_fallback(self):
        self.require_scope(tts.synth)
        path = self.artifact()
        with patch.object(tts, "TTS_ENABLED", True), patch.object(tts, "TTS_KEY", "offline"), \
                patch.object(tts, "_sing_dashscope", new=AsyncMock(return_value={"ok": False, "error": "offline failure"})), \
                patch.object(tts, "_sing_next", new=AsyncMock(return_value={"ok": True, "path": path})):
            result = await tts.synth("secret lyrics", sing=True, session="group:3", visibility="group")
        self.assertTrue(result["ok"])
        self.assert_result("tts", "success", "group:3", "group")

    async def test_tts_success_claim_without_artifact_is_unknown(self):
        with patch.object(tts, "TTS_ENABLED", True), patch.object(tts, "TTS_KEY", "offline"), \
                patch.object(tts, "_speak_dashscope", new=AsyncMock(return_value={"ok": True, "path": "missing.wav"})):
            await tts.synth("private words")
        self.assert_result("tts", "unknown")

    async def test_tts_disabled_boundary_is_a_logged_failure(self):
        with patch.object(tts, "TTS_ENABLED", False), self.assertLogs(level="WARNING"):
            self.assertFalse((await tts.synth("private words"))["ok"])
        self.assert_result("tts", "failure")

    async def test_image_refusal_records_once_and_keeps_existing_return_kind(self):
        self.require_scope(image_gen.generate)
        with patch.object(image_gen, "_generate_impl", new=AsyncMock(return_value={"ok": False, "error": "safety policy"})):
            result = await image_gen.generate("private prompt", session="probe:image")
        self.assertEqual(result["kind"], "refused")
        row = self.assert_result("image_gen", "failure", "probe:image")
        self.assertTrue(row["payload"]["synthetic"])

    async def test_image_existing_artifact_confirms_completion(self):
        path = self.artifact("result.png")
        with patch.object(image_gen, "_generate_impl", new=AsyncMock(return_value={"ok": True, "path": path, "bytes": 15})):
            await image_gen.generate("private prompt")
        self.assert_result("image_gen", "success")

    async def test_image_url_only_does_not_confirm_download(self):
        with patch.object(image_gen, "_generate_impl", new=AsyncMock(return_value={"ok": True, "url": "https://invalid.example/result.png"})):
            await image_gen.generate("private prompt")
        self.assert_result("image_gen", "unknown")

    async def test_asr_verification_context_survives_thread_pool(self):
        self.assertTrue(callable(getattr(sm, "tool_evidence_context", None)))
        with sm.tool_evidence_context("verification"), patch.object(asr, "configured", return_value=False):
            await asr.transcribe_async("missing.wav", session="private:1")
        row = self.assert_result("asr", "failure", "private:1")
        self.assertTrue(row["payload"]["synthetic"])

    async def test_tts_builtin_rejects_http_error_body_as_audio(self):
        # HTTP 500 的非空错误页不能落盘成 wav 后记 success。
        synth_module = SimpleNamespace(SpeechSynthesizer=SimpleNamespace(call=lambda **kw: {
            "output": {"audio": {"url": "https://invalid.example/audio.wav"}}
        }))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = httpx.Response(500, content=b"upstream error", request=httpx.Request("GET", "https://invalid.example/audio.wav"))
        client.post.return_value = httpx.Response(503, content=b"unavailable", request=httpx.Request("POST", "https://invalid.example/tts"))
        with patch.object(tts, "TTS_ENABLED", True), patch.object(tts, "TTS_KEY", "offline"), \
                patch.object(tts, "_runtime_cfg", return_value={}), patch.object(tts, "_provider", return_value="builtin"), \
                patch.dict(sys.modules, {"dashscope": SimpleNamespace(), "dashscope.audio.qwen_tts": synth_module}), \
                patch.object(tts.httpx, "AsyncClient", return_value=client):
            result = await tts.synth("offline speech")
        self.assertFalse(result["ok"])
        self.assert_result("tts", "failure")
        self.assertFalse(list(self.root.rglob("*.wav")))

    async def test_tts_real_builtin_download_confirms_one_result(self):
        synth_module = SimpleNamespace(SpeechSynthesizer=SimpleNamespace(call=lambda **kw: {
            "output": {"audio": {"data": "UklGRm9mZmxpbmU="}}
        }))
        with patch.object(tts, "TTS_ENABLED", True), patch.object(tts, "TTS_KEY", "offline"), \
                patch.object(tts, "_runtime_cfg", return_value={}), \
                patch.dict(sys.modules, {"dashscope": SimpleNamespace(), "dashscope.audio.qwen_tts": synth_module}):
            result = await tts.synth("offline speech")
        self.assertTrue(Path(result["path"]).is_file())
        self.assert_result("tts", "success")

    async def test_image_real_download_confirms_one_result(self):
        def response(method, url, **kw):
            data = {"output": {"choices": [{"message": {"content": [{"image": "https://invalid.example/result.png"}]}}]}}
            return httpx.Response(200, json=data if method == "POST" else None,
                                  content=None if method == "POST" else b"offline-image",
                                  headers={"content-type": "image/png"}, request=httpx.Request(method, url))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = lambda url, **kw: response("POST", url, **kw)
        client.get.side_effect = lambda url, **kw: response("GET", url, **kw)
        with patch.object(image_gen, "IMAGE_GEN_ENABLED", True), patch.object(image_gen, "FLARE_ENABLED", False), \
                patch.object(image_gen, "flare_ready", return_value=False), patch.object(image_gen, "qwenimg_ready", return_value=False), \
                patch.object(image_gen, "_provider", return_value="builtin"), \
                patch.object(image_gen, "_runtime_cfg", return_value={"api_key": "offline"}), \
                patch.object(image_gen, "build_final_prompt", new=AsyncMock(return_value=("offline prompt", {}))), \
                patch.object(image_gen.httpx, "AsyncClient", return_value=client):
            result = await image_gen.generate("offline prompt")
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["path"]).read_bytes(), b"offline-image")
        self.assert_result("image_gen", "success")

    async def test_image_unexpected_exception_is_logged_and_recorded(self):
        with patch.object(image_gen, "_generate_impl", new=AsyncMock(side_effect=OSError("offline"))):
            try:
                with self.assertLogs(level="WARNING"):
                    result = await image_gen.generate("private prompt")
            except OSError:
                self.fail("绘图异常没有收口")
        self.assertFalse(result["ok"])
        self.assert_result("image_gen", "failure")


if __name__ == "__main__":
    unittest.main()
