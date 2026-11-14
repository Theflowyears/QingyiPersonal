"""
多模型路由器 - 支持 DeepSeek / GLM / Qwen 等任意 OpenAI-compatible API
替代原有的 OpenClaw Gateway 依赖，实现直连 LLM
"""

import os
import json
import time
import asyncio
import httpx
from typing import Optional


# ============================================================
# 模型配置 - 从环境变量读取
# ============================================================

# 思考强度合法值；空字符串/None = Default（不发送该字段，由模型服务端默认决定）
VALID_EFFORTS = {"low", "medium", "high", "none", "minimal"}


def normalize_effort(value: str) -> str:
    """归一化思考强度：非法或空值一律回退为 Default（空串）。"""
    v = (value or "").strip().lower()
    return v if v in VALID_EFFORTS else ""


class ModelConfig:
    """单个模型的配置"""
    def __init__(self, name: str, base_url: str, api_key: str, model: str, 
                 max_tokens: int = 2048, timeout: float = 120,
                 reasoning_effort: str = ""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        # "" = Default：请求体中不包含 reasoning_effort / enable_thinking 字段
        self.reasoning_effort = normalize_effort(reasoning_effort)
    
    @property
    def url(self) -> str:
        # 兼容各种 API 格式
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/v1/chat/completions"
    
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


# 运行时覆盖（Web 控制台调用 set_model_override 写入，role -> ModelConfig）
_runtime_overrides: dict[str, ModelConfig] = {}


def _get_model_from_env(prefix: str) -> Optional[ModelConfig]:
    """从环境变量加载模型配置，前缀如 LLM_TEXT_, LLM_VISION_"""
    base_url = os.getenv(f"{prefix}BASE_URL", "").strip()
    api_key = os.getenv(f"{prefix}API_KEY", "").strip()
    model = os.getenv(f"{prefix}MODEL", "").strip()
    max_tokens = int(os.getenv(f"{prefix}MAX_TOKENS", "2048"))
    timeout = float(os.getenv(f"{prefix}TIMEOUT", "120"))
    # 思考强度：留空 = Default（不发送该字段）
    reasoning_effort = os.getenv(f"{prefix}REASONING_EFFORT", "").strip()
    
    if not (base_url and api_key and model):
        return None
    
    name = os.getenv(f"{prefix}NAME", prefix.rstrip("_")).strip()
    return ModelConfig(name, base_url, api_key, model, max_tokens, timeout,
                       reasoning_effort=reasoning_effort)


# 文本模型（主对话）
LLM_TEXT = _get_model_from_env("LLM_TEXT_")

# 视觉模型（图片理解）- 可选，不配置则用文本模型
LLM_VISION = _get_model_from_env("LLM_VISION_")

# 视频理解模型：qwen3.8-omni-flash 官方口径是全模态、支持视频输入、
# 兼容 OpenAI Chat Completions。视频单独走它，不跟视觉/文本混用。
DEFAULT_VIDEO_MODEL = "qwen3.8-omni-flash"


def _get_video_from_env() -> Optional[ModelConfig]:
    """读取 LLM_VIDEO_*；只配了 BASE_URL/API_KEY 时模型默认就是 omni。"""
    cfg = _get_model_from_env("LLM_VIDEO_")
    if cfg:
        return cfg
    base_url = os.getenv("LLM_VIDEO_BASE_URL", "").strip()
    api_key = os.getenv("LLM_VIDEO_API_KEY", "").strip()
    if not (base_url and api_key):
        return None
    return ModelConfig(
        os.getenv("LLM_VIDEO_NAME", "video").strip() or "video",
        base_url, api_key, DEFAULT_VIDEO_MODEL,
        int(os.getenv("LLM_VIDEO_MAX_TOKENS", "2048")),
        float(os.getenv("LLM_VIDEO_TIMEOUT", "120")),
        reasoning_effort=os.getenv("LLM_VIDEO_REASONING_EFFORT", ""),
    )


LLM_VIDEO = _get_video_from_env()

# 摘要/记忆压缩模型 - 可选，不配置则用文本模型
LLM_SUMMARY = _get_model_from_env("LLM_SUMMARY_")

# 备用模型列表（逗号分隔的前缀名，如 "LLM_FALLBACK1_,LLM_FALLBACK2_"）
FALLBACK_PREFIXES = [
    p.strip() for p in os.getenv("LLM_FALLBACK_PREFIXES", "").split(",") if p.strip()
]

# 常见的“占位符”API Key 特征：出现这些就当作未配置，避免 401 拖垮对话
_PLACEHOLDER_HINTS = ("your-", "your_", "sk-your", "xxx", "changeme", "placeholder", "todo")


def _key_looks_placeholder(key: str) -> bool:
    k = (key or "").strip().lower()
    if len(k) < 20:
        return True
    return any(h in k for h in _PLACEHOLDER_HINTS)

LLM_FALLBACKS = [_get_model_from_env(p) for p in FALLBACK_PREFIXES]
LLM_FALLBACKS = [m for m in LLM_FALLBACKS if m and m.is_configured() and not _key_looks_placeholder(m.api_key)]


def get_text_model() -> ModelConfig:
    """获取当前文本模型"""
    if LLM_TEXT and LLM_TEXT.is_configured():
        return LLM_TEXT
    raise RuntimeError(
        "未配置文本模型！请设置 LLM_TEXT_BASE_URL, LLM_TEXT_API_KEY, LLM_TEXT_MODEL"
    )


def get_vision_model() -> ModelConfig:
    """获取视觉模型。

    未配置、或 API Key 仍是占位符时，回退到文本模型（qwen3.8-flash 本身支持多模态），
    避免因一个无效视觉 Key 让带图消息直接失败。
    """
    if LLM_VISION and LLM_VISION.is_configured() and not _key_looks_placeholder(LLM_VISION.api_key):
        return LLM_VISION
    return get_text_model()


_CONSOLE_VIDEO = {"sig": None, "cfg": None}


def _console_video_model() -> Optional[ModelConfig]:
    """面板「模型矩阵」保存的 video 覆盖。

    Web 与 Bot 是两个进程，运行时覆盖传不过来，只能读 data/models_config.json；
    按 mtime+size 缓存，保存后下一次调用就生效。留空的地址/Key 沿用视觉通道，
    因为面板只让人改「模型 ID」时不该把同一条 Qwen 通道也清掉。
    """
    path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _CONSOLE_VIDEO.update(sig=None, cfg=None)
        return None
    if _CONSOLE_VIDEO["sig"] == sig:
        return _CONSOLE_VIDEO["cfg"]
    cfg = None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
        raw = data.get("video") or {}
        model = str(raw.get("model") or "").strip()
        base = str(raw.get("base_url") or "").strip()
        key = str(raw.get("api_key") or "").strip()
        if model:
            if not (base and key):
                try:
                    vision = get_vision_model()
                except RuntimeError:
                    vision = None
                base = base or (vision.base_url if vision else "")
                key = key or (vision.api_key if vision else "")
            if base and key:
                cfg = ModelConfig(
                    "video", base, key, model,
                    int(raw.get("max_tokens") or 2048),
                    float(raw.get("timeout") or 300),
                    raw.get("reasoning_effort") or "")
    except Exception:
        cfg = None
    _CONSOLE_VIDEO.update(sig=sig, cfg=cfg)
    return cfg


def get_video_model() -> ModelConfig:
    """获取视频理解模型：固定走 qwen3.8-omni-flash（全模态）。

    优先级：运行时 video 覆盖 → 面板保存的 video 覆盖 → LLM_VIDEO_* →
    视觉通道的 base_url/api_key（模型仍钉死 omni；视觉通道本身就是 omni 则直接复用）。
    **绝不回退文本模型**：视频不能发给只认文本/图片的模型。
    """
    override = _runtime_overrides.get("video")
    if override is not None and override.is_configured():
        return override
    console = _console_video_model()
    if console is not None:
        return console
    if LLM_VIDEO and LLM_VIDEO.is_configured() and not _key_looks_placeholder(LLM_VIDEO.api_key):
        return LLM_VIDEO

    try:
        vision = get_vision_model()
    except RuntimeError as e:
        raise RuntimeError(
            "未配置视频模型，且没有可复用的视觉/文本 Qwen 通道！"
            "请设置 LLM_VIDEO_BASE_URL / LLM_VIDEO_API_KEY（模型默认 qwen3.8-omni-flash）"
        ) from e

    # 视觉通道已经是全模态：直接用，连 token/超时/思考强度一起继承
    if "omni" in (vision.model or "").lower():
        return vision

    # 复用同一家 Qwen 的地址与 Key，但模型必须是全模态，不能是视觉/文本模型
    return ModelConfig(
        name="video",
        base_url=vision.base_url,
        api_key=vision.api_key,
        model=DEFAULT_VIDEO_MODEL,
        max_tokens=vision.max_tokens,
        timeout=vision.timeout,
        reasoning_effort=vision.reasoning_effort,
    )


def get_summary_model() -> ModelConfig:
    """获取摘要模型，未配置则回退到文本模型"""
    if LLM_SUMMARY and LLM_SUMMARY.is_configured():
        return LLM_SUMMARY
    return get_text_model()


def list_models() -> list[dict]:
    """列出所有已配置的模型（含思考强度，空串表示 Default）"""
    models = []
    if LLM_TEXT and LLM_TEXT.is_configured():
        models.append({"role": "text", "name": LLM_TEXT.name, "model": LLM_TEXT.model, "url": LLM_TEXT.base_url, "reasoning_effort": LLM_TEXT.reasoning_effort})
    if LLM_VISION and LLM_VISION.is_configured():
        models.append({"role": "vision", "name": LLM_VISION.name, "model": LLM_VISION.model, "url": LLM_VISION.base_url, "reasoning_effort": LLM_VISION.reasoning_effort})
    # 视频角色始终报告「实际会用哪个模型」——即使没配 LLM_VIDEO_*，
    # 生效的也是 omni 而不是视觉/文本模型，面板上不能看错。
    try:
        _video = get_video_model()
    except Exception:
        _video = None
    if _video is not None:
        models.append({"role": "video", "name": _video.name, "model": _video.model, "url": _video.base_url, "reasoning_effort": _video.reasoning_effort})
    if LLM_SUMMARY and LLM_SUMMARY.is_configured():
        models.append({"role": "summary", "name": LLM_SUMMARY.name, "model": LLM_SUMMARY.model, "url": LLM_SUMMARY.base_url, "reasoning_effort": LLM_SUMMARY.reasoning_effort})
    for i, fb in enumerate(LLM_FALLBACKS):
        models.append({"role": f"fallback_{i}", "name": fb.name, "model": fb.model, "url": fb.base_url, "reasoning_effort": fb.reasoning_effort})
    return models


# ============================================================
# 核心调用函数
# ============================================================

import re as _re
_THINK_BLOCK = _re.compile(r"(?is)<think[^>]*>.*?</think[^>]*>|(?:<think|(?:</think))", _re.I)
_THINK_TAG = _re.compile(r"<think[^>]*>?|</think[^>]*>?", _re.I)


def _strip_think(text):
    """清掉正文里泄漏的思考标签/思考块，防止污染群聊回复。"""
    if not text:
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _THINK_TAG.sub("", cleaned)
    return cleaned.strip()

def config_default_effort(model_config: Optional[ModelConfig]) -> str:
    """取模型配置自身的思考强度；无配置时返回 Default（空串）。"""
    if model_config is None:
        try:
            return get_text_model().reasoning_effort
        except Exception:
            return ""
    return model_config.reasoning_effort


# ── 共享连接池 ──────────────────────────────────────
# 原来每次调用都 async with httpx.AsyncClient(...)：新建 TCP + TLS 握手，
# 实测多花将近 2 秒（5.0s vs 3.0s）。复用连接后同一把 key 只握一次手。
_CLIENT = None
_CLIENT_LOCK = None


async def _client():
    global _CLIENT, _CLIENT_LOCK
    if _CLIENT is not None:
        return _CLIENT
    if _CLIENT_LOCK is None:
        _CLIENT_LOCK = asyncio.Lock()
    async with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                limits=httpx.Limits(max_connections=16,
                                    max_keepalive_connections=8,
                                    keepalive_expiry=120.0),
                follow_redirects=True,
            )
    return _CLIENT


