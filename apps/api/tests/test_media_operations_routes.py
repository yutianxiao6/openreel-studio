import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import routes_projects, routes_video_editor
from app.config import settings
from app.db import session as db_session
from app.db.models import Project, WorkflowEdge, WorkflowNode
from app.services import media_operations, video_edit_sequences


async def _setup_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'media-ops.db'}"
    engine = create_async_engine(database_url, echo=False, future=True, connect_args={"timeout": 30})
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    await db_session.init_db()
    async with db_session.session_scope() as session:
        session.add(Project(id="project-1", title="媒体操作测试", state_json="{}"))
        session.add(
            WorkflowNode(
                id="video-source",
                project_id="project-1",
                display_id=1,
                type="video",
                title="源视频",
                status="completed",
                position_x=100,
                position_y=200,
                output_json=json.dumps({
                    "type": "video",
                    "local_url": "/api/media/project-1/generated_videos/source.mp4",
                    "video": {"local_url": "/api/media/project-1/generated_videos/source.mp4"},
                }),
            )
        )
        await session.commit()


def _write_media_result(tmp_path: Path, project_id: str, kind: str, filename: str) -> tuple[str, Path]:
    dirname = media_operations.project_media_history.MEDIA_HISTORY_DIRS[kind]
    rel_path = f"{dirname}/video_ops/{filename}"
    path = tmp_path / "storage" / project_id / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    return rel_path, path


@pytest.mark.asyncio
async def test_video_export_frame_creates_image_node_and_dependency_edge(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)

    async def fake_export(project_id: str, node: WorkflowNode, **kwargs):
        rel_path, path = _write_media_result(tmp_path, project_id, "image", "tail.png")
        return media_operations.MediaOperationFile(
            kind="image",
            rel_path=rel_path,
            path=path,
            title=kwargs.get("title") or "源视频 尾帧",
            metadata={"type": "video.export_frame", "source_node_id": node.id, "frame_mode": kwargs.get("mode")},
        )

    monkeypatch.setattr(routes_projects.media_operations, "export_video_frame", fake_export)

    async with db_session.session_scope() as session:
        result = await routes_projects.run_project_media_operation(
            "project-1",
            routes_projects.ProjectMediaOperationRequest(
                operation="video.export_frame",
                source_node_id="video-source",
                frame_mode="tail",
                title="源视频 尾帧",
            ),
            session,
        )
        assert result["ok"] is True
        created = result["nodes"][0]
        assert created["type"] == "image"
        assert created["status"] == "completed"
        assert created["position"] == {"x": 480.0, "y": 200.0}
        assert created["output"]["images"][0]["local_url"].endswith("/generated_images/video_ops/tail.png")
        assert result["edges"][0]["source_node_id"] == "video-source"
        assert result["edges"][0]["target_node_id"] == created["id"]

        node = await session.get(WorkflowNode, created["id"])
        assert node is not None
        assert node.type == "image"
        assert "node:1" in json.loads(node.input_json or "{}")["depends_on"]


@pytest.mark.asyncio
async def test_video_split_tracks_creates_video_and_audio_nodes(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)

    async def fake_split(project_id: str, node: WorkflowNode):
        video_rel, video_path = _write_media_result(tmp_path, project_id, "video", "track.mp4")
        audio_rel, audio_path = _write_media_result(tmp_path, project_id, "audio", "track.m4a")
        return [
            media_operations.MediaOperationFile(
                kind="video",
                rel_path=video_rel,
                path=video_path,
                title="源视频 画面",
                metadata={"type": "video.split_tracks", "track": "video", "source_node_id": node.id},
            ),
            media_operations.MediaOperationFile(
                kind="audio",
                rel_path=audio_rel,
                path=audio_path,
                title="源视频 声音",
                metadata={"type": "video.split_tracks", "track": "audio", "source_node_id": node.id},
            ),
        ]

    monkeypatch.setattr(routes_projects.media_operations, "split_video_tracks", fake_split)

    async with db_session.session_scope() as session:
        result = await routes_projects.run_project_media_operation(
            "project-1",
            routes_projects.ProjectMediaOperationRequest(
                operation="video.split_tracks",
                source_node_id="video-source",
            ),
            session,
        )
        node_types = [node["type"] for node in result["nodes"]]
        assert node_types == ["video", "audio"]
        assert [node["position"]["y"] for node in result["nodes"]] == [75.0, 325.0]
        assert len(result["edges"]) == 2

        created_ids = [node["id"] for node in result["nodes"]]
        persisted_edges = (await session.exec(select(WorkflowEdge).where(
            WorkflowEdge.project_id == "project-1",
            WorkflowEdge.source_node_id == "video-source",
        ))).all()
        assert sorted(edge.target_node_id for edge in persisted_edges) == sorted(created_ids)


