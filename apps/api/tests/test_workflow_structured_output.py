import pytest

from app.agent import canvas_workflow_templates
from app.agent.workflow_authoring_spec import compile_authoring_workflow
from app.agent.workflow_spec_prompt_contract import AUTHORING_SPEC_EXAMPLE, AUTHORING_SPEC_GUIDE
from app.agent.workflow_structured_output import (
    WorkflowStructuredOutputError,
    parse_structured_output,
    structured_output_instructions,
)


def test_authoring_prompt_contract_example_compiles_to_repeated_video_workflow() -> None:
    compiled = compile_authoring_workflow(AUTHORING_SPEC_EXAMPLE)

    assert "Canonical example" in AUTHORING_SPEC_GUIDE
    assert "Generated visible text is kind text with visible:true" in AUTHORING_SPEC_GUIDE
    assert "Put the prompt on the media step" in AUTHORING_SPEC_GUIDE
    assert "If a prompt relies on another generated product" in AUTHORING_SPEC_GUIDE
    assert "put the repeat group id in needs" in AUTHORING_SPEC_GUIDE
    assert "Use literal numbers or real input refs" in AUTHORING_SPEC_GUIDE
    assert compiled["workflow_spec_version"] == "openreel.workflow.v1"
    assert compiled["authoring_spec_version"] == "openreel.workflow.authoring.v1"
    assert compiled["required_inputs"] == ["plot", "durationSeconds"]

    by_id = {step["id"]: step for step in compiled["steps"]}
    assert by_id["full_script"]["surface"] == "draft_canvas"
    assert by_id["segments"]["kind"] == "collection"
    assert by_id["segments"]["output_schema"]["type"] == "collection"
    assert by_id["segments"]["output_schema"]["fields"][0]["id"] == "segment_text"
    assert compiled["dimensions"]["segments"] == {
        "source": "steps.segments.output.items",
        "scope_key": "segment",
    }

    group = by_id["segment_loop"]
    assert group["role"] == "repeat_group"
    assert group["repeat"]["foreach"] == {
        "dimension": "segments",
        "scope_key": "segment",
    }
    child_ids = [step["id"] for step in group["steps"]]
    assert child_ids == ["segment_script", "segment_video_prompt", "segment_video"]
    assert group["steps"][0]["surface"] == "draft_canvas"
    assert group["steps"][1]["surface"] == "workflow_runtime"
    assert group["steps"][2]["runner"] == "workflow_canvas_output"
    assert group["steps"][2]["fields"]["duration_seconds"] == "{{segment.duration_seconds}}"

    segment_output = {
        "items": [
            {"id": "s1", "segment_text": "第一段", "duration_seconds": 15, "visual_notes": "雨夜"},
            {"id": "s2", "segment_text": "第二段", "duration_seconds": 15, "visual_notes": "霓虹"},
        ]
    }
    input_values = {
        "steps": {"segments": {"output": segment_output}},
        "context": {"segments": {"output": segment_output}},
        "outputs": {"segments": segment_output},
    }
    expanded = canvas_workflow_templates.normalize_inline_workflow(
        AUTHORING_SPEC_EXAMPLE,
        input_values=input_values,
    )
    assert expanded["deferred_groups"] == []
    assert [step["id"] for step in expanded["steps"]] == [
        "full_script",
        "segments",
        "segment_loop_s1_segment_script",
        "segment_loop_s1_segment_video_prompt",
        "segment_loop_s1_segment_video",
        "segment_loop_s2_segment_script",
        "segment_loop_s2_segment_video_prompt",
        "segment_loop_s2_segment_video",
    ]


def test_collection_structured_output_contract_is_generated_from_schema() -> None:
    workflow = {
        "output_mode": "json",
        "output_schema": {
            "type": "collection",
            "items_key": "items",
            "fields": [
                {"id": "name", "label": "名称", "type": "string", "required": True},
                {"id": "notes", "label": "说明", "type": "string"},
            ],
        },
    }

    instructions = structured_output_instructions(workflow)

    assert "top-level array field named \"items\"" in instructions
    assert "name (名称): string, required" in instructions
    assert "notes (说明): string, optional" in instructions
    assert "Do not wrap it in Markdown" in instructions


def test_collection_structured_output_parses_model_list_key_to_items() -> None:
    workflow = {
        "output_mode": "json",
        "output_schema": {
            "type": "collection",
            "fields": [
                {"id": "name", "label": "名称", "required": True},
                {"id": "description", "label": "说明"},
            ],
        },
    }

    parsed = parse_structured_output(
        '{"characters":[{"name":"林舟","description":"学生"},{"name":"云隙信使"}]}',
        workflow,
    )

    assert parsed["items"] == [
        {"name": "林舟", "description": "学生"},
        {"name": "云隙信使"},
    ]


