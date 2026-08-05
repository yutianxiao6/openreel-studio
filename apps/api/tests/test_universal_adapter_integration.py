from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config import settings
from app.config_store.schema import MediaProviderEntry
from app.services import media_generation, media_provider, node_recovery
from app.services.universal_adapter_config import (
    create_universal_adapter_binding,
    universal_adapter_cache_key,
)
from app.services.universal_adapter_service import UniversalAdapterService
from app.services.audio_target_catalog import load_audio_target_catalog
from app.services.image_target_catalog import load_image_target_catalog
from app.services.video_target_catalog import load_video_target_catalog


def _provider(
    *,
    kind: str,
    protocol_id: str,
    operation: str,
    model: str,
    uma: dict[str, Any] | None = None,
) -> tuple[SimpleNamespace, dict[str, Any]]:
    provider = SimpleNamespace(
        kind=kind,
        name=f"{kind}-uma-test",
        base_url="https://provider.example.invalid",
        api_key="secret-test-key",
        model_name=model,
        api_format="universal_adapter",
    )
    return provider, {
        "uma": {
            "protocol_id": protocol_id,
            "operation": operation,
            "poll_interval_seconds": 0,
            **(uma or {}),
        }
    }


def _service_with_binding(
    provider: SimpleNamespace,
    provider_params: dict[str, Any],
    handler: Any,
    *,
    download_handler: Any | None = None,
) -> tuple[UniversalAdapterService, Any]:
    binding = create_universal_adapter_binding(
        provider,
        provider_params,
        transport=httpx.MockTransport(handler),
    )
    if download_handler is None:
        service = UniversalAdapterService()
    else:
        service = UniversalAdapterService(
            download_client=httpx.AsyncClient(transport=httpx.MockTransport(download_handler)),
        )
        service._owns_download_client = True
    service._bindings[universal_adapter_cache_key(provider, provider_params)] = binding
    return service, binding


def test_runtime_config_accepts_targeted_universal_adapter_provider() -> None:
    entry = MediaProviderEntry(
        kind="image",
        name="image-uma",
        base_url="https://provider.example.invalid",
        api_key="${IMAGE_API_KEY}",
        model_name="gpt-image-2",
        api_format="universal_adapter",
        params={
            "uma": {
                "protocol_id": "openai.compatible-images-generations",
                "operation": "image.generate",
                "target_profile_id": "openai.compatible-images-generations:gpt-image-2",
            }
        },
    )

    assert entry.params["uma"]["protocol_id"] == "openai.compatible-images-generations"


def test_runtime_config_rejects_inline_universal_adapter_protocol() -> None:
    with pytest.raises(ValidationError, match="不能内嵌协议内容"):
        MediaProviderEntry(
            kind="video",
            name="video-uma",
            base_url="https://provider.example.invalid",
            api_key="secret",
            model_name="video-v1",
            api_format="universal_adapter",
            params={
                "uma": {
                    "protocol_id": "custom.video",
                    "operation": "video.generate",
                    "protocol": {"operations": {}},
                }
            },
        )


def test_runtime_config_rejects_blank_universal_adapter_protocol_id() -> None:
    with pytest.raises(ValidationError, match="protocol_id"):
        MediaProviderEntry(
            kind="image",
            name="image-uma",
            base_url="https://provider.example.invalid",
            api_key="secret",
            model_name="image-v1",
            api_format="universal_adapter",
            params={
                "uma": {
                    "protocol_id": "   ",
                    "operation": "image.generate",
                    "target_profile_id": "openai.compatible-images-generations:generic",
                }
            },
        )


