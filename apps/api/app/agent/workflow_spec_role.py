"""Workflow template selector subagent role contract.

This keeps the workflow selector role close to Codex-style mode templates:
agent_tools owns the generic subagent loop, while this module owns the
workflow-specific role prompt, tool boundary, and task-message shape.
"""
from __future__ import annotations

import json
from typing import Any


ROLE_NAME = "workflow_spec"

SELECTOR_TOOLS: list[str] = [
    "skill.search",
    "skill.get",
    "workflow.template.resolve",
    "workflow.template.read",
    "workflow.spec.read",
]

MAX_OUTPUT_TOKENS = 10000

SYSTEM_PROMPT = (
    "# workflow_spec Selector\n\n"
    "你只负责为主 Agent 选择现有 OpenReel workflow 模板。主 Agent 转述用户目标、补运行输入并执行流程。\n\n"
    "## Work\n\n"
    "- 普通制作视频、30秒视频、文生视频或最终视频目标默认返回 `general_short_drama_workflow` 的 template_id。\n"
    "- 用户明确指定模板、skill 或已有引用时，用 skill.search/get、workflow.template.resolve、workflow.template.read 和 workflow.spec.read 定位候选。\n"
    "- 返回最匹配的 template_id/version_id、input_fields、validation、user_preview 和 self_check。\n"
    "- 没有合适模板时返回 blocked，并用 blocked_reason 简短说明缺少哪类模板。\n\n"
    "## Output\n\n"
    f"completed 只返回已存在模板引用、输入定义、validation 和 self_check；每次 LLM 输出按 {MAX_OUTPUT_TOKENS} tokens 请求。\n"
)

RESULT_CONTRACT = (
    "result: {status:'completed|blocked', decision:'reuse_existing', "
    "template_id, version_id, input_fields, validation, user_preview:{title,summary}, "
    "self_check:{passed:boolean, checks:[string], issues:[string]}}。"
    "completed 表示已选出现有模板；blocked 表示无法选出可用模板。"
)


def role_preset() -> dict[str, Any]:
    return {
        "description": "工作流选择器:隔离读取 skill/模板，返回最匹配的可执行模板引用",
        "task_type": "subagent_workflow_spec",
        "readonly": True,
        "strict_allowed_tools": True,
        "include_tool_schemas": True,
        "enforce_max_steps": True,
        "max_steps": 10,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "scope_hint": "workflow 模板选择和输入定义读取",
        "system": SYSTEM_PROMPT,
        "allowed_tools": SELECTOR_TOOLS,
        "result_contract": RESULT_CONTRACT,
    }


def allowed_tools_for_mode(mode: str) -> list[str]:
    return SELECTOR_TOOLS


def build_task_message(task: str, inputs: dict | None) -> str:
    raw_task = str(task or "").strip() or "把输入流程处理成可执行 workflow 引用。"
    inputs_json = json.dumps(inputs or {}, ensure_ascii=False)
    return (
        "## Task\n"
        + raw_task
        + "\n\n## Inputs\n"
        + inputs_json
        + "\n\n## Mode\n"
        + "- selector 模式只选择已有 workflow 模板。\n"
        + "- 用 skill.search/skill.get、workflow.template.resolve、workflow.template.read 和 workflow.spec.read 查询候选。\n"
        + "- 选择最匹配的 template_id/version_id。\n"
        + "- default_video: 普通制作视频、30秒视频、文生视频或最终视频目标，返回 general_short_drama_workflow 的 template_id。\n"
        + "- 没有合适模板时返回 blocked，并用 blocked_reason 简短说明缺少哪类模板。\n"
        + "- 最终 JSON 只返回模板引用、decision、validation、self_check 和短 preview。\n"
        + "\n\n## 最终 JSON\n"
        + '{"status":"completed","summary":"...","result":{"status":"completed","decision":"reuse_existing","template_id":"general_short_drama_workflow","validation":{"ok":true},"self_check":{"passed":true,"checks":[],"issues":[]}}}'
    )
