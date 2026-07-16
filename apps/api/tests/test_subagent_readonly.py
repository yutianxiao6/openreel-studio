import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent import workflow_spec_artifacts
from app.mcp_tools import agent_tools


def test_subagent_roles_are_fixed_readonly_reviewers() -> None:
    assert {
        "researcher",
        "reviewer",
        "debugger",
        "media_prompt_reviewer",
        "project_mentor",
        "default",
    } <= set(agent_tools.ROLE_PRESETS)
    assert "writer" not in agent_tools.ROLE_PRESETS
    assert "workflow_matcher" not in agent_tools.ROLE_PRESETS
    assert agent_tools.ROLE_PRESETS["workflow_spec"]["readonly"] is True
    assert agent_tools.ROLE_PRESETS["workflow_spec"]["strict_allowed_tools"] is True
    assert agent_tools.ROLE_PRESETS["node_producer"]["readonly"] is False
    assert agent_tools.ROLE_PRESETS["image_editor"]["readonly"] is False
    assert "image_generator" not in agent_tools.ROLE_PRESETS
    assert "image_generator" not in agent_tools.AGENT_RUN_ROLE_NAMES


def test_resolve_role_filters_custom_tools_to_readonly_only() -> None:
    preset = agent_tools._resolve_role(
        "reviewer",
        [
            "node.list",
            "node.run",
            "project.reset",
            "tool.execute",
            "skill.project_mentor",
            "feature.list",
        ],
    )

    assert preset["readonly"] is True
    assert "node.list" in preset["allowed_tools"]
    assert "skill.project_mentor" in preset["allowed_tools"]
    assert "feature.list" in preset["allowed_tools"]
    assert "node.run" not in preset["allowed_tools"]
    assert "project.reset" not in preset["allowed_tools"]
    assert "tool.execute" not in preset["allowed_tools"]
    assert set(preset["denied_tools"]) >= {"node.run", "project.reset", "tool.execute"}


def test_image_editor_role_allows_only_its_worker_whitelist() -> None:
    preset = agent_tools._resolve_role(
        "image_editor",
        [
            "node.get",
            "vision.view_image",
            "image.segment",
            "image.edit",
            "node.run",
            "project.reset",
        ],
    )

    assert preset["readonly"] is False
    assert preset["include_tool_schemas"] is True
    assert preset["enforce_max_steps"] is True
    assert preset["allowed_tools"] == ["node.get", "vision.view_image", "image.segment", "image.edit"]
    assert preset["denied_tools"] == ["node.run", "project.reset"]


def test_node_producer_role_allows_scoped_worker_whitelist() -> None:
    preset = agent_tools._resolve_role(
        "node_producer",
        [
            "project.get_state",
            "skill.search",
            "skill.get",
            "node.get",
            "node.create",
            "node.update",
            "node.run",
            "vision.view_image",
            "project.reset",
        ],
    )

    assert preset["readonly"] is False
    assert preset["task_type"] == "subagent_node_producer"
    assert preset["include_tool_schemas"] is True
    assert preset["enforce_max_steps"] is True
    assert preset["allowed_tools"] == ["skill.get", "node.get", "node.update", "node.run", "vision.view_image"]
    assert "skill.search" not in preset["allowed_tools"]
    assert "skill.get" in preset["allowed_tools"]
    assert "node.create" not in preset["allowed_tools"]
    assert "node.update" in preset["allowed_tools"]
    assert "node.run" in preset["allowed_tools"]
    assert "project.reset" not in preset["allowed_tools"]
    assert preset["denied_tools"] == ["project.get_state", "skill.search", "node.create", "project.reset"]


def test_workflow_roles_keep_preset_minimum_step_limit() -> None:
    workflow_spec = agent_tools.ROLE_PRESETS[agent_tools.WORKFLOW_SPEC_ROLE_NAME]
    image_editor = agent_tools.ROLE_PRESETS[agent_tools.IMAGE_EDITOR_ROLE_NAME]

    assert agent_tools._effective_subagent_step_limit(
        role=agent_tools.WORKFLOW_SPEC_ROLE_NAME,
        preset=workflow_spec,
        max_steps=6,
    ) == workflow_spec["max_steps"]
    assert agent_tools._effective_subagent_step_limit(
        role=agent_tools.IMAGE_EDITOR_ROLE_NAME,
        preset=image_editor,
        max_steps=7,
    ) == 7


def test_workflow_spec_role_allows_only_template_selector_tools() -> None:
    preset = agent_tools._resolve_role(
        "workflow_spec",
        [
            "skill.search",
            "skill.get",
            "workflow.protocol_info",
            "workflow.template.resolve",
            "workflow.template.read",
            "workflow.template.clone_to_artifact",
            "workflow.spec.start",
            "workflow.spec.append_steps",
            "workflow.spec.commit",
            "workflow.spec.read",
            "workflow.spec.apply_patch",
            "workflow.canvas.inspect",
            "workflow.spec.patch",
            "workflow.materialize",
            "node.create",
            "project.reset",
        ],
    )

    assert preset["readonly"] is True
    assert preset["task_type"] == "subagent_workflow_spec"
    assert preset["include_tool_schemas"] is True
    assert preset["enforce_max_steps"] is True
    assert preset["allowed_tools"] == [
        "skill.search",
        "skill.get",
        "workflow.template.resolve",
        "workflow.template.read",
        "workflow.spec.read",
    ]
    assert preset["denied_tools"] == [
        "workflow.protocol_info",
        "workflow.template.clone_to_artifact",
        "workflow.spec.start",
        "workflow.spec.append_steps",
        "workflow.spec.commit",
        "workflow.spec.apply_patch",
        "workflow.canvas.inspect",
        "workflow.spec.patch",
        "workflow.materialize",
        "node.create",
        "project.reset",
    ]


