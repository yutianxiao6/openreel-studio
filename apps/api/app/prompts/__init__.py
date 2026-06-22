"""Prompt resolver — three-layer lookup for tool system prompts.

Priority (highest first):
  1. node.model_config_json.system_prompt_override — explicit node-level override
  2. state.prompt_overrides[tool_name]  — project-level override
  3. python module under app.prompts/   — auto-discovered .py with NAME or ALIASES

Each .py module declares NAME (str) or ALIASES (list[str]), optionally PROMPT
(static text) and / or build(ctx: WorkerContext) -> str. build() takes priority
over PROMPT when both are present.
"""
from __future__ import annotations

import json
from typing import Optional

from app.db.models import Project, WorkflowNode
from app.db.session import session_scope
from app.prompts._section import (
    WorkerContext,
    all_tool_names,
    get_section,
    render,
)


def default_prompt_for(tool_name: str, ctx: WorkerContext | None = None) -> str:
    return render(tool_name, ctx or WorkerContext())


async def resolve_prompt(
    tool_name: str,
    project_id: Optional[str] = None,
    node_id: Optional[str] = None,
    ctx: WorkerContext | None = None,
) -> str:
    """Return the system prompt for `tool_name`, applying overrides.

    Lookup order:
      1. node_id → WorkflowNode.model_config_json.system_prompt_override
      2. project_id → state.prompt_overrides[tool_name] (if non-empty)
      3. .py module's build(ctx) or PROMPT
    """
    if node_id:
        async with session_scope() as session:
            node = await session.get(WorkflowNode, node_id)
            if node and node.model_config_json:
                try:
                    model_config = json.loads(node.model_config_json)
                except (json.JSONDecodeError, TypeError):
                    model_config = {}
                override = model_config.get("system_prompt_override")
                if isinstance(override, str) and override.strip():
                    return override

    if project_id:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project:
                state = json.loads(project.state_json or "{}")
                overrides = state.get("prompt_overrides") or {}
                override = overrides.get(tool_name)
                if isinstance(override, str) and override.strip():
                    return override

    return default_prompt_for(tool_name, ctx)


# Backwards-compatible mapping for prompt_tools.py — now lists every tool that
# has a registered .py section, instead of pointing at .md files.
DEFAULT_PROMPT_FILES: dict[str, str] = {
    name: f"{name}.py" for name in all_tool_names()
}


def load_prompt_file(filename: str) -> str:
    """Deprecated — kept for any external caller that still loads raw files.

    Always returns empty string now; .py modules expose content via build/PROMPT.
    """
    return ""


__all__ = [
    "DEFAULT_PROMPT_FILES",
    "WorkerContext",
    "default_prompt_for",
    "get_section",
    "load_prompt_file",
    "resolve_prompt",
]
