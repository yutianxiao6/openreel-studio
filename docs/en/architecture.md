# Architecture

English · [简体中文](../zh-CN/architecture.md) · [Documentation home](../README.en.md)

## Overview

OpenReel Studio is a separated frontend/backend application whose creative source of truth is the visible node canvas.

```text
Browser / Electron
       │
       ▼
Next.js Web ── REST + SSE ── FastAPI
                              │
                ┌─────────────┼─────────────┐
                ▼             ▼             ▼
            Agent Loop    Node Runners   Workflow Runtime
                │             │             │
                └──── Tool Registry ────────┘
                              │
               SQLite + local assets + providers
```

## Repository layout

```text
apps/
  web/                 Next.js, React, and React Flow frontend
  api/                 FastAPI, agent, tools, and media services
  desktop/             Electron desktop shell
packages/
  shared/              Shared types and logic
  installer/           Release downloader CLI
config/                Runtime examples and media protocol catalogs
docs/                  Separate Chinese and English documentation
plugins/               Built-in or example plugins
skills/                User-editable <kebab-name>/SKILL.md packages
workflow_templates/    User workflow templates
data/                  SQLite, traces, tool results, and caches
storage/               Uploaded, generated, and exported assets
```

`data/` and `storage/` are runtime state, not source code, and must not be committed.

## Frontend

The frontend lives in `apps/web`:

- `app/`: pages, routes, and base-path entry points.
- `components/canvas/`: creation canvas, node details, workflow panel, and video editor.
- `components/settings/`: LLM, media, agent, and raw configuration surfaces.
- `stores/`: chat, project, and canvas state.
- `lib/`: API clients, event types, and display conversion.

The frontend consumes structured REST and SSE events. It does not parse node or edge operations out of natural-language agent replies.

## Backend

The backend lives in `apps/api/app`:

- `main.py`: FastAPI application entry point.
- `api/`: project, chat, node, asset, configuration, workflow, and debug routes.
- `agent/`: agent loop, prompt assembly, context management, permissions, and traces.
- `mcp_tools/`: core and deferred tools available to agents.
- `services/`: media generation, history, editing, export, and domain execution.
- `config_store/`: validation and materialization of `config/runtime.jsonc`.
- `skills/`: built-in runtime Skills and their `agents/`, `references/`, `scripts/`, and `assets/` resources.

## Node-first model

The creation canvas exposes four deliverable types:

- `text`
- `image`
- `video`
- `audio`

Node fields contain prompts, model settings, status, references, and outputs. `fields.references` represents production dependencies and maps to canvas edges. `parent_node_id` is for visual grouping only.

## Agent and tools

Ordinary natural-language requests enter the agent loop. The runtime injects a catalog containing each available Skill's `name`, `description`, and location. When the user names a Skill explicitly or the request clearly matches its description, the model reads the complete `SKILL.md` before invoking permission-controlled tools to create, update, and run nodes. The backend enforces safety confirmation, schemas, persistence, context limits, and event delivery without guessing business intent through keyword routing.

Frequently used creative tools stay in a stable core surface. Lower-frequency capabilities are discovered and loaded on demand. Large tool results are stored outside the model context, which retains only summaries and references.

## Workflows

A workflow spec describes a reusable process. Runtime instances resolve inputs, collections, loops, and step state. User-facing text, image, video, and audio results are still written to canvas nodes. This keeps reusable process structure separate from editable deliverables.

See the [Workflow Spec protocol](../workflow-spec-protocol.md).

## Media providers

Runtime configuration stores provider identity, model name, base URL, key, and UMA protocol/target references. Image, video, and audio HTTP requests, uploads, polling, status interpretation, and result extraction are declared by `config/universal_model_adapter/protocols/*.json` and the corresponding media target catalogs. See [Model providers](./model-providers.md).

## Persistence and events

- SQLite stores projects, messages, nodes, materialized configuration, and workflow state.
- `storage/` stores uploads, generated results, and exports.
- SSE carries chat deltas, node state, canvas changes, media progress, and token usage.
- Traces are mirrored to JSONL and the database for the diagnostic UI.

## Safety boundaries

- Canvas deletion and full reset require structured confirmation.
- Tool execution passes through permission policy.
- Project events are isolated by `project_id`.
- API keys must not appear in logs, screenshots, commits, or public issues.
- Uploaded and workspace file operations are constrained to approved roots.
