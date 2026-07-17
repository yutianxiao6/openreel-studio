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
from app.agent.workflow_spec_prompt_contract import WORKFLOW_SPEC_V2_EXAMPLE, WORKFLOW_SPEC_V2_GUIDE
from app.agent import workflow_template_store
from app.agent import workflow_canvas_projection
from app.agent import workflow_spec_artifacts
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


def test_video_execution_setting_controls_automatic_media_generation() -> None:
    payload = _base_spec()
    payload["steps"].append({
        "id": "final_video",
        "title": "最终视频",
        "kind": "video",
        "execution": "manual",
        "prompt": {"task": "根据 {{ steps.script.output }} 生成视频。"},
    })

    private = compile_private_execution_template(payload)
    by_id = {step["id"]: step for step in private["steps"]}
    assert by_id["final_video__prompt"]["manual_only"] is False
    assert by_id["final_video"]["manual_only"] is True
    assert by_id["final_video"]["fields"]["workflow_generate"] is False

    payload["steps"][-1]["execution"] = "auto"
    auto_private = compile_private_execution_template(payload)
    auto_video = next(step for step in auto_private["steps"] if step["id"] == "final_video")
    assert auto_video["manual_only"] is False
    assert auto_video["fields"]["workflow_generate"] is True


def test_v2_derives_dependencies_from_prompt_media_and_loop_paths() -> None:
    payload = _base_spec()
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


def _bounded_feedback_loop_spec() -> dict:
    payload = _base_spec()
    payload["inputs"] = {}
    payload["steps"] = [
        {
            "id": "quality_loop",
            "title": "质量反馈循环",
            "kind": "loop",
            "foreach": {
                "count": 3,
                "as": "attempt",
                "until": {
                    "path": "steps.quality_review.output.score",
                    "op": "gte",
                    "value": 80,
                },
            },
            "steps": [
                {
                    "id": "generate",
                    "title": "生成结果",
                    "kind": "text",
                    "prompt": {
                        "task": "根据原始要求生成；上一轮审核反馈：{{ previous }}。",
                    },
                },
                {
                    "id": "quality_review",
                    "title": "质量审核",
                    "kind": "object",
                    "needs": ["generate"],
                    "prompt": {
                        "task": "审核 {{ steps.generate.output }} 并给出结构化结果。",
                    },
                    "output": {
                        "schema": {
                            "fields": [
                                {"id": "score", "type": "integer", "required": True},
                                {"id": "summary", "type": "string", "required": True},
                                {"id": "regeneration_instruction", "type": "string"},
                            ]
                        }
                    },
                },
            ],
        },
        {
            "id": "result",
            "title": "最终结果",
            "kind": "text",
            "needs": ["quality_loop"],
            "prompt": {"task": "输出最终通过的结果。"},
        },
    ]
    return payload


def test_v2_bounded_feedback_loop_compiles_until_without_creating_a_dependency_cycle() -> None:
    payload = _bounded_feedback_loop_spec()

    public_plan = compile_workflow_spec(payload)
    public_loop = public_plan["steps"][0]
    private = compile_private_execution_template(payload)
    private_loop = private["steps"][0]

    assert public_loop["depends_on"] == []
    assert public_loop["foreach"]["until"] == {
        "path": "steps.quality_review.output.score",
        "op": "gte",
        "value": 80,
    }
    assert private_loop["foreach"]["until"] == public_loop["foreach"]["until"]


def test_v2_protocol_info_exposes_generic_bounded_feedback_loop_contract() -> None:
    protocol = canvas_workflow_templates.workflow_protocol_info()
    contract = protocol["loop_until"]

    assert protocol["media_runtime_settings"]["source"] == "frontend_ui_overrides"
    assert protocol["media_runtime_settings"]["spec_policy"] == "omitted"
    assert {
        "model",
        "provider",
        "aspect_ratio",
        "resolution",
        "width",
        "height",
        "quality",
        "fps",
    }.issubset(
        set(protocol["media_runtime_settings"]["input_keys"])
        | set(protocol["media_runtime_settings"]["media_field_keys"])
    )

    assert contract == {
        "source": "foreach.count",
        "count_min": 1,
        "count_max": 10,
        "path": "steps.<child>.output...",
        "gate_source": "direct_terminal_child",
        "gate_source_must_run_each_attempt": True,
        "gate_source_on_error": "stop",
        "gate_source_when": "unsupported",
        "operators": [
            "eq", "ne", "lt", "lte", "gt", "gte", "empty", "not_empty",
        ],
        "previous_context": "{{ previous }}",
        "feedback_wiring": "forward_only_runtime_dependency",
        "downstream_dependency": "loop_step",
        "exhaustion": "stop_downstream",
    }
    assert protocol["loop_scope"] == {
        "stable_item_identity": "foreach.key",
        "logical_reference_resolution": "shared_parent_scope_then_repeat_index",
        "feedback_downstream": "latest_completed_attempt_in_same_parent_scope",
        "cross_collection_reference": "uses.select",
        "projection_matches_runtime": True,
    }


@pytest.mark.parametrize(
    ("foreach_patch", "message"),
    [
        ({"items": "steps.generate.output", "count": None}, "fixed integer count"),
        ({"count": "inputs.episode_count"}, "fixed integer count"),
        ({"count": True}, "fixed integer count"),
        ({"count": 3.0}, "fixed integer count"),
        ({"count": 11}, "at most 10"),
        (
            {"until": {"path": "steps.missing.output.score", "op": "gte", "value": 80}},
            "current loop child",
        ),
    ],
)
def test_v2_bounded_feedback_loop_rejects_unsafe_until_contracts(
    foreach_patch: dict,
    message: str,
) -> None:
    payload = _bounded_feedback_loop_spec()
    foreach = payload["steps"][0]["foreach"]
    for key, value in foreach_patch.items():
        if value is None:
            foreach.pop(key, None)
        else:
            foreach[key] = value

    with pytest.raises(WorkflowSpecError, match=message):
        parse_workflow_spec(payload)


def test_v2_bounded_feedback_loop_requires_the_gate_source_to_be_terminal() -> None:
    payload = _bounded_feedback_loop_spec()
    payload["steps"][0]["steps"].append(
        {
            "id": "after_review",
            "title": "审核后处理",
            "kind": "text",
            "needs": ["quality_review"],
            "prompt": {"task": "继续处理。"},
        }
    )

    with pytest.raises(WorkflowSpecError, match="must be a terminal child"):
        compile_workflow_spec(payload)


@pytest.mark.parametrize(
    "source_patch",
    [
        {"on_error": "continue"},
        {"when": {"path": "inputs.episode_count", "op": "gte", "value": 1}},
    ],
)
def test_v2_bounded_feedback_loop_gate_source_must_run_each_attempt(source_patch: dict) -> None:
    payload = _bounded_feedback_loop_spec()
    payload["inputs"]["episode_count"] = {
        "type": "integer",
        "label": "集数",
        "default": 1,
    }
    payload["steps"][0]["steps"][1].update(source_patch)

    with pytest.raises(WorkflowSpecError, match="must run and produce an output"):
        compile_workflow_spec(payload)


def test_v2_bounded_feedback_loop_expands_one_attempt_at_a_time_and_chains_feedback() -> None:
    payload = _bounded_feedback_loop_spec()

    first = canvas_workflow_templates.normalize_inline_workflow(payload)
    first_ids = [step["id"] for step in first["steps"]]
    assert first_ids == [
        "quality_loop_i1_generate",
        "quality_loop_i1_quality_review",
        "result",
    ]

    failed_review = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "output": {
                "score": 60,
                "summary": "结果未达到要求",
                "regeneration_instruction": "修复审核发现的问题",
            },
        }
    }
    second = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"context": failed_review},
    )
    second_ids = [step["id"] for step in second["steps"]]
    assert second_ids == [
        "quality_loop_i1_generate",
        "quality_loop_i1_quality_review",
        "quality_loop_i2_generate",
        "quality_loop_i2_quality_review",
        "result",
    ]
    second_generate = next(step for step in second["steps"] if step["id"] == "quality_loop_i2_generate")
    assert "quality_loop_i1_quality_review" in second_generate["depends_on"]

    passed_review = deepcopy(failed_review)
    passed_review["quality_loop_i1_quality_review"]["output"]["score"] = 86
    passed = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"context": passed_review},
    )
    assert [step["id"] for step in passed["steps"]] == first_ids


def test_v2_bounded_feedback_loop_does_not_advance_from_a_stale_review() -> None:
    payload = _bounded_feedback_loop_spec()
    stale_context = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "stale": True,
            "output": {"score": 60, "summary": "过期审核结果"},
        }
    }

    normalized = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"context": stale_context},
    )

    assert "quality_loop_i2_generate" not in {step["id"] for step in normalized["steps"]}


def test_v2_bounded_feedback_loop_blocks_downstream_when_attempts_are_exhausted() -> None:
    payload = _bounded_feedback_loop_spec()
    context = {
        f"quality_loop_i{attempt}_quality_review": {
            "status": "completed",
            "output": {
                "score": 50 + attempt,
                "summary": f"第 {attempt} 次仍未达标",
                "regeneration_instruction": "继续修订",
            },
        }
        for attempt in range(1, 4)
    }
    normalized = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"context": context},
    )

    error = workflow_tools._workflow_repeat_until_error(normalized["steps"], context)

    assert error is not None
    assert error["error_kind"] == "workflow_loop_until_exhausted"
    assert error["group_id"] == "quality_loop"
    assert error["attempt"] == 3
    assert error["last_output"]["summary"] == "第 3 次仍未达标"


def test_v2_bounded_feedback_loop_rejects_invalid_completed_condition_output() -> None:
    payload = _bounded_feedback_loop_spec()
    context = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "output": {"score": "not-a-number", "summary": "输出错误"},
        }
    }

    with pytest.raises(
        canvas_workflow_templates.WorkflowTemplateError,
        match="requires finite numeric values",
    ):
        canvas_workflow_templates.normalize_inline_workflow(
            payload,
            input_values={"context": context},
        )


