from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.agent.workflow_spec import (
    WORKFLOW_PLAN_VERSION,
    WORKFLOW_SPEC_VERSION,
    WorkflowSpecError,
    compile_workflow_spec,
    parse_workflow_spec,
    workflow_spec_payload,
)
from app.agent import canvas_workflow_templates
from app.agent.workflow_execution_plan import compile_private_execution_template
from app.agent.workflow_audit import audit_workflow_spec
from app.agent.workflow_spec_prompt_contract import WORKFLOW_SPEC_V2_GUIDE
from app.agent import workflow_template_store
from app.agent import workflow_canvas_projection
from app.config import settings
from app.mcp_tools import node_universal, workflow_tools
from app.mcp_tools.registry import registry


def _base_spec() -> dict:
    return {
        "schema": WORKFLOW_SPEC_VERSION,
        "id": "video_flow",
        "title": "视频流程",
        "description": "从剧情生成剧本、人物图和视频。",
        "tags": ["video"],
        "inputs": {
            "plot": {"type": "long_text", "label": "剧情", "required": True},
            "episode_count": {"type": "integer", "label": "集数", "default": 1, "min": 1},
        },
        "steps": [
            {
                "id": "script",
                "title": "剧本",
                "kind": "text",
                "prompt": {"task": "根据 {{ inputs.plot }} 写剧本。"},
                "output": {"canvas": True},
            }
        ],
    }


def test_v2_is_the_only_accepted_public_schema() -> None:
    parsed = parse_workflow_spec(_base_spec())
    assert parsed.schema_ == WORKFLOW_SPEC_VERSION

    legacy = _base_spec()
    legacy["schema"] = "openreel.workflow.authoring.v1"
    with pytest.raises(WorkflowSpecError):
        parse_workflow_spec(legacy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_type", "text"),
        ("runner", "node.run"),
        ("surface", "workflow_runtime"),
        ("visibility", "flow_only"),
        ("prompt_template", "legacy"),
        ("llm_task_type", "script_generation"),
        ("runtime_hidden", True),
        ("manual_only", True),
        ("optional", True),
        ("auto_skip_when", "{{ inputs.episode_count }} <= 1"),
        ("context_refs", []),
        ("reference_selectors", []),
        ("repeat", {"count": 2}),
        ("for_each", "steps.script.output"),
        ("bindings", {}),
        ("prompt_spec", {}),
        ("expansion", {}),
        ("completion", {}),
        ("io", {}),
        ("branch", "legacy"),
        ("expand_when", "legacy"),
        ("instance_scope", {}),
        ("repeat_group_id", "legacy"),
        ("source_category", "legacy"),
        ("source_ui", "legacy"),
        ("source_behavior", "legacy"),
    ],
)
def test_v2_rejects_deleted_step_fields(field: str, value: object) -> None:
    payload = _base_spec()
    payload["steps"][0][field] = value
    with pytest.raises(WorkflowSpecError):
        parse_workflow_spec(payload)


def test_v2_rejects_deleted_root_fields() -> None:
    for field in ("name", "inputs_schema", "required_inputs", "defaults", "dimensions", "required_capabilities"):
        payload = _base_spec()
        payload[field] = {}
        with pytest.raises(WorkflowSpecError):
            parse_workflow_spec(payload)


def test_legacy_workflow_authoring_tools_are_not_registered() -> None:
    names = {tool.name for tool in registry.list_tools()}
    assert {
        "workflow.draft.start",
        "workflow.draft.append_steps",
        "workflow.draft.commit",
        "workflow.spec.start",
        "workflow.spec.append_steps",
        "workflow.spec.commit",
        "workflow.spec.patch",
    }.isdisjoint(names)


@pytest.mark.parametrize("field", ["model", "model_tier", "provider", "llm_task_type", "api_key"])
def test_v2_rejects_provider_and_model_routing_even_inside_fields(field: str) -> None:
    payload = _base_spec()
    payload["steps"][0]["fields"] = {field: "configured-elsewhere"}
    with pytest.raises(WorkflowSpecError, match="provider/model routing"):
        parse_workflow_spec(payload)