def test_workflow_spec_system_composes_system_prompt_from_skill_rules() -> None:
    preset = agent_tools._resolve_role("workflow_spec", None)
    package = agent_tools._build_subagent_prompt_package(
        "workflow_spec",
        preset,
        "根据用户目标选择合适的视频 workflow 模板",
        {"workflow_skill_name": "general_short_drama_workflow"},
    )

    system = package["system"]
    task_message = package["task_message"]
    assert "workflow_spec Selector" in system
    assert "只负责为主 Agent 选择现有 OpenReel workflow 模板" in system
    assert "/workflow" not in system
    assert "返回最匹配的 template_id/version_id" in system
    assert "普通制作视频、30秒视频、文生视频或最终视频目标默认返回" in system
    assert "没有合适模板时返回 blocked" in system
    assert "input_fields" in system
    assert str(agent_tools.WORKFLOW_SPEC_MAX_OUTPUT_TOKENS) in system
    assert "schema='openreel.workflow.authoring.v1'" not in system
    assert "workflow.spec.apply_patch" not in system
    assert "workflow.canvas.inspect" not in system
    assert "patch_existing" not in system
    assert "compile_new" not in system
    assert "不要为了查核心协议" not in system
    assert "不回传完整 skill/template" not in system
    assert "不提前写正文" not in system
    assert "只读子 Agent" in system
    assert "禁止调用任何写入" in system
    assert "workflow.template.resolve" in task_message
    assert "workflow.template.read" in task_message
    assert "reuse_existing" in task_message
    assert "selector 模式" in task_message
    assert "只选择已有 workflow 模板" in task_message
    assert "patch_existing" not in task_message
    assert "compile_new" not in task_message
    assert "artifact_ref" not in task_message
    assert "result.workflow 返回新 workflow" not in task_message
    assert "workflow.spec.start/append_steps/commit" not in task_message
    builder_package = agent_tools._build_subagent_prompt_package(
        "workflow_spec",
        preset,
        "根据用户目标选择合适的视频 workflow 模板",
        {"workflow_skill_name": "general_short_drama_workflow", "_workflow_spec_mode": "builder"},
    )
    builder_task_message = builder_package["task_message"]
    assert "selector 模式" in builder_task_message
    assert "builder 模式" not in builder_task_message
    assert "patch_existing" not in builder_task_message
    assert "compile_new" not in builder_task_message
    assert "workflow.spec.apply_patch" not in builder_task_message
    assert "workflow.canvas.inspect" not in builder_task_message
    assert "Authoring quick map" not in builder_task_message
    assert "workflow.spec.start/append_steps/commit" not in builder_task_message
    assert "最终 JSON 只返回模板引用" in builder_task_message
    assert "default_video" in task_message
    assert "返回 general_short_drama_workflow 的 template_id" in task_message
    assert "不会暴露给主 Agent" not in task_message
    assert "missing_questions" not in task_message
    assert "从 workflow skill 对应节点的系统提示词提取" not in system
    assert "从 workflow skill 对应节点的系统提示词提取" not in task_message


