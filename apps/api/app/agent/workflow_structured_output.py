"""Generic structured LLM output helpers for workflow steps.

The user-facing workflow editor owns the field schema. Runtime code turns that
schema into a strict JSON contract for the LLM and validates the response.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


class WorkflowStructuredOutputError(ValueError):
    """Raised when a workflow LLM output does not match its declared schema."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _output_mode(workflow: dict[str, Any]) -> str:
    output = _as_dict(workflow.get("output"))
    return str(workflow.get("output_mode") or output.get("mode") or "").strip().lower()


def _schema(workflow: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(workflow.get("output_schema"))


def _field_id(field: dict[str, Any]) -> str:
    return str(field.get("id") or field.get("key") or field.get("name") or "").strip()


def _schema_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    fields = schema.get("fields")
    if not isinstance(fields, list):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            fields = [
                {"id": key, **(_as_dict(value))}
                for key, value in properties.items()
            ]
        else:
            fields = []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in fields:
        if not isinstance(item, dict):
            continue
        field = dict(item)
        field_id = _field_id(field)
        if not field_id or field_id in seen:
            continue
        seen.add(field_id)
        field["id"] = field_id
        result.append(field)
    return result


def _is_collection_schema(workflow: dict[str, Any], schema: dict[str, Any]) -> bool:
    markers = [
        schema.get("type"),
        schema.get("kind"),
        schema.get("mode"),
        schema.get("shape"),
        schema.get("format"),
    ]
    if any(str(item or "").strip().lower() in {"collection", "list", "array", "table", "rows", "objects"} for item in markers):
        return True
    collection = workflow.get("collection")
    if isinstance(collection, dict) and collection:
        return True
    return any(key in schema for key in ("items", "item_schema", "item_fields", "items_key", "collection_key"))


def _collection_key(schema: dict[str, Any]) -> str:
    for key in ("items_key", "collection_key", "key"):
        value = str(schema.get(key) or "").strip()
        if value:
            return value
    return "items"


def structured_output_contract(workflow: dict[str, Any]) -> dict[str, Any] | None:
    if _output_mode(workflow) != "json":
        return None
    schema = _schema(workflow)
    fields = _schema_fields(schema)
    collection = _is_collection_schema(workflow, schema)
    return {
        "mode": "json",
        "shape": "collection" if collection else "object",
        "collection_key": _collection_key(schema) if collection else "",
        "fields": [
            {
                "id": _field_id(field),
                "label": field.get("label") or field.get("title") or field.get("name") or _field_id(field),
                "type": field.get("type") or "string",
                "required": field.get("required") is True,
                "description": field.get("description") or "",
            }
            for field in fields
        ],
        "allow_extra_fields": schema.get("allow_extra_fields") is True,
    }


def structured_output_instructions(workflow: dict[str, Any]) -> str:
    contract = structured_output_contract(workflow)
    if not contract:
        return ""
    fields = contract.get("fields") if isinstance(contract.get("fields"), list) else []
    allow_extra = contract.get("allow_extra_fields") is True
    lines = [
        "Structured output contract:",
        "Return only valid JSON. Do not wrap it in Markdown. Do not add explanations.",
    ]
    if contract.get("shape") == "collection":
        key = str(contract.get("collection_key") or "items")
        lines.append(f"Return one JSON object with a top-level array field named {json.dumps(key, ensure_ascii=False)}.")
        if fields:
            lines.append(f"Every item in {key} must be an object with these fields:")
        else:
            lines.append(f"Every item in {key} must be an object.")
    else:
        lines.append("Return one JSON object.")
        if fields:
            lines.append("The object fields are:")
    for field in fields:
        required = "required" if field.get("required") else "optional"
        desc = _scalar_text(field.get("description"))
        label = _scalar_text(field.get("label"))
        field_id = _scalar_text(field.get("id"))
        field_type = _scalar_text(field.get("type")) or "string"
        suffix = f"; {desc}" if desc else ""
        label_part = f" ({label})" if label and label != field_id else ""
        lines.append(f"- {field_id}{label_part}: {field_type}, {required}{suffix}")
    if fields and not allow_extra:
        lines.append("Use exactly these field ids; do not invent additional keys inside structured objects.")
    return "\n".join(lines)


def _json_candidates(value: str) -> list[str]:
    text = value.strip()
    candidates = [text] if text else []
    for match in _JSON_FENCE_RE.finditer(text):
        block = match.group(1).strip()
        if block:
            candidates.append(block)
    for opener, closer in (("{", "}"), ("[", "]")):
        starts = [index for index, char in enumerate(text) if char == opener]
        for start in starts[:8]:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start:index + 1])
                        break
    return candidates


def parse_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    if not isinstance(value, str):
        raise WorkflowStructuredOutputError("structured output must be JSON text")
    for candidate in _json_candidates(value):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    raise WorkflowStructuredOutputError("model did not return valid JSON")


def _find_collection_items(parsed: Any, key: str) -> tuple[dict[str, Any], list[Any]]:
    if isinstance(parsed, list):
        return {key: parsed}, parsed
    if not isinstance(parsed, dict):
        raise WorkflowStructuredOutputError("collection output must be a JSON object or array")
    if isinstance(parsed.get(key), list):
        return parsed, list(parsed[key])
    for fallback_key in ("items", "rows", "list", "results", "data"):
        if isinstance(parsed.get(fallback_key), list):
            normalized = dict(parsed)
            normalized[key] = list(parsed[fallback_key])
            return normalized, list(parsed[fallback_key])
    list_values = [(item_key, item_value) for item_key, item_value in parsed.items() if isinstance(item_value, list)]
    if len(list_values) == 1:
        normalized = dict(parsed)
        normalized[key] = list(list_values[0][1])
        return normalized, list(list_values[0][1])
    raise WorkflowStructuredOutputError(f"collection output missing array field {key!r}")


def _validate_required_fields(obj: dict[str, Any], fields: list[dict[str, Any]], *, label: str) -> None:
    missing = [
        _field_id(field)
        for field in fields
        if field.get("required") is True and obj.get(_field_id(field)) in (None, "", [], {})
    ]
    if missing:
        raise WorkflowStructuredOutputError(f"{label} missing required fields: {', '.join(missing)}")


def parse_structured_output(value: Any, workflow: dict[str, Any]) -> Any:
    contract = structured_output_contract(workflow)
    if not contract:
        return value
    schema = _schema(workflow)
    fields = _schema_fields(schema)
    parsed = parse_json_value(value)
    if contract.get("shape") == "collection":
        key = str(contract.get("collection_key") or "items")
        normalized, items = _find_collection_items(parsed, key)
        normalized_items: list[Any] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                item = {"value": item}
            item = dict(item)
            _validate_required_fields(item, fields, label=f"{key}[{index}]")
            normalized_items.append(item)
        normalized[key] = normalized_items
        return normalized
    if not isinstance(parsed, dict):
        raise WorkflowStructuredOutputError("structured output must be a JSON object")
    _validate_required_fields(parsed, fields, label="structured output")
    return parsed
