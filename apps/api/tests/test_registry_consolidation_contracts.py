from agent_plan_contract_helpers import *  # noqa: F401,F403

from app.agent.model_context.types import coerce_tool_output
from app.mcp_tools import skill_tools

@pytest.mark.asyncio
async def test_tool_search_supports_exact_select_for_deferred_tools() -> None:
    result = await tool_meta_tools.tool_search(
        query="select:system.models",
        category="system",
    )

    assert result["mode"] == "select"
    assert result["not_found"] == []
    assert [item["name"] for item in result["tools"]] == ["system.models"]
    assert result["tools"][0]["input_schema_summary"]["type"] == "object"
    assert "example" in result["tools"][0]


@pytest.mark.asyncio
async def test_tool_search_empty_query_lists_visible_deferred_catalog() -> None:
    result = await tool_meta_tools.tool_search(query="", limit=0)
    names = {item["name"] for item in result["tools"]}
    catalog_names = set(result["catalog"]["tool_names"])

    assert result["mode"] == "catalog"
    assert result["returned"] == result["total"]
    assert names == catalog_names
    assert "assets.save_to_project" in names
    assert "node.create" not in names
    for name in names:
        spec = registry.get(name)
        assert spec is not None, name
        assert tool_meta_tools._tier_of(spec) == 2, name


@pytest.mark.asyncio
async def test_tool_search_empty_category_lists_deferred_category_catalog() -> None:
    result = await tool_meta_tools.tool_search(query="", category="assets", limit=0)
    names = {item["name"] for item in result["tools"]}
    categories = {group["category"] for group in result["catalog"]["categories"]}

    assert result["mode"] == "catalog"
    assert categories == {"assets"}
    assert {
        "assets.get_library_path",
        "assets.save_to_project",
        "assets.save_to_shared",
        "assets.list_project",
        "assets.list_shared",
        "assets.read_asset",
        "assets.list_categories",
        "assets.create_category",
        "assets.move_asset",
        "assets.add_to_canvas",
    } <= names


@pytest.mark.asyncio
async def test_tool_search_supports_regex_patterns() -> None:
    result = await tool_meta_tools.tool_search(regex=r"workspace_(read|write)", category="file", limit=8)
    names = {item["name"] for item in result["tools"]}

    assert {"file.workspace_read", "file.workspace_write"} <= names
    assert any(item.get("match", {}).get("matched_patterns") == [r"workspace_(read|write)"] for item in result["tools"])


@pytest.mark.asyncio
async def test_tool_search_select_does_not_return_core_tools() -> None:
    result = await tool_meta_tools.tool_search(query="select:node.create")

    assert result["mode"] == "select"
    assert result["tools"] == []
    assert result["not_found"] == ["node.create"]

@pytest.mark.asyncio
async def test_tool_search_select_accepts_multiple_deferred_tools() -> None:
    result = await tool_meta_tools.tool_search(query="select:task.delete,system.models")
    names = {item["name"] for item in result["tools"]}

    assert result["mode"] == "select"
    assert result["not_found"] == []
    assert names == {"task.delete", "system.models"}


@pytest.mark.asyncio
async def test_task_delete_is_deferred_and_task_create_is_core() -> None:
    result = await tool_meta_tools.tool_search(
        query="select:task.create,task.delete",
        category="task",
    )
    names = {item["name"] for item in result["tools"]}

    assert result["mode"] == "select"
    assert result["not_found"] == ["task.create"]
    assert names == {"task.delete"}
    assert registry.tool_exposure("task.create") == "core"
    assert {item["tier"] for item in (await tool_meta_tools.tool_describe(sorted(names)))["tools"]} == {2}

def test_skill_packages_use_core_resource_tools() -> None:
    assert registry.tool_exposure("skills.list") == "core"
    assert registry.tool_exposure("skills.read") == "core"


