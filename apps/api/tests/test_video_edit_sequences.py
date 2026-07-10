from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Project, WorkflowNode
from app.services import video_edit_sequences


def sequence_spec(*, gain_db: float = 0.0) -> video_edit_sequences.SequenceSpec:
    return video_edit_sequences.SequenceSpec.model_validate({
        "schema_version": video_edit_sequences.SEQUENCE_SCHEMA_VERSION,
        "settings": {
            "frame_rate": {"numerator": 24, "denominator": 1},
            "width": 1920,
            "height": 1080,
            "audio_sample_rate": 48_000,
            "audio_channels": 2,
        },
        "tracks": [
            {"id": "v1", "kind": "video", "name": "Video 1", "order": 0},
            {"id": "a1", "kind": "audio", "name": "Audio 1", "order": 0},
        ],
        "clips": [
            {
                "id": "clip-video-1",
                "track_id": "v1",
                "media_id": "video-1",
                "timeline_start_frame": 0,
                "duration_frames": 120,
                "source_in_frame": 0,
                "source_frame_count": 361,
                "linked_group_id": "link-1",
            },
            {
                "id": "clip-audio-1",
                "track_id": "a1",
                "media_id": "embedded-audio:video-1",
                "timeline_start_frame": 0,
                "duration_frames": 120,
                "source_in_frame": 0,
                "source_frame_count": 361,
                "linked_group_id": "link-1",
                "gain_db": gain_db,
            },
        ],
    })


@pytest.mark.asyncio
async def test_sequence_persistence_revision_conflict_history_and_restore(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'video-editor.db'}", future=True)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    try:
        async with session_local() as db:
            db.add(Project(id="project-1", title="Project"))
            db.add(WorkflowNode(id="video-1", project_id="project-1", type="video", title="Video"))
            await db.commit()

            created = await video_edit_sequences.save_sequence(
                db,
                project_id="project-1",
                node_id="video-1",
                expected_revision=0,
                spec=sequence_spec(),
            )
            assert created.revision == 1
            assert created.spec.clips[0].duration_frames == 120

            updated = await video_edit_sequences.save_sequence(
                db,
                project_id="project-1",
                node_id="video-1",
                expected_revision=1,
                spec=sequence_spec(gain_db=-6.0),
            )
            assert updated.revision == 2
            assert updated.spec.clips[1].gain_db == -6.0

            with pytest.raises(video_edit_sequences.SequenceRevisionConflict) as conflict:
                await video_edit_sequences.save_sequence(
                    db,
                    project_id="project-1",
                    node_id="video-1",
                    expected_revision=1,
                    spec=sequence_spec(gain_db=-12.0),
                )
            assert conflict.value.current_revision == 2

            history = await video_edit_sequences.sequence_history(
                db,
                project_id="project-1",
                node_id="video-1",
            )
            assert [item.revision for item in history] == [2, 1]

            restored = await video_edit_sequences.restore_sequence(
                db,
                project_id="project-1",
                node_id="video-1",
                expected_revision=2,
                target_revision=1,
            )
            assert restored.revision == 3
            assert restored.spec.clips[1].gain_db == 0.0
    finally:
        await engine.dispose()


def test_sequence_contract_rejects_unknown_tracks_and_source_overflow() -> None:
    payload = sequence_spec().model_dump(mode="json")
    payload["clips"][0]["track_id"] = "missing"
    with pytest.raises(ValidationError, match="Unknown clip track"):
        video_edit_sequences.SequenceSpec.model_validate(payload)

    payload = sequence_spec().model_dump(mode="json")
    payload["clips"][0]["source_in_frame"] = 300
    payload["clips"][0]["duration_frames"] = 120
    with pytest.raises(ValidationError, match="exceeds source frame count"):
        video_edit_sequences.SequenceSpec.model_validate(payload)
