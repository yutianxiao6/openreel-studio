"""Frame-accurate persisted sequence contracts for the basic video editor."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import VideoEditSequence, VideoEditSequenceRevision


SEQUENCE_SCHEMA_VERSION = "openreel.video_sequence.v1"


class SequenceRevisionConflict(ValueError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("Sequence revision conflict")
        self.current_revision = current_revision


class SequenceNotFound(ValueError):
    pass


class FrameRate(BaseModel):
    numerator: int = Field(ge=1, le=240_000)
    denominator: int = Field(ge=1, le=100_000)


class SequenceSettings(BaseModel):
    frame_rate: FrameRate
    width: int = Field(ge=16, le=16_384)
    height: int = Field(ge=16, le=16_384)
    audio_sample_rate: int = Field(default=48_000, ge=8_000, le=384_000)
    audio_channels: int = Field(default=2, ge=1, le=32)


class SequenceTrack(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    kind: Literal["video", "audio"]
    name: str = Field(min_length=1, max_length=160)
    order: int = Field(ge=0, le=999)
    locked: bool = False
    sync_locked: bool = True
    visible: bool = True
    muted: bool = False
    solo: bool = False
    gain_db: float = Field(default=0.0, ge=-120.0, le=24.0)


class SequenceClip(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    track_id: str = Field(min_length=1, max_length=120)
    media_id: str = Field(min_length=1, max_length=240)
    timeline_start_frame: int = Field(ge=0)
    duration_frames: int = Field(ge=1)
    source_in_frame: int = Field(ge=0)
    source_frame_count: int | None = Field(default=None, ge=1)
    linked_group_id: str | None = Field(default=None, max_length=240)
    gain_db: float = Field(default=0.0, ge=-120.0, le=24.0)
    muted: bool = False
    fade_in_frames: int = Field(default=0, ge=0)
    fade_out_frames: int = Field(default=0, ge=0)


class SequenceSpec(BaseModel):
    schema_version: Literal[SEQUENCE_SCHEMA_VERSION] = SEQUENCE_SCHEMA_VERSION
    settings: SequenceSettings
    tracks: list[SequenceTrack]
    clips: list[SequenceClip]

    @model_validator(mode="after")
    def validate_graph(self) -> "SequenceSpec":
        track_ids = [track.id for track in self.tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("Track ids must be unique")
        track_orders = [(track.kind, track.order) for track in self.tracks]
        if len(track_orders) != len(set(track_orders)):
            raise ValueError("Track order must be unique within each track kind")
        clip_ids = [clip.id for clip in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("Clip ids must be unique")
        known_tracks = {track.id: track for track in self.tracks}
        for clip in self.clips:
            if clip.track_id not in known_tracks:
                raise ValueError(f"Unknown clip track: {clip.track_id}")
            if clip.source_frame_count is not None:
                source_out = clip.source_in_frame + clip.duration_frames
                if source_out > clip.source_frame_count:
                    raise ValueError(f"Clip exceeds source frame count: {clip.id}")
            if clip.fade_in_frames + clip.fade_out_frames > clip.duration_frames:
                raise ValueError(f"Clip fades exceed duration: {clip.id}")
        return self


class SequenceDocument(BaseModel):
    project_id: str
    node_id: str
    revision: int
    spec: SequenceSpec
    created_at: datetime
    updated_at: datetime


class SequenceHistoryItem(BaseModel):
    revision: int
    created_at: datetime


def _serialize_spec(spec: SequenceSpec) -> str:
    return json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _document(row: VideoEditSequence) -> SequenceDocument:
    return SequenceDocument(
        project_id=row.project_id,
        node_id=row.node_id,
        revision=row.revision,
        spec=SequenceSpec.model_validate_json(row.spec_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def read_sequence(db: AsyncSession, node_id: str) -> SequenceDocument | None:
    row = await db.get(VideoEditSequence, node_id)
    return _document(row) if row else None


async def save_sequence(
    db: AsyncSession,
    *,
    project_id: str,
    node_id: str,
    expected_revision: int,
    spec: SequenceSpec,
) -> SequenceDocument:
    now = datetime.utcnow()
    payload = _serialize_spec(spec)
    current = await db.get(VideoEditSequence, node_id)
    if current is None:
        if expected_revision != 0:
            raise SequenceRevisionConflict(0)
        row = VideoEditSequence(
            node_id=node_id,
            project_id=project_id,
            spec_json=payload,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.add(VideoEditSequenceRevision(
            node_id=node_id,
            project_id=project_id,
            revision=1,
            spec_json=payload,
            created_at=now,
        ))
        await db.commit()
        await db.refresh(row)
        return _document(row)

    if current.project_id != project_id:
        raise SequenceNotFound("Sequence not found")
    if current.revision != expected_revision:
        raise SequenceRevisionConflict(current.revision)

    next_revision = expected_revision + 1
    result = await db.exec(
        update(VideoEditSequence)
        .where(
            VideoEditSequence.node_id == node_id,
            VideoEditSequence.project_id == project_id,
            VideoEditSequence.revision == expected_revision,
        )
        .values(spec_json=payload, revision=next_revision, updated_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        latest = await db.get(VideoEditSequence, node_id)
        raise SequenceRevisionConflict(latest.revision if latest else 0)
    db.add(VideoEditSequenceRevision(
        node_id=node_id,
        project_id=project_id,
        revision=next_revision,
        spec_json=payload,
        created_at=now,
    ))
    await db.commit()
    updated = await db.get(VideoEditSequence, node_id)
    if updated is None:
        raise SequenceNotFound("Sequence not found after save")
    await db.refresh(updated)
    return _document(updated)


async def sequence_history(
    db: AsyncSession,
    *,
    project_id: str,
    node_id: str,
    limit: int = 50,
) -> list[SequenceHistoryItem]:
    rows = (await db.exec(
        select(VideoEditSequenceRevision)
        .where(
            VideoEditSequenceRevision.project_id == project_id,
            VideoEditSequenceRevision.node_id == node_id,
        )
        .order_by(VideoEditSequenceRevision.revision.desc())
        .limit(max(1, min(limit, 200)))
    )).all()
    return [SequenceHistoryItem(revision=row.revision, created_at=row.created_at) for row in rows]


async def restore_sequence(
    db: AsyncSession,
    *,
    project_id: str,
    node_id: str,
    expected_revision: int,
    target_revision: int,
) -> SequenceDocument:
    snapshot = (await db.exec(
        select(VideoEditSequenceRevision).where(
            VideoEditSequenceRevision.project_id == project_id,
            VideoEditSequenceRevision.node_id == node_id,
            VideoEditSequenceRevision.revision == target_revision,
        )
    )).first()
    if snapshot is None:
        raise SequenceNotFound("Sequence revision not found")
    return await save_sequence(
        db,
        project_id=project_id,
        node_id=node_id,
        expected_revision=expected_revision,
        spec=SequenceSpec.model_validate_json(snapshot.spec_json),
    )