@pytest.mark.asyncio
async def test_workflow_spec_subagent_requests_10000_output_tokens(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSessionScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeFinalMessage:
        content = json.dumps(
            {
                "status": "completed",
                "summary": "已判断复用模板。",
                "result": {
                    "status": "completed",
                    "decision": "reuse_existing",
                    "template_id": "demo_template",
                    "run_ready": True,
                    "self_check": {"passed": True, "checks": [], "issues": []},
                },
            },
            ensure_ascii=False,
        )
        tool_calls = []

    class FakeLLMService:
        def __init__(self, db):
            self.db = db

        async def generate_with_tools(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=FakeFinalMessage(), finish_reason="stop")],
                usage={"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
                model="fake-model",
            )

    monkeypatch.setattr(agent_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(agent_tools, "LLMService", FakeLLMService)
    monkeypatch.setattr(agent_tools, "dump_llm_request", lambda **kwargs: None)

    result = await agent_tools._subagent_loop(
        project_id="project-1",
        role="workflow_spec",
        task="按 skill 选择工作流模板",
        inputs={"workflow_skill_name": "demo"},
        max_steps=3,
        allowed_tools=None,
    )

    assert result["error"] == ""
    assert captured["task_type"] == "subagent_workflow_spec"
    assert captured["max_tokens"] == agent_tools.WORKFLOW_SPEC_MAX_OUTPUT_TOKENS


def test_subagent_project_id_injection_matches_tool_signature() -> None:
    assert agent_tools._subagent_tool_accepts_project_id("node.get") is True
    assert agent_tools._subagent_tool_accepts_project_id("project.get_state") is True
    assert agent_tools._subagent_tool_accepts_project_id("skill.search") is False
    assert agent_tools._subagent_tool_accepts_project_id("skill.get") is False
    assert agent_tools._subagent_tool_accepts_project_id("workflow.spec.apply_patch") is True


def test_readonly_role_system_prompt_forbids_mutation() -> None:
    preset = agent_tools._resolve_role("debugger", None)
    system = agent_tools._build_subagent_system(
        preset,
        "检查失败节点",
        {"node_id": "node-1"},
    )

    assert "只读子 Agent" in system
    assert "禁止调用任何写入、执行、生成、删除、批准、重置或配置变更工具" in system


def test_image_editor_system_prompt_uses_native_tool_protocol() -> None:
    preset = agent_tools._resolve_role("image_editor", None)
    system = agent_tools._build_subagent_system(
        preset,
        "裁剪节点12并提交",
        {"node_id": "12"},
    )

    assert "image_editor 子 Agent" in system
    assert "直接调用白名单工具" in system
    assert "## 工具参数" not in system
    assert '"name": "image.edit"' not in system
    assert '"name": "image.segment"' not in system
    assert "最终 result 使用对象结构" in system
    assert "只读子 Agent" not in system
    assert "裁剪节点12并提交" not in system
    assert "## 审查 profile" not in system
    assert "不重复调用 vision.view_image" in system
    assert "前端" not in system
    assert "后端" not in system


def test_node_producer_prompt_package_has_stable_prefix_and_cache_key() -> None:
    preset = agent_tools._resolve_role("node_producer", None)

    package_a = agent_tools._build_subagent_prompt_package(
        "node_producer",
        preset,
        "补全并运行人物参考图节点",
        {
            "node_id": "12",
            "allowed_node_types": ["image"],
            "inline_spec": "使用用户本轮给出的三面图规则。",
        },
    )
    package_b = agent_tools._build_subagent_prompt_package(
        "node_producer",
        preset,
        "补全视频提示词节点",
        {
            "node_id": "18",
            "allowed_node_types": ["video"],
            "primary_skill": "video_prompt_method",
        },
    )

    assert package_a["system"] == package_b["system"]
    assert package_a["cache_key"] == package_b["cache_key"]
    assert package_a["diagnostics"]["stable_system_hash"] == package_b["diagnostics"]["stable_system_hash"]
    assert package_a["task_message"] != package_b["task_message"]
    assert "补全并运行人物参考图节点" not in package_a["system"]
    assert "使用用户本轮给出的三面图规则" in package_a["task_message"]
    assert "node_producer 子 Agent" in package_a["system"]
    assert "skill.get" in package_a["system"]
    assert "skill.search" not in package_a["system"]
    assert "node.run" in package_a["system"]
    assert "reference_node_ids" in package_a["system"]
    assert "上游节点只读引用" in package_a["system"]
    assert package_a["diagnostics"]["cache_key"].startswith("subagent_prompt_v2:node_producer:")


def test_workflow_spec_prompt_package_has_stable_prefix_and_cache_key() -> None:
    preset = agent_tools._resolve_role("workflow_spec", None)

    package_a = agent_tools._build_subagent_prompt_package(
        "workflow_spec",
        preset,
        "按照本地 skill 制作文生视频工作流",
        {"workflow_skill_name": "text_to_video", "facts": {"plot": "江湖重逢"}},
    )
    package_b = agent_tools._build_subagent_prompt_package(
        "workflow_spec",
        preset,
        "局部修改当前工作流,增加关键帧提取",
        {"template_id": "general_short_drama_workflow", "facts": {"video": "node:12"}},
    )

    assert package_a["system"] == package_b["system"]
    assert package_a["cache_key"] == package_b["cache_key"]
    assert package_a["diagnostics"]["stable_system_hash"] == package_b["diagnostics"]["stable_system_hash"]
    assert package_a["task_message"] != package_b["task_message"]
    assert "江湖重逢" not in package_a["system"]
    assert "江湖重逢" in package_a["task_message"]
    assert package_a["diagnostics"]["section_count"] >= 4
    assert package_a["diagnostics"]["sections_by_trigger"] == {"always": package_a["diagnostics"]["section_count"]}
    assert package_a["diagnostics"]["cache_key"].startswith("subagent_prompt_v2:workflow_spec:")


def test_node_producer_prompt_package_stays_worker_sized() -> None:
    preset = agent_tools._resolve_role("node_producer", None)
    package = agent_tools._build_subagent_prompt_package(
        "node_producer",
        preset,
        "补全复杂节点",
        {"node_id": "12", "allowed_node_types": ["image"]},
    )
    tools_json = json.dumps(package["tools"], ensure_ascii=False, sort_keys=True, default=str)

    assert package["diagnostics"]["system_chars"] <= 1300
    assert len(package["system"]) + len(tools_json) <= 6200
    assert package["diagnostics"]["tool_names"] == [
        "skill.get",
        "node.get",
        "node.update",
        "node.run",
        "vision.view_image",
    ]


def test_subagent_openai_tools_exports_whitelist_as_native_tools() -> None:
    tools = agent_tools._subagent_openai_tools(["node.get", "image.segment", "image.edit"])
    by_name = {
        tool["function"]["name"]: tool["function"]
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }

    assert {"node__get", "image__segment", "image__edit"} <= set(by_name)
    assert "project__reset" not in by_name
    assert by_name["image__edit"]["parameters"]["type"] == "object"


def test_subagent_task_message_carries_dynamic_task_and_inputs() -> None:
    message = agent_tools._build_subagent_task_message("裁剪节点12并提交", {"node_id": "12"})

    assert "## 任务" in message
    assert "裁剪节点12并提交" in message
    assert '"node_id": "12"' in message


def test_image_editor_task_message_adds_target_acceptance_and_rollback_rules() -> None:
    message = agent_tools._build_subagent_task_message_for_role(
        "image_editor",
        "修复节点12的软件图标边角和外框",
        {"node_id": "12"},
    )

    assert "## 用户目标" in message
    assert "## 目标成品" in message
    assert "## 验收标准" in message
    assert "## 编辑会话规则" in message
    assert "主体完整" in message
    assert "安全边距" in message
    assert "rounded_rect" in message
    assert "base_ref" in message
    assert "checkpoint" in message
    assert "image.edit preview 附加的视觉上下文" in message
    assert "裁后主体贴边、缺边、缺底、缺角或比例异常的候选视为不合格" in message
    assert "call_tool" not in message


def test_node_producer_task_message_carries_scope_basis_and_lifecycle() -> None:
    message = agent_tools._build_subagent_task_message_for_role(
        "node_producer",
        "补全节点12的人物参考图并运行",
        {
            "node_id": "12",
            "allowed_node_types": ["image"],
            "basis": {"kind": "inline_spec"},
            "inline_spec": "使用用户指定的人物图写法。",
        },
    )

    assert "## 用户目标" in message
    assert "## 作用域" in message
    assert "## 节点生命周期" in message
    assert "补全节点12的人物参考图并运行" in message
    assert "inline_spec" in message
    assert "使用用户指定的人物图写法" in message
    assert "reference_node_ids" in message
    assert "fields.references" in message
    assert "depends_on" in message
    assert "上游节点" in message
    assert "allow_create" not in message
    assert "basis_used" in message


@pytest.mark.asyncio
async def test_subagent_loop_uses_native_tool_calls(monkeypatch) -> None:
    captured: dict[str, object] = {"llm_calls": []}

    class FakeSessionScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeToolCall:
        id = "call-node-get"
        function = SimpleNamespace(name="node__get", arguments=json.dumps({"node_id": "12"}))

    class FakeToolMessage:
        content = "我先读取节点。"
        tool_calls = [FakeToolCall()]

        def model_dump(self):
            return {
                "role": "assistant",
                "content": self.content,
                "tool_calls": [
                    {
                        "id": FakeToolCall.id,
                        "type": "function",
                        "function": {
                            "name": FakeToolCall.function.name,
                            "arguments": FakeToolCall.function.arguments,
                        },
                    }
                ],
            }

    class FakeFinalMessage:
        content = json.dumps(
            {
                "status": "completed",
                "summary": "节点已读取。",
                "result": {"status": "completed", "answer": "ok"},
            },
            ensure_ascii=False,
        )
        tool_calls = []

    class FakeLLMService:
        def __init__(self, db):
            self.db = db

        async def generate_with_tools(self, *, task_type, messages, tools, system, project_id):
            captured["llm_calls"].append({
                "task_type": task_type,
                "messages": list(messages),
                "tools": tools,
                "system": system,
                "project_id": project_id,
            })
            message = FakeToolMessage() if len(captured["llm_calls"]) == 1 else FakeFinalMessage()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage={"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
                model="fake-model",
            )

    async def fake_registry_call(name, **kwargs):
        captured["tool_call"] = {"name": name, "kwargs": kwargs}
        return {"ok": True, "node": {"id": "12", "title": "节点12"}}

    async def fake_emit_progress(**kwargs):
        captured.setdefault("progress", []).append(kwargs)

    def fake_dump_llm_request(**kwargs):
        captured.setdefault("prompt_dumps", []).append(kwargs)

    from app.mcp_tools.registry import registry

    monkeypatch.setattr(agent_tools, "session_scope", lambda: FakeSessionScope())
    monkeypatch.setattr(agent_tools, "LLMService", FakeLLMService)
    monkeypatch.setattr(registry, "call", fake_registry_call)
    monkeypatch.setattr(agent_tools, "_emit_subagent_progress", fake_emit_progress)
    monkeypatch.setattr(agent_tools, "dump_llm_request", fake_dump_llm_request)

    result = await agent_tools._subagent_loop(
        project_id="project-1",
        role="researcher",
        task="读取节点12",
        inputs={"node_id": "12"},
        max_steps=3,
        allowed_tools=["node.get"],
    )

    llm_calls = captured["llm_calls"]
    assert len(llm_calls) == 2
    assert llm_calls[0]["task_type"] == "agent_review"
    assert llm_calls[0]["tools"][0]["function"]["name"] == "node__get"
    assert captured["tool_call"]["name"] == "node.get"
    assert captured["tool_call"]["kwargs"]["project_id"] == "project-1"
    prompt_dumps = captured["prompt_dumps"]
    assert len(prompt_dumps) == 2
    first_prompt_assembly = prompt_dumps[0]["prompt_assembly"]
    assert first_prompt_assembly["schema_version"] == agent_tools.SUBAGENT_PROMPT_SCHEMA_VERSION
    assert first_prompt_assembly["cache_key"].startswith("subagent_prompt_v2:researcher:")
    assert first_prompt_assembly["stable_system_hash"]
    assert first_prompt_assembly["tool_schema_hash"]
    assert prompt_dumps[1]["prompt_assembly"]["cache_key"] == first_prompt_assembly["cache_key"]
    second_messages = llm_calls[1]["messages"]
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in second_messages)
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call-node-get"
        for message in second_messages
    )
    assert result["result"]["status"] == "completed"
    assert result["_subagent_usage"][0]["prompt_cache_key"] == first_prompt_assembly["cache_key"]
    assert result["_subagent_usage"][0]["usage"]["cache_hit_rate"] is not None


