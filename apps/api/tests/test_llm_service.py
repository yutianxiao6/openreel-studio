import json
from types import SimpleNamespace

import pytest

from app.agent.context_compact import CODEX_SUMMARY_PREFIX
from app.services import llm_service
from app.services.llm_service import LLMOutputTruncatedError, LLMService
from app.services.llm_responses import response_view


def _response(
    content: str,
    finish_reason: str = "stop",
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
):
    usage = None
    if prompt_tokens is not None or completion_tokens is not None:
        prompt = prompt_tokens or 0
        completion = completion_tokens or 0
        usage = SimpleNamespace(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=prompt + completion,
        )
    status = "incomplete" if finish_reason in {"length", "max_tokens", "max_output_tokens"} else "completed"
    return SimpleNamespace(
        id="resp-test",
        model="test/model",
        status=status,
        incomplete_details=(
            SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None
        ),
        output=[{
            "id": "msg-test",
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": (
                content
                if isinstance(content, list)
                else [{"type": "output_text", "text": content}]
            ),
        }],
        usage=usage,
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

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RateLimitError("rate limited")
        return _response("ok")

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)
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

    async def fake_aresponses(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "test/primary":
            raise RateLimitError("rate limited")
        response = _response("ok")
        response.model = kwargs["model"]
        return response

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)
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

    cfg = await llm_service._resolve_config("text_generation", db, None)

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
        await llm_service._resolve_config("text_generation", db, None)


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

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        raise RuntimeError("prompt too long: context length exceeded")

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    with pytest.raises(RuntimeError):
        await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_compaction_policy_uses_ninety_percent_and_provider_cap(monkeypatch) -> None:
    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-5.4",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_key": "sk-test",
            "model_metadata": {
                "context_window_tokens": 100_000,
                "params": {"auto_compact_token_limit": 70_000},
            },
            "provider_params": {"auto_compact_token_limit": 70_000},
        }

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)

    policy = await LLMService().get_compaction_policy()

    assert policy["threshold_tokens"] == 70_000
    assert policy["supports_responses_compact"] is True


