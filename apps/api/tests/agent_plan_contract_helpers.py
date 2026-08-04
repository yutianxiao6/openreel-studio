"""Shared imports and helpers for split agent contract test modules."""
# ruff: noqa: F401

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import message_queue as mq
from app.agent import orchestrator as orchestrator_module
from app.agent import prompts as prompt_sections_pkg
from app.agent import slash_commands, video_intake
from app.agent.confirmation_protocol import (
    confirmation_expires_at,
    decision_action,
    decision_from_user_metadata,
    expired_pending_confirmation_patch,
    is_pending_confirmation_expired,
)
from app.agent.context_policy import chat_history_visible_for_turn
from app.agent.lifecycle_hooks import (
    PermissionDenialState,
    next_permission_denial_state,
    run_before_model_call,
    run_pre_tool_use,
    run_stop_after_text_response,
)
from app.agent.orchestrator import AgentOrchestrator
from app.agent.permission_policy import (
    ToolPermissionContext,
    decide_tool_permission,
)
from app.agent.prompt_assembler import (
    PromptContext,
    assemble_split_result,
    get_split_prompt_result,
    select_tool_namespaces,
    select_tool_profile,
    trigger_matches,
)
from app.agent.prompts import (
    attachment_rule,
    core_rules,
    memory_write,
    plan_mode,
    runtime_context,
    task_loop,
    working_loop,
)
from app.agent.reset_flow import (
    reset_canvas_events,
    reset_confirmation_text,
)
from app.agent.video_intake import video_intake_state_patch_for_interaction
from app.agent.video_mode import build_video_mode_system_reminder
from app.api import routes_chat, routes_projects, routes_tools
from app.mcp_tools import (
    drama_tools,
    interaction_tools,
    node_universal,
    tool_meta_tools,
)
from app.mcp_tools.registry import registry
from app.services import media_generation, media_provider


plan_rule = SimpleNamespace(
    PROMPT=(
        "# Complex Work\n\n"
        "Choose video workflow Skills from the automatic name/description catalog, resolve exact handles with `skills.list`, read them with `skills.read`, then select reusable template candidates, "
        "then use `text` / `image` / `video` nodes as creative state, "
        "and use tasks only as a progress ledger. Write dependencies into node fields and verify outputs before completion."
    )
)


def _decision_metadata(
    kind: str,
    action: str,
    *,
    feedback: str = "",
    target: str | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "kind": kind,
        "action": action,
        "values": {"action": action},
    }
    if target:
        payload["target"] = target
        payload["values"]["target"] = target
    if feedback:
        payload["feedback"] = feedback
        payload["values"]["feedback"] = feedback
    return {"decisionInputs": payload}


def _visible_tools(message: str | None) -> set[str]:
    ctx = PromptContext(
        project_id="test",
        user_message=message or "",
        state={},
        attachments=[],
    )
    tools = registry.get_tools_for_agent_loop(profile=select_tool_profile(ctx))
    return {tool["function"]["name"].replace("__", ".") for tool in tools}


def _assert_system_prompt_v2(
    name: str,
    text: str,
    *,
    max_len: int,
    required_markers: tuple[str, ...],
) -> None:
    manual_markers = (
        "###",
        "```",
        "|---|",
        "❌",
        "报错示例",
        "标准修复流程",
        "决策树",
        "按下表",
        "推断顺序",
        "审核流程",
        "审核清单",
        "典型决策",
    )
    manual_labels = ("边界：", "用法：", "失败：")

    assert len(text) <= max_len, name
    assert not any(marker in text for marker in manual_markers), name
    assert not any(label in text for label in manual_labels), name
    for marker in required_markers:
        assert marker in text, (name, marker)


__all__ = [name for name in globals() if not name.startswith("__")]
