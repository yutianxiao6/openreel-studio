from agent_plan_contract_helpers import *  # noqa: F401,F403

def test_confirmation_protocol_reads_only_structured_decision_metadata() -> None:
    assert decision_action(None, "blueprint_revision") == ("", "")
    assert decision_action({"message": "确认"}, "blueprint_revision") == ("", "")

    metadata = _decision_metadata(
        "blueprint_revision",
        "confirm",
        feedback="应用这个修订",
    )
    decision = decision_from_user_metadata(metadata)

    assert decision.kind == "blueprint_revision"
    assert decision.action == "confirm"
    assert decision.feedback == "应用这个修订"
    assert decision_action(metadata, "blueprint_revision") == ("confirm", "应用这个修订")
    assert decision_action(metadata, "blueprint_section_review") == ("", "")

def test_build_pending_confirmation_uses_explicit_protocol_shape() -> None:
    confirmation = build_pending_confirmation(
        kind="reset",
        risk="destructive",
        actions=["confirm", "cancel"],
        confirmation_id="confirm-1",
        title="重置项目",
        summary="清空当前项目。",
        checksum="abc",
        can_skip=False,
        expires_at=123,
    )

    assert confirmation["id"] == "confirm-1"
    assert confirmation["kind"] == "reset"
    assert confirmation["risk"] == "destructive"
    assert confirmation["actions"] == ["confirm", "cancel"]
    assert confirmation["checksum"] == "abc"
    assert confirmation["can_skip"] is False
    assert confirmation["expires_at"] == 123
    assert isinstance(confirmation["created_at"], int)

def test_pending_confirmation_expiry_requires_explicit_expires_at() -> None:
    assert confirmation_expires_at(now=100, ttl_seconds=30) == 130
    assert is_pending_confirmation_expired({"scope": "full", "ts": 1}, now=100) is False
    assert is_pending_confirmation_expired({"expires_at": 99}, now=100) is True
    assert is_pending_confirmation_expired({"expires_at": "101"}, now=100) is False
    assert is_pending_confirmation_expired({"expires_at": "1970-01-01T00:01:39+00:00"}, now=100) is True

def test_expired_pending_confirmation_patch_clears_only_expired_protocol_keys() -> None:
    state = {
        "_pending_reset_confirm": {
            "scope": "full",
            "reason": "old reset",
            "expires_at": 90,
            "ts": 10,
        },
        "pending_blueprint_revision": {
            "status": "pending_review",
            "version": 2,
            "expires_at": 120,
        },
        "pending_blueprint_section_review": {
            "next_section_index": 3,
        },
        "pending_plan": {
            "id": "plan-1",
            "expires_at": 1,
        },
    }

    patch, expired = expired_pending_confirmation_patch(state, now=100)

    assert patch == {"_pending_reset_confirm": None}
    assert expired == [
        {
            "state_key": "_pending_reset_confirm",
            "confirmation_kind": "reset_project",
            "confirmation_id": None,
            "risk": None,
            "scope": "full",
            "target": None,
            "created_at": 10,
            "expires_at": 90,
            "version": None,
            "target_node_id": None,
        }
    ]

def test_blueprint_revision_skip_confirmations_gate_only_allows_low_risk_patch() -> None:
    from app.agent import blueprint_revision

    low = blueprint_revision._risk_for_revision_ops(
        [{"op": "replace", "path": "story.episodes[0].segments[0].plot", "value": "新段落"}],
        ["story.episodes[0].segments[0].plot"],
    )
    medium = blueprint_revision._risk_for_revision_ops(
        [{"op": "replace", "path": "story.global_outline", "value": "新主线"}],
        ["story.global_outline"],
    )
    high = blueprint_revision._risk_for_revision_ops(
        [{"op": "remove", "path": "characters[0]"}],
        ["characters[0]"],
    )

    assert low["risk"] == "low"
    assert low["requires_confirmation"] is False
    assert medium["risk"] == "medium"
    assert medium["requires_confirmation"] is True
    assert high["risk"] == "high"
    assert high["requires_confirmation"] is True

