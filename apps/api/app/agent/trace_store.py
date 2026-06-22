"""SQLite-backed query store for compact agent trace events.

JSONL files remain the durable human-readable artifact. This store mirrors the
same sanitized records into a queryable table for debug APIs.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.token_usage import (
    accumulate_usage,
    build_usage_monitor_payload,
    empty_usage_totals,
    latest_call_context_from_usage,
    latest_call_tokens_from_usage,
    normalize_usage_snapshot,
    reset_context_peak_usage,
)
from app.config import settings

logger = logging.getLogger(__name__)

TABLE_NAME = "agent_trace_events"
_ENSURED_PATHS: set[str] = set()


def sqlite_path_from_url(database_url: str | None = None) -> Path | None:
    url = database_url or settings.DATABASE_URL
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            if rest == ":memory:":
                return None
            return Path(rest)
    return None


def ensure_trace_table(database_url: str | None = None) -> Path | None:
    db_path = sqlite_path_from_url(database_url)
    if db_path is None:
        return None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(db_path)
    if key in _ENSURED_PATHS:
        return db_path

    with sqlite3.connect(db_path, timeout=1.0) as conn:
        conn.execute(_table_ddl())
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_project_run_ts "
            f"ON {TABLE_NAME} (project_id, run_id, ts)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_project_ts "
            f"ON {TABLE_NAME} (project_id, ts)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_event "
            f"ON {TABLE_NAME} (event)"
        )
    _ENSURED_PATHS.add(key)
    return db_path


def append_trace_event(record: dict[str, Any], database_url: str | None = None) -> None:
    if os.getenv("DRAMA_AGENT_TRACE_DB_ENABLED", "1").lower() in {"0", "false", "no"}:
        return
    db_path = ensure_trace_table(database_url)
    if db_path is None:
        return
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_NAME}
                    (project_id, run_id, ts, event, iteration, tool_name,
                     transition_reason, duration_ms, error_kind, payload_json,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.get("project_id") or ""),
                    str(record.get("run_id") or ""),
                    str(record.get("ts") or ""),
                    str(record.get("event") or ""),
                    record.get("iteration"),
                    record.get("tool_name"),
                    record.get("transition_reason"),
                    record.get("duration_ms"),
                    record.get("error_kind"),
                    json.dumps(record, ensure_ascii=False, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:
        logger.exception("agent trace DB mirror write failed")


def list_trace_runs(
    project_id: str,
    *,
    limit: int = 20,
    database_url: str | None = None,
) -> dict[str, Any] | None:
    db_path = ensure_trace_table(database_url)
    if db_path is None:
        return None
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {TABLE_NAME} WHERE project_id = ? GROUP BY run_id)",
                (project_id,),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT
                    run_id,
                    COUNT(*) AS event_count,
                    MIN(ts) AS started_at,
                    MAX(ts) AS last_event_at,
                    SUM(CASE WHEN error_kind IS NOT NULL OR event IN ('llm_error', 'tool_error') THEN 1 ELSE 0 END)
                        AS error_count
                FROM {TABLE_NAME}
                WHERE project_id = ?
                GROUP BY run_id
                ORDER BY MAX(ts) DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            traces = []
            for row in rows:
                last = conn.execute(
                    f"""
                    SELECT event, tool_name, error_kind
                    FROM {TABLE_NAME}
                    WHERE project_id = ? AND run_id = ?
                    ORDER BY ts DESC, id DESC
                    LIMIT 1
                    """,
                    (project_id, row["run_id"]),
                ).fetchone()
                traces.append({
                    "project_id": project_id,
                    "run_id": row["run_id"],
                    "path": f"db:{TABLE_NAME}/{project_id}/{row['run_id']}",
                    "source": "db",
                    "size_bytes": None,
                    "mtime": row["last_event_at"],
                    "event_count": row["event_count"],
                    "started_at": row["started_at"],
                    "last_event_at": row["last_event_at"],
                    "last_event": last["event"] if last else None,
                    "last_tool_name": last["tool_name"] if last else None,
                    "last_error_kind": last["error_kind"] if last else None,
                    "error_count": row["error_count"] or 0,
                })
            return {
                "project_id": project_id,
                "traces": traces,
                "total": total,
                "limit": limit,
                "source": "db",
            }
    except Exception:
        logger.exception("agent trace DB list failed")
        return None


def read_trace_events(
    project_id: str,
    run_id: str,
    *,
    limit: int = 200,
    database_url: str | None = None,
) -> dict[str, Any] | None:
    db_path = ensure_trace_table(database_url)
    if db_path is None:
        return None
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE project_id = ? AND run_id = ?",
                (project_id, run_id),
            ).fetchone()[0]
            if not total:
                return None
            rows = conn.execute(
                f"""
                SELECT payload_json
                FROM {TABLE_NAME}
                WHERE project_id = ? AND run_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (project_id, run_id, limit),
            ).fetchall()
            events = [_parse_payload(row["payload_json"]) for row in reversed(rows)]
            return {
                "project_id": project_id,
                "run_id": run_id,
                "path": f"db:{TABLE_NAME}/{project_id}/{run_id}",
                "source": "db",
                "events": events,
                "event_count": total,
                "returned": len(events),
                "truncated": total > len(events),
                "limit": limit,
            }
    except Exception:
        logger.exception("agent trace DB read failed")
        return None


