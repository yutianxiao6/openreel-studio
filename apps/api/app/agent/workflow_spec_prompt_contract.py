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
                    "id": "storyboard",
                    "title": "分镜图",
                    "kind": "image",
                    "prompt": {"task": "{{ shot.prompt }}"},
                    "uses": [
                        {
                            "from": "character_image",
                            "as": ["vision", "reference"],
                            "select": {
                                "values": "steps.plan.output.shots.character_ids",
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
                                "values": "steps.plan.output.shots.character_ids",
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

- Root uses exactly `schema`, `id`, `title`, optional description/tags, `inputs`, `steps`, optional `ui/extensions`. Schema is `openreel.workflow.v2`.
- Inputs are keyed objects. Every input has `type` and `label`; referenced inputs must be declared. Use snake_case ids.
- Step kinds are `text`, `object`, `collection`, `image`, `video`, `audio`, `loop`, `plugin`. Steps use globally unique ids.
- `text/object/collection` carry `prompt`; object and collection also carry `output.schema`. A visible text uses `output.canvas:true`.
- Media is one logical step and carries its own `prompt`. Do not create prompt sibling steps. Media settings belong in `fields`.
- Dependencies are explicit `needs` plus paths found in prompts, conditions, loop sources, plugin inputs, and `uses`. Paths use `inputs.<id>` or `steps.<id>.output...`.
- A loop uses exactly one `foreach.items` or `foreach.count`, plus `as`; nested author steps live in `steps`. Item paths normally end in `[]`.
- `uses` is the only reference contract. `vision` sends image pixels to the prompt LLM; `reference` sends the asset to media generation; `source` adopts one existing media output and cannot combine with other roles.
- Dynamic references add `select.values` and non-empty `select.by`. `from` names the candidate media step; values identify the current selection; by names stable identity fields.
- `when` is structured `{path,op,value}`. `execution` is `auto|manual`; `on_error` is `stop|continue`.
- Reusable specs never contain provider, model, tier, runner, node_type, surface, visibility, runtime ids/status, generated story text, or hidden prompt phases.
- Plugin ids are namespaced. The engine rejects unavailable required plugins before save or run.

Frequent errors: undeclared input/path; duplicate or unknown step id; output field read but absent from schema; loop without one source; media prompt split into another step; confusing `vision` with `reference`; dynamic selection without stable ids; provider/model routing inside a reusable spec.

Canonical example:
```json
""" + _EXAMPLE_JSON + """
```

Before saving, verify schema, input paths, ids, dependencies, output fields, loops, media roles, dynamic identity fields, conditions, and visible outputs. Then inspect the canvas projection.
"""
