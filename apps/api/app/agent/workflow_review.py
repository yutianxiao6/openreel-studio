"""Compact workflow evidence for semantic review.

This module builds deterministic, read-only packets. LLM review callers can use
the packet without loading full skills, full workflow JSON, or frontend state.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent import canvas_workflow_templates, workflow_spec_artifacts
from app.agent.workflow_audit import audit_workflow_spec


_MEDIA_NODE_TYPES = {"image", "video", "audio"}
_CANVAS_KINDS = {"canvas_text", "image", "video", "audio"}
_PROMPT_KEYS = ("prompt_template", "prompt", "prompt_spec", "output", "completion")
_FIELD_PROMPT_KEYS = ("prompt", "visual_prompt", "image_prompt", "video_prompt", "audio_prompt")


def _clip_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _coerce_list(value: Any) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _relation_id(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("step")
            or value.get("id")
            or value.get("ref")
            or value.get("source_step")
            or value.get("from_step")
            or value.get("source")
        )
    text = str(value or "").strip()
    return text.split(".", 1)[0].strip() if "." in text else text


def _is_canvas_step(step: dict[str, Any]) -> bool:
    surface = str(step.get("surface") or "").strip().lower()
    visibility = str(step.get("visibility") or "").strip().lower()
    runner = str(step.get("runner") or "").strip().lower()
    kind = str(step.get("kind") or "").strip().lower().replace("-", "_")
    if surface == "workflow_runtime" or visibility in {"flow_only", "workflow_runtime"}:
        return False
    if surface == "draft_canvas" or visibility == "canvas" or runner == "workflow_canvas_output":
        return True
    return kind in _CANVAS_KINDS


def _input_fields(workflow: dict[str, Any], input_values: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        fields = canvas_workflow_templates.template_input_field_summaries(workflow, input_values)
    except Exception:
        fields = []
    result: list[dict[str, Any]] = []
    for item in fields[:16]:
        if not isinstance(item, dict):
            continue
        compact = {
            key: deepcopy(value)
            for key, value in item.items()
            if key in {"id", "label", "type", "required", "help", "description", "options", "default", "min", "max", "step"}
            and value not in (None, "", [], {})
        }
        if compact.get("id"):
            if isinstance(compact.get("help"), str):
                compact["help"] = _clip_text(compact["help"], 160)
            if isinstance(compact.get("description"), str):
                compact["description"] = _clip_text(compact["description"], 160)
            result.append(compact)
    return result


def _step_prompt_summary(step: dict[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in _PROMPT_KEYS:
        if step.get(key) not in (None, "", [], {}):
            summary[key] = _clip_text(step.get(key), 280)
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    for key in _FIELD_PROMPT_KEYS:
        if fields.get(key) not in (None, "", [], {}):
            summary[f"fields.{key}"] = _clip_text(fields.get(key), 280)
    return summary


def _step_references(step: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    for value in (
        step.get("reads_from"),
        step.get("context_refs"),
        step.get("reference_selectors"),
        fields.get("references"),
        fields.get("depends_on"),
    ):
        for item in _coerce_list(value):
            ref = _relation_id(item)
            if ref and ref not in refs:
                refs.append(ref)
    return refs[:16]


def _step_summary(step: dict[str, Any]) -> dict[str, Any]:
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    source_step = str(fields.get("workflow_source_step") or step.get("source_step") or "").strip()
    payload = {
        "id": str(step.get("id") or "").strip(),
        "title": _clip_text(step.get("title") or step.get("label") or step.get("id"), 100),
        "kind": str(step.get("kind") or "").strip(),
        "node_type": str(step.get("node_type") or step.get("type") or "").strip(),
        "phase": str(step.get("phase") or "").strip(),
        "surface": str(step.get("surface") or "").strip(),
        "visibility": str(step.get("visibility") or "").strip(),
        "runner": str(step.get("runner") or "").strip(),
        "depends_on": [_relation_id(item) for item in _coerce_list(step.get("depends_on")) if _relation_id(item)],
        "references": _step_references(step),
        "source_step": source_step,
        "repeat_group_id": str(step.get("repeat_group_id") or "").strip(),
        "repeat_group_index": step.get("repeat_group_index"),
        "template_step_id": str(step.get("template_step_id") or "").strip(),
        "output_mode": str(step.get("output_mode") or "").strip(),
        "output_schema": deepcopy(step.get("output_schema")) if isinstance(step.get("output_schema"), dict) else {},
        "prompt_summary": _step_prompt_summary(step),
        "canvas_output": _is_canvas_step(step),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _dependency_summary(steps: list[dict[str, Any]]) -> dict[str, Any]:
    step_ids = {str(step.get("id") or "").strip() for step in steps if str(step.get("id") or "").strip()}
    edges: list[dict[str, str]] = []
    roots: list[str] = []
    missing: list[dict[str, str]] = []
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        deps = [_relation_id(item) for item in _coerce_list(step.get("depends_on")) if _relation_id(item)]
        if not deps:
            roots.append(step_id)
        for dep in deps:
            edges.append({"from": dep, "to": step_id})
            if dep not in step_ids:
                missing.append({"from": dep, "to": step_id})
    downstream: dict[str, int] = {step_id: 0 for step_id in step_ids}
    for edge in edges:
        if edge["from"] in downstream:
            downstream[edge["from"]] += 1
    leaves = sorted(step_id for step_id, count in downstream.items() if count == 0)
    return {
        "root_steps": roots[:24],
        "leaf_steps": leaves[:24],
        "edge_count": len(edges),
        "edges": edges[:120],
        "missing_edges": missing[:24],
    }


def _visible_outputs(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for step in steps:
        if not _is_canvas_step(step):
            continue
        summary = _step_summary(step)
        node_type = str(summary.get("node_type") or "").lower()
        outputs.append({
            "id": summary.get("id"),
            "title": summary.get("title"),
            "node_type": summary.get("node_type"),
            "depends_on": summary.get("depends_on") or [],
            "source_step": summary.get("source_step") or ((summary.get("depends_on") or [""])[0] if summary.get("depends_on") else ""),
            "media_output": node_type in _MEDIA_NODE_TYPES,
        })
    return outputs[:80]


def _audit_summary(audit: dict[str, Any]) -> dict[str, Any]:
    findings = audit.get("findings") if isinstance(audit.get("findings"), list) else []
    dry_run = audit.get("dry_run") if isinstance(audit.get("dry_run"), dict) else {}
    return {
        "status": audit.get("status"),
        "ok": bool(audit.get("ok", False)),
        "can_save": bool(audit.get("can_save", False)),
        "can_run": bool(audit.get("can_run", False)),
        "recommended_use": audit.get("recommended_use") or ("runnable" if audit.get("can_run") else "draft_only"),
        "summary": _clip_text(audit.get("summary"), 240),
        "severity_counts": deepcopy(audit.get("severity_counts") or {}),
        "findings": [
            {
                key: item.get(key)
                for key in ("code", "severity", "message", "step_id", "ref", "path")
                if item.get(key) not in (None, "", [], {})
            }
            for item in findings[:16]
            if isinstance(item, dict)
        ],
        "dry_run": {
            "status": dry_run.get("status"),
            "ok": bool(dry_run.get("ok", False)),
            "summary": _clip_text(dry_run.get("summary"), 240),
            "sample_inputs": deepcopy(dry_run.get("sample_inputs") or {}),
            "step_count": dry_run.get("step_count"),
            "executable_step_count": dry_run.get("executable_step_count"),
            "repeat_instance_count": dry_run.get("repeat_instance_count"),
            "repeat_groups": deepcopy((dry_run.get("repeat_groups") or [])[:16]),
            "duration_segment_expectation": deepcopy(dry_run.get("duration_segment_expectation") or {}),
            "executable_batches": deepcopy((dry_run.get("executable_batches") or [])[:24]),
            "final_output_ids": deepcopy(dry_run.get("final_output_ids") or []),
            "reachable_final_output_ids": deepcopy(dry_run.get("reachable_final_output_ids") or []),
        } if dry_run else {},
    }


def build_workflow_semantic_review_evidence(
    *,
    workflow: dict[str, Any],
    normalized: dict[str, Any] | None = None,
    audit: dict[str, Any] | None = None,
    input_values: dict[str, Any] | None = None,
    user_goal: str = "",
    source: dict[str, Any] | None = None,
    max_steps: int = 80,
) -> dict[str, Any]:
    """Build compact deterministic evidence for a semantic workflow review."""
    if not isinstance(workflow, dict):
        workflow = {}
    input_values = input_values if isinstance(input_values, dict) else {}
    if not isinstance(normalized, dict):
        try:
            normalized = canvas_workflow_templates.normalize_inline_workflow(workflow, input_values=input_values)
        except Exception:
            normalized = workflow
    if not isinstance(audit, dict):
        try:
            audit = audit_workflow_spec(workflow, normalized=normalized, sample_inputs=input_values)
        except Exception as exc:
            audit = {
                "status": "blocked",
                "ok": False,
                "can_save": False,
                "can_run": False,
                "recommended_use": "blocked",
                "summary": f"Workflow audit could not run: {exc}",
                "severity_counts": {"blocking": 1},
                "findings": [{"code": "audit_exception", "severity": "blocking", "message": str(exc)}],
            }
    preview = workflow_spec_artifacts.workflow_spec_preview(workflow, normalized=normalized)
    steps = [step for step in (normalized.get("steps") if isinstance(normalized, dict) else []) or [] if isinstance(step, dict)]
    step_limit = max(1, int(max_steps or 80))
    omitted = max(0, len(steps) - step_limit)
    return {
        "schema_version": "workflow_semantic_review_evidence_v1",
        "user_goal": _clip_text(user_goal, 1200),
        "source": deepcopy(source or {}),
        "workflow": {
            "id": preview.get("id"),
            "name": preview.get("name"),
            "description": _clip_text(preview.get("description"), 500),
            "workflow_spec_version": preview.get("workflow_spec_version"),
            "step_count": len(steps),
            "dimension_count": preview.get("dimension_count"),
            "deferred_group_count": preview.get("deferred_group_count"),
            "input_ids": deepcopy(preview.get("input_ids") or []),
            "required_inputs": deepcopy(preview.get("required_inputs") or []),
        },
        "input_fields": _input_fields(normalized if isinstance(normalized, dict) else workflow, input_values),
        "dimensions": deepcopy((normalized or {}).get("dimensions") or {}),
        "deferred_groups": deepcopy(((normalized or {}).get("deferred_groups") or (normalized or {}).get("_deferred_groups") or [])[:16]),
        "steps": [_step_summary(step) for step in steps[:step_limit]],
        "omitted_step_count": omitted,
        "dependencies": _dependency_summary(steps),
        "visible_outputs": _visible_outputs(steps),
        "audit": _audit_summary(audit),
        "semantic_checklist": [
            "Workflow inputs are understandable and sufficient for the user goal.",
            "Steps cover the requested workflow without unrelated stages.",
            "Dependencies match real content flow, especially inside repeated segment instances.",
            "Visible canvas outputs use the correct upstream source steps.",
            "Prompt summaries can plausibly produce the requested text, image, video, or audio output.",
            "Dry-run final outputs match duration, segment count, and requested deliverables.",
        ],
    }
