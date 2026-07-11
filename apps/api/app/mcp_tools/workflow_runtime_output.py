"""Normalization of workflow runner output before persistence and UI display."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


WORKFLOW_RUNTIME_RESULT_WRAPPER_KEYS = {
    "ok",
    "status",
    "result",
    "run_result",
    "node",
    "node_id",
    "_canvas_node_id",
    "_canvas_id",
    "_canvas_display_id",
    "runtime_step",
    "created",
}
WORKFLOW_RUNTIME_INTERNAL_OUTPUT_KEYS = {
    "id",
    "key",
    "type",
    "ok",
    "status",
    "provider",
    "model",
    "requested_model",
    "fallback_used",
    "model_tier",
    "url",
    "local_url",
    "remote_url",
    "local_path",
    "output_path",
    "asset_id",
    "asset_ids",
    "images",
    "media",
    "attempts",
    "n_index",
    "n_requested",
    "n_succeeded",
    "width",
    "height",
    "size_requested",
    "size_final",
    "quality_requested",
    "actual_aspect_ratio",
    "reference_warnings",
    "partial_error",
    "error",
    "error_message",
    "workflow_text_runner",
    "workflow_runtime_runner",
    "llm_task_type",
    "usage",
    "run_id",
    "prompt_dump_run_id",
    "raw_usage_keys",
    "node_id",
    "source_node_id",
    "template_id",
    "template_step_id",
    "instance_id",
    "step_id",
    "segment_index",
    "_canvas_node_id",
    "_canvas_id",
    "_canvas_display_id",
    "action",
    "async",
    "changes",
    "diagnosis",
    "depends_on",
    "exception_type",
    "hint",
    "job_id",
    "node",
    "node_render_attempts",
    "references",
    "render_state",
    "recovered_from_running_output",
    "suggested_next",
    "suggested_patch",
}
WORKFLOW_RUNTIME_CONTENT_KEYS = (
    "content",
    "full_text",
    "story_text",
    "script",
    "text",
    "prompt",
    "video_prompt",
    "image_prompt",
)
WORKFLOW_RUNTIME_MEDIA_OUTPUT_KEYS = (
    "url",
    "local_url",
    "remote_url",
    "output_path",
    "asset_id",
)


def json_object_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates = [stripped] if stripped else []
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", stripped, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if block:
            candidates.append(block)
    starts = [index for index, char in enumerate(stripped) if char == "{"]
    for start in starts[:8]:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(stripped)):
            char = stripped[index]
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
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(stripped[start:index + 1])
                    break
    return candidates


def parse_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return deepcopy(value)
    if not isinstance(value, str):
        return None
    for candidate in json_object_candidates(value):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def structured_workflow_output(value: Any) -> Any:
    parsed = parse_json_object(value)
    if parsed is None:
        return value
    content = parsed.get("content")
    parsed_content = parse_json_object(content)
    if parsed_content is None:
        return parsed
    result = {**parsed, **parsed_content}
    result["content"] = content
    return result


def workflow_output_value_type(value: Any) -> str:
    structured = structured_workflow_output(value)
    if isinstance(structured, dict):
        return "json"
    if isinstance(structured, list):
        return "array"
    if isinstance(structured, (int, float, bool)):
        return "scalar"
    return "text"


def workflow_runtime_clean_output_value(
    value: Any,
    *,
    depth: int = 0,
    drop_internal_keys: bool = False,
) -> Any:
    if value in (None, "", [], {}):
        return value
    if depth > 8:
        return value
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := workflow_runtime_clean_output_value(
                item,
                depth=depth + 1,
                drop_internal_keys=drop_internal_keys,
            )) not in (None, "", [], {})
        ]
    if not isinstance(value, dict):
        return value
    wrapper_value = value.get("run_result")
    if wrapper_value in (None, "", [], {}):
        wrapper_value = value.get("result")
    if wrapper_value not in (None, "", [], {}) and any(
        key in value for key in WORKFLOW_RUNTIME_RESULT_WRAPPER_KEYS
    ):
        return workflow_runtime_clean_output_value(
            wrapper_value,
            depth=depth + 1,
            drop_internal_keys=drop_internal_keys,
        )
    internal_keys = set(value) & WORKFLOW_RUNTIME_INTERNAL_OUTPUT_KEYS
    runner_internal_keys = internal_keys - {"type", "title", "name"}
    content_key = next(
        (key for key in WORKFLOW_RUNTIME_CONTENT_KEYS if value.get(key) not in (None, "", [], {})),
        "",
    )
    if content_key and runner_internal_keys:
        return structured_workflow_output(value[content_key])
    if runner_internal_keys and any(
        value.get(key) not in (None, "", [], {})
        for key in WORKFLOW_RUNTIME_MEDIA_OUTPUT_KEYS
    ):
        return {
            key: value[key]
            for key in WORKFLOW_RUNTIME_MEDIA_OUTPUT_KEYS
            if value.get(key) not in (None, "", [], {})
        }
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if (
            drop_internal_keys and key in WORKFLOW_RUNTIME_INTERNAL_OUTPUT_KEYS
        ) or item in (None, "", [], {}):
            continue
        cleaned_item = workflow_runtime_clean_output_value(
            item,
            depth=depth + 1,
            drop_internal_keys=drop_internal_keys,
        )
        if cleaned_item not in (None, "", [], {}):
            cleaned[key] = cleaned_item
    return cleaned


def workflow_runtime_clean_outputs(
    outputs: Any,
    *,
    drop_internal_keys: bool = False,
) -> list[dict[str, Any]] | None:
    if not isinstance(outputs, list):
        return None
    result: list[dict[str, Any]] = []
    for index, item in enumerate(outputs):
        if not isinstance(item, dict):
            cleaned_value = workflow_runtime_clean_output_value(
                item,
                drop_internal_keys=drop_internal_keys,
            )
            if cleaned_value not in (None, "", [], {}):
                result.extend(workflow_runtime_outputs_from_value(cleaned_value, name=f"output_{index + 1}"))
            continue
        cleaned_value = workflow_runtime_clean_output_value(
            item.get("value"),
            drop_internal_keys=drop_internal_keys,
        )
        if cleaned_value in (None, "", [], {}):
            continue
        result.append({
            **{
                key: value
                for key, value in item.items()
                if key in {"name", "label", "title", "type"} and value not in (None, "", [], {})
            },
            "name": str(item.get("name") or item.get("key") or f"output_{index + 1}"),
            "type": workflow_output_value_type(cleaned_value),
            "value": structured_workflow_output(cleaned_value),
        })
    return result


def workflow_runtime_output_from_runner_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return workflow_runtime_clean_output_value(payload)
    content_key = next(
        (key for key in WORKFLOW_RUNTIME_CONTENT_KEYS if payload.get(key) not in (None, "", [], {})),
        "",
    )
    if content_key:
        content = structured_workflow_output(payload[content_key])
        if isinstance(content, dict):
            return content
        return {content_key: content}
    media_output = {
        key: payload[key]
        for key in WORKFLOW_RUNTIME_MEDIA_OUTPUT_KEYS
        if payload.get(key) not in (None, "", [], {})
    }
    if media_output:
        return media_output
    return workflow_runtime_clean_output_value(payload, drop_internal_keys=True)


def workflow_runtime_outputs_from_value(value: Any, *, name: str = "output") -> list[dict[str, Any]]:
    if value is None:
        return []
    cleaned = workflow_runtime_clean_output_value(value)
    if cleaned in (None, "", [], {}):
        return []
    structured = structured_workflow_output(cleaned)
    return [{
        "name": name,
        "type": workflow_output_value_type(structured),
        "value": structured,
    }]


def workflow_runtime_primary_output_value(record: dict[str, Any]) -> Any:
    outputs = record.get("outputs") if isinstance(record.get("outputs"), list) else []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        value = workflow_runtime_clean_output_value(output.get("value"), drop_internal_keys=True)
        if value not in (None, "", [], {}):
            return value
    return workflow_runtime_clean_output_value(record.get("output"), drop_internal_keys=True)