@pytest.mark.asyncio
async def test_first_frame_mode_promotes_the_first_image_reference(monkeypatch) -> None:
    service = UniversalAdapterService()
    captured: dict[str, Any] = {}

    async def fake_submit_media(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "status": "queued"}

    monkeypatch.setattr(service, "_submit_media", fake_submit_media)
    provider, provider_params = _provider(
        kind="video",
        protocol_id="dramaagent.updream-video-task",
        operation="video.generate",
        model="sed2",
    )
    try:
        result = await service.submit_video(
            provider=provider,
            provider_params=provider_params,
            project_id="project-video",
            prompt="A goddess walks toward the camera",
            first_frame_url=None,
            last_frame_url=None,
            duration_seconds=10,
            reference_images=["node:0"],
            extra={"video_mode": "first_frame"},
            save_locally=False,
            wait_for_completion=False,
        )
    finally:
        await service.aclose()

    assert result["ok"] is True
    assert captured["input_values"]["mode"] == "first_frame"
    assert captured["media_values"] == [("first_frame", "node:0")]


def test_video_binding_requires_explicit_target_profile() -> None:
    provider, provider_params = _provider(
        kind="video",
        protocol_id="volcengine.seedance-video-task",
        operation="video.generate",
        model="doubao-seedance-2-0-260128",
    )

    with pytest.raises(ValueError, match="explicit target_profile_id"):
        create_universal_adapter_binding(provider, provider_params)


@pytest.mark.asyncio
async def test_every_video_target_compiles_against_its_uma_protocol() -> None:
    for target in load_video_target_catalog()["targets"]:
        model_name = target["match"] if target["match"] != "*" else "generic-video-model"
        provider = SimpleNamespace(
            kind="video",
            name=f"compile-{target['id']}",
            base_url="https://provider.example.invalid/v1",
            api_key="secret-test-key",
            model_name=model_name,
            api_format="universal_adapter",
        )
        bases = {
            item["slot"]: "https://uploads.example.invalid/v1"
            for item in target.get("additional_bases") or []
        }
        params = {
            "uma": {
                "protocol_id": target["protocol_id"],
                "operation": "video.generate",
                "target_profile_id": target["id"],
                **({"bases": bases} if bases else {}),
            }
        }
        binding = create_universal_adapter_binding(provider, params)
        try:
            assert binding.options.target_profile_id == target["id"]
            assert binding.options.protocol_id == target["protocol_id"]
        finally:
            await binding.client.aclose()


@pytest.mark.asyncio
async def test_every_image_target_compiles_against_its_uma_protocol() -> None:
    for target in load_image_target_catalog()["targets"]:
        model_name = target["match"] if target["match"] != "*" else "generic-image-model"
        provider = SimpleNamespace(
            kind="image",
            name=f"compile-{target['id']}",
            base_url="https://provider.example.invalid/v1",
            api_key="secret-test-key",
            model_name=model_name,
            api_format="universal_adapter",
        )
        params = {
            "uma": {
                "protocol_id": target["protocol_id"],
                "operation": target["operation"],
                "target_profile_id": target["id"],
            }
        }
        binding = create_universal_adapter_binding(provider, params)
        try:
            assert binding.options.target_profile_id == target["id"]
            assert binding.options.protocol_id == target["protocol_id"]
        finally:
            await binding.client.aclose()


@pytest.mark.asyncio
async def test_every_audio_target_compiles_against_its_uma_protocol() -> None:
    for target in load_audio_target_catalog()["targets"]:
        model_name = target["match"] if target["match"] != "*" else "generic-audio-model"
        provider = SimpleNamespace(
            kind="audio",
            name=f"compile-{target['id']}",
            base_url="https://provider.example.invalid/v1",
            api_key="secret-test-key",
            model_name=model_name,
            api_format="universal_adapter",
        )
        params = {
            "uma": {
                "protocol_id": target["protocol_id"],
                "operation": target["operation"],
                "target_profile_id": target["id"],
            }
        }
        binding = create_universal_adapter_binding(provider, params)
        try:
            assert binding.options.target_profile_id == target["id"]
            assert binding.options.protocol_id == target["protocol_id"]
            assert binding.operation == target["operation"]
        finally:
            await binding.client.aclose()


