"""统一的节点 API — 模型只需 text/image/video/audio + 字段,后端只执行通用媒介能力。

Agent 只看到节点原语(node.create / get / update / delete / list / run),
内部委托给 canvas_tools 和 service-level media services 实现。

公开节点 type 只允许:
  text / image / video / audio
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.agent.blueprint_revision import create_pending_revision_from_node_patch
from app.config import settings
from app.db.models import Asset, WorkflowNode
from app.db.session import session_scope
from app.mcp_tools import canvas_tools
from app.services import media_generation, media_history
from sqlmodel import select

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


NODE_RUN_TIMEOUT_SECONDS = _env_int("DRAMA_NODE_RUN_TIMEOUT_SECONDS", 600, minimum=30)
IMAGE_RENDER_TIMEOUT_SECONDS = _env_int("DRAMA_IMAGE_RENDER_TIMEOUT_SECONDS", 300, minimum=60)
STALE_RUNNING_SECONDS = max(
    NODE_RUN_TIMEOUT_SECONDS,
    IMAGE_RENDER_TIMEOUT_SECONDS,
) + 60
NODE_LIST_DEFAULT_LIMIT = 20
NODE_LIST_MAX_LIMIT = 800

NODE_SURFACE_PROJECT_PANEL = "project_panel"
NODE_SURFACE_DRAFT_CANVAS = "draft_canvas"
_VALID_NODE_SURFACES = {NODE_SURFACE_PROJECT_PANEL, NODE_SURFACE_DRAFT_CANVAS}


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
            "reference_images", "references", "depends_on", "model", "seed",
            "purpose", "prompt_source",
        ],
        "description": "通用图片节点。模型必须自己写最终图片 prompt、aspect_ratio 和精确像素 resolution；后端只按 prompt/fields/references 调图片服务，不判断它是人物、场景、分镜、首尾帧或故事模板。",
    },
    "video": {
        "required": ["prompt"],
        "optional": [
            "title", "description", "duration_seconds", "aspect_ratio", "resolution",
            "reference_images", "references", "depends_on", "model",
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
            "references", "depends_on", "model",
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
                "创建 image 节点必须写 fields.aspect_ratio 和精确像素 fields.resolution；不要写 2k/4k/8k。16:9 常用 2560x1440，最高 3840x2160；9:16 常用 1440x2560，最高 2160x3840。",
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
    if isinstance(existing, str):
        try:
            existing = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            existing = None
    if not isinstance(existing, dict) or existing.get("type") != "fusion":
        existing = {"type": "fusion", "subject": subj, "stages": []}

    stages = [dict(s) for s in (existing.get("stages") or []) if isinstance(s, dict)]
    payload: dict[str, Any] = {"name": stage_name, "status": status}
    for k, v in (
        ("url", url), ("local_url", local_url), ("remote_url", remote_url),
        ("size", size), ("aspect_ratio", aspect_ratio), ("quality", quality),
        ("error", error),
        ("diagnostics", diagnostics),
    ):
        if v is not None:
            payload[k] = v

    found = False
    for i, s in enumerate(stages):
        if s.get("name") == stage_name:
            # 若 status=completed 且新 payload 没带 error → 清掉旧 error
            merged = {**s, **payload}
            if status == "completed":
                merged.pop("error", None)
                merged.pop("diagnostics", None)
            elif status == "running":
                if url is None and local_url is None and remote_url is None:
                    for key in ("url", "local_url", "remote_url"):
                        merged.pop(key, None)
                if error is None:
                    merged.pop("error", None)
                    merged.pop("diagnostics", None)
            stages[i] = merged
            found = True
            break
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
        fusion = media_history.attach_media_history(fusion, history)
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
    "16:9": "2560x1440",
    "9:16": "1440x2560",
    "1:1": "2048x2048",
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
    "model",
    "negative_prompt",
    "no_visual_references",
    "production_path",
    "prompt_source",
    "prompt_status",
    "prompt_review",
    "purpose",
    "quality",
    "reference_images",
    "references",
    "resolution",
    "seed",
    "source_image",
    "visual_prompt",
}

_REVIEW_PASSED_STATUSES = {"pass", "passed", "approved", "ok", "true"}


def _resolution_examples(aspect_ratio: str | None) -> str:
    aspect = (aspect_ratio or "16:9").strip()
    primary = _COMMON_RESOLUTION_BY_ASPECT.get(aspect)
    examples = [v for v in [primary, "2560x1440", "1440x2560", "2048x2048"] if v]
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
    """Validate and return an exact provider size such as ``2560x1440``.

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
            "fields.resolution 必须是精确像素尺寸 '<width>x<height>'，不要写 2k/4k/8k 这种档位；"
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
            "并且与 fields.aspect_ratio 匹配；例如 16:9 写 2560x1440 或 3840x2160，"
            "9:16 写 1440x2560 或 2160x3840。不要写 2k/4k/8k。"
        ),
        "model_feedback": {
            "what_went_wrong": "图片节点分辨率字段不是后端可执行的精确像素值。",
            "how_to_fix": (
                "使用 node.update 修正原节点 input_json.resolution，例如 "
                "{\"resolution\":\"2560x1440\"}，再对同一个节点调用 node.run。"
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
    if not prefixed and not _looks_like_bare_workflow_node_id(raw_node_id):
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
    warning = "" if prefixed else f"reference_images 已将裸节点 ID {text} 规范化为 node:{node_id}"
    return f"node:{node_id}", warning, True


async def _normalize_reference_images_for_render(
    project_id: str,
    refs: Any,
) -> tuple[list[str], list[str]]:
    """Accept common reference asset identifiers and turn them into renderable inputs.

    The provider layer accepts local storage-relative paths, absolute paths,
    URLs, asset:<id>, and node:<image_id>. Agents often hold @mentions/ref_id from
    reference.manage, so normalize those here instead of letting the render
    fail before the image provider is reached.
    """
    if not isinstance(refs, list):
        return [], []
    state = await _read_project_state(project_id)
    store = state.get("reference_assets") if isinstance(state, dict) else None
    assets = store.get("assets") if isinstance(store, dict) and isinstance(store.get("assets"), list) else []
    lookup: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        reference_input = _reference_input_for_state_asset(asset)
        if not reference_input:
            continue
        for key in (
            asset.get("ref_id"),
            asset.get("mention"),
            asset.get("label"),
            asset.get("filename"),
            *(
                asset.get("aliases")
                if isinstance(asset.get("aliases"), list)
                else []
            ),
        ):
            _add_reference_lookup(lookup, key, reference_input)

    normalized: list[str] = []
    warnings: list[str] = []
    for raw in refs:
        if isinstance(raw, dict):
            candidate = (
                raw.get("reference_input")
                or raw.get("rel_path")
                or raw.get("source_path")
                or raw.get("url")
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
        replacement = lookup.get(text) or lookup.get(text.lstrip("@"))
        if replacement:
            if replacement != text:
                warnings.append(f"reference_images 已将 {text} 解析为 {replacement}")
            text = replacement
        elif text.startswith("ref_") or text.startswith("@"):
            warnings.append(f"reference_images 未能解析 {text};请使用 reference.manage(action='resolve') 返回的 reference_input")
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
        for key in (
            node.id,
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
        if text.startswith(("http://", "https://", "asset:")):
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
        fields.get("depends_on"),
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
    return Path(getattr(settings, "STORAGE_PATH", "./storage")).resolve()


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
        example_fields["aspect_ratio"] = "16:9"
        example_fields["resolution"] = "2560x1440"

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

    # Gate 1 + 2:模式守卫。业务流程由 skill.video_production 承接。
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

    return node


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
            if node.get("id"):
                client_node_ids[client_ref] = str(node["id"])
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
        "client_node_ids": client_node_ids,
        "next_action": "需要生成产物时按依赖顺序调用 node.run；需要修字段时批量或单个调用 node.update。",
    }


async def _auto_connect_topology(project_id: str, node_id: str, node_type: str, fields: dict) -> None:
    """Create explicit dependency edges from model-authored reference fields."""
    nodes = await canvas_tools.list_nodes(project_id)
    if not isinstance(nodes, list):
        return
    node_by_id = {str(n.get("id")): n for n in nodes if isinstance(n, dict) and n.get("id")}

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
        if "/" in text or text.startswith(("asset:", "upload:", "http://", "https://")):
            continue
        if text in node_by_id:
            await _link(text)


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
        if text and text not in normalized:
            normalized.append(text)
    return normalized


async def _node_get_one(node_id: str, project_id: str = "") -> dict:
    node = await canvas_tools.get_node(node_id)
    if isinstance(node, dict) and node.get("error") == "Node not found":
        return {
            "ok": False,
            "error": "Node not found",
            "error_kind": "node_not_found",
            "node_id": node_id,
            "hint": "node_id 必须是 node.create 返回的真实节点 id，不是 shot_id、segment_id、标题或别名。新任务没有节点时先创建合适的 text/image/video/audio 节点。",
        }
    if project_id and isinstance(node, dict) and str(node.get("project_id") or "") != project_id:
        return {
            "ok": False,
            "error": "Node does not belong to this project",
            "error_kind": "node_project_mismatch",
            "node_id": node_id,
            "project_id": project_id,
        }
    return node


async def node_get(
    node_id: str = "",
    project_id: str = "",
    node_ids: list[str] | str | None = None,
) -> dict:
    ids = _normalize_node_id_list(node_id, node_ids)
    if not ids:
        return {
            "ok": False,
            "error": "node_id or node_ids is required",
            "error_kind": "missing_node_id",
            "hint": "先用 node.list 获取真实节点 id；需要多个详情时一次传 node_ids。",
        }
    if node_ids is None and len(ids) == 1:
        return await _node_get_one(ids[0], project_id=project_id)

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
            "hint": "node_ids 必须来自 node.list 返回的真实 id。",
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
    return any(_field_filled(stage.get(k)) for k in ("url", "local_url", "remote_url"))


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
        for key in ("url", "local_url", "remote_url"):
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
            for key in ("url", "local_url", "remote_url"):
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
                    "{\"input_json\":{\"resolution\":\"2560x1440\"}}。"
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


async def _node_update_one(node_id: str, patch: dict | str | None) -> dict:
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

    node = await canvas_tools.get_node(node_id)
    if node.get("error"):
        return node

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
    if canvas_patch:
        input_field_patch_requested = "input_json" in canvas_patch or "input_data" in canvas_patch
        canvas_patch = _merge_input_patch_with_current(node, canvas_patch)
        canvas_patch = _sync_title_patch_with_input(node, canvas_patch)
        canvas_patch = _sync_prompt_patch_with_input(node, canvas_patch)
        if node.get("type") == "image":
            next_input = canvas_patch.get("input_json")
            if not isinstance(next_input, dict):
                next_input = canvas_patch.get("input_data")
            if isinstance(next_input, dict):
                next_prompt = str(canvas_patch.get("prompt") if "prompt" in canvas_patch else node.get("prompt") or "")
                if _image_render_inputs_changed(_old_input, next_input, _old_prompt, next_prompt):
                    canvas_patch["input_json"] = _with_image_render_state(next_input, "stale")
                    canvas_patch.pop("input_data", None)
                    image_render_marked_stale = True
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

    return canvas_result


async def node_update(
    node_id: str = "",
    patch: dict | str | None = None,
    updates: list[dict] | None = None,
    node_ids: list[str] | str | None = None,
) -> dict:
    """局部修改一个或多个节点。"""
    if updates is None and node_ids is not None:
        ids = _normalize_node_id_list(node_id, node_ids)
        updates = [{"node_id": item_id, "patch": patch} for item_id in ids]

    if updates is None:
        return await _node_update_one(node_id, patch)

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
        result = await _node_update_one(item_node_id, item_patch)
        if _node_tool_error(result):
            error = dict(result)
            error["index"] = index
            if item_node_id:
                error["node_id"] = item_node_id
            errors.append(error)
            continue
        updated = dict(result)
        updated["index"] = index
        updated.setdefault("node_id", item_node_id or updated.get("id"))
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


async def node_delete(node_id: str, cascade: bool = True) -> dict:
    """删除通用节点。cascade 参数仅保留为工具入参，不触发业务类型级联。"""
    node = await canvas_tools.get_node(node_id)
    if node.get("error"):
        return node

    return await canvas_tools.delete_node(node_id)


def _node_search_blob(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("id", "title", "type", "status", "prompt", "error_message"):
        if node.get(key):
            parts.append(str(node.get(key)))
    for key in ("input", "output"):
        value = node.get(key)
        if value:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
            except TypeError:
                parts.append(str(value))
    return "\n".join(parts).lower()


def _node_list_index_item(node: dict[str, Any], *, match_hint: bool = False) -> dict[str, Any]:
    prompt_text = str(node.get("prompt") or "")
    item: dict[str, Any] = {
        "id": node.get("id"),
        "node_id": node.get("id"),
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
        "created_at",
        "updated_at",
    ):
        value = node.get(key)
        if value not in (None, "", [], {}):
            item[key] = value
    if prompt_text:
        item["prompt_chars"] = len(prompt_text)
    if match_hint:
        item["match_hint"] = "这是 query 匹配到的候选节点；后续 node.get/node.run 必须使用 id 字段。"
    return item


async def node_list(
    project_id: str,
    type: str | None = None,
    status: str | None = None,
    surface: str | None = None,
    query: str | None = None,
    limit: int | None = NODE_LIST_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """列出项目节点索引；默认截断，可用 limit=0 明确读取全部。

    query 用于用户说“那张图/某标题/某描述”时先找候选节点，不能把
    query 文本当 node_id 直接传给 node.get/node.run。
    """
    nodes = await canvas_tools.list_nodes(project_id)
    if type:
        nodes = [n for n in nodes if n.get("type") == type]
    if status:
        nodes = [n for n in nodes if n.get("status") == status]
    if surface:
        nodes = [n for n in nodes if _node_surface(n) == surface]
    if query:
        needle = str(query).strip().lower()
        if needle:
            nodes = [n for n in nodes if needle in _node_search_blob(n)]
    limit_int: int | None = None
    try:
        if limit in (0, "0"):
            parsed_limit = 0
        elif limit in (None, ""):
            parsed_limit = NODE_LIST_DEFAULT_LIMIT
        else:
            parsed_limit = int(limit)
    except (TypeError, ValueError):
        parsed_limit = NODE_LIST_DEFAULT_LIMIT
    if parsed_limit > 0:
        limit_int = min(parsed_limit, NODE_LIST_MAX_LIMIT)
    total = len(nodes)
    if limit_int is not None:
        nodes = nodes[:limit_int]
    index_nodes = [_node_list_index_item(node, match_hint=bool(query)) for node in nodes]
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
    else:
        next_input.pop("prompt", None)
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
    next_patch["input_json"] = next_input
    next_patch.pop("input_data", None)
    return next_patch


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
    "model",
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

    prompt = _visual_prompt_from_fields(f)
    if prompt and prompt != f.get("prompt"):
        f = await _persist_prompt_to_node(node_id, f, prompt)
    if not prompt:
        return {
            "error": "image 节点缺 prompt，无法出图。请由模型读取需要的 skill/template 后写入最终图片 prompt。",
            "error_kind": "missing_prompt",
            "node_id": node_id,
            "node_type": node_type,
        }

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
    if reference_images != (f.get("reference_images") or []):
        f = {**f, "reference_images": reference_images}
        try:
            await canvas_tools.update_node(node_id, {"input_json": f})
        except Exception:
            logger.exception("persist normalized reference_images failed for node %s", node_id)

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

    image_result = await media_generation.generate_image(
        project_id=project_id, prompt=prompt,
        aspect_ratio=aspect,
        size=size,
        quality=quality,
        node_id=node_id, model=resolved_model,
        reference_images=f.get("reference_images"),
    )

    merged = _merge_image_output({}, image_result, prompt, f)
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
    content = str(f.get("content") or f.get("description") or "").strip()
    return {
        "type": "text",
        "title": f.get("title"),
        "content": content,
        "references": f.get("references") or [],
        "depends_on": f.get("depends_on") or [],
    }


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
    if reference_images != (f.get("reference_images") or []):
        f = {**f, "reference_images": reference_images}
        try:
            await canvas_tools.update_node(node_id, {"input_json": f})
        except Exception:
            logger.exception("persist video reference_images failed for node %s", node_id)
    video_extra = {
        key: f[key]
        for key in (
            "ratio",
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
    result = await media_generation.generate_video(
        project_id=project_id,
        prompt=prompt,
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
    )
    if isinstance(result, dict):
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
) -> dict:
    """跑这个节点。后端按 type 自动派发,自动管 status/产物落库。

    image/video/audio 节点必须已经有模型写入的 prompt。后端只执行,不合成业务提示词。

    action 选项:
      None / "run":  默认 — text 保存内容,image 出图,video 生成视频,audio 生成音频
      "render":      仅 image — 用节点 prompt+参数出图。失败/不满意可先 node.update 改参数再 render。
      "force":       忽略已 completed 状态强制重跑准备阶段
    extra_fields:    临时补字段(不写回 input),render 时可临时替换 prompt 等
    """
    node = await canvas_tools.get_node(node_id)
    if node.get("error"):
        return {
            "ok": False,
            "error": node.get("error") or "Node not found",
            "error_kind": "node_not_found",
            "node_id": node_id,
                "hint": "node.run 的 node_id 必须来自已存在节点。不要把 shot_id/segment_id/标题当 node_id；如果是新任务，先 node.create 创建合适节点，再 node.run。",
        }
    node_type = node.get("type")
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
            return {
                "ok": True,
                "node_id": node_id,
                "type": node_type,
                "status": "completed",
                "render_state": "fresh",
                "url": recovered_url,
                "result": node.get("output"),
                "recovered_from_running_output": True,
            }

    # 通用字段拼装
    fields: dict = dict(node.get("input") or {})
    node_prompt = str(node.get("prompt") or "").strip()
    if node_prompt:
        fields["prompt"] = node_prompt
    extra = _coerce_dict(extra_fields, "extra_fields")
    if extra:
        fields.update(extra)

    if node_type == "image" and _image_operation_name(fields) and action in {None, "run", "force"}:
        archived_output = await _archive_current_media_output_for_rerun(node_id, node, str(node_type), fields)
        if action == "force":
            await canvas_tools.update_node(node_id, {"status": "idle", "error_message": None})
        await canvas_tools.update_node(node_id, {"status": "running", "error_message": None})
        try:
            result = await _run_image_node(project_id, node_id, fields)
        except Exception as exc:
            err_text = f"image operation failed: {exc}"
            await canvas_tools.update_node(node_id, {"status": "failed", "error_message": err_text})
            return {
                "ok": False,
                "error": err_text,
                "error_kind": exc.__class__.__name__,
                "node_id": node_id,
                "node_type": node_type,
            }
        if isinstance(result, dict) and result.get("error"):
            await canvas_tools.update_node(
                node_id,
                {"status": "failed", "error_message": result.get("error")},
            )
            return {
                "ok": False,
                "error": result.get("error"),
                "node_id": node_id,
                "node_type": node_type,
                **{k: v for k, v in result.items() if k not in {"ok", "error"}},
            }
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
        return {
            "ok": True,
            "node_id": node_id,
            "type": node_type,
            "action": _image_operation_name(fields),
            "status": "completed",
            "render_state": "fresh",
            "result": result,
        }

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
            return {
                "ok": False,
                "error": f"action='render' 不支持 type={node_type!r}",
                "node_id": node_id,
                "node_type": node_type,
                "hint": (
                    f"render 仅用于 image 节点。"
                    f"对 {node_type!r},应改用:{_alt}。不要原地重试 render。"
                ),
                "renderable_types": sorted(_RENDERABLE),
                "suggested_next": _alt,
            }

        # 先把节点状态改 running,**同时**在 output_json 里写一个 running 的图片 stage,
        # 这样前端 SmartNode 的 StageImage 能渲染 skeleton(spinner + shimmer)占位,
        # 而不是只看到节点级 "生成中…" 文本,跟最终出图后的版面也保持一致。
        _subj, _stage_name = _SUBJECT_BY_TYPE.get(node_type, (node_type, "图片"))
        archived_output = await _archive_current_media_output_for_rerun(node_id, node, str(node_type), fields)
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
                input_data=fields,
            )
            await _emit_fusion_canvas_event(
                node_id, status="failed",
                error=err_text, preview=fusion, project_id=project_id,
            )
            return {
                "ok": False,
                "error": err_text,
                "error_kind": "invalid_resolution",
                "node_id": node_id,
                "node_type": node_type,
                "hint": (
                    "用 node.update 修改原节点 fields.resolution 为精确像素尺寸后重试；"
                    f"aspect_ratio={_aspect} 可用示例: {_resolution_examples(_aspect)}。"
                    "后端不会把 2k/4k 自动换算成像素，也不会自动重试。"
                ),
                "suggested_next": "repair_resolution_then_rerun_original_node",
            }
        running_output = await _merge_stage_into_fusion(
            node_id, node_type,
            status="running",
            size=_size_preview,
            aspect_ratio=_aspect,
            prompt=str(fields.get("prompt") or ""),
            input_data=fields,
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
                input_data=fields,
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
                input_data=fields,
                diagnostics=diagnosis,
            )
            await canvas_tools.update_node(
                node_id, {"status": "failed", "error_message": result["error"]},
            )
            await _emit_fusion_canvas_event(
                node_id, status="failed",
                error=result["error"], preview=fusion, project_id=project_id,
            )
            return {
                "ok": False,
                "error": result["error"],
                "node_id": node_id,
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
                        "quality_final", "downgraded",
                    )
                    if result.get(key) is not None
                },
            }

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
            input_data=fields,
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
        return response

    # action=review:旧固定剧本类型已移除。
    if action == "review":
        return {
            "ok": False,
            "error": "action='review' 已移除。需要审稿时由模型创建/更新 text 节点或调用合适的只读 guide。",
            "error_kind": "unsupported_action",
            "node_id": node_id,
            "node_type": node_type,
        }

    runner = _RUNNERS.get(node_type)
    if runner is None:
        return {
            "ok": False,
            "error": f"节点类型 {node_type!r} 没有 runner",
            "node_id": node_id,
            "node_type": node_type,
            "hint": (
                f"该类型节点没注册 runner,不能用 node.run 触发。"
                f"可用 runner 类型:{sorted(_RUNNERS.keys())}。"
                "如果只是想改字段,用 node.update;如果要删,用 canvas.delete。"
            ),
            "available_runners": sorted(_RUNNERS.keys()),
        }

    archived_output = None
    if node_type in {"image", "video", "audio"}:
        archived_output = await _archive_current_media_output_for_rerun(node_id, node, str(node_type), fields)

    if action == "force":
        await canvas_tools.update_node(node_id, {"status": "idle", "error_message": None})

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
        return {
            "ok": False,
            "error": err_text,
            "error_kind": "timeout",
            "node_id": node_id,
            "node_type": node_type,
            "hint": "外部模型响应超时。直接对原节点调用 node.run(force) 重试，不要新建节点。",
        }
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
        return {
            "ok": False,
            "error": err_text,
            "error_kind": "server_error" if _is_transient else (
                "invalid_field" if _is_value else "runner_exception"
            ),
            "node_id": node_id,
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
        }

    if node_type in {"video", "audio"} and isinstance(result, dict) and result.get("status") in {"queued", "running"}:
        result = media_history.preserve_media_history(result, archived_output)
        await canvas_tools.update_node(
            node_id,
            {"status": "running", "error_message": None, "output_data": result},
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
                        "output": result,
                        "job_id": result.get("job_id"),
                    },
                },
                project_id=project_id,
            )
        except Exception:
            logger.exception("emit media queued canvas event failed for node %s", node_id)
        return {
            "ok": True,
            "node_id": node_id,
            "type": node_type,
            "status": result.get("status"),
            "async": True,
            "job_id": result.get("job_id"),
            "result": result,
        }

    if isinstance(result, dict) and result.get("error"):
        err_text = result["error"]
        await canvas_tools.update_node(
            node_id, {"status": "failed", "error_message": err_text},
        )
        return {
            "ok": False,
            "error": err_text,
            "node_id": node_id,
            "node_type": node_type,
            "hint": result.get("hint") or (
                "runner 返回业务错误。检查节点 input 字段是否完整、依赖产物是否生成。"
                "用 node.get 看完整 input,node.list 看同 episode/segment 是否缺前置节点。"
            ),
            **{k: v for k, v in result.items() if k not in ("error", "hint", "ok")},
        }

    if node_type in {"image", "video", "audio"} and isinstance(result, dict):
        result = media_history.preserve_media_history(result, archived_output)
    await canvas_tools.update_node(
        node_id, {"status": "completed", "output_data": result},
    )
    return {"node_id": node_id, "type": node_type, "result": result}
