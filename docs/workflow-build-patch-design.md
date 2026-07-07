# Workflow Build Patch Design / 工作流搭建补丁写入设计

Date: 2026-07-06

## 中文摘要

Workflow Build Mode 是一个专门搭建和修改可复用 workflow spec 的模式。
它不参与普通视频制作，也不把完整 workflow 内容塞进主 Agent 上下文。

本设计把模型可见的写入路径收敛为一个补丁式写入工具：
`workflow.spec.apply_patch`。模型先读取必要的 skill、模板摘要或当前 artifact，
然后一次性创建、替换或局部修改 workflow spec。写入工具负责校验协议、生成画布
投影、保存 artifact 或用户模板，并返回短引用、输入字段和校验结果。

这样做的目标是减少工具编排成本，让模型把注意力放在 workflow 结构是否正确，而
不是记住一串 start/append/commit/promote 工具调用顺序。

## Purpose

Workflow Build Mode should behave like a focused spec editor: the model reads
the minimum context it needs, applies one validated change, and gets a
structured result. The current workflow authoring path exposes too many write tools
(`workflow.spec.start`, `workflow.spec.append_steps`, `workflow.spec.commit`,
`workflow.spec.patch`, `workflow.template.clone_to_artifact`,
`workflow.template.promote`). That makes the model learn tool choreography
instead of workflow design.

This design keeps the existing workflow protocol, compiler, artifact store,
template store, audit, runtime, and frontend behavior. It changes the
model-visible authoring interface to one patch/apply pipe owned by OpenReel.

## Current Problems

1. Workflow Build Mode still exposes a multi-tool write sequence.

   The model must decide when to start a draft, append steps, commit it, clone a
   template, patch an artifact, promote it, and export it. That is tool
   choreography, and it increases tool calls.

2. New spec writing uses in-memory draft state.

   `workflow.spec.start/append_steps/commit` stores draft state in
   `_WORKFLOW_DRAFTS`. This is fragile for long model turns, retries, and
   process restarts. It also forces multi-call authoring.

3. Prompt instructions have too much tool procedure.

   Workflow Build Mode should teach the authoring spec and the single write
   path. The default `workflow_spec` subagent remains a selector only: it reads
   candidates and returns an existing `template_id`.

4. Saving as a reusable template is a separate model path.

   A user intent like "make this a future template" should not require the
   model to create an artifact first and then remember a second promotion tool.
   The write pipe can save either a project artifact or a user template.

## Goals

- Keep Workflow Build Mode separate from ordinary production.
- Keep authoring schema `openreel.workflow.authoring.v1`.
- Keep runtime schema `openreel.workflow.v1`.
- Keep `workflow_authoring_spec`, `canvas_workflow_templates`,
  `workflow_spec_artifacts`, `workflow_template_store`, and deterministic audit.
- Replace visible spec writing with one tool:
  `workflow.spec.apply_patch`.
- Let that one tool create a new workflow, patch an existing artifact, patch a
  template into a project artifact, or save directly as a user template.
- Return a short template reference and input fields to the main Agent; full
  workflow content stays out of the main Agent context during default
  production.
- Keep read/discovery tools lightweight and explicit.
- Keep plugin/capability discovery available without putting protocol manuals in
  every prompt.

## Non-Goals

- No new keyword route or backend shortcut for business intent.
- No change to workflow execution semantics.
- No change to node truth source: visible products remain canvas nodes.
- No general workspace file write tool in Workflow Build Mode.
- No requirement for end users to write JSON. JSON/patch text is model output
  inside Workflow Build Mode only.

## Target Tool Surface

Workflow Build Mode core tools should become:

```text
project.get_state
interaction.request_input
skill.search
skill.get
workflow.template.resolve
workflow.template.read
workflow.spec.read
workflow.spec.apply_patch
workflow.template.export
```

Optional/deferred, not core:

```text
workflow.protocol_info
workflow.semantic_review
workflow.template.promote
workflow.template.clone_to_artifact
workflow.spec.start
workflow.spec.append_steps
workflow.spec.commit
workflow.spec.patch
```

Notes:

- `workflow.protocol_info` stays available for plugin/capability diagnostics and
  custom extension authoring, but normal workflow authoring should not spend a
  tool call on it. Core protocol facts stay in code validators and short prompt
  constants.
- `workflow.semantic_review` remains a targeted review tool for cases where
  deterministic audit cannot judge goal fit. The default check is deterministic.