def test_audio_binding_rejects_operation_that_conflicts_with_target() -> None:
    provider, provider_params = _provider(
        kind="audio",
        protocol_id="newapi.suno-music-task",
        operation="audio.speech",
        model="V5",
        uma={"target_profile_id": "newapi.suno-music-task:generic"},
    )

    with pytest.raises(ValueError, match="operation must match"):
        create_universal_adapter_binding(provider, provider_params)


@pytest.mark.asyncio
async def test_image_provider_calls_uma_image_backend() -> None:
    image_bytes = b"openreel-image"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/images/generations"
        assert request.headers["authorization"] == "Bearer secret-test-key"
        body = json.loads(request.content)
        assert body == {
            "model": "image-v1",
            "prompt": "A paper city",
            "n": 2,
            "quality": "high",
            "response_format": "url",
            "size": "2048x1152",
        }
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]},
            request=request,
        )

    provider, provider_params = _provider(
        kind="image",
        protocol_id="openai.compatible-images-generations",
        operation="image.generate",
        model="image-v1",
        uma={"target_profile_id": "openai.compatible-images-generations:generic"},
    )
    service, _ = _service_with_binding(provider, provider_params, handler)
    try:
        result = await service.generate_image(
            provider=provider,
            provider_params=provider_params,
            project_id="project-image",
            prompt="A paper city",
            negative_prompt=None,
            size="2048x1152",
            quality="high",
            count=2,
            reference_images=None,
            extra=None,
        )
    finally:
        await service.aclose()

    assert result["ok"] is True, result
    assert base64.b64decode(result["images"][0]["b64"]) == image_bytes
    assert result["adapter_route"]["protocol_id"] == "openai.compatible-images-generations"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_count", [1, 2])
async def test_image_provider_encodes_reference_images_through_uma(
    reference_count: int,
) -> None:
    reference_bytes = [f"reference-{index}".encode() for index in range(reference_count)]
    reference_images = [
        f"data:image/png;base64,{base64.b64encode(value).decode()}"
        for value in reference_bytes
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        expected = reference_images[0] if reference_count == 1 else reference_images
        assert body["image"] == expected
        return httpx.Response(
            200,
            json={"data": [{"url": "https://assets.example.invalid/generated.png"}]},
            request=request,
        )

    provider, provider_params = _provider(
        kind="image",
        protocol_id="openai.compatible-images-generations",
        operation="image.generate",
        model="image-v1",
        uma={"target_profile_id": "openai.compatible-images-generations:generic"},
    )
    service, _ = _service_with_binding(provider, provider_params, handler)
    try:
        result = await service.generate_image(
            provider=provider,
            provider_params=provider_params,
            project_id="project-image",
            prompt="Preserve the subject",
            negative_prompt=None,
            size="1024x1024",
            quality=None,
            count=1,
            reference_images=reference_images,
            extra=None,
        )
    finally:
        await service.aclose()

    assert result["ok"] is True, result
    assert result["images"][0]["url"] == "https://assets.example.invalid/generated.png"


@pytest.mark.asyncio
async def test_video_provider_uses_uma_handle_and_progress_events() -> None:
    polls = 0
    submitted_body: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path == "/videos/generations":
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "provider-task-7"}, request=request)
        assert request.url.path == "/videos/tasks/provider-task-7"
        polls += 1
        if polls == 1:
            return httpx.Response(
                200,
                json={"provider_response": {"status": "processing"}},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "provider_response": {"status": "succeeded"},
                "video_url": "https://assets.example.invalid/video.mp4",
            },
            request=request,
        )

    provider, provider_params = _provider(
        kind="video",
        protocol_id="dramaagent.updream-video-task",
        operation="video.generate",
        model="sed2",
        uma={"target_profile_id": "dramaagent.updream-video-task:sed2"},
    )
    service, _ = _service_with_binding(provider, provider_params, handler)
    progress: list[dict[str, Any]] = []
    try:
        queued = await service.submit_video(
            provider=provider,
            provider_params=provider_params,
            project_id="project-video",
            prompt="A train crossing a valley",
            first_frame_url=None,
            last_frame_url=None,
            duration_seconds=10,
            reference_images=None,
            extra={"aspect_ratio": "16:9"},
            save_locally=False,
            wait_for_completion=False,
        )
        result = await service.poll(
            provider=provider,
            job_id=queued["job_id"],
            kind="video",
            progress_callback=progress.append,
        )
    finally:
        await service.aclose()

    assert queued["ok"] is True, (queued, submitted_body, polls, result)
    assert submitted_body == {
        "provider": "updream",
        "model": "sed2",
        "prompt": "A train crossing a valley",
        "provider_payload": {
            "prompt": "A train crossing a valley",
            "model_name": "sed2",
            "generate_type": "t2v",
            "duration": 10,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "supports_audio": True,
            "sound": True,
        },
    }
    assert queued["job_id"] != "provider-task-7"
    assert result["ok"] is True, result
    assert result["remote_url"] == "https://assets.example.invalid/video.mp4"
    assert result["adapter_route"]["target_id"].endswith("/target")
    assert polls == 2
    assert any(update.get("status") == "running" for update in progress)


