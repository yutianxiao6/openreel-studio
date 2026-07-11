"""Full source-frame indexes and consecutive-frame thumbnail tiles."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections import deque
from pathlib import Path

import imageio_ffmpeg
from pydantic import BaseModel, Field

from app.config import settings
from app.services import subprocess_utils


INDEX_TIMEOUT_SECONDS = 600
TILE_TIMEOUT_SECONDS = 180
_locks: dict[str, asyncio.Lock] = {}

_CONFIG_RE = re.compile(
    r"config in time_base:\s*(?P<tb_num>-?\d+)/(?P<tb_den>\d+),\s*"
    r"frame_rate:\s*(?P<fps_num>-?\d+)/(?P<fps_den>\d+)"
)
_FRAME_RE = re.compile(
    r"\bn:\s*(?P<index>\d+)\s+pts:\s*(?P<pts>-?\d+)\s+"
    r"pts_time:(?P<pts_time>[-+\deE.]+)\s+duration:\s*(?P<duration>-?\d+)\s+"
    r"duration_time:(?P<duration_time>[-+\deE.]+).*?"
    r"\bs:(?P<width>\d+)x(?P<height>\d+).*?"
    r"\biskey:(?P<key>[01])\s+type:(?P<picture_type>\S+)"
)
_AUDIO_RE = re.compile(r"Audio:.*?,\s*(?P<sample_rate>\d+)\s*Hz,\s*(?P<layout>[^,]+)")


class TimelineMediaIndexError(ValueError):
    pass


class RationalValue(BaseModel):
    numerator: int
    denominator: int = Field(gt=0)


class IndexedFrame(BaseModel):
    index: int = Field(ge=0)
    pts: int
    pts_time: float
    duration: int
    duration_time: float = Field(ge=0)
    key_frame: bool
    picture_type: str


class AudioStreamInfo(BaseModel):
    present: bool = False
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None


class MediaIndexManifest(BaseModel):
    schema_version: str = "openreel.timeline_media_index.v1"
    cache_key: str
    frame_rate: RationalValue
    time_base: RationalValue
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    variable_frame_rate: bool = False
    audio: AudioStreamInfo = Field(default_factory=AudioStreamInfo)
    frames: list[IndexedFrame]

    def summary(self) -> dict:
        return self.model_dump(mode="json", exclude={"frames"})


def _source_key(source: Path) -> str:
    stat = source.stat()
    payload = ":".join([
        str(source.resolve()),
        str(stat.st_size),
        str(stat.st_mtime_ns),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _cache_dir(project_id: str, cache_key: str) -> Path:
    if not project_id or project_id in {".", ".."} or Path(project_id).name != project_id:
        raise TimelineMediaIndexError("Invalid project id")
    root = Path(settings.PROJECT_ROOT).expanduser().resolve() / "data" / "video_editor_cache"
    target = root / project_id / cache_key
    target.mkdir(parents=True, exist_ok=True)
    return target


def _channel_count(layout: str) -> int | None:
    normalized = layout.strip().lower()
    known = {
        "mono": 1,
        "stereo": 2,
        "2.1": 3,
        "3.0": 3,
        "4.0": 4,
        "5.0": 5,
        "5.1": 6,
        "7.1": 8,
    }
    if normalized in known:
        return known[normalized]
    match = re.search(r"(\d+)\s*channels?", normalized)
    return int(match.group(1)) if match else None


def _is_variable_frame_rate(frames: list[IndexedFrame]) -> bool:
    durations = [frame.duration_time for frame in frames if frame.duration_time > 0]
    if len(durations) < 2:
        return False
    baseline = sorted(durations)[len(durations) // 2]
    tolerance = max(0.000_05, baseline * 0.01)
    return any(abs(value - baseline) > tolerance for value in durations)


async def _probe_source(source: Path, cache_key: str) -> MediaIndexManifest:
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        "showinfo",
        "-an",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_utils.hidden_window_kwargs(),
    )
    frames: list[IndexedFrame] = []
    frame_rate: RationalValue | None = None
    time_base: RationalValue | None = None
    width = 0
    height = 0
    audio = AudioStreamInfo()
    error_tail: deque[str] = deque(maxlen=12)
    try:
        async with asyncio.timeout(INDEX_TIMEOUT_SECONDS):
            assert process.stderr is not None
            while True:
                raw_line = await process.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if line:
                    error_tail.append(line)
                config_match = _CONFIG_RE.search(line)
                if config_match:
                    time_base = RationalValue(
                        numerator=int(config_match.group("tb_num")),
                        denominator=int(config_match.group("tb_den")),
                    )
                    frame_rate = RationalValue(
                        numerator=int(config_match.group("fps_num")),
                        denominator=int(config_match.group("fps_den")),
                    )
                if not audio.present:
                    audio_match = _AUDIO_RE.search(line)
                    if audio_match:
                        layout = audio_match.group("layout").strip()
                        audio = AudioStreamInfo(
                            present=True,
                            sample_rate=int(audio_match.group("sample_rate")),
                            channels=_channel_count(layout),
                            channel_layout=layout,
                        )
                frame_match = _FRAME_RE.search(line)
                if frame_match:
                    width = int(frame_match.group("width"))
                    height = int(frame_match.group("height"))
                    frames.append(IndexedFrame(
                        index=int(frame_match.group("index")),
                        pts=int(frame_match.group("pts")),
                        pts_time=float(frame_match.group("pts_time")),
                        duration=int(frame_match.group("duration")),
                        duration_time=max(0.0, float(frame_match.group("duration_time"))),
                        key_frame=frame_match.group("key") == "1",
                        picture_type=frame_match.group("picture_type"),
                    ))
            return_code = await process.wait()
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TimelineMediaIndexError("视频逐帧索引生成超时") from exc
    if return_code != 0:
        raise TimelineMediaIndexError(error_tail[-1] if error_tail else "视频逐帧索引生成失败")
    if not frames or not frame_rate or not time_base or width <= 0 or height <= 0:
        raise TimelineMediaIndexError("没有读取到有效的视频帧索引")
    expected_indexes = list(range(len(frames)))
    actual_indexes = [frame.index for frame in frames]
    if actual_indexes != expected_indexes:
        raise TimelineMediaIndexError("视频帧索引不连续")
    last = frames[-1]
    duration_seconds = last.pts_time + max(last.duration_time, frame_rate.denominator / frame_rate.numerator)
    return MediaIndexManifest(
        cache_key=cache_key,
        frame_rate=frame_rate,
        time_base=time_base,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        frame_count=len(frames),
        variable_frame_rate=_is_variable_frame_rate(frames),
        audio=audio,
        frames=frames,
    )


async def ensure_media_index(project_id: str, source: Path) -> MediaIndexManifest:
    source = source.resolve()
    if not source.exists() or not source.is_file():
        raise TimelineMediaIndexError("视频源文件不存在")
    cache_key = _source_key(source)
    target_dir = _cache_dir(project_id, cache_key)
    manifest_path = target_dir / "index.json"
    if manifest_path.exists() and manifest_path.stat().st_size > 0:
        try:
            return MediaIndexManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest_path.unlink(missing_ok=True)

    lock = _locks.setdefault(f"index:{cache_key}", asyncio.Lock())
    async with lock:
        if manifest_path.exists() and manifest_path.stat().st_size > 0:
            return MediaIndexManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        manifest = await _probe_source(source, cache_key)
        temporary = manifest_path.with_name(f"index.tmp-{uuid.uuid4().hex[:8]}.json")
        try:
            temporary.write_text(manifest.model_dump_json(), encoding="utf-8")
            temporary.replace(manifest_path)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest


async def ensure_frame_tile(
    project_id: str,
    source: Path,
    *,
    tile_index: int,
    columns: int = 8,
    rows: int = 4,
    frame_width: int = 96,
    frame_height: int = 54,
) -> tuple[Path, MediaIndexManifest, int, int]:
    manifest = await ensure_media_index(project_id, source)
    columns = max(1, min(int(columns), 16))
    rows = max(1, min(int(rows), 8))
    frame_width = max(48, min(int(frame_width), 192))
    frame_height = max(28, min(int(frame_height), 108))
    frame_width += frame_width % 2
    frame_height += frame_height % 2
    frames_per_tile = columns * rows
    tile_count = (manifest.frame_count + frames_per_tile - 1) // frames_per_tile
    if tile_index < 0 or tile_index >= tile_count:
        raise TimelineMediaIndexError("时间线帧分块超出范围")
    start_frame = tile_index * frames_per_tile
    actual_count = min(frames_per_tile, manifest.frame_count - start_frame)
    target_dir = _cache_dir(project_id, manifest.cache_key) / "frame_tiles"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (
        f"tile-{tile_index:06d}-{columns}x{rows}-{frame_width}x{frame_height}.jpg"
    )
    if target.exists() and target.stat().st_size > 0:
        return target, manifest, start_frame, actual_count

    lock_key = f"tile:{manifest.cache_key}:{tile_index}:{columns}:{rows}:{frame_width}:{frame_height}"
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        if target.exists() and target.stat().st_size > 0:
            return target, manifest, start_frame, actual_count
        end_frame = start_frame + actual_count - 1
        temporary = target.with_name(f"{target.stem}.tmp-{uuid.uuid4().hex[:8]}.jpg")
        select_filter = f"select='between(n,{start_frame},{end_frame})'"
        video_filter = (
            f"{select_filter},"
            f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
            f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"tile={columns}x{rows}"
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
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            video_filter,
            "-frames:v",
            "1",
            "-q:v",
            "4",
            str(temporary),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_utils.hidden_window_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TILE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise TimelineMediaIndexError("逐帧缩略图生成超时") from exc
        if process.returncode != 0:
            detail = (stderr or stdout or b"").decode("utf-8", errors="ignore").strip()
            raise TimelineMediaIndexError(detail.splitlines()[-1][:240] if detail else "逐帧缩略图生成失败")
        try:
            if not temporary.exists() or temporary.stat().st_size <= 0:
                raise TimelineMediaIndexError("没有生成有效的逐帧缩略图")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target, manifest, start_frame, actual_count


def frame_page(manifest: MediaIndexManifest, *, start: int, limit: int) -> dict:
    safe_start = max(0, min(int(start), manifest.frame_count))
    safe_limit = max(1, min(int(limit), 2_000))
    frames = manifest.frames[safe_start:safe_start + safe_limit]
    return {
        "cache_key": manifest.cache_key,
        "frame_count": manifest.frame_count,
        "start": safe_start,
        "frames": [frame.model_dump(mode="json") for frame in frames],
    }
