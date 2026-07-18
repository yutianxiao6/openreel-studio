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
    assert 'target.tagName.toLowerCase() === "textarea"' in editor
    assert '["text", "search", "email", "url", "tel", "password"]' in editor
    assert '["input", "textarea", "select"]' not in editor


def test_media_clock_stalls_fall_back_to_timeline_and_resync_the_media() -> None:
    editor = _editor_source()

    assert "MEDIA_CLOCK_STALL_GRACE_MS" in editor
    assert "timelineTime: timelineTime + deltaSeconds" in editor
    assert "(clockAdvance.stalled || mediaClockLagging)" in editor
    assert "sampledMediaElement.currentTime = expectedMediaTime" in editor
    preview_start = editor.index('data-openreel-preview-video="true"')
    preview_end = editor.index("\n                    />", preview_start)
    assert 'preload="auto"' in editor[preview_start:preview_end]


def test_video_clock_and_audio_recovery_do_not_block_primary_playback() -> None:
    editor = _editor_source()

    clock_start = editor.index("const playbackClockSource")
    clock_end = editor.index("const playAudioElementForClip", clock_start)
    clock_contract = editor[clock_start:clock_end]
    assert clock_contract.index('currentVideoClip && currentVideoItem?.type === "video"') < clock_contract.index(
        "activeAudioTransition?.outgoingItem"
    )
    assert "const startPrimaryPlaybackMedia" in editor
    assert "startPrimaryPlaybackMedia(nextStart)" in editor
    assert "onCanPlay={() =>" in editor
    assert "reportPreviewAudioPlaybackFailure" in editor
    assert "Promise.all(mediaStarts)" not in editor
    assert "if (!playing) currentTimeRef.current" in editor
    assert "startTransition(() => setCurrentTime(timelineTime))" in editor
    assert "updatePlaybackChrome(timelineTime)" in editor
    assert "playheadRef.current.dataset.playheadTime = safeTimelineTime.toFixed(6)" in editor
    assert "programTimecodeRef.current.textContent = formatFrameTimecode(frame, framesPerSecond)" in editor
    assert "function playbackPresentationKeyAtFrame" in editor
    assert "nextPresentationKey !== playbackPresentationKeyRef.current" in editor
    assert "(activeVideoTransition || activeAudioTransition) && now - lastUiCommit" in editor


def test_playhead_is_hard_clamped_to_the_visible_timeline() -> None:
    editor = _editor_source()

    assert "function clampPlaybackTimelineTime" in editor
    assert "timelineTime = clampPlaybackTimelineTime(timelineTime, playbackEnd)" in editor
    assert 'data-playhead-time={clampPlaybackTimelineTime(currentTime, playbackEnd).toFixed(6)}' in editor
    assert "TRACK_LABEL_WIDTH + clampPlaybackTimelineTime(currentTime, playbackEnd) * pxPerSecond" in editor
    assert 'window.addEventListener("pointercancel", onEnd)' in editor
    assert 'window.removeEventListener("pointercancel", onEnd)' in editor


def test_full_source_sequences_adopt_the_indexed_media_frame_rate() -> None:
    editor = _editor_source()

    assert "function shouldAdoptPrimarySourceFrameRate" in editor
    assert "videoClip.durationFrames !== sourceFrameCount" in editor
    assert "audioClip.durationFrames === sourceFrameCount" in editor
    assert "frameRatesMatch(currentFrameRate, sourceFrameRate)" in editor
    assert "const primaryIndex = mediaIndexes[nodeId]" in editor
    assert "sourceFrameRate: primaryIndex.frame_rate" in editor


def test_new_video_sequences_wait_for_the_real_media_index() -> None:
    editor = _editor_source()

    assert 'if (primary.type === "video" && !primaryIndex) return' in editor
    assert "if (primaryIndex) setSequenceFrameRate(primaryIndex.frame_rate)" in editor
    assert "Math.round(duration * initializationFps)" in editor
    assert "sequenceRevisionRef.current === 0" not in editor
