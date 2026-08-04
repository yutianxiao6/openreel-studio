"""Factory prompt section with the small cache-friendly runtime summary."""

from __future__ import annotations

NAME = "runtime_context"
TRIGGER = "factory"
ORDER = 900


def _project_title(state: dict) -> str:
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return "未命名项目"
    title = " ".join(str(metadata.get("title") or "").split())[:120]
    return title or "未命名项目"


def build(
    state: dict,
    model_configs: list[dict] | None = None,
    user_facts: list[dict] | None = None,
    project_facts: list[dict] | None = None,
    latest_user_message: str = "",
    **_: object,
) -> str:
    return "\n".join([
        "## 运行时上下文",
        f"项目标题:{_project_title(state)!r}",
    ])