@pytest.mark.asyncio
async def test_video_sequence_render_creates_video_node_and_dependency_edge(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    spec = video_edit_sequences.SequenceSpec.model_validate({
        "schema_version": "openreel.video_sequence.v1",
        "settings": {
            "frame_rate": {"numerator": 24, "denominator": 1},
            "width": 1280,
            "height": 720,
            "audio_sample_rate": 48_000,
            "audio_channels": 2,
        },
        "tracks": [
            {"id": "v1", "kind": "video", "name": "Video 1", "order": 0},
            {"id": "a1", "kind": "audio", "name": "Audio 1", "order": 0},
        ],
        "clips": [
            {
                "id": "video-clip",
                "track_id": "v1",
                "media_id": "video-source",
                "timeline_start_frame": 0,
                "duration_frames": 48,
                "source_in_frame": 0,
                "source_frame_count": 48,
            },
            {
                "id": "audio-clip",
                "track_id": "a1",
                "media_id": "embedded-audio:video-source",
                "timeline_start_frame": 0,
                "duration_frames": 48,
                "source_in_frame": 0,
                "source_frame_count": 48,
            },
        ],
    })
    async with db_session.session_scope() as session:
        document = await video_edit_sequences.save_sequence(
            session,
            project_id="project-1",
            node_id="video-source",
            expected_revision=0,
            spec=spec,
        )

    async def fake_render(project_id: str, sequence, **kwargs):
        rel_path, path = _write_media_result(tmp_path, project_id, "video", "sequence.mp4")
        return media_operations.MediaOperationFile(
            kind="video",
            rel_path=rel_path,
            path=path,
            title=kwargs.get("title") or "时间线成片",
            metadata={
                "type": "video.render_sequence",
                "sequence_revision": kwargs["revision"],
                "duration_frames": 48,
                "frame_rate": {"numerator": 24, "denominator": 1},
                "width": 1280,
                "height": 720,
                "audio_sample_rate": 48_000,
                "audio_channels": 2,
                "source_node_ids": ["video-source"],
                "transition_count": 0,
            },
        )

    monkeypatch.setattr(routes_video_editor.video_sequence_renderer, "render_sequence", fake_render)

    async with db_session.session_scope() as session:
        result = await routes_video_editor.render_video_edit_sequence(
            "project-1",
            "video-source",
            routes_video_editor.RenderSequenceRequest(
                expected_revision=document.revision,
                title="正式成片",
            ),
            session,
        )
        assert result["ok"] is True
        assert result["sequence_revision"] == 1
        assert result["node"]["type"] == "video"
        assert result["node"]["title"] == "正式成片"
        rendered_output = json.loads(result["node"]["output_json"])
        assert rendered_output["video"]["local_url"].endswith(
            "/generated_videos/video_ops/sequence.mp4"
        )
        assert result["edges"][0]["source_node_id"] == "video-source"
        assert result["edges"][0]["target_node_id"] == result["node"]["id"]

        created = await session.get(WorkflowNode, result["node"]["id"])
        assert created is not None
        node_input = json.loads(created.input_json or "{}")
        assert node_input["source"]["sequence_revision"] == 1
        assert node_input["depends_on"] == ["node:1"]
