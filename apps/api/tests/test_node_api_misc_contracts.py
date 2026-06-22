import json
from types import SimpleNamespace

import pytest

from app.api import routes_projects
from app.config_store.schema import MediaProviderEntry
from app.db.models import WorkflowNode
from app.mcp_tools import node_universal
from app.services import media_generation
from app.services import media_history
from app.services import media_provider
from app.services.node_service import canvas_edge_payloads


def test_public_node_types_are_generic_only():
    assert node_universal.NODE_TYPES == ("text", "image", "video", "audio")
    assert set(node_universal._RUNNERS) == {"text", "image", "video", "audio"}
    assert set(node_universal._NODE_FIELD_SCHEMA) == {"text", "image", "video", "audio"}


@pytest.mark.asyncio
async def test_audio_node_run_uses_audio_generation_service(monkeypatch):
    updates: list[dict] = []
    captured: dict = {}

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "audio",
            "status": "idle",
            "input": {"prompt": "一段安静的纯音频氛围"},
            "prompt": "",
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def fake_generate_audio(**kwargs):
        captured.update(kwargs)
        return {
            "ok": False,
            "type": "audio",
            "status": "failed",
            "error": "No active audio provider configured.",
            "error_kind": "bad_config",
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal.media_generation, "generate_audio", fake_generate_audio)

    result = await node_universal.node_run(project_id="proj-1", node_id="audio-1")

    assert result["ok"] is False
    assert result["error_kind"] == "bad_config"
    assert captured["project_id"] == "proj-1"
    assert captured["node_id"] == "audio-1"
    assert captured["prompt"] == "一段安静的纯音频氛围"
    assert captured["record_asset"] is True
    assert updates[0] == {"status": "running", "error_message": None}
    assert updates[-1]["status"] == "failed"


def test_video_defaults_preserve_duration_alias():
    fields = node_universal._apply_defaults("video", {"duration": 15, "aspect_ratio": "16:9"})

    assert fields["duration"] == 15
    assert fields["duration_seconds"] == 15
    assert fields["aspect_ratio"] == "16:9"


def test_image_resolution_requires_exact_pixels_matching_aspect_ratio():
    assert node_universal._resolve_size("2560x1440", "16:9") == "2560x1440"
    assert node_universal._resolve_size("3840x2160", "16:9") == "3840x2160"
    assert node_universal._resolve_size("2160x3840", "9:16") == "2160x3840"

    with pytest.raises(ValueError, match="精确像素"):
        node_universal._resolve_size("2k", "16:9")
    with pytest.raises(ValueError, match="aspect_ratio"):
        node_universal._resolve_size("2048x2048", "16:9")
    with pytest.raises(ValueError, match="最高 4K"):
        node_universal._resolve_size("7680x4320", "16:9")


def test_canvas_edge_payloads_prefer_node_authored_dependencies():
    script = SimpleNamespace(
        id="script-1",
        project_id="proj-1",
        input_json=json.dumps({"content": "剧本"}, ensure_ascii=False),
    )
    red = SimpleNamespace(
        id="red-1",
        project_id="proj-1",
        input_json=json.dumps({"depends_on": ["node:script-1"]}, ensure_ascii=False),
    )
    blue = SimpleNamespace(
        id="blue-1",
        project_id="proj-1",
        input_json=json.dumps({"references": [{"ref": "script-1", "role": "context"}]}, ensure_ascii=False),
    )

    class FakeEdge:
        def __init__(self, source: str, target: str):
            self.id = f"edge-{source}-{target}"
            self.project_id = "proj-1"
            self.source_node_id = source
            self.target_node_id = target
            self.label = None

        def model_dump(self):
            return {
                "id": self.id,
                "project_id": self.project_id,
                "source_node_id": self.source_node_id,
                "target_node_id": self.target_node_id,
                "label": self.label,
            }

    payloads = canvas_edge_payloads(
        [script, red, blue],
        [
            FakeEdge("script-1", "red-1"),
            FakeEdge("red-1", "blue-1"),
        ],
    )

    pairs = {(edge["source_node_id"], edge["target_node_id"]) for edge in payloads}
    assert pairs == {("script-1", "red-1"), ("script-1", "blue-1")}