async def call_llm(
    messages: list[dict],
    model_config: Optional[ModelConfig] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    temperature=0.7,
    use_fallback: bool = True,
    reasoning_effort: Optional[str] = None,
    retries: Optional[int] = None,
    budget_s: Optional[float] = None,
) -> str:
    """
    调用 OpenAI-compatible LLM API
    
    Args:
        messages: OpenAI 格式消息列表
        model_config: 指定模型配置，None 则使用默认文本模型
        max_tokens: 覆盖最大 token 数
        timeout: 覆盖超时秒数
        temperature: 温度参数
        use_fallback: 是否在失败时尝试备用模型
        reasoning_effort: 思考强度覆盖；None = 用模型自身配置（空即 Default）
        budget_s: 总延迟预算（秒）。设了就把预算摊到各家供应商上：
                  每家只试一次、给的超时就是剩余预算，超时即降级到下一家。
                  预算全部用尽时**不丢回复** —— 改用完整超时再等主模型一次，
                  由调用方通过延迟仪表记为超预算。
    
    Returns:
        模型回复文本
    """
    if model_config is None:
        model_config = get_text_model()
    
    configs_to_try = [model_config]
    if use_fallback:
        configs_to_try.extend(LLM_FALLBACKS)

    deadline = (time.time() + float(budget_s)) if (budget_s and budget_s > 0) else None
    last_error = None
    skipped_suppliers = False

    effective_effort = normalize_effort(reasoning_effort) if reasoning_effort is not None else config_default_effort(model_config)

    for config in configs_to_try:
        if not config.is_configured():
            continue

        # 逐模型思考强度：备用模型使用自己的配置，主模型可用显式覆盖
        effort_for_this = (effective_effort if model_config is not None and config is model_config
                           else config.reasoning_effort)

        t_this = timeout
        r_this = retries
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0.15:
                # 预算见底，后面的家也没时间了；留给收尾那一次无预算重试
                skipped_suppliers = True
                break
            # 预算内不做退避重试，一家一次，快降级
            t_this = min(timeout or config.timeout, remaining)
            r_this = 1

        try:
            result = await _do_call(config, messages, max_tokens, t_this, temperature,
                                    reasoning_effort=effort_for_this, retries=r_this)
            return result
        except Exception as e:
            last_error = e
            print(f"[LLM] {config.name} 调用失败: {e}", flush=True)
            continue

    # 预算用尽（或预算内没试完就见底）但还没拿到答案：宁可慢，
    # 也不能把回复变成一句罐头话。这一次用的是各家自己配的完整超时，
    # 不是调用方为了赶预算传进来的那个短超时。
    if deadline is not None and (skipped_suppliers or time.time() >= deadline):
        print("[LLM] 预算 %.1fs 用尽，改用完整超时等主模型（本轮记为超预算）"
              % float(budget_s or 0), flush=True)
        try:
            return await _do_call(model_config, messages, max_tokens,
                                  max(float(timeout or 0),
                                      float(model_config.timeout or 0)),
                                  temperature,
                                  reasoning_effort=effective_effort, retries=retries)
        except Exception as e:
            last_error = e
            print(f"[LLM] 兜底等待也失败: {type(e).__name__}: {str(e)[:120]}", flush=True)
    
    error_msg = f"所有模型均调用失败"
    if last_error:
        error_msg += f": {last_error}"
    raise RuntimeError(error_msg)


