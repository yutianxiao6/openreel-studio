from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import session as db_session
from app.db.models import Project
from app.mcp_tools import canvas_tools
from app.services.node_placement import find_available_canvas_position
from app.services.node_service import NodeService


async def _setup_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'node-placement.db'}"
    engine = create_async_engine(database_url, echo=False, future=True, connect_args={"timeout": 30})
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    await db_session.init_db()
    async with db_session.session_scope() as session:
        session.add(Project(id="placement-project", title="Placement Test", state_json="{}"))
        await session.commit()


def test_find_available_canvas_position_prefers_nearest_open_slot() -> None:
    assert find_available_canvas_position([]) == (120.0, 90.0)
    assert find_available_canvas_position([(120.0, 90.0)]) == (480.0, 90.0)
    assert find_available_canvas_position([(120.0, 90.0), (480.0, 90.0)]) == (120.0, 350.0)


@pytest.mark.asyncio
async def test_canvas_create_without_position_avoids_existing_nodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _setup_db(monkeypatch, tmp_path)

    first = await canvas_tools.create_node("placement-project", "text", "First")
    second = await canvas_tools.create_node("placement-project", "image", "Second")
    explicit = await canvas_tools.create_node(
        "placement-project",
        "text",
        "Explicit",
        position_x=120,
        position_y=90,
    )
    workflow = await canvas_tools.create_node(
        "placement-project",
        "video",
        "Workflow",
        position_x=120,
        position_y=90,
        avoid_position_overlap=True,
    )

    assert first["position"] == {"x": 120.0, "y": 90.0}
    assert second["position"] == {"x": 480.0, "y": 90.0}
    assert explicit["position"] == {"x": 120.0, "y": 90.0}
    assert workflow["position"] == {"x": 120.0, "y": 350.0}


@pytest.mark.asyncio
async def test_node_service_uses_empty_slot_when_position_is_implicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _setup_db(monkeypatch, tmp_path)
    async with db_session.session_scope() as session:
        service = NodeService(session)
        first = await service.create_node("placement-project", {"type": "text", "title": "First"})
        second = await service.create_node("placement-project", {"type": "text", "title": "Second"})

    assert (first.position_x, first.position_y) == (120.0, 90.0)
    assert (second.position_x, second.position_y) == (480.0, 90.0)
