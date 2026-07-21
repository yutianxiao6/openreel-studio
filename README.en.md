# OpenReel Studio

English · [简体中文](./README.md)

[![Latest release](https://img.shields.io/github/v/release/yutianxiao6/openreel-studio?label=release)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![Release downloads](https://img.shields.io/github/downloads/yutianxiao6/openreel-studio/total?label=downloads)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![npm installer](https://img.shields.io/npm/v/openreel-studio-installer?label=npm%20installer)](https://www.npmjs.com/package/openreel-studio-installer)
[![License](https://img.shields.io/github/license/yutianxiao6/openreel-studio)](./LICENSE)

**Bring the agent, node canvas, reusable workflows, and frame-based timeline into one creative desktop.**

OpenReel Studio is an open-source, conversational workspace for AI video production. Start with a natural-language request and let the agent create and run visible text, image, video, and audio nodes. Build reusable production systems in the workflow editor, then finish the result in the built-in timeline.

[Quick start](./docs/en/getting-started.md) · [User guide](./docs/en/user-guide.md) · [Workflows](./docs/en/workflows.md) · [Architecture](./docs/en/architecture.md)

![The new OpenReel Studio creation canvas with chat, visible deliverables, and dependency edges in one workspace](./docs/assets/screenshots/creation-canvas.png)

## Why OpenReel Studio

AI video production is more than generating one image or clip. The hard part is keeping the brief, script, references, prompts, provider settings, results, and edit versions consistent—while still being able to redo only what failed.

OpenReel Studio organizes that chain around three principles:

- **Visible deliverables**: text, images, videos, and audio are real canvas nodes rather than hidden task records.
- **Traceable dependencies**: references between characters, scenes, storyboards, and final videos are represented by edges.
- **Local retries**: edit, run, retry, or replace one node without restarting the complete production.

## One workspace, four ways to collaborate

| Surface | Purpose |
| --- | --- |
| Project sessions | Create, switch, select, and manage projects from the collapsible left rail; every project keeps its own chat and canvas. |
| Agent chat | Create, update, run, and review nodes with natural language; resize the chat pane when the workspace needs more room. |
| Creation canvas | Inspect and edit the actual `text`, `image`, `video`, and `audio` deliverables and their dependencies. |
| Workflows and editing | Reuse production methods in the workflow editor and finish picture and sound in the frame-based timeline. |

## From one request to a finished video

1. **Describe the target**: provide the subject, duration, style, aspect ratio, and available material.
2. **Create visible deliverables**: let the agent build script, character, scene, storyboard, video, or audio nodes.
3. **Review and adjust locally**: inspect real previews, prompts, references, and result history, then rerun only the node that needs work.
4. **Reuse the method**: save reliable steps as workflows with inputs, dependencies, collections, conditions, and loops.
5. **Move into the timeline**: drag images, videos, and audio from the media pool onto tracks for arrangement and adjustment.
6. **Export back to the canvas**: rendered output returns as a new final-video node that can continue into downstream work.

## Core capabilities

| Capability | Current implementation |
| --- | --- |
| Node-first creation | User-visible text, image, video, and audio nodes are the source of truth for creative work. |
| Real visual references | Distinguish pixels shown to a prompt model, visual references used by a media model, and direct source-image adoption. |
| Generation and history | Run and retry nodes independently and restore previous results without replacing the latest successful preview on failure. |
| Workflow V2 | Dynamic inputs, `needs`, media `uses`, collection expansion, conditional branches, and bounded feedback loops. |
| Dynamic media settings | Models, aspect ratios, exact pixels, quality, and frame rate travel with the current front-end run instead of polluting reusable specs. |
| Provider flexibility | Configure LLM, image, video, and audio services independently; the Universal Model Adapter submodule handles media requests, polling, and parsing while legacy declarative protocols remain compatible. |
| Frame-based editing | Drag-in media, snapping, track arrangement, trims, splits, joins, real filmstrips, and real audio waveforms. |
| Picture and sound | Position, scale, rotation, opacity, rectangular crop, gain, mute, and fades. |
| Local and desktop runtime | Run from source, deploy with Docker, or install on Windows, Linux, and macOS. |
| Diagnostics | Inspect agent traces, tool results, token/cache usage, and diagnostic panels. |

## Product views

These screenshots come from the current running product and use a dedicated public demo project.

### Reusable workflows

The workflow panel defines production methods: steps, inputs, dependencies, dynamic deliverables, and runtime instances. The creation canvas remains focused on the deliverables users actually inspect and ship.

![The new OpenReel Studio workflow editor](./docs/assets/screenshots/workflow-editor.png)

### Frame-based video timeline

The built-in editor provides a media pool, program monitor, frame-based tracks, real waveforms, clip properties, and export. A timeline export creates a new final-video node on the original canvas.

![The new OpenReel Studio video editor](./docs/assets/screenshots/video-editor.png)

## Who it is for

- Creators combining several AI providers into a repeatable video production system.
- Short-form teams that need traceable character, scene, storyboard, and final-video references.
- Users who want model configuration, workflows, and generated assets on their own machine or server.
- Developers exploring agent orchestration, Workflow V2, and node-based media production.

OpenReel Studio does not include model credits. LLM, image, video, and audio operations require accounts and API keys from the providers you choose.

## Get started

- Desktop builds: download the current package from the [latest release](https://github.com/yutianxiao6/openreel-studio/releases/latest).
- Source install: follow the [English quick start](./docs/en/getting-started.md).
- First session: read the [English user guide](./docs/en/user-guide.md).
- Provider setup: read [Model providers](./docs/en/model-providers.md).

The installer CLI can download the latest package for the current platform:

```bash
npx openreel-studio-installer
```

## Documentation

Start with [docs/README.en.md](./docs/README.en.md) for English documentation or [docs/README.md](./docs/README.md) for Chinese documentation.

| Topic | English | 中文 |
| --- | --- | --- |
| Quick start | [Open](./docs/en/getting-started.md) | [打开](./docs/zh-CN/getting-started.md) |
| User guide | [Open](./docs/en/user-guide.md) | [打开](./docs/zh-CN/user-guide.md) |
| Architecture | [Open](./docs/en/architecture.md) | [打开](./docs/zh-CN/architecture.md) |
| Workflows | [Open](./docs/en/workflows.md) | [打开](./docs/zh-CN/workflows.md) |
| Model providers | [Open](./docs/en/model-providers.md) | [打开](./docs/zh-CN/model-providers.md) |
| Development | [Open](./docs/en/development.md) | [打开](./docs/zh-CN/development.md) |

## Public repository boundary

The repository contains source code, default protocols, built-in skills, workflow templates, and public documentation. Do not commit:

- `.env` files, API keys, access tokens, or private certificates;
- runtime databases, generated assets, traces, or user content from `data/` and `storage/`;
- local provider configuration, private workflows, build output, or temporary screenshots;
- third-party private data or material you do not have permission to redistribute.

Do not paste secrets, complete private configuration, or user data into public issues.

## Project status

OpenReel Studio remains under active development. Workflow contracts, provider adapters, desktop packaging, and editing behavior will continue to evolve. Validate your providers, media formats, and deployment environment before relying on it for production delivery.

## License

[MIT](./LICENSE)
