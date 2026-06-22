"""Blueprint tree confirmation state helpers.

This replaces the retired executable pending-plan path for blueprint review.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.agent.blueprint_tree import read_blueprint
from app.agent.blueprint_confirmation_state import (
    BLUEPRINT_CONFIRMATION_ROLE,
    PENDING_BLUEPRINT_CONFIRMATION_KEY,
    mark_blueprint_confirmation_plan,
    pending_blueprint_plan,
    set_pending_blueprint_confirmation,
)
from app.agent.project_state_io import read_project_state, write_project_state


def _coerce_doc(value: dict[str, Any] | str | None) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


def _attach_tree_preview(project_id: str, plan_doc: dict[str, Any]) -> None:
    try:
        bp_doc = read_blueprint(project_id)
    except Exception:
        return
    replacement_draft = bp_doc.get("replacement_draft") if isinstance(bp_doc.get("replacement_draft"), dict) else None
    if plan_doc.get("replacement") and isinstance(replacement_draft, dict) and isinstance(replacement_draft.get("root"), dict):
        root = replacement_draft.get("root", {})
        source_doc = replacement_draft
    else:
        root = bp_doc.get("root", {})
        source_doc = bp_doc
    summary_nodes = [
        {"id": c.get("id"), "type": c.get("type"), "title": str(c.get("title") or "")[:80]}
        for c in (root.get("children") or [])
        if isinstance(c, dict)
    ]
    plan_doc["tree_summary"] = plan_doc.get("tree_summary") or {
        "node_count": 0,
        "materialized_count": 0,
        "by_type": {},
        "top_level": summary_nodes,
    }
    plan_doc["tree_nodes"] = plan_doc.get("tree_nodes") or summary_nodes
    plan_doc["title"] = plan_doc.get("title") or source_doc.get("title") or bp_doc.get("title") or "视频蓝图"
    plan_doc["summary"] = plan_doc.get("summary") or source_doc.get("summary") or bp_doc.get("title") or "树形蓝图已就绪"
    if not plan_doc.get("sections"):
        plan_doc["sections"] = [
            {
                "type": "tree_preview",
                "content": plan_doc["summary"],
                "items": plan_doc["tree_nodes"],
            }
        ]


async def submit_blueprint_confirmation(
    *,
    project_id: str,
    plan_doc: dict[str, Any] | str | None,
) -> dict[str, Any]:
    project, state = await read_project_state(project_id)
    if not project:
        return {"ok": False, "error": "Project not found"}

    doc = _coerce_doc(plan_doc)
    if not doc:
        return {
            "ok": False,
            "error": "蓝图确认内容为空",
            "error_kind": "empty_blueprint_confirmation",
        }
    if str(doc.get("kind") or "") != "blueprint_tree":
        doc["kind"] = "blueprint_tree"

    previous = pending_blueprint_plan(state)
    iteration = (int(previous.get("iteration", 0)) + 1) if isinstance(previous, dict) else 1
    doc["iteration"] = iteration
    doc = mark_blueprint_confirmation_plan(doc)
    doc.setdefault("id", f"blueprint_confirmation_{int(time.time() * 1000)}")
    doc.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    doc.setdefault("phases", [])
    _attach_tree_preview(project_id, doc)
    doc["review"] = {
        "role": "blueprint_tree",
        "status": "passed",
        "summary": (
            "替换蓝图树已构建，确认后会替换当前蓝图并重新物化节点。"
            if doc.get("replacement")
            else "蓝图树已构建，确认后自动物化为画布节点。"
        ),
    }
    stored = set_pending_blueprint_confirmation(state, doc)
    await write_project_state(project_id, state)
    return {
        "ok": True,
        "plan": stored,
        "approval_role": BLUEPRINT_CONFIRMATION_ROLE,
        "state_key": PENDING_BLUEPRINT_CONFIRMATION_KEY,
        "preview_checklist": [],
        "preview_checklist_size": 0,
        "message": "蓝图已提交，等待用户确认。确认后自动物化。",
    }
