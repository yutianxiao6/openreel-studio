"""Prompt MCP-style tools — view and override system prompts per project / per node.

Three layers (highest first):
  1. node-level   — WorkflowNode.model_config_json.system_prompt_override
  2. project-level — state.prompt_overrides[tool_name]
  3. default      — prompts/<file>.md (declared in app.prompts.DEFAULT_PROMPT_FILES)
"""
from __future__ import annotations

import json

from app.db.models import Project, WorkflowNode
from app.db.session import session_scope
from app.prompts import (
    DEFAULT_PROMPT_FILES,
    default_prompt_for,
    resolve_prompt,
)


async def prompt_list(project_id: str | None = None) -> list[dict]:
    """List every tool that has a default prompt, plus its override status."""
    overrides: dict[str, str] = {}
    if project_id:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project:
                state = json.loads(project.state_json or "{}")
                raw = state.get("prompt_overrides") or {}
                if isinstance(raw, dict):
                    overrides = {
                        k: v for k, v in raw.items() if isinstance(v, str)
                    }

    return [
        {
            "tool": tool,
            "default_file": filename,
            "has_project_override": tool in overrides,
            "project_override_length": len(overrides.get(tool, "")) if tool in overrides else 0,
        }
        for tool, filename in sorted(DEFAULT_PROMPT_FILES.items())
    ]


async def prompt_get(
    tool_name: str,
    project_id: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Return the effective prompt + each layer's contribution."""
    if tool_name not in DEFAULT_PROMPT_FILES:
        return {"error": f"Unknown tool: {tool_name}"}

    default = default_prompt_for(tool_name)
    project_override: str | None = None
    node_override: str | None = None

    if project_id:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project:
                state = json.loads(project.state_json or "{}")
                raw = (state.get("prompt_overrides") or {}).get(tool_name)
                if isinstance(raw, str) and raw.strip():
                    project_override = raw

    if node_id:
        async with session_scope() as session:
            node = await session.get(WorkflowNode, node_id)
            if node and node.model_config_json:
                try:
                    model_config = json.loads(node.model_config_json)
                except (json.JSONDecodeError, TypeError):
                    model_config = {}
                raw = model_config.get("system_prompt_override")
                if isinstance(raw, str) and raw.strip():
                    node_override = raw

    effective = await resolve_prompt(tool_name, project_id, node_id)
    layer = (
        "node" if node_override
        else "project" if project_override
        else "default"
    )

    return {
        "tool": tool_name,
        "default_file": DEFAULT_PROMPT_FILES[tool_name],
        "default_prompt": default,
        "project_override": project_override,
        "node_override": node_override,
        "effective_prompt": effective,
        "effective_layer": layer,
    }


async def prompt_update_override(
    project_id: str,
    tool_name: str,
    prompt: str,
) -> dict:
    """Set or replace the project-level override for `tool_name`.

    Use empty string or call prompt_clear_override to remove instead.
    """
    if tool_name not in DEFAULT_PROMPT_FILES:
        return {"error": f"Unknown tool: {tool_name}"}
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "prompt must be a non-empty string; use prompt_clear_override to remove"}

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        overrides = state.get("prompt_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[tool_name] = prompt
        state["prompt_overrides"] = overrides
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

    return {
        "tool": tool_name,
        "project_id": project_id,
        "length": len(prompt),
        "ok": True,
    }


async def prompt_clear_override(project_id: str, tool_name: str) -> dict:
    """Remove the project-level override; tool falls back to the default file."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        overrides = state.get("prompt_overrides")
        removed = False
        if isinstance(overrides, dict) and tool_name in overrides:
            del overrides[tool_name]
            state["prompt_overrides"] = overrides
            project.state_json = json.dumps(state, ensure_ascii=False)
            session.add(project)
            await session.commit()
            removed = True

    return {"tool": tool_name, "project_id": project_id, "removed": removed}


async def prompt_preview(
    tool_name: str,
    project_id: str | None = None,
    node_id: str | None = None,
    max_chars: int = 1200,
) -> dict:
    """Return a truncated preview of the effective prompt."""
    full = await resolve_prompt(tool_name, project_id, node_id)
    truncated = full[:max_chars]
    return {
        "tool": tool_name,
        "preview": truncated,
        "full_length": len(full),
        "truncated": len(full) > max_chars,
    }
