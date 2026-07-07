---
name: project_mentor
tool_name: skill.project_mentor
description: OpenReel project mentor for node-first production, debugging, repair, and prompt hygiene.
when_to_use: Use for project rules, video workflow selection, node repair, delivery audit, trace/debug guidance, or prompt maintenance.
tags: [project, mentor, video, production, guide]
source: skill
---

# project_mentor

This skill explains OpenReel Studio project rules and points to the right local
guide when the model needs more than workflow/prompt skills.

Default production is node-first: the model works on one visible canvas with
`text`, `image`, `video`, and `audio` nodes. It does not create a separate blueprint or
maintain separate canvas/panel state before work appears.

## Topics

- `overview`: repo layout, commands, and current architecture.
- `agent_loop`: Agent loop, core tools, permission policy, trace, and compaction.
- `video_workflow`: default video workflow selection and information gathering.
- `video_workflow_t2v`: direct text-to-video.
- `video_workflow_storyboard`: storyboard/grid image driven video.
- `video_workflow_shot_images`: separate high-quality shot image driven video.
- `video_workflow_story_template`: story-template board driven video.
- `production_audit_guide`: final delivery and consistency review.
- `node_repair_guide`: failed node repair, rerun, and dependency recovery.
- `slash_commands`: deterministic command surface.
- `debugging`: trace, SSE, messages, tool results, and artifacts.
- `prompt_compaction`: prompt/cache budget and where rules should live.

## Current Rules

- Ordinary image/video work starts by searching user workflow skills, then reads
  the builtin `video_production` markdown skill through `skill.search` / `skill.get`
  when no user workflow matches. It then creates
  lightweight tasks for multi-step or media-generation work, then creates or
  updates `text`, `image`, `video`, and `audio` nodes directly on the canvas.
- Main Agent plans the node graph and dependency order. Each node is an
  independent task. Script, character image, scene image, shot grid image, and
  final video prompt are produced by `node.run` with one module prompt skill
  at a time.
- Reusable graph workflows are executed by deferred `workflow.run_step`,
  `workflow.run_next`, or `workflow.run_all` with `inputs`; the workflow runner
  calls `node.run` internally for visible product nodes.
- Canvas state is the creative truth source visible to the model. Drafts,
  grouping, method choice, review notes, and assumptions are node fields or text
  nodes, not a separate blueprint object.
- Dependencies are expressed with `parent_node_id` and `fields.references`;
  backend-created edges appear automatically. Use `{ref, role}` when needed:
  `visual_reference` for generation reference, `source_image` when an image node
  directly adopts an existing image as output.
- Project-local node numbers such as `#0` / `0` resolve directly with
  `node.get(node_id)`; titles or unclear references use `node.list(query|regex)`.
- Use `interaction.request_input(questions=[...])` only for blocking missing
  facts, up to 6 concise questions.
- Use `canvas.delete` only when the latest user message explicitly asks to
  remove canvas nodes; destructive actions require structured confirmation.
- Generated media remains in node output and local project storage by default.
  Save to the asset library only when the user explicitly asks.
- Natural-language tasks enter the Agent loop. Backend preprocessing may clean
  input and stale state, but it must not decide business actions for the model.
- Tool errors are observations. Read `error_kind`, `hint`, and
  `model_feedback`; repair the specific node or field before retrying.
- System prompt stays short. Detailed workflow, examples, and debugging advice
  live in skills, docs, tests, validators, and permission policy.

## Core Tools

`project.get_state`, `interaction.request_input`, `skill.search`, `skill.get`,
`task.create`, `task.list`, `task.update`, `task.complete`, `agent.review`,
`node.list`, `node.get`, `node.create`, `node.update`, `node.run`, and
`canvas.delete`. `tool.search`, `tool.describe`, and `tool.execute` are core
meta tools for discovering and running low-frequency deferred capabilities.

## References

- `README.md` and `SETUP.md`: public setup, usage, and packaging entry points.
- `apps/api/app/agent/prompts/`: short always-loaded prompt sections.
- `apps/api/app/mcp_tools/registry.py`: tool exposure and core/deferred surface.
- `apps/api/app/agent/orchestrator.py`: Agent loop and confirmation handling.
- `apps/api/app/skills/video_production/`: default node-first workflow index.
- `apps/api/app/skills/*_prompt/` and `script_writing/`: builtin prompt modules.

## Output

Give the next concrete action, relevant files, and the rule that justifies it.
When a question is about production, prefer the node/canvas path unless the user
explicitly asks about legacy blueprint internals.
