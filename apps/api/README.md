# OpenReel Studio API

English · [简体中文](./README.zh-CN.md)

FastAPI backend for projects, chat streaming, agents, nodes, workflows, assets, media generation, configuration, and diagnostics.

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

Run tests from this directory:

```bash
PYTHONPATH=. uv run pytest -q
```

See the repository [architecture](../../docs/en/architecture.md) and [development guide](../../docs/en/development.md) for the complete system context.
