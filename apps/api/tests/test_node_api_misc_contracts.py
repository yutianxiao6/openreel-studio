import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import routes_projects, routes_uploads
from app.config_store.schema import MediaProviderEntry
from app.db.models import Asset, Project, WorkflowNode
from app.mcp_tools import canvas_tools, node_universal
from app.services import media_generation
from app.services import media_history
from app.services import node_recovery
from app.services import media_provider
from app.services.node_service import canvas_edge_payloads


@pytest.fixture(autouse=True)
def isolate_node_project_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_project_state(project_id: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(node_universal, "_read_project_state", fake_project_state)


def test_public_node_types_are_generic_only():
    assert node_universal.NODE_TYPES == ("text", "image", "video", "audio")
    assert set(node_universal._RUNNERS) == {"text", "image", "video", "audio"}
    assert set(node_universal._NODE_FIELD_SCHEMA) == {"text", "image", "video", "audio"}


def test_project_active_workflow_template_state_round_trips():
    template_id = routes_projects.canvas_workflow_templates.list_template_summaries()[0]["id"]

    state = routes_projects._active_workflow_state_from_request(
        routes_projects.ProjectWorkflowActiveRequest(kind="template", template_id=template_id)
    )
    payload = routes_projects._project_active_workflow_payload(
        "project-1",
        {routes_projects.ACTIVE_WORKFLOW_STATE_KEY: state},
    )

    assert state["kind"] == "template"
    assert payload == {
        "kind": "template",
        "template_id": template_id,
        "updated_at": state["updated_at"],
    }


@pytest.mark.asyncio
async def test_restore_builtin_workflow_template_removes_user_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProjectService:
        def __init__(self, db: object) -> None:
            self.db = db

        async def get_project_state(self, project_id: str) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(routes_projects, "ProjectService", FakeProjectService)
    monkeypatch.setattr(
        routes_projects.canvas_workflow_templates,
        "get_builtin_template",
        lambda template_id: {"id": template_id, "name": "内置流程"},
    )
    monkeypatch.setattr(routes_projects.workflow_template_store, "user_template_exists", lambda template_id: True)
    monkeypatch.setattr(
        routes_projects.workflow_template_store,
        "delete_user_template",
        lambda template_id: {"ok": True, "template_id": template_id, "deleted_paths": ["user.json"]},
    )
    monkeypatch.setattr(
        routes_projects.canvas_workflow_templates,
        "list_template_summaries",
        lambda: [{"id": "demo_builtin", "name": "内置流程", "scope": "builtin", "steps": []}],
    )

    result = await routes_projects.restore_project_builtin_workflow_template(
        project_id="project-1",
        template_id="demo_builtin",
        db=object(),
    )

    assert result["ok"] is True
    assert result["template_id"] == "demo_builtin"
    assert result["restored_scope"] == "builtin"
    assert result["summary"]["scope"] == "builtin"
    assert result["deleted_user_template"]["deleted_paths"] == ["user.json"]


def test_project_active_workflow_imported_state_restores_preview():
    workflow = {
        "schema": "openreel.workflow.v2",
        "id": "grid_storyboard_workflow",
        "title": "宫格分镜流程",
        "inputs": {"plot": {"type": "long_text", "label": "剧情", "required": True}},
        "steps": [
            {"id": "script", "title": "剧本", "kind": "text", "prompt": {"task": "根据 {{ inputs.plot }} 写剧本。"}},
            {"id": "storyboard", "title": "宫格分镜", "kind": "image", "needs": ["script"], "prompt": {"task": "写分镜图提示词。"}},
        ],
    }

    state = routes_projects._active_workflow_state_from_request(
        routes_projects.ProjectWorkflowActiveRequest(
            kind="imported",
            workflow=workflow,
            name="宫格分镜流程",
        )
    )
    payload = routes_projects._project_active_workflow_payload(
        "project-1",
        {routes_projects.ACTIVE_WORKFLOW_STATE_KEY: state},
    )

    assert payload is not None
    assert payload["kind"] == "imported"
    assert payload["workflow"] == workflow
    assert payload["preview"]["step_count"] == 2
    assert payload["preview"]["input_ids"] == ["plot"]
    assert payload["preview"]["first_steps"][1]["id"] == "storyboard"


def test_project_active_workflow_imported_v2_returns_logical_preview():
    workflow = {
        "schema": "openreel.workflow.v2",
        "id": "grid_storyboard_authoring",
        "title": "宫格分镜作者层流程",
        "inputs": {"plot": {"type": "long_text", "label": "剧情", "required": True}},
        "steps": [
            {
                "id": "script",
                "title": "剧本",
                "kind": "text",
                "output": {"canvas": True},
                "prompt": {"role": "编剧", "task": "写剧本。"},
            },
            {
                "id": "storyboard",
                "title": "宫格分镜",
                "kind": "image",
                "needs": ["script"],
                "prompt": {"role": "分镜导演", "task": "写分镜图提示词。"},
            },
        ],
    }

    state = routes_projects._active_workflow_state_from_request(
        routes_projects.ProjectWorkflowActiveRequest(
            kind="imported",
            workflow=workflow,
            name="宫格分镜作者层流程",
        )
    )
    payload = routes_projects._project_active_workflow_payload(
        "project-1",
        {routes_projects.ACTIVE_WORKFLOW_STATE_KEY: state},
    )

    assert payload is not None
    assert payload["workflow"] == workflow
    assert payload["preview"]["schema"] == "openreel.workflow.v2"
    assert payload["preview"]["input_ids"] == ["plot"]
    assert payload["preview"]["required_inputs"] == ["plot"]
    assert payload["preview"]["first_steps"][0]["kind"] == "text"
    assert payload["preview"]["first_steps"][1]["id"] == "storyboard"
    assert payload["preview"]["first_steps"][1]["kind"] == "image"
    assert [step["id"] for step in payload["preview"]["first_steps"]] == ["script", "storyboard"]
    assert all("__" not in step["id"] for step in payload["preview"]["first_steps"])


def test_project_workflow_runtime_payload_restores_latest_matching_instance():
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_old": {
                    "template_id": "other_workflow",
                    "template_name": "其他流程",
                    "steps": {"input": {"title": "输入", "status": "completed"}},
                },
                "wf_current": {
                    "template_id": "grid_storyboard_workflow",
                    "template_name": "宫格分镜流程",
                    "updated_at": "2026-06-29T01:02:03Z",
                    "steps": {
                        "input": {"title": "输入", "type": "text", "status": "completed"},
                        "storyboard": {
                            "title": "宫格分镜",
                            "type": "image",
                            "status": "running",
                            "node_id": "node-1",
                            "workflow": {
                                "template_step_id": "storyboard",
                                "repeat_group_id": "episode_segments",
                                "repeat_group_label": "每集每段流程",
                                "repeat_group_index": 1,
                                "phase": "storyboard",
                                "kind": "image",
                                "depends_on": ["scene_reference", "plan_frames"],
                                "output": {"canvas": True},
                            },
                        },
                    },
                },
            }
        }
    }

    payload = routes_projects._project_workflow_runtime_payload(state, "grid_storyboard_workflow")

    assert payload["instance_id"] == "wf_current"
    assert payload["template_id"] == "grid_storyboard_workflow"
    assert payload["template_name"] == "宫格分镜流程"
    assert payload["updated_at"] == "2026-06-29T01:02:03Z"
    assert [(step["id"], step["status"], step["node_id"]) for step in payload["steps"]] == [
        ("input", "completed", ""),
        ("storyboard", "running", "node-1"),
    ]
    assert payload["steps"][0]["run_count"] == 0
    assert payload["steps"][0]["stale"] is False
    assert payload["steps"][0]["canvas_output"] is False
    assert payload["steps"][0]["runtime_only"] is True
    assert payload["steps"][1]["artifact_node_ids"] == []
    assert payload["steps"][1]["canvas_output"] is True
    assert payload["steps"][1]["runtime_only"] is False
    assert payload["steps"][1]["template_step_id"] == "storyboard"
    assert payload["steps"][1]["repeat_group_id"] == "episode_segments"
    assert payload["steps"][1]["repeat_group_label"] == "每集每段流程"
    assert payload["steps"][1]["phase"] == "storyboard"
    assert payload["steps"][1]["kind"] == "image"
    assert payload["steps"][1]["depends_on"] == ["scene_reference", "plan_frames"]
    assert payload["steps"][1]["output"] is None


def test_workflow_runtime_payload_with_missing_explicit_instance_does_not_fallback():
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_history": {
                    "template_id": "grid_storyboard_workflow",
                    "template_name": "历史流程",
                    "steps": {"script": {"title": "旧剧本", "status": "completed"}},
                },
            }
        }
    }

    payload = routes_projects.workflow_tools.workflow_runtime_public_payload(
        state,
        template_id="grid_storyboard_workflow",
        instance_id="wf_new",
    )

    assert payload == {
        "instance_id": "wf_new",
        "template_id": "grid_storyboard_workflow",
        "steps": [],
    }


def test_project_workflow_runtime_payloads_report_dependency_state():
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_ready": {
                    "template_id": "dependency_flow",
                    "template_name": "依赖流程",
                    "updated_at": "2026-07-01T01:00:00Z",
                    "steps": {
                        "script": {"title": "剧本", "status": "completed"},
                        "image": {
                            "title": "图片",
                            "status": "draft",
                            "workflow": {"depends_on": ["script"]},
                        },
                    },
                },
                "wf_blocked": {
                    "template_id": "dependency_flow",
                    "template_name": "依赖流程",
                    "updated_at": "2026-07-01T02:00:00Z",
                    "steps": {
                        "script": {"title": "剧本", "status": "draft"},
                        "image": {
                            "title": "图片",
                            "status": "draft",
                            "workflow": {"depends_on": ["script"]},
                        },
                    },
                },
            }
        }
    }

    payloads = routes_projects._project_workflow_runtime_payloads(state, "dependency_flow")

    assert [payload["instance_id"] for payload in payloads] == ["wf_blocked", "wf_ready"]
    by_id = {payload["instance_id"]: payload for payload in payloads}
    ready_steps = {step["id"]: step for step in by_id["wf_ready"]["steps"]}
    blocked_steps = {step["id"]: step for step in by_id["wf_blocked"]["steps"]}
    assert ready_steps["image"]["ready"] is True
    assert ready_steps["image"]["execution_state"] == "ready"
    assert blocked_steps["image"]["waiting_on"] == ["script"]
    assert blocked_steps["image"]["execution_state"] == "blocked"


def test_project_workflow_runtime_payloads_can_return_all_templates():
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_old": {
                    "template_id": "archive_flow",
                    "template_name": "归档流程",
                    "updated_at": "2026-07-01T01:00:00Z",
                    "steps": {"full_script": {"title": "剧本", "status": "completed"}},
                },
                "wf_new": {
                    "template_id": "current_flow",
                    "template_name": "当前流程",
                    "updated_at": "2026-07-01T02:00:00Z",
                    "steps": {"extract_keyframes": {"title": "提取关键帧", "status": "completed"}},
                },
            }
        }
    }

    all_payloads = routes_projects._project_workflow_runtime_payloads(state)
    filtered_payloads = routes_projects._project_workflow_runtime_payloads(state, "current_flow")

    assert [payload["instance_id"] for payload in all_payloads] == ["wf_new", "wf_old"]
    assert [payload["template_id"] for payload in all_payloads] == [
        "current_flow",
        "archive_flow",
    ]
    assert [payload["instance_id"] for payload in filtered_payloads] == ["wf_new"]


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


