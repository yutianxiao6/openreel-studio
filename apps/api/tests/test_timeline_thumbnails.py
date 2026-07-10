from pathlib import Path

import pytest

from app.api import routes_video_editor
from app.config import settings
from app.db.models import WorkflowNode
from app.services import timeline_thumbnails


@pytest.mark.asyncio
async def test_timeline_sprite_is_generated_once_and_cached(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path / "storage"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    calls = 0

    async def fake_render(source_path: Path, target: Path, **kwargs) -> None:
        nonlocal calls
        calls += 1
        assert source_path == source
        assert kwargs["frame_count"] == 18
        target.write_bytes(b"jpeg-sprite")

    monkeypatch.setattr(timeline_thumbnails, "_render_sprite", fake_render)
    first = await timeline_thumbnails.ensure_timeline_sprite(
        "project-1",
        source,
        frame_count=18,
        duration_seconds=15.0,
    )
    second = await timeline_thumbnails.ensure_timeline_sprite(
        "project-1",
        source,
        frame_count=18,
        duration_seconds=15.0,
    )

    assert first == second
    assert first.read_bytes() == b"jpeg-sprite"
    assert calls == 1


@pytest.mark.asyncio
async def test_timeline_sprite_rejects_path_traversal_project_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    with pytest.raises(timeline_thumbnails.TimelineThumbnailError, match="Invalid project id"):
        await timeline_thumbnails.ensure_timeline_sprite(
            "..",
            source,
            frame_count=18,
            duration_seconds=15.0,
        )


@pytest.mark.asyncio
async def test_timeline_sprite_route_validates_node_and_returns_image(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    sprite = tmp_path / "sprite.jpg"
    sprite.write_bytes(b"jpeg-sprite")
    node = WorkflowNode(id="video-1", project_id="project-1", type="video", title="Video")

    class FakeSession:
        async def get(self, model, node_id):
            assert model is WorkflowNode
            assert node_id == "video-1"
            return node

    async def fake_media_path(project_id, source_node, kind):
        assert (project_id, source_node.id, kind) == ("project-1", "video-1", "video")
        return source

    async def fake_sprite(project_id, source_path, **kwargs):
        assert project_id == "project-1"
        assert source_path == source
        assert kwargs["frame_count"] == 24
        return sprite

    monkeypatch.setattr(routes_video_editor.media_operations, "media_path_for_node", fake_media_path)
    monkeypatch.setattr(routes_video_editor.timeline_thumbnails, "ensure_timeline_sprite", fake_sprite)
    response = await routes_video_editor.get_timeline_sprite(
        "project-1",
        "video-1",
        frame_count=24,
        duration_seconds=15.0,
        frame_width=128,
        frame_height=72,
        db=FakeSession(),
    )

    assert response.path == str(sprite)
    assert response.media_type == "image/jpeg"
    assert response.headers["x-openreel-frame-count"] == "24"
