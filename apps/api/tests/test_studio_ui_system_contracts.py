from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = PROJECT_ROOT / "apps" / "web"


def read(relative_path: str) -> str:
    return (WEB_ROOT / relative_path).read_text(encoding="utf-8")


def test_studio_shell_uses_shared_chrome_and_pointer_atmosphere() -> None:
    home = read("app/page.tsx")
    project = read("app/projects/[projectId]/page.tsx")
    atmosphere = read("components/workspace/StudioAtmosphere.tsx")
    header = read("components/workspace/StudioHeader.tsx")

    for source in (home, project):
        assert "<StudioAtmosphere />" in source
        assert "<StudioHeader" in source
        assert 'className="studio-shell' in source

    assert "requestAnimationFrame" in atmosphere
    assert "ResizeObserver" in atmosphere
    assert 'closest<HTMLElement>(".studio-shell")' in atmosphere
    assert "prefers-reduced-motion" in atmosphere
    assert 'className="studio-topbar"' in header
    assert "studio-connection-pill" in header


def test_studio_visual_system_covers_primary_product_surfaces() -> None:
    styles = read("app/globals.css")
    required_selectors = (
        ".studio-topbar",
        ".studio-session-drawer",
        ".studio-chat-surface",
        ".studio-composer",
        ".studio-canvas-shell",
        ".openreel-smart-node-card",
        ".openreel-node-detail-panel",
        ".studio-settings-dialog",
        ".openreel-video-edit-panel",
        ".openreel-image-edit-panel",
        ".openreel-history-drawer",
        ".openreel-panorama-viewer",
        ".studio-action-card",
    )
    for selector in required_selectors:
        assert selector in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "studio-gradient-shift" in styles


def test_workspace_and_content_transitions_use_motion_contracts() -> None:
    tabs = read("components/workspace/WorkspaceViewTabs.tsx")
    chat = read("components/chat/ChatPanel.tsx")
    canvas = read("components/canvas/WorkflowCanvas.tsx")
    nodes = read("components/canvas/SmartNode.tsx")

    assert 'layoutId="openreel-workspace-active-tab"' in tabs
    assert 'type: "spring"' in tabs
    assert "studio-suggestion-chip" in chat
    assert "studio-message-bubble" in chat
    assert "<Background" in canvas
    assert "studio-canvas-empty" in canvas
    assert 'data-node-status={status}' in nodes


def test_chat_autoscroll_is_scoped_to_the_message_container() -> None:
    chat = read("components/chat/ChatPanel.tsx")

    assert "container.scrollTo({ top: container.scrollHeight, behavior })" in chat
    assert "messagesEndRef" not in chat
    assert "scrollIntoView({ behavior: \"smooth\" })" not in chat


def test_button_system_and_workflow_editor_use_advanced_interaction_layers() -> None:
    styles = read("app/globals.css")
    workflow = read("components/canvas/WorkflowCanvas.tsx")
    tabs = read("components/workspace/WorkspaceViewTabs.tsx")

    assert ".studio-shell button:not(:disabled):active" in styles
    assert "workflow-button-glint" in styles
    assert ".openreel-workflow-toolbar [data-workflow-action=\"primary\"]" in styles
    assert ".openreel-workflow-library button:hover:not(:disabled)" in styles
    assert ".openreel-workflow-graph::after" in styles
    assert ".openreel-workflow-node.is-selected" in styles
    assert "requestAnimationFrame" in workflow
    assert 'className="openreel-workflow-graph h-full w-full"' in workflow
    assert 'className="openreel-workflow-editor' in workflow
    assert 'data-workflow-action="success"' in workflow
    assert "studio-workspace-switcher-button" in tabs
