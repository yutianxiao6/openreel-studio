"""Node service — CRUD for workflow_nodes / workflow_edges."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode, WorkflowEdge


def _as_json_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _with_ui_creator(value: Any, creator: str = "agent") -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        value = parsed if isinstance(parsed, dict) else {}
    model_config = dict(value or {}) if isinstance(value, dict) else {}
    model_config.setdefault("_ui_creator", creator)
    return model_config


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _reference_value(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("ref", "reference", "reference_input", "node_id", "nodeId", "source_node_id", "sourceNodeId", "id", "value"):
            value = item.get(key)
            if value is not None:
                text = str(value).strip()
                return (
                    f"node:{text}"
                    if key in {"node_id", "nodeId", "source_node_id", "sourceNodeId"} and text and not text.startswith("node:")
                    else text
                )
        return ""
    return str(item or "").strip()


def _dependency_node_ids(input_data: dict[str, Any], node_ids: set[str]) -> list[str]:
    raw_items: list[Any] = []
    containers = [input_data]
    fields = input_data.get("fields")
    if isinstance(fields, dict):
        containers.append(fields)
    for container in containers:
        for key in ("depends_on", "references", "reference_images"):
            value = container.get(key)
            if isinstance(value, list):
                raw_items.extend(value)
            elif value:
                raw_items.append(value)

    deps: list[str] = []
    for raw in raw_items:
        text = _reference_value(raw)
        if text.startswith("@"):
            text = text[1:]
        if text.startswith("node:"):
            text = text[5:]
        if (
            not text
            or text.startswith(("asset:", "upload:", "http://", "https://"))
            or "/" in text
            or text not in node_ids
        ):
            continue
        if text not in deps:
            deps.append(text)
    return deps


def _has_dependency_keys(input_data: dict[str, Any]) -> bool:
    containers: list[dict[str, Any]] = [input_data]
    fields = input_data.get("fields")
    if isinstance(fields, dict):
        containers.append(fields)
    return any(any(key in container for key in ("depends_on", "references", "reference_images")) for container in containers)


def canvas_edge_payloads(
    nodes: list[WorkflowNode],
    persisted_edges: list[WorkflowEdge],
) -> list[dict[str, Any]]:
    """Return display edges with node-authored dependencies as the source of truth.

    A target node that declares references/depends_on owns its incoming dependency
    edges. This suppresses stale persisted edges such as A -> B after B was edited
    to depend on C.
    """
    node_ids = {node.id for node in nodes}
    desired_by_target: dict[str, list[str]] = {}
    dependency_owned_targets: set[str] = set()
    for node in nodes:
        input_data = _as_dict(node.input_json)
        if _has_dependency_keys(input_data):
            dependency_owned_targets.add(node.id)
        deps = [dep for dep in _dependency_node_ids(input_data, node_ids) if dep != node.id]
        if deps:
            desired_by_target[node.id] = deps

    persisted_by_pair = {
        (edge.source_node_id, edge.target_node_id): edge
        for edge in persisted_edges
        if edge.source_node_id in node_ids and edge.target_node_id in node_ids
    }
    payloads: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for edge in persisted_edges:
        pair = (edge.source_node_id, edge.target_node_id)
        if pair[0] not in node_ids or pair[1] not in node_ids or pair[0] == pair[1]:
            continue
        desired_sources = desired_by_target.get(edge.target_node_id, [])
        if edge.target_node_id in dependency_owned_targets and edge.source_node_id not in desired_sources:
            continue
        if pair in seen:
            continue
        seen.add(pair)
        payloads.append(edge.model_dump())

    for target_id, source_ids in desired_by_target.items():
        for source_id in source_ids:
            pair = (source_id, target_id)
            if pair in seen:
                continue
            persisted = persisted_by_pair.get(pair)
            if persisted is not None:
                payloads.append(persisted.model_dump())
            else:
                payloads.append({
                    "id": f"dep-{source_id}-{target_id}",
                    "project_id": nodes[0].project_id if nodes else "",
                    "source_node_id": source_id,
                    "target_node_id": target_id,
                    "label": None,
                    "created_at": None,
                    "_derived": "node_dependencies",
                })
            seen.add(pair)
    return payloads


def _display_render_state(node: WorkflowNode) -> str | None:
    if node.type != "image":
        return None
    input_data = _as_dict(node.input_json)
    raw = input_data.get("render_state")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if node.status == "completed" and node.output_json:
        return "fresh"
    return None


def workflow_node_payload(node: WorkflowNode) -> dict[str, Any]:
    payload = node.model_dump()
    payload["render_state"] = _display_render_state(node)
    return payload


class NodeService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_node(
        self, project_id: str, payload: dict[str, Any]
    ) -> WorkflowNode:
        now = datetime.utcnow()

        supersedes_id = payload.get("supersedes_id")
        version = int(payload.get("version", 1))
        if supersedes_id:
            prev = await self.db.get(WorkflowNode, supersedes_id)
            if prev is not None:
                version = max(version, int(prev.version or 1) + 1)

        node = WorkflowNode(
            id=str(uuid.uuid4()),
            project_id=project_id,
            type=payload.get("type", "script_generation"),
            title=payload.get("title", ""),
            status=payload.get("status", "idle"),
            position_x=float(payload.get("position_x", 0.0)),
            position_y=float(payload.get("position_y", 0.0)),
            input_json=_as_json_str(payload.get("input_json")),
            output_json=_as_json_str(payload.get("output_json")),
            model_config_json=_as_json_str(_with_ui_creator(payload.get("model_config_json"))),
            prompt=payload.get("prompt"),
            error_message=payload.get("error_message"),
            version=version,
            supersedes_id=supersedes_id,
            created_at=now,
            updated_at=now,
        )
        self.db.add(node)
        await self.db.commit()
        await self.db.refresh(node)
        return node

    async def update_node(
        self, node_id: str, patch: dict[str, Any]
    ) -> WorkflowNode | None:
        node = await self.db.get(WorkflowNode, node_id)
        if not node:
            return None
        json_fields = {"input_json", "output_json", "model_config_json"}
        for key, value in patch.items():
            if not hasattr(node, key):
                continue
            if key in json_fields:
                if key == "model_config_json":
                    existing = _with_ui_creator(node.model_config_json)
                    next_model_config = _with_ui_creator(value, existing.get("_ui_creator", "agent"))
                    if existing.get("_ui_creator") and "_ui_creator" not in (value if isinstance(value, dict) else {}):
                        next_model_config["_ui_creator"] = existing["_ui_creator"]
                    value = next_model_config
                value = _as_json_str(value)
            setattr(node, key, value)
        node.updated_at = datetime.utcnow()
        self.db.add(node)
        await self.db.commit()
        await self.db.refresh(node)
        return node

    async def list_nodes(self, project_id: str) -> list[WorkflowNode]:
        result = await self.db.exec(
            select(WorkflowNode)
            .where(WorkflowNode.project_id == project_id)
            .order_by(WorkflowNode.created_at)
        )
        return list(result.all())

    async def list_edges(self, project_id: str) -> list[WorkflowEdge]:
        result = await self.db.exec(
            select(WorkflowEdge).where(WorkflowEdge.project_id == project_id)
        )
        return list(result.all())

    async def list_canvas_edges(
        self,
        project_id: str,
        nodes: list[WorkflowNode] | None = None,
    ) -> list[dict[str, Any]]:
        canvas_nodes = nodes if nodes is not None else await self.list_nodes(project_id)
        persisted_edges = await self.list_edges(project_id)
        return canvas_edge_payloads(canvas_nodes, persisted_edges)

    async def create_edge(
        self,
        project_id: str,
        source_node_id: str,
        target_node_id: str,
        label: str | None = None,
    ) -> WorkflowEdge:
        existing = (await self.db.exec(
            select(WorkflowEdge).where(
                WorkflowEdge.project_id == project_id,
                WorkflowEdge.source_node_id == source_node_id,
                WorkflowEdge.target_node_id == target_node_id,
            )
        )).first()
        if existing is not None:
            return existing
        edge = WorkflowEdge(
            id=str(uuid.uuid4()),
            project_id=project_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            label=label,
            created_at=datetime.utcnow(),
        )
        self.db.add(edge)
        await self.db.commit()
        await self.db.refresh(edge)
        return edge
