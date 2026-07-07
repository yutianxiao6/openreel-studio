"""Deterministic workflow spec audit.

The audit report is stable backend evidence. It does not call an LLM and does
not mutate workflow data.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any


_PLACEHOLDER_RE = re.compile(r"\{\{?\s*([A-Za-z][A-Za-z0-9_-]*)\.")
_DIRECT_REF_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\.")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,100}$")
_IGNORED_REF_ROOTS = {
    "input",
    "inputs",
    "context",
    "workflow",
    "runtime",
    "env",
    "item",
    "current_item",
    "previous_item",
    "user",
    "json",
    "current_character",
    "current_segment",
    "current_scene",
}
_BLOCKING_SEVERITIES = {"blocking", "high"}
_CANVAS_MEDIA_NODE_TYPES = {"image", "video", "audio"}
_WORKFLOW_INPUT_RUNNERS = {"workflow_input", "input_form", "manual_input"}
_DURATION_INPUT_KEYS = (
    "durationSeconds",
    "totalDurationSeconds",
    "duration_seconds",
    "total_duration_seconds",
)
_SEGMENT_SECONDS_INPUT_KEYS = (
    "segmentSeconds",
    "segmentDurationSeconds",
    "segment_seconds",
    "per_segment_seconds",
)
_EPISODE_COUNT_INPUT_KEYS = ("episodeCount", "episode_count", "episodes")


class WorkflowAuditError(ValueError):
    """Raised when a workflow audit blocks saving or running."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        findings = report.get("findings") if isinstance(report.get("findings"), list) else []
        first = findings[0] if findings and isinstance(findings[0], dict) else {}
        message = str(first.get("message") or report.get("summary") or "workflow audit failed")
        super().__init__(message)


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    path: str = "",
    step_id: str = "",
    ref: str = "",
) -> dict[str, Any]:
    result = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    if path:
        result["path"] = path
    if step_id:
        result["step_id"] = step_id
    if ref:
        result["ref"] = ref
    return result


def _input_ids(workflow: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    inputs = workflow.get("inputs")
    if isinstance(inputs, dict):
        result.update(str(key) for key in inputs if str(key).strip())
    elif isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                value = item.get("id") or item.get("name") or item.get("key")
            else:
                value = item
            text = str(value or "").strip()
            if text:
                result.add(text)
    schema = workflow.get("inputs_schema")
    if isinstance(schema, dict):
        result.update(str(key) for key in schema if str(key).strip())
    return result


def _raw_step_id(step: dict[str, Any], fallback: str) -> str:
    return str(step.get("id") or step.get("name") or step.get("key") or fallback).strip()


def _normalize_id_for_audit(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    if not slug or not slug[0].isalpha():
        slug = fallback
    return slug[:80].rstrip("_") or fallback


def _audit_raw_step_scope(
    steps: Any,
    *,
    path: str,
    findings: list[dict[str, Any]],
) -> None:
    if not isinstance(steps, list) or not steps:
        findings.append(_finding(
            "steps_required",
            "blocking",
            "Workflow requires a non-empty steps array.",
            path=path,
        ))
        return
    seen: dict[str, str] = {}
    for index, item in enumerate(steps, start=1):
        step_path = f"{path}[{index - 1}]"
        if not isinstance(item, dict):
            findings.append(_finding(
                "step_not_object",
                "blocking",
                "Workflow step must be an object.",
                path=step_path,
            ))
            continue
        raw_id = str(item.get("id") or "").strip()
        if not raw_id:
            findings.append(_finding(
                "step_id_required",
                "blocking",
                "Workflow step id is required.",
                path=f"{step_path}.id",
            ))
        elif not _ID_RE.fullmatch(raw_id):
            findings.append(_finding(
                "step_id_unstable",
                "blocking",
                "Workflow step id must start with a letter and contain only letters, numbers, '_' or '-'.",
                path=f"{step_path}.id",
                step_id=raw_id,
            ))
        normalized_id = _normalize_id_for_audit(raw_id, fallback=f"step_{index}")
        if normalized_id in seen:
            findings.append(_finding(
                "duplicate_step_id",
                "blocking",
                f"Workflow step id duplicates another step after normalization: {raw_id}",
                path=f"{step_path}.id",
                step_id=raw_id,
                ref=seen[normalized_id],
            ))
        else:
            seen[normalized_id] = raw_id or f"step_{index}"
        child_steps = item.get("steps")
        if isinstance(child_steps, list):
            _audit_raw_step_scope(child_steps, path=f"{step_path}.steps", findings=findings)


def _audit_raw_dependency_values(
    steps: Any,
    *,
    path: str,
    findings: list[dict[str, Any]],
) -> None:
    if not isinstance(steps, list):
        return
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or item.get("name") or f"step_{index + 1}").strip()
        step_path = f"{path}[{index}]"
        for key in ("depends_on", "needs", "depends_on_previous"):
            if key not in item:
                continue
            value = item.get(key)
            if isinstance(value, str):
                if not value.strip():
                    findings.append(_finding(
                        "empty_dependency",
                        "high",
                        f"Step {step_id} has an empty dependency in {key}.",
                        path=f"{step_path}.{key}",
                        step_id=step_id,
                    ))
                continue
            if isinstance(value, list):
                for dep_index, dep in enumerate(value):
                    text = _relation_id(dep)
                    if not text:
                        findings.append(_finding(
                            "empty_dependency",
                            "high",
                            f"Step {step_id} has an empty dependency in {key}.",
                            path=f"{step_path}.{key}[{dep_index}]",
                            step_id=step_id,
                        ))
        child_steps = item.get("steps")
        if isinstance(child_steps, list):
            _audit_raw_dependency_values(child_steps, path=f"{step_path}.steps", findings=findings)


def _coerce_list(value: Any) -> list[Any]:
    if value in (None, "", {}, []):
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
            or value.get("candidate")
            or value.get("candidates")
        )
    text = str(value or "").strip()
    if not text:
        return ""
    if "." in text:
        text = text.split(".", 1)[0].strip()
    return text


