"""Root-level reusable workflow template library.

User templates live under PROJECT_ROOT/workflow_templates as workflow spec
JSON files. Built-in templates are loaded from app skills separately.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import settings
from app.agent.workflow_audit import WorkflowAuditError, ensure_workflow_audit_passes


TEMPLATE_LIBRARY_SCHEMA_VERSION = "workflow_template_library_v1"
TEMPLATE_VERSION_SCHEMA_VERSION = "workflow_template_version_v1"
USER_WORKFLOW_TEMPLATE_RELATIVE_DIR = Path("workflow_templates")
FILE_TEMPLATE_VERSION_ID = "file"
WORKFLOW_TEMPLATE_METADATA_KEY = "workflow_template"
WORKFLOW_TEMPLATE_METADATA_SCHEMA_VERSION = "workflow_template_file_meta_v1"
_TEMPLATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_SAFE_TEXT_RE = re.compile(r"[^a-z0-9_]+")
_STRUCTURAL_PREVIEW_KEYS = {
    "id",
    "name",
    "description",
    "category",
    "applies_to",
    "version",
    "workflow_spec_version",
    "required_capabilities",
    "required_extensions",
    "protocol",
    "inputs",
    "inputs_schema",
    "required_inputs",
    "steps",
    "template_graph",
    "audit_status",
    "can_save",
    "can_run",
    "recommended_use",
}


class WorkflowTemplateStoreError(ValueError):
    """Raised when a user workflow template cannot be stored or loaded."""


def workflow_template_library_root() -> Path:
    root = Path(settings.PROJECT_ROOT).expanduser().resolve() / USER_WORKFLOW_TEMPLATE_RELATIVE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _template_roots(*, include_legacy: bool = True) -> list[Path]:
    return [workflow_template_library_root()]


def _template_file_path(template_id: str) -> Path:
    normalized = normalize_template_id(template_id)
    root = workflow_template_library_root()
    path = (root / f"{normalized}.json").resolve()
    if root not in path.parents:
        raise WorkflowTemplateStoreError("invalid workflow template file path")
    return path


def _template_root(template_id: str, *, root: Path | None = None) -> Path:
    normalized = normalize_template_id(template_id)
    base = (root or workflow_template_library_root()).resolve()
    path = (base / normalized).resolve()
    if base not in path.parents and path != base:
        raise WorkflowTemplateStoreError("invalid workflow template path")
    return path


def _versions_root(template_id: str, *, root: Path | None = None) -> Path:
    path = _template_root(template_id, root=root) / "versions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(template_id: str, *, root: Path | None = None) -> Path:
    return _template_root(template_id, root=root) / "manifest.json"


def _version_path(template_id: str, version_id: str, *, root: Path | None = None) -> Path:
    safe_version = _safe_version_id(version_id)
    versions_root = _versions_root(template_id, root=root).resolve()
    path = (versions_root / f"{safe_version}.json").resolve()
    if versions_root not in path.parents and path != versions_root:
        raise WorkflowTemplateStoreError("invalid workflow template version path")
    return path


def normalize_template_id(value: Any, *, fallback_name: str = "workflow_template") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raw = fallback_name
    raw = raw.replace("-", "_").replace(" ", "_")
    normalized = _SAFE_TEXT_RE.sub("_", raw).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"workflow_{normalized or uuid.uuid4().hex[:8]}"
    normalized = normalized[:80].strip("_") or "workflow_template"
    if _TEMPLATE_ID_RE.fullmatch(normalized):
        return normalized
    candidate = f"workflow_{hashlib.sha1(raw.encode('utf-8', errors='ignore')).hexdigest()[:10]}"
    if not _TEMPLATE_ID_RE.fullmatch(candidate):
        raise WorkflowTemplateStoreError("invalid workflow template id")
    return candidate


def _template_exists(template_id: str) -> bool:
    normalized = normalize_template_id(template_id)
    for root in _template_roots(include_legacy=True):
        if (root / f"{normalized}.json").exists():
            return True
        if (root / normalized / "manifest.json").exists():
            return True
    return False


def unique_template_id(preferred_id: Any = "", *, name: Any = "") -> str:
    base = normalize_template_id(preferred_id or name or "workflow_template")
    if not _template_exists(base):
        return base
    suffix = 2
    while suffix < 1000:
        suffix_text = f"_{suffix}"
        candidate = f"{base[:80 - len(suffix_text)]}{suffix_text}"
        if not _template_exists(candidate):
            return candidate
        suffix += 1
    return normalize_template_id(f"{base}_{uuid.uuid4().hex[:8]}")


def _safe_version_id(value: Any = "") -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    safe = _SAFE_TEXT_RE.sub("_", raw).strip("_")
    if safe and re.fullmatch(r"[a-z0-9_]{1,80}", safe):
        return safe
    return f"v{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowTemplateStoreError(f"workflow template file not found: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowTemplateStoreError(f"invalid workflow template JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise WorkflowTemplateStoreError("workflow template payload must be an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")


def _load_manifest(template_id: str) -> dict[str, Any]:
    manifest = _load_json(_manifest_path(template_id))
    if manifest.get("kind") != "workflow_template":
        raise WorkflowTemplateStoreError("invalid workflow template manifest")
    return manifest


def _load_version_payload(template_id: str, version_id: str) -> dict[str, Any]:
    version = _load_json(_version_path(template_id, version_id))
    if version.get("kind") != "workflow_template_version":
        raise WorkflowTemplateStoreError("invalid workflow template version")
    workflow = version.get("workflow")
    if not isinstance(workflow, dict):
        raise WorkflowTemplateStoreError("workflow template version has no workflow object")
    return version


def _active_version_id(manifest: dict[str, Any], version_id: str = "") -> str:
    wanted = str(version_id or "").strip()
    if wanted:
        return _safe_version_id(wanted)
    active = str(manifest.get("active_version_id") or "").strip()
    if active:
        return _safe_version_id(active)
    versions = manifest.get("versions")
    if isinstance(versions, list) and versions:
        latest = versions[-1] if isinstance(versions[-1], dict) else {}
        latest_id = str(latest.get("version_id") or "").strip()
        if latest_id:
            return _safe_version_id(latest_id)
    raise WorkflowTemplateStoreError("workflow template has no active version")


def _summary_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    versions = [item for item in manifest.get("versions") or [] if isinstance(item, dict)]
    active_version_id = str(manifest.get("active_version_id") or "").strip()
    active = next((item for item in versions if str(item.get("version_id") or "") == active_version_id), versions[-1] if versions else {})
    preview = active.get("preview") if isinstance(active.get("preview"), dict) else {}
    summary = {
        "id": str(manifest.get("template_id") or preview.get("id") or "").strip(),
        "name": str(manifest.get("name") or preview.get("name") or "").strip(),
        "description": str(manifest.get("description") or preview.get("description") or "").strip(),
        "category": str(manifest.get("category") or preview.get("category") or "user").strip() or "user",
        "applies_to": str(manifest.get("applies_to") or preview.get("applies_to") or "").strip(),
        "version": str(active.get("version") or active_version_id or "1").strip(),
        "scope": "user",
        "source": "user_template",
        "downloadable": True,
        "active_version_id": active_version_id,
        "versions": [
            {
                "version_id": str(item.get("version_id") or ""),
                "version": str(item.get("version") or ""),
                "created_at_ms": item.get("created_at_ms"),
            }
            for item in versions
        ],
    }
    for key, value in preview.items():
        if key in _STRUCTURAL_PREVIEW_KEYS or key not in summary:
            summary[key] = deepcopy(value)
    summary["scope"] = "user"
    summary["source"] = "user_template"
    summary["downloadable"] = True
    summary["active_version_id"] = active_version_id
    for source_payload in (manifest.get("source"), active.get("source")):
        if not isinstance(source_payload, dict):
            continue
        source_skill = source_payload.get("source_skill")
        if isinstance(source_skill, dict):
            summary["source_skill"] = deepcopy(source_skill)
            break
    return summary


def _preview_workflow(workflow: dict[str, Any], sample_inputs: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from app.agent import canvas_workflow_templates, workflow_spec_artifacts

    normalized = canvas_workflow_templates.normalize_inline_workflow(workflow, input_values=sample_inputs or {})
    audit = ensure_workflow_audit_passes(workflow, normalized=normalized, sample_inputs=sample_inputs or {})
    preview = workflow_spec_artifacts.workflow_spec_preview(workflow, normalized=normalized)
    preview["audit_status"] = audit.get("status")
    preview["can_save"] = bool(audit.get("can_save"))
    preview["can_run"] = bool(audit.get("can_run"))
    preview["recommended_use"] = audit.get("recommended_use") or ("runnable" if audit.get("can_run") else "draft_only")
    return normalized, preview, audit


def _record_sort_key(record: dict[str, Any]) -> tuple[str, str, str]:
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    return (
        str(summary.get("category") or ""),
        str(summary.get("name") or ""),
        str(summary.get("id") or ""),
    )


def _workflow_payload_from_template_file(
    payload: dict[str, Any],
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    sample_inputs: dict[str, Any] = {}
    preview: dict[str, Any] = {}
    self_check: dict[str, Any] = {}
    source: dict[str, Any] = {"source": "workflow_template_file", "path": str(path)}
    kind = str(payload.get("kind") or "").strip()
    workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else None
    if kind == "openreel.workflow_template.export" and workflow:
        version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        sample_inputs = version.get("sample_inputs") if isinstance(version.get("sample_inputs"), dict) else {}
        self_check = version.get("self_check") if isinstance(version.get("self_check"), dict) else {}
        source = version.get("source") if isinstance(version.get("source"), dict) else source
    elif kind == "workflow_template_version" and workflow:
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        sample_inputs = payload.get("sample_inputs") if isinstance(payload.get("sample_inputs"), dict) else {}
        self_check = payload.get("self_check") if isinstance(payload.get("self_check"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else source
    elif workflow and not isinstance(payload.get("steps"), list):
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        sample_inputs = payload.get("sample_inputs") if isinstance(payload.get("sample_inputs"), dict) else {}
        self_check = payload.get("self_check") if isinstance(payload.get("self_check"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else source
    else:
        workflow = payload
    if not isinstance(workflow, dict):
        raise WorkflowTemplateStoreError(f"workflow template file has no workflow object: {path.name}")
    x_openreel = workflow.get("x-openreel") if isinstance(workflow.get("x-openreel"), dict) else {}
    template_meta = x_openreel.get(WORKFLOW_TEMPLATE_METADATA_KEY) if isinstance(x_openreel, dict) else None
    if isinstance(template_meta, dict):
        if not sample_inputs and isinstance(template_meta.get("sample_inputs"), dict):
            sample_inputs = template_meta["sample_inputs"]
        if not preview and isinstance(template_meta.get("preview"), dict):
            preview = template_meta["preview"]
        if not self_check and isinstance(template_meta.get("self_check"), dict):
            self_check = template_meta["self_check"]
        if isinstance(template_meta.get("source"), dict):
            source = template_meta["source"]
        elif isinstance(template_meta.get("source_skill"), dict):
            source = {**source, "source_skill": template_meta["source_skill"]}
    return deepcopy(workflow), deepcopy(sample_inputs), deepcopy(preview), deepcopy(self_check), deepcopy(source)


def _record_from_template_file(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    workflow, sample_inputs, user_preview, self_check, source = _workflow_payload_from_template_file(payload, path)
    normalized_id = normalize_template_id(workflow.get("id") or path.stem)
    workflow["id"] = normalized_id
    workflow.setdefault("name", path.stem)
    workflow.setdefault("reusable", True)
    normalized, structural_preview, audit = _preview_workflow(workflow, sample_inputs)
    template_name = str(workflow.get("name") or structural_preview.get("name") or normalized_id).strip()
    description = str(workflow.get("description") or structural_preview.get("description") or "").strip()
    category = str(workflow.get("category") or structural_preview.get("category") or "user").strip() or "user"
    applies_to = str(workflow.get("applies_to") or structural_preview.get("applies_to") or "").strip()
    merged_preview = {
        **{
            key: value
            for key, value in user_preview.items()
            if key not in _STRUCTURAL_PREVIEW_KEYS and value not in (None, "", [], {})
        },
        **structural_preview,
        "id": normalized_id,
        "name": template_name,
        "description": description,
        "category": category,
        "applies_to": applies_to,
        "scope": "user",
        "source": "user_template_file",
        "template_source": "project_root_spec",
        "downloadable": True,
    }
    modified_ms = int(path.stat().st_mtime * 1000) if path.exists() else _now_ms()
    version_id = FILE_TEMPLATE_VERSION_ID
    version_label = str(workflow.get("version") or "file").strip() or "file"
    manifest = {
        "kind": "workflow_template",
        "schema_version": TEMPLATE_LIBRARY_SCHEMA_VERSION,
        "template_id": normalized_id,
        "name": template_name,
        "description": description,
        "category": category,
        "applies_to": applies_to,
        "created_at_ms": modified_ms,
        "updated_at_ms": modified_ms,
        "active_version_id": version_id,
        "versions": [
            {
                "version_id": version_id,
                "version": version_label,
                "created_at_ms": modified_ms,
                "preview": merged_preview,
                "source": {
                    **deepcopy(source),
                    "path": str(path),
                    "template_source": "project_root_spec",
                },
            }
        ],
        "source": {
            **deepcopy(source),
            "path": str(path),
            "template_source": "project_root_spec",
        },
    }
    summary = _summary_from_manifest(manifest)
    summary["source"] = "user_template_file"
    summary["template_source"] = "project_root_spec"
    summary["path"] = str(path)
    source_skill = source.get("source_skill") if isinstance(source, dict) else None
    if isinstance(source_skill, dict):
        summary["source_skill"] = deepcopy(source_skill)
    version = {
        "kind": "workflow_template_version",
        "schema_version": TEMPLATE_VERSION_SCHEMA_VERSION,
        "template_id": normalized_id,
        "version_id": version_id,
        "version": version_label,
        "created_at_ms": modified_ms,
        "workflow": workflow,
        "sample_inputs": deepcopy(sample_inputs),
        "normalized_preview": structural_preview,
        "preview": merged_preview,
        "self_check": deepcopy(self_check),
        "audit": deepcopy(audit),
        "source": {
            **deepcopy(source),
            "path": str(path),
            "template_source": "project_root_spec",
        },
        "validation": {
            "ok": True,
            "workflow_id": normalized.get("id"),
            "step_count": len(normalized.get("steps") or []),
        },
    }
    return {"manifest": manifest, "version": version, "summary": summary}


def _record_from_manifest_path(manifest_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    if manifest.get("kind") != "workflow_template":
        raise WorkflowTemplateStoreError("invalid workflow template manifest")
    version_id = _active_version_id(manifest)
    template_id = str(manifest.get("template_id") or "")
    root = manifest_path.parent.parent
    version = _load_json(_version_path(template_id, version_id, root=root))
    return {
        "manifest": manifest,
        "version": version,
        "summary": _summary_from_manifest(manifest),
    }


def list_user_template_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _template_roots(include_legacy=True):
        for path in sorted(root.glob("*.json")):
            try:
                record = _record_from_template_file(path)
                template_id = str(record.get("summary", {}).get("id") or "")
                if not template_id or template_id in seen:
                    continue
                seen.add(template_id)
                records.append(record)
            except (OSError, WorkflowTemplateStoreError, WorkflowAuditError):
                continue
        for manifest_path in sorted(root.glob("*/manifest.json")):
            try:
                record = _record_from_manifest_path(manifest_path)
                template_id = str(record.get("summary", {}).get("id") or "")
                if not template_id or template_id in seen:
                    continue
                seen.add(template_id)
                records.append(record)
            except (OSError, WorkflowTemplateStoreError, WorkflowAuditError):
                continue
    records.sort(key=_record_sort_key)
    return records


def list_user_template_summaries() -> list[dict[str, Any]]:
    return [deepcopy(record.get("summary") or {}) for record in list_user_template_records()]


def load_user_template(template_id: str, version_id: str = "") -> dict[str, Any]:
    normalized = normalize_template_id(template_id)
    wanted_version = str(version_id or "").strip()
    for record in list_user_template_records():
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        if str(summary.get("id") or "") != normalized:
            continue
        active_version = str(summary.get("active_version_id") or "")
        if wanted_version and wanted_version not in {active_version, FILE_TEMPLATE_VERSION_ID}:
            break
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        return {
            "manifest": deepcopy(record.get("manifest") or {}),
            "version": deepcopy(version),
            "summary": deepcopy(summary),
            "workflow": deepcopy(version.get("workflow") or {}),
            "sample_inputs": deepcopy(version.get("sample_inputs") or {}),
            "preview": deepcopy(version.get("preview") or {}),
            "self_check": deepcopy(version.get("self_check") or {}),
            "audit": deepcopy(version.get("audit") or {}),
            "source": deepcopy(version.get("source") or {}),
        }
    # Fallback for old manifest versions when a specific non-active version is requested.
    if wanted_version:
        for root in _template_roots(include_legacy=True):
            manifest_path = root / normalized / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = _load_json(manifest_path)
            active_version = _active_version_id(manifest, wanted_version)
            version = _load_json(_version_path(normalized, active_version, root=root))
            summary = _summary_from_manifest(manifest)
            return {
                "manifest": manifest,
                "version": version,
                "summary": summary,
                "workflow": deepcopy(version.get("workflow") or {}),
                "sample_inputs": deepcopy(version.get("sample_inputs") or {}),
                "preview": deepcopy(version.get("preview") or {}),
                "self_check": deepcopy(version.get("self_check") or {}),
                "audit": deepcopy(version.get("audit") or {}),
                "source": deepcopy(version.get("source") or {}),
            }
    raise WorkflowTemplateStoreError(f"workflow template file not found: {normalized}")


def save_user_template(
    *,
    workflow: dict[str, Any],
    template_id: str = "",
    name: str = "",
    description: str = "",
    category: str = "user",
    applies_to: str = "",
    version: str = "",
    source: dict[str, Any] | None = None,
    sample_inputs: dict[str, Any] | None = None,
    self_check: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        raise WorkflowTemplateStoreError("workflow must be an object")
    normalized, structural_preview, audit = _preview_workflow(workflow, sample_inputs)
    template_name = str(name or workflow.get("name") or structural_preview.get("name") or "未命名流程").strip()
    normalized_id = normalize_template_id(template_id or workflow.get("id") or template_name)
    if _template_exists(normalized_id) and not replace_existing:
        normalized_id = unique_template_id(normalized_id)
    workflow_to_store = deepcopy(workflow)
    workflow_to_store["id"] = normalized_id
    workflow_to_store["name"] = template_name
    workflow_to_store["description"] = str(description or workflow_to_store.get("description") or structural_preview.get("description") or "").strip()
    workflow_to_store["category"] = str(category or workflow_to_store.get("category") or "user").strip() or "user"
    workflow_to_store["applies_to"] = str(applies_to or workflow_to_store.get("applies_to") or "").strip()
    if version:
        workflow_to_store["version"] = str(version).strip()
    workflow_to_store["reusable"] = True

    normalized, structural_preview, audit = _preview_workflow(workflow_to_store, sample_inputs)
    merged_preview = {
        **{
            key: value
            for key, value in (preview or {}).items()
            if key not in _STRUCTURAL_PREVIEW_KEYS and value not in (None, "", [], {})
        },
        **structural_preview,
        "id": normalized_id,
        "name": template_name,
        "description": str(description or workflow_to_store.get("description") or structural_preview.get("description") or "").strip(),
        "category": str(category or workflow_to_store.get("category") or "user").strip() or "user",
        "applies_to": str(applies_to or workflow_to_store.get("applies_to") or "").strip(),
        "scope": "user",
        "source": "user_template",
        "downloadable": True,
    }
    x_openreel = workflow_to_store.get("x-openreel") if isinstance(workflow_to_store.get("x-openreel"), dict) else {}
    workflow_to_store["x-openreel"] = {
        **deepcopy(x_openreel),
        WORKFLOW_TEMPLATE_METADATA_KEY: {
            "schema_version": WORKFLOW_TEMPLATE_METADATA_SCHEMA_VERSION,
            "saved_at_ms": _now_ms(),
            "source": deepcopy(source or {}),
            "sample_inputs": deepcopy(sample_inputs or {}),
            "self_check": deepcopy(self_check or {}),
            "audit": deepcopy(audit),
            "preview": deepcopy(merged_preview),
        },
    }
    path = _template_file_path(normalized_id)
    _write_json(path, workflow_to_store)
    record = _record_from_template_file(path)
    summary = deepcopy(record.get("summary") or {})
    summary["source_skill"] = deepcopy((source or {}).get("source_skill")) if isinstance((source or {}).get("source_skill"), dict) else {}
    return {
        "ok": True,
        "template_id": normalized_id,
        "version_id": summary.get("active_version_id") or FILE_TEMPLATE_VERSION_ID,
        "summary": summary,
        "preview": merged_preview,
        "audit": audit,
        "storage_path": str(path),
        "validation": {
            "ok": True,
            "workflow_id": normalized.get("id"),
            "step_count": len(normalized.get("steps") or []),
            "dimension_count": len(normalized.get("dimensions") or {}),
            "deferred_group_count": len(normalized.get("deferred_groups") or []),
            "audit": {
                "status": audit.get("status"),
                "can_save": audit.get("can_save"),
                "can_run": audit.get("can_run"),
                "recommended_use": audit.get("recommended_use") or "",
                "severity_counts": audit.get("severity_counts") or {},
            },
        },
    }


def promote_artifact_to_template(
    *,
    project_id: str,
    artifact_ref: str,
    template_id: str = "",
    name: str = "",
    description: str = "",
    category: str = "user",
    applies_to: str = "",
    version: str = "",
    source: dict[str, Any] | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    from app.agent import workflow_spec_artifacts

    artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
    workflow = artifact.get("workflow")
    if not isinstance(workflow, dict):
        raise WorkflowTemplateStoreError("workflow spec artifact has no workflow object")
    source_payload = {
        "project_id": project_id,
        "artifact_ref": artifact_ref,
        "artifact_source": deepcopy(artifact.get("source") or {}),
        **(deepcopy(source or {})),
    }
    return save_user_template(
        workflow=workflow,
        template_id=template_id or workflow.get("id") or artifact.get("preview", {}).get("id") or "",
        name=name or artifact.get("preview", {}).get("name") or workflow.get("name") or "",
        description=description or artifact.get("preview", {}).get("description") or workflow.get("description") or "",
        category=category,
        applies_to=applies_to,
        version=version,
        source=source_payload,
        sample_inputs=artifact.get("sample_inputs") if isinstance(artifact.get("sample_inputs"), dict) else {},
        self_check=artifact.get("self_check") if isinstance(artifact.get("self_check"), dict) else {},
        preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {},
        replace_existing=replace_existing,
    )


def clone_template_to_artifact(
    *,
    project_id: str,
    template_id: str,
    version_id: str = "",
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.agent import canvas_workflow_templates, workflow_spec_artifacts

    try:
        loaded = load_user_template(template_id, version_id)
    except WorkflowTemplateStoreError:
        workflow = canvas_workflow_templates.get_template(template_id)
        sample_inputs: dict[str, Any] = {}
        normalized = canvas_workflow_templates.normalize_inline_workflow(workflow, input_values=sample_inputs)
        preview = workflow_spec_artifacts.workflow_spec_preview(workflow, normalized=normalized)
        summary = {
            **preview,
            "id": workflow.get("id") or template_id,
            "name": workflow.get("name") or preview.get("name") or template_id,
            "active_version_id": workflow.get("active_version_id") or "",
            "scope": workflow.get("scope") or "builtin",
            "source": workflow.get("source") or "builtin_template",
        }
        self_check: dict[str, Any] = {}
    else:
        workflow = loaded["workflow"]
        sample_inputs = loaded.get("sample_inputs") if isinstance(loaded.get("sample_inputs"), dict) else {}
        normalized = canvas_workflow_templates.normalize_inline_workflow(workflow, input_values=sample_inputs)
        preview = loaded.get("preview") if isinstance(loaded.get("preview"), dict) else {}
        summary = loaded["summary"] if isinstance(loaded.get("summary"), dict) else {}
        self_check = loaded.get("self_check") if isinstance(loaded.get("self_check"), dict) else {}
    artifact = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id=project_id,
        workflow=workflow,
        normalized=normalized,
        self_check=self_check,
        user_preview=preview,
        sample_inputs=sample_inputs,
        source={
            "agent": "workflow_template_store",
            "template_id": summary.get("id"),
            "version_id": summary.get("active_version_id"),
            "template_scope": summary.get("scope") or workflow.get("scope") or "",
            **(deepcopy(source or {})),
        },
    )
    return {
        "ok": True,
        "template_id": summary.get("id"),
        "version_id": summary.get("active_version_id"),
        "artifact_ref": artifact["artifact_ref"],
        "preview": artifact["preview"],
        "audit": artifact.get("audit") or {},
        "self_check": artifact.get("self_check") or {},
    }


def export_template_package(template_id: str, version_id: str = "") -> dict[str, Any]:
    loaded = load_user_template(template_id, version_id)
    manifest = deepcopy(loaded["manifest"])
    version = deepcopy(loaded["version"])
    return {
        "kind": "openreel.workflow_template.export",
        "schema_version": "workflow_template_export_v1",
        "exported_at_ms": _now_ms(),
        "template_id": manifest.get("template_id"),
        "version_id": version.get("version_id"),
        "manifest": manifest,
        "version": version,
        "workflow": deepcopy(version.get("workflow") or {}),
        "preview": deepcopy(version.get("preview") or {}),
    }


def candidate_summaries_for_skill(
    *,
    skill_name: str = "",
    skill_summary: str = "",
    user_goal: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    from app.agent import canvas_workflow_templates

    query_text = f"{skill_name} {skill_summary} {user_goal}".strip().lower()
    query_tokens = _tokenize(query_text)
    raw_skill_name = str(skill_name or "").strip().lower().replace("-", "_").replace(" ", "_")
    normalized_skill_name = normalize_template_id(raw_skill_name) if _TEMPLATE_ID_RE.fullmatch(raw_skill_name) else ""
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for summary in canvas_workflow_templates.list_template_summaries():
        haystack_text = " ".join(
            str(summary.get(key) or "")
            for key in ("id", "name", "description", "category", "applies_to", "scope", "source")
        ).lower()
        haystack = _tokenize(haystack_text)
        overlap = len(query_tokens & haystack)
        overlap += sum(2 for token in query_tokens if token and token in haystack_text)
        overlap += sum(1 for token in haystack if token and token in query_text)
        if normalized_skill_name and str(summary.get("id") or "") == normalized_skill_name:
            overlap += 1000
        source = summary.get("source_skill")
        if isinstance(source, dict):
            source_text = json.dumps(source, ensure_ascii=False).lower()
            overlap += len(query_tokens & _tokenize(source_text))
            if str(source.get("name") or "").strip() == str(skill_name or "").strip():
                overlap += 1000
        if overlap > 0 or not query_tokens:
            scope_rank = 0 if str(summary.get("scope") or "") == "user" else 1
            candidates.append((overlap, scope_rank, summary))
    candidates.sort(key=lambda item: (-item[0], item[1], str(item[2].get("name") or ""), str(item[2].get("id") or "")))
    return [deepcopy(item[2]) | {"match_score": item[0]} for item in candidates[: max(1, min(limit, 20))]]


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", str(text or "").lower()))
    return {token[:80] for token in tokens}
