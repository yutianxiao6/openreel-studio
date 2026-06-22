"""Agent Team — persistent teammates with protocols, autonomy, and isolation.

Combines three mechanisms from Claude Code's harness design:
- Team Protocols (s10): request-response FSM for shutdown and plan approval
- Autonomous Agents (s11): idle polling, auto-claim from task board
- Execution Isolation (s12): each task binds to a state snapshot for rollback

Teammates are persistent (survive across messages), have roles, and
communicate via the MessageBus. They auto-claim unclaimed tasks from
the TaskGraph when idle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.message_bus import message_bus
from app.agent.task_graph import task_graph
from app.agent.event_stream import event_stream

logger = logging.getLogger(__name__)

IDLE_POLL_INTERVAL = 5  # seconds
IDLE_TIMEOUT = 60  # seconds before auto-shutdown
MAX_WORK_ITERATIONS = 30


@dataclass
class Teammate:
    name: str
    role: str
    status: str = "idle"  # idle | working | shutdown
    current_task_id: str | None = None
    spawned_at: float = 0.0
    last_active: float = 0.0


# ── Team Roster ──────────────────────────────────────────────────────────

_CONFIG_DIR = Path("data/team")
_CONFIG_PATH = _CONFIG_DIR / "config.json"


def _load_roster() -> dict[str, Teammate]:
    if not _CONFIG_PATH.exists():
        return {}
    data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        m["name"]: Teammate(**m)
        for m in data.get("members", [])
    }


def _save_roster(roster: dict[str, Teammate]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "members": [
            {
                "name": t.name,
                "role": t.role,
                "status": t.status,
                "current_task_id": t.current_task_id,
                "spawned_at": t.spawned_at,
                "last_active": t.last_active,
            }
            for t in roster.values()
        ]
    }
    _CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class TeamManager:
    """Manages the team roster and protocols."""

    def __init__(self):
        self._roster: dict[str, Teammate] = _load_roster()
        self._shutdown_requests: dict[str, dict] = {}
        self._plan_requests: dict[str, dict] = {}

    def spawn(self, name: str, role: str) -> Teammate:
        teammate = Teammate(
            name=name,
            role=role,
            status="idle",
            spawned_at=time.time(),
            last_active=time.time(),
        )
        self._roster[name] = teammate
        _save_roster(self._roster)
        event_stream.emit("agent.spawned", data={"name": name, "role": role})
        return teammate

    def get(self, name: str) -> Teammate | None:
        return self._roster.get(name)

    def list_teammates(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "role": t.role,
                "status": t.status,
                "current_task_id": t.current_task_id,
                "last_active": t.last_active,
            }
            for t in self._roster.values()
        ]

    def set_status(self, name: str, status: str) -> None:
        if name in self._roster:
            self._roster[name].status = status
            self._roster[name].last_active = time.time()
            _save_roster(self._roster)

    def remove(self, name: str) -> bool:
        if name in self._roster:
            del self._roster[name]
            _save_roster(self._roster)
            event_stream.emit("agent.shutdown", data={"name": name})
            return True
        return False

    # ── Shutdown Protocol (s10) ──────────────────────────────────────

    def request_shutdown(self, target: str) -> dict[str, Any]:
        """Lead requests a teammate to shut down gracefully."""
        req_id = uuid.uuid4().hex[:8]
        self._shutdown_requests[req_id] = {
            "target": target,
            "status": "pending",
            "requested_at": time.time(),
        }
        message_bus.send(
            "lead", target,
            "Please shut down gracefully.",
            msg_type="shutdown_request",
            extra={"request_id": req_id},
        )
        return {"request_id": req_id, "status": "pending"}

    def respond_shutdown(self, req_id: str, approve: bool, reason: str = "") -> dict[str, Any]:
        """Teammate responds to shutdown request."""
        req = self._shutdown_requests.get(req_id)
        if not req:
            return {"error": f"Unknown request: {req_id}"}
        req["status"] = "approved" if approve else "rejected"
        if approve:
            self.set_status(req["target"], "shutdown")
        return {"request_id": req_id, "status": req["status"]}

    # ── Plan Approval Protocol (s10) ─────────────────────────────────

    def submit_plan(self, from_agent: str, plan_summary: str) -> dict[str, Any]:
        """Teammate submits a plan for lead approval."""
        req_id = uuid.uuid4().hex[:8]
        self._plan_requests[req_id] = {
            "from": from_agent,
            "plan": plan_summary,
            "status": "pending",
            "submitted_at": time.time(),
        }
        message_bus.send(
            from_agent, "lead",
            f"Plan for review: {plan_summary}",
            msg_type="plan_review_request",
            extra={"request_id": req_id},
        )
        return {"request_id": req_id, "status": "pending"}

    def review_plan(self, req_id: str, approve: bool, feedback: str = "") -> dict[str, Any]:
        """Lead approves or rejects a teammate's plan."""
        req = self._plan_requests.get(req_id)
        if not req:
            return {"error": f"Unknown plan request: {req_id}"}
        req["status"] = "approved" if approve else "rejected"
        message_bus.send(
            "lead", req["from"],
            feedback or ("Approved" if approve else "Rejected"),
            msg_type="plan_review_response",
            extra={"request_id": req_id, "approve": approve},
        )
        return {"request_id": req_id, "status": req["status"]}

    # ── Autonomous Task Claiming (s11) ───────────────────────────────

    def auto_claim_task(self, agent_name: str, project_id: str = "") -> dict[str, Any] | None:
        """Agent scans the task board and claims the first unclaimed task."""
        unclaimed = task_graph.list_unclaimed(project_id or None)
        if not unclaimed:
            return None

        task = unclaimed[0]
        task_graph.claim(task.id, agent_name)
        self.set_status(agent_name, "working")
        self._roster[agent_name].current_task_id = task.id
        _save_roster(self._roster)

        event_stream.emit(
            "task.claimed",
            project_id=task.project_id,
            data={"task_id": task.id, "agent": agent_name},
        )
        return task.to_dict()

    # ── Execution Isolation (s12) ────────────────────────────────────

    def create_snapshot(self, task_id: str, state: dict) -> dict[str, Any]:
        """Save a state snapshot before task execution for rollback."""
        snapshots_dir = _CONFIG_DIR / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "task_id": task_id,
            "state": state,
            "created_at": time.time(),
        }
        path = snapshots_dir / f"{task_id}.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"task_id": task_id, "snapshot_path": str(path)}

    def restore_snapshot(self, task_id: str) -> dict[str, Any] | None:
        """Restore state from a pre-execution snapshot (rollback on failure)."""
        path = _CONFIG_DIR / "snapshots" / f"{task_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("state")

    def delete_snapshot(self, task_id: str) -> bool:
        """Clean up snapshot after successful execution."""
        path = _CONFIG_DIR / "snapshots" / f"{task_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False


# Global singleton
team_manager = TeamManager()
