from __future__ import annotations

import json

import pytest

from app.main import app
from app.services import codex_app_server as bridge_module
from app.services.codex_app_server import (
    OPENREEL_DYNAMIC_TOOLS,
    CodexAppServerBridge,
)


def test_codex_routes_are_separate_from_openreel_agent_routes() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/codex/status" in paths
    assert "/api/codex/connect" in paths
    assert "/api/codex/disconnect" in paths
    assert "/api/codex/projects/{project_id}/stream" in paths
    assert "/api/codex/projects/{project_id}/messages" in paths
    assert "/api/codex/projects/{project_id}/cancel" in paths


def test_codex_status_does_not_start_bridge_by_default() -> None:
    operation = app.openapi()["paths"]["/api/codex/status"]["get"]
    auto_start = next(item for item in operation["parameters"] if item["name"] == "auto_start")
    assert auto_start["schema"]["default"] is False


def test_dynamic_tools_are_project_scoped_and_node_first() -> None:
    names = {tool["name"] for tool in OPENREEL_DYNAMIC_TOOLS}
    assert {
        "openreel_project_state",
        "openreel_list_nodes",
        "openreel_get_nodes",
        "openreel_create_nodes",
        "openreel_update_nodes",
        "openreel_run_node",
        "openreel_move_node",
        "openreel_connect_nodes",
        "openreel_delete_nodes",
        "openreel_search_skills",
        "openreel_get_skill",
        "openreel_get_model_config",
    } <= names
    for tool in OPENREEL_DYNAMIC_TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "project_id" not in schema["properties"]


@pytest.mark.asyncio
async def test_missing_codex_binary_returns_actionable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "find_codex_binary", lambda: None)
    bridge = CodexAppServerBridge()
    status = await bridge.start()
    assert status["connected"] is False
    assert status["state"] == "missing_cli"
    assert "OPENREEL_CODEX_BIN" in str(status["detail"])


def test_fresh_bridge_is_optional_and_disconnected() -> None:
    status = CodexAppServerBridge().status_snapshot()
    assert status["state"] == "disconnected"
    assert status["app_server_running"] is False


@pytest.mark.asyncio
async def test_dynamic_node_tool_injects_bound_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(name: str, **kwargs: object) -> dict[str, object]:
        calls.append((name, kwargs))
        return {"ok": True, "nodes": []}

    monkeypatch.setattr(bridge_module.registry, "call", fake_call)
    bridge = CodexAppServerBridge()
    bridge._thread_projects["thread-1"] = "project-1"
    response = await bridge._execute_dynamic_tool({
        "threadId": "thread-1",
        "turnId": "turn-1",
        "callId": "call-1",
        "tool": "openreel_list_nodes",
        "arguments": {"type": "image", "limit": 10},
    })

    assert calls == [(
        "node.list",
        {"project_id": "project-1", "type": "image", "limit": 10},
    )]
    assert response["success"] is True
    payload = json.loads(response["contentItems"][0]["text"])
    assert payload == {"ok": True, "nodes": []}


@pytest.mark.asyncio
async def test_dynamic_delete_requires_explicit_confirmation() -> None:
    bridge = CodexAppServerBridge()
    result = await bridge._dispatch_openreel_tool(
        "project-1",
        "openreel_delete_nodes",
        {"node_ids": ["#1"], "confirm": False},
    )
    assert result["ok"] is False
    assert "confirm" in result["error"]
