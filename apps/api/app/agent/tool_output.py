"""Codex-style separation for tool results.

Tool handlers still return their local raw result. The agent loop wraps that
result at the boundary so model context, raw artifacts, trace diagnostics, and
UI events do not all consume the same payload.
"""
from __future__ import annotations

import json
from typing import Any

from app.agent import context_compact
from app.agent.vision_context import redact_image_data_urls
from app.agent.tool_observation import (
    MODEL_OBSERVATION_VERSION,
    build_model_observation,
    result_handler_ok,
)

TOOL_OUTPUT_VERSION = "tool_output_v1"


def build_tool_output_envelope(
    result: Any,
    *,
    project_id: str,
    run_id: str,
    iteration: int,
    tool_name: str,
    budget_chars: int = context_compact.TOOL_RESULT_CONTEXT_BUDGET_CHARS,
) -> dict[str, Any]:
    model_content_parts = _model_content_parts(result)
    result_for_context = _strip_model_content(result)
    raw_json = json.dumps(result_for_context, ensure_ascii=False, default=str)
    model_content = context_compact.prepare_tool_result_for_context(
        result_for_context,
        project_id=project_id,
        run_id=run_id,
        iteration=iteration,
        tool_name=tool_name,
        budget_chars=budget_chars,
    )
    model_payload = _loads_json_value(model_content)
    model_payload_object = model_payload if isinstance(model_payload, dict) else {}
    compacted = bool(model_payload_object.get("tool_result_compacted"))
    artifact_path = str(model_payload_object.get("full_result_path") or "") if compacted else ""
    summary = model_payload_object.get("summary") if compacted else _summary_for_trace(result_for_context, tool_name=tool_name)
    model_observation = build_model_observation(
        result_for_context,
        tool_name=tool_name,
        model_payload=model_payload,
    )
    handler_ok = bool(model_observation["handler_ok"])
    outcome = str(model_observation["outcome"])
    success = bool(model_observation["success"])
    next_action = str(model_observation.get("next_action") or "")
    observation_content = json.dumps(model_observation, ensure_ascii=False, default=str)
    trace_payload = {
        "tool_output_version": TOOL_OUTPUT_VERSION,
        "tool_observation_version": MODEL_OBSERVATION_VERSION,
        "tool_result_ok": handler_ok,
        "tool_result_handler_ok": handler_ok,
        "tool_result_success": success,
        "tool_result_outcome": outcome,
        "tool_result_next_action": next_action,
        "tool_result_compacted": compacted,
        "tool_result_artifact_path": artifact_path or None,
        "tool_result_raw_chars": len(raw_json),
        "tool_result_payload_chars": len(model_content),
        "tool_result_model_visible_chars": len(observation_content),
        "tool_result_summary": summary,
        "tool_result_keys": _result_keys(result_for_context),
        "tool_result_multimodal_parts": len(model_content_parts),
        "tool_result_multimodal_images": _model_content_image_count(model_content_parts),
    }
    ui_result = _ui_result(
        result_for_context,
        compacted=compacted,
        artifact_path=artifact_path,
        summary=summary,
        raw_chars=len(raw_json),
    )
    return {
        "version": TOOL_OUTPUT_VERSION,
        "tool": tool_name,
        "ok": handler_ok,
        "success": success,
        "outcome": outcome,
        "handler_ok": handler_ok,
        "model_visible": {
            "content": observation_content,
            "content_type": "json",
            "content_parts": model_content_parts,
            "compacted": compacted,
            "chars": len(observation_content),
            "summary": summary,
            "artifact_path": artifact_path or None,
        },
        "raw_artifact": {
            "path": artifact_path,
            "original_chars": len(raw_json),
        } if compacted and artifact_path else None,
        "trace": trace_payload,
        "ui": {
            "result": ui_result,
            "success": success,
            "outcome": outcome,
            "handler_ok": handler_ok,
            "summary": summary,
            "compacted": compacted,
            "artifact_path": artifact_path or None,
            "raw_result_chars": len(raw_json),
            "model_visible_chars": len(observation_content),
        },
    }


def tool_result_message(tool_call_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": str((envelope.get("model_visible") or {}).get("content") or ""),
    }


def tool_result_messages(tool_call_id: str, envelope: dict[str, Any]) -> list[dict[str, Any]]:
    return [tool_result_message(tool_call_id, envelope), *tool_result_context_messages(tool_call_id, envelope)]


