"""Model-facing tool observations and correction hints."""
from __future__ import annotations

from typing import Any

MODEL_OBSERVATION_VERSION = "tool_observation_v1"

_ACTION_REQUIRED_STATUSES = {
    "agent_review_required",
    "agent_review_revise_required",
    "review_required",
    "revision_required",
    "tool_action_required",
}
_AWAITING_USER_STATUSES = {
    "awaiting_user",
    "awaiting_input",
    "confirm_required",
    "confirmation_required",
    "input_required",
    "pending_confirmation",
    "pending_user_input",
    "user_input_required",
}


def result_handler_ok(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("requires_user_confirm") and not result.get("error"):
            return True
        return not (result.get("error") or result.get("ok") is False)
    return True


def build_model_observation(result: Any, *, tool_name: str, model_payload: Any) -> dict[str, Any]:
    handler_ok = result_handler_ok(result)
    outcome = _tool_outcome(result, handler_ok=handler_ok)
    success = outcome == "success"
    next_action = _next_action(result, outcome=outcome)
    observation: dict[str, Any] = {
        "tool_observation_version": MODEL_OBSERVATION_VERSION,
        "tool": tool_name,
        "success": success,
        "outcome": outcome,
        "handler_ok": handler_ok,
        "next_action": next_action,
        "result": model_payload,
    }
    if isinstance(result, dict):
        for key in ("status", "error", "error_kind", "hint", "message"):
            value = result.get(key)
            if value not in (None, "", [], {}):
                observation[key] = value
        feedback = _model_feedback(result, tool_name=tool_name, outcome=outcome, next_action=next_action)
        if feedback:
            observation["model_feedback"] = feedback
    elif not success:
        observation["model_feedback"] = _model_feedback({}, tool_name=tool_name, outcome=outcome, next_action=next_action)
    return observation


def _tool_outcome(result: Any, *, handler_ok: bool) -> str:
    if not handler_ok:
        return "recoverable_error"
    if not isinstance(result, dict):
        return "success"
    status = str(result.get("status") or "").strip()
    status_l = status.lower()
    if result.get("requires_user_confirm"):
        return "requires_confirmation"
    if (
        result.get("awaiting_user")
        or result.get("requires_user_input")
        or status_l in _AWAITING_USER_STATUSES
    ):
        return "awaiting_user"
    if (
        result.get("needs_revision")
        or status_l in _ACTION_REQUIRED_STATUSES
        or status_l.endswith("_review_required")
        or status_l.endswith("_revise_required")
    ):
        return "needs_action"
    if result.get("finalized") is False and (
        result.get("required_action")
        or result.get("suggested_tool")
        or status_l.endswith("_required")
    ):
        return "needs_action"
    return "success"


def _next_action(result: Any, *, outcome: str) -> str | None:
    if isinstance(result, dict):
        explicit = result.get("suggested_next")
        if explicit not in (None, "", [], {}):
            return str(explicit)
        status = str(result.get("status") or "").strip().lower()
        suggested_tool = str(result.get("suggested_tool") or "").strip()
        required_action = str(result.get("required_action") or "").strip()
        if status == "agent_review_required":
            return "call_agent_review"
        if suggested_tool == "agent.review" and outcome == "needs_action":
            return "call_agent_review"
        if status == "agent_review_revise_required" or result.get("needs_revision"):
            return "revise_then_review"
        if required_action:
            return required_action
        if outcome == "recoverable_error":
            return _next_for_error_kind(str(result.get("error_kind") or "tool_error"))
    if outcome in {"awaiting_user", "requires_confirmation"}:
        return "wait_for_user"
    if outcome == "needs_action":
        return "satisfy_required_action"
    if outcome == "success":
        return "continue"
    return "model_decides"


def _next_for_error_kind(error_kind: str) -> str:
    kind = str(error_kind or "")
    if kind in {
        "missing_field",
        "missing_id",
        "missing_node",
        "missing_patch",
        "missing_prompt",
        "missing_video_node_for_video_request",
        "bad_deferred_tool_arguments",
        "invalid_field",
        "duplicate_node_id",
        "unknown_fields",
        "aspect_ratio_conflict",
        "unsupported_video_aspect_ratio",
    }:
        return "repair_arguments"
    if kind in {
        "dependency_missing",
        "guide_not_loaded",
        "missing_prompt_source",
        "missing_prompt_template",
        "missing_template_selection_reason",
        "implicit_video_production_path",
    }:
        return "satisfy_dependency"
    if kind in {
        "permission_denied",
        "plan_pending_approval",
        "plan_required_before_action",
    }:
        return "ask_or_wait_for_user"
    if kind in {
        "project_missing",
        "node_missing",
        "not_found",
        "node_not_found",
        "parent_not_found",
        "task_not_found",
        "reference_not_found",
    }:
        return "read_state"
    return "model_decides"


def _model_feedback(
    result: dict[str, Any],
    *,
    tool_name: str,
    outcome: str,
    next_action: str | None,
) -> dict[str, Any] | None:
    existing = result.get("model_feedback")
    if isinstance(existing, dict):
        return existing
    if outcome == "success":
        return None
    error_kind = str(result.get("error_kind") or outcome)
    return {
        "tool": result.get("tool") or tool_name,
        "error_kind": error_kind,
        "what_went_wrong": _what_went_wrong(result, outcome=outcome),
        "how_to_fix": _how_to_fix(result, outcome=outcome, next_action=next_action),
        "suggested_next": next_action or "model_decides",
        "retry_policy": "不要用完全相同参数重复调用；先按 how_to_fix 修正参数、补状态/依赖、调用要求的检查工具，或停止等待用户。",
        "evidence": _diagnostic_evidence(result),
    }


def _what_went_wrong(result: dict[str, Any], *, outcome: str) -> str:
    for key in ("error", "message", "status", "required_action"):
        value = result.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    if outcome == "needs_action":
        return "工具已返回，但结果还需要模型执行后续检查或修订。"
    if outcome in {"awaiting_user", "requires_confirmation"}:
        return "工具已进入等待用户输入或确认状态。"
    return "工具返回了可恢复问题。"


def _how_to_fix(result: dict[str, Any], *, outcome: str, next_action: str | None) -> str:
    hint = result.get("hint")
    if hint not in (None, "", [], {}):
        return str(hint)
    if next_action == "call_agent_review":
        return "调用只读 review 工具检查当前结果，再根据 review 的 findings 决定修订或提交。"
    if next_action == "revise_then_review":
        return "先按返回的 findings 或 required_action 修订，再重新检查。"
    if next_action == "read_state":
        return "先读取真实项目状态和可用 id，再用真实 id 重试。"
    if next_action == "wait_for_user" or outcome in {"awaiting_user", "requires_confirmation"}:
        return "停止继续工具调用，等待用户提交卡片、补充信息或确认。"
    if outcome == "recoverable_error":
        return "根据 error、error_kind 和 evidence 修正参数、补齐依赖或询问用户。"
    return "根据 required_action 或 suggested_tool 执行下一步。"


def _diagnostic_evidence(result: dict[str, Any]) -> dict[str, Any]:
    evidence_keys = (
        "node_id",
        "parent_id",
        "task_id",
        "plan_id",
        "title",
        "status",
        "current_tree_version",
        "reviewed_tree_version",
        "tree_version",
        "draft_id",
        "expected_aspect_ratio",
        "conflicting_value",
        "production_basis",
        "mismatches",
        "allowed_fields",
        "missing_fields",
        "available_node_ids",
        "available_nodes",
        "candidates",
        "supported_aspect_ratios",
        "required_action",
        "suggested_tool",
        "missing_guide_topics",
        "required_tool_calls",
        "required_tool_flow",
        "fix_example",
        "grounded_findings_count",
        "blocking_findings_count",
        "collected_facts",
    )
    evidence: dict[str, Any] = {}
    for key in evidence_keys:
        value = result.get(key)
        if value not in (None, "", [], {}):
            evidence[key] = value
    for key in ("findings", "review_findings", "blocking_findings"):
        value = result.get(key)
        if isinstance(value, list) and value:
            evidence[f"{key}_count"] = len(value)
    return evidence
