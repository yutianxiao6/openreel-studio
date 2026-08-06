"""LLM service — unified Responses API gateway via LiteLLM.

配置真相源是 config/runtime.jsonc（ConfigStore 同步到 DB）。本服务每次请求：
  1. 查 model_configs.task_type → llm_provider_name
  2. 查 llm_providers 拿 base_url / api_key / provider / model_name
  3. 透传 api_base / api_key 给 litellm.aresponses（不依赖环境变量）

OpenReel 始终使用 Responses input/output items。暂不支持原生 Responses 的
provider 由 LiteLLM 在这一层桥接到 Chat Completions，上层 Agent 不维护第二套合同。

不再 _push_keys_to_env：这样改 runtime.jsonc 立即生效，无需重启。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

import litellm
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.token_usage import build_usage_snapshot, extract_usage_from_response
from app.config import settings
from app.db.session import session_scope
from app.llm_limits import DEFAULT_LLM_MAX_OUTPUT_TOKENS
from app.services.llm_responses import (
    ResponseStreamUpdate,
    prepare_response_input,
    replay_output_items,
    response_output_items,
    response_stream_update,
    response_view,
    responses_tools,
    stream_text_delta,
)


_TASK_DEFAULTS = {
    "agent_loop": "DEFAULT_FAST_MODEL",
    "agent_review": "DEFAULT_REVIEW_MODEL",
    "agent_compact": "DEFAULT_FAST_MODEL",
    "agent_aux": "DEFAULT_FAST_MODEL",
    "script_review": "DEFAULT_REVIEW_MODEL",
    "subagent_node_producer": "DEFAULT_TEXT_MODEL",
    "subagent_image_editor": "DEFAULT_FAST_MODEL",
    "subagent_workflow_spec": "DEFAULT_TEXT_MODEL",
}
_TASK_CONFIG_FALLBACKS = {
    "agent_review": "agent_loop",
    "agent_compact": "agent_loop",
    "subagent_node_producer": "agent_loop",
    "subagent_image_editor": "agent_loop",
    "subagent_workflow_spec": "agent_loop",
}

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}
_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "max context",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "too many tokens",
    "token limit",
)
_MAX_OUTPUT_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}
_RETRYABLE_STREAM_ERROR_MARKERS = (
    "service_unavailable",
    "upstream_unavailable",
    "server_overloaded",
    "rate_limit",
    "timeout",
    "temporarily unavailable",
    "connection",
)

logger = logging.getLogger(__name__)


class LLMConfigurationError(RuntimeError):
    """Raised when a hosted LLM task has no configured provider or API key."""


class LLMOutputTruncatedError(RuntimeError):
    """Raised when a complete text result cannot be produced within bounded continuations."""

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        continuation_count: int = 0,
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.continuation_count = continuation_count


class LLMResponseStatusError(RuntimeError):
    """Raised when a non-background Responses request does not reach a usable state."""


class LLMResponsesWebSocketError(RuntimeError):
    """Raised when the turn-scoped Responses WebSocket cannot continue safely."""

    def __init__(self, message: str, *, code: str = ""):
        super().__init__(message)
        self.code = code


def _policy_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _policy_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _policy_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _provider_params(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        return {}
    direct = cfg.get("provider_params")
    if isinstance(direct, dict):
        return direct
    metadata = cfg.get("model_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("params"), dict):
        return metadata["params"]
    return {}


def _llm_request_timeout_seconds(params: dict[str, Any] | None = None) -> float:
    params = params or {}
    try:
        env_default = float(os.getenv("DRAMA_LLM_REQUEST_TIMEOUT_SECONDS", "90") or "90")
    except (TypeError, ValueError):
        env_default = 90.0
    return _policy_float(
        params.get("request_timeout_seconds"),
        env_default,
        minimum=10.0,
        maximum=3600.0,
    )


def _llm_request_policy(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    params = _provider_params(cfg)
    model = str((cfg or {}).get("model") or "").strip().lower()
    api_base = str((cfg or {}).get("api_base") or "").strip()
    api_host = (urlparse(api_base).hostname or "").lower() if api_base else ""
    # Existing custom OpenAI-compatible relays predate Responses support and
    # are therefore bridged by default. A relay with a native /responses
    # endpoint opts in explicitly with use_chat_completions_api=false.
    default_chat_bridge = bool(
        model.startswith("openai/")
        and api_host
        and api_host not in {"api.openai.com"}
    )
    default_responses_websocket = bool(
        (model.startswith("openai/") or model.startswith("gpt-"))
        and (not api_host or api_host == "api.openai.com")
        and not default_chat_bridge
    )
    return {
        # max_retries means retries after the first OpenReel attempt.
        "max_retries": _policy_int(
            params.get("max_retries", os.getenv("DRAMA_LLM_MAX_RETRIES", "2")),
            2,
            minimum=0,
            maximum=10,
        ),
        # Disable the SDK's hidden retry layer by default. Keeping both layers
        # enabled multiplies one logical request into many relay requests.
        "sdk_max_retries": _policy_int(
            params.get("sdk_max_retries", os.getenv("DRAMA_LLM_SDK_MAX_RETRIES", "0")),
            0,
            minimum=0,
            maximum=10,
        ),
        "request_timeout_seconds": _llm_request_timeout_seconds(params),
        "retry_backoff_seconds": _policy_float(
            params.get("retry_backoff_seconds", os.getenv("DRAMA_LLM_RETRY_BACKOFF_SECONDS", "0.5")),
            0.5,
            minimum=0.0,
            maximum=60.0,
        ),
        "max_continuations": _policy_int(
            params.get("max_continuations", os.getenv("DRAMA_LLM_MAX_CONTINUATIONS", "1")),
            1,
            minimum=0,
            maximum=5,
        ),
        "accept_backend_content": _policy_bool(
            params.get("accept_backend_content", os.getenv("DRAMA_LLM_ACCEPT_BACKEND_CONTENT", "true")),
            True,
        ),
        # Stateless Responses calls replay typed output items locally. Including
        # encrypted reasoning keeps that replay valid for reasoning models.
        "include_reasoning_encrypted_content": _policy_bool(
            params.get("include_reasoning_encrypted_content", True),
            True,
        ),
        # Explicit compatibility bridge for OpenAI-compatible relays that only
        # expose /chat/completions. OpenReel still receives Responses objects.
        "use_chat_completions_api": _policy_bool(
            params.get("use_chat_completions_api", default_chat_bridge),
            default_chat_bridge,
        ),
        # Codex treats WebSocket support as a provider capability. Official
        # OpenAI endpoints have that capability by default; custom native
        # Responses relays must opt in explicitly.
        "supports_responses_websocket": _policy_bool(
            params.get("supports_responses_websocket", default_responses_websocket),
            default_responses_websocket,
        ),
    }


def _default_model_for(task_type: str) -> str:
    attr = _TASK_DEFAULTS.get(task_type, "DEFAULT_TEXT_MODEL")
    return getattr(settings, attr)


async def _lookup_llm_provider(provider_name: str):
    """从 llm_providers 表按名称读取一行。"""
    from app.db.models import LlmProvider
    async with session_scope() as session:
        r = await session.exec(
            select(LlmProvider).where(LlmProvider.name == provider_name)
        )
        row = r.first()
        return row if row and row.enabled else None


async def _lookup_llm_provider_by_override(value: str):
    """Resolve a node-level model override to a configured LLM provider when possible."""
    text = str(value or "").strip()
    if not text:
        return None
    by_name = await _lookup_llm_provider(text)
    if by_name is not None:
        return by_name

    from app.db.models import LlmProvider
    async with session_scope() as session:
        r = await session.exec(select(LlmProvider).where(LlmProvider.enabled == True))  # noqa: E712
        rows = list(r.all())
    normalized = text.split("/", 1)[1] if "/" in text else text
    for row in rows:
        provider = str(getattr(row, "provider", "") or "")
        model_name = str(getattr(row, "model_name", "") or "")
        candidates = {
            model_name,
            f"{provider}/{model_name}" if provider and model_name and "/" not in model_name else model_name,
        }
        if text in candidates or normalized == model_name:
            return row
    return None


def _llm_provider_metadata(provider_row: Any | None) -> dict[str, Any]:
    if provider_row is None:
        return {}
    params: dict[str, Any] = {}
    raw_params = getattr(provider_row, "params_json", None)
    if raw_params:
        try:
            parsed = json.loads(raw_params)
            if isinstance(parsed, dict):
                params = parsed
        except Exception:
            params = {}
    return {
        "provider_name": getattr(provider_row, "name", None),
        "context_window_tokens": getattr(provider_row, "context_window_tokens", None),
        "max_input_tokens": getattr(provider_row, "max_input_tokens", None),
        "max_output_tokens": getattr(provider_row, "max_output_tokens", None),
        "supports_prompt_cache": getattr(provider_row, "supports_prompt_cache", None),
        "supports_vision": getattr(provider_row, "supports_vision", None),
        "tokenizer": getattr(provider_row, "tokenizer", None),
        "tier": getattr(provider_row, "tier", None),
        "params": params,
    }


def _attach_model_metadata(response: Any, metadata: dict[str, Any]) -> Any:
    if not metadata:
        return response
    try:
        setattr(response, "_openreel_model_metadata", metadata)
    except Exception:
        if isinstance(response, dict):
            response["_openreel_model_metadata"] = metadata
    return response


async def _resolve_config(
    task_type: str,
    db: AsyncSession | None,
    node_override: str | None,
) -> dict[str, Any]:
    """优先级：node_override > task mapping > agent mapping > authenticated env default。"""
    if node_override:
        provider_row = await _lookup_llm_provider_by_override(node_override)
        if provider_row is not None:
            return _config_from_provider_row(provider_row)
        return {
            "model": node_override,
            "temperature": 0.7,
            "max_tokens": DEFAULT_LLM_MAX_OUTPUT_TOKENS,
            "api_base": None,
            "api_key": None,
            "model_metadata": {},
        }

    cfg_row = None
    provider_row = None
    if db is not None:
        from app.db.models import ModelConfig

        candidate_tasks = [task_type]
        if task_type != "agent_loop":
            fallback_task = _TASK_CONFIG_FALLBACKS.get(task_type)
            if fallback_task:
                candidate_tasks.append(fallback_task)
            candidate_tasks.append("agent_loop")

        for candidate_task in dict.fromkeys(candidate_tasks):
            result = await db.exec(
                select(ModelConfig)
                .where(
                    ModelConfig.task_type == candidate_task,
                    ModelConfig.enabled == True,  # noqa: E712
                )
                .order_by(ModelConfig.created_at.desc())
                .limit(1)
            )
            candidate_row = result.first()
            if candidate_row is None:
                continue
            candidate_provider = await _lookup_llm_provider(candidate_row.llm_provider_name)
            if candidate_provider is None:
                continue
            cfg_row = candidate_row
            provider_row = candidate_provider
            break

    if provider_row is not None:
        return _config_from_provider_row(
            provider_row,
            temperature=cfg_row.temperature if cfg_row else 0.7,
            top_p=cfg_row.top_p if cfg_row else 1.0,
            fallback_model=cfg_row.fallback_model if cfg_row else None,
            max_tokens=cfg_row.max_tokens if cfg_row and cfg_row.max_tokens else None,
        )

    # Source/dev fallback is allowed only when the default hosted model has an
    # actual environment/runtime key. Desktop installs must never call an
    # unconfigured built-in provider merely because it is the source default.
    default_model = _default_model_for(task_type)
    default_key = _resolve_env_key_for_default(default_model)
    if _hosted_default_requires_auth(default_model) and not default_key:
        raise LLMConfigurationError(
            f"No configured LLM provider for task {task_type!r}. "
            "Configure an Agent or model-tier LLM in Settings before running this step."
        )
    return {
        "model": default_model,
        "temperature": 0.7,
        "max_tokens": DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        "api_base": None,
        "api_key": default_key,
        "model_metadata": {},
    }


def _hosted_default_requires_auth(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized.startswith((
        "deepseek/",
        "openai/",
        "gpt-",
        "anthropic/",
        "claude",
        "dashscope/",
        "gemini/",
    ))


def _resolve_env_key_for_default(model: str) -> str | None:
    """settings 默认模型的兜底 key 来源（从 settings 取，仅作为最后兜底）。

    优先级：settings env → runtime.jsonc llm_providers → None
    """
    if model.startswith("deepseek/"):
        key = settings.DEEPSEEK_API_KEY or None
        if key:
            return key
    if model.startswith("openai/") or model.startswith("gpt-"):
        key = settings.OPENAI_API_KEY or None
        if key:
            return key
    if model.startswith("anthropic/") or model.startswith("claude"):
        key = settings.ANTHROPIC_API_KEY or None
        if key:
            return key
    if model.startswith("dashscope/"):
        key = settings.DASHSCOPE_API_KEY or None
        if key:
            return key
    if model.startswith("gemini/"):
        key = settings.GEMINI_API_KEY or None
        if key:
            return key

    # 兜底：从 runtime.jsonc 的 llm_providers 找匹配 provider 的 key
    try:
        import json5
        from pathlib import Path
        _cfg_path = Path(settings.PROJECT_ROOT) / "config" / "runtime.jsonc"
        if _cfg_path.exists():
            _raw = _cfg_path.read_text(encoding="utf-8")
            _cfg = json5.loads(_raw)
            _providers = _cfg.get("llm_providers") or []
            # 推断 provider name: deepseek/deepseek-chat → deepseek
            _provider = model.split("/")[0] if "/" in model else ""
            for _p in _providers:
                if not isinstance(_p, dict):
                    continue
                if not _p.get("enabled", True):
                    continue
                _p_name = _p.get("provider", "")
                if _p_name == _provider:
                    _key = _p.get("api_key")
                    if _key:
                        return _resolve_key_reference(_key)
    except Exception:
        pass

    return None


def _resolve_key_reference(value: str | None) -> str | None:
    if not value or not value.startswith("${") or not value.endswith("}"):
        return value
    return os.getenv(value[2:-1]) or None


def _config_from_provider_row(
    provider_row: Any,
    *,
    temperature: float = 0.7,
    top_p: float = 1.0,
    fallback_model: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    provider = provider_row.provider
    model_name = provider_row.model_name
    model = model_name if "/" in model_name else f"{provider}/{model_name}"
    metadata = _llm_provider_metadata(provider_row)
    return {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens or provider_row.max_output_tokens or DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        "top_p": top_p,
        "fallback_model": fallback_model,
        "api_base": provider_row.base_url,
        "api_key": provider_row.api_key,
        "model_metadata": metadata,
        "provider_params": metadata.get("params") or {},
    }


def _responses_kwargs(cfg: dict, *, with_tools: list | None = None,
                      stream: bool = False) -> dict[str, Any]:
    """构造透传 api_base/api_key 的 stateless Responses 参数。"""
    policy = _llm_request_policy(cfg)
    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0.7),
        "max_output_tokens": cfg.get("max_tokens", DEFAULT_LLM_MAX_OUTPUT_TOKENS),
        "store": False,
        "timeout": policy["request_timeout_seconds"],
        # LiteLLM passes this to the OpenAI client. It must be explicit so its
        # retry loop cannot silently multiply OpenReel's configured attempts.
        "max_retries": policy["sdk_max_retries"],
    }
    if cfg.get("top_p"):
        kwargs["top_p"] = cfg["top_p"]
    if cfg.get("api_base"):
        kwargs["api_base"] = cfg["api_base"]
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    if with_tools:
        kwargs["tools"] = responses_tools(with_tools)
    if policy["include_reasoning_encrypted_content"]:
        kwargs["include"] = ["reasoning.encrypted_content"]
    if policy["use_chat_completions_api"]:
        kwargs["use_chat_completions_api"] = True
    if stream:
        kwargs["stream"] = True
    return kwargs


def _responses_websocket_url(api_base: str | None) -> str:
    base = str(api_base or "https://api.openai.com/v1").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise LLMResponsesWebSocketError("invalid Responses WebSocket API base")
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    path = parsed.path.rstrip("/")
    if not path.endswith("/responses"):
        path = f"{path}/responses" if path else "/v1/responses"
    return urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))


def _responses_websocket_model(model: str) -> str:
    value = str(model or "").strip()
    return value.split("/", 1)[1] if value.startswith("openai/") else value


def _responses_websocket_body(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Project a LiteLLM Responses request onto ``response.create`` fields."""

    excluded = {
        "api_base",
        "api_key",
        "max_retries",
        "stream",
        "timeout",
        "use_chat_completions_api",
    }
    body = {
        key: value
        for key, value in kwargs.items()
        if key not in excluded and value is not None
    }
    body["type"] = "response.create"
    body["model"] = _responses_websocket_model(str(body.get("model") or ""))
    return body


