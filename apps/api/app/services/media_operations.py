"""Deterministic media operations for canvas video/audio editing."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.db.models import WorkflowNode
from app.services import media_history, project_media_history, subprocess_utils


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
REMOTE_MEDIA_MAX_BYTES = 1024 * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 300


class MediaOperationError(ValueError):
    """User-visible media operation failure."""


@dataclass
class MediaOperationFile:
    kind: str
    rel_path: str
    path: Path
    title: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _parse_json_value(raw: object) -> object:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _suffix_for_kind(kind: str) -> set[str]:
    if kind == "video":
        return VIDEO_EXTENSIONS
    if kind == "audio":
        return AUDIO_EXTENSIONS
    if kind == "image":
        return IMAGE_EXTENSIONS
    return set()


def _looks_like_kind(ref: str, kind: str) -> bool:
    suffix = Path(ref.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    return suffix in _suffix_for_kind(kind)


def _safe_operation_id(operation: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in operation.lower()).strip("-") or "media"


def _operation_output_path(project_id: str, kind: str, operation: str, suffix: str) -> tuple[str, Path]:
    root = project_media_history.project_root(project_id)
    media_dir = project_media_history.MEDIA_HISTORY_DIRS[kind]
    target_dir = root / media_dir / "video_ops"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_operation_id(operation)}-{uuid.uuid4().hex[:12]}{suffix}"
    target = (target_dir / filename).resolve()
    try:
        target.relative_to((root / media_dir).resolve())
    except ValueError as exc:
        raise MediaOperationError("媒体输出路径无效") from exc
    rel_path = f"{media_dir}/video_ops/{filename}"
    return rel_path, target


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError as exc:
        raise MediaOperationError("缺少 ffmpeg 运行时，无法处理视频或音频") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


async def _run_ffmpeg(args: list[str], *, timeout: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    process = await asyncio.create_subprocess_exec(
        _ffmpeg_exe(),
        "-hide_banner",
        "-nostdin",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_utils.hidden_window_kwargs(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaOperationError("媒体处理超时") from exc
    if process.returncode == 0:
        return
    detail = (stderr or stdout or b"").decode("utf-8", errors="ignore").strip()
    if detail:
        detail = detail.splitlines()[-1][:240]
    raise MediaOperationError(detail or "媒体处理失败")


async def _run_ffmpeg_with_fallback(primary: list[str], fallback: list[str]) -> None:
    try:
        await _run_ffmpeg(primary)
    except MediaOperationError:
        await _run_ffmpeg(fallback)


async def _download_remote_media(project_id: str, ref: str, kind: str) -> Path:
    suffix = Path(ref.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    if suffix not in _suffix_for_kind(kind):
        mime_type = mimetypes.guess_type(ref)[0] or ""
        suffix = mimetypes.guess_extension(mime_type) or (".mp4" if kind == "video" else ".m4a")
    rel_path, target = _operation_output_path(project_id, kind, "remote-cache", suffix)
    del rel_path
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=180.0)) as client:
            async with client.stream("GET", ref, follow_redirects=True) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > REMOTE_MEDIA_MAX_BYTES:
                            raise MediaOperationError("远程媒体文件过大")
                        handle.write(chunk)
    except MediaOperationError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise MediaOperationError("无法下载远程媒体") from exc
    return target


async def media_path_for_node(project_id: str, node: WorkflowNode, kind: str) -> Path:
    if node.type != kind:
        raise MediaOperationError(f"请选择{ {'video': '视频', 'audio': '音频', 'image': '图片'}.get(kind, kind) }节点")
    output = _parse_json_value(node.output_json)
    refs = media_history.collect_media_refs(output)
    remote_candidates: list[str] = []
    for ref in refs:
        if not _looks_like_kind(ref, kind):
            continue
        rel_path = project_media_history.rel_path_from_ref(project_id, ref)
        if rel_path:
            try:
                path = project_media_history.media_path_from_rel_path(project_id, rel_path)
            except ValueError:
                continue
            if path.exists() and path.is_file():
                return path
        if ref.startswith(("http://", "https://")):
            remote_candidates.append(ref)
    if remote_candidates:
        return await _download_remote_media(project_id, remote_candidates[0], kind)
    raise MediaOperationError("节点没有可处理的本地媒体产物")


def item_output(project_id: str, result: MediaOperationFile) -> dict[str, Any]:
    item = project_media_history.file_payload(project_id, result.rel_path, result.path)
    if not item:
        raise MediaOperationError("媒体产物登记失败")
    output = project_media_history.output_for_item(item)
    output["title"] = result.title
    output["operation"] = result.metadata
    return output


async def export_video_frame(
    project_id: str,
    node: WorkflowNode,
    *,
    mode: str,
    time_seconds: float | None,
    title: str | None = None,
) -> MediaOperationFile:
    source = await media_path_for_node(project_id, node, "video")
    rel_path, target = _operation_output_path(project_id, "image", "video-frame", ".png")
    if mode == "time":
        seek = max(float(time_seconds or 0), 0.0)
        args = ["-y", "-ss", f"{seek:.3f}", "-i", str(source), "-frames:v", "1", str(target)]
    else:
        args = ["-y", "-sseof", "-0.05", "-i", str(source), "-frames:v", "1", str(target)]
    await _run_ffmpeg_with_fallback(
        args,
        ["-y", "-sseof", "-1", "-i", str(source), "-frames:v", "1", str(target)],
    )
    if not target.exists() or target.stat().st_size <= 0:
        raise MediaOperationError("没有导出到有效画面")
    frame_label = "尾帧" if mode != "time" else f"{max(float(time_seconds or 0), 0.0):.2f}s"
    return MediaOperationFile(
        kind="image",
        rel_path=rel_path,
        path=target,
        title=title or f"{node.title or '视频'} {frame_label}",
        metadata={
            "type": "video.export_frame",
            "source_node_id": node.id,
            "frame_mode": mode,
            "time_seconds": time_seconds,
        },
    )


async def split_video_tracks(project_id: str, node: WorkflowNode) -> list[MediaOperationFile]:
    source = await media_path_for_node(project_id, node, "video")
    video_rel, video_target = _operation_output_path(project_id, "video", "video-track", ".mp4")
    audio_rel, audio_target = _operation_output_path(project_id, "audio", "audio-track", ".m4a")
    await _run_ffmpeg_with_fallback(
        ["-y", "-i", str(source), "-map", "0:v:0", "-an", "-c:v", "copy", str(video_target)],
        [
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(video_target),
        ],
    )
    await _run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(audio_target),
        ]
    )
    return [
        MediaOperationFile(
            kind="video",
            rel_path=video_rel,
            path=video_target,
            title=f"{node.title or '视频'} 画面",
            metadata={"type": "video.split_tracks", "track": "video", "source_node_id": node.id},
        ),
        MediaOperationFile(
            kind="audio",
            rel_path=audio_rel,
            path=audio_target,
            title=f"{node.title or '视频'} 声音",
            metadata={"type": "video.split_tracks", "track": "audio", "source_node_id": node.id},
        ),
    ]


async def trim_video(
    project_id: str,
    node: WorkflowNode,
    *,
    start_seconds: float,
    end_seconds: float,
    title: str | None = None,
) -> MediaOperationFile:
    if end_seconds <= start_seconds:
        raise MediaOperationError("结束时间必须大于开始时间")
    source = await media_path_for_node(project_id, node, "video")
    rel_path, target = _operation_output_path(project_id, "video", "video-trim", ".mp4")
    start = max(float(start_seconds), 0.0)
    end = max(float(end_seconds), start + 0.01)
    await _run_ffmpeg_with_fallback(
        [
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(target),
        ],
        [
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(source),
            "-map",
            "0",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ],
    )
    return MediaOperationFile(
        kind="video",
        rel_path=rel_path,
        path=target,
        title=title or f"{node.title or '视频'} 片段",
        metadata={
            "type": "video.trim",
            "source_node_id": node.id,
            "start_seconds": start,
            "end_seconds": end,
        },
    )


def _write_concat_list(path: Path, sources: list[Path]) -> None:
    lines = []
    for source in sources:
        escaped = str(source).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _normalize_video_sources(sources: list[Path], temp_dir: Path) -> list[Path]:
    normalized: list[Path] = []
    for index, source in enumerate(sources):
        target = temp_dir / f"clip-{index:03d}.mp4"
        await _run_ffmpeg(
            [
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
        normalized.append(target)
    return normalized


async def concat_video(project_id: str, nodes: list[WorkflowNode], *, title: str | None = None) -> MediaOperationFile:
    if len(nodes) < 2:
        raise MediaOperationError("至少选择两个视频片段")
    sources = [await media_path_for_node(project_id, node, "video") for node in nodes]
    rel_path, target = _operation_output_path(project_id, "video", "video-concat", ".mp4")
    with tempfile.TemporaryDirectory(prefix="openreel-video-concat-") as raw_temp:
        temp_dir = Path(raw_temp)
        concat_list = temp_dir / "clips.txt"
        _write_concat_list(concat_list, sources)
        try:
            await _run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(target)])
        except MediaOperationError:
            normalized = await _normalize_video_sources(sources, temp_dir)
            _write_concat_list(concat_list, normalized)
            await _run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(target)])
    return MediaOperationFile(
        kind="video",
        rel_path=rel_path,
        path=target,
        title=title or "拼接视频",
        metadata={"type": "video.concat", "source_node_ids": [node.id for node in nodes]},
    )


async def concat_audio(project_id: str, nodes: list[WorkflowNode], *, title: str | None = None) -> MediaOperationFile:
    if len(nodes) < 2:
        raise MediaOperationError("至少选择两个音频片段")
    sources = [await media_path_for_node(project_id, node, "audio") for node in nodes]
    rel_path, target = _operation_output_path(project_id, "audio", "audio-concat", ".m4a")
    with tempfile.TemporaryDirectory(prefix="openreel-audio-concat-") as raw_temp:
        temp_dir = Path(raw_temp)
        concat_list = temp_dir / "clips.txt"
        _write_concat_list(concat_list, sources)
        await _run_ffmpeg(
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(target),
            ]
        )
    return MediaOperationFile(
        kind="audio",
        rel_path=rel_path,
        path=target,
        title=title or "拼接音频",
        metadata={"type": "audio.concat", "source_node_ids": [node.id for node in nodes]},
    )
