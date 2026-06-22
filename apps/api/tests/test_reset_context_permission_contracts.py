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

    assert has_state_continuation_context(state) is False
    assert chat_history_visible_for_turn(state) is True

@pytest.mark.parametrize(
    "state",
    [
        {"pending_video_blueprint_request": {"stage": "structure"}},
        {"pending_blueprint_revision": {"status": "pending_review"}},
        {"blueprint_generation_progress": {"status": "paused_for_section_review"}},
    ],
)
def test_context_policy_tracks_state_continuation_without_chat_history_for_pending_state(state: dict) -> None:
    assert has_state_continuation_context(state) is True
    assert chat_history_visible_for_turn(state) is False

def test_context_policy_ignores_legacy_active_execution_checklist() -> None:
    state = {"active_plan_checklist": [{"status": "pending", "title": "继续执行"}]}

    assert has_state_continuation_context(state) is False
    assert chat_history_visible_for_turn(state) is True

def test_stale_blueprint_flow_state_patch_only_clears_draft_state() -> None:
    state = {
        "project_blueprint": {"id": "bp-1", "status": "active"},
        "pending_video_blueprint_request": {"stage": "structure"},
        "pending_blueprint_section_review": {"next_section_index": 2},
        "blueprint_window_progress": {"status": "failed"},
        "pending_plan": {"kind": "creative_blueprint", "id": "plan-old"},
        "pending_plan_preview_checklist": [{"title": "旧蓝图待确认"}],
        "pending_blueprint_draft": {"id": "draft-old"},
        "pending_blueprint_review": {"id": "review-old"},
        "pending_blueprint_revision": {"id": "rev-1"},
        "active_plan_checklist": [{"title": "真实执行任务", "status": "pending"}],
    }

    patch = orchestrator_module._stale_blueprint_flow_state_patch(state)

    assert patch == {
        "pending_video_blueprint_request": None,
        "pending_blueprint_section_review": None,
        "blueprint_window_progress": None,
        "pending_plan": None,
        "pending_plan_preview_checklist": None,
        "active_plan_checklist": None,
        "pending_blueprint_draft": None,
        "pending_blueprint_review": None,
    }
    assert "pending_blueprint_revision" not in patch
    assert "project_blueprint" not in patch

def test_project_reset_is_core_and_hides_internal_confirm_token() -> None:
    spec = registry.get("project.reset")

    assert "project.reset" in registry._CORE_AGENT_TOOLS
    assert spec is not None
    assert "_confirm_token" not in (spec.schema.get("properties") or {})
    assert set((spec.schema.get("properties") or {}).keys()) == {"scope", "reason", "new_theme"}

def test_project_reset_permission_allows_pending_plan_interrupt() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="project.reset",
            state={"pending_plan": {"id": "plan-1", "status": "pending"}},
            user_message="重置项目",
        )
    )

    assert decision.allowed is True

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

def test_reset_confirmation_text_names_blueprint_draft_tasks_panel_canvas_and_title() -> None:
    text = reset_confirmation_text()

    for phrase in ("项目蓝图", "蓝图草稿", "任务", "面板", "画布", "未命名项目"):
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

def test_full_reset_context_keys_cover_model_visible_project_state() -> None:
    required = {
        "memory",
        "project_mode",
        "project_sub_mode",
        "selected_video_mode",
        "active_plan_checklist",
        "pending_plan",
        "plan_history",
        "pending_blueprint_draft",
        "pending_blueprint_section_review",
        "pending_blueprint_revision",
        "blueprint_generation_progress",
        "blueprint_window_progress",
        "panel_layout",
        "agent_token_usage",
        "_mentor_guides_loaded",
        "_skills_loaded",
        "_last_template_lookup",
        "_last_agent_review",
        "_pending_reset_confirm",
        "_pending_tool_confirm",
    }

    assert required.issubset(set(drama_tools._FULL_RESET_CONTEXT_KEYS))

