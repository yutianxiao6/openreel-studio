from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest

from app.agent import workflow_spec_artifacts, workflow_template_store


pytestmark = [pytest.mark.asyncio, pytest.mark.live_e2e]


def _done(events: list[dict[str, Any]], status: str = "completed") -> None:
    done = [event for event in events if event.get("type") == "done"]
    assert done, [event.get("type") for event in events]
    assert done[-1].get("status") == status


def _no_contract_error(events: list[dict[str, Any]]) -> None:
    assert not [
        event
        for event in events
        if event.get("type") == "error"
        and "SSE event contract error" in str(event.get("message") or "")
    ]


def _tool_names(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(event.get("tool"))
        for event in events
        if event.get("type") in {"tool_start", "tool_done"} and event.get("tool")
    ]


def _write_workflow_skill(skills_root: Path) -> None:
    workflow_dir = skills_root / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "lunar_story_to_script.md").write_text(
        """---
category: workflow
description: 月影短剧剧情转剧本流程；用户输入 plot 剧情后生成剧本文本。
applies_to: lunar_story_to_script 月影短剧 剧情输入 剧本 文字流程
---

# 月影短剧剧情转剧本

这个 workflow skill 描述一个轻量文字流程：

- 运行输入包含 `plot`，含义是用户输入的剧情。
- 流程先承接剧情输入，再生成一个剧本文本节点。
- 不需要图片、视频或音频节点。
- 剧本节点的提示词模板应把 `plot` 和上游剧情输入作为主要依据。
""",
        encoding="utf-8",
    )


def _seed_matching_template() -> None:
    workflow_template_store.save_user_template(
        workflow={
            "id": "lunar_story_script_template",
            "name": "月影短剧剧情转剧本模板",
            "description": "匹配 lunar_story_to_script skill：用户输入剧情后生成剧本文本。",
            "applies_to": "lunar_story_to_script 月影短剧 剧情输入 剧本 文字流程",
            "inputs": [{"id": "plot", "label": "剧情", "type": "textarea"}],
            "required_inputs": ["plot"],
            "steps": [
                {
                    "id": "brief",
                    "title": "剧情输入",
                    "node_type": "text",
                    "runner": "node.run",
                    "fields": {"purpose": "承接用户剧情输入"},
                },
                {
                    "id": "script",
                    "title": "剧本文本",
                    "node_type": "text",
                    "runner": "node.run",
                    "depends_on": ["brief"],
                    "primary_skill": "script_writing",
                    "skill_category": "prompt",
                    "prompt_template": "SYSTEM: 原始月影剧本模板\nUSER: {{inputs.plot}}\nOUTPUT: text",
                },
            ],
        },
        template_id="lunar_story_script_template",
        name="月影短剧剧情转剧本模板",
        description="匹配 lunar_story_to_script skill：用户输入剧情后生成剧本文本。",
        category="user",
        applies_to="lunar_story_to_script 月影短剧 剧情输入 剧本 文字流程",
        source={
            "source_skill": {
                "name": "lunar_story_to_script",
                "summary": "用户输入 plot 剧情后生成剧本文本。",
            }
        },
        sample_inputs={"plot": "雨夜怀表"},
        replace_existing=True,
    )


def _workflow_prompt_template(node: dict[str, Any]) -> str:
    input_payload = node.get("input")
    if not isinstance(input_payload, dict):
        return ""
    workflow = input_payload.get("workflow")
    if not isinstance(workflow, dict):
        return ""
    return str(workflow.get("prompt_template") or "")


def _workflow_step_id(node: dict[str, Any]) -> str:
    input_payload = node.get("input")
    if not isinstance(input_payload, dict):
        return ""
    workflow = input_payload.get("workflow")
    if not isinstance(workflow, dict):
        return ""
    return str(workflow.get("step_id") or workflow.get("template_step_id") or "")


def _workflow_last_run_id(node: dict[str, Any]) -> str:
    input_payload = node.get("input")
    if not isinstance(input_payload, dict):
        return ""
    workflow = input_payload.get("workflow")
    if not isinstance(workflow, dict):
        return ""
    last_run = workflow.get("last_run")
    if not isinstance(last_run, dict):
        return ""
    return str(last_run.get("run_id") or "")


def _node_content(node: dict[str, Any]) -> str:
    input_payload = node.get("input")
    if isinstance(input_payload, dict) and input_payload.get("content"):
        return str(input_payload.get("content") or "")
    output = node.get("output")
    if isinstance(output, dict) and output.get("content"):
        return str(output.get("content") or "")
    return ""


async def _all_text_node_details(
    api_client: httpx.AsyncClient,
    project_id: str,
    call_tool_request: Callable[..., Awaitable[Any]],
) -> list[dict[str, Any]]:
    listed = await call_tool_request(api_client, "node.list", {"project_id": project_id, "type": "text", "limit": 0})
    assert listed["ok"] is True
    node_ids = [str(item["id"]) for item in listed["nodes"]]
    assert node_ids
    details = await call_tool_request(api_client, "node.get", {"project_id": project_id, "node_ids": node_ids})
    if "nodes" in details:
        return list(details["nodes"])
    return [details]


