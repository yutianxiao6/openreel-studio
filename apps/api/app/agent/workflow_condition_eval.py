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
