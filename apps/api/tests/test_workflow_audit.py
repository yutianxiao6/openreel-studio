from copy import deepcopy

from app.agent.workflow_audit import audit_workflow_spec


def _base_workflow() -> dict:
    return {
        "schema": "openreel.workflow.v2",
        "id": "audited_video_flow",
        "title": "审计视频流程",
        "inputs": {
            "plot": {"type": "long_text", "label": "剧情", "required": True},
        },
        "steps": [
            {
                "id": "script",
                "title": "剧本",
                "kind": "text",
                "prompt": {"task": "根据 {{ inputs.plot }} 写剧本。"},
                "output": {"canvas": True},
            },
            {
                "id": "final_video",
                "title": "成片",
                "kind": "video",
                "prompt": {"task": "根据 {{ steps.script.output }} 写视频提示词。"},
            },
        ],
    }


def _finding_codes(report: dict) -> set[str]:
    return {str(item.get("code") or "") for item in report.get("findings") or [] if isinstance(item, dict)}


def test_workflow_audit_passes_valid_v2_and_identifies_leaf_output() -> None:
    report = audit_workflow_spec(_base_workflow())

    assert report["ok"] is True
    assert report["can_save"] is True
    assert report["can_run"] is True
    assert report["dry_run"]["visible_output_ids"] == ["script", "final_video"]
    assert report["dry_run"]["final_output_ids"] == ["final_video"]
    assert _finding_codes(report) == set()


def test_workflow_audit_blocks_unknown_prompt_path() -> None:
    workflow = _base_workflow()
    workflow["steps"][1]["prompt"]["task"] = "根据 {{ steps.missing.output }} 写视频提示词。"
    report = audit_workflow_spec(workflow)

    assert report["ok"] is False
    assert report["can_save"] is False
    assert _finding_codes(report) == {"workflow_spec_invalid"}


def test_workflow_audit_blocks_deleted_v1_fields() -> None:
    workflow = _base_workflow()
    workflow["steps"][0]["prompt_template"] = "旧字段"
    report = audit_workflow_spec(workflow)

    assert report["ok"] is False
    assert _finding_codes(report) == {"workflow_spec_invalid"}


def test_workflow_audit_blocks_cycle_before_runtime_changes() -> None:
    workflow = _base_workflow()
    workflow["steps"][0]["needs"] = ["final_video"]
    report = audit_workflow_spec(workflow)

    assert report["ok"] is False
    assert _finding_codes(report) == {"workflow_spec_invalid"}


def test_workflow_audit_is_deterministic() -> None:
    first = audit_workflow_spec(_base_workflow())
    second = audit_workflow_spec(deepcopy(_base_workflow()))

    assert first["protocol"]["plan_hash"] == second["protocol"]["plan_hash"]
    assert first["dry_run"] == second["dry_run"]
