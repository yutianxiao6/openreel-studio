"""统一的节点 API — 模型只需 text/image/video/audio + 字段,后端只执行通用媒介能力。

Agent 只看到节点原语(node.create / get / update / delete / list / run),
内部委托给 canvas_tools 和 service-level media services 实现。

公开节点 type 只允许:
  text / image / video / audio
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

from app.agent.blueprint_revision import create_pending_revision_from_node_patch
from app.agent.prompt_dump import dump_llm_request, new_run_id
from app.agent.workflow_structured_output import (
    WorkflowStructuredOutputError,
    parse_structured_output,
    structured_output_contract,
    structured_output_instructions,
)
from app.config import settings
from app.db.models import Asset, WorkflowNode
from app.db.session import session_scope
from app.mcp_tools import canvas_tools
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
from app.services import media_generation, media_history
from app.services.llm_service import LLMService
from app.services.node_public_ids import (
    internal_to_public_id_map,
    looks_like_public_node_id,
    model_visible_node_payload,
    public_node_id_from_dict,
    publicize_node_refs,
    resolve_internal_node_id,
    strip_node_id_marker,
)
from sqlmodel import select

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


NODE_RUN_TIMEOUT_SECONDS = _env_int("DRAMA_NODE_RUN_TIMEOUT_SECONDS", 600, minimum=30)
IMAGE_RENDER_TIMEOUT_SECONDS = _env_int("DRAMA_IMAGE_RENDER_TIMEOUT_SECONDS", 300, minimum=60)
TEXT_REFERENCE_IMAGE_MAX_BYTES = _env_int("DRAMA_TEXT_REFERENCE_IMAGE_MAX_BYTES", 8 * 1024 * 1024, minimum=1024)
STALE_RUNNING_SECONDS = max(
    NODE_RUN_TIMEOUT_SECONDS,
    IMAGE_RENDER_TIMEOUT_SECONDS,
) + 60
NODE_LIST_DEFAULT_LIMIT = 20
NODE_LIST_MAX_LIMIT = 800
WORKFLOW_LLM_MAX_TEXT_CHARS = _env_int(
    "DRAMA_WORKFLOW_LLM_MAX_TEXT_CHARS",
    50_000,
    minimum=4_000,
)
WORKFLOW_LLM_MAX_IMAGE_COUNT = _env_int(
    "DRAMA_WORKFLOW_LLM_MAX_IMAGE_COUNT",
    8,
    minimum=1,
)

NODE_SURFACE_PROJECT_PANEL = "project_panel"
NODE_SURFACE_DRAFT_CANVAS = "draft_canvas"
NODE_SURFACE_WORKFLOW_RUNTIME = "workflow_runtime"
_VALID_NODE_SURFACES = {NODE_SURFACE_PROJECT_PANEL, NODE_SURFACE_DRAFT_CANVAS, NODE_SURFACE_WORKFLOW_RUNTIME}


async def _node_public_id_map(project_id: str) -> dict[str, str]:
    if not project_id:
        return {}
    async with session_scope() as session:
        return await internal_to_public_id_map(session, project_id)


async def _resolve_agent_node_id(project_id: str, node_id: Any) -> str:
    if not project_id:
        raw = strip_node_id_marker(node_id)
        if looks_like_public_node_id(raw):
            return ""
        return raw
    async with session_scope() as session:
        return await resolve_internal_node_id(session, project_id, node_id)


async def _model_visible_node(node: dict[str, Any], project_id: str = "") -> dict[str, Any]:
    id_map = await _node_public_id_map(project_id or str(node.get("project_id") or ""))
    payload = model_visible_node_payload(node, id_map)
    internal_id = str(node.get("id") or "")
    if internal_id:
        payload["_canvas_id"] = internal_id
        payload["_canvas_node_id"] = internal_id
    if node.get("display_id") is not None:
        payload["_canvas_display_id"] = node.get("display_id")
    return payload


async def _model_visible_result(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    id_map = await _node_public_id_map(project_id)
    return publicize_node_refs(payload, id_map)


_NODE_DEPENDENCIES: dict[str, list[str]] = {
    "text": [],
    "image": [],
    "video": [],
    "audio": [],
}

_SUBJECT_BY_TYPE: dict[str, tuple[str, str]] = {
    "image": ("image", "图片"),
}


_CREATIVE_NODE_TYPES: set[str] = {
    "text",
    "image",
    "video",
    "audio",
}

_MODE_ALLOWED_TYPES: dict[str, set[str]] = {
    "single_node": set(_CREATIVE_NODE_TYPES),
    "video_production": set(_CREATIVE_NODE_TYPES),
    "skill_freeform": set(_CREATIVE_NODE_TYPES),
}


def _surface_for_project_mode(mode: str | None) -> str:
    """Map new creative nodes to the unified canvas surface."""
    return NODE_SURFACE_DRAFT_CANVAS


def _node_surface_from_model_config(model_config: Any) -> str:
    if isinstance(model_config, str):
        try:
            model_config = json.loads(model_config)
        except (json.JSONDecodeError, TypeError):
            model_config = None
    if isinstance(model_config, dict):
        surface = model_config.get("surface") or model_config.get("_surface")
        if surface in _VALID_NODE_SURFACES:
            return surface
    return NODE_SURFACE_DRAFT_CANVAS


def _node_surface(node: dict[str, Any]) -> str:
    surface = node.get("surface")
    if surface in _VALID_NODE_SURFACES:
        return surface
    return _node_surface_from_model_config(node.get("model_config"))


def _has_video_project_context(state: dict) -> bool:
    # A remembered mode selection by itself is not a formal production chain.
    # Formal video context begins only once there is a blueprint or pending
    # blueprint state. This lets standalone visual
    # asset requests keep using draft_canvas without relying on language
    # shortcuts.
    return bool(
        state.get("project_blueprint")
        or state.get("pending_blueprint_draft")
        or state.get("pending_blueprint_review")
        or state.get("pending_blueprint_section_review")
        or state.get("pending_blueprint_confirmation")
    )


def _preferred_mode_for_node_type(
    state: dict,
    target_type: str,
    fields: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    if _has_video_project_context(state):
        return "video_production", None
    if target_type in _MODE_ALLOWED_TYPES["single_node"]:
        return "single_node", None
    return "video_production", None


async def _ensure_project_mode_for_type(
    project_id: str,
    state: dict,
    target_type: str,
    fields: dict[str, Any] | None = None,
) -> tuple[dict, bool]:
    """Infer and persist node surface mode when the agent uses node primitives."""
    mode = state.get("project_mode")
    preferred_mode, preferred_sub_mode = _preferred_mode_for_node_type(state, target_type, fields)
    patch: dict[str, Any] = {}

    if mode not in _MODE_ALLOWED_TYPES:
        patch["project_mode"] = preferred_mode
        patch["project_sub_mode"] = preferred_sub_mode
    elif target_type not in _MODE_ALLOWED_TYPES.get(mode, set()):
        patch["project_mode"] = preferred_mode
        patch["project_sub_mode"] = preferred_sub_mode
    elif (
        mode == "video_production"
        and preferred_mode == "single_node"
        and target_type in _MODE_ALLOWED_TYPES["single_node"]
    ):
        patch["project_mode"] = "single_node"
        patch["project_sub_mode"] = None

    if patch:
        await _write_project_state_patch(project_id, patch)
        state = dict(state)
        state.update(patch)
        return state, True
    return state, False

# 节点字段 schema(给 LLM 看怎么填 fields)
_NODE_FIELD_SCHEMA: dict[str, dict] = {
    "text": {
        "required": [],
        "optional": ["title", "content", "description", "references", "depends_on"],
        "description": "通用文本节点。用于 brief、故事、设定、镜头清单、制作说明等模型自定义结构；正文需要模型写入 fields.content，node.run 只保存已有正文。",
    },
    "image": {
        "required": ["prompt", "aspect_ratio", "resolution"],
        "optional": [
            "title", "description", "quality",
            "reference_images", "references", "depends_on", "seed",
            "purpose", "prompt_source",
        ],
        "description": "通用图片节点。模型必须自己写最终图片 prompt、aspect_ratio 和精确像素 resolution；后端只按 prompt/fields/references 调图片服务，不判断它是人物、场景、分镜、首尾帧或故事模板。",
    },
    "video": {
        "required": ["prompt"],
        "optional": [
            "title", "description", "duration_seconds", "aspect_ratio", "resolution",
            "reference_images", "reference_videos", "reference_audios", "media_references",
            "references", "depends_on", "video_mode", "mode",
            "first_frame_asset_id", "last_frame_asset_id",
            "generate_audio", "watermark", "return_last_frame", "seed",
            "priority", "execution_expires_after", "safety_identifier", "tools",
            "production_path", "prompt_status", "prompt_source",
        ],
        "description": "通用视频节点。模型必须自己写最终视频 prompt，并把已确认时长、比例、制作路径、参考图依赖写入 fields；后端只按 prompt/fields/references 调视频服务，不合成视频提示词。",
    },
    "audio": {
        "required": ["prompt"],
        "optional": [
            "title", "description", "style", "instrumental", "format", "duration_seconds",
            "voice", "speed", "instructions",
            "negative_tags", "custom_mode", "callback_url",
            "references", "depends_on",
        ],
        "description": "通用纯音频节点。模型必须自己写最终音频 prompt；TTS 语音可写 voice/speed/instructions，音乐可写 style/instrumental；后端只按 prompt/fields 调已配置的 audio provider。",
    },
}


def _prompt_guidance_for_type(node_type: str) -> dict[str, Any] | None:
    if node_type == "image":
        return {
            "required_before_prompt": [
                "先理解图片用途:人物/场景/宫格分镜/单张分镜/首尾帧/故事模板。",
                "按当前 skill 的要求自己写最终图片 prompt；用户自定义写法只放进 skill。",
                "创建 image 节点必须写 fields.aspect_ratio 和精确像素 fields.resolution；不要写 1k/2k/4k 这种档位。16:9 常用 1920x1080；9:16 常用 1080x1920。",
            ],
            "record_fields": [
                "fields.prompt_source",
            ],
            "fallback": (
                "没有用户自定义 skill 时按默认视频制作 skill 写 prompt，并记录 "
                "fields.prompt_source='skill_or_model_written'。"
            ),
        }
    if node_type == "video":
        return {
            "required_before_prompt": [
                "先确认视频路径:T2V/I2V/宫格分镜/单张分镜/首尾帧/故事模板/参考图/修复。",
                "有已生成图片 references/depends_on 时，先看图或读取视觉分析，再写最终视频 prompt；看不了图时明确说明看不了，不要假装看过。",
                "按当前 skill 的视频提示词要求自己写最终 video prompt；用户自定义写法只放进 skill。",
            ],
            "record_fields": [
                "fields.prompt_source",
            ],
            "fallback": (
                "没有用户自定义 skill 时按默认视频制作 skill 写 prompt，并记录 "
                "fields.prompt_source='skill_or_model_written'。"
            ),
        }
    return None


def _node_dependencies_for_context(
    target_type: str,
    state: dict | None = None,
    fields: dict[str, Any] | None = None,
) -> list[str]:
    return list(_NODE_DEPENDENCIES.get(target_type, []))


async def _check_node_deps(
    project_id: str,
    target_type: str,
    state: dict | None = None,
    fields: dict[str, Any] | None = None,
) -> tuple[bool, str, list[str]]:
    """硬性顺序依赖检查。返回 (ok, error_message, missing_list)。

    LLM 看到 dependency_missing 错误后应当去补依赖,不要换工具绕过。
    """
    needs = _node_dependencies_for_context(target_type, state, fields)
    if not needs:
        return True, "", []
    nodes = await canvas_tools.list_nodes(project_id) or []
    have_completed: set[str] = set()
    for n in nodes:
        if isinstance(n, dict) and n.get("status") == "completed" and isinstance(n.get("type"), str):
            have_completed.add(n["type"])

    missing = [t for t in needs if t not in have_completed]
    if missing:
        return False, (
            f"创建 {target_type} 缺少这些 completed 节点 {missing}。"
            f"当前 completed 类型:{sorted(have_completed) or '空'}。"
            f"请按铁律先做完缺失项的 create + run,不要换工具绕过。"
        ), missing
    return True, "", []


async def _merge_stage_into_fusion(
    node_id: str,
    node_type: str,
    *,
    status: str,
    url: str | None = None,
    local_url: str | None = None,
    remote_url: str | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    quality: str | None = None,
    prompt: str | None = None,
    input_data: dict[str, Any] | None = None,
    error: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict:
    """读现有 output_json 的 fusion stages,upsert 图片那一阶段后写回。

    保留之前已有的人物设定/提示词等阶段,不再整段覆盖。
    返回完整 fusion 结构供前端推送。
    """
    subj, stage_name = _SUBJECT_BY_TYPE.get(node_type, (node_type, "图片"))
    node = await canvas_tools.get_node(node_id)
    if isinstance(node, dict) and node.get("error"):
        # 节点已不存在,构造空 fusion 返回防止上层崩
        return {"type": "fusion", "subject": subj, "stages": []}
    existing = node.get("output") if isinstance(node, dict) else None
    raw_existing = existing
    project_id = str(node.get("project_id") or "") if isinstance(node, dict) else ""
    if isinstance(existing, str):
        try:
            existing = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            existing = None
            raw_existing = raw_existing.strip() if isinstance(raw_existing, str) else raw_existing

    def _media_from_output(value: Any) -> dict[str, str]:
        if isinstance(value, list):
            for item in value:
                parsed = _media_from_output(item)
                if parsed:
                    return parsed
            return {}
        if not isinstance(value, dict):
            if isinstance(value, str):
                text = value.strip()
                if text and (text.startswith(("http://", "https://")) or text.startswith("/")):
                    return {"url": text}
                if text.startswith("{") or text.startswith("["):
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    else:
                        return _media_from_output(parsed)
            return {}
        direct = {
            "local_url": value.get("local_url"),
            "url": value.get("url"),
            "remote_url": value.get("remote_url"),
            "composite_url": value.get("composite_url"),
            "thumbnail_url": value.get("thumbnail_url"),
            "poster": value.get("poster"),
            "last_frame_url": value.get("last_frame_url"),
        }
        if (
            not any(
                isinstance(value_, str) and value_.strip()
                for value_ in (direct["local_url"], direct["url"], direct["remote_url"])
            )
            and isinstance(value, dict)
        ):
            for fallback_key in ("composite_url", "thumbnail_url", "poster", "last_frame_url"):
                fallback_value = direct.get(fallback_key)
                if isinstance(fallback_value, str) and fallback_value.strip():
                    direct["url"] = fallback_value.strip()
                    break
        if not any(isinstance(url, str) and url for url in direct.values()):
            for key in ("output", "result", "images", "data", "history", "media_history"):
                if key not in value:
                    continue
                candidate = _media_from_output(value.get(key))
                if candidate:
                    direct.update(candidate)
                    break

        if not any(isinstance(url, str) and url for url in direct.values()):
            path_candidates = [
                value.get("path"),
                value.get("local_path"),
            ]
            source_path = None
            for path_candidate in path_candidates:
                if isinstance(path_candidate, str) and path_candidate.strip():
                    source_path = path_candidate.strip()
                    break
            if source_path:
                path = Path(source_path).expanduser()
                if project_id and not path.is_absolute():
                    path = _storage_root() / project_id / path
                if path.exists() and path.is_file():
                    local_url = _local_url_for_storage_path(project_id, path)
                    if local_url:
                        direct["local_url"] = local_url
                        direct["url"] = local_url
                        direct["local_path"] = str(path)
                    else:
                        direct["local_path"] = str(path)
                        direct["url"] = str(path)
        if not any(isinstance(url, str) and url for url in direct.values()):
            images = value.get("images")
            if isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, dict):
                    direct.update({
                        "local_url": first.get("local_url"),
                        "url": first.get("url"),
                        "remote_url": first.get("remote_url"),
                    })
        meta: dict[str, str] = {}
        for key in ("size", "size_requested", "size_final", "aspect_ratio", "quality"):
            val = value.get(key)
            if isinstance(val, str) and val.strip():
                meta[key] = val.strip()
        for key in ("local_url", "url", "remote_url", "local_path", "path"):
            val = direct.get(key)
            if isinstance(val, str) and val.strip():
                meta[key] = val.strip()
        for key in ("composite_url", "thumbnail_url", "poster", "last_frame_url"):
            val = direct.get(key)
            if isinstance(val, str) and val.strip():
                meta[key] = val.strip()
        for key in ("local_path", "path"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip() and "local_path" not in meta:
                meta[key] = raw.strip()
        return meta

    def _is_image_stage_name(name: Any) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        if re.search(r"提示词|prompt", text, flags=re.IGNORECASE):
            return False
        return bool(re.search(r"图|首帧|尾帧|模板|参考|image|storyboard|output|img", text, flags=re.IGNORECASE))

    def _stage_has_image_url(stage: dict) -> bool:
        return any(
            isinstance(value, str) and value.strip()
            for value in (
                stage.get("url"),
                stage.get("local_url"),
                stage.get("remote_url"),
                stage.get("composite_url"),
                stage.get("thumbnail_url"),
                stage.get("poster"),
                stage.get("last_frame_url"),
            )
        )

    def _pick_success_media_from_stages(stages_list: list[dict[str, Any]]) -> dict[str, str]:
        for stage in reversed(stages_list):
            if not isinstance(stage, dict):
                continue
            if not _is_image_stage_name(stage.get("name")):
                continue
            status_value = str(stage.get("status") or "").strip().lower()
            if status_value and status_value not in {"completed", "success", "succeeded", "done"}:
                continue
            candidate = _media_from_output(stage)
            if candidate:
                return candidate
        for stage in reversed(stages_list):
            if not isinstance(stage, dict):
                continue
            if not _stage_has_image_url(stage):
                continue
            status_value = str(stage.get("status") or "").strip().lower()
            if status_value and status_value not in {"completed", "success", "succeeded", "done"}:
                continue
            candidate = _media_from_output(stage)
            if candidate:
                return candidate
        return {}

    def _pick_success_media_from_history(output_obj: Any) -> dict[str, str]:
        for entry in media_history.media_history_from_output(output_obj):
            if not isinstance(entry, dict):
                continue
            candidate = _media_from_output(entry.get("output"))
            if candidate:
                return candidate
        return {}

    seeded_stage = None
    if not isinstance(existing, dict) or existing.get("type") != "fusion":
        existing = {"type": "fusion", "subject": subj, "stages": []}
        raw_media = _media_from_output(raw_existing)
        if raw_media:
            seeded_stage = {
                "name": stage_name,
                "status": "completed",
                "url": raw_media.get("url"),
                "local_url": raw_media.get("local_url"),
                "remote_url": raw_media.get("remote_url"),
            }
            size_hint = raw_media.get("size") or raw_media.get("size_requested") or raw_media.get("size_final")
            if size_hint:
                seeded_stage["size"] = size_hint
            if raw_media.get("aspect_ratio"):
                seeded_stage["aspect_ratio"] = raw_media["aspect_ratio"]
            if raw_media.get("quality"):
                seeded_stage["quality"] = raw_media["quality"]

    stages = [dict(s) for s in (existing.get("stages") or []) if isinstance(s, dict)]
    if not stages and seeded_stage:
        stages.append(seeded_stage)

    incoming_media = any(
        isinstance(v, str) and v.strip()
        for v in (url, local_url, remote_url)
    )
    payload: dict[str, Any] = {"name": stage_name, "status": status}
    for k, v in (
        ("url", url), ("local_url", local_url), ("remote_url", remote_url),
        ("size", size), ("aspect_ratio", aspect_ratio), ("quality", quality),
        ("error", error),
        ("diagnostics", diagnostics),
    ):
        if v is not None:
            payload[k] = v

    target_index = None
    for i, s in enumerate(stages):
        if s.get("name") == stage_name:
            target_index = i
            break
    if target_index is None:
        for i in range(len(stages) - 1, -1, -1):
            if _is_image_stage_name(stages[i].get("name")):
                target_index = i
                break
        if target_index is None:
            for i in range(len(stages) - 1, -1, -1):
                if _stage_has_image_url(stages[i]):
                    target_index = i
                    break
    found = False
    if target_index is not None:
        stage = stages[target_index]
        if status in {"running", "failed"} and not incoming_media:
            fallback = _media_from_output(stage)
            if not _stage_has_image_url(stage):
                fallback = _pick_success_media_from_stages(stages) or _pick_success_media_from_history(existing)
            if fallback:
                if not payload.get("url"):
                    payload["url"] = (
                        fallback.get("url")
                        or fallback.get("local_url")
                        or fallback.get("composite_url")
                        or fallback.get("thumbnail_url")
                        or fallback.get("poster")
                        or fallback.get("last_frame_url")
                    )
                if not payload.get("local_url"):
                    payload["local_url"] = (
                        fallback.get("local_url")
                        or fallback.get("url")
                        or fallback.get("composite_url")
                        or fallback.get("thumbnail_url")
                    )
                if not payload.get("remote_url"):
                    payload["remote_url"] = (
                        fallback.get("remote_url")
                        or fallback.get("url")
                        or fallback.get("composite_url")
                    )
        merged = {**stage, **payload}
        if status == "completed":
            for key in (
                "error",
                "error_message",
                "image_error",
                "provider_msg",
                "error_kind",
                "error_source",
                "http_code",
                "endpoint",
                "diagnostics",
            ):
                merged.pop(key, None)
        elif status == "running":
            if error is None:
                for key in (
                    "error",
                    "error_message",
                    "image_error",
                    "provider_msg",
                    "error_kind",
                    "error_source",
                    "http_code",
                    "endpoint",
                    "diagnostics",
                ):
                    merged.pop(key, None)
        stages[target_index] = merged
        found = True

    if target_index is None:
        if status in {"running", "failed"} and not incoming_media:
            fallback = _pick_success_media_from_stages(stages) or _pick_success_media_from_history(existing)
            if fallback:
                if not payload.get("url"):
                    payload["url"] = (
                        fallback.get("url")
                        or fallback.get("local_url")
                        or fallback.get("composite_url")
                        or fallback.get("thumbnail_url")
                        or fallback.get("poster")
                        or fallback.get("last_frame_url")
                    )
                if not payload.get("local_url"):
                    payload["local_url"] = (
                        fallback.get("local_url")
                        or fallback.get("url")
                        or fallback.get("composite_url")
                        or fallback.get("thumbnail_url")
                    )
                if not payload.get("remote_url"):
                    payload["remote_url"] = (
                        fallback.get("remote_url")
                        or fallback.get("url")
                        or fallback.get("composite_url")
                    )
        # 没有可匹配阶段时，先新增阶段（保持 prompt/历史阶段不变）
        stages.append(payload)
        found = True

    if not found:
        stages.append(payload)

    fusion: dict[str, Any] = {"type": "fusion", "subject": subj, "stages": stages}
    if prompt:
        fusion["prompt"] = prompt
    elif isinstance(existing.get("prompt"), str) and existing.get("prompt"):
        fusion["prompt"] = existing["prompt"]
    if input_data:
        fusion["input"] = media_history.strip_media_history(input_data)
    elif isinstance(existing.get("input"), dict):
        fusion["input"] = media_history.strip_media_history(existing["input"])
    history = media_history.media_history_from_output(existing)
    if history:
        fusion = media_history.attach_media_history(fusion, history, skip_current=False)
    await canvas_tools.update_node(node_id, {"output_data": fusion})
    return fusion


async def _archive_current_media_output_for_rerun(
    node_id: str,
    node: dict,
    node_type: str | None,
    fields: dict[str, Any],
) -> Any:
    current_output = node.get("output") if isinstance(node, dict) else None
    input_for_history = current_output.get("input") if isinstance(current_output, dict) and isinstance(current_output.get("input"), dict) else fields
    archived = media_history.archive_current_media_output(
        current_output,
        node_type=str(node_type or ""),
        prompt=media_history.prompt_from_state(current_output, input_for_history, str(node.get("prompt") or fields.get("prompt") or "")),
        input_data=input_for_history,
    )
    if isinstance(archived, dict) and archived != current_output:
        await canvas_tools.update_node(node_id, {"output_data": archived})
        node["output"] = archived
    return archived


async def _emit_fusion_canvas_event(
    node_id: str,
    status: str,
    *,
    preview: dict | None = None,
    error: str | None = None,
    render_state: str | None = None,
    project_id: str | None = None,
) -> None:
    """推完整 fusion preview 到前端,不再用简化 image preview 覆盖。"""
    try:
        from app.agent.orchestrator import emit_canvas_event
        payload: dict = {"id": node_id, "status": status}
        if error:
            payload["error"] = error[:200]
            payload["error_message"] = error[:200]
        elif status in {"completed", "running"}:
            payload["error"] = None
            payload["error_message"] = None
        if render_state:
            payload["render_state"] = render_state
        if preview:
            payload["preview"] = preview
        await emit_canvas_event(
            {"type": "canvas_action", "action": "update_node", "payload": payload},
            project_id=project_id,
        )
    except Exception:
        logger.exception("emit_fusion_canvas_event failed")


def _coerce_dict(value: Any, label: str) -> dict | None:
    """LLM 经常把 dict 参数序列化成 JSON 字符串。容错解析,失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