def test_generate_plan_keeps_node_first_core_surface() -> None:
    visible = _visible_tools("generate_plan")
    assert "agent.planner_make_plan" not in visible
    assert "node.create" in visible
    assert "node.run" in visible
    assert "skill.search" in visible
    assert "skill.get" in visible
    assert "skill.video_production" not in visible
    assert "canvas.connect_nodes" not in visible
    assert "plan.propose" not in visible
    assert "tool.search" in visible
    assert "node.draw_character" not in visible
    assert "plan.approve" not in visible
    assert "plan.clear" not in visible

def test_agent_tool_surface_is_stable_across_user_messages() -> None:
    assert _visible_tools(None) == _visible_tools("generate_plan")
    assert _visible_tools(None) == _visible_tools("switch_model")
    assert _visible_tools(None) == _visible_tools("generate_image")
    assert _visible_tools(None) == _visible_tools("generate_image_video")

def test_memory_auto_summarization_ignores_assistant_drafts() -> None:
    from app.mcp_tools.memory_tools import memory_summarization_messages

    payload = memory_summarization_messages([
        {"role": "assistant", "content": "白衣剑修与黑袍魔修在断崖大战。"},
        {"role": "tool", "content": "{\"draft\":\"未确认蓝图\"}"},
        {"role": "user", "content": "已提交：视频蓝图基础信息\n- 主题：两个修士大战"},
    ])

    assert payload == [
        {"role": "user", "content": "已提交：视频蓝图基础信息\n- 主题：两个修士大战"}
    ]

def test_prompt_namespace_hints_match_core_agent_surface() -> None:
    namespaces = set(select_tool_namespaces(PromptContext(project_id="p1")))

    assert namespaces == {
        "agent",
        "canvas",
        "interaction",
        "node",
        "project",
        "skill",
        "task",
        "tool",
        "vision",
    }
    assert not {"blueprint", "scene", "shot", "asset", "plan"} & namespaces

def test_agent_tool_surface_matches_node_first_contract() -> None:
    visible = _visible_tools(None)

    assert visible == {
        "agent.review",
        "canvas.delete",
        "interaction.request_input",
        "node.create",
        "node.get",
        "node.list",
        "node.run",
        "node.update",
        "project.get_state",
        "project.reset",
        "skill.get",
        "skill.search",
        "task.complete",
        "task.create",
        "task.list",
        "task.update",
        "tool.describe",
        "tool.execute",
        "tool.search",
        "vision.view_image",
    }
    assert len(visible) == 20

    assert registry.tool_exposure("agent.review") == "core"
    assert registry.tool_exposure("node.create") == "core"
    assert registry.tool_exposure("canvas.delete") == "core"
    assert registry.tool_exposure("project.reset") == "core"
    assert registry.tool_exposure("skill.get") == "core"
    assert registry.tool_exposure("skill.search") == "core"
    assert registry.tool_exposure("skill.video_production") == "deferred"
    assert registry.tool_exposure("task.create") == "core"
    assert registry.tool_exposure("task.list") == "core"
    assert registry.tool_exposure("task.update") == "core"
    assert registry.tool_exposure("task.complete") == "core"
    assert registry.tool_exposure("canvas.connect_nodes") == "unregistered"
    assert registry.tool_exposure("node.get_creation_guide") == "unregistered"
    assert registry.tool_exposure("tool.search") == "core"
    assert registry.tool_exposure("tool.describe") == "core"
    assert registry.tool_exposure("tool.execute") == "core"
    assert registry.tool_exposure("vision.view_image") == "core"
    assert registry.tool_exposure("blueprint.get") == "unregistered"
    assert registry.tool_exposure("blueprint.propose_tree") == "unregistered"
    assert registry.tool_exposure("node.draw_character") == "unregistered"

    retired_blueprint_tools: set[str] = {
        "blueprint.get",
        "blueprint.revise",
        "blueprint.start_tree_draft",
        "blueprint.append_tree_node",
        "blueprint.update_tree_node",
        "blueprint.finalize_tree_draft",
        "blueprint.propose_tree",
        "blueprint.add_child",
        "blueprint.delete_node",
        "blueprint.list_children",
        "blueprint.set_prompt",
        "blueprint.update_node",
    }
    for name in retired_blueprint_tools:
        assert registry.get(name) is None, name
        assert registry.tool_exposure(name) == "unregistered", name
        assert name not in visible

    deferred_control = {
        "drama.parse_uploaded_script",
        "memory.compact_context",
        "memory.recall",
        "project.create",
        "media.cancel_image_generation",
        "media.describe_image",
        "task.delete",
    }
    for name in deferred_control:
        spec = registry.get(name)
        assert spec is not None, name
        assert tool_meta_tools._tier_of(spec) == 2, name
        assert name not in visible

    hidden_control_plane = set(AGENT_HIDDEN_PROJECT_MODE_TOOL_NAMES)
    for name in hidden_control_plane:
        spec = registry.get(name)
        assert spec is not None, name
        assert tool_meta_tools._tier_of(spec) == 3, name
        assert name not in visible


