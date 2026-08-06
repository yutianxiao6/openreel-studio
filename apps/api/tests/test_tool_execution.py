from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.tool_execution import (
    ParallelToolCall,
    contiguous_parallel_read_batch,
    run_parallel_tool_calls,
    supports_parallel_read,
)
from app.agent import message_queue as mq
from app.agent import orchestrator as orchestrator_module
from app.agent.orchestrator import AgentOrchestrator


def _spec(*, read_only: bool, safe: bool, destructive: bool = False):
    return SimpleNamespace(
        is_read_only=read_only,
        is_concurrency_safe=safe,
        is_destructive=destructive,
        requires_confirmation=destructive,
    )


def _call(call_id: str, spec) -> ParallelToolCall:
    return ParallelToolCall(call_id, f"tool-{call_id}", {}, spec)


def test_parallel_read_batch_stops_at_write_barrier() -> None:
    read = _spec(read_only=True, safe=True)
    write = _spec(read_only=False, safe=False)
    calls = [_call("1", read), _call("2", read), _call("3", write), _call("4", read)]

    assert supports_parallel_read(read) is True
    assert supports_parallel_read(write) is False
    assert [call.call_id for call in contiguous_parallel_read_batch(calls, 0)] == ["1", "2"]
    assert contiguous_parallel_read_batch(calls, 2) == []


@pytest.mark.asyncio
async def test_parallel_tool_results_keep_model_order() -> None:
    read = _spec(read_only=True, safe=True)
    calls = [_call("slow", read), _call("fast", read)]
    active = 0
    max_active = 0

    async def invoke(call: ParallelToolCall):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02 if call.call_id == "slow" else 0.001)
        active -= 1
        return call.call_id

    results = await run_parallel_tool_calls(calls, invoke)

    assert max_active == 2
    assert [result.call_id for result in results] == ["slow", "fast"]
    assert [result.value for result in results] == ["slow", "fast"]


@pytest.mark.asyncio
async def test_orchestrator_runs_complete_safe_read_round_concurrently(monkeypatch) -> None:
    holder = {
        "state": {},
        "active": 0,
        "max_active": 0,
        "calls": [],
        "model_messages": [],
        "trace": [],
    }

    class FakeProjectService:
        async def get_project(self, _project_id: str):
            return SimpleNamespace(state_json=json.dumps(holder["state"]))

        async def get_project_state(self, _project_id: str):
            return dict(holder["state"])

        async def update_project_state(self, _project_id: str, patch: dict):
            holder["state"].update(patch)
            return SimpleNamespace(state_json=json.dumps(holder["state"]))

    class FakeTrace:
        def __init__(self, _project_id: str, _run_id: str):
            pass

        def emit(self, *args, **kwargs):
            holder["trace"].append((args, kwargs))

    class FakeLLMService:
        def __init__(self):
            self.count = 0

        async def generate_with_tools(self, *args, **kwargs):
            self.count += 1
            holder["model_messages"].append(list(kwargs["messages"]))
            if self.count == 1:
                output = [
                    {
                        "type": "function_call",
                        "call_id": "call-state",
                        "name": "project__get_state",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-tasks",
                        "name": "task__list",
                        "arguments": "{}",
                    },
                ]
            else:
                output = [{
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "读取完成。"}],
                }]
            return {
                "id": f"response-{self.count}",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                "model": "fake-model",
            }

        async def generate(self, *args, **kwargs):
            return {"content": "summary"}

    async def fake_registry_call(name: str, **kwargs):
        holder["calls"].append(name)
        holder["active"] += 1
        holder["max_active"] = max(holder["max_active"], holder["active"])
        await asyncio.sleep(0.02)
        holder["active"] -= 1
        return {"ok": True, "tool": name}

    async def fake_save_message(*_args, **_kwargs):
        return None

    async def fake_settings():
        return {"max_iterations": 3, "tool_call_budget": 0, "auto_archive": False}

    async def fake_compute_canvas_summary(_project_id: str):
        return {
            "total": 0,
            "by_type": {},
            "by_status": {},
            "by_surface": {},
            "node_refs": [],
        }

    async def fake_build_messages(_project_id: str, message: str, **_kwargs):
        return [{"role": "user", "content": message}]

    monkeypatch.setattr(orchestrator_module, "AgentTrace", FakeTrace)
    monkeypatch.setattr(orchestrator_module, "_load_agent_settings", fake_settings)
    monkeypatch.setattr(orchestrator_module.registry, "call", fake_registry_call)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.project_service = FakeProjectService()
    orchestrator.llm_service = FakeLLMService()
    orchestrator._save_message = fake_save_message
    orchestrator._compute_canvas_summary = fake_compute_canvas_summary
    orchestrator._build_messages = fake_build_messages
    orchestrator._maybe_compress_history = fake_save_message

    events = [
        event
        async for event in orchestrator._stream_one_turn("project-1", "读取状态和任务")
    ]

    assert holder["max_active"] == 2
    assert holder["calls"] == ["project.get_state", "task.list"]
    assert [event["tool"] for event in events if event.get("type") == "tool_done"] == [
        "project.get_state",
        "task.list",
    ]
    second_input = holder["model_messages"][1]
    outputs = [item for item in second_input if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["call-state", "call-tasks"]
    assert any(
        args and args[0] == "tool_parallel_batch_completed"
        for args, _kwargs in holder["trace"]
    )


@pytest.mark.asyncio
async def test_queued_user_turns_get_isolated_llm_sessions() -> None:
    project_id = "turn-session-isolation"
    await mq.pop_all(project_id)
    await mq.clear_cancel(project_id)
    sessions = []
    calls = 0

    class FakeTurnSession:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeLLMService:
        def new_turn_session(self):
            session = FakeTurnSession()
            sessions.append(session)
            return session

    async def fake_stream_one_turn(
        _project_id: str,
        message: str,
        _attachments=None,
        **kwargs,
    ):
        nonlocal calls
        calls += 1
        assert kwargs["llm_turn_session"] is sessions[-1]
        if calls == 1:
            await mq.enqueue(project_id, "第二个用户轮")
        yield {"type": "text_delta", "content": message}
        yield {"type": "done", "status": "completed"}

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.llm_service = FakeLLMService()
    orchestrator._stream_one_turn = fake_stream_one_turn
    try:
        events = [event async for event in orchestrator.stream(project_id, "第一个用户轮")]
    finally:
        await mq.pop_all(project_id)
        await mq.clear_cancel(project_id)

    assert calls == 2
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert [event["content"] for event in events if event.get("type") == "text_delta"] == [
        "第一个用户轮",
        "第二个用户轮",
    ]
