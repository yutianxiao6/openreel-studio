from agent_plan_contract_helpers import *  # noqa: F401,F403
from app.agent.prompts import identity, working_loop

def test_single_image_request_does_not_require_backend_plan_mode() -> None:
    ctx = PromptContext(
        project_id="test",
        user_message="生成一张赛博朋克街道的图片",
        state={},
    )
    assert should_require_plan(ctx) is False
    assert trigger_matches("complex_no_skip", ctx) is False

def test_prompt_sections_do_not_route_by_image_intent_or_loaded_content() -> None:
    ctx = PromptContext(
        project_id="test",
        user_message="生成一张女主的人物图",
        state={},
        has_script=True,
        has_characters=True,
    )
    assert should_require_plan(ctx) is False
    assert trigger_matches("complex_no_skip", ctx) is False

def test_unknown_business_prompt_triggers_are_not_loaded_automatically() -> None:
    ctx = PromptContext(
        project_id="test",
        user_message="做一段15秒的视频",
        state={},
    )

    result = assemble_split_result(ctx)
    loaded_triggers = {section.trigger for section in result.sections}

    assert "always" in loaded_triggers
    assert "factory" in loaded_triggers
    assert result.tool_namespaces == tuple(select_tool_namespaces(ctx))
    assert not {
        "assets",
        "complex",
        "complex_no_skip",
        "create",
        "first_contact",
        "introspect",
        "rerun",
        "template",
        "video",
    } & loaded_triggers

def test_workflow_build_prompt_loads_only_in_workflow_build_mode() -> None:
    default_result = assemble_split_result(PromptContext(
        project_id="workflow-build-default",
        user_message="搭建一个文生视频工作流",
        state={},
    ))
    workflow_result = assemble_split_result(PromptContext(
        project_id="workflow-build-mode",
        user_message="搭建一个文生视频工作流",
        state={},
        collaboration_mode="workflow_build",
    ))

    default_triggers = {section.trigger for section in default_result.sections}
    workflow_triggers = {section.trigger for section in workflow_result.sections}

    assert "workflow_build_mode" not in default_triggers
    assert "workflow_build_mode" in workflow_triggers
    assert "Workflow Build Mode" not in default_result.system
    assert "Workflow Build Mode" in workflow_result.system
    assert "Read `workflow.protocol_info` before writing" in workflow_result.system
    assert "foreach.until" in workflow_result.system
    assert "{{ previous }}" in workflow_result.system
    assert workflow_result.tool_profile == "workflow_build"
    assert workflow_result.tool_namespaces == ("project", "interaction", "skill", "workflow")
    assert "node.*" not in workflow_result.system
    assert "canvas.delete" not in workflow_result.system
    assert "task.*" not in workflow_result.history


def test_workflow_build_prompt_uses_dedicated_cached_prefix() -> None:
    ctx = PromptContext(
        project_id="workflow-build-mode",
        user_message="修改当前工作流，把图片节点改成循环输出",
        state={"metadata": {"title": "测试项目"}},
        collaboration_mode="workflow_build",
    )

    result = assemble_split_result(ctx)
    section_names = [section.name for section in result.sections]

    assert section_names == [
        "identity",
        "workflow_build_mode",
        "runtime_context",
    ]
    assert '"tool_profile": "workflow_build"' in result.cache_key
    assert result.diagnostics()["tool_profile"] == "workflow_build"
    assert len(result.system) < 6000
    assert "`openreel.workflow.v2`" in result.system
    assert "Generic bounded review pattern" in result.system
    assert "Media is one logical step" in result.system
    assert "`uses` is the only reference contract" in result.system
    assert "Dynamic references add `select.values`" in result.system
    assert "provider/model routing" in result.system
    assert "frontend supplies media runtime settings" in result.system
    assert "Put media settings in `fields`" not in result.system
    assert "Specs describe structure and settings" not in result.system
    assert "openreel.workflow.authoring.v1" not in result.system
    assert "After a repairable failure, continue from the returned `repair_ref`" in result.system
    assert "Ready means saved and inspected with `workflow.canvas.inspect`" in result.system
    assert "Patch again when visible outputs, loops, dependencies, or final outputs are missing" in result.system
    assert result.history == ""

def test_plan_mode_prompt_is_read_only_without_execution_sections() -> None:
    result = assemble_split_result(PromptContext(
        project_id="plan-mode",
        user_message="先给我一个计划",
        state={},
        collaboration_mode="plan",
    ))

    section_names = [section.name for section in result.sections]

    assert section_names == [
        "identity",
        "plan_mode",
        "runtime_context",
    ]
    assert "Plan Mode" in result.system
    assert "node.*" not in result.system
    assert "task.*" not in result.system
    assert result.history == ""

def test_default_prompt_budget_stays_small_for_ordinary_turns() -> None:
    result = assemble_split_result(PromptContext(
        project_id="test",
        user_message="你好",
        state={},
    ))

    assert len(result.system) < 2200
    assert len(result.system) + len(result.history) < 3600
    assert "你好" not in result.system
    assert "你好" not in result.runtime
    assert "项目标题" in result.runtime
    assert len(result.sections) <= 9

def test_always_prompt_models_shared_canvas_collaboration() -> None:
    text = "\n".join([identity.PROMPT, working_loop.PROMPT, core_rules.PROMPT])

    assert "co-author one" in text
    assert "Canvas is creative truth" in text
    assert "user and Agent nodes have equal authority" in text
    assert "Existing/draft nodes are work containers" in text
    assert "update matching nodes before new ones" in text

def test_runtime_context_does_not_duplicate_latest_user_goal() -> None:
    text = runtime_context.build(
        {},
        latest_user_message="继续之前的提示词相关优化，重点看注入缓存和不要忘记用户需求。" + "补充" * 120,
    )

    assert "项目标题" in text
    assert "本轮用户目标" not in text
    assert "继续之前的提示词相关优化" not in text
    assert len(text) < 200

def test_runtime_context_does_not_inject_video_blueprint_first_card() -> None:
    text = runtime_context.build(
        {
            "metadata": {"title": "未命名项目"},
            "workflow": {"nodes": [], "edges": []},
        },
        latest_user_message="做一个15秒的视频",
    )

    assert "项目标题" in text
    assert "本轮用户目标" not in text
    assert "做一个15秒的视频" not in text
    assert "首张视频信息卡优先问缺失入口字段" not in text
    assert "start_tree_draft" not in text
    assert "项目蓝图" not in text

