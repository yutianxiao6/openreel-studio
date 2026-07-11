from __future__ import annotations

import json
import asyncio
from copy import deepcopy
from typing import Any

import pytest

from app.agent import canvas_workflow_templates, workflow_spec_artifacts, workflow_template_store
from app.mcp_tools import agent_tools, node_universal, skill_tools, tool_meta_tools, workflow_spec_tools, workflow_tools


def install_fake_workflow_runtime_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {}

    async def fake_read_project_state(project_id: str) -> dict[str, Any]:
        return state

    async def fake_write_project_state_patch(project_id: str, patch: dict[str, Any]) -> None:
        state.update(patch)

    monkeypatch.setattr(workflow_tools, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", fake_write_project_state_patch)
    return state


async def fake_noop_sync_dependency_edges(project_id: str, node_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "changed": False, "added_edges": [], "removed_edges": []}


@pytest.fixture(autouse=True)
def isolate_workflow_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_workflow_runtime_state(monkeypatch)


@pytest.mark.asyncio
async def test_workflow_run_persists_active_workflow_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)

    await workflow_tools._persist_active_workflow_for_run(
        project_id="proj-1",
        template={"id": "grid_storyboard_workflow", "name": "宫格分镜"},
        artifact_ref="workflow_spec:demo.json",
        title="导入的宫格分镜",
    )
    assert state["active_workflow"]["kind"] == "artifact"
    assert state["active_workflow"]["artifact_ref"] == "workflow_spec:demo.json"

    await workflow_tools._persist_active_workflow_for_run(
        project_id="proj-1",
        template={"id": "general_short_drama_workflow", "name": "通用短剧制作工作流"},
        template_id="general_short_drama_workflow",
    )
    assert state["active_workflow"]["kind"] == "template"
    assert state["active_workflow"]["template_id"] == "general_short_drama_workflow"

    workflow = {
        "schema": "openreel.workflow.authoring.v1",
        "id": "custom_flow",
        "name": "自定义流程",
        "steps": [{"id": "script", "kind": "text", "title": "剧本"}],
    }
    await workflow_tools._persist_active_workflow_for_run(
        project_id="proj-1",
        template={"id": "custom_flow", "name": "自定义流程"},
        workflow=workflow,
    )
    assert state["active_workflow"]["kind"] == "imported"
    assert state["active_workflow"]["workflow"] == workflow


@pytest.mark.asyncio
async def test_workflow_runtime_delete_instance_removes_only_target(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_keep": {
                "template_id": "flow",
                "steps": {
                    "script": {"title": "剧本", "status": "completed"},
                },
            },
            "wf_delete": {
                "template_id": "flow",
                "steps": {
                    "brief": {"title": "需求", "status": "completed"},
                },
            },
        },
    }

    result = await workflow_tools.workflow_runtime_delete_instance("proj-1", "wf_delete")

    assert result["ok"] is True
    assert result["deleted"] is True
    assert "wf_delete" not in state["workflow_runtime"]["instances"]
    assert "wf_keep" in state["workflow_runtime"]["instances"]
    assert [item["instance_id"] for item in result["active_workflow_runtimes"]] == ["wf_keep"]


@pytest.mark.asyncio
async def test_workflow_runtime_request_pause_marks_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_pause": {
                "instance_id": "wf_pause",
                "template_id": "flow",
                "run_all_active": True,
                "steps": {
                    "script": {"id": "script", "title": "剧本", "status": "running"},
                },
            },
        },
    }

    result = await workflow_tools.workflow_runtime_request_pause(
        "proj-1",
        "wf_pause",
        template_id="flow",
        reason="user_requested",
    )

    instance = state["workflow_runtime"]["instances"]["wf_pause"]
    assert result["ok"] is True
    assert result["pause_requested"] is True
    assert result["runtime"]["status"] == "pause_requested"
    assert instance["pause_requested"] is True
    assert instance["pause_reason"] == "user_requested"


@pytest.mark.asyncio
async def test_workflow_runtime_request_pause_without_active_run_settles_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_pause": {
                "instance_id": "wf_pause",
                "template_id": "flow",
                "steps": {
                    "script": {"id": "script", "title": "剧本", "status": "running"},
                },
            },
        },
    }

    result = await workflow_tools.workflow_runtime_request_pause(
        "proj-1",
        "wf_pause",
        template_id="flow",
        reason="user_requested",
    )

    instance = state["workflow_runtime"]["instances"]["wf_pause"]
    assert result["ok"] is True
    assert result["pause_requested"] is False
    assert result["paused"] is True
    assert result["runtime"]["status"] == "paused"
    assert instance["pause_requested"] is False
    assert instance["status"] == "paused"
    assert instance["steps"]["script"]["status"] == "idle"


def test_workflow_runtime_public_payload_settles_stale_pause_request() -> None:
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_pause": {
                    "instance_id": "wf_pause",
                    "template_id": "flow",
                    "pause_requested": True,
                    "status": "pause_requested",
                    "steps": {
                        "script": {"id": "script", "title": "剧本", "status": "running"},
                    },
                },
            },
        },
    }

    payload = workflow_tools.workflow_runtime_public_payload(
        state,
        template_id="flow",
        instance_id="wf_pause",
    )

    assert payload["status"] == "paused"
    assert payload["pause_requested"] is False
    assert payload["progress"]["running"] == 0
    assert payload["steps"][0]["status"] == "idle"


def test_workflow_input_values_are_scoped_to_runtime_instance() -> None:
    state = {
        "workflow_input_values": {
            "by_workflow": {
                "flow": {
                    "workflow_id": "flow",
                    "values": {"plot": "旧剧情"},
                },
            },
            "by_instance": {
                "wf_new": {
                    "workflow_id": "flow",
                    "instance_id": "wf_new",
                    "values": {"plot": "新剧情"},
                },
            },
        },
        "workflow_runtime": {
            "instances": {
                "wf_new": {
                    "instance_id": "wf_new",
                    "template_id": "flow",
                    "steps": {
                        "input": {"id": "input", "title": "输入", "status": "idle"},
                    },
                },
            },
        },
    }

    assert workflow_tools.workflow_input_values_public_payload(
        state,
        workflow_id="flow",
        instance_id="wf_empty",
    ) == {}
    assert workflow_tools.workflow_input_values_public_payload(
        state,
        workflow_id="flow",
        instance_id="wf_new",
    ) == {"plot": "新剧情"}

    payload = workflow_tools.workflow_runtime_public_payload(
        state,
        template_id="flow",
        instance_id="wf_new",
    )
    assert payload["input_values"] == {"plot": "新剧情"}


def test_workflow_runtime_public_payload_treats_completed_repeat_group_as_dependency_done() -> None:
    payload = workflow_tools._workflow_runtime_payload_with_graph_state({
        "steps": [
            {
                "id": "asset_group_i1_prompt",
                "status": "completed",
                "workflow": {"repeat_group_id": "asset_group"},
            },
            {
                "id": "asset_group_i1_image",
                "status": "completed",
                "workflow": {"repeat_group_id": "asset_group"},
            },
            {
                "id": "downstream",
                "status": "idle",
                "depends_on": ["asset_group"],
            },
        ],
    })

    downstream = next(step for step in payload["steps"] if step["id"] == "downstream")
    assert downstream["waiting_on"] == []
    assert downstream["execution_state"] == "ready"
    assert payload["progress"]["ready"] == 1


def test_workflow_runtime_public_payload_ignores_stale_instance_failed_without_failed_steps() -> None:
    payload = workflow_tools.workflow_runtime_public_payload(
        {
            "workflow_runtime": {
                "instances": {
                    "wf_retry": {
                        "instance_id": "wf_retry",
                        "template_id": "retry_flow",
                        "status": "failed",
                        "steps": {
                            "script": {"id": "script", "title": "剧本", "status": "completed"},
                            "image": {"id": "image", "title": "图片", "status": "idle"},
                        },
                    },
                },
            },
        },
        template_id="retry_flow",
        instance_id="wf_retry",
    )

    assert payload["status"] == "partial"
    assert payload["progress"]["failed"] == 0


def test_workflow_dependency_completed_for_batch_uses_runtime_repeat_group_records() -> None:
    records_by_step = {
        "asset_group_i1_prompt": {
            "status": "completed",
            "workflow": {"repeat_group_id": "asset_group"},
        },
        "asset_group_i1_image": {
            "status": "completed",
            "workflow": {"repeat_group_id": "asset_group"},
        },
    }

    assert workflow_tools._workflow_dependency_completed_for_batch(
        "asset_group",
        records_by_step=records_by_step,
        steps_by_id={},
        virtual_step_ids=set(),
        failed_step_ids=set(),
    ) is True

    records_by_step["asset_group_i2_image"] = {
        "status": "idle",
        "workflow": {"repeat_group_id": "asset_group"},
    }
    assert workflow_tools._workflow_dependency_completed_for_batch(
        "asset_group",
        records_by_step=records_by_step,
        steps_by_id={},
        virtual_step_ids=set(),
        failed_step_ids=set(),
    ) is False


@pytest.mark.asyncio
async def test_workflow_runtime_clear_pause_state_resets_interrupted_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_pause": {
                "instance_id": "wf_pause",
                "template_id": "flow",
                "pause_requested": True,
                "status": "pause_requested",
                "steps": {
                    "script": {"id": "script", "title": "剧本", "status": "running"},
                },
            },
        },
    }

    await workflow_tools._workflow_runtime_clear_pause_state("proj-1", "wf_pause")

    instance = state["workflow_runtime"]["instances"]["wf_pause"]
    assert "pause_requested" not in instance
    assert "status" not in instance
    assert instance["steps"]["script"]["status"] == "idle"


@pytest.mark.asyncio
async def test_workflow_runtime_step_upsert_emits_refresh_event(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    emitted: list[tuple[dict[str, Any], str | None]] = []

    async def fake_emit_canvas_event(event: dict[str, Any], project_id: str | None = None) -> None:
        emitted.append((event, project_id))

    from app.agent import orchestrator

    monkeypatch.setattr(orchestrator, "emit_canvas_event", fake_emit_canvas_event)

    record = await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template={"id": "general_short_drama_workflow", "name": "通用视频制作工作流"},
        instance_id="wf_demo",
        step_id="script",
        node_type="text",
        title="剧本",
        fields={"workflow": {"surface": "draft_canvas"}},
        status="running",
    )

    assert record["status"] == "running"
    assert state["workflow_runtime"]["instances"]["wf_demo"]["steps"]["script"]["status"] == "running"
    assert len(emitted) == 1
    event, emitted_project_id = emitted[0]
    assert emitted_project_id == "proj-1"
    assert event["type"] == "canvas_action"
    assert event["action"] == "workflow_runtime_update"
    payload = event["payload"]
    assert payload["project_id"] == "proj-1"
    assert payload["template_id"] == "general_short_drama_workflow"
    assert payload["instance_id"] == "wf_demo"
    assert payload["step_id"] == "script"
    assert payload["status"] == "running"
    assert payload["runtime"]["instance_id"] == "wf_demo"
    runtime_steps = {step["id"]: step for step in payload["runtime"]["steps"]}
    assert runtime_steps["script"]["status"] == "running"


def test_builtin_workflow_templates_use_protocol_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        workflow_template_store,
        "workflow_template_library_root",
        lambda: tmp_path / "workflow_templates",
    )
    summaries = canvas_workflow_templates.list_template_summaries()
    ids = {item["id"] for item in summaries if item["scope"] == "builtin"}

    assert "short_video_canvas_workflow" not in ids
    assert "general_short_drama_workflow" in ids
    assert canvas_workflow_templates.get_template()["id"] == "general_short_drama_workflow"
    for template in summaries:
        if template["scope"] != "builtin":
            continue
        assert template["workflow_spec_version"] == "openreel.workflow.v1"
        assert "core.prompt_template" in template["required_capabilities"]


def test_user_template_shadows_builtin_template_with_same_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    template_root = tmp_path / "workflow_templates"
    monkeypatch.setattr(workflow_template_store, "workflow_template_library_root", lambda: template_root)
    workflow_template_store.save_user_template(
        workflow={
            "id": "general_short_drama_workflow",
            "name": "我的通用视频流程",
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "runner": "node.run",
                    "prompt_template": "根据输入生成剧本。",
                }
            ],
        },
        template_id="general_short_drama_workflow",
        replace_existing=False,
    )

    summaries = [
        item
        for item in canvas_workflow_templates.list_template_summaries()
        if item["id"] == "general_short_drama_workflow"
    ]

    assert len(summaries) == 1
    assert summaries[0]["scope"] == "user"
    assert summaries[0]["overrides_builtin"] is True
    assert summaries[0]["name"] == "我的通用视频流程"
    loaded = canvas_workflow_templates.get_template("general_short_drama_workflow")
    assert loaded["scope"] == "user"
    assert loaded["overrides_builtin"] is True
    assert loaded["name"] == "我的通用视频流程"

    deleted = workflow_template_store.delete_user_template("general_short_drama_workflow")

    assert deleted["ok"] is True
    assert not (template_root / "general_short_drama_workflow.json").exists()
    restored = canvas_workflow_templates.get_template("general_short_drama_workflow")
    assert restored["scope"] == "builtin"
    restored_summaries = [
        item
        for item in canvas_workflow_templates.list_template_summaries()
        if item["id"] == "general_short_drama_workflow"
    ]
    assert len(restored_summaries) == 1
    assert restored_summaries[0]["scope"] == "builtin"


def test_replacing_current_user_template_does_not_create_suffixed_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    template_root = tmp_path / "workflow_templates"
    monkeypatch.setattr(workflow_template_store, "workflow_template_library_root", lambda: template_root)
    workflow = {
        "id": "editable_story_flow",
        "name": "可编辑剧情流程",
        "steps": [
            {
                "id": "script",
                "title": "剧本",
                "node_type": "text",
                "runner": "node.run",
                "prompt_template": "生成第一版剧本。",
            }
        ],
    }
    workflow_template_store.save_user_template(
        workflow=workflow,
        template_id="editable_story_flow",
    )
    updated = deepcopy(workflow)
    updated["steps"][0]["prompt_template"] = "生成修改后的剧本。"

    saved = workflow_template_store.save_user_template(
        workflow=updated,
        template_id="editable_story_flow",
        replace_existing=True,
    )

    assert saved["template_id"] == "editable_story_flow"
    assert sorted(path.name for path in template_root.glob("*.json")) == ["editable_story_flow.json"]
    loaded = workflow_template_store.load_user_template("editable_story_flow")
    assert loaded["workflow"]["steps"][0]["prompt_template"] == "生成修改后的剧本。"


def test_template_step_summaries_preserve_fields_for_editor_roundtrip() -> None:
    steps = [
        {
            "id": "character_images",
            "title": "人物图循环",
            "node_type": "text",
            "steps": [
                {
                    "id": "character_prompt",
                    "title": "人物提示词",
                    "node_type": "text",
                },
                {
                    "id": "character_image",
                    "title": "人物图片",
                    "node_type": "image",
                    "depends_on": ["character_prompt"],
                    "fields": {
                        "workflow_source_step": "character_prompt",
                        "workflow_source_path": "output",
                        "workflow_generate": True,
                        "aspect_ratio": "16:9",
                        "resolution": "2560x1440",
                    },
                },
            ],
        },
        {
            "id": "final_video",
            "title": "成片",
            "node_type": "video",
            "fields": {
                "workflow_source_step": "video_prompt",
                "duration_seconds": 15,
                "resolution": "1080p",
            },
        },
    ]

    summaries = canvas_workflow_templates.template_step_summaries(steps)
    by_id = {step["id"]: step for step in summaries}

    assert by_id["character_image"]["fields"] == steps[0]["steps"][1]["fields"]
    assert by_id["final_video"]["fields"] == steps[1]["fields"]
    by_id["character_image"]["fields"]["workflow_source_step"] = "changed"
    assert steps[0]["steps"][1]["fields"]["workflow_source_step"] == "character_prompt"


@pytest.mark.asyncio
async def test_user_workflow_template_promote_clone_and_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    template_root = tmp_path / "workflow_templates"
    tool_root = tmp_path / "tool_results"
    monkeypatch.setattr(workflow_template_store, "workflow_template_library_root", lambda: template_root)
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tool_root)
    workflow = {
        "id": "plot_to_character_image",
        "name": "剧情到人物图",
        "description": "用户输入剧情后生成剧本和人物图",
        "inputs": [{"id": "plot", "label": "剧情"}],
        "required_inputs": ["plot"],
        "steps": [
            {
                "id": "script",
                "title": "生成剧本文本",
                "node_type": "text",
                "runner": "node.run",
                "prompt_template": "SYSTEM: 写剧本\nUSER: {{plot}}\nOUTPUT: text",
            },
            {
                "id": "character_image",
                "title": "生成人物图",
                "node_type": "image",
                "runner": "node.run",
                "depends_on": ["script"],
                "prompt_template": "SYSTEM: 生成人物图\nUSER: {{script.output}}\nOUTPUT: image prompt",
            },
        ],
    }
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-template",
        workflow=workflow,
        normalized=canvas_workflow_templates.normalize_inline_workflow(workflow, input_values={"plot": "少女进入古寺"}),
        sample_inputs={"plot": "少女进入古寺"},
    )

    promoted = await workflow_tools.workflow_template_promote(
        project_id="proj-template",
        artifact_ref=saved["artifact_ref"],
        template_id="plot_to_character_image",
        name="剧情到人物图模板",
        source_skill_name="custom_story_skill",
        source_skill_summary="输入剧情后生成剧本和人物图",
    )

    assert promoted["ok"] is True
    assert promoted["template_id"] == "plot_to_character_image"
    assert promoted["storage_path"].endswith("workflow_templates/plot_to_character_image.json")
    assert (template_root / "plot_to_character_image.json").exists()
    assert not (template_root / "plot_to_character_image" / "manifest.json").exists()
    summaries = canvas_workflow_templates.list_template_summaries()
    user_template = next(item for item in summaries if item["id"] == "plot_to_character_image")
    assert user_template["scope"] == "user"
    assert user_template["downloadable"] is True
    assert user_template["source_skill"]["name"] == "custom_story_skill"
    assert user_template["required_inputs"] == ["plot"]
    assert [step["id"] for step in user_template["steps"]] == ["script", "character_image"]
    saved_payload = json.loads((template_root / "plot_to_character_image.json").read_text(encoding="utf-8"))
    assert saved_payload["x-openreel"]["workflow_template"]["source"]["source_skill"]["name"] == "custom_story_skill"
    candidates = workflow_template_store.candidate_summaries_for_skill(
        skill_name="custom_story_skill",
        skill_summary="输入剧情后生成剧本和人物图",
        user_goal="制作人物图流程",
    )
    assert candidates and candidates[0]["id"] == "plot_to_character_image"

    template = canvas_workflow_templates.get_template("plot_to_character_image", input_values={"plot": "新版剧情"})
    assert template["scope"] == "user"
    assert template["steps"][1]["depends_on"] == ["script"]

    cloned = await workflow_tools.workflow_template_clone_to_artifact(
        project_id="proj-template",
        template_id="plot_to_character_image",
    )
    assert cloned["ok"] is True
    assert cloned["artifact_ref"].startswith("workflow_spec:")
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-template", cloned["artifact_ref"])
    assert loaded["workflow"]["id"] == "plot_to_character_image"

    exported = await workflow_tools.workflow_template_export(
        project_id="proj-template",
        template_id="plot_to_character_image",
    )
    assert exported["ok"] is True
    assert exported["filename"] == "plot_to_character_image.openreel-workflow-template.json"
    assert exported["package"]["workflow"]["steps"][0]["id"] == "script"


@pytest.mark.asyncio
async def test_workflow_template_clone_to_artifact_supports_builtin_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path / "tool_results")

    cloned = await workflow_tools.workflow_template_clone_to_artifact(
        project_id="proj-builtin-template",
        template_id="general_short_drama_workflow",
    )

    assert cloned["ok"] is True
    assert cloned["template_id"] == "general_short_drama_workflow"
    assert cloned["artifact_ref"].startswith("workflow_spec:")
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-builtin-template", cloned["artifact_ref"])
    assert loaded["workflow"]["id"] == "general_short_drama_workflow"
    assert loaded["source"]["template_scope"] == "builtin"


