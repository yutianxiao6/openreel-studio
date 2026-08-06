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
SKILLS_CONTEXT_MARKER = "<skills_instructions>"
SKILL_INSTRUCTIONS_MARKER = "<skill>"
SKILL_WARNING_MARKER = "<skill-warning>"
HISTORY_INSTRUCTIONS_MARKER = "<agent-instructions>"


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
    skills_context_added: bool = False
    removed_skills_contexts: int = 0
    skill_instructions_added: int = 0
    removed_skill_instructions: int = 0
    skill_warnings_added: int = 0
    removed_skill_warnings: int = 0


@dataclass(frozen=True)
class StopHookResult:
    should_run_audit: bool = False
    audit_message: str = ""
    audit_triggered: bool = False
    pending_steps: int = 0
    failed_steps: int = 0


def _is_execution_checklist_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") in {"developer", "user"}
        and isinstance(message.get("content"), str)
        and EXECUTION_CHECKLIST_MARKER in message["content"]
    )


def _is_runtime_context_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") in {"developer", "user"}
        and isinstance(message.get("content"), str)
        and RUNTIME_CONTEXT_MARKER in message["content"]
    )


def _is_skill_instruction_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and message["content"].startswith(f"{SKILL_INSTRUCTIONS_MARKER}\n<name>")
        and message["content"].rstrip().endswith("</skill>")
    )


def _is_skills_context_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "developer"
        and isinstance(message.get("content"), str)
        and message["content"].startswith(SKILLS_CONTEXT_MARKER)
        and message["content"].rstrip().endswith("</skills_instructions>")
    )


def _is_skill_warning_message(message: dict[str, Any]) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") in {"developer", "user"}
        and isinstance(message.get("content"), str)
        and message["content"].startswith(SKILL_WARNING_MARKER)
        and message["content"].rstrip().endswith("</skill-warning>")
    )


def _is_managed_context_message(message: dict[str, Any]) -> bool:
    return bool(
        _is_execution_checklist_message(message)
        or _is_runtime_context_message(message)
        or _is_skills_context_message(message)
        or _is_skill_instruction_message(message)
        or _is_skill_warning_message(message)
    )


def _context_insertion_index(messages: list[dict[str, Any]]) -> int:
    """Append contextual reminders so earlier prompt tokens stay cacheable.

    The reminders are per-call dynamic state. Inserting them before the latest
    user message makes every small state change invalidate the prompt prefix
    before the user's turn. Keeping them at the tail preserves the stable
    system/history/current-user prefix and confines volatility to the end.
    """
    return len(messages)


def prepend_history_instructions(
    messages: list[dict[str, Any]],
    instructions: str,
) -> list[dict[str, Any]]:
    """Install stable turn instructions as developer context, never fake chat."""

    cleaned = [
        message
        for message in messages
        if not (
            isinstance(message, dict)
            and message.get("role") == "developer"
            and isinstance(message.get("content"), str)
            and message["content"].startswith(f"{HISTORY_INSTRUCTIONS_MARKER}\n")
        )
    ]
    text = str(instructions or "").strip()
    if not text:
        return cleaned
    return [
        {
            "role": "developer",
            "content": f"{HISTORY_INSTRUCTIONS_MARKER}\n{text}\n</agent-instructions>",
        },
        *cleaned,
    ]