@pytest.mark.asyncio
async def test_node_get_accepts_batch_node_ids(monkeypatch):
    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "image",
            "title": f"节点 {node_id}",
            "status": "completed",
            "input": {"prompt": f"prompt {node_id}"},
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)

    result = await node_universal.node_get(
        project_id="proj-1",
        node_ids=["node:a", "b", "node:a"],
    )

    assert result["ok"] is True
    assert result["requested"] == 2
    assert result["returned"] == 2
    assert [node["id"] for node in result["nodes"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_node_list_defaults_to_twenty_index_items_and_limit_zero_returns_all(monkeypatch):
    nodes = [
        {
            "id": f"node-{index}",
            "type": "image",
            "title": f"节点 {index}",
            "status": "idle",
            "prompt": f"12345678901234567890 extra {index}",
            "output": {"large": "not returned by node.list"},
        }
        for index in range(25)
    ]

    async def fake_list_nodes(project_id: str):
        assert project_id == "proj-1"
        return list(nodes)

    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)

    default_result = await node_universal.node_list("proj-1")
    null_limit_result = await node_universal.node_list("proj-1", limit=None)
    all_result = await node_universal.node_list("proj-1", limit=0)

    assert default_result["returned"] == 20
    assert default_result["total"] == 25
    assert default_result["truncated"] is True
    assert null_limit_result["returned"] == 20
    assert null_limit_result["filters"]["limit"] == 20
    first = default_result["nodes"][0]
    assert first["node_id"] == "node-0"
    assert first["title"] == "节点 0"
    assert first["status"] == "idle"
    assert first["prompt_preview"] == "12345678901234567890"
    assert "output" not in first
    assert all_result["returned"] == 25
    assert all_result["truncated"] is False
    assert all_result["filters"]["unlimited"] is True


@pytest.mark.asyncio
async def test_node_create_accepts_small_batch_and_resolves_prior_client_refs(monkeypatch):
    created_records: list[dict] = []
    edges: list[dict] = []

    async def fake_mode_gate(project_id: str, node_type: str, fields: dict):
        return True, None

    async def fake_project_state(project_id: str):
        return {"project_mode": "single_node", "project_sub_mode": None}

    async def fake_create_node(**kwargs):
        node = {
            "id": f"node-{len(created_records) + 1}",
            "project_id": kwargs["project_id"],
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "input": kwargs["input_data"],
            "prompt": kwargs["prompt"],
        }
        created_records.append(node)
        return dict(node)

    async def fake_list_nodes(project_id: str):
        return [dict(node) for node in created_records]

    async def fake_connect_nodes(project_id: str, source_node_id: str, target_node_id: str, label=None):
        edge = {"id": f"edge-{len(edges) + 1}", "source": source_node_id, "target": target_node_id}
        edges.append(edge)
        return edge

    async def fake_emit_edge(project_id: str, edge: dict | None):
        return None

    monkeypatch.setattr(node_universal, "_check_mode_and_guide_gate", fake_mode_gate)
    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(node_universal.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(node_universal, "_emit_edge_created", fake_emit_edge)

    result = await node_universal.node_create(
        project_id="proj-1",
        nodes=[
            {
                "client_ref": "brief",
                "type": "text",
                "fields": {"title": "项目 brief", "content": "做一个 15 秒短片"},
            },
            {
                "client_ref": "shots",
                "type": "text",
                "parent_node_id": "client:brief",
                "fields": {
                    "title": "镜头清单",
                    "content": "三段节奏",
                    "references": [{"ref": "client:brief", "role": "context"}],
                },
            },
        ],
    )

    assert result["ok"] is True
    assert result["created_count"] == 2
    assert result["client_node_ids"] == {"brief": "node-1", "shots": "node-2"}
    assert result["nodes"][1]["input"]["references"] == [{"ref": "node-1", "role": "context"}]
    assert any(edge["source"] == "node-1" and edge["target"] == "node-2" for edge in edges)


@pytest.mark.asyncio
async def test_node_update_accepts_batch_updates(monkeypatch):
    updates: list[tuple[str, dict]] = []

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "type": "image",
            "status": "failed",
            "title": f"节点 {node_id}",
            "prompt": "old prompt",
            "input": {
                "title": f"节点 {node_id}",
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "1024x576",
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append((node_id, patch))
        return {
            "id": node_id,
            "type": "image",
            "status": "failed",
            "title": patch.get("title", f"节点 {node_id}"),
            "prompt": patch.get("prompt", "old prompt"),
            "input_json": patch.get("input_json", {}),
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        updates=[
            {"node_id": "image-1", "patch": {"fields": {"resolution": "2560x1440"}}},
            {"node_id": "image-2", "patch": {"title": "新标题"}},
        ],
    )

    assert result["ok"] is True
    assert result["updated_count"] == 2
    assert [item[0] for item in updates] == ["image-1", "image-2"]
    assert updates[0][1]["input_json"]["resolution"] == "2560x1440"
    assert result["results"][0]["node_id"] == "image-1"
    assert result["results"][1]["node_id"] == "image-2"


def test_manual_image_edge_writes_visual_reference_for_text_and_image_targets():
    source = WorkflowNode(id="image-source", project_id="proj-1", type="image", title="参考图")
    text_target = WorkflowNode(id="text-target", project_id="proj-1", type="text", title="文字")
    image_target = WorkflowNode(
        id="image-target",
        project_id="proj-1",
        type="image",
        title="图片",
        input_json=json.dumps({"render_state": "fresh"}, ensure_ascii=False),
    )

    assert routes_projects._add_edge_dependency(text_target, source) is True
    assert routes_projects._add_edge_dependency(image_target, source) is True

    text_input = json.loads(text_target.input_json or "{}")
    image_input = json.loads(image_target.input_json or "{}")
    expected_ref = {"ref": "node:image-source", "role": "visual_reference"}
    assert text_input["depends_on"] == ["node:image-source"]
    assert text_input["references"] == [expected_ref]
    assert image_input["depends_on"] == ["node:image-source"]
    assert image_input["references"] == [expected_ref]
    assert image_input["render_state"] == "stale"

    assert routes_projects._remove_edge_dependency(image_target, source.id) is True
    image_input = json.loads(image_target.input_json or "{}")
    assert "depends_on" not in image_input
    assert "references" not in image_input
    assert image_input["render_state"] == "stale"


@pytest.mark.asyncio
async def test_completed_fusion_stage_clears_previous_error_diagnostics(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "output": {
                "type": "fusion",
                "subject": "image",
                "stages": [
                    {
                        "name": "图片",
                        "status": "failed",
                        "error": "provider 500",
                        "diagnostics": {"kind": "image_render_failure"},
                    }
                ],
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id}

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    fusion = await node_universal._merge_stage_into_fusion(
        "image-1",
        "image",
        status="completed",
        url="/api/media/project/new.png",
        local_url="/api/media/project/new.png",
        size="1920x1080",
        aspect_ratio="16:9",
        quality="high",
    )

    stage = fusion["stages"][0]
    assert stage["status"] == "completed"
    assert stage["url"] == "/api/media/project/new.png"
    assert "error" not in stage
    assert "diagnostics" not in stage
    assert updates[-1]["output_data"] == fusion


def test_media_provider_timeout_default_is_interactive(monkeypatch):
    monkeypatch.delenv("DRAMA_IMAGE_PROVIDER_TIMEOUT_SECONDS", raising=False)

    timeout = media_provider._media_http_timeout()

    assert timeout.connect == 60.0
    assert timeout.read == 300.0
    assert timeout.write == 300.0
    assert timeout.pool == 300.0


def test_media_provider_video_poll_timeout_default_is_twenty_minutes(monkeypatch):
    monkeypatch.delenv("DRAMA_VIDEO_POLL_TIMEOUT_SECONDS", raising=False)
    provider = SimpleNamespace(params_json="{}")
    assert media_provider._ark_poll_settings(provider, None) == (10.0, 1200.0)
    assert media_provider._xai_poll_settings(provider, None) == (5.0, 1200.0)


def test_media_history_keeps_only_successful_state_snapshots():
    current = {
        "type": "fusion",
        "subject": "image",
        "prompt": "old prompt",
        "input": {"prompt": "old prompt", "aspect_ratio": "16:9"},
        "stages": [{"name": "图片", "status": "completed", "local_url": "/api/media/p/old.png"}],
        "history": [
            {
                "id": "failed",
                "prompt": "failed prompt",
                "output": {"type": "image", "status": "failed", "local_url": "/api/media/p/failed.png", "error": "boom"},
            },
            {
                "id": "queued",
                "prompt": "queued prompt",
                "output": {"type": "video", "status": "queued", "local_url": "/api/media/p/queued.mp4"},
            },
            {
                "id": "success",
                "prompt": "success prompt",
                "input": {"prompt": "success prompt", "resolution": "720p"},
                "output": {"type": "image", "status": "completed", "local_url": "/api/media/p/success.png"},
            },
        ],
    }

    history = media_history.media_history_from_output(current)

    assert [item["id"] for item in history] == ["success"]
    assert history[0]["prompt"] == "success prompt"
    assert history[0]["input"]["resolution"] == "720p"


def test_media_history_switch_returns_selected_output_and_state_snapshot():
    current = {
        "type": "image",
        "status": "completed",
        "prompt": "current prompt",
        "input": {"prompt": "current prompt", "resolution": "1080p"},
        "local_url": "/api/media/p/current.png",
        "history": [
            {
                "id": "hist-1",
                "prompt": "history prompt",
                "input": {"prompt": "history prompt", "resolution": "720p"},
                "output": {"type": "image", "status": "completed", "local_url": "/api/media/p/history.png"},
            }
        ],
    }

    next_output, selected = media_history.switch_media_history_version(current, history_id="hist-1")

    assert next_output["local_url"] == "/api/media/p/history.png"
    assert selected["prompt"] == "history prompt"
    assert selected["input"]["resolution"] == "720p"
    assert next_output["history"][0]["prompt"] == "current prompt"
    assert next_output["history"][0]["input"]["resolution"] == "1080p"


def test_media_provider_schema_accepts_xai_video_format():
    entry = MediaProviderEntry(
        kind="video",
        name="xai-grok-video",
        base_url="https://api.x.ai/v1",
        api_key="xai-key",
        model_name="grok-imagine-video-1.5",
        api_format="xai_video",
    )

    assert entry.api_format == "xai_video"


def test_media_provider_schema_accepts_grok_1_5_video_format():
    entry = MediaProviderEntry(
        kind="video",
        name="grok-1-5-video",
        base_url="https://relay.example/v1",
        api_key="relay-key",
        model_name="grok-1.5-video-15s",
        api_format="grok_1_5",
    )

    assert entry.api_format == "grok_1_5"


def test_media_provider_schema_accepts_t8_grok_video_3_format():
    entry = MediaProviderEntry(
        kind="video",
        name="t8-grok-video-3",
        base_url="https://relay.example",
        api_key="relay-key",
        model_name="grok-video-3",
        api_format="t8_grok_video_3",
    )

    assert entry.api_format == "t8_grok_video_3"


def test_media_provider_schema_accepts_suno_compatible_audio_format():
    entry = MediaProviderEntry(
        kind="audio",
        name="suno-compatible",
        base_url="https://audio.example",
        api_key="audio-key",
        model_name="V5",
        api_format="suno_compatible",
    )

    assert entry.kind == "audio"
    assert entry.api_format == "suno_compatible"


def test_media_provider_schema_accepts_openai_tts_audio_format():
    entry = MediaProviderEntry(
        kind="audio",
        name="openai-tts",
        base_url="https://audio.example/v1",
        api_key="audio-key",
        model_name="tts-1",
        api_format="openai_tts",
    )

    assert entry.kind == "audio"
    assert entry.api_format == "openai_tts"


def test_openai_tts_payload_prefers_node_format_and_filters_music_fields():
    provider = SimpleNamespace(
        model_name="tts-1",
        params_json=json.dumps({
            "response_format": "mp3",
            "speed": 1.05,
            "custom_mode": True,
        }),
    )

    payload, error = media_provider._build_openai_tts_payload(
        provider,
        prompt="旁白文本",
        extra_override={
            "voice": "nova",
            "format": "wav",
            "instructions": "自然、清晰的旁白",
            "negative_tags": "noise",
            "_debug": "hidden",
        },
    )

    assert error is None
    assert payload == {
        "model": "tts-1",
        "input": "旁白文本",
        "voice": "nova",
        "response_format": "wav",
        "speed": 1.05,
        "instructions": "自然、清晰的旁白",
    }


@pytest.mark.asyncio
async def test_audio_provider_routes_openai_tts(monkeypatch):
    provider = SimpleNamespace(
        name="tts-provider",
        kind="audio",
        api_format="openai_tts",
        base_url="https://audio.example/v1",
        api_key="audio-key",
        model_name="tts-1",
        enabled=True,
        params_json=None,
    )
    captured: dict = {}

    async def fake_get_active_provider(kind: str):
        assert kind == "audio"
        return provider

    async def fake_call_openai_tts_audio(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "provider": provider.name,
            "model": provider.model_name,
            "voice": kwargs["extra_override"]["voice"],
            "format": kwargs["extra_override"]["format"],
        }

    monkeypatch.setattr(media_provider, "_get_active_provider", fake_get_active_provider)
    monkeypatch.setattr(media_provider, "_call_openai_tts_audio", fake_call_openai_tts_audio)

    result = await media_provider.generate_audio_with_provider(
        project_id="proj-1",
        prompt="生成一句旁白",
        style="温和",
        extra={"voice": "nova", "format": "wav"},
    )

    assert result["ok"] is True
    assert result["provider"] == "tts-provider"
    assert captured["project_id"] == "proj-1"
    assert captured["prompt"] == "生成一句旁白"
    assert captured["extra_override"] == {
        "voice": "nova",
        "format": "wav",
        "style": "温和",
    }


def test_suno_audio_response_parser_handles_nested_audio_object():
    items = media_provider._collect_suno_audio_items({
        "code": 200,
        "data": {
            "status": "SUCCESS",
            "response": {
                "sunoData": [
                    {
                        "id": "song-1",
                        "title": "Theme",
                        "audio": {"audioUrl": "https://example.com/theme.mp3"},
                        "imageUrl": "https://example.com/theme.png",
                    }
                ]
            },
        },
    })

    assert items == [
        {
            "id": "song-1",
            "title": "Theme",
            "url": "https://example.com/theme.mp3",
            "remote_url": "https://example.com/theme.mp3",
            "source_audio_url": None,
            "stream_audio_url": None,
            "image_url": "https://example.com/theme.png",
            "duration_seconds": None,
            "tags": None,
        }
    ]


@pytest.mark.asyncio
async def test_node_create_rejects_legacy_type_before_side_effects():
    result = await node_universal.node_create(
        project_id="proj-1",
        type="segment_video_prompt",
        fields={},
    )

    assert "未知节点类型" in result["error"]
    assert "text" in result["error"]
    assert "image" in result["error"]
    assert "video" in result["error"]


@pytest.mark.asyncio
async def test_node_create_rejects_image_tier_resolution_before_side_effects(monkeypatch):
    async def fake_mode_gate(project_id: str, node_type: str, fields: dict):
        return True, None

    async def fake_project_state(project_id: str):
        return {"project_mode": "single_node", "project_sub_mode": None}

    async def fail_create_node(**kwargs):
        raise AssertionError("node.create should reject invalid resolution before DB write")

    monkeypatch.setattr(node_universal, "_check_mode_and_guide_gate", fake_mode_gate)
    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.canvas_tools, "create_node", fail_create_node)

    result = await node_universal.node_create(
        project_id="proj-1",
        type="image",
        fields={
            "title": "人物图",
            "prompt": "一张人物设定图",
            "aspect_ratio": "16:9",
            "resolution": "2K",
        },
    )

    assert result["ok"] is False
    assert result["error_kind"] == "invalid_resolution"
    assert "精确像素" in result["error"]
    assert "2560x1440" in result["hint"]


@pytest.mark.asyncio
async def test_node_create_prompt_returns_review_checkpoint_without_status_override(monkeypatch):
    async def fake_mode_gate(project_id: str, node_type: str, fields: dict):
        return True, None

    async def fake_project_state(project_id: str):
        return {"project_mode": "single_node", "project_sub_mode": None}

    async def fake_create_node(**kwargs):
        return {
            "id": "image-1",
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "prompt": kwargs["prompt"],
        }

    async def fake_list_nodes(project_id: str):
        return []

    monkeypatch.setattr(node_universal, "_check_mode_and_guide_gate", fake_mode_gate)
    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)

    result = await node_universal.node_create(
        project_id="proj-1",
        type="image",
        fields={
            "title": "人物图",
            "prompt": "人物设定图 prompt",
            "aspect_ratio": "16:9",
            "resolution": "2560x1440",
        },
    )

    assert result["id"] == "image-1"
    assert result["status"] == "idle"
    assert result["review_recommended"] is True
    assert result["review_status"] == "review_recommended"
    assert result["recommended_tool"] == "agent.review"


@pytest.mark.asyncio
async def test_node_create_parent_dependency_auto_connects_and_emits_edge(monkeypatch):
    captured: dict = {"edges": [], "events": []}

    async def fake_mode_gate(project_id: str, node_type: str, fields: dict):
        return True, {}

    async def fake_project_state(project_id: str):
        return {"project_mode": "single_node", "project_sub_mode": None}

    async def fake_create_node(**kwargs):
        return {
            "id": "child-node",
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
        }

    async def fake_connect_nodes(project_id: str, source_node_id: str, target_node_id: str, label=None):
        edge = {
            "id": f"edge-{len(captured['edges']) + 1}",
            "source": source_node_id,
            "target": target_node_id,
            "label": label,
        }
        captured["edges"].append(edge)
        return edge

    async def fake_list_nodes(project_id: str):
        return []

    async def fake_emit_edge(project_id: str, edge: dict | None):
        captured["events"].append((project_id, edge))

    monkeypatch.setattr(node_universal, "_check_mode_and_guide_gate", fake_mode_gate)
    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(node_universal.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(node_universal, "_emit_edge_created", fake_emit_edge)

    result = await node_universal.node_create(
        project_id="proj-1",
        type="text",
        fields={"content": "child"},
        parent_node_id="parent-node",
    )

    assert result["id"] == "child-node"
    assert captured["edges"] == [
        {"id": "edge-1", "source": "parent-node", "target": "child-node", "label": None}
    ]
    assert captured["events"] == [("proj-1", captured["edges"][0])]


@pytest.mark.asyncio
async def test_auto_connect_topology_uses_structured_references_node_refs(monkeypatch):
    captured: dict = {"edges": [], "events": []}

    async def fake_list_nodes(project_id: str):
        assert project_id == "proj-1"
        return [{"id": "image-1"}, {"id": "video-1"}]

    async def fake_connect_nodes(project_id: str, source_node_id: str, target_node_id: str, label=None):
        edge = {"id": "edge-1", "source": source_node_id, "target": target_node_id, "label": label}
        captured["edges"].append(edge)
        return edge

    async def fake_emit_edge(project_id: str, edge: dict | None):
        captured["events"].append((project_id, edge))

    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(node_universal.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(node_universal, "_emit_edge_created", fake_emit_edge)

    await node_universal._auto_connect_topology(
        "proj-1",
        "video-1",
        "video",
        {"references": [{"ref": "node:image-1", "role": "visual_reference"}]},
    )

    assert captured["edges"] == [
        {"id": "edge-1", "source": "image-1", "target": "video-1", "label": None}
    ]
    assert captured["events"] == [("proj-1", captured["edges"][0])]


@pytest.mark.asyncio
async def test_normalize_reference_images_accepts_bare_completed_image_node_id(monkeypatch):
    node_id = "af7347f1-6e75-49b8-ab8b-387b21bb8ed9"

    async def fake_project_state(project_id: str):
        assert project_id == "proj-1"
        return {}

    async def fake_get_node(requested_id: str):
        assert requested_id == node_id
        return {"id": node_id, "project_id": "proj-1", "type": "image", "status": "completed"}

    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)

    refs, warnings = await node_universal._normalize_reference_images_for_render(
        "proj-1",
        [node_id],
    )

    assert refs == [f"node:{node_id}"]
    assert "裸节点 ID" in warnings[0]


@pytest.mark.asyncio
async def test_normalize_reference_images_skips_text_node_refs(monkeypatch):
    node_id = "3f7ebcc7-45ff-4ae8-b58c-b28ee8f25116"

    async def fake_project_state(project_id: str):
        assert project_id == "proj-1"
        return {}

    async def fake_get_node(requested_id: str):
        assert requested_id == node_id
        return {"id": node_id, "project_id": "proj-1", "type": "text", "title": "剧本"}

    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)

    refs, warnings = await node_universal._normalize_reference_images_for_render(
        "proj-1",
        [f"node:{node_id}"],
    )

    assert refs == []
    assert "非图片节点 剧本" in warnings[0]


@pytest.mark.asyncio
async def test_video_runner_requires_model_authored_prompt():
    result = await node_universal._run_video_node("proj-1", "node-1", {"duration_seconds": 15})

    assert result["error_kind"] == "missing_prompt"
    assert result["type"] == "video"


@pytest.mark.asyncio
async def test_video_runner_passes_resolved_reference_images(monkeypatch):
    captured: dict = {}

    async def fake_reference_images(project_id: str, fields: dict):
        assert project_id == "proj-1"
        assert fields["references"] == [
            {"ref": "@scene_ref", "role": "visual_reference"},
            {"ref": "@storyboard_grid", "role": "visual_reference"},
        ]
        return ["node:image-1", "node:storyboard-1"], ["跳过未完成参考图"]

    async def fake_update_node(node_id: str, patch: dict):
        captured["update"] = (node_id, patch)
        return {"id": node_id, **patch}

    async def fake_generate_video(**kwargs):
        captured["generate"] = kwargs
        return {
            "status": "queued",
            "provider": "stub",
            "reference_images": kwargs.get("reference_images") or [],
        }

    monkeypatch.setattr(node_universal, "_reference_images_for_video_run", fake_reference_images)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal.media_generation, "generate_video", fake_generate_video)

    result = await node_universal._run_video_node(
        "proj-1",
        "video-1",
        {
            "prompt": "15秒动作短片",
            "duration_seconds": 15,
            "aspect_ratio": "9:16",
            "resolution": "1440x2560",
            "generate_audio": False,
            "references": [
                {"ref": "@scene_ref", "role": "visual_reference"},
                {"ref": "@storyboard_grid", "role": "visual_reference"},
            ],
        },
    )

    assert captured["generate"]["reference_images"] == ["node:image-1", "node:storyboard-1"]
    assert captured["generate"]["aspect_ratio"] == "9:16"
    assert captured["generate"]["resolution"] == "1440x2560"
    assert captured["generate"]["extra"]["generate_audio"] is False
    assert captured["update"][0] == "video-1"
    assert captured["update"][1]["input_json"]["reference_images"] == ["node:image-1", "node:storyboard-1"]
    assert result["reference_warnings"] == ["跳过未完成参考图"]


@pytest.mark.asyncio
async def test_media_generation_video_preserves_reference_images_without_default_asset_record(monkeypatch):
    captured: dict = {}

    async def fake_generate_video_with_provider(**kwargs):
        captured["provider"] = kwargs
        return {
            "ok": True,
            "provider": "video-provider",
            "model": "video-model",
            "status": "completed",
            "url": "https://example.com/video.mp4",
            "resolved_reference_images": ["/tmp/ref.png"],
            "reference_warnings": [],
        }

    async def fake_register_asset(**kwargs):
        captured["asset"] = kwargs
        return {"id": "asset-video-1"}

    monkeypatch.setattr(media_generation, "generate_video_with_provider", fake_generate_video_with_provider)
    monkeypatch.setattr(media_generation, "register_asset", fake_register_asset)

    result = await media_generation.generate_video(
        project_id="proj-1",
        prompt="video prompt",
        node_id="video-1",
        aspect_ratio="9:16",
        resolution="1440x2560",
        reference_images=["node:image-1"],
    )

    assert captured["provider"]["reference_images"] == ["node:image-1"]
    assert captured["provider"]["extra"]["aspect_ratio"] == "9:16"
    assert captured["provider"]["extra"]["resolution"] == "1440x2560"
    assert "asset" not in captured
    assert result["asset_id"] is None
    assert result["reference_images"] == ["node:image-1"]
    assert result["resolved_reference_images"] == ["/tmp/ref.png"]


@pytest.mark.asyncio
async def test_media_generation_video_queues_background_poll(monkeypatch):
    captured: dict = {}

    async def fake_generate_video_with_provider(**kwargs):
        captured["provider"] = kwargs
        return {
            "ok": True,
            "provider": "ark-video",
            "model": "doubao-seedance-2-0-260128",
            "status": "queued",
            "job_id": "ark-task-1",
            "resolved_reference_images": ["https://example.com/ref.png"],
            "reference_warnings": [],
        }

    def fake_schedule_background_video_poll(**kwargs):
        captured["background"] = kwargs

    monkeypatch.setattr(media_generation, "generate_video_with_provider", fake_generate_video_with_provider)
    monkeypatch.setattr(media_generation, "_schedule_background_video_poll", fake_schedule_background_video_poll)

    result = await media_generation.generate_video(
        project_id="proj-1",
        prompt="video prompt",
        node_id="video-1",
        model="doubao-seedance-2-0-260128",
        duration_seconds=15,
        aspect_ratio="9:16",
        resolution="1440x2560",
        reference_images=["node:image-1"],
        record_asset=True,
    )

    assert captured["provider"]["wait_for_completion"] is False
    assert captured["background"]["node_id"] == "video-1"
    assert captured["background"]["record_asset"] is True
    assert captured["background"]["queued_result"]["job_id"] == "ark-task-1"
    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["async"] is True
    assert result["job_id"] == "ark-task-1"


@pytest.mark.asyncio
async def test_media_generation_audio_queues_background_poll(monkeypatch):
    captured: dict = {}

    async def fake_generate_audio_with_provider(**kwargs):
        captured["provider"] = kwargs
        return {
            "ok": True,
            "provider": "suno-audio",
            "model": "V5",
            "status": "queued",
            "job_id": "audio-task-1",
        }

    def fake_schedule_background_audio_poll(**kwargs):
        captured["background"] = kwargs

    monkeypatch.setattr(media_generation, "generate_audio_with_provider", fake_generate_audio_with_provider)
    monkeypatch.setattr(media_generation, "_schedule_background_audio_poll", fake_schedule_background_audio_poll)

    result = await media_generation.generate_audio(
        project_id="proj-1",
        prompt="quiet piano theme",
        node_id="audio-1",
        model="suno-audio",
        title="Quiet Theme",
        style="ambient piano",
        instrumental=True,
        record_asset=True,
    )

    assert captured["provider"]["wait_for_completion"] is False
    assert captured["provider"]["instrumental"] is True
    assert captured["background"]["node_id"] == "audio-1"
    assert captured["background"]["record_asset"] is True
    assert captured["background"]["queued_result"]["job_id"] == "audio-task-1"
    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["async"] is True
    assert result["job_id"] == "audio-task-1"


@pytest.mark.asyncio
async def test_node_run_video_queue_keeps_node_running(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "video",
            "status": "idle",
            "title": "视频",
            "prompt": "video prompt",
            "input": {"prompt": "video prompt", "duration_seconds": 15},
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def fake_video_runner(project_id: str, node_id: str, fields: dict):
        return {
            "ok": True,
            "type": "video",
            "status": "queued",
            "job_id": "ark-task-1",
            "provider": "ark-video",
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setitem(node_universal._RUNNERS, "video", fake_video_runner)

    result = await node_universal.node_run(project_id="proj-1", node_id="video-1")

    assert result["ok"] is True
    assert result["async"] is True
    assert result["status"] == "queued"
    assert updates[0] == {"status": "running", "error_message": None}
    assert updates[-1]["status"] == "running"
    assert updates[-1]["output_data"]["job_id"] == "ark-task-1"


@pytest.mark.asyncio
async def test_node_run_audio_queue_keeps_node_running(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "audio",
            "status": "idle",
            "title": "音频",
            "prompt": "audio prompt",
            "input": {"prompt": "audio prompt", "style": "ambient", "instrumental": True},
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def fake_audio_runner(project_id: str, node_id: str, fields: dict):
        return {
            "ok": True,
            "type": "audio",
            "status": "queued",
            "job_id": "audio-task-1",
            "provider": "suno-audio",
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setitem(node_universal._RUNNERS, "audio", fake_audio_runner)

    result = await node_universal.node_run(project_id="proj-1", node_id="audio-1")

    assert result["ok"] is True
    assert result["async"] is True
    assert result["status"] == "queued"
    assert updates[0] == {"status": "running", "error_message": None}
    assert updates[-1]["status"] == "running"
    assert updates[-1]["output_data"]["job_id"] == "audio-task-1"


@pytest.mark.asyncio
async def test_volcengine_ark_seedance_payload_uses_model_specific_params():
    provider = SimpleNamespace(
        model_name="doubao-seedance-2-0-260128",
        params_json=json.dumps({"watermark": False, "generate_audio": True}, ensure_ascii=False),
    )

    payload, meta = await media_provider._build_ark_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="一只纸船沿着霓虹河道漂流，电影感",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=["https://example.com/ref.png"],
        extra_override={
            "aspect_ratio": "9:16",
            "resolution": "1440x2560",
            "generate_audio": False,
            "return_last_frame": True,
            "seed": 123,
        },
    )

    assert meta is None
    assert payload["model"] == "doubao-seedance-2-0-260128"
    assert payload["duration"] == 15
    assert payload["ratio"] == "9:16"
    assert payload["resolution"] == "1080p"
    assert payload["generate_audio"] is False
    assert payload["watermark"] is False
    assert payload["return_last_frame"] is True
    assert payload["seed"] == 123
    assert payload["content"][0] == {
        "type": "text",
        "text": "一只纸船沿着霓虹河道漂流，电影感",
    }
    assert payload["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/ref.png"},
        "role": "reference_image",
    }


@pytest.mark.parametrize(
    ("model_name", "variant"),
    [
        ("doubao-seedance-2-0-fast-260128", "Fast"),
        ("doubao-seedance-2-0-mini-260615", "Mini"),
    ],
)
@pytest.mark.asyncio
async def test_volcengine_ark_seedance_fast_and_mini_reject_1080p(model_name, variant):
    provider = SimpleNamespace(
        model_name=model_name,
        params_json="{}",
    )

    payload, error = await media_provider._build_ark_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="fast video",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=4,
        reference_images=None,
        extra_override={"resolution": "1080p"},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert variant in error["error"]
    assert "apps/api/app/skills/video_production/VIDEO_MODEL_CALLING.md" in error["hint"]
    assert error["model_feedback"]["suggested_next"] == (
        "read_video_model_calling_doc_then_update_original_video_node"
    )


@pytest.mark.asyncio
async def test_volcengine_ark_rejects_placeholder_resolution_with_doc_hint():
    provider = SimpleNamespace(
        model_name="doubao-seedance-2-0-260128",
        params_json="{}",
    )

    payload, error = await media_provider._build_ark_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="standard video",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=4,
        reference_images=None,
        extra_override={"resolution": "2k"},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert error["supported_resolutions"] == ["480p", "720p", "1080p"]
    assert "VIDEO_MODEL_CALLING.md" in error["hint"]


@pytest.mark.asyncio
async def test_volcengine_ark_seedance_mini_uses_seedance_2_params():
    provider = SimpleNamespace(
        model_name="doubao-seedance-2-0-mini-260615",
        params_json=json.dumps({"generate_audio": True, "return_last_frame": True}, ensure_ascii=False),
    )

    payload, error = await media_provider._build_ark_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="mini video",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=4,
        reference_images=None,
        extra_override={"aspect_ratio": "16:9", "resolution": "720p"},
    )

    assert error is None
    assert payload["model"] == "doubao-seedance-2-0-mini-260615"
    assert payload["duration"] == 4
    assert payload["ratio"] == "16:9"
    assert payload["resolution"] == "720p"
    assert payload["generate_audio"] is True
    assert payload["return_last_frame"] is True


@pytest.mark.asyncio
async def test_xai_video_payload_requires_exactly_one_source_image():
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        params_json="{}",
    )

    payload, error = await media_provider._build_xai_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate this still image with gentle camera motion",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=["https://example.com/a.png", "https://example.com/b.png"],
        extra_override={},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert "只支持一张源图" in error["error"]
    assert "VIDEO_MODEL_CALLING.md" in error["hint"]
    assert error["model_feedback"]["suggested_next"] == (
        "read_video_model_calling_doc_then_update_original_video_node"
    )


@pytest.mark.asyncio
async def test_xai_video_payload_rejects_unsupported_resolution_with_doc_hint():
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        params_json=json.dumps({"resolution": "1080p"}, ensure_ascii=False),
    )

    payload, error = await media_provider._build_xai_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate this still image with gentle camera motion",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=["https://example.com/source.png"],
        extra_override={},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert error["supported_resolutions"] == ["480p", "720p"]
    assert "VIDEO_MODEL_CALLING.md" in error["hint"]


