import json
from types import SimpleNamespace

import pytest

from app.config_store.schema import MediaProviderEntry
from app.services import media_provider


CATALOG_PROTOCOLS = [
    (media_provider._image_http_v1_protocol_from_catalog, [
        "openai_images_generations",
    ]),
    (media_provider._video_http_v1_protocol_from_catalog, [
        "seedance_2_0",
        "lingke_media_generate_json_task",
        "t8_grok_video_3_json_task",
        "xai_grok_imagine_video",
        "xai_grok_imagine_video_1_5",
        "grok_1_5_multipart",
    ]),
    (media_provider._audio_http_v1_protocol_from_catalog, [
        "openai_audio_speech",
        "newapi_suno_music",
        "suno_compatible_generate",
    ]),
]


def _protocol(loader, protocol_id: str) -> dict:
    protocol, error = loader(protocol_id)
    assert error is None
    assert protocol is not None
    return protocol


@pytest.mark.parametrize(("loader", "protocol_ids"), CATALOG_PROTOCOLS)
def test_media_protocol_catalogs_do_not_rewrite_provider_base_urls(loader, protocol_ids) -> None:
    for protocol_id in protocol_ids:
        protocol = _protocol(loader, protocol_id)
        assert "strip_base_suffixes" not in protocol
        for section_name in ("request", "poll", "upload"):
            section = protocol.get(section_name)
            if isinstance(section, dict):
                assert "strip_base_suffixes" not in section


def test_media_endpoint_builder_treats_provider_base_url_as_literal() -> None:
    provider = SimpleNamespace(base_url="https://relay.example/api/v3", params_json="{}")
    protocol = {
        "default_base_url": "https://ignored.example/v1",
        "strip_base_suffixes": ["/api/v3"],
    }

    endpoint = media_provider._video_http_v1_endpoint_for(
        provider,
        protocol,
        {"path": "/jobs"},
    )

    assert endpoint == "https://relay.example/api/v3/jobs"


def test_protocol_paths_only_contain_resources_not_api_versions() -> None:
    for loader, protocol_ids in CATALOG_PROTOCOLS:
        for protocol_id in protocol_ids:
            protocol = _protocol(loader, protocol_id)
            for section_name in ("request", "poll", "upload"):
                section = protocol.get(section_name)
                if not isinstance(section, dict):
                    continue
                path = str(section.get("path") or "")
                assert not path.startswith("/v1/")
                assert not path.startswith("/v2/")
                assert not path.startswith("/api/v")


def test_legacy_endpoint_helpers_use_versioned_api_base_literally() -> None:
    assert media_provider._ark_video_tasks_endpoint("https://ark.example/api/v3") == (
        "https://ark.example/api/v3/contents/generations/tasks"
    )
    assert media_provider._xai_video_generations_endpoint("https://xai.example/v1") == (
        "https://xai.example/v1/videos/generations"
    )
    assert media_provider._grok_1_5_video_endpoint("https://relay.example/v1") == (
        "https://relay.example/v1/videos"
    )
    assert media_provider._lingke_media_generate_endpoint("https://lingke.example/v1") == (
        "https://lingke.example/v1/media/generate"
    )
    assert media_provider._t8_grok_video_3_endpoint("https://t8.example/v2") == (
        "https://t8.example/v2/videos/generations"
    )


def test_t8_protocol_requires_explicit_versioned_upload_api_base() -> None:
    with pytest.raises(ValueError, match="upload_base_url"):
        MediaProviderEntry(
            kind="video",
            name="t8-video",
            base_url="https://ai.t8star.org/v2",
            api_key="test-key",
            model_name="grok-video-3",
            api_format="video_http_v1",
            params={"video_protocol_id": "t8_grok_video_3_json_task"},
        )

    entry = MediaProviderEntry(
        kind="video",
        name="t8-video",
        base_url="https://ai.t8star.org/v2",
        api_key="test-key",
        model_name="grok-video-3",
        api_format="video_http_v1",
        params={
            "video_protocol_id": "t8_grok_video_3_json_task",
            "upload_base_url": "https://ai.t8star.org/v1",
        },
    )

    assert entry.params["upload_base_url"] == "https://ai.t8star.org/v1"


@pytest.mark.parametrize(
    ("loader", "protocol_id", "base_url", "params", "section_name", "task_id", "expected"),
    [
        (
            media_provider._image_http_v1_protocol_from_catalog,
            "openai_images_generations",
            "https://ark.cn-beijing.volces.com/api/v3",
            {},
            "request",
            None,
            "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        ),
        (
            media_provider._video_http_v1_protocol_from_catalog,
            "seedance_2_0",
            "https://ark.cn-beijing.volces.com/api/v3",
            {},
            "request",
            None,
            "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
        ),
        (
            media_provider._video_http_v1_protocol_from_catalog,
            "lingke_media_generate_json_task",
            "https://api.lk888.ai/v1",
            {},
            "request",
            None,
            "https://api.lk888.ai/v1/media/generate",
        ),
        (
            media_provider._video_http_v1_protocol_from_catalog,
            "t8_grok_video_3_json_task",
            "https://ai.t8star.org/v2",
            {"upload_base_url": "https://ai.t8star.org/v1"},
            "upload",
            None,
            "https://ai.t8star.org/v1/files",
        ),
        (
            media_provider._video_http_v1_protocol_from_catalog,
            "t8_grok_video_3_json_task",
            "https://ai.t8star.org/v2",
            {"upload_base_url": "https://ai.t8star.org/v1"},
            "request",
            None,
            "https://ai.t8star.org/v2/videos/generations",
        ),
        (
            media_provider._video_http_v1_protocol_from_catalog,
            "xai_grok_imagine_video",
            "https://api.x.ai/v1",
            {},
            "poll",
            "job-1",
            "https://api.x.ai/v1/videos/job-1",
        ),
        (
            media_provider._video_http_v1_protocol_from_catalog,
            "grok_1_5_multipart",
            "https://ai.t8star.org/v1",
            {},
            "request",
            None,
            "https://ai.t8star.org/v1/videos",
        ),
        (
            media_provider._audio_http_v1_protocol_from_catalog,
            "openai_audio_speech",
            "https://ai.t8star.org/v1",
            {},
            "request",
            None,
            "https://ai.t8star.org/v1/audio/speech",
        ),
        (
            media_provider._audio_http_v1_protocol_from_catalog,
            "newapi_suno_music",
            "https://ai.t8star.org/suno",
            {},
            "request",
            None,
            "https://ai.t8star.org/suno/submit/music",
        ),
        (
            media_provider._audio_http_v1_protocol_from_catalog,
            "suno_compatible_generate",
            "https://suno.example/api/v1",
            {},
            "poll",
            "task-1",
            "https://suno.example/api/v1/generate/record-info?taskId=task-1",
        ),
    ],
)
def test_media_protocol_endpoint_contracts(
    loader,
    protocol_id: str,
    base_url: str,
    params: dict,
    section_name: str,
    task_id: str | None,
    expected: str,
) -> None:
    protocol = _protocol(loader, protocol_id)
    provider = SimpleNamespace(base_url=base_url, params_json=json.dumps(params))
    section = protocol[section_name]

    endpoint = media_provider._video_http_v1_endpoint_for(
        provider,
        protocol,
        section,
        task_id=task_id,
    )

    assert endpoint == expected