NODE_TYPES = (
    "text",
    "image",
    "video",
    "audio",
)


_TITLE_BUILDERS: dict[str, Callable[[dict], str]] = {
    "text":  lambda f: f.get("title") or "文本",
    "image": lambda f: f.get("title") or "图片",
    "video": lambda f: f.get("title") or "视频",
    "audio": lambda f: f.get("title") or "音频",
}


_EXACT_RESOLUTION_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_COMMON_RESOLUTION_BY_ASPECT = {
    "16:9": "1920x1080",
    "9:16": "1080x1920",
    "1:1": "1080x1080",
}
_MAX_4K_PIXEL_AREA = 3840 * 2160
_MAX_4K_DIMENSION = 3840

_DIRECT_INPUT_PATCH_KEYS = {
    "aspect_ratio",
    "content",
    "depends_on",
    "description",
    "duration",
    "duration_seconds",
    "first_frame_asset_id",
    "image_prompt",
    "last_frame_asset_id",
    "media_references",
    "mode",
    "negative_prompt",
    "no_visual_references",
    "production_path",
    "prompt_source",
    "prompt_status",
    "prompt_review",
    "purpose",
    "quality",
    "reference_audios",
    "reference_images",
    "reference_videos",
    "references",
    "resolution",
    "seed",
    "source_image",
    "visual_prompt",
    "video_mode",
}

_REVIEW_PASSED_STATUSES = {"pass", "passed", "approved", "ok", "true"}


def _resolution_examples(aspect_ratio: str | None) -> str:
    aspect = (aspect_ratio or "16:9").strip()
    primary = _COMMON_RESOLUTION_BY_ASPECT.get(aspect)
    examples = [v for v in [primary, "1920x1080", "1080x1920", "1080x1080"] if v]
    deduped = list(dict.fromkeys(examples))
    return "、".join(deduped)


def _parse_aspect_ratio(aspect_ratio: str | None) -> tuple[float, float, str]:
    raw = (aspect_ratio or "16:9").strip()
    try:
        w_part, h_part = raw.split(":", 1)
        w_ratio = float(w_part)
        h_ratio = float(h_part)
    except (ValueError, AttributeError):
        raise ValueError(f"fields.aspect_ratio 必须是 W:H 格式，例如 16:9、9:16、1:1；当前值: {raw!r}")
    if w_ratio <= 0 or h_ratio <= 0:
        raise ValueError(f"fields.aspect_ratio 必须使用正数比例；当前值: {raw!r}")
    return w_ratio, h_ratio, raw


def _resolve_size(resolution: str | None, aspect_ratio: str | None) -> str:
    """Validate and return an exact provider size such as ``1080x1920``.

    Backend no longer converts tier labels or fixes mismatches. The model must
    write a concrete pixel size, then repair the node when validation fails.
    """
    w_ratio, h_ratio, aspect = _parse_aspect_ratio(aspect_ratio)
    raw = str(resolution or "").strip().lower().replace("×", "x")
    if not raw:
        raise ValueError(
            "fields.resolution 必填，必须是精确像素尺寸 '<width>x<height>'；"
            f"aspect_ratio={aspect} 可用示例: {_resolution_examples(aspect)}。"
        )
    match = _EXACT_RESOLUTION_RE.fullmatch(raw)
    if not match:
        raise ValueError(
            "fields.resolution 必须是精确像素尺寸 '<width>x<height>'，不要写 1k/2k/4k 这种档位；"
            f"aspect_ratio={aspect} 可用示例: {_resolution_examples(aspect)}。"
        )
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("fields.resolution 的宽高必须是正整数。")
    if width % 8 != 0 or height % 8 != 0:
        raise ValueError(
            "fields.resolution 的宽高必须是 8 的倍数，便于媒体 provider 接收；"
            f"当前值: {raw}。"
        )
    if width > _MAX_4K_DIMENSION or height > _MAX_4K_DIMENSION or width * height > _MAX_4K_PIXEL_AREA:
        raise ValueError(
            "fields.resolution 超过后端最高 4K 等级；请写不超过 3840x2160 等价像素量的精确尺寸。"
            "16:9 最高 3840x2160，9:16 最高 2160x3840，1:1 建议不超过 2880x2880；"
            f"当前值: {raw}。"
        )
    actual = width / height
    target = w_ratio / h_ratio
    if abs(actual - target) / max(target, 1e-6) > 0.01:
        raise ValueError(
            "fields.resolution 必须与 fields.aspect_ratio 匹配；"
            f"当前 resolution={raw}, aspect_ratio={aspect}。"
            f"可用示例: {_resolution_examples(aspect)}。"
        )
    return raw


