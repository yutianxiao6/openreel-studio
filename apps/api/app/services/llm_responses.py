"""Responses API request/response adapters used by the OpenReel LLM gateway.

The rest of the application may still build ordinary role/content messages.  This
module turns those messages into Responses input items and exposes one normalized
view over native Responses objects.  Keeping that boundary here lets providers
without a native Responses endpoint use LiteLLM's Responses-to-Chat bridge while
the OpenReel agent loop remains Responses-native.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable


RESPONSES_API_MODE = "responses"
_NATIVE_INPUT_ITEM_TYPES = {
    "computer_call_output",
    "function_call",
    "function_call_output",
    "item_reference",
    "message",
    "reasoning",
}


def as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        payload = value.model_dump(exclude_none=True)
    except TypeError:
        try:
            payload = value.model_dump()
        except Exception:
            payload = None
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return payload
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        payload = {key: item for key, item in attributes.items() if not key.startswith("_")}
        if payload:
            return payload
    payload = {}
    for key in (
        "id",
        "type",
        "call_id",
        "role",
        "status",
        "content",
        "output",
        "name",
        "arguments",
        "function",
        "tool_calls",
        "incomplete_details",
        "reason",
    ):
        if hasattr(value, key):
            payload[key] = getattr(value, key)
    return payload


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text: list[str] = []
    for part in content:
        if isinstance(part, str):
            text.append(part)
            continue
        item = as_mapping(part)
        value = item.get("text") or item.get("content")
        if isinstance(value, str):
            text.append(value)
    return "".join(text)


def _input_content_parts(content: Any, *, allow_image_input: bool) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts: list[dict[str, Any]] = []
    omitted_images = 0
    for raw_part in content:
        if isinstance(raw_part, str):
            if raw_part:
                parts.append({"type": "input_text", "text": raw_part})
            continue
        part = as_mapping(raw_part)
        part_type = str(part.get("type") or "")
        if part_type in {"text", "input_text", "output_text"}:
            text = str(part.get("text") or "")
            if text:
                parts.append({"type": "input_text", "text": text})
            continue
        if part_type in {"image_url", "input_image"}:
            image_url = part.get("image_url") or part.get("url")
            detail = part.get("detail")
            if isinstance(image_url, dict):
                detail = detail or image_url.get("detail")
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url:
                continue
            if not allow_image_input:
                omitted_images += 1
                continue
            image_part: dict[str, Any] = {
                "type": "input_image",
                "image_url": image_url,
            }
            if detail in {"auto", "low", "high"}:
                image_part["detail"] = detail
            parts.append(image_part)
            continue
        value = part.get("content") or part.get("text")
        if isinstance(value, str) and value:
            parts.append({"type": "input_text", "text": value})

    if omitted_images:
        parts.append({
            "type": "input_text",
            "text": (
                f"[{omitted_images} image input part(s) omitted: selected model endpoint "
                "accepts text-only input.]"
            ),
        })
    if not parts:
        return ""
    return parts


def _tool_call_mapping(raw_call: Any) -> dict[str, Any]:
    call = as_mapping(raw_call)
    function = as_mapping(call.get("function"))
    call_id = str(call.get("call_id") or call.get("id") or "")
    name = str(call.get("name") or function.get("name") or "")
    arguments = call.get("arguments")
    if arguments is None:
        arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False, default=str)
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _ordinary_message_items(
    message: dict[str, Any],
    *,
    allow_image_input: bool,
) -> list[dict[str, Any]]:
    role = str(message.get("role") or "user")
    if role == "tool":
        call_id = str(message.get("tool_call_id") or "")
        if not call_id:
            return []
        output = message.get("content")
        if not isinstance(output, (str, list)):
            output = json.dumps(output or {}, ensure_ascii=False, default=str)
        return [{"type": "function_call_output", "call_id": call_id, "output": output}]

    items: list[dict[str, Any]] = []
    content = message.get("content")
    if content not in (None, "", []):
        clean_message: dict[str, Any] = {
            "role": role,
            "content": _input_content_parts(content, allow_image_input=allow_image_input),
        }
        if message.get("name"):
            clean_message["name"] = str(message["name"])
        items.append(clean_message)

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        items.extend(_tool_call_mapping(call) for call in tool_calls)
    return items


def prepare_response_input(
    messages: list[dict[str, Any]],
    system_prompt: str | None,
    *,
    allow_image_input: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """Convert application messages to Responses input items and instructions."""

    instructions: list[str] = []
    if system_prompt and system_prompt.strip():
        instructions.append(system_prompt.strip())

    input_items: list[dict[str, Any]] = []
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        item_type = str(raw_message.get("type") or "")
        if item_type in _NATIVE_INPUT_ITEM_TYPES:
            # Native output items are intentionally replayed intact. In
            # particular this preserves encrypted reasoning when store=false.
            input_items.append(dict(raw_message))
            continue
        if raw_message.get("role") == "system":
            text = _content_text(raw_message.get("content"))
            if text.strip():
                instructions.append(text.strip())
            continue
        input_items.extend(
            _ordinary_message_items(raw_message, allow_image_input=allow_image_input)
        )

    return input_items, "\n\n".join(instructions) or None


def responses_tools(tools: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten Chat Completions function schemas to Responses tool schemas."""

    result: list[dict[str, Any]] = []
    for raw_tool in tools or []:
        if not isinstance(raw_tool, dict):
            continue
        function = as_mapping(raw_tool.get("function"))
        if raw_tool.get("type") != "function" and not function:
            result.append(dict(raw_tool))
            continue
        if function:
            tool = {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                "strict": bool(function.get("strict", False)),
            }
            for key in ("cache_control", "defer_loading", "allowed_callers", "input_examples"):
                if raw_tool.get(key) is not None:
                    tool[key] = raw_tool[key]
            result.append(tool)
            continue
        result.append(dict(raw_tool))
    return result


