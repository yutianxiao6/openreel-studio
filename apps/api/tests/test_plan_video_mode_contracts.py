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
async def test_slash_project_new_creates_and_switches(monkeypatch):
    saved: list[tuple[str, str, str]] = []

    async def fake_create_project(title: str):
        return {"id": "project-new", "title": title, "status": "active"}

    async def fake_save_message(project_id: str, role: str, content: str, metadata=None):
        saved.append((project_id, role, content))

    monkeypatch.setattr(slash_commands, "_create_project_record", fake_create_project)
    monkeypatch.setattr(slash_commands, "_save_message", fake_save_message)

    events = [
        event async for event in slash_commands._project_events(
            "project-old",
            slash_commands.SlashCommand(name="project", args=["new", "新片场"], raw="/project new 新片场"),
        )
    ]

    assert events[0]["type"] == "project_switch"
    assert events[0]["project_id"] == "project-new"
    assert events[1]["command"] == "project"
    assert events[1]["action"] == "new"
    assert events[-1]["status"] == "completed"
    assert ("project-new", "user", "/project new 新片场") in saved


@pytest.mark.asyncio
async def test_slash_project_delete_requires_confirmation(monkeypatch):
    async def fake_list_projects():
        return [{"id": "project-1", "title": "旧项目"}]

    async def fake_store_pending(project_id: str, target: dict):
        return {"target_project_id": target["id"], "target_title": target["title"], "expires_at": "later"}

    async def fake_emit_text(*args, **kwargs):
        return None

    monkeypatch.setattr(slash_commands, "_list_project_records", fake_list_projects)
    monkeypatch.setattr(slash_commands, "_store_pending_project_delete", fake_store_pending)
    monkeypatch.setattr(slash_commands, "_emit_text", fake_emit_text)

    events = [
        event async for event in slash_commands._project_events(
            "project-1",
            slash_commands.SlashCommand(name="project", args=["delete"], raw="/project delete"),
        )
    ]

    confirm = next(event for event in events if event["type"] == "confirm_required")
    slash = next(event for event in events if event["type"] == "slash_command")
    assert confirm["action"] == "delete_project"
    assert confirm["target_project_id"] == "project-1"
    assert slash["requires_confirm"] is True
    assert events[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_slash_project_delete_current_switches_to_next_project(monkeypatch):
    saved: list[tuple[str, str, str]] = []

    async def fake_read_state(project_id: str):
        return None, {
            "_pending_project_delete_confirm": {
                "target_project_id": "project-1",
                "target_title": "旧项目",
                "expires_at": "2999-01-01T00:00:00",
            }
        }

    async def fake_delete_project(project_id: str):
        return {"ok": True, "project_id": project_id}

    async def fake_next_project(project_id: str):
        return {"id": "project-2", "title": "新项目"}

    async def fake_save_message(project_id: str, role: str, content: str, metadata=None):
        saved.append((project_id, role, content))

    monkeypatch.setattr(slash_commands, "_read_state", fake_read_state)
    monkeypatch.setattr(slash_commands, "_delete_project_record", fake_delete_project)
    monkeypatch.setattr(slash_commands, "_next_project_after_delete", fake_next_project)
    monkeypatch.setattr(slash_commands, "_save_message", fake_save_message)

    events = [
        event async for event in slash_commands._project_events(
            "project-1",
            slash_commands.SlashCommand(name="project", args=["delete", "confirm"], raw="/project delete confirm"),
        )
    ]

    switch = next(event for event in events if event["type"] == "project_switch")
    slash = next(event for event in events if event["type"] == "slash_command")
    assert switch["project_id"] == "project-2"
    assert switch["deleted_project_id"] == "project-1"
    assert slash["ok"] is True
    assert slash["result"]["deleted_current"] is True
    assert ("project-2", "user", "/project delete confirm") in saved


@pytest.mark.asyncio
async def test_node_creation_guide_only_exposes_generic_types(monkeypatch):
    async def fake_read_state(project_id: str):
        return {"project_mode": "single_node", "guide_loaded": {}}

    async def fake_write_patch(project_id: str, patch: dict):
        return None

    monkeypatch.setattr(node_universal, "_read_project_state", fake_read_state)
    monkeypatch.setattr(node_universal, "_write_project_state_patch", fake_write_patch)

    ok = await node_universal.node_get_creation_guide("proj-1", "image")
    bad = await node_universal.node_get_creation_guide("proj-1", "segment_storyboard")

    assert ok["ok"] is True
    assert ok["type"] == "image"
    assert ok["required_fields"] == ["prompt", "aspect_ratio", "resolution"]
    assert ok["call_example"]["args"]["fields"]["resolution"] == "2560x1440"
    assert bad["ok"] is False
    assert bad["valid_types"] == ["text", "image", "video", "audio"]
