"""Blueprint revision helpers.

Story and structure node edits must flow back to the project blueprint. This
module creates a pending revision draft from a node-targeted patch, then applies
that draft after user confirmation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.agent.project_blueprint import (
    blueprint_outline_markdown,
    blueprint_paths,
    render_blueprint_view_model,
    render_blueprint_markdown,
    sync_blueprint_outline_document,
    validate_blueprint_document,
    write_blueprint_files,
)
from app.agent.blueprint_tree import blueprint_exists as semantic_blueprint_exists
from app.agent.blueprint_tree import read_blueprint as read_semantic_blueprint
from app.agent.blueprint_confirmation_state import pending_blueprint_plan
from app.config import settings
from app.db.models import Project, WorkflowNode
from app.db.session import session_scope


STORY_FACT_PATCH_KEYS = {
    "plot",
    "characters",
    "scene_refs",
    "scene_id",
    "duration_seconds",
    "segment_arc",
    "workflow_mode",
    "mode",
    "summary",
    "script",
    "segments",
    "outline",
    "story",
    "episode",
    "output_json",
    "output_data",
}
SEGMENT_PATCH_KEYS = {
    "plot",
    "characters",
    "scene_refs",
    "scene_id",
    "duration_seconds",
    "segment_arc",
    "workflow_mode",
    "mode",
}
SEGMENT_SOURCE_RE = re.compile(r"story\.episodes\[(\d+)]\.segments(?:\[(\d+)])?")
LOW_RISK_PATCH_PATH_RE = re.compile(
    r"^story\.episodes\[\d+](?:\.segments\[\d+])?"
    r"\.(?:plot|description|summary|title|script|segment_arc|dialogue|beats|ending)$"
)
HIGH_RISK_SOURCE_PREFIXES = (
    "characters",
    "scenes",
    "production",
    "visual_strategy",
    "node_projection",
    "constraints",
)
JSON_PATCH_OPS = {"add", "replace", "remove"}
STORY_STRUCTURE_NODE_TYPES = {"text"}


def _checksum(doc: dict[str, Any]) -> str:
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_revision_patch(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coerce_ops(revision_patch: dict[str, Any]) -> list[dict[str, Any]]:
    raw_ops = revision_patch.get("ops")
    if isinstance(raw_ops, str):
        try:
            raw_ops = json.loads(raw_ops)
        except json.JSONDecodeError:
            raw_ops = []
    if not isinstance(raw_ops, list):
        return []
    ops: list[dict[str, Any]] = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op") or "").strip().lower()
        path = str(raw.get("path") or "").strip()
        if op not in JSON_PATCH_OPS or not path:
            continue
        normalized = {"op": op, "path": path}
        if op != "remove":
            normalized["value"] = raw.get("value")
        ops.append(normalized)
    return ops


def _pointer_unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _path_tokens(path: str) -> list[str | int]:
    """Parse JSON pointer or dotted blueprint source path."""
    text = str(path or "").strip()
    if not text:
        return []
    if text.startswith("/"):
        tokens: list[str | int] = []
        for token in text.strip("/").split("/"):
            unescaped = _pointer_unescape(token)
            if unescaped.isdigit():
                tokens.append(int(unescaped))
            else:
                tokens.append(unescaped)
        return tokens

    tokens = []
    for part in text.split("."):
        if not part:
            continue
        cursor = part
        while "[" in cursor and cursor.endswith("]"):
            name, _, rest = cursor.partition("[")
            if name:
                tokens.append(name)
            index_text = rest[:-1]
            if index_text.isdigit():
                tokens.append(int(index_text))
            cursor = ""
        if cursor:
            tokens.append(int(cursor) if cursor.isdigit() else cursor)
    return tokens


def _tokens_to_source_path(tokens: list[str | int]) -> str:
    parts: list[str] = []
    for token in tokens:
        if isinstance(token, int):
            if not parts:
                parts.append(f"[{token}]")
            else:
                parts[-1] = f"{parts[-1]}[{token}]"
        else:
            parts.append(str(token))
    return ".".join(parts)


def _parent_for_path(doc: dict[str, Any], tokens: list[str | int]) -> tuple[Any, str | int] | None:
    if not tokens:
        return None
    cursor: Any = doc
    for token in tokens[:-1]:
        if isinstance(token, int):
            if not isinstance(cursor, list) or token < 0 or token >= len(cursor):
                return None
            cursor = cursor[token]
        else:
            if not isinstance(cursor, dict) or token not in cursor:
                return None
            cursor = cursor[token]
    return cursor, tokens[-1]


def _path_exists(doc: dict[str, Any], source_path: str) -> bool:
    tokens = _path_tokens(source_path)
    parent_info = _parent_for_path(doc, tokens)
    if parent_info is None:
        return False
    parent, key = parent_info
    if isinstance(parent, list):
        return isinstance(key, int) and 0 <= key < len(parent)
    if isinstance(parent, dict):
        return not isinstance(key, int) and str(key) in parent
    return False


def _revision_set_op(doc: dict[str, Any], source_path: str, value: Any) -> dict[str, Any]:
    return {
        "op": "replace" if _path_exists(doc, source_path) else "add",
        "path": source_path,
        "value": value,
    }


def _apply_json_patch_op(doc: dict[str, Any], op: dict[str, Any]) -> str:
    tokens = _path_tokens(str(op.get("path") or ""))
    source_path = _tokens_to_source_path(tokens)
    parent_info = _parent_for_path(doc, tokens)
    if parent_info is None:
        raise ValueError(f"invalid path: {op.get('path')}")
    parent, key = parent_info
    operation = str(op.get("op") or "").lower()
    if isinstance(parent, list):
        if not isinstance(key, int):
            raise ValueError(f"list path requires numeric index: {op.get('path')}")
        if operation == "add":
            if key < 0 or key > len(parent):
                raise ValueError(f"list add index out of range: {op.get('path')}")
            parent.insert(key, op.get("value"))
        elif operation == "replace":
            if key < 0 or key >= len(parent):
                raise ValueError(f"list replace index out of range: {op.get('path')}")
            parent[key] = op.get("value")
        elif operation == "remove":
            if key < 0 or key >= len(parent):
                raise ValueError(f"list remove index out of range: {op.get('path')}")
            parent.pop(key)
        else:
            raise ValueError(f"unsupported op: {operation}")
        return source_path
    if not isinstance(parent, dict) or isinstance(key, int):
        raise ValueError(f"object path requires string key: {op.get('path')}")
    if str(key) == "prompt" and str(parent.get("type") or "").lower() == "text":
        value = op.get("value")
        if operation in {"add", "replace"}:
            parent["content"] = value
            fields = parent.get("fields")
            if isinstance(fields, dict):
                fields["content"] = value
            return _tokens_to_source_path([*tokens[:-1], "content"])
        if operation == "remove":
            parent.pop("content", None)
            fields = parent.get("fields")
            if isinstance(fields, dict):
                fields.pop("content", None)
            return _tokens_to_source_path([*tokens[:-1], "content"])
    if operation == "add":
        parent[str(key)] = op.get("value")
    elif operation == "replace":
        if str(key) not in parent:
            raise ValueError(f"replace path missing: {op.get('path')}")
        parent[str(key)] = op.get("value")
    elif operation == "remove":
        if str(key) not in parent:
            raise ValueError(f"remove path missing: {op.get('path')}")
        parent.pop(str(key), None)
    else:
        raise ValueError(f"unsupported op: {operation}")
    return source_path


def _apply_revision_ops(doc: dict[str, Any], ops: list[dict[str, Any]]) -> list[str]:
    affected: list[str] = []
    for op in ops:
        source_path = _apply_json_patch_op(doc, op)
        if source_path:
            affected.append(source_path)
    return list(dict.fromkeys(affected))


def _risk_for_revision_ops(ops: list[dict[str, Any]], affected_paths: list[str]) -> dict[str, Any]:
    touched_roots = {path.split(".", 1)[0] for path in affected_paths if path}
    high_reasons: list[str] = []
    if not ops:
        high_reasons.append("empty_patch")
    if len(ops) > 5:
        high_reasons.append("too_many_operations")
    if len(touched_roots) > 2:
        high_reasons.append("too_many_sections")
    if any(str(op.get("op")) == "remove" for op in ops):
        high_reasons.append("remove_operation")
    for path in affected_paths:
        if any(path == prefix or path.startswith(f"{prefix}.") or path.startswith(f"{prefix}[") for prefix in HIGH_RISK_SOURCE_PREFIXES):
            high_reasons.append(f"high_risk_path:{path}")
            break
    if high_reasons:
        return {
            "risk": "high",
            "reasons": high_reasons,
            "requires_confirmation": True,
            "operation_count": len(ops),
            "touched_roots": sorted(touched_roots),
        }
    if all(LOW_RISK_PATCH_PATH_RE.match(path) for path in affected_paths):
        return {
            "risk": "low",
            "reasons": [],
            "requires_confirmation": False,
            "operation_count": len(ops),
            "touched_roots": sorted(touched_roots),
        }
    return {
        "risk": "medium",
        "reasons": ["non_low_risk_story_path"],
        "requires_confirmation": True,
        "operation_count": len(ops),
        "touched_roots": sorted(touched_roots),
    }


async def _skip_confirmations_enabled() -> bool:
    try:
        import json5
        from app.config_store.schema import DEFAULT_APP_SETTINGS

        path = Path(settings.PROJECT_ROOT) / "config" / "runtime.jsonc"
        if not path.exists():
            settings_dict = DEFAULT_APP_SETTINGS
        else:
            parsed = json5.loads(path.read_text(encoding="utf-8"))
            settings_dict = parsed.get("app_settings") if isinstance(parsed, dict) else {}
            if not isinstance(settings_dict, dict):
                settings_dict = {}
    except Exception:
        settings_dict = {}
    return bool(settings_dict.get("agent.skip_confirmations"))


def _node_source_paths(node: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for container_key in ("output", "input"):
        container = node.get(container_key)
        if not isinstance(container, dict):
            continue
        raw_paths = container.get("blueprint_source_paths") or container.get("source_blueprint_paths")
        if isinstance(raw_paths, list):
            paths.extend(str(path) for path in raw_paths if path)
    return list(dict.fromkeys(paths))


def _workflow_node_source_paths(node: WorkflowNode) -> list[str]:
    paths: list[str] = []
    for raw_json in (node.output_json, node.input_json):
        data = _json_dict(raw_json)
        for key in ("blueprint_source_paths", "source_blueprint_paths"):
            raw_paths = data.get(key)
            if isinstance(raw_paths, list):
                paths.extend(str(path) for path in raw_paths if path)
    return list(dict.fromkeys(paths))


def _mark_node_blueprint_dirty(
    *,
    node: WorkflowNode,
    index: dict[str, Any],
    pending: dict[str, Any],
    affected_paths: list[str],
    node_paths: list[str],
) -> None:
    output = _json_dict(node.output_json)
    sync = {
        "status": "dirty",
        "reason": "blueprint_revision_applied",
        "blueprint_version": index.get("version"),
        "revision_from_version": pending.get("from_version"),
        "revision_version": pending.get("version"),
        "affected_source_paths": affected_paths,
        "node_source_paths": node_paths,
        "marked_at": _now_iso(),
    }
    output["sync_status"] = "dirty"
    output["blueprint_sync"] = sync
    if not output.get("blueprint_source_paths") and node_paths:
        output["blueprint_source_paths"] = node_paths
    node.output_json = json.dumps(output, ensure_ascii=False, default=str)
    node.updated_at = datetime.utcnow()


def _patch_touches_story_fact(patch: dict[str, Any]) -> bool:
    normalized_keys = {"output_json" if key == "output_data" else key for key in patch}
    return bool(STORY_FACT_PATCH_KEYS & normalized_keys)


def _active_blueprint_file(project_id: str, index: dict[str, Any]) -> Path:
    rel_path = str(index.get("file_json") or blueprint_paths(project_id)["json"])
    return Path(settings.PROJECT_ROOT) / rel_path


def _segment_location(
    doc: dict[str, Any],
    fields: dict[str, Any],
    source_paths: list[str],
) -> tuple[int, int] | None:
    for source_path in source_paths:
        match = SEGMENT_SOURCE_RE.search(source_path)
        if not match:
            continue
        ep_index = _int(match.group(1))
        seg_index = _int(match.group(2)) if match.group(2) is not None else None
        if ep_index is None:
            continue
        if seg_index is not None:
            return ep_index, seg_index
        requested_segment = _int(fields.get("segment_index") or fields.get("index"))
        if requested_segment is not None:
            return ep_index, max(0, requested_segment - 1)

    story = _as_dict(doc.get("story"))
    episodes = _as_list(story.get("episodes"))
    requested_ep = _int(fields.get("episode_number"))
    requested_segment = _int(fields.get("segment_index") or fields.get("index"))
    if requested_segment is None:
        return None
    for ep_index, episode in enumerate(episodes):
        if not isinstance(episode, dict):
            continue
        episode_number = _int(episode.get("episode_number")) or ep_index + 1
        if requested_ep is not None and episode_number != requested_ep:
            continue
        segments = _as_list(episode.get("segments"))
        for seg_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            segment_number = _int(segment.get("segment_index") or segment.get("index")) or seg_index + 1
            if segment_number == requested_segment:
                return ep_index, seg_index
    return None


def _segment_patch_ops(
    doc: dict[str, Any],
    fields: dict[str, Any],
    source_paths: list[str],
    patch: dict[str, Any],
) -> list[dict[str, Any]]:
    if not (SEGMENT_PATCH_KEYS & set(patch)):
        return []
    location = _segment_location(doc, fields, source_paths)
    if location is None:
        return []
    ep_index, seg_index = location
    story = doc.setdefault("story", {})
    if not isinstance(story, dict):
        return []
    episodes = story.get("episodes")
    if not isinstance(episodes, list) or ep_index >= len(episodes):
        return []
    episode = episodes[ep_index]
    if not isinstance(episode, dict):
        return []
    segments = episode.get("segments")
    if not isinstance(segments, list) or seg_index >= len(segments):
        return []
    segment = segments[seg_index]
    if not isinstance(segment, dict):
        return []

    base_path = f"story.episodes[{ep_index}].segments[{seg_index}]"
    ops: list[dict[str, Any]] = []
    if "plot" in patch:
        ops.append(_revision_set_op(doc, f"{base_path}.plot", patch.get("plot")))
    if "duration_seconds" in patch:
        parsed_duration = _int(patch.get("duration_seconds"))
        if parsed_duration:
            ops.append(_revision_set_op(doc, f"{base_path}.duration_seconds", parsed_duration))
    if "segment_arc" in patch:
        ops.append(_revision_set_op(doc, f"{base_path}.segment_arc", patch.get("segment_arc")))
    if "characters" in patch:
        characters = patch.get("characters")
        value = characters if isinstance(characters, list) else [characters]
        ops.append(_revision_set_op(doc, f"{base_path}.cast_refs", value))
    if "scene_refs" in patch:
        scene_refs = patch.get("scene_refs")
        value = scene_refs if isinstance(scene_refs, list) else [scene_refs]
        ops.append(_revision_set_op(doc, f"{base_path}.scene_refs", value))
    if "scene_id" in patch:
        current = segment.get("scene_refs") if isinstance(segment.get("scene_refs"), list) else []
        scene_id = str(patch.get("scene_id") or "").strip()
        if scene_id and scene_id not in current:
            ops.append(_revision_set_op(doc, f"{base_path}.scene_refs", [*current, scene_id]))
    if "workflow_mode" in patch or "mode" in patch:
        mode = patch.get("workflow_mode") or patch.get("mode")
        if mode:
            ops.append(_revision_set_op(doc, f"{base_path}.workflow_mode", mode))
    return ops


def _write_revision_files(project_id: str, doc: dict[str, Any], pending: dict[str, Any]) -> None:
    paths = blueprint_paths(project_id)
    json_path = Path(paths["revision_json_abs"])
    md_path = Path(paths["revision_markdown_abs"])
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_blueprint_markdown(doc, pending), encoding="utf-8")


async def create_pending_revision_from_patch(
    *,
    project_id: str,
    user_request: str,
    revision_patch: dict[str, Any] | str,
    source: str = "blueprint.revise",
    target_node_id: str | None = None,
    target_node_type: str | None = None,
    target_fields: dict[str, Any] | None = None,
    auto_apply: bool | None = None,
) -> dict[str, Any]:
    """Create a scoped pending blueprint revision from model-authored patch ops.

    The active blueprint is never overwritten directly. The model may propose
    JSON-patch-like ops; the backend applies them to a draft copy, evaluates
    risk, persists a pending revision, and optionally auto-applies only low-risk
    small patches when confirmations are globally skipped.
    """
    patch_doc = _coerce_revision_patch(revision_patch)
    ops = _coerce_ops(patch_doc)
    if not ops:
        return {
            "ok": False,
            "error": "蓝图修订必须提供非空 ops，且每项包含 op/path/value。",
            "error_kind": "blueprint_revision_empty_ops",
        }

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found", "error_kind": "project_missing"}
        state = json.loads(project.state_json or "{}")
        active_index = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
        if not active_index:
            pending_blueprint = pending_blueprint_plan(state)
            draft_doc: dict[str, Any] | None = None
            if semantic_blueprint_exists(project_id):
                draft_doc = read_semantic_blueprint(project_id)
            draft_root = draft_doc.get("root") if isinstance(draft_doc, dict) and isinstance(draft_doc.get("root"), dict) else {}
            has_semantic_draft = (
                bool(draft_root.get("children"))
                and str((draft_doc or {}).get("status") or "") in {"drafting", "pending_review"}
            )
            if pending_blueprint or has_semantic_draft:
                return {
                    "ok": True,
                    "status": "draft_revision_not_applied",
                    "needs_draft_edit": True,
                    "message": (
                        "当前蓝图仍在待确认阶段，不能用 blueprint.revise。"
                        "请先用 blueprint.get 查看 pending semantic tree，"
                        "再用 blueprint.update_tree_node 或 blueprint.append_tree_node 修改草稿，"
                        "最后调用 blueprint.finalize_tree_draft 刷新待确认方案。"
                    ),
                    "replacement_tools": [
                        "blueprint.get",
                        "blueprint.update_tree_node",
                        "blueprint.append_tree_node",
                        "blueprint.finalize_tree_draft",
                    ],
                    "received_ops_count": len(ops),
                }
            return {
                "ok": False,
                "error": "当前项目没有 active blueprint，不能创建蓝图修订。",
                "error_kind": "active_blueprint_missing",
            }
        path = _active_blueprint_file(project_id, active_index)
        if not path.exists():
            return {
                "ok": False,
                "error": "active blueprint 文件不存在，不能创建修订。",
                "error_kind": "blueprint_file_missing",
            }
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "error": f"active blueprint 读取失败：{exc}",
                "error_kind": "blueprint_file_read_failed",
            }
        if not isinstance(doc, dict):
            return {
                "ok": False,
                "error": "active blueprint JSON 必须是对象。",
                "error_kind": "blueprint_file_invalid",
            }

        draft_doc = copy.deepcopy(doc)
        try:
            affected_paths = _apply_revision_ops(draft_doc, ops)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "error_kind": "blueprint_revision_patch_invalid",
                "ops": ops,
            }
        risk = _risk_for_revision_ops(ops, affected_paths)
        next_version = int(active_index.get("version") or draft_doc.get("version") or 0) + 1
        draft_doc["version"] = next_version
        draft_doc["status"] = "revision_pending"
        draft_doc["updated_at"] = _now_iso()
        draft_doc["validation_report"] = validate_blueprint_document(draft_doc)
        draft_doc["revision_request"] = {
            "source": source,
            "target_node_id": target_node_id,
            "target_node_type": target_node_type,
            "user_request": user_request,
            "scope": patch_doc.get("scope"),
            "ops": ops,
            "affected_source_paths": affected_paths,
            "affected_refs": patch_doc.get("affected_refs") if isinstance(patch_doc.get("affected_refs"), list) else [],
            "keep_unchanged": patch_doc.get("keep_unchanged") if isinstance(patch_doc.get("keep_unchanged"), list) else [],
            "propagation_plan": patch_doc.get("propagation_plan") if isinstance(patch_doc.get("propagation_plan"), list) else [],
            "risk": risk,
            "created_at": _now_iso(),
        }

        checksum = _checksum(draft_doc)
        paths = blueprint_paths(project_id)
        skip_confirm = await _skip_confirmations_enabled() if auto_apply is None else bool(auto_apply)
        can_auto_apply = bool(skip_confirm and risk.get("risk") == "low")
        pending = {
            "id": draft_doc.get("id") or active_index.get("id"),
            "status": "pending_review",
            "from_version": active_index.get("version"),
            "version": next_version,
            "checksum": checksum,
            "file_json": paths["revision_json"],
            "file_markdown": paths["revision_markdown"],
            "target_node_id": target_node_id,
            "target_node_type": target_node_type,
            "target_fields": target_fields or {},
            "source_paths": affected_paths,
            "applied_source_paths": affected_paths,
            "requested_patch": patch_doc,
            "ops": ops,
            "user_request": user_request,
            "affected_refs": draft_doc["revision_request"]["affected_refs"],
            "keep_unchanged": draft_doc["revision_request"]["keep_unchanged"],
            "propagation_plan": draft_doc["revision_request"]["propagation_plan"],
            "risk": risk,
            "auto_apply_eligible": risk.get("risk") == "low",
            "requires_user_confirm": not can_auto_apply,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        _write_revision_files(project_id, draft_doc, pending)
        state["pending_blueprint_revision"] = pending
        progress = state.get("blueprint_progress") if isinstance(state.get("blueprint_progress"), dict) else {}
        progress["status"] = "revision_pending"
        progress["revision"] = {
            "version": next_version,
            "risk": risk.get("risk"),
            "affected_source_paths": affected_paths,
            "requires_user_confirm": not can_auto_apply,
        }
        state["blueprint_progress"] = progress
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

    if can_auto_apply:
        apply_result = await apply_pending_blueprint_revision(project_id)
        if isinstance(apply_result, dict):
            apply_result["auto_applied"] = True
            apply_result["pending_revision"] = pending
            apply_result["risk"] = risk
        return apply_result
    return {
        "ok": True,
        "requires_blueprint_revision": True,
        "requires_user_confirm": True,
        "status": "pending_review",
        "message": "已生成蓝图修订草稿。用户确认后会应用到 active blueprint，并标记相关下游节点需要同步。",
        "pending_revision": pending,
        "risk": risk,
        "affected_source_paths": affected_paths,
    }


async def create_pending_revision_from_node_patch(
    *,
    node: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Create a pending blueprint revision for story-fact node edits.

    Returns None when the patch is safe for ordinary node.update handling.
    """
    if not isinstance(patch, dict) or not _patch_touches_story_fact(patch):
        return None
    node_type = str(node.get("type") or "")
    if node_type not in STORY_STRUCTURE_NODE_TYPES and not (SEGMENT_PATCH_KEYS & set(patch)):
        return None
    source_paths = _node_source_paths(node)
    if not source_paths:
        return None
    project_id = str(node.get("project_id") or "")
    if not project_id:
        return {
            "ok": False,
            "requires_blueprint_revision": True,
            "error": "节点缺 project_id，无法创建蓝图修订草稿。",
            "error_kind": "node_project_missing",
        }

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found", "error_kind": "project_missing"}
        state = json.loads(project.state_json or "{}")
        active_index = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
        if not active_index:
            return None
        path = _active_blueprint_file(project_id, active_index)
        if not path.exists():
            return {
                "ok": False,
                "requires_blueprint_revision": True,
                "error": "active blueprint 文件不存在，不能直接修改剧情节点。",
                "error_kind": "blueprint_file_missing",
            }
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "requires_blueprint_revision": True,
                "error": f"active blueprint 读取失败：{exc}",
                "error_kind": "blueprint_file_read_failed",
            }
        if not isinstance(doc, dict):
            return {
                "ok": False,
                "requires_blueprint_revision": True,
                "error": "active blueprint JSON 必须是对象。",
                "error_kind": "blueprint_file_invalid",
            }

        fields = _as_dict(node.get("input"))
        ops = _segment_patch_ops(doc, fields, source_paths, patch)
    if not ops:
        pending = {
            "status": "needs_model_revision",
            "target_node_id": node.get("id"),
            "target_node_type": node.get("type"),
            "target_fields": _as_dict(node.get("input")),
            "source_paths": source_paths,
            "requested_patch": patch,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return {
            "ok": False,
            "requires_blueprint_revision": True,
            "error_kind": "blueprint_revision_requires_model",
            "error": "这个节点修改无法安全映射为确定性蓝图 patch，已记录为待模型修订请求，不能直接修改节点输出。",
            "hint": (
                "剧情、分段、结尾或结构修改请调用 blueprint.revise。"
                "如果只是写图片/视频生成提示词，请先 node.list 找到 type=image/video 的节点编号，"
                "再用 node.update 更新该媒体节点的 prompt/input_json；text 剧情节点和 output_json 保持现有内容源。"
            ),
            "pending_revision": pending,
        }
    affected_paths = [str(op.get("path") or "") for op in ops if op.get("path")]
    return await create_pending_revision_from_patch(
        project_id=project_id,
        user_request="用户修改蓝图绑定剧情节点。",
        revision_patch={
            "kind": "blueprint_revision",
            "scope": "node_story_fact",
            "ops": ops,
            "affected_refs": affected_paths,
            "keep_unchanged": ["未被 ops 命中的蓝图字段保持不变"],
            "propagation_plan": [{"target": "blueprint_bound_nodes", "action": "mark_dirty_or_rematerialize"}],
        },
        source="node.update",
        target_node_id=str(node.get("id") or ""),
        target_node_type=str(node.get("type") or ""),
        target_fields=_as_dict(node.get("input")),
    )


def _paths_intersect(left: list[str], right: list[str]) -> bool:
    def _contains(base: str, candidate: str) -> bool:
        return (
            candidate == base
            or candidate.startswith(f"{base}.")
            or candidate.startswith(f"{base}[")
        )

    for lhs in left:
        for rhs in right:
            if _contains(lhs, rhs) or _contains(rhs, lhs):
                return True
    return False


async def apply_pending_blueprint_revision(project_id: str) -> dict[str, Any]:
    """Promote pending blueprint revision and re-materialize the target node."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found", "error_kind": "project_missing"}
        state = json.loads(project.state_json or "{}")
        pending = state.get("pending_blueprint_revision")
        if not isinstance(pending, dict):
            return {"ok": False, "error": "没有待确认的蓝图修订。", "error_kind": "pending_blueprint_revision_missing"}
        if pending.get("status") != "pending_review":
            return {
                "ok": False,
                "error": "当前蓝图修订还需要模型重新生成草稿，不能直接确认应用。",
                "error_kind": "blueprint_revision_not_applicable",
                "pending_revision": pending,
            }
        rel_path = str(pending.get("file_json") or blueprint_paths(project_id)["revision_json"])
        path = Path(settings.PROJECT_ROOT) / rel_path
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "error": f"蓝图修订草稿读取失败：{exc}",
                "error_kind": "blueprint_revision_read_failed",
            }
        if not isinstance(doc, dict):
            return {"ok": False, "error": "蓝图修订草稿必须是 JSON object。", "error_kind": "blueprint_revision_invalid"}
        doc["status"] = "active"
        doc["updated_at"] = _now_iso()
        sync_blueprint_outline_document(doc)
        doc["validation_report"] = validate_blueprint_document(doc)

        active_index = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else {}
        theme = _as_dict(doc.get("theme"))
        production = _as_dict(doc.get("production"))
        index = {
            **active_index,
            "id": doc.get("id") or active_index.get("id"),
            "version": doc.get("version") or pending.get("version"),
            "status": "active",
            "theme_title": theme.get("title") or active_index.get("theme_title") or "项目蓝图",
            "short_summary": theme.get("logline") or active_index.get("short_summary") or "",
            "checksum": _checksum(doc),
            "updated_at": doc.get("updated_at"),
            "duration_seconds": theme.get("duration_seconds") or active_index.get("duration_seconds"),
            "episode_count": production.get("episode_count") or active_index.get("episode_count"),
            "segment_seconds": production.get("segment_seconds") or active_index.get("segment_seconds"),
        }
        paths = write_blueprint_files(project_id, doc, index)
        index["file_json"] = paths["json"]
        index["file_markdown"] = paths["markdown"]
        index["file_view_model"] = paths["view_model"]
        view_model = render_blueprint_view_model(doc, index)
        outline_markdown = blueprint_outline_markdown(doc)
        state["project_blueprint"] = index
        state["pending_blueprint_revision"] = None

        affected_paths = [
            str(path)
            for path in (
                pending.get("applied_source_paths")
                if isinstance(pending.get("applied_source_paths"), list)
                else []
            )
            if path
        ]
        rematerialized: list[str] = []
        stale_nodes: list[dict[str, Any]] = []
        nodes_result = await session.exec(select(WorkflowNode).where(WorkflowNode.project_id == project_id))
        nodes = list(nodes_result.all())
        for node in nodes:
            node_paths = _workflow_node_source_paths(node)
            if not _paths_intersect(node_paths, affected_paths):
                continue
            _mark_node_blueprint_dirty(
                node=node,
                index=index,
                pending=pending,
                affected_paths=affected_paths,
                node_paths=node_paths,
            )
            session.add(node)
            stale_nodes.append({
                "node_id": node.id,
                "type": node.type,
                "title": node.title,
                "source_paths": node_paths,
                "sync_status": "dirty",
                "action": "rerun_from_blueprint_or_confirm_regeneration",
                "affected_source_paths": affected_paths,
            })
        state["blueprint_stale_nodes"] = stale_nodes
        progress = state.get("blueprint_progress") if isinstance(state.get("blueprint_progress"), dict) else {}
        progress["status"] = "active"
        progress["revision"] = {
            "applied_version": index.get("version"),
            "rematerialized_node_ids": rematerialized,
            "stale_node_count": len(stale_nodes),
            "affected_source_paths": affected_paths,
        }
        state["blueprint_progress"] = progress
        history = state.get("blueprint_history") if isinstance(state.get("blueprint_history"), list) else []
        history.append({
            "id": index.get("id"),
            "version": index.get("version"),
            "checksum": index.get("checksum"),
            "applied_revision_from": pending.get("from_version"),
            "affected_source_paths": affected_paths,
            "rematerialized_node_ids": rematerialized,
            "stale_node_count": len(stale_nodes),
            "applied_at": _now_iso(),
        })
        state["blueprint_history"] = history[-20:]
        project.state_json = json.dumps(state, ensure_ascii=False)
        project.title = str(index.get("theme_title") or project.title)
        session.add(project)
        await session.commit()
    return {
        "ok": True,
        "blueprint": index,
        "view_model": view_model,
        "outline_markdown": outline_markdown,
        "rematerialized_node_ids": rematerialized,
        "stale_nodes": stale_nodes,
        "message": f"蓝图修订已应用，已重物化 {len(rematerialized)} 个剧情节点，标记 {len(stale_nodes)} 个下游节点需要同步。",
    }
