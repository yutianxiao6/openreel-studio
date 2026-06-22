from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from app.agent import context_compact, prompt_dump, vision_context
from app.agent.tool_output import (
    build_tool_output_envelope,
    tool_result_messages,
    tool_trace_fields,
)
from app.agent.vision_context import (
    VISION_METADATA_KEY,
    apply_vision_context_to_latest_user,
    attach_vision_metadata,
    build_vision_context,
    build_vision_context_from_metadata,
    vision_metadata_payload,
)
from app.agent.orchestrator import AgentOrchestrator
from app.db.models import WorkflowNode
from app.mcp_tools import vision_tools


def _write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path, format="PNG")


@pytest.mark.asyncio
async def test_upload_images_inject_as_temporary_multimodal_context(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-vision"
    attachments = []
    for index in range(10):
        rel_path = f"uploads/ref-{index}.png"
        _write_image(storage / project_id / rel_path, (index * 20 % 255, 20, 40))
        attachments.append({
            "kind": "image",
            "rel_path": rel_path,
            "filename": f"ref-{index}.png",
            "mention": f"@图{index + 1}",
        })

    class NoNodeDB:
        async def exec(self, statement):  # pragma: no cover - should not be called
            raise AssertionError("attachment-triggered context should not query image nodes")

    context = await build_vision_context(
        NoNodeDB(),
        project_id,
        "按这些参考图继续",
        attachments,
        max_images=8,
        max_dimension=512,
    )

    messages = [{"role": "assistant", "content": "旧回复"}, {"role": "user", "content": "按这些参考图继续"}]
    apply_vision_context_to_latest_user(messages, "按这些参考图继续", context)

    assert context.injected_count == 8
    assert context.omitted_count == 2
    assert messages[0] == {"role": "assistant", "content": "旧回复"}
    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert len(image_parts) == 8
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "data:image/" not in json.dumps(context.trace_payload())


@pytest.mark.asyncio
async def test_explicit_node_references_inject_completed_image_nodes(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-nodes"
    nodes: list[WorkflowNode] = []
    for index in range(3):
        filename = f"node-{index}.png"
        _write_image(storage / project_id / "generated_images" / filename, (40, index * 60 % 255, 90))
        nodes.append(WorkflowNode(
            project_id=project_id,
            type="image",
            title=f"分镜图 {index + 1}",
            status="completed",
            output_json=json.dumps({"url": f"/api/media/{project_id}/{filename}"}),
        ))

    class FakeResult:
        def all(self):
            return nodes

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    context = await build_vision_context(
        FakeDB(),
        project_id,
        "你看第四格和第五格的远近关系",
        [],
        referenced_node_ids=[nodes[1].id, nodes[0].id],
        max_images=2,
        max_dimension=512,
    )

    assert context.triggered is True
    assert context.trigger_reason == "explicit_node_reference"
    assert context.injected_count == 2
    assert [image.source_kind for image in context.images] == ["node", "node"]
    assert [image.node_id for image in context.images] == [nodes[1].id, nodes[0].id]
    assert all(image.node_id for image in context.images)


@pytest.mark.asyncio
async def test_visual_words_do_not_trigger_image_injection_without_structured_reference() -> None:
    class NoNodeDB:
        async def exec(self, statement):  # pragma: no cover - should not be called
            raise AssertionError("natural-language visual words must not query image nodes")

    context = await build_vision_context(
        NoNodeDB(),
        "project-no-keywords",
        "你看第四格和第五格的远近关系",
        [],
        max_images=8,
        max_dimension=512,
    )

    assert context.triggered is False
    assert context.trigger_reason == "not_visual"
    assert context.injected_count == 0


def test_prompt_dump_and_compaction_redact_image_data_urls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_ENABLED", "1")
    monkeypatch.setattr(prompt_dump, "_DUMP_ROOT", tmp_path)
    data_url = "data:image/png;base64," + ("A" * 5000)
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]

    prompt_dump.dump_llm_request(
        project_id="project-1",
        run_id="run-vision",
        iteration=0,
        system="system",
        messages=messages,
        tools=[],
        user_message="看这张图",
    )
    dumped = (tmp_path / "project-1" / "run-vision.jsonl").read_text(encoding="utf-8")
    assert data_url not in dumped
    assert "<image data URL omitted" in dumped
    assert data_url not in json.dumps(vision_context.redact_image_data_urls({"source": data_url}))

    summary_prompt = context_compact.build_compact_summary_prompt(messages)
    assert data_url not in summary_prompt
    transcript = context_compact.save_transcript(messages, project_id="project-1")
    assert data_url not in transcript.read_text(encoding="utf-8")
    assert context_compact.estimate_tokens(messages) >= vision_context.DEFAULT_IMAGE_TOKEN_ESTIMATE


@pytest.mark.asyncio
async def test_vision_metadata_rehydrates_images_from_database_references(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-vision-metadata"
    _write_image(storage / project_id / "uploads" / "history.png", (10, 80, 160))

    context = await build_vision_context(
        None,
        project_id,
        "看这张图",
        [{"kind": "image", "rel_path": "uploads/history.png", "filename": "history.png"}],
        max_images=8,
        max_dimension=512,
    )
    payload = vision_metadata_payload(context, source="user_message")
    metadata = attach_vision_metadata({}, payload)

    rehydrated = await build_vision_context_from_metadata(
        project_id,
        metadata,
        max_images=8,
        max_dimension=512,
    )

    assert rehydrated.injected_count == 1
    assert rehydrated.images[0].source == "uploads/history.png"
    assert rehydrated.images[0].image_url.startswith("data:image/jpeg;base64,")
    assert "data:image/" not in json.dumps(metadata, ensure_ascii=False)
    assert context_compact.estimate_tokens([
        {"role": "user", "content": "看这张图", "_metadata": metadata},
    ]) >= vision_context.DEFAULT_IMAGE_TOKEN_ESTIMATE


@pytest.mark.asyncio
async def test_build_messages_rehydrates_persisted_user_and_tool_vision_context(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-build-history-vision"
    _write_image(storage / project_id / "uploads" / "user.png", (220, 40, 40))
    _write_image(storage / project_id / "uploads" / "tool.png", (40, 220, 40))

    user_metadata = {
        VISION_METADATA_KEY: {
            "version": 1,
            "source": "user_message",
            "images": [{"label": "@图1", "source_kind": "attachment", "source": "uploads/user.png"}],
        }
    }
    assistant_metadata = {
        VISION_METADATA_KEY: {
            "version": 1,
            "source": "vision.view_image",
            "tool_name": "vision.view_image",
            "images": [{"label": "image:1", "source_kind": "source", "source": "uploads/tool.png"}],
        }
    }

    class FakeResult:
        def all(self):
            return [
                type("Row", (), {
                    "role": "assistant",
                    "content": "我看了图。",
                    "metadata_json": json.dumps(assistant_metadata, ensure_ascii=False),
                })(),
                type("Row", (), {
                    "role": "user",
                    "content": "请看图",
                    "metadata_json": json.dumps(user_metadata, ensure_ascii=False),
                })(),
            ]

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages(
        project_id,
        "继续",
        include_history=True,
        max_images=8,
        max_dimension=512,
    )

    assert messages[0]["role"] == "user"
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][1]["type"] == "image_url"
    assert messages[1] == {"role": "assistant", "content": "我看了图。"}
    assert messages[2]["role"] == "user"
    assert messages[2]["_persisted_vision_context"] is True
    assert messages[2]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert messages[-1] == {"role": "user", "content": "继续"}


@pytest.mark.asyncio
async def test_vision_view_image_returns_multimodal_tool_context(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-view-image"
    _write_image(storage / project_id / "uploads" / "frame.png", (120, 40, 80))

    result = await vision_tools.view_image(
        project_id=project_id,
        source="uploads/frame.png",
    )

    assert result["ok"] is True
    assert result["status"] == "image_attached"
    assert result["image_count"] == 1
    assert result["_vision_context_refs"][0]["source"] == "uploads/frame.png"
    model_content = result["_model_content"]
    assert model_content[0]["type"] == "text"
    assert model_content[1]["type"] == "image_url"
    assert model_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    envelope = build_tool_output_envelope(
        result,
        project_id=project_id,
        run_id="run-view-image",
        iteration=1,
        tool_name="vision.view_image",
    )
    observation_text = envelope["model_visible"]["content"]
    assert "data:image/" not in observation_text
    assert envelope["model_visible"]["content_parts"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert "data:image/" not in json.dumps(tool_trace_fields(envelope), ensure_ascii=False)

    messages = tool_result_messages("call-view-image", envelope)
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "call-view-image"
    assert messages[1]["role"] == "user"
    assert messages[1]["_tool_image_context"] is True
    assert messages[1]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_vision_view_image_supports_batch_sources(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-view-image-batch"
    for index in range(3):
        _write_image(storage / project_id / "uploads" / f"frame-{index}.png", (40 + index, 80, 120))

    result = await vision_tools.view_image(
        project_id=project_id,
        sources=["uploads/frame-0.png", "uploads/frame-1.png", "uploads/frame-2.png"],
        max_images=2,
    )

    assert result["ok"] is True
    assert result["image_count"] == 2
    assert result["omitted_count"] == 1
    image_parts = [part for part in result["_model_content"] if part.get("type") == "image_url"]
    assert len(image_parts) == 2
    assert len(result["_vision_context_refs"]) == 2
    assert "data:image/" not in json.dumps(result["_vision_context_refs"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_vision_view_image_loads_completed_image_node(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(vision_context.settings, "STORAGE_PATH", str(storage))
    monkeypatch.setattr(vision_context.settings, "STORAGE_DIR", str(storage))

    project_id = "project-view-node"
    _write_image(storage / project_id / "generated_images" / "storyboard.png", (20, 140, 90))
    node = WorkflowNode(
        id="image-node-1",
        project_id=project_id,
        type="image",
        title="分镜图",
        status="completed",
        output_json=json.dumps({"url": f"/api/media/{project_id}/storyboard.png"}),
    )

    class FakeSession:
        async def get(self, model, node_id):
            assert model is WorkflowNode
            assert node_id == "image-node-1"
            return node

    class FakeScope:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(vision_tools, "session_scope", lambda: FakeScope())

    result = await vision_tools.view_image(
        project_id=project_id,
        node_id="image-node-1",
    )

    assert result["ok"] is True
    assert result["node_id"] == "image-node-1"
    assert result["title"] == "分镜图"
    assert result["_model_content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_micro_compact_preserves_tool_image_contexts_for_stable_history() -> None:
    data_url = "data:image/png;base64," + ("A" * 1000)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"tool image {index}"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            "_tool_image_context": True,
        }
        for index in range(context_compact.KEEP_RECENT_TOOL_RESULTS + 1)
    ]

    context_compact.micro_compact(messages)

    assert all(isinstance(message["content"], list) for message in messages)
    assert all(not message.get("_tool_image_context_compacted") for message in messages)
