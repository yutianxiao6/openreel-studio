import json

from app.agent.model_context import artifact_store
from app.agent.model_context.policy import GLOBAL_MODEL_ITEM_MAX_TOKENS
from app.agent.model_context.types import ToolContentPart, ToolOutput
from app.agent.tool_output import (
    build_tool_output_envelope,
    tool_done_event,
    tool_result_context_messages,
    tool_result_input_item,
    tool_trace_fields,
)


def _isolate_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(artifact_store, "tool_results_dir", lambda: tmp_path)


def test_small_result_stays_inline_with_v2_contract(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    result = {"ok": True, "value": "small"}

    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=1,
        tool_name="tool.small",
    )

    observation = json.loads(envelope["model_visible"]["content"])
    assert envelope["version"] == "tool_output_v2"
    assert envelope["raw_artifact"] is None
    assert observation["tool_observation_version"] == "tool_observation_v2"
    assert observation["result"] == result
    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS
    item = tool_result_input_item("call-1", envelope)
    assert item == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": envelope["model_visible"]["content"],
    }
    assert tool_done_event("tool.small", 2, envelope)["result"] == result
    assert tool_trace_fields(envelope)["tool_result_compacted"] is False


def test_document_policy_keeps_one_8000_character_cjk_page_complete(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    page_text = "正" * 8_000
    result = {
        "id": "0",
        "type": "text",
        "content_page": {
            "content": page_text,
            "offset": 0,
            "limit": 8_000,
            "returned_chars": 8_000,
            "total_chars": 20_000,
            "next_offset": 8_000,
        },
    }

    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=1,
        tool_name="node.get",
    )

    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["result"]["content_page"]["content"] == page_text
    assert observation["result"]["content_page"]["next_offset"] == 8_000
    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS


def test_document_policy_reserves_large_string_budget_for_resumable_pages(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    nested_text = "非分页正文" * 10_000
    result = {
        "id": "0",
        "type": "text",
        "content_page": {
            "content": "",
            "offset": 0,
            "limit": 8_000,
            "returned_chars": 0,
            "total_chars": 0,
            "next_offset": None,
        },
        "input": {"fields": {"content": nested_text}},
    }

    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=1,
        tool_name="node.get",
    )

    observation = json.loads(envelope["model_visible"]["content"])
    visible_nested = observation["result"]["input"]["fields"]["content"]
    assert visible_nested != nested_text
    assert "tokens omitted" in visible_nested
    assert envelope["raw_artifact"] is not None
    assert envelope["model_visible"]["tokens"] < 1_000
    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS


def test_large_result_uses_opaque_artifact_ref_and_bounded_ui(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    result = {"ok": True, "items": ["x" * 1_000 for _ in range(100)]}

    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=3,
        tool_name="tool.large",
    )

    observation = json.loads(envelope["model_visible"]["content"])
    artifact = envelope["raw_artifact"]
    assert artifact["ref"].startswith("tool-result:")
    assert "path" not in artifact
    assert observation["artifact_ref"] == artifact["ref"]
    assert envelope["ui"]["result"]["tool_result_compacted"] is True
    event = tool_done_event("tool.large", 4, envelope)
    assert event["tool_output"]["artifact_ref"] == artifact["ref"]
    assert "path" not in event["tool_output"]
    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_large_error_is_bounded_without_duplicate_model_feedback(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    marker = "UNIQUE_FAILURE_MARKER"
    result = {
        "ok": False,
        "error": marker + ("错" * 100_000),
        "error_kind": "node_not_found",
        "hint": "先读取真实节点状态" + ("提示" * 50_000),
        "suggested_next": "read_state",
    }

    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=4,
        tool_name="node.get",
    )

    rendered = envelope["model_visible"]["content"]
    observation = json.loads(rendered)
    assert observation["outcome"] == "recoverable_error"
    assert observation["next_action"] == "read_state"
    assert rendered.count(marker) == 1
    assert "model_feedback" not in rendered
    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS


def test_boundary_redacts_secret_keys_and_image_data_from_json(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    data_url = "data:image/png;base64,SECRET_IMAGE_BYTES"

    envelope = build_tool_output_envelope(
        {
            "ok": True,
            "api_key": "sk-secret",
            "headers": {"Authorization": "Bearer secret"},
            "image": data_url,
        },
        project_id="project",
        run_id="run",
        iteration=1,
        tool_name="tool.sensitive",
    )

    rendered = envelope["model_visible"]["content"]
    assert "sk-secret" not in rendered
    assert "Bearer secret" not in rendered
    assert "SECRET_IMAGE_BYTES" not in rendered
    assert rendered.count("[REDACTED]") >= 2


def test_typed_multimodal_parts_are_separate_and_capped(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    parts = [ToolContentPart.text_part("visual evidence")]
    parts.extend(
        ToolContentPart.image_part(f"data:image/png;base64,IMAGE_{index}", detail="high")
        for index in range(20)
    )

    envelope = build_tool_output_envelope(
        ToolOutput(
            value={"ok": True, "image_count": 20},
            content_parts=tuple(parts),
            contains_external_context=True,
        ),
        project_id="project",
        run_id="run",
        iteration=2,
        tool_name="vision.view_image",
    )

    assert "IMAGE_0" not in envelope["model_visible"]["content"]
    provider_parts = envelope["model_visible"]["content_parts"]
    assert sum(part.get("type") == "image_url" for part in provider_parts) == 8
    context_messages = tool_result_context_messages("call-vision", envelope)
    assert len(context_messages) == 1
    assert context_messages[0]["content"] == provider_parts
    assert json.loads(envelope["model_visible"]["content"])["contains_external_context"] is True
    assert envelope["trace"]["tool_result_contains_external_context"] is True
    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS


def test_requested_budget_never_exceeds_global_hard_limit(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    envelope = build_tool_output_envelope(
        {"ok": True, "content": "甲" * 100_000},
        project_id="project",
        run_id="run",
        iteration=5,
        tool_name="node.get",
        requested_tokens=1_000_000,
    )

    assert envelope["model_visible"]["tokens"] <= GLOBAL_MODEL_ITEM_MAX_TOKENS
    json.loads(envelope["model_visible"]["content"])


def test_artifact_retention_is_bounded_per_run(tmp_path, monkeypatch) -> None:
    _isolate_artifacts(tmp_path, monkeypatch)
    monkeypatch.setattr(artifact_store, "MAX_TOOL_RESULT_FILES_PER_RUN", 3)
    monkeypatch.setattr(artifact_store, "MAX_TOOL_RESULT_BYTES_PER_RUN", 10_000_000)

    for iteration in range(6):
        artifact_store.save_tool_result(
            {"iteration": iteration},
            project_id="project",
            run_id="run",
            iteration=iteration,
            tool_name="tool.test",
        )

    assert len(list((tmp_path / "project" / "run").glob("*.json"))) <= 3
