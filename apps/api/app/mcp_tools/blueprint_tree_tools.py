"""Blueprint tree tools.

The agent-facing path builds a semantic tree incrementally:
start_tree_draft -> append_tree_node* -> finalize_tree_draft. Low-level tree CRUD
tools stay registered for internal/front-end compatibility but remain hidden
from the default Agent tool surface.
"""
from __future__ import annotations

import logging
import copy
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from app.agent.blueprint_confirmation import submit_blueprint_confirmation
from app.agent.blueprint_confirmation_state import pending_blueprint_plan
from app.agent.project_state_io import read_project_state
from app.agent.blueprint_tree_normalizer import (
    _DRAFT_PATCH_FIELD_ALIASES_TO_FIELDS,
    _MAX_APPEND_NODES,
    _PROMPT_EVIDENCE_FIELDS,
    _VALID_AUDIO_FIELDS,
    _VALID_IMAGE_FIELDS,
    _VALID_TEXT_FIELDS,
    _VALID_VIDEO_FIELDS,
    _VIDEO_ASPECT_RATIOS,
    _as_bool,
    _aspect_ratio_conflict,
    _available_node_refs,
    _coerce_tree_children,
    _collect_node_ids,
    _field_text,
    _is_segment_node,
    _node_materializes,
    _node_summary,
    _node_text,
    _normalize_all_links,
    _normalize_node_links,
    _normalize_node_type,
    _normalize_prompt_evidence_fields,
    _normalize_prompt_evidence_for_nodes,
    _normalize_references,
    _normalize_root_child_order,
    _normalize_segment_child_order,
    _normalize_semantic_node,
    _parse_jsonish,
    _preview_tree_nodes,
    _prompt_evidence_error,
    _prompt_text,
    _slug,
    _tree_summary,
    _walk_nodes,
    _walk_nodes_with_parent,
)
from app.agent.blueprint_tree_repair import (
    _auto_repair_default_segment_container,
    _auto_repair_flat_segment_media,
    _auto_repair_video_dependencies,
    _auto_repair_video_production_paths,
)
from app.agent.blueprint_tree_store import (
    _REPLACEMENT_DRAFT_KEY,
    _active_blueprint_exists,
    _active_blueprint_ref,
    _draft_container,
    _draft_mode,
    _draft_root,
    _empty_semantic_root,
    _normalize_draft_mode,
    _pending_blueprint_tree_exists,
    _replacement_draft,
)
from app.agent.blueprint_tree_validator import (
    _runtime_evidence_error,
    _semantic_quality_error,
    _video_output_readiness_error,
    validate_children_for_review,
)
from app.agent.blueprint_tree_plan_bridge import build_blueprint_tree_plan_doc
from app.agent.blueprint_stage_protocol import (
    BLUEPRINT_STAGE_PROTOCOL_VERSION,
    blueprint_stage_protocol_payload,
)
from app.agent.blueprint_tree import (
    add_child,
    blueprint_exists,
    blueprint_has_content,
    delete_node,
    find_node,
    find_parent,
    list_children,
    read_blueprint,
    update_node,
    write_blueprint,
)
from app.mcp_tools.registry import register

logger = logging.getLogger(__name__)


def _draft_model_feedback(*, action: str = "continue_drafting_or_finalize") -> dict[str, Any]:
    return {
        "what_went_wrong": "语义蓝图仍是 drafting 草稿，尚未提交为待确认蓝图。",
        "how_to_fix": (
            "按蓝图阶段协议先确认关键规模事实、读取所需 full guide，再使用 "
            "blueprint.append_tree_node 或 blueprint.update_tree_node 完成草稿；"
            "如果已经完整，先完成必要只读 review，然后调用 blueprint.finalize_tree_draft。"
        ),
        "suggested_next": action,
        "protocol_version": BLUEPRINT_STAGE_PROTOCOL_VERSION,
        "state_note": "文本回复不会创建、修改、提交蓝图，也不会物化工程节点。",
    }


_TREE_CONTEXT_TEXT_LIMIT = 360
_TREE_CONTEXT_FIELD_KEYS = (
    "episode_count",
    "segment_seconds",
    "production_basis",
    "production_path",
    "duration",
    "duration_seconds",
    "aspect_ratio",
    "resolution",
    "quality",
    "prompt_source",
    "prompt_template",
    "template_selection_reason",
)