def _script_node(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    exact = [node for node in nodes if _workflow_step_id(node) == "script"]
    if exact:
        return exact[-1]
    candidates = [
        node for node in nodes
        if "剧本" in str(node.get("title") or "")
        or "script" in json.dumps(node.get("input") or {}, ensure_ascii=False)
    ]
    assert candidates, nodes
    return candidates[-1]


async def test_natural_language_skill_workflow_local_patch_and_template_save(
    api_client: httpx.AsyncClient,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool_request: Callable[..., Awaitable[Any]],
) -> None:
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(workflow_template_store, "workflow_template_library_root", lambda: tmp_path / "workflow_templates")
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path / "tool_results")
    _write_workflow_skill(tmp_path / "skills")
    _seed_matching_template()

    create_events = await send_chat_request(
        api_client,
        project_id,
        (
            "请使用本地 skill 目录里的 lunar_story_to_script 这个 workflow skill，"
            "直接帮我搭建并运行一个文字工作流：运行输入 plot 是“雨夜怀表”，"
            "流程要把剧情输入转成剧本文本。不要生成图片、视频或音频；"
            "不要保存成模板；不要再问我，直接执行。"
        ),
    )
    _done(create_events)
    _no_contract_error(create_events)
    create_tools = _tool_names(create_events)
    assert "skill.search" in create_tools or "skill.get" in create_tools
    assert "node.create" in create_tools or "tool.execute" in create_tools

    nodes_after_create = await _all_text_node_details(api_client, project_id, call_tool_request)
    created_script = _script_node(nodes_after_create)
    created_template = _workflow_prompt_template(created_script)
    assert "原始月影剧本模板" in created_template
    created_script_content = _node_content(created_script)
    assert "雨夜怀表" in created_script_content
    assert len(created_script_content) > 300
    assert "人物" in created_script_content
    assert "：" in created_script_content
    created_last_run_id = _workflow_last_run_id(created_script)
    assert created_last_run_id

    local_prompt = "SYSTEM: 当前实例强化模板；输出必须包含“当前实例强化标记”，并写成短剧剧本\nUSER: {{inputs.plot}}\nOUTPUT: text"
    local_events = await send_chat_request(
        api_client,
        project_id,
        (
            "现在只修改当前画布里这个流程的剧本节点，不要改用户模板："
            f"把剧本节点的提示词模板局部改成“{local_prompt}”，"
            "然后重新运行这个剧本节点。不要生成图片、视频或音频。"
        ),
    )
    _done(local_events)
    _no_contract_error(local_events)
    local_tools = _tool_names(local_events)
    assert "node.update" in local_tools
    assert "node.run" in local_tools or "tool.execute" in local_tools

    nodes_after_local_patch = await _all_text_node_details(api_client, project_id, call_tool_request)
    patched_script = _script_node(nodes_after_local_patch)
    assert "当前实例强化模板" in _workflow_prompt_template(patched_script)
    assert _workflow_last_run_id(patched_script) != created_last_run_id
    assert "当前实例强化标记" in _node_content(patched_script)

    original_export = await call_tool_request(
        api_client,
        "workflow.template.export",
        {"project_id": project_id, "template_id": "lunar_story_script_template"},
    )
    assert original_export["ok"] is True
    assert "原始月影剧本模板" in json.dumps(original_export["package"]["workflow"], ensure_ascii=False)
    assert "当前实例强化模板" not in json.dumps(original_export["package"]["workflow"], ensure_ascii=False)

    reusable_prompt = "SYSTEM: 可复用模板强化钩子\nUSER: {{inputs.plot}}\nOUTPUT: text"
    save_events = await send_chat_request(
        api_client,
        project_id,
        (
            "现在把这个流程另存为今后可直接选择的用户模板，"
            "模板名叫“月影短剧强化剧本模板”。"
            f"可复用模板里的剧本节点提示词模板使用“{reusable_prompt}”。"
            "保存后不要生成图片、视频或音频。"
        ),
    )
    _done(save_events)
    _no_contract_error(save_events)
    save_tools = _tool_names(save_events)
    assert "tool.execute" in save_tools or "workflow.template.promote" in save_tools

    listed = await call_tool_request(
        api_client,
        "workflow.list_templates",
        {"project_id": project_id, "category": "user", "limit": 8},
    )
    assert listed["ok"] is True
    saved_templates = [item for item in listed["templates"] if "月影短剧强化剧本模板" in str(item.get("name") or "")]
    assert saved_templates, listed
    saved_template_id = str(saved_templates[-1]["id"])

    exported = await call_tool_request(
        api_client,
        "workflow.template.export",
        {"project_id": project_id, "template_id": saved_template_id},
    )
    assert exported["ok"] is True
    assert "可复用模板强化钩子" in json.dumps(exported["package"]["workflow"], ensure_ascii=False)
