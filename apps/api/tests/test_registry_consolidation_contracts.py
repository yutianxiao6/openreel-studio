from agent_plan_contract_helpers import *  # noqa: F401,F403

from app.mcp_tools import skill_tools

@pytest.mark.asyncio
async def test_tool_search_finds_deferred_project_create_tool() -> None:
    result = await tool_meta_tools.tool_search(query="new blank project", category="project")
    names = {item["name"] for item in result["tools"]}

    assert "project.create" in names

    described = await tool_meta_tools.tool_describe(["project.create"])
    assert described["not_found"] == []
    assert described["tools"][0]["tier"] == 2

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
    assert "skill.video_production" not in names
    assert "skill.search" not in names
    assert "node.create" not in names
    assert "assets.set_library_path" not in names
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
    assert "assets.set_library_path" not in names


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
    result = await tool_meta_tools.tool_search(query="select:project.create,system.models")
    names = {item["name"] for item in result["tools"]}

    assert result["mode"] == "select"
    assert result["not_found"] == []
    assert names == {"project.create", "system.models"}


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

@pytest.mark.asyncio
async def test_tool_search_discover_returns_schema_summary_and_example() -> None:
    result = await tool_meta_tools.tool_search(query="discover:视频制作 skill", limit=5)
    item = next(tool for tool in result["tools"] if tool["name"] == "skill.project_mentor")

    assert result["mode"] == "discover"
    assert item["category"] == "guide"
    assert item["input_schema_summary"]["type"] == "object"
    assert isinstance(item["input_schema_summary"]["properties"], list)
    assert item["example"]

@pytest.mark.asyncio
async def test_tool_search_uses_usage_hints_for_guide_tools() -> None:
    result = await tool_meta_tools.tool_search(query="提示词写法 guide", category="guide")
    item = next(tool for tool in result["tools"] if tool["name"] == "skill.project_mentor")

    assert item["usage_hints"]

    described = await tool_meta_tools.tool_describe(["skill.project_mentor"])
    assert described["not_found"] == []
    assert described["tools"][0]["category"] == "guide"
    assert described["tools"][0]["usage_hints"]
    assert described["tools"][0]["example"]

@pytest.mark.asyncio
async def test_tool_search_finds_video_blueprint_guides_for_chinese_workflow_queries() -> None:
    default_flow = await tool_meta_tools.tool_search(query="通用制作流程", category="guide")
    default_names = {item["name"] for item in default_flow["tools"]}

    story_template = await tool_meta_tools.tool_search(query="故事模板 图生视频 skill", category="guide")
    story_template_names = {item["name"] for item in story_template["tools"]}

    explicit_file = await tool_meta_tools.tool_search(
        query="file.read_text apps/api/app/skills/story_template_method/SKILL.md",
        category="file",
    )
    file_names = {item["name"] for item in explicit_file["tools"]}

    assert "skill.project_mentor" in default_names
    assert {"skill.project_mentor", "skill.story_template_method"} <= story_template_names
    assert "file.read_text" in file_names

@pytest.mark.asyncio
async def test_project_mentor_exposes_node_repair_and_audit_guides() -> None:
    repair = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "node_repair_guide"},
    )
    audit = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "production_audit_guide"},
    )

    assert repair["topic"] == "node_repair_guide"
    assert "Repair the original node first" in repair["guidance"]
    assert repair["references_count"] > 0
    assert "file.read_text" in repair["reference_policy"]
    assert "references" not in repair
    assert audit["topic"] == "production_audit_guide"
    assert "Before declaring work done" in audit["guidance"]
    assert audit["references_count"] > 0

@pytest.mark.asyncio
async def test_project_mentor_does_not_register_prompt_template_topics() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "prompt_template_video_index"},
    )

    assert result["topic"] == "overview"
    assert "prompt_template_video_index" not in result["available_topics"]


@pytest.mark.asyncio
async def test_template_tools_and_directory_are_not_user_facing() -> None:
    assert not Path("app/prompts/template_library").exists()
    for name in [
        "template.list_categories",
        "template.list",
        "template.get",
        "template.add",
        "template.update",
    ]:
        assert registry.get(name) is None
        assert registry.tool_exposure(name) == "unregistered"

    search = await tool_meta_tools.tool_search(query="视频提示词模板", category="template")
    assert search["tools"] == []


@pytest.mark.asyncio
async def test_project_mentor_skill_topic_docs_match_registered_topics() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    skill_doc = (repo_root / "apps/api/app/skills/project_mentor/SKILL.md").read_text(encoding="utf-8")
    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "overview"},
    )
    registered_topics = set(result["available_topics"])
    topics_section = skill_doc.split("## Topics", 1)[1].split("## Current Rules", 1)[0]
    documented_topics = {
        line.split("`", 2)[1]
        for line in topics_section.splitlines()
        if line.startswith("- `") and "`" in line
    }

    assert documented_topics <= registered_topics
    assert {"tool_surface", "harness_design", "token_monitoring"} not in documented_topics
    assert "node-first" in skill_doc
    assert "one visible canvas" in skill_doc
    assert "template.list_categories -> template.list -> template.get" not in skill_doc
    legacy_template_guides = repo_root / "apps/api/app/skills/project_mentor/guides/prompt_templates"
    assert not list(legacy_template_guides.glob("*.md"))