async def _do_call(
    config: ModelConfig,
    messages: list[dict],
    max_tokens: Optional[int],
    timeout: Optional[float],
    temperature=None,
    reasoning_effort: str = "",
    retries: Optional[int] = None,
) -> str:
    """执行单次 LLM API 调用

    reasoning_effort 为空串（Default）时，请求体不包含任何思考字段，
    由模型服务端按默认行为处理；非空时才下发 reasoning_effort。
    """
    
    # 合并多个 system 消息为一个（兼容 Qwen/GLM）
    system_parts = []
    non_system = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                content = content.strip()
            elif isinstance(content, list):
                # 多模态格式的 system content
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n\n".join(text_parts).strip()
            else:
                content = json.dumps(content, ensure_ascii=False)
            if content:
                system_parts.append(content)
        else:
            non_system.append(msg)
    
    if system_parts:
        merged_messages = [{"role": "system", "content": "\n\n".join(system_parts)}] + non_system
    else:
        merged_messages = non_system
    
    payload = {
        "model": config.model,
        "messages": merged_messages,
        "stream": False,
        "max_tokens": max_tokens or config.max_tokens,
    }
    # temperature 传 None 就不发这个字段。
    # 有的模型（claude-sonnet-5-5）明确拒绝非默认温度：
    #   400 "does not support non-default temperature"
    if temperature is not None:
        payload["temperature"] = temperature

    # 思考强度：Default（空）时不发送任何思考字段，保持模型默认行为。
    # none/minimal -> enable_thinking=false；low/medium/high -> OpenAI 风格 reasoning_effort。
    if reasoning_effort == "none" or reasoning_effort == "minimal":
        payload["enable_thinking"] = False
    elif reasoning_effort in ("low", "medium", "high"):
        payload["reasoning_effort"] = reasoning_effort
    # else: Default —— 什么都不加
    
    # 使用 ensure_ascii=True 避免编码问题
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {config.api_key}",
    }
    
    effective_timeout = timeout or config.timeout
    
    print(
        f"[LLM] calling {config.name} model={config.model} "
        f"msgs={len(merged_messages)} max_tokens={payload['max_tokens']}",
        flush=True
    )
    
    # 重试策略：只对「临时性」失败重试，配置类错误立刻抛。
    #   5xx / 429 —— 上游过载或限流。中转站常见 503「请求暂时无法处理，请稍后重试」，
    #                等几秒往往就好了；以前不重试，长文请求直接失败。
    #   传输层错误 —— DNS 抖动、连接被重置。
    #   其余 4xx（401/400/404）是 key 或请求本身有问题，重试没有意义。
    _RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
    # retries 可由调用方指定：深度模型抽风时要快速放弃、早点换备胎，
    # 不能在那儿老老实实重试 3 次（一次 90 秒，三分钟就没了）。
    _MAX_TRIES = max(1, int(retries if retries is not None
                            else (os.getenv("LLM_RETRY_TIMES", "3") or 3)))
    _BACKOFF = [1.5, 4.0, 9.0, 15.0]

    resp = None
    last_exc = None
    for _attempt in range(_MAX_TRIES):
        try:
            client = await _client()
            # Bind the actual serialized request, not a truncated UI preview.
            import hashlib
            import self_model
            self_model.record_event(
                'generation_request', source='llm_router',
                payload={'model': config.model, 'role': config.name,
                         'request_sha256': hashlib.sha256(body).hexdigest(),
                         'attempt': _attempt + 1,
                         'layers': {layer: ((self_model.layer_history(layer, 1) or [{}])[0].get('content_hash'))
                                    for layer in ('identity', 'boundary', 'personality', 'work_state')}})
            resp = await client.post(config.url, headers=headers, content=body,
                                     timeout=effective_timeout)
        except (httpx.TransportError, OSError) as e:
            last_exc = e
            resp = None
            print(f"[LLM] {config.name} 连接失败({_attempt+1}/{_MAX_TRIES}): "
                  f"{type(e).__name__}", flush=True)
            if _attempt < _MAX_TRIES - 1:
                await asyncio.sleep(_BACKOFF[min(_attempt, len(_BACKOFF) - 1)])
            continue

        if resp.status_code in _RETRY_STATUS and _attempt < _MAX_TRIES - 1:
            _t = (resp.text or "")[:120].replace("\n", " ")
            print(f"[LLM] {config.name} HTTP {resp.status_code}"
                  f"({_attempt+1}/{_MAX_TRIES})，{_BACKOFF[min(_attempt, len(_BACKOFF)-1)]}s 后重试：{_t}",
                  flush=True)
            await asyncio.sleep(_BACKOFF[min(_attempt, len(_BACKOFF) - 1)])
            continue
        break

    if resp is None:
        raise RuntimeError(f"{config.name} 连接失败: {type(last_exc).__name__}")

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(
            f"LLM 返回非 JSON（{config.name}/{config.model}）: "
            f"{type(e).__name__}: {(resp.text or '')[:200]}"
        ) from e

    # 结构错误必须明确抛错：静默返回「未返回有效内容」会把上游故障伪装成空回答，
    # 视频识别这类任务根本分不清是模型没说话还是网关返回了别的东西。
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError(
            f"LLM 返回结构异常（{config.name}/{config.model}）：缺少 choices: "
            f"{json.dumps(data, ensure_ascii=True)[:200]}"
        )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError(
            f"LLM 返回结构异常（{config.name}/{config.model}）：choices[0] 缺少 message: "
            f"{json.dumps(choices[0], ensure_ascii=True)[:200]}"
        )

    content = message.get("content")
    if content is None:
        content = ""
    if isinstance(content, list):
        # 全模态模型可能把输出拆成分段，只取文本段，别把结构化片段当字符串
        content = "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        )
    if not isinstance(content, str):
        raise RuntimeError(
            f"LLM 返回结构异常（{config.name}/{config.model}）："
            f"content 不是文本（{type(content).__name__}）"
        )

    answer = _strip_think(content.strip())

    # 记录 token 用量
    usage = data.get("usage", {})
    if usage:
        _pt = usage.get("prompt_tokens", 0) or 0
        _det = usage.get("prompt_tokens_details") or {}
        _cd = _det.get("cached_tokens", 0) or 0
        # 缓存创建：不同中转站字段名不一样，全试一遍。
        # 这笔钱比未命中还贵（1.25 vs 0.80），漏了就会低估成本。
        _cw = (_det.get("cache_creation_tokens")
               or _det.get("cache_creation_input_tokens")
               or usage.get("cache_creation_input_tokens")
               or 0) or 0
        _pct = (float(_cd) / float(_pt) * 100) if _pt else 0
        print(
            f"[LLM] {config.name} tokens: prompt={_pt} "
            f"completion={usage.get('completion_tokens', '?')} "
            f"cached={_cd}({_pct:.0f}%)",
            flush=True
        )
        # 累计统计，供面板看缓存效果
        try:
            _STATE["calls"] = int(_STATE.get("calls") or 0) + 1
            _STATE["prompt"] = int(_STATE.get("prompt") or 0) + int(_pt)
            _STATE["cached"] = int(_STATE.get("cached") or 0) + int(_cd)
            _STATE["completion"] = int(_STATE.get("completion") or 0) + int(usage.get("completion_tokens", 0) or 0)
        except Exception:
            pass

        # 按天+按模型的明细（面板「今日用量」读这个，重启不清零）
        try:
            import token_stats as _ts
            _ts.record(config.model, prompt=_pt, cached=_cd,
                       completion=usage.get("completion_tokens", 0) or 0,
                       role=config.name, write=_cw)
        except Exception as _tse:
            print("[用量] 记录失败 " + type(_tse).__name__, flush=True)

    return answer or "（模型未返回有效内容）"