def test_user_workflow_template_directory_scans_loose_specs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_template_store.settings, "PROJECT_ROOT", str(tmp_path))
    template_root = tmp_path / "workflow_templates"
    template_root.mkdir(parents=True)
    (template_root / "manual_script_flow.json").write_text(
        json.dumps(
            {
                "id": "manual_script_flow",
                "name": "手写剧情流程",
                "description": "用户目录里的手写 workflow spec",
                "category": "user",
                "applies_to": "手写 spec 剧情 剧本",
                "inputs": [{"id": "plot", "label": "剧情"}],
                "required_inputs": ["plot"],
                "steps": [
                    {
                        "id": "script",
                        "title": "剧本",
                        "node_type": "text",
                        "runner": "node.run",
                        "prompt_template": "SYSTEM: 写剧本\nUSER: {{inputs.plot}}\nOUTPUT: text",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summaries = canvas_workflow_templates.list_template_summaries()
    summary = next(item for item in summaries if item["id"] == "manual_script_flow")
    assert summary["scope"] == "user"
    assert summary["source"] == "user_template_file"
    assert summary["template_source"] == "project_root_spec"
    assert summary["downloadable"] is True
    assert summary["required_inputs"] == ["plot"]

    template = canvas_workflow_templates.get_template("manual_script_flow", input_values={"plot": "雨夜怀表"})
    assert template["scope"] == "user"
    assert template["source"] == "user_template_file"
    assert template["steps"][0]["prompt_template"].startswith("SYSTEM: 写剧本")

    loaded = workflow_template_store.load_user_template("manual_script_flow")
    assert loaded["summary"]["id"] == "manual_script_flow"
    assert loaded["workflow"]["steps"][0]["id"] == "script"


def test_general_short_drama_workflow_template_is_available() -> None:
    summaries = canvas_workflow_templates.list_template_summaries()
    assert [item["id"] for item in summaries] == ["general_short_drama_workflow"]
    template = next(item for item in summaries if item["id"] == "general_short_drama_workflow")

    assert template["name"] == "通用视频制作工作流"
    assert "默认视频制作流程" in template["description"]
    assert template["workflow_spec_version"] == "openreel.workflow.v1"
    assert "core.node.video" in template["required_capabilities"]
    assert "core.prompt_template" in template["required_capabilities"]
    assert "core.surface.canvas" in template["required_capabilities"]
    assert "core.surface.workflow_runtime" in template["required_capabilities"]
    assert "openreel.core" in template["extensions"]
    assert "plot" in template["inputs"]
    assert "durationSeconds" in template["inputs"]
    assert "segmentSeconds" in template["inputs"]
    assert "segmentCount" not in template["inputs"]
    assert "storyboardGrid" not in template["inputs"]
    assert "aspectRatio" not in template["inputs"]
    assert template["required_inputs"] == ["plot", "durationSeconds"]
    assert len(template["steps"]) == 21
    ids = [step["id"] for step in template["steps"]]
    assert not any("e1_s1" in step_id for step_id in ids)
    assert "story_template_prompt" not in ids
    assert "story_template" not in ids
    assert ids.index("storyboard") < ids.index("video_prompt")
    assert ids.index("video_prompt") < ids.index("final_video")
    by_id = {step["id"]: step for step in template["steps"]}
    assert by_id["input"]["runner"] == "workflow_input"
    assert "prompt_template" not in by_id["input"]
    assert by_id["script"]["surface"] == "workflow_runtime"
    assert by_id["script"]["visibility"] == "flow_only"
    assert by_id["script"]["title"] == "剧本"
    assert by_id["script"].get("output_mode") != "json"
    assert "只输出剧本正文" in by_id["script"]["prompt_template"]
    assert "segmentCount" not in by_id["script"]["prompt_template"]
    assert by_id["script_canvas"]["kind"] == "canvas_text"
    assert by_id["script_canvas"]["runner"] == "workflow_canvas_output"
    assert by_id["episode_segments"]["depends_on"] == ["script"]
    assert by_id["segment_script"]["surface"] == "workflow_runtime"
    assert by_id["segment_script"]["visibility"] == "flow_only"
    assert by_id["segment_script"].get("output_mode") != "json"
    assert "只输出当前段剧本正文" in by_id["segment_script"]["prompt_template"]
    assert by_id["segment_script_canvas"]["kind"] == "canvas_text"
    assert by_id["segment_script_canvas"]["runner"] == "workflow_canvas_output"
    assert by_id["main_characters"]["kind"] == "collection"
    assert by_id["main_characters"]["node_type"] == "text"
    assert by_id["main_characters"]["surface"] == "workflow_runtime"
    assert by_id["main_characters"]["visibility"] == "flow_only"
    assert by_id["main_characters"]["collection"]["label"] == "主要人物集合"
    assert by_id["main_character_images"]["role"] == "repeat_group"
    assert by_id["main_character_image_prompt"]["surface"] == "workflow_runtime"
    assert "官方设定集角色视觉参考表" in by_id["main_character_image_prompt"]["prompt_template"]
    assert by_id["main_character_image"]["kind"] == "image"
    assert by_id["main_character_image"]["runner"] == "workflow_canvas_output"
    assert by_id["minor_characters"]["kind"] == "collection"
    assert by_id["minor_characters"]["node_type"] == "text"
    assert by_id["minor_characters"]["surface"] == "workflow_runtime"
    assert by_id["minor_characters"]["collection"]["label"] == "当前段配角集合"
    assert by_id["minor_characters"]["depends_on"] == [
        "main_characters",
        "plan_characters_scenes",
        "segment_script_canvas",
    ]
    assert by_id["scene"]["kind"] == "collection"
    assert by_id["scene"]["node_type"] == "text"
    assert by_id["scene"]["surface"] == "workflow_runtime"
    assert by_id["scene"]["collection"]["label"] == "当前段场景集合"
    assert by_id["scene_reference_prompt"]["surface"] == "workflow_runtime"
    assert "2x2 四机位全景图网格" in by_id["scene_reference_prompt"]["prompt_template"]
    assert by_id["scene_reference"]["kind"] == "image"
    assert by_id["scene_reference"]["runner"] == "workflow_canvas_output"
    assert by_id["plan_frames"]["surface"] == "workflow_runtime"
    assert "storyboardGrid" not in by_id["plan_frames"]["prompt_template"]
    assert "普通段落建议 4 格" in by_id["plan_frames"]["prompt_template"]
    assert by_id["storyboard_prompt"]["surface"] == "workflow_runtime"
    assert "storyboardGrid" not in by_id["storyboard_prompt"]["prompt_template"]
    assert by_id["storyboard"]["kind"] == "image"
    assert by_id["storyboard"]["depends_on"] == ["storyboard_prompt", "main_character_images"]
    assert "depends_on_previous" not in by_id["storyboard"]
    assert by_id["storyboard"]["reference_selectors"][0]["from_group"] == "main_character_images"
    assert by_id["storyboard"]["reference_selectors"][0]["source_path"] == "output.appearing_characters"
    assert by_id["video_prompt"]["surface"] == "workflow_runtime"
    assert by_id["video_prompt"].get("output_mode") != "json"
    assert "只输出纯文本视频提示词" in by_id["video_prompt"]["prompt_template"]
    assert "inputs.aspectRatio" not in by_id["video_prompt"]["prompt_template"]
    assert by_id["final_video"]["kind"] == "video"
    assert by_id["final_video"]["node_type"] == "video"
    assert by_id["final_video"]["runner"] == "workflow_canvas_output"
    assert by_id["final_video"]["depends_on"] == [
        "video_prompt",
        "storyboard",
        "scene_reference",
        "main_character_images",
    ]
    assert by_id["final_video"]["context_refs"] == [
        {"ref": "storyboard", "role": "visual_reference"},
        {"ref": "scene_reference", "role": "visual_reference"},
        {"ref": "video_prompt", "role": "context"},
    ]
    assert by_id["final_video"]["reference_selectors"][0]["from_group"] == "main_character_images"
    assert by_id["final_video"]["reference_selectors"][0]["source_path"] == "output.appearing_characters"


@pytest.mark.asyncio
async def test_workflow_protocol_info_lists_core_capabilities() -> None:
    result = await workflow_tools.workflow_protocol_info(project_id="proj-1")

    assert result["ok"] is True
    assert result["protocol_version"] == "openreel.workflow.v1"
    assert "core.prompt_template" in result["available_capabilities"]
    assert "core.depends_on_previous" in result["available_capabilities"]
    assert "core.surface.workflow_runtime" in result["available_capabilities"]
    assert "openreel.core" in result["available_extensions"]


def test_inline_workflow_protocol_preserves_optional_extension_metadata() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "portable_workflow",
            "name": "可移植工作流",
            "workflow_spec_version": "openreel.workflow.v1",
            "required_capabilities": ["core.node.text", "core.runner.node_run"],
            "extensions": {
                "vendor.optional_pack": {
                    "label": "Optional Vendor Pack",
                    "version": "0.1",
                    "optional": True,
                }
            },
            "steps": [
                {
                    "id": "brief",
                    "title": "需求",
                    "node_type": "text",
                    "runner": "node.run",
                    "extension_config": {"vendor.optional_pack": {"mode": "compact"}},
                    "io": {"outputs": [{"name": "content", "type": "text"}]},
                    "x": {"vendor.optional_pack": {"ui": "compact"}},
                }
            ],
        }
    )

    assert template["workflow_spec_version"] == "openreel.workflow.v1"
    assert template["protocol"]["supported"] is True
    assert template["extensions"]["vendor.optional_pack"]["version"] == "0.1"
    step = template["steps"][0]
    assert step["extension_config"]["vendor.optional_pack"]["mode"] == "compact"
    assert step["io"]["outputs"][0]["name"] == "content"
    assert step["x"]["vendor.optional_pack"]["ui"] == "compact"


def test_inline_workflow_protocol_rejects_missing_required_extension() -> None:
    with pytest.raises(canvas_workflow_templates.WorkflowTemplateError) as excinfo:
        canvas_workflow_templates.normalize_inline_workflow(
            {
                "id": "third_party_workflow",
                "name": "第三方工作流",
                "workflow_spec_version": "openreel.workflow.v1",
                "required_extensions": ["comfyui.custom_nodes.magic_video"],
                "steps": [
                    {
                        "id": "brief",
                        "title": "需求",
                        "node_type": "text",
                    }
                ],
            }
        )

    assert "comfyui.custom_nodes.magic_video" in str(excinfo.value)


@pytest.mark.asyncio
async def test_workflow_materialize_step_stores_workflow_runtime_outside_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    project_state: dict[str, Any] = {}
    nodes: list[dict[str, Any]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("workflow_runtime steps must not create canvas nodes")

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        return {"id": "edge-1", **kwargs}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_read_project_state(project_id: str) -> dict[str, Any]:
        return project_state

    async def fake_write_project_state_patch(project_id: str, patch: dict[str, Any]) -> None:
        project_state.update(patch)

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", fake_write_project_state_patch)

    result = await workflow_tools.workflow_materialize_step(
        project_id="proj-1",
        workflow={
            "id": "runtime_surface_flow",
            "name": "运行时中间步骤",
            "steps": [
                {
                    "id": "plan",
                    "title": "规划",
                    "node_type": "text",
                    "surface": "workflow_runtime",
                    "visibility": "flow_only",
                }
            ],
        },
        step_id="plan",
    )

    assert result["ok"] is True
    assert result["runtime_step"] is True
    assert result["node"]["surface"] == "workflow_runtime"
    assert nodes == []
    instance = project_state["workflow_runtime"]["instances"][result["instance_id"]]
    record = instance["steps"]["plan"]
    assert record["surface"] == "workflow_runtime"
    assert record["input"]["surface"] == "workflow_runtime"
    assert record["input"]["workflow"]["surface"] == "workflow_runtime"
    assert record["input"]["workflow"]["visibility"] == "flow_only"


@pytest.mark.asyncio
async def test_workflow_materialize_visible_step_ignores_runtime_only_record(monkeypatch: pytest.MonkeyPatch) -> None:
    project_state = {
        "workflow_runtime": {
            "instances": {
                "wf_flow_only": {
                    "template_id": "visible_script_flow",
                    "steps": {
                        "script": {
                            "title": "剧本",
                            "type": "text",
                            "status": "completed",
                            "surface": "workflow_runtime",
                            "visibility": "flow_only",
                            "output": {"content": "流程内部剧本"},
                        }
                    },
                }
            }
        }
    }
    created_nodes: list[dict[str, Any]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        node = {
            "id": "node-script",
            "display_id": 1,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "surface": kwargs["model_config"]["surface"],
            "input": kwargs["input_data"],
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        return {"id": "edge-1", **kwargs}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_read_project_state(project_id: str) -> dict[str, Any]:
        return project_state

    async def fake_write_project_state_patch(project_id: str, patch: dict[str, Any]) -> None:
        project_state.update(patch)

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", fake_write_project_state_patch)

    result = await workflow_tools.workflow_materialize_step(
        project_id="proj-1",
        workflow={
            "id": "visible_script_flow",
            "name": "可见剧本流程",
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "surface": "draft_canvas",
                    "visibility": "canvas",
                }
            ],
        },
        step_id="script",
        instance_id="wf_flow_only",
    )

    assert result["ok"] is True
    assert result.get("runtime_step") is not True
    assert result["node_id"] == "node-script"
    assert created_nodes[0]["surface"] == "draft_canvas"
    record = project_state["workflow_runtime"]["instances"]["wf_flow_only"]["steps"]["script"]
    assert record["surface"] == "draft_canvas"
    assert record["node_id"] == "node-script"


@pytest.mark.asyncio
async def test_workflow_materialize_visible_step_recreates_missing_canvas_node_from_runtime_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_state = {
        "workflow_runtime": {
            "instances": {
                "wf_missing": {
                    "template_id": "visible_script_flow",
                    "steps": {
                        "script": {
                            "title": "剧本",
                            "type": "text",
                            "status": "completed",
                            "surface": "draft_canvas",
                            "input": {
                                "workflow": {
                                    "template_id": "visible_script_flow",
                                    "instance_id": "wf_missing",
                                    "step_id": "script",
                                    "surface": "draft_canvas",
                                    "visibility": "canvas",
                                }
                            },
                            "output": {"content": "画布节点已删除"},
                        }
                    },
                }
            }
        }
    }
    created_nodes: list[dict[str, Any]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        node = {
            "id": "node-script-new",
            "display_id": 2,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "surface": kwargs["model_config"]["surface"],
            "input": kwargs["input_data"],
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        return {"id": "edge-1", **kwargs}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_read_project_state(project_id: str) -> dict[str, Any]:
        return project_state

    async def fake_write_project_state_patch(project_id: str, patch: dict[str, Any]) -> None:
        project_state.update(patch)

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", fake_write_project_state_patch)

    result = await workflow_tools.workflow_materialize_step(
        project_id="proj-1",
        workflow={
            "id": "visible_script_flow",
            "name": "可见剧本流程",
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "surface": "draft_canvas",
                    "visibility": "canvas",
                }
            ],
        },
        step_id="script",
        instance_id="wf_missing",
    )

    assert result["ok"] is True
    assert result["node_id"] == "node-script-new"
    assert created_nodes[0]["surface"] == "draft_canvas"
    record = project_state["workflow_runtime"]["instances"]["wf_missing"]["steps"]["script"]
    assert record["node_id"] == "node-script-new"


@pytest.mark.asyncio
async def test_workflow_runtime_records_outputs_artifacts_and_stales_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)
    template = {
        "id": "typed_runtime_flow",
        "name": "Typed Runtime Flow",
        "steps": [
            {"id": "plan", "node_type": "text"},
            {"id": "image", "node_type": "image", "depends_on": ["plan"]},
        ],
    }
    plan_fields = {
        "workflow": {
            "template_id": "typed_runtime_flow",
            "template_name": "Typed Runtime Flow",
            "instance_id": "wf_typed",
            "step_id": "plan",
            "surface": "workflow_runtime",
        }
    }
    image_fields = {
        "references": [{"ref": "node:1", "role": "visual_reference"}],
        "workflow": {
            "template_id": "typed_runtime_flow",
            "template_name": "Typed Runtime Flow",
            "instance_id": "wf_typed",
            "step_id": "image",
            "surface": "draft_canvas",
            "visibility": "canvas",
        },
    }

    await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template=template,
        instance_id="wf_typed",
        step_id="plan",
        node_type="text",
        title="规划",
        fields=plan_fields,
        status="running",
        increment_run=True,
    )
    await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template=template,
        instance_id="wf_typed",
        step_id="plan",
        node_type="text",
        title="规划",
        fields=plan_fields,
        status="completed",
        output={"content": "第一版规划"},
    )
    await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template=template,
        instance_id="wf_typed",
        step_id="image",
        node_type="image",
        title="图片",
        fields=image_fields,
        status="running",
        artifacts=[{"kind": "canvas_node", "node_id": "node-image", "type": "image"}],
        node_id="node-image",
        increment_run=True,
    )
    await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template=template,
        instance_id="wf_typed",
        step_id="image",
        node_type="image",
        title="图片",
        fields=image_fields,
        status="completed",
        output={"url": "/storage/image.png"},
        artifacts=[{"kind": "canvas_node", "node_id": "node-image", "type": "image"}],
        node_id="node-image",
    )
    await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template=template,
        instance_id="wf_typed",
        step_id="plan",
        node_type="text",
        title="规划",
        fields=plan_fields,
        status="running",
        increment_run=True,
    )
    await workflow_tools._upsert_workflow_runtime_step(
        project_id="proj-1",
        template=template,
        instance_id="wf_typed",
        step_id="plan",
        node_type="text",
        title="规划",
        fields=plan_fields,
        status="completed",
        output={"content": "第二版规划"},
    )

    steps = runtime_state["workflow_runtime"]["instances"]["wf_typed"]["steps"]
    assert steps["plan"]["run_count"] == 2
    assert steps["plan"]["outputs"][0]["type"] == "json"
    assert steps["image"]["resolved_inputs"][0]["role"] == "visual_reference"
    assert steps["image"]["artifacts"][0]["node_id"] == "node-image"
    assert steps["image"]["stale"] is True
    assert steps["image"]["invalidated_by"] == "plan"
    payload = workflow_tools.workflow_runtime_public_payload(
        runtime_state,
        template_id="typed_runtime_flow",
        instance_id="wf_typed",
    )
    public_by_id = {step["id"]: step for step in payload["steps"]}
    assert public_by_id["plan"]["output"] == {"content": "第二版规划"}
    assert public_by_id["plan"]["outputs"][0]["value"] == {"content": "第二版规划"}
    assert public_by_id["image"]["artifacts"][0]["node_id"] == "node-image"


def test_workflow_runtime_public_payload_expands_template_from_saved_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    inputs = {
        "plot": "雨后天台收到云隙来信",
        "durationSeconds": 30,
        "segmentSeconds": 15,
        "style": "青春奇幻动漫短片",
        "type": "短剧",
    }
    state["active_workflow"] = {
        "kind": "template",
        "template_id": "general_short_drama_workflow",
    }
    state["workflow_input_values"] = {
        "version": 1,
        "by_workflow": {
            "general_short_drama_workflow": {
                "workflow_id": "general_short_drama_workflow",
                "values": deepcopy(inputs),
            }
        },
        "by_instance": {
            "wf_video": {
                "workflow_id": "general_short_drama_workflow",
                "instance_id": "wf_video",
                "values": deepcopy(inputs),
            }
        },
    }
    state["workflow_runtime"] = {
        "instances": {
            "wf_video": {
                "template_id": "general_short_drama_workflow",
                "steps": {
                    "script": {
                        "title": "剧本",
                        "status": "completed",
                        "surface": "draft_canvas",
                        "node_id": "node-script",
                        "input": {
                            "workflow": {
                                "template_id": "general_short_drama_workflow",
                                "instance_id": "wf_video",
                                "step_id": "script",
                                "surface": "draft_canvas",
                            }
                        },
                        "output": {
                            "segments": [
                                {"index": 1, "script": "第1段剧情"},
                                {"index": 2, "script": "第2段剧情"},
                            ]
                        },
                        "artifacts": [{"kind": "canvas_node", "node_id": "node-script"}],
                    }
                },
            }
        }
    }

    payload = workflow_tools.workflow_runtime_public_payload(
        state,
        template_id="general_short_drama_workflow",
        instance_id="wf_video",
    )

    by_id = {step["id"]: step for step in payload["steps"]}
    assert "input" in by_id
    assert "script" in by_id
    assert "script_canvas" in by_id
    assert "episode_segments_s1_video_prompt" in by_id
    assert "episode_segments_s2_final_video" in by_id
    assert by_id["input"]["status"] == "completed"
    assert by_id["input"]["virtual"] is True
    assert by_id["script"]["status"] == "completed"
    assert by_id["script"]["waiting_on"] == []
    assert "episode_segments_s1_minor_characters" in by_id
    assert "episode_segments_s2_minor_characters" in by_id
    assert not any(step_id.startswith("episode_segments_i") for step_id in by_id)
    assert payload["status"] == "partial"
    assert payload["progress"]["total"] >= 20
    assert payload["progress"]["completed"] == 2


@pytest.mark.asyncio
async def test_visible_text_workflow_step_runtime_stores_text_output(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)
    emitted: list[tuple[str, dict[str, Any]]] = []
    node = {
        "id": "node-script",
        "display_id": 3,
        "type": "text",
        "title": "剧本",
        "status": "running",
        "surface": "draft_canvas",
        "input": {
            "title": "剧本",
            "surface": "draft_canvas",
            "workflow": {
                "template_id": "visible_script_flow",
                "template_name": "可见剧本流程",
                "instance_id": "wf_visible",
                "step_id": "script",
                "surface": "draft_canvas",
                "visibility": "canvas",
            },
        },
    }

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        assert node_id == "node-script"
        return dict(node)

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        assert node_id == "node-script"
        if "input_data" in patch:
            node["input"] = patch["input_data"]
        if "status" in patch:
            node["status"] = patch["status"]
        return {"id": node_id, **patch}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        emitted.append((action, payload))

    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)

    await workflow_tools._set_workflow_step_runtime(
        project_id="proj-1",
        node_id="node-script",
        inputs={"plot": "雨夜重逢"},
        status="completed",
        result={
            "ok": True,
            "node_id": "node-script",
            "type": "text",
            "status": "completed",
            "result": {
                "type": "text",
                "title": "剧本",
                "content": json.dumps({"full_text": "生成的剧本正文"}, ensure_ascii=False),
                "references": [],
                "depends_on": [],
                "workflow_text_runner": "one_shot_llm",
                "llm_task_type": "script_generation",
                "model": "openai/test",
                "usage": {"total_tokens": 123},
                "run_id": "workflow_text_test",
                "prompt_dump_run_id": "workflow_text_test",
            },
        },
        template={"id": "visible_script_flow", "name": "可见剧本流程", "steps": [{"id": "script"}]},
    )

    record = runtime_state["workflow_runtime"]["instances"]["wf_visible"]["steps"]["script"]
    assert record["surface"] == "draft_canvas"
    assert record["node_id"] == "node-script"
    assert record["output"] == {"full_text": "生成的剧本正文"}
    assert record["outputs"][0]["value"] == {"full_text": "生成的剧本正文"}
    assert "usage" not in record["output"]
    assert "workflow_text_runner" not in record["outputs"][0]["value"]
    assert record["artifacts"][0]["node_id"] == "node-script"
    assert node["input"]["workflow"]["step_status"] == "completed"
    assert emitted[-1][0] == "update_node"


@pytest.mark.asyncio
async def test_workflow_run_next_step_selects_next_in_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_next": {
                "template_id": "next_flow",
                    "steps": {
                    "script": {
                        "title": "剧本",
                        "status": "completed",
                        "surface": "draft_canvas",
                        "node_id": "node-script",
                        "artifacts": [{"kind": "canvas_node", "node_id": "node-script"}],
                    },
                    "storyboard": {"title": "分镜", "status": "draft"},
                },
            }
        }
    }

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    captured: dict[str, Any] = {}

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "step_id": kwargs["step_id"],
            "instance_id": kwargs["instance_id"],
            "runtime": {"instance_id": kwargs["instance_id"], "steps": []},
        }

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    source_workflow = {
        "id": "next_flow",
        "name": "Next Flow",
        "steps": [
            {"id": "script", "title": "剧本", "node_type": "text"},
            {"id": "storyboard", "title": "分镜", "node_type": "image", "depends_on": ["script"]},
        ],
    }
    result = await workflow_tools.workflow_run_next_step(
        project_id="proj-1",
        workflow=source_workflow,
        instance_id="wf_next",
    )

    assert result["ok"] is True
    assert result["run_next"] is True
    assert result["selected_step_id"] == "storyboard"
    assert captured["step_id"] == "storyboard"
    assert captured["instance_id"] == "wf_next"
    assert captured["workflow"] is source_workflow


@pytest.mark.asyncio
async def test_workflow_run_next_step_respects_dependencies_when_order_is_not_topological(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {"instances": {"wf_dep": {"template_id": "dep_flow", "steps": {}}}}

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    captured: dict[str, Any] = {}

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "step_id": kwargs["step_id"],
            "instance_id": kwargs["instance_id"],
            "runtime": {"instance_id": kwargs["instance_id"], "steps": []},
        }

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_next_step(
        project_id="proj-1",
        workflow={
            "id": "dep_flow",
            "name": "Dependency Flow",
            "steps": [
                {"id": "storyboard", "title": "分镜", "node_type": "image", "depends_on": ["script"]},
                {"id": "script", "title": "剧本", "node_type": "text"},
            ],
        },
        instance_id="wf_dep",
    )

    assert result["ok"] is True
    assert result["selected_step_id"] == "script"
    assert captured["step_id"] == "script"
    assert captured["instance_id"] == "wf_dep"


@pytest.mark.asyncio
async def test_workflow_run_next_prepares_manual_media_step(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_manual": {
                "template_id": "manual_flow",
                "steps": {
                    "script": {
                        "status": "completed",
                        "surface": "draft_canvas",
                        "node_id": "node-script",
                        "artifacts": [{"kind": "canvas_node", "node_id": "node-script"}],
                    },
                    "video": {"status": "draft"},
                },
            },
        },
    }

    captured: dict[str, Any] = {}

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "step_id": kwargs["step_id"],
            "instance_id": kwargs["instance_id"],
            "awaiting_manual_generation": True,
        }

    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_next_step(
        project_id="proj-1",
        workflow={
            "id": "manual_flow",
            "name": "Manual Flow",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {
                    "id": "video",
                    "title": "视频",
                    "node_type": "video",
                    "depends_on": ["script"],
                    "manual_only": True,
                },
            ],
        },
        instance_id="wf_manual",
    )

    assert result["ok"] is True
    assert result["selected_step_id"] == "video"
    assert result["awaiting_manual_generation"] is True
    assert captured["step_id"] == "video"
    assert captured["instance_id"] == "wf_manual"


@pytest.mark.asyncio
async def test_workflow_run_next_rejects_instance_from_another_template(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_old": {"template_id": "old_flow", "steps": {}},
        },
    }

    result = await workflow_tools.workflow_run_next_step(
        project_id="proj-1",
        workflow={
            "id": "new_flow",
            "name": "New Flow",
            "steps": [{"id": "script", "title": "剧本", "node_type": "text"}],
        },
        instance_id="wf_old",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_instance_template_mismatch"
    assert result["template_id"] == "new_flow"
    assert result["instance_template_id"] == "old_flow"


@pytest.mark.asyncio
async def test_workflow_ready_batch_includes_manual_media_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_manual": {
                "template_id": "manual_flow",
                "steps": {},
            },
        },
    }

    result = await workflow_tools._workflow_ready_step_batch(
        project_id="proj-1",
        template={
            "id": "manual_flow",
            "name": "Manual Flow",
            "steps": [
                {"id": "video", "title": "视频", "node_type": "video", "manual_only": True},
            ],
        },
        instance_id="wf_manual",
    )

    assert result["ready_step_ids"] == ["video"]
    assert result["manual_step_ids"] == []
    assert result["done"] is False


@pytest.mark.asyncio
async def test_workflow_run_next_runs_visible_step_when_only_runtime_record_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_flow_only": {
                "template_id": "visible_script_flow",
                "steps": {
                    "script": {
                        "title": "剧本",
                        "status": "completed",
                        "surface": "workflow_runtime",
                        "visibility": "flow_only",
                        "output": {"content": "流程内部剧本"},
                    },
                    "character_plan": {"title": "人物规划", "status": "draft", "surface": "workflow_runtime"},
                },
            }
        }
    }

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    captured: dict[str, Any] = {}

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "step_id": kwargs["step_id"],
            "instance_id": kwargs["instance_id"],
            "runtime": {"instance_id": kwargs["instance_id"], "steps": []},
        }

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_next_step(
        project_id="proj-1",
        workflow={
            "id": "visible_script_flow",
            "name": "可见剧本流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text", "surface": "draft_canvas", "visibility": "canvas"},
                {"id": "character_plan", "title": "人物规划", "node_type": "text", "surface": "workflow_runtime", "depends_on": ["script"]},
            ],
        },
        instance_id="wf_flow_only",
    )

    assert result["ok"] is True
    assert result["selected_step_id"] == "script"
    assert captured["step_id"] == "script"
    assert captured["instance_id"] == "wf_flow_only"


