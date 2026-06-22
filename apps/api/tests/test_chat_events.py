import json

import pytest
from pydantic import ValidationError

from app.agent import parallel_executor
from app.agent.parallel_executor import execute_parallel
from app.agent import slash_commands
from app.agent.slash_commands import slash_command_events
from app.api.chat_events import event_to_sse, normalize_chat_event, validate_chat_event
from app.api.routes_chat import _to_sse
from app.mcp_tools.registry import registry


def test_proposed_plan_event_is_typed() -> None:
    event = normalize_chat_event(
        {
            "type": "proposed_plan",
            "project_id": "project-1",
            "plan": {"id": "plan-1", "sections": [{"type": "markdown", "content": "检查节点"}]},
        }
    )

    assert event == {
        "type": "proposed_plan",
        "project_id": "project-1",
        "plan": {"id": "plan-1", "sections": [{"type": "markdown", "content": "检查节点"}]},
    }


def test_proposed_plan_rejects_missing_plan() -> None:
    with pytest.raises(ValidationError):
        validate_chat_event(
            {
                "type": "proposed_plan",
                "project_id": "project-1",
            }
        )


@pytest.mark.asyncio
async def test_legacy_plan_action_is_rejected_before_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    streamed_to_agent = False

    class Orchestrator:
        async def stream(self, **_: object):
            nonlocal streamed_to_agent
            streamed_to_agent = True
            yield {"type": "agent_round"}

    async def fake_save_message(*_: object, **__: object) -> None:
        return None

    async def fake_emit_text(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(slash_commands, "_save_message", fake_save_message)
    monkeypatch.setattr(slash_commands, "_emit_text", fake_emit_text)
    monkeypatch.setattr(slash_commands, "_emit_control_plane_event", lambda *args, **kwargs: None)

    events = [
        event
        async for event in slash_command_events(
            "project-1",
            "/plan approve",
            orchestrator=Orchestrator(),
        )
    ]

    assert streamed_to_agent is False
    slash = next(event for event in events if event.get("type") == "slash_command")
    assert slash["command"] == "plan"
    assert slash["action"] == "approve"
    assert slash["ok"] is False
    assert slash["error"] == "legacy_plan_action_removed"
    assert events[-1] == {"type": "done", "status": "failed"}


def test_unknown_chat_event_remains_compatible() -> None:
    event = normalize_chat_event({"type": "custom_event", "value": 1})

    assert event == {"type": "custom_event", "value": 1}


def test_project_and_store_events_are_typed() -> None:
    assert normalize_chat_event({"type": "subscribed", "project_id": "project-1"}) == {
        "type": "subscribed",
        "project_id": "project-1",
    }
    assert normalize_chat_event({"type": "merged_messages", "count": 2}) == {
        "type": "merged_messages",
        "count": 2,
    }
    assert normalize_chat_event({"type": "queued", "queued_count": 1, "error": "busy"}) == {
        "type": "queued",
        "queued_count": 1,
        "error": "busy",
    }
    assert normalize_chat_event(
        {
            "type": "project_reset",
            "project_id": "project-1",
            "scope": "full",
            "title": "未命名项目",
            "cleared_all": True,
            "message": "已重置",
        }
    ) == {
        "type": "project_reset",
        "project_id": "project-1",
        "scope": "full",
        "title": "未命名项目",
        "cleared_all": True,
        "message": "已重置",
    }
    assert normalize_chat_event(
        {
            "type": "doctor_result",
            "ok": True,
            "project_id": "project-1",
            "feature_flags": {"total": 2, "enabled": 1},
        }
    ) == {
        "type": "doctor_result",
        "ok": True,
        "project_id": "project-1",
        "feature_flags": {"total": 2, "enabled": 1},
    }


def test_token_usage_event_is_typed() -> None:
    event = normalize_chat_event(
        {
            "type": "token_usage",
            "project_id": "project-1",
            "run_id": "run-1",
            "round": 2,
            "phase": "agent_loop",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cached_prompt_tokens": 25,
                "cache_hit_rate": 0.25,
                "latest_call_tokens": {"total_tokens": 120},
                "latest_call_context": {"context_remaining_tokens": 90_000},
            },
            "run_totals": {"total_tokens": 120, "llm_calls": 1},
            "session_totals": {"total_tokens": 240, "llm_calls": 2},
            "latest_call_tokens": {"total_tokens": 120},
            "latest_call_context": {"context_remaining_tokens": 90_000},
            "run_cumulative_tokens": {"total_tokens": 120, "llm_calls": 1},
            "session_cumulative_tokens": {"total_tokens": 240, "llm_calls": 2},
            "run_context_peak": {"context_remaining_tokens": 90_000},
            "session_context_peak": {"context_remaining_tokens": 80_000},
        }
    )

    assert event == {
        "type": "token_usage",
        "project_id": "project-1",
        "run_id": "run-1",
        "round": 2,
        "phase": "agent_loop",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_prompt_tokens": 25,
            "cache_hit_rate": 0.25,
            "latest_call_tokens": {"total_tokens": 120},
            "latest_call_context": {"context_remaining_tokens": 90_000},
        },
        "run_totals": {"total_tokens": 120, "llm_calls": 1},
        "session_totals": {"total_tokens": 240, "llm_calls": 2},
        "latest_call_tokens": {"total_tokens": 120},
        "latest_call_context": {"context_remaining_tokens": 90_000},
        "run_cumulative_tokens": {"total_tokens": 120, "llm_calls": 1},
        "session_cumulative_tokens": {"total_tokens": 240, "llm_calls": 2},
        "run_context_peak": {"context_remaining_tokens": 90_000},
        "session_context_peak": {"context_remaining_tokens": 80_000},
    }