@pytest.mark.parametrize(
    "output",
    [
        {"summary": "缺少评分"},
        {"score": "86", "summary": "评分类型错误"},
        {"score": float("nan"), "summary": "评分不是有限数值"},
        {"score": float("inf"), "summary": "评分不是有限数值"},
        {"score": 10**1000, "summary": "评分数值溢出"},
    ],
)
def test_v2_bounded_feedback_loop_rejects_missing_or_non_numeric_gate_values(output: dict) -> None:
    payload = _bounded_feedback_loop_spec()
    context = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "output": output,
        }
    }

    with pytest.raises(canvas_workflow_templates.WorkflowLoopUntilError):
        canvas_workflow_templates.normalize_inline_workflow(
            payload,
            input_values={"context": context},
        )


@pytest.mark.asyncio
async def test_v2_bounded_feedback_loop_reports_a_stable_invalid_gate_error() -> None:
    payload = _bounded_feedback_loop_spec()
    context = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "output": {"score": "not-a-number", "summary": "输出错误"},
        }
    }

    template, error = await workflow_tools._workflow_template_from_spec(
        project_id="project-1",
        workflow=payload,
        context=context,
    )

    assert template is None
    assert error is not None
    assert error["error_kind"] == "workflow_loop_until_invalid"


def test_v2_bounded_feedback_loop_injects_full_previous_review_into_next_prompt() -> None:
    payload = _bounded_feedback_loop_spec()
    review_output = {
        "score": 61,
        "summary": "存在多项质量问题",
        "regeneration_instruction": "逐项修复后重新生成",
    }
    normalized = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={
            "context": {
                "quality_loop_i1_quality_review": {
                    "status": "completed",
                    "output": review_output,
                }
            }
        },
    )
    target_step = next(step for step in normalized["steps"] if step["id"] == "quality_loop_i2_generate")
    previous_record = {
        "id": "workflow-runtime:wf_feedback:quality_loop_i1_quality_review",
        "type": "text",
        "title": "质量审核",
        "status": "completed",
        "output": review_output,
        "input": {
            "workflow": {
                "template_id": "video_flow",
                "instance_id": "wf_feedback",
                "step_id": "quality_loop_i1_quality_review",
                "template_step_id": "quality_review",
                "repeat_group_id": "quality_loop",
                "repeat_group_index": 1,
            }
        },
    }
    selected = workflow_tools._workflow_records_for_prompt_context(
        [previous_record],
        template_id="video_flow",
        instance_id="wf_feedback",
        target_step_id="quality_loop_i2_generate",
        target_step=target_step,
    )
    compact = [node_universal._compact_workflow_text_node(record) for record in selected]
    rendered = node_universal._workflow_render_prompt_template(
        target_step["prompt_template"],
        workflow={
            "repeat_group_id": "quality_loop",
            "repeat_group_index": 2,
            "input_facts": {},
        },
        target={"id": "quality_loop_i2_generate"},
        upstream_nodes=compact,
    )

    assert rendered["unresolved_template_paths"] == []
    assert json.dumps(review_output, ensure_ascii=False) in rendered["rendered_prompt_template"]


def test_v2_bounded_feedback_loop_keeps_nested_parent_scopes_isolated() -> None:
    inner = deepcopy(_bounded_feedback_loop_spec()["steps"][0])
    inner["id"] = "quality_loop"
    payload = _base_spec()
    payload["inputs"] = {}
    payload["steps"] = [
        {
            "id": "parent_loop",
            "title": "父循环",
            "kind": "loop",
            "foreach": {"count": 2, "as": "parent"},
            "steps": [inner],
        }
    ]
    context = {
        "parent_loop_i1_quality_loop_i1_quality_review": {
            "status": "completed",
            "output": {"score": 90, "summary": "父实例一已通过"},
        },
        "parent_loop_i2_quality_loop_i1_quality_review": {
            "status": "completed",
            "output": {"score": 60, "summary": "父实例二未通过"},
        },
    }

    normalized = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"context": context},
    )
    ids = [step["id"] for step in normalized["steps"]]

    assert "parent_loop_i1_quality_loop_i2_generate" not in ids
    assert "parent_loop_i2_quality_loop_i2_generate" in ids
    second_parent_retry = next(
        step for step in normalized["steps"]
        if step["id"] == "parent_loop_i2_quality_loop_i2_generate"
    )
    assert "parent_loop_i2_quality_loop_i1_quality_review" in second_parent_retry["depends_on"]
    assert "parent_loop_i1_quality_loop_i1_quality_review" not in second_parent_retry["depends_on"]


def test_v2_nested_loop_strips_structural_ancestor_dependencies() -> None:
    inner = deepcopy(_bounded_feedback_loop_spec()["steps"][0])
    inner["needs"] = ["frame_plan", "segment_production"]
    inner["steps"][0]["needs"] = [
        "frame_plan",
        "segment_production",
        "quality_loop",
    ]
    inner["steps"][1]["needs"] = [
        "frame_plan",
        "generate",
        "segment_production",
        "quality_loop",
    ]
    payload = _base_spec()
    payload["inputs"] = {}
    payload["steps"] = [
        {
            "id": "segment_production",
            "title": "分段制作",
            "kind": "loop",
            "foreach": {"count": 1, "as": "segment"},
            "steps": [
                {
                    "id": "frame_plan",
                    "title": "分镜规划",
                    "kind": "text",
                    "needs": ["segment_production"],
                    "prompt": {"task": "输出分镜规划。"},
                },
                inner,
            ],
        }
    ]

    normalized = canvas_workflow_templates.normalize_inline_workflow(payload)
    generate = next(
        step for step in normalized["steps"]
        if step["id"] == "segment_production_i1_quality_loop_i1_generate"
    )
    review = next(
        step for step in normalized["steps"]
        if step["id"] == "segment_production_i1_quality_loop_i1_quality_review"
    )

    assert generate["depends_on"] == ["segment_production_i1_frame_plan"]
    assert review["depends_on"] == [
        "segment_production_i1_frame_plan",
        "segment_production_i1_quality_loop_i1_generate",
    ]
    dependencies = {
        dependency
        for step in normalized["steps"]
        for dependency in step.get("depends_on") or []
    }
    assert "segment_production" not in dependencies
    assert "segment_production_i1_quality_loop" not in dependencies


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "expected_ready"),
    [
        (60, ["quality_loop_i2_generate"]),
        (86, ["result"]),
    ],
)
async def test_v2_ready_batch_uses_review_result_to_retry_or_continue(
    monkeypatch: pytest.MonkeyPatch,
    score: int,
    expected_ready: list[str],
) -> None:
    payload = _bounded_feedback_loop_spec()
    first = canvas_workflow_templates.normalize_inline_workflow(payload)
    review_output = {
        "score": score,
        "summary": "通过" if score >= 80 else "需要修订",
        "regeneration_instruction": "修订后重试" if score < 80 else "",
    }
    context = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "output": review_output,
        }
    }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_feedback": {
                    "instance_id": "wf_feedback",
                    "template_id": "video_flow",
                    "steps": {
                        "quality_loop_i1_generate": {
                            "status": "completed",
                            "output": {"content": "第一版"},
                            "workflow": {"surface": "workflow_runtime"},
                        },
                        "quality_loop_i1_quality_review": {
                            "status": "completed",
                            "output": review_output,
                            "workflow": {"surface": "workflow_runtime"},
                        },
                    },
                }
            }
        }
    }

    async def read_state(_project_id: str) -> dict:
        return state

    async def runtime_context(*_args: object, **_kwargs: object) -> dict:
        return context

    async def no_settle(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_context_from_project", runtime_context)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_settle_terminal_running_steps_for_run", no_settle)

    batch = await workflow_tools._workflow_ready_step_batch(
        project_id="project-1",
        template=first,
        workflow=payload,
        instance_id="wf_feedback",
    )

    assert batch["ok"] is True
    assert batch["ready_step_ids"] == expected_ready


@pytest.mark.asyncio
async def test_v2_ready_batch_returns_exhausted_error_before_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _bounded_feedback_loop_spec()
    context: dict[str, dict] = {}
    runtime_steps: dict[str, dict] = {}
    for attempt in range(1, 4):
        generate_id = f"quality_loop_i{attempt}_generate"
        review_id = f"quality_loop_i{attempt}_quality_review"
        review_output = {
            "score": 50 + attempt,
            "summary": f"第 {attempt} 次仍需修改",
            "regeneration_instruction": "继续修订",
        }
        context[review_id] = {"status": "completed", "output": review_output}
        runtime_steps[generate_id] = {
            "status": "completed",
            "output": {"content": f"版本 {attempt}"},
            "workflow": {"surface": "workflow_runtime"},
        }
        runtime_steps[review_id] = {
            "status": "completed",
            "output": review_output,
            "workflow": {"surface": "workflow_runtime"},
        }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_feedback": {
                    "instance_id": "wf_feedback",
                    "template_id": "video_flow",
                    "steps": runtime_steps,
                }
            }
        }
    }

    async def read_state(_project_id: str) -> dict:
        return state

    async def runtime_context(*_args: object, **_kwargs: object) -> dict:
        return context

    async def no_settle(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_context_from_project", runtime_context)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_settle_terminal_running_steps_for_run", no_settle)

    batch = await workflow_tools._workflow_ready_step_batch(
        project_id="project-1",
        template=canvas_workflow_templates.normalize_inline_workflow(payload),
        workflow=payload,
        instance_id="wf_feedback",
    )

    assert batch["ok"] is False
    assert batch["error_kind"] == "workflow_loop_until_exhausted"
    assert batch["attempt"] == 3
    assert batch["last_output"]["summary"] == "第 3 次仍需修改"
    assert batch["ready_step_ids"] == []