- `workflow.template.promote` and `workflow.template.clone_to_artifact` become
  internal service operations used by `workflow.spec.apply_patch`.
- Old spec tools should be removed from model-visible core. If temporarily kept
  for compatibility tests, tests must assert they are not in the Workflow Build
  Mode core profile.

## New Tool Contract

Tool name:

```text
workflow.spec.apply_patch
```

Description:

```text
Create or revise a reusable workflow spec, validate it, and save the result as
a project artifact or user template.
```

Initial JSON-compatible schema:

```json
{
  "type": "object",
  "properties": {
    "project_id": { "type": "string" },
    "operation": {
      "type": "string",
      "enum": ["create", "update", "replace"]
    },
    "base": {
      "type": "object",
      "properties": {
        "artifact_ref": { "type": "string" },
        "template_id": { "type": "string" },
        "version_id": { "type": "string" }
      },
      "additionalProperties": false
    },
    "workflow": {
      "type": "object",
      "additionalProperties": true,
      "description": "Required for create and replace."
    },
    "operations": {
      "type": "array",
      "items": { "type": "object", "additionalProperties": true },
      "description": "Merge/insert/remove/path operations for update."
    },
    "sample_inputs": {
      "type": "object",
      "additionalProperties": true
    },
    "context": {
      "type": "object",
      "additionalProperties": true
    },
    "save": {
      "type": "object",
      "properties": {
        "target": {
          "type": "string",
          "enum": ["artifact", "template"]
        },
        "template_id": { "type": "string" },
        "name": { "type": "string" },
        "description": { "type": "string" },
        "category": { "type": "string" },
        "applies_to": { "type": "string" },
        "version": { "type": "string" },
        "replace_existing": { "type": "boolean" }
      },
      "additionalProperties": false
    },
    "user_preview": {
      "type": "object",
      "additionalProperties": true
    },
    "self_check": {
      "type": "object",
      "additionalProperties": true
    }
  },
  "required": ["project_id", "operation"]
}
```

Successful response:

```json
{
  "ok": true,
  "status": "completed",
  "operation": "create",
  "artifact_ref": "workflow_spec:...",
  "template_id": "",
  "version_id": "",
  "preview": {},
  "input_fields": [],
  "validation": {
    "ok": true,
    "workflow_id": "...",
    "step_count": 8,
    "dimension_count": 1,
    "deferred_group_count": 1,
    "audit": {}
  },
  "applied": [],
  "self_check": {},
  "next_action": "Return the reference and input fields to the main Agent."
}
```

Failure response:

```json
{
  "ok": false,
  "error_kind": "workflow_audit_failed",
  "error": "...",
  "applied": [],
  "audit": {},
  "hint": "Patch the workflow and retry once with the returned evidence."
}
```

Later freeform shape:

```text
*** Begin Workflow Patch
*** Operation: create
*** Save Target: template
*** Template ID: text_to_video_workflow
*** Workflow
{ ...authoring workflow JSON... }
*** Sample Inputs
{ ... }
*** End Workflow Patch
```

The initial implementation can use the JSON-compatible schema because the
current registry exports function tools with JSON schema. The backend service
must be written so the parser is replaceable by a freeform grammar later.

## Patch Operation Semantics

`create`

- Requires `workflow`.
- Accepts authoring or runtime workflow.
- Compiles authoring workflow when `schema='openreel.workflow.authoring.v1'`.
- Validates, audits, then saves.

`replace`

- Requires `base.artifact_ref` or `base.template_id`.
- Requires full `workflow`.
- Replaces the base workflow with the supplied workflow.
- Saves a new artifact or template version; it never mutates the base artifact
  in place.

`update`

- Requires `base.artifact_ref` or `base.template_id`.
- Requires `operations`.
- Supports existing operation types from `_apply_workflow_spec_patch_operations`:
  `merge_workflow`, `merge_step`, `add_step`, `insert_between`, `remove_step`,
  `replace_steps`, and path `add/replace`.
- Saves a new artifact or template version.

`save.target`

- `artifact`: write a project-scoped workflow spec artifact.
- `template`: write directly to `PROJECT_ROOT/workflow_templates`, with metadata
  suitable for frontend selection and export.
- Default is `artifact`.

## Backend Structure

Add a service module:

```text
apps/api/app/agent/workflow_spec_patch.py
```

Responsibilities:

