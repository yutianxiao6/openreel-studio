"""Task graph behavior tests."""
from __future__ import annotations

from pathlib import Path

from app.agent.task_graph import TaskGraph
from app.agent.tool_output import build_tool_output_envelope
from app.mcp_tools import task_tools


def test_failed_task_default_block_keeps_dependents_blocked(tmp_path: Path) -> None:
    task_graph = TaskGraph(tmp_path / "tasks")
    root = task_graph.create(subject="root")
    dependent = task_graph.create(subject="downstream", blocked_by=[root.id])

    updated = task_graph.fail(root.id, error="provider error")

    assert updated is not None
    assert updated.status == "failed"
    assert updated.failure_action == "block"
    assert updated.retry_count == 0
    still_blocked = task_graph.get(dependent.id)
    assert still_blocked is not None
    assert still_blocked.status == "pending"
    assert still_blocked.blocked_by == [root.id]


def test_failed_task_with_skip_unblocks_dependents(tmp_path: Path) -> None:
    task_graph = TaskGraph(tmp_path / "tasks")
    root = task_graph.create(subject="root", failure_action="skip")
    dependent = task_graph.create(subject="downstream", blocked_by=[root.id])

    updated = task_graph.fail(root.id, error="non-fatal")

    assert updated is not None
    assert updated.status == "skipped"
    unblocked = task_graph.get(dependent.id)
    assert unblocked is not None
    assert unblocked.status == "pending"
    assert unblocked.blocked_by == []


def test_task_list_exposes_failed_task_recovery_summary(tmp_path: Path, monkeypatch) -> None:
    local_graph = TaskGraph(tmp_path / "tasks")
    monkeypatch.setattr(task_tools, "task_graph", local_graph)
    root = local_graph.create(subject="render scene", tool="node.run", project_id="project-1")
    downstream = local_graph.create(
        subject="make storyboard",
        tool="node.create",
        project_id="project-1",
        blocked_by=[root.id],
    )

    local_graph.fail(root.id, error="provider 502")

    import asyncio
    result = asyncio.run(task_tools.task_list(project_id="project-1"))

    assert result["failed"] == 1
    assert result["blocked"] == 1
    assert result["ready"] == 0
    assert result["failed_tasks"][0]["id"] == root.id
    assert result["failed_tasks"][0]["blocked_dependents"] == [downstream.id]
    assert result["suggested_next"] == "read_failed_task_and_repair_or_report"


def test_task_create_can_create_sequential_checklist_in_one_call(tmp_path: Path, monkeypatch) -> None:
    local_graph = TaskGraph(tmp_path / "tasks")
    monkeypatch.setattr(task_tools, "task_graph", local_graph)

    import asyncio
    result = asyncio.run(task_tools.task_create(
        project_id="project-1",
        mode="sequential",
        items=[
            {"subject": "写剧本", "tool": "node.create"},
            {"subject": "生成人物图", "tool": "node.create"},
            {"subject": "生成分镜图", "tool": "node.create"},
        ],
    ))

    assert result["ok"] is True
    assert result["mode"] == "sequential"
    assert result["count"] == 3
    tasks = result["tasks"]
    assert tasks[0]["blocked_by"] == []
    assert tasks[1]["blocked_by"] == [tasks[0]["id"]]
    assert tasks[2]["blocked_by"] == [tasks[1]["id"]]


def test_retry_fail_policy_retries_then_fails(tmp_path: Path) -> None:
    task_graph = TaskGraph(tmp_path / "tasks")
    root = task_graph.create(subject="retry", failure_action="retry", max_retries=1)
    dependent = task_graph.create(subject="downstream", blocked_by=[root.id])

    first = task_graph.fail(root.id, error="intermittent")
    assert first is not None
    assert first.status == "pending"
    assert first.retry_count == 1
    intermediate = task_graph.get(dependent.id)
    assert intermediate is not None
    assert intermediate.blocked_by == [root.id]

    second = task_graph.fail(root.id, error="still failing")
    assert second is not None
    assert second.status == "failed"
    final = task_graph.get(dependent.id)
    assert final is not None
    assert final.blocked_by == [root.id]


def test_task_graph_keeps_one_in_progress_per_project(tmp_path: Path) -> None:
    task_graph = TaskGraph(tmp_path / "tasks")
    first = task_graph.create(subject="script", project_id="project-1")
    second = task_graph.create(subject="storyboard", project_id="project-1")
    other_project = task_graph.create(subject="other", project_id="project-2")

    task_graph.update(first.id, status="in_progress", owner="agent")
    task_graph.update(other_project.id, status="in_progress", owner="agent")
    task_graph.update(second.id, status="in_progress", owner="agent")

    first_after = task_graph.get(first.id)
    second_after = task_graph.get(second.id)
    other_after = task_graph.get(other_project.id)

    assert first_after is not None
    assert first_after.status == "pending"
    assert first_after.owner == ""
    assert second_after is not None
    assert second_after.status == "in_progress"
    assert other_after is not None
    assert other_after.status == "in_progress"


def test_task_update_clears_stale_error_when_restarting_task(tmp_path: Path) -> None:
    task_graph = TaskGraph(tmp_path / "tasks")
    task = task_graph.create(subject="render storyboard", project_id="project-1")

    failed = task_graph.fail(task.id, error="provider 500")
    assert failed is not None
    assert failed.error == "provider 500"

    restarted = task_graph.update(task.id, status="in_progress", owner="agent")

    assert restarted is not None
    assert restarted.status == "in_progress"
    assert restarted.owner == "agent"
    assert restarted.error is None


def test_task_update_result_does_not_surface_task_error_as_tool_error(tmp_path: Path, monkeypatch) -> None:
    local_graph = TaskGraph(tmp_path / "tasks")
    monkeypatch.setattr(task_tools, "task_graph", local_graph)
    task = local_graph.create(subject="render scene", project_id="project-1")
    local_graph.fail(task.id, error="provider 500")

    import asyncio
    result = asyncio.run(task_tools.task_update(
        task_id=task.id,
        status="in_progress",
        owner="assistant",
    ))

    assert result["ok"] is True
    assert result["status"] == "in_progress"
    assert "error" not in result
    assert result["task"]["error"] is None

    envelope = build_tool_output_envelope(
        result,
        project_id="project-1",
        run_id="run",
        iteration=1,
        tool_name="task.update",
    )
    assert envelope["success"] is True
    assert envelope["outcome"] == "success"


def test_task_update_can_mark_task_failed_without_tool_failure(tmp_path: Path, monkeypatch) -> None:
    local_graph = TaskGraph(tmp_path / "tasks")
    monkeypatch.setattr(task_tools, "task_graph", local_graph)
    task = local_graph.create(subject="render scene", project_id="project-1")

    import asyncio
    result = asyncio.run(task_tools.task_update(
        task_id=task.id,
        status="failed",
        error="provider 500",
    ))

    assert result["ok"] is True
    assert result["status"] == "failed"
    assert result["task_error"] == "provider 500"
    assert "error" not in result

    envelope = build_tool_output_envelope(
        result,
        project_id="project-1",
        run_id="run",
        iteration=1,
        tool_name="task.update",
    )
    assert envelope["success"] is True
    assert envelope["outcome"] == "success"
