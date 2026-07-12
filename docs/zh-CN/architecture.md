# 项目结构

[English](../en/architecture.md) · [中文文档首页](../README.md)

## 总览

OpenReel Studio 是一个前后端分离、以画布节点为创作真相源的视频工作台。

```text
浏览器 / Electron
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
                 SQLite + 本地资产 + Provider
```

## 顶层目录

```text
apps/
  web/                 Next.js、React、React Flow 前端
  api/                 FastAPI、Agent、工具、媒体服务
  desktop/             Electron 桌面壳
packages/
  shared/              前后端共享类型和逻辑
  installer/           安装包下载 CLI
config/                运行配置示例和媒体协议目录
docs/                  中文与英文公开文档
plugins/               内置或示例插件
skills/                用户可编辑的工作流、提示词和评审 Skill
workflow_templates/    用户工作流模板
data/                  SQLite、trace、工具结果和缓存（运行数据）
storage/               上传、生成和导出资产（运行数据）
```

`data/` 和 `storage/` 不属于源代码，也不应提交到公开仓库。

## 前端

前端位于 `apps/web`：

- `app/`：页面、路由和 Base Path 入口。
- `components/canvas/`：创作画布、节点详情、工作流面板和视频剪辑器。
- `components/settings/`：LLM、媒体模型、Agent 和原始配置面板。
- `stores/`：聊天、项目和画布状态。
- `lib/`：API 客户端、事件类型和显示转换。

前端消费结构化 REST/SSE 数据，不从 Agent 的自然语言回复中解析节点或连线。

## 后端

后端位于 `apps/api/app`：

- `main.py`：FastAPI 应用入口。
- `api/`：项目、聊天、节点、资产、配置、工作流和调试路由。
- `agent/`：Agent loop、Prompt 组装、上下文治理、权限和 trace。
- `mcp_tools/`：Agent 可调用的核心与延迟工具。
- `services/`：媒体生成、历史、编辑、导出和业务执行。
- `config_store/`：`config/runtime.jsonc` 的校验与运行时物化。
- `skills/`：内置运行时 Skill 和模板。

## 节点优先模型

创作画布只使用四种用户可见产物：

- `text`
- `image`
- `video`
- `audio`

节点字段保存提示词、模型设置、状态、参考关系和输出。`fields.references` 表示生产依赖，并自动映射为画布连线；`parent_node_id` 只表示界面分组。

## Agent 与工具

所有普通自然语言请求进入 Agent loop。模型先读取项目状态和需要的 Skill，再调用受权限控制的工具创建、修改或运行节点。后端负责安全确认、结构校验、持久化、上下文压缩和事件投递，不在模型外通过关键词猜测用户意图。

高频创作能力保持为稳定核心工具；低频操作通过工具搜索按需加载。大工具结果写入文件，模型上下文只保留摘要和引用。

## 工作流

工作流 Spec 描述可复用流程，运行实例负责解析集合、循环、输入和步骤状态。真正的文本、图片、视频和音频结果仍写回画布节点。这样流程结构与用户产物分离，模板可以复用，节点仍可独立修改。

完整协议见 [Workflow Spec](./workflow-spec-protocol.md)。

## 媒体 Provider

运行配置只保存 Provider、模型名、Base URL、Key 和协议 ID。图片、视频和音频 HTTP 请求结构放在 `config/*_provider_protocols/catalog.json`，由统一媒体服务读取。详细说明见 [模型接入](./model-providers.md)。

## 持久化与事件

- SQLite 保存项目、消息、节点、配置物化和工作流状态。
- `storage/` 保存上传、生成结果和导出文件。
- SSE 推送聊天增量、节点状态、画布变化、媒体进度和 Token 使用量。
- trace 同时写入 JSONL 和数据库镜像，供诊断面板查询。

## 安全边界

- 删除画布和全量重置需要结构化确认。
- 工具执行经过权限策略。
- 项目事件按 `project_id` 隔离。
- API Key 不进入日志、截图、提交或公开 Issue。
- 上传和工作区文件操作被限制在允许的项目根目录内。
