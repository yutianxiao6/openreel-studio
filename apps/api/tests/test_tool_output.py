import json

from app.agent import context_compact
from app.agent.tool_output import (
    build_tool_output_envelope,
    tool_done_event,
    tool_result_message,
    tool_trace_fields,
)


def test_tool_output_envelope_keeps_small_result_inline(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {"ok": True, "value": "small"}
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=1,
        tool_name="tool.small",
        budget_chars=1000,
    )

    assert envelope["version"] == "tool_output_v1"
    assert envelope["raw_artifact"] is None
    assert envelope["ui"]["result"] == result
    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["tool_observation_version"] == "tool_observation_v1"
    assert observation["success"] is True
    assert observation["outcome"] == "success"
    assert observation["handler_ok"] is True
    assert observation["result"] == result
    assert tool_result_message("call-1", envelope)["content"] == envelope["model_visible"]["content"]
    event = tool_done_event("tool.small", 2, envelope)
    assert event["result"] == result
    assert event["tool_output"]["success"] is True
    assert event["tool_output"]["outcome"] == "success"
    assert event["tool_output"]["compacted"] is False
    trace = tool_trace_fields(envelope)
    assert trace["tool_result_compacted"] is False
    assert trace["tool_observation_version"] == "tool_observation_v1"
    assert trace["tool_result_success"] is True
    assert trace["tool_result_raw_chars"] >= len('{"ok":true}')


def test_tool_output_envelope_preserves_success_model_feedback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": True,
        "status": "created",
        "suggested_next": "continue_node_setup_or_run",
        "model_feedback": {
            "what_went_wrong": "节点已创建但还未运行。",
            "how_to_fix": "继续 node.update 补字段，或在输入就绪后调用 node.run。",
            "suggested_next": "continue_node_setup_or_run",
        },
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=2,
        tool_name="node.create",
        budget_chars=1000,
    )

    assert envelope["success"] is True
    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["success"] is True
    assert observation["next_action"] == "continue_node_setup_or_run"
    assert observation["model_feedback"]["suggested_next"] == "continue_node_setup_or_run"
    assert "node.run" in observation["model_feedback"]["how_to_fix"]


def test_tool_output_envelope_splits_large_raw_artifact_from_ui_and_model_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {"ok": True, "items": ["x" * 50 for _ in range(20)]}
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=3,
        tool_name="tool.large",
        budget_chars=120,
    )

    assert envelope["model_visible"]["compacted"] is True
    assert envelope["raw_artifact"]["path"]
    assert envelope["ui"]["result"]["tool_result_compacted"] is True
    assert envelope["ui"]["result"]["full_result_path"] == envelope["raw_artifact"]["path"]
    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["success"] is True
    assert observation["result"]["tool_result_compacted"] is True
    assert observation["result"]["full_result_path"] == envelope["raw_artifact"]["path"]
    written = list(tmp_path.rglob("*.json"))
    assert len(written) == 1
    assert '"items"' in written[0].read_text(encoding="utf-8")

    event = tool_done_event("tool.large", 4, envelope)
    assert event["result"]["tool_result_compacted"] is True
    assert event["tool_output"]["artifact_path"] == envelope["raw_artifact"]["path"]
    trace = tool_trace_fields(envelope)
    assert trace["tool_result_compacted"] is True
    assert trace["tool_result_artifact_path"] == envelope["raw_artifact"]["path"]


