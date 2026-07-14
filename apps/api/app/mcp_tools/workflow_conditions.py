"""Deterministic evaluation for structured positive workflow conditions."""
from __future__ import annotations

from typing import Any

from app.agent.workflow_condition_eval import condition_matches


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
    if isinstance(condition, dict):
        path = str(condition.get("path") or "").strip()
        key = path[len("inputs."):] if path.startswith("inputs.") else ""
        if not key:
            return False
        left = condition_value_from_inputs(inputs, key)
        operator = str(condition.get("op") or "").strip()
        right = condition.get("value")
        return not condition_matches(left, operator, right)
    return False