async def _connect_responses_websocket(
    url: str,
    *,
    api_key: str,
    timeout: float,
    provider_params: dict[str, Any],
) -> Any:
    from websockets.asyncio.client import connect

    headers: list[tuple[str, str]] = [("Authorization", f"Bearer {api_key}")]
    organization = str(provider_params.get("organization") or "").strip()
    project = str(provider_params.get("project") or "").strip()
    if organization:
        headers.append(("OpenAI-Organization", organization))
    if project:
        headers.append(("OpenAI-Project", project))
    return await connect(
        url,
        additional_headers=headers,
        open_timeout=min(timeout, 30.0),
        close_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        max_size=16 * 1024 * 1024,
    )


_TEXT_ONLY_IMAGE_UNSUPPORTED_MODEL_MARKERS = (
    "deepseek",
)
_IMAGE_SUPPORTED_MODEL_MARKERS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "gemini",
    "claude-3",
    "claude-sonnet",
    "claude-opus",
    "qwen-vl",
    "qwen2-vl",
    "qwen3-vl",
    "doubao-vision",
    "kimi-vl",
)


class LLMImageInputUnsupportedError(ValueError):
    """Raised when image input is required but the selected model is text-only."""


def model_supports_image_input(
    model: str | None,
    api_base: str | None = None,
    supports_vision: bool | None = None,
) -> bool:
    """Return whether the selected model accepts Responses image input parts."""
    if isinstance(supports_vision, bool):
        return supports_vision
    name = (model or "").lower()
    if any(marker in name for marker in _TEXT_ONLY_IMAGE_UNSUPPORTED_MODEL_MARKERS):
        return False
    return any(marker in name for marker in _IMAGE_SUPPORTED_MODEL_MARKERS)


