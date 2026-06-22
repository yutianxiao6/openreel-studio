"""Video blueprint intake state helpers.

This module only tracks structured intake cards and pending intake state. It
does not generate blueprints, approve plans, create nodes, or run media.
"""
from __future__ import annotations

import re
import time
from typing import Any

from app.agent.blueprint_confirmation_state import pending_blueprint_plan

_FACT_FIELD_ALIASES: dict[str, set[str]] = {
    "topic": {"theme", "topic", "brief", "core_event", "content_focus", "subject", "story"},
    "style": {"style", "visual_style", "look", "tone", "aesthetic"},
    "video_type": {"video_type", "content_type", "format", "genre"},
    "duration_seconds": {"duration_seconds", "duration", "total_duration", "length", "seconds"},
    "aspect_ratio": {"aspect_ratio", "format", "ratio", "video_aspect_ratio", "canvas_ratio"},
    "scene": {"scene", "court_scene", "location", "environment", "setting"},
    "character": {"character", "protagonist", "person", "subject_character"},
    "plot_outline": {"plot_outline", "outline", "story_outline", "剧情大纲"},
    "episode_count": {"episode_count", "episodes"},
    "segment_seconds": {"segment_seconds", "segment_duration", "segmentation", "segments"},
    "production_basis": {
        "production_basis",
        "generation_basis",
        "video_basis",
        "reference_basis",
        "production_path_preference",
        "basis",
        "generation_method",
        "production_method",
        "video_method",
    },
}

_FACT_LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "topic": ("主题", "核心事件", "主要拍什么", "内容"),
    "style": ("风格", "视觉", "调性"),
    "video_type": ("类型",),
    "duration_seconds": ("时长", "总时长", "秒"),
    "aspect_ratio": ("画幅", "比例", "横屏", "竖屏"),
    "scene": ("场景", "环境", "球场", "地点"),
    "character": ("人物", "角色", "主角", "形象"),
    "plot_outline": ("剧情", "大纲"),
    "episode_count": ("集数",),
    "segment_seconds": ("分段", "单段"),
    "production_basis": ("生成依据", "文生", "图生", "参考图", "分镜图"),
}


def _canonical_fact_id(field_id: Any, label: Any = "") -> str:
    raw_id = str(field_id or "").strip()
    key = raw_id.lower().replace("-", "_").replace(" ", "_")
    for canonical, aliases in _FACT_FIELD_ALIASES.items():
        if key == canonical or key in aliases:
            return canonical
    label_text = str(label or "").strip()
    if label_text:
        for canonical, keywords in _FACT_LABEL_KEYWORDS.items():
            if any(keyword in label_text for keyword in keywords):
                return canonical
    return key


def canonical_video_intake_field_id(field_id: Any, label: Any = "") -> str:
    """Return the stable collected-fact key for an intake question."""
    return _canonical_fact_id(field_id, label)


def _fact_value(item: dict[str, Any]) -> str:
    raw_value = item.get("raw_value")
    value = raw_value if raw_value not in (None, "", [], {}) else item.get("value")
    return _short_text(value, 240)