def _compact_tree_text(value: Any, *, limit: int = _TREE_CONTEXT_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_tree_node(node: dict[str, Any]) -> dict[str, Any]:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    payload: dict[str, Any] = {
        "id": node.get("id"),
        "type": node.get("type"),
        "title": node.get("title"),
    }
    if node.get("status") not in (None, ""):
        payload["status"] = node.get("status")
    if node.get("materialize") is not None:
        payload["materialize"] = node.get("materialize")
    for key in ("content", "description", "prompt", "negative_prompt"):
        value = node.get(key) or fields.get(key)
        if value not in (None, "", [], {}):
            payload[key] = _compact_tree_text(value)
    compact_fields = {
        key: fields.get(key)
        for key in _TREE_CONTEXT_FIELD_KEYS
        if fields.get(key) not in (None, "", [], {})
    }
    if compact_fields:
        payload["fields"] = compact_fields
    refs = _normalize_references(node.get("references") or fields.get("references"))
    deps = _normalize_references(node.get("depends_on") or fields.get("depends_on"))
    if refs:
        payload["references"] = refs
    if deps:
        payload["depends_on"] = deps
    children = [
        _compact_tree_node(child)
        for child in (node.get("children") or [])
        if isinstance(child, dict)
    ]
    if children or node.get("id") == "root":
        payload["children"] = children
    return payload


def _current_tree_context(root: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_count": _tree_summary(root).get("node_count", 0),
        "root": _compact_tree_node(root if isinstance(root, dict) else {"id": "root", "children": []}),
        "model_note": (
            "这是当前完整草稿树的压缩视图；父子层级表示 UI 分组，生产顺序看 references/depends_on。"
            "如需某个节点全文，使用 blueprint.get(node_id=...)。"
        ),
    }


def _root_with_prospective_child(root: dict[str, Any], parent_id: str, node: dict[str, Any]) -> dict[str, Any]:
    prospective_root = copy.deepcopy(root if isinstance(root, dict) else {"id": "root", "children": []})
    parent = find_node(prospective_root, parent_id or "root")
    if parent is None:
        return prospective_root
    parent.setdefault("children", []).append(copy.deepcopy(node))
    return prospective_root


def _append_guide_readiness_error(
    *,
    state: dict[str, Any],
    root: dict[str, Any],
    container: dict[str, Any],
    doc: dict[str, Any],
) -> dict[str, Any] | None:
    fields = container.get("fields") if isinstance(container.get("fields"), dict) else {}
    root_fields = root.get("fields") if isinstance(root.get("fields"), dict) else {}
    blueprint_fields = {**root_fields, **fields}
    readiness_error = _video_output_readiness_error(
        state,
        root.get("children") if isinstance(root.get("children"), list) else [],
        source_request=str(container.get("source_request") or doc.get("source_request") or ""),
        summary=str(container.get("summary") or doc.get("summary") or root.get("content") or ""),
        blueprint_fields=blueprint_fields,
    )
    if not readiness_error or readiness_error.get("error_kind") != "guide_not_loaded":
        return None
    result = dict(readiness_error)
    result["error"] = "视频蓝图建树前缺少完整制作指南读取记录，暂不写入草稿节点。"
    result["suggested_next"] = "load_missing_guides_then_append"
    result["current_tree"] = _current_tree_context(root)
    result["blueprint_stage_protocol"] = blueprint_stage_protocol_payload(max_append_nodes=_MAX_APPEND_NODES)
    return result

# ── CRUD tools ────────────────────────────────────────────────────────────────


_INITIAL_BLUEPRINT_FACT_FIELDS = ("episode_count", "segment_seconds", "production_basis")


def _positive_int_field(value: Any) -> int | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    parsed = int(digits)
    return parsed if parsed > 0 else None


def _normalize_production_basis_field(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text:
        return ""
    if any(marker in lowered for marker in ("text_to_video", "text-to-video", "t2v", "文生", "文本生成视频")):
        return "text_to_video"
    if any(marker in lowered for marker in ("image_to_video", "image-to-video", "i2v", "图生", "参考图", "分镜图", "首帧", "尾帧")):
        return "image_to_video"
    if any(marker in lowered for marker in ("model_decide", "model decide", "模型判断", "模型决定", "模型规划", "模型发挥", "由模型")):
        return "model_decide"
    return text


def _infer_production_basis_from_text(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text:
        return ""
    if any(marker in lowered for marker in ("text_to_video", "text-to-video", "t2v", "文生", "文本生成视频")):
        return "text_to_video"
    if any(marker in lowered for marker in ("image_to_video", "image-to-video", "i2v", "图生", "参考图", "分镜图", "首帧", "尾帧")):
        return "image_to_video"
    if any(marker in lowered for marker in ("model_decide", "model decide", "模型判断", "模型决定", "模型规划", "模型发挥", "由模型")):
        return "model_decide"
    return ""


def _infer_duration_seconds_from_text(*parts: Any) -> int | None:
    text = "\n".join(str(part or "") for part in parts if part not in (None, "", [], {}))
    if not text:
        return None
    patterns = (
        r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec|secs|second|seconds)",
        r"(?:时长|总时长|长度|duration)\D{0,8}(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            seconds = int(math.ceil(float(match.group(1))))
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return None


def _initial_blueprint_fields(
    *,
    episode_count: Any,
    segment_seconds: Any,
    production_basis: Any,
    title: Any = "",
    summary: Any = "",
    source_request: Any = "",
) -> tuple[dict[str, Any], list[str]]:
    fields: dict[str, Any] = {}
    missing: list[str] = []
    inferred_duration = _infer_duration_seconds_from_text(source_request, summary, title)
    text_context = "\n".join(str(item or "") for item in (source_request, summary, title))
    episode = _positive_int_field(episode_count)
    segment = _positive_int_field(segment_seconds)
    basis = _normalize_production_basis_field(production_basis) or _infer_production_basis_from_text(text_context)
    if episode is None and inferred_duration is not None:
        episode = 1
    if segment is None and inferred_duration is not None:
        segment = inferred_duration if inferred_duration <= 15 else 15
    if episode is None:
        missing.append("episode_count")
    else:
        fields["episode_count"] = episode
    if segment is None:
        missing.append("segment_seconds")
    else:
        fields["segment_seconds"] = segment
    if not basis:
        missing.append("production_basis")
    else:
        fields["production_basis"] = basis
    return fields, missing


def _set_initial_blueprint_fields(container: dict[str, Any], fields: dict[str, Any]) -> None:
    current = container.get("fields") if isinstance(container.get("fields"), dict) else {}
    container["fields"] = {**current, **fields}
    root = container.get("root") if isinstance(container.get("root"), dict) else {}
    if root:
        root_fields = root.get("fields") if isinstance(root.get("fields"), dict) else {}
        root["fields"] = {**root_fields, **fields}


def _ratio_from_pending_fields(fields: Any) -> str:
    if not isinstance(fields, list):
        return ""
    for field in fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("id") or "") != "aspect_ratio":
            continue
        for key in ("raw_value", "value"):
            value = str(field.get(key) or "").strip()
            if value in _VIDEO_ASPECT_RATIOS:
                return value
    return ""


async def _pending_intake_aspect_ratio(project_id: str) -> str:
    if not project_id:
        return ""
    try:
        from app.db.models import Project
        from app.db.session import session_scope

        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return ""
            state = json.loads(project.state_json or "{}")
    except Exception:
        logger.exception("failed to read pending intake aspect ratio")
        return ""

    pending = state.get("pending_video_blueprint_request")
    if not isinstance(pending, dict):
        return ""
    ratio = _ratio_from_pending_fields(pending.get("basic_answers"))
    if ratio:
        return ratio
    ratio = _ratio_from_pending_fields(pending.get("structure_answers"))
    if ratio:
        return ratio
    text = "\n".join(
        str(pending.get(key) or "")
        for key in ("basic_answer", "structure_answer", "raw_request")
    )
    for candidate in ("16:9", "9:16"):
        if candidate in text:
            return candidate
    return ""


async def _project_state_for_validation(project_id: str) -> dict[str, Any]:
    if not project_id:
        return {}
    try:
        from app.db.models import Project
        from app.db.session import session_scope

        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return {}
            state = json.loads(project.state_json or "{}")
            return state if isinstance(state, dict) else {}
    except Exception:
        logger.exception("failed to read project state for blueprint validation")
        return {}


async def _blueprint_node_id_for_workflow_node(project_id: str, workflow_node_id: str) -> str:
    if not project_id or not workflow_node_id:
        return ""
    try:
        from app.db.models import WorkflowNode
        from app.db.session import session_scope

        async with session_scope() as session:
            node = await session.get(WorkflowNode, workflow_node_id)
            if node is None or str(node.project_id) != str(project_id):
                return ""
            payload = json.loads(node.input_json or "{}")
            if not isinstance(payload, dict):
                return ""
            return str(payload.get("blueprint_node_id") or "").strip()
    except Exception:
        logger.exception("failed to resolve workflow node to blueprint node")
        return ""












async def _pending_blueprint_tree_guard_info(project_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    refs = _available_node_refs(root)
    info: dict[str, Any] = {
        "exists": _pending_blueprint_tree_exists(doc),
        "status": doc.get("status"),
        "title": doc.get("title") or root.get("title") or "",
        "available_nodes": refs,
    }
    try:
        _, state = await read_project_state(project_id)
    except Exception:
        state = {}
    pending = pending_blueprint_plan(state)
    if isinstance(pending, dict):
        info["exists"] = True
        info["plan_id"] = pending.get("id")
        info["title"] = pending.get("title") or info["title"]
        if not refs:
            tree_nodes = pending.get("tree_nodes") if isinstance(pending.get("tree_nodes"), list) else []
            info["available_nodes"] = [
                _node_summary(node)
                for node in tree_nodes[:24]
                if isinstance(node, dict)
            ]
    return info


















































async def _validate_children_for_review(
    project_id: str,
    children: list[dict[str, Any]],
    *,
    require_runtime_evidence: bool = False,
    current_tree_version: Any = None,
    source_request: str = "",
    summary: str = "",
    blueprint_fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return await validate_children_for_review(
        project_id,
        children,
        get_project_state=_project_state_for_validation,
        get_pending_aspect_ratio=_pending_intake_aspect_ratio,
        require_runtime_evidence=require_runtime_evidence,
        current_tree_version=current_tree_version,
        source_request=source_request,
        summary=summary,
        blueprint_fields=blueprint_fields,
    )


async def _submit_tree_plan(
    *,
    project_id: str,
    doc: dict[str, Any],
    title: str,
    summary: str,
    source_request: str,
    require_runtime_evidence: bool = False,
) -> dict[str, Any]:
    container = _draft_container(doc)
    replacement = container is not doc
    root = container.get("root") if isinstance(container.get("root"), dict) else {}
    children = root.get("children") if isinstance(root.get("children"), list) else []
    if not children:
        return {
            "ok": False,
            "error": "蓝图草稿没有节点，先用 blueprint.append_tree_node 添加单个节点或少量同父级批量节点。",
            "error_kind": "empty_tree",
        }
    _normalize_root_child_order(root)
    children = root.get("children") if isinstance(root.get("children"), list) else []
    auto_repairs = _auto_repair_default_segment_container(root)
    auto_repairs.extend(_auto_repair_flat_segment_media(root))
    if auto_repairs:
        children = root.get("children") if isinstance(root.get("children"), list) else []
    auto_dependency_repairs = _auto_repair_video_dependencies(root)
    auto_production_path_repairs = _auto_repair_video_production_paths(root)
    _normalize_segment_child_order(root)
    children = root.get("children") if isinstance(root.get("children"), list) else []
    _normalize_all_links(children)

    review_error = await _validate_children_for_review(
        project_id,
        children,
        require_runtime_evidence=require_runtime_evidence,
        current_tree_version=doc.get("tree_version"),
        source_request=source_request,
        summary=summary,
        blueprint_fields=container.get("fields") if isinstance(container.get("fields"), dict) else None,
    )
    if review_error:
        return review_error

    now = datetime.now(timezone.utc).isoformat()
    root.update({
        "id": "root",
        "type": "story",
        "title": title or root.get("title") or doc.get("title") or "视频蓝图",
        "content": summary or root.get("content") or doc.get("summary") or "",
        "status": "pending_review",
        "materialize": False,
        "updated_at": now,
    })
    container["root"] = root
    container["title"] = title or root.get("title") or container.get("title") or doc.get("title") or "视频蓝图"
    container["summary"] = summary or root.get("content") or container.get("summary") or doc.get("summary") or ""
    container["status"] = "pending_review"
    container["schema_name"] = "semantic_blueprint_tree"
    container["semantic_version"] = 1
    container["source_request"] = source_request or container.get("source_request") or doc.get("source_request") or ""
    container["updated_at"] = now
    if replacement:
        doc[_REPLACEMENT_DRAFT_KEY] = container
        doc["updated_at"] = now
    else:
        doc["root"] = root
        doc["title"] = container["title"]
        doc["summary"] = container["summary"]
        doc.pop("video_mode", None)
        doc.pop("video_generation_type", None)
        doc.pop("image_to_video_method", None)
        doc["status"] = "pending_review"
        doc["schema_name"] = "semantic_blueprint_tree"
        doc["semantic_version"] = 1
        doc["source_request"] = container["source_request"]
        doc["updated_at"] = now
    tree_version = write_blueprint(project_id, doc)

    summary_payload = _tree_summary(root)
    preview_nodes = _preview_tree_nodes(root)
    plan_doc = build_blueprint_tree_plan_doc(
        container=container,
        tree_version=tree_version,
        tree_summary=summary_payload,
        tree_nodes=preview_nodes,
        replacement=replacement,
    )

    plan = await submit_blueprint_confirmation(
        project_id=project_id,
        plan_doc=plan_doc,
    )
    if plan.get("error") or plan.get("ok") is False:
        return {
            "ok": False,
            "error": plan.get("error") or "提交蓝图树确认失败",
            "error_kind": plan.get("error_kind") or "blueprint_confirmation_failed",
            "tree_version": tree_version,
            "tree_summary": summary_payload,
        }

    return {
        "ok": True,
        "tree_version": tree_version,
        "tree_summary": summary_payload,
        "draft_mode": "replacement" if replacement else "new",
        "replacement": replacement,
        "auto_repairs": auto_repairs,
        "auto_dependency_repairs": auto_dependency_repairs,
        "auto_production_path_repairs": auto_production_path_repairs,
        "plan": plan.get("plan"),
        "preview_checklist": plan.get("preview_checklist") or [],
        "message": "语义蓝图树已提交，等待用户确认。确认后才会物化节点。",
    }


async def _refresh_pending_tree_plan_if_needed(
    *,
    project_id: str,
    doc: dict[str, Any],
    title: str = "",
    summary: str = "",
    source_request: str = "",
) -> dict[str, Any] | None:
    _, state = await read_project_state(project_id)
    pending = pending_blueprint_plan(state)
    if not isinstance(pending, dict):
        return None
    container = _draft_container(doc)
    return await _submit_tree_plan(
        project_id=project_id,
        doc=doc,
        title=title or str(pending.get("title") or container.get("title") or doc.get("title") or ""),
        summary=summary or str(container.get("summary") or pending.get("summary") or doc.get("summary") or ""),
        source_request=source_request or str(pending.get("source_request") or container.get("source_request") or doc.get("source_request") or ""),
        require_runtime_evidence=False,
    )


@register(
    "blueprint.start_tree_draft",
    description=(
        "开始增量语义蓝图草稿并记录入口字段；成功后先读 full 指南，再追加内容节点。"
        "无 pending tree 时首次建树；已有待确认树默认 update/append 后 finalize，"
        "明确重建才传 mode='replacement' 和 replace_reason。该工具不覆盖 active blueprint、创建节点、批准或运行媒体。"
    ),
    tags=["blueprint", "write"],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "source_request": {"type": "string"},
            "episode_count": {"type": "integer", "description": "蓝图初始事实：项目按几集组织。能从时长推断时直接填；15秒及以内默认1集，不询问。"},
            "segment_seconds": {"type": "integer", "description": "蓝图初始事实：每段多少秒。能从时长推断时直接填；15秒及以内默认单段等于总秒数，不询问分段。"},
            "production_basis": {
                "type": "string",
                "description": "蓝图初始事实：text_to_video、image_to_video 或 model_decide。可从参考图/分镜图/流程描述推断；无法推断时再问。",
            },
            "mode": {"type": "string", "enum": ["new", "replacement"], "default": "new"},
            "replace_reason": {"type": "string"},
        },
        "required": ["project_id", "title"],
    },
)
async def blueprint_start_tree_draft(
    project_id: str = "",
    title: str = "",
    summary: str = "",
    source_request: str = "",
    episode_count: int | str | None = None,
    segment_seconds: int | str | None = None,
    production_basis: str = "",
    mode: str = "new",
    replace_reason: str = "",
) -> dict[str, Any]:
    doc = read_blueprint(project_id)
    draft_mode = _normalize_draft_mode(mode)
    active_exists = _active_blueprint_exists(doc)
    pending_info = await _pending_blueprint_tree_guard_info(project_id, doc)
    if pending_info.get("exists") and not active_exists:
        available_nodes = pending_info.get("available_nodes") if isinstance(pending_info.get("available_nodes"), list) else []
        return {
            "ok": False,
            "error": "当前项目已有草稿/待确认蓝图树，不能重新开始并覆盖它。",
            "error_kind": "pending_blueprint_tree_exists",
            "hint": "继续在现有草稿上用 blueprint.update_tree_node 或 blueprint.append_tree_node 修改，再调用 blueprint.finalize_tree_draft；如果用户要放弃当前 pending tree，应走明确重置/清理确认，而不是重新 start。",
            "status": pending_info.get("status"),
            "plan_id": pending_info.get("plan_id"),
            "title": pending_info.get("title"),
            "available_node_ids": [node.get("id") for node in available_nodes if node.get("id")],
            "available_nodes": available_nodes,
        }
    if active_exists and draft_mode != "replacement":
        return {
            "ok": False,
            "error": "当前项目已有已确认蓝图。若用户要求整体重建错误树，请用 mode='replacement' 创建替换草稿；局部修改仍用 blueprint.revise。",
            "error_kind": "active_blueprint_exists",
            "hint": "调用 blueprint.start_tree_draft(title=..., mode='replacement', replace_reason='用户要求重建错误树')。",
        }
    initial_fields, missing_initial_fields = _initial_blueprint_fields(
        episode_count=episode_count,
        segment_seconds=segment_seconds,
        production_basis=production_basis,
        title=title,
        summary=summary,
        source_request=source_request,
    )
    if missing_initial_fields:
        return {
            "ok": False,
            "error": "开始蓝图草稿前缺少入口制作事实。",
            "error_kind": "missing_initial_blueprint_fields",
            "missing_fields": missing_initial_fields,
            "required_fields": list(_INITIAL_BLUEPRINT_FACT_FIELDS),
            "hint": (
                "能从时长和用户描述推断的规模事实直接填写：15秒及以内默认 episode_count=1、"
                "segment_seconds=总秒数。只有无法从用户消息、collected_facts 或项目状态推断时，"
                "才用 interaction.request_input 补问缺失字段；制作依据缺失时优先问文生视频还是图生视频。"
            ),
            "blueprint_stage_protocol": blueprint_stage_protocol_payload(max_append_nodes=_MAX_APPEND_NODES),
        }
    now = datetime.now(timezone.utc).isoformat()
    if active_exists and draft_mode == "replacement":
        root = _empty_semantic_root(title or doc.get("title") or "视频蓝图", summary, now)
        doc[_REPLACEMENT_DRAFT_KEY] = {
            "mode": "replacement",
            "status": "drafting",
            "title": title or "替换蓝图草稿",
            "summary": summary or "",
            "source_request": source_request,
            "replace_reason": replace_reason or "用户要求重建当前蓝图树",
            "replaces": _active_blueprint_ref(doc),
            "root": root,
            "schema_name": "semantic_blueprint_tree",
            "semantic_version": 1,
            "created_at": now,
            "updated_at": now,
        }
        _set_initial_blueprint_fields(doc[_REPLACEMENT_DRAFT_KEY], initial_fields)
        doc["updated_at"] = now
    else:
        doc.pop(_REPLACEMENT_DRAFT_KEY, None)
        doc["root"] = _empty_semantic_root(title or doc.get("title") or "视频蓝图", summary, now)
        doc["title"] = title or doc.get("title") or "视频蓝图"
        doc["summary"] = summary or ""
        doc["status"] = "drafting"
        doc["schema_name"] = "semantic_blueprint_tree"
        doc["semantic_version"] = 1
        doc["source_request"] = source_request
        doc["updated_at"] = now
        doc.pop("video_mode", None)
        doc.pop("video_generation_type", None)
        doc.pop("image_to_video_method", None)
        _set_initial_blueprint_fields(doc, initial_fields)
    tree_version = write_blueprint(project_id, doc)
    root = _draft_root(doc)
    return {
        "ok": True,
        "tree_version": tree_version,
        "node_id": "root",
        "draft_mode": _draft_mode(doc),
        "replacement": _draft_mode(doc) == "replacement",
        "replaces": (_replacement_draft(doc) or {}).get("replaces"),
        "tree_summary": _tree_summary(root),
        "current_tree": _current_tree_context(root),
        "initial_fields": initial_fields,
        "status": "drafting",
        "finalized": False,
        "suggested_next": "load_guides_before_append",
        "model_feedback": _draft_model_feedback(action="load_guides_before_append"),
        "blueprint_stage_protocol": blueprint_stage_protocol_payload(max_append_nodes=_MAX_APPEND_NODES),
        "message": "语义蓝图草稿已开始；先读取所需 full guide 并处理阻塞信息，再追加内容节点。",
    }


@register(
    "blueprint.append_tree_node",
    description=(
        "向 pending 草稿追加或 upsert text/image/video/audio 语义节点；视频草稿追加前需已读取所需 full guide。"
        "node 用于单个节点，nodes 用于少量同父级节点；parent_id 表示分组，生产依赖写 references/depends_on。"
        "该工具不运行、批准或覆盖整树。"
    ),
    tags=["blueprint", "write"],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "parent_id": {"type": "string", "default": "root"},
            "node": {"type": "object"},
            "tree_node": {"type": "object"},
            "nodes": {"type": "array", "items": {"type": "object"}},
            "mode": {"type": "string", "enum": ["append", "upsert"], "default": "append"},
        },
        "required": ["project_id"],
    },
)
async def blueprint_append_tree_node(
    project_id: str = "",
    parent_id: str = "root",
    node: dict[str, Any] | str | None = None,
    tree_node: dict[str, Any] | str | None = None,
    child: dict[str, Any] | str | None = None,
    item: dict[str, Any] | str | None = None,
    nodes: list[Any] | str | None = None,
    mode: str = "append",
) -> dict[str, Any]:
    if node is None:
        node = tree_node if tree_node is not None else child if child is not None else item
    raw_node = _parse_jsonish(node)
    raw_nodes = _parse_jsonish(nodes)
    if not isinstance(raw_node, dict) and isinstance(raw_nodes, list):
        if len(raw_nodes) > _MAX_APPEND_NODES:
            return {
                "ok": False,
                "error": f"一次最多追加 {_MAX_APPEND_NODES} 个蓝图语义节点；当前 {len(raw_nodes)} 个。",
                "error_kind": "blueprint_append_batch_too_large",
                "max_nodes": _MAX_APPEND_NODES,
                "hint": "少量节点一次提交；较大蓝图按父级或制作阶段拆批继续 append，最后统一 finalize。",
            }
        appended: list[dict[str, Any]] = []
        last_result: dict[str, Any] = {}
        for entry in raw_nodes:
            result = await blueprint_append_tree_node(
                project_id=project_id,
                parent_id=parent_id,
                node=entry,
                mode=mode,
            )
            last_result = result
            if not result.get("ok"):
                return {
                    **result,
                    "partial_nodes": appended,
                    "hint": (
                        str(result.get("hint") or "")
                        + " nodes 列表按顺序部分追加；失败后请修正当前条目再继续。"
                    ).strip(),
                }
            appended.append(result.get("node") if isinstance(result.get("node"), dict) else {"ok": True})
        return {
            "ok": True,
            "parent_id": parent_id or "root",
            "nodes": appended,
            "count": len(appended),
            "tree_version": last_result.get("tree_version"),
            "tree_summary": last_result.get("tree_summary"),
            "current_tree": last_result.get("current_tree"),
            "refreshed_pending_confirmation": last_result.get("refreshed_pending_confirmation"),
            "status": "drafting",
            "finalized": False,
            "suggested_next": "continue_drafting_or_finalize",
            "model_feedback": _draft_model_feedback(),
            "message": f"已追加 {len(appended)} 个草稿节点。",
        }
    if not isinstance(raw_node, dict):
        return {"ok": False, "error": "node 必须是一个 JSON 对象", "error_kind": "missing_node"}

    doc = read_blueprint(project_id)
    if _active_blueprint_exists(doc) and _replacement_draft(doc) is None:
        return {
            "ok": False,
            "error": "当前项目已有已确认蓝图，不能直接追加草稿节点。若用户要求整体重建错误树，请先用 blueprint.start_tree_draft(mode='replacement')。",
            "error_kind": "active_blueprint_exists",
        }
    container = _draft_container(doc)
    root = container.get("root") if isinstance(container.get("root"), dict) else {}
    if not root:
        now = datetime.now(timezone.utc).isoformat()
        root = _empty_semantic_root(str(container.get("title") or doc.get("title") or "视频蓝图"), str(container.get("summary") or doc.get("summary") or ""), now)
        container["root"] = root

    parent = find_node(root, parent_id or "root")
    if parent is None:
        return {
            "ok": False,
            "error": f"父节点 {parent_id!r} 不存在。先追加父节点，或把 parent_id 设为 root。",
            "error_kind": "parent_not_found",
        }

    existing_ids = _collect_node_ids(root.get("children") if isinstance(root.get("children"), list) else [])
    raw_id = str(raw_node.get("id") or "").strip()
    candidate_id = _slug(raw_id or raw_node.get("title") or "node", "node")
    if candidate_id in existing_ids:
        if str(mode or "").strip().lower() == "upsert":
            patch = {
                key: value
                for key, value in raw_node.items()
                if key not in {"id", "type", "children"} and value not in (None, "", [], {})
            }
            patch.setdefault("parent_id", parent_id or "root")
            updated = await blueprint_update_tree_node(
                project_id=project_id,
                node_id=candidate_id,
                patch=patch,
            )
            if isinstance(updated, dict) and updated.get("ok"):
                updated["upserted"] = True
                updated["message"] = f"已更新既有草稿节点：{candidate_id}"
            return updated
        return {
            "ok": False,
            "error": f"节点 id {candidate_id!r} 已存在。请更新已有节点或换一个稳定 id。",
            "error_kind": "duplicate_node_id",
            "node_id": candidate_id,
            "hint": "如果这是有意修改草稿节点，可用 blueprint.append_tree_node(mode='upsert', node={...}) 或 blueprint.update_tree_node。",
        }

    errors: list[str] = []
    normalized = _normalize_semantic_node(
        raw_node,
        index_path=f"{parent_id or 'root'}.child",
        seen_ids={"root", *existing_ids},
        errors=errors,
    )
    if errors or normalized is None:
        return {
            "ok": False,
            "error": "节点校验失败：" + "；".join(errors[:8]),
            "error_kind": "semantic_tree_validation_failed",
            "errors": errors[:20],
        }
    _normalize_node_links(normalized, existing_ids | _collect_node_ids([normalized]))
    state = await _project_state_for_validation(project_id)
    _normalize_prompt_evidence_fields(normalized, state=state, default_missing=True)
    _normalize_prompt_evidence_for_nodes(
        normalized.get("children") if isinstance(normalized.get("children"), list) else [],
        state=state,
        default_missing=True,
    )
    prospective_root = _root_with_prospective_child(root, parent_id or "root", normalized)
    guide_error = _append_guide_readiness_error(
        state=state,
        root=prospective_root,
        container=container,
        doc=doc,
    )
    if guide_error:
        return guide_error
    prompt_error = _prompt_evidence_error([normalized])
    if prompt_error:
        prompt_error = dict(prompt_error)
        prompt_error.setdefault("error", "媒体节点 prompt 缺少提示词证据，请补齐后重新追加。")
        prompt_error["hint"] = (
            f"{prompt_error.get('hint', '')} append 时也可以直接在 node 顶层传 "
            "prompt_source/prompt_template/template_selection_reason。"
        )
        return prompt_error
    expected_aspect_ratio = await _pending_intake_aspect_ratio(project_id)
    conflict = _aspect_ratio_conflict(expected_aspect_ratio, [normalized])
    if conflict:
        return {
            "ok": False,
            "error": (
                f"节点与用户画幅要求冲突：用户选择 {expected_aspect_ratio}，"
                f"但节点内容包含 {conflict}。"
            ),
            "error_kind": "aspect_ratio_conflict",
            "expected_aspect_ratio": expected_aspect_ratio,
            "conflicting_value": conflict,
        }

    parent.setdefault("children", []).append(normalized)
    parent["updated_at"] = datetime.now(timezone.utc).isoformat()
    root["status"] = "drafting"
    root["updated_at"] = parent["updated_at"]
    container["status"] = "drafting"
    container["schema_name"] = "semantic_blueprint_tree"
    container["semantic_version"] = 1
    container["updated_at"] = parent["updated_at"]
    if container is doc:
        doc["status"] = "drafting"
        doc["schema_name"] = "semantic_blueprint_tree"
        doc["semantic_version"] = 1
        doc["updated_at"] = parent["updated_at"]
    else:
        doc[_REPLACEMENT_DRAFT_KEY] = container
        doc["updated_at"] = parent["updated_at"]
    tree_version = write_blueprint(project_id, doc)
    refreshed_plan = await _refresh_pending_tree_plan_if_needed(project_id=project_id, doc=doc)
    return {
        "ok": True,
        "tree_version": tree_version,
        "draft_mode": _draft_mode(doc),
        "replacement": _draft_mode(doc) == "replacement",
        "parent_id": parent_id or "root",
        "node": _node_summary(normalized),
        "tree_summary": _tree_summary(root),
        "current_tree": _current_tree_context(root),
        "refreshed_pending_confirmation": refreshed_plan.get("plan") if isinstance(refreshed_plan, dict) else None,
        "status": "drafting",
        "finalized": False,
        "suggested_next": "continue_drafting_or_finalize",
        "model_feedback": _draft_model_feedback(),
        "message": f"已追加草稿节点：{normalized.get('title') or normalized.get('id')}",
    }