@pytest.mark.asyncio
async def test_workflow_run_all_steps_uses_backend_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    batches = [
        {"ready_step_ids": ["segment_1", "segment_2"], "done": False},
        {"ready_step_ids": [], "done": True},
    ]
    started: set[str] = set()
    segment_2_started = asyncio.Event()

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        batch = batches.pop(0)
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "parallel_flow",
            "instance_id": kwargs["instance_id"] or "wf_backend",
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"], "steps": []},
            "blocked_steps": [],
            **batch,
        }

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        step_id = kwargs["step_id"]
        started.add(step_id)
        if step_id == "segment_2":
            segment_2_started.set()
        if step_id == "segment_1":
            await asyncio.wait_for(segment_2_started.wait(), timeout=1)
            return {
                "ok": False,
                "step_id": step_id,
                "instance_id": kwargs["instance_id"],
                "node_id": "node-segment-1",
                "error": "segment 1 failed",
                "error_kind": "demo_failed",
            }
        return {
            "ok": True,
            "step_id": step_id,
            "instance_id": kwargs["instance_id"],
            "node_id": "node-segment-2",
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "id": "parallel_flow",
            "name": "并行流程",
            "steps": [
                {"id": "segment_1", "title": "第1段", "node_type": "text"},
                {"id": "segment_2", "title": "第2段", "node_type": "text"},
            ],
        },
    )

    assert result["ok"] is False
    assert result["run_all"] is True
    assert result["done"] is True
    assert result["instance_id"] == "wf_backend"
    assert result["steps_run"] == 2
    assert started == {"segment_1", "segment_2"}
    assert result["step_results"] == [
        {
            "ok": False,
            "done": False,
            "step_id": "segment_1",
            "node_id": "node-segment-1",
            "node_ids": [],
            "created_count": 0,
            "error": "segment 1 failed",
            "error_kind": "demo_failed",
        },
        {
            "ok": True,
            "done": False,
            "step_id": "segment_2",
            "node_id": "node-segment-2",
            "node_ids": [],
            "created_count": 0,
            "error": "",
            "error_kind": "",
        },
    ]
    assert result["failed_steps"] == [result["step_results"][0]]


@pytest.mark.asyncio
async def test_workflow_run_all_steps_uses_saved_instance_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_input_values"] = {
        "version": 1,
        "by_workflow": {
            "saved_input_flow": {
                "workflow_id": "saved_input_flow",
                "values": {
                    "plot": "旧剧情",
                    "durationSeconds": 15,
                },
            },
        },
        "by_instance": {
            "wf_saved_inputs": {
                "workflow_id": "saved_input_flow",
                "instance_id": "wf_saved_inputs",
                "values": {
                    "plot": "雨夜追车，主角冲出霓虹街区。",
                    "durationSeconds": 30,
                    "segmentSeconds": 15,
                },
            },
        },
    }
    captured_inputs: dict[str, Any] = {}

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        captured_inputs.update(kwargs["inputs"])
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "saved_input_flow",
            "instance_id": kwargs["instance_id"],
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"], "steps": []},
            "blocked_steps": [],
            "ready_step_ids": [],
            "done": True,
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "schema": "openreel.workflow.authoring.v1",
            "id": "saved_input_flow",
            "name": "保存输入流程",
            "inputs": [
                {"id": "plot", "label": "剧情"},
                {"id": "durationSeconds", "label": "时长", "type": "number"},
            ],
            "required_inputs": ["plot", "durationSeconds"],
            "steps": [
                {"id": "script", "title": "生成剧本", "node_type": "text"},
            ],
        },
        instance_id="wf_saved_inputs",
        inputs={},
    )

    assert result["ok"] is True
    assert captured_inputs == {
        "plot": "雨夜追车，主角冲出霓虹街区。",
        "durationSeconds": 30,
        "segmentSeconds": 15,
    }


@pytest.mark.asyncio
async def test_workflow_run_all_steps_persists_instance_status(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    ready_calls = 0

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        nonlocal ready_calls
        ready_calls += 1
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "status_flow",
            "instance_id": kwargs["instance_id"],
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"], "steps": []},
            "blocked_steps": [],
            "ready_step_ids": ["script"] if ready_calls == 1 else [],
            "done": ready_calls > 1,
        }

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        instance = state["workflow_runtime"]["instances"][kwargs["instance_id"]]
        assert instance["status"] == "running"
        assert instance["run_all_active"] is True
        return {
            "ok": True,
            "step_id": kwargs["step_id"],
            "instance_id": kwargs["instance_id"],
            "node_id": "node-script",
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "id": "status_flow",
            "name": "状态流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
            ],
        },
        instance_id="wf_status",
    )

    instance = state["workflow_runtime"]["instances"]["wf_status"]
    assert result["ok"] is True
    assert result["done"] is True
    assert result["runtime"]["status"] == "completed"
    assert instance["status"] == "completed"
    assert "run_all_active" not in instance


@pytest.mark.asyncio
async def test_workflow_run_all_duplicate_running_step_does_not_fail_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "project_id": kwargs["project_id"],
            "template_id": "running_flow",
            "instance_id": kwargs["instance_id"],
            "runtime": {
                "instance_id": kwargs["instance_id"],
                "status": "running",
                "steps": [{"id": "script", "status": "running"}],
            },
            "error": "Workflow step is already running",
            "error_kind": "workflow_step_running",
            "running_step_id": "script",
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "id": "running_flow",
            "name": "运行中流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
            ],
        },
        instance_id="wf_running",
    )

    instance = state["workflow_runtime"]["instances"]["wf_running"]
    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["running_step_id"] == "script"
    assert result["runtime"]["status"] == "running"
    assert instance["status"] == "running"
    assert instance["run_all_active"] is True
    assert "last_run_all_failed_at" not in instance


@pytest.mark.asyncio
async def test_workflow_ready_batch_resets_terminal_running_step_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_failed": {
                "instance_id": "wf_failed",
                "template_id": "retry_flow",
                "status": "failed",
                "steps": {
                    "script": {
                        "id": "script",
                        "title": "剧本",
                        "status": "running",
                        "last_started_at": "2026-07-05T06:39:35Z",
                    },
                },
            },
        },
    }

    result = await workflow_tools._workflow_ready_step_batch(
        project_id="proj-1",
        template={
            "id": "retry_flow",
            "name": "可重试流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
            ],
        },
        instance_id="wf_failed",
    )

    step = state["workflow_runtime"]["instances"]["wf_failed"]["steps"]["script"]
    assert result["ok"] is True
    assert result["ready_step_ids"] == ["script"]
    assert step["status"] == "idle"
    assert "last_started_at" not in step
    assert step["interrupted_at"]


@pytest.mark.asyncio
async def test_workflow_run_all_steps_pauses_before_next_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_workflow_runtime_state(monkeypatch)
    ready_calls = 0

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        nonlocal ready_calls
        ready_calls += 1
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "pause_flow",
            "instance_id": kwargs["instance_id"] or "wf_pause",
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"] or "wf_pause", "steps": []},
            "blocked_steps": [],
            "ready_step_ids": ["script"] if ready_calls == 1 else ["video"],
            "done": False,
        }

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        instance_id = kwargs["instance_id"]
        await workflow_tools.workflow_runtime_request_pause(
            kwargs["project_id"],
            instance_id,
            template_id="pause_flow",
            reason="user_requested",
        )
        return {
            "ok": True,
            "step_id": kwargs["step_id"],
            "instance_id": instance_id,
            "node_id": "node-script",
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "id": "pause_flow",
            "name": "可暂停流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {"id": "video", "title": "视频", "node_type": "video", "depends_on": ["script"]},
            ],
        },
        instance_id="wf_pause",
    )

    assert result["ok"] is True
    assert result["paused"] is True
    assert result["done"] is False
    assert result["instance_id"] == "wf_pause"
    assert result["steps_run"] == 1
    assert result["runtime"]["status"] == "paused"
    assert ready_calls == 1


@pytest.mark.asyncio
async def test_workflow_ready_batch_expands_deferred_groups_from_runtime_context(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_dynamic": {
                "template_id": "dynamic_segment_workflow",
                "steps": {
                    "plan_segments": {
                        "title": "分段规划",
                        "status": "completed",
                        "surface": "workflow_runtime",
                        "workflow": {
                            "template_id": "dynamic_segment_workflow",
                            "instance_id": "wf_dynamic",
                            "step_id": "plan_segments",
                            "surface": "workflow_runtime",
                        },
                        "output": {
                            "segments": [
                                {"id": "a", "index": 1, "storyBeat": "开端"},
                                {"id": "b", "index": 2, "storyBeat": "反转"},
                            ]
                        },
                    }
                },
            }
        }
    }

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    workflow = {
        "id": "dynamic_segment_workflow",
        "name": "动态分段流程",
        "dimensions": {
            "segments": {"source": "steps.plan_segments.output.segments"},
        },
        "steps": [
            {"id": "plan_segments", "title": "分段规划", "node_type": "text", "surface": "workflow_runtime"},
            {
                "id": "segment_flow",
                "title": "每段流程",
                "depends_on": ["plan_segments"],
                "foreach": {"dimension": "segments"},
                "steps": [
                    {"id": "segment_story", "title": "分段剧情", "node_type": "text", "surface": "workflow_runtime"},
                ],
            },
        ],
    }
    initial_template = canvas_workflow_templates.normalize_inline_workflow(workflow)

    result = await workflow_tools._workflow_ready_step_batch(
        project_id="proj-1",
        template=initial_template,
        workflow=workflow,
        instance_id="wf_dynamic",
    )

    assert result["ok"] is True
    assert result["done"] is False
    assert result["ready_step_ids"] == [
        "segment_flow_s1_segment_story",
        "segment_flow_s2_segment_story",
    ]
    assert [step["id"] for step in result["template"]["steps"]] == [
        "plan_segments",
        "segment_flow_s1_segment_story",
        "segment_flow_s2_segment_story",
    ]


@pytest.mark.asyncio
async def test_workflow_run_all_exact_max_steps_checks_final_done(monkeypatch: pytest.MonkeyPatch) -> None:
    batches = [
        {"ready_step_ids": ["script"], "done": False},
        {"ready_step_ids": [], "done": True},
    ]

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        batch = batches.pop(0)
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "exact_limit_flow",
            "instance_id": kwargs["instance_id"] or "wf_exact",
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"] or "wf_exact", "steps": []},
            "blocked_steps": [],
            **batch,
        }

    captured: dict[str, Any] = {}

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "step_id": kwargs["step_id"],
            "instance_id": kwargs["instance_id"],
            "node_id": "node-script",
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    source_workflow = {
        "id": "exact_limit_flow",
        "name": "精确上限流程",
        "steps": [{"id": "script", "title": "剧本", "node_type": "text"}],
    }
    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow=source_workflow,
        max_steps=1,
    )

    assert result["ok"] is True
    assert result["done"] is True
    assert result["steps_run"] == 1
    assert not result.get("error")
    assert captured["workflow"] is source_workflow


@pytest.mark.asyncio
async def test_workflow_run_all_attributes_source_compile_failure_to_ready_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_calls = 0

    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        nonlocal ready_calls
        ready_calls += 1
        failed_step_ids = kwargs.get("failed_step_ids") or set()
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "source_flow",
            "instance_id": kwargs["instance_id"] or "wf_source",
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"] or "wf_source", "steps": []},
            "ready_step_ids": [] if "script" in failed_step_ids else ["script"],
            "blocked_steps": [],
            "done": "script" in failed_step_ids,
        }

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "instance_id": kwargs["instance_id"],
            "error": "compiled runtime template was passed as source",
            "error_kind": "workflow_spec_artifact_error",
        }

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "id": "source_flow",
            "name": "Source Flow",
            "steps": [{"id": "script", "title": "剧本", "node_type": "text"}],
        },
        max_steps=3,
    )

    assert result.get("error_kind") != "workflow_run_all_step_limit"
    assert result["done"] is True
    assert result["steps_run"] == 1
    assert result["failed_steps"][0]["step_id"] == "script"
    assert ready_calls == 2


