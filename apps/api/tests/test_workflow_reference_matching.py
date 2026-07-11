from app.mcp_tools.workflow_reference_matching import (
    selector_key,
    workflow_alias_equal,
    workflow_context_get,
    workflow_tokens_from_value,
    workflow_tokens_match,
    workflow_values_at_path,
)


def test_reference_aliases_ignore_case_and_separators() -> None:
    assert selector_key(" Lin-Zhou_01 ") == "linzhou01"
    assert workflow_alias_equal("Lin Zhou", "lin_zhou") is True
    assert workflow_alias_equal("林-舟", "林舟") is True
    assert workflow_alias_equal("林舟", "沈鸢") is False


def test_context_lookup_and_nested_list_path_use_normalized_keys() -> None:
    context = {
        "character-plan": {
            "output": {
                "segments": [
                    {"characters": [{"name": "林舟"}, {"name": "沈鸢"}]},
                    {"characters": [{"name": "顾川"}]},
                ]
            }
        }
    }

    plan = workflow_context_get(context, "character_plan")
    assert workflow_values_at_path(plan, "output.segments[].characters[].name") == [
        "林舟",
        "沈鸢",
        "顾川",
    ]


def test_reference_tokens_respect_explicit_identity_fields() -> None:
    selected = workflow_tokens_from_value(
        [{"character_id": "lin_zhou", "name": "林舟"}],
        ["character_id"],
    )
    matching = workflow_tokens_from_value(
        {"character_id": "lin-zhou", "name": "同名演员"},
        ["character_id"],
    )
    unrelated = workflow_tokens_from_value(
        {"character_id": "shen_yuan", "name": "林舟"},
        ["character_id"],
    )

    assert workflow_tokens_match(selected, matching) is True
    assert workflow_tokens_match(selected, unrelated) is False
