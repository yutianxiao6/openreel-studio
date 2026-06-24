"""Reference asset tools for uploaded visual references.

This module keeps uploaded images as durable project reference assets. The
agent can then resolve @labels, analyze images, bind them to blueprints, and
explicitly save stable visual styles to user memory.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.config import settings
from app.db.models import Asset, Project
from app.db.session import session_scope
from app.mcp_tools.file_tools import _safe_path, read_image_base64_data_url
from app.services.node_public_ids import (
    internal_to_public_id_map,
    public_node_id_from_dict,
    resolve_internal_node_id,
)


REFERENCE_STATE_KEY = "reference_assets"
REFERENCE_STATE_VERSION = 1
REFERENCE_ANALYSIS_SCHEMA = "visual_reference_analysis_v1"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _strip_json_fence(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    return text.strip()


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_strip_json_fence(text))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _reference_store(state: dict[str, Any]) -> dict[str, Any]:
    store = state.get(REFERENCE_STATE_KEY)
    if not isinstance(store, dict):
        store = {}
        state[REFERENCE_STATE_KEY] = store
    store.setdefault("version", REFERENCE_STATE_VERSION)
    store.setdefault("assets", [])
    store.setdefault("bindings", [])
    return store


def _normalize_mention(value: str | None, fallback: str) -> str:
    label = _text(value) or fallback
    return label if label.startswith("@") else f"@{label}"


def _asset_aliases(asset: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for value in [asset.get("mention"), asset.get("label"), asset.get("filename"), *_as_list(asset.get("aliases"))]:
        text = _text(value)
        if not text:
            continue
        if text.startswith("@"):
            forms = [text, text.lstrip("@")]
        else:
            forms = [text, f"@{text}"]
        for form in forms:
            if form and form not in aliases:
                aliases.append(form)
    return aliases


def _find_asset(
    store: dict[str, Any],
    *,
    ref_id: str | None = None,
    mention: str | None = None,
    rel_path: str | None = None,
    source_path: str | None = None,
    asset_id: str | None = None,
    node_id: str | None = None,
    url: str | None = None,
    query: str | None = None,
) -> dict[str, Any] | None:
    ref_id = _text(ref_id)
    mention = _text(mention)
    rel_path = _text(rel_path)
    source_path = _text(source_path)
    asset_id = _text(asset_id)
    node_id = _text(node_id)
    url = _text(url)
    query = _text(query)
    for asset in _as_list(store.get("assets")):
        if not isinstance(asset, dict):
            continue
        if ref_id and asset.get("ref_id") == ref_id:
            return asset
        if rel_path and asset.get("rel_path") == rel_path:
            return asset
        if source_path and asset.get("source_path") == source_path:
            return asset
        if asset_id and asset.get("asset_id") == asset_id:
            return asset
        if node_id and asset.get("node_id") == node_id:
            return asset
        if url and asset.get("url") == url:
            return asset
        if mention and mention in _asset_aliases(asset):
            return asset
    if query:
        compact = query.lstrip("@").lower()
        for asset in _as_list(store.get("assets")):
            if not isinstance(asset, dict):
                continue
            haystack = " ".join(_asset_aliases(asset)).lower()
            analysis = asset.get("analysis") if isinstance(asset.get("analysis"), dict) else {}
            haystack += " " + " ".join(
                str(analysis.get(key) or "")
                for key in ("summary", "style_name", "prompt_fragment", "subject")
            ).lower()
            if compact and compact in haystack:
                return asset
    return None


def _image_index_for_next(store: dict[str, Any]) -> int:
    count = 0
    for asset in _as_list(store.get("assets")):
        if isinstance(asset, dict) and asset.get("kind") == "image":
            count += 1
    return count + 1


def _storage_roots_for_project(project_id: str) -> list[Path]:
    roots: list[Path] = []
    for key in ("STORAGE_DIR", "STORAGE_PATH"):
        root = Path(getattr(settings, key, "./storage")).expanduser().resolve() / project_id
        if root not in roots:
            roots.append(root)
    return roots


def _project_relative_for_path(project_id: str, path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser().resolve()
    for root in _storage_roots_for_project(project_id):
        try:
            return p.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return None


def _project_storage_file(project_id: str, rel_path: str | None) -> Path | None:
    rel = _text(rel_path).lstrip("/")
    if not rel:
        return None
    for root in _storage_roots_for_project(project_id):
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    try:
        return _safe_path(project_id, rel)
    except ValueError:
        return None


def _rel_path_from_media_url(project_id: str, url: str | None) -> str | None:
    url = _text(url)
    prefix = f"/api/media/{project_id}/"
    if not url.startswith(prefix):
        return None
    return f"generated_images/{url[len(prefix):].lstrip('/')}"


def _reference_input_value(asset: dict[str, Any]) -> str:
    if _text(asset.get("rel_path")):
        return _text(asset.get("rel_path"))
    if _text(asset.get("source_path")):
        return _text(asset.get("source_path"))
    if _text(asset.get("asset_id")):
        return f"asset:{asset.get('asset_id')}"
    return ""


async def _resolve_workflow_node_id(project_id: str, node_id: str | None) -> str:
    raw = _text(node_id)
    if not raw:
        return ""
    async with session_scope() as session:
        return await resolve_internal_node_id(session, project_id, raw)


async def _node_public_id_map(project_id: str) -> dict[str, str]:
    async with session_scope() as session:
        return await internal_to_public_id_map(session, project_id)


def _register_reference_asset(
    store: dict[str, Any],
    *,
    rel_path: str | None = None,
    source_path: str | None = None,
    mention: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
    size: int | None = None,
    attachment_id: str | None = None,
    asset_id: str | None = None,
    node_id: str | None = None,
    base64_rel_path: str | None = None,
    source: str = "upload",
    url: str | None = None,
    roles: list[str] | None = None,
) -> dict[str, Any]:
    existing = _find_asset(
        store,
        rel_path=rel_path,
        source_path=source_path,
        asset_id=asset_id,
        node_id=node_id,
        url=url,
    )
    now = _now_iso()
    if existing:
        if mention:
            normalized = _normalize_mention(mention, existing.get("mention") or "图")
            existing["mention"] = normalized
            aliases = list(dict.fromkeys([*_as_list(existing.get("aliases")), normalized, normalized.lstrip("@")]))
            existing["aliases"] = aliases
        if roles:
            existing["roles"] = sorted(set([*_as_list(existing.get("roles")), *roles]))
        if rel_path:
            existing["rel_path"] = rel_path
        if source_path:
            existing["source_path"] = source_path
        if asset_id:
            existing["asset_id"] = asset_id
        if node_id:
            existing["node_id"] = node_id
        if base64_rel_path:
            existing["base64_rel_path"] = base64_rel_path
        if url:
            existing["url"] = url
        existing["updated_at"] = now
        return existing

    fallback_mention = f"图{_image_index_for_next(store)}"
    normalized = _normalize_mention(mention, fallback_mention)
    effective_path = rel_path or source_path or url or ""
    asset = {
        "ref_id": f"ref_{uuid.uuid4().hex[:12]}",
        "mention": normalized,
        "label": normalized.lstrip("@"),
        "aliases": [normalized, normalized.lstrip("@")],
        "source": source,
        "kind": "image",
        "rel_path": rel_path,
        "source_path": source_path,
        "url": url,
        "filename": filename or Path(effective_path).name,
        "mime_type": mime_type or mimetypes.guess_type(effective_path)[0] or "image/png",
        "size": size,
        "attachment_id": attachment_id,
        "asset_id": asset_id,
        "node_id": node_id,
        "base64_rel_path": base64_rel_path,
        "roles": roles or ["visual_reference"],
        "status": "pending_analysis",
        "created_at": now,
        "updated_at": now,
    }
    store.setdefault("assets", []).append(asset)
    return asset


def _register_attachments(
    store: dict[str, Any],
    attachments: list[dict[str, Any]],
    *,
    attachment_aliases: list[str] | None = None,
    attachment_roles: list[list[str]] | list[str] | None = None,
) -> list[dict[str, Any]]:
    registered: list[dict[str, Any]] = []
    shared_roles: list[str] | None = None
    if isinstance(attachment_roles, list) and attachment_roles and all(
        isinstance(item, str) for item in attachment_roles
    ):
        shared_roles = [_text(item) for item in attachment_roles if _text(item)]
    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, dict) or attachment.get("kind") != "image":
            continue
        rel_path = _text(attachment.get("rel_path"))
        if not rel_path:
            continue
        explicit_alias = ""
        if isinstance(attachment_aliases, list) and index - 1 < len(attachment_aliases):
            explicit_alias = _text(attachment_aliases[index - 1])
        roles_for_attachment: list[str] | None = None
        if shared_roles is not None:
            roles_for_attachment = shared_roles
        elif isinstance(attachment_roles, list) and index - 1 < len(attachment_roles):
            raw_roles = attachment_roles[index - 1]
            if isinstance(raw_roles, list):
                roles_for_attachment = [_text(item) for item in raw_roles if _text(item)]
            elif isinstance(raw_roles, str):
                role_text = _text(raw_roles)
                roles_for_attachment = [role_text] if role_text else None
        mention = (
            explicit_alias
            or attachment.get("mention")
            or attachment.get("ref_label")
            or attachment.get("reference_label")
            or attachment.get("display_label")
            or f"图{index}"
        )
        registered.append(_register_reference_asset(
            store,
            rel_path=rel_path,
            mention=str(mention),
            filename=_text(attachment.get("filename")) or None,
            mime_type=_text(attachment.get("mime_type")) or None,
            size=attachment.get("size") if isinstance(attachment.get("size"), int) else None,
            attachment_id=_text(attachment.get("attachment_id") or attachment.get("id")) or None,
            base64_rel_path=_text(attachment.get("base64_rel_path")) or None,
            roles=roles_for_attachment,
        ))
    return registered


async def _load_asset_record(project_id: str, asset_id: str) -> Asset | None:
    async with session_scope() as session:
        asset = await session.get(Asset, asset_id)
        if not asset or asset.project_id != project_id:
            return None
        return asset


def _asset_metadata(asset: Asset) -> dict[str, Any]:
    try:
        data = json.loads(asset.metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


async def _register_from_asset_record(
    project_id: str,
    store: dict[str, Any],
    *,
    asset_id: str,
    mention: str | None = None,
    roles: list[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    db_asset = await _load_asset_record(project_id, asset_id)
    if not db_asset:
        return None, {"ok": False, "error": "Asset not found", "error_kind": "asset_not_found"}
    metadata = _asset_metadata(db_asset)
    source_path = _text(db_asset.path or metadata.get("local_path")) or None
    rel_path = _project_relative_for_path(project_id, source_path)
    if not rel_path:
        rel_path = _rel_path_from_media_url(project_id, db_asset.url or metadata.get("local_url"))
    url = _text(db_asset.url or metadata.get("url") or metadata.get("remote_url")) or None
    if not (rel_path or source_path or url):
        return None, {
            "ok": False,
            "error": "Asset has no readable image source",
            "error_kind": "asset_source_missing",
            "asset_id": asset_id,
        }
    asset = _register_reference_asset(
        store,
        rel_path=rel_path,
        source_path=source_path,
        mention=mention,
        filename=db_asset.name or (Path(source_path).name if source_path else None),
        mime_type=db_asset.mime_type,
        asset_id=db_asset.id,
        node_id=db_asset.node_id,
        source="asset_record",
        url=url,
        roles=roles,
    )
    return asset, None


def _collect_image_sources(value: Any, out: list[str] | None = None) -> list[str]:
    if out is None:
        out = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"url", "local_url", "remote_url", "local_path", "path"} and isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, (dict, list)):
                _collect_image_sources(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_image_sources(item, out)
    return out


async def _latest_asset_for_node(project_id: str, node_id: str) -> Asset | None:
    async with session_scope() as session:
        result = await session.exec(
            select(Asset)
            .where(Asset.project_id == project_id, Asset.node_id == node_id)
            .order_by(Asset.created_at.desc())
        )
        return result.first()


async def _register_from_node_output(
    project_id: str,
    store: dict[str, Any],
    *,
    node_id: str,
    mention: str | None = None,
    roles: list[str] | None = None,
    requested_node_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from app.mcp_tools import canvas_tools

    requested = _text(requested_node_id) or node_id
    resolved_node_id = await _resolve_workflow_node_id(project_id, node_id)
    if resolved_node_id:
        node_id = resolved_node_id
    node = await canvas_tools.get_node(node_id)
    if not isinstance(node, dict) or node.get("error"):
        return None, {
            "ok": False,
            "error": "Node not found",
            "error_kind": "node_not_found",
            "node_id": requested,
            "hint": "node_id 使用 node.list/node.get 显示的节点编号；后端会按当前项目自动解析。",
        }
    if node.get("status") != "completed":
        return None, {
            "ok": False,
            "error": "Node is not completed",
            "error_kind": "node_not_completed",
            "node_id": public_node_id_from_dict(node),
            "status": node.get("status"),
        }

    db_asset = await _latest_asset_for_node(project_id, node_id)
    if db_asset:
        return await _register_from_asset_record(
            project_id,
            store,
            asset_id=db_asset.id,
            mention=mention,
            roles=roles,
        )

    output = node.get("output") if isinstance(node.get("output"), dict) else {}
    candidates = _collect_image_sources(output)
    rel_path = ""
    source_path = ""
    url = ""
    for candidate in candidates:
        text = _text(candidate)
        if not text:
            continue
        rel = _rel_path_from_media_url(project_id, text)
        if rel and not rel_path:
            rel_path = rel
        if text.startswith(("http://", "https://")) and not url:
            url = text
        else:
            path = Path(text).expanduser()
            if path.is_absolute() and path.exists() and path.is_file() and not source_path:
                source_path = str(path.resolve())
    if not (rel_path or source_path or url):
        return None, {
            "ok": False,
            "error": "Completed node has no readable image output",
            "error_kind": "node_image_output_missing",
            "node_id": public_node_id_from_dict(node),
        }
    filename = Path(rel_path or source_path or url).name
    asset = _register_reference_asset(
        store,
        rel_path=rel_path or None,
        source_path=source_path or None,
        mention=mention,
        filename=filename,
        node_id=node_id,
        source="node_output",
        url=url or None,
        roles=roles,
    )
    return asset, None


async def _find_completed_image_nodes_by_query(
    project_id: str,
    query: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    from app.mcp_tools import canvas_tools

    needle = _text(query).lower()
    if not needle:
        return []
    nodes = await canvas_tools.list_nodes(project_id)
    matches: list[dict[str, Any]] = []
    image_types = {
        "character",
        "scene",
        "segment_storyboard",
        "image",
        "shot_first_frame",
        "shot_last_frame",
        "segment_story_template",
    }
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("type") not in image_types or node.get("status") != "completed":
            continue
        public_node_id = public_node_id_from_dict(node)
        blob_parts = [
            public_node_id,
            str(node.get("title") or ""),
            str(node.get("type") or ""),
            str(node.get("prompt") or ""),
        ]
        for key in ("input", "output"):
            value = node.get(key)
            if value:
                try:
                    blob_parts.append(json.dumps(value, ensure_ascii=False, default=str))
                except TypeError:
                    blob_parts.append(str(value))
        if needle in "\n".join(blob_parts).lower():
            matches.append({
                "node_id": public_node_id,
                "title": node.get("title"),
                "type": node.get("type"),
                "status": node.get("status"),
            })
            if len(matches) >= limit:
                break
    return matches


def _register_from_file_path(
    project_id: str,
    store: dict[str, Any],
    *,
    path: str,
    mention: str | None = None,
    roles: list[str] | None = None,
    source: str = "asset_library",
) -> dict[str, Any]:
    source_path = str(Path(path).expanduser().resolve())
    rel_path = _project_relative_for_path(project_id, source_path)
    target = Path(source_path)
    size = target.stat().st_size if target.exists() and target.is_file() else None
    return _register_reference_asset(
        store,
        rel_path=rel_path,
        source_path=source_path,
        mention=mention,
        filename=target.name,
        mime_type=mimetypes.guess_type(target.name)[0],
        size=size,
        source=source,
        roles=roles,
    )


def _asset_public(
    asset: dict[str, Any],
    *,
    include_analysis: bool = False,
    node_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    keys = [
        "ref_id", "mention", "label", "aliases", "source", "kind", "rel_path",
        "source_path", "url", "asset_id", "node_id", "filename", "mime_type",
        "size", "base64_rel_path", "roles", "status", "created_at",
        "updated_at", "analysis_schema", "analysis_model", "analysis_error", "analysis_warning",
    ]
    public = {key: asset.get(key) for key in keys if asset.get(key) not in (None, "", [], {})}
    if public.get("node_id") and node_id_map:
        public["node_id"] = node_id_map.get(str(public["node_id"]), str(public["node_id"]))
    if include_analysis and isinstance(asset.get("analysis"), dict):
        public["analysis"] = asset["analysis"]
    elif isinstance(asset.get("analysis"), dict):
        analysis = asset["analysis"]
        public["analysis_summary"] = {
            key: analysis.get(key)
            for key in ("summary", "style_name", "style_tags", "prompt_fragment")
            if analysis.get(key)
        }
    reference_input = _reference_input_value(asset)
    if reference_input:
        public["reference_input"] = reference_input
    return public


async def _image_data_url(project_id: str, asset: dict[str, Any]) -> str:
    base64_rel_path = _text(asset.get("base64_rel_path"))
    if base64_rel_path:
        try:
            return read_image_base64_data_url(project_id, base64_rel_path)
        except Exception:
            pass

    target: Path | None = None
    rel_path = _text(asset.get("rel_path"))
    if rel_path:
        target = _project_storage_file(project_id, rel_path)
    source_path = _text(asset.get("source_path"))
    if source_path:
        candidate = Path(source_path).expanduser().resolve()
        if candidate.exists():
            target = candidate
    if target is not None:
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(str(target))
        mime = mimetypes.guess_type(target.name)[0] or "image/png"
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    url = _text(asset.get("url"))
    if url.startswith("http://") or url.startswith("https://"):
        import httpx

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(url)
        response.raise_for_status()
        mime = response.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
        encoded = base64.b64encode(response.content).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    fallback = _reference_input_value(asset)
    if not fallback:
        raise FileNotFoundError("reference asset has no readable image source")
    target = _project_storage_file(project_id, fallback)
    if target is None:
        raise FileNotFoundError(fallback)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(fallback)
    mime = mimetypes.guess_type(target.name)[0] or "image/png"
    encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def _analyze_image_with_llm(
    project_id: str,
    asset: dict[str, Any],
    *,
    user_context: str | None = None,
) -> dict[str, Any]:
    from app.services.llm_service import llm_service

    data_url = await _image_data_url(project_id, asset)
    system = (
        "你是专业视频美术指导和视觉参考分析师。只根据图片可见信息分析，不要编造不可见事实。"
        "输出严格 JSON object。"
    )
    requested = {
        "summary": "一句话概括图片内容和可复用价值",
        "subject": "画面主体",
        "style_name": "风格名称",
        "style_tags": ["风格标签"],
        "color_palette": ["主色/辅色"],
        "lighting": "光线",
        "composition": "构图",
        "camera_language": "镜头语言",
        "texture": "材质/质感",
        "mood": "情绪氛围",
        "usable_roles": ["style_reference|character_reference|scene_reference|composition_reference"],
        "prompt_fragment": "可直接并入图片/视频提示词的中文风格片段",
        "negative_constraints": ["生成时应避免的偏差"],
    }
    text = (
        "分析这张参考图，面向短剧/视频创作复用。\n"
        f"用户上下文:{user_context or ''}\n"
        f"图片引用:{asset.get('mention')} source={_reference_input_value(asset)}\n"
        "必须输出这些字段:\n"
        f"{json.dumps(requested, ensure_ascii=False)}"
    )
    result = await llm_service.generate(
        task_type="image_understanding",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        system=system,
        project_id=project_id,
    )
    data = _json_object(str(result.get("content") or ""))
    if not data:
        raise ValueError("vision model did not return JSON")
    data["source_ref_id"] = asset.get("ref_id")
    data["source_mention"] = asset.get("mention")
    return {
        "analysis": data,
        "model": result.get("model"),
        "usage": result.get("usage"),
    }


def _fallback_analysis(asset: dict[str, Any]) -> dict[str, Any]:
    filename = _text(asset.get("filename") or Path(str(asset.get("rel_path") or "")).name)
    mention = _text(asset.get("mention")) or "@图"
    return {
        "summary": f"{mention} 是用户上传的图片参考，当前尚未完成视觉模型识别。",
        "subject": filename,
        "style_name": "",
        "style_tags": [],
        "color_palette": [],
        "lighting": "",
        "composition": "",
        "camera_language": "",
        "texture": "",
        "mood": "",
        "usable_roles": asset.get("roles") or ["visual_reference"],
        "prompt_fragment": f"参考用户上传图片 {mention} 的视觉特征。",
        "negative_constraints": [],
        "source_ref_id": asset.get("ref_id"),
        "source_mention": mention,
        "analysis_unavailable": True,
    }


async def _save_state(project_id: str, state: dict[str, Any]) -> dict[str, Any]:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found"}
        project.state_json = json.dumps(state, ensure_ascii=False, default=str)
        session.add(project)
        await session.commit()
    return {"ok": True}


async def _load_state(project_id: str) -> tuple[dict[str, Any] | None, str | None]:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return None, "Project not found"
        try:
            return json.loads(project.state_json or "{}"), None
        except json.JSONDecodeError:
            return {}, None


def _upsert_binding(
    store: dict[str, Any],
    asset: dict[str, Any],
    *,
    role: str,
    apply_to: list[str] | None = None,
    blueprint_id: str | None = None,
) -> dict[str, Any]:
    ref_id = str(asset.get("ref_id") or "")
    role = role or "visual_reference"
    for binding in _as_list(store.get("bindings")):
        if (
            isinstance(binding, dict)
            and binding.get("ref_id") == ref_id
            and binding.get("role") == role
            and binding.get("blueprint_id") == blueprint_id
        ):
            binding["apply_to"] = apply_to or binding.get("apply_to") or ["all_visual_nodes"]
            binding["updated_at"] = _now_iso()
            return binding
    binding = {
        "binding_id": f"rb_{uuid.uuid4().hex[:10]}",
        "ref_id": ref_id,
        "mention": asset.get("mention"),
        "role": role,
        "apply_to": apply_to or ["all_visual_nodes"],
        "blueprint_id": blueprint_id,
        "created_at": _now_iso(),
    }
    store.setdefault("bindings", []).append(binding)
    return binding


def _apply_binding_to_state_blueprint(state: dict[str, Any], asset: dict[str, Any], binding: dict[str, Any]) -> None:
    pending = state.get("pending_video_blueprint_request")
    if isinstance(pending, dict):
        refs = pending.setdefault("reference_images", [])
        reference_input = _reference_input_value(asset)
        if isinstance(refs, list) and not any(
            isinstance(item, dict)
            and (
                item.get("ref_id") == asset.get("ref_id")
                or item.get("rel_path") == reference_input
                or item.get("reference_input") == reference_input
            )
            for item in refs
        ):
            refs.append({
                "ref_id": asset.get("ref_id"),
                "mention": asset.get("mention"),
                "rel_path": reference_input or asset.get("rel_path"),
                "reference_input": reference_input,
                "source_path": asset.get("source_path"),
                "asset_id": asset.get("asset_id"),
                "node_id": asset.get("node_id"),
                "filename": asset.get("filename"),
                "usage": binding.get("role"),
            })
        pending.setdefault("reference_bindings", []).append(binding)


def _checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reference_record_from_asset(asset: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    analysis = asset.get("analysis") if isinstance(asset.get("analysis"), dict) else {}
    reference_input = _reference_input_value(asset)
    return {
        "ref_id": asset.get("ref_id"),
        "mention": asset.get("mention"),
        "label": asset.get("label"),
        "rel_path": reference_input or asset.get("rel_path"),
        "reference_input": reference_input,
        "source_path": asset.get("source_path"),
        "asset_id": asset.get("asset_id"),
        "node_id": asset.get("node_id"),
        "filename": asset.get("filename"),
        "usage": binding.get("role") or "visual_reference",
        "analysis_summary": analysis.get("summary"),
        "style_name": analysis.get("style_name"),
        "style_tags": analysis.get("style_tags") if isinstance(analysis.get("style_tags"), list) else [],
        "prompt_fragment": analysis.get("prompt_fragment"),
        "negative_constraints": analysis.get("negative_constraints") if isinstance(analysis.get("negative_constraints"), list) else [],
    }


def _apply_binding_to_active_blueprint_file(
    project_id: str,
    state: dict[str, Any],
    asset: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any] | None:
    index = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
    if not index or not index.get("file_json"):
        return None
    path = Path(settings.PROJECT_ROOT) / str(index.get("file_json"))
    if not path.exists():
        return {"ok": False, "error": "active blueprint file not found", "file_json": index.get("file_json")}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"active blueprint read failed: {exc}"}
    if not isinstance(doc, dict):
        return {"ok": False, "error": "active blueprint JSON must be object"}

    record = _reference_record_from_asset(asset, binding)
    refs = doc.setdefault("reference_images", [])
    if isinstance(refs, list):
        replaced = False
        for index_, item in enumerate(refs):
            if isinstance(item, dict) and (
                item.get("ref_id") == record.get("ref_id")
                or item.get("rel_path") == record.get("rel_path")
            ):
                refs[index_] = {**item, **record}
                replaced = True
                break
        if not replaced:
            refs.append(record)
    bindings = doc.setdefault("reference_bindings", [])
    if isinstance(bindings, list) and not any(
        isinstance(item, dict) and item.get("binding_id") == binding.get("binding_id")
        for item in bindings
    ):
        bindings.append(binding)

    doc["updated_at"] = _now_iso()

    try:
        from app.agent.project_blueprint import (
            render_blueprint_view_model,
            sync_blueprint_outline_document,
        )

        sync_blueprint_outline_document(doc)
        checksum = _checksum(doc)
        index["checksum"] = checksum
        index["updated_at"] = doc["updated_at"]
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        markdown_rel = index.get("file_markdown")
        if markdown_rel:
            markdown_path = Path(settings.PROJECT_ROOT) / str(markdown_rel)
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown = doc.get("outline_document") if isinstance(doc.get("outline_document"), dict) else {}
            markdown_path.write_text(str(markdown.get("content") or ""), encoding="utf-8")
        view_model_rel = index.get("file_view_model")
        if view_model_rel:
            view_model_path = Path(settings.PROJECT_ROOT) / str(view_model_rel)
            view_model_path.parent.mkdir(parents=True, exist_ok=True)
            view_model_path.write_text(
                json.dumps(render_blueprint_view_model(doc, index), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
    except Exception as exc:
        return {"ok": False, "error": f"active blueprint write failed: {exc}"}
    return {"ok": True, "blueprint_id": doc.get("id"), "checksum": checksum}


async def _save_asset_to_user_memory(project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    from app.mcp_tools.memory_tools import memory_save_user_fact

    analysis = asset.get("analysis") if isinstance(asset.get("analysis"), dict) else {}
    content = {
        "type": "visual_style_reference",
        "label": asset.get("label") or asset.get("mention"),
        "mention": asset.get("mention"),
        "style_name": analysis.get("style_name"),
        "style_tags": analysis.get("style_tags") or [],
        "prompt_fragment": analysis.get("prompt_fragment"),
        "negative_constraints": analysis.get("negative_constraints") or [],
        "source_project_id": project_id,
        "source_ref_id": asset.get("ref_id"),
    }
    return await memory_save_user_fact(
        content=json.dumps(content, ensure_ascii=False, default=str),
        kind="visual_style_reference",
        source_project_id=project_id,
    )


async def reference_manage(
    project_id: str,
    action: str,
    rel_path: str | None = None,
    source_path: str | None = None,
    library_path: str | None = None,
    asset_id: str | None = None,
    node_id: str | None = None,
    url: str | None = None,
    mention: str | None = None,
    ref_id: str | None = None,
    query: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    attachment_aliases: list[str] | None = None,
    attachment_roles: list[list[str]] | None = None,
    role: str | None = None,
    roles: list[str] | None = None,
    alias: str | None = None,
    apply_to: list[str] | None = None,
    user_context: str | None = None,
    include_analysis: bool = False,
    save_user_memory: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Manage project visual reference assets.

    Actions:
      register / register_asset / register_file / ingest_attachments / list /
      resolve / get / alias / analyze / bind_to_blueprint / save_to_user_memory.
    """
    action = _text(action)
    state, error = await _load_state(project_id)
    if state is None:
        return {"ok": False, "error": error or "Project not found"}
    store = _reference_store(state)
    node_id_map = await _node_public_id_map(project_id)
    requested_node_id = _text(node_id)
    if requested_node_id:
        node_id = await _resolve_workflow_node_id(project_id, requested_node_id)

    if action == "ingest_attachments":
        registered = _register_attachments(
            store,
            attachments or [],
            attachment_aliases=attachment_aliases,
            attachment_roles=attachment_roles,
        )
        await _save_state(project_id, state)
        return {
            "ok": True,
            "action": action,
            "assets": [
                _asset_public(asset, include_analysis=include_analysis, node_id_map=node_id_map)
                for asset in registered
            ],
            "total": len(_as_list(store.get("assets"))),
        }

    if action in {"register_asset", "register_asset_record"}:
        if not asset_id:
            return {"ok": False, "error": "register_asset requires asset_id", "error_kind": "missing_asset_id"}
        asset, asset_error = await _register_from_asset_record(
            project_id,
            store,
            asset_id=asset_id,
            mention=mention,
            roles=roles or ([role] if role else None),
        )
        if asset_error:
            return asset_error
        await _save_state(project_id, state)
        return {"ok": True, "action": action, "asset": _asset_public(asset or {}, include_analysis=True, node_id_map=node_id_map)}

    if action in {"register_file", "register_library_asset"}:
        path = _text(library_path or source_path or rel_path)
        if not path:
            return {"ok": False, "error": "register_file requires source_path/library_path", "error_kind": "missing_source_path"}
        target = Path(path).expanduser().resolve()
        if not target.exists() or not target.is_file():
            return {"ok": False, "error": "Reference image file not found", "error_kind": "file_not_found", "path": path}
        asset = _register_from_file_path(
            project_id,
            store,
            path=str(target),
            mention=mention,
            roles=roles or ([role] if role else None),
            source="asset_library" if action == "register_library_asset" else "file",
        )
        await _save_state(project_id, state)
        return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map)}

    if action == "register":
        if asset_id:
            asset, asset_error = await _register_from_asset_record(
                project_id,
                store,
                asset_id=asset_id,
                mention=mention,
                roles=roles or ([role] if role else None),
            )
            if asset_error:
                return asset_error
            await _save_state(project_id, state)
            return {"ok": True, "action": action, "asset": _asset_public(asset or {}, include_analysis=True, node_id_map=node_id_map)}
        if node_id:
            asset, asset_error = await _register_from_node_output(
                project_id,
                store,
                node_id=node_id,
                mention=mention,
                roles=roles or ([role] if role else None),
                requested_node_id=requested_node_id,
            )
            if asset_error:
                return asset_error
            await _save_state(project_id, state)
            return {"ok": True, "action": action, "asset": _asset_public(asset or {}, include_analysis=True, node_id_map=node_id_map)}
        if query:
            matches = await _find_completed_image_nodes_by_query(project_id, query)
            if len(matches) == 1 and matches[0].get("node_id"):
                asset, asset_error = await _register_from_node_output(
                    project_id,
                    store,
                    node_id=str(matches[0]["node_id"]),
                    mention=mention,
                    roles=roles or ([role] if role else None),
                    requested_node_id=str(matches[0]["node_id"]),
                )
                if asset_error:
                    return asset_error
                await _save_state(project_id, state)
                return {
                    "ok": True,
                    "action": action,
                    "matched_by_query": query,
                    "matched_node": matches[0],
                    "asset": _asset_public(asset or {}, include_analysis=True, node_id_map=node_id_map),
                }
            return {
                "ok": False,
                "error": "query did not resolve to exactly one completed image node",
                "error_kind": "ambiguous_node_query" if matches else "node_not_found",
                "query": query,
                "candidates": matches,
                "hint": "请先用 node.list(query=...) 或从 candidates 选择节点编号，再 reference.manage(action='register', node_id=..., mention=...)。",
            }
        if source_path or library_path:
            path = _text(source_path or library_path)
            rel = _rel_path_from_media_url(project_id, path)
            if rel:
                asset = _register_reference_asset(
                    store,
                    rel_path=rel,
                    mention=mention,
                    filename=Path(rel).name,
                    roles=roles or ([role] if role else None),
                )
                await _save_state(project_id, state)
                return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map)}
            target = Path(path).expanduser().resolve()
            if not target.exists() or not target.is_file():
                return {"ok": False, "error": "Reference image file not found", "error_kind": "file_not_found", "path": path}
            asset = _register_from_file_path(
                project_id,
                store,
                path=str(target),
                mention=mention,
                roles=roles or ([role] if role else None),
            )
            await _save_state(project_id, state)
            return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map)}
        if not (rel_path or url):
            return {
                "ok": False,
                "error": "register requires rel_path, source_path/library_path, asset_id, node_id, query, or url",
                "error_kind": "missing_reference_source",
            }
        asset = _register_reference_asset(
            store,
            rel_path=rel_path,
            url=url,
            mention=mention,
            roles=roles or ([role] if role else None),
        )
        await _save_state(project_id, state)
        return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map)}

    if action == "list":
        return {
            "ok": True,
            "action": action,
            "assets": [
                _asset_public(asset, include_analysis=include_analysis, node_id_map=node_id_map)
                for asset in _as_list(store.get("assets"))
                if isinstance(asset, dict)
            ],
            "bindings": _as_list(store.get("bindings")),
        }

    asset = _find_asset(
        store,
        ref_id=ref_id,
        mention=mention,
        rel_path=rel_path,
        source_path=source_path or library_path,
        asset_id=asset_id,
        node_id=node_id,
        url=url,
        query=query,
    )
    if not asset:
        available = [
            {
                "ref_id": item.get("ref_id"),
                "mention": item.get("mention"),
                "aliases": item.get("aliases") or [],
                "filename": item.get("filename"),
            }
            for item in _as_list(store.get("assets"))
            if isinstance(item, dict)
        ]
        usage_hint = (
            "设置别名时，mention/ref_id/source_path 必须指向已有参考图，alias 才是新名字；"
            "例如 action='alias', mention='@图1', alias='@红衣女侠'。"
            if action == "alias"
            else "先用 action='list' 查看可用 @图，或用 action='register/ingest_attachments' 注册上传图片。"
        )
        return {
            "ok": False,
            "error": "Reference asset not found",
            "error_kind": "reference_not_found",
            "hint": usage_hint,
            "available_assets": available,
        }

    if action in {"resolve", "get"}:
        return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=include_analysis or action == "get", node_id_map=node_id_map)}

    if action == "alias":
        if not alias:
            return {"ok": False, "error": "alias action requires alias", "error_kind": "missing_alias"}
        normalized = _normalize_mention(alias, alias)
        asset["mention"] = normalized
        asset["label"] = normalized.lstrip("@")
        asset["aliases"] = list(dict.fromkeys([*_as_list(asset.get("aliases")), normalized, normalized.lstrip("@")]))
        asset["updated_at"] = _now_iso()
        await _save_state(project_id, state)
        return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map)}

    if action == "analyze":
        if asset.get("status") in {"analyzed", "analysis_unavailable"} and asset.get("analysis") and not force:
            return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map), "cached": True}
        try:
            analysis_result = await _analyze_image_with_llm(project_id, asset, user_context=user_context)
            asset["analysis"] = analysis_result["analysis"]
            asset["analysis_model"] = analysis_result.get("model")
            asset["analysis_schema"] = REFERENCE_ANALYSIS_SCHEMA
            asset["status"] = "analyzed"
            asset.pop("analysis_error", None)
            asset.pop("analysis_warning", None)
            if analysis_result["analysis"].get("usable_roles"):
                asset["roles"] = sorted(set([*_as_list(asset.get("roles")), *analysis_result["analysis"].get("usable_roles")]))
            usage = analysis_result.get("usage")
        except Exception as exc:
            asset["analysis"] = _fallback_analysis(asset)
            asset["analysis_schema"] = REFERENCE_ANALYSIS_SCHEMA
            asset["analysis_warning"] = str(exc)[:500]
            asset.pop("analysis_error", None)
            asset["status"] = "analysis_unavailable"
            usage = None
        asset["updated_at"] = _now_iso()
        await _save_state(project_id, state)
        result: dict[str, Any] = {
            "ok": True,
            "action": action,
            "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map),
            "analysis_available": asset.get("status") == "analyzed",
        }
        if usage:
            result["usage"] = usage
        if asset.get("analysis_warning"):
            result["warning"] = asset.get("analysis_warning")
            result["warning_kind"] = "image_analysis_unavailable"
            result["hint"] = "参考图已登记，但视觉模型分析失败；配置 vision-capable image_understanding 模型后可 force=True 重试。"
        return result

    if action == "bind_to_blueprint":
        binding = _upsert_binding(
            store,
            asset,
            role=role or (_as_list(asset.get("roles"))[0] if _as_list(asset.get("roles")) else "visual_reference"),
            apply_to=apply_to,
            blueprint_id=(state.get("project_blueprint") or {}).get("id") if isinstance(state.get("project_blueprint"), dict) else None,
        )
        _apply_binding_to_state_blueprint(state, asset, binding)
        blueprint_update = _apply_binding_to_active_blueprint_file(project_id, state, asset, binding)
        await _save_state(project_id, state)
        return {
            "ok": True,
            "action": action,
            "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map),
            "binding": binding,
            "blueprint_update": blueprint_update,
        }

    if action == "save_to_user_memory":
        if not save_user_memory:
            return {
                "ok": False,
                "error": "save_to_user_memory requires save_user_memory=true and explicit user request",
                "error_kind": "explicit_memory_consent_required",
            }
        memory = await _save_asset_to_user_memory(project_id, asset)
        return {"ok": True, "action": action, "asset": _asset_public(asset, include_analysis=True, node_id_map=node_id_map), "memory": memory}

    return {"ok": False, "error": f"Unsupported reference action: {action}", "error_kind": "unsupported_action"}


async def describe_image_reference(project_id: str, rel_path: str, user_context: str | None = None) -> dict[str, Any]:
    """Compatibility helper for media.describe_image."""
    registered = await reference_manage(
        project_id=project_id,
        action="register",
        rel_path=rel_path,
    )
    if not registered.get("ok"):
        return registered
    return await reference_manage(
        project_id=project_id,
        action="analyze",
        rel_path=rel_path,
        user_context=user_context,
        include_analysis=True,
    )