def test_collection_structured_output_rejects_missing_required_field() -> None:
    workflow = {
        "output_mode": "json",
        "output_schema": {
            "type": "collection",
            "fields": [{"id": "name", "required": True}],
        },
    }

    with pytest.raises(WorkflowStructuredOutputError):
        parse_structured_output('{"items":[{"description":"缺名称"}]}', workflow)


def test_authoring_collection_step_compiles_to_generic_structured_collection() -> None:
    compiled = compile_authoring_workflow({
        "schema": "openreel.workflow.authoring.v1",
        "id": "collection_demo",
        "name": "集合测试",
        "steps": [
            {
                "id": "extract_items",
                "title": "提取集合",
                "kind": "collection",
                "prompt": {"task": "从上文提取需要处理的对象。"},
                "output_schema": {
                    "type": "object",
                    "fields": [
                        {"id": "name", "label": "名称", "required": True},
                        {"id": "reason", "label": "原因"},
                    ],
                },
            }
        ],
    })

    step = compiled["steps"][0]
    assert step["kind"] == "collection"
    assert step["output_mode"] == "json"
    assert step["output_schema"]["type"] == "collection"
    assert step["output_schema"]["items_key"] == "items"
    assert step["collection"]["kind"] == "llm_extracted_items"


def test_authoring_compiler_accepts_model_friendly_list_and_repeat_aliases() -> None:
    compiled = compile_authoring_workflow({
        "schema": "openreel.workflow.authoring.v1",
        "id": "alias_demo",
        "name": "别名测试",
        "inputs": [
            {"id": "plot", "type": "long_text", "required": True},
            {"id": "durationSeconds", "type": "number", "default": 30},
            {"id": "segmentSeconds", "type": "number", "default": 15},
        ],
        "steps": [
            {
                "id": "segments",
                "title": "分段清单",
                "type": "list",
                "prompt_template": "按时长切分剧情。",
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
                        "id": "segment_video",
                        "title": "本段视频",
                        "type": "video",
                        "depends_on": ["segments"],
                        "prompt_template": "根据 {{segment.segment_text}} 生成视频。",
                    }
                ],
            },
        ],
    })

    by_id = {step["id"]: step for step in compiled["steps"]}
    assert by_id["segments"]["kind"] == "collection"
    assert by_id["segments"]["output_mode"] == "json"
    assert by_id["segments"]["output_schema"]["type"] == "collection"
    assert by_id["segments"]["output_schema"]["fields"][0]["id"] == "segment_text"
    group = by_id["segment_loop"]
    assert group["role"] == "repeat_group"
    assert group["repeat"]["foreach"] == {
        "from": "steps",
        "path": "segments.output",
        "scope_key": "segment",
    }
    child_ids = [step["id"] for step in group["steps"]]
    assert child_ids == ["segment_video_prompt", "segment_video"]
    assert group["steps"][0]["surface"] == "workflow_runtime"
    assert group["steps"][1]["runner"] == "workflow_canvas_output"


def test_authoring_canvas_product_steps_compile_without_canvas_flags() -> None:
    compiled = compile_authoring_workflow({
        "schema": "openreel.workflow.authoring.v1",
        "id": "canvas_product_demo",
        "name": "画布产物测试",
        "steps": [
            {"id": "draft_prompt", "title": "写提示词", "kind": "text"},
            {"id": "script_card", "title": "剧本节点", "kind": "canvas_text", "needs": ["draft_prompt"]},
            {
                "id": "cover",
                "title": "封面图",
                "kind": "image",
                "needs": ["draft_prompt"],
                "output": {"canvas": False, "show_on_canvas": False, "type": "image"},
            },
        ],
    })

    by_id = {step["id"]: step for step in compiled["steps"]}
    assert by_id["draft_prompt"]["surface"] == "workflow_runtime"
    assert by_id["draft_prompt"]["runner"] == "node.run"
    assert by_id["script_card"]["surface"] == "draft_canvas"
    assert by_id["script_card"]["node_type"] == "text"
    assert by_id["script_card"]["runner"] == "workflow_canvas_output"
    assert by_id["cover"]["surface"] == "draft_canvas"
    assert by_id["cover"]["runner"] == "workflow_canvas_output"
    assert "canvas" not in by_id["cover"].get("output", {})
    assert "show_on_canvas" not in by_id["cover"].get("output", {})
