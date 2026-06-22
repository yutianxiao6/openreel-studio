"""Blueprint confirmation state helpers."""
from __future__ import annotations

import time
from typing import Any


BLUEPRINT_TREE_PLAN_KIND = "blueprint_tree"
BLUEPRINT_CONFIRMATION_ROLE = "blueprint_confirmation"
PENDING_BLUEPRINT_CONFIRMATION_KEY = "pending_blueprint_confirmation"


def is_blueprint_tree_plan(plan: Any) -> bool:
    return isinstance(plan, dict) and str(plan.get("kind") or "") == BLUEPRINT_TREE_PLAN_KIND


def mark_blueprint_confirmation_plan(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan)
    out["kind"] = BLUEPRINT_TREE_PLAN_KIND
    out["approval_role"] = BLUEPRINT_CONFIRMATION_ROLE
    out["ui_surface"] = "blueprint_confirmation"
    out["execution_state_source"] = None
    return out


def make_blueprint_confirmation(plan: dict[str, Any], *, status: str = "pending") -> dict[str, Any]:
    stored_plan = mark_blueprint_confirmation_plan(plan)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "kind": BLUEPRINT_CONFIRMATION_ROLE,
        "status": status,
        "id": stored_plan.get("id"),
        "title": stored_plan.get("title") or "",
        "summary": stored_plan.get("summary") or "",
        "plan_id": stored_plan.get("id"),
        "tree_version": stored_plan.get("tree_version"),
        "replacement": bool(stored_plan.get("replacement")),
        "plan": stored_plan,
        "created_at": stored_plan.get("created_at") or now,
        "updated_at": now,
    }


def pending_blueprint_confirmation(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    confirmation = state.get(PENDING_BLUEPRINT_CONFIRMATION_KEY)
    if isinstance(confirmation, dict):
        plan = confirmation.get("plan")
        if is_blueprint_tree_plan(plan):
            out = dict(confirmation)
            out["plan"] = mark_blueprint_confirmation_plan(plan)
            return out
    return None


def pending_blueprint_plan(state: dict[str, Any] | None) -> dict[str, Any] | None:
    confirmation = pending_blueprint_confirmation(state)
    if not confirmation:
        return None
    plan = confirmation.get("plan")
    return mark_blueprint_confirmation_plan(plan) if is_blueprint_tree_plan(plan) else None


def set_pending_blueprint_confirmation(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    stored_plan = mark_blueprint_confirmation_plan(plan)
    state[PENDING_BLUEPRINT_CONFIRMATION_KEY] = make_blueprint_confirmation(stored_plan)
    state.pop("pending_plan", None)
    state.pop("pending_plan_preview_checklist", None)
    return stored_plan


def pop_pending_blueprint_confirmation(state: dict[str, Any]) -> dict[str, Any] | None:
    plan = pending_blueprint_plan(state)
    state.pop(PENDING_BLUEPRINT_CONFIRMATION_KEY, None)
    state.pop("pending_plan", None)
    state.pop("pending_plan_preview_checklist", None)
    return plan
