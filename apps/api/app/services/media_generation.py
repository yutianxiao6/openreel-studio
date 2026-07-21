"""Internal media generation service used by node runners.

This module is intentionally not an MCP tool registry surface. Public/legacy
tool wrappers may delegate here, while `node.run` should call these functions
directly so media generation is an internal service behind the node protocol.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.config import settings
from app.db.models import Asset
from app.db.session import session_scope
from app.mcp_tools.shot_tools import register_asset
from app.services.media_provider import (
    generate_audio_with_provider,
    generate_image_with_provider,
    generate_video_with_provider,
    poll_audio_with_provider,
    poll_video_with_provider,
)
from app.services import media_history


_MAX_N = 10
logger = logging.getLogger(__name__)
_BACKGROUND_VIDEO_TASKS: dict[tuple[str, str, str], asyncio.Task] = {}
_BACKGROUND_AUDIO_TASKS: set[asyncio.Task] = set()


def _validate_n(n: int) -> int | None:
    """Returns coerced n in [1, _MAX_N], or None if invalid."""
    try:
        n_int = int(n)
    except (TypeError, ValueError):
        return None
    if n_int < 1 or n_int > _MAX_N:
        return None
    return n_int


def _backend_label() -> str:
    return getattr(settings, "IMAGE_BACKEND", "provider")


def _remote_url(value: str | None) -> bool:
    return str(value or "").startswith(("http://", "https://"))


def _asset_reference(asset: Asset | None) -> str | None:
    if not asset:
        return None
    if _remote_url(asset.url):
        return asset.url
    return asset.path or asset.url


async def generate_image(
    project_id: str,
    prompt: str,
    negative_prompt: str | None = None,
    aspect_ratio: str = "9:16",
    size: str | None = None,
    quality: str | None = None,
    shot_id: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    reference_images: list[str] | None = None,
    record_asset: bool = False,
) -> dict:
    """Generate one or more images using the active image provider."""
    n_valid = _validate_n(n)
    if n_valid is None:
        return {
            "ok": False,
            "error": f"参数 n 必须是 1-{_MAX_N} 之间的整数，收到: {n!r}",
            "status": "failed",
        }

    if not size:
        size = {
            "9:16": "1024x1792",
            "16:9": "1792x1024",
            "1:1": "1024x1024",
            "4:5": "1024x1280",
        }.get(aspect_ratio, "1024x1792")

    result = await generate_image_with_provider(
        project_id=project_id,
        prompt=prompt,
        negative_prompt=negative_prompt,
        size=size,
        quality=quality,
        model_name=model,
        n=n_valid,
        reference_images=reference_images,
        save_locally=True,
    )

    asset_type = "storyboard_image" if shot_id else "scene_image"

    if not result.get("ok"):
        error_detail = {
            "error": result.get("error"),
            "error_kind": result.get("error_kind"),
            "http_code": result.get("http_code"),
            "provider_msg": result.get("provider_msg"),
            "endpoint": result.get("endpoint"),
            "provider": result.get("provider"),
            "model": result.get("model") or model,
            "attempts": result.get("attempts") or [],
            "size_requested": result.get("size_requested") or size,
            "size_final": result.get("size_final") or size,
            "actual_size": result.get("actual_size"),
            "actual_aspect_ratio": result.get("actual_aspect_ratio"),
            "requested_aspect_ratio": result.get("requested_aspect_ratio"),
            "quality_requested": result.get("quality_requested") or quality,
            "quality_final": result.get("quality_final"),
            "downgraded": result.get("downgraded", False),
        }
        asset_id = None
        if node_id and record_asset:
            asset = await register_asset(
                project_id=project_id,
                asset_type=asset_type,
                name=f"image-failed-{uuid.uuid4().hex[:8]}",
                prompt=prompt,
                model_name=model or _backend_label(),
                metadata={
                    "status": "failed",
                    "reference_images": list(reference_images) if reference_images else [],
                    **error_detail,
                },
                node_id=node_id,
            )
            asset_id = asset["id"]
        return {
            "ok": False,
            "asset_id": asset_id,
            "asset_ids": [asset_id] if asset_id else [],
            "status": "failed",
            "n_requested": n_valid,
            "n_succeeded": 0,
            **error_detail,
        }

    images = result.get("images") or []
    asset_ids: list[str] = []
    image_outputs: list[dict] = []
    refs_provided = list(reference_images) if reference_images else []

    for idx, img in enumerate(images):
        display_url = img.get("local_url") or img.get("remote_url") or img.get("url")
        suffix = f"-{idx + 1}" if n_valid > 1 else ""
        asset_id = None
        if record_asset:
            asset = await register_asset(
                project_id=project_id,
                asset_type=asset_type,
                name=f"image-{uuid.uuid4().hex[:8]}{suffix}",
                prompt=prompt,
                model_name=result.get("model") or model or _backend_label(),
                metadata={
                    "provider": result.get("provider"),
                    "status": "completed",
                    "url": display_url,
                    "local_url": img.get("local_url"),
                    "local_path": img.get("local_path"),
                    "remote_url": img.get("remote_url"),
                    "negative_prompt": negative_prompt,
                    "size": img.get("actual_size") or result.get("size_final") or size,
                    "size_requested": result.get("size_requested") or size,
                    "size_final": img.get("actual_size") or result.get("size_final") or size,
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "actual_size": img.get("actual_size"),
                    "actual_aspect_ratio": img.get("actual_aspect_ratio"),
                    "aspect_ratio": aspect_ratio,
                    "quality_requested": result.get("quality_requested") or quality,
                    "quality_final": result.get("quality_final"),
                    "shot_id": shot_id,
                    "reference_images": refs_provided,
                    "n_index": idx,
                    "n_total": n_valid,
                },
                node_id=node_id,
                url=display_url,
                path=img.get("local_path"),
            )
            asset_id = asset["id"]
            asset_ids.append(asset_id)
        image_outputs.append({
            "asset_id": asset_id,
            "url": display_url,
            "local_url": img.get("local_url"),
            "local_path": img.get("local_path"),
            "remote_url": img.get("remote_url"),
            "n_index": idx,
            "width": img.get("width"),
            "height": img.get("height"),
            "actual_size": img.get("actual_size"),
            "actual_aspect_ratio": img.get("actual_aspect_ratio"),
        })

    primary = image_outputs[0] if image_outputs else {}
    return {
        "ok": True,
        "asset_id": primary.get("asset_id"),
        "asset_ids": asset_ids,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "url": primary.get("url"),
        "local_url": primary.get("local_url"),
        "local_path": primary.get("local_path"),
        "remote_url": primary.get("remote_url"),
        "images": image_outputs,
        "status": "completed",
        "n_requested": n_valid,
        "n_succeeded": len(image_outputs),
        "reference_images": refs_provided,
        "reference_warnings": result.get("reference_warnings") or [],
        "partial_error": result.get("partial_error"),
        "size": result.get("size_final") or size,
        "size_requested": result.get("size_requested") or size,
        "size_final": result.get("size_final") or size,
        "actual_size": primary.get("actual_size") or result.get("actual_size"),
        "actual_aspect_ratio": primary.get("actual_aspect_ratio") or result.get("actual_aspect_ratio"),
        "aspect_ratio": aspect_ratio,
        "quality": result.get("quality_final"),
        "quality_requested": result.get("quality_requested"),
        "downgraded": result.get("downgraded", False),
        "attempts": result.get("attempts") or [],
    }


async def generate_first_frame(
    project_id: str,
    shot_id: str,
    prompt: str,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    aspect_ratio: str = "16:9",
    size: str | None = None,
    quality: str | None = None,
    reference_images: list[str] | None = None,
) -> dict:
    result = await generate_image(
        project_id=project_id,
        prompt=prompt,
        shot_id=shot_id,
        node_id=node_id,
        model=model,
        n=n,
        aspect_ratio=aspect_ratio,
        size=size,
        quality=quality,
        reference_images=reference_images,
    )
    result["role"] = "first_frame"
    return result


async def generate_last_frame(
    project_id: str,
    shot_id: str,
    prompt: str,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    aspect_ratio: str = "16:9",
    size: str | None = None,
    quality: str | None = None,
    reference_images: list[str] | None = None,
) -> dict:
    result = await generate_image(
        project_id=project_id,
        prompt=prompt,
        shot_id=shot_id,
        node_id=node_id,
        model=model,
        n=n,
        aspect_ratio=aspect_ratio,
        size=size,
        quality=quality,
        reference_images=reference_images,
    )
    result["role"] = "last_frame"
    return result


def _video_display_url(result: dict[str, Any]) -> str | None:
    return result.get("local_url") or result.get("remote_url") or result.get("url")


def _resumable_video_job(
    output: Any,
    *,
    prompt: str,
    model: str | None,
    duration_seconds: int,
    aspect_ratio: str | None,
    resolution: str | None,
    reference_images: list[str] | None,
) -> dict[str, Any] | None:
    if not isinstance(output, dict) or _video_display_url(output):
        return None
    job_id = str(output.get("job_id") or "").strip()
    if not job_id:
        return None
    status = str(output.get("status") or "").strip().lower()
    error_kind = str(output.get("error_kind") or "").strip().lower()
    if status not in {"queued", "running", "processing"} and error_kind not in {
        "network",
        "rate_limit",
        "server_error",
        "timeout",
    }:
        return None

    previous_prompt = str(output.get("prompt") or "").strip()
    if previous_prompt and previous_prompt != prompt.strip():
        return None
    requested_model = str(model or "").strip()
    previous_provider = str(output.get("provider") or "").strip()
    previous_model = str(output.get("model") or "").strip()
    if requested_model and requested_model not in {previous_provider, previous_model}:
        return None
    previous_duration = output.get("duration_seconds")
    if previous_duration not in (None, ""):
        try:
            if int(float(str(previous_duration))) != duration_seconds:
                return None
        except (TypeError, ValueError):
            return None
    for previous, current in (
        (output.get("aspect_ratio"), aspect_ratio),
        (output.get("resolution"), resolution),
    ):
        if previous not in (None, "") and current not in (None, "") and str(previous) != str(current):
            return None
    previous_references = output.get("reference_images")
    if isinstance(previous_references, list) and list(previous_references) != list(reference_images or []):
        return None

    return {
        "ok": True,
        "provider": previous_provider or requested_model,
        "model": previous_model or requested_model,
        "status": "running",
        "job_id": job_id,
        "mode": output.get("mode") or output.get("video_mode"),
        "resolution": output.get("resolution") or resolution,
        "resolved_reference_images": output.get("resolved_reference_images") or [],
        "resolved_media_references": output.get("resolved_media_references") or [],
        "reference_warnings": output.get("reference_warnings") or [],
        "resumed_existing_job": True,
    }


def _merge_progress_output(
    *,
    current_output: Any,
    base_output: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    output = dict(current_output) if isinstance(current_output, dict) else dict(base_output)
    if output.get("type") != base_output.get("type"):
        output = {**base_output, **output}
    output.setdefault("type", base_output.get("type"))
    output.setdefault("job_id", base_output.get("job_id") or update.get("job_id"))
    output.setdefault("provider", base_output.get("provider"))
    output.setdefault("model", base_output.get("model"))
    output.setdefault("prompt", base_output.get("prompt"))
    output["status"] = "running"
    output["async"] = True
    poll_status = str(update.get("status") or "").strip()
    if poll_status:
        output["poll_status"] = poll_status
    if update.get("progress") is not None:
        output["progress"] = update.get("progress")
    if update.get("poll_count") is not None:
        output["poll_count"] = update.get("poll_count")
    if update.get("retrying"):
        output["poll_retrying"] = True
        output["poll_error"] = update.get("error")
        output["poll_error_kind"] = update.get("error_kind")
        output["poll_http_code"] = update.get("http_code")
        output["poll_retry_count"] = update.get("retry_count")
        output["poll_retry_in_seconds"] = update.get("retry_in_seconds")
    else:
        output.pop("poll_retrying", None)
        output.pop("poll_error", None)
        output.pop("poll_error_kind", None)
        output.pop("poll_http_code", None)
        output.pop("poll_retry_count", None)
        output.pop("poll_retry_in_seconds", None)
    last_poll = {
        key: update.get(key)
        for key in (
            "status",
            "progress",
            "poll_count",
            "updated_at",
            "retrying",
            "error_kind",
            "http_code",
            "error",
            "retry_count",
            "retry_in_seconds",
        )
        if update.get(key) is not None
    }
    if last_poll:
        output["last_poll"] = last_poll
    return output


async def _emit_media_progress_update(
    *,
    project_id: str,
    node_id: str | None,
    base_output: dict[str, Any],
    update: dict[str, Any],
) -> None:
    if not node_id:
        return
    from app.agent.orchestrator import emit_canvas_event
    from app.mcp_tools import canvas_tools

    try:
        current_node = await canvas_tools.get_node(node_id)
        current_output = current_node.get("output") if isinstance(current_node, dict) else None
        current_job_id = str(
            current_output.get("job_id") if isinstance(current_output, dict) else ""
        ).strip()
        expected_job_id = str(base_output.get("job_id") or update.get("job_id") or "").strip()
        if current_job_id and expected_job_id and current_job_id != expected_job_id:
            logger.info(
                "skip stale video progress node_id=%s current_job_id=%s polled_job_id=%s",
                node_id,
                current_job_id,
                expected_job_id,
            )
            return
        output = _merge_progress_output(
            current_output=current_output,
            base_output=base_output,
            update=update,
        )
        await canvas_tools.update_node(
            node_id,
            {"status": "running", "error_message": None, "output_data": output},
        )
        await emit_canvas_event(
            {
                "type": "canvas_action",
                "action": "update_node",
                "payload": {
                    "id": node_id,
                    "status": "running",
                    "output": output,
                    "job_id": output.get("job_id"),
                    "progress": output.get("progress"),
                    "poll_status": output.get("poll_status"),
                    "poll_count": output.get("poll_count"),
                },
            },
            project_id=project_id,
        )
    except Exception:
        logger.exception("media progress update failed node_id=%s job_id=%s", node_id, update.get("job_id"))


def _video_output(
    result: dict[str, Any],
    *,
    asset_id: str | None,
    asset_ids: list[str] | None,
    duration_seconds: int,
    aspect_ratio: str | None,
    resolution: str | None,
    reference_images: list[str],
) -> dict[str, Any]:
    ok = bool(result.get("ok"))
    status = result.get("status") or ("completed" if ok else "failed")
    return {
        "ok": ok,
        "type": "video",
        "asset_id": asset_id,
        "asset_ids": asset_ids or ([asset_id] if asset_id else []),
        "status": status,
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "resolution": result.get("resolution") or resolution,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "mode": result.get("mode"),
        "video_mode": result.get("mode"),
        "job_id": result.get("job_id"),
        "url": _video_display_url(result),
        "local_url": result.get("local_url"),
        "local_path": result.get("local_path"),
        "remote_url": result.get("remote_url"),
        "thumbnail_url": result.get("thumbnail_url"),
        "last_frame_url": result.get("last_frame_url"),
        "error": result.get("error"),
        "error_kind": result.get("error_kind"),
        "provider_msg": result.get("provider_msg"),
        "usage": result.get("usage"),
        "progress": result.get("progress"),
        "polls": result.get("polls") or [],
        "download_error": result.get("download_error"),
        "reference_images": reference_images,
        "resolved_reference_images": result.get("resolved_reference_images") or [],
        "resolved_media_references": result.get("resolved_media_references") or [],
        "reference_warnings": result.get("reference_warnings") or [],
        "resumed_existing_job": bool(result.get("resumed_existing_job")),
        "async": status in {"queued", "running"},
    }


async def _register_video_asset(
    *,
    project_id: str,
    prompt: str,
    shot_id: str | None,
    node_id: str | None,
    model: str | None,
    result: dict[str, Any],
    refs_provided: list[str],
    first_frame_asset_id: str | None,
    last_frame_asset_id: str | None,
    duration_seconds: int,
    aspect_ratio: str | None,
    resolution: str | None,
) -> str:
    display_url = _video_display_url(result)
    local_path = result.get("local_path")
    asset = await register_asset(
        project_id=project_id,
        asset_type="video",
        name=f"video-{(shot_id or uuid.uuid4().hex)[:8]}",
        prompt=prompt,
        model_name=result.get("model") or model or "video",
        metadata={
            "shot_id": shot_id,
            "first_frame": first_frame_asset_id,
            "last_frame": last_frame_asset_id,
            "reference_images": refs_provided,
            "resolved_reference_images": result.get("resolved_reference_images") or [],
            "resolved_media_references": result.get("resolved_media_references") or [],
            "aspect_ratio": aspect_ratio,
            "resolution": result.get("resolution") or resolution,
            "url": display_url,
            "local_url": result.get("local_url"),
            "local_path": local_path,
            "remote_url": result.get("remote_url"),
            "thumbnail_url": result.get("thumbnail_url"),
            "duration_seconds": duration_seconds,
            "status": result.get("status") or ("completed" if result.get("ok") else "failed"),
            "provider": result.get("provider"),
            "model": result.get("model") or model,
            "job_id": result.get("job_id"),
            "error": result.get("error"),
            "error_kind": result.get("error_kind"),
            "provider_msg": result.get("provider_msg"),
            "usage": result.get("usage"),
            "progress": result.get("progress"),
            "polls": result.get("polls") or [],
            "download_error": result.get("download_error"),
        },
        node_id=node_id,
        url=display_url,
        path=local_path,
        mime_type="video/mp4" if display_url or local_path else None,
    )
    return asset["id"]


async def _background_video_poll(
    *,
    project_id: str,
    prompt: str,
    shot_id: str | None,
    node_id: str | None,
    model: str | None,
    queued_result: dict[str, Any],
    refs_provided: list[str],
    first_frame_asset_id: str | None,
    last_frame_asset_id: str | None,
    duration_seconds: int,
    aspect_ratio: str | None,
    resolution: str | None,
    provider_extra: dict[str, Any],
    record_asset: bool,
) -> None:
    from app.agent.orchestrator import emit_canvas_event
    from app.mcp_tools import canvas_tools

    job_id = str(queued_result.get("job_id") or "").strip()
    if not job_id:
        return
    base_output = _video_output(
        queued_result,
        asset_id=None,
        asset_ids=[],
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        reference_images=refs_provided,
    )
    base_output["prompt"] = prompt

    async def progress_callback(update: dict[str, Any]) -> None:
        await _emit_media_progress_update(
            project_id=project_id,
            node_id=node_id,
            base_output=base_output,
            update=update,
        )

    result = await poll_video_with_provider(
        project_id=project_id,
        job_id=job_id,
        model_name=queued_result.get("provider") or model,
        extra=provider_extra,
        save_locally=True,
        progress_callback=progress_callback,
    )
    result["reference_images"] = refs_provided
    result["resolved_reference_images"] = queued_result.get("resolved_reference_images") or []
    result["resolved_media_references"] = queued_result.get("resolved_media_references") or result.get("resolved_media_references") or []
    result["resumed_existing_job"] = bool(queued_result.get("resumed_existing_job"))
    result["reference_warnings"] = [
        *(
            queued_result.get("reference_warnings")
            if isinstance(queued_result.get("reference_warnings"), list)
            else []
        ),
        *(
            result.get("reference_warnings")
            if isinstance(result.get("reference_warnings"), list)
            else []
        ),
    ]

    if node_id:
        try:
            current_node = await canvas_tools.get_node(node_id)
            current_output = current_node.get("output") if isinstance(current_node, dict) else None
            current_job_id = str(
                current_output.get("job_id") if isinstance(current_output, dict) else ""
            ).strip()
            if current_job_id and current_job_id != job_id:
                logger.info(
                    "discard stale video result node_id=%s current_job_id=%s polled_job_id=%s",
                    node_id,
                    current_job_id,
                    job_id,
                )
                return
        except Exception:
            logger.exception("verify video job ownership failed node_id=%s job_id=%s", node_id, job_id)
            return

    asset_id = None
    if record_asset:
        asset_id = await _register_video_asset(
            project_id=project_id,
            prompt=prompt,
            shot_id=shot_id,
            node_id=node_id,
            model=model,
            result=result,
            refs_provided=refs_provided,
            first_frame_asset_id=first_frame_asset_id,
            last_frame_asset_id=last_frame_asset_id,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )

    output = _video_output(
        result,
        asset_id=asset_id,
        asset_ids=[asset_id] if asset_id else [],
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        reference_images=refs_provided,
    )
    output["prompt"] = prompt
    if node_id:
        try:
            current_node = await canvas_tools.get_node(node_id)
            output = media_history.preserve_media_history(output, current_node.get("output"))
        except Exception:
            logger.exception("preserve video history failed node_id=%s job_id=%s", node_id, job_id)
    next_status = "completed" if result.get("ok") else "failed"
    patch: dict[str, Any] = {
        "status": next_status,
        "error_message": None if result.get("ok") else result.get("error"),
        "output_data": output,
    }
    if node_id:
        try:
            await canvas_tools.update_node(node_id, patch)
            await emit_canvas_event(
                {
                    "type": "canvas_action",
                    "action": "update_node",
                    "payload": {
                        "id": node_id,
                        "status": next_status,
                        "error": result.get("error"),
                        "error_message": result.get("error"),
                        "output": output,
                    },
                },
                project_id=project_id,
            )
        except Exception:
            logger.exception("background video node update failed node_id=%s job_id=%s", node_id, job_id)


def _schedule_background_video_poll(**kwargs: Any) -> bool:
    project_id = str(kwargs.get("project_id") or "").strip()
    node_id = str(kwargs.get("node_id") or "").strip()
    queued_result = kwargs.get("queued_result")
    job_id = str(queued_result.get("job_id") if isinstance(queued_result, dict) else "").strip()
    if not project_id or not job_id:
        return False
    task_key = (project_id, node_id, job_id)
    existing = _BACKGROUND_VIDEO_TASKS.get(task_key)
    if existing and not existing.done():
        return False

    task = asyncio.create_task(_background_video_poll(**kwargs))
    _BACKGROUND_VIDEO_TASKS[task_key] = task

    def _done(done: asyncio.Task) -> None:
        if _BACKGROUND_VIDEO_TASKS.get(task_key) is done:
            _BACKGROUND_VIDEO_TASKS.pop(task_key, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("background video poll task failed")

    task.add_done_callback(_done)
    return True


async def resume_persisted_video_poll(
    *,
    project_id: str,
    node_id: str,
    prompt: str,
    input_data: dict[str, Any] | None,
    output: dict[str, Any],
) -> bool:
    """Resume one provider task recorded on a video node without resubmitting it."""

    from app.mcp_tools import canvas_tools

    job_id = str(output.get("job_id") or "").strip()
    if not job_id or _video_display_url(output):
        return False
    fields = dict(input_data or {})
    nested_fields = fields.get("fields")
    if isinstance(nested_fields, dict):
        fields = {**nested_fields, **fields}

    provider_name = str(output.get("provider") or fields.get("provider") or "").strip()
    model_name = str(output.get("model") or fields.get("model") or provider_name).strip()
    if not provider_name and not model_name:
        return False

    try:
        duration_seconds = int(float(str(output.get("duration_seconds") or fields.get("duration_seconds") or 4)))
    except (TypeError, ValueError):
        duration_seconds = 4
    aspect_ratio = output.get("aspect_ratio") or fields.get("aspect_ratio")
    resolution = output.get("resolution") or fields.get("resolution")
    refs = output.get("reference_images")
    if not isinstance(refs, list):
        refs = fields.get("reference_images") if isinstance(fields.get("reference_images"), list) else []

    queued_result = dict(output)
    queued_result.update({
        "ok": True,
        "status": "running",
        "job_id": job_id,
        "provider": provider_name or model_name,
        "model": model_name or provider_name,
        "resumed_existing_job": True,
        "recovered_after_restart": True,
    })
    resumed_output = _video_output(
        queued_result,
        asset_id=None,
        asset_ids=[],
        duration_seconds=duration_seconds,
        aspect_ratio=str(aspect_ratio) if aspect_ratio is not None else None,
        resolution=str(resolution) if resolution is not None else None,
        reference_images=list(refs),
    )
    resumed_output["prompt"] = str(output.get("prompt") or prompt or "").strip()
    resumed_output["recovered_after_restart"] = True
    for key in (
        "error",
        "error_message",
        "error_kind",
        "poll_error",
        "poll_error_kind",
        "poll_http_code",
    ):
        resumed_output.pop(key, None)

    await canvas_tools.update_node(
        node_id,
        {"status": "running", "error_message": None, "output_data": resumed_output},
    )
    provider_extra = {
        key: fields[key]
        for key in ("_poll_interval_seconds", "_poll_timeout_seconds")
        if fields.get(key) is not None
    }
    scheduled = _schedule_background_video_poll(
        project_id=project_id,
        prompt=resumed_output["prompt"],
        shot_id=fields.get("shot_id"),
        node_id=node_id,
        model=model_name or provider_name,
        queued_result=queued_result,
        refs_provided=list(refs),
        first_frame_asset_id=fields.get("first_frame_asset_id"),
        last_frame_asset_id=fields.get("last_frame_asset_id"),
        duration_seconds=duration_seconds,
        aspect_ratio=str(aspect_ratio) if aspect_ratio is not None else None,
        resolution=str(resolution) if resolution is not None else None,
        provider_extra=provider_extra,
        record_asset=True,
    )
    return scheduled or (project_id, node_id, job_id) in _BACKGROUND_VIDEO_TASKS


async def stop_background_media_tasks() -> None:
    tasks = [*_BACKGROUND_VIDEO_TASKS.values(), *_BACKGROUND_AUDIO_TASKS]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _BACKGROUND_VIDEO_TASKS.clear()
    _BACKGROUND_AUDIO_TASKS.clear()


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
    extra: dict[str, Any] | None = None,
    record_asset: bool = False,
    resume_existing_job: bool = False,
) -> dict:
    """Resolve frame assets and delegate to the active video provider."""
    first_url = last_url = None
    if first_frame_asset_id or last_frame_asset_id:
        async with session_scope() as session:
            if first_frame_asset_id:
                asset = await session.get(Asset, first_frame_asset_id)
                first_url = _asset_reference(asset)
            if last_frame_asset_id:
                asset = await session.get(Asset, last_frame_asset_id)
                last_url = _asset_reference(asset)

    provider_extra = dict(extra or {})
    if aspect_ratio and "aspect_ratio" not in provider_extra and "ratio" not in provider_extra:
        provider_extra["aspect_ratio"] = aspect_ratio
    if resolution and "resolution" not in provider_extra:
        provider_extra["resolution"] = resolution

    refs_provided = list(reference_images) if reference_images else []
    result: dict[str, Any] | None = None
    if resume_existing_job and node_id:
        from app.mcp_tools import canvas_tools

        try:
            current_node = await canvas_tools.get_node(node_id)
            result = _resumable_video_job(
                current_node.get("output") if isinstance(current_node, dict) else None,
                prompt=prompt,
                model=model,
                duration_seconds=duration_seconds,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                reference_images=refs_provided,
            )
        except Exception:
            logger.exception("inspect resumable video job failed node_id=%s", node_id)

    if result is None:
        result = await generate_video_with_provider(
            project_id=project_id,
            prompt=prompt,
            first_frame_url=first_url,
            last_frame_url=last_url,
            duration_seconds=duration_seconds,
            model_name=model,
            extra=provider_extra,
            reference_images=reference_images,
            save_locally=True,
            wait_for_completion=False,
        )

    ok = bool(result.get("ok"))
    status = result.get("status") or ("completed" if ok else "failed")
    async_status = status in {"queued", "running"} and result.get("job_id")

    if result.get("ok") and async_status:
        _schedule_background_video_poll(
            project_id=project_id,
            prompt=prompt,
            shot_id=shot_id,
            node_id=node_id,
            model=model,
            queued_result=result,
            refs_provided=refs_provided,
            first_frame_asset_id=first_frame_asset_id,
            last_frame_asset_id=last_frame_asset_id,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            provider_extra=provider_extra,
            record_asset=record_asset,
        )
        return _video_output(
            result,
            asset_id=None,
            asset_ids=[],
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            reference_images=refs_provided,
        )

    asset_id = None
    if record_asset:
        asset_id = await _register_video_asset(
            project_id=project_id,
            prompt=prompt,
            shot_id=shot_id,
            node_id=node_id,
            model=model,
            result=result,
            refs_provided=refs_provided,
            first_frame_asset_id=first_frame_asset_id,
            last_frame_asset_id=last_frame_asset_id,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )
    return _video_output(
        result,
        asset_id=asset_id,
        asset_ids=[asset_id] if asset_id else [],
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        reference_images=refs_provided,
    )


def _audio_display_url(result: dict[str, Any]) -> str | None:
    return result.get("local_url") or result.get("remote_url") or result.get("url")


def _audio_output(
    result: dict[str, Any],
    *,
    asset_id: str | None,
    asset_ids: list[str] | None,
    prompt: str,
    title: str | None,
    style: str | None,
    instrumental: bool | None,
    duration_seconds: int | None,
    audio_format: str | None,
) -> dict[str, Any]:
    ok = bool(result.get("ok"))
    status = result.get("status") or ("completed" if ok else "failed")
    audios = result.get("audios") if isinstance(result.get("audios"), list) else []
    return {
        "ok": ok,
        "type": "audio",
        "asset_id": asset_id,
        "asset_ids": asset_ids or ([asset_id] if asset_id else []),
        "status": status,
        "prompt": prompt,
        "title": title,
        "style": style,
        "instrumental": instrumental,
        "voice": result.get("voice"),
        "speed": result.get("speed"),
        "instructions": result.get("instructions"),
        "duration_seconds": result.get("duration") or duration_seconds,
        "format": result.get("format") or audio_format,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "job_id": result.get("job_id"),
        "url": _audio_display_url(result),
        "local_url": result.get("local_url"),
        "local_path": result.get("local_path"),
        "remote_url": result.get("remote_url"),
        "stream_audio_url": result.get("stream_audio_url"),
        "source_audio_url": result.get("source_audio_url"),
        "image_url": result.get("image_url"),
        "mime_type": result.get("mime_type"),
        "audios": audios,
        "error": result.get("error"),
        "error_kind": result.get("error_kind"),
        "provider_msg": result.get("provider_msg"),
        "progress": result.get("progress"),
        "polls": result.get("polls") or [],
        "download_error": result.get("download_error"),
        "async": status in {"queued", "running"},
    }


async def _register_audio_asset(
    *,
    project_id: str,
    prompt: str,
    node_id: str | None,
    model: str | None,
    result: dict[str, Any],
    title: str | None,
    style: str | None,
    instrumental: bool | None,
    duration_seconds: int | None,
    audio_format: str | None,
) -> str:
    display_url = _audio_display_url(result)
    local_path = result.get("local_path")
    mime_type = (result.get("mime_type") or "audio/mpeg") if (display_url or local_path) else None
    asset = await register_asset(
        project_id=project_id,
        asset_type="audio",
        name=f"audio-{uuid.uuid4().hex[:8]}",
        prompt=prompt,
        model_name=result.get("model") or model or "audio",
        metadata={
            "title": title,
            "style": style,
            "instrumental": instrumental,
            "voice": result.get("voice"),
            "speed": result.get("speed"),
            "instructions": result.get("instructions"),
            "duration_seconds": result.get("duration") or duration_seconds,
            "format": result.get("format") or audio_format,
            "url": display_url,
            "local_url": result.get("local_url"),
            "local_path": local_path,
            "remote_url": result.get("remote_url"),
            "stream_audio_url": result.get("stream_audio_url"),
            "source_audio_url": result.get("source_audio_url"),
            "image_url": result.get("image_url"),
            "status": result.get("status") or ("completed" if result.get("ok") else "failed"),
            "provider": result.get("provider"),
            "model": result.get("model") or model,
            "job_id": result.get("job_id"),
            "error": result.get("error"),
            "error_kind": result.get("error_kind"),
            "provider_msg": result.get("provider_msg"),
            "progress": result.get("progress"),
            "polls": result.get("polls") or [],
            "download_error": result.get("download_error"),
            "audios": result.get("audios") if isinstance(result.get("audios"), list) else [],
        },
        node_id=node_id,
        url=display_url,
        path=local_path,
        mime_type=mime_type,
    )
    return asset["id"]


async def _background_audio_poll(
    *,
    project_id: str,
    prompt: str,
    node_id: str | None,
    model: str | None,
    queued_result: dict[str, Any],
    title: str | None,
    style: str | None,
    instrumental: bool | None,
    duration_seconds: int | None,
    audio_format: str | None,
    provider_extra: dict[str, Any],
    record_asset: bool,
) -> None:
    from app.agent.orchestrator import emit_canvas_event
    from app.mcp_tools import canvas_tools

    job_id = str(queued_result.get("job_id") or "").strip()
    if not job_id:
        return
    base_output = _audio_output(
        queued_result,
        asset_id=None,
        asset_ids=[],
        prompt=prompt,
        title=title,
        style=style,
        instrumental=instrumental,
        duration_seconds=duration_seconds,
        audio_format=audio_format,
    )

    async def progress_callback(update: dict[str, Any]) -> None:
        await _emit_media_progress_update(
            project_id=project_id,
            node_id=node_id,
            base_output=base_output,
            update=update,
        )

    result = await poll_audio_with_provider(
        project_id=project_id,
        job_id=job_id,
        model_name=queued_result.get("provider") or model,
        extra=provider_extra,
        save_locally=True,
        progress_callback=progress_callback,
    )

    asset_id = None
    if record_asset:
        asset_id = await _register_audio_asset(
            project_id=project_id,
            prompt=prompt,
            node_id=node_id,
            model=model,
            result=result,
            title=title,
            style=style,
            instrumental=instrumental,
            duration_seconds=duration_seconds,
            audio_format=audio_format,
        )

    output = _audio_output(
        result,
        asset_id=asset_id,
        asset_ids=[asset_id] if asset_id else [],
        prompt=prompt,
        title=title,
        style=style,
        instrumental=instrumental,
        duration_seconds=duration_seconds,
        audio_format=audio_format,
    )
    if node_id:
        try:
            current_node = await canvas_tools.get_node(node_id)
            output = media_history.preserve_media_history(output, current_node.get("output"))
        except Exception:
            logger.exception("preserve audio history failed node_id=%s job_id=%s", node_id, job_id)
    next_status = "completed" if result.get("ok") else "failed"
    if node_id:
        try:
            await canvas_tools.update_node(
                node_id,
                {
                    "status": next_status,
                    "error_message": None if result.get("ok") else result.get("error"),
                    "output_data": output,
                },
            )
            await emit_canvas_event(
                {
                    "type": "canvas_action",
                    "action": "update_node",
                    "payload": {
                        "id": node_id,
                        "status": next_status,
                        "error": result.get("error"),
                        "error_message": result.get("error"),
                        "output": output,
                    },
                },
                project_id=project_id,
            )
        except Exception:
            logger.exception("background audio node update failed node_id=%s job_id=%s", node_id, job_id)


def _schedule_background_audio_poll(**kwargs: Any) -> None:
    task = asyncio.create_task(_background_audio_poll(**kwargs))
    _BACKGROUND_AUDIO_TASKS.add(task)

    def _done(done: asyncio.Task) -> None:
        _BACKGROUND_AUDIO_TASKS.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("background audio poll task failed")

    task.add_done_callback(_done)


async def generate_audio(
    project_id: str,
    prompt: str,
    node_id: str | None = None,
    model: str | None = None,
    title: str | None = None,
    style: str | None = None,
    instrumental: bool | None = None,
    duration_seconds: int | None = None,
    audio_format: str | None = None,
    extra: dict[str, Any] | None = None,
    record_asset: bool = False,
) -> dict:
    """Delegate pure audio generation to the active audio provider."""
    provider_extra = dict(extra or {})
    result = await generate_audio_with_provider(
        project_id=project_id,
        prompt=prompt,
        title=title,
        style=style,
        instrumental=instrumental,
        model_name=model,
        extra=provider_extra,
        save_locally=True,
        wait_for_completion=False,
    )

    ok = bool(result.get("ok"))
    status = result.get("status") or ("completed" if ok else "failed")
    async_status = status in {"queued", "running"} and result.get("job_id")

    if result.get("ok") and async_status:
        _schedule_background_audio_poll(
            project_id=project_id,
            prompt=prompt,
            node_id=node_id,
            model=model,
            queued_result=result,
            title=title,
            style=style,
            instrumental=instrumental,
            duration_seconds=duration_seconds,
            audio_format=audio_format,
            provider_extra=provider_extra,
            record_asset=record_asset,
        )
        return _audio_output(
            result,
            asset_id=None,
            asset_ids=[],
            prompt=prompt,
            title=title,
            style=style,
            instrumental=instrumental,
            duration_seconds=duration_seconds,
            audio_format=audio_format,
        )

    asset_id = None
    if record_asset:
        asset_id = await _register_audio_asset(
            project_id=project_id,
            prompt=prompt,
            node_id=node_id,
            model=model,
            result=result,
            title=title,
            style=style,
            instrumental=instrumental,
            duration_seconds=duration_seconds,
            audio_format=audio_format,
        )
    return _audio_output(
        result,
        asset_id=asset_id,
        asset_ids=[asset_id] if asset_id else [],
        prompt=prompt,
        title=title,
        style=style,
        instrumental=instrumental,
        duration_seconds=duration_seconds,
        audio_format=audio_format,
    )


async def generate_panorama(
    project_id: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    scene_id: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    reference_images: list[str] | None = None,
    record_asset: bool = False,
) -> dict:
    """Generate a panoramic scene image intended for directional crop views."""
    n_valid = _validate_n(n)
    if n_valid is None:
        return {
            "ok": False,
            "error": f"参数 n 必须是 1-{_MAX_N} 之间的整数，收到: {n!r}",
            "status": "failed",
        }

    aspect_to_size = {
        "16:9": "1792x1024",
        "21:9": "1792x768",
        "1:1": "1024x1024",
    }
    size = aspect_to_size.get(aspect_ratio, "1792x1024")

    result = await generate_image_with_provider(
        project_id=project_id,
        prompt=prompt,
        size=size,
        model_name=model,
        n=n_valid,
        reference_images=reference_images,
        save_locally=True,
    )

    refs_provided = list(reference_images) if reference_images else []

    if not result.get("ok"):
        asset_id = None
        if record_asset:
            asset = await register_asset(
                project_id=project_id,
                asset_type="scene_image",
                name=f"panorama-{uuid.uuid4().hex[:8]}",
                prompt=prompt,
                model_name=model or _backend_label(),
                metadata={
                    "role": "panorama",
                    "scene_id": scene_id,
                    "aspect_ratio": aspect_ratio,
                    "status": "queued",
                    "backend": _backend_label(),
                    "reference_images": refs_provided,
                    "error": result.get("error"),
                },
                node_id=node_id,
            )
            asset_id = asset["id"]
        return {
            "ok": False,
            "asset_id": asset_id,
            "asset_ids": [asset_id] if asset_id else [],
            "role": "panorama",
            "status": "queued",
            "scene_id": scene_id,
            "n_requested": n_valid,
            "n_succeeded": 0,
            "error": result.get("error"),
            "reference_images": refs_provided,
        }

    images = result.get("images") or []
    asset_ids: list[str] = []
    image_outputs: list[dict] = []
    for idx, img in enumerate(images):
        display_url = img.get("local_url") or img.get("remote_url") or img.get("url")
        suffix = f"-{idx + 1}" if n_valid > 1 else ""
        asset_id = None
        if record_asset:
            asset = await register_asset(
                project_id=project_id,
                asset_type="scene_image",
                name=f"panorama-{uuid.uuid4().hex[:8]}{suffix}",
                prompt=prompt,
                model_name=result.get("model") or model or _backend_label(),
                metadata={
                    "role": "panorama",
                    "scene_id": scene_id,
                    "aspect_ratio": aspect_ratio,
                    "status": "completed",
                    "url": display_url,
                    "local_url": img.get("local_url"),
                    "local_path": img.get("local_path"),
                    "remote_url": img.get("remote_url"),
                    "size": size,
                    "reference_images": refs_provided,
                    "n_index": idx,
                    "n_total": n_valid,
                    "provider": result.get("provider"),
                },
                node_id=node_id,
                url=display_url,
                path=img.get("local_path"),
            )
            asset_id = asset["id"]
            asset_ids.append(asset_id)
        image_outputs.append({
            "asset_id": asset_id,
            "url": display_url,
            "local_url": img.get("local_url"),
            "local_path": img.get("local_path"),
            "remote_url": img.get("remote_url"),
            "n_index": idx,
        })

    primary = image_outputs[0] if image_outputs else {}
    return {
        "ok": True,
        "asset_id": primary.get("asset_id"),
        "asset_ids": asset_ids,
        "role": "panorama",
        "status": "completed",
        "scene_id": scene_id,
        "url": primary.get("url"),
        "images": image_outputs,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "n_requested": n_valid,
        "n_succeeded": len(image_outputs),
        "reference_images": refs_provided,
        "reference_warnings": result.get("reference_warnings") or [],
    }


async def crop_panorama(
    project_id: str,
    panorama_asset_id: str,
    mode: str = "single",
    direction: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Queue panorama crop view assets. Real image processing is still P3."""
    if mode not in {"single", "4-view", "9-view"}:
        mode = "single"

    async with session_scope() as session:
        source = await session.get(Asset, panorama_asset_id)
        if not source:
            return {"error": f"Panorama asset {panorama_asset_id} not found"}

    views = {
        "single": [direction or "front"],
        "4-view": ["front", "back", "left", "right"],
        "9-view": [
            "tl", "tc", "tr",
            "ml", "mc", "mr",
            "bl", "bc", "br",
        ],
    }[mode]

    crops = []
    for direction_name in views:
        asset = await register_asset(
            project_id=project_id,
            asset_type="scene_image",
            name=f"panoview-{panorama_asset_id[:8]}-{direction_name}",
            prompt=f"crop:{direction_name} of {panorama_asset_id}",
            model_name=_backend_label(),
            metadata={
                "role": "panorama_view",
                "panorama_id": panorama_asset_id,
                "direction": direction_name,
                "mode": mode,
                "status": "queued",
            },
            node_id=node_id,
        )
        crops.append({"asset_id": asset["id"], "direction": direction_name})

    return {
        "panorama_id": panorama_asset_id,
        "mode": mode,
        "view_count": len(crops),
        "views": crops,
        "status": "queued",
    }