@register(
    "blueprint.update_tree_node",
    description=(
        "更新或移动 pending 草稿中的既有 text/image/video/audio 节点。"
        "用户点名修改待确认蓝图节点时直接调用；finalize 前可补 title/content/prompt/fields/references/depends_on，"
        "修层级时传 parent_id 移到新父节点。该工具不改 id/type、运行媒体或修改已确认蓝图。"
    ),
    tags=["blueprint", "write"],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "node_id": {"type": "string"},
            "patch": {"type": "object"},
        },
        "required": ["project_id", "node_id", "patch"],
    },
)
async def blueprint_update_tree_node(
    project_id: str = "",
    node_id: str = "",
    patch: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    if not node_id:
        return {"ok": False, "error": "node_id 是必填字段", "error_kind": "missing_id"}
    raw_patch = _parse_jsonish(patch)
    if not isinstance(raw_patch, dict):
        return {"ok": False, "error": "patch 必须是一个 JSON 对象", "error_kind": "missing_patch"}
    if "id" in raw_patch or "type" in raw_patch:
        return {
            "ok": False,
            "error": "草稿更新不能修改节点 id 或 type；需要新语义节点时请追加新节点。",
            "error_kind": "immutable_node_identity",
        }

    doc = read_blueprint(project_id)
    if _active_blueprint_exists(doc) and _replacement_draft(doc) is None:
        return {
            "ok": False,
            "error": "当前项目已有已确认蓝图，不能直接更新草稿节点。若用户要求整体重建错误树，请先用 blueprint.start_tree_draft(mode='replacement')；局部修改用 blueprint.revise。",
            "error_kind": "active_blueprint_exists",
        }
    container = _draft_container(doc)
    root = container.get("root") if isinstance(container.get("root"), dict) else {}
    target = find_node(root, node_id)
    if target is None:
        available_nodes = _available_node_refs(root)
        return {
            "ok": False,
            "error": f"节点 {node_id!r} 不存在。",
            "error_kind": "node_not_found",
            "node_id": node_id,
            "available_node_ids": [node.get("id") for node in available_nodes if node.get("id")],
            "available_nodes": available_nodes,
            "hint": "从 available_node_ids 里选择现有草稿节点再 update；如果这是新内容，用 blueprint.append_tree_node 追加。",
        }
    if node_id == "root":
        return {"ok": False, "error": "不能用此工具更新 root。", "error_kind": "root_update_not_allowed"}

    requested_parent_id = None
    if "parent_id" in raw_patch:
        requested_parent_id = str(raw_patch.get("parent_id") or "root").strip() or "root"
        new_parent = find_node(root, requested_parent_id)
        if new_parent is None:
            return {
                "ok": False,
                "error": f"新父节点 {requested_parent_id!r} 不存在。",
                "error_kind": "parent_not_found",
            }
        if requested_parent_id == node_id or find_node(target, requested_parent_id) is not None:
            return {
                "ok": False,
                "error": "不能把节点移动到自身或自己的子节点下。",
                "error_kind": "invalid_parent_cycle",
            }

    node_type = _normalize_node_type(target.get("type"))
    valid = {
        "parent_id",
        "title",
        "content",
        "description",
        "prompt",
        "negative_prompt",
        "resolution",
        "quality",
        "duration",
        "aspect_ratio",
        "production_path",
        "episode_index",
        "segment_index",
        "episode_number",
        "segment_id",
        "shot_id",
        "source_path",
        "fields",
        "references",
        "depends_on",
        *_PROMPT_EVIDENCE_FIELDS,
        "status",
        "materialize",
    }
    normalized_patch = dict(raw_patch)
    moved_field_keys: list[str] = []
    for key in list(normalized_patch.keys()):
        if key in valid:
            continue
        if key in _DRAFT_PATCH_FIELD_ALIASES_TO_FIELDS or not str(key).startswith("_"):
            fields = normalized_patch.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            fields = dict(fields)
            fields[key] = normalized_patch.pop(key)
            normalized_patch["fields"] = fields
            moved_field_keys.append(key)
    unknown = [key for key in normalized_patch if key not in valid]
    if unknown:
        return {
            "ok": False,
            "error": f"字段错误: {', '.join(unknown)} 不能用于语义草稿节点。",
            "error_kind": "unknown_fields",
            "allowed_fields": sorted(valid),
        }
    raw_patch = normalized_patch

    patch_without_parent = {key: value for key, value in raw_patch.items() if key != "parent_id"}
    next_node = dict(target)
    next_fields = dict(target.get("fields") if isinstance(target.get("fields"), dict) else {})
    patch_fields = patch_without_parent.get("fields")
    if isinstance(patch_fields, dict):
        next_fields.update(patch_fields)
    for key, value in patch_without_parent.items():
        if key == "fields":
            continue
        next_node[key] = value
        if key not in {"status", "materialize"} and value not in (None, "", [], {}):
            next_fields[key] = value
    next_node["fields"] = next_fields
    state = await _project_state_for_validation(project_id)
    _normalize_prompt_evidence_fields(next_node, state=state)

    if node_type == "video":
        ratio = str(next_node.get("aspect_ratio") or next_fields.get("aspect_ratio") or "").strip()
        if ratio and ratio not in _VIDEO_ASPECT_RATIOS:
            return {
                "ok": False,
                "error": f"视频画幅 {ratio!r} 不受支持；视频节点只能使用 16:9 或 9:16。",
                "error_kind": "unsupported_video_aspect_ratio",
                "supported_aspect_ratios": sorted(_VIDEO_ASPECT_RATIOS),
            }

    known_ids = _collect_node_ids(root.get("children") if isinstance(root.get("children"), list) else [])
    _normalize_node_links(next_node, known_ids)
    expected_aspect_ratio = await _pending_intake_aspect_ratio(project_id)
    conflict = _aspect_ratio_conflict(expected_aspect_ratio, [next_node])
    if conflict:
        return {
            "ok": False,
            "error": (
                f"节点与用户画幅要求冲突：用户选择 {expected_aspect_ratio}，"
                f"但节点内容包含 {conflict}。"
            ),
            "error_kind": "aspect_ratio_conflict",
            "expected_aspect_ratio": expected_aspect_ratio,
            "conflicting_value": conflict,
        }

    updated_at = datetime.now(timezone.utc).isoformat()
    update_node(target, next_node)
    parent_id_after_update = None
    if requested_parent_id is not None:
        current_parent, current_index = find_parent(root, node_id)
        if current_parent is None or current_index < 0:
            return {"ok": False, "error": f"节点 {node_id!r} 没有可移动的父节点。", "error_kind": "parent_not_found"}
        current_parent_id = str(current_parent.get("id") or "root")
        if current_parent_id != requested_parent_id:
            children = current_parent.get("children") if isinstance(current_parent.get("children"), list) else []
            moved = children.pop(current_index)
            new_parent = find_node(root, requested_parent_id)
            if new_parent is None:
                children.insert(current_index, moved)
                return {
                    "ok": False,
                    "error": f"新父节点 {requested_parent_id!r} 不存在。",
                    "error_kind": "parent_not_found",
                }
            new_parent.setdefault("children", []).append(moved)
            new_parent["updated_at"] = updated_at
            current_parent["updated_at"] = updated_at
        parent_id_after_update = requested_parent_id
    root["status"] = "drafting"
    root["updated_at"] = updated_at
    container["status"] = "drafting"
    container["schema_name"] = "semantic_blueprint_tree"
    container["semantic_version"] = 1
    container["updated_at"] = root["updated_at"]
    if container is doc:
        doc["status"] = "drafting"
        doc["schema_name"] = "semantic_blueprint_tree"
        doc["semantic_version"] = 1
        doc["updated_at"] = root["updated_at"]
    else:
        doc[_REPLACEMENT_DRAFT_KEY] = container
        doc["updated_at"] = root["updated_at"]
    tree_version = write_blueprint(project_id, doc)
    refreshed_plan = await _refresh_pending_tree_plan_if_needed(project_id=project_id, doc=doc)
    return {
        "ok": True,
        "tree_version": tree_version,
        "draft_mode": _draft_mode(doc),
        "replacement": _draft_mode(doc) == "replacement",
        "node_id": node_id,
        "patch": raw_patch,
        "parent_id": parent_id_after_update,
        "node": _node_summary(target),
        "tree_summary": _tree_summary(root),
        "current_tree": _current_tree_context(root),
        "refreshed_pending_confirmation": refreshed_plan.get("plan") if isinstance(refreshed_plan, dict) else None,
        "moved_patch_fields_to_fields": moved_field_keys,
        "status": "drafting",
        "finalized": False,
        "suggested_next": "continue_drafting_or_finalize",
        "model_feedback": _draft_model_feedback(),
        "message": f"已更新草稿节点：{target.get('title') or target.get('id')}",
    }


@register(
    "blueprint.finalize_tree_draft",
	    description=(
	        "把 pending 草稿提交为待确认蓝图方案。append/upsert 完成后调用；"
	        "视频蓝图提交前先做 agent.review，后端会整理结构、引用和 source path。该工具不运行或批准。"
	    ),
    tags=["blueprint", "write"],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "source_request": {"type": "string"},
        },
        "required": ["project_id"],
    },
)
async def blueprint_finalize_tree_draft(
    project_id: str = "",
    title: str = "",
    summary: str = "",
    source_request: str = "",
) -> dict[str, Any]:
    doc = read_blueprint(project_id)
    if _active_blueprint_exists(doc) and _replacement_draft(doc) is None:
        return {
            "ok": False,
            "error": "当前项目已有已确认蓝图，没有可提交的替换草稿。若用户要求整体重建错误树，请先用 blueprint.start_tree_draft(mode='replacement')。",
            "error_kind": "active_blueprint_exists",
        }
    container = _draft_container(doc)
    return await _submit_tree_plan(
        project_id=project_id,
        doc=doc,
        title=title or str(container.get("title") or doc.get("title") or ""),
        summary=summary or str(container.get("summary") or doc.get("summary") or ""),
        source_request=source_request or str(container.get("source_request") or doc.get("source_request") or ""),
        require_runtime_evidence=True,
    )


