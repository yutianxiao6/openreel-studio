"""Deterministic collision-free placement for user-visible canvas nodes."""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode


CANVAS_ORIGIN_X = 120.0
CANVAS_ORIGIN_Y = 90.0
CANVAS_COLUMN_STEP = 360.0
CANVAS_ROW_STEP = 260.0
CANVAS_COLLISION_WIDTH = 320.0
CANVAS_COLLISION_HEIGHT = 220.0


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _node_surface(node: WorkflowNode) -> str:
    model_config = _as_dict(node.model_config_json)
    surface = model_config.get("surface") or model_config.get("_surface")
    if surface:
        return str(surface)
    input_data = _as_dict(node.input_json)
    return str(input_data.get("surface") or input_data.get("_surface") or "draft_canvas")


def _overlaps(position: tuple[float, float], occupied: Iterable[tuple[float, float]]) -> bool:
    x, y = position
    return any(
        abs(x - occupied_x) < CANVAS_COLLISION_WIDTH
        and abs(y - occupied_y) < CANVAS_COLLISION_HEIGHT
        for occupied_x, occupied_y in occupied
    )


def _candidate_offsets(max_ring: int = 128) -> Iterable[tuple[int, int]]:
    """Yield nearby grid cells, preferring right and then lower canvas space."""
    yield 0, 0
    for ring in range(1, max_ring + 1):
        for column in range(ring, -ring - 1, -1):
            row = ring - abs(column)
            if row == 0:
                yield column, 0
                continue
            yield column, row
            yield column, -row


def find_available_canvas_position(
    occupied: Iterable[tuple[float, float]],
    *,
    preferred_x: float = CANVAS_ORIGIN_X,
    preferred_y: float = CANVAS_ORIGIN_Y,
) -> tuple[float, float]:
    """Return the closest deterministic grid position that does not overlap."""
    occupied_positions = list(occupied)
    for column, row in _candidate_offsets():
        candidate = (
            float(preferred_x) + column * CANVAS_COLUMN_STEP,
            float(preferred_y) + row * CANVAS_ROW_STEP,
        )
        if not _overlaps(candidate, occupied_positions):
            return candidate

    rightmost = max((x for x, _y in occupied_positions), default=float(preferred_x))
    return rightmost + CANVAS_COLUMN_STEP, float(preferred_y)


async def resolve_canvas_node_position(
    session: AsyncSession,
    project_id: str,
    *,
    position_x: float | None = None,
    position_y: float | None = None,
    avoid_overlap: bool = False,
    surface: str = "draft_canvas",
) -> tuple[float, float]:
    """Resolve an implicit or collision-prone position against persisted nodes."""
    preferred_x = CANVAS_ORIGIN_X if position_x is None else float(position_x)
    preferred_y = CANVAS_ORIGIN_Y if position_y is None else float(position_y)
    if surface == "workflow_runtime":
        return preferred_x, preferred_y
    if position_x is not None and position_y is not None and not avoid_overlap:
        return preferred_x, preferred_y

    existing = list((await session.exec(
        select(WorkflowNode).where(WorkflowNode.project_id == project_id)
    )).all())
    occupied = [
        (float(node.position_x or 0.0), float(node.position_y or 0.0))
        for node in existing
        if _node_surface(node) != "workflow_runtime"
    ]
    return find_available_canvas_position(
        occupied,
        preferred_x=preferred_x,
        preferred_y=preferred_y,
    )
