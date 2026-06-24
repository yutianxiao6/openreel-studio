"""Asset library — user-designated permanent storage for project assets.

Two roots, both set by the user via dialogue:
  - project_root: per-project assets, organized by episode/kind
  - shared_root:  cross-project reusable assets (character/scene templates)

The library supports explicit user-driven classification and moves. Deletion is
still left to the front-end/filesystem control plane.

Storage layouts:

  <project_root>/<project_title>/episodes/ep<NN>/
    ├─ script.txt
    ├─ characters/<role>_<style>_<ts>.png
    ├─ scenes/
    ├─ first_frames/shot_<n>_first.png
    ├─ last_frames/shot_<n>_last.png
    ├─ storyboards/
    └─ story_template.md

  <shared_root>/
    ├─ characters/{male,female}_{young,old,child}/
    └─ scenes/<style>_<location_type>/
"""
from __future__ import annotations

import json
import mimetypes
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.config import settings
from app.db.models import Asset, Project, WorkflowNode
from app.db.session import session_scope
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
from app.services.node_ids import next_node_display_id
from app.services.node_public_ids import (
    looks_like_internal_node_id,
    looks_like_public_node_id,
    public_node_id_from_model,
    resolve_internal_node_id,
)
from sqlmodel import select


_PROJECT_KINDS = {
    "script", "character", "scene", "first_frame", "last_frame",
    "storyboard", "story_template",
}
_SHARED_KINDS = {"character", "scene"}
_PROJECT_KIND_DIR = {
    "script": "",
    "character": "characters",
    "scene": "scenes",
    "first_frame": "first_frames",
    "last_frame": "last_frames",
    "storyboard": "storyboards",
    "story_template": "",
}

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv", ".yaml", ".yml"}


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿\-]+", "_", name).strip("_")
    return cleaned or "untitled"


