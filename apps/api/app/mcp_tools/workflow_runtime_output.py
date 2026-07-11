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
WORKFLOW_RUNTIME_OUTPUT_PREVIEW_LIMIT = 6000
WORKFLOW_RUNTIME_OUTPUT_LABELS = {
    "title": "标题",
    "logline": "一句话梗概",
    "summary": "摘要",
    "description": "说明",
    "characters": "人物",
    "character": "人物",
    "segments": "分段",
    "segment": "分段",
    "scenes": "场景",
    "scene": "场景",
    "shots": "镜头",
    "frames": "画面",
    "actions": "动作",
    "dialogue": "对白",
    "location": "地点",
    "mood": "情绪",
    "duration_seconds": "时长",
    "duration": "时长",
    "prompt": "提示词",
    "visual_prompt": "视觉提示词",
    "video_prompt": "视频提示词",
    "image_prompt": "图片提示词",
    "style": "视觉风格",
    "references": "参考",
    "notes": "备注",
    "name": "名称",
    "role": "角色",
    "goal": "目标",
    "output": "输出",
}
WORKFLOW_RUNTIME_PREVIEW_HIDDEN_KEYS = {
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
}


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


def _output_label(key: str, labels: dict[str, str] | None = None) -> str:
    text = str(key or "").strip()
    if not text:
        return "内容"
    if labels and labels.get(text):
        return labels[text]
    return WORKFLOW_RUNTIME_OUTPUT_LABELS.get(text, text.replace("_", " "))


def _labels_from_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        field = str(key or "").strip()
        if not field:
            continue
        if isinstance(item, str) and item.strip():
            result[field] = item.strip()
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("title") or item.get("name") or "").strip()
            if label:
                result[field] = label
    return result


def _output_label_map(
    record: dict[str, Any],
    *,
    workflow_override: dict[str, Any] | None = None,
) -> dict[str, str]:
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    workflow = workflow_override if isinstance(workflow_override, dict) else (
        record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    )
    if not workflow and isinstance(fields.get("workflow"), dict):
        workflow = fields["workflow"]
    output = workflow.get("output") if isinstance(workflow.get("output"), dict) else {}
    schema = workflow.get("output_schema") if isinstance(workflow.get("output_schema"), dict) else {}
    labels: dict[str, str] = {}
    labels.update(_labels_from_mapping(output.get("labels")))
    labels.update(_labels_from_mapping(schema.get("labels")))
    labels.update(_labels_from_mapping(schema.get("properties")))
    fields_list = schema.get("fields")
    if isinstance(fields_list, list):
        for item in fields_list:
            if not isinstance(item, dict):
                continue
            field = str(item.get("id") or item.get("key") or item.get("name") or "").strip()
            label = str(item.get("label") or item.get("title") or "").strip()
            if field and label:
                labels[field] = label
    return labels


def _hidden_keys_from_mapping(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, list):
        result.update(str(item or "").strip() for item in value if str(item or "").strip())
    elif isinstance(value, dict):
        for key, enabled in value.items():
            if enabled is False:
                continue
            text = str(key or "").strip()
            if text:
                result.add(text)
    return result


def _output_hidden_keys(
    record: dict[str, Any],
    *,
    workflow_override: dict[str, Any] | None = None,
) -> set[str]:
    fields = record.get("input") if isinstance(record.get("input"), dict) else {}
    workflow = workflow_override if isinstance(workflow_override, dict) else (
        record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    )
    if not workflow and isinstance(fields.get("workflow"), dict):
        workflow = fields["workflow"]
    output = workflow.get("output") if isinstance(workflow.get("output"), dict) else {}
    schema = workflow.get("output_schema") if isinstance(workflow.get("output_schema"), dict) else {}
    hidden = set(WORKFLOW_RUNTIME_PREVIEW_HIDDEN_KEYS)
    for source in (output, schema):
        for key in ("hidden", "hidden_keys", "exclude", "exclude_keys"):
            hidden.update(_hidden_keys_from_mapping(source.get(key)))
    return hidden


def _preview_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value).strip()


