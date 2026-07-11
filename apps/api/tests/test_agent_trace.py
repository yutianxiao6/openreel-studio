import json

import pytest

from app.api import routes_agent_debug
from app.agent import orchestrator
from app.agent import agent_trace
from app.agent import prompt_dump
from app.agent import trace_store
from app.agent.agent_trace import AgentTrace, result_error_kind, visible_tool_names


def test_agent_trace_writes_sanitized_jsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_AGENT_TRACE_DB_ENABLED", "0")
    monkeypatch.setattr(agent_trace, "traces_root", lambda: tmp_path)
    trace = AgentTrace("project-1", "run-1")

    trace.emit(
        "tool_result",
        iteration=2,
        tool_name="node.create",
        transition_reason="tool_completed",
        duration_ms=12,
        payload={
            "api_key": "secret-value",
            "access_token": "also-secret",
            "note": "x" * 400,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cache_read_tokens": 25,
            },
        },
    )

    lines = trace.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "tool_result"
    assert record["project_id"] == "project-1"
    assert record["run_id"] == "run-1"
    assert record["iteration"] == 2
    assert record["tool_name"] == "node.create"
    assert record["payload"]["api_key"] == "<redacted>"
    assert record["payload"]["access_token"] == "<redacted>"
    assert record["payload"]["note"].endswith("...<truncated>")
    assert record["payload"]["usage"]["prompt_tokens"] == 100
    assert record["payload"]["usage"]["total_tokens"] == 120
    assert record["payload"]["usage"]["cache_read_tokens"] == 25


def test_orchestrator_tool_call_trace_keeps_delegation_task_without_runtime_state() -> None:
    image_data_url = "data:image/png;base64," + "a" * 100
    summary = orchestrator._traceable_tool_call_input(
        "tool.execute",
        {
            "name": "agent.run",
            "input": {
                "agent": "image_editor",
                "task": "修复节点12的软件图标边角和外框；成品要主体完整、安全边距稳定。",
                "inputs": {"node_id": "12", "preview": image_data_url},
                "max_steps": 24,
            },
            "_state": {"messages": ["huge"]},
            "_user_message": "用户原话",
            "_requires_plan": False,
            "project_id": "project-1",
        },
    )

    rendered = json.dumps(summary, ensure_ascii=False)
    assert summary["deferred_tool_name"] == "agent.run"
    assert summary["input"]["input"]["task"].startswith("修复节点12")
    assert '"_state"' not in rendered
    assert '"project_id"' not in rendered
    assert "data:image/png;base64" not in rendered
    assert "<image data URL omitted" in rendered


def test_orchestrator_extracts_subagent_usage_and_trace_records() -> None:
    result = {
        "_subagent_usage": [
            {"agent": "image_editor", "step": 1, "usage": {"total_tokens": 42}},
            {"agent": "image_editor", "step": 2, "usage": "bad"},
        ],
        "_subagent_trace": [
            {"agent": "image_editor", "step": 1, "event": "model_response"},
            "bad",
        ],
    }

    assert orchestrator._subagent_usage_records(result) == [
        {"agent": "image_editor", "step": 1, "usage": {"total_tokens": 42}},
    ]
    assert orchestrator._subagent_trace_records(result) == [
        {"agent": "image_editor", "step": 1, "event": "model_response"},
    ]


