# OpenReel Studio

English · [简体中文](./README.md)

[![Latest release](https://img.shields.io/github/v/release/yutianxiao6/openreel-studio?label=release)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![Release downloads](https://img.shields.io/github/downloads/yutianxiao6/openreel-studio/total?label=downloads)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![npm installer](https://img.shields.io/npm/v/openreel-studio-installer?label=npm%20installer)](https://www.npmjs.com/package/openreel-studio-installer)
[![License](https://img.shields.io/github/license/yutianxiao6/openreel-studio)](./LICENSE)

**Turn a creative request into a visible, editable, reusable, and cuttable video production flow.**

OpenReel Studio is an open-source, conversational workspace for AI video production. You can develop an idea with an agent or work directly with text, image, video, and audio nodes. Inputs, references, prompts, and generated results remain visible on the canvas, and finished clips can move into the built-in editor for essential cutting and delivery.

![OpenReel Studio creation canvas](./docs/assets/screenshots/creation-canvas.png)

## What it solves

AI video production often scatters scripts, references, prompts, and outputs across unrelated tools. OpenReel Studio keeps the process in one traceable workspace:

- The agent interprets requests, plans work, and calls models while outputs remain editable.
- Text, images, videos, and audio are first-class visible nodes connected by references.
- A failed step can be retried or replaced without restarting the whole production.
- A reliable process can be saved as a workflow template and reused across projects.
- Generated clips can move directly into a frame-based timeline for basic editing.

## From idea to delivery

1. **Describe the target**: provide the subject, duration, aspect ratio, style, and available material.
2. **Build the source material**: create or import scripts, characters, scenes, storyboards, and references.
3. **Generate media**: run image, video, and audio providers one node at a time.
4. **Review and select**: inspect generation history and choose which result should feed downstream work.
5. **Edit and export**: trim, join, arrange, adjust, and export the finished sequence.

## Highlights

| Capability | What it provides |
| --- | --- |
| Conversational creation | Create, update, run, and review production nodes with natural language. |
| Node canvas | Manage scripts, prompts, references, storyboards, videos, and audio together. |
| Visual references | Send pixels to vision tasks when the model must inspect an image, while keeping generation-only references separate. |
| Generation history | Preserve image and video candidates; a failed attempt does not replace the last successful preview. |
| Workflow editor | Build reusable flows with inputs, dependencies, collections, loops, and runtime instances. |
| Provider flexibility | Configure LLM, image, video, and audio services independently through declarative media protocols. |
| Essential video editing | Frame-based trimming, splitting, joining, multitrack arrangement, real filmstrips, real waveforms, and export. |
| Picture and sound controls | Position, scale, rotation, opacity, rectangular crop, gain, mute, and fades. |
| Local and desktop runtime | Run from source, deploy with Docker, or install on Windows, Linux, and macOS. |
| Diagnostics | Inspect traces, tool results, token/cache usage, and agent diagnostics. |

## Product views

The following images were captured from the current running product.

### Reusable workflows

The workflow panel defines reusable process structure. The creation canvas remains focused on user-facing text, image, video, and audio deliverables.

![OpenReel Studio workflow editor](./docs/assets/screenshots/workflow-editor.png)

### Frame-based video editor

The built-in editor focuses on rough cutting and delivery: real frame thumbnails, real audio waveforms, trims, splits, joins, tracks, levels, visual transforms, and export.

![OpenReel Studio video editor](./docs/assets/screenshots/video-editor.png)

## Who it is for

- Creators combining several AI providers into repeatable video workflows.
- Short-form teams that need traceable character, scene, and storyboard references.
- Users who want model configuration and generated assets on their own machine or server.
- Developers exploring agents, workflow protocols, and node-based media production.

OpenReel Studio does not include model credits. Image, video, audio, and LLM operations require accounts and API keys from the providers you choose.

## Get started

- Install a desktop build from the [latest release](https://github.com/yutianxiao6/openreel-studio/releases/latest).
- Run from source with the [English quick start](./docs/en/getting-started.md).
- Learn the workspace with the [English user guide](./docs/en/user-guide.md).
- Explore the codebase in the [English architecture guide](./docs/en/architecture.md).

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

OpenReel Studio is under active development. Workflow contracts, desktop packaging, and editing behavior will continue to evolve. Validate your providers, media formats, and deployment environment before relying on it for production delivery.

## License

[MIT](./LICENSE)