def run_before_model_call(
    messages: list[dict[str, Any]],
    checklist_reminder: str,
    runtime_context: str = "",
    skills_context: str = "",
    skill_instructions: tuple[str, ...] | list[str] = (),
    skill_warnings: tuple[str, ...] | list[str] = (),
) -> BeforeModelCallHookResult:
    original_messages = list(messages)
    existing_context_messages = [
        message for message in original_messages if _is_managed_context_message(message)
    ]
    cleaned_messages: list[dict[str, Any]] = []
    removed_checklist_count = 0
    removed_runtime_count = 0
    removed_skills_context_count = 0
    removed_skill_count = 0
    removed_skill_warning_count = 0
    for message in messages:
        if _is_execution_checklist_message(message):
            removed_checklist_count += 1
            continue
        if _is_runtime_context_message(message):
            removed_runtime_count += 1
            continue
        if _is_skills_context_message(message):
            removed_skills_context_count += 1
            continue
        if _is_skill_instruction_message(message):
            removed_skill_count += 1
            continue
        if _is_skill_warning_message(message):
            removed_skill_warning_count += 1
            continue
        cleaned_messages.append(message)

    context_messages: list[dict[str, Any]] = []
    if checklist_reminder:
        context_messages.append({"role": "developer", "content": checklist_reminder})
    if runtime_context:
        context_messages.append(
            {
                "role": "developer",
                "content": f"{RUNTIME_CONTEXT_MARKER}\n{runtime_context}\n</runtime-context>",
            }
        )
    if skills_context:
        context_messages.append({"role": "developer", "content": skills_context})
    for instruction in skill_instructions:
        if str(instruction or "").strip():
            context_messages.append({"role": "user", "content": str(instruction)})
    if skill_warnings:
        warning_text = "\n".join(f"- {warning}" for warning in skill_warnings if warning)
        if warning_text:
            context_messages.append(
                {
                    "role": "developer",
                    "content": f"{SKILL_WARNING_MARKER}\n{warning_text}\n</skill-warning>",
                }
            )
    # Preserve byte-for-byte identical context blocks at their original
    # positions. This keeps the next Responses request as an append-only
    # extension, allowing a turn-scoped WebSocket to send only new tool items.
    if existing_context_messages == context_messages:
        return BeforeModelCallHookResult(
            messages=original_messages,
            checklist_reminder_added=bool(checklist_reminder),
            runtime_context_added=bool(runtime_context),
            skills_context_added=bool(skills_context),
            skill_instructions_added=sum(
                1 for instruction in skill_instructions if str(instruction or "").strip()
            ),
            skill_warnings_added=sum(1 for warning in skill_warnings if warning),
        )
    if context_messages:
        insertion_index = _context_insertion_index(cleaned_messages)
        cleaned_messages[insertion_index:insertion_index] = context_messages

    return BeforeModelCallHookResult(
        messages=cleaned_messages,
        checklist_reminder_added=bool(checklist_reminder),
        removed_checklist_reminders=removed_checklist_count,
        runtime_context_added=bool(runtime_context),
        removed_runtime_contexts=removed_runtime_count,
        skills_context_added=bool(skills_context),
        removed_skills_contexts=removed_skills_context_count,
        skill_instructions_added=sum(
            1 for instruction in skill_instructions if str(instruction or "").strip()
        ),
        removed_skill_instructions=removed_skill_count,
        skill_warnings_added=sum(1 for warning in skill_warnings if warning),
        removed_skill_warnings=removed_skill_warning_count,
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
        + "3) image 节点 output 里有可读 url 才算真出图；video 节点 output 里有可读 url 才算真出片；audio 节点 output 里有可读 url 才算真出音频。\n"
        + "4) failed 或 output_json 为空的节点 → 优先在原节点重试；无法修复则向用户报告，未经用户明确要求不得删除。\n"
        + "5) 顺序按可见节点和 references 核对。\n"
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
    has_pending = (
        any(step.get("status") in (None, "pending", "in_progress") for step in checklist)
        if checklist
        else False
    )
    should_audit = (step_index >= 1) and has_pending
    if audit_triggered or not should_audit:
        return StopHookResult(audit_triggered=audit_triggered)

    # When tool errors blocked the model, trust its decision to stop.
    if tool_errors:
        _error_kinds = {str(err.get("error_kind") or "") for err in tool_errors}
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

    pending = [step for step in checklist if step.get("status") in (None, "pending", "in_progress")]
    failed = [step for step in checklist if step.get("status") == "failed"]

    return StopHookResult(
        should_run_audit=True,
        audit_message=_build_completion_audit_message(checklist, pending, failed),
        audit_triggered=True,
        pending_steps=len(pending),
        failed_steps=len(failed),
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
