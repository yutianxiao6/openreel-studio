from types import SimpleNamespace

import pytest

from app.services import media_provider, node_contract
from app.services.video_protocol_modes import derive_video_profile_modes


def _seedance_profile(model: str, image_limit: int) -> dict:
    return {
        "match": model,
        "generate_types": ["t2v", "i2v", "ref2v", "video_edit"],
        "supports_image_num": image_limit,
        "supports_firstlast": True,
        "max_ref_videos": 3,
        "supports_audio_url": True,
    }


def test_seedance_profile_keeps_first_frame_single_and_exposes_full_reference() -> None:
    modes = derive_video_profile_modes(_seedance_profile("sed2", 9))

    assert modes["first_frame"]["max_images"] == 1
    assert modes["first_last_frame"]["max_images"] == 2
    assert modes["multimodal_reference"]["request_mode"] == "ref2v"
    assert modes["multimodal_reference"]["max_images"] == 9
    assert modes["multimodal_reference"]["max_videos"] == 3


def test_seedance_variants_keep_their_provider_reference_limits() -> None:
    assert derive_video_profile_modes(_seedance_profile("sed2-fast", 3))["multimodal_reference"]["max_images"] == 3
    assert derive_video_profile_modes(_seedance_profile("sed2-mini", 12))["multimodal_reference"]["max_images"] == 12


def test_provider_runtime_and_node_contract_share_derived_modes() -> None:
    profile = _seedance_profile("sed2", 9)
    protocol = {
        "modes": {
            "text_to_video": {"max_images": 0},
            "first_frame": {"min_images": 1, "max_images": 1},
        }
    }

    provider_mode = media_provider._video_http_v1_mode_config(
        protocol,
        "multimodal_reference",
        profile,
    )
    contract_modes = node_contract._video_modes(protocol, profile)

    assert provider_mode["request_mode"] == "ref2v"
    assert provider_mode["max_images"] == 9
    assert contract_modes["multimodal_reference"]["max_images"] == 9
    assert set(contract_modes) >= {
        "text_to_video",
        "first_frame",
        "first_last_frame",
        "multimodal_reference",
    }


@pytest.mark.asyncio
async def test_seedance_full_reference_renders_provider_ref2v_mode(monkeypatch) -> None:
    profile = _seedance_profile("sed2", 9)
    protocol = {
        "model_profiles": [profile],
        "modes": {
            "text_to_video": {"max_images": 0},
            "first_frame": {"min_images": 1, "max_images": 1},
        },
        "content": {
            "text": {"type": "text", "type_key": "type", "text_key": "text"},
            "media_types": {
                "image": {
                    "type": "image_url",
                    "object_key": "image_url",
                    "url_key": "url",
                    "role_key": "role",
                }
            },
        },
        "request": {
            "body": {
                "model": "$model",
                "generate_type": "$generate_type",
                "ref_images": "$image_urls",
            }
        },
    }
    provider = SimpleNamespace(model_name="sed2", params_json="{}")
    refs = [f"https://example.test/ref-{index}.png" for index in range(9)]

    monkeypatch.setattr(media_provider, "_video_http_v1_protocol", lambda *_args: (protocol, None))

    async def resolve_refs(*_args):
        raw_refs = _args[-1]
        return [
            {"kind": "image", "role": "reference_image", "ref": item["ref"], "url": item["ref"]}
            for item in raw_refs
        ], []

    monkeypatch.setattr(media_provider, "_video_http_v1_resolve_media_refs", resolve_refs)

    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=provider,
        project_id="project-1",
        prompt="保持人物与场景一致",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=5,
        reference_images=refs,
        extra_override={"video_mode": "multimodal_reference"},
    )

    assert meta["mode"] == "multimodal_reference"
    assert payload == {
        "model": "sed2",
        "generate_type": "ref2v",
        "ref_images": refs,
    }
