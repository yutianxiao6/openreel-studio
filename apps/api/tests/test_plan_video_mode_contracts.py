import pytest

from app.agent import slash_commands
from app.agent.video_mode import build_video_mode_system_reminder
from app.mcp_tools import node_universal


def test_video_mode_reminder_is_coarse_state_only():
    reminder = build_video_mode_system_reminder({"project_mode": "video_production"})

    assert "视频制作" in reminder
    assert "节点字段" in reminder
    assert "references" in reminder
    assert "depends_on" in reminder
    assert "grid" not in reminder
    assert "frames" not in reminder
    assert "story_template" not in reminder


@pytest.mark.asyncio
async def test_project_management_slash_command_is_removed(monkeypatch):
    async def fake_save_message(*args, **kwargs):
        return None

    async def fake_emit_text(*args, **kwargs):
        return None

    monkeypatch.setattr(slash_commands, "_save_message", fake_save_message)
    monkeypatch.setattr(slash_commands, "_emit_text", fake_emit_text)

    events = [
        event async for event in slash_commands.slash_command_events(
            "project-1",
            "/project new 新片场",
        )
    ]

    slash = next(event for event in events if event["type"] == "slash_command")
    assert slash["ok"] is False
    assert slash["error"] == "unknown_command"
    assert events[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_node_creation_guide_only_exposes_generic_types(monkeypatch):
    async def fake_read_state(project_id: str):
        return {"project_mode": "single_node", "guide_loaded": {}}

    async def fake_write_patch(project_id: str, patch: dict):
        return None

    monkeypatch.setattr(node_universal, "_read_project_state", fake_read_state)
    monkeypatch.setattr(node_universal, "_write_project_state_patch", fake_write_patch)

    ok = await node_universal.node_get_creation_guide("proj-1", "image")
    bad = await node_universal.node_get_creation_guide("proj-1", "unsupported")

    assert ok["ok"] is True
    assert ok["type"] == "image"
    assert ok["required_fields"] == ["prompt", "aspect_ratio", "resolution"]
    assert ok["call_example"]["args"]["fields"]["resolution"] == "1080x1920"
    assert bad["ok"] is False
    assert bad["valid_types"] == ["text", "image", "video", "audio"]
