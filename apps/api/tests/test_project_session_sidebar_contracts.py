from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent import orchestrator
from app.api import routes_projects
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


@pytest.mark.asyncio
async def test_plugin_project_creation_can_activate_and_reload_the_open_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []

    class CreatedProject:
        id = "project-new"
        title = "插件新项目"

        def model_dump(self) -> dict[str, object]:
            return {"id": self.id, "title": self.title}

    class FakeProjectService:
        def __init__(self, _db: object) -> None:
            pass

        async def create_project(self, *, title: str) -> CreatedProject:
            assert title == "插件新项目"
            return CreatedProject()

    async def capture(source_project_id: str, project: dict[str, object]) -> int:
        emitted.append((source_project_id, project))
        return 2

    monkeypatch.setattr(routes_projects, "ProjectService", FakeProjectService)
    monkeypatch.setattr(routes_projects, "_emit_project_ui_switch", capture)

    result = await routes_projects.create_project(
        CreateProjectRequest(title="插件新项目"),
        db=object(),
        activate_ui=True,
        source_project_id="project-old",
    )

    assert result == {"id": "project-new", "title": "插件新项目"}
    assert emitted == [("project-old", result)]

    chat_panel = (
        PROJECT_ROOT / "apps" / "web" / "components" / "chat" / "ChatPanel.tsx"
    ).read_text(encoding="utf-8")
    web_api = (PROJECT_ROOT / "apps" / "web" / "lib" / "api.ts").read_text(
        encoding="utf-8"
    )
    assert "event.refresh_page === true" in chat_panel
    assert "window.location.assign(`/projects/${encodeURIComponent(newId)}`)" in chat_panel
    assert "refresh_page?: boolean" in web_api


@pytest.mark.asyncio
async def test_plugin_project_selection_activates_the_open_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []

    class ExistingProject:
        id = "project-target"
        title = "目标项目"

        def model_dump(self) -> dict[str, object]:
            return {"id": self.id, "title": self.title}

    class FakeProjectService:
        def __init__(self, _db: object) -> None:
            pass

        async def get_project(self, project_id: str) -> ExistingProject | None:
            assert project_id == "project-target"
            return ExistingProject()

    async def capture(source_project_id: str, project: dict[str, object]) -> int:
        emitted.append((source_project_id, project))
        return 3

    monkeypatch.setattr(routes_projects, "ProjectService", FakeProjectService)
    monkeypatch.setattr(routes_projects, "_emit_project_ui_switch", capture)

    result = await routes_projects.activate_project_ui(
        "project-target",
        db=object(),
        source_project_id="project-browser",
    )

    assert result == {
        "ok": True,
        "project": {"id": "project-target", "title": "目标项目"},
        "ui_activation": {
            "requested": True,
            "refresh_page": True,
            "subscribers_notified": 3,
        },
    }
    assert emitted == [("project-browser", result["project"])]


@pytest.mark.asyncio
async def test_external_project_switch_reaches_every_open_project_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "_project_subscribers", {})
    first = orchestrator._add_subscriber("project-browser-a")
    second = orchestrator._add_subscriber("project-browser-b")
    event = {
        "type": "project_switch",
        "project_id": "project-target",
        "refresh_page": True,
    }

    delivered = await orchestrator.emit_project_ui_event(event)

    assert delivered == 2
    assert first.get_nowait() == event
    assert second.get_nowait() == event
