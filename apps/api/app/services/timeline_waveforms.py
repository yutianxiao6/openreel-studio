"""Cached real PCM peak/RMS pyramids for video-editor audio tracks."""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from pydantic import BaseModel, Field

from app.services import subprocess_utils, timeline_media_index


WAVEFORM_TIMEOUT_SECONDS = 600
BASE_BUCKET_SAMPLES = 256
_locks: dict[str, asyncio.Lock] = {}


class TimelineWaveformError(ValueError):
    pass


class WaveformLevel(BaseModel):
    level: int = Field(ge=0)
    samples_per_bucket: int = Field(gt=0)
    bucket_count: int = Field(gt=0)


class WaveformManifest(BaseModel):
    schema_version: str = "openreel.timeline_waveform.v1"
    cache_key: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    channel_layout: str | None = None
    total_samples: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    peak: float = Field(ge=0)
    levels: list[WaveformLevel]


def _waveform_paths(project_id: str, cache_key: str) -> tuple[Path, Path]:
    cache_dir = timeline_media_index._cache_dir(project_id, cache_key) / "waveform"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "manifest.json", cache_dir / "peaks.npz"


def _aggregate_level(
    minimum: np.ndarray,
    maximum: np.ndarray,
    rms: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if minimum.shape[0] <= 1:
        return minimum, maximum, rms
    if minimum.shape[0] % 2:
        minimum = np.concatenate([minimum, minimum[-1:]], axis=0)
        maximum = np.concatenate([maximum, maximum[-1:]], axis=0)
        rms = np.concatenate([rms, rms[-1:]], axis=0)
    pairs = minimum.shape[0] // 2
    minimum_next = minimum.reshape(pairs, 2, minimum.shape[1]).min(axis=1)
    maximum_next = maximum.reshape(pairs, 2, maximum.shape[1]).max(axis=1)
    rms_next = np.sqrt(np.square(rms).reshape(pairs, 2, rms.shape[1]).mean(axis=1))
    return minimum_next.astype(np.float32), maximum_next.astype(np.float32), rms_next.astype(np.float32)


async def _decode_waveform(
    source: Path,
    *,
    cache_key: str,
    sample_rate: int,
    channels: int,
    channel_layout: str | None,
) -> tuple[WaveformManifest, dict[str, np.ndarray]]:
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_utils.hidden_window_kwargs(),
    )
    minimum_parts: list[np.ndarray] = []
    maximum_parts: list[np.ndarray] = []
    rms_parts: list[np.ndarray] = []
    pending = np.empty((0, channels), dtype=np.float32)
    pending_values = np.empty((0,), dtype=np.float32)
    pending_bytes = b""
    total_samples = 0
    try:
        async with asyncio.timeout(WAVEFORM_TIMEOUT_SECONDS):
            assert process.stdout is not None
            while True:
                raw = await process.stdout.read(1024 * 1024)
                if not raw:
                    break
                raw = pending_bytes + raw
                complete_bytes = len(raw) - (len(raw) % 4)
                pending_bytes = raw[complete_bytes:]
                if complete_bytes <= 0:
                    continue
                values = np.frombuffer(raw[:complete_bytes], dtype="<f4")
                if pending_values.size:
                    values = np.concatenate([pending_values, values])
                    pending_values = np.empty((0,), dtype=np.float32)
                complete_values = values.size - (values.size % channels)
                if complete_values <= 0:
                    pending_values = values.copy()
                    continue
                if complete_values < values.size:
                    pending_values = values[complete_values:].copy()
                samples = values[:complete_values].reshape(-1, channels).astype(np.float32, copy=False)
                total_samples += samples.shape[0]
                if pending.size:
                    samples = np.concatenate([pending, samples], axis=0)
                    pending = np.empty((0, channels), dtype=np.float32)
                full_bucket_samples = (samples.shape[0] // BASE_BUCKET_SAMPLES) * BASE_BUCKET_SAMPLES
                if full_bucket_samples:
                    full = samples[:full_bucket_samples].reshape(-1, BASE_BUCKET_SAMPLES, channels)
                    minimum_parts.append(full.min(axis=1).astype(np.float32))
                    maximum_parts.append(full.max(axis=1).astype(np.float32))
                    rms_parts.append(np.sqrt(np.square(full).mean(axis=1)).astype(np.float32))
                if full_bucket_samples < samples.shape[0]:
                    pending = samples[full_bucket_samples:].copy()
            stderr = await process.stderr.read() if process.stderr else b""
            return_code = await process.wait()
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TimelineWaveformError("真实音频波形生成超时") from exc
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="ignore").strip()
        raise TimelineWaveformError(detail.splitlines()[-1][:240] if detail else "真实音频波形生成失败")
    if pending.shape[0]:
        minimum_parts.append(pending.min(axis=0, keepdims=True).astype(np.float32))
        maximum_parts.append(pending.max(axis=0, keepdims=True).astype(np.float32))
        rms_parts.append(np.sqrt(np.square(pending).mean(axis=0, keepdims=True)).astype(np.float32))
    if total_samples <= 0 or not minimum_parts:
        raise TimelineWaveformError("音轨没有可用的 PCM 样本")

    minimum = np.concatenate(minimum_parts, axis=0)
    maximum = np.concatenate(maximum_parts, axis=0)
    rms = np.concatenate(rms_parts, axis=0)
    arrays: dict[str, np.ndarray] = {}
    levels: list[WaveformLevel] = []
    level = 0
    samples_per_bucket = BASE_BUCKET_SAMPLES
    while True:
        arrays[f"level_{level}_min"] = minimum
        arrays[f"level_{level}_max"] = maximum
        arrays[f"level_{level}_rms"] = rms
        levels.append(WaveformLevel(
            level=level,
            samples_per_bucket=samples_per_bucket,
            bucket_count=minimum.shape[0],
        ))
        if minimum.shape[0] <= 1:
            break
        minimum, maximum, rms = _aggregate_level(minimum, maximum, rms)
        level += 1
        samples_per_bucket *= 2

    peak = max(
        float(np.abs(arrays["level_0_min"]).max()),
        float(np.abs(arrays["level_0_max"]).max()),
    )
    manifest = WaveformManifest(
        cache_key=cache_key,
        sample_rate=sample_rate,
        channels=channels,
        channel_layout=channel_layout,
        total_samples=total_samples,
        duration_seconds=total_samples / sample_rate,
        peak=peak,
        levels=levels,
    )
    return manifest, arrays


