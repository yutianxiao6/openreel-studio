"""Media provider orchestration for OpenReel.

Image and audio still use the host protocol catalogs. Video request construction,
provider polling, response parsing, and output extraction belong exclusively to
Universal Model Adapter; this module only manages OpenReel jobs and media storage.
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


async def _resolve_reference_media(
    project_id: str,
    kind: str,
    refs: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Resolve video/audio node references for the UMA media boundary."""
    resolved: list[str] = []
    errors: list[str] = []
    project_root = settings.storage_path_resolved / project_id
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
            if asset is None:
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
        candidate = Path(ref).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / ref
        candidate = candidate.resolve()
        if candidate.exists() and candidate.is_file():
            resolved.append(str(candidate))
        else:
            errors.append(f"{kind} 文件不存在: {raw}")
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


def _protocol_join_url(base_url: str | None, path: str | None) -> str:
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


def _protocol_base_for(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> str:
    base_url_param = str(section.get("base_url_param") or "").strip()
    if base_url_param:
        return str(_parse_extra(provider).get(base_url_param) or "").strip()
    return str(
        getattr(provider, "base_url", "")
        or protocol.get("base_url")
        or protocol.get("default_base_url")
        or ""
    ).strip()


def _protocol_endpoint_for(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
    *,
    task_id: str | None = None,
) -> str:
    base = _protocol_base_for(provider, protocol, section)
    path = str(section.get("path") or section.get("endpoint") or "").strip()
    if task_id is not None:
        path = path.replace("{task_id}", task_id)
    if not base and not path.startswith(("http://", "https://")):
        return ""
    return _protocol_join_url(base, path)


def _protocol_headers(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for source in (protocol.get("headers"), section.get("headers")):
        if isinstance(source, dict):
            headers.update(
                {str(key): str(value) for key, value in source.items() if value is not None}
            )
    auth = str(section.get("auth") or protocol.get("auth") or "bearer").strip().lower()
    api_key = str(getattr(provider, "api_key", "") or "").strip()
    if api_key and auth in {"bearer", "authorization_bearer"}:
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key and auth in {"api_key_header", "header"}:
        name = str(
            section.get("api_key_header") or protocol.get("api_key_header") or "Authorization"
        ).strip()
        headers[name] = api_key
    elif api_key and auth in {"authorization_raw", "raw"}:
        headers["Authorization"] = api_key
    return headers


def _protocol_model_profile(protocol: dict[str, Any], model_name: str) -> dict[str, Any]:
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


def _protocol_render_value(value: Any, context: dict[str, Any]) -> Any:
    if (
        isinstance(value, str)
        and value.startswith("$")
        and re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*$", value)
    ):
        return context.get(value[1:])
    if isinstance(value, list):
        rendered = [_protocol_render_value(item, context) for item in value]
        return [item for item in rendered if item not in (None, "", [], {})]
    if isinstance(value, dict):
        rendered_dict: dict[str, Any] = {}
        for key, item in value.items():
            rendered = _protocol_render_value(item, context)
            if rendered not in (None, "", [], {}):
                rendered_dict[str(key)] = rendered
        return rendered_dict
    return value


def _protocol_provider_error(
    protocol: dict[str, Any],
    data: dict[str, Any],
) -> tuple[str, str | None]:
    error_config = protocol.get("error") if isinstance(protocol.get("error"), dict) else {}
    poll_config = protocol.get("poll") if isinstance(protocol.get("poll"), dict) else {}
    message_path = str(
        error_config.get("message_path") or poll_config.get("error_message_path") or "error"
    ).strip()
    code_path = str(
        error_config.get("code_path") or poll_config.get("error_code_path") or "error_code"
    ).strip()
    value = _lookup_path(data, message_path)
    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or value.get("detail")
    message = str(value or "").strip()
    code = str(_lookup_path(data, code_path) or "").strip() or None
    return message or "媒体生成任务失败", code


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


def _normalized_api_format(provider: MediaProvider) -> str:
    fmt = str(provider.api_format or "").strip().lower().replace("-", "_")
    if fmt == "raw_post":
        return "raw"
    return fmt


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


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


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


def _image_transport_mode(
    provider: MediaProvider, extra_override: dict[str, Any] | None, default: str = "data_url"
) -> str:
    extra = _parse_extra(provider)
    extra.update(extra_override or {})
    raw = (
        str(
            extra.get("image_transport")
            or extra.get("reference_image_transport")
            or extra.get("image_input")
            or default
        )
        .strip()
        .lower()
        .replace("-", "_")
    )
    if raw in {"public_url", "url", "remote_url", "http_url", "https_url"}:
        return "public_url"
    return "data_url"


def _public_media_url_for_ref(
    project_id: str, ref: str, public_base_url: str | None
) -> tuple[str | None, str | None]:
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
        return _public_media_url_for_ref(
            project_id, ref, _first_text(extra.get("public_base_url"), extra.get("site_base_url"))
        )

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


def _audio_http_v1_normalize_audio_item(
    protocol: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any] | None:
    result = _audio_http_v1_result_section(protocol)
    remote_url = _audio_http_v1_first_path_text(
        item, result.get("url_paths") or result.get("audio_url_paths")
    )
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
        "duration_seconds": _audio_http_v1_first_path_value(
            item, result.get("duration_paths") or ["duration"]
        ),
        "tags": _audio_http_v1_first_path_value(item, result.get("tags_paths") or ["tags"]),
    }


def _audio_http_v1_collect_from_value(
    protocol: dict[str, Any], value: Any, seen: set[int] | None = None
) -> list[dict[str, Any]]:
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


def _audio_http_v1_collect_audio_items(
    protocol: dict[str, Any], data: dict[str, Any]
) -> list[dict[str, Any]]:
    result = _audio_http_v1_result_section(protocol)
    paths = (
        result.get("items_paths")
        or result.get("audio_items_paths")
        or ["data", "audios", "audio", "items", "result"]
    )
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


async def _localize_audio_items(
    project_id: str, items: list[dict[str, Any]], save_locally: bool
) -> list[dict[str, Any]]:
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


def _response_json(
    resp: httpx.Response, endpoint: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
    return _protocol_endpoint_for(provider, protocol, section, task_id=task_id)


def _audio_http_v1_headers(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, str]:
    return _protocol_headers(provider, protocol, section)


def _audio_http_v1_model_profile(protocol: dict[str, Any], model_name: str) -> dict[str, Any]:
    return _protocol_model_profile(protocol, model_name)


def _audio_http_v1_response_success(
    protocol: dict[str, Any], data: dict[str, Any], section: dict[str, Any]
) -> bool:
    success_path = str(section.get("success_path") or section.get("ok_path") or "").strip()
    if not success_path:
        return True
    value = _lookup_path(data, success_path)
    if value is None:
        return True
    configured = section.get("success_values") or section.get("ok_values")
    values = (
        _string_set(configured) if configured is not None else {"0", "200", "success", "ok", "true"}
    )
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
        paths = [
            "id",
            "task_id",
            "taskId",
            "job_id",
            "data.id",
            "data.task_id",
            "data.taskId",
            "data.job_id",
        ]
    return _audio_http_v1_first_path_text(data, paths)


def _audio_http_v1_status(
    protocol: dict[str, Any], data: dict[str, Any], fallback: str = "queued"
) -> str:
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
    succeeded = _string_set(poll.get("succeeded") or poll.get("done_statuses")) or {
        "succeeded",
        "success",
        "completed",
        "complete",
        "done",
    }
    failed = _string_set(poll.get("failed") or poll.get("failed_statuses")) or {
        "failed",
        "failure",
        "error",
        "cancelled",
        "canceled",
        "expired",
    }
    running = _string_set(poll.get("running") or poll.get("running_statuses")) or {
        "queued",
        "pending",
        "running",
        "processing",
        "in_progress",
        "submitted",
        "created",
    }
    return (
        {item.lower() for item in succeeded},
        {item.lower() for item in failed},
        {item.lower() for item in running},
    )


def _audio_http_v1_poll_settings(
    provider: MediaProvider, protocol: dict[str, Any], extra_override: dict[str, Any] | None
) -> tuple[float, float]:
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
    defaults = (
        protocol.get("default_params") if isinstance(protocol.get("default_params"), dict) else {}
    )
    profile = _audio_http_v1_model_profile(protocol, model_name)
    profile_defaults = (
        profile.get("default_params") if isinstance(profile.get("default_params"), dict) else {}
    )
    return {**defaults, **profile_defaults}


def _audio_http_v1_numeric(
    value: Any, field_name: str
) -> tuple[float | None, dict[str, Any] | None]:
    if value in (None, ""):
        return None, None
    try:
        return float(str(value)), None
    except (TypeError, ValueError):
        return None, {
            "error": f"audio_http_v1 {field_name} 必须是数字，收到: {value!r}",
            "error_kind": "bad_request",
        }


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
    model_name = str(
        (extra_override or {}).get("model")
        or base_extra.get("model")
        or getattr(provider, "model_name", "")
        or ""
    ).strip()
    if not model_name:
        return None, {"error": "audio_http_v1 provider 缺少 model_name", "error_kind": "bad_config"}
    extra = _audio_http_v1_default_params(protocol, model_name)
    extra.update(base_extra)
    extra.update(extra_override or {})

    clean_prompt = str(prompt or "").strip()
    input_text = str(extra.get("input") or extra.get("text") or clean_prompt).strip()
    override = extra_override or {}
    response_format = (
        str(
            override.get("response_format")
            or override.get("format")
            or override.get("audio_format")
            or extra.get("response_format")
            or extra.get("format")
            or extra.get("audio_format")
            or protocol.get("default_response_format")
            or ""
        )
        .strip()
        .lower()
        or None
    )
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
        "instructions": str(extra.get("instructions") or style or extra.get("style") or "").strip()
        or None,
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
    payload = _protocol_render_value(body_template, context)
    if not isinstance(payload, dict):
        return None, {
            "error": "audio_http_v1 request.body 渲染结果不是对象",
            "error_kind": "bad_config",
        }
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
        return {
            "error": "audio_http_v1 provider 缺少 base_url 或 request.path",
            "error_kind": "bad_config",
        }
    headers = _audio_http_v1_headers(provider, protocol, request)
    method = str(request.get("method") or "POST").strip().upper()
    result_section = _audio_http_v1_result_section(protocol)
    response_type = (
        str(
            result_section.get("type")
            or result_section.get("response_type")
            or request.get("response_type")
            or "json"
        )
        .strip()
        .lower()
    )

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            resp = (
                await client.get(endpoint, params=payload, headers=headers)
                if method == "GET"
                else await client.request(method, endpoint, json=payload, headers=headers)
            )
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
            return {
                "error": "audio_http_v1 响应是 JSON，不是音频二进制",
                "error_kind": "bad_response",
                "endpoint": endpoint,
                "raw": data,
            }
        if not resp.content:
            return {
                "error": "audio_http_v1 响应为空",
                "error_kind": "empty_response",
                "endpoint": endpoint,
            }
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
            "mime_type": saved.get("mime_type")
            or str(content_type or "").split(";", 1)[0].strip()
            or None,
            "voice": payload.get("voice"),
            "speed": payload.get("speed"),
            "instructions": payload.get("instructions"),
            "format": payload.get("response_format") or payload.get("format"),
            "endpoint": endpoint,
            "audios": [
                {
                    "n_index": 0,
                    "url": saved.get("local_url"),
                    "local_url": saved.get("local_url"),
                    "local_path": saved.get("local_path"),
                    "mime_type": saved.get("mime_type"),
                }
            ]
            if saved.get("local_url")
            else [],
        }

    create_data, create_error = _response_json(resp, endpoint)
    if create_error:
        return create_error
    create_data = create_data or {}
    if not _audio_http_v1_response_success(protocol, create_data, request):
        provider_msg = _audio_http_v1_provider_message(protocol, create_data)
        return {
            "error": provider_msg,
            "error_kind": "provider_failed",
            "endpoint": endpoint,
            "provider_msg": provider_msg,
            "raw": create_data,
        }

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
        return {
            "error": "创建 audio_http_v1 任务响应缺少 task id 或音频结果",
            "error_kind": "bad_response",
            "endpoint": endpoint,
            "raw": create_data,
        }
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
        return {
            "error": provider_msg,
            "error_kind": "provider_failed",
            "endpoint": endpoint,
            "provider_msg": provider_msg,
            "raw": create_data,
            "job_id": task_id,
            "status": status,
        }
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
        return {
            "error": "audio_http_v1 协议未配置 poll，无法按 job_id 轮询",
            "error_kind": "unsupported_action",
            "job_id": task_id,
            "status": "failed",
        }
    query_endpoint = _audio_http_v1_endpoint_for(provider, protocol, poll, task_id=task_id)
    if not query_endpoint:
        return {
            "error": "audio_http_v1 protocol 缺少 poll.path",
            "error_kind": "bad_config",
            "job_id": task_id,
            "status": "failed",
        }
    headers = _audio_http_v1_headers(provider, protocol, poll)
    method = str(poll.get("method") or "GET").strip().upper()
    poll_interval, poll_timeout = _audio_http_v1_poll_settings(provider, protocol, extra_override)
    deadline = time.monotonic() + poll_timeout
    polls: list[dict[str, Any]] = []
    latest_data: dict[str, Any] = {}
    status = "queued"
    succeeded, failed, _running = _audio_http_v1_status_sets(protocol)
    poll_body = poll.get("body") if isinstance(poll.get("body"), dict) else {}
    poll_payload = (
        _protocol_render_value(poll_body, {"task_id": task_id, "job_id": task_id})
        if poll_body
        else {}
    )

    try:
        async with httpx.AsyncClient(timeout=_media_audio_timeout()) as client:
            while True:
                queried = (
                    await client.request(method, query_endpoint, json=poll_payload, headers=headers)
                    if method != "GET"
                    else await client.get(query_endpoint, headers=headers)
                )
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
                await _notify_progress(
                    progress_callback,
                    {
                        "job_id": task_id,
                        "status": status,
                        "progress": progress,
                        "poll_count": len(polls),
                        "provider": provider.name,
                        "model": provider.model_name,
                        "endpoint": query_endpoint,
                    },
                )

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
                if _audio_http_v1_collect_audio_items(protocol, query_data) and (
                    complete_on_items or status in succeeded
                ):
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
        return {
            "error": f"网络请求失败: {exc}",
            "error_kind": "network",
            "endpoint": query_endpoint,
        }


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
    return _protocol_endpoint_for(provider, protocol, section, task_id=task_id)


def _image_http_v1_headers(
    provider: MediaProvider,
    protocol: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, str]:
    return _protocol_headers(provider, protocol, section)


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
    payload = _protocol_render_value(body_template, context)
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
                        "error": _protocol_provider_error(protocol, latest)[0],
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
        return {
            "ok": False,
            "provider": provider.name,
            "model": provider.model_name,
            "error": "视频 provider 的 api_format 必须是 universal_adapter",
            "error_kind": "bad_config",
            "supported_api_formats": ["universal_adapter"],
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
