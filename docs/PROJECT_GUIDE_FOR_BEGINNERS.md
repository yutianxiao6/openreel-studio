# OpenReel Studio 新手项目说明 / Beginner Project Guide

## English Overview

This guide is for developers, product designers, and contributors who are new to
OpenReel Studio. It explains the project structure, core concepts, request flow,
and the main files to inspect when changing the web UI, API, Agent loop,
workflow runtime, skills, plugins, packaging, or documentation.

The Chinese section below is the detailed guide. New English contributors should
use it together with `README.md`, `SETUP.md`, `BLUEPRINT_OPERATING_MODEL.md`,
`docs/workflow-spec-protocol.md`, and `docs/workflow-build-patch-design.md`.

Key ideas:

- The visible canvas nodes are the source of truth for creative output.
- The API owns project state, tool execution, workflow runtime, media services,
  traces, and local storage.
- Workflow templates describe reusable inputs, steps, dependencies, loops, and
  visible canvas products.
- Skills hold production methods and prompt-writing guidance outside the stable
  system prompt.
- Runtime data, generated media, local config, traces, and secrets stay out of
  Git commits.

## 中文正文

这份文档面向第一次接触 OpenReel Studio 的开发者、产品同学和设计同学。目标不是替代代码注释或架构规范，而是先把项目的结构、核心概念、请求流向和常见改动入口讲清楚，让新手知道“这个项目是什么、代码在哪里、改某个功能应该先看哪里”。

如果你是 Agent 或长期维护者，还需要继续阅读根目录的 `AGENTS.md`、`BLUEPRINT_OPERATING_MODEL.md` 和相关 workflow 文档。它们定义了更严格的工程边界和 Agent 行为规则。

## 1. 项目是什么

OpenReel Studio 是一个聊天式视频智能创作工作台。用户可以通过聊天告诉系统想做什么，也可以在画布上直接编辑节点、运行节点、查看结果。项目把几个能力放在同一个工作台里：

- 对话式 Agent：理解用户需求，调用工具，创建或修改画布节点。
- 可视化画布：用节点表达剧情文本、图片、视频和其他创作产物。
- 工作流模板：把复杂视频制作流程保存成可复用的模板。
- 媒体生成：对接 LLM、图片模型、视频模型等 provider。
- 本地项目存储：默认用 SQLite 和本地文件系统保存项目数据、运行记录和媒体资产。
- 桌面打包：支持把 Web + API 打包成桌面应用。

一句话理解架构：

```text
Next.js 前端负责聊天、画布和设置界面；
FastAPI 后端负责项目数据、Agent loop、工具调用、工作流运行和媒体生成；
SQLite + 本地文件系统负责保存项目、节点、trace、上传文件和生成结果。
```

## 2. 先记住几个核心概念

### 项目 Project

项目是用户工作的容器。一个项目里有画布节点、聊天历史、任务状态、工作流运行实例、媒体资产和运行 trace。用户切换项目时，前端和后端都以 `project_id` 作为隔离边界。

### 画布节点 Canvas Node

画布节点是用户真正看见和编辑的创作内容。当前核心创作节点是：

- `text`：剧情、分段剧本、人物设定、分镜说明、提示词正文等文本内容。
- `image`：人物图、场景图、分镜图、参考图、首尾帧等图片产物。
- `video`：片段视频或最终视频。
- `audio`：音频类产物，主要用于扩展和兼容。

节点之间的依赖用字段表达，然后由后端或前端映射成画布连线。对模型和用户来说，节点是创作事实源，不应该让隐藏的临时状态取代节点成为最终内容来源。

### 边 Edge

边表示节点之间的引用或生产依赖。比如“第 1 段视频”依赖“第 1 段剧本”和“第 1 段分镜图”，画布上应该能看到对应连线。

### Agent

Agent 是后端里的模型调度循环。它读取系统提示词、项目状态和用户消息，然后通过工具改变项目状态。典型工具包括读取项目、创建节点、更新节点、运行节点、请求用户补充信息、查询 skill 或 workflow。

### Skill

Skill 是给模型看的专业说明。比如视频制作流程、提示词写法、审核方法等，不应该全部塞进常驻系统提示词。内置 skill 在 `apps/api/app/skills/`，用户自定义 skill 在根目录 `skills/` 下按分类保存。

