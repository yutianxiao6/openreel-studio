"""Parallel executor — run independent plan steps concurrently.

When the planner outputs multiple steps that don't depend on each other
(e.g., generating 3 characters, or outline + characters in parallel),
this executor dispatches them via asyncio.gather instead of serial execution.

It uses the TaskGraph for dependency tracking and the BackgroundManager
for non-blocking execution of slow operations.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator

from app.api.chat_events import normalize_chat_event
from app.agent.task_graph import task_graph, Task
from app.agent.event_stream import event_stream
from app.mcp_tools.registry import registry

logger = logging.getLogger(__name__)


def _event(event: dict[str, Any]) -> dict[str, Any]:
    return normalize_chat_event(event)


def analyze_dependencies(steps: list[dict]) -> list[list[int]]:
    """Analyze steps and return execution waves (groups that can run in parallel).

    Returns a list of waves, where each wave is a list of step indices
    that can execute concurrently.
    """
    n = len(steps)
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    deps: dict[int, set[int]] = {i: set() for i in range(n)}

    for i, step in enumerate(steps):
        tool = step.get("tool", "")
        for j in range(i):
            prev_tool = steps[j].get("tool", "")
            if _has_dependency(prev_tool, tool):
                deps[i].add(j)

    waves: list[list[int]] = []
    scheduled: set[int] = set()

    while len(scheduled) < n:
        wave = []
        for i in range(n):
            if i in scheduled:
                continue
            if deps[i].issubset(scheduled):
                wave.append(i)
        if not wave:
            remaining = [i for i in range(n) if i not in scheduled]
            wave = remaining
        waves.append(wave)
        scheduled.update(wave)

    return waves


def _has_dependency(upstream_tool: str, downstream_tool: str) -> bool:
    """Determine if downstream_tool depends on upstream_tool's output.

    Same-type tools never depend on each other (they can always run in parallel).
    """
    if upstream_tool == downstream_tool:
        return False

    rules = [
        (["generate_outline"], ["generate_episode_script", "rewrite_episode"]),
        (["generate_episode_script", "rewrite_episode"], ["generate_storyboard", "generate_shot"]),
        (["generate_storyboard", "generate_shot"], ["generate_image_prompt", "generate_shot_image_prompt"]),
        (["generate_image_prompt", "generate_shot_image_prompt"], ["generate_image", "generate_first_frame"]),
        (["generate_shot_video_prompt"], ["generate_video"]),
        (["generate_characters", "generate_character"], ["generate_outline"]),
    ]

    for upstream_keywords, downstream_keywords in rules:
        upstream_match = any(k in upstream_tool for k in upstream_keywords)
        downstream_match = any(k in downstream_tool for k in downstream_keywords)
        if upstream_match and downstream_match:
            return True
    return False


async def execute_step(
    step: dict,
    project_id: str,
) -> dict[str, Any]:
    """Execute a single step via the registry. Returns the result dict."""
    tool = step.get("tool", "")
    step_input = dict(step.get("input") or {})
    step_input.setdefault("project_id", project_id)

    spec = registry.get(tool)
    if not spec:
        return {"error": f"Unknown tool: {tool}"}

    import inspect
    try:
        sig = inspect.signature(spec.handler)
    except (TypeError, ValueError):
        sig = None

    if sig:
        params = sig.parameters
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            step_input = {k: v for k, v in step_input.items() if k in params}

    try:
        result = await registry.call(tool, **step_input)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        return {"error": str(exc)}


async def execute_parallel(
    steps: list[dict],
    project_id: str,
    max_concurrency: int = 5,
) -> AsyncGenerator[dict[str, Any], None]:
    """Execute plan steps with maximum parallelism respecting dependencies.

    Yields SSE-compatible events as steps start and complete.
    """
    waves = analyze_dependencies(steps)
    semaphore = asyncio.Semaphore(max_concurrency)

    total = len(steps)
    completed = 0

    yield _event({
        "type": "parallel_start",
        "total_steps": total,
        "waves": len(waves),
        "project_id": project_id,
    })

    for wave_idx, wave in enumerate(waves):
        wave_steps = [(i, steps[i]) for i in wave]

        async def run_one(idx: int, step: dict) -> tuple[int, dict]:
            async with semaphore:
                return idx, await execute_step(step, project_id)

        tasks = [run_one(idx, step) for idx, step in wave_steps]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for item in results:
            if isinstance(item, Exception):
                yield _event({"type": "step_failed", "error": str(item)})
            else:
                idx, result = item
                step = steps[idx]
                completed += 1
                event_stream.emit(
                    "step.completed",
                    project_id=project_id,
                    data={"tool": step.get("tool"), "step_index": idx},
                )
                yield _event({
                    "type": "step_completed",
                    "step_index": idx,
                    "tool": step.get("tool", ""),
                    "title": step.get("title", ""),
                    "result": result,
                    "progress": f"{completed}/{total}",
                })

    yield _event({
        "type": "parallel_done",
        "completed": completed,
        "total": total,
    })