def test_full_reset_state_cleanup_removes_prompt_visible_project_context() -> None:
    state = {
        "metadata": {
            "title": "旧蓝图标题",
            "genre": "国风动作",
            "description": "旧项目说明",
            "theme": "旧主题",
            "world_setting": "旧世界观",
        },
        "project_blueprint": {
            "id": "bp-old",
            "theme_title": "旧蓝图标题",
            "short_summary": "旧剧情摘要",
        },
        "active_plan_checklist": [
            {"title": "旧任务：生成分镜", "status": "pending"}
        ],
        "pending_plan": {"id": "plan-old", "summary": "旧计划摘要"},
        "memory": {
            "facts": [
                {"content": "旧用户要求生成两人牵手图", "pinned": True}
            ]
        },
        "project_sub_mode": None,
        "panel_layout": {"mode": "old"},
        "_pending_reset_confirm": {"scope": "full"},
        "_pending_tool_confirm": {"target": "canvas.delete", "reason": "旧清空画布确认"},
        "story_bible": {
            "logline": "旧故事线",
            "theme": "旧主题",
            "tone": "旧调性",
            "world_setting": "旧世界观",
            "visual_style": "旧视觉",
        },
    }

    for key in drama_tools._FULL_RESET_CONTEXT_KEYS:
        state.pop(key, None)
    clear_blueprint_state(state)
    meta = state.get("metadata") or {}
    meta["title"] = project_blueprint.UNTITLED_PROJECT_TITLE
    for key in ("genre", "description", "logline", "theme", "world_setting"):
        meta[key] = ""
    state["metadata"] = meta
    state["story_bible"] = {
        "logline": "",
        "theme": "",
        "tone": "",
        "world_setting": "",
        "visual_style": "",
    }

    context = runtime_context.build(state)
    context_json = json.dumps(state, ensure_ascii=False)

    for leak in (
        "旧蓝图标题",
        "旧剧情摘要",
        "旧任务：生成分镜",
        "旧计划摘要",
        "旧用户要求生成两人牵手图",
        "grid",
        "old",
        "旧故事线",
        "旧清空画布确认",
    ):
        assert leak not in context
        assert leak not in context_json
    assert project_blueprint.UNTITLED_PROJECT_TITLE in context


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
                    input_json=json.dumps({
                        "blueprint_node_id": "scene_ref",
                        "source_blueprint_paths": ["/root/children/1"],
                    }),
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
    assert summary["node_refs"][0]["blueprint_node_id"] == "scene_ref"
    assert summary["node_refs"][0]["source_blueprint_paths"] == ["/root/children/1"]


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
                    "blueprint_node_id": "storyboard_grid",
                    "source_blueprint_paths": ["/root/children/2/children/0/children/1"],
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


def test_session_clear_state_patch_keeps_artifacts_but_drops_unpinned_memory() -> None:
    pinned = {
        "id": "fact-keep",
        "kind": "preference",
        "content": "固定风格偏好",
        "pinned": True,
    }
    state = {
        "project_blueprint": {"id": "bp-1"},
        "active_plan_checklist": [{"title": "保留任务"}],
        "workflow": {"nodes": [{"id": "node-1"}]},
        "session": {"focus": "old"},
        "guide_loaded": {"node": True},
        "_mentor_guides_loaded": {
            "video_workflow": {
                "topic": "video_workflow",
                "guidance_summary": "旧视频工作流指南摘要",
                "guidance_hash": "oldhash",
            }
        },
        "_skills_loaded": {
            "video_production": {
                "skill": "video_production",
                "summary": "旧视频 skill 摘要",
                "guidance_hash": "oldskillhash",
            }
        },
        "_skills_loaded": {
            "video_production": {
                "skill": "video_production",
                "summary": "旧视频 skill 摘要",
                "guidance_hash": "oldskillhash",
            }
        },
        "memory": {
            "facts": [
                pinned,
                {
                    "id": "fact-drop",
                    "kind": "summary",
                    "content": "上一轮要生成两人牵手图",
                    "pinned": False,
                },
            ]
        },
    }

    patch, removed = routes_projects._session_clear_state_patch(
        state,
        cleared_at="2026-06-05T00:00:00",
    )

    assert removed == 1
    assert patch == {
        "session": {},
        "guide_loaded": {},
        "_mentor_guides_loaded": {},
        "_skills_loaded": {},
        "_last_template_lookup": None,
        "_last_agent_review": None,
        "memory": {"facts": [pinned]},
        "agent_token_usage": None,
        "context_cleared_at": "2026-06-05T00:00:00",
    }
    assert "project_blueprint" not in patch
    assert "active_plan_checklist" not in patch
    assert "workflow" not in patch