### Workflow Template

Workflow Template 是可复用的工作流模板。它描述输入是什么、有哪些步骤、每一步的依赖关系、哪些结果要映射到画布节点。内置模板通常在后端代码或内置 skill 里，用户自定义模板在根目录 `workflow_templates/`。

### Workflow Run

Workflow Run 是一次模板运行实例。用户可以添加多个流程实例，同一个模板也可以运行多次。每个实例应该有自己的输入、进度、输出和详情状态，不能和其他实例混用。

### Plugin

Plugin 是扩展工作流能力的机制，用于接入自定义功能。比如根目录 `plugins/keyframe-extractor/` 可以作为“输入视频，提取关键帧”的能力来源。

### Trace

Trace 是 Agent 和工具调用的运行记录。排查“模型为什么这么做”“工具调用了几次”“哪一步失败了”时，trace 是最重要的依据之一。

## 3. 顶层目录怎么读

根目录下的重要目录如下：

```text
openreel-studio/
├── apps/
│   ├── api/                 # FastAPI 后端
│   ├── web/                 # Next.js 前端
│   └── desktop/             # Electron 桌面壳
├── packages/
│   ├── shared/              # 前端共享类型和常量
│   └── installer/           # 一键安装器相关代码
├── docs/                    # 项目文档
├── skills/                  # 用户自定义 skill
├── workflow_templates/      # 用户自定义工作流模板
├── plugins/                 # 用户自定义或内置插件
├── data/                    # 本地运行数据、SQLite、trace、tool result
├── storage/                 # 上传和生成的媒体文件
├── assets/                  # 资产库数据
├── config/                  # 本地运行配置
├── deploy/                  # 服务器部署配置
├── packaging/               # PyInstaller 等打包配置
└── scripts/                 # 初始化、构建、打包和维护脚本
```

新手最常看的目录通常是：

- 改界面：看 `apps/web/`。
- 改 API、Agent、工具、工作流运行：看 `apps/api/`。
- 改共享类型：看 `packages/shared/`。
- 改项目说明或规范：看 `docs/`、`AGENTS.md`、`BLUEPRINT_OPERATING_MODEL.md`。
- 改用户可复用模板：看 `workflow_templates/`。
- 改用户可复用知识：看 `skills/`。
- 查运行问题：看 `data/agent_traces/`、`data/tool_results/`、`data/logs/`。

`data/`、`storage/`、本地密钥、运行配置和构建产物通常不应该提交到 Git。

## 4. 前端结构

前端位于 `apps/web`，技术栈是 Next.js、React、TypeScript、React Flow、Tailwind CSS 和 Zustand。

### 路由入口

```text
apps/web/app/
├── layout.tsx                  # Next.js 全局布局
├── page.tsx                    # 首页或默认跳转入口
└── projects/[projectId]/page.tsx # 项目工作台页面
```

用户打开某个项目时，核心页面从 `projects/[projectId]/page.tsx` 进入，再加载工作区、聊天面板、画布和各种侧栏。

### 工作区和面板

```text
apps/web/components/workspace/
apps/web/components/panel/
apps/web/components/project/
```

这些目录主要负责工作台整体布局、项目面板、项目标题、视图切换等。改“页面怎么分栏”“聊天和画布如何摆放”“项目标题怎么编辑”，通常从这里开始看。

### 聊天界面

```text
apps/web/components/chat/
├── ChatPanel.tsx              # 聊天主面板，负责发送消息和消费 SSE
├── SlashMenu.tsx              # slash command 菜单
├── PendingActionCard.tsx      # 待确认动作卡片
├── PendingDecisionActions.tsx # 待选择动作
└── ProposedPlanCard.tsx       # plan 模式输出卡片
```

聊天面板会把用户消息发给后端 `/api/chat/stream`，然后接收 SSE 事件。常见事件包括模型输出、工具开始、工具结束、画布更新、节点状态变化等。

### 画布

```text
apps/web/components/canvas/
├── WorkflowCanvas.tsx         # React Flow 画布和工作流运行栏
├── SmartNode.tsx              # 画布节点卡片
├── NodeDetailPanel.tsx        # 节点详情和编辑面板
├── ImageEditPanel.tsx         # 图片编辑面板
├── CanvasGroupLayer.tsx       # 分组背景层
├── nodeStyles.ts              # 节点样式
└── nodes/                     # 节点组件出口
```

