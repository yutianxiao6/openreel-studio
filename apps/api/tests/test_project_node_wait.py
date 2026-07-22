from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agent import orchestrator
from app.api import routes_projects


class FakeSession:
    def __init__(self, node: SimpleNamespace):
        self.node = node
        self.get_count = 0
        self.refresh_count = 0

    async def get(self, _model, node_id: str):
        self.get_count += 1
        return self.node if node_id == self.node.id else None

    async def refresh(self, node: SimpleNamespace) -> None:
        assert node is self.node
        self.refresh_count += 1


def install_session_scope(monkeypatch, db: FakeSession) -> None:
    @asynccontextmanager
    async def fake_session_scope():
        yield db

    monkeypatch.setattr(routes_projects, "session_scope", fake_session_scope)


@pytest.fixture
def node_detail(monkeypatch):
    async def fake_node_detail(node, project_id, _db):
        assert node.project_id == project_id
        return {"id": node.id, "project_id": project_id, "status": node.status}

    monkeypatch.setattr(routes_projects, "_node_detail_response", fake_node_detail)


@pytest.mark.asyncio
async def test_project_node_wait_wakes_from_terminal_canvas_event(monkeypatch, node_detail):
    node = SimpleNamespace(id="node-1", project_id="project-1", status="running")
    db = FakeSession(node)
    install_session_scope(monkeypatch, db)
    queues: list[asyncio.Queue] = []
    removed: list[asyncio.Queue] = []

    def fake_add_subscriber(project_id: str) -> asyncio.Queue:
        assert project_id == "project-1"
        queue: asyncio.Queue = asyncio.Queue()
        queues.append(queue)
        return queue

    def fake_remove_subscriber(project_id: str, queue: asyncio.Queue) -> None:
        assert project_id == "project-1"
        removed.append(queue)

    monkeypatch.setattr(orchestrator, "_add_subscriber", fake_add_subscriber)
    monkeypatch.setattr(orchestrator, "_remove_subscriber", fake_remove_subscriber)

    waiting = asyncio.create_task(
        routes_projects.wait_project_canvas_node_terminal(
            "project-1",
            "node-1",
            timeout_seconds=1,
        )
    )
    for _ in range(20):
        if queues:
            break
        await asyncio.sleep(0)
    assert queues

    node.status = "completed"
    await queues[0].put({
        "type": "canvas_action",
        "action": "update_node",
        "payload": {"id": "node-1", "status": "completed"},
    })

    result = await waiting

    assert result["ok"] is True
    assert result["terminal"] is True
    assert result["run_continues"] is False
    assert result["status"] == "completed"
    assert result["node"]["id"] == "node-1"
    assert db.get_count == 2
    assert removed == queues


@pytest.mark.asyncio
async def test_project_node_wait_timeout_is_non_error_and_does_not_poll(monkeypatch, node_detail):
    node = SimpleNamespace(id="node-2", project_id="project-1", status="running")
    db = FakeSession(node)
    install_session_scope(monkeypatch, db)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "media_progress", "node_id": "node-2", "progress": 50})
    queue.put_nowait({
        "type": "canvas_action",
        "action": "update_node",
        "payload": {"id": "node-2", "status": "processing"},
    })
    queue.put_nowait({
        "type": "canvas_action",
        "action": "update_node",
        "payload": {"id": "another-node", "status": "completed"},
    })
    removed: list[asyncio.Queue] = []

    monkeypatch.setattr(orchestrator, "_add_subscriber", lambda _project_id: queue)
    monkeypatch.setattr(
        orchestrator,
        "_remove_subscriber",
        lambda _project_id, removed_queue: removed.append(removed_queue),
    )

    result = await routes_projects.wait_project_canvas_node_terminal(
        "project-1",
        "node-2",
        timeout_seconds=0.01,
    )

    assert result["ok"] is True
    assert result["terminal"] is False
    assert result["generation_failed"] is False
    assert result["run_continues"] is True
    assert result["status"] == "running"
    assert db.get_count == 2
    assert removed == [queue]


@pytest.mark.asyncio
async def test_project_node_wait_returns_existing_terminal_node_without_event(monkeypatch, node_detail):
    node = SimpleNamespace(id="node-3", project_id="project-1", status="failed")
    db = FakeSession(node)
    install_session_scope(monkeypatch, db)
    queue: asyncio.Queue = asyncio.Queue()
    removed: list[asyncio.Queue] = []

    monkeypatch.setattr(orchestrator, "_add_subscriber", lambda _project_id: queue)
    monkeypatch.setattr(
        orchestrator,
        "_remove_subscriber",
        lambda _project_id, removed_queue: removed.append(removed_queue),
    )

    result = await routes_projects.wait_project_canvas_node_terminal(
        "project-1",
        "node-3",
        timeout_seconds=1,
    )

    assert result["ok"] is False
    assert result["terminal"] is True
    assert result["generation_failed"] is True
    assert result["run_continues"] is False
    assert result["status"] == "failed"
    assert db.get_count == 1
    assert removed == [queue]
