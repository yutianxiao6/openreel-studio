"""Version service — save and list project snapshots."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Version


class VersionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_version(
        self,
        project_id: str,
        target_type: str,
        target_id: str,
        snapshot: Any,
        message: str = "",
    ) -> Version:
        result = await self.db.exec(
            select(Version)
            .where(
                Version.project_id == project_id,
                Version.target_type == target_type,
                Version.target_id == target_id,
            )
            .order_by(Version.version_number.desc())
            .limit(1)
        )
        existing = result.first()
        version_number = (existing.version_number + 1) if existing else 1

        version = Version(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            version_number=version_number,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            message=message,
        )
        self.db.add(version)
        await self.db.commit()
        await self.db.refresh(version)
        return version

    async def list_versions(
        self, project_id: str, target_type: str, target_id: str
    ) -> list[Version]:
        result = await self.db.exec(
            select(Version)
            .where(
                Version.project_id == project_id,
                Version.target_type == target_type,
                Version.target_id == target_id,
            )
            .order_by(Version.version_number.desc())
        )
        return list(result.all())
