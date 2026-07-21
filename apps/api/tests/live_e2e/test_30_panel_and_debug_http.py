from __future__ import annotations

from typing import Any, Awaitable, Callable

import httpx
import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.live_e2e]


def _done(events: list[dict[str, Any]]) -> None:
    done = [event for event in events if event.get("type") == "done"]
    assert done, [event.get("type") for event in events]
    assert done[-1].get("status") == "completed"


def _tool_names(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(event.get("tool"))
        for event in events
        if event.get("type") in {"tool_start", "tool_done"} and event.get("tool")
    ]


async def test_node_detail_and_debug_trace_use_web_endpoints(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool_request: Callable[..., Awaitable[Any]],
) -> None:
    create_events = await send_chat_request(
        api_client,
        project_id,
        (
            "创建一个文本节点，标题叫「Live E2E 测试剧」。"
            "直接创建节点，然后运行节点生成文字大纲。"
            "不要生成图片，不要生成视频。"
        ),
    )

    _done(create_events)
    names = _tool_names(create_events)
    assert "node.get_creation_guide" not in names
    assert "node.create" in names
    assert "node.run" in names

    create_actions = [
        event
        for event in create_events
        if event.get("type") == "canvas_action" and event.get("action") == "create_node"
    ]
    assert create_actions
    node_id = str(create_actions[-1]["payload"]["id"])

    detail = await call_tool_request(api_client, "node.get", {"node_id": node_id})
    assert detail["id"] == node_id
    assert detail["type"] == "text"
    assert detail["status"] == "completed"
    assert detail["surface"] == "project_panel"
    assert detail["input"] is not None
    assert detail["output"] is not None

    trace_list = await api_client.get(f"/api/agent/debug/{project_id}/traces?source=auto&limit=20")
    assert trace_list.status_code == 200, trace_list.text
    traces = trace_list.json()["traces"]
    assert traces
    assert traces[0]["event_count"] > 0
    run_id = traces[0]["run_id"]

    trace_detail = await api_client.get(f"/api/agent/debug/{project_id}/traces/{run_id}?source=auto&limit=200")
    assert trace_detail.status_code == 200, trace_detail.text
    events = trace_detail.json()["events"]
    assert any(event.get("event") == "run_start" for event in events)
    assert any(event.get("event") == "tool_result" for event in events)
