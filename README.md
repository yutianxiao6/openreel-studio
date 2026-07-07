# OpenReel Studio

[![Latest release](https://img.shields.io/github/v/release/yutianxiao6/openreel-studio?label=release)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![Release downloads](https://img.shields.io/github/downloads/yutianxiao6/openreel-studio/total?label=downloads)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![npm installer](https://img.shields.io/npm/v/openreel-studio-installer?label=npm%20installer)](https://www.npmjs.com/package/openreel-studio-installer)
[![License](https://img.shields.io/github/license/yutianxiao6/openreel-studio)](./LICENSE)

English | [中文](#中文)

OpenReel Studio is a chat-driven video creation workspace. It combines an
agentic chat interface, a visual node canvas, reusable workflow templates,
runtime-configurable model providers, local asset storage, and desktop/server
deployment paths.

The current product model is node-first: user-visible creative output lives in
editable `text`, `image`, `video`, and `audio` nodes. Workflows and agents can
plan, transform, and run content, but the canvas nodes remain the primary source
of truth for what the user sees and edits.

New to the codebase? Start here:

- [docs/PROJECT_GUIDE_FOR_BEGINNERS.md](./docs/PROJECT_GUIDE_FOR_BEGINNERS.md)
  explains the project structure and request flow in Chinese.
- [SETUP.md](./SETUP.md) focuses on local startup and server deployment.

## Current Features

- Chat-driven project creation with streaming assistant output and tool events.
- Visual React Flow canvas with editable `text`, `image`, `video`, and `audio`
  nodes.
- Node detail panel for prompts, references, generated output, media metadata,
  reruns, image editing, asset saving, and media history restore.
- Dependency edges between nodes, selection, drag/drop layout, context menu node
  creation, multi-node deletion, minimap, zoom, and alignment guides.
- Workflow template panel for selecting, importing, editing, saving, exporting,
  previewing, materializing, and running workflow specs.
- Workflow run dock on the canvas for adding multiple workflow runs, filling
  per-run inputs, running one step, running next, running all, pausing, deleting,
  and inspecting per-step output.
- Built-in general video production workflow:
  `apps/api/app/skills/general_short_drama_workflow/templates/general_short_drama_workflow.json`.
- User-defined workflow templates in `workflow_templates/`.
- Workflow Build Mode through `/workflow`, with a scoped tool surface for
  creating, patching, saving, and inspecting reusable workflow specs.
- Plan Mode through `/plan`, for read-only analysis before execution.
- Slash commands for project management, reset, doctor diagnostics, workflow
  mode, and help.
- Built-in skills for video production, script writing, character prompts,
  scene prompts, video prompts, storyboard review, story-template workflows, and
  project mentoring.
- User markdown skills under `skills/workflows`, `skills/prompts`, and
  `skills/review`.
- Workflow plugin system under `plugins/`; the tracked example plugin extracts
  keyframes from videos.
- Upload, media, asset-library, model-provider, config, tool, workflow, and
  agent-debug HTTP APIs.
- Local SQLite database, filesystem-backed media storage, trace files, prompt
  dumps, and tool result artifacts.
- Runtime LLM and media provider configuration from the settings UI or
  `config/runtime.jsonc`.
- Web development, Docker server deployment, and Electron/PyInstaller desktop
  packaging paths.

## Product Modes

OpenReel has three user-facing operating modes:

| Mode | How to enter | Purpose |
| --- | --- | --- |
| Default creation mode | Normal chat | Create and modify project content, choose and run workflows, create or run nodes. |
| Plan Mode | `/plan` or `/plan <goal>` | Read-only planning and review. Use `/plan execute` to execute the latest proposed plan. |
| Workflow Build Mode | `/workflow` | Build, patch, inspect, save, and export reusable workflow templates. Use `/workflow exit` to return. |

Useful slash commands:

```text
/help
/doctor
/plan
/plan <goal>
/plan execute
/plan exit
/workflow
/workflow exit
/reset failed
/reset full
/reset confirm
/reset cancel
/project
/project new <title>
/project switch <id|title|index>
/project delete <id|title|index|current>
```

## Architecture

```text
openreel-studio/
├── apps/
│   ├── api/                 # FastAPI backend, agent loop, tools, workflows
│   ├── web/                 # Next.js frontend, chat UI, canvas, workflow UI
│   └── desktop/             # Electron desktop shell
├── packages/
│   ├── shared/              # Shared TypeScript types and constants
│   └── installer/           # npm one-command desktop installer
├── docs/                    # Project guides, workflow protocol, QA docs
├── skills/                  # User markdown skills
├── workflow_templates/      # User workflow templates
├── plugins/                 # Workflow plugin packages
├── data/                    # SQLite, traces, prompt dumps, tool results
├── storage/                 # Uploaded and generated media
├── assets/                  # Local asset library files
├── config/                  # Runtime provider configuration
├── deploy/                  # Server gateway and deployment files
├── packaging/               # PyInstaller API packaging
└── scripts/                 # Init, install, E2E, desktop packaging scripts
```

Runtime request flow:

```text
ChatPanel or Canvas UI
  -> FastAPI route
  -> agent orchestrator or workflow runner
  -> tool registry / services / media providers
  -> SQLite + data/storage/assets/config
  -> SSE or REST response
  -> Zustand stores update chat, canvas, workflow, and settings UI
```

Key backend entry points:

- `apps/api/app/main.py`: FastAPI app and route registration.
- `apps/api/app/api/routes_chat.py`: chat stream, cancellation, queue, history,
  and project event stream.
- `apps/api/app/api/routes_projects.py`: project, nodes, workflow templates,
  workflow runtime, media history, canvas edges, and panel layout.
- `apps/api/app/api/routes_workflow.py`: workflow node types and plugin reload.
- `apps/api/app/agent/orchestrator.py`: main agent loop.
- `apps/api/app/agent/prompt_assembler.py`: prompt assembly.
- `apps/api/app/mcp_tools/registry.py`: core and deferred tool registry.
- `apps/api/app/mcp_tools/node_universal.py`: node create/update/run behavior.
- `apps/api/app/mcp_tools/workflow_tools.py`: workflow template/runtime tools.
- `apps/api/app/services/workflow_plugins.py`: workflow plugin loading and
  execution.

Key frontend entry points:

- `apps/web/app/projects/[projectId]/page.tsx`: project workspace shell.
- `apps/web/components/chat/ChatPanel.tsx`: chat, SSE, slash command UI.
- `apps/web/components/workspace/WorkspaceViewTabs.tsx`: canvas/workflow switch.
- `apps/web/components/canvas/WorkflowCanvas.tsx`: React Flow canvas, workflow
  dock, workflow panel, context menus, media history, and canvas interactions.
- `apps/web/components/canvas/SmartNode.tsx`: node card rendering.
- `apps/web/components/canvas/NodeDetailPanel.tsx`: node detail and editing.
- `apps/web/components/settings/SettingsModal.tsx`: model, media, agent, debug,
  and raw config tabs.
- `apps/web/stores/*.ts`: canvas, chat, project, blueprint compatibility, and
  view-mode state.
- `apps/web/lib/api.ts`: frontend API and event helpers.

## Workflows, Skills, and Plugins

These three concepts are related but separate:

| Concept | Purpose | Location |
| --- | --- | --- |
| Skill | Teaches the model a workflow method, prompt style, review checklist, or project rule. | `apps/api/app/skills/`, `skills/` |
| Workflow template | Describes reusable inputs, steps, dependencies, loops, visible outputs, and runtime settings. | `apps/api/app/skills/*/templates/`, `workflow_templates/` |
| Plugin | Adds executable workflow node types such as keyframe extraction. | `plugins/` |

The workflow authoring schema is `openreel.workflow.authoring.v1`; the runtime
workflow schema is `openreel.workflow.v1`. Workflow Build Mode writes reusable
specs, validates them through the workflow spec writer, and inspects the canvas
projection before reporting readiness.

Important workflow docs:

- [docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md](./docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md)
- [docs/workflow-spec-protocol.md](./docs/workflow-spec-protocol.md)
- [docs/workflow-build-codex-style-design.md](./docs/workflow-build-codex-style-design.md)
- [WORKFLOW_SPEC_PROTOCOL.md](./WORKFLOW_SPEC_PROTOCOL.md)
- [WORKFLOW_EDITOR_DESIGN.md](./WORKFLOW_EDITOR_DESIGN.md)

## API Surface

Common HTTP routes:

| Prefix | Purpose |
| --- | --- |
| `/api/chat` | Streaming chat, cancellation, queued messages, history, project events. |
| `/api/projects` | Projects, project state, messages, nodes, edges, workflow templates, workflow runtime, media history. |
| `/api/nodes` | Node listing helpers. |
| `/api/workflow` | Workflow node types and plugin listing/reload. |
| `/api/uploads` | Project file uploads and uploaded file serving. |
| `/api/assets` | Asset library listing and preview. |
| `/api/media` | Generated media file serving. |
| `/api/models` | Model config and provider availability. |
| `/api/tools` | Direct tool calls, config editing, MCP server status. |
| `/api/agent/debug` | Doctor snapshot, workflow evidence, traces, token usage, artifacts. |

The default agent loop exposes a stable core tool surface and discovers lower
frequency capabilities through `tool.search`, `tool.describe`, and
`tool.execute`. Workflow Build Mode uses a smaller workflow-specific tool
surface.

## Requirements

- Node.js 20 or later
- pnpm 9 or later
- Python 3.11 or later
- uv
- Git
- Docker and Docker Compose for container deployment

Desktop packages should be built on the target operating system because
PyInstaller and Electron artifacts are platform-specific.

## Quick Start

```bash
git clone https://github.com/yutianxiao6/openreel-studio.git
cd openreel-studio
cp .env.example .env
bash install.sh
```

Start the API:

```bash
pnpm api:dev
```

Start the web app in another terminal:

```bash
pnpm dev
```

Open:

```text
http://localhost:3000
```

During local development, the web app proxies `/api/*` requests to
`http://localhost:8000`.

Manual dependency setup, if you do not use `install.sh`:

```bash
pnpm install
cd apps/api && uv sync && cd ../..
pnpm api:init-db
```

## Docker

Local Docker development:

```bash
docker compose up -d --build api web
```

Production-style compose startup:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build api web gateway
```

Restart without rebuilding:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d api web gateway
```

The production compose file mounts these local directories into the API
container:

- `data/`
- `storage/`
- `assets/`
- `config/`
- `plugins/`
- `skills/`
- `workflow_templates/`

For one-command Linux server setup:

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install-server.sh | bash
```

The production web build is configured for `/studio` through
`NEXT_PUBLIC_BASE_PATH` and `NEXT_PUBLIC_API_BASE_URL` in
`docker-compose.prod.yml`.

## Configuration

Runtime provider settings are stored in `config/runtime.jsonc`. The file is
ignored by Git. You can configure LLM and media providers from the settings UI
or edit the JSONC file directly.

`config/runtime.example.jsonc` is a no-secret reference file. Real secrets
should live in `.env.local`, `.env.production`, or shell environment variables.
The API can auto-generate `config/runtime.jsonc` on first startup from available
`*_API_KEY` environment variables.

Common provider environment variables:

```env
DEEPSEEK_API_KEY=<your-api-key>
OPENAI_API_KEY=<your-api-key>
ANTHROPIC_API_KEY=<your-api-key>
DASHSCOPE_API_KEY=<your-api-key>
GEMINI_API_KEY=<your-api-key>
```

## Runtime Data

OpenReel is local-first by default. Runtime data is stored under the repository
root unless configured otherwise:

```text
data/app.db                  # SQLite database
data/agent_traces/           # Agent JSONL traces
data/tool_results/           # Large tool results
data/prompt_dumps/           # Prompt dumps for debugging
data/logs/                   # Runtime logs
storage/                     # Generated and uploaded media
assets/                      # Local asset library
config/runtime.jsonc         # Provider config
workflow_templates/          # User workflow templates
skills/                      # User markdown skills
plugins/                     # Workflow plugins
```

Do not commit local runtime data, generated media, provider secrets, prompt
dumps, traces, or deployment certificates.

## Desktop Packaging

Desktop packaging combines:

- Electron shell from `apps/desktop`.
- Next.js standalone web output.
- PyInstaller-built API binary from `packaging/pyinstaller/openreel-api.spec`.

Build commands:

```bash
pnpm desktop:package:win
pnpm desktop:package:linux
pnpm desktop:package:mac
```

Windows also has:

```bat
package-windows.bat
```

Desktop artifacts are written to:

```text
dist/installers/
```

See [docs/DESKTOP_PACKAGING.md](./docs/DESKTOP_PACKAGING.md) for packaging
details.

## Installation Packages

Download the latest desktop build:

```text
https://github.com/yutianxiao6/openreel-studio/releases/latest
```

Release artifacts:

- Windows: `OpenReel.Studio-Setup-*.exe`
- Linux desktop: `OpenReel.Studio-*.AppImage` or `OpenReel.Studio-*.deb`
- macOS: `OpenReel.Studio-*.dmg`

One-command desktop download:

```powershell
irm https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.ps1 | iex
```

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.sh | bash
```

npm installer:

```bash
npx openreel-studio-installer
```

Set `OPENREEL_DOWNLOAD_DIR` to choose the download directory. Set
`OPENREEL_NO_RUN=1` on Windows or `OPENREEL_NO_OPEN=1` on macOS to download
without opening the installer.

## Development Commands

```bash
pnpm dev                 # Start Next.js web dev server
pnpm api:dev             # Start FastAPI dev server
pnpm api:init-db         # Initialize local SQLite database
pnpm -r typecheck        # Type-check workspace packages
pnpm --filter web build  # Build the web app
pnpm --filter web lint   # Run web lint script
```

API tests:

```bash
cd apps/api
PYTHONPATH=. uv run pytest -q
```

Patch formatting:

```bash
git diff --check
```

Agent live/E2E validation should follow
[docs/AGENT_QUALITY_ACCEPTANCE.md](./docs/AGENT_QUALITY_ACCEPTANCE.md).

## Debugging

Use these sources together when investigating behavior:

- `/doctor` in chat for project-level diagnostics.
- Settings -> Agent Debug for doctor snapshots, traces, artifacts, and token
  usage.
- `data/agent_traces/` for model/tool decision traces.
- `data/tool_results/` for large tool outputs.
- Browser Network tab for `/api/chat/stream` and project event streams.
- Backend logs for media provider, workflow, and node-run failures.
- Node detail panel for the actual node input, output, status, stages, and
  error.

## Release Automation

GitHub Actions builds desktop artifacts when a version tag is pushed:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The npm installer package is published from `packages/installer`:

```bash
cd packages/installer
npm publish --access public
```

Every npm release requires a unique package version.

## Documentation

- [docs/PROJECT_GUIDE_FOR_BEGINNERS.md](./docs/PROJECT_GUIDE_FOR_BEGINNERS.md)
- [SETUP.md](./SETUP.md)
- [docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md](./docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md)
- [WORKFLOW_SPEC_PROTOCOL.md](./WORKFLOW_SPEC_PROTOCOL.md)
- [docs/workflow-spec-protocol.md](./docs/workflow-spec-protocol.md)
- [docs/workflow-build-codex-style-design.md](./docs/workflow-build-codex-style-design.md)
- [docs/AGENT_QUALITY_ACCEPTANCE.md](./docs/AGENT_QUALITY_ACCEPTANCE.md)
- [docs/DESKTOP_PACKAGING.md](./docs/DESKTOP_PACKAGING.md)

## Security

- Keep `.env.local`, `.env.production`, and `config/runtime.jsonc` out of Git.
- Do not commit generated `data/`, `storage/`, `.next/`, `.venv/`, or
  `node_modules/`.
- Do not commit deployment certificates, prompt dumps, tool results, traces, or
  generated media.
- Rotate any provider key that has ever been committed to a public repository.

## License

MIT. See [LICENSE](./LICENSE).

---

# 中文

OpenReel Studio 是一个聊天式视频智能创作工作台。它把 Agent 聊天、可视化节点画布、可复用工作流模板、运行时模型 provider 配置、本地资产存储，以及 Web/服务器/桌面端部署整合到一个项目里。

当前产品模型是 node-first：用户真正看见和编辑的创作结果都落在 `text`、`image`、`video`、`audio` 节点上。Agent 和 workflow 可以规划、转换和执行内容，但画布节点才是用户可见结果的主要真相源。

第一次看项目，建议先读：

- [docs/PROJECT_GUIDE_FOR_BEGINNERS.md](./docs/PROJECT_GUIDE_FOR_BEGINNERS.md)：中文新手项目地图，解释目录结构和请求流向。
- [SETUP.md](./SETUP.md)：本地启动和服务器部署。

## 当前功能

- 聊天式项目创作，支持流式 assistant 输出和工具事件。
- React Flow 可视化画布，支持可编辑 `text`、`image`、`video`、`audio` 节点。
- 节点详情栏支持提示词、引用、生成结果、媒体元信息、重新运行、图片编辑、保存到资产库和媒体历史恢复。
- 节点依赖线、节点选择、拖拽排布、右键创建节点、框选删除、缩放、minimap 和对齐参考线。
- 流程面板支持选择、导入、编辑、保存、导出、预览、物化和运行 workflow spec。
- 画布运行流程栏支持添加多个流程实例、填写每个实例自己的输入、运行单步、运行下一步、一键运行、暂停、删除和查看步骤输出。
- 内置通用视频制作工作流：
  `apps/api/app/skills/general_short_drama_workflow/templates/general_short_drama_workflow.json`。
- 用户自定义工作流模板目录：`workflow_templates/`。
- 独立工作流搭建模式 `/workflow`，用于搭建、修补、保存和检查可复用 workflow spec。
- 只读计划模式 `/plan`，用于执行前分析和审查。
- slash commands 支持项目管理、reset、doctor 诊断、工作流模式和帮助。
- 内置视频制作、剧本、人物提示词、场景提示词、视频提示词、分镜审查、故事模板图和项目导师 skill。
- 用户 markdown skill 目录：`skills/workflows`、`skills/prompts`、`skills/review`。
- workflow plugin 系统，目录是 `plugins/`；当前跟踪的示例插件是视频关键帧提取。
- 上传、媒体、资产库、模型 provider、配置、工具、workflow 和 Agent debug API。
- 本地 SQLite 数据库、本地媒体文件、Agent trace、prompt dump 和 tool result artifact。
- 设置界面或 `config/runtime.jsonc` 支持运行时配置 LLM 和媒体 provider。
- 支持 Web 开发、Docker 服务器部署和 Electron/PyInstaller 桌面端打包。

## 产品模式

| 模式 | 进入方式 | 用途 |
| --- | --- | --- |
| 默认制作模式 | 普通聊天 | 创建和修改项目内容，选择和运行工作流，创建或运行节点。 |
| Plan Mode | `/plan` 或 `/plan <目标>` | 只读计划和审查；用 `/plan execute` 执行最近计划。 |
| Workflow Build Mode | `/workflow` | 搭建、修改、检查、保存和导出可复用工作流；用 `/workflow exit` 退出。 |

常用 slash commands：

```text
/help
/doctor
/plan
/plan <目标>
/plan execute
/plan exit
/workflow
/workflow exit
/reset failed
/reset full
/reset confirm
/reset cancel
/project
/project new <标题>
/project switch <id|标题|序号>
/project delete <id|标题|序号|current>
```

## 架构

```text
openreel-studio/
├── apps/
│   ├── api/                 # FastAPI 后端、Agent loop、工具、workflow
│   ├── web/                 # Next.js 前端、聊天、画布、流程界面
│   └── desktop/             # Electron 桌面壳
├── packages/
│   ├── shared/              # 前端共享 TypeScript 类型和常量
│   └── installer/           # npm 一键桌面安装器
├── docs/                    # 项目指南、workflow 协议、验收文档
├── skills/                  # 用户 markdown skill
├── workflow_templates/      # 用户工作流模板
├── plugins/                 # workflow 插件包
├── data/                    # SQLite、trace、prompt dump、tool result
├── storage/                 # 上传和生成的媒体
├── assets/                  # 本地资产库文件
├── config/                  # 运行时 provider 配置
├── deploy/                  # 服务器网关和部署文件
├── packaging/               # PyInstaller API 打包配置
└── scripts/                 # 初始化、安装、E2E、桌面打包脚本
```

运行时请求流：

```text
ChatPanel 或 Canvas UI
  -> FastAPI route
  -> agent orchestrator 或 workflow runner
  -> tool registry / services / media providers
  -> SQLite + data/storage/assets/config
  -> SSE 或 REST response
  -> Zustand stores 更新聊天、画布、流程和设置界面
```

后端关键入口：

- `apps/api/app/main.py`：FastAPI app 和路由注册。
- `apps/api/app/api/routes_chat.py`：chat stream、取消、队列、历史和项目事件流。
- `apps/api/app/api/routes_projects.py`：项目、节点、workflow 模板、workflow runtime、媒体历史、画布边和面板布局。
- `apps/api/app/api/routes_workflow.py`：workflow 节点类型和插件刷新。
- `apps/api/app/agent/orchestrator.py`：主 Agent loop。
- `apps/api/app/agent/prompt_assembler.py`：prompt 组装。
- `apps/api/app/mcp_tools/registry.py`：核心工具和 deferred 工具注册表。
- `apps/api/app/mcp_tools/node_universal.py`：节点创建、更新和运行。
- `apps/api/app/mcp_tools/workflow_tools.py`：workflow 模板和运行工具。
- `apps/api/app/services/workflow_plugins.py`：workflow 插件加载和执行。

前端关键入口：

- `apps/web/app/projects/[projectId]/page.tsx`：项目工作台外壳。
- `apps/web/components/chat/ChatPanel.tsx`：聊天、SSE 和 slash command UI。
- `apps/web/components/workspace/WorkspaceViewTabs.tsx`：创作画布和流程面板切换。
- `apps/web/components/canvas/WorkflowCanvas.tsx`：React Flow 画布、流程栏、流程面板、右键菜单、媒体历史和画布交互。
- `apps/web/components/canvas/SmartNode.tsx`：节点卡片显示。
- `apps/web/components/canvas/NodeDetailPanel.tsx`：节点详情和编辑。
- `apps/web/components/settings/SettingsModal.tsx`：模型、媒体、Agent、debug 和原始配置 tabs。
- `apps/web/stores/*.ts`：画布、聊天、项目、蓝图兼容和视图模式状态。
- `apps/web/lib/api.ts`：前端 API 和事件 helper。

## Workflow、Skill 和 Plugin

| 概念 | 作用 | 位置 |
| --- | --- | --- |
| Skill | 教模型流程方法、提示词写法、审查清单或项目规则。 | `apps/api/app/skills/`、`skills/` |
| Workflow template | 描述可复用输入、步骤、依赖、循环、可见产物和运行设置。 | `apps/api/app/skills/*/templates/`、`workflow_templates/` |
| Plugin | 增加可执行 workflow 节点类型，例如提取关键帧。 | `plugins/` |

工作流作者协议是 `openreel.workflow.authoring.v1`，运行时协议是
`openreel.workflow.v1`。Workflow Build Mode 会写入可复用 spec，通过写入工具校验协议，并用画布投影检查最终用户能看到的节点和依赖。

重要 workflow 文档：

- [docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md](./docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md)
- [docs/workflow-spec-protocol.md](./docs/workflow-spec-protocol.md)
- [docs/workflow-build-codex-style-design.md](./docs/workflow-build-codex-style-design.md)
- [WORKFLOW_SPEC_PROTOCOL.md](./WORKFLOW_SPEC_PROTOCOL.md)
- [WORKFLOW_EDITOR_DESIGN.md](./WORKFLOW_EDITOR_DESIGN.md)

## API 面

| 前缀 | 用途 |
| --- | --- |
| `/api/chat` | 流式聊天、取消、排队消息、历史、项目事件。 |
| `/api/projects` | 项目、项目状态、消息、节点、边、workflow 模板、workflow runtime、媒体历史。 |
| `/api/nodes` | 节点列表 helper。 |
| `/api/workflow` | workflow 节点类型和插件列表/重载。 |
| `/api/uploads` | 项目文件上传和上传文件读取。 |
| `/api/assets` | 资产库列表和预览。 |
| `/api/media` | 生成媒体文件读取。 |
| `/api/models` | 模型配置和 provider 可用性。 |
| `/api/tools` | 直接工具调用、配置编辑、MCP server 状态。 |
| `/api/agent/debug` | doctor、workflow evidence、trace、token usage、artifact。 |

默认 Agent Loop 暴露稳定核心工具，并通过 `tool.search`、`tool.describe`、`tool.execute` 发现低频能力。Workflow Build Mode 使用更小的工作流专用工具面。

## 环境要求

- Node.js 20 或更高版本
- pnpm 9 或更高版本
- Python 3.11 或更高版本
- uv
- Git
- Docker 和 Docker Compose，用于容器部署

桌面包应在目标操作系统构建，因为 PyInstaller 和 Electron 产物都和平台相关。

## 快速开始

```bash
git clone https://github.com/yutianxiao6/openreel-studio.git
cd openreel-studio
cp .env.example .env
bash install.sh
```

启动 API：

```bash
pnpm api:dev
```

另开终端启动 Web：

```bash
pnpm dev
```

访问：

```text
http://localhost:3000
```

本地开发时，Web 会将 `/api/*` 请求代理到 `http://localhost:8000`。

不使用 `install.sh` 时，也可以手动安装：

```bash
pnpm install
cd apps/api && uv sync && cd ../..
pnpm api:init-db
```

## Docker

本地 Docker 开发：

```bash
docker compose up -d --build api web
```

生产 compose 启动：

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build api web gateway
```

不重新构建直接重启：

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d api web gateway
```

生产 compose 会挂载这些本地目录：

- `data/`
- `storage/`
- `assets/`
- `config/`
- `plugins/`
- `skills/`
- `workflow_templates/`

Linux 服务器一键安装：

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install-server.sh | bash
```

生产 Web 构建在 `docker-compose.prod.yml` 中通过 `NEXT_PUBLIC_BASE_PATH` 和
`NEXT_PUBLIC_API_BASE_URL` 配置为 `/studio`。

## 配置

运行时 provider 配置存放在 `config/runtime.jsonc`，该文件被 Git 忽略。可以在设置界面配置 LLM 和媒体 provider，也可以直接编辑 JSONC。

`config/runtime.example.jsonc` 是无密钥参考文件。真实密钥应放在 `.env.local`、`.env.production` 或系统环境变量中。API 首次启动时可以根据已有的 `*_API_KEY` 环境变量自动生成 `config/runtime.jsonc`。

常用 provider 环境变量：

```env
DEEPSEEK_API_KEY=<your-api-key>
OPENAI_API_KEY=<your-api-key>
ANTHROPIC_API_KEY=<your-api-key>
DASHSCOPE_API_KEY=<your-api-key>
GEMINI_API_KEY=<your-api-key>
```

## 运行时数据

OpenReel 默认本地优先。运行数据默认保存在仓库根目录：

```text
data/app.db                  # SQLite 数据库
data/agent_traces/           # Agent JSONL traces
data/tool_results/           # 大工具结果
data/prompt_dumps/           # prompt dump
data/logs/                   # 运行日志
storage/                     # 生成和上传的媒体
assets/                      # 本地资产库
config/runtime.jsonc         # provider 配置
workflow_templates/          # 用户工作流模板
skills/                      # 用户 markdown skills
plugins/                     # workflow 插件
```

不要提交本地运行数据、生成媒体、provider 密钥、prompt dump、trace 或部署证书。

## 桌面端打包

桌面端打包由三部分组成：

- `apps/desktop` 的 Electron shell。
- Next.js standalone Web 输出。
- `packaging/pyinstaller/openreel-api.spec` 构建出的 API 二进制。

构建命令：

```bash
pnpm desktop:package:win
pnpm desktop:package:linux
pnpm desktop:package:mac
```

Windows 也可以使用：

```bat
package-windows.bat
```

桌面端产物输出到：

```text
dist/installers/
```

打包细节见 [docs/DESKTOP_PACKAGING.md](./docs/DESKTOP_PACKAGING.md)。

## 安装包

下载最新版桌面包：

```text
https://github.com/yutianxiao6/openreel-studio/releases/latest
```

Release 产物：

- Windows：`OpenReel.Studio-Setup-*.exe`
- Linux 桌面版：`OpenReel.Studio-*.AppImage` 或 `OpenReel.Studio-*.deb`
- macOS：`OpenReel.Studio-*.dmg`

一条命令下载桌面包：

```powershell
irm https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.ps1 | iex
```

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.sh | bash
```

npm installer：

```bash
npx openreel-studio-installer
```

通过 `OPENREEL_DOWNLOAD_DIR` 可以指定下载目录。Windows 设置
`OPENREEL_NO_RUN=1`、macOS 设置 `OPENREEL_NO_OPEN=1` 时只下载安装包，不自动打开。

## 开发命令

```bash
pnpm dev                 # 启动 Next.js Web 开发服务
pnpm api:dev             # 启动 FastAPI 开发服务
pnpm api:init-db         # 初始化本地 SQLite 数据库
pnpm -r typecheck        # 检查 workspace TypeScript 类型
pnpm --filter web build  # 构建 Web 应用
pnpm --filter web lint   # 运行 Web lint 脚本
```

API 测试：

```bash
cd apps/api
PYTHONPATH=. uv run pytest -q
```

补丁空白检查：

```bash
git diff --check
```

Agent live/E2E 验收应遵循
[docs/AGENT_QUALITY_ACCEPTANCE.md](./docs/AGENT_QUALITY_ACCEPTANCE.md)。

## 调试

排查问题时建议同时看这些来源：

- 聊天里运行 `/doctor` 查看项目诊断。
- 设置里的 Agent Debug 查看 doctor、trace、artifact 和 token usage。
- `data/agent_traces/` 查看模型和工具决策 trace。
- `data/tool_results/` 查看大工具结果。
- 浏览器 Network 查看 `/api/chat/stream` 和项目事件流。
- 后端日志查看媒体 provider、workflow 和 node-run 失败。
- 节点详情栏查看真实 input、output、status、stages 和 error。

## 自动发布

推送版本 tag 后，GitHub Actions 会构建桌面端产物：

```bash
git tag v0.1.0
git push origin v0.1.0
```

npm installer 包从 `packages/installer` 发布：

```bash
cd packages/installer
npm publish --access public
```

每次发布 npm 新版本前都必须更新 package 版本号。

## 文档

- [docs/PROJECT_GUIDE_FOR_BEGINNERS.md](./docs/PROJECT_GUIDE_FOR_BEGINNERS.md)
- [SETUP.md](./SETUP.md)
- [docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md](./docs/WORKFLOW_MANUAL_BUILD_TUTORIAL.md)
- [WORKFLOW_SPEC_PROTOCOL.md](./WORKFLOW_SPEC_PROTOCOL.md)
- [docs/workflow-spec-protocol.md](./docs/workflow-spec-protocol.md)
- [docs/workflow-build-codex-style-design.md](./docs/workflow-build-codex-style-design.md)
- [docs/AGENT_QUALITY_ACCEPTANCE.md](./docs/AGENT_QUALITY_ACCEPTANCE.md)
- [docs/DESKTOP_PACKAGING.md](./docs/DESKTOP_PACKAGING.md)

## 安全说明

- 不要提交 `.env.local`、`.env.production` 和 `config/runtime.jsonc`。
- 不要提交生成的 `data/`、`storage/`、`.next/`、`.venv/` 或 `node_modules/`。
- 不要提交部署证书、prompt dump、tool result、trace 或生成媒体。
- 如果任何 provider key 曾经进入公开仓库，应立即轮换。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