def test_project_mentor_docs_do_not_point_agents_to_removed_template_paths() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    skill_doc = (repo_root / "apps/api/app/skills/project_mentor/SKILL.md").read_text(encoding="utf-8")

    assert "prompt_template_video_index" not in skill_doc
    assert "prompt_template_t2v" not in skill_doc
    assert "template.list_categories -> template.list -> template.get" not in skill_doc
    assert "blueprint.start_tree_draft -> blueprint.append_tree_node" not in skill_doc
    assert "node-first" in skill_doc

@pytest.mark.asyncio
async def test_project_mentor_exposes_repair_and_audit_guides() -> None:
    repair = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "node_repair_guide"},
    )
    audit = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "production_audit_guide"},
    )

    assert repair["topic"] == "node_repair_guide"
    assert "Repair the original node first" in repair["guidance"]
    assert repair["references_count"] > 0
    assert "源码参考仅用于诊断计数" in repair["reference_policy"]
    assert audit["topic"] == "production_audit_guide"
    assert "Before declaring work done" in audit["guidance"]
    assert "node statuses" in audit["guidance"]
    assert audit["references_count"] > 0

@pytest.mark.asyncio
async def test_project_mentor_video_workflow_keeps_moved_prompt_details() -> None:
    workflow = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow"},
    )

    assert workflow["topic"] == "video_workflow"
    assert "interaction.request_input" in workflow["guidance"]
    assert "节点优先流程" in workflow["guidance"]
    assert "skill.search 查内置和用户 workflow" in workflow["guidance"]
    assert "15秒短视频通常不问分集分段" in workflow["guidance"]
    assert "text/image/video/audio" in workflow["guidance"]
    assert "skill.get 读取内置 `video_production` markdown skill" in workflow["guidance"]
    assert "独立 prompt skill" in workflow["guidance"]
    assert "node.run" in workflow["guidance"]
    assert "自动连线" in workflow["guidance"]
    assert "canvas.connect_nodes" not in workflow["guidance"]
    assert "start_tree_draft" not in workflow["guidance"]
    assert "blueprint_tree_guide" not in workflow["guidance"]
    assert "final mode" not in workflow["guidance"]
    assert workflow["references_count"] > 0


@pytest.mark.asyncio
async def test_project_mentor_video_workflow_full_is_mode_index_not_step_dump() -> None:
    workflow = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow", "detail": "full"},
    )
    guide = workflow["guide_content"]

    assert "## 默认骨架" in guide
    assert "## 优先级" in guide
    assert "节点优先流程" in guide
    assert "用户自定义 workflow" in guide
    assert "不是灵感参考" in guide
    assert "详细剧本 text -> 主要人物图 image -> 分集/分段故事 text" in guide
    assert "剧本、分集和分段 text 只写故事情节" in guide
    assert "15秒及以内通常单段" in guide
    assert "1集不建分集" in guide
    assert "1段不建分段" in guide
    assert "每段写段落故事" in guide
    assert "parent_node_id" in guide
    assert "人物设定集+3视图" in guide
    assert "无人物场景四宫格四视图" in guide
    assert "分镜图或故事模板图" in guide
    assert "独立 prompt skill" in guide
    assert "可复用 workflow 把这些写法放进每个逻辑步骤的 `prompt`" in guide
    assert "standalone worker 只读取当前模块需要的一份" in guide
    assert "每个节点是独立任务单元" in guide

    summary = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow", "detail": "summary"},
    )
    assert "video_production" in summary["guidance"]
    assert "skill.get" in summary["guidance"]
    assert "只补问阻塞事实" in summary["guidance"]
    assert "写入剧本/规划 text 节点" in summary["guidance"]
    assert "独立 prompt skill" in summary["guidance"]
    assert "node.run" in summary["guidance"]


@pytest.mark.asyncio
async def test_video_production_skill_guides_reference_driven_short_video_nodes() -> None:
    assert registry.get("skill.video_production") is None

    found = await skill_tools.skill_search(
        query="视频制作 默认视频流程 短剧",
        category="workflow",
        scope="builtin",
    )
    assert found["ok"] is True
    assert any(item["name"] == "video_production" for item in found["skills"])

    summary = await skill_tools.skill_get_skill(
        "video_production",
        category="workflow",
        scope="builtin",
    )
    assert summary["ok"] is True
    assert summary["detail"] == "summary"
    assert summary["content_available"] is True
    assert summary["workflow_template_match_hint"]["skill_name"] == "video_production"

    full = await skill_tools.skill_get_skill(
        "video_production",
        category="workflow",
        scope="builtin",
        detail="full",
    )
    guide = full["content"]

    assert full["ok"] is True
    assert full["detail"] == "full"
    assert full["source"] == "python_package"
    assert full["source_root"] == "builtin_default"
    assert "skill.video_production" not in guide
    assert "视频制作入口指南" in guide
    assert "skill.search" in guide
    assert "skill.get" in guide
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
    assert "默认模板" in guide
    assert "剧情/主题 `plot`" in guide
    assert "durationSeconds" in guide
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
    assert "duration_seconds" in guide
    assert "role:\"visual_reference\"" in guide
    assert "role:\"source_image\"" in guide
    assert "`path` 只做诊断来源" in guide


