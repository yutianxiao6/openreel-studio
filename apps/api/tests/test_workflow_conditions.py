from app.mcp_tools.workflow_conditions import workflow_step_condition_skipped


def test_structured_positive_conditions_support_comparison_and_empty_operators() -> None:
    inputs = {
        "episode_count": 1,
        "enabled": False,
        "style": "cinematic",
        "notes": "",
    }

    assert workflow_step_condition_skipped(
        {"when": {"path": "inputs.episode_count", "op": "lte", "value": 1}}, inputs
    ) is False
    assert workflow_step_condition_skipped(
        {"when": {"path": "inputs.enabled", "op": "eq", "value": False}}, inputs
    ) is False
    assert workflow_step_condition_skipped(
        {"when": {"path": "inputs.style", "op": "eq", "value": "cinematic"}}, inputs
    ) is False
    assert workflow_step_condition_skipped(
        {"when": {"path": "inputs.notes", "op": "empty"}}, inputs
    ) is False
    assert workflow_step_condition_skipped(
        {"when": {"path": "inputs.episode_count", "op": "gt", "value": 1}}, inputs
    ) is True


def test_missing_or_unknown_conditions_do_not_skip() -> None:
    assert workflow_step_condition_skipped({}, {}) is False
    assert workflow_step_condition_skipped({"when": "legacy string"}, {}) is False
    assert workflow_step_condition_skipped(
        {"when": {"path": "unknown.count", "op": "lt", "value": 2}}, {"count": 1}
    ) is False
