"""Deterministic canvas projection for workflow specs.

This module gives the workflow builder a cheap view of what a spec would map
to on the canvas. It validates and expands the spec, but it does not run nodes,
call an LLM, create media, or mutate project state.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.agent import canvas_workflow_templates, workflow_spec_artifacts, workflow_template_store
from app.agent.workflow_audit import audit_workflow_spec


_VALID_CANVAS_TYPES = {"text", "image", "video", "audio"}
_WORKFLOW_INPUT_RUNNERS = {"workflow_input", "input_form", "manual_input"}
_SOURCE_KEYS = ("workflow_source_step", "source_step", "source")


def _coerce_list(value: Any) -> list[Any]:
    if value in (None, "", {}, []):
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _clip(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _relation_id(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("step")
            or value.get("id")
            or value.get("ref")
            or value.get("source_step")
            or value.get("from_step")
            or value.get("source")
            or value.get("candidate")
            or value.get("candidates")
        )
    text = str(value or "").strip()
    if not text:
        return ""
    if "." in text:
        text = text.split(".", 1)[0].strip()
    return text


def _dimension_input_values(
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = dict(inputs or {})
    if isinstance(context, dict) and context:
        values["context"] = context
        values.setdefault("steps", context)
        values.setdefault("nodes", context)
        values.setdefault(
            "outputs",
            {
                str(key): deepcopy(value.get("output"))
                for key, value in context.items()
                if isinstance(value, dict) and value.get("output") not in (None, "", [], {})
            },
        )
    return values


def _workflow_with_dependency_order(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if not isinstance(steps, list) or len(steps) < 2:
        return workflow
    ids: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            return workflow
        step_id = str(step.get("id") or "").strip()
        if not step_id or step_id in by_id:
            return workflow
        ids.append(step_id)
        by_id[step_id] = step
    ordered: list[dict[str, Any]] = []
    remaining = set(ids)
    while remaining:
        progressed = False
        for step_id in ids:
            if step_id not in remaining:
                continue
            step = by_id[step_id]
            deps = [_relation_id(dep) for dep in _coerce_list(step.get("depends_on") or step.get("needs"))]
            if any(dep in remaining for dep in deps if dep):
                continue
            ordered.append(step)
            remaining.remove(step_id)
            progressed = True
        if not progressed:
            return workflow
    if [str(step.get("id") or "").strip() for step in ordered] == ids:
        return workflow
    result = dict(workflow)
    result["steps"] = ordered
    return result


def _merge_inputs(defaults: dict[str, Any] | None, overrides: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(defaults or {})
    result.update(dict(overrides or {}))
    return result


def _normalize_id(value: Any) -> str:
    return canvas_workflow_templates._normalize_inline_id(str(value or ""), fallback=str(value or ""))


def _walk_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            result.append(item)
            visit(item.get("steps"))

    visit(workflow.get("steps") if isinstance(workflow, dict) else None)
    return result


def _deferred_dimension_ids(normalized: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    groups = normalized.get("deferred_groups") if isinstance(normalized.get("deferred_groups"), list) else []
    for group in groups:
        if not isinstance(group, dict):
            continue
        foreach = group.get("foreach")
        if not isinstance(foreach, dict):
            repeat = group.get("repeat") if isinstance(group.get("repeat"), dict) else {}
            foreach = repeat.get("foreach") if isinstance(repeat.get("foreach"), dict) else {}
        dimension = _normalize_id(foreach.get("dimension")) if isinstance(foreach, dict) else ""
        group_id = str(group.get("id") or group.get("group_id") or "").strip()
        if dimension:
            result.setdefault(dimension, []).append(group_id)
    return result


def _dimension_output_source(definition: Any) -> dict[str, str] | None:
    if not isinstance(definition, dict):
        return None
    from_step = str(
        definition.get("from_step")
        or definition.get("from_node")
        or definition.get("planner_node")
        or ""
    ).strip()
    path = str(definition.get("path") or "").strip()
    source = str(definition.get("from") or definition.get("source") or "").strip()
    if from_step:
        output_path = path or "output"
        if not output_path.startswith(("output", "value", "result", "data")):
            output_path = f"output.{output_path}"
        return {
            "source": source or f"steps.{from_step}.{output_path}",
            "step_id": _normalize_id(from_step),
            "output_path": output_path,
        }
    if not source:
        return None
    parts = [part.strip() for part in source.split(".") if part.strip()]
    if len(parts) < 2:
        return None
    root, step_id = parts[0], parts[1]
    rest = ".".join(parts[2:]) or "output"
    if root in {"steps", "context", "nodes"}:
        return {"source": source, "step_id": _normalize_id(step_id), "output_path": rest}
    if root == "outputs":
        return {"source": source, "step_id": _normalize_id(step_id), "output_path": f"output.{rest}"}
    return None


def _sample_value_for_type(field_type: Any, field_id: str) -> Any:
    kind = str(field_type or "string").strip().lower()
    if kind in {"number", "integer", "float"}:
        return 1
    if kind == "boolean":
        return True
    if kind in {"array", "list"}:
        return [f"{field_id}_sample"]
    if kind in {"object", "dict", "json"}:
        return {"value": f"{field_id}_sample"}
    return f"{field_id}_sample"


def _sample_item_for_step(step: dict[str, Any] | None, dimension: str) -> dict[str, Any]:
    schema = step.get("output_schema") if isinstance(step, dict) and isinstance(step.get("output_schema"), dict) else {}
    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    item: dict[str, Any] = {"id": "sample_1"}
    for field in fields[:8]:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or field.get("name") or "").strip()
        if not field_id:
            continue
        item[field_id] = _sample_value_for_type(field.get("type"), field_id)
    if len(item) == 1:
        item["title"] = f"{dimension}_sample"
    return item


def _nest_sample_output(path: str, item: dict[str, Any]) -> Any:
    parts = [part.strip() for part in str(path or "").split(".") if part.strip()]
    if not parts:
        return [item]

    def build(index: int) -> Any:
        token = parts[index]
        is_array = token.endswith("[]")
        key = token[:-2] if is_array else token
        if index == len(parts) - 1:
            value: Any = [item]
        else:
            value = build(index + 1)
            if is_array and not isinstance(value, list):
                value = [value]
        return {key: value} if key else value

    return build(0)


def _missing_sample_outputs(
    *,
    raw_workflow: dict[str, Any],
    normalized: dict[str, Any],
) -> list[dict[str, Any]]:
    deferred_dimensions = _deferred_dimension_ids(normalized)
    if not deferred_dimensions:
        return []
    dimensions = normalized.get("dimensions") if isinstance(normalized.get("dimensions"), dict) else {}
    steps_by_id: dict[str, dict[str, Any]] = {}
    for step in [*_walk_steps(raw_workflow), *_walk_steps(normalized)]:
        step_id = _normalize_id(step.get("id"))
        if step_id and step_id not in steps_by_id:
            steps_by_id[step_id] = step

    missing: list[dict[str, Any]] = []
    for dimension, group_ids in deferred_dimensions.items():
        source = _dimension_output_source(dimensions.get(dimension))
        if not source:
            continue
        step_id = source["step_id"]
        sample_item = _sample_item_for_step(steps_by_id.get(step_id), dimension)
        context_example = {step_id: _nest_sample_output(source["output_path"], sample_item)}
        missing.append(
            {
                "dimension": dimension,
                "repeat_group_ids": [item for item in group_ids if item],
                "source": source["source"],
                "step_id": step_id,
                "output_path": source["output_path"],
                "reason": "repeat waits for a collection output from an earlier step",
                "context_example": context_example,
            }
        )
    return missing[:8]


def _load_source(
    *,
    project_id: str,
    template_id: str = "",
    version_id: str = "",
    artifact_ref: str = "",
    repair_ref: str = "",
    workflow: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    input_values = _dimension_input_values(inputs if isinstance(inputs, dict) else {}, context)
    source: dict[str, Any] = {}
    raw_workflow: dict[str, Any]
    default_inputs: dict[str, Any] = {}

    if isinstance(workflow, dict) and workflow:
        raw_workflow = deepcopy(workflow)
        source = {"kind": "inline_workflow"}
    elif repair_ref:
        candidate = workflow_spec_artifacts.load_workflow_repair_candidate(project_id, repair_ref)
        raw_workflow = deepcopy(candidate.get("workflow") or {})
        default_inputs = deepcopy(candidate.get("sample_inputs") or {})
        source = {
            "kind": "repair_candidate",
            "repair_ref": repair_ref,
            "candidate_preview": deepcopy(candidate.get("preview") or {}),
        }
    elif artifact_ref:
        artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
        raw_workflow = deepcopy(artifact.get("workflow") or {})
        default_inputs = deepcopy(artifact.get("sample_inputs") or {})
        source = {
            "kind": "artifact",
            "artifact_ref": artifact_ref,
            "preview": deepcopy(artifact.get("preview") or {}),
        }
    else:
        selected_template_id = str(template_id or "").strip()
        selected_version_id = str(version_id or "").strip()
        if selected_version_id:
            loaded = workflow_template_store.load_user_template(selected_template_id, selected_version_id)
            raw_workflow = deepcopy(loaded.get("workflow") or {})
            default_inputs = deepcopy(loaded.get("sample_inputs") or {})
            source = {
                "kind": "template",
                "template_id": selected_template_id,
                "version_id": selected_version_id,
                "scope": "user",
            }
        else:
            effective_inputs = _merge_inputs(default_inputs, input_values)
            normalized_template = canvas_workflow_templates.get_template(
                selected_template_id,
                input_values=effective_inputs,
            )
            raw_workflow = deepcopy(normalized_template)
            source = {
                "kind": "template",
                "template_id": str(normalized_template.get("id") or selected_template_id or "").strip(),
                "scope": str(normalized_template.get("scope") or "").strip(),
            }

    effective_inputs = _merge_inputs(default_inputs, input_values)
    normalized = canvas_workflow_templates.normalize_inline_workflow(
        _workflow_with_dependency_order(raw_workflow),
        input_values=effective_inputs,
    )
    if not source.get("template_id"):
        source["workflow_id"] = str(normalized.get("id") or raw_workflow.get("id") or "").strip()
    return raw_workflow, normalized, effective_inputs, source


def _step_surface(step: dict[str, Any]) -> str:
    surface = str(step.get("surface") or "").strip().lower()
    visibility = str(step.get("visibility") or "").strip().lower()
    runner = str(step.get("runner") or "").strip().lower()
    kind = str(step.get("kind") or "").strip().lower().replace("-", "_")
    node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
    if surface == "workflow_runtime" or visibility in {"flow_only", "workflow_runtime"}:
        return "workflow_runtime"
    if surface == "draft_canvas" or visibility == "canvas" or runner == "workflow_canvas_output":
        return "draft_canvas"
    if kind in {"canvas_text", "image", "video", "audio"}:
        return "draft_canvas"
    if not kind and node_type in _VALID_CANVAS_TYPES:
        return "draft_canvas"
    return "workflow_runtime"


def _is_virtual_step(step: dict[str, Any]) -> bool:
    return str(step.get("runner") or "").strip().lower() in _WORKFLOW_INPUT_RUNNERS or bool(step.get("runtime_hidden"))


def _canvas_node_type(step: dict[str, Any]) -> str:
    node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
    return node_type if node_type in _VALID_CANVAS_TYPES else "text"


def _step_deps(step: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for dep in _coerce_list(step.get("depends_on")):
        dep_id = _relation_id(dep)
        if dep_id and dep_id not in result:
            result.append(dep_id)
    return result


def _field_refs(step: dict[str, Any]) -> list[str]:
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    refs: list[str] = []
    for value in _coerce_list(fields.get("references")):
        ref = _relation_id(value)
        if ref and ref not in refs:
            refs.append(ref)
    for value in _coerce_list(step.get("reference_selectors")):
        ref = _relation_id(value)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _source_step(step: dict[str, Any]) -> str:
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    for key in _SOURCE_KEYS:
        value = str(fields.get(key) or step.get(key) or "").strip()
        if value:
            return _relation_id(value)
    deps = _step_deps(step)
    return deps[0] if deps else ""


def _source_path(step: dict[str, Any]) -> str:
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    return str(fields.get("workflow_source_path") or step.get("source_path") or "output").strip() or "output"


def _prompt_summary(step: dict[str, Any]) -> dict[str, Any]:
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    prompt_value = step.get("prompt_template") or step.get("prompt") or fields.get("prompt_template") or fields.get("prompt")
    summary: dict[str, Any] = {}
    if step.get("primary_skill") not in (None, "", [], {}):
        summary["primary_skill"] = step.get("primary_skill")
    if step.get("prompt_ref") not in (None, "", [], {}):
        summary["prompt_ref"] = step.get("prompt_ref")
    if prompt_value not in (None, "", [], {}):
        summary["template_preview"] = _clip(prompt_value, 220)
    if step.get("output_mode") not in (None, "", [], {}):
        summary["output_mode"] = step.get("output_mode")
    return summary


def _step_node(step: dict[str, Any]) -> dict[str, Any]:
    step_id = str(step.get("id") or "").strip()
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    result = {
        "id": step_id,
        "title": str(step.get("title") or step_id).strip(),
        "node_type": _canvas_node_type(step),
        "surface": _step_surface(step),
        "runner": str(step.get("runner") or "").strip(),
        "kind": str(step.get("kind") or "").strip(),
        "phase": str(step.get("phase") or "").strip(),
        "group": str(step.get("group") or "").strip(),
        "depends_on": _step_deps(step),
        "references": _field_refs(step),
        "repeat_group_id": str(step.get("repeat_group_id") or "").strip(),
        "repeat_group_label": str(step.get("repeat_group_label") or "").strip(),
        "repeat_group_index": step.get("repeat_group_index"),
        "template_step_id": str(step.get("template_step_id") or step_id).strip(),
        "virtual": _is_virtual_step(step),
        "prompt": _prompt_summary(step),
    }
    if fields:
        media_fields = {
            key: deepcopy(fields.get(key))
            for key in (
                "width",
                "height",
                "resolution",
                "duration_seconds",
                "fps",
                "quality",
                "aspect_ratio",
                "workflow_generate",
                "workflow_source_step",
                "workflow_source_path",
            )
            if fields.get(key) not in (None, "", [], {})
        }
        if media_fields:
            result["fields"] = media_fields
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _canvas_node(step: dict[str, Any]) -> dict[str, Any]:
    step_id = str(step.get("id") or "").strip()
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    node_type = _canvas_node_type(step)
    source_step = _source_step(step)
    result = {
        "id": step_id,
        "step_id": step_id,
        "title": str(step.get("title") or step_id).strip(),
        "type": node_type,
        "depends_on": _step_deps(step),
        "references": _field_refs(step),
        "source_step": source_step,
        "source_path": _source_path(step),
        "generated_by_node_run": bool(fields.get("workflow_generate", node_type in {"image", "video", "audio"})),
        "repeat_group_id": str(step.get("repeat_group_id") or "").strip(),
        "repeat_group_label": str(step.get("repeat_group_label") or "").strip(),
        "repeat_group_index": step.get("repeat_group_index"),
        "template_step_id": str(step.get("template_step_id") or step_id).strip(),
        "display_source": {
            "step_id": source_step,
            "path": _source_path(step),
        } if source_step else {},
        "media_settings": {
            key: deepcopy(fields.get(key))
            for key in ("width", "height", "resolution", "duration_seconds", "fps", "quality", "aspect_ratio")
            if fields.get(key) not in (None, "", [], {})
        },
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _edges(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    step_ids = {str(step.get("id") or "").strip() for step in steps if str(step.get("id") or "").strip()}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for step in steps:
        target = str(step.get("id") or "").strip()
        if not target:
            continue
        for dep in _step_deps(step):
            if dep not in step_ids:
                continue
            key = (dep, target, "depends_on")
            if key not in seen:
                seen.add(key)
                result.append({"source": dep, "target": target, "kind": "depends_on"})
        for ref in _field_refs(step):
            if ref not in step_ids:
                continue
            key = (ref, target, "reference")
            if key not in seen:
                seen.add(key)
                result.append({"source": ref, "target": target, "kind": "reference"})
    return result


def _canvas_edges(flow_edges: list[dict[str, Any]], canvas_ids: set[str]) -> list[dict[str, Any]]:
    return [
        deepcopy(edge)
        for edge in flow_edges
        if str(edge.get("source") or "") in canvas_ids and str(edge.get("target") or "") in canvas_ids
    ]


def _issue_summary(audit: dict[str, Any]) -> list[dict[str, Any]]:
    findings = audit.get("findings") if isinstance(audit.get("findings"), list) else []
    return [
        {
            key: deepcopy(item.get(key))
            for key in ("code", "severity", "message", "path", "step_id", "ref")
            if item.get(key) not in (None, "", [], {})
        }
        for item in findings[:24]
        if isinstance(item, dict)
    ]


def project_workflow_canvas(
    *,
    project_id: str,
    template_id: str = "",
    version_id: str = "",
    artifact_ref: str = "",
    repair_ref: str = "",
    workflow: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a read-only projected canvas view for a workflow source."""
    raw_workflow, normalized, input_values, source = _load_source(
        project_id=project_id,
        template_id=template_id,
        version_id=version_id,
        artifact_ref=artifact_ref,
        repair_ref=repair_ref,
        workflow=workflow,
        inputs=inputs,
        context=context,
    )
    audit = audit_workflow_spec(raw_workflow, normalized=normalized, sample_inputs=input_values)
    steps = [step for step in normalized.get("steps") or [] if isinstance(step, dict)]
    flow_nodes = [_step_node(step) for step in steps]
    canvas_nodes = [
        _canvas_node(step)
        for step in steps
        if _step_surface(step) == "draft_canvas" and not _is_virtual_step(step)
    ]
    canvas_ids = {str(node.get("step_id") or node.get("id") or "").strip() for node in canvas_nodes}
    flow_edges = _edges(steps)
    dry_run = audit.get("dry_run") if isinstance(audit.get("dry_run"), dict) else {}
    input_fields = canvas_workflow_templates.template_input_field_summaries(normalized, input_values)
    final_output_ids = [str(item) for item in dry_run.get("final_output_ids") or [] if str(item or "").strip()]
    missing_sample_outputs = _missing_sample_outputs(raw_workflow=raw_workflow, normalized=normalized)
    dynamic_inputs = {
        "status": "waiting_for_sample_outputs" if missing_sample_outputs else "ready",
        "missing_sample_outputs": missing_sample_outputs,
    }
    if missing_sample_outputs:
        source_list = ", ".join(item["source"] for item in missing_sample_outputs[:4])
        suggested_next = "provide_sample_outputs_then_reinspect"
        next_action = (
            "This projection is waiting for sample outputs from collection steps "
            f"({source_list}). Re-run workflow.canvas.inspect with context matching "
            "dynamic_inputs.missing_sample_outputs[].context_example; patch only if the expanded "
            "canvas projection still misses the user goal."
        )
    else:
        suggested_next = "compare_projection_then_patch_or_report"
        next_action = (
            "Compare canvas.nodes, canvas.edges, flow.executable_batches, final_outputs, and validation. "
            "Use workflow.spec.apply_patch with update or replace when the projection does not match the user goal."
        )
    return {
        "ok": True,
        "status": str(audit.get("status") or ""),
        "schema_version": "workflow_canvas_projection_v1",
        "source": source,
        "workflow": {
            "id": normalized.get("id"),
            "name": normalized.get("name"),
            "description": normalized.get("description") or "",
            "step_count": len(steps),
            "canvas_node_count": len(canvas_nodes),
            "final_output_ids": final_output_ids,
        },
        "inputs": {
            "values": {
                key: deepcopy(value)
                for key, value in input_values.items()
                if key not in {"context", "steps", "nodes", "outputs"}
            },
            "fields": input_fields,
            "missing_required": [
                field.get("id")
                for field in input_fields
                if isinstance(field, dict) and field.get("required") and field.get("missing")
            ],
        },
        "flow": {
            "nodes": flow_nodes,
            "edges": flow_edges,
            "executable_batches": deepcopy(dry_run.get("executable_batches") or []),
            "repeat_groups": deepcopy(dry_run.get("repeat_groups") or []),
        },
        "canvas": {
            "nodes": canvas_nodes,
            "edges": _canvas_edges(flow_edges, canvas_ids),
            "final_outputs": [
                node
                for node in canvas_nodes
                if str(node.get("step_id") or "") in final_output_ids
                or str(node.get("template_step_id") or "") in final_output_ids
            ],
        },
        "mappings": {
            "step_to_canvas": {
                str(node.get("step_id") or ""): str(node.get("id") or "")
                for node in canvas_nodes
                if str(node.get("step_id") or "")
            },
            "canvas_to_step": {
                str(node.get("id") or ""): str(node.get("step_id") or "")
                for node in canvas_nodes
                if str(node.get("id") or "")
            },
            "canvas_sources": {
                str(node.get("id") or ""): deepcopy(node.get("display_source") or {})
                for node in canvas_nodes
                if str(node.get("id") or "")
            },
        },
        "validation": {
            "status": audit.get("status"),
            "ok": bool(audit.get("ok")),
            "can_save": bool(audit.get("can_save")),
            "can_run": bool(audit.get("can_run")),
            "recommended_use": audit.get("recommended_use") or "",
            "summary": audit.get("summary") or "",
            "severity_counts": deepcopy(audit.get("severity_counts") or {}),
            "issues": _issue_summary(audit),
            "dry_run": {
                key: deepcopy(dry_run.get(key))
                for key in (
                    "status",
                    "ok",
                    "summary",
                    "step_count",
                    "executable_step_count",
                    "repeat_instance_count",
                    "duration_segment_expectation",
                    "visible_output_ids",
                    "leaf_visible_output_ids",
                    "final_output_ids",
                    "reachable_final_output_ids",
                )
                if dry_run.get(key) not in (None, "", [], {})
            } if dry_run else {},
        },
        "dynamic_inputs": dynamic_inputs,
        "suggested_next": suggested_next,
        "next_action": next_action,
    }


def project_workflow_canvas_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, FileNotFoundError):
        return {"ok": False, "error": str(exc), "error_kind": "workflow_source_not_found"}
    if isinstance(exc, json.JSONDecodeError):
        return {"ok": False, "error": str(exc), "error_kind": "workflow_source_json_error"}
    if isinstance(exc, workflow_template_store.WorkflowTemplateStoreError):
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    if isinstance(exc, canvas_workflow_templates.WorkflowTemplateError):
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    if isinstance(exc, ValueError):
        return {"ok": False, "error": str(exc), "error_kind": "workflow_canvas_projection_error"}
    return {"ok": False, "error": str(exc), "error_kind": "workflow_canvas_projection_error"}