@pytest.mark.asyncio
async def test_video_production_hands_off_explicit_story_template_requests() -> None:
    found = await skill_tools.skill_search(
        query="故事模板图 视频",
        category="workflow",
        scope="builtin",
    )
    assert found["ok"] is True
    assert any(item["name"] == "video_production" for item in found["skills"])

    result = await skill_tools.skill_get_skill(
        "video_production",
        category="workflow",
        scope="builtin",
        detail="full",
    )

    assert "story_template_method" in result["content"]
    assert "故事模板图/视觉开发板" in result["content"]
    assert "related_skill" not in result


def test_video_production_skill_uses_markdown_as_single_source() -> None:
    module_source = Path("app/skills/video_production/__init__.py").read_text(encoding="utf-8")

    assert "register(" not in module_source
    assert "skill.video_production" not in module_source
    assert "skill.search / skill.get" in module_source
    assert "_FULL_GUIDE" not in module_source
    assert "_MODEL_SUMMARY" not in module_source
    assert "## 核心流程" not in module_source


@pytest.mark.asyncio
async def test_project_mentor_t2v_workflow_requires_no_images() -> None:
    workflow = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow_t2v", "detail": "full"},
    )
    guide = workflow["guide_content"]

    assert workflow["topic"] == "video_workflow_t2v"
    assert "文生视频不生成参考图片" in guide
    assert "需要复用的提示词写法写到 skill" in guide
    assert "不要因为项目是视频就创建人物图、场景图、分镜图、首尾帧或故事模板图" in guide


@pytest.mark.asyncio
async def test_project_mentor_storyboard_workflow_waits_for_storyboard_image() -> None:
    workflow = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow_storyboard", "detail": "full"},
    )
    guide = workflow["guide_content"]

    assert workflow["topic"] == "video_workflow_storyboard"
    assert "一段默认一张宫格分镜图" in guide
    assert "直接写人物参考图、场景参考图和分镜图的 image prompt" in guide
    assert "需要复用的提示词写法写到 skill" in guide
    assert "运行分镜图 `image` 节点" in guide
    assert "读取已完成的分镜图输出" in guide
    assert "最终视频提示词必须等分镜图完成、看图或读取视觉分析后再写" in guide
    assert "当前模型看不了图时明确说明看不了" in guide
    assert "剧情必须按段落因果连续推进" in guide
    assert "180度轴线" in guide
    assert "关键帧选动作起势、转折、高潮、结果/钩子" in guide


@pytest.mark.asyncio
async def test_project_mentor_shot_image_and_story_template_modes_are_separate() -> None:
    shot = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow_shot_images", "detail": "full"},
    )
    template = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow_story_template", "detail": "full"},
    )

    assert shot["topic"] == "video_workflow_shot_images"
    assert "每个关键镜头/关键帧单独生成一张高质量图片" in shot["guide_content"]
    assert "只选影响剧情理解和视频运动的节点" in shot["guide_content"]
    assert "保持场景轴线、人物左右关系、视线方向、运动方向" in shot["guide_content"]
    assert "视频提示词等参考图完成、看图或读取视觉分析后再写" in shot["guide_content"]
    assert "需要复用的提示词写法写到 skill" in shot["guide_content"]
    assert template["topic"] == "video_workflow_story_template"
    assert "先读 `skill.story_template_method`" in template["guide_content"]
    assert "需要复用的提示词写法写到 skill" in template["guide_content"]
    assert "最终视频提示词必须等故事模板图完成、看图或读取视觉分析后再写" in template["guide_content"]


@pytest.mark.asyncio
async def test_story_template_method_is_separate_optional_guide() -> None:
    search = await tool_meta_tools.tool_search(query="故事模板 复杂动作 视觉开发板", category="guide")
    names = {item["name"] for item in search["tools"]}
    assert "skill.story_template_method" in names

    summary = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.story_template_method",
        input={"detail": "summary"},
    )
    assert summary["topic"] == "story_template_method"
    assert summary["not_default_fallback"] is True
    assert summary["node_pattern"] == [
        {"type": "image", "purpose": "story_template_board"},
        {"type": "video", "purpose": "video_from_story_template_board"},
    ]

    full = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.story_template_method",
        input={"detail": "full"},
    )
    assert "image" in full["guide_content"]
    assert "video" in full["guide_content"]

@pytest.mark.asyncio
async def test_tool_search_finds_revision_and_audit_guide_hints() -> None:
    revision = await tool_meta_tools.tool_search(query="蓝图修订 source path", category="guide")
    audit = await tool_meta_tools.tool_search(query="制作审查 prompt_source skill", category="guide")

    assert any(item["name"] == "skill.project_mentor" for item in revision["tools"])
    assert any(item["name"] == "skill.project_mentor" for item in audit["tools"])

@pytest.mark.asyncio
async def test_tool_search_finds_repair_and_plan_guide_hints() -> None:
    repair = await tool_meta_tools.tool_search(query="失败节点 原地修复 dependency_missing", category="guide")
    plan = await tool_meta_tools.tool_search(query="蓝图执行计划 pending_video_blueprint_request", category="guide")

    assert any(item["name"] == "skill.project_mentor" for item in repair["tools"])
    assert any(item["name"] == "skill.project_mentor" for item in plan["tools"])

