"""Hard budget tests for prompt content that enters every agent turn."""

from __future__ import annotations

import json

from app.agent.prompt_assembler import (
    PromptContext,
    assemble_split_result,
    select_tool_namespaces,
)
from app.mcp_tools.registry import registry


SYSTEM_CHAR_LIMIT = 2_200
HISTORY_CHAR_LIMIT = 1_300
SYSTEM_PLUS_HISTORY_CHAR_LIMIT = 3_600
RUNTIME_CHAR_LIMIT = 1_900
CORE_TOOLS_JSON_CHAR_LIMIT = 11_500
SYSTEM_PLUS_CORE_TOOLS_CHAR_LIMIT = 13_500
TOTAL_FIXED_PROMPT_CHAR_LIMIT = 14_500
SECTION_COUNT_LIMIT = 8

EXPECTED_FIXED_SECTIONS = [
    "identity",
    "working_loop",
    "task_loop",
    "tool_loader",
    "core_rules",
    "delete_rule",
    "memory_write",
    "runtime_context",
]


def _fixed_prompt_snapshot() -> dict[str, object]:
    ctx = PromptContext(
        project_id="budget-test",
        user_message="你好",
        state={},
        attachments=[],
    )
    prompt = assemble_split_result(ctx)
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(ctx))
    tools_json = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    return {
        "prompt": prompt,
        "tools": tools,
        "tools_json": tools_json,
        "section_names": [section.name for section in prompt.sections],
    }


def _has_schema_description_metadata(value: object, *, inside_properties: bool = False) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "description" and not inside_properties:
                return True
            if _has_schema_description_metadata(child, inside_properties=(key == "properties")):
                return True
    elif isinstance(value, list):
        return any(_has_schema_description_metadata(item) for item in value)
    return False


def test_fixed_prompt_prefix_stays_under_hard_budget() -> None:
    snapshot = _fixed_prompt_snapshot()
    prompt = snapshot["prompt"]
    tools_json = str(snapshot["tools_json"])

    system_chars = len(prompt.system)
    history_chars = len(prompt.history)
    runtime_chars = len(prompt.runtime)
    system_plus_history = system_chars + history_chars
    core_tools_chars = len(tools_json)

    assert system_chars <= SYSTEM_CHAR_LIMIT
    assert history_chars <= HISTORY_CHAR_LIMIT
    assert runtime_chars <= RUNTIME_CHAR_LIMIT
    assert system_plus_history <= SYSTEM_PLUS_HISTORY_CHAR_LIMIT
    assert core_tools_chars <= CORE_TOOLS_JSON_CHAR_LIMIT
    assert system_chars + core_tools_chars <= SYSTEM_PLUS_CORE_TOOLS_CHAR_LIMIT
    assert system_plus_history + core_tools_chars <= TOTAL_FIXED_PROMPT_CHAR_LIMIT


def test_fixed_prompt_only_loads_known_minimal_sections() -> None:
    snapshot = _fixed_prompt_snapshot()
    prompt = snapshot["prompt"]

    assert snapshot["section_names"] == EXPECTED_FIXED_SECTIONS
    assert len(prompt.sections) <= SECTION_COUNT_LIMIT
    assert all(section.trigger in {"always", "factory"} for section in prompt.sections)
    assert all(section.chars <= 700 for section in prompt.sections if section.source == "static")


def test_core_tool_schema_descriptions_are_not_in_always_loaded_prefix() -> None:
    snapshot = _fixed_prompt_snapshot()
    tools = snapshot["tools"]
    tools_json = str(snapshot["tools_json"])

    tool_names = {
        str((tool.get("function") or {}).get("name") or "").replace("__", ".")
        for tool in tools
    }
    assert len(tools) == 19
    assert "agent.review" in tool_names
    assert "canvas.delete" in tool_names
    assert "task.create" in tool_names
    assert "task.complete" in tool_names
    assert "tool.search" in tool_names
    assert "tool.describe" in tool_names
    assert "tool.execute" in tool_names
    assert "vision.view_image" in tool_names
    assert "canvas.connect_nodes" not in tool_names
    assert '"description"' in tools_json
    parameter_json = json.dumps(
        [
            (tool.get("function") or {}).get("parameters") or {}
            for tool in tools
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert not _has_schema_description_metadata(json.loads(parameter_json))