def test_v2_feedback_loop_downstream_selects_latest_completed_attempt() -> None:
    nodes = []
    for attempt in (1, 2):
        for child in ("generate", "quality_review"):
            nodes.append(
                {
                    "id": f"node-{attempt}-{child}",
                    "status": "completed",
                    "updated_at": f"2026-07-14T00:00:0{attempt}Z",
                    "output": {"content": f"版本 {attempt} {child}"},
                    "input": {
                        "workflow": {
                            "template_id": "video_flow",
                            "instance_id": "wf_feedback",
                            "step_id": f"quality_loop_i{attempt}_{child}",
                            "template_step_id": child,
                            "repeat_group_id": "quality_loop",
                            "repeat_group_index": attempt,
                            "repeat_until": {
                                "path": "steps.quality_review.output.score",
                                "op": "gte",
                                "value": 80,
                            },
                        }
                    },
                }
            )
    by_id = workflow_tools._workflow_step_nodes_by_id(nodes, "video_flow", "wf_feedback")
    by_alias = workflow_tools._workflow_step_nodes_by_alias(nodes, "video_flow", "wf_feedback")

    selected = workflow_tools._workflow_dependency_nodes(
        "generate",
        created_by_step=by_id,
        nodes_by_alias=by_alias,
        target_step={"id": "result"},
    )

    assert [node["id"] for node in selected] == ["node-2-generate"]

    selected_group = workflow_tools._workflow_dependency_nodes(
        "quality_loop",
        created_by_step=by_id,
        nodes_by_alias=by_alias,
        target_step={"id": "result"},
    )

    assert {node["id"] for node in selected_group} == {
        "node-2-generate",
        "node-2-quality_review",
    }


def test_v2_nested_feedback_dependencies_stay_inside_the_parent_segment() -> None:
    def record(
        node_id: str,
        *,
        template_step_id: str,
        step_id: str,
        group_id: str,
        group_index: int,
        segment_id: str,
        feedback: bool = False,
    ) -> dict:
        workflow = {
            "template_id": "nested_video_flow",
            "instance_id": "wf_nested_feedback",
            "step_id": step_id,
            "template_step_id": template_step_id,
            "repeat_group_id": group_id,
            "repeat_group_index": group_index,
            "instance_scope": {"segment_id": segment_id, "attempt": group_index},
        }
        if feedback:
            workflow["repeat_until"] = {
                "path": "steps.storyboard_review.output.score",
                "op": "gte",
                "value": 80,
            }
        return {
            "id": node_id,
            "status": "completed",
            "input": {"workflow": workflow},
        }

    records = [
        record(
            "scene-s1",
            template_step_id="scene_reference",
            step_id="segment_production_s1_scene_reference",
            group_id="segment_production",
            group_index=1,
            segment_id="s1",
        ),
        record(
            "scene-s2",
            template_step_id="scene_reference",
            step_id="segment_production_s2_scene_reference",
            group_id="segment_production",
            group_index=2,
            segment_id="s2",
        ),
    ]
    for segment_id in ("s1", "s2"):
        for attempt in (1, 2):
            records.append(
                record(
                    f"storyboard-{segment_id}-{attempt}",
                    template_step_id="storyboard",
                    step_id=(
                        f"segment_production_{segment_id}_storyboard_review_loop_"
                        f"i{attempt}_storyboard"
                    ),
                    group_id=f"segment_production_{segment_id}_storyboard_review_loop",
                    group_index=attempt,
                    segment_id=segment_id,
                    feedback=True,
                )
            )

    by_id = workflow_tools._workflow_step_nodes_by_id(
        records, "nested_video_flow", "wf_nested_feedback"
    )
    by_alias = workflow_tools._workflow_step_nodes_by_alias(
        records, "nested_video_flow", "wf_nested_feedback"
    )

    first_storyboard = {
        "repeat_group_id": "segment_production_s1_storyboard_review_loop",
        "repeat_group_index": 1,
        "instance_scope": {"segment_id": "s1", "attempt": 1},
    }
    assert [
        node["id"]
        for node in workflow_tools._workflow_dependency_nodes(
            "scene_reference",
            created_by_step=by_id,
            nodes_by_alias=by_alias,
            target_step=first_storyboard,
        )
    ] == ["scene-s1"]

    second_review = {
        "repeat_group_id": "segment_production_s1_storyboard_review_loop",
        "repeat_group_index": 2,
        "instance_scope": {"segment_id": "s1", "attempt": 2},
    }
    assert [
        node["id"]
        for node in workflow_tools._workflow_dependency_nodes(
            "storyboard",
            created_by_step=by_id,
            nodes_by_alias=by_alias,
            target_step=second_review,
        )
    ] == ["storyboard-s1-2"]

    first_final_video = {
        "repeat_group_id": "segment_production",
        "repeat_group_index": 1,
        "instance_scope": {"segment_id": "s1"},
    }
    second_final_video = {
        "repeat_group_id": "segment_production",
        "repeat_group_index": 2,
        "instance_scope": {"segment_id": "s2"},
    }
    assert [
        node["id"]
        for node in workflow_tools._workflow_dependency_nodes(
            "storyboard",
            created_by_step=by_id,
            nodes_by_alias=by_alias,
            target_step=first_final_video,
        )
    ] == ["storyboard-s1-2"]
    assert [
        node["id"]
        for node in workflow_tools._workflow_dependency_nodes(
            "storyboard",
            created_by_step=by_id,
            nodes_by_alias=by_alias,
            target_step=second_final_video,
        )
    ] == ["storyboard-s2-2"]


def test_v2_feedback_loop_projection_maps_context_refs_to_latest_attempt() -> None:
    payload = _bounded_feedback_loop_spec()
    loop = payload["steps"][0]
    loop["steps"][0] = {
        "id": "generate",
        "title": "生成图片",
        "kind": "image",
        "prompt": {"task": "生成候选图片；上一轮审核：{{ previous }}"},
    }
    loop["steps"][1]["uses"] = [{"from": "generate", "as": ["vision"]}]
    payload["steps"][1] = {
        "id": "result",
        "title": "最终视频",
        "kind": "video",
        "needs": ["quality_loop"],
        "uses": [{"from": "generate", "as": ["vision", "reference"]}],
        "prompt": {"task": "查看通过审核的图片并生成视频。"},
    }

    first = workflow_canvas_projection.project_workflow_canvas(
        project_id="feedback-projection",
        workflow=payload,
    )
    first_final = next(node for node in first["canvas"]["nodes"] if node["id"] == "result")

    assert first["ok"] is True
    assert first_final["references"] == ["quality_loop_i1_generate"]
    assert {
        (edge["source"], edge["target"], edge["kind"])
        for edge in first["canvas"]["edges"]
    } >= {("quality_loop_i1_generate", "result", "reference")}

    second = workflow_canvas_projection.project_workflow_canvas(
        project_id="feedback-projection",
        workflow=payload,
        context={
            "quality_loop_i1_quality_review": {
                "status": "completed",
                "output": {
                    "score": 60,
                    "summary": "需要修订",
                    "regeneration_instruction": "重做",
                },
            }
        },
    )
    second_final = next(node for node in second["canvas"]["nodes"] if node["id"] == "result")

    assert second["ok"] is True
    assert second_final["references"] == ["quality_loop_i2_generate"]
    assert {
        (edge["source"], edge["target"], edge["kind"])
        for edge in second["canvas"]["edges"]
    } >= {("quality_loop_i2_generate", "result", "reference")}


def test_v2_projection_reference_resolution_uses_parent_scope_not_id_prefix() -> None:
    target = {
        "id": "target_prefers_wrong_prefix",
        "repeat_group_id": "segments",
        "repeat_group_index": 1,
        "instance_scope": {"segment_id": "s1"},
    }
    wrong = {
        "id": "target_prefers_wrong_prefix_storyboard",
        "template_step_id": "storyboard",
        "repeat_group_id": "segment_s2_review",
        "repeat_group_index": 1,
        "instance_scope": {"segment_id": "s2", "attempt": 1},
    }
    correct = {
        "id": "unrelated_id_storyboard",
        "template_step_id": "storyboard",
        "repeat_group_id": "segment_s1_review",
        "repeat_group_index": 1,
        "instance_scope": {"segment_id": "s1", "attempt": 1},
    }

    assert workflow_canvas_projection._projected_ref_id(
        "storyboard",
        target=target,
        steps=[wrong, correct, target],
    ) == "unrelated_id_storyboard"


def test_v2_feedback_loop_direct_downstream_requires_a_matched_gate() -> None:
    payload = _bounded_feedback_loop_spec()
    context = {
        "quality_loop_i1_quality_review": {
            "status": "completed",
            "output": {"score": 60, "summary": "需要修订"},
        }
    }
    normalized = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"context": context},
    )

    error = workflow_tools._workflow_repeat_until_error(
        normalized["steps"],
        context,
        dependency_ids={"quality_loop"},
        require_matched=True,
    )

    assert error is not None
    assert error["error_kind"] == "workflow_loop_until_pending"


def test_v2_feedback_loop_failed_gate_cannot_be_treated_as_optional_completion() -> None:
    payload = _bounded_feedback_loop_spec()
    normalized = canvas_workflow_templates.normalize_inline_workflow(payload)
    context = {
        "quality_loop_i1_quality_review": {
            "status": "failed",
            "output": {"error": "审核模型调用失败"},
        }
    }

    error = workflow_tools._workflow_repeat_until_error(normalized["steps"], context)

    assert error is not None
    assert error["error_kind"] == "workflow_loop_until_invalid"


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