@pytest.mark.asyncio
async def test_registered_internal_raw_runner_set_is_empty() -> None:
    assert INTERNAL_RAW_RUNNER_TOOL_NAMES == ()

@pytest.mark.asyncio
async def test_drama_raw_runners_are_unregistered_after_registry_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES:
        assert registry.get(name) is None, name

    search = await tool_meta_tools.tool_search(query="generate_outline")
    assert all(item["name"] != "drama.generate_outline" for item in search["tools"])
    search = await tool_meta_tools.tool_search(query="generate_image")
    assert all(item["name"] != "media.generate_image" for item in search["tools"])

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES)

    for name in UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_media_raw_runners_are_unregistered_after_service_extraction() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES)

    for name in UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_media_status_wrapper_is_unregistered_after_node_state_extraction() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_MEDIA_STATUS_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_MEDIA_STATUS_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_MEDIA_STATUS_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_MEDIA_STATUS_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_MEDIA_STATUS_TOOL_NAMES)

    for name in UNREGISTERED_MEDIA_STATUS_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    control = await tool_meta_tools.tool_search(query="cancel image", category="control")
    assert "media.cancel_image_generation" in {item["name"] for item in control["tools"]}
    query = await tool_meta_tools.tool_search(query="describe image", category="query")
    assert "media.describe_image" not in {item["name"] for item in query["tools"]}

@pytest.mark.asyncio
async def test_model_config_wrappers_are_unregistered_and_system_models_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert "system.models" not in visible
    assert not set(UNREGISTERED_MODEL_CONFIG_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_MODEL_CONFIG_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_MODEL_CONFIG_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_MODEL_CONFIG_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_MODEL_CONFIG_TOOL_NAMES)

    for name in UNREGISTERED_MODEL_CONFIG_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    system_tools = await tool_meta_tools.tool_search(query="models", category="system")
    assert "system.models" in {item["name"] for item in system_tools["tools"]}

@pytest.mark.asyncio
async def test_mcp_meta_tools_are_unregistered_and_rest_status_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_MCP_META_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_MCP_META_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_MCP_META_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_MCP_META_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_MCP_META_TOOL_NAMES)

    for name in UNREGISTERED_MCP_META_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    servers = await routes_tools.list_mcp_servers()
    assert set(servers) == {"servers", "total"}
    assert isinstance(servers["servers"], list)
    assert servers["total"] == len(servers["servers"])

@pytest.mark.asyncio
async def test_config_write_tools_are_unregistered_and_rest_control_plane_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_CONFIG_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_CONFIG_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_CONFIG_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_CONFIG_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_CONFIG_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_CONFIG_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    for name in ("config.read", "config.read_file", "config.validate"):
        assert registry.get(name) is not None, name

    validate = await routes_tools.validate_config_text(routes_tools.ConfigTextRequest(content="{}"))
    assert set(validate) == {"ok", "errors"}

@pytest.mark.asyncio
async def test_drama_segment_wrappers_are_unregistered_after_service_extraction() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES)

    for name in UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_canvas_crud_wrappers_are_unregistered_after_node_convergence() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_CANVAS_CRUD_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_CANVAS_CRUD_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_CANVAS_CRUD_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_CANVAS_CRUD_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_CANVAS_CRUD_TOOL_NAMES)

    for name in UNREGISTERED_CANVAS_CRUD_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_blueprint_write_wrappers_are_unregistered_after_state_machine_internalization() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_deprecated_alias_tools_are_unregistered() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES)

    for name in UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_task_helper_tools_are_unregistered_after_task_list_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_TASK_HELPER_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_TASK_HELPER_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_TASK_HELPER_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_TASK_HELPER_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_TASK_HELPER_TOOL_NAMES)

    for name in UNREGISTERED_TASK_HELPER_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_task_write_tools_are_unregistered_after_plan_materialization() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_TASK_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_TASK_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_TASK_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_TASK_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_TASK_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_TASK_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_project_low_level_tools_are_unregistered_after_rest_and_blueprint_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES)

    for name in UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_memory_low_level_tools_are_unregistered_after_orchestrator_internalization() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES)

    for name in UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_file_write_tools_are_unregistered_after_readonly_file_boundary() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_FILE_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_FILE_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_FILE_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_FILE_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_FILE_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_FILE_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"


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


@pytest.mark.asyncio
async def test_guide_and_file_tools_have_distinct_discovery_boundaries() -> None:
    guide_tools = await tool_meta_tools.tool_search(query="guide", category="guide")
    file_tools = await tool_meta_tools.tool_search(query="file", category="file")

    guide_names = {item["name"] for item in guide_tools["tools"]}
    file_names = {item["name"] for item in file_tools["tools"]}

    assert "skill.project_mentor" in guide_names
    assert "skill.project_mentor" not in file_names
    assert {"file.read_text", "file.list_dir"} <= file_names
    assert "file.read_text" not in guide_names

    described = await tool_meta_tools.tool_describe(["skill.project_mentor", "file.read_text"])
    assert described["not_found"] == []
    descriptions = {tool["name"]: tool for tool in described["tools"]}
    assert descriptions["skill.project_mentor"]["category"] == "guide"
    assert descriptions["file.read_text"]["category"] == "file"