# ============================================================
# 便捷包装函数 - 替代原 call_openclaw / call_mimo_vision
# ============================================================

# 运行时统计：面板上要能看见 token 花在哪、缓存省了多少
_STATE = {"calls": 0, "prompt": 0, "cached": 0, "completion": 0, "since": 0.0}


def usage_stats():
    """给面板：累计调用次数、token 总量、缓存命中量。"""
    import time as _t
    n = int(_STATE.get("calls") or 0)
    pt = int(_STATE.get("prompt") or 0)
    cd = int(_STATE.get("cached") or 0)
    cp = int(_STATE.get("completion") or 0)
    return {
        "calls": n,
        "prompt": pt,
        "cached": cd,
        "completion": cp,
        "cached_pct": round(float(cd) / float(pt) * 100, 1) if pt else 0.0,
        "avg_prompt": round(float(pt) / float(n), 0) if n else 0,
        "since": _STATE.get("since") or 0,
    }


def reset_usage():
    import time as _t
    _STATE.update({"calls": 0, "prompt": 0, "cached": 0, "completion": 0, "since": _t.time()})


# ── 自适应主/备切换 ────────────────────────────────
# 主模型连续 N 条超过阈值，就临时切到备用模型；
# 冷却结束后再试主模型，还慢就继续留在备用。
_ADAPT = {
    "slow_streak": 0,        # 主模型连续慢了几条
    "using_fallback": False,
    "until": 0.0,            # 备用模式到期时间
    "last_latencies": [],    # 最近几次耗时，给面板看
    "switches": 0,           # 切过几次
}
ADAPT_SLOW_SECONDS = float(os.getenv("ADAPT_SLOW_SECONDS", "2"))
ADAPT_SLOW_STREAK = int(os.getenv("ADAPT_SLOW_STREAK", "4"))
ADAPT_COOLDOWN = float(os.getenv("ADAPT_COOLDOWN_SECONDS", "600"))


