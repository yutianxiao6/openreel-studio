"""Media provider abstraction layer.

Supports OpenAI-compatible image generation APIs (standard /v1/images/generations
endpoint), a generic raw HTTP POST image format, and adapter branches for video
providers whose task and parameter contracts differ by model family.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from sqlmodel import select

from app.config import settings
from app.db.models import Asset, MediaProvider, WorkflowNode
from app.db.session import session_scope


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
        )
        return result.first()


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
    if provider.params_json:
        try:
            return json.loads(provider.params_json)
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
    storage_root = Path(getattr(settings, "STORAGE_PATH", "./storage")).resolve()

    for raw in refs:
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"参考图引用为空: {raw!r}")
            continue
        ref = raw.strip()

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


def _openai_images_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/images/generations"):
        return base
    if base.endswith("/v1"):
        return base + "/images/generations"
    return base + "/v1/images/generations"


def _openai_tts_endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/audio/speech"):
        return base
    if base.endswith("/v1"):
        return base + "/audio/speech"
    return base + "/v1/audio/speech"


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
_XAI_VIDEO_FORMATS = {"xai_video"}
_GROK_1_5_VIDEO_FORMATS = {"grok_1_5"}
_XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"
_XAI_DONE_STATUSES = {"done", "completed", "succeeded"}
_XAI_FAILED_STATUSES = {"failed", "expired", "cancelled", "canceled"}
_XAI_RUNNING_STATUSES = {"running", "processing", "in_progress"}
_XAI_VIDEO_RESOLUTIONS = {"480p", "720p"}
_GROK_1_5_VIDEO_RESOLUTIONS = {"480p", "720p"}
_VIDEO_RESOLUTION_ORDER = {"480p": 0, "720p": 1, "1080p": 2, "2k": 3, "4k": 4}
_VIDEO_MODEL_CALLING_DOC = "apps/api/app/skills/video_production/VIDEO_MODEL_CALLING.md"
_SUNO_COMPATIBLE_FORMATS = {"suno_compatible", "suno", "suno_api"}
_OPENAI_TTS_FORMATS = {"openai_tts", "tts", "openai_speech", "openai_audio_speech"}
_SUNO_DONE_STATUSES = {"success", "succeeded", "completed", "complete", "done"}
_SUNO_FAILED_STATUSES = {
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "expired",
    "create_task_failed",
    "generate_audio_failed",
    "submit_failed",
}
_SUNO_RUNNING_STATUSES = {
    "pending",
    "queued",
    "running",
    "processing",
    "in_progress",
    "submitted",
    "created",
    "text_success",
    "first_success",
}


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
    return {"480p", "720p", "1080p"}


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


def _is_suno_compatible_audio_provider(provider: MediaProvider) -> bool:
    fmt = _normalized_api_format(provider)
    return fmt in _SUNO_COMPATIBLE_FORMATS


def _is_openai_tts_audio_provider(provider: MediaProvider) -> bool:
    fmt = _normalized_api_format(provider)
    return fmt in _OPENAI_TTS_FORMATS


def _ark_video_tasks_endpoint(base_url: str | None) -> str:
    base = str(base_url or _ARK_DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = _ARK_DEFAULT_BASE_URL
    if base.endswith("/contents/generations/tasks"):
        return base
    if base.endswith("/api/v3"):
        return base + "/contents/generations/tasks"
    if "ark.cn-beijing.volces.com" in base:
        return base + "/api/v3/contents/generations/tasks"
    return base + "/contents/generations/tasks"


def _xai_video_api_base(base_url: str | None) -> str:
    base = str(base_url or _XAI_DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = _XAI_DEFAULT_BASE_URL
    if base.endswith("/videos/generations"):
        return base[: -len("/videos/generations")]
    if base.endswith("/videos"):
        return base[: -len("/videos")]
    if base.endswith("/v1"):
        return base
    if "api.x.ai" in base:
        return base + "/v1"
    return base


def _xai_video_generations_endpoint(base_url: str | None) -> str:
    return _xai_video_api_base(base_url) + "/videos/generations"


def _xai_video_query_endpoint(base_url: str | None, request_id: str) -> str:
    return f"{_xai_video_api_base(base_url)}/videos/{request_id}"


def _grok_1_5_video_api_base(base_url: str | None) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/videos"):
        return base[: -len("/videos")]
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def _grok_1_5_video_endpoint(base_url: str | None) -> str:
    return _grok_1_5_video_api_base(base_url) + "/videos"


def _grok_1_5_video_query_endpoint(base_url: str | None, request_id: str) -> str:
    return f"{_grok_1_5_video_api_base(base_url)}/videos/{request_id}"


def _suno_generate_endpoint(base_url: str | None) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/generate"):
        return base
    if base.endswith("/api/v1"):
        return base + "/generate"
    return base + "/api/v1/generate"


def _suno_record_info_endpoint(base_url: str | None, task_id: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/generate"):
        api_base = base[: -len("/generate")]
    elif base.endswith("/api/v1"):
        api_base = base
    else:
        api_base = base + "/api/v1"
    return f"{api_base}/generate/record-info?{urlencode({'taskId': task_id})}"


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
    if text in {"480p", "720p", "1080p"}:
        return text
    if "x" not in text:
        return None
    left, right = text.split("x", 1)
    width = _coerce_int(left)
    height = _coerce_int(right)
    if not width or not height:
        return None
    short_side = min(width, height)
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


def _ark_image_role(value: Any) -> str:
    role = str(value or "reference_image").strip()
    return role if role in _ARK_IMAGE_ROLES else "reference_image"


async def _ark_image_url(project_id: str, ref: str | None) -> tuple[str | None, str | None]:
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
    return None, f"图片引用无法读取或转换: {text}"


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
        url, warning = await _ark_image_url(project_id, first_frame_url)
        if warning:
            reference_warnings.append(warning)
        elif url:
            content.append({"type": "image_url", "image_url": {"url": url}, "role": "first_frame"})

    roles = extra.get("reference_image_roles")
    role_list = roles if isinstance(roles, list) else []
    for idx, ref in enumerate(reference_images or []):
        url, warning = await _ark_image_url(project_id, ref)
        if warning:
            reference_warnings.append(warning)
            continue
        if url:
            role = _ark_image_role(role_list[idx] if idx < len(role_list) else "reference_image")
            content.append({"type": "image_url", "image_url": {"url": url}, "role": role})

    if last_frame_url:
        url, warning = await _ark_image_url(project_id, last_frame_url)
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


async def _xai_image_input(project_id: str, ref: str | None) -> tuple[dict[str, str] | None, str | None]:
    text = str(ref or "").strip()
    if not text:
        return None, "图片引用为空"
    if text.startswith("data:image/"):
        return {"url": text}, None
    if _is_remote_url(text):
        return {"url": text}, None
    local_ref = _project_media_path_from_url(project_id, text) or text
    data_url = await _ref_to_data_url(local_ref)
    if data_url:
        return {"url": data_url}, None
    return None, f"图片引用无法读取或转换: {text}"


async def _image_file_input(project_id: str, ref: str | None) -> tuple[tuple[str, bytes, str] | None, str | None]:
    text = str(ref or "").strip()
    if not text:
        return None, "图片引用为空"
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
        data.get("video_url"),
        data.get("url"),
    ]
    data_list = data.get("data")
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
    image, image_error = await _xai_image_input(project_id, source_ref)
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


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _suno_payload_data(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("data")
    return nested if isinstance(nested, dict) else data


def _suno_response_success(data: dict[str, Any]) -> bool:
    code = data.get("code")
    if code is None:
        return True
    return str(code).strip().lower() in {"0", "200", "success"}


def _extract_suno_task_id(data: dict[str, Any]) -> str | None:
    payload = _suno_payload_data(data)
    return _first_text(
        payload.get("taskId"),
        payload.get("task_id"),
        payload.get("id"),
        payload.get("job_id"),
        data.get("taskId"),
        data.get("task_id"),
        data.get("id"),
        data.get("job_id"),
    )


def _suno_status(data: dict[str, Any], fallback: str = "queued") -> str:
    payload = _suno_payload_data(data)
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    status = _first_text(
        payload.get("status"),
        payload.get("state"),
        payload.get("taskStatus"),
        payload.get("task_status"),
        response.get("status") if isinstance(response, dict) else None,
        data.get("status"),
        data.get("state"),
    )
    return str(status or fallback).strip().lower()


def _suno_provider_message(data: dict[str, Any]) -> str:
    payload = _suno_payload_data(data)
    err = payload.get("error") or data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)
    if err:
        return str(err)
    for obj in (payload, data):
        for key in ("message", "msg", "reason", "detail"):
            value = obj.get(key) if isinstance(obj, dict) else None
            if value:
                return str(value)
    return "音频生成任务失败"


def _audio_url_from_item(item: dict[str, Any]) -> str | None:
    return _first_text(
        item.get("audioUrl"),
        item.get("audio_url"),
        item.get("sourceAudioUrl"),
        item.get("source_audio_url"),
        item.get("streamAudioUrl"),
        item.get("stream_audio_url"),
        item.get("url"),
        item.get("mp3_url"),
        item.get("wav_url"),
    )


def _normalize_suno_audio_item(item: dict[str, Any]) -> dict[str, Any] | None:
    remote_url = _audio_url_from_item(item)
    if not remote_url:
        audio = item.get("audio") if isinstance(item.get("audio"), dict) else {}
        remote_url = _audio_url_from_item(audio)
    if not remote_url:
        return None
    return {
        "id": item.get("id") or item.get("audioId") or item.get("audio_id"),
        "title": item.get("title") or item.get("name"),
        "url": remote_url,
        "remote_url": remote_url,
        "source_audio_url": item.get("sourceAudioUrl") or item.get("source_audio_url"),
        "stream_audio_url": item.get("streamAudioUrl") or item.get("stream_audio_url"),
        "image_url": item.get("imageUrl") or item.get("image_url") or item.get("coverUrl") or item.get("cover_url"),
        "duration_seconds": item.get("duration") or item.get("duration_seconds"),
        "tags": item.get("tags"),
    }


def _collect_suno_audio_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_collect_suno_audio_items(item))
        return out
    if not isinstance(value, dict):
        return []

    current = _normalize_suno_audio_item(value)
    if current:
        return [current]

    for path in (
        ("data",),
        ("response", "sunoData"),
        ("response", "suno_data"),
        ("sunoData",),
        ("suno_data",),
        ("songs",),
        ("audios",),
        ("audio",),
        ("items",),
        ("records",),
        ("result",),
        ("results",),
    ):
        nested: Any = value
        for key in path:
            if not isinstance(nested, dict):
                nested = None
                break
            nested = nested.get(key)
        if nested is not None and nested is not value:
            found = _collect_suno_audio_items(nested)
            if found:
                return found
    return []


async def _localize_suno_audio_items(project_id: str, items: list[dict[str, Any]], save_locally: bool) -> list[dict[str, Any]]:
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


def _build_suno_audio_payload(
    provider: MediaProvider,
    prompt: str,
    title: str | None,
    style: str | None,
    instrumental: bool | None,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})

    model_name = str(extra.pop("model", None) or provider.model_name or "").strip()
    if not model_name:
        return None, {"error": "Audio provider 缺少 model_name", "error_kind": "bad_config"}

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        return None, {"error": "Suno-compatible audio 请求缺少 prompt", "error_kind": "bad_request"}

    custom_mode = _coerce_bool(extra.pop("customMode", extra.pop("custom_mode", None)))
    if custom_mode is None:
        custom_mode = False
    if instrumental is None:
        instrumental = _coerce_bool(extra.pop("instrumental", None))

    payload: dict[str, Any] = {
        "prompt": clean_prompt,
        "customMode": custom_mode,
        "model": model_name,
    }
    if instrumental is not None:
        payload["instrumental"] = instrumental
    clean_title = str(title or extra.pop("title", "") or "").strip()
    if clean_title:
        payload["title"] = clean_title
    clean_style = str(style or extra.pop("style", "") or "").strip()
    if clean_style:
        payload["style"] = clean_style

    negative_tags = extra.pop("negativeTags", extra.pop("negative_tags", None))
    if negative_tags:
        payload["negativeTags"] = negative_tags
    callback_url = extra.pop("callBackUrl", extra.pop("callback_url", None))
    if callback_url:
        payload["callBackUrl"] = callback_url

    for key in ("voice", "speed", "instructions", "response_format", "format", "audio_format"):
        extra.pop(key, None)
    for key in list(extra.keys()):
        if key.startswith("_"):
            extra.pop(key, None)
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload, None


def _build_openai_tts_payload(
    provider: MediaProvider,
    prompt: str,
    extra_override: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    override = dict(extra_override or {})
    extra = _parse_extra(provider)
    extra.update(override)

    model_name = str(extra.pop("model", None) or provider.model_name or "").strip()
    if not model_name:
        return None, {"error": "TTS provider 缺少 model_name", "error_kind": "bad_config"}
    clean_input = str(extra.pop("input", None) or extra.pop("text", None) or prompt or "").strip()
    if not clean_input:
        return None, {"error": "OpenAI-compatible TTS 请求缺少 input 文本", "error_kind": "bad_request"}

    voice = str(extra.pop("voice", None) or "").strip() or "alloy"
    payload: dict[str, Any] = {
        "model": model_name,
        "input": clean_input,
        "voice": voice,
    }

    response_format = str(
        override.get("response_format")
        or override.get("format")
        or override.get("audio_format")
        or extra.pop("response_format", None)
        or extra.pop("format", None)
        or extra.pop("audio_format", None)
        or ""
    ).strip().lower()
    for key in ("response_format", "format", "audio_format"):
        extra.pop(key, None)
    if response_format:
        payload["response_format"] = response_format

    speed = extra.pop("speed", None)
    if speed not in (None, ""):
        try:
            payload["speed"] = float(str(speed))
        except (TypeError, ValueError):
            return None, {"error": f"TTS speed 必须是数字，收到: {speed!r}", "error_kind": "bad_request"}

    instructions = str(extra.pop("instructions", None) or extra.pop("style", None) or "").strip()
    if instructions:
        payload["instructions"] = instructions

    for key in (
        "title",
        "duration",
        "duration_seconds",
        "instrumental",
        "customMode",
        "custom_mode",
        "negativeTags",
        "negative_tags",
        "callBackUrl",
        "callback_url",
        "personaId",
        "persona_id",
        "vocalGender",
        "vocal_gender",
        "styleWeight",
        "style_weight",
        "weirdness",
        "audioWeight",
        "audio_weight",
        "seed",
    ):
        extra.pop(key, None)
    for key in list(extra.keys()):
        if key.startswith("_"):
            extra.pop(key, None)
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload, None


def _suno_poll_settings(provider: MediaProvider, extra_override: dict[str, Any] | None) -> tuple[float, float]:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    poll_interval = max(
        1.0,
        _coerce_float(
            extra.get("_poll_interval_seconds")
            or os.getenv("DRAMA_AUDIO_POLL_INTERVAL_SECONDS")
            or 8,
            8.0,
        ),
    )
    poll_timeout = max(
        poll_interval,
        _coerce_float(
            extra.get("_poll_timeout_seconds")
            or os.getenv("DRAMA_AUDIO_POLL_TIMEOUT_SECONDS")
            or 1200,
            1200.0,
        ),
    )
    return poll_interval, poll_timeout


async def _call_suno_compatible_audio(
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
        return {"error": "Suno-compatible audio provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": "Suno-compatible audio provider 缺少 Base URL", "error_kind": "bad_config"}

    payload, payload_error = _build_suno_audio_payload(
        provider=provider,
        prompt=prompt,
        title=title,
        style=style,
        instrumental=instrumental,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_error or {"error": "无法构造 Suno-compatible audio 请求", "error_kind": "bad_request"}

    endpoint = _suno_generate_endpoint(provider.base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            created = await client.post(endpoint, json=payload, headers=headers)
            if created.status_code >= 400:
                return _make_http_error(created.status_code, created.text, endpoint)
            create_data, create_error = _response_json(created, endpoint)
            if create_error:
                return create_error
            if create_data is None:
                return {
                    "error": "创建音频任务响应为空",
                    "error_kind": "bad_response",
                    "endpoint": endpoint,
                }
            if not _suno_response_success(create_data):
                return {
                    "error": _suno_provider_message(create_data),
                    "error_kind": "provider_failed",
                    "endpoint": endpoint,
                    "provider_msg": _suno_provider_message(create_data),
                    "raw": create_data,
                }

            immediate_items = _collect_suno_audio_items(create_data)
            if immediate_items:
                audios = await _localize_suno_audio_items(project_id, immediate_items, save_locally)
                primary = audios[0] if audios else {}
                return {
                    "ok": True,
                    "provider": provider.name,
                    "model": payload.get("model") or provider.model_name,
                    "status": "completed",
                    "job_id": _extract_suno_task_id(create_data),
                    "url": primary.get("url"),
                    "local_url": primary.get("local_url"),
                    "local_path": primary.get("local_path"),
                    "remote_url": primary.get("remote_url"),
                    "stream_audio_url": primary.get("stream_audio_url"),
                    "source_audio_url": primary.get("source_audio_url"),
                    "image_url": primary.get("image_url"),
                    "duration": primary.get("duration_seconds"),
                    "mime_type": primary.get("mime_type"),
                    "audios": audios,
                    "endpoint": endpoint,
                    "download_error": primary.get("download_error"),
                }

            task_id = _extract_suno_task_id(create_data)
            if not task_id:
                return {
                    "error": "创建音频任务响应缺少 taskId",
                    "error_kind": "bad_response",
                    "endpoint": endpoint,
                    "raw": create_data,
                }

            status = _suno_status(create_data)
            queued_result = {
                "ok": True,
                "provider": provider.name,
                "model": payload.get("model") or provider.model_name,
                "status": "running" if status in _SUNO_RUNNING_STATUSES else "queued",
                "job_id": task_id,
                "endpoint": endpoint,
                "query_endpoint": _suno_record_info_endpoint(provider.base_url, task_id),
                "request": {
                    "customMode": payload.get("customMode"),
                    "instrumental": payload.get("instrumental"),
                    "has_style": bool(payload.get("style")),
                    "has_title": bool(payload.get("title")),
                },
            }
            if not wait_for_completion and status not in (_SUNO_DONE_STATUSES | _SUNO_FAILED_STATUSES):
                return queued_result

            return await _poll_suno_compatible_audio_task(
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


async def _call_openai_tts_audio(
    provider: MediaProvider,
    project_id: str,
    prompt: str,
    extra_override: dict[str, Any],
    save_locally: bool,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "OpenAI-compatible TTS provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": "OpenAI-compatible TTS provider 缺少 Base URL", "error_kind": "bad_config"}

    payload, payload_error = _build_openai_tts_payload(
        provider=provider,
        prompt=prompt,
        extra_override=extra_override,
    )
    if payload is None:
        return payload_error or {"error": "无法构造 OpenAI-compatible TTS 请求", "error_kind": "bad_request"}

    endpoint = _openai_tts_endpoint(provider.base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": endpoint,
        }

    if resp.status_code >= 400:
        return _make_http_error(resp.status_code, resp.text, endpoint)

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        data, parse_error = _response_json(resp, endpoint)
        if parse_error:
            return parse_error
        return {
            "error": "TTS 响应是 JSON，不是音频二进制",
            "error_kind": "bad_response",
            "endpoint": endpoint,
            "raw": data,
        }

    if not resp.content:
        return {
            "error": "TTS 响应为空",
            "error_kind": "empty_response",
            "endpoint": endpoint,
        }

    saved: dict[str, Any] = {}
    if save_locally:
        saved = await _save_audio_bytes(
            project_id,
            resp.content,
            response_format=payload.get("response_format"),
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
        "format": payload.get("response_format"),
        "endpoint": endpoint,
        "audios": [
            {
                "n_index": 0,
                "url": saved.get("local_url"),
                "local_url": saved.get("local_url"),
                "local_path": saved.get("local_path"),
                "mime_type": saved.get("mime_type"),
            }
        ] if saved.get("local_url") else [],
    }


async def _poll_suno_compatible_audio_task(
    provider: MediaProvider,
    project_id: str,
    task_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
) -> dict[str, Any]:
    if not provider.api_key:
        return {"error": "Suno-compatible audio provider 缺少 API Key", "error_kind": "bad_config"}
    if not str(provider.base_url or "").strip():
        return {"error": "Suno-compatible audio provider 缺少 Base URL", "error_kind": "bad_config"}

    query_endpoint = _suno_record_info_endpoint(provider.base_url, task_id)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    poll_interval, poll_timeout = _suno_poll_settings(provider, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
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
                if query_data is None:
                    return {
                        "error": "音频任务查询响应为空",
                        "error_kind": "bad_response",
                        "endpoint": query_endpoint,
                        "job_id": task_id,
                    }
                latest_data = query_data
                status = _suno_status(query_data, status)
                payload = _suno_payload_data(query_data)
                polls.append({
                    "status": status,
                    "progress": payload.get("progress") if isinstance(payload, dict) else None,
                })

                if not _suno_response_success(query_data):
                    provider_msg = _suno_provider_message(query_data)
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

                audio_items = _collect_suno_audio_items(query_data)
                if audio_items and status not in _SUNO_FAILED_STATUSES:
                    audios = await _localize_suno_audio_items(project_id, audio_items, save_locally)
                    primary = audios[0] if audios else {}
                    return {
                        "ok": True,
                        "provider": provider.name,
                        "model": provider.model_name,
                        "status": "completed",
                        "job_id": task_id,
                        "url": primary.get("url"),
                        "local_url": primary.get("local_url"),
                        "local_path": primary.get("local_path"),
                        "remote_url": primary.get("remote_url"),
                        "stream_audio_url": primary.get("stream_audio_url"),
                        "source_audio_url": primary.get("source_audio_url"),
                        "image_url": primary.get("image_url"),
                        "duration": primary.get("duration_seconds"),
                        "mime_type": primary.get("mime_type"),
                        "audios": audios,
                        "progress": payload.get("progress") if isinstance(payload, dict) else None,
                        "polls": polls,
                        "download_error": primary.get("download_error"),
                    }

                if status in _SUNO_FAILED_STATUSES:
                    provider_msg = _suno_provider_message(query_data)
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
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": query_endpoint,
        }


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


async def _poll_xai_video_task(
    provider: MediaProvider,
    project_id: str,
    request_id: str,
    extra_override: dict[str, Any] | None,
    save_locally: bool,
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


async def _call_openai_image(
    provider: MediaProvider,
    prompt: str,
    negative_prompt: str | None,
    size: str,
    quality: str | None,
    n: int,
    reference_images: list[str] | None,
    extra_override: dict[str, Any],
) -> dict[str, Any]:
    extra = _parse_extra(provider)
    extra.update(extra_override)

    ref_param = extra.pop("_reference_param", "image")
    ref_format = extra.pop("_reference_format", "data_url")

    payload: dict[str, Any] = {
        "model": provider.model_name,
        "prompt": prompt,
        "n": n,
        "size": size,
        **extra,
    }
    if quality:
        payload["quality"] = quality
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if "response_format" not in payload:
        payload["response_format"] = "url"

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
                    "endpoint": provider.base_url,
                }
            payload[ref_param] = data_urls if len(data_urls) > 1 else data_urls[0]
        else:
            payload[ref_param] = reference_images if len(reference_images) > 1 else reference_images[0]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    endpoint = _openai_images_endpoint(provider.base_url)

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

    data = resp.json()
    images = data.get("data", [])
    if not images:
        return {
            "error": "响应中没有图片数据",
            "error_kind": "empty_response",
            "raw": data,
            "endpoint": endpoint,
        }

    results = [{"url": img.get("url"), "b64": img.get("b64_json")} for img in images]
    return {"images": results}


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

    data = resp.json()
    val: Any = data
    try:
        for key in response_path:
            if isinstance(val, list):
                val = val[int(key)]
            else:
                val = val[key]
    except (KeyError, IndexError, TypeError):
        return {
            "error": f"无法按路径 {response_path} 解析响应",
            "error_kind": "bad_response",
            "raw": data,
            "endpoint": endpoint,
        }

    return {"images": [{"url": val, "b64": None}]}


# Image provider calls are single-shot. The model must repair the original node
# after a failed call; backend code must not silently lower resolution or quality.
def _downgrade_size(current: str) -> str | None:
    """Compatibility hook: automatic resolution downgrade is disabled."""
    return None


def _is_retryable_error(error_kind: str | None, http_code: int | None) -> bool:
    """Compatibility hook: provider image calls do not auto-retry."""
    return False


# ---- provider preset params ----

# Common parameters for well-known image providers.
# These fill in missing fields when provider.params_json doesn't specify them.
_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    # flux via SiliconFlow / Together / Replicate / Fal
    "flux": {
        "size": "1024x1792",
        "steps": 30,
        "guidance_scale": 3.5,
    },
    "flux-pro": {
        "size": "1024x1792",
        "steps": 28,
        "guidance_scale": 3.5,
        "quality": "hd",
    },
    "flux-schnell": {
        "size": "1024x1792",
        "steps": 4,
        "guidance_scale": 0.0,
    },
    "flux-dev": {
        "size": "1024x1792",
        "steps": 28,
        "guidance_scale": 3.5,
    },
    # sdxl
    "sdxl": {
        "size": "1024x1792",
        "steps": 30,
        "guidance_scale": 7.5,
        "sampler": "DPM++ 2M Karras",
    },
    "sdxl-turbo": {
        "size": "1024x1792",
        "steps": 4,
        "guidance_scale": 1.0,
    },
    # sdxl-lightning
    "sdxl-lightning": {
        "size": "1024x1792",
        "steps": 4,
        "guidance_scale": 1.0,
        "sampler": "DPM++ SDE Karras",
    },
    # sd3 / sd3.5
    "sd3": {
        "size": "1024x1792",
        "steps": 28,
        "guidance_scale": 7.0,
    },
    "sd3.5": {
        "size": "1024x1792",
        "steps": 28,
        "guidance_scale": 4.5,
    },
    # midjourney (via API proxies)
    "midjourney": {
        "size": "1024x1792",
        "quality": "hd",
        "stylize": 100,
    },
    # dall-e
    "dall-e-3": {
        "size": "1792x1024",
        "quality": "hd",
    },
    "dall-e-2": {
        "size": "1024x1024",
    },
    # kandinsky
    "kandinsky": {
        "size": "1024x1792",
        "steps": 50,
        "guidance_scale": 4.0,
    },
    # playground
    "playground": {
        "size": "1024x1792",
        "quality": "hd",
        "sampler": "DPMSolver++",
        "cfg_scale": 7.0,
        "steps": 30,
    },
    # PixArt
    "pixart": {
        "size": "1024x1792",
        "steps": 20,
        "guidance_scale": 4.5,
    },
    # Kolors
    "kolors": {
        "size": "1024x1792",
        "steps": 25,
        "guidance_scale": 5.0,
    },
    # HunyuanDiT
    "hunyuan": {
        "size": "1024x1792",
        "steps": 50,
        "guidance_scale": 6.0,
    },
    # Lumina
    "lumina": {
        "size": "1024x1792",
        "steps": 20,
        "guidance_scale": 4.0,
    },
    # CogView / CogVideo
    "cogview": {
        "size": "1024x1792",
    },
    # seedream
    "seedream": {
        "size": "1024x1792",
        "steps": 25,
        "guidance_scale": 5.0,
    },
    # generic defaults for unknown models
    "*": {
        "size": "1024x1792",
    },
}

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

    Performs case-insensitive substring matching against the preset keys.
    Returns None if no match found.
    """
    name_lower = model_name.lower().replace("_", "-").replace(" ", "-")
    # Prefer longest key match first
    for key in sorted(_PROVIDER_PRESETS.keys(), key=lambda k: -len(k)):
        if key == "*":
            continue
        if key in name_lower:
            return dict(_PROVIDER_PRESETS[key])
    return dict(_PROVIDER_PRESETS.get("*", {}))