1. Normalize request.
2. Load base workflow:
   - empty for `create`
   - `workflow_spec_artifacts.load_workflow_spec_artifact` for artifact refs
   - `workflow_template_store.load_user_template` or
     `canvas_workflow_templates.get_template` for templates
3. Apply requested mutation:
   - full workflow for `create` and `replace`
   - patch operations for `update`
4. Compile authoring spec with `workflow_authoring_spec` when needed.
5. Normalize runtime spec with `canvas_workflow_templates.normalize_inline_workflow`.
6. Reject framework contamination with the existing content guard.
7. Run deterministic audit with `ensure_workflow_audit_passes`.
8. Save:
   - artifact path through `workflow_spec_artifacts.save_workflow_spec_artifact`
   - user template path through `workflow_template_store.save_user_template`
9. Return compact preview, input fields, validation, audit, and applied change
   summary.

The service owns the write semantics. `workflow_tools.py` should only register
the tool and call the service.

## Prompt And Subagent Changes

Workflow Build Mode prompt becomes short:

```text
# Workflow Build Mode

Build reusable workflow files.

- Read source knowledge with skill.search/get and template/spec reads.
- Reuse an existing template when it matches the requested workflow.
- Use workflow.spec.apply_patch to create, revise, or save workflows.
- Return the workflow name, inputs, visible outputs, audit status, saved ref,
  and readiness.
```

`workflow_spec` subagent changes:

- `workflow_spec` is read-only and returns an existing `template_id`.
- It uses skill/template/spec read tools only.
- It does not expose `workflow.spec.apply_patch` or `workflow.canvas.inspect`.
- Build, patch, save, audit, and canvas projection live in `/workflow`
  Workflow Build Mode.
- The result contract is template-first:

```json
{
  "status": "completed",
  "decision": "reuse_existing",
  "template_id": "",
  "input_fields": [],
  "validation": {},
  "self_check": {}
}
```

Main Agent behavior stays:

- Default mode delegates workflow selection to `workflow_spec` selector.
- Main Agent reads returned input fields and asks only missing blocking inputs.
- Main Agent runs the chosen template.
- Main Agent does not read full workflow skills/templates/specs.
- Workflow Build Mode performs create/patch/save work directly with
  `workflow.spec.apply_patch`.

## Implementation Steps

### Step 1 - Add The Patch Service

Create `apps/api/app/agent/workflow_spec_patch.py`.

Implement:

```python
def apply_workflow_spec_patch(
    *,
    project_id: str,
    operation: str,
    base: dict[str, Any] | None,
    workflow: dict[str, Any] | None,
    operations: list[dict[str, Any]] | None,
    sample_inputs: dict[str, Any] | None,
    context: dict[str, Any] | None,
    save: dict[str, Any] | None,
    user_preview: dict[str, Any] | None,
    self_check: dict[str, Any] | None,
) -> dict[str, Any]:
    ...
```

Move or reuse these helpers from `workflow_tools.py`:

- `_apply_workflow_spec_patch_operations`
- `_workflow_framework_content_error`
- `_dimension_input_values`
- `_workflow_template_input_definitions`
- `_workflow_protocol_payload`

Keep helper movement behavior-preserving. The first implementation can import
private helpers from `workflow_tools.py` if needed, then cleanly extract once
tests cover the service.

### Step 2 - Register `workflow.spec.apply_patch`

Add the registered tool in `apps/api/app/mcp_tools/workflow_tools.py`.

The handler should:

- validate `project_id`
- call `workflow_spec_patch.apply_workflow_spec_patch`
- return service output unchanged except for tool-specific `next_action`

Do not create canvas nodes in this tool.

### Step 3 - Internalize Old Write Tools

Update `apps/api/app/mcp_tools/registry.py`:

- Add `workflow.spec.apply_patch` to `_WORKFLOW_BUILD_CORE_TOOLS`.
- Remove these from `_WORKFLOW_BUILD_CORE_TOOLS`:
  - `workflow.spec.start`
  - `workflow.spec.append_steps`
  - `workflow.spec.commit`
  - `workflow.spec.patch`
  - `workflow.template.clone_to_artifact`
  - `workflow.template.promote`
  - `workflow.protocol_info`
  - `workflow.semantic_review`

Keep old functions only if internal callers/tests still need them during the
same patch series. They must not be visible in the workflow build core tool
profile.

### Step 4 - Update Subagent Tool Lists