def test_image_editor_subagent_keeps_original_and_recent_two_images_in_visual_tail() -> None:
    stable_transcript = [{"role": "user", "content": "开始编辑"}]
    visual_tail: list[dict] = []

    for index in range(5):
        agent_tools._append_subagent_model_content(
            visual_tail,
            [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{index}"}}],
            role="image_editor",
        )

    image_messages = [
        message
        for message in visual_tail
        if message.get("_subagent_model_content")
        and any(part.get("type") == "image_url" for part in message["content"])
    ]
    urls = [message["content"][0]["image_url"]["url"] for message in image_messages]

    assert stable_transcript == [{"role": "user", "content": "开始编辑"}]
    assert len(image_messages) == 3
    assert urls == [
        "data:image/png;base64,0",
        "data:image/png;base64,3",
        "data:image/png;base64,4",
    ]


def test_subagent_model_content_parts_keeps_tool_images_for_worker_context() -> None:
    parts = agent_tools._subagent_model_content_parts({
        "_model_content": [
            {"type": "text", "text": "候选图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc", "detail": "high"}},
        ]
    })

    assert parts == [
        {"type": "text", "text": "候选图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc", "detail": "high"}},
    ]


def test_subagent_model_content_refs_are_stored_and_can_be_dropped_from_visual_tail() -> None:
    stable_transcript = [{"role": "user", "content": "开始编辑"}]
    visual_tail: list[dict] = []

    agent_tools._append_subagent_model_content(
        visual_tail,
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
        role="image_editor",
        refs=["/api/media/project/image_ops/edit-preview-1.png"],
        image_editor_state=agent_tools._new_image_editor_context_state({"node_id": "12"}),
    )

    assert stable_transcript == [{"role": "user", "content": "开始编辑"}]
    assert visual_tail[-1]["_subagent_image_refs"] == ["/api/media/project/image_ops/edit-preview-1.png"]
    removed = agent_tools._drop_subagent_image_context_for_refs(
        visual_tail,
        ["/api/media/project/image_ops/edit-preview-1.png"],
    )

    assert removed == 1
    assert not any(message.get("_subagent_model_content") for message in visual_tail)
    assert stable_transcript == [{"role": "user", "content": "开始编辑"}]


def test_subagent_messages_for_call_places_visual_tail_after_stable_prefix() -> None:
    stable_transcript = [
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": "读取节点", "tool_calls": []},
        {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
    ]
    visual_tail = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
            "_subagent_model_content": True,
        }
    ]

    messages = agent_tools._subagent_messages_for_call(stable_transcript, visual_tail)

    assert messages[:3] == stable_transcript
    assert messages[3:] == visual_tail
    assert stable_transcript[-1]["role"] == "tool"


def test_subagent_tool_result_text_omits_model_content_bytes() -> None:
    rendered = agent_tools._render_subagent_tool_result(
        "image.edit",
        {
            "ok": True,
            "candidate_ref": "/api/media/project/image_ops/edit-preview-1.png",
            "_model_content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 100}},
            ],
        },
    )

    assert "candidate_ref" in rendered
    assert "_model_content" not in rendered
    assert "data:image/png" not in rendered


@pytest.mark.asyncio
async def test_rejected_image_editor_preview_file_is_deleted(monkeypatch, tmp_path) -> None:
    from app.services import image_operations

    monkeypatch.setattr(image_operations.settings, "STORAGE_PATH", str(tmp_path / "storage"))
    preview = tmp_path / "storage" / "project-1" / "generated_images" / "image_ops" / "edit-preview-12-abc.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"not-real-but-local")

    deleted = await agent_tools._delete_rejected_image_editor_candidate_file(
        "project-1",
        "/api/media/project-1/image_ops/edit-preview-12-abc.png",
    )

    assert deleted is True
    assert not preview.exists()


def test_reviewer_is_general_readonly_checker() -> None:
    preset = agent_tools._resolve_role("reviewer", None)
    system = agent_tools._build_subagent_system(
        preset,
        "检查节点图是否可执行。",
        {"review_goal": "检查节点图", "work_summary": "已建人物和场景节点"},
    )

    assert preset["readonly"] is True
    assert {"project.get_state", "task.list", "node.list", "node.get", "skill.project_mentor"} <= set(preset["allowed_tools"])
    assert "blueprint.get" not in preset["allowed_tools"]
    assert "通用只读审查" in preset["description"]
    assert "审查范围可以是节点图、视频流程、提示词、工具选择、trace 摘要、前端问题、配置或其他工程事项" in system
    assert "project.get_state、node.list、node.get" in system
    assert "禁止调用任何写入、执行、生成、删除、批准、重置或配置变更工具" in system


def test_review_profiles_are_not_hardcoded_in_agent_tool_code() -> None:
    assert not hasattr(agent_tools, "REVIEW_PROFILES")


def test_review_system_accepts_custom_checklist_and_skill() -> None:
    preset = agent_tools._resolve_role("reviewer", None)
    package = agent_tools._build_subagent_prompt_package(
        "reviewer",
        preset,
        "检查视频提示词是否忠于分镜。",
        {
            "review_profile": "用户自定义分镜检查",
            "custom_checklist": ["逐格核对分镜内容", "发现新增剧情时 safe_to_run=false"],
            "review_skill": {
                "name": "my_storyboard_check",
                "summary": "用户自定义分镜一致性检查",
                "rules": ["视频提示词必须覆盖每个关键格", "不得新增分镜没有的角色"],
            },
        },
    )
    system = package["system"]
    task_message = package["task_message"]

    assert "用户自定义分镜检查" not in system
    assert "逐格核对分镜内容" not in system
    assert "用户自定义分镜检查" in task_message
    assert "逐格核对分镜内容" in task_message
    assert "发现新增剧情时 safe_to_run=false" in task_message
    assert "用户自定义分镜一致性检查" in task_message
    assert "不得新增分镜没有的角色" in task_message
    assert "自定义审查 skill 是本轮主要检查标准" in task_message
    assert "禁止调用任何写入、执行、生成、删除、批准、重置或配置变更工具" in system


def test_subagent_node_get_render_keeps_media_output_before_long_prompt() -> None:
    rendered = agent_tools._render_subagent_tool_result(
        "node.get",
        {
            "id": "image-1",
            "type": "image",
            "status": "completed",
            "input": {
                "prompt": "长提示词" * 800,
                "depends_on": ["script-1"],
            },
            "output": {
                "stages": [
                    {
                        "status": "completed",
                        "url": "/api/media/project/image.png",
                    }
                ]
            },
        },
    )

    assert len(rendered) < agent_tools.TOOL_RESULT_TRUNCATE
    assert "/api/media/project/image.png" in rendered
    assert rendered.index('"output"') < rendered.index('"input"')
    assert "script-1" in rendered