def _extract_relation_refs(value: Any) -> list[str]:
    refs: list[str] = []
    selector_keys = {"step", "id", "ref", "source_step", "from_step", "source", "candidate", "candidates"}

    def add(raw: Any) -> None:
        text = _relation_id(raw)
        if text and text not in _IGNORED_REF_ROOTS and text not in refs:
            refs.append(text)

    if isinstance(value, dict):
        selected = [value.get(key) for key in selector_keys if value.get(key) not in (None, "", [], {})]
        if selected:
            for item in selected:
                if isinstance(item, list):
                    for child in item:
                        if isinstance(child, dict) and any(child.get(key) not in (None, "", [], {}) for key in selector_keys):
                            for ref in _extract_relation_refs(child):
                                if ref not in refs:
                                    refs.append(ref)
                        else:
                            add(child)
                else:
                    add(item)
        else:
            for item in value.values():
                if isinstance(item, (dict, list)):
                    for ref in _extract_relation_refs(item):
                        if ref not in refs:
                            refs.append(ref)
                else:
                    add(item)
    elif isinstance(value, list):
        for item in value:
            for ref in _extract_relation_refs(item):
                if ref not in refs:
                    refs.append(ref)
    else:
        add(value)
    return refs


def _extract_prompt_refs(value: Any) -> list[str]:
    refs: list[str] = []

    def add(text: str) -> None:
        if text and text not in _IGNORED_REF_ROOTS and text not in refs:
            refs.append(text)

    def scan(item: Any) -> None:
        if isinstance(item, str):
            for match in _PLACEHOLDER_RE.finditer(item):
                add(match.group(1))
            direct = _DIRECT_REF_RE.match(item.strip())
            if direct:
                add(direct.group(1))
        elif isinstance(item, list):
            for child in item:
                scan(child)
        elif isinstance(item, dict):
            for child in item.values():
                scan(child)

    scan(value)
    return refs


