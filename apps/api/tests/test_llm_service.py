from types import SimpleNamespace

import pytest

from app.services import llm_service
from app.services.llm_service import LLMService


def _response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ]
    )


async def _fake_config(*args, **kwargs):
    return {
        "model": "test/model",
        "temperature": 0.0,
        "max_tokens": 100,
        "api_base": None,
        "api_key": None,
    }


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _FakeDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0

    async def exec(self, query):
        self.calls += 1
        value = self.rows.pop(0) if self.rows else None
        return _FakeResult(value)


@pytest.mark.asyncio
async def test_llm_generate_retries_retryable_error(monkeypatch) -> None:
    calls = {"count": 0}

    class RateLimitError(Exception):
        status_code = 429

    async def fake_acompletion(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RateLimitError("rate limited")
        return _response("ok")

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_service.asyncio, "sleep", fake_sleep)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "ok"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_llm_generate_reports_actual_fallback_model(monkeypatch) -> None:
    calls: list[str] = []

    class RateLimitError(Exception):
        status_code = 429

    async def fake_config(*args, **kwargs):
        return {
            "model": "test/primary",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_base": None,
            "api_key": None,
            "fallback_model": "test/fallback",
        }

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "test/primary":
            raise RateLimitError("rate limited")
        response = _response("ok")
        response.model = kwargs["model"]
        return response

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_service.asyncio, "sleep", fake_sleep)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert calls == ["test/primary", "test/primary", "test/primary", "test/fallback"]
    assert result["model"] == "test/fallback"
    assert result["usage"]["model"] == "test/fallback"
    assert result["usage"]["fallback_used"] is True


@pytest.mark.asyncio
async def test_workflow_spec_uses_agent_loop_config_fallback(monkeypatch) -> None:
    provider_names: list[str] = []

    async def fake_lookup_provider(name: str):
        provider_names.append(name)
        return SimpleNamespace(
            name=name,
            provider="openai",
            model_name="gpt-5.5",
            base_url="https://example.test/v1",
            api_key="sk-test",
            max_output_tokens=4000,
            context_window_tokens=None,
            max_input_tokens=None,
            supports_prompt_cache=None,
            supports_vision=None,
            tokenizer=None,
            tier=None,
            params_json=None,
        )

    agent_loop_config = SimpleNamespace(
        task_type="agent_loop",
        llm_provider_name="agent-provider",
        enabled=True,
        temperature=0.2,
        max_tokens=1234,
        top_p=0.8,
        fallback_model=None,
    )
    db = _FakeDb([None, agent_loop_config])
    monkeypatch.setattr(llm_service, "_lookup_llm_provider", fake_lookup_provider)

    cfg = await llm_service._resolve_config("subagent_workflow_spec", db, None)

    assert db.calls == 2
    assert provider_names == ["agent-provider"]
    assert cfg["model"] == "openai/gpt-5.5"
    assert cfg["api_key"] == "sk-test"
    assert cfg["temperature"] == 0.2
    assert cfg["max_tokens"] == 1234


@pytest.mark.asyncio
async def test_unmapped_workflow_task_uses_configured_agent_provider(monkeypatch) -> None:
    provider_names: list[str] = []

    async def fake_lookup_provider(name: str):
        provider_names.append(name)
        return SimpleNamespace(
            name=name,
            provider="openai",
            model_name="configured-workflow-model",
            base_url="https://relay.example.test/v1",
            api_key="configured-key",
            max_output_tokens=4096,
            context_window_tokens=None,
            max_input_tokens=None,
            supports_prompt_cache=None,
            supports_vision=None,
            tokenizer=None,
            tier=None,
            params_json=None,
        )

    agent_loop_config = SimpleNamespace(
        task_type="agent_loop",
        llm_provider_name="configured-agent",
        enabled=True,
        temperature=0.3,
        max_tokens=2048,
        top_p=0.9,
        fallback_model=None,
    )
    db = _FakeDb([None, agent_loop_config])
    monkeypatch.setattr(llm_service, "_lookup_llm_provider", fake_lookup_provider)

    cfg = await llm_service._resolve_config("outline_generation", db, None)

    assert db.calls == 2
    assert provider_names == ["configured-agent"]
    assert cfg["model"] == "openai/configured-workflow-model"
    assert cfg["api_base"] == "https://relay.example.test/v1"
    assert cfg["api_key"] == "configured-key"


@pytest.mark.asyncio
async def test_unconfigured_hosted_default_fails_before_litellm_auth(monkeypatch) -> None:
    db = _FakeDb([None, None])
    monkeypatch.setattr(llm_service, "_resolve_env_key_for_default", lambda model: None)

    with pytest.raises(llm_service.LLMConfigurationError, match="Configure an Agent or model-tier LLM"):
        await llm_service._resolve_config("outline_generation", db, None)


@pytest.mark.asyncio
async def test_node_override_provider_name_resolves_configured_llm_provider(monkeypatch) -> None:
    async def fake_lookup_provider(name: str):
        assert name == "Panel Text"
        return SimpleNamespace(
            name=name,
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://llm.example.test/v1",
            api_key="sk-panel",
            max_output_tokens=2048,
            context_window_tokens=None,
            max_input_tokens=None,
            supports_prompt_cache=None,
            supports_vision=False,
            tokenizer=None,
            tier="balanced",
            params_json=None,
        )

    monkeypatch.setattr(llm_service, "_lookup_llm_provider", fake_lookup_provider)

    cfg = await llm_service._resolve_config("text_generation", None, "Panel Text")

    assert cfg["model"] == "deepseek/deepseek-chat"
    assert cfg["api_base"] == "https://llm.example.test/v1"
    assert cfg["api_key"] == "sk-panel"
    assert cfg["max_tokens"] == 2048
    assert cfg["model_metadata"]["provider_name"] == "Panel Text"
    assert cfg["model_metadata"]["supports_vision"] is False


