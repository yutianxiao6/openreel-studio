"""Semantic blueprint-tree draft state helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any


_ACTIVE_BLUEPRINT_STATUSES = {"active", "materialized"}
_PENDING_BLUEPRINT_STATUSES = {"drafting", "pending_review"}
_REPLACEMENT_DRAFT_KEY = "replacement_draft"


def _stable_checksum(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _active_blueprint_exists(doc: dict[str, Any]) -> bool:
    return str(doc.get("status") or "") in _ACTIVE_BLUEPRINT_STATUSES


def _pending_blueprint_tree_exists(doc: dict[str, Any]) -> bool:
    status = str(doc.get("status") or "")
    if status not in _PENDING_BLUEPRINT_STATUSES:
        return False
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    children = root.get("children") if isinstance(root.get("children"), list) else []
    return status == "pending_review" or bool(children)


def _replacement_draft(doc: dict[str, Any]) -> dict[str, Any] | None:
    draft = doc.get(_REPLACEMENT_DRAFT_KEY)
    return draft if isinstance(draft, dict) else None


def _draft_container(doc: dict[str, Any]) -> dict[str, Any]:
    return _replacement_draft(doc) or doc


def _draft_root(doc: dict[str, Any]) -> dict[str, Any]:
    container = _draft_container(doc)
    root = container.get("root")
    return root if isinstance(root, dict) else {}


def _draft_mode(doc: dict[str, Any]) -> str:
    return "replacement" if _replacement_draft(doc) is not None else "new"


def _active_blueprint_ref(doc: dict[str, Any]) -> dict[str, Any]:
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    return {
        "tree_version": doc.get("tree_version"),
        "checksum": _stable_checksum(root),
        "status": doc.get("status"),
        "title": doc.get("title") or root.get("title") or "",
        "summary": doc.get("summary") or root.get("content") or "",
    }


def _normalize_draft_mode(value: Any) -> str:
    mode = str(value or "new").strip().lower()
    if mode in {"replacement", "replace", "rebuild", "recreate", "重建", "替换"}:
        return "replacement"
    return "new"


def _empty_semantic_root(title: str, summary: str, now: str) -> dict[str, Any]:
    return {
        "id": "root",
        "type": "story",
        "title": title or "视频蓝图",
        "content": summary or "",
        "status": "drafting",
        "materialize": False,
        "children": [],
        "created_at": now,
        "updated_at": now,
    }
