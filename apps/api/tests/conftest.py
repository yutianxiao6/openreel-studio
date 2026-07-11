"""Shared pytest isolation for repository-owned runtime libraries."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import context_compact, workflow_template_store


@pytest.fixture(autouse=True)
def isolate_user_workflow_templates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep local user templates from changing deterministic test results."""
    template_root = tmp_path / "workflow_templates"
    monkeypatch.setattr(
        workflow_template_store,
        "workflow_template_library_root",
        lambda: template_root,
    )
    tool_results_root = tmp_path / "tool_results"
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tool_results_root)
