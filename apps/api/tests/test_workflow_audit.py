import pytest

from app.agent import workflow_spec_artifacts
from app.agent.workflow_audit import audit_workflow_spec
from app.mcp_tools import workflow_tools


def _finding_codes(report: dict) -> set[str]:
    return {str(item.get("code") or "") for item in report.get("findings") or [] if isinstance(item, dict)}


def test_workflow_audit_passes_valid_canvas_media_source() -> None:
    report = audit_workflow_spec(
        {
            "id": "valid_video_flow",
            "name": "有效视频流程",
            "inputs": [{"id": "plot", "type": "long_text"}],
            "required_inputs": ["plot"],
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text", "runner": "workflow_input"},
                {
                    "id": "video_prompt",
                    "title": "视频提示词",
                    "node_type": "text",
                    "depends_on": ["input"],
                    "prompt_template": "根据 {{input.output}} 写视频提示词。",
                },
                {
                    "id": "final_video",
                    "title": "成片",
                    "node_type": "video",
                    "runner": "workflow_canvas_output",
                    "surface": "draft_canvas",
                    "visibility": "canvas",
                    "depends_on": ["video_prompt"],
                    "fields": {
                        "workflow_source_step": "video_prompt",
                        "workflow_source_path": "output",
                    },
                },
            ],
        }
    )

    assert report["ok"] is True
    assert report["can_save"] is True
    assert report["can_run"] is True
    assert report["visible_output_count"] == 1
    assert _finding_codes(report) == set()


def test_workflow_audit_blocks_duplicate_raw_step_ids() -> None:
    report = audit_workflow_spec(
        {
            "id": "duplicate_flow",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {"id": "Script", "title": "重复剧本", "node_type": "text"},
            ],
        }
    )

    assert report["ok"] is False
    assert "duplicate_step_id" in _finding_codes(report)


def test_workflow_audit_blocks_unknown_prompt_placeholder() -> None:
    report = audit_workflow_spec(
        {
            "id": "bad_prompt_ref_flow",
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "depends_on": ["input"],
                    "prompt_template": "根据 {{missing.output}} 写剧本。",
                },
            ],
        }
    )

    assert report["ok"] is False
    assert "unknown_step_ref" in _finding_codes(report)


def test_workflow_audit_blocks_required_input_without_schema() -> None:
    report = audit_workflow_spec(
        {
            "id": "missing_input_schema_flow",
            "required_inputs": ["plot"],
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
            ],
        }
    )

    assert report["ok"] is False
    assert "required_input_missing_schema" in _finding_codes(report)


def test_workflow_audit_blocks_canvas_media_without_source() -> None:
    report = audit_workflow_spec(
        {
            "id": "bad_canvas_source_flow",
            "steps": [
                {
                    "id": "final_video",
                    "title": "成片",
                    "node_type": "video",
                    "runner": "workflow_canvas_output",
                    "surface": "draft_canvas",
                    "visibility": "canvas",
                },
            ],
        }
    )

    assert report["ok"] is False
    assert "canvas_output_missing_source" in _finding_codes(report)


def test_workflow_audit_blocks_workflow_source_step_not_upstream() -> None:
    report = audit_workflow_spec(
        {
            "id": "late_source_flow",
            "steps": [
                {
                    "id": "final_video",
                    "title": "成片",
                    "node_type": "video",
                    "runner": "workflow_canvas_output",
                    "surface": "draft_canvas",
                    "visibility": "canvas",
                    "fields": {"workflow_source_step": "video_prompt"},
                },
                {"id": "video_prompt", "title": "视频提示词", "node_type": "text"},
            ],
        }
    )

    assert report["ok"] is False
    assert "reference_not_upstream" in _finding_codes(report)


@pytest.mark.asyncio
async def test_workflow_spec_commit_returns_audit_report_for_unknown_prompt_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    start = await workflow_tools.workflow_spec_start(
        project_id="proj-audit",
        workflow={"id": "bad_prompt_ref_flow", "name": "坏引用流程"},
    )
    await workflow_tools.workflow_spec_append_steps(
        project_id="proj-audit",
        draft_id=start["draft_id"],
        steps=[
            {"id": "input", "title": "输入", "node_type": "text"},
            {
                "id": "script",
                "title": "剧本",
                "node_type": "text",
                "depends_on": ["input"],
                "prompt_template": "根据 {{missing.output}} 写剧本。",
            },
        ],
    )

    result = await workflow_tools.workflow_spec_commit(
        project_id="proj-audit",
        draft_id=start["draft_id"],
        self_check={"passed": True, "checks": ["模型误判通过"], "issues": []},
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_audit_failed"
    assert "unknown_step_ref" in _finding_codes(result["audit"])
    assert not list((tmp_path / "proj-audit" / "workflow_specs").glob("*.json"))