def test_canvas_workflow_summary_keeps_reviewable_metadata():
    summary = canvas_tools._compact_workflow_summary({
            "workflow": {
                "template_id": "model_authored_workflow",
                "instance_id": "wf_test",
                "step_id": "single_storyboard",
                "step_index": 13,
                "mode": "single",
                "role": "template_step",
                "expansion": {"mode": "per_segment", "source": "script.episodes[].segments[]", "label": "按段展开", "extra": "hidden"},
                "collection": {"kind": "segments", "items_source": "script.episodes[].segments[]", "label": "段落", "extra": "hidden"},
                "instance_scope": {"episode": 1, "segment": 2},
                "template_step_id": "storyboard",
                "expand_when": "after_script_segments",
                "prompt_ref": "shot_grid_prompt#grid_storyboard",
                "prompt_spec": {"goal": "生成宫格分镜", "output": "image prompt", "private": "hidden"},
                "runner": "node_producer",
                "source_node_id": "singleStoryboard",
                "source_label": "单分镜帧",
                "source_category": "segment",
            "repeat": {"mode": "per_segment", "source": "script.segments", "label": "每段", "extra": "hidden"},
            "optional": True,
            "manual_only": True,
            "source_behavior": "手动添加，最多10张",
        }
    })

    assert summary == {
        "template_id": "model_authored_workflow",
        "instance_id": "wf_test",
        "step_id": "single_storyboard",
        "step_index": 13,
        "mode": "single",
        "role": "template_step",
        "expansion": {"mode": "per_segment", "source": "script.episodes[].segments[]", "label": "按段展开"},
        "collection": {"kind": "segments", "items_source": "script.episodes[].segments[]", "label": "段落"},
        "instance_scope": {"episode": 1, "segment": 2},
        "template_step_id": "storyboard",
        "expand_when": "after_script_segments",
        "prompt_ref": "shot_grid_prompt#grid_storyboard",
        "prompt_spec": {"goal": "生成宫格分镜", "output": "image prompt"},
        "runner": "node_producer",
        "source_node_id": "singleStoryboard",
        "source_label": "单分镜帧",
        "source_category": "segment",
        "source_behavior": "手动添加，最多10张",
        "repeat": {"mode": "per_segment", "source": "script.segments", "label": "每段"},
        "optional": True,
        "manual_only": True,
    }


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
    green = SimpleNamespace(
        id="green-1",
        project_id="proj-1",
        input_json=json.dumps({"references": [{"nodeId": "script-1", "role": "context"}]}, ensure_ascii=False),
    )
    empty = SimpleNamespace(
        id="empty-1",
        project_id="proj-1",
        input_json=json.dumps({"depends_on": [], "references": [], "reference_images": []}, ensure_ascii=False),
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
        [script, red, blue, green, empty],
        [
            FakeEdge("script-1", "red-1"),
            FakeEdge("script-1", "red-1"),
            FakeEdge("red-1", "blue-1"),
            FakeEdge("red-1", "green-1"),
            FakeEdge("script-1", "empty-1"),
        ],
    )

    pairs = {(edge["source_node_id"], edge["target_node_id"]) for edge in payloads}
    assert pairs == {("script-1", "red-1"), ("script-1", "blue-1"), ("script-1", "green-1")}
    assert len(payloads) == 3


def test_canvas_edge_payloads_resolve_public_node_reference_ids():
    source = SimpleNamespace(
        id="source-internal-id",
        display_id=12,
        project_id="proj-1",
        input_json=json.dumps({"content": "参考图"}, ensure_ascii=False),
    )
    panorama = SimpleNamespace(
        id="panorama-internal-id",
        display_id=13,
        project_id="proj-1",
        input_json=json.dumps({"references": [{"ref": "node:12", "role": "visual_reference"}]}, ensure_ascii=False),
    )

    payloads = canvas_edge_payloads([source, panorama], [])

    assert payloads == [{
        "id": "dep-source-internal-id-panorama-internal-id",
        "project_id": "proj-1",
        "source_node_id": "source-internal-id",
        "target_node_id": "panorama-internal-id",
        "label": None,
        "created_at": None,
        "_derived": "node_dependencies",
    }]


def test_canvas_edge_payloads_resolve_display_id_zero_reference():
    source = SimpleNamespace(
        id="source-internal-id",
        display_id=0,
        project_id="proj-1",
        input_json=json.dumps({"content": "根节点"}, ensure_ascii=False),
    )
    target = SimpleNamespace(
        id="target-internal-id",
        display_id=1,
        project_id="proj-1",
        input_json=json.dumps({"depends_on": ["node:0"]}, ensure_ascii=False),
    )

    payloads = canvas_edge_payloads([source, target], [])

    assert payloads == [{
        "id": "dep-source-internal-id-target-internal-id",
        "project_id": "proj-1",
        "source_node_id": "source-internal-id",
        "target_node_id": "target-internal-id",
        "label": None,
        "created_at": None,
        "_derived": "node_dependencies",
    }]


def test_canvas_edge_payloads_ignore_reference_image_cache_when_refs_exist():
    character = SimpleNamespace(
        id="character-node",
        display_id=1,
        project_id="proj-1",
        input_json=json.dumps({"content": "人物"}, ensure_ascii=False),
    )
    current_storyboard = SimpleNamespace(
        id="storyboard-current",
        display_id=6,
        project_id="proj-1",
        input_json=json.dumps({"content": "当前段分镜"}, ensure_ascii=False),
    )
    stale_storyboard = SimpleNamespace(
        id="storyboard-stale",
        display_id=4,
        project_id="proj-1",
        input_json=json.dumps({"content": "旧分镜"}, ensure_ascii=False),
    )
    target = SimpleNamespace(
        id="video-node",
        display_id=9,
        project_id="proj-1",
        input_json=json.dumps(
            {
                "references": [{"ref": "node:6", "role": "visual_reference"}],
                "depends_on": ["node:6"],
                "reference_images": ["node:1", "node:4"],
            },
            ensure_ascii=False,
        ),
    )

    payloads = canvas_edge_payloads([character, current_storyboard, stale_storyboard, target], [])

    pairs = {(edge["source_node_id"], edge["target_node_id"]) for edge in payloads}
    assert pairs == {("storyboard-current", "video-node")}


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
            "workflow": {"step_id": f"step_{index}", "mode": "grid"} if index == 0 else None,
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
    assert first["workflow"] == {"step_id": "step_0", "mode": "grid"}
    assert "output" not in first
    assert all_result["returned"] == 25
    assert all_result["truncated"] is False
    assert all_result["filters"]["unlimited"] is True


@pytest.mark.asyncio
async def test_node_list_omits_workflow_runtime_nodes_by_default(monkeypatch):
    nodes = [
        {
            "id": "runtime-1",
            "type": "text",
            "title": "人物集合",
            "status": "completed",
            "surface": "workflow_runtime",
            "input": {"surface": "workflow_runtime"},
        },
        {
            "id": "image-1",
            "type": "image",
            "title": "主要人物图",
            "status": "completed",
            "surface": "draft_canvas",
            "input": {"surface": "draft_canvas"},
        },
    ]

    async def fake_list_nodes(project_id: str):
        assert project_id == "proj-1"
        return list(nodes)

    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)

    result = await node_universal.node_list("proj-1", limit=0)

    assert result["returned"] == 1
    assert result["total"] == 1
    assert [node["node_id"] for node in result["nodes"]] == ["image-1"]


@pytest.mark.asyncio
async def test_node_run_rejects_workflow_runtime_node(monkeypatch):
    async def fake_resolve(project_id: str, node_id: str):
        assert project_id == "proj-1"
        assert node_id == "runtime-1"
        return "runtime-1"

    async def fake_public_id_map(project_id: str):
        assert project_id == "proj-1"
        return {"runtime-1": "0"}

    async def fake_get_node(node_id: str):
        assert node_id == "runtime-1"
        return {
            "id": "runtime-1",
            "display_id": 0,
            "project_id": "proj-1",
            "type": "text",
            "title": "Script",
            "status": "failed",
            "surface": "workflow_runtime",
            "input": {
                "surface": "workflow_runtime",
                "workflow": {
                    "step_id": "script",
                    "visibility": "flow_only",
                },
            },
            "prompt": "",
        }

    async def unexpected_update_node(*args, **kwargs):
        raise AssertionError("workflow runtime node must not be updated by node.run")

    monkeypatch.setattr(node_universal, "_resolve_agent_node_id", fake_resolve)
    monkeypatch.setattr(node_universal, "_node_public_id_map", fake_public_id_map)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", unexpected_update_node)

    result = await node_universal.node_run(project_id="proj-1", node_id="runtime-1")

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_runtime_node_not_runnable"
    assert result["node_id"] == "0"


@pytest.mark.asyncio
async def test_node_list_and_get_support_fuzzy_query_and_regex(monkeypatch):
    nodes = [
        {
            "id": "image-1",
            "project_id": "proj-1",
            "type": "image",
            "title": "红衣角色分镜",
            "status": "completed",
            "prompt": "雨夜里红衣女孩回头的电影分镜图",
            "input": {"purpose": "storyboard"},
            "workflow": {
                "step_id": "grid_storyboard",
                "mode": "grid",
                "repeat": {"mode": "per_segment", "source": "script.segments", "label": "每段"},
            },
        },
        {
            "id": "video-1",
            "project_id": "proj-1",
            "type": "video",
            "title": "最终视频",
            "status": "idle",
            "prompt": "城市街道镜头",
        },
    ]
    by_id = {node["id"]: node for node in nodes}

    async def fake_list_nodes(project_id: str):
        assert project_id == "proj-1"
        return list(nodes)

    async def fake_get_node(node_id: str):
        return by_id.get(node_id) or {"error": "Node not found"}

    monkeypatch.setattr(node_universal.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)

    fuzzy = await node_universal.node_list(project_id="proj-1", query="红衣 分镜")
    regex = await node_universal.node_list(project_id="proj-1", regex=r"红衣.*分镜|storyboard")
    detail = await node_universal.node_get(project_id="proj-1", query="红衣 分镜")

    assert [node["id"] for node in fuzzy["nodes"]] == ["image-1"]
    assert fuzzy["nodes"][0]["match"]["mode"] == "query"
    assert fuzzy["nodes"][0]["match_hint"]
    assert [node["id"] for node in regex["nodes"]] == ["image-1"]
    assert regex["nodes"][0]["match"]["matched_patterns"] == [r"红衣.*分镜|storyboard"]
    assert detail["ok"] is True
    assert detail["mode"] == "query"
    assert [node["id"] for node in detail["nodes"]] == ["image-1"]
    assert detail["nodes"][0]["workflow"]["step_id"] == "grid_storyboard"
    assert detail["nodes"][0]["workflow"]["repeat"]["mode"] == "per_segment"


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