@pytest.mark.asyncio
async def test_domain_business_skills_are_unregistered_but_project_mentor_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert registry.get("skill.project_mentor") is not None
    assert not set(UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES)

    for name in UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"


@pytest.mark.asyncio
async def test_plan_control_tools_are_unregistered_after_deterministic_handlers() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_PLAN_CONTROL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_PLAN_CONTROL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_PLAN_CONTROL_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_PLAN_CONTROL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_PLAN_CONTROL_TOOL_NAMES)

    for name in UNREGISTERED_PLAN_CONTROL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_agent_low_level_tools_are_unregistered_but_high_level_collab_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES)

    for name in UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    assert registry.tool_exposure("agent.review") == "core"
    assert "agent.review" in _visible_tools(None)

    collab = await tool_meta_tools.tool_search(query="agent", category="collab")
    collab_names = {item["name"] for item in collab["tools"]}
    assert "agent.review" not in collab_names
    assert {"agent.run", "agent.map_reduce", "agent.pipeline", "agent.hierarchical"} <= collab_names

    high_level = await tool_meta_tools.tool_describe(
        ["agent.map_reduce", "agent.pipeline", "agent.hierarchical"]
    )
    assert {tool["name"] for tool in high_level["tools"]} == {
        "agent.map_reduce",
        "agent.pipeline",
        "agent.hierarchical",
    }
    assert high_level["not_found"] == []

@pytest.mark.asyncio
async def test_team_protocol_tools_are_unregistered_after_collab_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_TEAM_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_TEAM_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_TEAM_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_TEAM_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_TEAM_TOOL_NAMES)

    for name in UNREGISTERED_TEAM_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    collab = await tool_meta_tools.tool_search(query="agent", category="collab")
    collab_names = {item["name"] for item in collab["tools"]}
    assert {"agent.run", "agent.map_reduce", "agent.pipeline", "agent.hierarchical"} <= collab_names
    assert not set(UNREGISTERED_TEAM_TOOL_NAMES) & collab_names

@pytest.mark.asyncio
async def test_legacy_drama_delete_wrappers_are_unregistered_and_canvas_delete_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert "canvas.delete" in visible
    assert registry.get("canvas.delete") is not None
    assert registry.get("node.delete") is None
    assert registry.get("canvas.clear_all") is None
    assert not set(UNREGISTERED_DRAMA_DELETE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_DRAMA_DELETE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_DRAMA_DELETE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_DRAMA_DELETE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_DRAMA_DELETE_TOOL_NAMES)

    for name in UNREGISTERED_DRAMA_DELETE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    delete_tools = await tool_meta_tools.tool_search(query="reset clear", category="delete")
    delete_names = {item["name"] for item in delete_tools["tools"]}
    assert delete_names == set()
    assert not set(UNREGISTERED_DRAMA_DELETE_TOOL_NAMES) & delete_names

@pytest.mark.asyncio
async def test_node_helper_tools_are_unregistered_after_node_protocol_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_NODE_HELPER_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_NODE_HELPER_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_NODE_HELPER_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_NODE_HELPER_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_NODE_HELPER_TOOL_NAMES)

    for name in UNREGISTERED_NODE_HELPER_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_session_focus_tools_are_unregistered_after_runtime_context_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_SESSION_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_SESSION_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_SESSION_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_SESSION_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_SESSION_TOOL_NAMES)

    for name in UNREGISTERED_SESSION_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_panel_layout_tools_are_unregistered_after_rest_api_migration() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_PANEL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_PANEL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_PANEL_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_PANEL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_PANEL_TOOL_NAMES)

    for name in UNREGISTERED_PANEL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

