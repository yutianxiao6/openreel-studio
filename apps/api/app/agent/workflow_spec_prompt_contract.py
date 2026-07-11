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
    "id": "dynamic_reference_video",
    "required_capabilities": ["core.vision_context"],
    "dimensions": {
        "assets": {"source": "steps.assets.output.items", "scope_key": "asset"},
        "shots": {"source": "steps.shots.output.items", "scope_key": "shot"},
    },
    "steps": [
        {
            "id": "assets",
            "kind": "collection",
            "prompt_template": "输出素材及稳定 asset_id。",
            "output_schema": {
                "type": "collection",
                "items_key": "items",
                "fields": [{"id": "asset_id", "required": True}],
            },
        },
        {
            "id": "asset_loop",
            "kind": "repeat",
            "needs": ["assets"],
            "foreach": {"dimension": "assets"},
            "item_name": "asset",
            "steps": [{"id": "asset_image", "kind": "image", "prompt_template": "生成 {{asset.asset_id}}。"}],
        },
        {
            "id": "shots",
            "kind": "collection",
            "needs": ["assets"],
            "prompt_template": "输出镜头；asset_ids 只取自 {{assets.output}}。",
            "output_schema": {
                "type": "collection",
                "items_key": "items",
                "fields": [
                    {"id": "prompt", "required": True},
                    {"id": "asset_ids", "type": "array", "items": {"type": "string"}, "required": True},
                    {"id": "duration_seconds", "type": "number", "required": True},
                ],
            },
        },
        {
            "id": "shot_loop",
            "kind": "repeat",
            "needs": ["shots", "asset_loop"],
            "foreach": {"dimension": "shots"},
            "item_name": "shot",
            "steps": [
                {
                    "id": "asset_selector",
                    "kind": "plan",
                    "prompt_template": "把 {{shot.asset_ids}} 原样输出为 selected_ids。",
                    "output_schema": {
                        "type": "object",
                        "fields": [{"id": "selected_ids", "type": "array", "items": {"type": "string"}, "required": True}],
                    },
                },
                {
                    "id": "storyboard",
                    "kind": "image",
                    "prompt_template": "根据 {{shot.prompt}} 生成分镜图。",
                },
                {
                    "id": "video",
                    "kind": "video",
                    "needs": ["storyboard", "asset_selector", "asset_loop"],
                    "context_refs": [
                        {"ref": "storyboard", "role": "vision_context"},
                        {"ref": "storyboard", "role": "visual_reference"},
                    ],
                    "references": [
                        {
                            "from_group": "asset_loop",
                            "source_step": "asset_selector",
                            "source_path": "output.selected_ids",
                            "match_fields": ["asset_id"],
                            "role": "vision_context",
                        },
                        {
                            "from_group": "asset_loop",
                            "source_step": "asset_selector",
                            "source_path": "output.selected_ids",
                            "match_fields": ["asset_id"],
                            "role": "visual_reference",
                        },
                    ],
                    "fields": {"duration_seconds": "{{shot.duration_seconds}}"},
                    "prompt_template": "看分镜图和选中的素材图，按 {{shot.prompt}} 写视频提示词。",
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
## Authoring Spec: exact rules

- Schema is `openreel.workflow.authoring.v1`. Declare every `{{inputs.id}}` in root `inputs`; never omit requested/referenced inputs. `needs` ids are upstream steps/groups or earlier siblings.
- Collections declare every later-read field in `output_schema` (`type:"collection"`, `items_key:"items"`, `fields`). Dimensions source `steps.<collection>.output.items`; repeats set `foreach.dimension`/`item_name`.
- Media carries its own prompt; never author hidden `*_prompt`. Media options exist only in `fields` and use `duration_seconds`, never `duration`, `settings`, or top-level keys.

## Image rules

- `vision_context` gives pixels to the prompt LLM and requires root `required_capabilities:["core.vision_context"]`; `visual_reference` serves only the media generator.
- Fixed images use `context_refs` with `ref` and belong in `needs`.
- Dynamic images use `references`, never `context_refs`. All keys are required: `from_group`, `source_step`, `source_path`, `match_fields`, `role`. The group is the candidate image loop; source is an earlier current-loop selector, never its media child; path is normally `output.selected_ids`; match fields are non-empty strings such as `["asset_id"]`. Put selector and group in `needs`.
- If ids only exist on the loop item, add a plan/text step that copies them to declared `selected_ids`. If LLM and generator both need a source, author both roles; compilation separates them.

Invalid: undeclared input; selector in `context_refs`; path missing `output.`; object `match_fields`; missing dependency/capability; options outside `fields`; manual prompt sibling.

## Canonical fixed + dynamic pattern
```json
""" + AUTHORING_SPEC_EXAMPLE_JSON + """
```

Before apply_patch verify inputs, ids/dependencies, schema fields, dimensions, selectors, roles, and media fields. Ready only when inspect expands samples without issues.
"""
