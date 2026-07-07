# Workflow Spec Protocol

OpenReel workflow spec describes a reusable creation workflow. The preferred
authoring format is `openreel.workflow.authoring.v1`; the backend compiles it to
the runtime workflow format used by canvas execution.

## Authoring Schema

```json
{
  "schema": "openreel.workflow.authoring.v1",
  "id": "grid_storyboard_workflow",
  "title": "宫格分镜流程",
  "inputs": {
    "plot": { "type": "long_text", "label": "剧情", "required": true },
    "segmentCount": { "type": "number", "label": "段数", "default": 1 }
  },
  "steps": [
    {
      "id": "script",
      "title": "剧本",
      "kind": "text",
      "prompt": {
        "role": "短剧编剧",
        "task": "根据用户输入剧情写分段剧本。",
        "output": "输出可继续拆分人物、场景和分镜的剧本正文。",
        "check": "每段包含人物、场景、动作和情绪变化。"
      },
      "output": { "canvas": true, "key": "script" }
    }
  ]
}
```

## Step Fields

- `id`: stable step id. Use ASCII identifiers for reusable specs.
- `title`: user-facing step title.
- `kind`: `input`, `text`, `plan`, `collection`, `plugin`, `loop`,
  `canvas_text`, `image`, `video`, or `audio`.
- `needs`: upstream step ids.
- `for_each`: repeated execution source, such as `production_plan.output.segments`.
- `item_name`: local name for the current repeated item.
- `references`: dynamic visual reference selectors.
- `prompt`: structured single-call LLM prompt skeleton.
- `output`: output contract. `canvas: true` creates a visible canvas product;
  `canvas: false` keeps the result in workflow runtime only.
- `phase` / `group`: optional UI grouping labels.
- `ui`: optional user-facing display hints.
- `fields`: optional node fields for generated canvas nodes.

## Prompt Sections

`prompt` may use these keys:

- `role` or `system`: what this step acts as.
- `task` or `instruction`: how to transform inputs into output.
- `output`: expected output shape or content.
- `check`: self-check criteria for this step.

The compiler turns these into stable `SYSTEM`, `USER`, `OUTPUT`, and `CHECK`
sections. The model writes workflow-level structure once; node execution reuses
the prompt template for each step.

## Canvas And Runtime Outputs

Visible canvas products use:

```json
{ "output": { "canvas": true, "key": "storyboards" } }
```

Workflow-only intermediate steps use:

```json
{ "output": { "canvas": false, "key": "scene_plan" } }
```

Runtime API payloads expose this as:

- `canvas_output: true`: the step has a visible canvas product.
- `runtime_only: true`: the step is kept in the top workflow runtime.

Frontend canvas filtering uses these explicit fields first, then falls back to
legacy `surface` and `visibility`.

## Dynamic Expansion

Reusable specs describe repeated structure once. The backend expands concrete
steps from inputs or upstream runtime output.

Examples:

- User-provided count: `for_each: inputs.segmentCount`.
- Planned list: `for_each: production_plan.output.segments`.
- Per-character assets: `for_each: character_plan.output.main_characters`.

Common authoring aliases are accepted before compilation:

- `type: list` means `kind: collection`.
- `type: repeat` means `kind: loop`.
- `repeat: { "items": "{{steps.segments.output}}", "item_name": "segment" }`
  means a repeated group over `steps.segments.output`, and child prompts may use
  `{{segment.field_name}}`.
- `prompt_template` is accepted as a shortcut for a string `prompt`.

Repeated steps should keep the same template id through `template_step_id` after
compilation, while runtime instances get concrete step ids.

## References

Visual references are selected from upstream outputs without drawing every
dependency line on the canvas.

```json
{
  "references": {
    "characters": {
      "source": "frame_plan.output.appearing_characters",
      "candidates": "character_reference"
    }
  }
}
```

The backend resolves matching candidates at execution time and writes concrete
node references to the visible product node.

## Compatibility

Older runtime specs with `node_type`, `depends_on`, `prompt_template`,
`surface`, and `visibility` remain accepted. New specs should prefer the
authoring schema and let the compiler derive runtime fields.

Extensions can be stored in `extensions`, `extension_config`, `x`, or
`x-openreel`. Unknown extension fields are preserved by normalization and
ignored by the core runner unless a capability declares support.
