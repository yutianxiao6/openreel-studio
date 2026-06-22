"""Project run broker for detached chat execution.

The HTTP chat stream is a subscriber, not the owner of the agent run.  If the
browser refreshes or the SSE connection is closed, the background run continues
and later subscribers can replay the in-memory event buffer for the active run.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL_EVENT_TYPES = {"done", "cancelled"}
MAX_BUFFERED_EVENTS = 2000
RECENT_RUN_TTL_SECONDS = 300


@dataclass
class ProjectRun:
    project_id: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    done_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    task: asyncio.Task | None = None
    cancel_reason: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def done(self) -> bool:
        return self.done_at is not None

    async def publish(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        async with self._lock:
            self.events.append(event)
            if len(self.events) > MAX_BUFFERED_EVENTS:
                del self.events[: len(self.events) - MAX_BUFFERED_EVENTS]
            if str(event.get("type") or "") in TERMINAL_EVENT_TYPES and self.done_at is None:
                self.done_at = time.time()
            subscribers = list(self.subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow or stale clients must not block the run.
                pass

    async def cancel(self, reason: str = "") -> bool:
        async with self._lock:
            if self.done:
                return False
            self.cancel_reason = reason.strip() or "用户请求停止当前任务"
            task = self.task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def subscribe(self, *, replay: bool = True) -> AsyncGenerator[dict[str, Any], None]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            replay_events = list(self.events) if replay else []
            if not self.done:
                self.subscribers.append(queue)
            already_done = self.done

        try:
            for event in replay_events:
                yield event
                if str(event.get("type") or "") in TERMINAL_EVENT_TYPES:
                    return
            if already_done:
                return
            while True:
                event = await queue.get()
                yield event
                if str(event.get("type") or "") in TERMINAL_EVENT_TYPES:
                    return
        finally:
            async with self._lock:
                if queue in self.subscribers:
                    self.subscribers.remove(queue)


class ProjectRunBroker:
    def __init__(self) -> None:
        self._runs: dict[str, ProjectRun] = {}
        self._lock = asyncio.Lock()

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [
            project_id
            for project_id, run in self._runs.items()
            if run.done and run.done_at is not None and now - run.done_at > RECENT_RUN_TTL_SECONDS
        ]
        for project_id in stale:
            self._runs.pop(project_id, None)

    async def get(self, project_id: str) -> ProjectRun | None:
        async with self._lock:
            self._prune_locked()
            return self._runs.get(project_id)

    async def is_running(self, project_id: str) -> bool:
        run = await self.get(project_id)
        return bool(run and not run.done)

    async def cancel(self, project_id: str, reason: str = "") -> dict[str, Any]:
        run = await self.get(project_id)
        cancelled = await run.cancel(reason) if run else False
        return {
            "ok": True,
            "project_id": project_id,
            "cancelled": cancelled,
            "running": bool(run and not run.done),
        }

    async def start(
        self,
        project_id: str,
        event_source_factory: Callable[[], AsyncGenerator[dict[str, Any], None]],
    ) -> ProjectRun:
        async with self._lock:
            self._prune_locked()
            existing = self._runs.get(project_id)
            if existing and not existing.done:
                return existing
            run = ProjectRun(project_id=project_id)
            self._runs[project_id] = run
            run.task = asyncio.create_task(
                self._run_source(run, event_source_factory),
                name=f"project-run:{project_id}:{run.run_id}",
            )
            return run

    async def _run_source(
        self,
        run: ProjectRun,
        event_source_factory: Callable[[], AsyncGenerator[dict[str, Any], None]],
    ) -> None:
        terminal_seen = False
        try:
            async for event in event_source_factory():
                if str(event.get("type") or "") in TERMINAL_EVENT_TYPES:
                    terminal_seen = True
                await run.publish(event)
        except asyncio.CancelledError:
            terminal_seen = True
            reason = run.cancel_reason or "用户请求停止当前任务"
            await run.publish({"type": "cancelled", "message": f"已停止当前任务：{reason}"})
        except Exception as exc:
            logger.exception("project run failed: project=%s run=%s", run.project_id, run.run_id)
            terminal_seen = True
            await run.publish({"type": "error", "message": str(exc)})
            await run.publish({"type": "done", "status": "failed"})
        finally:
            if not terminal_seen:
                await run.publish({"type": "done", "status": "completed"})


project_run_broker = ProjectRunBroker()
