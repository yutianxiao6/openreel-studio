from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import routes_projects, routes_tools
from app.db.models import Project, WorkflowEdge, WorkflowNode


@pytest_asyncio.fixture
async def canvas_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Project(id="project-1", title="Canvas control", state_json="{}"))
        session.add(WorkflowNode(
            id="source-1",
            project_id="project-1",
            display_id=1,
            type="image",
            title="Source",
            input_json="{}",
        ))
        session.add(WorkflowNode(
            id="target-1",
            project_id="project-1",
            display_id=2,
            type="video",
            title="Target",
            input_json="{}",
        ))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_canvas_graph_mutations_emit_project_events(
    monkeypatch: pytest.MonkeyPatch,
    canvas_session: AsyncSession,
) -> None:
    events: list[tuple[str, dict]] = []

    async def capture(project_id: str, action: str, payload: dict) -> None:
        assert project_id == "project-1"
        events.append((action, payload))

    monkeypatch.setattr(routes_projects, "_emit_project_canvas_action", capture)

    moved = await routes_projects.update_project_node_position(
        "project-1",
        "source-1",
        routes_projects.NodePositionRequest(x=320, y=180),
        canvas_session,
    )
    created = await routes_projects.create_project_edge(
        "project-1",
        routes_projects.CanvasEdgeRequest(
            source_node_id="source-1",
            target_node_id="target-1",
            label="reference",
        ),
        canvas_session,
    )
    updated = await routes_projects.update_project_edge(
        "project-1",
        created["id"],
        routes_projects.CanvasEdgeUpdateRequest(label="visual reference"),
        canvas_session,
    )
    deleted = await routes_projects.delete_project_edge(
        "project-1",
        created["id"],
        db=canvas_session,
    )

    assert moved["position"] == {"x": 320.0, "y": 180.0}
    assert updated["label"] == "visual reference"
    assert deleted["deleted_edge_ids"] == [created["id"]]
    assert [action for action, _payload in events] == [
        "update_node",
        "add_edge",
        "update_edge",
        "delete_edge",
    ]
    assert events[0][1]["position"] == {"x": 320.0, "y": 180.0}
    assert events[-1][1]["source_node_id"] == "source-1"
    assert events[-1][1]["target_node_id"] == "target-1"


@pytest.mark.asyncio
async def test_direct_node_update_tool_result_is_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[dict, str | None]] = []

    async def capture(event: dict, project_id: str | None = None) -> None:
        events.append((event, project_id))

    from app.agent import orchestrator

    monkeypatch.setattr(orchestrator, "emit_canvas_event", capture)

    await routes_tools._emit_direct_tool_canvas_events(
        "node.update",
        {"project_id": "project-1", "node_id": "#7"},
        {
            "id": "7",
            "_canvas_id": "internal-7",
            "type": "image",
            "title": "Updated",
            "edge_sync": {
                "changed": True,
                "added_edges": [{"id": "edge-new", "source_node_id": "source-1", "target_node_id": "internal-7"}],
                "removed_edges": [{"id": "edge-old", "source_node_id": "old-1", "target_node_id": "internal-7"}],
            },
        },
    )

    assert events == [(
        {
            "type": "canvas_action",
            "action": "update_node",
            "payload": {
                "id": "internal-7",
                "_canvas_id": "internal-7",
                "type": "image",
                "title": "Updated",
                "edge_sync": {
                    "changed": True,
                    "added_edges": [{"id": "edge-new", "source_node_id": "source-1", "target_node_id": "internal-7"}],
                    "removed_edges": [{"id": "edge-old", "source_node_id": "old-1", "target_node_id": "internal-7"}],
                },
            },
        },
        "project-1",
    ), (
        {
            "type": "canvas_action",
            "action": "add_edge",
            "payload": {"id": "edge-new", "source_node_id": "source-1", "target_node_id": "internal-7"},
        },
        "project-1",
    ), (
        {
            "type": "canvas_action",
            "action": "delete_edge",
            "payload": {"id": "edge-old", "source_node_id": "old-1", "target_node_id": "internal-7"},
        },
        "project-1",
    )]


def test_edge_update_route_is_part_of_the_public_canvas_api() -> None:
    from app.main import app

    operation = app.openapi()["paths"]["/api/projects/{project_id}/edges/{edge_id}"]
    assert set(operation) >= {"patch", "delete"}