def test_session_clear_preserves_blueprint_and_next_task_runtime_context() -> None:
    state = {
        "metadata": {"title": "旧项目"},
        "project_blueprint": {
            "id": "bp-1",
            "status": "active",
            "theme_title": "剑影竹风",
            "version": 2,
            "duration_seconds": 15,
            "short_summary": "侠客在竹林完成一次反转突围。",
            "file_markdown": "data/projects/p1/blueprint.md",
            "file_json": "data/projects/p1/blueprint.json",
        },
        "active_plan_checklist": [
            {
                "step_id": "step-1",
                "title": "生成段落分镜",
                "tool": "node.run",
                "expected_node_type": "image",
                "status": "pending",
            }
        ],
        "session": {"last_step": "旧聊天里失败的图片生成"},
        "guide_loaded": {"old": True},
        "_mentor_guides_loaded": {
            "video_workflow": {
                "topic": "video_workflow",
                "guidance_summary": "旧视频工作流指南摘要",
                "guidance_hash": "oldhash",
            }
        },
        "memory": {
            "facts": [
                {
                    "id": "fact-drop",
                    "content": "上一轮用户要生成两人牵手图",
                    "pinned": False,
                }
            ]
        },
    }

    patch, removed = routes_projects._session_clear_state_patch(
        state,
        cleared_at="2026-06-05T00:00:00",
    )
    next_state = {**state, **patch}

    assert removed == 1
    assert has_state_continuation_context(next_state) is False
    assert chat_history_visible_for_turn(next_state) is True

    context = runtime_context.build(next_state)

    assert "剑影竹风" not in context
    assert "生成段落分镜" not in context
    assert "旧聊天里失败的图片生成" not in context
    assert "上一轮用户要生成两人牵手图" not in context
    assert "旧视频工作流指南摘要" not in context
    assert "旧视频 skill 摘要" not in context

def test_runtime_context_omits_project_mentor_digest() -> None:
    context = runtime_context.build({
        "_mentor_guides_loaded": {
            "video_workflow": {
                "topic": "video_workflow",
                "detail": "summary",
                "has_full_guide": True,
                "guidance_summary": "视频制作先收集主题、风格、时长和参考素材。",
                "guidance_hash": "abc123def456",
                "references_count": 3,
                "loaded_at": 1234567890,
            }
        }
    })

    assert "指南复用缓存" not in context
    assert "video_workflow" not in context
    assert "abc123def456" not in context
    assert "视频制作先收集主题" not in context
    assert "loaded_at" not in context
    assert "1234567890" not in context

def test_runtime_context_shows_loaded_skill_marker_without_summary_body() -> None:
    context = runtime_context.build({
        "_skills_loaded": {
            "video_production": {
                "skill": "video_production",
                "tool": "skill.video_production",
                "detail": "summary",
                "summary": "视频制作先写剧本，再做人设、场景、分镜和视频节点。",
                "guidance_hash": "skill123",
                "guidance_chars": 1234,
            }
        }
    })

    assert "Skill 复用提醒" in context
    assert "video_production" in context
    assert "skill123" in context
    assert "视频制作先写剧本" not in context
    assert "用户换流程" not in context

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
                user_metadata={"decisionInputs": {"kind": "interaction_input", "values": {"topic": "雨夜"}}},
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
    assert events[-1] == {"type": "done", "status": "completed"}


def test_project_get_state_display_hides_default_episode_count_without_blueprint() -> None:
    from app.mcp_tools import project_tools

    result = project_tools._project_state_for_status_display(
        {
            "metadata": {
                "title": "未命名项目",
                "episode_count": 1,
            },
            "outline": {"episodes": []},
        }
    )

    assert "episode_count" not in result["metadata"]