def test_node_list_compacted_summary_preserves_node_index_for_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    prompt = "12345678901234567890extra"
    result = {
        "ok": True,
        "project_id": "project",
        "total": 2,
        "returned": 2,
        "truncated": False,
        "next_action": "需要节点详情时批量调用 node.get(node_ids=[...])。",
        "filters": {"limit": 20},
        "nodes": [
            {
                "id": "image-1",
                "node_id": "image-1",
                "type": "image",
                "title": "角色参考图",
                "status": "failed",
                "prompt": prompt,
                "error_message": "provider failed",
                "output": {"large": "x" * 400},
            },
            {
                "id": "video-1",
                "node_id": "video-1",
                "type": "video",
                "title": "最终视频",
                "status": "idle",
                "prompt_preview": "",
            },
        ],
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=4,
        tool_name="node.list",
        budget_chars=180,
    )

    observation = json.loads(envelope["model_visible"]["content"])
    summary = observation["result"]["summary"]
    assert observation["result"]["tool_result_compacted"] is True
    assert summary["total"] == 2
    assert summary["returned"] == 2
    assert summary["nodes"][0]["node_id"] == "image-1"
    assert summary["nodes"][0]["title"] == "角色参考图"
    assert summary["nodes"][0]["status"] == "failed"
    assert summary["nodes"][0]["prompt_preview"] == prompt[:20]
    assert summary["nodes"][1]["node_id"] == "video-1"
    assert summary["nodes"][1]["prompt_preview"] == ""
    assert "output" not in summary["nodes"][0]
    assert tool_trace_fields(envelope)["tool_result_summary"]["nodes"][0]["node_id"] == "image-1"


def test_batch_node_create_compacted_summary_preserves_created_nodes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": True,
        "status": "ok",
        "project_id": "project",
        "requested": 2,
        "created_count": 2,
        "failed_count": 0,
        "client_node_ids": {"brief": "text-1"},
        "nodes": [
            {
                "id": "text-1",
                "node_id": "text-1",
                "type": "text",
                "title": "项目 brief",
                "status": "idle",
                "prompt_preview": "",
                "output": {"large": "x" * 400},
                "index": 0,
                "client_ref": "brief",
            },
            {
                "id": "image-1",
                "node_id": "image-1",
                "type": "image",
                "title": "参考图",
                "status": "idle",
                "prompt": "12345678901234567890extra",
                "index": 1,
            },
        ],
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=4,
        tool_name="node.create",
        budget_chars=180,
    )

    observation = json.loads(envelope["model_visible"]["content"])
    summary = observation["result"]["summary"]
    assert summary["created_count"] == 2
    assert summary["nodes"][0]["node_id"] == "text-1"
    assert summary["nodes"][0]["client_ref"] == "brief"
    assert summary["nodes"][1]["prompt_preview"] == "12345678901234567890"
    assert "output" not in summary["nodes"][0]
    assert tool_trace_fields(envelope)["tool_result_summary"]["nodes"][1]["node_id"] == "image-1"


def test_tool_output_trace_summary_preserves_media_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": True,
        "node_id": "image-1",
        "type": "image",
        "status": "completed",
        "url": "/api/media/project/image.png",
        "result": {
            "status": "completed",
            "url": "/api/media/project/image.png",
            "n_succeeded": 1,
        },
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=4,
        tool_name="node.run",
        budget_chars=1000,
    )

    summary = tool_trace_fields(envelope)["tool_result_summary"]
    assert summary["node_id"] == "image-1"
    assert summary["status"] == "completed"
    assert summary["url"] == "/api/media/project/image.png"
    assert summary["result"]["status"] == "completed"
    assert summary["result"]["n_succeeded"] == 1


def test_tool_output_trace_summary_preserves_review_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "role": "reviewer",
        "summary": "图片已生成，但提示词需要补依赖。",
        "review_status": "revise_required",
        "result": {
            "status": "revise_required",
            "passed": False,
            "safe_to_run": False,
            "findings": [
                {
                    "severity": "high",
                    "issue": "缺少参考图依赖",
                    "evidence": "reference_images 为空",
                    "suggested_fix": "补 node:<id>",
                }
            ],
        },
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=5,
        tool_name="agent.review",
        budget_chars=3000,
    )

    summary = tool_trace_fields(envelope)["tool_result_summary"]
    assert summary["review_status"] == "revise_required"
    assert summary["findings_count"] == 1
    assert summary["safe_to_run"] is False
    assert summary["findings"][0]["issue"] == "缺少参考图依赖"


