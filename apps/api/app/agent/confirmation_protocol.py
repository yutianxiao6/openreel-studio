"""Structured confirmation helpers.

This module intentionally does not parse natural-language messages. It only
reads structured UI metadata such as decision_inputs.kind/action/values.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_CONFIRMATION_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class StructuredDecision:
    kind: str = ""
    target: str = ""
    action: str = ""
    feedback: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def matches(self, *targets: str) -> bool:
        if not targets:
            return bool(self.kind or self.target)
        return self.kind in targets or self.target in targets


def decision_from_user_metadata(user_metadata: dict[str, Any] | None) -> StructuredDecision:
    if not isinstance(user_metadata, dict):
        return StructuredDecision()
    decision = user_metadata.get("decisionInputs")
    if not isinstance(decision, dict):
        return StructuredDecision()
    values = decision.get("values") if isinstance(decision.get("values"), dict) else {}
    kind = str(decision.get("kind") or "").strip()
    target = str(
        decision.get("target")
        or decision.get("subject")
        or values.get("target")
        or values.get("subject")
        or kind
    ).strip()
    action = str(
        decision.get("action")
        or decision.get("decision")
        or values.get("action")
        or values.get("decision")
        or values.get("choice")
        or ""
    ).strip().lower()
    feedback = str(
        decision.get("feedback")
        or decision.get("comment")
        or values.get("feedback")
        or values.get("comment")
        or values.get("revision_request")
        or ""
    ).strip()
    return StructuredDecision(
        kind=kind,
        target=target,
        action=action,
        feedback=feedback,
        values=dict(values),
        raw=dict(decision),
    )


def decision_action(
    user_metadata: dict[str, Any] | None,
    *targets: str,
) -> tuple[str, str]:
    decision = decision_from_user_metadata(user_metadata)
    if not decision.matches(*targets):
        return "", ""
    return decision.action, decision.feedback


def build_pending_confirmation(
    *,
    kind: str,
    risk: str,
    actions: list[str],
    confirmation_id: str,
    title: str = "",
    summary: str = "",
    checksum: str = "",
    can_skip: bool = False,
    expires_at: int | None = None,
) -> dict[str, Any]:
    return {
        "id": confirmation_id,
        "kind": kind,
        "risk": risk,
        "title": title,
        "summary": summary,
        "actions": list(actions),
        "checksum": checksum,
        "can_skip": bool(can_skip),
        "created_at": int(time.time()),
        "expires_at": expires_at,
    }


def confirmation_expires_at(
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
) -> int:
    base = int(now if now is not None else time.time())
    return base + max(1, int(ttl_seconds))


def _coerce_epoch_seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        try:
            normalized = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def is_pending_confirmation_expired(
    pending: dict[str, Any] | None,
    *,
    now: int | None = None,
) -> bool:
    """Return True only when the pending object has an explicit expired expires_at."""
    if not isinstance(pending, dict):
        return False
    expires_at = _coerce_epoch_seconds(pending.get("expires_at"))
    if expires_at is None:
        return False
    current = int(now if now is not None else time.time())
    return expires_at <= current


_PENDING_CONFIRMATION_KEYS: dict[str, str] = {
    "_pending_reset_confirm": "reset_project",
    "_pending_tool_confirm": "tool_confirmation",
    "pending_blueprint_revision": "blueprint_revision",
    "pending_blueprint_section_review": "blueprint_section_review",
}


def expired_pending_confirmation_patch(
    state: dict[str, Any] | None,
    *,
    now: int | None = None,
) -> tuple[dict[str, None], list[dict[str, Any]]]:
    """Build a cleanup patch for explicitly expired pending confirmations.

    Missing expires_at means "not managed by the expiration protocol" and is
    intentionally left untouched.
    """
    if not isinstance(state, dict):
        return {}, []

    patch: dict[str, None] = {}
    expired: list[dict[str, Any]] = []
    for state_key, kind in _PENDING_CONFIRMATION_KEYS.items():
        pending = state.get(state_key)
        if not isinstance(pending, dict):
            continue
        if not is_pending_confirmation_expired(pending, now=now):
            continue
        patch[state_key] = None
        expired.append({
            "state_key": state_key,
            "confirmation_kind": kind,
            "confirmation_id": pending.get("id") or pending.get("confirmation_id"),
            "risk": pending.get("risk"),
            "scope": pending.get("scope"),
            "target": pending.get("target"),
            "created_at": pending.get("created_at") or pending.get("ts"),
            "expires_at": pending.get("expires_at"),
            "version": pending.get("version"),
            "target_node_id": pending.get("target_node_id"),
        })
    return patch, expired
