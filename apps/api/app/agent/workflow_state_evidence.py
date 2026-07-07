"""Backend workflow state evidence for review and debugging.

The evidence packet is deterministic. It reads persisted project state,
workflow runtime records, canvas nodes, and canvas edges without calling an LLM.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.node_service import NodeService
from app.services.project_service import ProjectService


_ACTIVE_WORKFLOW_STATE_KEY = "active_workflow"
_WORKFLOW_RUNTIME_STATE_KEY = "workflow_runtime"
_WORKFLOW_INPUT_VALUES_STATE_KEY = "workflow_input_values"
_PREVIEW_LIMIT = 700


async def build_workflow_state_evidence(
    project_id: str,
    db: AsyncSession,
    *,
    template_id: str = "",
    instance_id: str = "",
    node_limit: int = 240,
) -> dict[str, Any]:
    """Return a compact backend-only evidence packet for one project workflow."""
    state = await ProjectService(db).get_project_state(project_id)
    if state is None:
        return {
            "ok": False,
            "error": "Project not found",
            "error_kind": "project_not_found",
            "project_id": project_id,
        }
    state = state if isinstance(state, dict) else {}

    from app.mcp_tools import workflow_tools

    active = _active_workflow_summary(state)
    selected_template_id = str(template_id or _workflow_id_from_active(active) or "").strip()
    selected_instance_id = str(instance_id or "").strip()
    runtimes = workflow_tools.workflow_runtime_public_payloads(state, template_id=selected_template_id)
    if selected_instance_id:
        runtimes = [
            item for item in runtimes
            if str(item.get("instance_id") or "").strip() == selected_instance_id
        ]
    if not runtimes and selected_template_id:
        payload = workflow_tools.workflow_runtime_public_payload(
            state,
            template_id=selected_template_id,
            instance_id=selected_instance_id,
        )
        if payload.get("steps") or selected_instance_id:
            runtimes = [payload]

    svc = NodeService(db)
    db_nodes = await svc.list_nodes(project_id)
    raw_edges = await svc.list_edges(project_id)
    display_edges = await svc.list_canvas_edges(project_id, nodes=db_nodes)

    node_entries = [_node_entry(node) for node in db_nodes]
    relevant_nodes = [
        node for node in node_entries
        if _node_matches_filter(node, selected_template_id, selected_instance_id)
    ]
    canvas_nodes = [
        node for node in relevant_nodes
        if node["surface"] != "workflow_runtime"
    ]
    runtime_nodes = [
        node for node in relevant_nodes
        if node["surface"] == "workflow_runtime"
    ]
    relevant_node_ids = {str(node.get("id") or "") for node in relevant_nodes}
    persisted_edges = [
        edge for edge in (_edge_entry(edge) for edge in raw_edges)
        if edge["source_node_id"] in relevant_node_ids and edge["target_node_id"] in relevant_node_ids
    ]
    inferred_edges = _infer_edges_from_node_inputs(canvas_nodes)
    display_edge_entries = [
        edge for edge in (_display_edge_entry(edge) for edge in display_edges)
        if edge["source_node_id"] in relevant_node_ids and edge["target_node_id"] in relevant_node_ids
    ]
    mismatches = _edge_mismatches(canvas_nodes, persisted_edges, inferred_edges)
    mismatches.extend(_runtime_canvas_mismatches(runtimes, canvas_nodes))

    summary = {
        "runtime_instance_count": len(runtimes),
        "runtime_step_count": sum(len(item.get("steps") or []) for item in runtimes),
        "canvas_node_count": len(canvas_nodes),
        "runtime_node_count": len(runtime_nodes),
        "persisted_edge_count": len(persisted_edges),
        "inferred_edge_count": len(inferred_edges),
        "display_edge_count": len(display_edge_entries),
        "mismatch_count": len(mismatches),
    }
    return {
        "ok": True,
        "schema_version": "workflow_state_evidence_v1",
        "source": "backend_state",
        "project_id": project_id,
        "filters": {
            "template_id": selected_template_id,
            "instance_id": selected_instance_id,
        },
        "active_workflow": active,
        "workflow_input_values": _workflow_input_values_summary(
            state,
            selected_template_id,
            selected_instance_id,
        ),
        "runtime": {
            "instances": [_runtime_instance_summary(item) for item in runtimes],
        },
        "canvas": {
            "nodes": canvas_nodes[:node_limit],
            "runtime_nodes": runtime_nodes[:node_limit],
            "persisted_edges": persisted_edges[:node_limit],
            "inferred_edges": inferred_edges[:node_limit],
            "display_edges": display_edge_entries[:node_limit],
        },
        "consistency": {
            "status": "pass" if not mismatches else "attention",
            "summary": summary,
            "mismatches": mismatches[:node_limit],
        },
    }


def _active_workflow_summary(state: dict[str, Any]) -> dict[str, Any] | None:
    active = state.get(_ACTIVE_WORKFLOW_STATE_KEY)
    if not isinstance(active, dict):
        return None
    result = {
        "kind": str(active.get("kind") or "").strip(),
        "template_id": str(active.get("template_id") or "").strip(),
        "artifact_ref": str(active.get("artifact_ref") or "").strip(),
        "name": str(active.get("name") or "").strip(),
        "description": str(active.get("description") or "").strip(),
        "updated_at": str(active.get("updated_at") or "").strip(),
    }
    workflow = active.get("workflow") if isinstance(active.get("workflow"), dict) else {}
    preview = active.get("preview") if isinstance(active.get("preview"), dict) else {}
    workflow_id = str(workflow.get("id") or preview.get("id") or "").strip()
    if workflow_id:
        result["workflow_id"] = workflow_id
    return {key: value for key, value in result.items() if value not in ("", None)}


def _workflow_id_from_active(active: dict[str, Any] | None) -> str:
    if not isinstance(active, dict):
        return ""
    return str(active.get("template_id") or active.get("workflow_id") or "").strip()


def _workflow_input_values_summary(
    state: dict[str, Any],
    template_id: str,
    instance_id: str,
) -> dict[str, Any]:
    store = state.get(_WORKFLOW_INPUT_VALUES_STATE_KEY)
    if not isinstance(store, dict):
        return {}
    by_workflow = store.get("by_workflow") if isinstance(store.get("by_workflow"), dict) else {}
    by_instance = store.get("by_instance") if isinstance(store.get("by_instance"), dict) else {}
    values: dict[str, Any] = {}
    if template_id and isinstance(by_workflow.get(template_id), dict):
        values.update(_record_values(by_workflow[template_id]))
    if instance_id and isinstance(by_instance.get(instance_id), dict):
        values.update(_record_values(by_instance[instance_id]))
    return {
        "updated_at": str(store.get("updated_at") or ""),
        "input_ids": sorted(str(key) for key in values.keys()),
        "values_preview": {str(key): _preview_value(value, limit=180) for key, value in values.items()},
    }


def _record_values(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values")
    return dict(values) if isinstance(values, dict) else {}


def _runtime_instance_summary(runtime: dict[str, Any]) -> dict[str, Any]:
    steps = [
        _runtime_step_summary(step)
        for step in (runtime.get("steps") or [])
        if isinstance(step, dict)
    ]
    return {
        "instance_id": str(runtime.get("instance_id") or ""),
        "template_id": str(runtime.get("template_id") or ""),
        "template_name": str(runtime.get("template_name") or ""),
        "status": str(runtime.get("status") or ""),
        "current_step_id": str(runtime.get("current_step_id") or ""),
        "progress": runtime.get("progress") if isinstance(runtime.get("progress"), dict) else {},
        "pause_requested": bool(runtime.get("pause_requested")),
        "updated_at": str(runtime.get("updated_at") or ""),
        "steps": steps,
    }


def _runtime_step_summary(step: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": str(step.get("id") or ""),
        "title": str(step.get("title") or ""),
        "type": str(step.get("type") or step.get("node_type") or ""),
        "status": str(step.get("status") or ""),
        "execution_state": str(step.get("execution_state") or ""),
        "surface": str(step.get("surface") or ""),
        "visibility": str(step.get("visibility") or ""),
        "depends_on": [str(item) for item in (step.get("depends_on") or [])],
        "waiting_on": [str(item) for item in (step.get("waiting_on") or [])],
        "ready": bool(step.get("ready")),
        "stale": bool(step.get("stale")),
        "canvas_output": bool(step.get("canvas_output")),
        "node_id": str(step.get("node_id") or ""),
        "artifact_node_ids": [str(item) for item in (step.get("artifact_node_ids") or [])],
        "output_preview": step.get("output_preview") if isinstance(step.get("output_preview"), dict) else {},
    }
    for key in ("template_step_id", "repeat_group_id", "repeat_group_index", "phase", "group", "kind", "role"):
        value = step.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    return result


def _node_entry(node: Any) -> dict[str, Any]:
    input_data = _parse_dict(getattr(node, "input_json", None))
    output_data = _parse_value(getattr(node, "output_json", None))
    model_config = _parse_dict(getattr(node, "model_config_json", None))
    workflow = input_data.get("workflow") if isinstance(input_data.get("workflow"), dict) else {}
    surface = str(
        model_config.get("surface")
        or model_config.get("_surface")
        or workflow.get("surface")
        or input_data.get("surface")
        or ""
    ).strip() or ("workflow_runtime" if str(getattr(node, "id", "")).startswith("workflow-runtime:") else "canvas")
    return {
        "id": str(getattr(node, "id", "") or ""),
        "display_id": getattr(node, "display_id", None),
        "public_ref": _public_ref(getattr(node, "id", ""), getattr(node, "display_id", None)),
        "type": str(getattr(node, "type", "") or ""),
        "title": str(getattr(node, "title", "") or ""),
        "status": str(getattr(node, "status", "") or ""),
        "surface": surface,
        "position": {
            "x": float(getattr(node, "position_x", 0.0) or 0.0),
            "y": float(getattr(node, "position_y", 0.0) or 0.0),
        },
        "workflow": _workflow_summary(workflow),
        "input_keys": sorted(str(key) for key in input_data.keys()),
        "dependency_refs": _dependency_refs_from_input(input_data),
        "output_present": output_data not in (None, "", [], {}),
        "output_preview": _preview_value(output_data),
        "error_message": str(getattr(node, "error_message", "") or ""),
        "created_at": _iso(getattr(node, "created_at", None)),
        "updated_at": _iso(getattr(node, "updated_at", None)),
    }


def _workflow_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "template_id",
        "template_name",
        "instance_id",
        "step_id",
        "template_step_id",
        "source_node_id",
        "surface",
        "visibility",
        "repeat_group_id",
        "repeat_group_index",
        "kind",
        "role",
        "depends_on",
    )
    return {
        key: workflow.get(key)
        for key in keys
        if workflow.get(key) not in (None, "", [], {})
    }


def _node_matches_filter(node: dict[str, Any], template_id: str, instance_id: str) -> bool:
    workflow = node.get("workflow") if isinstance(node.get("workflow"), dict) else {}
    node_template_id = str(workflow.get("template_id") or "").strip()
    node_instance_id = str(workflow.get("instance_id") or "").strip()
    if template_id and node_template_id and node_template_id != template_id:
        return False
    if template_id and not node_template_id:
        return False
    if instance_id and node_instance_id and node_instance_id != instance_id:
        return False
    if instance_id and not node_instance_id:
        return False
    return True


def _edge_entry(edge: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(edge, "id", "") or ""),
        "source_node_id": str(getattr(edge, "source_node_id", "") or ""),
        "target_node_id": str(getattr(edge, "target_node_id", "") or ""),
        "label": getattr(edge, "label", None),
        "created_at": _iso(getattr(edge, "created_at", None)),
    }


def _display_edge_entry(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(edge.get("id") or ""),
        "source_node_id": str(edge.get("source_node_id") or edge.get("source") or ""),
        "target_node_id": str(edge.get("target_node_id") or edge.get("target") or ""),
        "label": edge.get("label"),
        "derived": edge.get("_derived") or "",
    }


def _infer_edges_from_node_inputs(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lookup: dict[str, str] = {}
    by_id = {str(node.get("id") or ""): node for node in nodes}
    for node in nodes:
        node_id = str(node.get("id") or "")
        display_id = node.get("display_id")
        candidates = [
            node_id,
            f"node:{node_id}",
            f"@{node_id}",
        ]
        if display_id not in (None, ""):
            display = str(display_id)
            candidates.extend([display, f"#{display}", f"node:{display}", f"@{display}"])
        workflow = node.get("workflow") if isinstance(node.get("workflow"), dict) else {}
        for key in ("step_id", "template_step_id", "source_node_id"):
            value = str(workflow.get(key) or "").strip()
            if value:
                candidates.extend([value, f"@{value}"])
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                lookup.setdefault(text, node_id)

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for node in nodes:
        target_id = str(node.get("id") or "")
        for ref in node.get("dependency_refs") or []:
            raw_ref = str(ref.get("ref") or "").strip()
            source_id = _resolve_node_ref(raw_ref, lookup)
            if not source_id or source_id == target_id or source_id not in by_id:
                continue
            key = (source_id, target_id, str(ref.get("role") or "context"))
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "id": f"inferred-{source_id}-{target_id}-{len(edges) + 1}",
                "source_node_id": source_id,
                "target_node_id": target_id,
                "role": str(ref.get("role") or "context"),
                "ref": raw_ref,
                "derived": "node_dependency_fields",
            })
    return edges


def _edge_mismatches(
    nodes: list[dict[str, Any]],
    persisted_edges: list[dict[str, Any]],
    inferred_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    node_ids = {str(node.get("id") or "") for node in nodes}
    dependency_owned_targets = {
        str(node.get("id") or "")
        for node in nodes
        if node.get("dependency_refs")
    }
    persisted_pairs = {
        (edge["source_node_id"], edge["target_node_id"])
        for edge in persisted_edges
        if edge.get("source_node_id") in node_ids
        and edge.get("target_node_id") in node_ids
        and edge.get("source_node_id") != edge.get("target_node_id")
    }
    inferred_pairs = {
        (edge["source_node_id"], edge["target_node_id"])
        for edge in inferred_edges
    }
    mismatches: list[dict[str, Any]] = []
    for edge in persisted_edges:
        source = edge.get("source_node_id")
        target = edge.get("target_node_id")
        if source not in node_ids or target not in node_ids:
            mismatches.append({
                "code": "edge_points_to_missing_node",
                "severity": "high",
                "message": "Persisted canvas edge points to a missing node.",
                "edge": edge,
            })
            continue
        if target in dependency_owned_targets and (source, target) not in inferred_pairs:
            mismatches.append({
                "code": "stale_persisted_edge",
                "severity": "medium",
                "message": "Persisted canvas edge is not present in node dependency fields.",
                "source_node_id": source,
                "target_node_id": target,
            })
    for source, target in sorted(inferred_pairs - persisted_pairs):
        mismatches.append({
            "code": "missing_persisted_edge",
            "severity": "medium",
            "message": "Node dependency fields imply an edge that is not persisted.",
            "source_node_id": source,
            "target_node_id": target,
        })
    return mismatches


def _runtime_canvas_mismatches(
    runtimes: list[dict[str, Any]],
    canvas_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes_by_id = {str(node.get("id") or ""): node for node in canvas_nodes}
    nodes_by_runtime_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for node in canvas_nodes:
        workflow = node.get("workflow") if isinstance(node.get("workflow"), dict) else {}
        key = (
            str(workflow.get("template_id") or ""),
            str(workflow.get("instance_id") or ""),
            str(workflow.get("step_id") or workflow.get("template_step_id") or ""),
        )
        if key[2]:
            nodes_by_runtime_key.setdefault(key, []).append(node)

    runtime_step_keys: set[tuple[str, str, str]] = set()
    mismatches: list[dict[str, Any]] = []
    for runtime in runtimes:
        template_id = str(runtime.get("template_id") or "")
        instance_id = str(runtime.get("instance_id") or "")
        for step in runtime.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or "")
            key = (template_id, instance_id, step_id)
            runtime_step_keys.add(key)
            artifact_ids = [
                str(item) for item in [step.get("node_id"), *(step.get("artifact_node_ids") or [])]
                if str(item or "").strip()
            ]
            missing_artifacts = [node_id for node_id in artifact_ids if node_id not in nodes_by_id]
            for node_id in missing_artifacts:
                mismatches.append({
                    "code": "runtime_artifact_node_missing",
                    "severity": "high",
                    "message": "Workflow runtime references a canvas node that is missing.",
                    "template_id": template_id,
                    "instance_id": instance_id,
                    "step_id": step_id,
                    "node_id": node_id,
                })
            canvas_output = bool(step.get("canvas_output"))
            status = str(step.get("status") or "")
            if canvas_output and status == "completed" and not artifact_ids and key not in nodes_by_runtime_key:
                mismatches.append({
                    "code": "completed_canvas_output_without_node",
                    "severity": "high",
                    "message": "Completed canvas-output workflow step has no canvas node.",
                    "template_id": template_id,
                    "instance_id": instance_id,
                    "step_id": step_id,
                })
    for key, nodes in nodes_by_runtime_key.items():
        if key not in runtime_step_keys:
            for node in nodes:
                mismatches.append({
                    "code": "canvas_node_missing_runtime_step",
                    "severity": "medium",
                    "message": "Canvas node has workflow metadata but no matching runtime step.",
                    "template_id": key[0],
                    "instance_id": key[1],
                    "step_id": key[2],
                    "node_id": node.get("id"),
                })
    return mismatches


def _dependency_refs_from_input(input_data: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    containers = [input_data]
    fields = input_data.get("fields")
    if isinstance(fields, dict):
        containers.append(fields)
    for container in containers:
        for key in ("depends_on", "references", "reference_images"):
            value = container.get(key)
            for ref in _coerce_list(value):
                text, role = _reference_text_and_role(ref)
                if text:
                    refs.append({"ref": text, "role": role or ("visual_reference" if key == "reference_images" else "context")})
    return _dedupe_refs(refs)


def _reference_text_and_role(value: Any) -> tuple[str, str]:
    if isinstance(value, dict):
        role = str(value.get("role") or "").strip()
        for key in ("ref", "reference", "reference_input", "node_id", "nodeId", "source_node_id", "sourceNodeId", "id", "value"):
            raw = value.get(key)
            if raw is not None:
                text = str(raw).strip()
                if key in {"node_id", "nodeId", "source_node_id", "sourceNodeId"} and text and not text.startswith("node:"):
                    text = f"node:{text}"
                return text, role
        return "", role
    return str(value or "").strip(), ""


def _resolve_node_ref(raw_ref: str, lookup: dict[str, str]) -> str:
    text = str(raw_ref or "").strip()
    if not text or text.startswith(("asset:", "upload:", "http://", "https://")) or "/" in text:
        return ""
    candidates = [text]
    if text.startswith("@"):
        candidates.append(text[1:])
    if text.startswith("node:"):
        candidates.append(text[5:])
    if text.startswith("#"):
        candidates.append(text[1:])
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    return ""


def _dedupe_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in refs:
        ref = str(item.get("ref") or "").strip()
        role = str(item.get("role") or "context").strip() or "context"
        if not ref:
            continue
        key = (ref, role)
        if key in seen:
            continue
        seen.add(key)
        result.append({"ref": ref, "role": role})
    return result


def _public_ref(node_id: str, display_id: Any) -> str:
    if display_id not in (None, ""):
        return f"node:{display_id}"
    return f"node:{node_id}"


def _coerce_list(value: Any) -> list[Any]:
    if value in (None, "", {}, []):
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _parse_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _preview_value(value: Any, *, limit: int = _PREVIEW_LIMIT) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")