@pytest.mark.asyncio
async def test_xai_video_payload_uses_one_image_url_and_duration():
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        params_json=json.dumps({"resolution": "720p"}, ensure_ascii=False),
    )

    payload, meta = await media_provider._build_xai_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate this still image with a slow cinematic push-in",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=12,
        reference_images=["https://example.com/source.png"],
        extra_override={"seed": 123},
    )

    assert meta == {
        "source_image_kind": "reference_images",
        "source_image_ref": "https://example.com/source.png",
    }
    assert payload == {
        "model": "grok-imagine-video-1.5",
        "prompt": "Animate this still image with a slow cinematic push-in",
        "image": {"url": "https://example.com/source.png"},
        "duration": 12,
        "resolution": "720p",
        "seed": 123,
    }


@pytest.mark.asyncio
async def test_grok_1_5_video_payload_uses_multipart_fields(monkeypatch):
    provider = SimpleNamespace(
        name="grok-1-5-video",
        model_name="grok-1.5-video-15s",
        params_json=json.dumps({"resolution": "720p"}, ensure_ascii=False),
    )

    async def fake_image_file_input(project_id, ref):
        return ("source.png", b"png-bytes", "image/png"), None

    monkeypatch.setattr(media_provider, "_image_file_input", fake_image_file_input)

    data, image_file, meta = await media_provider._build_grok_1_5_video_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate this portrait with a confident pose",
        first_frame_url=None,
        last_frame_url=None,
        reference_images=["/api/media/proj-1/source.png"],
        extra_override={"aspect_ratio": "16:9"},
    )

    assert data == {
        "model": "grok-1.5-video-15s",
        "prompt": "Animate this portrait with a confident pose",
        "size": "1280x720",
    }
    assert image_file == ("source.png", b"png-bytes", "image/png")
    assert meta == {
        "source_image_kind": "reference_images",
        "source_image_ref": "/api/media/proj-1/source.png",
    }


