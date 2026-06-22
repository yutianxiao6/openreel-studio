# OpenReel Studio

[![Latest release](https://img.shields.io/github/v/release/yutianxiao6/openreel-studio?label=release)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![Release downloads](https://img.shields.io/github/downloads/yutianxiao6/openreel-studio/total?label=downloads)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![npm installer](https://img.shields.io/npm/v/openreel-studio-installer?label=npm%20installer)](https://www.npmjs.com/package/openreel-studio-installer)
[![License](https://img.shields.io/github/license/yutianxiao6/openreel-studio)](./LICENSE)

English | [中文](#中文)

OpenReel Studio is a chat-driven creative production workspace for planning, generating, revising, and reviewing video projects. It combines a conversational agent, a node-first canvas, configurable media providers, and desktop packaging support for Windows, Linux, and macOS.

The project is under active development. Public interfaces, provider integrations, and packaging behavior may change between releases.

## Capabilities

- Convert natural-language creation requests into editable `text`, `image`, `video`, and `audio` nodes.
- Use the React Flow canvas as the visible source of truth for creative work.
- Reuse and revise existing canvas nodes before creating duplicates.
- Locate existing nodes with fuzzy or regex search, then read exact node details for safe updates.
- Manage project state, tasks, nodes, media outputs, and agent traces through a local FastAPI backend.
- Configure LLM, image, video, and audio providers at runtime.
- Package desktop builds for Windows, Linux, and macOS with Electron, Next.js standalone output, FastAPI, and PyInstaller.
- Store project data locally by default with SQLite and filesystem-backed media storage.

## Architecture

```text
openreel-studio/
├── apps/
│   ├── api/       # FastAPI backend, agent loop, tools, media runners
│   ├── web/       # Next.js frontend, chat UI, React Flow canvas
│   └── desktop/   # Electron desktop shell
├── packages/
│   └── shared/    # Shared TypeScript types/constants
├── packaging/     # PyInstaller packaging configuration
├── scripts/
│   └── desktop/   # Desktop packaging scripts
└── docs/          # Packaging and operations documentation
```

## Technology Stack

| Layer | Stack |
| --- | --- |
| Frontend | Next.js, React, TypeScript, React Flow, Tailwind CSS, Zustand |
| Backend | FastAPI, SQLite, SQLModel, LiteLLM, MCP-style tool registry |
| Desktop | Electron, PyInstaller, Next.js standalone output |
| Runtime | Local filesystem storage, SSE streaming, provider-based media generation |

## Agent Workflow

OpenReel Studio uses a node-first agent model. The canvas is the source of truth, and all creative state is represented by user-visible nodes.

- `text` nodes store briefs, scripts, structure notes, reviews, and prompt notes.
- `image` nodes store visual references, characters, scenes, storyboards, first/last frames, and generated images.
- `video` nodes store video prompts, references, status, generated outputs, and generation history.
- `audio` nodes store audio prompts and outputs such as speech, music, or sound design.

When the agent needs to find existing work, it first narrows candidates with `node.list(query|regex)`. If nothing matches, it falls back to `node.list(limit=0)` to inspect the full index, then reads exact details with `node.get(node_ids=[...])`. This keeps edits tied to real node IDs while avoiding unnecessary full-canvas reads.

## Requirements

- Node.js 20 or later
- pnpm 9 or later
- Python 3.11 or later
- uv
- Git

Desktop packages should be built on the target operating system. PyInstaller produces platform-specific API binaries, so Windows, Linux, and macOS artifacts are built separately.

## Quick Start

```bash
git clone https://github.com/yutianxiao6/openreel-studio.git
cd openreel-studio
cp .env.example .env
pnpm install
cd apps/api && uv sync && cd ../..
pnpm api:init-db
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

During local development, the web app proxies `/api/*` requests to `http://localhost:8000`.

## Configuration

Runtime provider settings are stored in `config/runtime.jsonc`. This file is intentionally ignored by Git. Configure model providers from the settings UI, or create the file locally and reference secrets through environment variables.

Do not commit real API keys. Store secrets in `.env.local`, `.env.production`, or your shell environment.

Example:

```env
DEEPSEEK_API_KEY=<your-api-key>
OPENAI_API_KEY=<your-api-key>
```

## Installation

### Desktop Release

Download the latest desktop build from:

```text
https://github.com/yutianxiao6/openreel-studio/releases/latest
```

Release artifacts:

- Windows: `OpenReel.Studio-Setup-*.exe`
- Linux desktop: `OpenReel.Studio-*.AppImage` or `OpenReel.Studio-*.deb`
- macOS: `OpenReel.Studio-*.dmg`

### One-command Desktop Download

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.ps1 | iex
```

Linux/macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.sh | bash
```

npm installer:

```bash
npx openreel-studio-installer
```

Set `OPENREEL_DOWNLOAD_DIR` to choose the download directory. Set `OPENREEL_NO_RUN=1` on Windows or `OPENREEL_NO_OPEN=1` on macOS to download the installer without opening it.

### Linux Server Deployment

Linux desktop users should use the AppImage or `.deb` release artifact. Linux server deployments should use the Docker stack:

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install-server.sh | bash
```

The server installer clones or updates the repository under `~/openreel-studio`, creates `.env.production` when needed, prompts for Basic Auth credentials, and starts:

- `api`: FastAPI backend, not exposed directly to the public network.
- `web`: Next.js standalone frontend.
- `gateway`: Caddy HTTPS reverse proxy with Basic Auth on `${APP_PUBLIC_PORT:-3100}`.

Open:

```text
https://<server-ip>:3100
```

Caddy uses an internal TLS certificate by default, so browsers may show a certificate warning on first visit. On first API startup, `config/runtime.jsonc` is generated automatically from available `*_API_KEY` environment variables. `config/runtime.example.jsonc` is a no-secret template for manual editing. Keep `config/runtime.jsonc`, `.env.production`, `data/`, and `storage/` private; they are ignored by Git.

## Desktop Packaging

Windows:

```bat
package-windows.bat
```

```powershell
pnpm desktop:package:win
```

Linux:

```bash
pnpm desktop:package:linux
```

macOS:

```bash
pnpm desktop:package:mac
```

Desktop artifacts are written to:

```text
dist/installers/
```

See [docs/DESKTOP_PACKAGING.md](./docs/DESKTOP_PACKAGING.md) for packaging details.

## Release Automation

GitHub Actions builds desktop artifacts when a version tag is pushed:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The release workflow builds Windows, Linux, and macOS artifacts and uploads them to the matching GitHub Release. Install scripts always resolve the latest Release.

The npm installer package is published from `packages/installer`:

```bash
cd packages/installer
npm publish --access public
```

For CI publishing, add an automation or granular npm publish token as `NPM_TOKEN`, or configure Trusted Publishing on npmjs.com:

- Owner: `yutianxiao6`
- Repository: `openreel-studio`
- Workflow filename: `npm-publish.yml`
- Allowed action: `npm publish`

Every npm release requires a unique package version.

## Development Commands

```bash
pnpm dev                 # Start Next.js web dev server
pnpm api:dev             # Start FastAPI dev server
pnpm api:init-db         # Initialize local SQLite database
pnpm -r typecheck        # Type-check workspace packages
pnpm --filter web build  # Build the web app
```

API tests:

```bash
cd apps/api
PYTHONPATH=. uv run pytest -q
```

## Repository Scope

This repository contains the core OpenReel Studio application, server deployment files, and desktop packaging path.

It does not include local runtime data, generated media, deployment certificates, private provider configuration, or separately maintained bot/plugin repositories.

## Security

- Keep `.env.local`, `.env.production`, and `config/runtime.jsonc` out of Git.
- Do not commit generated `data/`, `storage/`, `.next/`, `.venv/`, or `node_modules/`.
- Rotate any provider key that has ever been committed to a public repository.

## License

MIT. See [LICENSE](./LICENSE).

---

# 中文

OpenReel Studio 是一个聊天式创意生产工作台，用于规划、生成、修改和审查视频项目。它将对话式 Agent、节点优先画布、可配置媒体模型 provider，以及 Windows、Linux、macOS 三平台桌面打包能力整合在同一个工作流中。

项目处于持续开发阶段。公开接口、provider 适配和打包行为可能随版本迭代调整。

## 核心能力

- 将自然语言创作请求转换为可编辑的 `text`、`image`、`video`、`audio` 节点。
- 使用 React Flow 画布作为用户可见的创作真相源。
- 优先复用和修改现有画布节点，避免重复创建。
- 通过模糊查询或正则查询定位已有节点，再读取精确节点详情进行安全更新。
- 通过本地 FastAPI 后端管理项目状态、任务、节点、媒体产物和 Agent trace。
- 支持运行时配置 LLM、图片、视频和音频模型 provider。
- 使用 Electron、Next.js standalone、FastAPI 和 PyInstaller 构建 Windows、Linux、macOS 桌面包。
- 默认使用 SQLite 和本地文件系统存储项目数据。

## 架构

```text
openreel-studio/
├── apps/
│   ├── api/       # FastAPI 后端、Agent loop、工具和媒体执行
│   ├── web/       # Next.js 前端、聊天界面、React Flow 画布
│   └── desktop/   # Electron 桌面壳
├── packages/
│   └── shared/    # 前后端共享 TypeScript 类型和常量
├── packaging/     # PyInstaller 打包配置
├── scripts/
│   └── desktop/   # 桌面端打包脚本
└── docs/          # 打包和运维文档
```

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Next.js、React、TypeScript、React Flow、Tailwind CSS、Zustand |
| 后端 | FastAPI、SQLite、SQLModel、LiteLLM、MCP 风格工具注册表 |
| 桌面端 | Electron、PyInstaller、Next.js standalone 输出 |
| 运行时 | 本地文件存储、SSE 流式通信、可配置媒体生成 provider |

## Agent 工作流

OpenReel Studio 使用节点优先的 Agent 模型。画布是创作真相源，所有创作状态都通过用户可见节点表达。

- `text` 节点存放 brief、脚本、结构说明、审查结果和 prompt notes。
- `image` 节点存放视觉参考、人物、场景、分镜、首尾帧和生成图片。
- `video` 节点存放视频提示词、参考关系、状态、生成结果和生成历史。
- `audio` 节点存放语音、音乐、音效等纯音频提示词和产物。

当 Agent 需要查找已有内容时，会先使用 `node.list(query|regex)` 缩小候选范围。没有命中时，再使用 `node.list(limit=0)` 读取完整索引，最后通过 `node.get(node_ids=[...])` 批量读取精确详情。这样既能避免无意义地读取整个画布，也能确保后续修改始终绑定真实节点 ID。

## 环境要求

- Node.js 20 或更高版本
- pnpm 9 或更高版本
- Python 3.11 或更高版本
- uv
- Git

桌面包应在目标操作系统上构建。PyInstaller 会生成平台相关的 API 可执行文件，因此 Windows、Linux、macOS 产物需要分别构建。

## 快速开始

```bash
git clone https://github.com/yutianxiao6/openreel-studio.git
cd openreel-studio
cp .env.example .env
pnpm install
cd apps/api && uv sync && cd ../..
pnpm api:init-db
```

启动 API：

```bash
pnpm api:dev
```

另开一个终端启动 Web 应用：

```bash
pnpm dev
```

访问：

```text
http://localhost:3000
```

本地开发时，Web 应用会将 `/api/*` 请求代理到 `http://localhost:8000`。

## 配置

运行时 provider 配置存放在 `config/runtime.jsonc`。该文件已被 Git 忽略。可以在设置界面配置模型 provider，也可以在本地创建该文件，并通过环境变量引用密钥。

不要提交真实 API Key。密钥应存放在 `.env.local`、`.env.production` 或系统环境变量中。

示例：

```env
DEEPSEEK_API_KEY=<your-api-key>
OPENAI_API_KEY=<your-api-key>
```

## 安装

### 桌面版 Release

最新版桌面包下载地址：

```text
https://github.com/yutianxiao6/openreel-studio/releases/latest
```

Release 产物：

- Windows：`OpenReel.Studio-Setup-*.exe`
- Linux 桌面版：`OpenReel.Studio-*.AppImage` 或 `OpenReel.Studio-*.deb`
- macOS：`OpenReel.Studio-*.dmg`

### 一条命令下载桌面包

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.ps1 | iex
```

Linux/macOS：

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.sh | bash
```

npm installer：

```bash
npx openreel-studio-installer
```

通过 `OPENREEL_DOWNLOAD_DIR` 可以指定下载目录。Windows 设置 `OPENREEL_NO_RUN=1`、macOS 设置 `OPENREEL_NO_OPEN=1` 时，只下载安装包，不自动打开。

### Linux 服务器部署

Linux 桌面用户应使用 AppImage 或 `.deb` 发行产物。Linux 服务器部署应使用 Docker stack：

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install-server.sh | bash
```

服务器安装脚本会将仓库克隆或更新到 `~/openreel-studio`，按需创建 `.env.production`，提示设置 Basic Auth 登录账号和密码，并启动：

- `api`：FastAPI 后端，不直接暴露到公网。
- `web`：Next.js standalone 前端。
- `gateway`：Caddy HTTPS 网关，默认监听 `${APP_PUBLIC_PORT:-3100}` 并启用 Basic Auth。

访问：

```text
https://服务器IP:3100
```

Caddy 默认使用内部 TLS 证书，首次访问时浏览器可能提示证书风险。API 首次启动时会根据已有的 `*_API_KEY` 环境变量自动生成 `config/runtime.jsonc`。`config/runtime.example.jsonc` 是无密钥模板，可用于手动编辑参考。`config/runtime.jsonc`、`.env.production`、`data/`、`storage/` 均为私有运行时文件，已被 Git 忽略。

## 桌面端打包

Windows：

```bat
package-windows.bat
```

```powershell
pnpm desktop:package:win
```

Linux：

```bash
pnpm desktop:package:linux
```

macOS：

```bash
pnpm desktop:package:mac
```

桌面端产物输出到：

```text
dist/installers/
```

打包细节见 [docs/DESKTOP_PACKAGING.md](./docs/DESKTOP_PACKAGING.md)。

## 自动发布

推送版本 tag 后，GitHub Actions 会构建桌面端产物：

```bash
git tag v0.1.0
git push origin v0.1.0
```

Release workflow 会构建 Windows、Linux、macOS 产物并上传到对应的 GitHub Release。一键安装脚本始终解析最新版 Release。

npm installer 包从 `packages/installer` 发布：

```bash
cd packages/installer
npm publish --access public
```

CI 发布可以将 automation 或 granular npm publish token 配置为 `NPM_TOKEN`，也可以在 npmjs.com 配置 Trusted Publishing：

- Owner: `yutianxiao6`
- Repository: `openreel-studio`
- Workflow filename: `npm-publish.yml`
- Allowed action: `npm publish`

每次发布 npm 新版本前都必须更新 package 版本号。

## 开发命令

```bash
pnpm dev                 # 启动 Next.js Web 开发服务
pnpm api:dev             # 启动 FastAPI 开发服务
pnpm api:init-db         # 初始化本地 SQLite 数据库
pnpm -r typecheck        # 检查 TypeScript 类型
pnpm --filter web build  # 构建 Web 应用
```

API 测试：

```bash
cd apps/api
PYTHONPATH=. uv run pytest -q
```

## 仓库范围

本仓库包含 OpenReel Studio 核心应用、服务器部署文件和三平台桌面打包路径。

本仓库不包含本地运行数据、生成媒体、部署证书、私有 provider 配置或单独维护的 bot/plugin 仓库。

## 安全说明

- 不要提交 `.env.local`、`.env.production`、`config/runtime.jsonc`。
- 不要提交生成的 `data/`、`storage/`、`.next/`、`.venv/`、`node_modules/`。
- 如果任何 provider key 曾经进入公开仓库，应立即轮换。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
