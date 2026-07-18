"""Project-scoped media generation history.

Only media that has been explicitly attached to a canvas node is registered
here. The index lets users restore generated media after deleting a node without
promoting temporary files, such as image edit previews, into project history.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from PIL import Image, ImageOps

from app.config import settings
from app.db.models import WorkflowNode
from app.services import media_history


MEDIA_HISTORY_DIRS: dict[str, str] = {
    "image": "generated_images",
    "video": "generated_videos",
    "audio": "generated_audio",
}
MEDIA_HISTORY_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"),
    "video": (".mp4", ".webm", ".mov", ".m4v"),
    "audio": (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"),
}
INDEX_FILENAME = "media_history.json"


def parse_json_dict(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_json_value(raw: object) -> object:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def project_root(project_id: str) -> Path:
    if not project_id or "/" in project_id or "\\" in project_id or ".." in project_id:
        raise ValueError("Invalid project_id")
    return settings.storage_path_resolved / project_id


def index_path(project_id: str) -> Path:
    return project_root(project_id) / INDEX_FILENAME


def kind_from_rel_path(rel_path: str) -> str | None:
    normalized = rel_path.strip().replace("\\", "/")
    for kind, dirname in MEDIA_HISTORY_DIRS.items():
        if normalized.startswith(f"{dirname}/"):
            return kind
    suffix = Path(normalized.split("?", 1)[0]).suffix.lower()
    for kind, extensions in MEDIA_HISTORY_EXTENSIONS.items():
        if suffix in extensions:
            return kind
    return None


def rel_path_from_ref(project_id: str, ref: Any) -> str | None:
    if not isinstance(ref, str):
        return None
    text = ref.strip()
    if not text:
        return None
    media_prefix = f"/api/media/{project_id}/"
    if text.startswith(media_prefix):
        text = text[len(media_prefix):]
    elif text.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
        pass
    else:
        return None
    text = text.split("?", 1)[0].split("#", 1)[0].lstrip("/").replace("\\", "/")
    if not text or ".." in text.split("/"):
        return None
    if text.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
        return text
    kind = kind_from_rel_path(text)
    if kind:
        return f"{MEDIA_HISTORY_DIRS[kind]}/{text}"
    return f"generated_images/{text}"


def media_path_from_rel_path(project_id: str, rel_path: str) -> Path:
    root = project_root(project_id)
    normalized = rel_path.strip().replace("\\", "/").lstrip("/")
    if not normalized.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
        raise ValueError("Invalid media path")
    target = (root / normalized).resolve()
    allowed_roots = [(root / dirname).resolve() for dirname in MEDIA_HISTORY_DIRS.values()]
    for allowed_root in allowed_roots:
        try:
            target.relative_to(allowed_root)
            return target
        except ValueError:
            continue
    raise ValueError("Path outside storage")


def item_id(project_id: str, rel_path: str) -> str:
    digest = hashlib.sha1(f"{project_id}:{rel_path}".encode("utf-8")).hexdigest()[:18]
    return f"media_{digest}"


def media_url(project_id: str, rel_path: str) -> str:
    return f"/api/media/{project_id}/{rel_path}"


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if path.suffix.lower() not in MEDIA_HISTORY_EXTENSIONS["image"] or path.suffix.lower() == ".svg":
        return None, None
    try:
        with Image.open(path) as image:
            oriented = ImageOps.exif_transpose(image)
            return int(oriented.width), int(oriented.height)
    except (OSError, ValueError):
        return None, None


def file_payload(project_id: str, rel_path: str, path: Path) -> dict[str, Any] | None:
    kind = kind_from_rel_path(rel_path)
    if kind not in {"image", "video", "audio"}:
        return None
    stat = path.stat()
    mime_type, _ = mimetypes.guess_type(path.name)
    created_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
    payload: dict[str, Any] = {
        "id": item_id(project_id, rel_path),
        "project_id": project_id,
        "kind": kind,
        "rel_path": rel_path,
        "url": media_url(project_id, rel_path),
        "filename": path.name,
        "title": path.stem,
        "created_at": created_at,
        "updated_at": created_at,
        "size": stat.st_size,
        "mime_type": mime_type,
        "source": "index",
        "source_node_id": None,
        "source_node_title": None,
        "prompt": None,
    }
    if kind == "image":
        width, height = image_dimensions(path)
        if width and height:
            payload.update({"width": width, "height": height, "resolution": f"{width}x{height}"})
    return payload


def output_for_item(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "image")
    url = str(item.get("url") or "")
    output: dict[str, Any] = {
        "type": kind,
        "status": "completed",
        "url": url,
        "local_url": url,
    }
    if kind == "image":
        image = {"url": url, "local_url": url}
        for key in ("width", "height", "resolution"):
            if item.get(key) is not None:
                output[key] = item[key]
                image[key] = item[key]
        output["images"] = [image]
    elif kind == "video":
        output["video"] = {"url": url, "local_url": url}
    elif kind == "audio":
        output["audio"] = {"url": url, "local_url": url, "format": item.get("mime_type")}
    return output


def prompt_from_node(node: WorkflowNode, output: Any = None, history_entry: dict[str, Any] | None = None) -> str:
    current_input = parse_json_dict(node.input_json)
    if history_entry and isinstance(history_entry.get("prompt"), str) and history_entry.get("prompt", "").strip():
        return str(history_entry["prompt"]).strip()
    return media_history.prompt_from_state(output, current_input, node.prompt)


def merge_item(items: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    rel_path = str(item.get("rel_path") or "")
    if not rel_path:
        return
    current = items.get(rel_path)
    if current is None:
        items[rel_path] = item
        return
    merged = {**current, **{key: value for key, value in item.items() if value not in (None, "")}}
    if item.get("source") == "index" and current.get("source") == "node":
        merged["source"] = "node"
        merged["source_node_id"] = current.get("source_node_id")
        merged["source_node_title"] = current.get("source_node_title")
        merged["prompt"] = current.get("prompt")
        merged["title"] = current.get("title")
    items[rel_path] = merged


def add_output_items(
    *,
    project_id: str,
    node: WorkflowNode,
    output: Any,
    items: dict[str, dict[str, Any]],
    history_entry: dict[str, Any] | None = None,
) -> None:
    prompt = prompt_from_node(node, output, history_entry)
    entry_created_at = str(history_entry.get("created_at") or "") if history_entry else ""
    for ref in media_history.collect_media_refs(output):
        rel_path = rel_path_from_ref(project_id, ref)
        if not rel_path:
            continue
        try:
            path = media_path_from_rel_path(project_id, rel_path)
        except ValueError:
            continue
        if not path.exists() or not path.is_file():
            continue
        payload = file_payload(project_id, rel_path, path)
        if not payload:
            continue
        payload.update({
            "source": "node",
            "source_node_id": node.id,
            "source_node_title": node.title,
            "title": node.title or payload["title"],
            "prompt": prompt or None,
        })
        if entry_created_at:
            payload["created_at"] = entry_created_at
        merge_item(items, payload)


def explicit_items_from_node(project_id: str, node: WorkflowNode) -> list[dict[str, Any]]:
    if node.type not in {"image", "video", "audio"}:
        return []
    items: dict[str, dict[str, Any]] = {}
    output = parse_json_value(node.output_json)
    if output is not None:
        add_output_items(project_id=project_id, node=node, output=output, items=items)
    for entry in media_history.media_history_from_output(output):
        add_output_items(
            project_id=project_id,
            node=node,
            output=entry.get("output"),
            items=items,
            history_entry=entry,
        )
    return list(items.values())


def load_index(project_id: str) -> list[dict[str, Any]]:
    path = index_path(project_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def write_index(project_id: str, items: list[dict[str, Any]]) -> None:
    root = project_root(project_id)
    root.mkdir(parents=True, exist_ok=True)
    normalized = sorted(
        items,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    path = index_path(project_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"items": normalized}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def hydrate_index_item(project_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    rel_path = str(item.get("rel_path") or "")
    if not rel_path:
        return None
    try:
        path = media_path_from_rel_path(project_id, rel_path)
    except ValueError:
        return None
    if not path.exists() or not path.is_file():
        return None
    payload = file_payload(project_id, rel_path, path)
    if not payload:
        return None
    merged = {**payload, **{key: value for key, value in item.items() if value not in (None, "")}}
    merged["id"] = item_id(project_id, rel_path)
    merged["project_id"] = project_id
    merged["url"] = media_url(project_id, rel_path)
    merged["filename"] = path.name
    merged["size"] = path.stat().st_size
    if not merged.get("source"):
        merged["source"] = "index"
    return merged


def register_items(project_id: str, new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for item in load_index(project_id):
        hydrated = hydrate_index_item(project_id, item)
        if hydrated:
            merge_item(items, hydrated)
    for item in new_items:
        rel_path = str(item.get("rel_path") or "")
        if not rel_path:
            continue
        hydrated = hydrate_index_item(project_id, item)
        if hydrated:
            merge_item(items, hydrated)
    values = list(items.values())
    write_index(project_id, values)
    return values


def register_node_outputs(project_id: str, node: WorkflowNode) -> list[dict[str, Any]]:
    return register_items(project_id, explicit_items_from_node(project_id, node))


def register_nodes_outputs(project_id: str, nodes: list[WorkflowNode]) -> list[dict[str, Any]]:
    new_items: list[dict[str, Any]] = []
    for node in nodes:
        new_items.extend(explicit_items_from_node(project_id, node))
    return register_items(project_id, new_items)


async def list_items(project_id: str, db: AsyncSession) -> list[dict[str, Any]]:
    active_items: list[dict[str, Any]] = []
    result = await db.exec(select(WorkflowNode).where(WorkflowNode.project_id == project_id))
    for node in result.all():
        active_items.extend(explicit_items_from_node(project_id, node))
    items = register_items(project_id, active_items)
    return sorted(
        items,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )


async def find_item(project_id: str, item_id_value: str, db: AsyncSession) -> dict[str, Any] | None:
    for item in await list_items(project_id, db):
        if str(item.get("id") or "") == item_id_value:
            return item
    return None


def remove_item(project_id: str, item_id_value: str) -> dict[str, Any] | None:
    kept: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for item in load_index(project_id):
        rel_path = str(item.get("rel_path") or "")
        if not rel_path:
            continue
        current_id = item_id(project_id, rel_path)
        if current_id == item_id_value:
            removed = item
            continue
        hydrated = hydrate_index_item(project_id, item)
        if hydrated:
            kept.append(hydrated)
    write_index(project_id, kept)
    return removed