@pytest.mark.asyncio
async def test_grok_1_5_video_submit_uses_configured_base_url_and_file(monkeypatch):
    provider = SimpleNamespace(
        name="grok-1-5-video",
        model_name="grok-1.5-video-15s",
        base_url="https://relay.example/v1",
        api_key="relay-key",
        params_json=json.dumps({"resolution": "720p"}, ensure_ascii=False),
    )
    captured: dict = {}

    async def fake_image_file_input(project_id, ref):
        return ("source.png", b"png-bytes", "image/png"), None

    class FakeResponse:
        status_code = 200
        text = '{"id":"job-1","status":"queued"}'

        def json(self):
            return {"id": "job-1", "status": "queued"}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, endpoint, data, files, headers):
            captured["endpoint"] = endpoint
            captured["data"] = data
            captured["files"] = files
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(media_provider, "_image_file_input", fake_image_file_input)
    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._call_grok_1_5_video(
        provider=provider,
        project_id="proj-1",
        prompt="Animate the person into a confident pose",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=5,
        reference_images=["/api/media/proj-1/source.png"],
        extra_override={"aspect_ratio": "16:9"},
        save_locally=False,
        wait_for_completion=False,
    )

    assert captured["endpoint"] == "https://relay.example/v1/videos"
    assert captured["headers"]["Authorization"] == "Bearer relay-key"
    assert captured["data"] == {
        "model": "grok-1.5-video-15s",
        "prompt": "Animate the person into a confident pose",
        "size": "1280x720",
    }
    assert captured["files"]["input_reference"] == ("source.png", b"png-bytes", "image/png")
    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["job_id"] == "job-1"
    assert result["query_endpoint"] == "https://relay.example/v1/videos/job-1"


