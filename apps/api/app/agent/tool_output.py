"""Tool-result boundary for model context, UI, trace, and artifacts."""
from __future__ import annotations

from typing import Any, Iterable

from app.agent.model_context.compiler import (
    MODEL_OBSERVATION_VERSION,
    compile_tool_output,
)
from app.agent.model_context.policy import JSON_OUTPUT_POLICY, ToolOutputPolicy
from app.agent.model_context.types import ToolContentPart, ToolOutput, coerce_tool_output


TOOL_OUTPUT_VERSION = "tool_output_v2"


def build_tool_output_envelope(
    result: Any,
    *,
    project_id: str,
    run_id: str,
    iteration: int,
    tool_name: str,
    requested_tokens: int | None = None,
    content_parts: Iterable[ToolContentPart] | None = None,
) -> dict[str, Any]:
    typed = coerce_tool_output(result)
    if content_parts is not None:
        typed = ToolOutput(
            value=typed.value,
            content_parts=tuple(content_parts),
            content_refs=typed.content_refs,
            contains_external_context=typed.contains_external_context,
        )
    policy = _output_policy(tool_name, typed.value)
    compiled = compile_tool_output(
        typed,
        project_id=project_id,
        run_id=run_id,
        iteration=iteration,
        tool_name=tool_name,
        policy=policy,
        requested_tokens=requested_tokens,
    )
    artifact_ref = compiled.artifact.ref if compiled.artifact else None
    trace_payload = {
        "tool_output_version": TOOL_OUTPUT_VERSION,
        "tool_observation_version": MODEL_OBSERVATION_VERSION,
        "tool_result_ok": compiled.handler_ok,
        "tool_result_handler_ok": compiled.handler_ok,
        "tool_result_success": compiled.success,
        "tool_result_outcome": compiled.outcome,
        "tool_result_next_action": compiled.next_action,
        "tool_result_compacted": compiled.compacted,
        "tool_result_artifact_ref": artifact_ref,
        "tool_result_raw_tokens": compiled.original_tokens,
        "tool_result_model_visible_tokens": compiled.model_tokens,
        "tool_result_model_visible_chars": len(compiled.observation_text),
        "tool_result_summary": compiled.trace_summary,
        "tool_result_keys": _result_keys(compiled.raw_value),
        "tool_result_multimodal_parts": len(compiled.content_parts),
        "tool_result_multimodal_images": sum(
            1 for part in compiled.content_parts if part.get("type") == "image_url"
        ),
        "tool_result_contains_external_context": compiled.contains_external_context,
        "tool_result_output_policy": policy.as_dict(),
    }
    return {
        "version": TOOL_OUTPUT_VERSION,
        "tool": tool_name,
        "ok": compiled.handler_ok,
        "success": compiled.success,
        "outcome": compiled.outcome,
        "handler_ok": compiled.handler_ok,
        "model_visible": {
            "content": compiled.observation_text,
            "content_type": "json",
            "content_parts": list(compiled.content_parts),
            "compacted": compiled.compacted,
            "chars": len(compiled.observation_text),
            "tokens": compiled.model_tokens,
            "summary": compiled.trace_summary,
            "artifact_ref": artifact_ref,
        },
        "raw_artifact": {
            "ref": artifact_ref,
            "original_bytes": compiled.artifact.original_bytes,
        } if compiled.artifact else None,
        "trace": trace_payload,
        "ui": {
            "result": compiled.ui_result,
            "success": compiled.success,
            "outcome": compiled.outcome,
            "handler_ok": compiled.handler_ok,
            "summary": compiled.trace_summary,
            "compacted": compiled.compacted,
            "artifact_ref": artifact_ref,
            "raw_result_tokens": compiled.original_tokens,
            "model_visible_tokens": compiled.model_tokens,
        },
    }


def tool_result_input_item(tool_call_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Build a Responses function_call_output item for the next model turn."""
    return {
        "type": "function_call_output",
        "call_id": tool_call_id,
        "output": str((envelope.get("model_visible") or {}).get("content") or ""),
    }


def tool_result_context_messages(tool_call_id: str, envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Render typed media as a provider-compatible contextual user message."""

    model_visible = envelope.get("model_visible") if isinstance(envelope.get("model_visible"), dict) else {}
    parts = model_visible.get("content_parts") if isinstance(model_visible, dict) else None
    if not isinstance(parts, list) or not parts:
        return []
    if not any(isinstance(part, dict) and part.get("type") == "text" for part in parts):
        parts = [
            {"type": "text", "text": "The previous tool returned visual evidence for the current request."},
            *parts,
        ]
    return [{
        "role": "user",
        "content": parts,
        "_tool_image_context": True,
        "_tool_call_id": tool_call_id,
        "_tool_name": envelope.get("tool"),
    }]


def tool_done_event(
    tool_name: str,
    round_number: int,
    envelope: dict[str, Any],
    agent: str | None = None,
) -> dict[str, Any]:
    ui = envelope.get("ui") if isinstance(envelope.get("ui"), dict) else {}
    return {
        "type": "tool_done",
        "tool": tool_name,
        "round": round_number,
        "result": ui.get("result"),
        "agent": agent,
        "tool_output": {
            "version": envelope.get("version"),
            "success": envelope.get("success"),
            "outcome": envelope.get("outcome"),
            "handler_ok": envelope.get("handler_ok"),
            "summary": ui.get("summary"),
            "compacted": bool(ui.get("compacted")),
            "artifact_ref": ui.get("artifact_ref"),
            "raw_result_tokens": ui.get("raw_result_tokens"),
            "model_visible_tokens": ui.get("model_visible_tokens"),
        },
    }


def tool_trace_fields(envelope: dict[str, Any]) -> dict[str, Any]:
    trace = envelope.get("trace")
    return dict(trace) if isinstance(trace, dict) else {"tool_output_version": TOOL_OUTPUT_VERSION}


def _output_policy(tool_name: str, result: Any) -> ToolOutputPolicy:
    from app.mcp_tools.registry import registry

    resolved = tool_name
    if isinstance(result, dict):
        deferred = str(result.get("_deferred_tool") or "").strip()
        if deferred:
            resolved = deferred
    spec = registry.get(resolved) or registry.get(tool_name)
    return spec.output_policy if spec is not None else JSON_OUTPUT_POLICY


def _result_keys(result: Any) -> list[str]:
    if isinstance(result, dict):
        return [str(key) for key in list(result.keys())[:24]]
    if isinstance(result, list):
        return ["list", f"items:{len(result)}"]
    return [type(result).__name__]