def test_project_get_state_display_keeps_episode_count_with_blueprint() -> None:
    from app.mcp_tools import project_tools

    result = project_tools._project_state_for_status_display(
        {
            "metadata": {
                "title": "蓝图项目",
                "episode_count": 3,
            },
            "project_blueprint": {
                "id": "bp-1",
                "status": "active",
            },
        }
    )

    assert result["metadata"]["episode_count"] == 3


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
                "metadata": {"title": "蓝图项目", "episode_count": 1},
                "workflow": {"nodes": [], "edges": []},
                "project_blueprint": {"status": "materialized"},
            }

    async def fake_list_nodes(project_id: str):
        assert project_id == "proj-1"
        return [{"id": "node-1", "type": "image", "status": "idle"}]

    async def fake_list_edges(project_id: str):
        assert project_id == "proj-1"
        return [{"id": "edge-1", "source": "node-1", "target": "node-2"}]

    monkeypatch.setattr(project_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(project_tools, "ProjectService", FakeProjectService)
    monkeypatch.setattr(project_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(project_tools.canvas_tools, "list_edges", fake_list_edges)

    result = await project_tools.project_get_state("proj-1")

    assert result["workflow"] == {
        "nodes": [{"id": "node-1", "type": "image", "status": "idle"}],
        "edges": [{"id": "edge-1", "source": "node-1", "target": "node-2"}],
    }


@pytest.mark.asyncio
async def test_project_get_state_exposes_semantic_blueprint_draft(monkeypatch) -> None:
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
            return {"metadata": {"title": "未命名项目"}, "workflow": {"nodes": [], "edges": []}}

    async def fake_list_nodes(project_id: str):
        return []

    async def fake_list_edges(project_id: str):
        return []

    monkeypatch.setattr(project_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(project_tools, "ProjectService", FakeProjectService)
    monkeypatch.setattr(project_tools.canvas_tools, "list_nodes", fake_list_nodes)
    monkeypatch.setattr(project_tools.canvas_tools, "list_edges", fake_list_edges)
    monkeypatch.setattr(
        project_tools,
        "summarize_blueprint_for_state",
        lambda project_id: {
            "status": "drafting",
            "title": "草稿蓝图",
            "tree_version": 7,
            "node_count": 5,
            "needs_finalize": True,
        },
    )

    result = await project_tools.project_get_state("proj-1")

    assert result["semantic_blueprint"]["status"] == "drafting"
    assert result["suggested_next"] == "continue_from_existing_legacy_blueprint"
    assert "node.list" in result["model_feedback"]["how_to_fix"]
    assert "blueprint.finalize_tree_draft" not in result["model_feedback"]["how_to_fix"]


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
    assert "重置前旧蓝图剧情" not in body
    assert "上一轮让两个人牵手" not in body
    assert messages[-1] == {"role": "user", "content": "你好"}

def test_runtime_context_omits_pending_blueprint_refs_without_body_leakage() -> None:
    leak = "LEAK_PENDING_BODY"
    state = {
        "metadata": {
            "title": "蓝图项目",
            "project_mode": "video_production",
            "oversized": leak,
        },
        "pending_plan": {
            "id": "plan-1",
            "kind": "creative_blueprint",
            "title": "待确认蓝图",
            "status": "pending",
            "summary": leak,
            "plan_doc": {"sections": [{"content": leak}]},
            "blueprint_checksum": "bp-check-1",
        },
        "pending_blueprint_draft": {
            "id": "draft-1",
            "version": 2,
            "status": "pending_review",
            "checksum": "draft-check-1",
            "file_json": "data/projects/p1/draft.json",
            "short_summary": leak,
        },
        "pending_blueprint_review": {
            "id": "draft-1",
            "version": 2,
            "status": "pending_review",
            "checksum": "draft-check-1",
            "file_markdown": "data/projects/p1/draft.md",
            "outline_document": leak,
        },
        "pending_blueprint_section_review": {
            "next_section_index": 4,
            "review_mode": "section_step_review",
            "failed_generation": {"message": leak},
            "window_progress": {"status": "failed", "failure_reason": leak, "windows": [{"content": leak}]},
        },
        "pending_blueprint_revision": {
            "id": "rev-1",
            "status": "pending_review",
            "version": 3,
            "checksum": "rev-check-1",
            "source_paths": ["story.episodes[0].segments[0].plot"],
            "draft_doc": {"content": leak},
        },
        "pending_video_blueprint_request": {
            "stage": "structure",
            "selected_mode": "grid",
            "duration_seconds": 15,
            "raw_request": leak,
            "last_submitted_stage": "structure",
            "basic_answer": "视频主题或核心事件：雨夜石桥决斗",
            "structure_answer": "剧情大纲：少年剑客救人后反杀蒙面刺客",
            "structure_answers": [
                {"id": "plot_outline", "label": "剧情大纲", "value": "少年剑客救人后反杀蒙面刺客"},
                {"id": "segment_seconds", "label": "分段", "value": "不分段/单段连续"},
            ],
        },
        "blueprint_generation_progress": {
            "status": "drafting",
            "current_section": "segment_breakdown",
            "sections": [
                {"section_id": "requirements_digest", "status": "completed", "content": leak},
                {"section_id": "segment_breakdown", "status": "pending", "content": leak},
            ],
        },
        "blueprint_window_progress": {
            "status": "failed",
            "failed_window_index": 0,
            "failure_reason": leak,
            "windows": [{"content": leak}],
        },
    }

    context = runtime_context.build(state)

    assert "项目标题" in context
    assert "蓝图项目" in context
    assert "plan-1" not in context
    assert "draft-check-1" not in context
    assert "rev-check-1" not in context
    assert "sections_total" not in context
    assert "has_raw_request" not in context
    assert "少年剑客救人后反杀蒙面刺客" not in context
    assert "故事模板图" not in context
    assert "selected_mode" not in context
    assert leak not in context
    assert "outline_document" not in context
    assert "plan_doc" not in context
    assert "draft_doc" not in context

def test_runtime_context_does_not_auto_inject_memory_refs_or_bodies() -> None:
    user_secret = "用户之前要求继续生成牵手图"
    project_secret = "旧项目剧情要求自动生成蓝图"
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

def test_clear_blueprint_state_removes_project_blueprint_keys() -> None:
    state = {
        "project_blueprint": {"id": "bp-1"},
        "blueprint_progress": {"done": 1},
        "pending_blueprint_intake": {"stage": "basic"},
        "pending_blueprint_draft": {"section": "story"},
        "pending_blueprint_revision": {"path": "story.episodes[0]"},
        "pending_blueprint_confirmation": {"id": "plan-1", "status": "pending"},
        "semantic_blueprint": {"status": "drafting"},
        "blueprint_partial_plan_doc": {"id": "partial-1"},
        "blueprint_generation_progress": {"current_section": "segment_breakdown"},
        "blueprint_stale_nodes": ["node-1"],
        "blueprint_history": [{"id": "bp-1"}],
        "creative_blueprint_history": [{"id": "plan-1"}],
        "metadata": {"title": "保留标题由 reset 负责"},
    }

    cleared = clear_blueprint_state(state)

    assert "project_blueprint" in cleared
    assert "blueprint_progress" in cleared
    assert "pending_blueprint_intake" in cleared
    assert "pending_blueprint_draft" in cleared
    assert "pending_blueprint_revision" in cleared
    assert "pending_blueprint_confirmation" in cleared
    assert "semantic_blueprint" in cleared
    assert "blueprint_partial_plan_doc" in cleared
    assert "blueprint_generation_progress" in cleared
    assert "blueprint_stale_nodes" in cleared
    assert "blueprint_history" in cleared
    assert "creative_blueprint_history" in cleared
    assert "project_blueprint" not in state
    assert "pending_blueprint_confirmation" not in state
    assert "semantic_blueprint" not in state
    assert "blueprint_partial_plan_doc" not in state
    assert state["metadata"]["title"] == "保留标题由 reset 负责"

def test_full_reset_context_keys_clear_reference_assets() -> None:
    assert "reference_assets" in drama_tools._FULL_RESET_CONTEXT_KEYS

def test_full_reset_context_keys_clear_runtime_blueprint_residue() -> None:
    for key in (
        "pending_blueprint_confirmation",
        "semantic_blueprint",
        "blueprint_partial_plan_doc",
        "_template_lookups_by_category",
    ):
        assert key in drama_tools._FULL_RESET_CONTEXT_KEYS

def test_delete_blueprint_files_removes_data_and_storage_artifacts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(project_blueprint.settings, "PROJECT_ROOT", str(tmp_path))
    project_id = "reset-files-project"
    expected_abs_paths: list[str] = []

    for root in ("data", "storage"):
        paths = project_blueprint.blueprint_paths(project_id, root=root)
        for key in (
            "json_abs",
            "markdown_abs",
            "draft_json_abs",
            "draft_markdown_abs",
            "revision_json_abs",
            "revision_markdown_abs",
            "view_model_abs",
        ):
            path = Path(paths[key])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{key}\n", encoding="utf-8")
            expected_abs_paths.append(str(path))

    deleted = project_blueprint.delete_blueprint_files(project_id)

    assert set(deleted) == set(expected_abs_paths)
    assert all(not Path(path).exists() for path in expected_abs_paths)

def test_delete_blueprint_files_report_surfaces_delete_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(project_blueprint.settings, "PROJECT_ROOT", str(tmp_path))
    project_id = "reset-file-error-project"
    paths = project_blueprint.blueprint_paths(project_id)
    json_path = Path(paths["json_abs"])
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("{}\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args, **kwargs):
        if self == json_path:
            raise PermissionError("denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    report = project_blueprint.delete_blueprint_files_report(project_id)

    assert report["deleted"] == []
    assert report["errors"] == [
        {
            "path": str(json_path),
            "error": "denied",
            "error_kind": "PermissionError",
        }
    ]
    assert json_path.exists()

def test_reset_success_text_reports_blueprint_file_delete_errors() -> None:
    from app.agent.reset_flow import reset_success_text

    text = reset_success_text(
        {
            "deleted_node_ids": [],
            "blueprint_file_delete_errors": [{"path": "/tmp/blueprint.json", "error": "denied"}],
        },
        "full",
    )

    assert "1 个蓝图文件" in text
    assert "权限" in text

def test_permission_policy_allows_node_creation_without_plan() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.create",
            state={},
            user_message="做一个短剧视频",
            requires_plan=True,
        )
    )

    assert decision.allowed is True

def test_permission_policy_tool_sets_follow_registry_exposure() -> None:
    from app.agent import permission_policy

    registered = registry.registered_tool_names()
    hidden = registry.agent_hidden_tool_names()
    for set_name, tool_names in permission_policy.permission_policy_tool_sets().items():
        assert tool_names - registered == set(), set_name
        assert tool_names & hidden == set(), set_name

    policy_sets = permission_policy.permission_policy_tool_sets()
    assert permission_policy.plan_mode_allowed_tools() == policy_sets["plan_mode_allowed"]

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

def test_permission_policy_allows_readonly_preparation_tools_during_pending_plan() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.list",
            state={"pending_plan": {"id": "plan-1"}},
            user_message="继续",
        )
    )

    assert decision.allowed is True
    assert decision.result is None

def test_permission_policy_does_not_use_destructive_semantic_intent_gate() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="canvas.delete",
            state={},
            user_message="创建一个新节点，不用删已有节点",
        )
    )

    assert decision.allowed is True