@pytest.mark.asyncio
async def test_workflow_run_all_stops_immediately_when_ready_batch_makes_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ready_step_batch(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "project_id": kwargs["project_id"],
            "template_id": "no_progress_flow",
            "instance_id": kwargs["instance_id"] or "wf_no_progress",
            "template": kwargs["template"],
            "runtime": {"instance_id": kwargs["instance_id"] or "wf_no_progress", "steps": []},
            "ready_step_ids": ["script"],
            "blocked_steps": [],
            "done": False,
        }

    async def fake_run_step(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "done": True, "instance_id": kwargs["instance_id"]}

    monkeypatch.setattr(workflow_tools, "_workflow_ready_step_batch", fake_ready_step_batch)
    monkeypatch.setattr(workflow_tools, "workflow_run_step", fake_run_step)

    result = await workflow_tools.workflow_run_all_steps(
        project_id="proj-1",
        workflow={
            "id": "no_progress_flow",
            "name": "No Progress Flow",
            "steps": [{"id": "script", "title": "剧本", "node_type": "text"}],
        },
        max_steps=120,
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_run_all_no_progress"
    assert result["ready_step_ids"] == ["script"]
    assert result["steps_run"] == 0


@pytest.mark.asyncio
async def test_visible_final_video_uses_upstream_video_prompt_without_rewriting(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[tuple[str, dict[str, Any]]] = []
    full_prompt = "视频类型：15秒文生视频。主体设定完整，镜头按0-15秒连续推进。"

    async def fake_records_for_instance(project_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert project_id == "proj-1"
        return [
            {
                "id": "prompt-node",
                "type": "text",
                "title": "第1段 · 视频提示词",
                "status": "completed",
                "input": {
                    "content": json.dumps(
                        {
                            "prompt": full_prompt,
                            "negative_prompt": "不要新增剧情",
                            "duration_seconds": 15,
                            "aspect_ratio": "16:9",
                        },
                        ensure_ascii=False,
                    ),
                    "workflow": {
                        "template_id": "general_short_drama_workflow",
                        "instance_id": "wf_demo",
                        "step_id": "segments_s1_video_prompt",
                        "template_step_id": "video_prompt",
                        "source_node_id": "videoPrompt",
                    },
                },
                "output": json.dumps(
                    {
                        "prompt": full_prompt,
                        "negative_prompt": "不要新增剧情",
                        "duration_seconds": 15,
                        "aspect_ratio": "16:9",
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updates.append((node_id, patch))
        return {"ok": True}

    async def fake_sync_dependency_edges(project_id: str, node_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "changed": False, "added_edges": [], "removed_edges": []}

    async def fail_llm(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("final video prompt should be copied from upstream video_prompt")

    monkeypatch.setattr(workflow_tools, "_workflow_records_for_instance", fake_records_for_instance)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_sync_dependency_edges)
    monkeypatch.setattr(node_universal, "_call_workflow_text_llm", fail_llm)

    node = {
        "id": "video-node",
        "type": "video",
            "title": "第1段 · 文生视频",
        "status": "idle",
        "input": {
            "duration_seconds": 15,
            "aspect_ratio": "16:9",
            "workflow_source_step": "segments_s1_video_prompt",
            "workflow_source_path": "output.prompt",
            "workflow_generate": True,
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "template_name": "通用视频制作工作流",
                "instance_id": "wf_demo",
                "step_id": "segments_s1_final_video",
                "template_step_id": "final_video",
                "source_node_id": "finalVideo",
                "prompt_template": "USER: video_prompt={{videoPrompt.output}} duration={{json.duration_seconds}}",
                "instance_scope": {
                    "index": 1,
                    "start_second": 0,
                    "end_second": 15,
                    "duration_seconds": 15,
                },
            },
        },
        "prompt": "",
    }

    prepared = await workflow_tools._prepare_visible_workflow_node_for_run(
        project_id="proj-1",
        template={"id": "general_short_drama_workflow", "name": "通用视频制作工作流"},
        step={"id": "segments_s1_final_video", "node_type": "video", "depends_on": ["segments_s1_video_prompt"]},
        node=node,
    )

    assert prepared["prompt"] == full_prompt
    assert prepared["input"]["prompt"] == full_prompt
    assert prepared["input"]["duration_seconds"] == 15
    assert "negative_prompt" not in prepared["input"]
    assert prepared["input"]["prompt_status"] == "completed"
    assert updates[-1][0] == "video-node"
    assert updates[-1][1]["prompt"] == full_prompt


def test_workflow_canvas_output_value_keeps_canvas_body_user_facing() -> None:
    output = workflow_tools._workflow_canvas_output_value(
        {"content": "第一段剧情正文", "prompt": "机器提示词"},
        {"prompt": "节点提示词"},
        "text",
    )

    assert output == {"content": "第一段剧情正文"}
    assert "ok" not in output
    assert "status" not in output


@pytest.mark.asyncio
async def test_prepare_visible_workflow_canvas_text_syncs_visible_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[tuple[str, dict[str, Any]]] = []
    sync_calls: list[tuple[str, str, dict[str, Any]]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    template_id = "general_short_drama_workflow"
    instance_id = "wf-test"
    source_node = {
        "id": "full-script-node",
        "display_id": 1,
        "type": "text",
        "title": "完整剧本",
        "status": "completed",
        "input": {
            "workflow_source_step": "full_script",
            "workflow": {
                "template_id": template_id,
                "instance_id": instance_id,
                "step_id": "full_script_canvas",
                "surface": "draft_canvas",
                "runner": "workflow_canvas_output",
                "kind": "canvas_text",
            },
        },
        "output": {"content": "完整剧本正文"},
    }
    runtime_segment = {
        "id": "workflow-runtime:segment-script",
        "type": "text",
        "title": "分段剧本",
        "status": "completed",
        "workflow": {
            "template_id": template_id,
            "instance_id": instance_id,
            "step_id": "segments_s1_segment_script",
            "surface": "workflow_runtime",
            "depends_on": ["full_script"],
        },
        "output": {"content": "第一段剧情正文"},
    }
    target_node = {
        "id": "segment-script-canvas-node",
        "display_id": 2,
        "type": "text",
        "title": "第1段 · 分段剧本",
        "status": "idle",
        "input": {
            "workflow_source_step": "full_script",
            "workflow_source_path": "output",
            "workflow": {
                "template_id": template_id,
                "instance_id": instance_id,
                "step_id": "segments_s1_segment_script_canvas",
                "template_step_id": "segment_script_canvas",
                "surface": "draft_canvas",
                "runner": "workflow_canvas_output",
                "kind": "canvas_text",
                "repeat_group_id": "segments",
                "instance_scope": {"index": 1},
            },
        },
    }

    async def fake_records_for_instance(project_id: str, *, template_id: str, instance_id: str) -> list[dict[str, Any]]:
        return [source_node, target_node, runtime_segment]

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updates.append((node_id, patch))
        if node_id == target_node["id"] and isinstance(patch.get("input_data"), dict):
            target_node["input"] = patch["input_data"]
        return {"ok": True}

    async def fake_sync_dependency_edges(project_id: str, node_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        sync_calls.append((project_id, node_id, input_data))
        return {
            "ok": True,
            "changed": True,
            "added_edges": [{"id": "edge-1", "source_node_id": "full-script-node", "target_node_id": node_id}],
            "removed_edges": [],
        }

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        events.append((action, payload))

    monkeypatch.setattr(workflow_tools, "_workflow_records_for_instance", fake_records_for_instance)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_sync_dependency_edges)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)

    prepared = await workflow_tools._prepare_visible_workflow_node_for_run(
        project_id="proj-1",
        template={
            "id": template_id,
            "name": "通用视频制作工作流",
            "steps": [
                {"id": "full_script", "node_type": "text", "surface": "workflow_runtime"},
                {"id": "full_script_canvas", "node_type": "text", "kind": "canvas_text", "depends_on": ["full_script"]},
                {"id": "segments_s1_segment_script", "node_type": "text", "surface": "workflow_runtime", "depends_on": ["full_script"]},
                {
                    "id": "segments_s1_segment_script_canvas",
                    "node_type": "text",
                    "kind": "canvas_text",
                    "depends_on": ["full_script", "segments_s1_segment_script"],
                    "fields": {
                        "workflow_source_step": "segments_s1_segment_script",
                        "workflow_source_path": "output.content",
                    },
                },
            ],
        },
        step={
            "id": "segments_s1_segment_script_canvas",
            "node_type": "text",
            "kind": "canvas_text",
            "depends_on": ["full_script", "segments_s1_segment_script"],
            "fields": {
                "workflow_source_step": "segments_s1_segment_script",
                "workflow_source_path": "output.content",
            },
        },
        node=target_node,
    )

    assert prepared["_workflow_should_generate"] is False
    assert prepared["input"]["workflow_source_step"] == "segments_s1_segment_script"
    assert prepared["input"]["workflow_source_path"] == "output.content"
    assert prepared["input"]["content"] == "第一段剧情正文"
    assert prepared["input"]["references"] == [{"ref": "node:1", "role": "context"}]
    assert prepared["input"]["depends_on"] == ["node:1"]
    assert updates[-1][0] == "segment-script-canvas-node"
    assert sync_calls[-1][1] == "segment-script-canvas-node"
    assert events == [
        ("add_edge", {
            "id": "edge-1",
            "source": "full-script-node",
            "target": "segment-script-canvas-node",
            "source_node_id": "full-script-node",
            "target_node_id": "segment-script-canvas-node",
            "label": None,
        })
    ]


def test_general_short_drama_workflow_template_expands_with_inputs() -> None:
    inputs = {
        "plot": "雨夜误送古玉引来追兵",
        "durationSeconds": 30,
        "segmentSeconds": 15,
    }
    template = canvas_workflow_templates.get_template(
        "general_short_drama_workflow",
        input_values=inputs,
    )
    ids = [step["id"] for step in template["steps"]]

    assert len([step_id for step_id in ids if step_id.endswith("_storyboard")]) == 2
    assert len([step_id for step_id in ids if step_id.endswith("_segment_script")]) == 2
    assert len([step_id for step_id in ids if step_id.endswith("_segment_script_canvas")]) == 2
    assert len([step_id for step_id in ids if step_id.endswith("_scene_reference")]) == 2
    assert len([step_id for step_id in ids if step_id.endswith("_video_prompt")]) == 2
    assert len([step_id for step_id in ids if step_id.endswith("_story_template")]) == 0
    assert len([step_id for step_id in ids if step_id.endswith("_final_video")]) == 2
    by_id = {step["id"]: step for step in template["steps"]}
    assert by_id["script"].get("output_mode") != "json"
    assert "只输出剧本正文" in by_id["script"]["prompt_template"]
    assert by_id["episode_segments_s1_segment_script"]["depends_on"] == ["script"]
    assert by_id["episode_segments_s1_segment_script_canvas"]["depends_on"] == [
        "script",
        "episode_segments_s1_segment_script",
    ]
    assert (
        by_id["episode_segments_s1_segment_script_canvas"]["fields"]["workflow_source_step"]
        == "segment_script"
    )
    assert by_id["episode_segments_s1_minor_characters"]["depends_on"] == [
        "script",
        "main_characters",
        "plan_characters_scenes",
        "episode_segments_s1_segment_script_canvas",
    ]
    assert by_id["episode_segments_s1_scene"]["depends_on"] == [
        "script",
        "plan_characters_scenes",
        "episode_segments_s1_segment_script_canvas",
    ]
    assert by_id["episode_segments_s2_scene"]["depends_on"] == [
        "script",
        "plan_characters_scenes",
        "episode_segments_s2_segment_script_canvas",
    ]
    assert "episode_segments_s1_scene" not in by_id["episode_segments_s2_scene"]["depends_on"]
    assert "episode_segments_s1_storyboard" not in by_id["episode_segments_s2_plan_frames"]["depends_on"]
    assert "episode_segments_s1_final_video" not in by_id["episode_segments_s2_final_video"]["depends_on"]
    assert by_id["episode_segments_s1_storyboard"]["depends_on"] == [
        "script",
        "episode_segments_s1_storyboard_prompt",
        "main_character_images",
    ]
    assert by_id["episode_segments_s1_video_prompt"].get("output_mode") != "json"
    assert by_id["episode_segments_s1_video_prompt"]["fields"]["prompt_source"] == "skill:video_prompt"
    assert by_id["episode_segments_s1_final_video"]["kind"] == "video"
    assert by_id["episode_segments_s1_final_video"]["runner"] == "workflow_canvas_output"
    assert by_id["episode_segments_s1_final_video"]["fields"]["workflow_source_step"] == "video_prompt"
    assert by_id["episode_segments_s1_final_video"]["fields"]["production_path"] == "image_to_video"
    assert by_id["episode_segments_s1_final_video"]["depends_on"] == [
        "script",
        "episode_segments_s1_video_prompt",
        "episode_segments_s1_storyboard",
        "episode_segments_s1_scene_reference",
        "main_character_images",
    ]
    assert by_id["episode_segments_s1_final_video"]["context_refs"] == [
        {"ref": "storyboard", "role": "visual_reference"},
        {"ref": "scene_reference", "role": "visual_reference"},
        {"ref": "video_prompt", "role": "context"},
    ]
    assert by_id["episode_segments_s1_final_video"]["reference_selectors"][0]["from_group"] == "main_character_images"
    assert by_id["episode_segments_s2_final_video"]["instance_scope"]["start_second"] == 15
    assert by_id["episode_segments_s2_final_video"]["instance_scope"]["end_second"] == 30
    assert template["deferred_groups"][0]["id"] == "main_character_images"
    assert canvas_workflow_templates.missing_required_inputs(template, inputs) == []


def test_general_short_drama_workflow_expands_character_collection_with_context() -> None:
    template = canvas_workflow_templates.get_template(
        "general_short_drama_workflow",
        input_values={
            "plot": "雨夜误送古玉引来追兵",
            "durationSeconds": 30,
            "segmentSeconds": 15,
            "episodeCount": 1,
            "main_characters": {
                "output": {
                    "main_characters": [
                        {"name": "林舟", "reuse_key": "lin_zhou"},
                        {"name": "沈鸢", "reuse_key": "shen_yuan"},
                    ]
                }
            },
        },
    )
    ids = [step["id"] for step in template["steps"]]

    assert len([step_id for step_id in ids if step_id.endswith("_main_character_image")]) == 2
    assert template["deferred_groups"] == []


@pytest.mark.asyncio
async def test_general_short_drama_ready_batch_expands_character_group_from_template_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["workflow_runtime"] = {
        "instances": {
            "wf_drama": {
                "template_id": "general_short_drama_workflow",
                "steps": {
                    "script": {
                        "title": "剧本",
                        "type": "text",
                        "status": "completed",
                        "surface": "workflow_runtime",
                        "workflow": {
                            "template_id": "general_short_drama_workflow",
                            "instance_id": "wf_drama",
                            "step_id": "script",
                            "surface": "workflow_runtime",
                        },
                        "output": "雨夜误送古玉引来追兵。",
                    },
                    "plan_characters_scenes": {
                        "title": "人物与场景规划",
                        "type": "text",
                        "status": "completed",
                        "surface": "workflow_runtime",
                        "workflow": {
                            "template_id": "general_short_drama_workflow",
                            "instance_id": "wf_drama",
                            "step_id": "plan_characters_scenes",
                            "surface": "workflow_runtime",
                        },
                        "output": {
                            "main_characters": [{"name": "林舟", "reuse_key": "lin_zhou"}],
                            "style_template": "电影感雨夜",
                            "minor_characters_by_segment": [[], []],
                            "scenes_by_segment": [[], []],
                        },
                    },
                    "main_characters": {
                        "title": "主要人物集合",
                        "type": "text",
                        "status": "completed",
                        "surface": "workflow_runtime",
                        "workflow": {
                            "template_id": "general_short_drama_workflow",
                            "instance_id": "wf_drama",
                            "step_id": "main_characters",
                            "surface": "workflow_runtime",
                        },
                        "output": {
                            "main_characters": [{"name": "林舟", "reuse_key": "lin_zhou"}],
                        },
                    },
                },
            }
        }
    }

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    inputs = {
        "plot": "雨夜误送古玉引来追兵",
        "durationSeconds": 30,
        "segmentSeconds": 15,
    }
    template = canvas_workflow_templates.get_template(
        "general_short_drama_workflow",
        input_values=inputs,
    )

    result = await workflow_tools._workflow_ready_step_batch(
        project_id="proj-1",
        template=template,
        template_id="general_short_drama_workflow",
        inputs=inputs,
        instance_id="wf_drama",
    )

    assert result["ok"] is True
    assert any(step_id.endswith("_main_character_image_prompt") for step_id in result["ready_step_ids"])
    assert "episode_segments_s1_segment_script" in result["ready_step_ids"]
    assert "episode_segments_s2_segment_script" in result["ready_step_ids"]
    assert not any(step_id.endswith("_scene") for step_id in result["ready_step_ids"])
    assert any(
        item["step_id"] == "episode_segments_s1_scene"
        and "episode_segments_s1_segment_script_canvas" in item["waiting_on"]
        for item in result["blocked_steps"]
    )


def test_workflow_dependency_completed_for_batch_waits_for_repeat_group_children() -> None:
    steps_by_id = {
        "characters_i1_prompt": {"id": "characters_i1_prompt", "repeat_group_id": "characters", "surface": "workflow_runtime"},
        "characters_i1_image": {"id": "characters_i1_image", "repeat_group_id": "characters", "node_type": "image"},
        "segment_s1_storyboard": {"id": "segment_s1_storyboard", "depends_on": ["characters"]},
    }
    records_by_step = {
        "characters_i1_prompt": {"status": "completed"},
        "characters_i1_image": {"status": "idle"},
    }

    assert workflow_tools._workflow_dependency_completed_for_batch(
        "characters",
        records_by_step=records_by_step,
        steps_by_id=steps_by_id,
        virtual_step_ids=set(),
        failed_step_ids=set(),
    ) is False

    records_by_step["characters_i1_image"] = {"status": "completed"}
    assert workflow_tools._workflow_dependency_completed_for_batch(
        "characters",
        records_by_step=records_by_step,
        steps_by_id=steps_by_id,
        virtual_step_ids=set(),
        failed_step_ids=set(),
    ) is True


@pytest.mark.asyncio
async def test_tool_search_finds_canvas_workflow_tools() -> None:
    result = await tool_meta_tools.tool_search(query="", category="workflow", limit=0)
    names = {item["name"] for item in result["tools"]}

    assert {
        "workflow.list_templates",
        "workflow.runtime_status",
        "workflow.run_step",
        "workflow.run_next",
        "workflow.run_all",
        "agent.run",
    } <= names
    assert not {
        "workflow.canvas.inspect",
        "workflow.draft.start",
        "workflow.draft.append_steps",
        "workflow.draft.commit",
        "workflow.spec.apply_patch",
        "workflow.spec.start",
        "workflow.spec.append_steps",
        "workflow.spec.commit",
        "workflow.spec.read",
        "workflow.spec.patch",
        "workflow.template.resolve",
        "workflow.template.read",
        "workflow.template.clone_to_artifact",
        "workflow.template.promote",
        "workflow.template.export",
    } & names
    assert "node.create" not in names
    assert all(item["category"] == "workflow" for item in result["tools"])
    run_step = next(item for item in result["tools"] if item["name"] == "workflow.run_step")
    assert "内联 workflow" not in " ".join(run_step.get("usage_hints") or [])

    described = await tool_meta_tools.tool_describe(["workflow.runtime_status", "workflow.run_next", "workflow.run_all"])
    described_by_name = {item["name"]: item for item in described["tools"]}
    assert described["not_found"] == []
    assert described_by_name["workflow.runtime_status"]["boundaries"]["is_read_only"] is True
    assert described_by_name["workflow.run_next"]["tier"] == 2
    assert "inputs" in described_by_name["workflow.run_all"]["input_schema"]["properties"]
    assert "workflow" not in described_by_name["workflow.run_all"]["input_schema"]["properties"]
    assert "workflow" not in described_by_name["workflow.run_next"]["input_schema"]["properties"]


@pytest.mark.asyncio
async def test_tool_search_finds_workflow_skill_template_tools() -> None:
    exact = await tool_meta_tools.tool_search(
        query="agent.run",
        category="workflow",
        limit=8,
    )
    exact_names = [item["name"] for item in exact["tools"]]
    assert "agent.run" in exact_names

    save = await tool_meta_tools.tool_search(
        query="template save reusable workflow",
        category="workflow",
        limit=8,
    )
    save_names = [item["name"] for item in save["tools"]]
    assert "workflow.template.promote" not in save_names

    workflow_agent = await tool_meta_tools.tool_search(
        query="workflow_spec",
        category="workflow",
        limit=8,
    )
    workflow_agent_names = [item["name"] for item in workflow_agent["tools"]]
    assert "agent.run" in workflow_agent_names
    assert "workflow.template.resolve" not in workflow_agent_names

    run = await tool_meta_tools.tool_search(
        query="workflow.run",
        category="workflow",
        limit=8,
    )
    run_names = [item["name"] for item in run["tools"]]
    assert "workflow.run_all" in run_names
    assert "workflow.template.resolve" not in run_names


@pytest.mark.asyncio
async def test_workflow_list_templates_returns_light_catalog() -> None:
    result = await workflow_tools.workflow_list_templates("proj-1", limit=3)

    assert result["ok"] is True
    assert result["returned"] <= 3
    assert len(json.dumps(result, ensure_ascii=False)) < 6000
    for template in result["templates"]:
        assert "steps" not in template
        assert "template_graph" not in template
        assert "inputs_schema" not in template
        assert "prompt_template" not in template
        assert len(template.get("description") or "") <= 180
        assert {"id", "name", "inputs", "required_inputs", "step_count"} <= set(template)


@pytest.mark.asyncio
async def test_workflow_template_resolve_returns_candidates_with_decision_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow_tools.workflow_template_store,
        "candidate_summaries_for_skill",
        lambda **kwargs: [{"id": "segment_flow", "name": "分段人物图", "match_score": 3}],
    )

    result = await workflow_tools.workflow_template_resolve(
        project_id="proj-1",
        skill_summary="分段 人物图",
        user_goal="复用模板",
    )

    assert result["ok"] is True
    assert result["total"] == 1
    assert "recommended_next_tool" not in result
    assert "recommended_agent" not in result
    assert "workflow_spec" in result["decision_hint"]


@pytest.mark.asyncio
async def test_workflow_template_resolve_covers_builtin_direct_template_inputs() -> None:
    result = await workflow_tools.workflow_template_resolve(
        project_id="proj-1",
        skill_name="general_short_drama_workflow",
        skill_summary="通用视频制作",
        user_goal="使用通用视频制作工作流做视频",
        inputs={"plot": "雨夜天台收到未来来信"},
    )

    assert result["ok"] is True
    direct = result["direct_template"]
    assert direct["template_id"] == "general_short_drama_workflow"
    assert direct["scope"] == "builtin"
    assert direct["missing_inputs"] == ["durationSeconds"]
    assert [item["id"] for item in direct["input_questions"]] == ["durationSeconds"]
    assert any(item["id"] == "general_short_drama_workflow" for item in result["candidates"])
    assert "direct_template" in result["decision_hint"]


@pytest.mark.asyncio
async def test_workflow_template_resolve_matches_builtin_template_from_chinese_goal() -> None:
    result = await workflow_tools.workflow_template_resolve(
        project_id="proj-1",
        skill_name="文生视频",
        user_goal="使用文生视频工作流做视频",
    )

    assert result["ok"] is True
    assert any(item["id"] == "general_short_drama_workflow" for item in result["candidates"])
    candidate = next(item for item in result["candidates"] if item["id"] == "general_short_drama_workflow")
    assert candidate["scope"] == "builtin"
    assert candidate["missing_inputs"] == ["plot", "durationSeconds"]
    assert [item["id"] for item in candidate["input_questions"]] == ["plot", "durationSeconds"]


@pytest.mark.asyncio
async def test_workflow_template_read_supports_builtin_templates() -> None:
    result = await workflow_tools.workflow_template_read(
        project_id="proj-1",
        template_id="general_short_drama_workflow",
        detail="workflow",
    )

    assert result["ok"] is True
    assert result["summary"]["scope"] == "builtin"
    assert [field["id"] for field in result["input_fields"]] == [
        "plot",
        "style",
        "type",
        "episodeCount",
        "durationSeconds",
        "segmentSeconds",
    ]
    assert all("missing" not in field for field in result["input_fields"])
    assert result["workflow"]["id"] == "general_short_drama_workflow"
    assert result["workflow"]["required_inputs"] == ["plot", "durationSeconds"]


@pytest.mark.asyncio
async def test_workflow_runtime_status_returns_saved_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_fake_workflow_runtime_state(monkeypatch)
    state["active_workflow"] = {"kind": "template", "template_id": "demo_flow"}
    state["workflow_runtime"] = {
        "instances": {
            "wf_demo": {
                "template_id": "demo_flow",
                "template_name": "演示流程",
                "steps": {"script": {"title": "剧本", "status": "completed"}},
            }
        }
    }
    state["workflow_input_values"] = {
        "by_workflow": {
            "demo_flow": {
                "values": {"plot": "雨夜追逃", "durationSeconds": 15},
            }
        },
        "by_instance": {
            "wf_demo": {
                "values": {"durationSeconds": 30, "segmentCount": 2},
            }
        },
    }

    result = await workflow_tools.workflow_runtime_status("proj-1")

    assert result["ok"] is True
    assert result["active_workflow"]["workflow_id"] == "demo_flow"
    assert result["runtime"]["instance_id"] == "wf_demo"
    assert result["workflow_input_values"] == {
        "durationSeconds": 30,
        "segmentCount": 2,
    }


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_general_short_drama_skill_is_default_workflow_source() -> None:
    result = await skill_tools.skill_search(
        query="通用视频制作工作流",
        category="workflow",
        scope="builtin",
    )
    names = {item["name"] for item in result["skills"]}

    assert "general_short_drama_workflow" in names

    loaded = await skill_tools.skill_get_skill(
        name="general_short_drama_workflow",
        category="workflow",
        scope="builtin",
        detail="full",
    )
    content = str(loaded["content"])

    assert "默认视频制作模板说明" in content
    assert "`general_short_drama_workflow`" in content
    assert "segment_script -> segment_script_canvas -> minor_characters -> scene -> scene_reference -> plan_frames -> storyboard -> video_prompt -> final_video" in content
    assert "`workflow.run_step`、`workflow.run_next` 或 `workflow.run_all`" in content
    assert "`prompt_template`" in content
    assert "`script_writing`" in content
    assert "`character_prompt`" in content
    assert "`scene_prompt`" in content
    assert "`shot_grid_prompt`" in content
    assert "`video_prompt`" in content


def test_inline_workflow_normalizes_model_authored_source_ids() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "video-short-drama",
            "name": "通用短剧一键生成式工作流",
            "inputs": {"plot": "江湖相逢", "episodeCount": 1},
            "steps": [
                {
                    "id": "episodePlan",
                    "title": "剧集规划",
                    "node_type": "text",
                    "source_node_id": "episodePlan",
                },
                {
                    "id": "videoPrompt",
                    "title": "视频提示词",
                    "node_type": "text",
                    "depends_on": ["episodePlan"],
                    "source_node_id": "videoPrompt",
                },
            ],
        }
    )

    assert template["id"] == "video_short_drama"
    assert template["inputs"] == [
        {"id": "plot", "default": "江湖相逢"},
        {"id": "episodeCount", "default": 1},
    ]
    assert [step["id"] for step in template["steps"]] == ["episode_plan", "video_prompt"]
    assert template["steps"][1]["depends_on"] == ["episode_plan"]
    assert template["steps"][0]["source_node_id"] == "episodePlan"
    assert template["steps"][1]["source_node_id"] == "videoPrompt"


def test_inline_workflow_repeat_group_expands_instance_steps() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "segment_reuse_workflow",
            "name": "复用段落流程",
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                },
                {
                    "id": "segmentFlow",
                    "title": "每段流程",
                    "depends_on": ["script"],
                    "repeat": {
                        "mode": "per_segment",
                        "source": "script.segments",
                        "episode_count": 1,
                        "segment_count": 2,
                    },
                    "steps": [
                        {
                            "id": "scene",
                            "title": "场景设定",
                            "node_type": "image",
                            "runner": "node.run",
                        },
                        {
                            "id": "videoPrompt",
                            "title": "视频提示词",
                            "node_type": "text",
                            "depends_on": ["scene"],
                            "runner": "node.run",
                        },
                    ],
                },
            ],
        }
    )

    assert [step["id"] for step in template["steps"]] == [
        "script",
        "segment_flow_e1_s1_scene",
        "segment_flow_e1_s1_video_prompt",
        "segment_flow_e1_s2_scene",
        "segment_flow_e1_s2_video_prompt",
    ]
    first_scene = template["steps"][1]
    first_prompt = template["steps"][2]
    assert first_scene["title"] == "第1集第1段 · 场景设定"
    assert first_scene["depends_on"] == ["script"]
    assert first_scene["instance_scope"] == {"episode": 1, "segment": 1, "index": 1}
    assert first_scene["template_step_id"] == "scene"
    assert first_scene["repeat_group_id"] == "segment_flow"
    assert first_scene["repeat_group_label"] == "每段流程"
    assert first_prompt["depends_on"] == ["script", "segment_flow_e1_s1_scene"]
    assert first_prompt["template_step_id"] == "video_prompt"
    assert first_prompt["runner"] == "node.run"


def test_inline_workflow_repeat_group_supports_previous_instance_dependencies() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "previous_instance_workflow",
            "name": "跨段连续流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {
                    "id": "segment_flow",
                    "title": "每段流程",
                    "depends_on": ["script"],
                    "repeat": {"episode_count": 1, "segment_count": 3},
                    "steps": [
                        {
                            "id": "scene",
                            "title": "场景",
                            "node_type": "text",
                            "depends_on_previous": ["scene"],
                        },
                        {
                            "id": "plan_frames",
                            "title": "分镜规划",
                            "node_type": "text",
                            "depends_on": ["scene"],
                            "depends_on_previous": ["plan_frames"],
                        },
                    ],
                },
            ],
        }
    )
    by_id = {step["id"]: step for step in template["steps"]}

    assert by_id["segment_flow_e1_s1_scene"]["depends_on"] == ["script"]
    assert by_id["segment_flow_e1_s2_scene"]["depends_on"] == [
        "script",
        "segment_flow_e1_s1_scene",
    ]
    assert by_id["segment_flow_e1_s3_scene"]["depends_on"] == [
        "script",
        "segment_flow_e1_s2_scene",
    ]
    assert by_id["segment_flow_e1_s2_plan_frames"]["depends_on"] == [
        "script",
        "segment_flow_e1_s2_scene",
        "segment_flow_e1_s1_plan_frames",
    ]


def test_inline_workflow_dimension_count_expands_group() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "dimension_count_workflow",
            "name": "输入数量展开",
            "dimensions": {
                "segments": {"input_count": "segment_count", "scope_key": "segment"}
            },
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {
                    "id": "segment_flow",
                    "title": "每段流程",
                    "depends_on": ["script"],
                    "foreach": {"dimension": "segments"},
                    "steps": [
                        {"id": "video_prompt", "title": "视频提示词", "node_type": "text"},
                    ],
                },
            ],
        },
        input_values={"segment_count": 2},
    )

    assert [step["id"] for step in template["steps"]] == [
        "script",
        "segment_flow_s1_video_prompt",
        "segment_flow_s2_video_prompt",
    ]
    assert template["steps"][1]["instance_scope"] == {"segment": 1, "index": 1}
    assert template["steps"][2]["title"] == "第2段 · 视频提示词"
    assert template["deferred_groups"] == []
    assert template["dimensions"]["segments"]["input_count"] == "segment_count"


@pytest.mark.asyncio
async def test_workflow_canvas_inspect_projects_expanded_canvas_mapping() -> None:
    result = await workflow_spec_tools.workflow_canvas_inspect(
        project_id="proj-1",
        workflow={
            "id": "projection_workflow",
            "name": "投影检查流程",
            "inputs": [
                {"id": "plot", "type": "long_text", "label": "剧情", "required": True},
                {"id": "duration_seconds", "type": "number", "default": 30},
                {"id": "segment_seconds", "type": "number", "default": 15},
                {"id": "segment_count", "type": "number", "default": 2},
            ],
            "required_inputs": ["plot"],
            "dimensions": {
                "segments": {"input_count": "segment_count", "scope_key": "segment"}
            },
            "steps": [
                {
                    "id": "full_script",
                    "title": "完整剧本",
                    "node_type": "text",
                    "surface": "workflow_runtime",
                    "prompt_template": "根据 {{inputs.plot}} 写完整剧本。",
                },
                {
                    "id": "segment_flow",
                    "title": "每段流程",
                    "depends_on": ["full_script"],
                    "foreach": {"dimension": "segments"},
                    "steps": [
                        {
                            "id": "segment_text",
                            "title": "分段剧情",
                            "node_type": "text",
                            "surface": "draft_canvas",
                        },
                        {
                            "id": "character_image",
                            "title": "人物图",
                            "node_type": "image",
                            "surface": "draft_canvas",
                            "depends_on": ["segment_text"],
                            "fields": {"width": 1024, "height": 1024},
                        },
                    ],
                },
            ],
        },
        inputs={
            "plot": "雨夜巷口的悬疑短剧",
            "duration_seconds": 30,
            "segment_seconds": 15,
            "segment_count": 2,
        },
    )

    assert result["ok"] is True
    assert result["validation"]["ok"] is True
    assert result["workflow"]["canvas_node_count"] == 4
    assert result["flow"]["executable_batches"] == [
        ["full_script"],
        ["segment_flow_s1_segment_text", "segment_flow_s2_segment_text"],
        ["segment_flow_s1_character_image", "segment_flow_s2_character_image"],
    ]
    assert [node["id"] for node in result["canvas"]["final_outputs"]] == [
        "segment_flow_s1_character_image",
        "segment_flow_s2_character_image",
    ]
    assert result["canvas"]["edges"] == [
        {"source": "segment_flow_s1_segment_text", "target": "segment_flow_s1_character_image", "kind": "depends_on"},
        {"source": "segment_flow_s2_segment_text", "target": "segment_flow_s2_character_image", "kind": "depends_on"},
    ]
    assert result["validation"]["dry_run"]["duration_segment_expectation"]["expected_segment_instances"] == 2


@pytest.mark.asyncio
async def test_workflow_canvas_inspect_reports_missing_collection_sample_context() -> None:
    workflow = {
        "id": "dynamic_collection_projection",
        "name": "动态集合投影",
        "dimensions": {"segments": {"source": "steps.segments.output.items", "scope_key": "segment"}},
        "steps": [
            {
                "id": "segments",
                "title": "分段清单",
                "kind": "collection",
                "node_type": "text",
                "surface": "workflow_runtime",
                "output_schema": {
                    "type": "collection",
                    "items_key": "items",
                    "fields": [
                        {"id": "segment_text", "type": "string", "required": True},
                        {"id": "duration_seconds", "type": "number"},
                    ],
                },
            },
            {
                "id": "segment_flow",
                "title": "逐段出图",
                "depends_on": ["segments"],
                "foreach": {"dimension": "segments"},
                "steps": [
                    {
                        "id": "segment_image",
                        "title": "本段图片",
                        "node_type": "image",
                        "surface": "draft_canvas",
                        "fields": {"width": 1024, "height": 1024},
                    }
                ],
            },
        ],
    }

    waiting = await workflow_spec_tools.workflow_canvas_inspect(
        project_id="proj-1",
        workflow=workflow,
        inputs={"plot": "雨夜电台"},
    )

    assert waiting["ok"] is True
    assert waiting["suggested_next"] == "provide_sample_outputs_then_reinspect"
    assert waiting["workflow"]["canvas_node_count"] == 0
    assert waiting["dynamic_inputs"]["status"] == "waiting_for_sample_outputs"
    missing = waiting["dynamic_inputs"]["missing_sample_outputs"]
    assert missing[0]["dimension"] == "segments"
    assert missing[0]["source"] == "steps.segments.output.items"
    assert missing[0]["context_example"]["segments"]["output"]["items"][0]["segment_text"] == "segment_text_sample"
    assert "Re-run workflow.canvas.inspect with context" in waiting["next_action"]

    expanded = await workflow_spec_tools.workflow_canvas_inspect(
        project_id="proj-1",
        workflow=workflow,
        inputs={"plot": "雨夜电台"},
        context={
            "segments": {
                "output": {
                    "items": [
                        {"id": "s1", "segment_text": "第一段", "duration_seconds": 15},
                        {"id": "s2", "segment_text": "第二段", "duration_seconds": 15},
                    ]
                }
            }
        },
    )

    assert expanded["suggested_next"] == "compare_projection_then_patch_or_report"
    assert expanded["dynamic_inputs"] == {"status": "ready", "missing_sample_outputs": []}
    assert [node["id"] for node in expanded["canvas"]["nodes"]] == [
        "segment_flow_s1_segment_image",
        "segment_flow_s2_segment_image",
    ]


def test_inline_workflow_dimension_expands_from_runtime_output_record() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "runtime_output_dimension_workflow",
            "name": "运行输出维度展开",
            "dimensions": {
                "segments": {
                    "from_step": "productionPlan",
                    "path": "segments",
                }
            },
            "steps": [
                {
                    "id": "productionPlan",
                    "title": "生产规划",
                    "node_type": "text",
                    "surface": "workflow_runtime",
                },
                {
                    "id": "segmentProduction",
                    "title": "逐段生产",
                    "depends_on": ["productionPlan"],
                    "foreach": {"dimension": "segments"},
                    "steps": [
                        {
                            "id": "storyTemplate",
                            "title": "故事模板图",
                            "node_type": "image",
                            "fields": {"prompt": "{{story_goal}}"},
                        },
                    ],
                },
            ],
        },
        input_values={
            "context": {
                "production_plan": {
                    "output": {
                        "segments": [
                            {"segment": 1, "story_goal": "雨夜包抄"},
                            {"segment": 2, "story_goal": "街口突围"},
                        ]
                    }
                }
            }
        },
    )

    assert [step["id"] for step in template["steps"]] == [
        "production_plan",
        "segment_production_s1_story_template",
        "segment_production_s2_story_template",
    ]
    assert template["steps"][1]["title"] == "第1段 · 故事模板图"
    assert template["steps"][1]["fields"]["prompt"] == "雨夜包抄"
    assert template["steps"][2]["depends_on"] == ["production_plan"]
    assert template["deferred_groups"] == []


