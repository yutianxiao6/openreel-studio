"""Media provider orchestration for OpenReel.

Image, audio and video request construction, provider polling, response parsing,
and output extraction belong exclusively to Universal Model Adapter. This
module manages OpenReel jobs and media storage.
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import re
import struct
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import select

from app.config import settings
from app.db.models import Asset, MediaProvider, WorkflowNode
from app.db.session import session_scope
from app.services.media_path_security import (
    path_is_within_roots,
    project_media_roots,
    resolve_project_media_file,
)


ProgressCallback = Callable[[dict[str, Any]], Any]


async def _notify_progress(callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        result = callback(payload)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Progress reporting is observational; never fail the provider poll because
        # a UI update callback failed.
        return


def _media_http_timeout() -> httpx.Timeout:
    try:
        seconds = max(
            60.0,
            float(os.getenv("DRAMA_IMAGE_PROVIDER_TIMEOUT_SECONDS", "300") or "300"),
        )
    except (TypeError, ValueError):
        seconds = 300.0
    connect_seconds = min(60.0, seconds)
    return httpx.Timeout(seconds, connect=connect_seconds)


def _storage_path(project_id: str, filename: str) -> Path:
    base = Path(getattr(settings, "STORAGE_DIR", "./storage"))
    d = base / project_id / "generated_images"
    d.mkdir(parents=True, exist_ok=True)
    return d / filename


def _parse_image_size(size: str | None) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$", str(size or ""))
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _ratio_close(a: float, b: float, tolerance: float = 0.015) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tolerance


def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        if width > 0 and height > 0:
            return width, height
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        idx = 2
        while idx + 9 <= len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            idx += 2
            if marker in {0xD8, 0xD9}:
                continue
            if idx + 2 > len(data):
                break
            seg_len = int.from_bytes(data[idx:idx + 2], "big")
            if seg_len < 2 or idx + seg_len > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if idx + 7 <= len(data):
                    height = int.from_bytes(data[idx + 3:idx + 5], "big")
                    width = int.from_bytes(data[idx + 5:idx + 7], "big")
                    if width > 0 and height > 0:
                        return width, height
                break
            idx += seg_len
    return None


def _image_size_mismatch_error(
    *,
    provider: MediaProvider,
    requested_size: str,
    actual_size: str,
    images: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    quality: str | None,
) -> dict[str, Any]:
    actual_dims = _parse_image_size(actual_size)
    requested_dims = _parse_image_size(requested_size)
    actual_ratio = (
        f"{actual_dims[0]}:{actual_dims[1]}" if actual_dims else None
    )
    requested_ratio = (
        f"{requested_dims[0]}:{requested_dims[1]}" if requested_dims else None
    )
    message = (
        f"图片 provider 返回的真实尺寸 {actual_size} 与请求尺寸 {requested_size} 画幅不一致。"
        "后端已拦截该结果，避免把错误画幅标记为成功。"
    )
    return {
        "ok": False,
        "provider": provider.name,
        "model": provider.model_name,
        "error": message,
        "error_kind": "image_size_mismatch",
        "provider_msg": message,
        "images": images,
        "attempts": attempts,
        "size_requested": requested_size,
        "size_final": actual_size,
        "actual_size": actual_size,
        "actual_aspect_ratio": actual_ratio,
        "requested_aspect_ratio": requested_ratio,
        "quality_requested": quality,
        "quality_final": quality,
        "downgraded": False,
        "suggested_next": "换支持该画幅/尺寸的图片模型，或把原节点 resolution 改成 provider 实际支持的同画幅尺寸后重试。",
    }


def _project_media_path_from_url(project_id: str, url: str | None) -> str | None:
    text = str(url or "").strip()
    prefix = f"/api/media/{project_id}/"
    if not text.startswith(prefix):
        return None
    filename = text[len(prefix):].lstrip("/")
    if filename.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
        rel_paths = [filename]
    else:
        rel_paths = [f"generated_images/{filename}"]
    for raw_root in (
        getattr(settings, "STORAGE_PATH", "./storage"),
        getattr(settings, "STORAGE_DIR", "./storage"),
    ):
        root = Path(raw_root).resolve() / project_id
        for rel_path in rel_paths:
            candidate = (root / rel_path).resolve()
            if (
                path_is_within_roots(candidate, [root])
                and candidate.exists()
                and candidate.is_file()
            ):
                return str(candidate)
    return None


def _collect_output_image_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"url", "local_url", "remote_url", "local_path", "path"} and isinstance(item, str) and item:
                refs.append(item)
            elif isinstance(item, (dict, list)):
                refs.extend(_collect_output_image_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_output_image_refs(item))
    return refs


async def _pick_node_output_reference(project_id: str, node_id: str) -> str | None:
    async with session_scope() as session:
        node = await session.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id or not node.output_json:
        return None
    allowed_roots = await project_media_roots(project_id)
    try:
        output = json.loads(node.output_json)
    except (json.JSONDecodeError, TypeError):
        return None
    for candidate in _collect_output_image_refs(output):
        if candidate.startswith(("http://", "https://")):
            return candidate
        media_path = _project_media_path_from_url(project_id, candidate)
        if media_path:
            return media_path
        path = resolve_project_media_file(
            project_id,
            candidate,
            allowed_roots=allowed_roots,
        )
        if path is not None:
            return str(path)
    return None


async def _get_active_provider(kind: str) -> MediaProvider | None:
    async with session_scope() as session:
        result = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.is_active.is_(True))
            .where(MediaProvider.enabled.is_(True))
            .order_by(MediaProvider.created_at, MediaProvider.id)
        )
        provider = result.first()
        if provider:
            return provider
        fallback = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.enabled.is_(True))
            .order_by(MediaProvider.created_at, MediaProvider.id)
        )
        return fallback.first()


async def _get_provider_by_name(kind: str, name: str) -> MediaProvider | None:
    async with session_scope() as session:
        result = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.name == name)
            .where(MediaProvider.enabled.is_(True))
        )
        return result.first()


async def _get_provider_by_name_or_model(kind: str, name_or_model: str) -> MediaProvider | None:
    provider = await _get_provider_by_name(kind, name_or_model)
    if provider:
        return provider
    async with session_scope() as session:
        result = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.model_name == name_or_model)
            .where(MediaProvider.enabled.is_(True))
        )
        return result.first()


async def _get_provider_by_id(provider_id: str) -> MediaProvider | None:
    async with session_scope() as session:
        return await session.get(MediaProvider, provider_id)


def _parse_extra(provider: MediaProvider) -> dict[str, Any]:
    params_json = getattr(provider, "params_json", None)
    if params_json:
        try:
            return json.loads(params_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


async def _resolve_node_id_for_reference(project_id: str, node_ref: str) -> tuple[str | None, str | None]:
    """Resolve a full node id or a unique node-id prefix for reference images."""
    node_id = (node_ref or "").strip()
    if not node_id:
        return None, "节点引用为空"
    async with session_scope() as session:
        exact = await session.get(WorkflowNode, node_id)
        if exact and exact.project_id == project_id:
            return node_id, None
        if len(node_id) >= 36:
            return None, f"找不到当前项目节点 {node_id}"
        stmt = select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.id.like(f"{node_id}%"),
        )
        matches = list((await session.exec(stmt)).all())
    if len(matches) == 1:
        return matches[0].id, None
    if len(matches) > 1:
        return None, f"节点短 ID {node_id} 不唯一，请使用完整节点 ID"
    return node_id, None


async def _resolve_reference_images(
    project_id: str,
    refs: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Resolve user-supplied references into actual URLs/local paths.

    Accepts: 'asset:<id>' / 'node:<id>' / 'http(s)://...' / a path inside the
    current project storage or its configured asset library. Returns (resolved,
    errors). The resolved list contains URLs that providers can fetch or local
    paths that have passed the project-scoped allowlist.
    """
    if not refs:
        return [], []

    resolved: list[str] = []
    errors: list[str] = []
    allowed_roots = await project_media_roots(project_id)

    for raw in refs:
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"参考图引用为空: {raw!r}")
            continue
        ref = raw.strip()
        if ref.startswith("upload:"):
            rel = ref[len("upload:"):].strip().lstrip("/")
            ref = rel if rel.startswith("uploads/") else f"uploads/{rel}"

        if ref.startswith("http://") or ref.startswith("https://"):
            resolved.append(ref)
            continue

        if ref.startswith("asset:"):
            asset_id = ref[len("asset:"):].strip()
            async with session_scope() as session:
                asset = await session.get(Asset, asset_id)
            if not asset or asset.project_id != project_id:
                errors.append(f"找不到资产 asset:{asset_id}")
                continue
            url = asset.url
            path = asset.path
            picked = url if url and (url.startswith("http://") or url.startswith("https://")) else (path or url)
            if not picked:
                errors.append(f"资产 {asset_id} 没有可用的 url 或 path")
                continue
            if picked.startswith(("http://", "https://")):
                resolved.append(picked)
                continue
            local_asset = _project_media_path_from_url(project_id, picked)
            if local_asset is None:
                local_asset_path = resolve_project_media_file(
                    project_id,
                    picked,
                    allowed_roots=allowed_roots,
                )
                local_asset = str(local_asset_path) if local_asset_path is not None else None
            if local_asset is None:
                errors.append(f"资产 {asset_id} 的本地文件超出当前项目允许范围")
                continue
            resolved.append(local_asset)
            continue

        if ref.startswith("node:"):
            raw_node_id = ref[len("node:"):].strip()
            node_id, node_error = await _resolve_node_id_for_reference(project_id, raw_node_id)
            if node_error:
                errors.append(node_error)
                continue
            node_id = node_id or raw_node_id
            async with session_scope() as session:
                stmt = select(Asset).where(Asset.node_id == node_id)
                rows = (await session.exec(stmt)).all()
            picked = None
            for asset in rows:
                meta = {}
                if asset.metadata_json:
                    try:
                        meta = json.loads(asset.metadata_json)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                if meta.get("status") == "failed":
                    continue
                # 与 asset:<id> 分支保持一致：url 是 http(s) 才使用，否则优先
                # 交给 UMA 可读取的本地 path。register_asset 给本地图写入的
                # asset.url 是 "/api/media/..." 形式的相对 API URL。
                url = asset.url
                path = asset.path
                if url and (url.startswith("http://") or url.startswith("https://")):
                    picked = url
                elif path:
                    picked = path
                elif url:
                    picked = url
                if picked:
                    break
            if not picked:
                picked = await _pick_node_output_reference(project_id, node_id)
            if not picked:
                display_id = raw_node_id if raw_node_id != node_id else node_id
                errors.append(f"节点 {display_id} 没有可用的图片资产")
                continue
            resolved.append(picked)
            continue

        target = resolve_project_media_file(
            project_id,
            ref,
            allowed_roots=allowed_roots,
        )
        if target is None:
            errors.append(f"参考图文件不存在或超出当前项目允许范围: {ref}")
            continue
        resolved.append(str(target))

    return resolved, errors


