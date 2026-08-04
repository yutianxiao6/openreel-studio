from agent_plan_contract_helpers import *  # noqa: F401,F403
from unittest.mock import AsyncMock

def test_video_mode_reminder_respects_visual_preproduction_scope() -> None:
    reminder = build_video_mode_system_reminder(
        {"project_mode": "video_production"},
        video_output_disabled=True,
    )

    assert "视觉预制作" in reminder
    assert "文本说明和图片素材" in reminder
    assert "视频片段" not in reminder

def test_context_policy_keeps_chat_history_visible_without_state_continuation() -> None:
    state = {"memory": {"facts": [{"content": "上一轮要做视频"}]}}

    assert chat_history_visible_for_turn(state) is True

@pytest.mark.parametrize(
    "state",
    [
        {"pending_video_request": {"stage": "structure"}},
        {"pending_video_mode_choice": {"status": "pending"}},
        {"_pending_reset_confirm": {"scope": "full"}},
    ],
)
def test_context_policy_hides_chat_history_for_pending_state(state: dict) -> None:
    assert chat_history_visible_for_turn(state) is False

def test_project_reset_is_core_and_hides_internal_confirm_token() -> None:
    spec = registry.get("project.reset")

    assert "project.reset" in registry._CORE_AGENT_TOOLS
    assert spec is not None
    assert "_confirm_token" not in (spec.schema.get("properties") or {})
    assert set((spec.schema.get("properties") or {}).keys()) == {"scope", "reason", "new_theme"}

@pytest.mark.asyncio
async def test_tool_execute_rejects_core_project_reset_after_pending_confirmation() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="project.reset",
        input={"scope": "full"},
        _state={"_pending_reset_confirm": {"scope": "full", "reason": "test reset"}},
        _user_message="latest user message",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "core_tool_should_be_called_directly"
    assert result["tool"] == "project.reset"

@pytest.mark.asyncio
async def test_tool_execute_full_project_reset_is_not_deferred() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="project.reset",
        input={"scope": "full"},
        _state={},
        _user_message="重置项目，全部清空重新开始",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "core_tool_should_be_called_directly"
    assert result["tool"] == "project.reset"

@pytest.mark.asyncio
async def test_tool_execute_does_not_run_core_canvas_delete_without_pending_state(monkeypatch) -> None:
    called = False

    async def fake_registry_call(target: str, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(tool_meta_tools.registry, "call", fake_registry_call)

    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="canvas.delete",
        input={"scope": "all"},
        _state={},
        _user_message="清空画布",
    )

    assert called is False
    assert result["ok"] is False
    assert result["error_kind"] == "core_tool_should_be_called_directly"
    assert result["tool"] == "canvas.delete"

@pytest.mark.asyncio
async def test_tool_execute_does_not_run_core_canvas_delete_after_structured_pending_confirmation(monkeypatch) -> None:
    captured = {}

    async def fake_registry_call(target: str, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "deleted_nodes": 2}

    monkeypatch.setattr(tool_meta_tools.registry, "call", fake_registry_call)

    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="canvas.delete",
        input={"scope": "all"},
        _state={
            "_pending_tool_confirm": {
                "kind": "tool_confirmation",
                "target": "canvas.delete",
                "expires_at": confirmation_expires_at(),
            }
        },
        _user_message="确认清空画布",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "core_tool_should_be_called_directly"
    assert result["tool"] == "canvas.delete"
    assert captured == {}

def test_reset_confirmation_text_names_tasks_panel_canvas_and_title() -> None:
    text = reset_confirmation_text()

    for phrase in ("任务", "面板", "画布", "未命名项目"):
        assert phrase in text
    assert "聊天上下文" in text
    assert "trace" in text

@pytest.mark.asyncio
async def test_full_reset_chat_archive_helper_marks_active_messages() -> None:
    rows = [
        SimpleNamespace(project_id="project-1", archived=False),
        SimpleNamespace(project_id="project-1", archived=False),
    ]
    added = []

    class FakeResult:
        def all(self):
            return rows

    class FakeSession:
        async def exec(self, statement):
            return FakeResult()

        def add(self, item):
            added.append(item)

    count = await drama_tools._archive_project_chat_messages(FakeSession(), "project-1")

    assert count == 2
    assert all(row.archived is True for row in rows)
    assert added == rows


