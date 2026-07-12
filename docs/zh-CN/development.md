# 开发与测试

[English](../en/development.md) · [中文文档首页](../README.md)

## 开发环境

先按 [快速开始](./getting-started.md) 安装 Node.js、pnpm、Python 和 uv。

常用命令：

```bash
bash install.sh                 # 安装并初始化
pnpm dev                        # Web 开发服务器
pnpm api:dev                    # API 开发服务器
pnpm api:init-db                # 初始化数据库
pnpm -r typecheck               # TypeScript 检查
git diff --check                # 空白和补丁检查
```

后端测试：

```bash
cd apps/api
PYTHONPATH=. uv run pytest -q
```

前端界面改动除了 typecheck 和 build，还应在真实浏览器中覆盖修改状态并截图检查。

## 修改应该放在哪里

| 需求 | 优先位置 |
| --- | --- |
| 页面和交互 | `apps/web` |
| API 和持久化 | `apps/api/app/api`、`services`、`db` |
| Agent 调度 | `apps/api/app/agent` |
| 原子工具 | `apps/api/app/mcp_tools` |
| 视频制作知识 | Skill 或 workflow template |
| 媒体 HTTP 协议 | `config/*_provider_protocols/catalog.json` |
| 可复用工作流 | `workflow_templates/user` 或内置 Skill 模板 |

业务流程和提示词知识优先放进 Skill，不要把长教程写进每轮 system prompt。可以由 schema、validator 或权限策略确定的规则，优先用代码和测试保证。

## 提交前检查

1. 只保留本次任务相关修改。
2. 运行对应后端测试、前端 typecheck/build 和浏览器验证。
3. 执行 `git diff --check`。
4. 检查未跟踪文件。
5. 扫描 API Key、令牌、私钥、`.env`、运行数据和构建产物。
6. 确认 `data/`、`storage/`、本地截图和用户内容没有进入提交。

## 文档贡献

- 中文内容放在 `docs/zh-CN/`，英文内容放在 `docs/en/`。
- 根 `README.md` 是中文产品入口，`README.en.md` 是英文产品入口。
- 用户指南描述稳定产品行为；临时实施计划和排障记录不要放进主导航。
- `apps/api/app/skills/` 下的 Markdown 是运行时源码，修改会改变 Agent 行为，需要配套合同测试。
- 截图应来自真实界面，不包含 API Key、私人聊天、用户身份或无授权素材。

## Pull Request 建议

PR 说明应包含：

- 用户可见问题；
- 解决方式和边界；
- 修改文件；
- 测试命令和结果；
- 界面改动的真实截图；
- 已知限制和后续工作。

不要在 PR 中附上完整运行数据库、trace、模型响应原文或私有配置。
