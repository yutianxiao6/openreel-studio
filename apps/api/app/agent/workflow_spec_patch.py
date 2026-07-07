"""Codex-style workflow spec patch/apply service.

The model-facing tool should expose one write pipe. This module owns loading,
patching, compiling, validating, auditing, and saving workflow specs so the
tool layer stays thin.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent import canvas_workflow_templates, workflow_spec_artifacts, workflow_template_store
from app.agent.workflow_audit import WorkflowAuditError


_FRAMEWORK_CONTENT_KEYS = {
    "body",
    "caption",
    "captions",
    "content",
    "dialogue",
    "dialogues",
    "final_prompt",
    "image_prompt",
    "negative_prompt",
    "prompt",
    "script",
    "story",
    "subtitle",
    "subtitles",
    "text",
    "video_prompt",
}


class WorkflowSpecPatchError(ValueError):
    """Raised when a workflow spec patch request is invalid."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "workflow_spec_patch_error",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.payload = payload or {}


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _workflow_base(workflow: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(workflow or {})
    payload.setdefault("id", "model_authored_workflow")
    payload.setdefault("name", payload.get("id") or "模型编排工作流")
    payload.setdefault("workflow_spec_version", canvas_workflow_templates.WORKFLOW_SPEC_PROTOCOL_VERSION)
    payload.setdefault("steps", [])
    payload["reusable"] = True
    return payload


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


def _workflow_protocol_payload(template: dict[str, Any]) -> dict[str, Any]:
    protocol = template.get("protocol") if isinstance(template.get("protocol"), dict) else {}
    return {
        key: value
        for key, value in {
            "workflow_spec_version": template.get("workflow_spec_version"),
            "protocol_version": protocol.get("protocol_version") or template.get("workflow_spec_version"),
            "required_capabilities": template.get("required_capabilities") or [],
            "required_extensions": template.get("required_extensions") or [],
            "extension_ids": list((template.get("extensions") or {}).keys())
            if isinstance(template.get("extensions"), dict)
            else [],
        }.items()
        if value not in (None, "", [], {})
    }


def workflow_template_input_definitions(template: dict[str, Any]) -> list[dict[str, Any]]:
    fields = canvas_workflow_templates.template_input_field_summaries(template, {})
    result: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        cleaned = {
            key: deepcopy(value)
            for key, value in field.items()
            if key not in {"missing", "input_questions", "question", "header"}
            and value not in (None, "", [], {})
        }
        if cleaned.get("id"):
            result.append(cleaned)
    return result


def _has_filled_content_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _workflow_framework_content_issues(workflow: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    authoring_prompt_allowed = (
        str(workflow.get("schema") or workflow.get("authoring_spec_version") or "").strip()
        == "openreel.workflow.authoring.v1"
        or workflow.get("authoring") is True
    )

    def check_step(step: dict[str, Any], path: str) -> None:
        step_id = str(step.get("id") or path).strip() or path
        for key, value in step.items():
            key_text = str(key or "").strip()
            if key_text in {"fields", "steps"}:
                continue
            if authoring_prompt_allowed and key_text == "prompt":
                continue
            if key_text.lower() in _FRAMEWORK_CONTENT_KEYS and _has_filled_content_value(value):
                issues.append(f"{step_id}.{key_text}")
        fields = step.get("fields")
        if isinstance(fields, dict):
            for key, value in fields.items():
                key_text = str(key or "").strip()
                if key_text.lower() in _FRAMEWORK_CONTENT_KEYS and _has_filled_content_value(value):
                    issues.append(f"{step_id}.fields.{key_text}")
        child_steps = step.get("steps")
        if isinstance(child_steps, list):
            for index, child in enumerate(child_steps, start=1):
                if isinstance(child, dict):
                    check_step(child, f"{step_id}.steps[{index}]")

    steps = workflow.get("steps")
    if isinstance(steps, list):
        for index, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                check_step(step, f"steps[{index}]")
    return issues


def _workflow_framework_content_error(workflow: dict[str, Any]) -> dict[str, Any] | None:
    issues = _workflow_framework_content_issues(workflow)
    if not issues:
        return None
    return {
        "ok": False,
        "error": "Workflow spec must describe the framework only; fill node content during node execution",
        "error_kind": "workflow_framework_content_not_allowed",
        "content_fields": issues[:24],
        "hint": "使用 schema='openreel.workflow.authoring.v1' 保留 step prompt 作为模板；spec 写输入、步骤、循环、依赖、输出和节点设置，运行正文由节点执行生成。",
    }


def _workflow_spec_error_hint(message: str) -> str:
    text = str(message or "")
    if "repeat group requires" in text:
        return (
            "补齐循环展开来源：作者层可写 for_each='steps.plan.output.items'，"
            "或 repeat.items='{{steps.plan.output.items}}' + item_name='item'，"
            "或 repeat.count='segmentCount' + repeat.scope_key='segment'。"
        )
    if "Invalid authoring step kind" in text:
        return "使用作者层 step kind：text、plan、collection、plugin、loop、canvas_text、image、video、audio；list 可作为 collection 的别名。"
    return "修订后的 workflow 未通过最终校验；调整 patch 后重试。"


def _find_step_container(steps: list[Any], step_id: str) -> tuple[list[Any], int, dict[str, Any]] | None:
    target = str(step_id or "").strip()
    if not target:
        return None
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if str(step.get("id") or "").strip() == target:
            return steps, index, step
        child_steps = step.get("steps")
        if isinstance(child_steps, list):
            found = _find_step_container(child_steps, target)
            if found is not None:
                return found
    return None


def _insert_step_after(
    steps: list[Any],
    step: dict[str, Any],
    *,
    after_id: str = "",
    parent_step_id: str = "",
) -> None:
    parent = _find_step_container(steps, parent_step_id) if parent_step_id else None
    container = steps
    if parent is not None:
        parent_step = parent[2]
        child_steps = parent_step.setdefault("steps", [])
        if not isinstance(child_steps, list):
            child_steps = []
            parent_step["steps"] = child_steps
        container = child_steps
    if after_id:
        found = _find_step_container(container, after_id)
        if found is not None:
            found[0].insert(found[1] + 1, step)
            return
    container.append(step)


def _path_parts(path: Any) -> list[str]:
    text = str(path or "").strip()
    if not text:
        return []
    if text.startswith("/"):
        raw_parts = [part for part in text.strip("/").split("/") if part]
    else:
        raw_parts = [part for part in text.replace("/", ".").split(".") if part]
    parts: list[str] = []
    for raw_part in raw_parts:
        token = raw_part
        while token:
            if "[" not in token:
                parts.append(token)
                break
            prefix, rest = token.split("[", 1)
            if prefix:
                parts.append(prefix)
            bracket_value, sep, tail = rest.partition("]")
            if bracket_value:
                parts.append(bracket_value)
            token = tail if sep else ""
    return parts


def _set_nested_value(target: dict[str, Any], parts: list[str], value: Any) -> bool:
    if not parts:
        return False
    current: Any = target
    for part in parts[:-1]:
        if not isinstance(current, dict):
            return False
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    if not isinstance(current, dict):
        return False
    current[parts[-1]] = deepcopy(value)
    return True


def _get_nested_value(target: dict[str, Any], parts: list[str]) -> tuple[bool, Any]:
    current: Any = target
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current.get(part)
    return True, current


def _apply_path_patch(workflow: dict[str, Any], path: Any, value: Any) -> dict[str, Any]:
    parts = _path_parts(path)
    if not parts:
        return {"ok": False, "op": "path_patch", "path": str(path or ""), "error": "path_required"}
    if parts[0] == "workflow":
        exists, current = _get_nested_value(workflow, parts[1:])
        if exists and current == value:
            return {"ok": False, "op": "path_patch", "path": str(path), "target": "workflow", "error": "unchanged"}
        ok = _set_nested_value(workflow, parts[1:], value)
        if ok:
            return {"ok": True, "op": "path_patch", "path": str(path), "target": "workflow"}
        return {"ok": False, "op": "path_patch", "path": str(path), "error": "invalid_workflow_path"}
    if parts[0] == "steps" and len(parts) == 1 and isinstance(value, list):
        current = workflow.get("steps")
        if isinstance(current, list) and current == value:
            return {"ok": False, "op": "path_patch", "path": str(path), "target": "steps", "error": "unchanged"}
        workflow["steps"] = deepcopy(value)
        return {"ok": True, "op": "path_patch", "path": str(path), "target": "steps", "count": len(value)}
    if parts[0] == "steps" and len(parts) == 2 and parts[1] == "-" and isinstance(value, dict):
        steps = workflow.setdefault("steps", [])
        if not isinstance(steps, list):
            return {"ok": False, "op": "path_patch", "path": str(path), "error": "steps must be an array"}
        steps.append(deepcopy(value))
        return {"ok": True, "op": "path_patch", "path": str(path), "step_id": str(value.get("id") or "")}
    if parts[0] == "steps" and len(parts) >= 3:
        step_key = parts[1]
        found: tuple[list[Any], int, dict[str, Any]] | None = None
        if step_key.isdigit():
            index = int(step_key)
            steps = workflow.get("steps")
            if isinstance(steps, list) and 0 <= index < len(steps) and isinstance(steps[index], dict):
                found = (steps, index, steps[index])
        if found is None:
            steps = workflow.get("steps")
            found = _find_step_container(steps if isinstance(steps, list) else [], step_key)
        if found is None:
            return {"ok": False, "op": "path_patch", "path": str(path), "step_id": step_key, "error": "step_not_found"}
        exists, current = _get_nested_value(found[2], parts[2:])
        if exists and current == value:
            return {"ok": False, "op": "path_patch", "path": str(path), "step_id": str(found[2].get("id") or ""), "error": "unchanged"}
        ok = _set_nested_value(found[2], parts[2:], value)
        if ok:
            return {"ok": True, "op": "path_patch", "path": str(path), "step_id": str(found[2].get("id") or "")}
        return {"ok": False, "op": "path_patch", "path": str(path), "step_id": step_key, "error": "invalid_step_path"}
    if len(parts) >= 2:
        steps = workflow.get("steps")
        found = _find_step_container(steps if isinstance(steps, list) else [], parts[0])
        if found is not None:
            exists, current = _get_nested_value(found[2], parts[1:])
            if exists and current == value:
                return {"ok": False, "op": "path_patch", "path": str(path), "step_id": str(found[2].get("id") or ""), "error": "unchanged"}
            ok = _set_nested_value(found[2], parts[1:], value)
            if ok:
                return {"ok": True, "op": "path_patch", "path": str(path), "step_id": str(found[2].get("id") or "")}
            return {"ok": False, "op": "path_patch", "path": str(path), "step_id": parts[0], "error": "invalid_step_path"}
    return {"ok": False, "op": "path_patch", "path": str(path), "error": "unsupported_path"}


def apply_workflow_spec_patch_operations(
    workflow: dict[str, Any],
    operations: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    patched = deepcopy(workflow)
    applied: list[dict[str, Any]] = []
    steps = patched.setdefault("steps", [])
    if not isinstance(steps, list):
        steps = []
        patched["steps"] = steps
    for operation in operations:
        if not isinstance(operation, dict):
            applied.append({"ok": False, "op": "", "error": "operation must be an object"})
            continue
        action = str(operation.get("op") or operation.get("action") or "").strip()
        if not action and (operation.get("step_id") or operation.get("id")) and isinstance(operation.get("patch") or operation.get("changes"), dict):
            action = "merge_step"
        if action in {"merge_workflow", "update_workflow"}:
            patch = operation.get("patch")
            if isinstance(patch, dict):
                patched = _merge_dict(patched, patch)
                steps = patched.setdefault("steps", [])
                if not isinstance(steps, list):
                    steps = []
                    patched["steps"] = steps
                applied.append({"ok": True, "op": action})
            else:
                applied.append({"ok": False, "op": action, "error": "patch must be an object"})
            continue
        if action in {"merge_step", "update_step"}:
            found = _find_step_container(steps, str(operation.get("step_id") or operation.get("id") or ""))
            patch = operation.get("patch") if isinstance(operation.get("patch"), dict) else operation.get("changes")
            if not isinstance(patch, dict):
                patch = operation.get("fields")
            if found is not None and isinstance(patch, dict):
                found[0][found[1]] = _merge_dict(found[2], patch)
                applied.append({"ok": True, "op": action, "step_id": str(found[2].get("id") or "")})
            elif found is None:
                applied.append({"ok": False, "op": action, "step_id": operation.get("step_id") or operation.get("id"), "error": "step_not_found"})
            else:
                applied.append({"ok": False, "op": action, "error": "patch must be an object"})
            continue
        if action in {"insert_between", "insert_step_between"}:
            step = operation.get("step")
            after_id = str(operation.get("after_id") or operation.get("after_step_id") or operation.get("from_step") or "").strip()
            before_id = str(
                operation.get("before_id")
                or operation.get("before_step_id")
                or operation.get("to_step")
                or operation.get("target_step_id")
                or ""
            ).strip()
            if not isinstance(step, dict):
                applied.append({"ok": False, "op": action, "error": "step must be an object"})
                continue
            step_id = str(step.get("id") or "").strip()
            if not step_id:
                applied.append({"ok": False, "op": action, "error": "step.id is required"})
                continue
            if not after_id or not before_id:
                applied.append({"ok": False, "op": action, "step_id": step_id, "error": "after_id_and_before_id_required"})
                continue
            if _find_step_container(steps, step_id) is not None:
                applied.append({"ok": False, "op": action, "step_id": step_id, "error": "step_already_exists"})
                continue
            found_after = _find_step_container(steps, after_id)
            if found_after is None:
                applied.append({"ok": False, "op": action, "step_id": step_id, "after_id": after_id, "error": "after_step_not_found"})
                continue
            found_before = _find_step_container(steps, before_id)
            if found_before is None:
                applied.append({"ok": False, "op": action, "step_id": step_id, "before_id": before_id, "error": "before_step_not_found"})
                continue
            _insert_step_after(steps, deepcopy(step), after_id=after_id)
            deps = [
                str(dep).strip()
                for dep in (found_before[2].get("depends_on") or [])
                if str(dep).strip()
            ]
            if after_id in deps:
                deps = [step_id if dep == after_id else dep for dep in deps]
            elif step_id not in deps:
                deps.append(step_id)
            found_before[2]["depends_on"] = deps
            found_inserted = _find_step_container(steps, step_id)
            rewired = step_id in (found_before[2].get("depends_on") or [])
            applied.append({
                "ok": found_inserted is not None and rewired,
                "op": action,
                "step_id": step_id,
                "after_id": after_id,
                "before_id": before_id,
                "rewired": rewired,
            })
            continue
        if action in {"set_step_field", "replace_step_field"}:
            found = _find_step_container(steps, str(operation.get("step_id") or operation.get("id") or ""))
            field = str(operation.get("field") or "").strip()
            if found is not None and field:
                found[2][field] = deepcopy(operation.get("value"))
                applied.append({"ok": True, "op": action, "step_id": str(found[2].get("id") or ""), "field": field})
            elif found is None:
                applied.append({"ok": False, "op": action, "step_id": operation.get("step_id") or operation.get("id"), "error": "step_not_found"})
            else:
                applied.append({"ok": False, "op": action, "error": "field is required"})
            continue
        if action == "add_step":
            step = operation.get("step")
            if isinstance(step, dict):
                _insert_step_after(
                    steps,
                    deepcopy(step),
                    after_id=str(
                        operation.get("after_id")
                        or operation.get("after_step_id")
                        or operation.get("anchor_step_id")
                        or ""
                    ),
                    parent_step_id=str(operation.get("parent_step_id") or ""),
                )
                applied.append({"ok": True, "op": action, "step_id": str(step.get("id") or "")})
            else:
                applied.append({"ok": False, "op": action, "error": "step must be an object"})
            continue
        if action == "remove_step":
            found = _find_step_container(steps, str(operation.get("step_id") or operation.get("id") or ""))
            if found is not None:
                found[0].pop(found[1])
                applied.append({"ok": True, "op": action, "step_id": str(operation.get("step_id") or operation.get("id") or "")})
            else:
                applied.append({"ok": False, "op": action, "step_id": operation.get("step_id") or operation.get("id"), "error": "step_not_found"})
            continue
        if action == "replace_steps":
            replacement = operation.get("steps")
            if isinstance(replacement, list):
                patched["steps"] = deepcopy(replacement)
                steps = patched["steps"]
                applied.append({"ok": True, "op": action, "count": len(replacement)})
            else:
                applied.append({"ok": False, "op": action, "error": "steps must be an array"})
            continue
        if action in {"replace", "add"} and operation.get("path"):
            applied.append(_apply_path_patch(patched, operation.get("path"), operation.get("value")))
            steps = patched.setdefault("steps", [])
            if not isinstance(steps, list):
                steps = []
                patched["steps"] = steps
            continue
        applied.append({"ok": False, "op": action, "error": "unknown_patch_operation"})
    patched["reusable"] = True
    return patched, applied


def _load_base_workflow(
    *,
    project_id: str,
    base: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base_payload = base if isinstance(base, dict) else {}
    artifact_ref = str(base_payload.get("artifact_ref") or "").strip()
    repair_ref = str(base_payload.get("repair_ref") or "").strip()
    template_id = str(base_payload.get("template_id") or "").strip()
    version_id = str(base_payload.get("version_id") or "").strip()
    if artifact_ref:
        artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
        workflow = artifact.get("workflow")
        if not isinstance(workflow, dict):
            raise WorkflowSpecPatchError(
                "workflow spec artifact has no workflow object",
                error_kind="workflow_spec_artifact_error",
            )
        source = {
            "source_kind": "artifact",
            "artifact_ref": artifact_ref,
            "sample_inputs": deepcopy(artifact.get("sample_inputs") or {}),
            "preview": deepcopy(artifact.get("preview") or {}),
            "self_check": deepcopy(artifact.get("self_check") or {}),
        }
        return deepcopy(workflow), deepcopy(artifact), source
    if repair_ref:
        candidate = workflow_spec_artifacts.load_workflow_repair_candidate(project_id, repair_ref)
        workflow = candidate.get("workflow")
        if not isinstance(workflow, dict):
            raise WorkflowSpecPatchError(
                "workflow repair candidate has no workflow object",
                error_kind="workflow_repair_candidate_error",
            )
        source = {
            "source_kind": "repair_candidate",
            "repair_ref": repair_ref,
            "sample_inputs": deepcopy(candidate.get("sample_inputs") or {}),
            "preview": deepcopy(candidate.get("preview") or {}),
            "self_check": deepcopy(candidate.get("self_check") or {}),
        }
        return deepcopy(workflow), deepcopy(candidate), source
    if template_id:
        try:
            loaded = workflow_template_store.load_user_template(template_id, version_id)
        except workflow_template_store.WorkflowTemplateStoreError:
            workflow = canvas_workflow_templates.get_template(template_id)
            source = {
                "source_kind": "builtin_template",
                "template_id": template_id,
                "version_id": version_id,
                "sample_inputs": {},
                "preview": workflow_spec_artifacts.workflow_spec_preview(workflow),
                "self_check": {},
            }
            return deepcopy(workflow), {}, source
        source = {
            "source_kind": "user_template",
            "template_id": template_id,
            "version_id": loaded.get("summary", {}).get("active_version_id") or version_id,
            "sample_inputs": deepcopy(loaded.get("sample_inputs") or {}),
            "preview": deepcopy(loaded.get("preview") or {}),
            "self_check": deepcopy(loaded.get("self_check") or {}),
        }
        return deepcopy(loaded.get("workflow") or {}), deepcopy(loaded), source
    raise WorkflowSpecPatchError(
        "base.artifact_ref, base.repair_ref, or base.template_id is required",
        error_kind="workflow_patch_base_required",
    )


def _normalize_and_audit(
    workflow: dict[str, Any],
    *,
    sample_inputs: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    return canvas_workflow_templates.normalize_inline_workflow(
        workflow,
        input_values=_dimension_input_values(sample_inputs or {}, context),
    )


def _save_artifact(
    *,
    project_id: str,
    workflow: dict[str, Any],
    normalized: dict[str, Any],
    sample_inputs: dict[str, Any],
    user_preview: dict[str, Any],
    self_check: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    artifact = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id=project_id,
        workflow=workflow,
        normalized=normalized,
        self_check=self_check,
        user_preview=user_preview,
        sample_inputs=sample_inputs,
        source=source,
    )
    return {
        "artifact_ref": artifact["artifact_ref"],
        "template_id": "",
        "version_id": "",
        "preview": artifact.get("preview") or {},
        "audit": artifact.get("audit") or {},
        "self_check": artifact.get("self_check") or {},
        "storage_path": "",
    }


def _save_template(
    *,
    workflow: dict[str, Any],
    sample_inputs: dict[str, Any],
    user_preview: dict[str, Any],
    self_check: dict[str, Any],
    source: dict[str, Any],
    save: dict[str, Any],
) -> dict[str, Any]:
    saved = workflow_template_store.save_user_template(
        workflow=workflow,
        template_id=str(save.get("template_id") or workflow.get("id") or ""),
        name=str(save.get("name") or workflow.get("name") or ""),
        description=str(save.get("description") or workflow.get("description") or ""),
        category=str(save.get("category") or "user"),
        applies_to=str(save.get("applies_to") or workflow.get("applies_to") or ""),
        version=str(save.get("version") or workflow.get("version") or ""),
        source=source,
        sample_inputs=sample_inputs,
        self_check=self_check,
        preview=user_preview,
        replace_existing=bool(save.get("replace_existing")),
    )
    return {
        "artifact_ref": "",
        "template_id": saved.get("template_id") or "",
        "version_id": saved.get("version_id") or "",
        "preview": saved.get("preview") or saved.get("summary") or {},
        "audit": saved.get("audit") or {},
        "self_check": self_check,
        "storage_path": saved.get("storage_path") or "",
    }


def _validation_payload(normalized: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "workflow_id": normalized.get("id"),
        "step_count": len(normalized.get("steps") or []),
        "dimension_count": len(normalized.get("dimensions") or {}),
        "deferred_group_count": len(normalized.get("deferred_groups") or []),
        "reusable": True,
        "protocol": _workflow_protocol_payload(normalized),
        "audit": {
            "status": audit.get("status") if isinstance(audit, dict) else "",
            "can_save": audit.get("can_save") if isinstance(audit, dict) else None,
            "can_run": audit.get("can_run") if isinstance(audit, dict) else None,
            "recommended_use": audit.get("recommended_use") if isinstance(audit, dict) else "",
            "severity_counts": audit.get("severity_counts") if isinstance(audit, dict) else {},
        },
    }


def _repair_issues(audit: dict[str, Any] | None = None, *, fallback: str = "") -> list[dict[str, Any]]:
    if isinstance(audit, dict) and isinstance(audit.get("findings"), list):
        issues = [deepcopy(item) for item in audit["findings"] if isinstance(item, dict)]
        if issues:
            return issues[:24]
    if fallback:
        return [{"code": "workflow_validation_error", "severity": "blocking", "message": fallback}]
    return []


def _repair_strategy(issues: list[dict[str, Any]], *, op: str) -> str:
    if op == "replace":
        return "replace"
    blocking = [
        issue for issue in issues
        if str(issue.get("severity") or "") in {"blocking", "high"}
    ]
    codes = {str(issue.get("code") or "") for issue in issues}
    if len(blocking) >= 4:
        return "replace"
    if {
        "workflow_normalization_failed",
        "steps_required",
        "dependency_cycle",
        "dry_run_execution_order_blocked",
    } & codes:
        return "replace"
    return "update"


def _save_repair_response(
    *,
    project_id: str,
    workflow: dict[str, Any],
    sample_inputs: dict[str, Any],
    audit: dict[str, Any] | None,
    source: dict[str, Any],
    applied: list[dict[str, Any]],
    user_preview: dict[str, Any],
    self_check: dict[str, Any],
    error: str,
    error_kind: str,
    op: str,
    hint: str,
) -> dict[str, Any]:
    candidate = workflow_spec_artifacts.save_workflow_repair_candidate(
        project_id=project_id,
        workflow=workflow,
        sample_inputs=sample_inputs,
        audit=audit or {},
        source=source,
        applied=applied,
        user_preview=user_preview,
        self_check=self_check,
    )
    issues = _repair_issues(audit, fallback=error)
    return {
        "ok": False,
        "error": error,
        "error_kind": error_kind,
        "repair_ref": candidate["repair_ref"],
        "candidate_preview": candidate.get("preview") or {},
        "issues": issues,
        "suggested_strategy": _repair_strategy(issues, op=op),
        "audit": audit or {},
        "applied": applied,
        "hint": hint,
        "next_action": "Use workflow.spec.apply_patch with operation='update' for small fixes or operation='replace' for broad rewrites, using base.repair_ref.",
    }


def apply_workflow_spec_patch(
    *,
    project_id: str,
    operation: str,
    base: dict[str, Any] | None = None,
    workflow: dict[str, Any] | None = None,
    operations: list[dict[str, Any]] | None = None,
    sample_inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    save: dict[str, Any] | None = None,
    user_preview: dict[str, Any] | None = None,
    self_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    op = str(operation or "").strip().lower()
    if op not in {"create", "update", "replace"}:
        return {
            "ok": False,
            "error": "operation must be create, update, or replace",
            "error_kind": "workflow_patch_operation_invalid",
        }
    save_payload = save if isinstance(save, dict) else {}
    save_target = str(save_payload.get("target") or "artifact").strip().lower() or "artifact"
    if save_target not in {"artifact", "template"}:
        return {"ok": False, "error": "save.target must be artifact or template", "error_kind": "workflow_save_target_invalid"}

    base_source: dict[str, Any] = {}
    applied: list[dict[str, Any]] = []
    try:
        if op == "create":
            if not isinstance(workflow, dict):
                return {"ok": False, "error": "workflow is required for create", "error_kind": "workflow_required"}
            patched = _workflow_base(workflow)
        elif op == "replace":
            _base_workflow, _base_payload, base_source = _load_base_workflow(project_id=project_id, base=base)
            if not isinstance(workflow, dict):
                return {"ok": False, "error": "workflow is required for replace", "error_kind": "workflow_required"}
            patched = _workflow_base(workflow)
            applied = [{"ok": True, "op": "replace_workflow"}]
        else:
            base_workflow, _base_payload, base_source = _load_base_workflow(project_id=project_id, base=base)
            if not isinstance(operations, list) or not operations:
                return {"ok": False, "error": "operations is required", "error_kind": "workflow_patch_required"}
            patched, applied = apply_workflow_spec_patch_operations(base_workflow, operations)
            applied_count = sum(1 for item in applied if item.get("ok") is True)
            failed = [item for item in applied if item.get("ok") is False]
            if applied_count <= 0:
                return {
                    "ok": False,
                    "error": "No workflow spec patch operation was applied",
                    "error_kind": "workflow_patch_noop",
                    "applied": applied,
                }
            if failed:
                return {
                    "ok": False,
                    "error": "Some workflow spec patch operations failed",
                    "error_kind": "workflow_patch_failed",
                    "applied": applied,
                }
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_not_found"}
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_error"}
    except WorkflowSpecPatchError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind, **exc.payload}

    effective_inputs = (
        sample_inputs
        if isinstance(sample_inputs, dict)
        else (base_source.get("sample_inputs") if isinstance(base_source.get("sample_inputs"), dict) else {})
    )
    merged_preview = dict(user_preview) if isinstance(user_preview, dict) else {}
    merged_check = dict(self_check) if isinstance(self_check, dict) else {}
    if merged_check.get("passed") is False:
        return {
            "ok": False,
            "error": "Workflow spec self_check failed",
            "error_kind": "workflow_self_check_failed",
            "applied": applied,
            "self_check": merged_check,
        }
    if not merged_check:
        merged_check = {
            "passed": True,
            "checks": [
                "已保存 workflow spec，下一步用 workflow.canvas.inspect 验收画布映射。",
                f"操作类型：{op}。",
            ],
            "issues": [],
        }

    source = {
        "agent": "workflow_spec_apply_patch",
        "operation": op,
        "base": deepcopy(base if isinstance(base, dict) else {}),
        "base_source": deepcopy(base_source),
        "save_target": save_target,
        "operations_count": len(operations or []),
        "applied": applied,
    }
    if base_source.get("artifact_ref"):
        source["base_artifact_ref"] = base_source["artifact_ref"]
    if base_source.get("repair_ref"):
        source["base_repair_ref"] = base_source["repair_ref"]
    if op == "update":
        source["revision"] = True

    framework_error = _workflow_framework_content_error(patched)
    if framework_error is not None:
        repair_response = _save_repair_response(
            project_id=project_id,
            workflow=patched,
            sample_inputs=effective_inputs,
            audit={
                "status": "blocked",
                "ok": False,
                "can_save": False,
                "can_run": False,
                "recommended_use": "blocked",
                "summary": framework_error.get("error") or "Workflow framework content is invalid.",
                "findings": [
                    {
                        "code": framework_error.get("error_kind") or "workflow_framework_content_not_allowed",
                        "severity": "blocking",
                        "message": framework_error.get("error") or "Workflow contains generated content fields.",
                        "path": ",".join(framework_error.get("content_fields") or []),
                    }
                ],
            },
            source=source,
            applied=applied,
            user_preview=merged_preview,
            self_check=merged_check,
            error=str(framework_error.get("error") or "Workflow framework content is invalid."),
            error_kind=str(framework_error.get("error_kind") or "workflow_framework_content_not_allowed"),
            op=op,
            hint=str(framework_error.get("hint") or "调整 workflow 框架字段后重试。"),
        )
        repair_response["content_fields"] = list(framework_error.get("content_fields") or [])
        return repair_response

    try:
        normalized = _normalize_and_audit(
            patched,
            sample_inputs=effective_inputs,
            context=context if isinstance(context, dict) else {},
        )
        if op == "update":
            source["revision"] = True
        if save_target == "template":
            saved = _save_template(
                workflow=patched,
                sample_inputs=effective_inputs,
                user_preview=merged_preview,
                self_check=merged_check,
                source=source,
                save=save_payload,
            )
        else:
            saved = _save_artifact(
                project_id=project_id,
                workflow=patched,
                normalized=normalized,
                sample_inputs=effective_inputs,
                user_preview=merged_preview,
                self_check=merged_check,
                source=source,
            )
    except WorkflowAuditError as exc:
        return _save_repair_response(
            project_id=project_id,
            workflow=patched,
            sample_inputs=effective_inputs,
            audit=exc.report,
            source=source,
            applied=applied,
            user_preview=merged_preview,
            self_check=merged_check,
            error=str(exc),
            error_kind="workflow_audit_failed",
            op=op,
            hint="修订后的 workflow 未通过确定性 audit；调整 patch 后重试。",
        )
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        error_message = str(exc)
        return _save_repair_response(
            project_id=project_id,
            workflow=patched,
            sample_inputs=effective_inputs,
            audit={
                "status": "blocked",
                "ok": False,
                "can_save": False,
                "can_run": False,
                "recommended_use": "blocked",
                "summary": error_message,
                "findings": [
                    {
                        "code": "workflow_spec_error",
                        "severity": "blocking",
                        "message": error_message,
                        "path": "steps",
                    }
                ],
            },
            source=source,
            applied=applied,
            user_preview=merged_preview,
            self_check=merged_check,
            error=error_message,
            error_kind="workflow_spec_error",
            op=op,
            hint=_workflow_spec_error_hint(error_message),
        )
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_patch_error"}

    audit = saved.get("audit") if isinstance(saved.get("audit"), dict) else {}
    return {
        "ok": True,
        "status": "completed",
        "operation": op,
        "save_target": save_target,
        "artifact_ref": saved.get("artifact_ref") or "",
        "template_id": saved.get("template_id") or "",
        "version_id": saved.get("version_id") or "",
        "preview": saved.get("preview") or {},
        "input_fields": workflow_template_input_definitions(normalized),
        "validation": _validation_payload(normalized, audit),
        "audit": audit,
        "applied": applied,
        "self_check": saved.get("self_check") or merged_check,
        "storage_path": saved.get("storage_path") or "",
        "suggested_next": "call_workflow_canvas_inspect",
        "next_action": "Call workflow.canvas.inspect with the saved reference and sample inputs; patch again if projection misses the user goal.",
    }
