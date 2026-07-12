# Workflow Spec protocol

English · [简体中文](./zh-CN/workflow-spec-protocol.md) · [Documentation home](./README.en.md)

OpenReel workflow specs describe reusable creation processes. The preferred authoring schema is `openreel.workflow.authoring.v1`; the backend compiles it to the `openreel.workflow.v1` runtime format used by canvas execution.

## Minimal authoring spec

```json
{
  "schema": "openreel.workflow.authoring.v1",
  "id": "storyboard_workflow",
  "title": "Storyboard workflow",
  "inputs": {
    "plot": { "type": "long_text", "label": "Plot", "required": true }
  },
  "steps": [
    {
      "id": "script",
      "title": "Script",
      "kind": "text",
      "prompt": {
        "role": "Screenwriter",
        "task": "Turn the input plot into a segment script.",
        "output": "A readable script with characters, scenes, and actions.",
        "check": "Every segment has a clear visual change."
      },
      "output": { "canvas": true, "key": "script" }
    }
  ]
}
```

Users normally build this structure through Workflow Build Mode rather than writing JSON by hand.

## Top-level fields

| Field | Purpose |
| --- | --- |
| `schema` | Protocol version. |
| `id` | Stable ASCII workflow ID. |
| `title` | User-facing title. |
| `inputs` | Values required before execution. |
| `steps` | Reusable process steps. |
| `required_capabilities` | Engine capabilities required by the workflow. |
| `required_extensions` | Extensions that must be installed before import or execution. |
| `extensions` | Optional namespaced extension metadata. |

## Step fields

- `id`: stable step ID.
- `title`: user-facing label.
- `kind`: input, text, collection, loop, plugin, image, video, audio, or another supported authoring kind.
- `needs`: true execution dependencies.
- `for_each`: a repeated execution source.
- `item_name`: local name for the current repeated item.
- `references`: dynamic selectors for visual or contextual references.
- `prompt`: structured single-step prompt.
- `output`: output key and whether it creates a visible canvas node.
- `fields`: node fields written to a canvas deliverable.
- `phase`, `group`, and `ui`: optional presentation metadata.
- `extension_config`: namespaced step extension configuration.

## Prompt sections

`prompt` accepts role/system, task/instruction, output, and check sections. The compiler produces stable prompt sections while each runtime instance receives only the inputs and upstream output needed by that step.

## Canvas and runtime output

Visible deliverable:

```json
{ "output": { "canvas": true, "key": "storyboards" } }
```

Runtime-only intermediate:

```json
{ "output": { "canvas": false, "key": "scene_plan" } }
```

The runtime exposes equivalent `canvas_output` and `runtime_only` metadata. Explicit output metadata takes precedence over legacy surface or visibility fields.

## Dynamic expansion

Repeated structure is declared once and expanded from inputs or upstream output:

```json
{
  "id": "scene_image",
  "kind": "image",
  "for_each": "scene_plan.output.scenes",
  "item_name": "scene"
}
```

Accepted compatibility aliases include collection/list forms, repeat groups, and string `prompt_template`. Compiled runtime instances retain a stable `template_step_id` and receive concrete instance IDs.

Every repeat group must define a source through `for_each`, `repeat.count`, or another supported cardinality expression. Missing cardinality fails validation before execution.

## References

Selectors choose references from upstream collections without hard-coding every runtime edge:

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

The runner resolves concrete candidates and writes them to the visible node's `fields.references`.

## Extensions and compatibility

The core protocol remains stable. Namespaced capabilities and extensions declare optional behavior. Unknown optional extension metadata is preserved; an unknown required capability or extension blocks import or execution.

Legacy runtime specs using `node_type`, `depends_on`, `prompt_template`, `surface`, or `visibility` remain accepted. New reusable workflows should use the authoring schema and let the compiler derive runtime fields.