def test_review_skill_key_loads_from_root_skill_review_dir(tmp_path, monkeypatch) -> None:
    root = tmp_path
    monkeypatch.delenv("OPENREEL_SKILLS_DIR", raising=False)
    skill_dir = root / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "my_storyboard_check.md").write_text(
        "# 我的分镜检查\n\n- 必须逐格核对\n- 不得新增剧情\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_tools.settings, "PROJECT_ROOT", str(root))

    loaded = agent_tools._read_review_skill("my_storyboard_check")

    assert loaded["ok"] is True
    assert loaded["key"] == "my_storyboard_check"
    assert loaded["path"] == "skills/review/my_storyboard_check.md"
    assert "必须逐格核对" in loaded["content"]


@pytest.mark.asyncio
async def test_agent_review_loads_review_skill_key_before_subagent(tmp_path, monkeypatch) -> None:
    root = tmp_path
    monkeypatch.delenv("OPENREEL_SKILLS_DIR", raising=False)
    skill_dir = root / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "my_prompt_check.md").write_text(
        "# 提示词检查\n\n- 必须检查主体、动作和镜头\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_tools.settings, "PROJECT_ROOT", str(root))
    captured = {}

    async def fake_subagent_run(**kwargs):
        captured.update(kwargs)
        return {"error": "", "result": {"status": "pass"}, "summary": "ok"}

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(
        project_id="project-1",
        review_goal="检查提示词",
        user_request="检查这段提示词",
        work_summary="已写 image prompt",
        review_profile="用户自定义提示词检查",
        review_skill_key="my_prompt_check",
        custom_checklist=["检查是否复述剧情"],
    )

    assert result["summary"] == "ok"
    assert result["result"]["schema_version"] == agent_tools.REVIEW_RESULT_SCHEMA_VERSION
    assert result["result"]["parse_status"] == "parsed"
    assert result["result"]["session_status"] == "completed"
    assert result["result"]["safe_to_submit"] is True
    inputs = captured["inputs"]
    assert inputs["review_profile"] == "用户自定义提示词检查"
    assert inputs["review_skill_key"] == "my_prompt_check"
    assert inputs["review_skill"]["ok"] is True
    assert "必须检查主体、动作和镜头" in inputs["review_skill"]["content"]
    assert inputs["custom_checklist"] == ["检查是否复述剧情"]
    assert "current_blueprint_tree" not in inputs["evidence"]


@pytest.mark.asyncio
async def test_agent_review_returns_compact_model_facing_envelope(monkeypatch) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "error": "",
            "role": "reviewer",
            "task": "very long internal task" * 20,
            "summary": "审查通过，可以继续。",
            "steps_used": 3,
            "tool_log": [{"tool": "node.get", "ok": True}],
            "result": {
                "status": "revise_required",
                "passed": False,
                "safe_to_run": False,
                "safe_to_submit": False,
                "findings": [
                    {
                        "severity": "high",
                        "issue": "提示词遗漏了用户指定的场景约束。" * 20,
                        "evidence": "prompt 没有包含用户要求的四视图、无人物、仅场景环境。" * 20,
                        "suggested_fix": "补齐场景、四视图和无人物约束。" * 20,
                        "violated_requirement": "用户要求生成四视图无人物场景图。" * 20,
                    }
                ],
                "suggested_next": "修订原节点后继续。",
            },
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(
        project_id="project-1",
        review_goal="检查图像节点",
        user_request="生成四视图无人物场景图",
        work_summary="已创建图像节点",
        evidence={"node": {"id": "12", "prompt": "x" * 2000}},
    )

    assert set(result).issubset({"role", "result", "summary", "review_status", "subagent_error"})
    assert "task" not in result
    assert "tool_log" not in result
    assert "steps_used" not in result
    assert "review_subject" not in result
    assert "review_inputs_summary" not in result
    finding = result["result"]["findings"][0]
    assert len(finding["issue"]) <= 275
    assert len(finding["evidence"]) <= 195
    assert len(finding["suggested_fix"]) <= 195
    assert len(finding["violated_requirement"]) <= 175


@pytest.mark.asyncio
async def test_agent_review_preserves_private_subagent_usage_for_trace(monkeypatch) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "error": "",
            "role": "reviewer",
            "summary": "审查通过。",
            "steps_used": 1,
            "result": {
                "status": "pass",
                "passed": True,
                "safe_to_run": True,
                "safe_to_submit": True,
                "findings": [],
            },
            "_subagent_usage": [
                {
                    "agent": "reviewer",
                    "step": 1,
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "cache_read_tokens": 64,
                    },
                }
            ],
            "_subagent_trace": [
                {
                    "agent": "reviewer",
                    "step": 1,
                    "event": "model_response",
                    "prompt_cache_key": "subagent_prompt_v2:reviewer:test",
                }
            ],
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(
        project_id="project-1",
        review_goal="检查节点6",
        user_request="检查分镜",
        work_summary="节点6等待审查",
    )

    assert result["review_status"] == "pass"
    assert result["_subagent_usage"][0]["agent"] == "reviewer"
    assert result["_subagent_usage"][0]["usage"]["cache_read_tokens"] == 64
    assert result["_subagent_trace"][0]["prompt_cache_key"].startswith("subagent_prompt_v2:reviewer:")


@pytest.mark.asyncio
async def test_agent_run_delegates_to_registered_image_editor(monkeypatch) -> None:
    captured = {}

    async def fake_subagent_run(**kwargs):
        captured.update(kwargs)
        return {
            "error": "",
            "result": {
                "status": "completed",
                "committed": True,
                "node_id": "12",
                "committed_ref": "/api/media/project/edit.png",
            },
            "summary": "已完成图片编辑",
            "steps_used": 3,
            "tool_log": [{"tool": "image.edit", "ok": True}],
            "_subagent_usage": [{"agent": "image_editor", "step": 1, "usage": {"total_tokens": 42}}],
            "_subagent_trace": [{"agent": "image_editor", "step": 1, "event": "tool_result", "tool": "image.edit"}],
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="image_editor",
        task="裁剪节点12并提交",
        inputs={"node_id": "12", "allowed_tools": ["project.reset"]},
        max_steps=7,
    )

    assert result["ok"] is True
    assert result["agent"] == "image_editor"
    assert result["status"] == "completed"
    assert result["result"]["committed_ref"] == "/api/media/project/edit.png"
    assert result["_subagent_usage"][0]["usage"]["total_tokens"] == 42
    assert result["_subagent_trace"][0]["event"] == "tool_result"
    assert captured["project_id"] == "project-1"
    assert captured["role"] == "image_editor"
    assert captured["task"] == "裁剪节点12并提交"
    assert captured["inputs"]["node_id"] == "12"
    assert captured["max_steps"] == 7
    assert "allowed_tools" not in captured


@pytest.mark.asyncio
async def test_agent_run_delegates_to_registered_node_producer(monkeypatch) -> None:
    captured = {}

    async def fake_subagent_run(**kwargs):
        captured.update(kwargs)
        return {
            "error": "",
            "result": {
                "status": "completed",
                "node_ids": ["12"],
                "completed_node_ids": ["12"],
                "output_refs": ["/api/media/project/character.png"],
                "basis_used": {"kind": "inline_spec"},
                "verification": {"checked": True},
            },
            "summary": "已完成节点生产",
            "steps_used": 6,
            "tool_log": [{"tool": "node.run", "ok": True}],
            "_subagent_usage": [{"agent": "node_producer", "step": 1, "usage": {"total_tokens": 96}}],
            "_subagent_trace": [{"agent": "node_producer", "step": 1, "event": "tool_result", "tool": "node.run"}],
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="node_producer",
        task="补全并运行节点12的人物图",
        inputs={
            "node_id": "12",
            "allowed_node_types": ["image"],
            "basis": {"kind": "inline_spec"},
            "inline_spec": "用户本轮人物图规则。",
        },
        max_steps=10,
    )

    assert result["ok"] is True
    assert result["agent"] == "node_producer"
    assert result["status"] == "completed"
    assert result["result"]["completed_node_ids"] == ["12"]
    assert captured["role"] == "node_producer"
    assert captured["inputs"]["node_id"] == "12"
    assert captured["inputs"]["allowed_node_types"] == ["image"]
    assert captured["max_steps"] == 10


