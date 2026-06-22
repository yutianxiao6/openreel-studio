"""Minimal agent run trace.

The prompt dump stores large model inputs for debugging. This trace is a small
append-only JSONL audit trail for loop transitions, permissions, and tools.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.agent.trace_store import append_trace_event

logger = logging.getLogger(__name__)

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|secret|password|bearer|(^|[_-])(access|refresh|id|auth|session|api)?[_-]?token($|[_-]))"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)([\"'\s:=]+)([^\"'\s,}]+)"
)
_MAX_STRING = 300
_MAX_LIST = 80
_MAX_DICT_KEYS = 80


def traces_root() -> Path:
    path = Path(settings.PROJECT_ROOT) / "data" / "agent_traces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_DICT_KEYS]
        safe: dict[str, Any] = {}
        for key, item in items:
            key_str = str(key)
            if _SECRET_KEY_RE.search(key_str):
                safe[key_str] = "<redacted>"
            else:
                safe[key_str] = _safe_value(item)
        if len(value) > _MAX_DICT_KEYS:
            safe["_truncated_keys"] = len(value) - _MAX_DICT_KEYS
        return safe
    if isinstance(value, list):
        safe_list = [_safe_value(item) for item in value[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            safe_list.append({"_truncated_items": len(value) - _MAX_LIST})
        return safe_list
    if isinstance(value, str):
        text = _SECRET_VALUE_RE.sub(r"\1\2<redacted>", value)
        if len(text) > _MAX_STRING:
            return text[:_MAX_STRING] + "...<truncated>"
        return text
    return value


def visible_tool_names(tools: list[dict]) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        name = (tool.get("function") or {}).get("name")
        if isinstance(name, str) and name:
            names.append(name.replace("__", "."))
    return names


def result_error_kind(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("requires_user_confirm") and not result.get("error"):
        return None
    error_kind = result.get("error_kind")
    if error_kind:
        return str(error_kind)
    if result.get("error"):
        return "tool_error"
    if result.get("ok") is False:
        return "tool_failed"
    return None


class AgentTrace:
    def __init__(self, project_id: str, run_id: str):
        self.project_id = project_id
        self.run_id = run_id
        self.dir = traces_root() / project_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.jsonl"
        self.enabled = os.getenv("DRAMA_AGENT_TRACE_ENABLED", "1").lower() not in {
            "0",
            "false",
            "no",
        }

    def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        tool_name: str | None = None,
        transition_reason: str | None = None,
        duration_ms: int | None = None,
        error_kind: str | None = None,
        **fields: Any,
    ) -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "project_id": self.project_id,
            "run_id": self.run_id,
        }
        if iteration is not None:
            record["iteration"] = iteration
        if tool_name:
            record["tool_name"] = tool_name
        if transition_reason:
            record["transition_reason"] = transition_reason
        if duration_ms is not None:
            record["duration_ms"] = duration_ms
        if error_kind:
            record["error_kind"] = error_kind
        record.update({key: _safe_value(value) for key, value in fields.items()})

        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception("agent trace write failed")

        try:
            append_trace_event(record)
        except Exception:
            logger.exception("agent trace DB mirror failed")


def elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))