def test_runtime_context_omits_canvas_summary_and_only_keeps_project_title() -> None:
    context = runtime_context.build({
        "metadata": {"title": "节点区分测试"},
        "_canvas_summary": {
            "total": 3,
            "by_type": {"text": 1, "image": 1, "video": 1},
            "by_status": {"completed": 2, "failed": 1},
            "by_surface": {"project_panel": 2, "draft_canvas": 1},
            "surface_details": {
                "project_panel": {
                    "total": 2,
                    "by_type": {"image": 1, "video": 1},
                    "by_status": {"completed": 1, "failed": 1},
                },
                "draft_canvas": {
                    "total": 1,
                    "by_type": {"text": 1},
                    "by_status": {"completed": 1},
                },
            },
        },
    })

    assert "项目标题" in context
    assert "节点区分测试" in context
    assert "### 项目节点现状(以此为准)" not in context
    assert "项目节点(DB 真实):共 3 个" not in context
    assert "旧工程面板(project_panel)" not in context
    assert "统一画布(draft_canvas)" not in context
    assert "用户和 Agent 共用同一画布" not in context
    assert "空/草稿节点可补全" not in context


@pytest.mark.asyncio
async def test_canvas_summary_counts_nodes_by_surface() -> None:
    orchestrator = AgentOrchestrator(None)  # type: ignore[arg-type]

    class FakeNodeService:
        async def list_nodes(self, project_id: str):
            return [
                SimpleNamespace(
                    id="node-image",
                    title="场景参考图",
                    type="image",
                    status="completed",
                    model_config_json=json.dumps({"surface": "project_panel"}),
                    input_json=json.dumps({"source_node_id": "scene_ref"}),
                ),
                SimpleNamespace(
                    id="node-video",
                    title="最终视频",
                    type="video",
                    status="failed",
                    model_config_json=json.dumps({"surface": "project_panel"}),
                    input_json=None,
                ),
                SimpleNamespace(
                    id="node-text",
                    title="草稿文本",
                    type="text",
                    status="completed",
                    model_config_json=json.dumps({"surface": "draft_canvas"}),
                    input_json=None,
                ),
            ]

    orchestrator.node_service = FakeNodeService()
    summary = await orchestrator._compute_canvas_summary("project-1")

    assert summary["total"] == 3
    assert summary["by_surface"] == {"project_panel": 2, "draft_canvas": 1}
    assert summary["surface_details"]["project_panel"]["by_type"] == {
        "image": 1,
        "video": 1,
    }
    assert summary["surface_details"]["draft_canvas"]["by_type"] == {"text": 1}
    assert summary["node_refs"][0]["id"] == "node-image"
    assert set(summary["node_refs"][0]) == {"id", "type", "title", "status", "surface"}


def test_runtime_context_omits_node_refs_and_prompt_body() -> None:
    context = runtime_context.build({
        "metadata": {"title": "节点索引"},
        "_canvas_summary": {
            "total": 1,
            "by_type": {"image": 1},
            "by_status": {"completed": 1},
            "by_surface": {"project_panel": 1},
            "surface_details": {
                "project_panel": {"total": 1, "by_type": {"image": 1}, "by_status": {"completed": 1}},
                "draft_canvas": {"total": 0, "by_type": {}, "by_status": {}},
            },
            "node_refs": [
                {
                    "id": "node-image",
                    "type": "image",
                    "title": "宫格分镜",
                    "status": "completed",
                    "surface": "project_panel",
                    "source_node_id": "storyboard_grid",
                    "source_paths": ["/root/children/2/children/0/children/1"],
                    "prompt": "LEAK_PROMPT_BODY",
                }
            ],
        },
    })

    assert "节点定位索引" not in context
    assert "node-image" not in context
    assert "storyboard_grid" not in context
    assert "/root/children/2/children/0/children/1" not in context
    assert "LEAK_PROMPT_BODY" not in context


def test_runtime_context_shows_only_codex_style_skill_catalog() -> None:
    context = runtime_context.build({})

    assert "## Skills" in context
    assert "video_production" in context
    assert "### Available skills" in context
    assert "orchestrator resource" in context
    assert "skill://builtin/video_production/SKILL.md" in context
    assert "content_page" not in context
    assert len(context) <= 1_900