def test_split_prompt_cache_ignores_latest_user_and_mentor_guides() -> None:
    invalidate_cache()
    base = get_split_prompt_result(PromptContext(
        project_id="cache-runtime",
        user_message="继续优化提示词注入",
        state={},
    ))
    with_guide = get_split_prompt_result(PromptContext(
        project_id="cache-runtime",
        user_message="继续优化提示词注入",
        state={
            "_mentor_guides_loaded": {
                "video_workflow": {
                    "topic": "video_workflow",
                    "detail": "summary",
                    "has_full_guide": True,
                    "guidance_summary": "视频制作先写完整剧情，再规划人物、分集、分段和镜头。",
                    "guidance_hash": "guide-hash-1",
                    "references_count": 2,
                }
            }
        },
    ))
    changed_user = get_split_prompt_result(PromptContext(
        project_id="cache-runtime",
        user_message="改为检查图片和分镜一致性",
        state={},
    ))

    assert base.cache_key == with_guide.cache_key
    assert base.cache_key == changed_user.cache_key
    assert "继续优化提示词注入" not in base.cache_key
    assert "改为检查图片" not in changed_user.cache_key
    assert "### 指南复用缓存" not in base.runtime
    assert "### 指南复用缓存" not in with_guide.runtime
    assert "guide-hash-1" not in with_guide.runtime
    assert "继续优化提示词注入" not in base.runtime
    assert "改为检查图片和分镜一致性" not in changed_user.runtime
    assert "### 指南复用缓存" not in with_guide.system

def test_split_prompt_cache_ignores_dynamic_runtime_state() -> None:
    invalidate_cache()
    base = get_split_prompt_result(PromptContext(
        project_id="cache-runtime-state",
        user_message="继续",
        state={},
    ))
    with_runtime_state = get_split_prompt_result(PromptContext(
        project_id="cache-runtime-state",
        user_message="继续",
        state={
            "pending_video_blueprint_request": {
                "stage": "structure",
                "collected_facts": {
                    "topic": "雨夜桥头决斗",
                    "duration_seconds": "30秒",
                    "aspect_ratio": "16:9",
                },
            },
            "active_plan_checklist": [
                {"step_id": "s1", "title": "检查分镜提示词", "status": "pending", "tool": "agent.review"}
            ],
            "reference_assets": {
                "assets": [
                    {
                        "ref_id": "ref-1",
                        "mention": "@角色参考",
                        "rel_path": "uploads/role.png",
                        "status": "ready",
                    }
                ],
                "bindings": [],
            },
        },
    ))

    assert base.cache_key == with_runtime_state.cache_key
    assert "雨夜桥头决斗" not in with_runtime_state.cache_key
    assert "检查分镜提示词" not in with_runtime_state.cache_key
    assert "待补充创作信息" not in with_runtime_state.runtime
    assert "下一步任务" not in with_runtime_state.runtime
    assert "项目参考图资产索引" not in with_runtime_state.runtime
    assert "待补充创作信息" not in with_runtime_state.system


def test_runtime_context_omits_semantic_blueprint_drafting_state_from_default_prompt() -> None:
    result = assemble_split_result(PromptContext(
        project_id="drafting-blueprint",
        user_message="继续",
        state={
            "semantic_blueprint": {
                "status": "drafting",
                "title": "修士对决",
                "tree_version": 7,
                "node_count": 5,
                "needs_finalize": True,
                "root_children": [
                    {"id": "video_1", "type": "video", "title": "视频片段"},
                ],
            }
        },
    ))

    assert "状态:drafting(semantic blueprint file)" not in result.runtime
    assert "blueprint.finalize_tree_draft" not in result.runtime
    assert "蓝图阶段协议" not in result.runtime
    assert "蓝图阶段协议" not in result.system

def test_always_prompt_sections_are_contracts_not_manuals() -> None:
    result = assemble_split_result(PromptContext(
        project_id="test",
        user_message="你好",
        state={},
    ))
    manual_markers = ("### MUST", "报错示例", "|---|", "```")

    for stat in result.sections:
        if stat.source != "static":
            continue
        section = prompt_sections_pkg.get(stat.name)
        assert section is not None
        text = section.prompt or ""

        assert stat.chars <= 700, stat.name
        assert not any(marker in text for marker in manual_markers), stat.name

def test_working_loop_stays_domain_neutral_with_core_prompt() -> None:
    assert "Latest user" in working_loop.PROMPT
    assert "canvas state" in working_loop.PROMPT
    assert "existing workflow templates" in working_loop.PROMPT
    assert "Workflow Build Mode" not in working_loop.PROMPT
    assert "Tools mutate state" in working_loop.PROMPT
    assert "prompt rules" in working_loop.PROMPT
    assert "Before tools, write one progress sentence" in working_loop.PROMPT
    assert "blueprint.start_tree_draft" not in working_loop.PROMPT
    assert "finalize_tree_draft" not in working_loop.PROMPT
    assert "agent.review" not in working_loop.PROMPT
    assert "video_workflow" not in working_loop.PROMPT
    assert "workflow_spec returns" not in working_loop.PROMPT
    assert "workflow.run_*" not in working_loop.PROMPT

def test_state_prompt_sections_are_runtime_principles_not_manuals() -> None:
    sections = {
        "plan_rule": (plan_rule.PROMPT, ("skill", "text", "video", "node")),
    }

    for name, (text, markers) in sections.items():
        _assert_system_prompt_v2(name, text, max_len=1250, required_markers=markers)

def test_failure_trigger_is_not_supported_in_default_prompt() -> None:
    result = assemble_split_result(PromptContext(
        project_id="failure-cache",
        user_message="继续",
        state={},
        has_recent_failure=True,
    ))

    assert not trigger_matches("failure", PromptContext(project_id="failure-cache"))
    assert "Node Repair" not in result.runtime
    assert "Node Repair" not in result.history

def test_low_frequency_prompt_sections_are_runtime_principles_not_manuals() -> None:
    sections = {
        "attachment_rule": (attachment_rule.PROMPT, ("runtime state", "fields.references", "source_image")),
    }

    for name, (text, markers) in sections.items():
        _assert_system_prompt_v2(name, text, max_len=900, required_markers=markers)

def test_canvas_reference_request_is_not_backend_routed_by_parser_label() -> None:
    message = "让画布上的两个人在一起手牵手"
    ctx = PromptContext(
        project_id="test",
        user_message=message,
        state={"project_mode": "video_production"},
        has_characters=True,
    )

    assert should_require_plan(ctx) is False
    assert trigger_matches("complex_no_skip", ctx) is False

