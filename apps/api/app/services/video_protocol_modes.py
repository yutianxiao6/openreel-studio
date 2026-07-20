"""Derive OpenReel video modes from provider model capability metadata."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def _strings(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def derive_video_profile_modes(profile: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Translate upstream ``generate_types`` into OpenReel's canonical modes.

    Some provider catalogs expose per-model capabilities instead of repeating a
    complete ``modes`` object.  ``supports_image_num`` describes the multi-image
    reference capacity; a first-frame request remains a single-image mode.
    """
    profile = profile or {}
    generate_types = _strings(profile.get("generate_types"))
    if not generate_types:
        return {}

    reference_image_limit = max(
        1,
        _integer(profile.get("ref2v_max_images"), _integer(profile.get("supports_image_num"), 1)),
    )
    reference_video_limit = max(0, _integer(profile.get("max_ref_videos"), 0))
    reference_audio_limit = 1 if _boolean(profile.get("supports_audio_url")) else 0
    modes: dict[str, dict[str, Any]] = {}

    def with_duration(config: dict[str, Any], key: str) -> dict[str, Any]:
        duration = profile.get(f"{key}_duration")
        if isinstance(duration, dict):
            config["duration"] = deepcopy(duration)
        return config

    if "t2v" in generate_types:
        modes["text_to_video"] = with_duration({
            "label": "文生视频",
            "prompt_required": True,
            "max_images": 0,
            "max_videos": 0,
            "max_audios": 0,
            "request_mode": "t2v",
        }, "t2v")
    if "i2v" in generate_types:
        modes["first_frame"] = with_duration({
            "label": "图生视频",
            "prompt_required": True,
            "required_roles": ["first_frame"],
            "allowed_roles": ["first_frame"],
            "min_images": 1,
            "max_images": 1,
            "max_videos": 0,
            "max_audios": 0,
            "request_mode": "i2v",
        }, "i2v")
    if "firstlast" in generate_types or _boolean(profile.get("supports_firstlast")):
        modes["first_last_frame"] = with_duration({
            "label": "首尾帧",
            "prompt_required": True,
            "required_roles": ["first_frame", "last_frame"],
            "allowed_roles": ["first_frame", "last_frame"],
            "min_images": 2,
            "max_images": 2,
            "max_videos": 0,
            "max_audios": 0,
            "request_mode": "firstlast",
        }, "firstlast")
    if "ref2v" in generate_types:
        modes["multimodal_reference"] = with_duration({
            "label": "全能参考",
            "prompt_required": True,
            "allowed_roles": ["reference_image", "reference_video", "reference_audio"],
            "min_total_media": 1,
            "max_images": reference_image_limit,
            "max_videos": reference_video_limit,
            "max_audios": reference_audio_limit,
            "audio_requires_visual": reference_audio_limit > 0,
            "request_mode": "ref2v",
        }, "ref2v")
    if "video_edit" in generate_types:
        modes["video_edit"] = with_duration({
            "label": "视频编辑",
            "prompt_required": True,
            "allowed_roles": ["reference_image", "reference_video", "reference_audio"],
            "min_videos": 1,
            "max_images": reference_image_limit,
            "max_videos": 1,
            "max_audios": reference_audio_limit,
            "request_mode": "video_edit",
        }, "video_edit")
    if "clip2v" in generate_types:
        modes["video_continuation"] = with_duration({
            "label": "视频续写",
            "prompt_required": True,
            "allowed_roles": ["reference_image", "reference_video"],
            "min_videos": 1,
            "max_images": reference_image_limit,
            "max_videos": 1,
            "max_audios": 0,
            "request_mode": "clip2v",
        }, "clip2v")
    return modes