@pytest.mark.asyncio
async def test_node_update_prompt_reopens_failed_video_node(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "video-1"
        return {
            "id": node_id,
            "project_id": "proj-1",
            "type": "video",
            "status": "failed",
            "title": "视频提示词",
            "prompt": "old prompt",
            "error_message": "参数验证失败",
            "input": {
                "title": "视频提示词",
                "prompt": "old prompt",
                "duration_seconds": 15,
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, "type": "video", **patch}

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        project_id="proj-1",
        node_id="video-1",
        patch={
            "prompt": "new video prompt",
            "input_json": {"prompt_status": "final_prompt"},
        },
    )

    assert updates == [
        {
            "prompt": "new video prompt",
            "input_json": {
                "title": "视频提示词",
                "prompt": "new video prompt",
                "prompt_preview": "new video prompt",
                "duration_seconds": 15,
                "prompt_status": "final_prompt",
            },
            "status": "idle",
            "error_message": None,
        }
    ]
    assert result["status"] == "idle"
    assert result["error_message"] is None


def test_manual_image_edge_writes_visual_reference_for_text_and_image_targets():
    source = WorkflowNode(id="image-source", project_id="proj-1", display_id=7, type="image", title="参考图")
    text_target = WorkflowNode(id="text-target", project_id="proj-1", type="text", title="文字")
    image_target = WorkflowNode(
        id="image-target",
        project_id="proj-1",
        type="image",
        title="图片",
        input_json=json.dumps({
            "render_state": "fresh",
            "reference_images": [],
            "fields": {"depends_on": [], "references": [], "reference_images": []},
        }, ensure_ascii=False),
    )

    assert routes_projects._add_edge_dependency(text_target, source) is True
    assert routes_projects._add_edge_dependency(image_target, source) is True

    text_input = json.loads(text_target.input_json or "{}")
    image_input = json.loads(image_target.input_json or "{}")
    expected_ref = {"ref": "node:7", "role": "visual_reference"}
    assert text_input["depends_on"] == ["node:7"]
    assert text_input["references"] == [expected_ref]
    assert text_input["reference_images"] == ["node:7"]
    assert image_input["depends_on"] == ["node:7"]
    assert image_input["references"] == [expected_ref]
    assert image_input["reference_images"] == ["node:7"]
    assert image_input["fields"]["depends_on"] == ["node:7"]
    assert image_input["fields"]["references"] == [expected_ref]
    assert image_input["fields"]["reference_images"] == ["node:7"]
    assert image_input["render_state"] == "stale"

    image_input["references"].append({"ref": "node:7", "role": "source_image"})
    image_input["reference_images"] = ["node:7"]
    image_input["fields"] = {
        "depends_on": ["node:7"],
        "references": [
            {"ref": "node:7", "role": "visual_reference"},
            {"ref": "node:7", "role": "source_image"},
        ],
        "reference_images": ["node:7"],
    }
    image_target.input_json = json.dumps(image_input, ensure_ascii=False)
    assert routes_projects._remove_edge_dependency(image_target, source) is True
    image_input = json.loads(image_target.input_json or "{}")
    assert image_input["depends_on"] == []
    assert image_input["references"] == []
    assert image_input["reference_images"] == []
    assert image_input["fields"]["depends_on"] == []
    assert image_input["fields"]["references"] == []
    assert image_input["fields"]["reference_images"] == []
    assert node_universal._coerce_reference_values(
        image_input.get("references"),
        image_input.get("depends_on"),
        image_input.get("reference_images"),
        include_roles=node_universal._MEDIA_REFERENCE_ROLES,
        exclude_roles=node_universal._DIRECT_IMAGE_SOURCE_ROLES,
    ) == []
    assert node_universal._coerce_reference_values(
        image_input["fields"].get("references"),
        image_input["fields"].get("depends_on"),
        image_input["fields"].get("reference_images"),
        include_roles=node_universal._MEDIA_REFERENCE_ROLES,
        exclude_roles=node_universal._DIRECT_IMAGE_SOURCE_ROLES,
    ) == []

    legacy_target = WorkflowNode(
        id="legacy-target",
        project_id="proj-1",
        type="image",
        title="旧引用",
        input_json=json.dumps({
            "depends_on": ["node:image-source"],
            "references": [{"ref": "node:image-source", "role": "visual_reference"}],
            "reference_images": ["node:image-source"],
        }, ensure_ascii=False),
    )
    assert routes_projects._remove_edge_dependency(legacy_target, source) is True
    legacy_input = json.loads(legacy_target.input_json or "{}")
    assert legacy_input["depends_on"] == []
    assert legacy_input["references"] == []
    assert legacy_input["reference_images"] == []
    assert node_universal._coerce_reference_values(
        image_input.get("references"),
        image_input["fields"].get("references"),
        include_roles=node_universal._DIRECT_IMAGE_SOURCE_ROLES,
    ) == []
    assert image_input["render_state"] == "stale"


@pytest.mark.asyncio
async def test_batch_delete_nodes_cleans_derived_dependencies_without_edges(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delete-nodes.db'}", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(routes_projects.project_media_history, "register_nodes_outputs", lambda *_args, **_kwargs: [])

    try:
        async with session_local() as session:
            session.add(Project(id="proj-delete", title="删除测试", state_json="{}"))
            session.add(WorkflowNode(
                id="source-node",
                project_id="proj-delete",
                display_id=1,
                type="image",
                title="源图",
            ))
            session.add(WorkflowNode(
                id="second-source",
                project_id="proj-delete",
                display_id=2,
                type="text",
                title="源文本",
            ))
            session.add(WorkflowNode(
                id="target-node",
                project_id="proj-delete",
                display_id=3,
                type="video",
                title="下游视频",
                input_json=json.dumps({
                    "depends_on": ["node:1", "node:2", "node:external"],
                    "references": [
                        {"ref": "node:1", "role": "visual_reference"},
                        {"ref": "node:2", "role": "context"},
                        {"ref": "node:external", "role": "context"},
                    ],
                    "reference_images": ["node:1", "node:external"],
                    "fields": {
                        "depends_on": ["node:1", "node:2"],
                        "references": [{"ref": "node:1", "role": "visual_reference"}],
                        "reference_images": ["node:1"],
                    },
                }, ensure_ascii=False),
            ))
            session.add(Asset(
                id="asset-source",
                project_id="proj-delete",
                node_id="source-node",
                type="video",
                name="源资产",
            ))
            await session.commit()

            result = await routes_projects._delete_project_canvas_nodes(
                "proj-delete",
                ["1", "second-source"],
                session,
            )

            assert result["deleted_nodes"] == 2
            assert result["deleted_asset_records"] == 1
            assert result["cleaned_dependency_nodes"] == 1
            assert await session.get(WorkflowNode, "source-node") is None
            assert await session.get(WorkflowNode, "second-source") is None
            assert await session.get(Asset, "asset-source") is None
            target = await session.get(WorkflowNode, "target-node")
            assert target is not None
            target_input = json.loads(target.input_json or "{}")
            assert target_input["depends_on"] == ["node:external"]
            assert target_input["references"] == [{"ref": "node:external", "role": "context"}]
            assert target_input["reference_images"] == ["node:external"]
            assert target_input["fields"]["depends_on"] == []
            assert target_input["fields"]["references"] == []
            assert target_input["fields"]["reference_images"] == []
    finally:
        await engine.dispose()


def test_project_node_detail_payload_publicizes_reference_node_ids():
    source_id = "11111111-1111-4111-8111-111111111111"
    node = WorkflowNode(
        id="22222222-2222-4222-8222-222222222222",
        project_id="proj-1",
        display_id=8,
        type="image",
        title="目标图",
        status="idle",
        input_json=json.dumps({
            "depends_on": [f"node:{source_id}"],
            "references": [{"ref": f"node:{source_id}", "role": "visual_reference"}],
            "reference_images": [f"node:{source_id}"],
            "fields": {
                "references": [{"ref": f"node:{source_id}", "role": "visual_reference"}],
            },
        }, ensure_ascii=False),
        output_json=json.dumps({"source_node_id": source_id}, ensure_ascii=False),
    )

    payload = routes_projects._node_detail_payload(node, {source_id: "7"})

    assert payload["input"]["depends_on"] == ["node:7"]
    assert payload["input"]["references"] == [{"ref": "node:7", "role": "visual_reference"}]
    assert payload["input"]["reference_images"] == ["node:7"]
    assert payload["input"]["fields"]["references"] == [{"ref": "node:7", "role": "visual_reference"}]
    assert payload["output"]["source_node_id"] == "7"


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


@pytest.mark.asyncio
async def test_merge_stage_into_fusion_preserves_legacy_nested_output_on_render_fail(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "project_id": "project-1",
            "output": {
                "type": "image",
                "status": "completed",
                "result": {
                    "output": {
                        "url": "/api/media/project-1/legacy.png",
                    },
                },
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
        status="failed",
        error="bad response body",
        prompt="测试",
        size="1080x1920",
    )

    stage = fusion["stages"][0]
    assert stage["name"] == "图片"
    assert stage["status"] == "failed"
    assert stage["url"] == "/api/media/project-1/legacy.png"
    assert stage["error"] == "bad response body"
    assert updates and updates[-1]["output_data"] == fusion


@pytest.mark.asyncio
async def test_media_provider_raw_http_fallback_parses_body_when_response_path_mismatch(monkeypatch):

    class FakeResponse:
        status_code = 200

        def __init__(self, data: Any):
            self._data = data
            self.text = json.dumps(data)

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            assert url == "https://example.test"
            return FakeResponse({"result": {"data": {"url": "/api/media/project-1/generated.png"}}})

    provider = SimpleNamespace(
        base_url="https://example.test",
        api_key="token",
        params_json="{}",
    )

    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._call_raw_http(
        provider,
        prompt="cute cat",
        negative_prompt=None,
        size="1080x1920",
        reference_images=None,
        extra_override={"_response_image_path": ["missing", "url"]},
    )

    assert result.get("images") == [{"url": "/api/media/project-1/generated.png", "b64": None}]


def test_media_provider_timeout_default_is_interactive(monkeypatch):
    monkeypatch.delenv("DRAMA_IMAGE_PROVIDER_TIMEOUT_SECONDS", raising=False)

    timeout = media_provider._media_http_timeout()

    assert timeout.connect == 60.0
    assert timeout.read == 300.0
    assert timeout.write == 300.0
    assert timeout.pool == 300.0


def test_openai_image_protocol_uses_versioned_provider_base_without_appending_v1():
    provider = SimpleNamespace(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-key",
        api_format="image_http_v1",
        model_name="doubao-seedream-5-0-pro-260628",
        params_json=json.dumps({"image_protocol_id": "openai_images_generations"}),
    )

    protocol, error = media_provider._image_http_v1_protocol(provider)

    assert error is None
    assert protocol is not None
    endpoint = media_provider._image_http_v1_endpoint_for(
        provider,
        protocol,
        media_provider._image_http_v1_request_section(protocol),
    )
    assert endpoint == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    assert "/api/v3/v1/" not in endpoint


def test_openai_image_protocol_preserves_v1_when_it_is_part_of_provider_base():
    provider = SimpleNamespace(
        base_url="https://api.openai.com/v1",
        api_key="openai-key",
        api_format="image_http_v1",
        model_name="gpt-image-1",
        params_json=json.dumps({"image_protocol_id": "openai_images_generations"}),
    )

    protocol, error = media_provider._image_http_v1_protocol(provider)

    assert error is None
    assert protocol is not None
    endpoint = media_provider._image_http_v1_endpoint_for(
        provider,
        protocol,
        media_provider._image_http_v1_request_section(protocol),
    )
    assert endpoint == "https://api.openai.com/v1/images/generations"


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


@pytest.mark.asyncio
async def test_image_provider_rejects_downloaded_wrong_aspect_ratio(monkeypatch, tmp_path):
    provider = SimpleNamespace(name="fake-image", model_name="fake-model", api_format="openai")

    async def fake_get_active_provider(kind: str):
        assert kind == "image"
        return provider

    async def fake_call_image_http_v1(*args, **kwargs):
        return {"images": [{"url": "https://example.test/generated.png"}]}

    class FakeResponse:
        status_code = 200
        content = _png_header(1024, 1536)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str):
            assert url == "https://example.test/generated.png"
            return FakeResponse()

    monkeypatch.setattr(media_provider, "_get_active_provider", fake_get_active_provider)
    monkeypatch.setattr(media_provider, "_call_image_http_v1", fake_call_image_http_v1)
    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(media_provider.settings, "STORAGE_DIR", str(tmp_path), raising=False)

    result = await media_provider.generate_image_with_provider(
        project_id="project-1",
        prompt="test prompt",
        size="2560x1440",
        quality="high",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "image_size_mismatch"
    assert result["size_requested"] == "2560x1440"
    assert result["size_final"] == "1024x1536"
    assert result["actual_size"] == "1024x1536"
    assert result["provider"] == "fake-image"


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


@pytest.mark.asyncio
async def test_image_fusion_keeps_every_completed_generation_in_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = {
        "id": "node-image",
        "project_id": "project-1",
        "type": "image",
        "status": "completed",
        "prompt": "first prompt",
        "input": {"prompt": "first prompt"},
        "output": {
            "type": "fusion",
            "subject": "image",
            "stages": [
                {
                    "name": "图片",
                    "status": "completed",
                    "local_url": "/api/media/project-1/generated_images/first.png",
                }
            ],
        },
    }

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        return deepcopy(node)

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if "output_data" in patch:
            node["output"] = deepcopy(patch["output_data"])
        if "status" in patch:
            node["status"] = patch["status"]
        return deepcopy(node)

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    for index, filename in enumerate(("second.png", "third.png"), start=2):
        prompt = f"prompt {index}"
        node["prompt"] = prompt
        node["input"] = {"prompt": prompt}
        await node_universal._archive_current_media_output_for_rerun(
            "node-image",
            node,
            "image",
            node["input"],
        )
        await node_universal._merge_stage_into_fusion(
            "node-image",
            "image",
            status="running",
            prompt=prompt,
            input_data=node["input"],
        )
        await node_universal._merge_stage_into_fusion(
            "node-image",
            "image",
            status="completed",
            url=f"/api/media/project-1/generated_images/{filename}",
            local_url=f"/api/media/project-1/generated_images/{filename}",
            prompt=prompt,
            input_data=node["input"],
        )

    current_refs = media_history.collect_media_refs(node["output"])
    history = media_history.media_history_from_output(node["output"])
    history_refs = [media_history.collect_media_refs(item["output"]) for item in history]

    assert "/api/media/project-1/generated_images/third.png" in current_refs
    assert history_refs == [
        ["/api/media/project-1/generated_images/second.png"],
        ["/api/media/project-1/generated_images/first.png"],
    ]


def test_node_media_upload_classifier_matches_node_kind():
    assert routes_projects._classify_node_media_upload("frame.png", "image/png") == "image"
    assert routes_projects._classify_node_media_upload("clip.mp4", "video/mp4") == "video"
    assert routes_projects._classify_node_media_upload("clip.bin", "video/mp4") == "video"
    assert routes_projects._classify_node_media_upload("notes.txt", "text/plain") is None
    assert routes_uploads._classify("clip.mp4", "video/mp4") == "video"
    node = WorkflowNode(id="node-1", project_id="project-1", display_id=7, type="video", title="视频节点")
    assert routes_projects._safe_node_media_upload_filename(
        "clip",
        node=node,
        kind="video",
        mime_type="video/webm",
    ).endswith(".webm")


def test_uploaded_node_media_output_archives_previous_output(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"fake video")
    node = WorkflowNode(
        id="node-1",
        project_id="project-1",
        display_id=3,
        type="video",
        title="视频节点",
        status="idle",
        prompt="new prompt",
    )
    current_output = {
        "type": "video",
        "status": "completed",
        "local_url": "/api/media/project-1/generated_videos/old.mp4",
        "prompt": "old prompt",
    }
    current_input = {"prompt": "old prompt", "duration_seconds": 5}

    output = routes_projects._build_uploaded_node_media_output(
        project_id="project-1",
        node=node,
        rel_path="generated_videos/uploads/clip.mp4",
        target_path=target,
        original_filename="clip.mp4",
        mime_type="video/mp4",
        size=target.stat().st_size,
        uploaded_at="2026-06-27T00:00:00",
        current_output=current_output,
        current_input=current_input,
    )

    assert output["type"] == "video"
    assert output["status"] == "completed"
    assert output["source"] == "uploaded_node_media"
    assert output["video"]["local_url"] == "/api/media/project-1/generated_videos/uploads/clip.mp4"
    assert output["history"][0]["prompt"] == "old prompt"
    assert output["history"][0]["output"]["local_url"] == "/api/media/project-1/generated_videos/old.mp4"


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


def test_media_provider_schema_accepts_lingke_media_generate_format():
    entry = MediaProviderEntry(
        kind="video",
        name="custom-video-relay",
        base_url="https://api.lk888.ai/v1",
        api_key="relay-key",
        model_name="custom-video-model",
        api_format="lingke_media_generate",
    )

    assert entry.api_format == "lingke_media_generate"


def test_media_provider_schema_accepts_video_http_v1_format():
    entry = MediaProviderEntry(
        kind="video",
        name="seedance-http",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-key",
        model_name="doubao-seedance-2-0-260128",
        api_format="video_http_v1",
        params={"video_protocol_id": "seedance_2_0"},
    )

    assert entry.api_format == "video_http_v1"


def test_media_provider_schema_accepts_audio_http_v1_format():
    entry = MediaProviderEntry(
        kind="audio",
        name="audio-http",
        base_url="https://audio.example",
        api_key="audio-key",
        model_name="tts-1",
        api_format="audio_http_v1",
        params={"audio_protocol_id": "openai_audio_speech"},
    )

    assert entry.kind == "audio"
    assert entry.api_format == "audio_http_v1"


def test_media_provider_schema_accepts_audio_http_v1_suno_protocol():
    entry = MediaProviderEntry(
        kind="audio",
        name="suno-compatible",
        base_url="https://audio.example",
        api_key="audio-key",
        model_name="V5",
        api_format="audio_http_v1",
        params={"audio_protocol_id": "newapi_suno_music"},
    )

    assert entry.kind == "audio"
    assert entry.api_format == "audio_http_v1"


def test_audio_http_v1_payload_prefers_node_format_and_filters_music_fields():
    provider = SimpleNamespace(
        api_format="audio_http_v1",
        model_name="tts-1",
        params_json=json.dumps({
            "audio_protocol_id": "openai_audio_speech",
            "response_format": "mp3",
            "speed": 1.05,
            "custom_mode": True,
        }),
    )

    payload, meta = media_provider._build_audio_http_v1_payload(
        provider,
        prompt="旁白文本",
        title=None,
        style=None,
        instrumental=None,
        extra_override={
            "voice": "nova",
            "format": "wav",
            "instructions": "自然、清晰的旁白",
            "negative_tags": "noise",
            "_debug": "hidden",
        },
    )

    assert meta is not None
    assert meta["protocol"]["id"] == "openai_audio_speech"
    assert payload == {
        "model": "tts-1",
        "input": "旁白文本",
        "voice": "nova",
        "response_format": "wav",
        "speed": 1.05,
        "instructions": "自然、清晰的旁白",
    }


def test_audio_http_v1_newapi_suno_payload_preserves_instrumental_flag():
    provider = SimpleNamespace(
        api_format="audio_http_v1",
        model_name="V5",
        params_json=json.dumps({
            "audio_protocol_id": "newapi_suno_music",
        }),
    )

    payload, meta = media_provider._build_audio_http_v1_payload(
        provider,
        prompt="A warm cinematic pop theme",
        title="Theme",
        style="cinematic pop",
        instrumental=True,
        extra_override={"mv": "chirp-v4"},
    )

    assert meta is not None
    assert meta["protocol"]["id"] == "newapi_suno_music"
    assert payload == {
        "gpt_description_prompt": "A warm cinematic pop theme",
        "tags": "cinematic pop",
        "title": "Theme",
        "make_instrumental": True,
        "mv": "chirp-v4",
    }


@pytest.mark.asyncio
async def test_audio_provider_routes_audio_http_v1(monkeypatch):
    provider = SimpleNamespace(
        name="tts-provider",
        kind="audio",
        api_format="audio_http_v1",
        base_url="https://audio.example/v1",
        api_key="audio-key",
        model_name="tts-1",
        enabled=True,
        params_json=json.dumps({"audio_protocol_id": "openai_audio_speech"}),
    )
    captured: dict = {}

    async def fake_get_active_provider(kind: str):
        assert kind == "audio"
        return provider

    async def fake_call_audio_http_v1(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "provider": provider.name,
            "model": provider.model_name,
            "voice": kwargs["extra_override"]["voice"],
            "format": kwargs["extra_override"]["format"],
            "style": kwargs["style"],
        }

    monkeypatch.setattr(media_provider, "_get_active_provider", fake_get_active_provider)
    monkeypatch.setattr(media_provider, "_call_audio_http_v1", fake_call_audio_http_v1)

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
    assert captured["style"] == "温和"
    assert captured["extra_override"] == {"voice": "nova", "format": "wav"}


def test_audio_http_v1_response_parser_handles_newapi_suno_items():
    protocol, error = media_provider._audio_http_v1_protocol_from_catalog("newapi_suno_music")
    assert error is None
    assert protocol is not None

    items = media_provider._audio_http_v1_collect_audio_items(protocol, {
        "code": "success",
        "data": {
            "status": "SUCCESS",
            "data": [
                {
                    "id": "song-1",
                    "title": "Theme",
                    "audio_url": "https://example.com/theme.mp3",
                    "source_audio_url": "https://example.com/source.mp3",
                    "image_url": "https://example.com/theme.png",
                    "duration": 42.5,
                    "tags": "cinematic, pop",
                }
            ],
        },
    })

    assert items == [
        {
            "id": "song-1",
            "title": "Theme",
            "url": "https://example.com/theme.mp3",
            "remote_url": "https://example.com/theme.mp3",
            "source_audio_url": "https://example.com/source.mp3",
            "stream_audio_url": "https://example.com/theme.mp3",
            "image_url": "https://example.com/theme.png",
            "duration_seconds": 42.5,
            "tags": "cinematic, pop",
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
    assert "1080x1920" in result["hint"]


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

    async def fake_generate_video(**kwargs):
        captured["generate"] = kwargs
        return {
            "status": "queued",
            "provider": "stub",
            "reference_images": kwargs.get("reference_images") or [],
        }

    monkeypatch.setattr(node_universal, "_reference_images_for_video_run", fake_reference_images)
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
async def test_background_video_poll_updates_node_progress(monkeypatch):
    updates: list[dict] = []
    events: list[tuple[dict, str | None]] = []

    async def fake_poll_video_with_provider(**kwargs):
        callback = kwargs.get("progress_callback")
        assert callback is not None
        await callback({
            "job_id": "ark-task-1",
            "status": "running",
            "progress": 42,
            "poll_count": 2,
        })
        return {
            "ok": True,
            "provider": "ark-video",
            "model": "doubao-seedance-2-0-260128",
            "status": "completed",
            "job_id": "ark-task-1",
            "local_url": "/api/media/proj-1/generated_videos/video.mp4",
            "progress": 100,
        }

    async def fake_get_node(node_id: str):
        return {
            "id": node_id,
            "output": {
                "type": "video",
                "status": "running",
                "job_id": "ark-task-1",
                "history": [{"id": "history-1"}],
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        return {"id": node_id, **patch}

    async def fake_emit_canvas_event(event: dict, project_id: str | None = None):
        events.append((event, project_id))

    from app.agent import orchestrator
    from app.mcp_tools import canvas_tools

    monkeypatch.setattr(media_generation, "poll_video_with_provider", fake_poll_video_with_provider)
    monkeypatch.setattr(canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(orchestrator, "emit_canvas_event", fake_emit_canvas_event)

    await media_generation._background_video_poll(
        project_id="proj-1",
        prompt="video prompt",
        shot_id=None,
        node_id="video-1",
        model="doubao-seedance-2-0-260128",
        queued_result={
            "ok": True,
            "provider": "ark-video",
            "model": "doubao-seedance-2-0-260128",
            "status": "queued",
            "job_id": "ark-task-1",
        },
        refs_provided=[],
        first_frame_asset_id=None,
        last_frame_asset_id=None,
        duration_seconds=15,
        aspect_ratio="16:9",
        resolution="720p",
        provider_extra={},
        record_asset=False,
    )

    progress_patch = updates[0]
    assert progress_patch["status"] == "running"
    assert progress_patch["output_data"]["progress"] == 42
    assert progress_patch["output_data"]["poll_status"] == "running"
    assert progress_patch["output_data"]["poll_count"] == 2
    assert progress_patch["output_data"]["history"] == [{"id": "history-1"}]
    assert events[0][0]["payload"]["progress"] == 42
    assert events[0][1] == "proj-1"
    assert updates[-1]["status"] == "completed"


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
    assert error["supported_resolutions"] == ["480p", "720p", "1080p", "4k"]
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
async def test_json_image_url_adapters_default_to_data_url(monkeypatch):
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        params_json="{}",
    )
    captured: dict = {}

    async def fake_ref_to_data_url(ref: str):
        captured["ref"] = ref
        return "data:image/png;base64,abc"

    monkeypatch.setattr(media_provider, "_ref_to_data_url", fake_ref_to_data_url)

    image, warning = await media_provider._xai_image_input(
        "proj-1",
        "/api/media/proj-1/generated_images/source.png",
        provider,
        {},
    )

    assert warning is None
    assert captured["ref"] == "/api/media/proj-1/generated_images/source.png"
    assert image == {"url": "data:image/png;base64,abc"}


@pytest.mark.asyncio
async def test_json_image_url_adapters_can_use_public_url_mode(monkeypatch):
    monkeypatch.setenv("DRAMA_MEDIA_URL_SIGNING_SECRET", "test-only-secret")
    provider = SimpleNamespace(
        name="xai-grok-video",
        model_name="grok-imagine-video-1.5",
        params_json=json.dumps({
            "image_transport": "public_url",
            "public_base_url": "https://studio.example",
        }),
    )

    image, warning = await media_provider._xai_image_input(
        "proj-1",
        "/api/media/proj-1/generated_images/source.png",
        provider,
        {},
    )

    assert warning is None
    assert image is not None
    signed_url = image["url"]
    assert signed_url.startswith(
        "https://studio.example/api/media/proj-1/generated_images/source.png?"
    )
    assert "expires=" in signed_url
    assert "signature=" in signed_url


def test_public_url_mode_requires_public_base_for_local_media():
    url, warning = media_provider._public_media_url_for_ref(
        "proj-1",
        "/api/media/proj-1/generated_images/source.png",
        None,
    )

    assert url is None
    assert warning is not None
    assert "当前 provider 选择了公网 URL 图片输入模式" in warning


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
        params_json="{}",
    )

    adapter = media_provider._video_provider_adapter(provider)
    assert adapter is not None
    protocol, error = media_provider._video_http_v1_protocol(provider)

    assert adapter.name == "video_http_v1"
    assert error is None
    assert protocol["id"] == "t8_grok_video_3_json_task"
    assert protocol["image_transport"] == "upload_url"


def test_lingke_media_generate_adapter_uses_api_format_without_model_hardcoding():
    provider = SimpleNamespace(
        api_format="lingke_media_generate",
        model_name="custom-video-model",
        params_json="{}",
    )

    adapter = media_provider._video_provider_adapter(provider)
    assert adapter is not None
    protocol, error = media_provider._video_http_v1_protocol(provider)

    assert adapter.name == "video_http_v1"
    assert error is None
    assert protocol["id"] == "lingke_media_generate_json_task"
    assert protocol["request"]["path"] == "/media/generate"


def _seedance_video_http_protocol() -> dict[str, Any]:
    return {
        "version": "openreel.video_provider.v1",
        "display_name": "Seedance 2.0",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "image_transport": "data_url",
        "supported_ratios": ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"],
        "duration": {"min": 4, "max": 15, "allowed_values": [-1]},
        "forbidden_fields": ["seed", "frames", "camera_fixed", "draft", "service_tier"],
        "model_profiles": [
            {
                "match": "doubao-seedance-2-0-260128",
                "supported_resolutions": ["480p", "720p", "1080p", "4k"],
                "default_resolution": "720p",
            },
            {
                "match_contains": "fast",
                "supported_resolutions": ["480p", "720p"],
                "default_resolution": "720p",
            },
            {
                "match_contains": "mini",
                "supported_resolutions": ["480p", "720p"],
                "default_resolution": "720p",
            },
        ],
        "modes": {
            "text_to_video": {"prompt_required": True, "max_images": 0, "max_videos": 0, "max_audios": 0},
            "first_frame": {
                "prompt_required": False,
                "required_roles": ["first_frame"],
                "allowed_roles": ["first_frame"],
                "min_images": 1,
                "max_images": 1,
                "max_videos": 0,
                "max_audios": 0,
            },
            "first_last_frame": {
                "prompt_required": False,
                "required_roles": ["first_frame", "last_frame"],
                "allowed_roles": ["first_frame", "last_frame"],
                "min_images": 2,
                "max_images": 2,
                "max_videos": 0,
                "max_audios": 0,
            },
            "multimodal_reference": {
                "prompt_required": False,
                "allowed_roles": ["reference_image", "reference_video", "reference_audio"],
                "min_total_media": 1,
                "max_images": 9,
                "max_videos": 3,
                "max_audios": 3,
                "audio_requires_visual": True,
            },
        },
        "content": {
            "text": {"type": "text", "type_key": "type", "text_key": "text"},
            "media_types": {
                "image": {"type": "image_url", "object_key": "image_url", "url_key": "url", "role_key": "role"},
                "video": {"type": "video_url", "object_key": "video_url", "url_key": "url", "role_key": "role"},
                "audio": {"type": "audio_url", "object_key": "audio_url", "url_key": "url", "role_key": "role"},
            },
        },
        "request": {
            "method": "POST",
            "path": "/contents/generations/tasks",
            "auth": "bearer",
            "task_id_paths": ["id"],
            "body": {
                "model": "$model",
                "content": "$content",
                "duration": "$duration_seconds",
                "ratio": "$aspect_ratio",
                "resolution": "$resolution",
                "generate_audio": "$generate_audio",
                "return_last_frame": "$return_last_frame",
                "priority": "$priority",
                "safety_identifier": "$safety_identifier",
            },
        },
        "poll": {
            "method": "GET",
            "path": "/contents/generations/tasks/{task_id}",
            "status_path": "status",
            "succeeded": ["succeeded"],
            "failed": ["failed", "cancelled", "expired"],
            "running": ["queued", "running", "processing"],
        },
        "result": {
            "video_url_paths": ["content.video_url", "video_url"],
            "last_frame_url_paths": ["content.last_frame_url", "last_frame_url"],
        },
    }


def _video_http_provider(
    model_name: str = "doubao-seedance-2-0-260128",
    protocol_id: str = "seedance_2_0",
) -> SimpleNamespace:
    return SimpleNamespace(
        name="seedance-http",
        api_key="ark-key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_format="video_http_v1",
        model_name=model_name,
        params_json=json.dumps({"video_protocol_id": protocol_id}, ensure_ascii=False),
    )


def test_video_http_v1_adapter_uses_api_format():
    adapter = media_provider._video_provider_adapter(_video_http_provider())

    assert adapter is not None
    assert adapter.name == "video_http_v1"
    assert "video_http_v1" in media_provider._supported_video_api_formats()


@pytest.mark.asyncio
async def test_video_http_v1_seedance_text_to_video_payload_supports_4k():
    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider(),
        project_id="proj-1",
        prompt="雨夜里的霓虹街巷，镜头缓慢前推",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=None,
        extra_override={"aspect_ratio": "9:16", "resolution": "4k", "generate_audio": False},
    )

    assert meta["mode"] == "text_to_video"
    assert payload["model"] == "doubao-seedance-2-0-260128"
    assert payload["duration"] == 15
    assert payload["ratio"] == "9:16"
    assert payload["resolution"] == "4k"
    assert payload["generate_audio"] is False
    assert payload["content"] == [{"type": "text", "text": "雨夜里的霓虹街巷，镜头缓慢前推"}]


@pytest.mark.asyncio
async def test_video_http_v1_seedance_first_last_frame_payload():
    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider(),
        project_id="proj-1",
        prompt="从白天过渡到夜晚，保持角色位置一致",
        first_frame_url="https://example.com/first.png",
        last_frame_url="https://example.com/last.png",
        duration_seconds=8,
        reference_images=None,
        extra_override={"aspect_ratio": "16:9", "resolution": "1080p", "return_last_frame": True},
    )

    assert meta["mode"] == "first_last_frame"
    assert payload["return_last_frame"] is True
    assert payload["content"][1]["role"] == "first_frame"
    assert payload["content"][1]["image_url"]["url"] == "https://example.com/first.png"
    assert payload["content"][2]["role"] == "last_frame"
    assert payload["content"][2]["image_url"]["url"] == "https://example.com/last.png"


@pytest.mark.asyncio
async def test_video_http_v1_seedance_multimodal_payload_accepts_image_video_audio_refs():
    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider(),
        project_id="proj-1",
        prompt="参考素材的动作和节奏，生成同风格片段",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=["https://example.com/ref.png"],
        extra_override={
            "resolution": "720p",
            "reference_videos": ["https://example.com/ref.mp4"],
            "reference_audios": ["https://example.com/ref.mp3"],
        },
    )

    assert meta["mode"] == "multimodal_reference"
    media_items = payload["content"][1:]
    assert [item["type"] for item in media_items] == ["image_url", "video_url", "audio_url"]
    assert [item["role"] for item in media_items] == ["reference_image", "reference_video", "reference_audio"]


@pytest.mark.asyncio
async def test_video_http_v1_body_can_use_plain_media_url_lists(monkeypatch):
    protocol = _seedance_video_http_protocol()
    protocol["request"]["body"] = {
        "model": "$model",
        "params": {
            "prompt": "$prompt",
            "images": "$image_urls",
            "videos": "$video_urls",
            "audios": "$audio_urls",
        },
    }
    monkeypatch.setattr(
        media_provider,
        "_video_http_v1_protocol_from_catalog",
        lambda protocol_id: (protocol, None),
    )

    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider(),
        project_id="proj-1",
        prompt="plain url lists",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=["https://example.com/ref.png"],
        extra_override={
            "reference_videos": ["https://example.com/ref.mp4"],
            "reference_audios": ["https://example.com/ref.mp3"],
        },
    )

    assert meta["mode"] == "multimodal_reference"
    assert payload["params"]["images"] == ["https://example.com/ref.png"]
    assert payload["params"]["videos"] == ["https://example.com/ref.mp4"]
    assert payload["params"]["audios"] == ["https://example.com/ref.mp3"]


@pytest.mark.asyncio
async def test_video_http_v1_seedance_audio_only_reference_is_rejected():
    payload, error = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider(),
        project_id="proj-1",
        prompt="跟随音乐节奏生成视频",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=None,
        extra_override={"video_mode": "multimodal_reference", "reference_audios": ["https://example.com/ref.mp3"]},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert "音频参考" in error["error"]


@pytest.mark.asyncio
async def test_video_http_v1_seedance_fast_rejects_1080p():
    payload, error = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider("doubao-seedance-2-0-fast-260128"),
        project_id="proj-1",
        prompt="fast model",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=4,
        reference_images=None,
        extra_override={"resolution": "1080p"},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert error["supported_resolutions"] == ["480p", "720p"]


@pytest.mark.asyncio
async def test_video_http_v1_seedance_forbids_seed_field():
    payload, error = await media_provider._build_video_http_v1_payload(
        provider=_video_http_provider(),
        project_id="proj-1",
        prompt="seed should be rejected",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=4,
        reference_images=None,
        extra_override={"resolution": "720p", "seed": 123},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert "seed" in error["error"]


def test_video_http_v1_catalog_lists_migrated_video_protocols():
    catalog = media_provider.list_video_http_v1_protocol_catalog()

    assert catalog["ok"] is True
    protocol_ids = {item["id"] for item in catalog["protocols"]}
    assert {
        "seedance_2_0",
        "lingke_media_generate_json_task",
        "t8_grok_video_3_json_task",
        "xai_grok_imagine_video_1_5",
        "grok_1_5_multipart",
    }.issubset(protocol_ids)


@pytest.mark.asyncio
async def test_video_http_v1_t8_protocol_uploads_images_and_uppercases_resolution(monkeypatch):
    async def fake_upload(project_id, provider, protocol, ref):
        return f"https://files.example/{ref.rsplit('/', 1)[-1]}", None

    monkeypatch.setattr(media_provider, "_video_http_v1_upload_image_ref", fake_upload)
    provider = _video_http_provider("grok-video-3", "t8_grok_video_3_json_task")
    provider.base_url = "https://relay.example/v1"

    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Use @img1 as a character reference.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=["https://example.com/ref.png"],
        extra_override={"aspect_ratio": "9:16", "resolution": "1080p", "seed": 123},
    )

    assert meta["mode"] == "multimodal_reference"
    assert payload == {
        "model": "grok-video-3",
        "prompt": "Use @img1 as a character reference.",
        "ratio": "9:16",
        "duration": 15,
        "resolution": "1080P",
        "images": ["https://files.example/ref.png"],
        "seed": 123,
    }


@pytest.mark.asyncio
async def test_video_http_v1_t8_protocol_rejects_1080p_long_duration():
    provider = _video_http_provider("grok-video-3", "t8_grok_video_3_json_task")

    payload, error = await media_provider._build_video_http_v1_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Long duration video.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=20,
        reference_images=None,
        extra_override={"aspect_ratio": "16:9", "resolution": "1080p"},
    )

    assert payload is None
    assert error["error_kind"] == "bad_request"
    assert error["supported_resolutions"] == ["720p"]


def test_video_http_v1_duration_uses_product_default_when_config_is_missing():
    assert media_provider._video_http_v1_duration(5, {}, {}, {}) == (5, None)
    assert media_provider._video_http_v1_duration(15, {}, {}, {}) == (15, None)

    duration, error = media_provider._video_http_v1_duration(16, {}, {}, {})

    assert duration is None
    assert error == "video_http_v1 duration 只支持 5-15 秒"


@pytest.mark.asyncio
async def test_video_http_v1_xai_protocol_uses_first_image_url():
    provider = _video_http_provider("grok-imagine-video-1.5", "xai_grok_imagine_video_1_5")
    provider.base_url = "https://api.x.ai/v1"

    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate the source image with slow camera movement.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=6,
        reference_images=["https://example.com/source.png"],
        extra_override={"resolution": "720p", "seed": 42},
    )

    assert meta["mode"] == "first_frame"
    assert payload == {
        "model": "grok-imagine-video-1.5",
        "prompt": "Animate the source image with slow camera movement.",
        "image": {"url": "https://example.com/source.png"},
        "duration": 6,
        "resolution": "720p",
        "seed": 42,
    }