@pytest.mark.asyncio
async def test_xai_video_submit_returns_queued_job(monkeypatch):
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        base_url="https://api.x.ai/v1",
        api_key="xai-key",
        params_json="{}",
    )
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{"request_id":"req-1"}'

        def json(self):
            return {"request_id": "req-1"}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, endpoint, json, headers):
            captured["endpoint"] = endpoint
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._call_xai_video(
        provider=provider,
        project_id="proj-1",
        prompt="Animate the still image into a calm time-lapse",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=["https://example.com/source.png"],
        extra_override={},
        save_locally=False,
        wait_for_completion=False,
    )

    assert captured["endpoint"] == "https://api.x.ai/v1/videos/generations"
    assert captured["headers"]["Authorization"] == "Bearer xai-key"
    assert captured["json"]["model"] == "grok-imagine-video-1.5"
    assert captured["json"]["image"] == {"url": "https://example.com/source.png"}
    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["job_id"] == "req-1"
    assert result["query_endpoint"] == "https://api.x.ai/v1/videos/req-1"


@pytest.mark.asyncio
async def test_xai_video_poll_done_downloads_video(monkeypatch):
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        base_url="https://api.x.ai/v1",
        api_key="xai-key",
        params_json=json.dumps({"_poll_interval_seconds": 1, "_poll_timeout_seconds": 2}),
    )
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{"status":"done"}'

        def json(self):
            return {
                "status": "done",
                "video": {
                    "url": "https://example.com/video.mp4",
                    "duration": 12,
                    "thumbnail_url": "https://example.com/thumb.jpg",
                },
                "model": "grok-imagine-video-1.5",
                "usage": {"cost_in_usd_ticks": 500000000},
                "progress": 100,
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, endpoint, headers):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            return FakeResponse()

    async def fake_download(project_id: str, remote_url: str):
        captured["download"] = (project_id, remote_url)
        return {
            "local_url": "/api/media/proj-1/generated_videos/video.mp4",
            "local_path": "/tmp/video.mp4",
        }

    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(media_provider, "_download_video_result", fake_download)

    result = await media_provider._poll_xai_video_task(
        provider=provider,
        project_id="proj-1",
        request_id="req-1",
        extra_override={},
        save_locally=True,
    )

    assert captured["endpoint"] == "https://api.x.ai/v1/videos/req-1"
    assert captured["headers"]["Authorization"] == "Bearer xai-key"
    assert captured["download"] == ("proj-1", "https://example.com/video.mp4")
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["url"] == "/api/media/proj-1/generated_videos/video.mp4"
    assert result["remote_url"] == "https://example.com/video.mp4"
    assert result["thumbnail_url"] == "https://example.com/thumb.jpg"
    assert result["usage"] == {"cost_in_usd_ticks": 500000000}