def test_permission_policy_allows_active_checklist_autonomy() -> None:
    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.create",
            state={
                "active_plan_checklist": [
                    {
                        "status": "pending",
                        "title": "运行人物节点",
                        "tool": "node.run",
                        "expected_node_type": "character",
                    }
                ]
            },
            user_message="继续",
        )
    )

    assert decision.allowed is True
    assert decision.result is None

def test_permission_policy_allows_current_checklist_step_and_read_tools() -> None:
    state = {
        "active_plan_checklist": [
            {"status": "pending", "title": "运行人物节点", "tool": "node.run"}
        ]
    }
    current = decide_tool_permission(
        ToolPermissionContext(tool_name="node.run", state=state, user_message="继续")
    )
    read = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.list",
            state=state,
            user_message="继续",
        )
    )

    assert current.allowed is True
    assert read.allowed is True


def test_permission_policy_allows_repair_of_prior_checklist_node() -> None:
    state = {
        "active_plan_checklist": [
            {
                "step": 14,
                "status": "completed",
                "title": "渲染人物图",
                "tool": "node.run",
                "actual_node_id": "failed-scene",
            },
            {
                "step": 18,
                "status": "pending",
                "title": "创建分镜",
                "tool": "node.create",
                "expected_node_type": "image",
            },
        ]
    }

    update = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.update",
            state=state,
            user_message="继续",
            tool_args={"node_id": "failed-scene", "patch": {"input_json": {"resolution": "1k"}}},
        )
    )
    rerun = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.run",
            state=state,
            user_message="继续",
            tool_args={"node_id": "failed-scene", "action": "render"},
        )
    )

    assert update.allowed is True
    assert rerun.allowed is True


