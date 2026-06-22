"""Persistent Task Graph — file-backed DAG for multi-step goals.

Each task is a JSON file in data/tasks/. Tasks have:
- status: pending → in_progress → completed | failed | skipped
- blocked_by: list of task IDs that must complete first
- owner: which agent/worker claimed it

Completing a task auto-unblocks dependents. The graph survives restarts
and context compression — it's the coordination backbone for background
tasks, multi-agent, and autonomous execution.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    tool: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    project_id: str = ""
    status: str = "pending"  # pending | in_progress | completed | failed | skipped
    failure_action: str = "block"  # block | skip | retry
    max_retries: int = 0
    retry_count: int = 0
    blocked_by: list[str] = field(default_factory=list)
    owner: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "tool": self.tool,
            "input": self.input,
            "project_id": self.project_id,
            "status": self.status,
            "failure_action": self.failure_action,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "blocked_by": self.blocked_by,
            "owner": self.owner,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=data["id"],
            subject=data.get("subject", ""),
            description=data.get("description", ""),
            tool=data.get("tool", ""),
            input=data.get("input", {}),
            project_id=data.get("project_id", ""),
            status=data.get("status", "pending"),
            failure_action=data.get("failure_action", "block"),
            max_retries=int(data.get("max_retries", 0) or 0),
            retry_count=int(data.get("retry_count", 0) or 0),
            blocked_by=data.get("blocked_by", []),
            owner=data.get("owner", ""),
            result=data.get("result"),
            error=data.get("error"),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )

    @property
    def is_blocked(self) -> bool:
        return len(self.blocked_by) > 0


class TaskGraph:
    """File-persisted task DAG with dependency resolution."""

    def __init__(self, tasks_dir: Path | str | None = None):
        if tasks_dir is None:
            from app.config import settings
            tasks_dir = Path(settings.PROJECT_ROOT) / "data" / "tasks"
        self.dir = Path(tasks_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = []
        for f in self.dir.glob("task_*.json"):
            try:
                ids.append(int(f.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                pass
        return max(ids) if ids else 0

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _save(self, task: Task) -> None:
        task.updated_at = time.time()
        self._path(task.id).write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self, task_id: str) -> Task | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Task.from_dict(data)

    # ── CRUD ─────────────────────────────────────────────────────────

    def create(
        self,
        subject: str,
        *,
        description: str = "",
        tool: str = "",
        input: dict[str, Any] | None = None,
        project_id: str = "",
        blocked_by: list[str] | None = None,
        failure_action: str = "block",
        max_retries: int = 0,
    ) -> Task:
        task_id = f"task_{self._next_id}"
        self._next_id += 1
        if failure_action not in {"block", "skip", "retry"}:
            failure_action = "block"
        try:
            max_retries_int = int(max_retries)
        except (TypeError, ValueError):
            max_retries_int = 0
        if max_retries_int < 0:
            max_retries_int = 0
        task = Task(
            id=task_id,
            subject=subject,
            description=description,
            tool=tool,
            input=input or {},
            project_id=project_id,
            status="pending",
            failure_action=failure_action,
            max_retries=max_retries_int,
            blocked_by=blocked_by or [],
            created_at=time.time(),
        )
        self._save(task)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._load(task_id)

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        add_blocked_by: list[str] | None = None,
        remove_blocked_by: list[str] | None = None,
        failure_action: str | None = None,
        max_retries: int | None = None,
        retry_count: int | None = None,
    ) -> Task | None:
        task = self._load(task_id)
        if not task:
            return None

        if failure_action:
            task.failure_action = (
                failure_action if failure_action in {"block", "skip", "retry"} else task.failure_action
            )
        if max_retries is not None:
            try:
                task.max_retries = max(0, int(max_retries))
            except (TypeError, ValueError):
                pass
        if retry_count is not None:
            try:
                task.retry_count = max(0, int(retry_count))
            except (TypeError, ValueError):
                pass

        if status:
            status_value = status.strip().lower()
            if status_value == "failed" and task.failure_action == "skip":
                task.status = "skipped"
                task.error = None
                self._clear_dependency(task_id)
            elif status_value == "failed" and task.failure_action == "retry":
                if task.retry_count < task.max_retries:
                    task.status = "pending"
                    task.retry_count = task.retry_count + 1
                    task.owner = ""
                    task.error = None
                else:
                    task.status = "failed"
            else:
                if status_value == "in_progress":
                    self._clear_other_in_progress(task)
                task.status = status_value
                if status_value in {"completed", "skipped"}:
                    self._clear_dependency(task_id)
                if status_value in {"pending", "in_progress", "completed", "skipped"}:
                    task.error = None
        if owner is not None:
            task.owner = owner
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if add_blocked_by:
            task.blocked_by = list(set(task.blocked_by + add_blocked_by))
        if remove_blocked_by:
            task.blocked_by = [x for x in task.blocked_by if x not in remove_blocked_by]

        self._save(task)
        return task

    def delete(self, task_id: str, *, project_id: str = "") -> bool | int:
        """Delete a task by ID. If task_id is empty, delete ALL tasks for project_id."""
        if not task_id and project_id:
            tasks = self.list_all(project_id)
            count = 0
            for task in tasks:
                path = self._path(task.id)
                if path.exists():
                    path.unlink()
                    count += 1
            return count
        path = self._path(task_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def archive_completed(self, project_id: str) -> int:
        """Move completed tasks to history file, then delete them. Returns count."""
        tasks = self.list_all(project_id)
        completed = [t for t in tasks if t.status == "completed"]
        if not completed:
            return 0
        # Persist to history
        history_dir = self.dir / "history"
        history_dir.mkdir(exist_ok=True)
        history_file = history_dir / f"{project_id}.jsonl"
        with history_file.open("a", encoding="utf-8") as f:
            for t in completed:
                f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
                self._path(t.id).unlink()
        return len(completed)

    # ── Queries ──────────────────────────────────────────────────────

    def list_all(self, project_id: str | None = None) -> list[Task]:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if project_id and data.get("project_id") != project_id:
                continue
            tasks.append(Task.from_dict(data))
        return tasks

    def list_pending(self, project_id: str | None = None) -> list[Task]:
        return [
            t for t in self.list_all(project_id)
            if t.status == "pending" and not t.is_blocked
        ]

    def list_in_progress(self, project_id: str | None = None) -> list[Task]:
        return [t for t in self.list_all(project_id) if t.status == "in_progress"]

    def list_unclaimed(self, project_id: str | None = None) -> list[Task]:
        return [
            t for t in self.list_all(project_id)
            if t.status == "pending" and not t.is_blocked and not t.owner
        ]

    # ── Dependency resolution ────────────────────────────────────────

    def _clear_dependency(self, completed_id: str) -> None:
        for f in self.dir.glob("task_*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            blocked = data.get("blocked_by", [])
            if completed_id in blocked:
                blocked.remove(completed_id)
                data["blocked_by"] = blocked
                data["updated_at"] = time.time()
                f.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    def _clear_other_in_progress(self, active_task: Task) -> None:
        """Keep the visible checklist Codex-like: one active step per project."""
        for f in self.dir.glob("task_*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("id") == active_task.id:
                continue
            if data.get("status") != "in_progress":
                continue
            if active_task.project_id and data.get("project_id") != active_task.project_id:
                continue
            data["status"] = "pending"
            data["owner"] = ""
            data["updated_at"] = time.time()
            f.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def complete(self, task_id: str, result: dict[str, Any] | None = None) -> Task | None:
        return self.update(task_id, status="completed", result=result)

    def fail(self, task_id: str, error: str) -> Task | None:
        return self.update(task_id, status="failed", error=error)

    def claim(self, task_id: str, owner: str) -> Task | None:
        return self.update(task_id, status="in_progress", owner=owner)

    # ── Bulk operations ──────────────────────────────────────────────

    def create_from_plan(
        self,
        steps: list[dict[str, Any]],
        project_id: str,
    ) -> list[Task]:
        """Create tasks from a planner output (list of step dicts).
        Sequential steps get blocked_by the previous one."""
        tasks: list[Task] = []
        prev_id: str | None = None

        for step in steps:
            blocked = []
            if prev_id:
                blocked.append(prev_id)

            task = self.create(
                subject=step.get("title", step.get("tool", "untitled")),
                tool=step.get("tool", ""),
                input=step.get("input", {}),
                project_id=project_id,
                blocked_by=blocked,
            )
            tasks.append(task)
            prev_id = task.id

        return tasks

    def clear_project(self, project_id: str) -> int:
        """Remove all tasks for a project. Returns count deleted."""
        count = 0
        for f in list(self.dir.glob("task_*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("project_id") == project_id:
                f.unlink()
                count += 1
        return count


# Global singleton
task_graph = TaskGraph()