@pytest.mark.asyncio
async def test_native_responses_compaction_returns_canonical_items_and_cache_key(monkeypatch) -> None:
    captured: dict = {}

    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-5.4",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_key": "sk-test",
            "model_metadata": {
                "context_window_tokens": 100_000,
                "supports_prompt_cache": True,
            },
        }

    async def fake_compact(cfg, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="cmp-response-1",
            output=[
                {"role": "developer", "content": "stale runtime"},
                {"role": "user", "content": "当前任务"},
                {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque"},
            ],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
        )

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_native_responses_compact_request", fake_compact)

    result = await LLMService().compact_conversation(
        messages=[{"role": "user", "content": "当前任务"}],
        system="system",
        tools=[{
            "type": "function",
            "function": {
                "name": "node__list",
                "description": "List nodes.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        project_id="project-1",
    )

    assert result["implementation"] == "responses_compact"
    assert result["items"] == [
        {"role": "user", "content": "当前任务"},
        {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque"},
    ]
    assert captured["instructions"] == "system"
    assert captured["tools"][0]["function"]["name"] == "node__list"
    assert captured["prompt_cache_key"].startswith("openreel:agent_loop:")
    assert result["usage"]["prompt_tokens"] == 100


@pytest.mark.asyncio
async def test_native_responses_compact_request_uses_public_sdk_contract(monkeypatch) -> None:
    import openai

    captured: dict = {}

    class FakeResponses:
        async def compact(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(id="cmp-response", output=[], usage=None)

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = FakeResponses()

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)

    await llm_service._native_responses_compact_request(
        {
            "model": "openai/gpt-5.4",
            "api_key": "sk-test",
            "api_base": "https://api.openai.com/v1",
            "provider_params": {"sdk_max_retries": 0},
        },
        input_items=[{"role": "user", "content": "当前任务"}],
        instructions="system",
        tools=[{
            "type": "function",
            "function": {
                "name": "node__list",
                "description": "List nodes.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        prompt_cache_key="cache-key",
    )

    assert captured["client"]["base_url"] == "https://api.openai.com/v1"
    assert captured["request"] == {
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": "当前任务"}],
        "instructions": "system",
        "extra_body": {
            "tools": [{
                "type": "function",
                "name": "node__list",
                "description": "List nodes.",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            }],
            "parallel_tool_calls": True,
        },
        "prompt_cache_key": "cache-key",
    }
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_native_compaction_omits_cache_key_when_provider_disables_cache(monkeypatch) -> None:
    captured: dict = {}

    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-5.4",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_key": "sk-test",
            "model_metadata": {"supports_prompt_cache": False},
        }

    async def fake_compact(cfg, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="cmp-response-1",
            output=[
                {"role": "user", "content": "当前任务"},
                {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque"},
            ],
            usage=None,
        )

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_native_responses_compact_request", fake_compact)

    await LLMService().compact_conversation(
        messages=[{"role": "user", "content": "当前任务"}],
        system="system",
        project_id="project-1",
    )

    assert captured["prompt_cache_key"] is None


@pytest.mark.asyncio
async def test_remote_compaction_with_multiple_compaction_items_uses_local_fallback(monkeypatch) -> None:
    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-5.4",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_key": "sk-test",
            "model_metadata": {"supports_responses_compact": True},
        }

    async def fake_compact(cfg, **kwargs):
        return SimpleNamespace(
            id="cmp-invalid",
            output=[
                {"type": "compaction", "id": "cmp-1", "encrypted_content": "one"},
                {"type": "compaction", "id": "cmp-2", "encrypted_content": "two"},
            ],
            usage=None,
        )

    captured = {}

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("本地检查点")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_native_responses_compact_request", fake_compact)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    result = await LLMService().compact_conversation(
        messages=[{"role": "user", "content": "当前任务"}],
        system="system",
        tools=[{"type": "function", "function": {"name": "node__list"}}],
        project_id="project-1",
    )

    assert result["implementation"] == "local_compact"
    assert result["remote_error"] == "LLMResponseStatusError"
    assert "tools" not in captured


@pytest.mark.asyncio
async def test_local_compaction_never_exposes_agent_tools(monkeypatch) -> None:
    async def fake_config(*args, **kwargs):
        return {
            "model": "test/model",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_key": None,
            "provider_params": {"max_retries": 0},
        }

    captured = {}

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("完成 A；下一步 B")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    await LLMService().compact_conversation(
        messages=[{"role": "user", "content": "当前任务"}],
        system="system",
        tools=[{"type": "function", "function": {"name": "node__list"}}],
        project_id="project-1",
    )

    assert "tools" not in captured


@pytest.mark.asyncio
async def test_local_compaction_drops_oldest_function_pair_on_context_error(monkeypatch) -> None:
    calls: list[list[dict]] = []

    async def fake_config(*args, **kwargs):
        return {
            "model": "test/model",
            "temperature": 0.0,
            "max_tokens": 100,
            "api_key": None,
            "provider_params": {"max_retries": 0},
        }

    async def fake_aresponses(**kwargs):
        calls.append(kwargs["input"])
        if len(calls) == 1:
            raise RuntimeError("prompt too long: context length exceeded")
        return _response("完成 A；下一步 B", prompt_tokens=20, completion_tokens=8)

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    result = await LLMService().compact_conversation(
        messages=[
            {"type": "function_call", "call_id": "c1", "name": "node__list", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "{}"},
            {"role": "user", "content": "当前任务"},
        ],
        system="system",
        project_id="project-1",
    )

    assert len(calls) == 2
    assert not any(item.get("type") == "function_call" for item in calls[1])
    assert result["implementation"] == "local_compact"
    assert result["items"][0] == {"role": "user", "content": "当前任务"}
    assert result["items"][-1]["content"].startswith(
        CODEX_SUMMARY_PREFIX
    )


@pytest.mark.asyncio
async def test_llm_generate_rejects_failed_responses_status(monkeypatch) -> None:
    async def fake_aresponses(**kwargs):
        return SimpleNamespace(
            id="resp-failed",
            status="failed",
            error={"code": "provider_error", "message": "upstream failed"},
            output=[],
            usage=None,
        )

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    with pytest.raises(llm_service.LLMResponseStatusError, match="status=failed"):
        await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_llm_generate_continues_truncated_text(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(
                "hello ",
                finish_reason="length",
                prompt_tokens=10,
                completion_tokens=5,
            )
        return _response(
            "world",
            finish_reason="stop",
            prompt_tokens=20,
            completion_tokens=4,
        )

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "hello world"
    assert result["finish_reason"] == "stop"
    assert result["continuation_count"] == 1
    assert result["continuation_exhausted"] is False
    assert result["usage"]["prompt_tokens"] == 30
    assert result["usage"]["completion_tokens"] == 9
    assert result["usage"]["total_tokens"] == 39
    assert result["usage"]["llm_calls"] == 2
    assert result["usage"]["active_input_tokens"] == 20
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_llm_generate_requires_complete_text_after_bounded_continuation(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_config(*args, **kwargs):
        return {
            **(await _fake_config()),
            "provider_params": {"max_continuations": 1},
        }

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        return _response(
            "part one" if calls["count"] == 1 else " part two",
            finish_reason="length",
        )

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    with pytest.raises(LLMOutputTruncatedError) as exc_info:
        await LLMService().generate(
            "text_generation",
            [{"role": "user", "content": "write long text"}],
            require_complete=True,
        )

    assert exc_info.value.partial_content == "part one part two"
    assert exc_info.value.continuation_count == 1
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_llm_generate_applies_explicit_output_limit(monkeypatch) -> None:
    captured: dict = {}

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    await LLMService().generate(
        "agent_loop",
        [{"role": "user", "content": "hi"}],
        max_tokens=12_000,
    )

    assert captured["max_output_tokens"] == 12_000
    assert captured["store"] is False


@pytest.mark.asyncio
async def test_generate_with_tools_does_not_continue_truncated_tool_arguments(monkeypatch) -> None:
    calls = {"count": 0}
    tool_call = {
        "id": "fc-1",
        "type": "function_call",
        "call_id": "call-1",
        "name": "node__create",
        "arguments": '{"project_id":"p"',
    }

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        return SimpleNamespace(
            id="resp-tool",
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output=[tool_call],
            usage=None,
        )

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    response = await LLMService().generate_with_tools(
        "agent_loop",
        [{"role": "user", "content": "save"}],
        tools=[],
    )

    assert calls["count"] == 1
    assert response.output == [tool_call]
    assert response._openreel_tool_call_truncated is True


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
    kwargs = llm_service._responses_kwargs(cfg)

    assert policy == {
        "request_timeout_seconds": 240.0,
        "max_retries": 1,
        "sdk_max_retries": 0,
        "retry_backoff_seconds": 1.25,
        "max_continuations": 0,
        "accept_backend_content": True,
        "include_reasoning_encrypted_content": True,
        "use_chat_completions_api": False,
        "supports_responses_websocket": True,
        "supports_responses_compact": True,
    }
    assert kwargs["timeout"] == 240.0
    assert kwargs["max_retries"] == 0
    assert kwargs["store"] is False
    assert kwargs["include"] == ["reasoning.encrypted_content"]


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

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        raise ConnectionError("downstream disconnected")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

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

    async def fake_aresponses(**kwargs):
        raise RelayResponseError()

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

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

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response("content already received", finish_reason="length")
        raise ConnectionError("continuation disconnected")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    result = await LLMService().generate("agent_loop", [{"role": "user", "content": "hi"}])

    assert result["content"] == "content already received"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_message_content_accepts_backend_text_parts(monkeypatch) -> None:
    async def fake_aresponses(**kwargs):
        return _response([
            {"type": "text", "text": "hello "},
            {"type": "output_text", "text": "world"},
        ])

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

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

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

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

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

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

    user_message = captured["input"][0]
    assert isinstance(user_message["content"], list)
    assert user_message["content"][1]["type"] == "input_image"
    assert user_message["content"][1]["image_url"].startswith("data:image/png")


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

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    await LLMService().generate_with_tools(
        "agent_loop",
        [{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10000,
    )

    assert captured["max_output_tokens"] == 10000


@pytest.mark.asyncio
async def test_generate_with_tools_sends_native_responses_contract(monkeypatch) -> None:
    captured: dict = {}

    async def fake_config(*args, **kwargs):
        return {
            "model": "openai/gpt-5.5",
            "temperature": 0.2,
            "max_tokens": 2048,
            "api_base": "https://responses.example.test/v1",
            "api_key": "test-key",
            "provider_params": {"use_chat_completions_api": False},
        }

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("done")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    await LLMService().generate_with_tools(
        "agent_loop",
        [{"role": "user", "content": "Read node 7."}],
        tools=[{
            "type": "function",
            "function": {
                "name": "node__get",
                "description": "Read a node.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        system="You are the OpenReel agent.",
    )

    assert "messages" not in captured
    assert "max_tokens" not in captured
    assert captured["instructions"] == "You are the OpenReel agent."
    assert captured["input"] == [{"role": "user", "content": "Read node 7."}]
    assert captured["tools"][0]["name"] == "node__get"
    assert "function" not in captured["tools"][0]
    assert captured["max_output_tokens"] == 2048
    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert "use_chat_completions_api" not in captured


@pytest.mark.asyncio
async def test_stream_with_tools_yields_typed_deltas_items_and_terminal_response(monkeypatch) -> None:
    captured: dict = {}
    final_response = SimpleNamespace(
        id="resp-stream",
        model="test/model",
        status="completed",
        incomplete_details=None,
        output=[
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
            {
                "id": "fc-1",
                "type": "function_call",
                "call_id": "call-1",
                "name": "node__get",
                "arguments": '{"node_id":"7"}',
            },
        ],
        usage=SimpleNamespace(input_tokens=20, output_tokens=8, total_tokens=28),
    )

    class FakeResponsesStream:
        def __init__(self):
            self._events = iter([
                {"type": "response.created", "response": {"id": "resp-stream"}},
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
                {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "Hel"},
                {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "lo"},
                {
                    "type": "response.output_item.done",
                    "item": final_response.output[0],
                },
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "fc-1",
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "node__get",
                        "arguments": "",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc-1",
                    "delta": '{"node_id":"7"}',
                },
                {
                    "type": "response.output_item.done",
                    "item": final_response.output[1],
                },
                {"type": "response.completed", "response": final_response},
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return FakeResponsesStream()

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    updates = [
        update
        async for update in LLMService().stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "Read node 7."}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "node__get",
                    "description": "Read a node.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
        )
    ]

    assert captured["stream"] is True
    assert captured["store"] is False
    assert captured["tools"][0]["name"] == "node__get"
    assert [update.delta for update in updates if update.kind == "text_delta"] == ["Hel", "lo"]
    assert len([update for update in updates if update.kind == "output_item_done"]) == 2
    terminal = updates[-1]
    assert terminal.kind == "terminal"
    assert terminal.response is final_response
    assert terminal.response._openreel_actual_model == "test/model"


@pytest.mark.asyncio
async def test_stream_with_tools_rejects_stream_closed_before_completed(monkeypatch) -> None:
    class TruncatedResponsesStream:
        def __init__(self):
            self._events = iter([
                {"type": "response.output_text.delta", "delta": "partial"},
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    async def fake_aresponses(**kwargs):
        return TruncatedResponsesStream()

    monkeypatch.setattr(llm_service, "_resolve_config", _fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    with pytest.raises(
        llm_service.LLMResponseStatusError,
        match="closed before response.completed",
    ):
        async for _ in LLMService().stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "hi"}],
            tools=[],
        ):
            pass


@pytest.mark.asyncio
async def test_stream_with_tools_retries_retryable_terminal_before_text(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_config(*args, **kwargs):
        return {
            **(await _fake_config()),
            "provider_params": {"max_retries": 1, "retry_backoff_seconds": 0},
        }

    class EventStream:
        def __init__(self, events):
            self._events = iter(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return EventStream([{
                "type": "response.failed",
                "response": {
                    "id": "resp-failed",
                    "status": "failed",
                    "output": [],
                    "error": {
                        "type": "service_unavailable",
                        "code": "upstream_unavailable",
                    },
                },
            }])
        return EventStream([{
            "type": "response.completed",
            "response": _response("recovered"),
        }])

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    updates = [
        update
        async for update in LLMService().stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "hi"}],
            tools=[],
        )
    ]

    assert calls["count"] == 2
    assert updates[-1].kind == "terminal"
    assert response_view(updates[-1].response).content == "recovered"


@pytest.mark.asyncio
async def test_stream_with_tools_does_not_retry_after_text_delta(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_config(*args, **kwargs):
        return {
            **(await _fake_config()),
            "provider_params": {"max_retries": 2, "retry_backoff_seconds": 0},
        }

    class FailedAfterTextStream:
        def __init__(self):
            self._events = iter([
                {"type": "response.output_text.delta", "delta": "visible"},
                {
                    "type": "response.failed",
                    "response": {
                        "id": "resp-failed-after-text",
                        "status": "failed",
                        "output": [],
                        "error": {"type": "service_unavailable"},
                    },
                },
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    async def fake_aresponses(**kwargs):
        calls["count"] += 1
        return FailedAfterTextStream()

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    deltas = []
    with pytest.raises(llm_service.LLMResponseStatusError, match="status=failed"):
        async for update in LLMService().stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "hi"}],
            tools=[],
        ):
            if update.kind == "text_delta":
                deltas.append(update.delta)

    assert calls["count"] == 1
    assert deltas == ["visible"]


def test_custom_openai_compatible_base_defaults_to_responses_bridge() -> None:
    policy = llm_service._llm_request_policy({
        "model": "openai/custom-model",
        "api_base": "https://relay.example.test/v1",
    })

    assert policy["use_chat_completions_api"] is True
    assert policy["supports_responses_websocket"] is False
    assert llm_service._responses_kwargs({
        "model": "openai/custom-model",
        "api_base": "https://relay.example.test/v1",
        "max_tokens": 100,
    })["use_chat_completions_api"] is True


def test_custom_openai_compatible_base_can_opt_into_native_responses() -> None:
    kwargs = llm_service._responses_kwargs({
        "model": "openai/custom-model",
        "api_base": "https://relay.example.test/v1",
        "max_tokens": 100,
        "provider_params": {"use_chat_completions_api": False},
    })

    assert "use_chat_completions_api" not in kwargs


def test_custom_native_responses_relay_can_declare_websocket_capability() -> None:
    policy = llm_service._llm_request_policy({
        "model": "openai/custom-model",
        "api_base": "https://relay.example.test/v1",
        "provider_params": {
            "use_chat_completions_api": False,
            "supports_responses_websocket": True,
        },
    })

    assert policy["use_chat_completions_api"] is False
    assert policy["supports_responses_websocket"] is True


@pytest.mark.parametrize(
    ("supports_prompt_cache", "expects_key"),
    [(True, True), (None, True), (False, False)],
)
@pytest.mark.asyncio
async def test_prompt_cache_setting_controls_request_field(
    monkeypatch,
    supports_prompt_cache,
    expects_key,
) -> None:
    captured = {}

    async def fake_config(*_args, **_kwargs):
        return {
            "model": "openai/gpt-5.6",
            "temperature": 0.2,
            "max_tokens": 100,
            "api_base": "https://api.openai.com/v1",
            "api_key": "test-key",
            "model_metadata": {
                "supports_prompt_cache": supports_prompt_cache,
            },
        }

    async def fake_aresponses(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service.litellm, "aresponses", fake_aresponses)

    await LLMService().generate(
        "agent_loop",
        [{"role": "user", "content": "hi"}],
        project_id="project-cache-setting",
    )

    if expects_key:
        assert captured["prompt_cache_key"].startswith("openreel:agent_loop:")
    else:
        assert "prompt_cache_key" not in captured


@pytest.mark.asyncio
async def test_responses_websocket_connect_uses_codex_headers(monkeypatch) -> None:
    from websockets.asyncio import client

    captured = {}
    socket = SimpleNamespace()

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return socket

    monkeypatch.setattr(client, "connect", fake_connect)

    result = await llm_service._connect_responses_websocket(
        "wss://api.openai.com/v1/responses",
        api_key="test-key",
        timeout=90,
        provider_params={},
        session_id="session-1",
        thread_id="thread-1",
        turn_state="turn-state-1",
    )

    headers = dict(captured["additional_headers"])
    assert result is socket
    assert headers["OpenAI-Beta"] == "responses_websockets=2026-02-06"
    assert headers["session-id"] == "session-1"
    assert headers["thread-id"] == "thread-1"
    assert headers["x-codex-turn-state"] == "turn-state-1"
    assert headers["x-client-request-id"]


@pytest.mark.asyncio
async def test_turn_session_reuses_websocket_with_incremental_tool_output(monkeypatch) -> None:
    function_call = {
        "id": "fc-1",
        "type": "function_call",
        "call_id": "call-1",
        "name": "project__get_state",
        "arguments": "{}",
    }
    first_response = {
        "id": "resp-1",
        "model": "gpt-5.6",
        "status": "completed",
        "output": [function_call],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    final_message = {
        "id": "msg-2",
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "完成"}],
    }
    second_response = {
        "id": "resp-2",
        "model": "gpt-5.6",
        "status": "completed",
        "output": [final_message],
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    }

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.events = []
            self.closed = False

        async def send(self, value: str):
            self.sent.append(json.loads(value))
            response = first_response if len(self.sent) == 1 else second_response
            done_item = function_call if len(self.sent) == 1 else final_message
            self.events.extend([
                json.dumps({"type": "response.created", "response": {"id": response["id"]}}),
                json.dumps({"type": "response.output_item.done", "item": done_item}),
                json.dumps({"type": "response.completed", "response": response}),
            ])

        async def recv(self):
            return self.events.pop(0)

        async def close(self):
            self.closed = True

    socket = FakeWebSocket()
    captured = {}

    async def fake_config(*_args, **_kwargs):
        return {
            "model": "openai/gpt-5.6",
            "temperature": 0.2,
            "max_tokens": 100,
            "api_base": "https://api.openai.com/v1",
            "api_key": "test-key",
            "provider_params": {"max_retries": 0},
            "model_metadata": {},
        }

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return socket

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_connect_responses_websocket", fake_connect)

    session = LLMService().new_turn_session()
    first_updates = [
        update
        async for update in session.stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "读取项目"}],
            tools=[],
        )
    ]
    tool_output = {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"ok":true}',
    }
    second_updates = [
        update
        async for update in session.stream_with_tools(
            "agent_loop",
            [
                {"role": "user", "content": "读取项目"},
                function_call,
                tool_output,
            ],
            tools=[],
        )
    ]
    await session.aclose()

    assert captured["url"] == "wss://api.openai.com/v1/responses"
    assert captured["kwargs"]["session_id"]
    assert captured["kwargs"]["thread_id"]
    assert socket.sent[0]["model"] == "gpt-5.6"
    assert socket.sent[0]["store"] is False
    assert "previous_response_id" not in socket.sent[0]
    assert socket.sent[1]["previous_response_id"] == "resp-1"
    assert socket.sent[1]["input"] == [tool_output]
    assert first_updates[-1].response["_openreel_transport_incremental"] is False
    assert second_updates[-1].response["_openreel_transport_incremental"] is True
    assert second_updates[-1].response["_openreel_transport_input_items_sent"] == 1
    assert socket.closed is True


@pytest.mark.asyncio
async def test_turn_sessions_never_reuse_websocket_state_across_user_turns(monkeypatch) -> None:
    first_message = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "第一轮"}],
    }
    second_message = {
        "id": "msg-2",
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "第二轮"}],
    }
    responses = iter([
        {
            "id": "resp-cross-1",
            "model": "gpt-5.6",
            "status": "completed",
            "output": [first_message],
        },
        {
            "id": "resp-cross-2",
            "model": "gpt-5.6",
            "status": "completed",
            "output": [second_message],
        },
    ])

    class FakeWebSocket:
        def __init__(self, response):
            self.response = response
            self.sent = []
            self.events = []
            self.closed = False

        async def send(self, value: str):
            self.sent.append(json.loads(value))
            self.events.append(json.dumps({
                "type": "response.completed",
                "response": self.response,
            }))

        async def recv(self):
            return self.events.pop(0)

        async def close(self):
            self.closed = True

    sockets = []
    calls = {"connect": 0}

    async def fake_config(*_args, **_kwargs):
        return {
            "model": "openai/gpt-5.6",
            "temperature": 0.2,
            "max_tokens": 100,
            "api_base": "https://api.openai.com/v1",
            "api_key": "test-key",
            "provider_params": {"max_retries": 0},
            "model_metadata": {},
        }

    async def fake_connect(*_args, **_kwargs):
        calls["connect"] += 1
        socket = FakeWebSocket(next(responses))
        sockets.append(socket)
        return socket

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_connect_responses_websocket", fake_connect)
    first_session = LLMService().new_turn_session(project_id="project-cross-turn")
    first_updates = [
        update
        async for update in first_session.stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "第一轮"}],
            tools=[],
            project_id="project-cross-turn",
        )
    ]
    await first_session.aclose()

    assert first_updates[-1].kind == "terminal"
    assert sockets[0].closed is True

    second_session = LLMService().new_turn_session(project_id="project-cross-turn")
    second_updates = [
        update
        async for update in second_session.stream_with_tools(
            "agent_loop",
            [
                {"role": "user", "content": "第一轮"},
                first_message,
                {"role": "user", "content": "第二轮"},
            ],
            tools=[],
            project_id="project-cross-turn",
        )
    ]
    await second_session.aclose()

    assert second_updates[-1].kind == "terminal"
    assert calls["connect"] == 2
    assert "previous_response_id" not in sockets[1].sent[0]
    assert len(sockets[1].sent[0]["input"]) == 3
    assert "第二轮" in json.dumps(sockets[1].sent[0]["input"], ensure_ascii=False)
    assert sockets[1].closed is True


