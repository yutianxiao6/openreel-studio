"""项目级聊天队列 — 进程内内存(用户决定不持久化)。

设计意图(Claude Code 风格):
- 当 LLM 正在跑(stream 进行中),用户继续发的消息进**队列**而不阻塞前端
- 当前 stream 跑完一轮后,主循环逐条追加 queued user input,不把多条消息改写成一段自然语言
- 重启清空,无持久化(本人短期会话语义)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


# 每个 project 一条队列;list[dict] 而非 list[str],为以后加 attachments 留余地
_queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
_active_streams: set[str] = set()  # 当前有 stream 在跑的 project_id 集合
_cancel_requests: dict[str, str] = {}
_lock = asyncio.Lock()

# 单 project 队列上限,防止前端疯狂打字导致内存爆
MAX_QUEUE = 20


async def is_streaming(project_id: str) -> bool:
    async with _lock:
        return project_id in _active_streams


async def mark_streaming(project_id: str, on: bool) -> None:
    async with _lock:
        if on:
            _active_streams.add(project_id)
        else:
            _active_streams.discard(project_id)
            _cancel_requests.pop(project_id, None)


async def enqueue(
    project_id: str,
    message: str,
    attachments: list | None = None,
    *,
    referenced_node_ids: list[str] | None = None,
    display_message: str | None = None,
    user_metadata: dict[str, Any] | None = None,
) -> dict:
    """前端在 LLM 跑期间往这里塞新消息。"""
    async with _lock:
        q = _queues[project_id]
        if len(q) >= MAX_QUEUE:
            return {"ok": False, "error": f"队列已满({MAX_QUEUE}),请等当前对话完成", "queued_count": len(q)}
        q.append({
            "message": message,
            "attachments": list(attachments or []),
            "referenced_node_ids": list(referenced_node_ids or []),
            "display_message": display_message,
            "user_metadata": dict(user_metadata) if isinstance(user_metadata, dict) else None,
        })
        return {"ok": True, "queued_count": len(q)}


async def request_cancel(project_id: str, reason: str = "") -> dict:
    """Ask the active stream for this project to stop at the next safe point."""
    async with _lock:
        is_active = project_id in _active_streams
        if is_active:
            _cancel_requests[project_id] = reason.strip() or "用户请求停止当前任务"
        else:
            _cancel_requests.pop(project_id, None)
        return {
            "ok": True,
            "project_id": project_id,
            "streaming": is_active,
            "queued_count": len(_queues.get(project_id) or []),
        }


async def get_cancel_reason(project_id: str) -> str | None:
    async with _lock:
        return _cancel_requests.get(project_id)


async def clear_cancel(project_id: str) -> None:
    async with _lock:
        _cancel_requests.pop(project_id, None)


async def pop_all(project_id: str) -> list[dict[str, Any]]:
    """主循环在一轮 stream 结束时取出全部待处理消息。"""
    async with _lock:
        q = _queues.get(project_id) or []
        items = list(q)
        _queues[project_id] = []
        return items


async def peek_count(project_id: str) -> int:
    async with _lock:
        return len(_queues.get(project_id) or [])


def merge_messages(items: list[dict[str, Any]]) -> tuple[str, list]:
    """Legacy single-item adapter kept for compatibility with older tests/imports."""
    if not items:
        return "", []
    item = items[0]
    return item.get("message", ""), item.get("attachments") or []


def queued_preview(items: list[dict[str, Any]], *, limit: int = 300) -> str:
    parts: list[str] = []
    for item in items:
        text = " ".join(str(item.get("display_message") or item.get("message") or "").split())
        if text:
            parts.append(text)
        if len(" / ".join(parts)) >= limit:
            break
    preview = " / ".join(parts)
    if len(preview) > limit:
        return preview[: limit - 1] + "…"
    return preview
