"""Agent debugging API: doctor snapshot and compact trace viewer."""
from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.agent_trace import traces_root
from app.agent.context_compact import tool_results_dir
from app.agent.prompt_dump import prompt_dumps_root
from app.agent.slash_commands import build_doctor_snapshot
from app.agent.trace_store import list_trace_runs, read_trace_events, summarize_token_usage
from app.agent.workflow_state_evidence import build_workflow_state_evidence
from app.db.session import get_session
from app.services.project_service import ProjectService

router = APIRouter()

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_SAFE_ARTIFACT_KINDS = {"traces", "prompt_dumps", "tool_results"}
_TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".md"}
_MAX_ARTIFACT_READ_BYTES = 128 * 1024


@router.get("/debug/{project_id}/doctor")
async def agent_doctor(project_id: str) -> dict[str, Any]:
    """Return the same diagnostic snapshot used by `/doctor`, without chat writes."""
    return await build_doctor_snapshot(project_id)


@router.get("/debug/{project_id}/workflow-evidence")
async def get_workflow_state_evidence(
    project_id: str,
    template_id: Annotated[str, Query(max_length=120)] = "",
    instance_id: Annotated[str, Query(max_length=160)] = "",
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return backend workflow runtime, canvas node, and edge evidence."""
    result = await build_workflow_state_evidence(
        project_id,
        db,
        template_id=template_id,
        instance_id=instance_id,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/debug/{project_id}/traces")
async def list_agent_traces(
    project_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    source: Annotated[str, Query(pattern="^(auto|db|files)$")] = "auto",
) -> dict[str, Any]:
    if source in {"auto", "db"}:
        db_result = list_trace_runs(project_id, limit=limit)
        if db_result and (source == "db" or db_result.get("total", 0) > 0):
            return db_result
        if source == "db":
            return {"project_id": project_id, "traces": [], "total": 0, "limit": limit, "source": "db"}

    trace_dir = traces_root() / project_id
    if not trace_dir.exists():
        return {"project_id": project_id, "traces": [], "total": 0, "limit": limit, "source": "files"}

    files = sorted(
        (path for path in trace_dir.glob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    traces = [_trace_summary(project_id, path) for path in files[:limit]]
    return {
        "project_id": project_id,
        "traces": traces,
        "total": len(files),
        "limit": limit,
        "source": "files",
    }


@router.get("/debug/{project_id}/traces/{run_id}")
async def get_agent_trace(
    project_id: str,
    run_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    source: Annotated[str, Query(pattern="^(auto|db|files)$")] = "auto",
) -> dict[str, Any]:
    if source in {"auto", "db"}:
        db_result = read_trace_events(project_id, run_id, limit=limit)
        if db_result:
            return db_result
        if source == "db":
            raise HTTPException(status_code=404, detail="Trace not found")

    path = _trace_path(project_id, run_id)
    events, total = _read_trace_tail(path, limit=limit)
    return {
        "project_id": project_id,
        "run_id": run_id,
        "path": _display_path(project_id, path),
        "source": "files",
        "events": events,
        "event_count": total,
        "returned": len(events),
        "truncated": total > len(events),
        "limit": limit,
    }


@router.get("/debug/{project_id}/token-usage")
async def get_agent_token_usage(
    project_id: str,
    run_id: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    include_before_clear: bool = False,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if run_id and not _SAFE_RUN_ID.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid trace run_id")
    since_ts: str | None = None
    if not run_id and not include_before_clear:
        state = await ProjectService(db).get_project_state(project_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Project not found")
        raw_cleared_at = state.get("context_cleared_at") if isinstance(state, dict) else None
        if isinstance(raw_cleared_at, str) and raw_cleared_at.strip():
            since_ts = raw_cleared_at.strip()
    summary = summarize_token_usage(project_id, run_id=run_id, since_ts=since_ts, limit=limit)
    if summary is not None:
        summary["context_cleared_at"] = since_ts
        summary["include_before_clear"] = include_before_clear
        return summary
    return {
        "project_id": project_id,
        "run_id": run_id,
        "source": "unavailable",
        "event_count": 0,
        "limit": limit,
        "since_ts": since_ts,
        "context_cleared_at": since_ts,
        "include_before_clear": include_before_clear,
        "totals": {},
        "by_run": [],
        "last_usage": None,
        "latest_call_tokens": None,
        "latest_call_context": None,
        "session_cumulative_tokens": None,
        "session_context_peak": None,
        "last_event_at": None,
    }


@router.get("/debug/{project_id}/artifacts")
async def list_agent_artifacts(
    project_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "limit": limit,
        "artifacts": {
            "traces": _artifact_group(
                traces_root() / project_id,
                display_prefix=f"data/agent_traces/{project_id}",
                limit=limit,
            ),
            "prompt_dumps": _artifact_group(
                prompt_dumps_root() / project_id,
                display_prefix=f"data/prompt_dumps/{project_id}",
                limit=limit,
            ),
            "tool_results": _artifact_group(
                tool_results_dir() / project_id,
                display_prefix=f"data/tool_results/{project_id}",
                limit=limit,
                recursive=True,
            ),
        },
    }


@router.get("/debug/{project_id}/artifacts/read")
async def read_agent_artifact(
    project_id: str,
    kind: Annotated[str, Query(pattern="^(traces|prompt_dumps|tool_results)$")],
    path: Annotated[str, Query(min_length=1, max_length=500)],
    max_bytes: Annotated[int, Query(ge=1, le=_MAX_ARTIFACT_READ_BYTES)] = 32 * 1024,
    tail_lines: Annotated[int, Query(ge=0, le=1000)] = 200,
) -> dict[str, Any]:
    """Read a bounded text preview for one debug artifact.

    The client may pass either the artifact summary's `relative_path` or its
    display `path`. Only known project artifact roots are readable.
    """
    base_dir, display_prefix = _artifact_base(project_id, kind)
    artifact_path = _resolve_artifact_path(
        base_dir,
        display_prefix=display_prefix,
        requested_path=path,
    )
    if not artifact_path.exists() or not artifact_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact_path.suffix.lower() not in _TEXT_SUFFIXES:
        raise HTTPException(status_code=415, detail="Artifact type is not readable as text")

    content_result = _read_bounded_text(
        artifact_path,
        max_bytes=max_bytes,
        tail_lines=tail_lines,
    )
    stat = artifact_path.stat()
    relative = artifact_path.relative_to(base_dir).as_posix()
    return {
        "project_id": project_id,
        "kind": kind,
        "name": artifact_path.name,
        "path": f"{display_prefix}/{relative}",
        "relative_path": relative,
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "max_bytes": max_bytes,
        "tail_lines": tail_lines,
        **content_result,
    }


def _trace_path(project_id: str, run_id: str) -> Path:
    if not _SAFE_RUN_ID.fullmatch(run_id or ""):
        raise HTTPException(status_code=400, detail="Invalid trace run_id")
    path = traces_root() / project_id / f"{run_id}.jsonl"
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Trace not found")
    return path


def _trace_summary(project_id: str, path: Path) -> dict[str, Any]:
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    count = 0
    error_count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            count += 1
            event = _parse_event(line)
            if first is None:
                first = event
            last = event
            if event.get("error_kind") or event.get("event") in {"llm_error", "tool_error"}:
                error_count += 1
    stat = path.stat()
    return {
        "project_id": project_id,
        "run_id": path.stem,
        "path": _display_path(project_id, path),
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "event_count": count,
        "started_at": first.get("ts") if first else None,
        "last_event_at": last.get("ts") if last else None,
        "last_event": last.get("event") if last else None,
        "last_tool_name": last.get("tool_name") if last else None,
        "last_error_kind": last.get("error_kind") if last else None,
        "error_count": error_count,
    }


def _read_trace_tail(path: Path, *, limit: int) -> tuple[list[dict[str, Any]], int]:
    tail: deque[dict[str, Any]] = deque(maxlen=limit)
    total = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            tail.append(_parse_event(line))
    return list(tail), total


def _artifact_group(
    base_dir: Path,
    *,
    display_prefix: str,
    limit: int,
    recursive: bool = False,
) -> dict[str, Any]:
    if not base_dir.exists():
        return {"items": [], "total": 0}

    paths = base_dir.rglob("*") if recursive else base_dir.glob("*")
    files = sorted(
        (path for path in paths if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return {
        "items": [
            _artifact_summary(base_dir, path, display_prefix)
            for path in files[:limit]
        ],
        "total": len(files),
    }


def _artifact_summary(base_dir: Path, path: Path, display_prefix: str) -> dict[str, Any]:
    stat = path.stat()
    relative = path.relative_to(base_dir).as_posix()
    return {
        "id": path.stem,
        "name": path.name,
        "path": f"{display_prefix}/{relative}",
        "relative_path": relative,
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
    }


def _artifact_base(project_id: str, kind: str) -> tuple[Path, str]:
    if kind not in _SAFE_ARTIFACT_KINDS:
        raise HTTPException(status_code=400, detail="Invalid artifact kind")
    if kind == "traces":
        return traces_root() / project_id, f"data/agent_traces/{project_id}"
    if kind == "prompt_dumps":
        return prompt_dumps_root() / project_id, f"data/prompt_dumps/{project_id}"
    return tool_results_dir() / project_id, f"data/tool_results/{project_id}"


def _resolve_artifact_path(
    base_dir: Path,
    *,
    display_prefix: str,
    requested_path: str,
) -> Path:
    raw = (requested_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Artifact path is required")
    if raw.startswith(display_prefix + "/"):
        raw = raw[len(display_prefix) + 1:]
    if raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Absolute artifact paths are not allowed")

    relative = Path(raw)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    base_resolved = base_dir.resolve()
    target = (base_dir / relative).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Artifact path escapes project directory") from None
    return target


def _read_bounded_text(
    path: Path,
    *,
    max_bytes: int,
    tail_lines: int,
) -> dict[str, Any]:
    if tail_lines > 0:
        lines: deque[str] = deque(maxlen=tail_lines)
        total_lines = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                total_lines += 1
                lines.append(line)
        text = "".join(lines)
        text, byte_truncated = _trim_text_to_last_bytes(text, max_bytes)
        returned_lines = text.count("\n")
        if text and not text.endswith("\n"):
            returned_lines += 1
        return {
            "mode": "tail_lines",
            "content": text,
            "returned_bytes": len(text.encode("utf-8")),
            "total_lines": total_lines,
            "returned_lines": returned_lines,
            "truncated": total_lines > len(lines) or byte_truncated,
            "truncated_by_lines": total_lines > len(lines),
            "truncated_by_bytes": byte_truncated,
        }

    size = path.stat().st_size
    offset = max(0, size - max_bytes)
    with path.open("rb") as fh:
        if offset:
            fh.seek(offset)
        data = fh.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    return {
        "mode": "tail_bytes",
        "content": text,
        "returned_bytes": len(data),
        "offset": offset,
        "truncated": offset > 0,
        "truncated_by_lines": False,
        "truncated_by_bytes": offset > 0,
    }


def _trim_text_to_last_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    trimmed = encoded[-max_bytes:].decode("utf-8", errors="replace")
    return trimmed, True


def _parse_event(line: str) -> dict[str, Any]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"event": "trace_parse_error", "error": str(exc)}
    if isinstance(event, dict):
        return event
    return {"event": "trace_parse_error", "error": "Trace line is not an object"}


def _display_path(project_id: str, path: Path) -> str:
    return f"data/agent_traces/{project_id}/{path.name}"
