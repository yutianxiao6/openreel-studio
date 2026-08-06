"""Codex-style scheduling primitives for one model tool-call batch."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParallelToolCall:
    call_id: str
    tool_name: str
    tool_args: dict[str, Any]
    spec: Any


@dataclass(frozen=True)
class ParallelToolResult:
    call_id: str
    value: Any = None
    error: Exception | None = None


def supports_parallel_read(spec: Any) -> bool:
    """Only explicitly safe, non-destructive reads may share a parallel batch."""

    return bool(
        spec is not None
        and getattr(spec, "is_read_only", False)
        and getattr(spec, "is_concurrency_safe", False)
        and not getattr(spec, "is_destructive", False)
        and not getattr(spec, "requires_confirmation", False)
    )


def contiguous_parallel_read_batch(
    calls: Sequence[ParallelToolCall],
    start: int,
    *,
    eligible: Callable[[ParallelToolCall], bool] | None = None,
) -> list[ParallelToolCall]:
    """Return the safe-read run beginning at ``start``; writes are barriers."""

    batch: list[ParallelToolCall] = []
    for call in calls[start:]:
        if not supports_parallel_read(call.spec):
            break
        if eligible is not None and not eligible(call):
            break
        batch.append(call)
    return batch


async def run_parallel_tool_calls(
    calls: Sequence[ParallelToolCall],
    invoke: Callable[[ParallelToolCall], Awaitable[Any]],
) -> list[ParallelToolResult]:
    """Execute safe reads concurrently and retain model call order in results."""

    async def run_one(call: ParallelToolCall) -> ParallelToolResult:
        try:
            return ParallelToolResult(call_id=call.call_id, value=await invoke(call))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the orchestrator turns it into normal tool output
            return ParallelToolResult(call_id=call.call_id, error=exc)

    return list(await asyncio.gather(*(run_one(call) for call in calls)))
