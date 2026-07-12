"""Deferred canvas workflow tools."""
from __future__ import annotations

import uuid
import asyncio
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.agent import (
    canvas_workflow_templates,
    workflow_spec_artifacts,
    workflow_spec_patch as workflow_spec_patch_service,
    workflow_template_store,
)
from app.agent.workflow_structured_output import (
    WorkflowStructuredOutputError,
    parse_structured_output,
    structured_output_contract,
    structured_output_instructions,
)
from app.agent.workflow_audit import WorkflowAuditError, audit_workflow_spec
from app.agent.workflow_review import build_workflow_semantic_review_evidence
from app.db.session import session_scope
from app.mcp_tools import canvas_tools
from app.mcp_tools.registry import register
from app.mcp_tools.workflow_conditions import workflow_step_condition_skipped as _workflow_step_condition_skipped
from app.mcp_tools.workflow_reference_matching import (
    REFERENCE_SELECTOR_TOKEN_FIELDS as _REFERENCE_SELECTOR_TOKEN_FIELDS,
    flatten_workflow_values as _flatten_workflow_values,
    selector_key as _selector_key,
    workflow_alias_equal as _workflow_alias_equal,
    workflow_context_get as _workflow_context_get,
    workflow_token_variants as _workflow_token_variants,
    workflow_tokens_from_value as _workflow_tokens_from_value,
    workflow_tokens_match as _workflow_tokens_match,
    workflow_values_at_path as _workflow_values_at_path,
)
from app.mcp_tools.workflow_runtime_output import (
    parse_json_object as _parse_json_object,
    structured_workflow_output as _structured_workflow_output,
    workflow_output_value_type as _workflow_output_value_type,
    workflow_runtime_clean_output_value as _workflow_runtime_clean_output_value,
    workflow_runtime_clean_outputs as _workflow_runtime_clean_outputs,
    workflow_runtime_output_from_runner_payload as _workflow_runtime_output_from_runner_payload,
    workflow_runtime_output_preview,
    workflow_runtime_outputs_from_value as _workflow_runtime_outputs_from_value,
    workflow_runtime_primary_output_value as _workflow_runtime_primary_output_value,
)
from app.services import media_history
from app.services.reference_mentions import (
    build_reference_mention_candidates,
    parse_reference_mentions,
    reference_mention_instruction,
)
from app.services.project_service import ProjectService
from app.services.node_public_ids import internal_to_public_id_map, model_visible_node_payload


_WORKFLOW_STEP_METADATA_KEYS = (
    "logical_step_id",
    "source_node_id",
    "source_label",
    "source_category",
    "source_ui",
    "source_behavior",
    "mode",
    "repeat",
    "foreach",
    "bindings",
    "role",
    "start_action",
    "execution_state",
    "inputs_schema",
    "expansion",
    "collection",
    "instance_scope",
    "item_source",
    "item_name",
    "branch",
    "template_step_id",
    "expand_when",
    "expands_to",
    "repeat_group_id",
    "repeat_group_label",
    "repeat_group_index",
    "prompt_ref",
    "prompt_spec",
    "prompt_template",
    "context_refs",
    "output_mode",
    "output_schema",
    "completion",
    "operation",
    "capability",
    "plugin",
    "plugin_node_type",
    "plugin_inputs",
    "plugin_settings",
    "settings",
    "surface",
    "visibility",
    "required_capabilities",
    "required_extensions",
    "extension",
    "extension_config",
    "io",
    "x",
    "x-openreel",
    "runner",
    "reference_selectors",
    "depends_on_previous",
    "optional",
    "manual_only",
    "auto_skip_when",
    "runtime_hidden",
    "phase",
    "group",
    "kind",
    "ui",
    "authoring",
    "output",
)
_WORKFLOW_INPUT_RUNNERS = {"workflow_input", "input_form", "manual_input"}
_WORKFLOW_RUNTIME_STATE_KEY = "workflow_runtime"
_WORKFLOW_INPUT_VALUES_STATE_KEY = "workflow_input_values"
_WORKFLOW_REPAIR_ATTEMPTS_STATE_KEY = "_workflow_repair_attempts"
_WORKFLOW_RUNTIME_CONTENT_KEYS = (
    "content",
    "full_text",
    "story_text",
    "script",
    "text",
    "prompt",
    "video_prompt",
    "image_prompt",
)
_WORKFLOW_RUNTIME_MEDIA_OUTPUT_KEYS = (
    "url",
    "local_url",
    "remote_url",
    "output_path",
    "asset_id",
)
_ACTIVE_WORKFLOW_STATE_KEY = "active_workflow"


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _workflow_step_surface(step: dict[str, Any]) -> str:
    surface = str(step.get("surface") or "").strip().lower()
    visibility = str(step.get("visibility") or "").strip().lower()
    if surface == "workflow_runtime" or visibility in {"flow_only", "workflow_runtime"}:
        return "workflow_runtime"
    if surface == "draft_canvas" or visibility == "canvas":
        return "draft_canvas"
    kind = str(step.get("kind") or "").strip().lower().replace("-", "_")
    node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
    if kind in {"canvas_text", "image", "video", "audio"}:
        return "draft_canvas"
    if not kind and node_type in {"text", "image", "video", "audio"}:
        return "draft_canvas"
    return "workflow_runtime"


def _workflow_record_surface(record: dict[str, Any]) -> str:
    workflow = _workflow_metadata_from_node(record)
    return _workflow_step_surface({
        "surface": record.get("surface") or workflow.get("surface"),
        "visibility": record.get("visibility") or workflow.get("visibility"),
        "kind": workflow.get("kind") or record.get("kind"),
        "node_type": record.get("type") or workflow.get("node_type") or workflow.get("type"),
    })


def _workflow_runtime_record_canvas_output(record: dict[str, Any]) -> bool:
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    if not workflow and isinstance(record.get("input"), dict):
        input_workflow = record["input"].get("workflow")
        if isinstance(input_workflow, dict):
            workflow = input_workflow
    if _workflow_record_surface(record) == "workflow_runtime":
        return False
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), list) else []
    if record.get("node_id"):
        return True
    return any(isinstance(item, dict) and item.get("node_id") for item in artifacts)