这里是前端最核心的交互区域。常见改动入口：

- 节点卡片显示不对：先看 `SmartNode.tsx` 和 `apps/web/lib/nodeDisplay.ts`。
- 节点详情、编辑内容不一致：先看 `NodeDetailPanel.tsx`、`SmartNode.tsx` 和节点数据来源。
- 拖拽、框选、删除、缩放、右键菜单：先看 `WorkflowCanvas.tsx`。
- 工作流胶囊、运行流程栏、流程节点详情：也在 `WorkflowCanvas.tsx` 附近。

前端应尽量让画布预览、详情栏和编辑栏读取同一个节点数据模型，避免三个位置各自解析输出，导致状态不一致。

### 设置界面

```text
apps/web/components/settings/
├── SettingsModal.tsx
└── tabs/
    ├── AgentTab.tsx
    ├── AgentDebugTab.tsx
    ├── LlmTab.tsx
    ├── MediaTab.tsx
    └── RawFileTab.tsx
```

设置界面用于配置模型 provider、媒体生成 provider、查看 Agent 诊断信息、查看原始配置等。

### 前端状态

```text
apps/web/stores/
├── canvasStore.ts             # 画布节点、边、选择态等
├── chatStore.ts               # 聊天消息、streaming 状态等
├── projectStore.ts            # 当前项目、项目列表等
├── blueprintStore.ts          # 旧蓝图兼容状态
└── viewModeStore.ts           # 视图模式状态
```

如果你发现刷新后状态丢失、多个流程输入串了、删除后又恢复，一般要同时检查：

- Zustand store 是否把运行态混到了全局态里。
- 后端是否按 `project_id` 和 workflow `instance_id` 隔离。
- 前端重新拉取项目时是否覆盖了本地正确状态。

### 前端 API 封装

```text
apps/web/lib/api.ts
```

这里集中封装前端调用后端 API 的函数和 SSE 事件处理。改接口字段时，要同步检查这里和后端路由返回结构。

## 5. 后端结构

后端位于 `apps/api`，技术栈是 FastAPI、SQLite、SQLModel、LiteLLM 和一套 MCP-style tool registry。

### FastAPI 入口

```text
apps/api/app/main.py
```

这里创建 FastAPI app，注册路由、中间件和静态文件服务。服务启动命令通常是：

```bash
pnpm api:dev
```

它实际会进入 `apps/api` 并运行：

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### API 路由

```text
apps/api/app/api/
├── routes_chat.py             # 聊天 SSE 主入口
├── routes_projects.py         # 项目、节点、工作流相关 REST
├── routes_nodes.py            # 节点相关 API
├── routes_media.py            # 媒体文件和生成相关 API
├── routes_assets.py           # 资产库 API
├── routes_tools.py            # 工具和 MCP 相关 API
├── routes_models.py           # 模型配置 API
├── routes_agent_debug.py      # Agent 诊断和 trace 查询
└── chat_events.py             # SSE 事件结构校验
```

用户聊天时，最重要的是 `routes_chat.py`。用户直接点击画布或运行工作流时，通常会走 `routes_projects.py`、`routes_nodes.py` 或 `routes_workflow.py`。

### Agent 核心

```text
apps/api/app/agent/
├── orchestrator.py            # Agent loop 主体
├── prompt_assembler.py        # 组装 system prompt、历史和 runtime context
├── prompts/                   # 常驻 prompt 片段
├── slash_commands.py          # /plan、/reset、/project 等确定性命令
├── collaboration_mode.py      # Plan Mode 等模式状态
├── agent_trace.py             # trace 写入
├── trace_store.py             # trace 查询镜像
├── context_compact.py         # 上下文压缩
├── token_usage.py             # token 和 cache 监控
├── permission_policy.py       # 工具权限策略
├── workflow_template_store.py # 工作流模板读取和存储
├── workflow_canvas_projection.py # workflow 到画布映射
└── workflow_*.py              # workflow spec、审核、patch、证据等
```

Agent 的基本职责是：

1. 读取用户消息和必要项目状态。
2. 组装提示词和工具列表。
3. 调用模型。
4. 执行模型选择的工具。
5. 把工具结果、节点变化和文本输出通过 SSE 发给前端。

