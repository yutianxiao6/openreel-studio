"""Persist model-authored workflow specs outside the main agent context."""
from __future__ import annotations

import json
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.agent import canvas_workflow_templates
from app.agent.workflow_audit import ensure_workflow_audit_passes
from app.agent.workflow_spec import WORKFLOW_SPEC_VERSION, compile_workflow_spec, parse_workflow_spec

from app.agent.context_compact import tool_results_dir


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_REF_PREFIX = "workflow_spec:"
_REPAIR_REF_PREFIX = "workflow_repair:"
_STRUCTURAL_PREVIEW_KEYS = {
    "id",
    "name",
    "description",
    "step_count",
    "dimension_count",
    "deferred_group_count",
    "reusable",
    "workflow_spec_version",
    "required_capabilities",
    "required_extensions",
    "extension_ids",
    "protocol",
    "input_ids",
    "required_inputs",
    "first_steps",
    "dimensions",
    "deferred_groups",
    "audit_status",
    "can_save",
    "can_run",
    "recommended_use",
}


def _safe_project_id(project_id: str) -> str:
    return _SAFE_NAME_RE.sub("_", str(project_id or "default")).strip("._") or "default"


def _artifact_dir(project_id: str) -> Path:
    path = tool_results_dir() / _safe_project_id(project_id) / "workflow_specs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _repair_dir(project_id: str) -> Path:
    path = tool_results_dir() / _safe_project_id(project_id) / "workflow_repairs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_ref_name(value: str) -> str:
    name = _SAFE_NAME_RE.sub("_", str(value or "")).strip("._")
    if not name:
        name = f"spec_{uuid.uuid4().hex[:12]}.json"
    if not name.endswith(".json"):
        name += ".json"
    return name


def artifact_ref_from_name(name: str) -> str:
    return _REF_PREFIX + _safe_ref_name(name)


def repair_ref_from_name(name: str) -> str:
    return _REPAIR_REF_PREFIX + _safe_ref_name(name)


def _artifact_path(project_id: str, artifact_ref: str) -> Path:
    raw = str(artifact_ref or "").strip()
    if raw.startswith(_REF_PREFIX):
        raw = raw[len(_REF_PREFIX):]
    name = _safe_ref_name(raw)
    path = (_artifact_dir(project_id) / name).resolve()
    root = _artifact_dir(project_id).resolve()
    if root not in path.parents and path != root:
        raise ValueError("invalid workflow spec artifact ref")
    return path


def _repair_path(project_id: str, repair_ref: str) -> Path:
    raw = str(repair_ref or "").strip()
    if raw.startswith(_REPAIR_REF_PREFIX):
        raw = raw[len(_REPAIR_REF_PREFIX):]
    name = _safe_ref_name(raw)
    path = (_repair_dir(project_id) / name).resolve()
    root = _repair_dir(project_id).resolve()
    if root not in path.parents and path != root:
        raise ValueError("invalid workflow repair ref")
    return path


def _list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _dict_len(value: Any) -> int:
    return len(value) if isinstance(value, dict) else 0


def _workflow_input_ids(workflow: dict[str, Any]) -> list[str]:
    inputs = workflow.get("inputs")
    if isinstance(inputs, dict):
        return [str(key) for key in inputs.keys() if str(key).strip()][:16]
    if isinstance(inputs, list):
        result: list[str] = []
        for item in inputs:
            if isinstance(item, dict):
                value = item.get("id") or item.get("name") or item.get("key")
            else:
                value = item
            text = str(value or "").strip()
            if text:
                result.append(text)
        return result[:16]
    return []


def workflow_spec_preview(workflow: dict[str, Any], *, normalized: dict[str, Any] | None = None) -> dict[str, Any]:
    del normalized
    spec = parse_workflow_spec(workflow)
    plan = compile_workflow_spec(spec)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    extensions = spec.extensions
    return {
        "id": spec.id,
        "name": spec.title,
        "title": spec.title,
        "description": spec.description,
        "schema": WORKFLOW_SPEC_VERSION,
        "extension_ids": list(extensions.keys())[:24],
        "protocol": {
            "protocol_version": WORKFLOW_SPEC_VERSION,
            "execution_plan_version": plan.get("schema"),
            "supported": True,
            "plan_hash": plan.get("plan_hash"),
        },
        "step_count": len(steps),
        "requirements": deepcopy(plan.get("requirements") or {}),
        "input_ids": list(spec.inputs),
        "required_inputs": [key for key, item in spec.inputs.items() if item.required],
        "first_steps": [
            {
                "id": step.get("id"),
                "title": step.get("title") or step.get("id"),
                "kind": step.get("kind"),
                "depends_on": step.get("depends_on") or [],
                "ui": step.get("ui") if isinstance(step.get("ui"), dict) else {},
            }
            for step in steps[:8]
            if isinstance(step, dict)
        ],
    }