def test_v2_compiler_is_deterministic_and_has_no_provider_routing() -> None:
    first = compile_workflow_spec(_base_spec())
    second = compile_workflow_spec(deepcopy(_base_spec()))

    assert first == second
    assert first["schema"] == WORKFLOW_PLAN_VERSION
    assert len(first["plan_hash"]) == 64
    assert first["requirements"] == {
        "llm": True,
        "vision": False,
        "media": [],
        "plugins": [],
    }
    assert "llm_task_type" not in str(first)
    assert "provider" not in str(first)


def test_v2_derives_dependencies_from_prompt_media_and_loop_paths() -> None:
    payload = _base_spec()
    payload["inputs"]["aspect_ratio"] = {"type": "text", "label": "比例", "default": "16:9"}
    payload["steps"].extend(
        [
            {
                "id": "characters",
                "title": "人物集合",
                "kind": "collection",
                "prompt": {"task": "从 {{ steps.script.output }} 提取人物。"},
                "output": {
                    "schema": {
                        "fields": [
                            {"id": "name", "type": "string", "required": True},
                            {"id": "reuse_key", "type": "string", "required": True},
                        ]
                    }
                },
            },
            {
                "id": "character_images",
                "title": "逐个人物出图",
                "kind": "loop",
                "foreach": {"items": "steps.characters.output", "as": "character"},
                "steps": [
                    {
                        "id": "character_image",
                        "title": "人物参考图",
                        "kind": "image",
                        "prompt": {"task": "为 {{ character.name }} 生成人物图。"},
                        "fields": {"aspect_ratio": "{{ inputs.aspect_ratio }}"},
                    }
                ],
            },
            {
                "id": "final_video",
                "title": "最终视频",
                "kind": "video",
                "prompt": {"task": "根据 {{ steps.script.output }} 写视频提示词。"},
                "uses": [
                    {
                        "from": "character_image",
                        "as": ["vision", "reference"],
                        "select": {
                            "values": "steps.characters.output.reuse_key",
                            "by": ["reuse_key"],
                        },
                    }
                ],
            },
        ]
    )

    plan = compile_workflow_spec(payload)
    by_id = {step["id"]: step for step in plan["steps"]}
    child = plan["steps"][2]["steps"][0]

    assert by_id["characters"]["depends_on"] == ["script"]
    assert by_id["character_images"]["depends_on"] == ["characters"]
    assert child["depends_on"] == ["character_images"]
    assert by_id["final_video"]["depends_on"] == ["character_image", "characters", "script"]
    assert by_id["final_video"]["uses"][0]["as"] == ["vision", "reference"]
    assert plan["requirements"] == {
        "llm": True,
        "vision": True,
        "media": ["image", "video"],
        "plugins": [],
    }


def test_v2_rejects_unknown_input_step_and_cycles() -> None:
    unknown_input = _base_spec()
    unknown_input["steps"][0]["prompt"]["task"] = "{{ inputs.missing }}"
    with pytest.raises(WorkflowSpecError, match="unknown inputs"):
        compile_workflow_spec(unknown_input)

    unknown_step = _base_spec()
    unknown_step["steps"][0]["prompt"]["task"] = "{{ steps.missing.output }}"
    with pytest.raises(WorkflowSpecError, match="unknown steps"):
        compile_workflow_spec(unknown_step)

    cycle = _base_spec()
    cycle["steps"][0]["needs"] = ["second"]
    cycle["steps"].append(
        {
            "id": "second",
            "title": "第二步",
            "kind": "text",
            "needs": ["script"],
            "prompt": {"task": "继续。"},
        }
    )
    with pytest.raises(WorkflowSpecError, match="dependency cycle"):
        compile_workflow_spec(cycle)

    output_condition = _base_spec()
    output_condition["steps"][0]["when"] = {"path": "steps.other.output.ready", "op": "eq", "value": True}
    with pytest.raises(WorkflowSpecError, match="condition path must reference one root input"):
        compile_workflow_spec(output_condition)