def _model_delegation_value(value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    if normalized in {
        "model_decide",
        "model decide",
        "model_planning",
        "model planning",
        "模型规划",
        "模型发挥",
        "由模型判断",
        "模型判断",
        "模型决定",
        "你决定",
        "你看着办",
        "随便",
        "我不知道",
    }:
        return "model_decide"
    return ""


def _merge_collected_facts(pending: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return pending
    facts = dict(pending.get("collected_facts") if isinstance(pending.get("collected_facts"), dict) else {})
    fact_sources = dict(pending.get("collected_fact_sources") if isinstance(pending.get("collected_fact_sources"), dict) else {})
    for item in items:
        if not isinstance(item, dict):
            continue
        canonical = _canonical_fact_id(item.get("id"), item.get("label"))
        if not canonical or canonical in {"action", "target", "production_mode", "selected_mode", "selected_video_mode"}:
            continue
        value = _fact_value(item)
        delegated = _model_delegation_value(value)
        if delegated:
            value = delegated
        if not value or value == "__custom__":
            continue
        if canonical == "aspect_ratio" and value not in {"16:9", "9:16", "model_decide"}:
            continue
        facts[canonical] = value
        fact_sources[canonical] = {
            "question_id": item.get("id"),
            "label": item.get("label"),
        }
    if not facts:
        return pending
    return {
        **pending,
        "collected_facts": facts,
        "collected_fact_sources": fact_sources,
    }


def collected_video_intake_facts_from_state(state: dict[str, Any] | None) -> dict[str, Any]:
    pending = state.get("pending_video_blueprint_request") if isinstance(state, dict) else None
    if not isinstance(pending, dict):
        return {}
    facts = pending.get("collected_facts")
    return dict(facts) if isinstance(facts, dict) else {}

def _attachment_image_references(attachments: list[dict] | None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    image_index = 0
    for index, attachment in enumerate(attachments or [], start=1):
        if not isinstance(attachment, dict) or attachment.get("kind") != "image":
            continue
        rel_path = str(attachment.get("rel_path") or "").strip()
        if not rel_path:
            continue
        image_index += 1
        raw_label = (
            attachment.get("mention")
            or attachment.get("ref_label")
            or attachment.get("reference_label")
            or attachment.get("display_label")
            or f"图{image_index}"
        )
        mention = str(raw_label or "").strip()
        if not mention.startswith("@"):
            mention = f"@{mention}"
        refs.append({
            "source": "upload",
            "usage": "visual_reference",
            "label": mention.lstrip("@"),
            "mention": mention,
            "rel_path": rel_path,
            "filename": attachment.get("filename") or "",
            "mime_type": attachment.get("mime_type"),
            "size": attachment.get("size"),
            "attachment_id": attachment.get("attachment_id") or attachment.get("id"),
            "message_attachment_index": index,
        })
    return refs


def _merge_pending_reference_images(pending: dict[str, Any], attachments: list[dict] | None) -> dict[str, Any]:
    incoming = _attachment_image_references(attachments)
    if not incoming:
        return pending
    existing = pending.get("reference_images") if isinstance(pending.get("reference_images"), list) else []
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("rel_path") or item.get("mention") or item.get("filename") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))
    return {
        **pending,
        "reference_images": merged,
        "reference_image_policy": (
            "这些是用户在蓝图阶段上传的视觉参考图；后续人物、场景、分镜、关键帧、视觉资产和视频提示词"
            "应在不改写 rel_path/mention 的前提下按用户描述引用。"
        ),
    }


def _intake_field_default(intake: dict[str, Any] | None, *field_ids: str) -> Any:
    if not isinstance(intake, dict):
        return None
    values = intake.get("values")
    if isinstance(values, dict):
        for field_id in field_ids:
            value = values.get(field_id)
            if value not in (None, "", [], {}):
                return value
    return None


def _intake_default_duration(intake: dict[str, Any] | None, fallback: int) -> int:
    raw = _intake_field_default(intake, "duration_seconds", "duration", "total_duration")
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(raw or ""))
        if match:
            try:
                return max(1, int(float(match.group(0))))
            except ValueError:
                pass
        return max(1, int(fallback or 15))


def _short_text(value: Any, limit: int = 240) -> str:
    if isinstance(value, (list, tuple, set)):
        text = "、".join(str(item) for item in value if item not in (None, "", [], {}))
    elif isinstance(value, dict):
        parts = [f"{key}={val}" for key, val in value.items() if val not in (None, "", [], {})]
        text = "；".join(parts)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _intake_values(intake: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(intake, dict):
        return {}
    values = intake.get("values")
    return values if isinstance(values, dict) else {}


def _selected_option_label(question: dict[str, Any], raw_value: str) -> str:
    for option in question.get("options") or []:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or "").strip()
        if label == raw_value:
            return label
    return ""


def _structured_answer_items(intake: dict[str, Any] | None) -> list[dict[str, Any]]:
    values = _intake_values(intake)
    if not values:
        return []
    questions = intake.get("questions") if isinstance(intake, dict) and isinstance(intake.get("questions"), list) else []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "").strip()
        if not question_id or question_id in seen:
            continue
        raw = values.get(question_id)
        if raw in (None, "", [], {}):
            continue
        raw_text = _short_text(raw)
        if not raw_text or raw_text == "__custom__":
            continue
        selected_label = _selected_option_label(question, raw_text)
        display_value = selected_label or raw_text
        item: dict[str, Any] = {
            "id": question_id,
            "label": _short_text(question.get("header") or question.get("question") or question_id, 80),
            "value": _short_text(display_value),
        }
        items.append(item)
        seen.add(question_id)
        if len(items) >= 12:
            return items
    return items


