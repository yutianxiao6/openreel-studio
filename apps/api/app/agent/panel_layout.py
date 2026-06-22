"""Panel layout engine — bucket workflow nodes into a nested tier hierarchy.

The panel and canvas read from the same workflow_nodes table. This module
re-shapes the flat node list into the nested structure that the panel view
displays:

    global (L0)             → project_setting, outline, main/recurring chars, relationships
    episodes[N] (L1)        → script, review, segment_plan
        guests              → guest characters with episode_id only
        segments[seg_id]    → segment, guests, scenes, panoramas, shot_list/grid
            shots[shot_id]  → shot + 6 artifact slots
        exports             → episode_export
    exports                 → project_export
"""
from __future__ import annotations

import json
from typing import Any, Iterable

GLOBAL_TYPES = {
    "project_setting",
    "outline",
    "outline_generation",
    "character_relationship",
    "script_collection",  # 全剧剧本根
}
EPISODE_TYPES = {
    "episode_script", "episode_review", "episode_segment_plan",
    "episode_cast_scene_plan",  # 单集出场规划
    "script_generation", "script_review",
}
SEGMENT_TYPES = {
    "segment",
    # 12 类后段落级产物（统一进 segment 桶）
    "segment_storyboard",
    "segment_story_template",
    "segment_video_prompt",
    "segment_video_clip",
}
SCENE_TYPES = {"scene", "scene_image", "scene_image_prompt", "panorama", "panorama_view"}
SHOT_LIST_TYPES = {"shot_list", "storyboard_grid", "storyboard_generation"}
SHOT_TYPES = {"shot"}
# shot 子产物保留兼容映射（DB 里旧节点仍然能渲染到对应槽位）;
# 新画布上首尾帧已升级为独立融合节点,会同时落到下方 segment 桶。
SHOT_ARTIFACT_SLOT = {
    "shot_image_prompt": "image_prompt",
    "shot_reference_image": "reference_image",
    "shot_first_frame": "first_frame",
    "shot_last_frame": "last_frame",
    "shot_video_prompt": "video_prompt",
    "shot_video_clip": "video_clip",
    "image_prompt_generation": "image_prompt",
    "image_generation": "reference_image",
    "video_prompt_generation": "video_prompt",
    "video_generation": "video_clip",
}
# shot_first_frame / shot_last_frame 现在是独立融合节点,默认归到段落桶。
# 如果旧数据有 shot_id 关联且没有 segment_id,fall back 到 SHOT_ARTIFACT_SLOT。
SEGMENT_FUSION_SHOT_TYPES = {"shot_first_frame", "shot_last_frame"}
EXPORT_EPISODE_TYPES = {"episode_export"}
EXPORT_PROJECT_TYPES = {"project_export", "export"}

CHARACTER_TYPES = {"character", "character_generation", "character_image_prompt", "character_reference_image"}


def _parse(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _merged(node: dict[str, Any]) -> dict[str, Any]:
    inp = _parse(node.get("input_json"))
    out = _parse(node.get("output_json"))
    return {**inp, **out}


def _episode_num(node: dict[str, Any]) -> int | None:
    data = _merged(node)
    for key in ("episode_number", "episode", "ep_num"):
        v = data.get(key)
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, str) and v.isdigit() and int(v) > 0:
            return int(v)
    script = data.get("script")
    if isinstance(script, dict):
        v = script.get("episode_number") or script.get("episode")
        if isinstance(v, int) and v > 0:
            return v
    return None


def _segment_id(node: dict[str, Any]) -> str | None:
    data = _merged(node)
    for key in ("segment_id", "segment"):
        v = data.get(key)
        if v and isinstance(v, str):
            return v
    if node.get("type") == "segment":
        return node.get("id")
    return None


def _scene_id(node: dict[str, Any]) -> str | None:
    data = _merged(node)
    v = data.get("scene_id")
    return v if isinstance(v, str) and v else None


def _shot_id(node: dict[str, Any]) -> str | None:
    data = _merged(node)
    v = data.get("shot_id")
    if isinstance(v, str) and v:
        return v
    if node.get("type") == "shot":
        return node.get("id")
    return None


