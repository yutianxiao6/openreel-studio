"""Recover workflow run-all state left behind by an interrupted API process."""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from app.db.models import Project
from app.db.session import session_scope


logger = logging.getLogger(__name__)


def recover_interrupted_workflow_runtime_state(
    state: dict[str, Any],
    *,
    now: str,
) -> tuple[dict[str, Any], int]:
    """Convert persisted active runs into resumable paused runs.

    A fresh API process has no task capable of owning a persisted
    ``run_all_active`` flag. Such a flag is therefore always stale at startup.
    Completed steps are preserved; only in-flight steps are returned to idle.
    """
    runtime = state.get("workflow_runtime") if isinstance(state, dict) else None
    instances = runtime.get("instances") if isinstance(runtime, dict) else None
    if not isinstance(instances, dict):
        return state, 0

    next_state = deepcopy(state)
    next_runtime = next_state["workflow_runtime"]
    next_instances = next_runtime["instances"]
    recovered = 0

    for instance in next_instances.values():
        if not isinstance(instance, dict) or not instance.get("run_all_active"):
            continue
        instance.pop("run_all_active", None)
        instance["pause_requested"] = False
        instance["status"] = "paused"
        instance["paused_at"] = now
        instance["interrupted_at"] = now
        instance["pause_reason"] = "api_process_interrupted"
        instance["updated_at"] = now
        steps = instance.get("steps") if isinstance(instance.get("steps"), dict) else {}
        for record in steps.values():
            if not isinstance(record, dict) or str(record.get("status") or "").strip() != "running":
                continue
            record["status"] = "idle"
            record["interrupted_at"] = now
            record["updated_at"] = now
            record.pop("last_started_at", None)
        recovered += 1

    if recovered:
        next_runtime["updated_at"] = now
        return next_state, recovered
    return state, 0


async def recover_interrupted_workflow_runtimes() -> int:
    now = datetime.now(timezone.utc).isoformat()
    recovered_total = 0
    async with session_scope() as session:
        projects = list((await session.exec(select(Project))).all())
        for project in projects:
            try:
                state = json.loads(project.state_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            next_state, recovered = recover_interrupted_workflow_runtime_state(state, now=now)
            if not recovered:
                continue
            project.state_json = json.dumps(next_state, ensure_ascii=False)
            session.add(project)
            recovered_total += recovered
        if recovered_total:
            await session.commit()
    if recovered_total:
        logger.warning("Recovered %s interrupted workflow run-all instance(s)", recovered_total)
    return recovered_total
