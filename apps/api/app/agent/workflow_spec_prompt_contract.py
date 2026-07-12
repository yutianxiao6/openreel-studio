"""Compact model-facing guide for authoring Workflow Spec v2 documents."""
from __future__ import annotations

import json

from app.agent.workflow_spec import WORKFLOW_SPEC_VERSION


WORKFLOW_SPEC_V2_EXAMPLE = {
    "schema": WORKFLOW_SPEC_VERSION,
    "id": "reference_video",
    "title": "参考图视频",
    "description": "从剧情生成角色图、分镜和视频。",
    "inputs": {
        "plot": {"type": "long_text", "label": "剧情", "required": True},
        "duration_seconds": {"type": "integer", "label": "时长", "default": 15, "min": 1},
    },
    "steps": [
        {
            "id": "plan",
            "title": "制作规划",
            "kind": "object",
            "prompt": {
                "role": "影视制片规划师",
                "task": "根据 {{ inputs.plot }} 输出人物与镜头。",
                "output": "输出 characters 和 shots；两者使用稳定 id。",
                "check": "镜头总时长等于 {{ inputs.duration_seconds }}。",
            },
            "output": {
                "schema": {
                    "fields": [
                        {
                            "id": "characters",
                            "type": "array",
                            "required": True,
                            "fields": [
                                {"id": "character_id", "type": "string", "required": True},
                                {"id": "prompt", "type": "string", "required": True},
                            ],
                        },
                        {
                            "id": "shots",
                            "type": "array",
                            "required": True,
                            "fields": [
                                {"id": "shot_id", "type": "string", "required": True},
                                {"id": "character_ids", "type": "array", "required": True},
                                {"id": "prompt", "type": "string", "required": True},
                            ],
                        },
                    ]
                }
            },
        },
        {
            "id": "characters",
            "title": "人物参考图",
            "kind": "loop",
            "foreach": {"items": "steps.plan.output.characters[]", "as": "character", "key": "character_id"},
            "steps": [
                {
                    "id": "character_image",
                    "title": "人物图",
                    "kind": "image",
                    "prompt": {"task": "{{ character.prompt }}"},
                }
            ],
        },
        {
            "id": "shots",
            "title": "逐镜头制作",
            "kind": "loop",
            "foreach": {"items": "steps.plan.output.shots[]", "as": "shot", "key": "shot_id"},
            "steps": [
                {
                    "id": "shot_context",
                    "title": "镜头人物选择",
                    "kind": "object",
                    "prompt": {
                        "task": "根据当前镜头 {{ shot }} 输出本镜头实际使用的 selected_character_ids。",
                        "output": "只输出 selected_character_ids。",
                    },
                    "output": {
                        "schema": {
                            "fields": [
                                {"id": "selected_character_ids", "type": "array", "required": True},
                            ]
                        }
                    },
                },
                {
                    "id": "storyboard",
                    "title": "分镜图",
                    "kind": "image",
                    "needs": ["shot_context"],
                    "prompt": {"task": "{{ shot.prompt }}"},
                    "uses": [
                        {
                            "from": "character_image",
                            "as": ["vision", "reference"],
                            "select": {
                                "values": "steps.shot_context.output.selected_character_ids",
                                "by": ["character_id"],
                            },
                        }
                    ],
                },
                {
                    "id": "video",
                    "title": "视频",
                    "kind": "video",
                    "needs": ["storyboard"],
                    "prompt": {
                        "task": "看分镜图和选中的人物图，根据 {{ shot.prompt }} 写视频提示词。",
                        "output": "只输出视频提示词正文。",
                    },
                    "uses": [
                        {"from": "storyboard", "as": ["vision", "reference"]},
                        {
                            "from": "character_image",
                            "as": ["vision", "reference"],
                            "select": {
                                "values": "steps.shot_context.output.selected_character_ids",
                                "by": ["character_id"],
                            },
                        },
                    ],
                    "fields": {"duration_seconds": "{{ inputs.duration_seconds }}"},
                },
            ],
        },
    ],
}

_EXAMPLE_JSON = json.dumps(WORKFLOW_SPEC_V2_EXAMPLE, ensure_ascii=False, separators=(",", ":"))

WORKFLOW_SPEC_V2_GUIDE = """\
## Workflow Spec v2

- Root: `schema,id,title,inputs,steps`; optional `description,tags,ui,extensions`; schema `openreel.workflow.v2`.
- Input types are exactly `text|long_text|number|integer|boolean|enum|image|video|audio|json`; each input has `type,label`. Use `text`, never `string`, for short text. Enum options are `{value,label}`. Declare every referenced input.
- Step kinds are `text|object|collection|image|video|audio|loop|plugin`; ids are globally unique.
- `text|object|collection` require `prompt`; object/collection require `output.schema.fields`. A collection schema describes one item. Field types are `string|number|integer|boolean|object|array`; nested fields belong only to object/array. Declare every field later read. Visible text sets `output.canvas:true`.
- Media is one logical step with its own `prompt`; Do not create prompt sibling steps. Put media settings in `fields`.
- Dependencies come from `needs` and referenced `inputs.<id>`/`steps.<id>.output...` paths, loop sources, plugin inputs, and `uses`.
- Loop: exactly one `foreach.items|count`, plus `as` and nested `steps`; item paths end `[]`. Nested loops may use `episode.segments[]`. Use stable `key` for object items.
- `uses` is the only reference contract: `vision` gives pixels to the prompt LLM; `reference` gives assets to media generation; `source` adopts media alone.
- Dynamic references add `select.values` and `select.by` to `{from,as}`. Values use a scoped path such as `steps.frame_plan.output.selected_character_ids`; `by` is a stable candidate field such as `character_id`. For `shot.character_ids`, first emit it from an object/collection step inside that loop, then select through that step output; never bind by array order.
- `when` is `{path,op,value}` and path is one root input. `empty|not_empty` omit value; other ops require it. Fields: `execution=auto|manual`, `on_error=stop|continue`.
- Direct media adoption has exactly one `source` use, no prompt, and no other use.
- Reusable specs contain no provider/model/tier, runner/node_type/surface/visibility, runtime state, generated content, or hidden prompt phases. Plugin ids are namespaced.

Frequent errors: input `string`; undeclared fields; duplicate ids; invalid loops; prompt siblings; wrong media roles; unscoped selection; provider/model routing.

Canonical example:
```json
""" + _EXAMPLE_JSON + """
```

Before save, check paths, ids, dependencies, schemas, loops, roles, conditions, and visible outputs; then inspect canvas projection.
"""