@register(
    "blueprint.propose_tree",
    description=(
        "提交一棵完整语义蓝图树并创建待确认蓝图方案。传 title、summary 和 tree 或 nodes；"
        "可物化节点 type 只能是 text/image/video/audio，视觉资产写 image 节点，分集分段写 text，视频节点引用上游节点，纯音频写 audio 节点。"
        "该隐藏/测试路径不批准方案、创建节点、运行媒体、覆盖 active blueprint 或替模型选择制作路径。"
    ),
    tags=["blueprint", "write"],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string", "description": "蓝图标题，用户可见"},
            "summary": {"type": "string", "description": "蓝图摘要，用户可见"},
            "tree": {"type": "object", "description": "完整树，可包含 root.children 或 children"},
            "nodes": {"type": "array", "description": "根节点 children 的简写；每个物化节点 type 只能是 text/image/video/audio"},
            "source_request": {"type": "string", "description": "用户原始请求摘要"},
        },
        "required": ["project_id", "title"],
    },
)
async def blueprint_propose_tree(
    project_id: str = "",
    title: str = "",
    summary: str = "",
    tree: dict[str, Any] | str | None = None,
    nodes: list[dict[str, Any]] | str | None = None,
    source_request: str = "",
) -> dict[str, Any]:
    children, metadata = _coerce_tree_children(tree, nodes)
    if not children:
        return {
            "ok": False,
            "error": "必须提供 tree.children、tree.root.children 或 nodes。",
            "error_kind": "empty_tree",
            "hint": "提交完整语义树；公开节点 type 只允许 text / image / video / audio。结构分组、制作方法和执行参数写在 title/content/description/fields/references 中。",
        }

    errors: list[str] = []
    seen_ids: set[str] = {"root"}
    normalized_children: list[dict[str, Any]] = []
    for index, child in enumerate(children, 1):
        normalized = _normalize_semantic_node(
            child,
            index_path=f"root.{index}",
            seen_ids=seen_ids,
            errors=errors,
        )
        if normalized is not None:
            normalized_children.append(normalized)
    if errors:
        return {
            "ok": False,
            "error": "蓝图树校验失败：" + "；".join(errors[:8]),
            "error_kind": "semantic_tree_validation_failed",
            "errors": errors[:20],
        }
    temp_root = {"id": "root", "children": normalized_children}
    _normalize_root_child_order(temp_root)
    _auto_repair_default_segment_container(temp_root)
    _auto_repair_flat_segment_media(temp_root)
    _auto_repair_video_dependencies(temp_root)
    _auto_repair_video_production_paths(temp_root)
    normalized_children = temp_root.get("children") if isinstance(temp_root.get("children"), list) else normalized_children
    _normalize_all_links(normalized_children)

    expected_aspect_ratio = await _pending_intake_aspect_ratio(project_id)
    conflict = _aspect_ratio_conflict(expected_aspect_ratio, normalized_children)
    if conflict:
        return {
            "ok": False,
            "error": (
                f"蓝图树与用户画幅要求冲突：用户选择 {expected_aspect_ratio}，"
                f"但树内容包含 {conflict}。请按用户画幅重提完整语义树。"
            ),
            "error_kind": "aspect_ratio_conflict",
            "expected_aspect_ratio": expected_aspect_ratio,
            "conflicting_value": conflict,
        }
    quality_error = _semantic_quality_error(normalized_children)
    if quality_error:
        return quality_error

    doc = read_blueprint(project_id)
    if str(doc.get("status") or "") in {"active", "materialized"}:
        return {
            "ok": False,
            "error": "当前项目已有已确认蓝图，不能用 propose_tree 整棵覆盖。请用 blueprint.revise 做局部修订，或新建项目/确认重置后再提交新蓝图。",
            "error_kind": "active_blueprint_exists",
        }

    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    root["children"] = normalized_children
    _normalize_root_child_order(root)
    normalized_children = root.get("children") if isinstance(root.get("children"), list) else normalized_children
    root.update({
        "id": "root",
        "type": "story",
        "title": title or metadata.get("title") or doc.get("title") or "视频蓝图",
        "content": summary or metadata.get("summary") or "",
        "status": "pending_review",
        "materialize": False,
        "children": normalized_children,
        "updated_at": now,
    })
    doc["root"] = root
    doc["title"] = title or metadata.get("title") or doc.get("title") or "视频蓝图"
    doc["summary"] = summary or metadata.get("summary") or ""
    doc.pop("video_mode", None)
    doc.pop("video_generation_type", None)
    doc.pop("image_to_video_method", None)
    doc["status"] = "pending_review"
    doc["schema_name"] = "semantic_blueprint_tree"
    doc["semantic_version"] = 1
    doc["source_request"] = source_request
    doc["updated_at"] = now
    tree_version = write_blueprint(project_id, doc)

    summary_payload = _tree_summary(root)
    plan = await submit_blueprint_confirmation(
        project_id=project_id,
        plan_doc={
            "kind": "blueprint_tree",
            "title": doc["title"],
            "summary": doc["summary"] or f"语义蓝图树已生成，共 {summary_payload['node_count']} 个节点。",
            "source_request": source_request,
            "tree_version": tree_version,
            "tree_summary": summary_payload,
            "sections": [
                {
                    "type": "markdown",
                    "content": doc["summary"] or f"语义蓝图树已生成，共 {summary_payload['node_count']} 个节点。",
                }
            ],
        },
    )
    if plan.get("error") or plan.get("ok") is False:
        return {
            "ok": False,
            "error": plan.get("error") or "提交蓝图树确认失败",
            "error_kind": plan.get("error_kind") or "blueprint_confirmation_failed",
            "tree_version": tree_version,
            "tree_summary": summary_payload,
        }

    return {
        "ok": True,
        "tree_version": tree_version,
        "tree_summary": summary_payload,
        "plan": plan.get("plan"),
        "preview_checklist": plan.get("preview_checklist") or [],
        "message": "语义蓝图树已提交，等待用户确认。确认后才会物化节点。",
    }