def test_prompt_dump_writes_prompt_assembly_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_ENABLED", "1")
    monkeypatch.setattr(prompt_dump, "_DUMP_ROOT", tmp_path)

    prompt_dump.dump_llm_request(
        project_id="project-1",
        run_id="run-1",
        iteration=0,
        system="system",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"function": {"name": "node__create"}}],
        user_message="hi",
        prompt_assembly={
            "cache_key": "cache-1",
            "system_chars": 6,
            "history_chars": 0,
            "tool_namespaces": ["project", "node"],
            "sections": [
                {"name": "core_rules", "trigger": "always", "tier": "s", "chars": 6},
                {"name": "runtime_context", "trigger": "factory", "tier": "s", "source": "factory", "chars": 12},
            ],
            "api_key": "secret-value",
        },
    )

    path = tmp_path / "project-1" / "run-1.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["tools_count"] == 1
    assert record["prompt_assembly"]["cache_key"] == "cache-1"
    assert record["prompt_assembly"]["system_chars"] == 6
    assert record["prompt_assembly"]["tool_namespaces"] == ["project", "node"]
    assert record["prompt_assembly"]["sections"][0]["name"] == "core_rules"
    assert record["prompt_assembly"]["sections"][0]["tier"] == "s"
    assert record["prompt_assembly"]["sections"][0]["chars"] == 6
    assert record["prompt_assembly"]["api_key"] == "<redacted>"
    assert record["token_estimate"]["system_tokens"] > 0
    assert record["token_estimate"]["messages_tokens"] > 0
    assert record["token_estimate"]["tool_schema_tokens"] > 0
    assert record["token_estimate"]["stable_section_tokens"] > 0
    assert record["token_estimate"]["dynamic_section_tokens"] > 0

def test_prompt_dump_full_mode_writes_complete_request_each_iteration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_ENABLED", "1")
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_FULL", "1")
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_DIR", str(tmp_path))

    prompt_dump.dump_llm_request(
        project_id="project-1",
        run_id="run-full",
        iteration=3,
        system="system",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"function": {"name": "node__create", "parameters": {"type": "object"}}}],
        user_message=None,
    )

    path = tmp_path / "project-1" / "run-full.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["dump_mode"] == "full"
    assert record["tools"][0]["function"]["parameters"]["type"] == "object"
    assert record["api_request"]["messages"][0] == {"role": "system", "content": "system"}
    assert record["api_request"]["messages"][1] == {"role": "user", "content": "hi"}


def test_prompt_dump_metadata_mode_omits_user_and_prompt_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_ENABLED", "1")
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_INCLUDE_CONTENT", "0")
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_FULL", "1")
    monkeypatch.setattr(prompt_dump, "_DUMP_ROOT", tmp_path)

    prompt_dump.dump_llm_request(
        project_id="project-1",
        run_id="run-metadata",
        iteration=0,
        system="private system prompt",
        messages=[{"role": "user", "content": "private user request"}],
        tools=[{"function": {"name": "node__create", "description": "private schema"}}],
        user_message="private user request",
        prompt_assembly={"cache_key": "cache-1", "sections": []},
    )

    path = tmp_path / "project-1" / "run-metadata.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["dump_mode"] == "metadata"
    assert record["tool_names"] == ["node__create"]
    assert record["token_estimate"]["total_input_tokens"] > 0
    assert record["prompt_assembly"]["cache_key"] == "cache-1"
    assert "user_message" not in record
    assert "system" not in record
    assert "messages" not in record
    assert "tools" not in record
    assert "api_request" not in record
    assert "private user request" not in path.read_text(encoding="utf-8")


def test_prompt_dump_after_full_reset_contains_only_reset_visible_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_PROMPT_DUMP_ENABLED", "1")
    monkeypatch.setattr(prompt_dump, "_DUMP_ROOT", tmp_path)

    prompt_dump.dump_llm_request(
        project_id="project-1",
        run_id="run-reset",
        iteration=0,
        system="项目状态摘要:{\"title\":\"未命名项目\"}",
        messages=[
            {"role": "assistant", "content": "项目已重置，可以开始新内容"},
            {"role": "user", "content": "你好"},
        ],
        tools=[{"function": {"name": "project__get_state"}}],
        user_message="你好",
        prompt_assembly={
            "cache_key": "after-reset",
            "sections": [
                {"name": "runtime_context", "trigger": "factory", "tier": "s", "source": "factory", "chars": 12},
            ],
        },
    )

    path = tmp_path / "project-1" / "run-reset.jsonl"
    record_text = path.read_text(encoding="utf-8")

    assert "未命名项目" in record_text
    assert "项目已重置，可以开始新内容" in record_text
    assert "重置前旧蓝图剧情" not in record_text
    assert "上一轮让两个人牵手" not in record_text
    assert "旧蓝图标题" not in record_text


def test_visible_tool_names_normalizes_registry_names() -> None:
    tools = [
        {"function": {"name": "node__create"}},
        {"function": {"name": "task__create"}},
        {"function": {}},
    ]

    assert visible_tool_names(tools) == ["node.create", "task.create"]


