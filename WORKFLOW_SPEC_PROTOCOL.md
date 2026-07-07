# OpenReel Workflow Spec Protocol

This document defines the target workflow protocol for OpenReel Studio. The protocol separates model/user authoring, backend compilation, runtime execution, and UI graph presentation.

## Design Principles

- Authoring specs are for humans and Agents.
- Compiled specs are for backend validation and execution.
- Runtime graphs are for current project execution state.
- Product canvas nodes are the creative truth source, not workflow control nodes.
- UI ordering is not execution dependency.
- Dynamic repetition is represented once in the template and expanded only for runtime instances.

## Layer 1: Authoring Spec

Schema: `openreel.workflow.authoring.v1`.

The authoring spec is the source format written by workflow_spec subagents, imported user templates, and future visual workflow editing. It should be short, readable, and stable.

Authoring step example:

```json
{
  "id": "storyboard",
  "title": "宫格分镜图",
  "kind": "image",
  "needs": ["plan_frames"],
  "reads_from": ["main_characters", "scene"],
  "layout_after": ["scene_reference"],
  "references": {
    "appearing_characters": {
      "source": "plan_frames.output.appearing_characters",
      "candidates": "main_character_images",
      "role": "visual_reference"
    }
  },
  "prompt": {
    "system": "你是分镜图提示词编写者。",
    "task": "把每格规划组织成一张宫格分镜图提示词。",
    "output": "输出 image prompt 框架与字段建议。"
  },
  "output": {
    "canvas": true,
    "type": "image"
  }
}
```

Authoring step fields:

- `id`: stable local id.
- `title`: user-visible title.
- `kind`: `input`, `llm_text`, `llm_json`, `image`, `video`, `audio`, `review`, or `plugin`.
- `needs`: real blocking execution dependencies.
- `reads_from`: data/context reads that do not automatically block graph layout.
- `layout_after`: visual ordering only.
- `for_each` / `foreach`: dynamic repetition source.
- `steps`: nested child workflow for loop/group steps.
- `references`: media or asset selection rules.
- `prompt`: structured prompt sections.
- `output`: product/canvas/runtime output policy.
- `ui`: editable visual state.

Authoring specs should not require `runner`, `surface`, `visibility`, `reference_selectors`, `repeat_group_id`, `instance_scope`, or runtime node ids.

## Layer 2: Compiled Template Spec

Schema: `openreel.workflow.v1`.

The backend compiles authoring specs into compiled template specs. This is the durable executable template format.

Compiled spec responsibilities:

- Normalize ids.
- Validate dependency order for `depends_on`.
- Preserve nested loop blocks.
- Compile prompt sections into `prompt_template`.
- Infer `node_type`, `runner`, `surface`, `visibility`, and `output_mode`.
- Compile `references` into `reference_selectors`.
- Preserve UI-only graph metadata such as `layout_after`.
- Preserve authoring provenance in `authoring`.

Compiled step example:

```json
{
  "id": "episode_segments",
  "title": "按集/段制作",
  "node_type": "text",
  "kind": "loop",
  "role": "repeat_group",
  "depends_on": ["main_characters", "plan_characters_scenes"],
  "layout_after": ["main_character_images"],
  "repeat": {
    "mode": "per_episode_segment",
    "episode_count": "episodeCount",
    "segment_count": "segmentCount"
  },
  "surface": "workflow_runtime",
  "visibility": "flow_only",
  "steps": []
}
```

Compiled template specs are still reusable. They should not contain this project run's generated script, images, videos, or one-off runtime outputs.

## Layer 3: Template Graph

Template graph is an API view generated from the compiled template spec. It is not a new source of truth.

The template graph keeps hierarchy:

```json
{
  "nodes": [
    {
      "id": "episode_segments",
      "title": "按集/段制作",
      "shape": "loop",
      "children_scope": "episode_segments"
    }
  ],
  "edges": [
    {
      "source": "main_character_images",
      "target": "episode_segments",
      "type": "layout"
    }
  ],
  "scopes": {
    "root": ["input", "script", "episode_segments"],
    "episode_segments": ["minor_characters", "scene", "storyboard"]
  }
}
```

Template graph node shapes:

- `input`
- `step`
- `collection`
- `loop`
- `plugin`
- `review`

Template graph edge types:

- `execution`: from `depends_on`.
- `read`: from `reads_from` or `context_refs`.
- `layout`: from `layout_after`.
- `reference`: from `reference_selectors`.
- `previous_instance`: from `depends_on_previous`.

The frontend should render only one scope at a time. It should not flatten loop children into the root graph.

## Layer 4: Runtime Instance Graph

Runtime instance graph is project-specific. It is generated from compiled spec plus current input values and workflow runtime outputs.

Runtime instance nodes include:

```json
{
  "id": "episode_segments_e1_s2_storyboard",
  "template_step_id": "storyboard",
  "repeat_group_id": "episode_segments",
  "repeat_group_label": "按集/段制作",
  "instance_scope": {
    "episode_index": 1,
    "segment_index": 2
  },
  "status": "completed",
  "output_preview": {}
}
```

