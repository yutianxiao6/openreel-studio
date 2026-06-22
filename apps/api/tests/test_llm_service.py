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


@pytest.mark.asyncio
async def test_deepseek_generate_with_tools_downgrades_image_parts_to_text(monkeypatch) -> None:
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

    user_message = captured["messages"][1]
    assert user_message["role"] == "user"
    assert isinstance(user_message["content"], str)
    assert "Visual context retained." in user_message["content"]
    assert "image_url part(s) omitted" in user_message["content"]
    assert "data:image/" not in user_message["content"]


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