def _validate_node(node: dict[str, Any]) -> dict[str, Any] | None:
    """Return error dict if node is invalid, None if valid."""
    ntype = node.get("type", "")
    if ntype == "text":
        valid = _VALID_TEXT_FIELDS
        required = ["title", "content"]
    elif ntype == "image":
        valid = _VALID_IMAGE_FIELDS
        required = ["title", "description", "resolution", "quality"]
    elif ntype == "video":
        valid = _VALID_VIDEO_FIELDS
        required = ["title", "description", "duration", "resolution"]
    elif ntype == "audio":
        valid = _VALID_AUDIO_FIELDS
        required = ["title"]
    else:
        return {"ok": False, "error": "node.type 必须是 text / image / video / audio", "error_kind": "invalid_type"}

    unknown = [k for k in node if k not in valid and k not in ("children", "status", "created_at", "updated_at")]
    if unknown:
        hint = f"允许: {', '.join(sorted(valid))}"
        return {"ok": False, "error": f"字段错误: {', '.join(unknown)} 不能用于 {ntype} 节点。{hint}", "error_kind": "unknown_fields"}
    missing = [k for k in required if not node.get(k)]
    if missing:
        return {"ok": False, "error": f"缺少必填: {', '.join(missing)}", "error_kind": "missing_required_fields"}
    return None


