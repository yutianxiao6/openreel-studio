"""Typed contracts for chat SSE events.

The stream is still dict-compatible, but known event types are validated at the
API boundary before being serialized to SSE.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class TextDeltaEvent(ChatEvent):
    type: Literal["text_delta"]
    content: str


class AgentRoundEvent(ChatEvent):
    type: Literal["agent_round"]
    round: int
    content: str
    source: Literal["model", "action_summary"] = "model"
    tools: list[str] = Field(default_factory=list)
    tool_agents: list[str] = Field(default_factory=list)


class AgentRoundDoneEvent(ChatEvent):
    type: Literal["agent_round_done"]
    round: int


class SubagentRoundEvent(ChatEvent):
    type: Literal["subagent_round"]
    agent: str
    step: int
    content: str
    tool: str | None = None
    status: Literal["running", "completed", "failed"] = "running"
    source: Literal["model"] | None = None


class TokenUsageEvent(ChatEvent):
    type: Literal["token_usage"]
    project_id: str
    run_id: str
    round: int | None = None
    phase: str = "agent_loop"
    usage: dict[str, Any] = Field(default_factory=dict)
    run_totals: dict[str, Any] = Field(default_factory=dict)
    session_totals: dict[str, Any] = Field(default_factory=dict)
    latest_call_tokens: dict[str, Any] | None = None
    latest_call_context: dict[str, Any] | None = None
    run_cumulative_tokens: dict[str, Any] | None = None
    session_cumulative_tokens: dict[str, Any] | None = None
    run_context_peak: dict[str, Any] | None = None
    session_context_peak: dict[str, Any] | None = None


class ToolStartEvent(ChatEvent):
    type: Literal["tool_start"]
    tool: str
    round: int | None = None
    content: str | None = None
    agent: str | None = None


class ToolDoneEvent(ChatEvent):
    type: Literal["tool_done"]
    tool: str
    round: int | None = None
    result: Any = None
    tool_output: dict[str, Any] | None = None
    agent: str | None = None


class StepStartEvent(ChatEvent):
    type: Literal["step_start"]
    step_index: int
    total: int = 0
    tool: str
    title: str = ""


class StepDoneEvent(ChatEvent):
    type: Literal["step_done"]
    step_index: int
    tool: str
    status: str


class CanvasActionEvent(ChatEvent):
    type: Literal["canvas_action"]
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateEvent(ChatEvent):
    type: Literal["project_update"]
    project_id: str
    updates: dict[str, Any] = Field(default_factory=dict)


class ProjectSwitchEvent(ChatEvent):
    type: Literal["project_switch"]
    project_id: str
    title: str = ""
    refresh_page: bool = False


class ProjectResetEvent(ChatEvent):
    type: Literal["project_reset"]
    project_id: str
    scope: Literal["full"] = "full"
    title: str = "未命名项目"
    cleared_all: bool = True
    message: str | None = None


class SubscribedEvent(ChatEvent):
    type: Literal["subscribed"]
    project_id: str


class ProposedPlanEvent(ChatEvent):
    type: Literal["proposed_plan"]
    plan: dict[str, Any]
    project_id: str | None = None


class BlueprintStreamEvent(ChatEvent):
    type: Literal[
        "blueprint_draft_started",
        "blueprint_section_started",
        "blueprint_section_delta",
        "blueprint_section_completed",
        "blueprint_section_needs_revision",
        "blueprint_draft_saved",
        "blueprint_validation_completed",
        "blueprint_proposed",
        "blueprint_approved",
        "blueprint_revision_proposed",
        "blueprint_revision_applied",
        "blueprint_cleared",
    ]
    project_id: str | None = None
    section_id: str | None = None
    title: str | None = None
    section_index: int | None = None
    window_index: int | None = None
    window_count: int | None = None
    status: str | None = None
    summary_text: str | None = None
    failure_reason: str | None = None
    display_blocks: list[dict[str, Any]] | None = None
    view_model_patch: dict[str, Any] | None = None
    blueprint_ref: dict[str, Any] | None = None
    debug_json_path: str | None = None
    validation: dict[str, Any] | None = None


class BlueprintTreeChangedEvent(ChatEvent):
    type: Literal["blueprint_tree_changed"]
    project_id: str | None = None
    tree_version: int | None = None
    draft_mode: str | None = None
    replacement: bool | None = None
    action: str
    parent_id: str | None = None
    node_id: str | None = None
    node: dict[str, Any] | None = None
    patch: dict[str, Any] | None = None


class InteractionInputEvent(ChatEvent):
    type: Literal["interaction_input_requested"]
    project_id: str | None = None
    status: str | None = None
    summary_text: str | None = None
    intake: dict[str, Any] | None = None


class ChecklistUpdatedEvent(ChatEvent):
    type: Literal["checklist_updated"]
    checklist: Any


class ConfirmRequiredEvent(ChatEvent):
    type: Literal["confirm_required"]
    action: str
    scope: str | None = None
    reason: str | None = None


class ModeUpdatedEvent(ChatEvent):
    type: Literal["mode_updated"]
    ok: bool | None = None
    mode: str | None = None
    sub_mode: str | None = None
    selected_video_mode: str | None = None


class SlashCommandEvent(ChatEvent):
    type: Literal["slash_command"]
    command: str
    action: str | None = None
    ok: bool = False


class DoctorResultEvent(ChatEvent):
    type: Literal["doctor_result"]
    ok: bool = True
    project_id: str
    text: str | None = None
    feature_flags: dict[str, Any] | None = None


class ErrorEvent(ChatEvent):
    type: Literal["error"]
    message: str
    recoverable: bool | None = None


class DoneEvent(ChatEvent):
    type: Literal["done"]
    status: str | None = None


class CancelRequestedEvent(ChatEvent):
    type: Literal["cancel_requested"]
    project_id: str | None = None
    streaming: bool | None = None
    queued_count: int | None = None


class CancelledEvent(ChatEvent):
    type: Literal["cancelled"]
    message: str | None = None


class InfoEvent(ChatEvent):
    type: Literal["info"]
    message: str


class QueuedEvent(ChatEvent):
    type: Literal["queued"]
    ok: bool | None = None
    queued_count: int | None = None
    error: str | None = None


class MergedMessagesEvent(ChatEvent):
    type: Literal["merged_messages"]
    count: int


class QueuedTurnStartedEvent(ChatEvent):
    type: Literal["queued_turn_started"]
    client_user_message_id: str | None = None
    message: str | None = None
    queued_remaining: int | None = None


class ParallelStartEvent(ChatEvent):
    type: Literal["parallel_start"]
    total_steps: int
    waves: int
    project_id: str


class StepFailedEvent(ChatEvent):
    type: Literal["step_failed"]
    error: str
    step_index: int | None = None
    tool: str | None = None


class StepCompletedEvent(ChatEvent):
    type: Literal["step_completed"]
    step_index: int
    tool: str
    title: str = ""
    result: Any = None
    progress: str = ""


class ParallelDoneEvent(ChatEvent):
    type: Literal["parallel_done"]
    completed: int
    total: int


_EVENT_MODELS: dict[str, type[ChatEvent]] = {
    "text_delta": TextDeltaEvent,
    "agent_round": AgentRoundEvent,
    "agent_round_done": AgentRoundDoneEvent,
    "subagent_round": SubagentRoundEvent,
    "token_usage": TokenUsageEvent,
    "tool_start": ToolStartEvent,
    "tool_done": ToolDoneEvent,
    "step_start": StepStartEvent,
    "step_done": StepDoneEvent,
    "canvas_action": CanvasActionEvent,
    "project_update": ProjectUpdateEvent,
    "project_switch": ProjectSwitchEvent,
    "project_reset": ProjectResetEvent,
    "subscribed": SubscribedEvent,
    "proposed_plan": ProposedPlanEvent,
    "interaction_input_requested": InteractionInputEvent,
    "blueprint_draft_started": BlueprintStreamEvent,
    "blueprint_section_started": BlueprintStreamEvent,
    "blueprint_section_delta": BlueprintStreamEvent,
    "blueprint_section_completed": BlueprintStreamEvent,
    "blueprint_section_needs_revision": BlueprintStreamEvent,
    "blueprint_draft_saved": BlueprintStreamEvent,
    "blueprint_validation_completed": BlueprintStreamEvent,
    "blueprint_proposed": BlueprintStreamEvent,
    "blueprint_approved": BlueprintStreamEvent,
    "blueprint_revision_proposed": BlueprintStreamEvent,
    "blueprint_revision_applied": BlueprintStreamEvent,
    "blueprint_cleared": BlueprintStreamEvent,
    "blueprint_tree_changed": BlueprintTreeChangedEvent,
    "checklist_updated": ChecklistUpdatedEvent,
    "confirm_required": ConfirmRequiredEvent,
    "mode_updated": ModeUpdatedEvent,
    "slash_command": SlashCommandEvent,
    "doctor_result": DoctorResultEvent,
    "error": ErrorEvent,
    "done": DoneEvent,
    "cancel_requested": CancelRequestedEvent,
    "cancelled": CancelledEvent,
    "info": InfoEvent,
    "queued": QueuedEvent,
    "merged_messages": MergedMessagesEvent,
    "queued_turn_started": QueuedTurnStartedEvent,
    "parallel_start": ParallelStartEvent,
    "step_failed": StepFailedEvent,
    "step_completed": StepCompletedEvent,
    "parallel_done": ParallelDoneEvent,
}


def validate_chat_event(event: dict[str, Any]) -> ChatEvent:
    event_type = event.get("type")
    model = _EVENT_MODELS.get(event_type) if isinstance(event_type, str) else None
    if model is None:
        return ChatEvent.model_validate(event)
    return model.model_validate(event)


def normalize_chat_event(event: dict[str, Any]) -> dict[str, Any]:
    return validate_chat_event(event).model_dump(mode="json", exclude_none=True)


def event_to_sse(event: dict[str, Any]) -> str:
    normalized = normalize_chat_event(event)
    return f"data: {json.dumps(normalized, ensure_ascii=False)}\n\n"
