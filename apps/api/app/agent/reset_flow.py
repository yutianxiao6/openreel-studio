"""Deterministic reset flow helpers for the agent turn coordinator."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def make_reset_confirm_token(project_id: str, *, now: int | None = None) -> str:
    ts = int(now if now is not None else time.time())
    secret = (project_id or "drama-studio").encode()
    sig = hmac.new(secret, f"{project_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}:{sig}"

def reset_confirmation_text() -> str:
    return (
        "全量重置需要确认。该操作会清空项目蓝图、蓝图草稿、任务、面板、画布节点、连边和项目内的人物、"
        "剧本、分镜等内容，并归档本项目聊天上下文，后续模型不会再看到重置前的项目记录。"
        "系统 trace 和诊断日志会保留。项目只保留空壳，标题恢复为「未命名项目」。\n"
        "确认执行请使用确认卡，或明确告诉我确认执行；取消则回复其他新需求。"
    )


def reset_success_text(result: dict[str, Any], scope: str) -> str:
    deleted_count = len(result.get("deleted_node_ids") or [])
    if scope == "full":
        applied = result.get("new_theme_applied") or {}
        file_errors = result.get("blueprint_file_delete_errors") or []
        warning = ""
        if file_errors:
            warning = f" 但有 {len(file_errors)} 个蓝图文件因权限或文件系统错误未删除，请检查诊断日志。"
        if applied:
            return f"项目已重置。画布已清空（{deleted_count} 个节点）。新主题已应用：{json.dumps(applied, ensure_ascii=False)}{warning}"
        return f"项目已重置。蓝图和画布已清空（{deleted_count} 个节点），标题已恢复为「未命名项目」。{warning}请告诉我新项目想做什么？"
    return f"已清理失败或无产出的节点（{deleted_count} 个）。"


def reset_canvas_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("cleared_all"):
        return [{"type": "canvas_action", "action": "clear_all", "payload": {}}]
    return [
        {"type": "canvas_action", "action": "delete_node", "payload": {"id": node_id}}
        for node_id in (result.get("deleted_node_ids") or [])
        if node_id
    ]


def reset_project_event(
    project_id: str,
    result: dict[str, Any],
    *,
    scope: str,
    message: str | None = None,
) -> dict[str, Any] | None:
    """SSE event telling clients to clear all local project runtime state."""
    if scope != "full" or not result.get("ok"):
        return None
    return {
        "type": "project_reset",
        "project_id": project_id,
        "scope": "full",
        "title": result.get("title") or "未命名项目",
        "cleared_all": bool(result.get("cleared_all") or result.get("blueprint_cleared")),
        "message": message or reset_success_text(result, "full"),
    }