@pytest.mark.asyncio
async def test_video_http_v1_grok_1_5_protocol_builds_multipart_request(monkeypatch):
    async def fake_image_file(project_id, ref):
        return ("source.png", b"image-bytes", "image/png"), None

    monkeypatch.setattr(media_provider, "_image_file_input", fake_image_file)
    provider = _video_http_provider("grok-1.5-video-15s", "grok_1_5_multipart")
    provider.base_url = "https://relay.example/v1"

    payload, meta = await media_provider._build_video_http_v1_payload(
        provider=provider,
        project_id="proj-1",
        prompt="Animate the source image.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=["https://example.com/source.png"],
        extra_override={"aspect_ratio": "9:16", "resolution": "720p"},
    )

    assert payload == {
        "model": "grok-1.5-video-15s",
        "prompt": "Animate the source image.",
        "size": "720x1280",
    }
    assert meta["request"]["encoding"] == "multipart"
    assert meta["_multipart_files"]["input_reference"] == ("source.png", b"image-bytes", "image/png")


def test_video_adapter_uses_api_format_for_grok_relay_variants():
    provider = SimpleNamespace(
        api_format="t8_grok_video_3",
        model_name="grok-1.5-video-15s",
        params_json="{}",
    )

    adapter = media_provider._video_provider_adapter(provider)

    assert adapter is not None
    assert adapter.name == "video_http_v1"


