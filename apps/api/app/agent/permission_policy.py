"""Agent tool permission policy.

The orchestrator owns the loop, but the policy owns the pre-tool boundary.
Keep decisions deterministic and easy to unit test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.collaboration_mode import is_plan_mode


PLAN_MODE_EXPLICIT_ALLOWED_TOOLS: set[str] = {
    "agent.review",
    "interaction.request_input",
    "node.get",
    "node.list",
    "project.get_state",
    "system.models",
    "system.status",
    "task.list",
    "tool.describe",
    "tool.execute",
    "tool.search",
    "vision.view_image",
}


@dataclass(frozen=True)
class ToolPermissionContext:
    tool_name: str
    state: dict[str, Any]
    user_message: str
    requires_plan: bool = False
    tool_args: dict[str, Any] = field(default_factory=dict)
    via_tool_execute: bool = False


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    result: dict[str, Any] | None = None

    @classmethod
    def allow(cls) -> "PermissionDecision":
        return cls(True, None)

    @classmethod
    def deny(cls, result: dict[str, Any]) -> "PermissionDecision":
        return cls(False, result)


def _agent_visible_tool_names() -> set[str]:
    from app.mcp_tools.registry import registry

    return registry.agent_visible_tool_names()


def _registered_visible_policy_tools(names: set[str]) -> set[str]:
    return set(names) & _agent_visible_tool_names()


def plan_mode_allowed_tools() -> set[str]:
    from app.mcp_tools.registry import registry

    allowed = set(PLAN_MODE_EXPLICIT_ALLOWED_TOOLS)
    for name in registry.agent_visible_tool_names():
        spec = registry.get(name)
        if spec and spec.is_read_only and not spec.is_destructive and not spec.requires_confirmation:
            allowed.add(name)
    return _registered_visible_policy_tools(allowed)


def permission_policy_tool_sets() -> dict[str, set[str]]:
    return {"plan_mode_allowed": plan_mode_allowed_tools()}


def decide_tool_permission(ctx: ToolPermissionContext) -> PermissionDecision:
    """Decide whether the Agent may call a tool before execution."""
    state = ctx.state or {}

    if ctx.tool_name.startswith("file.") and not ctx.via_tool_execute:
        return PermissionDecision.deny({
            "ok": False,
            "error": f"{ctx.tool_name} 是 deferred 文件工具，Agent Loop 不能绕过 tool.search/tool.describe/tool.execute 直接调用。",
            "error_kind": "deferred_tool_must_use_tool_execute",
            "hint": "读取上传文本时先 tool.search(category='file') / tool.describe，再 tool.execute；guide 规则正文使用 skill.project_mentor 的 guidance/guide_content。",
        })

    if is_plan_mode(state) and ctx.tool_name not in plan_mode_allowed_tools():
        return PermissionDecision.deny({
            "ok": False,
            "error": "当前处于 Plan Mode，只允许读取、审查和提问，不能修改或生成项目内容。",
            "error_kind": "plan_mode_read_only",
            "hint": "先用只读工具补齐依据，然后在 <proposed_plan>...</proposed_plan> 中给出计划；需要执行时退出 Plan Mode。",
        })

    return PermissionDecision.allow()