@pytest.mark.asyncio
async def test_turn_session_reconnect_replays_current_turn_state(monkeypatch) -> None:
    response = {
        "id": "resp-reconnected",
        "model": "gpt-5.6",
        "status": "completed",
        "output": [],
    }

    class FailedWebSocket:
        response = SimpleNamespace(headers={"x-codex-turn-state": "turn-state-1"})

        async def send(self, _value: str):
            return None

        async def recv(self):
            raise ConnectionError("disconnected")

        async def close(self):
            return None

    class ReconnectedWebSocket:
        response = SimpleNamespace(headers={})

        def __init__(self):
            self.events = []

        async def send(self, _value: str):
            self.events.append(json.dumps({
                "type": "response.completed",
                "response": response,
            }))

        async def recv(self):
            return self.events.pop(0)

        async def close(self):
            return None

    sockets = [FailedWebSocket(), ReconnectedWebSocket()]
    connects = []

    async def fake_config(*_args, **_kwargs):
        return {
            "model": "openai/gpt-5.6",
            "temperature": 0.2,
            "max_tokens": 100,
            "api_base": "https://api.openai.com/v1",
            "api_key": "test-key",
            "provider_params": {
                "max_retries": 1,
                "retry_backoff_seconds": 0,
            },
            "model_metadata": {},
        }

    async def fake_connect(*_args, **kwargs):
        connects.append(kwargs)
        return sockets[len(connects) - 1]

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_connect_responses_websocket", fake_connect)

    session = LLMService().new_turn_session()
    updates = [
        update
        async for update in session.stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "重连"}],
            tools=[],
        )
    ]
    await session.aclose()

    assert updates[-1].kind == "terminal"
    assert len(connects) == 2
    assert connects[0]["turn_state"] == ""
    assert connects[1]["turn_state"] == "turn-state-1"


