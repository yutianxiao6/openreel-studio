"""The only boundary that turns tool runtime data into model-visible output."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from app.agent.vision_context import redact_image_data_urls

from .artifact_store import ToolResultArtifact, save_tool_result
from .policy import ToolOutputPolicy, estimate_text_tokens
from .truncate import bounded_json_value, truncate_text_middle
from .types import ToolContentPart, ToolOutput, coerce_tool_output


MODEL_OBSERVATION_VERSION = "tool_observation_v2"
IMAGE_TOKEN_ESTIMATE = 765
TRACE_PREVIEW_TOKENS = 512
UI_INLINE_MAX_BYTES = 12 * 1024
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "client_secret",
}


@dataclass(frozen=True)
class CompiledToolOutput:
    observation: dict[str, Any]
    observation_text: str
    content_parts: tuple[dict[str, Any], ...]
    raw_value: Any
    artifact: ToolResultArtifact | None
    compacted: bool
    original_tokens: int
    model_tokens: int
    handler_ok: bool
    success: bool
    outcome: str
    next_action: str
    trace_summary: Any
    ui_result: Any
    contains_external_context: bool


def compile_tool_output(
    output: ToolOutput | Any,
    *,
    project_id: str,
    run_id: str,
    iteration: int,
    tool_name: str,
    policy: ToolOutputPolicy,
    requested_tokens: int | None = None,
) -> CompiledToolOutput:
    typed = coerce_tool_output(output)
    raw_value = _sanitize_raw_value(typed.value)
    raw_text = json.dumps(raw_value, ensure_ascii=False, default=str, separators=(",", ":"))
    original_tokens = estimate_text_tokens(raw_text)
    artifact = None
    if len(raw_text.encode("utf-8")) > policy.artifact_threshold_bytes:
        artifact = save_tool_result(
            raw_value,
            project_id=project_id,
            run_id=run_id,
            iteration=iteration,
            tool_name=tool_name,
        )

    handler_ok = result_handler_ok(raw_value)
    outcome = tool_outcome(raw_value, handler_ok=handler_ok)
    success = outcome == "success"
    next_action = next_action_for_result(raw_value, outcome=outcome)
    max_tokens = policy.effective_tokens(requested_tokens)
    provider_parts, parts_tokens = _bounded_content_parts(
        typed.content_parts,
        max_tokens=max_tokens,
        max_media_items=policy.max_media_items,
    )
    observation_budget = max(64, max_tokens - parts_tokens)
    observation_base: dict[str, Any] = {
        "tool_observation_version": MODEL_OBSERVATION_VERSION,
        "tool": tool_name,
        "success": success,
        "outcome": outcome,
        "handler_ok": handler_ok,
        "next_action": next_action,
    }
    if typed.contains_external_context:
        observation_base["contains_external_context"] = True
    if artifact is not None:
        observation_base["artifact_ref"] = artifact.ref
    wrapper_tokens = estimate_text_tokens(
        json.dumps({**observation_base, "result": {}}, ensure_ascii=False, separators=(",", ":"))
    )
    result_budget = max(16, observation_budget - wrapper_tokens - 8)
    projected = bounded_json_value(raw_value, policy=policy, budget_tokens=result_budget)
    observation = {**observation_base, "result": projected}
    observation_text = json.dumps(observation, ensure_ascii=False, default=str, separators=(",", ":"))
    if estimate_text_tokens(observation_text) > observation_budget:
        observation = {
            **observation_base,
            "result": {
                "truncated": True,
                "preview": truncate_text_middle(raw_text, max(16, result_budget // 2)),
            },
        }
        observation_text = json.dumps(observation, ensure_ascii=False, default=str, separators=(",", ":"))
    if estimate_text_tokens(observation_text) > observation_budget:
        observation.pop("artifact_ref", None)
        observation["result"] = {"truncated": True}
        observation_text = json.dumps(observation, ensure_ascii=False, separators=(",", ":"))

    model_tokens = estimate_text_tokens(observation_text) + parts_tokens
    compacted = projected != raw_value or artifact is not None
    trace_summary = bounded_json_value(
        raw_value,
        policy=policy,
        budget_tokens=min(TRACE_PREVIEW_TOKENS, max_tokens),
    )
    ui_result = _ui_projection(
        raw_value,
        policy=policy,
        artifact=artifact,
    )
    return CompiledToolOutput(
        observation=observation,
        observation_text=observation_text,
        content_parts=provider_parts,
        raw_value=raw_value,
        artifact=artifact,
        compacted=compacted,
        original_tokens=original_tokens,
        model_tokens=model_tokens,
        handler_ok=handler_ok,
        success=success,
        outcome=outcome,
        next_action=next_action,
        trace_summary=trace_summary,
        ui_result=ui_result,
        contains_external_context=typed.contains_external_context,
    )


def result_handler_ok(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("requires_user_confirm") and not result.get("error"):
            return True
        return not (result.get("error") or result.get("ok") is False)
    return True


def tool_outcome(result: Any, *, handler_ok: bool) -> str:
    if not handler_ok:
        return "recoverable_error"
    if not isinstance(result, dict):
        return "success"
    status = str(result.get("status") or "").strip().lower()
    if result.get("requires_user_confirm"):
        return "requires_confirmation"
    if result.get("awaiting_user") or result.get("requires_user_input") or status in {
        "awaiting_user",
        "awaiting_input",
        "confirm_required",
        "confirmation_required",
        "input_required",
        "pending_confirmation",
        "pending_user_input",
        "user_input_required",
    }:
        return "awaiting_user"
    if (
        result.get("needs_revision")
        or status in {
            "agent_review_required",
            "agent_review_revise_required",
            "review_required",
            "revision_required",
            "tool_action_required",
        }
        or status.endswith("_review_required")
        or status.endswith("_revise_required")
    ):
        return "needs_action"
    if result.get("finalized") is False and (
        result.get("required_action") or result.get("suggested_tool") or status.endswith("_required")
    ):
        return "needs_action"
    return "success"


def next_action_for_result(result: Any, *, outcome: str) -> str:
    if isinstance(result, dict):
        explicit = result.get("suggested_next")
        if explicit not in (None, "", [], {}):
            return str(explicit)
        status = str(result.get("status") or "").strip().lower()
        if status == "agent_review_required" or (
            str(result.get("suggested_tool") or "") == "agent.review" and outcome == "needs_action"
        ):
            return "call_agent_review"
        if status == "agent_review_revise_required" or result.get("needs_revision"):
            return "revise_then_review"
        if result.get("required_action"):
            return str(result["required_action"])
        if outcome == "recoverable_error":
            return _next_for_error_kind(str(result.get("error_kind") or "tool_error"))
    if outcome in {"awaiting_user", "requires_confirmation"}:
        return "wait_for_user"
    if outcome == "needs_action":
        return "satisfy_required_action"
    return "continue" if outcome == "success" else "model_decides"


def _next_for_error_kind(error_kind: str) -> str:
    if error_kind == "subagent_blocked":
        return "report_blocked_to_user"
    if error_kind in {
        "missing_field", "missing_id", "missing_node", "missing_patch", "missing_prompt",
        "missing_video_node_for_video_request", "bad_deferred_tool_arguments", "invalid_field",
        "duplicate_node_id", "unknown_fields", "aspect_ratio_conflict", "unsupported_video_aspect_ratio",
    }:
        return "repair_arguments"
    if error_kind in {
        "dependency_missing", "missing_prompt_source", "missing_prompt_template",
        "missing_template_selection_reason", "implicit_video_production_path",
    }:
        return "satisfy_dependency"
    if error_kind in {"permission_denied", "plan_pending_approval", "plan_required_before_action"}:
        return "ask_or_wait_for_user"
    if error_kind in {
        "project_missing", "node_missing", "not_found", "node_not_found", "parent_not_found",
        "task_not_found", "reference_not_found",
    }:
        return "read_state"
    return "model_decides"


def _sanitize_raw_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in {"_subagent_usage", "_subagent_trace"}:
                continue
            if key_text in {"display_id", "project_id"} or key_text.startswith("_canvas"):
                continue
            normalized_key = key_text.strip().lower().replace("-", "_")
            if (
                normalized_key in _SENSITIVE_KEYS
                or normalized_key.endswith("_api_key")
                or normalized_key.endswith("_password")
                or normalized_key.endswith("_secret")
            ):
                sanitized[key_text] = "[REDACTED]"
                continue
            sanitized[key_text] = _sanitize_raw_value(item)
        return redact_image_data_urls(sanitized)
    if isinstance(value, list):
        return [_sanitize_raw_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_raw_value(item) for item in value]
    return value


def _bounded_content_parts(
    parts: Iterable[ToolContentPart],
    *,
    max_tokens: int,
    max_media_items: int,
) -> tuple[tuple[dict[str, Any], ...], int]:
    text_parts: list[ToolContentPart] = []
    media_parts: list[ToolContentPart] = []
    for part in parts:
        if part.type == "text" and part.text:
            text_parts.append(part)
        elif part.type in {"image_url", "audio_url"} and part.url:
            media_parts.append(part)
    if max_media_items <= 0:
        media_parts = []
    else:
        media_parts = media_parts[:max_media_items]
    image_count = sum(1 for part in media_parts if part.type == "image_url")
    media_tokens = image_count * IMAGE_TOKEN_ESTIMATE
    if media_tokens >= max_tokens:
        allowed_images = max(0, (max_tokens - 64) // IMAGE_TOKEN_ESTIMATE)
        kept: list[ToolContentPart] = []
        images = 0
        for part in media_parts:
            if part.type == "image_url":
                if images >= allowed_images:
                    continue
                images += 1
            kept.append(part)
        media_parts = kept
        image_count = images
        media_tokens = image_count * IMAGE_TOKEN_ESTIMATE
    text_budget = max(0, min(1_000, max_tokens - media_tokens - 32))
    combined_text = "\n".join(part.text for part in text_parts if part.text)
    provider_parts: list[dict[str, Any]] = []
    text_tokens = 0
    if combined_text and text_budget > 0:
        bounded_text = truncate_text_middle(combined_text, text_budget)
        provider_parts.append(ToolContentPart.text_part(bounded_text).as_provider_part())
        text_tokens = estimate_text_tokens(bounded_text)
    provider_parts.extend(part.as_provider_part() for part in media_parts)
    return tuple(provider_parts), text_tokens + media_tokens


def _ui_projection(
    raw_value: Any,
    *,
    policy: ToolOutputPolicy,
    artifact: ToolResultArtifact | None,
) -> Any:
    rendered = json.dumps(raw_value, ensure_ascii=False, default=str)
    if len(rendered.encode("utf-8")) <= UI_INLINE_MAX_BYTES:
        return raw_value
    summary = bounded_json_value(raw_value, policy=policy, budget_tokens=1_000)
    payload: dict[str, Any] = {
        "tool_result_compacted": True,
        "summary": summary,
        "original_bytes": len(rendered.encode("utf-8")),
    }
    if artifact is not None:
        payload["artifact_ref"] = artifact.ref
    return payload