def test_v2_loop_has_one_explicit_source_and_no_implicit_repeat_aliases() -> None:
    payload = _base_spec()
    payload["steps"] = [
        {
            "id": "loop",
            "title": "循环",
            "kind": "loop",
            "foreach": {"count": "inputs.episode_count", "as": "episode"},
            "steps": [
                {
                    "id": "episode_text",
                    "title": "分集文本",
                    "kind": "text",
                    "prompt": {"task": "写第 {{ episode }} 集。"},
                }
            ],
        }
    ]
    plan = compile_workflow_spec(payload)
    assert plan["steps"][0]["foreach"] == {"count": "inputs.episode_count", "as": "episode"}

    payload["steps"][0]["foreach"]["items"] = "inputs.plot"
    with pytest.raises(WorkflowSpecError, match="exactly one"):
        parse_workflow_spec(payload)


def test_v2_direct_source_media_is_unambiguous() -> None:
    payload = _base_spec()
    payload["inputs"]["image"] = {"type": "image", "label": "源图", "required": True}
    payload["steps"].append(
        {
            "id": "source_image",
            "title": "上传图",
            "kind": "plugin",
            "plugin": {
                "id": "openreel.input",
                "action": "read",
                "inputs": {"image": "{{ inputs.image }}"},
            },
        }
    )
    payload["steps"].append(
        {
            "id": "adopt",
            "title": "采用源图",
            "kind": "image",
            "uses": [{"from": "source_image", "as": ["source"]}],
        }
    )
    plan = compile_workflow_spec(payload)
    assert plan["steps"][-1]["operation"] == "media"
    assert plan["steps"][-1]["output"] == {"canvas": True, "shape": "image"}

    payload["steps"][-1]["uses"][0]["as"] = ["source", "vision"]
    with pytest.raises(WorkflowSpecError, match="source cannot be combined"):
        parse_workflow_spec(payload)


def test_v2_plugin_contract_derives_requirement() -> None:
    payload = _base_spec()
    payload["steps"].append(
        {
            "id": "extract",
            "title": "提取关键帧",
            "kind": "plugin",
            "needs": ["script"],
            "plugin": {
                "id": "video.keyframe_extractor",
                "action": "extract",
                "inputs": {"description": "{{ steps.script.output }}"},
                "settings": {"count": 8},
            },
        }
    )
    plan = compile_workflow_spec(payload)
    assert plan["requirements"]["plugins"] == ["video.keyframe_extractor"]
    assert plan["steps"][-1]["depends_on"] == ["script"]


def test_v2_canonical_payload_contains_no_runtime_state() -> None:
    payload = workflow_spec_payload(_base_spec())
    assert payload["schema"] == WORKFLOW_SPEC_VERSION
    assert payload["steps"][0]["output"] == {"canvas": True}
    assert "plan_hash" not in payload
    assert "status" not in str(payload)
    assert "runner" not in str(payload)


def test_builtin_template_is_native_v2_and_has_logical_media_steps() -> None:
    summary = next(
        item
        for item in canvas_workflow_templates.list_template_summaries()
        if item["id"] == "general_short_drama_workflow"
    )
    assert summary["workflow_spec_version"] == WORKFLOW_SPEC_VERSION
    assert [step["id"] for step in summary["steps"]] == [
        "episode_plan",
        "script",
        "production_plan",
        "character_images",
        "segment_production",
    ]
    segment_loop = summary["steps"][-1]
    assert [step["id"] for step in segment_loop["steps"]][-2:] == ["storyboard", "final_video"]
    assert not any(step["id"].endswith("_prompt") for step in segment_loop["steps"])


def test_builtin_v2_compiles_private_phases_without_persisting_them() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    private = compile_private_execution_template(public)
    assert private["schema"] == WORKFLOW_PLAN_VERSION
    assert private["public_spec"] == public
    assert "node_type" not in str(public)
    assert "runner" not in str(public)
    assert "model_tier" not in str(private)

    segment_loop = next(step for step in private["steps"] if step["id"] == "segment_production")
    private_child_ids = [step["id"] for step in segment_loop["steps"]]
    assert "storyboard__prompt" in private_child_ids
    assert "storyboard" in private_child_ids
    assert "final_video__prompt" in private_child_ids
    assert "final_video" in private_child_ids
    assert all("runtime_hidden" not in step for step in segment_loop["steps"])


