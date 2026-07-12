"""Deterministic evaluation for structured positive workflow conditions."""
from __future__ import annotations

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


def workflow_step_condition_skipped(
    step: dict[str, Any],
    inputs: dict[str, Any] | None,
) -> bool:
    condition = step.get("when")
    if isinstance(condition, dict):
        path = str(condition.get("path") or "").strip()
        key = path[len("inputs."):] if path.startswith("inputs.") else ""
        if not key:
            return False
        left = condition_value_from_inputs(inputs, key)
        operator = str(condition.get("op") or "").strip()
        right = condition.get("value")
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
    return False
