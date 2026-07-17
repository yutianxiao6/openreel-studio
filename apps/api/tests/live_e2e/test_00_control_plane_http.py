from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.live_e2e]


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type")) for event in events]


def _done(events: list[dict[str, Any]], status: str = "completed") -> None:
    done = [event for event in events if event.get("type") == "done"]
    assert done, _types(events)
    assert done[-1].get("status") == status


def _no_sse_contract_error(events: list[dict[str, Any]]) -> None:
    assert not [
        event for event in events
        if event.get("type") == "error"
        and "SSE event contract error" in str(event.get("message") or "")
    ]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _slash_menu_names() -> set[str]:
    text = (_repo_root() / "apps/web/components/chat/SlashMenu.tsx").read_text(encoding="utf-8")
    block = re.search(r"export const SLASH_COMMANDS: SlashCommandDef\[\] = \[(.*?)\n\]", text, re.S)
    assert block, "SLASH_COMMANDS not found"
    return set(re.findall(r'name:\s*"(/[^"]+)"', block.group(1)))


def _slash_completion_names() -> set[str]:
    text = (_repo_root() / "apps/web/components/chat/SlashMenu.tsx").read_text(encoding="utf-8")
    block = re.search(r"export const SLASH_COMMAND_COMPLETIONS: SlashCommandDef\[\] = \[(.*?)\n\]", text, re.S)
    assert block, "SLASH_COMMAND_COMPLETIONS not found"
    return set(re.findall(r'name:\s*"(/[^"]+)"', block.group(1)))


def _local_slash_names() -> set[str]:
    text = (_repo_root() / "apps/web/components/chat/ChatPanel.tsx").read_text(encoding="utf-8")
    block = re.search(r"const LOCAL_SLASH_COMMANDS = new Set\(\[(.*?)\]\)", text, re.S)
    assert block, "LOCAL_SLASH_COMMANDS not found"
    return set(re.findall(r'"(/[^"]+)"', block.group(1)))


async def test_slash_command_catalog_matches_frontend_backend_and_docs() -> None:
    from app.agent import slash_commands

    frontend_menu = _slash_menu_names()
    frontend_completions = _slash_completion_names()
    frontend_local = _local_slash_names()
    backend_stream = {"/plan", "/workflow", "/reset", "/doctor"}
    backend_compat = {f"/{name}" for name in slash_commands._COMMANDS}  # noqa: SLF001
    expected_menu = {
        "/help",
        "/plan",
        "/workflow",
        "/reset",
        "/doctor",
        "/status",
        "/config",
        "/model",
        "/mcp",
        "/clear",
    }

    assert frontend_menu == expected_menu
    assert frontend_local == {
        "/help",
        "/status",
        "/config",
        "/model",
        "/mcp",
        "/clear",
    }
    assert backend_stream.isdisjoint(frontend_local)
    assert backend_stream <= frontend_menu
    assert backend_stream <= backend_compat
    assert not {
        "/project",
        "/project list",
        "/project new",
        "/project switch",
        "/project delete",
        "/project delete confirm",
        "/project delete cancel",
    } & (frontend_menu | frontend_completions | backend_compat)
    assert not {"/prompts", "/templates"} & (frontend_menu | frontend_local | frontend_completions)
    assert "/help" in frontend_local
    assert "/help" in backend_compat


async def test_web_shape_doctor_command(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
) -> None:
    doctor_events = await send_chat_request(api_client, project_id, "/doctor")

    _done(doctor_events)
    _no_sse_contract_error(doctor_events)
    doctor = next(event for event in doctor_events if event.get("type") == "doctor_result")
    assert doctor["project_id"] == project_id
    assert "feature_flags" in doctor