async def generate_story_template(
    project_id: str,
    segment_id: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    size: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    n: int = 1,
    quality: str | None = None,
    reference_images: list[str] | None = None,
    record_asset: bool = False,
) -> dict:
    """Generate a story-template visual board image for a segment."""
    n_valid = _validate_n(n)
    if n_valid is None:
        return {
            "ok": False,
            "error": f"参数 n 必须是 1-{_MAX_N} 之间的整数，收到: {n!r}",
            "status": "failed",
        }

    aspect_to_size = {
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "1:1": "2160x2160",
        "4:3": "2880x2160",
        "3:4": "2160x2880",
    }
    requested_size = size or aspect_to_size.get(aspect_ratio, "3840x2160")
    try:
        generated = await generate_image_with_provider(
            project_id=project_id,
            prompt=prompt,
            size=requested_size,
            quality=quality,
            model_name=model,
            n=n_valid,
            reference_images=reference_images,
        )
    except Exception as exc:
        generated = {"ok": False, "error": f"image provider call failed: {exc}"}

    refs_provided = list(reference_images) if reference_images else []

    if not generated.get("ok"):
        return {
            "ok": False,
            "role": "story_template",
            "segment_id": segment_id,
            "status": "failed",
            "error": generated.get("error", "image generation failed"),
            "error_kind": generated.get("error_kind"),
            "http_code": generated.get("http_code"),
            "provider_msg": generated.get("provider_msg"),
            "endpoint": generated.get("endpoint"),
            "provider": generated.get("provider"),
            "model": generated.get("model"),
            "attempts": generated.get("attempts") or [],
            "n_requested": n_valid,
            "n_succeeded": 0,
            "reference_images": refs_provided,
            "size_requested": generated.get("size_requested") or requested_size,
            "size_final": generated.get("size_final") or requested_size,
            "quality_requested": generated.get("quality_requested") or quality,
            "quality_final": generated.get("quality_final"),
            "downgraded": generated.get("downgraded", False),
        }

    images = generated.get("images") or []
    asset_ids: list[str] = []
    image_outputs: list[dict] = []
    for idx, img in enumerate(images):
        display_url = img.get("local_url") or img.get("remote_url") or img.get("url")
        suffix = f"-{idx + 1}" if n_valid > 1 else ""
        asset_id = None
        if record_asset:
            asset = await register_asset(
                project_id=project_id,
                asset_type="scene_image",
                name=f"story-template-{(segment_id or uuid.uuid4().hex)[:8]}{suffix}",
                prompt=prompt,
                url=display_url,
                path=img.get("local_path"),
                model_name=model or _backend_label(),
                metadata={
                    "role": "story_template",
                    "segment_id": segment_id,
                    "aspect_ratio": aspect_ratio,
                    "status": "completed",
                    "backend": _backend_label(),
                    "url": display_url,
                    "local_url": img.get("local_url"),
                    "local_path": img.get("local_path"),
                    "remote_url": img.get("remote_url"),
                    "size": generated.get("size_final") or requested_size,
                    "size_requested": generated.get("size_requested") or requested_size,
                    "reference_images": refs_provided,
                    "n_index": idx,
                    "n_total": n_valid,
                    "provider": generated.get("provider"),
                },
                node_id=node_id,
            )
            asset_id = asset["id"]
            asset_ids.append(asset_id)
        image_outputs.append({
            "asset_id": asset_id,
            "url": display_url,
            "local_url": img.get("local_url"),
            "local_path": img.get("local_path"),
            "remote_url": img.get("remote_url"),
            "n_index": idx,
        })

    primary = image_outputs[0] if image_outputs else {}
    return {
        "ok": True,
        "asset_id": primary.get("asset_id"),
        "asset_ids": asset_ids,
        "role": "story_template",
        "segment_id": segment_id,
        "status": "completed",
        "url": primary.get("url"),
        "images": image_outputs,
        "provider": generated.get("provider"),
        "model": generated.get("model"),
        "n_requested": n_valid,
        "n_succeeded": len(image_outputs),
        "reference_images": refs_provided,
        "reference_warnings": generated.get("reference_warnings") or [],
        "size": generated.get("size_final") or requested_size,
        "size_requested": generated.get("size_requested") or requested_size,
        "size_final": generated.get("size_final") or requested_size,
        "quality": generated.get("quality_final"),
        "quality_requested": generated.get("quality_requested"),
        "downgraded": generated.get("downgraded", False),
        "attempts": generated.get("attempts") or [],
    }