def _character_id(node: dict[str, Any]) -> str | None:
    data = _merged(node)
    v = data.get("character_id")
    if isinstance(v, str) and v:
        return v
    if node.get("type") in {"character", "character_generation"}:
        return node.get("id")
    return None


def _character_tier(node: dict[str, Any]) -> str:
    data = _merged(node)
    tier = data.get("tier") or data.get("character_tier")
    if tier in {"main", "recurring", "guest"}:
        return tier
    char = data.get("character") if isinstance(data.get("character"), dict) else None
    if char:
        t = char.get("tier")
        if t in {"main", "recurring", "guest"}:
            return t
    return "main"


def _summary(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": n.get("id"),
        "title": n.get("title"),
        "type": n.get("type"),
        "status": n.get("status"),
        "version": n.get("version", 1),
        "supersedes_id": n.get("supersedes_id"),
        "created_at": n.get("created_at"),
        "preview": n.get("preview"),
        "prompt": n.get("prompt"),
        "blueprint_id": n.get("blueprint_id"),
        "blueprint_source_paths": n.get("blueprint_source_paths"),
        "source_ids": n.get("source_ids"),
    }


def _ensure_episode(grid: dict[str, Any], num: int) -> dict[str, Any]:
    eps = grid["episodes"]
    key = str(num)
    if key not in eps:
        eps[key] = {
            "episode_number": num,
            "scripts": [],
            "reviews": [],
            "segment_plans": [],
            "scenes": [],
            "guests": [],
            "segments": {},
            "exports": [],
        }
    return eps[key]


def _ensure_segment(ep: dict[str, Any], seg_id: str) -> dict[str, Any]:
    segs = ep["segments"]
    if seg_id not in segs:
        segs[seg_id] = {
            "segment_id": seg_id,
            "info": [],
            "guests": [],
            "scenes": [],
            "shot_list": [],
            "storyboard_grid": [],
            "shots": {},
        }
    return segs[seg_id]


def _ensure_shot(seg: dict[str, Any], shot_id: str) -> dict[str, Any]:
    shots = seg["shots"]
    if shot_id not in shots:
        shots[shot_id] = {
            "shot_id": shot_id,
            "core": [],
            "image_prompt": [],
            "reference_image": [],
            "first_frame": [],
            "last_frame": [],
            "video_prompt": [],
            "video_clip": [],
        }
    return shots[shot_id]


def _classify_character(node: dict[str, Any], grid: dict[str, Any]) -> None:
    tier = _character_tier(node)
    summary = _summary(node)
    ep_num = _episode_num(node)
    seg_id = _segment_id(node)

    if tier in {"main", "recurring"}:
        grid["global"][f"characters_{tier}"].append(summary)
        return

    if seg_id and ep_num:
        ep = _ensure_episode(grid, ep_num)
        seg = _ensure_segment(ep, seg_id)
        seg["guests"].append(summary)
        return
    if ep_num:
        ep = _ensure_episode(grid, ep_num)
        ep["guests"].append(summary)
        return
    grid["global"]["characters_main"].append(summary)