def get_fallback_model():
    """备用文本模型（LLM_TEXT_FALLBACK_）。没配返回 None。"""
    try:
        cfg = _get_model_from_env("LLM_TEXT_FALLBACK_")
        if cfg and cfg.is_configured():
            return cfg
    except Exception:
        pass
    return None


def _pick_text_config():
    """现在该用哪个：主 or 备。"""
    import time as _t
    fb = get_fallback_model()
    if fb is None:
        return get_text_model()
    now = _t.time()
    if _ADAPT["using_fallback"]:
        if now >= _ADAPT["until"]:
            # 冷却结束，试着回主模型
            _ADAPT["using_fallback"] = False
            _ADAPT["slow_streak"] = 0
            print("[模型] 冷却结束，试回主模型", flush=True)
            return get_text_model()
        return fb
    return get_text_model()


def record_latency(seconds):
    """每次调用完记一笔，决定要不要切。"""
    import time as _t
    s = float(seconds or 0)
    _ADAPT["last_latencies"].append(round(s, 2))
    _ADAPT["last_latencies"] = _ADAPT["last_latencies"][-20:]
    if _ADAPT["using_fallback"]:
        return
    if s > ADAPT_SLOW_SECONDS:
        _ADAPT["slow_streak"] += 1
    else:
        _ADAPT["slow_streak"] = 0
    if _ADAPT["slow_streak"] >= ADAPT_SLOW_STREAK and get_fallback_model() is not None:
        _ADAPT["using_fallback"] = True
        _ADAPT["until"] = _t.time() + ADAPT_COOLDOWN
        _ADAPT["switches"] += 1
        _ADAPT["slow_streak"] = 0
        print("[模型] 主模型连续 %d 条超过 %.0fs，切备用 %.0f 分钟"
              % (ADAPT_SLOW_STREAK, ADAPT_SLOW_SECONDS, ADAPT_COOLDOWN / 60), flush=True)