def test_v2_canonical_payload_discards_frontend_media_runtime_settings() -> None:
    payload = _base_spec()
    payload["inputs"].update(
        {
            "aspect_ratio": {"type": "text", "label": "比例", "default": "9:16"},
            "resolution": {"type": "text", "label": "清晰度", "default": "4k"},
            "quality": {"type": "text", "label": "画质", "default": "high"},
        }
    )
    payload["steps"].append(
        {
            "id": "image",
            "title": "图片",
            "kind": "image",
            "prompt": {"task": "生成图片。"},
            "fields": {
                "purpose": "cover",
                "aspect_ratio": "{{ inputs.aspect_ratio }}",
                "resolution": "{{ inputs.resolution }}",
                "quality": "{{ inputs.quality }}",
                "width": 2160,
                "height": 3840,
                "fps": 24,
            },
        }
    )

    canonical = workflow_spec_payload(payload)

    assert {"aspect_ratio", "resolution", "quality"}.isdisjoint(canonical["inputs"])
    assert canonical["steps"][1]["fields"] == {"purpose": "cover"}


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
    assert [step["id"] for step in segment_loop["steps"]][-4:] == [
        "storyboard_review_loop",
        "story_template_review_loop",
        "final_video",
        "final_video_story_template",
    ]
    review_loop = next(step for step in segment_loop["steps"] if step["id"] == "storyboard_review_loop")
    assert [step["id"] for step in review_loop["steps"]] == ["storyboard", "storyboard_review"]
    assert review_loop["when"] == {
        "path": "inputs.visual_plan_mode",
        "op": "eq",
        "value": "storyboard",
    }
    assert review_loop["foreach"] == {
        "count": 3,
        "as": "attempt",
        "until": {
            "path": "steps.storyboard_review.output.score",
            "op": "gte",
            "value": 80,
        },
    }
    story_template_loop = next(
        step for step in segment_loop["steps"] if step["id"] == "story_template_review_loop"
    )
    assert [step["id"] for step in story_template_loop["steps"]] == [
        "story_template",
        "story_template_review",
    ]
    assert story_template_loop["when"] == {
        "path": "inputs.visual_plan_mode",
        "op": "eq",
        "value": "story_template",
    }
    assert story_template_loop["foreach"]["until"] == {
        "path": "steps.story_template_review.output.score",
        "op": "gte",
        "value": 80,
    }
    assert not any(step["id"].endswith("_prompt") for step in segment_loop["steps"])

    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    assert public["inputs"]["video_type"]["type"] == "text"
    assert public["inputs"]["video_type"]["options"] == []
    assert public["inputs"]["visual_plan_mode"] == {
        "type": "enum",
        "label": "画面制作模式",
        "description": "宫格分镜适合通用叙事；故事模板适合复杂动作、空间调度和强视觉风格。",
        "required": False,
        "default": "storyboard",
        "options": [
            {"value": "storyboard", "label": "宫格分镜"},
            {"value": "story_template", "label": "故事模板"},
        ],
    }
    assert "aspect_ratio" not in public["inputs"]
    assert "resolution" not in public["inputs"]


def test_workflow_media_runtime_settings_come_from_ui_overrides() -> None:
    step = {
        "id": "character_images__hero__character_image",
        "logical_step_id": "character_image",
        "node_type": "image",
    }
    ui_overrides = {
        "media_model_defaults": {"image": "default-image-model"},
        "media_field_defaults": {
            "image": {
                "aspect_ratio": "16:9",
                "resolution": "2560x1440",
                "width": 2560,
                "height": 1440,
                "quality": "medium",
            }
        },
        "media_model_overrides": {"character_image": "image-model"},
        "media_field_overrides": {
            "character_image": {
                "aspect_ratio": "9:16",
                "resolution": "1440x2560",
                "width": 1440,
                "height": 2560,
                "quality": "high",
            }
        },
    }

    assert workflow_tools._workflow_ui_node_run_extra_fields(step, ui_overrides) == {
        "aspect_ratio": "9:16",
        "resolution": "1440x2560",
        "width": 1440,
        "height": 2560,
        "quality": "high",
        "model": "image-model",
    }

    dynamic_step = {
        "id": "character_images__supporting__character_image",
        "logical_step_id": "character_image_instance",
        "template_step_id": "character_image_instance",
        "node_type": "image",
    }
    assert workflow_tools._workflow_ui_node_run_extra_fields(dynamic_step, ui_overrides) == {
        "aspect_ratio": "16:9",
        "resolution": "2560x1440",
        "width": 2560,
        "height": 1440,
        "quality": "medium",
        "model": "default-image-model",
    }
    assert workflow_tools._workflow_strip_template_media_settings(
        {
            "purpose": "character_reference",
            "aspect_ratio": "1:1",
            "resolution": "2048x2048",
            "quality": "low",
            "model": "template-model",
        },
        "image",
    ) == {"purpose": "character_reference"}
    assert workflow_tools._workflow_sync_existing_canvas_fields(
        {"aspect_ratio": "16:9", "resolution": "1920x1080", "quality": "medium"},
        {
            "aspect_ratio": "9:16",
            "resolution": "1440x2560",
            "width": 1440,
            "height": 2560,
            "quality": "high",
        },
        "image",
    ) == {
        "aspect_ratio": "9:16",
        "resolution": "1440x2560",
        "width": 1440,
        "height": 2560,
        "quality": "high",
    }


def test_builtin_template_preserves_artifact_prompt_writing_methods() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    top_level = {step["id"]: step for step in public["steps"]}
    character_image = top_level["character_images"]["steps"][0]
    segment_steps = {
        step["id"]: step for step in top_level["segment_production"]["steps"]
    }
    review_steps = {
        step["id"]: step for step in segment_steps["storyboard_review_loop"]["steps"]
    }
    story_template_steps = {
        step["id"]: step for step in segment_steps["story_template_review_loop"]["steps"]
    }

    media_runtime_keys = {"model", "aspect_ratio", "resolution", "quality", "width", "height", "fps"}
    for media_step in (
        character_image,
        segment_steps["scene_reference"],
        review_steps["storyboard"],
        story_template_steps["story_template"],
        segment_steps["final_video"],
        segment_steps["final_video_story_template"],
    ):
        assert media_runtime_keys.isdisjoint(media_step.get("fields", {}))
    assert "官方设定集角色视觉参考表" in character_image["prompt"]["output"]
    assert "正面/侧面/背面全身三面图" in character_image["prompt"]["output"]
    assert "2x2 四机位全景图网格" in segment_steps["scene_reference"]["prompt"]["output"]
    assert "宫格分镜图，电影分镜，每格一个镜头" in review_steps["storyboard"]["prompt"]["output"]
    assert "{{ previous }}" in review_steps["storyboard"]["prompt"]["task"]
    assert review_steps["storyboard_review"]["uses"] == [
        {"from": "storyboard", "as": ["vision"]}
    ]
    review_fields = {
        field["id"]: field
        for field in review_steps["storyboard_review"]["output"]["schema"]["fields"]
    }
    assert set(review_fields) == {
        "score",
        "dimension_scores",
        "reason",
        "issues",
        "regeneration_instruction",
    }
    assert review_fields["score"]["type"] == "integer"
    assert {field["id"] for field in review_fields["dimension_scores"]["fields"]} == {
        "story_theme",
        "visual_expression",
        "shot_language",
        "spatial_continuity",
        "composition",
        "action_rhythm",
        "continuity",
        "technical_usability",
    }
    assert {field["id"] for field in review_fields["issues"]["fields"]} == {
        "category",
        "frame",
        "severity",
        "problem",
        "evidence",
        "correction",
    }
    review_prompt = review_steps["storyboard_review"]["prompt"]
    for criterion in ("剧情主题", "画面表达", "镜头语言", "180 度轴线", "构图", "动作节奏"):
        assert criterion in str(review_prompt)
    assert "任一重大问题都必须压到 80 分以下" in review_prompt["check"]
    assert "dimension_scores" in review_steps["storyboard"]["prompt"]["output"]
    assert "专业电影故事板设计图" in story_template_steps["story_template"]["prompt"]["output"]
    assert "不在提示词中写死比例、像素、模型或画质" in story_template_steps["story_template"]["prompt"]["output"]
    assert "{{ previous }}" in story_template_steps["story_template"]["prompt"]["task"]
    assert story_template_steps["story_template_review"]["uses"] == [
        {"from": "story_template", "as": ["vision"]}
    ]
    story_review_fields = {
        field["id"]: field
        for field in story_template_steps["story_template_review"]["output"]["schema"]["fields"]
    }
    assert set(story_review_fields) == {
        "score",
        "dimension_scores",
        "reason",
        "issues",
        "regeneration_instruction",
    }
    assert {field["id"] for field in story_review_fields["dimension_scores"]["fields"]} == {
        "story_match",
        "shot_sequence_timing",
        "shot_language",
        "spatial_staging",
        "composition_action",
        "reference_fidelity",
        "visual_continuity",
        "technical_usability",
    }
    assert "参考图片的用途声明" in segment_steps["final_video"]["prompt"]["output"]
    assert "画面概述→动作变化" in segment_steps["final_video"]["prompt"]["output"]
    assert "最后一段精确结束于" in segment_steps["final_video"]["prompt"]["output"]
    assert segment_steps["final_video"]["needs"] == ["storyboard_review_loop"]
    assert segment_steps["final_video_story_template"]["needs"] == ["story_template_review_loop"]
    assert "逐格转译" in segment_steps["final_video_story_template"]["prompt"]["role"]
    assert "图上没有的内容不得新增" in segment_steps["final_video_story_template"]["prompt"]["output"]
    assert "第X排第Y格" in segment_steps["final_video_story_template"]["prompt"]["check"]

    frame_schema = {
        field["id"]: field
        for field in segment_steps["frame_plan"]["output"]["schema"]["fields"]
    }
    assert "grid_count" in frame_schema
    assert "grid_position" in {
        field["id"] for field in frame_schema["frames"]["fields"]
    }

    private = compile_private_execution_template(public)
    private_top_level = {step["id"]: step for step in private["steps"]}
    private_character_steps = {
        step["id"]: step for step in private_top_level["character_images"]["steps"]
    }
    private_segment_steps = {
        step["id"]: step for step in private_top_level["segment_production"]["steps"]
    }
    private_review_steps = {
        step["id"]: step for step in private_segment_steps["storyboard_review_loop"]["steps"]
    }
    private_story_template_steps = {
        step["id"]: step
        for step in private_segment_steps["story_template_review_loop"]["steps"]
    }
    assert "官方设定集角色视觉参考表" in private_character_steps[
        "character_image__prompt"
    ]["prompt_template"]
    assert "2x2 四机位全景图网格" in private_segment_steps[
        "scene_reference__prompt"
    ]["prompt_template"]
    assert "每格一个镜头" in private_review_steps["storyboard__prompt"][
        "prompt_template"
    ]
    assert "{{ previous }}" in private_review_steps["storyboard__prompt"]["prompt_template"]
    assert "画面概述→动作变化" in private_segment_steps["final_video__prompt"][
        "prompt_template"
    ]
    assert "专业电影故事板设计图" in private_story_template_steps[
        "story_template__prompt"
    ]["prompt_template"]
    assert "逐格转译" in private_segment_steps["final_video_story_template__prompt"][
        "prompt_template"
    ]


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
    assert "storyboard_review_loop" in private_child_ids
    assert "story_template_review_loop" in private_child_ids
    assert "final_video__prompt" in private_child_ids
    assert "final_video" in private_child_ids
    assert "final_video_story_template__prompt" in private_child_ids
    assert "final_video_story_template" in private_child_ids
    review_loop = next(step for step in segment_loop["steps"] if step["id"] == "storyboard_review_loop")
    assert [step["id"] for step in review_loop["steps"]] == [
        "storyboard__prompt",
        "storyboard",
        "storyboard_review",
    ]
    story_template_loop = next(
        step for step in segment_loop["steps"] if step["id"] == "story_template_review_loop"
    )
    assert story_template_loop["when"]["value"] == "story_template"
    assert [step["id"] for step in story_template_loop["steps"]] == [
        "story_template__prompt",
        "story_template",
        "story_template_review",
    ]
    assert all("runtime_hidden" not in step for step in segment_loop["steps"])


