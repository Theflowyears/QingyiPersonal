"""回归锁：视频理解必须单独走 qwen3.8-omni-flash（全模态）。

qwen3.8-omni-flash 官方口径是全模态、支持视频输入、兼容 OpenAI Chat
Completions；用户要求视频识别只用它。三类真实错位各配锁：

  1. 没配 LLM_VIDEO_* 时，视频请求不能退化成视觉通道的 qwen3.8-flash
     （它看不了视频），更不能退化成文本模型。
  2. 视频消息里的 video_url / 视频帧数组 / text 必须原样到达上游：
     改写 URL、丢字段、把图片消息「转换」成视频，都会让模型看到错东西。
  3. 请求体不能带 audio 输出参数；返回结构异常必须明确抛错，
     而不是静默返回一句「模型未返回有效内容」。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import llm_router as lr  # noqa: E402


VIDEO_MODEL = "qwen3.8-omni-flash"
FLASH_MODEL = "qwen3.8-flash"
GOOD_KEY = "sk-" + "k" * 40


def _cfg(model, base="https://vision.example.com/compatible-mode",
         key=GOOD_KEY, effort="", name="fake"):
    return lr.ModelConfig(name, base, key, model, 2048, 120, reasoning_effort=effort)


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


class VideoModelConfigTests(unittest.TestCase):
    """get_video_model 的解析优先级。"""

    def test_unconfigured_video_uses_omni_not_flash(self):
        vision = _cfg(FLASH_MODEL)
        with patch.object(lr, "LLM_VIDEO", None, create=True), \
                patch.object(lr, "LLM_VISION", vision), \
                patch.object(lr, "LLM_TEXT", _cfg(FLASH_MODEL, base="https://text.example.com")):
            got = lr.get_video_model()
        self.assertEqual(got.model, VIDEO_MODEL)
        self.assertNotEqual(got.model, FLASH_MODEL)
        # 地址与 Key 复用视觉通道（同一家 Qwen），模型钉死全模态
        self.assertEqual(got.base_url, vision.base_url)
        self.assertEqual(got.api_key, vision.api_key)

    def test_vision_already_omni_is_reused_as_is(self):
        omni = _cfg(VIDEO_MODEL)
        with patch.object(lr, "LLM_VIDEO", None, create=True), \
                patch.object(lr, "LLM_VISION", omni):
            self.assertIs(lr.get_video_model(), omni)

    def test_explicit_video_config_wins(self):
        explicit = _cfg("qwen3.8-omni-flash-pro", base="https://video.example.com")
        with patch.object(lr, "LLM_VIDEO", explicit, create=True), \
                patch.object(lr, "LLM_VISION", _cfg(FLASH_MODEL)):
            self.assertIs(lr.get_video_model(), explicit)

    def test_placeholder_video_key_falls_back_to_omni(self):
        vision = _cfg(FLASH_MODEL)
        bad = _cfg("qwen3.8-omni-flash-pro", base="https://bad.example.com",
                   key="sk-your-key-here")
        with patch.object(lr, "LLM_VIDEO", bad, create=True), \
                patch.object(lr, "LLM_VISION", vision):
            got = lr.get_video_model()
        self.assertEqual(got.model, VIDEO_MODEL)
        self.assertEqual(got.api_key, vision.api_key)

    def test_video_never_falls_back_to_a_text_model(self):
        # 视觉通道也没配时，get_vision_model() 会回退文本模型；
        # 视频仍必须是 omni，绝不能把视频发给 qwen3.8-flash。
        with patch.object(lr, "LLM_VIDEO", None, create=True), \
                patch.object(lr, "LLM_VISION", None), \
                patch.object(lr, "LLM_TEXT", _cfg(FLASH_MODEL)):
            got = lr.get_video_model()
        self.assertEqual(got.model, VIDEO_MODEL)

    def test_nothing_configured_raises_clearly(self):
        with patch.object(lr, "LLM_VIDEO", None, create=True), \
                patch.object(lr, "LLM_VISION", None), \
                patch.object(lr, "LLM_TEXT", None):
            with self.assertRaises(RuntimeError) as ctx:
                lr.get_video_model()
        self.assertIn("视频", str(ctx.exception))

    def test_config_default_effort_applies(self):
        with patch.object(lr, "LLM_VIDEO", _cfg(VIDEO_MODEL, effort="high"), create=True):
            self.assertEqual(lr.config_default_effort(lr.get_video_model()), "high")

    def test_list_models_contains_video_role(self):
        with patch.object(lr, "LLM_VIDEO", _cfg(VIDEO_MODEL), create=True), \
                patch.object(lr, "LLM_VISION", _cfg(FLASH_MODEL)), \
                patch.object(lr, "LLM_TEXT", _cfg(FLASH_MODEL)):
            models = lr.list_models()
        video = [m for m in models if m["role"] == "video"]
        self.assertEqual(len(video), 1)
        self.assertEqual(video[0]["model"], VIDEO_MODEL)


class VideoEnvConfigTests(unittest.TestCase):
    """LLM_VIDEO_* 环境变量读取：显式 MODEL 覆盖、缺省 MODEL 用 omni。"""

    def test_explicit_video_model_env_wins(self):
        env = {
            "LLM_VIDEO_BASE_URL": "https://video-env.example.com",
            "LLM_VIDEO_API_KEY": GOOD_KEY,
            "LLM_VIDEO_MODEL": "qwen3.8-omni-flash-custom",
        }
        with patch.dict(os.environ, env, clear=False):
            got = lr._get_video_from_env()
        self.assertIsNotNone(got)
        self.assertEqual(got.model, "qwen3.8-omni-flash-custom")

    def test_env_without_model_defaults_to_omni(self):
        env = {
            "LLM_VIDEO_BASE_URL": "https://video-env.example.com",
            "LLM_VIDEO_API_KEY": GOOD_KEY,
            "LLM_VIDEO_MODEL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            got = lr._get_video_from_env()
        self.assertIsNotNone(got)
        self.assertEqual(got.model, VIDEO_MODEL)

    def test_no_video_env_is_none(self):
        env = {k: "" for k in ("LLM_VIDEO_BASE_URL", "LLM_VIDEO_API_KEY", "LLM_VIDEO_MODEL")}
        with patch.dict(os.environ, env, clear=False):
            self.assertIsNone(lr._get_video_from_env())


class ConsoleVideoOverrideTests(unittest.TestCase):
    """面板「模型矩阵」保存的 video 覆盖必须跨进程生效，且允许只改模型 ID。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="video_console_")
        self.env = patch.dict(os.environ, {"DATA_DIR": self.dir})
        self.env.start()
        lr._CONSOLE_VIDEO.update(sig=None, cfg=None)

    def tearDown(self):
        lr._CONSOLE_VIDEO.update(sig=None, cfg=None)
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, payload):
        with open(os.path.join(self.dir, "models_config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def test_panel_video_model_is_used_and_borrows_vision_channel(self):
        self._write({"video": {"model": "qwen3.8-omni-flash", "base_url": "",
                               "max_tokens": 3000, "timeout": 240}})
        vision = _cfg(FLASH_MODEL)
        with patch.object(lr, "LLM_VIDEO", None, create=True), \
                patch.object(lr, "LLM_VISION", vision):
            got = lr.get_video_model()
        self.assertEqual(got.model, VIDEO_MODEL)
        self.assertEqual(got.base_url, vision.base_url)
        self.assertEqual(got.api_key, vision.api_key)
        self.assertEqual(got.max_tokens, 3000)
        self.assertEqual(got.timeout, 240)

    def test_saved_channel_is_not_overridden_by_env(self):
        self._write({"video": {"model": "qwen3.8-omni-flash",
                               "base_url": "https://panel.example.com/v1",
                               "api_key": GOOD_KEY}})
        with patch.object(lr, "LLM_VIDEO", _cfg(VIDEO_MODEL, base="https://env.example.com"),
                          create=True):
            got = lr.get_video_model()
        self.assertEqual(got.base_url, "https://panel.example.com/v1")

    def test_file_change_reloads_without_restart(self):
        self._write({"video": {"model": "model-a", "base_url": "https://a.example.com",
                               "api_key": GOOD_KEY}})
        with patch.object(lr, "LLM_VIDEO", None, create=True):
            self.assertEqual(lr.get_video_model().model, "model-a")
            self._write({"video": {"model": "model-longer-b",
                                   "base_url": "https://a.example.com",
                                   "api_key": GOOD_KEY}})
            self.assertEqual(lr.get_video_model().model, "model-longer-b")


class _MockClientCase(unittest.IsolatedAsyncioTestCase):
    """走真实 _do_call → httpx.MockTransport，钉住请求体本身。"""

    async def asyncSetUp(self):
        self.requests = []
        self.bodies = []
        self.responses = []
        self.status = 200
        self.raw_body = None
        self._snapshot_overrides()

        def handler(request):
            self.requests.append(request)
            self.bodies.append(request.read())
            if self.raw_body is not None:
                return httpx.Response(self.status, text=self.raw_body)
            data = self.responses.pop(0) if self.responses else {
                "choices": [{"message": {"content": "看到了"}}]
            }
            return httpx.Response(self.status, json=data)

        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self._client.aclose)

        for patcher in (
            patch.object(lr, "_CLIENT", self._client),
            patch.object(lr, "LLM_VIDEO", None, create=True),
            patch.object(lr, "LLM_VISION", _cfg(FLASH_MODEL)),
            patch.object(lr, "LLM_TEXT", _cfg(FLASH_MODEL)),
            patch.object(lr, "LLM_FALLBACKS", []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _snapshot_overrides(self):
        self._ov = dict(lr._runtime_overrides)
        lr._runtime_overrides.clear()

        def restore():
            lr._runtime_overrides.clear()
            lr._runtime_overrides.update(self._ov)

        self.addCleanup(restore)

    def _last_body(self):
        return json.loads(self.bodies[-1])


class VideoPayloadTests(_MockClientCase):
    async def test_video_payload_keeps_multimodal_content_verbatim(self):
        video_url = "https://cdn.example.com/q/clip%2F2026.mp4?sign=a%2Fb%2Bc&t=1"
        user_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "这几段视频里她做了什么"},
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "video", "video": [
                    "https://cdn.example.com/q/f1.jpg",
                    "https://cdn.example.com/q/f2.jpg",
                ]},
                {"type": "video_url", "video_url": {
                    "url": "https://cdn.example.com/q/short.mp4", "fps": 2}},
            ],
        }
        original = json.loads(json.dumps(user_msg, ensure_ascii=False))
        await lr.call_video([
            {"role": "system", "content": "你是视频观察助手"},
            user_msg,
        ])

        body = self._last_body()
        self.assertEqual(body["model"], VIDEO_MODEL)
        self.assertEqual(body["messages"][-1], original,
                         "多模态 content（video_url/帧数组/text）必须逐字段原样保留")
        # URL 不能被改写或重新编码
        self.assertIn(video_url.encode("utf-8"), self.bodies[-1])
        self.assertEqual(body["max_tokens"], 2000)
        req = self.requests[-1]
        self.assertEqual(req.url.path, "/compatible-mode/v1/chat/completions")
        self.assertTrue(str(req.url).endswith("/chat/completions"))

    async def test_no_audio_output_fields_are_sent(self):
        await lr.call_video([{"role": "user", "content": [
            {"type": "text", "text": "看视频"},
            {"type": "video_url", "video_url": {"url": "https://cdn.example.com/v.mp4"}},
        ]}])
        keys = set(_all_keys(self._last_body()))
        for banned in ("audio", "audio_format", "modalities", "voice", "output_audio"):
            self.assertNotIn(banned, keys)

    async def test_image_message_is_not_converted(self):
        img_msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": "https://cdn.example.com/pic.jpg", "detail": "high"}},
            {"type": "text", "text": "看这张图"},
        ]}
        await lr.call_video([img_msg])
        body = self._last_body()
        self.assertEqual(body["messages"][0], img_msg)
        self.assertNotIn("video_url", json.dumps(body["messages"][0]))

    async def test_config_effort_flows_into_payload(self):
        with patch.object(lr, "LLM_VIDEO", _cfg(VIDEO_MODEL, effort="high"), create=True):
            await lr.call_video([{"role": "user", "content": "看视频"}])
        self.assertEqual(self._last_body().get("reasoning_effort"), "high")

    async def test_explicit_max_tokens_is_respected(self):
        await lr.call_video([{"role": "user", "content": "看视频"}], max_tokens=512)
        self.assertEqual(self._last_body()["max_tokens"], 512)

    async def test_video_does_not_fall_back_to_a_text_model(self):
        # 视频请求失败时不许拿文本/备用模型再试一遍（它们看不了视频）
        with patch.object(lr, "LLM_FALLBACKS", [
                _cfg(FLASH_MODEL, base="https://fallback.example.com")]):
            self.status = 400
            with self.assertRaises(RuntimeError):
                await lr.call_video([{"role": "user", "content": "看视频"}])
        self.assertEqual(len(self.requests), 1)


