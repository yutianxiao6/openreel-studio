"""Workflow spec artifact tools.

The larger workflow_tools module owns runtime/materialization behavior. This
module owns the model-facing spec read/write pipe so spec authoring stays on a
single validated patch primitive.
"""
from __future__ import annotations

import json
from typing import Any

from app.agent import workflow_canvas_projection
from app.agent import workflow_spec_artifacts
from app.agent import workflow_spec_patch as workflow_spec_patch_service
from app.mcp_tools.registry import register


@register(
    "workflow.spec.apply_patch",
    description="创建、替换或修订 workflow spec；保存成功后用 workflow.canvas.inspect 验收画布映射。",
    tags=["workflow", "artifact", "write"],
    search_hint=(
        "workflow spec apply patch create update replace save artifact template audit "
        "工作流 spec 一次写入 新建 修订 替换 保存 模板 artifact 校验"
    ),
    usage_hints=[
        "create 传 workflow；update 传 base 和 operations；replace 传 base 和 workflow。",
        "base 可引用 artifact_ref、template_id 或 version_id；save.target 为 artifact 或 template。",
        "新建和修改统一使用 schema='openreel.workflow.v2'。",
        "workflow 描述输入、逻辑步骤、提示词、循环、依赖、输出、引用角色和执行策略；媒体模型、画幅、分辨率、宽高、画质和 fps 由前端运行配置提供。",
        "工具会校验 V2、编译私有执行计划、执行 deterministic audit，并返回 artifact_ref 或 template_id。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "operation": {"type": "string", "enum": ["create", "update", "replace"]},
            "base": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "artifact_ref": {"type": "string"},
                    "repair_ref": {"type": "string"},
                    "template_id": {"type": "string"},
                    "version_id": {"type": "string"},
                },
            },
            "workflow": {"type": "object", "additionalProperties": True},
            "operations": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "sample_inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
            "save": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "target": {"type": "string", "enum": ["artifact", "template"]},
                    "template_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                    "applies_to": {"type": "string"},
                    "version": {"type": "string"},
                    "replace_existing": {"type": "boolean"},
                },
            },
            "user_preview": {"type": "object", "additionalProperties": True},
            "self_check": {"type": "object", "additionalProperties": True},
        },
        "required": ["operation"],
    },
    replace=True,
)
async def workflow_spec_apply_patch(
    project_id: str,
    operation: str,
    base: dict[str, Any] | None = None,
    workflow: dict[str, Any] | None = None,
    operations: list[dict[str, Any]] | None = None,
    sample_inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    save: dict[str, Any] | None = None,
    user_preview: dict[str, Any] | None = None,
    self_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return workflow_spec_patch_service.apply_workflow_spec_patch(
        project_id=project_id,
        operation=operation,
        base=base,
        workflow=workflow,
        operations=operations,
        sample_inputs=sample_inputs,
        context=context,
        save=save,
        user_preview=user_preview,
        self_check=self_check,
    )


@register(
    "workflow.canvas.inspect",
    description="只读投影 workflow 的批次、循环、画布节点、依赖边和最终输出。",
    tags=["workflow", "read", "review"],
    search_hint=(
        "workflow canvas inspect projection graph dry-run nodes edges final outputs dependencies "
        "工作流 画布 映射 投影 检查 节点 连线 最终产物 依赖"
    ),
    usage_hints=[
        "用于 workflow.spec.apply_patch 成功后验收画布映射是否符合用户目标。",
        "不运行节点、不调用 LLM、不生成媒体、不写项目状态。",
        "传 template_id、artifact_ref、repair_ref 或 inline workflow 之一；inputs 用于动态循环展开。",
        "集合输出驱动的循环用 context 传上游样例输出，例如 {'segments': {'output': {'items': [...]}}}。",
    ],
    is_read_only=True,
    is_concurrency_safe=True,
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "template_id": {"type": "string"},
            "version_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "repair_ref": {"type": "string"},
            "workflow": {"type": "object", "additionalProperties": True},
            "inputs": {"type": "object", "additionalProperties": True},
            "context": {"type": "object", "additionalProperties": True},
        },
        "required": ["project_id"],
    },
    replace=True,
)
async def workflow_canvas_inspect(
    project_id: str,
    template_id: str = "",
    version_id: str = "",
    artifact_ref: str = "",
    repair_ref: str = "",
    workflow: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    try:
        return workflow_canvas_projection.project_workflow_canvas(
            project_id=project_id,
            template_id=template_id,
            version_id=version_id,
            artifact_ref=artifact_ref,
            repair_ref=repair_ref,
            workflow=workflow,
            inputs=inputs,
            context=context,
        )
    except Exception as exc:
        return workflow_canvas_projection.project_workflow_canvas_error(exc)


@register(
    "workflow.spec.read",
    description="读取 workflow spec artifact 的 preview 或 workflow；供模板选择和搭建模式复查。",
    tags=["workflow", "artifact", "read"],
    search_hint=(
        "workflow spec artifact read preview workflow reusable revise tweak "
        "工作流 spec artifact 读取 预览 模板 微调 修订"
    ),
    usage_hints=[
        "detail='preview' 只返回用户可读摘要；detail='workflow' 返回完整模板给隔离子 Agent。",
    ],
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "detail": {"type": "string", "enum": ["preview", "workflow"]},
        },
        "required": ["artifact_ref"],
    },
    replace=True,
)
async def workflow_spec_read(
    project_id: str,
    artifact_ref: str,
    detail: str = "preview",
) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    try:
        artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_not_found"}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workflow_spec_artifact_error"}
    payload = {
        "ok": True,
        "artifact_ref": artifact_ref,
        "reusable": bool(artifact.get("reusable", True)),
        "preview": artifact.get("preview") or {},
        "sample_inputs": artifact.get("sample_inputs") or {},
        "self_check": artifact.get("self_check") or {},
    }
    if str(detail or "").strip() == "workflow":
        payload["workflow"] = artifact.get("workflow") or {}
    return payload
