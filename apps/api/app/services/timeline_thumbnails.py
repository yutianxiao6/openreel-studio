"""Cached FFmpeg sprites for the interactive video timeline."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import imageio_ffmpeg

from app.config import settings


SPRITE_TIMEOUT_SECONDS = 120
_locks: dict[str, asyncio.Lock] = {}


class TimelineThumbnailError(ValueError):
    """User-visible timeline thumbnail failure."""


def _cache_key(
    source: Path,
    *,
    frame_count: int,
    duration_seconds: float,
    frame_width: int,
    frame_height: int,
) -> str:
    stat = source.stat()
    payload = ":".join([
        str(source.resolve()),
        str(stat.st_size),
        str(stat.st_mtime_ns),
        str(frame_count),
        f"{duration_seconds:.6f}",
        str(frame_width),
        str(frame_height),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _cache_target(project_id: str, cache_key: str) -> Path:
    if not project_id or project_id in {".", ".."} or Path(project_id).name != project_id:
        raise TimelineThumbnailError("Invalid project id")
    target_dir = Path(settings.PROJECT_ROOT).expanduser().resolve() / "data" / "timeline_thumbnails" / project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"sprite-{cache_key}.jpg"


async def _render_sprite(
    source: Path,
    target: Path,
    *,
    frame_count: int,
    duration_seconds: float,
    frame_width: int,
    frame_height: int,
) -> None:
    sample_rate = max(frame_count / max(duration_seconds, 0.1), 0.001)
    video_filter = (
        f"fps={sample_rate:.8f},"
        f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
        f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"tile={frame_count}x1"
    )
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-an",
        "-vf",
        video_filter,
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=SPRITE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise TimelineThumbnailError("时间线帧预览生成超时") from exc
    if process.returncode != 0:
        detail = (stderr or stdout or b"").decode("utf-8", errors="ignore").strip()
        raise TimelineThumbnailError(detail.splitlines()[-1][:240] if detail else "时间线帧预览生成失败")


async def ensure_timeline_sprite(
    project_id: str,
    source: Path,
    *,
    frame_count: int,
    duration_seconds: float,
    frame_width: int = 128,
    frame_height: int = 72,
) -> Path:
    source = source.resolve()
    if not source.exists() or not source.is_file():
        raise TimelineThumbnailError("视频源文件不存在")
    frame_count = max(6, min(int(frame_count), 48))
    duration_seconds = max(0.1, min(float(duration_seconds), 7200.0))
    frame_width = max(80, min(int(frame_width), 192))
    frame_height = max(45, min(int(frame_height), 108))
    cache_key = _cache_key(
        source,
        frame_count=frame_count,
        duration_seconds=duration_seconds,
        frame_width=frame_width,
        frame_height=frame_height,
    )
    target = _cache_target(project_id, cache_key)
    if target.exists() and target.stat().st_size > 0:
        return target

    lock = _locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        if target.exists() and target.stat().st_size > 0:
            return target
        temporary = target.with_name(f"{target.stem}.tmp-{uuid.uuid4().hex[:8]}.jpg")
        try:
            await _render_sprite(
                source,
                temporary,
                frame_count=frame_count,
                duration_seconds=duration_seconds,
                frame_width=frame_width,
                frame_height=frame_height,
            )
            if not temporary.exists() or temporary.stat().st_size <= 0:
                raise TimelineThumbnailError("没有生成有效的时间线帧预览")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target
