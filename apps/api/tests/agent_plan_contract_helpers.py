"""Shared imports and helpers for split agent contract test modules."""

import asyncio

from datetime import datetime, timedelta

import hashlib

import json

from pathlib import Path

from types import SimpleNamespace

from typing import Any

import pytest

from app.agent import message_queue as mq

from app.agent import prompts as prompt_sections_pkg

from app.agent import orchestrator as orchestrator_module

from app.agent import parallel_executor

from app.agent import project_blueprint

from app.agent.blueprint_validator import validate_blueprint_document

from app.agent import video_intake

from app.agent.video_intake import video_intake_state_patch_for_interaction

from app.agent.lifecycle_hooks import (
    PermissionDenialState,
    next_permission_denial_state,
    run_before_model_call,
    run_before_turn,
    run_post_tool_use_checklist,
    run_pre_tool_use,
    run_stop_after_text_response,
)

from app.agent.orchestrator import AgentOrchestrator

from app.agent.permission_policy import (
    ToolPermissionContext,
    decide_tool_permission,
)

from app.agent.planner import PLANNER_PROMPT

from app.agent.project_blueprint import (
    apply_blueprint_plan_to_state,
    build_blueprint_document_from_plan,
    clear_blueprint_state,
    recover_pending_blueprint_section_review_state,
    prepare_blueprint_draft_checkpoint,
    prepare_blueprint_draft_from_plan,
    render_blueprint_view_model,
    validate_plan_blueprint_binding,
)

from app.agent.prompt_assembler import (
    PromptContext,
    assemble_split_result,
    get_split_prompt_result,
    select_tool_profile,
    invalidate_cache,
    select_tool_namespaces,
    should_require_plan,
    trigger_matches,
)

from app.agent.context_policy import (
    chat_history_visible_for_turn,
    has_state_continuation_context,
)

from app.agent.confirmation_protocol import (
    build_pending_confirmation,
    confirmation_expires_at,
    decision_action,
    decision_from_user_metadata,
    expired_pending_confirmation_patch,
    is_pending_confirmation_expired,
)

from app.agent.reset_flow import (
    reset_canvas_events,
    reset_confirmation_text,
)

from app.agent import slash_commands

from app.agent.prompts import (
    attachment_rule,
    core_rules,
    memory_write,
    plan_mode,
    runtime_context,
    task_loop,
    working_loop,
)

from app.agent.video_mode import (
    build_video_mode_system_reminder,
)

from app.api import routes_chat, routes_projects, routes_tools

from app.prompts import WorkerContext, default_prompt_for

from app.mcp_tools import blueprint_tools, drama_tools, interaction_tools, media_tools, node_universal, tool_meta_tools

from app.mcp_tools.registry import (
    INTERNAL_RAW_RUNNER_TOOL_NAMES,
    UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES,
    UNREGISTERED_ASSET_WRITE_TOOL_NAMES,
    UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES,
    UNREGISTERED_CANVAS_CRUD_TOOL_NAMES,
    UNREGISTERED_CONFIG_WRITE_TOOL_NAMES,
    UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES,
    UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES,
    UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES,
    UNREGISTERED_FILE_WRITE_TOOL_NAMES,
    UNREGISTERED_GENERIC_SKILL_TOOL_NAMES,
    UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES,
    UNREGISTERED_MEDIA_STATUS_TOOL_NAMES,
    UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES,
    AGENT_HIDDEN_MEDIA_PROVIDER_READ_TOOL_NAMES,
    UNREGISTERED_MCP_META_TOOL_NAMES,
    UNREGISTERED_MODEL_CONFIG_TOOL_NAMES,
    UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES,
    UNREGISTERED_NODE_HELPER_TOOL_NAMES,
    UNREGISTERED_PLAN_CONTROL_TOOL_NAMES,
    UNREGISTERED_PROMPT_TOOL_NAMES,
    UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES,
    AGENT_HIDDEN_PROJECT_MODE_TOOL_NAMES,
    AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES,
    UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES,
    UNREGISTERED_SESSION_TOOL_NAMES,
    UNREGISTERED_TASK_HELPER_TOOL_NAMES,
    UNREGISTERED_TASK_WRITE_TOOL_NAMES,
    UNREGISTERED_TEAM_TOOL_NAMES,
    registry,
)

from app.services import media_generation

from app.services import media_provider

plan_rule = SimpleNamespace(PROMPT=(
    "# Complex Work\n\n"
    "Read video workflow skills with `skill.search(category='workflow')`, select reusable template candidates before running, "
    "then use `text` / `image` / `video` nodes as creative state, "
    "and use tasks only as a progress ledger. Write dependencies into node fields and verify outputs before completion."
))
def _normalize_and_validate_plan(*_: Any, **__: Any) -> tuple[None, dict[str, Any]]:
    return None, {
        "ok": False,
        "error": "legacy plan validation was removed",
        "error_kind": "legacy_plan_removed",
    }

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
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(ctx))
    return {tool["function"]["name"].replace("__", ".") for tool in tools}

def _video_plan_doc(node_types: list[str]) -> dict:
    return {
        "title": "视频计划",
        "phases": [
            {
                "phase": 1,
                "steps": [
                    {
                        "step": index,
                        "tool": "node.create",
                        "input": {"type": node_type, "fields": {}},
                    }
                    for index, node_type in enumerate(node_types, 1)
                ],
            }
        ],
    }

def _sample_materialization_blueprint() -> tuple[dict[str, Any], dict[str, Any]]:
    doc = {
        "id": "bp-materialize",
        "version": 3,
        "theme": {
            "title": "雨夜桥头",
            "duration_seconds": 15,
        },
        "production": {
            "video_mode": "grid",
            "episode_count": 1,
            "segment_seconds": 15,
        },
        "story": {
            "global_outline": "少年剑客在雨夜桥头迎战黑衣刺客。",
            "episodes": [
                {
                    "episode_id": "ep-1",
                    "episode_number": 1,
                    "title": "桥头一战",
                    "summary": "拔剑、交锋、定格。",
                    "script": {
                        "title": "桥头一战",
                        "beats": ["雨夜拔剑", "黑衣刺客突袭", "剑光定格"],
                    },
                    "segments": [
                        {
                            "segment_id": "seg-1",
                            "segment_index": 1,
                            "duration_seconds": 15,
                            "plot": "雨线中拔剑、交锋、定格。",
                            "cast_refs": ["少年剑客"],
                            "scene_refs": ["石桥"],
                        }
                    ],
                }
            ],
        },
        "characters": [
            {"character_id": "char-1", "name": "少年剑客", "role": "主角", "description": "白衣持剑"}
        ],
        "scenes": [
            {"scene_id": "scene-1", "name": "石桥", "description": "灯笼、暴雨、远山剪影"}
        ],
        "visual_strategy": {"grid": {"policy": "2x2宫格分镜。"}},
        "constraints": {"user_requirements": ["动作打斗，国风动漫，16:9"]},
    }
    index = {"id": "bp-materialize", "version": 3, "checksum": "checksum-1"}
    return doc, index

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