def _step_text_refs(step: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for key in ("prompt_template", "prompt", "prompt_spec", "auto_skip_when", "completion", "output"):
        if step.get(key) not in (None, "", [], {}):
            refs.extend((ref, key) for ref in _extract_prompt_refs(step.get(key)))
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    for key in ("prompt_template", "prompt", "visual_prompt", "image_prompt", "video_prompt", "audio_prompt"):
        if fields.get(key) not in (None, "", [], {}):
            refs.extend((ref, f"fields.{key}") for ref in _extract_prompt_refs(fields.get(key)))
    return refs


def _step_local_ref_roots(step: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    for key in ("item_name", "item_source"):
        text = str(step.get(key) or "").strip()
        if text:
            roots.add(text)
            roots.add(_normalize_id_for_audit(text, fallback=text))
    repeat = step.get("repeat") if isinstance(step.get("repeat"), dict) else {}
    foreach = repeat.get("foreach") if isinstance(repeat.get("foreach"), dict) else step.get("foreach")
    for source in (repeat, foreach if isinstance(foreach, dict) else {}):
        if not isinstance(source, dict):
            continue
        text = str(source.get("scope_key") or source.get("item_name") or source.get("item") or "").strip()
        if text:
            roots.add(text)
            roots.add(_normalize_id_for_audit(text, fallback=text))
    return {root for root in roots if root}


def _step_relation_refs(step: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for key in ("depends_on", "layout_after", "reads_from", "context_refs", "depends_on_previous"):
        refs.extend((ref, key) for ref in _extract_relation_refs(step.get(key)))
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    refs.extend((ref, "fields.depends_on") for ref in _extract_relation_refs(fields.get("depends_on")))
    refs.extend((ref, "fields.references") for ref in _extract_relation_refs(fields.get("references")))
    refs.extend((ref, "reference_selectors") for ref in _extract_relation_refs(step.get("reference_selectors")))
    source_step = str(fields.get("workflow_source_step") or step.get("source_step") or "").strip()
    if source_step:
        refs.append((_relation_id(source_step), "fields.workflow_source_step"))
    return refs


def _is_canvas_step(step: dict[str, Any]) -> bool:
    surface = str(step.get("surface") or "").strip().lower()
    visibility = str(step.get("visibility") or "").strip().lower()
    runner = str(step.get("runner") or "").strip().lower()
    kind = str(step.get("kind") or "").strip().lower().replace("-", "_")
    if surface == "workflow_runtime" or visibility in {"flow_only", "workflow_runtime"}:
        return False
    if surface == "draft_canvas" or visibility == "canvas" or runner == "workflow_canvas_output":
        return True
    return kind in {"canvas_text", "image", "video", "audio"}


def _has_generation_source(step: dict[str, Any]) -> bool:
    if step.get("prompt_template") not in (None, "", [], {}):
        return True
    if step.get("prompt_ref") not in (None, "", [], {}):
        return True
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    return any(
        fields.get(key) not in (None, "", [], {})
        for key in ("prompt", "visual_prompt", "image_prompt", "video_prompt", "audio_prompt")
    )


def _resolve_ref(
    ref: str,
    *,
    current_step: dict[str, Any],
    steps: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    aliases: dict[str, str] | None = None,
    virtual_refs: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
    if ref in by_id:
        return ref, ""
    aliases = aliases or {}
    virtual_refs = virtual_refs or {}
    if ref in aliases:
        return aliases[ref], ""
    normalized_ref = _normalize_id_for_audit(ref, fallback=ref)
    if normalized_ref in aliases:
        return aliases[normalized_ref], ""
    if ref in virtual_refs:
        return ref, "virtual"
    if normalized_ref in virtual_refs:
        return normalized_ref, "virtual"
    current_group = str(current_step.get("repeat_group_id") or "").strip()
    current_index = current_step.get("repeat_group_index")

    def scoped_match(step: dict[str, Any]) -> bool:
        for value in (
            step.get("template_step_id"),
            step.get("source_node_id"),
            step.get("source_label"),
            step.get("id"),
        ):
            text = str(value or "").strip()
            if not text:
                continue
            if text == ref or _normalize_id_for_audit(text, fallback=text) == normalized_ref:
                return True
        return False

    candidates = [
        step for step in steps
        if scoped_match(step)
    ]
    if not candidates:
        return "", "unknown"
    if current_group:
        scoped = [
            step for step in candidates
            if str(step.get("repeat_group_id") or "").strip() == current_group
            and step.get("repeat_group_index") == current_index
        ]
        if len(scoped) == 1:
            return str(scoped[0].get("id") or ""), ""
    if len(candidates) == 1:
        return str(candidates[0].get("id") or ""), ""
    return "", "ambiguous_repeat_ref"


def _step_aliases(steps: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    collisions: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        values = [
            step_id,
            step.get("source_node_id"),
            step.get("source_label"),
        ]
        template_step_id = str(step.get("template_step_id") or "").strip()
        if template_step_id and len([item for item in steps if str(item.get("template_step_id") or "").strip() == template_step_id]) == 1:
            values.append(template_step_id)
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            for alias in {text, _normalize_id_for_audit(text, fallback=text)}:
                if alias in result and result[alias] != step_id:
                    collisions.add(alias)
                    continue
                result[alias] = step_id
    for alias in collisions:
        result.pop(alias, None)
    return result


def _virtual_refs(
    normalized: dict[str, Any],
    *,
    aliases: dict[str, str],
    order: dict[str, int],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def add_payload_aliases(payload: dict[str, Any], *values: Any) -> None:
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            for alias in {text, _normalize_id_for_audit(text, fallback=text)}:
                if alias:
                    result[alias] = payload

    groups = normalized.get("deferred_groups")
    if not isinstance(groups, list):
        groups = normalized.get("_deferred_groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("id") or "").strip()
            if not group_id:
                continue
            deps = []
            for dep in _extract_relation_refs(group.get("depends_on") or group.get("reads_from") or group.get("context_refs")):
                resolved = aliases.get(dep) or aliases.get(_normalize_id_for_audit(dep, fallback=dep)) or dep
                deps.append(resolved)
            dep_orders = [order[dep] for dep in deps if dep in order]
            virtual_order = max(dep_orders) + 0.5 if dep_orders else -0.5
            payload = {"id": group_id, "depends_on": deps, "order": virtual_order, "kind": "deferred_group"}
            add_payload_aliases(payload, group_id, group.get("source_node_id"))

            def add_child_aliases(children: Any) -> None:
                if not isinstance(children, list):
                    return
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    add_payload_aliases(payload, child.get("id"), child.get("source_node_id"))
                    add_child_aliases(child.get("steps"))

            add_child_aliases(group.get("steps"))

    repeat_groups: dict[str, dict[str, Any]] = {}
    for step in normalized.get("steps") or []:
        if not isinstance(step, dict):
            continue
        group_id = str(step.get("repeat_group_id") or "").strip()
        step_id = str(step.get("id") or "").strip()
        if not group_id or not step_id:
            continue
        payload = repeat_groups.setdefault(group_id, {
            "id": group_id,
            "depends_on": [],
            "order": -0.5,
            "kind": "repeat_group",
            "children": [],
        })
        payload["children"].append(step_id)
        payload["order"] = max(float(payload.get("order") or -0.5), float(order.get(step_id, -1)))
        add_payload_aliases(
            payload,
            group_id,
            step.get("repeat_group_label"),
        )
    return result


def _audit_graph(
    normalized: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
) -> None:
    steps = [step for step in normalized.get("steps") or [] if isinstance(step, dict)]
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            findings.append(_finding("step_id_required", "blocking", "Normalized workflow step id is required."))
            continue
        if step_id in by_id:
            duplicate_ids.add(step_id)
        by_id[step_id] = step
    for step_id in sorted(duplicate_ids):
        findings.append(_finding(
            "duplicate_step_id",
            "blocking",
            f"Duplicate normalized workflow step id: {step_id}",
            step_id=step_id,
        ))
    order = {str(step.get("id") or ""): index for index, step in enumerate(steps)}
    aliases = _step_aliases(steps)
    virtual_refs = _virtual_refs(normalized, aliases=aliases, order=order)

    adjacency: dict[str, list[str]] = {}
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        deps = [_relation_id(dep) for dep in _coerce_list(step.get("depends_on"))]
        deps = [dep for dep in deps if dep]
        resolved_deps: list[str] = []
        for dep in deps:
            resolved_dep, dep_reason = _resolve_ref(
                dep,
                current_step=step,
                steps=steps,
                by_id=by_id,
                aliases=aliases,
                virtual_refs=virtual_refs,
            )
            if resolved_dep and resolved_dep != dep and (resolved_dep in by_id or dep_reason == "virtual"):
                dep = resolved_dep
            if dep in by_id:
                resolved_deps.append(dep)
            if dep == step_id:
                findings.append(_finding(
                    "self_dependency",
                    "blocking",
                    f"Step {step_id} cannot depend on itself.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                    ref=dep,
                ))
            if dep not in by_id and dep not in virtual_refs:
                findings.append(_finding(
                    "unknown_dependency",
                    "blocking",
                    f"Step {step_id} depends on unknown step {dep}.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                    ref=dep,
                ))
            elif dep in by_id and order[dep] >= order[step_id]:
                findings.append(_finding(
                    "dependency_not_upstream",
                    "high",
                    f"Step {step_id} depends on {dep}, which is not upstream.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                    ref=dep,
                ))
        adjacency[step_id] = resolved_deps

    visited: set[str] = set()
    active: set[str] = set()

    def visit(step_id: str, chain: list[str]) -> None:
        if step_id in active:
            cycle = [*chain, step_id]
            findings.append(_finding(
                "dependency_cycle",
                "blocking",
                "Workflow dependency graph contains a cycle: " + " -> ".join(cycle),
                step_id=step_id,
            ))
            return
        if step_id in visited:
            return
        active.add(step_id)
        for dep in adjacency.get(step_id) or []:
            if dep in by_id:
                visit(dep, [*chain, step_id])
        active.discard(step_id)
        visited.add(step_id)

    for step_id in list(by_id):
        visit(step_id, [])

    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        refs = [*_step_relation_refs(step), *_step_text_refs(step)]
        local_ref_roots = _step_local_ref_roots(step)
        for ref, source in refs:
            if ref in local_ref_roots or _normalize_id_for_audit(ref, fallback=ref) in local_ref_roots:
                continue
            resolved, reason = _resolve_ref(
                ref,
                current_step=step,
                steps=steps,
                by_id=by_id,
                aliases=aliases,
                virtual_refs=virtual_refs,
            )
            if not resolved:
                severity = "high" if reason == "ambiguous_repeat_ref" else "blocking"
                findings.append(_finding(
                    "ambiguous_repeat_ref" if reason == "ambiguous_repeat_ref" else "unknown_step_ref",
                    severity,
                    f"Step {step_id} references unknown workflow step {ref} in {source}.",
                    path=f"steps.{step_id}.{source}",
                    step_id=step_id,
                    ref=ref,
                ))
                continue
            if resolved == step_id:
                findings.append(_finding(
                    "self_reference",
                    "high",
                    f"Step {step_id} references itself in {source}.",
                    path=f"steps.{step_id}.{source}",
                    step_id=step_id,
                    ref=ref,
                ))
                continue
            if reason == "virtual":
                virtual = virtual_refs.get(resolved) or {}
                if float(virtual.get("order") or -1) >= order.get(step_id, -1):
                    findings.append(_finding(
                        "reference_not_upstream",
                        "high",
                        f"Step {step_id} references {ref}, which is not upstream.",
                        path=f"steps.{step_id}.{source}",
                        step_id=step_id,
                        ref=ref,
                    ))
                continue
            if order.get(resolved, -1) >= order.get(step_id, -1):
                findings.append(_finding(
                    "reference_not_upstream",
                    "high",
                    f"Step {step_id} references {ref}, which is not upstream.",
                    path=f"steps.{step_id}.{source}",
                    step_id=step_id,
                    ref=ref,
                ))
            current_group = str(step.get("repeat_group_id") or "").strip()
            ref_step = by_id.get(resolved)
            if current_group and isinstance(ref_step, dict):
                ref_group = str(ref_step.get("repeat_group_id") or "").strip()
                if ref_group == current_group and ref_step.get("repeat_group_index") != step.get("repeat_group_index"):
                    findings.append(_finding(
                        "cross_instance_reference",
                        "high",
                        f"Step {step_id} references another repeat instance: {ref}.",
                        path=f"steps.{step_id}.{source}",
                        step_id=step_id,
                        ref=ref,
                    ))

    canvas_steps = [step for step in steps if _is_canvas_step(step)]
    for step in canvas_steps:
        step_id = str(step.get("id") or "").strip()
        node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
        fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
        source_step = str(fields.get("workflow_source_step") or step.get("source_step") or "").strip()
        deps = [_relation_id(dep) for dep in _coerce_list(step.get("depends_on"))]
        deps = [dep for dep in deps if dep]
        if node_type in _CANVAS_MEDIA_NODE_TYPES:
            effective_source = source_step or (deps[0] if deps else "")
            if not effective_source and not _has_generation_source(step):
                findings.append(_finding(
                    "canvas_output_missing_source",
                    "blocking",
                    f"Canvas {node_type} step {step_id} requires an upstream prompt/content source.",
                    path=f"steps.{step_id}.fields.workflow_source_step",
                    step_id=step_id,
                ))
            elif not source_step and effective_source:
                findings.append(_finding(
                    "canvas_output_implicit_source",
                    "low",
                    f"Canvas {node_type} step {step_id} uses first dependency as source; workflow_source_step is clearer.",
                    path=f"steps.{step_id}.fields.workflow_source_step",
                    step_id=step_id,
                    ref=effective_source,
                ))
        roots = _reachable_roots(step_id, adjacency)
        if not roots and deps:
            findings.append(_finding(
                "canvas_output_unreachable",
                "high",
                f"Canvas step {step_id} is not reachable from workflow roots.",
                path=f"steps.{step_id}.depends_on",
                step_id=step_id,
            ))


def _reachable_roots(step_id: str, adjacency: dict[str, list[str]]) -> set[str]:
    roots: set[str] = set()
    seen: set[str] = set()

    def walk(current: str) -> None:
        if current in seen:
            return
        seen.add(current)
        deps = adjacency.get(current) or []
        if not deps:
            roots.add(current)
            return
        for dep in deps:
            walk(dep)

    walk(step_id)
    return roots


def _scalar_sample(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return value if len(text) <= 160 else text[:157] + "..."
    return None


def _workflow_input_values_for_audit(
    workflow: dict[str, Any],
    normalized: dict[str, Any] | None,
    sample_inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def merge_mapping(mapping: Any, *, only_defaults: bool = False, override: bool = False) -> None:
        if not isinstance(mapping, dict):
            return
        for key, value in mapping.items():
            if only_defaults and isinstance(value, dict):
                value = value.get("default")
            if value not in (None, "", [], {}):
                if override:
                    result[str(key)] = deepcopy(value)
                else:
                    result.setdefault(str(key), deepcopy(value))

    for source in (workflow, normalized or {}):
        merge_mapping(source.get("defaults") if isinstance(source, dict) else None)
        merge_mapping(source.get("input_defaults") if isinstance(source, dict) else None)
        inputs = source.get("inputs") if isinstance(source, dict) else None
        if isinstance(inputs, dict):
            for key, value in inputs.items():
                if isinstance(value, dict):
                    default = value.get("default")
                    if default not in (None, "", [], {}):
                        result.setdefault(str(key), deepcopy(default))
                elif value not in (None, "", [], {}):
                    result.setdefault(str(key), deepcopy(value))
        elif isinstance(inputs, list):
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                input_id = str(item.get("id") or item.get("name") or item.get("key") or "").strip()
                if input_id and item.get("default") not in (None, "", [], {}):
                    result.setdefault(input_id, deepcopy(item.get("default")))
        merge_mapping(source.get("inputs_schema") if isinstance(source, dict) else None, only_defaults=True)
    merge_mapping(sample_inputs, override=True)
    return result


def _audit_input_value(values: dict[str, Any], keys: tuple[str, ...]) -> Any:
    if not isinstance(values, dict):
        return None
    normalized_keys = {
        _normalize_id_for_audit(key, fallback=key): key
        for key in values
    }
    for key in keys:
        if key in values and values[key] not in (None, "", [], {}):
            return values[key]
        normalized_key = _normalize_id_for_audit(key, fallback=key)
        actual_key = normalized_keys.get(normalized_key)
        if actual_key and values.get(actual_key) not in (None, "", [], {}):
            return values[actual_key]
    return None


def _positive_int(value: Any) -> int | None:
    if value in (None, "", [], {}):
        return None
    try:
        parsed = int(math.ceil(float(str(value).strip())))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _duration_segment_expectation(input_values: dict[str, Any]) -> dict[str, int] | None:
    duration = _positive_int(_audit_input_value(input_values, _DURATION_INPUT_KEYS))
    segment_seconds = _positive_int(_audit_input_value(input_values, _SEGMENT_SECONDS_INPUT_KEYS))
    if not duration or not segment_seconds:
        return None
    episode_count = _positive_int(_audit_input_value(input_values, _EPISODE_COUNT_INPUT_KEYS)) or 1
    segments_per_episode = max(1, int(math.ceil(duration / segment_seconds)))
    return {
        "duration_seconds": duration,
        "segment_seconds": segment_seconds,
        "episode_count": episode_count,
        "segments_per_episode": segments_per_episode,
        "expected_segment_instances": segments_per_episode * episode_count,
    }


def _step_is_virtual_for_dry_run(step: dict[str, Any], input_values: dict[str, Any]) -> bool:
    runner = str(step.get("runner") or "").strip().lower()
    if runner in _WORKFLOW_INPUT_RUNNERS:
        return True
    if bool(step.get("runtime_hidden")):
        return True
    condition = str(step.get("auto_skip_when") or "").strip()
    if not condition:
        return False
    match = re.fullmatch(r"\{\{\s*inputs\.([A-Za-z][A-Za-z0-9_-]*)\s*\}\}\s*<=\s*([0-9]+)", condition)
    if match:
        current = _positive_int(_audit_input_value(input_values, (match.group(1),)))
        return current is not None and current <= int(match.group(2))
    return False


def _repeat_index_int(step: dict[str, Any]) -> int | None:
    value = step.get("repeat_group_index")
    if value in (None, "", [], {}):
        scope = step.get("instance_scope") if isinstance(step.get("instance_scope"), dict) else {}
        value = scope.get("index") or scope.get("segment_index") or scope.get("segment")
    try:
        return int(str(value).strip()) if str(value or "").strip() else None
    except ValueError:
        return None


def _dependency_is_allowed_previous_instance(step: dict[str, Any], dep_step: dict[str, Any]) -> bool:
    previous_refs = {
        _relation_id(item)
        for item in _coerce_list(step.get("depends_on_previous"))
        if _relation_id(item)
    }
    if not previous_refs:
        return False
    dep_template_id = str(dep_step.get("template_step_id") or dep_step.get("id") or "").strip()
    if dep_template_id not in previous_refs:
        return False
    current_index = _repeat_index_int(step)
    dep_index = _repeat_index_int(dep_step)
    return current_index is not None and dep_index is not None and dep_index == current_index - 1


def _repeat_group_summaries(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for step in steps:
        group_id = str(step.get("repeat_group_id") or "").strip()
        step_id = str(step.get("id") or "").strip()
        if not group_id or not step_id:
            continue
        index = step.get("repeat_group_index")
        summary = groups.setdefault(group_id, {
            "id": group_id,
            "label": str(step.get("repeat_group_label") or group_id).strip(),
            "indices": [],
            "step_ids": [],
            "template_step_ids": [],
            "segment_like": False,
        })
        if index not in summary["indices"]:
            summary["indices"].append(index)
        summary["step_ids"].append(step_id)
        template_id = str(step.get("template_step_id") or "").strip()
        if template_id and template_id not in summary["template_step_ids"]:
            summary["template_step_ids"].append(template_id)
        scope = step.get("instance_scope") if isinstance(step.get("instance_scope"), dict) else {}
        segment_text = " ".join(
            str(value or "")
            for value in (
                group_id,
                summary["label"],
                template_id,
                scope.get("segment"),
                scope.get("segment_index"),
                scope.get("start_second"),
                scope.get("end_second"),
            )
        ).lower()
        if "segment" in segment_text or "分段" in segment_text or "start_second" in scope or "end_second" in scope:
            summary["segment_like"] = True
    for summary in groups.values():
        summary["indices"] = sorted(summary["indices"], key=lambda item: str(item))
        summary["instance_count"] = len(summary["indices"])
        summary["step_count"] = len(summary["step_ids"])
    return groups


def _group_dependency_children(dep_id: str, repeat_groups: dict[str, dict[str, Any]]) -> list[str]:
    group = repeat_groups.get(dep_id)
    if not group:
        return []
    return [str(item) for item in group.get("step_ids") or [] if str(item or "").strip()]


def _resolve_dependency_for_dry_run(
    dep: str,
    *,
    step: dict[str, Any],
    steps: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    virtual_refs: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    resolved, reason = _resolve_ref(
        dep,
        current_step=step,
        steps=steps,
        by_id=by_id,
        aliases=aliases,
        virtual_refs=virtual_refs,
    )
    if resolved:
        return resolved, reason
    normalized_dep = _normalize_id_for_audit(dep, fallback=dep)
    if normalized_dep in by_id:
        return normalized_dep, ""
    return "", reason or "unknown"


def dry_run_workflow_expansion(
    workflow: Any,
    *,
    normalized: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not isinstance(normalized, dict):
        return {
            "schema_version": "workflow_dry_run_report_v1",
            "status": "blocked",
            "ok": False,
            "summary": "Workflow dry-run requires a normalized workflow.",
            "findings": [
                _finding(
                    "dry_run_missing_normalized_workflow",
                    "blocking",
                    "Workflow dry-run requires a normalized workflow.",
                )
            ],
        }
    steps = [step for step in normalized.get("steps") or [] if isinstance(step, dict)]
    input_values = _workflow_input_values_for_audit(
        workflow if isinstance(workflow, dict) else {},
        normalized,
        sample_inputs,
    )
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            findings.append(_finding(
                "dry_run_step_id_required",
                "blocking",
                "Expanded workflow step id is required.",
            ))
            continue
        if step_id in by_id:
            duplicate_ids.add(step_id)
        by_id[step_id] = step
    for step_id in sorted(duplicate_ids):
        findings.append(_finding(
            "dry_run_duplicate_step_id",
            "blocking",
            f"Expanded workflow has duplicate step id: {step_id}",
            step_id=step_id,
        ))

    aliases = _step_aliases(steps)
    order = {str(step.get("id") or ""): index for index, step in enumerate(steps)}
    virtual_refs = _virtual_refs(normalized, aliases=aliases, order=order)
    repeat_groups = _repeat_group_summaries(steps)
    virtual_step_ids = {
        str(step.get("id") or "").strip()
        for step in steps
        if str(step.get("id") or "").strip() and _step_is_virtual_for_dry_run(step, input_values)
    }
    adjacency: dict[str, list[str]] = {}
    resolved_deps_by_step: dict[str, list[str]] = {}
    group_deps_by_step: dict[str, list[str]] = {}
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        resolved_deps: list[str] = []
        group_deps: list[str] = []
        for raw_dep in _coerce_list(step.get("depends_on")):
            dep = _relation_id(raw_dep)
            if not dep:
                findings.append(_finding(
                    "dry_run_empty_dependency",
                    "high",
                    f"Expanded step {step_id} has an empty dependency.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                ))
                continue
            resolved, reason = _resolve_dependency_for_dry_run(
                dep,
                step=step,
                steps=steps,
                by_id=by_id,
                aliases=aliases,
                virtual_refs=virtual_refs,
            )
            if not resolved:
                findings.append(_finding(
                    "dry_run_unknown_dependency",
                    "blocking",
                    f"Expanded step {step_id} depends on missing step {dep}.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                    ref=dep,
                ))
                continue
            if resolved == step_id:
                findings.append(_finding(
                    "dry_run_self_dependency",
                    "blocking",
                    f"Expanded step {step_id} cannot depend on itself.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                    ref=dep,
                ))
            if resolved in repeat_groups and resolved not in by_id:
                group_deps.append(resolved)
                continue
            if resolved in by_id:
                resolved_deps.append(resolved)
                dep_step = by_id[resolved]
                current_group = str(step.get("repeat_group_id") or "").strip()
                dep_group = str(dep_step.get("repeat_group_id") or "").strip()
                if (
                    current_group
                    and dep_group == current_group
                    and step.get("repeat_group_index") != dep_step.get("repeat_group_index")
                    and not _dependency_is_allowed_previous_instance(step, dep_step)
                ):
                    findings.append(_finding(
                        "dry_run_wrong_segment_dependency",
                        "high",
                        f"Expanded step {step_id} depends on another repeat instance: {dep}.",
                        path=f"steps.{step_id}.depends_on",
                        step_id=step_id,
                        ref=dep,
                    ))
                if order.get(resolved, -1) >= order.get(step_id, -1):
                    findings.append(_finding(
                        "dry_run_dependency_not_upstream",
                        "high",
                        f"Expanded step {step_id} depends on non-upstream step {dep}.",
                        path=f"steps.{step_id}.depends_on",
                        step_id=step_id,
                        ref=dep,
                    ))
            elif reason != "virtual":
                findings.append(_finding(
                    "dry_run_unknown_dependency",
                    "blocking",
                    f"Expanded step {step_id} depends on missing step {dep}.",
                    path=f"steps.{step_id}.depends_on",
                    step_id=step_id,
                    ref=dep,
                ))
        adjacency[step_id] = resolved_deps
        resolved_deps_by_step[step_id] = resolved_deps
        group_deps_by_step[step_id] = group_deps

    executable_ids = [
        str(step.get("id") or "").strip()
        for step in steps
        if str(step.get("id") or "").strip()
        and str(step.get("role") or "").strip() != "repeat_group"
        and str(step.get("id") or "").strip() not in virtual_step_ids
    ]
    completed = set(virtual_step_ids)
    remaining = set(executable_ids)
    batches: list[list[str]] = []
    while remaining:
        ready: list[str] = []
        for step_id in executable_ids:
            if step_id not in remaining:
                continue
            step_deps = resolved_deps_by_step.get(step_id) or []
            group_deps = group_deps_by_step.get(step_id) or []
            step_ready = all(dep in completed or dep not in by_id for dep in step_deps)
            groups_ready = all(
                all(child in completed or child in virtual_step_ids for child in _group_dependency_children(group_id, repeat_groups))
                for group_id in group_deps
            )
            if step_ready and groups_ready:
                ready.append(step_id)
        if not ready:
            blocked = sorted(remaining)[:12]
            findings.append(_finding(
                "dry_run_execution_order_blocked",
                "blocking",
                "Expanded workflow cannot produce an executable order for: " + ", ".join(blocked),
            ))
            break
        batches.append(ready)
        remaining.difference_update(ready)
        completed.update(ready)

    downstream: dict[str, list[str]] = {step_id: [] for step_id in by_id}
    for step_id, deps in resolved_deps_by_step.items():
        for dep in deps:
            if dep in downstream:
                downstream[dep].append(step_id)
        for group_id in group_deps_by_step.get(step_id) or []:
            for child in _group_dependency_children(group_id, repeat_groups):
                if child in downstream:
                    downstream[child].append(step_id)
    visible_outputs = [
        str(step.get("id") or "").strip()
        for step in steps
        if str(step.get("id") or "").strip() and _is_canvas_step(step)
    ]
    leaf_visible_output_ids = [
        step_id
        for step_id in visible_outputs
        if not downstream.get(step_id)
    ]
    media_leaf_output_ids = [
        step_id
        for step_id in leaf_visible_output_ids
        if str(by_id.get(step_id, {}).get("node_type") or by_id.get(step_id, {}).get("type") or "").strip().lower()
        in _CANVAS_MEDIA_NODE_TYPES
    ]
    final_output_ids = media_leaf_output_ids or leaf_visible_output_ids
    reachable_final_output_ids: list[str] = []
    for step_id in final_output_ids:
        roots = _reachable_roots(step_id, adjacency)
        if roots or not adjacency.get(step_id):
            reachable_final_output_ids.append(step_id)
        else:
            findings.append(_finding(
                "dry_run_final_output_unreachable",
                "high",
                f"Final visible output {step_id} is not reachable from workflow roots.",
                path=f"steps.{step_id}.depends_on",
                step_id=step_id,
            ))

    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id or not _is_canvas_step(step):
            continue
        node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
        if node_type not in _CANVAS_MEDIA_NODE_TYPES:
            continue
        fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
        source_step = str(fields.get("workflow_source_step") or step.get("source_step") or "").strip()
        deps = [_relation_id(dep) for dep in _coerce_list(step.get("depends_on"))]
        deps = [dep for dep in deps if dep]
        source = source_step or (deps[0] if deps else "")
        if not source:
            findings.append(_finding(
                "dry_run_canvas_source_missing",
                "blocking",
                f"Canvas {node_type} output {step_id} has no source step.",
                path=f"steps.{step_id}.fields.workflow_source_step",
                step_id=step_id,
            ))
            continue
        resolved, reason = _resolve_dependency_for_dry_run(
            source,
            step=step,
            steps=steps,
            by_id=by_id,
            aliases=aliases,
            virtual_refs=virtual_refs,
        )
        if not resolved or (resolved not in by_id and resolved not in repeat_groups):
            findings.append(_finding(
                "dry_run_canvas_source_missing",
                "blocking",
                f"Canvas {node_type} output {step_id} source step is missing: {source}.",
                path=f"steps.{step_id}.fields.workflow_source_step",
                step_id=step_id,
                ref=source,
            ))
        elif resolved in by_id and order.get(resolved, -1) >= order.get(step_id, -1):
            findings.append(_finding(
                "dry_run_canvas_source_not_upstream",
                "high",
                f"Canvas {node_type} output {step_id} source is not upstream: {source}.",
                path=f"steps.{step_id}.fields.workflow_source_step",
                step_id=step_id,
                ref=source,
            ))
        elif reason == "virtual" and virtual_refs.get(resolved, {}).get("order", -1) >= order.get(step_id, -1):
            findings.append(_finding(
                "dry_run_canvas_source_not_upstream",
                "high",
                f"Canvas {node_type} output {step_id} source is not upstream: {source}.",
                path=f"steps.{step_id}.fields.workflow_source_step",
                step_id=step_id,
                ref=source,
            ))

    expectation = _duration_segment_expectation(input_values)
    segment_group_ids: list[str] = []
    if expectation:
        expected_count = expectation["expected_segment_instances"]
        for group_id, summary in repeat_groups.items():
            if not summary.get("segment_like"):
                continue
            segment_group_ids.append(group_id)
            observed_count = int(summary.get("instance_count") or 0)
            if observed_count != expected_count:
                findings.append(_finding(
                    "dry_run_duration_segment_mismatch",
                    "high",
                    f"Repeat group {group_id} expands to {observed_count} instance(s), expected {expected_count} from duration/segmentSeconds.",
                    path=f"repeat_groups.{group_id}",
                    ref=group_id,
                ))
        if segment_group_ids:
            repeated_final_outputs = [
                step_id
                for step_id in final_output_ids
                if str(by_id.get(step_id, {}).get("repeat_group_id") or "").strip() in segment_group_ids
            ]
            if len(repeated_final_outputs) < expected_count:
                findings.append(_finding(
                    "dry_run_final_output_count_mismatch",
                    "high",
                    f"Segment workflow has {len(repeated_final_outputs)} repeated final output(s), expected at least {expected_count}.",
                    path="steps",
                ))

    blocking_count = sum(1 for item in findings if str(item.get("severity") or "") in _BLOCKING_SEVERITIES)
    return {
        "schema_version": "workflow_dry_run_report_v1",
        "status": "pass" if blocking_count == 0 else "blocked",
        "ok": blocking_count == 0,
        "summary": "Workflow dry-run passed." if blocking_count == 0 else f"Workflow dry-run found {blocking_count} blocking/high issue(s).",
        "sample_inputs": {
            key: value
            for key, value in (
                (str(key), _scalar_sample(value))
                for key, value in input_values.items()
            )
            if value is not None
        },
        "step_count": len(steps),
        "executable_step_count": len(executable_ids),
        "repeat_instance_count": sum(int(item.get("instance_count") or 0) for item in repeat_groups.values()),
        "repeat_groups": [
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "instance_count": item.get("instance_count") or 0,
                "step_count": item.get("step_count") or 0,
                "segment_like": bool(item.get("segment_like")),
                "template_step_ids": list(item.get("template_step_ids") or [])[:24],
            }
            for item in repeat_groups.values()
        ],
        "duration_segment_expectation": expectation or {},
        "executable_batches": batches,
        "executable_order": [step_id for batch in batches for step_id in batch],
        "virtual_step_ids": sorted(virtual_step_ids),
        "visible_output_ids": visible_outputs,
        "leaf_visible_output_ids": leaf_visible_output_ids,
        "final_output_ids": final_output_ids,
        "reachable_final_output_ids": reachable_final_output_ids,
        "findings": findings,
    }


def _audit_required_inputs(
    normalized: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
) -> None:
    input_ids = _input_ids(normalized)
    for item in normalized.get("required_inputs") or []:
        input_id = str(item or "").strip()
        if input_id and input_id not in input_ids:
            findings.append(_finding(
                "required_input_missing_schema",
                "high",
                f"Required workflow input {input_id} is not defined in inputs or inputs_schema.",
                path="required_inputs",
                ref=input_id,
            ))


def _audit_repeat_groups(
    normalized: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
) -> None:
    for step in normalized.get("steps") or []:
        if not isinstance(step, dict):
            continue
        child_steps = step.get("steps")
        kind = str(step.get("kind") or "").strip().lower()
        role = str(step.get("role") or "").strip().lower()
        if not isinstance(child_steps, list) and role != "repeat_group" and kind != "loop":
            continue
        step_id = str(step.get("id") or "").strip()
        if isinstance(child_steps, list) and not child_steps:
            findings.append(_finding(
                "repeat_group_empty",
                "high",
                f"Repeat group {step_id} has no child steps.",
                path=f"steps.{step_id}.steps",
                step_id=step_id,
            ))
        repeat = step.get("repeat") if isinstance(step.get("repeat"), dict) else {}
        has_repeat_source = any(
            repeat.get(key) not in (None, "", [], {})
            for key in ("foreach", "instances", "count", "episode_count", "segment_count", "episodes", "segments")
        )
        if (role == "repeat_group" or kind == "loop") and not has_repeat_source:
            findings.append(_finding(
                "repeat_group_missing_source",
                "high",
                f"Repeat group {step_id} requires repeat.foreach, instances, count, or count inputs.",
                path=f"steps.{step_id}.repeat",
                step_id=step_id,
            ))


def _audit_protocol(
    workflow: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    from app.agent import canvas_workflow_templates
    from app.agent.workflow_authoring_spec import AUTHORING_SPEC_VERSION, is_authoring_workflow

    try:
        diagnostics = canvas_workflow_templates.workflow_protocol_diagnostics(workflow)
    except Exception as exc:
        findings.append(_finding(
            "protocol_diagnostics_failed",
            "blocking",
            f"Workflow protocol diagnostics failed: {exc}",
            path="workflow_spec_version",
        ))
        return {}
    protocol_version = str(diagnostics.get("protocol_version") or "").strip()
    if protocol_version != canvas_workflow_templates.WORKFLOW_SPEC_PROTOCOL_VERSION:
        if not (
            is_authoring_workflow(workflow)
            and str(workflow.get("schema") or workflow.get("authoring_spec_version") or "").strip() == AUTHORING_SPEC_VERSION
        ):
            findings.append(_finding(
                "unsupported_protocol_version",
                "blocking",
                f"Workflow protocol version is unsupported: {protocol_version}",
                path="workflow_spec_version",
            ))
    missing = [*list(diagnostics.get("missing_capabilities") or []), *list(diagnostics.get("missing_extensions") or [])]
    if missing:
        findings.append(_finding(
            "missing_capability_or_extension",
            "blocking",
            "Workflow requires unsupported capabilities or extensions: " + ", ".join(str(item) for item in missing),
            path="required_capabilities",
        ))
    return diagnostics


def _report(
    *,
    workflow: dict[str, Any] | None,
    normalized: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    protocol: dict[str, Any] | None = None,
    dry_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    severity_counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "low")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    blocking_count = sum(severity_counts.get(item, 0) for item in _BLOCKING_SEVERITIES)
    medium_count = severity_counts.get("medium", 0)
    steps = normalized.get("steps") if isinstance(normalized, dict) and isinstance(normalized.get("steps"), list) else []
    canvas_steps = [step for step in steps if isinstance(step, dict) and _is_canvas_step(step)]
    can_save = blocking_count == 0
    can_run = can_save and medium_count == 0
    if blocking_count:
        status = "blocked"
        summary = f"Workflow audit found {blocking_count} blocking/high issue(s)."
        recommended_use = "blocked"
    elif medium_count:
        status = "warning"
        summary = f"Workflow audit found {medium_count} medium issue(s); save as draft, review before default use."
        recommended_use = "draft_only"
    else:
        status = "pass"
        summary = "Workflow audit passed."
        recommended_use = "runnable"
    report = {
        "schema_version": "workflow_audit_report_v1",
        "status": status,
        "ok": can_save,
        "can_save": can_save,
        "can_run": can_run,
        "recommended_use": recommended_use,
        "severity_counts": severity_counts,
        "summary": summary,
        "workflow_id": (normalized or workflow or {}).get("id") if isinstance((normalized or workflow), dict) else "",
        "step_count": len(steps),
        "visible_output_count": len(canvas_steps),
        "protocol": {
            key: value
            for key, value in (protocol or {}).items()
            if key in {"protocol_version", "engine_protocol_version", "supported", "required_capabilities", "required_extensions", "missing_capabilities", "missing_extensions"}
            and value not in (None, "", [], {})
        },
        "findings": findings,
    }
    if isinstance(dry_run, dict):
        report["dry_run"] = dry_run
    return report


def audit_workflow_spec(
    workflow: Any,
    *,
    normalized: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if isinstance(workflow, str):
        try:
            workflow = json.loads(workflow)
        except json.JSONDecodeError as exc:
            findings.append(_finding("json_parse_error", "blocking", f"Workflow JSON parse failed: {exc}"))
            return _report(workflow=None, normalized=None, findings=findings)
    if not isinstance(workflow, dict):
        findings.append(_finding("workflow_not_object", "blocking", "Workflow must be an object."))
        return _report(workflow=None, normalized=None, findings=findings)

    _audit_raw_step_scope(workflow.get("steps"), path="steps", findings=findings)
    _audit_raw_dependency_values(workflow.get("steps"), path="steps", findings=findings)
    protocol = _audit_protocol(workflow, findings=findings)
    if normalized is None:
        try:
            from app.agent import canvas_workflow_templates

            normalized = canvas_workflow_templates.normalize_inline_workflow(
                workflow,
                input_values=sample_inputs or {},
            )
        except Exception as exc:
            findings.append(_finding(
                "workflow_normalization_failed",
                "blocking",
                f"Workflow normalization failed: {exc}",
                path="steps",
            ))
            return _report(workflow=workflow, normalized=None, findings=findings, protocol=protocol)

    _audit_required_inputs(normalized, findings=findings)
    _audit_repeat_groups(normalized, findings=findings)
    _audit_graph(normalized, findings=findings)
    dry_run = dry_run_workflow_expansion(
        workflow,
        normalized=normalized,
        sample_inputs=sample_inputs or {},
    )
    dry_run_findings = dry_run.get("findings") if isinstance(dry_run.get("findings"), list) else []
    findings.extend(item for item in dry_run_findings if isinstance(item, dict))
    return _report(workflow=workflow, normalized=normalized, findings=findings, protocol=protocol, dry_run=dry_run)


def ensure_workflow_audit_passes(
    workflow: Any,
    *,
    normalized: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = audit_workflow_spec(workflow, normalized=normalized, sample_inputs=sample_inputs)
    if not report.get("ok"):
        raise WorkflowAuditError(report)
    return report