def test_deferred_file_tool_cannot_be_called_directly_by_agent_loop() -> None:
    direct = decide_tool_permission(ToolPermissionContext(
        tool_name="file.read_text",
        state={},
        user_message="读取上传的脚本",
        tool_args={"rel_path": "uploads/script.txt"},
    ))
    via_deferred = decide_tool_permission(ToolPermissionContext(
        tool_name="file.read_text",
        state={},
        user_message="读取上传的脚本",
        tool_args={"rel_path": "uploads/script.txt"},
        via_tool_execute=True,
    ))

    assert direct.allowed is False
    assert direct.result and direct.result["error_kind"] == "deferred_tool_must_use_tool_execute"
    assert via_deferred.allowed is True

@pytest.mark.asyncio
async def test_build_messages_after_session_clear_excludes_archived_history() -> None:
    holder = {"statement": ""}

    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(role="assistant", content="清除后的新回复"),
            ]

    class FakeDB:
        async def exec(self, statement):
            holder["statement"] = str(statement)
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages(
        "project-1",
        "画布上有几个节点？",
        include_history=True,
    )

    assert "archived" in holder["statement"].lower()
    assert "清除后的新回复" in json.dumps(messages, ensure_ascii=False)
    assert "清除前用户要求生成两人牵手图" not in json.dumps(messages, ensure_ascii=False)
    assert messages[-1] == {"role": "user", "content": "画布上有几个节点？"}

@pytest.mark.asyncio
async def test_build_messages_keeps_all_active_history_without_sliding_window() -> None:
    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(role="assistant", content=f"历史回复 {index:02d}")
                for index in range(20, 0, -1)
            ]

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages(
        "project-1",
        "继续刚才的要求",
        include_history=True,
    )
    body = json.dumps(messages, ensure_ascii=False)

    assert "历史回复 01" in body
    assert "历史回复 20" in body
    assert len(messages) == 21
    assert messages[-1] == {"role": "user", "content": "继续刚才的要求"}

@pytest.mark.asyncio
async def test_build_messages_excludes_slash_command_history_from_model_context() -> None:
    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(
                    role="assistant",
                    content="slash doctor dump should stay out",
                    metadata_json=json.dumps({"source": "slash_command", "command": "doctor"}, ensure_ascii=False),
                ),
                SimpleNamespace(
                    role="user",
                    content="/project list",
                    metadata_json=json.dumps({"source": "slash_command", "command": "project"}, ensure_ascii=False),
                ),
                SimpleNamespace(
                    role="assistant",
                    content="正常历史回复",
                    metadata_json=None,
                ),
                SimpleNamespace(
                    role="user",
                    content="显式允许进入模型的 slash",
                    metadata_json=json.dumps({
                        "source": "slash_command",
                        "command": "plan",
                        "model_visible": True,
                    }, ensure_ascii=False),
                ),
            ]

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages(
        "project-1",
        "继续",
        include_history=True,
    )
    body = json.dumps(messages, ensure_ascii=False)

    assert "slash doctor dump should stay out" not in body
    assert "/project list" not in body
    assert "正常历史回复" in body
    assert "显式允许进入模型的 slash" in body
    assert messages[-1] == {"role": "user", "content": "继续"}


@pytest.mark.asyncio
async def test_build_messages_window_zero_isolates_pending_confirmation_history() -> None:
    called = False

    class FakeDB:
        async def exec(self, statement):
            nonlocal called
            called = True
            raise AssertionError("pending-state isolation must not query old chat history")

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages(
        "project-1",
        "确认",
        include_history=False,
    )

    assert called is False
    assert messages == [{"role": "user", "content": "确认"}]

@pytest.mark.asyncio
async def test_maybe_compress_history_does_not_archive_short_history_by_message_count(monkeypatch) -> None:
    from app.mcp_tools import memory_tools

    called = False

    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(role="user", content=f"第 {index} 轮短消息")
                for index in range(40)
            ]

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    async def fake_compact(project_id: str, target_tail_tokens: int | None = None):
        nonlocal called
        called = True
        return {"archived": 1, "target_tail_tokens": target_tail_tokens}

    monkeypatch.setattr(memory_tools, "memory_compact_context", fake_compact)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    await orchestrator._maybe_compress_history("project-1")

    assert called is False

