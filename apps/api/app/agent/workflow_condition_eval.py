"""Pure comparison helpers shared by V2 input conditions and loop gates."""
from __future__ import annotations

import math
from typing import Any


EMPTY_VALUES = (None, "", [], {})
NUMERIC_OPERATORS = {"lt", "lte", "gt", "gte"}


def coerce_condition_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def finite_condition_number(value: Any) -> float | None:
    number = coerce_condition_number(value)
    return number if number is not None and math.isfinite(number) else None


def condition_matches(left: Any, operator: str, right: Any = None) -> bool:
    if operator == "empty":
        return left in EMPTY_VALUES
    if operator == "not_empty":
        return left not in EMPTY_VALUES

    left_number = coerce_condition_number(left)
    right_number = coerce_condition_number(right)
    if left_number is not None and right_number is not None:
        left, right = left_number, right_number
    try:
        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if operator == "lt":
            return left < right
        if operator == "lte":
            return left <= right
        if operator == "gt":
            return left > right
        if operator == "gte":
            return left >= right
    except TypeError:
        return False
    return True


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


def workflow_step_condition_skipped(
    step: dict[str, Any],
    inputs: dict[str, Any] | None,
) -> bool:
    condition = step.get("when")
    if not isinstance(condition, dict):
        return False
    path = str(condition.get("path") or "").strip()
    key = path[len("inputs.") :] if path.startswith("inputs.") else ""
    if not key:
        return False
    left = condition_value_from_inputs(inputs, key)
    operator = str(condition.get("op") or "").strip()
    right = condition.get("value")
    return not condition_matches(left, operator, right)
