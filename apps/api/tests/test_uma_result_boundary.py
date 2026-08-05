from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from universal_model_adapter import (
    AudioOutput,
    FileOutput,
    ImageOutput,
    InvocationError,
    InvocationResult,
    JsonOutput,
    TextOutput,
    ToolCallOutput,
    VideoOutput,
)

from app.config import settings
from app.services import media_generation
from app.services.node_public_ids import model_visible_node_output, model_visible_node_payload
from app.services.uma_result_store import (
    archive_invocation_result,
    load_invocation_result_archive,
)
from app.services.universal_adapter_service import UniversalAdapterService


def _invocation_result(*, invocation_id: str, outputs: tuple, status: str = "completed"):
    return InvocationResult(
        id=invocation_id,
        status=status,
        kind="video",
        operation="video.generate",
        model="mixed-output-model",
        outputs=outputs,
        error=(
            InvocationError(
                code="provider_failed",
                message="provider rejected the final result",
                stage="poll",
                retryable=False,
                details={"provider_payload": {"reason": "moderation"}},
            )
            if status == "failed"
            else None
        ),
        created_at=datetime.now(UTC),
        metadata={"request_class": "integration"},
        extensions={"vendor.trace": {"attempt": 2}},
    )


@pytest.mark.asyncio
async def test_uma_archive_preserves_every_output_type_without_embedding_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"wave")
    result = _invocation_result(
        invocation_id="all-output-types",
        outputs=(
            TextOutput(text="provider note", language="en", finish_reason="stop"),
            JsonOutput(value={"score": 0.9}, schema_id="review.v1"),
            ToolCallOutput(
                id="call-1",
                name="publish",
                arguments={"target": "canvas"},
                status="completed",
            ),
            ImageOutput(
                data=b"fake-png",
                mime_type="image/png",
                width=32,
                height=16,
                seed=7,
            ),
            VideoOutput(
                url="https://assets.example.invalid/video.mp4",
                mime_type="video/mp4",
                duration_seconds=5.5,
                fps=24,
            ),
            AudioOutput(
                path=audio_path,
                mime_type="audio/wav",
                duration_seconds=3.2,
                sample_rate_hz=48_000,
                transcript="internal transcript",
            ),
            FileOutput(
                store_id="provider-store:file-7",
                filename="report.zip",
                byte_size=123,
                mime_type="application/zip",
            ),
        ),
    )

    path = await archive_invocation_result(
        project_id="project/all",
        result=result,
        materialized_outputs={
            4: {"local_url": "/api/media/project/all/video.mp4", "local_path": "/tmp/video.mp4"}
        },
    )

    assert path is not None and path.is_file()
    archived = load_invocation_result_archive("project/all", result.id)
    assert archived is not None
    assert [item["type"] for item in archived["outputs"]] == [
        "text",
        "json",
        "tool_call",
        "image",
        "video",
        "audio",
        "file",
    ]
    assert archived["metadata"] == {"request_class": "integration"}
    assert archived["extensions"] == {"vendor.trace": {"attempt": 2}}
    assert archived["outputs"][5]["transcript"] == "internal transcript"
    assert archived["outputs"][6]["store_id"] == "provider-store:file-7"
    assert archived["outputs"][4]["host_materialization"]["local_path"] == "/tmp/video.mp4"
    image_archive = archived["outputs"][3]
    assert "data" not in image_archive
    binary = path.parent / image_archive["data_artifact"]["filename"]
    assert binary.read_bytes() == b"fake-png"
    assert image_archive["data_artifact"]["size_bytes"] == len(b"fake-png")


@pytest.mark.asyncio
async def test_media_projection_returns_all_expected_videos_and_archives_mixed_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    result = _invocation_result(
        invocation_id="two-videos-mixed",
        outputs=(
            TextOutput(text="internal provider note"),
            VideoOutput(
                url="https://assets.example.invalid/a.mp4",
                duration_seconds=4,
                width=1280,
                height=720,
            ),
            JsonOutput(value={"private": True}),
            VideoOutput(
                url="https://assets.example.invalid/b.mp4",
                duration_seconds=6,
                fps=30,
            ),
            FileOutput(store_id="provider-store:log", filename="trace.json"),
        ),
    ).model_copy(
        update={
            "metadata": {"diagnostic_blob": "x" * 50_000},
            "extensions": {"vendor.raw": {"response": "y" * 50_000}},
        }
    )
    binding = SimpleNamespace(
        kind="video",
        provider_name="test-provider",
        remote_model="video-v1",
        options=SimpleNamespace(max_output_bytes=10_000_000),
    )
    job = SimpleNamespace(binding=binding, project_id="project-video", save_locally=False)
    service = UniversalAdapterService()
    try:
        projected = await service._media_result(job, result)
    finally:
        await service.aclose()

    assert projected["ok"] is True
    assert [item["remote_url"] for item in projected["videos"]] == [
        "https://assets.example.invalid/a.mp4",
        "https://assets.example.invalid/b.mp4",
    ]
    assert projected["url"] == "https://assets.example.invalid/a.mp4"
    assert "outputs" not in projected
    assert "extensions" not in projected
    assert "metadata" not in projected
    assert "text" not in projected
    assert "value" not in projected
    assert len(str(projected)) < 5_000
    archived = load_invocation_result_archive("project-video", result.id)
    assert archived is not None
    assert len(archived["metadata"]["diagnostic_blob"]) == 50_000
    assert len(archived["extensions"]["vendor.raw"]["response"]) == 50_000
    assert [item["type"] for item in archived["outputs"]] == [
        "text",
        "video",
        "json",
        "video",
        "file",
    ]


