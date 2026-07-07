import pytest

from app.agent import slash_commands
from app.agent.collaboration_mode import (
    COLLABORATION_MODE_STATE_KEY,
    MODE_DEFAULT,
    MODE_PLAN,
    MODE_WORKFLOW_BUILD,
    collaboration_mode_patch,
    current_collaboration_mode,
    is_workflow_build_mode,
)


def test_collaboration_mode_helpers_support_workflow_build() -> None:
    assert current_collaboration_mode({}) == MODE_DEFAULT
    assert current_collaboration_mode({COLLABORATION_MODE_STATE_KEY: MODE_PLAN}) == MODE_PLAN
    assert current_collaboration_mode({COLLABORATION_MODE_STATE_KEY: MODE_WORKFLOW_BUILD}) == MODE_WORKFLOW_BUILD
    assert current_collaboration_mode({COLLABORATION_MODE_STATE_KEY: "unknown"}) == MODE_DEFAULT
    assert collaboration_mode_patch(MODE_WORKFLOW_BUILD) == {COLLABORATION_MODE_STATE_KEY: MODE_WORKFLOW_BUILD}
    assert collaboration_mode_patch("bad-mode") == {COLLABORATION_MODE_STATE_KEY: MODE_DEFAULT}
    assert is_workflow_build_mode({COLLABORATION_MODE_STATE_KEY: MODE_WORKFLOW_BUILD}) is True
    assert is_workflow_build_mode({COLLABORATION_MODE_STATE_KEY: MODE_PLAN}) is False


@pytest.mark.asyncio
async def test_slash_workflow_enters_workflow_build_mode(monkeypatch) -> None:
    updates: list[tuple[str, dict]] = []
    emitted: list[tuple[str, bool]] = []

    async def fake_project_update_state(project_id: str, patch: dict):
        updates.append((project_id, patch))
        return {"ok": True}

    async def fake_emit_text(project_id: str, command, text: str, ok: bool = True):
        emitted.append((text, ok))
        return None

    monkeypatch.setattr(slash_commands, "project_update_state", fake_project_update_state)
    monkeypatch.setattr(slash_commands, "_emit_text", fake_emit_text)

    events = [
        event async for event in slash_commands._workflow_events(
            "project-1",
            slash_commands.SlashCommand(name="workflow", args=[], raw="/workflow"),
        )
    ]

    assert updates == [("project-1", {COLLABORATION_MODE_STATE_KEY: MODE_WORKFLOW_BUILD})]
    assert events[0] == {
        "type": "mode_updated",
        "ok": True,
        "mode": MODE_WORKFLOW_BUILD,
        "collaboration_mode": MODE_WORKFLOW_BUILD,
    }
    assert events[1]["action"] == "enter"
    assert events[-1]["status"] == "completed"
    assert emitted and emitted[0][1] is True


@pytest.mark.asyncio
async def test_slash_workflow_exit_returns_to_default_mode(monkeypatch) -> None:
    updates: list[tuple[str, dict]] = []

    async def fake_project_update_state(project_id: str, patch: dict):
        updates.append((project_id, patch))
        return {"ok": True}

    async def fake_emit_text(*args, **kwargs):
        return None

    monkeypatch.setattr(slash_commands, "project_update_state", fake_project_update_state)
    monkeypatch.setattr(slash_commands, "_emit_text", fake_emit_text)

    events = [
        event async for event in slash_commands._workflow_events(
            "project-1",
            slash_commands.SlashCommand(name="workflow", args=["exit"], raw="/workflow exit"),
        )
    ]

    assert updates == [("project-1", {COLLABORATION_MODE_STATE_KEY: MODE_DEFAULT})]
    assert events[0]["mode"] == MODE_DEFAULT
    assert events[1]["action"] == "exit"
    assert events[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_slash_workflow_rejects_unknown_actions(monkeypatch) -> None:
    updates: list[tuple[str, dict]] = []

    async def fake_project_update_state(project_id: str, patch: dict):
        updates.append((project_id, patch))
        return {"ok": True}

    async def fake_emit_text(*args, **kwargs):
        return None

    monkeypatch.setattr(slash_commands, "project_update_state", fake_project_update_state)
    monkeypatch.setattr(slash_commands, "_emit_text", fake_emit_text)

    events = [
        event async for event in slash_commands._workflow_events(
            "project-1",
            slash_commands.SlashCommand(name="workflow", args=["status"], raw="/workflow status"),
        )
    ]

    assert updates == []
    assert events[0]["ok"] is False
    assert events[0]["error"] == "invalid_workflow_action"
    assert events[-1]["status"] == "failed"
