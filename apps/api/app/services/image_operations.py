"""Deterministic image operations for grid editing, extraction, and local edits."""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import cv2
import numpy as np
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from app.agent.vision_context import source_to_image_url
from app.config import settings
from app.db.models import WorkflowNode
from app.db.session import session_scope
from app.mcp_tools import canvas_tools
from app.services import media_history
from app.services.node_public_ids import (
    looks_like_internal_node_id,
    looks_like_public_node_id,
    resolve_internal_node_id,
)


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_GRID_PRESETS = {(2, 2), (2, 3), (3, 2), (3, 3)}
_EDIT_ACTIONS = {"preview", "commit"}
_EDIT_OPERATION_LIMIT = 80
_EDIT_TEMP_PREFIXES = ("edit-preview", "curve-preview")
_SEGMENT_MAX_DIMENSION = 1600
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _storage_root() -> Path:
    return settings.storage_path_resolved


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
        resolved = await resolve_internal_node_id(session, project_id, node_id)
        node = await session.get(WorkflowNode, resolved or node_id)
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
    media_index = text.find(media_prefix)
    upload_index = text.find(upload_prefix)
    if media_index >= 0:
        rel_path = text[media_index + len(media_prefix):].lstrip("/")
        if rel_path.startswith("generated_images/"):
            candidates.append(_storage_root() / project_id / rel_path)
        else:
            candidates.append(_storage_root() / project_id / "generated_images" / rel_path)
    elif upload_index >= 0:
        candidates.append(_storage_root() / project_id / text[upload_index + len(upload_prefix):].lstrip("/"))
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
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        path = unquote(parsed.path or "")
        for candidate in _local_candidates(project_id, path):
            if _is_image_path(candidate):
                return candidate
        return await _download_remote(project_id, text)
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
    if looks_like_public_node_id(text) or looks_like_internal_node_id(text):
        node = await _node(project_id, text)
        if node:
            for value in _iter_image_values(_parse_json_dict(node.output_json)):
                path = await resolve_image_path(project_id, value)
                if path:
                    return path
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