def test_private_loop_expansion_resolves_item_fields_but_keeps_workflow_paths() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    normalized = canvas_workflow_templates.normalize_inline_workflow(
        public,
        input_values={
            "production_plan": {
                "output": {
                    "main_characters": [
                        {"character_id": "hero", "name": "阿澈", "identity": "少年", "appearance": "黑发", "wardrobe": "蓝衣", "consistency_rules": "保持一致"}
                    ],
                    "segments": [
                        {"segment_id": "s1", "duration_seconds": 9, "title": "相遇"}
                    ],
                }
            }
        },
    )
    character_prompt = next(step for step in normalized["steps"] if step["id"].endswith("character_image__prompt"))
    final_video = next(step for step in normalized["steps"] if step["id"].endswith("final_video"))

    assert "阿澈" in character_prompt["prompt_template"]
    assert "{{ steps.production_plan.output.style_template }}" in character_prompt["prompt_template"]
    assert final_video["fields"]["duration_seconds"] == "9"


def test_private_llm_phases_execute_even_though_they_are_not_public_nodes() -> None:
    template = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow",
        input_values={"plot": "雨夜相遇", "duration_seconds": 15, "episode_count": 1},
    )
    virtual = workflow_tools._virtual_workflow_step_ids(template["steps"], template["input_values"])

    assert "episode_plan" in virtual
    assert "script__generate" not in virtual
    assert next(step for step in template["steps"] if step["id"] == "script__generate")["surface"] == "workflow_runtime"


def test_template_loader_rejects_v1_instead_of_converting_it() -> None:
    legacy = {
        "workflow_spec_version": "openreel.workflow.v1",
        "id": "legacy",
        "name": "旧模板",
        "steps": [{"id": "text", "node_type": "text", "runner": "node.run"}],
    }
    with pytest.raises(canvas_workflow_templates.WorkflowTemplateError):
        canvas_workflow_templates.normalize_inline_workflow(legacy)


def test_v2_audit_reports_logical_outputs_and_private_deferred_loops() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    report = audit_workflow_spec(public)

    assert report["status"] == "pass"
    assert report["protocol"]["protocol_version"] == WORKFLOW_SPEC_VERSION
    assert report["dry_run"]["visible_output_ids"] == [
        "script",
        "character_image",
        "segment_script",
        "scene_reference",
        "storyboard",
        "final_video",
    ]
    assert report["dry_run"]["leaf_visible_output_ids"] == ["final_video"]
    assert report["dry_run"]["final_output_ids"] == ["final_video"]
    assert report["dry_run"]["deferred_group_ids"] == ["character_images", "segment_production"]


def test_builtin_v2_canvas_projection_hides_private_phases_and_expands_final_videos() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    result = workflow_canvas_projection.project_workflow_canvas(
        project_id="projection-test",
        workflow=public,
        inputs={"plot": "雨夜天台收到未来来信", "duration_seconds": 30, "segment_seconds": 15},
        context={
            "production_plan": {
                "output": {
                    "main_characters": [{"character_id": "hero", "name": "林岚"}],
                    "segments": [
                        {"segment_id": "s1", "duration_seconds": 15},
                        {"segment_id": "s2", "duration_seconds": 15},
                    ],
                }
            }
        },
    )

    canvas_ids = [node["id"] for node in result["canvas"]["nodes"]]
    assert result["ok"] is True
    assert not any("__prompt" in step_id or "__generate" in step_id for step_id in canvas_ids)
    assert [node["id"] for node in result["canvas"]["final_outputs"]] == [
        "segment_production_s1_final_video",
        "segment_production_s2_final_video",
    ]


def test_workflow_build_guide_documents_v2_high_frequency_errors() -> None:
    assert "openreel.workflow.v2" in WORKFLOW_SPEC_V2_GUIDE
    assert "vision" in WORKFLOW_SPEC_V2_GUIDE
    assert "reference" in WORKFLOW_SPEC_V2_GUIDE
    assert "select.values" in WORKFLOW_SPEC_V2_GUIDE
    assert "Do not create prompt sibling steps" in WORKFLOW_SPEC_V2_GUIDE
    assert "provider/model routing" in WORKFLOW_SPEC_V2_GUIDE
    assert "openreel.workflow.authoring.v1" not in WORKFLOW_SPEC_V2_GUIDE


