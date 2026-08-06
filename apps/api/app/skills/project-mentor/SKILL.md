---
name: project-mentor
description: Explain and audit OpenReel Studio architecture, repository rules, Agent orchestration, node repair, delivery checks, debugging, or prompt maintenance. Use when the user asks how the project works, where behavior lives, why an Agent or node failed, or how to verify a repository-level change; do not use for ordinary media creation.
---

# Project mentor

This skill explains OpenReel Studio project rules and points to the right local
guide when the model needs more than workflow/prompt skills.

Default production is node-first: the model works on one visible canvas with
`text`, `image`, `video`, and `audio` nodes. It does not create a separate planning object or
maintain separate canvas/panel state before work appears.

## Topics

- `overview`: repo layout, commands, and current architecture.
- `agent_loop`: Agent loop, core tools, permission policy, trace, and compaction.
- `production_audit_guide`: final delivery and consistency review.
- `node_repair_guide`: failed node repair, rerun, and dependency recovery.
- `slash_commands`: deterministic command surface.
- `debugging`: trace, SSE, messages, tool results, and artifacts.
- `prompt_compaction`: prompt/cache budget and where rules should live.

## Current Rules

- Ordinary image/video work uses the automatically injected Skill catalog,
  chooses the minimal description match, resolves its exact package with
  `skills.list`, and reads the complete `SKILL.md` with `skills.read`. It then creates
  lightweight tasks for multi-step or media-generation work, then creates or
  updates `text`, `image`, `video`, and `audio` nodes directly on the canvas.
- Main Agent plans the node graph and dependency order. Each node is an
  independent task. Script, character image, scene image, shot grid image, and
  final video prompt are produced by `node.run` with one module prompt skill
  at a time.
- Reusable graph workflows are executed through `tool.search`, `tool.describe`,
  and `tool.execute` for deferred `workflow.run_step`, `workflow.run_next`, or
  `workflow.run_all`; the workflow runner
  calls `node.run` internally for visible product nodes.
- Canvas state is the creative truth source visible to the model. Drafts,
  grouping, method choice, review notes, and assumptions are node fields or text
  nodes, not a separate planning object.
- Dependencies are expressed with `parent_node_id` and `fields.references`;
  backend-created edges appear automatically. Use `{ref, role}` when needed:
  `visual_reference` for generation reference, `source_image` when an image node
  directly adopts an existing image as output.
- Project-local node numbers such as `#0` / `0` resolve directly with
  `node.get(node_id)`; titles or unclear references use `node.list(query|regex)`.
- Read the smallest sufficient evidence for each decision: aggregate state,
  then indexes, details, and only the required content page.
- Use `interaction.request_input(questions=[...])` for blocking missing facts,
  then wait; ask up to 6 concise questions.
- An explicit destructive request calls its matching tool once at the intended
  scope; that first call creates structured confirmation and ends the turn.
- Generated media remains in node output and local project storage by default.
  Save to the asset library only when the user explicitly asks.
- Natural-language tasks enter the Agent loop. Backend preprocessing may clean
  input and stale state, but it must not decide business actions for the model.
- Summaries, rewrites, formatting, analysis, and plans return in chat unless the
  latest user message explicitly asks to save or change canvas content. An
  explicitly saved long text uses a placeholder `text` node with
  `fields.generation={instruction,source_message_count}`, followed by
  `node.run`. The count covers the source message and current save request
  (usually 2 for a follow-up request); the runner captures those message ids and
  atomically saves only a complete result. A successful run is final and does
  not need `node.get` verification. Text bodies are returned once in
  `content_page`, with an 8,000-character default window. Continue with
  `content_offset=content_page.next_offset` and a bounded `content_limit`;
  `content_limit=0` returns body metadata only.
- Tool errors are observations. Read `error_kind`, `hint`, and
  `suggested_next`; repair the specific node or field before retrying.
- Every tool result crosses one typed model-context compiler. JSON, documents,
  collections, and multimodal parts have executable per-tool policies and a
  global 10,000-token hard ceiling. Large raw results are retained only as
  project-scoped diagnostic artifacts; model context and SSE receive bounded
  projections plus an opaque `artifact_ref`. Only resumable page content gets
  the document-sized string window; unrelated nested strings keep the normal
  per-field ceiling.
- Long text readers such as `node.get`, file readers, text assets, and workflow
  spec/template readers expose bounded content pages with `next_offset`.
  `skills.read` uses the opaque `next_cursor` returned by its preceding page;
  continue until it becomes null.
- User skills use `skills/<skill-name>/SKILL.md`. Standard frontmatter supplies
  `name` and `description`; the kebab-case directory name matches `name`.
  Supporting files stay inside the same package under
  `references/`, `scripts/`, `assets/`, `templates/`, or `agents/openai.yaml`.
  The runtime prompt carries only bounded metadata. Current-turn `$SkillName`,
  linked `skill://` / `SKILL.md` mentions, and structured `kind=skill` inputs
  resolve before the model call and inject each selected package as a bounded
  `<skill>` block. Multiple explicit selections are all injected, and they do
  not carry into later turns. Explicit selection remains available when
  `agents/openai.yaml` disables implicit invocation. Description matches and
  plain-text names use the model-visible catalog. Orchestrator resources use
  exact handles from `skills.list`; `skills.read` follows `next_cursor` through
  the full `SKILL.md` and resolves resources inside the same package.
- Collection readers use bounded pages. `project.get_state` returns runtime
  state plus canvas counts rather than every node and edge; use `node.list` and
  `node.get` for details. Most collections expose `offset`/`next_offset` or a
  nested page; `skills.list` uses its opaque `next_cursor`.
- System prompt stays short. Detailed workflow, examples, and debugging advice
  live in skills, docs, tests, validators, and permission policy.

## Core Tools

`project.get_state`, `interaction.request_input`, `skills.list`, `skills.read`,
`task.create`, `task.list`, `task.update`, `task.complete`, `agent.review`,
`node.list`, `node.get`, `node.create`, `node.update`, `node.run`, and
`canvas.delete`. `tool.search`, `tool.describe`, and `tool.execute` are core
meta tools for discovering and running low-frequency deferred capabilities.

## References

- `README.md` / `README.en.md`: Chinese and English product entry points.
- `docs/README.md` / `docs/README.en.md`: language-specific documentation maps.
- `apps/api/app/agent/prompts/`: short always-loaded prompt sections.
- `apps/api/app/mcp_tools/registry.py`: tool exposure and core/deferred surface.
- `apps/api/app/agent/orchestrator.py`: Agent loop and confirmation handling.
- `apps/api/app/skills/video-production/`: default node-first workflow index.
- `apps/api/app/skills/*-prompt/` and `script-writing/`: builtin prompt modules.

## Output

Give the next concrete action, relevant files, and the rule that justifies it.
When a question is about production, prefer the node/canvas path unless the user
explicitly asks about removed planning internals.
