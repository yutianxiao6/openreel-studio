from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
EDITOR_PATH = (
    PROJECT_ROOT / "apps" / "web" / "components" / "canvas" / "VideoEditPanel.tsx"
)


def _editor_source() -> str:
    return EDITOR_PATH.read_text(encoding="utf-8")


def test_spacebar_remains_exclusive_to_playback_after_clicking_editor_buttons() -> None:
    editor = _editor_source()

    assert "if (event.detail > 0) releaseEditorButtonFocus(event.target)" in editor
    assert "releaseEditorButtonFocus(document.activeElement)" in editor
    assert "event.stopImmediatePropagation()" in editor
    assert "if (isEditableKeyboardTarget(event.target)) return" in editor


def test_media_clock_stalls_fall_back_to_timeline_and_resync_the_media() -> None:
    editor = _editor_source()

    assert "MEDIA_CLOCK_STALL_GRACE_MS" in editor
    assert "timelineTime: timelineTime + deltaSeconds" in editor
    assert "(clockAdvance.stalled || mediaClockLagging)" in editor
    assert "sampledMediaElement.currentTime = expectedMediaTime" in editor
    preview_start = editor.index('data-openreel-preview-video="true"')
    preview_end = editor.index("\n                    />", preview_start)
    assert 'preload="auto"' in editor[preview_start:preview_end]