def test_node_create_schema_uses_single_references_entrypoint() -> None:
    spec = registry.get("node.create")
    top_level_props = spec.schema["properties"]
    fields = spec.schema["properties"]["fields"]["properties"]
    refs = fields["references"]
    role_enum = refs["items"]["oneOf"][1]["properties"]["role"]["enum"]

    assert top_level_props["type"]["enum"] == ["text", "image", "video", "audio"]
    assert top_level_props["nodes"]["items"]["properties"]["type"]["enum"] == ["text", "image", "video", "audio"]
    assert "prompt" not in top_level_props
    assert "name" not in top_level_props
    assert "nodes" in top_level_props
    assert spec.schema["required"] == ["project_id"]
    assert "prompt" in fields
    assert "title" in fields
    assert "references" in fields
    assert "depends_on" not in fields
    assert "reference_images" not in fields
    assert "source_image" in role_enum
    assert "visual_reference" in role_enum
    assert "16:9" in fields["aspect_ratio"]["description"]
    assert "精确像素" in fields["resolution"]["description"]
    assert "2560x1440" in fields["resolution"]["description"]
    assert "high" in fields["quality"]["description"]


def test_node_update_schema_prefers_input_patch_and_keeps_backend_alias_hidden() -> None:
    spec = registry.get("node.update")
    assert "required" not in spec.schema

    patch_props = spec.schema["properties"]["patch"]["properties"]
    assert "node_ids" in spec.schema["properties"]
    assert "updates" in spec.schema["properties"]
    assert "input_json" in patch_props
    assert "fields" not in patch_props
    assert "resolution" in patch_props["input_json"]["properties"]
    props = patch_props["input_json"]["properties"]
    for key in ("references", "depends_on"):
        items = props[key]["items"]
        assert "oneOf" in items
        assert {"type": "string"} in items["oneOf"]
    assert "局部合并" in spec.description


def test_agent_review_schema_keeps_structured_optional_arguments() -> None:
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(PromptContext()))
    review = next(tool for tool in tools if tool["function"]["name"] == "agent__review")
    props = review["function"]["parameters"]["properties"]

    assert props["evidence"]["type"] == "object"
    assert props["custom_checklist"]["type"] == "array"
    assert props["guide_topics"]["type"] == "array"
    assert props["focus"]["type"] == "array"
    assert props["custom_checklist"]["items"]["type"] == "string"


