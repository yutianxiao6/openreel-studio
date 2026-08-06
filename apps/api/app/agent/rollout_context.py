"""Persist bounded typed Responses items between user turns.

The chat transcript remains a user-facing projection.  This module stores the
model-facing rollout separately so function calls, function outputs, message
phases, and encrypted reasoning can be replayed without leaking them through
the public message API.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.context_compact import compact_preserved_tail, estimate_tokens
from app.services.llm_responses import response_message_phase, response_message_text


PERSISTED_ROLLOUT_VERSION = 1
PERSISTED_ROLLOUT_TOKEN_BUDGET = 40_000


@dataclass(frozen=True)
class PersistedRollout:
    items: tuple[dict[str, Any], ...]
    append_assistant_text: str = ""
    compacted: bool = False
    original_tokens: int = 0
    persisted_tokens: int = 0


def assistant_answer_text(items: list[dict[str, Any]]) -> str:
    """Return assistant answer text while excluding commentary-phase messages."""

    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if response_message_phase(item) == "commentary":
            continue
        text = response_message_text(item)
        if text:
            parts.append(text)
    return "".join(parts)


def _missing_assistant_suffix(
    items: list[dict[str, Any]],
    assistant_text: str,
    *,
    normalize_text: Callable[[str], str] | None = None,
) -> str:
    visible_text = assistant_answer_text(items)
    if normalize_text is not None:
        visible_text = normalize_text(visible_text)
    if not visible_text:
        return assistant_text
    if assistant_text.startswith(visible_text):
        return assistant_text[len(visible_text) :]
    # The typed item is the authoritative model output.  A mismatch should not
    # duplicate the full assistant answer on the next user turn.
    return ""


def encode_persisted_rollout(
    items: list[dict[str, Any]],
    assistant_text: str = "",
    *,
    normalize_text: Callable[[str], str] | None = None,
    token_budget: int = PERSISTED_ROLLOUT_TOKEN_BUDGET,
) -> str | None:
    clean_items = [dict(item) for item in items if isinstance(item, dict)]
    if not clean_items:
        return None

    original_tokens = estimate_tokens(clean_items)
    persisted_items = clean_items
    compacted = False
    if token_budget > 0 and original_tokens > token_budget:
        persisted_items = compact_preserved_tail(clean_items, token_budget=token_budget)
        compacted = persisted_items != clean_items

    payload = {
        "version": PERSISTED_ROLLOUT_VERSION,
        "items": persisted_items,
        "append_assistant_text": _missing_assistant_suffix(
            clean_items,
            str(assistant_text or ""),
            normalize_text=normalize_text,
        ),
        "compacted": compacted,
        "original_tokens": original_tokens,
        "persisted_tokens": estimate_tokens(persisted_items),
    }
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def decode_persisted_rollout(value: str | None) -> PersistedRollout | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != PERSISTED_ROLLOUT_VERSION:
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None
    items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
    if not items:
        return None

    def non_negative_int(raw: Any) -> int:
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    return PersistedRollout(
        items=items,
        append_assistant_text=str(payload.get("append_assistant_text") or ""),
        compacted=bool(payload.get("compacted")),
        original_tokens=non_negative_int(payload.get("original_tokens")),
        persisted_tokens=non_negative_int(payload.get("persisted_tokens")),
    )


def rollout_context_messages(
    value: str | None,
    *,
    fallback_assistant_text: str = "",
) -> list[dict[str, Any]]:
    rollout = decode_persisted_rollout(value)
    if rollout is None:
        return (
            [{"role": "assistant", "content": fallback_assistant_text}]
            if fallback_assistant_text
            else []
        )
    messages = [dict(item) for item in rollout.items]
    if rollout.append_assistant_text:
        messages.append({"role": "assistant", "content": rollout.append_assistant_text})
    return messages


def persisted_rollout_tokens(value: str | None) -> int:
    rollout = decode_persisted_rollout(value)
    return rollout.persisted_tokens if rollout is not None else 0