async def ensure_waveform(
    project_id: str,
    source: Path,
) -> tuple[WaveformManifest, Path]:
    media_index = await timeline_media_index.ensure_media_index(project_id, source)
    if not media_index.audio.present or not media_index.audio.sample_rate:
        raise TimelineWaveformError("视频没有可分析的音轨")
    channels = media_index.audio.channels or 1
    manifest_path, peaks_path = _waveform_paths(project_id, media_index.cache_key)
    if manifest_path.exists() and peaks_path.exists() and peaks_path.stat().st_size > 0:
        try:
            manifest = WaveformManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            return manifest, peaks_path
        except Exception:
            manifest_path.unlink(missing_ok=True)
            peaks_path.unlink(missing_ok=True)

    lock = _locks.setdefault(f"waveform:{media_index.cache_key}", asyncio.Lock())
    async with lock:
        if manifest_path.exists() and peaks_path.exists() and peaks_path.stat().st_size > 0:
            return WaveformManifest.model_validate_json(manifest_path.read_text(encoding="utf-8")), peaks_path
        manifest, arrays = await _decode_waveform(
            source.resolve(),
            cache_key=media_index.cache_key,
            sample_rate=media_index.audio.sample_rate,
            channels=channels,
            channel_layout=media_index.audio.channel_layout,
        )
        token = uuid.uuid4().hex[:8]
        temporary_manifest = manifest_path.with_name(f"manifest.tmp-{token}.json")
        temporary_peaks = peaks_path.with_name(f"peaks.tmp-{token}.npz")
        try:
            np.savez_compressed(temporary_peaks, **arrays)
            temporary_manifest.write_text(manifest.model_dump_json(), encoding="utf-8")
            temporary_peaks.replace(peaks_path)
            temporary_manifest.replace(manifest_path)
        finally:
            temporary_manifest.unlink(missing_ok=True)
            temporary_peaks.unlink(missing_ok=True)
        return manifest, peaks_path


def waveform_page(
    manifest: WaveformManifest,
    peaks_path: Path,
    *,
    level: int,
    start_bucket: int,
    limit: int,
) -> dict:
    level_info = next((item for item in manifest.levels if item.level == level), None)
    if level_info is None:
        raise TimelineWaveformError("波形精度层级不存在")
    safe_start = max(0, min(int(start_bucket), level_info.bucket_count))
    safe_limit = max(1, min(int(limit), 10_000))
    end = min(level_info.bucket_count, safe_start + safe_limit)
    with np.load(peaks_path, allow_pickle=False) as arrays:
        minimum = arrays[f"level_{level}_min"][safe_start:end]
        maximum = arrays[f"level_{level}_max"][safe_start:end]
        rms = arrays[f"level_{level}_rms"][safe_start:end]
    return {
        "cache_key": manifest.cache_key,
        "level": level,
        "samples_per_bucket": level_info.samples_per_bucket,
        "sample_rate": manifest.sample_rate,
        "channels": manifest.channels,
        "bucket_count": level_info.bucket_count,
        "start_bucket": safe_start,
        "minimum": minimum.tolist(),
        "maximum": maximum.tolist(),
        "rms": rms.tolist(),
    }


def gain_amplitude(gain_db: float) -> float:
    if gain_db <= -120:
        return 0.0
    return math.pow(10.0, gain_db / 20.0)