def test_tool_output_envelope_preserves_error_feedback_for_model_and_ui(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": False,
        "error": "节点不存在",
        "error_kind": "node_not_found",
        "hint": "先读取真实节点列表。",
        "suggested_next": "read_state",
        "available_node_ids": ["brief", "segment_01"],
        "large": ["x" * 80 for _ in range(20)],
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=4,
        tool_name="node.update",
        budget_chars=160,
    )

    assert envelope["ok"] is False
    assert envelope["success"] is False
    assert envelope["outcome"] == "recoverable_error"
    assert envelope["ui"]["result"]["ok"] is False
    assert envelope["ui"]["result"]["error_kind"] == "node_not_found"
    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["success"] is False
    assert observation["outcome"] == "recoverable_error"
    assert observation["error_kind"] == "node_not_found"
    assert observation["next_action"] == "read_state"
    assert observation["model_feedback"]["suggested_next"] == "read_state"
    assert "segment_01" in json.dumps(observation, ensure_ascii=False)
    trace = tool_trace_fields(envelope)
    assert trace["tool_result_ok"] is False
    assert trace["tool_result_success"] is False
    assert trace["tool_result_outcome"] == "recoverable_error"
    assert trace["tool_result_summary"]["error_kind"] == "node_not_found"


def test_tool_output_envelope_routes_permission_errors_to_user_input(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": False,
        "error": "删除节点需要用户确认。",
        "error_kind": "permission_denied",
        "reason": "destructive action requires confirmation",
        "hint": "向用户说明风险并等待确认。",
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=5,
        tool_name="canvas.delete",
        budget_chars=1000,
    )

    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["success"] is False
    assert observation["outcome"] == "recoverable_error"
    assert observation["next_action"] == "ask_or_wait_for_user"
    assert observation["model_feedback"]["suggested_next"] == "ask_or_wait_for_user"
    assert observation["error_kind"] == "permission_denied"
    assert "等待确认" in observation["hint"]


def test_tool_output_envelope_routes_invalid_node_field_to_argument_repair(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": False,
        "error": "节点字段不被允许。",
        "error_kind": "invalid_field",
        "node_id": "final_video",
        "allowed_fields": ["title", "prompt", "input_json", "fields"],
        "hint": "只更新 schema 允许字段。",
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=6,
        tool_name="node.update",
        budget_chars=1000,
    )

    observation = json.loads(envelope["model_visible"]["content"])
    evidence = observation["model_feedback"]["evidence"]
    assert observation["success"] is False
    assert observation["next_action"] == "repair_arguments"
    assert evidence["node_id"] == "final_video"
    assert evidence["allowed_fields"] == ["title", "prompt", "input_json", "fields"]


def test_tool_output_envelope_marks_review_required_as_model_action_needed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": True,
        "status": "agent_review_required",
        "finalized": False,
        "needs_review": True,
        "message": "节点已更新，需要只读审查。",
        "suggested_tool": "agent.review",
        "grounded_findings_count": 0,
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=5,
        tool_name="node.run",
        budget_chars=1000,
    )

    assert envelope["ok"] is True
    assert envelope["handler_ok"] is True
    assert envelope["success"] is False
    assert envelope["outcome"] == "needs_action"
    observation = json.loads(envelope["model_visible"]["content"])
    assert observation["handler_ok"] is True
    assert observation["success"] is False
    assert observation["outcome"] == "needs_action"
    assert observation["next_action"] == "call_agent_review"
    assert observation["model_feedback"]["suggested_next"] == "call_agent_review"
    assert "agent.review" in json.dumps(observation, ensure_ascii=False)
    trace = tool_trace_fields(envelope)
    assert trace["tool_result_ok"] is True
    assert trace["tool_result_success"] is False
    assert trace["tool_result_outcome"] == "needs_action"


def test_tool_output_envelope_keeps_review_recommendation_soft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    result = {
        "ok": True,
        "status": "completed",
        "review_recommended": True,
        "review_status": "review_recommended",
        "recommended_tool": "agent.review",
        "message": "节点已运行，建议按 active skill 检查。",
    }
    envelope = build_tool_output_envelope(
        result,
        project_id="project",
        run_id="run",
        iteration=5,
        tool_name="node.run",
        budget_chars=1000,
    )

    observation = json.loads(envelope["model_visible"]["content"])
    assert envelope["success"] is True
    assert envelope["outcome"] == "success"
    assert observation["success"] is True
    assert observation["outcome"] == "success"
    assert observation["next_action"] == "continue"
    assert "model_feedback" not in observation