def adapt_state():
    """给面板看的状态。"""
    import time as _t
    p_, f_ = get_text_model(), get_fallback_model()
    left = max(0, int(_ADAPT["until"] - _t.time()))
    return {
        "primary": getattr(p_, "model", ""),
        "fallback": getattr(f_, "model", "") if f_ else "",
        "using": "fallback" if (_ADAPT["using_fallback"] and left > 0) else "primary",
        "slow_streak": _ADAPT["slow_streak"],
        "threshold": ADAPT_SLOW_SECONDS,
        "streak_needed": ADAPT_SLOW_STREAK,
        "cooldown_left": left,
        "switches": _ADAPT["switches"],
        "last": _ADAPT["last_latencies"][-10:],
    }


_SOL_CACHE = {}


def get_sol_model():
    """备胎模型（LLM_SOL_*，claude-sonnet-5）。

    深度模型（中转站）抽风时先换它，比直接掉到水群模型保底得多。
    不配就返回 None，调用方自己决定下一步。
    """
    if "cfg" in _SOL_CACHE:
        return _SOL_CACHE["cfg"]
    try:
        cfg = _get_model_from_env("LLM_SOL_")
    except Exception:
        cfg = None
    if cfg and not cfg.is_configured():
        cfg = None
    if cfg and _key_looks_placeholder(cfg.api_key):
        cfg = None
    _SOL_CACHE["cfg"] = cfg
    return cfg


