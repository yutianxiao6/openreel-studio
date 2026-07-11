import asyncio
import json
from pathlib import Path

import pytest

from app.config import settings
from app.db.models import WorkflowNode
from app.services import (
    media_operations,
    timeline_media_index,
    timeline_waveforms,
    video_sequence_renderer,
)
from app.services.video_edit_sequences import SequenceSpec


def _sequence_spec() -> SequenceSpec:
    return SequenceSpec.model_validate({
        "schema_version": "openreel.video_sequence.v1",
        "settings": {
            "frame_rate": {"numerator": 24, "denominator": 1},
            "width": 320,
            "height": 180,
            "audio_sample_rate": 48_000,
            "audio_channels": 2,
        },
        "tracks": [
            {"id": "v1", "kind": "video", "name": "Video 1", "order": 0},
            {"id": "a1", "kind": "audio", "name": "Audio 1", "order": 0, "gain_db": -2.0},
        ],
        "clips": [
            {
                "id": "video-out",
                "track_id": "v1",
                "media_id": "video-source",
                "timeline_start_frame": 0,
                "duration_frames": 36,
                "source_in_frame": 12,
                "source_frame_count": 96,
            },
            {
                "id": "video-in",
                "track_id": "v1",
                "media_id": "video-source",
                "timeline_start_frame": 36,
                "duration_frames": 36,
                "source_in_frame": 48,
                "source_frame_count": 96,
                "visual_transform": {
                    "fit": "cover",
                    "position_x": 0.1,
                    "position_y": -0.05,
                    "scale": 1.05,
                    "rotation_deg": 5.0,
                    "opacity": 0.8,
                    "crop_left": 0.1,
                    "crop_top": 0.05,
                },
            },
            {
                "id": "audio-out",
                "track_id": "a1",
                "media_id": "embedded-audio:video-source",
                "timeline_start_frame": 0,
                "duration_frames": 36,
                "source_in_frame": 12,
                "source_frame_count": 96,
                "gain_db": -1.5,
                "fade_in_frames": 3,
            },
            {
                "id": "audio-in",
                "track_id": "a1",
                "media_id": "embedded-audio:video-source",
                "timeline_start_frame": 36,
                "duration_frames": 36,
                "source_in_frame": 48,
                "source_frame_count": 96,
                "fade_out_frames": 3,
            },
        ],
        "transitions": [
            {
                "id": "video-dissolve",
                "kind": "video_cross_dissolve",
                "track_id": "v1",
                "outgoing_clip_id": "video-out",
                "incoming_clip_id": "video-in",
                "duration_frames": 12,
            },
            {
                "id": "audio-power",
                "kind": "audio_constant_power",
                "track_id": "a1",
                "outgoing_clip_id": "audio-out",
                "incoming_clip_id": "audio-in",
                "duration_frames": 12,
            },
        ],
    })


def _resolved_sources(source: Path) -> dict[str, video_sequence_renderer.ResolvedClipSource]:
    return {
        clip_id: video_sequence_renderer.ResolvedClipSource(
            clip_id=clip_id,
            node_id="video-source",
            path=source,
            kind="video",
            has_audio=True,
        )
        for clip_id in ("video-out", "video-in", "audio-out", "audio-in")
    }


def _single_clip_spec(*, gain_db: float = 0.0, muted: bool = False) -> SequenceSpec:
    return SequenceSpec.model_validate({
        "schema_version": "openreel.video_sequence.v1",
        "settings": {
            "frame_rate": {"numerator": 24, "denominator": 1},
            "width": 320,
            "height": 180,
            "audio_sample_rate": 48_000,
            "audio_channels": 2,
        },
        "tracks": [
            {"id": "v1", "kind": "video", "name": "Video 1", "order": 0},
            {"id": "a1", "kind": "audio", "name": "Audio 1", "order": 0, "gain_db": gain_db, "muted": muted},
        ],
        "clips": [
            {
                "id": "video",
                "track_id": "v1",
                "media_id": "video-source",
                "timeline_start_frame": 0,
                "duration_frames": 48,
                "source_in_frame": 0,
                "source_frame_count": 48,
            },
            {
                "id": "audio",
                "track_id": "a1",
                "media_id": "embedded-audio:video-source",
                "timeline_start_frame": 0,
                "duration_frames": 48,
                "source_in_frame": 0,
                "source_frame_count": 48,
            },
        ],
    })