def _invalid_image_resolution_payload(
    message: str,
    fields: dict[str, Any],
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    aspect = str(fields.get("aspect_ratio") or "16:9").strip()
    payload: dict[str, Any] = {
        "ok": False,
        "error": message,
        "error_kind": "invalid_resolution",
        "fields": {
            "aspect_ratio": aspect,
            "resolution": fields.get("resolution"),
        },
        "hint": (
            "image 节点 fields.resolution 必须写精确像素尺寸 '<width>x<height>'，"
            "并且与 fields.aspect_ratio 匹配；例如 16:9 写 1920x1080，"
            "9:16 写 1080x1920。不要写 1k/2k/4k。"
        ),
        "model_feedback": {
            "what_went_wrong": "图片节点分辨率字段不是后端可执行的精确像素值。",
            "how_to_fix": (
                "使用 node.update 修正原节点 input_json.resolution，例如 "
                "{\"resolution\":\"1080x1920\"}，再对同一个节点调用 node.run。"
            ),
        },
    }
    if node_id:
        payload["node_id"] = node_id
    return payload


def _validate_image_resolution_fields(
    fields: dict[str, Any],
    *,
    node_id: str | None = None,
) -> dict[str, Any] | None:
    try:
        _resolve_size(fields.get("resolution"), fields.get("aspect_ratio") or "16:9")
    except ValueError as exc:
        return _invalid_image_resolution_payload(str(exc), fields, node_id=node_id)
    return None


def _node_content_needs_review(node_type: str | None, fields: dict[str, Any]) -> bool:
    if node_type not in NODE_TYPES:
        return False
    return bool(
        str(
            fields.get("prompt")
            or fields.get("image_prompt")
            or fields.get("visual_prompt")
            or fields.get("content")
            or fields.get("description")
            or ""
        ).strip()
    )


def _prompt_review_passed(fields: dict[str, Any], state: dict[str, Any] | None = None) -> bool:
    raw_status = fields.get("review_status") or fields.get("prompt_review_status")
    review = fields.get("prompt_review")
    if isinstance(review, dict):
        raw_status = raw_status or review.get("status") or review.get("review_status")
    if str(raw_status or "").strip().lower() in _REVIEW_PASSED_STATUSES:
        return True
    latest_review = state.get("_last_agent_review") if isinstance(state, dict) else None
    if isinstance(latest_review, dict):
        latest_status = str(latest_review.get("status") or latest_review.get("outcome") or "").strip().lower()
        if latest_status in {"pass", "passed"} and latest_review.get("safe_to_run") is not False:
            return True
    return False


def _prompt_review_required_payload(
    node_id: str,
    node_type: str,
    fields: dict[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    return {
        "review_recommended": True,
        "review_status": "review_recommended",
        "recommended_tool": "agent.review",
        "node_id": node_id,
        "node_type": node_type,
        "phase": phase,
        "review_goal": "检查节点内容、prompt、fields、references 是否符合当前用户要求和当前 skill，是否可以继续运行或交付。",
        "review_focus": [
            "active_skill_compliance",
            "user_requirements",
            "content_or_prompt_shape",
            "fields",
            "references",
        ],
        "review_evidence_hint": {
            "node_id": node_id,
            "type": node_type,
            "title": fields.get("title"),
            "purpose": fields.get("purpose"),
            "content_preview": str(fields.get("content") or fields.get("prompt") or "")[:600],
            "fields": {
                key: fields.get(key)
                for key in ("aspect_ratio", "resolution", "duration_seconds", "quality", "purpose", "references", "depends_on")
                if fields.get(key) not in (None, "", [], {})
            },
        },
        "how_to_continue": (
            "优先调用 agent.review 或自行按当前用户要求和 active skill 检查节点。"
            "如果发现问题，用 node.update 修原节点；没有阻塞问题时可以继续运行或交付。"
        ),
    }


def _node_run_timeout_seconds(node_type: str | None) -> int:
    if node_type == "image":
        return int(IMAGE_RENDER_TIMEOUT_SECONDS + 30)
    return NODE_RUN_TIMEOUT_SECONDS


def _image_render_failure_diagnosis(
    result: dict[str, Any],
    fields: dict[str, Any],
    *,
    node_id: str,
    node_type: str,
    aspect_ratio: str,
    requested_size: str,
) -> dict[str, Any]:
    attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
    http_code = result.get("http_code")
    try:
        code = int(http_code) if http_code is not None else None
    except (TypeError, ValueError):
        code = None
    likely_causes: list[str] = []
    if code is not None and 500 <= code < 600:
        likely_causes.append("provider_5xx_or_upstream_transient_failure")
    if not likely_causes:
        likely_causes.append("image_provider_returned_error")

    return {
        "kind": "image_render_failure",
        "node_id": node_id,
        "node_type": node_type,
        "likely_causes": likely_causes,
        "requested": {
            "resolution": fields.get("resolution"),
            "size": result.get("size_requested") or requested_size,
            "aspect_ratio": aspect_ratio,
            "quality": fields.get("quality"),
        },
        "last_attempt": {
            "size": result.get("size_final") or requested_size,
            "actual_size": result.get("actual_size"),
            "actual_aspect_ratio": result.get("actual_aspect_ratio"),
            "quality": result.get("quality_final"),
            "http_code": code,
            "error_kind": result.get("error_kind"),
        },
        "provider_retry_attempts": attempts,
        "suggested_patch": None,
        "suggested_next": (
            "先读取 provider_msg/error，判断是参数、provider 配置还是外部服务失败；"
            "需要改字段时用 node.update 修正原节点，再 node.run(node_id, action='render') 重试。"
            "后端不会自动降级、改质量或重试，不要新建替代节点。"
        ),
    }


_DEFAULT_FIELDS_BY_TYPE: dict[str, dict] = {
    "image": {
        "aspect_ratio": "16:9",
        "quality": "high",
    },
    "video": {
        "aspect_ratio": "16:9",
        "duration_seconds": 5,
    },
}


def _apply_defaults(node_type: str, fields: dict) -> dict:
    """按 type 给生图类节点 fields 兜底默认值,模型不传也能拿到完整规格。"""
    defaults = _DEFAULT_FIELDS_BY_TYPE.get(node_type)
    if not defaults:
        return fields
    if (
        node_type == "video"
        and fields.get("duration_seconds") in (None, "", [], {})
        and fields.get("duration") not in (None, "", [], {})
    ):
        fields["duration_seconds"] = fields.get("duration")
    for k, v in defaults.items():
        fields.setdefault(k, v)
    return fields


def _reference_input_for_state_asset(asset: dict[str, Any]) -> str:
    rel_path = str(asset.get("rel_path") or "").strip()
    if rel_path:
        return rel_path
    source_path = str(asset.get("source_path") or "").strip()
    if source_path:
        return source_path
    asset_id = str(asset.get("asset_id") or "").strip()
    if asset_id:
        return f"asset:{asset_id}"
    node_id = str(asset.get("node_id") or "").strip()
    if node_id:
        return f"node:{node_id}"
    url = str(asset.get("url") or "").strip()
    if url:
        return url
    return ""


def _add_reference_lookup(lookup: dict[str, str], key: Any, value: str) -> None:
    text = str(key or "").strip()
    if not text or not value:
        return
    lookup[text] = value
    if text.startswith("@"):
        lookup[text.lstrip("@")] = value
    else:
        lookup[f"@{text}"] = value


def _looks_like_bare_workflow_node_id(text: str) -> bool:
    if not text or text.startswith(("node:", "asset:", "http://", "https://")):
        return False
    if "/" in text or "\\" in text or "." in text:
        return False
    if len(text) != 36 or text.count("-") != 4:
        return False
    return all(ch in "0123456789abcdefABCDEF-" for ch in text)


async def _normalize_node_reference_image_for_render(project_id: str, text: str) -> tuple[str, str, bool]:
    prefixed = text.startswith("node:")
    raw_node_id = text[len("node:"):].strip() if prefixed else text
    if not raw_node_id:
        return "", "", False
    if not prefixed and not _looks_like_bare_workflow_node_id(raw_node_id) and not looks_like_public_node_id(raw_node_id):
        return "", "", False
    raw_node_id = await _resolve_agent_node_id(project_id, raw_node_id)
    if not raw_node_id:
        return "", "", False
    node = await canvas_tools.get_node(raw_node_id)
    if not isinstance(node, dict) or node.get("error"):
        return "", "", False
    if node.get("project_id") != project_id:
        return "", "", False
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        return "", "", False
    if node.get("type") != "image":
        title = str(node.get("title") or node_id)
        return "", f"reference_images 已跳过非图片节点 {title}", True
    warning = "" if prefixed else f"reference_images 已将裸节点 ID/节点编号 {text} 规范化为 node:{public_node_id_from_dict(node)}"
    return f"node:{node_id}", warning, True


async def _normalize_reference_images_for_render(
    project_id: str,
    refs: Any,
) -> tuple[list[str], list[str]]:
    """Accept node, upload, asset, URL, and path references as renderable inputs.

    The provider layer accepts local storage-relative paths, absolute paths,
    URLs, upload:<rel_path>, asset:<id>, and node:<image_id>. Node references are
    normalized to internal node ids; upload references are normalized to storage
    relative paths.
    """
    if not isinstance(refs, list):
        return [], []

    normalized: list[str] = []
    warnings: list[str] = []
    for raw in refs:
        if isinstance(raw, dict):
            candidate = (
                raw.get("ref")
                or raw.get("reference")
                or raw.get("reference_input")
                or raw.get("rel_path")
                or raw.get("source_path")
                or raw.get("url")
                or raw.get("path")
                or raw.get("local_path")
                or (f"asset:{raw.get('asset_id')}" if raw.get("asset_id") else "")
                or (f"node:{raw.get('node_id')}" if raw.get("node_id") else "")
                or raw.get("ref_id")
                or raw.get("mention")
            )
        else:
            candidate = raw
        text = str(candidate or "").strip()
        if not text:
            continue
        if text.startswith("ref:"):
            text = text[len("ref:"):].strip()
        if text.startswith("upload:"):
            text = _storage_relative_upload_reference(text)
        if text.startswith("ref_") or text.startswith("@"):
            warnings.append(f"reference_images 未能解析 {text};请改用 node:<编号>、upload:<rel_path>、asset:<id> 或图片路径")
            continue
        else:
            node_ref, node_warning, handled_node_ref = await _normalize_node_reference_image_for_render(project_id, text)
            if handled_node_ref and not node_ref:
                if node_warning:
                    warnings.append(node_warning)
                continue
            if node_ref:
                text = node_ref
                if node_warning:
                    warnings.append(node_warning)
        if text and text not in normalized:
            normalized.append(text)
    return normalized, warnings


_DIRECT_IMAGE_SOURCE_ROLES = {
    "source_image",
    "direct_image",
    "output_image",
    "use_as_output",
    "primary_image",
}

_MEDIA_REFERENCE_ROLES = {
    "",
    "reference",
    "visual_reference",
    "image_reference",
    "reference_image",
    "media_reference",
    "style_reference",
    "character_reference",
    "scene_reference",
    "storyboard_reference",
    "first_frame",
    "last_frame",
}


def _reference_role(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return (
        str(
            item.get("role")
            or item.get("usage")
            or item.get("purpose")
            or item.get("kind")
            or ""
        )
        .strip()
        .lower()
        .replace("-", "_")
    )


def _reference_candidate(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    candidate = (
        item.get("ref")
        or item.get("reference")
        or item.get("reference_input")
        or item.get("value")
        or item.get("rel_path")
        or item.get("source_path")
        or item.get("url")
        or item.get("path")
        or item.get("local_path")
        or item.get("node_id")
        or item.get("asset_id")
        or item.get("blueprint_node_id")
        or item.get("ref_id")
        or item.get("mention")
        or item.get("id")
        or item.get("title")
    )
    if item.get("node_id") and not str(candidate or "").startswith("node:"):
        candidate = f"node:{item.get('node_id')}"
    elif item.get("asset_id") and not str(candidate or "").startswith("asset:"):
        candidate = f"asset:{item.get('asset_id')}"
    return candidate


def _coerce_reference_values(
    *values: Any,
    include_roles: set[str] | None = None,
    exclude_roles: set[str] | None = None,
) -> list[str]:
    refs: list[str] = []
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            role = _reference_role(item)
            if include_roles is not None and role not in include_roles:
                continue
            if exclude_roles is not None and role in exclude_roles:
                continue
            candidate = _reference_candidate(item)
            text = str(candidate or "").strip()
            if text and text not in refs:
                refs.append(text)
    return refs


def _reference_lookup_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        text = text[1:].strip()
    for prefix in ("node:", "asset:", "blueprint:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def _add_node_reference_lookup(lookup: dict[str, WorkflowNode], key: Any, node: WorkflowNode) -> None:
    text = str(key or "").strip()
    if not text:
        return
    for candidate in {text, text.lstrip("@"), f"@{text.lstrip('@')}"}:
        normalized = _reference_lookup_key(candidate)
        if normalized:
            lookup.setdefault(normalized, node)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def _image_node_reference_images_for_video(
    project_id: str,
    refs: list[str],
) -> tuple[list[str], list[str]]:
    """Resolve semantic node references to completed image inputs for media runners."""
    if not refs:
        return [], []

    async with session_scope() as session:
        rows = list((await session.exec(
            select(WorkflowNode).where(WorkflowNode.project_id == project_id)
        )).all())

    lookup: dict[str, WorkflowNode] = {}
    for node in rows:
        data = _json_object(node.input_json)
        display_id = getattr(node, "display_id", None)
        for key in (
            node.id,
            display_id,
            f"#{display_id}" if display_id is not None else None,
            node.title,
            data.get("id"),
            data.get("title"),
            data.get("blueprint_node_id"),
            *(data.get("aliases") if isinstance(data.get("aliases"), list) else []),
        ):
            _add_node_reference_lookup(lookup, key, node)

    resolved: list[str] = []
    warnings: list[str] = []
    for ref in refs:
        text = str(ref or "").strip()
        if not text:
            continue
        if text.startswith("upload:"):
            text = _storage_relative_upload_reference(text)
        if (
            text.startswith(("http://", "https://", "asset:", "uploads/", "generated_images/", "/api/media/", "/api/uploads/"))
            or "/" in text
            or "\\" in text
        ):
            if text not in resolved:
                resolved.append(text)
            continue
        key = _reference_lookup_key(text)
        node = lookup.get(key)
        if node is None:
            continue
        if node.type != "image":
            continue
        if node.status != "completed":
            warnings.append(f"参考图 {text} 对应图片节点 {node.title or node.id} 尚未完成，已跳过")
            continue
        value = f"node:{node.id}"
        if value not in resolved:
            resolved.append(value)
    return resolved, warnings


async def _reference_images_for_media_run(
    project_id: str,
    fields: dict[str, Any],
) -> tuple[list[str], list[str]]:
    explicit_refs, warnings = await _normalize_reference_images_for_render(
        project_id,
        fields.get("reference_images") or [],
    )
    semantic_refs = _coerce_reference_values(
        fields.get("references"),
        fields.get("reference_images"),
        include_roles=_MEDIA_REFERENCE_ROLES,
        exclude_roles=_DIRECT_IMAGE_SOURCE_ROLES,
    )
    node_refs, node_warnings = await _image_node_reference_images_for_video(project_id, semantic_refs)
    merged: list[str] = []
    for ref in [*explicit_refs, *node_refs]:
        if ref and ref not in merged:
            merged.append(ref)
    return merged, [*warnings, *node_warnings]


def _reference_image_mentions(fields: dict[str, Any]) -> list[dict[str, Any]]:
    raw = fields.get("reference_image_mentions") or []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        mention = str(item.get("mention") or "").strip()
        ref = str(item.get("ref") or item.get("reference") or "").strip()
        if not mention or not ref:
            continue
        key = (mention, _reference_lookup_key(ref))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "mention": mention,
            "label": str(item.get("label") or mention.lstrip("@") or "reference image").strip(),
            "ref": ref,
            "source": str(item.get("source") or "").strip(),
            "index": item.get("index"),
        })
    return result


def _reference_image_mention_note(fields: dict[str, Any], reference_images: list[str]) -> str:
    mentions = _reference_image_mentions(fields)
    if not mentions:
        return ""
    index_by_ref = {
        _reference_lookup_key(ref): index + 1
        for index, ref in enumerate(reference_images)
        if _reference_lookup_key(ref)
    }
    raw_reference_images = fields.get("reference_images") or []
    if not isinstance(raw_reference_images, list):
        raw_reference_images = [raw_reference_images]
    for index, ref in enumerate(raw_reference_images):
        key = _reference_lookup_key(ref)
        if key and key not in index_by_ref:
            index_by_ref[key] = index + 1
    rows: list[str] = []
    for item in mentions:
        ref = str(item.get("ref") or "").strip()
        explicit_index = item.get("index")
        try:
            fallback_index = int(explicit_index) if explicit_index is not None else None
        except (TypeError, ValueError):
            fallback_index = None
        ref_index = index_by_ref.get(_reference_lookup_key(ref)) or fallback_index
        target = f"第 {ref_index} 张参考图" if ref_index else f"参考图 {ref}"
        rows.append(f"- {item['mention']} 指向 {target}")
    if not rows:
        return ""
    return "参考图标记说明：\n" + "\n".join(rows)


def _prompt_with_reference_image_mentions(prompt: str, fields: dict[str, Any], reference_images: list[str]) -> str:
    note = _reference_image_mention_note(fields, reference_images)
    if not note:
        return prompt
    return f"{note}\n\n用户提示词：\n{prompt}"


async def _reference_images_for_video_run(
    project_id: str,
    fields: dict[str, Any],
) -> tuple[list[str], list[str]]:
    return await _reference_images_for_media_run(project_id, fields)


def _collect_image_source_values(value: Any, out: list[str] | None = None) -> list[str]:
    if out is None:
        out = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"url", "local_url", "remote_url", "local_path", "path"} and isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, (dict, list)):
                _collect_image_source_values(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_image_source_values(item, out)
    return out


def _storage_root() -> Path:
    return settings.storage_path_resolved


def _storage_relative_upload_reference(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("upload:"):
        return text
    rel = text[len("upload:"):].strip().lstrip("/")
    if rel and not rel.startswith("uploads/"):
        rel = f"uploads/{rel}"
    return rel


def _local_url_for_storage_path(project_id: str, path: Path) -> str | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    project_root = (_storage_root() / project_id).resolve()
    generated_root = (project_root / "generated_images").resolve()
    uploads_root = (project_root / "uploads").resolve()
    try:
        rel = resolved.relative_to(generated_root)
        return f"/api/media/{project_id}/{rel.as_posix()}"
    except ValueError:
        pass
    try:
        rel = resolved.relative_to(uploads_root)
        return f"/api/uploads/{project_id}/file/uploads/{rel.as_posix()}"
    except ValueError:
        return None


def _image_output_from_source_value(project_id: str, value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("upload:"):
        text = _storage_relative_upload_reference(text)
    if text.startswith(("http://", "https://")):
        return {"url": text, "remote_url": text}
    if text.startswith(("/api/media/", "/api/uploads/")):
        return {"url": text, "local_url": text}
    if text.startswith("generated_images/"):
        local_url = f"/api/media/{project_id}/{text[len('generated_images/'):].lstrip('/')}"
        return {"url": local_url, "local_url": local_url}
    if text.startswith("uploads/"):
        local_url = f"/api/uploads/{project_id}/file/{text}"
        return {"url": local_url, "local_url": local_url}

    path = Path(text).expanduser()
    if not path.is_absolute():
        path = _storage_root() / project_id / text
    if path.exists() and path.is_file():
        local_url = _local_url_for_storage_path(project_id, path)
        output = {"local_path": str(path.resolve())}
        if local_url:
            output.update({"url": local_url, "local_url": local_url})
        else:
            output["url"] = str(path.resolve())
        return output
    return None


async def _image_output_from_node_reference(project_id: str, node_ref: str) -> tuple[dict[str, Any] | None, str | None]:
    node_id = node_ref[len("node:"):].strip() if node_ref.startswith("node:") else node_ref.strip()
    node_id = await _resolve_agent_node_id(project_id, node_id)
    node = await canvas_tools.get_node(node_id)
    if not isinstance(node, dict) or node.get("error"):
        return None, f"source_image 节点不存在: {node_ref}"
    if node.get("project_id") != project_id:
        return None, f"source_image 节点不属于当前项目: {node_ref}"
    if node.get("type") != "image":
        return None, f"source_image 只能直接采用 image 节点，收到 {node.get('type')}: {node_ref}"
    output = node.get("output")
    for candidate in _collect_image_source_values(output):
        resolved = _image_output_from_source_value(project_id, candidate)
        if resolved:
            resolved["source_node_id"] = node.get("id")
            return resolved, None
    return None, f"source_image 节点没有可用图片输出: {node_ref}"


async def _image_output_from_asset_reference(project_id: str, asset_ref: str) -> tuple[dict[str, Any] | None, str | None]:
    asset_id = asset_ref[len("asset:"):].strip()
    async with session_scope() as session:
        asset = await session.get(Asset, asset_id)
    if not asset or asset.project_id != project_id:
        return None, f"source_image 资产不存在: {asset_ref}"
    metadata = _json_object(asset.metadata_json)
    for candidate in (
        asset.url,
        metadata.get("local_url"),
        metadata.get("url"),
        metadata.get("remote_url"),
        asset.path,
        metadata.get("local_path"),
        metadata.get("path"),
    ):
        resolved = _image_output_from_source_value(project_id, str(candidate or ""))
        if resolved:
            resolved["source_asset_id"] = asset.id
            return resolved, None
    return None, f"source_image 资产没有可用图片输出: {asset_ref}"


async def _image_output_from_reference(project_id: str, ref: str) -> tuple[dict[str, Any] | None, str | None]:
    text = str(ref or "").strip()
    if not text:
        return None, "source_image 引用为空"
    if text.startswith("node:"):
        return await _image_output_from_node_reference(project_id, text)
    if text.startswith("asset:"):
        return await _image_output_from_asset_reference(project_id, text)
    output = _image_output_from_source_value(project_id, text)
    if output:
        return output, None
    return None, f"source_image 无法解析为可用图片: {text}"


def _project_storage_path_from_media_url(project_id: str, url: str) -> Path | None:
    text = unquote(str(url or "").strip())
    if not text.startswith("/"):
        return None
    media_prefix = f"/api/media/{project_id}/"
    upload_prefix = f"/api/uploads/{project_id}/file/"
    root = _storage_root() / project_id
    if text.startswith(media_prefix):
        rel = text[len(media_prefix):].lstrip("/")
        return root / "generated_images" / rel
    if text.startswith(upload_prefix):
        rel = text[len(upload_prefix):].lstrip("/")
        return root / rel
    return None


def _image_data_url_from_path(path: Path) -> tuple[str | None, str | None]:
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        return None, f"参考图路径无法解析: {path} ({exc})"
    if not resolved.exists() or not resolved.is_file():
        return None, f"参考图文件不存在: {path}"
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return None, f"参考图文件无法读取: {path} ({exc})"
    if size > TEXT_REFERENCE_IMAGE_MAX_BYTES:
        return None, f"参考图文件过大，已跳过: {path}"
    mime = mimetypes.guess_type(str(resolved))[0] or "image/png"
    try:
        data = base64.b64encode(resolved.read_bytes()).decode("ascii")
    except OSError as exc:
        return None, f"参考图文件无法读取: {path} ({exc})"
    return f"data:{mime};base64,{data}", None


def _llm_image_url_from_source_value(project_id: str, value: str) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    if text.startswith("upload:"):
        text = _storage_relative_upload_reference(text)
    if text.startswith(("http://", "https://", "data:image/")):
        return text, None

    path = _project_storage_path_from_media_url(project_id, text)
    if path is None and text.startswith(("generated_images/", "uploads/")):
        path = _storage_root() / project_id / text
    if path is None:
        raw_path = Path(text).expanduser()
        if raw_path.is_absolute():
            path = raw_path
        elif "/" in text or "\\" in text:
            path = _storage_root() / project_id / text

    if path is not None:
        data_url, warning = _image_data_url_from_path(path)
        if data_url:
            return data_url, None
        if text.startswith(("/api/media/", "/api/uploads/")):
            return text, warning
        return None, warning

    if text.startswith(("/api/media/", "/api/uploads/")):
        return text, None
    return None, f"参考图无法解析为可发送图片: {text}"


async def _llm_image_url_from_reference(project_id: str, ref: str) -> tuple[str | None, str | None]:
    from app.agent.vision_context import source_to_image_url

    async def prepare(source: Any) -> tuple[str | None, str | None]:
        text_source = str(source or "").strip()
        if not text_source:
            return None, None
        try:
            image_url, _metadata = await source_to_image_url(project_id, text_source)
            return image_url, None
        except Exception as exc:
            return None, f"参考图无法读取 {text_source}: {exc}"

    text = str(ref or "").strip()
    if not text:
        return None, None
    if text.startswith("node:"):
        output, error = await _image_output_from_node_reference(project_id, text)
        if not output:
            return None, error
        warnings: list[str] = []
        for candidate in (
            output.get("local_path"),
            output.get("url"),
            output.get("local_url"),
            output.get("remote_url"),
            output.get("path"),
        ):
            url, warning = await prepare(candidate)
            if url:
                return url, None
            if warning:
                warnings.append(warning)
        return None, "; ".join(dict.fromkeys(warnings)) or f"参考图片节点没有可发送图片: {text}"
    if text.startswith("asset:"):
        output, error = await _image_output_from_asset_reference(project_id, text)
        if not output:
            return None, error
        warnings = []
        for candidate in (
            output.get("local_path"),
            output.get("url"),
            output.get("local_url"),
            output.get("remote_url"),
            output.get("path"),
        ):
            url, warning = await prepare(candidate)
            if url:
                return url, None
            if warning:
                warnings.append(warning)
        return None, "; ".join(dict.fromkeys(warnings)) or f"参考资产没有可发送图片: {text}"
    return await prepare(text)


async def _reference_image_urls_for_text_run(
    project_id: str,
    fields: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    reference_images, warnings = await _reference_images_for_media_run(project_id, fields)
    urls: list[str] = []
    for ref in reference_images:
        url, warning = await _llm_image_url_from_reference(project_id, ref)
        if url and url not in urls:
            urls.append(url)
        if warning:
            warnings.append(warning)
    return reference_images, urls, warnings


async def _workflow_text_vision_context_image_urls(
    project_id: str,
    fields: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    declared_refs = _coerce_reference_values(
        fields.get("references"),
        include_roles={"vision_context"},
    )
    normalized_refs, warnings = await _normalize_reference_images_for_render(project_id, declared_refs)
    urls: list[str] = []
    for ref in normalized_refs:
        url, warning = await _llm_image_url_from_reference(project_id, ref)
        if url and url not in urls:
            urls.append(url)
        if warning:
            warnings.append(warning)
    return declared_refs, urls, warnings


async def _direct_image_source_output(
    project_id: str,
    fields: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    refs = _coerce_reference_values(
        fields.get("references"),
        include_roles=_DIRECT_IMAGE_SOURCE_ROLES,
    )
    if not refs:
        return None, None, []
    normalized_refs, warnings = await _normalize_reference_images_for_render(project_id, refs)
    for ref in normalized_refs:
        output, error = await _image_output_from_reference(project_id, ref)
        if output:
            image = {
                "asset_id": output.get("source_asset_id"),
                "url": output.get("url"),
                "local_url": output.get("local_url"),
                "local_path": output.get("local_path"),
                "remote_url": output.get("remote_url"),
                "source_ref": ref,
                "source_node_id": output.get("source_node_id"),
                "source_asset_id": output.get("source_asset_id"),
            }
            image = {k: v for k, v in image.items() if v not in (None, "", [], {})}
            return {
                "ok": True,
                "status": "completed",
                "source_mode": "direct_image",
                "source_image": ref,
                "url": output.get("url"),
                "local_url": output.get("local_url"),
                "local_path": output.get("local_path"),
                "remote_url": output.get("remote_url"),
                "images": [image],
                "n_requested": 1,
                "n_succeeded": 1,
                "reference_images": [],
                "reference_warnings": warnings,
            }, None, warnings
        if error:
            warnings.append(error)
    return None, {
        "ok": False,
        "error": "source_image 没有可用图片输出",
        "error_kind": "source_image_unresolved",
        "source_images": refs,
        "reference_warnings": warnings,
        "hint": "如果要直接采用图片，references 里使用 {ref:'node:<image_node_id>' 或 'asset:<id>' 或 URL/上传路径, role:'source_image'}；如果只是参考生成新图，使用普通 references 或 role:'visual_reference'。",
    }, warnings


# ─────────────────────────────────────────────────────────────────
# Mode + Guide gate(强约束三件套)
# ─────────────────────────────────────────────────────────────────

async def _read_project_state(project_id: str) -> dict:
    from app.services.project_service import ProjectService
    async with session_scope() as session:
        svc = ProjectService(session)
        state = await svc.get_project_state(project_id)
        return state or {}


async def _write_project_state_patch(project_id: str, patch: dict) -> None:
    from app.services.project_service import ProjectService
    async with session_scope() as session:
        svc = ProjectService(session)
        await svc.update_project_state(project_id, patch)


async def node_list_creatable_types(project_id: str) -> dict:
    """看当前项目状态下能建哪些 type,以及每种 type 的依赖前置。"""
    from app.services.project_service import ProjectService
    state = await _read_project_state(project_id)
    mode = state.get("project_mode")
    sub_mode = state.get("project_sub_mode")
    if not mode:
        items = []
        for t in sorted(NODE_TYPES):
            schema = _NODE_FIELD_SCHEMA.get(t, {})
            preferred_mode, preferred_sub_mode = _preferred_mode_for_node_type(state, t)
            deps = [] if preferred_mode == "single_node" else _node_dependencies_for_context(t, state)
            items.append({
                "type": t,
                "description": schema.get("description", ""),
                "required_fields": schema.get("required", []),
                "optional_fields": schema.get("optional", []),
                "depends_on": deps,
                "default_project_mode": preferred_mode,
                "default_project_sub_mode": preferred_sub_mode,
                "default_surface": _surface_for_project_mode(preferred_mode),
                "is_image_node": t in _SUBJECT_BY_TYPE,
            })
        return {
            "ok": True,
            "project_mode": None,
            "project_sub_mode": None,
            "mode_inference": "node.create 会按节点类型和蓝图/任务状态自动选择 single_node 或 video_production。",
            "surface_rule": (
                "无蓝图/无任务的单产物节点默认草稿画布(draft_canvas);"
                "有蓝图/视频任务或视频链路节点默认工程面板(project_panel)。"
            ),
            "creatable_types": items,
            "next_step": "根据当前用户目标和 node.create schema 直接创建 text/image/video/audio 节点；缺少阻塞信息时先向用户提问。",
        }
    allowed = sorted(_MODE_ALLOWED_TYPES.get(mode, set()))
    items = []
    for t in allowed:
        schema = _NODE_FIELD_SCHEMA.get(t, {})
        deps = [] if mode == "single_node" else _node_dependencies_for_context(t, state)
        items.append({
            "type": t,
            "description": schema.get("description", ""),
            "required_fields": schema.get("required", []),
            "optional_fields": schema.get("optional", []),
            "depends_on": deps,
            "is_image_node": t in _SUBJECT_BY_TYPE,
        })
    return {
        "ok": True,
        "project_mode": mode,
        "project_sub_mode": sub_mode,
        "node_surface": _surface_for_project_mode(mode),
        "surface_rule": (
            "video_production/skill_freeform 创建工程面板节点(project_panel);"
            "single_node 创建草稿画布节点(draft_canvas)。"
        ),
        "creatable_types": items,
        "next_step": "根据当前用户目标和 node.create schema 直接创建 text/image/video/audio 节点；缺少阻塞信息时先向用户提问。",
    }


async def node_get_creation_guide(project_id: str, type: str) -> dict:
    """创建任何创作类节点前必调:返回 text/image/video/audio 字段 schema。

    调用后,本会话内允许 node.create(type=同 type)。下一轮新对话需重新拉(防 LLM 用旧记忆)。
    """
    if type not in NODE_TYPES:
        return {
            "ok": False,
            "error": f"未知节点类型 {type!r},允许:{', '.join(NODE_TYPES)}",
            "error_kind": "unknown_node_type",
            "valid_types": list(NODE_TYPES),
            "hint": "公开节点 type 只允许 text / image / video / audio；制作方法、分组关系、质量参数和提示词策略写在 fields/content/prompt/references 中。",
        }
    state = await _read_project_state(project_id)
    state, inferred_mode = await _ensure_project_mode_for_type(project_id, state, type)
    mode = state.get("project_mode")
    if type not in _MODE_ALLOWED_TYPES.get(mode, set()):
        return {
            "ok": False,
            "error": f"当前模式 {mode!r} 不允许创建 {type!r}",
            "error_kind": "type_not_allowed_in_mode",
            "allowed_in_mode": sorted(_MODE_ALLOWED_TYPES.get(mode, set())),
        }

    schema = _NODE_FIELD_SCHEMA.get(type, {})
    defaults = _DEFAULT_FIELDS_BY_TYPE.get(type, {})
    deps = [] if mode == "single_node" else _node_dependencies_for_context(type, state)

    # 把 type 标记为本会话已 loaded
    guide_loaded = state.get("guide_loaded") or {}
    guide_loaded[type] = True
    await _write_project_state_patch(project_id, {"guide_loaded": guide_loaded})

    # 拼示例
    example_fields = {k: f"<{k}>" for k in schema.get("required", [])}
    example_fields.update(defaults)
    if type == "image":
        example_fields["aspect_ratio"] = "9:16"
        example_fields["resolution"] = "1080x1920"

    return {
        "ok": True,
        "type": type,
        "mode_inferred": inferred_mode,
        "project_mode": mode,
        "project_sub_mode": state.get("project_sub_mode"),
        "node_surface": _surface_for_project_mode(mode),
        "surface_rule": (
            "当前 mode 决定 node.create 的展示位置:"
            "single_node → 草稿画布(draft_canvas);"
            "video_production/skill_freeform → 工程面板(project_panel)。"
        ),
        "description": schema.get("description", ""),
        "required_fields": schema.get("required", []),
        "optional_fields": schema.get("optional", []),
        "default_values": defaults,
        "depends_on": deps,
        "is_image_node": type in _SUBJECT_BY_TYPE,
        "prompt_guidance": _prompt_guidance_for_type(type),
        "call_example": {
            "tool": "node.create",
            "args": {"project_id": project_id, "type": type, "fields": example_fields},
        },
        "next_step": (
            f"已记录 {type} 的指南本会话内有效。现在按 schema 填 fields 调 node.create(type={type!r})。"
            + (" image/video/audio 必须由模型写入可执行 prompt；后端不会自动合成。" if type in {"image", "video", "audio"} else "")
        ),
    }


async def _check_mode_and_guide_gate(
    project_id: str,
    target_type: str,
    fields: dict[str, Any] | None = None,
) -> tuple[bool, dict | None]:
    """node.create 入口的轻量 gate:
    1. 后端按节点类型/蓝图/任务状态自动推断 mode
    2. mode 允许这个 type

    返回 (ok, error_payload_or_None)
    """
    state = await _read_project_state(project_id)
    state, inferred_mode = await _ensure_project_mode_for_type(project_id, state, target_type, fields)
    mode = state.get("project_mode")
    if target_type not in _MODE_ALLOWED_TYPES.get(mode, set()):
        return False, {
            "ok": False,
            "error": f"当前模式 {mode!r} 不允许创建 {target_type!r}",
            "error_kind": "type_not_allowed_in_mode",
            "current_mode": mode,
            "allowed_in_mode": sorted(_MODE_ALLOWED_TYPES.get(mode, set())),
            "mode_inferred": inferred_mode,
            "hint": "换 type 或按当前节点状态创建匹配的节点；节点归属模式由后端自动推断。",
        }
    return True, None


async def _scan_unfinished_nodes(project_id: str) -> list[dict]:
    """扫描项目所有节点，返回未完成节点列表。

    判定规则（复用 node_check_readiness 逻辑）：
    - status=failed → 未完成
    - status=running（中断遗留）→ 未完成
    - 图融合类节点：提示词 stage 不存在/非 completed，或图 stage 不存在/无 URL → 未完成
    - 非图节点：output 为空 → 未完成
    - status=completed 且所有阶段齐全 → 已完成（不在列表中）
    """
    nodes = await canvas_tools.list_nodes(project_id)
    if not isinstance(nodes, list):
        return []

    unfinished: list[dict] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = n.get("id", "")
        ntype = n.get("type", "")
        nstatus = n.get("status", "")
        ntitle = n.get("title", "")

        if n.get("superseded"):
            continue

        # status=failed 永远算未完成
        if nstatus == "failed":
            output = n.get("output") or {}
            err_msg = ""
            if isinstance(output, dict) and output.get("type") == "fusion":
                for s in (output.get("stages") or []):
                    if isinstance(s, dict) and s.get("status") == "failed":
                        err_msg = s.get("error") or "stage 失败"
                        break
            unfinished.append({
                "node_id": nid,
                "type": ntype,
                "title": ntitle,
                "status": "failed",
                "reason": err_msg or (n.get("error_message") or "节点失败"),
                "suggested_action": "在原节点调用 node.run(action='render') 或 node.run(force) 重试；未经用户明确要求不要删除",
            })
            continue

        # status=running 超过任务超时窗口 → 视为服务重启/连接中断遗留，
        # 自动回收成 failed，避免永久占住 node.create 门禁。
        if nstatus == "running":
            updated_at_raw = n.get("updated_at")
            updated_at = None
            if isinstance(updated_at_raw, str):
                try:
                    updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
                    if updated_at.tzinfo is not None:
                        updated_at = updated_at.replace(tzinfo=None)
                except ValueError:
                    updated_at = None
            if updated_at and updated_at < datetime.utcnow() - timedelta(seconds=STALE_RUNNING_SECONDS):
                err_text = "任务执行中断或超时，已自动标记失败。请在原节点重试。"
                await canvas_tools.update_node(
                    nid, {"status": "failed", "error_message": err_text},
                )
                unfinished.append({
                    "node_id": nid,
                    "type": ntype,
                    "title": ntitle,
                    "status": "failed",
                    "reason": err_text,
                    "suggested_action": "在原节点调用 node.run(action='render') 或 node.run(force) 重试；未经用户明确要求不要删除",
                })
                continue
            unfinished.append({
                "node_id": nid,
                "type": ntype,
                "title": ntitle,
                "status": "running",
                "reason": "节点被中断，状态仍为 running",
                "suggested_action": "在原节点调用 node.run(action='render') 或 node.run(force) 重试；未经用户明确要求不要删除",
            })
            continue

        # 只处理 12 类节点
        if ntype not in NODE_TYPES:
            continue

        if ntype == "image":
            output = n.get("output") or {}
            if isinstance(output, dict) and any(output.get(k) for k in ("url", "local_url", "remote_url")):
                continue
            stages: list[dict] = []
            if isinstance(output, dict) and output.get("type") == "fusion":
                raw = output.get("stages")
                if isinstance(raw, list):
                    stages = [s for s in raw if isinstance(s, dict)]
            by_name = {s.get("name"): s for s in stages if s.get("name")}
            img_stage = by_name.get("图片")

            missing_stages: list[str] = []
            if not img_stage or img_stage.get("status") != "completed" or not _stage_has_image_url(img_stage):
                missing_stages.append("图片")

            if missing_stages:
                unfinished.append({
                    "node_id": nid,
                    "type": ntype,
                    "title": ntitle,
                    "status": nstatus,
                    "reason": f"缺阶段: {', '.join(missing_stages)}",
                    "suggested_action": "确认 prompt 后在原节点调用 node.run(node_id) 或 node.run(action='render') 出图",
                })
            continue

        # 非图节点：output 为空
        output = n.get("output")
        output_empty = (
            output is None
            or (isinstance(output, dict) and not output)
            or (isinstance(output, list) and not output)
        )
        if output_empty:
            unfinished.append({
                "node_id": nid,
                "type": ntype,
                "title": ntitle,
                "status": nstatus,
                "reason": "output 为空，尚未生成内容",
                "suggested_action": "在原节点调用 node.run(node_id) 生成内容；未经用户明确要求不要删除",
            })

    return unfinished


async def node_list_unfinished(project_id: str) -> dict:
    """列出画布上所有未完成节点，供 Agent 决定是否原地修复。

    未完成 = 没出图的 image 节点 / 没写内容或没产物的 text/video/audio 节点 / 失败节点 / 中断的 running 节点。
    """
    unfinished = await _scan_unfinished_nodes(project_id)
    return {
        "ok": True,
        "unfinished_count": len(unfinished),
        "unfinished": unfinished,
        "next_action": (
            "保留已有节点。用户要求修复时在原节点重试；用户明确要求新建时可以创建独立新节点。未经用户要求不要删除。"
            if unfinished
            else "当前没有未完成节点，可以继续创建新节点。"
        ),
    }


def _client_ref_key(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("@"):
        text = text[1:]
    if text.startswith("node:"):
        text = text[5:]
    if text.startswith("client:"):
        return text[7:].strip()
    return ""


def _resolve_client_ref_value(value: Any, client_node_ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        key = _client_ref_key(value)
        if not key or key not in client_node_ids:
            return value
        raw = value.strip()
        prefix = ""
        if raw.startswith("@"):
            prefix = "@"
            raw = raw[1:]
        if raw.startswith("node:"):
            prefix += "node:"
        return f"{prefix}{client_node_ids[key]}"
    if isinstance(value, list):
        return [_resolve_client_ref_value(item, client_node_ids) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_client_ref_value(item, client_node_ids) for key, item in value.items()}
    return value


def _unresolved_client_refs(value: Any, client_node_ids: dict[str, str]) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        key = _client_ref_key(value)
        if key and key not in client_node_ids:
            refs.append(key)
    elif isinstance(value, list):
        for item in value:
            for key in _unresolved_client_refs(item, client_node_ids):
                if key not in refs:
                    refs.append(key)
    elif isinstance(value, dict):
        for item in value.values():
            for key in _unresolved_client_refs(item, client_node_ids):
                if key not in refs:
                    refs.append(key)
    return refs


def _resolve_batch_create_refs(
    fields: dict[str, Any],
    parent_node_id: str | None,
    client_node_ids: dict[str, str] | None,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    if client_node_ids is None:
        return fields, parent_node_id, None
    unresolved: list[str] = []
    resolved_fields = dict(fields)
    for key in ("depends_on", "references", "reference_images"):
        if key not in resolved_fields:
            continue
        value = resolved_fields[key]
        resolved_fields[key] = _resolve_client_ref_value(value, client_node_ids)
        for ref_key in _unresolved_client_refs(value, client_node_ids):
            if ref_key not in unresolved:
                unresolved.append(ref_key)
    resolved_parent = parent_node_id
    if resolved_parent:
        resolved_parent = str(_resolve_client_ref_value(resolved_parent, client_node_ids))
        parent_key = _client_ref_key(parent_node_id)
        if parent_key and parent_key not in client_node_ids and parent_key not in unresolved:
            unresolved.append(parent_key)
        if resolved_parent.startswith("@"):
            resolved_parent = resolved_parent[1:]
        if resolved_parent.startswith("node:"):
            resolved_parent = resolved_parent[5:]
    if unresolved:
        return fields, parent_node_id, {
            "ok": False,
            "error": "Batch create references a client_ref that has not been created yet",
            "error_kind": "unresolved_client_ref",
            "unresolved_client_refs": unresolved,
            "hint": "批量创建中只能引用同一批次前面已经创建成功的 client_ref；需要互相引用时拆成多批。",
        }
    return resolved_fields, resolved_parent, None


def _node_tool_error(result: Any) -> bool:
    return isinstance(result, dict) and (result.get("error") or result.get("ok") is False)


async def _node_create_one(
    project_id: str,
    type: str | None,
    fields: dict | None = None,
    name: str | None = None,
    prompt: str | None = None,
    parent_node_id: str | None = None,
    client_node_ids: dict[str, str] | None = None,
) -> dict:
    """创建一个画布节点。

    Args:
      type: 必须是 text / image / video / audio
      fields: 通用字段；text 正文写 fields.content；视频时长/比例/制作路径/依赖写 fields.duration_seconds/aspect_ratio/production_path/references/depends_on
      name: 短标题(可选,后端会按 type 推断)
      prompt: 图片/视频类节点的提示词
      parent_node_id: 可选,创建后自动连边到该父节点

    Returns: {id, type, title, status}
    """
    if not type:
        return {"ok": False, "error": "type is required", "error_kind": "missing_type"}
    if type not in NODE_TYPES:
        return {"error": f"未知节点类型 {type!r},允许的类型:{', '.join(NODE_TYPES)}"}

    fields = _coerce_dict(fields, "fields") or {}
    fields, parent_node_id, ref_error = _resolve_batch_create_refs(fields, parent_node_id, client_node_ids)
    if ref_error is not None:
        return ref_error
    if parent_node_id:
        parent_node_id = await _resolve_agent_node_id(project_id, parent_node_id)
        if not parent_node_id:
            return {
                "ok": False,
                "error": "parent_node_id 无法解析",
                "error_kind": "node_not_found",
                "hint": "parent_node_id 使用 node.list 返回的编号 id，或省略该字段。",
            }

    # Gate 1 + 2:模式守卫。业务流程由 video_production markdown skill 承接。
    # 后端自动推断节点归属模式,不再强制旧 node.get_creation_guide 前置。
    gate_ok, gate_err = await _check_mode_and_guide_gate(project_id, type, fields)
    if not gate_ok:
        return gate_err  # 包含详细 error_kind / required_action / hint

    # Gate 3:不做业务流程顺序判断。显式 depends_on/references 会通过连边表达依赖。
    state = await _read_project_state(project_id)
    if state.get("project_mode") != "single_node":
        ok, dep_err, missing = await _check_node_deps(project_id, type, state, fields)
        if not ok:
            return {
                "ok": False,
                "error": dep_err,
                "error_kind": "dependency_missing",
                "missing": missing,
                "target_type": type,
                "hint": "按生成顺序铁律,先 node.create + node.run 完成缺失类型再回来,不要换工具绕过、不要重复同一调用。",
            }

    if name and "name" not in fields:
        fields["name"] = name
    fields = _apply_defaults(type, fields)
    create_prompt = str(prompt or fields.get("prompt") or "").strip()
    if create_prompt:
        fields["prompt"] = create_prompt
        prompt = create_prompt

    if type == "image":
        resolution_error = _validate_image_resolution_fields(fields)
        if resolution_error is not None:
            return resolution_error

    title_builder = _TITLE_BUILDERS.get(type, lambda f: name or type)
    title = title_builder(fields)

    surface = _surface_for_project_mode(state.get("project_mode"))
    model_config = {
        "surface": surface,
        "project_mode_at_create": state.get("project_mode"),
        "project_sub_mode_at_create": state.get("project_sub_mode"),
    }

    node = await canvas_tools.create_node(
        project_id=project_id,
        node_type=type,
        title=title,
        input_data=fields,
        model_config=model_config,
        prompt=prompt,
    )
    node["surface"] = surface
    node["project_mode"] = state.get("project_mode")
    if _node_content_needs_review(type, fields) and not _prompt_review_passed(fields, state):
        node.update(_prompt_review_required_payload(node["id"], type, fields, phase="after_node_create"))
    try:
        from app.agent.orchestrator import emit_canvas_event
        await emit_canvas_event(
            {"type": "canvas_action", "action": "create_node", "payload": node},
            project_id=project_id,
        )
    except Exception:
        logger.exception("emit node.create canvas event failed")

    if parent_node_id:
        try:
            edge = await canvas_tools.connect_nodes(
                project_id=project_id,
                source_node_id=parent_node_id,
                target_node_id=node["id"],
            )
            await _emit_edge_created(project_id, edge)
        except Exception as exc:
            node["edge_warning"] = f"连边失败:{exc}"

    # 自动建拓扑连线只使用模型显式写入的 references/depends_on/reference_images。
    # 后端不根据业务类型推导制作链路。
    try:
        await _auto_connect_topology(project_id, node["id"], type, fields)
    except Exception as exc:
        node.setdefault("edge_warning", f"自动连边失败:{exc}")

    return await _model_visible_node(node, project_id)


async def node_create(
    project_id: str,
    type: str | None = None,
    fields: dict | None = None,
    name: str | None = None,
    prompt: str | None = None,
    parent_node_id: str | None = None,
    nodes: list[dict] | None = None,
) -> dict:
    """创建一个或多个画布节点。

    单节点继续使用 type/fields；少量搭框架或低风险节点可用 nodes 批量创建。
    """
    if nodes is None:
        return await _node_create_one(
            project_id=project_id,
            type=type,
            fields=fields,
            name=name,
            prompt=prompt,
            parent_node_id=parent_node_id,
        )

    if not isinstance(nodes, list) or not nodes:
        return {
            "ok": False,
            "error": "nodes must be a non-empty array",
            "error_kind": "invalid_nodes",
            "hint": "单节点创建用 type/fields；批量创建用 nodes=[{type, fields, parent_node_id?}]。",
        }

    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    client_node_ids: dict[str, str] = {}
    client_public_node_ids: dict[str, str] = {}
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            errors.append({
                "index": index,
                "ok": False,
                "error": "Batch node item must be an object",
                "error_kind": "invalid_node_item",
            })
            continue
        client_ref = str(item.get("client_ref") or "").strip()
        if client_ref and client_ref in client_node_ids:
            errors.append({
                "index": index,
                "client_ref": client_ref,
                "ok": False,
                "error": "Duplicate client_ref in batch",
                "error_kind": "duplicate_client_ref",
            })
            continue
        result = await _node_create_one(
            project_id=project_id,
            type=item.get("type") or type,
            fields=item.get("fields"),
            name=item.get("name"),
            prompt=item.get("prompt"),
            parent_node_id=item.get("parent_node_id") or parent_node_id,
            client_node_ids=client_node_ids,
        )
        if _node_tool_error(result):
            error = dict(result)
            error["index"] = index
            if client_ref:
                error["client_ref"] = client_ref
            errors.append(error)
            continue
        node = dict(result)
        node["index"] = index
        if client_ref:
            node["client_ref"] = client_ref
            internal_node_id = str(node.get("_canvas_id") or node.get("_canvas_node_id") or "")
            if internal_node_id:
                client_node_ids[client_ref] = internal_node_id
                client_public_node_ids[client_ref] = str(node.get("id") or "")
        created.append(node)

    if not created:
        return {
            "ok": False,
            "status": "failed",
            "error": "No nodes were created",
            "error_kind": "batch_create_failed",
            "project_id": project_id,
            "requested": len(nodes),
            "created_count": 0,
            "failed_count": len(errors),
            "errors": errors,
            "hint": "检查每个 nodes[i].type、fields 和 client_ref 依赖；修正后分批重试。",
        }
    return {
        "ok": True,
        "status": "partial" if errors else "ok",
        "project_id": project_id,
        "requested": len(nodes),
        "created_count": len(created),
        "failed_count": len(errors),
        "nodes": created,
        "errors": errors,
        "client_node_ids": client_public_node_ids,
        "next_action": "需要生成产物时按依赖顺序调用 node.run；需要修字段时批量或单个调用 node.update。",
    }


async def _auto_connect_topology(project_id: str, node_id: str, node_type: str, fields: dict) -> None:
    """Create explicit dependency edges from model-authored reference fields."""
    nodes = await canvas_tools.list_nodes(project_id)
    if not isinstance(nodes, list):
        return
    node_by_id = {str(n.get("id")): n for n in nodes if isinstance(n, dict) and n.get("id")}
    public_to_internal: dict[str, str] = {}
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        public_id = public_node_id_from_dict(n)
        if public_id:
            public_to_internal[public_id] = str(n.get("id"))
            public_to_internal[f"#{public_id}"] = str(n.get("id"))

    async def _link(src: str | None) -> None:
        if not src or src == node_id:
            return
        try:
            edge = await canvas_tools.connect_nodes(
                project_id=project_id, source_node_id=src, target_node_id=node_id,
            )
            await _emit_edge_created(project_id, edge)
        except Exception:
            pass

    raw_refs: list[Any] = []
    for key in ("depends_on", "references", "reference_images"):
        value = fields.get(key)
        if isinstance(value, list):
            raw_refs.extend(value)
        elif value:
            raw_refs.append(value)
    for raw in raw_refs:
        ref = _reference_candidate(raw)
        text = str(ref or "").strip()
        if text.startswith("@"):
            text = text[1:]
        if text.startswith("node:"):
            text = text[5:]
        if text.startswith("#"):
            text = text[1:]
        if "/" in text or text.startswith(("asset:", "upload:", "http://", "https://")):
            continue
        src = text if text in node_by_id else public_to_internal.get(text)
        if src:
            await _link(src)


async def _emit_edge_created(project_id: str, edge: dict | None) -> None:
    """Emit a canvas edge event for backend-created topology edges."""
    if not isinstance(edge, dict) or not edge.get("id"):
        return
    try:
        from app.agent.orchestrator import emit_canvas_event
        await emit_canvas_event(
            {"type": "canvas_action", "action": "add_edge", "payload": edge},
            project_id=project_id,
        )
    except Exception:
        pass


def _normalize_node_id_list(node_id: str | None = "", node_ids: list[str] | str | None = None) -> list[str]:
    raw_items: list[Any] = []
    if isinstance(node_ids, str):
        text = node_ids.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                raw_items.extend(parsed)
            elif text:
                raw_items.append(text)
        elif text:
            raw_items.extend(part for part in re.split(r"[\s,]+", text) if part)
    elif isinstance(node_ids, list):
        raw_items.extend(node_ids)
    elif node_ids:
        raw_items.append(node_ids)
    if node_id:
        raw_items.insert(0, node_id)

    normalized: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        if text.startswith("@"):
            text = text[1:]
        if text.startswith("node:"):
            text = text[5:]
        if text.startswith("#"):
            text = text[1:]
        if text and text not in normalized:
            normalized.append(text)
    return normalized


async def _node_get_one(node_id: str, project_id: str = "") -> dict:
    resolved_node_id = await _resolve_agent_node_id(project_id, node_id)
    if not resolved_node_id:
        return {
            "ok": False,
            "error": "Current project context is missing; backend could not resolve the node number",
            "error_kind": "missing_project_context",
            "node_id": node_id,
            "hint": "节点编号由后端按当前项目自动解析；请检查 chat stream 是否注入了当前项目上下文。",
        }
    node = await canvas_tools.get_node(resolved_node_id)
    if isinstance(node, dict) and node.get("error") == "Node not found":
        return {
            "ok": False,
            "error": "Node not found",
            "error_kind": "node_not_found",
            "node_id": node_id,
            "hint": "node_id 使用 node.list/node.create 返回的节点编号；shot_id、segment_id、标题或别名需要先通过 node.list/node.get 转成节点编号。新任务先创建合适的 text/image/video/audio 节点。",
        }
    if project_id and isinstance(node, dict) and str(node.get("project_id") or "") != project_id:
        return {
            "ok": False,
            "error": "Node does not belong to this project",
            "error_kind": "node_project_mismatch",
            "node_id": node_id,
            "project_id": project_id,
        }
    return await _model_visible_node(node, project_id or str(node.get("project_id") or ""))


async def node_get(
    node_id: str = "",
    project_id: str = "",
    node_ids: list[str] | str | None = None,
    query: str | None = None,
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    limit: int | None = NODE_LIST_DEFAULT_LIMIT,
) -> dict:
    ids = _normalize_node_id_list(node_id, node_ids)
    if not ids and (query or regex or pattern):
        return await _node_get_by_query(
            project_id=project_id,
            query=query,
            regex=regex,
            pattern=pattern,
            case_sensitive=case_sensitive,
            limit=limit,
        )
    if not ids:
        return {
            "ok": False,
            "error": "node_id/node_ids or query/regex is required",
            "error_kind": "missing_node_id",
            "hint": "先用 node.list(query=... 或 regex=...) 获取候选节点编号；需要多个详情时一次传 node_ids。",
        }
    if node_ids is None and len(ids) == 1:
        result = await _node_get_one(ids[0], project_id=project_id)
        if (
            isinstance(result, dict)
            and result.get("error_kind") == "node_not_found"
            and project_id
        ):
            candidates = await _node_query_candidates(
                project_id=project_id,
                query=ids[0],
                regex=None,
                pattern=None,
                case_sensitive=case_sensitive,
                limit=8,
            )
            if candidates.get("nodes"):
                result["candidates"] = candidates.get("nodes")
                result["hint"] = "未找到精确 node_id。下面是模糊候选；请选候选 id 后重新调用 node.get/node.run。"
        return result

    nodes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item_id in ids:
        result = await _node_get_one(item_id, project_id=project_id)
        if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
            errors.append(result)
        elif isinstance(result, dict):
            nodes.append(result)

    if not nodes:
        return {
            "ok": False,
            "error": "No nodes found",
            "error_kind": "node_not_found",
            "project_id": project_id,
            "requested": len(ids),
            "returned": 0,
            "errors": errors,
            "hint": "node_ids 必须来自 node.list 返回的节点编号；如果只记得标题或描述，用 node.get(query=...) 或 node.list(regex=...) 先找候选。",
        }
    return {
        "ok": True,
        "status": "partial" if errors else "ok",
        "project_id": project_id,
        "requested": len(ids),
        "returned": len(nodes),
        "nodes": nodes,
        "errors": errors,
    }


async def _node_query_candidates(
    *,
    project_id: str,
    query: str | None,
    regex: str | list[str] | None,
    pattern: str | list[str] | None,
    case_sensitive: bool,
    limit: int | None,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    if not project_id:
        return {
            "ok": False,
            "error": "Current project context is missing; backend could not search project nodes",
            "error_kind": "missing_project_context",
            "hint": "节点搜索由后端在当前项目内执行；请检查 chat stream 是否注入了当前项目上下文。",
        }
    nodes = await canvas_tools.list_nodes(project_id)
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for node in nodes:
        match = match_text(
            _node_search_blob(node),
            query=query,
            regex=regex,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )
        if match.get("matched"):
            matches.append((node, match))

    parsed_limit = _parse_node_list_limit(limit)
    limit_int: int | None = None
    if parsed_limit > 0:
        limit_int = min(parsed_limit, NODE_LIST_MAX_LIMIT)

    total = len(matches)
    limited = matches[:limit_int] if limit_int is not None else matches
    return {
        "ok": True,
        "project_id": project_id,
        "mode": "query",
        "total": total,
        "returned": len(limited),
        "truncated": len(limited) < total,
        "nodes": [
            _node_list_index_item(node, match_hint=True, match_info=match)
            for node, match in limited
        ],
        "filters": {
            "query": query,
            "regex": regex,
            "pattern": pattern,
            "case_sensitive": case_sensitive,
            "limit": limit_int,
            "unlimited": limit_int is None,
        },
    }


async def _node_get_by_query(
    *,
    project_id: str,
    query: str | None,
    regex: str | list[str] | None,
    pattern: str | list[str] | None,
    case_sensitive: bool,
    limit: int | None,
) -> dict[str, Any]:
    candidates = await _node_query_candidates(
        project_id=project_id,
        query=query,
        regex=regex,
        pattern=pattern,
        case_sensitive=case_sensitive,
        limit=limit,
    )
    if not candidates.get("ok"):
        return candidates
    ids = [str(item.get("id") or item.get("node_id") or "") for item in candidates.get("nodes") or []]
    ids = [item_id for item_id in ids if item_id]
    if not ids:
        return {
            "ok": False,
            "error": "No nodes matched query",
            "error_kind": "node_not_found",
            "project_id": project_id,
            "filters": candidates.get("filters") or {},
            "hint": "换更明确的 query，或传 regex 列出候选节点。",
        }
    nodes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item_id in ids:
        result = await _node_get_one(item_id, project_id=project_id)
        if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
            errors.append(result)
        elif isinstance(result, dict):
            nodes.append(result)
    return {
        "ok": bool(nodes),
        "status": "partial" if errors else "ok",
        "mode": "query",
        "project_id": project_id,
        "total": candidates.get("total", len(nodes)),
        "returned": len(nodes),
        "truncated": bool(candidates.get("truncated")),
        "nodes": nodes,
        "candidate_index": candidates.get("nodes") or [],
        "errors": errors,
        "filters": candidates.get("filters") or {},
    }


# ───────────────────────────────────────────────────────────────────────
# node.check_readiness —— 失败修复入口:扫节点信息完整度,逐项 true/false
# ───────────────────────────────────────────────────────────────────────

def _field_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _stage_has_image_url(stage: dict) -> bool:
    return any(
        _field_filled(stage.get(k))
        for k in ("url", "local_url", "remote_url", "composite_url", "thumbnail_url", "poster", "last_frame_url")
    )


def _completed_image_url_from_output(output: Any, *, include_direct: bool = True) -> str:
    """Return a completed image URL from direct or fusion output, if present."""
    if isinstance(output, str) and output.strip():
        try:
            output = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            output = None
    if not isinstance(output, dict):
        return ""
    if include_direct:
        for key in ("url", "local_url", "remote_url", "composite_url", "thumbnail_url", "poster", "last_frame_url"):
            value = output.get(key)
            if _field_filled(value):
                return str(value)
    stages = output.get("stages")
    if isinstance(stages, list):
        for stage in reversed(stages):
            if not isinstance(stage, dict):
                continue
            if stage.get("status") != "completed" or not _stage_has_image_url(stage):
                continue
            for key in ("url", "local_url", "remote_url", "composite_url", "thumbnail_url", "poster", "last_frame_url"):
                value = stage.get(key)
                if _field_filled(value):
                    return str(value)
    return ""


async def node_check_readiness(node_id: str) -> dict:
    """扫节点的"信息完整度",返回逐项 true/false 清单。失败修复第一步。

    判定规则:
    - input 里 _NODE_FIELD_SCHEMA[type].required 每个字段非空 → field 项 ok
    - image 节点:output 里有 url/local_url/remote_url 或 fusion 图片阶段 completed
    - text/video 节点:output 非空 + 非 {} → output 项 ok
    - depends_on 在 single_node 模式下返回 [],提示语去掉递归部分

    LLM 看到 ready=False → 按 missing 补字段或 force run;有 depends_on 先递归往上验。
    """
    node = await canvas_tools.get_node(node_id)
    if not isinstance(node, dict) or node.get("error"):
        return {"ok": False, "error": node.get("error") if isinstance(node, dict) else "节点不存在"}

    node_type = node.get("type")
    if node_type not in NODE_TYPES:
        return {
            "ok": False,
            "error": f"未知节点类型 {node_type!r}",
            "node_id": node_id,
        }

    project_id = node.get("project_id") or ""
    state = await _read_project_state(project_id) if project_id else {}
    project_mode = state.get("project_mode")

    schema = _NODE_FIELD_SCHEMA.get(node_type, {})
    required_fields: list[str] = list(schema.get("required") or [])
    fields = node.get("input") or {}

    checklist: list[dict] = []
    missing: list[str] = []

    for f in required_fields:
        ok = _field_filled(fields.get(f))
        item = {"item": f, "ok": ok, "kind": "field"}
        if not ok:
            item["reason"] = "字段为空"
            missing.append(f)
        checklist.append(item)

    output = node.get("output")
    is_fusion_type = node_type in _SUBJECT_BY_TYPE

    if is_fusion_type:
        if isinstance(output, dict) and any(output.get(k) for k in ("url", "local_url", "remote_url")):
            checklist.append({"item": "图片", "ok": True, "kind": "output"})
        else:
            stages: list[dict] = []
            if isinstance(output, dict) and output.get("type") == "fusion":
                raw_stages = output.get("stages")
                if isinstance(raw_stages, list):
                    stages = [s for s in raw_stages if isinstance(s, dict)]
            by_name = {s.get("name"): s for s in stages if s.get("name")}
            s = by_name.get("图片")
            if not s or s.get("status") != "completed" or not _stage_has_image_url(s):
                checklist.append({
                    "item": "图片", "ok": False, "kind": "output",
                    "reason": "缺少已完成图片 URL",
                })
                missing.append("图片")
            else:
                checklist.append({"item": "图片", "ok": True, "kind": "stage"})
    else:
        # 文本类节点:output 非空就算 ok
        ok = isinstance(output, (dict, list)) and bool(output)
        item = {"item": "output", "ok": ok, "kind": "output"}
        if not ok:
            item["reason"] = "output 为空,需要 node.run 生成内容"
            missing.append("output")
        checklist.append(item)

    # 依赖列表:single_node 模式不要求依赖
    if project_mode == "single_node":
        depends_on: list[str] = []
    else:
        depends_on = list(_NODE_DEPENDENCIES.get(node_type, []))

    ready = not missing

    if ready:
        hint = "本节点信息齐全。如果之前是失败状态,可直接 node.run(force) 重跑;否则无需操作。"
    elif depends_on:
        hint = (
            f"本节点缺 {missing}。先对每个上游 type {depends_on} 调 node.list 找节点,"
            f"再 node.get 逐个看 status/output/stages;上游完成后再回头补本节点字段并 node.run force。"
        )
    else:
        hint = (
            f"本节点缺 {missing}。"
            f"field 类直接 node.update 补字段;stage 类直接 node.run(action='force') 触发生成。"
        )

    return {
        "ok": True,
        "node_id": node_id,
        "type": node_type,
        "status": node.get("status"),
        "ready": ready,
        "checklist": checklist,
        "missing": missing,
        "depends_on": depends_on,
        "project_mode": project_mode,
        "next_action_hint": hint,
    }


def _normalize_node_update_patch(node: dict, patch: dict) -> tuple[dict, dict | None]:
    """Accept common Agent field patch shapes and turn them into DB-safe keys."""
    next_patch = dict(patch)
    input_delta: dict[str, Any] = {}

    for key in ("fields", "input", "input_json", "input_data"):
        if key not in next_patch:
            continue
        raw_value = next_patch.pop(key)
        if raw_value in (None, "", [], {}):
            continue
        value = raw_value
        if isinstance(raw_value, str):
            value = _coerce_dict(raw_value, key)
        if not isinstance(value, dict):
            return {}, {
                "ok": False,
                "error": f"patch.{key} 必须是 JSON 对象，用于局部更新节点 fields/input。",
                "error_kind": "invalid_patch_shape",
                "hint": (
                    "修改节点字段时写 patch.input_json 或 patch.fields，例如 "
                    "{\"input_json\":{\"resolution\":\"1080x1920\"}}。"
                ),
            }
        input_delta.update(value)

    for key in list(next_patch.keys()):
        if key in _DIRECT_INPUT_PATCH_KEYS:
            input_delta[key] = next_patch.pop(key)

    if input_delta:
        current_input = node.get("input") if isinstance(node.get("input"), dict) else {}
        merged_input = dict(current_input)
        merged_input.update(input_delta)
        next_patch["input_json"] = merged_input
        if "title" in input_delta and "title" not in next_patch:
            next_patch["title"] = input_delta.get("title")
        if "prompt" in input_delta and "prompt" not in next_patch:
            next_patch["prompt"] = input_delta.get("prompt")

    return next_patch, None


def _patch_repairs_failed_node(node: dict, patch: dict) -> bool:
    if str(node.get("status") or "") != "failed":
        return False
    if "status" in patch:
        return False
    return any(key in patch for key in ("prompt", "input_json", "input_data"))


async def _node_update_one(node_id: str, patch: dict | str | None, project_id: str = "") -> dict:
    """局部修改节点。

    - 通用字段(title / status / position / prompt 等)直接落 WorkflowNode 表
    - 蓝图绑定字段先生成 blueprint revision；非蓝图节点只做通用字段 patch
    """
    if not node_id:
        return {"ok": False, "error": "node_id is required", "error_kind": "missing_node_id"}
    if patch is None:
        return {"ok": False, "error": "patch is required", "error_kind": "missing_patch"}
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except (json.JSONDecodeError, TypeError):
            return {"error": "patch 必须是 JSON 对象"}
    if not isinstance(patch, dict):
        return {"error": "patch 必须是 dict"}

    resolved_node_id = await _resolve_agent_node_id(project_id, node_id)
    if not resolved_node_id:
        return {
            "ok": False,
            "error": "Current project context is missing; backend could not resolve the node number",
            "error_kind": "missing_project_context",
            "node_id": node_id,
            "hint": "节点编号由后端按当前项目自动解析；请检查 chat stream 是否注入了当前项目上下文。",
        }
    node = await canvas_tools.get_node(resolved_node_id)
    if node.get("error"):
        return node
    node_id = resolved_node_id
    project_id = project_id or str(node.get("project_id") or "")

    # Snapshot old values before modification for diff
    _old_input = dict(node.get("input") or {})
    _old_prompt = str(node.get("prompt") or "")
    _old_title = str(node.get("title") or "")

    patch, patch_error = _normalize_node_update_patch(node, patch)
    if patch_error is not None:
        return patch_error

    revision_result = await create_pending_revision_from_node_patch(node=node, patch=patch)
    if revision_result is not None:
        return revision_result

    # 通用字段落画布；业务含义由模型写入 fields/content/prompt，不在后端派发。
    canvas_patch = dict(patch)
    image_render_marked_stale = False
    text_workflow_marked_stale = False
    if canvas_patch:
        input_field_patch_requested = "input_json" in canvas_patch or "input_data" in canvas_patch
        canvas_patch = _merge_input_patch_with_current(node, canvas_patch)
        canvas_patch = _sync_title_patch_with_input(node, canvas_patch)
        canvas_patch = _sync_prompt_patch_with_input(node, canvas_patch)
        if _patch_repairs_failed_node(node, canvas_patch):
            canvas_patch["status"] = "idle"
            canvas_patch["error_message"] = None
        if node.get("type") == "image":
            next_input = canvas_patch.get("input_json")
            if not isinstance(next_input, dict):
                next_input = canvas_patch.get("input_data")
            if isinstance(next_input, dict):
                next_prompt = str(canvas_patch.get("prompt") if "prompt" in canvas_patch else node.get("prompt") or "")
                if _image_render_inputs_changed(_old_input, next_input, _old_prompt, next_prompt):
                    if _image_node_has_rendered_output(node):
                        canvas_patch["input_json"] = _with_image_render_state(next_input, "stale")
                        image_render_marked_stale = True
                    else:
                        clean_input = dict(next_input)
                        clean_input.pop("render_state", None)
                        canvas_patch["input_json"] = clean_input
                    canvas_patch.pop("input_data", None)
        if node.get("type") == "text":
            next_input = canvas_patch.get("input_json")
            if not isinstance(next_input, dict):
                next_input = canvas_patch.get("input_data")
            if isinstance(next_input, dict):
                next_prompt = str(canvas_patch.get("prompt") if "prompt" in canvas_patch else node.get("prompt") or "")
                if _workflow_text_prompt_contract_changed(_old_input, next_input, _old_prompt, next_prompt):
                    canvas_patch["input_json"] = _with_workflow_text_stale(next_input)
                    canvas_patch.pop("input_data", None)
                    text_workflow_marked_stale = True
        if (
            node.get("type") == "image"
            and input_field_patch_requested
            and isinstance(canvas_patch.get("input_json"), dict)
        ):
            resolution_error = _validate_image_resolution_fields(
                canvas_patch["input_json"],
                node_id=node_id,
            )
            if resolution_error is not None:
                return resolution_error
    canvas_result = await canvas_tools.update_node(node_id, canvas_patch) if canvas_patch else {"id": node_id}
    if canvas_patch:
        for key in ("prompt", "input_json"):
            if key in canvas_patch and key not in canvas_result:
                canvas_result[key] = canvas_patch[key]
        if "input_json" in canvas_patch:
            canvas_result["input"] = canvas_patch["input_json"]
            canvas_result["input_json"] = canvas_patch["input_json"]
        output_patch = canvas_patch.get("output_json", canvas_patch.get("output_data"))
        if output_patch is not None:
            canvas_result["output"] = output_patch
            canvas_result["output_json"] = output_patch
    canvas_result.setdefault("type", node.get("type"))
    canvas_result.setdefault("project_id", node.get("project_id"))
    canvas_result.setdefault("surface", _node_surface(node))
    if "input" not in canvas_result and isinstance(node.get("input"), dict):
        canvas_result["input"] = dict(node.get("input") or {})
        canvas_result["input_json"] = canvas_result["input"]
    result_input = canvas_result.get("input") if isinstance(canvas_result.get("input"), dict) else {}
    if isinstance(result_input, dict) and str(canvas_result.get("project_id") or ""):
        try:
            edge_sync = await canvas_tools.sync_dependency_edges(
                str(canvas_result.get("project_id")),
                node_id,
                result_input,
            )
            if isinstance(edge_sync, dict) and edge_sync.get("changed"):
                canvas_result["edge_sync"] = edge_sync
        except Exception as exc:
            logger.exception("node.update dependency edge sync failed")
            canvas_result["edge_sync_warning"] = str(exc)[:200]
    if (
        canvas_result.get("type") in NODE_TYPES
        and _node_content_needs_review(str(canvas_result.get("type") or ""), result_input)
        and not _prompt_review_passed(result_input)
    ):
        canvas_result.update(
            _prompt_review_required_payload(
                node_id,
                str(canvas_result.get("type") or node.get("type") or ""),
                result_input,
                phase="after_node_update",
            )
        )

    # Build change diff for frontend display
    _changes: list[dict] = []
    _new_input = dict(canvas_result.get("input_json") or canvas_result.get("input") or {})
    _new_prompt = str(canvas_result.get("prompt") or "")
    _new_title = str(canvas_result.get("title") or "")

    # Check input_json field changes (skip fields tracked separately)
    _skip_keys = {"prompt", "title"}
    _all_keys = set(list(_old_input.keys()) + list(_new_input.keys()))
    for _k in sorted(_all_keys):
        if _k in _skip_keys:
            continue
        _ov = _old_input.get(_k)
        _nv = _new_input.get(_k)
        if str(_ov) != str(_nv):
            _changes.append({"field": _k, "label": _k, "before": str(_ov)[:500], "after": str(_nv)[:500]})

    # Check prompt change
    if _old_prompt != _new_prompt:
        _changes.append({"field": "prompt", "label": "提示词", "before": _old_prompt[:800], "after": _new_prompt[:800]})

    # Check title change
    if _old_title != _new_title:
        _changes.append({"field": "title", "label": "标题", "before": _old_title, "after": _new_title})

    # Deduplicate by field name
    _seen = set()
    _deduped = []
    for _c in _changes:
        if _c["field"] not in _seen:
            _seen.add(_c["field"])
            _deduped.append(_c)
    if _deduped:
        canvas_result["changes"] = _deduped
    if image_render_marked_stale:
        canvas_result["render_state"] = "stale"
        canvas_result["requires_rerun"] = True
        canvas_result["hint"] = (
            "图片节点提示词或生成参数已更新，当前图片仍是旧产物；"
            "请继续对这个节点调用 node.run(action='render') 重新生成。生成完成后 render_state 会变为 fresh。"
        )
    if text_workflow_marked_stale:
        canvas_result["requires_rerun"] = True
        canvas_result["hint"] = (
            "文本工作流节点提示词合同已更新，当前正文仍是旧产物；"
            "请继续对这个节点调用 node.run 重新生成。生成完成后 workflow.stale 会变为 false。"
        )

    return await _model_visible_node(canvas_result, project_id)


async def node_update(
    node_id: str = "",
    project_id: str = "",
    patch: dict | str | None = None,
    updates: list[dict] | None = None,
    node_ids: list[str] | str | None = None,
) -> dict:
    """局部修改一个或多个节点。"""
    if updates is None and node_ids is not None:
        ids = _normalize_node_id_list(node_id, node_ids)
        updates = [{"node_id": item_id, "patch": patch} for item_id in ids]

    if updates is None:
        return await _node_update_one(node_id, patch, project_id=project_id)

    if not isinstance(updates, list) or not updates:
        return {
            "ok": False,
            "error": "updates must be a non-empty array",
            "error_kind": "invalid_updates",
            "hint": "单节点更新用 node_id/patch；批量更新用 updates=[{node_id, patch}]。",
        }

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(updates):
        if not isinstance(item, dict):
            errors.append({
                "index": index,
                "ok": False,
                "error": "Batch update item must be an object",
                "error_kind": "invalid_update_item",
            })
            continue
        item_node_id = str(item.get("node_id") or "").strip()
        item_patch = item.get("patch")
        result = await _node_update_one(item_node_id, item_patch, project_id=project_id)
        if _node_tool_error(result):
            error = dict(result)
            error["index"] = index
            if item_node_id:
                error["node_id"] = item_node_id
            errors.append(error)
            continue
        updated = dict(result)
        updated["index"] = index
        updated.setdefault("node_id", updated.get("id") or item_node_id)
        results.append(updated)

    if not results:
        return {
            "ok": False,
            "status": "failed",
            "error": "No nodes were updated",
            "error_kind": "batch_update_failed",
            "requested": len(updates),
            "updated_count": 0,
            "failed_count": len(errors),
            "errors": errors,
            "hint": "检查每个 updates[i].node_id 是否来自 node.list，以及 patch 是否符合节点字段 schema。",
        }
    return {
        "ok": True,
        "status": "partial" if errors else "ok",
        "requested": len(updates),
        "updated_count": len(results),
        "failed_count": len(errors),
        "results": results,
        "errors": errors,
        "next_action": "需要重新生成时，对已更新节点按依赖顺序调用 node.run(action='force')。",
    }


async def node_delete(node_id: str, cascade: bool = True, project_id: str = "") -> dict:
    """删除通用节点。cascade 参数仅保留为工具入参，不触发业务类型级联。"""
    resolved_node_id = await _resolve_agent_node_id(project_id, node_id)
    if not resolved_node_id:
        return {
            "ok": False,
            "error": "Current project context is missing; backend could not resolve the node number",
            "error_kind": "missing_project_context",
            "node_id": node_id,
            "hint": "节点编号由后端按当前项目自动解析；请检查 chat stream 是否注入了当前项目上下文。",
        }
    node = await canvas_tools.get_node(resolved_node_id)
    if node.get("error"):
        return node

    return await canvas_tools.delete_node(resolved_node_id)


def _node_search_blob(node: dict[str, Any]) -> str:
    return search_blob(
        node.get("id"),
        public_node_id_from_dict(node),
        f"#{public_node_id_from_dict(node)}",
        node.get("title"),
        node.get("type"),
        node.get("status"),
        node.get("prompt"),
        node.get("error_message"),
        node.get("input"),
        node.get("output"),
    )


def _node_list_index_item(
    node: dict[str, Any],
    *,
    match_hint: bool = False,
    match_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_text = str(node.get("prompt") or "")
    public_id = public_node_id_from_dict(node)
    item: dict[str, Any] = {
        "id": public_id,
        "node_id": public_id,
        "type": node.get("type"),
        "title": node.get("title"),
        "status": node.get("status"),
        "prompt_preview": prompt_text[:20],
    }
    for key in (
        "surface",
        "render_state",
        "output_summary",
        "error_message",
        "version",
        "supersedes_id",
        "links",
        "workflow",
        "created_at",
        "updated_at",
    ):
        value = node.get(key)
        if value not in (None, "", [], {}):
            item[key] = value
    if prompt_text:
        item["prompt_chars"] = len(prompt_text)
    if match_info:
        item["match"] = {
            key: value
            for key, value in match_info.items()
            if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
        }
    if match_hint:
        item["match_hint"] = "这是 query 匹配到的候选节点；后续 node.get/node.run 使用 id 字段。"
    return item


def _parse_node_list_limit(limit: int | str | None) -> int:
    try:
        if limit in (0, "0"):
            return 0
        if limit in (None, ""):
            return NODE_LIST_DEFAULT_LIMIT
        return int(limit)
    except (TypeError, ValueError):
        return NODE_LIST_DEFAULT_LIMIT


async def node_list(
    project_id: str,
    type: str | None = None,
    status: str | None = None,
    surface: str | None = None,
    query: str | None = None,
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    limit: int | None = NODE_LIST_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """列出项目节点索引；默认截断，可用 limit=0 明确读取全部。

    query/regex 用于用户说“那张图/某标题/某描述”时先找候选节点，不能把
    查询文本当 node_id 直接传给 node.get/node.run。
    """
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    nodes = await canvas_tools.list_nodes(project_id)
    if not surface:
        nodes = [n for n in nodes if _node_surface(n) != NODE_SURFACE_WORKFLOW_RUNTIME]
    if type:
        nodes = [n for n in nodes if n.get("type") == type]
    if status:
        nodes = [n for n in nodes if n.get("status") == status]
    if surface:
        nodes = [n for n in nodes if _node_surface(n) == surface]
    match_by_id: dict[str, dict[str, Any]] = {}
    if query or regex or pattern:
        filtered: list[dict[str, Any]] = []
        for node in nodes:
            match = match_text(
                _node_search_blob(node),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            if not match.get("matched"):
                continue
            filtered.append(node)
            node_key = str(node.get("id") or "")
            if node_key:
                match_by_id[node_key] = match
        nodes = filtered
    limit_int: int | None = None
    parsed_limit = _parse_node_list_limit(limit)
    if parsed_limit > 0:
        limit_int = min(parsed_limit, NODE_LIST_MAX_LIMIT)
    total = len(nodes)
    if limit_int is not None:
        nodes = nodes[:limit_int]
    has_query = bool(query or regex or pattern)
    index_nodes = [
        _node_list_index_item(
            node,
            match_hint=has_query,
            match_info=match_by_id.get(str(node.get("id") or "")),
        )
        for node in nodes
    ]
    return {
        "ok": True,
        "project_id": project_id,
        "nodes": index_nodes,
        "total": total,
        "returned": len(index_nodes),
        "truncated": len(index_nodes) < total,
        "next_action": (
            "节点列表已截断；需要完整索引时调用 node.list(limit=0)，需要详情时批量调用 node.get(node_ids=[...])。"
            if len(index_nodes) < total
            else "需要节点详情时批量调用 node.get(node_ids=[...])。"
        ),
        "filters": {
            "type": type,
            "status": status,
            "surface": surface,
            "query": query,
            "regex": regex,
            "pattern": pattern,
            "case_sensitive": case_sensitive,
            "limit": limit_int,
            "unlimited": limit_int is None,
        },
    }


# ───────────────────────────────────────────────────────────────────────
# node.run — fat dispatcher,按 type 路由到具体实现
# ───────────────────────────────────────────────────────────────────────

NodeRunner = Callable[[str, str, dict], Awaitable[dict]]


def _visual_prompt_from_fields(f: dict, *, include_prompt: bool = True) -> str:
    """Return the model/user supplied visual prompt, accepting legacy aliases.

    The agent often writes `image_prompt` or `visual_prompt` from a guide. Render
    must treat those as first-class prompt text instead of generating a generic
    fallback and overwriting the user's intended visual.
    """
    keys = ["image_prompt", "visual_prompt"]
    if include_prompt:
        keys.insert(0, "prompt")
    for key in keys:
        value = str(f.get(key) or "").strip()
        if value:
            return value
    return ""


_IMAGE_CLARITY_PROMPT_HINTS = {
    "natural": "画面自然清晰，保留真实质感，避免过度锐化",
    "detailed": "主体细节清楚，边缘稳定，材质纹理丰富，面部和手部结构清晰",
    "sharp": "画面锐利，高对比细节清楚，主体边缘干净，避免模糊和糊脸",
    "自然": "画面自然清晰，保留真实质感，避免过度锐化",
    "细节": "主体细节清楚，边缘稳定，材质纹理丰富，面部和手部结构清晰",
    "锐利": "画面锐利，高对比细节清楚，主体边缘干净，避免模糊和糊脸",
}


def _image_prompt_with_render_modifiers(base_prompt: str, f: dict) -> str:
    prompt = str(base_prompt or "").strip()
    if not prompt:
        return ""
    clarity = str(f.get("clarity") or "").strip()
    clarity_hint = _IMAGE_CLARITY_PROMPT_HINTS.get(clarity, clarity)
    if clarity_hint and clarity_hint not in prompt:
        prompt = f"{prompt}\n\n清晰度要求：{clarity_hint}"
    return prompt


def _sync_prompt_patch_with_input(node: dict, patch: dict) -> dict:
    """Keep node.prompt and input_json.prompt in lockstep for Agent edits."""
    if "prompt" not in patch:
        return patch
    prompt = str(patch.get("prompt") or "").strip()
    next_patch = dict(patch)
    current_input = node.get("input") if isinstance(node.get("input"), dict) else {}
    patch_input = next_patch.get("input_json")
    if not isinstance(patch_input, dict):
        patch_input = next_patch.get("input_data")
    next_input = dict(current_input)
    if isinstance(patch_input, dict):
        next_input.update(patch_input)
    if prompt:
        next_input["prompt"] = prompt
        next_input["prompt_preview"] = prompt[:1200]
        workflow = next_input.get("workflow")
        if (
            node.get("type") == "text"
            and isinstance(workflow, dict)
            and str(workflow.get("prompt_template") or "").strip()
        ):
            next_workflow = dict(workflow)
            next_workflow["prompt_template"] = prompt
            next_input["workflow"] = next_workflow
    else:
        next_input.pop("prompt", None)
        next_input.pop("prompt_preview", None)
    next_patch["input_json"] = next_input
    return next_patch


def _merge_input_patch_with_current(node: dict, patch: dict) -> dict:
    """Treat node.update(input_json={...}) as a field patch, not a full replace."""
    patch_input = patch.get("input_json")
    if not isinstance(patch_input, dict):
        patch_input = patch.get("input_data")
    if not isinstance(patch_input, dict):
        return patch
    current_input = node.get("input") if isinstance(node.get("input"), dict) else {}
    next_patch = dict(patch)
    next_input = dict(current_input)
    next_input.update(patch_input)
    if isinstance(current_input.get("workflow"), dict) and isinstance(patch_input.get("workflow"), dict):
        next_input["workflow"] = {**current_input["workflow"], **patch_input["workflow"]}
    next_patch["input_json"] = next_input
    next_patch.pop("input_data", None)
    return next_patch


def _workflow_text_prompt_contract_changed(
    old_input: dict[str, Any],
    new_input: dict[str, Any],
    old_prompt: str,
    new_prompt: str,
) -> bool:
    old_workflow = old_input.get("workflow") if isinstance(old_input.get("workflow"), dict) else {}
    new_workflow = new_input.get("workflow") if isinstance(new_input.get("workflow"), dict) else {}
    if not old_workflow and not new_workflow:
        return False
    for key in ("prompt_template", "prompt_ref", "prompt_spec", "primary_skill", "source_node_id"):
        if _stable_json(old_workflow.get(key)) != _stable_json(new_workflow.get(key)):
            return True
    return (
        str(old_prompt or "").strip() != str(new_prompt or "").strip()
        and bool(str(new_workflow.get("prompt_template") or "").strip())
    )


def _with_workflow_text_stale(input_data: dict[str, Any] | None) -> dict[str, Any]:
    next_input = dict(input_data or {})
    workflow = next_input.get("workflow") if isinstance(next_input.get("workflow"), dict) else {}
    next_workflow = dict(workflow)
    next_workflow["stale"] = True
    next_input["workflow"] = next_workflow
    next_input["prompt_status"] = "stale"
    return next_input


def _sync_title_patch_with_input(node: dict, patch: dict) -> dict:
    """Keep node.title and input_json.title aligned for Agent edits."""
    if "title" not in patch:
        return patch
    title = str(patch.get("title") or "").strip()
    if not title:
        return patch
    current_input = node.get("input") if isinstance(node.get("input"), dict) else {}
    patch_input = patch.get("input_json")
    if not isinstance(patch_input, dict):
        patch_input = patch.get("input_data")
    next_input = dict(current_input)
    if isinstance(patch_input, dict):
        next_input.update(patch_input)
    next_input["title"] = title
    next_patch = dict(patch)
    next_patch["input_json"] = next_input
    next_patch.pop("input_data", None)
    return next_patch


_IMAGE_RENDER_FRESHNESS_KEYS = {
    "prompt",
    "image_prompt",
    "visual_prompt",
    "negative_prompt",
    "aspect_ratio",
    "resolution",
    "quality",
    "clarity",
    "seed",
    "style",
    "references",
    "reference_images",
    "depends_on",
}


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _image_render_inputs_changed(
    old_input: dict[str, Any],
    new_input: dict[str, Any],
    old_prompt: str,
    new_prompt: str,
) -> bool:
    if old_prompt.strip() != new_prompt.strip():
        return True
    for key in _IMAGE_RENDER_FRESHNESS_KEYS:
        if _stable_json(old_input.get(key)) != _stable_json(new_input.get(key)):
            return True
    return False


def _with_image_render_state(input_data: dict[str, Any] | None, state: str) -> dict[str, Any]:
    next_input = dict(input_data or {})
    next_input["render_state"] = state
    return next_input


def _image_node_has_rendered_output(node: dict[str, Any]) -> bool:
    output = node.get("output")
    if output not in (None, "", [], {}):
        return True
    if str(node.get("status") or "").strip().lower() in {"completed", "failed"}:
        return True
    node_input = node.get("input") if isinstance(node.get("input"), dict) else {}
    return str(node_input.get("render_state") or node.get("render_state") or "").strip() == "fresh"


async def _persist_prompt_to_node(node_id: str, f: dict, prompt: str) -> dict:
    """Persist normalized prompt into node.prompt and input_json.prompt."""
    if not prompt:
        return f
    next_input = dict(f)
    changed = False
    if next_input.get("prompt") != prompt:
        next_input["prompt"] = prompt
        changed = True
    try:
        node_now = await canvas_tools.get_node(node_id)
        current_input = node_now.get("input") if isinstance(node_now, dict) else None
        if isinstance(current_input, dict):
            merged = dict(current_input)
            if merged.get("prompt") != prompt:
                merged["prompt"] = prompt
                changed = True
            next_input = {**merged, **next_input}
    except Exception:
        logger.exception("read node before prompt persist failed")
    if changed:
        try:
            await canvas_tools.update_node(node_id, {"prompt": prompt, "input_json": next_input})
        except Exception:
            logger.exception("persist normalized prompt failed for node %s", node_id)
    return next_input


async def _resolve_image_model(model: str | None) -> tuple[str | None, str | None]:
    """验证 model 名是否存在,不存在则返回 (None, warning_msg) 让调用方走 active provider。

    Returns: (resolved_model_or_None, warning_or_None)
    """
    if not model:
        return None, None
    try:
        from app.mcp_tools.media_provider_tools import media_list_providers
        result = await media_list_providers(kind="image")
        names = [p.get("name") for p in (result.get("providers") or [])]
        if model in names:
            return model, None
        return None, f"model={model!r} 不在可用 image provider 中({names}),已自动改用 active provider"
    except Exception:
        # 查不到就让原工具自己报错(降级)
        return model, None


async def _render_image_node(project_id: str, node_id: str, f: dict, node_type: str) -> dict:
    """统一 image 类节点的 render 实现:用节点已就绪的 prompt + 参数出图。"""
    direct_output, direct_error, direct_warnings = await _direct_image_source_output(project_id, f)
    if direct_output:
        if direct_warnings:
            direct_output["reference_warnings"] = direct_warnings
        return direct_output
    if direct_error:
        return direct_error

    base_prompt = _visual_prompt_from_fields(f)
    if base_prompt and base_prompt != f.get("prompt"):
        f = await _persist_prompt_to_node(node_id, f, base_prompt)
        base_prompt = _visual_prompt_from_fields(f)
    if not base_prompt:
        return {
            "error": "image 节点缺 prompt，无法出图。请由模型读取需要的 skill/template 后写入最终图片 prompt。",
            "error_kind": "missing_prompt",
            "node_id": node_id,
            "node_type": node_type,
        }
    prompt = _image_prompt_with_render_modifiers(base_prompt, f)

    from app.agent import message_queue as mq
    cancel_reason = await mq.get_cancel_reason(project_id)
    if cancel_reason:
        return {
            "ok": False,
            "error": f"图片生成已停止：{cancel_reason}",
            "error_kind": "cancelled",
            "node_id": node_id,
            "type": node_type,
        }

    reference_images, reference_warnings = await _reference_images_for_media_run(project_id, f)

    # model 兜底:模型瞎填不存在的 provider 名 → 静默改走 active,避免直接失败
    resolved_model, warning = await _resolve_image_model(f.get("model"))

    aspect = f.get("aspect_ratio") or "16:9"
    try:
        size = _resolve_size(f.get("resolution"), aspect)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "invalid_resolution",
            "node_id": node_id,
            "type": node_type,
            "hint": (
                "用 node.update 修改原节点 fields.resolution 为精确像素尺寸后重试；"
                f"aspect_ratio={aspect} 可用示例: {_resolution_examples(aspect)}。"
            ),
        }
    quality = f.get("quality")

    generation_prompt = _prompt_with_reference_image_mentions(prompt, f, reference_images)
    image_result = await media_generation.generate_image(
        project_id=project_id, prompt=generation_prompt,
        aspect_ratio=aspect,
        size=size,
        quality=quality,
        node_id=node_id, model=resolved_model,
        reference_images=reference_images,
    )

    merged = _merge_image_output(
        {},
        image_result,
        prompt,
        {**f, "reference_images": reference_images},
    )
    if warning:
        merged["model_warning"] = warning
    if reference_warnings:
        merged["reference_warnings"] = [
            *(
                merged.get("reference_warnings")
                if isinstance(merged.get("reference_warnings"), list)
                else []
            ),
            *reference_warnings,
        ]
    return merged


async def _render_image_node_once(
    project_id: str,
    node_id: str,
    fields: dict,
    node_type: str,
) -> dict:
    try:
        result = await asyncio.wait_for(
            _render_image_node(project_id, node_id, fields, node_type),
            timeout=IMAGE_RENDER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        result = {
            "ok": False,
            "error": f"出图超时({IMAGE_RENDER_TIMEOUT_SECONDS}s)",
            "error_kind": "timeout",
            "node_id": node_id,
            "type": node_type,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "error_kind": exc.__class__.__name__,
            "node_id": node_id,
            "type": node_type,
        }
    final_result = result if isinstance(result, dict) else {"ok": False, "error": str(result)}
    if final_result.get("error"):
        final_result["node_render_attempts"] = [{
            "attempt": 1,
            "ok": False,
            "error": str(final_result.get("error") or "")[:500],
            "error_kind": final_result.get("error_kind"),
            "http_code": final_result.get("http_code"),
        }]
    return final_result


def _merge_image_output(text_meta: dict, image_result: Any, prompt: str, f: dict) -> dict:
    """把"先文本后图"两阶段结果合并成节点 output,保留生图参数让前端面板可显示。"""
    merged: dict[str, Any] = {}
    if isinstance(text_meta, dict):
        merged.update(text_meta)
    if isinstance(image_result, dict):
        merged.update(image_result)
    merged["prompt"] = prompt
    merged["resolution"] = (
        (image_result.get("size_final") if isinstance(image_result, dict) else None)
        or (image_result.get("size") if isinstance(image_result, dict) else None)
        or f.get("resolution")
    )
    merged["aspect_ratio"] = (
        (image_result.get("aspect_ratio") if isinstance(image_result, dict) else None)
        or f.get("aspect_ratio")
    )
    merged["quality"] = (
        (image_result.get("quality") if isinstance(image_result, dict) else None)
        or f.get("quality")
    )
    if f.get("clarity"):
        merged["clarity"] = f.get("clarity")
    merged["model"] = (
        (image_result.get("model") if isinstance(image_result, dict) else None)
        or f.get("model")
    )
    merged["reference_images"] = f.get("reference_images") or []
    if isinstance(image_result, dict) and not image_result.get("ok"):
        image_error = image_result.get("error") or image_result.get("provider_msg") or "image generation failed"
        merged["image_error"] = image_error
        merged["error"] = image_error
        merged["status"] = "failed"
        for key in ("error_kind", "error_source", "http_code", "provider_msg", "endpoint"):
            if image_result.get(key) is not None:
                merged[key] = image_result.get(key)
    return merged


async def _run_text_node(project_id: str, node_id: str, f: dict) -> dict:
    prompt = str(f.get("prompt") or f.get("instruction") or "").strip()
    content = str(f.get("content") or f.get("description") or "").strip()
    references = f.get("references") or []
    depends_on = f.get("depends_on") or []
    if not prompt:
        return {
            "type": "text",
            "title": f.get("title"),
            "content": content,
            "references": references,
            "depends_on": depends_on,
        }

    history = _text_chat_history_entries(f)
    reference_images, resolved_reference_image_urls, reference_warnings = await _reference_image_urls_for_text_run(project_id, f)
    messages: list[dict[str, Any]] = []
    for item in history[-6:]:
        previous_prompt = str(item.get("prompt") or "").strip()
        previous_content = str(item.get("content") or item.get("reply") or item.get("output") or "").strip()
        if previous_prompt:
            messages.append({"role": "user", "content": previous_prompt})
        if previous_content:
            messages.append({"role": "assistant", "content": previous_content})
    prompt_for_llm = _prompt_with_reference_image_mentions(prompt, f, reference_images)
    if resolved_reference_image_urls:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt_for_llm}]
        user_content.extend({
            "type": "image_url",
            "image_url": {"url": image_url},
        } for image_url in resolved_reference_image_urls)
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": prompt_for_llm})

    task_type = str(f.get("llm_task_type") or "text_generation").strip() or "text_generation"
    model_override = str(f.get("model") or f.get("llm_model") or f.get("provider") or "").strip() or None
    system = (
        "你是画布文本节点的单次对话助手。"
        "根据用户提示词、已有对话历史和随消息提供的参考图直接给出可展示正文。"
        "输出只包含正文内容，保持自然语言，不写 JSON、Markdown 代码围栏或额外状态说明。"
    )
    run_id = f"text_node_{new_run_id()}"
    dump_llm_request(
        project_id,
        run_id,
        0,
        system,
        messages,
        [],
        user_message=f"text node {node_id}: {prompt}",
    )
    async with session_scope() as session:
        llm_result = await LLMService(session).generate(
            task_type=task_type,
            messages=messages,
            system=system,
            project_id=project_id,
            node_override=model_override,
        )
    reply = _strip_llm_fences(str(llm_result.get("content") or "")).strip()
    if not reply:
        return {
            "type": "text",
            "title": f.get("title"),
            "content": content,
            "prompt": prompt,
            "references": references,
            "depends_on": depends_on,
            "reference_images": reference_images,
            "resolved_reference_image_count": len(resolved_reference_image_urls),
            "reference_warnings": reference_warnings,
            "error": "文本模型没有返回内容",
            "error_kind": "empty_llm_response",
        }

    history_entry = {
        "id": run_id,
        "prompt": prompt,
        "content": reply,
        "model": llm_result.get("model"),
        "usage": llm_result.get("usage"),
        "usage_total_tokens": _workflow_text_usage_total(llm_result.get("usage")),
        "created_at": _utc_now_iso(),
    }
    next_history = [*history, history_entry][-20:]
    next_fields = dict(f)
    next_fields["content"] = reply
    next_fields["prompt"] = prompt
    next_fields["text_chat_history"] = next_history
    next_fields["reference_images"] = reference_images
    next_fields.setdefault("llm_task_type", task_type)
    if model_override:
        next_fields["model"] = model_override
    await canvas_tools.update_node(node_id, {"input_data": next_fields})
    return {
        "type": "text",
        "title": f.get("title"),
        "content": reply,
        "prompt": prompt,
        "text_chat_history": next_history,
        "chat_history": next_history,
        "model": llm_result.get("model"),
        "usage": llm_result.get("usage"),
        "usage_total_tokens": history_entry["usage_total_tokens"],
        "references": references,
        "depends_on": depends_on,
        "reference_images": reference_images,
        "resolved_reference_image_count": len(resolved_reference_image_urls),
        "reference_warnings": reference_warnings,
    }


def _text_chat_history_entries(fields: dict[str, Any]) -> list[dict[str, Any]]:
    raw = fields.get("text_chat_history") or fields.get("chat_history") or []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or item.get("input") or item.get("user") or "").strip()
        content = str(item.get("content") or item.get("reply") or item.get("response") or item.get("output") or "").strip()
        if not prompt and not content:
            continue
        result.append(dict(item, prompt=prompt, content=content))
    return result


def _workflow_text_meta(fields: dict[str, Any]) -> dict[str, Any]:
    workflow = fields.get("workflow")
    return dict(workflow) if isinstance(workflow, dict) else {}


def _is_placeholder_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered in {"todo", "tbd", "pending", "draft"}:
        return True
    return text.startswith(("待", "TODO", "TBD"))


def _should_generate_workflow_text(fields: dict[str, Any], action: str | None) -> bool:
    workflow = _workflow_text_meta(fields)
    if not workflow:
        return False
    if action == "force":
        return True
    if not any(workflow.get(key) for key in ("prompt_template", "prompt_ref", "prompt_spec", "primary_skill", "source_node_id")):
        return False
    if bool(workflow.get("stale")):
        return True
    return _is_placeholder_text(fields.get("content") or fields.get("description"))


def _workflow_text_task_type(workflow: dict[str, Any], fields: dict[str, Any]) -> str:
    del workflow, fields
    return "workflow_text_generation"


def _strip_llm_fences(content: str) -> str:
    text = str(content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _workflow_text_ref_values(fields: dict[str, Any]) -> list[str]:
    raw_items: list[Any] = []
    for key in ("references", "depends_on"):
        value = fields.get(key)
        if isinstance(value, list):
            raw_items.extend(value)
        elif value:
            raw_items.append(value)
    refs: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            raw = item.get("ref") or item.get("node_id") or item.get("id") or item.get("value")
        else:
            raw = item
        text = str(raw or "").strip()
        if not text or text.startswith(("asset:", "upload:", "http://", "https://")) or "/" in text:
            continue
        if text not in refs:
            refs.append(text)
    return refs


_WORKFLOW_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_WORKFLOW_CONTEXT_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORKFLOW_CONTEXT_INDEX_RE = re.compile(r"^(.+?)\[(\d+)\]$")


def _workflow_unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        marker = _workflow_context_key(text)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(text)
    return result


def _workflow_context_key(value: Any) -> str:
    text = str(value or "").strip()
    text = _WORKFLOW_CONTEXT_CAMEL_RE.sub("_", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.lower())


def _workflow_lookup_dict(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    wanted = _workflow_context_key(key)
    for candidate_key, value in payload.items():
        if _workflow_context_key(candidate_key) == wanted:
            return value
    return None


def _workflow_json_object_candidates(value: str) -> list[str]:
    text = _strip_llm_fences(value)
    candidates = [text]
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        candidates.append(match.group(0))
    return candidates


def _workflow_parse_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    for candidate in _workflow_json_object_candidates(value):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _workflow_structured_value(value: Any) -> Any:
    parsed = _workflow_parse_json_object(value)
    if parsed is None:
        return value
    for key in ("content", "text", "full_text", "output"):
        nested = parsed.get(key)
        nested_parsed = _workflow_parse_json_object(nested)
        if nested_parsed is not None:
            result = {**parsed, **nested_parsed}
            result[key] = nested
            return result
    return parsed


_WORKFLOW_PROMPT_WORKFLOW_KEYS = (
    "step_id",
    "template_step_id",
    "source_node_id",
    "repeat_group_id",
    "repeat_group_index",
    "instance_scope",
)
_WORKFLOW_LLM_CONTRACT_KEYS = (
    "template_id",
    "template_name",
    "instance_id",
    "step_id",
    "template_step_id",
    "repeat_group_id",
    "repeat_group_index",
    "instance_scope",
    "runner",
    "primary_skill",
    "skill_category",
    "output_mode",
    "output_schema",
    "completion",
    "acceptance",
    "input_facts",
)
_WORKFLOW_PROMPT_MEDIA_OUTPUT_KEYS = (
    "type",
    "subject",
    "content",
    "text",
    "description",
    "summary",
    "caption",
    "transcript",
    "prompt",
    "visual_prompt",
    "video_prompt",
)


def _compact_workflow_prompt_value(value: Any, *, depth: int = 0) -> Any:
    """Bound structured workflow values without duplicating runtime history."""
    if depth >= 6:
        return str(value)[:1000]
    if isinstance(value, str):
        return value[:6000]
    if isinstance(value, list):
        return [
            _compact_workflow_prompt_value(item, depth=depth + 1)
            for item in value[:80]
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            if key in {"history", "run_history", "chat_history", "text_chat_history"}:
                continue
            result[str(key)] = _compact_workflow_prompt_value(item, depth=depth + 1)
        return result
    return value


def _compact_workflow_prompt_output(
    node: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    output = _workflow_structured_value(node.get("output"))
    structured_outputs = node.get("outputs") if isinstance(node.get("outputs"), list) else None
    if output in (None, "", [], {}) and structured_outputs:
        first_output = structured_outputs[0] if isinstance(structured_outputs[0], dict) else {}
        output = _workflow_structured_value(first_output.get("value") if isinstance(first_output, dict) else first_output)
    if not isinstance(output, dict):
        output = {"content": output} if output not in (None, "", [], {}) else {}
    if output in (None, "", [], {}):
        content = fields.get("content") or node.get("content")
        if content not in (None, "", [], {}):
            output = {"content": content}

    node_type = str(node.get("type") or "").strip()
    if node_type in {"image", "video", "audio"}:
        compact = {
            key: output.get(key)
            for key in _WORKFLOW_PROMPT_MEDIA_OUTPUT_KEYS
            if output.get(key) not in (None, "", [], {})
        }
        prompt = node.get("prompt") or fields.get("prompt")
        if prompt not in (None, "", [], {}):
            compact.setdefault("prompt", prompt)
        return _compact_workflow_prompt_value(compact)
    return _compact_workflow_prompt_value(output)


def _compact_workflow_llm_contract(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _compact_workflow_prompt_value(workflow[key])
        for key in _WORKFLOW_LLM_CONTRACT_KEYS
        if key in workflow and workflow[key] not in (None, "", [], {})
    }


def _compact_workflow_text_node(
    node: dict[str, Any],
    *,
    include_output: bool = True,
) -> dict[str, Any]:
    fields = dict(node.get("input") or {})
    workflow = fields.get("workflow") if isinstance(fields.get("workflow"), dict) else _workflow_text_meta(fields)
    if not workflow and isinstance(node.get("workflow"), dict):
        workflow = node["workflow"]
    compact_workflow = {
        key: workflow.get(key)
        for key in _WORKFLOW_PROMPT_WORKFLOW_KEYS
        if workflow.get(key) not in (None, "", [], {})
    }
    result = {
        "id": public_node_id_from_dict(node),
        "title": node.get("title"),
        "type": node.get("type"),
        "status": node.get("status"),
        "workflow": compact_workflow,
    }
    if include_output:
        output = _compact_workflow_prompt_output(node, fields)
        if output:
            # Keep one canonical value. Template aliases synthesize `.outputs`
            # from this field without serializing the same payload twice.
            result["output"] = output
    return {k: v for k, v in result.items() if v not in (None, "", [], {})}


def _workflow_context_aliases(payload: dict[str, Any]) -> list[str]:
    workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else {}
    return _workflow_unique_strings([
        workflow.get("step_id"),
        workflow.get("template_step_id"),
        workflow.get("source_node_id"),
        workflow.get("repeat_group_id"),
        payload.get("title"),
    ])


def _workflow_add_context_alias(context: dict[str, Any], alias: str, payload: dict[str, Any]) -> None:
    text = str(alias or "").strip()
    if not text:
        return
    context.setdefault(text, payload)
    normalized = _workflow_context_key(text)
    if normalized and normalized != text:
        context.setdefault(normalized, payload)


def _workflow_add_collection_aliases(context: dict[str, Any], alias: str, payload: dict[str, Any]) -> None:
    text = str(alias or "").strip()
    if not text:
        return
    for container_key, value in (
        ("steps", payload),
        ("nodes", payload),
        ("outputs", payload.get("output") if isinstance(payload, dict) else None),
    ):
        if value in (None, "", [], {}):
            continue
        container = context.setdefault(container_key, {})
        if not isinstance(container, dict):
            continue
        container.setdefault(text, value)
        normalized = _workflow_context_key(text)
        if normalized and normalized != text:
            container.setdefault(normalized, value)


def _workflow_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _workflow_template_context(
    *,
    workflow: dict[str, Any],
    target: dict[str, Any],
    upstream_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    input_facts = workflow.get("input_facts") if isinstance(workflow.get("input_facts"), dict) else {}
    instance_scope = workflow.get("instance_scope") if isinstance(workflow.get("instance_scope"), dict) else {}
    context: dict[str, Any] = {
        "inputs": input_facts,
        "input_facts": input_facts,
        "instance": instance_scope,
        "json": instance_scope or input_facts,
        "steps": {},
        "nodes": {},
        "outputs": {},
        "target": target,
        "target_node": target,
        "upstream_nodes": upstream_nodes,
    }
    item_name = str(workflow.get("item_name") or workflow.get("item_source") or "").strip()
    if item_name and instance_scope:
        context.setdefault(item_name, instance_scope)
        normalized_item_name = _workflow_context_key(item_name)
        if normalized_item_name and normalized_item_name != item_name:
            context.setdefault(normalized_item_name, instance_scope)
    previous_segment: dict[str, Any] = {}
    previous_candidates: list[dict[str, Any]] = []
    current_group = str(workflow.get("repeat_group_id") or "").strip()
    current_index = _workflow_int(workflow.get("repeat_group_index"))

    for upstream in upstream_nodes:
        upstream_workflow = upstream.get("workflow") if isinstance(upstream.get("workflow"), dict) else {}
        upstream_group = str(upstream_workflow.get("repeat_group_id") or "").strip()
        upstream_index = _workflow_int(upstream_workflow.get("repeat_group_index"))
        is_previous = bool(
            current_group
            and upstream_group == current_group
            and current_index is not None
            and upstream_index == current_index - 1
        )
        if is_previous:
            previous_candidates.append(upstream)
        target_context = previous_segment if is_previous else context
        alias_payload = upstream
        if "outputs" not in upstream and upstream.get("output") not in (None, "", [], {}):
            alias_payload = {**upstream, "outputs": upstream["output"]}
        for alias in _workflow_context_aliases(upstream):
            _workflow_add_context_alias(target_context, alias, alias_payload)
            if target_context is context:
                _workflow_add_collection_aliases(context, alias, alias_payload)

    # ``{{ previous }}`` is the previous attempt's result, not an alias index.
    # The alias index above is useful for path lookup, but serializing it repeats
    # the same review under step id, template id, group id, title, and normalized
    # aliases. A small review can otherwise expand into tens of thousands of
    # characters before the request reaches the provider.
    previous_value: Any = {}
    if previous_candidates:
        until_source = str(workflow.get("repeat_until_source_step") or "").strip()
        selected_previous = next(
            (
                item
                for item in reversed(previous_candidates)
                if str(
                    (
                        item.get("workflow")
                        if isinstance(item.get("workflow"), dict)
                        else {}
                    ).get("template_step_id")
                    or ""
                ).strip()
                == until_source
            ),
            previous_candidates[-1],
        )
        previous_value = selected_previous.get("output")
        if previous_value in (None, "", [], {}):
            previous_value = {
                key: selected_previous.get(key)
                for key in ("title", "status")
                if selected_previous.get(key) not in (None, "", [], {})
            }
    context["previous_segment"] = previous_value
    context["previous"] = previous_value
    return context


def _workflow_child_values(value: Any, segment: str) -> list[Any]:
    text = str(segment or "").strip()
    if not text:
        return []
    wants_list = text.endswith("[]")
    key = text[:-2] if wants_list else text
    index_match = _WORKFLOW_CONTEXT_INDEX_RE.match(key)
    explicit_index: int | None = None
    if index_match:
        key = index_match.group(1)
        explicit_index = int(index_match.group(2))

    if isinstance(value, dict):
        child = _workflow_lookup_dict(value, key)
        if explicit_index is not None and isinstance(child, list):
            return [child[explicit_index]] if 0 <= explicit_index < len(child) else []
        if wants_list and isinstance(child, list):
            return list(child)
        return [child] if child is not None else []
    if isinstance(value, list):
        if explicit_index is not None and key in {"", "item"}:
            return [value[explicit_index]] if 0 <= explicit_index < len(value) else []
        values: list[Any] = []
        for item in value:
            values.extend(_workflow_child_values(item, segment))
        return values
    return []


def _workflow_context_path(context: dict[str, Any], path: str) -> tuple[bool, Any]:
    parts = [part.strip() for part in str(path or "").split(".") if part.strip()]
    if not parts:
        return True, context
    values: list[Any] = [context]
    for part in parts:
        next_values: list[Any] = []
        for value in values:
            next_values.extend(_workflow_child_values(value, part))
        values = next_values
        if not values:
            return False, None
    if len(values) == 1:
        return True, values[0]
    return True, values


def _workflow_template_value_to_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return str(value)


def _workflow_render_prompt_template(
    template: Any,
    *,
    workflow: dict[str, Any],
    target: dict[str, Any],
    upstream_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = str(template or "")
    if not raw:
        return {"prompt_template": "", "rendered_prompt_template": "", "unresolved_template_paths": []}
    context = _workflow_template_context(
        workflow=workflow,
        target=target,
        upstream_nodes=upstream_nodes,
    )
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        expression = str(match.group(1) or "").strip()
        found, value = _workflow_context_path(context, expression)
        if not found:
            missing.append(expression)
            return match.group(0)
        return _workflow_template_value_to_text(value)

    rendered = _WORKFLOW_TEMPLATE_PLACEHOLDER_RE.sub(replace, raw)
    return {
        "prompt_template": raw,
        "rendered_prompt_template": rendered,
        "unresolved_template_paths": _workflow_unique_strings(missing),
    }


async def _load_workflow_text_skill(workflow: dict[str, Any]) -> dict[str, Any]:
    primary = str(workflow.get("primary_skill") or "").strip()
    if not primary:
        return {"ok": False, "error": "no primary_skill"}
    category = str(workflow.get("skill_category") or "prompt").strip()
    scope = str(workflow.get("skill_scope") or workflow.get("scope") or "").strip()
    from app.mcp_tools import skill_tools

    return await skill_tools.skill_get_skill(primary, category=category, scope=scope)


def _workflow_prompt_template_has_contract(prompt_runtime: dict[str, Any]) -> bool:
    return bool(
        str(prompt_runtime.get("rendered_prompt_template") or "").strip()
        or str(prompt_runtime.get("prompt_template") or "").strip()
    )


async def _workflow_runtime_skill_payload(
    workflow: dict[str, Any],
    prompt_runtime: dict[str, Any],
) -> dict[str, Any]:
    if _workflow_prompt_template_has_contract(prompt_runtime):
        return {
            "name": workflow.get("primary_skill"),
            "category": workflow.get("skill_category"),
            "scope": workflow.get("skill_scope") or workflow.get("scope"),
            "content": "",
            "content_mode": "compiled_prompt_template",
            "load_error": None,
        }

    skill = await _load_workflow_text_skill(workflow)
    skill_content = str(skill.get("content") or "") if skill.get("ok") else ""
    if len(skill_content) > 12000:
        skill_content = skill_content[:12000] + "\n\n[skill content truncated]"
    return {
        "name": skill.get("name") if skill.get("ok") else workflow.get("primary_skill"),
        "category": skill.get("category") if skill.get("ok") else workflow.get("skill_category"),
        "scope": skill.get("scope") if skill.get("ok") else workflow.get("skill_scope"),
        "content": skill_content,
        "content_mode": "fallback_skill_content" if skill.get("ok") else "missing_skill",
        "load_error": None if skill.get("ok") else skill.get("error"),
    }


async def _call_workflow_text_llm(
    *,
    task_type: str,
    system: str,
    message: str,
    project_id: str,
    image_urls: list[str] | None = None,
    image_labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text_chars = len(system or "") + len(message or "")
    if text_chars > WORKFLOW_LLM_MAX_TEXT_CHARS:
        raise ValueError(
            "workflow LLM request exceeds text budget: "
            f"{text_chars} > {WORKFLOW_LLM_MAX_TEXT_CHARS} chars; "
            "reduce rendered prompt variables or upstream structured output"
        )
    clean_image_urls = [
        str(image_url).strip()
        for image_url in (image_urls or [])
        if str(image_url or "").strip()
    ]
    if len(clean_image_urls) > WORKFLOW_LLM_MAX_IMAGE_COUNT:
        raise ValueError(
            "workflow LLM request exceeds image budget: "
            f"{len(clean_image_urls)} > {WORKFLOW_LLM_MAX_IMAGE_COUNT} images"
        )
    user_content: str | list[dict[str, Any]] = message
    if clean_image_urls:
        user_content = [{"type": "text", "text": message}]
        labels = image_labels if isinstance(image_labels, list) else []
        for index, image_url in enumerate(clean_image_urls):
            label = labels[index] if index < len(labels) and isinstance(labels[index], dict) else {}
            mention = str(label.get("mention") or "").strip()
            title = str(label.get("label") or "").strip()
            if mention:
                user_content.append({
                    "type": "text",
                    "text": f"参考图片标签：{mention}" + (f"（{title}）" if title else ""),
                })
            user_content.append({
                "type": "image_url",
                "image_url": {"url": image_url},
            })
    async with session_scope() as session:
        return await LLMService(session).generate(
            task_type=task_type,
            messages=[{"role": "user", "content": user_content}],
            system=system,
            project_id=project_id,
        )


def _workflow_text_usage_total(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "total"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _workflow_text_run_log(workflow: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    clean_record = {
        key: value
        for key, value in record.items()
        if value not in (None, "", [], {})
    }
    history = workflow.get("run_history")
    if not isinstance(history, list):
        history = []
    next_workflow = dict(workflow)
    next_workflow["last_run"] = clean_record
    next_workflow["run_history"] = [*history, clean_record][-8:]
    return next_workflow


async def _update_workflow_text_run_log(
    node_id: str,
    fields: dict[str, Any],
    record: dict[str, Any],
) -> None:
    updated_fields = dict(fields)
    workflow = _workflow_text_meta(fields)
    updated_fields["workflow"] = _workflow_text_run_log(workflow, record)
    try:
        await canvas_tools.update_node(node_id, {"input_data": updated_fields})
    except Exception:
        logger.exception("workflow text run log update failed")


async def _generate_workflow_text_node(
    *,
    project_id: str,
    node_id: str,
    node: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    workflow = _workflow_text_meta(fields)
    upstream_nodes: list[dict[str, Any]] = []
    for ref in _workflow_text_ref_values(fields):
        resolved = await _resolve_agent_node_id(project_id, ref)
        if not resolved or resolved == node_id:
            continue
        upstream = await canvas_tools.get_node(resolved)
        if not upstream or upstream.get("error"):
            continue
        upstream_nodes.append(_compact_workflow_text_node(upstream))

    # A visible text node may be regenerated after an upstream change; retain
    # its bounded previous output so the model can revise instead of starting
    # blind. Flow-only runtime steps use a separate path that excludes it.
    target = _compact_workflow_text_node(node)
    prompt_runtime = _workflow_render_prompt_template(
        workflow.get("prompt_template"),
        workflow=workflow,
        target=target,
        upstream_nodes=upstream_nodes,
    )
    skill_payload = await _workflow_runtime_skill_payload(workflow, prompt_runtime)
    vision_refs, vision_image_urls, vision_warnings = await _workflow_text_vision_context_image_urls(project_id, fields)
    if vision_refs and (vision_warnings or not vision_image_urls):
        detail = "; ".join(dict.fromkeys(vision_warnings)) or "没有可发送的图片"
        raise ValueError(f"必须查看的参考图不可用: {detail}")
    structured_contract = structured_output_contract(workflow)
    structured_instructions = structured_output_instructions(workflow)
    system = (
        "You are a one-shot workflow text node runner. "
        "Generate the final fields.content for exactly one text node from the provided node spec, prompt template, and upstream nodes. "
        "Use rendered_prompt_template as the execution contract when it is present. "
        "Use skill.content only when no prompt template is available. "
        "Return only the content to write into fields.content."
    )
    if structured_instructions:
        system = f"{system}\n\n{structured_instructions}"
    include_upstream_payload = bool(
        not str(prompt_runtime.get("rendered_prompt_template") or "").strip()
        or prompt_runtime.get("unresolved_template_paths")
    )
    message = json.dumps(
        {
            "target_node": target,
            "workflow": _compact_workflow_llm_contract(workflow),
            "prompt_template": prompt_runtime["prompt_template"],
            "rendered_prompt_template": prompt_runtime["rendered_prompt_template"],
            "unresolved_template_paths": prompt_runtime["unresolved_template_paths"],
            "prompt_ref": workflow.get("prompt_ref"),
            "prompt_spec": workflow.get("prompt_spec"),
            "output_mode": workflow.get("output_mode"),
            "output_schema": workflow.get("output_schema"),
            "structured_output_contract": structured_contract,
            "completion": workflow.get("completion"),
            "acceptance": workflow.get("acceptance"),
            "input_facts": workflow.get("input_facts"),
            "skill": skill_payload,
            "upstream_nodes": upstream_nodes if include_upstream_payload else [],
            "vision_context_images": vision_refs,
        },
        ensure_ascii=False,
        default=str,
    )
    task_type = _workflow_text_task_type(workflow, fields)
    started_at = _utc_now_iso()
    dump_run_id = f"workflow_text_{new_run_id()}"
    dump_llm_request(
        project_id,
        dump_run_id,
        0,
        system,
        [{"role": "user", "content": message}],
        [],
        user_message=f"workflow text node {public_node_id_from_dict(node) or node_id}",
    )
    try:
        llm_result = await _call_workflow_text_llm(
            task_type=task_type,
            system=system,
            message=message,
            project_id=project_id,
            image_urls=vision_image_urls,
        )
    except Exception as exc:
        await _update_workflow_text_run_log(
            node_id,
            fields,
            {
                "run_id": dump_run_id,
                "status": "failed",
                "task_type": task_type,
                "prompt_dump_run_id": dump_run_id,
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "error": str(exc)[:500],
            },
        )
        raise
    content = _strip_llm_fences(str(llm_result.get("content") or ""))
    if not content:
        await _update_workflow_text_run_log(
            node_id,
            fields,
            {
                "run_id": dump_run_id,
                "status": "failed",
                "task_type": task_type,
                "model": llm_result.get("model"),
                "usage_total_tokens": _workflow_text_usage_total(llm_result.get("usage")),
                "prompt_dump_run_id": dump_run_id,
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "error": "empty_llm_output",
            },
        )
        return {"error": "workflow text runner returned empty content", "error_kind": "empty_llm_output"}

    structured_output: Any | None = None
    if structured_contract:
        try:
            structured_output = parse_structured_output(content, workflow)
        except WorkflowStructuredOutputError as exc:
            await _update_workflow_text_run_log(
                node_id,
                fields,
                {
                    "run_id": dump_run_id,
                    "status": "failed",
                    "task_type": task_type,
                    "model": llm_result.get("model"),
                    "usage_total_tokens": _workflow_text_usage_total(llm_result.get("usage")),
                    "prompt_dump_run_id": dump_run_id,
                    "started_at": started_at,
                    "completed_at": _utc_now_iso(),
                    "error": str(exc)[:500],
                },
            )
            return {
                "error": f"workflow structured output invalid: {exc}",
                "error_kind": "structured_output_invalid",
            }

    updated_fields = dict(fields)
    updated_workflow = dict(workflow)
    updated_workflow["runner"] = "node.run"
    updated_workflow["llm_task_type"] = task_type
    updated_workflow["step_status"] = "completed"
    updated_workflow["stale"] = False
    updated_workflow = _workflow_text_run_log(
        updated_workflow,
        {
            "run_id": dump_run_id,
            "status": "completed",
            "task_type": task_type,
            "model": llm_result.get("model"),
            "usage": llm_result.get("usage"),
            "usage_total_tokens": _workflow_text_usage_total(llm_result.get("usage")),
            "prompt_dump_run_id": dump_run_id,
            "started_at": started_at,
            "completed_at": _utc_now_iso(),
            "content_chars": len(content),
            "request_message_chars": len(message),
            "upstream_record_count": len(upstream_nodes),
            "serialized_upstream_record_count": len(upstream_nodes) if include_upstream_payload else 0,
            "vision_image_count": len(vision_image_urls),
        },
    )
    updated_fields["workflow"] = updated_workflow
    updated_fields["content"] = content
    updated_fields["prompt_status"] = "completed"
    await canvas_tools.update_node(node_id, {"input_data": updated_fields})
    result: dict[str, Any] = {
        "type": "text",
        "title": fields.get("title") or node.get("title"),
        "content": content,
        "references": updated_fields.get("references") or [],
        "depends_on": updated_fields.get("depends_on") or [],
        "workflow_text_runner": "one_shot_llm",
        "llm_task_type": task_type,
        "model": llm_result.get("model"),
        "usage": llm_result.get("usage"),
        "run_id": dump_run_id,
        "prompt_dump_run_id": dump_run_id,
    }
    if structured_output is not None:
        result["structured_output"] = structured_output
        if isinstance(structured_output, dict):
            result.update(structured_output)
    return result

def _image_operation_name(fields: dict[str, Any]) -> str:
    return str(
        fields.get("operation")
        or fields.get("image_operation")
        or fields.get("operation_type")
        or ""
    ).strip().lower()


def _grid_rows_cols(fields: dict[str, Any]) -> tuple[int, int]:
    grid = fields.get("grid") if isinstance(fields.get("grid"), dict) else {}
    rows = grid.get("rows") or fields.get("rows") or 2
    cols = grid.get("cols") or fields.get("cols") or 2
    return int(rows), int(cols)


async def _run_image_node(project_id: str, node_id: str, f: dict) -> dict:
    operation = _image_operation_name(f)
    if operation in {"grid_split", "split_grid"}:
        from app.services import image_operations

        rows, cols = _grid_rows_cols(f)
        return await image_operations.split_grid_node(
            project_id=project_id,
            node_id=node_id,
            rows=rows,
            cols=cols,
            source_ref=f.get("source_ref") or f.get("source_image"),
        )
    if operation in {"grid_combine", "combine_grid"}:
        from app.services import image_operations

        rows, cols = _grid_rows_cols(f)
        source_refs = f.get("source_images") or f.get("source_refs") or f.get("reference_images") or []
        if not isinstance(source_refs, list):
            source_refs = [source_refs]
        return await image_operations.combine_grid_node(
            project_id=project_id,
            node_id=node_id,
            source_refs=[str(item) for item in source_refs],
            rows=rows,
            cols=cols,
            fit=str(f.get("fit") or "cover"),
        )
    if operation in {"inpaint", "inpaint_region", "grid_inpaint_cell"}:
        from app.services import image_operations

        return await image_operations.inpaint_region_node(
            project_id=project_id,
            node_id=node_id,
            prompt=str(f.get("prompt") or ""),
            mask_ref=f.get("mask_ref"),
            mask=f.get("mask") if isinstance(f.get("mask"), dict) else None,
            cell_id=f.get("cell_id"),
        )
    return await _render_image_node_once(project_id, node_id, f, "image")


async def _run_video_node(project_id: str, node_id: str, f: dict) -> dict:
    force_new_generation = bool(f.pop("_force_new_video_generation", False))
    prompt = str(f.get("prompt") or "").strip()
    if not prompt:
        return {
            "error": "video 节点缺 prompt，无法生成视频。请由模型判断上下文是否足够，并写入最终视频 prompt。",
            "error_kind": "missing_prompt",
            "node_id": node_id,
            "type": "video",
        }
    duration = f.get("duration_seconds") or f.get("duration") or 5
    try:
        duration_seconds = int(float(str(duration)))
    except (TypeError, ValueError):
        duration_seconds = 5
    reference_images, reference_warnings = await _reference_images_for_video_run(project_id, f)
    video_extra = {
        key: f[key]
        for key in (
            "video_mode",
            "mode",
            "ratio",
            "media_references",
            "reference_videos",
            "reference_audios",
            "generate_audio",
            "watermark",
            "return_last_frame",
            "seed",
            "priority",
            "execution_expires_after",
            "safety_identifier",
            "tools",
        )
        if key in f
    }
    generation_prompt = _prompt_with_reference_image_mentions(prompt, f, reference_images)
    result = await media_generation.generate_video(
        project_id=project_id,
        prompt=generation_prompt,
        shot_id=str(f.get("shot_id") or node_id),
        first_frame_asset_id=f.get("first_frame_asset_id"),
        last_frame_asset_id=f.get("last_frame_asset_id"),
        duration_seconds=duration_seconds if duration_seconds == -1 else max(1, duration_seconds),
        aspect_ratio=f.get("aspect_ratio"),
        resolution=f.get("resolution"),
        node_id=node_id,
        model=f.get("model"),
        reference_images=reference_images,
        extra=video_extra,
        record_asset=True,
        resume_existing_job=not force_new_generation,
    )
    if isinstance(result, dict):
        resolved_mode = str(result.get("video_mode") or result.get("mode") or "").strip()
        if resolved_mode and not str(f.get("video_mode") or f.get("mode") or "").strip():
            persisted_fields = dict(f)
            persisted_fields["video_mode"] = resolved_mode
            persisted_fields.pop("mode", None)
            await canvas_tools.update_node(node_id, {"input_data": persisted_fields})
        result["prompt"] = prompt
        result["input"] = media_history.strip_media_history(f)
    if reference_warnings:
        result["reference_warnings"] = [
            *(
                result.get("reference_warnings")
                if isinstance(result.get("reference_warnings"), list)
                else []
            ),
            *reference_warnings,
        ]
    return result


async def _run_audio_node(project_id: str, node_id: str, f: dict) -> dict:
    prompt = str(f.get("prompt") or "").strip()
    if not prompt:
        return {
            "ok": False,
            "type": "audio",
            "error": "audio 节点缺 prompt，无法生成音频。请先写入纯音频提示词。",
            "error_kind": "missing_prompt",
            "node_id": node_id,
        }
    duration = f.get("duration_seconds") or f.get("duration")
    duration_seconds = None
    if duration not in (None, ""):
        try:
            duration_seconds = int(float(str(duration)))
        except (TypeError, ValueError):
            duration_seconds = None
    instrumental = f.get("instrumental")
    if isinstance(instrumental, str):
        lowered = instrumental.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            instrumental = True
        elif lowered in {"0", "false", "no", "n", "off"}:
            instrumental = False
        else:
            instrumental = None
    elif not isinstance(instrumental, bool):
        instrumental = None
    audio_extra = {
        key: f[key]
        for key in (
            "customMode",
            "custom_mode",
            "negativeTags",
            "negative_tags",
            "callBackUrl",
            "callback_url",
            "personaId",
            "persona_id",
            "vocalGender",
            "vocal_gender",
            "styleWeight",
            "style_weight",
            "weirdness",
            "audioWeight",
            "audio_weight",
            "seed",
            "voice",
            "speed",
            "instructions",
            "format",
        )
        if key in f
    }
    result = await media_generation.generate_audio(
        project_id=project_id,
        prompt=prompt,
        node_id=node_id,
        model=f.get("model"),
        title=f.get("title"),
        style=f.get("style"),
        instrumental=instrumental,
        duration_seconds=duration_seconds,
        audio_format=f.get("format"),
        extra=audio_extra,
        record_asset=True,
    )
    if isinstance(result, dict):
        result["prompt"] = prompt
        result["input"] = media_history.strip_media_history(f)
    return result


def _strip_transient_field_keys(value: Any, keys: set[str]) -> Any:
    if not keys:
        return value
    if isinstance(value, dict):
        return {
            key: _strip_transient_field_keys(item, keys)
            for key, item in value.items()
            if key not in keys
        }
    if isinstance(value, list):
        return [_strip_transient_field_keys(item, keys) for item in value]
    return value


_RUNNERS: dict[str, NodeRunner] = {
    "text": _run_text_node,
    "image": _run_image_node,
    "video": _run_video_node,
    "audio": _run_audio_node,
}


async def node_run(
    project_id: str,
    node_id: str,
    action: str | None = None,
    extra_fields: dict | None = None,
    hidden_extra_field_keys: list[str] | None = None,
) -> dict:
    """跑这个节点。后端按 type 自动派发,自动管 status/产物落库。

    image/video/audio 节点必须已经有模型写入的 prompt。后端只执行,不合成业务提示词。

    action 选项:
      None / "run":  默认 — text 保存内容,image 出图,video 生成视频,audio 生成音频
      "render":      仅 image — 用节点 prompt+参数出图。失败/不满意可先 node.update 改参数再 render。
      "force":       忽略已 completed 状态强制重跑准备阶段
    extra_fields:    临时补字段(不写回 input),render 时可临时替换 prompt 等
    hidden_extra_field_keys: 临时字段名,会从持久化 output 和响应里移除
    """
    requested_node_id = node_id
    node_id = await _resolve_agent_node_id(project_id, node_id)
    if not node_id:
        return {
            "ok": False,
            "error": "Current project context is missing; backend could not resolve the node number",
            "error_kind": "missing_project_context",
            "node_id": requested_node_id,
            "hint": "节点编号由后端按当前项目自动解析；请检查 chat stream 是否注入了当前项目上下文。",
        }
    node = await canvas_tools.get_node(node_id)
    if node.get("error"):
        return {
            "ok": False,
            "error": node.get("error") or "Node not found",
            "error_kind": "node_not_found",
            "node_id": requested_node_id,
            "hint": "node.run 的 node_id 使用已存在节点的编号；shot_id、segment_id、标题需要先通过 node.list/node.get 转成节点编号。新任务先 node.create 创建合适节点，再 node.run。",
        }
    model_node_id = public_node_id_from_dict(node)
    project_node_id_map = await _node_public_id_map(project_id)

    hidden_response_keys: set[str] = set()

    def _visible_payload(payload: Any) -> Any:
        return _strip_transient_field_keys(payload, hidden_response_keys)

    def _run_response(payload: dict[str, Any]) -> dict[str, Any]:
        payload = _visible_payload(payload)
        mapped = publicize_node_refs(payload, {**project_node_id_map, node_id: model_node_id})
        if isinstance(mapped, dict):
            mapped["node_id"] = model_node_id
            mapped["_canvas_node_id"] = node_id
            mapped["_canvas_id"] = node_id
            if node.get("display_id") is not None:
                mapped["_canvas_display_id"] = node.get("display_id")
            return mapped
        return payload

    node_type = node.get("type")
    if _node_surface(node) == NODE_SURFACE_WORKFLOW_RUNTIME:
        return _run_response({
            "ok": False,
            "error": "workflow_runtime 节点是工作流内部记录，不能用 node.run 直接运行。请在工作流流程条运行对应步骤。",
            "error_kind": "workflow_runtime_node_not_runnable",
            "node_type": node_type,
        })
    if (
        node_type == "image"
        and str(node.get("status") or "") == "running"
        and action not in {"force", "render"}
    ):
        recovered_url = _completed_image_url_from_output(
            node.get("output"),
            include_direct=False,
        )
        if recovered_url:
            await canvas_tools.update_node(
                node_id,
                {
                    "status": "completed",
                    "error_message": None,
                    "input_data": _with_image_render_state(node.get("input") if isinstance(node.get("input"), dict) else {}, "fresh"),
                },
            )
            return _run_response({
                "ok": True,
                "type": node_type,
                "status": "completed",
                "render_state": "fresh",
                "url": recovered_url,
                "result": node.get("output"),
                "recovered_from_running_output": True,
            })

    # 通用字段拼装
    fields: dict = dict(node.get("input") or {})
    node_prompt = str(node.get("prompt") or "").strip()
    if node_prompt:
        fields["prompt"] = node_prompt
    extra = _coerce_dict(extra_fields, "extra_fields")
    if extra:
        fields.update(extra)
    hidden_response_keys = {
        str(key).strip()
        for key in (hidden_extra_field_keys or [])
        if str(key).strip() and str(key).strip() in extra
    }
    visible_fields = _strip_transient_field_keys(fields, hidden_response_keys)

    if node_type == "image" and _image_operation_name(fields) and action in {None, "run", "force"}:
        archived_output = await _archive_current_media_output_for_rerun(node_id, node, str(node_type), visible_fields)
        if action == "force":
            await canvas_tools.update_node(node_id, {"status": "idle", "error_message": None})
        await canvas_tools.update_node(node_id, {"status": "running", "error_message": None})
        try:
            result = await _run_image_node(project_id, node_id, fields)
        except Exception as exc:
            err_text = f"image operation failed: {exc}"
            await canvas_tools.update_node(node_id, {"status": "failed", "error_message": err_text})
            return _run_response({
                "ok": False,
                "error": err_text,
                "error_kind": exc.__class__.__name__,
                "node_type": node_type,
            })
        if isinstance(result, dict) and result.get("error"):
            await canvas_tools.update_node(
                node_id,
                {"status": "failed", "error_message": result.get("error")},
            )
            return _run_response({
                "ok": False,
                "error": result.get("error"),
                "node_type": node_type,
                **{k: v for k, v in result.items() if k not in {"ok", "error"}},
            })
        if isinstance(result, dict):
            result = media_history.preserve_media_history(result, archived_output)
        await canvas_tools.update_node(
            node_id,
            {
                "status": "completed",
                "error_message": None,
                "output_data": result,
                "input_data": _with_image_render_state(node.get("input") if isinstance(node.get("input"), dict) else {}, "fresh"),
            },
        )
        await _emit_fusion_canvas_event(
            node_id,
            status="completed",
            preview=result if isinstance(result, dict) else None,
            render_state="fresh",
            project_id=project_id,
        )
        return _run_response({
            "ok": True,
            "type": node_type,
            "action": _image_operation_name(fields),
            "status": "completed",
            "render_state": "fresh",
            "result": result,
        })

    if node_type == "image" and action in {None, "run", "force"}:
        action = "render"

    review_recommendation: dict[str, Any] | None = None
    if _node_content_needs_review(node_type, fields):
        state_for_review = await _read_project_state(project_id)
        if not _prompt_review_passed(fields, state_for_review):
            review_recommendation = _prompt_review_required_payload(
                node_id,
                str(node_type or ""),
                fields,
                phase="before_node_run",
            )

    _RENDERABLE = {"image"}
    if action == "render":
        if node_type not in _RENDERABLE:
            _alt = "node.run(action=None) 让默认 runner 处理"
            return _run_response({
                "ok": False,
                "error": f"action='render' 不支持 type={node_type!r}",
                "node_type": node_type,
                "hint": (
                    f"render 仅用于 image 节点。"
                    f"对 {node_type!r},应改用:{_alt}。不要原地重试 render。"
                ),
                "renderable_types": sorted(_RENDERABLE),
                "suggested_next": _alt,
            })

        # 先把节点状态改 running,**同时**在 output_json 里写一个 running 的图片 stage,
        # 这样前端 SmartNode 的 StageImage 能渲染 skeleton(spinner + shimmer)占位,
        # 而不是只看到节点级 "生成中…" 文本,跟最终出图后的版面也保持一致。
        _subj, _stage_name = _SUBJECT_BY_TYPE.get(node_type, (node_type, "图片"))
        archived_output = await _archive_current_media_output_for_rerun(node_id, node, str(node_type), visible_fields)
        # 透出当前规格让前端 skeleton 一旁就能显示
        _aspect = fields.get("aspect_ratio")
        if not _aspect:
            _aspect = "16:9"
        try:
            _size_preview = _resolve_size(fields.get("resolution"), _aspect)
        except ValueError as exc:
            err_text = str(exc)
            await canvas_tools.update_node(
                node_id,
                {"status": "failed", "error_message": err_text},
            )
            fusion = await _merge_stage_into_fusion(
                node_id, node_type, status="failed", error=err_text,
                aspect_ratio=_aspect,
                prompt=str(fields.get("prompt") or ""),
                input_data=visible_fields,
            )
            await _emit_fusion_canvas_event(
                node_id, status="failed",
                error=err_text, preview=fusion, project_id=project_id,
            )
            return _run_response({
                "ok": False,
                "error": err_text,
                "error_kind": "invalid_resolution",
                "node_type": node_type,
                "hint": (
                    "用 node.update 修改原节点 fields.resolution 为精确像素尺寸后重试；"
                    f"aspect_ratio={_aspect} 可用示例: {_resolution_examples(_aspect)}。"
                    "后端不会把 1k/2k/4k 自动换算成像素，也不会自动重试。"
                ),
                "suggested_next": "repair_resolution_then_rerun_original_node",
            })
        running_output = await _merge_stage_into_fusion(
            node_id, node_type,
            status="running",
            size=_size_preview,
            aspect_ratio=_aspect,
            prompt=str(fields.get("prompt") or ""),
            input_data=visible_fields,
        )
        await canvas_tools.update_node(
            node_id,
            {"status": "running", "error_message": None},
        )
        # 同时推一次画布事件,让节点立刻显示 skeleton(不等下一次轮询)
        await _emit_fusion_canvas_event(
            node_id, status="running", preview=running_output, project_id=project_id,
        )

        # ⭐ 同步阻塞出图:视频制作是线性的,LLM 必须等到图出来再继续。
        # 之前丢后台任务后立刻返回 queued,LLM 以为完成就跳到下一步,导致依赖图还没好就引用。
        # 现在改 await,期间 SSE 已经推 running fusion preview 给前端 skeleton 显示。
        try:
            result = await _render_image_node_once(project_id, node_id, fields, node_type)
        except (asyncio.CancelledError, GeneratorExit):
            err_text = "出图任务因连接中断被取消，请在原节点重试"
            fusion = await _merge_stage_into_fusion(
                node_id, node_type, status="failed", error=err_text,
                prompt=str(fields.get("prompt") or ""),
                input_data=visible_fields,
            )
            await canvas_tools.update_node(
                node_id, {"status": "failed", "error_message": err_text},
            )
            await _emit_fusion_canvas_event(
                node_id, status="failed",
                error=err_text, preview=fusion, project_id=project_id,
            )
            raise

        if isinstance(result, dict) and result.get("error"):
            diagnosis = _image_render_failure_diagnosis(
                result,
                fields,
                node_id=node_id,
                node_type=node_type,
                aspect_ratio=_aspect,
                requested_size=_size_preview,
            )
            fusion = await _merge_stage_into_fusion(
                node_id,
                node_type,
                status="failed",
                error=str(result["error"])[:300],
                size=result.get("size_final") or result.get("size_requested") or _size_preview,
                aspect_ratio=_aspect,
                quality=result.get("quality_final") or fields.get("quality"),
                prompt=str(fields.get("prompt") or ""),
                input_data=visible_fields,
                diagnostics=diagnosis,
            )
            await canvas_tools.update_node(
                node_id, {"status": "failed", "error_message": result["error"]},
            )
            await _emit_fusion_canvas_event(
                node_id, status="failed",
                error=result["error"], preview=fusion, project_id=project_id,
            )
            return _run_response({
                "ok": False,
                "error": result["error"],
                "type": node_type,
	                "hint": (
	                    "图片服务配置或鉴权错误，修改 provider 配置后再在原节点 render；不要原地重复调用。"
	                    if result.get("error_kind") in {"auth", "not_found"}
	                    else "出图失败,看 diagnosis 和 provider_msg/error 判断原因；需要改字段时用 node.update 修原节点后 render 重试。不要新建节点、不要跳过这一步。"
	                ),
                "diagnosis": diagnosis,
                "suggested_patch": diagnosis.get("suggested_patch"),
                "suggested_next": diagnosis.get("suggested_next"),
                **{
                    key: result.get(key)
                    for key in (
                        "error_kind", "error_source", "http_code", "provider_msg", "endpoint",
                        "provider", "model", "attempts", "node_render_attempts",
                        "size_requested", "size_final", "quality_requested",
                        "quality_final", "downgraded", "actual_size", "actual_aspect_ratio",
                        "requested_aspect_ratio",
                    )
                    if result.get(key) is not None
                },
            })

        # 关键修复:不再 output_data=result 整段覆盖。只 upsert "参考图/场景图/..."这一阶段,
        # 之前的"人物设定 / 提示词"等阶段保留在 fusion stages 里,刷新页面也不丢。
        fusion = await _merge_stage_into_fusion(
            node_id, node_type,
            status="completed",
            url=result.get("url") or result.get("local_url"),
            local_url=result.get("local_url"),
            remote_url=result.get("remote_url"),
            size=result.get("size") or result.get("size_final"),
            aspect_ratio=result.get("aspect_ratio"),
            quality=result.get("quality"),
            prompt=str(fields.get("prompt") or ""),
            input_data=visible_fields,
        )
        await canvas_tools.update_node(
            node_id,
            {
                "status": "completed",
                "error_message": None,
                "input_data": _with_image_render_state(node.get("input") if isinstance(node.get("input"), dict) else {}, "fresh"),
            },
        )
        await _emit_fusion_canvas_event(
            node_id, status="completed",
            preview=fusion, render_state="fresh", project_id=project_id,
        )
        _new_url = result.get("url") or result.get("local_url") or ""
        # Compare with old image if this node was previously rendered
        _old_output = node.get("output") or {}
        _old_stages = _old_output.get("stages") or []
        _old_url = ""
        for _stage in reversed(_old_stages):
            if isinstance(_stage, dict) and (_stage.get("url") or _stage.get("local_url")):
                _old_url = _stage.get("url") or _stage.get("local_url") or ""
                break
        if not _old_url:
            _old_url = _old_output.get("url") or _old_output.get("local_url") or ""
        _changes = []
        if _old_url and _new_url and _old_url != _new_url:
            _changes.append({"field": "image", "label": "图片", "before": _old_url, "after": _new_url})
        response = {
            "ok": True,
            "node_id": node_id,
            "type": node_type,
            "action": "render",
            "status": "completed",
            "url": _new_url,
            "local_url": result.get("local_url"),
            "remote_url": result.get("remote_url"),
            "result": result,
            "node_render_attempts": result.get("node_render_attempts"),
            "render_state": "fresh",
            "changes": _changes if _changes else None,
        }
        if review_recommendation:
            response.update(review_recommendation)
        return _run_response(response)

    # action=review:旧固定剧本类型已移除。
    if action == "review":
        return _run_response({
            "ok": False,
            "error": "action='review' 已移除。需要审稿时由模型创建/更新 text 节点或调用合适的只读 guide。",
            "error_kind": "unsupported_action",
            "node_type": node_type,
        })

    if node_type == "text" and _should_generate_workflow_text(fields, action):
        await canvas_tools.update_node(node_id, {"status": "running", "error_message": None})
        try:
            result = await asyncio.wait_for(
                _generate_workflow_text_node(
                    project_id=project_id,
                    node_id=node_id,
                    node=node,
                    fields=fields,
                ),
                timeout=_node_run_timeout_seconds(node_type),
            )
        except asyncio.TimeoutError:
            timeout_seconds = _node_run_timeout_seconds(node_type)
            err_text = f"text workflow LLM 超时({timeout_seconds}s)，请稍后重试"
            await canvas_tools.update_node(node_id, {"status": "failed", "error_message": err_text})
            return _run_response({
                "ok": False,
                "error": err_text,
                "error_kind": "timeout",
                "node_type": node_type,
            })
        except Exception as exc:
            err_text = f"text workflow LLM 异常: {exc}"
            await canvas_tools.update_node(node_id, {"status": "failed", "error_message": err_text})
            return _run_response({
                "ok": False,
                "error": err_text,
                "error_kind": "runner_exception",
                "node_type": node_type,
                "exception_type": exc.__class__.__name__,
            })
        if isinstance(result, dict) and result.get("error"):
            err_text = str(result.get("error") or "workflow text runner failed")
            await canvas_tools.update_node(node_id, {"status": "failed", "error_message": err_text})
            return _run_response({
                "ok": False,
                "error": err_text,
                "error_kind": result.get("error_kind") or "workflow_text_error",
                "node_type": node_type,
            })
        await canvas_tools.update_node(node_id, {"status": "completed", "error_message": None, "output_data": result})
        return _run_response({
            "ok": True,
            "node_id": node_id,
            "type": node_type,
            "status": "completed",
            "result": result,
        })

    runner = _RUNNERS.get(node_type)
    if runner is None:
        return _run_response({
            "ok": False,
            "error": f"节点类型 {node_type!r} 没有 runner",
            "node_type": node_type,
            "hint": (
                f"该类型节点没注册 runner,不能用 node.run 触发。"
                f"可用 runner 类型:{sorted(_RUNNERS.keys())}。"
                "如果只是想改字段,用 node.update;如果要删,用 canvas.delete。"
            ),
            "available_runners": sorted(_RUNNERS.keys()),
        })

    archived_output = None
    if node_type in {"image", "video", "audio"}:
        archived_output = await _archive_current_media_output_for_rerun(node_id, node, str(node_type), visible_fields)

    if action == "force":
        await canvas_tools.update_node(node_id, {"status": "idle", "error_message": None})
        if node_type == "video":
            fields["_force_new_video_generation"] = True

    await canvas_tools.update_node(node_id, {"status": "running", "error_message": None})
    try:
        result = await asyncio.wait_for(
            runner(project_id, node_id, fields),
            timeout=_node_run_timeout_seconds(node_type),
        )
    except (asyncio.CancelledError, GeneratorExit):
        err_text = f"{node_type} 准备阶段因连接中断被取消，请在原节点重试"
        await canvas_tools.update_node(
            node_id, {"status": "failed", "error_message": err_text},
        )
        raise
    except asyncio.TimeoutError:
        timeout_seconds = _node_run_timeout_seconds(node_type)
        err_text = f"{node_type} 准备阶段超时({timeout_seconds}s)，请稍后重试"
        await canvas_tools.update_node(
            node_id, {"status": "failed", "error_message": err_text},
        )
        return _run_response({
            "ok": False,
            "error": err_text,
            "error_kind": "timeout",
            "node_type": node_type,
            "hint": "外部模型响应超时。直接对原节点调用 node.run(force) 重试，不要新建节点。",
        })
    except Exception as exc:
        err_text = f"{node_type} runner 异常: {exc}"
        exc_name = exc.__class__.__name__
        await canvas_tools.update_node(
            node_id, {"status": "failed", "error_message": err_text},
        )
        _transient_names = {
            "TimeoutError", "ConnectionError", "RemoteDisconnected",
            "HTTPError", "HTTPStatusError", "ReadTimeout", "ConnectTimeout",
        }
        _is_transient = exc_name in _transient_names
        _is_value = exc_name in {"ValueError", "TypeError", "KeyError", "AttributeError"}
        return _run_response({
            "ok": False,
            "error": err_text,
            "error_kind": "server_error" if _is_transient else (
                "invalid_field" if _is_value else "runner_exception"
            ),
            "node_type": node_type,
            "exception_type": exc_name,
            "diagnosis": {
                "exception_type": exc_name,
                "is_transient": _is_transient,
                "is_value_error": _is_value,
                "node_type": node_type,
            },
            "suggested_patch": (
                {"action": "retry", "reason": "transient — retry the same node.run"}
                if _is_transient
                else {"action": "check_dependencies", "reason": "check upstream nodes"}
            ),
            "suggested_next": (
                "retry" if _is_transient
                else "satisfy_dependency" if not _is_value
                else "repair_arguments"
            ),
            "hint": (
                "外部 API 超时/5xx(server_error),根据 provider_msg/error 修改原节点参数或稍后重试。不要新建节点。"
                if _is_transient
                else "参数或依赖错误,检查输入字段和上游节点状态后重试。"
                if _is_value
                else "runner 抛异常,通常是缺少 prompt、参考资产或外部服务参数。"
                "先用 node.get 看当前节点 fields/output,缺啥先补啥再重试。"
                "不要原地反复 node.run 同一个节点。"
            ),
        })

    if node_type in {"video", "audio"} and isinstance(result, dict) and result.get("status") in {"queued", "running"}:
        result = media_history.preserve_media_history(result, archived_output)
        visible_result = _visible_payload(result)
        await canvas_tools.update_node(
            node_id,
            {"status": "running", "error_message": None, "output_data": visible_result},
        )
        try:
            from app.agent.orchestrator import emit_canvas_event
            await emit_canvas_event(
                {
                    "type": "canvas_action",
                    "action": "update_node",
                    "payload": {
                        "id": node_id,
                        "status": "running",
                        "output": visible_result,
                        "job_id": result.get("job_id"),
                    },
                },
                project_id=project_id,
            )
        except Exception:
            logger.exception("emit media queued canvas event failed for node %s", node_id)
        return _run_response({
            "ok": True,
            "type": node_type,
            "status": result.get("status"),
            "async": True,
            "job_id": result.get("job_id"),
            "result": visible_result,
        })

    if isinstance(result, dict) and result.get("error"):
        err_text = result["error"]
        await canvas_tools.update_node(
            node_id, {"status": "failed", "error_message": err_text},
        )
        return _run_response({
            "ok": False,
            "error": err_text,
            "node_type": node_type,
            "hint": result.get("hint") or (
                "runner 返回业务错误。检查节点 input 字段是否完整、依赖产物是否生成。"
                "用 node.get 看完整 input,node.list 看同 episode/segment 是否缺前置节点。"
            ),
            **{k: v for k, v in result.items() if k not in ("error", "hint", "ok")},
        })

    if node_type in {"image", "video", "audio"} and isinstance(result, dict):
        result = media_history.preserve_media_history(result, archived_output)
    visible_result = _visible_payload(result)
    await canvas_tools.update_node(
        node_id, {"status": "completed", "output_data": visible_result},
    )
    return _run_response({"node_id": node_id, "type": node_type, "result": visible_result})