def _preview_item_title(item: dict[str, Any], index: int, parent_key: str = "") -> str:
    for key in ("title", "name", "summary"):
        value = _preview_scalar(item.get(key))
        if value:
            return value
    item_index = _preview_scalar(item.get("index"))
    if item_index:
        if parent_key in {"segments", "segment"}:
            return f"第{item_index}段"
        if parent_key in {"shots", "frames"}:
            return f"第{item_index}格"
        return f"第{item_index}项"
    return f"第{index}项"


def _preview_lines(
    value: Any,
    *,
    depth: int = 0,
    parent_key: str = "",
    labels: dict[str, str] | None = None,
    hidden_keys: set[str] | None = None,
) -> list[str]:
    hidden = hidden_keys if hidden_keys is not None else WORKFLOW_RUNTIME_PREVIEW_HIDDEN_KEYS
    if value in (None, "", [], {}):
        return []
    structured = structured_workflow_output(value)
    if isinstance(structured, str):
        parsed = structured_workflow_output(structured)
        if parsed is not structured:
            return _preview_lines(parsed, depth=depth, parent_key=parent_key, labels=labels, hidden_keys=hidden)
        return [structured.strip()] if structured.strip() else []
    if isinstance(structured, (int, float, bool)):
        scalar = _preview_scalar(structured)
        return [scalar] if scalar else []
    if isinstance(structured, list):
        lines: list[str] = []
        for index, item in enumerate(structured, start=1):
            if item in (None, "", [], {}):
                continue
            if isinstance(item, dict):
                title = _preview_item_title(item, index, parent_key)
                child_obj = {
                    key: child_value
                    for key, child_value in item.items()
                    if key not in {"title", "name", "index"}
                    and key not in hidden
                    and child_value not in (None, "", [], {})
                }
                child_lines = _preview_lines(
                    child_obj,
                    depth=depth + 1,
                    parent_key=parent_key,
                    labels=labels,
                    hidden_keys=hidden,
                )
                lines.append(f"- {title}")
                lines.extend([f"  {line}" for line in child_lines])
            elif isinstance(item, list):
                child_lines = _preview_lines(
                    item,
                    depth=depth + 1,
                    parent_key=parent_key,
                    labels=labels,
                    hidden_keys=hidden,
                )
                if child_lines:
                    lines.append(f"- 第{index}项")
                    lines.extend([f"  {line}" for line in child_lines])
            else:
                child_lines = _preview_lines(
                    item,
                    depth=depth + 1,
                    parent_key=parent_key,
                    labels=labels,
                    hidden_keys=hidden,
                )
                lines.extend([f"- {line}" for line in child_lines])
        return lines
    if not isinstance(structured, dict):
        return []

    obj = dict(structured)
    content = obj.get("content")
    if content not in (None, "", [], {}):
        parsed_content = structured_workflow_output(content)
        if len(obj) == 1 or parsed_content is not content:
            content_lines = _preview_lines(
                parsed_content,
                depth=depth,
                parent_key=parent_key,
                labels=labels,
                hidden_keys=hidden,
            )
            if len(obj) == 1:
                return content_lines
        if len(obj) > 1:
            obj.pop("content", None)

    lines: list[str] = []
    for key, item in obj.items():
        if key in hidden or item in (None, "", [], {}):
            continue
        label = _output_label(key, labels)
        if isinstance(item, (dict, list)):
            child_lines = _preview_lines(
                item,
                depth=depth + 1,
                parent_key=key,
                labels=labels,
                hidden_keys=hidden,
            )
            if child_lines:
                lines.append(f"{label}:")
                lines.extend([f"  {line}" for line in child_lines])
            continue
        scalar = _preview_scalar(item)
        if scalar:
            lines.append(f"{label}: {scalar}")
    return lines


def workflow_runtime_output_preview(
    record: dict[str, Any],
    *,
    limit: int = WORKFLOW_RUNTIME_OUTPUT_PREVIEW_LIMIT,
    workflow_override: dict[str, Any] | None = None,
) -> str:
    value = workflow_runtime_primary_output_value(record)
    lines = _preview_lines(
        value,
        labels=_output_label_map(record, workflow_override=workflow_override),
        hidden_keys=_output_hidden_keys(record, workflow_override=workflow_override),
    )
    text = "\n".join(line for line in lines if line).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n...（已截断）"
