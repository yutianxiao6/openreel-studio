from __future__ import annotations

from typing import Any

import cv2


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _pick_video_ref(inputs: dict[str, Any]) -> Any:
    explicit = _first_present(inputs.get("video"), inputs.get("source_video"))
    if explicit:
        return explicit
    workflow_inputs = inputs.get("workflow_inputs") if isinstance(inputs.get("workflow_inputs"), dict) else {}
    explicit = _first_present(workflow_inputs.get("video"), workflow_inputs.get("source_video"))
    if explicit:
        return explicit
    fields = inputs.get("fields") if isinstance(inputs.get("fields"), dict) else {}
    explicit = _first_present(fields.get("video"), fields.get("source_video"), fields.get("url"), fields.get("local_url"))
    if explicit:
        return explicit
    references = fields.get("references") if isinstance(fields.get("references"), list) else []
    for item in references:
        if isinstance(item, dict):
            ref = _first_present(item.get("ref"), item.get("url"), item.get("local_url"), item.get("path"))
        else:
            ref = item
        if ref:
            return ref
    return None


def _uniform_indices(total_frames: int, count: int) -> list[int]:
    if total_frames <= 0:
        return []
    if count <= 1:
        return [max(0, total_frames // 2)]
    return sorted({round(index * (total_frames - 1) / max(1, count - 1)) for index in range(count)})


def _scene_change_indices(capture: cv2.VideoCapture, total_frames: int, count: int) -> list[int]:
    if total_frames <= 0:
        return []
    stride = max(1, total_frames // max(count * 12, 24))
    previous = None
    scores: list[tuple[float, int]] = []
    frame_index = 0
    while frame_index < total_frames:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok:
            break
        small = cv2.resize(frame, (96, 54), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if previous is not None:
            score = float(cv2.absdiff(gray, previous).mean())
            scores.append((score, frame_index))
        previous = gray
        frame_index += stride
    if not scores:
        return _uniform_indices(total_frames, count)
    best = sorted(scores, key=lambda item: item[0], reverse=True)[:count]
    selected = sorted({index for _, index in best})
    if len(selected) < count:
        selected = sorted({*selected, *_uniform_indices(total_frames, count)})
    return selected[:count]


async def run(ctx, inputs: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    video_ref = _pick_video_ref(inputs)
    if not video_ref:
        return {
            "status": "failed",
            "error": "没有找到视频输入",
            "error_kind": "missing_video_input",
        }

    video_path = await ctx.resolve_asset(video_ref)
    count = _bounded_int(settings.get("count"), 6, 1, 24)
    method = str(settings.get("method") or "scene_change").strip().lower()
    ctx.log(f"开始提取关键帧: method={method}, count={count}")

    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        return {
            "status": "failed",
            "error": "视频无法打开",
            "error_kind": "video_open_failed",
        }

    try:
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if method == "uniform":
            indices = _uniform_indices(total_frames, count)
        else:
            indices = _scene_change_indices(capture, total_frames, count)
        frames = []
        for order, frame_index in enumerate(indices, start=1):
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                continue
            saved = await ctx.save_image(frame, kind=f"keyframe-{order:02d}")
            saved["frame_index"] = frame_index
            frames.append(saved)
            ctx.progress(order / max(1, len(indices)), f"已提取 {order}/{len(indices)}")
        if not frames:
            return {
                "status": "failed",
                "error": "没有成功提取任何关键帧",
                "error_kind": "no_keyframes",
            }
        return {
            "status": "succeeded",
            "outputs": {
                "frames": frames,
                "count": len(frames),
                "method": method,
            },
            "summary": f"已提取 {len(frames)} 张关键帧",
        }
    finally:
        capture.release()
