"""Pure matching helpers for dynamic workflow references."""
from __future__ import annotations

import re
from typing import Any


REFERENCE_SELECTOR_TOKEN_FIELDS = (
    "name",
    "reuse_key",
    "character",
    "character_id",
    "id",
    "key",
    "title",
    "label",
    "item",
)


def selector_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def workflow_alias_equal(left: Any, right: Any) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    return bool(
        left_text
        and right_text
        and (
            left_text == right_text
            or selector_key(left_text) == selector_key(right_text)
        )
    )


def workflow_context_get(context: dict[str, Any], key: Any) -> Any:
    key_text = str(key or "").strip()
    if not key_text:
        return None
    if key_text in context:
        return context[key_text]
    key_slug = selector_key(key_text)
    for candidate, value in context.items():
        if selector_key(candidate) == key_slug:
            return value
    return None


def flatten_workflow_values(values: list[Any]) -> list[Any]:
    flattened: list[Any] = []
    for value in values:
        if isinstance(value, list):
            flattened.extend(flatten_workflow_values(value))
        else:
            flattened.append(value)
    return flattened


def workflow_values_at_path(root: Any, path: str) -> list[Any]:
    segments = [segment.strip() for segment in str(path or "").split(".") if segment.strip()]
    values = [root]
    for segment in segments:
        wants_list = segment.endswith("[]")
        key = segment[:-2] if wants_list else segment
        index = int(key) if key.isdigit() else None
        next_values: list[Any] = []
        for value in values:
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if index is not None and isinstance(candidate, list):
                    if 0 <= index < len(candidate):
                        next_values.append(candidate[index])
                elif index is not None:
                    continue
                elif isinstance(candidate, dict) and key in candidate:
                    child = candidate.get(key)
                    if wants_list and isinstance(child, list):
                        next_values.extend(child)
                    else:
                        next_values.append(child)
                elif isinstance(candidate, list):
                    next_values.extend(candidate)
        values = next_values
        if not values:
            break
    return flatten_workflow_values(values)


def workflow_token_variants(value: Any) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    compact = selector_key(text)
    tokens = {text}
    if compact:
        tokens.add(compact)
    return tokens


def workflow_tokens_from_value(
    value: Any,
    fields: list[str] | tuple[str, ...] | None = None,
) -> set[str]:
    selected_fields = tuple(fields or REFERENCE_SELECTOR_TOKEN_FIELDS)
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key in selected_fields:
            if key in value:
                tokens.update(workflow_tokens_from_value(value.get(key), selected_fields))
        if not tokens:
            for key in REFERENCE_SELECTOR_TOKEN_FIELDS:
                if key in value:
                    tokens.update(workflow_tokens_from_value(value.get(key), selected_fields))
        return tokens
    if isinstance(value, list):
        for item in value:
            tokens.update(workflow_tokens_from_value(item, selected_fields))
        return tokens
    return workflow_token_variants(value)


def workflow_tokens_match(selected_tokens: set[str], candidate_tokens: set[str]) -> bool:
    return bool(selected_tokens & candidate_tokens)
