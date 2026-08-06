from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

from app.services.llm_responses import (
    function_call_output_item,
    prepare_response_input,
    replay_output_items,
    response_stream_update,
    response_view,
    responses_tools,
    stream_text_delta,
)


def test_prepare_response_input_converts_messages_and_tool_rounds() -> None:
    reasoning = {
        "id": "rs-1",
        "type": "reasoning",
        "encrypted_content": "opaque-reasoning",
    }
    input_items, instructions = prepare_response_input(
        [
            {"role": "system", "content": "Additional stable rule."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            },
            {
                "role": "assistant",
                "content": "I will inspect it.",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "vision__view_image", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
            reasoning,
        ],
        "Primary instructions.",
    )

    assert instructions == "Primary instructions.\n\nAdditional stable rule."
    assert input_items[0]["role"] == "user"
    assert input_items[0]["content"] == [
        {"type": "input_text", "text": "Inspect this."},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]
    assert input_items[1] == {"role": "assistant", "content": "I will inspect it."}
    assert input_items[2] == {
        "type": "function_call",
        "call_id": "call-1",
        "name": "vision__view_image",
        "arguments": "{}",
    }
    assert input_items[3] == function_call_output_item("call-1", '{"ok":true}')
    assert input_items[4] == reasoning


def test_responses_tools_flattens_function_schema() -> None:
    tools = responses_tools([{
        "type": "function",
        "function": {
            "name": "node__get",
            "description": "Read a node.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
            "strict": True,
        },
    }])

    assert tools == [{
        "type": "function",
        "name": "node__get",
        "description": "Read a node.",
        "parameters": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
        "strict": True,
    }]


def test_response_view_and_replay_keep_typed_output_items() -> None:
    response = SimpleNamespace(
        id="resp-1",
        status="completed",
        incomplete_details=None,
        output=[
            {"id": "rs-1", "type": "reasoning", "encrypted_content": "opaque"},
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Checking."}],
            },
            {
                "id": "fc-1",
                "type": "function_call",
                "call_id": "call-1",
                "name": "node__get",
                "arguments": '{"node_id":"7"}',
            },
        ],
    )

    view = response_view(response)
    assert view.api_mode == "responses"
    assert view.response_id == "resp-1"
    assert view.finish_reason == "tool_calls"
    assert view.content == "Checking."
    assert view.tool_calls[0].id == "call-1"
    assert view.tool_calls[0].function.name == "node__get"
    assert replay_output_items(response) == response.output


def test_incomplete_response_maps_max_output_reason() -> None:
    view = response_view(SimpleNamespace(
        id="resp-incomplete",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[],
    ))

    assert view.finish_reason == "max_output_tokens"
    assert view.status == "incomplete"


def test_stream_text_delta_reads_typed_responses_event() -> None:
    assert stream_text_delta({
        "type": "response.output_text.delta",
        "delta": "hello",
    }) == "hello"
    assert stream_text_delta({"type": "response.completed"}) == ""


def test_response_stream_update_preserves_codex_style_event_boundaries() -> None:
    added = response_stream_update({
        "type": "response.output_item.added",
        "item": {
            "id": "fc-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "node__get",
            "arguments": "",
        },
    })
    arguments = response_stream_update({
        "type": "response.function_call_arguments.delta",
        "item_id": "fc-1",
        "delta": '{"node_id":',
    })
    done = response_stream_update({
        "type": "response.output_item.done",
        "item": {
            "id": "fc-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "node__get",
            "arguments": '{"node_id":"7"}',
        },
    })

    assert added is not None and added.kind == "output_item_added"
    assert added.call_id == "call-1"
    assert arguments is not None and arguments.kind == "tool_call_input_delta"
    assert arguments.delta == '{"node_id":'
    assert done is not None and done.kind == "output_item_done"
    assert done.item is not None
    assert done.item["arguments"] == '{"node_id":"7"}'


def test_response_stream_update_accepts_litellm_string_enum_event_types() -> None:
    class EventType(str, Enum):
        COMPLETED = "response.completed"

    response = SimpleNamespace(id="resp-enum", status="completed", output=[])
    update = response_stream_update({
        "type": EventType.COMPLETED,
        "response": response,
    })

    assert update is not None
    assert update.kind == "terminal"
    assert update.event_type == "response.completed"
    assert update.response is response
