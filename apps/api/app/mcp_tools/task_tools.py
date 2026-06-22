"""Task graph tools — CRUD + dependency management for persistent task DAG."""
from __future__ import annotations

from typing import Any

from app.agent.task_graph import task_graph
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
from app.mcp_tools.registry import register


def _task_success_response(task, *, action: str) -> dict[str, Any]:
    payload = task.to_dict()
    result: dict[str, Any] = {
        "ok": True,
        "action": action,
        "id": task.id,
        "task_id": task.id,
        "status": task.status,
        "subject": task.subject,
        "project_id": task.project_id,
        "task": payload,
    }
    if payload.get("error"):
        result["task_error"] = payload.get("error")
    return result


@register(
    "task.create",
    description=(
        "创建轻量进度任务/checklist。复杂多步、长耗时、多节点媒体或用户要求跟踪时使用；"
        "简单问答/单节点小改可跳过。items 可一次创建多个任务，mode='sequential' 表示前后依赖。"
        "subject 写结果，description 写完成条件。"
    ),
    tags=["task", "write"],
)
async def task_create(
    subject: str = "",
    project_id: str = "",
    description: str = "",
    tool: str = "",
    blocked_by: str = "",
    failure_action: str = "block",
    max_retries: int = 0,
    items: list[dict[str, Any]] | None = None,
    mode: str = "independent",
) -> dict[str, Any]:
    if isinstance(items, list) and items:
        created = []
        previous_id = ""
        sequential = str(mode or "").strip().lower() == "sequential"
        for raw in items[:20]:
            if not isinstance(raw, dict):
                continue
            item_subject = str(raw.get("subject") or raw.get("step") or raw.get("title") or "").strip()
            if not item_subject:
                continue
            raw_blocked = raw.get("blocked_by") or []
            if isinstance(raw_blocked, str):
                item_blocked = [b.strip() for b in raw_blocked.split(",") if b.strip()]
            elif isinstance(raw_blocked, list):
                item_blocked = [str(b).strip() for b in raw_blocked if str(b).strip()]
            else:
                item_blocked = []
            if sequential and previous_id and not item_blocked:
                item_blocked = [previous_id]
            task = task_graph.create(
                subject=item_subject,
                description=str(raw.get("description") or ""),
                tool=str(raw.get("tool") or tool or ""),
                project_id=project_id,
                blocked_by=item_blocked,
                failure_action=str(raw.get("failure_action") or failure_action or "block"),
                max_retries=raw.get("max_retries") or max_retries or 0,
            )
            previous_id = task.id
            created.append(task.to_dict())
        return {
            "ok": True,
            "mode": "sequential" if sequential else "independent",
            "tasks": created,
            "count": len(created),
            "next_action": "task.update first ready task to in_progress when work starts",
        }

    if not str(subject or "").strip():
        return {
            "ok": False,
            "error": "subject or items is required",
            "hint": "简单任务可不建 task；复杂任务用 subject 创建单个任务，或用 items 一次创建 checklist。",
        }
    blocked = [b.strip() for b in blocked_by.split(",") if b.strip()] if blocked_by else []
    task = task_graph.create(
        subject=subject,
        description=description,
        tool=tool,
        project_id=project_id,
        blocked_by=blocked,
        failure_action=failure_action,
        max_retries=max_retries,
    )
    return _task_success_response(task, action="created")