def test_permission_policy_allows_wrong_node_for_current_run_step() -> None:
    state = {
        "active_plan_checklist": [
            {
                "step": 12,
                "status": "completed",
                "tool": "node.create",
                "actual_node_id": "character-1",
            },
            {
                "step": 14,
                "status": "pending",
                "title": "渲染人物图",
                "tool": "node.run",
                "expected_node_ref_step": 12,
                "expected_action": "render",
            },
        ]
    }

    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.run",
            state=state,
            user_message="继续",
            tool_args={"node_id": "scene-1", "action": "render"},
        )
    )

    assert decision.allowed is True
    assert decision.result is None


def test_permission_policy_allows_render_when_current_step_is_default_run() -> None:
    state = {
        "active_plan_checklist": [
            {
                "step": 12,
                "status": "completed",
                "tool": "node.create",
                "actual_node_id": "character-1",
            },
            {
                "step": 13,
                "status": "pending",
                "title": "生成人物提示词",
                "tool": "node.run",
                "expected_node_ref_step": 12,
                "expected_action": "__default__",
            },
        ]
    }

    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.run",
            state=state,
            user_message="继续",
            tool_args={"node_id": "character-1", "action": "render"},
        )
    )

    assert decision.allowed is True
    assert decision.result is None


def test_permission_policy_allows_repair_of_future_pending_node() -> None:
    state = {
        "active_plan_checklist": [
            {"step": 1, "status": "pending", "tool": "node.run", "actual_node_id": "current"},
            {"step": 2, "status": "pending", "tool": "node.run", "actual_node_id": "future"},
        ]
    }

    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.run",
            state=state,
            user_message="继续",
            tool_args={"node_id": "future"},
        )
    )

    assert decision.allowed is True
    assert decision.result is None