def test_panel_layout_keeps_episode_scene_assets_with_preview() -> None:
    grid = panel_layout.bucket_nodes([
        {
            "id": "scene-1",
            "type": "scene",
            "title": "场景：九天仙台",
            "status": "completed",
            "version": 1,
            "input_json": json.dumps(
                {
                    "episode_number": 1,
                    "blueprint_id": "bp-1",
                    "prompt": "场景概念图，九天仙台。",
                },
                ensure_ascii=False,
            ),
            "output_json": json.dumps(
                {
                    "type": "fusion",
                    "subject": "scene",
                    "stages": [
                        {
                            "name": "场景图",
                            "status": "completed",
                            "local_url": "/api/media/p/scene.png",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            "preview": {
                "type": "fusion",
                "subject": "scene",
                "stages": [
                    {
                        "name": "场景图",
                        "status": "completed",
                        "local_url": "/api/media/p/scene.png",
                    }
                ],
            },
            "prompt": "场景概念图，九天仙台。",
            "created_at": "2026-06-05T00:00:00",
        }
    ])

    episode = grid["episodes"]["1"]
    assert len(episode["scenes"]) == 1
    assert episode["scenes"][0]["id"] == "scene-1"
    assert episode["scenes"][0]["preview"]["stages"][0]["name"] == "场景图"
    assert grid["unbucketed"] == []

@pytest.mark.asyncio
async def test_scene_shot_asset_write_tools_are_unregistered_after_node_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_scene_shot_asset_read_tools_are_hidden_after_node_and_assets_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES) & visible
    assert not set(AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES) & listed_names
    for name in AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES:
        spec = registry.get(name)
        assert spec is not None, name
        assert tool_meta_tools._tier_of(spec) == 3, name

    described = await tool_meta_tools.tool_describe(list(AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == {
        f"{name} (hidden)" for name in AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES
    }

@pytest.mark.asyncio
async def test_asset_library_path_config_is_unregistered_but_library_tools_are_deferred() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_ASSET_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_ASSET_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_ASSET_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_ASSET_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_ASSET_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_ASSET_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    for name in (
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
    ):
        assert registry.get(name) is not None, name
        assert name not in visible
        assert name in listed_names

    save_search = await tool_meta_tools.tool_search(query="保存到资产库", category="assets")
    save_names = {item["name"] for item in save_search["tools"]}
    assert "assets.save_to_project" in save_names
    assert "assets.save_to_shared" in save_names

    category_search = await tool_meta_tools.tool_search(query="创建分类", category="assets")
    category_names = {item["name"] for item in category_search["tools"]}
    assert "assets.create_category" in category_names

    move_search = await tool_meta_tools.tool_search(query="移动资产", category="assets")
    move_names = {item["name"] for item in move_search["tools"]}
    assert "assets.move_asset" in move_names

    canvas_search = await tool_meta_tools.tool_search(query="加入画布", category="assets")
    canvas_names = {item["name"] for item in canvas_search["tools"]}
    assert "assets.add_to_canvas" in canvas_names

@pytest.mark.asyncio
async def test_media_provider_write_tools_are_unregistered_but_provider_test_remains() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES)

    for name in UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

    assert registry.get("media.test_provider") is not None
    for name in AGENT_HIDDEN_MEDIA_PROVIDER_READ_TOOL_NAMES:
        spec = registry.get(name)
        assert spec is not None, name
        assert tool_meta_tools._tier_of(spec) == 3, name
        assert name not in listed_names

@pytest.mark.asyncio
async def test_prompt_management_tools_are_unregistered_after_prompt_contract_consolidation() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_PROMPT_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_PROMPT_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_PROMPT_TOOL_NAMES:
        assert registry.get(name) is None, name

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_PROMPT_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_PROMPT_TOOL_NAMES)

    for name in UNREGISTERED_PROMPT_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_generic_skill_management_tools_are_unregistered_but_concrete_skills_remain() -> None:
    visible = _visible_tools(None)
    listed = await tool_meta_tools.tool_search(query="", limit=0)
    listed_names = {item["name"] for item in listed["tools"]}

    assert not set(UNREGISTERED_GENERIC_SKILL_TOOL_NAMES) & visible
    assert not set(UNREGISTERED_GENERIC_SKILL_TOOL_NAMES) & listed_names
    for name in UNREGISTERED_GENERIC_SKILL_TOOL_NAMES:
        assert registry.get(name) is None, name
    assert "skill.search" in visible
    assert "skill.get" in visible
    assert registry.tool_exposure("skill.search") == "core"
    assert registry.tool_exposure("skill.get") == "core"
    assert registry.get("skill.project_mentor") is not None

    described = await tool_meta_tools.tool_describe(list(UNREGISTERED_GENERIC_SKILL_TOOL_NAMES))
    assert described["tools"] == []
    assert set(described["not_found"]) == set(UNREGISTERED_GENERIC_SKILL_TOOL_NAMES)

    for name in UNREGISTERED_GENERIC_SKILL_TOOL_NAMES:
        result = await tool_meta_tools.tool_execute(
            project_id="test",
            name=name,
            input={},
        )
        assert result["error_kind"] == "unknown_deferred_tool"

@pytest.mark.asyncio
async def test_media_control_tool_remains_deferred_and_legacy_image_describe_is_removed() -> None:
    result = await tool_meta_tools.tool_search(query="describe image", category="query")
    names = {item["name"] for item in result["tools"]}
    assert "media.describe_image" not in names
    assert "reference.manage" not in names

    described = await tool_meta_tools.tool_describe(["media.describe_image", "reference.manage"])
    assert described["tools"] == []
    assert set(described["not_found"]) == {"media.describe_image", "reference.manage"}
    for name in ("media.describe_image", "reference.manage"):
        executed = await tool_meta_tools.tool_execute(project_id="test", name=name, input={})
        assert executed["error_kind"] == "unknown_deferred_tool"

    result = await tool_meta_tools.tool_search(query="cancel image", category="control")
    names = {item["name"] for item in result["tools"]}
    assert "media.cancel_image_generation" in names

@pytest.mark.asyncio
async def test_system_tools_are_deferred_and_template_tools_are_removed() -> None:
    result = await tool_meta_tools.tool_search(query="system status", category="system")
    names = {item["name"] for item in result["tools"]}
    assert "system.status" in names
    assert "system.models" in names

    result = await tool_meta_tools.tool_search(query="template get", category="template")
    assert result["tools"] == []

    described = await tool_meta_tools.tool_describe(["template.add", "template.update"])
    assert described["tools"] == []
    assert set(described["not_found"]) == {"template.add", "template.update"}

@pytest.mark.asyncio
async def test_attachment_ingest_tool_is_deferred_and_discoverable() -> None:
    result = await tool_meta_tools.tool_search(query="parse uploaded script", category="attach")
    names = {item["name"] for item in result["tools"]}

    assert "drama.parse_uploaded_script" in names

    described = await tool_meta_tools.tool_describe(["drama.parse_uploaded_script"])
    assert described["not_found"] == []
    assert described["tools"][0]["tier"] == 2

def test_main_loop_does_not_auto_create_canvas_for_unregistered_raw_runners() -> None:
    assert orchestrator_module._NODE_PRODUCING_TOOLS == {"drama.parse_uploaded_script"}
    assert not (
        set(UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES)
        & set(orchestrator_module._NODE_PRODUCING_TOOLS)
    )
    assert "media.generate_image" not in orchestrator_module._NODE_PRODUCING_TOOLS


def test_main_loop_only_node_run_takes_over_node_lifecycle() -> None:
    assert orchestrator_module._NODE_TARGET_TOOLS == {"node.run"}

def test_node_universal_uses_media_generation_service_for_media_runners() -> None:
    assert node_universal.media_generation is media_generation
    assert not hasattr(node_universal, "media_tools")

def test_node_universal_removed_drama_legacy_segment_fallbacks() -> None:
    assert not hasattr(node_universal, "drama_legacy")

@pytest.mark.asyncio
async def test_media_raw_tool_wrapper_delegates_to_media_generation_service(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    async def fake_generate_image(**kwargs):
        calls.update(kwargs)
        return {"ok": True, "asset_id": "asset-1"}

    monkeypatch.setattr(media_generation, "generate_image", fake_generate_image)

    result = await media_tools.generate_image(
        project_id="project-1",
        prompt="test prompt",
        aspect_ratio="16:9",
        n=2,
    )

    assert result == {"ok": True, "asset_id": "asset-1"}
    assert calls["project_id"] == "project-1"
    assert calls["prompt"] == "test prompt"
    assert calls["aspect_ratio"] == "16:9"
    assert calls["n"] == 2

@pytest.mark.asyncio
async def test_story_template_generation_defaults_to_4k_size(monkeypatch) -> None:
    calls: dict[str, Any] = {}
    assets: list[dict[str, Any]] = []

    async def fake_generate_image_with_provider(**kwargs):
        calls.update(kwargs)
        return {
            "ok": True,
            "provider": "fake-provider",
            "model": "fake-image",
            "images": [{"url": "https://example.test/story-template.png"}],
            "size_requested": kwargs["size"],
            "size_final": kwargs["size"],
            "attempts": [],
        }

    async def fake_register_asset(**kwargs):
        assets.append(kwargs)
        return {"id": "asset-story-template"}

    monkeypatch.setattr(media_generation, "generate_image_with_provider", fake_generate_image_with_provider)
    monkeypatch.setattr(media_generation, "register_asset", fake_register_asset)

    result = await media_generation.generate_story_template(
        project_id="project-1",
        segment_id="seg-1",
        prompt="4K故事模板图",
        aspect_ratio="16:9",
    )

    assert calls["size"] == "3840x2160"
    assert result["ok"] is True
    assert result["size_final"] == "3840x2160"
    assert assets == []
    assert result["asset_id"] is None
    assert result["asset_ids"] == []

def test_image_provider_does_not_auto_downgrade_resolution() -> None:
    assert media_provider._downgrade_size("3840x2160") is None
    assert media_provider._downgrade_size("2560x1440") is None

@pytest.mark.asyncio
async def test_drama_segment_tool_wrapper_delegates_to_drama_legacy_service(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    async def fake_update_segment(**kwargs):
        calls.update(kwargs)
        return {"ok": True, "segment": {"index": kwargs["segment_index"]}}

    monkeypatch.setattr(drama_legacy, "update_segment", fake_update_segment)

    result = await drama_tools.update_segment(
        project_id="project-1",
        episode_number=1,
        segment_index=2,
        plot="雨夜桥头反击",
    )

    assert result == {"ok": True, "segment": {"index": 2}}
    assert calls == {
        "project_id": "project-1",
        "episode_number": 1,
        "segment_index": 2,
        "plot": "雨夜桥头反击",
        "characters": None,
        "scene_refs": None,
        "duration_seconds": None,
        "segment_arc": None,
    }

@pytest.mark.asyncio
async def test_reference_character_skill_calls_internal_runner_without_registry(monkeypatch) -> None:
    from app.skills.character_with_reference import character_with_reference

    calls: dict[str, Any] = {}

    async def fake_generate_character(**kwargs):
        calls.update(kwargs)
        return {"character": {"name": kwargs["name"], "appearance": "白衬衫"}}

    monkeypatch.setattr(drama_tools, "generate_character", fake_generate_character)

    result = await character_with_reference(
        project_id="project-1",
        reference_description="白衬衫、黑色长发、清晨地铁站",
        role_type="female_lead",
        name="林夏",
        node_id="node-1",
    )

    assert result["character"]["name"] == "林夏"
    assert calls["project_id"] == "project-1"
    assert calls["role_type"] == "female_lead"
    assert calls["node_id"] == "node-1"
    assert any("白衬衫" in item for item in calls["requirements"])

@pytest.mark.asyncio
async def test_hook_punch_review_skill_calls_internal_runner_without_registry(monkeypatch) -> None:
    from app.skills.hook_punch_review import hook_punch_review

    calls: dict[str, Any] = {}

    async def fake_review_script(**kwargs):
        calls.update(kwargs)
        return {
            "review": {
                "hook": "开场够强",
                "score": 8,
                "issues": ["钩子可以更快", "人物关系略弱"],
            }
        }

    monkeypatch.setattr(drama_tools, "review_script", fake_review_script)

    result = await hook_punch_review(
        project_id="project-1",
        episode_number=2,
        node_id="node-2",
    )

    assert calls == {
        "project_id": "project-1",
        "episode_number": 2,
        "node_id": "node-2",
    }
    assert result["narrowed_review"]["hook"] == "开场够强"
    assert result["narrowed_review"]["issues"] == ["钩子可以更快"]

def test_main_loop_raw_fusion_compatibility_helpers_are_removed() -> None:
    assert not hasattr(orchestrator_module, "_FUSION_STAGES")
    assert not hasattr(orchestrator_module, "_fusion_context")
    assert not hasattr(orchestrator_module.AgentOrchestrator, "_rebuild_fusion_lookup")

def test_script_collection_runner_is_removed_from_node_surface() -> None:
    assert "script_collection" not in node_universal._RUNNERS
    assert not hasattr(node_universal, "_run_script_collection")

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
async def test_tool_execute_records_target_permission_denial() -> None:
    result = await tool_meta_tools.tool_execute(
        project_id="test",
        name="canvas.delete",
        input={"scope": "all"},
        _state={"pending_plan": {"id": "plan-1", "status": "pending"}},
        _user_message="清空画布",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "core_tool_should_be_called_directly"
    assert result["tool"] == "canvas.delete"

@pytest.mark.asyncio
async def test_tool_execute_does_not_require_semantic_intent_for_target_permission(monkeypatch) -> None:
    async def fake_call(name: str, **kwargs):
        assert name == "project.create"
        return {"ok": True, "title": kwargs.get("title")}

    monkeypatch.setattr(tool_meta_tools.registry, "call", fake_call)

    result = await tool_meta_tools.tool_execute(
        project_id="test",
        name="project.create",
        input={"title": "新项目"},
        _state={},
        _user_message="start over",
    )

    assert result["_deferred_tool"] == "project.create"
    assert result["_deferred_permission"]["allowed"] is True
    assert result["ok"] is True
    assert result["title"] == "新项目"

@pytest.mark.asyncio
async def test_tool_execute_allows_project_create_despite_stale_legacy_state(monkeypatch) -> None:
    async def fake_call(name: str, **kwargs):
        assert name == "project.create"
        assert kwargs == {"title": "未命名项目"}
        return {"id": "project-new", "title": kwargs["title"]}

    monkeypatch.setattr(tool_meta_tools.registry, "call", fake_call)

    result = await tool_meta_tools.tool_execute(
        project_id="project-old",
        name="project.create",
        input={"title": "未命名项目"},
        _state={
            "pending_plan": {"title": "旧方案"},
            "active_plan_checklist": [
                {"status": "pending", "title": "旧任务", "tool": "node.create"}
            ],
        },
        _user_message="Create a new blank project",
    )

    assert result["_deferred_tool"] == "project.create"
    assert result["_deferred_permission"]["allowed"] is True
    assert result["id"] == "project-new"
    assert result["title"] == "未命名项目"

@pytest.mark.asyncio
async def test_tool_execute_allows_system_read_despite_pending_plan(monkeypatch) -> None:
    async def fake_call(name: str, **kwargs):
        assert name == "system.status"
        assert kwargs == {}
        return {"ok": True, "tools_total": 42}

    monkeypatch.setattr(tool_meta_tools.registry, "call", fake_call)

    result = await tool_meta_tools.tool_execute(
        project_id="project-old",
        name="system.status",
        input={},
        _state={"pending_plan": {"title": "旧方案"}},
        _user_message="What tools and models are available?",
    )

    assert result["_deferred_tool"] == "system.status"
    assert result["_deferred_permission"]["allowed"] is True
    assert result["ok"] is True
    assert result["tools_total"] == 42

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
async def test_tool_execute_reuses_cached_project_mentor_guide() -> None:
    state = {
        "_mentor_guides_loaded": {
            "video_workflow": {
                "topic": "video_workflow",
                "detail": "full",
                "has_full_guide": True,
                "guidance_summary": "cached workflow guidance",
                "guidance_hash": "abc123",
            }
        }
    }

    result = await tool_meta_tools.tool_execute(
        project_id="project-1",
        name="skill.project_mentor",
        input={"topic": "video_workflow", "detail": "full"},
        _state=state,
    )

    assert result["_deferred_tool"] == "skill.project_mentor"
    assert result["from_guide_cache"] is True
    assert result["guidance"] == "cached workflow guidance"
    assert result["guidance_hash"] == "abc123"


def test_user_visible_text_hides_internal_tool_names() -> None:
    text = AgentOrchestrator._clean_progress_commentary(
        "先查 project.reset 的参数，再调用 node.delete(node_id='abc')。"
    )
    assert "project.reset" not in text
    assert "node.delete" not in text
    assert "node_id" not in text


@pytest.mark.asyncio
async def test_tool_search_does_not_return_core_project_reset() -> None:
    result = await tool_meta_tools.tool_search(query="reset project", category="delete")
    names = {item["name"] for item in result["tools"]}

    assert "project.reset" not in names
    assert "canvas.delete" not in names
