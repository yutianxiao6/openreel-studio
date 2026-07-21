"""Media provider abstraction layer.

Image, video, and audio HTTP contracts are loaded from declarative protocol catalogs.
Legacy media formats remain as migration inputs, but model request bodies live
in config protocol files rather than Python adapter branches.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import mimetypes
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlmodel import select

from app.config import settings
from app.db.models import Asset, MediaProvider, WorkflowNode
from app.db.session import session_scope
from app.services.media_url_signing import MediaURLSigningError, sign_media_url


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


@dataclass(frozen=True)
class VideoProviderAdapter:
    name: str
    display_name: str
    api_formats: frozenset[str]
    model_names: frozenset[str]
    endpoint_for: Any
    generate: Any
    poll: Any
    requires_base_url: bool = False
    source_images_min: int | None = None
    source_images_max: int | None = None
    field_types: dict[str, str] | None = None
    supported_resolutions: frozenset[str] = frozenset()
    supported_ratios: frozenset[str] = frozenset()
    source_image_transport: str | None = None


@dataclass(frozen=True)
class JsonVideoTaskSpec:
    name: str
    display_name: str
    api_formats: frozenset[str]
    model_names: frozenset[str]
    create_path: str
    query_path_template: str
    upload_path: str | None
    upload_base_url_param: str | None
    payload_fields: dict[str, str]
    field_types: dict[str, str]
    source_images_field: str | None
    source_images_min: int
    source_images_max: int
    source_image_transport: str
    upload_file_field: str
    upload_response_url_paths: tuple[str, ...]
    task_id_paths: tuple[str, ...]
    status_path: str
    progress_path: str | None
    result_url_paths: tuple[str, ...]
    done_statuses: frozenset[str]
    failed_statuses: frozenset[str]
    running_statuses: frozenset[str]
    supported_ratios: frozenset[str]
    supported_resolutions: frozenset[str]
    default_ratio: str
    default_resolution: str
    duration_min: int
    duration_max: int
    long_duration_after: int | None = None
    long_duration_resolution: str | None = None
    resolution_output: str = "lower"


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


def _media_video_timeout() -> httpx.Timeout:
    try:
        seconds = max(
            120.0,
            float(os.getenv("DRAMA_VIDEO_PROVIDER_TIMEOUT_SECONDS", "600") or "600"),
        )
    except (TypeError, ValueError):
        seconds = 600.0
    connect_seconds = min(60.0, seconds)
    return httpx.Timeout(seconds, connect=connect_seconds)


def _media_audio_timeout() -> httpx.Timeout:
    try:
        seconds = max(
            120.0,
            float(os.getenv("DRAMA_AUDIO_PROVIDER_TIMEOUT_SECONDS", "600") or "600"),
        )
    except (TypeError, ValueError):
        seconds = 600.0
    connect_seconds = min(60.0, seconds)
    return httpx.Timeout(seconds, connect=connect_seconds)


def _storage_path(project_id: str, filename: str) -> Path:
    base = Path(getattr(settings, "STORAGE_DIR", "./storage"))
    d = base / project_id / "generated_images"
    d.mkdir(parents=True, exist_ok=True)
    return d / filename


def _storage_video_path(project_id: str, filename: str) -> Path:
    base = Path(getattr(settings, "STORAGE_DIR", "./storage"))
    d = base / project_id / "generated_videos"
    d.mkdir(parents=True, exist_ok=True)
    return d / filename


def _storage_audio_path(project_id: str, filename: str) -> Path:
    base = Path(getattr(settings, "STORAGE_DIR", "./storage"))
    d = base / project_id / "generated_audio"
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
            if candidate.exists() and candidate.is_file():
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
    if not node or not node.output_json:
        return None
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
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.exists() and path.is_file():
            return str(path.resolve())
    return None


async def _get_active_provider(kind: str) -> MediaProvider | None:
    async with session_scope() as session:
        result = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.is_active == True)
            .where(MediaProvider.enabled == True)
            .order_by(MediaProvider.created_at, MediaProvider.id)
        )
        provider = result.first()
        if provider:
            return provider
        fallback = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.enabled == True)
            .order_by(MediaProvider.created_at, MediaProvider.id)
        )
        return fallback.first()


async def _get_provider_by_name(kind: str, name: str) -> MediaProvider | None:
    async with session_scope() as session:
        result = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.name == name)
            .where(MediaProvider.enabled == True)
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
            .where(MediaProvider.enabled == True)
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
            return node_id, None
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

    Accepts: 'asset:<id>' / 'node:<id>' / 'http(s)://...' / a path relative to
    STORAGE_PATH/<project_id>/ (or absolute). Returns (resolved, errors). The
    resolved list contains URLs that providers can fetch (http/https) or local
    absolute paths (which callers convert to base64 if the provider needs it).
    """
    if not refs:
        return [], []

    resolved: list[str] = []
    errors: list[str] = []
    storage_root = settings.storage_path_resolved

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
            if not asset:
                errors.append(f"找不到资产 asset:{asset_id}")
                continue
            url = asset.url
            path = asset.path
            picked = url if url and (url.startswith("http://") or url.startswith("https://")) else (path or url)
            if not picked:
                errors.append(f"资产 {asset_id} 没有可用的 url 或 path")
                continue
            resolved.append(picked)
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
                # 与 asset:<id> 分支保持一致:url 是 http(s) 才用 url,否则优先用本地 path。
                # 因为 register_asset 给本地图写的 asset.url 是 "/api/media/..." 这种
                # 相对 API URL,既不是 http 也不是文件路径,直接用会被 _ref_to_data_url
                # 当成 "找不到" 报错。
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

        candidate = Path(ref)
        if candidate.is_absolute():
            target = candidate.resolve()
        else:
            target = (storage_root / project_id / ref).resolve()
        if not target.exists():
            errors.append(f"参考图文件不存在: {ref}")
            continue
        resolved.append(str(target))

    return resolved, errors


def _is_remote_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


async def _ref_to_data_url(ref: str) -> str | None:
    """Convert a resolved reference (remote URL or local path) into a data URL
    suitable for providers that don't accept remote URLs in their image input.
    """
    try:
        if _is_remote_url(ref):
            async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
                resp = await client.get(ref)
            if resp.status_code != 200:
                return None
            content = resp.content
            mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
        else:
            p = Path(ref)
            if not p.exists():
                return None
            content = p.read_bytes()
            mime = "image/png"
            ext = p.suffix.lower().lstrip(".")
            if ext in {"jpg", "jpeg"}:
                mime = "image/jpeg"
            elif ext == "webp":
                mime = "image/webp"
            elif ext == "gif":
                mime = "image/gif"
        return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    except Exception:
        return None


def _http_error_kind(code: int) -> str:
    if code == 401 or code == 403:
        return "auth"
    if code == 404:
        return "not_found"
    if code == 429:
        return "rate_limit"
    if code == 400 or code == 422:
        return "bad_request"
    if 500 <= code < 600:
        return "server_error"
    return "http_error"


def _make_http_error(code: int, text: str, endpoint: str) -> dict[str, Any]:
    return {
        "error": f"外部媒体服务 HTTP {code}: {text[:400]}",
        "error_kind": _http_error_kind(code),
        "error_source": "external_media_provider",
        "http_code": code,
        "provider_msg": text[:800],
        "endpoint": endpoint,
    }


_VIDEO_HTTP_V1_FORMATS = {"video_http_v1"}
_VIDEO_HTTP_V1_PROTOCOL_VERSION = "openreel.video_provider.v1"
_VIDEO_HTTP_V1_CATALOG_VERSION = "openreel.video_provider_catalog.v1"
_VIDEO_HTTP_V1_CATALOG_FILE = Path("config") / "video_provider_protocols" / "catalog.json"
_VIDEO_HTTP_V1_LEGACY_PROTOCOLS_BY_FORMAT = {
    "t8_grok_video_3": "t8_grok_video_3_json_task",
    "grok_1_5": "grok_1_5_multipart",
    "xai_video": "xai_grok_imagine_video_1_5",
    "lingke_media_generate": "lingke_media_generate_json_task",
    "lk888_media_generate": "lingke_media_generate_json_task",
}
_VIDEO_HTTP_V1_PROTOCOLS_BY_MODEL = {
    "grok-video-3": "t8_grok_video_3_json_task",
    "grok-1.5-video-15s": "grok_1_5_multipart",
    "grok-imagine-video-1.5": "xai_grok_imagine_video_1_5",
}
_IMAGE_HTTP_V1_FORMATS = {"image_http_v1"}
_IMAGE_HTTP_V1_PROTOCOL_VERSION = "openreel.image_provider.v1"
_IMAGE_HTTP_V1_CATALOG_VERSION = "openreel.image_provider_catalog.v1"
_IMAGE_HTTP_V1_CATALOG_FILE = Path("config") / "image_provider_protocols" / "catalog.json"
_IMAGE_HTTP_V1_LEGACY_PROTOCOLS_BY_FORMAT = {
    "openai": "openai_images_generations",
}
_AUDIO_HTTP_V1_FORMATS = {"audio_http_v1"}
_AUDIO_HTTP_V1_PROTOCOL_VERSION = "openreel.audio_provider.v1"
_AUDIO_HTTP_V1_CATALOG_VERSION = "openreel.audio_provider_catalog.v1"
_AUDIO_HTTP_V1_CATALOG_FILE = Path("config") / "audio_provider_protocols" / "catalog.json"
_AUDIO_HTTP_V1_LEGACY_PROTOCOLS_BY_FORMAT = {
    "openai_tts": "openai_audio_speech",
    "tts": "openai_audio_speech",
    "openai_speech": "openai_audio_speech",
    "openai_audio_speech": "openai_audio_speech",
    "suno_compatible": "newapi_suno_music",
    "suno": "suno_compatible_generate",
    "suno_api": "suno_compatible_generate",
}
_ARK_VIDEO_FORMATS = {"volcengine_ark", "ark", "ark_video"}
_ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_ARK_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
_ARK_RATIO_ALIASES = {
    "landscape": "16:9",
    "horizontal": "16:9",
    "portrait": "9:16",
    "vertical": "9:16",
    "square": "1:1",
}
_ARK_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}
_ARK_IMAGE_ROLES = {"reference_image", "first_frame", "last_frame"}
_VIDEO_HTTP_MEDIA_KINDS = {"image", "video", "audio"}
_XAI_VIDEO_FORMATS = {"xai_video"}
_GROK_1_5_VIDEO_FORMATS = {"grok_1_5"}
_T8_GROK_VIDEO_3_FORMATS = {"t8_grok_video_3"}
_LINGKE_MEDIA_GENERATE_FORMATS = {"lingke_media_generate", "lk888_media_generate"}
_XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"
_XAI_DONE_STATUSES = {"done", "completed", "succeeded"}
_XAI_FAILED_STATUSES = {"failed", "expired", "cancelled", "canceled"}
_XAI_RUNNING_STATUSES = {"running", "processing", "in_progress"}
_XAI_VIDEO_RESOLUTIONS = {"480p", "720p"}
_GROK_1_5_VIDEO_RESOLUTIONS = {"480p", "720p"}
_T8_GROK_VIDEO_3_RATIOS = {"2:3", "3:2", "16:9", "9:16", "1:1"}
_T8_GROK_VIDEO_3_RESOLUTIONS = {"480p", "720p", "1080p"}
_T8_GROK_VIDEO_3_DONE_STATUSES = {"success", "done", "completed", "succeeded"}
_T8_GROK_VIDEO_3_FAILED_STATUSES = {"failure", "failed", "error", "expired", "cancelled", "canceled"}
_T8_GROK_VIDEO_3_RUNNING_STATUSES = {"not_start", "queued", "pending", "running", "processing", "in_progress"}
_T8_GROK_VIDEO_3_SPEC = JsonVideoTaskSpec(
    name="t8_grok_video_3",
    display_name="T8 Grok Video 3",
    api_formats=frozenset(_T8_GROK_VIDEO_3_FORMATS),
    model_names=frozenset({"grok-video-3"}),
    create_path="/videos/generations",
    query_path_template="/videos/generations/{task_id}",
    upload_path="/files",
    upload_base_url_param="upload_base_url",
    payload_fields={
        "prompt": "prompt",
        "model": "model",
        "ratio": "ratio",
        "duration": "duration",
        "resolution": "resolution",
        "images": "images",
        "seed": "seed",
    },
    field_types={
        "prompt": "string",
        "model": "string",
        "ratio": "string",
        "duration": "integer",
        "resolution": "string_upper",
        "images": "url_list",
        "seed": "integer",
    },
    source_images_field="images",
    source_images_min=0,
    source_images_max=7,
    source_image_transport="upload_url_list",
    upload_file_field="file",
    upload_response_url_paths=("url", "data.url"),
    task_id_paths=("task_id", "id", "job_id"),
    status_path="status",
    progress_path="progress",
    result_url_paths=("data.output", "output", "video_url", "url", "data.video_url", "data.url"),
    done_statuses=frozenset(_T8_GROK_VIDEO_3_DONE_STATUSES),
    failed_statuses=frozenset(_T8_GROK_VIDEO_3_FAILED_STATUSES),
    running_statuses=frozenset(_T8_GROK_VIDEO_3_RUNNING_STATUSES),
    supported_ratios=frozenset(_T8_GROK_VIDEO_3_RATIOS),
    supported_resolutions=frozenset(_T8_GROK_VIDEO_3_RESOLUTIONS),
    default_ratio="16:9",
    default_resolution="720p",
    duration_min=6,
    duration_max=30,
    long_duration_after=15,
    long_duration_resolution="720p",
    resolution_output="upper",
)
_LINGKE_MEDIA_GENERATE_SPEC = JsonVideoTaskSpec(
    name="lingke_media_generate",
    display_name="Lingke media.generate",
    api_formats=frozenset(_LINGKE_MEDIA_GENERATE_FORMATS),
    model_names=frozenset(),
    create_path="/media/generate",
    query_path_template="/skills/task-status?task_id={task_id}",
    upload_path=None,
    upload_base_url_param=None,
    payload_fields={
        "prompt": "params.prompt",
        "model": "model",
        "ratio": "params.aspect_ratio",
        "duration": "params.duration",
        "resolution": "params.resolution",
        "images": "params.images",
        "seed": "params.seed",
    },
    field_types={
        "prompt": "string",
        "model": "string",
        "ratio": "string",
        "duration": "string",
        "resolution": "string",
        "images": "url_list",
        "seed": "integer",
    },
    source_images_field="params.images",
    source_images_min=0,
    source_images_max=12,
    source_image_transport="configurable_url_or_data_url_list",
    upload_file_field="file",
    upload_response_url_paths=(),
    task_id_paths=("data.task_id", "data.taskId", "data.id", "task_id", "taskId", "id", "job_id", "data.job_id"),
    status_path="state",
    progress_path="progress",
    result_url_paths=(
        "result_url",
        "data.result_url",
        "data.resultUrl",
        "data.video_url",
        "data.url",
        "data.output",
        "data.result.video_url",
        "data.result.url",
        "result_url",
        "video_url",
        "url",
    ),
    done_statuses=frozenset({"success", "succeeded", "completed", "complete", "done"}),
    failed_statuses=frozenset({"failure", "failed", "error", "expired", "cancelled", "canceled"}),
    running_statuses=frozenset({"not_start", "queued", "pending", "running", "processing", "in_progress", "submitted"}),
    supported_ratios=frozenset({"2:3", "3:2", "16:9", "9:16", "1:1"}),
    supported_resolutions=frozenset({"480p", "720p", "1080p"}),
    default_ratio="16:9",
    default_resolution="720p",
    duration_min=1,
    duration_max=30,
    resolution_output="lower",
)
_VIDEO_RESOLUTION_ORDER = {"480p": 0, "720p": 1, "1080p": 2, "2k": 3, "4k": 4}
_VIDEO_MODEL_CALLING_DOC = "apps/api/app/skills/video_production/VIDEO_MODEL_CALLING.md"
def _video_model_calling_doc_hint() -> str:
    return (
        f"用已有 workspace 文件读取工具 file.workspace_read 读取 {_VIDEO_MODEL_CALLING_DOC}，"
        "再按模型支持的 source image、resolution 和 api_format 修正原 video 节点后重跑。"
    )


def _video_model_feedback(what_went_wrong: str, how_to_fix: str) -> dict[str, str]:
    return {
        "suggested_next": "read_video_model_calling_doc_then_update_original_video_node",
        "doc_path": _VIDEO_MODEL_CALLING_DOC,
        "what_went_wrong": what_went_wrong,
        "how_to_fix": how_to_fix,
        "retry_policy": "先修正原 video 节点字段，不要用相同参数重复调用。",
    }


def _with_video_model_doc_hint(error: dict[str, Any]) -> dict[str, Any]:
    if "hint" not in error:
        error["hint"] = _video_model_calling_doc_hint()
    if "model_feedback" not in error:
        error["model_feedback"] = _video_model_feedback(
            "The video provider rejected the current model call arguments.",
            f"Read {_VIDEO_MODEL_CALLING_DOC}, update the existing video node, then rerun it.",
        )
    return error


def _normalized_api_format(provider: MediaProvider) -> str:
    fmt = str(provider.api_format or "").strip().lower().replace("-", "_")
    if fmt == "raw_post":
        return "raw"
    return fmt


def _is_seedance_model(model_name: str | None) -> bool:
    text = str(model_name or "").lower().replace("_", "-")
    return "seedance" in text or "seadance" in text


def _is_seedance_2_model(model_name: str | None) -> bool:
    text = str(model_name or "").lower().replace("_", "-")
    return _is_seedance_model(text) and ("2-0" in text or "2.0" in text)


def _is_seedance_2_720p_max_model(model_name: str | None) -> bool:
    text = str(model_name or "").lower().replace("_", "-")
    return _is_seedance_2_model(text) and ("fast" in text or "mini" in text)


def _ark_supported_resolutions(model_name: str | None) -> set[str]:
    if _is_seedance_2_720p_max_model(model_name):
        return {"480p", "720p"}
    return {"480p", "720p", "1080p", "4k"}


def _seedance_2_variant_label(model_name: str | None) -> str:
    text = str(model_name or "").lower().replace("_", "-")
    if "mini" in text:
        return "Mini"
    if "fast" in text:
        return "Fast"
    return "Standard"


def _is_ark_video_provider(provider: MediaProvider) -> bool:
    fmt = _normalized_api_format(provider)
    return fmt in _ARK_VIDEO_FORMATS or _is_seedance_model(provider.model_name)


def _is_xai_video_provider(provider: MediaProvider) -> bool:
    fmt = _normalized_api_format(provider)
    return fmt in _XAI_VIDEO_FORMATS


def _is_grok_1_5_video_provider(provider: MediaProvider) -> bool:
    fmt = _normalized_api_format(provider)
    return fmt in _GROK_1_5_VIDEO_FORMATS


