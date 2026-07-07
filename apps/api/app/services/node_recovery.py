"""Recovery helpers for node states that cannot survive an API restart."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import select

from app.db.models import WorkflowNode
from app.db.session import session_scope
from app.services import media_history

logger = logging.getLogger(__name__)

MEDIA_NODE_TYPES = {"image", "video", "audio"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


STALE_RUNNING_MEDIA_SECONDS = _env_int("DRAMA_STALE_RUNNING_MEDIA_SECONDS", 660, minimum=60)


def _parse_json_value(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failed_media_output(node: WorkflowNode, output: Any, *, message: str, error_kind: str) -> dict[str, Any]:
    if isinstance(output, dict):
        failed = dict(output)
    else:
        failed = {}
    failed.setdefault("type", node.type)
    failed["ok"] = False
    failed["status"] = "failed"
    failed["error"] = message
    failed["error_message"] = message
    failed["error_kind"] = error_kind
    if isinstance(failed.get("stages"), list):
        next_stages: list[Any] = []
        for stage in failed["stages"]:
            if not isinstance(stage, dict):
                next_stages.append(stage)
                continue
            stage_status = str(stage.get("status") or "").strip().lower()
            if stage_status in {"queued", "running"}:
                next_stage = dict(stage)
                next_stage["status"] = "failed"
                next_stage["error"] = message
                next_stage["error_message"] = message
                next_stages.append(next_stage)
            else:
                next_stages.append(stage)
        failed["stages"] = next_stages
    return failed


async def cleanup_interrupted_media_nodes(
    *,
    project_id: str | None = None,
    stale_after_seconds: int | None = STALE_RUNNING_MEDIA_SECONDS,
    reason: str = "stale_running_media",
) -> dict[str, Any]:
    """Settle running media nodes whose background worker can no longer update them."""

    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=stale_after_seconds) if stale_after_seconds is not None else None
    failed_message = "媒体生成任务已中断，后端已无法继续接收该任务结果。请在原节点重新运行。"
    completed = 0
    failed = 0
    checked = 0
    changed_ids: list[str] = []

    async with session_scope() as session:
        stmt = select(WorkflowNode).where(
            WorkflowNode.status == "running",
            WorkflowNode.type.in_(MEDIA_NODE_TYPES),
        )
        if project_id:
            stmt = stmt.where(WorkflowNode.project_id == project_id)
        result = await session.exec(stmt)
        nodes = list(result.all())
        for node in nodes:
            checked += 1
            if cutoff is not None and node.updated_at and node.updated_at > cutoff:
                continue

            output = _parse_json_value(node.output_json)
            if media_history.is_successful_media_output(output):
                node.status = "completed"
                node.error_message = None
                completed += 1
            else:
                node.status = "failed"
                node.error_message = failed_message
                node.output_json = _json_dumps(
                    _failed_media_output(
                        node,
                        output,
                        message=failed_message,
                        error_kind=reason,
                    ),
                )
                failed += 1
            node.updated_at = now
            session.add(node)
            changed_ids.append(node.id)
        if changed_ids:
            await session.commit()

    if changed_ids:
        logger.info(
            "cleaned interrupted media nodes project_id=%s checked=%s completed=%s failed=%s reason=%s",
            project_id or "*",
            checked,
            completed,
            failed,
            reason,
        )
    return {
        "ok": True,
        "checked": checked,
        "changed": len(changed_ids),
        "completed": completed,
        "failed": failed,
        "node_ids": changed_ids,
        "reason": reason,
    }