@dataclass(frozen=True)
class FunctionPayload:
    name: str
    arguments: str


@dataclass(frozen=True)
class FunctionCall:
    id: str
    function: FunctionPayload

    @property
    def call_id(self) -> str:
        return self.id


@dataclass(frozen=True)
class ResponseView:
    content: str
    tool_calls: tuple[FunctionCall, ...]
    finish_reason: str
    status: str
    response_id: str
    api_mode: str


def is_responses_response(response: Any) -> bool:
    if isinstance(response, dict):
        return isinstance(response.get("output"), list) and not response.get("choices")
    return hasattr(response, "output") and not hasattr(response, "choices")


def response_output_items(response: Any) -> list[dict[str, Any]]:
    if is_responses_response(response):
        raw_output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
        return [dict(item) for raw in (raw_output or []) if (item := as_mapping(raw))]

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, KeyError, TypeError):
        return []
    payload = as_mapping(message)
    if payload:
        payload.setdefault("role", "assistant")
        return [payload]
    return []


def _responses_content(response: Any) -> str:
    combined = getattr(response, "_openreel_combined_content", None)
    if isinstance(combined, str):
        return combined
    if isinstance(response, dict) and isinstance(response.get("_openreel_combined_content"), str):
        return response["_openreel_combined_content"]
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    texts: list[str] = []
    for item in response_output_items(response):
        if item.get("type") != "message":
            continue
        texts.append(_content_text(item.get("content")))
    return "".join(texts)


def _responses_finish_reason(response: Any) -> tuple[str, str]:
    mapping = response if isinstance(response, dict) else as_mapping(response)
    status = str(mapping.get("status") or getattr(response, "status", "") or "")
    if status == "incomplete":
        details = as_mapping(mapping.get("incomplete_details") or getattr(response, "incomplete_details", None))
        reason = str(details.get("reason") or "incomplete")
        return reason, status
    if status in {"failed", "cancelled"}:
        return status, status
    if any(item.get("type") == "function_call" for item in response_output_items(response)):
        return "tool_calls", status or "completed"
    return "stop", status or "completed"


def response_view(response: Any) -> ResponseView:
    if is_responses_response(response):
        calls: list[FunctionCall] = []
        for item in response_output_items(response):
            if item.get("type") != "function_call":
                continue
            call_id = str(item.get("call_id") or item.get("id") or "")
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False, default=str)
            calls.append(FunctionCall(
                id=call_id,
                function=FunctionPayload(
                    name=str(item.get("name") or ""),
                    arguments=arguments,
                ),
            ))
        finish_reason, status = _responses_finish_reason(response)
        response_id = str(
            (response.get("id") if isinstance(response, dict) else getattr(response, "id", "")) or ""
        )
        return ResponseView(
            content=_responses_content(response),
            tool_calls=tuple(calls),
            finish_reason=finish_reason,
            status=status,
            response_id=response_id,
            api_mode=RESPONSES_API_MODE,
        )

    try:
        choice = response.choices[0]
        message = choice.message
    except (AttributeError, IndexError, KeyError, TypeError):
        return ResponseView("", (), "", "", "", "unknown")
    calls = []
    for index, raw_call in enumerate(list(getattr(message, "tool_calls", None) or [])):
        call = _tool_call_mapping(raw_call)
        calls.append(FunctionCall(
            id=str(call.get("call_id") or f"call-{index}"),
            function=FunctionPayload(
                name=str(call.get("name") or ""),
                arguments=str(call.get("arguments") or ""),
            ),
        ))
    return ResponseView(
        content=_content_text(getattr(message, "content", None)),
        tool_calls=tuple(calls),
        finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        status="completed",
        response_id=str(getattr(response, "id", "") or ""),
        api_mode="chat_completions_compat",
    )


def replay_output_items(
    response: Any,
    *,
    invalid_call_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return model output suitable for the next Responses input turn."""

    items = response_output_items(response)
    invalid_call_ids = invalid_call_ids or set()
    if not invalid_call_ids:
        return items
    sanitized: list[dict[str, Any]] = []
    for item in items:
        clean = dict(item)
        if clean.get("type") == "function_call":
            call_id = str(clean.get("call_id") or clean.get("id") or "")
            if call_id in invalid_call_ids:
                clean["arguments"] = "{}"
        elif clean.get("role") == "assistant" and isinstance(clean.get("tool_calls"), list):
            calls = []
            for raw_call in clean["tool_calls"]:
                call = dict(raw_call) if isinstance(raw_call, dict) else as_mapping(raw_call)
                if str(call.get("id") or "") in invalid_call_ids:
                    function = dict(call.get("function") or {})
                    function["arguments"] = "{}"
                    call["function"] = function
                calls.append(call)
            clean["tool_calls"] = calls
            clean["content"] = ""
        sanitized.append(clean)
    return sanitized


def function_call_output_item(call_id: str, output: Any) -> dict[str, Any]:
    if not isinstance(output, (str, list)):
        output = json.dumps(output or {}, ensure_ascii=False, default=str)
    return {
        "type": "function_call_output",
        "call_id": str(call_id),
        "output": output,
    }


def stream_text_delta(event: Any) -> str:
    payload = event if isinstance(event, dict) else as_mapping(event)
    if payload.get("type") == "response.output_text.delta":
        delta = payload.get("delta")
        return delta if isinstance(delta, str) else ""
    # Compatibility for custom test doubles and old LiteLLM stream wrappers.
    try:
        delta = event.choices[0].delta.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    return delta if isinstance(delta, str) else ""