def test_t8_grok_video_3_adapter_capabilities_are_structured():
    provider = SimpleNamespace(
        api_format="t8_grok_video_3",
        model_name="grok-video-3",
    )

    adapter = media_provider._video_provider_adapter(provider)
    assert adapter is not None
    capabilities = media_provider._video_adapter_capabilities(adapter)

    assert adapter.name == "t8_grok_video_3"
    assert capabilities["source_images_min"] == 0
    assert capabilities["source_images_max"] == 7
    assert capabilities["field_types"]["duration"] == "integer"
    assert capabilities["field_types"]["images"] == "url_list"
    assert capabilities["supported_resolutions"] == ["480p", "720p", "1080p"]


@pytest.mark.asyncio
async def test_t8_grok_video_3_payload_uses_structured_spec():
    provider = SimpleNamespace(
        name="t8-grok-video-3",
        model_name="grok-video-3",
        params_json=json.dumps({"resolution": "1080p"}, ensure_ascii=False),
    )

    payload, image_candidates, meta = await media_provider._build_t8_grok_video_3_payload(
        provider=provider,
        project_id="proj-1",
        prompt="A cinematic product shot with slow camera movement. Use @img1 and @img2 as references.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=["https://example.com/a.png", "https://example.com/b.png"],
        extra_override={"aspect_ratio": "9:16", "seed": 123},
    )

    assert meta["source_image_count"] == 2
    assert image_candidates == [
        ("reference_images", "https://example.com/a.png"),
        ("reference_images", "https://example.com/b.png"),
    ]
    assert payload == {
        "prompt": "A cinematic product shot with slow camera movement. Use @img1 and @img2 as references.",
        "model": "grok-video-3",
        "ratio": "9:16",
        "duration": 15,
        "resolution": "1080P",
        "seed": 123,
    }


@pytest.mark.asyncio
async def test_t8_grok_video_3_payload_rejects_more_than_seven_images():
    provider = SimpleNamespace(
        name="t8-grok-video-3",
        model_name="grok-video-3",
        params_json="{}",
    )

    payload, image_candidates, error = await media_provider._build_t8_grok_video_3_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate all references.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=10,
        reference_images=[f"https://example.com/{idx}.png" for idx in range(8)],
        extra_override={},
    )

    assert payload is None
    assert image_candidates == []
    assert error["error_kind"] == "bad_request"
    assert "最多支持 7 张参考图" in error["error"]
    assert error["model_feedback"]["suggested_next"] == (
        "read_video_model_calling_doc_then_update_original_video_node"
    )


@pytest.mark.asyncio
async def test_t8_grok_video_3_submit_uploads_references_and_returns_job(monkeypatch):
    provider = SimpleNamespace(
        name="t8-grok-video-3",
        model_name="grok-video-3",
        base_url="https://relay.example/v1",
        api_key="relay-key",
        params_json=json.dumps({"resolution": "720p"}, ensure_ascii=False),
    )
    captured: dict = {"uploads": []}

    async def fake_image_file_input(project_id, ref):
        return ("source.png", f"bytes-{ref}".encode(), "image/png"), None

    class FakeResponse:
        def __init__(self, data):
            self.status_code = 200
            self._data = data
            self.text = json.dumps(data)

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, endpoint, **kwargs):
            if endpoint == "https://relay.example/v1/files":
                captured["uploads"].append(kwargs)
                return FakeResponse({"url": f"https://files.example/{len(captured['uploads'])}.png"})
            captured["endpoint"] = endpoint
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            return FakeResponse({"task_id": "task-1", "status": "NOT_START"})

    monkeypatch.setattr(media_provider, "_image_file_input", fake_image_file_input)
    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._call_t8_grok_video_3(
        provider=provider,
        project_id="proj-1",
        prompt="A neon-lit street scene. @img1 is the character, @img2 is the setting.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=10,
        reference_images=["/api/media/proj-1/a.png", "/api/media/proj-1/b.png"],
        extra_override={"aspect_ratio": "16:9"},
        save_locally=False,
        wait_for_completion=False,
    )

    assert len(captured["uploads"]) == 2
    assert captured["uploads"][0]["headers"]["Authorization"] == "Bearer relay-key"
    assert captured["endpoint"] == "https://relay.example/v2/videos/generations"
    assert captured["headers"]["Authorization"] == "Bearer relay-key"
    assert captured["json"]["images"] == ["https://files.example/1.png", "https://files.example/2.png"]
    assert captured["json"]["duration"] == 10
    assert captured["json"]["resolution"] == "720P"
    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["job_id"] == "task-1"
    assert result["query_endpoint"] == "https://relay.example/v2/videos/generations/task-1"


@pytest.mark.asyncio
async def test_t8_grok_video_3_poll_success_downloads_data_output(monkeypatch):
    provider = SimpleNamespace(
        name="t8-grok-video-3",
        model_name="grok-video-3",
        base_url="https://relay.example",
        api_key="relay-key",
        params_json=json.dumps({"_poll_interval_seconds": 1, "_poll_timeout_seconds": 2}),
    )
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{"status":"SUCCESS"}'

        def json(self):
            return {
                "status": "SUCCESS",
                "progress": 100,
                "data": {"output": "https://example.com/video.mp4"},
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, endpoint, headers):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            return FakeResponse()

    async def fake_download(project_id: str, remote_url: str):
        captured["download"] = (project_id, remote_url)
        return {
            "local_url": "/api/media/proj-1/generated_videos/video.mp4",
            "local_path": "/tmp/video.mp4",
        }

    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(media_provider, "_download_video_result", fake_download)

    result = await media_provider._poll_t8_grok_video_3_task(
        provider=provider,
        project_id="proj-1",
        task_id="task-1",
        extra_override={},
        save_locally=True,
    )

    assert captured["endpoint"] == "https://relay.example/v2/videos/generations/task-1"
    assert captured["headers"]["Authorization"] == "Bearer relay-key"
    assert captured["download"] == ("proj-1", "https://example.com/video.mp4")
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["url"] == "/api/media/proj-1/generated_videos/video.mp4"
    assert result["remote_url"] == "https://example.com/video.mp4"


@pytest.mark.asyncio
async def test_media_reference_resolution_excludes_source_image_role(monkeypatch):
    rows = [
        SimpleNamespace(
            id="storyboard-image",
            title="分镜图",
            type="image",
            status="completed",
            input_json=json.dumps({"blueprint_node_id": "storyboard_01"}, ensure_ascii=False),
        ),
        SimpleNamespace(
            id="source-image",
            title="直接采用图",
            type="image",
            status="completed",
            input_json=json.dumps({"blueprint_node_id": "source_01"}, ensure_ascii=False),
        ),
    ]

    class FakeExecResult:
        def all(self):
            return rows

    class FakeSession:
        async def exec(self, _stmt):
            return FakeExecResult()

    class FakeScope:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_project_state(project_id: str):
        return {}

    monkeypatch.setattr(node_universal, "session_scope", lambda: FakeScope())
    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)

    resolved, warnings = await node_universal._reference_images_for_media_run(
        "proj-1",
        {
            "references": [
                {"ref": "node:storyboard-image", "role": "visual_reference"},
                {"ref": "node:source-image", "role": "source_image"},
            ],
        },
    )

    assert resolved == ["node:storyboard-image"]
    assert warnings == []


