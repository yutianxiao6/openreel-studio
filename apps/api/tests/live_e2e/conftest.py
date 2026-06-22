"""Live backend E2E fixtures.

These tests intentionally use the same HTTP request shape as the web app. They
are skipped by default because they call the configured online test model.
Run with:

    DRAMA_RUN_LIVE_E2E=1 PYTHONPATH=. uv run pytest -q tests/live_e2e
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_e2e: backend tests that send real chat requests to the online test model",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv("DRAMA_RUN_LIVE_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="set DRAMA_RUN_LIVE_E2E=1 to run live backend E2E tests")
    live_dir = Path(__file__).resolve().parent
    for item in items:
        item_path = Path(str(item.fspath)).resolve()
        if live_dir in item_path.parents:
            item.add_marker(skip)


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                events.append(json.loads(payload))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data: "):
            data_lines.append(line[6:])
    if data_lines:
        events.append(json.loads("\n".join(data_lines)))
    return events


@pytest_asyncio.fixture
async def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client backed by an isolated SQLite DB.

    The HTTP routes, dependencies, registry tools, session_scope users, SSE
    validation, and agent loop are real. The database is isolated so live tests
    do not modify a developer's normal local project data.
    """
    from app.agent import agent_trace, context_compact, message_queue, prompt_dump, task_graph, trace_store
    from app.api import routes_agent_debug
    from app.db import session as db_session

    db_path = tmp_path / "live-e2e.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(
        database_url,
        echo=False,
        future=True,
        connect_args={"timeout": 30},
    )
    session_local = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr(db_session.settings, "DATABASE_URL", database_url)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(trace_store.settings, "DATABASE_URL", database_url)
    trace_store._ENSURED_PATHS.clear()

    trace_root = tmp_path / "agent_traces"
    prompt_root = tmp_path / "prompts"
    tool_root = tmp_path / "tool_results"
    task_root = tmp_path / "tasks"
    project_files_root = tmp_path / "project_files"
    task_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_trace, "traces_root", lambda: trace_root)
    monkeypatch.setattr(routes_agent_debug, "traces_root", lambda: trace_root)
    monkeypatch.setattr(prompt_dump, "_DUMP_ROOT", prompt_root)
    monkeypatch.setattr(prompt_dump, "prompt_dumps_root", lambda: prompt_root)
    monkeypatch.setattr(routes_agent_debug, "prompt_dumps_root", lambda: prompt_root)
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tool_root)
    monkeypatch.setattr(routes_agent_debug, "tool_results_dir", lambda: tool_root)
    monkeypatch.setattr(task_graph.task_graph, "dir", task_root)
    monkeypatch.setattr(task_graph.task_graph, "_next_id", 1)
    monkeypatch.setenv("DRAMA_AGENT_TRACE_DB_ENABLED", "1")
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_ENABLED", "1")

    async with message_queue._lock:  # type: ignore[attr-defined]
        message_queue._queues.clear()  # type: ignore[attr-defined]
        message_queue._active_streams.clear()  # type: ignore[attr-defined]
        message_queue._cancel_requests.clear()  # type: ignore[attr-defined]

    await db_session.init_db()

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=httpx.Timeout(240.0, connect=10.0),
    ) as client:
        yield client

    await engine.dispose()


@pytest_asyncio.fixture
async def project_id(api_client: httpx.AsyncClient) -> str:
    return await create_project(api_client)


@pytest.fixture
def send_chat_request():
    return send_chat


@pytest.fixture
def call_tool_request():
    return call_tool


@pytest.fixture
def project_state_request():
    return project_state


@pytest.fixture
def project_nodes_request():
    return project_nodes


async def create_project(client: httpx.AsyncClient, title: str = "Live E2E Backend Test") -> str:
    response = await client.post(
        "/api/projects",
        json={
            "title": title,
            "genre": "都市短剧",
            "format": "竖屏短剧",
            "episode_count": 2,
            "duration_per_episode": 30,
            "budget_level": "low",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"]
    return str(body["id"])


async def send_chat(
    client: httpx.AsyncClient,
    project_id: str,
    message: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    decision_inputs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Send the exact web chat request body to /api/chat/stream."""
    response = await client.post(
        "/api/chat/stream",
        json={
            "project_id": project_id,
            "message": message,
            "attachments": attachments or [],
            "decision_inputs": decision_inputs,
        },
    )
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert events, "chat stream returned no SSE events"
    return events


async def call_tool(
    client: httpx.AsyncClient,
    tool: str,
    args: dict[str, Any] | None = None,
) -> Any:
    response = await client.post(
        "/api/tools/call",
        json={"tool": tool, "args": args or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def project_state(client: httpx.AsyncClient, project_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/projects/{project_id}/state")
    assert response.status_code == 200, response.text
    return response.json()


async def project_nodes(client: httpx.AsyncClient, project_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/projects/{project_id}/nodes")
    assert response.status_code == 200, response.text
    return response.json()


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type")) for event in events]


def events_of_type(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == event_type]


def assert_done(events: list[dict[str, Any]], status: str | None = None) -> None:
    done = [event for event in events if event.get("type") == "done"]
    assert done, f"missing done event; saw {event_types(events)}"
    if status is not None:
        assert done[-1].get("status") == status


def assert_no_sse_contract_error(events: list[dict[str, Any]]) -> None:
    errors = [
        event
        for event in events
        if event.get("type") == "error"
        and "SSE event contract error" in str(event.get("message") or "")
    ]
    assert not errors


def tool_names(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(event.get("tool"))
        for event in events
        if event.get("type") in {"tool_start", "tool_done"} and event.get("tool")
    ]