Update `apps/api/app/mcp_tools/agent_tools.py`.

Selector tools:

```text
skill.search
skill.get
workflow.template.resolve
workflow.template.read
workflow.spec.read
```

Builder tools:

```text
skill.search
skill.get
workflow.template.resolve
workflow.template.read
workflow.spec.read
workflow.spec.apply_patch
```

Keep `WORKFLOW_SPEC_MAX_OUTPUT_TOKENS = 10000`.

Reduce `max_steps` after the new tool lands. Target: 8 to 10 steps. The old 16
was covering start/append/commit choreography.

### Step 5 - Shorten Prompt Text

Update:

- `apps/api/app/agent/prompts/workflow_build_mode.py`
- workflow_spec role prompt in `apps/api/app/agent/workflow_spec_role.py`
- `_build_workflow_spec_task_message`
- registry standard descriptions/usages for workflow tools
- `BLUEPRINT_OPERATING_MODEL.md`
- `AGENTS.md` if the durable tool contract changes

Remove references to visible `workflow.spec.start/append_steps/commit` from
model-facing prompts.

### Step 6 - Preserve Protocol And Template Docs

Update `docs/workflow-spec-protocol.md` only where it describes authoring
workflow usage. The protocol itself remains unchanged.

Add a short section explaining:

- users do not write JSON
- model-authored specs use authoring schema
- backend compiles and validates
- successful builds save as artifact or template

### Step 7 - Replace Tests

Update contract tests first:

- `apps/api/tests/test_agent_tool_contracts.py`
- `apps/api/tests/test_prompt_runtime_intake_contracts.py`
- `apps/api/tests/test_subagent_readonly.py`
- `apps/api/tests/test_context_compact.py`

Expected assertions:

- Workflow Build Mode core includes `workflow.spec.apply_patch`.
- Workflow Build Mode core excludes old spec draft tools.
- Prompt budget decreases or stays within budget.
- Builder subagent has only one write tool.

Then update workflow behavior tests:

- create new spec through `workflow.spec.apply_patch(operation='create')`
- patch artifact through `operation='update'`
- patch template directly without separate clone tool
- save directly as user template with `save.target='template'`
- reject prefilled runtime output/content
- reject invalid dependencies
- reject no-op updates
- export saved template with `workflow.template.export`

### Step 8 - Remove Dead Surface

After tests pass:

- Remove old workflow spec write tools from model-facing search metadata.
- Remove prompt references and usage hints for old write sequence.
- Keep low-level runtime draft tools only if still used by non-agent internals.
- Keep old Python functions only if directly called by tests or internal
  services; otherwise delete them.

### Step 9 - Live Validation

Run three natural-language black-box cases and score them with
`docs/AGENT_QUALITY_ACCEPTANCE.md`.

Required cases:

1. Reuse existing template:
   - User asks for a normal 30-second video workflow.
   - Expected: selector returns default reusable template; no new spec write.

2. Local patch in `/workflow`:
   - User asks to change one prompt or add one visible output to a chosen
     workflow.
   - Expected: one `workflow.spec.apply_patch` call and a new artifact/template
     version.

3. New workflow from skill in `/workflow`:
   - User gives a medium workflow skill and asks to build it as a future
     template.
   - Expected: builder reads skill, calls `workflow.spec.apply_patch` once with
     `save.target='template'`, export works, and dependencies/output content
     match the skill.

Pass criteria:

- task completion pass
- task correctness pass
- cost control no worse than current and preferably lower
- flexibility pass for renamed skill/template fields
- user experience pass with no internal tool choreography in final message
- observability pass with trace evidence and saved artifacts/templates

## Compatibility Notes

- This does not change the workflow protocol. Existing templates remain valid.
- Existing artifacts remain readable through `workflow.spec.read`.
- New writes should produce immutable artifact revisions or template versions.
- Old visible write tools should not remain in prompts as compatibility hints.
- If an old tool must stay registered for temporary tests, mark it deferred or
  hidden from the Workflow Build Mode core profile.

## Expected Result

After implementation, a Workflow Build Mode turn should usually look like:

1. `skill.search` or `workflow.template.resolve`
2. optional `skill.get` / `workflow.template.read`
3. `workflow.spec.apply_patch`
4. optional `workflow.canvas.inspect`
5. final response with saved template/artifact reference

That matches the project editing model: read the minimum required context,
apply one validated patch, and report the durable result.