def test_builtin_scene_chain_does_not_depend_on_character_images() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    private = compile_private_execution_template(public)
    segment_loop = next(step for step in private["steps"] if step["id"] == "segment_production")
    children = {step["id"]: step for step in segment_loop["steps"]}
    review_children = {
        step["id"]: step for step in children["storyboard_review_loop"]["steps"]
    }
    story_template_children = {
        step["id"]: step for step in children["story_template_review_loop"]["steps"]
    }

    assert segment_loop["depends_on"] == ["production_plan"]
    for step_id in (
        "segment_script__generate",
        "scene_plan",
        "scene_reference__prompt",
        "scene_reference",
        "frame_plan",
    ):
        assert "character_images" not in children[step_id].get("depends_on", [])
    assert "character_images" in review_children["storyboard__prompt"]["depends_on"]
    assert "character_images" not in review_children["storyboard_review"].get("depends_on", [])
    assert "character_images" in children["final_video__prompt"]["depends_on"]
    assert "character_images" in story_template_children["story_template__prompt"]["depends_on"]
    assert "character_images" not in story_template_children["story_template_review"].get("depends_on", [])
    assert "character_images" in children["final_video_story_template__prompt"]["depends_on"]


def test_builtin_vision_is_only_declared_for_steps_that_must_see_images() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    private = compile_private_execution_template(public)
    segment_loop = next(step for step in private["steps"] if step["id"] == "segment_production")
    children = {step["id"]: step for step in segment_loop["steps"]}
    review_children = {
        step["id"]: step for step in children["storyboard_review_loop"]["steps"]
    }
    story_template_children = {
        step["id"]: step for step in children["story_template_review_loop"]["steps"]
    }

    assert children["scene_reference__prompt"].get("context_refs") in (None, [])
    assert children["frame_plan"]["context_refs"] == [
        {"ref": "scene_reference", "role": "vision_context"}
    ]
    assert review_children["storyboard__prompt"]["context_refs"] == [
        {"ref": "scene_reference", "role": "vision_context"}
    ]
    assert review_children["storyboard__prompt"]["reference_selectors"][0]["role"] == "vision_context"
    assert review_children["storyboard_review"]["context_refs"] == [
        {"ref": "storyboard", "role": "vision_context"}
    ]
    assert children["final_video__prompt"]["context_refs"] == [
        {"ref": "storyboard", "role": "vision_context"},
        {"ref": "scene_reference", "role": "vision_context"},
    ]
    assert children["final_video__prompt"]["reference_selectors"][0]["role"] == "vision_context"
    assert review_children["storyboard"]["context_refs"] == [
        {"ref": "scene_reference", "role": "visual_reference"}
    ]
    assert children["final_video"]["context_refs"] == [
        {"ref": "storyboard", "role": "visual_reference"},
        {"ref": "scene_reference", "role": "visual_reference"},
    ]
    assert story_template_children["story_template__prompt"]["context_refs"] == [
        {"ref": "scene_reference", "role": "vision_context"}
    ]
    assert story_template_children["story_template_review"]["context_refs"] == [
        {"ref": "story_template", "role": "vision_context"}
    ]
    assert children["final_video_story_template__prompt"]["context_refs"] == [
        {"ref": "story_template", "role": "vision_context"},
        {"ref": "scene_reference", "role": "vision_context"},
    ]
    assert story_template_children["story_template"]["context_refs"] == [
        {"ref": "scene_reference", "role": "visual_reference"}
    ]
    assert children["final_video_story_template"]["context_refs"] == [
        {"ref": "story_template", "role": "visual_reference"},
        {"ref": "scene_reference", "role": "visual_reference"},
    ]


def test_workflow_managed_references_replace_legacy_edges_without_losing_manual_refs() -> None:
    fields = {
        "workflow": {"template_id": "short_drama", "instance_id": "wf-1"},
        "references": [
            {"ref": "node:character", "role": "context"},
            {"ref": "node:script", "role": "context"},
        ],
        "depends_on": ["node:character", "node:script"],
    }

    migrated = workflow_tools._merge_workflow_dependency_refs(
        fields,
        [{"ref": "node:script", "role": "context"}],
        replace_managed=True,
    )

    assert migrated["references"] == [{"ref": "node:script", "role": "context"}]
    assert migrated["depends_on"] == ["node:script"]
    assert migrated["workflow"]["managed_references"] == [
        {"ref": "node:script", "role": "context"}
    ]

    migrated["references"].append({"ref": "asset:user-style", "role": "style_reference"})
    refreshed = workflow_tools._merge_workflow_dependency_refs(
        migrated,
        [{"ref": "node:new-script", "role": "context"}],
        replace_managed=True,
    )

    assert refreshed["references"] == [
        {"ref": "asset:user-style", "role": "style_reference"},
        {"ref": "node:new-script", "role": "context"},
    ]
    assert refreshed["depends_on"] == ["asset:user-style", "node:new-script"]

    cleared = workflow_tools._merge_workflow_dependency_refs(
        {
            "workflow": {
                "template_id": "short_drama",
                "managed_references": [{"ref": "node:new-script", "role": "context"}],
            },
            "references": [{"ref": "node:new-script", "role": "context"}],
            "depends_on": ["node:new-script"],
        },
        [],
        replace_managed=True,
    )
    assert cleared["references"] == []
    assert cleared["depends_on"] == []


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
    character_image = next(
        step
        for step in normalized["steps"]
        if step["id"].endswith("character_image") and step["node_type"] == "image"
    )
    final_video = next(step for step in normalized["steps"] if step["id"].endswith("final_video"))

    assert "阿澈" in character_prompt["prompt_template"]
    assert "{{ steps.production_plan.output.style_template }}" in character_prompt["prompt_template"]
    assert "aspect_ratio" not in character_image["fields"]
    assert final_video["fields"]["duration_seconds"] == "9"
    assert "aspect_ratio" not in final_video["fields"]


def test_workflow_root_input_tokens_render_only_in_executable_fields() -> None:
    payload = _base_spec()
    payload["inputs"].update({
        "duration_seconds": {"type": "integer", "label": "时长", "default": 30},
        "missing": {"type": "text", "label": "缺省输入"},
    })
    payload["steps"].append({
        "id": "poster",
        "title": "海报",
        "kind": "image",
        "prompt": {"task": "制作 {{ inputs.duration_seconds }} 秒海报。"},
        "fields": {
            "duration_seconds": "{{ inputs.duration_seconds }}",
            "unresolved": "{{ inputs.missing }}",
        },
    })

    normalized = canvas_workflow_templates.normalize_inline_workflow(
        payload,
        input_values={"duration_seconds": 30},
    )
    by_id = {step["id"]: step for step in normalized["steps"]}
    assert by_id["poster"]["fields"] == {
        "duration_seconds": 30,
        "unresolved": "{{ inputs.missing }}",
        "workflow_source_step": "poster__prompt",
        "workflow_source_path": "output",
        "workflow_generate": True,
    }
    assert "{{ inputs.duration_seconds }}" in by_id["poster__prompt"]["prompt_template"]


def test_private_llm_phases_execute_even_though_they_are_not_public_nodes() -> None:
    template = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow",
        input_values={"plot": "雨夜相遇", "duration_seconds": 15, "episode_count": 1},
    )
    virtual = workflow_tools._virtual_workflow_step_ids(template["steps"], template["input_values"])

    assert "episode_plan" in virtual
    assert "script__generate" not in virtual
    assert next(step for step in template["steps"] if step["id"] == "script__generate")["surface"] == "workflow_runtime"


def test_conditional_feedback_loops_propagate_mode_to_expanded_children() -> None:
    public = canvas_workflow_templates.get_builtin_template(
        "general_short_drama_workflow"
    )["public_spec"]
    context = {
        "production_plan": {
            "output": {
                "main_characters": [],
                "segments": [{"segment_id": "s1", "duration_seconds": 15}],
            }
        }
    }

    storyboard = canvas_workflow_templates.normalize_inline_workflow(
        public,
        input_values={"visual_plan_mode": "storyboard", **context},
    )
    storyboard_steps = {
        step["id"]: step
        for step in storyboard["steps"]
        if "storyboard_review_loop" in step["id"]
    }
    story_template_steps = {
        step["id"]: step
        for step in storyboard["steps"]
        if "story_template_review_loop" in step["id"]
    }
    assert storyboard_steps
    assert story_template_steps
    assert {step["when"]["value"] for step in storyboard_steps.values()} == {"storyboard"}
    assert {step["when"]["value"] for step in story_template_steps.values()} == {"story_template"}

    virtual = workflow_tools._virtual_workflow_step_ids(
        storyboard["steps"], storyboard["input_values"]
    )
    assert not (set(storyboard_steps) & virtual)
    assert set(story_template_steps) <= virtual
    assert "segment_production_s1_final_video" not in virtual
    assert "segment_production_s1_final_video_story_template" in virtual


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
        "story_template",
        "final_video",
        "final_video_story_template",
    ]
    assert report["dry_run"]["leaf_visible_output_ids"] == [
        "final_video",
        "final_video_story_template",
    ]
    assert report["dry_run"]["final_output_ids"] == [
        "final_video",
        "final_video_story_template",
    ]
    assert report["dry_run"]["deferred_group_ids"] == ["character_images", "segment_production"]