@pytest.mark.asyncio
async def test_maybe_compress_history_ignores_slash_command_output_tokens(monkeypatch) -> None:
    from app.mcp_tools import memory_tools

    called = False

    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(
                    role="assistant",
                    content="x" * 180000,
                    metadata_json=json.dumps({"source": "slash_command", "command": "doctor"}, ensure_ascii=False),
                ),
                SimpleNamespace(role="user", content="继续正常任务", metadata_json=None),
            ]

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    async def fake_compact(project_id: str, target_tail_tokens: int | None = None):
        nonlocal called
        called = True
        return {"archived": 1, "target_tail_tokens": target_tail_tokens}

    monkeypatch.setattr(memory_tools, "memory_compact_context", fake_compact)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    await orchestrator._maybe_compress_history("project-1")

    assert called is False


@pytest.mark.asyncio
async def test_maybe_compress_history_archives_only_when_token_threshold_is_exceeded(monkeypatch) -> None:
    from app.mcp_tools import memory_tools

    captured = {}

    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(role="user", content="x" * 180000),
            ]

    class FakeDB:
        async def exec(self, statement):
            return FakeResult()

    async def fake_compact(project_id: str, target_tail_tokens: int | None = None):
        captured["project_id"] = project_id
        captured["target_tail_tokens"] = target_tail_tokens
        return {"archived": 1, "target_tail_tokens": target_tail_tokens}

    monkeypatch.setattr(memory_tools, "memory_compact_context", fake_compact)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    await orchestrator._maybe_compress_history("project-1")

    assert captured == {"project_id": "project-1", "target_tail_tokens": None}