def test_video_generation_request_does_not_backend_force_plan_mode() -> None:
    ctx = PromptContext(
        project_id="test",
        user_message="生成一个短剧视频",
        state={},
    )
    assert trigger_matches("complex_no_skip", ctx) is False
    assert should_require_plan(ctx) is False

def test_atomic_request_reminder_is_not_injected_for_mode_only_state() -> None:
    reminder = AgentOrchestrator._build_checklist_reminder(
        {"project_mode": "video_production", "project_sub_mode": "grid"},
        require_plan=False,
    )
    assert reminder == ""
    assert "任何创作动作前必须先 plan.propose" not in reminder

def test_complex_request_reminder_requires_plan() -> None:
    reminder = AgentOrchestrator._build_checklist_reminder(
        {"project_mode": "video_production", "project_sub_mode": "grid"},
        require_plan=True,
    )
    assert "计划工具不在本轮可见工具面" in reminder
    assert "plan.propose" not in reminder

def test_checklist_reminder_omits_skipped_tasks(monkeypatch) -> None:
    from app.agent import task_graph as task_graph_module

    monkeypatch.setattr(
        task_graph_module.task_graph,
        "list_all",
        lambda project_id=None: [
            SimpleNamespace(
                id="task_1",
                subject="旧分镜节奏",
                tool="task",
                status="skipped",
                blocked_by=[],
                input={},
            )
        ],
    )

    reminder = AgentOrchestrator._build_checklist_reminder({}, project_id="project-1")

    assert reminder == ""

def test_checklist_reminder_keeps_pending_and_filters_skipped(monkeypatch) -> None:
    from app.agent import task_graph as task_graph_module

    monkeypatch.setattr(
        task_graph_module.task_graph,
        "list_all",
        lambda project_id=None: [
            SimpleNamespace(
                id="task_1",
                subject="旧分镜节奏",
                tool="task",
                status="skipped",
                blocked_by=[],
                input={},
            ),
            SimpleNamespace(
                id="task_2",
                subject="重写剧本",
                tool="node.update",
                status="pending",
                blocked_by=[],
                input={},
            ),
        ],
    )

    reminder = AgentOrchestrator._build_checklist_reminder({}, project_id="project-1")

    assert "待处理清单(1 项,失败 0)" in reminder
    assert "重写剧本" in reminder
    assert "旧分镜节奏" not in reminder
    assert "[pending]" in reminder
    assert "[skipped]" not in reminder

def test_agent_loop_no_text_fallback_reports_tool_error() -> None:
    text = AgentOrchestrator._build_no_text_fallback(
        state={},
        pending_meta={"rounds": [{"round": 1}]},
        terminal_error={"ok": False, "error_kind": "empty_plan", "error": "empty plan"},
        tool_errors=[],
        step_index=0,
        project_switched=False,
    )

    assert "本轮" in text
    assert "方案提交失败" in text

def test_before_model_call_hook_replaces_execution_checklist_reminder() -> None:
    old_reminder = "<execution-checklist>\nold\n</execution-checklist>"
    new_reminder = "<execution-checklist>\nnew\n</execution-checklist>"
    result = run_before_model_call(
        [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": old_reminder},
            {"role": "assistant", "content": "ok"},
        ],
        new_reminder,
    )

    contents = [message["content"] for message in result.messages]
    assert result.removed_checklist_reminders == 1
    assert result.checklist_reminder_added is True
    assert old_reminder not in contents
    assert contents[-1] == new_reminder

def test_before_model_call_hook_replaces_runtime_context_reminder() -> None:
    old_runtime = "<runtime-context>\nold\n</runtime-context>"
    result = run_before_model_call(
        [
            {"role": "user", "content": "继续"},
            {"role": "user", "content": old_runtime},
        ],
        "",
        runtime_context="## 运行时上下文\nnew",
    )

    contents = [message["content"] for message in result.messages]
    assert result.removed_runtime_contexts == 1
    assert result.runtime_context_added is True
    assert old_runtime not in contents
    assert contents[-2] == "继续"
    assert contents[-1] == "<runtime-context>\n## 运行时上下文\nnew\n</runtime-context>"

def test_before_model_call_hook_appends_dynamic_context_for_cache_prefix() -> None:
    checklist = "<execution-checklist>\nnext\n</execution-checklist>"
    result = run_before_model_call(
        [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "当前用户需求"},
        ],
        checklist,
        runtime_context="## 运行时上下文\nstate",
    )

    assert result.messages[-3] == {"role": "user", "content": "当前用户需求"}
    assert result.messages[-2]["content"] == checklist
    assert result.messages[-1]["content"] == (
        "<runtime-context>\n## 运行时上下文\nstate\n</runtime-context>"
    )