@pytest.mark.asyncio
async def test_video_production_skill_guides_reference_driven_short_video_nodes() -> None:
    listed = coerce_tool_output(
        await skill_tools.skills_list({"kind": "orchestrator"})
    ).value
    item = next(item for item in listed["skills"] if item["name"] == "video_production")
    full = coerce_tool_output(
        await skill_tools.skills_read(
            item["authority"], item["package"], item["main_resource"]
        )
    ).value
    guide = full["contents"]

    assert full["resource"] == "SKILL.md"
    assert "视频制作入口指南" in guide
    assert "skills.list" in guide
    assert "skills.read" in guide
    assert "general_short_drama_workflow" in guide
    assert "不重新生成 spec" in guide
    assert "workflow_spec` 返回 blocked" in guide
    assert "workflow.run_step" in guide
    assert "workflow.run_next" in guide
    assert "workflow.run_all" in guide
    assert "script_writing" in guide
    assert "character_prompt" in guide
    assert "scene_prompt" in guide
    assert "shot_grid_prompt" in guide
    assert "video_prompt" in guide
    assert "Prompt Skill 索引" in guide
    assert "精确 `@参考图标签`" in guide
    assert "稳定的图片节点 ID" in guide
    assert "fields.director_capture=true" in guide
    assert "reference_usage=\"composition_only\"" in guide
    assert "不把构图参考自动当作视频首帧" in guide
    assert "默认模板" in guide
    assert "剧情/主题 `plot`" in guide
    assert "duration_seconds" in guide
    assert "episode_count" in guide
    assert "segment_seconds" in guide
    assert "video_type" in guide
    assert "aspect_ratio" in guide
    assert "durationSeconds" not in guide
    assert "final_video" not in guide
    assert "直接文生视频" in guide
    assert "task.create(items=" not in guide
    assert "`node.run`" in guide
    assert "agent.review" in guide
    assert "fields.content" in guide
    assert "`task` 只记录进度" in guide
    assert "role:\"visual_reference\"" in guide
    assert "role:\"source_image\"" in guide
    assert "`parent_node_id` 用于画布分组" not in guide
    assert "prompt 开头直接写几宫格" not in guide
    assert "官方设定集角色视觉参考表" not in guide
    assert "毛孔级写实特写" not in guide
    assert "grok-imagine-video-1.5" not in guide
    assert "role:\"visual_reference\"" in guide
    assert "role:\"source_image\"" in guide
    assert "source locator" in guide


@pytest.mark.asyncio
async def test_video_production_hands_off_explicit_story_template_requests() -> None:
    listed = coerce_tool_output(
        await skill_tools.skills_list({"kind": "orchestrator"})
    ).value
    item = next(item for item in listed["skills"] if item["name"] == "video_production")
    result = coerce_tool_output(
        await skill_tools.skills_read(item["authority"], item["package"], "SKILL.md")
    ).value

    assert "story_template_method" in result["contents"]
    assert "故事模板图/视觉开发板" in result["contents"]


def test_builtin_skill_packages_do_not_use_python_wrappers() -> None:
    skill_dirs = {
        Path(str(item["skill_dir"]))
        for item in skill_tools._build_unified_index()
        if item.get("scope") == "builtin"
    }

    assert skill_dirs
    assert all(not (skill_dir / "__init__.py").exists() for skill_dir in skill_dirs)


@pytest.mark.asyncio
async def test_story_template_method_is_separate_optional_guide() -> None:
    listed = coerce_tool_output(
        await skill_tools.skills_list({"kind": "orchestrator"})
    ).value
    item = next(item for item in listed["skills"] if item["name"] == "story_template_method")
    assert "复杂动作" in item["description"]
    full = coerce_tool_output(
        await skill_tools.skills_read(item["authority"], item["package"], "SKILL.md")
    ).value
    assert "image" in full["contents"]
    assert "video" in full["contents"]


@pytest.mark.asyncio
async def test_file_read_tools_can_be_discovered_in_file_category() -> None:
    file_tools = await tool_meta_tools.tool_search(query="file", category="file")
    names = {item["name"] for item in file_tools["tools"]}

    assert "file.read_text" in names
    assert "file.list_dir" in names

    described = await tool_meta_tools.tool_describe(["file.read_text", "file.list_dir"])
    assert described["not_found"] == []
    described_names = {tool["name"] for tool in described["tools"]}
    assert {"file.read_text", "file.list_dir"} <= described_names


def test_canvas_delete_is_the_registered_canvas_deletion_primitive() -> None:
    visible = _visible_tools(None)

    assert "canvas.delete" in visible
    assert registry.get("canvas.delete") is not None


@pytest.mark.asyncio
async def test_media_control_tool_is_deferred() -> None:
    result = await tool_meta_tools.tool_search(query="cancel image", category="control")
    names = {item["name"] for item in result["tools"]}
    assert "media.cancel_image_generation" in names

@pytest.mark.asyncio
async def test_system_tools_are_deferred() -> None:
    result = await tool_meta_tools.tool_search(query="system status", category="system")
    names = {item["name"] for item in result["tools"]}
    assert "system.status" in names
    assert "system.models" in names

