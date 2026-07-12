# Workflow Spec Protocol

OpenReel accepts one portable workflow format: `openreel.workflow.v2`. Import,
export, template storage, and Workflow Build Mode all read and write this public
document. Compiled execution phases and project runtime state are private and
never belong in a reusable spec.

## Public document

```json
{
  "schema": "openreel.workflow.v2",
  "id": "storyboard_video",
  "title": "Storyboard video",
  "description": "Create a storyboard and a video from a plot.",
  "tags": ["video"],
  "inputs": {
    "plot": {
      "type": "long_text",
      "label": "Plot",
      "required": true
    }
  },
  "steps": [
    {
      "id": "storyboard",
      "title": "Storyboard",
      "kind": "image",
      "prompt": {
        "role": "Storyboard director",
        "task": "Design a storyboard for {{ inputs.plot }}.",
        "check": "Keep characters and screen direction consistent."
      }
    },
    {
      "id": "final_video",
      "title": "Final video",
      "kind": "video",
      "needs": ["storyboard"],
      "prompt": {
        "task": "Write the final video prompt from {{ steps.storyboard.output }}."
      },
      "uses": [
        {"from": "storyboard", "as": ["vision", "reference"]}
      ]
    }
  ]
}
```

Root fields are `schema`, `id`, `title`, `description`, `tags`, `inputs`,
`steps`, `ui`, and namespaced `extensions`. Input definitions are keyed by id
and may contain `type`, `label`, `description`, `required`, `default`, `min`,
`max`, and `options`.

Step kinds are `text`, `object`, `collection`, `image`, `video`, `audio`,
`loop`, and `plugin`. A step may contain `id`, `title`, `kind`, `description`,
`needs`, `prompt`, `output`, `fields`, `uses`, `when`, `execution`, `on_error`,
`foreach`, nested `steps`, `plugin`, and `ui`. Unknown fields are rejected.

## Data and execution

Data paths use `inputs.<id>`, `steps.<id>.output`, and loop variables. `needs`
adds ordering when no data path already expresses the dependency. Conditions
are positive and structured, for example:

```json
{"when": {"path": "inputs.episode_count", "op": "gt", "value": 1}}
```

`execution` is `auto` or `manual`; `on_error` is `stop` or `continue`.
Repetition uses one `loop` step with nested steps and exactly one
`foreach.items` or `foreach.count` source.

Media references use `uses` only. `vision` sends resolved image pixels to the
prompt model, `reference` sends media to generation, and `source` adopts one
existing media output directly. `vision` and `reference` may be combined.

Provider names, model ids, model tiers, API addresses, generated content,
runtime statuses, canvas node ids, private runners, and compiled prompt phases
are not portable workflow fields. The runtime resolves them from project and
user configuration.

Earlier workflow schemas and authoring aliases are unsupported and must be
rewritten as V2 before import.
