"""Canvas workflow template loading and normalization.

Templates are data, not routing logic. The Agent chooses when a template fits;
this module only turns a selected template into draft canvas nodes.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.agent.workflow_authoring_spec import (
    WorkflowAuthoringSpecError,
    compile_authoring_workflow,
    is_authoring_workflow,
)


_APP_ROOT = Path(__file__).resolve().parents[1]
_BUILTIN_TEMPLATE_ROOT = _APP_ROOT / "skills"
WORKFLOW_SPEC_PROTOCOL_VERSION = "openreel.workflow.v1"
DEFAULT_WORKFLOW_TEMPLATE_ID = "general_short_drama_workflow"
_VALID_NODE_TYPES = {"text", "image", "video", "audio"}
_TEMPLATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_AUTHORING_ONLY_STEP_KEYS = {"needs", "for_each"}
_BUILTIN_WORKFLOW_EXTENSION_IDS = {"openreel.core"}
_BUILTIN_WORKFLOW_CAPABILITIES = {
    "core.artifact",
    "core.canvas.materialize",
    "core.context_refs",
    "core.depends_on",
    "core.depends_on_previous",
    "core.dimensions",
    "core.foreach",
    "core.layout_after",
    "core.node.audio",
    "core.node.image",
    "core.node.text",
    "core.node.video",
    "core.output_mode.json",
    "core.prompt_template",
    "core.reference_selectors",
    "core.reads_from",
    "core.repeat",
    "core.runner.node_run",
    "core.runner.workflow_input",
    "core.surface.canvas",
    "core.surface.workflow_runtime",
    "core.vision_context",
}
_BUILTIN_WORKFLOW_CAPABILITY_DETAILS = {
    "core.vision_context": {
        "summary": "Hydrate explicitly referenced image pixels into a text/LLM workflow step.",
        "required_declaration": "Add core.vision_context to root required_capabilities.",
        "fixed_image": {
            "field": "context_refs",
            "example": {"ref": "storyboard", "role": "vision_context"},
            "restriction": "Fixed refs only; dynamic selectors belong in references.",
        },
        "dynamic_images": {
            "field": "references",
            "required_role": "vision_context",
            "selector_fields": ["from_group", "source_step", "source_path", "match_fields"],
            "semantics": {
                "from_group": "Candidate image repeat group.",
                "source_step": "Upstream step that outputs selected identifiers for the current repeat instance; never the candidate media child.",
                "source_path": "Path from source_step payload to the selected-identifier array, normally output.selected_ids.",
                "match_fields": "Non-empty string list of identity fields present on candidate repeat scopes.",
            },
            "invalid_forms": [
                "selector object inside context_refs",
                "source_step points to the candidate media child",
                "source_path omits the output. prefix",
                "match_fields contains mapping objects instead of strings",
            ],
        },
        "media_reference_role": "visual_reference",
        "failure_behavior": "A declared fixed vision image that cannot be hydrated fails the step.",
    },
}
_STEP_SUMMARY_METADATA_KEYS = (
    "source_node_id",
    "source_label",
    "source_category",
    "source_ui",
    "source_behavior",
    "mode",
    "repeat",
    "foreach",
    "reads_from",
    "layout_after",
    "bindings",
    "role",
    "start_action",
    "execution_state",
    "inputs_schema",
    "expansion",
    "collection",
    "instance_scope",
    "item_source",
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
    "fields",
    "authoring",
    "output",
)


class WorkflowTemplateError(ValueError):
    """Raised when a workflow template cannot be loaded or normalized."""


def _authoring_only_field_paths(raw: dict[str, Any]) -> list[str]:
    if is_authoring_workflow(raw):
        return []
    issues: list[str] = []

    def visit(steps: Any, path: str) -> None:
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or f"step_{index}").strip()
            label = f"{path}.{step_id or index}"
            for key in sorted(_AUTHORING_ONLY_STEP_KEYS):
                if key in step and step.get(key) not in (None, "", [], {}):
                    issues.append(f"{label}.{key}")
            visit(step.get("steps"), f"{label}.steps")

    visit(raw.get("steps"), "steps")
    return issues


def _ensure_no_authoring_only_fields_without_schema(raw: dict[str, Any]) -> None:
    issues = _authoring_only_field_paths(raw)
    if not issues:
        return
    raise WorkflowTemplateError(
        "Workflow uses authoring-only step fields without schema='openreel.workflow.authoring.v1': "
        + ", ".join(issues[:12])
        + ". Add the authoring schema, or use runtime fields depends_on and foreach/repeat groups."
    )


def _coerce_dict(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise WorkflowTemplateError(f"{label} must be an object")


def _coerce_list(value: Any, *, label: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    raise WorkflowTemplateError(f"{label} must be an array")


def _workflow_plugin_protocol_nodes(plugin_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for node in plugin_nodes:
        if not isinstance(node, dict):
            continue
        nodes.append({
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
        })
    return nodes


def workflow_protocol_info() -> dict[str, Any]:
    from app.services import workflow_plugins

    plugin_nodes = workflow_plugins.plugin_node_types()
    extension_ids = sorted({*_BUILTIN_WORKFLOW_EXTENSION_IDS, *workflow_plugins.available_extension_ids()})
    return {
        "protocol_version": WORKFLOW_SPEC_PROTOCOL_VERSION,
        "available_capabilities": sorted(_BUILTIN_WORKFLOW_CAPABILITIES),
        "capability_details": deepcopy(_BUILTIN_WORKFLOW_CAPABILITY_DETAILS),
        "available_extensions": extension_ids,
        "available_plugin_nodes": _workflow_plugin_protocol_nodes(plugin_nodes),
        "plugin_errors": workflow_plugins.plugin_errors(),
        "extension_fields": {
            "workflow": [
                "workflow_spec_version",
                "required_capabilities",
                "required_extensions",
                "extensions",
                "capabilities",
                "input_defaults",
                "x",
                "x-openreel",
            ],
            "step": [
                "operation",
                "capability",
                "required_capabilities",
                "extension",
                "extension_config",
                "io",
                "layout_after",
                "reads_from",
                "x",
                "x-openreel",
            ],
        },
    }


def _coerce_string_list(value: Any, *, label: str) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, dict):
        if isinstance(value.get("required"), list):
            raw_items = list(value.get("required") or [])
        elif isinstance(value.get("items"), list):
            raw_items = list(value.get("items") or [])
        else:
            raw_items = [key for key, enabled in value.items() if enabled]
    elif isinstance(value, list):
        raw_items = list(value)
    else:
        raise WorkflowTemplateError(f"{label} must be a string, object, or array")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _collect_protocol_requirements(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    capabilities: list[str] = []
    extensions: list[str] = []

    def add_capabilities(value: Any, *, label: str) -> None:
        capabilities.extend(_coerce_string_list(value, label=label))

    def add_extensions(value: Any, *, label: str) -> None:
        extensions.extend(_coerce_string_list(value, label=label))

    def visit_step(step: dict[str, Any], label: str) -> None:
        add_capabilities(step.get("required_capabilities"), label=f"{label}.required_capabilities")
        if step.get("capability") not in (None, "", [], {}):
            add_capabilities(step.get("capability"), label=f"{label}.capability")
        add_extensions(step.get("required_extensions"), label=f"{label}.required_extensions")
        extension = step.get("extension")
        if isinstance(extension, str):
            add_extensions(extension, label=f"{label}.extension")
        elif isinstance(extension, dict):
            add_extensions(extension.get("id") or extension.get("name"), label=f"{label}.extension")
            add_capabilities(extension.get("required_capabilities"), label=f"{label}.extension.required_capabilities")
        child_steps = step.get("steps")
        if isinstance(child_steps, list):
            for index, child in enumerate(child_steps, start=1):
                if isinstance(child, dict):
                    visit_step(child, f"{label}.steps[{index}]")

    add_capabilities(payload.get("required_capabilities"), label="required_capabilities")
    add_extensions(payload.get("required_extensions"), label="required_extensions")
    capabilities_obj = payload.get("capabilities")
    if isinstance(capabilities_obj, dict):
        add_capabilities(capabilities_obj.get("required"), label="capabilities.required")
    elif isinstance(capabilities_obj, list):
        add_capabilities(capabilities_obj, label="capabilities")
    extensions_obj = payload.get("extensions")
    if isinstance(extensions_obj, dict):
        for extension_id, extension_spec in extensions_obj.items():
            if isinstance(extension_spec, dict) and extension_spec.get("required") is True:
                add_extensions(extension_id, label=f"extensions.{extension_id}")
            if isinstance(extension_spec, dict):
                add_capabilities(extension_spec.get("required_capabilities"), label=f"extensions.{extension_id}.required_capabilities")
    for index, step in enumerate(payload.get("steps") or [], start=1):
        if isinstance(step, dict):
            visit_step(step, f"steps[{index}]")
    return _unique_nonempty_strings(capabilities), _unique_nonempty_strings(extensions)


def workflow_protocol_diagnostics(raw: dict[str, Any]) -> dict[str, Any]:
    from app.services import workflow_plugins

    _ensure_no_authoring_only_fields_without_schema(raw)
    if is_authoring_workflow(raw):
        try:
            raw = compile_authoring_workflow(raw)
        except WorkflowAuthoringSpecError as exc:
            raise WorkflowTemplateError(str(exc)) from exc
    required_capabilities, required_extensions = _collect_protocol_requirements(raw)
    missing_capabilities = [
        item for item in required_capabilities
        if item not in _BUILTIN_WORKFLOW_CAPABILITIES
    ]
    available_extensions = {*_BUILTIN_WORKFLOW_EXTENSION_IDS, *workflow_plugins.available_extension_ids()}
    missing_extensions = [
        item for item in required_extensions
        if item not in available_extensions
    ]
    protocol_version = str(
        raw.get("workflow_spec_version")
        or raw.get("protocol_version")
        or WORKFLOW_SPEC_PROTOCOL_VERSION
    ).strip() or WORKFLOW_SPEC_PROTOCOL_VERSION
    return {
        "protocol_version": protocol_version,
        "engine_protocol_version": WORKFLOW_SPEC_PROTOCOL_VERSION,
        "required_capabilities": required_capabilities,
        "required_extensions": required_extensions,
        "missing_capabilities": missing_capabilities,
        "missing_extensions": missing_extensions,
        "supported": not missing_capabilities and not missing_extensions,
        "available_capabilities": sorted(_BUILTIN_WORKFLOW_CAPABILITIES),
        "available_extensions": sorted(available_extensions),
    }


def _ensure_workflow_protocol_supported(raw: dict[str, Any]) -> dict[str, Any]:
    diagnostics = workflow_protocol_diagnostics(raw)
    missing = [*diagnostics["missing_capabilities"], *diagnostics["missing_extensions"]]
    if missing:
        raise WorkflowTemplateError(
            "Workflow requires unsupported capabilities or extensions: " + ", ".join(missing)
        )
    return diagnostics


def _read_template_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowTemplateError(f"Invalid workflow template JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise WorkflowTemplateError(f"Workflow template must be a JSON object: {path}")
    raw["_path"] = str(path)
    return raw


def _normalize_inline_id(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    raw = _CAMEL_BOUNDARY_RE.sub("_", raw)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    if not slug or not slug[0].isalpha():
        slug = fallback
    return slug[:80].rstrip("_") or fallback


def _instance_label(instance: dict[str, Any], index: int) -> str:
    explicit = str(instance.get("label") or instance.get("title") or "").strip()
    if explicit:
        return explicit
    episode = instance.get("episode")
    segment = instance.get("segment")
    if episode not in (None, "") and segment not in (None, ""):
        return f"第{episode}集第{segment}段"
    if segment not in (None, ""):
        return f"第{segment}段"
    if episode not in (None, ""):
        return f"第{episode}集"
    return f"实例{index}"


def _instance_suffix(instance: dict[str, Any], index: int) -> str:
    explicit = instance.get("id") or instance.get("key")
    if explicit:
        return _normalize_inline_id(explicit, fallback=f"i{index}")
    parts: list[str] = []
    if instance.get("episode") not in (None, ""):
        parts.append(f"e{instance['episode']}")
    segment = instance.get("segment")
    if segment in (None, ""):
        segment = instance.get("segment_index") or instance.get("segmentIndex")
    if segment not in (None, ""):
        parts.append(f"s{segment}")
    if not parts and instance.get("index") not in (None, ""):
        parts.append(f"i{instance['index']}")
    for key in ("character", "scene", "item"):
        if instance.get(key) not in (None, ""):
            parts.append(_normalize_inline_id(instance[key], fallback=key))
            break
    return _normalize_inline_id("_".join(parts), fallback=f"i{index}")


def _coerce_instance_items(raw_instances: list[Any]) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for index, item in enumerate(raw_instances, start=1):
        if isinstance(item, dict):
            instance = deepcopy(item)
        else:
            instance = {"item": item}
        instance.setdefault("index", index)
        instance.setdefault("label", _instance_label(instance, index))
        instances.append(instance)
    return instances


def _instances_from_count(
    count: Any,
    *,
    label: str,
    scope_key: str = "index",
    start: Any = 1,
) -> list[dict[str, Any]]:
    try:
        total = max(1, int(count))
        first = int(start or 1)
    except (TypeError, ValueError) as exc:
        raise WorkflowTemplateError(f"{label} count must be an integer") from exc
    key = str(scope_key or "index").strip() or "index"
    return [
        {key: first + offset, "index": offset + 1}
        for offset in range(total)
    ]


def _resolve_count_value(value: Any, input_values: dict[str, Any], *, label: str, default: int = 1) -> int:
    raw = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raw = default
        else:
            looked_up = _context_value(input_values, text)
            raw = looked_up if looked_up not in (None, "", [], {}) else text
    if raw in (None, ""):
        raw = default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError) as exc:
        raise WorkflowTemplateError(f"{label} must be an integer or input key") from exc


def _first_context_value(input_values: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = _context_value(input_values, key)
        if value not in (None, "", [], {}):
            return value
    return None


def _looks_like_segment_repeat(group: dict[str, Any], source: dict[str, Any]) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            group.get("id"),
            group.get("title"),
            group.get("label"),
            source.get("dimension"),
            source.get("from_dimension"),
            source.get("from"),
            source.get("source"),
            source.get("from_step"),
            source.get("from_node"),
            source.get("path"),
        )
    ).lower()
    return "segment" in text or "分段" in text


def _segment_placeholder_instances(input_values: dict[str, Any]) -> list[dict[str, Any]] | None:
    duration = _first_context_value(
        input_values,
        ("duration_seconds", "total_duration_seconds", "durationSeconds", "totalDurationSeconds"),
    )
    segment_seconds = _first_context_value(
        input_values,
        ("segment_seconds", "segmentSeconds", "per_segment_seconds", "segmentDurationSeconds"),
    )
    if duration in (None, "", [], {}) or segment_seconds in (None, "", [], {}):
        return None
    try:
        total_seconds = max(1.0, float(duration))
        seconds_per_segment = max(1.0, float(segment_seconds))
    except (TypeError, ValueError):
        return None
    count = max(1, int(math.ceil(total_seconds / seconds_per_segment)))
    raw_episode_count = _first_context_value(
        input_values,
        ("episode_count", "episodeCount", "episodes"),
    )
    try:
        episode_count = max(1, int(raw_episode_count or 1))
    except (TypeError, ValueError):
        episode_count = 1
    instances: list[dict[str, Any]] = []
    for episode in range(1, episode_count + 1):
        for segment in range(1, count + 1):
            global_index = (episode - 1) * count + segment
            start_second = int(round((segment - 1) * seconds_per_segment))
            end_second = int(round(min(total_seconds, segment * seconds_per_segment)))
            duration_seconds = max(1, end_second - start_second)
            item = {
                "index": global_index,
                "segment": segment,
                "segment_index": segment,
                "start_second": start_second,
                "end_second": end_second,
                "duration_seconds": duration_seconds,
                "label": f"第{segment}段",
                "placeholder": True,
            }
            if episode_count > 1:
                item["episode"] = episode
                item["episode_index"] = episode
                item["label"] = f"第{episode}集第{segment}段"
            instances.append(item)
    return instances


def _normalize_segment_repeat_instances(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(instances, start=1):
        instance = deepcopy(item)
        had_segment = instance.get("segment") not in (None, "")
        had_segment_index = instance.get("segment_index") not in (None, "") or instance.get("segmentIndex") not in (None, "")
        segment = (
            instance.get("segment")
            or instance.get("segment_index")
            or instance.get("segmentIndex")
            or instance.get("index")
            or index
        )
        instance.setdefault("index", index)
        if not had_segment:
            instance["segment"] = segment
        if not had_segment_index and not had_segment:
            instance["segment_index"] = segment
        if str(instance.get("label") or "").startswith("实例"):
            instance["label"] = f"第{segment}段"
        else:
            instance.setdefault("label", f"第{segment}段")
        normalized.append(instance)
    return normalized


def _count_reference_missing(value: Any, input_values: dict[str, Any]) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or re.fullmatch(r"[+-]?\d+", text):
        return False
    return _context_value(input_values, text) in (None, "", [], {})


def _lookup_dict_key(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    normalized = _normalize_inline_id(key, fallback=key)
    for candidate_key, value in payload.items():
        if _normalize_inline_id(candidate_key, fallback=str(candidate_key)) == normalized:
            return value
    return None


def _ancestor_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): deepcopy(item)
        for key, item in value.items()
        if not isinstance(item, (list, dict)) and item not in (None, "", [], {})
    }


def _coerce_path_leaf(value: Any, scope: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        result = {**deepcopy(scope), **deepcopy(value)}
    else:
        result = {**deepcopy(scope), "item": value}
    return result


def _extract_path_instances(root: Any, path: str) -> list[dict[str, Any]]:
    parts = [part.strip() for part in str(path or "").split(".") if part.strip()]
    if not parts:
        if isinstance(root, list):
            return _coerce_instance_items(root)
        return [_coerce_path_leaf(root, {})] if root not in (None, "", [], {}) else []

    def walk(value: Any, index: int, scope: dict[str, Any]) -> list[dict[str, Any]]:
        if index >= len(parts):
            if isinstance(value, list):
                return [_coerce_path_leaf(item, scope) for item in value]
            return [_coerce_path_leaf(value, scope)] if value not in (None, "", [], {}) else []

        token = parts[index]
        is_array = token.endswith("[]")
        key = token[:-2] if is_array else token
        next_value = value
        if key:
            if not isinstance(value, dict):
                return []
            next_value = _lookup_dict_key(value, key)
        if is_array:
            if not isinstance(next_value, list):
                return []
            results: list[dict[str, Any]] = []
            parent_scope = {**scope, **_ancestor_scope(value)}
            for item in next_value:
                child_scope = {**parent_scope, **_ancestor_scope(item)}
                results.extend(walk(item, index + 1, child_scope))
            return results
        return walk(next_value, index + 1, {**scope, **_ancestor_scope(value)})

    return _coerce_instance_items(walk(root, 0, {}))


def _context_value(input_values: dict[str, Any], source_key: str) -> Any:
    key = str(source_key or "").strip()
    if not key:
        return input_values
    value = _lookup_dict_key(input_values, key)
    if value is not None:
        return value
    for container_key in ("nodes", "outputs", "steps", "context", "dimension_context"):
        container = input_values.get(container_key)
        if isinstance(container, dict):
            value = _lookup_dict_key(container, key)
            if value is not None:
                return value
    return None


def _source_path_instances(root: Any, path: str) -> list[dict[str, Any]]:
    candidates: list[Any] = [root]
    if isinstance(root, dict):
        for key in ("output", "value", "result", "data"):
            value = root.get(key)
            if value not in (None, "", [], {}):
                candidates.append(value)
        outputs = root.get("outputs")
        if isinstance(outputs, list):
            for output in outputs:
                if isinstance(output, dict):
                    for key in ("value", "output", "result", "data"):
                        value = output.get(key)
                        if value not in (None, "", [], {}):
                            candidates.append(value)
                elif output not in (None, "", [], {}):
                    candidates.append(output)

    for candidate in candidates:
        instances = _extract_path_instances(candidate, path)
        if instances:
            return instances
    return []


def _instances_from_source(
    source: dict[str, Any],
    *,
    dimensions: dict[str, Any],
    input_values: dict[str, Any],
    label: str,
) -> list[dict[str, Any]] | None:
    if isinstance(source.get("instances"), list):
        return _coerce_instance_items(source["instances"])
    if isinstance(source.get("foreach"), list):
        return _coerce_instance_items(source["foreach"])

    dimension_name = str(source.get("dimension") or source.get("from_dimension") or "").strip()
    if dimension_name:
        dimension = dimensions.get(dimension_name) or dimensions.get(_normalize_inline_id(dimension_name, fallback=dimension_name))
        if dimension is None:
            return None
        return _dimension_instances(
            dimension_name,
            dimension,
            dimensions=dimensions,
            input_values=input_values,
            label=f"{label}.dimension.{dimension_name}",
        )

    input_count_key = str(source.get("input_count") or "").strip()
    if input_count_key:
        count = _context_value(input_values, input_count_key)
        if count in (None, "", [], {}):
            return None
        return _instances_from_count(
            count,
            label=f"{label}.input_count",
            scope_key=str(source.get("scope_key") or "index"),
            start=source.get("start") or 1,
        )

    if source.get("count") not in (None, ""):
        return _instances_from_count(
            source.get("count"),
            label=f"{label}.count",
            scope_key=str(source.get("scope_key") or "index"),
            start=source.get("start") or 1,
        )

    from_expr = str(source.get("from") or source.get("source") or "").strip()
    from_step = str(source.get("from_step") or source.get("from_node") or source.get("planner_node") or "").strip()
    path = str(source.get("path") or "").strip()
    root: Any = None
    if from_step:
        root = _context_value(input_values, from_step)
    elif from_expr:
        if from_expr.startswith("inputs."):
            root = input_values
            path = from_expr[len("inputs."):]
        elif "." in from_expr and not path:
            first, rest = from_expr.split(".", 1)
            root = _context_value(input_values, first)
            path = rest
        else:
            root = _context_value(input_values, from_expr)
    if root in (None, "", [], {}):
        return None
    instances = _source_path_instances(root, path)
    return instances or None


def _dimension_instances(
    name: str,
    definition: Any,
    *,
    dimensions: dict[str, Any],
    input_values: dict[str, Any],
    label: str,
) -> list[dict[str, Any]] | None:
    if isinstance(definition, list):
        return _coerce_instance_items(definition)
    if not isinstance(definition, dict):
        return None
    return _instances_from_source(
        definition,
        dimensions=dimensions,
        input_values=input_values,
        label=label or name,
    )


def _render_instance_string(value: str, instance: dict[str, Any]) -> str:
    def lookup(path: str) -> str:
        text = path.strip()
        if text == "json":
            return json.dumps(instance, ensure_ascii=False, sort_keys=True)
        current: Any = instance
        for part in [item for item in text.split(".") if item]:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = None
            if current is None:
                return ""
        if isinstance(current, (dict, list)):
            return json.dumps(current, ensure_ascii=False, sort_keys=True)
        return str(current)

    return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", lambda match: lookup(match.group(1)), value)


def _render_instance_templates(value: Any, instance: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_instance_string(value, instance)
    if isinstance(value, list):
        return [_render_instance_templates(item, instance) for item in value]
    if isinstance(value, dict):
        return {key: _render_instance_templates(item, instance) for key, item in value.items()}
    return value


def _apply_instance_templates(step: dict[str, Any], instance: dict[str, Any]) -> dict[str, Any]:
    result = dict(step)
    for key in ("title", "purpose", "acceptance", "source_behavior"):
        if isinstance(result.get(key), str):
            result[key] = _render_instance_templates(result[key], instance)
    for key in ("fields", "prompt_spec", "bindings"):
        if key in result:
            result[key] = _render_instance_templates(result[key], instance)
    return result


def _repeat_instances(
    group: dict[str, Any],
    *,
    label: str,
    dimensions: dict[str, Any],
    input_values: dict[str, Any],
) -> list[dict[str, Any]] | None:
    repeat = group.get("repeat") if isinstance(group.get("repeat"), dict) else {}
    foreach = group.get("foreach")
    raw_instances = (
        group.get("instances")
        or (foreach if isinstance(foreach, list) else None)
        or repeat.get("instances")
        or (repeat.get("foreach") if isinstance(repeat.get("foreach"), list) else None)
    )
    if isinstance(raw_instances, list) and raw_instances:
        return _coerce_instance_items(raw_instances)

    episode_count = repeat.get("episode_count") or repeat.get("episodes") or group.get("episode_count")
    segment_count = repeat.get("segment_count") or repeat.get("segments") or group.get("segment_count")
    if episode_count not in (None, "") or segment_count not in (None, ""):
        if _count_reference_missing(episode_count, input_values) or _count_reference_missing(segment_count, input_values):
            return None
        episodes = _resolve_count_value(
            episode_count,
            input_values,
            label=f"{label}.repeat.episode_count",
        )
        segments = _resolve_count_value(
            segment_count,
            input_values,
            label=f"{label}.repeat.segment_count",
        )
        return [
            {"episode": episode, "segment": segment, "index": (episode - 1) * segments + segment}
            for episode in range(1, episodes + 1)
            for segment in range(1, segments + 1)
        ]

    count = repeat.get("count") or group.get("count")
    if count not in (None, ""):
        return _instances_from_count(
            count,
            label=f"{label}.repeat",
            scope_key=str(repeat.get("scope_key") or group.get("scope_key") or "index"),
            start=repeat.get("start") or group.get("start") or 1,
        )

    for source in (foreach, repeat.get("foreach"), repeat):
        if isinstance(source, dict):
            instances = _instances_from_source(
                source,
                dimensions=dimensions,
                input_values=input_values,
                label=label,
            )
            if instances:
                if _looks_like_segment_repeat(group, source):
                    return _normalize_segment_repeat_instances(instances)
                return instances
            if any(source.get(key) for key in ("dimension", "from_dimension", "from", "source", "from_step", "from_node", "planner_node", "input_count")):
                if _looks_like_segment_repeat(group, source):
                    placeholders = _segment_placeholder_instances(input_values)
                    if placeholders:
                        return placeholders
                return None

    raise WorkflowTemplateError(
        f"{label} repeat group requires instances/foreach, repeat.count, or repeat episode_count + segment_count"
    )


def _unique_nonempty_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dependency_keys(value: Any, *, label: str) -> list[str]:
    if value is None:
        return []
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, dict):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = list(value)
    else:
        raise WorkflowTemplateError(f"{label} must be a string, object, or array")

    keys: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            raw = item.get("step") or item.get("id") or item.get("ref") or item.get("source")
        else:
            raw = item
        text = str(raw or "").strip()
        if text:
            keys.append(text)
    return _unique_nonempty_strings(keys)


def _deferred_group_summary(group: dict[str, Any], *, group_id: str, group_title: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "id": group_id,
        "title": group_title,
        "depends_on": [str(dep).strip() for dep in group.get("depends_on") or [] if str(dep).strip()],
        "repeat": deepcopy(group.get("repeat")) if isinstance(group.get("repeat"), dict) else {},
        "foreach": deepcopy(group.get("foreach")) if isinstance(group.get("foreach"), dict) else group.get("foreach"),
        "steps": deepcopy(group.get("steps") or []),
        "status": "deferred",
    }
    for key in ("expansion", "collection", "mode", "source_node_id", "source_label", "source_category"):
        if key in group and group.get(key) not in (None, "", [], {}):
            summary[key] = deepcopy(group.get(key))
    return summary


def _repeat_scope_key(group: dict[str, Any], repeat: dict[str, Any]) -> str:
    for source in (
        repeat.get("foreach") if isinstance(repeat.get("foreach"), dict) else {},
        group.get("foreach") if isinstance(group.get("foreach"), dict) else {},
        repeat,
        group,
    ):
        if not isinstance(source, dict):
            continue
        value = source.get("scope_key") or source.get("item_name") or source.get("item")
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _expand_repeat_groups(
    steps: list[Any],
    *,
    label: str = "steps",
    dimensions: dict[str, Any] | None = None,
    input_values: dict[str, Any] | None = None,
    deferred_groups: list[dict[str, Any]] | None = None,
) -> list[Any]:
    dimensions = dimensions or {}
    input_values = input_values or {}
    deferred_groups = deferred_groups if deferred_groups is not None else []
    expanded: list[Any] = []
    for index, item in enumerate(steps, start=1):
        if not isinstance(item, dict):
            expanded.append(item)
            continue
        child_steps = item.get("steps")
        if not isinstance(child_steps, list):
            expanded.append(item)
            continue

        group = dict(item)
        group_id = str(group.get("id") or f"repeat_group_{index}").strip()
        group_id = _normalize_inline_id(group_id, fallback=f"repeat_group_{index}")
        group_title = str(group.get("title") or group.get("label") or group_id).strip()
        group_deps = [
            str(dep).strip()
            for dep in _coerce_list(group.get("depends_on"), label=f"{group_id}.depends_on")
            if str(dep).strip()
        ]
        repeat = group.get("repeat") if isinstance(group.get("repeat"), dict) else {}
        repeat_scope_key = _repeat_scope_key(group, repeat)
        instances = _repeat_instances(
            group,
            label=group_id,
            dimensions=dimensions,
            input_values=input_values,
        )
        if instances is None:
            deferred_groups.append(_deferred_group_summary(group, group_id=group_id, group_title=group_title))
            continue
        template_children = _expand_repeat_groups(
            child_steps,
            label=f"{group_id}.steps",
            dimensions=dimensions,
            input_values=input_values,
            deferred_groups=deferred_groups,
        )
        child_ids = {
            str(child.get("id") or "").strip()
            for child in template_children
            if isinstance(child, dict) and str(child.get("id") or "").strip()
        }

        previous_local_id_map: dict[str, str] = {}
        for instance_index, instance in enumerate(instances, start=1):
            suffix = _instance_suffix(instance, instance_index)
            instance_label = _instance_label(instance, instance_index)
            local_id_map = {
                child_id: f"{group_id}_{suffix}_{_normalize_inline_id(child_id, fallback='step')}"
                for child_id in child_ids
            }
            for child_offset, child in enumerate(template_children):
                if not isinstance(child, dict):
                    expanded.append(child)
                    continue
                template_child_id = str(child.get("id") or f"step_{child_offset + 1}").strip()
                expanded_child = deepcopy(child)
                expanded_child = _apply_instance_templates(expanded_child, instance)
                expanded_id = local_id_map.get(template_child_id) or (
                    f"{group_id}_{suffix}_{_normalize_inline_id(template_child_id, fallback=f'step_{child_offset + 1}')}"
                )
                expanded_child["id"] = expanded_id
                child_title = str(expanded_child.get("title") or template_child_id).strip()
                expanded_child["title"] = f"{instance_label} · {child_title}" if child_title else instance_label
                child_deps = [
                    str(dep).strip()
                    for dep in _coerce_list(expanded_child.get("depends_on"), label=f"{template_child_id}.depends_on")
                    if str(dep).strip()
                ]
                rewritten_deps = [local_id_map.get(dep, dep) for dep in child_deps]
                inherited_control_deps = [
                    local_id_map.get(dep, dep)
                    for dep in _dependency_keys(
                        expanded_child.get("_control_depends_on"),
                        label=f"{template_child_id}._control_depends_on",
                    )
                ]
                previous_deps = [
                    previous_local_id_map[dep]
                    for dep in _dependency_keys(
                        expanded_child.get("depends_on_previous"),
                        label=f"{template_child_id}.depends_on_previous",
                    )
                    if dep in previous_local_id_map
                ]
                expanded_child["depends_on"] = _unique_nonempty_strings([*group_deps, *rewritten_deps, *previous_deps])
                expanded_child["_control_depends_on"] = _unique_nonempty_strings([
                    *group_deps,
                    *inherited_control_deps,
                    *previous_deps,
                ])
                expanded_child.setdefault("role", "instance_step")
                expanded_child.setdefault("repeat", deepcopy(repeat))
                if group.get("foreach") not in (None, "", [], {}):
                    expanded_child.setdefault("foreach", deepcopy(group.get("foreach")))
                if group.get("bindings") not in (None, "", [], {}):
                    expanded_child.setdefault("bindings", deepcopy(group.get("bindings")))
                if repeat_scope_key:
                    expanded_child.setdefault("item_name", repeat_scope_key)
                expanded_child.setdefault("instance_scope", {
                    key: value
                    for key, value in instance.items()
                    if key not in {"label", "title"}
                })
                expanded_child.setdefault("template_step_id", _normalize_inline_id(template_child_id, fallback=f"step_{child_offset + 1}"))
                expanded_child.setdefault("repeat_group_id", group_id)
                expanded_child.setdefault("repeat_group_label", group_title)
                expanded_child.setdefault("repeat_group_index", instance_index)
                if "mode" not in expanded_child and repeat.get("mode"):
                    expanded_child["mode"] = repeat.get("mode")
                expanded.append(expanded_child)
            previous_local_id_map = local_id_map
    return expanded


def _normalize_template(raw: dict[str, Any]) -> dict[str, Any]:
    template_id = str(raw.get("id") or "").strip()
    if not _TEMPLATE_ID_RE.fullmatch(template_id):
        raise WorkflowTemplateError(f"Invalid workflow template id: {template_id!r}")
    steps = _coerce_list(raw.get("steps"), label="steps")
    if not steps:
        raise WorkflowTemplateError(f"Workflow template {template_id!r} has no steps")
    protocol = _ensure_workflow_protocol_supported(raw)

    normalized_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    deferred_group_ids = {
        str(group.get("id") or "").strip()
        for group in _coerce_list(raw.get("_deferred_groups"), label="deferred_groups")
        if isinstance(group, dict) and str(group.get("id") or "").strip()
    }
    repeat_group_ids = {
        str(step.get("repeat_group_id") or "").strip()
        for step in steps
        if isinstance(step, dict) and str(step.get("repeat_group_id") or "").strip()
    }
    group_dependency_ids = deferred_group_ids | repeat_group_ids
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            raise WorkflowTemplateError(f"Workflow template step #{index + 1} must be an object")
        step_id = str(item.get("id") or "").strip()
        if not _TEMPLATE_ID_RE.fullmatch(step_id):
            raise WorkflowTemplateError(f"Invalid step id in {template_id!r}: {step_id!r}")
        if step_id in seen:
            raise WorkflowTemplateError(f"Duplicate step id in {template_id!r}: {step_id!r}")
        seen.add(step_id)
        node_type = str(item.get("node_type") or item.get("type") or "").strip().lower()
        if node_type not in _VALID_NODE_TYPES:
            raise WorkflowTemplateError(f"Invalid node_type for {step_id!r}: {node_type!r}")
        deps = [str(dep).strip() for dep in _coerce_list(item.get("depends_on"), label=f"{step_id}.depends_on") if str(dep).strip()]
        if step_id in deps:
            raise WorkflowTemplateError(f"Step {step_id!r} cannot depend on itself")
        unknown = [dep for dep in deps if dep not in seen and dep not in group_dependency_ids]
        if unknown:
            raise WorkflowTemplateError(
                f"Step {step_id!r} references unknown or later dependencies: {', '.join(unknown)}"
            )
        normalized_steps.append({
            **item,
            "id": step_id,
            "node_type": node_type,
            "depends_on": deps,
            "fields": _coerce_dict(item.get("fields"), label=f"{step_id}.fields"),
            "position": _coerce_dict(item.get("position"), label=f"{step_id}.position"),
        })

    return {
        "id": template_id,
        "name": str(raw.get("name") or template_id).strip(),
        "description": str(raw.get("description") or "").strip(),
        "category": str(raw.get("category") or "workflow").strip() or "workflow",
        "applies_to": str(raw.get("applies_to") or "").strip(),
        "version": str(raw.get("version") or "1").strip(),
        "workflow_spec_version": protocol["protocol_version"],
        "authoring_spec_version": str(raw.get("authoring_spec_version") or "").strip(),
        "authoring": _coerce_dict(raw.get("authoring"), label="authoring"),
        "ui": _coerce_dict(raw.get("ui"), label="ui"),
        "phases": _coerce_list(raw.get("phases"), label="phases"),
        "protocol": protocol,
        "required_capabilities": list(protocol["required_capabilities"]),
        "required_extensions": list(protocol["required_extensions"]),
        "extensions": _coerce_dict(raw.get("extensions"), label="extensions"),
        "capabilities": deepcopy(raw.get("capabilities")) if isinstance(raw.get("capabilities"), (dict, list)) else {},
        "inputs": _coerce_list(raw.get("inputs"), label="inputs"),
        "inputs_schema": _coerce_dict(raw.get("inputs_schema"), label="inputs_schema"),
        "required_inputs": _coerce_list(raw.get("required_inputs"), label="required_inputs"),
        "dimensions": _coerce_dict(raw.get("dimensions"), label="dimensions"),
        "deferred_groups": _coerce_list(raw.get("_deferred_groups"), label="deferred_groups"),
        "defaults": _coerce_dict(raw.get("defaults"), label="defaults"),
        "input_defaults": _coerce_dict(raw.get("input_defaults"), label="input_defaults"),
        "steps": normalized_steps,
        "path": str(raw.get("_path") or ""),
    }


def _normalize_dimensions(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {
            _normalize_inline_id(key, fallback=f"dimension_{index}"): deepcopy(item)
            for index, (key, item) in enumerate(value.items(), start=1)
        }
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                continue
            name = _normalize_inline_id(item.get("id") or item.get("name"), fallback=f"dimension_{index}")
            result[name] = deepcopy(item)
        return result
    raise WorkflowTemplateError("dimensions must be an object or array")


def _template_input_ids(raw: dict[str, Any]) -> list[str]:
    result: list[str] = []
    inputs = raw.get("inputs")
    if isinstance(inputs, dict):
        result.extend(str(key) for key in inputs.keys())
    elif isinstance(inputs, list):
        for index, item in enumerate(inputs, start=1):
            if isinstance(item, dict):
                input_id = str(item.get("id") or item.get("name") or "").strip()
                if input_id:
                    result.append(input_id)
            else:
                input_id = str(item or "").strip()
                if input_id:
                    result.append(input_id)
    if not result and isinstance(raw.get("inputs_schema"), dict):
        result.extend(str(key) for key in raw["inputs_schema"].keys())
    return result


def _template_input_values(
    raw: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    input_ids = set(_template_input_ids(raw))
    defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
    for key in input_ids:
        if key in defaults and defaults.get(key) not in (None, "", [], {}):
            values[key] = deepcopy(defaults.get(key))
    input_defaults = raw.get("input_defaults") if isinstance(raw.get("input_defaults"), dict) else {}
    for key, value in input_defaults.items():
        if value not in (None, "", [], {}):
            values.setdefault(str(key), deepcopy(value))
    inputs = raw.get("inputs")
    if isinstance(inputs, dict):
        for key, item in inputs.items():
            if isinstance(item, dict) and item.get("default") not in (None, "", [], {}):
                values[str(key)] = deepcopy(item.get("default"))
            elif item not in (None, "", [], {}):
                values[str(key)] = deepcopy(item)
    elif isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            input_id = str(item.get("id") or item.get("name") or "").strip()
            if input_id and item.get("default") not in (None, "", [], {}):
                values[input_id] = deepcopy(item.get("default"))
    schema = raw.get("inputs_schema")
    if isinstance(schema, dict):
        for key, item in schema.items():
            if isinstance(item, dict) and item.get("default") not in (None, "", [], {}):
                values.setdefault(str(key), deepcopy(item.get("default")))
    if isinstance(overrides, dict):
        values.update({
            str(key): deepcopy(value)
            for key, value in overrides.items()
            if value not in (None, "", [], {})
        })
    return values


def _normalize_inline_ids(
    raw: dict[str, Any],
    *,
    default_id: str,
    input_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_no_authoring_only_fields_without_schema(raw)
    if is_authoring_workflow(raw):
        try:
            raw = compile_authoring_workflow(raw)
        except WorkflowAuthoringSpecError as exc:
            raise WorkflowTemplateError(str(exc)) from exc
    payload = dict(raw)
    payload["id"] = _normalize_inline_id(payload.get("id"), fallback=default_id)
    payload["dimensions"] = _normalize_dimensions(payload.get("dimensions"))
    if isinstance(payload.get("inputs"), dict):
        payload["inputs"] = [
            {"id": str(key), "default": value}
            for key, value in payload["inputs"].items()
        ]

    deferred_groups: list[dict[str, Any]] = [
        deepcopy(group)
        for group in _coerce_list(
            payload.get("_deferred_groups") or payload.get("deferred_groups"),
            label="deferred_groups",
        )
        if isinstance(group, dict)
    ]
    steps = _expand_repeat_groups(
        _coerce_list(payload.get("steps"), label="steps"),
        dimensions=payload["dimensions"],
        input_values=_template_input_values(payload, input_values),
        deferred_groups=deferred_groups,
    )
    payload["_deferred_groups"] = deferred_groups
    id_map: dict[str, str] = {}
    used: set[str] = set()
    normalized_steps: list[dict[str, Any]] = []

    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            normalized_steps.append(item)
            continue
        step = dict(item)
        original_id = str(step.get("id") or "").strip()
        base_id = _normalize_inline_id(original_id, fallback=f"step_{index + 1}")
        step_id = base_id
        suffix = 2
        while step_id in used:
            suffix_text = f"_{suffix}"
            step_id = f"{base_id[:80 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        used.add(step_id)
        if original_id:
            id_map[original_id] = step_id
        id_map[step_id] = step_id
        step["id"] = step_id
        normalized_steps.append(step)

    for step in normalized_steps:
        if not isinstance(step, dict):
            continue
        deps = []
        for dep in _coerce_list(step.get("depends_on"), label=f"{step.get('id')}.depends_on"):
            dep_text = str(dep).strip()
            if not dep_text:
                continue
            deps.append(id_map.get(dep_text) or _normalize_inline_id(dep_text, fallback=dep_text))
        step["depends_on"] = deps

    payload["steps"] = normalized_steps
    return payload


def normalize_inline_workflow(
    raw: dict[str, Any],
    *,
    default_id: str = "model_authored_workflow",
    input_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowTemplateError("workflow must be an object")
    payload = _normalize_inline_ids(raw, default_id=default_id, input_values=input_values)
    return _normalize_template(payload)


def _normalize_loaded_template(
    raw_template: dict[str, Any],
    *,
    input_values: dict[str, Any] | None = None,
    path: str = "",
    scope: str = "builtin",
    source: str = "builtin_template",
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = deepcopy(raw_template)
    _ensure_no_authoring_only_fields_without_schema(raw)
    if is_authoring_workflow(raw):
        try:
            raw = compile_authoring_workflow(raw)
        except WorkflowAuthoringSpecError as exc:
            raise WorkflowTemplateError(str(exc)) from exc
    if path:
        raw["_path"] = path
    raw["dimensions"] = _normalize_dimensions(raw.get("dimensions"))
    deferred_groups: list[dict[str, Any]] = []
    raw["steps"] = _expand_repeat_groups(
        _coerce_list(raw.get("steps"), label="steps"),
        dimensions=raw["dimensions"],
        input_values=_template_input_values(raw, input_values),
        deferred_groups=deferred_groups,
    )
    raw["_deferred_groups"] = deferred_groups
    template = _normalize_template(raw)
    template["scope"] = scope
    template["source"] = source
    template["downloadable"] = scope == "user"
    if isinstance(extra_summary, dict):
        for key in ("active_version_id", "versions", "template_source", "source_skill", "downloadable", "source", "path"):
            if key in extra_summary:
                template[key] = deepcopy(extra_summary[key])
    return template


def load_builtin_templates(input_values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for path in sorted(_BUILTIN_TEMPLATE_ROOT.glob("*/templates/*.json")):
        templates.append(_normalize_loaded_template(
            _read_template_file(path),
            input_values=input_values,
            path=str(path),
            scope="builtin",
            source="builtin_template",
        ))
    templates.sort(key=lambda item: (
        0 if str(item.get("id") or "") == DEFAULT_WORKFLOW_TEMPLATE_ID else 1,
        str(item.get("category") or ""),
        str(item.get("name") or ""),
        str(item.get("id") or ""),
    ))
    return templates


def get_builtin_template(
    template_id: str,
    *,
    input_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wanted = str(template_id or "").strip()
    for template in load_builtin_templates(input_values=input_values):
        if str(template.get("id") or "") == wanted:
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
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        templates.append(_normalize_loaded_template(
            workflow,
            input_values=input_values,
            scope="user",
            source="user_template",
            extra_summary=summary,
        ))
    templates.sort(key=lambda item: (str(item.get("category") or ""), str(item.get("name") or ""), str(item.get("id") or "")))
    return templates


def load_templates(input_values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    user_templates = load_user_templates(input_values=input_values)
    user_template_ids = {str(item.get("id") or "") for item in user_templates}
    builtin_templates = load_builtin_templates(input_values=input_values)
    builtin_template_ids = {str(item.get("id") or "") for item in builtin_templates}
    for template in user_templates:
        template["overrides_builtin"] = str(template.get("id") or "") in builtin_template_ids
    templates = [
        *user_templates,
        *(
            item
            for item in builtin_templates
            if str(item.get("id") or "") not in user_template_ids
        ),
    ]
    templates.sort(key=lambda item: (
        0 if str(item.get("scope") or "") == "user" else 1,
        0 if str(item.get("id") or "") == DEFAULT_WORKFLOW_TEMPLATE_ID else 1,
        str(item.get("category") or ""),
        str(item.get("name") or ""),
        str(item.get("id") or ""),
    ))
    return templates


def get_template(template_id: str = "", *, input_values: dict[str, Any] | None = None) -> dict[str, Any]:
    wanted = str(template_id or "").strip()
    templates = load_templates(input_values=input_values)
    if not templates:
        raise WorkflowTemplateError("No canvas workflow templates are available")
    if not wanted:
        for template in templates:
            if str(template.get("id") or "") == DEFAULT_WORKFLOW_TEMPLATE_ID:
                return template
        return templates[0]
    for template in templates:
        if template["id"] == wanted:
            return template
    raise WorkflowTemplateError(f"Workflow template not found: {wanted}")


def missing_required_inputs(template: dict[str, Any], input_values: dict[str, Any] | None = None) -> list[str]:
    values = input_values if isinstance(input_values, dict) else {}
    result: list[str] = []
    for item in template.get("required_inputs") or []:
        key = str(item or "").strip()
        if not key:
            continue
        if _context_value(values, key) in (None, "", [], {}):
            result.append(key)
    return result


def template_input_field_summaries(
    template: dict[str, Any],
    input_values: dict[str, Any] | None = None,
    *,
    only_missing: bool = False,
) -> list[dict[str, Any]]:
    values = input_values if isinstance(input_values, dict) else {}
    required = {str(item or "").strip() for item in template.get("required_inputs") or [] if str(item or "").strip()}
    schema = template.get("inputs_schema") if isinstance(template.get("inputs_schema"), dict) else {}
    input_defs: dict[str, dict[str, Any]] = {}
    inputs = template.get("inputs")
    if isinstance(inputs, dict):
        for key, value in inputs.items():
            input_id = str(key or "").strip()
            if not input_id:
                continue
            input_defs[input_id] = deepcopy(value) if isinstance(value, dict) else {"default": deepcopy(value)}
    elif isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                input_id = str(item.get("id") or item.get("name") or "").strip()
                if input_id:
                    input_defs[input_id] = deepcopy(item)
            else:
                input_id = str(item or "").strip()
                if input_id:
                    input_defs.setdefault(input_id, {})

    input_ids: list[str] = []
    for key in [*_template_input_ids(template), *required, *(str(key) for key in schema.keys())]:
        text = str(key or "").strip()
        if text and text not in input_ids:
            input_ids.append(text)

    fields: list[dict[str, Any]] = []
    for input_id in input_ids:
        spec: dict[str, Any] = {}
        if isinstance(input_defs.get(input_id), dict):
            spec.update(deepcopy(input_defs[input_id]))
        if isinstance(schema.get(input_id), dict):
            merged_schema = deepcopy(schema[input_id])
            merged_schema.update(spec)
            spec = merged_schema
        value = _context_value(values, input_id)
        missing = input_id in required and value in (None, "", [], {})
        if only_missing and not missing:
            continue
        label = str(spec.get("label") or spec.get("title") or spec.get("name") or input_id).strip()
        field: dict[str, Any] = {
            "id": input_id,
            "label": label,
            "type": str(spec.get("type") or "string"),
            "required": input_id in required,
            "missing": missing,
        }
        for key in ("description", "minimum", "maximum", "default"):
            if spec.get(key) not in (None, "", [], {}):
                field[key] = deepcopy(spec[key])
        enum_values = spec.get("enum") or spec.get("options")
        if isinstance(enum_values, list) and enum_values:
            field["options"] = [deepcopy(item) for item in enum_values[:12]]
        fields.append(field)
    return fields


def _workflow_relation_ids(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    raw_items = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            raw = item.get("step") or item.get("id") or item.get("ref") or item.get("source") or item.get("from_step")
        else:
            raw = item
        text = str(raw or "").strip()
        if not text:
            continue
        if "." in text:
            text = text.split(".", 1)[0].strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _workflow_reference_relation_ids(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    raw_items = value if isinstance(value, list) else list(value.values()) if isinstance(value, dict) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            raw_candidates = [
                item.get("source_step"),
                item.get("from_step"),
                item.get("candidates"),
                item.get("candidate"),
            ]
            if item.get("source") not in (None, "", [], {}):
                raw_candidates.append(item.get("source"))
        else:
            raw_candidates = [item]
        for raw in raw_candidates:
            text = str(raw or "").strip()
            if not text:
                continue
            if "." in text:
                text = text.split(".", 1)[0].strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def _workflow_graph_shape(step: dict[str, Any], child_steps: list[Any] | None = None) -> str:
    raw_kind = str(step.get("kind") or "").strip().lower()
    raw_role = str(step.get("role") or "").strip().lower()
    if child_steps is not None or raw_kind == "loop" or raw_role == "repeat_group":
        return "loop"
    if raw_kind == "input" or str(step.get("runner") or "").strip() == "workflow_input":
        return "input"
    if raw_kind == "plugin" or step.get("plugin") not in (None, "", [], {}) or step.get("plugin_node_type") not in (None, "", [], {}):
        return "plugin"
    if step.get("collection") not in (None, "", [], {}) or raw_kind in {"plan", "json", "llm_json", "collection"}:
        return "collection"
    if raw_kind == "review":
        return "review"
    return "step"


def _workflow_graph_node_summary(
    step: dict[str, Any],
    *,
    fallback: str,
    child_scope_id: str = "",
    child_steps: list[Any] | None = None,
) -> dict[str, Any]:
    step_id = _normalize_inline_id(step.get("id"), fallback=fallback)
    node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
    if node_type not in _VALID_NODE_TYPES:
        node_type = "text"
    summary: dict[str, Any] = {
        "id": step_id,
        "title": step.get("title") or step.get("label") or step_id,
        "node_type": node_type,
        "shape": _workflow_graph_shape(step, child_steps),
        "purpose": step.get("purpose") or step.get("fields", {}).get("purpose") or "",
        "depends_on": _workflow_relation_ids(step.get("depends_on")),
        "layout_after": _workflow_relation_ids(step.get("layout_after")),
        "reads_from": _workflow_relation_ids(step.get("reads_from") or step.get("context_refs")),
        "primary_skill": step.get("primary_skill") or "",
        "skill_category": step.get("skill_category") or "",
        "acceptance": step.get("acceptance") or "",
    }
    if child_scope_id:
        summary["child_scope_id"] = child_scope_id
        summary["has_children"] = True
        summary["role"] = str(step.get("role") or "repeat_group")
        child_ids = [
            _normalize_inline_id(child.get("id"), fallback=f"{step_id}_step_{index}")
            for index, child in enumerate(child_steps or [], start=1)
            if isinstance(child, dict)
        ]
        if child_ids:
            summary["expands_to"] = child_ids
    for key in _STEP_SUMMARY_METADATA_KEYS:
        if key in step and step.get(key) not in (None, "", [], {}):
            summary[key] = deepcopy(step.get(key))
    return summary


def _workflow_graph_edges_for_scope(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node_ids = {str(node.get("id") or "") for node in nodes}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edges(target: str, sources: list[str], edge_type: str) -> None:
        for source in sources:
            if not source or source == target or source not in node_ids:
                continue
            key = (source, target, edge_type)
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "id": f"{edge_type}:{source}->{target}",
                "source": source,
                "target": target,
                "type": edge_type,
            })

    for node in nodes:
        target = str(node.get("id") or "").strip()
        add_edges(target, _workflow_relation_ids(node.get("depends_on")), "execution")
        add_edges(target, _workflow_relation_ids(node.get("layout_after")), "layout")
        add_edges(target, _workflow_relation_ids(node.get("reads_from") or node.get("context_refs")), "read")
        add_edges(target, _workflow_reference_relation_ids(node.get("reference_selectors")), "reference")
        add_edges(target, _workflow_relation_ids(node.get("depends_on_previous")), "previous_instance")
    return edges


def template_graph_summary(steps: list[Any]) -> dict[str, Any]:
    scopes: dict[str, dict[str, Any]] = {}

    def build_scope(scope_id: str, title: str, raw_steps: list[Any]) -> None:
        nodes: list[dict[str, Any]] = []
        child_scope_builds: list[tuple[str, str, list[Any]]] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            step_id = _normalize_inline_id(raw_step.get("id"), fallback=f"{scope_id}_step_{index}")
            child_steps = raw_step.get("steps")
            if isinstance(child_steps, list):
                node = _workflow_graph_node_summary(
                    raw_step,
                    fallback=step_id,
                    child_scope_id=step_id,
                    child_steps=child_steps,
                )
                child_scope_builds.append((step_id, str(node.get("title") or step_id), child_steps))
            else:
                node = _workflow_graph_node_summary(raw_step, fallback=step_id)
            nodes.append(node)
        scopes[scope_id] = {
            "id": scope_id,
            "title": title,
            "nodes": nodes,
            "edges": _workflow_graph_edges_for_scope(nodes),
        }
        for child_scope_id, child_title, child_steps in child_scope_builds:
            build_scope(child_scope_id, child_title, child_steps)

    build_scope("root", "模板结构", _coerce_list(steps, label="steps"))
    return {
        "root_scope_id": "root",
        "scopes": scopes,
    }


def template_step_summaries(steps: list[Any]) -> list[dict[str, Any]]:
    def step_summary(
        step: dict[str, Any],
        *,
        fallback: str,
        inherited_repeat: dict[str, Any] | None = None,
        repeat_group_id: str = "",
        repeat_group_label: str = "",
    ) -> dict[str, Any]:
        step_id = _normalize_inline_id(step.get("id"), fallback=fallback)
        node_type = str(step.get("node_type") or step.get("type") or "").strip().lower()
        if node_type not in _VALID_NODE_TYPES:
            node_type = "text"
        item = {
            "id": step_id,
            "title": step.get("title") or step_id,
            "node_type": node_type,
            "purpose": step.get("purpose") or step.get("fields", {}).get("purpose") or "",
            "depends_on": list(step.get("depends_on") or []),
            "primary_skill": step.get("primary_skill") or "",
            "skill_category": step.get("skill_category") or "",
            "acceptance": step.get("acceptance") or "",
        }
        if inherited_repeat and "repeat" not in step:
            item["repeat"] = deepcopy(inherited_repeat)
        if repeat_group_id:
            item["repeat_group_id"] = repeat_group_id
        if repeat_group_label:
            item["repeat_group_label"] = repeat_group_label
        for key in _STEP_SUMMARY_METADATA_KEYS:
            if key in step and step.get(key) not in (None, "", [], {}):
                item[key] = deepcopy(step.get(key))
        return item

    def step_summaries(steps: list[Any], *, prefix: str = "") -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            step_id = _normalize_inline_id(raw_step.get("id"), fallback=f"{prefix}step_{index}")
            child_steps = raw_step.get("steps")
            if isinstance(child_steps, list):
                group_title = str(raw_step.get("title") or raw_step.get("label") or step_id).strip()
                child_ids = [
                    _normalize_inline_id(child.get("id"), fallback=f"{step_id}_step_{child_index}")
                    for child_index, child in enumerate(child_steps, start=1)
                    if isinstance(child, dict)
                ]
                group_summary = step_summary(raw_step, fallback=step_id)
                group_summary["role"] = str(raw_step.get("role") or "repeat_group")
                group_summary["shape"] = "loop"
                group_summary["child_scope_id"] = step_id
                group_summary["has_children"] = True
                group_summary["node_type"] = "text"
                group_summary["expands_to"] = child_ids
                summaries.append(group_summary)
                inherited_repeat = raw_step.get("repeat") if isinstance(raw_step.get("repeat"), dict) else None
                for child_index, child in enumerate(child_steps, start=1):
                    if not isinstance(child, dict):
                        continue
                    if isinstance(child.get("steps"), list):
                        nested = step_summaries([child], prefix=f"{step_id}_")
                        for nested_item in nested:
                            nested_item.setdefault("repeat_group_id", step_id)
                            nested_item.setdefault("repeat_group_label", group_title)
                            if inherited_repeat and "repeat" not in nested_item:
                                nested_item["repeat"] = deepcopy(inherited_repeat)
                        summaries.extend(nested)
                    else:
                        summaries.append(step_summary(
                            child,
                            fallback=f"{step_id}_step_{child_index}",
                            inherited_repeat=inherited_repeat,
                            repeat_group_id=step_id,
                            repeat_group_label=group_title,
                        ))
                continue
            summaries.append(step_summary(raw_step, fallback=f"{prefix}step_{index}"))
        return summaries

    return step_summaries(_coerce_list(steps, label="steps"))


def _template_summary_from_raw(
    raw_template: dict[str, Any],
    *,
    path: str = "",
    scope: str = "builtin",
    source: str = "builtin_template",
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = deepcopy(raw_template)
    if is_authoring_workflow(raw):
        try:
            raw = compile_authoring_workflow(raw)
        except WorkflowAuthoringSpecError as exc:
            raise WorkflowTemplateError(str(exc)) from exc
    if path:
        raw["_path"] = path
    protocol = _ensure_workflow_protocol_supported(raw)
    template_id = str(raw.get("id") or "").strip()
    if not _TEMPLATE_ID_RE.fullmatch(template_id):
        raise WorkflowTemplateError(f"Invalid workflow template id: {template_id!r}")
    summary = {
        "id": template_id,
        "name": str(raw.get("name") or template_id).strip(),
        "description": str(raw.get("description") or "").strip(),
        "category": str(raw.get("category") or "workflow").strip() or "workflow",
        "applies_to": str(raw.get("applies_to") or "").strip(),
        "version": str(raw.get("version") or "1").strip(),
        "scope": scope,
        "source": source,
        "downloadable": scope == "user",
        "active_version_id": "",
        "versions": [],
        "workflow_spec_version": protocol["protocol_version"],
        "protocol": protocol,
        "required_capabilities": list(protocol["required_capabilities"]),
        "required_extensions": list(protocol["required_extensions"]),
        "extensions": _coerce_dict(raw.get("extensions"), label="extensions"),
        "inputs": _template_input_ids(raw),
        "inputs_schema": _coerce_dict(raw.get("inputs_schema"), label="inputs_schema"),
        "required_inputs": _coerce_list(raw.get("required_inputs"), label="required_inputs"),
        "steps": template_step_summaries(raw.get("steps") if isinstance(raw.get("steps"), list) else []),
        "template_graph": template_graph_summary(raw.get("steps") if isinstance(raw.get("steps"), list) else []),
    }
    if isinstance(extra_summary, dict):
        for key in ("active_version_id", "versions", "downloadable", "source_skill", "source", "template_source", "path"):
            if key in extra_summary:
                summary[key] = deepcopy(extra_summary[key])
    return summary


def list_template_summaries() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    user_template_ids: set[str] = set()
    from app.agent import workflow_template_store

    builtin_workflows = [
        (path, _read_template_file(path))
        for path in sorted(_BUILTIN_TEMPLATE_ROOT.glob("*/templates/*.json"))
    ]
    builtin_template_ids = {str(workflow.get("id") or "") for _, workflow in builtin_workflows}

    for record in workflow_template_store.list_user_template_records():
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        workflow = version.get("workflow") if isinstance(version.get("workflow"), dict) else None
        if not workflow:
            continue
        user_template_ids.add(str(workflow.get("id") or ""))
        summary = _template_summary_from_raw(
            workflow,
            scope="user",
            source="user_template",
            extra_summary=record.get("summary") if isinstance(record.get("summary"), dict) else {},
        )
        summary["overrides_builtin"] = str(workflow.get("id") or "") in builtin_template_ids
        templates.append(summary)
    for path, workflow in builtin_workflows:
        if str(workflow.get("id") or "") in user_template_ids:
            continue
        templates.append(_template_summary_from_raw(
            workflow,
            path=str(path),
            scope="builtin",
            source="builtin_template",
        ))
    templates.sort(key=lambda item: (
        0 if str(item.get("scope") or "") == "user" else 1,
        0 if str(item.get("id") or "") == DEFAULT_WORKFLOW_TEMPLATE_ID else 1,
        str(item.get("category") or ""),
        str(item.get("name") or ""),
        str(item.get("id") or ""),
    ))
    return [
        deepcopy(template)
        for template in templates
    ]
