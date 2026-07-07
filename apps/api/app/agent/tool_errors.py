"""Normalize tool-returned errors before they go back into the Agent Loop."""
from __future__ import annotations

from typing import Any


def normalize_tool_result(result: Any, *, tool_name: str = "tool") -> Any:
    """Add a stable error envelope to dict tool results.

    Tools can still return their local fields. This helper only fills missing
    contract fields when the result already signals an error through `error` or
    `ok: false`.
    """
    if not isinstance(result, dict):
        return result
    if result.get("requires_user_confirm") and not result.get("error"):
        return result
    has_error = bool(result.get("error")) or result.get("ok") is False
    if not has_error:
        return result

    normalized = dict(result)
    normalized["ok"] = False
    normalized["error"] = str(normalized.get("error") or "Tool returned ok=false")
    normalized["error_kind"] = str(normalized.get("error_kind") or _default_error_kind(result))
    normalized.setdefault("tool", tool_name)
    normalized.setdefault("hint", _default_hint(normalized["error_kind"]))
    normalized.setdefault("suggested_next", _default_suggested_next(normalized["error_kind"]))
    normalized.setdefault("model_feedback", _model_feedback(normalized))
    return normalized


def _default_error_kind(result: dict[str, Any]) -> str:
    if result.get("ok") is False and not result.get("error_kind"):
        return "tool_failed"
    return "tool_error"


def _default_hint(error_kind: str) -> str:
    kind = str(error_kind or "")
    if kind == "subagent_blocked":
        return "子 Agent 已返回 blocked 终态。向用户说明失败原因、已尝试步骤和可选下一步。"
    if kind in {"missing_field", "missing_id", "missing_node", "missing_patch", "missing_prompt", "missing_video_node_for_video_request", "bad_deferred_tool_arguments", "invalid_field"}:
        return "检查工具参数和必填字段，必要时先读取项目或节点状态后再重试。"
    if kind in {"dependency_missing", "guide_not_loaded", "missing_prompt_source", "missing_prompt_template", "missing_template_selection_reason", "implicit_video_production_path"}:
        return "先补齐依赖或读取对应指南，再回到原工具调用；不要新建替代节点。"
    if kind in {"permission_denied", "plan_pending_approval", "plan_required_before_action"}:
        return "遵守权限边界；需要用户确认、计划批准或补充信息时先停止执行。"
    if kind in {"project_missing", "node_missing", "not_found", "node_not_found", "parent_not_found", "task_not_found", "reference_not_found"}:
        return "先读取项目状态和可用候选节点编号，标题、shot_id、segment_id 或旧编号需要通过 node.list/node.get 转成当前节点编号。"
    if kind in {"duplicate_node_id", "unknown_fields", "aspect_ratio_conflict", "unsupported_video_aspect_ratio"}:
        return "按错误里的 allowed/expected/available 字段修正参数后再试；保持用户已确认的时长、画幅和制作路径。"
    return "把错误作为下一步依据：修正参数、补依赖、询问用户，或停止重复调用。"


def _default_suggested_next(error_kind: str) -> str:
    kind = str(error_kind or "")
    if kind == "subagent_blocked":
        return "report_blocked_to_user"
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
    if kind in {"dependency_missing", "guide_not_loaded", "missing_prompt_source", "missing_prompt_template", "missing_template_selection_reason", "implicit_video_production_path"}:
        return "satisfy_dependency"
    if kind in {"permission_denied", "plan_pending_approval", "plan_required_before_action"}:
        return "ask_or_wait_for_user"
    if kind in {"project_missing", "node_missing", "not_found", "node_not_found", "parent_not_found", "task_not_found", "reference_not_found"}:
        return "read_state"
    return "model_decides"


def _diagnostic_evidence(result: dict[str, Any]) -> dict[str, Any]:
    evidence_keys = (
        "node_id",
        "parent_id",
        "task_id",
        "plan_id",
        "title",
        "status",
        "expected_aspect_ratio",
        "conflicting_value",
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
        "agent",
        "committed",
        "candidate_ref",
        "committed_ref",
        "steps_used",
    )
    evidence: dict[str, Any] = {}
    for key in evidence_keys:
        value = result.get(key)
        if value not in (None, "", [], {}):
            evidence[key] = value
    return evidence


def _model_feedback(result: dict[str, Any]) -> dict[str, Any]:
    kind = str(result.get("error_kind") or "tool_error")
    hint = str(result.get("hint") or _default_hint(kind))
    suggested_next = str(result.get("suggested_next") or _default_suggested_next(kind))
    error = str(result.get("error") or "Tool returned an error")
    return {
        "tool": result.get("tool") or "tool",
        "error_kind": kind,
        "what_went_wrong": error,
        "how_to_fix": hint,
        "suggested_next": suggested_next,
        "retry_policy": "不要用完全相同参数重复调用；先按 how_to_fix 修正参数、补状态/依赖，或停止询问用户。",
        "evidence": _diagnostic_evidence(result),
    }