def _array_schema_errors(schema: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(schema, dict):
        if schema.get("type") == "array":
            items = schema.get("items")
            if not isinstance(items, dict):
                errors.append(f"{path or '<root>'}: missing object items")
            elif not any(key in items for key in ("type", "oneOf", "anyOf", "allOf")):
                errors.append(f"{path or '<root>'}.items: missing type/union")
        for key, value in schema.items():
            next_path = f"{path}.{key}" if path else str(key)
            errors.extend(_array_schema_errors(value, next_path))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            errors.extend(_array_schema_errors(value, f"{path}[{index}]"))
    return errors


def test_registered_tool_schemas_do_not_expose_arrays_without_items() -> None:
    errors: list[str] = []
    for spec in registry.list_tools():
        errors.extend(
            f"{spec.name}: {error}"
            for error in _array_schema_errors(spec.schema or {})
        )

    assert errors == []


def test_agent_loop_tool_schemas_are_provider_safe_for_arrays() -> None:
    ctx = PromptContext(project_id="tool-schema-array-check", user_message="hello", state={})
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(ctx))
    errors: list[str] = []
    for tool in tools:
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "").replace("__", ".")
        errors.extend(
            f"{name}: {error}"
            for error in _array_schema_errors(fn.get("parameters") or {})
        )

    assert errors == []


@pytest.mark.asyncio
async def test_interaction_request_input_accepts_codex_style_questions() -> None:
    result = await interaction_tools.request_input(
        project_id="project-1",
        questions=[
            {
                "id": "scope",
                "header": "范围",
                "question": "这次先处理哪个范围？",
                "options": [
                    {"label": "只修当前问题", "description": "最快，改动最小"},
                    {"label": "顺手整理相邻代码", "description": "范围稍大"},
                ],
            },
            {
                "header": "验证",
                "id": "validation",
                "question": "需要我跑哪类验证？",
                "options": [
                    {"label": "只跑相关测试", "description": "速度快，覆盖当前改动"},
                    {"label": "跑完整测试", "description": "更慢，但覆盖面更广"},
                    {"label": "暂不验证", "description": "只改代码，不执行测试"},
                ],
            },
        ],
    )

    assert result["ok"] is True
    assert result["intake"]["title"] == "范围"
    assert [question["id"] for question in result["intake"]["questions"]] == ["scope", "validation"]
    assert "fields" not in result["intake"]
    assert "presentation" not in result["intake"]
    assert result["intake"]["questions"][0]["options"][0] == {
        "label": "只修当前问题",
        "description": "最快，改动最小",
    }


@pytest.mark.asyncio
async def test_interaction_request_input_accepts_up_to_six_questions() -> None:
    questions = [
        {"id": f"q_{index}", "header": f"问题{index}", "question": f"第 {index} 个问题？"}
        for index in range(1, 7)
    ]

    result = await interaction_tools.request_input(
        project_id="project-1",
        questions=questions,
    )

    assert result["ok"] is True
    assert len(result["intake"]["questions"]) == 6

    rejected = await interaction_tools.request_input(
        project_id="project-1",
        questions=[
            {"id": f"q_{index}", "header": f"问题{index}", "question": f"第 {index} 个问题？"}
            for index in range(1, 8)
        ],
    )
    assert rejected["ok"] is False
    assert "at most 6" in rejected["error"]