@pytest.mark.asyncio
async def test_memory_compact_context_persists_summary_not_sliding_tail(monkeypatch) -> None:
    from app.mcp_tools import memory_tools

    active_rows = [
        SimpleNamespace(id="m1", role="user", content="x" * 180000, archived=False),
        SimpleNamespace(id="m2", role="assistant", content="小尾部", archived=False),
    ]
    created_rows = []

    class FakeResult:
        def all(self):
            return active_rows

    class FakeSession:
        async def exec(self, statement):
            return FakeResult()

        async def get(self, model, row_id):
            return next((row for row in active_rows if row.id == row_id), None)

        def add(self, row):
            if row not in active_rows:
                created_rows.append(row)

        async def commit(self):
            return None

    class FakeSessionScope:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeLLMService:
        def __init__(self, session):
            self.session = session

        async def generate(self, *args, **kwargs):
            return {"content": "压缩后的背景摘要", "usage": {"total_tokens": 10}}

    monkeypatch.setattr(memory_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr("app.services.llm_service.LLMService", FakeLLMService)
    monkeypatch.setattr(memory_tools, "memory_summarize_conversation", AsyncMock(return_value={"facts": []}))

    result = await memory_tools.memory_compact_context("project-1", target_tail_tokens=100)

    assert result["summary_inserted"] is True
    assert result["archived"] == 2
    assert result["active"] == 3
    assert all(row.archived for row in active_rows)
    assert "<compacted_context kind=\"background_summary\">" in created_rows[0].content
    assert "压缩后的背景摘要" in created_rows[0].content
    assert created_rows[1].role == "assistant"
    assert created_rows[2].content == "小尾部"


@pytest.mark.asyncio
async def test_message_queue_removes_queued_message_by_client_id() -> None:
    project_id = "project-remove-queued"
    await mq.pop_all(project_id)
    try:
        await mq.enqueue(
            project_id,
            "保留",
            user_metadata={"clientUserMessageId": "keep"},
        )
        await mq.enqueue(
            project_id,
            "删除",
            user_metadata={"clientUserMessageId": "remove"},
        )

        removed = await mq.remove_queued(project_id, "remove")
        remaining = await mq.pop_all(project_id)

        assert removed == {"ok": True, "removed": True, "queued_count": 1}
        assert [item["message"] for item in remaining] == ["保留"]
    finally:
        await mq.pop_all(project_id)
        await mq.clear_cancel(project_id)


@pytest.mark.asyncio
async def test_orchestrator_stream_drains_queued_messages_before_final_done(monkeypatch) -> None:
    project_id = "project-queued-stream"
    await mq.pop_all(project_id)
    await mq.clear_cancel(project_id)
    calls: list[dict] = []

    async def fake_stream_one_turn(
        self,
        project_id_arg: str,
        message: str,
        attachments: list[dict] | None = None,
        referenced_node_ids: list[str] | None = None,
        display_message: str | None = None,
        user_metadata: dict | None = None,
    ):
        calls.append(
            {
                "project_id": project_id_arg,
                "message": message,
                "attachments": attachments,
                "referenced_node_ids": referenced_node_ids,
                "display_message": display_message,
                "user_metadata": user_metadata,
            }
        )
        yield {"type": "text_delta", "content": f"turn-{len(calls)}:{message}"}
        if len(calls) == 1:
            await mq.enqueue(
                project_id_arg,
                "追加消息一",
                [{"filename": "ref.png"}],
                referenced_node_ids=["node-queued-1"],
                user_metadata={
                    "clientUserMessageId": "client-1",
                    "decisionInputs": {"kind": "interaction_input", "values": {"topic": "雨夜"}},
                },
            )
            await mq.enqueue(
                project_id_arg,
                "追加消息二",
                [{"filename": "second.png"}],
                user_metadata={"clientUserMessageId": "client-2"},
            )
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr(AgentOrchestrator, "_stream_one_turn", fake_stream_one_turn)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    try:
        events = [
            event
            async for event in orchestrator.stream(
                project_id,
                "第一条消息",
                attachments=[{"filename": "first.png"}],
                referenced_node_ids=["node-first"],
                display_message="第一条显示消息",
                user_metadata={"source": "test"},
            )
        ]
    finally:
        await mq.pop_all(project_id)
        await mq.clear_cancel(project_id)

    done_indexes = [
        index for index, event in enumerate(events)
        if event.get("type") == "done"
    ]
    assert done_indexes == [len(events) - 1]
    assert [call["message"] for call in calls] == ["第一条消息", "追加消息一", "追加消息二"]
    assert calls[0]["attachments"] == [{"filename": "first.png"}]
    assert calls[0]["referenced_node_ids"] == ["node-first"]
    assert calls[0]["display_message"] == "第一条显示消息"
    assert calls[0]["user_metadata"] == {"source": "test"}
    assert calls[1]["attachments"] == [{"filename": "ref.png"}]
    assert calls[1]["referenced_node_ids"] == ["node-queued-1"]
    assert calls[1]["display_message"] is None
    assert calls[1]["user_metadata"] == {
        "clientUserMessageId": "client-1",
        "decisionInputs": {"kind": "interaction_input", "values": {"topic": "雨夜"}},
    }
    assert calls[2]["attachments"] == [{"filename": "second.png"}]
    assert calls[2]["referenced_node_ids"] == []
    assert calls[2]["display_message"] is None
    assert calls[2]["user_metadata"] == {"clientUserMessageId": "client-2"}
    assert any(event.get("type") == "merged_messages" for event in events)
    merged = next(event for event in events if event.get("type") == "merged_messages")
    assert merged["mode"] == "sequential_turn_inputs"
    assert "用户在我处理上一条期间又发了" not in str(merged)
    queued_starts = [event for event in events if event.get("type") == "queued_turn_started"]
    assert queued_starts == [
        {
            "type": "queued_turn_started",
            "client_user_message_id": "client-1",
            "message": "追加消息一",
            "queued_remaining": 1,
        },
        {
            "type": "queued_turn_started",
            "client_user_message_id": "client-2",
            "message": "追加消息二",
            "queued_remaining": 0,
        },
    ]
    assert events[-1] == {"type": "done", "status": "completed"}


def test_project_get_state_display_returns_detached_state() -> None:
    from app.mcp_tools import project_tools

    state = {"metadata": {"title": "节点项目", "episode_count": 3}}
    result = project_tools._project_state_for_status_display(state)

    assert result == state
    assert result is not state


def test_project_get_state_summarizes_large_runtime_collections() -> None:
    from app.mcp_tools import project_tools

    state = {
        "memory": {"facts": [{"pinned": True}, {"pinned": False}]},
        "active_workflow": {
            "kind": "imported",
            "workflow": {"id": "wf-1", "steps": [{"id": "a"}, {"id": "b"}]},
        },
        "workflow_runtime": {
            "instances": {
                "one": {"status": "running", "steps": {"large": "x" * 20_000}},
                "two": {"status": "completed"},
            }
        },
        "workflow_input_values": {
            "by_workflow": {"wf-1": {"values": {"script": "x" * 20_000}}},
            "by_instance": {"one": {"values": {"script": "x" * 20_000}}},
        },
        "episodes": [{"script": "x" * 20_000}],
        "director_desk": {
            "version": 1,
            "revision": 12,
            "captures": [{"pixels": "x" * 20_000}],
            "model_assets": [{"data": "x" * 20_000}],
        },
    }

    result = project_tools._project_state_for_status_display(state)

    assert "memory" not in result
    assert "workflow_runtime" not in result
    assert "workflow_input_values" not in result
    assert "episodes" not in result
    assert "director_desk" not in result
    assert result["memory_summary"] == {
        "fact_count": 2,
        "pinned_count": 1,
        "detail_tool": "memory.recall",
    }
    assert result["active_workflow"]["workflow_id"] == "wf-1"
    assert result["active_workflow"]["step_count"] == 2
    assert result["workflow_runtime_summary"]["instance_count"] == 2
    assert result["episodes_summary"]["count"] == 1
    assert result["director_desk_summary"]["capture_count"] == 1
    assert result["director_desk_summary"]["model_asset_count"] == 1


@pytest.mark.asyncio
async def test_project_get_state_overlays_db_workflow_snapshot(monkeypatch) -> None:
    from app.mcp_tools import project_tools

    class FakeSessionScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeProjectService:
        def __init__(self, session):
            self.session = session

        async def get_project_state(self, project_id: str):
            assert project_id == "proj-1"
            return {
                "metadata": {"title": "节点项目", "episode_count": 1},
                "workflow": {"nodes": [], "edges": []},
            }

    async def fake_canvas_summary(session, project_id: str):
        assert session is not None
        assert project_id == "proj-1"
        return {
            "node_count": 1,
            "edge_count": 1,
            "by_type": {"image": 1},
            "by_status": {"idle": 1},
            "detail_tool": "node.list",
            "hint": "Use node.list for a bounded node index page and node.get for selected details.",
        }

    monkeypatch.setattr(project_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(project_tools, "ProjectService", FakeProjectService)
    monkeypatch.setattr(project_tools, "_load_canvas_state_summary", fake_canvas_summary)

    result = await project_tools.project_get_state("proj-1")

    assert result["workflow"] == {
        "node_count": 1,
        "edge_count": 1,
        "by_type": {"image": 1},
        "by_status": {"idle": 1},
        "detail_tool": "node.list",
        "hint": "Use node.list for a bounded node index page and node.get for selected details.",
    }


@pytest.mark.asyncio
async def test_build_messages_after_full_reset_excludes_archived_project_history() -> None:
    holder = {"statement": ""}

    class FakeResult:
        def all(self):
            # Simulates DB state after full reset archived all previous rows:
            # only reset-after messages remain visible to prompt assembly.
            return [
                SimpleNamespace(role="assistant", content="项目已重置，可以开始新内容"),
            ]

    class FakeDB:
        async def exec(self, statement):
            holder["statement"] = str(statement)
            return FakeResult()

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.db = FakeDB()

    messages = await orchestrator._build_messages(
        "project-1",
        "你好",
        include_history=True,
    )
    body = json.dumps(messages, ensure_ascii=False)

    assert "archived" in holder["statement"].lower()
    assert "项目已重置，可以开始新内容" in body
    assert "重置前旧项目剧情" not in body
    assert "上一轮让两个人牵手" not in body
    assert messages[-1] == {"role": "user", "content": "你好"}

def test_runtime_context_does_not_auto_inject_memory_refs_or_bodies() -> None:
    user_secret = "用户之前要求继续生成牵手图"
    project_secret = "旧项目剧情要求自动生成视频"
    context = runtime_context.build(
        {"metadata": {"title": "记忆测试"}},
        user_facts=[
            {
                "id": "u-1",
                "kind": "preference",
                "content": user_secret,
                "pinned": True,
                "created_at": "2026-06-06T00:00:00Z",
            }
        ],
        project_facts=[
            {
                "id": "p-1",
                "kind": "summary",
                "content": project_secret,
                "pinned": False,
                "created_at": "2026-06-06T00:00:00Z",
            }
        ],
    )

    assert "用户长期偏好索引" not in context
    assert "本项目长期事实索引" not in context
    assert "u-1" not in context
    assert "p-1" not in context
    assert "body_policy" not in context
    assert user_secret not in context
    assert project_secret not in context

def test_prompt_cache_key_uses_memory_refs_not_memory_bodies() -> None:
    ctx_a = PromptContext(
        project_id="project-1",
        user_facts=[{"id": "u-1", "kind": "preference", "content": "旧内容 A", "pinned": True}],
        project_facts=[{"id": "p-1", "kind": "summary", "content": "旧内容 B"}],
    )
    ctx_b = PromptContext(
        project_id="project-1",
        user_facts=[{"id": "u-1", "kind": "preference", "content": "完全不同正文", "pinned": True}],
        project_facts=[{"id": "p-1", "kind": "summary", "content": "另一个旧正文"}],
    )

    key = ctx_a.cache_key()

    assert ctx_a.cache_key() == ctx_b.cache_key()
    assert "旧内容 A" not in key
    assert "完全不同正文" not in key

def test_reset_canvas_events_prefers_clear_all() -> None:
    assert reset_canvas_events({"cleared_all": True, "deleted_node_ids": ["a"]}) == [
        {"type": "canvas_action", "action": "clear_all", "payload": {}}
    ]
    assert reset_canvas_events({"deleted_node_ids": ["a", "", "b"]}) == [
        {"type": "canvas_action", "action": "delete_node", "payload": {"id": "a"}},
        {"type": "canvas_action", "action": "delete_node", "payload": {"id": "b"}},
    ]

def test_full_reset_context_keys_clear_reference_assets() -> None:
    assert "reference_assets" in drama_tools._FULL_RESET_CONTEXT_KEYS

def test_permission_policy_does_not_use_plan_submission_as_precondition() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.create",
            state={"agent_collaboration_mode": "plan"},
            user_message="做一个短剧视频",
        )
    )

    assert decision.allowed is False
    assert decision.result
    assert decision.result["error_kind"] == "plan_mode_read_only"