def list_presets() -> dict[str, dict[str, Any]]:
    """Return all known provider presets (for settings UI)."""
    return dict(_PROVIDER_PRESETS)


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
        if provider.api_format == "openai":
            return await _call_openai_image(
                provider, prompt, negative_prompt, _size, _quality, n,
                resolved_refs or None, extra_override,
            )
        # raw_http
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
    for img in images:
        remote_url = img.get("url")
        b64 = img.get("b64")
        local_path: str | None = None
        local_url: str | None = None

        if save_locally:
            filename = f"{uuid.uuid4().hex[:12]}.png"
            dest = _storage_path(project_id, filename)
            try:
                if b64:
                    dest.write_bytes(base64.b64decode(b64))
                    local_path = str(dest)
                elif remote_url:
                    async with httpx.AsyncClient(timeout=_media_http_timeout()) as client:
                        r = await client.get(remote_url)
                    if r.status_code == 200:
                        dest.write_bytes(r.content)
                        local_path = str(dest)
            except Exception:
                local_path = None
            if local_path:
                local_url = f"/api/media/{project_id}/{filename}"

        # `url` is what consumers should display: prefer local (stable), fall back to remote
        output_images.append({
            "url": local_url or remote_url,
            "local_url": local_url,
            "local_path": local_path,
            "remote_url": remote_url,
        })

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
        "size_final": last_attempt_size,
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
    if _is_suno_compatible_audio_provider(provider):
        result = await _call_suno_compatible_audio(
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
    elif _is_openai_tts_audio_provider(provider):
        tts_extra = dict(extra_override)
        if style and "instructions" not in tts_extra and "style" not in tts_extra:
            tts_extra["style"] = style
        result = await _call_openai_tts_audio(
            provider=provider,
            project_id=project_id,
            prompt=prompt,
            extra_override=tts_extra,
            save_locally=save_locally,
        )
    else:
        result = {
            "error": (
                f"Unsupported audio provider api_format: {provider.api_format}. "
                "Use api_format='openai_tts' for OpenAI-compatible speech or 'suno_compatible' for Suno-compatible music generation APIs."
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

    if _is_suno_compatible_audio_provider(provider):
        result = await _poll_suno_compatible_audio_task(
            provider=provider,
            project_id=project_id,
            task_id=job_id,
            extra_override=extra or {},
            save_locally=save_locally,
        )
    elif _is_openai_tts_audio_provider(provider):
        result = {
            "error": "OpenAI-compatible TTS 是同步接口，不支持按 job_id 轮询",
            "error_kind": "unsupported_action",
            "status": "failed",
            "job_id": job_id,
        }
    else:
        result = {
            "error": (
                f"Unsupported audio provider api_format: {provider.api_format}. "
                "Use api_format='openai_tts' for OpenAI-compatible speech or 'suno_compatible' for Suno-compatible music generation APIs."
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
        if _is_xai_video_provider(provider):
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            endpoint = _xai_video_generations_endpoint(provider.base_url)
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": "xai_video",
                "endpoint": endpoint,
                "check": "configuration_only",
                "error": f"缺少配置: {', '.join(missing)}" if missing else None,
            }
        if _is_grok_1_5_video_provider(provider):
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            if not provider.base_url:
                missing.append("base_url")
            endpoint = _grok_1_5_video_endpoint(provider.base_url) if provider.base_url else ""
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": "grok_1_5",
                "endpoint": endpoint,
                "check": "configuration_only",
                "error": f"缺少配置: {', '.join(missing)}" if missing else None,
            }
        if _is_ark_video_provider(provider):
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            endpoint = _ark_video_tasks_endpoint(provider.base_url)
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": "volcengine_ark",
                "endpoint": endpoint,
                "check": "configuration_only",
                "error": f"缺少配置: {', '.join(missing)}" if missing else None,
            }
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "error": f"Unsupported video provider api_format: {provider.api_format}",
        }

    if provider.kind == "audio":
        if _is_openai_tts_audio_provider(provider):
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            if not provider.base_url:
                missing.append("base_url")
            endpoint = _openai_tts_endpoint(provider.base_url) if provider.base_url else ""
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": "openai_tts",
                "endpoint": endpoint,
                "check": "configuration_only",
                "error": f"缺少配置: {', '.join(missing)}" if missing else None,
            }
        if _is_suno_compatible_audio_provider(provider):
            missing: list[str] = []
            if not provider.api_key:
                missing.append("api_key")
            if not provider.model_name:
                missing.append("model_name")
            if not provider.base_url:
                missing.append("base_url")
            endpoint = _suno_generate_endpoint(provider.base_url) if provider.base_url else ""
            return {
                "ok": not missing,
                "provider": provider.name,
                "model": provider.model_name,
                "adapter": "suno_compatible",
                "endpoint": endpoint,
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
    if _is_ark_video_provider(provider):
        result = await _call_volcengine_ark_video(
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
    elif _is_grok_1_5_video_provider(provider):
        result = await _call_grok_1_5_video(
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
    elif _is_xai_video_provider(provider):
        result = await _call_xai_video(
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
                "Use api_format='volcengine_ark' for Seedance 2.0, 'xai_video' for official xAI video, or 'grok_1_5' for Grok 1.5 multipart-compatible video."
            ),
            "error_kind": "unsupported_provider",
            "status": "failed",
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

    if _is_ark_video_provider(provider):
        result = await _poll_volcengine_ark_video_task(
            provider=provider,
            project_id=project_id,
            task_id=job_id,
            extra_override=extra or {},
            save_locally=save_locally,
        )
    elif _is_grok_1_5_video_provider(provider):
        result = await _poll_grok_1_5_video_task(
            provider=provider,
            project_id=project_id,
            request_id=job_id,
            extra_override=extra or {},
            save_locally=save_locally,
        )
    elif _is_xai_video_provider(provider):
        result = await _poll_xai_video_task(
            provider=provider,
            project_id=project_id,
            request_id=job_id,
            extra_override=extra or {},
            save_locally=save_locally,
        )
    else:
        result = _with_video_model_doc_hint({
            "error": (
                f"Unsupported video provider api_format: {provider.api_format}. "
                "Use api_format='volcengine_ark' for Seedance 2.0, 'xai_video' for official xAI video, or 'grok_1_5' for Grok 1.5 multipart-compatible video."
            ),
            "error_kind": "unsupported_provider",
            "status": "failed",
            "job_id": job_id,
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
