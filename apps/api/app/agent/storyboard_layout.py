"""Storyboard grid layout helpers.

The model chooses shot density in the blueprint. These helpers only normalize
that explicit choice so downstream nodes do not silently fall back to 4 panels.
"""
from __future__ import annotations

import re
from typing import Any


VALID_STORYBOARD_LAYOUTS = (4, 6, 9)

STORYBOARD_DENSITY_RULE = (
    "宫格数是单个 video segment 内的镜头密度，不是视频分段数量。"
    "4格(2x2)适合聊天、走路、平滑视角推进；"
    "6格(2x3)适合需要动作推进、人物反应、特写的段落；"
    "9格(3x3)适合打斗、追逐、快速调度等高密度段落。"
    "选择时看段落的镜头/动作/特写需求，只允许4、6、9宫格。"
)


def normalize_storyboard_layout(value: Any, *, default: int = 4) -> int:
    if isinstance(value, bool):
        return default if default in VALID_STORYBOARD_LAYOUTS else 4
    if isinstance(value, int):
        return value if value in VALID_STORYBOARD_LAYOUTS else default
    raw = str(value or "").strip().lower()
    if not raw:
        return default if default in VALID_STORYBOARD_LAYOUTS else 4
    match = re.search(r"(\d+)\s*[x×*]\s*(\d+)", raw)
    if match:
        count = int(match.group(1)) * int(match.group(2))
        return count if count in VALID_STORYBOARD_LAYOUTS else default
    match = re.search(r"(\d+)", raw)
    if match:
        count = int(match.group(1))
        return count if count in VALID_STORYBOARD_LAYOUTS else default
    if "九" in raw:
        return 9
    if "六" in raw:
        return 6
    if "四" in raw:
        return 4
    return default if default in VALID_STORYBOARD_LAYOUTS else 4


def storyboard_grid_label(layout: Any) -> str:
    count = normalize_storyboard_layout(layout)
    return {4: "2x2", 6: "2x3", 9: "3x3"}[count]


def storyboard_layout_from_segment(segment: dict[str, Any], *, default: int = 4) -> int:
    for key in (
        "storyboard_layout",
        "storyboard_panel_count",
        "storyboard_cells",
        "grid_layout",
        "grid",
        "layout",
    ):
        if key in segment and segment.get(key) not in (None, "", [], {}):
            return normalize_storyboard_layout(segment.get(key), default=default)
    return default if default in VALID_STORYBOARD_LAYOUTS else 4