def tool_result_context_messages(tool_call_id: str, envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Return model messages for a tool result.

    Chat Completions tool messages are text-only in the local SDK.  Multimodal
    tool output is therefore bridged as a user-context message after the
    required textual tool result.  The live message carries pixels; database
    history stores only references/metadata and hydrates them on later turns.
    """
    model_visible = envelope.get("model_visible") if isinstance(envelope.get("model_visible"), dict) else {}
    parts = model_visible.get("content_parts") if isinstance(model_visible, dict) else None
    if not isinstance(parts, list) or not parts:
        return []
    if not any(isinstance(part, dict) and part.get("type") == "text" for part in parts):
        parts = [
            {
                "type": "text",
                "text": "The previous tool returned visual evidence for the current request.",
            },
            *parts,
        ]
    return [{
        "role": "user",
        "content": parts,
        "_tool_image_context": True,
        "_tool_call_id": tool_call_id,
        "_tool_name": envelope.get("tool"),
    }]


def tool_done_event(tool_name: str, round_number: int, envelope: dict[str, Any]) -> dict[str, Any]:
    ui = envelope.get("ui") if isinstance(envelope.get("ui"), dict) else {}
    return {
        "type": "tool_done",
        "tool": tool_name,
        "round": round_number,
        "result": ui.get("result"),
        "tool_output": {
            "version": envelope.get("version"),
            "success": envelope.get("success"),
            "outcome": envelope.get("outcome"),
            "handler_ok": envelope.get("handler_ok"),
            "summary": ui.get("summary"),
            "compacted": bool(ui.get("compacted")),
            "artifact_path": ui.get("artifact_path"),
            "raw_result_chars": ui.get("raw_result_chars"),
            "model_visible_chars": ui.get("model_visible_chars"),
        },
    }


def tool_trace_fields(envelope: dict[str, Any]) -> dict[str, Any]:
    trace = envelope.get("trace")
    return dict(trace) if isinstance(trace, dict) else {"tool_output_version": TOOL_OUTPUT_VERSION}


def _loads_json_value(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def _result_keys(result: Any) -> list[str]:
    if isinstance(result, dict):
        return [str(key) for key in list(result.keys())[:24]]
    if isinstance(result, list):
        return ["list", f"items:{len(result)}"]
    return [type(result).__name__]


def _summary_for_trace(result: Any, *, tool_name: str = "") -> Any:
    if isinstance(result, dict):
        if tool_name in {"node.create", "node.get", "node.list", "node.run", "node.update", "agent.review"}:
            return context_compact.summarize_tool_result_for_context(tool_name, result)
        summary: dict[str, Any] = {
            "ok": result_handler_ok(result),
            "keys": _result_keys(result)[:12],
        }
        for key in (
            "error",
            "error_kind",
            "hint",
            "suggested_next",
            "status",
            "message",
            "node_id",
            "id",
            "_deferred_tool",
            "url",
            "local_url",
            "remote_url",
        ):
            value = result.get(key)
            if value not in (None, "", [], {}):
                summary[key] = value
        nested = result.get("result")
        if isinstance(nested, dict):
            for key in ("status", "url", "local_url", "remote_url", "n_succeeded"):
                value = nested.get(key)
                if value not in (None, "", [], {}):
                    summary[f"result_{key}"] = value
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        stages = output.get("stages") if isinstance(output, dict) else None
        if isinstance(stages, list) and stages and isinstance(stages[0], dict):
            first_stage = stages[0]
            for key in ("status", "url", "local_url", "remote_url", "size", "aspect_ratio"):
                value = first_stage.get(key)
                if value not in (None, "", [], {}):
                    summary[f"output_{key}"] = value
        review = result.get("result") if isinstance(result.get("result"), dict) else {}
        if result.get("review_status") or review.get("status"):
            summary["review_status"] = result.get("review_status") or review.get("status")
            findings = review.get("findings") if isinstance(review.get("findings"), list) else []
            summary["findings_count"] = len(findings)
            for key in ("safe_to_run", "safe_to_submit", "passed"):
                value = review.get(key)
                if value not in (None, "", [], {}):
                    summary[key] = value
        return summary
    if isinstance(result, list):
        return {"ok": True, "type": "list", "items": len(result)}
    return {"ok": True, "type": type(result).__name__}


def _model_content_parts(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    raw = result.get("_model_content")
    if not isinstance(raw, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append({"type": "text", "text": text})
        elif kind == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "")
                if url:
                    payload = {"url": url}
                    detail = image_url.get("detail")
                    if detail not in (None, "", [], {}):
                        payload["detail"] = str(detail)
                    parts.append({"type": "image_url", "image_url": payload})
    return parts


def _model_content_image_count(parts: list[dict[str, Any]]) -> int:
    return sum(1 for part in parts if isinstance(part, dict) and part.get("type") == "image_url")


def _strip_model_content(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if "_model_content" not in result:
        return result
    parts = _model_content_parts(result)
    stripped = {
        key: value
        for key, value in result.items()
        if key != "_model_content"
    }
    stripped["_model_content"] = {
        "omitted": True,
        "content_type": str(result.get("_model_content_type") or "multimodal"),
        "parts": len(parts),
        "images": _model_content_image_count(parts),
        "redaction": "Image bytes are omitted from logs/artifacts; persisted history stores image references only.",
    }
    return redact_image_data_urls(stripped)


def _ui_result(
    result: Any,
    *,
    compacted: bool,
    artifact_path: str,
    summary: Any,
    raw_chars: int,
) -> Any:
    if not compacted:
        return result
    payload: dict[str, Any] = {
        "tool_result_compacted": True,
        "summary": summary,
        "full_result_path": artifact_path or None,
        "original_chars": raw_chars,
    }
    if isinstance(result, dict):
        for key in (
            "ok",
            "error",
            "error_kind",
            "hint",
            "suggested_next",
            "requires_user_confirm",
            "status",
            "message",
            "summary_text",
            "assistant_text",
            "id",
            "node_id",
            "project_id",
            "_deferred_tool",
        ):
            value = result.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value
    else:
        payload["ok"] = True
    return payload
