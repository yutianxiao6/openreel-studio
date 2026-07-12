from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.workflow_execution_plan import compile_private_execution_template
from app.agent.workflow_spec import WorkflowSpecError, compile_workflow_spec
from app.config import settings
from app.mcp_tools import workflow_tools
from app.services import workflow_plugins


def _write_plugin(root: Path, *, plugin_id: str = "test.echo") -> None:
    plugin_dir = root / "plugins" / "echo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": "回声插件",
                "version": "1.0.0",
                "category": "text",
                "nodes": [
                    {
                        "type": "echo",
                        "title": "回声",
                        "inputs": [{"id": "text", "label": "文本", "type": "text"}],
                        "outputs": [{"id": "saved", "label": "保存结果", "type": "text"}],
                        "runtime": {"kind": "python", "entrypoint": "main:run"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        """
async def run(ctx, inputs, settings):
    saved = await ctx.save_text(inputs.get("text") or "hello", kind="echo")
    ctx.log("echo done")
    return {"status": "succeeded", "outputs": {"saved": saved}}
""".strip(),
        encoding="utf-8",
    )


@pytest.fixture()
def plugin_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    workflow_plugins.reload_plugins()
    yield tmp_path
    workflow_plugins._PLUGIN_CACHE = None


def test_workflow_plugin_loader_exposes_node_types(plugin_project_root: Path) -> None:
    _write_plugin(plugin_project_root)

    result = workflow_plugins.reload_plugins()
    nodes = result["nodes"]

    assert len(nodes) == 1
    assert nodes[0]["id"] == "test.echo/echo@1.0.0"
    assert nodes[0]["title"] == "回声"
    assert nodes[0]["runtime"]["kind"] == "python"


@pytest.mark.asyncio
async def test_workflow_protocol_info_exposes_custom_plugin_nodes(plugin_project_root: Path) -> None:
    _write_plugin(plugin_project_root, plugin_id="video.keyframe_extractor")
    workflow_plugins.reload_plugins()

    result = await workflow_tools.workflow_protocol_info(project_id="project-1")

    assert result["ok"] is True
    nodes = result["available_plugin_nodes"]
    assert nodes[0]["plugin_id"] == "video.keyframe_extractor"
    assert nodes[0]["type"] == "echo"
    assert nodes[0]["inputs"][0]["id"] == "text"
    assert nodes[0]["outputs"][0]["id"] == "saved"


@pytest.mark.asyncio
async def test_workflow_python_plugin_runtime_executes(plugin_project_root: Path) -> None:
    _write_plugin(plugin_project_root)
    workflow_plugins.reload_plugins()

    result = await workflow_plugins.run_plugin_step(
        project_id="project-1",
        template={"id": "workflow-1", "name": "测试流程"},
        step={"id": "echo_step", "extension": "test.echo", "operation": "echo"},
        record={"input": {"workflow": {"extension": "test.echo", "operation": "echo"}}},
        inputs={"text": "hello plugin"},
    )

    assert result["ok"] is True
    run_result = result["run_result"]
    assert run_result["outputs"]["saved"]["local_url"].startswith("/api/media/project-1/generated_images/plugin_outputs/")
    assert run_result["logs"] == [{"level": "info", "message": "echo done"}]


def test_v2_compiles_plugin_steps_into_private_runner() -> None:
    public = {
        "schema": "openreel.workflow.v2",
        "id": "plugin_flow",
        "title": "插件流程",
        "steps": [
            {
                "id": "extract",
                "title": "提取关键帧",
                "kind": "plugin",
                "plugin": {
                    "id": "video.keyframe_extractor",
                    "action": "keyframe_extract",
                    "settings": {"count": 4},
                },
            }
        ],
    }
    compiled = compile_private_execution_template(public)

    step = compiled["steps"][0]
    assert step["runner"] == "workflow_plugin"
    assert step["plugin"]["id"] == "video.keyframe_extractor"
    assert step["plugin"]["action"] == "keyframe_extract"


def test_v2_preserves_structured_output_schema() -> None:
    compiled = compile_workflow_spec(
        {
            "schema": "openreel.workflow.v2",
            "id": "structured_flow",
            "title": "结构化流程",
            "steps": [
                {
                    "id": "segments",
                    "title": "分段规划",
                    "kind": "object",
                    "prompt": {"task": "规划分段。"},
                    "output": {
                        "schema": {
                            "fields": [
                                {"id": "segments", "label": "分段", "type": "array", "required": True},
                            ]
                        }
                    },
                }
            ],
        }
    )

    step = compiled["steps"][0]
    assert step["output"]["shape"] == "object"
    assert step["output"]["schema"]["fields"][0]["id"] == "segments"


def test_v2_rejects_deleted_advanced_runtime_fields() -> None:
    with pytest.raises(WorkflowSpecError):
        compile_workflow_spec({
            "schema": "openreel.workflow.v2",
            "id": "advanced_flow",
            "title": "高级流程",
            "steps": [{
                "id": "advanced_step",
                "title": "高级节点",
                "kind": "text",
                "prompt": {"task": "生成文本。"},
                "settings": {"model_tier": "strong"},
                "runtime_hidden": True,
            }],
        })
