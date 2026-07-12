# OpenReel Studio

[English](./README.en.md) · 简体中文

[![Latest release](https://img.shields.io/github/v/release/yutianxiao6/openreel-studio?label=release)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![Release downloads](https://img.shields.io/github/downloads/yutianxiao6/openreel-studio/total?label=downloads)](https://github.com/yutianxiao6/openreel-studio/releases/latest)
[![npm installer](https://img.shields.io/npm/v/openreel-studio-installer?label=npm%20installer)](https://www.npmjs.com/package/openreel-studio-installer)
[![License](https://img.shields.io/github/license/yutianxiao6/openreel-studio)](./LICENSE)

**把一句创作需求变成可查看、可修改、可复用、可剪辑的视频生产流程。**

OpenReel Studio 是一个开源的聊天式视频智能创作工作台。你可以和 Agent 讨论创意，也可以直接在画布上编辑文本、图片、视频和音频节点；每一步的输入、参考素材、提示词和生成结果都保持可见，最终再进入内置剪辑器完成基础剪辑和导出。

![OpenReel Studio 创作画布](./docs/assets/screenshots/creation-canvas.png)

## 它解决什么问题

传统 AI 视频工具经常把剧本、参考图、提示词和生成结果分散在多个页面，失败后也很难知道应该从哪一步重做。OpenReel Studio 把创作过程放在同一张可追踪画布上：

- Agent 负责理解需求、规划步骤和调用模型，人始终可以查看并修改产物。
- 文本、图片、视频和音频都是可见节点，参考关系通过连线表达。
- 单个节点可以独立运行、重试或替换，不必整条流程从头开始。
- 成熟流程可以保存为工作流模板，在不同项目中重复使用。
- 生成的视频可以直接进入帧级时间线，完成裁剪、拼接和基础音画调整。

## 从想法到成片

1. **描述目标**：告诉 Agent 题材、时长、画幅、风格和已有素材。
2. **组织素材**：生成或导入剧本、人物、场景、分镜和参考图。
3. **生成内容**：按节点运行图片、视频和音频模型，失败只影响当前步骤。
4. **检查与选择**：查看节点历史，选择更合适的图片或视频继续向下游传递。
5. **剪辑导出**：在时间线上裁剪、拼接、调节画面和声音并导出结果。

## 主要功能

| 能力 | 你可以做什么 |
| --- | --- |
| 聊天式创作 | 用自然语言创建、修改、运行和复核创作节点。 |
| 节点画布 | 同时管理剧本、提示词、参考图、分镜、视频和音频。 |
| 视觉参考 | 让需要“看图说话”的步骤读取真实图片，把仅用于生成的素材作为模型参考图。 |
| 生成历史 | 保留多次图片和视频结果，失败不会覆盖最近一次成功预览。 |
| 工作流编辑器 | 可视化搭建可复用流程，支持输入、依赖、集合、循环和运行实例。 |
| 多模型接入 | 分别配置 LLM、图片、视频和音频服务；媒体 HTTP 协议使用声明式目录。 |
| 基础视频剪辑 | 帧级裁剪、分割、拼接、多轨排列、真实帧缩略图、真实音频波形和导出。 |
| 画面与声音 | 缩放、位置、旋转、不透明度、矩形裁剪、音量、静音和淡入淡出。 |
| 本地与桌面运行 | 支持源码运行、Docker 部署以及 Windows、Linux、macOS 桌面安装包。 |
| 调试与可观测性 | 提供运行 trace、工具结果、Token/缓存统计和 Agent 诊断面板。 |

## 界面预览

以下图片均截取自当前版本的真实运行界面。

### 可复用工作流

工作流面板用于搭建和保存流程结构；创作画布只展示真正交付给用户的文本、图片、视频和音频产物。

![OpenReel Studio 工作流编辑器](./docs/assets/screenshots/workflow-editor.png)

### 帧级视频剪辑

内置剪辑器聚焦基础粗剪和交付：真实帧画面、真实音频波形、裁剪、分割、拼接、多轨、音量、画面变换与导出。

![OpenReel Studio 视频剪辑器](./docs/assets/screenshots/video-editor.png)

## 适合谁

- 想把多种 AI 模型组合成稳定视频流程的创作者。
- 需要角色、场景和分镜参考保持可追踪的短视频团队。
- 希望在本地或自有服务器管理模型配置和生成资产的用户。
- 需要研究 Agent、工作流协议和节点式媒体生产的开发者。

OpenReel Studio 不内置模型额度。实际生成图片、视频、音频或调用 LLM 时，需要配置你自己的服务商账号和 API Key。

## 开始使用

- 想直接安装：前往 [最新 Release](https://github.com/yutianxiao6/openreel-studio/releases/latest) 下载桌面安装包。
- 想从源码运行：阅读 [中文快速开始](./docs/zh-CN/getting-started.md)。
- 第一次使用工作台：阅读 [中文使用指南](./docs/zh-CN/user-guide.md)。
- 想了解代码：阅读 [中文项目结构](./docs/zh-CN/architecture.md)。

安装器也可以直接下载当前平台的最新安装包：

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
| 开发与测试 | [打开](./docs/zh-CN/development.md) | [Open](./docs/en/development.md) |

## 开源仓库边界

仓库只保存代码、默认协议、内置 Skill、工作流模板和公开文档。以下内容不应提交：

- `.env`、API Key、访问令牌和私有证书；
- `data/`、`storage/` 中的运行数据库、生成资产、trace 和用户内容；
- 本地模型配置、私人工作流、构建产物和临时截图；
- 含有第三方隐私信息或无再分发权利的素材。

发现安全问题时，请不要在公开 Issue 中粘贴密钥、完整配置或用户数据。

## 项目状态

项目正在持续开发，工作流协议、桌面打包和剪辑器仍会迭代。用于正式生产前，请先在你的模型服务、素材格式和部署环境中完成验证。

## License

[MIT](./LICENSE)
