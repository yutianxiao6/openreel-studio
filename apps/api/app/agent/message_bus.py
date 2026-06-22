"""JSONL Message Bus — async communication between agents via append-only inboxes.

Each agent has a named inbox file (.team/inbox/<name>.jsonl). Messages are
appended as single JSON lines. Reading drains the inbox (read + truncate).

This decouples agents from each other: the sender doesn't need to know if
the receiver is running. Messages accumulate until drained.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class MessageBus:
    """Append-only JSONL inbox system for inter-agent communication."""

    def __init__(self, bus_dir: Path | str | None = None):
        if bus_dir is None:
            bus_dir = Path("data/team/inbox")
        self.dir = Path(bus_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _inbox_path(self, name: str) -> Path:
        return self.dir / f"{name}.jsonl"

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a message to the recipient's inbox."""
        msg = {
            "type": msg_type,
            "from": sender,
            "to": to,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)

        path = self._inbox_path(to)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        return msg

    def broadcast(
        self,
        sender: str,
        content: str,
        msg_type: str = "broadcast",
        exclude: list[str] | None = None,
    ) -> int:
        """Send to all known inboxes (excluding sender and exclude list)."""
        exclude_set = set(exclude or [])
        exclude_set.add(sender)
        count = 0
        for path in self.dir.glob("*.jsonl"):
            name = path.stem
            if name in exclude_set:
                continue
            self.send(sender, name, content, msg_type)
            count += 1
        return count

    def read_inbox(self, name: str, drain: bool = True) -> list[dict[str, Any]]:
        """Read all messages from an inbox. If drain=True, clears the inbox after reading."""
        path = self._inbox_path(name)
        if not path.exists():
            return []

        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []

        messages = []
        for line in text.splitlines():
            if line.strip():
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if drain:
            path.write_text("", encoding="utf-8")

        return messages

    def peek_inbox(self, name: str) -> list[dict[str, Any]]:
        """Read without draining."""
        return self.read_inbox(name, drain=False)

    def inbox_count(self, name: str) -> int:
        """Count messages without reading them."""
        path = self._inbox_path(name)
        if not path.exists():
            return 0
        text = path.read_text(encoding="utf-8").strip()
        return len(text.splitlines()) if text else 0

    def list_inboxes(self) -> list[dict[str, Any]]:
        """List all inboxes with message counts."""
        result = []
        for path in sorted(self.dir.glob("*.jsonl")):
            name = path.stem
            text = path.read_text(encoding="utf-8").strip()
            count = len(text.splitlines()) if text else 0
            result.append({"name": name, "pending_messages": count})
        return result

    def clear_inbox(self, name: str) -> bool:
        """Clear an inbox without reading."""
        path = self._inbox_path(name)
        if path.exists():
            path.write_text("", encoding="utf-8")
            return True
        return False


# Global singleton
message_bus = MessageBus()