@pytest.mark.asyncio
async def test_interaction_request_input_filters_collected_video_intake_questions(monkeypatch) -> None:
    async def fake_read_project_state(project_id: str) -> dict:
        return {
            "pending_video_blueprint_request": {
                "collected_facts": {
                    "aspect_ratio": "16:9",
                    "duration_seconds": "15秒",
                }
            }
        }

    monkeypatch.setattr(interaction_tools, "_read_project_state", fake_read_project_state)

    mixed = await interaction_tools.request_input(
        project_id="project-1",
        title="补充篮球短片信息",
        purpose="video_blueprint_intake",
        stage="basic",
        questions=[
            {
                "id": "aspect_ratio",
                "header": "画幅",
                "question": "画幅比例？",
                "options": [
                    {"label": "16:9", "description": "横屏"},
                    {"label": "9:16", "description": "竖屏"},
                ],
            },
            {
                "id": "scene",
                "header": "场景",
                "question": "场景环境？",
                "options": [
                    {"label": "模型规划", "description": "由模型决定"},
                    {"label": "室内", "description": "室内场景"},
                ],
            },
        ],
    )

    assert mixed["ok"] is True
    assert [question["id"] for question in mixed["intake"]["questions"]] == ["scene"]
    assert mixed["intake"]["collected_facts"]["aspect_ratio"] == "16:9"
    assert mixed["intake"]["omitted_collected_questions"][0]["fact"] == "aspect_ratio"

    duplicate_only = await interaction_tools.request_input(
        project_id="project-1",
        title="确认画幅",
        purpose="video_blueprint_intake",
        stage="basic",
        questions=[
            {
                "id": "aspect_ratio",
                "header": "画幅",
                "question": "画幅比例？",
                "options": [
                    {"label": "16:9", "description": "横屏"},
                    {"label": "9:16", "description": "竖屏"},
                ],
            }
        ],
    )

    assert duplicate_only["ok"] is False
    assert duplicate_only["error_kind"] == "intake_questions_already_collected"
    assert duplicate_only["collected_facts"]["aspect_ratio"] == "16:9"

@pytest.mark.asyncio
async def test_interaction_request_input_accepts_free_text_questions_without_options() -> None:
    result = await interaction_tools.request_input(
        project_id="project-1",
        questions=[{"id": "character", "header": "角色", "question": "主角是谁？"}],
    )

    assert result["ok"] is True
    question = result["intake"]["questions"][0]
    assert question["id"] == "character"
    assert question["options"] == []

def test_registered_tool_descriptions_are_present_and_concise() -> None:
    """Every tool must have a non-empty description."""
    missing: list[str] = []
    for spec in registry.list_tools():
        description = (spec.description or "").strip()
        if not description:
            missing.append(spec.name)

    assert missing == [], f"Tools without descriptions: {missing}"

def test_core_tool_descriptions_follow_codex_style_short_contract() -> None:
    ctx = PromptContext(project_id="core-tool-style", user_message="hello", state={})
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(ctx))
    old_contract_markers = ("边界：", "用法：", "示例：")
    too_long: list[str] = []
    old_style: list[str] = []

    for tool in tools:
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "").replace("__", ".")
        description = str(fn.get("description") or "").strip()
        if any(marker in description for marker in old_contract_markers):
            old_style.append(name)
        if len(description) > 260:
            too_long.append(name)

    assert old_style == []
    assert too_long == []

def test_registered_tool_specs_expose_boundary_metadata() -> None:
    required_attrs = {
        "is_read_only",
        "is_destructive",
        "requires_confirmation",
        "is_concurrency_safe",
        "max_result_size",
    }
    missing: list[str] = []
    for spec in registry.list_tools():
        for attr in required_attrs:
            if not hasattr(spec, attr):
                missing.append(f"{spec.name}.{attr}")

    assert missing == []
    reset = registry.get("project.reset")
    get_state = registry.get("project.get_state")
    canvas_delete = registry.get("canvas.delete")
    assert reset is not None
    assert reset.is_destructive is True
    assert reset.requires_confirmation is True
    assert reset.is_read_only is False
    assert get_state is not None
    assert get_state.is_read_only is True
    assert get_state.is_concurrency_safe is True
    assert canvas_delete is not None
    assert canvas_delete.is_destructive is True
    assert canvas_delete.requires_confirmation is True

def test_tool_error_normalizer_fills_missing_contract_fields() -> None:
    from app.agent.tool_errors import normalize_tool_result

    result = normalize_tool_result({"error": "Project not found"}, tool_name="project.get_state")

    assert result["ok"] is False
    assert result["error"] == "Project not found"
    assert result["error_kind"] == "tool_error"
    assert result["tool"] == "project.get_state"
    assert result["hint"]
    assert result["suggested_next"] == "model_decides"
    assert result["model_feedback"]["what_went_wrong"] == "Project not found"
    assert result["model_feedback"]["how_to_fix"] == result["hint"]
    assert "不要用完全相同参数重复调用" in result["model_feedback"]["retry_policy"]