@pytest.mark.asyncio
async def test_node_producer_scope_rejects_create_without_allow_create() -> None:
    scope = await agent_tools._new_subagent_write_scope(
        project_id="project-1",
        role="node_producer",
        inputs={"allowed_node_types": ["image"]},
    )

    result = await agent_tools._validate_subagent_tool_scope(
        project_id="project-1",
        role="node_producer",
        tool_name="node.create",
        tool_input={"type": "image"},
        scope=scope,
    )

    assert result is not None
    assert result["error_kind"] == "subagent_scope_denied"
    assert "allow_create" in result["error"]


@pytest.mark.asyncio
async def test_node_producer_scope_rejects_update_outside_scoped_node(monkeypatch) -> None:
    async def fake_node_for_scope(project_id, node_id):
        return f"internal-{node_id}", "image", None

    monkeypatch.setattr(agent_tools, "_node_for_subagent_scope", fake_node_for_scope)
    scope = {
        "role": "node_producer",
        "allowed_node_ids": {"12"},
        "allowed_resolved_node_ids": {"internal-12"},
        "created_node_ids": set(),
        "created_public_node_ids": set(),
        "allowed_node_types": {"image"},
        "allow_create": False,
        "require_node_scope": True,
    }

    result = await agent_tools._validate_subagent_tool_scope(
        project_id="project-1",
        role="node_producer",
        tool_name="node.update",
        tool_input={"node_id": "13", "patch": {"prompt": "new"}},
        scope=scope,
    )

    assert result is not None
    assert result["error_kind"] == "subagent_scope_denied"
    assert "指定作用域内节点" in result["error"]


@pytest.mark.asyncio
async def test_node_producer_scope_allows_created_node_after_create(monkeypatch) -> None:
    async def fake_node_for_scope(project_id, node_id):
        return "internal-new", "image", None

    monkeypatch.setattr(agent_tools, "_node_for_subagent_scope", fake_node_for_scope)
    scope = {
        "role": "node_producer",
        "allowed_node_ids": set(),
        "allowed_resolved_node_ids": set(),
        "created_node_ids": set(),
        "created_public_node_ids": set(),
        "allowed_node_types": {"image"},
        "allow_create": True,
        "require_node_scope": True,
    }
    agent_tools._record_subagent_created_nodes(scope, {"id": "27", "_canvas_id": "internal-new"})

    result = await agent_tools._validate_subagent_tool_scope(
        project_id="project-1",
        role="node_producer",
        tool_name="node.run",
        tool_input={"node_id": "27"},
        scope=scope,
    )

    assert result is None


@pytest.mark.asyncio
async def test_agent_run_marks_image_editor_blocked_as_terminal(monkeypatch) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "error": "",
            "result": {
                "status": "blocked",
                "committed": False,
                "node_id": "12",
                "candidate_ref": "/api/media/project/preview.png",
                "committed_ref": None,
                "issues": ["背景和发丝颜色过近，当前分割不可靠。"],
            },
            "summary": "已尝试分割但无法可靠提交。",
            "steps_used": 12,
            "tool_log": [{"tool": "image.segment", "ok": True}],
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="image_editor",
        task="抠出节点12人物",
        inputs={"node_id": "12"},
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_kind"] == "subagent_blocked"
    assert result["suggested_next"] == "report_blocked_to_user"
    assert result["terminal"] is True
    assert result["model_feedback"]["evidence"]["node_id"] == "12"
    assert result["model_feedback"]["evidence"]["committed"] is False
    assert result["model_feedback"]["evidence"]["candidate_ref"].endswith("preview.png")


@pytest.mark.asyncio
async def test_agent_run_can_return_catalog_without_running_worker() -> None:
    result = await agent_tools.agent_run(project_id="project-1", agent="catalog")

    assert result["ok"] is True
    assert result["status"] == "catalog"
    assert {item["agent"] for item in result["available_agents"]} == {
        "node_producer",
        "workflow_spec",
        "image_editor",
    }
    assert result["available_agents"][0]["agent"] == "node_producer"


@pytest.mark.asyncio
async def test_agent_run_does_not_expose_workflow_matcher_to_main_agent() -> None:
    result = await agent_tools.agent_run(project_id="project-1", agent="workflow_matcher")

    assert result["ok"] is False
    assert result["error_kind"] == "unknown_subagent"
    assert {item["agent"] for item in result["available_agents"]} == {
        "node_producer",
        "workflow_spec",
        "image_editor",
    }