def _messages_have_image_input(messages: list[dict]) -> bool:
    for message in messages:
        if message.get("type") == "input_image":
            return True
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict)
            and part.get("type") in {"image_url", "input_image"}
            for part in content
        ):
            return True
    return False


def _build_response_request_for_config(
    messages: list[dict],
    system_prompt: str | None,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    supports_images = model_supports_image_input(
        cfg.get("model"),
        cfg.get("api_base"),
        (cfg.get("model_metadata") or {}).get("supports_vision"),
    )
    if _messages_have_image_input(messages) and not supports_images:
        raise LLMImageInputUnsupportedError(
            "selected model does not support required image input: "
            f"{cfg.get('model') or '<unknown>'}; choose a vision-capable model or set supports_vision=true"
        )
    return prepare_response_input(
        messages,
        system_prompt,
        allow_image_input=supports_images,
    )


def _exc_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def is_context_length_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


def _is_retryable_llm_error(exc: Exception) -> bool:
    if is_context_length_error(exc):
        return False
    code = _exc_status_code(exc)
    if code in _RETRYABLE_STATUS_CODES:
        return True
    name = exc.__class__.__name__.lower()
    return any(marker in name for marker in ("ratelimit", "timeout", "connection", "serviceunavailable"))


def _is_retryable_stream_status(response: Any) -> bool:
    raw_error = (
        response.get("error")
        if isinstance(response, dict)
        else getattr(response, "error", None)
    )
    text = json.dumps(raw_error, ensure_ascii=False, default=str).lower()
    return any(marker in text for marker in _RETRYABLE_STREAM_ERROR_MARKERS)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text") or item.get("content")
        else:
            text = getattr(item, "text", None) or getattr(item, "content", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _payload_message_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content"))


def _exception_response_payloads(exc: Exception) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for attr in ("body", "json_body"):
        value = getattr(exc, attr, None)
        if isinstance(value, dict):
            payloads.append(value)

    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        payloads.append(response)
    elif response is not None:
        try:
            value = response.json()
            if isinstance(value, dict):
                payloads.append(value)
        except Exception:
            text = getattr(response, "text", None)
            if isinstance(text, str) and text.strip():
                try:
                    value = json.loads(text)
                    if isinstance(value, dict):
                        payloads.append(value)
                except Exception:
                    pass
    return payloads


def _recover_backend_response(exc: Exception, *, model: str) -> Any | None:
    """Recover a complete OpenAI-compatible response attached to an SDK error.

    Some relays return a usable response body while the SDK still raises for a
    transport/status edge case. Only a standard non-empty choices/message
    payload is accepted; arbitrary error text is never treated as model output.
    """
    for raw_payload in _exception_response_payloads(exc):
        candidates = [raw_payload]
        nested = raw_payload.get("data")
        if isinstance(nested, dict):
            candidates.append(nested)
        for payload in candidates:
            if isinstance(payload.get("output"), list) and payload.get("output"):
                normalized = dict(payload)
                normalized.setdefault("id", f"resp_recovered_{uuid.uuid4().hex[:12]}")
                normalized.setdefault("created_at", 0)
                normalized.setdefault("model", model)
                normalized.setdefault("status", "completed")
                try:
                    response = litellm.ResponsesAPIResponse(**normalized)
                except Exception:
                    response = None
                if response is not None:
                    try:
                        setattr(response, "_openreel_recovered_from_exception", True)
                        setattr(response, "_openreel_recovery_exception_type", exc.__class__.__name__)
                    except Exception:
                        pass
                    return response
            if not _payload_message_content(payload).strip():
                continue
            normalized = dict(payload)
            normalized.setdefault("model", model)
            try:
                response = litellm.ModelResponse(**normalized)
            except Exception:
                continue
            try:
                setattr(response, "_openreel_recovered_from_exception", True)
                setattr(response, "_openreel_recovery_exception_type", exc.__class__.__name__)
            except Exception:
                pass
            return response
    return None


async def _aresponses_with_retries(
    kwargs: dict[str, Any],
    *,
    fallback_model: str | None = None,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0.5,
    accept_backend_content: bool = True,
    request_id: str | None = None,
) -> Any:
    request_id = request_id or uuid.uuid4().hex[:12]
    max_attempts = max(1, int(max_attempts))
    models = [kwargs["model"]]
    if fallback_model and fallback_model not in models:
        models.append(fallback_model)

    last_exc: Exception | None = None
    for model in models:
        call_kwargs = dict(kwargs)
        call_kwargs["model"] = model
        for attempt in range(max_attempts):
            started_at = asyncio.get_running_loop().time()
            try:
                logger.info(
                    "LLM request start request_id=%s model=%s attempt=%s/%s",
                    request_id,
                    model,
                    attempt + 1,
                    max_attempts,
                )
                response = await litellm.aresponses(**call_kwargs)
                try:
                    setattr(response, "_openreel_requested_model", kwargs["model"])
                    setattr(response, "_openreel_actual_model", model)
                    setattr(response, "_openreel_fallback_used", model != kwargs["model"])
                except Exception:
                    if isinstance(response, dict):
                        response["_openreel_requested_model"] = kwargs["model"]
                        response["_openreel_actual_model"] = model
                        response["_openreel_fallback_used"] = model != kwargs["model"]
                if hasattr(response, "__aiter__"):
                    logger.info(
                        "LLM Responses stream established request_id=%s model=%s attempt=%s/%s elapsed=%.3fs",
                        request_id,
                        model,
                        attempt + 1,
                        max_attempts,
                        asyncio.get_running_loop().time() - started_at,
                    )
                    return response
                view = response_view(response)
                if view.status in {"failed", "cancelled", "queued", "in_progress"}:
                    raw_error = (
                        response.get("error")
                        if isinstance(response, dict)
                        else getattr(response, "error", None)
                    )
                    raise LLMResponseStatusError(
                        f"Responses request ended with status={view.status}: {raw_error or 'no error details'}"
                    )
                logger.info(
                    "LLM Responses request completed request_id=%s model=%s attempt=%s/%s elapsed=%.3fs status=%s finish_reason=%s tool_calls=%s has_content=%s",
                    request_id,
                    model,
                    attempt + 1,
                    max_attempts,
                    asyncio.get_running_loop().time() - started_at,
                    view.status,
                    view.finish_reason,
                    len(view.tool_calls),
                    bool(view.content.strip()),
                )
                return response
            except Exception as exc:
                last_exc = exc
                if accept_backend_content:
                    recovered = _recover_backend_response(exc, model=model)
                    if recovered is not None:
                        try:
                            setattr(recovered, "_openreel_requested_model", kwargs["model"])
                            setattr(recovered, "_openreel_actual_model", model)
                            setattr(recovered, "_openreel_fallback_used", model != kwargs["model"])
                        except Exception:
                            pass
                        logger.warning(
                            "LLM request accepted attached backend content request_id=%s model=%s attempt=%s/%s elapsed=%.3fs exception=%s",
                            request_id,
                            model,
                            attempt + 1,
                            max_attempts,
                            asyncio.get_running_loop().time() - started_at,
                            exc.__class__.__name__,
                        )
                        return recovered
                logger.warning(
                    "LLM request failed request_id=%s model=%s attempt=%s/%s elapsed=%.3fs exception=%s retryable=%s",
                    request_id,
                    model,
                    attempt + 1,
                    max_attempts,
                    asyncio.get_running_loop().time() - started_at,
                    exc.__class__.__name__,
                    _is_retryable_llm_error(exc),
                )
                if is_context_length_error(exc):
                    raise
                if not _is_retryable_llm_error(exc) or attempt >= max_attempts - 1:
                    break
                await asyncio.sleep(min(60.0, retry_backoff_seconds * (2 ** attempt)))
    assert last_exc is not None
    raise last_exc


def _response_finish_reason(response: Any) -> str:
    return response_view(response).finish_reason


def _response_content(response: Any) -> str:
    return response_view(response).content


def _copy_response_with_content(response: Any, content: str) -> Any:
    try:
        setattr(response, "_openreel_combined_content", content)
    except Exception:
        if isinstance(response, dict):
            response["_openreel_combined_content"] = content
    return response


def _set_response_finish_reason(response: Any, finish_reason: str) -> None:
    try:
        setattr(response, "_openreel_final_finish_reason", finish_reason)
    except Exception:
        if isinstance(response, dict):
            response["_openreel_final_finish_reason"] = finish_reason


def _aggregate_response_usage(responses: list[Any]) -> dict[str, Any]:
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cached_prompt_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    )
    totals = {key: 0 for key in keys}
    found: set[str] = set()
    for response in responses:
        usage = extract_usage_from_response(response)
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] += max(0, int(value))
            found.add(key)
    result = {key: totals[key] for key in keys if key in found}
    result["llm_calls"] = len(responses)
    prompt_tokens = result.get("prompt_tokens")
    cached_tokens = result.get("cached_prompt_tokens")
    if isinstance(prompt_tokens, int) and prompt_tokens > 0 and isinstance(cached_tokens, int):
        result["cache_hit_rate"] = round(cached_tokens / prompt_tokens, 4)
    return result