@pytest.mark.asyncio
async def test_failed_partial_and_store_only_outputs_are_archived_but_not_exposed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    binding = SimpleNamespace(
        kind="video",
        provider_name="test-provider",
        remote_model="video-v1",
        options=SimpleNamespace(max_output_bytes=10_000_000),
    )
    job = SimpleNamespace(binding=binding, project_id="project-failed", save_locally=False)
    service = UniversalAdapterService()
    try:
        failed_result = _invocation_result(
            invocation_id="failed-with-partial",
            status="failed",
            outputs=(
                VideoOutput(url="https://assets.example.invalid/partial.mp4"),
                TextOutput(text="private failure detail"),
            ),
        )
        failed = await service._media_result(job, failed_result)
        store_result = _invocation_result(
            invocation_id="store-only",
            outputs=(VideoOutput(store_id="provider-store:video-1"),),
        )
        store_only = await service._media_result(job, store_result)
    finally:
        await service.aclose()

    assert failed["ok"] is False
    assert "videos" not in failed
    assert "partial.mp4" not in str(failed)
    failed_archive = load_invocation_result_archive("project-failed", failed_result.id)
    assert failed_archive is not None
    assert failed_archive["outputs"][0]["url"].endswith("partial.mp4")
    assert failed_archive["error"]["details"]["provider_payload"]["reason"] == "moderation"

    assert store_only["ok"] is False
    assert store_only["error_kind"] == "artifact_unavailable"
    assert "provider-store:video-1" not in str(store_only)
    store_archive = load_invocation_result_archive("project-failed", store_result.id)
    assert store_archive is not None
    assert store_archive["outputs"][0]["store_id"] == "provider-store:video-1"


@pytest.mark.asyncio
async def test_media_generation_registers_every_video_output(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[dict] = []

    async def fake_register_asset(**kwargs):
        registered.append(kwargs)
        return {"id": f"asset-{len(registered)}"}

    monkeypatch.setattr(media_generation, "register_asset", fake_register_asset)
    provider_result = {
        "ok": True,
        "status": "completed",
        "provider": "video-provider",
        "model": "video-v1",
        "url": "/api/media/project/a.mp4",
        "local_url": "/api/media/project/a.mp4",
        "videos": [
            {
                "url": "/api/media/project/a.mp4",
                "local_url": "/api/media/project/a.mp4",
                "local_path": "/tmp/a.mp4",
                "duration": 4,
                "output_index": 1,
            },
            {
                "url": "/api/media/project/b.mp4",
                "local_url": "/api/media/project/b.mp4",
                "local_path": "/tmp/b.mp4",
                "duration": 6,
                "output_index": 3,
            },
        ],
    }
    asset_ids = await media_generation._register_video_assets(
        project_id="project",
        prompt="two videos",
        shot_id=None,
        node_id="node-1",
        model="video-v1",
        result=provider_result,
        refs_provided=[],
        first_frame_asset_id=None,
        last_frame_asset_id=None,
        duration_seconds=5,
        aspect_ratio="16:9",
        resolution="720p",
    )
    output = media_generation._video_output(
        provider_result,
        asset_id=asset_ids[0],
        asset_ids=asset_ids,
        duration_seconds=5,
        aspect_ratio="16:9",
        resolution="720p",
        reference_images=[],
    )

    assert asset_ids == ["asset-1", "asset-2"]
    assert [item["url"] for item in registered] == [
        "/api/media/project/a.mp4",
        "/api/media/project/b.mp4",
    ]
    assert output["asset_ids"] == asset_ids
    assert [item["asset_id"] for item in output["videos"]] == asset_ids


def test_agent_node_projection_keeps_media_and_strips_private_runtime_state() -> None:
    stored_output = {
        "type": "video",
        "status": "completed",
        "job_id": "job-1",
        "provider_task_id": "provider-task-1",
        "url": "/api/media/project/a.mp4",
        "adapter_resume_request": {"input": {"prompt": "large private request"}},
        "adapter_route": {"protocol_hash": "private"},
        "usage": {"provider_units": 12},
        "polls": [{"raw": "large provider payload"}],
        "local_path": "/private/storage/a.mp4",
        "history": [{"output": {"url": "/api/media/project/old.mp4"}}],
        "videos": [
            {
                "url": "/api/media/project/a.mp4",
                "local_url": "/api/media/project/a.mp4",
                "local_path": "/private/storage/a.mp4",
                "duration": 5,
            }
        ],
    }
    node = {
        "id": "internal-node-id",
        "display_id": 7,
        "project_id": "project",
        "type": "video",
        "status": "completed",
        "output": stored_output,
    }

    projected = model_visible_node_payload(node)

    assert projected["id"] == "7"
    assert projected["output"]["job_id"] == "job-1"
    assert projected["output"]["provider_task_id"] == "provider-task-1"
    assert projected["output"]["videos"][0]["url"].endswith("a.mp4")
    rendered = str(projected["output"])
    for private_value in (
        "adapter_resume_request",
        "adapter_route",
        "provider_units",
        "large provider payload",
        "/private/storage/a.mp4",
        "old.mp4",
    ):
        assert private_value not in rendered

    usage_payload = {
        "result": stored_output,
        "_subagent_usage": [{"agent": "runner", "usage": {"total_tokens": 42}}],
    }
    projected_usage = model_visible_node_output(usage_payload)
    assert "usage" not in projected_usage["result"]
    assert projected_usage["_subagent_usage"][0]["usage"]["total_tokens"] == 42
