"""Skill/freewrite prompt synthesis for legacy blueprint visual/media nodes."""
from __future__ import annotations

import json
import re
from typing import Any

from app.agent.storyboard_layout import STORYBOARD_DENSITY_RULE, normalize_storyboard_layout, storyboard_grid_label
from app.services.llm_service import LLMService


TEMPLATE_SYNTHESIS_VERSION = "model-selected-template-json-v2"

NODE_TEMPLATE_CATEGORY: dict[str, str] = {
    "character": "character_image",
    "scene": "scene_image",
    "segment_storyboard": "storyboard_image",
    "shot_first_frame": "first_frame_image",
    "shot_last_frame": "last_frame_image",
    "segment_story_template": "story_template",
    "segment_video_prompt": "video_prompt",
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _text(item))]
    if isinstance(value, str):
        return [text for item in re.split(r"[、,，;；|/]+", value) if (text := item.strip())]
    return []


def _normalize_result_schema(node_type: str, data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    normalized["prompt"] = _text(normalized.get("prompt"))
    normalized["negative_prompt"] = _text(normalized.get("negative_prompt"))
    normalized["aspect_ratio"] = _text(normalized.get("aspect_ratio"))
    normalized["style_tags"] = _text_list(normalized.get("style_tags"))

    if node_type == "segment_storyboard":
        normalized["cells"] = [cell for cell in _as_list(normalized.get("cells")) if isinstance(cell, dict)]
        layout = normalize_storyboard_layout(normalized.get("storyboard_layout") or normalized.get("grid") or normalized.get("layout"))
        normalized["storyboard_layout"] = layout
        normalized["grid"] = _text(normalized.get("grid")) or storyboard_grid_label(layout)
    if node_type == "segment_story_template":
        normalized["layout_modules"] = _text_list(normalized.get("layout_modules"))
    if node_type in {"shot_first_frame", "shot_last_frame"}:
        normalized["frame_role"] = _text(normalized.get("frame_role")).lower()
        normalized["continuity_notes"] = _text_list(normalized.get("continuity_notes"))
    if node_type == "segment_video_prompt":
        normalized["mode"] = _text(normalized.get("mode"))
        normalized["visual_anchors"] = _text_list(normalized.get("visual_anchors"))
        normalized["continuity_constraints"] = _text_list(normalized.get("continuity_constraints"))
        normalized["timeline"] = [beat for beat in _as_list(normalized.get("timeline")) if isinstance(beat, dict)]
        normalized["shots"] = [shot for shot in _as_list(normalized.get("shots")) if isinstance(shot, dict)]
        try:
            normalized["duration_seconds"] = int(float(str(normalized.get("duration_seconds")).strip()))
        except Exception:
            normalized["duration_seconds"] = None
    return normalized


def _compact_compare_text(value: Any) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】\-—_]+", "", str(value or ""))


