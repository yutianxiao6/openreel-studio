import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from universal_model_adapter import ProtocolCatalog

from app.config import settings
from app.config_store.schema import MediaProviderEntry
from app.services import media_provider
from app.services.video_target_catalog import (
    compile_video_target_options,
    list_video_model_targets,
    load_video_target_catalog,
)


def _protocol(loader, protocol_id: str) -> dict:
    protocol, error = loader(protocol_id)
    assert error is None
    assert protocol is not None
    return protocol


@pytest.mark.parametrize(
    ("loader", "protocol_ids"),
    [
        (media_provider._image_http_v1_protocol_from_catalog, ["openai_images_generations"]),
        (
            media_provider._audio_http_v1_protocol_from_catalog,
            ["openai_audio_speech", "newapi_suno_music", "suno_compatible_generate"],
        ),
    ],
)
def test_host_catalogs_are_limited_to_image_and_audio(loader, protocol_ids) -> None:
    for protocol_id in protocol_ids:
        protocol = _protocol(loader, protocol_id)
        assert protocol["id"] == protocol_id


def test_host_endpoint_builder_treats_provider_base_url_as_literal() -> None:
    provider = SimpleNamespace(base_url="https://relay.example/api/v3", params_json="{}")
    endpoint = media_provider._protocol_endpoint_for(
        provider,
        {"default_base_url": "https://ignored.example/v1"},
        {"path": "/jobs"},
    )
    assert endpoint == "https://relay.example/api/v3/jobs"


@pytest.mark.parametrize(
    ("loader", "protocol_id", "base_url", "section_name", "task_id", "expected"),
    [
        (
            media_provider._image_http_v1_protocol_from_catalog,
            "openai_images_generations",
            "https://provider.example/v1",
            "request",
            None,
            "https://provider.example/v1/images/generations",
        ),
        (
            media_provider._audio_http_v1_protocol_from_catalog,
            "openai_audio_speech",
            "https://provider.example/v1",
            "request",
            None,
            "https://provider.example/v1/audio/speech",
        ),
        (
            media_provider._audio_http_v1_protocol_from_catalog,
            "suno_compatible_generate",
            "https://provider.example/v1",
            "poll",
            "task-1",
            "https://provider.example/v1/generate/record-info?taskId=task-1",
        ),
    ],
)
def test_remaining_host_protocol_endpoints(
    loader,
    protocol_id: str,
    base_url: str,
    section_name: str,
    task_id: str | None,
    expected: str,
) -> None:
    protocol = _protocol(loader, protocol_id)
    provider = SimpleNamespace(base_url=base_url, params_json="{}")
    endpoint = media_provider._protocol_endpoint_for(
        provider,
        protocol,
        protocol[section_name],
        task_id=task_id,
    )
    assert endpoint == expected


def test_every_openreel_video_protocol_loads_through_uma_v2() -> None:
    protocol_dir = Path(settings.PROJECT_ROOT) / "config" / "universal_model_adapter" / "protocols"
    catalog = ProtocolCatalog.load([protocol_dir])
    assert {item.document.id for item in catalog.list()} == {
        "volcengine.seedance-video-task",
        "lingke.media-video-task",
        "t8.grok-video-task",
        "xai.video-task",
        "grok.multipart-video-task",
        "dramaagent.updream-video-task",
    }
    assert all(item.document.format == "uma.protocol/v2" for item in catalog.list())
    assert all(item.document.version == "2.0.0" for item in catalog.list())


def test_video_targets_keep_capabilities_separate_from_wire_protocols() -> None:
    catalog = load_video_target_catalog()
    targets = catalog["targets"]
    assert len(targets) == 25
    assert {target["match"] for target in targets} >= {
        "doubao-seedance-2-0-260128",
        "grok-video-3",
        "sed2",
        "vidu-q2-pro",
        "wan-2.7",
    }
    for target in targets:
        serialized = json.dumps(target, ensure_ascii=False)
        assert '"request"' not in serialized
        assert '"response"' not in serialized
        assert '"request_mode"' not in serialized
        assert '"status_path"' not in serialized
        assert '"result_url_paths"' not in serialized

    public = list_video_model_targets()
    dramaagent = next(
        item for item in public["protocols"] if item["id"] == "dramaagent.updream-video-task"
    )
    profiles = {item["match"]: item for item in dramaagent["model_profiles"]}
    assert profiles["sed2"]["modes"]["first_frame"]["max_images"] == 1
    assert profiles["sed2"]["modes"]["multimodal_reference"]["max_images"] == 9
    assert profiles["sed2"]["supports_native_audio"] is True
    assert profiles["sed2"]["default_generate_audio"] is True
    assert profiles["wan-2.7"]["modes"]["video_continuation"]["min_videos"] == 1

    sed2_target = next(target for target in targets if target["match"] == "sed2")
    sed2_options = compile_video_target_options(sed2_target)
    assert sed2_options["target_defaults"]["parameters"]["generate_audio"] is True
    assert sed2_options["request_schema"]["properties"]["parameters"]["properties"][
        "generate_audio"
    ] == {"type": "boolean"}

    protocol_dir = Path(settings.PROJECT_ROOT) / "config" / "universal_model_adapter" / "protocols"
    protocol = ProtocolCatalog.load([protocol_dir]).get("dramaagent.updream-video-task")
    continuation = protocol.document.entrypoints["video.generate"].variants["video_continuation"]
    assert continuation.bind["generate_type"] == "clip2v"


def test_video_provider_schema_requires_universal_adapter() -> None:
    entry = MediaProviderEntry(
        kind="video",
        name="video-uma",
        base_url="https://provider.example/v1",
        api_key="secret",
        model_name="doubao-seedance-2-0-260128",
        api_format="universal_adapter",
        params={
            "uma": {
                "protocol_id": "volcengine.seedance-video-task",
                "operation": "video.generate",
                "target_profile_id": ("volcengine.seedance-video-task:doubao-seedance-2-0-260128"),
            }
        },
    )
    assert entry.api_format == "universal_adapter"

    with pytest.raises(ValidationError, match="universal_adapter"):
        MediaProviderEntry(
            kind="video",
            name="unsupported-video",
            base_url="https://provider.example/v1",
            api_key="secret",
            model_name="doubao-seedance-2-0-260128",
            api_format="raw",
            params={},
        )

    with pytest.raises(ValidationError, match="target_profile_id"):
        MediaProviderEntry(
            kind="video",
            name="video-without-target",
            base_url="https://provider.example/v1",
            api_key="secret",
            model_name="doubao-seedance-2-0-260128",
            api_format="universal_adapter",
            params={
                "uma": {
                    "protocol_id": "volcengine.seedance-video-task",
                    "operation": "video.generate",
                }
            },
        )