def test_user_template_file_stays_a_plain_portable_v2_document(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    saved = workflow_template_store.save_user_template(
        workflow=_base_spec(),
        template_id="portable_video_flow",
        name="可移植视频流程",
        replace_existing=True,
    )
    stored = json.loads(
        (tmp_path / "workflow_templates" / "user" / "portable_video_flow.json").read_text(encoding="utf-8")
    )

    assert saved["ok"] is True
    assert stored["schema"] == WORKFLOW_SPEC_VERSION
    assert stored["id"] == "portable_video_flow"
    assert stored["title"] == "可移植视频流程"
    assert "workflow" not in stored
    assert "x-openreel" not in stored
    assert "runner" not in str(stored)


def test_workflow_llm_routing_does_not_classify_titles_or_skills() -> None:
    for workflow, fields in (
        ({"primary_skill": "character_prompt"}, {"title": "主要人物参考图提示词"}),
        ({"llm_task_type": "script_generation"}, {"title": "剧本"}),
        ({}, {"title": "任意文本"}),
    ):
        assert node_universal._workflow_text_task_type(workflow, fields) == "workflow_text_generation"


def test_runtime_public_steps_collapse_private_prompt_phases() -> None:
    collapsed = workflow_tools._collapse_workflow_runtime_phases([
        {
            "id": "storyboard__prompt",
            "logical_step_id": "storyboard",
            "title": "分镜图 · 提示词",
            "type": "text",
            "status": "completed",
            "runtime_only": True,
            "canvas_output": False,
            "depends_on": ["scene"],
            "run_count": 1,
        },
        {
            "id": "storyboard",
            "logical_step_id": "storyboard",
            "title": "分镜图",
            "type": "image",
            "status": "completed",
            "runtime_only": False,
            "canvas_output": True,
            "depends_on": ["storyboard__prompt", "scene"],
            "node_id": "node-storyboard",
            "run_count": 1,
        },
    ])

    assert len(collapsed) == 1
    assert collapsed[0]["id"] == "storyboard"
    assert collapsed[0]["logical_step_id"] == "storyboard"
    assert collapsed[0]["depends_on"] == ["scene"]
    assert collapsed[0]["run_count"] == 2
    assert "提示词" not in collapsed[0]["title"]


def test_runtime_public_payload_never_exposes_builtin_prompt_phase() -> None:
    state = {
        "workflow_input_values": {
            "by_instance": {
                "wf_v2": {
                    "workflow_id": "general_short_drama_workflow",
                    "instance_id": "wf_v2",
                    "values": {"plot": "雨夜相遇", "duration_seconds": 15},
                }
            }
        },
        "workflow_runtime": {
            "instances": {
                "wf_v2": {
                    "template_id": "general_short_drama_workflow",
                    "template_name": "通用视频制作工作流",
                    "steps": {
                        "script__generate": {
                            "type": "text",
                            "title": "剧本 · 生成",
                            "status": "completed",
                            "surface": "workflow_runtime",
                            "workflow": {
                                "step_id": "script__generate",
                                "logical_step_id": "script",
                                "surface": "workflow_runtime",
                                "runtime_hidden": True,
                            },
                            "output": {"content": "生成的剧本"},
                        },
                        "script": {
                            "type": "text",
                            "title": "剧本",
                            "status": "completed",
                            "surface": "draft_canvas",
                            "node_id": "node-script",
                            "workflow": {
                                "step_id": "script",
                                "logical_step_id": "script",
                                "surface": "draft_canvas",
                            },
                            "output": {"content": "生成的剧本"},
                        },
                    },
                }
            }
        },
    }
    payload = workflow_tools.workflow_runtime_public_payload(
        state,
        template_id="general_short_drama_workflow",
        instance_id="wf_v2",
    )
    ids = [step["id"] for step in payload["steps"]]

    assert "script" in ids
    assert "script__generate" not in ids
    assert next(step for step in payload["steps"] if step["id"] == "script")["status"] == "completed"