def test_before_model_call_hook_appends_context_for_tool_continuation() -> None:
    result = run_before_model_call(
        [
            {"role": "user", "content": "当前用户需求"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ],
        "",
        runtime_context="## 运行时上下文\nstate",
    )

    assert result.messages[-2]["role"] == "tool"
    assert result.messages[-1]["content"] == (
        "<runtime-context>\n## 运行时上下文\nstate\n</runtime-context>"
    )

def test_before_model_call_hook_removes_checklist_without_new_reminder() -> None:
    old_reminder = "<execution-checklist>\nold\n</execution-checklist>"
    result = run_before_model_call(
        [
            {"role": "user", "content": "继续"},
            {"role": "user", "content": old_reminder},
        ],
        "",
    )

    assert result.removed_checklist_reminders == 1
    assert result.checklist_reminder_added is False
    assert result.messages == [{"role": "user", "content": "继续"}]

def test_before_turn_hook_clears_guide_loaded() -> None:
    result = run_before_turn({"guide_loaded": {"character": True}})

    assert result.state_patch == {"guide_loaded": {}}

def test_before_turn_hook_keeps_project_mentor_guide_digest() -> None:
    result = run_before_turn({
        "_mentor_guides_loaded": {
            "video_workflow": {
                "topic": "video_workflow",
                "guidance_hash": "abc123",
                "guidance_summary": "已读视频工作流摘要",
            }
        }
    })

    assert result.state_patch == {}

def test_before_turn_hook_leaves_state_without_loaded_guides() -> None:
    result = run_before_turn({"project_mode": "single_node"})

    assert result.state_patch == {}

def test_stop_hook_skips_completion_audit_without_pending_work() -> None:
    result = run_stop_after_text_response(
        step_index=2,
        checklist=[],
        audit_triggered=False,
    )

    assert result.should_run_audit is False
    assert result.audit_triggered is False
    assert result.pending_steps == 0
    assert result.failed_steps == 0
    assert result.audit_message == ""

def test_stop_hook_counts_pending_and_failed_checklist_steps() -> None:
    result = run_stop_after_text_response(
        step_index=1,
        checklist=[
            {"status": "completed", "title": "已完成"},
            {"status": "pending", "title": "待处理"},
            {"status": "failed", "title": "失败项"},
        ],
        audit_triggered=False,
    )

    assert result.should_run_audit is True
    assert result.pending_steps == 1
    assert result.failed_steps == 1
    assert "未完成 1 步,失败 1 步" in result.audit_message
    assert "待处理" in result.audit_message
    assert "失败项" in result.audit_message
    assert "不要无条件续跑旧失败" in result.audit_message
    assert "pending/failed 项必须补完" not in result.audit_message

def test_stop_hook_does_not_repeat_completion_audit() -> None:
    result = run_stop_after_text_response(
        step_index=2,
        checklist=[{"status": "pending", "title": "待处理"}],
        audit_triggered=True,
    )

    assert result.should_run_audit is False
    assert result.audit_triggered is True
    assert result.audit_message == ""

def test_agent_review_is_model_called_not_orchestrator_hardcoded() -> None:
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(PromptContext(project_id="test")))
    visible = {str((tool.get("function") or {}).get("name") or "").replace("__", ".") for tool in tools}

    assert "agent.review" in visible
    assert "agent.run" not in visible
    assert "workflow.spec.apply_patch" not in visible

def test_post_tool_use_hook_matches_node_create_expected_type() -> None:
    result = run_post_tool_use_checklist(
        tool_name="node.create",
        tool_args={"type": "scene"},
        result={"id": "scene-1"},
        node_id=None,
        checklist=[
            {"status": "completed", "tool": "node.create", "expected_node_type": "character"},
            {"status": "pending", "tool": "node.create", "expected_node_type": "scene"},
        ],
    )

    assert result.should_update is True
    assert result.matched_index == 1
    assert result.status == "completed"
    assert result.actual_node_id == "scene-1"


def test_post_tool_use_hook_does_not_let_node_id_preempt_create_type_match() -> None:
    result = run_post_tool_use_checklist(
        tool_name="node.create",
        tool_args={"type": "scene"},
        result={"id": "scene-1"},
        node_id=None,
        checklist=[
            {"status": "pending", "tool": "node.create", "expected_node_type": "character"},
            {"status": "pending", "tool": "node.create", "expected_node_type": "scene"},
        ],
    )

    assert result.should_update is True
    assert result.matched_index == 1
    assert result.actual_node_id == "scene-1"

def test_post_tool_use_hook_marks_failed_result_on_matched_step() -> None:
    result = run_post_tool_use_checklist(
        tool_name="node.create",
        tool_args={"type": "scene"},
        result={"ok": False, "error": "provider failed"},
        node_id=None,
        checklist=[
            {"status": "pending", "tool": "node.run", "expected_node_type": "scene"},
        ],
    )

    assert result.should_update is True
    assert result.matched_index == 0
    assert result.status == "failed"

def test_post_tool_use_hook_does_not_mark_confirmation_request_failed() -> None:
    result = run_post_tool_use_checklist(
        tool_name="canvas.delete",
        tool_args={"scope": "selected", "node_ids": ["node-1"]},
        result={
            "ok": False,
            "requires_user_confirm": True,
            "action": "canvas.delete",
        },
        node_id=None,
        checklist=[
            {"status": "pending", "tool": "canvas.delete"},
        ],
    )

    assert result.should_update is False
    assert result.matched_index is None

def test_post_tool_use_hook_prefers_local_node_id() -> None:
    result = run_post_tool_use_checklist(
        tool_name="node.create",
        tool_args={"type": "character"},
        result={"id": "from-result"},
        node_id="from-node",
        checklist=[
            {"status": "pending", "tool": "node.create", "expected_node_type": "character"},
        ],
    )

    assert result.should_update is True
    assert result.actual_node_id == "from-node"


def test_post_tool_use_hook_matches_node_run_by_resolved_node_ref() -> None:
    result = run_post_tool_use_checklist(
        tool_name="node.run",
        tool_args={"node_id": "scene-1", "action": "render"},
        result={"ok": False, "error": "provider failed", "node_id": "scene-1"},
        node_id="scene-1",
        checklist=[
            {
                "step": 12,
                "status": "completed",
                "tool": "node.create",
                "actual_node_id": "character-1",
            },
            {
                "step": 14,
                "status": "pending",
                "tool": "node.run",
                "title": "渲染人物图",
                "expected_node_ref_step": 12,
                "expected_action": "render",
            },
            {
                "step": 15,
                "status": "completed",
                "tool": "node.create",
                "actual_node_id": "scene-1",
            },
            {
                "step": 17,
                "status": "pending",
                "tool": "node.run",
                "title": "渲染场景图",
                "expected_node_ref_step": 15,
                "expected_action": "render",
            },
        ],
    )

    assert result.should_update is True
    assert result.matched_index == 3
    assert result.status == "failed"
    assert result.actual_node_id == "scene-1"


def test_post_tool_use_hook_does_not_mark_wrong_node_run_step() -> None:
    result = run_post_tool_use_checklist(
        tool_name="node.run",
        tool_args={"node_id": "scene-1", "action": "render"},
        result={"ok": True, "node_id": "scene-1"},
        node_id="scene-1",
        checklist=[
            {
                "step": 12,
                "status": "completed",
                "tool": "node.create",
                "actual_node_id": "character-1",
            },
            {
                "step": 14,
                "status": "pending",
                "tool": "node.run",
                "title": "渲染人物图",
                "expected_node_ref_step": 12,
                "expected_action": "render",
            },
        ],
    )

    assert result.should_update is False
    assert result.matched_index is None
    assert result.actual_node_id == "scene-1"

def test_agent_round_summary_prefers_model_progress_text() -> None:
    event = AgentOrchestrator._build_agent_round_summary(
        1,
        "我会先检查当前节点状态，再决定下一步。",
        ["node.list"],
    )

    assert event["type"] == "agent_round"
    assert event["round"] == 2
    assert event["source"] == "model"
    assert event["content"] == "我会先检查当前节点状态，再决定下一步。"