@register(
    "blueprint.add_child",
    description=(
        "往蓝图树中指定父节点下追加一个子节点。"
        "增量草稿流程使用 blueprint.append_tree_node；此工具仅保留给内部/兼容路径。"
        "image/video/audio 节点的可执行内容使用 prompt，text 节点传 title 和 content。"
    ),
    tags=["blueprint", "write"],
)
async def blueprint_add_child(
    parent_id: str = "root",
    node: dict[str, Any] | None = None,
    project_id: str = "",
) -> dict[str, Any]:
    if not node or not isinstance(node, dict):
        return {"ok": False, "error": "node 必须是一个 JSON 对象", "error_kind": "missing_node"}
    if not node.get("id"):
        return {"ok": False, "error": "node.id 是必填字段", "error_kind": "missing_id"}
    if not node.get("type") or node["type"] not in ("text", "image", "video", "audio"):
        return {"ok": False, "error": "node.type 必须是 text / image / video / audio", "error_kind": "invalid_type"}
    err = _validate_node(node)
    if err:
        return err

    doc = read_blueprint(project_id)
    root = doc["root"]

    # Only root-level children must be unique — they are the blueprint entry points.
    node_id = node.get("id", "")
    if parent_id == "root" and any(c.get("id") == node_id for c in (root.get("children") or [])):
        existing = [c.get("id") for c in (root.get("children") or [])]
        return {"ok": False, "error": f"根节点下已存在 '{node_id}'，不能重复创建入口。现有入口: {', '.join(existing)}。用 blueprint.update_node 修改或用 blueprint.delete_node 先删除。", "error_kind": "duplicate_entry"}

    parent = find_node(root, parent_id)
    if parent is None:
        return {"ok": False, "error": f"父节点 '{parent_id}' 不存在，用 blueprint.list_children 查看可用的父节点", "error_kind": "parent_not_found"}

    added = add_child(parent, dict(node))
    tree_version = write_blueprint(project_id, doc)
    logger.info("blueprint.add_child: %s → %s", parent_id, added.get("id"))
    return {
        "ok": True,
        "parent_id": parent_id,
        "node": _node_summary(added),
        "tree_version": tree_version,
    }