def test_result_error_kind_uses_specific_kind_then_fallbacks() -> None:
    assert result_error_kind({"error_kind": "permission_denied"}) == "permission_denied"
    assert result_error_kind({"error": "boom"}) == "tool_error"
    assert result_error_kind({"ok": False}) == "tool_failed"
    assert result_error_kind({"ok": True}) is None


def test_trace_store_mirrors_and_queries_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_AGENT_TRACE_DB_ENABLED", "1")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}"

    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:00.000+00:00",
            "event": "run_start",
            "project_id": "project-1",
            "run_id": "run-1",
        },
        database_url=database_url,
    )
    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:01.000+00:00",
            "event": "tool_result",
            "project_id": "project-1",
            "run_id": "run-1",
            "tool_name": "node.create",
            "error_kind": "tool_error",
        },
        database_url=database_url,
    )

    listing = trace_store.list_trace_runs("project-1", database_url=database_url)
    assert listing
    assert listing["source"] == "db"
    assert listing["total"] == 1
    assert listing["traces"][0]["run_id"] == "run-1"
    assert listing["traces"][0]["event_count"] == 2
    assert listing["traces"][0]["last_event"] == "tool_result"
    assert listing["traces"][0]["last_tool_name"] == "node.create"
    assert listing["traces"][0]["error_count"] == 1

    detail = trace_store.read_trace_events("project-1", "run-1", limit=1, database_url=database_url)
    assert detail
    assert detail["source"] == "db"
    assert detail["event_count"] == 2
    assert detail["returned"] == 1
    assert detail["truncated"] is True
    assert detail["events"][0]["event"] == "tool_result"


def test_trace_store_summarizes_token_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRAMA_AGENT_TRACE_DB_ENABLED", "1")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}"

    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:00.000+00:00",
            "event": "llm_usage",
            "project_id": "project-1",
            "run_id": "run-1",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cached_prompt_tokens": 25,
                "active_input_tokens": 80_000,
                "context_limit_tokens": 100_000,
                "context_remaining_tokens": 20_000,
                "context_used_rate": 0.8,
                "context_available_rate": 0.2,
            },
        },
        database_url=database_url,
    )
    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:00.500+00:00",
            "event": "compact_boundary",
            "project_id": "project-1",
            "run_id": "run-1",
            "compact_kind": "auto",
        },
        database_url=database_url,
    )
    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:01.000+00:00",
            "event": "llm_usage",
            "project_id": "project-1",
            "run_id": "run-1",
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 40,
                "total_tokens": 340,
                "cached_prompt_tokens": 75,
                "active_input_tokens": 10_000,
                "context_limit_tokens": 100_000,
                "context_remaining_tokens": 90_000,
                "context_used_rate": 0.1,
                "context_available_rate": 0.9,
            },
        },
        database_url=database_url,
    )
    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:02.000+00:00",
            "event": "llm_usage",
            "project_id": "project-1",
            "run_id": "run-2",
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 40,
                "total_tokens": 240,
                "cached_prompt_tokens": 50,
                "active_input_tokens": 30_000,
                "context_limit_tokens": 100_000,
                "context_remaining_tokens": 70_000,
                "context_used_rate": 0.3,
                "context_available_rate": 0.7,
            },
        },
        database_url=database_url,
    )

    summary = trace_store.summarize_token_usage("project-1", database_url=database_url)

    assert summary
    assert summary["event_count"] == 3
    assert summary["totals"]["llm_calls"] == 3
    assert summary["totals"]["prompt_tokens"] == 600
    assert summary["totals"]["total_tokens"] == 700
    assert summary["totals"]["cached_prompt_tokens"] == 150
    assert summary["totals"]["cache_hit_rate"] == 0.25
    assert summary["totals"]["context_peak_used_rate"] == 0.3
    assert summary["totals"]["context_peak_available_rate"] == 0.7
    assert summary["totals"]["context_peak_remaining_tokens"] == 70_000
    assert summary["totals"]["cumulative_tokens"]["total_tokens"] == 700
    assert summary["last_usage"]["latest_call_context"]["context_remaining_tokens"] == 70_000
    assert summary["latest_call_tokens"]["total_tokens"] == 240
    assert summary["latest_call_context"]["context_available_rate"] == 0.7
    assert summary["session_cumulative_tokens"]["total_tokens"] == 700
    assert summary["session_context_peak"]["context_remaining_tokens"] == 70_000
    assert {item["run_id"] for item in summary["by_run"]} == {"run-1", "run-2"}
    by_run = {item["run_id"]: item["totals"] for item in summary["by_run"]}
    assert by_run["run-1"]["prompt_tokens"] == 400
    assert by_run["run-1"]["context_peak_available_rate"] == 0.9
    assert by_run["run-1"]["context_peak"]["context_available_rate"] == 0.9
    assert by_run["run-2"]["context_peak_available_rate"] == 0.7

    since_clear = trace_store.summarize_token_usage(
        "project-1",
        since_ts="2026-06-03T00:00:00.750+00:00",
        database_url=database_url,
    )

    assert since_clear
    assert since_clear["since_ts"] == "2026-06-03T00:00:00.750+00:00"
    assert since_clear["event_count"] == 2
    assert since_clear["totals"]["llm_calls"] == 2
    assert since_clear["totals"]["prompt_tokens"] == 500
    assert since_clear["totals"]["total_tokens"] == 580
    assert {item["run_id"] for item in since_clear["by_run"]} == {"run-1", "run-2"}


