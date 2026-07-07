"""MCP-style tools (internal first, MCP-compatible signatures)."""
from app.mcp_tools import (
    agent_tools,
    canvas_tools,
    drama_tools,
    event_tools,
    feature_tools,
    file_tools,
    image_operation_tools,
    media_tools,
    memory_tools,
    model_tools,
    project_tools,
    prompt_tools,
    shot_tools,
    skill_tools,
    workflow_tools,
    workflow_spec_tools,
)
from app.mcp_tools.registry import load_skills, register, registry

# Load any user-supplied skills
load_skills()

__all__ = [
    "agent_tools",
    "canvas_tools",
    "drama_tools",
    "event_tools",
    "feature_tools",
    "file_tools",
    "image_operation_tools",
    "media_tools",
    "memory_tools",
    "model_tools",
    "project_tools",
    "prompt_tools",
    "shot_tools",
    "skill_tools",
    "workflow_tools",
    "workflow_spec_tools",
    "registry",
    "register",
    "load_skills",
]
