"""Embedded Codex chat routes.

These routes stream Codex app-server events and persist a Codex-only chat
history.  They do not enter OpenReel's routes_chat/orchestrator Agent Loop.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Message, Project
from app.db.session import get_session, session_scope
from app.services.codex_app_server import CodexBridgeError, codex_app_server
from app.services.project_service import ProjectService


router = APIRouter()


class CodexConnectRequest(BaseModel):
    restart: bool = False


class CodexMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    client_user_message_id: str | None = None


def _metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


async def _save_codex_message(
    project_id: str,
    role: str,
    content: str,
    *,
    thread_id: str | None = None,
    turn_id: str | None = None,
    message_id: str | None = None,
) -> Message:
    metadata = {
        "source": "codex",
        "thread_id": thread_id,
        "turn_id": turn_id,
    }
    async with session_scope() as db:
        if message_id:
            existing = await db.get(Message, message_id)
            if existing is not None:
                if existing.project_id != project_id:
                    raise CodexBridgeError("Codex message id belongs to another project")
                return existing
        message = Message(
            id=message_id or None,
            project_id=project_id,
            role=role,
            content=content,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        ) if message_id else Message(
            project_id=project_id,
            role=role,
            content=content,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        db.add(message)
        await db.commit()
        await db.refresh(message)
        return message


async def _project_thread(project_id: str) -> tuple[str, bool]:
    async with session_scope() as db:
        service = ProjectService(db)
        state = await service.get_project_state(project_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Project not found")
        bridge_state = state.get("codex_bridge") if isinstance(state.get("codex_bridge"), dict) else {}
        existing = str((bridge_state or {}).get("thread_id") or "").strip() or None
        thread_id, created = await codex_app_server.ensure_thread(project_id, existing)
        if created or existing != thread_id:
            await service.update_project_state(project_id, {
                "codex_bridge.thread_id": thread_id,
                "codex_bridge.connected_at": datetime.utcnow().isoformat(),
                "codex_bridge.agent": "codex",
                "codex_bridge.control_mode": "embedded_dynamic_tools",
            })
        return thread_id, created


def _activity_from_item(item: Any, *, completed: bool) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type == "dynamicToolCall":
        return {
            "type": "activity",
            "kind": "tool",
            "name": str(item.get("tool") or "OpenReel tool"),
            "status": str(item.get("status") or ("completed" if completed else "running")),
            "success": item.get("success"),
        }
    if item_type == "mcpToolCall":
        return {
            "type": "activity",
            "kind": "tool",
            "name": str(item.get("tool") or "MCP tool"),
            "status": str(item.get("status") or ("completed" if completed else "running")),
        }
    if item_type in {"commandExecution", "fileChange", "webSearch", "imageGeneration"}:
        return {
            "type": "activity",
            "kind": item_type,
            "name": str(item.get("command") or item.get("query") or item_type)[:160],
            "status": str(item.get("status") or ("completed" if completed else "running")),
        }
    return None


@router.get("/status")
async def codex_status(auto_start: bool = Query(default=True)) -> dict[str, Any]:
    if auto_start:
        return await codex_app_server.start()
    return codex_app_server.status_snapshot()


@router.post("/connect")
async def connect_codex(req: CodexConnectRequest) -> dict[str, Any]:
    return await codex_app_server.start(restart=req.restart)


@router.get("/projects/{project_id}/messages")
async def list_codex_messages(
    project_id: str,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if await db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    rows = list((await db.exec(
        select(Message)
        .where(Message.project_id == project_id, Message.archived == False)  # noqa: E712
        .order_by(Message.created_at)
    )).all())
    result: list[dict[str, Any]] = []
    for message in rows:
        metadata = _metadata(message.metadata_json)
        if metadata.get("source") != "codex":
            continue
        payload = message.model_dump()
        payload["metadata"] = metadata
        result.append(payload)
    return result


@router.post("/projects/{project_id}/stream")
async def stream_codex_message(
    project_id: str,
    req: CodexMessageRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    if await db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    status = await codex_app_server.start()
    if not status.get("connected"):
        raise HTTPException(status_code=503, detail=status.get("detail") or status.get("label"))

    async def event_stream() -> AsyncIterator[str]:
        lock = codex_app_server.project_lock(project_id)
        if lock.locked():
            yield _sse({"type": "error", "message": "Codex 正在处理上一条消息，请等待完成或先停止。"})
            return
        async with lock:
            thread_id = ""
            turn_id = ""
            assistant_text = ""
            completed_agent_messages: list[str] = []
            try:
                thread_id, _created = await _project_thread(project_id)
                user_row = await _save_codex_message(
                    project_id,
                    "user",
                    message,
                    thread_id=thread_id,
                    message_id=req.client_user_message_id,
                )
                yield _sse({
                    "type": "connected",
                    "label": "Codex 已连接",
                    "thread_id": thread_id,
                    "message_id": user_row.id,
                })
                async with codex_app_server.subscribe(thread_id) as queue:
                    response = await codex_app_server.start_turn(
                        project_id,
                        thread_id,
                        message,
                        client_user_message_id=req.client_user_message_id,
                    )
                    turn_id = str((response.get("turn") or {}).get("id") or "")
                    yield _sse({"type": "turn_started", "thread_id": thread_id, "turn_id": turn_id})
                    while True:
                        if await request.is_disconnected():
                            await codex_app_server.interrupt_project(project_id)
                            return
                        try:
                            notification = await asyncio.wait_for(queue.get(), timeout=15.0)
                        except asyncio.TimeoutError:
                            yield ": keep-alive\n\n"
                            continue
                        method = str(notification.get("method") or "")
                        params = notification.get("params") if isinstance(notification.get("params"), dict) else {}
                        event_turn_id = str(params.get("turnId") or (params.get("turn") or {}).get("id") or "")
                        if event_turn_id and turn_id and event_turn_id != turn_id:
                            continue
                        if method == "item/agentMessage/delta":
                            delta = str(params.get("delta") or "")
                            if delta:
                                assistant_text += delta
                                yield _sse({"type": "delta", "delta": delta, "turn_id": turn_id})
                            continue
                        if method in {"item/started", "item/completed"}:
                            item = params.get("item")
                            if method == "item/completed" and isinstance(item, dict) and item.get("type") == "agentMessage":
                                text = str(item.get("text") or "").strip()
                                if text:
                                    completed_agent_messages.append(text)
                            activity = _activity_from_item(item, completed=method == "item/completed")
                            if activity:
                                activity["turn_id"] = turn_id
                                yield _sse(activity)
                            continue
                        if method == "error":
                            error = params.get("error")
                            error_text = str((error or {}).get("message") if isinstance(error, dict) else error or "Codex 运行失败")
                            yield _sse({"type": "error", "message": error_text, "turn_id": turn_id})
                            continue
                        if method == "turn/completed":
                            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                            turn_status = str(turn.get("status") or "completed")
                            final_text = completed_agent_messages[-1] if completed_agent_messages else assistant_text.strip()
                            if final_text:
                                assistant_row = await _save_codex_message(
                                    project_id,
                                    "assistant",
                                    final_text,
                                    thread_id=thread_id,
                                    turn_id=turn_id,
                                )
                                assistant_message_id = assistant_row.id
                            else:
                                assistant_message_id = None
                            yield _sse({
                                "type": "done",
                                "status": turn_status,
                                "thread_id": thread_id,
                                "turn_id": turn_id,
                                "message_id": assistant_message_id,
                                "content": final_text,
                            })
                            return
            except CodexBridgeError as exc:
                yield _sse({"type": "error", "message": str(exc), "thread_id": thread_id, "turn_id": turn_id})
            except asyncio.CancelledError:
                if turn_id:
                    try:
                        await codex_app_server.interrupt_project(project_id)
                    except Exception:
                        pass
                raise
            except Exception as exc:
                yield _sse({"type": "error", "message": f"Codex 连接异常：{exc}", "thread_id": thread_id, "turn_id": turn_id})
            finally:
                codex_app_server.clear_active_turn(project_id, turn_id or None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/projects/{project_id}/cancel")
async def cancel_codex_turn(project_id: str) -> dict[str, Any]:
    try:
        interrupted = await codex_app_server.interrupt_project(project_id)
    except CodexBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "interrupted": interrupted}