@pytest.mark.asyncio
async def test_agent_debug_reads_trace_from_db_source(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}"
    trace_store.append_trace_event(
        {
            "ts": "2026-06-03T00:00:00.000+00:00",
            "event": "run_start",
            "project_id": "project-1",
            "run_id": "run-1",
        },
        database_url=database_url,
    )
    monkeypatch.setattr(
        routes_agent_debug,
        "list_trace_runs",
        lambda project_id, limit=20: trace_store.list_trace_runs(
            project_id, limit=limit, database_url=database_url,
        ),
    )
    monkeypatch.setattr(
        routes_agent_debug,
        "read_trace_events",
        lambda project_id, run_id, limit=200: trace_store.read_trace_events(
            project_id, run_id, limit=limit, database_url=database_url,
        ),
    )

    listing = await routes_agent_debug.list_agent_traces("project-1", source="db")
    detail = await routes_agent_debug.get_agent_trace("project-1", "run-1", source="db")

    assert listing["source"] == "db"
    assert listing["total"] == 1
    assert detail["source"] == "db"
    assert detail["events"][0]["event"] == "run_start"


@pytest.mark.asyncio
async def test_agent_debug_lists_trace_summaries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_agent_debug, "traces_root", lambda: tmp_path)
    trace_dir = tmp_path / "project-1"
    trace_dir.mkdir()
    (trace_dir / "run-1.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-06-03T00:00:00Z", "event": "run_start"}),
            json.dumps({
                "ts": "2026-06-03T00:00:01Z",
                "event": "tool_result",
                "tool_name": "node.create",
                "error_kind": "tool_error",
            }),
        ]),
        encoding="utf-8",
    )

    result = await routes_agent_debug.list_agent_traces("project-1", source="files")

    assert result["total"] == 1
    summary = result["traces"][0]
    assert summary["run_id"] == "run-1"
    assert summary["event_count"] == 2
    assert summary["last_event"] == "tool_result"
    assert summary["last_tool_name"] == "node.create"
    assert summary["error_count"] == 1


@pytest.mark.asyncio
async def test_agent_debug_reads_trace_tail(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_agent_debug, "traces_root", lambda: tmp_path)
    trace_dir = tmp_path / "project-1"
    trace_dir.mkdir()
    (trace_dir / "run-1.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": f"2026-06-03T00:00:0{i}Z", "event": f"event_{i}"})
            for i in range(5)
        ),
        encoding="utf-8",
    )

    result = await routes_agent_debug.get_agent_trace("project-1", "run-1", limit=2, source="files")

    assert result["event_count"] == 5
    assert result["returned"] == 2
    assert result["truncated"] is True
    assert [event["event"] for event in result["events"]] == ["event_3", "event_4"]


