from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import routes_tools
from app.mcp_tools import config_tools
from app.services import media_provider, node_contract


def _image_config() -> dict:
    return {
        "media_providers": [
            {
                "kind": "image",
                "name": "image-active",
                "model_name": "image-v2",
                "api_format": "image_http_v1",
                "enabled": True,
                "is_active": True,
                "params": {"image_protocol_id": "image-contract"},
            }
        ]
    }


def _video_config() -> dict:
    return {
        "media_providers": [
            {
                "kind": "video",
                "name": "video-active",
                "model_name": "video-v2",
                "api_format": "universal_adapter",
                "enabled": True,
                "is_active": True,
                "params": {
                    "uma": {
                        "protocol_id": "test.video-task",
                        "operation": "video.generate",
                        "target_profile_id": "test.video-task:video-v2",
                    }
                },
            }
        ]
    }


def _video_catalog() -> dict:
    return {
        "ok": True,
        "protocols": [
            {
                "id": "test.video-task",
                "targets": [
                    {
                        "id": "test.video-task:video-v2",
                        "model_match": "video-v2",
                        "capabilities": {
                            "supports_native_audio": True,
                            "default_generate_audio": True,
                            "supported_ratios": ["16:9"],
                            "supported_resolutions": ["720p"],
                            "default_ratio": "16:9",
                            "default_resolution": "720p",
                            "duration": {"allowed_values": [5, 10]},
                            "modes": {
                                "text_to_video": {"max_images": 0},
                                "first_frame": {"min_images": 1, "max_images": 1},
                                "multimodal_reference": {
                                    "min_total_media": 1,
                                    "max_images": 2,
                                },
                            },
                        },
                    }
                ],
            }
        ],
    }


def test_image_contract_requires_dynamic_exact_pixels_without_hardcoded_default() -> None:
    result = node_contract.build_node_contract(
        node_type="image",
        fields={"prompt": "cinematic product photo"},
        config=_image_config(),
        project_state={},
        protocol_catalog={"ok": True, "protocols": [{"id": "image-contract"}]},
    )

    assert result["ok"] is True
    assert result["ready"] is False
    assert "resolution" not in result["normalized_fields"]
    assert "aspect_ratio" not in result["normalized_fields"]
    assert {(item["field"], item["code"]) for item in result["errors"]} == {
        ("aspect_ratio", "missing_required_field"),
        ("resolution", "missing_required_field"),
    }
    assert result["normalized_fields"]["model"] == "image-active"


def test_image_contract_uses_project_frontend_output_settings() -> None:
    result = node_contract.build_node_contract(
        node_type="image",
        fields={"prompt": "cinematic product photo"},
        config=_image_config(),
        project_state={
            "output_settings": {
                "image": {"aspect_ratio": "16:9", "resolution": "2560x1440"},
            }
        },
        protocol_catalog={"ok": True, "protocols": [{"id": "image-contract"}]},
    )

    assert result["ready"] is True
    assert result["normalized_fields"]["aspect_ratio"] == "16:9"
    assert result["normalized_fields"]["resolution"] == "2560x1440"
    assert result["field_sources"]["resolution"] == "project_state.output_settings"


def test_video_contract_resolves_provider_protocol_and_reference_mode() -> None:
    result = node_contract.build_node_contract(
        node_type="video",
        fields={
            "prompt": "slow camera push-in",
            "reference_images": ["node:image-1"],
            "duration_seconds": 10,
        },
        config=_video_config(),
        project_state={},
        protocol_catalog=_video_catalog(),
    )

    assert result["ready"] is True
    assert result["provider"] == {
        "name": "video-active",
        "model_name": "video-v2",
        "api_format": "universal_adapter",
        "protocol_id": "test.video-task",
        "selection": "active_or_first_enabled",
    }
    assert result["effective_video_mode"] == "multimodal_reference"
    assert result["normalized_fields"] == {
        "prompt": "slow camera push-in",
        "reference_images": ["node:image-1"],
        "duration_seconds": 10,
        "model": "video-active",
        "video_mode": "multimodal_reference",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "generate_audio": True,
    }
    assert result["field_schema"]["properties"]["generate_audio"]["type"] == "boolean"
    assert result["field_sources"]["generate_audio"] == "provider_protocol.default_generate_audio"
    assert result["capabilities"]["supports_native_audio"] is True
    assert result["capabilities"]["default_generate_audio"] is True
    assert result["capabilities"]["supported_modes"] == [
        "text_to_video",
        "first_frame",
        "multimodal_reference",
    ]


