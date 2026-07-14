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