def test_video_http_v1_protocol_id_is_inferred_for_legacy_video_formats():
    assert media_provider._video_http_v1_protocol_id_for_provider(
        SimpleNamespace(api_format="t8_grok_video_3", model_name="grok-video-3", params_json="{}")
    ) == "t8_grok_video_3_json_task"
    assert media_provider._video_http_v1_protocol_id_for_provider(
        SimpleNamespace(api_format="lingke_media_generate", model_name="grok-video-3", params_json="{}")
    ) == "lingke_media_generate_json_task"
    assert media_provider._video_http_v1_protocol_id_for_provider(
        SimpleNamespace(api_format="grok_1_5", model_name="grok-1.5-video-15s", params_json="{}")
    ) == "grok_1_5_multipart"


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
async def test_t8_grok_video_3_payload_preserves_configured_model_name():
    provider = SimpleNamespace(
        name="relay-grok-video",
        model_name="grok-1.5-video-15s",
        params_json="{}",
    )

    payload, image_candidates, meta = await media_provider._build_t8_grok_video_3_payload(
        provider=provider,
        project_id="proj-1",
        prompt="A cinematic establishing shot with gentle camera movement.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=10,
        reference_images=[],
        extra_override={"aspect_ratio": "16:9", "resolution": "720p"},
    )

    assert payload["model"] == "grok-1.5-video-15s"
    assert image_candidates == []
    assert meta["source_image_count"] == 0


