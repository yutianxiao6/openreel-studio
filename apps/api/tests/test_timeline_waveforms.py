import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import pytest

from app.config import settings
from app.services import timeline_waveforms


def make_waveform_test_video(path: Path) -> None:
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
            r"aevalsrc=if(lt(t\,1)\,0.5*sin(2*PI*440*t)\,0):s=48000:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_f32le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_real_pcm_waveform_tracks_signal_silence_and_gain(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    source = tmp_path / "source.mkv"
    make_waveform_test_video(source)

    manifest, peaks_path = await timeline_waveforms.ensure_waveform("project-1", source)
    cached, cached_path = await timeline_waveforms.ensure_waveform("project-1", source)

    assert cached.cache_key == manifest.cache_key
    assert cached_path == peaks_path
    assert manifest.sample_rate == 48_000
    assert manifest.channels == 1
    assert manifest.total_samples == 96_000
    assert manifest.duration_seconds == pytest.approx(2.0)
    assert manifest.peak == pytest.approx(0.5, abs=0.01)
    assert manifest.levels[0].samples_per_bucket == 256
    assert manifest.levels[-1].bucket_count == 1

    page = timeline_waveforms.waveform_page(
        manifest,
        peaks_path,
        level=0,
        start_bucket=0,
        limit=manifest.levels[0].bucket_count,
    )
    maximum = np.asarray(page["maximum"], dtype=np.float32)[:, 0]
    minimum = np.asarray(page["minimum"], dtype=np.float32)[:, 0]
    rms = np.asarray(page["rms"], dtype=np.float32)[:, 0]
    assert maximum[:180].max() > 0.49
    assert minimum[:180].min() < -0.49
    assert rms[:180].mean() == pytest.approx(0.3535, abs=0.02)
    assert np.abs(maximum[200:]).max() < 0.0001
    assert np.abs(minimum[200:]).max() < 0.0001

    assert timeline_waveforms.gain_amplitude(-6.0) == pytest.approx(0.501187, rel=1e-5)
    assert timeline_waveforms.gain_amplitude(-120.0) == 0.0
