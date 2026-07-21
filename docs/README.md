# OpenReel Studio 中文文档

[English documentation](./README.en.md) · [返回项目首页](../README.md)

## 第一次使用

1. [快速开始](./zh-CN/getting-started.md)：安装桌面版、从源码启动或使用 Docker。
2. [使用指南](./zh-CN/user-guide.md)：配置模型、创建项目、运行节点、管理历史和剪辑视频。
3. [工作流指南](./zh-CN/workflows.md)：使用内置模板或搭建可复用流程。
4. [模型配置与协议接入](./zh-CN/model-providers.md)：配置 LLM/媒体 Provider、使用统一模型适配器、编写协议并完成验证与排障。

## 开发者文档

- [项目结构](./zh-CN/architecture.md)：前端、后端、Agent、节点、工作流和数据目录。
- [开发与测试](./zh-CN/development.md)：本地开发命令、测试要求和贡献边界。
- [桌面打包](./zh-CN/desktop-packaging.md)：Windows、Linux、macOS 构建与发布。
- [Workflow Spec 协议](./zh-CN/workflow-spec-protocol.md)：可移植工作流的作者层和运行时合同。
- [Agent 质量验收](./zh-CN/agent-quality.md)：真实工具调用、状态一致性和用户体验检查。

## 文档边界

公开文档只描述用户需要的产品行为、安装方式、稳定协议和贡献方法。阶段性实施计划、迁移记录、临时排障笔记和本地运行数据不进入公开文档导航。`apps/api/app/skills/` 下的 Markdown 是 Agent 运行时 Skill 源码，不是普通用户教程。