def get_deep_model() -> ModelConfig:
    """深度模型（LLM_DEEP_*）。不配就退回普通文本模型。

    用来把「认真答/写长文」和「随手水群」分开：
    水群要快（qwen 1.5-2.5s），认真答可以等（gpt-6-luna 4-12s 但质量更好）。
    """
    try:
        cfg = _get_model_from_env("LLM_DEEP_")
        if cfg and cfg.is_configured():
            return cfg
    except Exception:
        pass
    return get_text_model()


async def call_text(messages: list[dict], max_tokens: int = 800, timeout: float = 120,
                    reasoning_effort=None, deep: bool = False,
                    budget_s: Optional[float] = None) -> str:
    """调用文本模型（替代 call_openclaw）。

    reasoning_effort="none" 关闭深度思考（水群用：秒回、不被推理吃预算、人设更稳）。
    deep=True 走 LLM_DEEP_* 那套（质量更高、更慢）。
    budget_s 只对水群快车道有意义：长文本来就该慢，不给它设延迟预算。
    """
    import time as _t
    cfg = get_deep_model() if deep else _pick_text_config()
    t0 = _t.time()
    _b = None if deep else budget_s
    try:
        try:
            # deep 路径**关掉 call_llm 自带的兜底**，否则它一失败就直接跳 DeepSeek，
            # 我们精心排的「深度模型 → Sonnet → 文本模型」这一层永远轮不到。
            out = await call_llm(messages, cfg, max_tokens=max_tokens, timeout=timeout,
                                 reasoning_effort=reasoning_effort,
                                 use_fallback=not deep,
                                 retries=1 if deep else None,
                                 budget_s=_b)
        except Exception as e:
            if not deep:
                raise
            # 长文三级降级：深度模型 → Sonnet → 直连文本模型。
            # 中转站的深度模型整段不可用时（503 要等 90 秒才回），
            # 直接掉到水群模型写长文质量太差，中间加一层 Sonnet 兜着。
            _sol = get_sol_model()
            if _sol is not None:
                print(f"[LLM] 深度模型失败({type(e).__name__})，改用 {_sol.model}", flush=True)
                try:
                    return await call_llm(messages, _sol, max_tokens=max_tokens,
                                          timeout=max(timeout, _sol.timeout),
                                          reasoning_effort=reasoning_effort,
                                          temperature=None,   # Sonnet 不接受非默认温度
                                          use_fallback=False)
                except Exception as e2:
                    print(f"[LLM] {_sol.model} 也失败({type(e2).__name__})，退回文本模型", flush=True)
            else:
                print(f"[LLM] 深度模型失败({type(e).__name__})，退回文本模型", flush=True)
            out = await call_llm(messages, _pick_text_config(), max_tokens=max_tokens,
                                 timeout=timeout, reasoning_effort="")
    finally:
        # 只统计水群（deep 本来就要慢，不能拿它当切换依据）
        if not deep:
            try:
                record_latency(_t.time() - t0)
            except Exception:
                pass
    return out


