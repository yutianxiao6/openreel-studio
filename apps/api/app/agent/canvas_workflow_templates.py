"""Strict Workflow Spec v2 template catalog and private-plan expansion."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.agent.workflow_execution_plan import compile_private_execution_template
from app.agent.workflow_spec import (
    WORKFLOW_PLAN_VERSION,
    WORKFLOW_SPEC_VERSION,
    WorkflowSpecError,
    compile_workflow_spec,
    parse_workflow_spec,
    workflow_spec_payload,
)


_APP_ROOT = Path(__file__).resolve().parents[1]
_BUILTIN_TEMPLATE_ROOT = _APP_ROOT / "skills"
WORKFLOW_SPEC_PROTOCOL_VERSION = WORKFLOW_SPEC_VERSION
DEFAULT_WORKFLOW_TEMPLATE_ID = "general_short_drama_workflow"
_VALID_NODE_TYPES = {"text", "image", "video", "audio"}
_TEMPLATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,100}$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_INSTANCE_TOKEN_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class WorkflowTemplateError(ValueError):
    """Raised when a v2 workflow cannot be loaded or compiled."""


def _read_template_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowTemplateError(f"Invalid workflow template JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise WorkflowTemplateError(f"Workflow template must be a JSON object: {path}")
    return raw


def _normalize_inline_id(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    raw = _CAMEL_BOUNDARY_RE.sub("_", raw)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    if not slug or not slug[0].isalpha():
        slug = fallback
    return slug[:100].rstrip("_") or fallback


def _plugin_protocol_nodes(plugin_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": node.get("id"),
            "plugin_id": node.get("plugin_id"),
            "plugin_name": node.get("plugin_name"),
            "plugin_version": node.get("plugin_version"),
            "type": node.get("type"),
            "title": node.get("title") or node.get("name"),
            "description": node.get("description") or "",
            "category": node.get("category") or "",
            "inputs": deepcopy(node.get("inputs") or []),
            "outputs": deepcopy(node.get("outputs") or []),
            "settings": deepcopy(node.get("settings") or []),
        }
        for node in plugin_nodes
        if isinstance(node, dict)
    ]


def workflow_protocol_info() -> dict[str, Any]:
    from app.services import workflow_plugins

    plugin_nodes = workflow_plugins.plugin_node_types()
    return {
        "protocol_version": WORKFLOW_SPEC_VERSION,
        "execution_plan_version": WORKFLOW_PLAN_VERSION,
        "step_kinds": ["text", "object", "collection", "image", "video", "audio", "loop", "plugin"],
        "reference_roles": ["vision", "reference", "source"],
        "execution_modes": ["auto", "manual"],
        "error_policies": ["stop", "continue"],
        "available_plugin_nodes": _plugin_protocol_nodes(plugin_nodes),
        "plugin_errors": workflow_plugins.plugin_errors(),
    }


def workflow_protocol_diagnostics(raw: dict[str, Any]) -> dict[str, Any]:
    from app.services import workflow_plugins

    try:
        plan = compile_workflow_spec(raw)
    except (WorkflowSpecError, ValueError) as exc:
        raise WorkflowTemplateError(str(exc)) from exc
    available_plugins: set[str] = set()
    for item in workflow_plugins.plugin_node_types():
        if not isinstance(item, dict):
            continue
        for key in ("id", "plugin_id"):
            value = str(item.get(key) or "").strip()
            if value:
                available_plugins.add(value)
    required_plugins = list(plan.get("requirements", {}).get("plugins") or [])
    missing_plugins = [item for item in required_plugins if item not in available_plugins]
    return {
        "protocol_version": WORKFLOW_SPEC_VERSION,
        "execution_plan_version": WORKFLOW_PLAN_VERSION,
        "plan_hash": plan.get("plan_hash"),
        "requirements": deepcopy(plan.get("requirements") or {}),
        "missing_plugins": missing_plugins,
        "supported": not missing_plugins,
    }


def _ensure_supported(raw: dict[str, Any]) -> dict[str, Any]:
    diagnostics = workflow_protocol_diagnostics(raw)
    missing = diagnostics.get("missing_plugins") or []
    if missing:
        raise WorkflowTemplateError("Workflow requires unavailable plugins: " + ", ".join(missing))
    return diagnostics


def _lookup(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    normalized = _normalize_inline_id(key, fallback=key)
    for candidate, value in payload.items():
        if _normalize_inline_id(candidate, fallback=str(candidate)) == normalized:
            return value
    return None


def _context_value(values: dict[str, Any], key: str) -> Any:
    direct = _lookup(values, key)
    if direct is not None:
        return direct
    for container_key in ("steps", "nodes", "outputs", "context"):
        container = values.get(container_key)
        if isinstance(container, dict):
            result = _lookup(container, key)
            if result is not None:
                return result
    return None


def _path_values(root: Any, path: str) -> list[Any]:
    values = [root]
    for raw_part in [item for item in str(path or "").split(".") if item]:
        wants_items = raw_part.endswith("[]")
        part = raw_part[:-2] if wants_items else raw_part
        next_values: list[Any] = []
        for value in values:
            if isinstance(value, dict):
                child = _lookup(value, part)
                if child is None:
                    continue
                if wants_items and isinstance(child, list):
                    next_values.extend(child)
                else:
                    next_values.append(child)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        child = _lookup(item, part)
                        if child is None:
                            continue
                        if wants_items and isinstance(child, list):
                            next_values.extend(child)
                        else:
                            next_values.append(child)
        values = next_values
        if not values:
            break
    if len(values) == 1 and isinstance(values[0], list):
        return list(values[0])
    return values


def _instance_items(raw_items: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        instance = deepcopy(item) if isinstance(item, dict) else {"item": deepcopy(item)}
        instance.setdefault("index", index)
        instance.setdefault("label", str(instance.get("title") or instance.get("name") or f"实例{index}"))
        result.append(instance)
    return result


def _loop_instances(group: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]] | None:
    foreach = group.get("foreach") if isinstance(group.get("foreach"), dict) else {}
    count = foreach.get("count")
    if count not in (None, ""):
        resolved = count
        if isinstance(count, str):
            resolved = _context_value(values, count)
        if resolved in (None, ""):
            return None
        try:
            total = int(resolved)
        except (TypeError, ValueError) as exc:
            raise WorkflowTemplateError(f"{group.get('id')}.foreach.count must resolve to an integer") from exc
        if total < 1:
            raise WorkflowTemplateError(f"{group.get('id')}.foreach.count must be positive")
        scope_key = str(foreach.get("scope_key") or "index")
        return _instance_items([{scope_key: index} for index in range(1, total + 1)])

    source_step = str(foreach.get("from_step") or "").strip()
    path = str(foreach.get("path") or "").strip()
    if not source_step:
        raise WorkflowTemplateError(f"{group.get('id')}.foreach requires from_step or count")
    source = _context_value(values, source_step)
    if source in (None, "", [], {}):
        return None
    items = _path_values(source, path)
    return _instance_items(items) if items else None


def _instance_suffix(instance: dict[str, Any], index: int, key: str = "") -> str:
    candidates = [
        instance.get(key) if key else None,
        instance.get("id"),
        instance.get("key"),
        instance.get("character_id"),
        instance.get("segment_id"),
        instance.get("episode"),
        instance.get("index"),
    ]
    selected = next((value for value in candidates if value not in (None, "")), index)
    return _normalize_inline_id(selected, fallback=f"i{index}")


def _render_instance_tokens(value: Any, instance: dict[str, Any], item_name: str) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            expression = str(match.group(1) or "").strip()
            parts = [part for part in expression.split(".") if part]
            if parts and parts[0] in {item_name, "instance"}:
                parts = parts[1:]
            elif len(parts) != 1:
                return match.group(0)
            current: Any = instance
            for part in parts:
                if not isinstance(current, dict) or part not in current:
                    return match.group(0)
                current = current[part]
            if isinstance(current, (dict, list)):
                return json.dumps(current, ensure_ascii=False, sort_keys=True)
            return str(current)

        return _INSTANCE_TOKEN_RE.sub(replace, value)
    if isinstance(value, list):
        return [_render_instance_tokens(item, instance, item_name) for item in value]
    if isinstance(value, dict):
        return {key: _render_instance_tokens(item, instance, item_name) for key, item in value.items()}
    return value


def _deferred_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": group.get("id"),
        "title": group.get("title") or group.get("id"),
        "depends_on": deepcopy(group.get("depends_on") or []),
        "foreach": deepcopy(group.get("foreach") or {}),
        "steps": deepcopy(group.get("steps") or []),
        "logical_step_id": group.get("logical_step_id") or group.get("id"),
        "status": "deferred",
    }


def _expand_private_loops(
    steps: list[Any],
    *,
    values: dict[str, Any],
    deferred: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise WorkflowTemplateError(f"Execution step #{index} must be an object")
        children = raw.get("steps")
        if not isinstance(children, list):
            expanded.append(deepcopy(raw))
            continue

        group = deepcopy(raw)
        group_id = str(group.get("id") or f"loop_{index}").strip()
        instances = _loop_instances(group, values)
        if instances is None:
            deferred.append(_deferred_group(group))
            continue
        foreach = group.get("foreach") if isinstance(group.get("foreach"), dict) else {}
        item_name = str(foreach.get("scope_key") or "item").strip() or "item"
        key = str(foreach.get("key") or "").strip()
        child_ids = {
            str(child.get("id") or "").strip()
            for child in children
            if isinstance(child, dict) and str(child.get("id") or "").strip()
        }
        for instance_index, instance in enumerate(instances, start=1):
            suffix = _instance_suffix(instance, instance_index, key)
            local_ids = {child_id: f"{group_id}_{suffix}_{child_id}" for child_id in child_ids}
            for child in children:
                if not isinstance(child, dict):
                    continue
                rendered = _render_instance_tokens(deepcopy(child), instance, item_name)
                template_child_id = str(child.get("id") or "").strip()
                rendered["id"] = local_ids[template_child_id]
                label = str(instance.get("label") or f"实例{instance_index}")
                rendered["title"] = f"{label} · {rendered.get('title') or template_child_id}"
                child_dependencies = [str(item) for item in rendered.get("depends_on") or [] if str(item)]
                rendered["depends_on"] = list(dict.fromkeys([
                    *[str(item) for item in group.get("depends_on") or [] if str(item)],
                    *[local_ids.get(item, item) for item in child_dependencies],
                ]))
                rendered["template_step_id"] = template_child_id
                rendered["repeat_group_id"] = group_id
                rendered["repeat_group_label"] = group.get("title") or group_id
                rendered["repeat_group_index"] = instance_index
                rendered["instance_scope"] = {
                    key: deepcopy(value)
                    for key, value in instance.items()
                    if key not in {"label", "title"}
                }
                rendered["item_name"] = item_name
                expanded.append(rendered)
    return expanded


def _template_input_values(private: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    values = deepcopy(private.get("defaults") or {})
    if isinstance(overrides, dict):
        values.update({str(key): deepcopy(value) for key, value in overrides.items()})
    return values


def _normalize_private_execution_template(
    private: dict[str, Any],
    *,
    input_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template_id = str(private.get("id") or "").strip()
    if not _TEMPLATE_ID_RE.fullmatch(template_id):
        raise WorkflowTemplateError(f"Invalid workflow template id: {template_id!r}")
    values = _template_input_values(private, input_values)
    deferred: list[dict[str, Any]] = []
    expanded = _expand_private_loops(private.get("steps") or [], values=values, deferred=deferred)
    group_ids = {
        str(item.get("id") or "")
        for item in deferred
        if str(item.get("id") or "")
    }
    group_ids.update(
        str(item.get("repeat_group_id") or "")
        for item in expanded
        if str(item.get("repeat_group_id") or "")
    )
    seen: set[str] = set()
    normalized_steps: list[dict[str, Any]] = []
    for item in expanded:
        step_id = str(item.get("id") or "").strip()
        if not _TEMPLATE_ID_RE.fullmatch(step_id):
            raise WorkflowTemplateError(f"Invalid execution step id in {template_id!r}: {step_id!r}")
        if step_id in seen:
            raise WorkflowTemplateError(f"Duplicate execution step id in {template_id!r}: {step_id!r}")
        node_type = str(item.get("node_type") or "").strip().lower()
        if node_type not in _VALID_NODE_TYPES:
            raise WorkflowTemplateError(f"Invalid private node type for {step_id!r}: {node_type!r}")
        dependencies = [str(value) for value in item.get("depends_on") or [] if str(value)]
        unknown = [value for value in dependencies if value not in seen and value not in group_ids]
        if step_id in dependencies:
            raise WorkflowTemplateError(f"Execution step {step_id!r} cannot depend on itself")
        if unknown:
            raise WorkflowTemplateError(
                f"Execution step {step_id!r} references unknown or later dependencies: {', '.join(unknown)}"
            )
        seen.add(step_id)
        normalized_steps.append({
            **deepcopy(item),
            "node_type": node_type,
            "depends_on": list(dict.fromkeys(dependencies)),
            "fields": deepcopy(item.get("fields") or {}),
            "position": deepcopy(item.get("position") or {}),
        })
    return {
        **deepcopy(private),
        "workflow_spec_version": WORKFLOW_SPEC_VERSION,
        "protocol": {
            "protocol_version": WORKFLOW_SPEC_VERSION,
            "execution_plan_version": WORKFLOW_PLAN_VERSION,
            "supported": True,
            "plan_hash": private.get("plan_hash"),
        },
        "steps": normalized_steps,
        "deferred_groups": deferred,
        "input_values": values,
    }


def normalize_inline_workflow(
    raw: dict[str, Any],
    *,
    default_id: str = "model_authored_workflow",
    input_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del default_id
    if not isinstance(raw, dict):
        raise WorkflowTemplateError("workflow must be an object")
    try:
        public = workflow_spec_payload(raw)
        _ensure_supported(public)
        private = compile_private_execution_template(public)
    except (WorkflowSpecError, ValueError) as exc:
        raise WorkflowTemplateError(str(exc)) from exc
    return _normalize_private_execution_template(private, input_values=input_values)


def _normalize_loaded_template(
    raw: dict[str, Any],
    *,
    input_values: dict[str, Any] | None,
    path: str = "",
    scope: str,
    source: str,
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template = normalize_inline_workflow(raw, input_values=input_values)
    template.update({
        "path": path,
        "scope": scope,
        "source": source,
        "downloadable": scope == "user",
    })
    if isinstance(extra_summary, dict):
        for key in ("active_version_id", "versions", "template_source", "source_skill", "downloadable", "source", "path"):
            if key in extra_summary:
                template[key] = deepcopy(extra_summary[key])
    return template


def load_builtin_templates(input_values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    templates = [
        _normalize_loaded_template(
            _read_template_file(path),
            input_values=input_values,
            path=str(path),
            scope="builtin",
            source="builtin_template",
        )
        for path in sorted(_BUILTIN_TEMPLATE_ROOT.glob("*/templates/*.json"))
    ]
    templates.sort(key=lambda item: (item.get("id") != DEFAULT_WORKFLOW_TEMPLATE_ID, item.get("name") or ""))
    return templates


def get_builtin_template(template_id: str, *, input_values: dict[str, Any] | None = None) -> dict[str, Any]:
    wanted = str(template_id or "").strip()
    for template in load_builtin_templates(input_values=input_values):
        if template.get("id") == wanted:
            return template
    raise WorkflowTemplateError(f"Built-in workflow template not found: {wanted}")


def load_user_templates(input_values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from app.agent import workflow_template_store

    templates: list[dict[str, Any]] = []
    for record in workflow_template_store.list_user_template_records():
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        workflow = version.get("workflow") if isinstance(version.get("workflow"), dict) else None
        if not workflow:
            continue
        try:
            templates.append(_normalize_loaded_template(
                workflow,
                input_values=input_values,
                scope="user",
                source="user_template",
                extra_summary=record.get("summary") if isinstance(record.get("summary"), dict) else {},
            ))
        except WorkflowTemplateError:
            continue
    return sorted(templates, key=lambda item: (item.get("name") or "", item.get("id") or ""))


def load_templates(input_values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    user = load_user_templates(input_values=input_values)
    user_ids = {str(item.get("id") or "") for item in user}
    builtin = [item for item in load_builtin_templates(input_values=input_values) if item.get("id") not in user_ids]
    return [*user, *builtin]


def get_template(template_id: str = "", *, input_values: dict[str, Any] | None = None) -> dict[str, Any]:
    wanted = str(template_id or "").strip()
    templates = load_templates(input_values=input_values)
    if not wanted:
        wanted = DEFAULT_WORKFLOW_TEMPLATE_ID
    for template in templates:
        if template.get("id") == wanted:
            return template
    if templates and not template_id:
        return templates[0]
    raise WorkflowTemplateError(f"Workflow template not found: {wanted}")


def missing_required_inputs(template: dict[str, Any], input_values: dict[str, Any] | None = None) -> list[str]:
    values = input_values if isinstance(input_values, dict) else {}
    return [
        key
        for key in template.get("required_inputs") or []
        if _context_value(values, str(key)) in (None, "", [], {})
    ]


def template_input_field_summaries(
    template: dict[str, Any],
    input_values: dict[str, Any] | None = None,
    *,
    only_missing: bool = False,
) -> list[dict[str, Any]]:
    values = input_values if isinstance(input_values, dict) else {}
    inputs = template.get("inputs") if isinstance(template.get("inputs"), dict) else {}
    required = set(template.get("required_inputs") or [])
    fields: list[dict[str, Any]] = []
    for input_id, raw in inputs.items():
        spec = raw if isinstance(raw, dict) else {}
        missing = input_id in required and _context_value(values, input_id) in (None, "", [], {})
        if only_missing and not missing:
            continue
        field = {
            "id": input_id,
            "label": spec.get("label") or input_id,
            "type": spec.get("type") or "text",
            "required": input_id in required,
            "missing": missing,
        }
        for source, target in (("description", "description"), ("default", "default"), ("min", "minimum"), ("max", "maximum"), ("options", "options")):
            if spec.get(source) not in (None, "", [], {}):
                field[target] = deepcopy(spec[source])
        fields.append(field)
    return fields


def _logical_step_summary(step: dict[str, Any]) -> dict[str, Any]:
    kind = str(step.get("kind") or "text")
    summary: dict[str, Any] = {
        "id": step.get("id"),
        "title": step.get("title") or step.get("id"),
        "description": step.get("description") or "",
        "kind": kind,
        "node_type": kind if kind in _VALID_NODE_TYPES else "text",
        "shape": "loop" if kind == "loop" else "collection" if kind in {"object", "collection"} else kind,
        "depends_on": list(step.get("depends_on") or []),
        "execution": step.get("execution") or "auto",
        "on_error": step.get("on_error") or "stop",
    }
    for key in ("prompt", "output", "fields", "uses", "when", "foreach", "plugin", "ui"):
        if step.get(key) not in (None, "", [], {}):
            summary[key] = deepcopy(step[key])
    if isinstance(step.get("steps"), list):
        summary["steps"] = [_logical_step_summary(child) for child in step["steps"] if isinstance(child, dict)]
        summary["has_children"] = True
        summary["child_scope_id"] = str(step.get("id") or "")
    return summary


def template_step_summaries(steps: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        item.setdefault("title", item.get("id"))
        item.setdefault("node_type", "text")
        item.setdefault("depends_on", [])
        summaries.append(item)
        if isinstance(raw.get("steps"), list):
            summaries.extend(template_step_summaries(raw["steps"]))
    return summaries


def _template_graph(steps: list[dict[str, Any]]) -> dict[str, Any]:
    scopes: dict[str, dict[str, Any]] = {}

    def visit(scope_id: str, title: str, items: list[dict[str, Any]]) -> None:
        nodes: list[dict[str, Any]] = []
        for item in items:
            node = {key: deepcopy(value) for key, value in item.items() if key != "steps"}
            nodes.append(node)
            if isinstance(item.get("steps"), list):
                visit(str(item.get("id")), str(item.get("title") or item.get("id")), item["steps"])
        node_ids = {str(item.get("id") or "") for item in nodes}
        edges = [
            {"id": f"execution:{source}->{node.get('id')}", "source": source, "target": node.get("id"), "type": "execution"}
            for node in nodes
            for source in node.get("depends_on") or []
            if source in node_ids
        ]
        scopes[scope_id] = {"id": scope_id, "title": title, "nodes": nodes, "edges": edges}

    visit("root", "模板结构", steps)
    return {"root_scope_id": "root", "scopes": scopes}


def _template_summary_from_raw(
    raw: dict[str, Any],
    *,
    path: str = "",
    scope: str,
    source: str,
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        spec = parse_workflow_spec(raw)
        public = workflow_spec_payload(spec)
        protocol = _ensure_supported(public)
        plan = compile_workflow_spec(spec)
    except (WorkflowSpecError, ValueError) as exc:
        raise WorkflowTemplateError(str(exc)) from exc
    steps = [_logical_step_summary(step) for step in plan["steps"] if isinstance(step, dict)]
    input_schema = {
        key: item.model_dump(by_alias=True, exclude_none=True)
        for key, item in spec.inputs.items()
    }
    summary: dict[str, Any] = {
        "id": spec.id,
        "name": spec.title,
        "title": spec.title,
        "description": spec.description,
        "category": str(spec.ui.get("category") or "workflow"),
        "tags": list(spec.tags),
        "scope": scope,
        "source": source,
        "downloadable": scope == "user",
        "schema": WORKFLOW_SPEC_VERSION,
        "workflow_spec_version": WORKFLOW_SPEC_VERSION,
        "protocol": protocol,
        "requirements": deepcopy(plan.get("requirements") or {}),
        "extensions": deepcopy(spec.extensions),
        "inputs": list(spec.inputs),
        "inputs_schema": input_schema,
        "required_inputs": [key for key, item in spec.inputs.items() if item.required],
        "steps": steps,
        "template_graph": _template_graph(steps),
        "path": path,
        "active_version_id": "",
        "versions": [],
    }
    if isinstance(extra_summary, dict):
        for key in ("active_version_id", "versions", "downloadable", "source_skill", "source", "template_source", "path"):
            if key in extra_summary:
                summary[key] = deepcopy(extra_summary[key])
    return summary


def list_template_summaries() -> list[dict[str, Any]]:
    from app.agent import workflow_template_store

    builtin = [(path, _read_template_file(path)) for path in sorted(_BUILTIN_TEMPLATE_ROOT.glob("*/templates/*.json"))]
    builtin_ids = {str(raw.get("id") or "") for _, raw in builtin}
    summaries: list[dict[str, Any]] = []
    user_ids: set[str] = set()
    for record in workflow_template_store.list_user_template_records():
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        workflow = version.get("workflow") if isinstance(version.get("workflow"), dict) else None
        if not workflow:
            continue
        try:
            summary = _template_summary_from_raw(
                workflow,
                scope="user",
                source="user_template",
                extra_summary=record.get("summary") if isinstance(record.get("summary"), dict) else {},
            )
        except WorkflowTemplateError:
            continue
        user_ids.add(str(summary.get("id") or ""))
        summary["overrides_builtin"] = summary.get("id") in builtin_ids
        summaries.append(summary)
    summaries.extend(
        _template_summary_from_raw(raw, path=str(path), scope="builtin", source="builtin_template")
        for path, raw in builtin
        if raw.get("id") not in user_ids
    )
    summaries.sort(key=lambda item: (
        item.get("scope") != "user",
        item.get("id") != DEFAULT_WORKFLOW_TEMPLATE_ID,
        item.get("name") or "",
    ))
    return deepcopy(summaries)