def test_agent_round_summary_has_no_fallback_text_without_model_progress() -> None:
    event = AgentOrchestrator._build_agent_round_summary(
        0,
        None,
        ["node.create", "node.run"],
    )

    assert event["type"] == "agent_round"
    assert event["round"] == 1
    assert event["source"] == "action_summary"
    assert event["content"] == ""

def test_agent_round_summary_hides_internal_deferred_loader_noise() -> None:
    event = AgentOrchestrator._build_agent_round_summary(
        0,
        None,
        ["tool.search", "tool.describe", "tool.execute"],
    )

    assert event["type"] == "agent_round"
    assert event["source"] == "action_summary"
    assert event["content"] == ""

def test_agent_round_history_persists_compact_tool_results() -> None:
    rounds = AgentOrchestrator._extract_agent_round_history(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "node__list", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '[{"id":"node-1"},{"id":"node-2"}]',
            },
        ]
    )

    assert rounds == [
        {
            "round": 1,
            "content": "",
            "source": "action_summary",
            "tools": ["node.list"],
            "status": "completed",
            "results": [
                {
                    "tool": "node.list",
                    "status": "completed",
                    "summary": "返回 2 条记录",
                }
            ],
        }
    ]

def test_repeated_tool_error_fallback_names_tool_kind_count_and_next_step() -> None:
    text = AgentOrchestrator._build_no_text_fallback(
        state={},
        pending_meta={},
        terminal_error={
            "ok": False,
            "tool": "node.run",
            "error": "缺少参考图",
            "error_kind": "dependency_missing",
            "hint": "先生成分镜图。",
            "suggested_next": "satisfy_dependency",
            "stop_reason": "repeated_tool_error",
            "repeat_count": 3,
        },
        tool_errors=[],
        step_index=1,
        project_switched=False,
    )

    assert "本轮已停止" in text
    assert "node.run" in text
    assert "dependency_missing" in text
    assert "3 次" in text
    assert "补齐依赖" in text
    assert "satisfy_dependency" not in text

def test_repeated_tool_error_fallback_beats_existing_pending_plan() -> None:
    text = AgentOrchestrator._build_no_text_fallback(
        state={"pending_plan": {"kind": "node_execution", "title": "旧方案"}},
        pending_meta={"plan": {"kind": "node_execution", "title": "旧方案"}},
        terminal_error={
            "ok": False,
            "tool": "node.update",
            "error": "节点 'segment_01' 不存在。",
            "error_kind": "node_not_found",
            "hint": "从 available_node_ids 选择现有节点。",
            "suggested_next": "read_state",
            "stop_reason": "repeated_tool_error",
            "repeat_count": 3,
        },
        tool_errors=[],
        step_index=3,
        project_switched=False,
    )

    assert "本轮已停止" in text
    assert "node.update" in text
    assert "node_not_found" in text
    assert "方案已提交" not in text

def test_video_blueprint_intake_state_patch_starts_basic_stage() -> None:
    patch = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")
    assert patch["pending_video_blueprint_request"]["stage"] == "basic"
    assert "selected_video_mode" not in patch
    assert patch["pending_video_blueprint_request"]["duration_seconds"] == 15

def test_video_blueprint_intake_persists_uploaded_reference_images() -> None:
    first = video_intake_state_patch_for_interaction(
        {},
        "制作一个15秒的视频，参考 @水墨",
        [{
            "kind": "image",
            "rel_path": "uploads/style.png",
            "filename": "style.png",
            "mention": "@水墨",
            "mime_type": "image/png",
        }],
        "basic",
    )
    pending = first["pending_video_blueprint_request"]
    assert pending["reference_images"][0]["mention"] == "@水墨"
    assert pending["reference_images"][0]["rel_path"] == "uploads/style.png"

    second = video_intake_state_patch_for_interaction(
        first,
        "剧情模型发挥，制作模式宫格分镜",
        [{
            "kind": "image",
            "rel_path": "uploads/character.png",
            "filename": "character.png",
            "ref_label": "角色参考",
            "mime_type": "image/png",
        }],
        "structure",
    )
    refs = second["pending_video_blueprint_request"]["reference_images"]
    assert [ref["mention"] for ref in refs] == ["@水墨", "@角色参考"]
    assert [ref["rel_path"] for ref in refs] == ["uploads/style.png", "uploads/character.png"]

def test_video_intake_does_not_expose_fixed_backend_card_factories() -> None:
    assert not hasattr(video_intake, "_basic_intake_event")
    assert not hasattr(video_intake, "_structure_intake_event")

def test_video_intake_preserves_model_delegation_as_collected_facts() -> None:
    basic_intake = {
        "values": {
            "topic": "模型发挥",
            "production_basis": "由模型判断",
            "aspect_ratio": "模型规划",
        },
        "questions": [
            {"id": "topic", "header": "主题", "question": "视频主题", "options": [{"label": "模型发挥"}]},
            {"id": "production_basis", "header": "生成依据", "question": "依据", "options": [{"label": "由模型判断"}]},
            {"id": "aspect_ratio", "header": "画幅", "question": "画幅", "options": [{"label": "模型规划"}]},
        ],
    }
    first = video_intake_state_patch_for_interaction(
        {},
        "做15秒视频，你全权决定",
        [],
        "basic",
        basic_intake,
    )
    facts = first["pending_video_blueprint_request"]["collected_facts"]
    assert facts["topic"] == "model_decide"
    assert facts["production_basis"] == "model_decide"
    assert facts["aspect_ratio"] == "model_decide"

    structure_intake = {
        "values": {
            "plot_outline": "模型发挥",
            "episode_count": "模型规划",
            "segment_seconds": "模型规划",
        },
        "questions": [
            {"id": "plot_outline", "header": "剧情", "question": "剧情", "options": [{"label": "模型发挥"}]},
            {"id": "episode_count", "header": "集数", "question": "集数", "options": [{"label": "模型规划"}]},
            {"id": "segment_seconds", "header": "分段", "question": "分段", "options": [{"label": "模型规划"}]},
        ],
    }
    second = video_intake_state_patch_for_interaction(
        first,
        "都由模型规划",
        [],
        "structure",
        structure_intake,
    )
    facts = second["pending_video_blueprint_request"]["collected_facts"]
    assert facts["plot_outline"] == "model_decide"
    assert facts["episode_count"] == "model_decide"
    assert facts["segment_seconds"] == "model_decide"
    runtime_text = runtime_context.build(second, latest_user_message="开始做节点")
    assert "model_decide 表示用户授权模型选择" not in runtime_text
    assert "duration/aspect_ratio/production_basis 等字段要落成具体可执行值" not in runtime_text

