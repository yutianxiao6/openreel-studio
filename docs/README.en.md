# OpenReel Studio documentation

English · [中文文档](./README.md) · [Project home](../README.en.md)

## First use

1. [Quick start](./en/getting-started.md): install a desktop build, run from source, or use Docker.
2. [User guide](./en/user-guide.md): configure providers, create projects, run nodes, manage history, and edit video.
3. [Workflow guide](./en/workflows.md): use built-in templates or build reusable flows.
4. [Model configuration and protocols](./en/model-providers.md): add LLM/media Providers in Settings, author Catalog protocols, validate, and troubleshoot.

## Developer documentation

- [Architecture](./en/architecture.md): frontend, backend, agents, nodes, workflows, and runtime data.
- [Development and testing](./en/development.md): local commands, quality checks, and contribution boundaries.
- [Desktop packaging](./DESKTOP_PACKAGING.md): Windows, Linux, and macOS builds and releases.
- [Workflow Spec protocol](./workflow-spec-protocol.md): the portable authoring and runtime contract.
- [Agent quality acceptance](./AGENT_QUALITY_ACCEPTANCE.md): real tool use, state consistency, and user experience checks.

## Documentation boundary

Public documentation covers product behavior, installation, stable protocols, and contribution workflows. Temporary implementation plans, migration logs, local diagnostics, and runtime data do not belong in the public navigation. Markdown files under `apps/api/app/skills/` are runtime skill source files rather than end-user tutorials.