def test_tool_error_normalizer_maps_common_id_errors_to_state_recovery() -> None:
    from app.agent.tool_errors import normalize_tool_result

    result = normalize_tool_result(
        {
            "ok": False,
            "error": "节点 'segment_01' 不存在。",
            "error_kind": "node_not_found",
            "node_id": "segment_01",
            "available_node_ids": ["story_synopsis", "characters"],
        },
        tool_name="node.get",
    )

    assert result["suggested_next"] == "read_state"
    assert "真实 id" in result["hint"]
    assert result["model_feedback"]["evidence"]["node_id"] == "segment_01"
    assert result["model_feedback"]["evidence"]["available_node_ids"] == ["story_synopsis", "characters"]

def test_tool_error_normalizer_preserves_confirmation_requests() -> None:
    from app.agent.tool_errors import normalize_tool_result

    original = {
        "ok": False,
        "requires_user_confirm": True,
        "scope": "full",
        "reason": "用户请求重置",
    }
    result = normalize_tool_result(original, tool_name="project.reset")

    assert result == original
    assert "error" not in result
    assert result["requires_user_confirm"] is True

@pytest.mark.asyncio
async def test_tool_describe_includes_boundary_metadata_for_core_reset() -> None:
    describe = await tool_meta_tools.tool_describe(["project.reset"])
    tool = describe["tools"][0]
    assert tool["boundaries"]["is_destructive"] is True
    assert tool["boundaries"]["requires_confirmation"] is True
    assert tool["boundaries"]["is_read_only"] is False

def test_interaction_request_input_description_is_generic_card_contract() -> None:
    spec = registry.get("interaction.request_input")
    assert spec is not None
    description = spec.description or ""
    schema_props = (spec.schema or {}).get("properties") or {}

    assert "questions" in description
    assert "questions" in schema_props
    assert schema_props["questions"]["maxItems"] == 6
    item_required = schema_props["questions"]["items"]["required"]
    assert "options" not in item_required
    assert "fields" not in schema_props
    assert "presentation" not in schema_props
    assert "segment_seconds" not in description
    assert "15秒以内默认不分段" not in description
    assert "purpose='video_blueprint_intake'" not in description
    assert "批准" in description


def test_node_read_tools_support_index_then_batch_detail_contract() -> None:
    get_spec = registry.get("node.get")
    list_spec = registry.get("node.list")
    assert get_spec is not None
    assert list_spec is not None

    get_props = (get_spec.schema or {}).get("properties") or {}
    list_props = (list_spec.schema or {}).get("properties") or {}

    assert get_props["node_ids"]["type"] == "array"
    assert get_props["node_ids"]["items"]["type"] == "string"
    assert "node_ids" in (get_spec.description or "")
    assert "query" in get_props
    assert "regex" in get_props
    assert list_props["limit"]["type"] == "integer"
    assert "query" in list_props
    assert "regex" in list_props
    assert "默认返回 20" in (list_spec.description or "")
    assert "limit=0" in (list_spec.description or "")

def test_agent_visible_tool_descriptions_do_not_advertise_retired_shortcuts() -> None:
    retired_markers = {
        "assets.set_library_path",
        "config.patch",
        "config.write_file",
        "drama.delete_",
        "media.add_provider",
        "media.generate_",
        "mcp.list_servers",
        "model.set_config",
        "node.draw_",
        "panel.",
        "plan.approve",
        "project.update_state",
        "session.",
        "task.get",
    }
    exposed_descriptions = "\n".join(
        spec.description or ""
        for spec in registry.list_tools()
        if tool_meta_tools._tier_of(spec) != 3
    )

    for marker in retired_markers:
        assert marker not in exposed_descriptions

