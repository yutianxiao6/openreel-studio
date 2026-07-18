from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_windows_packaging_waits_for_windowless_api_smoke_test() -> None:
    script = (PROJECT_ROOT / "scripts" / "desktop" / "build-windows.ps1").read_text(
        encoding="utf-8"
    )

    smoke_start = script.index('$env:OPENREEL_PACKAGING_SMOKE = "1"')
    catalog_check = script.index("foreach ($ProtocolDirName", smoke_start)
    smoke_block = script[smoke_start:catalog_check]

    assert "Start-Process" in smoke_block
    assert "-Wait" in smoke_block
    assert "-PassThru" in smoke_block
    assert "$SmokeProcess.ExitCode" in smoke_block
    assert "Invoke-Native" not in smoke_block


def test_desktop_runtime_data_stays_beside_the_packaged_application() -> None:
    main = (PROJECT_ROOT / "apps" / "desktop" / "src" / "main.cjs").read_text(
        encoding="utf-8"
    )

    data_root_start = main.index("function desktopDataRoot()")
    data_root_end = main.index("\n}\n", data_root_start)
    data_root = main[data_root_start:data_root_end]
    assert "const installRoot = packagedInstallRoot()" in data_root
    assert "return installRoot" in data_root
    assert "migrateLegacyInstallData" not in main
    assert "migrateAppDataBackToInstall(fixedUserDataDir(), root)" in main


def test_windows_installer_keeps_runtime_directories_in_place() -> None:
    installer = (PROJECT_ROOT / "apps" / "desktop" / "build" / "installer.nsh").read_text(
        encoding="utf-8"
    )

    assert "OpenReelDetectInPlaceInstall" in installer
    assert "OpenReelBypassOldUninstallerForInPlaceInstall" in installer
    assert 'RMDir /r "$INSTDIR\\data"' not in installer
    assert 'RMDir /r "$INSTDIR\\storage"' not in installer
    assert 'RMDir /r "$INSTDIR\\assets"' not in installer
    assert 'RMDir /r "$INSTDIR\\config"' not in installer
    assert 'Delete "$INSTDIR\\*.json"' not in installer
    assert 'Delete "$INSTDIR\\*.dat"' not in installer
    assert 'Delete "$INSTDIR\\*.dll"' not in installer
    assert 'Delete "$INSTDIR\\*.pak"' not in installer
    assert 'RMDir /r "$INSTDIR"' not in installer
    assert "openreel-upgrade-backup" not in installer
    assert "Rename \"$INSTDIR" not in installer


def test_video_editor_uses_native_full_resolution_preview_on_windows() -> None:
    editor = (
        PROJECT_ROOT / "apps" / "web" / "components" / "canvas" / "VideoEditPanel.tsx"
    ).read_text(encoding="utf-8")

    assert "requiresCanvasVideoPreview" not in editor
    assert 'data-video-preview-engine={playbackResolution === "full" ? "native" : "canvas"}' in editor
    preview_start = editor.index('data-openreel-preview-video="true"')
    preview_end = editor.index("\n                    />", preview_start)
    preview_video = editor[preview_start:preview_end]
    assert "crossOrigin=" not in preview_video
    assert 'playbackResolution === "full"' in preview_video
    assert "onError=" in preview_video

    audio_graph_start = editor.index("const elements: Array<HTMLMediaElement | null> = [")
    audio_graph_end = editor.index("]", audio_graph_start)
    assert "videoRef.current" not in editor[audio_graph_start:audio_graph_end]


def test_packaged_media_downloads_share_one_persistent_directory_action() -> None:
    main = (PROJECT_ROOT / "apps" / "desktop" / "src" / "main.cjs").read_text(
        encoding="utf-8"
    )
    preload = (PROJECT_ROOT / "apps" / "desktop" / "src" / "preload.cjs").read_text(
        encoding="utf-8"
    )
    api = (PROJECT_ROOT / "apps" / "web" / "lib" / "api.ts").read_text(
        encoding="utf-8"
    )
    canvas = (
        PROJECT_ROOT / "apps" / "web" / "components" / "canvas" / "WorkflowCanvas.tsx"
    ).read_text(encoding="utf-8")
    chat = (
        PROJECT_ROOT / "apps" / "web" / "components" / "chat" / "ChatPanel.tsx"
    ).read_text(encoding="utf-8")
    settings = (
        PROJECT_ROOT / "apps" / "web" / "components" / "settings" / "SettingsModal.tsx"
    ).read_text(encoding="utf-8")
    feedback = (
        PROJECT_ROOT / "apps" / "web" / "components" / "common" / "DownloadFeedback.tsx"
    ).read_text(encoding="utf-8")
    layout = (PROJECT_ROOT / "apps" / "web" / "app" / "layout.tsx").read_text(
        encoding="utf-8"
    )

    assert 'ipcMain.handle("openreel:save-media"' in main
    assert 'ipcMain.handle("openreel:get-media-download-directory"' in main
    assert 'ipcMain.handle("openreel:choose-media-download-directory"' in main
    assert 'path.join(fixedUserDataDir(), "media-download.json")' in main
    assert 'properties: ["openDirectory", "createDirectory"]' in main
    assert "uniqueMediaDownloadPath" in main
    assert "showSaveDialog" not in main

    assert 'ipcRenderer.invoke("openreel:save-media"' in preload
    assert 'ipcRenderer.invoke("openreel:get-media-download-directory"' in preload
    assert 'ipcRenderer.invoke("openreel:choose-media-download-directory"' in preload

    desktop_branch = api[api.index("export async function saveMediaFile"):]
    assert "window.openReelDesktop?.saveMedia" in desktop_branch
    assert 'document.createElement("a")' in desktop_branch
    assert "fetch(resolvedUrl" not in desktop_branch
    assert "await saveMediaFile(url, filename)" in canvas
    assert 'mediaKind?: "image" | "video"' in canvas
    assert chat.count("await saveMediaFile(url, assetBasename(item.path || item.title))") == 2
    assert 'label: "下载位置"' in settings
    assert 'desktop && tab === "download"' in settings
    assert 'MEDIA_DOWNLOAD_EVENT = "openreel:media-download"' in api
    assert 'status: "started"' in api
    assert 'status: "completed"' in api
    assert "正在下载…" in feedback
    assert "下载完成" in feedback
    assert "已交给浏览器下载" in feedback
    assert "<DownloadFeedback />" in layout
