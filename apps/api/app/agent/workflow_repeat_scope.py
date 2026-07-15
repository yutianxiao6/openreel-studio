"""Shared repeat-scope matching for workflow runtime and canvas projection."""
from __future__ import annotations

import json
from typing import Any


_EXPLICIT_SCOPE_IDENTITY_KEYS = {
    "id",
    "key",
    "episode",
    "segment",
    "scene",
    "shot",
    "module",
    "lesson",
    "chapter",
    "episode_index",
    "segment_index",
    "scene_index",
    "shot_index",
    "module_index",
    "lesson_index",
    "chapter_index",
    "start_second",
    "end_second",
    "start_time",
    "end_time",
}


def workflow_item_metadata(item: dict[str, Any] | None) -> dict[str, Any]:
    """Return workflow metadata from either a normalized step or runtime record."""
    if not isinstance(item, dict):
        return {}
    workflow = item.get("workflow")
    if isinstance(workflow, dict) and workflow:
        return workflow
    fields = item.get("input")
    if isinstance(fields, dict):
        workflow = fields.get("workflow")
        if isinstance(workflow, dict) and workflow:
            return workflow
    return item


def workflow_instance_scope(item: dict[str, Any] | None) -> dict[str, Any]:
    metadata = workflow_item_metadata(item)
    scope = metadata.get("instance_scope")
    return scope if isinstance(scope, dict) else {}


def workflow_repeat_group_id(item: dict[str, Any] | None) -> str:
    return str(workflow_item_metadata(item).get("repeat_group_id") or "").strip()


def workflow_repeat_index(item: dict[str, Any] | None) -> int | None:
    metadata = workflow_item_metadata(item)
    value = metadata.get("repeat_group_index")
    if value in (None, ""):
        value = workflow_instance_scope(item).get("index")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_scope_identity_key(key: str) -> bool:
    return key in _EXPLICIT_SCOPE_IDENTITY_KEYS or key.endswith("_id")


def _scope_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value).strip()


def workflow_scopes_conflict(
    target: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
) -> bool:
    """Return true when records declare different values for a shared stable scope."""
    target_scope = workflow_instance_scope(target)
    candidate_scope = workflow_instance_scope(candidate)
    for key in target_scope.keys() & candidate_scope.keys():
        if not _is_scope_identity_key(str(key)):
            continue
        target_value = _scope_value(target_scope.get(key))
        candidate_value = _scope_value(candidate_scope.get(key))
        if target_value and candidate_value and target_value != candidate_value:
            return True
    return False


def workflow_same_repeat_scope(
    target: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
) -> bool:
    """Match a candidate to the target's current outer and inner loop instance.

    Nested loops have different repeat_group_id values, so shared stable scope
    fields such as segment_id must be checked before the current group's index.
    """
    if workflow_scopes_conflict(target, candidate):
        return False
    target_group = workflow_repeat_group_id(target)
    candidate_group = workflow_repeat_group_id(candidate)
    if not target_group or not candidate_group or target_group != candidate_group:
        return True
    target_index = workflow_repeat_index(target)
    candidate_index = workflow_repeat_index(candidate)
    if target_index is not None and candidate_index is not None:
        return target_index == candidate_index
    return True