@register(
    "blueprint.update_node",
    description="更新蓝图树中指定节点的字段。传什么字段改什么，其他不动。",
    tags=["blueprint", "write"],
)
async def blueprint_update_node(
    node_id: str = "",
    patch: dict[str, Any] | None = None,
    project_id: str = "",
) -> dict[str, Any]:
    if not node_id:
        return {"ok": False, "error": "node_id 是必填字段", "error_kind": "missing_id"}
    if not patch or not isinstance(patch, dict):
        return {"ok": False, "error": "patch 必须是一个 JSON 对象", "error_kind": "missing_patch"}

    doc = read_blueprint(project_id)
    root = doc["root"]
    target = find_node(root, node_id)
    if target is None:
        return {"ok": False, "error": f"节点 {node_id} 不存在", "error_kind": "node_not_found"}

    # Validate patch fields against node type
    ntype = target.get("type", "")
    if ntype == "text":
        valid = _VALID_TEXT_FIELDS
    elif ntype == "image":
        valid = _VALID_IMAGE_FIELDS | {"prompt", "negative_prompt", "url"}  # writable in generation
    elif ntype == "video":
        valid = _VALID_VIDEO_FIELDS | {"prompt", "url"}
    elif ntype == "audio":
        valid = _VALID_AUDIO_FIELDS | {"url", "local_url", "remote_url"}
    else:
        valid = {
            "id",
            "type",
            "title",
            "content",
            "description",
            "fields",
            "references",
            "status",
            "materialize",
            "children",
            "created_at",
            "updated_at",
        }
    unknown = [k for k in patch if k not in valid and k not in ("status",)]
    if unknown:
        hint = f"允许: {', '.join(sorted(valid))}"
        return {"ok": False, "error": f"字段错误: {', '.join(unknown)} 不能用于 {ntype} 节点。{hint}", "error_kind": "unknown_fields"}

    update_node(target, patch)
    tree_version = write_blueprint(project_id, doc)
    return {"ok": True, "node_id": node_id, "patch": patch, "tree_version": tree_version}