业务判断应该主要由模型和 skill 完成，后端负责状态不变量、权限、安全确认、数据保存和工具执行。

### Prompt 结构

```text
apps/api/app/agent/prompts/
├── identity.py
├── core_rules.py
├── working_loop.py
├── task_loop.py
├── runtime_context.py
├── plan_mode.py
├── workflow_build_mode.py
└── tools_manifest.py
```

这些文件是每轮提示词的核心来源。它们应该保持短、稳定、缓存友好。视频流程、提示词写法和复杂业务知识应放到 skill 或 workflow 指南里，而不是写成长篇常驻 prompt。

### 工具注册和工具实现

```text
apps/api/app/mcp_tools/
├── registry.py                # 工具注册表，决定哪些工具对 Agent 可见
├── node_universal.py          # node.list/get/create/update/run 等核心节点工具
├── workflow_tools.py          # workflow 模板、运行、检查、物化等工具
├── interaction_tools.py       # interaction.request_input
├── task_tools.py              # task.create/update/complete/list
├── project_tools.py           # project.get_state/reset 等
├── canvas_tools.py            # canvas.delete 等
├── skill_tools.py             # skill.search/get 等
├── vision_tools.py            # vision.view_image
├── tool_meta_tools.py         # tool.search/describe/execute
├── agent_tools.py             # agent.run 等协作能力
└── file_tools.py              # workspace 文件读写 patch 能力
```

工具是 Agent 改变项目状态的主要方式。新手修改工具时要先确认：

- 这个工具是否应该常驻给主 Agent。
- 是否更适合做 deferred tool，通过 `tool.search` / `tool.describe` / `tool.execute` 按需使用。
- 工具 description 是否足够短，schema 是否能约束输入。
- 是否需要 permission policy 或确认流程。
- 是否会破坏 prompt cache 前缀。

### 数据库

```text
apps/api/app/db/
├── models.py                  # SQLModel 数据模型
└── session.py                 # SQLite session
```

项目、消息、节点、trace、配置等持久化结构通常从这里开始看。数据库文件默认在 `data/` 下，不应该提交。

### 服务层

```text
apps/api/app/services/
```

服务层承接具体业务执行，比如项目服务、媒体 provider、媒体生成历史、上传处理、插件执行等。Agent 工具不应该把所有逻辑都写在工具函数里，复杂执行应该下沉到 service。

### 内置 Skill

```text
apps/api/app/skills/
├── video_production/
├── general_short_drama_workflow/
├── script_writing/
├── character_prompt/
├── scene_prompt/
├── video_prompt/
└── ...
```

这些 skill 是内置知识。比如视频制作流程、人物提示词、场景提示词、分镜提示词、审核方法等。修改“模型应该怎么写提示词”时，通常先看这里，而不是先改 system prompt。

## 6. 一条聊天请求如何流动

用户在聊天框输入一句话后，大致流向如下：

```text
浏览器 ChatPanel
  -> POST /api/chat/stream
  -> routes_chat.py
  -> orchestrator.py
  -> prompt_assembler.py 组装提示词和工具
  -> LLM 返回文本或工具调用
  -> registry.py 找到工具并执行
  -> 工具写数据库、更新节点、写 trace
  -> SSE 事件回到前端
  -> chatStore / canvasStore 更新 UI
```

如果模型要创建节点，会调用 `node.create`。如果要运行节点，会调用 `node.run`。如果需要用户补充信息，会调用 `interaction.request_input`，前端会显示通用输入卡。

如果模型要使用工作流，通常会先查询或选择 workflow template，再实例化为 workflow run。真正用户可见的产物最终还是会映射为画布节点。

## 7. 一次画布操作如何流动

用户在画布上拖动、选择、编辑节点时，主要由前端 React Flow 和 Zustand 管理本地交互状态。需要保存时，再调用后端 REST API。

典型流向：

```text
WorkflowCanvas / NodeDetailPanel
  -> 更新 canvasStore 中的本地状态
  -> 调用 apps/web/lib/api.ts 中的 API 函数
  -> routes_projects.py 或 routes_nodes.py
  -> 数据库保存节点、边、位置、字段
  -> 前端刷新或接收 SSE 后同步最新状态
```

需要注意的是：不是所有点击都应该触发“需要保存”。只有节点内容、位置、连线、字段等发生实质变化时，才应该进入保存状态。

