"""Compile human/model authored workflow specs into runtime workflow specs."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


AUTHORING_SPEC_VERSION = "openreel.workflow.authoring.v1"
RUNTIME_SPEC_VERSION = "openreel.workflow.v1"

_VALID_KINDS = {
    "input",
    "text",
    "plan",
    "json",
    "collection",
    "canvas_text",
    "canvas-text",
    "image",
    "video",
    "audio",
    "plugin",
    "llm_text",
    "llm_json",
    "review",
    "loop",
}
_KIND_ALIASES = {
    "array": "collection",
    "foreach": "loop",
    "for_each": "loop",
    "llm": "text",
    "list": "collection",
    "object_list": "collection",
    "repeat": "loop",
    "structured": "collection",
    "structured_list": "collection",
    "table": "collection",
}
_NODE_KINDS = {"text", "image", "video", "audio"}
_CANVAS_PRODUCT_KINDS = {"canvas_text", "image", "video", "audio"}
_AUTHORING_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,100}$")


class WorkflowAuthoringSpecError(ValueError):
    """Raised when an authoring workflow spec cannot be compiled."""


def is_authoring_workflow(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    schema = str(
        value.get("schema")
        or value.get("authoring_spec_version")
        or value.get("workflow_authoring_version")
        or ""
    ).strip()
    if schema == AUTHORING_SPEC_VERSION:
        return True
    return value.get("authoring") is True


def _normalize_id(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    if not slug or not slug[0].isalpha():
        slug = fallback
    return slug[:80].rstrip("_") or fallback


def _copy_non_empty(value: Any) -> Any:
    return deepcopy(value) if value not in (None, "", [], {}) else None


def _truthy_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "visible", "canvas"}
    return False


def _strip_template_wrappers(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", text)
    return match.group(1).strip() if match else text


def _normalize_kind(value: Any, *, step_id: str) -> str:
    raw = str(value or "text").strip().lower()
    kind = re.sub(r"[\s-]+", "_", raw)
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in _VALID_KINDS:
        aliases = ", ".join(sorted(_KIND_ALIASES))
        allowed = ", ".join(sorted(_VALID_KINDS))
        raise WorkflowAuthoringSpecError(
            f"Invalid authoring step kind for {step_id!r}: {raw!r}. "
            f"Use one of: {allowed}. Common aliases accepted: {aliases}."
        )
    return kind


def _coerce_list(value: Any) -> list[Any]:
    if value in (None, "", {}):
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _coerce_string_list(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _coerce_list(value):
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _normalize_inputs(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    inputs: list[dict[str, Any]] = []
    required: list[str] = []
    raw_items: list[Any]
    if isinstance(value, dict):
        raw_items = []
        for key, item in value.items():
            if isinstance(item, dict):
                raw_items.append({"id": key, **deepcopy(item)})
            else:
                raw_items.append({"id": key, "default": deepcopy(item)})
    else:
        raw_items = _coerce_list(value)
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            spec: dict[str, Any] = {"id": item}
        elif isinstance(item, dict):
            spec = deepcopy(item)
        else:
            continue
        input_id = str(spec.get("id") or spec.get("name") or spec.get("key") or "").strip()
        if not input_id:
            input_id = f"input_{index}"
        spec["id"] = input_id
        if spec.get("required") is True:
            required.append(input_id)
        inputs.append(spec)
    return inputs, required


def _prompt_section(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item or "").strip())
    if isinstance(value, dict):
        return "\n".join(
            f"{key}: {item}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        )
    return str(value).strip()


def _compile_prompt_template(prompt: Any, *, title: str, kind: str) -> str:
    if isinstance(prompt, str):
        return prompt.strip()
    if not isinstance(prompt, dict):
        return ""
    if isinstance(prompt.get("template"), str) and prompt["template"].strip():
        return prompt["template"].strip()
    system = (
        _prompt_section(prompt.get("system"))
        or _prompt_section(prompt.get("role"))
        or f"你负责执行工作流步骤：{title}。"
    )
    task = (
        _prompt_section(prompt.get("instruction"))
        or _prompt_section(prompt.get("task"))
        or _prompt_section(prompt.get("goal"))
    )
    output = _prompt_section(prompt.get("output"))
    check = _prompt_section(prompt.get("check") or prompt.get("acceptance"))
    if not output and kind in {"plan", "json"}:
        output = "输出一个 JSON 对象，字段名保持稳定，供后续步骤引用。"
    if not output and kind == "collection":
        output = "按本节点的输出字段提取一个对象列表；用户只需要写自然语言任务，结构化 JSON 协议由运行器自动注入。"
    sections = [
        ("SYSTEM", system),
        ("USER", task),
        ("OUTPUT", output),
        ("CHECK", check),
    ]
    return "\n".join(f"{label}:\n{text}" for label, text in sections if text).strip()


def _split_path(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    if "." not in text:
        return text, ""
    head, tail = text.split(".", 1)
    return head.strip(), tail.strip()


def _compile_foreach(value: Any, *, item_name: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result = deepcopy(value)
        if "items" in result and "foreach" not in result and "instances" not in result:
            raw_items = result.pop("items")
            if isinstance(raw_items, list):
                result["instances"] = raw_items
            else:
                result["foreach"] = _compile_foreach(raw_items, item_name=item_name or str(result.get("item_name") or result.get("item") or ""))
        if "item" in result and "scope_key" not in result:
            result["scope_key"] = result.get("item")
        if "item_name" in result and "scope_key" not in result:
            result["scope_key"] = result.get("item_name")
        if item_name and "scope_key" not in result:
            result["scope_key"] = item_name
        return result
    text = str(_strip_template_wrappers(value) or "").strip()
    if not text:
        return {}
    source, path = _split_path(text)
    if source == "inputs" and path:
        leaf = path.split(".")[-1]
        if leaf.lower().endswith("count"):
            result = {"input_count": path}
            if item_name:
                result["scope_key"] = item_name
            return result
        result = {"from": f"inputs.{path}"}
        if item_name:
            result["scope_key"] = item_name
        return result
    result: dict[str, Any] = {"from": source}
    if path:
        result["path"] = path
    if item_name:
        result["scope_key"] = item_name
    return result


def _reference_selector_from_mapping(name: str, value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        selector = deepcopy(value)
        if "candidates" in selector and "from_step" not in selector:
            selector["from_step"] = selector.pop("candidates")
        if "candidate" in selector and "from_step" not in selector:
            selector["from_step"] = selector.pop("candidate")
        if "source" in selector and "source_step" not in selector:
            source, path = _split_path(str(selector["source"]))
            selector["source_step"] = source
            if path and "source_path" not in selector:
                selector["source_path"] = path
        selector.setdefault("role", "visual_reference")
        selector.setdefault("name", name)
        return selector
    text = str(value or "").strip()
    if not text:
        return None
    left, sep, right = text.partition("->")
    if not sep:
        return {"name": name, "source_step": text, "role": "visual_reference"}
    source, path = _split_path(left.strip())
    selector: dict[str, Any] = {
        "name": name,
        "source_step": source,
        "from_step": right.strip(),
        "role": "visual_reference",
    }
    if path:
        selector["source_path"] = path
    return selector


def _compile_references(value: Any) -> list[dict[str, Any]]:
    if value in (None, "", [], {}):
        return []
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            selector = _reference_selector_from_mapping(str(key), item)
            if selector:
                result.append(selector)
        return result
    for index, item in enumerate(_coerce_list(value), start=1):
        selector = _reference_selector_from_mapping(f"reference_{index}", item)
        if selector:
            result.append(selector)
    return result


def _output_canvas(step: dict[str, Any], *, kind: str) -> bool:
    if kind == "text":
        return False
    output = _step_output_spec(step)
    if _truthy_flag(output.get("canvas")) or _truthy_flag(output.get("show_on_canvas")):
        return True
    if _truthy_flag(step.get("visible")) or _truthy_flag(step.get("show_on_canvas")):
        return True
    return kind in _CANVAS_PRODUCT_KINDS


def _output_mode(step: dict[str, Any], *, kind: str) -> str:
    output = _step_output_spec(step)
    mode = str(output.get("mode") or step.get("output_mode") or "").strip().lower()
    if mode:
        return mode
    return "json" if kind in {"plan", "json", "collection"} else ""


def _step_output_spec(step: dict[str, Any]) -> dict[str, Any]:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    result = deepcopy(output)
    outputs = step.get("outputs")
    first_output = outputs[0] if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict) else None
    if first_output:
        result.setdefault("key", first_output.get("id") or first_output.get("key") or first_output.get("name"))
        result.setdefault("label", first_output.get("label") or first_output.get("title"))
        result.setdefault("type", first_output.get("type") or first_output.get("kind"))
    if _truthy_flag(step.get("visible")) or _truthy_flag(step.get("show_on_canvas")):
        result.setdefault("canvas", True)
    return result


def _step_output_schema(step: dict[str, Any]) -> dict[str, Any]:
    schema = step.get("output_schema")
    if not isinstance(schema, dict):
        schema = step.get("schema")
    if not isinstance(schema, dict):
        return {}
    result = deepcopy(schema)
    if str(result.get("type") or "").strip().lower() == "array":
        result["type"] = "collection"
        item_schema = result.get("items") if isinstance(result.get("items"), dict) else {}
        if "fields" not in result and isinstance(item_schema.get("properties"), dict):
            result["fields"] = [
                {"id": key, **(deepcopy(value) if isinstance(value, dict) else {})}
                for key, value in item_schema["properties"].items()
            ]
            required = item_schema.get("required") if isinstance(item_schema.get("required"), list) else []
            for field in result["fields"]:
                if field.get("id") in required:
                    field["required"] = True
    return result


def _compiled_step_base(step: dict[str, Any], *, index: int) -> dict[str, Any]:
    step_id = str(step.get("id") or "").strip()
    if not step_id:
        step_id = f"step_{index}"
    if not _AUTHORING_ID_RE.fullmatch(step_id):
        step_id = _normalize_id(step_id, fallback=f"step_{index}")
    title = str(step.get("title") or step.get("name") or step_id).strip()
    kind = _normalize_kind(step.get("kind") or step.get("type") or "text", step_id=step_id)
    if kind in {"json", "llm_json"}:
        kind = "plan"
    if kind in {"llm_text", "review"}:
        kind = "text"
    node_type = str(step.get("node_type") or "").strip().lower()
    if not node_type:
        node_type = (
            "text"
            if kind in {"input", "plan", "collection", "plugin", "loop", "canvas_text"}
            else kind
        )
    if node_type not in {"text", "image", "video", "audio"}:
        raise WorkflowAuthoringSpecError(f"Invalid node_type for {step_id!r}: {node_type!r}")
    canvas = _output_canvas(step, kind=kind)
    surface = "draft_canvas" if canvas else "workflow_runtime"
    visibility = "canvas" if canvas else "flow_only"
    if kind in {"input", "loop"}:
        surface = "workflow_runtime"
        visibility = "flow_only"
    prompt_source = step.get("prompt")
    if prompt_source in (None, "", [], {}):
        prompt_source = step.get("prompt_template")
    prompt_template = _compile_prompt_template(prompt_source, title=title, kind=kind)
    compiled: dict[str, Any] = {
        "id": step_id,
        "title": title,
        "node_type": node_type,
        "depends_on": _coerce_string_list(step.get("needs") or step.get("depends_on")),
        "runner": (
            "workflow_input"
            if kind == "input"
            else "workflow_plugin"
            if kind == "plugin"
            else "workflow_canvas_output"
            if kind in _CANVAS_PRODUCT_KINDS
            else "node.run"
        ),
        "surface": surface,
        "visibility": visibility,
        "kind": kind,
        "authoring": {
            "kind": kind,
            "canvas": canvas,
        },
    }
    for key in (
        "phase",
        "group",
        "purpose",
        "acceptance",
        "primary_skill",
        "prompt_ref",
        "prompt_spec",
        "manual_only",
        "optional",
        "auto_skip_when",
        "runtime_hidden",
        "extension",
        "extension_config",
        "capability",
        "plugin",
        "plugin_node_type",
        "plugin_inputs",
        "plugin_settings",
        "operation",
        "layout_after",
        "reads_from",
        "output_schema",
        "bindings",
        "inputs_schema",
        "expansion",
        "collection",
        "instance_scope",
        "completion",
        "settings",
        "io",
        "x",
        "x-openreel",
        "item_source",
        "item_name",
        "branch",
        "expand_when",
        "source_node_id",
        "source_label",
        "source_category",
        "source_ui",
        "source_behavior",
    ):
        value = _copy_non_empty(step.get(key))
        if value is not None:
            compiled[key] = value
    ui = step.get("ui") if isinstance(step.get("ui"), dict) else {}
    if ui:
        compiled["ui"] = deepcopy(ui)
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    output = _step_output_spec(step)
    if output:
        compiled["output"] = {
            key: deepcopy(value)
            for key, value in output.items()
            if key not in {"canvas", "show_on_canvas"}
        }
        key = _copy_non_empty(output.get("key"))
        if key is not None:
            compiled.setdefault("fields", {})["workflow_output_key"] = key
    output_mode = _output_mode(step, kind=kind)
    if output_mode:
        compiled["output_mode"] = output_mode
    if kind == "collection":
        schema = compiled.get("output_schema") if isinstance(compiled.get("output_schema"), dict) else {}
        if not schema:
            schema = _step_output_schema(step)
        compiled["output_schema"] = {
            **schema,
            "type": "collection",
            "items_key": str(schema.get("items_key") or schema.get("collection_key") or "items").strip() or "items",
        }
        compiled["collection"] = {
            "kind": "llm_extracted_items",
            **(compiled.get("collection") if isinstance(compiled.get("collection"), dict) else {}),
        }
    if prompt_template:
        compiled["prompt_template"] = prompt_template
    references = _compile_references(step.get("references"))
    if references:
        compiled["reference_selectors"] = references
    context_refs = _copy_non_empty(step.get("reads_from") or step.get("context") or step.get("context_refs"))
    if context_refs is not None:
        compiled["context_refs"] = context_refs
    fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
    if fields:
        compiled["fields"] = {**deepcopy(fields), **compiled.get("fields", {})}
    step_inputs = _copy_non_empty(step.get("inputs"))
    if step_inputs is not None:
        compiled.setdefault("bindings", deepcopy(step_inputs))
    return compiled


def _foreach_key(step: dict[str, Any]) -> str:
    foreach = step.get("for_each")
    if foreach in (None, "", [], {}):
        foreach = step.get("foreach")
    if foreach in (None, "", [], {}):
        return ""
    if isinstance(foreach, dict):
        return repr(sorted(foreach.items(), key=lambda item: str(item[0])))
    return str(foreach).strip()


def _group_id_for_foreach(step: dict[str, Any], *, fallback: str) -> str:
    repeat_group = step.get("repeat_group") or step.get("repeat_group_id") or step.get("group")
    if repeat_group not in (None, "", [], {}):
        return _normalize_id(repeat_group, fallback=fallback)
    foreach = step.get("for_each") or step.get("foreach")
    if isinstance(foreach, str):
        source, path = _split_path(foreach)
        leaf = path.split(".")[-1] if path else source
        if leaf:
            return _normalize_id(leaf, fallback=fallback)
    return fallback


def _external_dependencies(
    children: list[dict[str, Any]],
    child_ids: set[str],
    existing: list[Any] | None = None,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in existing or []:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    for child in children:
        local_deps: list[str] = []
        for dep in child.get("depends_on") or []:
            dep_text = str(dep or "").strip()
            if not dep_text:
                continue
            if dep_text in child_ids:
                local_deps.append(dep_text)
                continue
            if dep_text not in seen:
                seen.add(dep_text)
                result.append(dep_text)
        child["depends_on"] = local_deps
    return result


def _with_unique_id(base: str, used: set[str], *, fallback: str) -> str:
    candidate = _normalize_id(base, fallback=fallback)
    index = 2
    while candidate in used:
        candidate = _normalize_id(f"{base}_{index}", fallback=f"{fallback}_{index}")
        index += 1
    used.add(candidate)
    return candidate


def _split_canvas_media_prompt_step(step: dict[str, Any], *, used_ids: set[str]) -> list[dict[str, Any]]:
    kind = str(step.get("kind") or "").strip().lower().replace("-", "_")
    if kind not in {"image", "video", "audio"}:
        return [step]
    prompt_template = str(step.get("prompt_template") or "").strip()
    if not prompt_template:
        return [step]

    product = deepcopy(step)
    step_id = str(product.get("id") or "").strip()
    prompt_id = _with_unique_id(f"{step_id}_prompt", used_ids, fallback=f"{step_id}_prompt")
    original_deps = _coerce_string_list(product.get("depends_on"))
    prompt_step: dict[str, Any] = {
        "id": prompt_id,
        "title": f"{product.get('title') or step_id}提示词",
        "node_type": "text",
        "depends_on": original_deps,
        "runner": "node.run",
        "surface": "workflow_runtime",
        "visibility": "flow_only",
        "kind": "text",
        "prompt_template": prompt_template,
        "authoring": {
            "kind": "text",
            "prompt_for": step_id,
        },
    }
    for key in (
        "phase",
        "group",
        "purpose",
        "acceptance",
        "primary_skill",
        "prompt_ref",
        "prompt_spec",
        "manual_only",
        "optional",
        "auto_skip_when",
        "runtime_hidden",
        "extension",
        "extension_config",
        "capability",
        "completion",
        "settings",
        "io",
        "x",
        "x-openreel",
        "item_source",
        "item_name",
        "branch",
        "expand_when",
    ):
        value = _copy_non_empty(product.get(key))
        if value is not None:
            prompt_step[key] = value
    fields = product.get("fields") if isinstance(product.get("fields"), dict) else {}
    prompt_fields = {
        key: deepcopy(value)
        for key, value in fields.items()
        if key not in {
            "prompt",
            "visual_prompt",
            "image_prompt",
            "video_prompt",
            "audio_prompt",
            "reference_images",
            "references",
            "depends_on",
            "workflow_source_step",
            "workflow_source_path",
            "workflow_generate",
        }
    }
    if prompt_fields:
        prompt_step["fields"] = prompt_fields
    source_node_id = str(product.get("source_node_id") or "").strip()
    if source_node_id:
        prompt_step["source_node_id"] = f"{source_node_id}Prompt"

    for key in (
        "prompt_template",
        "primary_skill",
        "prompt_ref",
        "prompt_spec",
        "completion",
        "output_mode",
        "output_schema",
    ):
        product.pop(key, None)
    product["runner"] = "workflow_canvas_output"
    product["surface"] = "draft_canvas"
    product["visibility"] = "canvas"
    product["depends_on"] = [prompt_id]
    product_fields = product.get("fields") if isinstance(product.get("fields"), dict) else {}
    product["fields"] = {
        **deepcopy(product_fields),
        "workflow_source_step": prompt_id,
        "workflow_source_path": "output",
        "workflow_generate": True,
    }
    if original_deps:
        existing_context = product.get("context_refs") if isinstance(product.get("context_refs"), list) else []
        product["context_refs"] = [
            *deepcopy(existing_context),
            *({"ref": dep, "role": "visual_reference"} for dep in original_deps),
        ]
    return [prompt_step, product]


def _compile_repeat_spec(raw_repeat: Any, *, foreach: Any, item_name: str) -> dict[str, Any]:
    repeat = deepcopy(raw_repeat) if isinstance(raw_repeat, dict) else {}
    scope_key = str(item_name or repeat.get("scope_key") or repeat.get("item_name") or repeat.get("item") or "").strip()
    if "items" in repeat and "foreach" not in repeat and "instances" not in repeat:
        items = repeat.pop("items")
        if isinstance(items, list):
            repeat["instances"] = items
        else:
            repeat["foreach"] = _compile_foreach(items, item_name=scope_key)
    if foreach not in (None, "", [], {}) and "foreach" not in repeat and "instances" not in repeat:
        repeat["foreach"] = _compile_foreach(foreach, item_name=scope_key)
    if scope_key and "scope_key" not in repeat:
        repeat["scope_key"] = scope_key
    repeat.pop("item", None)
    repeat.pop("item_name", None)
    return repeat


def _compile_explicit_group(raw_step: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    child_steps = raw_step.get("steps")
    if not isinstance(child_steps, list):
        return None
    step_id = str(raw_step.get("id") or "").strip()
    if not step_id:
        step_id = f"group_{index}"
    if not _AUTHORING_ID_RE.fullmatch(step_id):
        step_id = _normalize_id(step_id, fallback=f"group_{index}")
    title = str(raw_step.get("title") or raw_step.get("name") or step_id).strip()
    compiled_children = _compile_steps(child_steps)
    child_ids = {
        str(child.get("id") or "")
        for child in compiled_children
        if isinstance(child, dict) and str(child.get("id") or "")
    }
    repeat = _compile_repeat_spec(
        raw_step.get("repeat"),
        foreach=raw_step.get("for_each") if raw_step.get("for_each") not in (None, "", [], {}) else raw_step.get("foreach"),
        item_name=str(raw_step.get("item_name") or raw_step.get("item") or ""),
    )
    foreach = raw_step.get("for_each") if raw_step.get("for_each") not in (None, "", [], {}) else raw_step.get("foreach")
    group: dict[str, Any] = {
        "id": step_id,
        "title": title,
        "node_type": "text",
        "kind": "loop",
        "role": "repeat_group",
        "depends_on": _external_dependencies(
            compiled_children,
            child_ids,
            _coerce_string_list(raw_step.get("needs") or raw_step.get("depends_on")),
        ),
        "runner": "node.run",
        "surface": "workflow_runtime",
        "visibility": "flow_only",
        "steps": compiled_children,
        "authoring": {
            "kind": "loop",
        },
    }
    if isinstance(repeat, dict) and repeat:
        group["repeat"] = repeat
    elif foreach not in (None, "", [], {}):
        group["repeat"] = {
            "foreach": _compile_foreach(
                foreach,
                item_name=str(raw_step.get("item_name") or raw_step.get("item") or ""),
            )
        }
    for key in (
        "phase",
        "group",
        "purpose",
        "acceptance",
        "primary_skill",
        "prompt_ref",
        "layout_after",
        "reads_from",
        "source_node_id",
        "source_label",
        "source_category",
    ):
        value = _copy_non_empty(raw_step.get(key))
        if value is not None:
            group[key] = value
    context_refs = _copy_non_empty(raw_step.get("reads_from") or raw_step.get("context") or raw_step.get("context_refs"))
    if context_refs is not None:
        group["context_refs"] = context_refs
    fields = raw_step.get("fields") if isinstance(raw_step.get("fields"), dict) else {}
    if fields:
        group["fields"] = deepcopy(fields)
    ui = raw_step.get("ui") if isinstance(raw_step.get("ui"), dict) else {}
    if ui:
        group["ui"] = deepcopy(ui)
    return group


def _compile_steps(steps: list[Any]) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    foreach_groups: dict[str, dict[str, Any]] = {}
    used_ids = {
        str(step.get("id") or "").strip()
        for step in steps
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    }
    for index, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raise WorkflowAuthoringSpecError(f"Authoring step #{index} must be an object")
        explicit_group = _compile_explicit_group(raw_step, index=index)
        if explicit_group is not None:
            compiled.append(explicit_group)
            continue
        child = _compiled_step_base(raw_step, index=index)
        child_steps = _split_canvas_media_prompt_step(child, used_ids=used_ids)
        foreach_key = _foreach_key(raw_step)
        if not foreach_key:
            compiled.extend(child_steps)
            continue
        group = foreach_groups.get(foreach_key)
        if group is None:
            group_id = _group_id_for_foreach(raw_step, fallback=child["id"])
            group = {
                "id": group_id,
                "title": str(raw_step.get("repeat_title") or raw_step.get("group_title") or raw_step.get("title") or group_id),
                "role": "repeat_group",
                "repeat": {
                    "foreach": _compile_foreach(
                        raw_step.get("for_each") or raw_step.get("foreach"),
                        item_name=str(raw_step.get("item_name") or raw_step.get("item") or ""),
                    )
                },
                "steps": [],
            }
            foreach_groups[foreach_key] = group
            compiled.append(group)
        group["steps"].extend(child_steps)
        child_ids = {str(item.get("id") or "") for item in group["steps"] if isinstance(item, dict)}
        group["depends_on"] = _external_dependencies(group["steps"], child_ids, group.get("depends_on"))
    return compiled


def compile_authoring_workflow(raw: dict[str, Any]) -> dict[str, Any]:
    if not is_authoring_workflow(raw):
        return deepcopy(raw)
    workflow_id = str(raw.get("id") or "").strip() or "model_authored_workflow"
    workflow_id = _normalize_id(workflow_id, fallback="model_authored_workflow")
    inputs, required_inputs = _normalize_inputs(raw.get("inputs"))
    explicit_required = _coerce_string_list(raw.get("required_inputs"))
    if explicit_required:
        required_inputs = explicit_required
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowAuthoringSpecError("authoring workflow requires steps")
    compiled: dict[str, Any] = {
        "id": workflow_id,
        "name": str(raw.get("name") or raw.get("title") or workflow_id).strip(),
        "description": str(raw.get("description") or "").strip(),
        "category": str(raw.get("category") or "workflow").strip() or "workflow",
        "workflow_spec_version": RUNTIME_SPEC_VERSION,
        "authoring_spec_version": AUTHORING_SPEC_VERSION,
        "authoring": {
            "schema": AUTHORING_SPEC_VERSION,
            "source_schema": raw.get("schema") or raw.get("authoring_spec_version") or AUTHORING_SPEC_VERSION,
        },
        "inputs": inputs,
        "required_inputs": required_inputs,
        "steps": _compile_steps(steps),
    }
    for key in ("defaults", "dimensions", "ui", "phases", "extensions", "capabilities", "required_capabilities", "required_extensions"):
        value = _copy_non_empty(raw.get(key))
        if value is not None:
            compiled[key] = value
    return compiled
