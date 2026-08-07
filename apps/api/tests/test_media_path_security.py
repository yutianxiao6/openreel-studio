from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.db.models import Asset
from app.services import media_provider
from app.services.media_path_security import resolve_project_media_file
from app.services.universal_adapter_service import _media_source


@pytest.mark.asyncio
async def test_provider_rejects_partially_resolved_images_before_indices_can_shift(
    monkeypatch,
) -> None:
    provider = SimpleNamespace(
        name="uma-provider",
        model_name="media-model",
        api_format="universal_adapter",
    )
    uma_calls: list[str] = []

    async def fake_active_provider(_kind: str):
        return provider

    async def fake_resolve(_project_id: str, _refs: list[str]):
        return ["https://example.test/valid.png"], ["找不到第二张参考图"]

    async def fake_generate_image(**_kwargs):
        uma_calls.append("image")
        return {"ok": True}

    async def fake_submit_video(**_kwargs):
        uma_calls.append("video")
        return {"ok": True}

    monkeypatch.setattr(media_provider, "_get_active_provider", fake_active_provider)
    monkeypatch.setattr(media_provider, "_resolve_reference_images", fake_resolve)
    monkeypatch.setattr(
        "app.services.universal_adapter_service.universal_adapter_service.generate_image",
        fake_generate_image,
    )
    monkeypatch.setattr(
        "app.services.universal_adapter_service.universal_adapter_service.submit_video",
        fake_submit_video,
    )

    image_result = await media_provider.generate_image_with_provider(
        project_id="project-1",
        prompt="人物看图片2",
        reference_images=["node:first", "node:second"],
    )
    video_result = await media_provider.generate_video_with_provider(
        project_id="project-1",
        prompt="人物看图片2",
        reference_images=["node:first", "node:second"],
    )

    assert image_result["error_kind"] == "bad_request"
    assert video_result["error_kind"] == "bad_request"
    assert "无法完整解析" in image_result["error"]
    assert "无法完整解析" in video_result["error"]
    assert uma_calls == []


def test_project_media_path_rejects_absolute_and_relative_escape(monkeypatch, tmp_path) -> None:
    storage = tmp_path / "storage"
    project_root = storage / "project-1"
    project_root.mkdir(parents=True)
    allowed = project_root / "uploads" / "reference.png"
    allowed.parent.mkdir()
    allowed.write_bytes(b"image")
    secret = tmp_path / "secret.env"
    secret.write_text("SECRET=value", encoding="utf-8")
    monkeypatch.setattr(settings, "STORAGE_PATH", str(storage))

    roots = (project_root.resolve(),)
    assert resolve_project_media_file("project-1", allowed, allowed_roots=roots) == allowed
    assert resolve_project_media_file("project-1", secret, allowed_roots=roots) is None
    assert resolve_project_media_file(
        "project-1",
        "../../secret.env",
        allowed_roots=roots,
    ) is None


def test_project_media_path_rejects_symlink_escape(monkeypatch, tmp_path) -> None:
    storage = tmp_path / "storage"
    project_root = storage / "project-1"
    project_root.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = project_root / "linked-secret.txt"
    link.symlink_to(secret)
    monkeypatch.setattr(settings, "STORAGE_PATH", str(storage))

    assert resolve_project_media_file(
        "project-1",
        link,
        allowed_roots=(project_root,),
    ) is None


@pytest.mark.asyncio
async def test_media_provider_rejects_host_file_reference(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "storage" / "project-1"
    project_root.mkdir(parents=True)
    host_file = tmp_path / "hosts"
    host_file.write_text("127.0.0.1", encoding="utf-8")

    async def fake_roots(_project_id: str) -> tuple[Path, ...]:
        return (project_root,)

    monkeypatch.setattr(media_provider, "project_media_roots", fake_roots)

    images, image_errors = await media_provider._resolve_reference_images(
        "project-1",
        [str(host_file)],
    )
    audios, audio_errors = await media_provider._resolve_reference_media(
        "project-1",
        "audio",
        [str(host_file)],
    )

    assert images == []
    assert audios == []
    assert "超出当前项目允许范围" in image_errors[0]
    assert "超出当前项目允许范围" in audio_errors[0]


@pytest.mark.asyncio
async def test_media_provider_rejects_asset_owned_by_another_project(monkeypatch, tmp_path) -> None:
    foreign_asset = Asset(
        id="asset-foreign",
        project_id="project-2",
        type="image",
        name="foreign",
        path=str(tmp_path / "foreign.png"),
    )

    class FakeSession:
        async def get(self, model, asset_id):
            assert model is Asset
            assert asset_id == "asset-foreign"
            return foreign_asset

    @asynccontextmanager
    async def fake_session_scope():
        yield FakeSession()

    async def fake_roots(_project_id: str) -> tuple[Path, ...]:
        return ((tmp_path / "storage" / "project-1").resolve(),)

    monkeypatch.setattr(media_provider, "session_scope", fake_session_scope)
    monkeypatch.setattr(media_provider, "project_media_roots", fake_roots)

    resolved, errors = await media_provider._resolve_reference_images(
        "project-1",
        ["asset:asset-foreign"],
    )

    assert resolved == []
    assert errors == ["找不到资产 asset:asset-foreign"]


@pytest.mark.asyncio
async def test_media_provider_rejects_project_asset_pointing_outside_allowed_roots(monkeypatch, tmp_path) -> None:
    external_file = tmp_path / "host-secret.png"
    external_file.write_bytes(b"secret")
    unsafe_asset = Asset(
        id="asset-unsafe",
        project_id="project-1",
        type="image",
        name="unsafe",
        path=str(external_file),
    )

    class FakeSession:
        async def get(self, model, asset_id):
            return unsafe_asset

    @asynccontextmanager
    async def fake_session_scope():
        yield FakeSession()

    project_root = tmp_path / "storage" / "project-1"

    async def fake_roots(_project_id: str) -> tuple[Path, ...]:
        return (project_root.resolve(),)

    monkeypatch.setattr(media_provider, "session_scope", fake_session_scope)
    monkeypatch.setattr(media_provider, "project_media_roots", fake_roots)

    resolved, errors = await media_provider._resolve_reference_images(
        "project-1",
        ["asset:asset-unsafe"],
    )

    assert resolved == []
    assert errors == ["资产 asset-unsafe 的本地文件超出当前项目允许范围"]


def test_uma_bridge_revalidates_local_path_scope(tmp_path) -> None:
    project_root = tmp_path / "storage" / "project-1"
    project_root.mkdir(parents=True)
    host_file = tmp_path / "host-secret"
    host_file.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="allowed project media path"):
        _media_source(
            "project-1",
            str(host_file),
            allowed_local_roots=(project_root,),
        )