## 8. 一次工作流运行如何流动

工作流模板是一份可复用 spec。用户把模板添加到画布或运行栏后，会产生一个 workflow run instance。

典型流向：

```text
用户选择工作流模板
  -> 后端创建 workflow run instance
  -> 前端显示运行流程胶囊或流程栏
  -> 用户填写该实例自己的输入
  -> 点击运行或逐步运行
  -> 后端根据依赖执行步骤
  -> flow_only 步骤写入 workflow_runtime
  -> 产物步骤映射为 text/image/video 节点
  -> 前端实时显示流程进度和画布节点状态
```

几个重要原则：

- 每个 workflow run instance 都应该有自己的输入和进度。
- 删除一个运行实例后，不应该恢复旧输出。
- 工作流运行进度和画布节点状态是两套相关但独立的状态。
- 没有依赖的步骤可以并行；某一步失败时，是否继续取决于 workflow runner 的策略。
- 用户可见产物应该落到画布节点，不应该只藏在 JSON 或调试输出里。

## 9. 工作流模板、Skill 和 Plugin 的区别

这三个概念容易混：

| 类型 | 主要回答的问题 | 放在哪里 | 谁使用 |
| --- | --- | --- | --- |
| Skill | 模型应该怎么理解任务、怎么写提示词、怎么审核 | `apps/api/app/skills/` 或 `skills/` | Agent / 子 Agent |
| Workflow Template | 一个流程有哪些输入、步骤、依赖和产物 | `workflow_templates/` 或内置模板目录 | Workflow runner / Agent |
| Plugin | 某个步骤需要调用的外部或自定义能力 | `plugins/` | Workflow runner / 工具 |

例子：

- “怎么写人物设定提示词”是 skill。
- “文生视频流程：输入主题和时长，生成剧本，分段，出图，出视频”是 workflow template。
- “输入视频，提取关键帧图片”是 plugin。

## 10. 数据和文件保存在哪里

本地开发时，常见运行数据位置如下：

```text
data/
├── app.db                     # SQLite 数据库
├── agent_traces/              # Agent trace JSONL
├── tool_results/              # 大工具结果
├── prompt_dumps/              # prompt dump
├── logs/                      # 日志
└── workflow_templates/        # 部分运行期模板数据

storage/
├── <project_id>/              # 项目媒体文件
├── assets/                    # 资产库文件
├── exports/                   # 导出结果
└── temp/                      # 临时文件

config/
├── runtime.jsonc              # 本地 provider 配置，通常不提交
├── image_provider_protocols/  # 图片 provider 请求协议 catalog，不含密钥
├── video_provider_protocols/  # 视频 provider 请求协议 catalog，不含密钥
└── audio_provider_protocols/  # 音频 provider 请求协议 catalog，不含密钥
```

提交代码前要特别检查：

- 不提交 `.env`、`.env.local`、`.env.production`。
- 不提交真实 API Key。
- 不提交 `data/`、`storage/`、生成图片、生成视频、trace、tool result。
- 不提交 `.claude/`、临时测试文件、构建产物。

## 11. 常用开发命令

安装依赖：

```bash
pnpm install
cd apps/api && uv sync && cd ../..
```

初始化数据库：

```bash
pnpm api:init-db
```

启动后端：

```bash
pnpm api:dev
```

启动前端：

```bash
pnpm dev
```

类型检查：

```bash
pnpm -r typecheck
```

后端测试：

```bash
cd apps/api && PYTHONPATH=. uv run pytest -q
```

空白和补丁格式检查：

```bash
git diff --check
```

查看当前改动：

```bash
git status --short --branch
git diff --stat
```

## 12. 想改某个功能应该看哪里

### 改节点卡片显示

先看：

- `apps/web/components/canvas/SmartNode.tsx`
- `apps/web/lib/nodeDisplay.ts`
- `apps/web/components/canvas/NodeDetailPanel.tsx`

再看后端节点输出结构：

- `apps/api/app/mcp_tools/node_universal.py`
- `apps/api/app/db/models.py`

### 改画布交互

先看：

- `apps/web/components/canvas/WorkflowCanvas.tsx`
- `apps/web/stores/canvasStore.ts`

典型交互包括拖动、框选、键盘删除、右键创建节点、自动对齐、缩放、平移、连线。