@pytest.mark.asyncio
async def test_agent_can_request_context_compaction_via_deferred_tool() -> None:
    visible = _visible_tools(None)
    assert "memory.compact_context" not in visible

    result = await tool_meta_tools.tool_search(query="compact memory", category="memory")
    names = {item["name"] for item in result["tools"]}
    assert "memory.compact_context" in names

def test_legacy_blueprint_tools_are_not_agent_tools() -> None:
    visible = _visible_tools(None)
    retired = {
        "blueprint.get",
        "blueprint.revise",
        "blueprint.start_tree_draft",
        "blueprint.append_tree_node",
        "blueprint.update_tree_node",
        "blueprint.finalize_tree_draft",
        "blueprint.clear",
        "blueprint.save_from_plan",
    }
    assert not retired & visible
    for name in retired:
        assert registry.tool_exposure(name) == "unregistered"

def test_low_frequency_tools_are_deferred_and_reset_is_core() -> None:
    visible = _visible_tools(None)
    assert "canvas.clear_all" not in visible
    assert "project.create" not in visible
    assert "project.reset" in visible
    assert "media.describe_image" not in visible
    assert "tool.search" in visible
    assert "tool.describe" in visible
    assert "tool.execute" in visible

def test_project_mentor_skill_is_loaded_but_not_in_core_tool_surface() -> None:
    assert registry.get("skill.project_mentor") is not None
    assert "skill.project_mentor" not in _visible_tools(None)

@pytest.mark.asyncio
async def test_project_mentor_exposes_prompt_compaction_topic() -> None:
    tool = registry.get("skill.project_mentor")
    assert tool is not None

    result = await tool.handler(topic="prompt_compaction")

    assert result["topic"] == "prompt_compaction"
    assert result["references_count"] > 0
    assert "file.read_text" in result["reference_policy"]
    assert "permission policy" in result["guidance"]

def test_agent_prompt_sections_use_current_video_mode_names() -> None:
    prompt_text = "\n".join(
        [
            core_rules.PROMPT,
            flow_paths.PROMPT,
            segment_rule.PROMPT,
            node_contract.PROMPT,
            single_image_rule.PROMPT,
            video_types.PROMPT,
        ]
    )

    assert "shot_list" not in prompt_text
    assert "text" in prompt_text
    assert "image" in prompt_text
    assert "video" in prompt_text
    assert "image_to_video_method" not in prompt_text
    assert "node.get_creation_guide" not in prompt_text

def test_single_image_prompt_documents_reference_image_to_image_path() -> None:
    prompt_text = "\n".join(
        [
            core_rules.PROMPT,
            single_image_rule.PROMPT,
            node_contract.PROMPT,
        ]
    )

    assert "图生图" in prompt_text
    assert "fields.references" in prompt_text
    assert "source_image" in prompt_text
    assert "node.list" in prompt_text
    assert "node.list(query|regex)" in prompt_text
    assert "node.list(limit=0)" in prompt_text
    assert "active/user skill" in prompt_text
    assert "prompt rules" in prompt_text
    assert "不要进入蓝图或计划流程" in prompt_text

def test_prompt_rules_prioritize_latest_user_message_over_historical_failures() -> None:
    prompt_text = "\n".join([core_rules.PROMPT, task_loop.PROMPT])
    assert "任务是轻量执行账本" in prompt_text
    assert "Complex requests create a short outcome checklist" in prompt_text
    assert 'task.create(items=[...], mode="sequential")' in prompt_text
    assert "simple Q&A or one-node edits may skip tasks" in prompt_text
    assert "Follow active skill" in prompt_text
    assert "active skill" in prompt_text
    assert "check output against user/active skill" in prompt_text
    assert "task.delete" not in prompt_text
    assert "task.complete" in prompt_text
    assert "历史 status=failed 节点" in rerun_rule.PROMPT
    assert "不要主动重跑" in rerun_rule.PROMPT
    assert "历史失败节点只是背景提醒" in repair_rule.PROMPT
    reminder = AgentOrchestrator._build_checklist_reminder(
        {"project_mode": "single_node"},
        {"total": 1, "by_type": {"character": 1}, "by_status": {"failed": 1}},
    )
    assert reminder == ""
    assert "node.run(action" not in reminder

