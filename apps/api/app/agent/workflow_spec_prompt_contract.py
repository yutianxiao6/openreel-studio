"""Compact model-facing guide for authoring Workflow Spec v2 documents."""
from __future__ import annotations

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

WORKFLOW_SPEC_V2_GUIDE = """\
## Workflow Spec v2

- Root must be one strict JSON object with `schema,id,title,inputs,steps`; optional keys are only `description,tags,ui,extensions`. Use schema `openreel.workflow.v2`. `inputs` is an object map keyed by input id, never an array; `steps` is an array; every step id is globally unique.
- End-to-end identifiers must satisfy the stricter projection runtime, not merely the public parser: use lowercase snake case `^[a-z][a-z0-9_]+$` for workflow/input/step ids. Authored ids are at most 32 characters and loop ids at most 10. Generated item keys used by `foreach.key` are compact lowercase snake ids at most 12 characters. A projected nested id concatenates every ancestor loop id, item key, bounded attempt (`i1`), child id, and sometimes `__prompt`; the complete result must be at most 101 characters. Budget this before writing and use short loop ids such as `episodes`, `segments`, `quality`, never verbose `*_loop` chains.
- Input types are exactly `text|long_text|number|integer|boolean|enum|image|video|audio|json`. Every input requires `type,label`; allowed optional keys are `description,required,default,min,max,options`. Use `text`, never `string`, for short text. Enum options are `{value,label}` and are allowed only for enum. Declare every referenced input.
- Step kinds are exactly `text|object|collection|image|video|audio|loop|plugin`. Common optional fields are `description,needs,prompt,output,fields,uses,when,execution,on_error,ui`. `execution` is only the JSON string `"auto"` or `"manual"`; `on_error` is only the JSON string `"stop"` or `"continue"`. They are never objects. Omit `execution` for normal automatic and bounded-loop steps because `"auto"` is the default. Bounded attempts are already serialized by `foreach.until`; never add `ordered`, `sequential`, `concurrency`, `parallel`, or custom retry fields anywhere.
- A prompt is `{task}` plus optional `role,output,check`; `task` must be non-empty. `text|object|collection` require a prompt. `object|collection` also require `output.schema.fields`; `text` must not declare a structured schema.
- Output field types are exactly `string|number|integer|boolean|object|array`. Nested `fields` are allowed only on `object` or `array`. For an array of objects, its `fields` describe one item; for an array of primitive ids, omit `fields` and do not invent `items`. Declare every field later read. Example: `{"id":"episodes","type":"array","fields":[{"id":"episode_id","type":"string","required":true},{"id":"segments","type":"array","fields":[{"id":"segment_id","type":"string","required":true}]}]}`.
- Canvas visibility is not an intermediate-media hiding mechanism. Current runtime always projects every `image|video|audio` step to canvas, whether `output.canvas` is omitted, true, or false. For `text|object|collection`, the default is hidden and `output.canvas:true` makes it visible. Do not claim an intermediate media step is hidden.
- Media is one logical `image|video|audio` step with its own prompt. Do not create prompt sibling steps. A generated media step requires `prompt`. Direct media adoption instead uses exactly one `source` use, has no prompt, and has no other use.
- Dependencies come from `needs` and referenced `inputs.<id>`/`steps.<id>.output...` paths, loop sources, plugin inputs, and `uses`. `needs` contains logical step ids, not projected instance ids. Do not create a reverse edge to model retries.
- An item loop has exactly `foreach:{"items":"<path>[]","as":"<item_var>","key":"<stable_field>"}` and nested `steps`. `key` is the current object item's stable field name, not a template or full path. Example outer loop: `{"items":"steps.plan.output.episodes[]","as":"episode","key":"episode_id"}`; nested loop: `{"items":"episode.segments[]","as":"segment","key":"segment_id"}`. In prompts read loop data as `{{ episode.title }}` and `{{ segment.script }}`.
- A count loop has exactly `foreach:{"count":<positive integer or input path>,"as":"<item_var>"}`. Only bounded feedback loops require a literal integer count from 1 through 10.
- Nested scope binds the same shared parent item first, then the current repeat index. A bounded-loop downstream use of an inner child resolves the latest completed attempt in that parent scope. For a different collection, use `uses.select`; never bind assets by array position.
- `uses` is the only asset-reference contract. `vision` supplies pixels to a prompt LLM, `reference` supplies assets to media generation, and `source` adopts media without generation. One use may contain `as:["vision","reference"]`; `source` must be the only role and the only use.
- Dynamic selection has the exact shape `{"from":"character_image","as":["reference"],"select":{"values":"steps.selection.output.selected_character_ids","by":["character_id"]}}`. `select.values` is one scoped string path to a declared object/collection output. `select.by` is always a non-empty JSON array of unique stable candidate field names, even when it contains only `"character_id"`; it is never a string. First emit a loop-local selection such as `selected_character_ids`, then select the unrelated character collection through that output.
- A bounded feedback loop uses literal `foreach.count` 1..10 and `foreach.until={path,op,value}`. The path is `steps.<terminal_child>.output[.<declared_field>]`. Ordered comparisons use a finite number. The gate source is a direct child that runs every attempt, has `on_error:"stop"`, no `when`, is not a loop, and is terminal: no sibling may depend on it.
- Inside a feedback loop, wire producer -> review only: the review depends on the producer; never add review -> producer or make another sibling depend on the review. Image review receives pixels with `uses:[{"from":"candidate","as":["vision"]}]`, is `kind:"object"`, and declares the gate plus every feedback field in its output schema.
- Put the exact token `{{ previous }}` exactly once in each revising producer's prompt and nowhere else in that loop. It renders `{}` on attempt 1 and the complete prior review later. Matching `until` selects the last passing attempt; a non-match expands one next attempt; exhaustion stops downstream with `workflow_loop_until_exhausted`; invalid gate output is an error.
- A downstream step depends on the loop id, never an attempt child. To consume the accepted media, set `needs:["quality_loop"]` and use the logical inner producer id, for example `uses:[{"from":"candidate","as":["vision","reference"]}]`; scope resolution selects the accepted/latest completed attempt from the same parent item.
- `when` is exactly `{path,op,value}` and `path` references one root input such as `inputs.include_teaser`. Operators are `eq|ne|lt|lte|gt|gte|empty|not_empty`; `empty|not_empty` omit `value`, all others require it.
- Specs omit provider/model and all runtime media settings including aspect ratio, resolution, width/height, quality, fps. Frontend supplies media settings. Plugin ids and root extension keys are namespaced.

Frequent fatal errors: uppercase/hyphen/overlong ids; projected nested id over 101 characters; `execution` as an object; invented `ordered|sequential|concurrency|parallel|retry` fields; `string` input type; undeclared output fields; duplicate ids; `select.by` as a string; object arrays without nested fields; primitive arrays with invented `items`; prompt sibling steps; reverse retry dependencies; downstream depending on an attempt child; wrong media roles; unscoped cross-collection selection; provider/model routing.

Canonical bounded review pattern:
```json
{"id":"quality_loop","title":"质量确认","kind":"loop","foreach":{"count":3,"as":"attempt","until":{"path":"steps.quality_review.output.score","op":"gte","value":80}},"steps":[{"id":"candidate","title":"候选产物","kind":"image","prompt":{"task":"生成候选；上一轮完整审核：{{ previous }}"}},{"id":"quality_review","title":"质量审核","kind":"object","needs":["candidate"],"uses":[{"from":"candidate","as":["vision"]}],"prompt":{"task":"查看候选图片并输出评分、问题原因和可执行修改要求。"},"output":{"schema":{"fields":[{"id":"score","type":"integer","required":true},{"id":"reason","type":"string","required":true},{"id":"issues","type":"array","required":true},{"id":"regeneration_instruction","type":"string","required":true}]}}}]}
```
Canonical downstream consumer: `{"id":"final_video","title":"最终视频","kind":"video","needs":["quality_loop"],"uses":[{"from":"candidate","as":["vision","reference"]}],"prompt":{"task":"根据审核通过的候选生成最终视频。"}}`.

Before the first write, self-check root/input keys, id syntax and projected-id length budget, declared paths, output schemas, loop sources/keys, bounded-loop terminal review, `{{ previous }}` count, roles, `select.by` arrays, conditions, and actual media visibility. Submit a complete valid spec instead of relying on repair. Then inspect with representative inputs and compact-key context for every upstream collection output. Projection is a structural authoring preview, not proof of runtime gate outcomes: if an expected dynamic loop has zero expanded instances, provide sample context and inspect again; runtime is authoritative for `workflow_loop_until_exhausted`.
"""
