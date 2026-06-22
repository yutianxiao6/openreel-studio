"""Project Blueprint MCP-style tools."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.project_blueprint import (
    apply_blueprint_plan_to_state,
    blueprint_outline_markdown,
    blueprint_paths,
    clear_blueprint_state,
    delete_blueprint_files,
    render_blueprint_markdown,
    render_blueprint_view_model,
    sync_blueprint_outline_document,
    UNTITLED_PROJECT_TITLE,
)
from app.agent.blueprint_revision import (
    apply_pending_blueprint_revision,
    create_pending_revision_from_patch,
)
from app.config import settings
from app.db.models import Project
from app.db.session import session_scope


def _project_root() -> Path:
    return Path(settings.PROJECT_ROOT)


def _active_blueprint_paths(project_id: str, index: dict[str, Any]) -> dict[str, Any]:
    paths = blueprint_paths(project_id)
    rel_json = str(index.get("file_json") or paths["json"])
    rel_markdown = str(index.get("file_markdown") or paths["markdown"])
    rel_view_model = str(index.get("file_view_model") or paths["view_model"])
    root = _project_root()
    return {
        "json": rel_json,
        "markdown": rel_markdown,
        "view_model": rel_view_model,
        "json_abs": root / rel_json,
        "markdown_abs": root / rel_markdown,
        "view_model_abs": root / rel_view_model,
    }


def render_blueprint_view_model_files(
    *,
    project_id: str,
    state: dict[str, Any],
    include_view_model: bool = False,
) -> dict[str, Any]:
    """Regenerate readable blueprint files from canonical blueprint JSON.

    This helper is intentionally deterministic: it reads only the active
    blueprint JSON and writes derived Markdown/view-model files. It never edits
    story facts.
    """
    index = state.get("project_blueprint")
    if not isinstance(index, dict) or not index:
        return {"ok": True, "project_id": project_id, "blueprint": None}

    paths = _active_blueprint_paths(project_id, index)
    json_abs = Path(paths["json_abs"])
    if not json_abs.exists():
        return {
            "ok": False,
            "project_id": project_id,
            "error": "Active blueprint JSON file does not exist.",
            "error_kind": "blueprint_json_missing",
            "file_json": paths["json"],
        }
    try:
        doc = json.loads(json_abs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "project_id": project_id,
            "error": str(exc),
            "error_kind": "blueprint_json_read_failed",
            "file_json": paths["json"],
        }
    if not isinstance(doc, dict):
        return {
            "ok": False,
            "project_id": project_id,
            "error": "Active blueprint JSON must be an object.",
            "error_kind": "blueprint_json_invalid",
            "file_json": paths["json"],
        }

    sync_blueprint_outline_document(doc)
    json_abs.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    file_index = {
        **index,
        "file_json": paths["json"],
        "file_markdown": paths["markdown"],
        "file_view_model": paths["view_model"],
    }
    markdown_abs = Path(paths["markdown_abs"])
    view_model_abs = Path(paths["view_model_abs"])
    markdown_abs.parent.mkdir(parents=True, exist_ok=True)
    view_model_abs.parent.mkdir(parents=True, exist_ok=True)
    view_model = render_blueprint_view_model(doc, file_index)
    markdown_abs.write_text(render_blueprint_markdown(doc, file_index), encoding="utf-8")
    view_model_abs.write_text(
        json.dumps(view_model, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    index["file_json"] = paths["json"]
    index["file_markdown"] = paths["markdown"]
    index["file_view_model"] = paths["view_model"]
    state["project_blueprint"] = index
    result: dict[str, Any] = {
        "ok": True,
        "project_id": project_id,
        "blueprint": index,
        "files": {
            "json": paths["json"],
            "markdown": paths["markdown"],
            "view_model": paths["view_model"],
        },
    }
    if include_view_model:
        result["view_model"] = view_model
    result["outline_markdown"] = blueprint_outline_markdown(doc)
    return result


async def blueprint_get(
    project_id: str,
    include_document: bool = False,
    include_outline: bool = False,
    include_view_model: bool = False,
) -> dict[str, Any]:
    """Read the current project blueprint index and optional derived documents."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found"}
        state = json.loads(project.state_json or "{}")

    index = state.get("project_blueprint")
    if not isinstance(index, dict) or not index:
        return {"ok": True, "project_id": project_id, "blueprint": None}

    result: dict[str, Any] = {
        "ok": True,
        "project_id": project_id,
        "blueprint": index,
        "progress": state.get("blueprint_progress") if isinstance(state.get("blueprint_progress"), dict) else {},
        "section_results": state.get("blueprint_section_results") if isinstance(state.get("blueprint_section_results"), list) else [],
    }
    paths = blueprint_paths(project_id)
    rel_json = str(index.get("file_json") or paths["json"])
    rel_markdown = str(index.get("file_markdown") or paths["markdown"])
    rel_view_model = str(index.get("file_view_model") or paths["view_model"])
    root = Path.cwd()
    try:
        from app.config import settings
        root = Path(settings.PROJECT_ROOT)
    except Exception:
        pass
    json_abs = root / rel_json
    markdown_abs = root / rel_markdown
    view_model_abs = root / rel_view_model
    result["files"] = {
        "json": rel_json,
        "markdown": rel_markdown,
        "view_model": rel_view_model,
        "json_exists": json_abs.exists(),
        "markdown_exists": markdown_abs.exists(),
        "view_model_exists": view_model_abs.exists(),
    }
    document: dict[str, Any] | None = None
    if (include_document or include_outline) and json_abs.exists():
        try:
            loaded = json.loads(json_abs.read_text(encoding="utf-8"))
            document = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            result["document_error"] = str(exc)
    if include_document and document is not None:
        result["document"] = document
    if include_outline and document is not None:
        result["outline_markdown"] = blueprint_outline_markdown(document)
    if include_view_model and view_model_abs.exists():
        try:
            result["view_model"] = json.loads(view_model_abs.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result["view_model_error"] = str(exc)
    return result


async def blueprint_render_view_model(
    project_id: str,
    include_view_model: bool = False,
) -> dict[str, Any]:
    """Regenerate blueprint Markdown and user-facing view model from JSON."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        result = render_blueprint_view_model_files(
            project_id=project_id,
            state=state,
            include_view_model=include_view_model,
        )
        if result.get("ok") and result.get("blueprint") is not None:
            project.state_json = json.dumps(state, ensure_ascii=False)
            session.add(project)
            await session.commit()
    return result


async def blueprint_draft_video(
    project_id: str,
    structure_answer: str = "",
    selected_mode: str = "",
    review_mode: str = "",
) -> dict[str, Any]:
    """Deprecated legacy entry.

    Development builds no longer keep the old section/window creative blueprint
    path alive. Agent-facing video blueprint creation must use
    blueprint.start_tree_draft -> blueprint.append_tree_node ->
    blueprint.finalize_tree_draft.
    """
    return {
        "ok": False,
        "error": "blueprint.draft_video 已迁移下线；请使用增量语义蓝图草稿工具提交完整语义蓝图树。",
        "error_kind": "unsupported_legacy_blueprint_flow",
        "replacement_tool": "blueprint.start_tree_draft -> blueprint.append_tree_node/update_tree_node -> blueprint.finalize_tree_draft",
        "project_id": project_id,
        "ignored_args": {
            "has_structure_answer": bool(str(structure_answer or "").strip()),
            "selected_mode": selected_mode,
            "review_mode": review_mode,
        },
    }


async def blueprint_save_from_plan(project_id: str, plan_doc: dict | str) -> dict[str, Any]:
    """Persist a creative blueprint plan as the project's active blueprint.

    This is primarily for backend/UI migration paths. Normal user approval goes
    through plan.approve, which calls the same persistence helper internally.
    """
    if isinstance(plan_doc, str):
        try:
            plan_doc = json.loads(plan_doc)
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "error": "plan_doc must be a JSON object"}
    if not isinstance(plan_doc, dict):
        return {"ok": False, "error": "plan_doc must be a dict"}

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        saved = apply_blueprint_plan_to_state(
            project_id=project_id,
            state=state,
            plan=plan_doc,
            persist_files=True,
        )
        project.title = saved["title"]
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "blueprint": saved["blueprint"],
            "title": saved["title"],
            "files": {
                "json": saved["paths"]["json"],
                "markdown": saved["paths"]["markdown"],
                "view_model": saved["paths"].get("view_model"),
            },
        }


async def blueprint_clear(project_id: str, reset_title: bool = False) -> dict[str, Any]:
    """Clear the project blueprint. Intended for full reset/internal control."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"ok": False, "error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        cleared = clear_blueprint_state(state)
        deleted = delete_blueprint_files(project_id)
        if reset_title:
            project.title = UNTITLED_PROJECT_TITLE
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            metadata["title"] = UNTITLED_PROJECT_TITLE
            state["metadata"] = metadata
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
    return {
        "ok": True,
        "project_id": project_id,
        "cleared_state_keys": cleared,
        "deleted_files": deleted,
        "title": UNTITLED_PROJECT_TITLE if reset_title else None,
    }


async def blueprint_apply_pending_revision(project_id: str) -> dict[str, Any]:
    """Apply the pending blueprint revision after user confirmation."""
    return await apply_pending_blueprint_revision(project_id)


async def blueprint_revise(
    project_id: str,
    user_request: str = "",
    revision_patch: dict[str, Any] | str = "",
    action: str = "propose",
    auto_apply: bool | None = None,
) -> dict[str, Any]:
    """Propose or confirm a scoped revision to the active project blueprint.

    Boundaries:
    - The model must decide the target section and provide explicit patch ops.
    - This tool never parses the newest user message to infer intent, never
      creates nodes, never runs media, and never rewrites the whole blueprint.
    - Low-risk patches can auto-apply only when the runtime confirmation setting
      allows it; medium/high-risk patches remain pending for user confirmation.
    """
    normalized_action = str(action or "propose").strip().lower()
    if normalized_action in {"apply", "confirm", "approve"}:
        return await apply_pending_blueprint_revision(project_id)
    if normalized_action != "propose":
        return {
            "ok": False,
            "error": "action 只能是 propose 或 apply。",
            "error_kind": "invalid_blueprint_revision_action",
            "allowed_actions": ["propose", "apply"],
        }
    return await create_pending_revision_from_patch(
        project_id=project_id,
        user_request=user_request,
        revision_patch=revision_patch,
        source="blueprint.revise",
        auto_apply=auto_apply,
    )