def test_prompt_cache_sensitive_snapshot_for_blank_turn() -> None:
    ctx = PromptContext(
        project_id="cache-snapshot",
        user_message="hello",
        state={},
        attachments=[],
    )
    result = assemble_split_result(ctx)
    tools = registry.get_tools_for_agent_loop(namespaces=select_tool_namespaces(ctx))
    core_tools = []
    for tool in tools:
        fn = tool.get("function") or {}
        core_tools.append({
            "name": str(fn.get("name") or "").replace("__", "."),
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or {},
        })
    core_tools.sort(key=lambda item: item["name"])
    assert [section.name for section in result.sections] == [
        "identity",
        "working_loop",
        "task_loop",
        "tool_loader",
        "core_rules",
        "delete_rule",
        "memory_write",
        "runtime_context",
    ]
    assert len(result.system or "") < 2200
    assert "本轮用户目标" not in (result.system or "")
    assert "本轮用户目标" not in (result.runtime or "")
    assert "项目标题" in (result.runtime or "")
    assert len(result.history or "") > 900
    assert len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) < 11_500
    assert len(result.system or "") + len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) < 13_500
    def has_schema_description_metadata(value, *, inside_properties: bool = False) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "description" and not inside_properties:
                    return True
                if has_schema_description_metadata(child, inside_properties=(key == "properties")):
                    return True
        if isinstance(value, list):
            return any(has_schema_description_metadata(item) for item in value)
        return False

    assert not has_schema_description_metadata([tool["parameters"] for tool in core_tools])
    assert [tool["name"] for tool in core_tools] == [
        "agent.review",
        "canvas.delete",
        "interaction.request_input",
        "node.create",
        "node.get",
        "node.list",
        "node.run",
        "node.update",
        "project.get_state",
        "project.reset",
        "skill.get",
        "skill.search",
        "task.complete",
        "task.create",
        "task.list",
        "task.update",
        "tool.describe",
        "tool.execute",
        "tool.search",
        "vision.view_image",
    ]

def test_agent_prompt_sections_do_not_advertise_retired_tool_names() -> None:
    prompt_text = "\n".join(
        [
            core_rules.PROMPT,
            flow_paths.PROMPT,
            memory_write.PROMPT,
            node_contract.PROMPT,
            plan_rule.PROMPT,
            rerun_rule.PROMPT,
            segment_rule.PROMPT,
            single_image_rule.PROMPT,
            tool_loader.PROMPT,
            video_types.PROMPT,
        ]
    )
    retired_markers = {
        "assets.set_library_path",
        "config.patch",
        "config.write_file",
        "drama.delete_",
        "media.add_provider",
        "media.generate_",
        "mcp.list_servers",
        "model.set_config",
        "node.draw_",
        "panel.",
        "plan.approve",
        "project.update_state",
        "session.",
        "task.get",
    }

    for marker in retired_markers:
        assert marker not in prompt_text

def test_generation_prompt_bodies_do_not_advertise_retired_tool_names() -> None:
    ctx = WorkerContext(workflow_mode="grid", grid="2*3")
    prompt_text = "\n".join(
        [
            default_prompt_for("drama.generate_image_prompt", ctx),
            default_prompt_for("drama.generate_storyboard_grid", ctx),
            default_prompt_for("drama.generate_segment_shots", ctx),
        ]
    )
    retired_markers = {
        "drama.generate_",
        "media.generate_",
        "node.draw_",
        "session.",
    }

    for marker in retired_markers:
        assert marker not in prompt_text
