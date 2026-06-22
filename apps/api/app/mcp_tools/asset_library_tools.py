"""Asset library — user-designated permanent storage for project assets.

Two roots, both set by the user via dialogue:
  - project_root: per-project assets, organized by episode/kind
  - shared_root:  cross-project reusable assets (character/scene templates)

The library is **append-only** — there is no delete tool. Re-use is achieved
by reading and re-saving (copy), never by mutating.

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
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.models import Asset, Project, WorkflowNode
from app.db.session import session_scope
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
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


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿\-]+", "_", name).strip("_")
    return cleaned or "untitled"


def _ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


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
        stmt = select(Asset).where(Asset.project_id == project_id, Asset.node_id == node_id)
        rows = (await session.exec(stmt)).all()
        node = await session.get(WorkflowNode, node_id)
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
    sub = _PROJECT_KIND_DIR[kind]
    target_dir = ep_dir / sub if sub else ep_dir
    target_dir.mkdir(parents=True, exist_ok=True)

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