def _ark_video_tasks_endpoint(base_url: str | None) -> str:
    base = str(base_url or _ARK_DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = _ARK_DEFAULT_BASE_URL
    return base + "/contents/generations/tasks"


def _xai_video_api_base(base_url: str | None) -> str:
    base = str(base_url or _XAI_DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = _XAI_DEFAULT_BASE_URL
    return base


def _xai_video_generations_endpoint(base_url: str | None) -> str:
    return _xai_video_api_base(base_url) + "/videos/generations"


def _xai_video_query_endpoint(base_url: str | None, request_id: str) -> str:
    return f"{_xai_video_api_base(base_url)}/videos/{request_id}"


def _grok_1_5_video_api_base(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/")


def _grok_1_5_video_endpoint(base_url: str | None) -> str:
    return _grok_1_5_video_api_base(base_url) + "/videos"


def _grok_1_5_video_query_endpoint(base_url: str | None, request_id: str) -> str:
    return f"{_grok_1_5_video_api_base(base_url)}/videos/{request_id}"


def _json_video_task_api_root(base_url: str | None, spec: JsonVideoTaskSpec) -> str:
    del spec
    return str(base_url or "").strip().rstrip("/")


def _json_video_task_endpoint(base_url: str | None, spec: JsonVideoTaskSpec) -> str:
    return _json_video_task_api_root(base_url, spec) + spec.create_path


def _json_video_task_query_endpoint(base_url: str | None, spec: JsonVideoTaskSpec, task_id: str) -> str:
    return _json_video_task_api_root(base_url, spec) + spec.query_path_template.format(task_id=task_id)


def _json_video_task_upload_endpoint(
    base_url: str | None,
    spec: JsonVideoTaskSpec,
    params: dict[str, Any] | None = None,
) -> str:
    if not spec.upload_path:
        return ""
    upload_base = base_url
    if spec.upload_base_url_param:
        upload_base = str((params or {}).get(spec.upload_base_url_param) or "").strip()
    if not upload_base:
        return ""
    return _json_video_task_api_root(upload_base, spec) + spec.upload_path


def _t8_grok_video_3_endpoint(base_url: str | None) -> str:
    return _json_video_task_endpoint(base_url, _T8_GROK_VIDEO_3_SPEC)


def _lingke_media_generate_endpoint(base_url: str | None) -> str:
    return _json_video_task_endpoint(base_url, _LINGKE_MEDIA_GENERATE_SPEC)


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in value if str(item or "").strip()}
    return set()


def _int_set(value: Any) -> set[int]:
    out: set[int] = set()
    for item in _string_set(value):
        coerced = _coerce_int(item)
        if coerced is not None:
            out.add(coerced)
    return out


def _ark_ratio(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    text = _ARK_RATIO_ALIASES.get(text, text)
    return text if text in _ARK_RATIOS else None


def _ark_resolution(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"480p", "720p", "1080p", "4k"}:
        return text
    if "x" not in text:
        return None
    left, right = text.split("x", 1)
    width = _coerce_int(left)
    height = _coerce_int(right)
    if not width or not height:
        return None
    short_side = min(width, height)
    if short_side >= 2000:
        return "4k"
    if short_side >= 1000:
        return "1080p"
    if short_side >= 700:
        return "720p"
    return "480p"


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _unsupported_video_resolution_error(
    provider_label: str,
    model_name: str,
    raw_resolution: Any,
    supported_resolutions: set[str],
) -> dict[str, Any]:
    supported = sorted(supported_resolutions, key=lambda item: _VIDEO_RESOLUTION_ORDER.get(item, 99))
    return _with_video_model_doc_hint({
        "error": (
            f"{provider_label} model={model_name} 不支持 resolution={raw_resolution!r}；"
            f"支持: {', '.join(supported)}"
        ),
        "error_kind": "bad_request",
        "supported_resolutions": supported,
    })


def _ark_duration(value: Any, model_name: str | None) -> tuple[int | None, str | None]:
    duration = _coerce_int(value)
    if duration is None:
        return None, f"视频时长必须是整数，收到: {value!r}"
    if _is_seedance_2_model(model_name):
        if duration == -1 or 4 <= duration <= 15:
            return duration, None
        return None, "Seedance 2.0 duration 只支持 -1 或 4-15 秒"
    if duration < 1:
        return None, "视频时长必须大于 0 秒"
    return duration, None


def _json_video_task_supported_ratios(spec: JsonVideoTaskSpec, extra: dict[str, Any]) -> frozenset[str]:
    configured = _string_set(extra.get("supported_ratios") or extra.get("supported_aspect_ratios"))
    return frozenset(configured) if configured else spec.supported_ratios


def _json_video_task_supported_resolutions(spec: JsonVideoTaskSpec, extra: dict[str, Any]) -> frozenset[str]:
    configured = _string_set(extra.get("supported_resolutions") or extra.get("supported_sizes"))
    normalized = {
        item.lower() if re.match(r"^\d+p$", item.strip(), re.I) else item.strip()
        for item in configured
    }
    return frozenset(normalized) if normalized else spec.supported_resolutions


def _json_video_task_duration_bounds(spec: JsonVideoTaskSpec, extra: dict[str, Any]) -> tuple[int, int]:
    min_value = _coerce_int(extra.get("duration_min") or extra.get("min_duration"))
    max_value = _coerce_int(extra.get("duration_max") or extra.get("max_duration"))
    duration_min = min_value if min_value is not None and min_value > 0 else spec.duration_min
    duration_max = max_value if max_value is not None and max_value >= duration_min else spec.duration_max
    return duration_min, duration_max


def _json_video_task_ratio(value: Any, supported_ratios: frozenset[str]) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    text = _ARK_RATIO_ALIASES.get(text, text)
    return text if text in supported_ratios else None


def _json_video_task_resolution(value: Any, supported_resolutions: frozenset[str]) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    supported_lower = {item.lower() for item in supported_resolutions}
    if text in supported_lower:
        return text
    if text in {"480", "720", "1080"}:
        candidate = f"{text}p"
        return candidate if candidate in supported_lower else None
    if "x" not in text:
        return None
    left, right = text.split("x", 1)
    width = _coerce_int(left)
    height = _coerce_int(right)
    if not width or not height:
        return None
    short_side = min(width, height)
    for candidate in ("1080p", "720p", "480p"):
        if candidate not in supported_lower:
            continue
        threshold = 1000 if candidate == "1080p" else 700 if candidate == "720p" else 0
        if short_side >= threshold:
            return candidate
    return None


def _json_video_task_duration(
    value: Any,
    spec: JsonVideoTaskSpec,
    supported_durations: set[int] | None = None,
    duration_min: int | None = None,
    duration_max: int | None = None,
) -> tuple[int | None, str | None]:
    duration = _coerce_int(value)
    if duration is None:
        return None, f"{spec.name} duration 必须是整数，收到: {value!r}"
    if supported_durations:
        if duration in supported_durations:
            return duration, None
        return None, f"{spec.name} duration 只支持 {', '.join(str(item) for item in sorted(supported_durations))} 秒"
    minimum = duration_min if duration_min is not None else spec.duration_min
    maximum = duration_max if duration_max is not None else spec.duration_max
    if minimum <= duration <= maximum:
        return duration, None
    return None, f"{spec.name} duration 只支持 {minimum}-{maximum} 秒"


def _json_video_task_payload_resolution(resolution: str, spec: JsonVideoTaskSpec, extra: dict[str, Any]) -> str:
    output = str(extra.get("resolution_output") or extra.get("size_output") or spec.resolution_output).strip().lower()
    if output == "upper":
        return resolution.upper()
    return resolution


def _ark_image_role(value: Any) -> str:
    role = str(value or "reference_image").strip()
    return role if role in _ARK_IMAGE_ROLES else "reference_image"


async def _ark_image_url(
    project_id: str,
    ref: str | None,
    provider: MediaProvider,
    extra_override: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    return await _image_url_or_data_url_for_ref(
        project_id,
        str(ref or ""),
        provider,
        extra_override,
        default_transport="data_url",
    )


async def _build_ark_video_payload(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})

    model_name = str(extra.get("model") or provider.model_name or "").strip()
    if not model_name:
        return None, {"error": "Video provider 缺少 model_name", "error_kind": "bad_config"}

    duration, duration_error = _ark_duration(duration_seconds, model_name)
    if duration_error:
        return None, {"error": duration_error, "error_kind": "bad_request"}

    content: list[dict[str, Any]] = []
    clean_prompt = str(prompt or "").strip()
    if clean_prompt:
        content.append({"type": "text", "text": clean_prompt})

    reference_warnings: list[str] = []
    if first_frame_url:
        url, warning = await _ark_image_url(project_id, first_frame_url, provider, extra_override)
        if warning:
            reference_warnings.append(warning)
        elif url:
            content.append({"type": "image_url", "image_url": {"url": url}, "role": "first_frame"})

    roles = extra.get("reference_image_roles")
    role_list = roles if isinstance(roles, list) else []
    for idx, ref in enumerate(reference_images or []):
        url, warning = await _ark_image_url(project_id, ref, provider, extra_override)
        if warning:
            reference_warnings.append(warning)
            continue
        if url:
            role = _ark_image_role(role_list[idx] if idx < len(role_list) else "reference_image")
            content.append({"type": "image_url", "image_url": {"url": url}, "role": role})

    if last_frame_url:
        url, warning = await _ark_image_url(project_id, last_frame_url, provider, extra_override)
        if warning:
            reference_warnings.append(warning)
        elif url:
            content.append({"type": "image_url", "image_url": {"url": url}, "role": "last_frame"})

    if not content:
        return None, {"error": "Seedance 2.0 请求缺少 prompt 或参考内容", "error_kind": "bad_request"}

    payload: dict[str, Any] = {
        "model": model_name,
        "content": content,
        "duration": duration,
    }

    ratio = _ark_ratio(extra.get("ratio") or extra.get("aspect_ratio"))
    if ratio:
        payload["ratio"] = ratio

    raw_resolution = extra.get("resolution")
    if _has_value(raw_resolution):
        resolution = _ark_resolution(raw_resolution)
        supported_resolutions = _ark_supported_resolutions(model_name)
        if not resolution or resolution not in supported_resolutions:
            return None, _unsupported_video_resolution_error(
                f"Seedance 2.0 {_seedance_2_variant_label(model_name)}",
                model_name,
                raw_resolution,
                supported_resolutions,
            )
        payload["resolution"] = resolution

    for key in ("generate_audio", "watermark", "return_last_frame"):
        value = _coerce_bool(extra.get(key))
        if value is not None:
            payload[key] = value

    for key in ("seed", "priority", "execution_expires_after"):
        value = _coerce_int(extra.get(key))
        if value is not None:
            payload[key] = value

    safety_identifier = extra.get("safety_identifier")
    if safety_identifier:
        payload["safety_identifier"] = str(safety_identifier)

    tools = extra.get("tools")
    if isinstance(tools, list) and tools:
        payload["tools"] = tools

    result_meta: dict[str, Any] = {}
    if reference_warnings:
        result_meta["reference_warnings"] = reference_warnings
    return payload, result_meta or None


def _video_http_v1_endpoint(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/")


def _video_http_v1_join_url(base_url: str | None, path: str | None) -> str:
    raw_path = str(path or "").strip()
    if raw_path.startswith(("http://", "https://")):
        return raw_path
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return raw_path
    if not raw_path:
        return base
    if raw_path.startswith("?"):
        return base + raw_path
    return f"{base}/{raw_path.lstrip('/')}"


def _video_http_v1_protocol_catalog_paths() -> list[Path]:
    root = Path(settings.PROJECT_ROOT).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[4]
    env_value = os.environ.get("OPENREEL_VIDEO_PROTOCOLS_FILE", "").strip()
    candidates: list[Path] = [Path(env_value).expanduser()] if env_value else []
    candidates.extend([root / _VIDEO_HTTP_V1_CATALOG_FILE, repo_root / _VIDEO_HTTP_V1_CATALOG_FILE])
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        resolved = path.resolve()
        if resolved not in seen and resolved.exists() and resolved.is_file():
            seen.add(resolved)
            result.append(resolved)
    return result


def _video_http_v1_protocol_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "error_kind": "bad_config", **extra}


def _video_http_v1_load_protocol_catalog(
    path: Path,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, _video_http_v1_protocol_error(
            f"video_http_v1 protocol catalog 无法读取或不是合法 JSON: {path.name}",
            detail=str(exc),
        )
    if not isinstance(raw, dict):
        return None, _video_http_v1_protocol_error(f"video_http_v1 protocol catalog 必须是 JSON 对象: {path.name}")
    version = str(raw.get("version") or "").strip()
    if version != _VIDEO_HTTP_V1_CATALOG_VERSION:
        return None, _video_http_v1_protocol_error(
            f"video_http_v1 protocol catalog.version 必须是 {_VIDEO_HTTP_V1_CATALOG_VERSION}",
            catalog_file=path.name,
        )
    protocols = raw.get("protocols")
    if isinstance(protocols, list):
        mapped = {
            str(item.get("id") or "").strip(): item
            for item in protocols
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
    elif isinstance(protocols, dict):
        mapped = {str(key): value for key, value in protocols.items() if isinstance(value, dict)}
    else:
        mapped = {}
    if not mapped:
        return None, _video_http_v1_protocol_error(
            f"video_http_v1 protocol catalog 缺少 protocols",
            catalog_file=path.name,
        )
    return mapped, None


def _video_http_v1_protocol_from_catalog(
    protocol_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    wanted = str(protocol_id or "").strip()
    if not wanted:
        return None, _video_http_v1_protocol_error("video_http_v1 provider 缺少 params.video_protocol_id")
    paths = _video_http_v1_protocol_catalog_paths()
    if not paths:
        return None, _video_http_v1_protocol_error(
            "video_http_v1 未找到 protocol catalog 文件",
            expected=str(_VIDEO_HTTP_V1_CATALOG_FILE),
        )
    for path in paths[:1]:
        protocols, error = _video_http_v1_load_protocol_catalog(path)
        if error:
            return None, error
        assert protocols is not None
        raw = protocols.get(wanted)
        if isinstance(raw, dict):
            version = str(raw.get("version") or raw.get("protocol") or "").strip()
            if version != _VIDEO_HTTP_V1_PROTOCOL_VERSION:
                return None, _video_http_v1_protocol_error(
                    f"video_http_v1 protocol.version 必须是 {_VIDEO_HTTP_V1_PROTOCOL_VERSION}",
                    protocol_id=wanted,
                    catalog_file=path.name,
                )
            raw_id = str(raw.get("id") or wanted).strip()
            if raw_id != wanted:
                return None, _video_http_v1_protocol_error(
                    f"video_http_v1 protocol id 不匹配: 期望 {wanted}, 文件内是 {raw_id}",
                    protocol_id=wanted,
                    catalog_file=path.name,
                )
            return raw, None
        return None, _video_http_v1_protocol_error(
            f"video_http_v1 未找到协议: {wanted}",
            protocol_id=wanted,
            catalog_file=path.name,
            available_protocols=sorted(protocols.keys()),
        )

    return None, _video_http_v1_protocol_error(
        "video_http_v1 未找到可用 protocol catalog 文件",
        protocol_id=wanted,
        checked_files=[str(path) for path in paths],
    )


def list_video_http_v1_protocol_catalog() -> dict[str, Any]:
    paths = _video_http_v1_protocol_catalog_paths()
    if not paths:
        return {
            "ok": False,
            "catalog_file": None,
            "protocols": [],
            "total": 0,
            "error": "video_http_v1 未找到 protocol catalog 文件",
        }
    path = paths[0]
    protocols, error = _video_http_v1_load_protocol_catalog(path)
    if error:
        return {
            "ok": False,
            "catalog_file": str(path),
            "protocols": [],
            "total": 0,
            "error": error.get("error"),
        }
    assert protocols is not None
    items: list[dict[str, Any]] = []
    def duration_summary(source: dict[str, Any]) -> dict[str, Any]:
        has_duration_object = isinstance(source.get("duration"), dict)
        raw = source.get("duration") if has_duration_object else {}
        summary = {
            "min": raw.get("min") if has_duration_object else source.get("duration_min"),
            "max": raw.get("max") if has_duration_object else source.get("duration_max"),
            "allowed_values": raw.get("allowed_values")
                if has_duration_object
                else source.get("allowed_values") or source.get("allowed_durations") or source.get("supported_durations"),
            "step": raw.get("step") if has_duration_object else source.get("duration_step"),
        }
        return {key: value for key, value in summary.items() if value not in (None, "", [])}

    for protocol_id, protocol in sorted(protocols.items()):
        profiles = protocol.get("model_profiles") or protocol.get("models") or []
        model_names: list[str] = []
        model_profiles: list[dict[str, Any]] = []
        if isinstance(profiles, list):
            for profile in profiles:
                if isinstance(profile, dict):
                    model = str(profile.get("match") or profile.get("model") or "").strip()
                    if model:
                        model_names.append(model)
                    profile_summary = {
                        "match": model,
                        "label": str(profile.get("label") or "").strip(),
                        "supported_ratios": sorted(_string_set(
                            profile.get("supported_ratios")
                            or profile.get("ratios")
                            or profile.get("supported_aspect_ratios")
                        )),
                        "supported_resolutions": sorted(_string_set(
                            profile.get("supported_resolutions")
                            or profile.get("resolutions")
                        )),
                        "default_ratio": str(profile.get("default_ratio") or profile.get("ratio") or "").strip(),
                        "default_resolution": str(profile.get("default_resolution") or profile.get("resolution") or "").strip(),
                        "duration": duration_summary(profile),
                    }
                    if isinstance(profile.get("modes"), (dict, list)):
                        profile_summary["modes"] = profile.get("modes")
                    supported_modes = sorted(_string_set(profile.get("supported_modes")))
                    if supported_modes:
                        profile_summary["supported_modes"] = supported_modes
                    model_profiles.append(profile_summary)
        modes_raw = protocol.get("modes")
        modes: dict[str, Any] = {}
        if isinstance(modes_raw, dict):
            for mode_id, mode_config in sorted(modes_raw.items()):
                if not isinstance(mode_config, dict):
                    continue
                modes[str(mode_id)] = {
                    "label": str(mode_config.get("label") or mode_id),
                    "prompt_required": mode_config.get("prompt_required"),
                    "min_images": mode_config.get("min_images"),
                    "max_images": mode_config.get("max_images"),
                    "min_videos": mode_config.get("min_videos"),
                    "max_videos": mode_config.get("max_videos"),
                    "min_audios": mode_config.get("min_audios"),
                    "max_audios": mode_config.get("max_audios"),
                    "min_total_media": mode_config.get("min_total_media") or mode_config.get("min_media"),
                    "max_total_media": mode_config.get("max_total_media") or mode_config.get("max_media"),
                    "required_roles": sorted(_string_set(mode_config.get("required_roles"))),
                    "allowed_roles": sorted(_string_set(mode_config.get("allowed_roles"))),
                    "supported_ratios": sorted(_string_set(
                        mode_config.get("supported_ratios")
                        or mode_config.get("ratios")
                        or mode_config.get("supported_aspect_ratios")
                    )),
                    "supported_resolutions": sorted(_string_set(
                        mode_config.get("supported_resolutions")
                        or mode_config.get("resolutions")
                    )),
                    "default_ratio": str(mode_config.get("default_ratio") or mode_config.get("ratio") or "").strip(),
                    "default_resolution": str(mode_config.get("default_resolution") or mode_config.get("resolution") or "").strip(),
                    "duration": duration_summary(mode_config),
                }
        items.append({
            "id": protocol_id,
            "display_name": str(protocol.get("display_name") or protocol_id),
            "additional_base_urls": _video_http_v1_additional_base_urls(protocol),
            "model_names": model_names,
            "model_profiles": model_profiles,
            "modes": modes,
            "supported_ratios": sorted(_string_set(protocol.get("supported_ratios") or protocol.get("ratios"))),
            "supported_resolutions": sorted(_string_set(protocol.get("supported_resolutions") or protocol.get("resolutions"))),
            "default_ratio": str(protocol.get("default_ratio") or protocol.get("ratio") or "").strip(),
            "default_resolution": str(protocol.get("default_resolution") or protocol.get("resolution") or "").strip(),
            "duration": duration_summary(protocol),
        })
    return {
        "ok": True,
        "catalog_file": str(path),
        "protocols": items,
        "total": len(items),
    }


def _image_http_v1_protocol_catalog_paths() -> list[Path]:
    root = Path(settings.PROJECT_ROOT).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[4]
    env_value = os.environ.get("OPENREEL_IMAGE_PROTOCOLS_FILE", "").strip()
    candidates: list[Path] = [Path(env_value).expanduser()] if env_value else []
    candidates.extend([root / _IMAGE_HTTP_V1_CATALOG_FILE, repo_root / _IMAGE_HTTP_V1_CATALOG_FILE])
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        resolved = path.resolve()
        if resolved not in seen and resolved.exists() and resolved.is_file():
            seen.add(resolved)
            result.append(resolved)
    return result


def _image_http_v1_protocol_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "error_kind": "bad_config", **extra}


def _image_http_v1_load_protocol_catalog(
    path: Path,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, _image_http_v1_protocol_error(
            f"image_http_v1 protocol catalog 无法读取或不是合法 JSON: {path.name}",
            detail=str(exc),
        )
    if not isinstance(raw, dict):
        return None, _image_http_v1_protocol_error(f"image_http_v1 protocol catalog 必须是 JSON 对象: {path.name}")
    version = str(raw.get("version") or "").strip()
    if version != _IMAGE_HTTP_V1_CATALOG_VERSION:
        return None, _image_http_v1_protocol_error(
            f"image_http_v1 protocol catalog.version 必须是 {_IMAGE_HTTP_V1_CATALOG_VERSION}",
            catalog_file=path.name,
        )
    protocols = raw.get("protocols")
    if isinstance(protocols, list):
        mapped = {
            str(item.get("id") or "").strip(): item
            for item in protocols
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
    elif isinstance(protocols, dict):
        mapped = {str(key): value for key, value in protocols.items() if isinstance(value, dict)}
    else:
        mapped = {}
    if not mapped:
        return None, _image_http_v1_protocol_error(
            "image_http_v1 protocol catalog 缺少 protocols",
            catalog_file=path.name,
        )
    return mapped, None


def _image_http_v1_protocol_from_catalog(
    protocol_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    wanted = str(protocol_id or "").strip()
    if not wanted:
        return None, _image_http_v1_protocol_error("image_http_v1 provider 缺少 params.image_protocol_id")
    paths = _image_http_v1_protocol_catalog_paths()
    if not paths:
        return None, _image_http_v1_protocol_error(
            "image_http_v1 未找到 protocol catalog 文件",
            expected=str(_IMAGE_HTTP_V1_CATALOG_FILE),
        )
    for path in paths[:1]:
        protocols, error = _image_http_v1_load_protocol_catalog(path)
        if error:
            return None, error
        assert protocols is not None
        raw = protocols.get(wanted)
        if isinstance(raw, dict):
            version = str(raw.get("version") or raw.get("protocol") or "").strip()
            if version != _IMAGE_HTTP_V1_PROTOCOL_VERSION:
                return None, _image_http_v1_protocol_error(
                    f"image_http_v1 protocol.version 必须是 {_IMAGE_HTTP_V1_PROTOCOL_VERSION}",
                    protocol_id=wanted,
                    catalog_file=path.name,
                )
            raw_id = str(raw.get("id") or wanted).strip()
            if raw_id != wanted:
                return None, _image_http_v1_protocol_error(
                    f"image_http_v1 protocol id 不匹配: 期望 {wanted}, 文件内是 {raw_id}",
                    protocol_id=wanted,
                    catalog_file=path.name,
                )
            return raw, None
        return None, _image_http_v1_protocol_error(
            f"image_http_v1 未找到协议: {wanted}",
            protocol_id=wanted,
            catalog_file=path.name,
            available_protocols=sorted(protocols.keys()),
        )
    return None, _image_http_v1_protocol_error(
        "image_http_v1 未找到可用 protocol catalog 文件",
        protocol_id=wanted,
        checked_files=[str(path) for path in paths],
    )


def list_image_http_v1_protocol_catalog() -> dict[str, Any]:
    paths = _image_http_v1_protocol_catalog_paths()
    if not paths:
        return {
            "ok": False,
            "catalog_file": None,
            "protocols": [],
            "total": 0,
            "error": "image_http_v1 未找到 protocol catalog 文件",
        }
    path = paths[0]
    protocols, error = _image_http_v1_load_protocol_catalog(path)
    if error:
        return {
            "ok": False,
            "catalog_file": str(path),
            "protocols": [],
            "total": 0,
            "error": error.get("error"),
        }
    assert protocols is not None
    items: list[dict[str, Any]] = []
    for protocol_id, protocol in sorted(protocols.items()):
        profiles = protocol.get("model_profiles") or protocol.get("models") or []
        model_names: list[str] = []
        if isinstance(profiles, list):
            for profile in profiles:
                if isinstance(profile, dict):
                    model = str(profile.get("match") or profile.get("model") or "").strip()
                    if model:
                        model_names.append(model)
        items.append({
            "id": protocol_id,
            "display_name": str(protocol.get("display_name") or protocol_id),
            "model_names": model_names,
            "supported_sizes": sorted(_string_set(protocol.get("supported_sizes") or protocol.get("sizes"))),
        })
    return {
        "ok": True,
        "catalog_file": str(path),
        "protocols": items,
        "total": len(items),
    }


def _image_http_v1_protocol_id_for_provider(provider: MediaProvider, extra: dict[str, Any] | None = None) -> str:
    params = extra if isinstance(extra, dict) else _parse_extra(provider)
    explicit = str(params.get("image_protocol_id") or params.get("protocol_id") or "").strip()
    if explicit:
        return explicit
    fmt = _normalized_api_format(provider)
    return _IMAGE_HTTP_V1_LEGACY_PROTOCOLS_BY_FORMAT.get(fmt, "")


def _image_http_v1_protocol(
    provider: MediaProvider,
    extra_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    if "image_protocol" in extra or isinstance(extra.get("protocol"), dict):
        return None, _image_http_v1_protocol_error(
            "image_http_v1 provider 配置只保存 params.image_protocol_id；协议 JSON 放在 config/image_provider_protocols/catalog.json"
        )
    protocol_id = _image_http_v1_protocol_id_for_provider(provider, extra)
    return _image_http_v1_protocol_from_catalog(protocol_id)


def _audio_http_v1_protocol_catalog_paths() -> list[Path]:
    root = Path(settings.PROJECT_ROOT).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[4]
    env_value = os.environ.get("OPENREEL_AUDIO_PROTOCOLS_FILE", "").strip()
    candidates: list[Path] = [Path(env_value).expanduser()] if env_value else []
    candidates.extend([root / _AUDIO_HTTP_V1_CATALOG_FILE, repo_root / _AUDIO_HTTP_V1_CATALOG_FILE])
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        resolved = path.resolve()
        if resolved not in seen and resolved.exists() and resolved.is_file():
            seen.add(resolved)
            result.append(resolved)
    return result


def _audio_http_v1_protocol_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "error_kind": "bad_config", **extra}


def _audio_http_v1_load_protocol_catalog(
    path: Path,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, _audio_http_v1_protocol_error(
            f"audio_http_v1 protocol catalog 无法读取或不是合法 JSON: {path.name}",
            detail=str(exc),
        )
    if not isinstance(raw, dict):
        return None, _audio_http_v1_protocol_error(f"audio_http_v1 protocol catalog 必须是 JSON 对象: {path.name}")
    version = str(raw.get("version") or "").strip()
    if version != _AUDIO_HTTP_V1_CATALOG_VERSION:
        return None, _audio_http_v1_protocol_error(
            f"audio_http_v1 protocol catalog.version 必须是 {_AUDIO_HTTP_V1_CATALOG_VERSION}",
            catalog_file=path.name,
        )
    protocols = raw.get("protocols")
    if isinstance(protocols, list):
        mapped = {
            str(item.get("id") or "").strip(): item
            for item in protocols
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
    elif isinstance(protocols, dict):
        mapped = {str(key): value for key, value in protocols.items() if isinstance(value, dict)}
    else:
        mapped = {}
    if not mapped:
        return None, _audio_http_v1_protocol_error(
            "audio_http_v1 protocol catalog 缺少 protocols",
            catalog_file=path.name,
        )
    return mapped, None


def _audio_http_v1_protocol_from_catalog(
    protocol_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    wanted = str(protocol_id or "").strip()
    if not wanted:
        return None, _audio_http_v1_protocol_error("audio_http_v1 provider 缺少 params.audio_protocol_id")
    paths = _audio_http_v1_protocol_catalog_paths()
    if not paths:
        return None, _audio_http_v1_protocol_error(
            "audio_http_v1 未找到 protocol catalog 文件",
            expected=str(_AUDIO_HTTP_V1_CATALOG_FILE),
        )
    for path in paths[:1]:
        protocols, error = _audio_http_v1_load_protocol_catalog(path)
        if error:
            return None, error
        assert protocols is not None
        raw = protocols.get(wanted)
        if isinstance(raw, dict):
            version = str(raw.get("version") or raw.get("protocol") or "").strip()
            if version != _AUDIO_HTTP_V1_PROTOCOL_VERSION:
                return None, _audio_http_v1_protocol_error(
                    f"audio_http_v1 protocol.version 必须是 {_AUDIO_HTTP_V1_PROTOCOL_VERSION}",
                    protocol_id=wanted,
                    catalog_file=path.name,
                )
            raw_id = str(raw.get("id") or wanted).strip()
            if raw_id != wanted:
                return None, _audio_http_v1_protocol_error(
                    f"audio_http_v1 protocol id 不匹配: 期望 {wanted}, 文件内是 {raw_id}",
                    protocol_id=wanted,
                    catalog_file=path.name,
                )
            return raw, None
        return None, _audio_http_v1_protocol_error(
            f"audio_http_v1 未找到协议: {wanted}",
            protocol_id=wanted,
            catalog_file=path.name,
            available_protocols=sorted(protocols.keys()),
        )
    return None, _audio_http_v1_protocol_error(
        "audio_http_v1 未找到可用 protocol catalog 文件",
        protocol_id=wanted,
        checked_files=[str(path) for path in paths],
    )


def list_audio_http_v1_protocol_catalog() -> dict[str, Any]:
    paths = _audio_http_v1_protocol_catalog_paths()
    if not paths:
        return {
            "ok": False,
            "catalog_file": None,
            "protocols": [],
            "total": 0,
            "error": "audio_http_v1 未找到 protocol catalog 文件",
        }
    path = paths[0]
    protocols, error = _audio_http_v1_load_protocol_catalog(path)
    if error:
        return {
            "ok": False,
            "catalog_file": str(path),
            "protocols": [],
            "total": 0,
            "error": error.get("error"),
        }
    assert protocols is not None
    items: list[dict[str, Any]] = []
    for protocol_id, protocol in sorted(protocols.items()):
        profiles = protocol.get("model_profiles") or protocol.get("models") or []
        model_names: list[str] = []
        if isinstance(profiles, list):
            for profile in profiles:
                if isinstance(profile, dict):
                    model = str(profile.get("match") or profile.get("model") or "").strip()
                    if model:
                        model_names.append(model)
        result = protocol.get("result") if isinstance(protocol.get("result"), dict) else {}
        items.append({
            "id": protocol_id,
            "display_name": str(protocol.get("display_name") or protocol_id),
            "model_names": model_names,
            "result_type": str(result.get("type") or result.get("response_type") or ""),
        })
    return {
        "ok": True,
        "catalog_file": str(path),
        "protocols": items,
        "total": len(items),
    }


def _audio_http_v1_protocol_id_for_provider(provider: MediaProvider, extra: dict[str, Any] | None = None) -> str:
    params = _parse_extra(provider)
    if isinstance(extra, dict):
        params.update(extra)
    explicit = str(params.get("audio_protocol_id") or params.get("protocol_id") or "").strip()
    if explicit:
        return explicit
    fmt = _normalized_api_format(provider)
    return _AUDIO_HTTP_V1_LEGACY_PROTOCOLS_BY_FORMAT.get(fmt, "")


def _audio_http_v1_protocol(
    provider: MediaProvider,
    extra_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    if "audio_protocol" in extra or isinstance(extra.get("protocol"), dict):
        return None, _audio_http_v1_protocol_error(
            "audio_http_v1 provider 配置只保存 params.audio_protocol_id；协议 JSON 放在 config/audio_provider_protocols/catalog.json"
        )
    protocol_id = _audio_http_v1_protocol_id_for_provider(provider, extra)
    return _audio_http_v1_protocol_from_catalog(protocol_id)


def _video_http_v1_protocol(
    provider: MediaProvider,
    extra_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    if "video_protocol" in extra or isinstance(extra.get("protocol"), dict):
        return None, _video_http_v1_protocol_error(
            "video_http_v1 provider 配置只保存 params.video_protocol_id；协议 JSON 放在 config/video_provider_protocols/catalog.json"
        )
    protocol_id = _video_http_v1_protocol_id_for_provider(provider, extra)
    return _video_http_v1_protocol_from_catalog(protocol_id)


def _video_http_v1_protocol_id_for_provider(provider: MediaProvider, extra: dict[str, Any] | None = None) -> str:
    params = extra if isinstance(extra, dict) else _parse_extra(provider)
    explicit = str(params.get("video_protocol_id") or params.get("protocol_id") or "").strip()
    if explicit:
        return explicit
    fmt = _normalized_api_format(provider)
    mapped = _VIDEO_HTTP_V1_LEGACY_PROTOCOLS_BY_FORMAT.get(fmt)
    if mapped:
        return mapped
    model = str(getattr(provider, "model_name", "") or "").strip().lower().replace("_", "-")
    mapped = _VIDEO_HTTP_V1_PROTOCOLS_BY_MODEL.get(model)
    if mapped:
        return mapped
    if _is_seedance_model(getattr(provider, "model_name", "")):
        return "seedance_2_0"
    return ""


def _video_http_v1_request_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("request")
    return section if isinstance(section, dict) else {}


def _video_http_v1_poll_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("poll")
    return section if isinstance(section, dict) else {}


def _video_http_v1_result_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("result")
    return section if isinstance(section, dict) else {}


def _video_http_v1_upload_section(protocol: dict[str, Any], kind: str = "image") -> dict[str, Any]:
    section = protocol.get("upload")
    if isinstance(section, dict):
        nested = section.get(kind)
        if isinstance(nested, dict):
            return nested
        return section
    uploads = protocol.get("uploads")
    if isinstance(uploads, dict):
        nested = uploads.get(kind)
        if isinstance(nested, dict):
            return nested
    return {}


def _video_http_v1_base_for(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> str:
    base_url_param = str(section.get("base_url_param") or "").strip()
    if base_url_param:
        params = _parse_extra(provider)
        return str(params.get(base_url_param) or "").strip()
    return str(
        getattr(provider, "base_url", "")
        or protocol.get("base_url")
        or protocol.get("default_base_url")
        or ""
    ).strip()


def _video_http_v1_headers(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for source in (protocol.get("headers"), section.get("headers")):
        if isinstance(source, dict):
            for key, value in source.items():
                if value is not None:
                    headers[str(key)] = str(value)
    auth = str(section.get("auth") or protocol.get("auth") or "bearer").strip().lower()
    api_key = str(getattr(provider, "api_key", "") or "").strip()
    if api_key and auth in {"bearer", "authorization_bearer"}:
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key and auth in {"api_key_header", "header"}:
        header_name = str(section.get("api_key_header") or protocol.get("api_key_header") or "Authorization").strip()
        headers[header_name] = api_key
    elif api_key and auth in {"authorization_raw", "raw"}:
        headers["Authorization"] = api_key
    return headers


def _video_http_v1_endpoint_for(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
    *,
    task_id: str | None = None,
) -> str:
    base = _video_http_v1_base_for(provider, protocol, section)
    path = str(section.get("path") or section.get("endpoint") or "").strip()
    if task_id is not None:
        path = path.replace("{task_id}", task_id)
    if not base and not path.startswith(("http://", "https://")):
        return ""
    return _video_http_v1_join_url(base, path)


def _video_http_v1_additional_base_urls(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section_name in ("upload", "request", "poll"):
        section = protocol.get(section_name)
        if not isinstance(section, dict):
            continue
        param = str(section.get("base_url_param") or "").strip()
        if not param or param in seen:
            continue
        seen.add(param)
        result.append({
            "param": param,
            "label": str(section.get("base_url_label") or param),
            "hint": str(section.get("base_url_hint") or ""),
            "section": section_name,
            "required": True,
        })
    return result


def _video_http_v1_model_profile(protocol: dict[str, Any], model_name: str) -> dict[str, Any]:
    profiles = protocol.get("model_profiles") or protocol.get("models") or []
    if not isinstance(profiles, list):
        return {}
    model_key = model_name.strip().lower()
    for item in profiles:
        if not isinstance(item, dict):
            continue
        exact = str(item.get("match") or item.get("model") or "").strip().lower()
        if exact and exact == model_key:
            return item
        contains = str(item.get("match_contains") or "").strip().lower()
        if contains and contains in model_key:
            return item
        pattern = str(item.get("match_regex") or "").strip()
        if pattern:
            try:
                if re.search(pattern, model_name, re.I):
                    return item
            except re.error:
                continue
    return {}


def _video_http_v1_mode_config(protocol: dict[str, Any], mode: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    modes = protocol.get("modes")
    base_config: dict[str, Any] = {}
    if isinstance(modes, dict):
        config = modes.get(mode)
        if isinstance(config, dict):
            base_config = dict(config)
    profile = profile or {}
    profile_modes = profile.get("modes")
    if isinstance(profile_modes, list):
        allowed = {str(item) for item in profile_modes}
        return base_config if mode in allowed else {}
    if isinstance(profile_modes, dict):
        if mode not in profile_modes:
            return {}
        override = profile_modes.get(mode)
        return {**base_config, **override} if isinstance(override, dict) else base_config
    supported_modes = _string_set(profile.get("supported_modes"))
    if supported_modes and mode not in supported_modes:
        return {}
    return base_config


def _video_http_v1_pick(value: Any, fallback: Any = None) -> Any:
    return value if value not in (None, "") else fallback


def _video_http_v1_supported_resolutions(
    protocol: dict[str, Any],
    profile: dict[str, Any],
    mode_config: dict[str, Any],
) -> frozenset[str]:
    for source in (mode_config, profile, protocol):
        values = _string_set(source.get("supported_resolutions") or source.get("resolutions"))
        if values:
            return frozenset(item.lower() if re.match(r"^\d+p$", item, re.I) else item.lower() for item in values)
    return frozenset()


def _video_http_v1_resolution(value: Any, supported_resolutions: frozenset[str]) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    supported = {item.lower() for item in supported_resolutions}
    if supported and text in supported:
        return text
    if text in {"480", "720", "1080"}:
        candidate = f"{text}p"
        return candidate if not supported or candidate in supported else None
    if text in {"2160", "4k", "uhd"}:
        return "4k" if not supported or "4k" in supported else None
    if "x" not in text:
        return None
    left, right = text.split("x", 1)
    width = _coerce_int(left)
    height = _coerce_int(right)
    if not width or not height:
        return None
    short_side = min(width, height)
    candidates = (
        ("4k", 2000),
        ("1080p", 1000),
        ("720p", 700),
        ("480p", 0),
    )
    for candidate, threshold in candidates:
        if short_side >= threshold and (not supported or candidate in supported):
            return candidate
    return None


def _video_http_v1_supported_ratios(
    protocol: dict[str, Any],
    profile: dict[str, Any],
    mode_config: dict[str, Any],
) -> frozenset[str]:
    for source in (mode_config, profile, protocol):
        values = _string_set(source.get("supported_ratios") or source.get("ratios") or source.get("supported_aspect_ratios"))
        if values:
            return frozenset(values)
    return frozenset()


def _video_http_v1_ratio(value: Any, supported_ratios: frozenset[str]) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    text = _ARK_RATIO_ALIASES.get(text, text)
    if supported_ratios and text not in supported_ratios:
        return None
    return text


def _video_http_v1_duration_rules(
    protocol: dict[str, Any],
    profile: dict[str, Any],
    mode_config: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (protocol, profile, mode_config):
        rules = source.get("duration")
        if isinstance(rules, dict):
            merged.update(rules)
        for key in ("duration_min", "duration_max", "supported_durations", "allowed_durations", "allowed_values"):
            if key in source:
                merged[key] = source[key]
    return merged


def _video_http_v1_duration(
    value: Any,
    protocol: dict[str, Any],
    profile: dict[str, Any],
    mode_config: dict[str, Any],
) -> tuple[int | None, str | None]:
    duration = _coerce_int(value)
    if duration is None:
        return None, f"video_http_v1 duration 必须是整数，收到: {value!r}"
    rules = _video_http_v1_duration_rules(protocol, profile, mode_config)
    allowed = _int_set(rules.get("supported_durations") or rules.get("allowed_durations") or rules.get("allowed_values"))
    if duration in allowed:
        return duration, None
    minimum = _coerce_int(rules.get("min") or rules.get("duration_min"))
    maximum = _coerce_int(rules.get("max") or rules.get("duration_max"))
    # Product fallback, not a model capability claim: use the same editable
    # 5–15 second range as the node panel only when no duration rule exists.
    if minimum is None and maximum is None and not allowed:
        minimum, maximum = 5, 15
    if minimum is not None or maximum is not None:
        lower = minimum if minimum is not None else duration
        upper = maximum if maximum is not None else duration
        if lower <= duration <= upper:
            return duration, None
        allowed_text = f" 或 {', '.join(str(item) for item in sorted(allowed))}" if allowed else ""
        return None, f"video_http_v1 duration 只支持 {lower}-{upper} 秒{allowed_text}"
    if allowed:
        return None, f"video_http_v1 duration 只支持 {', '.join(str(item) for item in sorted(allowed))} 秒"
    if duration < 1:
        return None, "video_http_v1 duration 必须大于 0 秒"
    return duration, None


def _video_http_v1_ref_value(raw: Any) -> tuple[str, str, str]:
    if isinstance(raw, str):
        return "", "", raw.strip()
    if not isinstance(raw, dict):
        return "", "", ""
    kind = str(raw.get("kind") or raw.get("media_kind") or raw.get("media_type") or "").strip().lower()
    raw_type = str(raw.get("type") or "").strip().lower()
    if not kind and raw_type in {"image_url", "image"}:
        kind = "image"
    elif not kind and raw_type in {"video_url", "video"}:
        kind = "video"
    elif not kind and raw_type in {"audio_url", "audio"}:
        kind = "audio"
    role = str(raw.get("role") or raw.get("usage") or "").strip()
    for key in ("ref", "reference", "url", "local_url", "remote_url", "path", "value", "source"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return kind, role, value.strip()
    for key, prefix in (("node_id", "node:"), ("asset_id", "asset:")):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return kind, role, f"{prefix}{value.strip()}"
    return kind, role, ""


def _video_http_v1_collect_media_refs(
    first_frame_url: str | None,
    last_frame_url: str | None,
    reference_images: list[str] | None,
    extra: dict[str, Any],
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    if first_frame_url:
        refs.append({"kind": "image", "role": "first_frame", "ref": str(first_frame_url)})
    roles = extra.get("reference_image_roles")
    role_list = roles if isinstance(roles, list) else []
    for idx, ref in enumerate(reference_images or []):
        role = str(role_list[idx]).strip() if idx < len(role_list) and str(role_list[idx]).strip() else "reference_image"
        refs.append({"kind": "image", "role": role, "ref": str(ref)})
    if last_frame_url:
        refs.append({"kind": "image", "role": "last_frame", "ref": str(last_frame_url)})

    media_references = extra.get("media_references")
    if isinstance(media_references, list):
        for item in media_references:
            kind, role, ref = _video_http_v1_ref_value(item)
            if ref:
                refs.append({
                    "kind": kind if kind in _VIDEO_HTTP_MEDIA_KINDS else "image",
                    "role": role,
                    "ref": ref,
                })
    for key, kind, role in (
        ("reference_videos", "video", "reference_video"),
        ("reference_audios", "audio", "reference_audio"),
    ):
        values = extra.get(key)
        if isinstance(values, str):
            values = [item.strip() for item in values.splitlines() if item.strip()]
        if not isinstance(values, list):
            continue
        for item in values:
            _, item_role, ref = _video_http_v1_ref_value(item)
            if ref:
                refs.append({"kind": kind, "role": item_role or role, "ref": ref})
    return refs


def _video_http_v1_supported_mode_names(protocol: dict[str, Any], profile: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for source in (protocol, profile):
        modes = source.get("modes") if isinstance(source, dict) else None
        if isinstance(modes, dict):
            names.update(str(key) for key in modes.keys() if str(key).strip())
    return names


def _video_http_v1_infer_mode(
    extra: dict[str, Any],
    refs: list[dict[str, str]],
    protocol: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> str:
    explicit = str(extra.get("video_mode") or extra.get("mode") or extra.get("generation_mode") or "").strip()
    if explicit:
        return explicit
    roles = {item.get("role") for item in refs}
    non_first_last = [
        item for item in refs
        if item.get("role") not in {"first_frame", "last_frame"}
    ]
    supported_modes = _video_http_v1_supported_mode_names(protocol or {}, profile or {})
    image_refs = [item for item in refs if item.get("kind") == "image" or not item.get("kind")]
    if "first_frame" in roles and "last_frame" in roles and not non_first_last:
        return "first_last_frame"
    if "first_frame" in roles and not non_first_last:
        return "first_frame"
    if (
        refs
        and supported_modes
        and "multimodal_reference" not in supported_modes
        and all(item in image_refs for item in refs)
    ):
        if len(image_refs) == 2 and "first_last_frame" in supported_modes:
            return "first_last_frame"
        if len(image_refs) == 1 and "first_frame" in supported_modes:
            return "first_frame"
    if refs:
        return "multimodal_reference"
    return "text_to_video"


def _video_http_v1_normalize_roles_for_mode(mode: str, refs: list[dict[str, str]]) -> list[dict[str, str]]:
    if mode not in {"first_frame", "first_last_frame"}:
        return refs
    normalized = [dict(item) for item in refs]
    image_refs = [item for item in normalized if item.get("kind") == "image" or not item.get("kind")]
    if mode == "first_frame" and len(image_refs) == 1 and image_refs[0].get("role") in {"", "reference_image"}:
        image_refs[0]["role"] = "first_frame"
    if mode == "first_last_frame" and len(image_refs) == 2:
        for item, role in zip(image_refs, ("first_frame", "last_frame"), strict=False):
            if item.get("role") in {"", "reference_image"}:
                item["role"] = role
    return normalized


async def _resolve_non_image_media_ref(project_id: str, kind: str, raw_ref: str) -> tuple[str | None, str | None]:
    ref = str(raw_ref or "").strip()
    if not ref:
        return None, f"{kind} 引用为空"
    if ref.startswith("upload:"):
        rel = ref[len("upload:"):].strip().lstrip("/")
        return rel if rel.startswith("uploads/") else f"uploads/{rel}", None
    if ref.startswith(("http://", "https://", "/api/media/", "/api/uploads/", "generated_videos/", "generated_audio/", "uploads/")):
        return ref, None
    if ref.startswith("asset:"):
        asset_id = ref[len("asset:"):].strip()
        async with session_scope() as session:
            asset = await session.get(Asset, asset_id)
        if not asset:
            return None, f"找不到资产 asset:{asset_id}"
        picked = asset.url or asset.path
        return picked, None if picked else f"资产 {asset_id} 没有可用的 url 或 path"
    if ref.startswith("node:"):
        raw_node_id = ref[len("node:"):].strip()
        node_id, node_error = await _resolve_node_id_for_reference(project_id, raw_node_id)
        if node_error:
            return None, node_error
        node_id = node_id or raw_node_id
        async with session_scope() as session:
            rows = (await session.exec(select(Asset).where(Asset.node_id == node_id))).all()
        for asset in rows:
            meta: dict[str, Any] = {}
            if asset.metadata_json:
                try:
                    meta = json.loads(asset.metadata_json)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if meta.get("status") == "failed":
                continue
            if asset.url or asset.path:
                return asset.url or asset.path, None
        picked = await _pick_node_output_reference(project_id, node_id)
        if picked:
            return picked, None
        return None, f"节点 {raw_node_id} 没有可用的 {kind} 资产"
    candidate = Path(ref)
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (settings.storage_path_resolved / project_id / ref).resolve()
    if target.exists() and target.is_file():
        return str(target), None
    return None, f"{kind} 文件不存在: {ref}"


async def _video_http_v1_media_url(
    project_id: str,
    provider: MediaProvider,
    protocol: dict[str, Any],
    extra_override: dict[str, Any],
    kind: str,
    ref: str,
) -> tuple[str | None, str | None]:
    if kind == "image":
        image_transport = str(protocol.get("image_transport") or "data_url").strip().lower()
        if image_transport in {"upload_url", "upload_url_list", "uploaded_url"}:
            return await _video_http_v1_upload_image_ref(project_id, provider, protocol, ref)
        image_ref = ref
        if not str(image_ref or "").strip().startswith((
            "http://",
            "https://",
            "data:image/",
            "/api/media/",
            "/api/uploads/",
            "generated_images/",
            "uploads/",
        )):
            resolved_images, image_warnings = await _resolve_reference_images(project_id, [ref])
            if resolved_images:
                image_ref = resolved_images[0]
            elif image_warnings:
                return None, image_warnings[0]
        return await _image_url_or_data_url_for_ref(
            project_id,
            image_ref,
            provider,
            extra_override,
            default_transport="public_url" if image_transport in {"public_url", "url"} else "data_url",
        )
    resolved, warning = await _resolve_non_image_media_ref(project_id, kind, ref)
    if warning:
        return None, warning
    if not resolved:
        return None, f"{kind} 引用无法解析: {ref}"
    if _is_remote_url(resolved):
        return resolved, None
    if str(resolved).startswith("data:"):
        return resolved, None
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    return _public_media_url_for_ref(
        project_id,
        resolved,
        _first_text(extra.get("public_base_url"), extra.get("site_base_url")),
    )


async def _video_http_v1_upload_image_ref(
    project_id: str,
    provider: MediaProvider,
    protocol: dict[str, Any],
    ref: str,
) -> tuple[str | None, str | None]:
    upload = _video_http_v1_upload_section(protocol, "image")
    if not upload:
        return None, "video_http_v1 protocol 使用 upload_url 图片模式但缺少 upload 配置"
    resolved_refs, resolve_errors = await _resolve_reference_images(project_id, [ref])
    file_ref = resolved_refs[0] if resolved_refs else ref
    image_file, image_error = await _image_file_input(project_id, file_ref)
    if image_error or not image_file:
        return None, image_error or (resolve_errors[0] if resolve_errors else "图片引用无法读取")

    endpoint = _video_http_v1_endpoint_for(provider, protocol, upload)
    if not endpoint:
        return None, "video_http_v1 upload 缺少 path 或 base_url"
    headers = _video_http_v1_headers(provider, protocol, upload)
    headers.pop("Content-Type", None)
    method = str(upload.get("method") or "POST").strip().upper()
    file_field = str(upload.get("file_field") or upload.get("field") or "file").strip() or "file"
    filename, content, mime = image_file
    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            uploaded = await client.request(
                method,
                endpoint,
                files={file_field: (filename, content, mime)},
                headers=headers,
            )
        if uploaded.status_code >= 400:
            return None, f"上传参考图失败: HTTP {uploaded.status_code} - {uploaded.text[:300]}"
        upload_data, upload_error = _response_json(uploaded, endpoint)
        if upload_error:
            return None, upload_error.get("error") or "上传参考图响应无法解析"
    except httpx.HTTPError as exc:
        return None, f"上传参考图网络请求失败: {exc}"

    paths = upload.get("url_paths") or upload.get("result_url_paths") or ["url", "data.url"]
    if isinstance(paths, str):
        paths = [paths]
    tuple_paths = tuple(str(path) for path in paths) if isinstance(paths, list) else ("url", "data.url")
    url = _first_path_text(upload_data or {}, tuple_paths)
    if not url:
        return None, f"上传参考图响应缺少 url: {str(upload_data)[:300]}"
    return url, None


async def _video_http_v1_resolve_media_refs(
    project_id: str,
    provider: MediaProvider,
    protocol: dict[str, Any],
    extra_override: dict[str, Any],
    refs: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    out: list[dict[str, str]] = []
    warnings: list[str] = []
    for item in refs:
        kind = item.get("kind") if item.get("kind") in _VIDEO_HTTP_MEDIA_KINDS else "image"
        url, warning = await _video_http_v1_media_url(
            project_id,
            provider,
            protocol,
            extra_override,
            kind,
            item.get("ref") or "",
        )
        if warning:
            warnings.append(warning)
            continue
        if url:
            out.append({
                "kind": kind,
                "role": item.get("role") or f"reference_{kind}",
                "url": url,
                "ref": item.get("ref") or "",
            })
    return out, warnings


def _video_http_v1_validate_mode(
    *,
    mode: str,
    mode_config: dict[str, Any],
    protocol: dict[str, Any],
    prompt: str,
    refs: list[dict[str, str]],
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    forbidden_fields = []
    for source in (protocol, mode_config):
        raw = source.get("forbidden_fields")
        if isinstance(raw, list):
            forbidden_fields.extend(str(item) for item in raw)
    for field in forbidden_fields:
        if _has_value(extra.get(field)):
            return {
                "error": f"video_http_v1 mode={mode} 不支持字段 {field}",
                "error_kind": "bad_request",
                "mode": mode,
            }

    prompt_required = mode_config.get("prompt_required")
    if prompt_required is None:
        prompt_required = mode == "text_to_video"
    if _coerce_bool(prompt_required) is True and not prompt.strip():
        return {"error": f"video_http_v1 mode={mode} 需要 prompt", "error_kind": "bad_request", "mode": mode}

    counts = {kind: 0 for kind in _VIDEO_HTTP_MEDIA_KINDS}
    roles: set[str] = set()
    for item in refs:
        kind = item.get("kind") if item.get("kind") in _VIDEO_HTTP_MEDIA_KINDS else "image"
        counts[kind] += 1
        if item.get("role"):
            roles.add(str(item.get("role")))
    if _coerce_bool(mode_config.get("audio_requires_visual")) is True and counts["audio"] and not (counts["image"] or counts["video"]):
        return {"error": f"video_http_v1 mode={mode} 的音频参考必须搭配图片或视频参考", "error_kind": "bad_request", "mode": mode}
    for kind in _VIDEO_HTTP_MEDIA_KINDS:
        min_key = f"min_{kind}s"
        max_key = f"max_{kind}s"
        minimum = _coerce_int(mode_config.get(min_key), 0)
        maximum = _coerce_int(mode_config.get(max_key))
        if minimum and counts[kind] < minimum:
            return {"error": f"video_http_v1 mode={mode} 至少需要 {minimum} 个 {kind} 参考", "error_kind": "bad_request", "mode": mode}
        if maximum is not None and counts[kind] > maximum:
            return {"error": f"video_http_v1 mode={mode} 最多支持 {maximum} 个 {kind} 参考", "error_kind": "bad_request", "mode": mode}
    min_total = _coerce_int(mode_config.get("min_total_media") or mode_config.get("min_media"), 0)
    max_total = _coerce_int(mode_config.get("max_total_media") or mode_config.get("max_media"))
    total = sum(counts.values())
    if min_total and total < min_total:
        return {"error": f"video_http_v1 mode={mode} 至少需要 {min_total} 个媒体参考", "error_kind": "bad_request", "mode": mode}
    if max_total is not None and total > max_total:
        return {"error": f"video_http_v1 mode={mode} 最多支持 {max_total} 个媒体参考", "error_kind": "bad_request", "mode": mode}
    required_roles = _string_set(mode_config.get("required_roles"))
    missing = sorted(required_roles - roles)
    if missing:
        return {"error": f"video_http_v1 mode={mode} 缺少角色: {', '.join(missing)}", "error_kind": "bad_request", "mode": mode}
    allowed_roles = _string_set(mode_config.get("allowed_roles"))
    if allowed_roles:
        unsupported = sorted(role for role in roles if role not in allowed_roles)
        if unsupported:
            return {"error": f"video_http_v1 mode={mode} 不支持角色: {', '.join(unsupported)}", "error_kind": "bad_request", "mode": mode}
    return None


def _video_http_v1_truncate_refs_for_mode(
    mode: str,
    mode_config: dict[str, Any],
    refs: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    counts = {kind: 0 for kind in _VIDEO_HTTP_MEDIA_KINDS}
    total = 0
    max_total = _coerce_int(mode_config.get("max_total_media") or mode_config.get("max_media"))
    kept: list[dict[str, str]] = []
    dropped = 0
    for item in refs:
        kind = item.get("kind") if item.get("kind") in _VIDEO_HTTP_MEDIA_KINDS else "image"
        maximum = _coerce_int(mode_config.get(f"max_{kind}s"))
        if maximum is not None and counts[kind] >= maximum:
            dropped += 1
            continue
        if max_total is not None and total >= max_total:
            dropped += 1
            continue
        kept.append(item)
        counts[kind] += 1
        total += 1
    if dropped <= 0:
        return kept, []
    return kept, [f"video_http_v1 mode={mode} 参考媒体超过上限，已使用前 {len(kept)} 个，忽略 {dropped} 个。"]


def _video_http_v1_text_item(protocol: dict[str, Any], prompt: str) -> dict[str, Any] | None:
    if not prompt.strip():
        return None
    content = protocol.get("content")
    content_spec = content if isinstance(content, dict) else {}
    text_spec = content_spec.get("text") if isinstance(content_spec.get("text"), dict) else {}
    type_key = str(text_spec.get("type_key") or "type")
    text_key = str(text_spec.get("text_key") or "text")
    item_type = str(text_spec.get("type") or "text")
    return {type_key: item_type, text_key: prompt.strip()}


def _video_http_v1_media_item(protocol: dict[str, Any], ref: dict[str, str]) -> dict[str, Any]:
    content = protocol.get("content")
    content_spec = content if isinstance(content, dict) else {}
    media_types = content_spec.get("media_types") if isinstance(content_spec.get("media_types"), dict) else {}
    kind = ref.get("kind") if ref.get("kind") in _VIDEO_HTTP_MEDIA_KINDS else "image"
    spec = media_types.get(kind) if isinstance(media_types.get(kind), dict) else {}
    item_type = str(spec.get("type") or f"{kind}_url")
    type_key = str(spec.get("type_key") or "type")
    object_key = str(spec.get("object_key") or item_type)
    url_key = str(spec.get("url_key") or "url")
    role_key = str(spec.get("role_key") or "role")
    item: dict[str, Any] = {
        type_key: item_type,
        object_key: {url_key: ref.get("url")},
    }
    role = str(ref.get("role") or "").strip()
    if role and role_key:
        item[role_key] = role
    return item


def _video_http_v1_content(protocol: dict[str, Any], prompt: str, refs: list[dict[str, str]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    text_item = _video_http_v1_text_item(protocol, prompt)
    if text_item:
        content.append(text_item)
    content.extend(_video_http_v1_media_item(protocol, ref) for ref in refs)
    return content


def _video_http_v1_render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$") and re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*$", value):
        return context.get(value[1:])
    if isinstance(value, list):
        rendered = [_video_http_v1_render_value(item, context) for item in value]
        return [item for item in rendered if item is not None and item != "" and item != [] and item != {}]
    if isinstance(value, dict):
        rendered_dict: dict[str, Any] = {}
        for key, item in value.items():
            rendered = _video_http_v1_render_value(item, context)
            if rendered is None or rendered == "" or rendered == [] or rendered == {}:
                continue
            rendered_dict[str(key)] = rendered
        return rendered_dict
    return value


def _video_http_v1_output_resolution(
    resolution: str | None,
    protocol: dict[str, Any],
    profile: dict[str, Any],
    mode_config: dict[str, Any],
) -> str | None:
    if resolution is None:
        return None
    output = "lower"
    for source in (protocol, profile, mode_config):
        raw = str(source.get("resolution_output") or source.get("size_output") or "").strip().lower()
        if raw:
            output = raw
    if output == "upper":
        return resolution.upper()
    return resolution.lower() if re.match(r"^\d+p$", resolution, re.I) else resolution


def _video_http_v1_video_size(resolution: str | None, ratio: str | None) -> str | None:
    if not resolution:
        return None
    return _video_size_for_resolution(resolution, ratio)


def _video_http_v1_resolution_rule_error(
    duration: int | None,
    resolution: str | None,
    protocol: dict[str, Any],
    profile: dict[str, Any],
    mode_config: dict[str, Any],
) -> dict[str, Any] | None:
    if duration is None or not resolution:
        return None
    rules: list[Any] = []
    for source in (protocol, profile, mode_config):
        raw = source.get("resolution_rules")
        if isinstance(raw, list):
            rules.extend(raw)
    for item in rules:
        if not isinstance(item, dict):
            continue
        gt = _coerce_int(item.get("duration_gt"))
        gte = _coerce_int(item.get("duration_gte") or item.get("duration_min"))
        lt = _coerce_int(item.get("duration_lt"))
        lte = _coerce_int(item.get("duration_lte") or item.get("duration_max"))
        if gt is not None and duration <= gt:
            continue
        if gte is not None and duration < gte:
            continue
        if lt is not None and duration >= lt:
            continue
        if lte is not None and duration > lte:
            continue
        allowed = _string_set(item.get("supported_resolutions") or item.get("resolutions"))
        allowed_lower = {value.lower() for value in allowed}
        if allowed_lower and resolution.lower() not in allowed_lower:
            return _with_video_model_doc_hint({
                "error": str(
                    item.get("message")
                    or f"video_http_v1 duration={duration} 时只支持 resolution={', '.join(sorted(allowed_lower))}"
                ),
                "error_kind": "bad_request",
                "supported_resolutions": sorted(allowed_lower),
            })
    return None


async def _video_http_v1_multipart_files(
    protocol: dict[str, Any],
    project_id: str,
    raw_refs: list[dict[str, str]],
    request: dict[str, Any],
) -> tuple[dict[str, tuple[str, bytes, str]], dict[str, Any] | None]:
    files_spec = request.get("files")
    if not isinstance(files_spec, dict) or not files_spec:
        return {}, None
    image_refs = [item for item in raw_refs if item.get("kind") == "image" or not item.get("kind")]
    out: dict[str, tuple[str, bytes, str]] = {}
    for field, selector in files_spec.items():
        raw_selector = str(selector or "").strip()
        if raw_selector not in {"$first_image_file", "$image_file", "$source_image_file"}:
            return {}, {
                "error": f"video_http_v1 multipart files 不支持 selector={raw_selector!r}",
                "error_kind": "bad_config",
            }
        if not image_refs:
            return {}, {"error": "video_http_v1 multipart 请求缺少图片引用", "error_kind": "bad_request"}
        raw_ref = image_refs[0].get("ref") or ""
        resolved_refs, resolve_errors = await _resolve_reference_images(project_id, [raw_ref])
        file_ref = resolved_refs[0] if resolved_refs else raw_ref
        image_file, image_error = await _image_file_input(project_id, file_ref)
        if image_error or not image_file:
            return {}, _with_video_model_doc_hint({
                "error": image_error or (resolve_errors[0] if resolve_errors else "源图无法读取"),
                "error_kind": "bad_request",
            })
        out[str(field)] = image_file
    return out, None


async def _build_video_http_v1_payload(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    protocol, protocol_error = _video_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return None, protocol_error
    assert protocol is not None
    extra = _parse_extra(provider)
    extra.update(extra_override or {})

    model_name = str(extra.get("model") or getattr(provider, "model_name", "") or "").strip()
    if not model_name:
        return None, {"error": "video_http_v1 provider 缺少 model_name", "error_kind": "bad_config"}
    profile = _video_http_v1_model_profile(protocol, model_name)

    raw_refs = _video_http_v1_collect_media_refs(first_frame_url, last_frame_url, reference_images, extra)
    mode = _video_http_v1_infer_mode(extra, raw_refs, protocol=protocol, profile=profile)
    raw_refs = _video_http_v1_normalize_roles_for_mode(mode, raw_refs)
    mode_config = _video_http_v1_mode_config(protocol, mode, profile)
    if not mode_config and isinstance(protocol.get("modes"), dict):
        return None, {"error": f"video_http_v1 protocol 不支持 mode={mode}", "error_kind": "bad_request", "mode": mode}
    explicit_mode = str(extra.get("video_mode") or extra.get("mode") or extra.get("generation_mode") or "").strip()
    if explicit_mode and mode == "text_to_video" and raw_refs:
        return None, {
            "error": "video_http_v1 文生视频模式不接受参考图片或其他媒体；请移除参考媒体，或切换为图生视频/多模态参考模式",
            "error_kind": "bad_request",
            "error_code": "video_mode_reference_conflict",
            "mode": mode,
            "reference_count": len(raw_refs),
        }
    raw_refs, limit_warnings = _video_http_v1_truncate_refs_for_mode(mode, mode_config, raw_refs)

    duration, duration_error = _video_http_v1_duration(duration_seconds, protocol, profile, mode_config)
    if duration_error:
        return None, {"error": duration_error, "error_kind": "bad_request", "mode": mode}

    ratio = None
    raw_ratio = _video_http_v1_pick(extra.get("ratio"), extra.get("aspect_ratio"))
    if _has_value(raw_ratio):
        supported_ratios = _video_http_v1_supported_ratios(protocol, profile, mode_config)
        ratio = _video_http_v1_ratio(raw_ratio, supported_ratios)
        if not ratio:
            return None, {
                "error": f"video_http_v1 mode={mode} 不支持 aspect_ratio={raw_ratio!r}",
                "error_kind": "bad_request",
                "supported_ratios": sorted(supported_ratios),
                "mode": mode,
            }

    resolution = None
    raw_resolution = _video_http_v1_pick(
        extra.get("resolution"),
        mode_config.get("default_resolution") or profile.get("default_resolution") or protocol.get("default_resolution"),
    )
    if _has_value(raw_resolution):
        supported_resolutions = _video_http_v1_supported_resolutions(protocol, profile, mode_config)
        resolution = _video_http_v1_resolution(raw_resolution, supported_resolutions)
        if not resolution:
            return None, _unsupported_video_resolution_error(
                str(protocol.get("display_name") or "video_http_v1"),
                model_name,
                raw_resolution,
                set(supported_resolutions),
            )

    resolution_rule_error = _video_http_v1_resolution_rule_error(duration, resolution, protocol, profile, mode_config)
    if resolution_rule_error:
        return None, resolution_rule_error

    resolved_refs, reference_warnings = await _video_http_v1_resolve_media_refs(
        project_id,
        provider,
        protocol,
        extra,
        raw_refs,
    )
    reference_warnings = [*limit_warnings, *reference_warnings]
    validation_error = _video_http_v1_validate_mode(
        mode=mode,
        mode_config=mode_config,
        protocol=protocol,
        prompt=str(prompt or ""),
        refs=resolved_refs,
        extra=extra,
    )
    if validation_error:
        return None, validation_error

    content = _video_http_v1_content(protocol, str(prompt or ""), resolved_refs)
    if not content:
        return None, {"error": "video_http_v1 请求缺少 prompt 或媒体参考", "error_kind": "bad_request", "mode": mode}
    image_urls = [ref.get("url") for ref in resolved_refs if ref.get("kind") == "image" and ref.get("url")]
    video_urls = [ref.get("url") for ref in resolved_refs if ref.get("kind") == "video" and ref.get("url")]
    audio_urls = [ref.get("url") for ref in resolved_refs if ref.get("kind") == "audio" and ref.get("url")]
    first_frame_image_urls = image_urls if mode == "first_frame" else []
    reference_image_urls = image_urls if mode == "multimodal_reference" else []
    output_resolution = _video_http_v1_output_resolution(resolution, protocol, profile, mode_config)

    context = {
        "model": model_name,
        "prompt": str(prompt or "").strip(),
        "content": content,
        "media_references": resolved_refs,
        "image_urls": image_urls,
        "image_url_objects": [{"url": url} for url in image_urls],
        "first_frame_image_url": first_frame_image_urls[0] if first_frame_image_urls else None,
        "reference_image_urls": reference_image_urls,
        "reference_image_objects": [{"url": url} for url in reference_image_urls],
        "video_urls": video_urls,
        "audio_urls": audio_urls,
        "duration_seconds": duration,
        "duration": duration,
        "aspect_ratio": ratio,
        "ratio": ratio,
        "resolution": output_resolution,
        "raw_resolution": resolution,
        "video_size": _video_http_v1_video_size(resolution, ratio),
        "first_image_url": image_urls[0] if image_urls else None,
        "first_video_url": video_urls[0] if video_urls else None,
        "first_audio_url": audio_urls[0] if audio_urls else None,
        "mode": mode,
        "generate_audio": _coerce_bool(extra.get("generate_audio")),
        "watermark": _coerce_bool(extra.get("watermark")),
        "return_last_frame": _coerce_bool(extra.get("return_last_frame")),
        "priority": _coerce_int(extra.get("priority")),
        "execution_expires_after": _coerce_int(extra.get("execution_expires_after")),
        "safety_identifier": str(extra.get("safety_identifier")).strip() if _has_value(extra.get("safety_identifier")) else None,
        "seed": _coerce_int(extra.get("seed")),
        "tools": extra.get("tools") if isinstance(extra.get("tools"), list) else None,
    }
    request = _video_http_v1_request_section(protocol)
    request_encoding = str(
        request.get("encoding") or request.get("body_type") or request.get("content_type") or "json"
    ).strip().lower()
    multipart = request_encoding in {"multipart", "multipart/form-data", "form_data", "form-data"}
    configured_body = request.get("form") if multipart and isinstance(request.get("form"), dict) else request.get("body")
    body_template = configured_body if isinstance(configured_body, dict) else {
        "model": "$model",
        "content": "$content",
        "duration": "$duration_seconds",
        "ratio": "$aspect_ratio",
        "resolution": "$resolution",
        "generate_audio": "$generate_audio",
        "watermark": "$watermark",
        "return_last_frame": "$return_last_frame",
        "priority": "$priority",
        "execution_expires_after": "$execution_expires_after",
        "safety_identifier": "$safety_identifier",
        "tools": "$tools",
    }
    payload = _video_http_v1_render_value(body_template, context)
    if not isinstance(payload, dict):
        return None, {"error": "video_http_v1 request.body 渲染结果不是对象", "error_kind": "bad_config"}
    multipart_files: dict[str, tuple[str, bytes, str]] = {}
    if multipart:
        multipart_files, multipart_error = await _video_http_v1_multipart_files(protocol, project_id, raw_refs, request)
        if multipart_error:
            return None, multipart_error
    meta: dict[str, Any] = {
        "mode": mode,
        "resolved_media_references": resolved_refs,
        "reference_warnings": reference_warnings,
        "request": {
            "content_count": len(content),
            "duration": duration,
            "ratio": ratio,
            "resolution": output_resolution,
            "mode": mode,
            "encoding": "multipart" if multipart else "json",
        },
    }
    if multipart:
        meta["_multipart_files"] = multipart_files
    return payload, meta


def _video_http_v1_status_sets(protocol: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    poll = _video_http_v1_poll_section(protocol)
    succeeded = _string_set(poll.get("succeeded") or poll.get("done_statuses")) or {"succeeded", "success", "completed", "complete", "done"}
    failed = _string_set(poll.get("failed") or poll.get("failed_statuses")) or {"failed", "failure", "error", "cancelled", "canceled", "expired"}
    running = _string_set(poll.get("running") or poll.get("running_statuses")) or {"queued", "pending", "running", "processing", "in_progress", "submitted", "created"}
    return {item.lower() for item in succeeded}, {item.lower() for item in failed}, {item.lower() for item in running}


def _video_http_v1_poll_settings(
    provider: MediaProvider,
    protocol: dict[str, Any],
    extra_override: dict[str, Any] | None,
) -> tuple[float, float]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    poll = _video_http_v1_poll_section(protocol)
    poll_interval = max(
        1.0,
        _coerce_float(
            extra.get("_poll_interval_seconds")
            or poll.get("interval_seconds")
            or os.getenv("DRAMA_VIDEO_POLL_INTERVAL_SECONDS")
            or 10,
            10.0,
        ),
    )
    poll_timeout = max(
        poll_interval,
        _coerce_float(
            extra.get("_poll_timeout_seconds")
            or poll.get("timeout_seconds")
            or os.getenv("DRAMA_VIDEO_POLL_TIMEOUT_SECONDS")
            or 1200,
            1200.0,
        ),
    )
    return poll_interval, poll_timeout


def _video_http_v1_task_id(protocol: dict[str, Any], data: dict[str, Any]) -> str | None:
    request = _video_http_v1_request_section(protocol)
    paths = request.get("task_id_paths") or request.get("id_paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = ["id", "task_id", "taskId", "job_id", "data.id", "data.task_id", "data.taskId", "data.job_id"]
    return _first_path_text(data, tuple(str(path) for path in paths))


def _video_http_v1_status(protocol: dict[str, Any], data: dict[str, Any], fallback: str = "queued") -> str:
    poll = _video_http_v1_poll_section(protocol)
    status_path = str(poll.get("status_path") or "status")
    return str(_lookup_path(data, status_path) or data.get("status") or data.get("state") or fallback).strip().lower()


def _video_http_v1_progress(protocol: dict[str, Any], data: dict[str, Any]) -> Any:
    poll = _video_http_v1_poll_section(protocol)
    path = str(poll.get("progress_path") or "").strip()
    if path:
        return _lookup_path(data, path)
    return data.get("progress")


def _video_http_v1_result_url(protocol: dict[str, Any], data: dict[str, Any]) -> str | None:
    result = _video_http_v1_result_section(protocol)
    path = str(result.get("video_url_path") or result.get("url_path") or "").strip()
    if path:
        value = _lookup_path(data, path)
        text = str(value or "").strip()
        return text or None
    paths = result.get("video_url_paths") or result.get("url_paths") or result.get("result_url_paths")
    if isinstance(paths, str):
        paths = [paths]
    if isinstance(paths, list):
        found = _first_path_text(data, tuple(str(path) for path in paths))
        if found:
            return found
    return _video_url_from_response(data)


def _video_http_v1_last_frame_url(protocol: dict[str, Any], data: dict[str, Any]) -> str | None:
    result = _video_http_v1_result_section(protocol)
    paths = result.get("last_frame_url_paths")
    if isinstance(paths, str):
        paths = [paths]
    if isinstance(paths, list):
        return _first_path_text(data, tuple(str(path) for path in paths))
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    return _first_text(content.get("last_frame_url"), data.get("last_frame_url"))


def _video_http_v1_provider_error(
    protocol: dict[str, Any],
    data: dict[str, Any],
) -> tuple[str, str | None]:
    error_config = protocol.get("error")
    if not isinstance(error_config, dict):
        error_config = {}
    poll_config = _video_http_v1_poll_section(protocol)
    message_path = str(
        error_config.get("message_path")
        or poll_config.get("error_message_path")
        or "error"
    ).strip()
    code_path = str(
        error_config.get("code_path")
        or poll_config.get("error_code_path")
        or "error_code"
    ).strip()
    value = _lookup_path(data, message_path)
    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or value.get("detail")
    message = str(value or "").strip()
    code = str(_lookup_path(data, code_path) or "").strip() or None
    return message or "视频生成任务失败", code


async def _video_http_v1_completed_result(
    *,
    provider: MediaProvider,
    protocol: dict[str, Any],
    project_id: str,
    task_id: str,
    status: str,
    endpoint: str,
    data: dict[str, Any],
    polls: list[dict[str, Any]],
    save_locally: bool,
) -> dict[str, Any]:
    remote_url = _video_http_v1_result_url(protocol, data)
    if not remote_url:
        return {
            "error": "video_http_v1 任务成功但响应缺少 video url",
            "error_kind": "bad_response",
            "job_id": task_id,
            "status": status,
            "endpoint": endpoint,
            "raw": data,
            "polls": polls,
        }
    downloaded: dict[str, Any] = {}
    if save_locally:
        downloaded = await _download_video_result(project_id, str(remote_url))
    return {
        "ok": True,
        "provider": provider.name,
        "model": provider.model_name,
        "status": "completed",
        "job_id": task_id,
        "url": downloaded.get("local_url") or remote_url,
        "local_url": downloaded.get("local_url"),
        "local_path": downloaded.get("local_path"),
        "remote_url": remote_url,
        "last_frame_url": _video_http_v1_last_frame_url(protocol, data),
        "duration": _lookup_path(data, "duration"),
        "ratio": _lookup_path(data, "ratio"),
        "resolution": _lookup_path(data, "resolution"),
        "usage": data.get("usage"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "polls": polls,
        "download_error": downloaded.get("download_error"),
    }


async def _call_video_http_v1(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if not getattr(provider, "api_key", None):
        return {"error": "video_http_v1 provider 缺少 API Key", "error_kind": "bad_config"}
    protocol, protocol_error = _video_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return protocol_error
    assert protocol is not None
    payload, payload_meta = await _build_video_http_v1_payload(
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=reference_images,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_meta or {"error": "无法构造 video_http_v1 请求", "error_kind": "bad_request"}

    request = _video_http_v1_request_section(protocol)
    endpoint = _video_http_v1_endpoint_for(provider, protocol, request)
    if not endpoint:
        return {"error": "video_http_v1 provider 缺少 base_url 或 request.path", "error_kind": "bad_config"}
    headers = _video_http_v1_headers(provider, protocol, request)
    method = str(request.get("method") or "POST").strip().upper()
    request_meta = (payload_meta or {}).get("request")
    request_encoding = str(request_meta.get("encoding") if isinstance(request_meta, dict) else "json").lower()

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            if method == "GET":
                created = await client.get(endpoint, params=payload, headers=headers)
            elif request_encoding == "multipart":
                multipart_files = (payload_meta or {}).get("_multipart_files")
                if not isinstance(multipart_files, dict) or not multipart_files:
                    return {"error": "video_http_v1 multipart 请求缺少文件", "error_kind": "bad_request", "endpoint": endpoint}
                headers.pop("Content-Type", None)
                created = await client.request(method, endpoint, data=payload, files=multipart_files, headers=headers)
            else:
                created = await client.request(method, endpoint, json=payload, headers=headers)
            if created.status_code >= 400:
                err = _make_http_error(created.status_code, created.text, endpoint)
                if err.get("error_kind") == "bad_request":
                    _with_video_model_doc_hint(err)
                return err
            create_data, create_error = _response_json(created, endpoint)
            if create_error:
                return create_error
    except httpx.HTTPError as exc:
        return {"error": f"网络请求失败: {exc}", "error_kind": "network", "endpoint": endpoint}

    task_id = _video_http_v1_task_id(protocol, create_data or {})
    if not task_id:
        return {
            "error": "创建 video_http_v1 任务响应缺少 task id",
            "error_kind": "bad_response",
            "endpoint": endpoint,
            "raw": create_data,
        }
    status = _video_http_v1_status(protocol, create_data or {})
    succeeded, failed, _running = _video_http_v1_status_sets(protocol)
    poll_section = _video_http_v1_poll_section(protocol)
    query_endpoint = _video_http_v1_endpoint_for(provider, protocol, poll_section, task_id=task_id)
    queued_result = {
        "ok": True,
        "provider": provider.name,
        "model": provider.model_name,
        "status": "running" if status == "running" else "queued",
        "job_id": task_id,
        "endpoint": endpoint,
        "query_endpoint": query_endpoint,
        "created_at": (create_data or {}).get("created_at"),
        "updated_at": (create_data or {}).get("updated_at"),
        "mode": (payload_meta or {}).get("mode"),
        "reference_warnings": (payload_meta or {}).get("reference_warnings") or [],
        "resolved_media_references": (payload_meta or {}).get("resolved_media_references") or [],
        "request": (payload_meta or {}).get("request") or {},
    }
    if status in succeeded:
        completed = await _video_http_v1_completed_result(
            provider=provider,
            protocol=protocol,
            project_id=project_id,
            task_id=task_id,
            status=status,
            endpoint=endpoint,
            data=create_data or {},
            polls=[],
            save_locally=save_locally,
        )
        completed.setdefault("mode", (payload_meta or {}).get("mode"))
        completed.setdefault("reference_warnings", (payload_meta or {}).get("reference_warnings") or [])
        completed.setdefault("resolved_media_references", (payload_meta or {}).get("resolved_media_references") or [])
        return completed
    if status in failed:
        provider_msg, provider_error_code = _video_http_v1_provider_error(protocol, create_data or {})
        return {
            "error": provider_msg,
            "error_kind": "provider_failed",
            "provider": provider.name,
            "model": provider.model_name,
            "job_id": task_id,
            "status": status,
            "endpoint": endpoint,
            "provider_msg": provider_msg,
            "provider_error_code": provider_error_code,
            "raw": create_data,
        }
    if not wait_for_completion:
        return queued_result
    final_result = await _poll_video_http_v1_task(
        provider=provider,
        project_id=project_id,
        task_id=task_id,
        extra_override=extra_override,
        save_locally=save_locally,
    )
    if payload_meta and payload_meta.get("reference_warnings"):
        final_result["reference_warnings"] = [
            *payload_meta.get("reference_warnings", []),
            *(
                final_result.get("reference_warnings")
                if isinstance(final_result.get("reference_warnings"), list)
                else []
            ),
        ]
    return final_result


async def _poll_video_http_v1_task(
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    if not getattr(provider, "api_key", None):
        return {"error": "video_http_v1 provider 缺少 API Key", "error_kind": "bad_config"}
    protocol, protocol_error = _video_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return {**protocol_error, "job_id": task_id, "status": "failed"}
    assert protocol is not None
    poll = _video_http_v1_poll_section(protocol)
    query_endpoint = _video_http_v1_endpoint_for(provider, protocol, poll, task_id=task_id)
    if not query_endpoint:
        return {"error": "video_http_v1 protocol 缺少 poll.path", "error_kind": "bad_config", "job_id": task_id}
    headers = _video_http_v1_headers(provider, protocol, poll)
    method = str(poll.get("method") or "GET").strip().upper()
    poll_interval, poll_timeout = _video_http_v1_poll_settings(provider, protocol, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    status = "queued"
    succeeded, failed, _running = _video_http_v1_status_sets(protocol)
    latest_data: dict[str, Any] = {}
    last_poll_error: dict[str, Any] | None = None
    consecutive_poll_errors = 0
    max_retry_interval = max(
        poll_interval,
        _coerce_float(poll.get("max_retry_interval_seconds") or 60, 60.0),
    )

    async def retry_transient_poll_error(error: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal consecutive_poll_errors, last_poll_error
        consecutive_poll_errors += 1
        last_poll_error = dict(error)
        retry_in_seconds = min(
            max_retry_interval,
            poll_interval * (2 ** min(consecutive_poll_errors - 1, 6)),
        )
        poll_record = {
            "status": status or "unknown",
            "retrying": True,
            "error_kind": error.get("error_kind"),
            "http_code": error.get("http_code"),
            "error": error.get("error"),
            "retry_count": consecutive_poll_errors,
            "retry_in_seconds": retry_in_seconds,
        }
        polls.append({key: value for key, value in poll_record.items() if value is not None})
        await _notify_progress(progress_callback, {
            "job_id": task_id,
            "status": status or "unknown",
            "progress": _video_http_v1_progress(protocol, latest_data),
            "poll_count": len(polls),
            "provider": provider.name,
            "model": provider.model_name,
            "endpoint": query_endpoint,
            "retrying": True,
            "error_kind": error.get("error_kind"),
            "http_code": error.get("http_code"),
            "error": error.get("error"),
            "retry_count": consecutive_poll_errors,
            "retry_in_seconds": retry_in_seconds,
        })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "error": f"视频任务轮询持续失败，已超过本地轮询超时 {int(poll_timeout)} 秒",
                "error_kind": "timeout",
                "provider": provider.name,
                "model": provider.model_name,
                "job_id": task_id,
                "status": status or "unknown",
                "endpoint": query_endpoint,
                "raw": latest_data,
                "polls": polls,
                "last_poll_error": last_poll_error,
            }
        await asyncio.sleep(min(retry_in_seconds, remaining))
        return None

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            while True:
                try:
                    if method == "POST":
                        queried = await client.post(query_endpoint, json={}, headers=headers)
                    else:
                        queried = await client.get(query_endpoint, headers=headers)
                except httpx.HTTPError as exc:
                    terminal = await retry_transient_poll_error({
                        "error": f"视频任务轮询网络异常: {exc}",
                        "error_kind": "network",
                        "endpoint": query_endpoint,
                    })
                    if terminal:
                        return terminal
                    continue
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, query_endpoint)
                    err.update({"job_id": task_id, "status": status or "unknown"})
                    if queried.status_code in {408, 425, 429, 500, 502, 503, 504}:
                        terminal = await retry_transient_poll_error(err)
                        if terminal:
                            return terminal
                        continue
                    return err
                query_data, query_error = _response_json(queried, query_endpoint)
                if query_error:
                    query_error.update({"job_id": task_id, "status": status or "unknown"})
                    terminal = await retry_transient_poll_error(query_error)
                    if terminal:
                        return terminal
                    continue

                latest_data = query_data or {}
                last_poll_error = None
                consecutive_poll_errors = 0
                status = _video_http_v1_status(protocol, latest_data, status)
                progress = _video_http_v1_progress(protocol, latest_data)
                polls.append({"status": status, "progress": progress, "updated_at": latest_data.get("updated_at")})
                await _notify_progress(progress_callback, {
                    "job_id": task_id,
                    "status": status,
                    "progress": progress,
                    "poll_count": len(polls),
                    "provider": provider.name,
                    "model": provider.model_name,
                    "endpoint": query_endpoint,
                    "updated_at": latest_data.get("updated_at"),
                })

                if status in succeeded:
                    return await _video_http_v1_completed_result(
                        provider=provider,
                        protocol=protocol,
                        project_id=project_id,
                        task_id=task_id,
                        status=status,
                        endpoint=query_endpoint,
                        data=latest_data,
                        polls=polls,
                        save_locally=save_locally,
                    )
                if status in failed:
                    provider_msg, provider_error_code = _video_http_v1_provider_error(protocol, latest_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "provider_error_code": provider_error_code,
                        "raw": latest_data,
                        "polls": polls,
                    }
                if time.monotonic() >= deadline:
                    return {
                        "error": f"视频任务仍在 {status}，已超过本地轮询超时 {int(poll_timeout)} 秒",
                        "error_kind": "timeout",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "raw": latest_data,
                        "polls": polls,
                    }
                await asyncio.sleep(poll_interval)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": query_endpoint,
            "job_id": task_id,
        }


async def _xai_image_input(
    project_id: str,
    ref: str | None,
    provider: MediaProvider,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, str] | None, str | None]:
    url, warning = await _image_url_or_data_url_for_ref(
        project_id,
        str(ref or ""),
        provider,
        extra_override,
        default_transport="data_url",
    )
    if warning or not url:
        return None, warning or "图片引用为空"
    return {"url": url}, None


async def _image_file_input(project_id: str, ref: str | None) -> tuple[tuple[str, bytes, str] | None, str | None]:
    text = str(ref or "").strip()
    if not text:
        return None, "图片引用为空"
    if text.startswith("data:image/") and "," in text:
        header, payload = text.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip() or "image/png"
        try:
            content = base64.b64decode(payload)
        except Exception as exc:
            return None, f"图片 data URL 无法解码: {exc}"
        suffix = mimetypes.guess_extension(mime) or ".png"
        return (f"reference{suffix}", content, mime), None
    local_ref = _project_media_path_from_url(project_id, text) or text
    try:
        if _is_remote_url(local_ref):
            async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
                resp = await client.get(local_ref)
            if resp.status_code != 200:
                return None, f"图片下载失败: HTTP {resp.status_code}"
            content = resp.content
            mime = resp.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
            suffix = ".jpg" if mime == "image/jpeg" else ".png"
            return (f"reference{suffix}", content, mime), None
        path = Path(local_ref)
        if not path.exists():
            return None, f"图片引用无法读取: {text}"
        content = path.read_bytes()
        ext = path.suffix.lower()
        mime = "image/png"
        if ext in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif ext == ".webp":
            mime = "image/webp"
        elif ext == ".gif":
            mime = "image/gif"
        return (path.name or "reference.png", content, mime), None
    except Exception as exc:
        return None, f"图片引用无法读取: {exc}"


def _video_size_for_resolution(resolution: Any, aspect_ratio: Any) -> str:
    raw = str(resolution or "").strip().lower()
    if re.match(r"^\d{3,5}x\d{3,5}$", raw):
        return raw
    aspect = str(aspect_ratio or "16:9").strip()
    portrait = aspect == "9:16"
    table = {
        "480p": "480x854" if portrait else "854x480",
        "720p": "720x1280" if portrait else "1280x720",
        "1080p": "1080x1920" if portrait else "1920x1080",
    }
    return table.get(raw or "720p", table["720p"])


def _video_url_from_response(data: dict[str, Any]) -> str | None:
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    response = data.get("response") if isinstance(data.get("response"), dict) else {}
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    candidates = [
        video.get("url"),
        response.get("video_url"),
        output.get("video_url"),
        output.get("url"),
        data.get("output") if isinstance(data.get("output"), str) else None,
        data.get("video_url"),
        data.get("url"),
    ]
    data_list = data.get("data")
    if isinstance(data_list, dict):
        nested_url = _video_url_from_response(data_list)
        if nested_url:
            return nested_url
        candidates.extend([
            data_list.get("output") if isinstance(data_list.get("output"), str) else None,
            data_list.get("video_url"),
            data_list.get("url"),
        ])
    if isinstance(data_list, list):
        for item in data_list:
            obj = item if isinstance(item, dict) else {}
            candidates.extend([obj.get("video_url"), obj.get("url")])
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return None


async def _build_xai_video_payload(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})

    model_name = str(extra.pop("model", None) or provider.model_name or "").strip()
    if not model_name:
        return None, {"error": "xAI video provider 缺少 model_name", "error_kind": "bad_config"}

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        return None, {"error": "xAI video 请求缺少 prompt", "error_kind": "bad_request"}

    duration = _coerce_int(extra.pop("duration", duration_seconds))
    if duration is None or duration < 1:
        return None, {
            "error": f"xAI video duration 必须是大于 0 的整数，收到: {duration_seconds!r}",
            "error_kind": "bad_request",
        }

    image_candidates: list[tuple[str, str]] = []
    if first_frame_url:
        image_candidates.append(("first_frame_url", first_frame_url))
    for ref in reference_images or []:
        image_candidates.append(("reference_images", ref))
    if last_frame_url:
        image_candidates.append(("last_frame_url", last_frame_url))

    if len(image_candidates) != 1:
        return None, _with_video_model_doc_hint({
            "error": (
                "grok-imagine-video-1.5 只支持一张源图图生视频；"
                f"当前解析到 {len(image_candidates)} 张源图"
            ),
            "error_kind": "bad_request",
            "model_feedback": _video_model_feedback(
                "xAI Grok Imagine Video 1.5 requires exactly one source image.",
                (
                    f"Read {_VIDEO_MODEL_CALLING_DOC}, keep exactly one visual_reference or "
                    "one reference_images entry on the existing video node, then call node.run(force)."
                ),
            ),
        })

    source_kind, source_ref = image_candidates[0]
    image, image_error = await _xai_image_input(project_id, source_ref, provider, extra_override)
    if image_error or not image:
        return None, _with_video_model_doc_hint({
            "error": image_error or "源图无法读取",
            "error_kind": "bad_request",
            "hint": "确认该 image 节点已完成且有可读输出，或传入可访问图片 URL / 项目内图片路径。",
        })

    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": clean_prompt,
        "image": image,
        "duration": duration,
    }
    raw_resolution = extra.pop("resolution", None)
    if _has_value(raw_resolution):
        resolution = str(raw_resolution).strip().lower()
        if resolution not in _XAI_VIDEO_RESOLUTIONS:
            return None, _unsupported_video_resolution_error(
                "xAI video",
                model_name,
                raw_resolution,
                _XAI_VIDEO_RESOLUTIONS,
            )
        payload["resolution"] = resolution
    if "seed" in extra:
        seed = _coerce_int(extra.pop("seed"))
        if seed is not None:
            payload["seed"] = seed

    return payload, {"source_image_kind": source_kind, "source_image_ref": source_ref}


async def _build_grok_1_5_video_payload(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, str] | None, tuple[str, bytes, str] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})

    model_name = str(extra.pop("model", None) or provider.model_name or "").strip()
    if not model_name:
        return None, None, {"error": "Grok 1.5 video provider 缺少 model_name", "error_kind": "bad_config"}

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        return None, None, {"error": "Grok 1.5 video 请求缺少 prompt", "error_kind": "bad_request"}

    image_candidates: list[tuple[str, str]] = []
    if first_frame_url:
        image_candidates.append(("first_frame_url", first_frame_url))
    for ref in reference_images or []:
        image_candidates.append(("reference_images", ref))
    if last_frame_url:
        image_candidates.append(("last_frame_url", last_frame_url))

    if len(image_candidates) != 1:
        return None, None, _with_video_model_doc_hint({
            "error": (
                "grok-1.5-video-15s 只支持一张 input_reference；"
                f"当前解析到 {len(image_candidates)} 张源图"
            ),
            "error_kind": "bad_request",
            "model_feedback": _video_model_feedback(
                "Grok 1.5 requires exactly one input_reference image.",
                (
                    f"Read {_VIDEO_MODEL_CALLING_DOC}, keep exactly one visual_reference or "
                    "one reference_images entry on the existing video node, then call node.run(force)."
                ),
            ),
        })

    source_kind, source_ref = image_candidates[0]
    image_file, image_error = await _image_file_input(project_id, source_ref)
    if image_error or not image_file:
        return None, None, _with_video_model_doc_hint({
            "error": image_error or "源图无法读取",
            "error_kind": "bad_request",
            "hint": "确认该 image 节点已完成且有可读输出，或传入可访问图片 URL / 项目内图片路径。",
        })

    resolution = str(extra.pop("resolution", "") or "").strip().lower()
    if resolution and resolution not in _GROK_1_5_VIDEO_RESOLUTIONS and not re.match(r"^\d{3,5}x\d{3,5}$", resolution):
        return None, None, _unsupported_video_resolution_error(
            "Grok 1.5 video",
            model_name,
            resolution,
            _GROK_1_5_VIDEO_RESOLUTIONS,
        )
    data = {
        "model": model_name,
        "prompt": clean_prompt,
        "size": _video_size_for_resolution(resolution or "720p", extra.get("aspect_ratio")),
    }
    return data, image_file, {"source_image_kind": source_kind, "source_image_ref": source_ref}


def _json_video_task_set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        return
    current = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _json_video_task_payload_fields(spec: JsonVideoTaskSpec, extra: dict[str, Any]) -> dict[str, str]:
    fields = dict(spec.payload_fields)
    configured = extra.get("payload_fields") or extra.get("payload_field_paths")
    if isinstance(configured, dict):
        for key, value in configured.items():
            if key in fields and isinstance(value, str) and value.strip():
                fields[key] = value.strip()
    resolution_field = extra.get("resolution_payload_field") or extra.get("size_payload_field")
    if isinstance(resolution_field, str) and resolution_field.strip():
        fields["resolution"] = resolution_field.strip()
    return fields


def _json_video_task_field_types(spec: JsonVideoTaskSpec, extra: dict[str, Any]) -> dict[str, str]:
    field_types = dict(spec.field_types)
    configured = extra.get("field_types") or extra.get("payload_field_types")
    if isinstance(configured, dict):
        for key, value in configured.items():
            if key in field_types and isinstance(value, str) and value.strip():
                field_types[key] = value.strip()
    resolution_type = extra.get("resolution_field_type") or extra.get("size_field_type")
    if isinstance(resolution_type, str) and resolution_type.strip():
        field_types["resolution"] = resolution_type.strip()
    return field_types


def _json_video_task_put(
    payload: dict[str, Any],
    spec: JsonVideoTaskSpec,
    field: str,
    value: Any,
    payload_fields: dict[str, str] | None = None,
    field_types: dict[str, str] | None = None,
) -> None:
    fields = payload_fields or spec.payload_fields
    types = field_types or spec.field_types
    payload_field = fields.get(field)
    if not payload_field:
        return
    field_type = types.get(field, "string")
    converted: Any
    if field_type == "integer":
        converted = _coerce_int(value)
        if converted is None:
            return
        _json_video_task_set_path(payload, payload_field, converted)
    elif field_type == "string_upper":
        _json_video_task_set_path(payload, payload_field, str(value).upper())
    elif field_type == "url_list":
        if isinstance(value, list):
            converted = [str(item) for item in value if str(item or "").strip()]
            _json_video_task_set_path(payload, payload_field, converted)
    else:
        _json_video_task_set_path(payload, payload_field, str(value))


def _json_video_task_payload_value(
    payload: dict[str, Any],
    spec: JsonVideoTaskSpec,
    field: str,
    payload_fields: dict[str, str] | None = None,
) -> Any:
    fields = payload_fields or spec.payload_fields
    payload_field = fields.get(field)
    if not payload_field:
        return None
    return _lookup_path(payload, payload_field)


async def _build_json_video_task_payload(
    spec: JsonVideoTaskSpec,
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[tuple[str, str]], dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    payload_fields = _json_video_task_payload_fields(spec, extra)
    field_types = _json_video_task_field_types(spec, extra)
    supported_ratios = _json_video_task_supported_ratios(spec, extra)
    supported_resolutions = _json_video_task_supported_resolutions(spec, extra)
    supported_durations = _int_set(extra.get("supported_durations") or extra.get("duration_options"))
    duration_min, duration_max = _json_video_task_duration_bounds(spec, extra)

    model_name = str(extra.pop("model", None) or provider.model_name or "").strip()
    if not model_name:
        return None, [], {"error": f"{spec.display_name} provider 缺少 model_name", "error_kind": "bad_config"}

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        return None, [], {"error": f"{spec.display_name} 请求缺少 prompt", "error_kind": "bad_request"}

    duration, duration_error = _json_video_task_duration(
        extra.pop("duration", None) or duration_seconds,
        spec,
        supported_durations=supported_durations,
        duration_min=duration_min,
        duration_max=duration_max,
    )
    if duration_error:
        return None, [], _with_video_model_doc_hint({
            "error": duration_error,
            "error_kind": "bad_request",
        })

    default_ratio = str(extra.get("default_ratio") or extra.get("default_aspect_ratio") or spec.default_ratio).strip().lower()
    if default_ratio not in supported_ratios:
        default_ratio = spec.default_ratio if spec.default_ratio in supported_ratios else sorted(supported_ratios)[0]
    ratio = _json_video_task_ratio(extra.pop("ratio", None) or extra.pop("aspect_ratio", None), supported_ratios)
    if ratio is None:
        ratio = default_ratio

    raw_resolution = extra.pop("resolution", None) or extra.pop("size", None) or extra.get("default_resolution") or extra.get("default_size") or spec.default_resolution
    resolution = _json_video_task_resolution(raw_resolution, supported_resolutions)
    if resolution is None or resolution.lower() not in {item.lower() for item in supported_resolutions}:
        return None, [], _unsupported_video_resolution_error(
            spec.display_name,
            model_name,
            raw_resolution,
            set(supported_resolutions),
        )
    if (
        spec.long_duration_after is not None
        and spec.long_duration_resolution
        and duration
        and duration > spec.long_duration_after
        and resolution != spec.long_duration_resolution
    ):
        return None, [], _with_video_model_doc_hint({
            "error": (
                f"{spec.display_name} {spec.long_duration_after + 1}-{duration_max} 秒视频"
                f"只支持 resolution='{spec.long_duration_resolution}'"
            ),
            "error_kind": "bad_request",
            "supported_resolutions": [spec.long_duration_resolution],
        })

    image_candidates: list[tuple[str, str]] = []
    if first_frame_url:
        image_candidates.append(("first_frame_url", first_frame_url))
    for ref in reference_images or []:
        image_candidates.append(("reference_images", ref))
    if last_frame_url:
        image_candidates.append(("last_frame_url", last_frame_url))

    if len(image_candidates) < spec.source_images_min:
        return None, [], _with_video_model_doc_hint({
            "error": f"{spec.display_name} 至少需要 {spec.source_images_min} 张参考图，当前解析到 {len(image_candidates)} 张",
            "error_kind": "bad_request",
        })
    if len(image_candidates) > spec.source_images_max:
        return None, [], _with_video_model_doc_hint({
            "error": f"{spec.display_name} 最多支持 {spec.source_images_max} 张参考图，当前解析到 {len(image_candidates)} 张",
            "error_kind": "bad_request",
            "model_feedback": _video_model_feedback(
                f"{spec.display_name} supports at most {spec.source_images_max} reference images.",
                (
                    f"Read {_VIDEO_MODEL_CALLING_DOC}, keep no more than {spec.source_images_max} visual_reference "
                    "entries on the existing video node, then call node.run(force)."
                ),
            ),
        })

    payload: dict[str, Any] = {}
    _json_video_task_put(payload, spec, "prompt", clean_prompt, payload_fields, field_types)
    _json_video_task_put(payload, spec, "model", model_name, payload_fields, field_types)
    _json_video_task_put(payload, spec, "ratio", ratio, payload_fields, field_types)
    _json_video_task_put(payload, spec, "duration", duration, payload_fields, field_types)
    _json_video_task_put(
        payload,
        spec,
        "resolution",
        _json_video_task_payload_resolution(resolution, spec, extra),
        payload_fields,
        field_types,
    )
    if "seed" in extra:
        seed = _coerce_int(extra.pop("seed"))
        if seed is not None and seed > 0:
            _json_video_task_put(payload, spec, "seed", seed, payload_fields, field_types)

    return payload, image_candidates, {
        "source_image_count": len(image_candidates),
        "source_image_refs": [ref for _, ref in image_candidates],
        "payload_fields": payload_fields,
        "field_types": field_types,
    }


async def _build_t8_grok_video_3_payload(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[tuple[str, str]], dict[str, Any] | None]:
    return await _build_json_video_task_payload(
        _T8_GROK_VIDEO_3_SPEC,
        provider,
        project_id,
        prompt,
        first_frame_url,
        last_frame_url,
        duration_seconds,
        reference_images,
        extra_override,
    )


async def _upload_json_video_task_image(
    spec: JsonVideoTaskSpec,
    provider: MediaProvider,
    project_id: str,
    ref: str,
    client: httpx.AsyncClient,
) -> tuple[str | None, str | None]:
    image_file, image_error = await _image_file_input(project_id, ref)
    if image_error or not image_file:
        return None, image_error or "图片引用无法读取"

    filename, content, mime = image_file
    provider_params = _parse_extra(provider)
    endpoint = _json_video_task_upload_endpoint(provider.base_url, spec, provider_params)
    if not endpoint:
        required = spec.upload_base_url_param or "base_url"
        return None, f"{spec.display_name} 未配置参考图上传 API Base URL: params.{required}"
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    uploaded = await client.post(
        endpoint,
        files={spec.upload_file_field: (filename, content, mime)},
        headers=headers,
    )
    if uploaded.status_code >= 400:
        return None, f"上传参考图失败: HTTP {uploaded.status_code} - {uploaded.text[:300]}"
    upload_data, upload_error = _response_json(uploaded, endpoint)
    if upload_error:
        return None, upload_error.get("error") or "上传参考图响应无法解析"
    url = _first_path_text(upload_data or {}, spec.upload_response_url_paths)
    if not url:
        return None, f"上传参考图响应缺少 url: {str(upload_data)[:300]}"
    return url, None


async def _download_video_result(project_id: str, remote_url: str) -> dict[str, Any]:
    filename = f"{uuid.uuid4().hex[:12]}.mp4"
    dest = _storage_video_path(project_id, filename)
    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            resp = await client.get(remote_url)
        if resp.status_code != 200:
            return {"download_error": f"下载视频失败: HTTP {resp.status_code}"}
        dest.write_bytes(resp.content)
    except Exception as exc:
        return {"download_error": f"下载视频失败: {exc}"}
    return {
        "local_path": str(dest),
        "local_url": f"/api/media/{project_id}/generated_videos/{filename}",
    }


def _audio_suffix(remote_url: str, content_type: str | None = None) -> str:
    parsed_suffix = Path(urlparse(remote_url).path).suffix.lower()
    if parsed_suffix in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return parsed_suffix
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    mapped = mimetypes.guess_extension(mime or "")
    if mapped in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return mapped
    if mime == "audio/mpeg":
        return ".mp3"
    return ".mp3"


async def _download_audio_result(project_id: str, remote_url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            resp = await client.get(remote_url)
        if resp.status_code != 200:
            return {"download_error": f"下载音频失败: HTTP {resp.status_code}"}
        content_type = resp.headers.get("content-type")
        suffix = _audio_suffix(remote_url, content_type)
        filename = f"{uuid.uuid4().hex[:12]}{suffix}"
        dest = _storage_audio_path(project_id, filename)
        dest.write_bytes(resp.content)
    except Exception as exc:
        return {"download_error": f"下载音频失败: {exc}"}
    return {
        "local_path": str(dest),
        "local_url": f"/api/media/{project_id}/generated_audio/{filename}",
        "mime_type": str(content_type or "").split(";", 1)[0].strip() or None,
    }


def _lookup_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (IndexError, TypeError, ValueError):
                return None
            continue
        if current is None:
            return None
        return None
    return current


def _first_path_text(data: dict[str, Any], paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = _lookup_path(data, path)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _json_video_task_api_error(spec: JsonVideoTaskSpec, data: dict[str, Any], endpoint: str) -> dict[str, Any] | None:
    code = data.get("code")
    if code is None:
        return None
    code_text = str(code).strip().lower()
    if code_text in {"0", "200", "success", "ok"}:
        return None
    msg = _first_text(data.get("msg"), data.get("message"), _lookup_path(data, "error.message"))
    detail = data.get("data")
    if isinstance(detail, dict):
        detail_text = _first_text(
            detail.get("详情"),
            detail.get("detail"),
            detail.get("message"),
            detail.get("error"),
        )
        if detail_text and detail_text not in str(msg or ""):
            msg = f"{msg or spec.display_name}: {detail_text}"
    error_kind = "provider_failed"
    numeric_code = _coerce_int(code)
    if numeric_code is not None:
        error_kind = _http_error_kind(numeric_code)
    return {
        "error": msg or f"{spec.display_name} 返回业务错误 code={code}",
        "error_kind": error_kind,
        "error_source": "external_media_provider",
        "provider_msg": json.dumps(data, ensure_ascii=False)[:800],
        "endpoint": endpoint,
        "raw": data,
    }


def _image_transport_mode(provider: MediaProvider, extra_override: dict[str, Any] | None, default: str = "data_url") -> str:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    raw = str(
        extra.get("image_transport")
        or extra.get("reference_image_transport")
        or extra.get("image_input")
        or default
    ).strip().lower().replace("-", "_")
    if raw in {"public_url", "url", "remote_url", "http_url", "https_url"}:
        return "public_url"
    return "data_url"


def _public_media_url_for_ref(project_id: str, ref: str, public_base_url: str | None) -> tuple[str | None, str | None]:
    text = str(ref or "").strip()
    if not text:
        return None, "图片引用为空"
    if _is_remote_url(text):
        return text, None

    base = str(public_base_url or "").strip().rstrip("/")
    local_url: str | None = None
    if text.startswith("/api/media/") or text.startswith("/api/uploads/"):
        local_url = text
    elif text.startswith("generated_images/"):
        local_url = f"/api/media/{project_id}/{text}"
    elif text.startswith("uploads/"):
        local_url = f"/api/uploads/{project_id}/file/{text}"
    else:
        path_text = _project_media_path_from_url(project_id, text) or text
        try:
            path = Path(path_text).expanduser()
            if path.is_absolute() and path.exists() and path.is_file():
                project_root = (settings.storage_path_resolved / project_id).resolve()
                rel = path.resolve().relative_to(project_root).as_posix()
                if rel.startswith("uploads/"):
                    local_url = f"/api/uploads/{project_id}/file/{rel}"
                else:
                    local_url = f"/api/media/{project_id}/{rel}"
        except Exception:
            local_url = None

    if local_url and base:
        try:
            signed_url = sign_media_url(local_url)
        except MediaURLSigningError as exc:
            return None, str(exc)
        return f"{base}{signed_url}", None
    if local_url:
        return None, (
            "当前 provider 选择了公网 URL 图片输入模式。"
            "请在 provider.params.public_base_url 配置站点外网根地址，或传入 http(s) 图片 URL。"
        )
    return None, f"图片引用不是公网 URL，且无法转换为项目媒体 URL: {text}"


async def _image_url_or_data_url_for_ref(
    project_id: str,
    ref: str,
    provider: MediaProvider,
    extra_override: dict[str, Any] | None,
    *,
    default_transport: str = "data_url",
) -> tuple[str | None, str | None]:
    mode = _image_transport_mode(provider, extra_override, default_transport)
    if mode == "public_url":
        extra = _parse_extra(provider)
        extra.update(extra_override or {})
        return _public_media_url_for_ref(project_id, ref, _first_text(extra.get("public_base_url"), extra.get("site_base_url")))

    text = str(ref or "").strip()
    if not text:
        return None, "图片引用为空"
    if text.startswith("data:image/"):
        return text, None
    if _is_remote_url(text):
        return text, None
    local_ref = _project_media_path_from_url(project_id, text) or text
    data_url = await _ref_to_data_url(local_ref)
    if data_url:
        return data_url, None
    return None, f"图片引用无法读取或转换为 data URL: {text}"


def _audio_http_v1_first_path_text(data: Any, paths: Any) -> str | None:
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return None
    for path in paths:
        value = _lookup_path(data, str(path))
        text = str(value or "").strip()
        if text:
            return text
    return None


def _audio_http_v1_first_path_value(data: Any, paths: Any) -> Any:
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return None
    for path in paths:
        value = _lookup_path(data, str(path))
        if value not in (None, ""):
            return value
    return None


def _audio_http_v1_result_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("result")
    return section if isinstance(section, dict) else {}


def _audio_http_v1_normalize_audio_item(protocol: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    result = _audio_http_v1_result_section(protocol)
    remote_url = _audio_http_v1_first_path_text(item, result.get("url_paths") or result.get("audio_url_paths"))
    if not remote_url:
        return None
    return {
        "id": _audio_http_v1_first_path_text(item, result.get("id_paths")),
        "title": _audio_http_v1_first_path_text(item, result.get("title_paths")),
        "url": remote_url,
        "remote_url": remote_url,
        "source_audio_url": _audio_http_v1_first_path_text(item, result.get("source_url_paths")),
        "stream_audio_url": _audio_http_v1_first_path_text(item, result.get("stream_url_paths")),
        "image_url": _audio_http_v1_first_path_text(item, result.get("image_url_paths")),
        "duration_seconds": _audio_http_v1_first_path_value(item, result.get("duration_paths") or ["duration"]),
        "tags": _audio_http_v1_first_path_value(item, result.get("tags_paths") or ["tags"]),
    }


def _audio_http_v1_collect_from_value(protocol: dict[str, Any], value: Any, seen: set[int] | None = None) -> list[dict[str, Any]]:
    if seen is None:
        seen = set()
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_audio_http_v1_collect_from_value(protocol, item, seen))
        return out
    if not isinstance(value, dict):
        return []
    current = _audio_http_v1_normalize_audio_item(protocol, value)
    if current:
        return [current]
    out: list[dict[str, Any]] = []
    for nested in value.values():
        out.extend(_audio_http_v1_collect_from_value(protocol, nested, seen))
        if out:
            return out
    return out


def _audio_http_v1_collect_audio_items(protocol: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    result = _audio_http_v1_result_section(protocol)
    paths = result.get("items_paths") or result.get("audio_items_paths") or ["data", "audios", "audio", "items", "result"]
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = []
    for path in paths:
        value = _lookup_path(data, str(path))
        if value is None:
            continue
        found = _audio_http_v1_collect_from_value(protocol, value)
        if found:
            return found
    return _audio_http_v1_collect_from_value(protocol, data)


async def _localize_audio_items(project_id: str, items: list[dict[str, Any]], save_locally: bool) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        remote_url = str(item.get("remote_url") or item.get("url") or "").strip()
        if not remote_url:
            continue
        downloaded: dict[str, Any] = {}
        if save_locally:
            downloaded = await _download_audio_result(project_id, remote_url)
        localized = {
            **item,
            "n_index": idx,
            "url": downloaded.get("local_url") or remote_url,
            "local_url": downloaded.get("local_url"),
            "local_path": downloaded.get("local_path"),
            "remote_url": remote_url,
            "mime_type": downloaded.get("mime_type"),
            "download_error": downloaded.get("download_error"),
        }
        output.append(localized)
    return output


async def _save_audio_bytes(
    project_id: str,
    content: bytes,
    *,
    response_format: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    suffix = ""
    if response_format:
        normalized = str(response_format).strip().lower().lstrip(".")
        if normalized:
            suffix = f".{normalized}"
    if suffix not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".pcm"}:
        suffix = _audio_suffix("", content_type)
    filename = f"{uuid.uuid4().hex[:12]}{suffix}"
    dest = _storage_audio_path(project_id, filename)
    dest.write_bytes(content)
    return {
        "local_path": str(dest),
        "local_url": f"/api/media/{project_id}/generated_audio/{filename}",
        "mime_type": str(content_type or "").split(";", 1)[0].strip() or None,
    }


def _response_json(resp: httpx.Response, endpoint: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        data = resp.json()
    except ValueError:
        return None, {
            "error": f"响应不是 JSON: {resp.text[:400]}",
            "error_kind": "bad_response",
            "endpoint": endpoint,
        }
    if not isinstance(data, dict):
        return None, {
            "error": "响应 JSON 不是对象",
            "error_kind": "bad_response",
            "endpoint": endpoint,
            "raw": data,
        }
    return data, None


def _extract_image_candidate_from_raw_response(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _is_remote_url(text) or text.startswith("/"):
            return {"url": text, "b64": None}
        if text.startswith("data:image/") and "," in text:
            b64 = _image_http_v1_b64_value(text)
            if b64:
                return {"url": None, "b64": b64}
        return None

    if isinstance(value, list):
        for item in value:
            candidate = _extract_image_candidate_from_raw_response(item)
            if candidate:
                return candidate
        return None

    if isinstance(value, dict):
        priority = (
            "url",
            "remote_url",
            "local_url",
            "url_path",
            "path",
            "local_path",
            "image",
            "result",
            "output",
            "data",
            "images",
        )
        for key in priority:
            if key not in value:
                continue
            candidate = _extract_image_candidate_from_raw_response(value.get(key))
            if candidate:
                return candidate
        for item in value.values():
            candidate = _extract_image_candidate_from_raw_response(item)
            if candidate:
                return candidate

    return None


def _audio_http_v1_request_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("request")
    return section if isinstance(section, dict) else {}


def _audio_http_v1_poll_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("poll")
    return section if isinstance(section, dict) else {}


def _audio_http_v1_endpoint_for(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
    *,
    task_id: str | None = None,
) -> str:
    return _video_http_v1_endpoint_for(provider, protocol, section, task_id=task_id)


def _audio_http_v1_headers(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, str]:
    return _video_http_v1_headers(provider, protocol, section)


def _audio_http_v1_model_profile(protocol: dict[str, Any], model_name: str) -> dict[str, Any]:
    return _video_http_v1_model_profile(protocol, model_name)


def _audio_http_v1_response_success(protocol: dict[str, Any], data: dict[str, Any], section: dict[str, Any]) -> bool:
    success_path = str(section.get("success_path") or section.get("ok_path") or "").strip()
    if not success_path:
        return True
    value = _lookup_path(data, success_path)
    if value is None:
        return True
    configured = section.get("success_values") or section.get("ok_values")
    values = _string_set(configured) if configured is not None else {"0", "200", "success", "ok", "true"}
    return str(value).strip().lower() in {item.lower() for item in values}


def _audio_http_v1_provider_message(protocol: dict[str, Any], data: dict[str, Any]) -> str:
    result = _audio_http_v1_result_section(protocol)
    paths = result.get("message_paths") or [
        "error.message",
        "error",
        "message",
        "msg",
        "reason",
        "detail",
        "data.error",
        "data.message",
        "data.msg",
        "data.reason",
        "data.detail",
    ]
    found = _audio_http_v1_first_path_text(data, paths)
    return found or "音频生成任务失败"


def _audio_http_v1_task_id(protocol: dict[str, Any], data: dict[str, Any]) -> str | None:
    request = _audio_http_v1_request_section(protocol)
    paths = request.get("task_id_paths") or request.get("id_paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = ["id", "task_id", "taskId", "job_id", "data.id", "data.task_id", "data.taskId", "data.job_id"]
    return _audio_http_v1_first_path_text(data, paths)


def _audio_http_v1_status(protocol: dict[str, Any], data: dict[str, Any], fallback: str = "queued") -> str:
    poll = _audio_http_v1_poll_section(protocol)
    status_path = str(poll.get("status_path") or "status")
    value = _lookup_path(data, status_path) or data.get("status") or data.get("state")
    return str(value or fallback).strip().lower()


def _audio_http_v1_progress(protocol: dict[str, Any], data: dict[str, Any]) -> Any:
    poll = _audio_http_v1_poll_section(protocol)
    path = str(poll.get("progress_path") or "").strip()
    if path:
        return _lookup_path(data, path)
    return data.get("progress")


def _audio_http_v1_status_sets(protocol: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    poll = _audio_http_v1_poll_section(protocol)
    succeeded = _string_set(poll.get("succeeded") or poll.get("done_statuses")) or {"succeeded", "success", "completed", "complete", "done"}
    failed = _string_set(poll.get("failed") or poll.get("failed_statuses")) or {"failed", "failure", "error", "cancelled", "canceled", "expired"}
    running = _string_set(poll.get("running") or poll.get("running_statuses")) or {"queued", "pending", "running", "processing", "in_progress", "submitted", "created"}
    return {item.lower() for item in succeeded}, {item.lower() for item in failed}, {item.lower() for item in running}


def _audio_http_v1_poll_settings(provider: MediaProvider, protocol: dict[str, Any], extra_override: dict[str, Any] | None) -> tuple[float, float]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    poll = _audio_http_v1_poll_section(protocol)
    poll_interval = max(
        1.0,
        _coerce_float(
            extra.get("_poll_interval_seconds")
            or poll.get("interval_seconds")
            or os.getenv("DRAMA_AUDIO_POLL_INTERVAL_SECONDS")
            or 8,
            8.0,
        ),
    )
    poll_timeout = max(
        poll_interval,
        _coerce_float(
            extra.get("_poll_timeout_seconds")
            or poll.get("timeout_seconds")
            or os.getenv("DRAMA_AUDIO_POLL_TIMEOUT_SECONDS")
            or 1200,
            1200.0,
        ),
    )
    return poll_interval, poll_timeout


_AUDIO_HTTP_V1_INTERNAL_EXTRA_KEYS = {
    "audio_protocol_id",
    "protocol_id",
    "audio_protocol",
    "protocol",
    "model",
    "prompt",
    "input",
    "text",
    "title",
    "style",
    "lyrics",
    "mv",
    "version",
    "voice",
    "speed",
    "instructions",
    "response_format",
    "format",
    "audio_format",
    "instrumental",
    "make_instrumental",
    "customMode",
    "custom_mode",
    "negativeTags",
    "negative_tags",
    "callBackUrl",
    "callback_url",
    "notify_hook",
    "notifyHook",
}


def _audio_http_v1_payload_extra(extra: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in extra.items():
        if key in _AUDIO_HTTP_V1_INTERNAL_EXTRA_KEYS or str(key).startswith("_"):
            continue
        if value is not None:
            payload[str(key)] = value
    return payload


def _audio_http_v1_default_params(protocol: dict[str, Any], model_name: str) -> dict[str, Any]:
    defaults = protocol.get("default_params") if isinstance(protocol.get("default_params"), dict) else {}
    profile = _audio_http_v1_model_profile(protocol, model_name)
    profile_defaults = profile.get("default_params") if isinstance(profile.get("default_params"), dict) else {}
    return {**defaults, **profile_defaults}


def _audio_http_v1_numeric(value: Any, field_name: str) -> tuple[float | None, dict[str, Any] | None]:
    if value in (None, ""):
        return None, None
    try:
        return float(str(value)), None
    except (TypeError, ValueError):
        return None, {"error": f"audio_http_v1 {field_name} 必须是数字，收到: {value!r}", "error_kind": "bad_request"}


def _build_audio_http_v1_payload(
    provider: MediaProvider,
    prompt: str,
    title: str | None,
    style: str | None,
    instrumental: bool | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    protocol, protocol_error = _audio_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return None, protocol_error
    assert protocol is not None
    base_extra = _parse_extra(provider)
    model_name = str((extra_override or {}).get("model") or base_extra.get("model") or getattr(provider, "model_name", "") or "").strip()
    if not model_name:
        return None, {"error": "audio_http_v1 provider 缺少 model_name", "error_kind": "bad_config"}
    extra = _audio_http_v1_default_params(protocol, model_name)
    extra.update(base_extra)
    extra.update(extra_override or {})

    clean_prompt = str(prompt or "").strip()
    input_text = str(extra.get("input") or extra.get("text") or clean_prompt).strip()
    override = extra_override or {}
    response_format = str(
        override.get("response_format")
        or override.get("format")
        or override.get("audio_format")
        or extra.get("response_format")
        or extra.get("format")
        or extra.get("audio_format")
        or protocol.get("default_response_format")
        or ""
    ).strip().lower() or None
    speed, speed_error = _audio_http_v1_numeric(extra.get("speed"), "speed")
    if speed_error:
        return None, speed_error
    custom_mode = _coerce_bool(extra.get("customMode"))
    if custom_mode is None:
        custom_mode = _coerce_bool(extra.get("custom_mode"))
    if instrumental is None:
        instrumental = _coerce_bool(extra.get("instrumental"))
    if instrumental is None:
        instrumental = _coerce_bool(extra.get("make_instrumental"))

    context = {
        "model": model_name,
        "prompt": clean_prompt,
        "input": input_text,
        "text": input_text,
        "title": str(title or extra.get("title") or "").strip() or None,
        "style": str(style or extra.get("style") or "").strip() or None,
        "lyrics": str(extra.get("lyrics") or "").strip() or None,
        "mv": str(extra.get("mv") or extra.get("version") or "").strip() or None,
        "voice": str(extra.get("voice") or "").strip() or None,
        "speed": speed,
        "instructions": str(extra.get("instructions") or style or extra.get("style") or "").strip() or None,
        "response_format": response_format,
        "format": response_format,
        "audio_format": response_format,
        "instrumental": instrumental,
        "customMode": custom_mode,
        "custom_mode": custom_mode,
        "negativeTags": extra.get("negativeTags") or extra.get("negative_tags"),
        "callBackUrl": extra.get("callBackUrl") or extra.get("callback_url"),
        "notify_hook": extra.get("notify_hook") or extra.get("notifyHook"),
        "seed": _coerce_int(extra.get("seed")),
    }
    request = _audio_http_v1_request_section(protocol)
    required_context = _string_set(request.get("required_context"))
    for key in sorted(required_context):
        if not _has_value(context.get(key)):
            return None, {"error": f"audio_http_v1 请求缺少 {key}", "error_kind": "bad_request"}
    body_template = request.get("body")
    if not isinstance(body_template, dict):
        return None, {"error": "audio_http_v1 request.body 必须是对象", "error_kind": "bad_config"}
    payload = _video_http_v1_render_value(body_template, context)
    if not isinstance(payload, dict):
        return None, {"error": "audio_http_v1 request.body 渲染结果不是对象", "error_kind": "bad_config"}
    if _coerce_bool(request.get("merge_extra")):
        payload = {**payload, **_audio_http_v1_payload_extra(extra)}
    return payload, {
        "protocol": protocol,
        "request": {
            "has_title": bool(context["title"]),
            "has_style": bool(context["style"]),
            "instrumental": instrumental,
            "response_format": response_format,
        },
    }


async def _audio_http_v1_completed_result(
    *,
    provider: MediaProvider,
    project_id: str,
    protocol: dict[str, Any],
    data: dict[str, Any],
    endpoint: str,
    payload: dict[str, Any],
    job_id: str | None = None,
    polls: list[dict[str, Any]] | None = None,
    save_locally: bool,
) -> dict[str, Any]:
    items = _audio_http_v1_collect_audio_items(protocol, data)
    if not items:
        return {
            "error": "audio_http_v1 任务成功但响应缺少音频 URL",
            "error_kind": "bad_response",
            "endpoint": endpoint,
            "job_id": job_id,
            "raw": data,
            "polls": polls or [],
        }
    audios = await _localize_audio_items(project_id, items, save_locally)
    primary = audios[0] if audios else {}
    return {
        "ok": True,
        "provider": provider.name,
        "model": payload.get("model") or provider.model_name,
        "status": "completed",
        "job_id": job_id,
        "url": primary.get("url"),
        "local_url": primary.get("local_url"),
        "local_path": primary.get("local_path"),
        "remote_url": primary.get("remote_url"),
        "stream_audio_url": primary.get("stream_audio_url"),
        "source_audio_url": primary.get("source_audio_url"),
        "image_url": primary.get("image_url"),
        "duration": primary.get("duration_seconds"),
        "mime_type": primary.get("mime_type"),
        "voice": payload.get("voice"),
        "speed": payload.get("speed"),
        "instructions": payload.get("instructions"),
        "format": payload.get("response_format"),
        "endpoint": endpoint,
        "audios": audios,
        "polls": polls or [],
        "download_error": primary.get("download_error"),
    }


async def _call_audio_http_v1(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    title: str | None,
    style: str | None,
    instrumental: bool | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "audio_http_v1 provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": "audio_http_v1 provider 缺少 Base URL", "error_kind": "bad_config"}
    protocol, protocol_error = _audio_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return protocol_error
    assert protocol is not None
    payload, payload_meta = _build_audio_http_v1_payload(
        provider=provider,
        prompt=prompt,
        title=title,
        style=style,
        instrumental=instrumental,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_meta or {"error": "无法构造 audio_http_v1 请求", "error_kind": "bad_request"}

    request = _audio_http_v1_request_section(protocol)
    endpoint = _audio_http_v1_endpoint_for(provider, protocol, request)
    if not endpoint:
        return {"error": "audio_http_v1 provider 缺少 base_url 或 request.path", "error_kind": "bad_config"}
    headers = _audio_http_v1_headers(provider, protocol, request)
    method = str(request.get("method") or "POST").strip().upper()
    result_section = _audio_http_v1_result_section(protocol)
    response_type = str(result_section.get("type") or result_section.get("response_type") or request.get("response_type") or "json").strip().lower()

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            resp = await client.get(endpoint, params=payload, headers=headers) if method == "GET" else await client.request(method, endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {"error": f"网络请求失败: {exc}", "error_kind": "network", "endpoint": endpoint}

    if resp.status_code >= 400:
        return _make_http_error(resp.status_code, resp.text, endpoint)

    if response_type in {"binary", "binary_audio", "audio_bytes"}:
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type.lower():
            data, parse_error = _response_json(resp, endpoint)
            if parse_error:
                return parse_error
            return {"error": "audio_http_v1 响应是 JSON，不是音频二进制", "error_kind": "bad_response", "endpoint": endpoint, "raw": data}
        if not resp.content:
            return {"error": "audio_http_v1 响应为空", "error_kind": "empty_response", "endpoint": endpoint}
        saved: dict[str, Any] = {}
        if save_locally:
            saved = await _save_audio_bytes(
                project_id,
                resp.content,
                response_format=str(payload.get("response_format") or payload.get("format") or ""),
                content_type=content_type,
            )
        return {
            "ok": True,
            "provider": provider.name,
            "model": payload.get("model") or provider.model_name,
            "status": "completed",
            "url": saved.get("local_url"),
            "local_url": saved.get("local_url"),
            "local_path": saved.get("local_path"),
            "mime_type": saved.get("mime_type") or str(content_type or "").split(";", 1)[0].strip() or None,
            "voice": payload.get("voice"),
            "speed": payload.get("speed"),
            "instructions": payload.get("instructions"),
            "format": payload.get("response_format") or payload.get("format"),
            "endpoint": endpoint,
            "audios": [{
                "n_index": 0,
                "url": saved.get("local_url"),
                "local_url": saved.get("local_url"),
                "local_path": saved.get("local_path"),
                "mime_type": saved.get("mime_type"),
            }] if saved.get("local_url") else [],
        }

    create_data, create_error = _response_json(resp, endpoint)
    if create_error:
        return create_error
    create_data = create_data or {}
    if not _audio_http_v1_response_success(protocol, create_data, request):
        provider_msg = _audio_http_v1_provider_message(protocol, create_data)
        return {"error": provider_msg, "error_kind": "provider_failed", "endpoint": endpoint, "provider_msg": provider_msg, "raw": create_data}

    immediate_items = _audio_http_v1_collect_audio_items(protocol, create_data)
    if immediate_items:
        return await _audio_http_v1_completed_result(
            provider=provider,
            project_id=project_id,
            protocol=protocol,
            data=create_data,
            endpoint=endpoint,
            payload=payload,
            job_id=_audio_http_v1_task_id(protocol, create_data),
            save_locally=save_locally,
        )

    task_id = _audio_http_v1_task_id(protocol, create_data)
    if not task_id:
        return {"error": "创建 audio_http_v1 任务响应缺少 task id 或音频结果", "error_kind": "bad_response", "endpoint": endpoint, "raw": create_data}
    status = _audio_http_v1_status(protocol, create_data)
    succeeded, failed, running = _audio_http_v1_status_sets(protocol)
    poll_section = _audio_http_v1_poll_section(protocol)
    query_endpoint = _audio_http_v1_endpoint_for(provider, protocol, poll_section, task_id=task_id)
    queued_result = {
        "ok": True,
        "provider": provider.name,
        "model": provider.model_name,
        "status": "running" if status in running or status == "running" else "queued",
        "job_id": task_id,
        "endpoint": endpoint,
        "query_endpoint": query_endpoint,
        "request": (payload_meta or {}).get("request") or {},
    }
    if status in succeeded:
        return await _audio_http_v1_completed_result(
            provider=provider,
            project_id=project_id,
            protocol=protocol,
            data=create_data,
            endpoint=endpoint,
            payload=payload,
            job_id=task_id,
            save_locally=save_locally,
        )
    if status in failed:
        provider_msg = _audio_http_v1_provider_message(protocol, create_data)
        return {"error": provider_msg, "error_kind": "provider_failed", "endpoint": endpoint, "provider_msg": provider_msg, "raw": create_data, "job_id": task_id, "status": status}
    if not wait_for_completion:
        return queued_result
    return await _poll_audio_http_v1_task(
        provider=provider,
        project_id=project_id,
        task_id=task_id,
        extra_override=extra_override,
        save_locally=save_locally,
    )


async def _poll_audio_http_v1_task(
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    protocol, protocol_error = _audio_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return {**protocol_error, "job_id": task_id, "status": "failed"}
    assert protocol is not None
    poll = _audio_http_v1_poll_section(protocol)
    if not poll:
        return {"error": "audio_http_v1 协议未配置 poll，无法按 job_id 轮询", "error_kind": "unsupported_action", "job_id": task_id, "status": "failed"}
    query_endpoint = _audio_http_v1_endpoint_for(provider, protocol, poll, task_id=task_id)
    if not query_endpoint:
        return {"error": "audio_http_v1 protocol 缺少 poll.path", "error_kind": "bad_config", "job_id": task_id, "status": "failed"}
    headers = _audio_http_v1_headers(provider, protocol, poll)
    method = str(poll.get("method") or "GET").strip().upper()
    poll_interval, poll_timeout = _audio_http_v1_poll_settings(provider, protocol, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"
    succeeded, failed, _running = _audio_http_v1_status_sets(protocol)
    poll_body = poll.get("body") if isinstance(poll.get("body"), dict) else {}
    poll_payload = _video_http_v1_render_value(poll_body, {"task_id": task_id, "job_id": task_id}) if poll_body else {}

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            while True:
                queried = await client.request(method, query_endpoint, json=poll_payload, headers=headers) if method != "GET" else await client.get(query_endpoint, headers=headers)
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, query_endpoint)
                    err.update({"job_id": task_id, "status": status or "unknown"})
                    return err
                query_data, query_error = _response_json(queried, query_endpoint)
                if query_error:
                    query_error.update({"job_id": task_id, "status": status or "unknown"})
                    return query_error
                query_data = query_data or {}
                latest_data = query_data
                status = _audio_http_v1_status(protocol, query_data, status)
                progress = _audio_http_v1_progress(protocol, query_data)
                polls.append({"status": status, "progress": progress})
                await _notify_progress(progress_callback, {
                    "job_id": task_id,
                    "status": status,
                    "progress": progress,
                    "poll_count": len(polls),
                    "provider": provider.name,
                    "model": provider.model_name,
                    "endpoint": query_endpoint,
                })

                if not _audio_http_v1_response_success(protocol, query_data, poll):
                    provider_msg = _audio_http_v1_provider_message(protocol, query_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "raw": query_data,
                        "polls": polls,
                    }

                result_section = _audio_http_v1_result_section(protocol)
                complete_on_items = _coerce_bool(result_section.get("complete_on_audio_items"))
                if complete_on_items is None:
                    complete_on_items = True
                if _audio_http_v1_collect_audio_items(protocol, query_data) and (complete_on_items or status in succeeded):
                    return await _audio_http_v1_completed_result(
                        provider=provider,
                        project_id=project_id,
                        protocol=protocol,
                        data=query_data,
                        endpoint=query_endpoint,
                        payload={"model": provider.model_name},
                        job_id=task_id,
                        polls=polls,
                        save_locally=save_locally,
                    )

                if status in succeeded:
                    return await _audio_http_v1_completed_result(
                        provider=provider,
                        project_id=project_id,
                        protocol=protocol,
                        data=query_data,
                        endpoint=query_endpoint,
                        payload={"model": provider.model_name},
                        job_id=task_id,
                        polls=polls,
                        save_locally=save_locally,
                    )

                if status in failed:
                    provider_msg = _audio_http_v1_provider_message(protocol, query_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "raw": query_data,
                        "polls": polls,
                    }

                if time.monotonic() >= deadline:
                    return {
                        "error": f"音频任务仍在 {status}，已超过本地轮询超时 {int(poll_timeout)} 秒",
                        "error_kind": "timeout",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "raw": latest_data,
                        "polls": polls,
                    }
                await asyncio.sleep(poll_interval)
    except httpx.HTTPError as exc:
        return {"error": f"网络请求失败: {exc}", "error_kind": "network", "endpoint": query_endpoint}


async def _call_volcengine_ark_video(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "Volcengine Ark video provider 缺少 API Key", "error_kind": "bad_config"}

    payload, payload_meta = await _build_ark_video_payload(
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=reference_images,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_meta or {"error": "无法构造 Seedance 2.0 请求", "error_kind": "bad_request"}

    endpoint = _ark_video_tasks_endpoint(provider.base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            created = await client.post(endpoint, json=payload, headers=headers)
            if created.status_code >= 400:
                err = _make_http_error(created.status_code, created.text, endpoint)
                if err.get("error_kind") == "bad_request":
                    _with_video_model_doc_hint(err)
                return err
            create_data, create_error = _response_json(created, endpoint)
            if create_error:
                return create_error
            task_id = str(create_data.get("id") or create_data.get("task_id") or "").strip()
            if not task_id:
                return {
                    "error": "创建视频任务响应缺少 id",
                    "error_kind": "bad_response",
                    "endpoint": endpoint,
                    "raw": create_data,
                }

            query_endpoint = f"{endpoint.rstrip('/')}/{task_id}"
            status = str(create_data.get("status") or "queued").lower()
            queued_result = {
                "ok": True,
                "provider": provider.name,
                "model": payload.get("model") or provider.model_name,
                "status": "running" if status == "running" else "queued",
                "job_id": task_id,
                "endpoint": endpoint,
                "query_endpoint": query_endpoint,
                "created_at": create_data.get("created_at"),
                "updated_at": create_data.get("updated_at"),
                "reference_warnings": (payload_meta or {}).get("reference_warnings") or [],
                "request": {
                    "content_count": len(payload.get("content") or []),
                    "duration": payload.get("duration"),
                    "ratio": payload.get("ratio"),
                    "resolution": payload.get("resolution"),
                },
            }
            if not wait_for_completion and status not in _ARK_TERMINAL_STATUSES:
                return queued_result

            final_result = await _poll_volcengine_ark_video_task(
                provider=provider,
                project_id=project_id,
                task_id=task_id,
                extra_override=extra_override,
                save_locally=save_locally,
            )
            if payload_meta and payload_meta.get("reference_warnings"):
                final_result["reference_warnings"] = [
                    *payload_meta.get("reference_warnings", []),
                    *(
                        final_result.get("reference_warnings")
                        if isinstance(final_result.get("reference_warnings"), list)
                        else []
                    ),
                ]
            return final_result
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }


def _ark_poll_settings(provider: MediaProvider, extra_override: dict[str, Any] | None) -> tuple[float, float]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    poll_interval = max(
        1.0,
        _coerce_float(
            extra.get("_poll_interval_seconds")
            or os.getenv("DRAMA_VIDEO_POLL_INTERVAL_SECONDS")
            or 10,
            10.0,
        ),
    )
    poll_timeout = max(
        poll_interval,
        _coerce_float(
            extra.get("_poll_timeout_seconds")
            or os.getenv("DRAMA_VIDEO_POLL_TIMEOUT_SECONDS")
            or 1200,
            1200.0,
        ),
    )
    return poll_interval, poll_timeout


async def _poll_volcengine_ark_video_task(
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "Volcengine Ark video provider 缺少 API Key", "error_kind": "bad_config"}

    endpoint = _ark_video_tasks_endpoint(provider.base_url)
    query_endpoint = f"{endpoint.rstrip('/')}/{task_id}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    poll_interval, poll_timeout = _ark_poll_settings(provider, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            while True:
                queried = await client.get(query_endpoint, headers=headers)
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, query_endpoint)
                    err.update({"job_id": task_id, "status": status or "unknown"})
                    return err
                query_data, query_error = _response_json(queried, query_endpoint)
                if query_error:
                    query_error.update({"job_id": task_id, "status": status or "unknown"})
                    return query_error

                latest_data = query_data
                status = str(query_data.get("status") or status or "unknown").lower()
                polls.append({
                    "status": status,
                    "updated_at": query_data.get("updated_at"),
                })
                await _notify_progress(progress_callback, {
                    "job_id": task_id,
                    "status": status,
                    "progress": query_data.get("progress"),
                    "poll_count": len(polls),
                    "provider": provider.name,
                    "model": provider.model_name,
                    "endpoint": query_endpoint,
                    "updated_at": query_data.get("updated_at"),
                })

                if status == "succeeded":
                    content = query_data.get("content") if isinstance(query_data.get("content"), dict) else {}
                    remote_url = (
                        content.get("video_url")
                        or query_data.get("video_url")
                        or content.get("url")
                    )
                    if not remote_url:
                        return {
                            "error": "Seedance 2.0 任务成功但响应缺少 video_url",
                            "error_kind": "bad_response",
                            "job_id": task_id,
                            "status": status,
                            "endpoint": query_endpoint,
                            "raw": query_data,
                        }
                    downloaded: dict[str, Any] = {}
                    if save_locally:
                        downloaded = await _download_video_result(project_id, str(remote_url))
                    return {
                        "ok": True,
                        "provider": provider.name,
                        "model": provider.model_name,
                        "status": "completed",
                        "job_id": task_id,
                        "url": downloaded.get("local_url") or remote_url,
                        "local_url": downloaded.get("local_url"),
                        "local_path": downloaded.get("local_path"),
                        "remote_url": remote_url,
                        "last_frame_url": content.get("last_frame_url"),
                        "duration": query_data.get("duration"),
                        "ratio": query_data.get("ratio"),
                        "resolution": query_data.get("resolution"),
                        "framespersecond": query_data.get("framespersecond"),
                        "generate_audio": query_data.get("generate_audio"),
                        "seed": query_data.get("seed"),
                        "usage": query_data.get("usage"),
                        "created_at": query_data.get("created_at"),
                        "updated_at": query_data.get("updated_at"),
                        "polls": polls,
                        "download_error": downloaded.get("download_error"),
                    }

                if status in _ARK_TERMINAL_STATUSES:
                    provider_msg = (
                        query_data.get("error")
                        or query_data.get("message")
                        or query_data.get("reason")
                        or "视频生成任务失败"
                    )
                    return {
                        "error": str(provider_msg),
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": str(provider_msg),
                        "raw": query_data,
                        "polls": polls,
                    }

                if time.monotonic() >= deadline:
                    return {
                        "error": f"视频任务仍在 {status}，已超过本地轮询超时 {int(poll_timeout)} 秒",
                        "error_kind": "timeout",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "raw": latest_data,
                        "polls": polls,
                    }

                await asyncio.sleep(poll_interval)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": query_endpoint,
        }


async def _call_xai_video(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "xAI video provider 缺少 API Key", "error_kind": "bad_config"}

    payload, payload_meta = await _build_xai_video_payload(
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=reference_images,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_meta or {"error": "无法构造 xAI video 请求", "error_kind": "bad_request"}

    endpoint = _xai_video_generations_endpoint(provider.base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            created = await client.post(endpoint, json=payload, headers=headers)
            if created.status_code >= 400:
                err = _make_http_error(created.status_code, created.text, endpoint)
                if err.get("error_kind") == "bad_request":
                    _with_video_model_doc_hint(err)
                return err
            create_data, create_error = _response_json(created, endpoint)
            if create_error:
                return create_error
            request_id = str(create_data.get("request_id") or create_data.get("id") or "").strip()
            if not request_id:
                return {
                    "error": "创建 xAI 视频任务响应缺少 request_id",
                    "error_kind": "bad_response",
                    "endpoint": endpoint,
                    "raw": create_data,
                }

            query_endpoint = _xai_video_query_endpoint(provider.base_url, request_id)
            status = str(create_data.get("status") or "queued").lower()
            queued_result = {
                "ok": True,
                "provider": provider.name,
                "model": payload.get("model") or provider.model_name,
                "status": "running" if status in _XAI_RUNNING_STATUSES else "queued",
                "job_id": request_id,
                "endpoint": endpoint,
                "query_endpoint": query_endpoint,
                "source_image_kind": (payload_meta or {}).get("source_image_kind"),
                "source_image_ref": (payload_meta or {}).get("source_image_ref"),
                "request": {
                    "duration": payload.get("duration"),
                    "has_image": bool(payload.get("image")),
                    "resolution": payload.get("resolution"),
                },
            }
            if not wait_for_completion and status not in (_XAI_DONE_STATUSES | _XAI_FAILED_STATUSES):
                return queued_result

            return await _poll_xai_video_task(
                provider=provider,
                project_id=project_id,
                request_id=request_id,
                extra_override=extra_override,
                save_locally=save_locally,
            )
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }


async def _call_grok_1_5_video(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "Grok 1.5 video provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": "Grok 1.5 video provider 缺少 Base URL", "error_kind": "bad_config"}

    data, image_file, payload_meta = await _build_grok_1_5_video_payload(
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_images=reference_images,
        extra_override=extra_override,
    )
    if data is None or image_file is None:
        return payload_meta or {"error": "无法构造 Grok 1.5 video 请求", "error_kind": "bad_request"}

    endpoint = _grok_1_5_video_endpoint(provider.base_url)
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    filename, content, mime = image_file
    files = {"input_reference": (filename, content, mime)}

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            created = await client.post(endpoint, data=data, files=files, headers=headers)
            if created.status_code >= 400:
                err = _make_http_error(created.status_code, created.text, endpoint)
                if err.get("error_kind") == "bad_request":
                    _with_video_model_doc_hint(err)
                return err
            create_data, create_error = _response_json(created, endpoint)
            if create_error:
                return create_error

            remote_url = _video_url_from_response(create_data)
            status = str(create_data.get("status") or ("completed" if remote_url else "queued")).lower()
            request_id = str(
                create_data.get("request_id")
                or create_data.get("id")
                or create_data.get("task_id")
                or create_data.get("job_id")
                or ""
            ).strip()

            if remote_url:
                downloaded: dict[str, Any] = {}
                if save_locally:
                    downloaded = await _download_video_result(project_id, str(remote_url))
                return {
                    "ok": True,
                    "provider": provider.name,
                    "model": data.get("model") or provider.model_name,
                    "status": "completed",
                    "job_id": request_id or None,
                    "url": downloaded.get("local_url") or remote_url,
                    "local_url": downloaded.get("local_url"),
                    "local_path": downloaded.get("local_path"),
                    "remote_url": remote_url,
                    "resolution": data.get("size"),
                    "endpoint": endpoint,
                    "raw": create_data,
                    "source_image_kind": (payload_meta or {}).get("source_image_kind"),
                    "source_image_ref": (payload_meta or {}).get("source_image_ref"),
                    "download_error": downloaded.get("download_error"),
                }

            if not request_id:
                return {
                    "error": "创建 Grok 1.5 视频任务响应缺少视频 URL 或任务 id",
                    "error_kind": "bad_response",
                    "endpoint": endpoint,
                    "raw": create_data,
                }

            query_endpoint = _grok_1_5_video_query_endpoint(provider.base_url, request_id)
            queued_result = {
                "ok": True,
                "provider": provider.name,
                "model": data.get("model") or provider.model_name,
                "status": "running" if status in _XAI_RUNNING_STATUSES else "queued",
                "job_id": request_id,
                "endpoint": endpoint,
                "query_endpoint": query_endpoint,
                "source_image_kind": (payload_meta or {}).get("source_image_kind"),
                "source_image_ref": (payload_meta or {}).get("source_image_ref"),
                "request": {
                    "duration": duration_seconds,
                    "has_image": True,
                    "size": data.get("size"),
                },
                "raw": create_data,
            }
            if not wait_for_completion and status not in (_XAI_DONE_STATUSES | _XAI_FAILED_STATUSES):
                return queued_result

            return await _poll_grok_1_5_video_task(
                provider=provider,
                project_id=project_id,
                request_id=request_id,
                extra_override=extra_override,
                save_locally=save_locally,
            )
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc!r}",
            "error_kind": "network",
            "endpoint": endpoint,
        }


async def _poll_grok_1_5_video_task(
    provider: MediaProvider,
    project_id: str,
    request_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "Grok 1.5 video provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": "Grok 1.5 video provider 缺少 Base URL", "error_kind": "bad_config"}

    query_endpoint = _grok_1_5_video_query_endpoint(provider.base_url, request_id)
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    poll_interval, poll_timeout = _xai_poll_settings(provider, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            while True:
                queried = await client.get(query_endpoint, headers=headers)
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, query_endpoint)
                    err.update({"job_id": request_id, "status": status or "unknown"})
                    return err
                query_data, query_error = _response_json(queried, query_endpoint)
                if query_error:
                    query_error.update({"job_id": request_id, "status": status or "unknown"})
                    return query_error

                latest_data = query_data
                status = str(query_data.get("status") or status or "unknown").lower()
                polls.append({
                    "status": status,
                    "progress": query_data.get("progress"),
                })
                await _notify_progress(progress_callback, {
                    "job_id": request_id,
                    "status": status,
                    "progress": query_data.get("progress"),
                    "poll_count": len(polls),
                    "provider": provider.name,
                    "model": provider.model_name,
                    "endpoint": query_endpoint,
                })

                remote_url = _video_url_from_response(query_data)
                if remote_url and (status in _XAI_DONE_STATUSES or status not in _XAI_FAILED_STATUSES):
                    downloaded: dict[str, Any] = {}
                    if save_locally:
                        downloaded = await _download_video_result(project_id, str(remote_url))
                    return {
                        "ok": True,
                        "provider": provider.name,
                        "model": query_data.get("model") or provider.model_name,
                        "status": "completed",
                        "job_id": request_id,
                        "url": downloaded.get("local_url") or remote_url,
                        "local_url": downloaded.get("local_url"),
                        "local_path": downloaded.get("local_path"),
                        "remote_url": remote_url,
                        "thumbnail_url": query_data.get("thumbnail_url"),
                        "duration": query_data.get("duration"),
                        "usage": query_data.get("usage"),
                        "progress": query_data.get("progress"),
                        "polls": polls,
                        "download_error": downloaded.get("download_error"),
                    }

                if status in _XAI_FAILED_STATUSES:
                    provider_msg = _xai_provider_message(query_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": query_data.get("model") or provider.model_name,
                        "job_id": request_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "raw": query_data,
                        "polls": polls,
                    }

                if time.monotonic() >= deadline:
                    return {
                        "error": f"Grok 1.5 视频任务仍在 {status}，已超过本地轮询超时 {int(poll_timeout)} 秒",
                        "error_kind": "timeout",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": request_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "raw": latest_data,
                        "polls": polls,
                    }

                await asyncio.sleep(poll_interval)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc!r}",
            "error_kind": "network",
            "endpoint": query_endpoint,
        }


def _xai_poll_settings(provider: MediaProvider, extra_override: dict[str, Any] | None) -> tuple[float, float]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    poll_interval = max(
        1.0,
        _coerce_float(
            extra.get("_poll_interval_seconds")
            or os.getenv("DRAMA_VIDEO_POLL_INTERVAL_SECONDS")
            or 5,
            5.0,
        ),
    )
    poll_timeout = max(
        poll_interval,
        _coerce_float(
            extra.get("_poll_timeout_seconds")
            or os.getenv("DRAMA_VIDEO_POLL_TIMEOUT_SECONDS")
            or 1200,
            1200.0,
        ),
    )
    return poll_interval, poll_timeout


def _xai_provider_message(data: dict[str, Any]) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)
    if err:
        return str(err)
    for key in ("message", "reason", "detail"):
        value = data.get(key)
        if value:
            return str(value)
    return "xAI 视频生成任务失败"


def _json_video_task_provider_message(spec: JsonVideoTaskSpec, data: dict[str, Any]) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)
    if err:
        return str(err)
    for path in ("fail_reason", "message", "reason", "detail", "data.error", "data.message"):
        value = _lookup_path(data, path)
        if value:
            return str(value)
    return f"{spec.display_name} 视频生成任务失败"


async def _call_json_video_task(
    spec: JsonVideoTaskSpec,
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": f"{spec.display_name} provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": f"{spec.display_name} provider 缺少 Base URL", "error_kind": "bad_config"}

    payload, image_candidates, payload_meta = await _build_json_video_task_payload(
        spec=spec,
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=reference_images,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_meta or {"error": f"无法构造 {spec.display_name} 请求", "error_kind": "bad_request"}

    endpoint = _json_video_task_endpoint(provider.base_url, spec)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            uploaded_urls: list[str] = []
            if image_candidates and spec.source_image_transport == "upload_url_list":
                for _, ref in image_candidates:
                    uploaded_url, upload_error = await _upload_json_video_task_image(
                        spec,
                        provider,
                        project_id,
                        ref,
                        client,
                    )
                    if upload_error or not uploaded_url:
                        return _with_video_model_doc_hint({
                            "error": upload_error or "参考图上传失败",
                            "error_kind": "bad_request",
                            "endpoint": _json_video_task_upload_endpoint(
                                provider.base_url,
                                spec,
                                _parse_extra(provider),
                            ),
                        })
                    uploaded_urls.append(uploaded_url)
            elif image_candidates and spec.source_image_transport == "public_url_list":
                for _, ref in image_candidates:
                    public_url, url_error = await _image_url_or_data_url_for_ref(
                        project_id,
                        ref,
                        provider,
                        extra_override,
                        default_transport="public_url",
                    )
                    if url_error or not public_url:
                        return _with_video_model_doc_hint({
                            "error": url_error or "参考图无法转换为公网 URL",
                            "error_kind": "bad_request",
                            "endpoint": endpoint,
                        })
                    uploaded_urls.append(public_url)
            elif image_candidates and spec.source_image_transport == "data_url_list":
                for _, ref in image_candidates:
                    data_url, data_error = await _image_url_or_data_url_for_ref(
                        project_id,
                        ref,
                        provider,
                        extra_override,
                        default_transport="data_url",
                    )
                    if data_error or not data_url:
                        return _with_video_model_doc_hint({
                            "error": data_error or f"参考图无法读取或转换为 data URL: {ref}",
                            "error_kind": "bad_request",
                            "endpoint": endpoint,
                        })
                    uploaded_urls.append(data_url)
            elif image_candidates and spec.source_image_transport == "configurable_url_or_data_url_list":
                for _, ref in image_candidates:
                    image_url, image_error = await _image_url_or_data_url_for_ref(
                        project_id,
                        ref,
                        provider,
                        extra_override,
                        default_transport="data_url",
                    )
                    if image_error or not image_url:
                        return _with_video_model_doc_hint({
                            "error": image_error or f"参考图无法转换: {ref}",
                            "error_kind": "bad_request",
                            "endpoint": endpoint,
                        })
                    uploaded_urls.append(image_url)
            elif image_candidates and spec.source_image_transport != "none":
                uploaded_urls = [ref for _, ref in image_candidates]

            payload_fields = (payload_meta or {}).get("payload_fields")
            if not isinstance(payload_fields, dict):
                payload_fields = None
            field_types = (payload_meta or {}).get("field_types")
            if not isinstance(field_types, dict):
                field_types = None
            if uploaded_urls and spec.source_images_field:
                _json_video_task_put(payload, spec, "images", uploaded_urls, payload_fields, field_types)

            created = await client.post(endpoint, json=payload, headers=headers)
            if created.status_code >= 400:
                err = _make_http_error(created.status_code, created.text, endpoint)
                if err.get("error_kind") == "bad_request":
                    _with_video_model_doc_hint(err)
                return err
            create_data, create_error = _response_json(created, endpoint)
            if create_error:
                return create_error
            api_error = _json_video_task_api_error(spec, create_data or {}, endpoint)
            if api_error:
                return api_error
            task_id = _first_path_text(create_data or {}, spec.task_id_paths)
            if not task_id:
                return {
                    "error": f"创建 {spec.display_name} 视频任务响应缺少 task id",
                    "error_kind": "bad_response",
                    "endpoint": endpoint,
                    "raw": create_data,
                }

            query_endpoint = _json_video_task_query_endpoint(provider.base_url, spec, task_id)
            status = str(_lookup_path(create_data, spec.status_path) or "queued").strip().lower()
            queued_result = {
                "ok": True,
                "provider": provider.name,
                "model": _json_video_task_payload_value(payload, spec, "model", payload_fields) or provider.model_name,
                "status": "running" if status in spec.running_statuses else "queued",
                "job_id": task_id,
                "endpoint": endpoint,
                "query_endpoint": query_endpoint,
                "source_image_count": (payload_meta or {}).get("source_image_count", 0),
                "source_image_refs": (payload_meta or {}).get("source_image_refs", []),
                "request": {
                    "duration": _json_video_task_payload_value(payload, spec, "duration", payload_fields),
                    "ratio": _json_video_task_payload_value(payload, spec, "ratio", payload_fields),
                    "resolution": _json_video_task_payload_value(payload, spec, "resolution", payload_fields),
                    "images_count": len(uploaded_urls),
                },
                "raw": create_data,
            }
            if not wait_for_completion and status not in (spec.done_statuses | spec.failed_statuses):
                return queued_result

            return await _poll_json_video_task(
                spec=spec,
                provider=provider,
                project_id=project_id,
                task_id=task_id,
                extra_override=extra_override,
                save_locally=save_locally,
            )
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }


async def _poll_json_video_task(
    spec: JsonVideoTaskSpec,
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": f"{spec.display_name} provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": f"{spec.display_name} provider 缺少 Base URL", "error_kind": "bad_config"}

    query_endpoint = _json_video_task_query_endpoint(provider.base_url, spec, task_id)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    poll_interval, poll_timeout = _xai_poll_settings(provider, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            while True:
                queried = await client.get(query_endpoint, headers=headers)
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, query_endpoint)
                    err.update({"job_id": task_id, "status": status or "unknown"})
                    return err
                query_data, query_error = _response_json(queried, query_endpoint)
                if query_error:
                    query_error.update({"job_id": task_id, "status": status or "unknown"})
                    return query_error
                api_error = _json_video_task_api_error(spec, query_data or {}, query_endpoint)
                if api_error:
                    api_error.update({"job_id": task_id, "status": status or "unknown"})
                    return api_error

                latest_data = query_data or {}
                status = str(_lookup_path(latest_data, spec.status_path) or status or "unknown").strip().lower()
                progress = _lookup_path(latest_data, spec.progress_path) if spec.progress_path else None
                status_group = str(_lookup_path(latest_data, "status_group") or "").strip()
                is_final = _coerce_bool(_lookup_path(latest_data, "is_final"))
                polls.append({
                    "status": status,
                    "progress": progress,
                    "status_group": status_group or None,
                    "is_final": is_final,
                })
                await _notify_progress(progress_callback, {
                    "job_id": task_id,
                    "status": status,
                    "progress": progress,
                    "status_group": status_group or None,
                    "is_final": is_final,
                    "poll_count": len(polls),
                    "provider": provider.name,
                    "model": provider.model_name,
                    "endpoint": query_endpoint,
                })

                remote_url = _first_path_text(latest_data, spec.result_url_paths) or _video_url_from_response(latest_data)
                status_failed = status in spec.failed_statuses or status_group == "失败"
                status_done = status in spec.done_statuses or status_group == "已完成"
                if remote_url and not status_failed:
                    downloaded: dict[str, Any] = {}
                    if save_locally:
                        downloaded = await _download_video_result(project_id, str(remote_url))
                    return {
                        "ok": True,
                        "provider": provider.name,
                        "model": _lookup_path(latest_data, "model") or provider.model_name,
                        "status": "completed",
                        "job_id": task_id,
                        "url": downloaded.get("local_url") or remote_url,
                        "local_url": downloaded.get("local_url"),
                        "local_path": downloaded.get("local_path"),
                        "remote_url": remote_url,
                        "duration": _lookup_path(latest_data, "duration"),
                        "ratio": _lookup_path(latest_data, "ratio"),
                        "resolution": _lookup_path(latest_data, "resolution"),
                        "usage": _lookup_path(latest_data, "usage"),
                        "progress": progress,
                        "polls": polls,
                        "raw": latest_data,
                        "download_error": downloaded.get("download_error"),
                    }

                if status_failed:
                    provider_msg = _json_video_task_provider_message(spec, latest_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "raw": latest_data,
                        "polls": polls,
                    }

                if is_final is True or status_done:
                    provider_msg = _json_video_task_provider_message(spec, latest_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "bad_response",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "raw": latest_data,
                        "polls": polls,
                    }

                if time.monotonic() >= deadline:
                    return {
                        "error": f"{spec.display_name} 视频任务仍在 {status}，已超过本地轮询超时 {int(poll_timeout)} 秒",
                        "error_kind": "timeout",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": task_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "raw": latest_data,
                        "polls": polls,
                    }

                await asyncio.sleep(poll_interval)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": query_endpoint,
        }


async def _call_t8_grok_video_3(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    return await _call_json_video_task(
        spec=_T8_GROK_VIDEO_3_SPEC,
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=reference_images,
        extra_override=extra_override,
        save_locally=save_locally,
        wait_for_completion=wait_for_completion,
    )


async def _poll_t8_grok_video_3_task(
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_json_video_task(
        spec=_T8_GROK_VIDEO_3_SPEC,
        provider=provider,
        project_id=project_id,
        task_id=task_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _call_lingke_media_generate(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    first_frame_url: str | None,
    last_frame_url: str | None,
    duration_seconds: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
    save_locally: bool,
    wait_for_completion: bool = False,
) -> dict[str, Any]:
    return await _call_json_video_task(
        spec=_LINGKE_MEDIA_GENERATE_SPEC,
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration_seconds=duration_seconds,
        reference_images=reference_images,
        extra_override=extra_override,
        save_locally=save_locally,
        wait_for_completion=wait_for_completion,
    )


async def _poll_lingke_media_generate_task(
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_json_video_task(
        spec=_LINGKE_MEDIA_GENERATE_SPEC,
        provider=provider,
        project_id=project_id,
        task_id=task_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _poll_xai_video_task(
    provider: MediaProvider,
    project_id: str,
    request_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "xAI video provider 缺少 API Key", "error_kind": "bad_config"}

    query_endpoint = _xai_video_query_endpoint(provider.base_url, request_id)
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    poll_interval, poll_timeout = _xai_poll_settings(provider, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"

    try:
        async with httpx.AsyncClient(timeout=_media_video_timeout()) as client:
            while True:
                queried = await client.get(query_endpoint, headers=headers)
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, query_endpoint)
                    err.update({"job_id": request_id, "status": status or "unknown"})
                    return err
                query_data, query_error = _response_json(queried, query_endpoint)
                if query_error:
                    query_error.update({"job_id": request_id, "status": status or "unknown"})
                    return query_error

                latest_data = query_data
                status = str(query_data.get("status") or status or "unknown").lower()
                polls.append({
                    "status": status,
                    "progress": query_data.get("progress"),
                })
                await _notify_progress(progress_callback, {
                    "job_id": request_id,
                    "status": status,
                    "progress": query_data.get("progress"),
                    "poll_count": len(polls),
                    "provider": provider.name,
                    "model": query_data.get("model") or provider.model_name,
                    "endpoint": query_endpoint,
                })

                if status in _XAI_DONE_STATUSES:
                    video = query_data.get("video") if isinstance(query_data.get("video"), dict) else {}
                    response = query_data.get("response") if isinstance(query_data.get("response"), dict) else {}
                    remote_url = (
                        video.get("url")
                        or response.get("video_url")
                        or query_data.get("video_url")
                        or query_data.get("url")
                    )
                    if not remote_url:
                        return {
                            "error": "xAI 视频任务成功但响应缺少 video.url",
                            "error_kind": "bad_response",
                            "provider": provider.name,
                            "model": query_data.get("model") or provider.model_name,
                            "job_id": request_id,
                            "status": status,
                            "endpoint": query_endpoint,
                            "raw": query_data,
                        }
                    downloaded: dict[str, Any] = {}
                    if save_locally:
                        downloaded = await _download_video_result(project_id, str(remote_url))
                    return {
                        "ok": True,
                        "provider": provider.name,
                        "model": query_data.get("model") or provider.model_name,
                        "status": "completed",
                        "job_id": request_id,
                        "url": downloaded.get("local_url") or remote_url,
                        "local_url": downloaded.get("local_url"),
                        "local_path": downloaded.get("local_path"),
                        "remote_url": remote_url,
                        "thumbnail_url": video.get("thumbnail_url") or response.get("thumbnail_url"),
                        "duration": video.get("duration") or query_data.get("duration"),
                        "usage": query_data.get("usage"),
                        "progress": query_data.get("progress"),
                        "polls": polls,
                        "download_error": downloaded.get("download_error"),
                    }

                if status in _XAI_FAILED_STATUSES:
                    provider_msg = _xai_provider_message(query_data)
                    return {
                        "error": provider_msg,
                        "error_kind": "provider_failed",
                        "provider": provider.name,
                        "model": query_data.get("model") or provider.model_name,
                        "job_id": request_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "provider_msg": provider_msg,
                        "raw": query_data,
                        "polls": polls,
                    }

                if time.monotonic() >= deadline:
                    return {
                        "error": f"xAI 视频任务仍在 {status}，已超过本地轮询超时 {int(poll_timeout)} 秒",
                        "error_kind": "timeout",
                        "provider": provider.name,
                        "model": provider.model_name,
                        "job_id": request_id,
                        "status": status,
                        "endpoint": query_endpoint,
                        "raw": latest_data,
                        "polls": polls,
                    }

                await asyncio.sleep(poll_interval)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": query_endpoint,
        }


async def _poll_volcengine_ark_video_adapter(
    provider: MediaProvider,
    project_id: str,
    job_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_volcengine_ark_video_task(
        provider=provider,
        project_id=project_id,
        task_id=job_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _poll_grok_1_5_video_adapter(
    provider: MediaProvider,
    project_id: str,
    job_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_grok_1_5_video_task(
        provider=provider,
        project_id=project_id,
        request_id=job_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _poll_xai_video_adapter(
    provider: MediaProvider,
    project_id: str,
    job_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_xai_video_task(
        provider=provider,
        project_id=project_id,
        request_id=job_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _poll_t8_grok_video_3_adapter(
    provider: MediaProvider,
    project_id: str,
    job_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_t8_grok_video_3_task(
        provider=provider,
        project_id=project_id,
        task_id=job_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _poll_lingke_media_generate_adapter(
    provider: MediaProvider,
    project_id: str,
    job_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_lingke_media_generate_task(
        provider=provider,
        project_id=project_id,
        task_id=job_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


async def _poll_video_http_v1_adapter(
    provider: MediaProvider,
    project_id: str,
    job_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    return await _poll_video_http_v1_task(
        provider=provider,
        project_id=project_id,
        task_id=job_id,
        extra_override=extra_override,
        save_locally=save_locally,
        progress_callback=progress_callback,
    )


_VIDEO_PROVIDER_ADAPTERS: tuple[VideoProviderAdapter, ...] = (
    VideoProviderAdapter(
        name="video_http_v1",
        display_name="Declarative Video HTTP v1",
        api_formats=frozenset(_VIDEO_HTTP_V1_FORMATS),
        model_names=frozenset(),
        endpoint_for=_video_http_v1_endpoint,
        generate=_call_video_http_v1,
        poll=_poll_video_http_v1_adapter,
        requires_base_url=True,
        source_images_min=0,
        source_images_max=None,
        field_types={
            "model": "string",
            "content": "array[text_image_video_audio]",
            "duration": "integer",
            "mode": "string",
            "ratio": "string",
            "resolution": "string",
            "media_references": "array[media_ref]",
            "reference_videos": "url_list",
            "reference_audios": "url_list",
        },
        source_image_transport="protocol_configured",
    ),
    VideoProviderAdapter(
        name="lingke_media_generate",
        display_name=_LINGKE_MEDIA_GENERATE_SPEC.display_name,
        api_formats=_LINGKE_MEDIA_GENERATE_SPEC.api_formats,
        model_names=frozenset(),
        endpoint_for=_lingke_media_generate_endpoint,
        generate=_call_lingke_media_generate,
        poll=_poll_lingke_media_generate_adapter,
        requires_base_url=True,
        source_images_min=_LINGKE_MEDIA_GENERATE_SPEC.source_images_min,
        source_images_max=_LINGKE_MEDIA_GENERATE_SPEC.source_images_max,
        field_types=_LINGKE_MEDIA_GENERATE_SPEC.field_types,
        supported_resolutions=_LINGKE_MEDIA_GENERATE_SPEC.supported_resolutions,
        supported_ratios=_LINGKE_MEDIA_GENERATE_SPEC.supported_ratios,
        source_image_transport=_LINGKE_MEDIA_GENERATE_SPEC.source_image_transport,
    ),
    VideoProviderAdapter(
        name="t8_grok_video_3",
        display_name=_T8_GROK_VIDEO_3_SPEC.display_name,
        api_formats=_T8_GROK_VIDEO_3_SPEC.api_formats,
        model_names=_T8_GROK_VIDEO_3_SPEC.model_names,
        endpoint_for=_t8_grok_video_3_endpoint,
        generate=_call_t8_grok_video_3,
        poll=_poll_t8_grok_video_3_adapter,
        requires_base_url=True,
        source_images_min=_T8_GROK_VIDEO_3_SPEC.source_images_min,
        source_images_max=_T8_GROK_VIDEO_3_SPEC.source_images_max,
        field_types=_T8_GROK_VIDEO_3_SPEC.field_types,
        supported_resolutions=_T8_GROK_VIDEO_3_SPEC.supported_resolutions,
        supported_ratios=_T8_GROK_VIDEO_3_SPEC.supported_ratios,
        source_image_transport=_T8_GROK_VIDEO_3_SPEC.source_image_transport,
    ),
    VideoProviderAdapter(
        name="grok_1_5",
        display_name="Grok 1.5 Multipart",
        api_formats=frozenset(_GROK_1_5_VIDEO_FORMATS),
        model_names=frozenset({"grok-1.5-video-15s"}),
        endpoint_for=_grok_1_5_video_endpoint,
        generate=_call_grok_1_5_video,
        poll=_poll_grok_1_5_video_adapter,
        requires_base_url=True,
        source_images_min=1,
        source_images_max=1,
        field_types={
            "model": "multipart_field:string",
            "prompt": "multipart_field:string",
            "size": "multipart_field:string",
            "input_reference": "multipart_file:image",
        },
        supported_resolutions=frozenset(_GROK_1_5_VIDEO_RESOLUTIONS),
        supported_ratios=frozenset({"16:9", "9:16"}),
        source_image_transport="multipart_file",
    ),
    VideoProviderAdapter(
        name="xai_video",
        display_name="xAI Video",
        api_formats=frozenset(_XAI_VIDEO_FORMATS),
        model_names=frozenset({"grok-imagine-video-1.5"}),
        endpoint_for=_xai_video_generations_endpoint,
        generate=_call_xai_video,
        poll=_poll_xai_video_adapter,
        source_images_min=1,
        source_images_max=1,
        field_types={
            "model": "string",
            "prompt": "string",
            "image": "image_url_object",
            "duration": "integer",
            "resolution": "string",
            "seed": "integer",
        },
        supported_resolutions=frozenset(_XAI_VIDEO_RESOLUTIONS),
        supported_ratios=frozenset({"16:9", "9:16"}),
        source_image_transport="json_configurable_url_or_data_url",
    ),
    VideoProviderAdapter(
        name="volcengine_ark",
        display_name="Volcengine Ark",
        api_formats=frozenset(_ARK_VIDEO_FORMATS),
        model_names=frozenset(),
        endpoint_for=_ark_video_tasks_endpoint,
        generate=_call_volcengine_ark_video,
        poll=_poll_volcengine_ark_video_adapter,
        source_images_min=0,
        source_images_max=None,
        field_types={
            "model": "string",
            "content": "array[text_or_image_url]",
            "duration": "integer",
            "ratio": "string",
            "resolution": "string",
            "generate_audio": "boolean",
            "watermark": "boolean",
            "return_last_frame": "boolean",
            "seed": "integer",
        },
        supported_resolutions=frozenset({"480p", "720p", "1080p"}),
        supported_ratios=frozenset(_ARK_RATIOS),
        source_image_transport="json_configurable_url_or_data_url",
    ),
)
_VIDEO_PROVIDER_ADAPTERS_BY_NAME = {adapter.name: adapter for adapter in _VIDEO_PROVIDER_ADAPTERS}


def _supported_video_api_formats() -> list[str]:
    return sorted({fmt for adapter in _VIDEO_PROVIDER_ADAPTERS for fmt in adapter.api_formats})


def _video_provider_adapter(provider: MediaProvider) -> VideoProviderAdapter | None:
    fmt = _normalized_api_format(provider)
    model = str(provider.model_name or "").strip().lower()
    if _video_http_v1_protocol_id_for_provider(provider):
        return _VIDEO_PROVIDER_ADAPTERS_BY_NAME["video_http_v1"]
    for adapter in _VIDEO_PROVIDER_ADAPTERS:
        if fmt in adapter.api_formats or model in adapter.model_names:
            return adapter
    if _is_seedance_model(provider.model_name):
        return _VIDEO_PROVIDER_ADAPTERS_BY_NAME["volcengine_ark"]
    return None


def _video_adapter_capabilities(adapter: VideoProviderAdapter) -> dict[str, Any]:
    capabilities: dict[str, Any] = {
        "source_images_min": adapter.source_images_min,
        "source_images_max": adapter.source_images_max,
        "source_image_transport": adapter.source_image_transport,
        "field_types": adapter.field_types or {},
    }
    if adapter.supported_resolutions:
        capabilities["supported_resolutions"] = sorted(
            adapter.supported_resolutions,
            key=lambda item: _VIDEO_RESOLUTION_ORDER.get(item, 99),
        )
    if adapter.supported_ratios:
        capabilities["supported_ratios"] = sorted(adapter.supported_ratios)
    return capabilities


def _image_http_v1_request_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("request")
    return section if isinstance(section, dict) else {}


def _image_http_v1_poll_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("poll")
    return section if isinstance(section, dict) else {}


def _image_http_v1_result_section(protocol: dict[str, Any]) -> dict[str, Any]:
    section = protocol.get("result")
    return section if isinstance(section, dict) else {}


def _image_http_v1_endpoint_for(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
    *,
    task_id: str | None = None,
) -> str:
    return _video_http_v1_endpoint_for(provider, protocol, section, task_id=task_id)


def _image_http_v1_headers(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, str]:
    return _video_http_v1_headers(provider, protocol, section)


def _image_http_v1_lookup(data: Any, path: str) -> Any:
    current = data
    for part in str(path or "").split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _image_http_v1_first_value(data: Any, paths: Any) -> Any:
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return None
    for path in paths:
        value = _image_http_v1_lookup(data, str(path))
        if value not in (None, "", [], {}):
            return value
    return None


def _image_http_v1_b64_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("data:image/") and "," in text:
        return text.split(",", 1)[1]
    return text


def _image_http_v1_images_from_result(protocol: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    result = _image_http_v1_result_section(protocol)
    images: list[dict[str, Any]] = []
    images_path = result.get("images_path") or result.get("items_path")
    item_url_path = str(result.get("url_path") or "url")
    item_b64_path = str(result.get("b64_path") or "b64_json")
    items = _image_http_v1_lookup(data, str(images_path)) if images_path else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                url = _image_http_v1_lookup(item, item_url_path)
                b64 = _image_http_v1_lookup(item, item_b64_path)
                images.append({
                    "url": str(url).strip() if url else None,
                    "b64": _image_http_v1_b64_value(b64),
                })
            elif isinstance(item, str) and item.strip():
                text = item.strip()
                images.append({
                    "url": text if _is_remote_url(text) else None,
                    "b64": None if _is_remote_url(text) else _image_http_v1_b64_value(text),
                })
    images = [item for item in images if item.get("url") or item.get("b64")]
    if images:
        return images

    url = _image_http_v1_first_value(data, result.get("image_url_paths") or result.get("url_paths"))
    b64 = _image_http_v1_first_value(data, result.get("b64_paths") or result.get("b64_json_paths"))
    if url or b64:
        return [{"url": str(url).strip() if url else None, "b64": _image_http_v1_b64_value(b64)}]
    return []


_IMAGE_HTTP_V1_INTERNAL_EXTRA_KEYS = {
    "image_protocol_id",
    "protocol_id",
    "image_protocol",
    "protocol",
    "image_transport",
    "reference_image_transport",
    "image_input",
    "public_base_url",
    "site_base_url",
    "_endpoint",
    "_response_image_path",
    "_reference_param",
    "_reference_format",
}


def _image_http_v1_payload_extra(extra: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in extra.items():
        if key in _IMAGE_HTTP_V1_INTERNAL_EXTRA_KEYS or str(key).startswith("_"):
            continue
        if value in (None, "", [], {}):
            continue
        clean[str(key)] = value
    return clean


async def _image_http_v1_reference_values(
    provider: MediaProvider,
    project_id: str,
    protocol: dict[str, Any],
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    refs: list[str] = []
    warnings: list[str] = []
    default_transport = str(protocol.get("image_transport") or "data_url")
    for ref in reference_images or []:
        value, warning = await _image_url_or_data_url_for_ref(
            project_id,
            ref,
            provider,
            extra_override,
            default_transport=default_transport,
        )
        if warning:
            warnings.append(warning)
        elif value:
            refs.append(value)
    return refs, warnings


async def _build_image_http_v1_payload(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    negative_prompt: str | None,
    size: str,
    quality: str | None,
    n: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    protocol, protocol_error = _image_http_v1_protocol(provider, extra)
    if protocol_error:
        return None, protocol_error
    assert protocol is not None
    request = _image_http_v1_request_section(protocol)
    reference_values, reference_warnings = await _image_http_v1_reference_values(
        provider,
        project_id,
        protocol,
        reference_images,
        extra,
    )
    if reference_images and not reference_values:
        return None, {
            "error": "所有参考图都无法转换为协议需要的图片输入: " + "; ".join(reference_warnings or ["未知原因"]),
            "error_kind": "bad_request",
        }
    model_name = str(extra.get("model") or getattr(provider, "model_name", "") or "").strip()
    if not model_name:
        return None, {"error": "image_http_v1 provider 缺少 model_name", "error_kind": "bad_config"}
    count = max(1, int(n or 1))
    context = {
        "model": model_name,
        "prompt": str(prompt or "").strip(),
        "negative_prompt": str(negative_prompt or "").strip() or None,
        "size": str(size or "").strip(),
        "quality": str(quality or "").strip() or None,
        "count": count,
        "n": count,
        "response_format": extra.get("response_format") or protocol.get("default_response_format") or "url",
        "reference_images": reference_values,
        "reference_image_urls": reference_values,
        "reference_image_input": reference_values[0] if len(reference_values) == 1 else reference_values or None,
        "first_reference_image": reference_values[0] if reference_values else None,
    }
    body_template = request.get("body")
    if not isinstance(body_template, dict):
        return None, {"error": "image_http_v1 request.body 必须是对象", "error_kind": "bad_config"}
    payload = _video_http_v1_render_value(body_template, context)
    if not isinstance(payload, dict):
        return None, {"error": "image_http_v1 request.body 渲染结果不是对象", "error_kind": "bad_config"}
    if _coerce_bool(request.get("merge_extra")):
        payload = {**payload, **_image_http_v1_payload_extra(extra)}
    return payload, {
        "protocol": protocol,
        "reference_warnings": reference_warnings,
        "request": {"count": count, "size": context["size"], "has_references": bool(reference_values)},
    }


def _image_http_v1_task_id(protocol: dict[str, Any], data: dict[str, Any]) -> str | None:
    request = _image_http_v1_request_section(protocol)
    paths = request.get("task_id_paths") or request.get("id_paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = ["id", "task_id", "taskId", "job_id", "data.id", "data.task_id", "data.taskId", "data.job_id"]
    value = _image_http_v1_first_value(data, paths)
    return str(value).strip() if value else None


def _image_http_v1_status(protocol: dict[str, Any], data: dict[str, Any], fallback: str = "queued") -> str:
    poll = _image_http_v1_poll_section(protocol)
    status_path = str(poll.get("status_path") or "status")
    return str(_image_http_v1_lookup(data, status_path) or data.get("status") or data.get("state") or fallback).strip().lower()


def _image_http_v1_status_sets(protocol: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    poll = _image_http_v1_poll_section(protocol)
    succeeded = _string_set(poll.get("succeeded") or poll.get("done_statuses")) or {"succeeded", "success", "completed", "complete", "done"}
    failed = _string_set(poll.get("failed") or poll.get("failed_statuses")) or {"failed", "failure", "error", "cancelled", "canceled", "expired"}
    running = _string_set(poll.get("running") or poll.get("running_statuses")) or {"queued", "pending", "running", "processing", "in_progress", "submitted", "created"}
    return {item.lower() for item in succeeded}, {item.lower() for item in failed}, {item.lower() for item in running}


async def _poll_image_http_v1_task(
    provider: MediaProvider,
    protocol: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    poll = _image_http_v1_poll_section(protocol)
    if not poll:
        return {"error": "image_http_v1 响应缺少图片且协议未配置 poll", "error_kind": "bad_response", "job_id": task_id}
    endpoint = _image_http_v1_endpoint_for(provider, protocol, poll, task_id=task_id)
    if not endpoint:
        return {"error": "image_http_v1 protocol 缺少 poll.path", "error_kind": "bad_config", "job_id": task_id}
    headers = _image_http_v1_headers(provider, protocol, poll)
    method = str(poll.get("method") or "GET").strip().upper()
    interval = max(1.0, _coerce_float(poll.get("interval_seconds") or 5, 5.0))
    timeout = max(interval, _coerce_float(poll.get("timeout_seconds") or 600, 600.0))
    deadline = time.monotonic() + timeout
    succeeded, failed, _running = _image_http_v1_status_sets(protocol)
    status = "queued"
    polls: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
            while True:
                queried = await client.post(endpoint, json={}, headers=headers) if method == "POST" else await client.get(endpoint, headers=headers)
                if queried.status_code >= 400:
                    err = _make_http_error(queried.status_code, queried.text, endpoint)
                    err.update({"job_id": task_id, "status": status})
                    return err
                data, data_error = _response_json(queried, endpoint)
                if data_error:
                    data_error.update({"job_id": task_id, "status": status})
                    return data_error
                latest = data or {}
                status = _image_http_v1_status(protocol, latest, status)
                polls.append({"status": status, "updated_at": latest.get("updated_at")})
                if status in succeeded:
                    images = _image_http_v1_images_from_result(protocol, latest)
                    if images:
                        return {"images": images, "job_id": task_id, "status": "completed", "polls": polls}
                    return {"error": "image_http_v1 任务成功但响应缺少图片", "error_kind": "bad_response", "raw": latest, "job_id": task_id, "polls": polls}
                if status in failed:
                    return {
                        "error": _video_http_v1_provider_error(protocol, latest)[0],
                        "error_kind": "provider_failed",
                        "raw": latest,
                        "job_id": task_id,
                        "status": status,
                        "polls": polls,
                    }
                if time.monotonic() >= deadline:
                    return {
                        "error": f"图片任务仍在 {status}，已超过本地轮询超时 {int(timeout)} 秒",
                        "error_kind": "timeout",
                        "raw": latest,
                        "job_id": task_id,
                        "status": status,
                        "polls": polls,
                    }
                await asyncio.sleep(interval)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }


async def _call_image_http_v1(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    negative_prompt: str | None,
    size: str,
    quality: str | None,
    n: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
) -> dict[str, Any]:
    if not getattr(provider, "api_key", None):
        return {"error": "image_http_v1 provider 缺少 API Key", "error_kind": "bad_config"}
    protocol, protocol_error = _image_http_v1_protocol(provider, extra_override)
    if protocol_error:
        return protocol_error
    assert protocol is not None
    payload, payload_meta = await _build_image_http_v1_payload(
        provider=provider,
        project_id=project_id,
        prompt=prompt,
        negative_prompt=negative_prompt,
        size=size,
        quality=quality,
        n=n,
        reference_images=reference_images,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_meta or {"error": "无法构造 image_http_v1 请求", "error_kind": "bad_request"}
    request = _image_http_v1_request_section(protocol)
    endpoint = _image_http_v1_endpoint_for(provider, protocol, request)
    if not endpoint:
        return {"error": "image_http_v1 provider 缺少 base_url 或 request.path", "error_kind": "bad_config"}
    headers = _image_http_v1_headers(provider, protocol, request)
    method = str(request.get("method") or "POST").strip().upper()
    try:
        async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
            if method == "GET":
                resp = await client.get(endpoint, params=payload, headers=headers)
            else:
                resp = await client.request(method, endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }
    if resp.status_code >= 400:
        return _make_http_error(resp.status_code, resp.text, endpoint)
    data, data_error = _response_json(resp, endpoint)
    if data_error:
        return data_error
    images = _image_http_v1_images_from_result(protocol, data or {})
    if images:
        result: dict[str, Any] = {"images": images, "endpoint": endpoint}
        if payload_meta and payload_meta.get("reference_warnings"):
            result["reference_warnings"] = payload_meta.get("reference_warnings")
        return result
    task_id = _image_http_v1_task_id(protocol, data or {})
    if task_id:
        polled = await _poll_image_http_v1_task(provider, protocol, task_id)
        if payload_meta and payload_meta.get("reference_warnings"):
            polled["reference_warnings"] = payload_meta.get("reference_warnings")
        return polled
    return {
        "error": "响应中没有图片数据",
        "error_kind": "empty_response",
        "raw": data,
        "endpoint": endpoint,
    }


async def _call_raw_http(
    provider: MediaProvider,
    prompt: str,
    negative_prompt: str | None,
    size: str,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
) -> dict[str, Any]:
    extra = _parse_extra(provider)
    extra.update(extra_override)

    response_path: list = extra.pop("_response_image_path", ["images", 0, "url"])
    endpoint: str = extra.pop("_endpoint", provider.base_url.rstrip("/"))
    ref_param = extra.pop("_reference_param", "image")
    ref_format = extra.pop("_reference_format", "url")

    payload: dict[str, Any] = {
        "prompt": prompt,
        "size": size,
        **extra,
    }
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt

    if reference_images:
        if ref_format == "data_url":
            data_urls: list[str] = []
            for ref in reference_images:
                du = await _ref_to_data_url(ref)
                if du:
                    data_urls.append(du)
            if not data_urls:
                return {
                    "error": "所有参考图都无法读取（远程下载失败或本地路径不存在）",
                    "error_kind": "bad_request",
                    "endpoint": endpoint,
                }
            payload[ref_param] = data_urls if len(data_urls) > 1 else data_urls[0]
        else:
            payload[ref_param] = reference_images if len(reference_images) > 1 else reference_images[0]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }

    if resp.status_code != 200:
        return _make_http_error(resp.status_code, resp.text, endpoint)

    raw_data: Any
    try:
        raw_data = resp.json()
    except ValueError:
        return {
            "error": f"响应不是 JSON: {resp.text[:400]}",
            "error_kind": "bad_response",
            "endpoint": endpoint,
        }

    data_dict: dict[str, Any] | None = raw_data if isinstance(raw_data, dict) else None
    if data_dict is None:
        fallback = _extract_image_candidate_from_raw_response(raw_data)
        if fallback is not None:
            return {"images": [fallback]}
        return {
            "error": "响应 JSON 不是对象且未解析到图片",
            "error_kind": "bad_response",
            "raw": raw_data,
            "endpoint": endpoint,
        }

    if not isinstance(response_path, list):
        response_path = [response_path]

    val: Any = data_dict
    try:
        for key in response_path:
            if isinstance(val, list):
                idx = int(key) if not isinstance(key, int) else key
                val = val[idx]
            else:
                val = val[key]
    except (KeyError, IndexError, TypeError, ValueError):
        fallback = _extract_image_candidate_from_raw_response(data_dict)
        if fallback is not None:
            return {"images": [fallback]}
        return {
            "error": f"无法按路径 {response_path} 解析响应",
            "error_kind": "bad_response",
            "raw": data_dict,
            "endpoint": endpoint,
        }
    if isinstance(val, str):
        candidate = _extract_image_candidate_from_raw_response(val)
        if candidate is not None:
            return {"images": [candidate]}
        if _is_remote_url(val):
            return {"images": [{"url": val, "b64": None}]}
    elif isinstance(val, list):
        candidate = _extract_image_candidate_from_raw_response(val)
        if candidate is not None:
            return {"images": [candidate]}
    elif isinstance(val, dict):
        candidate = _extract_image_candidate_from_raw_response(val)
        if candidate is not None:
            return {"images": [candidate]}

    return {
        "error": "响应里未取到图片 URL",
        "error_kind": "bad_response",
        "raw": data_dict,
        "endpoint": endpoint,
    }


# Image provider calls are single-shot. The model must repair the original node
# after a failed call; backend code must not silently lower resolution or quality.
def _downgrade_size(current: str) -> str | None:
    """Compatibility hook: automatic resolution downgrade is disabled."""
    return None


def _is_retryable_error(error_kind: str | None, http_code: int | None) -> bool:
    """Compatibility hook: provider image calls do not auto-retry."""
    return False


# ---- provider preset params ----


def _image_protocol_presets_from_catalog() -> dict[str, dict[str, Any]]:
    paths = _image_http_v1_protocol_catalog_paths()
    if not paths:
        return {}
    protocols, error = _image_http_v1_load_protocol_catalog(paths[0])
    if error or not protocols:
        return {}
    presets: dict[str, dict[str, Any]] = {}
    for protocol in protocols.values():
        protocol_defaults = protocol.get("default_params") if isinstance(protocol.get("default_params"), dict) else {}
        profiles = protocol.get("model_profiles") or protocol.get("models") or []
        if isinstance(profiles, list):
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                model = str(profile.get("match") or profile.get("model") or "").strip()
                if not model:
                    continue
                profile_defaults = profile.get("default_params") if isinstance(profile.get("default_params"), dict) else {}
                presets[model] = {**protocol_defaults, **profile_defaults}
        if protocol_defaults and "*" not in presets:
            presets["*"] = dict(protocol_defaults)
    return presets

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

    Presets come from config/image_provider_protocols/catalog.json.
    """
    presets = _image_protocol_presets_from_catalog()
    name_lower = model_name.lower().replace("_", "-").replace(" ", "-")
    for key in sorted(presets.keys(), key=lambda k: -len(k)):
        if key == "*":
            continue
        if key in name_lower:
            return dict(presets[key])
    return dict(presets.get("*", {}))


def list_presets() -> dict[str, dict[str, Any]]:
    """Return image provider presets declared by the image protocol catalog."""
    return _image_protocol_presets_from_catalog()


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
        if not resolved_refs:
            # 业务约束:出图必须有可用参考图,不允许无参考图降级。
            return {
                "ok": False,
                "provider": provider.name,
                "model": provider.model_name,
                "error": "参考图全部无法解析: " + "; ".join(ref_errors or ["未知原因"]),
                "error_kind": "bad_request",
                "reference_warnings": ref_errors,
            }

    attempts: list[dict[str, Any]] = []
    last_attempt_size = size
    last_attempt_quality = quality

    async def _one_call(_size: str, _quality: str | None) -> dict[str, Any]:
        if provider.api_format == "universal_adapter":
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
        if _image_http_v1_protocol_id_for_provider(provider):
            return await _call_image_http_v1(
                provider=provider,
                project_id=project_id,
                prompt=prompt,
                negative_prompt=negative_prompt,
                size=_size,
                quality=_quality,
                n=n,
                reference_images=resolved_refs or None,
                extra_override=extra_override,
            )
        if n <= 1:
            return await _call_raw_http(
                provider, prompt, negative_prompt, _size,
                resolved_refs or None, extra_override,
            )
        collected: list[dict] = []
        last_err: dict[str, Any] | None = None
        for _ in range(n):
            one = await _call_raw_http(
                provider, prompt, negative_prompt, _size,
                resolved_refs or None, extra_override,
            )
            if "error" in one:
                last_err = one
                break
            collected.extend(one.get("images", []))
        if last_err and not collected:
            return last_err
        if last_err:
            return {"images": collected, "partial_error": last_err.get("error")}
        return {"images": collected}

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
    if provider.api_format == "universal_adapter":
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
    elif _audio_http_v1_protocol_id_for_provider(provider, extra_override):
        result = await _call_audio_http_v1(
            provider=provider,
            project_id=project_id,
            prompt=prompt,
            title=title,
            style=style,
            instrumental=instrumental,
            extra_override=extra_override,
            save_locally=save_locally,
            wait_for_completion=wait_for_completion,
        )
    else:
        result = {
            "error": (
                f"Unsupported audio provider api_format: {provider.api_format}. "
                "Use api_format='audio_http_v1' with params.audio_protocol_id from config/audio_provider_protocols/catalog.json."
            ),
            "error_kind": "unsupported_provider",
            "status": "failed",
        }

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

    if provider.api_format == "universal_adapter":
        from app.services.universal_adapter_service import universal_adapter_service

        result = await universal_adapter_service.poll(
            provider=provider,
            job_id=job_id,
            kind="audio",
            progress_callback=progress_callback,
        )
    elif _audio_http_v1_protocol_id_for_provider(provider, extra or {}):
        result = await _poll_audio_http_v1_task(
            provider=provider,
            project_id=project_id,
            task_id=job_id,
            extra_override=extra or {},
            save_locally=save_locally,
            progress_callback=progress_callback,
        )
    else:
        result = {
            "error": (
                f"Unsupported audio provider api_format: {provider.api_format}. "
                "Use api_format='audio_http_v1' with params.audio_protocol_id from config/audio_provider_protocols/catalog.json."
            ),
            "error_kind": "unsupported_provider",
            "status": "failed",
            "job_id": job_id,
        }

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

    if provider.kind == "image":
        result = await generate_image_with_provider(
            project_id="test",
            prompt="a simple white circle on black background, minimal",
            size="256x256",
            model_name=provider.name,
            n=1,
            save_locally=False,
        )
        return {
            "ok": result.get("ok", False),
            "provider": provider.name,
            "model": provider.model_name,
            "error": result.get("error"),
            "sample_url": (result.get("images") or [{}])[0].get("url") if result.get("ok") else None,
        }

    if provider.kind == "video":
        adapter = _video_provider_adapter(provider)
        if adapter:
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            if adapter.requires_base_url and not provider.base_url:
                missing.append("base_url")
            endpoint = adapter.endpoint_for(provider.base_url) if provider.base_url or not adapter.requires_base_url else ""
            if adapter.name == "video_http_v1":
                protocol, protocol_error = _video_http_v1_protocol(provider)
                if protocol_error:
                    missing.append("params.video_protocol_id")
                elif protocol:
                    endpoint = _video_http_v1_endpoint_for(
                        provider,
                        protocol,
                        _video_http_v1_request_section(protocol),
                    )
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": adapter.name,
                "adapter_display_name": adapter.display_name,
                "endpoint": endpoint,
                "check": "configuration_only",
                "capabilities": _video_adapter_capabilities(adapter),
                "error": f"缺少配置: {', '.join(missing)}" if missing else None,
            }
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "error": f"Unsupported video provider api_format: {provider.api_format}",
            "supported_api_formats": _supported_video_api_formats(),
        }

    if provider.kind == "audio":
        protocol_id = _audio_http_v1_protocol_id_for_provider(provider)
        if protocol_id:
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            if not provider.base_url:
                missing.append("base_url")
            protocol, protocol_error = _audio_http_v1_protocol(provider)
            endpoint = ""
            result_type = ""
            if protocol_error:
                missing.append("params.audio_protocol_id")
            elif protocol:
                endpoint = _audio_http_v1_endpoint_for(
                    provider,
                    protocol,
                    _audio_http_v1_request_section(protocol),
                )
                result = _audio_http_v1_result_section(protocol)
                result_type = str(result.get("type") or result.get("response_type") or "")
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": "audio_http_v1",
                "protocol_id": protocol_id,
                "endpoint": endpoint,
                "result_type": result_type,
                "check": "configuration_only",
                "error": f"缺少配置: {', '.join(missing)}" if missing else None,
            }
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "error": f"Unsupported audio provider api_format: {provider.api_format}",
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
        if not resolved_refs:
            return {
                "ok": False,
                "provider": provider.name,
                "model": provider.model_name,
                "status": "failed",
                "error": "视频参考图全部无法解析: " + "; ".join(ref_errors or ["未知原因"]),
                "error_kind": "bad_request",
                "reference_images": list(reference_images),
                "resolved_reference_images": [],
                "reference_warnings": ref_errors,
            }

    extra_override = extra or {}
    if provider.api_format == "universal_adapter":
        from app.services.universal_adapter_service import universal_adapter_service

        result = await universal_adapter_service.submit_video(
            provider=provider,
            provider_params=_parse_extra(provider),
            project_id=project_id,
            prompt=prompt,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            duration_seconds=duration_seconds,
            reference_images=resolved_refs or None,
            extra=extra_override,
            save_locally=save_locally,
            wait_for_completion=wait_for_completion,
        )
    else:
        adapter = _video_provider_adapter(provider)
        if adapter:
            result = await adapter.generate(
                provider=provider,
                project_id=project_id,
                prompt=prompt,
                first_frame_url=first_frame_url,
                last_frame_url=last_frame_url,
                duration_seconds=duration_seconds,
                reference_images=resolved_refs or None,
                extra_override=extra_override,
                save_locally=save_locally,
                wait_for_completion=wait_for_completion,
            )
        else:
            result = _with_video_model_doc_hint({
                "error": (
                    f"Unsupported video provider api_format: {provider.api_format}. "
                    f"Supported video api_format values: {', '.join(_supported_video_api_formats())}."
                ),
                "error_kind": "unsupported_provider",
                "status": "failed",
                "supported_api_formats": _supported_video_api_formats(),
            })

    ok = bool(result.get("ok"))
    warnings = [
        *ref_errors,
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

    if provider.api_format == "universal_adapter":
        from app.services.universal_adapter_service import universal_adapter_service

        result = await universal_adapter_service.poll(
            provider=provider,
            job_id=job_id,
            kind="video",
            progress_callback=progress_callback,
        )
    else:
        adapter = _video_provider_adapter(provider)
        if adapter:
            result = await adapter.poll(
                provider=provider,
                project_id=project_id,
                job_id=job_id,
                extra_override=extra or {},
                save_locally=save_locally,
                progress_callback=progress_callback,
            )
        else:
            result = _with_video_model_doc_hint({
                "error": (
                    f"Unsupported video provider api_format: {provider.api_format}. "
                    f"Supported video api_format values: {', '.join(_supported_video_api_formats())}."
                ),
                "error_kind": "unsupported_provider",
                "status": "failed",
                "job_id": job_id,
                "supported_api_formats": _supported_video_api_formats(),
            })

    ok = bool(result.get("ok"))
    return {
        **result,
        "ok": ok,
        "provider": result.get("provider") or provider.name,
        "model": result.get("model") or provider.model_name,
        "status": result.get("status") or ("completed" if ok else "failed"),
        "job_id": result.get("job_id") or job_id,
    }
