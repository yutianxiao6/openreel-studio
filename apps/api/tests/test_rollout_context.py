from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.orchestrator import AgentOrchestrator
from app.agent.rollout_context import (
    decode_persisted_rollout,
    encode_compaction_checkpoint,
    encode_persisted_rollout,
    rollout_context_messages,
)
from app.db.models import Message


def _typed_items() -> list[dict]:
    return [
        {
            "id": "reasoning-1",
            "type": "reasoning",
            "encrypted_content": "encrypted-reasoning",
        },
        {
            "id": "call-item-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "project__get_state",
            "arguments": '{"project_id":"project-1"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"ok":true}',
        },
        {
            "id": "message-1",
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "状态正常。"}],
        },
    ]


def test_persisted_rollout_round_trips_typed_responses_items() -> None:
    encoded = encode_persisted_rollout(_typed_items(), "状态正常。")
    decoded = decode_persisted_rollout(encoded)

    assert decoded is not None
    assert list(decoded.items) == _typed_items()
    assert decoded.append_assistant_text == ""
    assert decoded.original_tokens >= decoded.persisted_tokens > 0


def test_compaction_checkpoint_preserves_opaque_item_and_redacts_image_bytes() -> None:
    data_url = "data:image/png;base64,SECRET"
    encoded = encode_compaction_checkpoint([
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": data_url},
            ],
        },
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"},
    ])
    decoded = decode_persisted_rollout(encoded)

    assert decoded is not None
    assert data_url not in encoded
    assert list(decoded.items)[-1] == {
        "type": "compaction",
        "id": "cmp_1",
        "encrypted_content": "opaque",
    }


def test_persisted_rollout_appends_only_backend_generated_assistant_suffix() -> None:
    encoded = encode_persisted_rollout(_typed_items(), "状态正常。请确认是否继续。")

    messages = rollout_context_messages(encoded)

    assert messages[:-1] == _typed_items()
    assert messages[-1] == {"role": "assistant", "content": "请确认是否继续。"}


def test_message_public_dump_excludes_private_model_context() -> None:
    message = Message(
        project_id="project-1",
        role="assistant",
        content="完成",
        model_context_json=encode_persisted_rollout(_typed_items(), "状态正常。"),
    )

    assert "model_context_json" not in message.model_dump()


@pytest.mark.asyncio
async def test_build_messages_replays_typed_rollout_across_user_turns() -> None:
    encoded = encode_persisted_rollout(_typed_items(), "状态正常。")
    rows = [
        SimpleNamespace(
            role="user",
            content="检查项目",
            metadata_json=None,
            model_context_json=None,
        ),
        SimpleNamespace(
            role="assistant",
            content="状态正常。",
            metadata_json=None,
            model_context_json=encoded,
        ),
    ]

    class FakeResult:
        def all(self):
            return list(reversed(rows))

    class FakeDB:
        async def exec(self, _statement):
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages("project-1", "继续")

    assert messages[0] == {"role": "user", "content": "检查项目"}
    assert messages[1:5] == _typed_items()
    assert messages[-1] == {"role": "user", "content": "继续"}


@pytest.mark.asyncio
async def test_build_messages_replays_developer_compaction_checkpoint() -> None:
    checkpoint_items = [
        {"role": "user", "content": "最近任务"},
        {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque"},
    ]
    rows = [
        SimpleNamespace(
            role="developer",
            content="internal checkpoint",
            metadata_json=None,
            model_context_json=encode_compaction_checkpoint(checkpoint_items),
        ),
    ]

    class FakeResult:
        def all(self):
            return list(reversed(rows))

    class FakeDB:
        async def exec(self, _statement):
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages("project-1", "继续")

    assert messages[:2] == checkpoint_items
    assert messages[-1] == {"role": "user", "content": "继续"}