def test_turn_session_does_not_chain_when_full_context_changed() -> None:
    session = LLMService().new_turn_session()
    previous_user = {"role": "user", "content": "旧上下文"}
    previous_output = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "旧回答"}],
    }
    kwargs = {
        "model": "openai/gpt-5.6",
        "store": False,
        "input": [],
        "instructions": "stable",
    }
    session._last_response_id = "resp-old"
    session._last_request_input = [previous_user]
    session._last_response_items = [previous_output]
    session._last_request_fingerprint = session._request_fingerprint(
        llm_service._responses_websocket_body(kwargs)
    )

    changed_input = [{"role": "developer", "content": "compacted"}]
    body, incremental, reason = session._request_body(kwargs, changed_input)

    assert incremental is False
    assert reason == "full_context_changed"
    assert "previous_response_id" not in body
    assert body["input"] == changed_input


@pytest.mark.asyncio
async def test_turn_session_falls_back_to_http_once_for_websocket_failure(monkeypatch) -> None:
    calls = {"connect": 0, "http": 0}

    async def fake_config(*_args, **_kwargs):
        return {
            "model": "openai/gpt-5.6",
            "temperature": 0.2,
            "max_tokens": 100,
            "api_base": "https://api.openai.com/v1",
            "api_key": "test-key",
            "provider_params": {"max_retries": 0},
            "model_metadata": {},
        }

    async def failed_connect(*_args, **_kwargs):
        calls["connect"] += 1
        raise ConnectionError("unavailable")

    async def fake_http(self, cfg, **_kwargs):
        calls["http"] += 1
        yield llm_service.ResponseStreamUpdate(
            kind="terminal",
            event_type="response.completed",
            response=_response("fallback"),
        )

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_connect_responses_websocket", failed_connect)
    monkeypatch.setattr(LLMService, "_stream_with_tools_resolved", fake_http)

    session = LLMService().new_turn_session()
    responses = []
    for _ in range(2):
        updates = [
            update
            async for update in session.stream_with_tools(
                "agent_loop",
                [{"role": "user", "content": "继续"}],
                tools=[],
            )
        ]
        responses.append(updates[-1].response)
    await session.aclose()

    assert calls == {"connect": 1, "http": 2}
    assert all(response._openreel_transport == "responses_http_sse" for response in responses)
    assert all("websocket_" in response._openreel_transport_reuse_reason for response in responses)