def test_default_prompt_omits_old_workflow_authoring_routes() -> None:
    result = assemble_split_result(PromptContext(
        project_id="test",
        user_message="生成一张女主角人物参考图",
        state={},
    ))
    section_names = [section.name for section in result.sections]

    assert "tool_loader" not in section_names
    assert "template_rule" not in section_names
    assert "workflow.run_*" not in result.system
    assert "latest_authorized_workflow_ref" not in result.system
    assert "tool.execute(name=\"agent.run\"" not in result.system
    assert "Main Agent keeps workflow skill/spec/template decisions inside workflow_spec" not in result.system
    assert "workflow_spec returns" not in result.system
    assert "Workflow Build Mode" not in result.system
    assert "`node.*`" in result.system
    assert "template.list_categories" not in result.system
    assert "template.list(category, query)" not in result.system
    assert "template.get" not in result.system

def test_runtime_context_omits_recent_review_records_and_template_lookup() -> None:
    text = runtime_context.build(
        {
            "_last_template_lookup": {
                "tool": "template.list",
                "category": "storyboard_image",
                "query": "宫格 分镜 15秒",
                "count": 2,
                "updated_at": "2026-06-12T10:00:00",
            },
            "_last_agent_review": {
                "review_profile": "视频蓝图检查",
                "review_skill_key": "my_storyboard_check",
                "status": "pass",
                "safe_to_submit": True,
                "findings_count": 0,
                "updated_at": "2026-06-12T10:01:00",
            },
        },
        latest_user_message="提交蓝图",
    )

    assert "最近检查记录" not in text
    assert "storyboard_image" not in text
    assert "my_storyboard_check" not in text
    assert "safe_to_submit" not in text

def test_legacy_blueprint_runtime_context_is_not_injected_or_leaked() -> None:
    text = runtime_context.build({
        "metadata": {"title": "旧蓝图兼容"},
        "pending_plan": {
            "kind": "blueprint_tree",
            "id": "plan-1",
            "title": "15秒篮球短片",
            "summary": "pending",
            "tree_nodes": [
                {
                    "id": "storyboard_grid",
                    "type": "image",
                    "title": "宫格分镜",
                    "level": 2,
                    "prompt": "LEAK_FULL_STORYBOARD_PROMPT",
                }
            ],
        }
    })

    assert "项目标题" in text
    assert "待确认蓝图" not in text
    assert "legacy_pending_tree:true" not in text
    assert "video_workflow" not in text
    assert "blueprint_tree_guide" not in text
    assert "brief/text -> 人物图" not in text
    assert "storyboard_grid" not in text
    assert "LEAK_FULL_STORYBOARD_PROMPT" not in text

def test_video_blueprint_flow_then_asks_outline_episode_segments() -> None:
    state = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")

    patch = video_intake_state_patch_for_interaction(state, "动作打斗，国风动漫，动作短片，16:9", [], "structure")
    pending = patch["pending_video_blueprint_request"]
    assert pending["stage"] == "structure"
    assert "动作打斗" in pending["basic_answer"]

def test_video_blueprint_flow_basic_intake_uses_structured_duration_default() -> None:
    state = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")

    patch = video_intake_state_patch_for_interaction(
        state,
        "视频主题或核心事件：雨夜桥头动作打斗\n风格：国风动漫\n视频类型：动作短片\n总时长：30秒\n画幅比例：16:9",
        [],
        "structure",
        {"values": {"duration_seconds": 30}},
    )

    pending = patch["pending_video_blueprint_request"]
    assert pending["stage"] == "structure"
    assert pending["duration_seconds"] == 30

def test_video_blueprint_flow_basic_stage_does_not_set_mode_from_structured_default() -> None:
    state = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")

    patch = video_intake_state_patch_for_interaction(
        state,
        "用户提交表单",
        [],
        "basic",
        {"values": {"production_mode": "frames"}},
    )
    pending = patch["pending_video_blueprint_request"]
    assert pending["stage"] == "basic"
    assert "selected_mode" not in pending
    assert "selected_video_mode" not in patch
    assert "project_sub_mode" not in patch

def test_video_blueprint_basic_answer_values_are_persisted_without_mode_selection() -> None:
    state = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")

    patch = video_intake_state_patch_for_interaction(
        state,
        "用户提交基础表单",
        [],
        "basic",
        {
            "kind": "interaction_input",
            "purpose": "video_blueprint_intake",
            "stage": "basic",
            "values": {
                "topic": "雨夜石桥决斗",
                "production_basis": "先做参考图/分镜图",
                "duration_seconds": "30秒",
                "aspect_ratio": "16:9",
            },
            "questions": [
                {
                    "id": "topic",
                    "header": "主题",
                    "question": "视频主题、核心事件或视频类型按什么做？",
                    "options": [
                        {"label": "模型发挥", "description": "由模型规划"},
                        {"label": "沿用当前描述", "description": "使用本轮描述"},
                    ],
                },
                {
                    "id": "production_basis",
                    "header": "生成依据",
                    "question": "视频生成依据按什么走？",
                    "options": [
                        {"label": "先做参考图/分镜图", "description": "一致性更好"},
                        {"label": "纯文生视频", "description": "更快"},
                    ],
                },
                {
                    "id": "aspect_ratio",
                    "header": "画幅",
                    "question": "画幅按什么做？",
                    "options": [
                        {"label": "模型规划", "description": "由模型规划"},
                        {"label": "16:9", "description": "横屏"},
                    ],
                },
            ],
        },
    )

    pending = patch["pending_video_blueprint_request"]
    assert pending["stage"] == "basic"
    assert pending["last_submitted_stage"] == "basic"
    assert pending["duration_seconds"] == 30
    assert "主题：雨夜石桥决斗" in pending["basic_answer"]
    assert pending["basic_answers"][1]["value"] == "先做参考图/分镜图"
    assert pending["collected_facts"]["topic"] == "雨夜石桥决斗"
    assert pending["collected_facts"]["production_basis"] == "先做参考图/分镜图"
    assert pending["collected_facts"]["aspect_ratio"] == "16:9"
    assert "production_mode" not in pending["collected_facts"]
    assert "selected_mode" not in pending
    assert "selected_video_mode" not in patch
    assert "project_sub_mode" not in patch