@pytest.mark.asyncio
async def test_lingke_media_generate_payload_uses_nested_params():
    provider = SimpleNamespace(
        name="custom-video-relay",
        model_name="custom-video-model",
        params_json="{}",
    )

    payload, image_candidates, meta = await media_provider._build_json_video_task_payload(
        media_provider._LINGKE_MEDIA_GENERATE_SPEC,
        provider=provider,
        project_id="proj-1",
        prompt="A cinematic establishing shot with slow camera movement.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=[],
        extra_override={"aspect_ratio": "9:16", "resolution": "720p", "seed": 123},
    )

    assert image_candidates == []
    assert meta["source_image_count"] == 0
    assert payload == {
        "model": "custom-video-model",
        "params": {
            "prompt": "A cinematic establishing shot with slow camera movement.",
            "aspect_ratio": "9:16",
            "duration": "15",
            "resolution": "720p",
            "seed": 123,
        },
    }


@pytest.mark.asyncio
async def test_lingke_media_generate_payload_uses_configured_fields_and_custom_duration():
    provider = SimpleNamespace(
        name="custom-video-relay",
        model_name="grok-video-3",
        params_json=json.dumps(
            {
                "payload_fields": {"resolution": "params.size"},
                "resolution_output": "upper",
                "supported_ratios": ["2:3", "3:2", "1:1"],
                "default_ratio": "3:2",
                "duration_max": 60,
            }
        ),
    )

    payload, image_candidates, meta = await media_provider._build_json_video_task_payload(
        media_provider._LINGKE_MEDIA_GENERATE_SPEC,
        provider=provider,
        project_id="proj-1",
        prompt="A cinematic establishing shot with slow camera movement.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=22,
        reference_images=[],
        extra_override={"aspect_ratio": "16:9", "resolution": "720p"},
    )

    assert image_candidates == []
    assert meta["source_image_count"] == 0
    assert payload == {
        "model": "grok-video-3",
        "params": {
            "prompt": "A cinematic establishing shot with slow camera movement.",
            "aspect_ratio": "3:2",
            "duration": "22",
            "size": "720P",
        },
    }


def test_lingke_media_generate_business_error_is_classified():
    error = media_provider._json_video_task_api_error(
        media_provider._LINGKE_MEDIA_GENERATE_SPEC,
        {
            "code": 403,
            "msg": "无可用渠道分组",
            "data": {"详情": "该模型未在自定义渠道策略中配置可用渠道分组"},
        },
        "https://api.lk888.ai/v1/media/generate",
    )

    assert error is not None
    assert error["error_kind"] == "auth"
    assert "渠道" in error["error"]
    assert error["endpoint"] == "https://api.lk888.ai/v1/media/generate"


@pytest.mark.asyncio
async def test_lingke_media_generate_submit_uses_nested_params_and_data_url_default(monkeypatch):
    provider = SimpleNamespace(
        name="custom-video-relay",
        model_name="custom-video-model",
        base_url="https://api.lk888.ai/v1",
        api_key="relay-key",
        params_json=json.dumps({"resolution": "720p"}, ensure_ascii=False),
    )
    captured: dict = {}

    async def fake_ref_to_data_url(ref: str):
        captured["ref"] = ref
        return "data:image/png;base64,abc"

    class FakeResponse:
        status_code = 200
        text = '{"code":0,"data":{"task_id":"task-1","status":"pending"}}'

        def json(self):
            return {"code": 0, "data": {"task_id": "task-1", "status": "pending"}}

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

    monkeypatch.setattr(media_provider, "_ref_to_data_url", fake_ref_to_data_url)
    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._call_lingke_media_generate(
        provider=provider,
        project_id="proj-1",
        prompt="A neon-lit street scene with slow camera movement.",
        first_frame_url=None,
        last_frame_url=None,
        duration_seconds=15,
        reference_images=["/api/media/proj-1/generated_images/source.png"],
        extra_override={"aspect_ratio": "9:16"},
        save_locally=False,
        wait_for_completion=False,
    )

    assert captured["ref"] == "/api/media/proj-1/generated_images/source.png"
    assert captured["endpoint"] == "https://api.lk888.ai/v1/media/generate"
    assert captured["headers"]["Authorization"] == "Bearer relay-key"
    assert captured["json"] == {
        "model": "custom-video-model",
        "params": {
            "prompt": "A neon-lit street scene with slow camera movement.",
            "aspect_ratio": "9:16",
            "duration": "15",
            "resolution": "720p",
            "images": ["data:image/png;base64,abc"],
        },
    }
    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["job_id"] == "task-1"
    assert result["query_endpoint"] == "https://api.lk888.ai/v1/skills/task-status?task_id=task-1"
    assert result["request"]["duration"] == "15"
    assert result["request"]["ratio"] == "9:16"


