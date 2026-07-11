from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Project
from app.services.project_service import ProjectService


@pytest.mark.asyncio
async def test_project_state_concurrent_patches_do_not_lose_updates(tmp_path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'project-state.db'}",
        future=True,
        connect_args={"timeout": 30},
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    async with sessions() as session:
        session.add(Project(id="project-1", title="并发状态测试", state_json="{}"))
        await session.commit()

    async def update(index: int) -> None:
        async with sessions() as session:
            await ProjectService(session).update_project_state(
                "project-1",
                {f"parallel.step_{index}": {"status": "completed", "index": index}},
            )

    await asyncio.gather(*(update(index) for index in range(12)))

    async with sessions() as session:
        project = await session.get(Project, "project-1")
        assert project is not None
        state = json.loads(project.state_json or "{}")
    assert state["parallel"] == {
        f"step_{index}": {"status": "completed", "index": index}
        for index in range(12)
    }
    await engine.dispose()
