"""LLM service — unified model gateway via LiteLLM.

配置真相源是 config/runtime.jsonc（ConfigStore 同步到 DB）。本服务每次请求：
  1. 查 model_configs.task_type → llm_provider_name
  2. 查 llm_providers 拿 base_url / api_key / provider / model_name
  3. 透传 api_base / api_key 给 litellm.acompletion（不依赖环境变量）

不再 _push_keys_to_env：这样改 runtime.jsonc 立即生效，无需重启。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator

import litellm
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.token_usage import build_usage_snapshot
from app.config import settings
from app.db.session import session_scope


_TASK_DEFAULTS = {
    "agent_loop": "DEFAULT_FAST_MODEL",
    "agent_review": "DEFAULT_REVIEW_MODEL",
    "agent_compact": "DEFAULT_FAST_MODEL",
    "agent_aux": "DEFAULT_FAST_MODEL",
    # Legacy compatibility only. Runtime code should call agent_loop.
    "intent_parse": "DEFAULT_FAST_MODEL",
    "planning": "DEFAULT_FAST_MODEL",
    "script_generation": "DEFAULT_SCRIPT_MODEL",
    "script_review": "DEFAULT_REVIEW_MODEL",
    "character_generation": "DEFAULT_TEXT_MODEL",
    "outline_generation": "DEFAULT_TEXT_MODEL",
    "storyboard_generation": "DEFAULT_TEXT_MODEL",
    "image_understanding": "DEFAULT_TEXT_MODEL",
    "image_prompt_generation": "DEFAULT_TEXT_MODEL",
    "video_prompt_generation": "DEFAULT_TEXT_MODEL",
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
_MAX_OUTPUT_FINISH_REASONS = {"length", "max_tokens"}


class LLMConfigurationError(RuntimeError):
    """Raised when a hosted LLM task has no configured provider or API key."""


def _llm_request_timeout_seconds() -> float:
    try:
        return max(10.0, float(os.getenv("DRAMA_LLM_REQUEST_TIMEOUT_SECONDS", "90") or "90"))
    except (TypeError, ValueError):
        return 90.0


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
            "max_tokens": 8192,
            "api_base": None,
            "api_key": None,
            "model_metadata": {},
        }

    cfg_row = None
    provider_row = None
    if db is not None:
        from app.db.models import ModelConfig

        candidate_tasks = [task_type]
        if task_type == "agent_loop":
            candidate_tasks.append("intent_parse")
        else:
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
        "max_tokens": 8192,
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
    return {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens or provider_row.max_output_tokens or 8192,
        "top_p": top_p,
        "fallback_model": fallback_model,
        "api_base": provider_row.base_url,
        "api_key": provider_row.api_key,
        "model_metadata": _llm_provider_metadata(provider_row),
    }


def _completion_kwargs(cfg: dict, *, with_tools: list | None = None,
                       stream: bool = False) -> dict[str, Any]:
    """构造透传 api_base/api_key 的 acompletion 参数。"""
    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0.7),
        "max_tokens": cfg.get("max_tokens", 8192),
        "timeout": _llm_request_timeout_seconds(),
    }
    if cfg.get("top_p"):
        kwargs["top_p"] = cfg["top_p"]
    if cfg.get("api_base"):
        kwargs["api_base"] = cfg["api_base"]
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    if with_tools:
        kwargs["tools"] = with_tools
    if stream:
        kwargs["stream"] = True
    return kwargs


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
    """Return whether the chat endpoint accepts OpenAI-style image_url parts."""
    if isinstance(supports_vision, bool):
        return supports_vision
    name = (model or "").lower()
    if any(marker in name for marker in _TEXT_ONLY_IMAGE_UNSUPPORTED_MODEL_MARKERS):
        return False
    return any(marker in name for marker in _IMAGE_SUPPORTED_MODEL_MARKERS)


def _messages_have_image_input(messages: list[dict]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(part, dict) and part.get("type") == "image_url" for part in content):
            return True
    return False


def _build_messages_for_config(
    messages: list[dict],
    system_prompt: str | None,
    cfg: dict[str, Any],
) -> list[dict]:
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
    return _build_messages(messages, system_prompt, allow_image_input=supports_images)


_MESSAGE_API_KEYS = {
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "function_call",
}


def _text_only_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    texts: list[str] = []
    omitted_images = 0
    for part in content:
        if not isinstance(part, dict):
            text = str(part or "")
            if text:
                texts.append(text)
            continue
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text:
                texts.append(text)
        elif kind == "image_url":
            omitted_images += 1
        else:
            text = str(part.get("content") or part.get("text") or "")
            if text:
                texts.append(text)
    if omitted_images:
        texts.append(
            f"[{omitted_images} image_url part(s) omitted: selected model endpoint accepts text-only chat messages.]"
        )
    return "\n".join(texts)


def _message_for_api(message: dict, *, allow_image_input: bool = True) -> dict:
    clean = {
        key: value
        for key, value in message.items()
        if key in _MESSAGE_API_KEYS and value is not None
    }
    if not allow_image_input and "content" in clean:
        clean["content"] = _text_only_content(clean.get("content"))
    return clean


def _build_messages(
    messages: list[dict],
    system_prompt: str | None,
    *,
    allow_image_input: bool = True,
) -> list[dict]:
    clean_messages = [
        _message_for_api(message, allow_image_input=allow_image_input)
        for message in messages
    ]
    if system_prompt:
        return [{"role": "system", "content": system_prompt}, *clean_messages]
    return clean_messages


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


async def _acompletion_with_retries(
    kwargs: dict[str, Any],
    *,
    fallback_model: str | None = None,
    max_attempts: int = 3,
) -> Any:
    models = [kwargs["model"]]
    if fallback_model and fallback_model not in models:
        models.append(fallback_model)

    last_exc: Exception | None = None
    for model in models:
        call_kwargs = dict(kwargs)
        call_kwargs["model"] = model
        for attempt in range(max_attempts):
            try:
                response = await litellm.acompletion(**call_kwargs)
                try:
                    setattr(response, "_openreel_requested_model", kwargs["model"])
                    setattr(response, "_openreel_actual_model", model)
                    setattr(response, "_openreel_fallback_used", model != kwargs["model"])
                except Exception:
                    if isinstance(response, dict):
                        response["_openreel_requested_model"] = kwargs["model"]
                        response["_openreel_actual_model"] = model
                        response["_openreel_fallback_used"] = model != kwargs["model"]
                return response
            except Exception as exc:
                last_exc = exc
                if is_context_length_error(exc):
                    raise
                if not _is_retryable_llm_error(exc) or attempt >= max_attempts - 1:
                    break
                await asyncio.sleep(min(4.0, 0.5 * (2 ** attempt)))
    assert last_exc is not None
    raise last_exc


def _choice_message(response: Any) -> Any:
    return response.choices[0].message


def _choice_finish_reason(response: Any) -> str:
    return str(getattr(response.choices[0], "finish_reason", "") or "")


def _message_content(response: Any) -> str:
    return getattr(_choice_message(response), "content", None) or ""


def _copy_response_with_content(response: Any, content: str) -> Any:
    try:
        response.choices[0].message.content = content
    except Exception:
        pass
    return response


async def _continue_text_if_truncated(
    kwargs: dict[str, Any],
    response: Any,
    *,
    fallback_model: str | None = None,
    max_continuations: int = 1,
) -> Any:
    finish_reason = _choice_finish_reason(response)
    if finish_reason not in _MAX_OUTPUT_FINISH_REASONS:
        return response
    msg = _choice_message(response)
    if getattr(msg, "tool_calls", None):
        return response

    combined = _message_content(response)
    if not combined:
        return response

    continue_kwargs = dict(kwargs)
    continue_messages = list(kwargs.get("messages") or [])
    for _ in range(max_continuations):
        continue_messages = [
            *continue_messages,
            {"role": "assistant", "content": combined},
            {"role": "user", "content": "Continue exactly where you stopped. Do not repeat previous text."},
        ]
        continue_kwargs["messages"] = continue_messages
        next_response = await _acompletion_with_retries(
            continue_kwargs,
            fallback_model=fallback_model,
        )
        combined += _message_content(next_response)
        finish_reason = _choice_finish_reason(next_response)
        if finish_reason not in _MAX_OUTPUT_FINISH_REASONS:
            break
    return _copy_response_with_content(response, combined)


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
    ) -> dict[str, Any]:
        cfg = await _resolve_config(task_type, self.db, node_override)
        kwargs = _completion_kwargs(cfg)
        kwargs["messages"] = _build_messages_for_config(messages, system, cfg)
        response = await _acompletion_with_retries(
            kwargs,
            fallback_model=cfg.get("fallback_model"),
        )
        response = await _continue_text_if_truncated(
            kwargs,
            response,
            fallback_model=cfg.get("fallback_model"),
        )
        response = _attach_model_metadata(response, cfg.get("model_metadata") or {})
        content = response.choices[0].message.content or ""
        actual_model = str(getattr(response, "_openreel_actual_model", "") or kwargs["model"])
        return {
            "content": content,
            "model": actual_model,
            "usage": build_usage_snapshot(
                response,
                messages=kwargs["messages"],
                model=actual_model,
                model_metadata=cfg.get("model_metadata") or {},
            ),
        }

    async def generate_text(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model or settings.DEFAULT_FAST_MODEL,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        env_key = _resolve_env_key_for_default(kwargs["model"])
        if env_key:
            kwargs["api_key"] = env_key
        kwargs["messages"] = _build_messages_for_config(messages, system_prompt, kwargs)
        response = await _acompletion_with_retries(kwargs)
        response = await _continue_text_if_truncated(kwargs, response)
        return response.choices[0].message.content or ""

    async def stream(
        self,
        task_type: str,
        messages: list[dict],
        system: str | None = None,
        project_id: str | None = None,
        node_override: str | None = None,
    ) -> AsyncIterator[str]:
        cfg = await _resolve_config(task_type, self.db, node_override)
        kwargs = _completion_kwargs(cfg, stream=True)
        kwargs["messages"] = _build_messages_for_config(messages, system, cfg)
        response = await _acompletion_with_retries(
            kwargs,
            fallback_model=cfg.get("fallback_model"),
        )
        async for chunk in response:
            try:
                delta = chunk.choices[0].delta.content
            except (AttributeError, IndexError):
                delta = None
            if delta:
                yield delta

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
        kwargs = _completion_kwargs(cfg, with_tools=tools)
        if max_tokens is not None:
            kwargs["max_tokens"] = max(1, int(max_tokens))
        kwargs["messages"] = _build_messages_for_config(messages, system, cfg)
        response = await _acompletion_with_retries(
            kwargs,
            fallback_model=cfg.get("fallback_model"),
        )
        response = _attach_model_metadata(response, cfg.get("model_metadata") or {})
        response = await _continue_text_if_truncated(
            kwargs,
            response,
            fallback_model=cfg.get("fallback_model"),
        )
        return response


# Module-level singleton used by planner, mcp_tools, and agent helpers.
llm_service = LLMService(db=None)


async def generate_json(
    task_type: str,
    messages: list[dict],
    db: AsyncSession,
    system: str | None = None,
    project_id: str | None = None,
) -> Any:
    svc = LLMService(db)
    result = await svc.generate(task_type, messages, system=system, project_id=project_id)
    text = result["content"].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
        else:
            text = "\n".join(lines[1:])
    return json.loads(text)