@pytest.mark.asyncio
async def test_lingke_media_generate_poll_reads_task_status_success(monkeypatch):
    provider = SimpleNamespace(
        name="custom-video-relay",
        model_name="grok-video-3",
        base_url="https://api.lk888.ai/v1",
        api_key="relay-key",
        params_json="{}",
    )
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{"task_id":"task-1","state":"success","is_final":true,"result_url":"https://cdn.example.com/video.mp4","progress":"100%"}'

        def json(self):
            return {
                "task_id": "task-1",
                "model": "grok-video-3",
                "state": "success",
                "status": "生成完成",
                "status_group": "已完成",
                "is_final": True,
                "result_url": "https://cdn.example.com/video.mp4",
                "progress": "100%",
                "duration_seconds": 94,
                "cost": 0.54,
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

    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._poll_lingke_media_generate_task(
        provider=provider,
        project_id="proj-1",
        task_id="task-1",
        extra_override={},
        save_locally=False,
    )

    assert captured["endpoint"] == "https://api.lk888.ai/v1/skills/task-status?task_id=task-1"
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["remote_url"] == "https://cdn.example.com/video.mp4"
    assert result["polls"][0]["status"] == "success"
    assert result["polls"][0]["is_final"] is True


@pytest.mark.asyncio
async def test_lingke_media_generate_poll_reads_task_status_failure(monkeypatch):
    provider = SimpleNamespace(
        name="custom-video-relay",
        model_name="grok-video-3",
        base_url="https://api.lk888.ai/v1",
        api_key="relay-key",
        params_json="{}",
    )

    class FakeResponse:
        status_code = 200
        text = '{"task_id":"task-1","state":"failed","is_final":true,"error":"图像下载失败","refunded":true}'

        def json(self):
            return {
                "task_id": "task-1",
                "model": "grok-video-3",
                "state": "failed",
                "status": "生成失败",
                "status_group": "失败",
                "is_final": True,
                "progress": "100%",
                "error": "图像下载失败",
                "refunded": True,
                "refunded_amount": 0.54,
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, endpoint, headers):
            return FakeResponse()

    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)

    result = await media_provider._poll_lingke_media_generate_task(
        provider=provider,
        project_id="proj-1",
        task_id="task-1",
        extra_override={},
        save_locally=False,
    )

    assert result["error_kind"] == "provider_failed"
    assert result["status"] == "failed"
    assert result["provider_msg"] == "图像下载失败"
    assert result["raw"]["refunded"] is True


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
        base_url="https://relay.example/v2",
        api_key="relay-key",
        params_json=json.dumps({
            "resolution": "720p",
            "upload_base_url": "https://relay.example/v1",
        }, ensure_ascii=False),
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
        base_url="https://relay.example/v2",
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
            "depends_on": ["node:source-image"],
            "references": [
                {"ref": "node:storyboard-image", "role": "visual_reference"},
                {"ref": "node:source-image", "role": "context"},
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
async def test_text_runner_uses_node_model_override(monkeypatch):
    llm_calls: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []

    class FakeLLMService:
        def __init__(self, _session):
            pass

        async def generate(self, *, task_type, messages, system, project_id, node_override=None):
            llm_calls.append({
                "task_type": task_type,
                "messages": messages,
                "system": system,
                "project_id": project_id,
                "node_override": node_override,
            })
            return {
                "content": "这是模型回复正文。",
                "model": "deepseek/deepseek-chat",
                "usage": {"total_tokens": 32},
            }

    class FakeScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_reference_images(project_id: str, fields: dict[str, Any]):
        return [], [], []

    async def fake_update_node(node_id: str, patch: dict[str, Any]):
        updates.append({"node_id": node_id, **patch})
        return {"id": node_id, **patch}

    monkeypatch.setattr(node_universal, "LLMService", FakeLLMService)
    monkeypatch.setattr(node_universal, "session_scope", lambda: FakeScope())
    monkeypatch.setattr(node_universal, "_reference_image_urls_for_text_run", fake_reference_images)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal._run_text_node(
        "proj-1",
        "node-1",
        {
            "title": "文本节点",
            "prompt": "写一个开场。",
            "model": "Panel Text",
            "llm_task_type": "text_generation",
        },
    )

    assert llm_calls[0]["node_override"] == "Panel Text"
    assert llm_calls[0]["task_type"] == "text_generation"
    assert result["content"] == "这是模型回复正文。"
    assert result["model"] == "deepseek/deepseek-chat"
    assert updates[0]["input_data"]["model"] == "Panel Text"
    assert updates[0]["input_data"]["content"] == "这是模型回复正文。"


@pytest.mark.asyncio
async def test_workflow_runtime_skill_payload_prefers_compiled_prompt_template(monkeypatch):
    async def fake_load_skill(workflow: dict):
        raise AssertionError("compiled prompt_template should avoid full skill loading")

    monkeypatch.setattr(node_universal, "_load_workflow_text_skill", fake_load_skill)

    payload = await node_universal._workflow_runtime_skill_payload(
        {"primary_skill": "script_writing", "skill_category": "prompt", "skill_scope": "builtin"},
        {"prompt_template": "SYSTEM: 写剧本", "rendered_prompt_template": "SYSTEM: 写剧本"},
    )

    assert payload == {
        "name": "script_writing",
        "category": "prompt",
        "scope": "builtin",
        "content": "",
        "content_mode": "compiled_prompt_template",
        "load_error": None,
    }


@pytest.mark.asyncio
async def test_workflow_runtime_skill_payload_loads_skill_only_as_legacy_fallback(monkeypatch):
    calls: list[dict] = []

    async def fake_load_skill(workflow: dict):
        calls.append(workflow)
        return {
            "ok": True,
            "name": "legacy_prompt",
            "category": "prompt",
            "scope": "user",
            "content": "旧节点提示词写法",
        }

    monkeypatch.setattr(node_universal, "_load_workflow_text_skill", fake_load_skill)

    payload = await node_universal._workflow_runtime_skill_payload(
        {"primary_skill": "legacy_prompt", "skill_category": "prompt", "skill_scope": "user"},
        {"prompt_template": "", "rendered_prompt_template": ""},
    )

    assert len(calls) == 1
    assert payload["name"] == "legacy_prompt"
    assert payload["content"] == "旧节点提示词写法"
    assert payload["content_mode"] == "fallback_skill_content"


@pytest.mark.asyncio
async def test_node_run_workflow_text_node_uses_one_shot_llm(monkeypatch):
    updates: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] = []
    nodes = {
        "script-1": {
            "id": "script-1",
            "display_id": 2,
            "project_id": "proj-1",
            "type": "text",
            "title": "剧本",
            "status": "idle",
            "input": {
                "title": "剧本",
                "content": "待写剧本。",
                "references": [{"ref": "node:1", "role": "context"}],
                "workflow": {
                    "step_id": "script",
                    "prompt_ref": "script_writing#script",
                    "prompt_spec": {"output": "fields.content"},
                    "prompt_template": "SYSTEM: 剧本写作者\nUSER: 主题={{inputs.plot}}；需求={{brief.output.content}}",
                    "primary_skill": "script_writing",
                    "skill_category": "prompt",
                    "acceptance": "写出可用于后续分镜的剧本。",
                    "input_facts": {"plot": "江湖雨夜相逢"},
                },
            },
            "prompt": "",
        },
        "brief-1": {
            "id": "brief-1",
            "display_id": 1,
            "project_id": "proj-1",
            "type": "text",
            "title": "制作需求",
            "status": "completed",
            "input": {
                "content": "江湖雨夜相逢，15秒。",
                "workflow": {"step_id": "brief"},
            },
            "output": {"content": "江湖雨夜相逢，15秒。"},
            "prompt": "",
        },
    }

    async def fake_resolve(project_id: str, node_id: str):
        assert project_id == "proj-1"
        return {"script-1": "script-1", "node:1": "brief-1", "1": "brief-1"}.get(str(node_id), str(node_id))

    async def fake_get_node(node_id: str):
        return nodes[node_id]

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        if "input_data" in patch:
            nodes[node_id]["input"] = patch["input_data"]
        if "status" in patch:
            nodes[node_id]["status"] = patch["status"]
        if "output_data" in patch:
            nodes[node_id]["output"] = patch["output_data"]
        return {"id": node_id, **patch}

    async def fake_load_skill(workflow: dict):
        raise AssertionError("prompt_template nodes should not reload full prompt skill at runtime")

    async def fake_call_llm(**kwargs):
        llm_calls.append(kwargs)
        assert kwargs["task_type"] == "workflow_text_generation"
        assert "剧本写法 skill 正文" not in kwargs["message"]
        assert "江湖雨夜相逢" in kwargs["message"]
        assert "rendered_prompt_template" in kwargs["message"]
        assert "主题=江湖雨夜相逢" in kwargs["message"]
        assert "需求=江湖雨夜相逢，15秒。" in kwargs["message"]
        payload = json.loads(kwargs["message"])
        assert payload["skill"]["name"] == "script_writing"
        assert payload["skill"]["content"] == ""
        assert payload["skill"]["content_mode"] == "compiled_prompt_template"
        return {"content": "生成的剧本正文", "model": "test-model", "usage": {"total_tokens": 42}}

    async def fake_public_id_map(project_id: str):
        assert project_id == "proj-1"
        return {"script-1": "2", "brief-1": "1"}

    monkeypatch.setattr(node_universal, "_resolve_agent_node_id", fake_resolve)
    monkeypatch.setattr(node_universal, "_node_public_id_map", fake_public_id_map)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_load_workflow_text_skill", fake_load_skill)
    monkeypatch.setattr(node_universal, "_call_workflow_text_llm", fake_call_llm)

    result = await node_universal.node_run(project_id="proj-1", node_id="script-1")

    assert result["ok"] is True
    assert result["type"] == "text"
    assert result["result"]["workflow_text_runner"] == "one_shot_llm"
    assert result["result"]["content"] == "生成的剧本正文"
    assert len(llm_calls) == 1
    input_update = next(update["input_data"] for update in updates if "input_data" in update)
    assert input_update["content"] == "生成的剧本正文"
    assert input_update["workflow"]["runner"] == "node.run"
    assert input_update["workflow"]["last_run"]["status"] == "completed"
    assert input_update["workflow"]["last_run"]["model"] == "test-model"
    assert input_update["workflow"]["last_run"]["usage_total_tokens"] == 42
    assert input_update["workflow"]["last_run"]["prompt_dump_run_id"].startswith("workflow_text_")
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["output_data"]["workflow_text_runner"] == "one_shot_llm"
    assert updates[-1]["output_data"]["prompt_dump_run_id"].startswith("workflow_text_")


@pytest.mark.asyncio
async def test_node_run_workflow_text_node_regenerates_stale_content(monkeypatch):
    updates: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] = []
    nodes = {
        "script-1": {
            "id": "script-1",
            "display_id": 1,
            "project_id": "proj-1",
            "type": "text",
            "title": "剧本",
            "status": "completed",
            "input": {
                "title": "剧本",
                "content": "旧剧本正文",
                "workflow": {
                    "step_id": "script",
                    "prompt_template": "SYSTEM: 新剧本模板\nUSER: {{inputs.plot}}",
                    "input_facts": {"plot": "雨夜怀表"},
                    "stale": True,
                },
            },
            "prompt": "",
        },
    }

    async def fake_resolve(project_id: str, node_id: str):
        assert project_id == "proj-1"
        return str(node_id)

    async def fake_get_node(node_id: str):
        return nodes[node_id]

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        if "input_data" in patch:
            nodes[node_id]["input"] = patch["input_data"]
        if "status" in patch:
            nodes[node_id]["status"] = patch["status"]
        if "output_data" in patch:
            nodes[node_id]["output"] = patch["output_data"]
        return {"id": node_id, **patch}

    async def fake_call_llm(**kwargs):
        llm_calls.append(kwargs)
        assert "旧剧本正文" in kwargs["message"]
        assert "新剧本模板" in kwargs["message"]
        return {"content": "新剧本正文", "model": "test-model", "usage": {"total_tokens": 11}}

    async def fake_public_id_map(project_id: str):
        assert project_id == "proj-1"
        return {"script-1": "1"}

    monkeypatch.setattr(node_universal, "_resolve_agent_node_id", fake_resolve)
    monkeypatch.setattr(node_universal, "_node_public_id_map", fake_public_id_map)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_call_workflow_text_llm", fake_call_llm)

    result = await node_universal.node_run(project_id="proj-1", node_id="script-1")

    assert result["ok"] is True
    assert result["result"]["content"] == "新剧本正文"
    assert len(llm_calls) == 1
    input_update = next(update["input_data"] for update in updates if "input_data" in update)
    assert input_update["content"] == "新剧本正文"
    assert input_update["workflow"]["stale"] is False
    assert input_update["workflow"]["last_run"]["usage_total_tokens"] == 11