def test_video_contract_preserves_explicit_silent_video_choice() -> None:
    result = node_contract.build_node_contract(
        node_type="video",
        fields={
            "prompt": "silent tracking shot",
            "duration_seconds": 10,
            "generate_audio": False,
        },
        config=_video_config(),
        project_state={},
        protocol_catalog=_video_catalog(),
    )

    assert result["ready"] is True
    assert result["normalized_fields"]["generate_audio"] is False
    assert result["field_sources"]["generate_audio"] == "request.fields"


def test_video_contract_deduplicates_the_same_reference_across_alias_fields() -> None:
    result = node_contract.build_node_contract(
        node_type="video",
        fields={
            "prompt": "slow camera push-in",
            "video_mode": "first_frame",
            "references": ["0"],
            "reference_images": ["node:0"],
            "duration_seconds": 10,
        },
        config=_video_config(),
        project_state={},
        protocol_catalog=_video_catalog(),
    )

    assert result["ready"] is True
    assert result["effective_video_mode"] == "first_frame"
    assert result["reference_counts"] == {
        "images": 1,
        "videos": 0,
        "audios": 0,
        "total": 1,
    }


def test_video_contract_returns_field_level_errors_before_creation() -> None:
    result = node_contract.build_node_contract(
        node_type="video",
        fields={
            "prompt": "tracking shot",
            "video_mode": "text_to_video",
            "reference_images": ["one", "two"],
            "duration_seconds": 8,
            "resolution": "4k",
        },
        config=_video_config(),
        project_state={},
        protocol_catalog=_video_catalog(),
    )

    assert result["ready"] is False
    errors = {(item["field"], item["code"]) for item in result["errors"]}
    assert ("duration_seconds", "unsupported_value") in errors
    assert ("resolution", "unsupported_value") in errors
    assert ("references", "too_many_references") in errors
    assert result["repair"]["retry_same_node"] is True


class _FakeDb:
    def __init__(self, project):
        self.project = project

    async def get(self, _model, _project_id):
        return self.project


@pytest.mark.asyncio
async def test_node_contract_route_uses_masked_runtime_config(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def fake_read(*, mask_secrets: bool):
        calls["mask_secrets"] = mask_secrets
        return _image_config()

    monkeypatch.setattr(config_tools, "config_read", fake_read)
    monkeypatch.setattr(
        media_provider,
        "list_image_http_v1_protocol_catalog",
        lambda: {"ok": True, "protocols": [{"id": "image-contract"}]},
    )
    project = SimpleNamespace(
        state_json='{"output_settings":{"image":{"aspect_ratio":"9:16","resolution":"1440x2560"}}}'
    )

    result = await routes_tools.describe_node_contract(
        routes_tools.NodeContractRequest(
            project_id="project-1",
            type="image",
            fields={"prompt": "portrait"},
        ),
        db=_FakeDb(project),
    )

    assert calls["mask_secrets"] is True
    assert result["ready"] is True
    assert result["normalized_fields"]["resolution"] == "1440x2560"


@pytest.mark.asyncio
async def test_node_contract_route_rejects_unknown_project() -> None:
    with pytest.raises(HTTPException) as exc:
        await routes_tools.describe_node_contract(
            routes_tools.NodeContractRequest(project_id="missing", type="image", fields={}),
            db=_FakeDb(None),
        )

    assert exc.value.status_code == 404
