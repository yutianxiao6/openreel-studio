from app.mcp_tools.workflow_conditions import (
    workflow_auto_skip_condition_met,
    workflow_step_auto_skipped,
)


def test_auto_skip_conditions_support_numbers_booleans_strings_and_empty() -> None:
    inputs = {
        "episodeCount": 1,
        "enabled": False,
        "style": "cinematic",
        "notes": "",
    }

    assert workflow_auto_skip_condition_met("{{inputs.episodeCount}} <= 1", inputs) is True
    assert workflow_auto_skip_condition_met("{{inputs.enabled}} == false", inputs) is True
    assert workflow_auto_skip_condition_met("{{inputs.style}} == 'cinematic'", inputs) is True
    assert workflow_auto_skip_condition_met("{{inputs.notes}} is empty", inputs) is True
    assert workflow_auto_skip_condition_met("{{inputs.episodeCount}} > 1", inputs) is False


def test_unknown_or_invalid_conditions_fail_closed() -> None:
    assert workflow_auto_skip_condition_met("python: delete_everything()", {}) is False
    assert workflow_auto_skip_condition_met("{{inputs.count}} < 2", {"count": "unknown"}) is False
    assert workflow_step_auto_skipped({}, {}) is False