def test_video_blueprint_intake_aliases_basis_to_production_basis() -> None:
    state = video_intake_state_patch_for_interaction({}, "做一个15秒视频", [], "basic")

    patch = video_intake_state_patch_for_interaction(
        state,
        "用户提交基础表单",
        [],
        "basic",
        {
            "kind": "interaction_input",
            "purpose": "video_blueprint_intake",
            "stage": "basic",
            "values": {
                "basis": "先做分镜图再生产视频",
            },
            "questions": [
                {
                    "id": "basis",
                    "header": "生成依据",
                    "question": "生成依据按什么走？",
                    "options": [{"label": "先做分镜图再生产视频", "description": "图生视频路径"}],
                },
            ],
        },
    )

    pending = patch["pending_video_blueprint_request"]
    assert pending["collected_facts"]["production_basis"] == "先做分镜图再生产视频"
    assert "basis" not in pending["collected_facts"]


def test_video_blueprint_flow_structure_does_not_set_mode_from_structured_default() -> None:
    first = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")
    second = video_intake_state_patch_for_interaction(first, "动作打斗，国风动漫，动作短片，16:9", [], "structure")
    state = {**first, **second}

    patch = video_intake_state_patch_for_interaction(
        state,
        "剧情大纲：你来发挥\n集数：1\n每段秒数：15\n制作模式：首尾帧",
        [],
        "structure",
        {"values": {"production_mode": "frames"}},
    )

    pending = patch["pending_video_blueprint_request"]
    assert "selected_mode" not in pending
    assert "selected_video_mode" not in patch
    assert "project_sub_mode" not in patch

def test_video_blueprint_structure_answer_values_are_persisted_as_constraints_not_mode() -> None:
    first = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")
    second = video_intake_state_patch_for_interaction(first, "雨夜石桥决斗，国风动漫，动作短片，16:9", [], "structure")
    state = {**first, **second}

    patch = video_intake_state_patch_for_interaction(
        state,
        "用户提交结构表单",
        [],
        "structure",
        {
            "kind": "interaction_input",
            "purpose": "video_blueprint_intake",
            "stage": "structure",
            "values": {
                "plot_outline": "少年剑客救人后反杀蒙面刺客",
                "episode_count": "1集",
                "segment_seconds": "不分段/单段连续",
            },
            "questions": [
                {
                    "id": "plot_outline",
                    "header": "剧情大纲",
                    "question": "剧情大纲按什么处理？",
                    "options": [
                        {"label": "模型发挥", "description": "由模型规划"},
                        {"label": "沿用我给的大纲", "description": "按上下文约束"},
                    ],
                },
                {
                    "id": "episode_count",
                    "header": "集数",
                    "question": "项目按几集组织？",
                    "options": [
                        {"label": "模型规划", "description": "由模型规划"},
                        {"label": "1集", "description": "单集"},
                    ],
                },
                {
                    "id": "segment_seconds",
                    "header": "分段",
                    "question": "视频片段分段方式按什么处理？",
                    "options": [
                        {"label": "模型规划", "description": "由模型规划"},
                        {"label": "不分段/单段连续", "description": "单段连续"},
                    ],
                },
            ],
        },
    )

    pending = patch["pending_video_blueprint_request"]
    assert pending["stage"] == "structure"
    assert pending["last_submitted_stage"] == "structure"
    assert "剧情大纲：少年剑客救人后反杀蒙面刺客" in pending["structure_answer"]
    assert pending["structure_answers"][2]["value"] == "不分段/单段连续"
    assert "start/append/finalize 蓝图草稿工具" in pending["mode_selection_policy"]
    assert "selected_mode" not in pending
    assert "selected_video_mode" not in patch
    assert "project_sub_mode" not in patch

def test_video_blueprint_flow_structure_stage_does_not_set_story_template_default() -> None:
    first = video_intake_state_patch_for_interaction({}, "制作一个15秒的视频", [], "basic")
    second = video_intake_state_patch_for_interaction(first, "动作打斗，国风动漫，动作短片，16:9", [], "structure")
    state = {**first, **second}

    patch = video_intake_state_patch_for_interaction(
        state,
        "用户提交表单",
        [],
        "structure",
        {"values": {"production_mode": "story_template"}},
    )
    pending = patch["pending_video_blueprint_request"]
    assert pending["stage"] == "structure"
    assert "selected_mode" not in pending
    assert "selected_video_mode" not in patch
    assert "project_sub_mode" not in patch