### 改聊天和 Agent 行为

先看：

- `apps/api/app/api/routes_chat.py`
- `apps/api/app/agent/orchestrator.py`
- `apps/api/app/agent/prompt_assembler.py`
- `apps/api/app/agent/prompts/`
- `apps/api/app/mcp_tools/registry.py`

如果是业务知识或提示词写法，优先看：

- `apps/api/app/skills/`
- `skills/`

### 改工作流模板

先看：

- `workflow_templates/`
- `apps/api/app/agent/workflow_template_store.py`
- `apps/api/app/mcp_tools/workflow_tools.py`
- `docs/workflow-spec-protocol.md`

如果模板来自内置 skill，还要看 `apps/api/app/skills/` 下对应目录。

### 改工作流运行状态或流程栏

先看前端：

- `apps/web/components/canvas/WorkflowCanvas.tsx`
- `apps/web/stores/canvasStore.ts`
- `apps/web/lib/api.ts`

再看后端：

- `apps/api/app/api/routes_projects.py`
- `apps/api/app/api/routes_workflow.py`
- `apps/api/app/mcp_tools/workflow_tools.py`
- `apps/api/app/agent/workflow_canvas_projection.py`

### 改媒体生成

先看：

- `apps/api/app/services/media_provider.py`
- `apps/api/app/mcp_tools/node_universal.py`
- `apps/api/app/api/routes_media.py`
- `apps/web/components/settings/tabs/MediaTab.tsx`

图片、视频和音频生成一般由 `node.run` 触发。`config/runtime.jsonc` 只保存 Base URL、模型名、API Key 和选中的协议 ID；具体 HTTP 请求结构写在 `config/image_provider_protocols/catalog.json`、`config/video_provider_protocols/catalog.json` 和 `config/audio_provider_protocols/catalog.json`。字段定义、模板变量和错误行为见 `docs/MODEL_PROVIDER_PROTOCOLS.md`。

### 改文件读写或开发者工具

先看：

- `apps/api/app/mcp_tools/file_tools.py`
- `apps/api/app/mcp_tools/tool_meta_tools.py`
- `apps/api/app/agent/permission_policy.py`

文件工具用于开发和管理文件，不应该变成让 Agent 绕过业务工具的万能入口。

## 13. 调试顺序

遇到问题时，不要只看前端表现。推荐按这个顺序查：

1. 看浏览器控制台和 Network，确认请求是否成功、SSE 是否断开。
2. 看设置里的 Agent 诊断面板，确认当前项目、模式、pending 状态、最近 trace。
3. 看后端日志，确认 API 是否报错。
4. 看节点状态，失败节点先读 error、output、stages。
5. 看 `data/agent_traces/<project_id>/`，确认模型调用了什么工具。
6. 看 `data/tool_results/<project_id>/`，确认大工具结果是否正确。
7. 看数据库或 debug API，确认前端看到的状态是否和后端一致。
8. 如果是媒体问题，再看 `storage/` 文件是否存在，provider 返回是否成功。

常见问题判断：

- “前端没反应”：先查 Network 和 SSE，再查后端有没有实际更新节点。
- “模型乱做”：先查 trace，看 prompt、工具列表、工具调用顺序和工具结果。
- “刷新后恢复旧状态”：查后端持久化状态和前端拉取覆盖逻辑。
- “两个流程互相串输入”：查 workflow instance id 是否隔离。
- “画布卡片、详情、编辑不一致”：查三处是否读取了不同字段或做了不同解析。

## 14. 测试和验收

不同改动需要不同验证。

### 文档或配置改动

至少运行：

```bash
git diff --check
```

### 前端 UI 改动

建议运行：

```bash
pnpm -r typecheck
```

如果改了复杂交互，应该用浏览器真实操作验证，比如创建节点、拖动、框选、删除、运行流程、刷新页面。

### 后端工具或 Agent 改动

建议运行：

```bash
cd apps/api && PYTHONPATH=. uv run pytest -q
```

如果改了 Agent 行为，仅单元测试通常不够，还要看真实 trace，确认：

- 任务能否完成。
- 结果是否符合用户真实意图。
- 工具调用次数是否合理。
- 是否复用了应该复用的模板或 skill。
- 是否正确处理缺失输入和报错。
- 是否没有污染原有路径。

