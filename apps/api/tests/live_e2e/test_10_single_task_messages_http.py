from __future__ import annotations

from typing import Any, Awaitable, Callable

import httpx
import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.live_e2e]


def _events(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == event_type]


def _tool_names(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(event.get("tool"))
        for event in events
        if event.get("type") in {"tool_start", "tool_done"} and event.get("tool")
    ]


def _done(events: list[dict[str, Any]]) -> None:
    done = _events(events, "done")
    assert done, [event.get("type") for event in events]
    assert done[-1].get("status") == "completed"


def _no_contract_error(events: list[dict[str, Any]]) -> None:
    assert not [
        event
        for event in events
        if event.get("type") == "error"
        and "SSE event contract error" in str(event.get("message") or "")
    ]


async def test_web_chat_create_query_and_delete_single_node(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    project_nodes_request: Callable[..., Awaitable[dict[str, Any]]],
    project_state_request: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    create_events = await send_chat_request(
        api_client,
        project_id,
        (
            "创建一个测试用的文本节点，标题叫「Live E2E 单节点剧」。"
            "直接创建节点，然后运行这个节点生成文字大纲。"
            "不要生成图片，不要生成视频。"
        ),
    )

    _done(create_events)
    _no_contract_error(create_events)
    names = _tool_names(create_events)
    assert "node.get_creation_guide" not in names, create_events
    assert "node.create" in names, create_events
    assert "node.run" in names, create_events
    assert any(event.get("type") == "agent_round" for event in create_events)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "create_node" for event in create_events)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "update_node" for event in create_events)

    nodes_payload = await project_nodes_request(api_client, project_id)
    nodes = nodes_payload["nodes"]
    assert nodes
    created = next(node for node in nodes if node["type"] == "text")
    assert created["status"] == "completed"
    assert "draft_canvas" in str(created.get("model_config_json") or "")

    query_events = await send_chat_request(api_client, project_id, "查询当前画布上有哪些节点。")

    _done(query_events)
    _no_contract_error(query_events)
    query_tools = _tool_names(query_events)
    assert "node.list" in query_tools or "project.get_state" in query_tools
    assert not any(
        event.get("type") == "canvas_action" and event.get("action") == "create_node"
        for event in query_events
    )

    delete_events = await send_chat_request(
        api_client,
        project_id,
        "删除刚才创建的 Live E2E 单节点剧测试节点，只删除这个测试节点。",
    )

    _done(delete_events)
    _no_contract_error(delete_events)
    delete_tools = _tool_names(delete_events)
    assert "canvas.delete" in delete_tools
    confirm_events = _events(delete_events, "confirm_required")
    assert confirm_events
    assert confirm_events[-1].get("action") == "canvas.delete"
    assert not any(event.get("type") == "canvas_action" and event.get("action") == "delete_node" for event in delete_events)
    state = await project_state_request(api_client, project_id)
    pending_tool = state.get("_pending_tool_confirm") if isinstance(state, dict) else None
    assert isinstance(pending_tool, dict)
    assert pending_tool.get("target") == "canvas.delete"
    assert (pending_tool.get("input") or {}).get("scope") == "selected"
    assert (pending_tool.get("input") or {}).get("node_ids") == [created["id"]]

    confirm_delete_events = await send_chat_request(
        api_client,
        project_id,
        "确认删除节点",
        decision_inputs={
            "kind": "confirmation",
            "target": "canvas.delete",
            "action": "confirm",
            "values": {"target": "canvas.delete", "decision": "confirm"},
        },
    )

    _done(confirm_delete_events)
    _no_contract_error(confirm_delete_events)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "delete_node" for event in confirm_delete_events)
    state = await project_state_request(api_client, project_id)
    assert not state.get("_pending_tool_confirm")


async def test_web_chat_full_reset_natural_language_needs_only_one_confirmation(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    project_state_request: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    first = await send_chat_request(api_client, project_id, "重置整个项目")

    _done(first)
    _no_contract_error(first)
    assert any(event.get("type") == "confirm_required" for event in first)
    state = await project_state_request(api_client, project_id)
    assert state["_pending_reset_confirm"]["scope"] == "full"

    second = await send_chat_request(api_client, project_id, "确定")

    _done(second)
    _no_contract_error(second)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "clear_all" for event in second)
    assert not any(event.get("type") == "confirm_required" for event in second)
    state = await project_state_request(api_client, project_id)
    assert not state.get("_pending_reset_confirm")