@pytest.mark.asyncio
async def test_turn_session_never_replays_http_after_visible_websocket_text(monkeypatch) -> None:
    calls = {"http": 0}

    class FailingWebSocket:
        def __init__(self):
            self.recv_count = 0
            self.closed = False

        async def send(self, _value: str):
            return None

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({
                    "type": "response.output_text.delta",
                    "item_id": "msg-1",
                    "delta": "已经显示",
                })
            raise ConnectionError("stream interrupted")

        async def close(self):
            self.closed = True

    socket = FailingWebSocket()

    async def fake_config(*_args, **_kwargs):
        return {
            "model": "openai/gpt-5.6",
            "temperature": 0.2,
            "max_tokens": 100,
            "api_base": "https://api.openai.com/v1",
            "api_key": "test-key",
            "provider_params": {"max_retries": 0},
            "model_metadata": {},
        }

    async def fake_connect(*_args, **_kwargs):
        return socket

    async def fake_http(self, cfg, **_kwargs):
        calls["http"] += 1
        yield llm_service.ResponseStreamUpdate(
            kind="terminal",
            event_type="response.completed",
            response=_response("must not replay"),
        )

    monkeypatch.setattr(llm_service, "_resolve_config", fake_config)
    monkeypatch.setattr(llm_service, "_connect_responses_websocket", fake_connect)
    monkeypatch.setattr(LLMService, "_stream_with_tools_resolved", fake_http)

    session = LLMService().new_turn_session()
    deltas = []
    with pytest.raises(ConnectionError, match="stream interrupted"):
        async for update in session.stream_with_tools(
            "agent_loop",
            [{"role": "user", "content": "继续"}],
            tools=[],
        ):
            if update.kind == "text_delta":
                deltas.append(update.delta)
    await session.aclose()

    assert deltas == ["已经显示"]
    assert calls["http"] == 0
    assert socket.closed is True