@pytest.mark.asyncio
async def test_image_node_source_image_adopts_existing_output_without_generation(monkeypatch):
    async def fake_get_node(node_id: str):
        assert node_id == "source-image"
        return {
            "id": "source-image",
            "project_id": "proj-1",
            "type": "image",
            "status": "completed",
            "output": {
                "url": "/api/media/proj-1/source.png",
                "local_url": "/api/media/proj-1/source.png",
            },
        }

    async def fake_project_state(project_id: str):
        return {}

    async def should_not_generate(**kwargs):
        raise AssertionError("source_image should adopt an existing image without generation")

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)
    monkeypatch.setattr(node_universal.media_generation, "generate_image", should_not_generate)

    result = await node_universal._render_image_node(
        "proj-1",
        "target-image",
        {"references": [{"ref": "node:source-image", "role": "source_image"}]},
        "image",
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["source_mode"] == "direct_image"
    assert result["url"] == "/api/media/proj-1/source.png"
    assert result["images"][0]["source_node_id"] == "source-image"


@pytest.mark.asyncio
async def test_video_reference_resolver_maps_blueprint_ids_to_completed_image_nodes(monkeypatch):
    rows = [
        SimpleNamespace(
            id="image-node-1",
            title="宫格分镜图",
            type="image",
            status="completed",
            input_json=json.dumps({"blueprint_node_id": "storyboard_grid_01"}, ensure_ascii=False),
        ),
        SimpleNamespace(
            id="image-node-2",
            title="未完成角色图",
            type="image",
            status="idle",
            input_json=json.dumps({"blueprint_node_id": "character_mo_ying"}, ensure_ascii=False),
        ),
        SimpleNamespace(
            id="text-node-1",
            title="分段剧本",
            type="text",
            status="completed",
            input_json=json.dumps({"blueprint_node_id": "segment_01"}, ensure_ascii=False),
        ),
    ]

    class FakeExecResult:
        def all(self):
            return rows

    class FakeSession:
        async def exec(self, _stmt):
            return FakeExecResult()

    class FakeScope:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(node_universal, "session_scope", lambda: FakeScope())

    resolved, warnings = await node_universal._image_node_reference_images_for_video(
        "proj-1",
        ["@storyboard_grid_01", "@character_mo_ying", "@segment_01"],
    )

    assert resolved == ["node:image-node-1"]
    assert len(warnings) == 1
    assert "未完成角色图" in warnings[0]


@pytest.mark.asyncio
async def test_text_runner_preserves_tree_dependency_fields():
    result = await node_universal._run_text_node(
        "proj-1",
        "node-1",
        {
            "title": "故事设定",
            "content": "雨夜决斗。",
            "references": ["image-1"],
            "depends_on": ["text-0"],
        },
    )

    assert result == {
        "type": "text",
        "title": "故事设定",
        "content": "雨夜决斗。",
        "references": ["image-1"],
        "depends_on": ["text-0"],
    }


@pytest.mark.asyncio
async def test_node_list_returns_agent_safe_envelope(monkeypatch):
    async def fake_list_nodes(project_id: str):
        assert project_id == "proj-1"
        return [
            {"id": "n1", "type": "text", "status": "completed", "title": "brief"},
            {"id": "n2", "type": "image", "status": "idle", "title": "storyboard"},
        ]

    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)

    result = await node_universal.node_list(project_id="proj-1", type="image")

    assert result["ok"] is True
    assert result["total"] == 1
    assert result["returned"] == 1
    assert result["nodes"] == [
        {
            "id": "n2",
            "node_id": "n2",
            "type": "image",
            "status": "idle",
            "title": "storyboard",
            "prompt_preview": "",
        }
    ]
    assert result["filters"]["type"] == "image"


