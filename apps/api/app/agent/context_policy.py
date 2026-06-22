"""State-only context visibility policy for agent turns."""
from __future__ import annotations

from typing import Any


_PENDING_CONTEXT_KEYS = (
    "_pending_reset_confirm",
    "_pending_tool_confirm",
    "pending_blueprint_confirmation",
    "pending_video_blueprint_request",
    "pending_video_mode_choice",
    "pending_video_brief",
    "pending_blueprint_intake",
    "pending_blueprint_review",
    "pending_blueprint_draft",
    "pending_blueprint_revision",
    "pending_blueprint_section_review",
    "blueprint_window_progress",
)
_ACTIVE_BLUEPRINT_PROGRESS_STATUSES = {
    "drafting",
    "pending_review",
    "pending_section_confirmation",
    "paused_for_section_review",
}


def has_state_continuation_context(state: dict[str, Any]) -> bool:
    """Return whether persisted state says this turn continues an open workflow.

    This deliberately does not inspect user text. Natural-language decisions
    stay with the model; this policy only decides how much previous context is
    visible to reduce stale-history drift.
    """
    if not isinstance(state, dict):
        return False
    for key in _PENDING_CONTEXT_KEYS:
        value = state.get(key)
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True

    progress = state.get("blueprint_generation_progress")
    if isinstance(progress, dict):
        status = str(progress.get("status") or "")
        if status in _ACTIVE_BLUEPRINT_PROGRESS_STATUSES:
            return True

    return False


def chat_history_visible_for_turn(state: dict[str, Any]) -> bool:
    """Return whether persisted chat history should be visible this turn.

    Pending confirmations, blueprint drafts/reviews and other resumable state
    are already represented in runtime context. Pulling old chat text just
    because such state exists makes new messages drift into old executions. For
    ordinary turns, keep full active chat history; compaction handles pressure
    only when context is near the limit.
    """
    if not isinstance(state, dict):
        return True

    for key in _PENDING_CONTEXT_KEYS:
        value = state.get(key)
        if isinstance(value, dict) and value:
            return False
        if isinstance(value, list) and value:
            return False
        if isinstance(value, str) and value.strip():
            return False

    progress = state.get("blueprint_generation_progress")
    if isinstance(progress, dict):
        status = str(progress.get("status") or "")
        if status in _ACTIVE_BLUEPRINT_PROGRESS_STATUSES:
            return False

    return True