def summarize_token_usage(
    project_id: str,
    *,
    run_id: str | None = None,
    since_ts: str | None = None,
    limit: int = 1000,
    database_url: str | None = None,
) -> dict[str, Any] | None:
    db_path = ensure_trace_table(database_url)
    if db_path is None:
        return None
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            where = "project_id = ? AND event IN ('llm_usage', 'compact_boundary')"
            params: list[Any] = [project_id]
            if run_id:
                where += " AND run_id = ?"
                params.append(run_id)
            if since_ts:
                where += " AND ts > ?"
                params.append(since_ts)
            rows = conn.execute(
                f"""
                SELECT run_id, ts, event, payload_json
                FROM {TABLE_NAME}
                WHERE {where}
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
    except Exception:
        logger.exception("agent token usage summary failed")
        return None

    rows = list(reversed(rows))
    totals = _empty_token_totals()
    by_run: dict[str, dict[str, Any]] = {}
    last_usage: dict[str, Any] | None = None
    last_event_at: str | None = None
    usage_event_count = 0
    for row in rows:
        payload = _parse_payload(row["payload_json"])
        event_name = str(row["event"] or "")
        row_run_id = str(row["run_id"] or "")
        if event_name == "compact_boundary":
            totals = reset_context_peak_usage(totals)
            if row_run_id:
                by_run[row_run_id] = reset_context_peak_usage(
                    by_run.get(row_run_id) or _empty_token_totals()
                )
            continue
        usage = normalize_usage_snapshot(payload.get("usage"))
        totals = _add_token_usage(totals, usage)
        run_total = by_run.get(row_run_id) or _empty_token_totals()
        by_run[row_run_id] = _add_token_usage(run_total, usage)
        last_usage = usage
        last_event_at = str(row["ts"] or "") or last_event_at
        usage_event_count += 1

    usage_payload = build_usage_monitor_payload(
        last_usage or {},
        by_run.get(str(run_id or "")) or _empty_token_totals(),
        totals,
    )
    latest_call_tokens = latest_call_tokens_from_usage(last_usage or {}) or None
    latest_call_context = latest_call_context_from_usage(last_usage or {}) or None
    return {
        "project_id": project_id,
        "run_id": run_id,
        "source": "db",
        "event_count": usage_event_count,
        "limit": limit,
        "since_ts": since_ts,
        "totals": totals,
        "by_run": [
            {"run_id": key, "totals": value}
            for key, value in sorted(by_run.items())
        ],
        "last_usage": last_usage,
        "latest_call_tokens": latest_call_tokens,
        "latest_call_context": latest_call_context,
        "session_cumulative_tokens": usage_payload.get("session_cumulative_tokens"),
        "session_context_peak": usage_payload.get("session_context_peak"),
        "last_event_at": last_event_at,
    }


def _empty_token_totals() -> dict[str, Any]:
    return empty_usage_totals()


def _add_token_usage(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    return accumulate_usage(total, usage)


def _parse_payload(payload_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        return {"event": "trace_parse_error", "error": str(exc)}
    return payload if isinstance(payload, dict) else {"event": "trace_parse_error"}


def _table_ddl() -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id VARCHAR NOT NULL,
        run_id VARCHAR NOT NULL,
        ts VARCHAR NOT NULL,
        event VARCHAR NOT NULL,
        iteration INTEGER,
        tool_name VARCHAR,
        transition_reason VARCHAR,
        duration_ms INTEGER,
        error_kind VARCHAR,
        payload_json TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """
