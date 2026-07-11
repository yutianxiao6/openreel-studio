"""Compile and render persisted frame-native editor sequences with FFmpeg."""
from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.db.models import WorkflowNode
from app.services import media_operations, subprocess_utils, timeline_media_index
from app.services.video_edit_sequences import SequenceClip, SequenceSpec, SequenceTransition


class SequenceRenderError(media_operations.MediaOperationError):
    """A user-visible sequence render failure."""


@dataclass(frozen=True)
class ResolvedClipSource:
    clip_id: str
    node_id: str
    path: Path
    kind: str
    has_audio: bool = True


@dataclass(frozen=True)
class SequenceRenderPlan:
    ffmpeg_args: list[str]
    filter_complex: str
    duration_frames: int
    source_node_ids: list[str]


RenderProgressCallback = Callable[[int, str], Awaitable[None] | None]


async def _emit_progress(
    callback: RenderProgressCallback | None,
    progress: int,
    phase: str,
) -> None:
    if callback is None:
        return
    result = callback(max(0, min(100, int(progress))), phase)
    if inspect.isawaitable(result):
        await result


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _run_ffmpeg_with_progress(
    args: list[str],
    *,
    duration_frames: int,
    progress_callback: RenderProgressCallback | None,
    timeout: int,
) -> None:
    progress_args = [*args[:-1], "-progress", "pipe:1", "-nostats", args[-1]]
    process = await asyncio.create_subprocess_exec(
        media_operations._ffmpeg_exe(),  # noqa: SLF001
        "-hide_banner",
        "-nostdin",
        *progress_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_utils.hidden_window_kwargs(),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    latest_progress = -1
    await _emit_progress(progress_callback, 1, "正在编码")
    try:
        async with asyncio.timeout(timeout):
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break
                key, separator, value = raw_line.decode("utf-8", errors="ignore").strip().partition("=")
                if not separator:
                    continue
                if key == "frame":
                    try:
                        encoded_frames = int(value)
                    except ValueError:
                        continue
                    progress = min(99, max(1, round(encoded_frames / max(1, duration_frames) * 100)))
                    if progress > latest_progress:
                        latest_progress = progress
                        await _emit_progress(progress_callback, progress, "正在编码")
                elif key == "progress" and value == "end":
                    await _emit_progress(progress_callback, 100, "正在登记成片")
            return_code = await process.wait()
            stderr = await stderr_task
    except TimeoutError as exc:
        await _stop_process(process)
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise SequenceRenderError("序列渲染超时") from exc
    except asyncio.CancelledError:
        await _stop_process(process)
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise
    if return_code == 0:
        return
    detail = stderr.decode("utf-8", errors="ignore").strip()
    if detail:
        detail = detail.splitlines()[-1][:240]
    raise SequenceRenderError(detail or "序列渲染失败")


def _seconds(frames: int, fps: float) -> str:
    return f"{frames / fps:.9f}"


def _number(value: float) -> str:
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _transition_sides(transition: SequenceTransition | None) -> tuple[int, int]:
    if transition is None:
        return 0, 0
    before = transition.duration_frames // 2
    return before, transition.duration_frames - before


def _visual_filter(
    *,
    input_index: int,
    clip: SequenceClip,
    source: ResolvedClipSource,
    incoming_transition: SequenceTransition | None,
    outgoing_transition: SequenceTransition | None,
    fps: float,
    fps_expr: str,
    width: int,
    height: int,
    output_label: str,
) -> list[str]:
    incoming_before, _ = _transition_sides(incoming_transition)
    _, outgoing_after = _transition_sides(outgoing_transition)
    segment_frames = incoming_before + clip.duration_frames + outgoing_after
    timeline_start = clip.timeline_start_frame - incoming_before
    source_start = clip.source_in_frame - incoming_before
    if source_start < 0:
        raise SequenceRenderError(f"片段缺少转场前置素材把手: {clip.id}")
    if clip.source_frame_count is not None and source_start + segment_frames > clip.source_frame_count:
        raise SequenceRenderError(f"片段缺少转场后置素材把手: {clip.id}")
    transform = clip.visual_transform
    crop_width = 1.0 - transform.crop_left - transform.crop_right
    crop_height = 1.0 - transform.crop_top - transform.crop_bottom
    target_width = max(2, round(width * transform.scale))
    target_height = max(2, round(height * transform.scale))
    fit_mode = "increase" if transform.fit == "cover" else "decrease"
    raw_label = f"{output_label}raw"
    base_label = f"{output_label}base"
    normalized_label = f"{output_label}normalized"
    chain: list[str] = []
    if source.kind == "image":
        source_trim = f"trim=duration={_seconds(segment_frames, fps)}"
    else:
        source_trim = f"trim=start_frame={source_start}:end_frame={source_start + segment_frames}"
    filters = [
        source_trim,
        "setpts=PTS-STARTPTS",
        f"fps={fps_expr}",
        (
            "crop="
            f"iw*{_number(crop_width)}:ih*{_number(crop_height)}:"
            f"iw*{_number(transform.crop_left)}:ih*{_number(transform.crop_top)}"
        ),
        f"scale={target_width}:{target_height}:force_original_aspect_ratio={fit_mode}:flags=lanczos",
    ]
    if abs(transform.rotation_deg) > 0.0001:
        radians = transform.rotation_deg * math.pi / 180.0
        filters.append(
            f"rotate={_number(radians)}:c=none:ow=rotw(iw):oh=roth(ih)"
        )
    filters.extend(["format=rgba", f"colorchannelmixer=aa={_number(transform.opacity)}"])
    if incoming_transition is not None:
        filters.append(
            f"fade=t=in:st=0:d={_seconds(incoming_transition.duration_frames, fps)}:alpha=1"
        )
    chain.append(f"[{input_index}:v:0]{','.join(filters)}[{raw_label}]")
    chain.append(
        f"color=c=black@0.0:s={width}x{height}:r={fps_expr}:"
        f"d={_seconds(segment_frames, fps)},format=rgba[{base_label}]"
    )
    position_x = f"(W-w)/2+{_number(transform.position_x)}*W"
    position_y = f"(H-h)/2+{_number(transform.position_y)}*H"
    chain.append(
        f"[{base_label}][{raw_label}]overlay=x='{position_x}':y='{position_y}':"
        f"shortest=1:format=auto[{normalized_label}]"
    )
    chain.append(
        f"[{normalized_label}]setpts=PTS+{_seconds(timeline_start, fps)}/TB[{output_label}]"
    )
    return chain


def _audio_filter(
    *,
    input_index: int,
    clip: SequenceClip,
    track_gain_db: float,
    incoming_transition: SequenceTransition | None,
    outgoing_transition: SequenceTransition | None,
    fps: float,
    sample_rate: int,
    channel_layout: str,
    output_label: str,
) -> str:
    incoming_before, _ = _transition_sides(incoming_transition)
    outgoing_before, outgoing_after = _transition_sides(outgoing_transition)
    segment_frames = incoming_before + clip.duration_frames + outgoing_after
    source_start_frame = clip.source_in_frame - incoming_before
    timeline_start_frame = clip.timeline_start_frame - incoming_before
    if source_start_frame < 0:
        raise SequenceRenderError(f"音频片段缺少转场前置素材把手: {clip.id}")
    if clip.source_frame_count is not None and source_start_frame + segment_frames > clip.source_frame_count:
        raise SequenceRenderError(f"音频片段缺少转场后置素材把手: {clip.id}")
    filters = [
        (
            f"atrim=start={_seconds(source_start_frame, fps)}:"
            f"duration={_seconds(segment_frames, fps)}"
        ),
        "asetpts=PTS-STARTPTS",
        f"aformat=sample_rates={sample_rate}:channel_layouts={channel_layout}",
        f"volume={_number(math.pow(10.0, (clip.gain_db + track_gain_db) / 20.0))}",
    ]
    if clip.fade_in_frames:
        filters.append(
            f"afade=t=in:st={_seconds(incoming_before, fps)}:"
            f"d={_seconds(clip.fade_in_frames, fps)}"
        )
    if clip.fade_out_frames:
        fade_out_start = incoming_before + clip.duration_frames - clip.fade_out_frames
        filters.append(
            f"afade=t=out:st={_seconds(fade_out_start, fps)}:"
            f"d={_seconds(clip.fade_out_frames, fps)}"
        )
    if incoming_transition is not None:
        transition_seconds = _seconds(incoming_transition.duration_frames, fps)
        filters.append(
            "volume='if(lt(t," + transition_seconds + "),"
            "sin(t/" + transition_seconds + "*PI/2),1)'"
        )
    if outgoing_transition is not None:
        transition_start = incoming_before + clip.duration_frames - outgoing_before
        transition_end = transition_start + outgoing_transition.duration_frames
        start_seconds = _seconds(transition_start, fps)
        duration_seconds = _seconds(outgoing_transition.duration_frames, fps)
        end_seconds = _seconds(transition_end, fps)
        filters.append(
            "volume='if(lt(t," + start_seconds + "),1,"
            "if(lt(t," + end_seconds + "),"
            "cos((t-" + start_seconds + ")/" + duration_seconds + "*PI/2),0))'"
        )
    delay_samples = max(0, round(timeline_start_frame / fps * sample_rate))
    filters.append(f"adelay={delay_samples}S:all=1")
    return f"[{input_index}:a:0]{','.join(filters)}[{output_label}]"


def compile_sequence_render_plan(
    spec: SequenceSpec,
    sources: dict[str, ResolvedClipSource],
    target: Path,
) -> SequenceRenderPlan:
    fps = spec.settings.frame_rate.numerator / spec.settings.frame_rate.denominator
    fps_expr = f"{spec.settings.frame_rate.numerator}/{spec.settings.frame_rate.denominator}"
    width = spec.settings.width
    height = spec.settings.height
    sample_rate = spec.settings.audio_sample_rate
    if spec.settings.audio_channels not in {1, 2}:
        raise SequenceRenderError("当前序列导出仅支持单声道或双声道")
    channel_layout = "mono" if spec.settings.audio_channels == 1 else "stereo"
    duration_frames = max(
        1,
        *(clip.timeline_start_frame + clip.duration_frames for clip in spec.clips),
    )
    duration_seconds = _seconds(duration_frames, fps)
    track_by_id = {track.id: track for track in spec.tracks}
    incoming_transitions = {transition.incoming_clip_id: transition for transition in spec.transitions}
    outgoing_transitions = {transition.outgoing_clip_id: transition for transition in spec.transitions}
    input_args: list[str] = []
    filter_parts: list[str] = []
    input_index_by_clip: dict[str, int] = {}
    source_node_ids: list[str] = []

    renderable_clips: list[SequenceClip] = []
    for clip in spec.clips:
        track = track_by_id[clip.track_id]
        source = sources.get(clip.id)
        if source is None:
            raise SequenceRenderError(f"片段缺少可渲染媒体源: {clip.id}")
        if track.kind == "video" and (not track.visible or source.kind not in {"video", "image"}):
            continue
        if track.kind == "audio" and (track.muted or clip.muted or not source.has_audio):
            continue
        input_index_by_clip[clip.id] = len(input_index_by_clip)
        if source.kind == "image":
            input_args.extend(["-loop", "1", "-framerate", fps_expr, "-i", str(source.path)])
        else:
            input_args.extend(["-i", str(source.path)])
        renderable_clips.append(clip)
        if source.node_id not in source_node_ids:
            source_node_ids.append(source.node_id)

    video_tracks = sorted(
        (track for track in spec.tracks if track.kind == "video" and track.visible),
        key=lambda track: track.order,
    )
    ordered_video_clips: list[SequenceClip] = []
    for track in video_tracks:
        ordered_video_clips.extend(sorted(
            (
                clip for clip in renderable_clips
                if clip.track_id == track.id and sources[clip.id].kind in {"video", "image"}
            ),
            key=lambda clip: (clip.timeline_start_frame, clip.id),
        ))

    filter_parts.append(
        f"color=c=black:s={width}x{height}:r={fps_expr}:d={duration_seconds},format=rgba[vbase0]"
    )
    previous_video_label = "vbase0"
    for index, clip in enumerate(ordered_video_clips):
        clip_label = f"vclip{index}"
        incoming = incoming_transitions.get(clip.id)
        outgoing = outgoing_transitions.get(clip.id)
        filter_parts.extend(_visual_filter(
            input_index=input_index_by_clip[clip.id],
            clip=clip,
            source=sources[clip.id],
            incoming_transition=incoming if incoming and incoming.kind == "video_cross_dissolve" else None,
            outgoing_transition=outgoing if outgoing and outgoing.kind == "video_cross_dissolve" else None,
            fps=fps,
            fps_expr=fps_expr,
            width=width,
            height=height,
            output_label=clip_label,
        ))
        next_label = f"vbase{index + 1}"
        filter_parts.append(
            f"[{previous_video_label}][{clip_label}]overlay=eof_action=pass:repeatlast=0:"
            f"shortest=0:format=auto[{next_label}]"
        )
        previous_video_label = next_label
    filter_parts.append(
        f"[{previous_video_label}]fps={fps_expr},tpad=stop_mode=clone:stop_duration=1,"
        f"trim=start_frame=0:end_frame={duration_frames},setpts=PTS-STARTPTS,format=yuv420p[vout]"
    )

    audio_tracks = [track for track in spec.tracks if track.kind == "audio"]
    has_solo = any(track.solo and not track.muted for track in audio_tracks)
    audio_labels: list[str] = []
    for clip in renderable_clips:
        track = track_by_id[clip.track_id]
        source = sources[clip.id]
        if track.kind != "audio" or not source.has_audio:
            continue
        if has_solo and not track.solo:
            continue
        label = f"aclip{len(audio_labels)}"
        incoming = incoming_transitions.get(clip.id)
        outgoing = outgoing_transitions.get(clip.id)
        filter_parts.append(_audio_filter(
            input_index=input_index_by_clip[clip.id],
            clip=clip,
            track_gain_db=track.gain_db,
            incoming_transition=incoming if incoming and incoming.kind == "audio_constant_power" else None,
            outgoing_transition=outgoing if outgoing and outgoing.kind == "audio_constant_power" else None,
            fps=fps,
            sample_rate=sample_rate,
            channel_layout=channel_layout,
            output_label=label,
        ))
        audio_labels.append(label)
    filter_parts.append(
        f"anullsrc=r={sample_rate}:cl={channel_layout}:d={duration_seconds}[asilence]"
    )
    mix_inputs = "[asilence]" + "".join(f"[{label}]" for label in audio_labels)
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(audio_labels) + 1}:normalize=0:dropout_transition=0,"
        f"atrim=duration={duration_seconds},aresample={sample_rate}[aout]"
    )
    filter_complex = ";".join(filter_parts)
    ffmpeg_args = [
        "-y",
        *input_args,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        duration_seconds,
        "-r",
        fps_expr,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        str(sample_rate),
        "-ac",
        str(spec.settings.audio_channels),
        "-movflags",
        "+faststart",
        str(target),
    ]
    return SequenceRenderPlan(
        ffmpeg_args=ffmpeg_args,
        filter_complex=filter_complex,
        duration_frames=duration_frames,
        source_node_ids=source_node_ids,
    )


async def resolve_sequence_sources(
    project_id: str,
    spec: SequenceSpec,
    nodes_by_id: dict[str, WorkflowNode],
) -> dict[str, ResolvedClipSource]:
    resolved: dict[str, ResolvedClipSource] = {}
    for clip in spec.clips:
        embedded_video_id = clip.media_id.removeprefix("embedded-audio:")
        is_embedded_audio = embedded_video_id != clip.media_id
        node_id = embedded_video_id if is_embedded_audio else clip.media_id
        node = nodes_by_id.get(node_id)
        if node is None:
            raise SequenceRenderError(f"找不到片段媒体节点: {node_id}")
        kind = "video" if is_embedded_audio else node.type
        if kind not in {"video", "audio", "image"}:
            raise SequenceRenderError(f"不支持的片段媒体类型: {kind}")
        path = await media_operations.media_path_for_node(project_id, node, kind)
        has_audio = True
        if is_embedded_audio:
            try:
                manifest = await timeline_media_index.ensure_media_index(project_id, path)
            except timeline_media_index.TimelineMediaIndexError as exc:
                raise SequenceRenderError(str(exc)) from exc
            has_audio = manifest.audio.present
        resolved[clip.id] = ResolvedClipSource(
            clip_id=clip.id,
            node_id=node_id,
            path=path,
            kind=kind,
            has_audio=has_audio,
        )
    return resolved


async def render_sequence(
    project_id: str,
    spec: SequenceSpec,
    *,
    revision: int,
    nodes_by_id: dict[str, WorkflowNode],
    title: str | None = None,
    progress_callback: RenderProgressCallback | None = None,
) -> media_operations.MediaOperationFile:
    await _emit_progress(progress_callback, 0, "正在准备素材")
    sources = await resolve_sequence_sources(project_id, spec, nodes_by_id)
    rel_path, target = media_operations._operation_output_path(  # noqa: SLF001
        project_id,
        "video",
        "sequence-render",
        ".mp4",
    )
    plan = compile_sequence_render_plan(spec, sources, target)
    try:
        await _run_ffmpeg_with_progress(
            plan.ffmpeg_args,
            duration_frames=plan.duration_frames,
            progress_callback=progress_callback,
            timeout=max(media_operations.FFMPEG_TIMEOUT_SECONDS, 1_200),
        )
    except (media_operations.MediaOperationError, asyncio.CancelledError):
        target.unlink(missing_ok=True)
        raise
    if not target.exists() or target.stat().st_size <= 0:
        raise SequenceRenderError("序列没有渲染出有效视频")
    return media_operations.MediaOperationFile(
        kind="video",
        rel_path=rel_path,
        path=target,
        title=title or "时间线成片",
        metadata={
            "type": "video.render_sequence",
            "sequence_revision": revision,
            "duration_frames": plan.duration_frames,
            "frame_rate": spec.settings.frame_rate.model_dump(mode="json"),
            "width": spec.settings.width,
            "height": spec.settings.height,
            "audio_sample_rate": spec.settings.audio_sample_rate,
            "audio_channels": spec.settings.audio_channels,
            "source_node_ids": plan.source_node_ids,
            "transition_count": len(spec.transitions),
        },
    )