def _story_copy_error(
    node_type: str,
    data: dict[str, Any],
    materialized: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    if node_type not in {
        "segment_storyboard",
        "shot_first_frame",
        "shot_last_frame",
        "segment_story_template",
        "segment_video_prompt",
    }:
        return ""
    prompt_text = _compact_compare_text(data.get("prompt"))
    if len(prompt_text) < 8:
        return ""
    payload_segment = _as_dict((payload or {}).get("segment"))
    segment = _as_dict(materialized.get("segment")) or payload_segment
    candidates = [
        segment.get("plot"),
        segment.get("description"),
        segment.get("segment_arc"),
        payload_segment.get("plot"),
        payload_segment.get("description"),
        payload_segment.get("segment_arc"),
        materialized.get("plot"),
        materialized.get("description"),
    ]
    for candidate in candidates:
        story_text = _compact_compare_text(candidate)
        if len(story_text) < 8:
            continue
        if prompt_text == story_text or (story_text in prompt_text and len(prompt_text) <= int(len(story_text) * 1.25)):
            return "模板输出 prompt 只是复述蓝图剧情，没有转写成画面/镜头/运动提示词。"
    return ""


def _template_checksum(text: str) -> str:
    return ""


def _extract_json(text: str, default: object) -> object:
    text = str(text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    pattern = r"\[.*\]" if isinstance(default, list) else r"\{.*\}"
    match = re.search(pattern, text, re.DOTALL)
    try:
        return json.loads(match.group()) if match else default
    except Exception:
        return default


def _json_block(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _candidate_summary(category: str, query: str | None = None) -> list[dict[str, Any]]:
    return []


def _selection_query(node_type: str, payload: dict[str, Any]) -> str:
    project = _as_dict(payload.get("project"))
    visual = _as_dict(payload.get("visual_strategy"))
    segment = _as_dict(payload.get("segment"))
    fields = _as_dict(payload.get("node_fields"))
    bits = [
        node_type,
        project.get("style"),
        project.get("video_mode"),
        segment.get("plot"),
        segment.get("scene_design"),
        fields.get("style"),
        fields.get("requirements"),
        visual,
    ]
    return " ".join(_text(bit) for bit in bits if _text(bit))[:1200]


async def _choose_template_reference(
    *,
    svc: LLMService,
    project_id: str,
    node_type: str,
    category: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "strategy": "freewrite",
        "category": category,
        "template_key": None,
        "template_text": "",
        "template_checksum": "",
        "reason": "提示词写法来自 skill 或模型按当前节点目标自由组织。",
        "candidates": [],
    }


def _names_from_refs(refs: list[Any]) -> list[str]:
    names: list[str] = []
    for ref in refs:
        if isinstance(ref, dict):
            value = ref.get("name") or ref.get("id") or ref.get("character_id") or ref.get("scene_id")
        else:
            value = ref
        text = _text(value)
        if text and text not in names:
            names.append(text)
    return names


def _match_names(items: list[Any], names: list[str], *keys: str) -> list[dict[str, Any]]:
    if not names:
        return [item for item in items if isinstance(item, dict)][:3]
    matched: list[dict[str, Any]] = []
    wanted = set(names)
    for item in items:
        if not isinstance(item, dict):
            continue
        values = {_text(item.get(key)) for key in keys}
        if values & wanted:
            matched.append(item)
    return matched[:5]


def build_template_payload(
    *,
    node_type: str,
    doc: dict[str, Any],
    index: dict[str, Any],
    fields: dict[str, Any],
    materialized: dict[str, Any],
) -> dict[str, Any]:
    theme = _as_dict(doc.get("theme"))
    production = _as_dict(doc.get("production"))
    segment = _as_dict(materialized.get("segment"))
    character_names = _names_from_refs(_as_list(materialized.get("characters") or segment.get("characters") or segment.get("cast_refs")))
    scene_names = _names_from_refs(_as_list(materialized.get("scenes") or segment.get("scene_refs") or segment.get("scene_ids")))
    characters = _match_names(_as_list(doc.get("characters")), character_names, "name", "character_id", "id")
    scenes = _match_names(_as_list(doc.get("scenes")), scene_names, "name", "scene_id", "id", "location")
    visual_strategy = _as_dict(doc.get("visual_strategy"))
    reference_images = (
        _as_list(fields.get("reference_images"))
        or _as_list(materialized.get("reference_images"))
        or [
            item.get("rel_path")
            for item in _as_list(doc.get("reference_images"))
            if isinstance(item, dict) and item.get("rel_path")
        ]
    )
    reference_image_details = (
        _as_list(fields.get("reference_image_details"))
        or _as_list(materialized.get("reference_image_details"))
        or _as_list(doc.get("reference_images"))
    )
    storyboard_spec = None
    if node_type == "segment_storyboard":
        layout = normalize_storyboard_layout(
            fields.get("layout")
            or fields.get("storyboard_layout")
            or segment.get("storyboard_layout")
            or segment.get("storyboard_grid")
            or materialized.get("storyboard_layout")
            or materialized.get("layout")
        )
        storyboard_spec = {
            "layout": layout,
            "grid": storyboard_grid_label(layout),
            "layout_reason": (
                fields.get("storyboard_layout_reason")
                or segment.get("storyboard_layout_reason")
                or segment.get("layout_reason")
                or materialized.get("storyboard_layout_reason")
                or ""
            ),
            "density_rule": STORYBOARD_DENSITY_RULE,
        }
    return {
        "node_type": node_type,
        "blueprint_ref": {
            "id": doc.get("id") or index.get("id"),
            "version": doc.get("version") or index.get("version"),
            "checksum": index.get("checksum"),
            "source_paths": materialized.get("blueprint_source_paths") or [],
            "source_ids": materialized.get("source_ids") or {},
        },
        "project": {
            "title": theme.get("title") or index.get("theme_title"),
            "style": theme.get("style"),
            "logline": theme.get("logline"),
            "aspect_ratio": production.get("aspect_ratio") or theme.get("aspect_ratio"),
            "video_mode": production.get("video_mode"),
            "duration_seconds": theme.get("duration_seconds") or production.get("duration_seconds"),
        },
        "node_fields": fields,
        "materialized_blueprint_excerpt": {
            key: value
            for key, value in materialized.items()
            if key
            in {
                "name",
                "episode_number",
                "segment_index",
                "segment_id",
                "shot_id",
                "characters",
                "scenes",
            }
        },
        "character": materialized.get("character") if node_type == "character" else None,
        "scene": materialized.get("scene") if node_type == "scene" else None,
        "segment": segment if segment else None,
        "storyboard_spec": storyboard_spec,
        "segment_characters": characters,
        "segment_scenes": scenes,
        "visual_strategy": visual_strategy,
        "requirements": _as_list(_as_dict(doc.get("constraints")).get("user_requirements")),
        "visual_reference_nodes": _as_list(fields.get("visual_reference_nodes")),
        "reference_images": reference_images,
        "reference_image_details": reference_image_details,
        "no_visual_references": bool(fields.get("no_visual_references")),
    }


def _node_contract(node_type: str) -> str:
    if node_type == "character":
        return (
            "生成角色参考图 prompt。只写人物设计，不演剧情；必须突出外貌、服装、气质、关键道具、可复用性。"
        )
    if node_type == "scene":
        return (
            "生成场景概念图 prompt。只写空间设计，不放无关人物；必须突出空间结构、光线方向、关键道具、可拍摄区域。"
        )
    if node_type == "segment_storyboard":
        return (
            "生成一张多宫格分镜图 prompt。必须把剧情转成逐格镜头设计，并返回 cells 数组；"
            "宫格数必须等于 payload.storyboard_spec.layout；"
            "每格包含 time、row、col、shot_type、composition、character_blocking、action、camera、lighting。"
        )
    if node_type in {"shot_first_frame", "shot_last_frame"}:
        role = "首帧动作起势" if node_type == "shot_first_frame" else "尾帧动作结果"
        return (
            f"生成段落{role}的单张参考图 prompt。必须明确主体站位、动作方向、情绪状态、"
            "场景锚点和后续视频连续性；不要输出宫格、漫画页或多张画面。"
        )
    if node_type == "segment_story_template":
        return (
            "生成故事模板图 prompt，image 节点 fields.resolution 推荐写 3840x2160。它是影视开发图纸，不是海报；故事分镜/动作流程区必须占最大面积，"
            "并用辅助模块呈现场景空间、角色锚点、镜头运动、光线材质和色彩关系。"
        )
    if node_type == "segment_video_prompt":
        return (
            "生成图生视频 prompt。不要复述剧情，必须把参考视觉转成运动控制；返回 timeline 数组，"
            "每段包含 time、camera、subject_motion、environment_motion、continuity。"
        )
    return "生成符合节点类型的提示词 JSON。"


def _output_schema(node_type: str) -> dict[str, Any]:
    common = {
        "prompt": "最终喂给图片/视频模型的中文提示词",
        "negative_prompt": "负面约束，中文或通用英文标签均可",
        "aspect_ratio": "项目画幅比例",
        "style_tags": ["风格标签"],
    }
    if node_type == "segment_storyboard":
        common["cells"] = [
            {
                "time": "0-3s",
                "row": 1,
                "col": 1,
                "shot_type": "中景",
                "composition": "构图和景别",
                "character_blocking": "人物站位",
                "action": "动作起止",
                "camera": "镜头方向",
                "lighting": "光线氛围",
            }
        ]
        common["storyboard_layout"] = "必须等于 payload.storyboard_spec.layout"
        common["grid"] = "必须等于 payload.storyboard_spec.grid"
    if node_type == "segment_video_prompt":
        common.update(
            {
                "duration_seconds": 15,
                "mode": "grid|frames|story_template|text_to_video",
                "timeline": [
                    {
                        "time": "0-3s",
                        "camera": "缓慢推进",
                        "subject_motion": "主体动作",
                        "environment_motion": "环境运动",
                        "continuity": "保持人物和场景一致",
                    }
                ],
                "visual_anchors": ["使用的参考图/分镜/首尾帧"],
                "continuity_constraints": ["不换脸", "不换装", "不新增角色"],
                "motion_intensity": "low|medium|high",
            }
        )
    if node_type in {"shot_first_frame", "shot_last_frame"}:
        common.update(
            {
                "frame_role": "first|last",
                "continuity_notes": ["主体站位", "动作方向", "服装场景一致性"],
            }
        )
    if node_type == "segment_story_template":
        common["layout_modules"] = ["故事分镜/动作流程区", "空间动线区", "角色连续性区", "镜头与美术区"]
    return common


def _validate_result(
    node_type: str,
    data: dict[str, Any],
    materialized: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    if len(_text(data.get("prompt"))) < 12:
        return "模板输出缺少有效 prompt。"
    if not _text(data.get("negative_prompt")):
        return "模板输出缺少有效 negative_prompt。"
    if not _text(data.get("aspect_ratio")):
        return "模板输出缺少有效 aspect_ratio。"
    if not _text_list(data.get("style_tags")):
        return "模板输出必须包含 style_tags 数组。"
    if node_type == "segment_storyboard":
        cells = _as_list(data.get("cells"))
        if not cells:
            return "分镜模板输出必须包含 cells 数组。"
        storyboard_spec = _as_dict((payload or {}).get("storyboard_spec"))
        expected_layout = normalize_storyboard_layout(storyboard_spec.get("layout")) if storyboard_spec else 0
        if expected_layout and len(cells) != expected_layout:
            return f"分镜 cells 数量必须等于 storyboard_layout={expected_layout}，当前为 {len(cells)}。"
        required = ("time", "row", "col", "shot_type", "composition", "character_blocking", "action", "camera", "lighting")
        for index, cell in enumerate(cells, start=1):
            missing = [key for key in required if not _text(cell.get(key))]
            if missing:
                return f"分镜 cells[{index}] 缺少字段: {', '.join(missing)}。"
    if node_type == "segment_story_template" and not _text_list(data.get("layout_modules")):
        return "故事模板图输出必须包含 layout_modules 数组。"
    if node_type in {"shot_first_frame", "shot_last_frame"}:
        expected_role = "first" if node_type == "shot_first_frame" else "last"
        if _text(data.get("frame_role")).lower() != expected_role:
            return f"{'首帧' if expected_role == 'first' else '尾帧'}模板输出 frame_role 必须为 {expected_role}。"
        if not _text_list(data.get("continuity_notes")):
            return "首尾帧模板输出必须包含 continuity_notes 数组。"
    if node_type == "segment_video_prompt":
        if not isinstance(data.get("duration_seconds"), int) or data.get("duration_seconds") <= 0:
            return "视频提示词模板输出必须包含有效 duration_seconds。"
        if not _text(data.get("mode")):
            return "视频提示词模板输出必须包含 mode。"
        timeline = _as_list(data.get("timeline"))
        shots = _as_list(data.get("shots"))
        if not (timeline or shots):
            return "视频提示词模板输出必须包含 timeline 或 shots 数组。"
        for index, beat in enumerate(timeline, start=1):
            missing = [
                key
                for key in ("time", "camera", "subject_motion", "environment_motion", "continuity")
                if not _text(beat.get(key))
            ]
            if missing:
                return f"视频 timeline[{index}] 缺少字段: {', '.join(missing)}。"
        if not _text_list(data.get("visual_anchors")):
            return "视频提示词模板输出必须包含 visual_anchors 数组。"
        if not _text_list(data.get("continuity_constraints")):
            return "视频提示词模板输出必须包含 continuity_constraints 数组。"
    copy_error = _story_copy_error(node_type, data, materialized or {}, payload)
    if copy_error:
        return copy_error
    return ""


async def synthesize_visual_prompt_from_blueprint(
    *,
    project_id: str,
    session: Any,
    node_type: str,
    doc: dict[str, Any],
    index: dict[str, Any],
    fields: dict[str, Any],
    materialized: dict[str, Any],
) -> dict[str, Any]:
    category = _text(materialized.get("template_category")) or NODE_TEMPLATE_CATEGORY.get(node_type, "")
    if not category:
        return {
            **materialized,
            "ok": False,
            "error": f"{node_type!r} 没有可用提示词模板类别。",
            "error_kind": "template_category_missing",
        }
    payload = build_template_payload(
        node_type=node_type,
        doc=doc,
        index=index,
        fields=fields,
        materialized=materialized,
    )
    svc = LLMService(session)
    selection = await _choose_template_reference(
        svc=svc,
        project_id=project_id,
        node_type=node_type,
        category=category,
        payload=payload,
    )
    template_text = _text(selection.get("template_text"))
    template_key = _text(selection.get("template_key"))
    prompt_source = "model_written"
    template_block = (
        "## Skill 参考\n"
        "下面内容来自当前 skill 或用户自定义流程。"
        "它不是默认值,不是硬约束;必须结合当前用户场景改写,不能机械套用。\n\n"
        f"{template_text.rstrip()}\n"
        if template_text
        else (
            "## 自由写作模式\n"
            "请根据当前用户场景、skill、蓝图和节点目标直接设计最合适的提示词结构。"
        )
    )
    system = (
        template_block.rstrip()
        + "\n\n## 后端强制边界\n"
        "- 只能使用 user payload 中的蓝图事实，不能新增剧情、角色、场景或设定。\n"
        "- 不要照抄剧情摘要；必须把蓝图事实改写成可执行的画面/运动提示词。\n"
        "- segment_video_prompt 只写最终视频提示词；人物、场景、分镜图、首尾帧图片和故事模板图属于视觉资产提示词。\n"
        "- 严格输出 JSON，不要 markdown，不要解释。\n"
    )
    user_prompt = (
        f"节点类型:{node_type}\n"
        f"节点任务:{_node_contract(node_type)}\n\n"
        "## 蓝图摘录 payload\n"
        f"{_json_block(payload)}\n\n"
        "## 模板选择结果\n"
        f"{_json_block({key: value for key, value in selection.items() if key != 'template_text'})}\n\n"
        "## 必须输出的 JSON 字段\n"
        f"{_json_block(_output_schema(node_type))}\n"
    )
    task_type = "video_prompt_generation" if node_type == "segment_video_prompt" else "image_prompt_generation"
    result = await svc.generate(
        task_type=task_type,
        messages=[{"role": "user", "content": user_prompt}],
        system=system,
        project_id=project_id,
    )
    content = _text(_as_dict(result).get("content") if isinstance(result, dict) else getattr(result, "content", ""))
    data = _extract_json(content, default={})
    if not isinstance(data, dict):
        data = {"prompt": str(data)}
    data = _normalize_result_schema(node_type, data)
    error = _validate_result(node_type, data, materialized, payload)
    if error:
        return {
            **materialized,
            "ok": False,
            "error": error,
            "error_kind": "template_output_invalid",
            "template_category": category,
            "template_key": template_key or None,
            "template_checksum": selection.get("template_checksum") or "",
            "template_selection_strategy": selection.get("strategy"),
            "template_selection_reason": selection.get("reason"),
        }
    return {
        **materialized,
        **data,
        "ok": True,
        "prompt": _text(data.get("prompt")),
        "storyboard_layout": data.get("storyboard_layout") if node_type == "segment_storyboard" else None,
        "grid": data.get("grid") if node_type == "segment_storyboard" else data.get("grid"),
        "reference_images": (
            _as_list(data.get("reference_images"))
            or _as_list(fields.get("reference_images"))
            or _as_list(materialized.get("reference_images"))
        ),
        "reference_image_details": (
            _as_list(data.get("reference_image_details"))
            or _as_list(fields.get("reference_image_details"))
            or _as_list(materialized.get("reference_image_details"))
        ),
        "visual_reference_nodes": _as_list(fields.get("visual_reference_nodes")),
        "prompt_source": prompt_source,
        "template_category": category,
        "template_key": template_key or None,
        "template_checksum": selection.get("template_checksum") or "",
        "template_selection_strategy": selection.get("strategy"),
        "template_selection_reason": selection.get("reason"),
        "template_candidates_considered": [
            {
                "category": item.get("category"),
                "key": item.get("key"),
                "title": item.get("title"),
                "builtin": item.get("builtin"),
            }
            for item in _as_list(selection.get("candidates"))[:12]
            if isinstance(item, dict)
        ],
        "template_version": TEMPLATE_SYNTHESIS_VERSION,
        "synthesis_payload_schema": "blueprint_visual_prompt_payload_v1",
    }
