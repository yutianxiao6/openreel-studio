"""MCP-style project tools."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import select

from app.db.models import Project, Version
from app.db.session import session_scope
from app.agent.blueprint_tree import summarize_blueprint_for_state
from app.mcp_tools import canvas_tools
from app.services.project_service import DEFAULT_EPISODE_COUNT, ProjectService
from app.services.version_service import VersionService


async def project_list() -> list[dict]:
    async with session_scope() as session:
        svc = ProjectService(session)
        items = await svc.list_projects()
        return [
            {
                "id": p.id,
                "title": p.title,
                "genre": p.genre,
                "episode_count": p.episode_count,
                "status": p.status,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in items
        ]


async def project_create(
    title: str,
    genre: str | None = None,
    episode_count: int = DEFAULT_EPISODE_COUNT,
    description: str | None = None,
    format: str | None = "竖屏短剧",
    duration_per_episode: int = 90,
    budget_level: str = "low",
) -> dict:
    async with session_scope() as session:
        svc = ProjectService(session)
        project = await svc.create_project(
            title=title,
            description=description,
            genre=genre,
            format=format,
            episode_count=episode_count,
            duration_per_episode=duration_per_episode,
            budget_level=budget_level,
        )
        return {"id": project.id, "title": project.title}


async def project_rename(project_id: str, title: str) -> dict:
    async with session_scope() as session:
        svc = ProjectService(session)
        project = await svc.update_project(project_id, {"title": title})
        if not project:
            return {"error": "Project not found"}
        # also reflect in metadata
        await svc.update_project_state(project_id, {"metadata.title": title})
        return {"id": project.id, "title": project.title}


async def project_delete(project_id: str) -> dict:
    async with session_scope() as session:
        svc = ProjectService(session)
        ok = await svc.delete_project(project_id)
        return {"ok": ok}


async def project_get_state(project_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        svc = ProjectService(session)
        state = await svc.get_project_state(project_id)
        if state is None:
            return {"error": f"Project {project_id} not found"}
        result = _project_state_for_status_display(state)
        result["workflow"] = {
            "nodes": await canvas_tools.list_nodes(project_id),
            "edges": await canvas_tools.list_edges(project_id),
        }
        semantic_blueprint = summarize_blueprint_for_state(project_id)
        if semantic_blueprint:
            result["semantic_blueprint"] = semantic_blueprint
            if not isinstance(result.get("project_blueprint"), dict):
                result["suggested_next"] = (
                    "continue_from_existing_legacy_blueprint"
                    if semantic_blueprint.get("needs_finalize")
                    else "read_project_nodes"
                )
                result["model_feedback"] = {
                    "what_went_wrong": "项目存在旧蓝图文件，但当前 Agent 工具面已改为节点优先。",
                    "how_to_fix": (
                        "读取当前节点状态，优先把可用旧蓝图信息转换成 text/image/video 节点；"
                        "后续修改和执行都走 node.list、node.get、node.create、node.update、node.run。"
                    ),
                    "suggested_next": result["suggested_next"],
                }
        result["reference_assets_summary"] = _reference_assets_summary(result)
        result["agent_token_usage_summary"] = _agent_token_usage_summary(result)
        return result


def _has_blueprint_episode_plan(state: dict[str, Any]) -> bool:
    for key in ("project_blueprint", "pending_blueprint_draft", "pending_blueprint_review"):
        value = state.get(key)
        if isinstance(value, dict) and value:
            return True
    return False


def _project_state_for_status_display(state: dict[str, Any]) -> dict[str, Any]:
    """Return state for project/status queries without promoting defaults to facts."""
    result = dict(state)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        if not _has_blueprint_episode_plan(state):
            metadata.pop("episode_count", None)
        result["metadata"] = metadata
    return result


def _reference_assets_summary(state: dict[str, Any]) -> dict[str, Any]:
    store = state.get("reference_assets")
    if not isinstance(store, dict):
        return {"count": 0, "assets": []}
    assets = store.get("assets") if isinstance(store.get("assets"), list) else []
    items: list[dict[str, Any]] = []
    for asset in assets[:20]:
        if not isinstance(asset, dict):
            continue
        analysis = asset.get("analysis") if isinstance(asset.get("analysis"), dict) else {}
        items.append({
            "ref_id": asset.get("ref_id"),
            "mention": asset.get("mention"),
            "aliases": asset.get("aliases") if isinstance(asset.get("aliases"), list) else [],
            "status": asset.get("status"),
            "roles": asset.get("roles") if isinstance(asset.get("roles"), list) else [],
            "filename": asset.get("filename"),
            "style_tags": analysis.get("style_tags") if isinstance(analysis.get("style_tags"), list) else [],
            "analysis_available": asset.get("status") == "analyzed",
        })
    return {
        "count": len(assets),
        "assets": items,
        "bindings_count": len(store.get("bindings") if isinstance(store.get("bindings"), list) else []),
    }


def _percent(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value) * 100, 2)
    return None


def _agent_token_usage_summary(state: dict[str, Any]) -> dict[str, Any]:
    usage = state.get("agent_token_usage")
    if not isinstance(usage, dict):
        return {
            "available": False,
            "note": "当前项目尚未记录模型 token/cache usage。",
        }
    cache_hit_rate = usage.get("cache_hit_rate")
    latest_call_context = usage.get("latest_call_context") if isinstance(usage.get("latest_call_context"), dict) else {}
    latest_context_map = latest_call_context if isinstance(latest_call_context, dict) else {}
    context_peak = usage.get("context_peak") if isinstance(usage.get("context_peak"), dict) else {}
    context_peak_map = context_peak if isinstance(context_peak, dict) else {}
    context_available_rate = latest_context_map.get("context_available_rate", usage.get("context_available_rate"))
    context_used_rate = latest_context_map.get("context_used_rate", usage.get("context_used_rate"))
    context_peak_available_rate = context_peak_map.get("context_available_rate", usage.get("context_peak_available_rate"))
    context_peak_used_rate = context_peak_map.get("context_used_rate", usage.get("context_peak_used_rate"))
    summary = {
        "available": True,
        "llm_calls": usage.get("llm_calls", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cached_prompt_tokens": usage.get("cached_prompt_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_tokens", 0),
        "cache_hit_rate": cache_hit_rate,
        "cache_hit_percent": _percent(cache_hit_rate),
    }
    for key in (
        "cumulative_tokens",
        "latest_call_tokens",
        "latest_call_context",
        "context_peak",
    ):
        if isinstance(usage.get(key), dict):
            summary[key] = usage.get(key)
    for key in (
        "estimated_input_tokens",
        "active_input_tokens",
        "active_input_tokens_source",
        "context_limit_tokens",
        "context_limit_source",
        "context_remaining_tokens",
        "context_usage_scope",
        "context_peak_active_input_tokens",
        "context_peak_active_input_tokens_source",
        "context_peak_limit_tokens",
        "context_peak_limit_source",
        "context_peak_remaining_tokens",
        "context_peak_model",
        "context_peak_usage_scope",
    ):
        if usage.get(key) is not None:
            summary[key] = usage.get(key)
    if context_used_rate is not None:
        summary["context_used_rate"] = context_used_rate
        summary["context_used_percent"] = _percent(context_used_rate)
    if context_available_rate is not None:
        summary["context_available_rate"] = context_available_rate
        summary["context_available_percent"] = _percent(context_available_rate)
    if context_peak_used_rate is not None:
        summary["context_peak_used_rate"] = context_peak_used_rate
        summary["context_peak_used_percent"] = _percent(context_peak_used_rate)
    if context_peak_available_rate is not None:
        summary["context_peak_available_rate"] = context_peak_available_rate
        summary["context_peak_available_percent"] = _percent(context_peak_available_rate)
    return summary


async def project_update_state(project_id: str, patch: dict | str) -> dict:
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except (json.JSONDecodeError, TypeError):
            return {"error": "patch must be a JSON object, not a string"}
    if not isinstance(patch, dict):
        return {"error": "patch must be a dict"}
    async with session_scope() as session:
        svc = ProjectService(session)
        project = await svc.update_project_state(project_id, patch)
        if not project:
            return {"error": f"Project {project_id} not found"}
        return {"ok": True, "project_id": project_id}


async def project_lock_field(project_id: str, field_path: str) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        locked = set(state.get("locked_fields", []))
        locked.add(field_path)
        state["locked_fields"] = sorted(locked)
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {"locked_fields": state["locked_fields"]}


async def project_unlock_field(project_id: str, field_path: str) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        locked = [f for f in state.get("locked_fields", []) if f != field_path]
        state["locked_fields"] = locked
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {"locked_fields": locked}


async def project_save_version(
    project_id: str,
    target_type: str,
    target_id: str,
    snapshot: dict,
    message: str = "",
) -> dict:
    async with session_scope() as session:
        svc = VersionService(session)
        version = await svc.save_version(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            snapshot=snapshot,
            message=message,
        )
        return {
            "version_id": version.id,
            "version_number": version.version_number,
        }


async def project_list_versions(
    project_id: str, target_type: str | None = None, target_id: str | None = None
) -> list[dict]:
    async with session_scope() as session:
        stmt = select(Version).where(Version.project_id == project_id)
        if target_type:
            stmt = stmt.where(Version.target_type == target_type)
        if target_id:
            stmt = stmt.where(Version.target_id == target_id)
        stmt = stmt.order_by(Version.created_at.desc()).limit(50)
        rows = (await session.exec(stmt)).all()
        return [
            {
                "id": v.id,
                "target_type": v.target_type,
                "target_id": v.target_id,
                "version_number": v.version_number,
                "message": v.message,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in rows
        ]


async def project_restore_version(version_id: str) -> dict:
    """Restore a snapshot back into project state. The snapshot is expected to
    contain {"after": {...}} or {"state": {...}}; we merge that under the
    appropriate key.
    """
    async with session_scope() as session:
        version = await session.get(Version, version_id)
        if not version:
            return {"error": "Version not found"}
        snapshot = json.loads(version.snapshot_json or "{}")
        payload = snapshot.get("after") or snapshot.get("state") or snapshot

        project = await session.get(Project, version.project_id)
        if not project:
            return {"error": "Project not found"}

        if version.target_type == "project":
            project.state_json = json.dumps(payload, ensure_ascii=False)
        else:
            state = json.loads(project.state_json or "{}")
            bucket = state.setdefault(version.target_type + "s", {})
            if isinstance(bucket, dict):
                bucket[version.target_id] = payload
            project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {
            "ok": True,
            "project_id": version.project_id,
            "target_type": version.target_type,
            "target_id": version.target_id,
        }
