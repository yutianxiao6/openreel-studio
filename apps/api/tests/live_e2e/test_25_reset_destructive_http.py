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


def _done(events: list[dict[str, Any]], status: str = "completed") -> None:
    done = _events(events, "done")
    assert done, [event.get("type") for event in events]
    assert done[-1].get("status") == status


def _no_contract_error(events: list[dict[str, Any]]) -> None:
    assert not [
        event
        for event in events
        if event.get("type") == "error"
        and "SSE event contract error" in str(event.get("message") or "")
    ]


async def _seed_text_node(
    api_client: httpx.AsyncClient,
    project_id: str,
    call_tool_request: Callable[..., Awaitable[Any]],
    *,
    name: str,
) -> str:
    created = await call_tool_request(
        api_client,
        "node.create",
        {
            "project_id": project_id,
            "type": "text",
            "fields": {
                "title": name,
                "content": "Live E2E 破坏性操作保护测试用文本节点。",
            },
        },
    )
    assert created["id"]
    return str(created["id"])


async def test_natural_language_full_reset_needs_only_one_confirmation_and_preserves_before_confirm(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool_request: Callable[..., Awaitable[Any]],
    project_state_request: Callable[..., Awaitable[dict[str, Any]]],
    project_nodes_request: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    seed_id = await _seed_text_node(api_client, project_id, call_tool_request, name="D01 保留到确认前")

    first = await send_chat_request(api_client, project_id, "重置整个项目")

    _done(first)
    _no_contract_error(first)
    assert any(event.get("type") == "confirm_required" and event.get("action") == "reset_project" for event in first)
    assert not any(event.get("type") == "canvas_action" and event.get("action") == "clear_all" for event in first)
    state = await project_state_request(api_client, project_id)
    assert state["_pending_reset_confirm"]["scope"] == "full"
    nodes = (await project_nodes_request(api_client, project_id))["nodes"]
    assert any(node["id"] == seed_id for node in nodes)

    second = await send_chat_request(api_client, project_id, "确认")

    _done(second)
    _no_contract_error(second)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "clear_all" for event in second)
    assert not any(event.get("type") == "confirm_required" for event in second)
    state = await project_state_request(api_client, project_id)
    assert not state.get("_pending_reset_confirm")
    nodes = (await project_nodes_request(api_client, project_id))["nodes"]
    assert nodes == []


async def test_plain_confirmation_without_pending_reset_does_not_clear_canvas(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool_request: Callable[..., Awaitable[Any]],
    project_state_request: Callable[..., Awaitable[dict[str, Any]]],
    project_nodes_request: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    seed_id = await _seed_text_node(api_client, project_id, call_tool_request, name="D02 不能被确认清掉")

    events = await send_chat_request(api_client, project_id, "确认")

    _done(events)
    _no_contract_error(events)
    assert not any(event.get("type") == "confirm_required" for event in events)
    assert not any(event.get("type") == "canvas_action" and event.get("action") == "clear_all" for event in events)
    state = await project_state_request(api_client, project_id)
    assert not state.get("_pending_reset_confirm")
    nodes = (await project_nodes_request(api_client, project_id))["nodes"]
    assert any(node["id"] == seed_id for node in nodes)


async def test_negated_delete_request_creates_new_node_without_destructive_actions(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool_request: Callable[..., Awaitable[Any]],
    project_nodes_request: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    seed_id = await _seed_text_node(api_client, project_id, call_tool_request, name="D03 必须保留")
    before_nodes = (await project_nodes_request(api_client, project_id))["nodes"]

    events = await send_chat_request(
        api_client,
        project_id,
        "不要删除旧节点，帮我新建一个角色，名字叫「D03 新角色」。只创建节点，不要生成图片，不要生成视频。",
    )

    _done(events)
    _no_contract_error(events)
    names = _tool_names(events)
    assert "node.create" in names, events
    assert "canvas.delete" not in names
    assert "project.reset" not in names
    assert not any(
        event.get("type") == "canvas_action" and event.get("action") in {"delete_node", "clear_all"}
        for event in events
    )

    after_nodes = (await project_nodes_request(api_client, project_id))["nodes"]
    assert any(node["id"] == seed_id for node in after_nodes)
    assert len(after_nodes) >= len(before_nodes) + 1