def test_workflow_reference_selector_uses_exact_character_tokens() -> None:
    selectors = [
        {
            "from_group": "main_character_images",
            "source_step": "planFrames",
            "source_path": "output.appearing_characters",
            "match_fields": ["name", "reuse_key", "title"],
            "role": "visual_reference",
        }
    ]
    context = {"planFrames": {"output": {"appearing_characters": ["林舟"]}}}
    nodes = [
        {
            "id": "node-1",
            "display_id": 1,
            "type": "image",
            "title": "林舟",
            "input": {
                "workflow": {
                    "template_id": "tpl",
                    "instance_id": "wf-1",
                    "repeat_group_id": "main_character_images",
                    "template_step_id": "main_character_image",
                    "instance_scope": {"name": "林舟", "reuse_key": "lin_zhou"},
                }
            },
        },
        {
            "id": "node-2",
            "display_id": 2,
            "type": "image",
            "title": "旧照中的林舟",
            "input": {
                "workflow": {
                    "template_id": "tpl",
                    "instance_id": "wf-1",
                    "repeat_group_id": "main_character_images",
                    "template_step_id": "main_character_image",
                    "instance_scope": {"name": "旧照中的林舟", "reuse_key": "old_photo_lin_zhou"},
                }
            },
        },
        {
            "id": "workflow-runtime:wf-1:main_character_images_i1_main_character_image",
            "title": "林舟",
            "surface": "draft_canvas",
            "input": {
                "workflow": {
                    "template_id": "tpl",
                    "instance_id": "wf-1",
                    "repeat_group_id": "main_character_images",
                    "template_step_id": "main_character_image",
                    "instance_scope": {"name": "林舟", "reuse_key": "lin_zhou"},
                }
            },
        },
    ]

    selected = workflow_tools._workflow_reference_selector_nodes(
        selectors,
        nodes=nodes,
        context=context,
        template_id="tpl",
        instance_id="wf-1",
    )

    assert [node["id"] for node, _ in selected] == ["node-1"]


def test_workflow_dependency_refs_do_not_expand_runtime_upstream_to_canvas() -> None:
    visible_nodes = [
        {
            "id": "node-script",
            "display_id": 0,
            "type": "text",
            "title": "剧本",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "script_canvas",
            },
        },
        {
            "id": "node-scene-1",
            "display_id": 1,
            "type": "image",
            "title": "第1段 · 场景参考图",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s1_scene_reference",
                "template_step_id": "scene_reference",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 1,
                "instance_scope": {"index": 1, "segment": 1},
            },
        },
        {
            "id": "node-scene-2",
            "display_id": 2,
            "type": "image",
            "title": "第2段 · 场景参考图",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s2_scene_reference",
                "template_step_id": "scene_reference",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 2,
                "instance_scope": {"index": 2, "segment": 2},
            },
        },
        {
            "id": "node-storyboard-1",
            "display_id": 3,
            "type": "image",
            "title": "第1段 · 宫格分镜图",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s1_storyboard",
                "template_step_id": "storyboard",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 1,
                "instance_scope": {"index": 1, "segment": 1},
            },
        },
    ]
    runtime_records = [
        {
            "id": "workflow-runtime:wf-test:episode_segments_s2_storyboard_prompt",
            "surface": "workflow_runtime",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s2_storyboard_prompt",
                "template_step_id": "storyboard_prompt",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 2,
                "depends_on": [
                    "script_canvas",
                    "episode_segments_s2_plan_frames",
                    "episode_segments_s1_storyboard",
                ],
            },
        },
        {
            "id": "workflow-runtime:wf-test:episode_segments_s2_plan_frames",
            "surface": "workflow_runtime",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s2_plan_frames",
                "template_step_id": "plan_frames",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 2,
                "depends_on": ["episode_segments_s2_scene_reference", "episode_segments_s1_storyboard"],
            },
        },
    ]
    all_records = [*visible_nodes, *runtime_records]
    created_by_step = workflow_tools._workflow_step_nodes_by_id(
        all_records,
        "general_short_drama_workflow",
        "wf-test",
    )
    nodes_by_alias = workflow_tools._workflow_step_nodes_by_alias(
        all_records,
        "general_short_drama_workflow",
        "wf-test",
    )

    refs = workflow_tools._workflow_dependency_refs_for_step(
        {
            "id": "episode_segments_s2_storyboard",
            "template_step_id": "storyboard",
            "repeat_group_id": "episode_segments",
            "repeat_group_index": 2,
            "instance_scope": {"index": 2, "segment": 2},
            "depends_on": ["episode_segments_s2_storyboard_prompt"],
            "context_refs": [{"ref": "scene_reference", "role": "context"}],
        },
        created_by_step=created_by_step,
        nodes_by_alias=nodes_by_alias,
        steps_by_id={
            "episode_segments_s2_storyboard_prompt": {
                "depends_on": [
                    "script_canvas",
                    "episode_segments_s2_plan_frames",
                    "episode_segments_s1_storyboard",
                ],
            },
            "episode_segments_s2_plan_frames": {
                "depends_on": ["episode_segments_s2_scene_reference", "episode_segments_s1_storyboard"],
            },
        },
        virtual_step_ids=set(),
    )

    assert refs == [{"ref": "node:2", "role": "context"}]


def test_merge_workflow_dependency_refs_dedupes_depends_on_by_ref() -> None:
    fields = workflow_tools._merge_workflow_dependency_refs(
        {},
        [
            {"ref": "node:1", "role": "context"},
            {"ref": "node:1", "role": "visual_reference"},
            {"ref": "node:2", "role": "context"},
        ],
    )

    assert fields["references"] == [
        {"ref": "node:1", "role": "context"},
        {"ref": "node:1", "role": "visual_reference"},
        {"ref": "node:2", "role": "context"},
    ]
    assert fields["depends_on"] == ["node:1", "node:2"]


def test_workflow_dependency_refs_filter_same_repeat_scope_alias() -> None:
    nodes = [
        {
            "id": "node-scene-1",
            "display_id": 1,
            "type": "image",
            "title": "第1段 · 场景参考图",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s1_scene_reference",
                "template_step_id": "scene_reference",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 1,
                "instance_scope": {"index": 1, "segment": 1},
            },
        },
        {
            "id": "node-scene-2",
            "display_id": 2,
            "type": "image",
            "title": "第2段 · 场景参考图",
            "workflow": {
                "template_id": "general_short_drama_workflow",
                "instance_id": "wf-test",
                "step_id": "episode_segments_s2_scene_reference",
                "template_step_id": "scene_reference",
                "repeat_group_id": "episode_segments",
                "repeat_group_index": 2,
                "instance_scope": {"index": 2, "segment": 2},
            },
        },
    ]
    created_by_step = workflow_tools._workflow_step_nodes_by_id(
        nodes,
        "general_short_drama_workflow",
        "wf-test",
    )
    nodes_by_alias = workflow_tools._workflow_step_nodes_by_alias(
        nodes,
        "general_short_drama_workflow",
        "wf-test",
    )

    refs = workflow_tools._workflow_dependency_refs_for_step(
        {
            "id": "episode_segments_s1_storyboard",
            "template_step_id": "storyboard",
            "repeat_group_id": "episode_segments",
            "repeat_group_index": 1,
            "instance_scope": {"index": 1, "segment": 1},
            "context_refs": [{"ref": "scene_reference", "role": "context"}],
        },
        created_by_step=created_by_step,
        nodes_by_alias=nodes_by_alias,
        steps_by_id={},
        virtual_step_ids=set(),
    )

    assert refs == [{"ref": "node:1", "role": "context"}]


def test_repeat_group_control_dependencies_do_not_become_canvas_references() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "repeat_control_flow",
            "name": "Repeat Control Flow",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {"id": "character_image", "title": "人物图", "node_type": "image", "depends_on": ["script"]},
                {
                    "id": "segments",
                    "title": "分段",
                    "depends_on": ["script", "character_image"],
                    "repeat": {"count": 1},
                    "steps": [
                        {
                            "id": "scene_prompt",
                            "title": "场景提示词",
                            "node_type": "text",
                            "surface": "workflow_runtime",
                        },
                        {
                            "id": "scene_reference",
                            "title": "场景参考图",
                            "node_type": "image",
                            "depends_on": ["scene_prompt"],
                        },
                    ],
                },
            ],
        },
    )
    steps_by_id = {step["id"]: step for step in template["steps"]}
    scene_prompt = next(step for step in template["steps"] if step.get("template_step_id") == "scene_prompt")
    scene_reference = next(step for step in template["steps"] if step.get("template_step_id") == "scene_reference")
    assert scene_reference["_control_depends_on"] == ["script", "character_image"]

    records = [
        {
            "id": "node-script",
            "display_id": 1,
            "surface": "draft_canvas",
            "workflow": {"template_id": "repeat_control_flow", "instance_id": "wf-test", "step_id": "script"},
        },
        {
            "id": "node-character",
            "display_id": 2,
            "surface": "draft_canvas",
            "workflow": {"template_id": "repeat_control_flow", "instance_id": "wf-test", "step_id": "character_image"},
        },
        {
            "id": f"workflow-runtime:wf-test:{scene_prompt['id']}",
            "surface": "workflow_runtime",
            "workflow": {
                "template_id": "repeat_control_flow",
                "instance_id": "wf-test",
                "step_id": scene_prompt["id"],
                "depends_on": scene_prompt["depends_on"],
            },
        },
    ]
    created_by_step = workflow_tools._workflow_step_nodes_by_id(records, "repeat_control_flow", "wf-test")
    nodes_by_alias = workflow_tools._workflow_step_nodes_by_alias(records, "repeat_control_flow", "wf-test")

    refs = workflow_tools._workflow_dependency_refs_for_step(
        scene_reference,
        created_by_step=created_by_step,
        nodes_by_alias=nodes_by_alias,
        steps_by_id=steps_by_id,
        virtual_step_ids=set(),
        include_runtime_upstream=True,
        extra_dep_keys=[next(iter(workflow_tools._workflow_data_dependency_ids(scene_reference)), "")],
    )

    assert refs == []


def test_general_short_drama_template_has_no_default_previous_segment_visual_deps() -> None:
    template = canvas_workflow_templates.get_template(
        "general_short_drama_workflow",
        input_values={"plot": "雨夜追击", "durationSeconds": 30, "segmentSeconds": 15},
    )

    assert all("depends_on_previous" not in step for step in template["steps"])
    assert "previous_segment" not in json.dumps(template, ensure_ascii=False)


def test_inline_workflow_repeat_group_uses_input_count_keys() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "reusable_segment_workflow",
            "name": "复用段落流程",
            "inputs": [
                {"id": "episodeCount", "type": "number", "default": 1},
                {"id": "segmentCount", "type": "number", "default": 2},
            ],
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {
                    "id": "segmentFlow",
                    "title": "每段流程",
                    "depends_on": ["script"],
                    "repeat": {"episode_count": "episodeCount", "segment_count": "segmentCount"},
                    "steps": [
                        {"id": "videoPrompt", "title": "视频提示词", "node_type": "text"},
                    ],
                },
            ],
        },
        input_values={"episodeCount": 1, "segmentCount": 3},
    )

    assert [step["id"] for step in template["steps"]] == [
        "script",
        "segment_flow_e1_s1_video_prompt",
        "segment_flow_e1_s2_video_prompt",
        "segment_flow_e1_s3_video_prompt",
    ]
    assert template["steps"][3]["instance_scope"] == {"episode": 1, "segment": 3, "index": 3}


def test_inline_workflow_unresolved_foreach_becomes_deferred_group() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "deferred_dimension_workflow",
            "name": "延迟展开",
            "dimensions": {
                "segments": {"from_step": "structure_plan", "path": "episodes[].segments[]"}
            },
            "steps": [
                {"id": "structure_plan", "title": "分集分段规划", "node_type": "text"},
                {
                    "id": "segment_flow",
                    "title": "每段流程",
                    "depends_on": ["structure_plan"],
                    "foreach": {"dimension": "segments"},
                    "steps": [
                        {"id": "shot_grid", "title": "几宫格分镜", "node_type": "image"},
                    ],
                },
            ],
        }
    )

    assert [step["id"] for step in template["steps"]] == ["structure_plan"]
    assert template["deferred_groups"][0]["id"] == "segment_flow"
    assert template["deferred_groups"][0]["foreach"] == {"dimension": "segments"}
    assert template["deferred_groups"][0]["status"] == "deferred"


def test_workflow_dimension_inputs_expose_step_outputs_for_deferred_foreach() -> None:
    workflow = {
        "id": "dynamic_segment_workflow",
        "name": "动态分段流程",
        "dimensions": {
            "segments": {"source": "steps.plan_segments.output.segments"},
        },
        "steps": [
            {"id": "plan_segments", "title": "分段规划", "node_type": "text", "surface": "workflow_runtime"},
            {
                "id": "segment_flow",
                "title": "每段流程",
                "depends_on": ["plan_segments"],
                "foreach": {"dimension": "segments"},
                "steps": [
                    {"id": "segment_story", "title": "分段剧情", "node_type": "text", "surface": "workflow_runtime"},
                ],
            },
        ],
    }

    initial = canvas_workflow_templates.normalize_inline_workflow(workflow)
    assert [step["id"] for step in initial["steps"]] == ["plan_segments"]
    expanded = canvas_workflow_templates.normalize_inline_workflow(
        workflow,
        input_values=workflow_tools._dimension_input_values(
            {},
            {
                "plan_segments": {
                    "output": {
                        "segments": [
                            {"id": "a", "index": 1, "storyBeat": "开端"},
                            {"id": "b", "index": 2, "storyBeat": "反转"},
                        ]
                    }
                }
            },
        ),
    )

    assert expanded["deferred_groups"] == []
    assert [step["id"] for step in expanded["steps"]] == [
        "plan_segments",
        "segment_flow_a_segment_story",
        "segment_flow_b_segment_story",
    ]


def test_workflow_prompt_template_resolves_steps_and_item_name_aliases() -> None:
    rendered = node_universal._workflow_render_prompt_template(
        "规划：{{steps.plan_segments.output.segments}} 当前：{{segment}}",
        workflow={
            "item_name": "segment",
            "instance_scope": {"id": "s1", "index": 1, "storyBeat": "开端"},
        },
        target={"id": "segment_flow_s1_segment_story"},
        upstream_nodes=[
            {
                "id": "plan_segments",
                "title": "分段规划",
                "output": {"segments": [{"id": "s1", "storyBeat": "开端"}]},
                "workflow": {"step_id": "plan_segments"},
            }
        ],
    )

    assert rendered["unresolved_template_paths"] == []
    assert "storyBeat" in rendered["rendered_prompt_template"]
    assert "开端" in rendered["rendered_prompt_template"]


def test_inline_workflow_segment_foreach_uses_duration_placeholders() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "id": "segment_placeholder_workflow",
            "name": "分段占位流程",
            "steps": [
                {"id": "workflow_inputs", "title": "输入", "node_type": "text", "surface": "workflow_runtime"},
                {
                    "id": "split_segments",
                    "title": "切分剧情",
                    "node_type": "text",
                    "depends_on": ["workflow_inputs"],
                    "surface": "workflow_runtime",
                },
                {
                    "id": "segment_flow",
                    "title": "每段人物图",
                    "depends_on": ["split_segments"],
                    "foreach": {"from": "split_segments", "path": "segments"},
                    "steps": [
                        {
                            "id": "character_prompt",
                            "title": "人物提示词",
                            "node_type": "text",
                            "surface": "workflow_runtime",
                        },
                        {
                            "id": "character_image",
                            "title": "人物图",
                            "node_type": "image",
                            "depends_on": ["character_prompt"],
                        },
                    ],
                },
            ],
        },
        input_values={"duration_seconds": 45, "segment_seconds": 15},
    )

    assert template["deferred_groups"] == []
    assert [step["id"] for step in template["steps"]] == [
        "workflow_inputs",
        "split_segments",
        "segment_flow_s1_character_prompt",
        "segment_flow_s1_character_image",
        "segment_flow_s2_character_prompt",
        "segment_flow_s2_character_image",
        "segment_flow_s3_character_prompt",
        "segment_flow_s3_character_image",
    ]
    assert template["steps"][2]["instance_scope"] == {
        "index": 1,
        "segment": 1,
        "segment_index": 1,
        "start_second": 0,
        "end_second": 15,
        "duration_seconds": 15,
        "placeholder": True,
    }
    assert template["steps"][3]["depends_on"] == [
        "split_segments",
        "segment_flow_s1_character_prompt",
    ]


@pytest.mark.asyncio
async def test_workflow_materialize_creates_nodes_edges_and_public_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        model_config = kwargs.get("model_config") or {}
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "surface": model_config.get("surface") or "draft_canvas",
            "prompt": kwargs.get("prompt"),
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "source": kwargs["source_node_id"],
            "target": kwargs["target_node_id"],
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        events.append((action, payload))

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {"node-1": "1", "node-2": "2"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)

    result = await workflow_tools.workflow_materialize(
        project_id="project-1",
        workflow={
            "id": "custom_video_flow",
            "name": "自定义视频流程",
            "steps": [
                {
                    "id": "brief",
                    "title": "需求",
                    "node_type": "text",
                    "fields": {"content": "整理需求"},
                    "position": {"x": 0, "y": 0},
                },
                {
                    "id": "final_video",
                    "title": "视频",
                    "node_type": "video",
                    "depends_on": ["brief"],
                    "source_node_id": "videoPrompt",
                    "source_label": "视频提示词",
                    "source_category": "segment",
                    "mode": "grid",
                    "repeat": {"mode": "per_segment", "source": "script.segments", "label": "每段"},
                    "role": "template_step",
                    "expansion": {"mode": "per_segment", "source": "script.episodes[].segments[]", "label": "按段展开"},
                    "collection": {"kind": "segments", "items_source": "script.episodes[].segments[]", "label": "段落"},
                    "instance_scope": {"episode": 1, "segment": 2},
                    "template_step_id": "videoPrompt",
                    "expand_when": "after_script_segments",
                    "prompt_ref": "video_prompt#video_prompt",
                    "prompt_spec": {"goal": "按分镜写视频提示词", "output": "fields.content"},
                    "runner": "node.run",
                    "optional": True,
                    "manual_only": True,
                    "source_behavior": "source metadata survives materialization",
                    "fields": {"prompt": "待写最终视频提示词"},
                    "position": {"x": 400, "y": 0},
                },
            ],
        },
        inputs={"topic": "江湖相逢"},
    )

    assert result["ok"] is True
    assert result["created_count"] == 2
    assert result["edges_count"] == 1
    assert result["runtime"]["steps"][0]["artifact_count"] == 1
    assert len(runtime_state["workflow_runtime"]["instances"][result["instance_id"]]["steps"]) == 2
    runtime_steps = runtime_state["workflow_runtime"]["instances"][result["instance_id"]]["steps"]
    assert runtime_steps["brief"]["input"]["workflow"]["input_facts"] == {"topic": "江湖相逢"}
    assert runtime_steps["final_video"]["input"]["workflow"]["input_facts"] == {"topic": "江湖相逢"}
    assert [node["id"] for node in result["nodes"]] == ["1", "2"]
    video_input = result["nodes"][1]["input"]
    assert video_input["workflow"]["step_id"] == "final_video"
    assert video_input["workflow"]["surface"] == "draft_canvas"
    assert video_input["workflow"]["visibility"] == "canvas"
    assert video_input["workflow"]["input_facts"] == {"topic": "江湖相逢"}
    assert video_input["workflow"]["source_node_id"] == "videoPrompt"
    assert video_input["workflow"]["source_label"] == "视频提示词"
    assert video_input["workflow"]["source_category"] == "segment"
    assert video_input["workflow"]["mode"] == "grid"
    assert video_input["workflow"]["repeat"] == {"mode": "per_segment", "source": "script.segments", "label": "每段"}
    assert video_input["workflow"]["role"] == "template_step"
    assert video_input["workflow"]["expansion"] == {
        "mode": "per_segment",
        "source": "script.episodes[].segments[]",
        "label": "按段展开",
    }
    assert video_input["workflow"]["collection"] == {
        "kind": "segments",
        "items_source": "script.episodes[].segments[]",
        "label": "段落",
    }
    assert video_input["workflow"]["instance_scope"] == {"episode": 1, "segment": 2}
    assert video_input["workflow"]["template_step_id"] == "videoPrompt"
    assert video_input["workflow"]["expand_when"] == "after_script_segments"
    assert video_input["workflow"]["prompt_ref"] == "video_prompt#video_prompt"
    assert video_input["workflow"]["prompt_spec"] == {"goal": "按分镜写视频提示词", "output": "fields.content"}
    assert video_input["workflow"]["runner"] == "node.run"
    assert video_input["workflow"]["optional"] is True
    assert video_input["workflow"]["manual_only"] is True
    assert video_input["workflow"]["source_behavior"] == "source metadata survives materialization"
    assert video_input["references"] == [{"ref": "node:1", "role": "context"}]
    assert video_input["depends_on"] == ["node:1"]
    assert [action for action, _ in events] == ["create_node", "create_node", "add_edge"]


@pytest.mark.asyncio
async def test_workflow_materialize_bridges_runtime_dependencies_to_visible_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        model_config = kwargs.get("model_config") or {}
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "surface": model_config.get("surface") or "draft_canvas",
            "input": kwargs["input_data"],
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {"node-1": "1", "node-2": "2"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    install_fake_workflow_runtime_state(monkeypatch)

    result = await workflow_tools.workflow_materialize(
        project_id="project-1",
        workflow={
            "id": "runtime_bridge_workflow",
            "name": "运行时桥接",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {
                    "id": "shot_plan",
                    "title": "分镜规划",
                    "node_type": "text",
                    "surface": "workflow_runtime",
                    "depends_on": ["script"],
                },
                {
                    "id": "storyboard",
                    "title": "故事模板图",
                    "node_type": "image",
                    "depends_on": ["shot_plan"],
                },
            ],
        },
    )

    assert result["ok"] is True
    assert result["created_count"] == 2
    assert created_edges == [
        {"id": "edge-1", "source_node_id": "node-1", "target_node_id": "node-2", "label": ""}
    ]
    storyboard_input = created_nodes[1]["input"]
    assert storyboard_input["depends_on"] == ["node:1"]
    assert storyboard_input["references"] == [{"ref": "node:1", "role": "context"}]


@pytest.mark.asyncio
async def test_workflow_materialize_expands_repeat_group_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        model_config = kwargs.get("model_config") or {}
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "surface": model_config.get("surface") or "draft_canvas",
            "prompt": kwargs.get("prompt"),
        }
        node["input"] = kwargs["input_data"]
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {f"node-{index}": str(index) for index in range(1, 6)}

    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)

    result = await workflow_tools.workflow_materialize(
        project_id="proj-1",
        workflow={
            "id": "segment_reuse_workflow",
            "name": "复用段落流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
                {
                    "id": "segment_flow",
                    "title": "每段流程",
                    "depends_on": ["script"],
                    "repeat": {"mode": "per_segment", "episode_count": 1, "segment_count": 2},
                    "steps": [
                        {"id": "scene", "title": "场景设定", "node_type": "image", "runner": "node.run"},
                        {
                            "id": "video_prompt",
                            "title": "视频提示词",
                            "node_type": "text",
                            "depends_on": ["scene"],
                            "runner": "node.run",
                        },
                    ],
                },
            ],
        },
    )

    assert result["ok"] is True
    assert result["created_count"] == 5
    assert result["edges_count"] == 2
    assert len(runtime_state["workflow_runtime"]["instances"][result["instance_id"]]["steps"]) == 5
    assert [node["title"] for node in created_nodes] == [
        "剧本",
        "第1集第1段 · 场景设定",
        "第1集第1段 · 视频提示词",
        "第1集第2段 · 场景设定",
        "第1集第2段 · 视频提示词",
    ]
    assert [node["position"] for node in created_nodes] == [
        {"x": 120, "y": 120},
        {"x": 120, "y": 360},
        {"x": 120, "y": 600},
        {"x": 480, "y": 360},
        {"x": 480, "y": 600},
    ]
    first_scene_workflow = created_nodes[1]["input"]["workflow"]
    assert first_scene_workflow["repeat_group_id"] == "segment_flow"
    assert first_scene_workflow["template_step_id"] == "scene"
    assert first_scene_workflow["instance_scope"] == {"episode": 1, "segment": 1, "index": 1}
    assert created_nodes[2]["input"]["depends_on"] == ["node:2"]
    assert created_nodes[2]["input"]["workflow"]["runner"] == "node.run"


