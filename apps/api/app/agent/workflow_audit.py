"""Deterministic audit for the strict Workflow Spec v2 contract."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.agent.workflow_spec import WorkflowSpecError, compile_workflow_spec, parse_workflow_spec


class WorkflowAuditError(ValueError):
    """Raised when a workflow cannot be saved or run."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _finding(code: str, message: str, *, path: str = "") -> dict[str, Any]:
    payload = {"code": code, "severity": "blocking", "message": message}
    if path:
        payload["path"] = path
    return payload


def _flatten_plan_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for step in steps:
        result.append(step)
        if isinstance(step.get("steps"), list):
            result.extend(_flatten_plan_steps(step["steps"]))
    return result


def _execution_batches(steps: list[dict[str, Any]]) -> list[list[str]]:
    flattened = _flatten_plan_steps(steps)
    remaining = {str(step.get("id")): set(step.get("depends_on") or []) for step in flattened}
    completed: set[str] = set()
    batches: list[list[str]] = []
    while remaining:
        ready = sorted(step_id for step_id, deps in remaining.items() if deps <= completed)
        if not ready:
            break
        batches.append(ready)
        completed.update(ready)
        for step_id in ready:
            remaining.pop(step_id, None)
    return batches


def _visible_output_ids(steps: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for step in _flatten_plan_steps(steps):
        kind = str(step.get("kind") or "")
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if kind in {"image", "video", "audio"} or output.get("canvas") is True:
            result.append(str(step.get("id") or ""))
    return [item for item in result if item]


def _leaf_visible_output_ids(steps: list[dict[str, Any]]) -> list[str]:
    flattened = _flatten_plan_steps(steps)
    visible = _visible_output_ids(steps)
    visible_set = set(visible)
    dependencies = {
        str(step.get("id") or ""): {str(item) for item in step.get("depends_on") or [] if str(item)}
        for step in flattened
        if str(step.get("id") or "")
    }
    loop_outputs = {
        str(step.get("id") or ""): {
            child_id
            for child_id in _visible_output_ids(step.get("steps") or [])
        }
        for step in flattened
        if step.get("kind") == "loop" and str(step.get("id") or "")
    }
    child_parent: dict[str, str] = {}

    def map_parents(items: list[dict[str, Any]], parent_loop: str = "") -> None:
        for item in items:
            item_id = str(item.get("id") or "")
            if item_id and parent_loop:
                child_parent[item_id] = parent_loop
            next_parent = item_id if item.get("kind") == "loop" else parent_loop
            map_parents(item.get("steps") or [], next_parent)

    map_parents(steps)

    def ancestors(step_id: str) -> set[str]:
        found: set[str] = set()
        pending = list(dependencies.get(step_id) or [])
        containing_loops: set[str] = set()
        parent = child_parent.get(step_id)
        while parent:
            containing_loops.add(parent)
            parent = child_parent.get(parent)
        while pending:
            dependency = pending.pop()
            expanded = loop_outputs.get(dependency) if dependency not in containing_loops else None
            if expanded:
                for child_id in expanded:
                    if child_id not in found:
                        found.add(child_id)
                pending.extend(dependencies.get(dependency) or [])
                continue
            if dependency in found:
                continue
            found.add(dependency)
            pending.extend(dependencies.get(dependency) or [])
        return found

    consumed: set[str] = set()
    for step_id in visible:
        consumed.update(ancestors(step_id) & visible_set)
    return [step_id for step_id in visible if step_id not in consumed]


def _repeat_groups(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for step in _flatten_plan_steps(steps):
        if step.get("kind") != "loop":
            continue
        result.append({
            "id": step.get("id"),
            "title": step.get("title") or step.get("id"),
            "foreach": deepcopy(step.get("foreach") or {}),
            "child_step_ids": [
                str(child.get("id") or "")
                for child in step.get("steps") or []
                if isinstance(child, dict) and str(child.get("id") or "")
            ],
        })
    return result


def _blocked_report(findings: list[dict[str, Any]], workflow_id: str = "") -> dict[str, Any]:
    return {
        "schema_version": "openreel.workflow.audit.v2",
        "status": "blocked",
        "ok": False,
        "can_save": False,
        "can_run": False,
        "recommended_use": "blocked",
        "severity_counts": {"blocking": len(findings)},
        "summary": f"Workflow audit found {len(findings)} blocking issue(s).",
        "workflow_id": workflow_id,
        "step_count": 0,
        "visible_output_count": 0,
        "protocol": {},
        "findings": findings,
    }


def audit_workflow_spec(
    workflow: Any,
    *,
    normalized: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(workflow, str):
        try:
            workflow = json.loads(workflow)
        except json.JSONDecodeError as exc:
            return _blocked_report([_finding("json_parse_error", f"Workflow JSON parse failed: {exc}")])
    if not isinstance(workflow, dict):
        return _blocked_report([_finding("workflow_not_object", "Workflow must be an object.")])

    try:
        spec = parse_workflow_spec(workflow)
        plan = compile_workflow_spec(spec)
        from app.agent import canvas_workflow_templates

        diagnostics = canvas_workflow_templates.workflow_protocol_diagnostics(workflow)
        private = normalized or canvas_workflow_templates.normalize_inline_workflow(
            workflow,
            input_values=sample_inputs or {},
        )
    except (WorkflowSpecError, ValueError) as exc:
        return _blocked_report(
            [_finding("workflow_spec_invalid", str(exc), path="workflow")],
            str(workflow.get("id") or ""),
        )

    missing_plugins = list(diagnostics.get("missing_plugins") or [])
    if missing_plugins:
        return _blocked_report([
            _finding(
                "missing_plugin",
                "Workflow requires unavailable plugins: " + ", ".join(missing_plugins),
                path="steps",
            )
        ], spec.id)

    flattened = _flatten_plan_steps(plan["steps"])
    visible_output_ids = _visible_output_ids(plan["steps"])
    final_output_ids = _leaf_visible_output_ids(plan["steps"])
    batches = _execution_batches(plan["steps"])
    deferred = private.get("deferred_groups") if isinstance(private.get("deferred_groups"), list) else []
    dry_run = {
        "status": "pass",
        "ok": True,
        "summary": "V2 schema, dependencies, loops, references, and private execution plan are valid.",
        "sample_inputs": deepcopy(sample_inputs or {}),
        "step_count": len(flattened),
        "executable_step_count": len(private.get("steps") or []),
        "repeat_instance_count": len({
            (item.get("repeat_group_id"), item.get("repeat_group_index"))
            for item in private.get("steps") or []
            if isinstance(item, dict) and item.get("repeat_group_id")
        }),
        "repeat_groups": _repeat_groups(plan["steps"]),
        "deferred_group_ids": [str(item.get("id") or "") for item in deferred if isinstance(item, dict)],
        "executable_batches": batches,
        "visible_output_ids": visible_output_ids,
        "leaf_visible_output_ids": final_output_ids,
        "final_output_ids": final_output_ids,
        "reachable_final_output_ids": final_output_ids,
        "plan_hash": plan.get("plan_hash"),
    }
    return {
        "schema_version": "openreel.workflow.audit.v2",
        "status": "pass",
        "ok": True,
        "can_save": True,
        "can_run": True,
        "recommended_use": "runnable",
        "severity_counts": {},
        "summary": "Workflow Spec v2 audit passed.",
        "workflow_id": spec.id,
        "step_count": len(flattened),
        "visible_output_count": len(visible_output_ids),
        "protocol": {
            "protocol_version": diagnostics.get("protocol_version"),
            "execution_plan_version": diagnostics.get("execution_plan_version"),
            "supported": diagnostics.get("supported"),
            "plan_hash": diagnostics.get("plan_hash"),
        },
        "requirements": deepcopy(plan.get("requirements") or {}),
        "findings": [],
        "dry_run": dry_run,
    }


def ensure_workflow_audit_passes(
    workflow: Any,
    *,
    normalized: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = audit_workflow_spec(workflow, normalized=normalized, sample_inputs=sample_inputs)
    if not report.get("can_save"):
        raise WorkflowAuditError(str(report.get("summary") or "Workflow audit failed"), report)
    return report
