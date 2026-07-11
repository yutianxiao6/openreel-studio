from app.mcp_tools.workflow_runtime_output import (
    structured_workflow_output,
    workflow_runtime_clean_output_value,
    workflow_runtime_output_from_runner_payload,
    workflow_runtime_output_preview,
    workflow_runtime_outputs_from_value,
)


def test_structured_output_extracts_json_from_fenced_or_wrapped_content() -> None:
    fenced = '说明\n```json\n{"segments":[{"title":"开场"}]}\n```'
    assert structured_workflow_output(fenced) == {"segments": [{"title": "开场"}]}
    assert structured_workflow_output({"content": '{"shots":[1,2]}', "status": "ok"}) == {
        "content": '{"shots":[1,2]}',
        "status": "ok",
        "shots": [1, 2],
    }


def test_runner_output_keeps_content_or_media_and_drops_internal_diagnostics() -> None:
    text_output = workflow_runtime_output_from_runner_payload({
        "ok": True,
        "content": '{"segments":[{"title":"开场"}]}',
        "usage": {"total_tokens": 100},
        "prompt_dump_run_id": "private-debug-id",
    })
    media_output = workflow_runtime_clean_output_value({
        "ok": True,
        "status": "completed",
        "url": "/api/media/proj/image.png",
        "model": "provider-model",
        "usage": {"total_tokens": 100},
    })

    assert text_output == {"segments": [{"title": "开场"}]}
    assert media_output == {"url": "/api/media/proj/image.png"}


def test_runtime_outputs_have_stable_name_type_and_clean_value() -> None:
    outputs = workflow_runtime_outputs_from_value(
        {"title": "第一集", "segments": [{"title": "开场"}]},
        name="script_plan",
    )

    assert outputs == [{
        "name": "script_plan",
        "type": "json",
        "value": {"title": "第一集", "segments": [{"title": "开场"}]},
    }]


def test_runtime_output_preview_applies_schema_labels_and_hides_diagnostics() -> None:
    preview = workflow_runtime_output_preview(
        {
            "output": {
                "segments": [{"index": 1, "script": "雨夜开场", "usage": {"total_tokens": 99}}],
                "approved": False,
            },
        },
        workflow_override={
            "output_schema": {
                "properties": {
                    "script": {"title": "剧情"},
                    "approved": {"title": "已通过"},
                },
            },
        },
    )

    assert preview == "分段:\n  - 雨夜开场\n已通过: 否"
    assert "tokens" not in preview


def test_runtime_output_preview_respects_character_limit() -> None:
    preview = workflow_runtime_output_preview({"output": {"content": "长" * 20}}, limit=8)

    assert preview == f"{'长' * 8}\n...（已截断）"