@pytest.mark.asyncio
async def test_attachment_ingest_tool_is_deferred_and_discoverable() -> None:
    result = await tool_meta_tools.tool_search(query="parse uploaded script", category="attach")
    names = {item["name"] for item in result["tools"]}

    assert "drama.parse_uploaded_script" in names

    described = await tool_meta_tools.tool_describe(["drama.parse_uploaded_script"])
    assert described["not_found"] == []
    assert described["tools"][0]["tier"] == 2


def test_main_loop_only_node_run_takes_over_node_lifecycle() -> None:
    assert orchestrator_module._NODE_TARGET_TOOLS == {"node.run"}

def test_node_universal_uses_media_generation_service_for_media_runners() -> None:
    assert node_universal.media_generation is media_generation



@pytest.mark.asyncio
async def test_tool_execute_blocks_hidden_and_core_targets() -> None:
    hidden = await tool_meta_tools.tool_execute(
        project_id="test",
        name="node.draw_character",
        input={"name": "测试"},
    )
    core = await tool_meta_tools.tool_execute(
        project_id="test",
        name="node.list",
        input={},
    )

    assert hidden["error_kind"] == "unknown_deferred_tool"
    assert core["error_kind"] == "core_tool_should_be_called_directly"

@pytest.mark.asyncio
async def test_tool_execute_rejects_core_project_reset() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="test",
        name="project.reset",
        input={"scope": "full"},
        _state={},
        _user_message="创建一个新节点，不用删已有节点",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "core_tool_should_be_called_directly"
    assert result["tool"] == "project.reset"


@pytest.mark.asyncio
async def test_tool_execute_rejects_inline_workflow_run_objects() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="test",
        name="workflow.run_all",
        input={
            "workflow": {
                "id": "inline",
                "name": "Inline",
                "steps": [{"id": "story", "title": "Story", "node_type": "text"}],
            },
            "inputs": {"plot": "雨夜"},
        },
    )

    assert result["ok"] is False
    assert result["_deferred_tool"] == "workflow.run_all"
    assert result["error_kind"] == "workflow_inline_requires_workflow_spec"
    assert "template_id" in result["hint"]


@pytest.mark.asyncio
async def test_tool_execute_rejects_unauthorized_workflow_template_runs() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="test",
        name="workflow.run_all",
        input={
            "template_id": "general_short_drama_workflow",
            "inputs": {"plot": "雨夜", "durationSeconds": 30, "style": "冷色"},
        },
        _state={},
    )

    assert result["ok"] is False
    assert result["_deferred_tool"] == "workflow.run_all"
    assert result["error_kind"] == "workflow_ref_requires_workflow_spec"
    assert "workflow_spec" in result["hint"]


@pytest.mark.asyncio
async def test_tool_execute_allows_workflow_template_after_workflow_spec_authorization() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="test",
        name="workflow.run_all",
        input={
            "template_id": "missing_after_auth",
            "inputs": {"plot": "雨夜"},
        },
        _state={
            "_workflow_spec_authorized_refs": [
                {"template_id": "missing_after_auth", "authorized_by": "workflow_spec"}
            ]
        },
    )

    assert result.get("error_kind") != "workflow_ref_requires_workflow_spec"


@pytest.mark.asyncio
async def test_tool_execute_runs_registered_deferred_tool_with_filtered_kwargs() -> None:
    async def fake_tool(project_id: str, value: str) -> dict:
        return {"ok": True, "project_id": project_id, "value": value}

    registry.register(
        "tmp.deferred_echo",
        fake_tool,
        description="Temporary deferred echo tool",
    )
    try:
        result = await tool_meta_tools.tool_execute(
            project_id="project-1",
            name="tmp.deferred_echo",
            input={"value": "hello", "ignored": "drop"},
        )
    finally:
        registry.unregister("tmp.deferred_echo")

    assert result["_deferred_tool"] == "tmp.deferred_echo"
    assert result["_deferred_permission"]["allowed"] is True
    assert result["ok"] is True
    assert result["project_id"] == "project-1"
    assert result["value"] == "hello"


@pytest.mark.asyncio
async def test_tool_execute_resolves_file_read_alias_to_read_text() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="file.read",
        input={"rel_path": "missing.txt"},
    )

    assert result["_deferred_tool"] == "file.read_text"
    assert result["_deferred_alias"] == {"requested": "file.read", "resolved": "file.read_text"}
    assert result["error_kind"] != "unknown_deferred_tool"


@pytest.mark.asyncio
async def test_tool_search_does_not_return_core_project_reset() -> None:
    result = await tool_meta_tools.tool_search(query="reset project", category="delete")
    names = {item["name"] for item in result["tools"]}

    assert "project.reset" not in names
    assert "canvas.delete" not in names
