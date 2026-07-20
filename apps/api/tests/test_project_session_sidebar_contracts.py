from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.routes_projects import CreateProjectRequest, UpdateProjectRequest
from app.mcp_tools.project_tools import project_create
from app.services.project_service import _initial_state


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_project_management_uses_the_left_session_sidebar() -> None:
    sidebar = (
        PROJECT_ROOT
        / "apps"
        / "web"
        / "components"
        / "project"
        / "ProjectSessionSidebar.tsx"
    ).read_text(encoding="utf-8")
    home = (PROJECT_ROOT / "apps" / "web" / "app" / "page.tsx").read_text(
        encoding="utf-8"
    )
    project_page = (
        PROJECT_ROOT
        / "apps"
        / "web"
        / "app"
        / "projects"
        / "[projectId]"
        / "page.tsx"
    ).read_text(encoding="utf-8")

    assert 'data-openreel-project-sidebar="true"' in sidebar
    assert 'data-openreel-project-session-list="true"' in sidebar
    assert 'data-openreel-project-create="true"' in sidebar
    assert 'data-openreel-project-delete-confirm="true"' in sidebar
    assert 'data-openreel-project-multi-select="true"' in sidebar
    assert 'data-openreel-project-multi-actions="true"' in sidebar
    assert "const [expanded, setExpanded] = useState(false)" in sidebar
    assert "LS_SIDEBAR_EXPANDED" not in sidebar
    assert "deleteSelectedProjects" in sidebar
    assert "api.createProject" in sidebar
    assert "api.deleteProject" in sidebar
    assert "router.push(path)" in sidebar
    assert "router.replace(path)" in sidebar
    assert "<ProjectSessionSidebar />" in home
    assert "<ProjectSessionSidebar />" in project_page


def test_project_crud_is_absent_from_slash_command_surfaces() -> None:
    slash_menu = (
        PROJECT_ROOT / "apps" / "web" / "components" / "chat" / "SlashMenu.tsx"
    ).read_text(encoding="utf-8")
    chat_panel = (
        PROJECT_ROOT / "apps" / "web" / "components" / "chat" / "ChatPanel.tsx"
    ).read_text(encoding="utf-8")
    slash_backend = (
        PROJECT_ROOT / "apps" / "api" / "app" / "agent" / "slash_commands.py"
    ).read_text(encoding="utf-8")

    assert 'name: "/project"' not in slash_menu
    assert 'name: "/project new"' not in slash_menu
    assert 'name: "/project delete"' not in slash_menu
    assert "buildProjectSlashCompletions" not in chat_panel
    assert '"project", "help"' not in slash_backend
    assert "async def _project_events" not in slash_backend
    assert "async def _project_delete_events" not in slash_backend


def test_project_sidebar_uses_compact_list_and_rest_delete_api() -> None:
    web_api = (PROJECT_ROOT / "apps" / "web" / "lib" / "api.ts").read_text(
        encoding="utf-8"
    )
    routes = (
        PROJECT_ROOT / "apps" / "api" / "app" / "api" / "routes_projects.py"
    ).read_text(encoding="utf-8")

    assert "options.compact ? '?compact=true' : ''" in web_api
    assert "export async function deleteProject(projectId: string)" in web_api
    assert "method: 'DELETE'" in web_api
    assert "compact: bool = Query(default=False)" in routes
    assert 'exclude = {"state_json"} if compact else None' in routes


def test_project_session_creation_only_accepts_a_title() -> None:
    assert set(CreateProjectRequest.model_fields) == {"title"}
    assert set(UpdateProjectRequest.model_fields) == {"title"}
    assert list(project_create.__annotations__) == ["title", "return"]
    assert _initial_state("新会话")["metadata"] == {"title": "新会话"}

    with pytest.raises(ValidationError):
        CreateProjectRequest(title="旧参数", episode_count=12)


def test_frontend_new_session_calls_only_send_the_title() -> None:
    sources = [
        PROJECT_ROOT / "apps" / "web" / "app" / "page.tsx",
        PROJECT_ROOT / "apps" / "web" / "components" / "project" / "ProjectSessionSidebar.tsx",
        PROJECT_ROOT / "apps" / "web" / "components" / "chat" / "ChatPanel.tsx",
    ]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert 'genre: ""' not in text
        assert "episode_count: 1" not in text
        assert 'budget_level: "low"' not in text
