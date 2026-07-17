"""Deterministic slash-command handlers for chat.

These commands are intentionally handled before the LLM sees a turn. They are
small control-plane operations where a backend state transition is safer and
more predictable than asking the model to infer intent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from sqlmodel import select

from app.agent.confirmation_protocol import (
    confirmation_expires_at,
    is_pending_confirmation_expired,
)
from app.agent.collaboration_mode import (
    MODE_DEFAULT,
    MODE_PLAN,
    MODE_WORKFLOW_BUILD,
    collaboration_mode_patch,
    proposed_plan_markdown,
)
from app.agent.event_stream import event_stream
from app.agent.reset_flow import reset_project_event
from app.agent.feature_flags import get_feature_states
from app.agent.blueprint_confirmation_state import pending_blueprint_plan
from app.agent.project_state_io import (
    read_project_state as _read_state,
    write_project_state as _write_state,
)
from app.agent.project_blueprint import BLUEPRINT_SECTION_TITLES
from app.db.models import Message, WorkflowNode
from app.db.session import session_scope
from app.mcp_tools.drama_tools import reset_project
from app.mcp_tools.project_tools import (
    project_get_state,
    project_update_state,
)


@dataclass(frozen=True)
class SlashCommand:
    name: str
    args: list[str]
    raw: str


_COMMANDS = {"plan", "workflow", "reset", "doctor", "help"}
_PLAN_EXIT_ACTIONS = {"exit", "default", "off", "quit", "退出", "关闭"}
_WORKFLOW_EXIT_ACTIONS = {"exit", "default", "off", "quit", "退出", "关闭"}
_PLAN_EXECUTE_ACTIONS = {"execute", "run", "apply", "执行", "实施"}
_PLAN_LEGACY_ACTIONS = {
    "approve",
    "reject",
    "clear",
    "pending",
    "get",
    "status",
    "批准",
    "拒绝",
    "清除",
    "清空",
    "查看",
    "状态",
}


def parse_slash_command(message: str) -> SlashCommand | None:
    raw = (message or "").strip()
    if not raw.startswith("/"):
        return None
    body = raw[1:].strip()
    if not body:
        return SlashCommand(name="help", args=[], raw=raw)
    parts = body.split()
    return SlashCommand(name=parts[0].lower(), args=parts[1:], raw=raw)


def is_slash_command(message: str) -> bool:
    return parse_slash_command(message) is not None


async def slash_command_events(
    project_id: str,
    message: str,
    *,
    orchestrator: Any | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    command = parse_slash_command(message)
    if command is None:
        return

    if not _slash_command_streams_to_agent(command):
        await _save_message(
            project_id,
            "user",
            command.raw,
            {"source": "slash_command", "command": command.name},
        )
    _emit_control_plane_event(
        project_id,
        command,
        branch="slash_command_received",
        status="received",
    )

    if command.name not in _COMMANDS:
        text = _help_text(f"未知命令 /{command.name}。")
        await _emit_text(project_id, command, text, ok=False)
        yield {"type": "slash_command", "command": command.name, "ok": False, "error": "unknown_command"}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "failed"}
        return

    if command.name in {"help"}:
        text = _help_text()
        await _emit_text(project_id, command, text)
        yield {"type": "slash_command", "command": command.name, "ok": True}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    if command.name == "plan":
        async for event in _plan_events(project_id, command, orchestrator=orchestrator):
            yield event
        return

    if command.name == "workflow":
        async for event in _workflow_events(project_id, command):
            yield event
        return

    if command.name == "reset":
        async for event in _reset_events(project_id, command):
            yield event
        return

    if command.name == "doctor":
        async for event in _doctor_events(project_id, command):
            yield event
        return


def _slash_command_streams_to_agent(command: SlashCommand) -> bool:
    if command.name != "plan" or not command.args:
        return False
    action = command.args[0].lower().strip()
    if action in _PLAN_EXECUTE_ACTIONS:
        return True
    if action in _PLAN_EXIT_ACTIONS or action in _PLAN_LEGACY_ACTIONS:
        return False
    return True


async def _workflow_events(
    project_id: str,
    command: SlashCommand,
) -> AsyncGenerator[dict[str, Any], None]:
    action = (command.args[0].lower() if command.args else "").strip()

    if not action:
        await project_update_state(project_id, collaboration_mode_patch(MODE_WORKFLOW_BUILD))
        text = "已进入工作流搭建模式。这个模式聚焦工作流搭建、修改、检查和保存；生成视频请退出后运行工作流。发送 `/workflow exit` 退出。"
        await _emit_text(project_id, command, text, ok=True)
        yield {"type": "mode_updated", "ok": True, "mode": MODE_WORKFLOW_BUILD, "collaboration_mode": MODE_WORKFLOW_BUILD}
        yield {"type": "slash_command", "command": "workflow", "action": "enter", "ok": True}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    if action in _WORKFLOW_EXIT_ACTIONS:
        await project_update_state(project_id, collaboration_mode_patch(MODE_DEFAULT))
        text = "已退出工作流搭建模式，回到默认制作模式。"
        await _emit_text(project_id, command, text, ok=True)
        yield {"type": "mode_updated", "ok": True, "mode": MODE_DEFAULT, "collaboration_mode": MODE_DEFAULT}
        yield {"type": "slash_command", "command": "workflow", "action": "exit", "ok": True}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    text = "工作流命令只支持 `/workflow` 和 `/workflow exit`。进入模式后，用普通自然语言描述要搭建或修改的工作流。"
    await _emit_text(project_id, command, text, ok=False)
    yield {"type": "slash_command", "command": "workflow", "action": action, "ok": False, "error": "invalid_workflow_action"}
    yield {"type": "text_delta", "content": text}
    yield {"type": "done", "status": "failed"}


async def _plan_events(
    project_id: str,
    command: SlashCommand,
    *,
    orchestrator: Any | None,
) -> AsyncGenerator[dict[str, Any], None]:
    action = (command.args[0].lower() if command.args else "").strip()

    if not action:
        await project_update_state(project_id, collaboration_mode_patch(MODE_PLAN))
        text = "已进入 Plan Mode。这个模式只读取、审查和提问，不会修改或生成项目内容。发送 `/plan exit` 退出。"
        await _emit_text(project_id, command, text, ok=True)
        yield {"type": "mode_updated", "ok": True, "mode": MODE_PLAN, "collaboration_mode": MODE_PLAN}
        yield {"type": "slash_command", "command": "plan", "action": "enter", "ok": True}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    if action in _PLAN_EXIT_ACTIONS:
        await project_update_state(project_id, collaboration_mode_patch(MODE_DEFAULT))
        text = "已退出 Plan Mode，回到默认执行模式。"
        await _emit_text(project_id, command, text, ok=True)
        yield {"type": "mode_updated", "ok": True, "mode": MODE_DEFAULT, "collaboration_mode": MODE_DEFAULT}
        yield {"type": "slash_command", "command": "plan", "action": "exit", "ok": True}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    if action in _PLAN_LEGACY_ACTIONS:
        text = "这个旧计划命令已经下线。可用：/plan、/plan <目标>、/plan execute、/plan exit。"
        await _emit_text(project_id, command, text, ok=False)
        yield {
            "type": "slash_command",
            "command": "plan",
            "action": action,
            "ok": False,
            "error": "legacy_plan_action_removed",
        }
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "failed"}
        return

    if action in _PLAN_EXECUTE_ACTIONS:
        async for event in _plan_execute_events(project_id, command, orchestrator=orchestrator):
            yield event
        return

    if orchestrator is None:
        text = "/plan <目标> 需要 chat stream executor。"
        await _emit_text(project_id, command, text, ok=False)
        yield {"type": "slash_command", "command": "plan", "action": "prompt", "ok": False}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "failed"}
        return

    plan_prompt = " ".join(command.args).strip()
    await project_update_state(project_id, collaboration_mode_patch(MODE_PLAN))
    yield {"type": "mode_updated", "ok": True, "mode": MODE_PLAN, "collaboration_mode": MODE_PLAN}
    async for event in orchestrator.stream(
        project_id=project_id,
        message=plan_prompt,
        display_message=command.raw,
        user_metadata={
            "source": "slash_command",
            "command": "plan",
            "action": "prompt",
            "collaboration_mode": MODE_PLAN,
        },
    ):
        yield event


async def _plan_execute_events(
    project_id: str,
    command: SlashCommand,
    *,
    orchestrator: Any | None,
) -> AsyncGenerator[dict[str, Any], None]:
    if orchestrator is None:
        text = "/plan execute 需要 chat stream executor。"
        await _emit_text(project_id, command, text, ok=False)
        yield {"type": "slash_command", "command": "plan", "action": "execute", "ok": False}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "failed"}
        return

    plan, markdown = await _latest_proposed_plan(project_id)
    if not markdown:
        text = "没有找到最近的 proposed plan。请先用 `/plan <目标>` 生成计划。"
        await _emit_text(project_id, command, text, ok=False)
        yield {"type": "slash_command", "command": "plan", "action": "execute", "ok": False}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "failed"}
        return

    await project_update_state(project_id, collaboration_mode_patch(MODE_DEFAULT))
    yield {"type": "mode_updated", "ok": True, "mode": MODE_DEFAULT, "collaboration_mode": MODE_DEFAULT}
    yield {
        "type": "slash_command",
        "command": "plan",
        "action": "execute",
        "ok": True,
        "result": {"plan_id": plan.get("id") if isinstance(plan, dict) else None},
    }
    execution_message = "执行上一条计划：\n\n" + markdown
    async for event in orchestrator.stream(
        project_id=project_id,
        message=execution_message,
        display_message="执行上一条计划",
        user_metadata={
            "source": "slash_command",
            "command": "plan",
            "action": "execute",
            "executed_proposed_plan_id": plan.get("id") if isinstance(plan, dict) else None,
        },
    ):
        yield event


def _emit_control_plane_event(
    project_id: str,
    command: SlashCommand,
    *,
    branch: str,
    status: str,
    **data: Any,
) -> None:
    """Mirror deterministic slash branches into lifecycle events."""
    try:
        event_stream.emit(
            "control_plane_branch",
            project_id=project_id,
            correlation_id=f"slash:{command.name}:{int(time.time())}",
            data={
                "protocol": "slash_command",
                "protocol_reason": "deterministic chat command bypasses LLM by contract",
                "branch": branch,
                "status": status,
                "command": command.name,
                "args": list(command.args),
                **{key: value for key, value in data.items() if value is not None},
            },
        )
    except Exception:
        # Debug events must not break deterministic control-plane commands.
        pass


async def _reset_events(
    project_id: str,
    command: SlashCommand,
) -> AsyncGenerator[dict[str, Any], None]:
    action = (command.args[0].lower() if command.args else "status").strip()

    if action in {"status", "help"}:
        _, state = await _read_state(project_id)
        pending = state.get("_pending_reset_confirm") if isinstance(state, dict) else None
        text = (
            "重置命令：/reset failed 清理失败节点；/reset full 请求全量重置；"
            "/reset confirm 确认全量重置；/reset cancel 取消确认。"
            f"\n当前待确认全量重置：{'有' if pending else '无'}。"
        )
        await _emit_text(project_id, command, text)
        yield {"type": "slash_command", "command": "reset", "action": action, "ok": True, "pending": bool(pending)}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    if action in {"failed", "cleanup", "test"}:
        result = await reset_project(project_id=project_id, scope="failed", reason="slash /reset failed")
        async for event in _reset_result_events(project_id, command, action, result):
            yield event
        return

    if action == "full":
        result = await reset_project(project_id=project_id, scope="full", reason="slash /reset full")
        if result.get("requires_user_confirm"):
            pending_reset = await _store_pending_reset(project_id, result)
            _emit_control_plane_event(
                project_id,
                command,
                branch="reset_full_confirmation_created",
                status="pending_confirmation",
                confirmation_kind="reset_project",
                risk="destructive",
                scope="full",
                reason=result.get("reason"),
                expires_at=pending_reset.get("expires_at"),
            )
            text = (
                "全量重置需要确认。该操作会清空项目蓝图、蓝图草稿、任务、面板、画布节点、连边、"
                "人物、剧本、分镜等，并归档本项目聊天上下文；trace 和诊断日志会保留，"
                "标题恢复为「未命名项目」。"
                "确认执行请发送 /reset confirm；取消请发送 /reset cancel。"
            )
            await _emit_text(project_id, command, text)
            yield {
                "type": "confirm_required",
                "action": "reset_project",
                "scope": "full",
                "reason": result.get("reason"),
                "expires_at": pending_reset.get("expires_at"),
            }
            yield {"type": "slash_command", "command": "reset", "action": "full", "ok": False, "requires_confirm": True}
            yield {"type": "text_delta", "content": text}
            yield {"type": "done", "status": "completed"}
            return
        async for event in _reset_result_events(project_id, command, action, result):
            yield event
        return

    if action == "confirm":
        _, state = await _read_state(project_id)
        pending = state.get("_pending_reset_confirm") if isinstance(state, dict) else None
        if not pending:
            text = "没有待确认的全量重置。先发送 /reset full。"
            await _emit_text(project_id, command, text, ok=False)
            _emit_control_plane_event(
                project_id,
                command,
                branch="reset_confirm_missing_pending",
                status="failed",
                confirmation_kind="reset_project",
                error_kind="no_pending_reset",
            )
            yield {"type": "slash_command", "command": "reset", "action": "confirm", "ok": False, "error": "no_pending_reset"}
            yield {"type": "text_delta", "content": text}
            yield {"type": "done", "status": "failed"}
            return
        if is_pending_confirmation_expired(pending):
            if isinstance(state, dict):
                state.pop("_pending_reset_confirm", None)
                await _write_state(project_id, state)
            _emit_control_plane_event(
                project_id,
                command,
                branch="reset_full_confirmation_expired",
                status="expired",
                confirmation_kind="reset_project",
                scope="full",
                expires_at=pending.get("expires_at"),
            )
            try:
                event_stream.emit(
                    "confirmation_expired",
                    project_id=project_id,
                    data={
                        "protocol": "slash_command",
                        "protocol_reason": "deterministic chat command checks pending confirmation expiry",
                        "confirmation_kind": "reset_project",
                        "state_key": "_pending_reset_confirm",
                        "scope": "full",
                        "expires_at": pending.get("expires_at"),
                    },
                )
            except Exception:
                pass
            text = "待确认的全量重置已过期。请重新发送 /reset full 后再确认。"
            await _emit_text(project_id, command, text, ok=False)
            yield {"type": "slash_command", "command": "reset", "action": "confirm", "ok": False, "error": "confirmation_expired"}
            yield {"type": "text_delta", "content": text}
            yield {"type": "done", "status": "failed"}
            return
        result = await reset_project(
            project_id=project_id,
            scope="full",
            _confirm_token=_make_reset_confirm_token(project_id),
            reason=pending.get("reason") or "slash /reset confirm",
        )
        # reset_project writes a new project state. Do not write the stale
        # pre-reset `state` back here, or it restores the old blueprint/tasks.
        _, fresh_state = await _read_state(project_id)
        if isinstance(fresh_state, dict) and fresh_state.get("_pending_reset_confirm") is not None:
            fresh_state.pop("_pending_reset_confirm", None)
            await _write_state(project_id, fresh_state)
        _emit_control_plane_event(
            project_id,
            command,
            branch="reset_full_confirmation_resolved",
            status="completed" if result.get("ok") else "failed",
            confirmation_kind="reset_project",
            action="confirm",
            scope="full",
            cleared_all=bool(result.get("cleared_all")),
            error_kind=result.get("error_kind"),
        )
        async for event in _reset_result_events(project_id, command, action, result):
            yield event
        return

    if action == "cancel":
        _, state = await _read_state(project_id)
        if isinstance(state, dict):
            state.pop("_pending_reset_confirm", None)
            await _write_state(project_id, state)
        _emit_control_plane_event(
            project_id,
            command,
            branch="reset_full_confirmation_resolved",
            status="cancelled",
            confirmation_kind="reset_project",
            action="cancel",
            scope="full",
        )
        text = "已取消待确认的全量重置。"
        await _emit_text(project_id, command, text)
        yield {"type": "slash_command", "command": "reset", "action": "cancel", "ok": True}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "completed"}
        return

    text = "重置命令不合法。可用：/reset failed、/reset full、/reset confirm、/reset cancel。"
    await _emit_text(project_id, command, text, ok=False)
    yield {"type": "slash_command", "command": "reset", "action": action, "ok": False, "error": "invalid_reset_action"}
    yield {"type": "text_delta", "content": text}
    yield {"type": "done", "status": "failed"}


async def _doctor_events(
    project_id: str,
    command: SlashCommand,
) -> AsyncGenerator[dict[str, Any], None]:
    snapshot = await build_doctor_snapshot(project_id)
    if not snapshot.get("ok"):
        text = f"诊断失败：{snapshot.get('error')}"
        await _emit_text(project_id, command, text, ok=False)
        yield {"type": "slash_command", "command": "doctor", "ok": False, "error": snapshot.get("error")}
        yield {"type": "text_delta", "content": text}
        yield {"type": "done", "status": "failed"}
        return

    text = snapshot.get("text") or "项目诊断完成。"
    await _emit_text(project_id, command, text)
    yield {
        "type": "doctor_result",
        **snapshot,
    }
    yield {"type": "slash_command", "command": "doctor", "ok": True}
    yield {"type": "text_delta", "content": text}
    yield {"type": "done", "status": "completed"}


async def build_doctor_snapshot(project_id: str) -> dict[str, Any]:
    state = await project_get_state(project_id)
    if state.get("error"):
        return {"ok": False, "project_id": project_id, "error": state["error"]}

    node_summary = await _node_summary(project_id)
    pending_blueprint = pending_blueprint_plan(state)
    pending_reset = state.get("_pending_reset_confirm")
    project_blueprint = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
    blueprint_progress = state.get("blueprint_progress") if isinstance(state.get("blueprint_progress"), dict) else {}
    blueprint_generation_progress = (
        state.get("blueprint_generation_progress")
        if isinstance(state.get("blueprint_generation_progress"), dict)
        else {}
    )
    blueprint_section_results = (
        state.get("blueprint_section_results")
        if isinstance(state.get("blueprint_section_results"), list)
        else []
    )
    blueprint_stale_nodes = state.get("blueprint_stale_nodes")
    stale_node_count = len(blueprint_stale_nodes) if isinstance(blueprint_stale_nodes, (list, dict)) else 0
    feature_flags = _feature_flag_summary(await get_feature_states())
    generation_sections = blueprint_generation_progress.get("sections") if isinstance(blueprint_generation_progress.get("sections"), list) else []
    completed_generation_sections = sum(1 for section in generation_sections if isinstance(section, dict) and section.get("status") == "completed")
    current_section = str(blueprint_generation_progress.get("current_section") or "")
    current_section_title = BLUEPRINT_SECTION_TITLES.get(current_section, current_section)
    blueprint_status_line = (
        f"- 蓝图进度：{blueprint_progress.get('status') or blueprint_generation_progress.get('status') or '无'}"
        f"，章节 {completed_generation_sections}/{len(generation_sections)}"
        + (f"，当前 {current_section_title}" if current_section_title else "")
        + (f"，stale 节点 {stale_node_count}" if stale_node_count else "")
    )
    text = "\n".join([
        "项目诊断",
        (
            f"- 项目蓝图：{project_blueprint.get('theme_title') or project_blueprint.get('id')} "
            f"v{project_blueprint.get('version')} ({project_blueprint.get('status')})"
            if project_blueprint else "- 项目蓝图：无"
        ),
        blueprint_status_line,
        f"- 节点总数：{node_summary['total']}",
        f"- 节点状态：{_compact_counts(node_summary['by_status'])}",
        f"- 节点类型：{_compact_counts(node_summary['by_type'])}",
        f"- 待确认蓝图：{'有' if pending_blueprint else '无'}",
        f"- 待确认重置：{'有' if pending_reset else '无'}",
        (
            f"- 功能开关：{feature_flags['enabled']}/{feature_flags['total']} 开启"
            + (f"，{feature_flags['killed']} 个 kill switch 生效" if feature_flags["killed"] else "")
        ),
    ])
    return {
        "ok": True,
        "project_id": project_id,
        "node_summary": node_summary,
        "has_pending_blueprint_confirmation": bool(pending_blueprint),
        "pending_blueprint_confirmation_id": pending_blueprint.get("id") if isinstance(pending_blueprint, dict) else None,
        "has_pending_reset": bool(pending_reset),
        "project_blueprint": project_blueprint,
        "blueprint_progress": blueprint_progress,
        "blueprint_generation_progress": blueprint_generation_progress,
        "blueprint_section_results": blueprint_section_results,
        "blueprint_stale_node_count": stale_node_count,
        "feature_flags": feature_flags,
        "text": text,
    }


async def _reset_result_events(
    project_id: str,
    command: SlashCommand,
    action: str,
    result: dict[str, Any],
) -> AsyncGenerator[dict[str, Any], None]:
    ok = bool(result.get("ok"))
    if ok and result.get("cleared_all"):
        yield {"type": "canvas_action", "action": "clear_all", "payload": {}}
        if result.get("title"):
            yield {
                "type": "project_update",
                "project_id": project_id,
                "updates": {"title": result.get("title")},
            }
    elif ok:
        for node_id in result.get("_canvas_deleted_node_ids") or result.get("deleted_node_ids") or []:
            if node_id:
                yield {"type": "canvas_action", "action": "delete_node", "payload": {"id": node_id}}
    text = _format_reset_result(result)
    if not (ok and result.get("scope") == "full"):
        await _emit_text(project_id, command, text, ok=ok)
    if ok and result.get("scope") == "full":
        reset_event = reset_project_event(project_id, result, scope="full", message=text)
        if reset_event:
            yield reset_event
    yield {"type": "slash_command", "command": "reset", "action": action, "ok": ok, "result": result}
    yield {"type": "text_delta", "content": text}
    yield {"type": "done", "status": "completed" if ok else "failed"}


async def _node_summary(project_id: str) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    async with session_scope() as session:
        rows = (await session.exec(
            select(WorkflowNode).where(WorkflowNode.project_id == project_id)
        )).all()
    for node in rows:
        by_type[node.type] = by_type.get(node.type, 0) + 1
        by_status[node.status] = by_status.get(node.status, 0) + 1
    return {"total": len(rows), "by_type": by_type, "by_status": by_status}


def _feature_flag_summary(states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    items = [dict(item) for item in states.values()]
    items.sort(key=lambda item: str(item.get("name") or ""))
    owner_counts: dict[str, dict[str, int]] = {}
    for item in items:
        owner = str(item.get("owner") or "unknown")
        bucket = owner_counts.setdefault(
            owner,
            {"total": 0, "enabled": 0, "disabled": 0, "killed": 0},
        )
        bucket["total"] += 1
        if item.get("enabled"):
            bucket["enabled"] += 1
        else:
            bucket["disabled"] += 1
        if item.get("killed"):
            bucket["killed"] += 1

    disabled_names = [str(item["name"]) for item in items if not item.get("enabled")]
    killed_names = [str(item["name"]) for item in items if item.get("killed")]
    return {
        "total": len(items),
        "enabled": len(items) - len(disabled_names),
        "disabled": len(disabled_names),
        "killed": len(killed_names),
        "owners": owner_counts,
        "disabled_names": disabled_names,
        "killed_names": killed_names,
        "items": items,
    }


async def _store_pending_reset(project_id: str, result: dict[str, Any]) -> dict[str, Any]:
    _, state = await _read_state(project_id)
    pending = {
        "scope": result.get("scope", "full"),
        "reason": result.get("reason", "slash command requested full reset"),
        "ts": int(time.time()),
        "expires_at": confirmation_expires_at(),
    }
    state["_pending_reset_confirm"] = pending
    await _write_state(project_id, state)
    return pending


def _make_reset_confirm_token(project_id: str) -> str:
    ts = int(time.time())
    secret = (project_id or "drama-studio").encode()
    sig = hmac.new(secret, f"{project_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}:{sig}"


async def _save_message(
    project_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if isinstance(metadata, dict) and metadata.get("source") == "slash_command":
        metadata = dict(metadata)
        metadata.setdefault("model_visible", False)
    async with session_scope() as session:
        session.add(
            Message(
                project_id=project_id,
                role=role,
                content=content,
                metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
            )
        )
        await session.commit()


async def _latest_proposed_plan(project_id: str) -> tuple[dict[str, Any] | None, str]:
    async with session_scope() as session:
        rows = await session.exec(
            select(Message)
            .where(
                Message.project_id == project_id,
                Message.role == "assistant",
                Message.archived == False,  # noqa: E712
            )
            .order_by(Message.created_at.desc())
            .limit(50)
        )
        for row in rows.all():
            raw = row.metadata_json
            if not raw:
                continue
            try:
                metadata = json.loads(raw)
            except Exception:
                continue
            if not isinstance(metadata, dict):
                continue
            plan = metadata.get("proposedPlan")
            markdown = proposed_plan_markdown(plan if isinstance(plan, dict) else None)
            if markdown:
                return plan, markdown
    return None, ""


async def _emit_text(
    project_id: str,
    command: SlashCommand,
    text: str,
    *,
    ok: bool = True,
) -> None:
    await _save_message(
        project_id,
        "assistant",
        text,
        {
            "source": "slash_command",
            "command": command.name,
            "ok": ok,
        },
    )


def _format_reset_result(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"重置失败：{result.get('error') or result.get('reason') or '未知错误'}"
    scope = result.get("scope")
    deleted_nodes = len(result.get("deleted_node_ids") or [])
    deleted_edges = result.get("deleted_edges", 0)
    if scope == "full":
        return (
            f"已完成全量重置。蓝图已清空，删除节点 {deleted_nodes} 个，"
            f"连边 {deleted_edges} 条，标题已恢复为「{result.get('title') or '未命名项目'}」。"
        )
    return f"已清理失败节点。删除节点 {deleted_nodes} 个，连边 {deleted_edges} 条。"


def _compact_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "无"
    return "，".join(f"{key}:{value}" for key, value in sorted(counts.items()))


def _help_text(prefix: str | None = None) -> str:
    lines = []
    if prefix:
        lines.append(prefix)
    lines.extend([
        "可用 slash commands：",
        "- /plan [目标|execute|exit]",
        "- /workflow [exit]",
        "- /reset [failed|full|confirm|cancel]",
        "- /doctor",
    ])
    return "\n".join(lines)