@pytest.mark.asyncio
async def test_workflow_run_step_materializes_and_runs_incrementally(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    run_calls: list[tuple[str, str | None]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(nodes) + 1
        workflow = kwargs["input_data"]["workflow"]
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "workflow": workflow,
            "input": kwargs["input_data"],
        }
        nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        edges.append(edge)
        return edge

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                return dict(node)
        return {"error": "Node not found"}

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                if "input_data" in patch:
                    node["input"] = patch["input_data"]
                    node["workflow"] = patch["input_data"].get("workflow") or {}
                if "prompt" in patch:
                    node["prompt"] = patch["prompt"] or ""
                if "status" in patch:
                    node["status"] = patch["status"]
                if "output_data" in patch:
                    node["output"] = patch["output_data"]
        return {"id": node_id}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_node_run(project_id: str, node_id: str, action: str | None = None, **kwargs: Any) -> dict[str, Any]:
        run_calls.append((node_id, action))
        return {"ok": True, "node_id": node_id, "status": "completed"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_noop_sync_dependency_edges)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(node_universal, "node_run", fake_node_run)

    workflow = {
        "id": "incremental_flow",
        "name": "增量流程",
        "steps": [
            {"id": "brief", "title": "需求", "kind": "canvas_text", "node_type": "text", "fields": {"content": "待生成"}},
            {"id": "cover", "title": "封面图", "kind": "image", "node_type": "image", "depends_on": ["brief"], "manual_only": True, "fields": {"workflow_source_path": "output.content"}},
        ],
    }

    first = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow=workflow,
        step_id="brief",
        inputs={"plot": "雨夜追逃"},
    )
    second = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow=workflow,
        step_id="cover",
        instance_id=first["instance_id"],
        inputs={"plot": "雨夜追逃"},
    )

    assert first["ok"] is True
    assert first["created"] is True
    assert second["ok"] is True
    assert second["created"] is True
    assert [node["id"] for node in nodes] == ["node-1", "node-2"]
    assert edges == [
        {
            "id": "edge-1",
            "source_node_id": "node-1",
            "target_node_id": "node-2",
            "label": "",
        }
    ]
    assert nodes[1]["input"]["references"] == [{"ref": "node:1", "role": "context"}]
    assert nodes[1]["input"]["prompt"] == "待生成"
    assert run_calls == []
    assert second["awaiting_manual_generation"] is True
    assert second["run_result"]["status"] == "awaiting_manual_generation"
    assert nodes[1]["status"] == "idle"
    assert nodes[0]["input"]["workflow"]["step_status"] == "completed"
    assert nodes[1]["input"]["workflow"]["step_status"] == "completed"
    assert nodes[1]["input"]["workflow"]["last_step_run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_run_step_marks_visible_step_failed_when_node_run_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    status_updates: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_materialize_workflow_step(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "created": True,
            "node_id": "node-script",
            "instance_id": kwargs["instance_id"] or "wf_visible",
            "node": {
                "id": "node-script",
                "type": "text",
                "title": "剧本",
                "input": {
                    "workflow": {
                        "template_id": "visible_fail_flow",
                        "instance_id": kwargs["instance_id"] or "wf_visible",
                        "step_id": kwargs["step_id"],
                        "runner": "node.run",
                    },
                },
            },
        }

    async def fake_prepare_visible_workflow_node_for_run(**kwargs: Any) -> dict[str, Any]:
        return kwargs["node"]

    async def fake_hydrate_workflow_node_with_inputs(node_id: str, inputs: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "id": node_id,
            "type": "text",
            "title": "剧本",
            "input": {
                "workflow": {
                    "template_id": "visible_fail_flow",
                    "instance_id": "wf_visible",
                    "step_id": "script",
                    "runner": "node.run",
                },
            },
        }

    async def fake_set_workflow_step_runtime(**kwargs: Any) -> dict[str, Any]:
        status_updates.append((kwargs["status"], kwargs.get("result")))
        return {"id": kwargs["node_id"], "status": kwargs["status"]}

    async def fake_node_run(project_id: str, node_id: str, action: str | None = None) -> dict[str, Any]:
        raise RuntimeError("node runner crashed")

    monkeypatch.setattr(workflow_tools, "_materialize_workflow_step", fake_materialize_workflow_step)
    monkeypatch.setattr(workflow_tools, "_prepare_visible_workflow_node_for_run", fake_prepare_visible_workflow_node_for_run)
    monkeypatch.setattr(workflow_tools, "_hydrate_workflow_node_with_inputs", fake_hydrate_workflow_node_with_inputs)
    monkeypatch.setattr(workflow_tools, "_set_workflow_step_runtime", fake_set_workflow_step_runtime)
    monkeypatch.setattr(node_universal, "node_run", fake_node_run)

    result = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow={
            "id": "visible_fail_flow",
            "name": "可见节点失败流程",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text", "runner": "node.run"},
            ],
        },
        step_id="script",
        instance_id="wf_visible",
    )

    assert result["ok"] is False
    assert result["error"] == "node runner crashed"
    assert result["error_kind"] == "RuntimeError"
    assert [status for status, _ in status_updates] == ["running", "failed"]
    assert status_updates[-1][1] == {
        "ok": False,
        "status": "failed",
        "error": "node runner crashed",
        "error_kind": "RuntimeError",
    }


@pytest.mark.asyncio
async def test_workflow_run_step_selects_only_appearing_character_references(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes: list[dict[str, Any]] = [
        {
            "id": "node-plan",
            "display_id": 1,
            "type": "text",
            "title": "Plan Frames",
            "status": "completed",
            "workflow": {
                "template_id": "selector_flow",
                "instance_id": "wf-test",
                "step_id": "plan_frames",
                "source_node_id": "planFrames",
            },
            "input": {
                "workflow": {
                    "template_id": "selector_flow",
                    "instance_id": "wf-test",
                    "step_id": "plan_frames",
                    "source_node_id": "planFrames",
                }
            },
            "output": {
                "type": "text",
                "content": json.dumps({"appearing_characters": [{"name": "林舟", "reuse_key": "lin_zhou"}]}),
            },
        },
        {
            "id": "node-scene",
            "display_id": 2,
            "type": "image",
            "title": "场景参考图",
            "status": "completed",
            "workflow": {
                "template_id": "selector_flow",
                "instance_id": "wf-test",
                "step_id": "scene_reference",
                "source_node_id": "sceneReference",
            },
            "input": {"workflow": {"template_id": "selector_flow", "instance_id": "wf-test", "step_id": "scene_reference"}},
        },
        {
            "id": "node-lin",
            "display_id": 3,
            "type": "image",
            "title": "林舟 · 主要人物参考图",
            "status": "completed",
            "workflow": {
                "template_id": "selector_flow",
                "instance_id": "wf-test",
                "step_id": "main_character_image_lin",
                "template_step_id": "main_character_image",
                "repeat_group_id": "main_character_images",
                "instance_scope": {"name": "林舟", "reuse_key": "lin_zhou"},
            },
            "input": {"workflow": {"template_id": "selector_flow", "instance_id": "wf-test", "repeat_group_id": "main_character_images"}},
        },
        {
            "id": "node-shen",
            "display_id": 4,
            "type": "image",
            "title": "沈鸢 · 主要人物参考图",
            "status": "completed",
            "workflow": {
                "template_id": "selector_flow",
                "instance_id": "wf-test",
                "step_id": "main_character_image_shen",
                "template_step_id": "main_character_image",
                "repeat_group_id": "main_character_images",
                "instance_scope": {"name": "沈鸢", "reuse_key": "shen_yuan"},
            },
            "input": {"workflow": {"template_id": "selector_flow", "instance_id": "wf-test", "repeat_group_id": "main_character_images"}},
        },
    ]
    edges: list[dict[str, Any]] = []
    agent_calls: list[dict[str, Any]] = []
    run_calls: list[tuple[str, str | None]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        node = {
            "id": "node-storyboard",
            "display_id": 5,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "workflow": kwargs["input_data"]["workflow"],
            "input": kwargs["input_data"],
        }
        nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        edges.append(edge)
        return edge

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                return dict(node)
        return {"error": "Node not found"}

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                if "input_data" in patch:
                    node["input"] = patch["input_data"]
                    node["workflow"] = patch["input_data"].get("workflow") or {}
                if "status" in patch:
                    node["status"] = patch["status"]
        return {"id": node_id}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_agent_run(**kwargs: Any) -> dict[str, Any]:
        agent_calls.append(kwargs)
        return {"ok": True, "status": "completed", "result": {"node_ids": [kwargs["inputs"]["node_id"]]}}

    async def fake_node_run(
        project_id: str,
        node_id: str,
        action: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        run_calls.append((node_id, action))
        return {"ok": True, "node_id": node_id, "status": "completed"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_noop_sync_dependency_edges)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(node_universal, "node_run", fake_node_run)
    monkeypatch.setattr(agent_tools, "agent_run", fake_agent_run)

    result = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow={
            "id": "selector_flow",
            "name": "选择参考图流程",
            "steps": [
                {"id": "plan_frames", "title": "Plan Frames", "node_type": "text"},
                {"id": "scene_reference", "title": "场景参考图", "node_type": "image", "runner": "node.run"},
                {
                    "id": "storyboard",
                    "title": "Storyboard",
                    "node_type": "image",
                    "depends_on": ["plan_frames", "scene_reference"],
                    "runner": "node.run",
                    "reference_selectors": [
                        {
                            "from_group": "main_character_images",
                            "source_step": "planFrames",
                            "source_path": "output.appearing_characters",
                            "match_fields": ["name", "reuse_key"],
                            "role": "visual_reference",
                        }
                    ],
                },
            ],
        },
        step_id="storyboard",
        instance_id="wf-test",
    )

    assert result["ok"] is True
    assert agent_calls == []
    assert run_calls == [("node-storyboard", "render")]
    storyboard_refs = nodes[-1]["input"]["references"]
    assert {"ref": "node:3", "role": "visual_reference"} in storyboard_refs
    assert all(ref.get("ref") != "node:4" for ref in storyboard_refs)
    assert {edge["source_node_id"] for edge in edges} == {"node-plan", "node-scene", "node-lin"}


@pytest.mark.asyncio
async def test_workflow_run_step_expands_template_step_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes: list[dict[str, Any]] = [
        {
            "id": "node-1",
            "display_id": 1,
            "type": "text",
            "title": "人物规划",
            "status": "completed",
            "workflow": {
                "template_id": "dynamic_character_flow",
                "instance_id": "wf-test",
                "step_id": "planner",
            },
            "input": {"workflow": {"template_id": "dynamic_character_flow", "instance_id": "wf-test", "step_id": "planner"}},
        }
    ]
    edges: list[dict[str, Any]] = []
    run_calls: list[tuple[str, str | None]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "workflow": kwargs["input_data"]["workflow"],
            "input": kwargs["input_data"],
        }
        nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        edges.append(edge)
        return edge

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                return dict(node)
        return {"error": "Node not found"}

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                if "input_data" in patch:
                    node["input"] = patch["input_data"]
                    node["workflow"] = patch["input_data"].get("workflow") or {}
                if "status" in patch:
                    node["status"] = patch["status"]
        return {"id": node_id}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_node_run(project_id: str, node_id: str, action: str | None = None) -> dict[str, Any]:
        run_calls.append((node_id, action))
        return {"ok": True, "node_id": node_id, "status": "completed"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_noop_sync_dependency_edges)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(node_universal, "node_run", fake_node_run)

    result = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow={
            "id": "dynamic_character_flow",
            "name": "动态人物流程",
            "steps": [
                {"id": "planner", "title": "人物规划", "node_type": "text"},
                {
                    "id": "character_images",
                    "title": "人物参考图集合",
                    "depends_on": ["planner"],
                    "foreach": {"from_step": "planner", "path": "output.main_characters"},
                    "steps": [
                        {"id": "character_image", "title": "人物参考图", "node_type": "image"},
                    ],
                },
            ],
        },
        step_id="character_image",
        context={
            "planner": {
                "output": {
                    "main_characters": [
                        {"name": "林舟", "character": "lin_zhou"},
                        {"name": "沈鸢", "character": "shen_yuan"},
                    ]
                }
            }
        },
    )

    assert result["ok"] is True
    assert result["node_ids"] == ["node-2", "node-3"]
    assert run_calls == [("node-2", "render"), ("node-3", "render")]
    assert nodes[1]["input"]["workflow"]["template_step_id"] == "character_image"
    assert nodes[2]["input"]["workflow"]["template_step_id"] == "character_image"


@pytest.mark.asyncio
async def test_builtin_workflow_template_uses_run_step_context() -> None:
    template, error = await workflow_tools._workflow_template_from_spec(
        project_id="project-1",
        template_id="general_short_drama_workflow",
        inputs={
            "plot": "雨夜误送古玉引来追兵",
            "durationSeconds": 15,
            "episodeCount": 1,
            "segmentSeconds": 15,
        },
        context={
            "main_characters": {
                "output": {
                    "main_characters": [
                        {"name": "林舟", "reuse_key": "lin_zhou"},
                    ]
                }
            }
        },
    )

    assert error is None
    assert template is not None
    assert workflow_tools._resolve_workflow_target_steps(template, "main_character_image")


@pytest.mark.asyncio
async def test_workflow_run_step_restores_deferred_context_from_existing_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes: list[dict[str, Any]] = [
        {
            "id": "node-1",
            "display_id": 1,
            "type": "text",
            "title": "人物规划",
            "status": "completed",
            "workflow": {
                "template_id": "dynamic_character_flow",
                "instance_id": "wf-test",
                "step_id": "planner",
            },
            "input": {"workflow": {"template_id": "dynamic_character_flow", "instance_id": "wf-test", "step_id": "planner"}},
            "output": {
                "type": "text",
                "content": "```json\n{\"main_characters\":[{\"name\":\"林舟\",\"reuse_key\":\"lin_zhou\"}]}\n```",
            },
        }
    ]
    edges: list[dict[str, Any]] = []
    run_calls: list[tuple[str, str | None]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "workflow": kwargs["input_data"]["workflow"],
            "input": kwargs["input_data"],
        }
        nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        edges.append(edge)
        return edge

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                return dict(node)
        return {"error": "Node not found"}

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                if "input_data" in patch:
                    node["input"] = patch["input_data"]
                    node["workflow"] = patch["input_data"].get("workflow") or {}
                if "status" in patch:
                    node["status"] = patch["status"]
        return {"id": node_id}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_node_run(project_id: str, node_id: str, action: str | None = None) -> dict[str, Any]:
        run_calls.append((node_id, action))
        return {"ok": True, "node_id": node_id, "status": "completed"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_noop_sync_dependency_edges)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(node_universal, "node_run", fake_node_run)

    result = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow={
            "id": "dynamic_character_flow",
            "name": "动态人物流程",
            "steps": [
                {"id": "planner", "title": "人物规划", "node_type": "text"},
                {
                    "id": "character_images",
                    "title": "人物参考图集合",
                    "depends_on": ["planner"],
                    "foreach": {"from_step": "planner", "path": "output.main_characters"},
                    "steps": [
                        {"id": "character_image", "title": "人物参考图", "node_type": "image"},
                    ],
                },
            ],
        },
        step_id="character_image",
    )

    assert result["ok"] is True
    assert result["node_id"] == "node-2"
    assert run_calls == [("node-2", "render")]
    assert nodes[1]["input"]["workflow"]["template_step_id"] == "character_image"


@pytest.mark.asyncio
async def test_workflow_run_step_treats_input_step_as_virtual_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)
    nodes: list[dict[str, Any]] = []
    updates: list[tuple[str, dict[str, Any]]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        node = {
            "id": "node-1",
            "display_id": 1,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "workflow": kwargs["input_data"]["workflow"],
            "input": kwargs["input_data"],
        }
        nodes.append(node)
        return dict(node)

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        return dict(nodes[0])

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updates.append((node_id, patch))
        if "input_data" in patch:
            nodes[0]["input"] = patch["input_data"]
            nodes[0]["workflow"] = patch["input_data"]["workflow"]
        if "status" in patch:
            nodes[0]["status"] = patch["status"]
        return {"id": node_id, "status": nodes[0]["status"]}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def unexpected_node_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("workflow input step must not call node.run")

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(node_universal, "node_run", unexpected_node_run)

    result = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow={
            "id": "input_flow",
            "name": "输入流程",
            "steps": [
                {
                    "id": "input",
                    "title": "输入",
                    "node_type": "text",
                    "runner": "workflow_input",
                    "inputs_schema": {"plot": {"type": "string"}},
                }
            ],
        },
        step_id="input",
        inputs={"plot": "雨夜追逃", "durationSeconds": 15},
    )

    assert result["ok"] is True
    assert result["run_result"]["result"]["input_facts"] == {
        "plot": "雨夜追逃",
        "durationSeconds": 15,
    }
    assert result["virtual"] is True
    assert result["node_id"] is None
    assert nodes == []
    assert updates == []
    assert workflow_tools.workflow_input_values_public_payload(
        runtime_state,
        workflow_id="input_flow",
        instance_id=result["instance_id"],
    ) == {"plot": "雨夜追逃", "durationSeconds": 15}


@pytest.mark.asyncio
async def test_workflow_run_step_auto_skips_when_input_condition_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes: list[dict[str, Any]] = []
    updates: list[tuple[str, dict[str, Any]]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [dict(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index - 1,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "workflow": kwargs["input_data"]["workflow"],
            "input": kwargs["input_data"],
        }
        nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        return {
            "id": "edge-1",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
        }

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                return dict(node)
        return {"error": "Node not found"}

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updates.append((node_id, patch))
        for node in nodes:
            if node["id"] == node_id:
                if "input_data" in patch:
                    node["input"] = patch["input_data"]
                    node["workflow"] = patch["input_data"]["workflow"]
                if "status" in patch:
                    node["status"] = patch["status"]
        return {"id": node_id, "status": patch.get("status")}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def unexpected_node_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("auto-skipped workflow step must not call node.run")

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(node_universal, "node_run", unexpected_node_run)

    workflow = {
        "id": "skip_flow",
        "name": "跳过流程",
        "steps": [
            {"id": "input", "title": "输入", "node_type": "text", "runner": "workflow_input"},
            {
                "id": "episode_plan",
                "title": "剧集规划",
                "node_type": "text",
                "depends_on": ["input"],
                "runner": "node.run",
                "auto_skip_when": "{{inputs.episodeCount}} <= 1",
            },
        ],
    }
    inputs = {"plot": "雨夜追逃", "episodeCount": 1}

    first = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow=workflow,
        step_id="input",
        inputs=inputs,
    )
    second = await workflow_tools.workflow_run_step(
        project_id="project-1",
        workflow=workflow,
        step_id="episode_plan",
        instance_id=first["instance_id"],
        inputs=inputs,
    )

    assert second["ok"] is True
    assert second["run_result"]["skipped"] is True
    assert second["virtual"] is True
    assert second["node_id"] is None
    assert nodes == []
    assert updates == []


@pytest.mark.asyncio
async def test_workflow_materialize_expands_dimension_from_planner_output(monkeypatch: pytest.MonkeyPatch) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "prompt": kwargs.get("prompt"),
            "input": kwargs["input_data"],
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {"node-1": "1", "node-2": "2"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)

    result = await workflow_tools.workflow_materialize(
        project_id="proj-1",
        workflow={
            "id": "scene_dimension_workflow",
            "name": "场景维度展开",
            "dimensions": {
                "segment_scenes": {
                    "from_step": "character_scene_plan",
                    "path": "segments[].scenes[]",
                }
            },
            "steps": [
                {"id": "character_scene_plan", "title": "人物场景规划", "node_type": "text"},
                {
                    "id": "scene_flow",
                    "title": "每个场景",
                    "depends_on": ["character_scene_plan"],
                    "foreach": {"dimension": "segment_scenes"},
                    "steps": [
                        {
                            "id": "scene_image",
                            "title": "场景图",
                            "node_type": "image",
                            "fields": {"prompt": "{{visual_brief}}"},
                            "prompt_spec": {"scene": "{{json}}"},
                        },
                    ],
                },
            ],
        },
        context={
            "character_scene_plan": {
                "segments": [
                    {
                        "episode": 1,
                        "segment": 2,
                        "scenes": [
                            {
                                "scene": "rain_alley",
                                "title": "雨夜巷口",
                                "visual_brief": "雨夜巷口，青石地面反光。",
                            }
                        ],
                    }
                ]
            }
        },
    )

    assert result["ok"] is True
    assert result["runtime"]["steps"][0]["artifact_count"] == 1
    assert len(runtime_state["workflow_runtime"]["instances"][result["instance_id"]]["steps"]) == 2
    assert result["created_count"] == 2
    assert result["deferred_group_count"] == 0
    assert created_edges == [
        {"id": "edge-1", "source_node_id": "node-1", "target_node_id": "node-2", "label": ""}
    ]
    scene_node = created_nodes[1]
    assert scene_node["title"] == "雨夜巷口 · 场景图"
    assert scene_node["input"]["prompt"] == "雨夜巷口，青石地面反光。"
    workflow = scene_node["input"]["workflow"]
    assert workflow["foreach"] == {"dimension": "segment_scenes"}
    assert workflow["repeat_group_id"] == "scene_flow"
    assert workflow["instance_scope"]["episode"] == 1
    assert workflow["instance_scope"]["segment"] == 2
    assert workflow["instance_scope"]["scene"] == "rain_alley"
    assert workflow["prompt_spec"]["scene"].startswith('{"episode": 1')


@pytest.mark.asyncio
async def test_workflow_materialize_artifact_uses_saved_spec(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "input": kwargs["input_data"],
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {"node-1": "1", "node-2": "2"}

    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)

    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "artifact_workflow",
            "name": "Artifact Workflow",
            "steps": [
                {"id": "brief", "title": "需求", "node_type": "text"},
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "depends_on": ["brief"],
                    "prompt_template": "根据输入需求和上游信息生成剧本草稿。",
                },
            ],
        },
    )

    result = await workflow_tools.workflow_materialize_artifact(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
    )

    assert result["ok"] is True
    assert result["artifact_ref"] == saved["artifact_ref"]
    assert len(runtime_state["workflow_runtime"]["instances"][result["instance_id"]]["steps"]) == 2
    assert result["created_count"] == 2
    assert result["edges_count"] == 1
    assert [node["title"] for node in created_nodes] == ["需求", "剧本"]
    assert created_nodes[1]["input"]["workflow"]["prompt_template"] == "根据输入需求和上游信息生成剧本草稿。"


