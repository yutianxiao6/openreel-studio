"""MCP compatibility wrappers for media generation.

The implementation lives in `app.services.media_generation`. Keep this module
thin so raw media generators can later be unregistered without moving business
logic again.
"""
from __future__ import annotations

import json

from app.db.models import Asset
from app.db.session import session_scope
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