def _ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _storage_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("STORAGE_PATH", "STORAGE_DIR"):
        raw = getattr(settings, key, None)
        if not raw:
            continue
        root = Path(str(raw)).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    return roots


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _library_roots(lib: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for key in ("project_root", "shared_root"):
        raw = lib.get(key)
        if raw:
            roots.append(Path(str(raw)).expanduser().resolve())
    return roots


def _mime_for_path(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _asset_kind_from_path(path: Path, mime_type: str = "", explicit: str | None = None) -> str:
    explicit_kind = str(explicit or "").strip().lower()
    if explicit_kind in {"text", "image", "video", "audio"}:
        return explicit_kind
    suffix = path.suffix.lower()
    mime = mime_type.lower()
    if mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
        return "image"
    if mime.startswith("video/") or suffix in _VIDEO_SUFFIXES:
        return "video"
    if mime.startswith("audio/") or suffix in _AUDIO_SUFFIXES:
        return "audio"
    return "text"


def _web_url_for_file(project_id: str, path: Path, lib: dict[str, Any] | None = None) -> str:
    resolved = path.expanduser().resolve()
    for root in _storage_roots():
        project_root = root / project_id
        if _path_is_within(resolved, project_root):
            rel = resolved.relative_to(project_root).as_posix()
            if rel.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
                return f"/api/media/{project_id}/{rel}"
            if rel.startswith("uploads/"):
                return f"/api/uploads/{project_id}/file/{rel}"
    for root in _library_roots(lib or {}):
        if _path_is_within(resolved, root):
            return f"/api/assets/{project_id}/preview?path={quote(str(resolved), safe='')}"
    return ""


def _target_path_without_overwrite(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}_{_ts()}{suffix}")


def _project_target_dir(lib: dict[str, Any], project_title: str, episode: int, kind: str) -> Path:
    if kind not in _PROJECT_KINDS:
        raise ValueError(f"kind 必须是 {sorted(_PROJECT_KINDS)} 之一")
    ep_dir = _project_episode_dir(lib, project_title, episode)
    sub = _PROJECT_KIND_DIR[kind]
    target_dir = ep_dir / sub if sub else ep_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _project_media_rel_path(project_id: str, url: str | None) -> str | None:
    url = str(url or "").strip()
    prefix = f"/api/media/{project_id}/"
    if not url.startswith(prefix):
        return None
    return f"generated_images/{url[len(prefix):].lstrip('/')}"


async def _resolve_asset_record_source(project_id: str, asset_id: str) -> Path | None:
    async with session_scope() as session:
        asset = await session.get(Asset, asset_id)
        if not asset or asset.project_id != project_id:
            return None
        meta = {}
        if asset.metadata_json:
            try:
                parsed = json.loads(asset.metadata_json)
                meta = parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        source = asset.path or meta.get("local_path")
        if source:
            p = Path(str(source)).expanduser().resolve()
            if p.exists() and p.is_file():
                return p
        rel = _project_media_rel_path(project_id, asset.url or meta.get("local_url"))
        if rel:
            for key in ("STORAGE_DIR", "STORAGE_PATH"):
                p = Path(getattr(settings, key, "./storage")).resolve() / project_id / rel
                if p.exists() and p.is_file():
                    return p
    return None


async def _resolve_node_asset_source(project_id: str, node_id: str) -> Path | None:
    async with session_scope() as session:
        resolved_node_id = await resolve_internal_node_id(session, project_id, node_id)
        stmt = select(Asset).where(Asset.project_id == project_id, Asset.node_id == resolved_node_id)
        rows = (await session.exec(stmt)).all()
        node = await session.get(WorkflowNode, resolved_node_id)
    for asset in rows:
        p = await _resolve_asset_record_source(project_id, asset.id)
        if p:
            return p
    if node and node.project_id == project_id and node.output_json:
        try:
            output = json.loads(node.output_json)
        except (json.JSONDecodeError, TypeError):
            output = {}
        for candidate in _collect_output_sources(output):
            p = _resolve_output_source_path(project_id, candidate)
            if p:
                return p
    return None


def _collect_output_sources(value: Any) -> list[str]:
    sources: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"path", "local_path", "url", "local_url"} and isinstance(item, str) and item:
                sources.append(item)
            elif isinstance(item, (dict, list)):
                sources.extend(_collect_output_sources(item))
    elif isinstance(value, list):
        for item in value:
            sources.extend(_collect_output_sources(item))
    return sources


def _resolve_output_source_path(project_id: str, source: str) -> Path | None:
    text = str(source or "").strip()
    if not text or text.startswith(("http://", "https://", "data:")):
        return None
    rel = _project_media_rel_path(project_id, text)
    candidates: list[Path] = []
    if rel:
        for key in ("STORAGE_DIR", "STORAGE_PATH"):
            candidates.append(Path(getattr(settings, key, "./storage")).resolve() / project_id / rel)
    path = Path(text).expanduser()
    if path.is_absolute():
        candidates.append(path.resolve())
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


async def _resolve_source(project_id: str, source: str) -> Path:
    """Source can be asset:<id>, node:<id>, absolute path, or a path relative to STORAGE_DIR/<project_id>."""
    source = str(source or "").strip()
    if source.startswith("asset:"):
        resolved = await _resolve_asset_record_source(project_id, source[len("asset:"):].strip())
        if resolved:
            return resolved
        raise FileNotFoundError(f"Asset source not found: {source}")
    if source.startswith("node:"):
        resolved = await _resolve_node_asset_source(project_id, source[len("node:"):].strip())
        if resolved:
            return resolved
        raise FileNotFoundError(f"Node image source not found: {source}")
    if looks_like_public_node_id(source) or looks_like_internal_node_id(source):
        resolved = await _resolve_node_asset_source(project_id, source)
        if resolved:
            return resolved
    p = Path(source)
    if p.is_absolute() and p.exists():
        return p
    storage_root = Path(getattr(settings, "STORAGE_DIR", "./data/storage")).resolve()
    candidate = (storage_root / project_id / source).resolve()
    if candidate.exists():
        return candidate
    if p.exists():
        return p.resolve()
    raise FileNotFoundError(f"Source file not found: {source}")


async def _get_state(project_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")
        return json.loads(project.state_json or "{}")


async def _set_state(project_id: str, state: dict[str, Any]) -> None:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()


async def assets_set_library_path(
    project_id: str,
    project_root: str | None = None,
    shared_root: str | None = None,
) -> dict[str, Any]:
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    if project_root:
        p = Path(project_root).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        lib["project_root"] = str(p)
    if shared_root:
        p = Path(shared_root).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        lib["shared_root"] = str(p)
    state["asset_library"] = lib
    await _set_state(project_id, state)
    return {"ok": True, "asset_library": lib}


async def assets_get_library_path(project_id: str) -> dict[str, Any]:
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    if not lib.get("project_root") and not lib.get("shared_root"):
        return {
            "configured": False,
            "error": "资产库尚未配置。请先告诉我项目库和公用素材库的本地路径。",
        }
    return {"configured": True, **lib}


def _project_episode_dir(lib: dict[str, Any], project_title: str, episode: int) -> Path:
    root = lib.get("project_root")
    if not root:
        raise ValueError("project_root 未设置,请先通过项目设置或资产库 API 配置")
    base = Path(root) / _slug(project_title) / "episodes" / f"ep{int(episode):02d}"
    base.mkdir(parents=True, exist_ok=True)
    return base


async def assets_save_to_project(
    project_id: str,
    episode: int,
    kind: str,
    source: str,
    name: str | None = None,
) -> dict[str, Any]:
    if kind not in _PROJECT_KINDS:
        return {"error": f"kind 必须是 {sorted(_PROJECT_KINDS)} 之一"}

    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    if not lib.get("project_root"):
        return {"error": "project_root 未配置。请告诉我项目资产库的本地路径。"}

    title = (state.get("metadata") or {}).get("title") or project_id
    ep_dir = _project_episode_dir(lib, title, episode)
    target_dir = _project_target_dir(lib, title, episode, kind)

    src = await _resolve_source(project_id, source)
    suffix = src.suffix
    if kind == "script":
        target = ep_dir / "script.txt"
    elif kind == "story_template":
        target = ep_dir / "story_template.md"
    else:
        stem = _slug(name) if name else f"{src.stem}_{_ts()}"
        target = target_dir / f"{stem}{suffix}"

    shutil.copy2(src, target)
    return {"ok": True, "kind": kind, "episode": episode, "path": str(target)}


def _shared_category_dir(lib: dict[str, Any], kind: str, category: str) -> Path:
    root = lib.get("shared_root")
    if not root:
        raise ValueError("shared_root 未设置,请先通过项目设置或资产库 API 配置")
    base = Path(root) / f"{kind}s" / _slug(category)
    base.mkdir(parents=True, exist_ok=True)
    return base


async def assets_save_to_shared(
    project_id: str,
    kind: str,
    category: str,
    source: str,
    name: str | None = None,
) -> dict[str, Any]:
    if kind not in _SHARED_KINDS:
        return {"error": f"kind 必须是 {sorted(_SHARED_KINDS)} 之一"}
    if not str(category or "").strip():
        return {"error": "保存到共享资产库需要 category 分类"}

    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    if not lib.get("shared_root"):
        return {"error": "shared_root 未配置。请告诉我公用素材库的本地路径。"}

    target_dir = _shared_category_dir(lib, kind, category)
    src = await _resolve_source(project_id, source)
    stem = _slug(name) if name else f"{src.stem}_{_ts()}"
    target = target_dir / f"{stem}{src.suffix}"
    shutil.copy2(src, target)
    return {"ok": True, "kind": kind, "category": category, "path": str(target)}


async def assets_list_categories(
    project_id: str,
    library: str = "all",
    kind: str | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """List project and shared asset-library categories."""
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    library_key = str(library or "all").strip().lower()
    if library_key not in {"all", "project", "shared"}:
        return {"error": "library 必须是 all、project 或 shared"}
    result: dict[str, Any] = {
        "ok": True,
        "project": [],
        "shared": [],
        "project_kinds": sorted(_PROJECT_KINDS),
        "shared_kinds": sorted(_SHARED_KINDS),
    }

    if library_key in {"all", "project"} and lib.get("project_root"):
        title = (state.get("metadata") or {}).get("title") or project_id
        episodes_root = Path(lib["project_root"]) / _slug(title) / "episodes"
        if episodes_root.exists():
            ep_dirs = (
                [episodes_root / f"ep{int(episode):02d}"]
                if episode is not None
                else sorted(p for p in episodes_root.iterdir() if p.is_dir())
            )
            for ep_dir in ep_dirs:
                if not ep_dir.exists() or not ep_dir.is_dir():
                    continue
                for item_kind, sub in _PROJECT_KIND_DIR.items():
                    if kind and item_kind != kind:
                        continue
                    category_dir = ep_dir / sub if sub else ep_dir
                    if not category_dir.exists():
                        continue
                    count = sum(1 for child in category_dir.iterdir() if child.is_file())
                    result["project"].append({
                        "library": "project",
                        "episode": ep_dir.name,
                        "kind": item_kind,
                        "path": str(category_dir),
                        "count": count,
                    })

    if library_key in {"all", "shared"} and lib.get("shared_root"):
        shared_root = Path(lib["shared_root"])
        for item_kind in sorted(_SHARED_KINDS):
            if kind and item_kind != kind:
                continue
            kind_dir = shared_root / f"{item_kind}s"
            if not kind_dir.exists():
                continue
            for category_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
                count = sum(1 for child in category_dir.iterdir() if child.is_file())
                result["shared"].append({
                    "library": "shared",
                    "kind": item_kind,
                    "category": category_dir.name,
                    "path": str(category_dir),
                    "count": count,
                })

    return result


async def assets_create_category(
    project_id: str,
    library: str,
    kind: str,
    category: str | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """Create an asset-library classification bucket."""
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    library_key = str(library or "").strip().lower()
    item_kind = str(kind or "").strip().lower()
    if library_key == "shared":
        if item_kind not in _SHARED_KINDS:
            return {"error": f"kind 必须是 {sorted(_SHARED_KINDS)} 之一"}
        if not str(category or "").strip():
            return {"error": "共享资产库分类需要 category"}
        if not lib.get("shared_root"):
            return {"error": "shared_root 未配置。请先在设置或资产面板配置公用素材库路径。"}
        target_dir = _shared_category_dir(lib, item_kind, str(category))
        return {
            "ok": True,
            "library": "shared",
            "kind": item_kind,
            "category": target_dir.name,
            "path": str(target_dir),
        }
    if library_key == "project":
        if item_kind not in _PROJECT_KINDS:
            return {"error": f"kind 必须是 {sorted(_PROJECT_KINDS)} 之一"}
        if episode is None:
            return {"error": "项目资产库分类需要 episode"}
        if not lib.get("project_root"):
            return {"error": "project_root 未配置。请先在设置或资产面板配置项目资产库路径。"}
        title = (state.get("metadata") or {}).get("title") or project_id
        target_dir = _project_target_dir(lib, title, int(episode), item_kind)
        return {
            "ok": True,
            "library": "project",
            "episode": int(episode),
            "kind": item_kind,
            "path": str(target_dir),
        }
    return {"error": "library 必须是 project 或 shared"}


async def assets_move_asset(
    project_id: str,
    path: str,
    library: str,
    kind: str,
    category: str | None = None,
    episode: int | None = None,
    name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Move an existing asset-library file to another classification bucket."""
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    source = Path(path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        return {"error": f"文件不存在: {path}"}
    if not any(_path_is_within(source, root) for root in _library_roots(lib)):
        return {"error": "只能移动已配置资产库范围内的文件"}

    library_key = str(library or "").strip().lower()
    item_kind = str(kind or "").strip().lower()
    try:
        if library_key == "shared":
            if item_kind not in _SHARED_KINDS:
                return {"error": f"kind 必须是 {sorted(_SHARED_KINDS)} 之一"}
            if not str(category or "").strip():
                return {"error": "共享资产移动需要 category"}
            target_dir = _shared_category_dir(lib, item_kind, str(category))
        elif library_key == "project":
            if item_kind not in _PROJECT_KINDS:
                return {"error": f"kind 必须是 {sorted(_PROJECT_KINDS)} 之一"}
            if episode is None:
                return {"error": "项目资产移动需要 episode"}
            title = (state.get("metadata") or {}).get("title") or project_id
            target_dir = _project_target_dir(lib, title, int(episode), item_kind)
        else:
            return {"error": "library 必须是 project 或 shared"}
    except ValueError as exc:
        return {"error": str(exc)}

    target_name = f"{_slug(name)}{source.suffix}" if str(name or "").strip() else source.name
    target = (target_dir / target_name).resolve()
    if source == target:
        return {"ok": True, "path": str(source), "unchanged": True}
    if target.exists():
        if overwrite:
            target.unlink()
        else:
            target = _target_path_without_overwrite(target)
    shutil.move(str(source), str(target))
    return {
        "ok": True,
        "from": str(source),
        "path": str(target),
        "library": library_key,
        "kind": item_kind,
        "category": category if library_key == "shared" else None,
        "episode": int(episode) if library_key == "project" and episode is not None else None,
    }


async def assets_add_to_canvas(
    project_id: str,
    source: str,
    title: str | None = None,
    node_type: str | None = None,
    x: float | None = None,
    y: float | None = None,
) -> dict[str, Any]:
    """Create a completed canvas node from a generated asset or library file."""
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    try:
        src = await _resolve_source(project_id, source)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    mime_type = _mime_for_path(src)
    resolved_type = _asset_kind_from_path(src, mime_type, node_type)
    web_url = _web_url_for_file(project_id, src, lib)
    if resolved_type in {"image", "video", "audio"} and not web_url:
        return {"error": "该资产不在项目存储或配置的资产库范围内，无法加入画布预览"}

    node_title = (title or src.stem or "资产节点").strip()
    async with session_scope() as session:
        existing_count = len((await session.exec(
            select(WorkflowNode.id).where(WorkflowNode.project_id == project_id)
        )).all())
        now = datetime.utcnow()
        pos_x = float(x) if x is not None else 120.0 + float(existing_count % 4) * 300.0
        pos_y = float(y) if y is not None else 90.0 + float(existing_count // 4) * 220.0
        fields: dict[str, Any] = {
            "source_asset": source,
            "source_path": str(src),
            "mime_type": mime_type,
        }
        if resolved_type == "image":
            fields["references"] = [{"ref": str(src), "role": "source_image"}]
        input_data: dict[str, Any] = {
            "surface": "draft_canvas",
            "title": node_title,
            "fields": fields,
        }
        output: dict[str, Any]
        text_preview = ""
        if resolved_type in {"image", "video", "audio"}:
            output = {
                "type": resolved_type,
                "url": web_url,
                "local_url": web_url,
                "path": str(src),
                "mime_type": mime_type,
                "source": "asset_library",
            }
        else:
            if src.suffix.lower() in _TEXT_SUFFIXES:
                text_preview = src.read_text(encoding="utf-8", errors="replace")[:4000]
            input_data["content"] = text_preview
            output = {
                "type": "text",
                "path": str(src),
                "mime_type": mime_type,
                "source": "asset_library",
                "text_preview": text_preview[:1000],
            }
        node = WorkflowNode(
            project_id=project_id,
            display_id=await next_node_display_id(session, project_id),
            type=resolved_type,
            title=node_title,
            status="completed",
            position_x=pos_x,
            position_y=pos_y,
            input_json=json.dumps(input_data, ensure_ascii=False),
            output_json=json.dumps(output, ensure_ascii=False),
            model_config_json=json.dumps({"surface": "draft_canvas", "_ui_creator": "user"}, ensure_ascii=False),
            prompt=text_preview if resolved_type == "text" and text_preview else None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(node)
        await session.commit()
        await session.refresh(node)
    return {
        "ok": True,
        "node_id": public_node_id_from_model(node),
        "type": resolved_type,
        "title": node.title,
        "path": str(src),
        "url": web_url or None,
    }


async def assets_list_project(
    project_id: str,
    episode: int | None = None,
    kind: str | None = None,
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    root = lib.get("project_root")
    if not root:
        return {"error": "project_root 未配置"}

    title = (state.get("metadata") or {}).get("title") or project_id
    proj_dir = Path(root) / _slug(title) / "episodes"
    if not proj_dir.exists():
        return {"items": [], "project_dir": str(proj_dir)}

    items: list[dict[str, Any]] = []
    ep_dirs = (
        [proj_dir / f"ep{int(episode):02d}"]
        if episode is not None
        else sorted(proj_dir.iterdir())
    )
    for ed in ep_dirs:
        if not ed.exists() or not ed.is_dir():
            continue
        ep_label = ed.name
        for k, sub in _PROJECT_KIND_DIR.items():
            if kind and k != kind:
                continue
            scan_dir = ed / sub if sub else ed
            if not scan_dir.exists():
                continue
            if k in {"script", "story_template"}:
                fname = "script.txt" if k == "script" else "story_template.md"
                f = ed / fname
                if f.exists():
                    items.append({"episode": ep_label, "kind": k, "path": str(f), "size": f.stat().st_size})
            else:
                for f in sorted(scan_dir.iterdir()):
                    if f.is_file():
                        items.append({"episode": ep_label, "kind": k, "path": str(f), "size": f.stat().st_size})
    if query or regex or pattern:
        filtered: list[dict[str, Any]] = []
        for item in items:
            match = match_text(
                search_blob(item),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            if match.get("matched"):
                next_item = dict(item)
                next_item["match"] = {
                    key: value
                    for key, value in match.items()
                    if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
                }
                filtered.append(next_item)
        items = filtered
    return {"items": items, "project_dir": str(proj_dir), "count": len(items)}


async def assets_list_shared(
    project_id: str,
    kind: str | None = None,
    category: str | None = None,
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    root = lib.get("shared_root")
    if not root:
        return {"error": "shared_root 未配置"}

    base = Path(root)
    if not base.exists():
        return {"items": [], "shared_root": str(base)}

    items: list[dict[str, Any]] = []
    kind_dirs = [base / f"{kind}s"] if kind else [base / f"{k}s" for k in _SHARED_KINDS]
    for kd in kind_dirs:
        if not kd.exists():
            continue
        cur_kind = kd.name.rstrip("s")
        cat_dirs = [kd / _slug(category)] if category else sorted(kd.iterdir())
        for cd in cat_dirs:
            if not cd.exists() or not cd.is_dir():
                continue
            for f in sorted(cd.iterdir()):
                if f.is_file():
                    items.append({"kind": cur_kind, "category": cd.name, "path": str(f), "size": f.stat().st_size})
    if query or regex or pattern:
        filtered: list[dict[str, Any]] = []
        for item in items:
            match = match_text(
                search_blob(item),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            if match.get("matched"):
                next_item = dict(item)
                next_item["match"] = {
                    key: value
                    for key, value in match.items()
                    if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
                }
                filtered.append(next_item)
        items = filtered
    return {"items": items, "shared_root": str(base), "count": len(items)}


async def assets_read_asset(project_id: str, path: str) -> dict[str, Any]:
    state = await _get_state(project_id)
    lib = state.get("asset_library") or {}
    project_root = lib.get("project_root")
    shared_root = lib.get("shared_root")

    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        return {"error": f"文件不存在: {path}"}

    ok = False
    for root in (project_root, shared_root):
        if root:
            try:
                p.relative_to(Path(root).resolve())
                ok = True
                break
            except ValueError:
                continue
    if not ok:
        return {"error": "路径不在配置的资产库范围内"}

    suffix = p.suffix.lower()
    info: dict[str, Any] = {
        "path": str(p),
        "size": p.stat().st_size,
        "modified_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
    }
    if suffix in {".txt", ".md"}:
        info["text"] = p.read_text(encoding="utf-8", errors="replace")
    return info
