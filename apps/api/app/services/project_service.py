"""Project service — CRUD and state management."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update as sqlalchemy_update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Project


DEFAULT_EPISODE_COUNT = 1
PROJECT_STATE_UPDATE_MAX_ATTEMPTS = 25


class ProjectStateConflictError(RuntimeError):
    """Raised when optimistic project-state updates cannot converge."""


def _initial_state(
    title: str,
) -> dict[str, Any]:
    return {
        "metadata": {"title": title},
        "story_bible": {
            "logline": "",
            "theme": "",
            "tone": "强冲突、快节奏、爽感强",
            "world_setting": "",
            "visual_style": "",
        },
        "characters": [],
        "relationships": [],
        "outline": {"acts": [], "episodes": []},
        "episodes": {},
        "scenes": {},
        "shots": {},
        "assets": [],
        "locked_fields": [],
        "workflow": {"nodes": [], "edges": []},
    }


def _is_blank_legacy_sixty_episode_project(project: Project, state: dict[str, Any]) -> bool:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    if project.episode_count != 60 and metadata.get("episode_count") != 60:
        return False
    if state.get("project_blueprint") or state.get("pending_blueprint_draft"):
        return False
    outline = state.get("outline") if isinstance(state.get("outline"), dict) else {}
    if outline.get("episodes"):
        return False
    if state.get("workflow", {}).get("nodes") if isinstance(state.get("workflow"), dict) else False:
        return False
    if state.get("characters") or state.get("scenes") or state.get("shots"):
        return False
    title = str(metadata.get("title") or project.title or "").strip()
    genre = str(metadata.get("genre") or project.genre or "").strip()
    return title in {"", "未命名项目"} and genre == ""


def _normalize_legacy_blank_defaults(project: Project, state: dict[str, Any]) -> bool:
    if not _is_blank_legacy_sixty_episode_project(project, state):
        return False
    metadata = state.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        state["metadata"] = metadata
    metadata["episode_count"] = DEFAULT_EPISODE_COUNT
    project.episode_count = DEFAULT_EPISODE_COUNT
    return True


class ProjectService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_project(
        self,
        title: str,
    ) -> Project:
        now = datetime.utcnow()
        state = _initial_state(title)
        project = Project(
            id=str(uuid.uuid4()),
            title=title,
            status="active",
            state_json=json.dumps(state, ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        self.db.add(project)
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def _normalize_project_defaults_if_needed(self, project: Project) -> Project:
        state = json.loads(project.state_json or "{}")
        if not _normalize_legacy_blank_defaults(project, state):
            return project
        project.state_json = json.dumps(state, ensure_ascii=False)
        project.updated_at = datetime.utcnow()
        self.db.add(project)
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def get_project(self, project_id: str) -> Project | None:
        project = await self.db.get(Project, project_id)
        if project is None:
            return None
        return await self._normalize_project_defaults_if_needed(project)

    async def list_projects(self) -> list[Project]:
        result = await self.db.exec(select(Project).order_by(Project.updated_at.desc()))
        items = list(result.all())
        normalized: list[Project] = []
        for project in items:
            normalized.append(await self._normalize_project_defaults_if_needed(project))
        return normalized

    async def update_project(
        self, project_id: str, patch: dict[str, Any]
    ) -> Project | None:
        project = await self.get_project(project_id)
        if not project:
            return None
        for key, value in patch.items():
            if hasattr(project, key):
                setattr(project, key, value)
        project.updated_at = datetime.utcnow()
        self.db.add(project)
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def get_project_state(self, project_id: str) -> dict[str, Any] | None:
        project = await self.get_project(project_id)
        if not project:
            return None
        # expire stale cache: tools use session_scope() to write state in
        # separate sessions, so our cached project may be out of date
        await self.db.refresh(project, ["state_json"])
        state = json.loads(project.state_json or "{}")
        await self._normalize_project_defaults_if_needed(project)
        state = json.loads(project.state_json or "{}")
        return state

    async def update_project_state(
        self, project_id: str, patch: dict[str, Any]
    ) -> Project | None:
        for attempt in range(PROJECT_STATE_UPDATE_MAX_ATTEMPTS):
            project = await self.db.get(Project, project_id)
            if not project:
                return None
            await self.db.refresh(project, ["state_json"])
            previous_state_json = project.state_json
            state = json.loads(previous_state_json or "{}")
            for key, value in patch.items():
                if "." in key:
                    head, tail = key.split(".", 1)
                    bucket = state.get(head)
                    if not isinstance(bucket, dict):
                        bucket = {}
                        state[head] = bucket
                    bucket[tail] = value
                else:
                    state[key] = value
            next_state_json = json.dumps(state, ensure_ascii=False)
            statement = (
                sqlalchemy_update(Project)
                .where(Project.id == project_id)
                .where(Project.state_json == previous_state_json)
                .values(
                    state_json=next_state_json,
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
            result = await self.db.exec(statement)
            if int(result.rowcount or 0) == 1:
                await self.db.commit()
                self.db.expire_all()
                return await self.db.get(Project, project_id)
            await self.db.rollback()
            self.db.expire_all()
            await asyncio.sleep(min(0.05, 0.001 * (2**attempt)))
        raise ProjectStateConflictError(
            f"project state update conflicted after {PROJECT_STATE_UPDATE_MAX_ATTEMPTS} attempts: {project_id}"
        )

    async def delete_project(self, project_id: str) -> bool:
        project = await self.get_project(project_id)
        if not project:
            return False
        await self.db.delete(project)
        await self.db.commit()
        return True