async def call_vision(messages: list[dict], max_tokens: int = 800, timeout: float = 120) -> str:
    """调用视觉模型（替代 call_mimo_vision）"""
    return await call_llm(messages, get_vision_model(), max_tokens=max_tokens, timeout=timeout)


async def call_video(messages: list[dict], max_tokens: int = 2000, timeout: float = 300,
                     reasoning_effort=None) -> str:
    """调用视频理解模型（qwen3.8-omni-flash，全模态）。

    messages 里的 video_url / 视频帧数组 / text 原样透传：不改写 URL、不丢字段、
    不把图片消息「转换」成视频；也不请求 audio 输出。
    关掉 call_llm 的备用模型：文本/备用模型看不了视频，失败必须直接报错。
    """
    return await call_llm(
        messages, get_video_model(),
        max_tokens=max_tokens,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
        use_fallback=False,
    )


async def call_summary(messages: list[dict], max_tokens: int = 1000, timeout: float = 300) -> str:
    """调用摘要模型"""
    return await call_llm(messages, get_summary_model(), max_tokens=max_tokens, timeout=timeout)


# ============================================================
# 运行时热更新支持
# ============================================================

# 覆盖表 _runtime_overrides 在文件顶部声明（get_video_model 也要读它）


def set_model_override(role: str, base_url: str, api_key: str, model: str,
                       reasoning_effort: str = "", max_tokens: int = 2048,
                       timeout: float = 120):
    """运行时覆盖模型配置（Web 控制台调用）。

    role 支持 text / vision / summary / video 等；video 覆盖会被
    get_video_model() 优先采用（此时模型不再强制 omni，由运维显式指定）。
    """
    config = ModelConfig(
        name=f"override_{role}",
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )
    if not config.is_configured():
        raise ValueError("模型配置不完整")
    _runtime_overrides[role] = config
    effort_label = config.reasoning_effort or "Default"
    print(f"[LLM] 运行时覆盖 {role} -> {model} @ {base_url} (思考强度={effort_label})", flush=True)


def clear_model_override(role: str):
    """清除运行时覆盖"""
    _runtime_overrides.pop(role, None)


def get_effective_model(role: str = "text") -> ModelConfig:
    """获取生效的模型配置（优先运行时覆盖）"""
    if role in _runtime_overrides:
        return _runtime_overrides[role]
    if role == "text":
        return get_text_model()
    elif role == "vision":
        return get_vision_model()
    elif role == "video":
        return get_video_model()
    elif role == "summary":
        return get_summary_model()
    return get_text_model()
