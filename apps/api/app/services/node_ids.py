from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode


async def next_node_display_id(session: AsyncSession, project_id: str) -> int:
    result = await session.exec(
        select(WorkflowNode.display_id)
        .where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.display_id.is_not(None),
        )
        .order_by(WorkflowNode.display_id.desc())
        .limit(1)
    )
    value = result.first()
    try:
        return int(value) + 1 if value is not None else 0
    except (TypeError, ValueError):
        return 0
