"""Deterministic condition evaluation for workflow step auto-skip rules."""
from __future__ import annotations

import re
from typing import Any


def condition_value_from_inputs(inputs: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(inputs, dict):
        return None
    if key in inputs:
        return inputs[key]
    normalized = key.strip().lower()
    for candidate, value in inputs.items():
        if str(candidate).strip().lower() == normalized:
            return value
    return None


def coerce_condition_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def workflow_auto_skip_condition_met(
    condition: str,
    inputs: dict[str, Any] | None,
) -> bool:
    text = str(condition or "").strip()
    if not text:
        return False
    match = re.fullmatch(
        r"\{\{\s*inputs\.([A-Za-z0-9_]+)\s*\}\}\s*(<=|>=|==|!=|<|>)\s*([+-]?\d+(?:\.\d+)?|true|false|\"[^\"]*\"|'[^']*')",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        left = condition_value_from_inputs(inputs, match.group(1))
        operator = match.group(2)
        raw_right = match.group(3)
        if raw_right.lower() in {"true", "false"}:
            right: Any = raw_right.lower() == "true"
        elif raw_right.startswith(("'", '"')) and raw_right.endswith(("'", '"')):
            right = raw_right[1:-1]
        else:
            right = coerce_condition_number(raw_right)
        left_number = coerce_condition_number(left)
        right_number = coerce_condition_number(right)
        if left_number is not None and right_number is not None:
            left_value: Any = left_number
            right_value: Any = right_number
        else:
            left_value = left
            right_value = right
        try:
            if operator == "<=":
                return left_value <= right_value
            if operator == ">=":
                return left_value >= right_value
            if operator == "<":
                return left_value < right_value
            if operator == ">":
                return left_value > right_value
            if operator == "==":
                return left_value == right_value
            if operator == "!=":
                return left_value != right_value
        except TypeError:
            return False
    empty_match = re.fullmatch(
        r"\{\{\s*inputs\.([A-Za-z0-9_]+)\s*\}\}\s+is\s+empty",
        text,
        flags=re.IGNORECASE,
    )
    if empty_match:
        return condition_value_from_inputs(inputs, empty_match.group(1)) in (None, "", [], {})
    return False


def workflow_step_auto_skipped(
    step: dict[str, Any],
    inputs: dict[str, Any] | None,
) -> bool:
    structured = step.get("when")
    if isinstance(structured, dict):
        path = str(structured.get("path") or "").strip()
        key = path[len("inputs."):] if path.startswith("inputs.") else ""
        if not key:
            return False
        left = condition_value_from_inputs(inputs, key)
        operator = str(structured.get("op") or "").strip()
        right = structured.get("value")
        if operator == "empty":
            return left not in (None, "", [], {})
        if operator == "not_empty":
            return left in (None, "", [], {})
        left_number = coerce_condition_number(left)
        right_number = coerce_condition_number(right)
        if left_number is not None and right_number is not None:
            left, right = left_number, right_number
        try:
            if operator == "eq":
                matched = left == right
            elif operator == "ne":
                matched = left != right
            elif operator == "lt":
                matched = left < right
            elif operator == "lte":
                matched = left <= right
            elif operator == "gt":
                matched = left > right
            elif operator == "gte":
                matched = left >= right
            else:
                matched = True
        except TypeError:
            matched = False
        return not matched
    condition = str(step.get("auto_skip_when") or "").strip()
    return bool(condition and workflow_auto_skip_condition_met(condition, inputs))