@pytest.mark.asyncio
async def test_node_run_workflow_image_node_renders_existing_prompt_without_llm(monkeypatch):
    updates: list[dict] = []
    llm_calls: list[dict] = []
    render_calls: list[dict] = []
    nodes = {
        "scene-image-1": {
            "id": "scene-image-1",
            "display_id": 3,
            "project_id": "proj-1",
            "type": "image",
            "title": "场景参考图",
            "status": "idle",
            "input": {
                "title": "场景参考图",
                "prompt": "16:9 cinematic scene reference, rainy stone bridge, lanterns, wet bluestone, no characters",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "references": [{"ref": "node:2", "role": "context"}],
                "workflow": {
                    "step_id": "scene_reference",
                    "prompt_template": "SYSTEM: 场景概念图提示词编写者\nUSER: {{scene.output}}",
                    "primary_skill": "scene_prompt",
                    "skill_category": "prompt",
                    "acceptance": "生成无人物场景参考图。",
                },
            },
            "prompt": "",
        },
        "scene-text-1": {
            "id": "scene-text-1",
            "display_id": 2,
            "project_id": "proj-1",
            "type": "text",
            "title": "场景集合",
            "status": "completed",
            "input": {
                "content": "雨夜石桥，灯笼，湿润青石。",
                "workflow": {"step_id": "scene"},
            },
            "output": {"content": "雨夜石桥，灯笼，湿润青石。"},
            "prompt": "",
        },
    }

    async def fake_resolve(project_id: str, node_id: str):
        assert project_id == "proj-1"
        return {"scene-image-1": "scene-image-1", "node:2": "scene-text-1", "2": "scene-text-1"}.get(str(node_id), str(node_id))

    async def fake_public_id_map(project_id: str):
        assert project_id == "proj-1"
        return {"scene-image-1": "3", "scene-text-1": "2"}

    async def fake_get_node(node_id: str):
        return nodes[node_id]

    async def fake_update_node(node_id: str, patch: dict):
        updates.append(patch)
        if "input_data" in patch:
            nodes[node_id]["input"] = patch["input_data"]
        if "prompt" in patch:
            nodes[node_id]["prompt"] = patch["prompt"]
        if "status" in patch:
            nodes[node_id]["status"] = patch["status"]
        if "output_data" in patch:
            nodes[node_id]["output"] = patch["output_data"]
        return {"id": node_id, **patch}

    async def fake_load_skill(workflow: dict):
        raise AssertionError("image workflow nodes should not load prompt skills at node.run time")

    async def fake_call_llm(**kwargs):
        llm_calls.append(kwargs)
        raise AssertionError("image workflow nodes should not call LLM at node.run time")

    async def fake_render(project_id: str, node_id: str, fields: dict, node_type: str):
        render_calls.append({"project_id": project_id, "node_id": node_id, "fields": dict(fields), "node_type": node_type})
        return {"url": "/storage/scene.png", "local_url": "/storage/scene.png", "size": "2560x1440", "aspect_ratio": "16:9"}

    async def fake_merge(*args, **kwargs):
        return {"type": "fusion", "stages": [{"name": "图片", **kwargs}]}

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr(node_universal, "_resolve_agent_node_id", fake_resolve)
    monkeypatch.setattr(node_universal, "_node_public_id_map", fake_public_id_map)
    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(node_universal, "_load_workflow_text_skill", fake_load_skill)
    monkeypatch.setattr(node_universal, "_call_workflow_text_llm", fake_call_llm)
    monkeypatch.setattr(node_universal, "_render_image_node_once", fake_render)
    monkeypatch.setattr(node_universal, "_merge_stage_into_fusion", fake_merge)
    monkeypatch.setattr(node_universal, "_emit_fusion_canvas_event", fake_emit)

    result = await node_universal.node_run(project_id="proj-1", node_id="scene-image-1", action="render")

    assert result["ok"] is True
    assert result["type"] == "image"
    assert len(llm_calls) == 0
    assert len(render_calls) == 1
    assert render_calls[0]["fields"]["prompt"].startswith("16:9 cinematic scene reference")


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
    assert result["call_example"]["args"]["fields"]["resolution"] == "1080x1920"
    assert "prompt_source" in result["optional_fields"]
    assert "prompt_template" not in result["optional_fields"]
    assert "template_selection_reason" not in result["optional_fields"]
    guidance_text = str(result["prompt_guidance"])
    assert "当前 skill" in guidance_text
    assert "最终图片 prompt" in guidance_text
    assert "精确像素" in guidance_text
    assert "1080x1920" in guidance_text
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
    assert result["input_json"]["prompt_preview"] == "两位女修士一白一玄，剑光更清晰。"
    assert result["input_json"]["aspect_ratio"] == "16:9"
    assert updates == [
        {
            "title": "人物参考图·一白一玄",
            "prompt": "两位女修士一白一玄，剑光更清晰。",
                "input_json": {
                    "title": "人物参考图·一白一玄",
                    "prompt": "两位女修士一白一玄，剑光更清晰。",
                    "prompt_preview": "两位女修士一白一玄，剑光更清晰。",
                    "aspect_ratio": "16:9",
                    "render_state": "stale",
                },
            }
        ]
    assert result["render_state"] == "stale"
    assert result["requires_rerun"] is True


@pytest.mark.asyncio
async def test_node_update_prompt_syncs_workflow_text_prompt_template(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "script-1"
        return {
            "id": node_id,
            "type": "text",
            "status": "completed",
            "title": "剧本文本",
            "prompt": "",
            "input": {
                "title": "剧本文本",
                "workflow": {
                    "template_id": "story_flow",
                    "instance_id": "wf_1",
                    "step_id": "script",
                    "runner": "node.run",
                    "prompt_template": "SYSTEM: 原模板",
                },
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        assert node_id == "script-1"
        updates.append(patch)
        return {
            "id": node_id,
            "type": "text",
            "status": "completed",
            "title": "剧本文本",
            "prompt": patch.get("prompt"),
            "input_json": patch.get("input_json", {}),
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        node_id="script-1",
        patch={"prompt": "SYSTEM: 当前实例强化模板\nUSER: {{inputs.plot}}\nOUTPUT: text"},
    )

    workflow = result["input_json"]["workflow"]
    assert workflow["prompt_template"].startswith("SYSTEM: 当前实例强化模板")
    assert workflow["step_id"] == "script"
    assert workflow["runner"] == "node.run"
    assert workflow["stale"] is True
    assert result["input_json"]["prompt_status"] == "stale"
    assert result["requires_rerun"] is True
    assert result["input_json"]["prompt_preview"].startswith("SYSTEM: 当前实例强化模板")
    assert updates[0]["input_json"]["workflow"]["template_id"] == "story_flow"
    assert updates[0]["input_json"]["workflow"]["stale"] is True


@pytest.mark.asyncio
async def test_node_update_merges_partial_workflow_input_patch(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "script-1"
        return {
            "id": node_id,
            "type": "text",
            "status": "completed",
            "title": "剧本文本",
            "input": {
                "title": "剧本文本",
                "workflow": {
                    "template_id": "story_flow",
                    "instance_id": "wf_1",
                    "step_id": "script",
                    "runner": "node.run",
                    "prompt_template": "SYSTEM: 原模板",
                },
            },
        }

    async def fake_update_node(node_id: str, patch: dict):
        assert node_id == "script-1"
        updates.append(patch)
        return {
            "id": node_id,
            "type": "text",
            "status": "completed",
            "title": "剧本文本",
            "input_json": patch.get("input_json", {}),
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        node_id="script-1",
        patch={"input_json": {"workflow": {"prompt_template": "SYSTEM: 局部模板"}}},
    )

    workflow = result["input_json"]["workflow"]
    assert workflow["prompt_template"] == "SYSTEM: 局部模板"
    assert workflow["template_id"] == "story_flow"
    assert workflow["instance_id"] == "wf_1"
    assert workflow["step_id"] == "script"
    assert workflow["runner"] == "node.run"
    assert updates[0]["input_json"]["workflow"] == workflow


@pytest.mark.asyncio
async def test_node_update_does_not_mark_unrendered_image_draft_stale(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "status": "idle",
            "title": "分镜图",
            "prompt": "",
            "input": {
                "title": "分镜图",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
            },
            "output": None,
        }

    async def fake_update_node(node_id: str, patch: dict):
        assert node_id == "image-1"
        updates.append(patch)
        return {
            "id": node_id,
            "type": "image",
            "status": "idle",
            "title": "分镜图",
            "prompt": patch.get("prompt"),
            "input_json": patch.get("input_json", {}),
        }

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(node_universal.canvas_tools, "update_node", fake_update_node)

    result = await node_universal.node_update(
        node_id="image-1",
        patch={"prompt": "新的分镜图提示词"},
    )

    assert result["input_json"]["prompt"] == "新的分镜图提示词"
    assert "render_state" not in result["input_json"]
    assert "render_state" not in result
    assert "requires_rerun" not in result
    assert updates == [
        {
            "prompt": "新的分镜图提示词",
            "input_json": {
                "title": "分镜图",
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "prompt": "新的分镜图提示词",
                "prompt_preview": "新的分镜图提示词",
            },
        }
    ]


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
                },
                "status": "idle",
                "error_message": None,
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
async def test_cleanup_interrupted_media_nodes_marks_running_stage_failed(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'node-recovery.db'}", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def fake_session_scope():
        async with session_local() as session:
            yield session

    monkeypatch.setattr(node_recovery, "session_scope", fake_session_scope)

    old_time = datetime.utcnow() - timedelta(seconds=3600)
    output = {
        "type": "fusion",
        "status": "running",
        "stages": [
            {"name": "提示词", "status": "completed", "text": "prompt"},
            {"name": "图片", "status": "running", "job_id": "job-1"},
        ],
    }
    async with session_local() as session:
        session.add(Project(id="proj-recovery", title="恢复测试", state_json="{}"))
        session.add(WorkflowNode(
            id="image-running",
            project_id="proj-recovery",
            display_id=1,
            type="image",
            title="卡住的图片",
            status="running",
            output_json=json.dumps(output, ensure_ascii=False),
            updated_at=old_time,
        ))
        await session.commit()

    result = await node_recovery.cleanup_interrupted_media_nodes(
        project_id="proj-recovery",
        stale_after_seconds=60,
        reason="test_interrupted_media",
    )

    assert result["changed"] == 1
    assert result["failed"] == 1
    async with session_local() as session:
        node = await session.get(WorkflowNode, "image-running")
        assert node is not None
        assert node.status == "failed"
        assert "无法继续接收" in (node.error_message or "")
        next_output = json.loads(node.output_json or "{}")
        assert next_output["status"] == "failed"
        assert next_output["error_kind"] == "test_interrupted_media"
        assert next_output["stages"][0]["status"] == "completed"
        assert next_output["stages"][1]["status"] == "failed"


@pytest.mark.asyncio
async def test_running_fusion_stage_preserves_last_successful_image(monkeypatch):
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
    assert stage["url"] == "/api/media/project/old.png"
    assert stage["local_url"] == "/api/media/project/old.png"
    assert stage["remote_url"] == "https://example.test/old.png"
    assert "error" not in stage
    assert updates == [{"output_data": fusion}]


@pytest.mark.asyncio
async def test_merge_stage_into_fusion_recovers_failed_media_from_history(monkeypatch):
    updates: list[dict] = []

    async def fake_get_node(node_id: str):
        assert node_id == "image-1"
        return {
            "id": node_id,
            "type": "image",
            "output": {
                "type": "fusion",
                "subject": "image",
                "status": "running",
                "stages": [
                    {
                        "name": "提示词",
                        "status": "completed",
                        "text": "prompt",
                    },
                ],
                "history": [
                    {
                        "id": "hist-1",
                        "output": {
                            "type": "image",
                            "status": "completed",
                            "local_url": "/api/media/project/history.png",
                        },
                    },
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
        status="failed",
        error="bad response body",
        prompt="测试",
        size="1080x1920",
    )

    assert len(fusion["stages"]) == 2
    stage = fusion["stages"][-1]
    assert stage["name"] == "图片"
    assert stage["status"] == "failed"
    assert stage["local_url"] == "/api/media/project/history.png"
    assert stage["error"] == "bad response body"
    assert updates and updates[-1]["output_data"] == fusion


@pytest.mark.asyncio
async def test_merge_stage_into_fusion_recovers_failed_media_from_composite_url(monkeypatch):
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
                        "name": "结果",
                        "status": "completed",
                        "composite_url": "/api/media/project/legacy-composite.png",
                    },
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
        status="failed",
        error="bad response body",
        prompt="测试",
        size="1080x1920",
    )

    stage = fusion["stages"][0]
    assert stage["name"] == "图片"
    assert stage["status"] == "failed"
    assert stage["url"] == "/api/media/project/legacy-composite.png"
    assert stage["error"] == "bad response body"
    assert updates and updates[-1]["output_data"] == fusion


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