def test_v2_audit_keeps_leaf_outputs_inside_nested_loops() -> None:
    payload = _base_spec()
    payload["steps"] = [
        {
            "id": "plan",
            "title": "课程规划",
            "kind": "object",
            "prompt": {"task": "输出模块和课时。"},
            "output": {
                "schema": {
                    "fields": [
                        {
                            "id": "modules",
                            "type": "array",
                            "fields": [
                                {"id": "module_id", "type": "string"},
                                {
                                    "id": "lessons",
                                    "type": "array",
                                    "fields": [{"id": "lesson_id", "type": "string"}],
                                },
                            ],
                        }
                    ]
                }
            },
        },
        {
            "id": "modules",
            "title": "逐模块",
            "kind": "loop",
            "foreach": {"items": "steps.plan.output.modules[]", "as": "module", "key": "module_id"},
            "steps": [
                {
                    "id": "lessons",
                    "title": "逐课时",
                    "kind": "loop",
                    "foreach": {"items": "module.lessons[]", "as": "lesson", "key": "lesson_id"},
                    "steps": [
                        {
                            "id": "lesson_image",
                            "title": "知识图",
                            "kind": "image",
                            "prompt": {"task": "生成 {{ lesson.lesson_id }} 的知识图。"},
                        },
                        {
                            "id": "lesson_video",
                            "title": "课时视频",
                            "kind": "video",
                            "needs": ["lesson_image"],
                            "prompt": {"task": "根据知识图生成视频。"},
                            "uses": [{"from": "lesson_image", "as": ["vision", "reference"]}],
                        },
                    ],
                }
            ],
        },
    ]

    report = audit_workflow_spec(payload)

    assert report["status"] == "pass"
    assert report["dry_run"]["visible_output_ids"] == ["lesson_image", "lesson_video"]
    assert report["dry_run"]["leaf_visible_output_ids"] == ["lesson_video"]

    projection = workflow_canvas_projection.project_workflow_canvas(
        project_id="nested-loop-projection",
        workflow=payload,
        context={
            "plan": {
                "output": {
                    "modules": [
                        {
                            "module_id": "m1",
                            "lessons": [
                                {"lesson_id": "l1"},
                                {"lesson_id": "l2"},
                            ],
                        }
                    ]
                }
            }
        },
    )

    assert projection["dynamic_inputs"]["status"] == "ready"
    assert [item["id"] for item in projection["canvas"]["final_outputs"]] == [
        "modules_m1_lessons_l1_lesson_video",
        "modules_m1_lessons_l2_lesson_video",
    ]


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
    assert not any("story_template" in step_id for step_id in canvas_ids)
    assert [node["id"] for node in result["canvas"]["final_outputs"]] == [
        "segment_production_s1_final_video",
        "segment_production_s2_final_video",
    ]
    flow_by_id = {node["id"]: node for node in result["flow"]["nodes"]}
    for segment_id, other_segment_id in (("s1", "s2"), ("s2", "s1")):
        storyboard_id = (
            f"segment_production_{segment_id}_storyboard_review_loop_i1_storyboard"
        )
        review_id = f"{storyboard_id}_review"
        final_video_id = f"segment_production_{segment_id}_final_video"
        own_scene_id = f"segment_production_{segment_id}_scene_reference"

        assert own_scene_id in flow_by_id[storyboard_id]["references"]
        assert not any(
            f"segment_production_{other_segment_id}_" in ref
            for ref in flow_by_id[storyboard_id]["references"]
        )
        assert flow_by_id[review_id]["references"] == [storyboard_id]
        assert storyboard_id in flow_by_id[final_video_id]["references"]
        assert own_scene_id in flow_by_id[final_video_id]["references"]
        assert not any(
            f"segment_production_{other_segment_id}_" in ref
            for ref in flow_by_id[final_video_id]["references"]
        )

    retry = workflow_canvas_projection.project_workflow_canvas(
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
            },
            "segment_production_s1_storyboard_review_loop_i1_storyboard_review": {
                "status": "completed",
                "output": {"score": 60},
            },
        },
    )
    retry_flow = {node["id"]: node for node in retry["flow"]["nodes"]}
    assert retry_flow["segment_production_s1_final_video"]["references"] == [
        "segment_production_s1_frame_plan",
        "segment_production_s1_storyboard_review_loop_i2_storyboard",
        "segment_production_s1_scene_reference",
    ]
    assert retry_flow["segment_production_s2_final_video"]["references"] == [
        "segment_production_s2_frame_plan",
        "segment_production_s2_storyboard_review_loop_i1_storyboard",
        "segment_production_s2_scene_reference",
    ]

    story_template_result = workflow_canvas_projection.project_workflow_canvas(
        project_id="projection-test",
        workflow=public,
        inputs={
            "plot": "雨夜天台收到未来来信",
            "duration_seconds": 15,
            "segment_seconds": 15,
            "visual_plan_mode": "story_template",
        },
        context={
            "production_plan": {
                "output": {
                    "main_characters": [{"character_id": "hero", "name": "林岚"}],
                    "segments": [{"segment_id": "s1", "duration_seconds": 15}],
                }
            }
        },
    )
    story_canvas_ids = [node["id"] for node in story_template_result["canvas"]["nodes"]]
    story_template_id = (
        "segment_production_s1_story_template_review_loop_i1_story_template"
    )
    story_final_id = "segment_production_s1_final_video_story_template"
    assert story_template_id in story_canvas_ids
    assert story_final_id in story_canvas_ids
    assert not any("storyboard_review_loop" in step_id for step_id in story_canvas_ids)
    assert [node["id"] for node in story_template_result["canvas"]["final_outputs"]] == [
        story_final_id
    ]
    story_flow = {node["id"]: node for node in story_template_result["flow"]["nodes"]}
    assert story_template_id in story_flow[story_final_id]["references"]
    assert "segment_production_s1_scene_reference" in story_flow[story_final_id]["references"]


def test_workflow_build_guide_documents_v2_high_frequency_errors() -> None:
    assert "openreel.workflow.v2" in WORKFLOW_SPEC_V2_GUIDE
    assert "Use `text`, never `string`" in WORKFLOW_SPEC_V2_GUIDE
    assert "Input types are exactly" in WORKFLOW_SPEC_V2_GUIDE
    assert "`inputs` is an object map keyed by input id, never an array" in WORKFLOW_SPEC_V2_GUIDE
    assert "vision" in WORKFLOW_SPEC_V2_GUIDE
    assert "reference" in WORKFLOW_SPEC_V2_GUIDE
    assert "select.values" in WORKFLOW_SPEC_V2_GUIDE
    assert "Values use a scoped path" in WORKFLOW_SPEC_V2_GUIDE
    assert "first emit it from an object/collection step inside that loop" in WORKFLOW_SPEC_V2_GUIDE
    assert "Direct media adoption" in WORKFLOW_SPEC_V2_GUIDE
    assert "Do not create prompt sibling steps" in WORKFLOW_SPEC_V2_GUIDE
    assert "provider/model routing" in WORKFLOW_SPEC_V2_GUIDE
    assert "aspect ratio, resolution, width/height, quality, fps" in WORKFLOW_SPEC_V2_GUIDE
    assert "Frontend supplies media settings" in WORKFLOW_SPEC_V2_GUIDE
    assert "Put media settings in `fields`" not in WORKFLOW_SPEC_V2_GUIDE
    assert "foreach.until" in WORKFLOW_SPEC_V2_GUIDE
    assert "{{ previous }}" in WORKFLOW_SPEC_V2_GUIDE
    assert "terminal: no sibling may depend on it" in WORKFLOW_SPEC_V2_GUIDE
    assert 'uses:[{"from":"candidate","as":["vision"]}]' in WORKFLOW_SPEC_V2_GUIDE
    assert "workflow_loop_until_exhausted" in WORKFLOW_SPEC_V2_GUIDE
    assert "Downstream steps depend on `quality_loop`" in WORKFLOW_SPEC_V2_GUIDE
    assert "same shared parent item" in WORKFLOW_SPEC_V2_GUIDE
    assert "Projection and runtime must agree" in WORKFLOW_SPEC_V2_GUIDE
    assert "openreel.workflow.authoring.v1" not in WORKFLOW_SPEC_V2_GUIDE


def test_workflow_build_guide_example_compiles_with_scoped_dynamic_selection() -> None:
    plan = compile_workflow_spec(WORKFLOW_SPEC_V2_EXAMPLE)
    by_id = {
        step["id"]: step
        for root in plan["steps"]
        for step in ([root] if root.get("kind") != "loop" else [root, *(root.get("steps") or [])])
    }

    assert by_id["storyboard"]["uses"][0]["select"] == {
        "values": "steps.shot_context.output.selected_character_ids",
        "by": ["character_id"],
    }