@pytest.mark.asyncio
async def test_agent_run_rejects_removed_image_generator() -> None:
    result = await agent_tools.agent_run(project_id="project-1", agent="image_generator")

    assert result["ok"] is False
    assert "未知子 Agent" in result["error"]
    assert "image_generator" not in {item["agent"] for item in result["available_agents"]}


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_selector_blocks_inline_workflow(monkeypatch, tmp_path) -> None:
    captured = {}

    async def fake_subagent_run(**kwargs):
        captured.update(kwargs)
        return {
            "result": {
                "status": "completed",
                "workflow": {
                    "id": "demo_workflow",
                    "name": "演示工作流",
                    "steps": [
                        {"id": "brief", "title": "需求", "node_type": "text"},
                        {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["brief"]},
                    ],
                },
                "user_preview": {"title": "演示工作流", "summary": "两步文本流程"},
                "self_check": {"passed": True, "checks": ["依赖顺序正确"], "issues": []},
            },
            "summary": "错误地返回了新工作流结构",
            "steps_used": 1,
            "tool_log": [],
            "_subagent_usage": [{"agent": "workflow_spec", "step": 1, "usage": {"total_tokens": 12}}],
            "_subagent_trace": [{"agent": "workflow_spec", "step": 1, "event": "finish"}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="选择合适的工作流模板",
        inputs={"facts": {"topic": "江湖"}},
    )

    assert result["ok"] is False
    assert result["agent"] == "workflow_spec"
    payload = result["result"]
    assert payload["status"] == "blocked"
    assert payload["validation"]["ok"] is False
    assert "只选择现有模板" in payload["blocked_reason"]
    assert captured["allowed_tools"] == agent_tools.WORKFLOW_SPEC_SELECTOR_TOOLS
    assert captured["inputs"]["_workflow_spec_mode"] == "selector"
    assert not list(tmp_path.glob("project-1/workflow_specs/*.json"))


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_blocks_builder_inline_workflow(monkeypatch, tmp_path) -> None:
    captured = {}

    async def fake_subagent_run(**kwargs):
        captured.update(kwargs)
        return {
            "result": {
                "status": "completed",
                "workflow": {
                    "id": "demo_workflow",
                    "name": "演示工作流",
                    "steps": [
                        {"id": "brief", "title": "需求", "node_type": "text"},
                        {"id": "script", "title": "剧本", "node_type": "text", "depends_on": ["brief"]},
                    ],
                },
                "user_preview": {"title": "演示工作流", "summary": "两步文本流程"},
                "self_check": {"passed": True, "checks": ["依赖顺序正确"], "issues": []},
            },
            "summary": "错误地返回了新工作流结构",
            "steps_used": 1,
            "tool_log": [],
            "_subagent_usage": [{"agent": "workflow_spec", "step": 1, "usage": {"total_tokens": 12}}],
            "_subagent_trace": [{"agent": "workflow_spec", "step": 1, "event": "finish"}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="选择合适的工作流模板",
        inputs={"facts": {"topic": "江湖"}},
    )

    assert result["ok"] is False
    assert result["agent"] == "workflow_spec"
    payload = result["result"]
    assert payload["status"] == "blocked"
    assert payload["validation"]["ok"] is False
    assert "只选择现有模板" in payload["blocked_reason"]
    assert "workflow" not in payload
    assert "spec" not in payload
    assert not list(tmp_path.glob("project-1/workflow_specs/*.json"))
    assert captured["role"] == "workflow_spec"
    assert captured["allowed_tools"] == agent_tools.WORKFLOW_SPEC_SELECTOR_TOOLS
    assert captured["inputs"]["_workflow_spec_mode"] == "selector"


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_rejects_half_authoring_workflow(monkeypatch, tmp_path) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "result": {
                "status": "completed",
                "decision": "compile_new",
                "workflow": {
                    "id": "bad_dynamic_workflow",
                    "workflow_spec_version": "openreel.workflow.v1",
                    "inputs": [{"id": "topic", "type": "text"}],
                    "steps": [
                        {"id": "plan", "title": "规划", "node_type": "text"},
                        {
                            "id": "segment_story",
                            "title": "分段剧情",
                            "node_type": "text",
                            "needs": ["plan"],
                            "for_each": "plan.output.segments",
                        },
                    ],
                },
                "self_check": {"passed": True, "checks": ["模型误判通过"], "issues": []},
            },
            "summary": "错误地返回了动态 workflow。",
            "steps_used": 1,
            "tool_log": [],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="按 skill 选择工作流模板",
        inputs={"facts": {"topic": "江湖"}},
    )

    assert result["ok"] is False
    assert result["result"]["status"] == "blocked"
    assert result["result"]["validation"]["ok"] is False
    assert "只选择现有模板" in result["result"]["validation"]["error"]
    assert not list(tmp_path.glob("project-1/workflow_specs/*.json"))


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_blocks_committed_artifact_ref(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
            project_id="project-1",
            workflow={
                "schema": "openreel.workflow.v2",
                "id": "committed_workflow",
                "title": "已提交工作流",
                "inputs": {},
                "steps": [
                    {"id": "brief", "title": "需求", "kind": "text", "prompt": {"task": "整理需求。"}},
                    {"id": "script", "title": "剧本", "kind": "text", "needs": ["brief"], "prompt": {"task": "写剧本。"}},
                ],
        },
        self_check={"passed": True, "checks": ["已校验"], "issues": []},
    )

    async def fake_subagent_run(**kwargs):
        return {
            "result": {
                "status": "completed",
                "artifact_ref": saved["artifact_ref"],
                "preview": {
                    **saved["preview"],
                    "step_count": 99,
                    "first_steps": [{"id": "fake", "title": "不存在", "node_type": "text"}],
                },
                "validation": {"ok": True, "step_count": 2},
                "self_check": {"passed": True, "checks": ["已校验"], "issues": []},
            },
            "summary": "已保存 artifact。",
            "steps_used": 3,
            "tool_log": [{"tool": "workflow.spec.apply_patch", "ok": True}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="选择合适的工作流模板",
        inputs={"facts": {"topic": "江湖"}},
    )

    assert result["ok"] is False
    payload = result["result"]
    assert payload["status"] == "blocked"
    assert payload["validation"]["ok"] is False
    assert "不能返回 artifact_ref" in payload["blocked_reason"]
    assert "workflow" not in payload
    assert "spec" not in payload


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_defaults_plain_video_to_general_template(monkeypatch) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "result": {
                "status": "completed",
                "decision": "reuse_existing",
                "template_id": "custom_video_flow",
                "version_id": "builtin",
                "self_check": {"passed": True, "checks": ["模板匹配"], "issues": []},
            },
            "summary": "复用视频模板。",
            "steps_used": 2,
            "tool_log": [{"tool": "workflow.template.resolve", "ok": True}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="制作30秒视频",
        inputs={"facts": {"duration_seconds": 30}},
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["status"] == "completed"
    assert payload["decision"] == "reuse_existing"
    assert payload["template_id"] == "general_short_drama_workflow"
    assert "artifact_ref" not in payload
    assert "input_schema" not in payload
    assert [field["id"] for field in payload["input_fields"]] == [
        "plot",
        "style",
        "video_type",
        "episode_count",
        "duration_seconds",
        "segment_seconds",
    ]
    assert all("missing" not in field for field in payload["input_fields"])
    assert all("question" not in field for field in payload["input_fields"])
    assert "known_input_values" not in payload
    assert "missing_questions" not in payload
    assert "run_ready" not in payload
    assert "workflow" not in payload
    assert "spec" not in payload
    assert "input_fields" in payload["next_action"]


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_accepts_explicit_reusable_template_id(monkeypatch) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "result": {
                "status": "completed",
                "decision": "reuse_existing",
                "template_id": "general_short_drama_workflow",
                "version_id": "builtin",
                "self_check": {"passed": True, "checks": ["模板匹配"], "issues": []},
            },
            "summary": "复用通用视频模板。",
            "steps_used": 2,
            "tool_log": [{"tool": "workflow.template.resolve", "ok": True}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="按指定 workflow skill 制作流程",
        inputs={
            "workflow_skill_name": "general_short_drama_workflow",
            "facts": {"plot": "雨夜天台收到未来来信"},
        },
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["template_id"] == "general_short_drama_workflow"
    assert "input_schema" not in payload
    assert [field["id"] for field in payload["input_fields"]] == [
        "plot",
        "style",
        "video_type",
        "episode_count",
        "duration_seconds",
        "segment_seconds",
    ]
    assert all("missing" not in field for field in payload["input_fields"])
    assert "workflow" not in payload
    assert "spec" not in payload


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_ask_user_with_template_returns_schema(monkeypatch) -> None:
    async def fake_subagent_run(**kwargs):
        return {
            "result": {
                "status": "blocked",
                "decision": "ask_user",
                "template_id": "general_short_drama_workflow",
                "version_id": "1",
                "preview": {"name": "30秒视频工作流"},
                "input_schema": [
                    {"id": "plot", "label": "故事主题", "type": "string", "required": True},
                    {"id": "effect_type", "label": "特效类型", "type": "string", "required": True},
                ],
                "missing_questions": [
                    {"id": "plot", "question": "这30秒视频具体拍什么？"},
                    {"id": "effect_type", "question": "突出什么特效？"},
                ],
                "run_ready": False,
                "validation": {"ok": False, "blocked_reason": "缺少核心创意输入。"},
                "self_check": {"passed": True, "checks": ["已定位模板"], "issues": []},
            },
            "summary": "已定位模板，需补充输入。",
            "steps_used": 3,
            "tool_log": [{"tool": "workflow.template.resolve", "ok": True}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="制作30秒视频工作流",
        inputs={"facts": {"duration_seconds": 30}},
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert "error_kind" not in result
    payload = result["result"]
    assert payload["status"] == "completed"
    assert payload["decision"] == "reuse_existing"
    assert payload["template_id"] == "general_short_drama_workflow"
    assert "input_schema" not in payload
    assert [field["id"] for field in payload["input_fields"]] == [
        "plot",
        "style",
        "video_type",
        "episode_count",
        "duration_seconds",
        "segment_seconds",
    ]
    assert all("missing" not in field for field in payload["input_fields"])
    assert all("question" not in field for field in payload["input_fields"])
    assert "known_input_values" not in payload
    assert "missing_questions" not in payload
    assert "run_ready" not in payload
    assert payload["validation"]["ok"] is True
    assert "input_fields" in payload["next_action"]
    assert "workflow" not in payload
    assert "spec" not in payload


@pytest.mark.asyncio
async def test_agent_run_workflow_spec_blocks_artifact_ref_even_with_self_check(monkeypatch, tmp_path) -> None:
    async def fake_mode(project_id: str) -> str:
        return "builder"

    monkeypatch.setattr(workflow_spec_artifacts, "tool_results_dir", lambda: tmp_path)
    saved = workflow_spec_artifacts.save_workflow_spec_artifact(
            project_id="project-1",
            workflow={
                "schema": "openreel.workflow.v2",
                "id": "committed_workflow",
                "title": "已提交工作流",
                "inputs": {},
                "steps": [
                    {"id": "brief", "title": "需求", "kind": "text", "prompt": {"task": "整理需求。"}},
                    {"id": "script", "title": "剧本", "kind": "text", "needs": ["brief"], "prompt": {"task": "写剧本。"}},
                ],
        },
        self_check={"passed": True, "checks": ["已校验"], "issues": []},
    )

    async def fake_subagent_run(**kwargs):
        return {
            "result": {
                "status": "completed",
                "artifact_ref": saved["artifact_ref"],
                "preview": saved["preview"],
                "validation": {"ok": True, "step_count": 2},
                "self_check": {"passed": False, "checks": [], "issues": ["hook_review 自依赖"]},
            },
            "summary": "已保存 artifact。",
            "steps_used": 3,
            "tool_log": [{"tool": "workflow.spec.apply_patch", "ok": True}],
            "error": "",
        }

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_run(
        project_id="project-1",
        agent="workflow_spec",
        task="选择合适的工作流模板",
        inputs={},
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["result"]["validation"]["ok"] is False
    assert "不能返回 artifact_ref" in result["result"]["blocked_reason"]


@pytest.mark.asyncio
async def test_agent_review_coerces_string_evidence_and_loads_app_skill(monkeypatch) -> None:
    captured = {}

    async def fake_subagent_run(**kwargs):
        captured.update(kwargs)
        return {"error": "", "result": {"status": "pass"}, "summary": "ok"}

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(
        project_id="project-1",
        review_goal="检查是否应该修原节点",
        user_request="降低分辨率重新生成图片",
        work_summary="已新建一个替代分镜节点",
        evidence="node_id=d213; status=idle",
        custom_checklist="检查是否应改原节点；检查是否漏掉人物和场景",
        guide_topics="video_production",
        focus="工具选择, 修复范围",
        review_skill="video_production",
    )

    assert result["summary"] == "ok"
    inputs = captured["inputs"]
    assert inputs["evidence"] == {"text": "node_id=d213; status=idle"}
    assert inputs["custom_checklist"] == ["检查是否应改原节点", "检查是否漏掉人物和场景"]
    assert inputs["guide_topics"] == ["video_production"]
    assert inputs["focus"] == ["工具选择", "修复范围"]
    assert inputs["review_skill"]["ok"] is True
    assert inputs["review_skill"]["source"] == "app_skill"
    assert "每个节点都是独立任务" in inputs["review_skill"]["content"]


@pytest.mark.asyncio
async def test_agent_review_returns_blocked_result_when_subagent_errors(monkeypatch) -> None:
    project_id = "project-1"

    async def fake_subagent_run(**kwargs):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(project_id=project_id, review_goal="检查提示词")

    assert "error" not in result
    assert result["subagent_error"].startswith("subagent_exception")
    assert result["result"]["status"] == "blocked"
    assert result["result"]["passed"] is False
    assert result["result"]["safe_to_run"] is False
    assert result["result"]["safe_to_submit"] is False
    assert result["result"]["session_status"] == "exception"
    assert result["result"]["parse_status"] == "not_run"
    assert result["summary"].startswith("审查阻塞")


@pytest.mark.asyncio
async def test_agent_review_blocks_non_object_finish_result(monkeypatch) -> None:
    project_id = "project-1"

    async def fake_subagent_run(**kwargs):
        return {"error": "", "result": "looks good", "summary": "ok", "steps_used": 1}

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(project_id=project_id, review_goal="检查节点图")

    assert result["summary"].startswith("审查阻塞")
    assert result["result"]["status"] == "blocked"
    assert result["result"]["parse_status"] == "invalid_result_type"
    assert result["result"]["session_status"] == "completed"
    assert result["result"]["safe_to_submit"] is False


@pytest.mark.asyncio
async def test_agent_review_blocks_missing_status_result(monkeypatch) -> None:
    project_id = "project-1"

    async def fake_subagent_run(**kwargs):
        return {"error": "", "result": {"passed": True}, "summary": "ok", "steps_used": 1}

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(project_id=project_id, review_goal="检查节点图")

    assert result["result"]["status"] == "blocked"
    assert result["result"]["parse_status"] == "invalid_status"
    assert result["result"]["failure_reason"] == "missing_or_invalid_status"
    assert result["result"]["safe_to_submit"] is False


@pytest.mark.asyncio
async def test_agent_review_blocks_timeout(monkeypatch) -> None:
    project_id = "project-1"
    monkeypatch.setattr(agent_tools, "_agent_review_timeout_seconds", lambda max_steps: 0.01)

    async def fake_subagent_run(**kwargs):
        await asyncio.sleep(0.05)
        return {"error": "", "result": {"status": "pass"}, "summary": "ok"}

    monkeypatch.setattr(agent_tools, "subagent_run", fake_subagent_run)

    result = await agent_tools.agent_review(project_id=project_id, review_goal="检查节点图")

    assert result["subagent_error"] == "subagent_timeout"
    assert result["result"]["status"] == "blocked"
    assert result["result"]["session_status"] == "timeout"
    assert result["result"]["timed_out"] is True
    assert result["result"]["safe_to_submit"] is False


@pytest.mark.asyncio
async def test_subagent_run_rejects_custom_write_tools_before_llm() -> None:
    result = await agent_tools.subagent_run(
        project_id="project-1",
        role="reviewer",
        task="检查计划",
        allowed_tools=["node.create", "node.list"],
    )

    assert result["error"] == "readonly_tool_denied"
    assert result["steps_used"] == 0
    assert result["denied_tools"] == ["node.create"]
    assert result["allowed_tools"] == ["node.list"]
    assert result["tool_log"] == [
        {
            "tool": "node.create",
            "ok": False,
            "error": "readonly_tool_denied",
            "step": 0,
        }
    ]