def _structured_answer_summary(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items[:12]:
        label = _short_text(item.get("label") or item.get("id") or "字段", 80)
        value = _short_text(item.get("value") or "", 240)
        if not value:
            continue
        raw_value = _short_text(item.get("raw_value") or "", 80)
        suffix = f"({raw_value})" if raw_value and raw_value != value else ""
        lines.append(f"{label}：{value}{suffix}")
    return "\n".join(lines)[:1200]


def _apply_structured_answer(
    pending: dict[str, Any],
    stage: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not items:
        return pending
    summary = _structured_answer_summary(items)
    if not summary:
        return pending
    next_pending = {
        **pending,
        "last_submitted_stage": stage,
        f"{stage}_answer": summary,
        f"{stage}_answers": items,
    }
    next_pending = _merge_collected_facts(next_pending, items)
    if stage == "structure":
        next_pending["mode_selection_policy"] = (
            "表单答案只作为用户偏好和约束；模型通过 start/append/finalize 蓝图草稿工具，在树、节点字段、references 和 depends_on 中表达制作方法。"
        )
    return next_pending


def video_intake_state_patch_for_interaction(
    state: dict[str, Any],
    message: str,
    attachments: list[dict] | None,
    stage: str,
    intake: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist video-blueprint intake progress after a structured intake card."""
    normalized_stage = str(stage or "").strip()
    pending = state.get("pending_video_blueprint_request") if isinstance(state, dict) else None
    now = int(time.time())
    duration = _intake_default_duration(intake, 15)
    structured_items = _structured_answer_items(intake)

    if normalized_stage in {"basic", "video_basic"}:
        if isinstance(pending, dict) and str(pending.get("stage") or "basic") == "basic":
            duration = _intake_default_duration(intake, pending.get("duration_seconds") or duration)
            next_pending = {
                **pending,
                "duration_seconds": duration,
                "updated_at": now,
            }
        else:
            if pending_blueprint_plan(state):
                return {}
            next_pending = {
                "stage": "basic",
                "raw_request": message,
                "duration_seconds": duration,
                "created_at": now,
            }
        next_pending = _apply_structured_answer(next_pending, "basic", structured_items)
        next_pending = _merge_pending_reference_images(next_pending, attachments)
        return {"pending_video_blueprint_request": next_pending}

    if normalized_stage in {"structure", "video_structure"}:
        if isinstance(pending, dict) and str(pending.get("stage") or "basic") == "basic":
            duration = _intake_default_duration(intake, pending.get("duration_seconds") or duration)
            next_pending = {
                **pending,
                "stage": "structure",
                "duration_seconds": duration,
                "updated_at": now,
            }
            if structured_items:
                if not next_pending.get("basic_answer"):
                    next_pending["basic_answer"] = _short_text(message, 1200)
                next_pending = _apply_structured_answer(next_pending, "structure", structured_items)
            else:
                next_pending["basic_answer"] = message
            next_pending = _merge_pending_reference_images(next_pending, attachments)
            return {"pending_video_blueprint_request": next_pending}
        if isinstance(pending, dict) and str(pending.get("stage") or "") == "structure":
            duration = _intake_default_duration(intake, pending.get("duration_seconds") or duration)
            next_pending = {
                **pending,
                "duration_seconds": duration,
                "updated_at": now,
            }
            next_pending = _apply_structured_answer(next_pending, "structure", structured_items)
            next_pending = _merge_pending_reference_images(next_pending, attachments)
            return {"pending_video_blueprint_request": next_pending}

        next_pending = {
            "stage": "structure",
            "raw_request": message,
            "duration_seconds": duration,
            "created_at": now,
        }
        if structured_items:
            next_pending = _apply_structured_answer(next_pending, "structure", structured_items)
        else:
            next_pending["basic_answer"] = message
        next_pending = _merge_pending_reference_images(next_pending, attachments)
        return {"pending_video_blueprint_request": next_pending}
    return {}
