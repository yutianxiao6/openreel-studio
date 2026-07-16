"""Compile the public Workflow Spec v2 into the current private executor plan.

The output of this module is runtime-only. It is never persisted as a reusable
workflow document and must not be exposed in the workflow editor.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from app.agent.workflow_spec import (
    WORKFLOW_PLAN_VERSION,
    WorkflowSpec,
    WorkflowStep,
    compile_workflow_spec,
    parse_workflow_spec,
    workflow_spec_payload,
)


_STEP_PATH_RE = re.compile(r"^steps\.([A-Za-z][A-Za-z0-9_-]*)\.(.+)$")


def _prompt_template(step: WorkflowStep) -> str:
    prompt = step.prompt
    if prompt is None:
        return ""
    sections = [
        ("ROLE", prompt.role),
        ("TASK", prompt.task),
        ("OUTPUT", prompt.output),
        ("CHECK", prompt.check),
    ]
    return "\n".join(f"{label}:\n{text.strip()}" for label, text in sections if text.strip())


def _output_schema(step: WorkflowStep) -> dict[str, Any] | None:
    schema = step.output.schema_ if step.output else None
    if schema is None:
        return None
    payload = schema.model_dump(by_alias=True, exclude_none=True)
    payload["allow_extra_fields"] = payload.pop("allow_extra", False)
    return payload


def _step_paths_by_id(spec: WorkflowSpec) -> tuple[dict[str, WorkflowStep], dict[str, str | None]]:
    steps: dict[str, WorkflowStep] = {}
    parents: dict[str, str | None] = {}

    def visit(items: list[WorkflowStep], parent: str | None = None) -> None:
        for item in items:
            steps[item.id] = item
            parents[item.id] = parent
            if item.kind == "loop":
                visit(item.steps, item.id)

    visit(spec.steps)
    return steps, parents


def _ancestor_loop_ids(step_id: str, parent_by_id: dict[str, str | None]) -> set[str]:
    ancestors: set[str] = set()
    current = parent_by_id.get(step_id)
    while current and current not in ancestors:
        ancestors.add(current)
        current = parent_by_id.get(current)
    return ancestors


def _source_path(value: str) -> tuple[str, str]:
    match = _STEP_PATH_RE.fullmatch(str(value or "").strip())
    if not match:
        return "", ""
    return match.group(1), match.group(2).replace("[]", "")


def _private_uses(
    step: WorkflowStep,
    *,
    parent_by_id: dict[str, str | None],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    prompt_refs: list[dict[str, str]] = []
    prompt_selectors: list[dict[str, Any]] = []
    media_refs: list[dict[str, str]] = []
    media_selectors: list[dict[str, Any]] = []
    for use in step.uses:
        if use.select is None:
            if "vision" in use.as_:
                prompt_refs.append({"ref": use.from_, "role": "vision_context"})
            if "reference" in use.as_:
                media_refs.append({"ref": use.from_, "role": "visual_reference"})
            if "source" in use.as_:
                media_refs.append({"ref": use.from_, "role": "source_image"})
            continue

        source_step, source_path = _source_path(use.select.values)
        selector_base = {
            "from_group": parent_by_id.get(use.from_) or use.from_,
            "source_step": source_step,
            "source_path": source_path or use.select.values,
            "match_fields": list(use.select.by),
        }
        if "vision" in use.as_:
            prompt_selectors.append({**selector_base, "role": "vision_context"})
        if "reference" in use.as_:
            media_selectors.append({**selector_base, "role": "visual_reference"})
    return prompt_refs, prompt_selectors, media_refs, media_selectors


_MEDIA_RUNTIME_FIELD_KEYS = {
    "model",
    "provider",
    "aspect_ratio",
    "ratio",
    "aspect_width",
    "aspect_height",
    "ratio_width",
    "ratio_height",
    "width_ratio",
    "height_ratio",
    "resolution",
    "size",
    "dimensions",
    "width",
    "height",
    "resolution_width",
    "resolution_height",
    "pixel_width",
    "pixel_height",
    "image_width",
    "image_height",
    "video_width",
    "video_height",
    "quality",
    "fps",
}


def _private_step_fields(step: WorkflowStep) -> dict[str, Any]:
    fields = deepcopy(step.fields)
    if step.kind in {"image", "video", "audio"}:
        for key in _MEDIA_RUNTIME_FIELD_KEYS:
            fields.pop(key, None)
    return fields


def _base_private_step(
    step: WorkflowStep,
    *,
    dependencies: list[str],
    parent_loop_id: str | None,
) -> dict[str, Any]:
    clean_dependencies = [item for item in dependencies if item != parent_loop_id]
    payload: dict[str, Any] = {
        "id": step.id,
        "title": step.title,
        "depends_on": clean_dependencies,
        "fields": _private_step_fields(step),
        "optional": step.on_error == "continue",
        "manual_only": step.execution == "manual",
        "logical_step_id": step.id,
    }
    if step.description:
        payload["description"] = step.description
    if step.when is not None:
        payload["when"] = step.when.model_dump(by_alias=True, exclude_none=True)
    return payload


def _private_prompt_step(
    step: WorkflowStep,
    *,
    step_id: str,
    title: str,
    dependencies: list[str],
    parent_loop_id: str | None,
    parent_by_id: dict[str, str | None],
    canvas: bool = False,
) -> dict[str, Any]:
    prompt_refs, prompt_selectors, _, _ = _private_uses(step, parent_by_id=parent_by_id)
    payload = _base_private_step(
        step,
        dependencies=dependencies,
        parent_loop_id=parent_loop_id,
    )
    payload.update({
        "id": step_id,
        "title": title,
        "node_type": "text",
        "runner": "node.run",
        "surface": "draft_canvas" if canvas else "workflow_runtime",
        "visibility": "canvas" if canvas else "flow_only",
        "prompt_template": _prompt_template(step),
        "manual_only": False,
    })
    if prompt_refs:
        payload["context_refs"] = prompt_refs
    if prompt_selectors:
        payload["reference_selectors"] = prompt_selectors
    schema = _output_schema(step)
    if schema:
        payload["output_mode"] = "json"
        payload["output_schema"] = schema
    return payload


def _compile_private_step(
    step: WorkflowStep,
    *,
    plan_steps: dict[str, dict[str, Any]],
    parent_by_id: dict[str, str | None],
) -> list[dict[str, Any]]:
    compiled = plan_steps[step.id]
    parent_loop_id = parent_by_id.get(step.id)
    ancestor_loop_ids = _ancestor_loop_ids(step.id, parent_by_id)
    dependencies: list[str] = []
    for dependency in compiled.get("depends_on") or []:
        dependency = str(dependency)
        if dependency in ancestor_loop_ids:
            continue
        dependency_parent = parent_by_id.get(dependency)
        resolved = (
            dependency_parent
            if dependency_parent and dependency_parent not in ancestor_loop_ids
            else dependency
        )
        if resolved not in dependencies:
            dependencies.append(resolved)

    if step.kind == "loop":
        foreach = step.foreach.model_dump(by_alias=True, exclude_none=True) if step.foreach else {}
        private_foreach: dict[str, Any] = {"scope_key": foreach.get("as")}
        if foreach.get("key"):
            private_foreach["key"] = foreach["key"]
        if isinstance(foreach.get("until"), dict):
            private_foreach["until"] = deepcopy(foreach["until"])
        if foreach.get("items"):
            from_step, path = _source_path(str(foreach["items"]))
            if from_step:
                private_foreach.update({"from_step": from_step, "path": path})
            else:
                private_foreach["from"] = foreach["items"]
        else:
            count = foreach.get("count")
            if isinstance(count, str) and count.startswith("inputs."):
                count = count[len("inputs."):]
            private_foreach["count"] = count
        children: list[dict[str, Any]] = []
        for child in step.steps:
            children.extend(_compile_private_step(
                child,
                plan_steps=plan_steps,
                parent_by_id=parent_by_id,
            ))
        return [{
            "id": step.id,
            "title": step.title,
            "depends_on": [item for item in dependencies if item != parent_loop_id],
            "foreach": private_foreach,
            "repeat": {"scope_key": foreach.get("as")},
            "steps": children,
            "logical_step_id": step.id,
        }]

    if step.kind == "plugin":
        payload = _base_private_step(step, dependencies=dependencies, parent_loop_id=parent_loop_id)
        payload.update({
            "node_type": "text",
            "runner": "workflow_plugin",
            "surface": "workflow_runtime",
            "visibility": "flow_only",
            "plugin": step.plugin.model_dump(by_alias=True, exclude_none=True) if step.plugin else {},
        })
        return [payload]

    canvas = bool(step.output and step.output.canvas)
    if step.kind in {"text", "object", "collection"}:
        if not canvas:
            return [_private_prompt_step(
                step,
                step_id=step.id,
                title=step.title,
                dependencies=dependencies,
                parent_loop_id=parent_loop_id,
                parent_by_id=parent_by_id,
            )]
        prompt_id = f"{step.id}__generate"
        prompt_step = _private_prompt_step(
            step,
            step_id=prompt_id,
            title=f"{step.title} · 生成",
            dependencies=dependencies,
            parent_loop_id=parent_loop_id,
            parent_by_id=parent_by_id,
        )
        visible = _base_private_step(step, dependencies=[prompt_id], parent_loop_id=parent_loop_id)
        visible.update({
            "node_type": "text",
            "runner": "workflow_canvas_output",
            "surface": "draft_canvas",
            "visibility": "canvas",
            "fields": {
                **_private_step_fields(step),
                "workflow_source_step": prompt_id,
                "workflow_source_path": "output",
                "workflow_generate": False,
            },
            "manual_only": False,
        })
        return [prompt_step, visible]

    prompt_refs, prompt_selectors, media_refs, media_selectors = _private_uses(
        step,
        parent_by_id=parent_by_id,
    )
    private_steps: list[dict[str, Any]] = []
    media_dependencies = list(dependencies)
    if step.prompt is not None:
        prompt_id = f"{step.id}__prompt"
        private_steps.append(_private_prompt_step(
            step,
            step_id=prompt_id,
            title=f"{step.title} · 提示词",
            dependencies=dependencies,
            parent_loop_id=parent_loop_id,
            parent_by_id=parent_by_id,
        ))
        media_dependencies = [prompt_id, *dependencies]
    media = _base_private_step(step, dependencies=media_dependencies, parent_loop_id=parent_loop_id)
    media.update({
        "node_type": step.kind,
        "runner": "workflow_canvas_output",
        "surface": "draft_canvas",
        "visibility": "canvas",
        "fields": {
            **_private_step_fields(step),
            "workflow_source_step": f"{step.id}__prompt" if step.prompt is not None else "",
            "workflow_source_path": "output",
            "workflow_generate": step.execution == "auto" and step.prompt is not None,
        },
    })
    if media_refs:
        media["context_refs"] = media_refs
    if media_selectors:
        media["reference_selectors"] = media_selectors
    private_steps.append(media)
    return private_steps


def compile_private_execution_template(value: Any) -> dict[str, Any]:
    """Return a runtime-only template from a strict v2 document."""
    spec = value if isinstance(value, WorkflowSpec) else parse_workflow_spec(value)
    public_plan = compile_workflow_spec(spec)
    step_models, parent_by_id = _step_paths_by_id(spec)

    plan_steps: dict[str, dict[str, Any]] = {}

    def index(items: list[dict[str, Any]]) -> None:
        for item in items:
            plan_steps[str(item["id"])] = item
            if isinstance(item.get("steps"), list):
                index(item["steps"])

    index(public_plan["steps"])
    private_steps: list[dict[str, Any]] = []
    for step in spec.steps:
        private_steps.extend(_compile_private_step(
            step,
            plan_steps=plan_steps,
            parent_by_id=parent_by_id,
        ))

    inputs = {
        key: item.model_dump(by_alias=True, exclude_none=True)
        for key, item in spec.inputs.items()
    }
    required_inputs = [key for key, item in spec.inputs.items() if item.required]
    defaults = {
        key: deepcopy(item.default)
        for key, item in spec.inputs.items()
        if item.default is not None
    }
    return {
        "schema": WORKFLOW_PLAN_VERSION,
        "id": spec.id,
        "name": spec.title,
        "title": spec.title,
        "description": spec.description,
        "category": "workflow",
        "tags": list(spec.tags),
        "inputs": inputs,
        "inputs_schema": inputs,
        "required_inputs": required_inputs,
        "defaults": defaults,
        "steps": private_steps,
        "requirements": deepcopy(public_plan["requirements"]),
        "plan_hash": public_plan["plan_hash"],
        "ui": deepcopy(spec.ui),
        "extensions": deepcopy(spec.extensions),
        "public_spec": workflow_spec_payload(spec),
        "logical_step_ids": list(step_models),
    }
