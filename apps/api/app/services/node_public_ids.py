"""Helpers for model/user-visible workflow node ids.

The database keeps UUID primary keys for foreign keys and media records.
The agent and UI should see project-local numeric ids only.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def strip_node_id_marker(value: Any) -> str:
    text = str(value or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in ("@", "node:", "#"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
    return text


def looks_like_internal_node_id(value: Any) -> bool:
    return bool(_UUID_RE.fullmatch(strip_node_id_marker(value)))


def looks_like_public_node_id(value: Any) -> bool:
    text = strip_node_id_marker(value)
    return bool(text) and text.isdigit()


def public_node_id_from_dict(node: dict[str, Any]) -> str:
    value = node.get("display_id")
    if value is not None:
        try:
            return str(int(value))
        except (TypeError, ValueError):
            pass
    return str(node.get("id") or node.get("node_id") or "")


def public_node_id_from_model(node: WorkflowNode) -> str:
    if node.display_id is not None:
        return str(node.display_id)
    return str(node.id)


async def resolve_internal_node_id(
    session: AsyncSession,
    project_id: str,
    node_id: Any,
) -> str:
    raw = strip_node_id_marker(node_id)
    if not raw:
        return ""
    if project_id and raw.isdigit():
        if not hasattr(session, "exec"):
            return raw
        try:
            result = await session.exec(
                select(WorkflowNode.id).where(
                    WorkflowNode.project_id == project_id,
                    WorkflowNode.display_id == int(raw),
                )
            )
            resolved = result.first()
            return str(resolved or raw)
        except (AttributeError, OperationalError, ProgrammingError):
            return raw
    return raw


async def internal_to_public_id_map(
    session: AsyncSession,
    project_id: str,
) -> dict[str, str]:
    if not hasattr(session, "exec"):
        return {}
    try:
        rows = (await session.exec(
            select(WorkflowNode.id, WorkflowNode.display_id).where(
                WorkflowNode.project_id == project_id,
            )
        )).all()
    except (AttributeError, OperationalError, ProgrammingError):
        return {}
    mapping: dict[str, str] = {}
    for node_id, display_id in rows:
        mapping[str(node_id)] = str(display_id) if display_id is not None else str(node_id)
    return mapping


def publicize_node_refs(value: Any, id_map: dict[str, str]) -> Any:
    if isinstance(value, str):
        original = value.strip()
        raw = strip_node_id_marker(original)
        public = id_map.get(raw)
        if not public:
            return value
        if original.startswith("@node:"):
            return f"@node:{public}"
        if original.startswith("node:"):
            return f"node:{public}"
        if original.startswith("@"):
            return f"@{public}"
        return public
    if isinstance(value, list):
        return [publicize_node_refs(item, id_map) for item in value]
    if isinstance(value, dict):
        return {key: publicize_node_refs(item, id_map) for key, item in value.items()}
    return value


def model_visible_node_payload(
    node: dict[str, Any],
    id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    mapping = id_map or {}
    public_id = public_node_id_from_dict(node)
    payload = {
        key: publicize_node_refs(value, mapping)
        for key, value in node.items()
        if key not in {"display_id", "project_id"}
    }
    payload["id"] = public_id
    return payload


def model_visible_edge_payload(
    edge: dict[str, Any],
    id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    mapping = id_map or {}
    payload = {
        key: publicize_node_refs(value, mapping)
        for key, value in edge.items()
        if key != "project_id"
    }
    if "source_node_id" in payload:
        payload["source_node_id"] = publicize_node_refs(payload["source_node_id"], mapping)
    if "target_node_id" in payload:
        payload["target_node_id"] = publicize_node_refs(payload["target_node_id"], mapping)
    if "source" in payload:
        payload["source"] = publicize_node_refs(payload["source"], mapping)
    if "target" in payload:
        payload["target"] = publicize_node_refs(payload["target"], mapping)
    return payload
