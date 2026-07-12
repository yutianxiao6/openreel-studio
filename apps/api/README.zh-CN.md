# OpenReel Studio API

[English](./README.md) · 简体中文

这是项目的 FastAPI 后端，负责项目、聊天流、Agent、节点、工作流、资产、媒体生成、配置和诊断。

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

在当前目录运行测试：

```bash
PYTHONPATH=. uv run pytest -q
```

完整说明见 [项目结构](../../docs/zh-CN/architecture.md) 和 [开发与测试](../../docs/zh-CN/development.md)。
