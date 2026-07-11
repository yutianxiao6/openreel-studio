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

AUTHORING_SPEC_PROMPT_EXAMPLE = {
    "schema": "openreel.workflow.authoring.v1",
    "id": "storyboard_video",
    "name": "分镜视频",
    "required_capabilities": ["core.vision_context"],
    "inputs": [{"id": "plot", "type": "long_text", "required": True}],
    "dimensions": {"segments": {"source": "steps.segments.output.items", "scope_key": "segment"}},
    "steps": [
        {"id": "full_script", "kind": "text", "visible": True, "prompt_template": "根据 {{inputs.plot}} 写剧本。"},
        {
            "id": "segments",
            "kind": "collection",
            "needs": ["full_script"],
            "prompt_template": "拆分 {{full_script.output}}。",
            "output_schema": {"type": "collection", "items_key": "items", "fields": [{"id": "segment_text", "required": True}]},
        },
        {
            "id": "segment_loop",
            "kind": "repeat",
            "foreach": {"dimension": "segments"},
            "item_name": "segment",
            "steps": [
                {"id": "storyboard", "kind": "image", "prompt_template": "为 {{segment.segment_text}} 生成分镜图。"},
                {
                    "id": "video",
                    "kind": "video",
                    "needs": ["storyboard"],
                    "context_refs": [
                        {"ref": "storyboard", "role": "vision_context"},
                        {"ref": "storyboard", "role": "visual_reference"},
                    ],
                    "fields": {"duration_seconds": 5},
                    "prompt_template": "看分镜图，为 {{segment.segment_text}} 写视频提示词。",
                },
            ],
        },
    ],
}

AUTHORING_SPEC_EXAMPLE_JSON = json.dumps(
    AUTHORING_SPEC_PROMPT_EXAMPLE,
    ensure_ascii=False,
    separators=(",", ":"),
)


AUTHORING_SPEC_GUIDE = """\
## Authoring Spec

- Root schema='openreel.workflow.authoring.v1'; fields: id/name/inputs/defaults/dimensions/steps.
- inputs are UI fields; use {{inputs.id}} in prompts, not one run's values.
- Stable step ids; needs/depends_on list upstream products; prompt refs use {{step_id.output}}.
- Processing kinds: text/plan/collection/plugin. Visible generated text uses text plus visible:true or output.canvas:true; canvas_text only copies text.
- Media kinds image/video/audio are visible. Put the prompt there; the compiler creates its hidden prompt step.
- Collections define output_schema.items_key/fields. Loops use a dimension sourced from `steps.collection.output.items`, then foreach.dimension/item_name.
- Inspect dynamic loops with sample `context.collection.output.items`; UI values belong in inputs.
- Cross-loop use requires the repeat group in needs. Qualified fixed refs use group.child, not bare child.
- Core media settings go in `fields:{"width":1920,"height":1080,"duration_seconds":5}` (not top-level and not duration). Use numbers or input refs; dimensions are repeat axes.
- Prompts use role/task/output/check, or one prompt_template.

Image-use roles:
- `vision_context` means a text/LLM prompt must inspect image pixels; root `required_capabilities` must contain `core.vision_context`.
- Fixed image refs use only `context_refs:[{"ref":"storyboard","role":"vision_context"}]` plus needs.
- Dynamic selectors always go in `references`, never `context_refs`: `from_group` is the candidate image repeat group; `source_step` is the current repeat's upstream selector/planner; `source_path` is normally `output.selected_ids`; `match_fields` is a string list such as `["product_id","reuse_key"]`. Put both in needs.
- The candidate media child is not `source_step`. If selected ids exist only on the loop item, add a current-loop plan/text step that outputs them, then select from that step.
- `visual_reference` is only for image/video generation and does not send pixels to the prompt-writing LLM.
- If one media step must first look at an image and then generate from it, author both roles on that media step. The compiler routes `vision_context` to its hidden prompt step and keeps `visual_reference` on the visible media product.

Canonical example:
```json
""" + AUTHORING_SPEC_EXAMPLE_JSON + """
```

Self-check before writing:
- Every dependency id exists and points upstream or to a sibling in the same repeat group.
- Collection schemas contain every field later read by loop children.
- Media steps carry their own prompt; no hand-written media_prompt sibling for the same product.
- Every vision source and selector candidate is upstream; root capabilities include `core.vision_context`.
- workflow.canvas.inspect should show expected repeat groups, canvas nodes, edges, and final image/video/audio outputs.
"""