@register(
    "blueprint.delete_node",
    description="删除蓝图树中的指定节点及其所有子节点。",
    tags=["blueprint", "write"],
)
async def blueprint_delete_node(
    node_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    if not node_id:
        return {"ok": False, "error": "node_id 是必填字段", "error_kind": "missing_id"}
    doc = read_blueprint(project_id)
    root = doc["root"]

    if node_id == "root":
        # Clear all children but keep the root node itself
        deleted = len(root.get("children", []))
        root["children"] = []
        tree_version = write_blueprint(project_id, doc)
        return {
            "ok": True,
            "deleted": True,
            "node_id": "root",
            "children_cleared": deleted,
            "tree_version": tree_version,
        }

    if not delete_node(root, node_id):
        return {"ok": False, "error": f"节点 {node_id} 不存在", "error_kind": "node_not_found"}

    tree_version = write_blueprint(project_id, doc)
    return {"ok": True, "deleted": True, "node_id": node_id, "tree_version": tree_version}


@register(
    "blueprint.get",
    description=(
        "读取蓝图树或单个蓝图节点。已知节点 id 时传 node_id；"
        "不传则返回整棵树。该工具不修改、批准、物化或运行节点。"
    ),
    tags=["blueprint", "read"],
)
async def blueprint_get(
    node_id: str = "",
    project_id: str = "",
    # backward compat — old frontend passes these, ignored in tree mode
    include_outline: bool = False,
    include_view_model: bool = False,
) -> dict[str, Any]:
    if not blueprint_exists(project_id):
        result: dict[str, Any] = {
            "ok": True,
            "tree": None,
            "blueprint": None,
            "status": "none",
            "tree_version": None,
            "section_results": [],
        }
        if node_id:
            result.update({
                "ok": False,
                "error": f"节点 {node_id} 不存在：当前项目没有蓝图树。",
                "error_kind": "blueprint_missing",
            })
        if include_view_model:
            result["view_model"] = None
        if include_outline:
            result["outline_markdown"] = None
        return result
    doc = read_blueprint(project_id)
    if not blueprint_has_content(doc):
        result = {
            "ok": True,
            "tree": None,
            "blueprint": None,
            "status": "none",
            "tree_version": None,
            "section_results": [],
        }
        if node_id:
            result.update({
                "ok": False,
                "error": f"节点 {node_id} 不存在：当前项目没有蓝图树。",
                "error_kind": "blueprint_missing",
            })
        if include_view_model:
            result["view_model"] = None
        if include_outline:
            result["outline_markdown"] = None
        return result
    if node_id:
        root = doc["root"]
        target = find_node(root, node_id)
        resolved_node_id = ""
        if target is None:
            resolved_node_id = await _blueprint_node_id_for_workflow_node(project_id, node_id)
            if resolved_node_id and resolved_node_id != node_id:
                target = find_node(root, resolved_node_id)
        if target is None:
            return {"ok": False, "error": f"节点 {node_id} 不存在", "error_kind": "node_not_found"}
        result = {"ok": True, "node": target}
        if resolved_node_id:
            result["requested_node_id"] = node_id
            result["node_id"] = resolved_node_id
            result["resolved_from"] = "workflow_node"
        return result
    status = str(doc.get("status") or "drafting")
    result: dict[str, Any] = {"ok": True, "tree": doc, "blueprint": {"status": status, "theme_title": doc.get("title")}, "status": status}
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    children = root.get("children") if isinstance(root.get("children"), list) else []
    if status == "drafting" and children:
        result.update({
            "finalized": False,
            "suggested_next": "continue_drafting_or_finalize",
            "model_feedback": _draft_model_feedback(),
        })
    # compat — old frontend expects these
    if include_view_model:
        result["view_model"] = None
    if include_outline:
        result["outline_markdown"] = None
    result["tree_version"] = doc.get("tree_version")
    result["section_results"] = []
    return result


@register(
    "blueprint.list_children",
    description="列出指定节点的直接子节点摘要，不递归。",
    tags=["blueprint", "read"],
)
async def blueprint_list_children(
    parent_id: str = "root",
    project_id: str = "",
) -> dict[str, Any]:
    if not parent_id:
        parent_id = "root"
    doc = read_blueprint(project_id)
    root = doc["root"]
    parent = find_node(root, parent_id)
    if parent is None:
        return {"ok": False, "error": f"节点 '{parent_id}' 不存在，用 blueprint.get 查看完整树结构", "error_kind": "node_not_found"}
    return {"ok": True, "parent_id": parent_id, "children": list_children(parent)}


# ── generation-phase tool ─────────────────────────────────────────────────────


@register(
    "blueprint.set_prompt",
    description=(
        "生成阶段专用：给 image/video 节点写入最终提示词。写入后自动触发渲染。"
        "只在节点 status 为 pending 或 failed 时可调用。"
        "prompt 应根据节点的 description + references 中的已渲染图片 + 剧情上下文来写。"
    ),
    tags=["blueprint", "write"],
)
async def blueprint_set_prompt(
    node_id: str = "",
    prompt: str = "",
    negative_prompt: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    if not node_id:
        return {"ok": False, "error": "node_id 是必填字段", "error_kind": "missing_id"}
    if not prompt.strip():
        return {"ok": False, "error": "prompt 不能为空", "error_kind": "empty_prompt"}

    doc = read_blueprint(project_id)
    root = doc["root"]
    target = find_node(root, node_id)
    if target is None:
        return {"ok": False, "error": f"节点 {node_id} 不存在", "error_kind": "node_not_found"}
    if target.get("type") not in ("image", "video"):
        return {"ok": False, "error": "只能给 image/video 节点设置 prompt", "error_kind": "wrong_type"}
    if target.get("status") not in ("pending", "failed"):
        return {"ok": False, "error": "节点状态不是 pending/failed，不能设置 prompt", "error_kind": "invalid_status"}

    target["prompt"] = prompt.strip()
    if negative_prompt.strip():
        target["negative_prompt"] = negative_prompt.strip()
    target["status"] = "rendering"
    from datetime import datetime, timezone
    target["updated_at"] = datetime.now(timezone.utc).isoformat()
    tree_version = write_blueprint(project_id, doc)

    # Find matching canvas node (created by materialize_blueprint) and trigger render
    try:
        from app.db.session import AsyncSessionLocal
        from app.db.models import WorkflowNode
        from sqlmodel import select, or_
        import json as _json
        async with AsyncSessionLocal() as session:
            stmt = select(WorkflowNode).where(
                WorkflowNode.project_id == project_id,
                WorkflowNode.input_json.contains(node_id),
            )
            result = await session.execute(stmt)
            found = False
            for canvas_node in result.scalars().all():
                try: inp = _json.loads(canvas_node.input_json) if isinstance(canvas_node.input_json, str) else canvas_node.input_json
                except: continue
                if isinstance(inp, dict) and inp.get("blueprint_node_id") == node_id:
                    canvas_node.prompt = prompt.strip()
                    if negative_prompt.strip():
                        setattr(canvas_node, 'negative_prompt', negative_prompt.strip())
                    canvas_node.status = "running"
                    session.add(canvas_node)
                    await session.commit()
                    from app.mcp_tools.node_universal import node_run
                    await node_run(project_id=project_id, node_id=str(canvas_node.id), action="render")
                    found = True
                    break
            if not found:
                logger.warning("blueprint.set_prompt: %s — no canvas node found (not materialized yet?)", node_id)
                return {"ok": False, "error": "未找到对应画布节点，请先确认蓝图(blueprint.approve)触发物化", "error_kind": "canvas_node_not_found"}
        logger.info("blueprint.set_prompt: %s — triggered render", node_id)
    except Exception as exc:
        logger.exception("blueprint.set_prompt: %s — render trigger failed", node_id)
        return {"ok": False, "error": f"触发渲染失败: {exc}", "error_kind": "render_trigger_failed"}

    return {
        "ok": True,
        "node_id": node_id,
        "prompt": prompt.strip(),
        "negative_prompt": negative_prompt.strip(),
        "status": "rendering",
        "tree_version": tree_version,
        "patch": {
            "prompt": prompt.strip(),
            "status": "rendering",
            **({"negative_prompt": negative_prompt.strip()} if negative_prompt.strip() else {}),
        },
    }


# ── helpers ───────────────────────────────────────────────────────────────────