def _classify_node(node: dict[str, Any], grid: dict[str, Any]) -> None:
    ntype = node.get("type") or ""
    summary = _summary(node)

    if ntype in CHARACTER_TYPES:
        if ntype in {"character_image_prompt", "character_reference_image"}:
            char_id = _character_id(node)
            slot = "prompts" if ntype == "character_image_prompt" else "images"
            for tier_key in ("characters_main", "characters_recurring"):
                for c in grid["global"][tier_key]:
                    if c["id"] == char_id:
                        c.setdefault("artifacts", {}).setdefault(slot, []).append(summary)
                        return
            grid["global"][f"character_{slot}_orphan"].append(summary)
            return
        _classify_character(node, grid)
        return

    if ntype in GLOBAL_TYPES:
        if ntype in {"outline", "outline_generation"}:
            grid["global"]["outlines"].append(summary)
        elif ntype == "character_relationship":
            grid["global"]["relationships"].append(summary)
        else:
            grid["global"]["settings"].append(summary)
        return

    if ntype in EXPORT_PROJECT_TYPES:
        grid["exports"].append(summary)
        return

    ep_num = _episode_num(node)
    if ep_num is None:
        if ntype in SCENE_TYPES:
            grid["global"]["scene_assets"].append(summary)
            return
        grid["unbucketed"].append(summary)
        return

    ep = _ensure_episode(grid, ep_num)

    if ntype in EXPORT_EPISODE_TYPES:
        ep["exports"].append(summary)
        return

    if ntype in EPISODE_TYPES:
        if ntype in {"episode_script", "script_generation"}:
            ep["scripts"].append(summary)
        elif ntype in {"episode_review", "script_review"}:
            ep["reviews"].append(summary)
        elif ntype == "episode_cast_scene_plan":
            ep.setdefault("cast_scene_plans", []).append(summary)
        else:
            ep["segment_plans"].append(summary)
        return

    seg_id = _segment_id(node)
    if seg_id is None and ntype in SEGMENT_TYPES:
        seg_id = node.get("id")

    if ntype in SCENE_TYPES and seg_id is None:
        ep.setdefault("scenes", []).append(summary)
        return

    if ntype in SEGMENT_TYPES and seg_id:
        seg = _ensure_segment(ep, seg_id)
        if ntype == "segment_storyboard":
            data = _merged(node)
            if data.get("mode") == "grid" or "grid" in str(data.get("layout", "")):
                seg["storyboard_grid"].append(summary)
            else:
                seg["shot_list"].append(summary)
        elif ntype == "segment_story_template":
            seg.setdefault("story_template", []).append(summary)
        elif ntype == "segment_video_prompt":
            seg.setdefault("video_prompt", []).append(summary)
        elif ntype == "segment_video_clip":
            seg.setdefault("video_clip", []).append(summary)
        else:
            seg["info"].append(summary)
        return

    if seg_id is None:
        ep.setdefault("loose", []).append(summary)
        return

    seg = _ensure_segment(ep, seg_id)

    if ntype in SCENE_TYPES:
        seg["scenes"].append(summary)
        return

    if ntype in SHOT_LIST_TYPES:
        if ntype in {"storyboard_grid"}:
            seg["storyboard_grid"].append(summary)
        else:
            seg["shot_list"].append(summary)
        return

    # 首尾帧融合节点：作为段落槽位（不再挂在单镜头下）
    if ntype in SEGMENT_FUSION_SHOT_TYPES:
        slot = "first_frames" if ntype == "shot_first_frame" else "last_frames"
        seg.setdefault(slot, []).append(summary)
        return

    shot_id = _shot_id(node)
    if shot_id:
        shot = _ensure_shot(seg, shot_id)
        if ntype in SHOT_TYPES:
            shot["core"].append(summary)
        elif ntype in SHOT_ARTIFACT_SLOT:
            slot = SHOT_ARTIFACT_SLOT[ntype]
            shot[slot].append(summary)
        else:
            shot["core"].append(summary)
        return

    seg.setdefault("loose", []).append(summary)


def bucket_nodes(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grid: dict[str, Any] = {
        "global": {
            "settings": [],
            "outlines": [],
            "characters_main": [],
            "characters_recurring": [],
            "scene_assets": [],
            "relationships": [],
            "character_prompts_orphan": [],
            "character_images_orphan": [],
        },
        "episodes": {},
        "exports": [],
        "unbucketed": [],
    }
    chars: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for n in nodes:
        ntype = n.get("type") or ""
        if ntype in {"character", "character_generation"}:
            chars.append(n)
        elif ntype in {"character_image_prompt", "character_reference_image"}:
            artifacts.append(n)
        else:
            others.append(n)

    for n in chars:
        _classify_node(n, grid)
    for n in artifacts:
        _classify_node(n, grid)
    for n in others:
        _classify_node(n, grid)

    return grid


def episode_order(grid: dict[str, Any]) -> list[int]:
    return sorted(int(k) for k in grid["episodes"].keys())
