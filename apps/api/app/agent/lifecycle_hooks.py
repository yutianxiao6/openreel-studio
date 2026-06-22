"""Lifecycle hook helpers for the agent loop.

The orchestrator still owns streaming and execution, while this module keeps
hook decisions deterministic and easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.agent_trace import result_error_kind
from app.agent.permission_policy import ToolPermissionContext, decide_tool_permission


PERMISSION_DENIAL_STOP_THRESHOLD = 3

REPEATED_PERMISSION_DENIAL_MESSAGE = (
    "\n\n我连续多次尝试的下一步都被同一条执行策略拒绝，"
    "本轮已停止，避免继续重复无效调用。请调整方案或明确授权后再继续。"
)
EXECUTION_CHECKLIST_MARKER = "<execution-checklist>"
RUNTIME_CONTEXT_MARKER = "<runtime-context>"


@dataclass(frozen=True)
class PermissionDenialState:
    key: tuple[str, str] | None = None
    count: int = 0


@dataclass(frozen=True)
class PreToolUseHookResult:
    allowed: bool
    result: dict[str, Any] | None = None
    denial_state: PermissionDenialState = field(default_factory=PermissionDenialState)
    should_stop: bool = False
    stop_message: str = ""
    error_kind: str = ""


@dataclass(frozen=True)
class BeforeModelCallHookResult:
    messages: list[dict[str, Any]]
    checklist_reminder_added: bool = False
    removed_checklist_reminders: int = 0
    runtime_context_added: bool = False
    removed_runtime_contexts: int = 0


@dataclass(frozen=True)
class StopHookResult:
    should_run_audit: bool = False
    audit_message: str = ""
    audit_triggered: bool = False
    pending_steps: int = 0
    failed_steps: int = 0


@dataclass(frozen=True)
class PostToolUseChecklistResult:
    should_update: bool = False
    matched_index: int | None = None
    status: str = ""
    actual_node_id: Any | None = None


@dataclass(frozen=True)
class BeforeTurnHookResult:
    state_patch: dict[str, Any] = field(default_factory=dict)


def _is_execution_checklist_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and EXECUTION_CHECKLIST_MARKER in message["content"]
    )


def _is_runtime_context_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and RUNTIME_CONTEXT_MARKER in message["content"]
    )


def _context_insertion_index(messages: list[dict[str, Any]]) -> int:
    """Append contextual reminders so earlier prompt tokens stay cacheable.

    The reminders are per-call dynamic state. Inserting them before the latest
    user message makes every small state change invalidate the prompt prefix
    before the user's turn. Keeping them at the tail preserves the stable
    system/history/current-user prefix and confines volatility to the end.
    """
    return len(messages)


def run_before_turn(state: dict[str, Any]) -> BeforeTurnHookResult:
    if state.get("guide_loaded"):
        return BeforeTurnHookResult(state_patch={"guide_loaded": {}})
    return BeforeTurnHookResult()


def run_before_model_call(
    messages: list[dict[str, Any]],
    checklist_reminder: str,
    runtime_context: str = "",
) -> BeforeModelCallHookResult:
    cleaned_messages: list[dict[str, Any]] = []
    removed_checklist_count = 0
    removed_runtime_count = 0
    for message in messages:
        if _is_execution_checklist_message(message):
            removed_checklist_count += 1
            continue
        if _is_runtime_context_message(message):
            removed_runtime_count += 1
            continue
        cleaned_messages.append(message)

    context_messages: list[dict[str, Any]] = []
    if checklist_reminder:
        context_messages.append({"role": "user", "content": checklist_reminder})
    if runtime_context:
        context_messages.append({
            "role": "user",
            "content": f"{RUNTIME_CONTEXT_MARKER}\n{runtime_context}\n</runtime-context>",
        })
    if context_messages:
        insertion_index = _context_insertion_index(cleaned_messages)
        cleaned_messages[insertion_index:insertion_index] = context_messages

    return BeforeModelCallHookResult(
        messages=cleaned_messages,
        checklist_reminder_added=bool(checklist_reminder),
        removed_checklist_reminders=removed_checklist_count,
        runtime_context_added=bool(runtime_context),
        removed_runtime_contexts=removed_runtime_count,
    )


def _build_completion_audit_message(
    checklist: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> str:
    extras: list[str] = []
    if checklist:
        extras.append(
            f"原计划清单 {len(checklist)} 步,未完成 {len(pending)} 步,失败 {len(failed)} 步。"
        )
        if pending:
            extras.append(f"未完成项:{[step.get('title') for step in pending]}")
        if failed:
            extras.append(f"失败项:{[step.get('title') for step in failed]}")

    return (
        "<system-reminder>\n收尾自检(必走):\n"
        + ("\n".join(extras) + "\n" if extras else "")
        + "1) 核对 pending 项是否确实还需执行；核对 failed 项是否可原地修复，不能修复就报告阻塞原因，不要无条件续跑旧失败。\n"
        + "2) 用 node.list 看一遍每个应有节点真存在且 status=completed。\n"
        + "3) image 节点 output 里有可读 url 才算真出图；video 节点 output 里有可读 url 才算真出片。\n"
        + "4) failed 或 output_json 为空的节点 → 优先在原节点重试；无法修复则向用户报告，未经用户明确要求不得删除。\n"
        + "5) 顺序按蓝图树、references 和 depends_on 核对。\n"
        + "审核完报告真实核对结果和未完成项。\n"
        + "</system-reminder>"
    )


def run_stop_after_text_response(
    *,
    step_index: int,
    checklist: list[dict[str, Any]],
    audit_triggered: bool,
    tool_errors: list[dict[str, Any]] | None = None,
) -> StopHookResult:
    """Decide whether to inject a completion audit after the model's text response.

    When the model produces text (no tool calls), it signals "I'm done with this
    turn."  The audit injects a system message to check for forgotten pending
    checklist items before the loop stops.

    However, if the model stopped because it hit tool errors (permission denials,
    checklist violations, opaque server errors), forcing another iteration is
    harmful — it turns a 3-iteration recovery into a 13-iteration death loop.
    The model already decided to stop for good reason; respect that.
    """
    # Only audit when the model has done work (step_index >= 1) AND there are
    # pending tasks.  step_index == 0 means the model replied without any tools —
    # a valid choice for conversation or when no action is needed.
    has_pending = any(
        step.get("status") in (None, "pending", "in_progress")
        for step in checklist
    ) if checklist else False
    should_audit = (step_index >= 1) and has_pending
    if audit_triggered or not should_audit:
        return StopHookResult(audit_triggered=audit_triggered)

    # When tool errors blocked the model, trust its decision to stop.
    if tool_errors:
        _error_kinds = {
            str(err.get("error_kind") or "")
            for err in tool_errors
        }
        _wall_kinds = {
            "checklist_violation",
            "checklist_failed_step_requires_repair",
            "server_error",
            "permission_denied",
            "plan_pending_approval",
            "plan_required_before_action",
        }
        if _error_kinds & _wall_kinds:
            return StopHookResult(audit_triggered=False)

    pending = [
        step
        for step in checklist
        if step.get("status") in (None, "pending", "in_progress")
    ]
    failed = [step for step in checklist if step.get("status") == "failed"]

    return StopHookResult(
        should_run_audit=True,
        audit_message=_build_completion_audit_message(checklist, pending, failed),
        audit_triggered=True,
        pending_steps=len(pending),
        failed_steps=len(failed),
    )


def run_post_tool_use_checklist(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
    node_id: Any | None,
    checklist: list[dict[str, Any]],
) -> PostToolUseChecklistResult:
    if not checklist:
        return PostToolUseChecklistResult()
    if isinstance(result, dict) and result.get("requires_user_confirm") and not result.get("error"):
        return PostToolUseChecklistResult()

    is_ok = not (
        isinstance(result, dict)
        and (result.get("error") or result.get("ok") is False)
    )
    actual_node_id = node_id
    if actual_node_id is None and isinstance(result, dict) and result.get("id"):
        actual_node_id = result.get("id")
    if actual_node_id is None and isinstance(result, dict) and result.get("node_id"):
        actual_node_id = result.get("node_id")
    if (
        actual_node_id is None
        and isinstance(result, dict)
        and isinstance(result.get("result"), dict)
        and result["result"].get("node_id")
    ):
        actual_node_id = result["result"].get("node_id")

    expected_type = None
    if tool_name == "node.create" and isinstance(tool_args, dict):
        expected_type = tool_args.get("type")

    actual_action = ""
    if isinstance(tool_args, dict) and tool_args.get("action") is not None:
        actual_action = str(tool_args.get("action"))

    def _expected_node_for_step(step: dict[str, Any]) -> Any | None:
        if step.get("expected_node_id"):
            return step.get("expected_node_id")
        if step.get("actual_node_id"):
            return step.get("actual_node_id")
        ref_step = step.get("expected_node_ref_step")
        if ref_step is not None:
            for prior in checklist:
                if prior.get("step") == ref_step and prior.get("actual_node_id"):
                    return prior.get("actual_node_id")
        return None

    def _action_matches(step: dict[str, Any]) -> bool:
        expected_action = step.get("expected_action")
        if expected_action is None or expected_action == "":
            return True
        if expected_action == "__default__":
            return actual_action == ""
        return str(expected_action) == actual_action

    def _has_node_constraint(step: dict[str, Any]) -> bool:
        return bool(
            step.get("expected_node_id")
            or step.get("actual_node_id")
            or step.get("expected_node_ref_step") is not None
        )

    def _node_matches(step: dict[str, Any]) -> bool:
        expected_node = _expected_node_for_step(step)
        if expected_node and actual_node_id:
            return expected_node == actual_node_id
        if _has_node_constraint(step):
            return False
        return True

    if actual_node_id is not None:
        for index, step in enumerate(checklist):
            if step.get("status") == "completed":
                continue
            if step.get("tool") != tool_name:
                continue
            if expected_type and step.get("expected_node_type") and step.get("expected_node_type") != expected_type:
                continue
            if not _action_matches(step):
                continue
            if _node_matches(step):
                return PostToolUseChecklistResult(
                    should_update=True,
                    matched_index=index,
                    status="completed" if is_ok else "failed",
                    actual_node_id=actual_node_id,
                )

        # If this result clearly belongs to a different checklist node, never
        # fall through to "first pending step with same tool"; that is what
        # corrupts adjacent node.run steps.
        for step in checklist:
            if step.get("actual_node_id") == actual_node_id or _expected_node_for_step(step) == actual_node_id:
                return PostToolUseChecklistResult(
                    status="completed" if is_ok else "failed",
                    actual_node_id=actual_node_id,
                )

    for index, step in enumerate(checklist):
        if step.get("status") == "completed":
            continue
        if step.get("tool") == tool_name:
            if not _action_matches(step):
                continue
            if actual_node_id is not None and not _node_matches(step):
                continue
            if expected_type and step.get("expected_node_type"):
                if step.get("expected_node_type") == expected_type:
                    return PostToolUseChecklistResult(
                        should_update=True,
                        matched_index=index,
                        status="completed" if is_ok else "failed",
                        actual_node_id=actual_node_id,
                    )
                continue
            return PostToolUseChecklistResult(
                should_update=True,
                matched_index=index,
                status="completed" if is_ok else "failed",
                actual_node_id=actual_node_id,
            )
        if expected_type and step.get("expected_node_type") == expected_type:
            return PostToolUseChecklistResult(
                should_update=True,
                matched_index=index,
                status="completed" if is_ok else "failed",
                actual_node_id=actual_node_id,
            )

    return PostToolUseChecklistResult(
        status="completed" if is_ok else "failed",
        actual_node_id=actual_node_id,
    )


def next_permission_denial_state(
    current: PermissionDenialState,
    tool_name: str,
    result: dict[str, Any],
) -> tuple[PermissionDenialState, bool]:
    key = (tool_name, result_error_kind(result))
    count = current.count + 1 if key == current.key else 1
    state = PermissionDenialState(key=key, count=count)
    return state, count >= PERMISSION_DENIAL_STOP_THRESHOLD


def run_pre_tool_use(
    ctx: ToolPermissionContext,
    denial_state: PermissionDenialState,
) -> PreToolUseHookResult:
    permission = decide_tool_permission(ctx)
    if permission.allowed:
        return PreToolUseHookResult(allowed=True)

    result = permission.result or {
        "ok": False,
        "error": "工具调用被权限策略拒绝",
        "error_kind": "permission_denied",
    }
    next_state, should_stop = next_permission_denial_state(
        denial_state,
        ctx.tool_name,
        result,
    )
    if should_stop:
        result = {
            **result,
            "denial_count": next_state.count,
            "stop_reason": "repeated_permission_denial",
        }

    return PreToolUseHookResult(
        allowed=False,
        result=result,
        denial_state=next_state,
        should_stop=should_stop,
        stop_message=REPEATED_PERMISSION_DENIAL_MESSAGE if should_stop else "",
        error_kind=result_error_kind(result),
    )