@pytest.mark.asyncio
async def test_workflow_prompt_template_local_patch_and_reusable_template_e2e(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    project_id = "proj-prompt-template-e2e"
    template_root = tmp_path / "workflow_templates"
    tool_root = tmp_path / "tool_results"
    monkeypatch.setattr(workflow_template_store, "workflow_template_library_root", lambda: template_root)
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tool_root)
    install_fake_workflow_runtime_state(monkeypatch)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    run_calls: list[dict[str, Any]] = []

    async def fake_list_nodes(project_id: str) -> list[dict[str, Any]]:
        return [deepcopy(node) for node in nodes]

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(nodes) + 1
        input_data = deepcopy(kwargs["input_data"])
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "project_id": kwargs["project_id"],
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "surface": input_data.get("surface") or "draft_canvas",
            "workflow": input_data.get("workflow") or {},
            "input": input_data,
            "input_json": input_data,
            "prompt": kwargs.get("prompt") or input_data.get("prompt") or "",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "position_x": kwargs["position_x"],
            "position_y": kwargs["position_y"],
        }
        nodes.append(node)
        return deepcopy(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
            "label": kwargs.get("label") or "",
        }
        edges.append(edge)
        return deepcopy(edge)

    async def fake_get_node(node_id: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] == node_id:
                return deepcopy(node)
        return {"error": "Node not found"}

    async def fake_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        for node in nodes:
            if node["id"] != node_id:
                continue
            input_data = patch.get("input_data")
            if input_data is None:
                input_data = patch.get("input_json")
            if isinstance(input_data, dict):
                next_input = deepcopy(input_data)
                node["input"] = next_input
                node["input_json"] = next_input
                node["workflow"] = next_input.get("workflow") or {}
                node["surface"] = next_input.get("surface") or node.get("surface") or "draft_canvas"
            if "prompt" in patch:
                node["prompt"] = patch["prompt"]
            if "title" in patch:
                node["title"] = patch["title"]
            if "status" in patch:
                node["status"] = patch["status"]
            output = patch.get("output_data")
            if output is None:
                output = patch.get("output_json")
            if output is not None:
                node["output"] = deepcopy(output)
                node["output_json"] = deepcopy(output)
            if "error_message" in patch:
                node["error_message"] = patch["error_message"]
            return deepcopy(node)
        return {"error": "Node not found"}

    async def fake_sync_dependency_edges(project_id: str, node_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"changed": False}

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    async def fake_node_run(project_id: str, node_id: str, action: str | None = None) -> dict[str, Any]:
        node = next(item for item in nodes if item["id"] == node_id)
        workflow = node["input"]["workflow"]
        template = str(workflow.get("prompt_template") or "")
        output = {"content": f"used prompt template: {template}"}
        run_calls.append({"node_id": node_id, "action": action, "prompt_template": template})
        await fake_update_node(node_id, {"status": "completed", "output_data": output})
        return {"ok": True, "node_id": node_id, "status": "completed", "result": output, "content": output["content"]}

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {str(node["id"]): str(node["display_id"]) for node in nodes}

    async def fake_resolve_agent_node_id(project_id: str, node_id: Any) -> str:
        raw = str(node_id or "").replace("node:", "").strip()
        if raw.isdigit():
            return f"node-{raw}"
        return raw

    async def fake_node_public_id_map(project_id: str) -> dict[str, str]:
        return {str(node["id"]): str(node["display_id"]) for node in nodes}

    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", fake_get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "sync_dependency_edges", fake_sync_dependency_edges)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    monkeypatch.setattr(node_universal, "node_run", fake_node_run)
    monkeypatch.setattr(node_universal, "_resolve_agent_node_id", fake_resolve_agent_node_id)
    monkeypatch.setattr(node_universal, "_node_public_id_map", fake_node_public_id_map)

    original_template = "SYSTEM: 原始剧本模板\nUSER: {{inputs.plot}}\nOUTPUT: text"
    local_template = "SYSTEM: 当前实例强化模板\nUSER: {{inputs.plot}}\nOUTPUT: text"
    reusable_template = "SYSTEM: 可复用模板强化钩子\nUSER: {{inputs.plot}}\nOUTPUT: text"
    workflow = {
        "id": "plot_to_script_prompt_e2e",
        "name": "剧情到剧本提示词 E2E",
        "description": "用户输入剧情后生成剧本文本",
        "inputs": [{"id": "plot", "label": "剧情", "type": "textarea"}],
        "required_inputs": ["plot"],
        "steps": [
            {
                "id": "brief",
                "title": "输入",
                "node_type": "text",
                "runner": "node.run",
                "fields": {"purpose": "承接用户剧情输入"},
            },
            {
                "id": "script",
                "title": "剧本",
                "node_type": "text",
                "runner": "node.run",
                "depends_on": ["brief"],
                "prompt_template": original_template,
            },
        ],
    }
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id=project_id,
        workflow=workflow,
        normalized=canvas_workflow_templates.normalize_inline_workflow(workflow, input_values={"plot": "雨夜怀表"}),
        sample_inputs={"plot": "雨夜怀表"},
    )

    materialized = await workflow_tools.workflow_materialize_artifact(
        project_id=project_id,
        artifact_ref=saved["artifact_ref"],
        inputs={"plot": "雨夜怀表"},
    )
    assert materialized["ok"] is True
    assert materialized["created_count"] == 2
    assert edges == [{"id": "edge-1", "source_node_id": "node-1", "target_node_id": "node-2", "label": ""}]

    script_node = next(node for node in nodes if node["input"]["workflow"]["step_id"] == "script")
    assert script_node["input"]["workflow"]["prompt_template"] == original_template

    patched_fields = deepcopy(script_node["input"])
    patched_fields["workflow"] = {**patched_fields["workflow"], "prompt_template": local_template}
    updated = await node_universal.node_update(
        node_id=str(script_node["display_id"]),
        project_id=project_id,
        patch={"input_json": patched_fields},
    )
    assert updated["id"] == str(script_node["display_id"])
    assert script_node["input"]["workflow"]["prompt_template"] == local_template

    local_run = await workflow_tools.workflow_run_step(
        project_id=project_id,
        artifact_ref=saved["artifact_ref"],
        step_id="script",
        instance_id=materialized["instance_id"],
        inputs={"plot": "雨夜怀表"},
        persist_active=False,
    )

    assert local_run["ok"] is True
    assert local_run["created"] is False
    assert run_calls[-1] == {"node_id": script_node["id"], "action": "force", "prompt_template": local_template}
    base_after_local_patch = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, saved["artifact_ref"])
    assert base_after_local_patch["workflow"]["steps"][1]["prompt_template"] == original_template
    assert "当前实例强化模板" not in json.dumps(base_after_local_patch["workflow"], ensure_ascii=False)

    revision = await workflow_tools.workflow_spec_patch(
        project_id=project_id,
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "replace",
                "path": "/steps/script/prompt_template",
                "value": reusable_template,
            }
        ],
        sample_inputs={"plot": "雨夜怀表"},
    )

    assert revision["ok"] is True
    assert revision["artifact_ref"] != saved["artifact_ref"]
    patched_artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, revision["artifact_ref"])
    assert patched_artifact["workflow"]["steps"][1]["prompt_template"] == reusable_template
    assert patched_artifact["source"]["base_artifact_ref"] == saved["artifact_ref"]

    promoted = await workflow_tools.workflow_template_promote(
        project_id=project_id,
        artifact_ref=revision["artifact_ref"],
        template_id="plot_to_script_prompt_e2e",
        name="剧情到剧本提示词 E2E 模板",
        replace_existing=True,
        source_skill_name="e2e_prompt_skill",
        source_skill_summary="剧情生成剧本文本的提示词写法",
    )
    assert promoted["ok"] is True
    assert promoted["template_id"] == "plot_to_script_prompt_e2e"

    listed = await workflow_tools.workflow_list_templates(
        project_id=project_id,
        query="剧情到剧本提示词",
        category="user",
        limit=5,
    )
    assert listed["ok"] is True
    assert any(item["id"] == "plot_to_script_prompt_e2e" for item in listed["templates"])
    assert "prompt_template" not in json.dumps(listed["templates"], ensure_ascii=False)

    instantiated_count = len(nodes)
    instantiated = await workflow_tools.workflow_instantiate(
        project_id=project_id,
        template_id="plot_to_script_prompt_e2e",
        inputs={"plot": "晨雾信件"},
    )
    assert instantiated["ok"] is True
    assert instantiated["created_count"] == 2
    instantiated_nodes = nodes[instantiated_count:]
    instantiated_script = next(node for node in instantiated_nodes if node["input"]["workflow"]["step_id"] == "script")
    assert instantiated_script["input"]["workflow"]["prompt_template"] == reusable_template

    cloned = await workflow_tools.workflow_template_clone_to_artifact(
        project_id=project_id,
        template_id="plot_to_script_prompt_e2e",
    )
    assert cloned["ok"] is True
    cloned_artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, cloned["artifact_ref"])
    assert cloned_artifact["workflow"]["steps"][1]["prompt_template"] == reusable_template

    exported = await workflow_tools.workflow_template_export(
        project_id=project_id,
        template_id="plot_to_script_prompt_e2e",
    )
    assert exported["ok"] is True
    assert exported["filename"] == "plot_to_script_prompt_e2e.openreel-workflow-template.json"
    assert exported["package"]["workflow"]["steps"][1]["prompt_template"] == reusable_template


@pytest.mark.asyncio
async def test_workflow_template_save_current_uses_instance_prompt_template_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    project_id = "proj-save-current"
    template_root = tmp_path / "workflow_templates"
    monkeypatch.setattr(workflow_template_store, "workflow_template_library_root", lambda: template_root)

    original_template = "SYSTEM: 原始模板\nUSER: {{inputs.plot}}\nOUTPUT: text"
    current_template = "SYSTEM: 当前实例模板\nUSER: {{inputs.plot}}\nOUTPUT: text"
    workflow_template_store.save_user_template(
        workflow={
            "id": "current_story_flow",
            "name": "当前剧情流程",
            "inputs": [{"id": "plot", "label": "剧情"}],
            "required_inputs": ["plot"],
            "steps": [
                {"id": "brief", "title": "剧情输入", "node_type": "text"},
                {
                    "id": "script",
                    "title": "剧本文本",
                    "node_type": "text",
                    "runner": "node.run",
                    "depends_on": ["brief"],
                    "prompt_template": original_template,
                },
            ],
        },
        template_id="current_story_flow",
        name="当前剧情流程",
        replace_existing=True,
    )

    state = {
        "active_workflow": {"kind": "template", "template_id": "current_story_flow"},
        "workflow_input_values": {
            "by_workflow": {"current_story_flow": {"values": {"plot": "雨夜怀表"}}},
            "by_instance": {"wf_current": {"values": {"plot": "雨夜怀表"}}},
        },
        "workflow_runtime": {
            "instances": {
                "wf_current": {
                    "template_id": "current_story_flow",
                    "template_name": "当前剧情流程",
                    "steps": {
                        "script": {
                            "id": "workflow-runtime:wf_current:script",
                            "type": "text",
                            "title": "剧本文本",
                            "fields": {
                                "workflow": {
                                    "template_id": "current_story_flow",
                                    "instance_id": "wf_current",
                                    "step_id": "script",
                                    "prompt_template": current_template,
                                }
                            },
                            "status": "completed",
                        }
                    },
                }
            }
        },
    }

    async def fake_read_project_state(project_id_arg: str) -> dict[str, Any]:
        assert project_id_arg == project_id
        return state

    async def fake_list_nodes(project_id_arg: str) -> list[dict[str, Any]]:
        assert project_id_arg == project_id
        return [
            {
                "id": "script-1",
                "type": "text",
                "title": "剧本文本",
                "input": {
                    "workflow": {
                        "template_id": "current_story_flow",
                        "instance_id": "wf_current",
                        "step_id": "script",
                        "prompt_template": current_template,
                    }
                },
            }
        ]

    monkeypatch.setattr(workflow_tools, "_read_project_state", fake_read_project_state)
    monkeypatch.setattr(workflow_tools.canvas_tools, "list_nodes", fake_list_nodes)

    saved = await workflow_tools.workflow_template_save_current(
        project_id=project_id,
        template_id="current_story_flow_saved",
        name="当前剧情流程已保存",
        step_prompt_templates={"script": "SYSTEM: 可复用模板\nUSER: {{inputs.plot}}\nOUTPUT: text"},
        replace_existing=True,
    )

    assert saved["ok"] is True
    assert saved["template_id"] == "current_story_flow_saved"
    assert saved["base_template_id"] == "current_story_flow"
    assert saved["instance_id"] == "wf_current"
    assert saved["applied_overrides"] == [
        {"step_id": "script", "field": "prompt_template"},
        {"step_id": "script", "field": "prompt_template"},
    ]
    exported = await workflow_tools.workflow_template_export(
        project_id=project_id,
        template_id="current_story_flow_saved",
    )
    assert exported["ok"] is True
    workflow = exported["package"]["workflow"]
    assert workflow["steps"][1]["prompt_template"] == "SYSTEM: 可复用模板\nUSER: {{inputs.plot}}\nOUTPUT: text"
    assert original_template not in json.dumps(workflow, ensure_ascii=False)


@pytest.mark.asyncio
async def test_workflow_materialize_artifact_expands_segment_placeholders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "input": kwargs["input_data"],
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {f"node-{index}": str(index) for index in range(1, 4)}

    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)

    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "segment_character_workflow",
            "name": "分段人物图",
            "inputs": [
                {"id": "duration_seconds", "type": "number"},
                {"id": "segment_seconds", "type": "number", "default": 15},
            ],
            "steps": [
                {"id": "workflow_inputs", "title": "输入", "node_type": "text", "surface": "workflow_runtime"},
                {
                    "id": "split_segments",
                    "title": "切分剧情",
                    "node_type": "text",
                    "depends_on": ["workflow_inputs"],
                    "surface": "workflow_runtime",
                },
                {
                    "id": "segment_flow",
                    "title": "每段人物图",
                    "depends_on": ["split_segments"],
                    "foreach": {"from": "split_segments", "path": "segments"},
                    "steps": [
                        {
                            "id": "character_prompt",
                            "title": "人物提示词",
                            "node_type": "text",
                            "surface": "workflow_runtime",
                        },
                        {
                            "id": "character_image",
                            "title": "人物图",
                            "node_type": "image",
                            "depends_on": ["character_prompt"],
                        },
                    ],
                },
            ],
        },
    )

    result = await workflow_tools.workflow_materialize_artifact(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        inputs={"duration_seconds": 45, "segment_seconds": 15},
    )

    assert result["ok"] is True
    assert result["created_count"] == 3
    assert result["deferred_group_count"] == 0
    assert "已创建画布节点" in result["next_action"]
    assert [node["type"] for node in created_nodes] == ["image", "image", "image"]
    assert [node["title"] for node in created_nodes] == [
        "第1段 · 人物图",
        "第2段 · 人物图",
        "第3段 · 人物图",
    ]
    assert created_nodes[0]["input"]["workflow"]["instance_scope"]["placeholder"] is True
    runtime_steps = runtime_state["workflow_runtime"]["instances"][result["instance_id"]]["steps"]
    assert len(runtime_steps) == 8
    assert runtime_steps["segment_flow_s3_character_image"]["node_id"] == "node-3"


@pytest.mark.asyncio
async def test_workflow_spec_draft_commits_artifact_without_materializing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def fail_create_node(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("workflow.spec.commit must not create canvas nodes")

    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fail_create_node)

    start = await workflow_tools.workflow_spec_start(
        project_id="proj-1",
        workflow={"id": "video-short-drama", "name": "通用短剧"},
        inputs={
            "plot": {"type": "string", "label": "剧情", "required": True},
            "segmentCount": {"type": "number", "label": "段数", "default": 2},
        },
        sample_inputs={"plot": "雨夜怀表", "segmentCount": 2},
        expected_batches=["公共", "grid"],
    )
    draft_id = start["draft_id"]

    first = await workflow_tools.workflow_spec_append_steps(
        project_id="proj-1",
        draft_id=draft_id,
        batch_label="公共",
        steps=[
            {"id": "input", "title": "输入", "node_type": "text"},
            {
                "id": "script",
                "title": "剧本",
                "node_type": "text",
                "depends_on": ["input"],
                "prompt_template": "根据用户输入主题、风格和类型写完整剧本草稿。",
            },
        ],
    )
    second = await workflow_tools.workflow_spec_append_steps(
        project_id="proj-1",
        draft_id=draft_id,
        batch_label="grid",
        steps=[
            {"id": "storyboard", "title": "分镜图", "node_type": "image", "depends_on": ["script"]},
            {
                "id": "segmentFlow",
                "title": "每段流程",
                "depends_on": ["storyboard"],
                "repeat": {"segment_count": "segmentCount"},
                "steps": [
                    {"id": "videoPrompt", "title": "视频提示词", "node_type": "text"},
                ],
            },
        ],
    )
    commit = await workflow_tools.workflow_spec_commit(
        project_id="proj-1",
        draft_id=draft_id,
        self_check={"passed": True, "checks": ["依赖顺序正确"], "issues": []},
    )

    assert start["ok"] is True
    assert first["ok"] is True
    assert second["ok"] is True
    assert commit["ok"] is True
    assert commit["artifact_ref"].startswith("workflow_spec:")
    assert commit["validation"]["step_count"] == 5
    assert commit["validation"]["reusable"] is True
    assert commit["validation"]["protocol"]["workflow_spec_version"] == "openreel.workflow.v1"
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", commit["artifact_ref"])
    workflow_json = json.dumps(loaded["workflow"], ensure_ascii=False)
    assert "雨夜怀表" not in workflow_json
    assert loaded["sample_inputs"] == {"plot": "雨夜怀表", "segmentCount": 2}
    assert loaded["preview"]["workflow_spec_version"] == "openreel.workflow.v1"
    assert [item["id"] for item in loaded["workflow"]["inputs"]] == ["plot", "segmentCount"]
    assert [step["id"] for step in loaded["workflow"]["steps"]] == ["input", "script", "storyboard", "segmentFlow"]
    assert loaded["workflow"]["steps"][1]["prompt_template"] == "根据用户输入主题、风格和类型写完整剧本草稿。"


@pytest.mark.asyncio
async def test_workflow_spec_apply_patch_creates_artifact_without_materializing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def fail_create_node(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("workflow.spec.apply_patch must not create canvas nodes")

    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fail_create_node)

    result = await workflow_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="create",
        workflow={
            "id": "apply_patch_workflow",
            "name": "一次写入工作流",
            "inputs": [{"id": "plot", "type": "string", "label": "剧情", "required": True}],
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "depends_on": ["input"],
                    "prompt_template": "根据 {input.output} 写完整剧本。",
                },
            ],
        },
        sample_inputs={"plot": "雨夜怀表"},
        self_check={"passed": True, "checks": ["依赖顺序正确"], "issues": []},
    )

    assert result["ok"] is True
    assert result["artifact_ref"].startswith("workflow_spec:")
    assert result["validation"]["step_count"] == 2
    assert [field["id"] for field in result["input_fields"]] == ["plot"]

    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", result["artifact_ref"])
    assert loaded["workflow"]["id"] == "apply_patch_workflow"
    assert loaded["sample_inputs"] == {"plot": "雨夜怀表"}


@pytest.mark.asyncio
async def test_workflow_spec_apply_patch_updates_existing_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    created = await workflow_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="create",
        workflow={
            "id": "patchable_workflow",
            "name": "可修订工作流",
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"]},
            ],
        },
        self_check={"passed": True, "checks": ["初版可用"], "issues": []},
    )

    updated = await workflow_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="update",
        base={"artifact_ref": created["artifact_ref"]},
        operations=[
            {
                "op": "add_step",
                "after_id": "script",
                "step": {
                    "id": "storyboard",
                    "title": "分镜图",
                    "node_type": "image",
                    "depends_on": ["script"],
                },
            },
        ],
        self_check={"passed": True, "checks": ["新增分镜依赖剧本"], "issues": []},
    )

    assert updated["ok"] is True
    assert updated["artifact_ref"] != created["artifact_ref"]
    assert updated["validation"]["step_count"] == 3
    assert updated["applied"] == [{"ok": True, "op": "add_step", "step_id": "storyboard"}]
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", updated["artifact_ref"])
    assert [step["id"] for step in loaded["workflow"]["steps"]] == ["input", "script", "storyboard"]


@pytest.mark.asyncio
async def test_workflow_spec_append_steps_keeps_advisory_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    start = await workflow_tools.workflow_spec_start(
        project_id="proj-1",
        workflow={"id": "draft_spec", "name": "草稿"},
    )
    append = await workflow_tools.workflow_spec_append_steps(
        project_id="proj-1",
        draft_id=start["draft_id"],
        steps=[
            {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["missing_input"]},
        ],
    )

    assert append["ok"] is True
    assert append["validation"]["ok"] is False
    assert "missing_input" in append["validation"]["warning"]
    assert append["step_ids"] == ["script"]


@pytest.mark.asyncio
async def test_workflow_spec_commit_rejects_prefilled_node_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    start = await workflow_tools.workflow_spec_start(
        project_id="proj-1",
        workflow={"id": "video-short-drama", "name": "通用短剧"},
    )
    await workflow_tools.workflow_spec_append_steps(
        project_id="proj-1",
        draft_id=start["draft_id"],
        steps=[
            {
                "id": "script",
                "title": "剧本",
                "node_type": "text",
                "fields": {"content": "雨夜里两人重逢。"},
            },
        ],
    )

    result = await workflow_tools.workflow_spec_commit(
        project_id="proj-1",
        draft_id=start["draft_id"],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_framework_content_not_allowed"
    assert result["content_fields"] == ["script.fields.content"]


@pytest.mark.asyncio
async def test_workflow_spec_patch_creates_reusable_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "reusable": True,
            "inputs": [
                {"id": "plot", "type": "string"},
                {"id": "segmentCount", "type": "number", "default": 2},
            ],
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"], "prompt_ref": "script_writing"},
            ],
        },
        sample_inputs={"plot": "雨夜怀表", "segmentCount": 2},
    )

    read = await workflow_tools.workflow_spec_read(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        detail="workflow",
    )
    revision = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "merge_step",
                "step_id": "script",
                "patch": {"prompt_ref": "script_writing#strong_hook", "primary_skill": "script_writing"},
            }
        ],
    )

    assert read["ok"] is True
    assert read["workflow"]["steps"][1]["prompt_ref"] == "script_writing"
    assert revision["ok"] is True
    assert revision["artifact_ref"] != saved["artifact_ref"]
    assert revision["applied"] == [{"ok": True, "op": "merge_step", "step_id": "script"}]
    base = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", saved["artifact_ref"])
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", revision["artifact_ref"])
    assert base["workflow"]["steps"][1]["prompt_ref"] == "script_writing"
    assert patched["workflow"]["steps"][1]["prompt_ref"] == "script_writing#strong_hook"
    assert patched["source"]["base_artifact_ref"] == saved["artifact_ref"]
    assert patched["reusable"] is True
    assert "雨夜怀表" not in json.dumps(patched["workflow"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_workflow_spec_patch_ignores_non_object_optional_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "reusable": True,
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text", "prompt_ref": "script_writing"},
            ],
        },
    )

    revision = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "merge_step",
                "step_id": "script",
                "patch": {"prompt_ref": "script_writing#style"},
            }
        ],
        user_preview="不是对象",  # type: ignore[arg-type]
        self_check=["不是对象"],  # type: ignore[arg-type]
    )

    assert revision["ok"] is True
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", revision["artifact_ref"])
    assert patched["workflow"]["steps"][0]["prompt_ref"] == "script_writing#style"
    assert patched["self_check"]["passed"] is True


@pytest.mark.asyncio
async def test_workflow_spec_patch_rejects_prefilled_prompt_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "script", "title": "剧本", "node_type": "text"},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "merge_step",
                "step_id": "script",
                "patch": {"fields": {"prompt": "请写一段雨夜重逢剧本。"}},
            }
        ],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_framework_content_not_allowed"
    assert result["content_fields"] == ["script.fields.prompt"]


def test_workflow_spec_artifact_preview_keeps_structural_truth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "inputs": {"plot": {"type": "string"}},
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "depends_on": ["input"],
                    "prompt_template": "根据输入主题写剧本。",
                    "prompt_ref": "script_writing",
                },
            ],
        },
        user_preview={
            "title": "用户可读标题",
            "step_count": 99,
            "input_ids": ["fake"],
            "first_steps": [{"id": "fake", "title": "不存在", "node_type": "text"}],
        },
    )

    assert saved["preview"]["title"] == "用户可读标题"
    assert saved["preview"]["step_count"] == 2
    assert saved["preview"]["input_ids"] == ["plot"]
    assert [step["id"] for step in saved["preview"]["first_steps"]] == ["input", "script"]
    assert saved["preview"]["first_steps"][1]["prompt_template"] == "根据输入主题写剧本。"
    assert saved["preview"]["first_steps"][1]["prompt_ref"] == "script_writing"


def test_normalize_inline_workflow_rejects_self_dependency() -> None:
    with pytest.raises(canvas_workflow_templates.WorkflowTemplateError, match="cannot depend on itself"):
        canvas_workflow_templates.normalize_inline_workflow(
            {
                "id": "video-short-drama",
                "name": "通用短剧",
                "steps": [
                    {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["script"]},
                ],
            }
        )


def test_authoring_workflow_spec_compiles_to_runtime_contract() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "schema": "openreel.workflow.authoring.v1",
            "id": "grid_storyboard_authoring",
            "title": "宫格分镜作者层流程",
            "inputs": {
                "plot": {"type": "long_text", "label": "剧情", "required": True},
                "segmentCount": {"type": "number", "label": "段数", "default": 2},
            },
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "kind": "text",
                    "output": {"canvas": True, "key": "script"},
                    "prompt": {
                        "role": "短剧编剧",
                        "task": "根据 plot={{inputs.plot}} 写分段剧本。",
                        "output": "分段剧本。",
                        "check": "每段有动作、人物和场景。",
                    },
                },
                {
                    "id": "segment_plan",
                    "title": "分段规划",
                    "kind": "plan",
                    "needs": ["script"],
                    "prompt": {
                        "role": "执行规划助手",
                        "task": "从 script={{script.output}} 提取 segments。",
                    },
                    "output": {"key": "segments"},
                },
                {
                    "id": "storyboard",
                    "title": "宫格分镜",
                    "kind": "image",
                    "needs": ["segment_plan"],
                    "for_each": "segment_plan.output.segments",
                    "item_name": "segment",
                    "references": {
                        "characters": {
                            "source": "segment_plan.output.appearing_characters",
                            "candidates": "character_image",
                        }
                    },
                    "prompt": {
                        "role": "分镜导演",
                        "task": "根据当前 segment 写宫格分镜图提示词。",
                        "output": "几宫格、视觉风格和每格构图。",
                    },
                    "output": {"canvas": True, "key": "storyboards"},
                },
                {
                    "id": "video_prompt",
                    "title": "视频提示词",
                    "kind": "text",
                    "needs": ["storyboard"],
                    "for_each": "segment_plan.output.segments",
                    "item_name": "segment",
                    "prompt": {
                        "role": "视频提示词导演",
                        "task": "根据 storyboard={{storyboard.output}} 写视频提示词。",
                        "output": "单段视频提示词。",
                    },
                    "output": {"canvas": False, "key": "video_prompts"},
                },
            ],
        },
        input_values={
            "segment_plan": {
                "output": {
                    "segments": [
                        {"index": 1, "summary": "开场"},
                        {"index": 2, "summary": "反转"},
                    ]
                }
            }
        },
    )

    assert template["workflow_spec_version"] == "openreel.workflow.v1"
    assert template["authoring_spec_version"] == "openreel.workflow.authoring.v1"
    assert template["required_inputs"] == ["plot"]
    by_id = {step["id"]: step for step in template["steps"]}
    script = by_id["script"]
    plan = by_id["segment_plan"]
    storyboard_prompt = by_id["segments_s1_storyboard_prompt"]
    storyboard = by_id["segments_s1_storyboard"]
    video_prompt = by_id["segments_s1_video_prompt"]
    assert script["node_type"] == "text"
    assert script["runner"] == "node.run"
    assert script["surface"] == "workflow_runtime"
    assert "SYSTEM:" in script["prompt_template"]
    assert plan["surface"] == "workflow_runtime"
    assert plan["output_mode"] == "json"
    assert storyboard_prompt["surface"] == "workflow_runtime"
    assert storyboard_prompt["template_step_id"] == "storyboard_prompt"
    assert storyboard["template_step_id"] == "storyboard"
    assert storyboard["repeat_group_id"] == "segments"
    assert storyboard["kind"] == "image"
    assert storyboard["runner"] == "workflow_canvas_output"
    assert storyboard["surface"] == "draft_canvas"
    assert storyboard["fields"]["workflow_source_step"] == "storyboard_prompt"
    assert storyboard["reference_selectors"][0]["from_step"] == "character_image"
    assert video_prompt["template_step_id"] == "video_prompt"
    assert video_prompt["surface"] == "workflow_runtime"


