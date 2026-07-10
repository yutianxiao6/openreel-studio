import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from app.config import settings
from app.services import timeline_media_index


def make_indexed_test_video(path: Path) -> None:
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x36:rate=4:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_media_index_contains_every_real_frame_and_cached_tiles(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    source = tmp_path / "source.mp4"
    make_indexed_test_video(source)

    first = await timeline_media_index.ensure_media_index("project-1", source)
    second = await timeline_media_index.ensure_media_index("project-1", source)

    assert first.cache_key == second.cache_key
    assert first.frame_rate.model_dump() == {"numerator": 4, "denominator": 1}
    assert first.time_base.model_dump() == {"numerator": 1, "denominator": 16_384}
    assert first.frame_count == 8
    assert [frame.index for frame in first.frames] == list(range(8))
    assert [frame.pts for frame in first.frames] == [index * 4096 for index in range(8)]
    assert first.frames[0].key_frame is True
    assert first.width == 64
    assert first.height == 36
    assert first.duration_seconds == pytest.approx(2.0)
    assert first.variable_frame_rate is False
    assert first.audio.present is True
    assert first.audio.sample_rate == 48_000
    assert first.audio.channels == 1

    tile, manifest, start_frame, actual_count = await timeline_media_index.ensure_frame_tile(
        "project-1",
        source,
        tile_index=0,
        columns=4,
        rows=2,
        frame_width=32,
        frame_height=18,
    )
    assert manifest.cache_key == first.cache_key
    assert start_frame == 0
    assert actual_count == 8
    assert tile.exists()
    with Image.open(tile) as image:
        assert image.size == (192, 56)  # dimensions clamp to even production minimums

    page = timeline_media_index.frame_page(first, start=3, limit=2)
    assert page["start"] == 3
    assert [frame["index"] for frame in page["frames"]] == [3, 4]