def test_user_template_file_stays_a_plain_portable_v2_document(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    workflow = _base_spec()
    workflow["inputs"]["aspect_ratio"] = {"type": "text", "label": "画幅", "default": "9:16"}
    workflow["steps"].append({
        "id": "poster",
        "title": "海报",
        "kind": "image",
        "prompt": {"task": "生成海报。"},
        "fields": {
            "purpose": "poster",
            "aspect_ratio": "{{ inputs.aspect_ratio }}",
            "resolution": "1440x2560",
            "width": 1440,
            "height": 2560,
            "quality": "high",
        },
    })
    saved = workflow_template_store.save_user_template(
        workflow=workflow,
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
    assert "aspect_ratio" not in stored["inputs"]
    assert stored["steps"][1]["fields"] == {"purpose": "poster"}


def test_legacy_user_template_is_canonicalized_before_model_read_or_export(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    workflow = _base_spec()
    workflow["inputs"]["resolution"] = {"type": "text", "label": "分辨率", "default": "2k"}
    workflow["steps"].append({
        "id": "poster",
        "title": "海报",
        "kind": "image",
        "prompt": {"task": "生成海报。"},
        "fields": {"purpose": "poster", "resolution": "2560x1440", "quality": "high"},
    })
    root = tmp_path / "workflow_templates" / "user"
    root.mkdir(parents=True)
    (root / "video_flow.json").write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")

    loaded = workflow_template_store.load_user_template("video_flow")
    exported = workflow_template_store.export_template_package("video_flow")

    assert "resolution" not in loaded["workflow"]["inputs"]
    assert loaded["workflow"]["steps"][1]["fields"] == {"purpose": "poster"}
    assert exported["workflow"] == loaded["workflow"]
    assert exported["version"]["workflow"] == loaded["workflow"]


def test_workflow_artifact_persists_only_canonical_public_spec(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    workflow = _base_spec()
    workflow["inputs"]["quality"] = {"type": "text", "label": "画质", "default": "high"}
    workflow["steps"].append({
        "id": "poster",
        "title": "海报",
        "kind": "image",
        "prompt": {"task": "生成海报。"},
        "fields": {"purpose": "poster", "resolution": "2560x1440", "quality": "high"},
    })

    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
        project_id="project-1",
        workflow=workflow,
    )
    loaded = workflow_spec_artifacts.load_workflow_spec_artifact("project-1", saved["artifact_ref"])

    assert "quality" not in loaded["workflow"]["inputs"]
    assert loaded["workflow"]["steps"][1]["fields"] == {"purpose": "poster"}


@pytest.mark.asyncio
async def test_workflow_template_read_returns_public_v2_instead_of_private_execution_plan() -> None:
    result = await workflow_tools.workflow_template_read(
        project_id="project-1",
        template_id="general_short_drama_workflow",
        detail="workflow",
    )

    assert result["ok"] is True
    assert result["workflow"]["schema"] == WORKFLOW_SPEC_VERSION
    assert "public_spec" not in result["workflow"]
    assert "plan_hash" not in result["workflow"]
    assert "node_type" not in str(result["workflow"])


def test_invalid_user_override_does_not_hide_runnable_builtin_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin = {"id": "general_short_drama_workflow", "scope": "builtin", "name": "内置流程"}
    user = {"id": "general_short_drama_workflow", "scope": "user", "name": "旧用户覆盖"}
    record = {
        "summary": {"id": "general_short_drama_workflow"},
        "version": {"audit": {"can_run": False}},
    }
    monkeypatch.setattr(canvas_workflow_templates, "load_builtin_templates", lambda input_values=None: [builtin])
    monkeypatch.setattr(canvas_workflow_templates, "load_user_templates", lambda input_values=None: [user])
    monkeypatch.setattr(workflow_template_store, "list_user_template_records", lambda: [record])

    templates = canvas_workflow_templates.load_templates()

    assert templates == [builtin]


@pytest.mark.asyncio
async def test_builtin_run_authorization_ignores_invalid_same_id_user_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_spec = {"schema": WORKFLOW_SPEC_VERSION, "id": "general_short_drama_workflow", "steps": []}
    normalized_template = {
        "id": "general_short_drama_workflow",
        "scope": "builtin",
        "public_spec": public_spec,
        "steps": [],
    }
    audit_calls: list[tuple[dict, dict, dict]] = []

    def audit_builtin(raw_workflow: dict, *, normalized: dict, sample_inputs: dict):
        audit_calls.append((raw_workflow, normalized, sample_inputs))
        return {"can_run": True, "status": "pass"}

    monkeypatch.setattr(
        workflow_tools,
        "audit_workflow_spec",
        audit_builtin,
    )

    def fail_if_user_override_is_loaded(_template_id: str):
        raise AssertionError("builtin authorization must not load a same-id user override")

    monkeypatch.setattr(workflow_template_store, "load_user_template", fail_if_user_override_is_loaded)

    error = await workflow_tools._authorize_workflow_for_run(
        project_id="project-1",
        template=normalized_template,
        template_id="general_short_drama_workflow",
        inputs={"plot": "test plot", "duration_seconds": 15},
    )

    assert error is None
    assert audit_calls == [
        (public_spec, normalized_template, {"plot": "test plot", "duration_seconds": 15})
    ]


def test_default_unnamed_label_does_not_overwrite_workflow_title(tmp_path) -> None:
    saved = workflow_template_store.save_user_template(
        workflow=_base_spec(),
        template_id="video_flow",
        name="未命名流程",
        replace_existing=True,
    )
    stored = json.loads(
        (tmp_path / "workflow_templates" / "user" / "video_flow.json").read_text(encoding="utf-8")
    )

    assert saved["summary"]["name"] == "视频流程"
    assert stored["title"] == "视频流程"


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


def test_resolving_public_step_for_rerun_includes_all_private_phases() -> None:
    template = {
        "steps": [
            {
                "id": "storyboard__prompt",
                "logical_step_id": "storyboard",
                "repeat_group_id": "segments",
                "repeat_group_index": 1,
            },
            {
                "id": "storyboard",
                "logical_step_id": "storyboard",
                "repeat_group_id": "segments",
                "repeat_group_index": 1,
            },
            {
                "id": "storyboard__prompt_2",
                "logical_step_id": "storyboard",
                "repeat_group_id": "segments",
                "repeat_group_index": 2,
            },
        ]
    }

    resolved = workflow_tools._resolve_workflow_target_steps(template, "storyboard")

    assert [step["id"] for step in resolved] == ["storyboard__prompt", "storyboard"]


@pytest.mark.asyncio
async def test_manual_rerun_resets_only_descendants_and_marks_capsule_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = {
        "id": "resume_flow",
        "steps": [
            {"id": "script__generate", "logical_step_id": "script", "depends_on": []},
            {"id": "script", "logical_step_id": "script", "depends_on": ["script__generate"]},
            {"id": "scene", "logical_step_id": "scene", "depends_on": ["script"]},
            {"id": "video", "logical_step_id": "video", "depends_on": ["scene"]},
            {"id": "unrelated", "logical_step_id": "unrelated", "depends_on": []},
        ],
    }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_resume": {
                    "instance_id": "wf_resume",
                    "template_id": "resume_flow",
                    "status": "completed",
                    "steps": {
                        step_id: {
                            "id": step_id,
                            "step_id": step_id,
                            "status": "completed",
                            "run_count": 1,
                            "output": {"content": f"old {step_id}"},
                            "workflow": {
                                "step_id": step_id,
                                "logical_step_id": step_id.removesuffix("__generate"),
                                "depends_on": next(
                                    step["depends_on"] for step in template["steps"] if step["id"] == step_id
                                ),
                            },
                        }
                        for step_id in ("script__generate", "script", "scene", "video", "unrelated")
                    },
                }
            }
        }
    }

    async def read_state(_project_id: str) -> dict:
        return state

    async def write_patch(_project_id: str, patch: dict) -> None:
        state.update(patch)

    async def emit_update(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", write_patch)
    monkeypatch.setattr(workflow_tools, "_emit_workflow_runtime_update", emit_update)
    targets = workflow_tools._resolve_workflow_target_steps(template, "script")

    await workflow_tools._prepare_workflow_runtime_manual_rerun(
        project_id="project-1",
        template=template,
        instance_id="wf_resume",
        target_steps=targets,
        requested_step_id="script",
    )

    instance = state["workflow_runtime"]["instances"]["wf_resume"]
    assert instance["status"] == "partial"
    assert instance["last_rerun_step_id"] == "script"
    assert instance["steps"]["script__generate"]["status"] == "completed"
    assert instance["steps"]["script"]["status"] == "completed"
    assert instance["steps"]["unrelated"]["status"] == "completed"
    for step_id in ("scene", "video"):
        assert instance["steps"][step_id]["status"] == "idle"
        assert instance["steps"][step_id]["stale"] is True
        assert instance["steps"][step_id]["invalidated_by"] == "script"
        assert instance["steps"][step_id]["output"] == {"content": f"old {step_id}"}
        assert workflow_tools._workflow_step_needs_run_for_batch(
            next(step for step in template["steps"] if step["id"] == step_id),
            instance["steps"][step_id],
            failed_step_ids=set(),
        ) is True


def test_resume_selection_retries_failed_step_without_rerunning_completed_prefix() -> None:
    completed = {"status": "completed", "stale": False}
    failed = {"status": "failed", "stale": False}

    assert workflow_tools._workflow_step_needs_run_for_batch(
        {"id": "script"}, completed, failed_step_ids=set()
    ) is False
    assert workflow_tools._workflow_step_needs_run_for_batch(
        {"id": "image"}, failed, failed_step_ids=set()
    ) is True
    assert workflow_tools._workflow_step_needs_run_for_batch(
        {"id": "image"}, failed, failed_step_ids={"image"}
    ) is False


@pytest.mark.asyncio
async def test_ready_batch_after_failure_resumes_at_failed_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = {
        "id": "resume_flow",
        "steps": [
            {"id": "script", "depends_on": []},
            {"id": "image", "depends_on": ["script"]},
            {"id": "video", "depends_on": ["image"]},
        ],
    }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_resume": {
                    "instance_id": "wf_resume",
                    "template_id": "resume_flow",
                    "status": "failed",
                    "steps": {
                        "script": {"status": "completed", "workflow": {"depends_on": []}},
                        "image": {"status": "failed", "workflow": {"depends_on": ["script"]}},
                        "video": {"status": "idle", "workflow": {"depends_on": ["image"]}},
                    },
                }
            }
        }
    }

    async def read_state(_project_id: str) -> dict:
        return state

    async def no_context(*_args: object, **_kwargs: object) -> dict:
        return {}

    async def no_settle(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_context_from_project", no_context)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_settle_terminal_running_steps_for_run", no_settle)

    batch = await workflow_tools._workflow_ready_step_batch(
        project_id="project-1",
        template=template,
        template_id="resume_flow",
        instance_id="wf_resume",
        failed_step_ids=set(),
    )

    assert batch["ready_step_ids"] == ["image"]
    assert batch["blocked_steps"] == [{"step_id": "video", "waiting_on": ["image"]}]


