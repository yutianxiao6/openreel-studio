from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode


_DISPLAY_ID_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_DISPLAY_ID_LOCKS_MUTEX = threading.Lock()


def _display_id_lock(project_id: str) -> asyncio.Lock:
    loop_key = id(asyncio.get_running_loop())
    key = (loop_key, str(project_id or ""))
    with _DISPLAY_ID_LOCKS_MUTEX:
        lock = _DISPLAY_ID_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _DISPLAY_ID_LOCKS[key] = lock
        return lock


@asynccontextmanager
async def node_display_id_allocation(project_id: str) -> AsyncIterator[None]:
    async with _display_id_lock(project_id):
        yield


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
