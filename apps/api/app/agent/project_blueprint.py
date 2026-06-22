"""Project Blueprint persistence and state helpers.

DEPRECATED (2026-06): BLUEPRINT_SECTION_ORDER, BLUEPRINT_OUTPUT_CONTRACTS,
and section-based blueprint generation are replaced by the tree-based blueprint
(blueprint_tree.py + blueprint_materializer.py).  This module is kept for
backward compatibility reading old blueprint_draft.json files.

The blueprint is the project-level creative source of truth. Chat history,
pending plans, compact summaries, and canvas nodes are derived from it; they do
not replace it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from app.agent.blueprint_validator import validate_blueprint_document
from app.agent.storyboard_layout import STORYBOARD_DENSITY_RULE, normalize_storyboard_layout, storyboard_grid_label
from app.config import settings


BLUEPRINT_JSON_NAME = "blueprint.json"
BLUEPRINT_MD_NAME = "blueprint.md"
BLUEPRINT_DRAFT_JSON_NAME = "blueprint_draft.json"
BLUEPRINT_DRAFT_MD_NAME = "blueprint_draft.md"
BLUEPRINT_REVISION_JSON_NAME = "blueprint_revision_draft.json"
BLUEPRINT_REVISION_MD_NAME = "blueprint_revision_draft.md"
BLUEPRINT_VIEW_MODEL_JSON_NAME = "blueprint_view_model.json"
UNTITLED_PROJECT_TITLE = "未命名项目"
BLUEPRINT_SECTION_ORDER = (
    "production_spec",
    "global_story_outline",
    "visual_style",
    "character_bible",
    "scene_bible",
    "episode_scripts",
    "segment_breakdown",
)
BLUEPRINT_SECTION_TITLES = {
    "production_spec": "制作模式",
    "global_story_outline": "故事大纲",
    "episode_index": "剧集概要",
    "visual_style": "视觉风格",
    "character_bible": "人物设定",
    "scene_bible": "场景设定",
    "episode_scripts": "分集剧情",
    "segment_breakdown": "分段剧情",
    "outline_document": "整体大纲",
}


def project_data_dir(project_id: str, *, create: bool = True, root: str = "data") -> Path:
    path = Path(settings.PROJECT_ROOT) / root / "projects" / project_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def blueprint_paths(project_id: str, *, root: str = "data") -> dict[str, str]:
    base = Path(settings.PROJECT_ROOT)
    project_dir = project_data_dir(project_id, create=False, root=root)
    json_path = project_dir / BLUEPRINT_JSON_NAME
    md_path = project_dir / BLUEPRINT_MD_NAME
    draft_json_path = project_dir / BLUEPRINT_DRAFT_JSON_NAME
    draft_md_path = project_dir / BLUEPRINT_DRAFT_MD_NAME
    revision_json_path = project_dir / BLUEPRINT_REVISION_JSON_NAME
    revision_md_path = project_dir / BLUEPRINT_REVISION_MD_NAME
    view_model_path = project_dir / BLUEPRINT_VIEW_MODEL_JSON_NAME
    return {
        "json_abs": str(json_path),
        "markdown_abs": str(md_path),
        "draft_json_abs": str(draft_json_path),
        "draft_markdown_abs": str(draft_md_path),
        "revision_json_abs": str(revision_json_path),
        "revision_markdown_abs": str(revision_md_path),
        "view_model_abs": str(view_model_path),
        "json": str(json_path.relative_to(base)),
        "markdown": str(md_path.relative_to(base)),
        "draft_json": str(draft_json_path.relative_to(base)),
        "draft_markdown": str(draft_md_path.relative_to(base)),
        "revision_json": str(revision_json_path.relative_to(base)),
        "revision_markdown": str(revision_md_path.relative_to(base)),
        "view_model": str(view_model_path.relative_to(base)),
    }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _safe_text(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _numeric_candidates(value: Any) -> list[float]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, bool):
        return [1.0 if value else 0.0]
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return [float(text)]
        except ValueError:
            match = re.search(r"\d+(?:\.\d+)?", text)
            return [float(match.group(0))] if match else []
    if isinstance(value, list):
        candidates: list[float] = []
        for item in value:
            candidates.extend(_numeric_candidates(item))
        return candidates
    if isinstance(value, dict):
        candidates: list[float] = []
        for key in ("value", "seconds", "duration_seconds", "count", "number", "total"):
            if key in value:
                candidates.extend(_numeric_candidates(value.get(key)))
        if not candidates:
            for item in value.values():
                candidates.extend(_numeric_candidates(item))
        return candidates
    return []


def _safe_positive_int(value: Any, default: int, *, maximum: int | None = None) -> int:
    candidates = [candidate for candidate in _numeric_candidates(value) if candidate > 0]
    parsed = int(math.ceil(max(candidates))) if candidates else int(default)
    parsed = max(1, parsed)
    return min(parsed, maximum) if maximum else parsed


NO_SEGMENT_SPLIT_RE = re.compile(
    r"(不分段|不切段|不要拆|不拆段|单段|一段\s*\d{1,3}\s*秒(?:的)?(?:视频|短片|短剧)?|一段视频|整段|"
    r"single\s+segment|one\s+segment|no\s+split)",
    re.IGNORECASE,
)
EXPLICIT_SEGMENT_SPLIT_RE = re.compile(
    r"(分段|切段|拆段|拆成\s*\d{1,3}\s*段|每段|\d{1,3}\s*秒\s*一段|"
    r"segment|segments|per\s+segment|each\s+segment|split\s+into\s+\d{1,3}\s+(?:segments|parts))",
    re.IGNORECASE,
)
STRONG_SEGMENT_SPLIT_RE = re.compile(
    r"(拆成\s*\d{1,3}\s*段|分成\s*\d{1,3}\s*段|切成\s*\d{1,3}\s*段|每段|\d{1,3}\s*秒\s*一段|"
    r"per\s+segment|each\s+segment|split\s+into\s+\d{1,3}\s+(?:segments|parts))",
    re.IGNORECASE,
)


def _has_explicit_segment_split_request(text: str) -> bool:
    if not text:
        return False
    if STRONG_SEGMENT_SPLIT_RE.search(text):
        return True
    if NO_SEGMENT_SPLIT_RE.search(text):
        return False
    return bool(EXPLICIT_SEGMENT_SPLIT_RE.search(text))


def _unique_text_values(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _safe_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _text_refs(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, "", {}, []):
        return []
    return [value]


def _reference_images_from_sources(*sources: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    image_index = 0
    for source in sources:
        if source in (None, "", {}, []):
            continue
        items = source if isinstance(source, list) else [source]
        for item in items:
            if isinstance(item, str):
                rel_path = _safe_text(item)
                record: dict[str, Any] = {"source": "upload", "usage": "visual_reference", "rel_path": rel_path}
            elif isinstance(item, dict):
                record = dict(item)
                rel_path = _safe_text(
                    record.get("reference_input")
                    or record.get("rel_path")
                    or record.get("path")
                    or record.get("url")
                    or record.get("source_path")
                    or (f"asset:{record.get('asset_id')}" if record.get("asset_id") else "")
                )
                if rel_path:
                    record["rel_path"] = rel_path
                    record.setdefault("reference_input", rel_path)
            else:
                continue
            rel_path = _safe_text(record.get("rel_path"))
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)
            image_index += 1
            mention = _safe_text(record.get("mention") or record.get("ref_label") or record.get("label"))
            if not mention:
                mention = f"@图{image_index}"
            elif not mention.startswith("@"):
                mention = f"@{mention}"
            record["mention"] = mention
            record["label"] = _safe_text(record.get("label") or mention.lstrip("@"))
            record.setdefault("source", "upload")
            record.setdefault("usage", "visual_reference")
            refs.append(record)
    return refs


def _reference_images_from_state_assets(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    store = state.get("reference_assets") if isinstance(state.get("reference_assets"), dict) else {}
    assets = store.get("assets") if isinstance(store.get("assets"), list) else []
    bindings = store.get("bindings") if isinstance(store.get("bindings"), list) else []
    asset_by_id = {
        str(asset.get("ref_id")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("ref_id")
    }
    selected_ids = {
        str(binding.get("ref_id"))
        for binding in bindings
        if isinstance(binding, dict) and binding.get("ref_id")
    }
    refs: list[dict[str, Any]] = []
    for ref_id in selected_ids:
        asset = asset_by_id.get(ref_id)
        if not asset:
            continue
        rel_path = _safe_text(
            asset.get("rel_path")
            or asset.get("source_path")
            or (f"asset:{asset.get('asset_id')}" if asset.get("asset_id") else "")
        )
        if not rel_path:
            continue
        analysis = asset.get("analysis") if isinstance(asset.get("analysis"), dict) else {}
        refs.append({
            "ref_id": ref_id,
            "mention": asset.get("mention"),
            "label": asset.get("label"),
            "rel_path": rel_path,
            "reference_input": rel_path,
            "source_path": asset.get("source_path"),
            "asset_id": asset.get("asset_id"),
            "node_id": asset.get("node_id"),
            "filename": asset.get("filename"),
            "analysis_summary": analysis.get("summary"),
            "style_name": analysis.get("style_name"),
            "style_tags": analysis.get("style_tags") if isinstance(analysis.get("style_tags"), list) else [],
            "prompt_fragment": analysis.get("prompt_fragment"),
            "negative_constraints": analysis.get("negative_constraints") if isinstance(analysis.get("negative_constraints"), list) else [],
            "usage": ",".join(sorted({
                str(binding.get("role") or "visual_reference")
                for binding in bindings
                if isinstance(binding, dict) and str(binding.get("ref_id")) == ref_id
            })),
        })
    return refs, [dict(binding) for binding in bindings if isinstance(binding, dict)]


def _merge_short_video_segments(
    episodes: list[dict[str, Any]],
    *,
    episode_duration: int,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for fallback_ep_no, episode in enumerate(episodes, start=1):
        ep = dict(episode)
        segments = [dict(seg) for seg in (ep.get("segments") or []) if isinstance(seg, dict)]
        if len(segments) <= 1:
            if segments:
                segments[0]["segment_index"] = 1
                segments[0]["duration_seconds"] = episode_duration
                ep["segments"] = segments
            normalized.append(ep)
            continue

        plots = _unique_text_values([
            seg.get("plot") or seg.get("description") or seg.get("summary")
            for seg in segments
        ])
        scenes = _unique_text_values([
            seg.get("scene_design") or seg.get("scene")
            for seg in segments
        ])
        cast_refs = _unique_text_values([
            ref
            for seg in segments
            for ref in _text_refs(seg.get("cast_refs") or seg.get("characters"))
        ])
        scene_refs = _unique_text_values([
            ref
            for seg in segments
            for ref in _text_refs(seg.get("scene_refs") or seg.get("scene_ids"))
        ])
        episode_number = ep.get("episode_number") or fallback_ep_no
        merged = {
            "segment_index": 1,
            "duration_seconds": episode_duration,
            "plot": " ".join(plots)[:1600] or _safe_text(ep.get("summary") or "连续剧情段落。"),
            "scene_design": "；".join(scenes)[:1000],
            "cast_refs": cast_refs,
            "scene_refs": scene_refs,
            "workflow_mode": segments[0].get("workflow_mode") or segments[0].get("visual_mode"),
            "merge_note": "15秒以内且用户未明确要求拆段，已将模型误拆的节拍合并为一个连续视频片段。",
        }
        if any(seg.get("segment_id") for seg in segments):
            merged["segment_id"] = f"seg-{episode_number}-1"
        ep["segments"] = [merged]
        normalized.append(ep)
    return normalized


def _json_checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _section_items(plan: dict[str, Any], section_type: str, key: str = "items") -> list[Any]:
    for section in plan.get("sections") or []:
        if isinstance(section, dict) and section.get("type") == section_type:
            items = section.get(key)
            return items if isinstance(items, list) else []
    return []


def _section_markdown(plan: dict[str, Any]) -> str:
    chunks: list[str] = []
    for section in plan.get("sections") or []:
        if isinstance(section, dict) and section.get("type") == "markdown" and section.get("content"):
            chunks.append(str(section.get("content")))
    return "\n\n".join(chunks)


def _extract_aspect_ratio(text: str) -> str:
    match = re.search(r"\b(\d{1,2}\s*[:：]\s*\d{1,2})\b", text or "")
    if not match:
        return ""
    return match.group(1).replace("：", ":").replace(" ", "")


def _clean_title_candidate(text: str) -> str:
    candidate = re.sub(r"\b\d{1,3}\s*秒\b", "", text or "")
    candidate = re.sub(r"\b\d{1,2}\s*[:：]\s*\d{1,2}\b", "", candidate)
    candidate = re.sub(r"(创意大纲|详细大纲|制作方案|视频|短片|短剧|请|帮我|制作|做一段|做一个|生成)", "", candidate)
    candidate = candidate.strip(" ：:，,。；;、-_/")
    return candidate[:32]


def infer_blueprint_title(plan: dict[str, Any], blueprint: dict[str, Any]) -> str:
    theme = blueprint.get("theme") if isinstance(blueprint.get("theme"), dict) else {}
    for value in (
        theme.get("title") if isinstance(theme, dict) else None,
        blueprint.get("theme_title"),
        blueprint.get("title"),
    ):
        candidate = _clean_title_candidate(str(value or ""))
        if candidate:
            return candidate

    basic = str(blueprint.get("basic_answer") or "")
    for part in re.split(r"[\n，,。；;]", basic):
        candidate = _clean_title_candidate(part)
        if len(candidate) >= 2:
            return candidate

    source = str(plan.get("source_request") or "")
    for part in re.split(r"[\n，,。；;]", source):
        candidate = _clean_title_candidate(part)
        if len(candidate) >= 2:
            return candidate

    candidate = _clean_title_candidate(str(plan.get("title") or ""))
    return candidate or "视频蓝图"


def _episodes_from_plan(plan: dict[str, Any], blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    episodes = blueprint.get("episodes")
    if isinstance(episodes, list) and episodes:
        return [dict(ep) for ep in episodes if isinstance(ep, dict)]
    section_episodes = _section_items(plan, "outline_preview", key="episodes")
    if section_episodes:
        return [dict(ep) for ep in section_episodes if isinstance(ep, dict)]
    return []


def _characters_from_plan(plan: dict[str, Any], blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    characters = blueprint.get("characters")
    if isinstance(characters, list) and characters:
        return [dict(item) for item in characters if isinstance(item, dict)]
    section_items = _section_items(plan, "characters_preview")
    return [dict(item) for item in section_items if isinstance(item, dict)]


def _shots_from_plan(plan: dict[str, Any], blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    shots = blueprint.get("shots")
    if isinstance(shots, list) and shots:
        return [dict(item) for item in shots if isinstance(item, dict)]
    section_items = _section_items(plan, "shots_preview")
    return [dict(item) for item in section_items if isinstance(item, dict)]


def _storyboard_layout_lookup(shots: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    lookup: dict[tuple[int, int], dict[str, Any]] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        try:
            ep_no = int(shot.get("episode_number") or 1)
            seg_no = int(shot.get("segment_index") or shot.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if seg_no <= 0:
            continue
        layout_source = shot.get("storyboard_layout") or shot.get("storyboard_grid") or shot.get("layout")
        layout = normalize_storyboard_layout(layout_source)
        lookup[(ep_no, seg_no)] = {
            "storyboard_layout": layout,
            "storyboard_grid": storyboard_grid_label(layout),
            "storyboard_layout_reason": _safe_text(
                shot.get("layout_reason") or shot.get("storyboard_layout_reason") or shot.get("reason") or "",
                limit=300,
            ),
        }
    return lookup


def _apply_storyboard_layouts_to_segments(episodes: list[dict[str, Any]], shots: list[dict[str, Any]]) -> None:
    layout_by_segment = _storyboard_layout_lookup(shots)
    if not layout_by_segment:
        return
    for ep_idx, episode in enumerate(episodes, 1):
        if not isinstance(episode, dict):
            continue
        try:
            ep_no = int(episode.get("episode_number") or ep_idx)
        except (TypeError, ValueError):
            ep_no = ep_idx
        for seg_idx, segment in enumerate(episode.get("segments") or [], 1):
            if not isinstance(segment, dict):
                continue
            try:
                seg_no = int(segment.get("segment_index") or segment.get("index") or seg_idx)
            except (TypeError, ValueError):
                seg_no = seg_idx
            layout = layout_by_segment.get((ep_no, seg_no))
            if layout:
                segment.update({key: value for key, value in layout.items() if value not in (None, "")})


def _scenes_from_plan(plan: dict[str, Any], blueprint: dict[str, Any], episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scenes = blueprint.get("scenes")
    if isinstance(scenes, list) and scenes:
        return [dict(item) for item in scenes if isinstance(item, dict)]
    section_items = _section_items(plan, "scenes_preview")
    if section_items:
        return [dict(item) for item in section_items if isinstance(item, dict)]
    return _scenes_from_episodes(episodes)


def _scenes_from_episodes(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    scenes: list[dict[str, Any]] = []
    for ep in episodes:
        for seg in ep.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            scene_text = _safe_text(seg.get("scene_design") or seg.get("scene") or "", limit=600)
            if not scene_text or scene_text in seen:
                continue
            seen.add(scene_text)
            scenes.append({
                "name": f"场景{len(scenes) + 1}",
                "description": scene_text,
            })
    return scenes


def _mode_strategy(mode: str, shots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if mode == "frames":
        return {"frames": {"policy": "每个段落生成首帧和尾帧，再生成段落视频提示词和视频片段。"}}
    if mode == "story_template":
        return {
            "story_template": {
	                "policy": (
	                    "每个段落生成一张故事模板图，fields.resolution 推荐 3840x2160，最低 2560x1440；故事分镜/动作流程区占最大面积，"
	                    "再按模板图复述并锁定视频提示词和视频片段。"
	                )
            }
        }
    if mode == "text_to_video":
        return {"text_to_video": {"policy": "直接文生视频，不走多阶段视觉预制作。"}}
    return {
        "grid": {
            "layouts": [4, 6, 9],
            "policy": (
                "每个段落生成一张多宫格分镜图，再生成段落视频提示词和视频片段。"
                f"{STORYBOARD_DENSITY_RULE}"
            ),
            "shots": shots or [],
        }
    }


def _node_projection() -> dict[str, Any]:
    return {
        "script_collection": {
            "mode": "copy_from_blueprint",
            "source_paths": ["story.global_outline", "story.episodes"],
        },
        "episode_script": {
            "mode": "copy_from_blueprint",
            "source_paths": ["story.episodes[].summary", "story.episodes[].script"],
        },
        "episode_segment_plan": {
            "mode": "copy_from_blueprint",
            "source_paths": ["story.episodes[].segments"],
        },
        "episode_cast_scene_plan": {
            "mode": "copy_from_blueprint",
            "source_paths": ["story.episodes[].segments[].cast_refs", "story.episodes[].segments[].scene_refs"],
        },
        "character": {
            "mode": "template_from_blueprint",
            "source_paths": ["characters[]"],
            "template_category": "character_image",
        },
        "scene": {
            "mode": "template_from_blueprint",
            "source_paths": ["scenes[]"],
            "template_category": "scene_image",
        },
        "segment_storyboard": {
            "mode": "template_from_blueprint",
            "source_paths": ["story.episodes[].segments[]", "characters[]", "scenes[]"],
            "template_category": "storyboard_image",
        },
        "shot_first_frame": {
            "mode": "template_from_blueprint",
            "source_paths": ["story.episodes[].segments[]", "characters[]", "scenes[]"],
            "template_category": "first_frame_image",
        },
        "shot_last_frame": {
            "mode": "template_from_blueprint",
            "source_paths": ["story.episodes[].segments[]", "characters[]", "scenes[]"],
            "template_category": "last_frame_image",
        },
        "segment_story_template": {
            "mode": "template_from_blueprint",
            "source_paths": ["story.episodes[].segments[]", "characters[]", "scenes[]", "visual_strategy"],
            "template_category": "story_template",
        },
        "segment_video_prompt": {
            "mode": "template_from_blueprint",
            "source_paths": ["story.episodes[].segments[]", "visual_strategy"],
            "template_category": "video_prompt",
        },
    }


def _completed_generation_sections() -> list[dict[str, Any]]:
    return [{"section_id": section_id, "status": "completed"} for section_id in BLUEPRINT_SECTION_ORDER]


def build_blueprint_document_from_plan(
    plan: dict[str, Any],
    state: dict[str, Any] | None = None,
    *,
    blueprint_id: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    state = state or {}
    raw_blueprint = plan.get("blueprint") if isinstance(plan.get("blueprint"), dict) else {}
    blueprint = dict(raw_blueprint)
    selected_mode = (
        plan.get("selected_video_mode")
        or blueprint.get("mode")
        or state.get("selected_video_mode")
        or state.get("project_sub_mode")
        or "grid"
    )
    title = infer_blueprint_title(plan, blueprint)
    duration = _safe_positive_int(blueprint.get("duration_seconds"), 15)
    episode_count = _safe_positive_int(blueprint.get("episode_count"), 1, maximum=60)
    segment_seconds = _safe_positive_int(blueprint.get("segment_seconds"), min(15, duration), maximum=15)
    source_request = _safe_text(plan.get("source_request") or "")
    basic_answer = _safe_text(blueprint.get("basic_answer") or "")
    structure_answer = _safe_text(blueprint.get("structure_answer") or "")
    pending_request = state.get("pending_video_blueprint_request") if isinstance(state.get("pending_video_blueprint_request"), dict) else {}
    state_reference_images, state_reference_bindings = _reference_images_from_state_assets(state)
    reference_images = _reference_images_from_sources(
        blueprint.get("reference_images"),
        plan.get("reference_images"),
        pending_request.get("reference_images"),
        state_reference_images,
    )
    markdown = _section_markdown(plan)
    episodes = _episodes_from_plan(plan, blueprint)
    episode_duration = max(1, duration if episode_count <= 1 else math.ceil(duration / episode_count))
    request_context = "\n".join(item for item in (source_request, basic_answer, structure_answer) if item)
    forced_single_segment = episode_duration <= 15 and not _has_explicit_segment_split_request(request_context)
    if forced_single_segment:
        segment_seconds = episode_duration
        episodes = _merge_short_video_segments(episodes, episode_duration=episode_duration)
    characters = _characters_from_plan(plan, blueprint)
    shots = _shots_from_plan(plan, blueprint)
    _apply_storyboard_layouts_to_segments(episodes, shots)
    aspect_ratio = _safe_text(blueprint.get("aspect_ratio") or "") or _extract_aspect_ratio("\n".join([source_request, basic_answer, structure_answer]))
    global_outline = markdown or _safe_text(blueprint.get("global_outline") or plan.get("summary") or source_request, limit=4000)
    now = _now_iso()
    model_assumptions = [
        "用户未明确提供的蓝图字段由模型补全；修改必须通过对话生成新版本。"
    ]
    if any(keyword in structure_answer for keyword in ("你来发挥", "模型发挥", "模型决定", "AI决定", "没有剧情", "无大纲")):
        model_assumptions.append("用户授权模型补全剧情大纲、分集剧情、分段剧情和视觉策略。")
    if not basic_answer:
        model_assumptions.append("用户未提供完整风格/类型描述，蓝图生成时由模型按视频需求补全。")
    if not blueprint.get("episode_count"):
        model_assumptions.append("用户未明确集数时默认按 1 集规划。")
    if not blueprint.get("segment_seconds"):
        model_assumptions.append("用户未明确分段方式时，15秒以内默认单段连续视频，超过单段上限再拆段。")
    if forced_single_segment:
        model_assumptions.append("单集时长不超过15秒且用户未明确要求拆段，蓝图按一个连续视频片段规划；分镜只作为段内镜头设计。")
    if reference_images:
        model_assumptions.append("用户上传的参考图已进入蓝图，视觉节点必须通过 reference_images 继承，不要凭空改写引用路径。")

    doc = {
        "schema_version": 1,
        "id": blueprint_id or f"bp_{int(time.time() * 1000)}",
        "version": int(version or 1),
        "created_at": now,
        "updated_at": now,
        "source_plan_id": plan.get("id"),
        "source_request": source_request,
        "theme": {
            "title": title,
            "logline": _safe_text(plan.get("summary") or global_outline, limit=1000),
            "genre": _safe_text(blueprint.get("video_type") or ""),
            "style": basic_answer,
            "aspect_ratio": aspect_ratio,
            "duration_seconds": duration,
        },
        "production": {
            "video_mode": selected_mode,
            "episode_count": episode_count,
            "segment_seconds": segment_seconds,
            "blueprint_generation_strategy": "sectioned",
            "blueprint_review_mode": "continuous_final_review",
        },
        "generation_progress": {
            "strategy": "sectioned",
            "current_section": None,
            "sections": _completed_generation_sections(),
        },
        "story": {
            "global_outline": global_outline,
            "episodes": episodes,
        },
        "characters": characters,
        "scenes": _scenes_from_plan(plan, blueprint, episodes),
        "reference_images": reference_images,
        "reference_bindings": state_reference_bindings,
        "shots": shots,
        "visual_strategy": _mode_strategy(str(selected_mode), shots),
        "node_projection": _node_projection(),
        "outline_document": {
            "format": "markdown",
            "generated_from_json": True,
            "content": global_outline,
        },
        "constraints": {
            "user_requirements": [
                item for item in (source_request, basic_answer, structure_answer) if item
            ],
            "reference_images": reference_images,
            "model_assumptions": model_assumptions,
        },
        "legacy_creative_blueprint": blueprint,
        "sections": plan.get("sections") or [],
    }
    sync_blueprint_outline_document(doc)
    doc["validation_report"] = validate_blueprint_document(doc)
    return doc


def render_blueprint_markdown(doc: dict[str, Any], index: dict[str, Any]) -> str:
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    story = doc.get("story") if isinstance(doc.get("story"), dict) else {}
    lines = [
        f"# {theme.get('title') or index.get('theme_title') or '项目蓝图'}",
        "",
        f"> 蓝图 ID: `{doc.get('id')}` | 版本: {doc.get('version')} | 状态: {index.get('status')}",
        "",
        "## 基本信息",
        "",
        f"- 类型: {theme.get('genre') or '未指定'}",
        f"- 风格: {theme.get('style') or '未指定'}",
        f"- 比例: {theme.get('aspect_ratio') or '未指定'}",
        f"- 总时长: {theme.get('duration_seconds') or production.get('duration_seconds') or '未指定'} 秒",
        f"- 制作模式: {production.get('video_mode') or '未指定'}",
        f"- 集数: {production.get('episode_count') or '未指定'}",
        f"- 分段: {production.get('segment_seconds') or '未指定'} 秒/段",
        "",
        "## 故事总纲",
        "",
        str(story.get("global_outline") or "").strip() or "未填写。",
        "",
        "## 分集与分段",
        "",
    ]
    episodes = story.get("episodes") if isinstance(story.get("episodes"), list) else []
    if not episodes:
        lines.append("未填写。")
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        lines.extend([
            f"### 第 {ep.get('episode_number') or '?'} 集 {ep.get('title') or ''}".rstrip(),
            "",
            str(ep.get("summary") or "").strip() or "未填写。",
            "",
        ])
        for seg in ep.get("segments") or []:
            if isinstance(seg, dict):
                lines.append(
                    f"- 第 {seg.get('segment_index') or '?'} 段"
                    f"（{seg.get('duration_seconds') or production.get('segment_seconds') or '?'} 秒）:"
                    f" {seg.get('plot') or seg.get('description') or ''}"
                )
                if seg.get("scene_design"):
                    lines.append(f"  - 场景: {seg.get('scene_design')}")
                if seg.get("storyboard_layout") or seg.get("storyboard_grid"):
                    layout = normalize_storyboard_layout(seg.get("storyboard_layout") or seg.get("storyboard_grid"))
                    reason = str(seg.get("storyboard_layout_reason") or "").strip()
                    reason_text = f"，原因：{reason}" if reason else ""
                    lines.append(f"  - 分镜密度: {layout}宫格（{storyboard_grid_label(layout)}）{reason_text}")
        lines.append("")

    lines.extend(["## 人物", ""])
    characters = doc.get("characters") if isinstance(doc.get("characters"), list) else []
    if not characters:
        lines.append("未填写。")
    for char in characters:
        if isinstance(char, dict):
            lines.append(f"- **{char.get('name') or '?'}**: {char.get('description') or char.get('identity') or char.get('role') or ''}")

    lines.extend(["", "## 视觉策略", ""])
    visual_strategy = doc.get("visual_strategy") if isinstance(doc.get("visual_strategy"), dict) else {}
    if not visual_strategy:
        lines.append("未填写。")
    for strategy_name, strategy in visual_strategy.items():
        if isinstance(strategy, dict):
            label = {
                "grid": "宫格分镜",
                "frames": "首尾帧",
                "story_template": "故事模板",
                "text_to_video": "文生视频",
            }.get(str(strategy_name), str(strategy_name))
            policy = strategy.get("policy") or strategy.get("storyboard_policy") or ""
            layout = strategy.get("layout")
            suffix = f"（{layout}格）" if layout else ""
            lines.append(f"- **{label}{suffix}**: {policy or '按该模式制作。'}")
    validation = doc.get("validation_report") if isinstance(doc.get("validation_report"), dict) else {}
    issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
    lines.extend(["", "## 校验结果", ""])
    if not issues:
        lines.append("- 未发现需要修订的问题。")
    else:
        for issue in issues:
            if isinstance(issue, dict):
                severity = "错误" if issue.get("severity") == "error" else "提醒"
                lines.append(f"- **{severity}**: {issue.get('message') or '蓝图需要检查。'}")
    lines.append("> 此文件由系统自动生成。用户不能直接编辑蓝图；修改请通过聊天告诉模型。")
    return "\n".join(lines).rstrip() + "\n"


def _outline_source_checksum(doc: dict[str, Any]) -> str:
    source = {
        "theme": doc.get("theme") if isinstance(doc.get("theme"), dict) else {},
        "production": doc.get("production") if isinstance(doc.get("production"), dict) else {},
        "story": doc.get("story") if isinstance(doc.get("story"), dict) else {},
        "characters": doc.get("characters") if isinstance(doc.get("characters"), list) else [],
        "scenes": doc.get("scenes") if isinstance(doc.get("scenes"), list) else [],
        "visual_strategy": doc.get("visual_strategy") if isinstance(doc.get("visual_strategy"), dict) else {},
    }
    return _json_checksum(source)


def render_blueprint_outline_markdown(doc: dict[str, Any]) -> str:
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    story = doc.get("story") if isinstance(doc.get("story"), dict) else {}
    episodes = story.get("episodes") if isinstance(story.get("episodes"), list) else []
    lines = [
        f"### {theme.get('title') or '整体大纲'}",
        "",
        str(story.get("global_outline") or theme.get("logline") or "蓝图大纲已生成。").strip(),
    ]
    for ep in episodes[:12]:
        if not isinstance(ep, dict):
            continue
        lines.extend([
            "",
            f"#### 第 {ep.get('episode_number') or '?'} 集 {ep.get('title') or ''}".rstrip(),
            "",
            str(ep.get("summary") or "").strip() or "未填写。",
        ])
        for seg in (ep.get("segments") or [])[:20]:
            if not isinstance(seg, dict):
                continue
            duration = seg.get("duration_seconds") or production.get("segment_seconds") or "?"
            lines.append(
                f"- 第 {seg.get('segment_index') or '?'} 段（{duration} 秒）: "
                f"{seg.get('plot') or seg.get('description') or '未填写。'}"
            )
    return "\n".join(lines).strip()


def sync_blueprint_outline_document(doc: dict[str, Any]) -> dict[str, Any]:
    outline_doc = doc.get("outline_document") if isinstance(doc.get("outline_document"), dict) else {}
    checksum = _outline_source_checksum(doc)
    if str(outline_doc.get("source_blueprint_checksum") or "") == checksum:
        content = str(outline_doc.get("content") or outline_doc.get("markdown") or "").strip()
    else:
        content = ""
    if not content:
        content = render_blueprint_outline_markdown(doc)
    synced = {
        **outline_doc,
        "format": "markdown",
        "generated_from_json": True,
        "source_blueprint_checksum": checksum,
        "content": content,
    }
    doc["outline_document"] = synced
    return synced


def blueprint_outline_markdown(doc: dict[str, Any]) -> str:
    outline_doc = sync_blueprint_outline_document(doc)
    return str(outline_doc.get("content") or "").strip()


def render_blueprint_view_model(doc: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    """Render a user-facing view model derived from the canonical blueprint JSON.

    This is display cache only. Normal UI should render these blocks instead of
    dumping raw blueprint JSON.
    """
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    story = doc.get("story") if isinstance(doc.get("story"), dict) else {}
    episodes = story.get("episodes") if isinstance(story.get("episodes"), list) else []
    characters = doc.get("characters") if isinstance(doc.get("characters"), list) else []
    scenes = doc.get("scenes") if isinstance(doc.get("scenes"), list) else []
    reference_images = doc.get("reference_images") if isinstance(doc.get("reference_images"), list) else []
    visual_style = doc.get("visual_style") if isinstance(doc.get("visual_style"), dict) else {}
    checksum = index.get("checksum") or _json_checksum(doc)

    def _segment_items() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            for seg in ep.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                rows.append({
                    "title": f"第 {ep.get('episode_number') or '?'} 集第 {seg.get('segment_index') or '?'} 段",
                    "duration": f"{seg.get('duration_seconds') or production.get('segment_seconds') or '?'} 秒",
                    "summary": seg.get("plot") or seg.get("description") or "",
                    "scene": seg.get("scene_design") or seg.get("scene") or "",
                    "storyboard": (
                        f"{normalize_storyboard_layout(seg.get('storyboard_layout') or seg.get('storyboard_grid'))}宫格"
                        if (seg.get("storyboard_layout") or seg.get("storyboard_grid"))
                        else ""
                    ),
                    "storyboard_reason": seg.get("storyboard_layout_reason") or "",
                })
        return rows

    def _script_items() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            segments: list[dict[str, Any]] = []
            for seg in ep.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                dialogue = seg.get("dialogue") or seg.get("dialogues") or seg.get("lines") or ""
                segments.append({
                    "title": f"第 {seg.get('segment_index') or '?'} 段",
                    "duration": f"{seg.get('duration_seconds') or production.get('segment_seconds') or '?'} 秒",
                    "scene": seg.get("scene_design") or seg.get("scene") or "",
                    "action": seg.get("action") or seg.get("plot") or seg.get("description") or "",
                    "dialogue": dialogue if isinstance(dialogue, str) else "",
                    "beat": seg.get("beat") or "",
                })
            rows.append({
                "title": f"第 {ep.get('episode_number') or '?'} 集 {ep.get('title') or ''}".strip(),
                "summary": ep.get("summary") or "",
                "script": ep.get("script") or "",
                "segments": segments,
            })
        return rows

    sections: list[dict[str, Any]] = [
        {
            "section_id": "global_story_outline",
            "title": BLUEPRINT_SECTION_TITLES["global_story_outline"],
            "display_type": "prose",
            "blocks": [{"type": "paragraph", "text": str(story.get("global_outline") or "未填写。")}],
        },
        {
            "section_id": "episode_index",
            "title": BLUEPRINT_SECTION_TITLES["episode_index"],
            "display_type": "timeline",
            "items": [
                {
                    "title": f"第 {ep.get('episode_number') or '?'} 集 {ep.get('title') or ''}".strip(),
                    "summary": ep.get("summary") or "",
                }
                for ep in episodes
                if isinstance(ep, dict)
            ],
        },
        {
            "section_id": "episode_scripts",
            "title": BLUEPRINT_SECTION_TITLES["episode_scripts"],
            "display_type": "script",
            "items": _script_items(),
        },
        {
            "section_id": "segment_breakdown",
            "title": BLUEPRINT_SECTION_TITLES["segment_breakdown"],
            "display_type": "table",
            "items": _segment_items(),
        },
        {
            "section_id": "character_bible",
            "title": BLUEPRINT_SECTION_TITLES["character_bible"],
            "display_type": "cards",
            "items": [
                {
                    "title": char.get("name") or "?",
                    "subtitle": char.get("role") or char.get("role_type") or char.get("identity") or "",
                    "body": char.get("description") or char.get("appearance") or char.get("motivation") or "",
                }
                for char in characters
                if isinstance(char, dict)
            ],
        },
        {
            "section_id": "scene_bible",
            "title": BLUEPRINT_SECTION_TITLES["scene_bible"],
            "display_type": "cards",
            "items": [
                {
                    "title": scene.get("name") or scene.get("location") or "?",
                    "body": scene.get("description") or scene.get("visual_prompt") or "",
                }
                for scene in scenes
                if isinstance(scene, dict)
            ],
        },
        {
            "section_id": "visual_style",
            "title": BLUEPRINT_SECTION_TITLES.get("visual_style", "视觉风格"),
            "display_type": "card",
            "items": [{
                "style_name": visual_style.get("style_name") or "",
                "description": visual_style.get("description") or "",
                "color_palette": visual_style.get("color_palette") or "",
                "lighting": visual_style.get("lighting") or "",
                "camera_style": visual_style.get("camera_style") or "",
            }] if visual_style.get("style_name") else [],
        },
        {
            "section_id": "reference_images",
            "title": "参考图",
            "display_type": "references",
            "items": [
                {
                    "title": ref.get("mention") or ref.get("label") or ref.get("filename") or "参考图",
                    "summary": ref.get("filename") or ref.get("rel_path") or "",
                    "rel_path": ref.get("rel_path") or "",
                    "usage": ref.get("usage") or "visual_reference",
                }
                for ref in reference_images
                if isinstance(ref, dict)
            ],
        },
    ]
    return {
        "kind": "blueprint_view_model",
        "blueprint_id": doc.get("id"),
        "version": doc.get("version"),
        "source_blueprint_checksum": checksum,
        "header": {
            "title": theme.get("title") or index.get("theme_title") or "项目蓝图",
            "status_label": index.get("status") or "unknown",
            "badges": [
                item for item in (
                    f"{theme.get('duration_seconds')}秒" if theme.get("duration_seconds") else "",
                    theme.get("aspect_ratio") or "",
                    theme.get("style") or "",
                    production.get("video_mode") or "",
                ) if item
            ],
        },
        "sections": sections,
        "actions": [
            {"type": "request_revision", "label": "告诉 AI 修改蓝图"},
            {"type": "approve_blueprint", "label": "确认大纲"},
        ],
    }


def _section_payload(doc: dict[str, Any], section_id: str) -> dict[str, Any]:
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    story = doc.get("story") if isinstance(doc.get("story"), dict) else {}
    if section_id == "requirements_digest":
        return {
            "source_request": doc.get("source_request"),
            "constraints": doc.get("constraints"),
        }
    if section_id == "production_spec":
        return {"theme": theme, "production": production}
    if section_id == "global_story_outline":
        return {"global_outline": story.get("global_outline")}
    if section_id == "episode_index":
        return {
            "episodes": [
                {
                    "episode_id": ep.get("episode_id"),
                    "episode_number": ep.get("episode_number"),
                    "title": ep.get("title"),
                    "summary": ep.get("summary"),
                }
                for ep in (story.get("episodes") if isinstance(story.get("episodes"), list) else [])
                if isinstance(ep, dict)
            ]
        }
    if section_id == "character_bible":
        return {"characters": doc.get("characters") if isinstance(doc.get("characters"), list) else []}
    if section_id == "scene_bible":
        return {"scenes": doc.get("scenes") if isinstance(doc.get("scenes"), list) else []}
    if section_id == "episode_scripts":
        return {"episodes": story.get("episodes") if isinstance(story.get("episodes"), list) else []}
    if section_id == "segment_breakdown":
        return {
            "segments": [
                {**seg, "episode_number": ep.get("episode_number")}
                for ep in (story.get("episodes") if isinstance(story.get("episodes"), list) else [])
                if isinstance(ep, dict)
                for seg in (ep.get("segments") or [])
                if isinstance(seg, dict)
            ]
        }
    if section_id == "visual_strategy":
        return {"visual_strategy": doc.get("visual_strategy") if isinstance(doc.get("visual_strategy"), dict) else {}}
    if section_id == "node_projection":
        return {"node_projection": doc.get("node_projection") if isinstance(doc.get("node_projection"), dict) else {}}
    if section_id == "validation_report":
        return {"validation_report": doc.get("validation_report") if isinstance(doc.get("validation_report"), dict) else {}}
    if section_id == "outline_document":
        return {"outline_document": doc.get("outline_document") if isinstance(doc.get("outline_document"), dict) else {}}
    return {}


def _draft_section_summary(doc: dict[str, Any], section_id: str) -> str:
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    story = doc.get("story") if isinstance(doc.get("story"), dict) else {}
    episodes = story.get("episodes") if isinstance(story.get("episodes"), list) else []
    segments = [
        seg
        for ep in episodes
        if isinstance(ep, dict)
        for seg in (ep.get("segments") or [])
        if isinstance(seg, dict)
    ]
    if section_id == "requirements_digest":
        return "已整理用户原始需求、硬约束和模型补全假设。"
    if section_id == "production_spec":
        return (
            f"{theme.get('duration_seconds') or '?'}秒，"
            f"{production.get('episode_count') or '?'}集，"
            f"{production.get('segment_seconds') or '?'}秒/段，"
            f"{production.get('video_mode') or '未指定'}模式。"
        )
    if section_id == "global_story_outline":
        return _safe_text(story.get("global_outline") or "故事总纲已生成。", limit=180)
    if section_id == "episode_index":
        return f"已规划 {len(episodes)} 集剧情。"
    if section_id == "character_bible":
        characters = doc.get("characters") if isinstance(doc.get("characters"), list) else []
        return f"已规划 {len(characters)} 个主要人物。"
    if section_id == "scene_bible":
        scenes = doc.get("scenes") if isinstance(doc.get("scenes"), list) else []
        return f"已规划 {len(scenes)} 个主要场景。"
    if section_id == "episode_scripts":
        return f"已生成 {len(episodes)} 集的剧情内容。"
    if section_id == "segment_breakdown":
        return f"已拆分 {len(segments)} 个剧情段落。"
    if section_id == "visual_strategy":
        return "已根据制作模式规划视觉节点策略。"
    if section_id == "node_projection":
        return "已建立蓝图字段到节点类型的映射。"
    if section_id == "validation_report":
        validation = doc.get("validation_report") if isinstance(doc.get("validation_report"), dict) else {}
        issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
        return "校验通过。" if not issues else f"发现 {len(issues)} 个需要检查的问题。"
    if section_id == "outline_document":
        return "已生成用户可读的大纲文档。"
    return "蓝图章节已生成。"


def _section_display_blocks(
    *,
    doc: dict[str, Any],
    view_model: dict[str, Any],
    section_id: str,
) -> list[dict[str, Any]]:
    for section in view_model.get("sections") or []:
        if isinstance(section, dict) and section.get("section_id") == section_id:
            if section.get("blocks"):
                return section.get("blocks") if isinstance(section.get("blocks"), list) else []
            if section.get("items"):
                return [
                    {
                        "type": str(section.get("display_type") or "list"),
                        "title": section.get("title"),
                        "items": section.get("items"),
                    }
                ]
    payload = _section_payload(doc, section_id)
    if section_id == "production_spec":
        theme = payload.get("theme") if isinstance(payload.get("theme"), dict) else {}
        production = payload.get("production") if isinstance(payload.get("production"), dict) else {}
        return [
            {
                "type": "facts",
                "items": [
                    {"label": "总时长", "value": f"{theme.get('duration_seconds') or '?'}秒"},
                    {"label": "比例", "value": theme.get("aspect_ratio") or "未指定"},
                    {"label": "制作模式", "value": production.get("video_mode") or "未指定"},
                    {"label": "分段", "value": f"{production.get('segment_seconds') or '?'}秒/段"},
                ],
            }
        ]
    return [{"type": "paragraph", "text": _draft_section_summary(doc, section_id)}]


def _validation_issues_for_section(doc: dict[str, Any], section_id: str) -> list[dict[str, Any]]:
    report = doc.get("validation_report") if isinstance(doc.get("validation_report"), dict) else {}
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    section_issues: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("section_id") or "") == section_id:
            section_issues.append(dict(issue))
    return section_issues


def _validation_error_issues_for_section(doc: dict[str, Any], section_id: str) -> list[dict[str, Any]]:
    return [
        issue for issue in _validation_issues_for_section(doc, section_id)
        if str(issue.get("severity") or "").lower() == "error"
    ]


def _section_revision_summary(doc: dict[str, Any], section_id: str) -> str:
    issues = _validation_error_issues_for_section(doc, section_id)
    if not issues:
        return ""
    messages = [
        str(issue.get("message") or "").strip()
        for issue in issues[:3]
        if str(issue.get("message") or "").strip()
    ]
    if not messages:
        return "该章节未通过蓝图校验，需要修订。"
    suffix = " 等" if len(issues) > len(messages) else ""
    return "；".join(messages) + suffix


def _has_needs_revision_sections(sections: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(section, dict) and section.get("status") == "needs_revision"
        for section in sections
    )


def _completed_section_count(sections: list[dict[str, Any]]) -> int:
    return sum(
        1
        for section in sections
        if isinstance(section, dict) and section.get("status") == "completed"
    )


def _first_needs_revision_section(sections: list[dict[str, Any]]) -> dict[str, Any] | None:
    for section in sections:
        if isinstance(section, dict) and section.get("status") == "needs_revision":
            return section
    return None


def _needs_revision_section_events(
    *,
    project_id: str,
    sections: list[dict[str, Any]],
    blueprint_ref: dict[str, Any],
    debug_json_path: str | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict) or section.get("status") != "needs_revision":
            continue
        section_id = str(section.get("section_id") or "")
        title = str(section.get("title") or BLUEPRINT_SECTION_TITLES.get(section_id, section_id))
        validator = section.get("validator_result") if isinstance(section.get("validator_result"), dict) else {}
        issues = validator.get("issues") if isinstance(validator.get("issues"), list) else []
        error_messages = [
            str(issue.get("message") or "").strip()
            for issue in issues
            if isinstance(issue, dict)
            and str(issue.get("severity") or "").lower() == "error"
            and str(issue.get("message") or "").strip()
        ]
        reason = "；".join(error_messages[:3]) or "该章节未通过蓝图校验"
        if len(error_messages) > 3:
            reason += " 等"
        summary = f"「{title}」需要修订：{reason}。回复“继续”会重试这一节，也可以直接说修改意见。"
        try:
            section_index = BLUEPRINT_SECTION_ORDER.index(section_id)
        except ValueError:
            section_index = 0
        events.append({
            "type": "blueprint_section_needs_revision",
            "project_id": project_id,
            "section_id": section_id,
            "title": title,
            "section_index": section_index,
            "status": "failed",
            "summary_text": summary,
            "display_blocks": [{"type": "paragraph", "text": summary}],
            "failure_reason": reason,
            "blueprint_ref": {**blueprint_ref, "status": "needs_revision"},
            "debug_json_path": debug_json_path,
        })
    return events


def _section_progress_record(
    doc: dict[str, Any],
    section_id: str,
    *,
    status: str,
    summary_text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload if payload is not None else (_section_payload(doc, section_id) if status == "completed" else {})
    issues = _validation_issues_for_section(doc, section_id)
    error_issues = _validation_error_issues_for_section(doc, section_id)
    if status == "completed" and error_issues:
        status = "needs_revision"
    validator_status = "failed" if error_issues else ("needs_attention" if issues else "passed")
    if status in {"failed", "needs_revision"}:
        validator_status = "failed"
    if summary_text is None:
        summary_text = (
            _section_revision_summary(doc, section_id)
            if status == "needs_revision"
            else (_draft_section_summary(doc, section_id) if status == "completed" else "")
        )
    return {
        "section_id": section_id,
        "title": BLUEPRINT_SECTION_TITLES.get(section_id, section_id),
        "status": status,
        "checksum": _json_checksum(payload) if payload else "",
        "summary_text": summary_text,
        "validator_result": {
            "status": validator_status,
            "issues": issues,
        },
    }


def _draft_section_progress(doc: dict[str, Any]) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    for section_id in BLUEPRINT_SECTION_ORDER:
        payload = _section_payload(doc, section_id)
        progress.append(_section_progress_record(doc, section_id, status="completed", payload=payload))
    return progress


def write_blueprint_draft_files(project_id: str, doc: dict[str, Any], index: dict[str, Any]) -> dict[str, str]:
    sync_blueprint_outline_document(doc)
    last_error: OSError | None = None
    for root in ("data", "storage"):
        try:
            project_data_dir(project_id, create=True, root=root)
            paths = blueprint_paths(project_id, root=root)
            file_index = {
                **index,
                "file_json": paths["draft_json"],
                "file_markdown": paths["draft_markdown"],
            }
            draft_json_path = Path(paths["draft_json_abs"])
            draft_md_path = Path(paths["draft_markdown_abs"])
            draft_json_path.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            draft_md_path.write_text(render_blueprint_markdown(doc, file_index), encoding="utf-8")
            return paths
        except OSError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise OSError("Unable to write blueprint draft files")


def prepare_blueprint_draft_from_plan(
    *,
    project_id: str,
    state: dict[str, Any],
    plan: dict[str, Any],
    review_mode: str = "continuous_final_review",
    persist_files: bool = True,
    emit_section_events: bool = True,
) -> dict[str, Any]:
    """Build a sectioned pending blueprint draft from a creative blueprint plan.

    The current transition flow still stores the user-facing approval artifact
    as `kind=creative_blueprint`, but this helper makes the canonical blueprint
    draft section-based and file-backed before approval.
    """
    if review_mode not in {"continuous_final_review", "section_review"}:
        review_mode = "continuous_final_review"
    previous = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
    version = int(previous.get("version") or 0) + 1 if previous else 1
    blueprint_id = f"bp_{int(time.time() * 1000)}" if not previous else str(previous.get("id") or f"bp_{int(time.time() * 1000)}")
    doc = build_blueprint_document_from_plan(plan, state, blueprint_id=blueprint_id, version=version)
    doc["status"] = "pending_review"
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    production["blueprint_generation_strategy"] = "sectioned"
    production["blueprint_review_mode"] = review_mode
    doc["production"] = production
    sync_blueprint_outline_document(doc)
    sections = _draft_section_progress(doc)
    draft_status = "needs_revision" if _has_needs_revision_sections(sections) else "pending_review"
    progress_status = "needs_revision" if draft_status == "needs_revision" else "completed"
    doc["status"] = draft_status
    doc["generation_progress"] = {
        "strategy": "sectioned",
        "review_mode": review_mode,
        "status": progress_status,
        "current_section": None,
        "sections": sections,
    }
    sync_blueprint_outline_document(doc)
    checksum = _json_checksum(doc)
    paths = blueprint_paths(project_id)
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    index = {
        "id": doc.get("id"),
        "version": doc.get("version"),
        "status": draft_status,
        "theme_title": theme.get("title") or "项目蓝图",
        "short_summary": _safe_text(theme.get("logline") or doc.get("source_request") or "", limit=1000),
        "file_json": paths["draft_json"],
        "file_markdown": paths["draft_markdown"],
        "checksum": checksum,
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "selected_video_mode": production.get("video_mode"),
        "duration_seconds": theme.get("duration_seconds"),
        "episode_count": production.get("episode_count"),
        "segment_seconds": production.get("segment_seconds"),
        "review_mode": review_mode,
        "generation_strategy": "sectioned",
        "source_plan_id": plan.get("id"),
    }
    if persist_files:
        try:
            paths = write_blueprint_draft_files(project_id, doc, index)
            index["file_json"] = paths["draft_json"]
            index["file_markdown"] = paths["draft_markdown"]
        except OSError as exc:
            index["file_error"] = str(exc)

    view_model = render_blueprint_view_model(doc, index)
    draft_ref = {
        "id": index.get("id"),
        "version": index.get("version"),
        "status": index.get("status"),
        "checksum": index.get("checksum"),
        "file_json": index.get("file_json"),
        "file_markdown": index.get("file_markdown"),
        "review_mode": review_mode,
        "generation_strategy": "sectioned",
    }
    plan["draft_blueprint_ref"] = draft_ref
    state["pending_blueprint_draft"] = index
    state["pending_blueprint_review"] = draft_ref
    state["blueprint_generation_progress"] = doc["generation_progress"]
    state["blueprint_section_results"] = sections
    state["blueprint_progress"] = {
        "status": draft_status,
        "blueprint_id": index.get("id"),
        "blueprint_version": index.get("version"),
        "current_section": None,
        "completed_sections": _completed_section_count(sections),
        "total_sections": len(sections),
        "review_mode": review_mode,
    }

    blueprint_ref = {
        "id": index.get("id"),
        "version": index.get("version"),
        "status": index.get("status"),
        "checksum": index.get("checksum"),
        "file_json": index.get("file_json"),
        "file_markdown": index.get("file_markdown"),
    }
    revision_events = _needs_revision_section_events(
        project_id=project_id,
        sections=sections,
        blueprint_ref=blueprint_ref,
        debug_json_path=index.get("file_json"),
    )
    events: list[dict[str, Any]] = []
    if emit_section_events:
        events.append(
            {
                "type": "blueprint_draft_started",
                "project_id": project_id,
                "status": "drafting",
                "summary_text": "开始逐段生成项目蓝图。",
                "blueprint_ref": blueprint_ref,
                "debug_json_path": index.get("file_json"),
            }
        )
        for section in sections:
            section_id = str(section.get("section_id") or "")
            section_title = str(section.get("title") or BLUEPRINT_SECTION_TITLES.get(section_id, section_id))
            events.append({
                "type": "blueprint_section_started",
                "project_id": project_id,
                "section_id": section_id,
                "title": section_title,
                "status": "running",
                "summary_text": f"正在生成：{section_title}",
                "blueprint_ref": blueprint_ref,
                "debug_json_path": index.get("file_json"),
            })
            if section.get("status") == "needs_revision":
                matching = [
                    event for event in revision_events
                    if event.get("section_id") == section_id
                ]
                events.extend(matching)
            else:
                events.append({
                    "type": "blueprint_section_completed",
                    "project_id": project_id,
                    "section_id": section_id,
                    "title": section_title,
                    "status": "completed",
                    "summary_text": str(section.get("summary_text") or ""),
                    "display_blocks": _section_display_blocks(doc=doc, view_model=view_model, section_id=section_id),
                    "blueprint_ref": blueprint_ref,
                    "debug_json_path": index.get("file_json"),
                })
    elif revision_events:
        events.extend(revision_events)
    events.extend([
        {
            "type": "blueprint_validation_completed",
            "project_id": project_id,
            "section_id": "validation_report",
            "title": BLUEPRINT_SECTION_TITLES.get("validation_report", "校验报告"),
            "status": "needs_revision" if draft_status == "needs_revision" else "completed",
            "summary_text": _draft_section_summary(doc, "validation_report"),
            "validation": doc.get("validation_report") if isinstance(doc.get("validation_report"), dict) else {},
            "blueprint_ref": blueprint_ref,
            "debug_json_path": index.get("file_json"),
        },
        {
            "type": "blueprint_draft_saved",
            "project_id": project_id,
            "status": draft_status,
            "summary_text": (
                "蓝图草稿已保存，但部分章节需要修订。"
                if draft_status == "needs_revision"
                else "蓝图草稿已保存，等待整体大纲确认。"
            ),
            "view_model_patch": view_model,
            "blueprint_ref": blueprint_ref,
            "debug_json_path": index.get("file_json"),
        },
    ])
    if draft_status != "needs_revision":
        events.append({
            "type": "blueprint_proposed",
            "project_id": project_id,
            "status": "pending_review",
            "summary_text": "请确认整体大纲；确认后才会创建制作任务。",
            "view_model_patch": view_model,
            "blueprint_ref": blueprint_ref,
            "debug_json_path": index.get("file_json"),
        })
    return {
        "ok": True,
        "status": draft_status,
        "draft": index,
        "document": doc,
        "view_model": view_model,
        "events": events,
        "needs_revision_sections": [
            section for section in sections
            if isinstance(section, dict) and section.get("status") == "needs_revision"
        ],
        "state_patch": {
            "pending_blueprint_draft": index,
            "pending_blueprint_review": draft_ref,
            "blueprint_generation_progress": doc["generation_progress"],
            "blueprint_section_results": sections,
            "blueprint_progress": state["blueprint_progress"],
        },
    }


def prepare_blueprint_draft_checkpoint(
    *,
    project_id: str,
    state: dict[str, Any],
    plan: dict[str, Any],
    review_mode: str,
    next_section_index: int,
    persist_files: bool = True,
) -> dict[str, Any]:
    """Persist an in-progress sectioned blueprint draft checkpoint."""
    if review_mode not in {"continuous_final_review", "section_review"}:
        review_mode = "section_review"
    next_section_index = max(0, min(int(next_section_index or 0), len(BLUEPRINT_SECTION_ORDER)))
    previous = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
    version = int(previous.get("version") or 0) + 1 if previous else 1
    blueprint_id = f"bp_{int(time.time() * 1000)}" if not previous else str(previous.get("id") or f"bp_{int(time.time() * 1000)}")
    existing_draft = state.get("pending_blueprint_draft") if isinstance(state.get("pending_blueprint_draft"), dict) else {}
    if existing_draft.get("id"):
        blueprint_id = str(existing_draft.get("id"))
        version = int(existing_draft.get("version") or version)
    doc = build_blueprint_document_from_plan(plan, state, blueprint_id=blueprint_id, version=version)
    doc["status"] = "drafting" if next_section_index < len(BLUEPRINT_SECTION_ORDER) else "pending_section_confirmation"
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    production["blueprint_generation_strategy"] = "sectioned"
    production["blueprint_review_mode"] = review_mode
    doc["production"] = production
    current_section = (
        BLUEPRINT_SECTION_ORDER[next_section_index]
        if next_section_index < len(BLUEPRINT_SECTION_ORDER)
        else None
    )
    window_progress = (
        state.get("blueprint_window_progress")
        if isinstance(state.get("blueprint_window_progress"), dict)
        else None
    )
    section_failure = (
        plan.get("_blueprint_section_failure")
        if isinstance(plan.get("_blueprint_section_failure"), dict)
        else None
    )

    sync_blueprint_outline_document(doc)
    sections: list[dict[str, Any]] = []
    for idx, section_id in enumerate(BLUEPRINT_SECTION_ORDER):
        if idx < next_section_index:
            status = "completed"
        elif (
            idx == next_section_index
            and isinstance(window_progress, dict)
            and window_progress.get("section_id") == section_id
            and str(window_progress.get("status") or "") == "failed"
        ):
            status = "failed"
        elif (
            idx == next_section_index
            and isinstance(section_failure, dict)
            and section_failure.get("section_id") == section_id
            and str(section_failure.get("status") or "") == "failed"
        ):
            status = "failed"
        elif idx == next_section_index:
            status = "pending"
        else:
            status = "pending"
        payload = _section_payload(doc, section_id) if status == "completed" else {}
        failure_reason = ""
        if status == "failed" and isinstance(window_progress, dict) and window_progress.get("section_id") == section_id:
            failure_reason = str(window_progress.get("failure_reason") or "")
        if status == "failed" and isinstance(section_failure, dict) and section_failure.get("section_id") == section_id:
            failure_reason = str(section_failure.get("failure_reason") or failure_reason)
        sections.append(
            _section_progress_record(
                doc,
                section_id,
                status=status,
                payload=payload,
                summary_text=_draft_section_summary(doc, section_id) if status == "completed" else failure_reason,
            )
        )
    if next_section_index >= len(BLUEPRINT_SECTION_ORDER):
        progress_status = "pending_section_confirmation"
    elif review_mode == "section_review":
        progress_status = "paused_for_section_review"
    else:
        progress_status = "drafting"
    revision_section = _first_needs_revision_section(sections)
    if isinstance(revision_section, dict):
        progress_status = "needs_revision"
        current_section = str(revision_section.get("section_id") or current_section or "")
        try:
            next_section_index = BLUEPRINT_SECTION_ORDER.index(current_section)
        except ValueError:
            pass
    doc["generation_progress"] = {
        "strategy": "sectioned",
        "review_mode": review_mode,
        "status": progress_status,
        "current_section": current_section,
        "next_section_index": next_section_index,
        "sections": sections,
    }
    if isinstance(window_progress, dict) and window_progress.get("section_id") == current_section:
        doc["generation_progress"]["window_progress"] = window_progress
    sync_blueprint_outline_document(doc)
    checksum = _json_checksum(doc)
    paths = blueprint_paths(project_id)
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    index = {
        "id": doc.get("id"),
        "version": doc.get("version"),
        "status": "drafting" if next_section_index < len(BLUEPRINT_SECTION_ORDER) else "pending_section_confirmation",
        "theme_title": theme.get("title") or existing_draft.get("theme_title") or "项目蓝图",
        "short_summary": _safe_text(theme.get("logline") or doc.get("source_request") or "", limit=1000),
        "file_json": paths["draft_json"],
        "file_markdown": paths["draft_markdown"],
        "checksum": checksum,
        "created_at": existing_draft.get("created_at") or doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "selected_video_mode": production.get("video_mode"),
        "duration_seconds": theme.get("duration_seconds"),
        "episode_count": production.get("episode_count"),
        "segment_seconds": production.get("segment_seconds"),
        "review_mode": review_mode,
        "generation_strategy": "sectioned",
        "next_section_index": next_section_index,
        "current_section": current_section,
        "window_progress": window_progress if isinstance(window_progress, dict) else None,
        "source_plan_id": plan.get("id"),
    }
    if isinstance(revision_section, dict):
        index["status"] = "needs_revision"
        index["current_section"] = current_section
        index["next_section_index"] = next_section_index
    if persist_files:
        try:
            paths = write_blueprint_draft_files(project_id, doc, index)
            index["file_json"] = paths["draft_json"]
            index["file_markdown"] = paths["draft_markdown"]
        except OSError as exc:
            index["file_error"] = str(exc)
    draft_ref = {
        "id": index.get("id"),
        "version": index.get("version"),
        "status": index.get("status"),
        "checksum": index.get("checksum"),
        "file_json": index.get("file_json"),
        "file_markdown": index.get("file_markdown"),
        "review_mode": review_mode,
        "generation_strategy": "sectioned",
        "next_section_index": next_section_index,
        "current_section": current_section,
        "window_progress": window_progress if isinstance(window_progress, dict) else None,
    }
    plan["draft_blueprint_ref"] = draft_ref
    state["pending_blueprint_draft"] = index
    state["pending_blueprint_review"] = draft_ref
    state["blueprint_generation_progress"] = doc["generation_progress"]
    state["blueprint_section_results"] = sections
    state["blueprint_progress"] = {
        "status": index["status"],
        "blueprint_id": index.get("id"),
        "blueprint_version": index.get("version"),
        "current_section": current_section,
        "completed_sections": _completed_section_count(sections),
        "total_sections": len(BLUEPRINT_SECTION_ORDER),
        "review_mode": review_mode,
    }
    if isinstance(window_progress, dict):
        state["blueprint_progress"]["window_progress"] = window_progress
    return {
        "ok": True,
        "draft": index,
        "document": doc,
        "state_patch": {
            "pending_blueprint_draft": index,
            "pending_blueprint_review": draft_ref,
            "blueprint_generation_progress": doc["generation_progress"],
            "blueprint_section_results": sections,
            "blueprint_progress": state["blueprint_progress"],
        },
    }


def load_pending_blueprint_draft_document(
    *,
    project_id: str,
    state: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    ref = plan.get("draft_blueprint_ref") if isinstance(plan.get("draft_blueprint_ref"), dict) else None
    if ref is None:
        ref = state.get("pending_blueprint_draft") if isinstance(state.get("pending_blueprint_draft"), dict) else None
    if not isinstance(ref, dict):
        return None
    rel_path = str(ref.get("file_json") or blueprint_paths(project_id)["draft_json"])
    doc = _read_json_file(Path(settings.PROJECT_ROOT) / rel_path)
    if not isinstance(doc, dict):
        return None
    if ref.get("id") and str(ref.get("id")) != str(doc.get("id")):
        return None
    if ref.get("version") and str(ref.get("version")) != str(doc.get("version")):
        return None
    return doc


def creative_plan_from_blueprint_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the creative-plan shape from a canonical blueprint document."""
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    story = doc.get("story") if isinstance(doc.get("story"), dict) else {}
    legacy = doc.get("legacy_creative_blueprint") if isinstance(doc.get("legacy_creative_blueprint"), dict) else {}
    episodes = story.get("episodes") if isinstance(story.get("episodes"), list) else []
    characters = doc.get("characters") if isinstance(doc.get("characters"), list) else []
    scenes = doc.get("scenes") if isinstance(doc.get("scenes"), list) else []
    shots = doc.get("shots") if isinstance(doc.get("shots"), list) else []
    outline_doc = doc.get("outline_document") if isinstance(doc.get("outline_document"), dict) else {}
    markdown = str(outline_doc.get("content") or story.get("global_outline") or theme.get("logline") or "").strip()
    if not markdown:
        markdown = "### 详细剧本大纲\n蓝图草稿已恢复，请继续生成后续章节。"

    blueprint = {
        **legacy,
        "duration_seconds": theme.get("duration_seconds") or production.get("duration_seconds") or legacy.get("duration_seconds") or 15,
        "episode_count": production.get("episode_count") or legacy.get("episode_count") or 1,
        "segment_seconds": production.get("segment_seconds") or legacy.get("segment_seconds") or 15,
        "mode": production.get("video_mode") or legacy.get("mode") or "grid",
        "theme_title": theme.get("title") or legacy.get("theme_title") or "项目蓝图",
        "video_type": theme.get("genre") or legacy.get("video_type") or "",
        "basic_answer": theme.get("style") or legacy.get("basic_answer") or "",
        "global_outline": story.get("global_outline") or legacy.get("global_outline") or "",
        "episodes": episodes,
        "characters": characters,
        "scenes": scenes,
        "shots": shots,
    }
    return {
        "kind": "creative_blueprint",
        "title": f"{blueprint['theme_title']}创意大纲",
        "summary": theme.get("logline") or story.get("global_outline") or doc.get("source_request") or "项目蓝图草稿",
        "source_request": doc.get("source_request") or "",
        "selected_video_mode": production.get("video_mode") or legacy.get("mode") or "grid",
        "blueprint": blueprint,
        "sections": [
            {"type": "markdown", "content": markdown},
            {"type": "outline_preview", "episodes": episodes},
            {"type": "characters_preview", "items": characters},
            {"type": "scenes_preview", "items": scenes},
            {"type": "shots_preview", "items": shots},
            {"type": "risks", "items": ["恢复自蓝图草稿；确认整体大纲前不会创建或运行画布节点。"]},
        ],
    }