def test_blueprint_events_are_typed() -> None:
    event = normalize_chat_event(
        {
            "type": "blueprint_section_completed",
            "project_id": "project-1",
            "section_id": "segment_breakdown",
            "title": "分段剧情",
            "status": "completed",
            "summary_text": "分段剧情已生成。",
            "display_blocks": [{"type": "paragraph", "text": "第一段雨夜交锋。"}],
            "blueprint_ref": {"id": "bp-1", "version": 1},
            "debug_json_path": "data/projects/project-1/blueprint_draft.json",
        }
    )

    assert event == {
        "type": "blueprint_section_completed",
        "project_id": "project-1",
        "section_id": "segment_breakdown",
        "title": "分段剧情",
        "status": "completed",
        "summary_text": "分段剧情已生成。",
        "display_blocks": [{"type": "paragraph", "text": "第一段雨夜交锋。"}],
        "blueprint_ref": {"id": "bp-1", "version": 1},
        "debug_json_path": "data/projects/project-1/blueprint_draft.json",
    }


def test_blueprint_tree_events_are_typed() -> None:
    event = normalize_chat_event(
        {
            "type": "blueprint_tree_changed",
            "project_id": "project-1",
            "tree_version": 7,
            "action": "update_node",
            "node_id": "seg-1",
            "patch": {"status": "rendering"},
        }
    )

    assert event == {
        "type": "blueprint_tree_changed",
        "project_id": "project-1",
        "tree_version": 7,
        "action": "update_node",
        "node_id": "seg-1",
        "patch": {"status": "rendering"},
    }

    replace_event = normalize_chat_event(
        {
            "type": "blueprint_tree_changed",
            "project_id": "project-1",
            "tree_version": 8,
            "action": "replace_tree",
            "node_id": "root",
            "patch": {"tree_summary": {"node_count": 5}},
        }
    )

    assert replace_event["action"] == "replace_tree"
    assert replace_event["patch"]["tree_summary"]["node_count"] == 5


def test_tool_done_event_keeps_tool_output_envelope() -> None:
    event = normalize_chat_event(
        {
            "type": "tool_done",
            "tool": "node.run",
            "round": 1,
            "result": {"ok": True, "node_id": "node-1"},
            "tool_output": {
                "version": "tool_output_v1",
                "summary": {"ok": True},
                "compacted": False,
                "artifact_path": None,
                "raw_result_chars": 32,
                "model_visible_chars": 32,
            },
        }
    )

    assert event["tool_output"]["version"] == "tool_output_v1"
    assert event["tool_output"]["compacted"] is False
    assert event["result"]["node_id"] == "node-1"


def test_interaction_input_event_preserves_structured_payload() -> None:
    event = normalize_chat_event(
        {
            "type": "interaction_input_requested",
            "project_id": "project-1",
            "status": "awaiting_user",
            "summary_text": "请补充视频主题、风格和类型。",
            "intake": {
                "purpose": "video_blueprint_intake",
                "stage": "basic",
                "title": "补充蓝图基础信息",
                "questions": [
                    {
                        "id": "topic",
                        "header": "主题",
                        "question": "视频主题或核心事件？",
                        "options": [
                            {"label": "模型发挥", "description": "由模型规划"},
                            {"label": "沿用当前描述", "description": "使用本轮描述"},
                        ],
                    }
                ],
            },
        }
    )

    assert event["type"] == "interaction_input_requested"
    assert event["intake"]["purpose"] == "video_blueprint_intake"
    assert event["intake"]["stage"] == "basic"
    assert "presentation" not in event["intake"]
    assert event["intake"]["questions"][0]["id"] == "topic"


def test_parallel_events_are_typed() -> None:
    assert normalize_chat_event(
        {
            "type": "parallel_start",
            "total_steps": 2,
            "waves": 1,
            "project_id": "project-1",
        }
    ) == {
        "type": "parallel_start",
        "total_steps": 2,
        "waves": 1,
        "project_id": "project-1",
    }
    assert normalize_chat_event(
        {
            "type": "step_completed",
            "step_index": 0,
            "tool": "tmp.echo",
            "title": "测试",
            "result": {"ok": True},
            "progress": "1/1",
        }
    ) == {
        "type": "step_completed",
        "step_index": 0,
        "tool": "tmp.echo",
        "title": "测试",
        "result": {"ok": True},
        "progress": "1/1",
    }
    assert normalize_chat_event({"type": "step_failed", "error": "boom"}) == {
        "type": "step_failed",
        "error": "boom",
    }
    assert normalize_chat_event({"type": "parallel_done", "completed": 1, "total": 1}) == {
        "type": "parallel_done",
        "completed": 1,
        "total": 1,
    }


