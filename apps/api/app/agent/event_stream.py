"""Event stream — append-only lifecycle log for auditing, debugging, and recovery.

All significant operations (task state changes, tool calls, agent spawns,
plan approvals) emit events to a JSONL file. The stream is:
- Append-only (never edited, only appended)
- Per-project (data/events/<project_id>.jsonl) + global (data/events/global.jsonl)
- Queryable by type, time range, or correlation ID
- Used for: debugging, crash recovery, monitoring dashboard, training data
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class EventStream:
    """Append-only JSONL event log."""

    def __init__(self, events_dir: Path | str | None = None):
        if events_dir is None:
            events_dir = Path("data/events")
        self.dir = Path(events_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, project_id: str | None) -> Path:
        if project_id:
            return self.dir / f"{project_id}.jsonl"
        return self.dir / "global.jsonl"

    def emit(
        self,
        event_type: str,
        *,
        project_id: str | None = None,
        data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Append an event. Returns the event dict."""
        event = {
            "type": event_type,
            "ts": time.time(),
            "project_id": project_id,
        }
        if correlation_id:
            event["correlation_id"] = correlation_id
        if data:
            event["data"] = data

        line = json.dumps(event, ensure_ascii=False, default=str)

        # Write to project-specific log
        if project_id:
            with open(self._file(project_id), "a", encoding="utf-8") as f:
                f.write(line + "\n")

        # Always write to global log
        with open(self._file(None), "a", encoding="utf-8") as f:
            f.write(line + "\n")

        return event

    def query(
        self,
        project_id: str | None = None,
        event_type: str | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read events from the log, newest first."""
        path = self._file(project_id)
        if not path.exists():
            return []

        events = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type and ev.get("type") != event_type:
                continue
            if since and ev.get("ts", 0) < since:
                continue
            events.append(ev)

        events.reverse()
        return events[:limit]

    def tail(self, project_id: str | None = None, n: int = 20) -> list[dict[str, Any]]:
        """Get the last N events."""
        return self.query(project_id=project_id, limit=n)


# Global singleton
event_stream = EventStream()