Runtime graph is allowed to show execution status. Template graph is not.

## Product Canvas Output

`output.canvas=true` creates or updates a product node on the creative canvas.

`output.canvas=false` keeps the result in workflow runtime.

Canvas product nodes should not include loop blocks, collection controls, or workflow-only planning nodes unless the template explicitly marks them as canvas outputs.

## Dynamic Expansion

Dynamic expansion has two modes:

### Known cardinality

When count comes from user input, for example `episodeCount=2` and `segmentCount=4`, the backend can preview all instances before running every step.

### Deferred cardinality

When count comes from an upstream output, for example `main_characters.output.main_characters[]`, the template graph shows a loop block. Runtime graph expands after the upstream step completes.

Deferred groups must remain visible and explain what will trigger expansion.

## Relationship Semantics

### `depends_on`

Execution dependency. The backend validates dependency order and uses it for readiness.

### `reads_from` / `context_refs`

Context dependency. The step reads upstream outputs, but this relation does not force the primary visual flow line.

### `reference_selectors`

Media reference selection. This is used for dynamic image/video references, such as selecting only characters appearing in a segment.

### `layout_after`

Visual ordering. The runtime ignores this for execution. Validators only need to check that referenced ids exist somewhere in the same scope or parent scope.

### `depends_on_previous`

Cross-instance continuity. The backend rewrites this to the previous instance's concrete step id during expansion.

## Authoring to Compiled Mapping

| Authoring field | Compiled field |
| --- | --- |
| `kind=input` | `runner=workflow_input`, `node_type=text` |
| `kind=llm_json` | `node_type=text`, `output_mode=json` |
| `kind=image` | `node_type=image`, `runner=node.run` |
| `needs` | `depends_on` |
| `reads_from` | `context_refs` |
| `layout_after` | `layout_after` |
| `references` | `reference_selectors` |
| `for_each` | `repeat.foreach` or group `foreach` |
| `prompt` | `prompt_template` |
| `output.canvas` | `surface` and `visibility` |

## ArtChat Grid Example

The ArtChat grid workflow should be represented as:

```json
{
  "id": "main_character_images",
  "title": "按主要人物生成参考图",
  "kind": "loop",
  "needs": ["main_characters"],
  "for_each": "main_characters.output.main_characters",
  "steps": [
    {
      "id": "main_character_image",
      "title": "主要人物参考图",
      "kind": "image",
      "output": { "canvas": true, "type": "image" }
    }
  ]
}
```

```json
{
  "id": "episode_segments",
  "title": "按集/段制作",
  "kind": "loop",
  "needs": ["main_characters", "plan_characters_scenes"],
  "layout_after": ["main_character_images"],
  "repeat": {
    "mode": "per_episode_segment",
    "episode_count": "episodeCount",
    "segment_count": "segmentCount"
  },
  "steps": [
    {
      "id": "storyboard",
      "title": "宫格分镜图",
      "kind": "image",
      "needs": ["plan_frames", "scene_reference"],
      "references": {
        "appearing_characters": {
          "source": "plan_frames.output.appearing_characters",
          "candidates": "main_character_images",
          "role": "visual_reference"
        }
      }
    }
  ]
}
```

The segment loop uses `layout_after` to appear after the character-image loop. It should not use `depends_on=["main_character_images"]`, because each segment should select only the characters it needs.

## Compatibility

Existing `openreel.workflow.v1` specs remain valid.

Compatibility rules:

- Missing `layout_after` means layout is inferred from `depends_on`.
- Missing `kind` means frontend infers from `role`, `repeat`, `foreach`, `collection`, `plugin`, and `node_type`.
- Legacy flattened `steps` can still render in fallback mode.
- New graph APIs should include legacy `steps` until the frontend no longer depends on them.

## Backend Work Items

1. Extend `workflow_authoring_spec.py` to accept `reads_from`, `layout_after`, `kind=loop`, nested loop metadata, and UI graph fields.
2. Extend `canvas_workflow_templates.py` validation to preserve hierarchy and validate `layout_after` separately from `depends_on`.
3. Add template graph generation for root and nested scopes.
4. Add runtime graph generation for expanded instances.
5. Update workflow spec artifact preview to return authoring summary, compiled summary, and graph preview without returning huge specs to the main Agent.
6. Keep legacy `steps` and existing runner behavior during migration.

## Frontend Work Items

1. Split template graph rendering from runtime instance graph rendering.
2. Render loop blocks as drill-down nodes.
3. Add breadcrumb navigation for nested scopes.
4. Save positions per scope.
5. Add edge type controls in node/edge inspector.
6. Render runtime instances grouped by repeat scope.
7. Keep creative canvas product-only.

## Acceptance Criteria

- A workflow with loop blocks displays top-level blocks, not flattened child nodes.
- Template mode never displays runtime status.
- Runtime mode displays expanded instances and execution state.
- Editing a child workflow once affects all runtime instances.
- `layout_after` can make one loop appear after another without changing execution readiness.
- ArtChat grid template visually reads as a single production flow.
- Old specs remain importable and runnable.