@pytest.mark.asyncio
async def test_video_provider_preserves_explicit_generate_audio_false() -> None:
    submitted_body: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/videos/generations":
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "provider-task-silent"}, request=request)
        return httpx.Response(
            200,
            json={
                "provider_response": {"status": "succeeded"},
                "video_url": "https://assets.example.invalid/silent.mp4",
            },
            request=request,
        )

    provider, provider_params = _provider(
        kind="video",
        protocol_id="dramaagent.updream-video-task",
        operation="video.generate",
        model="sed2",
        uma={"target_profile_id": "dramaagent.updream-video-task:sed2"},
    )
    service, _ = _service_with_binding(provider, provider_params, handler)
    try:
        queued = await service.submit_video(
            provider=provider,
            provider_params=provider_params,
            project_id="project-video",
            prompt="A deliberately silent shot",
            first_frame_url=None,
            last_frame_url=None,
            duration_seconds=5,
            reference_images=None,
            extra={"generate_audio": False},
            save_locally=False,
            wait_for_completion=False,
        )
    finally:
        await service.aclose()

    assert queued["ok"] is True
    assert submitted_body["provider_payload"]["supports_audio"] is False
    assert submitted_body["provider_payload"]["sound"] is False


@pytest.mark.asyncio
async def test_video_poll_resumes_from_persisted_provider_task_after_restart() -> None:
    poll_started = asyncio.Event()

    async def first_process_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/videos/generations":
            return httpx.Response(200, json={"id": "provider-task-resume"}, request=request)
        poll_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    provider, provider_params = _provider(
        kind="video",
        protocol_id="dramaagent.updream-video-task",
        operation="video.generate",
        model="sed2",
        uma={"target_profile_id": "dramaagent.updream-video-task:sed2"},
    )
    first_service, _ = _service_with_binding(provider, provider_params, first_process_handler)
    queued = await first_service.submit_video(
        provider=provider,
        provider_params=provider_params,
        project_id="project-video",
        prompt="Resume this render",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=8,
        reference_images=None,
        extra=None,
        save_locally=False,
        wait_for_completion=False,
    )
    await poll_started.wait()
    await first_service.aclose()

    requests: list[tuple[str, str]] = []

    async def restarted_process_handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.url.path == "/videos/tasks/provider-task-resume"
        return httpx.Response(
            200,
            json={
                "provider_response": {"status": "succeeded"},
                "video_url": "https://assets.example.invalid/resumed.mp4",
            },
            request=request,
        )

    restarted_service, _ = _service_with_binding(
        provider, provider_params, restarted_process_handler
    )
    try:
        result = await restarted_service.poll(
            provider=provider,
            provider_params=provider_params,
            project_id="project-video",
            job_id=queued["job_id"],
            kind="video",
            save_locally=False,
            provider_task_id=queued["provider_task_id"],
            resume_request=queued["adapter_resume_request"],
        )
    finally:
        await restarted_service.aclose()

    assert queued["adapter_resume_supported"] is True
    assert result["ok"] is True, result
    assert result["remote_url"] == "https://assets.example.invalid/resumed.mp4"
    assert requests == [("GET", "/videos/tasks/provider-task-resume")]


