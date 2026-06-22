"""Helpers for media node output history.

The active node output stays the source of truth. Previous generated media are
kept inside the same output payload under ``history`` so project snapshots and
node details restore without a separate table.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


MEDIA_HISTORY_LIMIT = 12
MEDIA_HISTORY_KEYS = {"history", "media_history"}
MEDIA_URL_KEYS = {
    "url",
    "local_url",
    "remote_url",
    "composite_url",
    "thumbnail_url",
    "poster",
    "last_frame_url",
    "audio_url",
}
MEDIA_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".svg",
    ".mp4",
    ".webm",
    ".mov",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".flac",
)


def _jsonable(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def _looks_like_media_ref(value: str) -> bool:
    text = value.strip().lower().split("?", 1)[0]
    return (
        text.startswith(("/api/media/", "/storage/"))
        or "/generated_images/" in text
        or "/generated_videos/" in text
        or "/generated_audio/" in text
        or text.endswith(MEDIA_EXTENSIONS)
    )


def strip_media_history(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_media_history(item)
            for key, item in value.items()
            if key not in MEDIA_HISTORY_KEYS
        }
    if isinstance(value, list):
        return [strip_media_history(item) for item in value]
    return value


def collect_media_refs(value: Any) -> list[str]:
    refs: list[str] = []

    def walk(item: Any, key: str | None = None) -> None:
        if isinstance(item, dict):
            for child_key, child_value in item.items():
                if child_key in MEDIA_HISTORY_KEYS:
                    continue
                walk(child_value, str(child_key))
            return
        if isinstance(item, list):
            for child in item:
                walk(child, key)
            return
        if not isinstance(item, str):
            return
        text = item.strip()
        if not text:
            return
        if key in MEDIA_URL_KEYS or _looks_like_media_ref(text):
            if text not in refs:
                refs.append(text)

    walk(value)
    return refs


def media_signature(output: Any) -> str:
    cleaned = strip_media_history(output)
    refs = collect_media_refs(cleaned)
    if refs:
        return "|".join(sorted(refs))
    return hashlib.sha1(_jsonable(cleaned).encode("utf-8")).hexdigest()


def has_media_output(output: Any) -> bool:
    return bool(collect_media_refs(strip_media_history(output)))


def prompt_from_state(output: Any, input_data: Any = None, fallback: str | None = None) -> str:
    for container in (output, input_data):
        if not isinstance(container, dict):
            continue
        for key in ("prompt", "video_prompt", "image_prompt", "visual_prompt"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        fields = container.get("fields")
        if isinstance(fields, dict):
            for key in ("prompt", "video_prompt", "image_prompt", "visual_prompt"):
                value = fields.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return (fallback or "").strip()


def is_successful_media_output(output: Any) -> bool:
    cleaned = strip_media_history(output)
    if not has_media_output(cleaned):
        return False
    if not isinstance(cleaned, dict):
        return True
    if cleaned.get("ok") is False:
        return False
    status = str(cleaned.get("status") or "").strip().lower()
    if status in {"failed", "error", "cancelled", "canceled", "queued", "running"}:
        return False
    if cleaned.get("error") or cleaned.get("error_message"):
        return False
    if cleaned.get("type") == "fusion" and isinstance(cleaned.get("stages"), list):
        media_stage_count = 0
        for stage in cleaned.get("stages") or []:
            if not isinstance(stage, dict) or not collect_media_refs(stage):
                continue
            media_stage_count += 1
            stage_status = str(stage.get("status") or "").strip().lower()
            if stage_status and stage_status not in {"completed", "success", "succeeded", "done"}:
                return False
            if stage.get("error") or stage.get("error_message"):
                return False
        return media_stage_count > 0
    return True


def normalize_history_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    output = entry.get("output")
    if output is None:
        output = {
            key: value
            for key, value in entry.items()
            if key not in {"id", "created_at", "type", "prompt", "input", "label"}
        }
    output = strip_media_history(output)
    if not is_successful_media_output(output):
        return None
    signature = media_signature(output)
    history_id = str(entry.get("id") or "").strip()
    if not history_id:
        history_id = f"hist_{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:16]}"
    normalized: dict[str, Any] = {
        "id": history_id,
        "created_at": str(entry.get("created_at") or _utc_now()),
        "type": entry.get("type"),
        "output": output,
        "signature": signature,
    }
    for key in ("prompt", "input", "label"):
        if entry.get(key) is not None:
            normalized[key] = entry.get(key)
    return normalized


def media_history_from_output(output: Any, *, limit: int = MEDIA_HISTORY_LIMIT) -> list[dict[str, Any]]:
    if not isinstance(output, dict):
        return []
    raw = output.get("history")
    if raw is None:
        raw = output.get("media_history")
    if not isinstance(raw, list):
        return []
    history: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        normalized = normalize_history_entry(item)
        if not normalized:
            continue
        signature = str(normalized.get("signature") or "")
        if signature in seen:
            continue
        seen.add(signature)
        history.append(normalized)
        if len(history) >= limit:
            break
    return history


def make_history_entry(
    output: Any,
    *,
    node_type: str | None = None,
    prompt: str | None = None,
    input_data: dict[str, Any] | None = None,
    label: str | None = None,
) -> dict[str, Any] | None:
    cleaned = strip_media_history(output)
    if not is_successful_media_output(cleaned):
        return None
    signature = media_signature(cleaned)
    created_at = _utc_now()
    digest = hashlib.sha1(f"{created_at}:{signature}".encode("utf-8")).hexdigest()[:16]
    selected_prompt = prompt_from_state(cleaned, input_data, prompt)
    selected_input = strip_media_history(input_data) if input_data else None
    if isinstance(cleaned, dict) and isinstance(cleaned.get("input"), dict):
        selected_input = strip_media_history(cleaned["input"])
    entry: dict[str, Any] = {
        "id": f"hist_{digest}",
        "created_at": created_at,
        "type": node_type,
        "output": cleaned,
        "signature": signature,
    }
    if selected_prompt:
        entry["prompt"] = selected_prompt
    if selected_input:
        entry["input"] = selected_input
    if label:
        entry["label"] = label
    return entry


def attach_media_history(
    output: Any,
    history: list[dict[str, Any]],
    *,
    limit: int = MEDIA_HISTORY_LIMIT,
    skip_current: bool = True,
) -> Any:
    if not isinstance(output, dict):
        return output
    cleaned = strip_media_history(output)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in history:
        entry = normalize_history_entry(item)
        if not entry:
            continue
        signature = str(entry.get("signature") or "")
        if signature in seen or (skip_current and signature == media_signature(cleaned)):
            continue
        seen.add(signature)
        normalized.append(entry)
        if len(normalized) >= limit:
            break
    if normalized:
        cleaned["history"] = normalized
    return cleaned


def archive_current_media_output(
    current_output: Any,
    *,
    node_type: str | None = None,
    prompt: str | None = None,
    input_data: dict[str, Any] | None = None,
    limit: int = MEDIA_HISTORY_LIMIT,
) -> Any:
    if not isinstance(current_output, dict):
        return current_output
    history = media_history_from_output(current_output, limit=limit)
    entry = make_history_entry(
        current_output,
        node_type=node_type,
        prompt=prompt,
        input_data=input_data,
    )
    if entry:
        history = [entry, *history]
    return attach_media_history(current_output, history, limit=limit, skip_current=False)


def preserve_media_history(output: Any, source_output: Any, *, limit: int = MEDIA_HISTORY_LIMIT) -> Any:
    history = media_history_from_output(output, limit=limit)
    if not history:
        history = media_history_from_output(source_output, limit=limit)
    return attach_media_history(output, history, limit=limit)


def switch_media_history_version(
    current_output: Any,
    *,
    history_id: str | None = None,
    index: int | None = None,
    node_type: str | None = None,
    prompt: str | None = None,
    input_data: dict[str, Any] | None = None,
    limit: int = MEDIA_HISTORY_LIMIT,
) -> tuple[Any, dict[str, Any]]:
    history = media_history_from_output(current_output, limit=limit)
    selected: dict[str, Any] | None = None
    selected_index = -1
    if history_id:
        for idx, item in enumerate(history):
            if str(item.get("id") or "") == history_id:
                selected = item
                selected_index = idx
                break
    elif index is not None and 0 <= index < len(history):
        selected = history[index]
        selected_index = index
    if selected is None:
        raise ValueError("History entry not found")

    selected_output = strip_media_history(selected.get("output"))
    next_history = [item for idx, item in enumerate(history) if idx != selected_index]
    current_entry = make_history_entry(
        current_output,
        node_type=node_type,
        prompt=prompt,
        input_data=input_data,
        label="previous_current",
    )
    if current_entry:
        next_history = [current_entry, *next_history]
    return attach_media_history(selected_output, next_history, limit=limit), selected