def _attach_continuation_metadata(
    response: Any,
    *,
    responses: list[Any],
    continuation_count: int,
    final_finish_reason: str,
    exhausted: bool,
    error: str | None = None,
) -> Any:
    latest_usage = extract_usage_from_response(responses[-1]) if responses else {}
    metadata = {
        "_openreel_aggregate_usage": _aggregate_response_usage(responses),
        "_openreel_latest_prompt_tokens": latest_usage.get("prompt_tokens"),
        "_openreel_continuation_count": continuation_count,
        "_openreel_final_finish_reason": final_finish_reason,
        "_openreel_continuation_exhausted": exhausted,
    }
    if error:
        metadata["_openreel_continuation_error"] = error
    for key, value in metadata.items():
        try:
            setattr(response, key, value)
        except Exception:
            if isinstance(response, dict):
                response[key] = value
    _set_response_finish_reason(response, final_finish_reason)
    return response


async def _continue_text_if_truncated(
    kwargs: dict[str, Any],
    response: Any,
    *,
    fallback_model: str | None = None,
    max_continuations: int = 1,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0.5,
    accept_backend_content: bool = True,
    require_complete: bool = False,
) -> Any:
    finish_reason = _response_finish_reason(response)
    if finish_reason not in _MAX_OUTPUT_FINISH_REASONS:
        return response
    if response_view(response).tool_calls:
        try:
            setattr(response, "_openreel_tool_call_truncated", True)
        except Exception:
            pass
        return response

    combined = _response_content(response)
    if not combined:
        return response

    continue_kwargs = dict(kwargs)
    continuation_input = list(kwargs.get("input") or [])
    responses = [response]
    continuation_count = 0
    continuation_error: str | None = None
    for _ in range(max_continuations):
        continue_input = [
            *continuation_input,
            *replay_output_items(responses[-1]),
            {"role": "user", "content": "Continue exactly where you stopped. Do not repeat previous text."},
        ]
        continue_kwargs["input"] = continue_input
        try:
            next_response = await _aresponses_with_retries(
                continue_kwargs,
                fallback_model=fallback_model,
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                accept_backend_content=accept_backend_content,
            )
        except Exception as exc:
            continuation_error = str(exc)
            if require_complete or not accept_backend_content or not combined.strip():
                _attach_continuation_metadata(
                    response,
                    responses=responses,
                    continuation_count=continuation_count,
                    final_finish_reason=finish_reason,
                    exhausted=True,
                    error=continuation_error,
                )
                raise LLMOutputTruncatedError(
                    "LLM continuation failed before a complete text result was produced",
                    partial_content=combined,
                    continuation_count=continuation_count,
                ) from exc
            logger.warning(
                "LLM continuation failed; accepting previously received backend content exception=%s content_chars=%s",
                exc.__class__.__name__,
                len(combined),
            )
            try:
                setattr(response, "_openreel_partial_content_accepted", True)
                setattr(response, "_openreel_continuation_error", str(exc))
            except Exception:
                pass
            break
        responses.append(next_response)
        continuation_input = continue_input
        continuation_count += 1
        combined += _response_content(next_response)
        finish_reason = _response_finish_reason(next_response)
        if finish_reason not in _MAX_OUTPUT_FINISH_REASONS:
            break
    exhausted = finish_reason in _MAX_OUTPUT_FINISH_REASONS
    response = _copy_response_with_content(response, combined)
    response = _attach_continuation_metadata(
        response,
        responses=responses,
        continuation_count=continuation_count,
        final_finish_reason=finish_reason,
        exhausted=exhausted,
        error=continuation_error,
    )
    if require_complete and exhausted:
        raise LLMOutputTruncatedError(
            "LLM output remained truncated after bounded continuation",
            partial_content=combined,
            continuation_count=continuation_count,
        )
    return response


