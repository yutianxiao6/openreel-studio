"""Asset library — one local folder for reusable creative assets.

Assets are organized by kind first, then by user-chosen category folders:

  <asset_root>/
    ├─ 人物/<style-or-role>/
    ├─ 场景/<style-or-place>/
    ├─ 分镜/<category>/
    └─ ...
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
from app.services.asset_library_paths import effective_asset_library
from app.services.node_ids import next_node_display_id, node_display_id_allocation
from app.services.node_public_ids import (
    looks_like_internal_node_id,
    looks_like_public_node_id,
    public_node_id_from_model,
    resolve_internal_node_id,
)
from sqlmodel import select


_PROJECT_KINDS = {
    "character", "scene", "storyboard",
}
_LIBRARY_KINDS = set(_PROJECT_KINDS)
_SHARED_KINDS = _LIBRARY_KINDS
_PROJECT_KIND_DIR = {
    "character": "人物",
    "scene": "场景",
    "storyboard": "分镜",
}
_LEGACY_KIND_DIRS = {
    "character": ["characters"],
    "scene": ["scenes"],
    "storyboard": ["storyboards"],
}

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv", ".yaml", ".yml"}
_ASSET_META_SUFFIX = ".openreel.json"
_GENERIC_ASSET_TITLES = {"", "未命名", "未命名图片", "图片节点", "image", "image node"}


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿\-]+", "_", name).strip("_")
    return cleaned or "untitled"


def _category_name(category: str | None, fallback: str = "未分类") -> str:
    text = str(category or "").strip()
    return text or fallback


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
    raw = lib.get("root") or lib.get("shared_root") or lib.get("project_root")
    if not raw:
        return []
    return [Path(str(raw)).expanduser().resolve()]


def _library_root(lib: dict[str, Any]) -> Path:
    roots = _library_roots(effective_asset_library(lib, ensure_dirs=True))
    if not roots:
        roots = _library_roots(effective_asset_library({}, ensure_dirs=True))
    return roots[0]


def _kind_dir_name(kind: str) -> str:
    return _PROJECT_KIND_DIR.get(kind, _slug(kind))


def _kind_dir(root: Path, kind: str) -> Path:
    return root / _kind_dir_name(kind)


def _kind_dir_candidates(root: Path, kind: str) -> list[Path]:
    names = [_kind_dir_name(kind), *_LEGACY_KIND_DIRS.get(kind, [])]
    candidates: list[Path] = []
    for name in names:
        for candidate in (root / name, root / "shared" / name):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


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


def _asset_sidecar_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{_ASSET_META_SUFFIX}")


def _is_sidecar_file(path: Path) -> bool:
    return path.name.startswith(".") and path.name.endswith(_ASSET_META_SUFFIX)


def _display_title_from_name(path: Path) -> str:
    title = re.sub(r"[_\-]+", " ", path.stem).strip()
    return title or path.stem or path.name


def _read_asset_sidecar(path: Path) -> dict[str, Any]:
    meta_path = _asset_sidecar_path(path)
    if not meta_path.exists() or not meta_path.is_file():
        return {}
    try:
        parsed = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_asset_sidecar(path: Path, metadata: dict[str, Any]) -> None:
    compact = {
        key: value
        for key, value in metadata.items()
        if value not in (None, "", [], {})
    }
    if not compact:
        return
    compact["asset_file"] = path.name
    compact["updated_at"] = datetime.utcnow().isoformat()
    _asset_sidecar_path(path).write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")


def _move_asset_sidecar(source: Path, target: Path) -> None:
    source_meta = _asset_sidecar_path(source)
    if not source_meta.exists() or not source_meta.is_file():
        return
    target_meta = _asset_sidecar_path(target)
    target_meta.parent.mkdir(parents=True, exist_ok=True)
    if target_meta.exists():
        target_meta.unlink()
    shutil.move(str(source_meta), str(target_meta))


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if path.suffix.lower() not in _IMAGE_SUFFIXES:
        return None, None
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return None, None


def _asset_file_payload(path: Path, **extra: Any) -> dict[str, Any]:
    metadata = _read_asset_sidecar(path)
    width = metadata.get("width")
    height = metadata.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        detected_width, detected_height = _image_dimensions(path)
        width = width if isinstance(width, int) else detected_width
        height = height if isinstance(height, int) else detected_height
    title = str(metadata.get("title") or "").strip() or _display_title_from_name(path)
    prompt = str(metadata.get("prompt") or "").strip()
    mime_type = _mime_for_path(path)
    payload = {
        **extra,
        "path": str(path),
        "name": path.name,
        "title": title,
        "size": path.stat().st_size,
        "mime_type": mime_type,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
    }
    if isinstance(width, int) and isinstance(height, int):
        payload["width"] = width
        payload["height"] = height
        payload["resolution"] = f"{width}x{height}"
    if prompt:
        payload["prompt"] = prompt
        payload["prompt_snippet"] = prompt[:180]
    return payload


async def _metadata_for_source(project_id: str, source: str, resolved_path: Path) -> dict[str, Any]:
    text = str(source or "").strip()
    metadata: dict[str, Any] = {
        "title": _display_title_from_name(resolved_path),
        "source": text,
        "mime_type": _mime_for_path(resolved_path),
    }
    if text.startswith("asset:"):
        asset_id = text[len("asset:"):].strip()
        async with session_scope() as session:
            asset = await session.get(Asset, asset_id)
        if asset and asset.project_id == project_id:
            if asset.name:
                metadata["title"] = asset.name
            if asset.prompt:
                metadata["prompt"] = asset.prompt
            if asset.type:
                metadata["source_type"] = asset.type
            if asset.metadata_json:
                try:
                    parsed = json.loads(asset.metadata_json)
                    if isinstance(parsed, dict):
                        for key in ("width", "height", "resolution", "model", "provider"):
                            if parsed.get(key) is not None:
                                metadata[key] = parsed[key]
                except (json.JSONDecodeError, TypeError):
                    pass
    elif text.startswith("node:") or looks_like_public_node_id(text) or looks_like_internal_node_id(text):
        node_ref = text[len("node:"):].strip() if text.startswith("node:") else text
        async with session_scope() as session:
            resolved_node_id = await resolve_internal_node_id(session, project_id, node_ref)
            node = await session.get(WorkflowNode, resolved_node_id)
        if node and node.project_id == project_id:
            title = str(node.title or "").strip()
            if title and title.lower() not in _GENERIC_ASSET_TITLES:
                metadata["title"] = title
            prompt = str(node.prompt or "").strip()
            if not prompt and node.input_json:
                try:
                    parsed_input = json.loads(node.input_json)
                    if isinstance(parsed_input, dict):
                        prompt = str(parsed_input.get("prompt") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    pass
            if prompt:
                metadata["prompt"] = prompt
            if node.output_json:
                try:
                    parsed_output = json.loads(node.output_json)
                    if isinstance(parsed_output, dict):
                        for key in ("width", "height", "resolution", "model", "provider"):
                            if parsed_output.get(key) is not None:
                                metadata[key] = parsed_output[key]
                        stages = parsed_output.get("stages")
                        if isinstance(stages, list):
                            for stage in stages:
                                if not isinstance(stage, dict):
                                    continue
                                if stage.get("width") and "width" not in metadata:
                                    metadata["width"] = stage.get("width")
                                if stage.get("height") and "height" not in metadata:
                                    metadata["height"] = stage.get("height")
                                if stage.get("prompt") and "prompt" not in metadata:
                                    metadata["prompt"] = stage.get("prompt")
                except (json.JSONDecodeError, TypeError):
                    pass
    width, height = _image_dimensions(resolved_path)
    if width and "width" not in metadata:
        metadata["width"] = width
    if height and "height" not in metadata:
        metadata["height"] = height
    return metadata


def _project_target_dir(lib: dict[str, Any], project_title: str, episode: int, kind: str) -> Path:
    if kind not in _LIBRARY_KINDS:
        raise ValueError(f"kind 必须是 {sorted(_LIBRARY_KINDS)} 之一")
    target_dir = _kind_dir(_library_root(lib), kind) / f"第{int(episode)}集"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _project_media_rel_path(project_id: str, url: str | None) -> str | None:
    url = str(url or "").strip()
    prefix = f"/api/media/{project_id}/"
    if not url.startswith(prefix):
        return None
    rel_path = url[len(prefix):].lstrip("/")
    return rel_path if rel_path.startswith("generated_images/") else f"generated_images/{rel_path}"


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
    root: str | None = None,
    library_root: str | None = None,
    project_root: str | None = None,
    shared_root: str | None = None,
) -> dict[str, Any]:
    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)
    selected_root = root or library_root or shared_root or project_root
    if selected_root:
        p = Path(selected_root).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        lib["root"] = str(p)
        lib["project_root"] = str(p)
        lib["shared_root"] = str(p)
    state["asset_library"] = lib
    await _set_state(project_id, state)
    return {"ok": True, "asset_library": lib}


async def assets_get_library_path(project_id: str) -> dict[str, Any]:
    state = await _get_state(project_id)
    raw_lib = state.get("asset_library")
    lib = effective_asset_library(raw_lib, ensure_dirs=True)
    return {"configured": True, "using_default": not bool(raw_lib), **lib}


def _project_episode_dir(lib: dict[str, Any], project_title: str, episode: int) -> Path:
    root = lib.get("project_root")
    if not root:
        root = effective_asset_library({}, ensure_dirs=True)["project_root"]
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
    if kind not in _LIBRARY_KINDS:
        return {"error": f"kind 必须是 {sorted(_LIBRARY_KINDS)} 之一"}

    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)

    title = (state.get("metadata") or {}).get("title") or project_id
    target_dir = _project_target_dir(lib, title, episode, kind)

    src = await _resolve_source(project_id, source)
    source_metadata = await _metadata_for_source(project_id, source, src)
    stem = _slug(name or str(source_metadata.get("title") or "") or src.stem)
    target = _target_path_without_overwrite(target_dir / f"{stem}{src.suffix}")

    shutil.copy2(src, target)
    _write_asset_sidecar(target, {
        **source_metadata,
        "title": name or source_metadata.get("title") or _display_title_from_name(target),
        "library": "asset",
        "kind": kind,
        "category": f"第{int(episode)}集",
    })
    return {"ok": True, "kind": kind, "category": f"第{int(episode)}集", "path": str(target)}


def _shared_category_dir(lib: dict[str, Any], kind: str, category: str) -> Path:
    base = _kind_dir(_library_root(lib), kind) / _slug(category)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _category_dirs_for_scan(kind_dir: Path, category: str | None) -> list[Path]:
    if not category:
        return sorted(p for p in kind_dir.iterdir() if p.is_dir())
    names = [_slug(category), str(category).strip()]
    dirs: list[Path] = []
    for name in names:
        if not name:
            continue
        candidate = kind_dir / name
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _filter_asset_items(
    items: list[dict[str, Any]],
    *,
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> list[dict[str, Any]]:
    if not (query or regex or pattern):
        return items
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
    return filtered


async def _list_library_items(
    project_id: str,
    *,
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
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)
    root = _library_root(lib)
    if not root.exists():
        return {"ok": True, "items": [], "root": str(root), "shared_root": str(root), "project_dir": str(root), "count": 0}

    item_kinds = [kind] if kind else sorted(_LIBRARY_KINDS)
    items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item_kind in item_kinds:
        item_kind = str(item_kind or "").strip().lower()
        if item_kind not in _LIBRARY_KINDS:
            continue
        for kind_dir in _kind_dir_candidates(root, item_kind):
            if not kind_dir.exists() or not kind_dir.is_dir():
                continue
            for file_path in sorted(p for p in kind_dir.iterdir() if p.is_file() and not _is_sidecar_file(p)):
                resolved = str(file_path.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                items.append(_asset_file_payload(
                    file_path,
                    library="asset",
                    kind=item_kind,
                    category="未分类",
                ))
            for category_dir in _category_dirs_for_scan(kind_dir, category):
                if not category_dir.exists() or not category_dir.is_dir():
                    continue
                for file_path in sorted(p for p in category_dir.iterdir() if p.is_file() and not _is_sidecar_file(p)):
                    resolved = str(file_path.resolve())
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    items.append(_asset_file_payload(
                        file_path,
                        library="asset",
                        kind=item_kind,
                        category=category_dir.name,
                    ))

    items = _filter_asset_items(
        items,
        query=query,
        regex=regex,
        pattern=pattern,
        case_sensitive=case_sensitive,
    )
    return {"ok": True, "items": items, "root": str(root), "shared_root": str(root), "project_dir": str(root), "count": len(items)}


async def assets_save_to_shared(
    project_id: str,
    kind: str,
    source: str,
    category: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    if kind not in _LIBRARY_KINDS:
        return {"error": f"kind 必须是 {sorted(_LIBRARY_KINDS)} 之一"}
    category_name = _category_name(category)

    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)

    target_dir = _shared_category_dir(lib, kind, category_name)
    src = await _resolve_source(project_id, source)
    source_metadata = await _metadata_for_source(project_id, source, src)
    stem = _slug(name or str(source_metadata.get("title") or "") or src.stem)
    target = _target_path_without_overwrite(target_dir / f"{stem}{src.suffix}")
    shutil.copy2(src, target)
    _write_asset_sidecar(target, {
        **source_metadata,
        "title": name or source_metadata.get("title") or _display_title_from_name(target),
        "library": "asset",
        "kind": kind,
        "category": category_name,
    })
    return {"ok": True, "kind": kind, "category": category_name, "path": str(target)}


async def assets_list_categories(
    project_id: str,
    library: str = "all",
    kind: str | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """List categories in the single asset library."""
    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)
    root = _library_root(lib)
    result: dict[str, Any] = {
        "ok": True,
        "root": str(root),
        "items": [],
        "shared": [],
        "project": [],
        "project_kinds": sorted(_LIBRARY_KINDS),
        "shared_kinds": sorted(_LIBRARY_KINDS),
        "kinds": sorted(_LIBRARY_KINDS),
    }
    for item_kind in sorted(_LIBRARY_KINDS):
        if kind and item_kind != kind:
            continue
        for kind_dir in _kind_dir_candidates(root, item_kind):
            if not kind_dir.exists():
                continue
            for category_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
                count = sum(
                    1
                    for child in category_dir.iterdir()
                    if child.is_file() and not _is_sidecar_file(child)
                )
                item = {
                    "library": "asset",
                    "kind": item_kind,
                    "category": category_dir.name,
                    "path": str(category_dir),
                    "count": count,
                }
                result["items"].append(item)
                result["shared"].append(item)

    return result


async def assets_create_category(
    project_id: str,
    kind: str,
    category: str | None = None,
    library: str = "asset",
    episode: int | None = None,
) -> dict[str, Any]:
    """Create an asset-library classification bucket."""
    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)
    item_kind = str(kind or "").strip().lower()
    if item_kind not in _LIBRARY_KINDS:
        return {"error": f"kind 必须是 {sorted(_LIBRARY_KINDS)} 之一"}
    category_name = _category_name(category, f"第{int(episode)}集" if episode is not None else "未分类")
    target_dir = _shared_category_dir(lib, item_kind, category_name)
    return {
        "ok": True,
        "library": "asset",
        "kind": item_kind,
        "category": target_dir.name,
        "path": str(target_dir),
    }


async def assets_move_asset(
    project_id: str,
    path: str,
    kind: str,
    category: str | None = None,
    library: str = "asset",
    episode: int | None = None,
    name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Move an existing asset-library file to another classification bucket."""
    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)
    source = Path(path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        return {"error": f"文件不存在: {path}"}
    if not any(_path_is_within(source, root) for root in _library_roots(lib)):
        return {"error": "只能移动已配置资产库范围内的文件"}

    item_kind = str(kind or "").strip().lower()
    try:
        if item_kind not in _LIBRARY_KINDS:
            return {"error": f"kind 必须是 {sorted(_LIBRARY_KINDS)} 之一"}
        category_name = _category_name(category, f"第{int(episode)}集" if episode is not None else "未分类")
        target_dir = _shared_category_dir(lib, item_kind, category_name)
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
    _move_asset_sidecar(source, target)
    existing_metadata = _read_asset_sidecar(target)
    _write_asset_sidecar(target, {
        **existing_metadata,
        "library": "asset",
        "kind": item_kind,
        "category": category_name,
    })
    return {
        "ok": True,
        "from": str(source),
        "path": str(target),
        "library": "asset",
        "kind": item_kind,
        "category": category_name,
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
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)
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
    async with node_display_id_allocation(project_id):
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
                if resolved_type == "image":
                    width, height = _image_dimensions(src)
                    if width and height:
                        dimensions = {"width": width, "height": height, "resolution": f"{width}x{height}"}
                        fields.update(dimensions)
                        output.update(dimensions)
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
    result = await _list_library_items(
        project_id,
        kind=kind,
        query=query,
        regex=regex,
        pattern=pattern,
        case_sensitive=case_sensitive,
    )
    if episode is not None and not result.get("error"):
        category = f"第{int(episode)}集"
        result["items"] = [item for item in result.get("items", []) if item.get("category") == category]
        result["count"] = len(result["items"])
    return result


async def assets_list_shared(
    project_id: str,
    kind: str | None = None,
    category: str | None = None,
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    return await _list_library_items(
        project_id,
        kind=kind,
        category=category,
        query=query,
        regex=regex,
        pattern=pattern,
        case_sensitive=case_sensitive,
    )


async def assets_read_asset(project_id: str, path: str) -> dict[str, Any]:
    state = await _get_state(project_id)
    lib = effective_asset_library(state.get("asset_library"), ensure_dirs=True)

    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        return {"error": f"文件不存在: {path}"}

    if not any(_path_is_within(p, root) for root in _library_roots(lib)):
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