@pytest.mark.asyncio
async def test_single_step_run_updates_capsule_status_and_clears_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_resume": {
                    "instance_id": "wf_resume",
                    "template_id": "resume_flow",
                    "status": "partial",
                    "steps": {
                        "image": {
                            "status": "idle",
                            "stale": True,
                            "invalidated_by": "script",
                            "invalidated_at": "old",
                        }
                    },
                }
            }
        }
    }

    async def read_state(_project_id: str) -> dict:
        return state

    async def write_patch(_project_id: str, patch: dict) -> None:
        state.update(patch)

    async def emit_update(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", write_patch)
    monkeypatch.setattr(workflow_tools, "_emit_workflow_runtime_update", emit_update)
    template = {"id": "resume_flow", "name": "续跑", "steps": [{"id": "image", "depends_on": []}]}

    await workflow_tools._upsert_workflow_runtime_step(
        project_id="project-1",
        template=template,
        instance_id="wf_resume",
        step_id="image",
        node_type="image",
        title="图片",
        fields={"workflow": {"step_id": "image"}},
        status="running",
        increment_run=True,
    )
    instance = state["workflow_runtime"]["instances"]["wf_resume"]
    assert instance["status"] == "running"
    assert "invalidated_by" not in instance["steps"]["image"]
    assert "invalidated_at" not in instance["steps"]["image"]

    await workflow_tools._upsert_workflow_runtime_step(
        project_id="project-1",
        template=template,
        instance_id="wf_resume",
        step_id="image",
        node_type="image",
        title="图片",
        fields={"workflow": {"step_id": "image"}},
        status="failed",
        error="provider failed",
    )
    assert instance["status"] == "failed"
    assert instance["steps"]["image"]["last_error"] == "provider failed"


@pytest.mark.asyncio
async def test_manual_step_completion_accepts_uploaded_media_and_completes_private_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = {
        "id": "manual-flow",
        "name": "人工验收流程",
        "steps": [
            {
                "id": "image__prompt",
                "title": "图片 · 提示词",
                "node_type": "text",
                "surface": "workflow_runtime",
                "logical_step_id": "image",
                "depends_on": [],
            },
            {
                "id": "image",
                "title": "图片",
                "node_type": "image",
                "surface": "draft_canvas",
                "logical_step_id": "image",
                "depends_on": ["image__prompt"],
            },
        ],
    }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_manual": {
                    "instance_id": "wf_manual",
                    "template_id": "manual-flow",
                    "template_name": "人工验收流程",
                    "status": "failed",
                    "steps": {
                        "image__prompt": {
                            "status": "failed",
                            "type": "text",
                            "surface": "workflow_runtime",
                            "run_count": 1,
                            "error": "prompt failed",
                            "output": {"error": "prompt failed"},
                            "input": {"workflow": {"step_id": "image__prompt", "logical_step_id": "image"}},
                            "workflow": {"step_id": "image__prompt", "logical_step_id": "image"},
                        },
                        "image": {
                            "status": "failed",
                            "type": "image",
                            "surface": "draft_canvas",
                            "run_count": 1,
                            "error": "provider failed",
                            "node_id": "node-image",
                            "artifacts": [{"node_id": "node-image", "type": "image"}],
                            "input": {"workflow": {"step_id": "image", "logical_step_id": "image"}},
                            "workflow": {"step_id": "image", "logical_step_id": "image"},
                        },
                    },
                }
            }
        }
    }
    uploaded_node = {
        "id": "node-image",
        "project_id": "project-1",
        "type": "image",
        "title": "人物图",
        "status": "completed",
        "input": {
            "workflow": {
                "template_id": "manual-flow",
                "instance_id": "wf_manual",
                "step_id": "image",
                "logical_step_id": "image",
                "surface": "draft_canvas",
            }
        },
        "output": {
            "status": "completed",
            "image": {"url": "/api/media/project-1/generated_images/uploads/manual.png"},
        },
    }
    node_patches: list[dict] = []

    async def read_state(_project_id: str) -> dict:
        return state

    async def write_patch(_project_id: str, patch: dict) -> None:
        state.update(patch)

    def load_template(_state: dict, *, template_id: str, instance_id: str) -> tuple[dict, dict]:
        assert template_id == "manual-flow"
        assert instance_id == "wf_manual"
        return template, {}

    async def get_node(node_id: str) -> dict:
        assert node_id == "node-image"
        return deepcopy(uploaded_node)

    async def update_node(node_id: str, patch: dict) -> dict:
        assert node_id == "node-image"
        node_patches.append(deepcopy(patch))
        return {"id": node_id, "status": patch.get("status")}

    async def no_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", write_patch)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_template_for_state", load_template)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", update_node)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", no_emit)
    monkeypatch.setattr(workflow_tools, "_emit_workflow_runtime_update", no_emit)

    result = await workflow_tools.workflow_runtime_complete_step_manually(
        "project-1",
        "wf_manual",
        "image",
        template_id="manual-flow",
        node_id="node-image",
    )

    assert result["ok"] is True
    assert result["completed_step_ids"] == ["image__prompt", "image"]
    instance = state["workflow_runtime"]["instances"]["wf_manual"]
    assert "status" not in instance
    assert instance["steps"]["image__prompt"]["status"] == "completed"
    assert instance["steps"]["image__prompt"].get("output") in (None, {})
    assert instance["steps"]["image"]["status"] == "completed"
    assert instance["steps"]["image"]["output"] == uploaded_node["output"]
    assert instance["steps"]["image"]["workflow"]["manual_completion"]["source"] == "user"
    assert result["runtime"]["steps"][0]["status"] == "completed"
    assert result["runtime"]["steps"][0]["manual_completion"]["source"] == "user"
    assert node_patches[-1]["status"] == "completed"
    assert node_patches[-1]["error_message"] is None


@pytest.mark.asyncio
async def test_manual_media_step_completion_rejects_missing_usable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = {
        "id": "manual-flow",
        "steps": [
            {
                "id": "image",
                "title": "图片",
                "node_type": "image",
                "surface": "draft_canvas",
                "depends_on": [],
            }
        ],
    }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_manual": {
                    "template_id": "manual-flow",
                    "status": "failed",
                    "steps": {
                        "image": {
                            "status": "failed",
                            "type": "image",
                            "surface": "draft_canvas",
                            "node_id": "node-image",
                            "input": {"workflow": {"step_id": "image"}},
                            "workflow": {"step_id": "image"},
                        }
                    },
                }
            }
        }
    }

    async def read_state(_project_id: str) -> dict:
        return state

    def load_template(_state: dict, *, template_id: str, instance_id: str) -> tuple[dict, dict]:
        return template, {}

    async def get_node(_node_id: str) -> dict:
        return {
            "id": "node-image",
            "project_id": "project-1",
            "type": "image",
            "status": "failed",
            "input": {"workflow": {"template_id": "manual-flow", "instance_id": "wf_manual", "step_id": "image"}},
            "output": {"status": "failed", "error": "provider failed"},
        }

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_template_for_state", load_template)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", get_node)

    result = await workflow_tools.workflow_runtime_complete_step_manually(
        "project-1",
        "wf_manual",
        "image",
        template_id="manual-flow",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "workflow_manual_completion_requires_output"
    assert state["workflow_runtime"]["instances"]["wf_manual"]["steps"]["image"]["status"] == "failed"


@pytest.mark.asyncio
async def test_manual_media_step_completion_restores_latest_successful_history_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = {
        "id": "manual-flow",
        "steps": [
            {
                "id": "image",
                "title": "图片",
                "node_type": "image",
                "surface": "draft_canvas",
                "depends_on": [],
            }
        ],
    }
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_manual": {
                    "template_id": "manual-flow",
                    "status": "failed",
                    "steps": {
                        "image": {
                            "status": "failed",
                            "type": "image",
                            "surface": "draft_canvas",
                            "node_id": "node-image",
                            "input": {"workflow": {"step_id": "image"}},
                            "workflow": {"step_id": "image"},
                        }
                    },
                }
            }
        }
    }
    successful_output = {
        "type": "fusion",
        "stages": [
            {
                "name": "图片",
                "status": "completed",
                "url": "/api/media/project-1/success.png",
            }
        ],
    }
    failed_output = {
        "type": "fusion",
        "stages": [
            {
                "name": "图片",
                "status": "failed",
                "url": "/api/media/project-1/success.png",
                "error": "provider failed",
            }
        ],
        "history": [
            {
                "id": "hist-success",
                "created_at": "2026-07-14T12:00:00Z",
                "type": "image",
                "output": successful_output,
            }
        ],
    }
    failed_node = {
        "id": "node-image",
        "project_id": "project-1",
        "type": "image",
        "title": "人物图",
        "status": "failed",
        "error_message": "provider failed",
        "input": {
            "workflow": {
                "template_id": "manual-flow",
                "instance_id": "wf_manual",
                "step_id": "image",
                "surface": "draft_canvas",
            }
        },
        "output": failed_output,
    }
    node_patches: list[dict] = []

    async def read_state(_project_id: str) -> dict:
        return state

    async def write_patch(_project_id: str, patch: dict) -> None:
        state.update(patch)

    def load_template(_state: dict, *, template_id: str, instance_id: str) -> tuple[dict, dict]:
        return template, {}

    async def get_node(_node_id: str) -> dict:
        return deepcopy(failed_node)

    async def update_node(_node_id: str, patch: dict) -> dict:
        node_patches.append(deepcopy(patch))
        return {"id": "node-image", "status": patch.get("status")}

    async def no_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_tools, "_read_project_state", read_state)
    monkeypatch.setattr(workflow_tools, "_write_project_state_patch", write_patch)
    monkeypatch.setattr(workflow_tools, "_workflow_runtime_template_for_state", load_template)
    monkeypatch.setattr(workflow_tools.canvas_tools, "get_node", get_node)
    monkeypatch.setattr(workflow_tools.canvas_tools, "update_node", update_node)
    monkeypatch.setattr(workflow_tools, "_emit_canvas_action", no_emit)
    monkeypatch.setattr(workflow_tools, "_emit_workflow_runtime_update", no_emit)

    result = await workflow_tools.workflow_runtime_complete_step_manually(
        "project-1",
        "wf_manual",
        "image",
        template_id="manual-flow",
        node_id="node-image",
    )

    assert result["ok"] is True
    assert result["manual_completion"]["activated_history"] is True
    assert result["manual_completion"]["history_id"] == "hist-success"
    assert node_patches[-1]["status"] == "completed"
    assert node_patches[-1]["error_message"] is None
    assert node_patches[-1]["output_data"]["stages"][0]["status"] == "completed"
    runtime_output = state["workflow_runtime"]["instances"]["wf_manual"]["steps"]["image"]["output"]
    assert runtime_output["stages"][0]["url"] == "/api/media/project-1/success.png"
    assert workflow_tools.media_history.is_successful_media_output(runtime_output) is True


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