def _apply_call_max_tokens(cfg: dict[str, Any], kwargs: dict[str, Any], max_tokens: int | None) -> None:
    if max_tokens is None:
        return
    requested = max(1, int(max_tokens))
    metadata = cfg.get("model_metadata")
    provider_limit = metadata.get("max_output_tokens") if isinstance(metadata, dict) else None
    if isinstance(provider_limit, int) and provider_limit > 0:
        requested = min(requested, provider_limit)
    kwargs["max_output_tokens"] = requested


class LLMService:
    """Class form used by the orchestrator (needs db handle for model config)."""

    def __init__(self, db: AsyncSession | None = None):
        self.db = db

    async def generate(
        self,
        task_type: str,
        messages: list[dict],
        system: str | None = None,
        project_id: str | None = None,
        node_override: str | None = None,
        max_tokens: int | None = None,
        require_complete: bool = False,
    ) -> dict[str, Any]:
        cfg = await _resolve_config(task_type, self.db, node_override)
        policy = _llm_request_policy(cfg)
        kwargs = _responses_kwargs(cfg)
        _apply_call_max_tokens(cfg, kwargs, max_tokens)
        input_items, instructions = _build_response_request_for_config(messages, system, cfg)
        kwargs["input"] = input_items
        if instructions:
            kwargs["instructions"] = instructions
        response = await _aresponses_with_retries(
            kwargs,
            fallback_model=cfg.get("fallback_model"),
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
        )
        response = await _continue_text_if_truncated(
            kwargs,
            response,
            fallback_model=cfg.get("fallback_model"),
            max_continuations=policy["max_continuations"],
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
            require_complete=require_complete,
        )
        response = _attach_model_metadata(response, cfg.get("model_metadata") or {})
        view = response_view(response)
        content = view.content
        actual_model = str(getattr(response, "_openreel_actual_model", "") or kwargs["model"])
        return {
            "content": content,
            "model": actual_model,
            "usage": build_usage_snapshot(
                response,
                messages=input_items,
                system=instructions,
                model=actual_model,
                model_metadata=cfg.get("model_metadata") or {},
            ),
            "finish_reason": str(
                getattr(response, "_openreel_final_finish_reason", "")
                or view.finish_reason
            ),
            "response_id": view.response_id,
            "response_status": view.status,
            "api_mode": view.api_mode,
            "continuation_count": int(
                getattr(response, "_openreel_continuation_count", 0) or 0
            ),
            "continuation_exhausted": bool(
                getattr(response, "_openreel_continuation_exhausted", False)
            ),
        }

    async def generate_text(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        require_complete: bool = False,
    ) -> str:
        policy = _llm_request_policy({})
        cfg: dict[str, Any] = {
            "model": model or settings.DEFAULT_FAST_MODEL,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        env_key = _resolve_env_key_for_default(cfg["model"])
        if env_key:
            cfg["api_key"] = env_key
        kwargs = _responses_kwargs(cfg)
        input_items, instructions = _build_response_request_for_config(messages, system_prompt, cfg)
        kwargs["input"] = input_items
        if instructions:
            kwargs["instructions"] = instructions
        response = await _aresponses_with_retries(
            kwargs,
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
        )
        response = await _continue_text_if_truncated(
            kwargs,
            response,
            max_continuations=policy["max_continuations"],
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
            require_complete=require_complete,
        )
        return _response_content(response)

    async def stream(
        self,
        task_type: str,
        messages: list[dict],
        system: str | None = None,
        project_id: str | None = None,
        node_override: str | None = None,
    ) -> AsyncIterator[str]:
        cfg = await _resolve_config(task_type, self.db, node_override)
        policy = _llm_request_policy(cfg)
        kwargs = _responses_kwargs(cfg, stream=True)
        input_items, instructions = _build_response_request_for_config(messages, system, cfg)
        kwargs["input"] = input_items
        if instructions:
            kwargs["instructions"] = instructions
        response = await _aresponses_with_retries(
            kwargs,
            fallback_model=cfg.get("fallback_model"),
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
        )
        if not hasattr(response, "__aiter__"):
            content = _response_content(response)
            if content:
                yield content
            return
        async for event in response:
            delta = stream_text_delta(event)
            if delta:
                yield delta

    async def stream_with_tools(
        self,
        task_type: str,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        project_id: str | None = None,
        node_override: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ResponseStreamUpdate]:
        cfg = await _resolve_config(task_type, self.db, node_override)
        async for update in self._stream_with_tools_resolved(
            cfg,
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
        ):
            yield update

    async def _stream_with_tools_resolved(
        self,
        cfg: dict[str, Any],
        *,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ResponseStreamUpdate]:
        """Stream a tool-capable Responses turn as typed lifecycle updates.

        Text deltas may be presented immediately. Durable model actions come
        from completed output items and the terminal response. A stream ending
        without a terminal event is an error, matching the Codex turn-loop
        invariant and preventing partial output from being committed as final.
        """

        policy = _llm_request_policy(cfg)
        kwargs = _responses_kwargs(cfg, with_tools=tools, stream=True)
        _apply_call_max_tokens(cfg, kwargs, max_tokens)
        input_items, instructions = _build_response_request_for_config(messages, system, cfg)
        kwargs["input"] = input_items
        if instructions:
            kwargs["instructions"] = instructions
        stream_attempts = policy["max_retries"] + 1
        for stream_attempt in range(stream_attempts):
            emitted_text = False
            retry_error: Exception | None = None
            try:
                stream = await _aresponses_with_retries(
                    kwargs,
                    fallback_model=cfg.get("fallback_model"),
                    max_attempts=1,
                    retry_backoff_seconds=policy["retry_backoff_seconds"],
                    accept_backend_content=policy["accept_backend_content"],
                )

                if not hasattr(stream, "__aiter__"):
                    response = _attach_model_metadata(stream, cfg.get("model_metadata") or {})
                    yield ResponseStreamUpdate(
                        kind="terminal",
                        event_type="response.completed.compat",
                        response=response,
                    )
                    return

                actual_model = str(
                    getattr(stream, "_openreel_actual_model", "")
                    or cfg.get("model")
                    or ""
                )
                terminal_seen = False
                async for raw_event in stream:
                    update = response_stream_update(raw_event)
                    if update is None:
                        continue
                    if update.kind != "terminal":
                        emitted_text |= update.kind == "text_delta"
                        yield update
                        continue

                    response = update.response
                    if response is None:
                        raise LLMResponseStatusError(
                            f"Responses stream emitted {update.event_type} "
                            "without a response payload"
                        )
                    response = _attach_model_metadata(
                        response,
                        cfg.get("model_metadata") or {},
                    )
                    try:
                        setattr(response, "_openreel_requested_model", cfg.get("model"))
                        setattr(response, "_openreel_actual_model", actual_model)
                        setattr(
                            response,
                            "_openreel_fallback_used",
                            actual_model != cfg.get("model"),
                        )
                    except Exception:
                        if isinstance(response, dict):
                            response["_openreel_requested_model"] = cfg.get("model")
                            response["_openreel_actual_model"] = actual_model
                            response["_openreel_fallback_used"] = (
                                actual_model != cfg.get("model")
                            )

                    view = response_view(response)
                    failed = (
                        update.event_type in {"response.failed", "response.cancelled"}
                        or view.status in {
                            "failed",
                            "cancelled",
                            "queued",
                            "in_progress",
                        }
                    )
                    if failed:
                        raw_error = (
                            response.get("error")
                            if isinstance(response, dict)
                            else getattr(response, "error", None)
                        )
                        status_error = LLMResponseStatusError(
                            "Responses stream ended with "
                            f"status={view.status or update.event_type}: "
                            f"{raw_error or 'no error details'}"
                        )
                        if (
                            not emitted_text
                            and stream_attempt < stream_attempts - 1
                            and _is_retryable_stream_status(response)
                        ):
                            retry_error = status_error
                            break
                        raise status_error

                    terminal_seen = True
                    yield ResponseStreamUpdate(
                        kind="terminal",
                        event_type=update.event_type,
                        response=response,
                    )
                    return

                if retry_error is None and not terminal_seen:
                    closed_error = LLMResponseStatusError(
                        "Responses stream closed before response.completed or "
                        "response.incomplete"
                    )
                    if not emitted_text and stream_attempt < stream_attempts - 1:
                        retry_error = closed_error
                    else:
                        raise closed_error
            except Exception as exc:
                if (
                    not emitted_text
                    and stream_attempt < stream_attempts - 1
                    and _is_retryable_llm_error(exc)
                ):
                    retry_error = exc
                else:
                    raise

            if retry_error is not None:
                logger.warning(
                    "LLM Responses stream retrying attempt=%s/%s exception=%s",
                    stream_attempt + 1,
                    stream_attempts,
                    retry_error.__class__.__name__,
                )
                await asyncio.sleep(
                    min(
                        60.0,
                        policy["retry_backoff_seconds"] * (2 ** stream_attempt),
                    )
                )
                continue

        raise LLMResponseStatusError("Responses stream retries exhausted")

    def new_turn_session(self) -> "LLMResponsesTurnSession":
        """Create isolated model transport state for one user turn."""

        return LLMResponsesTurnSession(self)

    async def generate_with_tools(
        self,
        task_type: str,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        project_id: str | None = None,
        node_override: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """LLM call with function-calling tools. Returns the full response object."""
        cfg = await _resolve_config(task_type, self.db, node_override)
        policy = _llm_request_policy(cfg)
        kwargs = _responses_kwargs(cfg, with_tools=tools)
        _apply_call_max_tokens(cfg, kwargs, max_tokens)
        input_items, instructions = _build_response_request_for_config(messages, system, cfg)
        kwargs["input"] = input_items
        if instructions:
            kwargs["instructions"] = instructions
        response = await _aresponses_with_retries(
            kwargs,
            fallback_model=cfg.get("fallback_model"),
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
        )
        response = _attach_model_metadata(response, cfg.get("model_metadata") or {})
        response = await _continue_text_if_truncated(
            kwargs,
            response,
            fallback_model=cfg.get("fallback_model"),
            max_continuations=policy["max_continuations"],
            max_attempts=policy["max_retries"] + 1,
            retry_backoff_seconds=policy["retry_backoff_seconds"],
            accept_backend_content=policy["accept_backend_content"],
        )
        return response


class LLMResponsesTurnSession:
    """Turn-scoped Responses transport with Codex-style WebSocket fallback.

    The socket, previous response id, and incremental-input cache never cross a
    user-turn boundary. Providers without explicit WebSocket capability keep
    using the existing stateless HTTP/SSE path.
    """

    def __init__(self, service: LLMService):
        self._service = service
        self._cfg: dict[str, Any] | None = None
        self._config_key: tuple[str, str] | None = None
        self._ws: Any | None = None
        self._websocket_disabled = False
        self._last_request_fingerprint = ""
        self._last_request_input: list[dict[str, Any]] = []
        self._last_response_items: list[dict[str, Any]] = []
        self._last_response_id = ""

    async def _resolved_config(
        self,
        task_type: str,
        node_override: str | None,
    ) -> dict[str, Any]:
        key = (str(task_type or ""), str(node_override or ""))
        if self._cfg is None:
            self._cfg = await _resolve_config(task_type, self._service.db, node_override)
            self._config_key = key
        elif self._config_key != key:
            await self.aclose()
            self._websocket_disabled = False
            self._cfg = await _resolve_config(task_type, self._service.db, node_override)
            self._config_key = key
        return self._cfg

    @staticmethod
    def _set_response_metadata(response: Any, **metadata: Any) -> Any:
        for key, value in metadata.items():
            attr = f"_openreel_{key}"
            try:
                setattr(response, attr, value)
            except Exception:
                if isinstance(response, dict):
                    response[attr] = value
        return response

    @staticmethod
    def _request_fingerprint(body: dict[str, Any]) -> str:
        stable = {
            key: value
            for key, value in body.items()
            if key not in {"input", "previous_response_id", "type"}
        }
        return json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)

    def _request_body(
        self,
        kwargs: dict[str, Any],
        full_input: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool, str]:
        body = _responses_websocket_body(kwargs)
        fingerprint = self._request_fingerprint(body)
        expected_prefix = [
            *self._last_request_input,
            *self._last_response_items,
        ]
        can_increment = bool(
            self._last_response_id
            and fingerprint == self._last_request_fingerprint
            and len(full_input) > len(expected_prefix)
            and full_input[: len(expected_prefix)] == expected_prefix
        )
        if can_increment:
            body["previous_response_id"] = self._last_response_id
            body["input"] = full_input[len(expected_prefix) :]
            return body, True, "incremental_extension"
        body.pop("previous_response_id", None)
        body["input"] = full_input
        reason = "first_request" if not self._last_response_id else "full_context_changed"
        return body, False, reason

    async def _ensure_websocket(
        self,
        cfg: dict[str, Any],
        policy: dict[str, Any],
    ) -> Any:
        if self._ws is not None:
            return self._ws
        api_key = _resolve_key_reference(str(cfg.get("api_key") or ""))
        if not api_key:
            raise LLMResponsesWebSocketError("Responses WebSocket requires an API key")
        self._ws = await _connect_responses_websocket(
            _responses_websocket_url(cfg.get("api_base")),
            api_key=api_key,
            timeout=float(policy["request_timeout_seconds"]),
            provider_params=_provider_params(cfg),
        )
        return self._ws

    async def _stream_websocket(
        self,
        cfg: dict[str, Any],
        policy: dict[str, Any],
        kwargs: dict[str, Any],
        full_input: list[dict[str, Any]],
    ) -> AsyncIterator[ResponseStreamUpdate]:
        ws = await self._ensure_websocket(cfg, policy)
        body, incremental, reuse_reason = self._request_body(kwargs, full_input)
        sent_input_count = len(body.get("input") or [])
        async with asyncio.timeout(float(policy["request_timeout_seconds"])):
            await ws.send(json.dumps(body, ensure_ascii=False, default=str))
            while True:
                raw_event = await ws.recv()
                if isinstance(raw_event, bytes):
                    raw_event = raw_event.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw_event)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise LLMResponsesWebSocketError(
                        "Responses WebSocket returned invalid JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "error":
                    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                    code = str(error.get("code") or payload.get("code") or "")
                    message = str(error.get("message") or payload.get("message") or code or "unknown error")
                    raise LLMResponsesWebSocketError(message, code=code)

                update = response_stream_update(payload)
                if update is None:
                    continue
                if update.kind != "terminal":
                    yield update
                    continue

                response = update.response
                if response is None:
                    raise LLMResponsesWebSocketError(
                        f"Responses WebSocket emitted {update.event_type} without a response"
                    )
                response = _attach_model_metadata(response, cfg.get("model_metadata") or {})
                response = self._set_response_metadata(
                    response,
                    requested_model=cfg.get("model"),
                    actual_model=_responses_websocket_model(str(cfg.get("model") or "")),
                    fallback_used=False,
                    transport="responses_websocket",
                    transport_incremental=incremental,
                    transport_reuse_reason=reuse_reason,
                    transport_input_items_sent=sent_input_count,
                    transport_full_input_items=len(full_input),
                )
                view = response_view(response)
                if (
                    update.event_type in {"response.failed", "response.cancelled"}
                    or view.status in {"failed", "cancelled", "queued", "in_progress"}
                ):
                    raw_error = (
                        response.get("error")
                        if isinstance(response, dict)
                        else getattr(response, "error", None)
                    )
                    raise LLMResponsesWebSocketError(
                        "Responses WebSocket ended with "
                        f"status={view.status or update.event_type}: "
                        f"{raw_error or 'no error details'}"
                    )

                self._last_request_fingerprint = self._request_fingerprint(body)
                self._last_request_input = [dict(item) for item in full_input]
                self._last_response_items = response_output_items(response)
                self._last_response_id = view.response_id
                yield ResponseStreamUpdate(
                    kind="terminal",
                    event_type=update.event_type,
                    response=response,
                )
                return

    async def _stream_http(
        self,
        cfg: dict[str, Any],
        *,
        messages: list[dict],
        tools: list[dict],
        system: str | None,
        max_tokens: int | None,
        reason: str,
    ) -> AsyncIterator[ResponseStreamUpdate]:
        async for update in self._service._stream_with_tools_resolved(
            cfg,
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
        ):
            if update.kind == "terminal" and update.response is not None:
                self._set_response_metadata(
                    update.response,
                    transport="responses_http_sse",
                    transport_incremental=False,
                    transport_reuse_reason=reason,
                )
            yield update

    async def stream_with_tools(
        self,
        task_type: str,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        project_id: str | None = None,
        node_override: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ResponseStreamUpdate]:
        del project_id
        cfg = await self._resolved_config(task_type, node_override)
        policy = _llm_request_policy(cfg)
        websocket_capable = bool(
            policy["supports_responses_websocket"]
            and not policy["use_chat_completions_api"]
            and _resolve_key_reference(str(cfg.get("api_key") or ""))
        )
        if self._websocket_disabled or not websocket_capable:
            reason = (
                "websocket_fallback"
                if self._websocket_disabled
                else "provider_not_websocket_capable"
            )
            async for update in self._stream_http(
                cfg,
                messages=messages,
                tools=tools,
                system=system,
                max_tokens=max_tokens,
                reason=reason,
            ):
                yield update
            return

        kwargs = _responses_kwargs(cfg, with_tools=tools, stream=True)
        _apply_call_max_tokens(cfg, kwargs, max_tokens)
        full_input, instructions = _build_response_request_for_config(messages, system, cfg)
        kwargs["input"] = full_input
        if instructions:
            kwargs["instructions"] = instructions

        emitted_text = False
        try:
            async for update in self._stream_websocket(cfg, policy, kwargs, full_input):
                emitted_text |= update.kind == "text_delta"
                yield update
            return
        except Exception as exc:
            await self._disable_websocket()
            if emitted_text:
                raise
            logger.warning(
                "Responses WebSocket unavailable; falling back to HTTP for this turn: %s",
                exc.__class__.__name__,
            )
            async for update in self._stream_http(
                cfg,
                messages=messages,
                tools=tools,
                system=system,
                max_tokens=max_tokens,
                reason=f"websocket_error:{getattr(exc, 'code', '') or exc.__class__.__name__}",
            ):
                yield update

    async def _disable_websocket(self) -> None:
        self._websocket_disabled = True
        ws, self._ws = self._ws, None
        self._last_request_fingerprint = ""
        self._last_request_input = []
        self._last_response_items = []
        self._last_response_id = ""
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("Responses WebSocket close failed", exc_info=True)

    async def aclose(self) -> None:
        ws, self._ws = self._ws, None
        self._last_request_fingerprint = ""
        self._last_request_input = []
        self._last_response_items = []
        self._last_response_id = ""
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("Responses WebSocket close failed", exc_info=True)