def recover_pending_blueprint_section_review_state(
    *,
    project_id: str,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover section-review state from draft files when transient state is lost."""
    draft = state.get("pending_blueprint_draft") if isinstance(state.get("pending_blueprint_draft"), dict) else None
    progress = (
        state.get("blueprint_generation_progress")
        if isinstance(state.get("blueprint_generation_progress"), dict)
        else None
    )
    if not isinstance(draft, dict) or not isinstance(progress, dict):
        return None
    review_mode = str(progress.get("review_mode") or draft.get("review_mode") or "")
    if review_mode != "section_review":
        return None
    status = str(progress.get("status") or draft.get("status") or "")
    if status not in {"paused_for_section_review", "drafting", "pending_section_confirmation"}:
        return None
    try:
        next_section_index = int(
            progress.get("next_section_index")
            if progress.get("next_section_index") is not None
            else draft.get("next_section_index")
        )
    except (TypeError, ValueError):
        next_section_index = int(state.get("blueprint_progress", {}).get("completed_sections") or 0)
    next_section_index = max(0, min(next_section_index, len(BLUEPRINT_SECTION_ORDER)))

    rel_path = str(draft.get("file_json") or blueprint_paths(project_id)["draft_json"])
    doc = _read_json_file(Path(settings.PROJECT_ROOT) / rel_path)
    if not isinstance(doc, dict):
        return None
    if draft.get("id") and str(draft.get("id")) != str(doc.get("id")):
        return None
    if draft.get("version") and str(draft.get("version")) != str(doc.get("version")):
        return None

    plan = creative_plan_from_blueprint_document(doc)
    doc_progress = doc.get("generation_progress") if isinstance(doc.get("generation_progress"), dict) else {}
    window_progress = (
        state.get("blueprint_window_progress")
        if isinstance(state.get("blueprint_window_progress"), dict)
        else doc_progress.get("window_progress")
    )
    if isinstance(window_progress, dict):
        plan["_blueprint_window_progress"] = dict(window_progress)
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    legacy = doc.get("legacy_creative_blueprint") if isinstance(doc.get("legacy_creative_blueprint"), dict) else {}
    pending = {
        "stage": "structure",
        "raw_request": doc.get("source_request") or "",
        "basic_answer": theme.get("style") or legacy.get("basic_answer") or "",
        "selected_mode": production.get("video_mode") or legacy.get("mode") or "grid",
        "duration_seconds": theme.get("duration_seconds") or production.get("duration_seconds") or 15,
    }
    structure_answer = str(legacy.get("structure_answer") or "").strip()
    if not structure_answer:
        constraints = doc.get("constraints") if isinstance(doc.get("constraints"), dict) else {}
        requirements = constraints.get("user_requirements") if isinstance(constraints.get("user_requirements"), list) else []
        structure_answer = "\n".join(str(item) for item in requirements if item) or "继续生成蓝图"
    return {
        "pending": pending,
        "structure_answer": structure_answer,
        "plan_doc": plan,
        "next_section_index": next_section_index,
        "review_mode": review_mode,
        "window_progress": window_progress if isinstance(window_progress, dict) else None,
        "recovered_from": "pending_blueprint_draft",
        "updated_at": int(time.time()),
    }


def write_blueprint_files(project_id: str, doc: dict[str, Any], index: dict[str, Any]) -> dict[str, str]:
    sync_blueprint_outline_document(doc)
    last_error: OSError | None = None
    for root in ("data", "storage"):
        try:
            project_data_dir(project_id, create=True, root=root)
            paths = blueprint_paths(project_id, root=root)
            file_index = {
                **index,
                "file_json": paths["json"],
                "file_markdown": paths["markdown"],
                "file_view_model": paths["view_model"],
            }
            json_path = Path(paths["json_abs"])
            md_path = Path(paths["markdown_abs"])
            json_path.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            md_path.write_text(render_blueprint_markdown(doc, file_index), encoding="utf-8")
            view_model_path = Path(paths["view_model_abs"])
            view_model_path.write_text(
                json.dumps(
                    render_blueprint_view_model(doc, file_index),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ) + "\n",
                encoding="utf-8",
            )
            return paths
        except OSError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise OSError("Unable to write blueprint files")


def apply_blueprint_plan_to_state(
    *,
    project_id: str,
    state: dict[str, Any],
    plan: dict[str, Any],
    persist_files: bool = False,
) -> dict[str, Any]:
    previous = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
    version = int(previous.get("version") or 0) + 1 if previous else 1
    blueprint_id = f"bp_{int(time.time() * 1000)}" if not previous else str(previous.get("id") or f"bp_{int(time.time() * 1000)}")
    draft_doc = load_pending_blueprint_draft_document(project_id=project_id, state=state, plan=plan)
    if isinstance(draft_doc, dict):
        doc = dict(draft_doc)
        doc["status"] = "active"
        doc["updated_at"] = _now_iso()
        doc["validation_report"] = validate_blueprint_document(doc)
    else:
        doc = build_blueprint_document_from_plan(
            plan,
            state,
            blueprint_id=blueprint_id,
            version=version,
        )
        doc["status"] = "active"
    sync_blueprint_outline_document(doc)
    doc["validation_report"] = validate_blueprint_document(doc)
    checksum = _json_checksum(doc)
    paths = blueprint_paths(project_id)
    theme = doc.get("theme") if isinstance(doc.get("theme"), dict) else {}
    production = doc.get("production") if isinstance(doc.get("production"), dict) else {}
    title = str(theme.get("title") or "视频蓝图")
    index = {
        "id": doc.get("id"),
        "version": doc.get("version"),
        "status": "active",
        "theme_title": title,
        "short_summary": _safe_text(theme.get("logline") or plan.get("summary") or "", limit=1000),
        "file_json": paths["json"],
        "file_markdown": paths["markdown"],
        "file_view_model": paths["view_model"],
        "checksum": checksum,
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "approved_at": _now_iso(),
        "selected_video_mode": production.get("video_mode"),
        "duration_seconds": theme.get("duration_seconds"),
        "episode_count": production.get("episode_count"),
        "segment_seconds": production.get("segment_seconds"),
        "source_plan_id": plan.get("id"),
    }
    if persist_files:
        try:
            paths = write_blueprint_files(project_id, doc, index)
            index["file_json"] = paths["json"]
            index["file_markdown"] = paths["markdown"]
            index["file_view_model"] = paths["view_model"]
        except OSError as exc:
            index["file_error"] = str(exc)

    if previous:
        state.setdefault("blueprint_history", []).append({
            **previous,
            "status": "archived",
            "archived_at": _now_iso(),
        })
    state.setdefault("blueprint_history", []).append(index)
    state["project_blueprint"] = index
    state.pop("pending_blueprint_draft", None)
    state.pop("pending_blueprint_review", None)
    generation_progress = doc.get("generation_progress") if isinstance(doc.get("generation_progress"), dict) else {}
    section_results = generation_progress.get("sections") if isinstance(generation_progress.get("sections"), list) else []
    state["blueprint_generation_progress"] = generation_progress
    state["blueprint_section_results"] = section_results
    state["blueprint_progress"] = {
        "status": "active",
        "blueprint_id": index.get("id"),
        "blueprint_version": index.get("version"),
        "current_section": generation_progress.get("current_section"),
        "completed_sections": len(generation_progress.get("sections") or []),
        "total_sections": len(generation_progress.get("sections") or []),
        "review_mode": production.get("blueprint_review_mode"),
    }
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    metadata["title"] = title
    if production.get("episode_count"):
        metadata["episode_count"] = production.get("episode_count")
    if theme.get("duration_seconds"):
        metadata["duration_per_episode"] = theme.get("duration_seconds")
    state["metadata"] = metadata
    if production.get("video_mode"):
        state["selected_video_mode"] = production.get("video_mode")
        if production.get("video_mode") in {"grid", "frames", "story_template"}:
            state["project_mode"] = "video_production"
            state["project_sub_mode"] = production.get("video_mode")
    return {
        "ok": True,
        "blueprint": index,
        "document": doc,
        "title": title,
        "paths": paths,
    }


def attach_blueprint_to_plan(plan: dict[str, Any], blueprint_index: dict[str, Any] | None) -> None:
    if not isinstance(plan, dict) or not isinstance(blueprint_index, dict):
        return
    blueprint_id = blueprint_index.get("id")
    blueprint_version = blueprint_index.get("version")
    if not blueprint_id or not blueprint_version:
        return
    plan["blueprint_id"] = blueprint_id
    plan["blueprint_version"] = blueprint_version
    plan["blueprint_title"] = blueprint_index.get("theme_title")
    plan["blueprint_path"] = blueprint_index.get("file_markdown")
    for phase in plan.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for step in phase.get("steps") or []:
            if not isinstance(step, dict) or step.get("tool") != "node.create":
                continue
            inp = step.get("input") if isinstance(step.get("input"), dict) else {}
            fields = inp.get("fields") if isinstance(inp.get("fields"), dict) else {}
            fields = dict(fields)
            fields.setdefault("blueprint_id", blueprint_id)
            fields.setdefault("blueprint_version", blueprint_version)
            fields.setdefault("blueprint_title", blueprint_index.get("theme_title"))
            fields.setdefault("blueprint_path", blueprint_index.get("file_markdown"))
            inp["fields"] = fields
            step["input"] = inp


def validate_plan_blueprint_binding(plan: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    blueprint = state.get("project_blueprint") if isinstance(state.get("project_blueprint"), dict) else None
    if not blueprint:
        return None
    plan_blueprint_id = plan.get("blueprint_id")
    plan_blueprint_version = plan.get("blueprint_version")
    if plan_blueprint_id is None and plan_blueprint_version is None:
        return None
    if str(plan_blueprint_id) == str(blueprint.get("id")) and str(plan_blueprint_version) == str(blueprint.get("version")):
        return None
    return {
        "ok": False,
        "error": "执行计划引用的蓝图版本不是当前 active blueprint，禁止批准执行。",
        "error_kind": "stale_blueprint_plan",
        "plan_blueprint_id": plan_blueprint_id,
        "plan_blueprint_version": plan_blueprint_version,
        "active_blueprint_id": blueprint.get("id"),
        "active_blueprint_version": blueprint.get("version"),
    }


def clear_blueprint_state(state: dict[str, Any]) -> list[str]:
    cleared: list[str] = []
    for key in (
        "project_blueprint",
        "blueprint_progress",
        "pending_blueprint_intake",
        "pending_blueprint_review",
        "pending_blueprint_draft",
        "pending_blueprint_revision",
        "pending_blueprint_section_review",
        "pending_blueprint_confirmation",
        "semantic_blueprint",
        "blueprint_partial_plan_doc",
        "blueprint_generation_progress",
        "blueprint_section_results",
        "blueprint_window_progress",
        "blueprint_stale_nodes",
        "blueprint_history",
        "creative_blueprint_history",
        "video_generation_type",
        "image_to_video_method",
    ):
        if key in state:
            state.pop(key, None)
            cleared.append(key)
    return cleared


def delete_blueprint_files_report(project_id: str) -> dict[str, Any]:
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for root in ("data", "storage"):
        paths = blueprint_paths(project_id, root=root)
        for key in (
            "json_abs",
            "markdown_abs",
            "draft_json_abs",
            "draft_markdown_abs",
            "revision_json_abs",
            "revision_markdown_abs",
            "view_model_abs",
        ):
            path = Path(paths[key])
            try:
                path.unlink()
                deleted.append(str(path))
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append({
                    "path": str(path),
                    "error": str(exc),
                    "error_kind": type(exc).__name__,
                })
                continue
    return {"deleted": deleted, "errors": errors}


def delete_blueprint_files(project_id: str) -> list[str]:
    return list(delete_blueprint_files_report(project_id).get("deleted") or [])
