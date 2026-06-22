"""Internal team helpers.

These functions are not registered as Agent tools. High-level collaboration is
exposed through agent.map_reduce, agent.pipeline, and agent.hierarchical.
"""
from __future__ import annotations

from typing import Any

from app.agent.team import team_manager


async def team_spawn(name: str, role: str) -> dict[str, Any]:
    teammate = team_manager.spawn(name, role)
    return {"name": teammate.name, "role": teammate.role, "status": teammate.status}


async def team_list() -> dict[str, Any]:
    teammates = team_manager.list_teammates()
    return {"teammates": teammates, "count": len(teammates)}


async def team_remove(name: str) -> dict[str, Any]:
    ok = team_manager.remove(name)
    return {"removed": ok, "name": name}


async def team_request_shutdown(target: str) -> dict[str, Any]:
    return team_manager.request_shutdown(target)


async def team_respond_shutdown(request_id: str, approve: bool = True, reason: str = "") -> dict[str, Any]:
    return team_manager.respond_shutdown(request_id, approve, reason)


async def team_submit_plan(from_agent: str, plan_summary: str) -> dict[str, Any]:
    return team_manager.submit_plan(from_agent, plan_summary)


async def team_review_plan(request_id: str, approve: bool = True, feedback: str = "") -> dict[str, Any]:
    return team_manager.review_plan(request_id, approve, feedback)


async def team_auto_claim(agent_name: str, project_id: str = "") -> dict[str, Any]:
    task = team_manager.auto_claim_task(agent_name, project_id)
    if not task:
        return {"claimed": False, "message": "No unclaimed tasks available"}
    return {"claimed": True, "task": task}


async def team_snapshot(task_id: str, state_json: str = "{}") -> dict[str, Any]:
    import json
    state = json.loads(state_json) if state_json else {}
    return team_manager.create_snapshot(task_id, state)


async def team_restore(task_id: str) -> dict[str, Any]:
    state = team_manager.restore_snapshot(task_id)
    if state is None:
        return {"error": f"No snapshot for task {task_id}"}
    return {"restored": True, "state": state}
