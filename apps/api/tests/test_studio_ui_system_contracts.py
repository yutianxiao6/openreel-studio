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


def test_project_workspace_restores_chat_resize_and_video_editor_escapes_workspace_stack() -> None:
    project = read("app/projects/[projectId]/page.tsx")
    editor = read("components/canvas/VideoEditPanel.tsx")

    assert 'role="separator"' in project
    assert 'aria-label="调整聊天区宽度"' in project
    assert "setPointerCapture(event.pointerId)" in project
    assert "event.clientX - chatLeft" in project
    assert 'window.localStorage.setItem(LS_CHAT_WIDTH' in project
    assert 'import { createPortal } from "react-dom"' in editor
    assert "), document.body)" in editor
    assert editor.index('data-openreel-frame-strip="true"') < editor.index("return createPortal((")
    assert editor.index("return createPortal((") < editor.index('className="openreel-video-edit-panel')


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
    assert "@container (max-width: 760px)" in styles
    assert "@container workflow-editor (max-width: 1040px)" in styles
    assert "container: workflow-editor / inline-size" in styles
    assert ".openreel-workflow-toolbar-fields" in styles
    assert "requestAnimationFrame" in workflow
    assert 'className="openreel-workflow-graph h-full w-full"' in workflow
    assert 'className="openreel-workflow-editor' in workflow
    assert 'data-workflow-action="success"' in workflow
    assert "openreel-workflow-body" in workflow
    assert "openreel-workflow-editor-dismiss-layer" in workflow
    assert "openreel-workflow-dock-dismiss-layer" in workflow
    assert "openreel-workflow-dock-trigger" in workflow
    assert 'className="absolute bottom-5 left-0 right-0 z-40 mx-auto w-fit"' in workflow
    assert 'left-4 right-4 mx-auto w-[min(760px,calc(100%-32px))]' in workflow
    assert "dragFrameRef" in workflow
    assert "graphPanningRef" in workflow
    assert "onMoveStart={handleGraphMoveStart}" in workflow
    assert "onMoveEnd={handleGraphMoveEnd}" in workflow
    assert "onPointerDownCapture={handleGraphPointerDownCapture}" in workflow
    assert 'window.addEventListener("pointerup", finishGraphPointerInteraction' in workflow
    assert 'surface.setAttribute("data-dragging", "true")' in workflow
    assert 'surface.removeAttribute("data-dragging")' in workflow
    assert 'layoutId="openreel-workflow-inspector-active-tab"' in workflow
    assert "studio-workspace-switcher-button" in tabs
