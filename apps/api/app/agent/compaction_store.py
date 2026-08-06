"""Persist Codex-style model-context checkpoints without hiding chat history."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.context_compact import compaction_checkpoint_message
from app.agent.rollout_context import encode_compaction_checkpoint
from app.agent.vision_context import VISION_METADATA_KEY, vision_metadata_from_message
from app.db.models import Message


def _metadata(message: Message) -> dict[str, Any]:
    raw = getattr(message, "metadata_json", None)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _model_visible(metadata: dict[str, Any]) -> bool:
    if metadata.get("model_visible") is False:
        return False
    if metadata.get("source") == "slash_command" and metadata.get("model_visible") is not True:
        return False
    return True


def _merged_vision_payload(
    payloads: list[dict[str, Any]],
    *,
    max_images: int,
) -> dict[str, Any] | None:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    omitted = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        try:
            omitted += max(0, int(payload.get("omitted_count") or 0))
        except (TypeError, ValueError):
            pass
        for image in payload.get("images") or []:
            if not isinstance(image, dict):
                continue
            source = str(image.get("source") or "").strip()
            key = source or json.dumps(image, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            if len(images) >= max_images:
                omitted += 1
                continue
            images.append(dict(image))
    if not images:
        return None
    return {
        "version": 1,
        "kind": "vision_context",
        "source": "compaction_checkpoint",
        "images": images,
        "image_count": len(images),
        "omitted_count": omitted,
    }


async def persist_compaction_checkpoint(
    db: AsyncSession,
    *,
    project_id: str,
    items: list[dict[str, Any]],
    source: str,
    trigger: str,
    reason: str,
    phase: str,
    implementation: str,
    model: str | None,
    transcript_path: str,
    extra_vision_payloads: list[dict[str, Any]] | None = None,
    max_images: int = 8,
) -> dict[str, Any]:
    """Replace model-visible rows with one typed checkpoint.

    Existing user/assistant rows remain active for the chat UI. Their metadata
    marks them model-invisible, so loading the next turn sees only the new
    developer checkpoint plus messages created after it.
    """

    encoded = encode_compaction_checkpoint(items)
    if not encoded:
        raise ValueError("cannot persist an empty compaction checkpoint")

    result = await db.exec(
        select(Message)
        .where(
            Message.project_id == project_id,
            Message.archived == False,  # noqa: E712
            Message.role.in_(("user", "assistant", "developer")),
        )
        .order_by(Message.created_at)
    )
    active = list(result.all())
    checkpoint = Message(
        project_id=project_id,
        role="developer",
        content=compaction_checkpoint_message(implementation)["content"],
        model_context_json=encoded,
    )

    vision_payloads: list[dict[str, Any]] = []
    replaced = 0
    for row in active:
        metadata = _metadata(row)
        if not _model_visible(metadata):
            continue
        vision_payload = vision_metadata_from_message(metadata)
        if vision_payload:
            vision_payloads.append(vision_payload)
        metadata["model_visible"] = False
        metadata["compacted_into"] = checkpoint.id
        row.metadata_json = json.dumps(metadata, ensure_ascii=False)
        db.add(row)
        replaced += 1
    vision_payloads.extend(extra_vision_payloads or [])

    checkpoint_metadata: dict[str, Any] = {
        "kind": "compaction_checkpoint",
        "source": source,
        "trigger": trigger,
        "reason": reason,
        "phase": phase,
        "implementation": implementation,
        "model": str(model or "") or None,
        "transcript": transcript_path,
        "model_visible": True,
    }
    merged_vision = _merged_vision_payload(
        vision_payloads,
        max_images=max(0, int(max_images)),
    )
    if merged_vision:
        checkpoint_metadata[VISION_METADATA_KEY] = merged_vision
    checkpoint.metadata_json = json.dumps(checkpoint_metadata, ensure_ascii=False)
    db.add(checkpoint)
    await db.commit()
    return {
        "checkpoint_id": checkpoint.id,
        "replaced_messages": replaced,
        "checkpoint_items": len(items),
        "retained_vision_references": len((merged_vision or {}).get("images") or []),
    }
