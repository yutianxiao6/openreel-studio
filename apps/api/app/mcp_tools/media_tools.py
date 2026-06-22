"""MCP compatibility wrappers for media generation.

The implementation lives in `app.services.media_generation`. Keep this module
thin so raw media generators can later be unregistered without moving business
logic again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from app.db.models import Asset
from app.db.session import session_scope
from app.mcp_tools.file_tools import _safe_path
from app.services import media_generation


async def cancel_image_generation(project_id: str, reason: str = "") -> dict:
    """Request cancellation of the active image/chat generation for a project."""
    from app.agent import message_queue as mq

    result = await mq.request_cancel(
        project_id,
        reason or "用户要求停止图片生成",
    )
    return {
        **result,
        "status": "cancel_requested",
        "message": "已请求停止当前图片生成。若外部图片服务已接收请求，系统会在下一个安全点停止后续执行和写回。",
    }


async def generate_image(
    project_id: str,
    prompt: str,
    negative_prompt: str | None = None,
    aspect_ratio: str = "9:16",
    size: str | None = None,
    shot_id: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    reference_images: list[str] | None = None,
) -> dict:
    return await media_generation.generate_image(
        project_id=project_id,
        prompt=prompt,
        negative_prompt=negative_prompt,
        aspect_ratio=aspect_ratio,
        size=size,
        shot_id=shot_id,
        node_id=node_id,
        model=model,
        n=n,
        reference_images=reference_images,
    )


async def generate_first_frame(
    project_id: str,
    shot_id: str,
    prompt: str,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    aspect_ratio: str = "16:9",
    size: str | None = None,
    reference_images: list[str] | None = None,
) -> dict:
    return await media_generation.generate_first_frame(
        project_id=project_id,
        shot_id=shot_id,
        prompt=prompt,
        node_id=node_id,
        model=model,
        n=n,
        aspect_ratio=aspect_ratio,
        size=size,
        reference_images=reference_images,
    )


async def generate_last_frame(
    project_id: str,
    shot_id: str,
    prompt: str,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    aspect_ratio: str = "16:9",
    size: str | None = None,
    reference_images: list[str] | None = None,
) -> dict:
    return await media_generation.generate_last_frame(
        project_id=project_id,
        shot_id=shot_id,
        prompt=prompt,
        node_id=node_id,
        model=model,
        n=n,
        aspect_ratio=aspect_ratio,
        size=size,
        reference_images=reference_images,
    )


async def generate_video(
    project_id: str,
    prompt: str,
    shot_id: str | None = None,
    first_frame_asset_id: str | None = None,
    last_frame_asset_id: str | None = None,
    duration_seconds: int = 4,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    reference_images: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    return await media_generation.generate_video(
        project_id=project_id,
        prompt=prompt,
        shot_id=shot_id,
        first_frame_asset_id=first_frame_asset_id,
        last_frame_asset_id=last_frame_asset_id,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        node_id=node_id,
        model=model,
        reference_images=reference_images,
        extra=extra,
    )


async def get_media_status(asset_id: str) -> dict:
    async with session_scope() as session:
        asset = await session.get(Asset, asset_id)
        if not asset:
            return {"error": "Asset not found"}
        meta = json.loads(asset.metadata_json or "{}")
        return {
            "id": asset.id,
            "type": asset.type,
            "status": meta.get("status", "unknown"),
            "path": asset.path,
            "url": asset.url,
            "metadata": meta,
        }


_NODE_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}$")


def _url_path(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith(("http://", "https://")):
        return urlsplit(text).path
    return text


def _media_rel_path(project_id: str, value: str) -> str | None:
    path = _url_path(value)
    prefix = f"/api/media/{project_id}/"
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):].lstrip("/")
    if rel.startswith(("generated_images/", "generated_videos/")):
        return rel
    return f"generated_images/{rel}"


def _upload_rel_path(project_id: str, value: str) -> str | None:
    path = _url_path(value)
    prefix = f"/api/uploads/{project_id}/file/"
    if path.startswith(prefix):
        return path[len(prefix):].lstrip("/")
    return None


def _existing_rel_path(project_id: str, value: str) -> str | None:
    text = str(value or "").strip().lstrip("/")
    if not text or text.startswith(("http://", "https://", "data:")):
        return None
    if text.startswith(("/api/media/", "/api/uploads/")):
        return None
    if text.startswith(("uploads/", "generated_images/")):
        return text
    try:
        target = _safe_path(project_id, text)
    except ValueError:
        return None
    return text if target.exists() and target.is_file() else None


async def describe_image(
    project_id: str,
    rel_path: str = "",
    node_id: str | None = None,
    source_path: str | None = None,
    url: str | None = None,
    user_context: str | None = None,
    force: bool = False,
) -> dict:
    """Analyze an uploaded or generated image and persist it as a reference asset."""
    from app.mcp_tools.reference_tools import reference_manage

    raw_source = str(source_path or url or rel_path or "").strip()
    node_ref = str(node_id or "").strip()
    register_input: dict[str, object] = {
        "project_id": project_id,
        "action": "register",
    }
    resolved_path = ""
    if node_ref:
        register_input["node_id"] = node_ref[5:].strip() if node_ref.startswith("node:") else node_ref
    elif raw_source.startswith("node:"):
        register_input["node_id"] = raw_source[5:].strip()
    elif _NODE_ID_RE.match(raw_source):
        register_input["node_id"] = raw_source
    else:
        rel = (
            _media_rel_path(project_id, raw_source)
            or _upload_rel_path(project_id, raw_source)
            or _existing_rel_path(project_id, raw_source)
        )
        if rel:
            register_input["rel_path"] = rel
            resolved_path = rel
        elif raw_source.startswith(("http://", "https://")):
            register_input["url"] = raw_source
            resolved_path = raw_source
        else:
            raw_path = Path(raw_source).expanduser()
            if raw_path.is_absolute() and raw_path.exists() and raw_path.is_file():
                register_input["source_path"] = str(raw_path.resolve())
                resolved_path = str(raw_path.resolve())
            else:
                return {
                    "ok": False,
                    "error": "File not found",
                    "error_kind": "file_not_found",
                    "source": raw_source,
                    "hint": (
                        "media.describe_image 支持 uploads/...、generated_images/...、"
                        "/api/uploads/...、/api/media/...、node_id/node:<id>、本地绝对路径或 http(s) URL。"
                    ),
                }

    registered = await reference_manage(**register_input)
    if not isinstance(registered, dict) or not registered.get("ok"):
        return registered
    asset = registered.get("asset") if isinstance(registered.get("asset"), dict) else {}
    analyze_input: dict[str, object] = {
        "project_id": project_id,
        "action": "analyze",
        "include_analysis": True,
        "force": force,
    }
    if asset.get("ref_id"):
        analyze_input["ref_id"] = asset["ref_id"]
    elif register_input.get("node_id"):
        analyze_input["node_id"] = register_input["node_id"]
    elif register_input.get("rel_path"):
        analyze_input["rel_path"] = register_input["rel_path"]
    elif register_input.get("source_path"):
        analyze_input["source_path"] = register_input["source_path"]
    elif register_input.get("url"):
        analyze_input["url"] = register_input["url"]
    if user_context:
        analyze_input["user_context"] = user_context

    result = await reference_manage(**analyze_input)
    asset = result.get("asset") if isinstance(result, dict) else None
    analysis = asset.get("analysis") if isinstance(asset, dict) and isinstance(asset.get("analysis"), dict) else {}
    reference_input = asset.get("reference_input") if isinstance(asset, dict) else None
    path = str(reference_input or resolved_path or raw_source)
    size = asset.get("size") if isinstance(asset, dict) else None
    if not isinstance(size, int):
        try:
            local_rel = asset.get("rel_path") if isinstance(asset, dict) else ""
            target = _safe_path(project_id, str(local_rel or ""))
            size = target.stat().st_size if target.exists() and target.is_file() else None
        except Exception:
            size = None
    return {
        **result,
        "path": path,
        "description": analysis.get("summary") or analysis.get("prompt_fragment") or "",
        "style_tags": analysis.get("style_tags") or [],
        "prompt_fragment": analysis.get("prompt_fragment") or "",
        **({"size": size} if isinstance(size, int) else {}),
    }


async def generate_panorama(
    project_id: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    scene_id: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    reference_images: list[str] | None = None,
) -> dict:
    return await media_generation.generate_panorama(
        project_id=project_id,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        scene_id=scene_id,
        node_id=node_id,
        model=model,
        n=n,
        reference_images=reference_images,
    )


async def crop_panorama(
    project_id: str,
    panorama_asset_id: str,
    mode: str = "single",
    direction: str | None = None,
    node_id: str | None = None,
) -> dict:
    return await media_generation.crop_panorama(
        project_id=project_id,
        panorama_asset_id=panorama_asset_id,
        mode=mode,
        direction=direction,
        node_id=node_id,
    )


async def generate_story_template(
    project_id: str,
    segment_id: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    size: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    reference_images: list[str] | None = None,
) -> dict:
    return await media_generation.generate_story_template(
        project_id=project_id,
        segment_id=segment_id,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        size=size,
        node_id=node_id,
        model=model,
        n=n,
        reference_images=reference_images,
    )


async def get_presets(
    model_name: str | None = None,
) -> dict:
    """Get recommended default parameters for image providers."""
    from app.services.media_provider import match_preset, list_presets, get_preset_descriptions

    if model_name:
        preset = match_preset(model_name)
        return {
            "model_name": model_name,
            "preset": preset or {},
            "descriptions": get_preset_descriptions(),
        }

    return {
        "presets": list_presets(),
        "descriptions": get_preset_descriptions(),
    }