class VideoOverrideAndErrorTests(_MockClientCase):
    async def test_runtime_override_role_video(self):
        lr.set_model_override("video", "https://rt.example.com", GOOD_KEY,
                              "qwen3.8-omni-flash-rt", reasoning_effort="low")
        self.addCleanup(lr.clear_model_override, "video")
        await lr.call_video([{"role": "user", "content": "看视频"}])
        body = self._last_body()
        self.assertEqual(body["model"], "qwen3.8-omni-flash-rt")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(self.requests[-1].url.host, "rt.example.com")

    async def test_missing_choices_raises_clearly(self):
        self.responses = [{"choices": []}]
        with self.assertRaises(RuntimeError) as ctx:
            await lr.call_video([{"role": "user", "content": "看视频"}])
        self.assertIn("choices", str(ctx.exception))

    async def test_missing_message_raises_clearly(self):
        self.responses = [{"choices": [{"index": 0}]}]
        with self.assertRaises(RuntimeError) as ctx:
            await lr.call_video([{"role": "user", "content": "看视频"}])
        self.assertIn("message", str(ctx.exception))

    async def test_non_json_body_raises_clearly(self):
        self.raw_body = "<html>not json</html>"
        with self.assertRaises(RuntimeError) as ctx:
            await lr.call_video([{"role": "user", "content": "看视频"}])
        self.assertIn("JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