def save_workflow_spec_artifact(
    *,
    project_id: str,
    workflow: dict[str, Any],
    normalized: dict[str, Any] | None = None,
    self_check: dict[str, Any] | None = None,
    user_preview: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}.json"
    path = _artifact_path(project_id, name)
    audit = ensure_workflow_audit_passes(
        workflow,
        normalized=normalized,
        sample_inputs=sample_inputs or {},
    )
    preview = workflow_spec_preview(workflow, normalized=normalized)
    if isinstance(user_preview, dict):
        preview = {
            **{
                k: v
                for k, v in user_preview.items()
                if k not in _STRUCTURAL_PREVIEW_KEYS and v not in (None, "", [], {})
            },
            **preview,
        }
    preview["audit_status"] = audit.get("status")
    preview["can_save"] = bool(audit.get("can_save"))
    preview["can_run"] = bool(audit.get("can_run"))
    preview["recommended_use"] = audit.get("recommended_use") or ("runnable" if audit.get("can_run") else "draft_only")
    payload = {
        "kind": "workflow_spec",
        "schema_version": "workflow_spec_artifact_v1",
        "created_at_ms": int(time.time() * 1000),
        "project_id": project_id,
        "reusable": bool(workflow.get("reusable", True)),
        "workflow": workflow,
        "sample_inputs": sample_inputs or {},
        "normalized_preview": workflow_spec_preview(workflow, normalized=normalized),
        "preview": preview,
        "audit": audit,
        "self_check": self_check or {},
        "source": source or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return {
        "artifact_ref": artifact_ref_from_name(path.name),
        "preview": preview,
        "audit": audit,
        "self_check": payload["self_check"],
    }


def load_workflow_spec_artifact(project_id: str, artifact_ref: str) -> dict[str, Any]:
    path = _artifact_path(project_id, artifact_ref)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"workflow spec artifact not found: {artifact_ref}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("kind") != "workflow_spec":
        raise ValueError("invalid workflow spec artifact")
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("workflow spec artifact has no workflow object")
    return payload


def save_workflow_repair_candidate(
    *,
    project_id: str,
    workflow: dict[str, Any],
    sample_inputs: dict[str, Any] | None = None,
    audit: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    applied: list[dict[str, Any]] | None = None,
    user_preview: dict[str, Any] | None = None,
    self_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}.json"
    path = _repair_path(project_id, name)
    preview = workflow_spec_preview(workflow)
    if isinstance(user_preview, dict):
        preview = {
            **{
                k: v
                for k, v in user_preview.items()
                if k not in _STRUCTURAL_PREVIEW_KEYS and v not in (None, "", [], {})
            },
            **preview,
        }
    if isinstance(audit, dict):
        preview["audit_status"] = audit.get("status")
        preview["can_save"] = bool(audit.get("can_save"))
        preview["can_run"] = bool(audit.get("can_run"))
        preview["recommended_use"] = audit.get("recommended_use") or "blocked"
    payload = {
        "kind": "workflow_repair_candidate",
        "schema_version": "workflow_repair_candidate_v1",
        "created_at_ms": int(time.time() * 1000),
        "project_id": project_id,
        "workflow": workflow,
        "sample_inputs": sample_inputs or {},
        "preview": preview,
        "audit": audit or {},
        "applied": applied or [],
        "self_check": self_check or {},
        "source": source or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return {
        "repair_ref": repair_ref_from_name(path.name),
        "preview": preview,
        "audit": payload["audit"],
        "self_check": payload["self_check"],
    }


def load_workflow_repair_candidate(project_id: str, repair_ref: str) -> dict[str, Any]:
    path = _repair_path(project_id, repair_ref)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"workflow repair candidate not found: {repair_ref}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("kind") != "workflow_repair_candidate":
        raise ValueError("invalid workflow repair candidate")
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("workflow repair candidate has no workflow object")
    return payload
