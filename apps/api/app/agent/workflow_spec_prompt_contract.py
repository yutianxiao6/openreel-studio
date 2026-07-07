"""Shared prompt fragments for workflow spec authoring."""
from __future__ import annotations

import json


AUTHORING_SPEC_EXAMPLE = {
    "schema": "openreel.workflow.authoring.v1",
    "id": "text_to_segmented_video",
    "name": "文生分段视频",
    "description": "输入剧情、时长和风格，生成剧本、分段文本和分段视频。",
    "inputs": [
        {"id": "plot", "label": "剧情主题", "type": "long_text", "required": True},
        {"id": "durationSeconds", "label": "总时长", "type": "number", "default": 30, "required": True},
        {"id": "segmentSeconds", "label": "每段时长", "type": "number", "default": 15},
        {"id": "style", "label": "视觉风格", "type": "text", "default": "电影感写实"},
    ],
    "dimensions": {
        "segments": {"source": "steps.segments.output.items", "scope_key": "segment"},
    },
    "steps": [
        {
            "id": "full_script",
            "title": "完整剧本",
            "kind": "text",
            "visible": True,
            "prompt": {
                "role": "短剧编剧",
                "task": "根据 {{inputs.plot}}、{{inputs.durationSeconds}} 秒和 {{inputs.style}} 写完整剧本。",
                "output": "只输出连续剧情正文。",
                "check": "有开端、推进和结尾。",
            },
            "output": {"canvas": True, "key": "full_script"},
        },
        {
            "id": "segments",
            "title": "分段清单",
            "kind": "collection",
            "needs": ["full_script"],
            "prompt": {
                "role": "分段导演",
                "task": "把 {{full_script.output}} 按每段 {{inputs.segmentSeconds}} 秒拆成若干段。",
                "output": "每段包含 segment_text、duration_seconds、visual_notes。",
                "check": "覆盖总时长；segment_text 是完整本段剧情。",
            },
            "output_schema": {
                "type": "collection",
                "items_key": "items",
                "fields": [
                    {"id": "segment_text", "type": "string", "required": True},
                    {"id": "duration_seconds", "type": "number", "required": True},
                    {"id": "visual_notes", "type": "string"},
                ],
            },
        },
        {
            "id": "segment_loop",
            "title": "逐段生成",
            "kind": "repeat",
            "needs": ["segments"],
            "foreach": {"dimension": "segments"},
            "item_name": "segment",
            "steps": [
                {
                    "id": "segment_script",
                    "title": "本段剧情正文",
                    "kind": "text",
                    "visible": True,
                    "prompt_template": "只输出本段剧情正文：{{segment.segment_text}}",
                    "output": {"canvas": True, "key": "segment_script"},
                },
                {
                    "id": "segment_video",
                    "title": "本段视频",
                    "kind": "video",
                    "needs": ["segment_script"],
                    "prompt": {
                        "role": "AI 视频提示词导演",
                        "task": "根据 {{segment.segment_text}}、{{segment.visual_notes}} 和 {{inputs.style}} 写视频提示词。",
                        "output": "只输出视频提示词正文。",
                        "check": "包含主体、场景、动作、镜头、光线、运动；不输出 JSON。",
                    },
                    "fields": {"duration_seconds": "{{segment.duration_seconds}}", "width": 1280, "height": 720},
                },
            ],
        },
    ],
}

AUTHORING_SPEC_EXAMPLE_JSON = json.dumps(
    AUTHORING_SPEC_EXAMPLE,
    ensure_ascii=False,
    separators=(",", ":"),
)


AUTHORING_SPEC_GUIDE = """\
## Authoring Spec

- Root: schema='openreel.workflow.authoring.v1', id/name/description/inputs/defaults/dimensions/steps.
- inputs are UI fields; use {{inputs.id}} in prompts, not one run's values.
- Step ids are stable. Dependencies use needs/depends_on; prompt refs use {{step_id.output}}.
- If a prompt relies on another generated product, include its step or repeat group in needs.
- Processing kinds: text/plan/collection/plugin. Generated visible text is kind text with visible:true or output.canvas:true.
- canvas_text copies existing text only.
- Media kinds image/video/audio are visible products. Put the prompt on the media step; the compiler creates the hidden prompt step.
- Lists use kind collection with output_schema.items_key and fields; runtime injects JSON format.
- Loops use dimensions from collection output, then foreach.dimension. Example: dimensions.segments.source='steps.segments.output.items', foreach.dimension='segments', item_name='segment'.
- Inspect collection-driven loops with sample context, e.g. context.segments.output.items; normal inputs only fill UI fields.
- To use products from another loop, put the repeat group id in needs and describe the reference in the prompt. If fields.references is needed, use character_loop.character_image, not bare character_image.
- Media settings go in fields/settings, e.g. width/height/duration_seconds. Use literal numbers or real input refs; dimensions are repeat axes, not resolution.
- Prompts use role/task/output/check, or one prompt_template.

Canonical example:
```json
""" + AUTHORING_SPEC_EXAMPLE_JSON + """
```

Self-check before writing:
- Every dependency id exists and points upstream or to a sibling in the same repeat group.
- Collection schemas contain every field later read by loop children.
- Media steps carry their own prompt; no hand-written media_prompt sibling for the same product.
- workflow.canvas.inspect should show expected repeat groups, canvas nodes, edges, and final image/video/audio outputs.
"""
