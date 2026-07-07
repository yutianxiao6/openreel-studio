"""Hidden image operation tools used by the frontend and node runner."""
from __future__ import annotations

from typing import Any

from app.db.session import session_scope
from app.services import image_operations
from app.services.node_public_ids import resolve_internal_node_id


async def grid_split(
    project_id: str,
    node_id: str,
    rows: int,
    cols: int,
    source_ref: str | None = None,
) -> dict[str, Any]:
    return await image_operations.split_grid_node(
        project_id=project_id,
        node_id=node_id,
        rows=rows,
        cols=cols,
        source_ref=source_ref,
    )


async def grid_combine(
    project_id: str,
    node_id: str,
    source_refs: list[str],
    rows: int,
    cols: int,
    fit: str = "cover",
) -> dict[str, Any]:
    return await image_operations.combine_grid_node(
        project_id=project_id,
        node_id=node_id,
        source_refs=source_refs,
        rows=rows,
        cols=cols,
        fit=fit,
    )


async def extract_grid_cell(
    project_id: str,
    grid_node_id: str,
    cell_id: str,
    x: float = 0,
    y: float = 0,
    remove_from_grid: bool = False,
) -> dict[str, Any]:
    return await image_operations.extract_grid_cell_node(
        project_id=project_id,
        grid_node_id=grid_node_id,
        cell_id=cell_id,
        x=x,
        y=y,
        remove_from_grid=remove_from_grid,
    )


async def place_grid_cell(
    project_id: str,
    grid_node_id: str,
    cell_id: str,
    source_ref: str,
    fit: str = "cover",
    remove_source_node: bool = False,
) -> dict[str, Any]:
    return await image_operations.place_grid_cell_node(
        project_id=project_id,
        grid_node_id=grid_node_id,
        cell_id=cell_id,
        source_ref=source_ref,
        fit=fit,
        remove_source_node=remove_source_node,
    )


async def edit(
    project_id: str,
    node_id: str,
    operations: list[dict[str, Any]] | None = None,
    action: str = "preview",
    source_ref: str | None = None,
    candidate_ref: str | None = None,
) -> dict[str, Any]:
    async with session_scope() as session:
        resolved_node_id = await resolve_internal_node_id(session, project_id, node_id)
    return await image_operations.edit_image_node(
        project_id=project_id,
        node_id=resolved_node_id or node_id,
        operations=operations or [],
        action=action,
        source_ref=source_ref,
        candidate_ref=candidate_ref,
    )


async def segment(
    project_id: str,
    node_id: str | None = None,
    source_ref: str | None = None,
    target: str = "main_subject",
    method: str = "auto",
    unit: str = "normalized",
    rect: dict[str, Any] | list[Any] | None = None,
    bbox: dict[str, Any] | list[Any] | None = None,
    foreground_points: list[Any] | None = None,
    background_points: list[Any] | None = None,
    background_tolerance: int = 28,
    expand: int = 0,
    shrink: int = 0,
    feather: float = 1.0,
    smooth: int = 1,
    grabcut_iterations: int = 5,
) -> dict[str, Any]:
    resolved_node_id = None
    if node_id:
        async with session_scope() as session:
            resolved_node_id = await resolve_internal_node_id(session, project_id, node_id)
    return await image_operations.segment_image_node(
        project_id=project_id,
        node_id=resolved_node_id or node_id,
        source_ref=source_ref,
        target=target,
        method=method,
        unit=unit,
        rect=rect,
        bbox=bbox,
        foreground_points=foreground_points or [],
        background_points=background_points or [],
        background_tolerance=background_tolerance,
        expand=expand,
        shrink=shrink,
        feather=feather,
        smooth=smooth,
        grabcut_iterations=grabcut_iterations,
    )


async def inpaint_region(
    project_id: str,
    node_id: str,
    prompt: str,
    mask_ref: str | None = None,
    mask: dict[str, Any] | None = None,
    cell_id: str | None = None,
) -> dict[str, Any]:
    return await image_operations.inpaint_region_node(
        project_id=project_id,
        node_id=node_id,
        prompt=prompt,
        mask_ref=mask_ref,
        mask=mask,
        cell_id=cell_id,
    )
