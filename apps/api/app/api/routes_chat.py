"""Chat API with SSE streaming."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any, AsyncGenerator, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent import message_queue as mq
from app.agent.event_stream import event_stream
from app.agent.orchestrator import AgentOrchestrator
from app.agent.run_broker import project_run_broker
from app.agent.slash_commands import is_slash_command, slash_command_events
from app.api.chat_events import event_to_sse, normalize_chat_event
from app.db.models import Message
from app.db.session import AsyncSessionLocal, get_session

router = APIRouter()
CHAT_STREAM_HEARTBEAT_SECONDS = 15.0
SSE_TEXT_DELTA_CHUNK_CHARS = 56
SSE_TEXT_DELTA_DELAY_SECONDS = 0.012
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    project_id: str
    message: str
    attachments: list = Field(default_factory=list)
    referenced_node_ids: list[str] = Field(default_factory=list)
    decision_inputs: dict | None = None
    client_user_message_id: str | None = None


class CancelRequest(BaseModel):
    project_id: str
    reason: str = ""


def _iter_text_delta_chunks(content: str, max_chars: int = SSE_TEXT_DELTA_CHUNK_CHARS) -> Iterator[str]:
    if max_chars <= 0 or len(content) <= max_chars:
        yield content
        return

    preferred_breaks = " \t\n，。！？；：、,.!?;:"
    start = 0
    while start < len(content):
        end = min(start + max_chars, len(content))
        if end < len(content):
            window = content[start:end]
            break_at = max(window.rfind(ch) for ch in preferred_breaks)
            if break_at >= max_chars // 2:
                end = start + break_at + 1
        chunk = content[start:end]
        if chunk:
            yield chunk
        start = end


def _expand_stream_event(event: dict) -> Iterator[tuple[dict, bool]]:
    if event.get("type") != "text_delta":
        yield event, False
        return
    content = event.get("content")
    if not isinstance(content, str) or len(content) <= SSE_TEXT_DELTA_CHUNK_CHARS:
        yield event, False
        return
    for chunk in _iter_text_delta_chunks(content):
        yield {**event, "content": chunk}, True


def _request_message(request: ChatRequest) -> str:
    return request.message


def _request_user_metadata(request: ChatRequest) -> dict | None:
    metadata: dict[str, Any] = {}
    decision_inputs = request.decision_inputs
    if isinstance(decision_inputs, dict):
        metadata["decisionInputs"] = decision_inputs
    if request.client_user_message_id:
        metadata["clientUserMessageId"] = request.client_user_message_id
    if request.referenced_node_ids:
        metadata["referencedNodeIds"] = request.referenced_node_ids
    return metadata or None


def _emit_rest_control_event(
    project_id: str,
    route: str,
    *,
    branch: str,
    status: str,
    **data: Any,
) -> None:
    """Mirror deterministic REST control-plane branches into lifecycle events."""
    try:
        event_stream.emit(
            "control_plane_branch",
            project_id=project_id,
            correlation_id=f"rest:{route}:{int(time.time())}",
            data={
                "protocol": "rest_control",
                "protocol_reason": "deterministic REST control endpoint bypasses LLM by contract",
                "route": route,
                "branch": branch,
                "status": status,
                **{key: value for key, value in data.items() if value is not None},
            },
        )
    except Exception:
        # Observability must not break deterministic control-plane endpoints.
        pass


def _emit_sse_mirror_event(
    project_id: str | None,
    *,
    stream_kind: str,
    summary: dict[str, object],
) -> None:
    if not project_id:
        return
    try:
        event_stream.emit(
            "sse_event",
            project_id=project_id,
            correlation_id=f"sse:{stream_kind}:{int(time.time())}",
            data={
                "protocol": "chat_sse",
                "protocol_reason": "normalized SSE event emitted to frontend",
                "stream_kind": stream_kind,
                **summary,
            },
        )
    except Exception:
        # SSE delivery must not depend on diagnostics.
        pass


async def _heartbeat_stream(
    source: AsyncGenerator[str, None],
) -> AsyncGenerator[str, None]:
    """Yield SSE chunks one at a time, without pre-reading the source stream.

    The previous implementation used a background producer and an unbounded
    queue. That allowed the orchestrator to continue into long tool calls before
    StreamingResponse had a chance to send the already-produced agent_round /
    tool_start chunks, so the browser could see progress only after the run was
    effectively done.
    """
    source_iter = source.__aiter__()
    next_task: asyncio.Task[str] | None = asyncio.create_task(source_iter.__anext__())
    try:
        while next_task is not None:
            done, _ = await asyncio.wait(
                {next_task},
                timeout=CHAT_STREAM_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                yield ": ping\n\n"
                continue

            try:
                chunk = next_task.result()
            except StopAsyncIteration:
                break

            yield chunk
            await asyncio.sleep(0)
            next_task = asyncio.create_task(source_iter.__anext__())
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            with suppress(asyncio.CancelledError):
                await next_task
        aclose = getattr(source_iter, "aclose", None)
        if aclose is not None:
            with suppress(Exception, asyncio.CancelledError):
                await aclose()


async def _merge_project_canvas_events(
    source: AsyncGenerator[dict, None], project_id: str
) -> AsyncGenerator[dict, None]:
    """Merge orchestrator stream with live project canvas events.

    - tool execution 主流：orchestrator.stream
    - 画布补充流：emit_canvas_event(project_id=...)
    """
    from app.agent.orchestrator import _add_subscriber, _remove_subscriber

    queue = _add_subscriber(project_id)
    source_iter = source.__aiter__()
    source_task = asyncio.create_task(source_iter.__anext__())
    canvas_task = asyncio.create_task(queue.get())
    source_done = False

    try:
        while True:
            if source_done:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    break
                yield event
                continue

            done, _ = await asyncio.wait(
                {source_task, canvas_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if canvas_task in done:
                event = canvas_task.result()
                if event is not None:
                    yield event
                if source_done:
                    canvas_task = None
                else:
                    canvas_task = asyncio.create_task(queue.get())

            if source_task in done:
                try:
                    event = source_task.result()
                except StopAsyncIteration:
                    source_done = True
                    if canvas_task is not None and not canvas_task.done():
                        canvas_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await canvas_task
                    canvas_task = None
                else:
                    yield event
                    source_task = asyncio.create_task(source_iter.__anext__())

    finally:
        if source_task is not None and not source_task.done():
            source_task.cancel()
            with suppress(asyncio.CancelledError):
                await source_task
        if canvas_task is not None and not canvas_task.done():
            canvas_task.cancel()
            with suppress(asyncio.CancelledError):
                await canvas_task
        _remove_subscriber(project_id, queue)


async def _to_sse(
    source: AsyncGenerator[dict, None],
    *,
    project_id: str | None = None,
    stream_kind: str = "chat",
) -> AsyncGenerator[str, None]:
    async for event in source:
        try:
            event = normalize_chat_event(event)
        except Exception as exc:
            logger.exception("chat_sse_contract_error event=%s", event)
            event = {
                "type": "error",
                "message": f"SSE event contract error: {event.get('type')}: {exc}",
            }
        for outbound_event, split_text in _expand_stream_event(event):
            event_type = outbound_event.get("type")
            summary: dict[str, object] = {"type": event_type}
            if event_type == "agent_round":
                summary.update({
                    "round": outbound_event.get("round"),
                    "source": outbound_event.get("source"),
                    "tools": outbound_event.get("tools"),
                })
            elif event_type in ("tool_start", "tool_done"):
                summary.update({
                    "round": outbound_event.get("round"),
                    "tool": outbound_event.get("tool"),
                })
            elif event_type == "canvas_action":
                payload = outbound_event.get("payload") if isinstance(outbound_event.get("payload"), dict) else {}
                summary.update({
                    "action": outbound_event.get("action"),
                    "node_id": payload.get("id"),
                    "status": payload.get("status"),
                })
            elif event_type == "text_delta":
                content = outbound_event.get("content") or ""
                summary["content_len"] = len(content) if isinstance(content, str) else 0
                if split_text:
                    summary["split_text"] = True
            elif event_type in ("done", "error"):
                summary["status"] = outbound_event.get("status")
                summary["message"] = outbound_event.get("message")
            logger.info("chat_sse_send %s", json.dumps(summary, ensure_ascii=False, default=str))
            _emit_sse_mirror_event(project_id, stream_kind=stream_kind, summary=summary)
            yield event_to_sse(outbound_event)
            if split_text and SSE_TEXT_DELTA_DELAY_SECONDS > 0:
                await asyncio.sleep(SSE_TEXT_DELTA_DELAY_SECONDS)


async def _event_stream(
    orchestrator: AgentOrchestrator, request: ChatRequest
) -> AsyncGenerator[dict, None]:
    await mq.mark_streaming(request.project_id, True)
    message = _request_message(request)
    display_message = request.message if message != request.message else None
    finished = False
    terminal_status = "completed"
    try:
        async for event in orchestrator.stream(
            project_id=request.project_id,
            message=message,
            attachments=request.attachments,
            referenced_node_ids=request.referenced_node_ids,
            display_message=display_message,
            user_metadata=_request_user_metadata(request),
        ):
            if event.get("type") == "done":
                finished = True
            yield event
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        terminal_status = "failed"
        yield {"type": "error", "message": str(exc)}
    finally:
        await mq.mark_streaming(request.project_id, False)
    if not finished:
        yield {"type": "done", "status": terminal_status}


async def _detached_chat_source(
    request: ChatRequest,
    *,
    slash_command: bool = False,
) -> AsyncGenerator[dict, None]:
    async with AsyncSessionLocal() as run_db:
        orchestrator = AgentOrchestrator(db=run_db)
        source = (
            _slash_event_stream(orchestrator, request)
            if slash_command
            else _event_stream(orchestrator, request)
        )
        async for event in _merge_project_canvas_events(source, request.project_id):
            yield event


async def _slash_event_stream(
    orchestrator: AgentOrchestrator, request: ChatRequest
) -> AsyncGenerator[dict, None]:
    await mq.mark_streaming(request.project_id, True)
    finished = False
    try:
        async for event in slash_command_events(
            project_id=request.project_id,
            message=request.message,
            orchestrator=orchestrator,
        ):
            if event.get("type") == "done":
                finished = True
            yield event
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
    finally:
        await mq.mark_streaming(request.project_id, False)
    if not finished:
        yield {"type": "done", "status": "completed"}


@router.post("/stream")
async def chat_stream(
    request: ChatRequest, db: AsyncSession = Depends(get_session)
):
    """主入口。

    - 当前项目没 stream 在跑 → 启动新 stream
    - 已有 stream 在跑 → 入队,返回一个超短"已入队"事件流而不是开新 stream
      (避免并发两个 LLM 同时改同一个 project)
    """
    slash_command = is_slash_command(request.message)
    message = _request_message(request)

    if await project_run_broker.is_running(request.project_id) or await mq.is_streaming(request.project_id):
        if slash_command:
            async def _slash_busy_ack() -> AsyncGenerator[str, None]:
                yield event_to_sse({"type": "error", "message": "当前项目已有任务在执行，等它结束后再发送 slash command。"})
                yield event_to_sse({"type": "done", "status": "busy"})
            return StreamingResponse(
                _slash_busy_ack(),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        enq = await mq.enqueue(
            request.project_id,
            message,
            request.attachments or [],
            referenced_node_ids=request.referenced_node_ids,
            user_metadata=_request_user_metadata(request),
        )
        async def _quick_ack() -> AsyncGenerator[str, None]:
            yield event_to_sse({"type": "queued", **enq})
            yield event_to_sse({"type": "done", "status": "queued"})
        return StreamingResponse(
            _quick_ack(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    run = await project_run_broker.start(
        request.project_id,
        lambda: _detached_chat_source(request, slash_command=slash_command),
    )

    return StreamingResponse(
        _heartbeat_stream(
            _to_sse(
                run.subscribe(replay=True),
                project_id=request.project_id,
                stream_kind="slash" if slash_command else "chat",
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/cancel")
async def chat_cancel(request: CancelRequest):
    """Request cancellation of the currently running chat/media task."""
    queue_result = await mq.request_cancel(request.project_id, request.reason)
    run_result = await project_run_broker.cancel(request.project_id, request.reason)
    return {
        **queue_result,
        "cancelled": bool(run_result.get("cancelled")),
        "running": bool(run_result.get("running")),
        "streaming": bool(queue_result.get("streaming") or run_result.get("cancelled")),
    }


@router.post("/enqueue")
async def chat_enqueue(request: ChatRequest):
    """显式入队接口(前端确认要排队时用)。

    返回 {ok, queued_count}。前端不需要等 SSE。
    """
    return await mq.enqueue(
        request.project_id,
        _request_message(request),
        request.attachments or [],
        referenced_node_ids=request.referenced_node_ids,
        user_metadata=_request_user_metadata(request),
    )


@router.get("/queue/{project_id}")
async def chat_queue_status(project_id: str):
    """看队列长度 + 是否有 stream 在跑(前端轮询/调试用)。"""
    return {
        "queued": await mq.peek_count(project_id),
        "streaming": await mq.is_streaming(project_id),
    }


@router.get("/events/{project_id}")
async def chat_project_events(project_id: str):
    """项目级长连 SSE — 前端打开项目期间一直挂着,接收画布异步事件。

    后台任务(render / video / 等)完成后通过 emit_canvas_event(project_id=...)
    fan-out 到这里,即使主聊天 stream 已结束,画布也能实时刷新。
    """
    from app.agent.orchestrator import _add_subscriber, _remove_subscriber
    queue = _add_subscriber(project_id)

    async def _stream() -> AsyncGenerator[str, None]:
        # 心跳:防止网关超时关闭连接;同时把订阅成功通知一下前端
        yield event_to_sse({"type": "subscribed", "project_id": project_id})
        run = await project_run_broker.get(project_id)
        run_iter = None
        run_task: asyncio.Task | None = None
        if run and not run.done:
            run_iter = run.subscribe(replay=True).__aiter__()
            run_task = asyncio.create_task(run_iter.__anext__())
        project_task: asyncio.Task | None = asyncio.create_task(queue.get())
        try:
            while True:
                tasks = {task for task in (project_task, run_task) if task is not None}
                if not tasks:
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=25.0)
                        yield event_to_sse(ev)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                    continue
                try:
                    done, _ = await asyncio.wait(
                        tasks,
                        timeout=25.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if not done:
                    yield ": ping\n\n"
                    continue

                if project_task in done:
                    ev = project_task.result()
                    try:
                        yield event_to_sse(ev)
                    except Exception as exc:
                        logger.exception("project_sse_contract_error event=%s", ev)
                        yield event_to_sse({
                            "type": "error",
                            "message": f"Project SSE event contract error: {ev.get('type')}: {exc}",
                        })
                    project_task = asyncio.create_task(queue.get())

                if run_task is not None and run_task in done:
                    try:
                        ev = run_task.result()
                    except StopAsyncIteration:
                        run_task = None
                    else:
                        try:
                            yield event_to_sse(ev)
                        except Exception as exc:
                            logger.exception("project_run_sse_contract_error event=%s", ev)
                            yield event_to_sse({
                                "type": "error",
                                "message": f"Project run SSE event contract error: {ev.get('type')}: {exc}",
                            })
                        if ev.get("type") in {"done", "cancelled"}:
                            run_task = None
                        elif run_iter is not None:
                            run_task = asyncio.create_task(run_iter.__anext__())
        except asyncio.CancelledError:
            pass
        finally:
            for task in (project_task, run_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if run_iter is not None:
                aclose = getattr(run_iter, "aclose", None)
                if aclose is not None:
                    with suppress(Exception, asyncio.CancelledError):
                        await aclose()
            _remove_subscriber(project_id, queue)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/history/{project_id}")
async def get_chat_history(
    project_id: str, db: AsyncSession = Depends(get_session)
):
    result = await db.exec(
        select(Message)
        .where(
            Message.project_id == project_id,
            Message.archived == False,  # noqa: E712
        )
        .order_by(Message.created_at)
    )
    messages = list(result.all())
    return {"messages": [m.model_dump() for m in messages]}