@pytest.mark.asyncio
async def test_orchestrator_video_blueprint_basic_intake_emits_structured_event(monkeypatch) -> None:
    holder = {"state": {}, "saved": [], "trace": []}

    class FakeProjectService:
        async def get_project(self, project_id: str):
            return SimpleNamespace(state_json=json.dumps(holder["state"]))

        async def get_project_state(self, project_id: str):
            return dict(holder["state"])

        async def update_project_state(self, project_id: str, patch: dict):
            holder["state"].update(patch)
            return SimpleNamespace(state_json=json.dumps(holder["state"]))

    class FakeTrace:
        def __init__(self, project_id: str, run_id: str):
            self.events = []

        def emit(self, *args, **kwargs):
            holder["trace"].append((args, kwargs))
            self.events.append((args, kwargs))

    class FakeToolCall:
        id = "call-interaction-1"
        function = SimpleNamespace(
            name="interaction__request_input",
            arguments=json.dumps(
                {
                    "stage": "basic",
                    "purpose": "video_blueprint_intake",
                    "title": "补充视频基础信息",
                    "description": "先确认主题、风格、类型、时长和画幅。",
                    "submit_label": "继续填写剧情结构",
                    "summary_text": "请补充视频主题、风格和类型，用于后续生成项目蓝图。",
                    "assistant_text": "可以做。先补充视频主题、风格和类型，我再继续写详细大纲。",
                    "questions": [
                        {
                            "id": "topic",
                            "header": "主题",
                            "question": "视频主题、核心事件或视频类型按什么做？",
                            "options": [
                                {"label": "模型发挥", "description": "由模型规划"},
                                {"label": "沿用当前描述", "description": "使用本轮描述"},
                            ],
                        },
                        {
                            "id": "style",
                            "header": "风格",
                            "question": "视觉风格和人物场景气质按什么方向？",
                            "options": [
                                {"label": "模型规划", "description": "由模型规划"},
                                {"label": "国风动漫", "description": "国风动漫"},
                            ],
                        },
                        {
                            "id": "aspect_ratio",
                            "header": "画幅",
                            "question": "15 秒视频的画幅按什么做？",
                            "options": [
                                {"label": "模型规划", "description": "由模型规划"},
                                {"label": "16:9", "description": "横屏"},
                                {"label": "9:16", "description": "竖屏"},
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        )

    class FakeMessage:
        content = "我先整理需要你确认的信息。"
        tool_calls = [FakeToolCall()]

        def model_dump(self):
            return {
                "role": "assistant",
                "content": self.content,
                "tool_calls": [
                    {
                        "id": FakeToolCall.id,
                        "function": {
                            "name": FakeToolCall.function.name,
                            "arguments": FakeToolCall.function.arguments,
                        },
                    }
                ],
            }

    class FakeLLMService:
        async def generate_with_tools(self, *args, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=FakeMessage())],
                usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
                model="fake-model",
            )

        async def generate(self, *args, **kwargs):
            return {"content": "正在整理需要你确认的信息。"}

    async def fake_save_message(project_id: str, role: str, content: str, metadata=None):
        holder["saved"].append((role, content, metadata))

    async def fake_settings():
        return {
            "max_iterations": 3,
            "auto_archive": True,
        }

    async def fake_compute_canvas_summary(project_id: str):
        return {
            "total": 0,
            "by_type": {},
            "running": 0,
            "failed": 0,
            "completed": 0,
            "nodes": [],
        }

    async def fake_build_messages(project_id: str, message: str, include_history: bool = True, current_message_aliases=None):
        return [{"role": "user", "content": message}]

    async def fake_maybe_compress_history(project_id: str):
        return None

    monkeypatch.setattr(orchestrator_module, "AgentTrace", FakeTrace)
    monkeypatch.setattr(orchestrator_module, "_load_agent_settings", fake_settings)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.project_service = FakeProjectService()
    orchestrator.llm_service = FakeLLMService()
    orchestrator._save_message = fake_save_message
    orchestrator._compute_canvas_summary = fake_compute_canvas_summary
    orchestrator._build_messages = fake_build_messages
    orchestrator._maybe_compress_history = fake_maybe_compress_history

    events = [
        event
        async for event in orchestrator._stream_one_turn("project-1", "制作一个15秒的视频")
    ]

    intake_event = next(event for event in events if event.get("type") == "interaction_input_requested")
    event_types = [event.get("type") for event in events]
    assert intake_event["project_id"] == "project-1"
    assert intake_event["intake"]["purpose"] == "video_blueprint_intake"
    assert intake_event["intake"]["stage"] == "basic"
    assert "presentation" not in intake_event["intake"]
    assert [question["id"] for question in intake_event["intake"]["questions"]] == [
        "topic",
        "style",
        "aspect_ratio",
    ]
    assert "plan_proposed" not in event_types
    assert "canvas_action" not in event_types
    assert "agent_round" in event_types
    assert any(args and args[0] == "llm_response" for args, _kwargs in holder["trace"])
    assistant_text = "".join(str(event.get("content") or "") for event in events if event.get("type") == "text_delta")
    assert "视频主题" in assistant_text
    assert "先选一下视频制作方式" not in assistant_text
    assert holder["saved"][1][2]["interactionInput"]["stage"] == "basic"
    assert "presentation" not in holder["saved"][1][2]["interactionInput"]
    assert holder["state"]["pending_video_blueprint_request"]["stage"] == "basic"


@pytest.mark.asyncio
async def test_orchestrator_retries_empty_length_response(monkeypatch) -> None:
    holder = {"state": {}, "saved": [], "trace": []}

    class FakeProjectService:
        async def get_project(self, project_id: str):
            return SimpleNamespace(state_json=json.dumps(holder["state"]))

        async def get_project_state(self, project_id: str):
            return dict(holder["state"])

        async def update_project_state(self, project_id: str, patch: dict):
            holder["state"].update(patch)
            return SimpleNamespace(state_json=json.dumps(holder["state"]))

    class FakeTrace:
        def __init__(self, project_id: str, run_id: str):
            self.events = []

        def emit(self, *args, **kwargs):
            holder["trace"].append((args, kwargs))
            self.events.append((args, kwargs))

    class FakeMessage:
        def __init__(self, content: str = "", tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class FakeLLMService:
        def __init__(self):
            self.calls = 0

        async def generate_with_tools(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                message = FakeMessage("")
                finish_reason = "length"
            else:
                message = FakeMessage("已用更短方式继续。")
                finish_reason = "stop"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
                model="fake-model",
            )

        async def generate(self, *args, **kwargs):
            return {"content": "继续。"}

    async def fake_save_message(project_id: str, role: str, content: str, metadata=None):
        holder["saved"].append((role, content, metadata))

    async def fake_settings():
        return {
            "max_iterations": 3,
            "auto_archive": True,
        }

    async def fake_compute_canvas_summary(project_id: str):
        return {"total": 0, "by_type": {}, "running": 0, "failed": 0, "completed": 0, "nodes": []}

    async def fake_build_messages(project_id: str, message: str, include_history: bool = True, current_message_aliases=None):
        return [{"role": "user", "content": message}]

    async def fake_maybe_compress_history(project_id: str):
        return None

    monkeypatch.setattr(orchestrator_module, "AgentTrace", FakeTrace)
    monkeypatch.setattr(orchestrator_module, "_load_agent_settings", fake_settings)

    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.project_service = FakeProjectService()
    orchestrator.llm_service = FakeLLMService()
    orchestrator._save_message = fake_save_message
    orchestrator._compute_canvas_summary = fake_compute_canvas_summary
    orchestrator._build_messages = fake_build_messages
    orchestrator._maybe_compress_history = fake_maybe_compress_history

    events = [
        event
        async for event in orchestrator._stream_one_turn("project-1", "生成工作流")
    ]

    assistant_text = "".join(str(event.get("content") or "") for event in events if event.get("type") == "text_delta")
    assert "已用更短方式继续" in assistant_text
    assert "这轮没有生成可见回复" not in assistant_text
    assert orchestrator.llm_service.calls == 2
    assert any(
        args and args[0] == "loop_transition" and kwargs.get("transition_reason") == "empty_length_response_retry"
        for args, kwargs in holder["trace"]
    )
    assert holder["saved"][-1][1] == "已用更短方式继续。"