@pytest.mark.asyncio
async def test_audio_provider_materializes_binary_output_in_openreel_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_bytes = b"openreel-audio"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/audio/speech"
        body = json.loads(request.content)
        assert body == {
            "model": "speech-v1",
            "input": "Welcome",
            "voice": "alloy",
            "response_format": "mp3",
        }
        return httpx.Response(
            200,
            content=audio_bytes,
            headers={"content-type": "audio/mpeg"},
            request=request,
        )

    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path))
    provider, provider_params = _provider(
        kind="audio",
        protocol_id="openai.media",
        operation="audio.speech",
        model="speech-v1",
        uma={"target_profile_id": "openai.media:generic-speech"},
    )
    service, _ = _service_with_binding(provider, provider_params, handler)
    try:
        result = await service.submit_audio(
            provider=provider,
            provider_params=provider_params,
            project_id="project-audio",
            prompt="Welcome",
            title=None,
            style=None,
            instrumental=None,
            extra={"voice": "alloy", "format": "mp3"},
            save_locally=True,
            wait_for_completion=True,
        )
    finally:
        await service.aclose()

    assert result["ok"] is True
    output_path = result["local_path"]
    assert output_path
    assert Path(output_path).read_bytes() == audio_bytes
    assert result["local_url"].startswith("/api/media/project-audio/generated_audio/")


@pytest.mark.asyncio
async def test_suno_audio_provider_uses_uma_task_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = 0
    submitted_body: dict[str, Any] = {}
    audio_bytes = {
        "/song-a.mp3": b"openreel-suno-audio-a",
        "/song-b.mp3": b"openreel-suno-audio-b",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path == "/submit/music":
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"code": "success", "data": "song-task-1"}, request=request)
        assert request.url.path == "/fetch/song-task-1"
        polls += 1
        if polls == 1:
            return httpx.Response(
                200,
                json={"code": "success", "data": {"status": "IN_PROGRESS", "progress": "30%"}},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "code": "success",
                "data": {
                    "status": "SUCCESS",
                    "progress": "100%",
                    "data": [
                        {
                            "audio_url": "https://assets.example.invalid/song-a.mp3",
                            "duration": 42.5,
                        },
                        {
                            "audio_url": "https://assets.example.invalid/song-b.mp3",
                            "duration": 41.0,
                        },
                    ],
                },
            },
            request=request,
        )

    async def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=audio_bytes[request.url.path],
            headers={"content-type": "audio/mpeg"},
            request=request,
        )

    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path))
    provider, provider_params = _provider(
        kind="audio",
        protocol_id="newapi.suno-music-task",
        operation="audio.generate",
        model="V5",
        uma={"target_profile_id": "newapi.suno-music-task:generic"},
    )
    service, _ = _service_with_binding(
        provider,
        provider_params,
        handler,
        download_handler=download_handler,
    )
    try:
        result = await service.submit_audio(
            provider=provider,
            provider_params=provider_params,
            project_id="project-audio",
            prompt="A warm cinematic pop theme",
            title="Theme",
            style="cinematic pop",
            instrumental=True,
            extra={"mv": "chirp-v4"},
            save_locally=True,
            wait_for_completion=True,
        )
    finally:
        await service.aclose()

    assert result["ok"] is True, result
    assert submitted_body == {
        "gpt_description_prompt": "A warm cinematic pop theme",
        "tags": "cinematic pop",
        "title": "Theme",
        "make_instrumental": True,
        "mv": "chirp-v4",
    }
    assert [item["remote_url"] for item in result["audios"]] == [
        "https://assets.example.invalid/song-a.mp3",
        "https://assets.example.invalid/song-b.mp3",
    ]
    assert [Path(item["local_path"]).read_bytes() for item in result["audios"]] == [
        audio_bytes["/song-a.mp3"],
        audio_bytes["/song-b.mp3"],
    ]
    assert len({item["local_url"] for item in result["audios"]}) == 2
    assert result["duration"] == 42.5
    assert polls == 2


