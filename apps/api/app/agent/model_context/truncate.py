"""UTF-8-safe text and structured-output bounding."""
from __future__ import annotations

import json
from typing import Any

from .policy import ToolOutputPolicy, estimate_text_tokens


_PRIORITY_KEYS = (
    "ok",
    "status",
    "error_kind",
    "error",
    "hint",
    "suggested_next",
    "required_action",
    "requires_user_confirm",
    "awaiting_user",
    "node_id",
    "node_ids",
    "id",
    "task_id",
    "template_id",
    "artifact_ref",
    "repair_ref",
    "candidate_ref",
    "committed_ref",
    "url",
    "local_url",
    "remote_url",
    "message",
    "summary",
    "content_page",
    "resource",
    "contents",
    "next_cursor",
    "next_offset",
    "total",
    "returned",
    "truncated",
    "nodes",
    "items",
    "errors",
    "result",
)

_RESUMABLE_PAGE_KEYS = {
    "content",
    "offset",
    "limit",
    "returned_chars",
    "total_chars",
    "next_offset",
}
_RESUMABLE_RESOURCE_KEYS = {"resource", "contents", "next_cursor"}
_NORMAL_DOCUMENT_STRING_MAX_TOKENS = 600


def truncate_text_middle(text: str, max_tokens: int) -> str:
    value = str(text or "")
    if max_tokens <= 0:
        return ""
    if estimate_text_tokens(value) <= max_tokens:
        return value

    marker = f"\n…{estimate_text_tokens(value) - max_tokens} tokens omitted…\n"
    target_bytes = max(0, max_tokens * 3 - len(marker.encode("utf-8")))
    if target_bytes <= 0:
        return marker.strip()
    encoded = value.encode("utf-8")
    head_budget = target_bytes // 2
    tail_budget = target_bytes - head_budget
    head = _decode_prefix(encoded, head_budget)
    tail = _decode_suffix(encoded, tail_budget)
    candidate = f"{head}{marker}{tail}"
    while candidate and estimate_text_tokens(candidate) > max_tokens:
        if len(head) >= len(tail) and head:
            head = head[:-1]
        elif tail:
            tail = tail[1:]
        else:
            break
        candidate = f"{head}{marker}{tail}"
    return candidate


def _decode_prefix(value: bytes, limit: int) -> str:
    return value[: max(0, limit)].decode("utf-8", errors="ignore")


def _decode_suffix(value: bytes, limit: int) -> str:
    if limit <= 0:
        return ""
    return value[-limit:].decode("utf-8", errors="ignore")


def bounded_json_value(value: Any, *, policy: ToolOutputPolicy, budget_tokens: int) -> Any:
    """Return valid JSON data that fits the requested serialized token budget."""

    max_items = max(1, policy.max_items)
    if policy.profile == "document":
        max_string_tokens = max(
            64,
            min(_NORMAL_DOCUMENT_STRING_MAX_TOKENS, budget_tokens // 3),
        )
        max_page_string_tokens = max(64, int(budget_tokens * 0.88))
    else:
        max_string_tokens = max(48, min(600, budget_tokens // 3))
        max_page_string_tokens = max_string_tokens

    projected = _project(
        value,
        max_string_tokens=max_string_tokens,
        max_page_string_tokens=max_page_string_tokens,
        max_items=max_items,
        depth=0,
        max_depth=7,
    )
    for _ in range(8):
        rendered = json.dumps(projected, ensure_ascii=False, default=str, separators=(",", ":"))
        if estimate_text_tokens(rendered) <= budget_tokens:
            return projected
        max_string_tokens = max(16, max_string_tokens // 2)
        max_page_string_tokens = max(16, max_page_string_tokens // 2)
        max_items = max(1, max_items // 2)
        projected = _project(
            value,
            max_string_tokens=max_string_tokens,
            max_page_string_tokens=max_page_string_tokens,
            max_items=max_items,
            depth=0,
            max_depth=6,
        )

    preview = truncate_text_middle(
        json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")),
        max(16, budget_tokens - 24),
    )
    return {"truncated": True, "preview": preview}


def _project(
    value: Any,
    *,
    max_string_tokens: int,
    max_page_string_tokens: int,
    max_items: int,
    depth: int,
    max_depth: int,
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return truncate_text_middle(value, max_string_tokens)
    if depth >= max_depth:
        return {"truncated": True, "type": type(value).__name__}
    if isinstance(value, dict):
        ordered_keys = [key for key in _PRIORITY_KEYS if key in value]
        ordered_keys.extend(key for key in value if key not in ordered_keys)
        selected = ordered_keys[:max_items]
        resumable_page = _RESUMABLE_PAGE_KEYS.issubset(value)
        resumable_resource = _RESUMABLE_RESOURCE_KEYS.issubset(value)
        projected = {
            str(key): _project(
                value[key],
                max_string_tokens=(
                    max_page_string_tokens
                    if (resumable_page and key == "content")
                    or (resumable_resource and key == "contents")
                    else max_string_tokens
                ),
                max_page_string_tokens=max_page_string_tokens,
                max_items=max_items,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for key in selected
        }
        omitted = len(ordered_keys) - len(selected)
        if omitted > 0:
            projected["_omitted_keys"] = omitted
        return projected
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        selected = items[:max_items]
        projected_items = [
            _project(
                item,
                max_string_tokens=max_string_tokens,
                max_page_string_tokens=max_page_string_tokens,
                max_items=max_items,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in selected
        ]
        if len(items) > len(selected):
            projected_items.append({"_omitted_items": len(items) - len(selected)})
        return projected_items
    return truncate_text_middle(str(value), max_string_tokens)
