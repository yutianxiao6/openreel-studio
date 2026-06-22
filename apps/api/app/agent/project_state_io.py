"""Small project-state read/write helpers shared by control-plane code."""
from __future__ import annotations

import json
from typing import Any

from app.db.models import Project
from app.db.session import session_scope


async def read_project_state(project_id: str) -> tuple[Project | None, dict[str, Any]]:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return None, {}
        try:
            state = json.loads(project.state_json or "{}")
        except (TypeError, json.JSONDecodeError):
            state = {}
        return project, state if isinstance(state, dict) else {}


async def write_project_state(project_id: str, state: dict[str, Any]) -> None:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return
        project.state_json = json.dumps(state if isinstance(state, dict) else {}, ensure_ascii=False)
        session.add(project)
        await session.commit()
