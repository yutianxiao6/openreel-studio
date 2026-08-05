# OpenReel Studio

[English](./README.en.md) · 简体中文

[![Latest release](https://img.shields.io/github/v/release/yutianxiao6/openreel-studio?label=release)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![Release downloads](https://img.shields.io/github/downloads/yutianxiao6/openreel-studio/total?label=downloads)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![npm installer](https://img.shields.io/npm/v/openreel-studio-installer?label=npm%20installer)](https://www.npmjs.com/package/openreel-studio-installer)
[![License](https://img.shields.io/github/license/yutianxiao6/openreel-studio)](./LICENSE)

**让 Agent、节点画布、可复用工作流与帧级时间线在同一个创作桌面协作。**

OpenReel Studio 是一个开源的聊天式 AI 视频创作工作台。你可以从一句自然语言需求开始，让 Agent 直接创建和运行文本、图片、视频、音频节点；也可以在 3D 导演台排演机位与构图、进入流程编辑器搭建可复用生产线，最后在内置时间线上完成剪辑与导出。

[快速开始](./docs/zh-CN/getting-started.md) · [使用指南](./docs/zh-CN/user-guide.md) · [工作流](./docs/zh-CN/workflows.md) · [外部智能体](./docs/zh-CN/agent-integrations.md) · [项目结构](./docs/zh-CN/architecture.md)

![OpenReel Studio 新版创作画布：聊天、节点产物与依赖关系位于同一工作区](./docs/assets/screenshots/creation-canvas.png)

## 为什么是 OpenReel Studio

AI 视频生产不只是生成一次图片或视频。真正困难的是让需求、剧本、参考图、提示词、模型参数、生成结果和剪辑版本保持一致，同时还能在失败时只重做必要的部分。

OpenReel Studio 用三个原则组织这条链路：

- **产物可见**：文本、图片、视频和音频都是画布上的真实节点，不藏在黑盒任务里。
- **依赖可追踪**：角色、场景、分镜和最终视频之间的参考关系通过连线表达。
- **步骤可重做**：单个节点可以编辑、运行、重试或替换，不必从头执行整条制作链。

## 一个工作台，五种协作方式

| 工作区 | 用途 |
| --- | --- |
| 项目会话 | 在左侧折叠栏中新建、切换、选择和管理项目，每个项目保留独立对话与画布。 |
| Agent 对话 | 用自然语言创建、修改、运行和复核节点；聊天区可以拖动调整宽度。 |
| 创作画布 | 查看和编辑真正交付的 `text`、`image`、`video`、`audio` 产物及其依赖。 |
| 3D 导演台 | 用白模、常用道具、自定义 GLB、环境全景和多机位排演空间关系，把选中的构图截图放回画布。 |
| 流程与剪辑 | 在流程编辑器中复用生产方法，在帧级时间线中完成音画整理与成片导出。 |

## 从一句话到成片

1. **描述目标**：输入题材、时长、风格、画幅以及已有素材。
2. **排演空间与机位**：需要精确构图时，在 3D 导演台摆放人物和道具、切换机位并保存构图参考。
3. **生成可见产物**：Agent 创建剧本、角色、场景、分镜、视频或音频节点。
4. **检查并局部调整**：查看真实预览、提示词、参考来源和历史结果，只重跑需要修改的节点。
5. **复用制作方法**：把稳定步骤保存为工作流，通过输入、依赖、集合、条件和循环批量运行。
6. **进入时间线**：从媒体区把图片、视频和音频拖入轨道，完成排列、裁剪、音量和画面调整。
7. **导出回画布**：渲染结果会作为新的成片视频节点返回画布，继续参与后续制作。

## 核心能力

| 能力 | 当前实现 |
| --- | --- |
| 节点优先创作 | 以用户可见的文本、图片、视频和音频节点作为创作真相源。 |
| 标准 Skill 运行时 | 自动发现内置和用户 `skills/<skill-name>/SKILL.md` 包；显式点名时直接加载，需求明显匹配 frontmatter `description` 时按需读取完整 Skill。 |
| 真实视觉参考 | 区分提示词模型看图、媒体模型视觉参考和直接采用现有图片等引用角色。 |
| 生成与历史 | 支持节点独立运行、失败重试和历史结果恢复；失败不会覆盖最近一次成功预览。 |
| 3D 导演台 | 支持白模姿态与动画、常用 3D 道具、自定义 GLB、环境全景、多机位和截图时间线；构图截图可作为普通图片参考节点进入后续生成。 |
| 工作流 V2 | 支持动态输入、`needs` 依赖、媒体 `uses`、集合展开、条件分支和有界反馈循环。 |
| 动态媒体设置 | 模型、比例、精确像素、画质和帧率由前端产物设置随本次运行传递，不污染可复用 Spec。 |
| 统一媒体协议 | 图片、音频和视频全部由 Universal Model Adapter 负责请求、上传、轮询、状态判断和结果提取；OpenReel 管理节点生命周期和落盘，在模型上下文之外归档完整结果，只返回有界投影。 |
| 音频卡片播放 | 音频节点直接在卡片上使用内置播放器播放并显示实时频率；上游返回多个音频时会完整保留为本地结果。 |
| 帧级视频剪辑 | 支持拖入素材、自动吸附、轨道排列、裁剪、分割、拼接、真实帧缩略图和音频波形。 |
| 画面与声音 | 支持位置、缩放、旋转、不透明度、矩形裁剪、音量、静音和淡入淡出。 |
| 本地与桌面运行 | 支持源码运行、Docker 部署以及 Windows、Linux、macOS 桌面安装包。 |
| 调试与可观测性 | 提供 Agent trace、工具结果、Token/缓存统计和诊断面板。 |

## 界面预览

以下截图来自当前版本的真实运行界面，演示内容为专门制作的公开示例。

### 3D 导演台

导演台用于在正式生成前排演人物、道具、环境和机位。截图先进入导演台内部时间线，只有用户选择“放入画布”后才创建普通 `image` 构图参考节点；它不会自动成为视频首帧。

### 可复用工作流

流程面板负责生产方法：搭建步骤、声明输入、连接依赖、配置动态产物并查看运行实例。创作画布仍只展示用户真正需要查看和交付的产物节点。

![OpenReel Studio 新版工作流编辑器](./docs/assets/screenshots/workflow-editor.png)

### 帧级视频时间线

内置剪辑器提供媒体池、画面监看、帧级轨道、真实波形、片段属性和导出。时间线导出后会在原画布创建新的成片节点。

![OpenReel Studio 新版视频剪辑器](./docs/assets/screenshots/video-editor.png)

## 适合谁

- 想把多种 AI 模型组合成稳定视频生产流程的创作者。
- 需要角色、场景、分镜和最终视频参考关系可追踪的短视频团队。
- 希望在本地或自有服务器管理模型配置、工作流和生成资产的用户。
- 研究 Agent 编排、Workflow V2 和节点式媒体生产的开发者。

OpenReel Studio 不内置模型额度。实际调用 LLM 或生成图片、视频、音频时，需要配置你自己的服务商账号和 API Key。

## 用 Codex 或其他智能体操作画布

[OpenReel Agent Plugin](https://github.com/yutianxiao6/openreel-agent-plugin)
提供操作 Skill 和本地 MCP 工具桥。Codex 可以直接从
marketplace 安装；Claude Code、Cursor、VS Code/Copilot、Gemini CLI 和
Windsurf 等支持本地 stdio MCP 的客户端，可以单独连接底层工具桥。

不同智能体不一定支持同一种插件安装格式；直接 MCP 客户端获得同一组工具，但不会
自动加载安装包中的 Skill。宿主有图片生成服务时优先使用宿主生成，没有时使用
OpenReel 图片节点。安装命令、通用 MCP 配置、支持级别和安全限制见
[外部智能体与 Agent Plugin](./docs/zh-CN/agent-integrations.md)。

## 开始使用

- 桌面安装：前往 [最新 Release](https://github.com/yutianxiao6/openreel-studio/releases/latest) 下载当前平台安装包。
- 源码运行：阅读 [中文快速开始](./docs/zh-CN/getting-started.md)。
- 第一次使用：阅读 [中文使用指南](./docs/zh-CN/user-guide.md)。
- 模型配置：阅读 [模型接入](./docs/zh-CN/model-providers.md)。

安装器也可以自动下载当前平台的最新安装包：

```bash
npx openreel-studio-installer
```

## 文档

完整中文文档从 [docs/README.md](./docs/README.md) 开始，英文文档从 [docs/README.en.md](./docs/README.en.md) 开始。

| 主题 | 中文 | English |
| --- | --- | --- |
| 快速开始 | [打开](./docs/zh-CN/getting-started.md) | [Open](./docs/en/getting-started.md) |
| 使用指南 | [打开](./docs/zh-CN/user-guide.md) | [Open](./docs/en/user-guide.md) |
| 项目结构 | [打开](./docs/zh-CN/architecture.md) | [Open](./docs/en/architecture.md) |
| 工作流 | [打开](./docs/zh-CN/workflows.md) | [Open](./docs/en/workflows.md) |
| 模型接入 | [打开](./docs/zh-CN/model-providers.md) | [Open](./docs/en/model-providers.md) |
| 外部智能体 | [打开](./docs/zh-CN/agent-integrations.md) | [Open](./docs/en/agent-integrations.md) |
| 开发与测试 | [打开](./docs/zh-CN/development.md) | [Open](./docs/en/development.md) |

## 开源仓库边界

仓库只保存代码、默认协议、内置 Skill、工作流模板和公开文档。以下内容不应提交：

- `.env`、API Key、访问令牌和私有证书；
- `data/`、`storage/` 中的运行数据库、生成资产、trace 和用户内容；
- 本地模型配置、私人工作流、构建产物和临时截图；
- 含有第三方隐私信息或无再分发权利的素材。

发现安全问题时，请不要在公开 Issue 中粘贴密钥、完整配置或用户数据。

## 项目状态

项目仍在持续开发，工作流协议、模型适配、桌面打包和剪辑器会继续迭代。用于正式生产前，请先在你的模型服务、素材格式和部署环境中完成验证。

## License

[MIT](./LICENSE)