### 工作流模板改动

除了格式校验，还要检查展开后的画布映射：

- 输入是否清楚。
- 步骤依赖是否正确。
- 每个产物是否映射到正确节点类型。
- 画布连线是否符合真实生产关系。
- 文本节点是否展示正文。
- 图片和视频节点是否包含必要属性，比如宽、高、时长、参考来源。

## 15. 新手推荐阅读路径

如果你只有半小时，按这个顺序看：

1. `README.md`：了解项目能力和启动方式。
2. 本文档：了解目录和请求流向。
3. `apps/web/components/canvas/WorkflowCanvas.tsx`：理解用户主要操作界面。
4. `apps/web/components/chat/ChatPanel.tsx`：理解聊天和 SSE。
5. `apps/api/app/api/routes_chat.py`：理解聊天请求入口。
6. `apps/api/app/agent/orchestrator.py`：理解 Agent loop。
7. `apps/api/app/mcp_tools/node_universal.py`：理解节点如何创建、更新和运行。
8. `apps/api/app/mcp_tools/workflow_tools.py`：理解工作流如何运行。

如果你要改 Agent 或工作流，再继续看：

- `AGENTS.md`
- `BLUEPRINT_OPERATING_MODEL.md`
- `docs/workflow-spec-protocol.md`
- `docs/workflow-build-patch-design.md`
- `docs/AGENT_QUALITY_ACCEPTANCE.md`

## 16. 常见误区

### 把业务判断写成后端关键词路由

普通自然语言请求应该进入 Agent loop，由模型结合状态、skill 和工具来决定动作。后端可以做安全确认、输入净化和状态不变量，但不应该用关键词替模型决定“用户要做视频，所以直接创建某个节点”。

### 把大量业务说明塞进 system prompt

常驻 prompt 要短、稳定、缓存友好。详细视频流程、提示词写法、审核规则应该放到 skill、workflow 指南或工具 schema 里。

### 让隐藏 JSON 成为用户看不见的真相源

用户可见产物应该落到画布节点。JSON 可以作为机器中间结构，但最终剧情、提示词、图片和视频要用用户能理解的形式展示。

### 画布、详情和编辑各自解析一套内容

这会导致“卡片显示四个字，详情显示另一段，编辑又是第三份”的问题。应尽量统一节点内容读取和展示逻辑。

### 多个 workflow run 共享状态

每次添加流程都应该产生独立实例。输入、进度、输出、详情展开状态都要按实例隔离。

### 随意改工具名和工具 schema

工具名、description、schema 和排序会影响模型行为和 prompt cache。改动前要确认是否影响现有路径，并同步文档、测试和 prompt 合同。

### 提交运行数据

`data/`、`storage/`、trace、tool result、生成媒体和密钥都不应该混进提交。

## 17. 提交前检查清单

提交前至少确认：

- 改动范围和本次任务相关。
- 没有混入运行数据、密钥、构建产物和临时文件。
- 文档链接有效。
- 前端类型或后端测试按改动风险执行过。
- `git diff --check` 通过。
- `git status --short --branch` 里只有应提交文件。

项目规则要求每次代码或文档修改后完成本地 commit。提交前可以用：

```bash
git status --short --branch
git diff --stat
git diff --check
```

## 18. 继续深入

下面这些文档适合在理解基础结构后继续阅读：

- `README.md`：项目能力、安装、启动和打包入口。
- `SETUP.md`：本地启动和生产部署步骤。
- `AGENTS.md`：Agent 工作规则和工程边界。
- `BLUEPRINT_OPERATING_MODEL.md`：节点优先创作模型和工具合同。
- `docs/workflow-spec-protocol.md`：workflow spec 协议。
- `docs/workflow-build-patch-design.md`：工作流搭建模式设计。
- `docs/MODEL_PROVIDER_PROTOCOLS.md`：图片、视频、音频模型 provider 协议配置说明。
- `docs/AGENT_QUALITY_ACCEPTANCE.md`：Agent 质量验收标准。
- `docs/DESKTOP_PACKAGING.md`：桌面打包说明。

如果你不知道从哪里开始，先跑起来项目，创建一个空项目，再分别观察“聊天创建节点”“手动创建节点”“运行一个工作流”三条路径。把这三条路径走通后，再改具体功能会清晰很多。