@pytest.mark.asyncio
async def test_media_is_rejected_when_target_does_not_declare_roles() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    provider, provider_params = _provider(
        kind="image",
        protocol_id="openai.compatible-images-generations",
        operation="image.generate",
        model="dall-e-3",
        uma={"target_profile_id": "openai.compatible-images-generations:dall-e-3"},
    )
    service, _ = _service_with_binding(provider, provider_params, handler)
    try:
        result = await service.generate_image(
            provider=provider,
            provider_params=provider_params,
            project_id="project-image",
            prompt="A paper city",
            negative_prompt=None,
            size="1024x1024",
            quality=None,
            count=1,
            reference_images=["https://assets.example.invalid/reference.png"],
            extra=None,
        )
    finally:
        await service.aclose()

    assert result["ok"] is False
    assert result["error_kind"] == "adapter_configuration_error"
    assert "accepted_media_roles" in result["error"]
    assert calls == 0


@pytest.mark.asyncio
async def test_media_generation_service_delegates_universal_adapter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, provider_params = _provider(
        kind="image",
        protocol_id="openai.compatible-images-generations",
        operation="image.generate",
        model="image-v1",
        uma={"target_profile_id": "openai.compatible-images-generations:generic"},
    )
    provider.params_json = json.dumps(provider_params)
    captured: dict[str, Any] = {}

    async def lookup(_: str, __: str) -> SimpleNamespace:
        return provider

    async def generate(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "images": [{"url": "https://assets.example.invalid/image.png"}],
            "provider": provider.name,
            "model": provider.model_name,
        }

    monkeypatch.setattr(media_provider, "_get_provider_by_name", lookup)
    monkeypatch.setattr(
        "app.services.universal_adapter_service.universal_adapter_service.generate_image",
        generate,
    )

    result = await media_provider.generate_image_with_provider(
        project_id="project-image",
        prompt="A paper city",
        size="1024x1024",
        model_name=provider.name,
        save_locally=False,
    )

    assert result["ok"] is True
    assert result["images"][0]["url"] == "https://assets.example.invalid/image.png"
    assert captured["provider_params"] == provider_params
    assert captured["count"] == 1


@pytest.mark.asyncio
async def test_audio_generation_forwards_node_duration_and_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def generate(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "url": "https://assets.example.invalid/audio.wav",
            "provider": "audio-uma",
            "model": "audio-v1",
        }

    monkeypatch.setattr(media_generation, "generate_audio_with_provider", generate)

    result = await media_generation.generate_audio(
        project_id="project-audio",
        prompt="Rising strings",
        duration_seconds=12,
        audio_format="wav",
        extra={"voice": "alloy"},
    )

    assert result["ok"] is True
    assert captured["extra"] == {
        "voice": "alloy",
        "duration_seconds": 12,
        "audio_format": "wav",
    }


def test_restart_recovery_requires_persisted_uma_resume_data() -> None:
    node = SimpleNamespace(type="video")
    output = {
        "ok": True,
        "status": "running",
        "job_id": "uma-invocation",
        "adapter_resume_supported": False,
    }

    assert node_recovery._resumable_uma_media_output(node, output) is False

    audio_node = SimpleNamespace(type="audio")
    resumable_audio = {
        "ok": True,
        "status": "running",
        "job_id": "uma-audio-invocation",
        "provider_task_id": "provider-audio-task",
        "adapter_resume_request": {"operation": "audio.generate"},
        "adapter_resume_supported": True,
    }
    assert node_recovery._resumable_uma_media_output(audio_node, resumable_audio) is True
