"""Deterministic image operations for grid editing and extraction."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from app.config import settings
from app.db.models import WorkflowNode
from app.db.session import session_scope
from app.mcp_tools import canvas_tools


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_GRID_PRESETS = {(2, 2), (2, 3), (3, 2), (3, 3)}


def _storage_root() -> Path:
    return Path(getattr(settings, "STORAGE_PATH", "./storage")).resolve()


def _generated_root(project_id: str) -> Path:
    root = _storage_root() / project_id / "generated_images" / "image_ops"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _media_url(project_id: str, path: Path) -> str:
    rel = path.resolve().relative_to((_storage_root() / project_id / "generated_images").resolve())
    return f"/api/media/{project_id}/{rel.as_posix()}"


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_image_path(path: Path) -> bool:
    return path.exists() and path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS


async def _node(project_id: str, node_id: str) -> WorkflowNode | None:
    async with session_scope() as session:
        node = await session.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        return None
    return node


def _iter_image_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        if value.get("type") == "image_grid":
            for key in ("composite_local_path", "local_path", "composite_url", "local_url", "url"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    values.append(item)
        if value.get("type") == "fusion" and isinstance(value.get("stages"), list):
            for stage in reversed(value["stages"]):
                if not isinstance(stage, dict):
                    continue
                for key in ("local_path", "local_url", "url", "remote_url"):
                    item = stage.get(key)
                    if isinstance(item, str) and item:
                        values.append(item)
        for key in ("local_path", "path", "local_url", "url", "remote_url"):
            item = value.get(key)
            if isinstance(item, str) and item:
                values.append(item)
        for item in value.values():
            if isinstance(item, (dict, list)):
                values.extend(_iter_image_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_iter_image_values(item))
    return values


def _local_candidates(project_id: str, value: str) -> list[Path]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith(("http://", "https://", "data:")):
        return []
    candidates: list[Path] = []
    media_prefix = f"/api/media/{project_id}/"
    upload_prefix = f"/api/uploads/{project_id}/file/"
    if text.startswith(media_prefix):
        candidates.append(_storage_root() / project_id / "generated_images" / text[len(media_prefix):].lstrip("/"))
    elif text.startswith(upload_prefix):
        candidates.append(_storage_root() / project_id / text[len(upload_prefix):].lstrip("/"))
    elif text.startswith("generated_images/"):
        candidates.append(_storage_root() / project_id / text)
    elif text.startswith("uploads/"):
        candidates.append(_storage_root() / project_id / text)
    else:
        raw = Path(text).expanduser()
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append(_storage_root() / project_id / text)
    return [path.resolve() for path in candidates]


async def _download_remote(project_id: str, url: str) -> Path | None:
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
        suffix = Path(url.split("?", 1)[0]).suffix.lower()
        if suffix not in _IMAGE_EXTENSIONS:
            suffix = ".png"
        path = _generated_root(project_id) / f"remote-{uuid.uuid4().hex[:10]}{suffix}"
        path.write_bytes(response.content)
        return path
    except Exception:
        return None


async def _resolve_cell_reference(project_id: str, node_ref: str) -> tuple[Path | None, dict[str, Any] | None]:
    if "#cell:" not in node_ref:
        return None, None
    node_id, cell_id = node_ref[len("node:"):].split("#cell:", 1)
    node = await _node(project_id, node_id.strip())
    if not node:
        return None, None
    output = _parse_json_dict(node.output_json)
    if output.get("type") != "image_grid":
        return None, None
    for cell in output.get("cells") or []:
        if not isinstance(cell, dict) or str(cell.get("cell_id") or "") != cell_id:
            continue
        for value in _iter_image_values(cell):
            path = await resolve_image_path(project_id, value)
            if path:
                return path, cell
    return None, None


async def resolve_image_path(project_id: str, ref: str) -> Path | None:
    text = str(ref or "").strip()
    if not text:
        return None
    if text.startswith("node:"):
        if "#cell:" in text:
            path, _ = await _resolve_cell_reference(project_id, text)
            return path
        node = await _node(project_id, text[len("node:"):].strip())
        if not node:
            return None
        for value in _iter_image_values(_parse_json_dict(node.output_json)):
            path = await resolve_image_path(project_id, value)
            if path:
                return path
        return None
    if text.startswith(("http://", "https://")):
        return await _download_remote(project_id, text)
    for candidate in _local_candidates(project_id, text):
        if _is_image_path(candidate):
            return candidate
    return None


def _validate_grid(rows: int, cols: int) -> tuple[int, int] | None:
    try:
        rows_i = int(rows)
        cols_i = int(cols)
    except (TypeError, ValueError):
        return None
    return (rows_i, cols_i) if (rows_i, cols_i) in _GRID_PRESETS else None


def _open_image(path: Path) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGBA")


def _save_image(project_id: str, image: Image.Image, prefix: str) -> dict[str, Any]:
    path = _generated_root(project_id) / f"{prefix}-{uuid.uuid4().hex[:10]}.png"
    image.save(path, format="PNG")
    url = _media_url(project_id, path)
    return {
        "url": url,
        "local_url": url,
        "local_path": str(path),
        "width": image.width,
        "height": image.height,
    }


def _fit_image(image: Image.Image, size: tuple[int, int], fit: str = "cover") -> Image.Image:
    if fit == "contain":
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        fitted = ImageOps.contain(image, size)
        x = (size[0] - fitted.width) // 2
        y = (size[1] - fitted.height) // 2
        canvas.alpha_composite(fitted, (x, y))
        return canvas
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)


def _compose(project_id: str, cells: list[dict[str, Any]], rows: int, cols: int) -> dict[str, Any]:
    first = next((cell for cell in cells if cell.get("width") and cell.get("height")), cells[0])
    cell_w = int(first.get("width") or 1)
    cell_h = int(first.get("height") or 1)
    canvas = Image.new("RGBA", (cell_w * cols, cell_h * rows), (0, 0, 0, 0))
    for cell in cells:
        path = Path(str(cell.get("local_path") or ""))
        if not _is_image_path(path):
            continue
        image = _fit_image(_open_image(path), (cell_w, cell_h), fit="cover")
        row = int(cell.get("row") or 1) - 1
        col = int(cell.get("col") or 1) - 1
        canvas.alpha_composite(image, (col * cell_w, row * cell_h))
    return _save_image(project_id, canvas, "grid-composite")


def _empty_grid_cell(cell: dict[str, Any]) -> dict[str, Any]:
    next_cell = {
        key: value
        for key, value in cell.items()
        if key not in {"url", "local_url", "local_path", "source", "asset_id"}
    }
    next_cell["empty"] = True
    next_cell["source"] = {"kind": "empty"}
    return next_cell


def _replace_grid_cell(output: dict[str, Any], cell_id: str, replacement: dict[str, Any]) -> dict[str, Any]:
    return {
        **output,
        "cells": [
            replacement if isinstance(cell, dict) and str(cell.get("cell_id") or "") == cell_id else cell
            for cell in output.get("cells") or []
        ],
    }


async def _save_grid_output(project_id: str, node_id: str, output: dict[str, Any]) -> dict[str, Any]:
    grid = output.get("grid") if isinstance(output.get("grid"), dict) else {}
    rows = int(grid.get("rows") or 1)
    cols = int(grid.get("cols") or 1)
    cells = [cell for cell in output.get("cells") or [] if isinstance(cell, dict)]
    composite = _compose(project_id, cells, rows, cols)
    next_output = {
        **output,
        "cells": cells,
        "url": composite["url"],
        "local_url": composite["local_url"],
        "local_path": composite["local_path"],
        "width": composite["width"],
        "height": composite["height"],
        "composite_url": composite["local_url"],
        "composite_local_path": composite["local_path"],
        "updated_at": datetime.utcnow().isoformat(),
    }
    await canvas_tools.update_node(node_id, {"status": "completed", "error_message": None, "output_data": next_output})
    await _emit_node_update(project_id, node_id, next_output)
    return next_output


async def _emit_node_update(project_id: str, node_id: str, output: dict[str, Any], *, status: str = "completed") -> None:
    try:
        from app.agent.orchestrator import emit_canvas_event

        await emit_canvas_event(
            {
                "type": "canvas_action",
                "action": "update_node",
                "payload": {"id": node_id, "status": status, "preview": output, "error": None, "error_message": None},
            },
            project_id=project_id,
        )
    except Exception:
        return


async def split_grid_node(
    project_id: str,
    node_id: str,
    rows: int,
    cols: int,
    *,
    source_ref: str | None = None,
) -> dict[str, Any]:
    grid = _validate_grid(rows, cols)
    if not grid:
        return {"ok": False, "error": "grid must be one of 2x2, 2x3, 3x2, 3x3", "error_kind": "invalid_grid"}
    node = await _node(project_id, node_id)
    if not node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
    rows_i, cols_i = grid
    ref = source_ref or f"node:{node_id}"
    source_path = await resolve_image_path(project_id, ref)
    if not source_path:
        return {
            "ok": False,
            "error": "source image not found",
            "error_kind": "source_image_unresolved",
            "source_ref": ref,
        }
    image = _open_image(source_path)
    cell_w = image.width // cols_i
    cell_h = image.height // rows_i
    cells: list[dict[str, Any]] = []
    base_title = node.title or "图片"
    for row in range(rows_i):
        for col in range(cols_i):
            index = row * cols_i + col + 1
            left = col * cell_w
            top = row * cell_h
            right = image.width if col == cols_i - 1 else (col + 1) * cell_w
            bottom = image.height if row == rows_i - 1 else (row + 1) * cell_h
            crop = image.crop((left, top, right, bottom))
            saved = _save_image(project_id, crop, f"grid-cell-{node_id[:8]}-{index}")
            cells.append({
                "cell_id": f"r{row + 1}c{col + 1}",
                "index": index,
                "row": row + 1,
                "col": col + 1,
                "title": f"{base_title}的第{index}图片",
                "crop_box": [left, top, right, bottom],
                "source": {"kind": "crop", "source_ref": ref, "source_node_id": node_id},
                **saved,
            })
    composite = _compose(project_id, cells, rows_i, cols_i)
    output = {
        "ok": True,
        "type": "image_grid",
        "operation": "grid_split",
        "status": "completed",
        "source_image": {"ref": ref, "local_path": str(source_path)},
        "grid": {"rows": rows_i, "cols": cols_i},
        "cells": cells,
        "url": composite["url"],
        "local_url": composite["local_url"],
        "local_path": composite["local_path"],
        "width": composite["width"],
        "height": composite["height"],
        "composite_url": composite["local_url"],
        "composite_local_path": composite["local_path"],
        "created_at": datetime.utcnow().isoformat(),
    }
    input_data = _parse_json_dict(node.input_json)
    input_data.update({"operation": "grid_split", "grid": {"rows": rows_i, "cols": cols_i}})
    await canvas_tools.update_node(node_id, {"status": "completed", "error_message": None, "input_json": input_data, "output_data": output})
    await _emit_node_update(project_id, node_id, output)
    return output


async def combine_grid_node(
    project_id: str,
    node_id: str,
    source_refs: list[str],
    rows: int,
    cols: int,
    *,
    fit: str = "cover",
) -> dict[str, Any]:
    grid = _validate_grid(rows, cols)
    if not grid:
        return {"ok": False, "error": "grid must be one of 2x2, 2x3, 3x2, 3x3", "error_kind": "invalid_grid"}
    node = await _node(project_id, node_id)
    if not node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
    rows_i, cols_i = grid
    required = rows_i * cols_i
    refs = [str(ref or "").strip() for ref in source_refs if str(ref or "").strip()]
    if len(refs) != required:
        return {
            "ok": False,
            "error": f"grid {rows_i}x{cols_i} requires {required} images",
            "error_kind": "invalid_source_count",
            "source_count": len(refs),
        }
    paths = []
    for ref in refs:
        path = await resolve_image_path(project_id, ref)
        if not path:
            return {"ok": False, "error": f"source image not found: {ref}", "error_kind": "source_image_unresolved"}
        paths.append(path)
    first = _open_image(paths[0])
    cell_size = first.size
    cells: list[dict[str, Any]] = []
    base_title = node.title or "组合图"
    for index, (ref, path) in enumerate(zip(refs, paths), start=1):
        row = (index - 1) // cols_i + 1
        col = (index - 1) % cols_i + 1
        fitted = _fit_image(_open_image(path), cell_size, fit=fit)
        saved = _save_image(project_id, fitted, f"grid-combine-{node_id[:8]}-{index}")
        cells.append({
            "cell_id": f"r{row}c{col}",
            "index": index,
            "row": row,
            "col": col,
            "title": f"{base_title}的第{index}图片",
            "source": {"kind": "external", "source_ref": ref},
            **saved,
        })
    composite = _compose(project_id, cells, rows_i, cols_i)
    output = {
        "ok": True,
        "type": "image_grid",
        "operation": "grid_combine",
        "status": "completed",
        "grid": {"rows": rows_i, "cols": cols_i},
        "cells": cells,
        "url": composite["url"],
        "local_url": composite["local_url"],
        "local_path": composite["local_path"],
        "width": composite["width"],
        "height": composite["height"],
        "composite_url": composite["local_url"],
        "composite_local_path": composite["local_path"],
        "created_at": datetime.utcnow().isoformat(),
    }
    input_data = _parse_json_dict(node.input_json)
    input_data.update({"operation": "grid_combine", "grid": {"rows": rows_i, "cols": cols_i}, "source_images": refs})
    await canvas_tools.update_node(node_id, {"status": "completed", "error_message": None, "input_json": input_data, "output_data": output})
    await _emit_node_update(project_id, node_id, output)
    return output


async def extract_grid_cell_node(
    project_id: str,
    grid_node_id: str,
    cell_id: str,
    *,
    x: float = 0,
    y: float = 0,
    remove_from_grid: bool = False,
) -> dict[str, Any]:
    grid_node = await _node(project_id, grid_node_id)
    if not grid_node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": grid_node_id}
    output = _parse_json_dict(grid_node.output_json)
    if output.get("type") != "image_grid":
        return {"ok": False, "error": "Node is not an image grid", "error_kind": "not_image_grid"}
    cell = next(
        (item for item in output.get("cells") or [] if isinstance(item, dict) and str(item.get("cell_id") or "") == cell_id),
        None,
    )
    if not cell:
        return {"ok": False, "error": "Grid cell not found", "error_kind": "cell_not_found", "cell_id": cell_id}
    if cell.get("empty") or not (cell.get("local_url") or cell.get("url") or cell.get("local_path")):
        return {"ok": False, "error": "Grid cell is empty", "error_kind": "cell_empty", "cell_id": cell_id}
    title = str(cell.get("title") or f"{grid_node.title or '图片'}的第{cell.get('index') or ''}图片").strip()
    ref = f"node:{grid_node_id}#cell:{cell_id}"
    input_data = {
        "title": title,
        "references": [{"ref": ref, "role": "source_image"}],
        "source_grid_node_id": grid_node_id,
        "source_grid_cell_id": cell_id,
    }
    node = await canvas_tools.create_node(
        project_id=project_id,
        node_type="image",
        title=title,
        position_x=x,
        position_y=y,
        input_data=input_data,
        model_config={"surface": "draft_canvas", "_ui_creator": "user"},
    )
    image_output = {
        "ok": True,
        "type": "image",
        "source_mode": "grid_cell",
        "source_grid_node_id": grid_node_id,
        "source_grid_cell_id": cell_id,
        "url": cell.get("url") or cell.get("local_url"),
        "local_url": cell.get("local_url") or cell.get("url"),
        "local_path": cell.get("local_path"),
        "width": cell.get("width"),
        "height": cell.get("height"),
    }
    await canvas_tools.update_node(node["id"], {"status": "completed", "output_data": image_output})
    updated_grid = None
    if remove_from_grid:
        emptied = _empty_grid_cell(cell)
        updated_grid = await _save_grid_output(
            project_id,
            grid_node_id,
            _replace_grid_cell(output, cell_id, emptied),
        )
    try:
        from app.agent.orchestrator import emit_canvas_event

        await emit_canvas_event(
            {
                "type": "canvas_action",
                "action": "create_node",
                "payload": {**node, "status": "completed", "preview": image_output},
            },
            project_id=project_id,
        )
    except Exception:
        pass
    return {
        "ok": True,
        "node": {**node, "status": "completed", "output": image_output},
        "cell": cell,
        "grid": updated_grid,
    }


async def place_grid_cell_node(
    project_id: str,
    grid_node_id: str,
    cell_id: str,
    source_ref: str,
    *,
    fit: str = "cover",
    remove_source_node: bool = False,
) -> dict[str, Any]:
    grid_node = await _node(project_id, grid_node_id)
    if not grid_node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": grid_node_id}
    output = _parse_json_dict(grid_node.output_json)
    if output.get("type") != "image_grid":
        return {"ok": False, "error": "Node is not an image grid", "error_kind": "not_image_grid"}
    cell = next(
        (item for item in output.get("cells") or [] if isinstance(item, dict) and str(item.get("cell_id") or "") == cell_id),
        None,
    )
    if not cell:
        return {"ok": False, "error": "Grid cell not found", "error_kind": "cell_not_found", "cell_id": cell_id}
    source = str(source_ref or "").strip()
    if not source:
        return {"ok": False, "error": "source_ref is required", "error_kind": "missing_source_ref"}
    source_path = await resolve_image_path(project_id, source)
    if not source_path:
        return {"ok": False, "error": "source image not found", "error_kind": "source_image_unresolved", "source_ref": source}

    width = int(cell.get("width") or 0)
    height = int(cell.get("height") or 0)
    if width <= 0 or height <= 0:
        size_source = next(
            (item for item in output.get("cells") or [] if isinstance(item, dict) and item.get("width") and item.get("height")),
            None,
        )
        width = int((size_source or {}).get("width") or 1)
        height = int((size_source or {}).get("height") or 1)

    fitted = _fit_image(_open_image(source_path), (width, height), fit=fit)
    saved = _save_image(project_id, fitted, f"grid-place-{grid_node_id[:8]}-{cell.get('index') or cell_id}")
    replacement = {
        **cell,
        "empty": False,
        "source": {"kind": "placed", "source_ref": source},
        **saved,
    }
    updated_grid = await _save_grid_output(
        project_id,
        grid_node_id,
        _replace_grid_cell(output, cell_id, replacement),
    )

    removed_source_node = None
    if remove_source_node and source.startswith("node:"):
        source_node_id = source[len("node:"):].split("#", 1)[0].strip()
        if source_node_id and source_node_id != grid_node_id:
            removed_source_node = await canvas_tools.delete_nodes(project_id, [source_node_id])

    return {
        "ok": True,
        "grid": updated_grid,
        "cell": replacement,
        "removed_source_node": removed_source_node,
    }


async def inpaint_region_node(*_: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "局部重绘需要支持 image edit/inpaint 的图片 provider；当前版本先保留 UI/API 协议。",
        "error_kind": "provider_unsupported_inpaint",
        "node_id": kwargs.get("node_id"),
        "mask": kwargs.get("mask"),
        "mask_ref": kwargs.get("mask_ref"),
        "cell_id": kwargs.get("cell_id"),
    }