@pytest.mark.asyncio
async def test_agent_debug_lists_artifact_summaries(tmp_path, monkeypatch) -> None:
    trace_root = tmp_path / "traces"
    prompt_root = tmp_path / "prompts"
    tool_root = tmp_path / "tool_results"
    (trace_root / "project-1").mkdir(parents=True)
    (prompt_root / "project-1").mkdir(parents=True)
    (tool_root / "project-1" / "run-1").mkdir(parents=True)

    (trace_root / "project-1" / "run-1.jsonl").write_text("{}", encoding="utf-8")
    (prompt_root / "project-1" / "run-1.jsonl").write_text("{}", encoding="utf-8")
    (tool_root / "project-1" / "run-1" / "result.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes_agent_debug, "traces_root", lambda: trace_root)
    monkeypatch.setattr(routes_agent_debug, "prompt_dumps_root", lambda: prompt_root)
    monkeypatch.setattr(routes_agent_debug, "tool_results_dir", lambda: tool_root)

    result = await routes_agent_debug.list_agent_artifacts("project-1")

    artifacts = result["artifacts"]
    assert artifacts["traces"]["total"] == 1
    assert artifacts["prompt_dumps"]["total"] == 1
    assert artifacts["tool_results"]["total"] == 1
    assert artifacts["traces"]["items"][0]["path"] == "data/agent_traces/project-1/run-1.jsonl"
    assert artifacts["traces"]["items"][0]["relative_path"] == "run-1.jsonl"
    assert artifacts["prompt_dumps"]["items"][0]["path"] == "data/prompt_dumps/project-1/run-1.jsonl"
    assert artifacts["tool_results"]["items"][0]["path"] == "data/tool_results/project-1/run-1/result.json"


@pytest.mark.asyncio
async def test_agent_debug_reads_artifact_tail_with_bounds(tmp_path, monkeypatch) -> None:
    trace_root = tmp_path / "traces"
    (trace_root / "project-1").mkdir(parents=True)
    artifact = trace_root / "project-1" / "run-1.jsonl"
    artifact.write_text("\n".join(f"line-{i}" for i in range(6)), encoding="utf-8")
    monkeypatch.setattr(routes_agent_debug, "traces_root", lambda: trace_root)

    result = await routes_agent_debug.read_agent_artifact(
        "project-1",
        kind="traces",
        path="data/agent_traces/project-1/run-1.jsonl",
        max_bytes=12,
        tail_lines=3,
    )

    assert result["kind"] == "traces"
    assert result["relative_path"] == "run-1.jsonl"
    assert result["mode"] == "tail_lines"
    assert result["total_lines"] == 6
    assert result["truncated"] is True
    assert result["returned_bytes"] <= 12
    assert "line-5" in result["content"]


@pytest.mark.asyncio
async def test_agent_debug_reads_nested_tool_result_artifact(tmp_path, monkeypatch) -> None:
    tool_root = tmp_path / "tool_results"
    (tool_root / "project-1" / "run-1").mkdir(parents=True)
    artifact = tool_root / "project-1" / "run-1" / "result.json"
    artifact.write_text('{"ok": true}', encoding="utf-8")
    monkeypatch.setattr(routes_agent_debug, "tool_results_dir", lambda: tool_root)

    result = await routes_agent_debug.read_agent_artifact(
        "project-1",
        kind="tool_results",
        path="run-1/result.json",
        tail_lines=0,
    )

    assert result["path"] == "data/tool_results/project-1/run-1/result.json"
    assert result["relative_path"] == "run-1/result.json"
    assert result["mode"] == "tail_bytes"
    assert result["content"] == '{"ok": true}'


@pytest.mark.asyncio
async def test_agent_debug_rejects_artifact_path_traversal(tmp_path, monkeypatch) -> None:
    trace_root = tmp_path / "traces"
    (trace_root / "project-1").mkdir(parents=True)
    monkeypatch.setattr(routes_agent_debug, "traces_root", lambda: trace_root)

    with pytest.raises(routes_agent_debug.HTTPException) as exc_info:
        await routes_agent_debug.read_agent_artifact(
            "project-1",
            kind="traces",
            path="../secret.jsonl",
        )

    assert exc_info.value.status_code == 400
