from __future__ import annotations

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