@pytest.mark.asyncio
async def test_llm_generate_does_not_retry_context_length(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_acompletion(**kwargs):
        calls["count"] += 1
        raise RuntimeError("prompt too long: context length exceeded")

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    with pytest.raises(RuntimeError):
        await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_llm_generate_continues_truncated_text(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_acompletion(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response("hello ", finish_reason="length")
        return _response("world", finish_reason="stop")

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "hello world"
    assert calls["count"] == 2


def test_llm_request_policy_uses_provider_params() -> None:
    cfg = {
        "model": "openai/test",
        "provider_params": {
            "request_timeout_seconds": 240,
            "max_retries": 1,
            "sdk_max_retries": 0,
            "retry_backoff_seconds": 1.25,
            "max_continuations": 0,
            "accept_backend_content": True,
        },
    }

    policy = llm_service._llm_request_policy(cfg)
    kwargs = llm_service._completion_kwargs(cfg)

    assert policy == {
        "request_timeout_seconds": 240.0,
        "max_retries": 1,
        "sdk_max_retries": 0,
        "retry_backoff_seconds": 1.25,
        "max_continuations": 0,
        "accept_backend_content": True,
    }
    assert kwargs["timeout"] == 240.0
    assert kwargs["max_retries"] == 0


@pytest.mark.asyncio
async def test_llm_generate_honors_zero_configured_retries(monkeypatch) -> None:
    calls = {"count": 0}

    class ConnectionError(Exception):
        pass

    async def fake_config(*args, **kwargs):
        return {
            **(await _fake_config()),
            "provider_params": {"max_retries": 0, "sdk_max_retries": 0},
        }

    async def fake_acompletion(**kwargs):
        calls["count"] += 1
        raise ConnectionError("downstream disconnected")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    with pytest.raises(ConnectionError):
        await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_llm_generate_recovers_standard_backend_content_from_exception(monkeypatch) -> None:
    class RelayResponseError(Exception):
        def __init__(self):
            super().__init__("relay closed after response")
            self.body = {
                "id": "relay-response",
                "model": "relay/model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "accepted backend content"},
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }

    async def fake_config(*args, **kwargs):
        return {
            **(await _fake_config()),
            "provider_params": {
                "max_retries": 0,
                "sdk_max_retries": 0,
                "accept_backend_content": True,
            },
        }

    async def fake_acompletion(**kwargs):
        raise RelayResponseError()

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "accepted backend content"


@pytest.mark.asyncio
async def test_failed_continuation_keeps_content_already_received(monkeypatch) -> None:
    calls = {"count": 0}

    class ConnectionError(Exception):
        pass

    async def fake_config(*args, **kwargs):
        return {
            **(await _fake_config()),
            "provider_params": {
                "max_retries": 0,
                "sdk_max_retries": 0,
                "max_continuations": 1,
                "accept_backend_content": True,
            },
        }

    async def fake_acompletion(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response("content already received", finish_reason="length")
        raise ConnectionError("continuation disconnected")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "content already received"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_message_content_accepts_backend_text_parts(monkeypatch) -> None:
    async def fake_acompletion(**kwargs):
        return _response([
            {"type": "text", "text": "hello "},
            {"type": "output_text", "text": "world"},
        ])

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "hello world"


@pytest.mark.asyncio
async def test_deepseek_generate_with_tools_rejects_required_image_input(monkeypatch) -> None:
    captured = {}

    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/deepseek-v4-pro",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_base": "https://api.deepseek.com/v1",
            "api_key": "test-key",
        }

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    with pytest.raises(llm_service.LLMImageInputUnsupportedError, match="does not support required image input"):
        await LLMService().generate_with_tools(
            "agent_loop",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Visual context retained."},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                    "_persisted_vision_context": True,
                }
            ],
            tools=[],
            system="system",
        )

    assert captured == {}


def test_model_image_capability_is_fail_closed_without_metadata() -> None:
    assert llm_service.model_supports_image_input("openai/gpt-5.5") is True
    assert llm_service.model_supports_image_input("openai/custom-vision-model") is False
    assert llm_service.model_supports_image_input(
        "openai/custom-vision-model",
        supports_vision=True,
    ) is True


@pytest.mark.asyncio
async def test_image_capable_generate_with_tools_keeps_image_parts(monkeypatch) -> None:
    captured = {}

    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-4o",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_base": None,
            "api_key": None,
        }

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    await LLMService().generate_with_tools(
        "agent_loop",
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ],
        tools=[],
    )

    user_message = captured["messages"][0]
    assert isinstance(user_message["content"], list)
    assert user_message["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_generate_with_tools_accepts_call_level_max_tokens(monkeypatch) -> None:
    captured = {}

    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-4o",
            "temperature": 0.0,
            "max_tokens": 4000,
            "api_base": None,
            "api_key": None,
        }

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_acompletion)

    await LLMService().generate_with_tools(
        "agent_loop",
        [{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10000,
    )

    assert captured["max_tokens"] == 10000