def _cleanup_edit_temp_images(project_id: str, node_public_id: str, keep_paths: list[Path] | None = None) -> dict[str, Any]:
    public_id = str(node_public_id or "").strip()
    if not public_id:
        return {"deleted": [], "errors": []}
    keep = {path.resolve() for path in (keep_paths or []) if str(path)}
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    root = _generated_root(project_id)
    for prefix in _EDIT_TEMP_PREFIXES:
        for path in root.glob(f"{prefix}-{public_id}-*.png"):
            resolved = path.resolve()
            if resolved in keep or not resolved.is_file():
                continue
            try:
                resolved.unlink()
                deleted.append(str(resolved))
            except OSError as exc:
                errors.append({"path": str(resolved), "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


async def cleanup_image_edit_temps(project_id: str, node_id: str) -> dict[str, Any]:
    node = await _node(project_id, node_id)
    if not node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
    if node.type != "image":
        return {
            "ok": False,
            "error": "image edit cleanup only supports image nodes",
            "error_kind": "invalid_node_type",
            "node_id": _public_node_id(node),
            "node_type": node.type,
        }
    cleanup = _cleanup_edit_temp_images(project_id, _public_node_id(node))
    return {
        "ok": True,
        "node_id": _public_node_id(node),
        "deleted_temp_files": cleanup["deleted"],
        "cleanup_errors": cleanup["errors"],
    }


async def _model_content_for_saved_image(
    project_id: str,
    saved: dict[str, Any],
    *,
    label: str,
    source_ref: str,
    note: str,
) -> dict[str, Any]:
    ref = str(source_ref or saved.get("local_url") or saved.get("url") or "").strip()
    if not ref:
        return {}
    try:
        image_url, meta = await source_to_image_url(project_id, ref)
    except Exception:
        return {}
    text = (
        f"<image-edit-result label=\"{label}\" source=\"{ref}\">\n"
        f"{note}\n"
        f"size={meta.get('width') or saved.get('width')}x{meta.get('height') or saved.get('height')}\n"
        "</image-edit-result>"
    )
    return {
        "_model_content_type": "image_edit_result",
        "_model_content_refs": [ref],
        "_model_content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }


def _public_node_id(node: WorkflowNode) -> str:
    if node.display_id is not None:
        return str(node.display_id)
    return str(node.id)


def _resolve_edit_source(output: Any) -> list[str]:
    return _iter_image_values(output)


def _operation_kind(operation: dict[str, Any]) -> str:
    return str(operation.get("type") or operation.get("kind") or operation.get("op") or "").strip().lower()


def _unit(operation: dict[str, Any]) -> str:
    unit = str(operation.get("unit") or operation.get("coordinates") or "normalized").strip().lower()
    return "pixel" if unit in {"px", "pixel", "pixels"} else "normalized"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _rgba(color: Any, opacity: Any = 1.0) -> tuple[int, int, int, int]:
    alpha = int(round(_clamp(_number(opacity, 1.0), 0.0, 1.0) * 255))
    try:
        parsed = ImageColor.getcolor(str(color or "#ffffff"), "RGBA")
    except ValueError:
        parsed = (255, 255, 255, 255)
    return (int(parsed[0]), int(parsed[1]), int(parsed[2]), int(round(parsed[3] * alpha / 255)))


def _point(value: Any, size: tuple[int, int], unit: str) -> tuple[float, float] | None:
    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = value[0], value[1]
    else:
        return None
    px = _number(x)
    py = _number(y)
    if unit != "pixel":
        px *= size[0]
        py *= size[1]
    return (_clamp(px, 0, max(0, size[0])), _clamp(py, 0, max(0, size[1])))


def _points(values: Any, size: tuple[int, int], unit: str) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    return [point for point in (_point(item, size, unit) for item in values) if point is not None]


def _rect_box(value: Any, size: tuple[int, int], unit: str) -> tuple[int, int, int, int] | None:
    if isinstance(value, dict):
        if all(key in value for key in ("left", "top", "right", "bottom")):
            left = _number(value.get("left"))
            top = _number(value.get("top"))
            right = _number(value.get("right"))
            bottom = _number(value.get("bottom"))
        else:
            left = _number(value.get("x", value.get("left")))
            top = _number(value.get("y", value.get("top")))
            width = _number(value.get("width", value.get("w")))
            height = _number(value.get("height", value.get("h")))
            right = left + width
            bottom = top + height
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        left, top, third, fourth = (_number(item) for item in value[:4])
        # Rect arrays are x, y, width, height by default.
        right = left + third
        bottom = top + fourth
    else:
        return None
    if unit != "pixel":
        left *= size[0]
        right *= size[0]
        top *= size[1]
        bottom *= size[1]
    x1 = int(round(_clamp(min(left, right), 0, size[0])))
    y1 = int(round(_clamp(min(top, bottom), 0, size[1])))
    x2 = int(round(_clamp(max(left, right), 0, size[0])))
    y2 = int(round(_clamp(max(top, bottom), 0, size[1])))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _radius_value(value: Any, size: tuple[int, int], unit: str) -> int:
    radius = _number(value, 0.0)
    if unit != "pixel":
        radius *= min(size)
    return max(0, min(int(round(radius)), min(size) // 2))


def _mask_for_shape_operation(operation: dict[str, Any], size: tuple[int, int]) -> Image.Image | None:
    unit = _unit(operation)
    shape = str(operation.get("shape") or operation.get("selection") or "rect").strip().lower()
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    if shape in {"polygon", "lasso", "path", "freehand"}:
        points = _points(operation.get("points") or operation.get("path"), size, unit)
        if len(points) < 3:
            return None
        draw.polygon(points, fill=255)
        return mask
    rect = _rect_box(operation.get("rect") or operation.get("box") or operation.get("bounds"), size, unit)
    if rect is None:
        points = _points(operation.get("points") or operation.get("path"), size, unit)
        if len(points) >= 3:
            draw.polygon(points, fill=255)
            return mask
        return None
    if shape in {"ellipse", "oval", "circle"}:
        draw.ellipse(rect, fill=255)
    elif shape in {"rounded_rect", "rounded_rectangle", "round_rect", "squircle"}:
        radius = _radius_value(
            operation.get("radius", operation.get("corner_radius", operation.get("rounding"))),
            size,
            unit,
        )
        draw.rounded_rectangle(rect, radius=radius, fill=255)
    else:
        draw.rectangle(rect, fill=255)
    return mask


def _combine_masks(masks: list[Image.Image], size: tuple[int, int]) -> Image.Image | None:
    combined: Image.Image | None = None
    for mask in masks:
        next_mask = mask.convert("L").resize(size) if mask.size != size else mask.convert("L")
        combined = next_mask if combined is None else ImageChops.lighter(combined, next_mask)
    return combined


def _color_equal_mask(image: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    rgb = image.convert("RGB")
    masks = [
        channel.point(lambda value, target=target: 255 if int(value) == target else 0)
        for channel, target in zip(rgb.split(), color)
    ]
    return ImageChops.darker(ImageChops.darker(masks[0], masks[1]), masks[2])


def _color_range_mask(image: Image.Image, color: Any, tolerance: Any = 24) -> Image.Image:
    rgb = image.convert("RGB")
    try:
        target = ImageColor.getcolor(str(color or "#ffffff"), "RGB")
    except ValueError:
        target = (255, 255, 255)
    solid = Image.new("RGB", image.size, target)
    diff = ImageChops.difference(rgb, solid)
    max_diff = ImageChops.lighter(ImageChops.lighter(diff.getchannel("R"), diff.getchannel("G")), diff.getchannel("B"))
    threshold = max(0, min(255, int(round(_number(tolerance, 24)))))
    return max_diff.point(lambda value: 255 if int(value) <= threshold else 0)


def _seed_points_for_background(operation: dict[str, Any], size: tuple[int, int]) -> list[tuple[int, int]]:
    unit = _unit(operation)
    explicit = _points(operation.get("seeds") or operation.get("seed_points"), size, unit)
    if explicit:
        return [
            (int(round(_clamp(x, 0, size[0] - 1))), int(round(_clamp(y, 0, size[1] - 1))))
            for x, y in explicit
            if size[0] > 0 and size[1] > 0
        ]
    seed_mode = str(operation.get("seed") or operation.get("seeds_from") or "edges").strip().lower()
    w, h = size
    if w <= 0 or h <= 0:
        return []
    if seed_mode in {"corners", "corner"}:
        return [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    if seed_mode not in {"edges", "edge", "border", "borders"}:
        return [(0, 0)]
    default_step = 1 if max(w, h) <= 1024 else max(1, min(w, h) // 256)
    step = max(1, int(_number(operation.get("seed_step"), default_step)))
    seeds: list[tuple[int, int]] = []
    for x in range(0, w, step):
        seeds.append((x, 0))
        seeds.append((x, h - 1))
    for y in range(0, h, step):
        seeds.append((0, y))
        seeds.append((w - 1, y))
    seeds.extend([(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)])
    return list(dict.fromkeys(seeds))


def _background_flood_mask(image: Image.Image, operation: dict[str, Any]) -> Image.Image:
    rgb = image.convert("RGB")
    before = rgb.copy()
    marker_candidates = [(255, 0, 255), (0, 255, 255), (255, 255, 0), (1, 2, 3), (254, 1, 253)]
    marker = marker_candidates[0]
    tolerance = max(0, min(255, int(round(_number(operation.get("tolerance", operation.get("threshold")), 28)))))
    for seed in _seed_points_for_background(operation, rgb.size):
        if rgb.getpixel(seed) == marker:
            continue
        ImageDraw.floodfill(rgb, seed, marker, thresh=tolerance)
    filled = _color_equal_mask(rgb, marker)
    original = _color_equal_mask(before, marker)
    return ImageChops.subtract(filled, original)


def _alpha_threshold_mask(image: Image.Image, operation: dict[str, Any]) -> Image.Image:
    alpha = image.convert("RGBA").getchannel("A")
    threshold = max(0, min(255, int(round(_number(operation.get("threshold"), 1)))))
    direction = str(operation.get("direction") or operation.get("compare") or "below").strip().lower()
    if direction in {"above", "greater", "gt", "opaque"}:
        return alpha.point(lambda value: 255 if int(value) >= threshold else 0)
    return alpha.point(lambda value: 255 if int(value) <= threshold else 0)


def _mask_for_edit_mask_operation(image: Image.Image, operation: dict[str, Any]) -> Image.Image | None:
    mode = str(operation.get("mode") or operation.get("source") or operation.get("selection") or "shape").strip().lower()
    if mode in {"shape", "manual", "geometry"}:
        return _mask_for_shape_operation(operation, image.size)
    if mode in {"background", "edge_background", "flood", "floodfill", "remove_background"}:
        return _background_flood_mask(image, operation)
    if mode in {"color", "color_range", "chroma"}:
        colors = operation.get("colors") if isinstance(operation.get("colors"), list) else [operation.get("color", operation.get("target_color"))]
        masks = [_color_range_mask(image, color, operation.get("tolerance", operation.get("threshold", 24))) for color in colors if color not in (None, "")]
        return _combine_masks(masks, image.size)
    if mode in {"alpha", "transparency"}:
        return _alpha_threshold_mask(image, operation)
    return None


def _refine_mask(mask: Image.Image, operation: dict[str, Any]) -> Image.Image:
    refined = mask.convert("L")
    expand = int(round(_number(operation.get("expand", operation.get("grow")), 0)))
    shrink = int(round(_number(operation.get("shrink", operation.get("erode")), 0)))
    smooth = int(round(_number(operation.get("smooth"), 0)))
    if expand > 0:
        refined = refined.filter(ImageFilter.MaxFilter(expand * 2 + 1))
    if shrink > 0:
        refined = refined.filter(ImageFilter.MinFilter(shrink * 2 + 1))
    if smooth > 0:
        refined = refined.filter(ImageFilter.MedianFilter(smooth * 2 + 1))
    feather = _number(operation.get("feather", operation.get("blur")), 0)
    if feather > 0:
        refined = refined.filter(ImageFilter.GaussianBlur(radius=feather))
    return refined


def _should_invert_mask(operation: dict[str, Any]) -> bool:
    effect = str(operation.get("effect") or operation.get("action") or "").strip().lower()
    target = str(operation.get("target") or operation.get("area") or "").strip().lower()
    return bool(operation.get("invert")) or effect in {"keep", "isolate", "keep_selection"} or target in {"outside", "inverse", "non_selection"}


def _apply_alpha_with_mask(image: Image.Image, mask: Image.Image, alpha_value: int) -> Image.Image:
    current = image.copy().convert("RGBA")
    alpha = current.getchannel("A")
    replacement = Image.new("L", current.size, max(0, min(255, alpha_value)))
    next_alpha = Image.composite(replacement, alpha, mask)
    if alpha_value < 255:
        visibility = max(0.0, min(1.0, alpha_value / 255.0))
        faded = ImageEnhance.Brightness(current).enhance(max(0.08, visibility * 1.35))
        faded = ImageEnhance.Contrast(faded).enhance(0.65)
        current = Image.composite(faded, current, mask.convert("L"))
    current.putalpha(next_alpha)
    return current


def _apply_mask_edit(image: Image.Image, operation: dict[str, Any]) -> Image.Image:
    mask = _mask_for_edit_mask_operation(image, operation)
    if mask is None or mask.getbbox() is None:
        raise ValueError("mask operation requires a valid shape/color/background/alpha selection")
    if _should_invert_mask(operation):
        mask = ImageOps.invert(mask.convert("L"))
    mask = _refine_mask(mask, operation)
    effect = str(operation.get("effect") or operation.get("action") or "transparent").strip().lower()
    if effect in {"transparent", "clear", "erase", "remove", "keep", "isolate", "keep_selection"}:
        alpha_value = int(round(_clamp(_number(operation.get("alpha"), 0.0), 0.0, 1.0) * 255))
        return _apply_alpha_with_mask(image, mask, alpha_value)
    if effect in {"opaque", "restore_alpha"}:
        return _apply_alpha_with_mask(image, mask, 255)
    if effect in {"fill", "color"}:
        fill_op = {
            **operation,
            "style": {
                "type": "solid",
                "color": operation.get("fill_color", operation.get("color", "#ffffff")),
                "opacity": operation.get("opacity", 1),
            },
        }
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay.paste(Image.new("RGBA", image.size, _rgba(fill_op["style"]["color"], fill_op["style"]["opacity"])), (0, 0), mask)
        current = image.copy().convert("RGBA")
        current.alpha_composite(overlay)
        return current
    raise ValueError(f"unsupported mask effect: {effect}")


def _mask_area(mask: Image.Image) -> int:
    arr = np.array(mask.convert("L"), dtype=np.uint8)
    return int(np.count_nonzero(arr > 8))


def _bbox_dict(bbox: tuple[int, int, int, int] | None) -> dict[str, int] | None:
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    return {
        "x": int(left),
        "y": int(top),
        "width": int(max(0, right - left)),
        "height": int(max(0, bottom - top)),
        "left": int(left),
        "top": int(top),
        "right": int(right),
        "bottom": int(bottom),
    }


def _scale_image_for_segmentation(image: Image.Image) -> tuple[Image.Image, float]:
    max_dim = max(image.size)
    if max_dim <= _SEGMENT_MAX_DIMENSION:
        return image, 1.0
    scale = _SEGMENT_MAX_DIMENSION / float(max_dim)
    size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    return image.resize(size, Image.Resampling.LANCZOS), scale


def _scaled_rect(rect: tuple[int, int, int, int] | None, scale: float, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    if rect is None:
        return None
    left, top, right, bottom = rect
    scaled = (
        int(round(left * scale)),
        int(round(top * scale)),
        int(round(right * scale)),
        int(round(bottom * scale)),
    )
    x1 = max(0, min(scaled[0], size[0] - 1))
    y1 = max(0, min(scaled[1], size[1] - 1))
    x2 = max(x1 + 1, min(scaled[2], size[0]))
    y2 = max(y1 + 1, min(scaled[3], size[1]))
    return (x1, y1, x2, y2)


def _auto_grabcut_rect(size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    margin_x = max(1, int(round(width * 0.04)))
    margin_y = max(1, int(round(height * 0.04)))
    return (margin_x, margin_y, max(margin_x + 1, width - margin_x), max(margin_y + 1, height - margin_y))


def _grabcut_mask(
    image: Image.Image,
    *,
    rect: tuple[int, int, int, int] | None = None,
    foreground_points: list[tuple[float, float]] | None = None,
    background_points: list[tuple[float, float]] | None = None,
    iterations: int = 5,
) -> Image.Image | None:
    if image.width < 2 or image.height < 2:
        return None
    working, scale = _scale_image_for_segmentation(image.convert("RGBA"))
    rgb = np.array(working.convert("RGB"), dtype=np.uint8)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    mask = np.full((working.height, working.width), cv2.GC_PR_BGD, dtype=np.uint8)
    scaled_rect = _scaled_rect(rect, scale, working.size) or _auto_grabcut_rect(working.size)
    x1, y1, x2, y2 = scaled_rect
    mask[y1:y2, x1:x2] = cv2.GC_PR_FGD
    mask[0, :] = cv2.GC_BGD
    mask[-1, :] = cv2.GC_BGD
    mask[:, 0] = cv2.GC_BGD
    mask[:, -1] = cv2.GC_BGD

    radius = max(2, int(round(min(working.size) * 0.012)))
    for point in foreground_points or []:
        px = int(round(_clamp(point[0] * scale, 0, working.width - 1)))
        py = int(round(_clamp(point[1] * scale, 0, working.height - 1)))
        cv2.circle(mask, (px, py), radius, cv2.GC_FGD, -1)
    for point in background_points or []:
        px = int(round(_clamp(point[0] * scale, 0, working.width - 1)))
        py = int(round(_clamp(point[1] * scale, 0, working.height - 1)))
        cv2.circle(mask, (px, py), radius, cv2.GC_BGD, -1)

    try:
        bgd_model = np.zeros((1, 65), np.float64)
        fgd_model = np.zeros((1, 65), np.float64)
        cv2.grabCut(
            bgr,
            mask,
            None,
            bgd_model,
            fgd_model,
            max(1, min(int(iterations), 10)),
            cv2.GC_INIT_WITH_MASK,
        )
    except cv2.error:
        return None

    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype("uint8")
    result = Image.fromarray(foreground, mode="L")
    if working.size != image.size:
        result = result.resize(image.size, Image.Resampling.LANCZOS)
    return result


def _alpha_subject_mask(image: Image.Image) -> Image.Image | None:
    alpha = image.convert("RGBA").getchannel("A")
    if alpha.getextrema() == (255, 255):
        return None
    bbox = alpha.point(lambda value: 255 if int(value) > 8 else 0).getbbox()
    if bbox is None:
        return None
    area = _mask_area(alpha)
    total = max(1, image.width * image.height)
    if area >= int(total * 0.995):
        return None
    return alpha


def _flood_subject_mask(image: Image.Image, *, tolerance: int) -> Image.Image | None:
    background = _background_flood_mask(
        image,
        {
            "tolerance": tolerance,
            "seed": "edges",
            "seed_step": 1 if max(image.size) <= 1024 else max(1, min(image.size) // 256),
        },
    )
    if background.getbbox() is None:
        return None
    subject = ImageOps.invert(background.convert("L"))
    existing_alpha = image.convert("RGBA").getchannel("A")
    subject = ImageChops.multiply(subject, existing_alpha)
    bbox = subject.point(lambda value: 255 if int(value) > 8 else 0).getbbox()
    if bbox is None:
        return None
    area = _mask_area(subject)
    total = max(1, image.width * image.height)
    if area <= max(16, int(total * 0.002)) or area >= int(total * 0.985):
        return None
    return subject


def _refine_segment_mask(mask: Image.Image, *, expand: int = 0, shrink: int = 0, feather: float = 1.0, smooth: int = 1) -> Image.Image:
    operation = {
        "expand": expand,
        "shrink": shrink,
        "feather": feather,
        "smooth": smooth,
    }
    refined = _refine_mask(mask, operation)
    return refined.point(lambda value: 255 if int(value) >= 128 else int(value))


def segment_image(
    image: Image.Image,
    *,
    rect: tuple[int, int, int, int] | None = None,
    foreground_points: list[tuple[float, float]] | None = None,
    background_points: list[tuple[float, float]] | None = None,
    method: str = "auto",
    background_tolerance: int = 28,
    expand: int = 0,
    shrink: int = 0,
    feather: float = 1.0,
    smooth: int = 1,
    grabcut_iterations: int = 5,
) -> dict[str, Any]:
    current = image.convert("RGBA")
    method_name = str(method or "auto").strip().lower()
    mask: Image.Image | None = None
    engine = method_name
    if method_name in {"auto", "alpha"}:
        mask = _alpha_subject_mask(current)
        if mask is not None:
            engine = "alpha"
    if mask is None and method_name in {"auto", "background", "flood", "floodfill"}:
        mask = _flood_subject_mask(current, tolerance=max(0, min(255, int(background_tolerance))))
        if mask is not None:
            engine = "background_flood"
    if mask is None and method_name in {"auto", "grabcut", "opencv", "main_subject"}:
        mask = _grabcut_mask(
            current,
            rect=rect,
            foreground_points=foreground_points,
            background_points=background_points,
            iterations=grabcut_iterations,
        )
        if mask is not None:
            engine = "opencv_grabcut"
    if mask is None or mask.getbbox() is None:
        raise ValueError("Unable to segment a foreground subject from this image")

    refined = _refine_segment_mask(mask, expand=expand, shrink=shrink, feather=feather, smooth=smooth)
    bbox = refined.point(lambda value: 255 if int(value) > 8 else 0).getbbox()
    if bbox is None:
        raise ValueError("Segmentation produced an empty subject mask")
    cutout = current.copy()
    cutout.putalpha(ImageChops.multiply(current.getchannel("A"), refined))
    area = _mask_area(refined)
    total = max(1, current.width * current.height)
    return {
        "image": cutout,
        "mask": refined,
        "bbox": bbox,
        "engine": engine,
        "subject_area_ratio": round(area / total, 4),
    }


def _mask_for_operation(operation: dict[str, Any], size: tuple[int, int]) -> Image.Image | None:
    return _mask_for_shape_operation(operation, size)


def _font(size: int) -> ImageFont.ImageFont:
    font_size = max(8, min(512, int(size)))
    for path in _FONT_CANDIDATES:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_arrow(draw: ImageDraw.ImageDraw, start: tuple[float, float], end: tuple[float, float], color: tuple[int, int, int, int], width: int, head_size: int) -> None:
    draw.line([start, end], fill=color, width=width)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return
    angle = math.atan2(dy, dx)
    head = max(head_size, width * 3)
    spread = math.radians(28)
    left = (
        end[0] - head * math.cos(angle - spread),
        end[1] - head * math.sin(angle - spread),
    )
    right = (
        end[0] - head * math.cos(angle + spread),
        end[1] - head * math.sin(angle + spread),
    )
    draw.polygon([end, left, right], fill=color)


def _draw_mesh_lines(
    draw: ImageDraw.ImageDraw,
    bbox: tuple[int, int, int, int],
    color: tuple[int, int, int, int],
    spacing: int,
    line_width: int,
) -> None:
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    step = max(6, spacing)
    samples = max(18, min(96, width // 12 if width >= height else height // 12))
    amplitude = max(2.0, min(width, height) * 0.035)

    y = top - step
    row = 0
    while y <= bottom + step:
        points: list[tuple[float, float]] = []
        phase = row * 0.72
        for i in range(samples + 1):
            t = i / samples
            x = left + width * t
            yy = y + math.sin(t * math.pi * 2.0 + phase) * amplitude
            points.append((x, yy))
        draw.line(points, fill=color, width=line_width, joint="curve")
        y += step
        row += 1

    x = left - step
    col = 0
    while x <= right + step:
        points = []
        phase = col * 0.66
        for i in range(samples + 1):
            t = i / samples
            xx = x + math.sin(t * math.pi * 2.0 + phase) * amplitude
            yy = top + height * t
            points.append((xx, yy))
        draw.line(points, fill=color, width=line_width, joint="curve")
        x += step
        col += 1

    diagonal_alpha = max(24, min(180, int(color[3] * 0.55)))
    diagonal_color = (color[0], color[1], color[2], diagonal_alpha)
    for offset in range(-height, width + height, step * 2):
        points = []
        for i in range(samples + 1):
            t = i / samples
            x_pos = left + offset + (width + height) * t
            y_pos = bottom - height * t + math.sin(t * math.pi * 1.5 + offset * 0.02) * amplitude
            points.append((x_pos, y_pos))
        draw.line(points, fill=diagonal_color, width=max(1, line_width - 1), joint="curve")


def _wireframe_mask(
    image: Image.Image,
    mask: Image.Image,
    style: dict[str, Any],
    operation: dict[str, Any],
) -> Image.Image:
    gray = ImageOps.autocontrast(image.convert("L"))
    strength = _clamp(_number(style.get("strength", operation.get("strength")), 0.72), 0.05, 1.0)
    spacing = max(10, min(96, int(_number(style.get("spacing", operation.get("spacing")), 32))))
    line_width = max(1, min(12, int(_number(style.get("line_width", operation.get("line_width")), 2))))
    bbox = mask.getbbox()
    if bbox is None:
        return Image.new("L", image.size, 0)

    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    amplitude = max(2.0, min(width, height) * (0.025 + strength * 0.04))
    form = gray.filter(ImageFilter.GaussianBlur(radius=max(2.0, spacing * 0.18)))

    def pixel(x: float, y: float) -> int:
        px = int(round(_clamp(x, 0, image.width - 1)))
        py = int(round(_clamp(y, 0, image.height - 1)))
        return int(form.getpixel((px, py)))

    def warped_point(x: float, y: float, normal_x: float, normal_y: float) -> tuple[float, float]:
        normal_len = math.hypot(normal_x, normal_y) or 1.0
        nx = normal_x / normal_len
        ny = normal_y / normal_len
        lum = (pixel(x, y) - 128) / 128.0
        gradient_x = (pixel(x + spacing * 0.25, y) - pixel(x - spacing * 0.25, y)) / 255.0
        gradient_y = (pixel(x, y + spacing * 0.25) - pixel(x, y - spacing * 0.25)) / 255.0
        relief = (lum * 0.42 + (gradient_x * nx + gradient_y * ny) * 1.05) * amplitude
        tangent = (gradient_x * -ny + gradient_y * nx) * amplitude * 0.22
        return (
            _clamp(x + nx * relief - ny * tangent, left, right),
            _clamp(y + ny * relief + nx * tangent, top, bottom),
        )

    line_mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(line_mask)
    mesh_width = max(1, line_width)
    cx = left + width / 2
    cy = top + height / 2
    rx = width / 2
    ry = height / 2
    line_count = max(5, min(15, int(min(width, height) / max(12, spacing * 0.62)) + 4))
    surface_samples = max(72, min(240, int((width + height) / 5)))

    for index in range(line_count):
        value = -0.92 + 1.84 * index / max(1, line_count - 1)
        envelope = math.sqrt(max(0.0, 1.0 - value * value))
        points: list[tuple[float, float]] = []
        for sample in range(surface_samples + 1):
            u = -1.0 + 2.0 * sample / surface_samples
            x = cx + rx * u * envelope
            y = cy + ry * value
            points.append(warped_point(x, y, u, value))
        draw.line(points, fill=235, width=mesh_width, joint="curve")

    for index in range(line_count):
        value = -0.92 + 1.84 * index / max(1, line_count - 1)
        points = []
        for sample in range(surface_samples + 1):
            v = -1.0 + 2.0 * sample / surface_samples
            envelope = math.sqrt(max(0.0, 1.0 - v * v))
            x = cx + rx * value * envelope
            y = cy + ry * v
            points.append(warped_point(x, y, value, v))
        draw.line(points, fill=210, width=mesh_width, joint="curve")

    ring_count = max(2, min(8, line_count // 2 + 1))
    ring_samples = max(120, min(300, surface_samples + 40))
    for index in range(1, ring_count + 1):
        scale = (index / (ring_count + 1)) ** 0.78
        points = []
        for sample in range(ring_samples + 1):
            theta = math.tau * sample / ring_samples
            normal_x = math.cos(theta)
            normal_y = math.sin(theta)
            x = cx + rx * scale * normal_x
            y = cy + ry * scale * normal_y
            points.append(warped_point(x, y, normal_x, normal_y))
        draw.line(points, fill=185, width=max(1, mesh_width - 1), joint="curve")

    edge_threshold = int(round(244 - strength * 48))
    edges = form.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(radius=0.6))
    edges = edges.point(lambda value: 255 if int(value) >= edge_threshold else 0)
    if line_width > 1:
        edges = edges.filter(ImageFilter.MaxFilter(3))
    edges = edges.point(lambda value: int(int(value) * 0.55))

    combined = ImageChops.lighter(line_mask, edges)
    combined = ImageChops.multiply(combined.convert("L"), mask.convert("L"))
    return combined


def _apply_wireframe_fill(
    image: Image.Image,
    mask: Image.Image,
    style: dict[str, Any],
    operation: dict[str, Any],
    color: Any,
    opacity: Any,
) -> Image.Image:
    wire_mask = _wireframe_mask(image, mask, style, operation)
    if wire_mask.getbbox() is None:
        return image
    base = image.copy().convert("RGBA")
    region = ImageEnhance.Color(base).enhance(0.18)
    region = ImageEnhance.Brightness(region).enhance(0.24)
    region = ImageEnhance.Contrast(region).enhance(1.35)
    image.paste(region, (0, 0), mask)
    rgba = _rgba(color, opacity)
    alpha = wire_mask.point(lambda value: int(round(int(value) * rgba[3] / 255)))
    layer = Image.new("RGBA", image.size, (rgba[0], rgba[1], rgba[2], 0))
    layer.putalpha(alpha)
    image.alpha_composite(layer)
    return image


def _odd_kernel(value: int, lower: int, upper: int) -> int:
    number = max(lower, min(upper, int(value)))
    return number if number % 2 == 1 else number + 1 if number < upper else number - 1


def _auto_canny_thresholds(gray: np.ndarray) -> tuple[int, int]:
    median = float(np.median(gray))
    if median <= 0:
        return 24, 72
    lower = int(max(0, (1.0 - 0.42) * median))
    upper = int(min(255, (1.0 + 0.42) * median))
    if upper <= lower:
        upper = min(255, lower + 48)
    return lower, upper


def _draw_filtered_contours(
    target: np.ndarray,
    contours: list[np.ndarray],
    *,
    min_length: float,
    width: int,
    height: int,
    value: int,
) -> None:
    for contour in contours:
        length = cv2.arcLength(contour, closed=False)
        if length < min_length:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 2 or h <= 2:
            continue
        if x <= 1 and y <= 1 and x + w >= width - 2 and y + h >= height - 2:
            continue
        cv2.drawContours(target, [contour], -1, value, 1, lineType=cv2.LINE_AA)


def _opencv_curve_image(
    image: Image.Image,
    *,
    color: Any = "#22d3ee",
    detail: Any = 0.78,
    line_strength: Any = 0.92,
    base_visibility: Any = 0.12,
) -> Image.Image:
    source = image.copy().convert("RGBA")
    rgb = np.array(source.convert("RGB"), dtype=np.uint8)
    alpha = np.array(source.getchannel("A"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    if width <= 1 or height <= 1:
        return source

    detail_value = _clamp(_number(detail, 0.78), 0.05, 1.0)
    strength = _clamp(_number(line_strength, 0.92), 0.1, 1.0)
    visibility = _clamp(_number(base_visibility, 0.12), 0.0, 0.45)
    line_color = np.array(_rgba(color, 1.0)[:3], dtype=np.float32)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    denoised = cv2.bilateralFilter(gray, 7, 55, 55)
    clahe = cv2.createCLAHE(clipLimit=1.5 + detail_value * 2.8, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)
    blur_sigma = max(0.35, 1.9 - detail_value * 1.15)
    smooth = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=blur_sigma, sigmaY=blur_sigma)

    min_dim = max(1, min(width, height))
    block_base = int(round(min_dim / (28 + detail_value * 52)))
    block_size = _odd_kernel(block_base, 11, 91)
    adaptive_c = int(round(8 - detail_value * 4))
    adaptive = cv2.adaptiveThreshold(
        smooth,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        max(2, adaptive_c),
    )

    lower, upper = _auto_canny_thresholds(smooth)
    edges = cv2.Canny(smooth, lower, upper, apertureSize=3, L2gradient=True)
    edge_contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    edge_mask = np.zeros_like(gray)
    min_edge_length = max(12.0, min_dim * (0.004 + (1.0 - detail_value) * 0.01))
    _draw_filtered_contours(
        edge_mask,
        edge_contours,
        min_length=min_edge_length,
        width=width,
        height=height,
        value=255,
    )

    contour_mask = np.zeros_like(gray)
    level_count = int(round(10 + detail_value * 24))
    min_contour_length = max(18.0, min_dim * (0.01 + (1.0 - detail_value) * 0.018))
    for level in np.linspace(24, 232, level_count):
        _, binary = cv2.threshold(smooth, int(level), 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        _draw_filtered_contours(
            contour_mask,
            contours,
            min_length=min_contour_length,
            width=width,
            height=height,
            value=210,
        )

    flow_mask = np.zeros_like(gray)
    grad_x = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(grad_x, grad_y)
    active_threshold = float(np.percentile(magnitude, max(18, 58 - detail_value * 34)))
    seed_spacing = max(12, min(52, int(round(min_dim / (38 + detail_value * 58)))))
    trace_steps = max(10, min(38, int(round(min_dim / 54))))
    step_length = max(2.0, min(5.0, min_dim / 520))

    def trace_direction(start_x: float, start_y: float, direction: float) -> list[tuple[int, int]]:
        points: list[tuple[int, int]] = []
        x = start_x
        y = start_y
        last_tx = 0.0
        last_ty = 0.0
        for _ in range(trace_steps):
            ix = int(round(x))
            iy = int(round(y))
            if ix < 1 or iy < 1 or ix >= width - 1 or iy >= height - 1:
                break
            if float(magnitude[iy, ix]) < active_threshold * 0.45:
                break
            gx = float(grad_x[iy, ix])
            gy = float(grad_y[iy, ix])
            length = math.hypot(gx, gy)
            if length <= 0.001:
                break
            tx = -gy / length
            ty = gx / length
            if last_tx * tx + last_ty * ty < 0:
                tx = -tx
                ty = -ty
            last_tx, last_ty = tx, ty
            points.append((ix, iy))
            x += tx * step_length * direction
            y += ty * step_length * direction
        return points

    for seed_y in range(seed_spacing // 2, height, seed_spacing):
        for seed_x in range(seed_spacing // 2, width, seed_spacing):
            if float(magnitude[seed_y, seed_x]) < active_threshold:
                continue
            backward = trace_direction(float(seed_x), float(seed_y), -1.0)
            forward = trace_direction(float(seed_x), float(seed_y), 1.0)
            points = list(reversed(backward)) + forward[1:]
            if len(points) >= 5:
                cv2.polylines(flow_mask, [np.array(points, dtype=np.int32)], False, 170, 1, lineType=cv2.LINE_AA)

    adaptive = cv2.medianBlur(adaptive, 3)
    line_mask = cv2.max(edge_mask, contour_mask)
    line_mask = cv2.max(line_mask, flow_mask)
    line_mask = cv2.max(line_mask, (adaptive.astype(np.float32) * (0.38 + detail_value * 0.34)).astype(np.uint8))
    if min_dim >= 900:
        kernel = np.ones((2, 2), np.uint8)
        line_mask = cv2.dilate(line_mask, kernel, iterations=1)
    line_mask = cv2.GaussianBlur(line_mask, (3, 3), 0.45)

    gray_rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB).astype(np.float32)
    base_rgb = (gray_rgb * visibility).clip(0, 255)
    mask = ((line_mask.astype(np.float32) / 255.0) ** 0.72 * strength).clip(0.0, 1.0)
    mask_3 = mask[..., None]
    output_rgb = (base_rgb * (1.0 - mask_3) + line_color * mask_3).clip(0, 255).astype(np.uint8)
    output_alpha = np.where(alpha > 0, 255, 0).astype(np.uint8)
    output = np.dstack([output_rgb, output_alpha])
    return Image.fromarray(output, mode="RGBA")


def _apply_fill(image: Image.Image, operation: dict[str, Any]) -> Image.Image:
    size = image.size
    mask = _mask_for_operation(operation, size)
    if mask is None:
        raise ValueError("fill operation requires a valid rect or polygon selection")
    style = operation.get("style") if isinstance(operation.get("style"), dict) else {}
    fill_type = str(style.get("type") or operation.get("fill") or operation.get("fill_type") or "solid").strip().lower()
    color = style.get("color", operation.get("color", "#00d5ff"))
    opacity = style.get("opacity", operation.get("opacity", 0.45 if fill_type in {"transparent", "translucent"} else 1))
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    if fill_type in {"wireframe", "body_mesh"}:
        return _apply_wireframe_fill(image, mask, style, operation, color, opacity)
    if fill_type in {"mesh", "contour"}:
        bbox = mask.getbbox()
        if bbox is None:
            return image
        draw = ImageDraw.Draw(overlay)
        spacing = max(8, int(_number(style.get("spacing", operation.get("spacing")), 34)))
        line_width = max(1, int(_number(style.get("line_width", operation.get("line_width")), 2)))
        _draw_mesh_lines(draw, bbox, _rgba(color, opacity), spacing, line_width)
        clipped = Image.new("RGBA", size, (0, 0, 0, 0))
        clipped.paste(overlay, (0, 0), mask)
        image.alpha_composite(clipped)
        return image
    if fill_type in {"grid", "grid_lines", "lines"}:
        bbox = mask.getbbox()
        if bbox is None:
            return image
        draw = ImageDraw.Draw(overlay)
        spacing = max(4, int(_number(style.get("spacing", operation.get("spacing")), 24)))
        line_width = max(1, int(_number(style.get("line_width", operation.get("line_width")), 2)))
        rgba = _rgba(color, opacity)
        for x in range(bbox[0] - (bbox[0] % spacing), bbox[2] + spacing, spacing):
            draw.line([(x, bbox[1]), (x, bbox[3])], fill=rgba, width=line_width)
        for y in range(bbox[1] - (bbox[1] % spacing), bbox[3] + spacing, spacing):
            draw.line([(bbox[0], y), (bbox[2], y)], fill=rgba, width=line_width)
        clipped = Image.new("RGBA", size, (0, 0, 0, 0))
        clipped.paste(overlay, (0, 0), mask)
        image.alpha_composite(clipped)
        return image
    draw = ImageDraw.Draw(overlay)
    rgba = _rgba(color, opacity)
    if mask.getbbox():
        solid = Image.new("RGBA", size, rgba)
        overlay.paste(solid, (0, 0), mask)
        image.alpha_composite(overlay)
    return image


def apply_image_edit_operations(image: Image.Image, operations: list[dict[str, Any]]) -> Image.Image:
    current = image.copy().convert("RGBA")
    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise ValueError(f"operation {index} must be an object")
        kind = _operation_kind(operation)
        unit = _unit(operation)
        if kind == "crop":
            rect = _rect_box(operation.get("rect") or operation.get("box") or operation.get("bounds"), current.size, unit)
            if rect is None:
                raise ValueError("crop operation requires a valid rect")
            current = current.crop(rect)
            continue
        if kind == "brush":
            draw = ImageDraw.Draw(current)
            strokes = operation.get("strokes") if isinstance(operation.get("strokes"), list) else [operation]
            for stroke in strokes:
                if not isinstance(stroke, dict):
                    continue
                stroke_unit = _unit(stroke) if stroke.get("unit") else unit
                points = _points(stroke.get("points") or stroke.get("path"), current.size, stroke_unit)
                if len(points) < 2:
                    continue
                width = max(1, int(_number(stroke.get("width", stroke.get("brush_size", operation.get("width", 8))), 8)))
                color = _rgba(stroke.get("color", operation.get("color", "#00d5ff")), stroke.get("opacity", operation.get("opacity", 1)))
                draw.line(points, fill=color, width=width, joint="curve")
            continue
        if kind == "fill":
            current = _apply_fill(current, operation)
            continue
        if kind in {"mask", "selection", "segment"}:
            current = _apply_mask_edit(current, operation)
            continue
        if kind == "text":
            text = str(operation.get("text") or "")
            if not text:
                continue
            position = _point(operation.get("position") or operation.get("at") or {"x": operation.get("x"), "y": operation.get("y")}, current.size, unit)
            if position is None:
                raise ValueError("text operation requires a valid position")
            font_size = int(_number(operation.get("font_size", operation.get("size")), 36))
            draw = ImageDraw.Draw(current)
            draw.text(
                position,
                text,
                font=_font(font_size),
                fill=_rgba(operation.get("color", "#ffffff"), operation.get("opacity", 1)),
                stroke_width=max(0, int(_number(operation.get("stroke_width"), 0))),
                stroke_fill=_rgba(operation.get("stroke_color", "#000000"), operation.get("stroke_opacity", 1)),
            )
            continue
        if kind == "arrow":
            start = _point(operation.get("start") or operation.get("from"), current.size, unit)
            end = _point(operation.get("end") or operation.get("to"), current.size, unit)
            if start is None or end is None:
                raise ValueError("arrow operation requires start and end")
            width = max(1, int(_number(operation.get("width", operation.get("line_width")), 6)))
            head_size = max(width * 3, int(_number(operation.get("head_size"), width * 5)))
            draw = ImageDraw.Draw(current)
            _draw_arrow(draw, start, end, _rgba(operation.get("color", "#ffffff"), operation.get("opacity", 1)), width, head_size)
            continue
        raise ValueError(f"unsupported image edit operation: {kind or '<empty>'}")
    return current


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


async def _source_path_for_edit(
    project_id: str,
    *,
    node: WorkflowNode,
    source_ref: str | None = None,
    candidate_ref: str | None = None,
) -> tuple[Path | None, str]:
    refs: list[str] = []
    if candidate_ref:
        refs.append(str(candidate_ref))
    if source_ref:
        refs.append(str(source_ref))
    if not refs:
        refs.extend(_resolve_edit_source(_parse_json_dict(node.output_json)))
    for ref in refs:
        path = await resolve_image_path(project_id, ref)
        if path:
            return path, ref
    return None, refs[0] if refs else f"node:{_public_node_id(node)}"


async def edit_image_node(
    project_id: str,
    node_id: str,
    operations: list[dict[str, Any]] | None = None,
    *,
    action: str = "preview",
    source_ref: str | None = None,
    candidate_ref: str | None = None,
) -> dict[str, Any]:
    action = str(action or "preview").strip().lower()
    if action not in _EDIT_ACTIONS:
        return {"ok": False, "error": "action must be preview or commit", "error_kind": "invalid_action"}
    op_list = operations or []
    if not isinstance(op_list, list):
        return {"ok": False, "error": "operations must be an array", "error_kind": "invalid_operations"}
    if len(op_list) > _EDIT_OPERATION_LIMIT:
        return {
            "ok": False,
            "error": f"operations exceeds limit {_EDIT_OPERATION_LIMIT}",
            "error_kind": "too_many_operations",
        }

    node = await _node(project_id, node_id)
    if not node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
    if node.type != "image":
        return {
            "ok": False,
            "error": "image.edit only supports image nodes",
            "error_kind": "invalid_node_type",
            "node_id": _public_node_id(node),
            "node_type": node.type,
        }

    source_path, source_used = await _source_path_for_edit(
        project_id,
        node=node,
        source_ref=source_ref,
        candidate_ref=candidate_ref if action == "commit" else None,
    )
    if not source_path:
        return {
            "ok": False,
            "error": "source image not found",
            "error_kind": "source_image_unresolved",
            "node_id": _public_node_id(node),
            "source_ref": source_ref or candidate_ref or f"node:{_public_node_id(node)}",
        }

    try:
        edited = _open_image(source_path)
        if op_list:
            edited = apply_image_edit_operations(edited, op_list)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "image_edit_failed",
            "node_id": _public_node_id(node),
        }

    if action == "preview":
        saved = _save_image(project_id, edited, f"edit-preview-{_public_node_id(node)}")
        model_content = await _model_content_for_saved_image(
            project_id,
            saved,
            label="preview",
            source_ref=saved["local_url"],
            note="Preview candidate generated by image.edit; this image is attached to the current model context.",
        )
        return {
            "ok": True,
            "action": "preview",
            "node_id": _public_node_id(node),
            "candidate_ref": saved["local_url"],
            "image": saved,
            "operation_count": len(op_list),
            "source_ref": source_used,
            "suggested_next": "The preview image is attached to the next model turn as visual context. Judge it directly; commit this candidate_ref if acceptable, or preview again from base_ref/checkpoint if not.",
            **model_content,
        }

    saved = _save_image(project_id, edited, f"edit-final-{_public_node_id(node)}")
    async with session_scope() as session:
        resolved_id = await resolve_internal_node_id(session, project_id, node_id)
        current = await session.get(WorkflowNode, resolved_id or node_id)
        if not current or current.project_id != project_id:
            return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
        current_output = _parse_json_dict(current.output_json)
        current_input = _parse_json_dict(current.input_json)
        archived = media_history.archive_current_media_output(
            current_output,
            node_type="image",
            prompt=media_history.prompt_from_state(current_output, current_input, current.prompt or current_input.get("prompt")),
            input_data=current_input,
        )
        history = media_history.media_history_from_output(archived)
        next_output: dict[str, Any] = {
            "ok": True,
            "type": "image",
            "operation": "image_edit",
            "status": "completed",
            "source_image": {
                "ref": source_used,
                "candidate_ref": candidate_ref,
            },
            "edit": {
                "operation_count": len(op_list),
                "operations": media_history.strip_media_history(op_list),
                "committed_at": datetime.utcnow().isoformat(),
            },
            **saved,
        }
        if history:
            next_output = media_history.attach_media_history(next_output, history)
        current.output_json = json.dumps(next_output, ensure_ascii=False)
        current.status = "completed"
        current.error_message = None
        current_input["render_state"] = "fresh"
        current.input_json = json.dumps(current_input, ensure_ascii=False)
        current.updated_at = datetime.utcnow()
        session.add(current)
        await session.commit()
        await session.refresh(current)
        public_id = _public_node_id(current)

    await _emit_node_update(project_id, str(resolved_id or node_id), next_output)
    cleanup = _cleanup_edit_temp_images(project_id, public_id, keep_paths=[Path(str(saved.get("local_path") or ""))])
    result = {
        "ok": True,
        "action": "commit",
        "node_id": public_id,
        "image": saved,
        "url": saved["url"],
        "local_url": saved["local_url"],
        "width": saved["width"],
        "height": saved["height"],
        "history_count": len(history),
        "operation_count": len(op_list),
        "source_ref": source_used,
        "cleaned_temp_files": cleanup["deleted"],
    }
    if cleanup["errors"]:
        result["cleanup_errors"] = cleanup["errors"]
    result.update(await _model_content_for_saved_image(
        project_id,
        saved,
        label="committed",
        source_ref=saved["local_url"],
        note="Committed image edit result.",
    ))
    return result


async def segment_image_node(
    project_id: str,
    node_id: str | None = None,
    *,
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
    node: WorkflowNode | None = None
    public_node_id = str(node_id or "").strip()
    source_used = str(source_ref or "").strip()
    source_path: Path | None = None

    if node_id:
        node = await _node(project_id, str(node_id))
        if not node:
            return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
        if node.type != "image":
            return {
                "ok": False,
                "error": "image.segment only supports image nodes",
                "error_kind": "invalid_node_type",
                "node_id": _public_node_id(node),
                "node_type": node.type,
            }
        public_node_id = _public_node_id(node)
        source_path, source_used = await _source_path_for_edit(project_id, node=node, source_ref=source_ref)
    elif source_used:
        source_path = await resolve_image_path(project_id, source_used)
    else:
        return {"ok": False, "error": "node_id or source_ref is required", "error_kind": "missing_source"}

    if not source_path:
        return {
            "ok": False,
            "error": "source image not found",
            "error_kind": "source_image_unresolved",
            "node_id": public_node_id,
            "source_ref": source_ref or (f"node:{public_node_id}" if public_node_id else ""),
        }

    try:
        image = _open_image(source_path)
        coordinate_unit = "pixel" if str(unit or "").lower() in {"pixel", "pixels", "px"} else "normalized"
        box = _rect_box(rect or bbox, image.size, coordinate_unit)
        fg_points = _points(foreground_points or [], image.size, coordinate_unit)
        bg_points = _points(background_points or [], image.size, coordinate_unit)
        segmented = segment_image(
            image,
            rect=box,
            foreground_points=fg_points,
            background_points=bg_points,
            method=method,
            background_tolerance=background_tolerance,
            expand=expand,
            shrink=shrink,
            feather=feather,
            smooth=smooth,
            grabcut_iterations=grabcut_iterations,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "image_segment_failed",
            "node_id": public_node_id,
            "source_ref": source_used,
        }

    label = public_node_id or "source"
    cutout_saved = _save_image(project_id, segmented["image"], f"segment-cutout-{label}")
    mask_saved = _save_image(project_id, segmented["mask"].convert("L"), f"segment-mask-{label}")
    bbox_xyxy = [int(value) for value in segmented["bbox"]]
    return {
        "ok": True,
        "action": "segment",
        "node_id": public_node_id,
        "target": str(target or "main_subject"),
        "source_ref": source_used,
        "engine": segmented["engine"],
        "cutout_ref": cutout_saved["local_url"],
        "mask_ref": mask_saved["local_url"],
        "cutout": cutout_saved,
        "mask": mask_saved,
        "bbox": _bbox_dict(segmented["bbox"]),
        "bbox_xyxy": bbox_xyxy,
        "subject_area_ratio": segmented["subject_area_ratio"],
        "suggested_next": (
            "Use image.edit with source_ref=cutout_ref for crop, square icon normalization, rounded-corner alpha, "
            "then inspect with vision.view_image and commit when acceptable."
        ),
    }


async def preview_curve_image_node(
    project_id: str,
    node_id: str,
    *,
    source_ref: str | None = None,
    color: str = "#22d3ee",
    detail: float = 0.78,
    line_strength: float = 0.92,
    base_visibility: float = 0.12,
) -> dict[str, Any]:
    node = await _node(project_id, node_id)
    if not node:
        return {"ok": False, "error": "Node not found", "error_kind": "node_not_found", "node_id": node_id}
    if node.type != "image":
        return {
            "ok": False,
            "error": "image curve preview only supports image nodes",
            "error_kind": "invalid_node_type",
            "node_id": _public_node_id(node),
            "node_type": node.type,
        }

    source_path, source_used = await _source_path_for_edit(
        project_id,
        node=node,
        source_ref=source_ref,
    )
    if not source_path:
        return {
            "ok": False,
            "error": "source image not found",
            "error_kind": "source_image_unresolved",
            "node_id": _public_node_id(node),
            "source_ref": source_ref or f"node:{_public_node_id(node)}",
        }

    try:
        curve = _opencv_curve_image(
            _open_image(source_path),
            color=color,
            detail=detail,
            line_strength=line_strength,
            base_visibility=base_visibility,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "image_curve_failed",
            "node_id": _public_node_id(node),
        }

    saved = _save_image(project_id, curve, f"curve-preview-{_public_node_id(node)}")
    return {
        "ok": True,
        "action": "curve_preview",
        "node_id": _public_node_id(node),
        "candidate_ref": saved["local_url"],
        "image": saved,
        "source_ref": source_used,
        "curve": {
            "engine": "opencv",
            "detail": _clamp(_number(detail, 0.78), 0.05, 1.0),
            "line_strength": _clamp(_number(line_strength, 0.92), 0.1, 1.0),
            "base_visibility": _clamp(_number(base_visibility, 0.12), 0.0, 0.45),
        },
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
