# Workflow guide

English · [简体中文](../zh-CN/workflows.md) · [Documentation home](../README.en.md)

## When to use a workflow

A one-off image or video can be created with standalone nodes. A workflow is useful when:

- the same production sequence will be reused across projects;
- inputs, dependencies, collections, and loops must be explicit;
- a team should follow one repeatable process;
- prompt preparation should be separated from the actual media call;
- each step needs observable runtime status and failure boundaries.

## Two canvases

- The **workflow panel** defines reusable process structure.
- The **creation canvas** displays text, image, video, and audio produced by a run.

Collection planning, loop expansion, and input normalization may stay in workflow runtime. Only user-facing, editable deliverables need canvas nodes.

## Run an existing template

1. Open the workflow panel.
2. Select a built-in or user template.
3. Read its summary and required inputs.
4. Provide the story, duration, aspect ratio, material, and other parameters.
5. Run the next step, a selected step, or the full workflow.
6. Return to the creation canvas and inspect generated nodes and references.

A built-in template retains a recoverable source definition. Editing it creates an editable user copy; reset restores the built-in definition without overwriting unrelated user templates.

## Build a workflow

In Workflow Build Mode, design from inputs toward deliverables:

1. Define required runtime `inputs`.
2. Add text planning or content-generation steps.
3. Use collections and loops only when cardinality is genuinely dynamic.
4. Declare real execution dependencies.
5. Add visual-reference selectors for image and video steps.
6. Mark which steps create canvas outputs and which remain runtime-only.
7. Inspect the canvas projection before spending on media generation.
8. Save as a user template and test it with a different input set.

## Prompt responsibility

Each step prompt should describe only that step. It should not include the complete workflow JSON, every node, or unrelated history. A useful structure contains:

- role or expertise;
- current task;
- available inputs and upstream outputs;
- output requirements;
- self-check criteria.

Content prompts produce normal prose unless the step contract specifically requires structured data. Character descriptions, scene prompts, and final video prompts should not emit JSON without a protocol reason.

## Dependencies and references

- `needs` controls execution ordering.
- Node `fields.references` identifies the text or visual material a deliverable actually reads.
- `parent_node_id` is visual grouping, not a production dependency.
- A video commonly references character, scene, and storyboard assets together; selectors can choose only the assets relevant to the current segment.

Execution order and canvas position are independent. Do not create fake dependencies for layout, and do not omit a real dependency because two nodes appear near each other.

## Collections and loops

When character, segment, or episode count comes from inputs or upstream output, a loop needs an explicit source:

- `for_each` points to an array;
- `repeat.count` points to a known count;
- or a supported episode/segment cardinality expression is used.

Child steps read the current item. Stable template step IDs produce distinct runtime instances. Copying several fixed steps that all read the first item is not a loop.

## Manual generation

Manual generation does not skip the step. The workflow still:

1. resolves upstream output;
2. creates the target node;
3. writes the final prompt and references;
4. pauses before calling the media provider.

The user can then run that node explicitly.

## Save and version behavior

- Saving a user template updates that template.
- Editing a built-in template creates one user-owned copy.
- **Save as** explicitly creates another template; ordinary save should not duplicate it.
- Check `required_capabilities` and `required_extensions` before import.
- Missing required extensions must fail before materialization or execution.

## Pre-release checklist

- Inputs have clear names and types.
- Step IDs are unique and stable.
- Dependencies are complete and acyclic.
- Every loop has a concrete source.
- Image and video references are complete.
- Canvas outputs and runtime-only intermediates are classified correctly.
- Manual steps pause before the media call.
- At least one realistic input passes projection and runtime testing.

See the [Workflow Spec protocol](../workflow-spec-protocol.md) for field-level details.
