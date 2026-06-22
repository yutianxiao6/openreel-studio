"""Agent Orchestrator — pure Agent Loop (Claude Code style).

Every user message enters the same loop: LLM decides whether to call tools
or respond with text. Loop continues until the LLM produces a text-only
response or hits MAX_ITERATIONS.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from app.agent.agent_trace import (
    AgentTrace,
    elapsed_ms,
    result_error_kind,
    visible_tool_names,
)
from app.agent.blueprint_revision import apply_pending_blueprint_revision
from app.agent.blueprint_tree import summarize_blueprint_for_state
from app.agent.context_policy import (
    chat_history_visible_for_turn,
)
from app.agent.collaboration_mode import (
    build_proposed_plan_doc,
    current_collaboration_mode,
    split_proposed_plan_blocks,
)
from app.agent.confirmation_protocol import (
    confirmation_expires_at,
    decision_action,
    expired_pending_confirmation_patch,
)
from app.agent.interaction_payload import (
    build_interaction_agent_message,
    is_interaction_input,
)
from app.agent.blueprint_confirmation_state import pending_blueprint_plan
from app.agent.token_usage import (
    accumulate_usage,
    build_usage_monitor_payload,
    build_usage_snapshot,
    normalize_usage_totals,
    reset_context_peak_usage,
)
from app.agent.turn_budget import (
    TurnBudgetLimits,
    TurnBudgetState,
)
from app.agent.video_intake import video_intake_state_patch_for_interaction
from app.agent.vision_context import (
    attach_vision_metadata,
    apply_vision_context_to_latest_user,
    build_vision_context_from_metadata,
    build_vision_context,
    configured_max_images,
    multimodal_content,
    vision_metadata_payload,
)
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.prompt_dump import dump_llm_request, new_run_id
from app.agent.context_compact import (
    micro_compact,
    auto_compact_needed,
    estimate_tokens,
    save_transcript,
    build_compact_summary_prompt,
    compact_messages,
    compact_preserved_tail,
    list_run_tool_result_artifacts,
)
from app.agent.tool_errors import normalize_tool_result
from app.agent.tool_output import (
    build_tool_output_envelope,
    tool_done_event,
    tool_result_context_messages,
    tool_result_message,
    tool_trace_fields,
)
from app.agent.lifecycle_hooks import (
    PermissionDenialState,
    run_before_turn,
    run_before_model_call,
    run_pre_tool_use,
    run_stop_after_text_response,
)
from app.agent.permission_policy import ToolPermissionContext, plan_mode_allowed_tools
from app.agent.reset_flow import (
    make_reset_confirm_token,
    reset_canvas_events,
    reset_confirmation_text,
    reset_project_event,
    reset_success_text,
)
from app.agent.video_mode import (
    build_video_mode_system_reminder,
)
from app.db.models import Message
from app.mcp_tools.registry import registry
from app.services.llm_service import LLMService, is_context_length_error
from app.services.node_service import NodeService
from app.services.project_service import ProjectService
from app.services.version_service import VersionService

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MAX_ITERATIONS = 200  # 上限很高，几乎不限；真正拦截靠重复错误检测
_MENTOR_GUIDE_CACHE_KEY = "_mentor_guides_loaded"
_SKILL_GUIDE_CACHE_KEY = "_skills_loaded"

_INTERNAL_NAME_RE = re.compile(
    r"\b(?:agent|asset|canvas|config|drama|file|media|memory|mcp|model|node|"
    r"plan|project|prompt|scene|shot|skill|system|task|template|tool)"
    r"\.[A-Za-z_][\w_]*\b"
)
_INTERNAL_FUNCTION_RE = re.compile(
    r"\b(?:reset_project|delete_node|clear_all_nodes|cleanup_test_nodes|"
    r"planner_make_plan|generate_[A-Za-z0-9_]+)\b"
)
_INTERNAL_PARAM_RE = re.compile(
    r"\b(?:scope|node_id|project_id|tool|function|api|API)\s*=\s*['\"][^'\"]+['\"]"
)


def _sanitize_user_visible_text(text: str) -> str:
    """Remove internal tool/function/API identifiers from user-facing text."""
    if not text:
        return ""
    cleaned = _INTERNAL_NAME_RE.sub("内部动作", text)
    cleaned = _INTERNAL_FUNCTION_RE.sub("内部动作", cleaned)
    cleaned = _INTERNAL_PARAM_RE.sub("内部参数", cleaned)
    cleaned = cleaned.replace("API 名", "内部接口名")
    cleaned = cleaned.replace("api 名", "内部接口名")
    return cleaned


def _active_blueprint_state(state: dict[str, Any]) -> dict[str, Any] | None:
    blueprint = state.get("project_blueprint") if isinstance(state, dict) else None
    if isinstance(blueprint, dict) and str(blueprint.get("status") or "") == "active":
        return blueprint
    return None


def _stale_blueprint_flow_state_patch(state: dict[str, Any]) -> dict[str, Any]:
    """Clear draft/intake state that cannot own turns after the blueprint is active."""
    if not isinstance(state, dict):
        return {}
    patch: dict[str, Any] = {}
    for key in (
        "pending_plan",
        "pending_plan_preview_checklist",
        "active_plan_checklist",
        "active_plan_id",
    ):
        if key in state and state.get(key) is not None:
            patch[key] = None
    active_blueprint = _active_blueprint_state(state)
    pending_blueprint = pending_blueprint_plan(state)
    if not active_blueprint:
        draft = state.get("pending_blueprint_draft")
        if (
            not isinstance(pending_blueprint, dict)
            and isinstance(draft, dict)
            and str(draft.get("status") or "") == "pending_review"
        ):
            for key in (
                "pending_blueprint_draft",
                "pending_blueprint_review",
                "pending_blueprint_section_review",
                "pending_video_blueprint_request",
                "blueprint_partial_plan_doc",
                "blueprint_progress",
                "blueprint_generation_progress",
                "blueprint_section_results",
                "blueprint_window_progress",
            ):
                if state.get(key) is not None:
                    patch[key] = None
            mode_defaults = {"project_mode": "single_node"}
            for key, value in mode_defaults.items():
                if state.get(key) != value:
                    patch[key] = value
            if state.get("project_sub_mode") is not None:
                patch["project_sub_mode"] = None
            return patch
        return patch

    for key in (
        "pending_video_blueprint_request",
        "pending_blueprint_draft",
        "pending_blueprint_review",
        "pending_blueprint_section_review",
        "blueprint_window_progress",
        "blueprint_partial_plan_doc",
    ):
        if state.get(key) is not None:
            patch[key] = None

    return patch


def _state_with_semantic_blueprint(project_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = {}
    summary = summarize_blueprint_for_state(project_id)
    if not summary:
        return state
    next_state = dict(state)
    next_state["semantic_blueprint"] = summary
    return next_state


def _guide_loaded_trace_payload(tool_name: str, result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if result.get("from_guide_cache") is True:
        return None
    guide_tool = str(result.get("_deferred_tool") or tool_name or "")
    if guide_tool != "skill.project_mentor":
        return None
    if result.get("error") or result.get("ok") is False:
        return None

    guidance = str(result.get("guidance") or "")
    return {
        "guide_tool": guide_tool,
        "trigger_tool": tool_name,
        "guide_name": str(result.get("topic") or "overview"),
        "chars": len(guidance),
        "references_count": _guide_references_count(result),
    }


def _guide_references_count(result: dict[str, Any]) -> int:
    raw_count = result.get("references_count")
    if isinstance(raw_count, int):
        return max(0, raw_count)
    references = result.get("references")
    if isinstance(references, list):
        return len(references)
    return 0


def _compact_cached_guide_summary(value: Any, limit: int = 520) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _cached_guide_hash(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _mentor_guide_cache_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("from_guide_cache") is True:
        return None
    topic = str(result.get("topic") or "").strip().lower()
    if not topic:
        return None
    guidance = str(result.get("guidance") or "")
    return {
        "topic": topic,
        "detail": str(result.get("detail") or "summary"),
        "has_full_guide": bool(result.get("has_full_guide")),
        "guidance_summary": _compact_cached_guide_summary(guidance),
        "guidance_hash": _cached_guide_hash(guidance),
        "guidance_chars": len(guidance),
        "references_count": _guide_references_count(result),
    }


def _skill_guide_cache_payload(tool_name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if result.get("from_guide_cache") is True or result.get("error") or result.get("ok") is False:
        return None
    resolved_tool = str(result.get("_deferred_tool") or tool_name or "").strip()
    if not resolved_tool.startswith("skill.") or resolved_tool == "skill.project_mentor":
        return None
    skill_name = str(result.get("skill") or resolved_tool.removeprefix("skill.")).strip()
    if not skill_name:
        return None
    guidance = str(result.get("guidance") or result.get("model_summary") or "")
    summary = str(result.get("model_summary") or guidance)
    guidance_hash = str(result.get("guidance_hash") or "").strip() or _cached_guide_hash(guidance)
    return {
        "skill": skill_name,
        "tool": resolved_tool,
        "path": str(result.get("skill_path") or ""),
        "detail": str(result.get("detail") or "summary"),
        "summary": _compact_cached_guide_summary(summary),
        "guidance_hash": guidance_hash,
        "guidance_chars": len(guidance),
    }


def _deferred_tool_input(tool_args: dict[str, Any]) -> dict[str, Any]:
    input_args = tool_args.get("input") if isinstance(tool_args, dict) else {}
    return input_args if isinstance(input_args, dict) else {}


def _deferred_tool_target(tool_args: dict[str, Any]) -> str:
    if not isinstance(tool_args, dict):
        return ""
    return str(tool_args.get("name") or "").strip().replace("__", ".")


def _template_lookup_state_payload(tool_args: dict[str, Any], result: Any) -> dict[str, Any] | None:
    return None


def _agent_review_state_payload(tool_args: dict[str, Any], result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    target = str(result.get("_deferred_tool") or "")
    if target != "agent.review":
        return None
    if result.get("error") or result.get("ok") is False:
        return None
    input_args = _deferred_tool_input(tool_args)
    review_result = result.get("result") if isinstance(result.get("result"), dict) else {}
    findings = review_result.get("findings") if isinstance(review_result.get("findings"), list) else []
    status = str(review_result.get("status") or "unknown")
    outcome = str(review_result.get("outcome") or status)
    schema_version = str(review_result.get("schema_version") or result.get("review_schema_version") or "")
    parse_status = str(review_result.get("parse_status") or "")
    session_status = str(review_result.get("session_status") or "")
    timed_out = bool(review_result.get("timed_out")) if "timed_out" in review_result else False
    passed = review_result.get("passed")
    safe_to_run = review_result.get("safe_to_run")
    safe_to_submit_raw = review_result.get("safe_to_submit")
    grounded_findings_count = 0
    blocking_findings_count = 0
    advisory_findings_count = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "").strip().lower()
        evidence = str(finding.get("evidence") or "").strip()
        violated = str(finding.get("violated_requirement") or "").strip()
        grounded = bool(evidence or violated)
        if severity in {"medium", "high", "blocking"} and grounded:
            grounded_findings_count += 1
        else:
            advisory_findings_count += 1
        if severity == "blocking" and grounded:
            blocking_findings_count += 1
    if isinstance(safe_to_submit_raw, bool):
        safe_to_submit = safe_to_submit_raw
    else:
        safe_to_submit = status in {"pass", "passed", "ok"} and passed is not False
        if status == "revise_required" and grounded_findings_count:
            safe_to_submit = False
    if session_status and session_status != "completed":
        safe_to_submit = False
    if parse_status and parse_status not in {"parsed", "repaired"}:
        safe_to_submit = False
    review_subject = result.get("review_subject") if isinstance(result.get("review_subject"), dict) else {}
    return {
        "tool": target,
        "schema_version": schema_version,
        "review_goal": _compact_cached_guide_summary(input_args.get("review_goal"), 240),
        "review_profile": str(input_args.get("review_profile") or ""),
        "review_skill_key": str(input_args.get("review_skill_key") or ""),
        "custom_checklist_count": len(input_args.get("custom_checklist") or [])
        if isinstance(input_args.get("custom_checklist"), list)
        else 0,
        "guide_topics": input_args.get("guide_topics")[:8]
        if isinstance(input_args.get("guide_topics"), list)
        else [],
        "focus": input_args.get("focus")[:12]
        if isinstance(input_args.get("focus"), list)
        else [],
        "status": status,
        "outcome": outcome,
        "parse_status": parse_status,
        "session_status": session_status,
        "timed_out": timed_out,
        "passed": passed if isinstance(passed, bool) else None,
        "safe_to_run": safe_to_run if isinstance(safe_to_run, bool) else None,
        "safe_to_submit": safe_to_submit,
        "findings_count": len(findings),
        "grounded_findings_count": grounded_findings_count,
        "blocking_findings_count": blocking_findings_count,
        "advisory_findings_count": advisory_findings_count,
        "review_subject": {
            "tree_version": review_subject.get("tree_version"),
            "checksum": review_subject.get("checksum"),
            "node_count": review_subject.get("node_count"),
            "media_node_count": review_subject.get("media_node_count"),
            "blueprint_status": review_subject.get("blueprint_status"),
        },
        "summary": _compact_cached_guide_summary(result.get("summary"), 240),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


async def _load_agent_settings() -> dict:
    """读 app_settings 里 agent.* 偏好；失败时返回内置默认。"""
    def _review_mode(app_settings: dict) -> str:
        explicit = str(app_settings.get("agent.blueprint_review_mode") or "").strip()
        if explicit in {"continuous_final_review", "section_review"}:
            return explicit
        legacy = str(app_settings.get("agent.video_plan_confirmation_mode", "one_shot"))
        return "section_review" if legacy == "stepwise" else "continuous_final_review"

    try:
        from app.config_store import get_store
        cfg = await get_store().get_runtime()
        blueprint_review_mode = _review_mode(cfg.app_settings)
        return {
            "max_iterations": int(cfg.app_settings.get("agent.max_iterations", MAX_ITERATIONS)),
            "tool_call_budget": int(cfg.app_settings.get("agent.tool_call_budget", 0)),
            "auto_archive": bool(cfg.app_settings.get("agent.auto_archive", True)),
            "vision_context_max_images": cfg.app_settings.get("agent.vision_context_max_images"),
            "vision_context_max_dimension": cfg.app_settings.get("agent.vision_context_max_dimension"),
            "blueprint_review_mode": blueprint_review_mode,
            "video_plan_confirmation_mode": str(
                cfg.app_settings.get("agent.video_plan_confirmation_mode", "one_shot")
            ),
        }
    except Exception:
        return {
            "max_iterations": MAX_ITERATIONS,
            "tool_call_budget": 0,
            "auto_archive": True,
            "vision_context_max_images": None,
            "vision_context_max_dimension": None,
            "blueprint_review_mode": "continuous_final_review",
            "video_plan_confirmation_mode": "one_shot",
        }

# Tools that produce a real artifact and deserve a canvas node.
# Anything outside this set (queries, list, get, save_fact, system.status, etc.)
# does NOT auto-create a node, even if its namespace is drama/media/node.

_STEP_REF_RE = re.compile(r"<由\s*step\s*(\d+)\s*产出>")


def _plan_step_node_type(step: dict[str, Any]) -> str:
    if not isinstance(step, dict) or step.get("tool") != "node.create":
        return ""
    inp = step.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    fields = inp.get("fields") if isinstance(inp.get("fields"), dict) else {}
    node_type = inp.get("type") or fields.get("type")
    return node_type if isinstance(node_type, str) else ""


def _strip_video_output_steps_from_plan_doc(plan_doc: dict[str, Any]) -> dict[str, Any]:
    """Keep visual-preproduction plans in text/image space."""
    if not isinstance(plan_doc, dict):
        return plan_doc

    def scrub_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        replacements = {
            "生成每个段落的视频提示词和视频节点。": "生成每个段落的视觉素材和说明。",
            "生成每个段落的视频提示词和视频节点": "生成每个段落的视觉素材和说明",
            "视频提示词和视频节点": "视觉素材和说明",
            "视频提示词和视频成片": "视觉素材和说明",
            "、视频提示词和视频节点": "、视觉素材说明",
            "和视频节点": "",
        }
        out = value
        for old, new in replacements.items():
            out = out.replace(old, new)
        if out == "生成视频":
            out = "生成视觉素材"
        return out

    plan_doc["video_output_disabled"] = True
    plan_doc["scope"] = plan_doc.get("scope") or "visual_preproduction"
    plan_doc["summary"] = (
        "按视觉预制作范围生成文本说明和图片素材。"
    )
    if isinstance(plan_doc.get("risks"), list):
        plan_doc["risks"] = [
            risk for risk in plan_doc["risks"]
            if "视频 provider" not in str(risk) and "视频节点" not in str(risk)
        ]
    removed_steps: set[str] = set()

    def keep_step(step: dict[str, Any]) -> bool:
        tool = str(step.get("tool") or "")
        if tool == "media.generate_video":
            if step.get("step") is not None:
                removed_steps.add(str(step.get("step")))
            return False
        if _plan_step_node_type(step) == "video":
            if step.get("step") is not None:
                removed_steps.add(str(step.get("step")))
            return False
        inp = step.get("input") or {}
        node_id = inp.get("node_id") if isinstance(inp, dict) else None
        if isinstance(node_id, str):
            match = _STEP_REF_RE.search(node_id)
            if match and match.group(1) in removed_steps:
                if step.get("step") is not None:
                    removed_steps.add(str(step.get("step")))
                return False
        return True

    for phase in plan_doc.get("phases") or []:
        if isinstance(phase, dict) and isinstance(phase.get("steps"), list):
            phase["title"] = scrub_text(phase.get("title"))
            phase["goal"] = scrub_text(phase.get("goal"))
            phase["steps"] = [
                step
                for step in phase["steps"]
                if not isinstance(step, dict) or keep_step(step)
            ]
    for section in plan_doc.get("sections") or []:
        if (
            isinstance(section, dict)
            and section.get("type") == "tool_steps"
            and isinstance(section.get("steps"), list)
        ):
            section["content"] = scrub_text(section.get("content"))
            section["steps"] = [
                step
                for step in section["steps"]
                if not isinstance(step, dict) or keep_step(step)
            ]
        elif isinstance(section, dict):
            section["content"] = scrub_text(section.get("content"))
    design_notes = plan_doc.get("design_notes")
    if isinstance(design_notes, dict):
        design_notes["content"] = scrub_text(design_notes.get("content"))
    return plan_doc

_NODE_PRODUCING_TOOLS: set[str] = {
    # Direct agent-visible ingest that still creates a canvas node.
    # Raw drama/media generators are internal runner targets behind node.run
    # and must not trigger direct canvas creation from the main agent loop.
    "drama.parse_uploaded_script",
}

_NODE_TARGET_TOOLS: set[str] = {
    # Only node.run owns lifecycle state. node.get is read-only and node.update
    # edits fields.
    "node.run",
}

# 通用画布节点类型。
_CANVAS_NODE_TYPES: set[str] = {"text", "image", "video"}

# Tools that reset/destroy project state — trigger canvas clear
_CONFIRMABLE_DESTRUCTIVE_TOOLS = {
    "canvas.delete",
}

_PENDING_RESET_CONFIRM_TOKENS = {"确认", "确定", "确认执行", "执行", "yes", "y", "confirm"}
_PENDING_RESET_CANCEL_TOKENS = {"取消", "不重置", "不要重置", "放弃", "否", "no", "n", "cancel"}

_KIND_LABEL = {
    "image": "图片",
    "script": "剧本",
    "document": "文档",
    "other": "文件",
}

# 画布事件投递支持两类 sink：
# 1) 无 project_id: 写入本地 queue，由 stream 主循环兼容路径（如旧子 agent 调用）drain。
# 2) 有 project_id: 直达项目级订阅者（/api/chat/events + 实时 stream 合并）。
import asyncio
_canvas_event_queue: asyncio.Queue = asyncio.Queue()  # 兼容旧 sub-agent 路径
_project_subscribers: dict[str, list[asyncio.Queue]] = {}


def _add_subscriber(project_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _project_subscribers.setdefault(project_id, []).append(q)
    return q


def _remove_subscriber(project_id: str, q: asyncio.Queue) -> None:
    subs = _project_subscribers.get(project_id) or []
    if q in subs:
        subs.remove(q)
    if not subs and project_id in _project_subscribers:
        del _project_subscribers[project_id]


async def emit_canvas_event(event: dict, project_id: str | None = None) -> None:
    """画布事件多路投递:
       - 若给了 project_id，优先 fan-out 到该项目所有长连订阅者。
       - 若未给 project_id，塞入本地 queue，给主 stream 的历史兼容路径 drain。
    """
    if project_id is None:
        await _canvas_event_queue.put(event)
    else:
        # 带 project_id 的事件应走 /chat/events 订阅路径，避免与主循环旧 drain 重复。
        pass
    if project_id:
        for q in list(_project_subscribers.get(project_id) or []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 慢消费端不阻塞投递


# ── Helpers ──────────────────────────────────────────────────────────────

def _format_size(size: int | None) -> str:
    if not isinstance(size, int) or size <= 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _coerce_types(kwargs: dict, params) -> dict:
    """Auto-fix LLM parameter type mismatches.

    Many models (DeepSeek, GLM, etc.) pass dict/list params as JSON strings.
    This inspects the function signature and converts string→dict/list where needed.
    """
    import inspect
    import types
    import typing

    result = dict(kwargs)
    for name, value in result.items():
        if name not in params:
            continue
        param = params[name]
        ann = param.annotation
        if ann is inspect.Parameter.empty:
            continue
        if isinstance(ann, str):
            ann_text = ann.replace("typing.", "").replace(" ", "")
            if isinstance(value, str) and (
                ann_text == "dict"
                or ann_text.startswith("dict[")
                or ann_text.startswith("dict|")
                or "|dict" in ann_text
            ):
                try:
                    result[name] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
                continue
            if isinstance(value, str) and (
                ann_text == "list"
                or ann_text.startswith("list[")
                or ann_text.startswith("list|")
                or "|list" in ann_text
            ):
                try:
                    result[name] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
                continue
            if isinstance(value, str) and (ann_text == "int" or "int|" in ann_text or "|int" in ann_text):
                try:
                    result[name] = int(value)
                except ValueError:
                    pass
                continue
            if isinstance(value, str) and (ann_text == "float" or "float|" in ann_text or "|float" in ann_text):
                try:
                    result[name] = float(value)
                except ValueError:
                    pass
                continue

        # Unwrap Optional / Union types (both typing.Union and Python 3.10+ X | Y)
        origin = getattr(ann, "__origin__", None)
        args = getattr(ann, "__args__", ())
        if origin is typing.Union or isinstance(ann, types.UnionType):
            for a in args:
                if a is type(None) or a is str:
                    continue
                ann = a
                break
            origin = getattr(ann, "__origin__", None)

        # String that should be dict or list → json.loads
        if isinstance(value, str) and (ann is dict or origin is dict or ann is list or origin is list):
            try:
                result[name] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        # Int passed as string
        elif isinstance(value, str) and ann is int:
            try:
                result[name] = int(value)
            except ValueError:
                pass
        # Float passed as string
        elif isinstance(value, str) and ann is float:
            try:
                result[name] = float(value)
            except ValueError:
                pass

    return result


def _attachment_mention(attachment: dict, *, index: int, image_index: int) -> str:
    raw = (
        attachment.get("mention")
        or attachment.get("ref_label")
        or attachment.get("reference_label")
        or attachment.get("display_label")
        or ""
    )
    label = str(raw or "").strip()
    if label:
        return label if label.startswith("@") else f"@{label}"
    if attachment.get("kind") == "image":
        return f"@图{image_index}"
    return f"@附件{index}"


def _attachments_block(attachments: list[dict] | None) -> str:
    if not attachments:
        return ""
    lines = [
        "",
        "---",
        "📎 附件:",
        "这些附件来自用户本轮消息。图片注册为项目参考图时，优先调用 `reference.manage(action='ingest_attachments')`，"
        "把下面的对象按顺序放入 `attachments`；不要猜本地绝对路径。",
    ]
    image_index = 0
    for index, a in enumerate(attachments, start=1):
        if not isinstance(a, dict):
            continue
        kind = a.get("kind", "other")
        if kind == "image":
            image_index += 1
        mention = _attachment_mention(a, index=index, image_index=max(1, image_index))
        label = _KIND_LABEL.get(kind, "文件")
        filename = a.get("filename", "")
        rel_path = a.get("rel_path", "")
        size_str = _format_size(a.get("size"))
        meta = f"ref={mention}, rel_path={rel_path}"
        if size_str:
            meta += f", size={size_str}"
        lines.append(f"- {mention} [{label}] {filename} ({meta})")
        attach_obj = {
            "kind": kind,
            "mention": mention,
            "filename": filename,
            "rel_path": rel_path,
        }
        if a.get("mime_type"):
            attach_obj["mime_type"] = a.get("mime_type")
        if isinstance(a.get("size"), int):
            attach_obj["size"] = a.get("size")
        lines.append(
            "  attachment_object="
            + json.dumps(attach_obj, ensure_ascii=False, separators=(",", ":"))
        )
    return "\n".join(lines)


def _message_with_attachments(message: str, attachments: list[dict] | None) -> str:
    block = _attachments_block(attachments)
    if not block:
        return message
    return (message or "").rstrip() + "\n" + block


def _compact_context_quote(value: Any, *, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _tool_confirmation_continuation_message(
    *,
    target: str,
    source_user_message: str,
    confirm_user_message: str,
    result_text: str,
) -> str:
    source = _compact_context_quote(source_user_message)
    confirm_text = _compact_context_quote(confirm_user_message, limit=1000)
    lines = [
        "[SYSTEM] 用户刚确认了一个待确认的破坏性操作，后端已经执行该操作。",
        f"已执行操作：{target or 'unknown'}",
        f"执行结果：{result_text}",
        "继续处理确认前同一条原始用户请求里尚未完成的部分；不要重复执行已经确认完成的删除或清空操作。",
        "如果原始请求没有剩余工作，直接简短告知已完成。",
    ]
    if source:
        lines.append(f"原始用户请求：{source}")
    if confirm_text:
        lines.append(f"本轮确认消息：{confirm_text}")
    return "\n".join(lines)


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def build_base_system(
    state: dict,
    model_configs: list[dict] | None = None,
    project_id: str | None = None,
    user_message: str = "",
    attachments: list[dict] | None = None,
    canvas_summary: dict | None = None,
) -> str:
    """旧接口:返回完整 system(等价 system+history 拼接)。供向后兼容。"""
    result = await build_split_system_result(
        state, model_configs, project_id, user_message, attachments,
        canvas_summary,
    )
    system, history, runtime = result.system, result.history, result.runtime
    if history:
        system = system + "\n\n---\n\n" + history if system else history
    if runtime:
        system = system + "\n\n---\n\n" + runtime if system else runtime
    return system


async def build_split_system(
    state: dict,
    model_configs: list[dict] | None = None,
    project_id: str | None = None,
    user_message: str = "",
    attachments: list[dict] | None = None,
    canvas_summary: dict | None = None,
) -> tuple[str, str]:
    """新接口:返回 (system, history) 分层版本。

    - system: 每次 LLM 调用都重发的稳定前缀(身份/工作循环/核心规则)
    - history: 首轮注入 messages 后不再重发(详细规则/澄清/审计等)
    """
    result = await build_split_system_result(
        state,
        model_configs=model_configs,
        project_id=project_id,
        user_message=user_message,
        attachments=attachments,
        canvas_summary=canvas_summary,
    )
    return result.system, result.history


async def build_split_system_result(
    state: dict,
    model_configs: list[dict] | None = None,
    project_id: str | None = None,
    user_message: str = "",
    attachments: list[dict] | None = None,
    canvas_summary: dict | None = None,
):
    """返回 prompt 分层文本和 section/tool 诊断元数据。"""
    from .prompt_assembler import (
        PromptContext,
        PromptAssemblyResult,
        PromptSectionStat,
        derive_status_flags,
        get_split_prompt_result,
        should_require_plan,
    )

    # Memory is available through tools; it is not injected automatically.
    user_facts: list[dict] = []
    project_facts: list[dict] = []
    flags = derive_status_flags(state)

    _state_with_canvas = dict(state)
    _state_with_canvas["_canvas_summary"] = canvas_summary or {
        "total": 0, "by_type": {}, "by_status": {}
    }

    by_status = (canvas_summary or {}).get("by_status") or {}
    has_recent_failure = int(by_status.get("failed", 0) or 0) > 0
    project_mode = state.get("project_mode") if isinstance(state, dict) else None
    collaboration_mode = current_collaboration_mode(state)

    ctx = PromptContext(
        project_id=project_id,
        user_message=user_message or "",
        state=_state_with_canvas,
        attachments=attachments or [],
        model_configs=model_configs,
        user_facts=user_facts or [],
        project_facts=project_facts or [],
        has_recent_failure=has_recent_failure,
        project_mode=project_mode,
        collaboration_mode=collaboration_mode,
        **flags,
    )
    result = get_split_prompt_result(ctx)
    system = result.system
    runtime = result.runtime
    sections = list(result.sections)
    mode_state = state if isinstance(state, dict) else {}
    mode_context_active = bool(
        mode_state.get("project_mode") == "video_production"
        or mode_state.get("pending_video_blueprint_request")
    )
    mode_reminder = (
        build_video_mode_system_reminder(
            mode_state,
            video_output_disabled=False,
        )
        if mode_context_active
        else ""
    )
    if mode_reminder:
        runtime = runtime + "\n\n---\n\n" + mode_reminder if runtime else mode_reminder
        sections.append(PromptSectionStat(
            name="video_mode_system_reminder",
            trigger="state",
            tier="h",
            chars=len(mode_reminder),
            source="state",
        ))
    return PromptAssemblyResult(
        system=system,
        history=result.history,
        sections=tuple(sections),
        tool_namespaces=result.tool_namespaces,
        cache_key=result.cache_key,
        runtime=runtime,
    )


class AgentOrchestrator:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.project_service = ProjectService(db)
        self.node_service = NodeService(db)
        self.version_service = VersionService(db)
        self.llm_service = LLMService(db)

    @staticmethod
    def _clean_progress_commentary(text: str) -> str:
        import re
        cleaned = _sanitize_user_visible_text(text.strip().strip("\"'“”‘’"))
        if not cleaned:
            return ""
        # Strip tool/function names like `project.get_state`, `node.create`, etc.
        cleaned = re.sub(r'`?[a-z_]+\.[a-z_]+`?', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        cleaned = lines[0] if lines else cleaned
        for prefix in ("进度：", "说明：", "我会：", "答："):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
        return cleaned[:300]

    async def _build_live_agent_round_summary(
        self,
        iteration: int,
        content: Any,
        tools: list[str],
        user_message: str,
        planned_actions: list[str],
    ) -> dict:
        """Show the model's own thinking text as live progress.

        Extracts the text portion of the LLM response (has_text).  Falls back
        to the deterministic action summary when the model didn't write text.
        No extra LLM call — uses the text the model already generated.
        """
        # Extract text from LLM response content (string or list of blocks)
        model_text = ""
        if isinstance(content, str):
            model_text = content
        elif isinstance(content, list):
            for block in content:
                if hasattr(block, "text") and block.text:
                    model_text += block.text
                elif isinstance(block, dict) and block.get("type") == "text":
                    model_text += str(block.get("text", ""))
        model_text = self._clean_progress_commentary(model_text)
        return {
            "type": "agent_round",
            "round": iteration + 1,
            "content": model_text,
            "source": "model" if model_text else "action_summary",
            "tools": tools,
        }

    @staticmethod
    def _action_summary_for_tools(tools: list[str]) -> str:
        return ""

    @classmethod
    def _build_agent_round_summary(
        cls,
        iteration: int,
        content: Any,
        tools: list[str],
    ) -> dict:
        model_text = ""
        if isinstance(content, str):
            model_text = content
        elif isinstance(content, list):
            for block in content:
                if hasattr(block, "text") and block.text:
                    model_text += block.text
                elif isinstance(block, dict) and block.get("type") == "text":
                    model_text += str(block.get("text", ""))
        model_text = cls._clean_progress_commentary(model_text)
        return {
            "type": "agent_round",
            "round": iteration + 1,
            "content": model_text,
            "source": "model" if model_text else "action_summary",
            "tools": tools,
        }

    @staticmethod
    def _summarize_round_tool_result(tool_name: str, result: Any) -> dict:
        """Keep a compact, non-sensitive tool result for the chat timeline."""
        status = "completed"
        if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
            status = "failed"
            error = str(result.get("error") or "执行失败")
            kind = str(result.get("error_kind") or "").strip()
            hint = str(result.get("hint") or "").strip()
            parts = [error[:180]]
            if kind:
                parts.append(f"error_kind: {kind}")
            if hint:
                parts.append(f"hint: {hint[:180]}")
            return {"tool": tool_name, "status": status, "summary": "；".join(parts)}

        if isinstance(result, list):
            summary = f"返回 {len(result)} 条记录"
        elif isinstance(result, dict):
            parts: list[str] = []
            if result.get("message"):
                parts.append(str(result["message"])[:180])
            if result.get("title"):
                parts.append(f"标题: {result['title']}")
            if result.get("type"):
                parts.append(f"类型: {result['type']}")
            if result.get("status"):
                parts.append(f"状态: {result['status']}")
            node_id = result.get("node_id") or result.get("id")
            if node_id:
                parts.append(f"节点: {str(node_id)[:8]}")
            if isinstance(result.get("nodes"), list):
                parts.append(f"节点数: {len(result['nodes'])}")
            summary = "；".join(parts[:4]) or "执行完成"
        elif result is None:
            summary = "执行完成"
        else:
            summary = str(result)[:240]
        _entry: dict = {"tool": tool_name, "status": status, "summary": summary}
        # Persist change diff so it survives page refresh
        if isinstance(result, dict) and isinstance(result.get("changes"), list):
            _entry["changes"] = result["changes"]
        return _entry

    @staticmethod
    def _tool_error_user_text(result: dict[str, Any] | None) -> str:
        if not isinstance(result, dict):
            return ""
        kind = str(result.get("error_kind") or "")
        error = str(result.get("error") or "").strip()
        hint = str(result.get("hint") or "").strip()
        stop_reason = str(result.get("stop_reason") or "")
        if stop_reason == "repeated_tool_error":
            tool = str(result.get("tool") or result.get("_deferred_tool") or "unknown_tool")
            repeat_count = result.get("repeat_count") or "多"
            suggested_next = str(result.get("suggested_next") or "").strip()
            next_labels = {
                "repair_arguments": "需要修正工具参数后再试。",
                "satisfy_dependency": "需要先补齐依赖或读取对应指南。",
                "ask_or_wait_for_user": "需要等待用户确认或补充信息。",
                "read_state": "需要先重新读取项目状态。",
                "model_decides": "需要根据错误重新选择下一步。",
            }
            need = next_labels.get(suggested_next, suggested_next) or hint or "需要用户补充信息或修改请求后再试。"
            detail = error or hint or "工具连续返回同类错误。"
            return f"工具 {tool} 连续 {repeat_count} 次返回 {kind or 'tool_error'}。{detail} 下一步：{need}。"
        if kind == "empty_plan":
            return "方案提交失败：没有拿到可提交的方案内容。"
        if kind == "plan_required_before_action":
            return "当前动作被执行策略拦截：系统认为这轮需要先提交可审核方案。"
        if kind == "guide_not_loaded":
            return "节点创建前缺少创建指南，需要先读取对应节点类型的字段规范。"
        if kind == "dependency_missing":
            return "节点依赖还不完整，需要先补齐上游节点或参考素材。"
        if error:
            return error
        if hint:
            return hint
        return "内部动作没有返回可继续展示的结果。"

    @classmethod
    def _build_no_text_fallback(
        cls,
        *,
        state: dict[str, Any],
        pending_meta: dict[str, Any],
        terminal_error: dict[str, Any] | None,
        tool_errors: list[dict[str, Any]],
        step_index: int,
        project_switched: bool,
    ) -> str:
        if project_switched:
            return ""
        error_result = terminal_error or (tool_errors[-1] if tool_errors else None)
        if error_result:
            text = cls._tool_error_user_text(error_result)
            stop_reason = str(error_result.get("stop_reason") or "")
            prefix = "本轮已停止，避免继续重复无效调用。" if stop_reason else "本轮没有完成。"
            return f"{prefix}{text}"
        if pending_meta.get("proposedPlan"):
            return "计划已生成，请查看计划卡。"
        if step_index > 0:
            return "本轮工具执行已结束，请查看画布或项目面板的最新结果。"
        return "这轮没有生成可见回复，请再试一次或补充更明确的目标。"

    @classmethod
    def _extract_agent_round_history(cls, messages: list[dict]) -> list[dict]:
        """Extract completed tool-call rounds for persistence after the run."""
        rounds: list[dict] = []
        current: dict | None = None
        call_names: dict[str, str] = {}

        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list) or not tool_calls:
                    current = None
                    call_names = {}
                    continue
                tools: list[str] = []
                call_names = {}
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    fn = tool_call.get("function")
                    if not isinstance(fn, dict):
                        continue
                    name = registry.resolve_tool_name(str(fn.get("name") or ""))
                    if name:
                        tools.append(name)
                        call_names[str(tool_call.get("id") or "")] = name
                # Extract model's thinking text from LLM response
                raw_content = message.get("content")
                round_text = ""
                if isinstance(raw_content, str):
                    round_text = raw_content
                elif isinstance(raw_content, list):
                    for block in raw_content:
                        if hasattr(block, "text") and block.text:
                            round_text += block.text
                        elif isinstance(block, dict) and block.get("type") == "text":
                            round_text += str(block.get("text", ""))
                round_text = cls._clean_progress_commentary(round_text)
                event = {
                    "type": "agent_round",
                    "round": len(rounds) + 1,
                    "content": round_text,
                    "source": "model" if round_text else "action_summary",
                    "tools": tools,
                }
                current = {
                    "round": event["round"],
                    "content": event["content"],
                    "source": event["source"],
                    "tools": event["tools"],
                    "status": "completed",
                    "results": [],
                }
                rounds.append(current)
                continue
            if message.get("role") == "tool" and current is not None:
                tool_name = call_names.get(str(message.get("tool_call_id") or ""), "tool")
                result: Any = message.get("content")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        pass
                current["results"].append(cls._summarize_round_tool_result(tool_name, result))

        return rounds[-30:]

    async def _compute_canvas_summary(self, project_id: str) -> dict:
        def _input_data(node: Any) -> dict[str, Any]:
            raw = getattr(node, "input_json", None)
            if not raw:
                return {}
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _node_surface(node: Any) -> str:
            for raw in (getattr(node, "model_config_json", None), getattr(node, "input_json", None)):
                if not raw:
                    continue
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    data = {}
                if isinstance(data, dict):
                    surface = data.get("surface") or data.get("_surface")
                    if surface in {"project_panel", "draft_canvas"}:
                        return str(surface)
            return "project_panel"

        def _ensure_surface(summary: dict, surface: str) -> dict:
            details = summary.setdefault("surface_details", {})
            item = details.get(surface)
            if not isinstance(item, dict):
                item = {"total": 0, "by_type": {}, "by_status": {}}
                details[surface] = item
            return item

        try:
            _nodes = await self.node_service.list_nodes(project_id)
            summary: dict[str, Any] = {
                "total": len(_nodes),
                "by_type": {},
                "by_status": {},
                "by_surface": {},
                "node_refs": [],
                "surface_details": {
                    "project_panel": {"total": 0, "by_type": {}, "by_status": {}},
                    "draft_canvas": {"total": 0, "by_type": {}, "by_status": {}},
                },
            }
            by_type: dict[str, int] = summary["by_type"]
            by_status: dict[str, int] = summary["by_status"]
            by_surface: dict[str, int] = summary["by_surface"]
            for n in _nodes:
                input_data = _input_data(n)
                node_type = str(getattr(n, "type", "") or "unknown")
                status = str(getattr(n, "status", "") or "unknown")
                surface = _node_surface(n)
                by_type[node_type] = by_type.get(node_type, 0) + 1
                by_status[status] = by_status.get(status, 0) + 1
                by_surface[surface] = by_surface.get(surface, 0) + 1
                surface_summary = _ensure_surface(summary, surface)
                surface_summary["total"] = int(surface_summary.get("total") or 0) + 1
                surface_by_type = surface_summary.setdefault("by_type", {})
                surface_by_status = surface_summary.setdefault("by_status", {})
                surface_by_type[node_type] = surface_by_type.get(node_type, 0) + 1
                surface_by_status[status] = surface_by_status.get(status, 0) + 1
                if len(summary["node_refs"]) < 30:
                    source_paths = input_data.get("source_blueprint_paths") or input_data.get("blueprint_source_paths")
                    summary["node_refs"].append({
                        "id": str(getattr(n, "id", "") or ""),
                        "type": node_type,
                        "title": str(getattr(n, "title", "") or "")[:120],
                        "status": status,
                        "surface": surface,
                        "blueprint_node_id": input_data.get("blueprint_node_id"),
                        "source_blueprint_paths": source_paths[:4] if isinstance(source_paths, list) else [],
                    })
            return summary
        except Exception:
            logger.exception("canvas_summary query failed")
            return {
                "total": 0,
                "by_type": {},
                "by_status": {},
                "by_surface": {},
                "node_refs": [],
                "surface_details": {
                    "project_panel": {"total": 0, "by_type": {}, "by_status": {}},
                    "draft_canvas": {"total": 0, "by_type": {}, "by_status": {}},
                },
            }

    async def stream(
        self,
        project_id: str,
        message: str,
        attachments: list[dict] | None = None,
        referenced_node_ids: list[str] | None = None,
        display_message: str | None = None,
        user_metadata: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """主入口:跑一轮 → 检查 message_queue 是否有积压 → 逐条追加继续跑。

        视频制作是线性的:出图同步阻塞,LLM 等图出来才继续,不存在"后台任务"概念。
        用户在 LLM 跑期间继续输入时,每条输入保持独立 user turn boundary。
        """
        from app.agent import message_queue as mq

        cur_msg = message
        cur_attachments = attachments
        cur_referenced_node_ids = referenced_node_ids
        cur_display_message = display_message
        cur_user_metadata = user_metadata
        queued_backlog: list[dict[str, Any]] = []
        turn = 0
        final_status = "completed"
        while True:
            cancel_reason = await mq.get_cancel_reason(project_id)
            if cancel_reason:
                await mq.clear_cancel(project_id)
                yield {"type": "cancelled", "message": f"已停止当前任务：{cancel_reason}"}
                yield {"type": "text_delta", "content": _sanitize_user_visible_text(f"\n\n已停止当前任务。{cancel_reason}")}
                return

            turn += 1
            async for event in self._stream_one_turn(
                project_id,
                cur_msg,
                cur_attachments,
                referenced_node_ids=cur_referenced_node_ids,
                display_message=cur_display_message,
                user_metadata=cur_user_metadata,
            ):
                if event.get("type") == "done":
                    final_status = str(event.get("status") or final_status or "completed")
                    continue
                yield event

            # 一轮完了,看看用户在期间又发了什么
            pending = queued_backlog or await mq.pop_all(project_id)
            if pending:
                if not queued_backlog:
                    yield {
                        "type": "merged_messages",
                        "count": len(pending),
                        "mode": "sequential_turn_inputs",
                        "queued_preview": mq.queued_preview(pending),
                    }
                item = pending.pop(0)
                queued_backlog = pending
                cur_msg = str(item.get("message") or "")
                cur_attachments = item.get("attachments") or []
                cur_referenced_node_ids = item.get("referenced_node_ids") or []
                cur_display_message = item.get("display_message")
                cur_user_metadata = item.get("user_metadata")
                continue

            break
        yield {"type": "done", "status": final_status}

    async def _stream_one_turn(
        self,
        project_id: str,
        message: str,
        attachments: list[dict] | None = None,
        referenced_node_ids: list[str] | None = None,
        display_message: str | None = None,
        user_metadata: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Pure Agent Loop: LLM decides everything."""
        from app.agent import message_queue as mq

        project = await self.project_service.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project {project_id} not found"}
            return

        if hasattr(self.project_service, "get_project_state"):
            state = await self.project_service.get_project_state(project_id)
        else:
            state = json.loads(project.state_json or "{}")
        if not isinstance(state, dict):
            state = {}
        state = _state_with_semantic_blueprint(project_id, state)
        run_id = new_run_id()
        trace = AgentTrace(project_id, run_id)
        # 推送当前 task_graph 任务到前端
        try:
            from app.agent.task_graph import task_graph as _tg_init
            _init_tasks = _tg_init.list_all(project_id or None)
            _active_tasks = [t for t in _init_tasks if t.status != "completed"]
            if _active_tasks:
                yield {
                    "type": "checklist_updated",
                    "checklist": [
                        {"step_id": t.id, "title": t.subject, "tool": t.tool or "", "status": t.status,
                         "blocked_by": t.blocked_by or []}
                        for t in _active_tasks
                    ],
                }
                trace.emit(
                    "loop_transition",
                    iteration=-1,
                    transition_reason="task_checklist_emitted_turn_start",
                    task_count=len(_init_tasks),
                )
        except Exception as exc:
            logger.exception("task checklist emission at turn start failed")
        trace.emit(
            "run_start",
            transition_reason="turn_started",
            message_chars=len(message or ""),
            attachments_count=len(attachments or []),
        )
        logger.info("agent run start: project=%s run=%s", project_id, run_id)
        message_to_save = display_message or message
        message_for_agent = message
        user_message_already_saved = False
        pre_loop_assistant_text = ""

        # Clear only the legacy per-turn node guide marker. Project mentor
        # digests live in _mentor_guides_loaded and are kept across turns so the
        # model can reuse already-read guide summaries without re-querying.
        before_turn = run_before_turn(state)
        if before_turn.state_patch:
            state.update(before_turn.state_patch)
            try:
                await self.project_service.update_project_state(
                    project_id, before_turn.state_patch,
                )
            except Exception:
                logger.exception("reset guide_loaded failed")

        agent_prefs = await _load_agent_settings()
        turn_budget = TurnBudgetState(TurnBudgetLimits.from_settings(agent_prefs))
        run_token_totals = normalize_usage_totals(None)
        session_token_totals = normalize_usage_totals(state.get("agent_token_usage"))

        async def _persist_blueprint_patch(patch: dict[str, Any]) -> None:
            state.update(patch)
            try:
                await self.project_service.update_project_state(project_id, patch)
            except Exception:
                logger.exception("persist blueprint generation state failed")

        stale_blueprint_patch = _stale_blueprint_flow_state_patch(state)
        if stale_blueprint_patch:
            await _persist_blueprint_patch(stale_blueprint_patch)
            trace.emit(
                "stale_blueprint_flow_state_cleared",
                transition_reason="stale_blueprint_flow_state",
                cleared_keys=sorted(stale_blueprint_patch.keys()),
            )

        expired_confirmation_patch, expired_confirmations = expired_pending_confirmation_patch(state)
        if expired_confirmation_patch:
            await _persist_blueprint_patch(expired_confirmation_patch)
            for expired_confirmation in expired_confirmations:
                trace.emit(
                    "confirmation_expired",
                    transition_reason="pending_confirmation_expired_before_turn",
                    **expired_confirmation,
                )

        def _trace_pre_loop_branch(reason: str, **fields: Any) -> None:
            trace.emit(
                "pre_loop_branch",
                transition_reason=reason,
                branch=reason,
                **fields,
            )

        decision_inputs = (
            user_metadata.get("decisionInputs")
            if isinstance(user_metadata, dict) and isinstance(user_metadata.get("decisionInputs"), dict)
            else None
        )
        if is_interaction_input(decision_inputs):
            message_for_agent = build_interaction_agent_message(message_for_agent, decision_inputs)
            trace.emit(
                "interaction_input_payload_prepared",
                transition_reason="interaction_input_typed_payload",
                purpose=str(decision_inputs.get("purpose") or decision_inputs.get("target") or "").strip(),
                stage=str(decision_inputs.get("stage") or "").strip(),
                fields_count=len(decision_inputs.get("values") or {})
                if isinstance(decision_inputs.get("values"), dict)
                else 0,
            )
        if (
            is_interaction_input(decision_inputs)
            and str(decision_inputs.get("purpose") or decision_inputs.get("target") or "").strip()
            == "video_blueprint_intake"
        ):
            stage = str(decision_inputs.get("stage") or "").strip() or "basic"
            state_patch = video_intake_state_patch_for_interaction(
                state,
                message,
                attachments or [],
                stage,
                decision_inputs,
            )
            if state_patch:
                await _persist_blueprint_patch(state_patch)
                pending_video_request = state_patch.get("pending_video_blueprint_request")
                pending_stage = (
                    pending_video_request.get("stage")
                    if isinstance(pending_video_request, dict)
                    else stage
                )
                _trace_pre_loop_branch(
                    "video_blueprint_intake_answer_synced",
                    purpose="video_blueprint_intake",
                    stage=pending_stage,
                    submitted_stage=stage,
                    fields_count=len(decision_inputs.get("values") or {})
                    if isinstance(decision_inputs.get("values"), dict)
                    else 0,
                )

        pending_reset = state.get("_pending_reset_confirm") if isinstance(state, dict) else None
        if isinstance(pending_reset, dict):
            reset_decision, _reset_feedback = decision_action(
                user_metadata,
                "reset_project",
                "reset_project_confirmation",
            )
            short_decision_text = str(message or "").strip().lower()
            if not reset_decision and len(short_decision_text) <= 12:
                if short_decision_text in _PENDING_RESET_CONFIRM_TOKENS:
                    reset_decision = "confirm"
                elif short_decision_text in _PENDING_RESET_CANCEL_TOKENS:
                    reset_decision = "cancel"
            if reset_decision in {"cancel", "reject", "dismiss"}:
                _trace_pre_loop_branch(
                    "reset_confirmation_cancel",
                    pending_kind="reset_project",
                    scope=pending_reset.get("scope") or "full",
                )
                saved_user = _message_with_attachments(message_to_save, attachments)
                await self._save_message(project_id, "user", saved_user, user_metadata)
                await _persist_blueprint_patch({"_pending_reset_confirm": None})
                trace.emit(
                    "confirmation_resolved",
                    transition_reason="reset_confirmation_cancelled",
                    confirmation_kind="reset_project",
                    action="cancel",
                    scope=pending_reset.get("scope") or "full",
                )
                text = "已取消重置，项目内容保持不变。"
                await self._save_message(project_id, "assistant", text)
                yield {"type": "text_delta", "content": text}
                trace.emit("run_complete", transition_reason="reset_confirmation_cancelled")
                yield {"type": "done", "status": "completed"}
                return
            if reset_decision in {"apply", "approve", "confirm"}:
                _trace_pre_loop_branch(
                    "reset_confirmation_confirm",
                    pending_kind="reset_project",
                    scope=pending_reset.get("scope") or "full",
                )
                saved_user = _message_with_attachments(message_to_save, attachments)
                await self._save_message(project_id, "user", saved_user, user_metadata)
                result = await registry.call(
                    "project.reset",
                    project_id=project_id,
                    scope="full",
                    reason=pending_reset.get("reason") or "用户确认重置项目",
                    _confirm_token=make_reset_confirm_token(project_id),
                )
                await _persist_blueprint_patch({"_pending_reset_confirm": None})
                if isinstance(result, dict) and result.get("ok"):
                    trace.emit(
                        "confirmation_resolved",
                        transition_reason="reset_confirmation_confirmed",
                        confirmation_kind="reset_project",
                        action="confirm",
                        scope="full",
                        cleared_all=bool(result.get("cleared_all")),
                    )
                    for event in reset_canvas_events(result):
                        yield event
                    if result.get("title"):
                        yield {
                            "type": "project_update",
                            "project_id": project_id,
                            "updates": {"title": result.get("title")},
                        }
                    reset_text = reset_success_text(result, "full")
                    reset_event = reset_project_event(
                        project_id,
                        result,
                        scope="full",
                        message=reset_text,
                    )
                    if reset_event:
                        yield reset_event
                    await self._drop_session_cache(None)
                    await self._save_message(project_id, "assistant", reset_text)
                    yield {"type": "text_delta", "content": reset_text}
                    trace.emit("run_complete", transition_reason="reset_confirmation_confirmed")
                    yield {"type": "done", "status": "completed"}
                    return
                text = _sanitize_user_visible_text(
                    f"重置执行失败：{result.get('error', '未知错误') if isinstance(result, dict) else '未知错误'}"
                )
                await self._save_message(project_id, "assistant", text)
                yield {"type": "text_delta", "content": text}
                trace.emit(
                    "confirmation_resolved",
                    transition_reason="reset_confirmation_failed",
                    confirmation_kind="reset_project",
                    action="confirm",
                    scope="full",
                    error_kind=result_error_kind(result),
                )
                trace.emit("run_complete", transition_reason="reset_confirmation_failed")
                yield {"type": "done", "status": "failed"}
                return
            trace.emit(
                "pending_reset_confirmation_continues_agent_loop",
                transition_reason="latest_user_message_requires_model_decision",
                scope=pending_reset.get("scope") or "full",
            )

        pending_tool_confirm = state.get("_pending_tool_confirm") if isinstance(state, dict) else None
        if isinstance(pending_tool_confirm, dict):
            pending_target = str(pending_tool_confirm.get("target") or "")
            pending_tool_input = (
                pending_tool_confirm.get("input")
                if isinstance(pending_tool_confirm.get("input"), dict)
                else {}
            )
            tool_confirm_action, _tool_confirm_feedback = decision_action(
                user_metadata,
                "confirmation",
                pending_target,
                "tool_confirmation",
            )
            if tool_confirm_action in {"cancel", "reject", "dismiss"}:
                _trace_pre_loop_branch(
                    "tool_confirmation_cancel",
                    pending_kind="tool_confirmation",
                    target=pending_target,
                )
                saved_user = _message_with_attachments(message_to_save, attachments)
                await self._save_message(project_id, "user", saved_user, user_metadata)
                await _persist_blueprint_patch({"_pending_tool_confirm": None})
                trace.emit(
                    "confirmation_resolved",
                    transition_reason="tool_confirmation_cancelled",
                    confirmation_kind="tool_confirmation",
                    action="cancel",
                    target=pending_target,
                )
                text = "已取消该待确认操作。"
                await self._save_message(project_id, "assistant", text)
                yield {"type": "text_delta", "content": text}
                trace.emit("run_complete", transition_reason="tool_confirmation_cancelled")
                yield {"type": "done", "status": "completed"}
                return
            if tool_confirm_action in {"apply", "approve", "confirm"}:
                _trace_pre_loop_branch(
                    "tool_confirmation_confirm",
                    pending_kind="tool_confirmation",
                    target=pending_target,
                )
                saved_user = _message_with_attachments(message_to_save, attachments)
                await self._save_message(project_id, "user", saved_user, user_metadata)
                if pending_target not in _CONFIRMABLE_DESTRUCTIVE_TOOLS:
                    await _persist_blueprint_patch({"_pending_tool_confirm": None})
                    text = "待确认操作已经失效，请重新发起。"
                    await self._save_message(project_id, "assistant", text)
                    yield {"type": "text_delta", "content": text}
                    trace.emit(
                        "confirmation_resolved",
                        transition_reason="tool_confirmation_unsupported_target",
                        confirmation_kind="tool_confirmation",
                        action="confirm",
                        target=pending_target,
                        error_kind="unsupported_tool_confirmation_target",
                    )
                    trace.emit("run_complete", transition_reason="tool_confirmation_unsupported_target")
                    yield {"type": "done", "status": "failed"}
                    return
                pending_delete_scope = str(pending_tool_input.get("scope") or "selected").strip().lower()
                pending_delete_ids = pending_tool_input.get("node_ids")
                if isinstance(pending_delete_ids, str):
                    pending_delete_ids = [pending_delete_ids]
                if (
                    pending_target == "canvas.delete"
                    and pending_delete_scope not in {"all", "canvas", "clear_all"}
                    and not [str(item).strip() for item in (pending_delete_ids or []) if str(item).strip()]
                ):
                    await _persist_blueprint_patch({"_pending_tool_confirm": None})
                    text = "待确认操作缺少目标节点，请重新发起。"
                    await self._save_message(project_id, "assistant", text)
                    yield {"type": "text_delta", "content": text}
                    trace.emit(
                        "confirmation_resolved",
                        transition_reason="tool_confirmation_missing_input",
                        confirmation_kind="tool_confirmation",
                        action="confirm",
                        target=pending_target,
                        error_kind="missing_tool_confirmation_input",
                    )
                    trace.emit("run_complete", transition_reason="tool_confirmation_missing_input")
                    yield {"type": "done", "status": "failed"}
                    return
                target_spec = registry.get("canvas.delete")
                call_input = dict(pending_tool_input)
                call_input["project_id"] = project_id
                target_kwargs = (
                    self._filter_kwargs(target_spec.handler, call_input)
                    if target_spec
                    else call_input
                )
                result = await registry.call("canvas.delete", **target_kwargs)
                await _persist_blueprint_patch({"_pending_tool_confirm": None})
                if isinstance(result, dict) and result.get("ok"):
                    trace.emit(
                        "confirmation_resolved",
                        transition_reason="tool_confirmation_confirmed",
                        confirmation_kind="tool_confirmation",
                        action="confirm",
                        target=pending_target,
                        deleted_nodes=result.get("deleted_nodes"),
                        deleted_node_ids=result.get("deleted_node_ids"),
                        node_id=(pending_delete_ids or [None])[0] if isinstance(pending_delete_ids, list) else None,
                    )
                    if str(result.get("scope") or pending_delete_scope) == "all":
                        yield {"type": "canvas_action", "action": "clear_all", "payload": {}}
                        text = f"已清空画布（{int(result.get('deleted_nodes') or 0)} 个节点）。"
                    else:
                        deleted_ids = result.get("deleted_node_ids") or []
                        if deleted_ids:
                            for nid in deleted_ids:
                                if nid:
                                    yield {"type": "canvas_action", "action": "delete_node", "payload": {"id": nid}}
                        elif result.get("id"):
                            yield {"type": "canvas_action", "action": "delete_node", "payload": {"id": result["id"]}}
                        await self._drop_session_cache(None)
                        text = "已删除节点。"
                    yield {"type": "text_delta", "content": text}
                    source_user_message = str(pending_tool_confirm.get("source_user_message") or "").strip()
                    if source_user_message:
                        message_for_agent = _tool_confirmation_continuation_message(
                            target=pending_target,
                            source_user_message=source_user_message,
                            confirm_user_message=message_to_save,
                            result_text=text,
                        )
                        pre_loop_assistant_text += text
                        user_message_already_saved = True
                        trace.emit(
                            "tool_confirmation_continuation_started",
                            transition_reason="tool_confirmation_confirmed_continue_agent_loop",
                            confirmation_kind="tool_confirmation",
                            action="confirm",
                            target=pending_target,
                            source_user_message_chars=len(source_user_message),
                        )
                    else:
                        await self._save_message(project_id, "assistant", text)
                        trace.emit(
                            "run_complete",
                            transition_reason="tool_confirmation_confirmed",
                            confirmation_kind="tool_confirmation",
                            action="confirm",
                            target=pending_target,
                        )
                        yield {"type": "done", "status": "completed"}
                        return
                else:
                    text = _sanitize_user_visible_text(
                        f"确认操作执行失败：{result.get('error', '未知错误') if isinstance(result, dict) else '未知错误'}"
                    )
                    await self._save_message(project_id, "assistant", text)
                    yield {"type": "text_delta", "content": text}
                    trace.emit(
                        "confirmation_resolved",
                        transition_reason="tool_confirmation_failed",
                        confirmation_kind="tool_confirmation",
                        action="confirm",
                        target=pending_target,
                        error_kind=result_error_kind(result),
                    )
                    trace.emit("run_complete", transition_reason="tool_confirmation_failed")
                    yield {"type": "done", "status": "failed"}
                    return
            trace.emit(
                "pending_tool_confirmation_continues_agent_loop",
                transition_reason="latest_user_message_requires_model_decision",
                target=pending_target,
            )

        pending_revision = state.get("pending_blueprint_revision") if isinstance(state, dict) else None
        if isinstance(pending_revision, dict):
            revision_action, _revision_feedback = decision_action(
                user_metadata,
                "blueprint_revision",
            )
            if revision_action in {"cancel", "reject", "dismiss"}:
                _trace_pre_loop_branch(
                    "blueprint_revision_cancel",
                    pending_kind="blueprint_revision",
                    target_node_id=pending_revision.get("target_node_id"),
                )
                saved_user = _message_with_attachments(message_to_save, attachments)
                await self._save_message(project_id, "user", saved_user, user_metadata)
                await _persist_blueprint_patch({"pending_blueprint_revision": None})
                trace.emit(
                    "confirmation_resolved",
                    transition_reason="blueprint_revision_cancelled",
                    confirmation_kind="blueprint_revision",
                    action="cancel",
                    target_node_id=pending_revision.get("target_node_id"),
                )
                text = "已取消蓝图修订，当前蓝图保持不变。"
                await self._save_message(project_id, "assistant", text)
                yield {"type": "text_delta", "content": text}
                trace.emit("run_complete", transition_reason="blueprint_revision_cancelled")
                yield {"type": "done", "status": "completed"}
                return
            if revision_action in {"apply", "approve", "confirm"}:
                _trace_pre_loop_branch(
                    "blueprint_revision_confirm",
                    pending_kind="blueprint_revision",
                    target_node_id=pending_revision.get("target_node_id"),
                )
                saved_user = _message_with_attachments(message_to_save, attachments)
                await self._save_message(project_id, "user", saved_user, user_metadata)
                result = await apply_pending_blueprint_revision(project_id)
                if isinstance(result, dict) and result.get("ok") and isinstance(result.get("blueprint"), dict):
                    blueprint_index = result.get("blueprint")
                    stale_nodes = result.get("stale_nodes") if isinstance(result.get("stale_nodes"), list) else []
                    rematerialized = (
                        result.get("rematerialized_node_ids")
                        if isinstance(result.get("rematerialized_node_ids"), list)
                        else []
                    )
                    affected_source_paths = (
                        pending_revision.get("applied_source_paths")
                        or pending_revision.get("affected_source_paths")
                        or []
                    )
                    state["project_blueprint"] = blueprint_index
                    state["pending_blueprint_revision"] = None
                    trace.emit(
                        "blueprint_revision_applied",
                        transition_reason="blueprint_revision_confirmed",
                        revision_version=blueprint_index.get("version"),
                        target_node_id=pending_revision.get("target_node_id"),
                        affected_source_paths=affected_source_paths,
                        rematerialized_node_ids=rematerialized,
                        stale_node_count=len(stale_nodes),
                        stale_nodes=stale_nodes,
                    )
                    trace.emit(
                        "confirmation_resolved",
                        transition_reason="blueprint_revision_confirmed",
                        confirmation_kind="blueprint_revision",
                        action="confirm",
                        target_node_id=pending_revision.get("target_node_id"),
                        affected_source_paths=affected_source_paths,
                        rematerialized_node_ids=rematerialized,
                        stale_node_count=len(stale_nodes),
                        stale_nodes=stale_nodes,
                    )
                    yield {
                        "type": "blueprint_revision_applied",
                        "project_id": project_id,
                        "blueprint": blueprint_index,
                        "view_model": result.get("view_model"),
                        "rematerialized_node_ids": rematerialized,
                        "stale_nodes": stale_nodes,
                        "auto_applied": False,
                    }
                    if blueprint_index.get("theme_title"):
                        yield {
                            "type": "project_update",
                            "project_id": project_id,
                            "updates": {"title": blueprint_index.get("theme_title")},
                        }
                    text = _sanitize_user_visible_text(
                        f"蓝图修订已应用，已重物化 {len(rematerialized)} 个目标剧情节点，"
                        f"标记 {len(stale_nodes)} 个下游节点需要同步。"
                    )
                    await self._save_message(project_id, "assistant", text)
                    yield {"type": "text_delta", "content": text}
                    trace.emit("run_complete", transition_reason="blueprint_revision_confirmed")
                    yield {"type": "done", "status": "completed"}
                    return
                text = _sanitize_user_visible_text(
                    f"蓝图修订应用失败：{result.get('error', '未知错误') if isinstance(result, dict) else '未知错误'}"
                )
                await self._save_message(project_id, "assistant", text)
                yield {"type": "text_delta", "content": text}
                trace.emit(
                    "confirmation_resolved",
                    transition_reason="blueprint_revision_failed",
                    confirmation_kind="blueprint_revision",
                    action="confirm",
                    target_node_id=pending_revision.get("target_node_id"),
                    error_kind=result_error_kind(result),
                )
                trace.emit("run_complete", transition_reason="blueprint_revision_failed")
                yield {"type": "done", "status": "failed"}
                return
            trace.emit(
                "pending_blueprint_revision_continues_agent_loop",
                transition_reason="latest_user_message_requires_model_decision",
                target_node_id=pending_revision.get("target_node_id"),
            )

        # ── Stale blueprint state cleanup ──
        for _k in ["pending_blueprint_revision", "pending_blueprint_section_review"]:
            if isinstance(state.get(_k), dict):
                pass  # Keep — model handles via tools

        # Append attachments to the persisted user message and the normalized
        # model-facing message separately. Confirmation continuations may
        # replace the model-facing message, while chat history keeps the user's
        # actual latest reply.
        block = _attachments_block(attachments)
        saved_user_message = _message_with_attachments(message_to_save, attachments)
        agent_message = message_for_agent
        if block:
            agent_message = (agent_message or "").rstrip() + "\n" + block
        message = agent_message
        vision_context = await build_vision_context(
            getattr(self, "db", None),
            project_id,
            message,
            attachments or [],
            referenced_node_ids=referenced_node_ids or [],
            max_images=agent_prefs.get("vision_context_max_images"),
            max_dimension=agent_prefs.get("vision_context_max_dimension"),
        )
        user_vision_metadata = vision_metadata_payload(
            vision_context,
            source="user_message",
        )
        if not user_message_already_saved:
            user_metadata = attach_vision_metadata(user_metadata, user_vision_metadata)
        if not user_message_already_saved:
            await self._save_message(project_id, "user", saved_user_message, user_metadata)

        # Build context. Prompt sections are state-gated; business decisions stay
        # in the Agent Loop.

        canvas_summary = await self._compute_canvas_summary(project_id)
        logger.info(
            "canvas_summary for %s: total=%d types=%s",
            project_id,
            canvas_summary["total"],
            list(canvas_summary["by_type"].keys()),
        )

        async def _rebuild_system_result(_state: dict, _summary: dict):
            _state = _state_with_semantic_blueprint(project_id, _state)
            return await build_split_system_result(
                _state,
                project_id=project_id,
                user_message=message,
                attachments=attachments,
                canvas_summary=_summary,
            )

        prompt_assembly = await _rebuild_system_result(state, canvas_summary)
        system, history_inject = prompt_assembly.system, prompt_assembly.history
        runtime_inject = prompt_assembly.runtime
        prompt_assembly_diag = prompt_assembly.diagnostics()
        history_visible = chat_history_visible_for_turn(state)
        messages = await self._call_build_messages(
            project_id,
            message,
            include_history=history_visible,
            current_message_aliases=[saved_user_message] if saved_user_message != message else None,
            max_images=agent_prefs.get("vision_context_max_images"),
            max_dimension=agent_prefs.get("vision_context_max_dimension"),
        )
        apply_vision_context_to_latest_user(messages, message, vision_context)
        trace.emit(
            "chat_history_context",
            transition_reason="history_context_built",
            history_visible=history_visible,
            isolation_reason="pending_state" if not history_visible else None,
            history_message_count=len(messages),
            estimated_history_tokens=estimate_tokens(messages),
        )
        trace.emit(
            "vision_context",
            iteration=0,
            transition_reason="vision_context_built",
            **vision_context.trace_payload(),
        )

        # 首轮 history 注入：把详细规则塞进 messages，后续 iter 不重发。
        if history_inject:
            messages.insert(0, {
                "role": "user",
                "content": f"<system-reminder>\n{history_inject}\n</system-reminder>",
            })
            messages.insert(1, {
                "role": "assistant",
                "content": "明白。我会按这些规则工作。",
            })

        from .prompt_assembler import (
            PromptContext,
            derive_status_flags,
            select_tool_namespaces,
            should_require_plan,
        )
        _ctx = PromptContext(
            project_id=project_id,
            user_message=message or "",
            state=state,
            attachments=attachments or [],
            collaboration_mode=current_collaboration_mode(state),
            **derive_status_flags(state),
        )
        tools = registry.get_tools_for_agent_loop(
            namespaces=select_tool_namespaces(_ctx)
        )
        if _ctx.collaboration_mode == "plan":
            allowed_tools = plan_mode_allowed_tools()
            tools = [
                tool
                for tool in tools
                if str((tool.get("function") or {}).get("name") or "").replace("__", ".") in allowed_tools
            ]
        _requires_plan = should_require_plan(_ctx)
        _visible_tool_names = visible_tool_names(tools)
        trace.emit(
            "prompt_assembly",
            iteration=0,
            transition_reason="before_model_call",
            cache_key=prompt_assembly_diag.get("cache_key"),
            section_count=prompt_assembly_diag.get("section_count"),
            sections_by_tier=prompt_assembly_diag.get("sections_by_tier"),
            sections_by_trigger=prompt_assembly_diag.get("sections_by_trigger"),
            sections=prompt_assembly_diag.get("sections"),
            system_chars=prompt_assembly_diag.get("system_chars"),
            history_chars=prompt_assembly_diag.get("history_chars"),
            tool_namespaces=prompt_assembly_diag.get("tool_namespaces"),
            tools_count=len(_visible_tool_names),
        )
        trace.emit(
            "agent_loop_ready",
            iteration=0,
            transition_reason="agent_loop_ready",
            visible_tools=_visible_tool_names,
            requires_plan=_requires_plan,
            project_mode=state.get("project_mode"),
            prompt_assembly=prompt_assembly_diag,
        )

        full_response = pre_loop_assistant_text
        _pending_meta: dict = {}  # accumulate plan/nodes data during loop → save with assistant msg
        tool_vision_contexts: list[dict[str, Any]] = []
        step_index = 0
        project_switched = False
        stop_after_tool = False
        full_reset_completed = False
        audit_triggered = False  # 退出 loop 前最多注入一次收尾自检 reminder
        interrupted_by_new_message = False
        permission_denial_state = PermissionDenialState()
        tool_errors: list[dict[str, Any]] = []
        tool_error_counts: dict[tuple[str, str], int] = {}
        terminal_loop_error: dict[str, Any] | None = None
        max_iter = agent_prefs["max_iterations"]
        EXTRACT_EVERY = 10  # 每 N 个 iteration 周期抽取一次关键事实
        for iteration in range(max_iter):
            # 每个 iteration 开始前检查用户是否追加了新消息
            # 只查不取 — 让外层 stream() 的 pop_all + merge 处理，保证上下文不丢
            if iteration > 0:
                from app.agent import message_queue as _mq
                if await _mq.peek_count(project_id) > 0:
                    trace.emit(
                        "loop_transition",
                        iteration=iteration,
                        transition_reason="new_message_arrived_mid_loop",
                    )
                    yield {"type": "new_message_pending", "project_id": project_id}
                    interrupted_by_new_message = True
                    stop_after_tool = True
                    break

            trace.emit(
                "iteration_start",
                iteration=iteration,
                transition_reason="loop_iteration",
                visible_tools=_visible_tool_names,
                requires_plan=_requires_plan,
                prompt_assembly=prompt_assembly_diag,
            )
            model_budget = turn_budget.before_model_call(state)
            if not model_budget.allowed:
                terminal_loop_error = model_budget.to_tool_result()
                trace.emit(
                    "loop_transition",
                    iteration=iteration,
                    transition_reason=model_budget.reason,
                    error_kind=terminal_loop_error.get("error_kind"),
                    phase=model_budget.phase,
                    count=model_budget.count,
                    limit=model_budget.limit,
                )
                break
            # Context compression
            micro_compact(messages)
            if auto_compact_needed(messages):
                compact_tokens_before = estimate_tokens(messages)
                transcript_path = save_transcript(messages, project_id)
                tool_result_artifacts = list_run_tool_result_artifacts(
                    project_id=project_id,
                    run_id=run_id,
                )
                trace.emit(
                    "loop_transition",
                    iteration=iteration,
                    transition_reason="auto_compact_started",
                    estimated_tokens_before=compact_tokens_before,
                    transcript_path=str(transcript_path),
                )
                # P0-② 压缩前先抽 fact 落库,避免细节丢失
                try:
                    from app.mcp_tools.memory_tools import memory_summarize_conversation
                    await memory_summarize_conversation(project_id, messages[-30:])
                except Exception:
                    logger.exception("pre-compact fact extraction failed")

                summary_prompt = build_compact_summary_prompt(messages)
                preserved_tail = compact_preserved_tail(
                    messages,
                    exclude_latest_user_content=message,
                )
                try:
                    summary_result = await self.llm_service.generate(
                        task_type="agent_loop",
                        messages=[{"role": "user", "content": summary_prompt}],
                        system="You are a conversation summarizer. Be concise.",
                    )
                    summary_usage = summary_result.get("usage")
                    if isinstance(summary_usage, dict):
                        run_token_totals = accumulate_usage(
                            run_token_totals,
                            summary_usage,
                            track_context_peak=False,
                        )
                        session_token_totals = accumulate_usage(
                            session_token_totals,
                            summary_usage,
                            track_context_peak=False,
                        )
                        usage_payload = build_usage_monitor_payload(
                            summary_usage,
                            run_token_totals,
                            session_token_totals,
                        )
                        trace.emit(
                            "llm_usage",
                            iteration=iteration,
                            transition_reason="auto_compact_usage",
                            **usage_payload,
                        )
                        state["agent_token_usage"] = session_token_totals
                        try:
                            await self.project_service.update_project_state(
                                project_id,
                                {"agent_token_usage": session_token_totals},
                            )
                        except Exception:
                            logger.exception("failed to persist auto compact token usage")
                        yield {
                            "type": "token_usage",
                            "project_id": project_id,
                            "run_id": run_id,
                            "round": iteration + 1,
                            "phase": "auto_compact",
                            **usage_payload,
                        }
                    messages[:] = compact_messages(
                        summary_result.get("content", ""),
                        preserved_tail=preserved_tail,
                    )
                    run_token_totals = reset_context_peak_usage(run_token_totals)
                    session_token_totals = reset_context_peak_usage(session_token_totals)
                    state["agent_token_usage"] = session_token_totals
                    try:
                        await self.project_service.update_project_state(
                            project_id,
                            {"agent_token_usage": session_token_totals},
                        )
                    except Exception:
                        logger.exception("failed to persist auto compact context peak reset")
                    # Compact 抹掉了首轮注入的 <system-reminder>,重新注入一遍
                    if history_inject:
                        messages.insert(0, {
                            "role": "user",
                            "content": f"<system-reminder>\n{history_inject}\n</system-reminder>",
                        })
                        messages.insert(1, {
                            "role": "assistant",
                            "content": "明白。我会按这些规则工作。",
                        })
                    messages.append({"role": "user", "content": message})
                    apply_vision_context_to_latest_user(messages, message, vision_context)
                    trace.emit(
                        "loop_transition",
                        iteration=iteration,
                        transition_reason="auto_compact_completed",
                        compacted_message_count=len(messages),
                        preserved_tail_count=len(preserved_tail),
                    )
                    trace.emit(
                        "compact_boundary",
                        iteration=iteration,
                        transition_reason="auto_compact_completed",
                        compact_kind="auto",
                        estimated_tokens_before=compact_tokens_before,
                        transcript_path=str(transcript_path),
                        compacted_message_count=len(messages),
                        preserved_tail_count=len(preserved_tail),
                        tool_result_files=tool_result_artifacts,
                    )
                except Exception:
                    trace.emit(
                        "loop_transition",
                        iteration=iteration,
                        transition_reason="auto_compact_failed",
                        error_kind="auto_compact_failed",
                    )

            # P1-④ 周期抽取:每 EXTRACT_EVERY 步把最近对话抽成 fact(后台跑不阻塞)
            if iteration > 0 and iteration % EXTRACT_EVERY == 0 and len(messages) >= 6:
                try:
                    from app.mcp_tools.memory_tools import memory_summarize_conversation
                    import asyncio as _aio
                    _aio.create_task(memory_summarize_conversation(project_id, messages[-12:]))
                except Exception:
                    pass

            # 出图已改同步阻塞,不存在后台任务通知 — drain 逻辑已删除

            # 每轮 LLM 前注入任务账本 reminder。它来自 task_graph，不是旧 pending plan。
            checklist_reminder = self._build_checklist_reminder(
                state,
                canvas_summary,
                require_plan=_requires_plan,
                project_id=project_id,
            )
            before_model_call = run_before_model_call(
                messages,
                checklist_reminder,
                runtime_context=runtime_inject,
            )
            messages = before_model_call.messages

            # 落盘:本次 iteration 发给模型的完整 prompt(排查用)
            dump_llm_request(
                project_id=project_id,
                run_id=run_id,
                iteration=iteration,
                system=system,
                messages=messages,
                tools=tools,
                user_message=message if iteration == 0 else None,
                prompt_assembly=prompt_assembly_diag,
            )

            # LLM call with tools
            cancel_reason = await mq.get_cancel_reason(project_id)
            if cancel_reason:
                await mq.clear_cancel(project_id)
                trace.emit(
                    "run_cancelled",
                    iteration=iteration,
                    transition_reason="cancel_before_llm",
                )
                yield {"type": "cancelled", "message": f"已停止当前任务：{cancel_reason}"}
                yield {"type": "text_delta", "content": _sanitize_user_visible_text(f"\n\n已停止当前任务。{cancel_reason}")}
                return

            progress_system = system
            llm_started_at = time.perf_counter()
            try:
                response = await self.llm_service.generate_with_tools(
                    task_type="agent_loop",
                    messages=messages,
                    tools=tools,
                    system=progress_system,
                    project_id=project_id,
                )
            except Exception as exc:
                if is_context_length_error(exc):
                    trace.emit(
                        "loop_transition",
                        iteration=iteration,
                        transition_reason="reactive_compact_started",
                        duration_ms=elapsed_ms(llm_started_at),
                        error_kind=exc.__class__.__name__,
                    )
                    try:
                        compact_tokens_before = estimate_tokens(messages)
                        transcript_path = save_transcript(messages, project_id)
                        tool_result_artifacts = list_run_tool_result_artifacts(
                            project_id=project_id,
                            run_id=run_id,
                        )
                        trace.emit(
                            "loop_transition",
                            iteration=iteration,
                            transition_reason="reactive_compact_transcript_saved",
                            estimated_tokens_before=compact_tokens_before,
                            transcript_path=str(transcript_path),
                        )
                        summary_prompt = build_compact_summary_prompt(messages)
                        preserved_tail = compact_preserved_tail(
                            messages,
                            exclude_latest_user_content=message,
                        )
                        summary_result = await self.llm_service.generate(
                            task_type="agent_loop",
                            messages=[{"role": "user", "content": summary_prompt}],
                            system="You are a conversation summarizer. Be concise.",
                        )
                        summary_usage = summary_result.get("usage")
                        if isinstance(summary_usage, dict):
                            run_token_totals = accumulate_usage(
                                run_token_totals,
                                summary_usage,
                                track_context_peak=False,
                            )
                            session_token_totals = accumulate_usage(
                                session_token_totals,
                                summary_usage,
                                track_context_peak=False,
                            )
                            usage_payload = build_usage_monitor_payload(
                                summary_usage,
                                run_token_totals,
                                session_token_totals,
                            )
                            trace.emit(
                                "llm_usage",
                                iteration=iteration,
                                transition_reason="reactive_compact_usage",
                                **usage_payload,
                            )
                            state["agent_token_usage"] = session_token_totals
                            try:
                                await self.project_service.update_project_state(
                                    project_id,
                                    {"agent_token_usage": session_token_totals},
                                )
                            except Exception:
                                logger.exception("failed to persist reactive compact token usage")
                            yield {
                                "type": "token_usage",
                                "project_id": project_id,
                                "run_id": run_id,
                                "round": iteration + 1,
                                "phase": "reactive_compact",
                                **usage_payload,
                            }
                        messages[:] = compact_messages(
                            summary_result.get("content", ""),
                            preserved_tail=preserved_tail,
                        )
                        run_token_totals = reset_context_peak_usage(run_token_totals)
                        session_token_totals = reset_context_peak_usage(session_token_totals)
                        state["agent_token_usage"] = session_token_totals
                        try:
                            await self.project_service.update_project_state(
                                project_id,
                                {"agent_token_usage": session_token_totals},
                            )
                        except Exception:
                            logger.exception("failed to persist reactive compact context peak reset")
                        if history_inject:
                            messages.insert(0, {
                                "role": "user",
                                "content": f"<system-reminder>\n{history_inject}\n</system-reminder>",
                            })
                            messages.insert(1, {
                                "role": "assistant",
                                "content": "明白。我会按这些规则工作。",
                            })
                        messages.append({"role": "user", "content": message})
                        apply_vision_context_to_latest_user(messages, message, vision_context)
                        before_model_call = run_before_model_call(
                            messages,
                            checklist_reminder,
                            runtime_context=runtime_inject,
                        )
                        messages = before_model_call.messages
                        llm_started_at = time.perf_counter()
                        response = await self.llm_service.generate_with_tools(
                            task_type="agent_loop",
                            messages=messages,
                            tools=tools,
                            system=progress_system,
                            project_id=project_id,
                        )
                        trace.emit(
                            "loop_transition",
                            iteration=iteration,
                            transition_reason="reactive_compact_retry_succeeded",
                            duration_ms=elapsed_ms(llm_started_at),
                            compacted_message_count=len(messages),
                            preserved_tail_count=len(preserved_tail),
                        )
                        trace.emit(
                            "compact_boundary",
                            iteration=iteration,
                            transition_reason="reactive_compact_retry_succeeded",
                            compact_kind="reactive",
                            estimated_tokens_before=compact_tokens_before,
                            transcript_path=str(transcript_path),
                            compacted_message_count=len(messages),
                            preserved_tail_count=len(preserved_tail),
                            tool_result_files=tool_result_artifacts,
                        )
                    except Exception as retry_exc:
                        logger.exception("LLM reactive compact retry failed")
                        trace.emit(
                            "llm_error",
                            iteration=iteration,
                            transition_reason="reactive_compact_retry_failed",
                            duration_ms=elapsed_ms(llm_started_at),
                            error_kind=retry_exc.__class__.__name__,
                        )
                        trace.emit("run_complete", transition_reason="llm_failed_after_reactive_compact")
                        error_text = f"LLM 上下文压缩重试失败: {retry_exc}"
                        try:
                            await self._save_message(project_id, "assistant", f"错误：{error_text}")
                        except Exception:
                            logger.exception("failed to persist LLM reactive compact error message")
                        yield {
                            "type": "error",
                            "message": error_text,
                            "error_kind": "reactive_compact_retry_failed",
                            "hint": "压缩后的模型重试仍失败，本轮已停止，避免重复提交同一超长上下文。",
                        }
                        return
                else:
                    logger.exception("LLM call failed")
                    trace.emit(
                        "llm_error",
                        iteration=iteration,
                        transition_reason="llm_call_failed",
                        duration_ms=elapsed_ms(llm_started_at),
                        error_kind=exc.__class__.__name__,
                    )
                    trace.emit("run_complete", transition_reason="llm_failed")
                    error_text = f"LLM 调用失败: {exc}"
                    try:
                        await self._save_message(project_id, "assistant", f"错误：{error_text}")
                    except Exception:
                        logger.exception("failed to persist LLM error message")
                    yield {"type": "error", "message": error_text}
                    return

            choice = response.choices[0]
            msg = choice.message
            usage_snapshot = build_usage_snapshot(
                response,
                messages=messages,
                system=progress_system,
                tools=tools,
            )
            run_token_totals = accumulate_usage(run_token_totals, usage_snapshot)
            session_token_totals = accumulate_usage(session_token_totals, usage_snapshot)
            usage_payload = build_usage_monitor_payload(
                usage_snapshot,
                run_token_totals,
                session_token_totals,
            )
            trace.emit(
                "llm_response",
                iteration=iteration,
                transition_reason="tool_calls" if msg.tool_calls else "text_response",
                duration_ms=elapsed_ms(llm_started_at),
                tool_call_count=len(msg.tool_calls or []),
                has_text=bool(msg.content),
            )
            trace.emit(
                "llm_usage",
                iteration=iteration,
                transition_reason="model_response_usage",
                **usage_payload,
            )
            state["agent_token_usage"] = session_token_totals
            try:
                await self.project_service.update_project_state(
                    project_id,
                    {"agent_token_usage": session_token_totals},
                )
            except Exception:
                logger.exception("failed to persist agent token usage")
            yield {
                "type": "token_usage",
                "project_id": project_id,
                "run_id": run_id,
                "round": iteration + 1,
                "phase": "agent_loop",
                **usage_payload,
            }

            # No tool calls → output text, done(but check if audit needed first)
            if not msg.tool_calls:
                raw_text = msg.content or ""
                proposed_plan_markdown = ""
                if current_collaboration_mode(state) == "plan":
                    raw_text, proposed_plan_markdown = split_proposed_plan_blocks(raw_text)
                text = _sanitize_user_visible_text(raw_text)
                if text:
                    yield {"type": "text_delta", "content": text}
                    full_response += text
                if proposed_plan_markdown:
                    plan_doc = build_proposed_plan_doc(
                        proposed_plan_markdown,
                        source_request=message or "",
                    )
                    _pending_meta["proposedPlan"] = plan_doc
                    yield {
                        "type": "proposed_plan",
                        "project_id": project_id,
                        "plan": plan_doc,
                    }
                    if not full_response.strip():
                        full_response += proposed_plan_markdown
                # 收尾自检: 从 task_graph 读取当前任务状态
                try:
                    from app.agent.task_graph import task_graph as _tg2
                    _tasks = _tg2.list_all(project_id or None)
                    _active = [t for t in _tasks if t.status != "completed"]
                    checklist = [{"step_id": t.id, "title": t.subject, "tool": t.tool or "", "status": t.status} for t in _active]
                except Exception:
                    checklist = []
                stop_hook = run_stop_after_text_response(
                    step_index=step_index,
                    checklist=checklist,
                    audit_triggered=audit_triggered,
                    tool_errors=tool_errors,
                )
                audit_triggered = stop_hook.audit_triggered
                if stop_hook.should_run_audit:
                    messages.append({"role": "user", "content": stop_hook.audit_message})
                    trace.emit(
                        "loop_transition",
                        iteration=iteration,
                        transition_reason="audit_required",
                        pending_steps=stop_hook.pending_steps,
                        failed_steps=stop_hook.failed_steps,
                    )
                    # 进入下一轮 LLM 让它跑自检
                    continue
                trace.emit(
                    "loop_transition",
                    iteration=iteration,
                    transition_reason="text_response_done",
                )
                break

            # Has tool calls → execute each one
            messages.append(msg.model_dump())

            stop_after_tool = False
            round_tool_calls: list[tuple[Any, str, dict]] = []
            round_tools: list[str] = []
            planned_actions: list[str] = []
            for tool_call in msg.tool_calls:
                fn = tool_call.function
                tool_name = registry.resolve_tool_name(fn.name)
                try:
                    tool_args = json.loads(fn.arguments) if fn.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}
                # Always use the real project_id, never trust LLM's value
                tool_args["project_id"] = project_id
                if tool_name == "tool.execute":
                    tool_args["_state"] = state
                    tool_args["_user_message"] = message
                    tool_args["_requires_plan"] = _requires_plan
                if (
                    tool_name == "reference.manage"
                    and str(tool_args.get("action") or "").strip() == "ingest_attachments"
                    and not tool_args.get("attachments")
                    and attachments
                ):
                    tool_args["attachments"] = attachments
                round_tools.append(tool_name)
                round_tool_calls.append((tool_call, tool_name, tool_args))
                planned_actions.append(tool_name)

            trace.emit(
                "tool_calls_requested",
                iteration=iteration,
                transition_reason="assistant_requested_tools",
                tool_names=round_tools,
            )
            round_progress_event = await self._build_live_agent_round_summary(
                iteration,
                msg.content,
                round_tools,
                message,
                planned_actions,
            )
            yield round_progress_event
            round_tool_start_content = (
                str(round_progress_event.get("content") or "").strip()
                if round_progress_event.get("source") == "model"
                else ""
            )
            model_feedback_after_tool = False
            round_tool_context_messages: list[dict[str, Any]] = []

            def _append_tool_result_messages(tool_call_id: str, tool_output: dict[str, Any]) -> None:
                messages.append(tool_result_message(tool_call_id, tool_output))
                round_tool_context_messages.extend(
                    tool_result_context_messages(tool_call_id, tool_output)
                )

            def _record_tool_vision_context(result: Any) -> None:
                if not isinstance(result, dict):
                    return
                payload = result.get("_vision_context")
                if not isinstance(payload, dict):
                    return
                images = payload.get("images")
                if isinstance(images, list) and images:
                    tool_vision_contexts.append(payload)

            for tool_call, tool_name, tool_args in round_tool_calls:
                tool_started_at = time.perf_counter()
                deferred_tool_name = _deferred_tool_target(tool_args) if tool_name == "tool.execute" else ""
                cancel_reason = await mq.get_cancel_reason(project_id)
                if cancel_reason:
                    await mq.clear_cancel(project_id)
                    trace.emit(
                        "run_cancelled",
                        iteration=iteration,
                        tool_name=tool_name,
                        transition_reason="cancel_before_tool",
                    )
                    yield {"type": "cancelled", "message": f"已停止当前任务：{cancel_reason}"}
                    yield {"type": "text_delta", "content": _sanitize_user_visible_text(f"\n\n已停止当前任务。{cancel_reason}")}
                    return

                tool_budget = turn_budget.before_tool_call(tool_name, tool_args, state)
                if not tool_budget.allowed:
                    result = tool_budget.to_tool_result()
                    tool_output = build_tool_output_envelope(
                        result,
                        project_id=project_id,
                        run_id=run_id,
                        iteration=iteration,
                        tool_name=tool_name,
                    )
                    trace.emit(
                        "tool_result",
                        iteration=iteration,
                        tool_name=tool_name,
                        deferred_tool_name=deferred_tool_name,
                        transition_reason=tool_budget.reason,
                        duration_ms=elapsed_ms(tool_started_at),
                        error_kind=result_error_kind(result),
                        phase=tool_budget.phase,
                        count=tool_budget.count,
                        limit=tool_budget.limit,
                        **tool_trace_fields(tool_output),
                    )
                    yield tool_done_event(tool_name, iteration + 1, tool_output)
                    _append_tool_result_messages(tool_call.id, tool_output)
                    tool_errors.append(result)
                    terminal_loop_error = result
                    stop_after_tool = True
                    break

                pre_tool_use = run_pre_tool_use(
                    ToolPermissionContext(
                        tool_name=tool_name,
                        state=state,
                        user_message=message,
                        requires_plan=_requires_plan,
                        tool_args=tool_args,
                    ),
                    permission_denial_state,
                )
                permission_denial_state = pre_tool_use.denial_state
                if not pre_tool_use.allowed:
                    result = pre_tool_use.result or {
                        "ok": False,
                        "error": "工具调用被权限策略拒绝",
                        "error_kind": "permission_denied",
                    }
                    result = normalize_tool_result(result, tool_name=tool_name)
                    tool_output = build_tool_output_envelope(
                        result,
                        project_id=project_id,
                        run_id=run_id,
                        iteration=iteration,
                        tool_name=tool_name,
                    )
                    trace.emit(
                        "permission_decision",
                        iteration=iteration,
                        tool_name=tool_name,
                        deferred_tool_name=deferred_tool_name,
                        transition_reason="pre_tool_use",
                        permission_decision="deny",
                        allowed=False,
                        error_kind=result_error_kind(result),
                        denial_count=permission_denial_state.count,
                    )
                    trace.emit(
                        "tool_result",
                        iteration=iteration,
                        tool_name=tool_name,
                        deferred_tool_name=deferred_tool_name,
                        transition_reason="permission_denied",
                        duration_ms=elapsed_ms(tool_started_at),
                        error_kind=result_error_kind(result),
                        **tool_trace_fields(tool_output),
                    )
                    yield tool_done_event(tool_name, iteration + 1, tool_output)
                    _append_tool_result_messages(tool_call.id, tool_output)
                    if result_error_kind(result):
                        tool_errors.append(result)
                    if pre_tool_use.should_stop:
                        terminal_loop_error = result
                        stop_after_tool = True
                        trace.emit(
                            "loop_transition",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="repeated_permission_error_stopped",
                            error_kind=result_error_kind(result),
                            denial_count=permission_denial_state.count,
                        )
                        break
                    continue
                trace.emit(
                    "permission_decision",
                    iteration=iteration,
                    tool_name=tool_name,
                    deferred_tool_name=deferred_tool_name,
                    transition_reason="pre_tool_use",
                    permission_decision="allow",
                    allowed=True,
                )

                yield {
                    "type": "tool_start",
                    "tool": tool_name,
                    "round": iteration + 1,
                    "content": round_tool_start_content,
                }

                spec = registry.get(tool_name)
                if not spec:
                    result = normalize_tool_result(
                        {"error": f"Unknown tool: {tool_name}", "error_kind": "unknown_tool"},
                        tool_name=tool_name,
                    )
                    tool_output = build_tool_output_envelope(
                        result,
                        project_id=project_id,
                        run_id=run_id,
                        iteration=iteration,
                        tool_name=tool_name,
                    )
                    trace.emit(
                        "tool_result",
                        iteration=iteration,
                        tool_name=tool_name,
                        deferred_tool_name=deferred_tool_name,
                        transition_reason="unknown_tool",
                        duration_ms=elapsed_ms(tool_started_at),
                        error_kind=result_error_kind(result),
                        **tool_trace_fields(tool_output),
                    )
                    yield tool_done_event(tool_name, iteration + 1, tool_output)
                    _append_tool_result_messages(tool_call.id, tool_output)
                    continue

                # Auto-create canvas node for whitelisted producer tools only.
                # Query/admin tools (project.get_state, system.status, memory.recall,
                # media.list_providers, etc.) are excluded so the canvas stays clean.
                is_gen = tool_name in _NODE_PRODUCING_TOOLS
                node = None
                # Only node.* target tools may let the orchestrator take over a
                # node lifecycle. Other tools can accept node_id as a source
                # reference, and must not mutate that source node's status.
                # 删除类工具会真删那一行,这里复用并预先 update 会让本 session
                # identity map 残留过期实例 → 后续成功分支再 UPDATE 触发 StaleDataError。
                explicit_node_id = tool_args.get("node_id") if isinstance(tool_args, dict) else None
                _is_destructive = "destructive" in (spec.tags or [])
                tool_self_manages_node_lifecycle = tool_name == "node.run"
                if (
                    tool_name in _NODE_TARGET_TOOLS
                    and explicit_node_id
                    and isinstance(explicit_node_id, str)
                    and not _is_destructive
                ):
                    from app.db.models import WorkflowNode as _WfNode
                    existing = await self.db.get(_WfNode, explicit_node_id)
                    if existing is not None and existing.project_id == project_id:
                        node = existing
                        is_gen = True  # 强制走完成/失败时的节点更新分支
                        if not tool_self_manages_node_lifecycle:
                            # 复用前先把状态改 running,清掉旧错误。node.run 自己管理
                            # running/completed/failed，避免长媒体任务在外层断流时
                            # 留下由 orchestrator 预写的孤立 running 状态。
                            await self.node_service.update_node(
                                existing.id,
                                {"status": "running", "error_message": None},
                            )
                            yield {
                                "type": "canvas_action",
                                "action": "update_node",
                                "payload": {"id": existing.id, "status": "running"},
                            }
                        yield {"type": "step_start", "step_index": step_index, "total": 0, "tool": tool_name, "title": existing.title or tool_name}

                if is_gen and not spec.requires_node and node is None:
                    node_type = self._resolve_node_type(tool_name, tool_args)
                    step_title = tool_args.get("title") or spec.short_name
                    node = await self.node_service.create_node(
                        project_id,
                        {
                            "type": node_type,
                            "title": step_title,
                            "status": "running",
                            "input_json": tool_args,
                        },
                    )
                    yield {
                        "type": "canvas_action",
                        "action": "create_node",
                        "payload": {
                            "id": node.id,
                            "type": node.type,
                            "title": node.title,
                            "status": "running",
                        },
                    }
                    yield {"type": "step_start", "step_index": step_index, "total": 0, "tool": tool_name, "title": step_title}

                # Execute tool
                raw_result: Any = None
                try:
                    call_kwargs = self._filter_kwargs(spec.handler, tool_args)
                    _destructive = "destructive" in (spec.tags or [])
                    if tool_name == "canvas.delete":
                        raw_result = {
                            "ok": False,
                            "requires_user_confirm": True,
                            "action": "canvas.delete",
                            "risk": "destructive",
                            "reason": "该操作会删除画布节点、关联边和这些节点的本地生成产物，确认前不会执行。",
                            "input": {
                                "scope": call_kwargs.get("scope", "selected"),
                                "node_ids": call_kwargs.get("node_ids") or [],
                            },
                        }
                    else:
                        if _destructive:
                            await self._drop_session_cache(node)
                        raw_result = await registry.call(tool_name, **call_kwargs)
                        if _destructive:
                            await self._drop_session_cache(None)
                        raw_result = normalize_tool_result(raw_result, tool_name=tool_name)
                        skill_cache = _skill_guide_cache_payload(tool_name, raw_result)
                        if skill_cache:
                            existing_skill_cache = state.get(_SKILL_GUIDE_CACHE_KEY)
                            if not isinstance(existing_skill_cache, dict):
                                existing_skill_cache = {}
                            existing_payload = existing_skill_cache.get(skill_cache["skill"])
                            if existing_payload != skill_cache:
                                next_skill_cache = dict(existing_skill_cache)
                                next_skill_cache[skill_cache["skill"]] = skill_cache
                                state[_SKILL_GUIDE_CACHE_KEY] = next_skill_cache
                                await _persist_blueprint_patch({_SKILL_GUIDE_CACHE_KEY: next_skill_cache})
                            trace.emit(
                                "skill_loaded",
                                iteration=iteration,
                                tool_name=tool_name,
                                transition_reason="skill_tool_result_loaded",
                                skill=skill_cache.get("skill"),
                                detail=skill_cache.get("detail"),
                                guidance_chars=skill_cache.get("guidance_chars"),
                                guidance_hash=skill_cache.get("guidance_hash"),
                            )
                        guide_cache = _mentor_guide_cache_payload(raw_result)
                        if guide_cache:
                            existing_cache = state.get(_MENTOR_GUIDE_CACHE_KEY)
                            if not isinstance(existing_cache, dict):
                                existing_cache = {}
                            existing_payload = existing_cache.get(guide_cache["topic"])
                            if existing_payload != guide_cache:
                                next_cache = dict(existing_cache)
                                next_cache[guide_cache["topic"]] = guide_cache
                                state[_MENTOR_GUIDE_CACHE_KEY] = next_cache
                                await _persist_blueprint_patch({_MENTOR_GUIDE_CACHE_KEY: next_cache})
                        guide_trace = _guide_loaded_trace_payload(tool_name, raw_result)
                        if guide_trace:
                            trace.emit(
                                "guide_loaded",
                                iteration=iteration,
                                tool_name=tool_name,
                                transition_reason="guide_tool_result_loaded",
                                **guide_trace,
                            )
                        template_lookup = _template_lookup_state_payload(tool_args, raw_result)
                        if template_lookup:
                            lookup_by_category = (
                                dict(state.get("_template_lookups_by_category"))
                                if isinstance(state.get("_template_lookups_by_category"), dict)
                                else {}
                            )
                            lookup_category = str(template_lookup.get("category") or "").strip()
                            if lookup_category:
                                lookup_by_category[lookup_category] = template_lookup
                            state["_last_template_lookup"] = template_lookup
                            state["_template_lookups_by_category"] = lookup_by_category
                            await _persist_blueprint_patch({
                                "_last_template_lookup": template_lookup,
                                "_template_lookups_by_category": lookup_by_category,
                            })
                            trace.emit(
                                "template_lookup_recorded",
                                iteration=iteration,
                                tool_name=tool_name,
                                transition_reason="template_lookup_state_recorded",
                                **template_lookup,
                            )
                        review_payload = _agent_review_state_payload(tool_args, raw_result)
                        if review_payload:
                            await _persist_blueprint_patch({"_last_agent_review": review_payload})
                            trace.emit(
                                "agent_review_recorded",
                                iteration=iteration,
                                tool_name=tool_name,
                                transition_reason="agent_review_state_recorded",
                                review_profile=review_payload.get("review_profile"),
                                review_skill_key=review_payload.get("review_skill_key"),
                                status=review_payload.get("status"),
                                safe_to_submit=review_payload.get("safe_to_submit"),
                                findings_count=review_payload.get("findings_count"),
                            )

                    tool_error_kind = result_error_kind(raw_result)
                    if isinstance(raw_result, dict) and tool_error_kind:
                        # 工具返回的 {error} 是"业务返回",不是 Python 异常 ——
                        # raise 会把 stream 炸断 + LLM 看不到 hint 只能盲目重试。
                        # 全部走静默回流:把 error/hint/可诊断字段喂给 LLM,让它
                        # 自己改参数 / 补依赖 / 换工具,而不是后端死循环报错。
                        result = raw_result
                        tool_output = build_tool_output_envelope(
                            result,
                            project_id=project_id,
                            run_id=run_id,
                            iteration=iteration,
                            tool_name=tool_name,
                        )
                        trace.emit(
                            "tool_result",
                            iteration=iteration,
                            tool_name=tool_name,
                            deferred_tool_name=deferred_tool_name,
                            resolved_deferred_tool=str(result.get("_deferred_tool") or "") if isinstance(result, dict) else "",
                            transition_reason="tool_returned_error",
                            duration_ms=elapsed_ms(tool_started_at),
                            error_kind=result_error_kind(result),
                            **tool_trace_fields(tool_output),
                        )
                        yield tool_done_event(tool_name, iteration + 1, tool_output)
                        # 如果之前已经为这个 tool_call 建了宿主 node,把它标 failed,
                        # 否则画布上会留一个永远 running 的孤儿
                        if node is not None:
                            try:
                                await self.node_service.update_node(
                                    node.id,
                                    {
                                        "status": "failed",
                                        "error_message": str(raw_result.get("error") or raw_result.get("message") or "tool returned ok=false")[:300],
                                    },
                                )
                                yield {
                                    "type": "canvas_action",
                                    "action": "update_node",
                                    "payload": {"id": node.id, "status": "failed"},
                                }
                            except Exception:
                                logger.exception("failed to mark node failed after tool error")
                        _append_tool_result_messages(tool_call.id, tool_output)
                        yield {"type": "step_done", "step_index": step_index, "tool": tool_name, "status": "failed"}
                        step_index += 1
                        error_kind = tool_error_kind or "tool_error"
                        tool_errors.append(result)
                        error_key = (tool_name, error_kind)
                        tool_error_counts[error_key] = tool_error_counts.get(error_key, 0) + 1
                        # Also count per-tool errors (any error_kind) so the model
                        # cannot cycle through different error kinds to avoid the
                        # 3-error stop guard.
                        tool_total_key = (tool_name, "__any__")
                        tool_total = tool_error_counts.get(tool_total_key, 0) + 1
                        tool_error_counts[tool_total_key] = tool_total
                        if tool_error_counts[error_key] >= 3 or tool_total >= 5:
                            terminal_loop_error = {
                                **result,
                                "stop_reason": "repeated_tool_error",
                                "repeat_count": max(tool_error_counts[error_key], tool_total),
                            }
                            stop_after_tool = True
                            trace.emit(
                                "loop_transition",
                                iteration=iteration,
                                tool_name=tool_name,
                                transition_reason="repeated_tool_error_stopped",
                                error_kind=error_kind,
                                repeat_count=tool_error_counts[error_key],
                            )
                            break
                        continue

                    result = raw_result
                    _record_tool_vision_context(result)
                    if (
                        tool_name == "interaction.request_input"
                        and isinstance(result, dict)
                        and result.get("ok")
                        and result.get("status") == "awaiting_user"
                    ):
                        intake = result.get("intake") if isinstance(result.get("intake"), dict) else None
                        interaction_event = result.get("event") if isinstance(result.get("event"), dict) else {}
                        if intake is None and isinstance(interaction_event.get("intake"), dict):
                            intake = interaction_event.get("intake")
                        purpose = str((intake or {}).get("purpose") or result.get("purpose") or "").strip()
                        state_patch: dict[str, Any] = {}
                        if purpose == "video_blueprint_intake":
                            stage = str((intake or {}).get("stage") or result.get("stage") or "")
                            state_patch = video_intake_state_patch_for_interaction(
                                state,
                                message,
                                attachments,
                                stage,
                                intake,
                            )
                        if state_patch:
                            state.update(state_patch)
                            try:
                                await self.project_service.update_project_state(project_id, state_patch)
                            except Exception:
                                logger.exception("interaction state patch failed")

                        tool_output = build_tool_output_envelope(
                            result,
                            project_id=project_id,
                            run_id=run_id,
                            iteration=iteration,
                            tool_name=tool_name,
                        )
                        trace.emit(
                            "tool_result",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="awaiting_user_interaction",
                            duration_ms=elapsed_ms(tool_started_at),
                            error_kind=result_error_kind(result),
                            **tool_trace_fields(tool_output),
                        )
                        yield tool_done_event(tool_name, iteration + 1, tool_output)
                        event = dict(interaction_event or {})
                        event.setdefault("type", "interaction_input_requested")
                        event.setdefault("project_id", project_id)
                        event.setdefault("status", "awaiting_user")
                        if intake is not None:
                            event.setdefault("intake", intake)
                        if result.get("summary_text"):
                            event.setdefault("summary_text", result.get("summary_text"))

                        assistant_text = _sanitize_user_visible_text(
                            str(result.get("assistant_text") or result.get("summary_text") or "请补充信息。")
                        )
                        assistant_metadata = {"interactionInput": intake} if isinstance(intake, dict) else None
                        await self._save_message(
                            project_id,
                            "assistant",
                            assistant_text,
                            assistant_metadata,
                        )
                        yield event
                        if assistant_text:
                            yield {"type": "text_delta", "content": assistant_text}
                        yield {"type": "agent_round_done", "round": iteration + 1}
                        trace.emit(
                            "loop_transition",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="awaiting_user_interaction",
                        )
                        trace.emit(
                            "run_complete",
                            transition_reason="awaiting_user_interaction",
                            step_count=step_index,
                        )
                        yield {"type": "done", "status": "completed"}
                        return
                    if node is not None:
                        # 只有 _NODE_PRODUCING_TOOLS 的返回值
                        # 才是"该写进 output_json 的真实产物"。其他工具(node.* 元工具自管 output、
                        # project.* 等返回薄壳)
                        # 不能用它们的返回值覆盖 output_json,否则详情面板会读不到剧本/段落/规划。
                        is_node_meta = (spec.namespace == "node")
                        is_producer = (tool_name in _NODE_PRODUCING_TOOLS)
                        if tool_self_manages_node_lifecycle:
                            from app.db.models import WorkflowNode as _WfNode2
                            refreshed_node = await self.db.get(_WfNode2, node.id)
                            try:
                                preview_output = json.loads(refreshed_node.output_json) if refreshed_node and refreshed_node.output_json else None
                            except (json.JSONDecodeError, TypeError):
                                preview_output = None
                            node_status = getattr(refreshed_node, "status", None) or "completed"
                            if tool_args.get("action") != "render":
                                yield {
                                    "type": "canvas_action",
                                    "action": "update_node",
                                    "payload": {
                                        "id": node.id,
                                        "status": node_status,
                                        "preview": self._build_preview(tool_name, preview_output) if preview_output else None,
                                    },
                                }
                        elif is_node_meta or not is_producer:
                            # 元工具/非产物工具:只同步状态,不动 output_json
                            # (runner / 上层工具自己已写好真实产物)
                            await self.node_service.update_node(
                                node.id, {"status": "completed"}
                            )
                            # node.run(action='render') 内部已通过 _emit_fusion_canvas_event
                            # 推了带完整 fusion preview 的 completed 事件,这里不覆盖
                            if not (tool_name == "node.run" and tool_args.get("action") == "render"):
                                # 重新读一次拿到真实 output 给前端预览
                                from app.db.models import WorkflowNode as _WfNode2
                                refreshed_node = await self.db.get(_WfNode2, node.id)
                                try:
                                    preview_output = json.loads(refreshed_node.output_json) if refreshed_node and refreshed_node.output_json else None
                                except (json.JSONDecodeError, TypeError):
                                    preview_output = None
                                yield {
                                    "type": "canvas_action",
                                    "action": "update_node",
                                    "payload": {
                                        "id": node.id,
                                        "status": "completed",
                                        "preview": self._build_preview(tool_name, preview_output) if preview_output else None,
                                    },
                                }
                        else:
                            await self.node_service.update_node(
                                node.id, {"status": "completed", "output_json": result}
                            )
                            yield {
                                "type": "canvas_action",
                                "action": "update_node",
                                "payload": {
                                    "id": node.id,
                                    "status": "completed",
                                    "preview": self._build_preview(tool_name, result),
                                },
                            }
                        yield {"type": "step_done", "step_index": step_index, "tool": tool_name, "status": "completed"}
                        step_index += 1

                except asyncio.CancelledError:
                    if node is not None:
                        error_text = "任务因连接中断被取消，请在原节点重试"
                        await self.node_service.update_node(
                            node.id,
                            {"status": "failed", "error_message": error_text},
                        )
                    raise
                except Exception as exc:
                    logger.exception("Tool %s failed", tool_name)
                    error_text = str(exc)
                    error_detail: dict[str, Any] = {}
                    if isinstance(raw_result, dict):
                        # 把工具返回的诊断字段全部回流给 LLM,让它能读懂"哪个节点失败 /
                        # 该改用什么工具",而不是只看到一句 "action='render' 不支持..."
                        # 就盲目重试。
                        for k in (
                            "error", "error_kind", "http_code", "provider_msg",
                            "endpoint", "provider", "model",
                            "node_id", "node_type", "hint", "suggested_next",
                            "renderable_types", "available_runners",
                            "episode_number", "segment_index", "shot_id",
                        ):
                            if raw_result.get(k) is not None:
                                error_detail[k] = raw_result.get(k)
                    if not error_detail.get("error"):
                        error_detail["error"] = error_text
                    if node is not None and "node_id" not in error_detail:
                        error_detail["node_id"] = node.id
                        error_detail["node_type"] = getattr(node, "type", None)
                    error_detail["tool"] = tool_name
                    result = normalize_tool_result({"ok": False, **error_detail}, tool_name=tool_name)
                    tool_errors.append(result)
                    if node is not None:
                        await self.node_service.update_node(
                            node.id, {"status": "failed", "error_message": error_text}
                        )
                        yield {
                            "type": "canvas_action",
                            "action": "update_node",
                            "payload": {"id": node.id, "status": "failed", "error": error_text[:200]},
                        }
                        yield {"type": "step_done", "step_index": step_index, "tool": tool_name, "status": "failed"}
                        step_index += 1

                tool_output = build_tool_output_envelope(
                    result,
                    project_id=project_id,
                    run_id=run_id,
                    iteration=iteration,
                    tool_name=tool_name,
                )
                trace.emit(
                    "tool_result",
                    iteration=iteration,
                    tool_name=tool_name,
                    deferred_tool_name=deferred_tool_name,
                    resolved_deferred_tool=str(result.get("_deferred_tool") or "") if isinstance(result, dict) else "",
                    transition_reason="tool_completed",
                    duration_ms=elapsed_ms(tool_started_at),
                    error_kind=result_error_kind(result),
                    **tool_trace_fields(tool_output),
                )
                yield tool_done_event(tool_name, iteration + 1, tool_output)

                # Blueprint tree tools → emit blueprint_tree_changed SSE event
                # so the frontend can incrementally update its tree cache.
                if tool_name.startswith("blueprint.") and isinstance(result, dict) and result.get("ok"):
                    tree_event: dict[str, Any] = {
                        "type": "blueprint_tree_changed",
                        "project_id": project_id,
                        "tree_version": result.get("tree_version"),
                    }
                    if result.get("draft_mode"):
                        tree_event["draft_mode"] = result.get("draft_mode")
                    if "replacement" in result:
                        tree_event["replacement"] = bool(result.get("replacement"))
                    if tool_name == "blueprint.start_tree_draft":
                        tree_event["action"] = "replace_tree"
                        tree_event["node_id"] = "root"
                        tree_event["patch"] = {"tree_summary": result.get("tree_summary") or {}}
                    elif tool_name == "blueprint.append_tree_node":
                        tree_event["action"] = "add_child"
                        tree_event["parent_id"] = result.get("parent_id") or tool_args.get("parent_id", "root")
                        tree_event["node"] = result.get("node", {})
                    elif tool_name == "blueprint.update_tree_node":
                        tree_event["action"] = "update_node"
                        tree_event["node_id"] = result.get("node_id") or tool_args.get("node_id", "")
                        tree_event["patch"] = result.get("patch", {})
                        tree_event["node"] = result.get("node", {})
                    elif tool_name in {"blueprint.propose_tree", "blueprint.finalize_tree_draft"}:
                        tree_event["action"] = "replace_tree"
                        tree_event["node_id"] = "root"
                        tree_event["patch"] = {"tree_summary": result.get("tree_summary") or {}}
                    elif tool_name == "blueprint.add_child":
                        tree_event["action"] = "add_child"
                        tree_event["parent_id"] = tool_args.get("parent_id", "root")
                        tree_event["node"] = result.get("node", {})
                    elif tool_name == "blueprint.update_node":
                        tree_event["action"] = "update_node"
                        tree_event["node_id"] = tool_args.get("node_id", "")
                        tree_event["patch"] = result.get("patch", {})
                    elif tool_name == "blueprint.delete_node":
                        tree_event["action"] = "delete_node"
                        tree_event["node_id"] = tool_args.get("node_id", "")
                    elif tool_name == "blueprint.set_prompt":
                        tree_event["action"] = "update_node"
                        tree_event["node_id"] = tool_args.get("node_id", "")
                        patch = result.get("patch")
                        if not isinstance(patch, dict):
                            patch = {}
                        prompt_value = result.get("prompt")
                        if isinstance(prompt_value, str):
                            patch["prompt"] = prompt_value
                        negative_prompt_value = result.get("negative_prompt")
                        if isinstance(negative_prompt_value, str) and negative_prompt_value:
                            patch["negative_prompt"] = negative_prompt_value
                        status_value = result.get("status")
                        if isinstance(status_value, str):
                            patch["status"] = status_value
                        if patch:
                            tree_event["patch"] = patch
                    if tree_event.get("action"):
                        yield tree_event

                # Emit canvas events for canvas-affecting tools. Deferred tools
                # report their target in _deferred_tool so existing UI events
                # still fire for project.reset.
                event_tool_name = (
                    result.get("_deferred_tool")
                    if isinstance(result, dict) and isinstance(result.get("_deferred_tool"), str)
                    else tool_name
                )
                if event_tool_name == "canvas.delete" and isinstance(result, dict) and result.get("ok"):
                    if str(result.get("scope") or "") == "all":
                        yield {"type": "canvas_action", "action": "clear_all", "payload": {}}
                    else:
                        for nid in result.get("deleted_node_ids") or []:
                            if nid:
                                yield {"type": "canvas_action", "action": "delete_node", "payload": {"id": nid}}
                elif event_tool_name == "node.create" and isinstance(result, dict) and result.get("id"):
                    yield {"type": "canvas_action", "action": "create_node", "payload": result}
                elif event_tool_name == "node.update" and isinstance(result, dict) and result.get("id"):
                    yield {"type": "canvas_action", "action": "update_node", "payload": result}
                # Task tool events: emit checklist update so frontend panel refreshes
                if event_tool_name in {"task.create", "task.update", "task.complete", "task.delete"} and isinstance(result, dict):
                    try:
                        from app.agent.task_graph import task_graph
                        tasks = task_graph.list_all(project_id or None)
                        active = [t for t in tasks if t.status != "completed"]
                        checklist_payload = [
                            {
                                "step_id": t.id,
                                "title": t.subject,
                                "tool": t.tool or "task",
                                "status": t.status,
                                "blocked_by": t.blocked_by or [],
                            }
                            for t in active
                        ]
                        yield {
                            "type": "checklist_updated",
                            "checklist": checklist_payload,
                        }
                        trace.emit(
                            "loop_transition",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="task_checklist_emitted",
                            task_count=len(checklist_payload),
                        )
                    except Exception as exc:
                        logger.exception("task checklist emission failed")

                elif event_tool_name == "node.run" and isinstance(result, dict) and result.get("node_id") \
                        and tool_args.get("action") != "render" and node is None and not result_error_kind(result):
                    # node.run 非 render 路径(默认 runner / review 等)完成后构造画布预览
                    # render 路径由 node.run 内部 _emit_fusion_canvas_event 推完整 fusion preview
                    inner = result.get("result") or {}
                    payload: dict[str, Any] = {
                        "id": result["node_id"],
                        "status": "completed",
                    }
                    if isinstance(inner, dict):
                        img_url = inner.get("url") or inner.get("local_url")
                        if img_url:
                            payload["preview"] = {
                                "type": "image",
                                "url": img_url,
                                "local_url": inner.get("local_url"),
                                "remote_url": inner.get("remote_url"),
                            }
                    yield {"type": "canvas_action", "action": "update_node", "payload": payload}
                elif event_tool_name == "project.reset" and isinstance(result, dict) and result.get("ok"):
                    reset_scope = str(result.get("scope") or "full")
                    if result.get("cleared_all"):
                        yield {"type": "canvas_action", "action": "clear_all", "payload": {}}
                        if result.get("title"):
                            yield {
                                "type": "project_update",
                                "project_id": project_id,
                                "updates": {"title": result.get("title")},
                            }
                    else:
                        for nid in result.get("deleted_node_ids") or []:
                            if nid:
                                yield {"type": "canvas_action", "action": "delete_node", "payload": {"id": nid}}
                    if reset_scope == "full":
                        trace.emit(
                            "confirmation_resolved",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="full_reset_confirmed",
                            confirmation_kind="reset_project",
                            action="confirm",
                            scope=reset_scope,
                            cleared_all=bool(result.get("cleared_all")),
                        )
                        full_reset_completed = True
                        stop_after_tool = True
                        reset_text = reset_success_text(result, reset_scope)
                        reset_event = reset_project_event(
                            project_id,
                            result,
                            scope=reset_scope,
                            message=reset_text,
                        )
                        if reset_event:
                            yield reset_event
                        yield {"type": "text_delta", "content": reset_text}
                elif event_tool_name == "project.reset" and isinstance(result, dict) and result.get("requires_user_confirm"):
                    # 拦截到了 agent 擅自 full reset — 存待确认标记，下次用户回复检测
                    try:
                        import time as _t2
                        state["_pending_reset_confirm"] = {
                            "scope": result.get("scope", "full"),
                            "reason": result.get("reason", "agent 请求清空项目"),
                            "ts": int(_t2.time()),
                            "expires_at": confirmation_expires_at(),
                        }
                        await self.project_service.update_project_state(
                            project_id, {"_pending_reset_confirm": state["_pending_reset_confirm"]},
                        )
                    except Exception:
                        logger.exception("store _pending_reset_confirm failed")
                    trace.emit(
                        "confirmation_created",
                        iteration=iteration,
                        tool_name=tool_name,
                        transition_reason="full_reset_requires_confirmation",
                        confirmation_kind="reset_project",
                        risk="destructive",
                        action="reset_project",
                        scope="full",
                        reason=result.get("reason", "agent 请求清空项目"),
                        expires_at=state.get("_pending_reset_confirm", {}).get("expires_at"),
                    )
                    yield {
                        "type": "confirm_required",
                        "action": "reset_project",
                        "scope": "full",
                        "reason": result.get("reason", "agent 请求清空项目"),
                        "expires_at": state.get("_pending_reset_confirm", {}).get("expires_at"),
                    }
                    assistant_text = reset_confirmation_text()
                    full_response += assistant_text
                    yield {"type": "text_delta", "content": assistant_text}
                    stop_after_tool = True
                elif (
                    isinstance(result, dict)
                    and result.get("requires_user_confirm")
                    and event_tool_name in _CONFIRMABLE_DESTRUCTIVE_TOOLS
                ):
                    confirmation_input = (
                        result.get("input")
                        if isinstance(result.get("input"), dict)
                        else {
                            key: value
                            for key, value in tool_args.items()
                            if key not in {"project_id", "_state", "_user_message", "_requires_plan"}
                        }
                    )
                    pending_tool = {
                        "kind": "tool_confirmation",
                        "target": event_tool_name,
                        "risk": result.get("risk") or "destructive",
                        "reason": result.get("reason") or "该操作需要确认。",
                        "input": confirmation_input,
                        "source_user_message": _compact_context_quote(saved_user_message),
                        "source_agent_message": _compact_context_quote(message),
                        "source_user_metadata": user_metadata if isinstance(user_metadata, dict) else None,
                        "ts": int(time.time()),
                        "expires_at": confirmation_expires_at(),
                    }
                    state["_pending_tool_confirm"] = pending_tool
                    try:
                        await self.project_service.update_project_state(
                            project_id, {"_pending_tool_confirm": pending_tool},
                        )
                    except Exception:
                        logger.exception("store _pending_tool_confirm failed")
                    trace.emit(
                        "confirmation_created",
                        iteration=iteration,
                        tool_name=tool_name,
                        transition_reason="destructive_tool_requires_confirmation",
                        confirmation_kind="tool_confirmation",
                        risk=pending_tool["risk"],
                        action=event_tool_name,
                        target=event_tool_name,
                        reason=pending_tool["reason"],
                        node_id=confirmation_input.get("node_id"),
                        expires_at=pending_tool["expires_at"],
                    )
                    delete_scope = str(confirmation_input.get("scope") or "selected").strip().lower()
                    scope = "canvas" if delete_scope in {"all", "canvas", "clear_all"} else "node"
                    yield {
                        "type": "confirm_required",
                        "action": event_tool_name,
                        "scope": scope,
                        "reason": pending_tool["reason"],
                        "risk": pending_tool["risk"],
                        "node_id": (confirmation_input.get("node_ids") or [None])[0]
                        if isinstance(confirmation_input.get("node_ids"), list)
                        else None,
                        "expires_at": pending_tool["expires_at"],
                    }
                    assistant_text = (
                        "清空画布需要确认。确认前不会删除任何节点、连线或本地产物。"
                        if scope == "canvas"
                        else "删除节点需要确认。确认前不会删除节点、连线或本地产物。"
                    )
                    full_response += assistant_text
                    yield {"type": "text_delta", "content": assistant_text}
                    stop_after_tool = True
                elif event_tool_name == "project.create" and isinstance(result, dict) and result.get("id"):
                    yield {"type": "project_switch", "project_id": result["id"], "title": result.get("title", "")}
                    yield {"type": "canvas_action", "action": "clear_all", "payload": {}}
                    project_switched = True

                if tool_name == "blueprint.revise" and isinstance(result, dict) and result.get("ok"):
                    if isinstance(result.get("blueprint"), dict):
                        blueprint_index = result.get("blueprint")
                        state["project_blueprint"] = blueprint_index
                        state["pending_blueprint_revision"] = None
                        stale_nodes = result.get("stale_nodes") if isinstance(result.get("stale_nodes"), list) else []
                        rematerialized = result.get("rematerialized_node_ids") if isinstance(result.get("rematerialized_node_ids"), list) else []
                        pending_revision = result.get("pending_revision") if isinstance(result.get("pending_revision"), dict) else {}
                        trace.emit(
                            "blueprint_revision_applied",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="blueprint_revise_tool_applied",
                            revision_version=blueprint_index.get("version"),
                            target_node_id=pending_revision.get("target_node_id"),
                            affected_source_paths=pending_revision.get("applied_source_paths") or [],
                            rematerialized_node_ids=rematerialized,
                            stale_node_count=len(stale_nodes),
                            stale_nodes=stale_nodes,
                            auto_applied=bool(result.get("auto_applied")),
                        )
                        if result.get("auto_applied"):
                            trace.emit(
                                "confirmation_skipped",
                                iteration=iteration,
                                tool_name=tool_name,
                                transition_reason="blueprint_revision_auto_applied",
                                confirmation_kind="blueprint_revision",
                                reason="low_risk_auto_apply",
                                risk=(result.get("risk") if isinstance(result.get("risk"), dict) else {}),
                            )
                        event = {
                            "type": "blueprint_revision_applied",
                            "project_id": project_id,
                            "blueprint": blueprint_index,
                            "view_model": result.get("view_model"),
                            "rematerialized_node_ids": rematerialized,
                            "stale_nodes": stale_nodes,
                            "auto_applied": bool(result.get("auto_applied")),
                        }
                        yield event
                        if isinstance(blueprint_index, dict) and blueprint_index.get("theme_title"):
                            yield {
                                "type": "project_update",
                                "project_id": project_id,
                                "updates": {"title": blueprint_index.get("theme_title")},
                            }
                        assistant_text = _sanitize_user_visible_text(
                            str(result.get("message") or "蓝图修订已应用。")
                        )
                        if assistant_text:
                            full_response += assistant_text
                            yield {"type": "text_delta", "content": assistant_text}
                        stop_after_tool = True
                    elif result.get("requires_user_confirm"):
                        pending_revision = result.get("pending_revision") if isinstance(result.get("pending_revision"), dict) else {}
                        state["pending_blueprint_revision"] = pending_revision
                        _pending_meta["blueprintRevision"] = pending_revision
                        affected = result.get("affected_source_paths") if isinstance(result.get("affected_source_paths"), list) else []
                        risk = result.get("risk") if isinstance(result.get("risk"), dict) else {}
                        trace.emit(
                            "confirmation_created",
                            iteration=iteration,
                            tool_name=tool_name,
                            transition_reason="blueprint_revision_requires_confirmation",
                            confirmation_kind="blueprint_revision",
                            action="confirm_or_revise",
                            risk=risk,
                            pending_revision_id=pending_revision.get("id"),
                            revision_version=pending_revision.get("version"),
                            target_node_id=pending_revision.get("target_node_id"),
                            affected_source_paths=affected,
                        )
                        yield {
                            "type": "blueprint_revision_proposed",
                            "project_id": project_id,
                            "pending_revision": pending_revision,
                            "risk": risk,
                            "affected_source_paths": affected,
                        }
                        assistant_text = _sanitize_user_visible_text(
                            str(
                                result.get("message")
                                or f"已生成蓝图修订草稿，影响 {len(affected)} 处内容。确认后应用；需要调整可以直接说明。"
                            )
                        )
                        if assistant_text:
                            full_response += assistant_text
                            yield {"type": "text_delta", "content": assistant_text}
                        stop_after_tool = True

                # Feed result back to LLM
                _append_tool_result_messages(tool_call.id, tool_output)

                # Drain canvas events from sub-agents
                while not _canvas_event_queue.empty():
                    try:
                        ev = _canvas_event_queue.get_nowait()
                        yield ev
                    except asyncio.QueueEmpty:
                        break

                if model_feedback_after_tool:
                    break
                if stop_after_tool:
                    break

            if round_tool_context_messages:
                messages.extend(round_tool_context_messages)

            yield {"type": "agent_round_done", "round": iteration + 1}
            trace.emit(
                "loop_transition",
                iteration=iteration,
                transition_reason="tool_round_completed",
                stop_after_tool=stop_after_tool,
                model_feedback_after_tool=model_feedback_after_tool,
                project_switched=project_switched,
            )

            # Refresh state after tool execution
            refreshed = await self.project_service.get_project_state(project_id)
            state_refreshed = False
            if refreshed is not None:
                state.clear()
                state.update(refreshed)
                state_refreshed = True

            new_summary = await self._compute_canvas_summary(project_id)
            if state_refreshed or new_summary != canvas_summary:
                canvas_summary = new_summary
                # 只重建 system,history 已经在 messages 里,不必重新注入
                prompt_assembly = await _rebuild_system_result(state, canvas_summary)
                system = prompt_assembly.system
                runtime_inject = prompt_assembly.runtime
                prompt_assembly_diag = prompt_assembly.diagnostics()

            if project_switched:
                yield {
                    "type": "text_delta",
                    "content": "\n\n✅ 新项目已创建并切换为当前项目。请告诉我接下来想做什么。",
                }
                break
            if stop_after_tool:
                break

        if not full_reset_completed and not full_response and not interrupted_by_new_message:
            fallback_text = self._build_no_text_fallback(
                state=state,
                pending_meta=_pending_meta,
                terminal_error=terminal_loop_error,
                tool_errors=tool_errors,
                step_index=step_index,
                project_switched=project_switched,
            )
            fallback_text = _sanitize_user_visible_text(fallback_text)
            if fallback_text:
                full_response = fallback_text
                yield {"type": "text_delta", "content": fallback_text}

        if tool_vision_contexts:
            max_tool_images = configured_max_images(agent_prefs.get("vision_context_max_images"))
            combined_images: list[dict[str, Any]] = []
            seen_sources: set[str] = set()
            omitted_images = 0
            for payload in tool_vision_contexts:
                raw_images = payload.get("images") if isinstance(payload, dict) else None
                if not isinstance(raw_images, list):
                    continue
                for image in raw_images:
                    if not isinstance(image, dict):
                        continue
                    source = str(image.get("source") or "").strip()
                    key = source or json.dumps(image, ensure_ascii=False, sort_keys=True, default=str)
                    if key in seen_sources:
                        continue
                    seen_sources.add(key)
                    if len(combined_images) >= max_tool_images:
                        omitted_images += 1
                        continue
                    combined_images.append(dict(image))
                try:
                    omitted_images += int(payload.get("omitted_count") or 0)
                except (TypeError, ValueError):
                    pass
            if combined_images:
                _pending_meta = attach_vision_metadata(
                    _pending_meta,
                    {
                        "version": 1,
                        "kind": "vision_context",
                        "source": "vision.view_image",
                        "tool_name": "vision.view_image",
                        "images": combined_images,
                        "image_count": len(combined_images),
                        "omitted_count": omitted_images,
                    },
                ) or _pending_meta

        # Save assistant response with metadata so refresh restores rich chat cards.
        # Always save if we have plan/nodes metadata, even when full_response is empty.
        persisted_rounds = self._extract_agent_round_history(messages)
        user_facing_meta_before_rounds = bool(_pending_meta)
        if persisted_rounds and not full_reset_completed and not (
            interrupted_by_new_message and not full_response and not user_facing_meta_before_rounds
        ):
            _pending_meta["rounds"] = persisted_rounds

        if not full_reset_completed and (full_response or _pending_meta):
            meta_to_save = dict(_pending_meta) if _pending_meta else None
            await self._save_message(project_id, "assistant", full_response or "(tool calls)", meta_to_save)

        # 归档已完成的任务到历史，删除任务文件
        try:
            from app.agent.task_graph import task_graph as _tg_end
            _tg_end.archive_completed(project_id)
        except Exception:
            pass

        # Compress if needed
        if agent_prefs["auto_archive"] and not full_reset_completed:
            await self._maybe_compress_history(project_id)
        if full_reset_completed:
            completion_reason = "project_reset_completed"
        elif terminal_loop_error:
            completion_reason = str(terminal_loop_error.get("stop_reason") or "stopped_after_tool_error")
        elif project_switched:
            completion_reason = "project_switched"
        elif interrupted_by_new_message:
            completion_reason = "new_message_pending"
        elif stop_after_tool:
            completion_reason = "stopped_after_tool"
        else:
            completion_reason = "completed"
        trace.emit(
            "run_complete",
            transition_reason=completion_reason,
            step_count=step_index,
        )
        yield {"type": "done", "status": "completed"}

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _drop_session_cache(self, node) -> None:
        """销毁类工具会用独立 session 删/改行 → 主 session identity map
        会留下过期实例,下一次 update/flush 命中老对象就 StaleDataError。
        调用方在 destructive 工具前后各喊一次:进去前 expunge 已加载的目标节点,
        出来后 expire_all 让任何残留实例下次访问走 DB。"""
        try:
            if node is not None:
                try:
                    self.db.expunge(node)
                except Exception:
                    pass
            else:
                self.db.expire_all()
        except Exception:
            logger.exception("session cache drop failed")

    async def _save_message(
        self, project_id: str, role: str, content: str, metadata: dict | None = None,
    ) -> None:
        if role == "assistant":
            content = _sanitize_user_visible_text(content)
        msg = Message(
            project_id=project_id,
            role=role,
            content=content,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        self.db.add(msg)
        await self.db.commit()

    async def _call_build_messages(
        self,
        project_id: str,
        current_message: str,
        *,
        include_history: bool = True,
        current_message_aliases: list[str] | None = None,
        max_images: Any = None,
        max_dimension: Any = None,
    ) -> list[dict]:
        import inspect

        kwargs: dict[str, Any] = {
            "include_history": include_history,
            "current_message_aliases": current_message_aliases,
            "max_images": max_images,
            "max_dimension": max_dimension,
        }
        try:
            sig = inspect.signature(self._build_messages)
            params = sig.parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass
        return await self._build_messages(project_id, current_message, **kwargs)

    async def _build_messages(
        self,
        project_id: str,
        current_message: str,
        include_history: bool = True,
        current_message_aliases: list[str] | None = None,
        max_images: Any = None,
        max_dimension: Any = None,
    ) -> list[dict]:
        if not include_history:
            return [{"role": "user", "content": current_message}]

        result = await self.db.exec(
            select(Message)
            .where(
                Message.project_id == project_id,
                Message.archived == False,  # noqa: E712
                Message.role.in_(("user", "assistant")),
            )
            .order_by(Message.created_at.desc())
        )
        rows = list(result.all())
        rows.reverse()

        history_entries: list[tuple[dict, str | None]] = []
        for m in rows:
            if m.role not in ("user", "assistant"):
                continue
            raw_content = str(m.content or "")
            message = {"role": m.role, "content": raw_content}
            metadata: dict[str, Any] = {}
            metadata_json = getattr(m, "metadata_json", None)
            if metadata_json:
                try:
                    parsed = json.loads(metadata_json)
                    if isinstance(parsed, dict):
                        metadata = parsed
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
            vision_context = await build_vision_context_from_metadata(
                project_id,
                metadata,
                max_images=max_images,
                max_dimension=max_dimension,
            )
            if vision_context.images:
                if m.role == "user":
                    message["content"] = multimodal_content(raw_content, vision_context)
                    history_entries.append((message, raw_content))
                else:
                    history_entries.append((message, raw_content))
                    history_entries.append((
                        {
                            "role": "user",
                            "content": multimodal_content(
                                "Visual context retained from the previous vision.view_image result.",
                                vision_context,
                            ),
                            "_tool_image_context": True,
                            "_persisted_vision_context": True,
                        },
                        None,
                    ))
            else:
                history_entries.append((message, raw_content))

        aliases = {current_message, *(current_message_aliases or [])}
        if history_entries:
            last_message, last_raw_content = history_entries[-1]
            if last_message.get("role") == "user" and last_raw_content in aliases:
                history_entries.pop()
        history = [message for message, _raw_content in history_entries]
        history.append({"role": "user", "content": current_message})
        return history

    async def _maybe_compress_history(self, project_id: str) -> None:
        try:
            from app.mcp_tools.memory_tools import memory_compact_context
            result = await self.db.exec(
                select(Message).where(
                    Message.project_id == project_id,
                    Message.archived == False,  # noqa: E712
                )
            )
            active_messages = []
            for row in result.all():
                if row.role not in ("user", "assistant"):
                    continue
                metadata: dict[str, Any] = {}
                metadata_json = getattr(row, "metadata_json", None)
                if metadata_json:
                    try:
                        parsed = json.loads(metadata_json)
                        if isinstance(parsed, dict):
                            metadata = parsed
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                active_messages.append({
                    "role": row.role,
                    "content": row.content,
                    "_metadata": metadata,
                })
            if not auto_compact_needed(active_messages):
                return
            await memory_compact_context(project_id)
        except Exception as exc:
            logger.warning("Compression failed: %s", exc)

    @staticmethod
    def _build_checklist_reminder(
        state: dict,
        canvas_summary: dict | None = None,
        *,
        require_plan: bool = False,
        project_id: str | None = None,
    ) -> str:
        """拼当前执行状态的 system-reminder,每轮 LLM 前注入。

        包含:
        - 已有任务图进度(只作恢复线索)
        - 未完成节点提示(画布上有失败/未出图节点时)
        - 最新用户消息优先的执行边界
        """
        # Read tasks from task_graph; legacy plan-state mirrors are cleaned before the turn.
        task_project_id = str(project_id or (state.get("project_id") if isinstance(state, dict) else "") or "").strip()
        try:
            from app.agent.task_graph import task_graph as _tg
            all_tasks = _tg.list_all(task_project_id) if task_project_id else []
        except Exception:
            all_tasks = []
        checklist = [
            {
                "step_id": t.id,
                "title": t.subject,
                "tool": t.tool or "",
                "status": t.status,
                "blocked_by": t.blocked_by or [],
                "actual_node_id": (t.input or {}).get("node_id", ""),
            }
            for t in all_tasks
        ]
        # 没有任务、没有 plan 前置要求 → 不注入。节点状态由模型按需 node.list/node.get 读取。
        if not checklist and not require_plan:
            return ""

        lines: list[str] = ["<execution-checklist>"]

        # 清单段
        if checklist:
            done = sum(1 for s in checklist if s.get("status") == "completed")
            failed = sum(1 for s in checklist if s.get("status") == "failed")
            pending_idx = next(
                (i for i, s in enumerate(checklist)
                 if s.get("status") in (None, "pending", "in_progress")),
                None,
            )
            lines.append(
                f"\n执行清单({done}/{len(checklist)} 完成,失败 {failed}):"
            )
            for i, s in enumerate(checklist):
                st = s.get("status") or "pending"
                marker = {
                    "completed": "[done]",
                    "failed": "[failed]",
                    "in_progress": "[running]",
                }.get(st, "[pending]")
                cursor = " ← 当前应做" if i == pending_idx else ""
                nid = s.get("actual_node_id")
                nid_str = f" [node {nid[:8]}]" if isinstance(nid, str) and nid else ""
                title = s.get("title") or s.get("tool") or ""
                tool = s.get("tool") or ""
                lines.append(f"  {i + 1}. {marker} {title} ({tool}){nid_str}{cursor}")
            if pending_idx is not None:
                cur = checklist[pending_idx]
                lines.append(
                    f"\n当前步骤:第 {pending_idx + 1} 步 {cur.get('title')} (tool={cur.get('tool')})。"
                    "如果用户这轮说了新的修改要求（如改剧情、删节点、调整结构），优先处理用户消息，再决定是否继续参考清单。"
                )
            else:
                lines.append(
                    "\n清单已全部跑完。做收尾自检，然后纯文本告诉用户结果。"
                    "用户明确要求清理残留画布内容时，按删除确认流程处理。"
                )
        elif require_plan:
            lines.append(
                "\n当前项目要求先确认计划，但计划工具不在本轮可见工具面。"
                "不要猜测隐藏工具；请向用户说明这个阻塞，或在用户明确同意后按可见节点工具继续。"
            )

        # 任务执行引导
        if checklist or require_plan:
            lines.append(
                "\n任务提示:\n"
                "1) 已有清单是当前执行账本；继续/修复时优先按可执行任务推进\n"
                "2) 当前可执行记录以 node 状态和工具返回为准，任务完成必须以真实结果为依据\n"
                "3) 用户这轮的新消息优先级高于清单，先回应用户的修改请求，再决定是否更新或继续清单"
            )
        lines.append("\n此清单仅作为你的执行参考，不要在回复中逐条复述清单内容。用户已在面板中看到任务进度。")
        lines.append("</execution-checklist>")
        return "\n".join(lines)

    @staticmethod
    def _check_workflow_mutex(tool_name: str, args: dict, project_state: dict) -> str | None:
        return None

    @staticmethod
    def _resolve_step_refs(inputs: dict, refs: dict[str, str]) -> None:
        """替换步骤输入中的占位符引用,如 <由 step1 产出> → 实际 node_id。"""
        import re
        _ref_re = re.compile(r"<由\s*step\s*(\d+)\s*产出>")
        for key, val in list(inputs.items()):
            if isinstance(val, str):
                m = _ref_re.search(val)
                if m:
                    ref_step = m.group(1)
                    if ref_step in refs:
                        inputs[key] = refs[ref_step]
            elif isinstance(val, dict):
                AgentOrchestrator._resolve_step_refs(val, refs)

    @staticmethod
    def _normalize_step_input(tool: str, step_input: dict) -> dict:
        """Return a shallow copy before kwargs filtering."""
        return dict(step_input)

    @staticmethod
    def _filter_kwargs(handler, kwargs: dict) -> dict:
        import inspect
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            return kwargs
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return _coerce_types(kwargs, params)
        filtered = {k: v for k, v in kwargs.items() if k in params}
        return _coerce_types(filtered, params)

    @staticmethod
    def _resolve_node_type(tool_name: str, args: dict) -> str:
        """Map the remaining direct producer to a canvas node type."""
        if tool_name == "drama.parse_uploaded_script":
            return "text"
        return "text"

    @staticmethod
    def _summarize_result(tool: str, title: str, result: Any) -> list[str]:
        parts: list[str] = []
        if not isinstance(result, dict):
            parts.append(f"\n\n✅ **{title}** 完成。\n")
            return parts

        parts.append(f"\n\n✅ **{title}** 完成。\n")
        return parts

    @staticmethod
    def _build_preview(tool_name: str, result: Any) -> dict:
        """Extract a compact preview from tool result for canvas node display."""
        if not isinstance(result, dict):
            return {}
        preview: dict[str, Any] = {}

        if isinstance(result, dict) and result.get("type") == "fusion":
            preview["type"] = "fusion"
            preview["subject"] = result.get("subject")
            preview["stages"] = result.get("stages")

        return preview