def test_permission_policy_does_not_use_read_only_semantic_intent_gate() -> None:
    create = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.create",
            state={},
            user_message="画布上有几个节点",
        )
    )
    get_node = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.get",
            state={},
            user_message="画布上有几个节点",
        )
    )
    read = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.list",
            state={},
            user_message="画布上有几个节点",
        )
    )

    assert create.allowed is True
    assert get_node.allowed is True
    assert read.allowed is True

def test_permission_policy_does_not_use_destructive_semantic_intent_gate() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="canvas.delete",
            state={},
            user_message="创建一个新节点，不用删已有节点",
        )
    )

    assert decision.allowed is True








def test_permission_denial_streak_blocks_after_repeated_same_reason() -> None:
    state = PermissionDenialState()
    blocked = False
    result = {"ok": False, "error_kind": "checklist_violation"}

    for _ in range(3):
        state, blocked = next_permission_denial_state(
            state,
            "node.create",
            result,
        )

    assert state.key == ("node.create", "checklist_violation")
    assert state.count == 3
    assert blocked is True

def test_permission_denial_streak_resets_for_different_reason() -> None:
    state, blocked = next_permission_denial_state(
        PermissionDenialState(),
        "node.create",
        {"ok": False, "error_kind": "checklist_violation"},
    )
    state, blocked = next_permission_denial_state(
        state,
        "node.create",
        {"ok": False, "error_kind": "plan_pending_approval"},
    )

    assert state.key == ("node.create", "plan_pending_approval")
    assert state.count == 1
    assert blocked is False

def test_permission_policy_allows_reset_tool_without_semantic_intent() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="project.reset",
            state={},
            user_message="start over",
        )
    )

    assert decision.allowed is True

def test_permission_policy_ignores_compound_semantic_intent_fields() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="project.reset",
            state={},
            user_message="start fresh and create a character node",
        )
    )

    assert decision.allowed is True