async def test_all_stream_slash_plan_and_reset_variants_are_authenticated_and_compared(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
) -> None:
    plan_events = await send_chat_request(api_client, project_id, "/plan")
    _done(plan_events)
    slash = next(event for event in plan_events if event.get("type") == "slash_command")
    assert slash["command"] == "plan"
    assert slash["action"] == "enter"
    assert slash["ok"] is True
    assert not any(event.get("type") == "agent_round" for event in plan_events)

    execute_no_plan = await send_chat_request(api_client, project_id, "/plan execute")
    _done(execute_no_plan, status="failed")
    slash = next(event for event in execute_no_plan if event.get("type") == "slash_command")
    assert slash["command"] == "plan"
    assert slash["action"] == "execute"
    assert slash["ok"] is False

    legacy_approve = await send_chat_request(api_client, project_id, "/plan approve")
    _done(legacy_approve, status="failed")
    slash = next(event for event in legacy_approve if event.get("type") == "slash_command")
    assert slash["command"] == "plan"
    assert slash["action"] == "approve"
    assert slash["ok"] is False
    assert slash["error"] == "legacy_plan_action_removed"
    assert not any(event.get("type") == "agent_round" for event in legacy_approve)

    exit_plan = await send_chat_request(api_client, project_id, "/plan exit")
    _done(exit_plan)
    slash = next(event for event in exit_plan if event.get("type") == "slash_command")
    assert slash["command"] == "plan"
    assert slash["action"] == "exit"
    assert slash["ok"] is True

    reset_status = await send_chat_request(api_client, project_id, "/reset")
    _done(reset_status)
    slash = next(event for event in reset_status if event.get("type") == "slash_command")
    assert slash["command"] == "reset"
    assert slash["action"] == "status"
    assert slash["ok"] is True

    reset_failed = await send_chat_request(api_client, project_id, "/reset failed")
    _done(reset_failed)
    slash = next(event for event in reset_failed if event.get("type") == "slash_command")
    assert slash["command"] == "reset"
    assert slash["action"] == "failed"
    assert slash["ok"] is True

    confirm_without_pending = await send_chat_request(api_client, project_id, "/reset confirm")
    _done(confirm_without_pending, status="failed")
    slash = next(event for event in confirm_without_pending if event.get("type") == "slash_command")
    assert slash["command"] == "reset"
    assert slash["action"] == "confirm"
    assert slash["ok"] is False
    assert slash["error"] == "no_pending_reset"

    full = await send_chat_request(api_client, project_id, "/reset full")
    _done(full)
    assert any(event.get("type") == "confirm_required" for event in full)
    slash = next(event for event in full if event.get("type") == "slash_command")
    assert slash["command"] == "reset"
    assert slash["action"] == "full"
    assert slash["requires_confirm"] is True
    state = await project_state_request(api_client, project_id)
    assert state["_pending_reset_confirm"]["scope"] == "full"

    cancel = await send_chat_request(api_client, project_id, "/reset cancel")
    _done(cancel)
    slash = next(event for event in cancel if event.get("type") == "slash_command")
    assert slash["command"] == "reset"
    assert slash["action"] == "cancel"
    assert slash["ok"] is True
    state = await project_state_request(api_client, project_id)
    assert not state.get("_pending_reset_confirm")


async def test_web_shape_reset_full_is_two_step_confirmed_once(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    project_state_request: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    request_events = await send_chat_request(api_client, project_id, "/reset full")

    _done(request_events)
    _no_sse_contract_error(request_events)
    assert any(event.get("type") == "confirm_required" for event in request_events)
    assert any(
        event.get("type") == "slash_command"
        and event.get("command") == "reset"
        and event.get("action") == "full"
        for event in request_events
    )
    state = await project_state_request(api_client, project_id)
    assert state["_pending_reset_confirm"]["scope"] == "full"

    confirm_events = await send_chat_request(api_client, project_id, "/reset confirm")

    _done(confirm_events)
    _no_sse_contract_error(confirm_events)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "clear_all" for event in confirm_events)
    assert any(
        event.get("type") == "slash_command"
        and event.get("command") == "reset"
        and event.get("action") == "confirm"
        and event.get("ok") is True
        for event in confirm_events
    )
    state = await project_state_request(api_client, project_id)
    assert not state.get("_pending_reset_confirm")


async def test_web_shape_busy_project_queues_normal_chat_but_rejects_slash(
    api_client: httpx.AsyncClient,
    project_id: str,
) -> None:
    from app.agent import message_queue

    await message_queue.mark_streaming(project_id, True)
    try:
        normal = await api_client.post(
            "/api/chat/stream",
            json={"project_id": project_id, "message": "这条应该排队", "attachments": []},
        )
        assert normal.status_code == 200
        assert '"type":"queued"' in normal.text or '"type": "queued"' in normal.text

        slash = await api_client.post(
            "/api/chat/stream",
            json={"project_id": project_id, "message": "/doctor", "attachments": []},
        )
        assert slash.status_code == 200
        assert "当前项目已有任务在执行" in slash.text
    finally:
        await message_queue.mark_streaming(project_id, False)


async def test_web_shape_queue_status_reports_detached_run_for_refresh_recovery(
    api_client: httpx.AsyncClient,
    project_id: str,
) -> None:
    from app.agent.run_broker import project_run_broker

    started = asyncio.Event()

    async def source():
        started.set()
        yield {"type": "agent_round", "round": 1, "content": "running", "tools": []}
        await asyncio.sleep(60)
        yield {"type": "done", "status": "completed"}

    run = await project_run_broker.start(project_id, source)
    await started.wait()
    try:
        response = await api_client.get(f"/api/chat/queue/{project_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["running"] is True
        assert payload["streaming"] is True
    finally:
        await project_run_broker.cancel(project_id, "test cleanup")
        if run.task is not None:
            await asyncio.wait_for(run.task, timeout=1.0)