@pytest.mark.asyncio
async def test_image_creation_guide_exposes_skill_prompt_workflow(monkeypatch):
    patches: list[dict] = []

    async def fake_read_project_state(project_id: str):
        assert project_id == "proj-1"
        return {"project_mode": "single_node"}

    async def fake_write_project_state_patch(project_id: str, patch: dict):
        assert project_id == "proj-1"
        patches.append(patch)

    monkeypatch.setattr(node_universal, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(node_universal, "_write_project_state_patch", fake_write_project_state_patch)

    result = await node_universal.node_get_creation_guide(project_id="proj-1", type="image")

    assert result["ok"] is True
    assert "resolution" in result["required_fields"]
    assert "aspect_ratio" in result["required_fields"]
    assert result["call_example"]["args"]["fields"]["resolution"] == "2560x1440"
    assert "prompt_source" in result["optional_fields"]
    assert "prompt_template" not in result["optional_fields"]
    assert "template_selection_reason" not in result["optional_fields"]
    guidance_text = str(result["prompt_guidance"])
    assert "当前 skill" in guidance_text
    assert "最终图片 prompt" in guidance_text
    assert "精确像素" in guidance_text
    assert "2560x1440" in guidance_text
    assert "skill_or_model_written" in guidance_text
    assert "template.list" not in guidance_text
    assert patches[-1] == {"guide_loaded": {"image": True}}


@pytest.mark.asyncio
async def test_node_update_keeps_title_and_prompt_in_input_json(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "status": "completed",
            "title": "人物参考图",
            "prompt": "old prompt",
            "input": {
                "title": "人物参考图",
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        assert node_id == "image-1"
        updates.append(patch)
        return {
            "id": node_id,
            "type": "image",
            "status": "completed",
            "title": patch.get("title", "人物参考图"),
            "prompt": patch.get("prompt", "old prompt"),
            "input_json": patch.get("input_json", {}),
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        node_id="image-1",
        patch={
            "title": "人物参考图·一白一玄",
            "prompt": "两位女修士一白一玄，剑光更清晰。",
        },
    )

    assert result["title"] == "人物参考图·一白一玄"
    assert result["input_json"]["title"] == "人物参考图·一白一玄"
    assert result["input_json"]["prompt"] == "两位女修士一白一玄，剑光更清晰。"
    assert result["input_json"]["aspect_ratio"] == "16:9"
    assert updates == [
        {
            "title": "人物参考图·一白一玄",
            "prompt": "两位女修士一白一玄，剑光更清晰。",
                "input_json": {
                    "title": "人物参考图·一白一玄",
                    "prompt": "两位女修士一白一玄，剑光更清晰。",
                    "aspect_ratio": "16:9",
                    "render_state": "stale",
                },
            }
        ]
    assert result["render_state"] == "stale"
    assert result["requires_rerun"] is True


@pytest.mark.asyncio
async def test_node_update_fields_alias_merges_image_input_and_preserves_fields(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "image",
            "status": "failed",
            "title": "人物参考图",
            "prompt": "old prompt",
            "input": {
                "title": "人物参考图",
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "2K",
                "quality": "high",
                "references": [{"ref": "node:story-1", "role": "context"}],
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {
            "id": node_id,
            "status": "failed",
            "title": "人物参考图",
            "prompt": "old prompt",
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        node_id="image-1",
        patch={"fields": {"resolution": "2560x1440"}},
    )

    assert result["input_json"]["resolution"] == "2560x1440"
    assert result["input_json"]["prompt"] == "old prompt"
    assert result["input_json"]["quality"] == "high"
    assert result["input_json"]["references"] == [{"ref": "node:story-1", "role": "context"}]
    assert result["input"] == result["input_json"]
    assert updates == [
        {
            "input_json": {
                "title": "人物参考图",
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                    "quality": "high",
                    "references": [{"ref": "node:story-1", "role": "context"}],
                    "render_state": "stale",
                }
            }
        ]
    assert result["render_state"] == "stale"
    assert result["requires_rerun"] is True


@pytest.mark.asyncio
async def test_node_update_syncs_dependency_edges_from_fields(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "image",
            "status": "completed",
            "title": "蓝方拳手",
            "prompt": "old prompt",
            "input": {
                "title": "蓝方拳手",
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "depends_on": ["node:red-1"],
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "image",
            "status": "completed",
            "title": "蓝方拳手",
            "prompt": patch.get("prompt", "old prompt"),
            "input_json": patch.get("input_json", {}),
        }

    async def fake_sync_dependency_edges(project_id: str, target_node_id: str, input_data: dict):
        calls.append((project_id, target_node_id, input_data))
        return {
            "ok": True,
            "changed": True,
            "added_edges": [{"source_node_id": "script-1", "target_node_id": target_node_id}],
            "removed_edges": [{"source_node_id": "red-1", "target_node_id": target_node_id}],
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal.canvas_tools, "sync_dependency_edges", fake_sync_dependency_edges)

    result = await node_universal.node_update(
        node_id="image-1",
        patch={
            "fields": {
                "depends_on": ["node:script-1"],
                "references": [{"ref": "script-1", "role": "context"}],
            }
        },
    )

    assert calls == [
        (
            "proj-1",
            "image-1",
            {
                "title": "蓝方拳手",
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "depends_on": ["node:script-1"],
                "references": [{"ref": "script-1", "role": "context"}],
                "render_state": "stale",
            },
        )
    ]
    assert result["edge_sync"]["changed"] is True
    assert result["edge_sync"]["added_edges"][0]["source_node_id"] == "script-1"
    assert result["edge_sync"]["removed_edges"][0]["source_node_id"] == "red-1"


@pytest.mark.asyncio
async def test_node_update_rejects_invalid_image_resolution_patch(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "image",
            "status": "idle",
            "title": "场景图",
            "prompt": "old prompt",
            "input": {
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id}

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        node_id="image-1",
        patch={"fields": {"resolution": "2K"}},
    )

    assert result["ok"] is False
    assert result["error_kind"] == "invalid_resolution"
    assert updates == []


@pytest.mark.asyncio
async def test_node_run_recommends_review_without_blocking_render(monkeypatch):
    render_called = False

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "image",
            "status": "idle",
            "title": "人物图",
            "prompt": "人物设定图 prompt",
            "input": {
                "title": "人物图",
                "prompt": "人物设定图 prompt",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
            },
        }

    async def fake_read_project_state(project_id: str):
        return {"project_mode": "single_node"}

    async def fake_render(*args, **kwargs):
        nonlocal render_called
        render_called = True
        return {"url": "/api/media/proj-1/image.png"}

    async def fake_update_node(node_id: str, patch: dict):
        return {"id": node_id, **patch}

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(node_universal, "_render_image_node_once", fake_render)

    result = await node_universal.node_run(
        project_id="proj-1",
        node_id="image-1",
        action="render",
    )

    assert render_called is True
    assert result["ok"] is True
    assert result["review_recommended"] is True
    assert result["review_status"] == "review_recommended"
    assert result["recommended_tool"] == "agent.review"
    assert result["url"] == "/api/media/proj-1/image.png"


@pytest.mark.asyncio
async def test_video_creation_guide_exposes_skill_prompt_workflow(monkeypatch):
    patches: list[dict] = []

    async def fake_read_project_state(project_id: str):
        assert project_id == "proj-1"
        return {"project_mode": "video_production"}

    async def fake_write_project_state_patch(project_id: str, patch: dict):
        assert project_id == "proj-1"
        patches.append(patch)

    monkeypatch.setattr(node_universal, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(node_universal, "_write_project_state_patch", fake_write_project_state_patch)

    result = await node_universal.node_get_creation_guide(project_id="proj-1", type="video")

    assert result["ok"] is True
    assert "prompt_source" in result["optional_fields"]
    assert "production_path" in result["optional_fields"]
    assert "prompt_status" in result["optional_fields"]
    guidance_text = str(result["prompt_guidance"])
    assert "宫格分镜" in guidance_text
    assert "看图" in guidance_text
    assert "看不了图" in guidance_text
    assert "当前 skill" in guidance_text
    assert "最终 video prompt" in guidance_text
    assert "template.list" not in guidance_text
    assert patches[-1] == {"guide_loaded": {"video": True}}


@pytest.mark.asyncio
async def test_default_image_node_run_uses_image_render_timeout_budget(monkeypatch):
    monkeypatch.setattr(node_universal, "NODE_RUN_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(node_universal, "IMAGE_RENDER_TIMEOUT_SECONDS", 600)

    updates: list[dict] = []
    captured_timeouts: list[float | None] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "status": "idle",
            "title": "人物参考图",
            "prompt": "",
            "input": {
                "prompt": "cinematic portrait",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "prompt_review": {"status": "passed"},
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        assert node_id == "image-1"
        updates.append(patch)
        return {"id": node_id, **patch}

    async def fake_render(project_id: str, node_id: str, fields: dict, node_type: str):
        assert project_id == "proj-1"
        assert node_id == "image-1"
        assert node_type == "image"
        assert fields["prompt"] == "cinematic portrait"
        return {"ok": True, "url": "/storage/image.png"}

    async def fake_wait_for(coro, timeout=None):
        captured_timeouts.append(timeout)
        return await coro

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_render_image_node", fake_render)
    monkeypatch.setattr(node_universal, "_emit_fusion_canvas_event", fake_emit)
    monkeypatch.setattr(node_universal.asyncio, "wait_for", fake_wait_for)

    result = await node_universal.node_run(project_id="proj-1", node_id="image-1")

    assert result["node_id"] == "image-1"
    assert result["result"]["url"] == "/storage/image.png"
    assert captured_timeouts == [600]
    assert {"status": "running", "error_message": None} in updates
    assert updates[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_image_node_run_render_bypasses_stored_image_operation(monkeypatch):
    updates: list[dict] = []
    render_calls: list[dict] = []
    fusion_statuses: list[str] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "status": "completed",
            "title": "红方拳手",
            "prompt": "new boxer prompt",
            "input": {
                "prompt": "old prompt",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "quality": "standard",
                "operation": "grid_split",
                "grid": {"rows": 2, "cols": 2},
                "prompt_review": {"status": "passed"},
            },
            "output": {"type": "image_grid", "url": "/api/media/project/old-grid.png"},
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def fail_image_operation(project_id: str, node_id: str, fields: dict):
        raise AssertionError("action='render' should not rerun stored image operation")

    async def fake_merge(node_id: str, node_type: str, *, status: str, **kwargs):
        fusion_statuses.append(status)
        return {"type": "fusion", "subject": node_type, "stages": [{"name": "图片", "status": status, **kwargs}]}

    async def fake_render_once(project_id: str, node_id: str, fields: dict, node_type: str):
        render_calls.append(fields)
        return {
            "url": "/api/media/project/new.png",
            "local_url": "/api/media/project/new.png",
            "size": "1920x1080",
            "aspect_ratio": "16:9",
            "quality": "standard",
        }

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_run_image_node", fail_image_operation)
    monkeypatch.setattr(node_universal, "_merge_stage_into_fusion", fake_merge)
    monkeypatch.setattr(node_universal, "_render_image_node_once", fake_render_once)
    monkeypatch.setattr(node_universal, "_emit_fusion_canvas_event", fake_emit)

    result = await node_universal.node_run(
        project_id="proj-1",
        node_id="image-1",
        action="render",
    )

    assert result["ok"] is True
    assert result["action"] == "render"
    assert result["url"] == "/api/media/project/new.png"
    assert result["render_state"] == "fresh"
    assert render_calls and render_calls[0]["prompt"] == "new boxer prompt"
    assert fusion_statuses == ["running", "completed"]
    assert updates[-1]["input_data"]["render_state"] == "fresh"


@pytest.mark.asyncio
async def test_image_node_run_recovers_running_node_with_completed_output(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "type": "image",
            "status": "running",
            "title": "人物参考图",
            "prompt": "",
            "input": {
                "prompt": "cinematic portrait",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "prompt_review": {"status": "passed"},
            },
            "output": {
                "type": "fusion",
                "subject": "image",
                "stages": [
                    {
                        "name": "图片",
                        "status": "completed",
                        "url": "/api/media/project/image.png",
                    },
                ],
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def should_not_run(project_id: str, node_id: str, fields: dict):
        raise AssertionError("runner should not be called for completed running output")

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setitem(node_universal._RUNNERS, "image", should_not_run)

    result = await node_universal.node_run(project_id="proj-1", node_id="image-1")

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["url"] == "/api/media/project/image.png"
    assert result["recovered_from_running_output"] is True
    assert updates == [
        {
            "status": "completed",
            "error_message": None,
            "input_data": {
                "prompt": "cinematic portrait",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "prompt_review": {"status": "passed"},
                "render_state": "fresh",
            },
        }
    ]
    assert result["render_state"] == "fresh"


@pytest.mark.asyncio
async def test_running_fusion_stage_clears_stale_completed_url(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "output": {
                "type": "fusion",
                "subject": "image",
                "stages": [
                    {
                        "name": "图片",
                        "status": "completed",
                        "url": "/api/media/project/old.png",
                        "local_url": "/api/media/project/old.png",
                        "remote_url": "https://example.test/old.png",
                        "error": "old warning",
                    }
                ],
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        assert node_id == "image-1"
        updates.append(patch)
        return {"id": node_id, **patch}

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    fusion = await node_universal._merge_stage_into_fusion("image-1", "image", status="running")

    stage = fusion["stages"][0]
    assert stage["status"] == "running"
    assert "url" not in stage
    assert "local_url" not in stage
    assert "remote_url" not in stage
    assert "error" not in stage
    assert updates == [{"output_data": fusion}]


@pytest.mark.asyncio
async def test_node_run_marks_failed_when_async_generator_is_closed(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "type": "image",
            "status": "idle",
            "title": "人物参考图",
            "prompt": "",
            "input": {
                "prompt": "cinematic portrait",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "prompt_review": {"status": "passed"},
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def closing_render(project_id: str, node_id: str, fields: dict, node_type: str):
        raise GeneratorExit()

    async def fake_wait_for(coro, timeout=None):
        return await coro

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_render_image_node", closing_render)
    monkeypatch.setattr(node_universal, "_emit_fusion_canvas_event", fake_emit)
    monkeypatch.setattr(node_universal.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(GeneratorExit):
        await node_universal.node_run(project_id="proj-1", node_id="image-1")

    assert {"status": "running", "error_message": None} in updates
    assert updates[-1]["status"] == "failed"
    assert "连接中断" in updates[-1]["error_message"]