@register(
    "task.list",
    description=(
        "读取当前任务图、状态统计、失败任务和可继续执行的任务。"
        "用于执行前检查是否已有同类任务，或在失败/残留任务影响判断时读取列表；支持 query/regex 找候选任务。"
    ),
    tags=["task", "read"],
)
async def task_list(
    project_id: str = "",
    status: str = "",
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    tasks = task_graph.list_all(project_id or None)
    total_unfiltered = len(tasks)
    if status:
        tasks = [t for t in tasks if t.status == status]
    if query or regex or pattern:
        filtered = []
        for task in tasks:
            match = match_text(
                search_blob(task.to_dict()),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            if match.get("matched"):
                task_dict = task.to_dict()
                task_dict["match"] = {
                    key: value
                    for key, value in match.items()
                    if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
                }
                filtered.append((task, task_dict))
        task_dict_by_id = {task.id: task_dict for task, task_dict in filtered}
        tasks = [task for task, _ in filtered]
    else:
        task_dict_by_id = {}
    try:
        parsed_limit = int(limit or 0)
    except (TypeError, ValueError):
        parsed_limit = 0
    if parsed_limit > 0:
        tasks = tasks[: min(parsed_limit, 200)]
    ready = [t for t in tasks if t.status == "pending" and not t.is_blocked]
    blocked = [t for t in tasks if t.status == "pending" and t.is_blocked]
    failed_tasks = [
        {
            "id": t.id,
            "subject": t.subject,
            "tool": t.tool,
            "error": t.error,
            "failure_action": t.failure_action,
            "retry_count": t.retry_count,
            "max_retries": t.max_retries,
            "blocked_dependents": [
                dep.id for dep in tasks
                if t.id in dep.blocked_by and dep.status == "pending"
            ][:8],
        }
        for t in tasks
        if t.status == "failed"
    ]
    return {
        "tasks": [task_dict_by_id.get(t.id) or t.to_dict() for t in tasks],
        "total": len(tasks),
        "total_unfiltered": total_unfiltered,
        "pending": sum(1 for t in tasks if t.status == "pending"),
        "ready": len(ready),
        "blocked": len(blocked),
        "in_progress": sum(1 for t in tasks if t.status == "in_progress"),
        "completed": sum(1 for t in tasks if t.status == "completed"),
        "skipped": sum(1 for t in tasks if t.status == "skipped"),
        "failed": sum(1 for t in tasks if t.status == "failed"),
        "failed_tasks": failed_tasks[:12],
        "suggested_next": (
            "read_failed_task_and_repair_or_report"
            if failed_tasks
            else ("claim_ready_task" if ready else "model_decides")
        ),
        "filters": {
            "project_id": project_id,
            "status": status,
            "query": query,
            "regex": regex,
            "pattern": pattern,
            "case_sensitive": case_sensitive,
            "limit": parsed_limit,
        },
    }


async def task_get(task_id: str) -> dict[str, Any]:
    task = task_graph.get(task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}
    return task.to_dict()


@register(
    "task.update",
    description=(
        "更新已有任务的状态、负责人、错误或依赖。开始执行时设 status='in_progress'；"
        "同项目最多保留一个 in_progress；新的任务开始时旧 in_progress 会回到 pending。"
        "失败时设 status='failed' 并写 error；依赖变化时增删 blocked_by。该工具不创建新任务。"
    ),
)
async def task_update(
    task_id: str,
    status: str = "",
    owner: str = "",
    error: str = "",
    failure_action: str = "",
    max_retries: int | None = None,
    retry_count: int | None = None,
    add_blocked_by: str = "",
    remove_blocked_by: str = "",
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if status:
        kwargs["status"] = status
    if owner:
        kwargs["owner"] = owner
    if error:
        kwargs["error"] = error
    if failure_action:
        kwargs["failure_action"] = failure_action
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    if retry_count is not None:
        kwargs["retry_count"] = retry_count
    if add_blocked_by:
        kwargs["add_blocked_by"] = [b.strip() for b in add_blocked_by.split(",") if b.strip()]
    if remove_blocked_by:
        kwargs["remove_blocked_by"] = [b.strip() for b in remove_blocked_by.split(",") if b.strip()]

    task = task_graph.update(task_id, **kwargs)
    if not task:
        return {"ok": False, "error": f"Task {task_id} not found", "error_kind": "task_not_found"}
    return _task_success_response(task, action="updated")


@register(
    "task.complete",
    description=(
        "把真实完成并已验证的任务标记为 completed。"
        "工具调用成功且产物/状态确认后使用，并用 result_summary 写短结果。"
    ),
)
async def task_complete(task_id: str, result_summary: str = "") -> dict[str, Any]:
    result = {"summary": result_summary} if result_summary else None
    task = task_graph.complete(task_id, result=result)
    if not task:
        return {"ok": False, "error": f"Task {task_id} not found", "error_kind": "task_not_found"}
    return _task_success_response(task, action="completed")


@register(
    "task.delete",
    description=(
        "删除用户明确要求清理的残留任务或已被新目标取代的任务。"
        "先用 task.list 找到 task_id；传 task_id 删除单个，task_id 留空或 '__all__' 清空当前项目任务。"
    ),
    tags=["task", "write"],
)
async def task_delete(task_id: str = "", project_id: str = "") -> dict[str, Any]:
    if task_id and task_id != "__all__":
        # Check task exists before deleting
        existing = task_graph.get(task_id)
        if not existing:
            return {
                "ok": False,
                "error": f"任务 {task_id} 不存在。先用 task.list 查看当前任务列表，找到正确的 task_id 再删。",
                "error_kind": "task_not_found",
                "hint": "调用 task.list 获取所有任务的 id 和 subject，确认要删哪个后再传正确的 task_id。",
            }
    result = task_graph.delete(task_id, project_id=project_id)
    if isinstance(result, bool):
        if not result and task_id:
            return {
                "ok": False,
                "error": f"删除任务 {task_id} 失败，任务可能已被删除。",
                "error_kind": "task_delete_failed",
            }
        return {"ok": True, "deleted": result, "task_id": task_id}
    return {"ok": True, "deleted_count": result, "task_id": "__all__"}


async def task_list_pending(project_id: str = "") -> dict[str, Any]:
    tasks = task_graph.list_pending(project_id or None)
    return {"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}