def test_compile_sequence_render_plan_includes_frame_transforms_and_audio_mix(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    plan = video_sequence_renderer.compile_sequence_render_plan(
        _sequence_spec(),
        _resolved_sources(source),
        tmp_path / "render.mp4",
    )

    assert plan.duration_frames == 72
    assert plan.source_node_ids == ["video-source"]
    assert "trim=start_frame=12:end_frame=54" in plan.filter_complex
    assert "trim=start_frame=42:end_frame=84" in plan.filter_complex
    assert "fade=t=in:st=0:d=0.500000000:alpha=1" in plan.filter_complex
    assert "sin(t/0.500000000*PI/2)" in plan.filter_complex
    assert "cos((t-1.250000000)/0.500000000*PI/2)" in plan.filter_complex
    assert "crop=" in plan.filter_complex
    assert "rotate=" in plan.filter_complex
    assert "colorchannelmixer=aa=0.8" in plan.filter_complex
    assert "+0.1*W" in plan.filter_complex
    assert "+-0.05*H" in plan.filter_complex
    assert "adelay=60000S:all=1" in plan.filter_complex
    assert "amix=inputs=3:normalize=0" in plan.filter_complex
    assert plan.ffmpeg_args[-1].endswith("render.mp4")


@pytest.mark.asyncio
async def test_render_sequence_creates_frame_exact_h264_aac_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    project_id = "render-project"
    source = tmp_path / "storage" / project_id / "generated_videos" / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    await media_operations._run_ffmpeg([  # noqa: SLF001
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=24:duration=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=4",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(source),
    ])
    node = WorkflowNode(
        id="video-source",
        project_id=project_id,
        type="video",
        title="Source",
        status="completed",
        output_json=json.dumps({
            "type": "video",
            "local_url": f"/api/media/{project_id}/generated_videos/source.mp4",
        }),
    )

    progress_updates: list[tuple[int, str]] = []
    result = await video_sequence_renderer.render_sequence(
        project_id,
        _sequence_spec(),
        revision=7,
        nodes_by_id={node.id: node},
        title="Rendered sequence",
        progress_callback=lambda progress, phase: progress_updates.append((progress, phase)),
    )
    manifest = await timeline_media_index.ensure_media_index(project_id, result.path)

    assert result.path.exists()
    assert result.path.stat().st_size > 0
    assert result.title == "Rendered sequence"
    assert result.metadata["sequence_revision"] == 7
    assert result.metadata["transition_count"] == 2
    assert manifest.width == 320
    assert manifest.height == 180
    assert manifest.frame_count == 72
    assert manifest.audio.present is True
    assert manifest.audio.sample_rate == 48_000
    assert manifest.audio.channels == 2
    assert progress_updates[0] == (0, "正在准备素材")
    assert progress_updates[-1] == (100, "正在登记成片")
    assert [progress for progress, _ in progress_updates] == sorted(
        progress for progress, _ in progress_updates
    )


@pytest.mark.asyncio
async def test_real_ffmpeg_progress_process_can_be_cancelled(tmp_path: Path) -> None:
    target = tmp_path / "cancelled.mp4"
    encoding_started = asyncio.Event()

    async def on_progress(progress: int, phase: str) -> None:
        if progress >= 1 and phase == "正在编码":
            encoding_started.set()

    task = asyncio.create_task(video_sequence_renderer._run_ffmpeg_with_progress(  # noqa: SLF001
        [
            "-y",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=20",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            str(target),
        ],
        duration_frames=480,
        progress_callback=on_progress,
        timeout=60,
    ))
    await asyncio.wait_for(encoding_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    target.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_exported_silence_and_minus_six_db_have_expected_real_amplitude(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    project_id = "audio-level-project"
    source = tmp_path / "storage" / project_id / "generated_videos" / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    await media_operations._run_ffmpeg([  # noqa: SLF001
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=navy:size=320x180:rate=24:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=997:sample_rate=48000:duration=2",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(source),
    ])
    node = WorkflowNode(
        id="video-source",
        project_id=project_id,
        type="video",
        title="Tone",
        status="completed",
        output_json=json.dumps({
            "type": "video",
            "local_url": f"/api/media/{project_id}/generated_videos/source.mp4",
        }),
    )

    async def render(gain_db: float = 0.0, muted: bool = False):
        result = await video_sequence_renderer.render_sequence(
            project_id,
            _single_clip_spec(gain_db=gain_db, muted=muted),
            revision=1,
            nodes_by_id={node.id: node},
        )
        manifest, _ = await timeline_waveforms.ensure_waveform(project_id, result.path)
        return manifest

    unity = await render()
    plus_six = await render(gain_db=6.0)
    minus_six = await render(gain_db=-6.0)
    silence = await render(muted=True)

    assert unity.peak > 0.05
    assert plus_six.peak / unity.peak == pytest.approx(10 ** (6 / 20), rel=0.04)
    assert minus_six.peak / unity.peak == pytest.approx(10 ** (-6 / 20), rel=0.04)
    assert silence.peak == pytest.approx(0.0, abs=1e-6)