def test_runtime_workflow_rejects_authoring_only_dependency_fields_without_schema() -> None:
    with pytest.raises(canvas_workflow_templates.WorkflowTemplateError, match="authoring-only"):
        canvas_workflow_templates.normalize_inline_workflow(
            {
                "id": "half_authoring_workflow",
                "name": "半作者层流程",
                "workflow_spec_version": "openreel.workflow.v1",
                "steps": [
                    {"id": "plan", "title": "规划", "node_type": "text"},
                    {
                        "id": "segment_story",
                        "title": "分段剧情",
                        "node_type": "text",
                        "needs": ["plan"],
                        "for_each": "plan.output.segments",
                    },
                ],
            }
        )


def test_authoring_loop_preserves_foreach_when_repeat_also_has_display_metadata() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "schema": "openreel.workflow.authoring.v1",
            "id": "repeat_editor_roundtrip",
            "title": "编辑器循环往返",
            "steps": [
                {
                    "id": "main_characters",
                    "title": "主要人物",
                    "kind": "collection",
                    "output": {"canvas": False},
                },
                {
                    "id": "main_character_images",
                    "title": "主要人物参考图",
                    "kind": "loop",
                    "needs": ["main_characters"],
                    "repeat": {
                        "label": "按主要人物逐个展开",
                        "mode": "per_main_character",
                    },
                    "foreach": {
                        "from_step": "main_characters",
                        "path": "output.main_characters",
                        "kind": "characters",
                    },
                    "steps": [
                        {
                            "id": "main_character_image",
                            "title": "主要人物参考图",
                            "kind": "image",
                            "output": {"canvas": True},
                        }
                    ],
                },
            ],
        }
    )

    group = template["deferred_groups"][0]
    assert group["id"] == "main_character_images"
    assert group["repeat"]["label"] == "按主要人物逐个展开"
    assert group["repeat"]["mode"] == "per_main_character"
    assert group["repeat"]["foreach"] == {
        "from_step": "main_characters",
        "path": "output.main_characters",
        "kind": "characters",
    }


def test_authoring_workflow_repeat_suffix_prefers_stable_index_over_scene_text() -> None:
    template = canvas_workflow_templates.normalize_inline_workflow(
        {
            "schema": "openreel.workflow.authoring.v1",
            "id": "scene_suffix_authoring",
            "title": "中文场景动态展开",
            "steps": [
                {"id": "script", "title": "剧本", "kind": "text", "output": {"canvas": True}},
                {"id": "production_plan", "title": "规划", "kind": "plan", "needs": ["script"], "output": {"canvas": False}},
                {
                    "id": "storyboard",
                    "title": "分镜",
                    "kind": "image",
                    "needs": ["production_plan"],
                    "for_each": "production_plan.output.segments",
                    "item_name": "segment",
                    "output": {"canvas": True},
                },
                {
                    "id": "video_prompt",
                    "title": "视频提示词",
                    "kind": "text",
                    "needs": ["storyboard"],
                    "for_each": "production_plan.output.segments",
                    "item_name": "segment",
                    "output": {"canvas": False},
                },
            ],
        },
        input_values={
            "production_plan": {
                "output": {
                    "segments": [
                        {"index": 1, "summary": "开场", "scene": "雨夜巷口"},
                        {"index": 2, "summary": "反转", "scene": "天台"},
                    ]
                }
            }
        },
    )

    steps = template["steps"]
    ids = [step["id"] for step in steps]
    assert len(ids) == len(set(ids))
    storyboard_ids = [step["id"] for step in steps if step.get("template_step_id") == "storyboard"]
    video_prompt_steps = [step for step in steps if step.get("template_step_id") == "video_prompt"]
    assert len(storyboard_ids) == 2
    assert len(video_prompt_steps) == 2
    assert video_prompt_steps[0]["depends_on"] == ["production_plan", storyboard_ids[0]]
    assert video_prompt_steps[1]["depends_on"] == ["production_plan", storyboard_ids[1]]


@pytest.mark.asyncio
async def test_workflow_spec_commit_accepts_authoring_steps_without_runtime_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    start = await workflow_tools.workflow_spec_start(
        project_id="proj-1",
        workflow={
            "schema": "openreel.workflow.authoring.v1",
            "id": "authoring_short_drama",
            "title": "作者层短剧流程",
        },
        inputs={
            "plot": {"type": "long_text", "label": "剧情", "required": True},
            "segmentCount": {"type": "number", "label": "段数", "default": 1},
        },
        sample_inputs={"plot": "雨夜怀表", "segmentCount": 1},
    )
    await workflow_tools.workflow_spec_append_steps(
        project_id="proj-1",
        draft_id=start["draft_id"],
        steps=[
            {
                "id": "script",
                "title": "剧本",
                "kind": "text",
                "output": {"canvas": True, "key": "script"},
                "prompt": {
                    "role": "短剧编剧",
                    "task": "根据 plot={{inputs.plot}} 写剧本。",
                    "output": "完整剧本框架。",
                },
            },
            {
                "id": "storyboard",
                "title": "宫格分镜",
                "kind": "image",
                "needs": ["script"],
                "prompt": {
                    "role": "分镜导演",
                    "task": "根据 script={{script.output}} 写宫格分镜图提示词。",
                    "output": "几宫格、视觉风格和每格构图。",
                },
                "output": {"canvas": True, "key": "storyboard"},
            },
        ],
    )

    commit = await workflow_tools.workflow_spec_commit(
        project_id="proj-1",
        draft_id=start["draft_id"],
        self_check={"passed": True, "checks": ["作者层字段完整"], "issues": []},
    )

    assert commit["ok"] is True
    assert commit["validation"]["step_count"] == 3
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", commit["artifact_ref"])
    assert loaded["workflow"]["schema"] == "openreel.workflow.authoring.v1"
    assert "runner" not in json.dumps(loaded["workflow"], ensure_ascii=False)
    assert loaded["preview"]["first_steps"][0]["node_type"] == "text"
    assert loaded["preview"]["first_steps"][0]["surface"] == "workflow_runtime"
    assert "SYSTEM:" in loaded["preview"]["first_steps"][0]["prompt_template"]
    assert loaded["preview"]["first_steps"][1]["id"] == "storyboard_prompt"
    assert loaded["preview"]["first_steps"][2]["node_type"] == "image"
    assert loaded["preview"]["first_steps"][2]["surface"] == "draft_canvas"


@pytest.mark.asyncio
async def test_workflow_spec_commit_rejects_authoring_only_fields_without_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    start = await workflow_tools.workflow_spec_start(
        project_id="proj-1",
        workflow={
            "id": "half_authoring_commit",
            "workflow_spec_version": "openreel.workflow.v1",
        },
        inputs={"topic": {"type": "text", "required": True}},
        sample_inputs={"topic": "雨夜电台"},
    )
    await workflow_tools.workflow_spec_append_steps(
        project_id="proj-1",
        draft_id=start["draft_id"],
        steps=[
            {"id": "plan", "title": "规划", "node_type": "text"},
            {
                "id": "segment_story",
                "title": "分段剧情",
                "node_type": "text",
                "needs": ["plan"],
                "for_each": "plan.output.segments",
            },
        ],
    )

    commit = await workflow_tools.workflow_spec_commit(
        project_id="proj-1",
        draft_id=start["draft_id"],
        self_check={"passed": True, "checks": ["模型误判通过"], "issues": []},
    )

    assert commit["ok"] is False
    assert commit["error_kind"] == "workflow_spec_error"
    assert "authoring-only" in commit["error"]
    assert not list(tmp_path.glob("proj-1/workflow_specs/*.json"))


@pytest.mark.asyncio
async def test_workflow_spec_apply_patch_returns_repair_ref_for_invalid_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    result = await workflow_spec_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="create",
        workflow={
            "id": "bad_content_workflow",
            "name": "错误内容流程",
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "fields": {"content": "这里已经是运行产物正文"},
                },
            ],
        },
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_framework_content_not_allowed"
    assert result["repair_ref"].startswith("workflow_repair:")
    assert result["suggested_strategy"] == "update"
    assert result["issues"][0]["severity"] == "blocking"
    assert not list(tmp_path.glob("proj-1/workflow_specs/*.json"))
    assert list(tmp_path.glob("proj-1/workflow_repairs/*.json"))


@pytest.mark.asyncio
async def test_workflow_spec_apply_patch_accepts_authoring_aliases_and_inspects_canvas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    result = await workflow_spec_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="create",
        workflow={
            "schema": "openreel.workflow.authoring.v1",
            "id": "alias_video_workflow",
            "name": "别名视频工作流",
            "inputs": [
                {"id": "plot", "label": "剧情主题", "type": "long_text", "required": True},
                {"id": "durationSeconds", "label": "总时长", "type": "number", "default": 30},
                {"id": "segmentSeconds", "label": "每段秒数", "type": "number", "default": 15},
            ],
            "steps": [
                {
                    "id": "script",
                    "title": "完整剧本",
                    "type": "text",
                    "visible": True,
                    "prompt_template": "根据 {{inputs.plot}} 写完整剧本。",
                },
                {
                    "id": "segments",
                    "title": "分段清单",
                    "type": "list",
                    "depends_on": ["script"],
                    "prompt_template": "按总时长和每段秒数拆分剧本。",
                    "schema": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "segment_text": {"type": "string"},
                                "duration": {"type": "number"},
                            },
                            "required": ["segment_text"],
                        },
                    },
                },
                {
                    "id": "segment_loop",
                    "title": "逐段生成",
                    "type": "repeat",
                    "depends_on": ["segments"],
                    "repeat": {"items": "{{steps.segments.output}}", "item_name": "segment"},
                    "steps": [
                        {
                            "id": "segment_text",
                            "title": "本段剧情",
                            "type": "text",
                            "visible": True,
                            "prompt_template": "只输出 {{segment.segment_text}}。",
                        },
                        {
                            "id": "segment_video",
                            "title": "本段视频",
                            "type": "video",
                            "depends_on": ["segment_text"],
                            "prompt_template": "根据 {{segment.segment_text}} 生成视频提示词。",
                            "fields": {"duration_seconds": "{{segment.duration_seconds}}"},
                        },
                    ],
                },
            ],
        },
        sample_inputs={"plot": "雨夜电台"},
    )

    assert result["ok"] is True
    assert result["artifact_ref"].startswith("workflow_spec:")
    assert result["suggested_next"] == "call_workflow_canvas_inspect"

    inspected = await workflow_spec_tools.workflow_canvas_inspect(
        project_id="proj-1",
        artifact_ref=result["artifact_ref"],
        inputs={"plot": "雨夜电台", "durationSeconds": 30, "segmentSeconds": 15},
    )

    assert inspected["ok"] is True
    assert inspected["validation"]["ok"] is True
    assert inspected["workflow"]["canvas_node_count"] >= 5
    assert inspected["validation"]["dry_run"]["repeat_instance_count"] == 2
    assert [node["type"] for node in inspected["canvas"]["final_outputs"]] == ["video", "video"]


@pytest.mark.asyncio
async def test_workflow_spec_apply_patch_updates_from_repair_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    failed = await workflow_spec_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="create",
        workflow={
            "id": "repair_update_workflow",
            "name": "修复候选",
            "steps": [
                {
                    "id": "script",
                    "title": "剧本",
                    "node_type": "text",
                    "fields": {"content": "运行时正文"},
                },
            ],
        },
    )

    fixed = await workflow_spec_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="update",
        base={"repair_ref": failed["repair_ref"]},
        operations=[
            {"op": "replace", "path": "/steps/script/fields/content", "value": ""},
        ],
    )

    assert fixed["ok"] is True
    assert fixed["artifact_ref"].startswith("workflow_spec:")
    assert fixed["suggested_next"] == "call_workflow_canvas_inspect"
    assert "workflow.canvas.inspect" in fixed["next_action"]
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", fixed["artifact_ref"])
    assert loaded["source"]["base_repair_ref"] == failed["repair_ref"]
    assert loaded["workflow"]["steps"][0]["fields"]["content"] == ""


@pytest.mark.asyncio
async def test_workflow_spec_apply_patch_replaces_from_repair_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    failed = await workflow_spec_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="create",
        workflow={"id": "empty_repair_workflow", "name": "空流程", "steps": []},
    )

    fixed = await workflow_spec_tools.workflow_spec_apply_patch(
        project_id="proj-1",
        operation="replace",
        base={"repair_ref": failed["repair_ref"]},
        workflow={
            "id": "empty_repair_workflow",
            "name": "空流程修复",
            "steps": [
                {"id": "brief", "title": "需求", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["brief"]},
            ],
        },
    )

    assert failed["ok"] is False
    assert failed["repair_ref"].startswith("workflow_repair:")
    assert fixed["ok"] is True
    assert fixed["preview"]["step_count"] == 2
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", fixed["artifact_ref"])
    assert loaded["source"]["base_repair_ref"] == failed["repair_ref"]
    assert loaded["source"]["operation"] == "replace"


@pytest.mark.asyncio
async def test_workflow_spec_patch_rejects_noop_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [{"id": "input", "title": "输入", "node_type": "text"}],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[{"op": "merge_step", "step_id": "missing", "patch": {"title": "不存在"}}],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_patch_noop"
    assert result["applied"] == [{"ok": False, "op": "merge_step", "step_id": "missing", "error": "step_not_found"}]


@pytest.mark.asyncio
async def test_workflow_spec_patch_accepts_json_patch_step_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"], "primary_skill": "script_writing"},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {"op": "replace", "path": "/steps/script/primary_skill", "value": "script_writing#strong_hook"},
        ],
    )

    assert result["ok"] is True
    assert result["applied"] == [
        {"ok": True, "op": "path_patch", "path": "/steps/script/primary_skill", "step_id": "script"}
    ]
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", result["artifact_ref"])
    assert patched["workflow"]["steps"][1]["primary_skill"] == "script_writing#strong_hook"


@pytest.mark.asyncio
async def test_workflow_spec_patch_rejects_unchanged_json_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"], "primary_skill": "script_writing"},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {"op": "replace", "path": "/steps/script/primary_skill", "value": "script_writing"},
        ],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_patch_noop"
    assert result["applied"] == [
        {"ok": False, "op": "path_patch", "path": "/steps/script/primary_skill", "step_id": "script", "error": "unchanged"}
    ]


@pytest.mark.asyncio
async def test_workflow_spec_patch_insert_between_rewires_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"]},
                {"id": "planCharactersScenes", "title": "人物场景规划", "node_type": "text", "depends_on": ["script"]},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "insert_between",
                "after_id": "script",
                "before_id": "planCharactersScenes",
                "step": {
                    "id": "hookReview",
                    "title": "钩子检查",
                    "node_type": "text",
                    "depends_on": ["script"],
                    "primary_skill": "hook_punch_review",
                    "runner": "node.run",
                },
            }
        ],
    )

    assert result["ok"] is True
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", result["artifact_ref"])
    steps = patched["workflow"]["steps"]
    assert [step["id"] for step in steps] == ["input", "script", "hookReview", "planCharactersScenes"]
    assert steps[3]["depends_on"] == ["hookReview"]


@pytest.mark.asyncio
async def test_workflow_spec_patch_insert_between_requires_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "input", "title": "输入", "node_type": "text"},
                {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"]},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "insert_between",
                "step": {
                    "id": "hookReview",
                    "title": "钩子检查",
                    "node_type": "text",
                },
            }
        ],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_patch_noop"
    assert result["applied"] == [
        {"ok": False, "op": "insert_between", "step_id": "hookReview", "error": "after_id_and_before_id_required"}
    ]


@pytest.mark.asyncio
async def test_workflow_spec_patch_accepts_model_friendly_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "write_script", "title": "写剧本", "node_type": "text"},
                {"id": "plan_characters_scenes", "title": "人物场景规划", "node_type": "text", "depends_on": ["write_script"]},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "add_step",
                "after_step_id": "write_script",
                "step": {
                    "id": "hook_review",
                    "title": "钩子检查",
                    "node_type": "text",
                    "depends_on": ["write_script"],
                    "runner": "node.run",
                    "primary_skill": "hook_review",
                },
            },
            {
                "op": "update_step",
                "step_id": "plan_characters_scenes",
                "fields": {"depends_on": ["hook_review"]},
            },
        ],
    )

    assert result["ok"] is True
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", result["artifact_ref"])
    steps = patched["workflow"]["steps"]
    assert [step["id"] for step in steps] == ["write_script", "hook_review", "plan_characters_scenes"]
    assert steps[2]["depends_on"] == ["hook_review"]


@pytest.mark.asyncio
async def test_workflow_spec_patch_accepts_bracket_paths_and_anchor_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "write_script", "title": "写剧本", "node_type": "text"},
                {"id": "plan_characters_scenes", "title": "人物场景规划", "node_type": "text", "depends_on": ["write_script"]},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "add_step",
                "path": "steps",
                "position": "after",
                "anchor_step_id": "write_script",
                "step": {
                    "id": "hook_review",
                    "title": "钩子检查",
                    "node_type": "text",
                    "depends_on": ["write_script"],
                },
            },
            {
                "op": "replace",
                "path": "steps[plan_characters_scenes].depends_on",
                "value": ["hook_review"],
            },
        ],
    )

    assert result["ok"] is True
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", result["artifact_ref"])
    steps = patched["workflow"]["steps"]
    assert [step["id"] for step in steps] == ["write_script", "hook_review", "plan_characters_scenes"]
    assert steps[2]["depends_on"] == ["hook_review"]


@pytest.mark.asyncio
async def test_workflow_spec_patch_can_replace_steps_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "write_script", "title": "写剧本", "node_type": "text"},
                {"id": "plan_characters_scenes", "title": "人物场景规划", "node_type": "text", "depends_on": ["write_script"]},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "replace",
                "path": "steps",
                "value": [
                    {"id": "write_script", "title": "写剧本", "node_type": "text"},
                    {"id": "hook_review", "title": "钩子检查", "node_type": "text", "depends_on": ["write_script"]},
                    {"id": "plan_characters_scenes", "title": "人物场景规划", "node_type": "text", "depends_on": ["hook_review"]},
                ],
            }
        ],
    )

    assert result["ok"] is True
    patched = workflow_spec_artifacts.load_workflow_spec_artifact("proj-1", result["artifact_ref"])
    assert [step["id"] for step in patched["workflow"]["steps"]] == [
        "write_script",
        "hook_review",
        "plan_characters_scenes",
    ]


@pytest.mark.asyncio
async def test_workflow_spec_patch_rejects_index_patch_self_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "write_script", "title": "写剧本", "node_type": "text"},
                {"id": "plan_characters_scenes", "title": "人物场景规划", "node_type": "text", "depends_on": ["write_script"]},
            ],
        },
    )

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "add_step",
                "after_step_id": "write_script",
                "step": {
                    "id": "hook_review",
                    "title": "钩子检查",
                    "node_type": "text",
                    "depends_on": ["write_script"],
                },
            },
            {
                "op": "replace",
                "path": "steps[1].depends_on",
                "value": ["hook_review"],
            },
        ],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_spec_error"
    assert "cannot depend on itself" in result["error"]


@pytest.mark.asyncio
async def test_workflow_spec_patch_does_not_save_partial_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="proj-1",
        workflow={
            "id": "video-short-drama",
            "name": "通用短剧",
            "steps": [
                {"id": "write_script", "title": "写剧本", "node_type": "text"},
            ],
        },
    )
    artifact_dir = tmp_path / "proj-1" / "workflow_specs"
    before_files = {path.name for path in artifact_dir.glob("*.json")}

    result = await workflow_tools.workflow_spec_patch(
        project_id="proj-1",
        artifact_ref=saved["artifact_ref"],
        operations=[
            {
                "op": "add_step",
                "after_step_id": "write_script",
                "step": {"id": "hook_review", "title": "钩子检查", "node_type": "text", "depends_on": ["write_script"]},
            },
            {
                "op": "update_step",
                "step_id": "missing_step",
                "fields": {"depends_on": ["hook_review"]},
            },
        ],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_patch_failed"
    assert {path.name for path in artifact_dir.glob("*.json")} == before_files


@pytest.mark.asyncio
async def test_workflow_draft_appends_batches_then_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    created_nodes: list[dict[str, Any]] = []
    created_edges: list[dict[str, Any]] = []

    async def fake_create_node(**kwargs: Any) -> dict[str, Any]:
        index = len(created_nodes) + 1
        node = {
            "id": f"node-{index}",
            "display_id": index,
            "type": kwargs["node_type"],
            "title": kwargs["title"],
            "status": "idle",
            "position": {"x": kwargs["position_x"], "y": kwargs["position_y"]},
            "prompt": kwargs.get("prompt"),
        }
        created_nodes.append(node)
        return dict(node)

    async def fake_connect_nodes(**kwargs: Any) -> dict[str, Any]:
        edge = {
            "id": f"edge-{len(created_edges) + 1}",
            "source_node_id": kwargs["source_node_id"],
            "target_node_id": kwargs["target_node_id"],
        }
        created_edges.append(edge)
        return edge

    async def fake_emit(project_id: str, action: str, payload: dict[str, Any]) -> None:
        return None

    class FakeSessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    async def fake_public_map(session: object, project_id: str) -> dict[str, str]:
        return {"node-1": "1", "node-2": "2", "node-3": "3"}

    monkeypatch.setattr(workflow_tools.canvas_tools, "create_node", fake_create_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "connect_nodes", fake_connect_nodes)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", fake_emit)
    monkeypatch.setattr(workflow_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(workflow_tools, "internal_to_public_id_map", fake_public_map)
    runtime_state = install_fake_workflow_runtime_state(monkeypatch)

    start = await workflow_tools.workflow_draft_start(
        project_id="project-1",
        title="分批工作流",
        workflow={"id": "video-short-drama", "name": "通用短剧"},
        inputs={"plot": "江湖相逢"},
        expected_batches=["公共", "grid"],
    )
    draft_id = start["draft_id"]

    first = await workflow_tools.workflow_draft_append_steps(
        project_id="project-1",
        draft_id=draft_id,
        batch_label="公共",
        steps=[
            {"id": "input", "title": "输入", "node_type": "text", "fields": {"content": "填写需求"}},
            {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["input"]},
        ],
    )
    second = await workflow_tools.workflow_draft_append_steps(
        project_id="project-1",
        draft_id=draft_id,
        batch_label="grid",
        steps=[
            {
                "id": "gridVideoPrompt",
                "title": "视频提示词",
                "node_type": "text",
                "depends_on": ["script"],
                "source_node_id": "videoPrompt",
            },
        ],
    )
    commit = await workflow_tools.workflow_draft_commit(
        project_id="project-1",
        draft_id=draft_id,
    )

    assert start["ok"] is True
    assert first["ok"] is True
    assert first["step_count"] == 2
    assert second["ok"] is True
    assert second["step_ids"] == ["input", "script", "grid_video_prompt"]
    assert commit["ok"] is True
    assert commit["created_count"] == 2
    assert commit["edges_count"] == 1
    assert len(runtime_state["workflow_runtime"]["instances"][commit["instance_id"]]["steps"]) == 2
    assert commit["draft_committed"] is True
    assert created_edges == [
        {"id": "edge-1", "source_node_id": "node-1", "target_node_id": "node-2"},
    ]

    missing = await workflow_tools.workflow_draft_commit(project_id="project-1", draft_id=draft_id)
    assert missing["ok"] is False
    assert missing["error_kind"] == "workflow_draft_not_found"
