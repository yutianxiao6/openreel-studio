from pathlib import Path

import pytest

from app.agent.permission_policy import ToolPermissionContext, decide_tool_permission
from app.mcp_tools import file_tools, tool_meta_tools
from app.mcp_tools.registry import registry


@pytest.mark.asyncio
async def test_workspace_file_tools_cover_read_search_write_patch_delete(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(file_tools.settings, "PROJECT_ROOT", str(tmp_path))
    source = tmp_path / "apps" / "api" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("class AgentOrchestrator:\n    pass\n", encoding="utf-8")

    listed = await file_tools.workspace_list(path="apps", recursive=True)
    assert listed["ok"] is True
    assert {entry["path"] for entry in listed["entries"]} >= {"apps/api", "apps/api/example.py"}

    found = await file_tools.workspace_search(query="AgentOrchestrator", glob="*.py")
    assert found["ok"] is True
    assert found["matches"] == [
        {
            "path": "apps/api/example.py",
            "match_type": "content",
            "line_number": 1,
            "preview": "class AgentOrchestrator:",
        }
    ]

    read = await file_tools.workspace_read(path="apps/api/example.py", offset=1, limit=1)
    assert read["ok"] is True
    assert read["content"] == "class AgentOrchestrator:"

    written = await file_tools.workspace_write(path="tmp/notes.txt", content="old value\n")
    assert written["ok"] is True
    assert (tmp_path / "tmp" / "notes.txt").read_text(encoding="utf-8") == "old value\n"

    patched = await file_tools.workspace_patch(
        path="tmp/notes.txt",
        old_text="old",
        new_text="new",
    )
    assert patched["ok"] is True
    assert (tmp_path / "tmp" / "notes.txt").read_text(encoding="utf-8") == "new value\n"

    deleted = await file_tools.workspace_delete(path="tmp/notes.txt")
    assert deleted == {"ok": True, "path": "tmp/notes.txt", "deleted": True, "recursive": False}
    assert not (tmp_path / "tmp" / "notes.txt").exists()


@pytest.mark.asyncio
async def test_workspace_file_tools_reject_escape_and_git_mutation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(file_tools.settings, "PROJECT_ROOT", str(tmp_path))

    escaped = await file_tools.workspace_read(path="../outside.txt")
    assert escaped["ok"] is False
    assert escaped["error_kind"] == "workspace_path_denied"

    git_write = await file_tools.workspace_write(path=".git/config", content="[core]\n")
    assert git_write["ok"] is False
    assert git_write["error_kind"] == "workspace_path_denied"

    root_delete = await file_tools.workspace_delete(path=".")
    assert root_delete["ok"] is False
    assert root_delete["error_kind"] == "workspace_path_denied"


@pytest.mark.asyncio
async def test_workspace_file_tools_are_deferred_and_discoverable() -> None:
    visible = registry.core_agent_tool_names()
    workspace_tools = {
        "file.workspace_delete",
        "file.workspace_list",
        "file.workspace_patch",
        "file.workspace_read",
        "file.workspace_search",
        "file.workspace_write",
    }

    assert not workspace_tools & visible
    for name in workspace_tools:
        spec = registry.get(name)
        assert spec is not None, name
        assert registry.tool_exposure(name) == "deferred", name

    result = await tool_meta_tools.tool_search(query="workspace write file", category="file", limit=12)
    names = {item["name"] for item in result["tools"]}
    assert workspace_tools <= names


def test_workspace_file_tools_follow_deferred_file_permission_boundary() -> None:
    direct = decide_tool_permission(ToolPermissionContext(
        tool_name="file.workspace_write",
        state={},
        user_message="写一个文件",
        tool_args={"path": "tmp/notes.txt", "content": "hello"},
    ))
    via_deferred = decide_tool_permission(ToolPermissionContext(
        tool_name="file.workspace_write",
        state={},
        user_message="写一个文件",
        tool_args={"path": "tmp/notes.txt", "content": "hello"},
        via_tool_execute=True,
    ))

    assert direct.allowed is False
    assert direct.result and direct.result["error_kind"] == "deferred_tool_must_use_tool_execute"
    assert via_deferred.allowed is True