async def _resolve_reference_media(
    project_id: str,
    kind: str,
    refs: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Resolve video/audio node references for the UMA media boundary."""
    resolved: list[str] = []
    errors: list[str] = []
    allowed_roots = await project_media_roots(project_id)
    for raw in refs or []:
        ref = str(raw or "").strip()
        if not ref:
            errors.append(f"{kind} 引用为空")
            continue
        if ref.startswith("upload:"):
            relative = ref.removeprefix("upload:").strip().lstrip("/")
            ref = relative if relative.startswith("uploads/") else f"uploads/{relative}"
        if ref.startswith(("http://", "https://", "data:")):
            resolved.append(ref)
            continue
        if ref.startswith("asset:"):
            asset_id = ref.removeprefix("asset:").strip()
            async with session_scope() as session:
                asset = await session.get(Asset, asset_id)
            if asset is None or asset.project_id != project_id:
                errors.append(f"找不到资产 asset:{asset_id}")
                continue
            picked = asset.path or asset.url
            if not picked:
                errors.append(f"资产 {asset_id} 没有可用的 url 或 path")
                continue
            ref = str(picked)
        elif ref.startswith("node:"):
            raw_node_id = ref.removeprefix("node:").strip()
            node_id, node_error = await _resolve_node_id_for_reference(project_id, raw_node_id)
            if node_error:
                errors.append(node_error)
                continue
            picked = await _pick_node_output_reference(project_id, node_id or raw_node_id)
            if not picked:
                errors.append(f"节点 {raw_node_id} 没有可用的 {kind} 产物")
                continue
            ref = picked
        if ref.startswith(("http://", "https://", "data:")):
            resolved.append(ref)
            continue
        local_from_url = _project_media_path_from_url(project_id, ref)
        if local_from_url:
            resolved.append(local_from_url)
            continue
        upload_prefix = f"/api/uploads/{project_id}/file/"
        if ref.startswith(upload_prefix):
            ref = ref[len(upload_prefix) :].lstrip("/")
        candidate = resolve_project_media_file(
            project_id,
            ref,
            allowed_roots=allowed_roots,
        )
        if candidate is not None:
            resolved.append(str(candidate))
        else:
            errors.append(f"{kind} 文件不存在或超出当前项目允许范围: {raw}")
    return resolved, errors


# ---- provider preset params ----


def _image_target_presets() -> dict[str, dict[str, Any]]:
    from app.services.image_target_catalog import image_model_presets

    return image_model_presets()


# Parameter descriptions for settings UI hints
_PARAM_DESCRIPTIONS: dict[str, str] = {
    "size": "输出尺寸，如 1024x1792(9:16竖屏)/1792x1024(横屏)/1024x1024(方形)",
    "quality": "质量: standard(标准) / hd(高清)",
    "steps": "推理步数，越多越精细但也越慢(4-50)",
    "guidance_scale": "提示词引导强度，低=创意/高=忠实(1.0-15.0)",
    "sampler": "采样器，如 DPM++ 2M Karras / Euler a",
    "cfg_scale": "同 guidance_scale，部分 API 用此字段名",
    "seed": "随机种子，固定可复现(整数)",
    "stylize": "Midjourney 风格化强度(0-1000)",
    "negative_prompt": "负面提示词(默认留空)",
}


def match_preset(model_name: str) -> dict[str, Any] | None:
    """Return recommended default params for a given model name.

    Presets come from the UMA image target catalog.
    """
    presets = _image_target_presets()
    name_lower = model_name.lower().replace("_", "-").replace(" ", "-")
    for key in sorted(presets.keys(), key=lambda k: -len(k)):
        if key == "*":
            continue
        if key in name_lower:
            return dict(presets[key])
    return dict(presets.get("*", {}))


def list_presets() -> dict[str, dict[str, Any]]:
    """Return image provider presets declared by the image target catalog."""
    return _image_target_presets()


def get_preset_descriptions() -> dict[str, str]:
    """Return parameter descriptions (for settings UI tooltips)."""
    return dict(_PARAM_DESCRIPTIONS)


async def generate_image_with_provider(
    project_id: str,
    prompt: str,
    negative_prompt: str | None = None,
    size: str = "1024x1792",
    quality: str | None = None,
    model_name: str | None = None,
    n: int = 1,
    reference_images: list[str] | None = None,
    save_locally: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if model_name:
        provider = await _get_provider_by_name("image", model_name)
        if not provider:
            return {"ok": False, "error": f"Image provider '{model_name}' not found"}
    else:
        provider = await _get_active_provider("image")
        if not provider:
            return {
                "ok": False,
                "error": "No active image provider configured. Use the settings panel or config API to add one.",
            }

    extra_override = extra or {}

    resolved_refs: list[str] = []
    ref_errors: list[str] = []
    if reference_images:
        resolved_refs, ref_errors = await _resolve_reference_images(project_id, reference_images)
        if ref_errors:
            # 部分解析会压缩数组，导致提示词中的后续“图片N”全部错位。
            return {
                "ok": False,
                "provider": provider.name,
                "model": provider.model_name,
                "error": "参考图无法完整解析: " + "; ".join(ref_errors),
                "error_kind": "bad_request",
                "reference_warnings": ref_errors,
            }

    attempts: list[dict[str, Any]] = []
    last_attempt_size = size
    last_attempt_quality = quality

    async def _one_call(_size: str, _quality: str | None) -> dict[str, Any]:
        if provider.api_format != "universal_adapter":
            return {
                "error": "图片 provider 的 api_format 必须是 universal_adapter",
                "error_kind": "bad_config",
            }
        from app.services.universal_adapter_service import universal_adapter_service

        return await universal_adapter_service.generate_image(
            provider=provider,
            provider_params=_parse_extra(provider),
            project_id=project_id,
            prompt=prompt,
            negative_prompt=negative_prompt,
            size=_size,
            quality=_quality,
            count=n,
            reference_images=resolved_refs or None,
            extra=extra_override,
        )

    result = await _one_call(size, quality)
    attempts.append({
        "attempt": 1,
        "size": size,
        "quality": quality,
        "ok": "error" not in result,
        "error": result.get("error") if "error" in result else None,
        "error_kind": result.get("error_kind"),
        "http_code": result.get("http_code"),
        "provider_msg": result.get("provider_msg"),
    })

    if "error" in result:
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "error": result["error"],
            "error_kind": result.get("error_kind"),
            "http_code": result.get("http_code"),
            "provider_msg": result.get("provider_msg"),
            "endpoint": result.get("endpoint"),
            "attempts": attempts,
            "size_requested": size,
            "size_final": last_attempt_size,
            "quality_requested": quality,
            "quality_final": last_attempt_quality,
            "downgraded": False,
        }

    images = result.get("images", [])
    output_images = []
    requested_dims = _parse_image_size(size)
    for img in images:
        remote_url = img.get("url")
        b64 = img.get("b64")
        local_path: str | None = None
        local_url: str | None = None
        actual_dimensions: tuple[int, int] | None = None

        if save_locally:
            filename = f"{uuid.uuid4().hex[:12]}.png"
            dest = _storage_path(project_id, filename)
            try:
                if b64:
                    image_bytes = base64.b64decode(b64)
                    actual_dimensions = _image_dimensions_from_bytes(image_bytes)
                    dest.write_bytes(image_bytes)
                    local_path = str(dest)
                elif remote_url:
                    async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
                        r = await client.get(remote_url)
                    if r.status_code == 200:
                        actual_dimensions = _image_dimensions_from_bytes(r.content)
                        dest.write_bytes(r.content)
                        local_path = str(dest)
            except Exception:
                local_path = None
            if local_path:
                local_url = f"/api/media/{project_id}/{filename}"

        # `url` is what consumers should display: prefer local (stable), fall back to remote
        image_output = {
            "url": local_url or remote_url,
            "local_url": local_url,
            "local_path": local_path,
            "remote_url": remote_url,
        }
        if actual_dimensions:
            width, height = actual_dimensions
            image_output.update({
                "width": width,
                "height": height,
                "actual_size": f"{width}x{height}",
                "actual_aspect_ratio": f"{width}:{height}",
            })
        output_images.append(image_output)

    for image_output in output_images:
        actual_size = image_output.get("actual_size")
        actual_dims = _parse_image_size(actual_size)
        if requested_dims and actual_dims:
            requested_ratio = requested_dims[0] / requested_dims[1]
            actual_ratio = actual_dims[0] / actual_dims[1]
            if not _ratio_close(requested_ratio, actual_ratio):
                return _image_size_mismatch_error(
                    provider=provider,
                    requested_size=size,
                    actual_size=str(actual_size),
                    images=output_images,
                    attempts=attempts,
                    quality=last_attempt_quality,
                )

    primary_actual_size = next(
        (img.get("actual_size") for img in output_images if img.get("actual_size")),
        None,
    )
    primary_actual_ratio = next(
        (img.get("actual_aspect_ratio") for img in output_images if img.get("actual_aspect_ratio")),
        None,
    )

    return {
        "ok": True,
        "provider": provider.name,
        "model": provider.model_name,
        "images": output_images,
        "reference_images": list(reference_images) if reference_images else [],
        "resolved_reference_images": resolved_refs,
        "reference_warnings": ref_errors,
        "partial_error": result.get("partial_error"),
        "attempts": attempts,
        "size_requested": size,
        "size_final": primary_actual_size or last_attempt_size,
        "actual_size": primary_actual_size,
        "actual_aspect_ratio": primary_actual_ratio,
        "quality_requested": quality,
        "quality_final": last_attempt_quality,
        "downgraded": False,
    }


async def generate_audio_with_provider(
    project_id: str,
    prompt: str,
    title: str | None = None,
    style: str | None = None,
    instrumental: bool | None = None,
    model_name: str | None = None,
    extra: dict[str, Any] | None = None,
    save_locally: bool = True,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if model_name:
        provider = await _get_provider_by_name_or_model("audio", model_name)
    else:
        provider = await _get_active_provider("audio")
    if not provider:
        label = f" '{model_name}'" if model_name else ""
        return {
            "ok": False,
            "status": "failed",
            "error": f"No active audio provider{label} configured. Use the settings panel or config API to add one.",
            "error_kind": "bad_config",
        }

    extra_override = extra or {}
    if provider.api_format != "universal_adapter":
        result = {
            "error": "音频 provider 的 api_format 必须是 universal_adapter",
            "error_kind": "bad_config",
            "status": "failed",
        }
    else:
        from app.services.universal_adapter_service import universal_adapter_service

        result = await universal_adapter_service.submit_audio(
            provider=provider,
            provider_params=_parse_extra(provider),
            project_id=project_id,
            prompt=prompt,
            title=title,
            style=style,
            instrumental=instrumental,
            extra=extra_override,
            save_locally=save_locally,
            wait_for_completion=wait_for_completion,
        )

    ok = bool(result.get("ok"))
    return {
        **result,
        "ok": ok,
        "provider": result.get("provider") or provider.name,
        "model": result.get("model") or provider.model_name,
        "status": result.get("status") or ("completed" if ok else "failed"),
    }


async def poll_audio_with_provider(
    project_id: str,
    job_id: str,
    model_name: str | None = None,
    extra: dict[str, Any] | None = None,
    save_locally: bool = True,
    progress_callback: ProgressCallback | None = None,
    provider_task_id: str | None = None,
    adapter_resume_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if model_name:
        provider = await _get_provider_by_name_or_model("audio", model_name)
    else:
        provider = await _get_active_provider("audio")
    if not provider:
        label = f" '{model_name}'" if model_name else ""
        return {
            "ok": False,
            "status": "failed",
            "error": f"No active audio provider{label} configured. Use the settings panel or config API to add one.",
            "error_kind": "bad_config",
            "job_id": job_id,
        }

    if provider.api_format != "universal_adapter":
        result = {
            "error": "音频 provider 的 api_format 必须是 universal_adapter",
            "error_kind": "bad_config",
            "status": "failed",
            "job_id": job_id,
        }
    else:
        from app.services.universal_adapter_service import universal_adapter_service

        result = await universal_adapter_service.poll(
            provider=provider,
            provider_params=_parse_extra(provider),
            project_id=project_id,
            job_id=job_id,
            kind="audio",
            save_locally=save_locally,
            provider_task_id=provider_task_id,
            resume_request=adapter_resume_request,
            progress_callback=progress_callback,
        )

    ok = bool(result.get("ok"))
    return {
        **result,
        "ok": ok,
        "provider": result.get("provider") or provider.name,
        "model": result.get("model") or provider.model_name,
        "status": result.get("status") or ("completed" if ok else "failed"),
        "job_id": result.get("job_id") or job_id,
    }


async def test_provider(provider_id: str) -> dict[str, Any]:
    provider = await _get_provider_by_id(provider_id)
    if not provider:
        return {"ok": False, "error": "Provider not found"}

    if provider.api_format == "universal_adapter":
        from app.services.universal_adapter_service import universal_adapter_service

        return await universal_adapter_service.inspect_provider(
            provider=provider,
            provider_params=_parse_extra(provider),
        )

    if provider.kind in {"image", "video", "audio"}:
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "error": f"{provider.kind} provider 的 api_format 必须是 universal_adapter",
            "error_kind": "bad_config",
            "supported_api_formats": ["universal_adapter"],
        }

    return {"ok": False, "error": f"Unknown provider kind: {provider.kind}"}


async def generate_video_with_provider(
    project_id: str,
    prompt: str,
    first_frame_url: str | None = None,
    last_frame_url: str | None = None,
    duration_seconds: int = 4,
    model_name: str | None = None,
    extra: dict[str, Any] | None = None,
    reference_images: list[str] | None = None,
    save_locally: bool = True,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if model_name:
        provider = await _get_provider_by_name_or_model("video", model_name)
    else:
        provider = await _get_active_provider("video")
    if not provider:
        label = f" '{model_name}'" if model_name else ""
        return {
            "ok": False,
            "status": "failed",
            "error": f"No active video provider{label} configured. Use the settings panel or config API to add one.",
            "error_kind": "bad_config",
        }

    resolved_refs: list[str] = []
    ref_errors: list[str] = []
    if reference_images:
        resolved_refs, ref_errors = await _resolve_reference_images(project_id, reference_images)
        if ref_errors:
            return {
                "ok": False,
                "provider": provider.name,
                "model": provider.model_name,
                "status": "failed",
                "error": "视频参考图无法完整解析: " + "; ".join(ref_errors),
                "error_kind": "bad_request",
                "reference_images": list(reference_images),
                "resolved_reference_images": [],
                "reference_warnings": ref_errors,
            }

    extra_override = extra or {}
    if provider.api_format != "universal_adapter":
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "status": "failed",
            "error": "视频 provider 的 api_format 必须是 universal_adapter",
            "error_kind": "bad_config",
        }
    from app.services.universal_adapter_service import universal_adapter_service

    raw_video_refs = list(extra_override.get("reference_videos") or [])
    raw_audio_refs = list(extra_override.get("reference_audios") or [])
    for item in extra_override.get("media_references") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or item.get("url") or item.get("source") or "").strip()
        role = str(item.get("role") or item.get("kind") or item.get("type") or "").lower()
        if not ref:
            continue
        if "audio" in role:
            raw_audio_refs.append(ref)
        elif "video" in role:
            raw_video_refs.append(ref)
    resolved_videos, video_errors = await _resolve_reference_media(
        project_id, "video", raw_video_refs
    )
    resolved_audios, audio_errors = await _resolve_reference_media(
        project_id, "audio", raw_audio_refs
    )
    result = await universal_adapter_service.submit_video(
        provider=provider,
        provider_params=_parse_extra(provider),
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=resolved_refs or None,
        reference_videos=resolved_videos or None,
        reference_audios=resolved_audios or None,
        extra=extra_override,
        save_locally=save_locally,
        wait_for_completion=wait_for_completion,
    )

    ok = bool(result.get("ok"))
    warnings = [
        *ref_errors,
        *video_errors,
        *audio_errors,
        *(
            result.get("reference_warnings")
            if isinstance(result.get("reference_warnings"), list)
            else []
        ),
    ]
    return {
        **result,
        "ok": ok,
        "provider": result.get("provider") or provider.name,
        "model": result.get("model") or provider.model_name,
        "status": result.get("status") or ("completed" if ok else "failed"),
        "reference_images": list(reference_images) if reference_images else [],
        "resolved_reference_images": resolved_refs,
        "reference_warnings": warnings,
        "resolved_media_references": [
            *({"kind": "video", "ref": value} for value in resolved_videos),
            *({"kind": "audio", "ref": value} for value in resolved_audios),
        ],
        "first_frame_url": first_frame_url,
        "last_frame_url": last_frame_url,
    }


async def poll_video_with_provider(
    project_id: str,
    job_id: str,
    model_name: str | None = None,
    extra: dict[str, Any] | None = None,
    save_locally: bool = True,
    progress_callback: ProgressCallback | None = None,
    provider_task_id: str | None = None,
    adapter_resume_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if model_name:
        provider = await _get_provider_by_name_or_model("video", model_name)
    else:
        provider = await _get_active_provider("video")
    if not provider:
        label = f" '{model_name}'" if model_name else ""
        return {
            "ok": False,
            "status": "failed",
            "error": f"No active video provider{label} configured. Use the settings panel or config API to add one.",
            "error_kind": "bad_config",
            "job_id": job_id,
        }

    if provider.api_format != "universal_adapter":
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "status": "failed",
            "error": "视频 provider 的 api_format 必须是 universal_adapter",
            "error_kind": "bad_config",
            "job_id": job_id,
        }
    from app.services.universal_adapter_service import universal_adapter_service

    result = await universal_adapter_service.poll(
        provider=provider,
        job_id=job_id,
        kind="video",
        progress_callback=progress_callback,
        provider_params=_parse_extra(provider),
        project_id=project_id,
        save_locally=save_locally,
        provider_task_id=provider_task_id,
        resume_request=adapter_resume_request,
    )

    ok = bool(result.get("ok"))
    return {
        **result,
        "ok": ok,
        "provider": result.get("provider") or provider.name,
        "model": result.get("model") or provider.model_name,
        "status": result.get("status") or ("completed" if ok else "failed"),
        "job_id": result.get("job_id") or job_id,
    }