async def _read_project_state(project_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        state = await ProjectService(session).get_project_state(project_id)
    return state if isinstance(state, dict) else {}


async def _write_project_state_patch(project_id: str, patch: dict[str, Any]) -> None:
    async with session_scope() as session:
        await ProjectService(session).update_project_state(project_id, patch)


def _workflow_review_subject_key(source: dict[str, Any], workflow: dict[str, Any] | None = None) -> str:
    if isinstance(source, dict):
        for key in ("artifact_ref", "template_id"):
            text = str(source.get(key) or "").strip()
            if text:
                return text
    if isinstance(workflow, dict):
        text = str(workflow.get("id") or workflow.get("name") or "").strip()
        if text:
            return text
        digest = hashlib.sha1(json.dumps(workflow, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        return f"inline:{digest}"
    return "workflow:unknown"


async def _record_workflow_review_repair_attempt(
    *,
    project_id: str,
    subject_key: str,
    failed: bool,
    status: str = "",
    findings: list[Any] | None = None,
) -> dict[str, Any]:
    state = await _read_project_state(project_id)
    store = state.get(_WORKFLOW_REPAIR_ATTEMPTS_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(store, dict):
        store = {}
    records = store.get("records")
    if not isinstance(records, dict):
        records = {}
    key = str(subject_key or "workflow:unknown").strip() or "workflow:unknown"
    record = records.get(key) if isinstance(records.get(key), dict) else {}
    count = int(record.get("failed_attempts") or 0)
    if failed:
        count += 1
    else:
        count = 0
    record = {
        "subject_key": key,
        "failed_attempts": count,
        "max_auto_repair_attempts": 2,
        "repair_allowed": count <= 2,
        "blocked": count > 2,
        "last_status": str(status or "").strip(),
        "last_finding_count": len(findings or []),
        "updated_at": _utc_now_iso(),
    }
    records[key] = record
    sorted_records = sorted(
        records.values(),
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )[:30]
    store = {
        "schema_version": "workflow_repair_attempts_v1",
        "updated_at": record["updated_at"],
        "records": {
            str(item.get("subject_key") or index): item
            for index, item in enumerate(sorted_records)
            if isinstance(item, dict)
        },
    }
    await _write_project_state_patch(project_id, {_WORKFLOW_REPAIR_ATTEMPTS_STATE_KEY: store})
    return record


def _workflow_run_authorization_error(source: dict[str, Any], audit: dict[str, Any] | None) -> dict[str, Any] | None:
    audit = audit if isinstance(audit, dict) else {}
    if audit.get("can_run") is True:
        return None
    severity_counts = audit.get("severity_counts") if isinstance(audit.get("severity_counts"), dict) else {}
    return {
        "ok": False,
        "error": "Workflow ref is not authorized as runnable.",
        "error_kind": "workflow_not_runnable",
        "source": deepcopy(source),
        "audit": {
            "status": audit.get("status") or "unknown",
            "can_save": audit.get("can_save"),
            "can_run": audit.get("can_run"),
            "recommended_use": audit.get("recommended_use") or "blocked",
            "summary": audit.get("summary") or "",
            "severity_counts": deepcopy(severity_counts),
        },
        "hint": "修复工作流模板并通过 audit 后，再运行这个工作流。",
    }


def _workflow_unaudited_error(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "Workflow ref has no audit report.",
        "error_kind": "workflow_unaudited",
        "source": deepcopy(source),
        "hint": "重新保存工作流模板并通过 audit 后，再运行这个工作流。",
    }


async def _authorize_workflow_for_run(
    *,
    project_id: str,
    template: dict[str, Any],
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if artifact_ref:
        try:
            artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_error"}
        audit = artifact.get("audit") if isinstance(artifact.get("audit"), dict) else {}
        if not audit:
            return _workflow_unaudited_error({"kind": "artifact", "artifact_ref": artifact_ref})
        return _workflow_run_authorization_error(
            {"kind": "artifact", "artifact_ref": artifact_ref},
            audit,
        )

    if isinstance(workflow, dict) and workflow:
        return None

    resolved_template_id = str(template.get("id") or template_id or "").strip()
    if not resolved_template_id:
        return None
    try:
        loaded = workflow_template_store.load_user_template(resolved_template_id)
    except workflow_template_store.WorkflowTemplateStoreError:
        audit = audit_workflow_spec(template, normalized=template, sample_inputs=inputs or {})
        source = {"kind": "template", "template_id": resolved_template_id, "scope": "builtin"}
    else:
        audit = loaded.get("audit") if isinstance(loaded.get("audit"), dict) else {}
        if not audit:
            return _workflow_unaudited_error({"kind": "template", "template_id": resolved_template_id, "scope": "user"})
        source = {"kind": "template", "template_id": resolved_template_id, "scope": "user"}
    return _workflow_run_authorization_error(source, audit)


def _workflow_runtime_state(state: dict[str, Any] | None) -> dict[str, Any]:
    runtime = state.get(_WORKFLOW_RUNTIME_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(runtime, dict):
        runtime = {}
    instances = runtime.get("instances")
    if not isinstance(instances, dict):
        runtime["instances"] = {}
    return runtime


def _workflow_input_values_state(state: dict[str, Any] | None) -> dict[str, Any]:
    store = state.get(_WORKFLOW_INPUT_VALUES_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(store, dict):
        store = {}
    by_workflow = store.get("by_workflow")
    by_instance = store.get("by_instance")
    if not isinstance(by_workflow, dict):
        by_workflow = {}
    if not isinstance(by_instance, dict):
        by_instance = {}
    return {
        "version": 1,
        "updated_at": str(store.get("updated_at") or ""),
        "by_workflow": deepcopy(by_workflow),
        "by_instance": deepcopy(by_instance),
    }


def _workflow_input_record_values(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    values = record.get("values")
    return deepcopy(values) if isinstance(values, dict) else {}


def workflow_input_values_public_payload(
    state: dict[str, Any] | None,
    *,
    workflow_id: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    store = _workflow_input_values_state(state)
    values: dict[str, Any] = {}
    selected_workflow_id = str(workflow_id or "").strip()
    selected_instance_id = str(instance_id or "").strip()
    if selected_instance_id:
        return _workflow_input_record_values(store["by_instance"].get(selected_instance_id))
    if selected_workflow_id:
        values.update(_workflow_input_record_values(store["by_workflow"].get(selected_workflow_id)))
    return values


def _workflow_input_workflow_id(
    *,
    template: dict[str, Any] | None = None,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
) -> str:
    if isinstance(template, dict):
        inferred = str(template.get("id") or "").strip()
        if inferred:
            return inferred
    for value in (template_id, artifact_ref):
        text = str(value or "").strip()
        if text:
            return text
    if isinstance(workflow, dict):
        return str(workflow.get("id") or workflow.get("name") or "").strip()
    return ""


async def _persist_workflow_input_values(
    *,
    project_id: str,
    template: dict[str, Any] | None = None,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    instance_id: str = "",
    inputs: dict[str, Any] | None = None,
) -> None:
    if not project_id or not isinstance(inputs, dict) or not inputs:
        return
    workflow_id = _workflow_input_workflow_id(
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
    )
    selected_instance_id = str(instance_id or "").strip()
    if not workflow_id and not selected_instance_id:
        return
    state = await _read_project_state(project_id)
    store = _workflow_input_values_state(state)
    now = _utc_now_iso()
    record = {
        "workflow_id": workflow_id,
        "artifact_ref": str(artifact_ref or "").strip(),
        "updated_at": now,
        "values": deepcopy(inputs),
    }
    if selected_instance_id:
        store["by_instance"][selected_instance_id] = {
            **deepcopy(record),
            "instance_id": selected_instance_id,
        }
    elif workflow_id:
        store["by_workflow"][workflow_id] = deepcopy(record)
    store["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_INPUT_VALUES_STATE_KEY: store})


async def _saved_workflow_input_values(
    *,
    project_id: str,
    template: dict[str, Any] | None = None,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    if not project_id:
        return {}
    state = await _read_project_state(project_id)
    workflow_id = _workflow_input_workflow_id(
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
    )
    if not workflow_id:
        active = state.get(_ACTIVE_WORKFLOW_STATE_KEY) if isinstance(state, dict) else None
        if isinstance(active, dict):
            kind = str(active.get("kind") or "").strip().lower()
            if kind == "template":
                workflow_id = str(active.get("template_id") or "").strip()
            elif kind == "artifact":
                workflow_id = str(active.get("artifact_ref") or "").strip()
            elif kind == "imported":
                active_workflow = active.get("workflow") if isinstance(active.get("workflow"), dict) else {}
                workflow_id = str(active_workflow.get("id") or active.get("name") or "").strip()
    return workflow_input_values_public_payload(
        state,
        workflow_id=workflow_id,
        instance_id=instance_id,
    )


async def _workflow_inputs_with_saved_values(
    *,
    project_id: str,
    template: dict[str, Any] | None = None,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    instance_id: str = "",
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = await _saved_workflow_input_values(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
    )
    if isinstance(inputs, dict):
        merged.update(deepcopy(inputs))
    return merged


async def _workflow_instance_template_mismatch_error(
    *,
    project_id: str,
    instance_id: str,
    template_id: str,
) -> dict[str, Any] | None:
    target_instance_id = str(instance_id or "").strip()
    target_template_id = str(template_id or "").strip()
    if not target_instance_id or not target_template_id:
        return None
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    instance = instances.get(target_instance_id)
    if not isinstance(instance, dict):
        return None
    existing_template_id = str(instance.get("template_id") or "").strip()
    if not existing_template_id or existing_template_id == target_template_id:
        return None
    return {
        "ok": False,
        "error": "The selected workflow instance belongs to a different template. Refresh the workflow instance selection and run again.",
        "error_kind": "workflow_instance_template_mismatch",
        "project_id": project_id,
        "instance_id": target_instance_id,
        "template_id": target_template_id,
        "instance_template_id": existing_template_id,
    }


def _workflow_runtime_instance(runtime: dict[str, Any], instance_id: str) -> dict[str, Any]:
    instances = runtime.setdefault("instances", {})
    instance = instances.get(instance_id)
    if not isinstance(instance, dict):
        instance = {}
        instances[instance_id] = instance
    steps = instance.get("steps")
    if not isinstance(steps, dict):
        instance["steps"] = {}
    return instance


def _runtime_step_record_id(instance_id: str, step_id: str) -> str:
    return f"workflow-runtime:{instance_id}:{step_id}"


def _workflow_downstream_step_ids(template: dict[str, Any], changed_step_id: str) -> set[str]:
    steps = [step for step in template.get("steps") or [] if isinstance(step, dict)]
    deps_by_step: dict[str, set[str]] = {}
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        deps_by_step[step_id] = {str(dep or "").strip() for dep in step.get("depends_on") or [] if str(dep or "").strip()}
    changed = {str(changed_step_id or "").strip()}
    result: set[str] = set()
    progressed = True
    while progressed:
        progressed = False
        for candidate_id, deps in deps_by_step.items():
            if candidate_id in result or candidate_id in changed:
                continue
            if deps & (changed | result):
                result.add(candidate_id)
                progressed = True
    return result


def _workflow_logical_target_steps(
    template: dict[str, Any],
    target_step: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every private phase represented by one public workflow step."""
    steps = [step for step in template.get("steps") or [] if isinstance(step, dict)]
    logical_id = str(
        target_step.get("logical_step_id")
        or target_step.get("template_step_id")
        or target_step.get("id")
        or ""
    ).strip()
    if not logical_id:
        return [target_step]
    group_id = str(target_step.get("repeat_group_id") or "").strip()
    group_index = str(target_step.get("repeat_group_index") or "").strip()
    scope = target_step.get("instance_scope") if isinstance(target_step.get("instance_scope"), dict) else {}

    def same_scope(step: dict[str, Any]) -> bool:
        candidate_logical_id = str(
            step.get("logical_step_id")
            or step.get("template_step_id")
            or step.get("id")
            or ""
        ).strip()
        if candidate_logical_id != logical_id:
            return False
        if str(step.get("repeat_group_id") or "").strip() != group_id:
            return False
        if str(step.get("repeat_group_index") or "").strip() != group_index:
            return False
        candidate_scope = step.get("instance_scope") if isinstance(step.get("instance_scope"), dict) else {}
        return not scope or not candidate_scope or candidate_scope == scope

    phases = [step for step in steps if same_scope(step)]
    return phases or [target_step]


async def _prepare_workflow_runtime_manual_rerun(
    *,
    project_id: str,
    template: dict[str, Any],
    instance_id: str,
    target_steps: list[dict[str, Any]],
    requested_step_id: str,
) -> dict[str, Any] | None:
    """Invalidate descendants before an explicit single-step rerun.

    Outputs and artifacts remain available as history, while their active run
    status is reset so the next run-all resumes from the rerun boundary.
    """
    target_instance_id = str(instance_id or "").strip()
    if not project_id or not target_instance_id or not target_steps:
        return None
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    instance = instances.get(target_instance_id)
    if not isinstance(instance, dict):
        return None
    records = instance.get("steps") if isinstance(instance.get("steps"), dict) else {}
    target_ids = {
        str(step.get("id") or "").strip()
        for step in target_steps
        if str(step.get("id") or "").strip()
    }
    has_previous_run = any(
        isinstance(records.get(step_id), dict)
        and (
            int(records[step_id].get("run_count") or 0) > 0
            or str(records[step_id].get("status") or "").strip() not in {"", "idle", "draft"}
        )
        for step_id in target_ids
    )
    if not has_previous_run:
        return None

    downstream_ids: set[str] = set()
    for target_id in target_ids:
        downstream_ids.update(_workflow_downstream_step_ids(template, target_id))
    downstream_ids.difference_update(target_ids)
    now = _utc_now_iso()
    changed = False
    for downstream_id in downstream_ids:
        record = records.get(downstream_id)
        if not isinstance(record, dict):
            continue
        record.update({
            "status": "idle",
            "stale": True,
            "invalidated_by": str(requested_step_id or "").strip() or next(iter(target_ids), ""),
            "invalidated_at": now,
            "updated_at": now,
        })
        for key in ("error", "last_error", "last_started_at", "last_failed_at"):
            record.pop(key, None)
        changed = True
    if not changed:
        return None

    instance.update({
        "status": "partial",
        "last_rerun_step_id": str(requested_step_id or "").strip(),
        "last_rerun_started_at": now,
        "updated_at": now,
    })
    instance.pop("run_all_active", None)
    runtime["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    state_for_payload = {**state, _WORKFLOW_RUNTIME_STATE_KEY: runtime}
    runtime_payload = _workflow_runtime_public_payload(
        state_for_payload,
        template_id=str(instance.get("template_id") or template.get("id") or ""),
        instance_id=target_instance_id,
    )
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(instance.get("template_id") or template.get("id") or ""),
        instance_id=target_instance_id,
        step_id=str(requested_step_id or "").strip(),
        status="partial",
        runtime=runtime_payload,
    )
    return runtime_payload


def _workflow_runtime_resolved_inputs(fields: dict[str, Any]) -> list[dict[str, Any]]:
    workflow = fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {}
    result: list[dict[str, Any]] = []
    input_facts = workflow.get("input_facts")
    if input_facts in (None, "", [], {}):
        input_facts = fields.get("input_values")
    if input_facts not in (None, "", [], {}):
        result.append({
            "name": "input_values",
            "source": "workflow_inputs",
            "type": _workflow_output_value_type(input_facts),
            "value": deepcopy(input_facts),
        })
    references = fields.get("references")
    if isinstance(references, list):
        for index, ref in enumerate(references):
            if not isinstance(ref, dict):
                continue
            value = str(ref.get("ref") or ref.get("nodeId") or ref.get("node_id") or "").strip()
            if not value:
                continue
            role = str(ref.get("role") or "context").strip() or "context"
            result.append({
                "name": role or f"reference_{index + 1}",
                "source": "canvas_node",
                "ref": value,
                "role": role,
            })
    return result


def _workflow_runtime_artifact_from_node(node: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    fields = node.get("input") if isinstance(node.get("input"), dict) else {}
    surface = node.get("surface") or fields.get("surface")
    artifact = {
        "kind": "canvas_node",
        "node_id": node.get("id"),
        "type": node.get("type"),
        "title": node.get("title"),
        "status": node.get("status"),
        "surface": surface or _workflow_step_surface({"surface": surface}),
    }
    if isinstance(result, dict):
        if result.get("url"):
            artifact["url"] = result.get("url")
        if result.get("asset_id"):
            artifact["asset_id"] = result.get("asset_id")
        if result.get("output_path"):
            artifact["output_path"] = result.get("output_path")
    return {key: value for key, value in artifact.items() if value not in (None, "", [], {})}


def _runtime_record_from_state(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else fields.get("workflow")
    if not isinstance(workflow, dict):
        workflow = {}
    output = _workflow_runtime_clean_output_value(record.get("output"), drop_internal_keys=True)
    outputs = _workflow_runtime_clean_outputs(record.get("outputs"), drop_internal_keys=True) if isinstance(record.get("outputs"), list) else None
    if outputs is None:
        outputs = _workflow_runtime_outputs_from_value(output)
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), list) else []
    return {
        "id": record.get("id") or _runtime_step_record_id(str(workflow.get("instance_id") or ""), str(workflow.get("step_id") or "")),
        "display_id": None,
        "type": record.get("type") or "text",
        "title": record.get("title") or workflow.get("step_id") or "Workflow Runtime Step",
        "status": record.get("status") or "idle",
        "surface": record.get("surface") or workflow.get("surface") or "workflow_runtime",
        "visibility": record.get("visibility") or workflow.get("visibility") or "",
        "input": fields,
        "resolved_inputs": record.get("resolved_inputs") if isinstance(record.get("resolved_inputs"), list) else [],
        "output": output,
        "outputs": outputs,
        "artifacts": artifacts,
        "node_id": record.get("node_id") or "",
        "run_count": record.get("run_count") or 0,
        "stale": bool(record.get("stale")),
        "invalidated_by": record.get("invalidated_by") or "",
        "workflow": workflow,
        "created_at": record.get("created_at") or "",
        "updated_at": record.get("updated_at") or "",
    }


def _workflow_runtime_records_from_state(
    state: dict[str, Any],
    *,
    template_id: str,
    instance_id: str = "",
) -> list[dict[str, Any]]:
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    records: list[dict[str, Any]] = []
    for candidate_instance_id, instance in instances.items():
        if not isinstance(instance, dict):
            continue
        if template_id and str(instance.get("template_id") or "").strip() != template_id:
            continue
        if instance_id and str(candidate_instance_id or "").strip() != instance_id:
            continue
        steps = instance.get("steps") if isinstance(instance.get("steps"), dict) else {}
        for record in steps.values():
            if isinstance(record, dict):
                records.append(_runtime_record_from_state(record))
    return records


async def _workflow_runtime_records_from_project(
    project_id: str,
    *,
    template_id: str,
    instance_id: str = "",
) -> list[dict[str, Any]]:
    state = await _read_project_state(project_id)
    return _workflow_runtime_records_from_state(state, template_id=template_id, instance_id=instance_id)


async def _upsert_workflow_runtime_step(
    *,
    project_id: str,
    template: dict[str, Any],
    instance_id: str,
    step_id: str,
    node_type: str,
    title: str,
    fields: dict[str, Any],
    status: str,
    output: Any = None,
    outputs: list[dict[str, Any]] | None = None,
    resolved_inputs: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    node_id: str = "",
    surface: str = "",
    stale: bool | None = None,
    increment_run: bool = False,
    error: str = "",
) -> dict[str, Any]:
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instance = _workflow_runtime_instance(runtime, instance_id)
    now = _utc_now_iso()
    instance.update({
        "instance_id": instance_id,
        "template_id": str(template.get("id") or ""),
        "template_name": template.get("name") or "",
        "updated_at": now,
    })
    steps = instance.setdefault("steps", {})
    existing = steps.get(step_id) if isinstance(steps.get(step_id), dict) else {}
    workflow = fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {}
    selected_surface = surface or str(workflow.get("surface") or fields.get("surface") or existing.get("surface") or "workflow_runtime")
    selected_visibility = str(workflow.get("visibility") or existing.get("visibility") or "")
    cleaned_output = _workflow_runtime_clean_output_value(output) if output is not None else None
    next_output = cleaned_output if output is not None else existing.get("output")
    if outputs is not None:
        next_outputs = _workflow_runtime_clean_outputs(outputs)
    elif output is not None:
        next_outputs = _workflow_runtime_outputs_from_value(output)
    else:
        next_outputs = _workflow_runtime_clean_outputs(existing.get("outputs"))
    if not isinstance(next_outputs, list):
        next_outputs = _workflow_runtime_outputs_from_value(next_output)
    next_resolved_inputs = resolved_inputs if resolved_inputs is not None else existing.get("resolved_inputs")
    if not isinstance(next_resolved_inputs, list):
        next_resolved_inputs = _workflow_runtime_resolved_inputs(fields)
    next_artifacts = artifacts if artifacts is not None else existing.get("artifacts")
    if not isinstance(next_artifacts, list):
        next_artifacts = []
    run_count = int(existing.get("run_count") or 0)
    if increment_run:
        run_count += 1
    selected_stale = bool(existing.get("stale")) if stale is None else bool(stale)
    if status in {"running", "completed"}:
        selected_stale = False
    record = {
        **existing,
        "id": _runtime_step_record_id(instance_id, step_id),
        "project_id": project_id,
        "template_id": str(template.get("id") or ""),
        "instance_id": instance_id,
        "step_id": step_id,
        "type": node_type,
        "title": title,
        "status": status,
        "surface": selected_surface,
        "visibility": selected_visibility,
        "input": fields,
        "workflow": workflow,
        "resolved_inputs": next_resolved_inputs,
        "output": next_output,
        "outputs": next_outputs,
        "artifacts": next_artifacts,
        "node_id": node_id or existing.get("node_id") or "",
        "run_count": run_count,
        "stale": selected_stale,
        "error": error,
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    if status in {"running", "completed"}:
        record.pop("invalidated_by", None)
        record.pop("invalidated_at", None)
    if status == "completed" and run_count > 1:
        for downstream_step_id in _workflow_downstream_step_ids(template, step_id):
            downstream = steps.get(downstream_step_id)
            if not isinstance(downstream, dict):
                continue
            if str(downstream.get("status") or "") != "completed":
                continue
            downstream["stale"] = True
            downstream["invalidated_by"] = step_id
            downstream["updated_at"] = now
    if status == "running":
        record["last_started_at"] = now
        record.pop("last_error", None)
    elif status == "completed":
        record["last_completed_at"] = now
    elif status == "failed":
        record["last_failed_at"] = now
        record["last_error"] = error or "步骤运行失败"
    if not _workflow_runtime_run_all_active(instance):
        if status == "running":
            instance["status"] = "running"
        elif status == "failed":
            instance["status"] = "failed"
        elif status == "completed":
            instance.pop("status", None)
    steps[step_id] = {key: value for key, value in record.items() if value not in (None, "", [], {})}
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    state_for_payload = {**state, _WORKFLOW_RUNTIME_STATE_KEY: runtime}
    runtime_payload = _workflow_runtime_public_payload(
        state_for_payload,
        template_id=str(template.get("id") or ""),
        instance_id=instance_id,
    )
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(template.get("id") or ""),
        instance_id=instance_id,
        step_id=step_id,
        status=status,
        runtime=runtime_payload,
    )
    return _runtime_record_from_state(steps[step_id])


def _workflow_runtime_template_step_lookup(template_id: str) -> dict[str, dict[str, Any]]:
    wanted = str(template_id or "").strip()
    if not wanted:
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    try:
        for template in canvas_workflow_templates.list_template_summaries():
            if str(template.get("id") or "").strip() != wanted:
                continue
            for step in template.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                for key in (step.get("id"), step.get("template_step_id"), step.get("source_node_id")):
                    step_key = str(key or "").strip()
                    if step_key and step_key not in lookup:
                        lookup[step_key] = step
            break
    except canvas_workflow_templates.WorkflowTemplateError:
        pass
    try:
        template = canvas_workflow_templates.get_template(wanted)
    except canvas_workflow_templates.WorkflowTemplateError:
        return lookup
    for step in template.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for key in (step.get("id"), step.get("template_step_id"), step.get("source_node_id")):
            step_key = str(key or "").strip()
            if step_key and step_key not in lookup:
                lookup[step_key] = step
    return lookup


def _workflow_runtime_merge_template_metadata(
    workflow: dict[str, Any],
    template_step: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(template_step, dict) or not template_step:
        return workflow
    merged = dict(workflow)
    for key in _WORKFLOW_STEP_METADATA_KEYS:
        value = template_step.get(key)
        if value in (None, "", [], {}):
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = deepcopy(value)
    for key in ("repeat_group_label",):
        value = template_step.get(key)
        if value not in (None, "", [], {}) and _workflow_runtime_title_looks_machine(merged.get(key)):
            merged[key] = deepcopy(value)
    template_scope = template_step.get("instance_scope")
    if isinstance(template_scope, dict) and template_scope:
        existing_scope = merged.get("instance_scope") if isinstance(merged.get("instance_scope"), dict) else {}
        merged["instance_scope"] = {**deepcopy(template_scope), **deepcopy(existing_scope)}
    return merged


def _workflow_runtime_template_step_for_record(
    lookup: dict[str, dict[str, Any]],
    step_id: str,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    if not workflow and isinstance(fields.get("workflow"), dict):
        workflow = fields["workflow"]
    for key in (
        step_id,
        record.get("step_id"),
        workflow.get("step_id"),
        workflow.get("template_step_id"),
        workflow.get("source_node_id"),
    ):
        step_key = str(key or "").strip()
        if step_key and lookup.get(step_key):
            return lookup[step_key]
    return None


def _workflow_runtime_template_for_state(
    state: dict[str, Any],
    *,
    template_id: str,
    instance_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    selected_template_id = str(template_id or "").strip()
    selected_instance_id = str(instance_id or "").strip()
    inputs = workflow_input_values_public_payload(
        state,
        workflow_id=selected_template_id,
        instance_id=selected_instance_id,
    )
    if not selected_template_id:
        return None, inputs

    def load_with_inputs(input_values: dict[str, Any]) -> dict[str, Any]:
        active = state.get(_ACTIVE_WORKFLOW_STATE_KEY) if isinstance(state, dict) else None
        if isinstance(active, dict):
            workflow = active.get("workflow") if isinstance(active.get("workflow"), dict) else {}
            active_workflow_id = str(workflow.get("id") or active.get("workflow_id") or "").strip()
            if workflow and active_workflow_id == selected_template_id:
                return canvas_workflow_templates.normalize_inline_workflow(
                    _workflow_with_dependency_order(workflow),
                    input_values=input_values,
                )
        return canvas_workflow_templates.get_template(
            selected_template_id,
            input_values=input_values,
        )

    try:
        template = load_with_inputs(inputs)
    except (ValueError, json.JSONDecodeError, canvas_workflow_templates.WorkflowTemplateError):
        return None, inputs

    effective_inputs = _workflow_effective_inputs(template, inputs)
    if effective_inputs != inputs:
        try:
            template = load_with_inputs(effective_inputs)
        except (ValueError, json.JSONDecodeError, canvas_workflow_templates.WorkflowTemplateError):
            pass
    runtime_context = _workflow_runtime_context_from_nodes(
        _workflow_runtime_records_from_state(
            state,
            template_id=selected_template_id,
            instance_id=selected_instance_id,
        ),
        template_id=selected_template_id,
        instance_id=selected_instance_id,
    )
    if runtime_context:
        try:
            template = load_with_inputs(_dimension_input_values(effective_inputs, runtime_context))
        except (ValueError, json.JSONDecodeError, canvas_workflow_templates.WorkflowTemplateError):
            pass
    return template, effective_inputs


def _workflow_runtime_template_step_public_payload(
    step: dict[str, Any],
    *,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    step_id = str(step.get("id") or "").strip()
    if not step_id:
        return {}
    is_virtual = _workflow_input_step_spec(step, inputs) or _workflow_step_condition_skipped(step, inputs) or bool(step.get("runtime_hidden"))
    status = "completed" if is_virtual else "idle"
    canvas_output = _workflow_step_surface(step) != "workflow_runtime"
    payload: dict[str, Any] = {
        "id": step_id,
        "title": step.get("title") or step_id,
        "type": step.get("node_type") or step.get("type") or "text",
        "status": status,
        "error": "",
        "updated_at": "",
        "node_id": "",
        "surface": step.get("surface") or "",
        "visibility": step.get("visibility") or "",
        "canvas_output": canvas_output,
        "runtime_only": not canvas_output,
        "stale": False,
        "run_count": 0,
        "resolved_inputs": [],
        "output": None,
        "outputs": [],
        "artifacts": [],
        "resolved_input_count": 0,
        "output_count": 0,
        "output_preview": "",
        "artifact_count": 0,
        "artifact_node_ids": [],
    }
    for key in (
        "logical_step_id",
        "template_step_id",
        "repeat_group_id",
        "repeat_group_label",
        "repeat_group_index",
        "phase",
        "group",
        "kind",
        "role",
        "purpose",
        "acceptance",
        "primary_skill",
        "prompt_ref",
        "depends_on",
    ):
        value = step.get(key)
        if value not in (None, "", [], {}):
            payload[key] = deepcopy(value)
    for key in ("ui", "authoring", "instance_scope", "collection", "expansion"):
        value = step.get(key)
        if isinstance(value, dict) and value:
            payload[key] = deepcopy(value)
    if is_virtual:
        payload["virtual"] = True
    return payload


def _workflow_runtime_title_looks_machine(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    tail = text.split("·")[-1].strip()
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9 _/-]*", tail))


def _workflow_runtime_display_title(raw_title: Any, template_title: Any, fallback: str) -> str:
    raw = str(raw_title or "").strip()
    template = str(template_title or "").strip()
    if not template:
        return raw or fallback
    if not raw or _workflow_runtime_title_looks_machine(raw):
        if "·" in raw:
            prefix = raw.rsplit("·", 1)[0].strip()
            if prefix:
                return f"{prefix} · {template}"
        return template
    return raw


def _workflow_runtime_step_dependency_ids(step: dict[str, Any]) -> list[str]:
    deps = step.get("depends_on") if isinstance(step, dict) else []
    if not isinstance(deps, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        text = str(dep or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _workflow_runtime_public_step_completed(step: dict[str, Any] | None) -> bool:
    if not isinstance(step, dict):
        return False
    return str(step.get("status") or "").strip() == "completed" and not bool(step.get("stale"))


def _workflow_step_repeat_group_id(step: dict[str, Any] | None) -> str:
    if not isinstance(step, dict):
        return ""
    workflow = step.get("workflow") if isinstance(step.get("workflow"), dict) else {}
    return str(step.get("repeat_group_id") or workflow.get("repeat_group_id") or "").strip()


def _workflow_runtime_public_dependency_completed(dep_id: str, by_id: dict[str, dict[str, Any]]) -> bool:
    dep = str(dep_id or "").strip()
    if not dep:
        return True
    if _workflow_runtime_public_step_completed(by_id.get(dep)):
        return True
    group_steps = [
        step
        for step in by_id.values()
        if isinstance(step, dict) and _workflow_step_repeat_group_id(step) == dep
    ]
    if not group_steps:
        return False
    return all(_workflow_runtime_public_step_completed(step) for step in group_steps)


def _workflow_runtime_run_all_active(instance: dict[str, Any]) -> bool:
    return bool(instance.get("run_all_active"))


def _workflow_runtime_reset_interrupted_running_steps(instance: dict[str, Any], *, now: str) -> bool:
    steps = instance.get("steps") if isinstance(instance.get("steps"), dict) else {}
    changed = False
    for record in steps.values():
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "").strip() != "running":
            continue
        record["status"] = "idle"
        record["interrupted_at"] = now
        record["updated_at"] = now
        record.pop("last_started_at", None)
        changed = True
    return changed


def _workflow_runtime_settle_terminal_running_steps(instance: dict[str, Any], *, now: str) -> bool:
    if _workflow_runtime_run_all_active(instance):
        return False
    status = str(instance.get("status") or "").strip()
    if status not in {"failed", "completed", "paused"}:
        return False
    changed = _workflow_runtime_reset_interrupted_running_steps(instance, now=now)
    if changed:
        instance["updated_at"] = now
    return changed


async def _workflow_runtime_settle_terminal_running_steps_for_run(
    project_id: str,
    instance_id: str,
    *,
    template_id: str = "",
) -> dict[str, Any] | None:
    target_id = str(instance_id or "").strip()
    if not project_id or not target_id:
        return None
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    instance = instances.get(target_id)
    if not isinstance(instance, dict):
        return None
    now = _utc_now_iso()
    if not _workflow_runtime_settle_terminal_running_steps(instance, now=now):
        return None
    runtime["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    state_for_payload = {**state, _WORKFLOW_RUNTIME_STATE_KEY: runtime}
    runtime_payload = _workflow_runtime_public_payload(
        state_for_payload,
        template_id=str(instance.get("template_id") or template_id or ""),
        instance_id=target_id,
    )
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(instance.get("template_id") or template_id or ""),
        instance_id=target_id,
        status=str(instance.get("status") or "idle"),
        runtime=runtime_payload,
    )
    return runtime_payload


def _workflow_runtime_settle_inactive_pause(instance: dict[str, Any], *, now: str) -> bool:
    if _workflow_runtime_run_all_active(instance):
        return False
    status = str(instance.get("status") or "").strip()
    pause_requested = bool(instance.get("pause_requested"))
    if not pause_requested and status != "pause_requested":
        return False
    changed = False
    if instance.get("pause_requested") is not False:
        instance["pause_requested"] = False
        changed = True
    if status != "paused":
        instance["status"] = "paused"
        changed = True
    if not instance.get("paused_at"):
        instance["paused_at"] = now
        changed = True
    changed = _workflow_runtime_reset_interrupted_running_steps(instance, now=now) or changed
    if changed:
        instance["updated_at"] = now
    return changed


def _workflow_runtime_payload_with_graph_state(payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    by_id = {
        str(step.get("id") or "").strip(): step
        for step in steps
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    }
    completed = 0
    running = 0
    failed = 0
    waiting = 0
    ready = 0
    current_step_id = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        status = str(step.get("status") or "idle").strip() or "idle"
        is_completed = status == "completed" and not bool(step.get("stale"))
        is_running = status == "running"
        is_failed = status == "failed"
        if is_completed:
            completed += 1
        elif is_running:
            running += 1
            current_step_id = current_step_id or step_id
        elif is_failed:
            failed += 1
            current_step_id = current_step_id or step_id

        waiting_on = [
            dep
            for dep in _workflow_runtime_step_dependency_ids(step)
            if not _workflow_runtime_public_dependency_completed(dep, by_id)
        ]
        can_run = not is_completed and not is_running and not waiting_on
        step["waiting_on"] = waiting_on
        step["ready"] = bool(can_run and not is_failed)
        if waiting_on and not is_completed and not is_running:
            waiting += 1
            step["execution_state"] = "blocked"
        elif can_run and not is_failed:
            ready += 1
            step["execution_state"] = "ready"
            current_step_id = current_step_id or step_id
        elif is_completed:
            step["execution_state"] = "completed"
        elif is_running:
            step["execution_state"] = "running"
        elif is_failed:
            step["execution_state"] = "failed"
        else:
            step["execution_state"] = "idle"

    total = len([step for step in steps if isinstance(step, dict)])
    pending = max(0, total - completed - running - failed)
    if running:
        status = "running"
    elif failed:
        status = "failed"
    elif total > 0 and completed == total:
        status = "completed"
    elif completed > 0:
        status = "partial"
    else:
        status = "idle"
    payload["status"] = status
    payload["current_step_id"] = current_step_id
    payload["progress"] = {
        "total": total,
        "completed": completed,
        "running": running,
        "failed": failed,
        "pending": pending,
        "ready": ready,
        "waiting": waiting,
    }
    return payload


async def _workflow_runtime_clear_pause_state(project_id: str, instance_id: str) -> None:
    target_id = str(instance_id or "").strip()
    if not project_id or not target_id:
        return
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    instance = instances.get(target_id)
    if not isinstance(instance, dict):
        return
    changed = False
    was_paused = bool(instance.get("pause_requested")) or str(instance.get("status") or "").strip() in {"pause_requested", "paused"}
    active = _workflow_runtime_run_all_active(instance)
    now = _utc_now_iso()
    for key in ("pause_requested", "pause_requested_at", "pause_reason", "paused_at"):
        if key in instance:
            instance.pop(key, None)
            changed = True
    if str(instance.get("status") or "").strip() in {"pause_requested", "paused"}:
        instance.pop("status", None)
        changed = True
    if was_paused and not active:
        changed = _workflow_runtime_reset_interrupted_running_steps(instance, now=now) or changed
    if not changed:
        return
    instance["updated_at"] = now
    runtime["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(instance.get("template_id") or ""),
        instance_id=target_id,
        status="resumed",
    )


async def _workflow_runtime_pause_requested(project_id: str, instance_id: str) -> bool:
    target_id = str(instance_id or "").strip()
    if not project_id or not target_id:
        return False
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    instance = instances.get(target_id)
    return isinstance(instance, dict) and bool(instance.get("pause_requested"))


async def _workflow_runtime_mark_paused(
    *,
    project_id: str,
    template_id: str,
    instance_id: str,
) -> dict[str, Any] | None:
    target_id = str(instance_id or "").strip()
    if not project_id or not target_id:
        return None
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instance = _workflow_runtime_instance(runtime, target_id)
    now = _utc_now_iso()
    if template_id and not str(instance.get("template_id") or "").strip():
        instance["template_id"] = template_id
    instance.update({
        "instance_id": target_id,
        "pause_requested": False,
        "paused_at": now,
        "status": "paused",
        "updated_at": now,
    })
    instance.pop("run_all_active", None)
    runtime["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(instance.get("template_id") or template_id or ""),
        instance_id=target_id,
        status="paused",
    )
    state = await _read_project_state(project_id)
    return _workflow_runtime_public_payload(
        state,
        template_id=str(instance.get("template_id") or template_id or ""),
        instance_id=target_id,
    )


async def _workflow_runtime_mark_run_all_status(
    *,
    project_id: str,
    template_id: str,
    instance_id: str,
    status: str,
) -> dict[str, Any] | None:
    target_id = str(instance_id or "").strip()
    selected_status = str(status or "").strip()
    if not project_id or not target_id or not selected_status:
        return None
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instance = _workflow_runtime_instance(runtime, target_id)
    now = _utc_now_iso()
    if template_id and not str(instance.get("template_id") or "").strip():
        instance["template_id"] = template_id
    instance.update({
        "instance_id": target_id,
        "status": selected_status,
        "updated_at": now,
    })
    if selected_status == "running":
        instance["run_all_active"] = True
        instance["last_run_all_started_at"] = now
    else:
        instance.pop("run_all_active", None)
        if selected_status == "completed":
            instance["last_run_all_completed_at"] = now
        elif selected_status == "failed":
            instance["last_run_all_failed_at"] = now
    runtime["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    state_for_payload = {**state, _WORKFLOW_RUNTIME_STATE_KEY: runtime}
    runtime_payload = _workflow_runtime_public_payload(
        state_for_payload,
        template_id=str(instance.get("template_id") or template_id or ""),
        instance_id=target_id,
    )
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(instance.get("template_id") or template_id or ""),
        instance_id=target_id,
        status=selected_status,
        runtime=runtime_payload,
    )
    return runtime_payload


async def workflow_runtime_request_pause(
    project_id: str,
    instance_id: str,
    *,
    template_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    target_id = str(instance_id or "").strip()
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    if not target_id:
        return {"ok": False, "error": "instance_id is required", "error_kind": "missing_instance_id"}
    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instance = _workflow_runtime_instance(runtime, target_id)
    now = _utc_now_iso()
    selected_template_id = str(instance.get("template_id") or template_id or "").strip()
    if not _workflow_runtime_run_all_active(instance):
        if selected_template_id and not str(instance.get("template_id") or "").strip():
            instance["template_id"] = selected_template_id
        instance.update({
            "instance_id": target_id,
            "pause_requested": False,
            "pause_reason": str(reason or "").strip(),
            "status": "paused",
            "paused_at": now,
            "updated_at": now,
        })
        instance.pop("run_all_active", None)
        _workflow_runtime_reset_interrupted_running_steps(instance, now=now)
        runtime["updated_at"] = now
        await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
        state = await _read_project_state(project_id)
        runtime_payload = _workflow_runtime_public_payload(
            state,
            template_id=selected_template_id,
            instance_id=target_id,
        )
        await _emit_workflow_runtime_update(
            project_id=project_id,
            template_id=selected_template_id,
            instance_id=target_id,
            status="paused",
            runtime=runtime_payload,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "instance_id": target_id,
            "template_id": selected_template_id,
            "pause_requested": False,
            "paused": True,
            "runtime": runtime_payload,
            "active_workflow_runtimes": workflow_runtime_public_payloads(state),
        }
    instance.update({
        "instance_id": target_id,
        "template_id": selected_template_id,
        "pause_requested": True,
        "pause_requested_at": now,
        "pause_reason": str(reason or "").strip(),
        "status": "pause_requested",
        "updated_at": now,
    })
    runtime["updated_at"] = now
    await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=selected_template_id,
        instance_id=target_id,
        status="pause_requested",
    )
    state = await _read_project_state(project_id)
    runtime_payload = _workflow_runtime_public_payload(
        state,
        template_id=selected_template_id,
        instance_id=target_id,
    )
    return {
        "ok": True,
        "project_id": project_id,
        "instance_id": target_id,
        "template_id": selected_template_id,
        "pause_requested": True,
        "runtime": runtime_payload,
        "active_workflow_runtimes": workflow_runtime_public_payloads(state),
    }


def _collapse_workflow_runtime_phases(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose one logical V2 step while retaining private prompt phases internally."""
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, int, str]] = []
    for step in steps:
        logical_id = str(step.get("logical_step_id") or step.get("template_step_id") or step.get("id") or "").strip()
        group_id = str(step.get("repeat_group_id") or "").strip()
        try:
            group_index = int(step.get("repeat_group_index") or 0)
        except (TypeError, ValueError):
            group_index = 0
        key = (group_id, group_index, logical_id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(step)

    id_to_public: dict[str, str] = {}
    representatives: dict[tuple[str, int, str], dict[str, Any]] = {}
    for key in order:
        phases = groups[key]
        representative = next(
            (step for step in reversed(phases) if step.get("canvas_output") and not step.get("runtime_only")),
            phases[-1],
        )
        representatives[key] = representative
        public_id = str(representative.get("id") or key[2]).strip()
        for phase in phases:
            phase_id = str(phase.get("id") or "").strip()
            if phase_id:
                id_to_public[phase_id] = public_id

    result: list[dict[str, Any]] = []
    status_priority = {"failed": 5, "running": 4, "ready": 3, "waiting": 2, "idle": 1, "completed": 0}
    for key in order:
        phases = groups[key]
        representative = deepcopy(representatives[key])
        statuses = [str(step.get("status") or "idle") for step in phases]
        failed_phase = next((step for step in phases if step.get("status") == "failed"), None)
        if failed_phase:
            representative["status"] = "failed"
            representative["error"] = failed_phase.get("error") or representative.get("error") or ""
        elif any(status == "running" for status in statuses):
            representative["status"] = "running"
        elif all(status == "completed" for status in statuses):
            representative["status"] = "completed"
        elif representative.get("status") != "completed":
            representative["status"] = max(statuses, key=lambda item: status_priority.get(item, 1))
        representative["logical_step_id"] = key[2]
        representative["run_count"] = sum(int(step.get("run_count") or 0) for step in phases)
        dependencies: list[str] = []
        for phase in phases:
            for dependency in phase.get("depends_on") or []:
                resolved = id_to_public.get(str(dependency), str(dependency))
                if resolved and resolved != representative.get("id") and resolved not in dependencies:
                    dependencies.append(resolved)
        representative["depends_on"] = dependencies
        result.append(representative)
    return result


def _workflow_runtime_public_payload(
    state: dict[str, Any],
    *,
    template_id: str,
    instance_id: str = "",
) -> dict[str, Any]:
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    selected_id = str(instance_id or "").strip()
    selected: dict[str, Any] | None = None
    if selected_id:
        candidate = instances.get(selected_id)
        if isinstance(candidate, dict):
            if not template_id or str(candidate.get("template_id") or "").strip() == template_id:
                selected = candidate
        if selected is None:
            return {"instance_id": selected_id, "template_id": template_id, "steps": []}
    if selected is None:
        for candidate_id, instance in reversed(list(instances.items())):
            if not isinstance(instance, dict):
                continue
            if template_id and str(instance.get("template_id") or "").strip() != template_id:
                continue
            selected_id = str(candidate_id)
            selected = instance
            break
    if not selected:
        return {"instance_id": selected_id, "template_id": template_id, "steps": []}
    if bool(selected.get("pause_requested")) and not _workflow_runtime_run_all_active(selected):
        selected = deepcopy(selected)
        _workflow_runtime_settle_inactive_pause(selected, now=_utc_now_iso())
    steps = selected.get("steps") if isinstance(selected.get("steps"), dict) else {}
    selected_template_id = str(selected.get("template_id") or template_id or "").strip()
    template_step_lookup = _workflow_runtime_template_step_lookup(selected_template_id)
    template, template_inputs = _workflow_runtime_template_for_state(
        state,
        template_id=selected_template_id,
        instance_id=selected_id,
    )
    template_steps = [
        step
        for step in (template.get("steps") if isinstance(template, dict) else []) or []
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    ]
    template_steps_by_id = {
        str(step.get("id") or "").strip(): step
        for step in template_steps
        if str(step.get("id") or "").strip()
    }
    runtime_steps_by_id: dict[str, dict[str, Any]] = {}
    for step_id, record in steps.items():
        if not isinstance(record, dict):
            continue
        public_step_id = str(step_id or "").strip()
        if not public_step_id:
            continue
        template_step = template_steps_by_id.get(public_step_id) or template_step_lookup.get(public_step_id) or _workflow_runtime_template_step_for_record(
            template_step_lookup,
            public_step_id,
            record,
        )
        if template_steps and not template_step:
            continue
        payload = workflow_runtime_step_public_payload(
            public_step_id,
            record,
            template_step=template_step,
        )
        if public_step_id not in runtime_steps_by_id:
            runtime_steps_by_id[public_step_id] = payload
    public_steps: list[dict[str, Any]] = []
    used_step_ids: set[str] = set()
    for template_step in template_steps:
        step_id = str(template_step.get("id") or "").strip()
        if not step_id:
            continue
        runtime_step = runtime_steps_by_id.get(step_id)
        if runtime_step:
            public_steps.append(runtime_step)
        else:
            placeholder = _workflow_runtime_template_step_public_payload(
                template_step,
                inputs=template_inputs,
            )
            if placeholder:
                public_steps.append(placeholder)
        used_step_ids.add(step_id)
    for step_id, runtime_step in runtime_steps_by_id.items():
        if step_id not in used_step_ids:
            public_steps.append(runtime_step)
    public_steps = _collapse_workflow_runtime_phases(public_steps)
    payload = _workflow_runtime_payload_with_graph_state({
        "instance_id": selected_id or selected.get("instance_id") or "",
        "template_id": selected_template_id,
        "template_name": selected.get("template_name") or "",
        "input_values": workflow_input_values_public_payload(
            state,
            workflow_id=selected_template_id,
            instance_id=selected_id,
        ),
        "pause_requested": bool(selected.get("pause_requested")),
        "pause_requested_at": selected.get("pause_requested_at") or "",
        "pause_reason": selected.get("pause_reason") or "",
        "paused_at": selected.get("paused_at") or "",
        "updated_at": selected.get("updated_at") or "",
        "steps": public_steps,
    })
    selected_status = str(selected.get("status") or "").strip()
    if bool(selected.get("pause_requested")):
        payload["status"] = "pause_requested"
    elif selected_status == "paused":
        payload["status"] = "paused"
    elif selected_status == "running":
        payload["status"] = "running"
    elif selected_status == "completed":
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        total = int(progress.get("total") or 0)
        if total == 0 or int(progress.get("completed") or 0) >= total:
            payload["status"] = "completed"
    elif selected_status == "failed":
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        if int(progress.get("failed") or 0) > 0:
            payload["status"] = selected_status
    elif selected_status:
        payload["status"] = selected_status
    return payload


def workflow_runtime_public_payload(
    state: dict[str, Any],
    *,
    template_id: str,
    instance_id: str = "",
) -> dict[str, Any]:
    return _workflow_runtime_public_payload(state, template_id=template_id, instance_id=instance_id)


def workflow_runtime_public_payloads(
    state: dict[str, Any],
    *,
    template_id: str = "",
) -> list[dict[str, Any]]:
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    payloads: list[dict[str, Any]] = []
    for candidate_id, instance in reversed(list(instances.items())):
        if not isinstance(instance, dict):
            continue
        selected_template_id = str(instance.get("template_id") or template_id or "").strip()
        if template_id and selected_template_id != template_id:
            continue
        payload = _workflow_runtime_public_payload(
            state,
            template_id=selected_template_id,
            instance_id=str(candidate_id or ""),
        )
        if payload.get("steps"):
            payloads.append(payload)
    return payloads


def _active_workflow_public_summary(project_id: str, state: dict[str, Any] | None) -> dict[str, Any] | None:
    active = state.get(_ACTIVE_WORKFLOW_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(active, dict):
        return None
    kind = str(active.get("kind") or "").strip().lower()
    if kind == "template":
        template_id = str(active.get("template_id") or "").strip()
        if not template_id:
            return None
        return {
            "kind": "template",
            "template_id": template_id,
            "workflow_id": template_id,
            "updated_at": active.get("updated_at") or "",
        }
    if kind == "artifact":
        artifact_ref = str(active.get("artifact_ref") or "").strip()
        if not artifact_ref:
            return None
        payload: dict[str, Any] = {
            "kind": "artifact",
            "artifact_ref": artifact_ref,
            "workflow_id": "",
            "name": active.get("name") or "",
            "description": active.get("description") or "",
            "updated_at": active.get("updated_at") or "",
        }
        try:
            artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
            workflow = artifact.get("workflow") if isinstance(artifact.get("workflow"), dict) else {}
            preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
            payload["workflow_id"] = str(workflow.get("id") or preview.get("id") or "").strip()
            payload["name"] = payload["name"] or str(preview.get("name") or workflow.get("name") or "")
            payload["description"] = payload["description"] or str(preview.get("description") or workflow.get("description") or "")
            payload["step_count"] = len(workflow.get("steps") or []) if isinstance(workflow.get("steps"), list) else preview.get("step_count") or 0
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            payload["error"] = str(exc)
        return payload
    if kind == "imported":
        workflow = active.get("workflow") if isinstance(active.get("workflow"), dict) else {}
        if not workflow:
            return None
        steps = workflow.get("steps")
        return {
            "kind": "imported",
            "workflow_id": str(workflow.get("id") or active.get("name") or "").strip(),
            "name": active.get("name") or workflow.get("name") or workflow.get("title") or "",
            "description": active.get("description") or workflow.get("description") or "",
            "step_count": len(steps) if isinstance(steps, list) else 0,
            "updated_at": active.get("updated_at") or "",
        }
    return None


@register(
    "workflow.runtime_status",
    description="读取当前项目的工作流选择、运行态和已保存输入值。",
    tags=["workflow", "read"],
    is_read_only=True,
    is_concurrency_safe=True,
    search_hint=(
        "workflow runtime status active workflow saved inputs instance step state progress "
        "工作流 运行态 状态 输入值 胶囊 流程 进度"
    ),
    usage_hints=[
        "需要确认当前 active workflow、并行运行实例、已填写 inputs 或下一步状态时使用。",
        "template_id 可限定某个模板或 spec id；instance_id 可读取指定并行运行实例。",
        "返回 workflow_input_values 可直接作为 workflow.run_step/run_next/run_all 的 inputs 基础。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "instance_id": {"type": "string"},
        },
    },
)
async def workflow_runtime_status(
    project_id: str,
    template_id: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    state = await _read_project_state(project_id)
    active_workflow = _active_workflow_public_summary(project_id, state)
    resolved_template_id = str(
        template_id
        or (active_workflow or {}).get("workflow_id")
        or (active_workflow or {}).get("template_id")
        or ""
    ).strip()
    runtime_payload = _workflow_runtime_public_payload(
        state,
        template_id=resolved_template_id,
        instance_id=instance_id,
    )
    if not resolved_template_id:
        resolved_template_id = str(runtime_payload.get("template_id") or "").strip()
    selected_instance_id = str(instance_id or runtime_payload.get("instance_id") or "").strip()
    workflow_input_values = workflow_input_values_public_payload(
        state,
        workflow_id=resolved_template_id,
        instance_id=selected_instance_id,
    )
    progress = runtime_payload.get("progress") if isinstance(runtime_payload.get("progress"), dict) else {}
    if progress.get("running"):
        next_action = "等待当前步骤结束，或稍后再查 workflow.runtime_status。"
    elif progress.get("ready") or progress.get("pending"):
        next_action = "需要继续执行时调用 workflow.run_next 或 workflow.run_all；要只跑一个步骤则调用 workflow.run_step。"
    elif runtime_payload.get("steps"):
        next_action = "当前运行实例没有待执行步骤；需要新一轮时不传 instance_id 或新增运行胶囊。"
    else:
        next_action = "先选择或物化 workflow，再传 inputs 调用 workflow.run_next、workflow.run_step 或 workflow.run_all。"
    return {
        "ok": True,
        "project_id": project_id,
        "template_id": resolved_template_id,
        "instance_id": selected_instance_id,
        "active_workflow": active_workflow,
        "runtime": runtime_payload,
        "runtimes": workflow_runtime_public_payloads(state, template_id=resolved_template_id),
        "workflow_input_values": workflow_input_values,
        "stored_inputs": workflow_input_values,
        "next_action": next_action,
    }


async def workflow_runtime_delete_instance(project_id: str, instance_id: str) -> dict[str, Any]:
    target_id = str(instance_id or "").strip()
    if not project_id:
        return {"ok": False, "error": "project_id is required"}
    if not target_id:
        return {"ok": False, "error": "instance_id is required"}

    state = await _read_project_state(project_id)
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    deleted = target_id in instances
    if deleted:
        instances.pop(target_id, None)
        runtime["instances"] = instances
        runtime["updated_at"] = _utc_now_iso()
        await _write_project_state_patch(project_id, {_WORKFLOW_RUNTIME_STATE_KEY: runtime})
        state = await _read_project_state(project_id)

    return {
        "ok": True,
        "project_id": project_id,
        "instance_id": target_id,
        "deleted": deleted,
        "active_workflow_runtimes": workflow_runtime_public_payloads(state),
    }


def workflow_runtime_step_public_payload(
    step_id: str,
    record: dict[str, Any],
    *,
    template_step: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    if not workflow and isinstance(fields.get("workflow"), dict):
        workflow = fields["workflow"]
    workflow = _workflow_runtime_merge_template_metadata(workflow, template_step)
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), list) else []
    output = _workflow_runtime_clean_output_value(record.get("output"), drop_internal_keys=True)
    outputs = _workflow_runtime_clean_outputs(record.get("outputs"), drop_internal_keys=True) if isinstance(record.get("outputs"), list) else []
    if not outputs:
        outputs = _workflow_runtime_outputs_from_value(output)
    output_count = len(outputs) if outputs else (1 if output not in (None, "", [], {}) else 0)
    surface = record.get("surface") or workflow.get("surface") or ""
    visibility = record.get("visibility") or workflow.get("visibility") or ""
    canvas_output = _workflow_runtime_record_canvas_output(record)
    payload: dict[str, Any] = {
        "id": step_id,
        "title": _workflow_runtime_display_title(
            record.get("title"),
            template_step.get("title") if isinstance(template_step, dict) else "",
            step_id,
        ),
        "type": record.get("type") or "text",
        "status": record.get("status") or "idle",
        "error": record.get("error") or "",
        "updated_at": record.get("updated_at") or "",
        "node_id": record.get("node_id") or "",
        "surface": surface,
        "visibility": visibility,
        "canvas_output": canvas_output,
        "runtime_only": not canvas_output,
        "stale": bool(record.get("stale")),
        "run_count": record.get("run_count") or 0,
        "resolved_inputs": record.get("resolved_inputs") if isinstance(record.get("resolved_inputs"), list) else [],
        "output": output,
        "outputs": outputs,
        "artifacts": artifacts,
        "resolved_input_count": len(record.get("resolved_inputs") or []) if isinstance(record.get("resolved_inputs"), list) else 0,
        "output_count": output_count,
        "output_preview": workflow_runtime_output_preview(record, workflow_override=workflow),
        "artifact_count": len(artifacts),
        "artifact_node_ids": [
            str(item.get("node_id"))
            for item in artifacts
            if isinstance(item, dict) and item.get("node_id")
        ],
    }
    for key in (
        "logical_step_id",
        "template_step_id",
        "repeat_group_id",
        "repeat_group_label",
        "repeat_group_index",
        "phase",
        "group",
        "kind",
        "role",
        "purpose",
        "acceptance",
        "primary_skill",
        "prompt_ref",
        "depends_on",
    ):
        value = workflow.get(key)
        if value not in (None, "", [], {}):
            payload[key] = deepcopy(value)
    for key in ("ui", "output", "authoring", "instance_scope", "collection", "expansion"):
        value = workflow.get(key)
        if isinstance(value, dict) and value:
            if key == "output":
                value = {
                    output_key: output_value
                    for output_key, output_value in value.items()
                    if output_key not in {"canvas", "show_on_canvas"}
                }
            if value:
                payload[key] = deepcopy(value)
    return payload


def _unique_nonempty_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _public_ref(node: dict[str, Any]) -> str:
    display_id = node.get("display_id")
    if display_id is not None:
        return f"node:{display_id}"
    return f"node:{node.get('id')}"


def _workflow_step_writes_media_prompt(template: dict[str, Any], step: dict[str, Any]) -> bool:
    step_id = str(step.get("id") or "").strip()
    if not step_id.endswith("__prompt"):
        return False
    target_id = step_id.removesuffix("__prompt")
    return any(
        str(candidate.get("id") or "").strip() == target_id
        and str(candidate.get("node_type") or "").strip() in {"image", "video", "audio"}
        for candidate in (template.get("steps") or [])
        if isinstance(candidate, dict)
    )


def _workflow_is_canvas_dependency_record(node: dict[str, Any] | None) -> bool:
    if not isinstance(node, dict):
        return False
    node_id = str(node.get("id") or "").strip()
    return _workflow_record_surface(node) != "workflow_runtime" and not node_id.startswith("workflow-runtime:")


def _reference_for_dep(node: dict[str, Any], role: str) -> dict[str, str]:
    return {"ref": _public_ref(node), "role": role or "context"}


def _dedupe_workflow_references(refs: list[dict[str, str]]) -> list[dict[str, str]]:
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


def _merge_workflow_dependency_refs(
    fields: dict[str, Any],
    refs: list[dict[str, str]],
    *,
    replace_managed: bool = False,
) -> dict[str, Any]:
    dep_refs = _dedupe_workflow_references(refs)
    existing_refs = _dedupe_workflow_references(
        fields.get("references") if isinstance(fields.get("references"), list) else []
    )
    workflow = dict(fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {})
    if replace_managed:
        previous_managed = _dedupe_workflow_references(
            workflow.get("managed_references")
            if isinstance(workflow.get("managed_references"), list)
            else []
        )
        previous_keys = {
            (str(item.get("ref") or "").strip(), str(item.get("role") or "context").strip() or "context")
            for item in previous_managed
        }
        if previous_managed:
            existing_refs = [
                item
                for item in existing_refs
                if (item["ref"], item["role"]) not in previous_keys
            ]
        elif workflow.get("template_id") or workflow.get("instance_id"):
            # Existing workflow nodes predate managed_references. Their stored
            # references were executor-generated, so migrate them as one set.
            existing_refs = []
        workflow["managed_references"] = dep_refs
        fields["workflow"] = workflow
        if workflow.get("template_id") or workflow.get("instance_id"):
            # Resolved media references are derived from managed references on
            # every run. Remove legacy persisted copies so deleted dependencies
            # cannot remain as sticky media inputs.
            fields.pop("reference_images", None)
    merged_refs = _dedupe_workflow_references([*existing_refs, *dep_refs])
    if merged_refs:
        fields["references"] = merged_refs
        fields["depends_on"] = _unique_nonempty_strings(
            [item["ref"] for item in merged_refs if item.get("ref")]
        )
    elif replace_managed:
        # Keep explicit empty dependency keys so edge synchronization removes
        # previously projected workflow edges.
        fields["references"] = []
        fields["depends_on"] = []
    else:
        fields.pop("references", None)
        fields.pop("depends_on", None)
    return fields


def _workflow_input_reference_refs(
    input_values: dict[str, Any] | None,
    step: dict[str, Any],
    fields: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    values = input_values if isinstance(input_values, dict) else {}
    field_values = fields if isinstance(fields, dict) else {}
    raw = step.get("input_references") or field_values.get("input_references")
    if raw in (None, "", [], {}):
        return []
    items = raw if isinstance(raw, list) else [raw]
    refs: list[dict[str, str]] = []

    def add_ref(value: Any, role: str) -> None:
        if isinstance(value, list):
            for item in value:
                add_ref(item, role)
            return
        if isinstance(value, dict):
            ref_value = value.get("ref") or value.get("reference") or value.get("node") or value.get("value")
            if ref_value not in (None, "", [], {}):
                add_ref(ref_value, role)
            return
        ref = str(value or "").strip()
        if ref:
            refs.append({"ref": ref, "role": role or "context"})

    for item in items:
        if isinstance(item, dict):
            input_id = str(item.get("input") or item.get("input_id") or item.get("id") or item.get("key") or "").strip()
            role = str(item.get("role") or "context").strip() or "context"
            value = item.get("value")
            if value in (None, "", [], {}) and input_id:
                value = values.get(input_id)
        else:
            input_id = str(item or "").strip()
            role = "context"
            value = values.get(input_id)
        add_ref(value, role)
    return _dedupe_workflow_references(refs)


def _workflow_canvas_output_value(fields: dict[str, Any], node: dict[str, Any], node_type: str) -> dict[str, Any]:
    if node_type == "text":
        content = fields.get("content")
        return {"content": content} if content not in (None, "", [], {}) else {}
    media_url = str(fields.get("url") or fields.get("local_url") or fields.get("remote_url") or "").strip()
    media_path = str(fields.get("path") or fields.get("rel_path") or fields.get("output_path") or "").strip()
    if node_type in {"image", "video", "audio"} and (media_url or media_path):
        payload: dict[str, Any] = {
            "type": node_type,
            "status": "completed",
        }
        if media_url:
            payload["url"] = media_url
            payload["local_url"] = str(fields.get("local_url") or media_url)
        if fields.get("remote_url") not in (None, "", [], {}):
            payload["remote_url"] = fields.get("remote_url")
        if media_path:
            payload["path"] = media_path
        if fields.get("local_path") not in (None, "", [], {}):
            payload["local_path"] = fields.get("local_path")
        for key in ("width", "height", "mime_type", "size", "frame_index", "timestamp_seconds"):
            if fields.get(key) not in (None, "", [], {}):
                payload[key] = fields.get(key)
        if node_type == "image":
            image_item = {key: value for key, value in payload.items() if key not in {"type", "status"}}
            payload["images"] = [image_item]
        elif node_type == "video":
            payload["video"] = {key: value for key, value in payload.items() if key not in {"type", "status"}}
        elif node_type == "audio":
            payload["audio"] = {key: value for key, value in payload.items() if key not in {"type", "status"}}
        return payload
    prompt = fields.get("prompt") or node.get("prompt")
    if prompt not in (None, "", [], {}):
        return {"prompt": prompt}
    content = fields.get("content")
    return {"content": content} if content not in (None, "", [], {}) else {}


def _workflow_step_source_config(step: dict[str, Any]) -> tuple[str, str]:
    step_fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    source_step = str(
        step.get("source_step")
        or step_fields.get("workflow_source_step")
        or step_fields.get("source_step")
        or ""
    ).strip()
    source_path = str(
        step.get("source_path")
        or step_fields.get("workflow_source_path")
        or step_fields.get("source_path")
        or ""
    ).strip()
    return source_step, source_path


def _workflow_control_dependency_ids(step: dict[str, Any] | None) -> set[str]:
    if not isinstance(step, dict):
        return set()
    return {
        str(item or "").strip()
        for item in (step.get("_control_depends_on") or [])
        if str(item or "").strip()
    }


def _workflow_data_dependency_ids(step: dict[str, Any] | None) -> list[str]:
    if not isinstance(step, dict):
        return []
    control_deps = _workflow_control_dependency_ids(step)
    return [
        str(dep or "").strip()
        for dep in (step.get("depends_on") or [])
        if str(dep or "").strip() and str(dep or "").strip() not in control_deps
    ]


def _workflow_effective_source_step(fields: dict[str, Any], step: dict[str, Any]) -> str:
    field_source = str(
        fields.get("workflow_source_step")
        or fields.get("source_step")
        or fields.get("from_step")
        or ""
    ).strip()
    step_source, _source_path = _workflow_step_source_config(step)
    first_dep = next(iter(_workflow_data_dependency_ids(step)), "")
    if step_source and (not field_source or field_source == first_dep):
        return step_source
    return field_source or step_source or first_dep


def _workflow_effective_source_path(fields: dict[str, Any], step: dict[str, Any]) -> str:
    field_path = str(fields.get("workflow_source_path") or fields.get("source_path") or "").strip()
    _step_source, step_path = _workflow_step_source_config(step)
    if step_path and (not field_path or field_path == "output"):
        return step_path
    return field_path or step_path or "output"


def _step_position(
    step: dict[str, Any],
    *,
    index: int,
    steps: list[dict[str, Any]] | None = None,
    origin_x: float,
    origin_y: float,
    spacing_x: float,
    spacing_y: float,
) -> tuple[float, float]:
    position = step.get("position") if isinstance(step.get("position"), dict) else {}
    if position:
        return (
            origin_x + _number(position.get("x"), (index % 3) * spacing_x),
            origin_y + _number(position.get("y"), (index // 3) * spacing_y),
        )
    grouped = _workflow_repeat_group_position(step, steps or [])
    if grouped is not None:
        column, row = grouped
        return (
            origin_x + column * spacing_x,
            origin_y + row * spacing_y,
        )
    return (
        origin_x + _number(position.get("x"), (index % 3) * spacing_x),
        origin_y + _number(position.get("y"), (index // 3) * spacing_y),
    )


def _workflow_repeat_group_position(
    step: dict[str, Any],
    steps: list[dict[str, Any]],
) -> tuple[int, int] | None:
    if _workflow_step_surface(step) == "workflow_runtime":
        return None
    group_id = str(step.get("repeat_group_id") or "").strip()
    if not group_id:
        return None
    instance_scope = step.get("instance_scope") if isinstance(step.get("instance_scope"), dict) else {}
    raw_column = instance_scope.get("index") or step.get("repeat_group_index")
    try:
        column = max(0, int(raw_column) - 1)
    except (TypeError, ValueError):
        return None

    group_order: list[str] = []
    row_keys_by_group: dict[str, list[str]] = {}
    leading_top_level_rows = 0
    for item in steps:
        if not isinstance(item, dict) or _workflow_step_surface(item) == "workflow_runtime":
            continue
        item_group = str(item.get("repeat_group_id") or "").strip()
        if not item_group:
            if not group_order:
                leading_top_level_rows += 1
            continue
        if item_group not in row_keys_by_group:
            group_order.append(item_group)
            row_keys_by_group[item_group] = []
        row_key = str(item.get("template_step_id") or item.get("id") or "").strip()
        if row_key and row_key not in row_keys_by_group[item_group]:
            row_keys_by_group[item_group].append(row_key)

    row_start = leading_top_level_rows
    row_by_group: dict[str, dict[str, int]] = {}
    for item_group in group_order:
        row_keys = row_keys_by_group[item_group] or [item_group]
        row_by_group[item_group] = {
            key: row_start + offset
            for offset, key in enumerate(row_keys)
        }
        row_start += len(row_keys) + 1

    row_key = str(step.get("template_step_id") or step.get("id") or "").strip()
    row = row_by_group.get(group_id, {}).get(row_key)
    if row is None:
        return None
    return column, row


def _input_summary(inputs: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (inputs or {}).items():
        if value in (None, "", [], {}):
            continue
        result[str(key)] = value
    return result


def _workflow_effective_inputs(template: dict[str, Any] | None, inputs: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    defaults = template.get("defaults") if isinstance(template, dict) else None
    if isinstance(defaults, dict):
        result.update({
            str(key): deepcopy(value)
            for key, value in defaults.items()
            if key != "fields" and value not in (None, "", [], {})
        })
    input_defaults = template.get("input_defaults") if isinstance(template, dict) else None
    if isinstance(input_defaults, dict):
        result.update({
            str(key): deepcopy(value)
            for key, value in input_defaults.items()
            if value not in (None, "", [], {})
        })
    if isinstance(inputs, dict):
        result.update({
            str(key): deepcopy(value)
            for key, value in inputs.items()
            if value not in (None, "", [], {})
        })
    return result


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


def _required_input_error(template: dict[str, Any], inputs: dict[str, Any] | None) -> dict[str, Any] | None:
    missing = canvas_workflow_templates.missing_required_inputs(template, inputs)
    if not missing:
        return None
    input_payload = _workflow_template_input_payload(template, inputs)
    return {
        "ok": False,
        "error": "Workflow requires explicit inputs before it can be materialized",
        "error_kind": "workflow_required_inputs_missing",
        "missing_inputs": missing,
        "required_inputs": list(template.get("required_inputs") or []),
        "input_fields": input_payload.get("input_fields") or [],
        "input_questions": input_payload.get("input_questions") or [],
        "template_id": template.get("id"),
        "template_name": template.get("name"),
        "hint": "根据 input_questions 调用 interaction.request_input 补齐缺失输入，再继续运行或实例化工作流。",
    }


def _step_workflow_metadata(step: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _WORKFLOW_STEP_METADATA_KEYS:
        if key in step and step.get(key) not in (None, "", [], {}):
            result[key] = deepcopy(step.get(key))
    return result


def _workflow_protocol_payload(template: dict[str, Any]) -> dict[str, Any]:
    protocol = template.get("protocol") if isinstance(template.get("protocol"), dict) else {}
    return {
        key: value
        for key, value in {
            "schema": canvas_workflow_templates.WORKFLOW_SPEC_PROTOCOL_VERSION,
            "protocol_version": protocol.get("protocol_version") or canvas_workflow_templates.WORKFLOW_SPEC_PROTOCOL_VERSION,
            "execution_plan_version": protocol.get("execution_plan_version") or template.get("schema"),
            "plan_hash": protocol.get("plan_hash") or template.get("plan_hash"),
            "requirements": template.get("requirements") or {},
        }.items()
        if value not in (None, "", [], {})
    }


def _copy_present(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: deepcopy(source[key])
        for key in keys
        if key in source and source[key] not in (None, "", [], {})
    }


def _workflow_template_input_questions(input_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for field in input_fields:
        if not field.get("missing"):
            continue
        input_id = str(field.get("id") or "").strip()
        if not input_id:
            continue
        label = str(field.get("label") or input_id).strip()
        description = str(field.get("description") or "").strip()
        question_text = description or f"请填写{label}。"
        question: dict[str, Any] = {
            "id": input_id,
            "header": label[:80],
            "question": question_text,
        }
        options = field.get("options")
        if isinstance(options, list) and 2 <= len(options) <= 3:
            normalized_options: list[dict[str, Any]] = []
            for option in options:
                if isinstance(option, dict):
                    option_label = str(option.get("label") or option.get("value") or "").strip()
                    option_description = str(option.get("description") or "").strip()
                else:
                    option_label = str(option or "").strip()
                    option_description = ""
                if not option_label:
                    continue
                normalized = {"label": option_label}
                if option_description:
                    normalized["description"] = option_description[:240]
                normalized_options.append(normalized)
            if 2 <= len(normalized_options) <= 3:
                question["options"] = normalized_options
        questions.append(question)
        if len(questions) >= 6:
            break
    return questions


def _workflow_template_input_payload(
    template: dict[str, Any],
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_fields = canvas_workflow_templates.template_input_field_summaries(template, inputs)
    missing_inputs = [
        str(field.get("id") or "")
        for field in input_fields
        if field.get("required") and field.get("missing")
    ]
    return {
        "input_fields": input_fields[:12],
        "missing_inputs": missing_inputs,
        "input_questions": _workflow_template_input_questions(input_fields),
    }


def _workflow_template_input_definitions(template: dict[str, Any]) -> list[dict[str, Any]]:
    fields = canvas_workflow_templates.template_input_field_summaries(template, {})
    result: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        cleaned = {
            key: deepcopy(value)
            for key, value in field.items()
            if key not in {"missing", "input_questions", "question", "header"} and value not in (None, "", [], {})
        }
        if cleaned.get("id"):
            result.append(cleaned)
    return result


def _workflow_template_candidate_payload(
    summary: dict[str, Any],
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    steps = summary.get("steps") if isinstance(summary.get("steps"), list) else []
    graph = summary.get("template_graph") if isinstance(summary.get("template_graph"), dict) else {}
    graph_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    graph_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    payload = _copy_present(
        summary,
        (
            "id",
            "name",
            "description",
            "category",
            "applies_to",
            "scope",
            "source",
            "downloadable",
            "version",
            "active_version_id",
            "inputs",
            "required_inputs",
            "step_count",
            "graph_node_count",
            "graph_edge_count",
            "match_score",
        ),
    )
    payload.setdefault("step_count", len(steps))
    if graph_nodes:
        payload.setdefault("graph_node_count", len(graph_nodes))
    if graph_edges:
        payload.setdefault("graph_edge_count", len(graph_edges))
    payload.update(_workflow_template_input_payload(summary, inputs))
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _direct_workflow_template_payload(
    summary: dict[str, Any],
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _workflow_template_candidate_payload(summary, inputs)
    payload["template_id"] = payload.get("id") or summary.get("id")
    payload["recommended_tool"] = "workflow.run_all"
    payload["next_action"] = "有 input_questions 时先调用 interaction.request_input；输入齐全后直接用该 template_id 运行或实例化。"
    return payload


def _direct_workflow_template_summary_for_skill(skill_name: str) -> dict[str, Any] | None:
    name = str(skill_name or "").strip()
    if not name:
        return None
    matches: list[tuple[int, dict[str, Any]]] = []
    for summary in canvas_workflow_templates.list_template_summaries():
        template_id = str(summary.get("id") or "").strip()
        source_skill = summary.get("source_skill") if isinstance(summary.get("source_skill"), dict) else {}
        source_skill_name = str(source_skill.get("name") or "").strip()
        if template_id == name:
            matches.append((0, summary))
        elif source_skill_name == name:
            matches.append((1, summary))
    matches.sort(key=lambda item: (
        item[0],
        0 if str(item[1].get("scope") or "") == "user" else 1,
        str(item[1].get("name") or ""),
        str(item[1].get("id") or ""),
    ))
    return deepcopy(matches[0][1]) if matches else None


def _template_catalog_summary(template: dict[str, Any]) -> dict[str, Any]:
    inputs = template.get("inputs") if isinstance(template.get("inputs"), list) else []
    required_inputs = template.get("required_inputs") if isinstance(template.get("required_inputs"), list) else []
    steps = template.get("steps") if isinstance(template.get("steps"), list) else []
    graph = template.get("template_graph") if isinstance(template.get("template_graph"), dict) else {}
    graph_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    graph_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    description = str(template.get("description") or "")
    applies_to = str(template.get("applies_to") or "")
    return {
        "id": str(template.get("id") or ""),
        "name": str(template.get("name") or ""),
        "description": description[:180],
        "category": str(template.get("category") or ""),
        "applies_to": applies_to[:180],
        "scope": str(template.get("scope") or ""),
        "source": str(template.get("source") or ""),
        "downloadable": bool(template.get("downloadable")),
        "version": str(template.get("version") or ""),
        "active_version_id": str(template.get("active_version_id") or ""),
        "inputs": [str(item) for item in inputs if str(item or "").strip()],
        "required_inputs": [str(item) for item in required_inputs if str(item or "").strip()],
        "step_count": len(steps),
        "graph_node_count": len(graph_nodes),
        "graph_edge_count": len(graph_edges),
    }


def _template_catalog_tokens(template: dict[str, Any]) -> set[str]:
    text = " ".join(
        str(template.get(key) or "")
        for key in ("id", "name", "description", "category", "applies_to", "scope", "source")
    )
    for item in template.get("inputs") or []:
        text += f" {item}"
    for item in template.get("required_inputs") or []:
        text += f" {item}"
    return {
        token[:80]
        for token in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", text.lower())
    }


def _light_template_catalog(
    *,
    query: str = "",
    category: str = "",
    limit: int = 12,
) -> tuple[list[dict[str, Any]], int]:
    summaries = canvas_workflow_templates.list_template_summaries()
    selected: list[dict[str, Any]] = []
    query_tokens = {
        token[:80]
        for token in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", str(query or "").lower())
    }
    category_text = str(category or "").strip().lower()
    for template in summaries:
        if category_text and category_text not in {
            str(template.get("category") or "").lower(),
            str(template.get("scope") or "").lower(),
            str(template.get("source") or "").lower(),
        }:
            continue
        if query_tokens and not (query_tokens & _template_catalog_tokens(template)):
            continue
        selected.append(_template_catalog_summary(template))
    selected.sort(key=lambda item: (
        0 if item.get("scope") == "user" else 1,
        0 if item.get("id") == canvas_workflow_templates.DEFAULT_WORKFLOW_TEMPLATE_ID else 1,
        str(item.get("category") or ""),
        str(item.get("name") or ""),
        str(item.get("id") or ""),
    ))
    capped = max(1, min(int(limit or 12), 30))
    return selected[:capped], len(selected)


def _workflow_input_step_spec(step: dict[str, Any], inputs: dict[str, Any] | None) -> bool:
    runner = str(step.get("runner") or "").strip()
    step_id = str(step.get("id") or step.get("template_step_id") or "").strip().lower()
    if runner in _WORKFLOW_INPUT_RUNNERS:
        return True
    return step_id in {"input", "inputs", "workflow_input"} and bool(inputs)


def _virtual_workflow_step_ids(steps: list[dict[str, Any]], inputs: dict[str, Any] | None) -> set[str]:
    return {
        str(step.get("id") or "").strip()
        for step in steps
        if str(step.get("id") or "").strip()
        and (
            _workflow_input_step_spec(step, inputs)
            or _workflow_step_condition_skipped(step, inputs)
            or bool(step.get("runtime_hidden"))
        )
    }


def _workflow_node_matches(
    node: dict[str, Any],
    *,
    template_id: str,
    instance_id: str,
    step_ids: set[str],
) -> bool:
    workflow = node.get("workflow") if isinstance(node.get("workflow"), dict) else {}
    if not workflow and isinstance(node.get("input"), dict):
        workflow = node["input"].get("workflow") if isinstance(node["input"].get("workflow"), dict) else {}
    if str(workflow.get("template_id") or "").strip() != template_id:
        return False
    if instance_id and str(workflow.get("instance_id") or "").strip() != instance_id:
        return False
    step_id = str(workflow.get("step_id") or "").strip()
    template_step_id = str(workflow.get("template_step_id") or "").strip()
    return bool((step_id and step_id in step_ids) or (template_step_id and template_step_id in step_ids))


async def _delete_virtual_workflow_nodes(
    *,
    project_id: str,
    nodes: list[dict[str, Any]],
    template_id: str,
    instance_id: str,
    step_ids: set[str],
) -> list[str]:
    if not step_ids:
        return []
    target_node_ids = [
        str(node.get("id") or "")
        for node in nodes
        if node.get("id") and _workflow_node_matches(
            node,
            template_id=template_id,
            instance_id=instance_id,
            step_ids=step_ids,
        )
    ]
    if not target_node_ids:
        return []
    result = await canvas_tools.delete_nodes(project_id, target_node_ids)
    deleted = [str(node_id) for node_id in result.get("_canvas_deleted_node_ids") or []]
    for node_id in deleted:
        await _emit_canvas_action(project_id, "delete_node", {"id": node_id})
    return deleted


def _without_node_ids(nodes: list[dict[str, Any]], deleted_ids: list[str]) -> list[dict[str, Any]]:
    if not deleted_ids:
        return nodes
    deleted = set(deleted_ids)
    return [node for node in nodes if str(node.get("id") or "") not in deleted]


def _virtual_workflow_step_result(
    *,
    project_id: str,
    template: dict[str, Any],
    step: dict[str, Any],
    instance_id: str,
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    input_facts = _input_summary(inputs or {})
    skipped = _workflow_step_condition_skipped(step, inputs)
    result_type = "workflow_skip" if skipped else "workflow_input"
    content = "该步骤已按输入条件跳过。" if skipped else "运行输入已保存。"
    step_id = str(step.get("id") or "").strip()
    return {
        "ok": True,
        "project_id": project_id,
        "template_id": template.get("id"),
        "template_name": template.get("name"),
        "instance_id": instance_id,
        "step_id": step_id,
        "node_id": None,
        "node": None,
        "created": False,
        "virtual": True,
        "skipped": skipped,
        "run_result": {
            "ok": True,
            "node_id": None,
            "type": "text",
            "status": "completed",
            "virtual": True,
            "skipped": skipped,
            "result": {
                "ok": True,
                "type": result_type,
                "title": step.get("title") or step_id or "输入",
                "input_facts": input_facts,
                "content": content,
                "reason": deepcopy(step.get("when")) if skipped else "",
            },
        },
    }


async def _hydrate_workflow_node_with_inputs(
    node_id: str,
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    if not node_id or str(node_id).startswith("workflow-runtime:"):
        raise ValueError(f"Workflow step is not bound to a canvas node: {node_id}")
    hydrated = await canvas_tools.get_node(node_id)
    if not isinstance(hydrated, dict) or hydrated.get("error"):
        raise ValueError(f"Node {node_id} not found")
    if not isinstance(inputs, dict) or not inputs:
        return hydrated
    fields = dict(hydrated.get("input") if isinstance(hydrated.get("input"), dict) else {})
    workflow = dict(fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {})
    input_facts = _input_summary(inputs)
    if workflow.get("input_facts") == input_facts and fields.get("input_values") == input_facts:
        return hydrated
    workflow["input_facts"] = input_facts
    fields["workflow"] = workflow
    fields["input_values"] = input_facts
    await canvas_tools.update_node(node_id, {"input_data": fields})
    hydrated["input"] = fields
    hydrated["workflow"] = workflow
    return hydrated


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _workflow_result_error(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    return str(result.get("error") or result.get("message") or "").strip()


def _workflow_ui_media_model_override(step: dict[str, Any], ui_overrides: dict[str, Any] | None) -> str:
    if not isinstance(ui_overrides, dict):
        return ""
    node_type = str(step.get("node_type") or "").strip()
    if node_type not in {"image", "video", "audio"}:
        return ""
    overrides = ui_overrides.get("media_model_overrides")
    if not isinstance(overrides, dict):
        return ""
    step_id = str(step.get("id") or "").strip()
    if not step_id:
        return ""
    return str(overrides.get(step_id) or "").strip()


def _workflow_ui_node_run_extra_fields(step: dict[str, Any], ui_overrides: dict[str, Any] | None) -> dict[str, Any]:
    model = _workflow_ui_media_model_override(step, ui_overrides)
    return {"model": model} if model else {}


def _workflow_strip_template_media_model(fields: dict[str, Any], node_type: str) -> dict[str, Any]:
    if node_type in {"image", "video", "audio"}:
        fields.pop("model", None)
    return fields


def _workflow_default_image_resolution(aspect_ratio: Any) -> str:
    aspect = str(aspect_ratio or "").strip().lower().replace("：", ":")
    if aspect in {"1:1", "square"}:
        return "2048x2048"
    if aspect in {"16:9", "landscape"}:
        return "2560x1440"
    if aspect in {"9:16", "portrait"}:
        return "1440x2560"
    return "2560x1440"


_WORKFLOW_CANVAS_SPEC_SYNC_KEYS = {
    "aspect_ratio",
    "resolution",
    "width",
    "height",
    "quality",
    "duration_seconds",
    "workflow_generate",
    "workflow_source_step",
    "workflow_source_path",
}


def _workflow_sync_existing_canvas_fields(
    existing_fields: dict[str, Any],
    desired_fields: dict[str, Any],
    node_type: str,
) -> dict[str, Any]:
    if node_type not in {"image", "video", "audio"}:
        return existing_fields
    result = dict(existing_fields)
    for key in _WORKFLOW_CANVAS_SPEC_SYNC_KEYS:
        if key in desired_fields:
            value = desired_fields.get(key)
            if value in (None, "", [], {}):
                result.pop(key, None)
            else:
                result[key] = deepcopy(value)
    if node_type == "video":
        for key in ("width", "height", "resolution_width", "resolution_height", "pixel_width", "pixel_height"):
            if key not in desired_fields:
                result.pop(key, None)
    return result


def _workflow_runtime_output_from_run_result(result: Any, hydrated: dict[str, Any]) -> Any:
    if isinstance(result, dict):
        for key in ("run_result", "result"):
            inner = result.get(key)
            if inner not in (None, "", [], {}):
                return _workflow_runtime_output_from_runner_payload(inner)
        content_key = next(
            (key for key in _WORKFLOW_RUNTIME_CONTENT_KEYS if result.get(key) not in (None, "", [], {})),
            "",
        )
        if content_key:
            return _workflow_runtime_clean_output_value({content_key: result[content_key]})
        media_output = {
            key: result[key]
            for key in _WORKFLOW_RUNTIME_MEDIA_OUTPUT_KEYS
            if result.get(key) not in (None, "", [], {})
        }
        if media_output:
            return media_output
        return _workflow_runtime_clean_output_value(result)
    output = hydrated.get("output")
    if output not in (None, "", [], {}):
        return _workflow_runtime_clean_output_value(output)
    return result


async def _set_workflow_step_runtime(
    *,
    project_id: str,
    node_id: str,
    inputs: dict[str, Any] | None,
    status: str,
    result: dict[str, Any] | None = None,
    template: dict[str, Any] | None = None,
    node_status: str | None = None,
) -> dict[str, Any]:
    hydrated = await _hydrate_workflow_node_with_inputs(node_id, inputs)
    fields = dict(hydrated.get("input") if isinstance(hydrated.get("input"), dict) else {})
    workflow = dict(fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {})
    now = _utc_now_iso()
    workflow["step_status"] = status
    if status == "running":
        workflow["last_started_at"] = now
        workflow.pop("last_error", None)
    elif status == "completed":
        workflow["last_completed_at"] = now
    elif status == "failed":
        workflow["last_failed_at"] = now
        workflow["last_error"] = _workflow_result_error(result) or "步骤运行失败"
    run_record = {
        "status": status,
        "at": now,
        "node_id": node_id,
        "error": workflow.get("last_error") if status == "failed" else None,
    }
    history = workflow.get("step_run_history")
    if not isinstance(history, list):
        history = []
    workflow["last_step_run"] = {k: v for k, v in run_record.items() if v not in (None, "", [], {})}
    workflow["step_run_history"] = [*history, workflow["last_step_run"]][-12:]
    fields["workflow"] = workflow
    effective_node_status = str(node_status or status).strip()
    patch: dict[str, Any] = {"input_data": fields}
    if effective_node_status in {"idle", "running", "completed", "failed"}:
        patch["status"] = effective_node_status
    if status == "running":
        patch["error_message"] = None
    elif status == "failed":
        patch["error_message"] = workflow["last_error"]
    await canvas_tools.update_node(node_id, patch)
    payload = {"id": node_id, "status": effective_node_status, "input": fields}
    if status == "failed":
        payload["error_message"] = workflow["last_error"]
    await _emit_canvas_action(project_id, "update_node", payload)
    hydrated["input"] = fields
    hydrated["workflow"] = workflow
    hydrated["status"] = effective_node_status
    template_id = str(workflow.get("template_id") or "").strip()
    step_id = str(workflow.get("step_id") or workflow.get("template_step_id") or "").strip()
    instance_id = str(workflow.get("instance_id") or "").strip()
    if template_id and step_id and instance_id:
        runtime_output = (
            _workflow_runtime_output_from_run_result(result, hydrated)
            if status == "completed"
            else result if status == "failed"
            else None
        )
        await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template if isinstance(template, dict) else {"id": template_id, "name": workflow.get("template_name") or ""},
            instance_id=instance_id,
            step_id=step_id,
            node_type=str(hydrated.get("type") or fields.get("type") or "text"),
            title=str(hydrated.get("title") or fields.get("title") or step_id),
            fields=fields,
            status=status,
            output=runtime_output,
            artifacts=[_workflow_runtime_artifact_from_node(hydrated, runtime_output if isinstance(runtime_output, dict) else result)],
            node_id=node_id,
            surface=str(workflow.get("surface") or fields.get("surface") or "draft_canvas"),
            increment_run=status == "running",
            error=workflow.get("last_error") if status == "failed" else "",
        )
    return hydrated


async def _sync_workflow_dependency_edges(
    *,
    project_id: str,
    node_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    sync_result = await canvas_tools.sync_dependency_edges(project_id, node_id, fields)
    for edge in sync_result.get("added_edges") or []:
        await _emit_canvas_action(project_id, "add_edge", {
            "id": edge.get("id"),
            "source": edge.get("source_node_id"),
            "target": edge.get("target_node_id"),
            "source_node_id": edge.get("source_node_id"),
            "target_node_id": edge.get("target_node_id"),
            "label": edge.get("label"),
        })
    for edge in sync_result.get("removed_edges") or []:
        await _emit_canvas_action(project_id, "delete_edge", {
            "id": edge.get("id"),
            "source": edge.get("source_node_id"),
            "target": edge.get("target_node_id"),
            "source_node_id": edge.get("source_node_id"),
            "target_node_id": edge.get("target_node_id"),
        })
    return sync_result


def _workflow_metadata_from_node(node: dict[str, Any]) -> dict[str, Any]:
    workflow = node.get("workflow") if isinstance(node.get("workflow"), dict) else {}
    if workflow:
        return workflow
    if isinstance(node.get("input"), dict) and isinstance(node["input"].get("workflow"), dict):
        return node["input"]["workflow"]
    return {}


def _workflow_step_nodes_by_id(
    nodes: list[dict[str, Any]],
    template_id: str,
    instance_id: str = "",
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        workflow = _workflow_metadata_from_node(node)
        if str(workflow.get("template_id") or "").strip() != template_id:
            continue
        if instance_id and str(workflow.get("instance_id") or "").strip() != instance_id:
            continue
        step_id = str(workflow.get("step_id") or workflow.get("template_step_id") or "").strip()
        if step_id and step_id not in result:
            result[step_id] = node
    return result


def _workflow_node_aliases(node: dict[str, Any]) -> list[str]:
    workflow = _workflow_metadata_from_node(node)
    fields = node.get("input") if isinstance(node.get("input"), dict) else {}
    step_id = str(workflow.get("step_id") or "").strip()
    template_step_id = str(workflow.get("template_step_id") or "").strip()
    source_step = str(
        fields.get("workflow_source_step")
        or fields.get("source_step")
        or fields.get("from_step")
        or workflow.get("workflow_source_step")
        or workflow.get("source_step")
        or ""
    ).strip()
    derived_source_aliases: list[str] = []
    if step_id.endswith("_canvas"):
        derived_source_aliases.append(step_id.removesuffix("_canvas"))
    if source_step and template_step_id and step_id.endswith(template_step_id):
        prefix = step_id[: -len(template_step_id)]
        if prefix:
            derived_source_aliases.append(f"{prefix}{source_step}")
    repeat_scoped = bool(workflow.get("repeat_group_id") or workflow.get("instance_scope"))
    if source_step and not repeat_scoped:
        derived_source_aliases.append(source_step)
    return _unique_nonempty_strings([
        step_id,
        template_step_id,
        str(workflow.get("source_node_id") or ""),
        str(workflow.get("repeat_group_id") or ""),
        *derived_source_aliases,
    ])


def _workflow_step_nodes_by_alias(
    nodes: list[dict[str, Any]],
    template_id: str,
    instance_id: str = "",
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        workflow = _workflow_metadata_from_node(node)
        if str(workflow.get("template_id") or "").strip() != template_id:
            continue
        if instance_id and str(workflow.get("instance_id") or "").strip() != instance_id:
            continue
        for alias in _workflow_node_aliases(node):
            result.setdefault(alias, []).append(node)
    return result


def _workflow_dependency_nodes(
    dep_key: str,
    *,
    created_by_step: dict[str, dict[str, Any]],
    nodes_by_alias: dict[str, list[dict[str, Any]]],
    target_step: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    dep = str(dep_key or "").strip()
    if not dep:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(node: dict[str, Any] | None) -> None:
        if not isinstance(node, dict):
            return
        node_id = str(node.get("id") or "").strip()
        marker = node_id or str(id(node))
        if marker in seen:
            return
        seen.add(marker)
        result.append(node)

    def add_if_scoped(node: dict[str, Any] | None) -> None:
        if not _workflow_dependency_node_matches_scope(dep, target_step, node):
            return
        add(node)

    add_if_scoped(created_by_step.get(dep))
    for node in created_by_step.values():
        if dep in _workflow_node_aliases(node):
            add_if_scoped(node)
    for node in nodes_by_alias.get(dep) or []:
        add_if_scoped(node)
    return result


def _workflow_item_workflow_meta(item: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    workflow = _workflow_metadata_from_node(item)
    return workflow if workflow else item


def _workflow_item_scope(item: dict[str, Any] | None) -> dict[str, Any]:
    meta = _workflow_item_workflow_meta(item)
    scope = meta.get("instance_scope") if isinstance(meta.get("instance_scope"), dict) else {}
    return scope


def _workflow_item_repeat_group(item: dict[str, Any] | None) -> str:
    return str(_workflow_item_workflow_meta(item).get("repeat_group_id") or "").strip()


def _workflow_scope_index_key(item: dict[str, Any] | None) -> str:
    meta = _workflow_item_workflow_meta(item)
    scope = _workflow_item_scope(item)
    for value in (
        meta.get("repeat_group_index"),
        scope.get("index"),
        scope.get("segment_index"),
        scope.get("segment"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _workflow_same_current_repeat_scope(target: dict[str, Any] | None, candidate: dict[str, Any] | None) -> bool:
    target_group = _workflow_item_repeat_group(target)
    candidate_group = _workflow_item_repeat_group(candidate)
    if not target_group or not candidate_group or target_group != candidate_group:
        return True
    target_index = _workflow_scope_index_key(target)
    candidate_index = _workflow_scope_index_key(candidate)
    if target_index and candidate_index:
        return target_index == candidate_index
    target_scope = _workflow_item_scope(target)
    candidate_scope = _workflow_item_scope(candidate)
    compared = False
    for key in ("episode", "segment", "index", "episode_index", "segment_index", "start_second", "end_second"):
        target_value = str(target_scope.get(key) or "").strip()
        candidate_value = str(candidate_scope.get(key) or "").strip()
        if not target_value or not candidate_value:
            continue
        compared = True
        if target_value != candidate_value:
            return False
    return True


def _workflow_repeat_index_int(item: dict[str, Any] | None) -> int | None:
    text = _workflow_scope_index_key(item)
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _workflow_same_or_previous_prompt_scope(target: dict[str, Any] | None, candidate: dict[str, Any] | None) -> bool:
    target_group = _workflow_item_repeat_group(target)
    candidate_group = _workflow_item_repeat_group(candidate)
    if not target_group or not candidate_group or target_group != candidate_group:
        return True
    target_index = _workflow_repeat_index_int(target)
    candidate_index = _workflow_repeat_index_int(candidate)
    if target_index is not None and candidate_index is not None:
        return candidate_index in {target_index, target_index - 1}
    return _workflow_same_current_repeat_scope(target, candidate)


def _workflow_dependency_node_matches_scope(
    dep_key: str,
    target_step: dict[str, Any] | None,
    node: dict[str, Any] | None,
) -> bool:
    if not isinstance(target_step, dict) or not isinstance(node, dict):
        return isinstance(node, dict)
    target_group = _workflow_item_repeat_group(target_step)
    candidate_group = _workflow_item_repeat_group(node)
    if not target_group or not candidate_group or target_group != candidate_group:
        return True
    dep = str(dep_key or "").strip()
    candidate_meta = _workflow_item_workflow_meta(node)
    candidate_step_id = str(candidate_meta.get("step_id") or "").strip()
    if dep and candidate_step_id and dep == candidate_step_id:
        return True
    return _workflow_same_current_repeat_scope(target_step, node)


def _workflow_visible_dependency_nodes(
    dep_key: str,
    *,
    created_by_step: dict[str, dict[str, Any]],
    nodes_by_alias: dict[str, list[dict[str, Any]]],
    steps_by_id: dict[str, dict[str, Any]],
    target_step: dict[str, Any] | None = None,
    seen_step_ids: set[str] | None = None,
    exclude_node_ids: set[str] | None = None,
    include_runtime_upstream: bool = False,
) -> list[dict[str, Any]]:
    dep = str(dep_key or "").strip()
    if not dep:
        return []
    seen_step_ids = set(seen_step_ids or set())
    exclude_node_ids = {str(item) for item in (exclude_node_ids or set()) if str(item or "").strip()}
    result: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()

    def add(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or "").strip()
        marker = node_id or str(id(node))
        if marker in seen_nodes:
            return
        seen_nodes.add(marker)
        result.append(node)

    for node in _workflow_dependency_nodes(
        dep,
        created_by_step=created_by_step,
        nodes_by_alias=nodes_by_alias,
        target_step=target_step,
    ):
        node_id = str(node.get("id") or "").strip()
        if _workflow_is_canvas_dependency_record(node):
            if node_id not in exclude_node_ids:
                add(node)
            continue
        if not include_runtime_upstream:
            continue
        workflow = _workflow_metadata_from_node(node)
        runtime_step_id = str(
            workflow.get("step_id")
            or workflow.get("template_step_id")
            or dep
        ).strip()
        if not runtime_step_id or runtime_step_id in seen_step_ids:
            continue
        step = steps_by_id.get(runtime_step_id) or {}
        upstream_deps = [
            str(item or "").strip()
            for item in (
                step.get("depends_on")
                or workflow.get("depends_on")
                or []
            )
            if str(item or "").strip()
        ]
        control_deps = {
            *_workflow_control_dependency_ids(step),
            *_workflow_control_dependency_ids(workflow),
        }
        for upstream_dep in upstream_deps:
            if upstream_dep in control_deps:
                continue
            for upstream_node in _workflow_visible_dependency_nodes(
                upstream_dep,
                created_by_step=created_by_step,
                nodes_by_alias=nodes_by_alias,
                steps_by_id=steps_by_id,
                target_step=target_step,
                seen_step_ids={*seen_step_ids, runtime_step_id},
                exclude_node_ids=exclude_node_ids,
                include_runtime_upstream=include_runtime_upstream,
            ):
                add(upstream_node)
    return result


def _workflow_dependency_refs_for_step(
    step: dict[str, Any],
    *,
    created_by_step: dict[str, dict[str, Any]],
    nodes_by_alias: dict[str, list[dict[str, Any]]],
    steps_by_id: dict[str, dict[str, Any]],
    virtual_step_ids: set[str],
    target_node_id: str = "",
    extra_dep_keys: list[str] | None = None,
    include_runtime_upstream: bool = False,
) -> list[dict[str, str]]:
    dep_refs: list[dict[str, str]] = []
    exclude_node_ids = {target_node_id} if target_node_id else set()

    def add_dep(dep_key: Any, role: str) -> None:
        dep_text = str(dep_key or "").strip()
        if not dep_text or dep_text in virtual_step_ids:
            return
        for dep_node in _workflow_visible_dependency_nodes(
            dep_text,
            created_by_step=created_by_step,
            nodes_by_alias=nodes_by_alias,
            steps_by_id=steps_by_id,
            target_step=step,
            exclude_node_ids=exclude_node_ids,
            include_runtime_upstream=include_runtime_upstream,
        ):
            dep_refs.append(_reference_for_dep(dep_node, role))

    role = str(step.get("dependency_role") or "context").strip() or "context"
    for dep in _workflow_data_dependency_ids(step):
        add_dep(dep, role)
    for dep in extra_dep_keys or []:
        add_dep(dep, role)
    for context_ref in _workflow_context_ref_specs(step):
        add_dep(context_ref["ref"], context_ref["role"])
    return _dedupe_workflow_references(dep_refs)


_WORKFLOW_LLM_VISION_ROLE = "vision_context"


def _workflow_reference_selectors(step: dict[str, Any], workflow: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    raw: Any
    if isinstance(workflow, dict) and "reference_selectors" in workflow:
        raw = workflow.get("reference_selectors")
    else:
        raw = step.get("reference_selectors")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [deepcopy(item) for item in raw if isinstance(item, dict)]


def _workflow_context_ref_specs(step: dict[str, Any]) -> list[dict[str, str]]:
    raw = step.get("context_refs")
    if raw in (None, "", [], {}):
        return []
    items = raw if isinstance(raw, list) else [raw]
    result: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            ref = item.get("step") or item.get("id") or item.get("ref") or item.get("source")
            role = item.get("role") or "context"
        else:
            ref = item
            role = "context"
        text = str(ref or "").strip()
        if text:
            result.append({"ref": text, "role": str(role or "context").strip() or "context"})
    return result


def _workflow_vision_context_nodes(
    step: dict[str, Any],
    *,
    records: list[dict[str, Any]],
    context: dict[str, Any],
    template_id: str,
    instance_id: str,
    steps_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    created_by_step = _workflow_step_nodes_by_id(records, template_id, instance_id)
    nodes_by_alias = _workflow_step_nodes_by_alias(records, template_id, instance_id)
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()

    def add(node: dict[str, Any]) -> None:
        if not _workflow_is_canvas_dependency_record(node) or str(node.get("type") or "") != "image":
            return
        marker = str(node.get("id") or "").strip() or str(id(node))
        if marker in seen:
            return
        seen.add(marker)
        selected.append(node)

    for spec in _workflow_context_ref_specs(step):
        if str(spec.get("role") or "").strip() != _WORKFLOW_LLM_VISION_ROLE:
            continue
        matches = _workflow_visible_dependency_nodes(
            spec["ref"],
            created_by_step=created_by_step,
            nodes_by_alias=nodes_by_alias,
            steps_by_id=steps_by_id,
            target_step=step,
        )
        image_matches = [
            node for node in matches
            if _workflow_is_canvas_dependency_record(node) and str(node.get("type") or "") == "image"
        ]
        if not image_matches:
            missing.append(spec["ref"])
            continue
        for node in image_matches:
            add(node)

    vision_selectors = [
        selector
        for selector in _workflow_reference_selectors(step)
        if str(selector.get("role") or "").strip() == _WORKFLOW_LLM_VISION_ROLE
    ]
    for node, _role in _workflow_reference_selector_nodes(
        vision_selectors,
        nodes=records,
        context=context,
        template_id=template_id,
        instance_id=instance_id,
        target_step=step,
    ):
        add(node)
    return selected, _unique_nonempty_strings(missing)


def _workflow_node_selector_tokens(node: dict[str, Any], selector: dict[str, Any]) -> set[str]:
    fields = selector.get("match_fields") if isinstance(selector.get("match_fields"), list) else None
    workflow = _workflow_metadata_from_node(node)
    tokens: set[str] = set()
    tokens.update(_workflow_tokens_from_value(workflow.get("instance_scope"), fields))
    tokens.update(_workflow_tokens_from_value(workflow.get("source_node_id"), fields))
    tokens.update(_workflow_tokens_from_value(workflow.get("step_id"), fields))
    tokens.update(_workflow_tokens_from_value(workflow.get("template_step_id"), fields))
    tokens.update(_workflow_tokens_from_value(node.get("title"), fields))
    if isinstance(node.get("input"), dict):
        tokens.update(_workflow_tokens_from_value(node["input"].get("workflow", {}).get("instance_scope"), fields))
    return tokens


def _workflow_reference_selector_nodes(
    selectors: list[dict[str, Any]],
    *,
    nodes: list[dict[str, Any]],
    context: dict[str, Any],
    template_id: str,
    instance_id: str = "",
    target_step: dict[str, Any] | None = None,
) -> list[tuple[dict[str, Any], str]]:
    result: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for selector in selectors:
        source_step = selector.get("source_step") or selector.get("from_source_step") or selector.get("source")
        source_payload = None
        if source_step and isinstance(target_step, dict):
            source_key = str(source_step or "").strip()
            for node in nodes:
                workflow = _workflow_metadata_from_node(node)
                if template_id and str(workflow.get("template_id") or "").strip() != template_id:
                    continue
                if instance_id and str(workflow.get("instance_id") or "").strip() != instance_id:
                    continue
                if source_key not in _workflow_node_aliases(node):
                    continue
                if not _workflow_dependency_node_matches_scope(source_key, target_step, node):
                    continue
                source_payload = _workflow_node_context_payload(node)
                break
        if source_payload is None:
            source_payload = _workflow_context_get(context, source_step)
        source_path = str(selector.get("source_path") or selector.get("path") or "output.appearing_characters").strip()
        selected_values = _workflow_values_at_path(source_payload, source_path) if source_payload is not None else []
        match_fields = selector.get("match_fields") if isinstance(selector.get("match_fields"), list) else None
        selected_tokens: set[str] = set()
        for value in selected_values:
            selected_tokens.update(_workflow_tokens_from_value(value, match_fields))
        if not selected_tokens:
            continue

        group_key = selector.get("from_group") or selector.get("candidate_group") or selector.get("from_step")
        role = str(selector.get("role") or "visual_reference").strip() or "visual_reference"
        for node in nodes:
            if not _workflow_is_canvas_dependency_record(node):
                continue
            workflow = _workflow_metadata_from_node(node)
            if template_id and str(workflow.get("template_id") or "").strip() != template_id:
                continue
            if instance_id and str(workflow.get("instance_id") or "").strip() != instance_id:
                continue
            aliases = _workflow_node_aliases(node)
            if group_key and not any(_workflow_alias_equal(alias, group_key) for alias in aliases):
                continue
            candidate_tokens = _workflow_node_selector_tokens(node, selector)
            if not _workflow_tokens_match(selected_tokens, candidate_tokens):
                continue
            node_id = str(node.get("id") or "").strip()
            marker = node_id or str(id(node))
            if marker in seen:
                continue
            seen.add(marker)
            result.append((node, role))
    return result


def _workflow_node_context_payload(node: dict[str, Any]) -> dict[str, Any]:
    output = _structured_workflow_output(node.get("output"))
    outputs = node.get("outputs") if isinstance(node.get("outputs"), list) else output
    payload: dict[str, Any] = {
        "node_id": node.get("id"),
        "title": node.get("title"),
        "type": node.get("type"),
        "status": node.get("status"),
        "output": output,
        "outputs": outputs,
    }
    if node.get("artifacts"):
        payload["artifacts"] = node.get("artifacts")
    if isinstance(node.get("input"), dict):
        payload["input"] = node["input"]
    return {key: value for key, value in payload.items() if value is not None}


def _workflow_runtime_context_from_nodes(
    nodes: list[dict[str, Any]],
    *,
    template_id: str,
    instance_id: str = "",
) -> dict[str, Any]:
    target_template_id = str(template_id or "").strip()
    target_instance_id = str(instance_id or "").strip()
    matching: list[dict[str, Any]] = []
    for node in nodes:
        workflow = _workflow_metadata_from_node(node)
        if target_template_id and str(workflow.get("template_id") or "").strip() != target_template_id:
            continue
        matching.append(node)

    if not target_instance_id:
        for node in reversed(matching):
            workflow = _workflow_metadata_from_node(node)
            candidate = str(workflow.get("instance_id") or "").strip()
            if candidate:
                target_instance_id = candidate
                break

    context: dict[str, Any] = {}
    for node in reversed(matching):
        workflow = _workflow_metadata_from_node(node)
        if target_instance_id and str(workflow.get("instance_id") or "").strip() != target_instance_id:
            continue
        payload = _workflow_node_context_payload(node)
        for key in (
            workflow.get("step_id"),
            workflow.get("template_step_id"),
            workflow.get("source_node_id"),
        ):
            text = str(key or "").strip()
            if text and text not in context:
                context[text] = payload
    return context


async def _workflow_runtime_context_from_project(
    project_id: str,
    *,
    template_id: str,
    instance_id: str = "",
) -> dict[str, Any]:
    nodes = [
        node
        for node in await canvas_tools.list_nodes(project_id)
        if _workflow_is_canvas_dependency_record(node)
    ]
    nodes.extend(await _workflow_runtime_records_from_project(
        project_id,
        template_id=template_id,
        instance_id=instance_id,
    ))
    return _workflow_runtime_context_from_nodes(
        nodes,
        template_id=template_id,
        instance_id=instance_id,
    )


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
            raw_deps = step.get("depends_on") or step.get("needs") or []
            if isinstance(raw_deps, str):
                raw_deps = [raw_deps]
            if not isinstance(raw_deps, list):
                raw_deps = []
            deps = [
                str(dep or "").strip()
                for dep in raw_deps
                if str(dep or "").strip()
            ]
            if any(dep in remaining for dep in deps):
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


async def _workflow_template_from_spec(
    *,
    project_id: str,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        if workflow:
            template = canvas_workflow_templates.normalize_inline_workflow(
                _workflow_with_dependency_order(workflow),
                input_values=_dimension_input_values(
                    inputs if isinstance(inputs, dict) else {},
                    context,
                ),
            )
        elif artifact_ref:
            artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
            template = canvas_workflow_templates.normalize_inline_workflow(
                _workflow_with_dependency_order(artifact["workflow"]),
                input_values=_dimension_input_values(
                    inputs if isinstance(inputs, dict) else {},
                    context,
                ),
            )
        else:
            template = canvas_workflow_templates.get_template(
                template_id,
                input_values=_dimension_input_values(
                    inputs if isinstance(inputs, dict) else {},
                    context,
                ),
            )
    except FileNotFoundError as exc:
        return None, {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_not_found"}
    except (ValueError, json.JSONDecodeError) as exc:
        return None, {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_error"}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return None, {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_template_error",
            "available_templates": _light_template_catalog(limit=12)[0],
        }
    required_error = _required_input_error(template, _workflow_effective_inputs(template, inputs))
    if required_error:
        if artifact_ref:
            required_error["artifact_ref"] = artifact_ref
        return None, required_error
    return template, None


@register(
    "workflow.state_evidence",
    description="返回工作流运行态、画布节点和依赖边的后端只读证据。",
    tags=["workflow", "read"],
    search_hint=(
        "workflow backend state evidence runtime canvas nodes edges dependency debug review "
        "工作流 后端证据 运行态 画布节点 依赖边 审查 排障"
    ),
    usage_hints=[
        "审查工作流运行结果、画布映射或依赖边是否正确时使用。",
        "template_id 和 instance_id 可选；为空时返回当前项目相关工作流状态。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "instance_id": {"type": "string"},
        },
        "required": ["project_id"],
    },
)
async def workflow_state_evidence(
    project_id: str,
    template_id: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    from app.agent.workflow_state_evidence import build_workflow_state_evidence

    async with session_scope() as session:
        return await build_workflow_state_evidence(
            project_id,
            session,
            template_id=template_id,
            instance_id=instance_id,
        )


@register(
    "workflow.semantic_review",
    description="用压缩证据对 workflow spec 做只读语义审查。",
    tags=["workflow", "review", "read"],
    search_hint=(
        "workflow semantic review evidence audit dry-run visible outputs dependencies "
        "工作流 语义审查 证据 audit dry-run 可见产物 依赖 输入 prompt"
    ),
    usage_hints=[
        "用于 deterministic audit 通过后，检查流程是否真正符合用户目标。",
        "audit 失败时直接返回阻塞证据，不调用 reviewer。",
        "传 template_id、artifact_ref 或 inline workflow 三选一；inputs 作为样例输入和动态展开依据。",
    ],
    is_read_only=True,
    is_concurrency_safe=False,
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "workflow": {"type": "object", "additionalProperties": True},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "user_goal": {"type": "string"},
            "review_goal": {"type": "string"},
            "max_steps": {"type": "integer"},
        },
        "required": ["project_id"],
    },
)
async def workflow_semantic_review(
    project_id: str,
    template_id: str = "",
    artifact_ref: str = "",
    workflow: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    user_goal: str = "",
    review_goal: str = "",
    max_steps: int = 3,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}

    input_values = _dimension_input_values(inputs if isinstance(inputs, dict) else {}, context)
    source: dict[str, Any] = {}
    raw_workflow: dict[str, Any] | None = workflow if isinstance(workflow, dict) else None
    normalized: dict[str, Any] | None = None

    try:
        if raw_workflow is not None:
            normalized = canvas_workflow_templates.normalize_inline_workflow(
                raw_workflow,
                input_values=input_values,
            )
            source = {"kind": "inline_workflow"}
        elif artifact_ref:
            artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
            raw_workflow = artifact.get("workflow") if isinstance(artifact.get("workflow"), dict) else {}
            artifact_inputs = artifact.get("sample_inputs") if isinstance(artifact.get("sample_inputs"), dict) else {}
            if not input_values:
                input_values = dict(artifact_inputs)
            else:
                input_values = {**artifact_inputs, **input_values}
            normalized = canvas_workflow_templates.normalize_inline_workflow(
                raw_workflow,
                input_values=input_values,
            )
            source = {"kind": "artifact", "artifact_ref": artifact_ref}
        else:
            template = canvas_workflow_templates.get_template(
                template_id,
                input_values=input_values,
            )
            raw_workflow = template.get("public_spec") if isinstance(template.get("public_spec"), dict) else template
            normalized = template
            source = {
                "kind": "template",
                "template_id": str(template.get("id") or template_id or "").strip(),
                "scope": str(template.get("scope") or "").strip(),
            }
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_not_found"}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_error"}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_template_error",
            "available_templates": _light_template_catalog(limit=12)[0],
        }

    audit = audit_workflow_spec(raw_workflow or {}, normalized=normalized, sample_inputs=input_values)
    evidence = build_workflow_semantic_review_evidence(
        workflow=raw_workflow or {},
        normalized=normalized,
        audit=audit,
        input_values=input_values,
        user_goal=user_goal,
        source=source,
    )
    subject_key = _workflow_review_subject_key(source, raw_workflow)
    if not audit.get("ok"):
        repair_policy = await _record_workflow_review_repair_attempt(
            project_id=project_id,
            subject_key=subject_key,
            failed=True,
            status=str(audit.get("status") or "blocked"),
            findings=audit.get("findings") if isinstance(audit.get("findings"), list) else [],
        )
        return {
            "ok": True,
            "status": "blocked",
            "review_skipped": True,
            "skip_reason": "deterministic_audit_failed",
            "source": source,
            "audit": evidence.get("audit") or {},
            "evidence": evidence,
            "repair_policy": repair_policy,
            **({
                "terminal": True,
                "suggested_next": "report_blocked_to_user",
            } if repair_policy.get("blocked") else {}),
            "review_result": {
                "status": "blocked",
                "passed": False,
                "safe_to_run": False,
                "safe_to_submit": False,
                "findings": audit.get("findings") if isinstance(audit.get("findings"), list) else [],
            },
        }

    from app.mcp_tools.agent_tools import agent_review

    effective_review_goal = (
        str(review_goal or "").strip()
        or "检查 workflow 是否语义上满足用户目标、输入定义清晰、依赖与可见产物正确。"
    )
    review = await agent_review(
        project_id=project_id,
        review_goal=effective_review_goal,
        user_request=str(user_goal or ""),
        work_summary=(
            f"Workflow {evidence.get('workflow', {}).get('name') or evidence.get('workflow', {}).get('id') or ''} "
            f"has {evidence.get('workflow', {}).get('step_count') or 0} expanded step(s), "
            f"{len(evidence.get('visible_outputs') or [])} visible output step(s), "
            f"audit status {audit.get('status')}."
        ),
        review_profile="workflow_semantic",
        evidence=evidence,
        custom_checklist=evidence.get("semantic_checklist") if isinstance(evidence.get("semantic_checklist"), list) else [],
        focus=[
            "workflow inputs",
            "step coverage",
            "dependency semantics",
            "visible canvas outputs",
            "prompt feasibility",
            "dry-run final outputs",
        ],
        max_steps=max(1, min(int(max_steps or 3), 6)),
    )
    review_result = review.get("result") if isinstance(review.get("result"), dict) else {}
    review_status = str(review.get("review_status") or review_result.get("status") or "reviewed").strip()
    findings = review_result.get("findings") if isinstance(review_result.get("findings"), list) else []
    review_failed = review_status in {"revise_required", "blocked"} or bool(
        review_result and review_result.get("safe_to_run") is False and findings
    )
    repair_policy = await _record_workflow_review_repair_attempt(
        project_id=project_id,
        subject_key=subject_key,
        failed=review_failed,
        status=review_status,
        findings=findings,
    )
    return {
        "ok": bool(review.get("ok", True)),
        "status": review_status,
        "source": source,
        "audit": evidence.get("audit") or {},
        "evidence": evidence,
        "repair_policy": repair_policy,
        "review_result": review_result,
        "review": review,
        **({
            "terminal": True,
            "suggested_next": "report_blocked_to_user",
        } if repair_policy.get("blocked") else {}),
        "next_action": "若 review_result 为 revise_required 或 blocked，使用 workflow.spec.apply_patch 修订；通过后再保存或运行。",
    }


async def workflow_preview(
    project_id: str,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    instance_id: str = "",
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    input_facts = inputs if isinstance(inputs, dict) else {}

    def resolve_template(input_context: dict[str, Any] | None) -> dict[str, Any]:
        input_values = _dimension_input_values(input_facts, input_context)
        if workflow:
            return canvas_workflow_templates.normalize_inline_workflow(
                workflow,
                input_values=input_values,
            )
        if artifact_ref:
            artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
            return canvas_workflow_templates.normalize_inline_workflow(
                artifact["workflow"],
                input_values=input_values,
            )
        return canvas_workflow_templates.get_template(
            template_id,
            input_values=input_values,
        )

    try:
        template = resolve_template(context)
        resolved_template_id = str(template.get("id") or template_id or "").strip()
        server_context = await _workflow_runtime_context_from_project(
            project_id,
            template_id=resolved_template_id,
            instance_id=instance_id,
        )
        if server_context:
            merged_context = _merge_dict(context if isinstance(context, dict) else {}, server_context)
            template = resolve_template(merged_context)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_not_found"}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_error"}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_template_error",
            "available_templates": _light_template_catalog(limit=12)[0],
        }
    steps = canvas_workflow_templates.template_step_summaries(template.get("steps") or [])
    return {
        "ok": True,
        "project_id": project_id,
        "template_id": str(template.get("id") or template_id or "").strip(),
        "name": str(template.get("name") or template.get("id") or template_id or "").strip(),
        "description": str(template.get("description") or "").strip(),
        "inputs": [str(item) for item in template.get("inputs") or []],
        "required_inputs": [str(item) for item in template.get("required_inputs") or []],
        "steps": steps,
        "step_count": len(steps),
        "deferred_groups": deepcopy(template.get("deferred_groups") or []),
    }


async def _persist_active_workflow_for_run(
    *,
    project_id: str,
    template: dict[str, Any],
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    title: str = "",
) -> None:
    now = _utc_now_iso()
    active: dict[str, Any]
    artifact = str(artifact_ref or "").strip()
    selected_template_id = str(template_id or "").strip()
    if artifact:
        active = {
            "kind": "artifact",
            "artifact_ref": artifact,
            "name": str(title or template.get("name") or "").strip(),
            "description": str(template.get("description") or "").strip(),
            "updated_at": now,
        }
    elif selected_template_id:
        active = {
            "kind": "template",
            "template_id": selected_template_id,
            "updated_at": now,
        }
    elif isinstance(workflow, dict) and workflow:
        active = {
            "kind": "imported",
            "workflow": deepcopy(workflow),
            "name": str(title or workflow.get("name") or workflow.get("title") or template.get("name") or "").strip(),
            "description": str(workflow.get("description") or template.get("description") or "").strip(),
            "updated_at": now,
        }
    else:
        inferred_template_id = str(template.get("id") or "").strip()
        if not inferred_template_id:
            return
        active = {
            "kind": "template",
            "template_id": inferred_template_id,
            "updated_at": now,
        }
    await _write_project_state_patch(project_id, {_ACTIVE_WORKFLOW_STATE_KEY: active})
    await _emit_workflow_runtime_update(
        project_id=project_id,
        template_id=str(active.get("template_id") or template.get("id") or ""),
        instance_id="",
        step_id="",
        status="active_workflow",
    )


async def _emit_workflow_runtime_update(
    *,
    project_id: str,
    template_id: str = "",
    instance_id: str = "",
    step_id: str = "",
    status: str = "",
    runtime: dict[str, Any] | None = None,
) -> None:
    if not project_id:
        return
    try:
        from app.agent.orchestrator import emit_canvas_event

        payload: dict[str, Any] = {
            "project_id": project_id,
            "template_id": template_id,
            "instance_id": instance_id,
            "step_id": step_id,
            "status": status,
        }
        if isinstance(runtime, dict) and runtime:
            payload["runtime"] = runtime
        await emit_canvas_event(
            {
                "type": "canvas_action",
                "action": "workflow_runtime_update",
                "payload": payload,
            },
            project_id=project_id,
        )
    except Exception:
        return


async def _materialize_workflow_step(
    *,
    project_id: str,
    template: dict[str, Any],
    step_id: str,
    inputs: dict[str, Any] | None = None,
    instance_id: str = "",
    title: str = "",
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    target_id = str(step_id or "").strip()
    if not target_id:
        return {"ok": False, "error": "step_id is required", "error_kind": "missing_step_id"}
    steps = [step for step in template.get("steps") or [] if isinstance(step, dict)]
    step_index = next((index for index, step in enumerate(steps) if str(step.get("id") or "").strip() == target_id), -1)
    if step_index < 0:
        return {
            "ok": False,
            "error": f"Workflow step not found: {target_id}",
            "error_kind": "workflow_step_not_found",
            "step_id": target_id,
            "available_step_ids": [str(step.get("id") or "") for step in steps],
        }
    step = steps[step_index]
    raw_nodes = await canvas_tools.list_nodes(project_id)
    nodes = [
        node
        for node in raw_nodes
        if _workflow_is_canvas_dependency_record(node)
    ]
    nodes_by_canvas_id = {
        str(node.get("id") or "").strip(): node
        for node in nodes
        if str(node.get("id") or "").strip()
    }
    template_id = str(template.get("id") or "")
    if not instance_id:
        for node in reversed(nodes):
            workflow = _workflow_metadata_from_node(node)
            if str(workflow.get("template_id") or "").strip() == template_id:
                instance_id = str(workflow.get("instance_id") or "").strip()
                if instance_id:
                    break
    state = await _read_project_state(project_id)
    runtime_records = _workflow_runtime_records_from_state(state, template_id=template_id, instance_id=instance_id)
    if not instance_id:
        for node in reversed(runtime_records):
            workflow = _workflow_metadata_from_node(node)
            candidate = str(workflow.get("instance_id") or "").strip()
            if candidate:
                instance_id = candidate
                break
    if not instance_id:
        instance_id = f"wf_{uuid.uuid4().hex[:12]}"
        runtime_records = []
    elif runtime_records:
        runtime_records = [
            record
            for record in runtime_records
            if str(_workflow_metadata_from_node(record).get("instance_id") or "").strip() == instance_id
        ]

    all_records = [*nodes, *runtime_records]
    created_by_step = _workflow_step_nodes_by_id(all_records, template_id, instance_id)
    nodes_by_alias = _workflow_step_nodes_by_alias(all_records, template_id, instance_id)
    steps_by_id = {
        str(item.get("id") or "").strip(): item
        for item in steps
        if str(item.get("id") or "").strip()
    }
    virtual_step_ids = _virtual_workflow_step_ids(steps, inputs)
    deleted_virtual_ids = await _delete_virtual_workflow_nodes(
        project_id=project_id,
        nodes=raw_nodes,
        template_id=template_id,
        instance_id=instance_id,
        step_ids=virtual_step_ids,
    )
    if deleted_virtual_ids:
        nodes = _without_node_ids(nodes, deleted_virtual_ids)
        all_records = [*nodes, *runtime_records]
        created_by_step = _workflow_step_nodes_by_id(all_records, template_id, instance_id)
        nodes_by_alias = _workflow_step_nodes_by_alias(all_records, template_id, instance_id)
    if target_id in virtual_step_ids:
        return _virtual_workflow_step_result(
            project_id=project_id,
            template=template,
            step=step,
            instance_id=instance_id,
            inputs=inputs,
        )
    default_fields = template.get("defaults", {}).get("fields")
    if not isinstance(default_fields, dict):
        default_fields = {}
    target_surface = _workflow_step_surface(step)
    existing = created_by_step.get(target_id)
    if existing and target_surface != "workflow_runtime":
        existing_id = str(existing.get("id") or "").strip()
        existing_node_id = str(existing.get("node_id") or "").strip()
        if not existing_node_id:
            artifacts = existing.get("artifacts") if isinstance(existing.get("artifacts"), list) else []
            existing_node_id = next(
                (
                    str(artifact.get("node_id") or "").strip()
                    for artifact in artifacts
                    if isinstance(artifact, dict) and str(artifact.get("node_id") or "").strip()
                ),
                "",
            )
        if existing_id.startswith("workflow-runtime:"):
            existing = nodes_by_canvas_id.get(existing_node_id)
        elif _workflow_record_surface(existing) == "workflow_runtime":
            existing = None
    if existing:
        if target_surface != "workflow_runtime" and not str(existing.get("id") or "").startswith("workflow-runtime:"):
            existing_fields = dict(existing.get("input") if isinstance(existing.get("input"), dict) else {})
            original_existing_fields = deepcopy(existing_fields)
            existing_node_type = str(existing.get("type") or step.get("node_type") or "text")
            desired_step_fields = _workflow_strip_template_media_model(
                _merge_dict(default_fields, step.get("fields") or {}),
                existing_node_type,
            )
            if existing_node_type == "image":
                desired_step_fields.setdefault("aspect_ratio", template.get("defaults", {}).get("aspect_ratio") or "9:16")
                desired_step_fields.setdefault(
                    "resolution",
                    template.get("defaults", {}).get("resolution")
                    or _workflow_default_image_resolution(desired_step_fields.get("aspect_ratio")),
                )
                desired_step_fields.setdefault("quality", template.get("defaults", {}).get("quality") or "high")
            if existing_node_type == "video":
                desired_step_fields.setdefault("aspect_ratio", template.get("defaults", {}).get("aspect_ratio") or "9:16")
                desired_step_fields.setdefault("resolution", template.get("defaults", {}).get("resolution") or "720p")
                if template.get("defaults", {}).get("duration_seconds"):
                    desired_step_fields.setdefault("duration_seconds", template["defaults"]["duration_seconds"])
            existing_fields = _workflow_sync_existing_canvas_fields(
                existing_fields,
                desired_step_fields,
                existing_node_type,
            )
            existing_workflow = dict(existing_fields.get("workflow") if isinstance(existing_fields.get("workflow"), dict) else {})
            canonical_workflow = {
                **existing_workflow,
                "template_id": template_id,
                "template_name": template.get("name"),
                "instance_id": instance_id,
                "step_id": target_id,
                "step_index": step_index + 1,
                "depends_on": [str(dep).strip() for dep in (step.get("depends_on") or []) if str(dep).strip()],
                "surface": target_surface,
                "visibility": step.get("visibility") or "canvas",
            }
            for key, value in _step_workflow_metadata(step).items():
                if key == "prompt_template" and existing_workflow.get(key) not in (None, "", [], {}):
                    continue
                canonical_workflow[key] = value
            existing_fields["workflow"] = canonical_workflow
            effective_source = _workflow_effective_source_step(existing_fields, step)
            if effective_source:
                existing_fields["workflow_source_step"] = effective_source
            effective_source_path = _workflow_effective_source_path(existing_fields, step)
            if effective_source_path:
                existing_fields["workflow_source_path"] = effective_source_path
            existing_fields = _merge_workflow_dependency_refs(
                existing_fields,
                _workflow_dependency_refs_for_step(
                    step,
                    created_by_step=created_by_step,
                    nodes_by_alias=nodes_by_alias,
                    steps_by_id=steps_by_id,
                    virtual_step_ids=virtual_step_ids,
                    target_node_id=str(existing.get("id") or ""),
                    extra_dep_keys=[
                        str(existing_fields.get("workflow_source_step") or "").strip(),
                        str(existing_fields.get("source_step") or "").strip(),
                    ],
                ),
                replace_managed=True,
            )
            if existing_fields != original_existing_fields:
                await canvas_tools.update_node(str(existing["id"]), {"input_data": existing_fields})
                await _sync_workflow_dependency_edges(
                    project_id=project_id,
                    node_id=str(existing["id"]),
                    fields=existing_fields,
                )
                existing["input"] = existing_fields
                existing["workflow"] = existing_fields.get("workflow") or canonical_workflow
        if target_surface != "workflow_runtime":
            await _upsert_workflow_runtime_step(
                project_id=project_id,
                template=template,
                instance_id=instance_id,
                step_id=target_id,
                node_type=str(existing.get("type") or step.get("node_type") or "text"),
                title=str(existing.get("title") or step.get("title") or target_id),
                fields=existing.get("input") if isinstance(existing.get("input"), dict) else {},
                status=str(existing.get("status") or "draft"),
                artifacts=[_workflow_runtime_artifact_from_node(existing)],
                node_id=str(existing.get("id") or ""),
                surface=target_surface,
            )
        return {
            "ok": True,
            "project_id": project_id,
            "template_id": template_id,
            "template_name": template.get("name"),
            "protocol": _workflow_protocol_payload(template),
            "instance_id": instance_id,
            "step_id": target_id,
            "node_id": existing.get("id"),
            "node": existing,
            "runtime_step": target_surface == "workflow_runtime",
            "created": False,
        }

    missing_deps = [
        dep for dep in (step.get("depends_on") or [])
        if str(dep or "").strip()
        and str(dep or "").strip() not in virtual_step_ids
        and not _workflow_dependency_nodes(
            str(dep or "").strip(),
            created_by_step=created_by_step,
            nodes_by_alias=nodes_by_alias,
            target_step=step,
        )
    ]
    if missing_deps:
        return {
            "ok": False,
            "error": "Workflow step dependencies are not materialized yet",
            "error_kind": "workflow_step_dependencies_missing",
            "step_id": target_id,
            "missing_step_ids": missing_deps,
            "hint": "先在顶部流程图运行上游步骤，或使用一键执行按顺序运行。",
        }

    input_facts = _input_summary(inputs or {})
    default_fields = template.get("defaults", {}).get("fields")
    if not isinstance(default_fields, dict):
        default_fields = {}
    node_type = str(step.get("node_type") or "text")
    fields = _workflow_strip_template_media_model(_merge_dict(default_fields, step.get("fields") or {}), node_type)
    step_title = str(step.get("title") or fields.get("title") or step.get("id") or "工作流步骤").strip()
    if title and step_index == 0:
        step_title = str(title).strip()
    fields.setdefault("title", step_title)
    fields.setdefault("purpose", step.get("purpose") or step.get("id"))
    fields.setdefault("stage", step.get("id"))
    if node_type in {"image", "video", "audio"}:
        fields.setdefault("prompt_status", "draft")
    if node_type == "image":
        fields.setdefault("aspect_ratio", template.get("defaults", {}).get("aspect_ratio") or "9:16")
        fields.setdefault(
            "resolution",
            template.get("defaults", {}).get("resolution")
            or _workflow_default_image_resolution(fields.get("aspect_ratio")),
        )
        fields.setdefault("quality", template.get("defaults", {}).get("quality") or "high")
    if node_type == "video":
        fields.setdefault("aspect_ratio", template.get("defaults", {}).get("aspect_ratio") or "9:16")
        fields.setdefault("resolution", template.get("defaults", {}).get("resolution") or "720p")
        if template.get("defaults", {}).get("duration_seconds"):
            fields.setdefault("duration_seconds", template["defaults"]["duration_seconds"])
    surface = _workflow_step_surface(step)
    fields["surface"] = surface
    if surface != "workflow_runtime":
        first_dep = next(iter(_workflow_data_dependency_ids(step)), "")
        fields.setdefault("workflow_source_step", str(step.get("source_step") or first_dep).strip())
        fields.setdefault("workflow_source_path", str(step.get("source_path") or "output").strip() or "output")
        fields.setdefault("workflow_generate", node_type in {"image", "video", "audio"})

    dep_refs = _workflow_dependency_refs_for_step(
        step,
        created_by_step=created_by_step,
        nodes_by_alias=nodes_by_alias,
        steps_by_id=steps_by_id,
        virtual_step_ids=virtual_step_ids,
        extra_dep_keys=[
            str(fields.get("workflow_source_step") or "").strip(),
            str(fields.get("source_step") or "").strip(),
        ],
    )
    dep_refs.extend(_workflow_input_reference_refs(inputs or {}, step, fields))
    all_records = [*nodes, *runtime_records]
    runtime_context = _workflow_runtime_context_from_nodes(all_records, template_id=template_id, instance_id=instance_id)
    selector_ref_nodes = _workflow_reference_selector_nodes(
        _workflow_reference_selectors(step),
        nodes=all_records,
        context=runtime_context,
        template_id=template_id,
        instance_id=instance_id,
        target_step=step,
    )
    for dep_node, role in selector_ref_nodes:
        if not _workflow_is_canvas_dependency_record(dep_node):
            continue
        dep_refs.append(_reference_for_dep(dep_node, role))
    fields = _merge_workflow_dependency_refs(fields, dep_refs, replace_managed=True)

    workflow_meta = fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {}
    fields["workflow"] = {
        **workflow_meta,
        "template_id": template_id,
        "template_name": template.get("name"),
        "instance_id": instance_id,
        "step_id": target_id,
        "step_index": step_index + 1,
        "step_status": "draft",
        "depends_on": [str(dep).strip() for dep in (step.get("depends_on") or []) if str(dep).strip()],
        "surface": surface,
        "visibility": step.get("visibility") or ("flow_only" if surface == "workflow_runtime" else "canvas"),
        "primary_skill": step.get("primary_skill") or "",
        "skill_category": step.get("skill_category") or "",
        "acceptance": step.get("acceptance") or "",
        "input_facts": input_facts,
        "protocol": _workflow_protocol_payload(template),
        **_step_workflow_metadata(step),
    }
    if step.get("worker"):
        fields["workflow"]["worker"] = step.get("worker")

    if surface == "workflow_runtime":
        output: Any = None
        if str(step.get("runner") or "").strip() in _WORKFLOW_INPUT_RUNNERS:
            output = {
                "inputs": input_facts,
                "input_values": input_facts,
                "content": json.dumps(input_facts, ensure_ascii=False, default=str),
            }
        record = await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=target_id,
            node_type=node_type,
            title=step_title,
            fields=fields,
            status="draft",
            output=output,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "template_id": template_id,
            "template_name": template.get("name"),
            "protocol": _workflow_protocol_payload(template),
            "instance_id": instance_id,
            "step_id": target_id,
            "node_id": record.get("id"),
            "node": record,
            "runtime_step": True,
            "created": True,
        }

    x, y = _step_position(
        step,
        index=(
            sum(1 for item in steps[:step_index] if _workflow_step_surface(item) != "workflow_runtime")
            if surface != "workflow_runtime"
            else step_index
        ),
        steps=steps,
        origin_x=origin_x,
        origin_y=origin_y,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
    )
    node = await canvas_tools.create_node(
        project_id=project_id,
        node_type=node_type,
        title=step_title,
        position_x=x,
        position_y=y,
        input_data=fields,
        model_config={
            "surface": surface,
            "_ui_creator": "agent",
            "workflow_template_id": template_id,
            "workflow_instance_id": instance_id,
        },
        prompt=str(fields.get("prompt") or "") or None,
    )
    node["input"] = fields
    node["input_json"] = fields
    node["position_x"] = x
    node["position_y"] = y
    await _upsert_workflow_runtime_step(
        project_id=project_id,
        template=template,
        instance_id=instance_id,
        step_id=target_id,
        node_type=node_type,
        title=step_title,
        fields=fields,
        status="draft",
        artifacts=[_workflow_runtime_artifact_from_node(node)],
        node_id=str(node.get("id") or ""),
        surface=surface,
    )
    await _emit_canvas_action(project_id, "create_node", node)

    target_node_id = str(node.get("id") or "")
    for dep in _workflow_data_dependency_ids(step):
        dep_key = str(dep or "").strip()
        if dep_key in virtual_step_ids:
            continue
        for source in _workflow_visible_dependency_nodes(
            dep_key,
            created_by_step=created_by_step,
            nodes_by_alias=nodes_by_alias,
            steps_by_id=steps_by_id,
            target_step=step,
            exclude_node_ids={target_node_id},
        ):
            source_node_id = str(source.get("id") or "")
            if not source_node_id or source_node_id == target_node_id:
                continue
            edge = await canvas_tools.connect_nodes(
                project_id=project_id,
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                label=str(step.get("dependency_role") or ""),
            )
            await _emit_canvas_action(project_id, "add_edge", edge)
    for source, role in selector_ref_nodes:
        if not _workflow_is_canvas_dependency_record(source):
            continue
        source_node_id = str(source.get("id") or "")
        if not source_node_id or source_node_id == target_node_id:
            continue
        edge = await canvas_tools.connect_nodes(
            project_id=project_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            label=role,
        )
        await _emit_canvas_action(project_id, "add_edge", edge)

    return {
        "ok": True,
        "project_id": project_id,
        "template_id": template_id,
        "template_name": template.get("name"),
        "protocol": _workflow_protocol_payload(template),
        "instance_id": instance_id,
        "step_id": target_id,
        "node_id": node.get("id"),
        "node": node,
        "created": True,
    }


async def workflow_materialize_step(
    project_id: str,
    step_id: str,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    title: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    instance_id: str = "",
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    inputs = await _workflow_inputs_with_saved_values(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    template, error = await _workflow_template_from_spec(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
        context=context,
    )
    if error:
        return error
    assert template is not None
    return await _materialize_workflow_step(
        project_id=project_id,
        template=template,
        step_id=step_id,
        inputs=inputs,
        instance_id=instance_id,
        title=title,
        origin_x=origin_x,
        origin_y=origin_y,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
    )


def _resolve_workflow_target_steps(template: dict[str, Any], step_id: str) -> list[dict[str, Any]]:
    target = str(step_id or "").strip()
    if not target:
        return []
    steps = [step for step in template.get("steps") or [] if isinstance(step, dict)]
    exact = [step for step in steps if str(step.get("id") or "").strip() == target]
    if exact:
        step = exact[0]
        if str(step.get("role") or "").strip() == "repeat_group":
            grouped = [
                item for item in steps
                if str(item.get("repeat_group_id") or "").strip() == target
            ]
            return grouped or exact
        return _workflow_logical_target_steps(template, step)
    return [
        step for step in steps
        if str(step.get("template_step_id") or "").strip() == target
        or str(step.get("repeat_group_id") or "").strip() == target
        or str(step.get("source_node_id") or "").strip() == target
    ]


async def _workflow_records_for_instance(
    project_id: str,
    *,
    template_id: str,
    instance_id: str,
) -> list[dict[str, Any]]:
    nodes = [
        node
        for node in await canvas_tools.list_nodes(project_id)
        if _workflow_is_canvas_dependency_record(node)
    ]
    runtime_records = await _workflow_runtime_records_from_project(
        project_id,
        template_id=template_id,
        instance_id=instance_id,
    )
    return [*nodes, *runtime_records]


def _workflow_records_for_prompt_context(
    records: list[dict[str, Any]],
    *,
    template_id: str,
    instance_id: str,
    target_step_id: str,
    target_step: dict[str, Any],
    target_record: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    scoped_records: list[dict[str, Any]] = []
    for record in records:
        workflow = _workflow_metadata_from_node(record)
        if template_id and str(workflow.get("template_id") or "").strip() != template_id:
            continue
        if instance_id and str(workflow.get("instance_id") or "").strip() != instance_id:
            continue
        if str(workflow.get("step_id") or "").strip() == target_step_id:
            continue
        scoped_records.append(record)

    created_by_step = _workflow_step_nodes_by_id(scoped_records, template_id, instance_id)
    nodes_by_alias = _workflow_step_nodes_by_alias(scoped_records, template_id, instance_id)
    prompt_dependency_keys: list[str] = []
    prompt_template = str(target_step.get("prompt_template") or "")
    builtin_context_keys = {
        "inputs",
        "input_facts",
        "instance",
        "json",
        "target",
        "target_node",
        "previous",
        "previous_segment",
        "upstream_nodes",
        "steps",
        "nodes",
        "outputs",
    }
    for match in re.finditer(r"\{\{\s*([^{}]+?)\s*\}\}", prompt_template):
        expression = str(match.group(1) or "").strip()
        root_key = expression.split(".", 1)[0].split("[", 1)[0].strip()
        if root_key and root_key not in builtin_context_keys:
            prompt_dependency_keys.append(root_key)
    dependency_keys = _unique_nonempty_strings([
        *(target_step.get("depends_on") or []),
        *prompt_dependency_keys,
        *(spec.get("ref") for spec in _workflow_context_ref_specs(target_step)),
        *(
            selector.get("source_step")
            for selector in _workflow_reference_selectors(target_step)
            if isinstance(selector, dict)
        ),
        *(
            selector.get("from_group")
            for selector in _workflow_reference_selectors(target_step)
            if isinstance(selector, dict)
        ),
    ])

    candidates: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    for dependency_key in dependency_keys:
        for record in _workflow_dependency_nodes(
            dependency_key,
            created_by_step=created_by_step,
            nodes_by_alias=nodes_by_alias,
            target_step=target_step,
        ):
            record_id = str(record.get("id") or "").strip()
            marker = record_id or str(id(record))
            if marker in seen_record_ids:
                continue
            seen_record_ids.add(marker)
            candidates.append(record)

    # A canvas record and its workflow-runtime shadow represent the same step.
    # Prefer the actual canvas record, then the newest completed candidate.
    selected_by_step: dict[str, dict[str, Any]] = {}

    def preference(record: dict[str, Any]) -> tuple[int, int, str]:
        record_id = str(record.get("id") or "")
        is_canvas = _workflow_is_canvas_dependency_record(record) and not record_id.startswith("workflow-runtime:")
        is_completed = str(record.get("status") or "") == "completed"
        return (int(is_canvas), int(is_completed), str(record.get("updated_at") or ""))

    for record in candidates:
        workflow = _workflow_metadata_from_node(record)
        identity = str(workflow.get("step_id") or workflow.get("template_step_id") or record.get("id") or "").strip()
        if not identity:
            continue
        current = selected_by_step.get(identity)
        if current is None or preference(record) > preference(current):
            selected_by_step[identity] = record
    return list(selected_by_step.values())


def _workflow_record_matches_prompt_source(record: dict[str, Any], marker: str) -> bool:
    wanted = _selector_key(marker)
    if not wanted:
        return False
    workflow = _workflow_metadata_from_node(record)
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    tokens = [
        workflow.get("step_id"),
        workflow.get("template_step_id"),
        workflow.get("source_node_id"),
        fields.get("purpose"),
        fields.get("stage"),
        record.get("title"),
    ]
    return any(_selector_key(value) == wanted for value in tokens if value not in (None, ""))


def _workflow_output_payload_from_record(record: dict[str, Any], node_universal: Any) -> dict[str, Any]:
    candidates: list[Any] = [
        record.get("output"),
        record.get("outputs"),
    ]
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    candidates.extend([fields.get("content"), fields.get("prompt"), record.get("content"), record.get("prompt")])
    for candidate in candidates:
        if candidate in (None, "", [], {}):
            continue
        structured = node_universal._workflow_structured_value(candidate)
        if isinstance(structured, dict):
            return structured
        if isinstance(candidate, str) and candidate.strip():
            return {"content": candidate.strip()}
    return {}


def _workflow_int_value(value: Any) -> int | None:
    if value in (None, "", [], {}):
        return None
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _workflow_segment_duration_from_scope(scope: dict[str, Any]) -> int | None:
    direct = _workflow_int_value(scope.get("duration_seconds") or scope.get("duration"))
    if direct:
        return direct
    start = _workflow_int_value(scope.get("start_second"))
    end = _workflow_int_value(scope.get("end_second"))
    if start is not None and end is not None and end > start:
        return end - start
    return None


def _workflow_direct_video_prompt_from_upstream(
    *,
    workflow: dict[str, Any],
    fields: dict[str, Any],
    upstream_nodes: list[dict[str, Any]],
    node_universal: Any,
) -> dict[str, Any] | None:
    for upstream in upstream_nodes:
        if not (
            _workflow_record_matches_prompt_source(upstream, "video_prompt")
            or _workflow_record_matches_prompt_source(upstream, "videoPrompt")
        ):
            continue
        payload = _workflow_output_payload_from_record(upstream, node_universal)
        prompt = str(
            payload.get("prompt")
            or payload.get("video_prompt")
            or payload.get("full_text")
            or payload.get("content")
            or ""
        ).strip()
        if not prompt:
            continue
        instance_scope = workflow.get("instance_scope") if isinstance(workflow.get("instance_scope"), dict) else {}
        input_facts = workflow.get("input_facts") if isinstance(workflow.get("input_facts"), dict) else {}
        duration = (
            _workflow_segment_duration_from_scope(instance_scope)
            or _workflow_int_value(payload.get("duration_seconds") or payload.get("duration"))
            or _workflow_int_value(fields.get("duration_seconds") or fields.get("duration"))
            or _workflow_int_value(input_facts.get("durationSeconds") or input_facts.get("duration_seconds"))
        )
        aspect_ratio = str(
            payload.get("aspect_ratio")
            or payload.get("ratio")
            or fields.get("aspect_ratio")
            or input_facts.get("aspectRatio")
            or input_facts.get("aspect_ratio")
            or ""
        ).strip()
        suggested_fields: dict[str, Any] = {
            "prompt": prompt,
            "prompt_status": "completed",
            "production_path": fields.get("production_path") or "text_to_video",
        }
        if duration:
            suggested_fields["duration_seconds"] = duration
        if aspect_ratio:
            suggested_fields["aspect_ratio"] = aspect_ratio
        for key in ("negative_prompt", "style", "resolution", "quality"):
            if payload.get(key) not in (None, "", [], {}):
                suggested_fields[key] = payload[key]
        return suggested_fields
    return None


async def _run_runtime_llm_step(
    *,
    project_id: str,
    template: dict[str, Any],
    step: dict[str, Any],
    record: dict[str, Any],
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    from app.mcp_tools import node_universal

    fields = dict(record.get("input") if isinstance(record.get("input"), dict) else {})
    stored_workflow = dict(fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {})
    workflow = {
        **stored_workflow,
        "template_id": str(template.get("id") or stored_workflow.get("template_id") or ""),
        "template_name": template.get("name") or stored_workflow.get("template_name"),
        "step_id": str(step.get("id") or stored_workflow.get("step_id") or ""),
        "depends_on": [str(dep).strip() for dep in (step.get("depends_on") or []) if str(dep).strip()],
        "surface": _workflow_step_surface(step),
        "visibility": step.get("visibility") or stored_workflow.get("visibility") or "flow_only",
        "primary_skill": step.get("primary_skill") or "",
        "skill_category": step.get("skill_category") or "",
        "acceptance": step.get("acceptance") or "",
        "input_facts": _input_summary(inputs or {}),
        "protocol": _workflow_protocol_payload(template),
        **_step_workflow_metadata(step),
    }
    fields["workflow"] = workflow
    instance_id = str(workflow.get("instance_id") or record.get("instance_id") or "").strip()
    step_id = str(workflow.get("step_id") or record.get("step_id") or step.get("id") or "").strip()
    title = str(record.get("title") or step.get("title") or step_id or "Workflow Runtime Step")
    runner = str(step.get("runner") or workflow.get("runner") or "").strip()
    if runner in _WORKFLOW_INPUT_RUNNERS:
        output = _input_summary(inputs or {})
        updated = await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=step_id,
            node_type=str(record.get("type") or "text"),
            title=title,
            fields=fields,
            status="completed",
            output=output,
            increment_run=True,
        )
        return {"ok": True, "runtime_step": True, "node": updated, "node_id": updated.get("id"), "run_result": output}

    await _upsert_workflow_runtime_step(
        project_id=project_id,
        template=template,
        instance_id=instance_id,
        step_id=step_id,
        node_type=str(record.get("type") or "text"),
        title=title,
        fields=fields,
        status="running",
        output=record.get("output"),
        increment_run=True,
    )
    if runner == "workflow_plugin":
        from app.services import workflow_plugins

        try:
            plugin_result = await workflow_plugins.run_plugin_step(
                project_id=project_id,
                template=template,
                step=step,
                record=record,
                inputs=inputs,
            )
        except Exception as exc:
            failed = await _upsert_workflow_runtime_step(
                project_id=project_id,
                template=template,
                instance_id=instance_id,
                step_id=step_id,
                node_type=str(record.get("type") or "text"),
                title=title,
                fields=fields,
                status="failed",
                output=record.get("output"),
                error=str(exc)[:500],
            )
            return {"ok": False, "runtime_step": True, "node": failed, "node_id": failed.get("id"), "error": str(exc), "error_kind": "workflow_plugin_error"}
        ok = bool(plugin_result.get("ok"))
        output = plugin_result.get("run_result") if isinstance(plugin_result.get("run_result"), dict) else plugin_result
        updated = await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=step_id,
            node_type=str(record.get("type") or "text"),
            title=title,
            fields=fields,
            status="completed" if ok else "failed",
            output=output,
            error=str(plugin_result.get("error") or "")[:500],
        )
        return {
            **plugin_result,
            "runtime_step": True,
            "node": updated,
            "node_id": updated.get("id"),
        }
    records = await _workflow_records_for_instance(project_id, template_id=str(template.get("id") or ""), instance_id=instance_id)
    template_id = str(template.get("id") or "")
    steps_by_id = {
        str(item.get("id") or "").strip(): item
        for item in (template.get("steps") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    runtime_context = _workflow_runtime_context_from_nodes(
        records,
        template_id=template_id,
        instance_id=instance_id,
    )
    vision_nodes, missing_vision_refs = _workflow_vision_context_nodes(
        step,
        records=records,
        context=runtime_context,
        template_id=template_id,
        instance_id=instance_id,
        steps_by_id=steps_by_id,
    )
    mention_candidates = build_reference_mention_candidates([
        {
            "ref": _public_ref(node),
            "label": node.get("title"),
            "source": "node",
        }
        for node in vision_nodes
    ]) if _workflow_step_writes_media_prompt(template, step) else []
    mention_by_ref = {
        str(candidate.get("ref") or "").strip(): candidate
        for candidate in mention_candidates
    }
    vision_context: list[dict[str, Any]] = []
    vision_image_urls: list[str] = []
    vision_errors: list[str] = [f"未找到图片节点: {ref}" for ref in missing_vision_refs]
    for index, vision_node in enumerate(vision_nodes, start=1):
        ref = _public_ref(vision_node)
        image_url, warning = await node_universal._llm_image_url_from_reference(project_id, ref)
        vision_context.append({
            "index": index,
            "ref": ref,
            "title": vision_node.get("title"),
            "mention": (mention_by_ref.get(ref) or {}).get("mention"),
            "workflow_step_id": _workflow_metadata_from_node(vision_node).get("step_id"),
        })
        if image_url:
            vision_image_urls.append(image_url)
        else:
            vision_errors.append(warning or f"图片无法发送: {ref}")
    if vision_errors:
        error_message = "必须查看的参考图不可用: " + "; ".join(_unique_nonempty_strings(vision_errors))
        failed = await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=step_id,
            node_type=str(record.get("type") or "text"),
            title=title,
            fields=fields,
            status="failed",
            output=record.get("output"),
            error=error_message[:500],
        )
        return {
            "ok": False,
            "runtime_step": True,
            "node": failed,
            "node_id": failed.get("id"),
            "error": error_message,
            "error_kind": "workflow_vision_context_unavailable",
        }
    upstream_nodes = _workflow_records_for_prompt_context(
        records,
        template_id=template_id,
        instance_id=instance_id,
        target_step_id=step_id,
        target_step=step,
        target_record=record,
    )
    compact_upstream = [node_universal._compact_workflow_text_node(item) for item in upstream_nodes]
    target = node_universal._compact_workflow_text_node(record, include_output=False)
    prompt_runtime = node_universal._workflow_render_prompt_template(
        workflow.get("prompt_template"),
        workflow=workflow,
        target=target,
        upstream_nodes=compact_upstream,
    )
    skill_payload = await node_universal._workflow_runtime_skill_payload(workflow, prompt_runtime)
    structured_contract = structured_output_contract(workflow)
    structured_instructions = structured_output_instructions(workflow)
    system = (
        "You are a one-shot workflow runtime step runner. "
        "Generate the final output for exactly one workflow step from the provided step spec, prompt template, inputs, and upstream outputs. "
        "Use rendered_prompt_template as the execution contract when it is present. "
        "Use skill.content only when no prompt template is available. "
        "Return only the content to store as this step output."
    )
    if structured_instructions:
        system = f"{system}\n\n{structured_instructions}"
    mention_instruction = reference_mention_instruction(mention_candidates)
    if mention_instruction:
        system = f"{system}\n\n{mention_instruction}"
    include_upstream_payload = bool(
        not str(prompt_runtime.get("rendered_prompt_template") or "").strip()
        or prompt_runtime.get("unresolved_template_paths")
    )
    message = json.dumps(
        {
            "target_step": target,
            "workflow": node_universal._compact_workflow_llm_contract(workflow),
            "prompt_template": prompt_runtime["prompt_template"],
            "rendered_prompt_template": prompt_runtime["rendered_prompt_template"],
            "unresolved_template_paths": prompt_runtime["unresolved_template_paths"],
            "output_mode": workflow.get("output_mode"),
            "output_schema": workflow.get("output_schema"),
            "structured_output_contract": structured_contract,
            "acceptance": workflow.get("acceptance"),
            "input_facts": workflow.get("input_facts"),
            "skill": skill_payload,
            "upstream_steps": compact_upstream if include_upstream_payload else [],
            "vision_context_images": vision_context,
            "allowed_reference_mentions": mention_candidates,
        },
        ensure_ascii=False,
        default=str,
    )
    task_type = node_universal._workflow_text_task_type(workflow, fields)
    started_at = _utc_now_iso()
    dump_run_id = f"workflow_runtime_{node_universal.new_run_id()}"
    node_universal.dump_llm_request(
        project_id,
        dump_run_id,
        0,
        system,
        [{"role": "user", "content": message}],
        [],
        user_message=f"workflow runtime step {step_id}",
    )
    request_diagnostics = {
        "run_id": dump_run_id,
        "task_type": task_type,
        "prompt_dump_run_id": dump_run_id,
        "started_at": started_at,
        "request_message_chars": len(message),
        "upstream_record_count": len(compact_upstream),
        "serialized_upstream_record_count": len(compact_upstream) if include_upstream_payload else 0,
        "vision_image_count": len(vision_image_urls),
        "vision_image_refs": [item.get("ref") for item in vision_context],
        "vision_image_mentions": [item.get("mention") for item in mention_candidates],
    }
    try:
        llm_result = await node_universal._call_workflow_text_llm(
            task_type=task_type,
            system=system,
            message=message,
            project_id=project_id,
            image_urls=vision_image_urls,
            image_labels=mention_candidates,
        )
    except Exception as exc:
        failed_fields = dict(fields)
        failed_fields["workflow"] = node_universal._workflow_text_run_log(
            workflow,
            {
                **request_diagnostics,
                "status": "failed",
                "completed_at": _utc_now_iso(),
                "error": str(exc)[:500],
            },
        )
        failed = await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=step_id,
            node_type=str(record.get("type") or "text"),
            title=title,
            fields=failed_fields,
            status="failed",
            output=record.get("output"),
            error=str(exc)[:500],
        )
        return {"ok": False, "runtime_step": True, "node": failed, "node_id": failed.get("id"), "error": str(exc)}
    content = node_universal._strip_llm_fences(str(llm_result.get("content") or ""))
    if not content:
        failed_fields = dict(fields)
        failed_fields["workflow"] = node_universal._workflow_text_run_log(
            workflow,
            {
                **request_diagnostics,
                "status": "failed",
                "model": llm_result.get("model"),
                "usage": llm_result.get("usage"),
                "usage_total_tokens": node_universal._workflow_text_usage_total(llm_result.get("usage")),
                "completed_at": _utc_now_iso(),
                "error": "empty_llm_output",
            },
        )
        failed = await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=step_id,
            node_type=str(record.get("type") or "text"),
            title=title,
            fields=failed_fields,
            status="failed",
            output=record.get("output"),
            error="empty_llm_output",
        )
        return {"ok": False, "runtime_step": True, "node": failed, "node_id": failed.get("id"), "error": "workflow runtime step returned empty content", "error_kind": "empty_llm_output"}
    if mention_candidates:
        _matched_mentions, unknown_mentions, missing_mentions = parse_reference_mentions(
            content,
            mention_candidates,
        )
        if unknown_mentions or missing_mentions:
            details: list[str] = []
            if unknown_mentions:
                details.append("未知标签: " + "、".join(unknown_mentions))
            if missing_mentions:
                details.append("缺少标签: " + "、".join(missing_mentions))
            error_message = "媒体提示词参考图标签校验失败；" + "；".join(details)
            failed_fields = dict(fields)
            failed_fields["workflow"] = node_universal._workflow_text_run_log(
                workflow,
                {
                    **request_diagnostics,
                    "status": "failed",
                    "model": llm_result.get("model"),
                    "usage": llm_result.get("usage"),
                    "usage_total_tokens": node_universal._workflow_text_usage_total(llm_result.get("usage")),
                    "completed_at": _utc_now_iso(),
                    "error": error_message[:500],
                },
            )
            failed = await _upsert_workflow_runtime_step(
                project_id=project_id,
                template=template,
                instance_id=instance_id,
                step_id=step_id,
                node_type=str(record.get("type") or "text"),
                title=title,
                fields=failed_fields,
                status="failed",
                output=record.get("output"),
                error=error_message[:500],
            )
            return {
                "ok": False,
                "runtime_step": True,
                "node": failed,
                "node_id": failed.get("id"),
                "error": error_message,
                "error_kind": "workflow_reference_mentions_invalid",
            }
    output_value: Any = content
    if structured_contract:
        try:
            output_value = parse_structured_output(content, workflow)
        except WorkflowStructuredOutputError as exc:
            failed_fields = dict(fields)
            failed_fields["workflow"] = node_universal._workflow_text_run_log(
                workflow,
                {
                    **request_diagnostics,
                    "status": "failed",
                    "model": llm_result.get("model"),
                    "usage": llm_result.get("usage"),
                    "usage_total_tokens": node_universal._workflow_text_usage_total(llm_result.get("usage")),
                    "completed_at": _utc_now_iso(),
                    "error": str(exc)[:500],
                },
            )
            failed = await _upsert_workflow_runtime_step(
                project_id=project_id,
                template=template,
                instance_id=instance_id,
                step_id=step_id,
                node_type=str(record.get("type") or "text"),
                title=title,
                fields=failed_fields,
                status="failed",
                output=record.get("output"),
                error=str(exc)[:500],
            )
            return {
                "ok": False,
                "runtime_step": True,
                "node": failed,
                "node_id": failed.get("id"),
                "error": f"workflow structured output invalid: {exc}",
                "error_kind": "structured_output_invalid",
            }

    updated_fields = dict(fields)
    updated_fields["workflow"] = node_universal._workflow_text_run_log(
        workflow,
        {
            **request_diagnostics,
            "status": "completed",
            "model": llm_result.get("model"),
            "usage": llm_result.get("usage"),
            "usage_total_tokens": node_universal._workflow_text_usage_total(llm_result.get("usage")),
            "completed_at": _utc_now_iso(),
            "content_chars": len(content),
        },
    )
    updated_fields["content"] = content
    updated_fields["prompt_status"] = "completed"
    updated = await _upsert_workflow_runtime_step(
        project_id=project_id,
        template=template,
        instance_id=instance_id,
        step_id=step_id,
        node_type=str(record.get("type") or "text"),
        title=title,
        fields=updated_fields,
        status="completed",
        output=output_value,
    )
    return {
        "ok": True,
        "runtime_step": True,
        "node": updated,
        "node_id": updated.get("id"),
        "run_result": {
            "type": "text",
            "content": content,
            "structured_output": output_value if structured_contract else None,
            "workflow_runtime_runner": "one_shot_llm",
            "llm_task_type": task_type,
            "model": llm_result.get("model"),
            "usage": llm_result.get("usage"),
        },
    }


async def _prepare_visible_workflow_node_for_run(
    *,
    project_id: str,
    template: dict[str, Any],
    step: dict[str, Any],
    node: dict[str, Any],
) -> dict[str, Any]:
    node_type = str(node.get("type") or step.get("node_type") or "")
    fields = dict(node.get("input") if isinstance(node.get("input"), dict) else {})
    workflow = dict(fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {})
    instance_id = str(workflow.get("instance_id") or "").strip()
    step_id = str(workflow.get("step_id") or step.get("id") or "").strip()
    runner = str(workflow.get("runner") or step.get("runner") or "").strip()
    kind = str(workflow.get("kind") or step.get("kind") or "").strip().lower().replace("-", "_")
    is_canvas_output_step = runner == "workflow_canvas_output" or kind == "canvas_text"
    if node_type == "text" and not is_canvas_output_step:
        node["_workflow_should_generate"] = True
        return node
    source_step = _workflow_effective_source_step(fields, step)
    source_path = _workflow_effective_source_path(fields, step)
    generate_media = fields.get("workflow_generate")
    if not isinstance(generate_media, bool):
        generate_media = fields.get("generate")
    if not isinstance(generate_media, bool):
        generate_media = node_type in {"image", "video", "audio"}

    records = await _workflow_records_for_instance(project_id, template_id=str(template.get("id") or ""), instance_id=instance_id)
    template_steps = [item for item in template.get("steps") or [] if isinstance(item, dict)]
    steps_by_id = {
        str(item.get("id") or "").strip(): item
        for item in template_steps
        if str(item.get("id") or "").strip()
    }
    input_facts = workflow.get("input_facts") if isinstance(workflow.get("input_facts"), dict) else {}
    virtual_step_ids = _virtual_workflow_step_ids(template_steps, input_facts)
    created_by_step = _workflow_step_nodes_by_id(records, str(template.get("id") or ""), instance_id)
    nodes_by_alias = _workflow_step_nodes_by_alias(records, str(template.get("id") or ""), instance_id)
    context = _workflow_runtime_context_from_nodes(
        records,
        template_id=str(template.get("id") or ""),
        instance_id=instance_id,
    )
    scoped_source_records = _workflow_dependency_nodes(
        source_step,
        created_by_step=created_by_step,
        nodes_by_alias=nodes_by_alias,
        target_step=step,
    ) if source_step else []
    source_payload = (
        _workflow_node_context_payload(scoped_source_records[0])
        if scoped_source_records
        else _workflow_context_get(context, source_step)
    )
    source_values = _workflow_values_at_path(source_payload, source_path) if source_payload is not None else []
    if not source_values and source_path != "output" and source_payload is not None:
        source_values = _workflow_values_at_path(source_payload, "output")
    source_value = source_values[0] if source_values else None

    def _text_from_value(value: Any) -> str:
        cleaned = _workflow_runtime_clean_output_value(value, drop_internal_keys=True)
        if cleaned in (None, "", [], {}):
            return ""
        if isinstance(cleaned, str):
            return cleaned.strip()
        if isinstance(cleaned, (int, float, bool)):
            return str(cleaned)
        if isinstance(cleaned, dict):
            for key in (
                "prompt",
                "video_prompt",
                "image_prompt",
                "audio_prompt",
                "content",
                "full_text",
                "story_text",
                "script",
                "text",
                "description",
            ):
                text = str(cleaned.get(key) or "").strip()
                if text:
                    return text
            return workflow_runtime_output_preview({"output": cleaned}, workflow_override=workflow)
        if isinstance(cleaned, list):
            return "\n\n".join(_text_from_value(item) for item in cleaned if _text_from_value(item)).strip()
        return str(cleaned).strip()

    mapped_text = _text_from_value(source_value)
    if mapped_text:
        if node_type == "text":
            fields["content"] = mapped_text
        elif node_type in {"image", "video", "audio"}:
            fields["prompt"] = mapped_text
            node["prompt"] = mapped_text
    if source_step:
        fields["workflow_source_step"] = source_step
    fields["workflow_source_path"] = source_path
    fields["workflow_generate"] = bool(generate_media)
    dep_refs = [
        *_workflow_dependency_refs_for_step(
            step,
            created_by_step=created_by_step,
            nodes_by_alias=nodes_by_alias,
            steps_by_id=steps_by_id,
            virtual_step_ids=virtual_step_ids,
            target_node_id=str(node.get("id") or ""),
            extra_dep_keys=[source_step],
        ),
        *_workflow_input_reference_refs(input_facts, step, fields),
    ]
    selector_ref_nodes = _workflow_reference_selector_nodes(
        _workflow_reference_selectors(step, workflow),
        nodes=records,
        context=context,
        template_id=str(template.get("id") or ""),
        instance_id=instance_id,
        target_step=step,
    )
    for dep_node, role in selector_ref_nodes:
        if _workflow_is_canvas_dependency_record(dep_node):
            dep_refs.append(_reference_for_dep(dep_node, role))
    fields = _merge_workflow_dependency_refs(fields, dep_refs, replace_managed=True)
    if node_type == "text":
        fields["prompt_status"] = "completed"
        await canvas_tools.update_node(str(node["id"]), {"input_data": fields})
        await _sync_workflow_dependency_edges(
            project_id=project_id,
            node_id=str(node["id"]),
            fields=fields,
        )
        node["input"] = fields
        node["_workflow_should_generate"] = False
        return node

    fields["prompt_status"] = "completed" if mapped_text else "missing_source"
    await canvas_tools.update_node(str(node["id"]), {"input_data": fields, "prompt": str(fields.get("prompt") or "") or None})
    await _sync_workflow_dependency_edges(
        project_id=project_id,
        node_id=str(node["id"]),
        fields=fields,
    )
    node["input"] = fields
    node["prompt"] = str(fields.get("prompt") or "")
    node["_workflow_should_generate"] = bool(generate_media)
    return node


@register(
    "workflow.run_step",
    description="运行工作流中的指定步骤；会按依赖物化缺失节点，并用 inputs 填写运行输入。",
    tags=["workflow", "write"],
    search_hint=(
        "workflow run specific step start step fill inputs execute one step instance "
        "工作流 运行 指定步骤 开始 单步 输入 实例"
    ),
    usage_hints=[
        "已经有 workflow template_id 或 artifact_ref 后使用；step_id 指定要运行的步骤。",
        "inputs 是本次流程输入，例如 plot、durationSeconds、segmentCount；会保存到项目 workflow_input_values。",
        "instance_id 用于继续某个并行运行胶囊；不传则使用最近实例或创建新实例。",
        "依赖未完成时会返回缺失依赖，不会绕过拓扑顺序。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "step_id": {"type": "string"},
            "template_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "workflow": {"type": "object", "additionalProperties": True},
            "title": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "instance_id": {"type": "string"},
            "origin_x": {"type": "number"},
            "origin_y": {"type": "number"},
            "spacing_x": {"type": "number"},
            "spacing_y": {"type": "number"},
        },
        "required": ["step_id"],
    },
)
async def workflow_run_step(
    project_id: str,
    step_id: str,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    title: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    ui_overrides: dict[str, Any] | None = None,
    instance_id: str = "",
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
    persist_active: bool = True,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    inputs = await _workflow_inputs_with_saved_values(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    template, error = await _workflow_template_from_spec(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
        context=context,
    )
    if error:
        return error
    assert template is not None
    mismatch_error = await _workflow_instance_template_mismatch_error(
        project_id=project_id,
        instance_id=instance_id,
        template_id=str(template.get("id") or template_id or ""),
    )
    if mismatch_error:
        return mismatch_error
    inputs = _workflow_effective_inputs(template, inputs)
    authorization_error = await _authorize_workflow_for_run(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
    )
    if authorization_error:
        return authorization_error
    if persist_active:
        await _workflow_runtime_clear_pause_state(project_id, instance_id)
    if persist_active:
        await _persist_active_workflow_for_run(
            project_id=project_id,
            template=template,
            template_id=template_id,
            workflow=workflow,
            artifact_ref=artifact_ref,
            title=title,
        )
    await _persist_workflow_input_values(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    target_steps = _resolve_workflow_target_steps(template, step_id)
    if not target_steps:
        server_context = await _workflow_runtime_context_from_project(
            project_id,
            template_id=str(template.get("id") or template_id or ""),
            instance_id=instance_id,
        )
        if server_context:
            merged_context = _merge_dict(context if isinstance(context, dict) else {}, server_context)
            expanded_template, expanded_error = await _workflow_template_from_spec(
                project_id=project_id,
                template_id=template_id,
                workflow=workflow,
                artifact_ref=artifact_ref,
                inputs=inputs,
                context=merged_context,
            )
            if expanded_template is not None and not expanded_error:
                expanded_targets = _resolve_workflow_target_steps(expanded_template, step_id)
                if expanded_targets:
                    template = expanded_template
                    target_steps = expanded_targets
    if not target_steps:
        return {
            "ok": False,
            "error": f"Workflow step not found or not expanded yet: {step_id}",
            "error_kind": "workflow_step_not_found",
            "step_id": step_id,
            "available_step_ids": [str(step.get("id") or "") for step in template.get("steps") or [] if isinstance(step, dict)],
            "deferred_groups": deepcopy(template.get("deferred_groups") or []),
            "hint": "先运行上游规划步骤；它的结构化输出会作为 context 展开人物、场景或分段集合。",
        }

    if persist_active:
        await _prepare_workflow_runtime_manual_rerun(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            target_steps=target_steps,
            requested_step_id=step_id,
        )

    from app.mcp_tools import node_universal

    instance = str(instance_id or "").strip()
    step_results: list[dict[str, Any]] = []
    first_materialized: dict[str, Any] | None = None
    for target_step in target_steps:
        materialized = await _materialize_workflow_step(
            project_id=project_id,
            template=template,
            step_id=str(target_step.get("id") or ""),
            inputs=inputs,
            instance_id=instance,
            title=title,
            origin_x=origin_x,
            origin_y=origin_y,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
        )
        if materialized.get("ok") is False:
            return {
                **materialized,
                "partial_results": step_results,
            }
        if not first_materialized:
            first_materialized = materialized
        if materialized.get("instance_id"):
            instance = str(materialized.get("instance_id") or instance)
        if materialized.get("virtual"):
            step_results.append(materialized)
            continue

        if materialized.get("runtime_step"):
            runtime_result = await _run_runtime_llm_step(
                project_id=project_id,
                template=template,
                step=target_step,
                record=materialized.get("node") if isinstance(materialized.get("node"), dict) else {},
                inputs=inputs,
            )
            step_results.append({
                **materialized,
                **runtime_result,
                "created": materialized.get("created"),
            })
            continue

        node_id = str(materialized.get("node_id") or "")
        node = materialized.get("node") if isinstance(materialized.get("node"), dict) else {}
        node = await _prepare_visible_workflow_node_for_run(
            project_id=project_id,
            template=template,
            step=target_step,
            node=node,
        )
        should_generate = bool(node.pop("_workflow_should_generate", True))
        try:
            node = await _hydrate_workflow_node_with_inputs(node_id, inputs)
        except Exception as exc:
            runtime_payload = _workflow_runtime_public_payload(
                await _read_project_state(project_id),
                template_id=str(template.get("id") or template_id or ""),
                instance_id=instance,
            )
            step_results.append({
                **materialized,
                "ok": False,
                "error": str(exc),
                "error_kind": "workflow_step_canvas_node_missing",
                "runtime": runtime_payload,
            })
            continue
        step_workflow = node.get("input", {}).get("workflow") if isinstance(node.get("input"), dict) else {}
        if not isinstance(step_workflow, dict):
            step_workflow = {}

        node_type = str(node.get("type") or "")
        if node_type in {"image", "video", "audio"} and node_id:
            node_fields = dict(node.get("input") if isinstance(node.get("input"), dict) else {})
            if "model" in node_fields:
                node_fields.pop("model", None)
                await canvas_tools.update_node(node_id, {"input_data": node_fields})
                node["input"] = node_fields
                node["input_json"] = node_fields
        action = "render" if node_type == "image" else "force"
        manual_media_generation = (
            target_step.get("manual_only") is True
            and node_type in {"image", "video", "audio"}
        )
        if manual_media_generation:
            prepared_status = "completed" if media_history.has_media_output(node.get("output")) else "idle"
            run_result = {
                "ok": True,
                "type": node_type,
                "status": "awaiting_manual_generation",
                "manual_generation_pending": True,
                "prompt": str(node.get("prompt") or ""),
                "node_id": node_id,
            }
            await canvas_tools.update_node(node_id, {"status": prepared_status, "error_message": None})
            await _set_workflow_step_runtime(
                project_id=project_id,
                node_id=node_id,
                inputs=inputs,
                status="completed",
                result=run_result,
                template=template,
                node_status=prepared_status,
            )
            step_results.append({
                **materialized,
                "ok": True,
                "run_result": run_result,
                "awaiting_manual_generation": True,
                "error": None,
                "error_kind": None,
            })
            continue
        if not should_generate:
            fields = node.get("input") if isinstance(node.get("input"), dict) else {}
            canvas_output = _workflow_canvas_output_value(fields, node, node_type)
            run_result = {
                "ok": True,
                "type": node_type,
                "status": "completed",
                **canvas_output,
                "workflow_canvas_output": True,
            }
            await canvas_tools.update_node(node_id, {"status": "completed", "output_data": canvas_output})
            await _set_workflow_step_runtime(
                project_id=project_id,
                node_id=node_id,
                inputs=inputs,
                status="completed",
                result=run_result,
                template=template,
            )
            step_results.append({
                **materialized,
                "ok": True,
                "run_result": run_result,
                "error": None,
                "error_kind": None,
            })
            continue
        await _set_workflow_step_runtime(
            project_id=project_id,
            node_id=node_id,
            inputs=inputs,
            status="running",
            template=template,
        )
        runner = str(step_workflow.get("runner") or target_step.get("runner") or "").strip()
        if runner == "workflow_plugin":
            from app.services import workflow_plugins

            try:
                plugin_result = await workflow_plugins.run_plugin_step(
                    project_id=project_id,
                    template=template,
                    step=target_step,
                    record=node,
                    inputs=inputs,
                )
            except Exception as exc:
                plugin_result = {
                    "ok": False,
                    "runtime_step": False,
                    "error": str(exc),
                    "error_kind": "workflow_plugin_error",
                    "run_result": {"status": "failed", "error": str(exc), "error_kind": "workflow_plugin_error"},
                }
            ok = bool(plugin_result.get("ok"))
            run_result = plugin_result.get("run_result") if isinstance(plugin_result.get("run_result"), dict) else plugin_result
            if isinstance(run_result, dict):
                await canvas_tools.update_node(node_id, {"output_data": run_result})
            await _set_workflow_step_runtime(
                project_id=project_id,
                node_id=node_id,
                inputs=inputs,
                status="completed" if ok else "failed",
                result=run_result if isinstance(run_result, dict) else plugin_result,
                template=template,
            )
            step_results.append({
                **materialized,
                "ok": bool(ok),
                "run_result": run_result,
                "error": plugin_result.get("error"),
                "error_kind": plugin_result.get("error_kind"),
            })
            continue
        try:
            extra_fields = _workflow_ui_node_run_extra_fields(target_step, ui_overrides)
            run_result = await node_universal.node_run(
                project_id=project_id,
                node_id=node_id,
                action=action,
                extra_fields=extra_fields or None,
                hidden_extra_field_keys=list(extra_fields.keys()) or None,
            )
        except Exception as exc:
            run_result = {
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "error_kind": exc.__class__.__name__,
            }
        ok = not (isinstance(run_result, dict) and (run_result.get("ok") is False or run_result.get("error")))
        await _set_workflow_step_runtime(
            project_id=project_id,
            node_id=node_id,
            inputs=inputs,
            status="completed" if ok else "failed",
            result=run_result if isinstance(run_result, dict) else None,
            template=template,
        )
        step_results.append({
            **materialized,
            "ok": bool(ok),
            "run_result": run_result,
            "error": run_result.get("error") if isinstance(run_result, dict) else None,
            "error_kind": run_result.get("error_kind") if isinstance(run_result, dict) else None,
            })

    await _persist_workflow_input_values(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance,
        inputs=inputs,
    )
    runtime_payload = _workflow_runtime_public_payload(
        await _read_project_state(project_id),
        template_id=str(template.get("id") or template_id or ""),
        instance_id=instance,
    )
    if len(step_results) == 1:
        return {**step_results[0], "runtime": runtime_payload}
    ok = all(not (isinstance(item, dict) and item.get("ok") is False) for item in step_results)
    node_ids = [str(item.get("node_id") or "") for item in step_results if item.get("node_id")]
    return {
        **(first_materialized or {}),
        "ok": ok,
        "step_id": step_id,
        "instance_id": instance,
        "node_id": node_ids[0] if node_ids else None,
        "node_ids": node_ids,
        "run_results": step_results,
        "created_count": sum(1 for item in step_results if item.get("created")),
        "runtime": runtime_payload,
    }


@register(
    "workflow.run_next",
    description="运行当前工作流的下一个可执行步骤；会根据依赖选择 ready 步骤并保存 inputs。",
    tags=["workflow", "write"],
    search_hint=(
        "workflow run next ready step continue fill inputs dependency topological "
        "工作流 运行 下一步 继续 输入 依赖"
    ),
    usage_hints=[
        "用户说开始、继续、下一步，或 Agent 已填好输入后使用。",
        "inputs 是流程输入；可从用户最新回答或 workflow.runtime_status.workflow_input_values 合并得到。",
        "instance_id 用于继续某个并行运行胶囊；多个流程并行时不要省略目标 instance_id。",
        "返回 done=true 表示没有待执行步骤；blocked_steps 表示等待上游依赖。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "workflow": {"type": "object", "additionalProperties": True},
            "title": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "instance_id": {"type": "string"},
            "origin_x": {"type": "number"},
            "origin_y": {"type": "number"},
            "spacing_x": {"type": "number"},
            "spacing_y": {"type": "number"},
        },
    },
)
async def workflow_run_next_step(
    project_id: str,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    title: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    ui_overrides: dict[str, Any] | None = None,
    instance_id: str = "",
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    inputs = await _workflow_inputs_with_saved_values(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    template, error = await _workflow_template_from_spec(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
        context=context,
    )
    if error:
        return error
    assert template is not None
    mismatch_error = await _workflow_instance_template_mismatch_error(
        project_id=project_id,
        instance_id=instance_id,
        template_id=str(template.get("id") or template_id or ""),
    )
    if mismatch_error:
        return mismatch_error
    inputs = _workflow_effective_inputs(template, inputs)
    authorization_error = await _authorize_workflow_for_run(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
    )
    if authorization_error:
        return authorization_error
    await _persist_active_workflow_for_run(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        title=title,
    )
    await _persist_workflow_input_values(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    resolved_template_id = str(template.get("id") or template_id or "").strip()
    server_context = await _workflow_runtime_context_from_project(
        project_id,
        template_id=resolved_template_id,
        instance_id=instance_id,
    )
    if server_context:
        merged_context = _merge_dict(context if isinstance(context, dict) else {}, server_context)
        expanded_template, expanded_error = await _workflow_template_from_spec(
            project_id=project_id,
            template_id=template_id,
            workflow=workflow,
            artifact_ref=artifact_ref,
            inputs=inputs,
            context=merged_context,
        )
        if expanded_template is not None and not expanded_error:
            template = expanded_template
            resolved_template_id = str(template.get("id") or resolved_template_id).strip()

    state = await _read_project_state(project_id)
    runtime_payload = _workflow_runtime_public_payload(
        state,
        template_id=resolved_template_id,
        instance_id=instance_id,
    )
    selected_instance_id = str(instance_id or runtime_payload.get("instance_id") or "").strip()
    records_by_step = {
        str(step.get("id") or ""): step
        for step in (runtime_payload.get("steps") or [])
        if isinstance(step, dict) and step.get("id")
    }
    steps = [step for step in template.get("steps") or [] if isinstance(step, dict)]
    steps_by_id = {
        str(step.get("id") or "").strip(): step
        for step in steps
        if str(step.get("id") or "").strip()
    }
    virtual_step_ids = _virtual_workflow_step_ids(steps, inputs)

    def usable_record_for_step(step: dict[str, Any]) -> dict[str, Any] | None:
        candidate_id = str(step.get("id") or "").strip()
        record = records_by_step.get(candidate_id)
        if not record:
            return None
        if _workflow_step_surface(step) != "workflow_runtime":
            record_surface = _workflow_record_surface(record)
            artifact_node_ids = record.get("artifact_node_ids") if isinstance(record.get("artifact_node_ids"), list) else []
            if record_surface == "workflow_runtime" or not (record.get("node_id") or artifact_node_ids):
                return None
        return record

    def dependency_completed(dep_id: str) -> bool:
        return _workflow_dependency_completed_for_batch(
            dep_id,
            records_by_step=records_by_step,
            steps_by_id=steps_by_id,
            virtual_step_ids=virtual_step_ids,
            failed_step_ids=set(),
        )

    running_record: tuple[str, dict[str, Any]] | None = None
    for step in steps:
        record = usable_record_for_step(step)
        if record and str(record.get("status") or "").strip() == "running":
            running_record = (str(step.get("id") or "").strip(), record)
            break
    if running_record:
        running_step_id = running_record[0]
        return {
            "ok": False,
            "error": "Workflow step is already running",
            "error_kind": "workflow_step_running",
            "running_step_id": running_step_id,
            "project_id": project_id,
            "template_id": resolved_template_id,
            "instance_id": selected_instance_id,
            "runtime": runtime_payload,
        }

    next_step_id = ""
    manual_step_ids: list[str] = []
    blocked_steps: list[dict[str, Any]] = []
    for step in steps:
        candidate_id = str(step.get("id") or "").strip()
        if not candidate_id or candidate_id in virtual_step_ids:
            continue
        if str(step.get("role") or "").strip() == "repeat_group":
            continue
        record = usable_record_for_step(step)
        needs_run = False
        if not record:
            needs_run = True
        else:
            status = str(record.get("status") or "").strip()
            needs_run = bool(record.get("stale")) or status in {"", "idle", "draft", "failed"}
            if status not in {"completed", "running"}:
                needs_run = True
        if not needs_run:
            continue
        waiting_on = [
            dep
            for dep in _workflow_runtime_step_dependency_ids(step)
            if not dependency_completed(dep)
        ]
        if waiting_on:
            blocked_steps.append({"step_id": candidate_id, "waiting_on": waiting_on})
            continue
        if step.get("manual_only") is True and str(step.get("node_type") or "") not in {"image", "video", "audio"}:
            manual_step_ids.append(candidate_id)
            continue
        next_step_id = candidate_id
        break
    if not next_step_id:
        await _persist_workflow_input_values(
            project_id=project_id,
            template=template,
            template_id=template_id,
            workflow=workflow,
            artifact_ref=artifact_ref,
            instance_id=selected_instance_id,
            inputs=inputs,
        )
        if manual_step_ids:
            return {
                "ok": True,
                "done": False,
                "awaiting_manual": True,
                "project_id": project_id,
                "template_id": resolved_template_id,
                "instance_id": selected_instance_id,
                "runtime": runtime_payload,
                "manual_step_ids": manual_step_ids,
                "blocked_steps": blocked_steps,
            }
        if blocked_steps:
            return {
                "ok": False,
                "done": False,
                "project_id": project_id,
                "template_id": resolved_template_id,
                "instance_id": selected_instance_id,
                "runtime": runtime_payload,
                "blocked_steps": blocked_steps,
                "error": "No workflow step is ready; upstream dependencies are not completed.",
                "error_kind": "workflow_waiting_for_dependencies",
            }
        return {
            "ok": True,
            "done": True,
            "project_id": project_id,
            "template_id": resolved_template_id,
            "instance_id": selected_instance_id,
            "runtime": runtime_payload,
        }
    result = await workflow_run_step(
        project_id=project_id,
        step_id=next_step_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        title=title,
        inputs=inputs,
        context=context,
        ui_overrides=ui_overrides,
        instance_id=selected_instance_id,
        origin_x=origin_x,
        origin_y=origin_y,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
        persist_active=False,
    )
    return {
        **result,
        "run_next": True,
        "selected_step_id": next_step_id,
    }


def _workflow_run_all_step_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok", True),
        "done": bool(result.get("done")),
        "step_id": result.get("selected_step_id") or result.get("step_id") or "",
        "node_id": result.get("node_id") or "",
        "node_ids": result.get("node_ids") if isinstance(result.get("node_ids"), list) else [],
        "created_count": result.get("created_count") or 0,
        "error": result.get("error") or "",
        "error_kind": result.get("error_kind") or "",
    }


async def _workflow_run_all_paused_result(
    *,
    project_id: str,
    template_id: str,
    instance_id: str,
    step_results: list[dict[str, Any]],
    runtime_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    runtime_payload = await _workflow_runtime_mark_paused(
        project_id=project_id,
        template_id=template_id,
        instance_id=instance_id,
    ) or runtime_payload
    return {
        "ok": True,
        "run_all": True,
        "done": False,
        "paused": True,
        "project_id": project_id,
        "template_id": template_id,
        "instance_id": instance_id,
        "steps_run": len(step_results),
        "step_results": step_results,
        "failed_steps": [item for item in step_results if item.get("ok") is False],
        "runtime": runtime_payload,
    }


async def _workflow_run_all_manual_result(
    *,
    project_id: str,
    template_id: str,
    instance_id: str,
    step_results: list[dict[str, Any]],
    runtime_payload: dict[str, Any] | None,
    manual_step_ids: list[str],
    blocked_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    if instance_id:
        runtime_payload = await _workflow_runtime_mark_run_all_status(
            project_id=project_id,
            template_id=template_id,
            instance_id=instance_id,
            status="paused",
        ) or runtime_payload
    return {
        "ok": True,
        "run_all": True,
        "done": False,
        "awaiting_manual": True,
        "project_id": project_id,
        "template_id": template_id,
        "instance_id": instance_id,
        "steps_run": len(step_results),
        "step_results": step_results,
        "failed_steps": [item for item in step_results if item.get("ok") is False],
        "manual_step_ids": manual_step_ids,
        "blocked_steps": blocked_steps,
        "runtime": runtime_payload,
    }


def _workflow_run_all_already_running_result(
    *,
    project_id: str,
    template_id: str,
    instance_id: str,
    step_results: list[dict[str, Any]],
    batch: dict[str, Any],
    runtime_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "run_all": True,
        "done": False,
        "already_running": True,
        "running_step_id": str(batch.get("running_step_id") or ""),
        "project_id": project_id,
        "template_id": template_id,
        "instance_id": instance_id,
        "steps_run": len(step_results),
        "step_results": step_results,
        "failed_steps": [item for item in step_results if item.get("ok") is False],
        "runtime": runtime_payload if isinstance(runtime_payload, dict) else batch.get("runtime"),
    }


def _workflow_runtime_usable_record(
    step: dict[str, Any],
    records_by_step: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidate_id = str(step.get("id") or "").strip()
    record = records_by_step.get(candidate_id)
    if not record:
        return None
    if _workflow_step_surface(step) != "workflow_runtime":
        record_surface = _workflow_record_surface(record)
        artifact_node_ids = record.get("artifact_node_ids") if isinstance(record.get("artifact_node_ids"), list) else []
        if record_surface == "workflow_runtime" or not (record.get("node_id") or artifact_node_ids):
            return None
    return record


def _workflow_step_is_optional(step: dict[str, Any]) -> bool:
    return bool(step.get("optional") is True or str(step.get("failure_policy") or "").strip().lower() == "continue")


def _workflow_repeat_group_child_step_ids(
    group_id: str,
    steps_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    target = str(group_id or "").strip()
    if not target:
        return []
    return [
        step_id
        for step_id, step in steps_by_id.items()
        if step_id
        and str(step.get("role") or "").strip() != "repeat_group"
        and str(step.get("repeat_group_id") or "").strip() == target
    ]


def _workflow_runtime_record_repeat_group_id(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    return str(record.get("repeat_group_id") or workflow.get("repeat_group_id") or "").strip()


def _workflow_runtime_record_group_child_step_ids(
    group_id: str,
    records_by_step: dict[str, dict[str, Any]],
) -> list[str]:
    target = str(group_id or "").strip()
    if not target:
        return []
    return [
        step_id
        for step_id, record in records_by_step.items()
        if step_id and _workflow_runtime_record_repeat_group_id(record) == target
    ]


def _workflow_dependency_record_completed(
    dep_id: str,
    *,
    records_by_step: dict[str, dict[str, Any]],
    steps_by_id: dict[str, dict[str, Any]],
    failed_step_ids: set[str],
) -> bool:
    dep = str(dep_id or "").strip()
    record = records_by_step.get(dep)
    if not record:
        return False
    status = str(record.get("status") or "").strip()
    if status == "completed" and not record.get("stale"):
        return True
    return bool(status == "failed" and dep in failed_step_ids and _workflow_step_is_optional(steps_by_id.get(dep, {})))


def _workflow_dependency_completed_for_batch(
    dep_id: str,
    *,
    records_by_step: dict[str, dict[str, Any]],
    steps_by_id: dict[str, dict[str, Any]],
    virtual_step_ids: set[str],
    failed_step_ids: set[str],
) -> bool:
    dep = str(dep_id or "").strip()
    if not dep or dep in virtual_step_ids:
        return True
    if _workflow_dependency_record_completed(
        dep,
        records_by_step=records_by_step,
        steps_by_id=steps_by_id,
        failed_step_ids=failed_step_ids,
    ):
        return True
    group_child_ids = _workflow_repeat_group_child_step_ids(dep, steps_by_id)
    if not group_child_ids:
        group_child_ids = _workflow_runtime_record_group_child_step_ids(dep, records_by_step)
    if not group_child_ids:
        return False
    return all(
        _workflow_dependency_record_completed(
            child_id,
            records_by_step=records_by_step,
            steps_by_id=steps_by_id,
            failed_step_ids=failed_step_ids,
        )
        for child_id in group_child_ids
    )


def _workflow_step_needs_run_for_batch(
    step: dict[str, Any],
    record: dict[str, Any] | None,
    *,
    failed_step_ids: set[str],
) -> bool:
    step_id = str(step.get("id") or "").strip()
    if record is None:
        return True
    status = str(record.get("status") or "").strip()
    if status == "failed" and step_id in failed_step_ids:
        return False
    if bool(record.get("stale")):
        return True
    return status in {"", "idle", "draft", "failed"}


async def _workflow_ready_step_batch(
    *,
    project_id: str,
    template: dict[str, Any],
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    instance_id: str = "",
    failed_step_ids: set[str] | None = None,
) -> dict[str, Any]:
    failed_step_ids = failed_step_ids or set()
    resolved_template_id = str(template.get("id") or template_id or "").strip()
    server_context = await _workflow_runtime_context_from_project(
        project_id,
        template_id=resolved_template_id,
        instance_id=instance_id,
    )
    active_template = template
    if server_context:
        merged_context = _merge_dict(context if isinstance(context, dict) else {}, server_context)
        workflow_for_expand = workflow if isinstance(workflow, dict) and workflow else None
        if workflow_for_expand is None and not template_id and not artifact_ref:
            workflow_for_expand = template
        expanded_template, expanded_error = await _workflow_template_from_spec(
            project_id=project_id,
            template_id=template_id,
            workflow=workflow_for_expand,
            artifact_ref=artifact_ref,
            inputs=inputs,
            context=merged_context,
        )
        if expanded_template is not None and not expanded_error:
            active_template = expanded_template
            resolved_template_id = str(active_template.get("id") or resolved_template_id).strip()

    await _workflow_runtime_settle_terminal_running_steps_for_run(
        project_id,
        instance_id,
        template_id=resolved_template_id,
    )
    state = await _read_project_state(project_id)
    runtime_payload = _workflow_runtime_public_payload(
        state,
        template_id=resolved_template_id,
        instance_id=instance_id,
    )
    selected_instance_id = str(instance_id or runtime_payload.get("instance_id") or "").strip()
    steps = [step for step in active_template.get("steps") or [] if isinstance(step, dict)]
    steps_by_id = {
        str(step.get("id") or "").strip(): step
        for step in steps
        if str(step.get("id") or "").strip()
    }
    runtime_state = _workflow_runtime_state(state)
    runtime_instances = runtime_state.get("instances") if isinstance(runtime_state.get("instances"), dict) else {}
    selected_instance = runtime_instances.get(selected_instance_id) if selected_instance_id else None
    raw_records = selected_instance.get("steps") if isinstance(selected_instance, dict) and isinstance(selected_instance.get("steps"), dict) else {}
    records_by_step = {
        str(step_id): record
        for step_id, record in raw_records.items()
        if isinstance(record, dict) and str(step_id).strip()
    }
    virtual_step_ids = _virtual_workflow_step_ids(steps, inputs)

    for step in steps:
        record = _workflow_runtime_usable_record(step, records_by_step)
        if record and str(record.get("status") or "").strip() == "running":
            running_step_id = str(step.get("id") or "").strip()
            return {
                "ok": False,
                "error": "Workflow step is already running",
                "error_kind": "workflow_step_running",
                "running_step_id": running_step_id,
                "project_id": project_id,
                "template_id": resolved_template_id,
                "instance_id": selected_instance_id,
                "runtime": runtime_payload,
                "template": active_template,
                "ready_step_ids": [],
            }

    ready_step_ids: list[str] = []
    manual_step_ids: list[str] = []
    blocked_steps: list[dict[str, Any]] = []
    for step in steps:
        candidate_id = str(step.get("id") or "").strip()
        if not candidate_id or candidate_id in virtual_step_ids:
            continue
        if str(step.get("role") or "").strip() == "repeat_group":
            continue
        record = _workflow_runtime_usable_record(step, records_by_step)
        if not _workflow_step_needs_run_for_batch(step, record, failed_step_ids=failed_step_ids):
            continue
        waiting_on = [
            dep
            for dep in _workflow_runtime_step_dependency_ids(step)
            if not _workflow_dependency_completed_for_batch(
                dep,
                records_by_step=records_by_step,
                steps_by_id=steps_by_id,
                virtual_step_ids=virtual_step_ids,
                failed_step_ids=failed_step_ids,
            )
        ]
        if waiting_on:
            blocked_steps.append({"step_id": candidate_id, "waiting_on": waiting_on})
            continue
        if step.get("manual_only") is True and str(step.get("node_type") or "") not in {"image", "video", "audio"}:
            manual_step_ids.append(candidate_id)
            continue
        ready_step_ids.append(candidate_id)

    return {
        "ok": True,
        "project_id": project_id,
        "template_id": resolved_template_id,
        "instance_id": selected_instance_id,
        "template": active_template,
        "runtime": runtime_payload,
        "ready_step_ids": ready_step_ids,
        "manual_step_ids": manual_step_ids,
        "blocked_steps": blocked_steps,
        "done": not ready_step_ids and not manual_step_ids and not blocked_steps,
    }


@register(
    "workflow.run_all",
    description="按依赖顺序连续运行当前工作流的所有剩余可执行步骤，并保存 inputs。",
    tags=["workflow", "write"],
    search_hint=(
        "workflow run all remaining steps execute full flow fill inputs parallel instance "
        "工作流 一键运行 全部 剩余步骤 输入 并行实例"
    ),
    usage_hints=[
        "用户要求直接跑完整流程、开始生成整套流程时使用。",
        "会循环调用 workflow.run_next；遇到失败、阻塞或达到 max_steps 会停止并返回真实状态。",
        "inputs 是流程输入；多个并行运行实例用 instance_id 指定目标。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "workflow": {"type": "object", "additionalProperties": True},
            "title": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "instance_id": {"type": "string"},
            "origin_x": {"type": "number"},
            "origin_y": {"type": "number"},
            "spacing_x": {"type": "number"},
            "spacing_y": {"type": "number"},
            "max_steps": {"type": "integer"},
        },
    },
)
async def workflow_run_all_steps(
    project_id: str,
    template_id: str = "",
    workflow: dict[str, Any] | None = None,
    artifact_ref: str = "",
    title: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    ui_overrides: dict[str, Any] | None = None,
    instance_id: str = "",
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
    max_steps: int = 0,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    inputs = await _workflow_inputs_with_saved_values(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    template, error = await _workflow_template_from_spec(
        project_id=project_id,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
        context=context,
    )
    if error:
        return error
    assert template is not None
    mismatch_error = await _workflow_instance_template_mismatch_error(
        project_id=project_id,
        instance_id=instance_id,
        template_id=str(template.get("id") or template_id or ""),
    )
    if mismatch_error:
        return mismatch_error
    inputs = _workflow_effective_inputs(template, inputs)
    authorization_error = await _authorize_workflow_for_run(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
    )
    if authorization_error:
        return authorization_error
    await _persist_workflow_input_values(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        instance_id=instance_id,
        inputs=inputs,
    )
    template_steps = [step for step in template.get("steps") or [] if isinstance(step, dict)]
    if max_steps > 0:
        step_limit = min(max(1, max_steps), 500)
    else:
        step_limit = min(max(len(template_steps) + 20, 120), 500)
    current_instance_id = str(instance_id or "").strip()
    await _workflow_runtime_clear_pause_state(project_id, current_instance_id)
    run_template_id = str(template.get("id") or template_id or "")
    step_results: list[dict[str, Any]] = []
    failed_step_ids: set[str] = set()
    runtime_payload: dict[str, Any] | None = None
    run_all_marked_running = False
    if current_instance_id:
        runtime_payload = await _workflow_runtime_mark_run_all_status(
            project_id=project_id,
            template_id=run_template_id,
            instance_id=current_instance_id,
            status="running",
        )
        run_all_marked_running = True
    for _ in range(step_limit):
        if current_instance_id and await _workflow_runtime_pause_requested(project_id, current_instance_id):
            return await _workflow_run_all_paused_result(
                project_id=project_id,
                template_id=str(template.get("id") or template_id or ""),
                instance_id=current_instance_id,
                step_results=step_results,
                runtime_payload=runtime_payload,
            )
        batch = await _workflow_ready_step_batch(
            project_id=project_id,
            template=template,
            template_id=template_id,
            workflow=workflow,
            artifact_ref=artifact_ref,
            inputs=inputs,
            context=context,
            instance_id=current_instance_id,
            failed_step_ids=failed_step_ids,
        )
        if batch.get("instance_id"):
            current_instance_id = str(batch["instance_id"])
        if current_instance_id and not run_all_marked_running:
            runtime_payload = await _workflow_runtime_mark_run_all_status(
                project_id=project_id,
                template_id=str(batch.get("template_id") or run_template_id),
                instance_id=current_instance_id,
                status="running",
            )
            run_all_marked_running = True
        if isinstance(batch.get("runtime"), dict):
            runtime_payload = batch["runtime"]
        if batch.get("ok") is False:
            if str(batch.get("error_kind") or "") == "workflow_step_running":
                return _workflow_run_all_already_running_result(
                    project_id=project_id,
                    template_id=str(batch.get("template_id") or template.get("id") or template_id or ""),
                    instance_id=current_instance_id,
                    step_results=step_results,
                    batch=batch,
                    runtime_payload=runtime_payload,
                )
            if current_instance_id:
                marked_runtime = await _workflow_runtime_mark_run_all_status(
                    project_id=project_id,
                    template_id=str(batch.get("template_id") or run_template_id),
                    instance_id=current_instance_id,
                    status="failed",
                )
                if isinstance(marked_runtime, dict):
                    runtime_payload = marked_runtime
            return {
                "ok": False,
                "run_all": True,
                "done": False,
                "project_id": project_id,
                "template_id": str(batch.get("template_id") or template.get("id") or template_id or ""),
                "instance_id": current_instance_id,
                "steps_run": len(step_results),
                "step_results": step_results,
                "runtime": runtime_payload,
                "failed_steps": [item for item in step_results if item.get("ok") is False],
                "error": batch.get("error") or "Workflow run failed",
                "error_kind": batch.get("error_kind") or "workflow_run_failed",
            }
        ready_step_ids = [str(item or "").strip() for item in (batch.get("ready_step_ids") or []) if str(item or "").strip()]
        if not current_instance_id and len(ready_step_ids) > 1:
            current_instance_id = f"wf_{uuid.uuid4().hex[:12]}"
            runtime_payload = await _workflow_runtime_mark_run_all_status(
                project_id=project_id,
                template_id=str(batch.get("template_id") or run_template_id),
                instance_id=current_instance_id,
                status="running",
            )
            run_all_marked_running = True
        if current_instance_id and await _workflow_runtime_pause_requested(project_id, current_instance_id):
            return await _workflow_run_all_paused_result(
                project_id=project_id,
                template_id=str(batch.get("template_id") or template.get("id") or template_id or ""),
                instance_id=current_instance_id,
                step_results=step_results,
                runtime_payload=runtime_payload,
            )
        if batch.get("done") or not ready_step_ids:
            failed_steps = [item for item in step_results if item.get("ok") is False]
            manual_step_ids = [
                str(item or "").strip()
                for item in (batch.get("manual_step_ids") or [])
                if str(item or "").strip()
            ]
            if manual_step_ids:
                if not current_instance_id:
                    current_instance_id = f"wf_{uuid.uuid4().hex[:12]}"
                return await _workflow_run_all_manual_result(
                    project_id=project_id,
                    template_id=str(batch.get("template_id") or template.get("id") or template_id or ""),
                    instance_id=current_instance_id,
                    step_results=step_results,
                    runtime_payload=runtime_payload,
                    manual_step_ids=manual_step_ids,
                    blocked_steps=batch.get("blocked_steps") or [],
                )
            if batch.get("blocked_steps"):
                if current_instance_id:
                    marked_runtime = await _workflow_runtime_mark_run_all_status(
                        project_id=project_id,
                        template_id=str(batch.get("template_id") or run_template_id),
                        instance_id=current_instance_id,
                        status="failed",
                    )
                    if isinstance(marked_runtime, dict):
                        runtime_payload = marked_runtime
                return {
                    "ok": False,
                    "run_all": True,
                    "done": False,
                    "project_id": project_id,
                    "template_id": str(batch.get("template_id") or template.get("id") or template_id or ""),
                    "instance_id": current_instance_id,
                    "steps_run": len(step_results),
                    "step_results": step_results,
                    "failed_steps": failed_steps,
                    "blocked_steps": batch.get("blocked_steps") or [],
                    "runtime": runtime_payload,
                    "error": "No workflow step is ready; upstream dependencies are not completed.",
                    "error_kind": "workflow_waiting_for_dependencies",
                }
            if current_instance_id:
                marked_runtime = await _workflow_runtime_mark_run_all_status(
                    project_id=project_id,
                    template_id=str(batch.get("template_id") or run_template_id),
                    instance_id=current_instance_id,
                    status="failed" if failed_steps else "completed",
                )
                if isinstance(marked_runtime, dict):
                    runtime_payload = marked_runtime
            return {
                "ok": not failed_steps,
                "run_all": True,
                "done": True,
                "project_id": project_id,
                "template_id": str(batch.get("template_id") or template.get("id") or template_id or ""),
                "instance_id": current_instance_id,
                "steps_run": len(step_results),
                "step_results": step_results,
                "failed_steps": failed_steps,
                "runtime": runtime_payload,
            }
        remaining = step_limit - len(step_results)
        if remaining <= 0:
            break
        ready_step_ids = ready_step_ids[:remaining]

        async def run_one(step_id: str) -> dict[str, Any]:
            try:
                result = await workflow_run_step(
                    project_id=project_id,
                    step_id=step_id,
                    template_id=template_id,
                    workflow=workflow,
                    artifact_ref=artifact_ref,
                    title=title,
                    inputs=inputs,
                    context=context,
                    ui_overrides=ui_overrides,
                    instance_id=current_instance_id,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    spacing_x=spacing_x,
                    spacing_y=spacing_y,
                    persist_active=False,
                )
                if not str(result.get("step_id") or "").strip():
                    result = {**result, "step_id": step_id}
                return result
            except Exception as exc:
                return {
                    "ok": False,
                    "step_id": step_id,
                    "error": str(exc),
                    "error_kind": exc.__class__.__name__,
                }

        results_before_batch = len(step_results)
        batch_results = await asyncio.gather(*(run_one(step_id) for step_id in ready_step_ids))
        for result in batch_results:
            if result.get("instance_id"):
                current_instance_id = str(result["instance_id"])
            if isinstance(result.get("runtime"), dict):
                runtime_payload = result["runtime"]
            summary = _workflow_run_all_step_summary(result)
            if not summary["done"]:
                step_results.append(summary)
            if result.get("ok") is False:
                failed_id = str(summary.get("step_id") or result.get("step_id") or "").strip()
                if failed_id:
                    failed_step_ids.add(failed_id)
        if len(step_results) == results_before_batch:
            if current_instance_id:
                marked_runtime = await _workflow_runtime_mark_run_all_status(
                    project_id=project_id,
                    template_id=str(batch.get("template_id") or run_template_id),
                    instance_id=current_instance_id,
                    status="failed",
                )
                if isinstance(marked_runtime, dict):
                    runtime_payload = marked_runtime
            return {
                "ok": False,
                "run_all": True,
                "done": False,
                "project_id": project_id,
                "template_id": str(batch.get("template_id") or template.get("id") or template_id or ""),
                "instance_id": current_instance_id,
                "steps_run": len(step_results),
                "step_results": step_results,
                "ready_step_ids": ready_step_ids,
                "runtime": runtime_payload,
                "error": "Workflow run-all made no progress while ready steps remained.",
                "error_kind": "workflow_run_all_no_progress",
            }
    if current_instance_id and await _workflow_runtime_pause_requested(project_id, current_instance_id):
        return await _workflow_run_all_paused_result(
            project_id=project_id,
            template_id=str(template.get("id") or template_id or ""),
            instance_id=current_instance_id,
            step_results=step_results,
            runtime_payload=runtime_payload,
        )
    final_batch = await _workflow_ready_step_batch(
        project_id=project_id,
        template=template,
        template_id=template_id,
        workflow=workflow,
        artifact_ref=artifact_ref,
        inputs=inputs,
        context=context,
        instance_id=current_instance_id,
        failed_step_ids=failed_step_ids,
    )
    if final_batch.get("instance_id"):
        current_instance_id = str(final_batch["instance_id"])
    if isinstance(final_batch.get("runtime"), dict):
        runtime_payload = final_batch["runtime"]
    failed_steps = [item for item in step_results if item.get("ok") is False]
    if final_batch.get("ok") is False:
        if str(final_batch.get("error_kind") or "") == "workflow_step_running":
            return _workflow_run_all_already_running_result(
                project_id=project_id,
                template_id=str(final_batch.get("template_id") or template.get("id") or template_id or ""),
                instance_id=current_instance_id,
                step_results=step_results,
                batch=final_batch,
                runtime_payload=runtime_payload,
            )
        if current_instance_id:
            marked_runtime = await _workflow_runtime_mark_run_all_status(
                project_id=project_id,
                template_id=str(final_batch.get("template_id") or run_template_id),
                instance_id=current_instance_id,
                status="failed",
            )
            if isinstance(marked_runtime, dict):
                runtime_payload = marked_runtime
        return {
            "ok": False,
            "run_all": True,
            "done": False,
            "project_id": project_id,
            "template_id": str(final_batch.get("template_id") or template.get("id") or template_id or ""),
            "instance_id": current_instance_id,
            "steps_run": len(step_results),
            "step_results": step_results,
            "failed_steps": failed_steps,
            "runtime": runtime_payload,
            "error": final_batch.get("error") or "Workflow run failed",
            "error_kind": final_batch.get("error_kind") or "workflow_run_failed",
        }
    final_ready_step_ids = [
        str(item or "").strip()
        for item in (final_batch.get("ready_step_ids") or [])
        if str(item or "").strip()
    ]
    if final_batch.get("done") or not final_ready_step_ids:
        manual_step_ids = [
            str(item or "").strip()
            for item in (final_batch.get("manual_step_ids") or [])
            if str(item or "").strip()
        ]
        if manual_step_ids:
            if not current_instance_id:
                current_instance_id = f"wf_{uuid.uuid4().hex[:12]}"
            return await _workflow_run_all_manual_result(
                project_id=project_id,
                template_id=str(final_batch.get("template_id") or template.get("id") or template_id or ""),
                instance_id=current_instance_id,
                step_results=step_results,
                runtime_payload=runtime_payload,
                manual_step_ids=manual_step_ids,
                blocked_steps=final_batch.get("blocked_steps") or [],
            )
        if final_batch.get("blocked_steps"):
            if current_instance_id:
                marked_runtime = await _workflow_runtime_mark_run_all_status(
                    project_id=project_id,
                    template_id=str(final_batch.get("template_id") or run_template_id),
                    instance_id=current_instance_id,
                    status="failed",
                )
                if isinstance(marked_runtime, dict):
                    runtime_payload = marked_runtime
            return {
                "ok": False,
                "run_all": True,
                "done": False,
                "project_id": project_id,
                "template_id": str(final_batch.get("template_id") or template.get("id") or template_id or ""),
                "instance_id": current_instance_id,
                "steps_run": len(step_results),
                "step_results": step_results,
                "failed_steps": failed_steps,
                "blocked_steps": final_batch.get("blocked_steps") or [],
                "runtime": runtime_payload,
                "error": "No workflow step is ready; upstream dependencies are not completed.",
                "error_kind": "workflow_waiting_for_dependencies",
            }
        if current_instance_id:
            marked_runtime = await _workflow_runtime_mark_run_all_status(
                project_id=project_id,
                template_id=str(final_batch.get("template_id") or run_template_id),
                instance_id=current_instance_id,
                status="failed" if failed_steps else "completed",
            )
            if isinstance(marked_runtime, dict):
                runtime_payload = marked_runtime
        return {
            "ok": not failed_steps,
            "run_all": True,
            "done": True,
            "project_id": project_id,
            "template_id": str(final_batch.get("template_id") or template.get("id") or template_id or ""),
            "instance_id": current_instance_id,
            "steps_run": len(step_results),
            "step_results": step_results,
            "failed_steps": failed_steps,
            "runtime": runtime_payload,
        }
    if current_instance_id:
        marked_runtime = await _workflow_runtime_mark_run_all_status(
            project_id=project_id,
            template_id=run_template_id,
            instance_id=current_instance_id,
            status="failed",
        )
        if isinstance(marked_runtime, dict):
            runtime_payload = marked_runtime
    return {
        "ok": False,
        "run_all": True,
        "done": False,
        "project_id": project_id,
        "template_id": str(template.get("id") or template_id or ""),
        "instance_id": current_instance_id,
        "steps_run": len(step_results),
        "step_results": step_results,
        "failed_steps": failed_steps,
        "ready_step_ids": final_ready_step_ids,
        "runtime": runtime_payload,
        "error": f"Workflow run-all reached step limit: {step_limit}",
        "error_kind": "workflow_run_all_step_limit",
    }


async def _emit_canvas_action(project_id: str, action: str, payload: dict[str, Any]) -> None:
    try:
        from app.agent.orchestrator import emit_canvas_event

        await emit_canvas_event(
            {"type": "canvas_action", "action": action, "payload": payload},
            project_id=project_id,
        )
    except Exception:
        return


@register(
    "workflow.protocol_info",
    description="查看当前 Workflow Spec v2 合同、引用角色、执行模式和可用插件。",
    tags=["workflow", "read", "meta"],
    search_hint=(
        "workflow spec protocol capabilities extensions custom nodes import "
        "工作流 协议 能力 扩展 自定义节点 导入"
    ),
    usage_hints=[
        "workflow 统一使用 schema='openreel.workflow.v2'。",
        "需要看图写提示词使用 uses.as=['vision']；媒体生成参考使用 reference。",
        "第三方步骤使用 namespaced plugin.id；插件不可用时保存和运行前报错。",
        "可移植 spec 不写 provider、model、tier 或私有运行字段。",
    ],
    is_read_only=True,
    is_concurrency_safe=True,
)
async def workflow_protocol_info(project_id: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "project_id": project_id,
        **canvas_workflow_templates.workflow_protocol_info(),
    }


async def _materialize_template(
    *,
    project_id: str,
    template: dict[str, Any],
    title: str = "",
    inputs: dict[str, Any] | None = None,
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    instance_id = f"wf_{uuid.uuid4().hex[:12]}"
    inputs = _workflow_effective_inputs(template, inputs)
    input_facts = _input_summary(inputs or {})
    default_fields = template.get("defaults", {}).get("fields")
    if not isinstance(default_fields, dict):
        default_fields = {}

    created_by_step: dict[str, dict[str, Any]] = {}
    public_nodes: list[dict[str, Any]] = []
    internal_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    runtime_touched = False
    template_steps = [step for step in template["steps"] if isinstance(step, dict)]
    steps_by_id = {
        str(step.get("id") or "").strip(): step
        for step in template_steps
        if str(step.get("id") or "").strip()
    }
    virtual_step_ids = _virtual_workflow_step_ids(
        template_steps,
        inputs,
    )

    for index, step in enumerate(template["steps"]):
        if step["id"] in virtual_step_ids:
            continue
        node_type = str(step["node_type"])
        fields = _workflow_strip_template_media_model(_merge_dict(default_fields, step.get("fields") or {}), node_type)
        step_title = str(step.get("title") or fields.get("title") or step["id"]).strip()
        if title and index == 0:
            step_title = str(title).strip()
        fields.setdefault("title", step_title)
        fields.setdefault("purpose", step.get("purpose") or step.get("id"))
        fields.setdefault("stage", step.get("id"))
        if node_type in {"image", "video", "audio"}:
            fields.setdefault("prompt_status", "draft")
        if node_type == "image":
            fields.setdefault("aspect_ratio", template.get("defaults", {}).get("aspect_ratio") or "9:16")
            fields.setdefault(
                "resolution",
                template.get("defaults", {}).get("resolution")
                or _workflow_default_image_resolution(fields.get("aspect_ratio")),
            )
            fields.setdefault("quality", template.get("defaults", {}).get("quality") or "high")
        if node_type == "video":
            fields.setdefault("aspect_ratio", template.get("defaults", {}).get("aspect_ratio") or "9:16")
            fields.setdefault("resolution", template.get("defaults", {}).get("resolution") or "720p")
            if template.get("defaults", {}).get("duration_seconds"):
                fields.setdefault("duration_seconds", template["defaults"]["duration_seconds"])
        surface = _workflow_step_surface(step)
        fields["surface"] = surface
        if surface != "workflow_runtime":
            first_dep = next(iter(_workflow_data_dependency_ids(step)), "")
            fields.setdefault("workflow_source_step", str(step.get("source_step") or first_dep).strip())
            fields.setdefault("workflow_source_path", str(step.get("source_path") or "output").strip() or "output")
            fields.setdefault("workflow_generate", node_type in {"image", "video", "audio"})

        fields = _merge_workflow_dependency_refs(
            fields,
            [
                *_workflow_dependency_refs_for_step(
                    step,
                    created_by_step=created_by_step,
                    nodes_by_alias={},
                    steps_by_id=steps_by_id,
                    virtual_step_ids=virtual_step_ids,
                    include_runtime_upstream=True,
                    extra_dep_keys=[
                        str(fields.get("workflow_source_step") or "").strip(),
                        str(fields.get("source_step") or "").strip(),
                    ],
                ),
                *_workflow_input_reference_refs(inputs or {}, step, fields),
            ],
            replace_managed=True,
        )

        workflow_meta = fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {}
        fields["workflow"] = {
            **workflow_meta,
            "template_id": template["id"],
            "template_name": template["name"],
            "instance_id": instance_id,
            "step_id": step["id"],
            "step_index": index + 1,
            "step_status": "draft",
            "surface": surface,
            "visibility": step.get("visibility") or ("flow_only" if surface == "workflow_runtime" else "canvas"),
            "primary_skill": step.get("primary_skill") or "",
            "skill_category": step.get("skill_category") or "",
            "acceptance": step.get("acceptance") or "",
            "input_facts": input_facts,
            "protocol": _workflow_protocol_payload(template),
            **_step_workflow_metadata(step),
        }
        if step.get("worker"):
            fields["workflow"]["worker"] = step.get("worker")

        if surface == "workflow_runtime":
            output: Any = None
            if str(step.get("runner") or "").strip() in _WORKFLOW_INPUT_RUNNERS:
                output = {
                    "inputs": input_facts,
                    "input_values": input_facts,
                    "content": json.dumps(input_facts, ensure_ascii=False, default=str),
                }
            record = await _upsert_workflow_runtime_step(
                project_id=project_id,
                template=template,
                instance_id=instance_id,
                step_id=str(step["id"]),
                node_type=node_type,
                title=step_title,
                fields=fields,
                status="draft",
                output=output,
            )
            runtime_touched = True
            created_by_step[str(step["id"])] = record
            continue

        x, y = _step_position(
            step,
            index=(
                sum(1 for item in template["steps"][:index] if isinstance(item, dict) and _workflow_step_surface(item) != "workflow_runtime")
                if surface != "workflow_runtime"
                else index
            ),
            steps=template_steps,
            origin_x=origin_x,
            origin_y=origin_y,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
        )
        node = await canvas_tools.create_node(
            project_id=project_id,
            node_type=node_type,
            title=step_title,
            position_x=x,
            position_y=y,
            input_data=fields,
            model_config={
                "surface": surface,
                "_ui_creator": "agent",
                "workflow_template_id": template["id"],
                "workflow_instance_id": instance_id,
            },
            prompt=str(fields.get("prompt") or "") or None,
        )
        node["input"] = fields
        node["input_json"] = fields
        node["position_x"] = x
        node["position_y"] = y
        created_by_step[step["id"]] = node
        internal_nodes.append(node)
        await _upsert_workflow_runtime_step(
            project_id=project_id,
            template=template,
            instance_id=instance_id,
            step_id=str(step["id"]),
            node_type=node_type,
            title=step_title,
            fields=fields,
            status="draft",
            artifacts=[_workflow_runtime_artifact_from_node(node)],
            node_id=str(node.get("id") or ""),
            surface=surface,
        )
        runtime_touched = True
        await _emit_canvas_action(project_id, "create_node", node)

        target_node_id = str(node.get("id") or "")
        for dep in _workflow_data_dependency_ids(step):
            if dep in virtual_step_ids:
                continue
            for source in _workflow_visible_dependency_nodes(
                str(dep or "").strip(),
                created_by_step=created_by_step,
                nodes_by_alias={},
                steps_by_id=steps_by_id,
                target_step=step,
                exclude_node_ids={target_node_id},
                include_runtime_upstream=True,
            ):
                source_node_id = str(source.get("id") or "")
                if not source_node_id or source_node_id == target_node_id:
                    continue
                edge = await canvas_tools.connect_nodes(
                    project_id=project_id,
                    source_node_id=source_node_id,
                    target_node_id=target_node_id,
                    label=str(step.get("dependency_role") or ""),
                )
                edges.append(edge)
                await _emit_canvas_action(project_id, "add_edge", edge)

    async with session_scope() as session:
        id_map = await internal_to_public_id_map(session, project_id)
    for node in internal_nodes:
        public = model_visible_node_payload(node, id_map)
        public["_canvas_id"] = node.get("id")
        public["_canvas_display_id"] = node.get("display_id")
        public_nodes.append(public)

    runtime_payload = None
    if runtime_touched:
        runtime_payload = _workflow_runtime_public_payload(
            await _read_project_state(project_id),
            template_id=str(template.get("id") or ""),
            instance_id=instance_id,
        )
    if public_nodes:
        next_action = "已创建画布节点；返回的 nodes 和 runtime 可作为验收依据，随后完成对应任务。"
    else:
        next_action = "已创建运行态流程；需要展开用户可见产物时运行 ready 步骤或读取 deferred_groups 后补齐输入。"

    return {
        "ok": True,
        "project_id": project_id,
        "template_id": template["id"],
        "template_name": template["name"],
        "protocol": _workflow_protocol_payload(template),
        "instance_id": instance_id,
        "created_count": len(public_nodes),
        "nodes": public_nodes,
        "edges_count": len(edges),
        "runtime": runtime_payload,
        "deferred_groups": deepcopy(template.get("deferred_groups") or []),
        "deferred_group_count": len(template.get("deferred_groups") or []),
        "dimensions": deepcopy(template.get("dimensions") or {}),
        "next_action": next_action,
    }


@register(
    "workflow.list_templates",
    description="列出可实例化到画布的轻量 workflow 模板目录。",
    tags=["workflow", "read"],
    search_hint=(
        "canvas workflow templates scaffold graph nodes dependencies reusable short video "
        "画布 工作流 模板 骨架 节点 依赖 短剧 短视频"
    ),
    usage_hints=[
        "按 skill 或目标匹配模板时，可用 workflow.template.resolve 查看候选。",
        "本工具返回轻量目录；读取单个模板结构用 workflow.template.read。",
    ],
    is_read_only=True,
    is_concurrency_safe=True,
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "query": {"type": "string"},
            "category": {"type": "string"},
            "limit": {"type": "integer"},
        },
    },
)
async def workflow_list_templates(
    project_id: str = "",
    query: str = "",
    category: str = "",
    limit: int = 12,
) -> dict[str, Any]:
    templates, matched_total = _light_template_catalog(query=query, category=category, limit=limit)
    return {
        "ok": True,
        "project_id": project_id,
        "query": query,
        "category": category,
        "templates": templates,
        "total": matched_total,
        "returned": len(templates),
        "hint": "返回的 template_id 可用于实例化；语义相似候选可通过 workflow.template.resolve 获取。",
    }


@register(
    "workflow.instantiate",
    description="把已选择的工作流模板实例化成画布 draft 节点和依赖边；不生成内容、不运行节点。",
    tags=["workflow", "write"],
    search_hint=(
        "instantiate canvas workflow scaffold create draft nodes edges dependencies template short video "
        "实例化 画布 工作流 搭建 骨架 创建 节点 连线 依赖 短剧 短视频"
    ),
    usage_hints=[
        "已选择或复用模板时实例化；后续按节点用 skill 和 agent/node 工具补内容。",
        "简单单节点任务继续直接用 node.create/node.update/node.run。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "title": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "origin_x": {"type": "number"},
            "origin_y": {"type": "number"},
            "spacing_x": {"type": "number"},
            "spacing_y": {"type": "number"},
        },
    },
)
async def workflow_instantiate(
    project_id: str,
    template_id: str = "",
    title: str = "",
    inputs: dict[str, Any] | None = None,
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    if not project_id:
        return {
            "ok": False,
            "error": "project_id is required",
            "error_kind": "missing_project_id",
        }
    try:
        template = canvas_workflow_templates.get_template(template_id, input_values=inputs)
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_template_error",
            "available_templates": _light_template_catalog(limit=12)[0],
        }
    inputs = _workflow_effective_inputs(template, inputs)
    required_error = _required_input_error(template, inputs)
    if required_error:
        return required_error
    return await _materialize_template(
        project_id=project_id,
        template=template,
        title=title,
        inputs=inputs,
        origin_x=origin_x,
        origin_y=origin_y,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
    )


@register(
    "workflow.template.resolve",
    description="按 skill 摘要或目标检索内置和用户可复用 workflow 模板候选。",
    tags=["workflow", "artifact", "read"],
    search_hint=(
        "resolve reusable workflow template candidates skill summary builtin user template directory "
        "复用 工作流 模板 候选 skill 摘要 内置 用户 自定义 模板目录"
    ),
    usage_hints=[
        "返回候选、短摘要和缺失输入问题；workflow_spec 只确认并返回最匹配的现有模板。",
    ],
    is_read_only=True,
    is_concurrency_safe=True,
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "skill_name": {"type": "string"},
            "skill_summary": {"type": "string"},
            "user_goal": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "limit": {"type": "integer"},
        },
    },
)
async def workflow_template_resolve(
    project_id: str = "",
    skill_name: str = "",
    skill_summary: str = "",
    user_goal: str = "",
    inputs: dict[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    input_values = inputs if isinstance(inputs, dict) else {}
    raw_candidates = workflow_template_store.candidate_summaries_for_skill(
        skill_name=skill_name,
        skill_summary=skill_summary,
        user_goal=user_goal,
        limit=limit,
    )
    candidates = [
        _workflow_template_candidate_payload(candidate, input_values)
        for candidate in raw_candidates
        if isinstance(candidate, dict)
    ]
    direct_summary = _direct_workflow_template_summary_for_skill(skill_name)
    direct_template = (
        _direct_workflow_template_payload(direct_summary, input_values)
        if isinstance(direct_summary, dict)
        else None
    )
    if direct_template:
        decision_hint = "已命中 direct_template；交给 workflow_spec 确认并返回最终引用。"
    elif candidates:
        decision_hint = "候选已返回；workflow_spec 读取完整 skill/模板后选择最匹配的现有模板。"
    else:
        decision_hint = "未找到内置或用户模板候选；workflow_spec 应返回 blocked 并说明缺少哪类模板。"
    result = {
        "ok": True,
        "project_id": project_id,
        "candidates": candidates,
        "total": len(candidates),
        "decision_hint": decision_hint,
    }
    if direct_template:
        result["direct_template"] = direct_template
    if input_values:
        result["inputs"] = deepcopy(input_values)
    return result


@register(
    "workflow.template.read",
    description="读取内置或用户 workflow 模板的 preview 或完整 workflow。",
    tags=["workflow", "artifact", "read"],
    search_hint=(
        "read builtin user reusable workflow template preview full workflow semantic match "
        "读取 内置 用户 自定义 可复用 工作流 模板 预览 完整"
    ),
    usage_hints=[
        "detail='preview' 返回轻量摘要；detail='workflow' 返回完整模板结构。",
        "detail='workflow' 返回完整结构，适合隔离上下文做语义匹配。",
    ],
    is_read_only=True,
    is_concurrency_safe=True,
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "version_id": {"type": "string"},
            "detail": {"type": "string", "enum": ["preview", "workflow"]},
        },
        "required": ["template_id"],
    },
)
async def workflow_template_read(
    project_id: str = "",
    template_id: str = "",
    version_id: str = "",
    detail: str = "preview",
) -> dict[str, Any]:
    try:
        loaded = workflow_template_store.load_user_template(template_id, version_id)
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        try:
            template = canvas_workflow_templates.get_template(template_id)
        except canvas_workflow_templates.WorkflowTemplateError:
            return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
        summary = _template_catalog_summary(template)
        payload = {
            "ok": True,
            "project_id": project_id,
            "template_id": summary.get("id"),
            "version_id": summary.get("active_version_id") or "",
            "summary": summary,
            "preview": summary,
            "input_fields": _workflow_template_input_definitions(template),
            "sample_inputs": {},
            "self_check": {},
        }
        if str(detail or "").strip() == "workflow":
            payload["workflow"] = template
            payload["source"] = {"source": template.get("source") or "builtin_template"}
        return payload
    payload: dict[str, Any] = {
        "ok": True,
        "project_id": project_id,
        "template_id": loaded["summary"].get("id"),
        "version_id": loaded["summary"].get("active_version_id"),
        "summary": loaded.get("summary") or {},
        "preview": loaded.get("preview") or {},
        "input_fields": _workflow_template_input_definitions(loaded.get("workflow") or {}),
        "sample_inputs": loaded.get("sample_inputs") or {},
        "self_check": loaded.get("self_check") or {},
    }
    if str(detail or "").strip() == "workflow":
        payload["workflow"] = loaded.get("workflow") or {}
        payload["source"] = loaded.get("source") or {}
    return payload


@register(
    "workflow.template.clone_to_artifact",
    description="把内置或用户模板克隆为当前项目 workflow spec artifact；用于复用或作为 patch 基线。",
    tags=["workflow", "artifact", "read"],
    search_hint=(
        "clone reusable workflow template to project artifact patch baseline "
        "模板 克隆 artifact 复用 微调 基线"
    ),
    usage_hints=[
        "用于把内置或用户模板作为当前项目 artifact 基线。",
        "小改动可先 clone_to_artifact，再基于 artifact_ref 生成 patch revision。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "version_id": {"type": "string"},
            "source": {"type": "object", "additionalProperties": True},
        },
        "required": ["template_id"],
    },
)
async def workflow_template_clone_to_artifact(
    project_id: str,
    template_id: str,
    version_id: str = "",
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    try:
        cloned = workflow_template_store.clone_template_to_artifact(
            project_id=project_id,
            template_id=template_id,
            version_id=version_id,
            source=source,
        )
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    except WorkflowAuditError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_audit_failed", "audit": exc.report}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_error"}
    return {
        **cloned,
        "hint": "返回的 artifact_ref 可用于复用物化，或作为后续微调基线。",
    }


def _latest_workflow_instance_id(
    state: dict[str, Any],
    *,
    template_id: str = "",
) -> str:
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    for candidate_id, instance in reversed(list(instances.items())):
        if not isinstance(instance, dict):
            continue
        if template_id and str(instance.get("template_id") or "").strip() != template_id:
            continue
        candidate = str(candidate_id or instance.get("instance_id") or "").strip()
        if candidate:
            return candidate
    return ""


def _current_workflow_template_id(state: dict[str, Any], base_template_id: str = "") -> str:
    selected = str(base_template_id or "").strip()
    if selected:
        return selected
    active = state.get(_ACTIVE_WORKFLOW_STATE_KEY) if isinstance(state.get(_ACTIVE_WORKFLOW_STATE_KEY), dict) else {}
    for key in ("template_id", "workflow_id"):
        value = str(active.get(key) or "").strip()
        if value:
            return value
    runtime = _workflow_runtime_state(state)
    instances = runtime.get("instances") if isinstance(runtime.get("instances"), dict) else {}
    for instance in reversed(list(instances.values())):
        if not isinstance(instance, dict):
            continue
        value = str(instance.get("template_id") or "").strip()
        if value:
            return value
    return ""


def _apply_step_prompt_overrides(
    workflow: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(overrides, dict) or not overrides:
        return workflow, []
    patched = deepcopy(workflow)
    steps = patched.get("steps")
    if not isinstance(steps, list):
        return patched, []
    applied: list[dict[str, str]] = []
    for step_id, value in overrides.items():
        step_key = str(step_id or "").strip()
        if not step_key:
            continue
        prompt = deepcopy(value) if isinstance(value, dict) else {"task": str(value or "").strip()}
        if not str(prompt.get("task") or "").strip():
            continue
        found = workflow_spec_patch_service.find_workflow_step_container(steps, step_key)
        if found is None:
            continue
        found[2]["prompt"] = prompt
        applied.append({"step_id": step_key, "field": "prompt"})
    return patched, applied



@register(
    "workflow.template.save_current",
    description="把当前工作流的公开 V2 spec 保存为用户可复用模板。",
    tags=["workflow", "artifact", "read"],
    search_hint=(
        "save current workflow public v2 spec as user reusable template "
        "保存 当前 流程 画布 实例 用户 可复用 模板 另存为 模板"
    ),
    usage_hints=[
        "用户说把当前流程/画布流程另存为今后可选模板时使用；不需要重新编写 spec。",
        "运行状态、画布节点内容和私有提示词阶段不会写回模板。",
        "如果用户明确指定某个步骤的新提示词合同，用 step_prompts 按 step id 传入。",
        "已有 artifact_ref 且要直接提升 artifact 时才用 workflow.template.promote。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "base_template_id": {"type": "string"},
            "instance_id": {"type": "string"},
            "template_id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "category": {"type": "string"},
            "applies_to": {"type": "string"},
            "version": {"type": "string"},
            "replace_existing": {"type": "boolean"},
            "step_prompts": {
                "type": "object",
                "additionalProperties": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "object", "additionalProperties": True},
                    ]
                },
                "description": "可选：按 step id 覆盖公开 V2 的 step.prompt。",
            },
            "source_skill_name": {"type": "string"},
            "source_skill_scope": {"type": "string"},
            "source_skill_summary": {"type": "string"},
        },
    },
)
async def workflow_template_save_current(
    project_id: str,
    base_template_id: str = "",
    instance_id: str = "",
    template_id: str = "",
    name: str = "",
    description: str = "",
    category: str = "user",
    applies_to: str = "",
    version: str = "",
    replace_existing: bool = False,
    step_prompts: dict[str, Any] | None = None,
    source_skill_name: str = "",
    source_skill_scope: str = "",
    source_skill_summary: str = "",
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    state = await _read_project_state(project_id)
    selected_template_id = _current_workflow_template_id(state, base_template_id)
    if not selected_template_id:
        return {
            "ok": False,
            "error": "No active workflow template found",
            "error_kind": "workflow_template_not_selected",
            "hint": "先运行或选择一个 workflow；已有 artifact_ref 时可用 workflow.template.promote。",
        }
    selected_instance_id = str(instance_id or "").strip() or _latest_workflow_instance_id(
        state,
        template_id=selected_template_id,
    )
    input_values = workflow_input_values_public_payload(
        state,
        workflow_id=selected_template_id,
        instance_id=selected_instance_id,
    )
    try:
        base_template = canvas_workflow_templates.get_template(
            selected_template_id,
            input_values=input_values,
        )
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}

    public_spec = base_template.get("public_spec")
    if not isinstance(public_spec, dict):
        return {
            "ok": False,
            "error": "Selected workflow does not expose a public V2 spec",
            "error_kind": "workflow_public_spec_missing",
        }
    workflow, applied = _apply_step_prompt_overrides(deepcopy(public_spec), step_prompts)
    template_name = str(name or workflow.get("title") or base_template.get("name") or "当前流程模板").strip()
    if template_id:
        workflow["id"] = template_id
    workflow["title"] = template_name
    if description:
        workflow["description"] = description
    try:
        saved = workflow_template_store.save_user_template(
            workflow=workflow,
            template_id=template_id or workflow.get("id") or template_name,
            name=template_name,
            description=description or str(workflow.get("description") or ""),
            category=category or "user",
            applies_to=applies_to,
            version=version,
            replace_existing=replace_existing,
            sample_inputs=input_values,
            preview={"saved_from_instance_id": selected_instance_id, "applied_overrides": applied},
            source={
                "agent": "workflow_template_save_current",
                "project_id": project_id,
                "base_template_id": selected_template_id,
                "instance_id": selected_instance_id,
                "source_skill": {
                    "name": source_skill_name,
                    "scope": source_skill_scope,
                    "summary": source_skill_summary,
                },
            },
        )
    except WorkflowAuditError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_audit_failed", "audit": exc.report}
    except (ValueError, json.JSONDecodeError, workflow_template_store.WorkflowTemplateStoreError) as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_error"}
    return {
        **saved,
        "base_template_id": selected_template_id,
        "instance_id": selected_instance_id,
        "applied_overrides": applied,
        "next_action": "模板已保存到 workflow_templates/user/；前端可选择，下载可用 workflow.template.export。",
    }


@register(
    "workflow.template.promote",
    description="把当前项目 workflow spec artifact 保存为用户可复用模板，之后可在前端选择或下载。",
    tags=["workflow", "artifact", "read"],
    search_hint=(
        "promote workflow spec artifact to reusable user template save future download "
        "保存为模板 用户复用 下载 工作流 artifact"
    ),
    usage_hints=[
        "用户明确说今后可复用/保存成模板时调用。",
        "source_skill_* 只存摘要和标识；完整 skill 内容由子 Agent 读取，不写入主上下文。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "template_id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "category": {"type": "string"},
            "applies_to": {"type": "string"},
            "version": {"type": "string"},
            "replace_existing": {"type": "boolean"},
            "source_skill_name": {"type": "string"},
            "source_skill_scope": {"type": "string"},
            "source_skill_summary": {"type": "string"},
        },
        "required": ["artifact_ref"],
    },
)
async def workflow_template_promote(
    project_id: str,
    artifact_ref: str,
    template_id: str = "",
    name: str = "",
    description: str = "",
    category: str = "user",
    applies_to: str = "",
    version: str = "",
    replace_existing: bool = False,
    source_skill_name: str = "",
    source_skill_scope: str = "",
    source_skill_summary: str = "",
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    try:
        promoted = workflow_template_store.promote_artifact_to_template(
            project_id=project_id,
            artifact_ref=artifact_ref,
            template_id=template_id,
            name=name,
            description=description,
            category=category or "user",
            applies_to=applies_to,
            version=version,
            replace_existing=replace_existing,
            source={
                "agent": "workflow_template_promote",
                "source_skill": {
                    "name": source_skill_name,
                    "scope": source_skill_scope,
                    "summary": source_skill_summary,
                },
            },
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_not_found"}
    except WorkflowAuditError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_audit_failed", "audit": exc.report}
    except (ValueError, json.JSONDecodeError, workflow_template_store.WorkflowTemplateStoreError) as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_error"}
    return {
        **promoted,
        "next_action": "模板已写入 workflow_templates/user/；前端刷新后可直接选择，下载可用 workflow.template.export 或前端下载按钮。",
    }


@register(
    "workflow.template.export",
    description="导出用户 workflow 模板为可下载 JSON 包。",
    tags=["workflow", "artifact", "read", "export"],
    search_hint=(
        "export reusable workflow template json download package "
        "导出 下载 工作流 模板 JSON"
    ),
    usage_hints=[
        "仅导出用户模板；内置模板需要先另存为用户模板。",
    ],
    is_read_only=True,
    is_concurrency_safe=True,
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "version_id": {"type": "string"},
        },
        "required": ["template_id"],
    },
)
async def workflow_template_export(
    project_id: str = "",
    template_id: str = "",
    version_id: str = "",
) -> dict[str, Any]:
    try:
        package = workflow_template_store.export_template_package(template_id, version_id)
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_template_error"}
    return {
        "ok": True,
        "project_id": project_id,
        "template_id": package.get("template_id"),
        "version_id": package.get("version_id"),
        "filename": f"{package.get('template_id') or 'workflow_template'}.openreel-workflow-template.json",
        "package": package,
    }


@register(
    "workflow.materialize",
    description="校验并物化一个 openreel.workflow.v2 文档；仅用于已有 V2 spec，搭建或修改请使用 Workflow Build Mode。",
    tags=["workflow", "write"],
    search_hint="materialize workflow v2 canvas draft nodes edges 物化 工作流 画布 节点 连线",
    usage_hints=[
        "workflow 必须是完整的 openreel.workflow.v2 文档；旧字段、未知字段和包装对象会被拒绝。",
        "inputs 只提供本次运行值，不会写回可复用 spec。",
    ],
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string"},
            "workflow": {
                "type": "object",
                "description": "完整 openreel.workflow.v2 文档。",
                "additionalProperties": True,
            },
            "title": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "origin_x": {"type": "number"},
            "origin_y": {"type": "number"},
            "spacing_x": {"type": "number"},
            "spacing_y": {"type": "number"},
        },
        "required": ["workflow"],
    },
)

async def workflow_materialize(
    project_id: str,
    workflow: dict[str, Any],
    title: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    if not project_id:
        return {
            "ok": False,
            "error": "project_id is required",
            "error_kind": "missing_project_id",
        }
    try:
        template = canvas_workflow_templates.normalize_inline_workflow(
            workflow,
            input_values=_dimension_input_values(
                inputs if isinstance(inputs, dict) else {},
                context,
            ),
        )
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_spec_error",
            "hint": "workflow 只接受 schema openreel.workflow.v2；步骤写 kind/needs/prompt/output，循环写 kind='loop'、foreach 和 steps。",
        }
    required_error = _required_input_error(template, inputs)
    if required_error:
        return required_error
    return await _materialize_template(
        project_id=project_id,
        template=template,
        title=title,
        inputs=inputs,
        origin_x=origin_x,
        origin_y=origin_y,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
    )


@register(
    "workflow.materialize_artifact",
    description="按 workflow spec artifact_ref 物化画布 draft 节点和依赖边；不把完整 spec 放进主上下文。",
    tags=["workflow", "write"],
    search_hint=(
        "materialize workflow spec artifact_ref compiler output canvas graph nodes edges "
        "物化 工作流 spec artifact 引用 编译结果 画布 节点 连线"
    ),
    usage_hints=[
        "已有 artifact_ref 后使用；主 Agent 不需要读取完整 spec。",
        "planner 已完成时，把 planner 输出放入 context；缺少集合时结果会返回 deferred_groups。",
        "物化只创建 draft 节点和依赖边，不生成内容、不运行节点。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "title": {"type": "string"},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "origin_x": {"type": "number"},
            "origin_y": {"type": "number"},
            "spacing_x": {"type": "number"},
            "spacing_y": {"type": "number"},
        },
        "required": ["artifact_ref"],
    },
)
async def workflow_materialize_artifact(
    project_id: str,
    artifact_ref: str,
    title: str = "",
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    origin_x: float = 120,
    origin_y: float = 120,
    spacing_x: float = 360,
    spacing_y: float = 240,
) -> dict[str, Any]:
    if not project_id:
        return {
            "ok": False,
            "error": "project_id is required",
            "error_kind": "missing_project_id",
        }
    try:
        artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
        workflow = artifact["workflow"]
        template = canvas_workflow_templates.normalize_inline_workflow(
            workflow,
            input_values=_dimension_input_values(
                inputs if isinstance(inputs, dict) else {},
                context,
            ),
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_spec_artifact_not_found",
        }
    except (ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_spec_artifact_error",
        }
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "workflow_spec_error",
            "hint": "artifact spec 校验失败；用隔离 spec 修正流程重新提交。",
        }
    required_error = _required_input_error(template, inputs)
    if required_error:
        required_error["artifact_ref"] = artifact_ref
        return required_error
    result = await _materialize_template(
        project_id=project_id,
        template=template,
        title=title,
        inputs=inputs,
        origin_x=origin_x,
        origin_y=origin_y,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
    )
    result["artifact_ref"] = artifact_ref
    return result