def test_permission_policy_allows_repair_of_future_failed_node_before_current_pending() -> None:
    state = {
        "active_plan_checklist": [
            {"step": 1, "status": "pending", "tool": "node.run", "actual_node_id": "current"},
            {"step": 2, "status": "failed", "tool": "node.run", "actual_node_id": "future-failed"},
        ]
    }

    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.update",
            state=state,
            user_message="继续",
            tool_args={"node_id": "future-failed", "patch": {"input_json": {"resolution": "1k"}}},
        )
    )

    assert decision.allowed is True
    assert decision.result is None


def test_permission_policy_allows_later_pending_step_when_prior_step_failed() -> None:
    state = {
        "active_plan_checklist": [
            {
                "step": 17,
                "status": "failed",
                "title": "渲染场景图",
                "tool": "node.run",
                "actual_node_id": "scene-failed",
                "expected_action": "render",
            },
            {
                "step": 18,
                "status": "pending",
                "title": "创建分镜",
                "tool": "node.create",
                "expected_node_type": "image",
            },
        ]
    }

    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.create",
            state=state,
            user_message="继续",
            tool_args={"type": "image"},
        )
    )

    assert decision.allowed is True
    assert decision.result is None


def test_permission_policy_allows_prior_failed_node_repair_before_later_pending() -> None:
    state = {
        "active_plan_checklist": [
            {
                "step": 17,
                "status": "failed",
                "title": "渲染场景图",
                "tool": "node.run",
                "actual_node_id": "scene-failed",
                "expected_action": "render",
            },
            {
                "step": 18,
                "status": "pending",
                "title": "创建分镜",
                "tool": "node.create",
                "expected_node_type": "image",
            },
        ]
    }

    update = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.update",
            state=state,
            user_message="继续",
            tool_args={"node_id": "scene-failed", "patch": {"input_json": {"resolution": "1k"}}},
        )
    )
    rerun = decide_tool_permission(
        ToolPermissionContext(
            tool_name="node.run",
            state=state,
            user_message="继续",
            tool_args={"node_id": "scene-failed", "action": "render"},
        )
    )

    assert update.allowed is True
    assert rerun.allowed is True

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

def test_pre_tool_use_hook_does_not_stop_on_active_checklist_autonomy() -> None:
    state = PermissionDenialState()
    hook_result = None
    ctx = ToolPermissionContext(
        tool_name="node.create",
        state={
            "active_plan_checklist": [
                {"status": "pending", "title": "运行当前节点", "tool": "node.run"}
            ]
        },
        user_message="继续",
    )

    for _ in range(3):
        hook_result = run_pre_tool_use(ctx, state)
        state = hook_result.denial_state

    assert hook_result is not None
    assert hook_result.allowed is True
    assert hook_result.should_stop is False
    assert hook_result.error_kind == ""
    assert hook_result.result is None

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
