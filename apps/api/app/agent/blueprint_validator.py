"""Deterministic validation for project blueprints.

The validator checks structure, references, durations, and production-mode
consistency without rewriting story facts. Reports are meant for UI and agent
control flow, so issue messages must stay user-readable.
"""
from __future__ import annotations

from typing import Any


VALID_VIDEO_MODES = {"grid", "frames", "story_template", "text_to_video"}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _issue(
    code: str,
    severity: str,
    message: str,
    *,
    section_id: str,
    path: str,
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "section_id": section_id,
        "path": path,
        "message": message,
    }


def _key_for(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(item.get(key))
        if value:
            return value
    return ""


def _duplicate_issues(
    items: list[Any],
    *,
    keys: tuple[str, ...],
    section_id: str,
    path: str,
    label: str,
) -> list[dict[str, str]]:
    seen: set[str] = set()
    issues: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        key = _key_for(item, keys)
        if not key:
            continue
        if key in seen:
            issues.append(_issue(
                "duplicate_id",
                "error",
                f"{label}存在重复标识: {key}",
                section_id=section_id,
                path=f"{path}[{index}]",
            ))
        seen.add(key)
    return issues


def _stable_id_issues(
    items: list[Any],
    *,
    id_key: str,
    section_id: str,
    path: str,
    label: str,
) -> list[dict[str, str]]:
    seen: set[str] = set()
    issues: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        stable_id = _text(item.get(id_key))
        if not stable_id:
            issues.append(_issue(
                "missing_stable_id",
                "warning",
                f"{label}缺少稳定 ID 字段 {id_key}，后续版本迁移时应补齐。",
                section_id=section_id,
                path=f"{path}[{index}].{id_key}",
            ))
            continue
        if stable_id in seen:
            issues.append(_issue(
                "duplicate_stable_id",
                "error",
                f"{label}存在重复稳定 ID: {stable_id}",
                section_id=section_id,
                path=f"{path}[{index}].{id_key}",
            ))
        seen.add(stable_id)
    return issues


def validate_blueprint_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Validate a canonical project blueprint document.

    Returns a stable report:
      {
        ok: bool,
        status: "passed" | "needs_revision",
        error_count: int,
        warning_count: int,
        issues: [{code,severity,section_id,path,message}, ...],
      }
    """
    issues: list[dict[str, str]] = []
    theme = _as_dict(doc.get("theme"))
    production = _as_dict(doc.get("production"))
    story = _as_dict(doc.get("story"))
    episodes = _as_list(story.get("episodes"))
    characters = _as_list(doc.get("characters"))
    scenes = _as_list(doc.get("scenes"))
    visual_strategy = _as_dict(doc.get("visual_strategy"))
    constraints = _as_dict(doc.get("constraints"))

    if not _text(doc.get("id")):
        issues.append(_issue("missing_required", "error", "蓝图缺少 id。", section_id="requirements_digest", path="id"))
    if _positive_int(doc.get("version")) is None:
        issues.append(_issue("missing_required", "error", "蓝图缺少有效版本号。", section_id="requirements_digest", path="version"))
    if not _text(theme.get("title")):
        issues.append(_issue("missing_required", "error", "蓝图缺少主题标题。", section_id="requirements_digest", path="theme.title"))
    if not _text(story.get("global_outline")):
        issues.append(_issue("missing_required", "error", "蓝图缺少故事总纲。", section_id="global_story_outline", path="story.global_outline"))

    mode = _text(production.get("video_mode"))
    if mode not in VALID_VIDEO_MODES:
        issues.append(_issue(
            "invalid_video_mode",
            "error",
            "制作模式必须是 grid、frames、story_template 或 text_to_video。",
            section_id="production_spec",
            path="production.video_mode",
        ))
    elif mode not in visual_strategy:
        issues.append(_issue(
            "visual_strategy_mismatch",
            "error",
            f"视觉策略中缺少当前制作模式 {mode} 的策略。",
            section_id="visual_strategy",
            path=f"visual_strategy.{mode}",
        ))

    duration = _positive_int(theme.get("duration_seconds"))
    if duration is None:
        issues.append(_issue("invalid_duration", "error", "总时长必须是正整数秒。", section_id="production_spec", path="theme.duration_seconds"))

    segment_seconds = _positive_int(production.get("segment_seconds"))
    if segment_seconds is None:
        issues.append(_issue("invalid_segment_seconds", "error", "分段秒数必须是正整数。", section_id="production_spec", path="production.segment_seconds"))
    elif segment_seconds > 15:
        issues.append(_issue("segment_too_long", "error", "每段时长最多 15 秒。", section_id="production_spec", path="production.segment_seconds"))

    episode_count = _positive_int(production.get("episode_count"))
    if episode_count is None:
        issues.append(_issue("invalid_episode_count", "error", "集数必须是正整数。", section_id="episode_index", path="production.episode_count"))
    elif episodes and len([ep for ep in episodes if isinstance(ep, dict)]) != episode_count:
        issues.append(_issue(
            "episode_count_mismatch",
            "warning",
            "剧集列表数量和 production.episode_count 不一致，请确认是否需要补齐或调整。",
            section_id="episode_index",
            path="story.episodes",
        ))

    if not episodes:
        issues.append(_issue("missing_episodes", "warning", "蓝图还没有剧集索引。", section_id="episode_index", path="story.episodes"))

    issues.extend(_stable_id_issues(
        episodes,
        id_key="episode_id",
        section_id="episode_index",
        path="story.episodes",
        label="剧集",
    ))
    issues.extend(_stable_id_issues(
        characters,
        id_key="character_id",
        section_id="character_bible",
        path="characters",
        label="人物",
    ))
    issues.extend(_stable_id_issues(
        scenes,
        id_key="scene_id",
        section_id="scene_bible",
        path="scenes",
        label="场景",
    ))

    issues.extend(_duplicate_issues(
        episodes,
        keys=("episode_id", "episode_number"),
        section_id="episode_index",
        path="story.episodes",
        label="剧集",
    ))
    issues.extend(_duplicate_issues(
        characters,
        keys=("character_id", "name"),
        section_id="character_bible",
        path="characters",
        label="人物",
    ))
    issues.extend(_duplicate_issues(
        scenes,
        keys=("scene_id", "name", "location"),
        section_id="scene_bible",
        path="scenes",
        label="场景",
    ))

    character_refs = {
        _key_for(item, ("character_id", "name"))
        for item in characters
        if isinstance(item, dict)
    }
    character_refs.discard("")
    scene_refs = {
        _key_for(item, ("scene_id", "name", "location"))
        for item in scenes
        if isinstance(item, dict)
    }
    scene_refs.discard("")

    segment_keys: set[str] = set()
    segment_stable_ids: set[str] = set()
    for ep_index, episode in enumerate(episodes):
        if not isinstance(episode, dict):
            issues.append(_issue(
                "invalid_episode",
                "error",
                "剧集条目必须是对象。",
                section_id="episode_index",
                path=f"story.episodes[{ep_index}]",
            ))
            continue
        ep_key = _key_for(episode, ("episode_id", "episode_number")) or str(ep_index + 1)
        segments = _as_list(episode.get("segments"))
        if not segments:
            issues.append(_issue(
                "missing_segments",
                "warning",
                f"第 {ep_key} 集还没有分段剧情。",
                section_id="segment_breakdown",
                path=f"story.episodes[{ep_index}].segments",
            ))
        for seg_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                issues.append(_issue(
                    "invalid_segment",
                    "error",
                    "分段条目必须是对象。",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}]",
                ))
                continue
            segment_key = _key_for(segment, ("segment_id", "segment_index")) or str(seg_index + 1)
            segment_stable_id = _text(segment.get("segment_id"))
            if not segment_stable_id:
                issues.append(_issue(
                    "missing_stable_id",
                    "warning",
                    "分段缺少稳定 ID 字段 segment_id，后续版本迁移时应补齐。",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}].segment_id",
                ))
            elif segment_stable_id in segment_stable_ids:
                issues.append(_issue(
                    "duplicate_stable_id",
                    "error",
                    f"蓝图存在重复分段稳定 ID: {segment_stable_id}",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}].segment_id",
                ))
            segment_stable_ids.add(segment_stable_id)
            compound_key = f"{ep_key}:{segment_key}"
            if compound_key in segment_keys:
                issues.append(_issue(
                    "duplicate_id",
                    "error",
                    f"第 {ep_key} 集存在重复分段标识: {segment_key}",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}]",
                ))
            segment_keys.add(compound_key)
            seg_duration = _positive_int(segment.get("duration_seconds"))
            if seg_duration is None:
                issues.append(_issue(
                    "invalid_segment_duration",
                    "warning",
                    f"第 {ep_key} 集第 {segment_key} 段缺少有效时长，将按默认分段时长处理。",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}].duration_seconds",
                ))
            elif seg_duration > 15:
                issues.append(_issue(
                    "segment_too_long",
                    "error",
                    f"第 {ep_key} 集第 {segment_key} 段超过 15 秒。",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}].duration_seconds",
                ))
            if not (_text(segment.get("plot")) or _text(segment.get("description"))):
                issues.append(_issue(
                    "missing_segment_plot",
                    "warning",
                    f"第 {ep_key} 集第 {segment_key} 段缺少剧情描述。",
                    section_id="segment_breakdown",
                    path=f"story.episodes[{ep_index}].segments[{seg_index}]",
                ))

            for ref in _as_list(segment.get("cast_refs")):
                ref_text = _text(ref)
                if ref_text and character_refs and ref_text not in character_refs:
                    issues.append(_issue(
                        "unresolved_cast_ref",
                        "error",
                        f"分段引用了不存在的人物: {ref_text}",
                        section_id="segment_breakdown",
                        path=f"story.episodes[{ep_index}].segments[{seg_index}].cast_refs",
                    ))
            for ref in _as_list(segment.get("scene_refs")):
                ref_text = _text(ref)
                if ref_text and scene_refs and ref_text not in scene_refs:
                    issues.append(_issue(
                        "unresolved_scene_ref",
                        "error",
                        f"分段引用了不存在的场景: {ref_text}",
                        section_id="segment_breakdown",
                        path=f"story.episodes[{ep_index}].segments[{seg_index}].scene_refs",
                    ))

    if not _as_list(constraints.get("user_requirements")):
        issues.append(_issue(
            "missing_user_constraints",
            "warning",
            "蓝图没有记录用户硬性要求，后续修改时可能丢失约束。",
            section_id="requirements_digest",
            path="constraints.user_requirements",
        ))

    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    warning_count = sum(1 for issue in issues if issue.get("severity") == "warning")
    return {
        "ok": error_count == 0,
        "status": "passed" if error_count == 0 else "needs_revision",
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
