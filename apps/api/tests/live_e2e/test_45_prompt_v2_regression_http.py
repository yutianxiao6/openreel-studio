from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx
import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.live_e2e]


def _done(events: list[dict[str, Any]], status: str = "completed") -> None:
    done = [event for event in events if event.get("type") == "done"]
    assert done, [event.get("type") for event in events]
    assert done[-1].get("status") == status


def _tool_names(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(event.get("tool"))
        for event in events
        if event.get("type") in {"tool_start", "tool_done"} and event.get("tool")
    ]


def _text(events: list[dict[str, Any]]) -> str:
    return "".join(str(event.get("content") or "") for event in events if event.get("type") == "text_delta")


async def _json_get(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    return body if isinstance(body, dict) else {"value": body}


async def _latest_trace(
    api_client: httpx.AsyncClient,
    project_id: str,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    listing = await _json_get(api_client, f"/api/agent/debug/{project_id}/traces?source=auto&limit=1")
    traces = listing.get("traces") or []
    assert traces, listing
    run_id = traces[0]["run_id"]
    return await _json_get(api_client, f"/api/agent/debug/{project_id}/traces/{run_id}?source=auto&limit={limit}")


async def _latest_prompt_dump(
    api_client: httpx.AsyncClient,
    project_id: str,
) -> dict[str, Any]:
    artifacts = await _json_get(api_client, f"/api/agent/debug/{project_id}/artifacts?limit=1")
    prompt_dumps = artifacts.get("artifacts", {}).get("prompt_dumps", {})
    items = prompt_dumps.get("items") or []
    assert items, artifacts
    item = items[0]
    read = await _json_get(
        api_client,
        f"/api/agent/debug/{project_id}/artifacts/read?kind=prompt_dumps&path={item['relative_path']}",
    )
    lines = [line for line in str(read.get("content") or "").splitlines() if line.strip()]
    assert lines, read
    return json.loads(lines[-1])


def _prompt_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    prompt_assembly = record.get("prompt_assembly") if isinstance(record.get("prompt_assembly"), dict) else {}
    sections = prompt_assembly.get("sections") if isinstance(prompt_assembly.get("sections"), list) else []
    return {
        "system_len": record.get("system_len"),
        "tools_count": record.get("tools_count"),
        "system_tokens": record.get("token_estimate", {}).get("system_tokens"),
        "total_input_tokens": record.get("token_estimate", {}).get("total_input_tokens"),
        "cache_key": prompt_assembly.get("cache_key"),
        "section_count": prompt_assembly.get("section_count"),
        "section_names": [
            section.get("name")
            for section in sections
            if isinstance(section, dict)
        ],
        "section_chars": {
            str(section.get("name")): section.get("chars")
            for section in sections
            if isinstance(section, dict)
        },
        "tool_namespaces": prompt_assembly.get("tool_namespaces"),
        "guide_sections": [
            section.get("name")
            for section in sections
            if isinstance(section, dict) and section.get("source") == "guide"
        ],
    }


async def _seed_text_nodes(
    api_client: httpx.AsyncClient,
    project_id: str,
    call_tool_request: Callable[..., Awaitable[Any]],
) -> list[str]:
    ids: list[str] = []
    for idx in range(2):
        created = await call_tool_request(
            api_client,
            "node.create",
            {
                "project_id": project_id,
                "type": "text",
                "fields": {
                    "title": f"牵手参考{idx + 1}",
                    "content": "用于提示词缓存回归测试的文本参考节点。",
                },
            },
        )
        ids.append(str(created["id"]))
    return ids


async def test_prompt_v2_ordinary_turns_stay_read_only_and_cache_stable(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
) -> None:
    first = await send_chat_request(api_client, project_id, "你好")
    _done(first)
    assert "node.create" not in _tool_names(first)
    assert "node.run" not in _tool_names(first)
    assert "plan.propose" not in _tool_names(first)
    assert "project.reset" not in _tool_names(first)
    text = _text(first)
    assert text.strip()

    first_prompt = _prompt_snapshot(await _latest_prompt_dump(api_client, project_id))
    first_trace = await _latest_trace(api_client, project_id)
    assert first_trace["events"]
    assert first_prompt["cache_key"]
    assert first_prompt["section_count"] >= 1
    assert first_prompt["tools_count"] > 0
    assert first_prompt["system_len"] > 0

    second = await send_chat_request(api_client, project_id, "画布上有几个节点")
    _done(second)
    assert "node.create" not in _tool_names(second)
    assert "node.run" not in _tool_names(second)
    assert "plan.propose" not in _tool_names(second)
    assert "project.reset" not in _tool_names(second)

    second_prompt = _prompt_snapshot(await _latest_prompt_dump(api_client, project_id))
    second_trace = await _latest_trace(api_client, project_id)
    assert second_trace["events"]
    assert second_prompt["cache_key"]
    assert second_prompt["tools_count"] == first_prompt["tools_count"]
    assert second_prompt["section_count"] == first_prompt["section_count"]
    assert second_prompt["tool_namespaces"] == first_prompt["tool_namespaces"]
    assert second_prompt["system_len"] <= first_prompt["system_len"] + 512
    assert second_prompt["total_input_tokens"] > 0


async def test_prompt_v2_video_intake_and_reset_flow_keep_prompt_dumps_and_trace_visible(
    api_client: httpx.AsyncClient,
    project_id: str,
    send_chat_request: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool_request: Callable[..., Awaitable[Any]],
) -> None:
    first = await send_chat_request(api_client, project_id, "做一段15秒的视频")
    _done(first)
    assert "node.create" not in _tool_names(first)
    assert "node.run" not in _tool_names(first)
    assert "project.reset" not in _tool_names(first)
    first_text = _text(first)
    assert "视频主题" in first_text
    assert "风格" in first_text
    assert "视频类型" in first_text

    first_prompt = _prompt_snapshot(await _latest_prompt_dump(api_client, project_id))
    first_trace = await _latest_trace(api_client, project_id)
    assert first_prompt["cache_key"]
    assert first_prompt["section_count"] >= 1
    assert first_prompt["tools_count"] > 0
    assert {"identity", "working_loop", "core_rules"} <= set(first_prompt["section_names"])
    assert any(event.get("event") == "prompt_assembly" for event in first_trace["events"])
    assert any(event.get("event") == "llm_usage" for event in first_trace["events"])

    second = await send_chat_request(
        api_client,
        project_id,
        "动作打斗，国风动漫，动作短片，16:9，其他你自己决定",
    )
    _done(second)
    assert "node.create" not in _tool_names(second)
    assert "node.run" not in _tool_names(second)
    second_text = _text(second)
    assert "剧情大纲" in second_text or "集数" in second_text

    third = await send_chat_request(
        api_client,
        project_id,
        "没有现成大纲，你来发挥；做1集，每段15秒。使用宫格分镜。",
    )
    _done(third)
    assert "node.create" not in _tool_names(third)
    assert "node.run" not in _tool_names(third)
    assert "project.reset" not in _tool_names(third)
    third_text = _text(third)
    assert "蓝图" in third_text or "剧情大纲" in third_text or "确认" in third_text

    third_prompt = _prompt_snapshot(await _latest_prompt_dump(api_client, project_id))
    third_trace = await _latest_trace(api_client, project_id)
    assert third_prompt["cache_key"]
    assert third_prompt["section_count"] >= 1
    assert third_prompt["tools_count"] == first_prompt["tools_count"]
    assert third_prompt["tool_namespaces"] == first_prompt["tool_namespaces"]
    assert any(event.get("event") == "prompt_assembly" for event in third_trace["events"])
    assert any(event.get("event") == "llm_usage" for event in third_trace["events"])

    seeded_ids = await _seed_text_nodes(api_client, project_id, call_tool_request)
    assert len(seeded_ids) == 2

    image_events = await send_chat_request(
        api_client,
        project_id,
        "让画布上的两个人在一起手牵手，生成一张新的图片，不要做视频。",
    )
    _done(image_events)
    names = _tool_names(image_events)
    assert "project.reset" not in names
    assert "plan.propose" not in names
    assert "blueprint.draft_video" not in names
    assert "video" not in names
    image_text = _text(image_events)
    assert image_text.strip()

    reset_events = await send_chat_request(api_client, project_id, "重置项目")
    _done(reset_events)
    assert any(event.get("type") == "confirm_required" and event.get("action") == "reset_project" for event in reset_events)
    assert "blueprint.draft_video" not in _tool_names(reset_events)

    confirm_events = await send_chat_request(api_client, project_id, "确认")
    _done(confirm_events)
    assert any(event.get("type") == "canvas_action" and event.get("action") == "clear_all" for event in confirm_events)

    reset_prompt = _prompt_snapshot(await _latest_prompt_dump(api_client, project_id))
    reset_trace = await _latest_trace(api_client, project_id)
    assert reset_prompt["cache_key"]
    assert reset_prompt["section_count"] >= 1
    assert reset_prompt["tools_count"] > 0
    assert any(event.get("event") in {"run_start", "prompt_assembly"} for event in reset_trace["events"])
    assert any(event.get("event") == "llm_usage" for event in reset_trace["events"])