def test_parallel_step_completed_rejects_missing_tool() -> None:
    with pytest.raises(ValidationError):
        validate_chat_event(
            {
                "type": "step_completed",
                "step_index": 0,
            }
        )


def test_event_to_sse_serializes_normalized_json() -> None:
    chunk = event_to_sse({"type": "text_delta", "content": "你好"})

    assert chunk.startswith("data: ")
    assert chunk.endswith("\n\n")
    payload = json.loads(chunk.removeprefix("data: ").strip())
    assert payload == {"type": "text_delta", "content": "你好"}


@pytest.mark.asyncio
async def test_to_sse_converts_contract_errors_to_error_event() -> None:
    async def source():
        yield {
            "type": "proposed_plan",
            "project_id": "project-1",
        }

    chunks = [chunk async for chunk in _to_sse(source())]
    payload = json.loads(chunks[0].removeprefix("data: ").strip())

    assert payload["type"] == "error"
    assert "SSE event contract error: proposed_plan" in payload["message"]


@pytest.mark.asyncio
async def test_to_sse_splits_large_text_delta(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes_chat.SSE_TEXT_DELTA_DELAY_SECONDS", 0)
    content = "x" * 180

    async def source():
        yield {"type": "text_delta", "content": content}

    chunks = [chunk async for chunk in _to_sse(source())]
    payloads = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]

    assert len(payloads) > 1
    assert all(payload["type"] == "text_delta" for payload in payloads)
    assert all(len(payload["content"]) <= 56 for payload in payloads)
    assert "".join(payload["content"] for payload in payloads) == content


@pytest.mark.asyncio
async def test_to_sse_mirrors_sanitized_event_summaries(monkeypatch) -> None:
    emitted = []

    class FakeEventStream:
        def emit(self, event_type: str, **kwargs):
            emitted.append((event_type, kwargs))
            return {"type": event_type, **kwargs}

    monkeypatch.setattr("app.api.routes_chat.event_stream", FakeEventStream())

    async def source():
        yield {"type": "text_delta", "content": "不要把这段正文写入生命周期事件"}
        yield {
            "type": "tool_done",
            "tool": "node.run",
            "round": 1,
            "result": {"large": "SECRET_RESULT_BODY"},
        }

    chunks = [
        chunk
        async for chunk in _to_sse(source(), project_id="project-1", stream_kind="chat")
    ]

    assert len(chunks) == 2
    mirrors = [kwargs for event_type, kwargs in emitted if event_type == "sse_event"]
    assert len(mirrors) == 2
    first = mirrors[0]["data"]
    assert first["protocol"] == "chat_sse"
    assert first["protocol_reason"] == "normalized SSE event emitted to frontend"
    assert first["stream_kind"] == "chat"
    assert first["type"] == "text_delta"
    assert first["content_len"] == len("不要把这段正文写入生命周期事件")
    assert "content" not in first
    second = mirrors[1]["data"]
    assert second == {
        "protocol": "chat_sse",
        "protocol_reason": "normalized SSE event emitted to frontend",
        "stream_kind": "chat",
        "type": "tool_done",
        "round": 1,
        "tool": "node.run",
    }
    assert "SECRET_RESULT_BODY" not in json.dumps(mirrors, ensure_ascii=False)


@pytest.mark.asyncio
async def test_execute_parallel_yields_typed_events(monkeypatch) -> None:
    async def fake_tool(project_id: str, value: str) -> dict:
        return {"ok": True, "project_id": project_id, "value": value}

    monkeypatch.setattr(parallel_executor.event_stream, "emit", lambda *args, **kwargs: {})
    registry.register(
        "tmp.parallel_echo",
        fake_tool,
        description="Temporary parallel echo tool",
    )
    try:
        events = [
            event
            async for event in execute_parallel(
                [
                    {
                        "tool": "tmp.parallel_echo",
                        "title": "并行测试",
                        "input": {"value": "hello"},
                    }
                ],
                "project-1",
            )
        ]
    finally:
        registry.unregister("tmp.parallel_echo")

    assert events[0] == {
        "type": "parallel_start",
        "total_steps": 1,
        "waves": 1,
        "project_id": "project-1",
    }
    assert events[1]["type"] == "step_completed"
    assert events[1]["tool"] == "tmp.parallel_echo"
    assert events[1]["progress"] == "1/1"
    assert events[-1] == {"type": "parallel_done", "completed": 1, "total": 1}
