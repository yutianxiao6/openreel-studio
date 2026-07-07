import pytest

from app.mcp_tools import skill_tools


@pytest.mark.asyncio
async def test_markdown_skills_default_to_project_root_skills(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENREEL_SKILLS_DIR", raising=False)
    monkeypatch.setattr(skill_tools.settings, "PROJECT_ROOT", str(tmp_path))

    workflow_dir = tmp_path / "skills" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "custom_flow.md").write_text(
        "---\n"
        "description: 自定义流程\n"
        "applies_to: video\n"
        "---\n\n"
        "按用户指定的自定义流程执行。\n",
        encoding="utf-8",
    )

    category_prompt = await skill_tools.skill_search(query="custom_flow")
    assert category_prompt["needs_category"] is True
    assert category_prompt["skills"] == []
    assert "workflow" in category_prompt["available_categories"]

    found = await skill_tools.skill_search(query="custom_flow", category="workflow")
    assert any(item["name"] == "custom_flow" for item in found["skills"])
    custom_item = next(item for item in found["skills"] if item["name"] == "custom_flow")
    assert "可复用模板" in custom_item["usage"]
    assert "workflow_spec" in custom_item["usage"]

    user_only = await skill_tools.skill_search(query="custom_flow", category="workflow", scope="user")
    assert [item["scope"] for item in user_only["skills"]] == ["user"]

    builtin_only = await skill_tools.skill_search(query="视频制作 默认流程", category="workflow", scope="builtin")
    assert all(item["scope"] == "builtin" for item in builtin_only["skills"])
    assert any(item["name"] == "video_production" for item in builtin_only["skills"])

    loaded = await skill_tools.skill_get_skill("custom_flow", category="workflow")
    assert loaded["ok"] is True
    assert loaded["detail"] == "summary"
    assert "content" not in loaded
    assert loaded["workflow_template_match_hint"]["skill_name"] == "custom_flow"
    assert "内置和用户" in loaded["workflow_template_match_hint"]["hint"]

    multi = await skill_tools.skill_search(queries=["custom_flow"], category="workflow")
    assert "workflow_spec" in multi["hint"]
    assert "direct_template" in multi["hint"]
    assert "直接运行" not in multi["hint"]

    loaded_full = await skill_tools.skill_get_skill("custom_flow", category="workflow", detail="full")
    assert loaded_full["ok"] is True
    assert loaded_full["detail"] == "full"
    assert "自定义流程" in loaded_full["content"]

    loaded_with_scope = await skill_tools.skill_get_skill("custom_flow", category="workflow", scope="user")
    assert loaded_with_scope["ok"] is True
    assert loaded_with_scope["scope"] == "user"


@pytest.mark.asyncio
async def test_markdown_skills_can_use_explicit_skills_dir(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    skills_root = tmp_path / "install-root" / "skills"
    monkeypatch.setattr(skill_tools.settings, "PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    prompt_dir = skills_root / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "bright_prompt.md").write_text(
        "---\n"
        "description: 明亮提示词写法\n"
        "applies_to: image\n"
        "---\n\n"
        "画面明亮，主体清晰。\n",
        encoding="utf-8",
    )

    found = await skill_tools.skill_search(query="bright", category="prompt")
    assert any(item["name"] == "bright_prompt" for item in found["skills"])

    loaded = await skill_tools.skill_get_skill("bright_prompt", category="prompt")
    assert loaded["ok"] is True
    assert "画面明亮" in loaded["content"]


@pytest.mark.asyncio
async def test_missing_or_unusable_user_skill_dir_is_empty_index(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills-file"
    skills_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    user_only = await skill_tools.skill_search(query="写剧本", category="workflow", scope="user")
    assert user_only["ok"] is True
    assert user_only["skills"] == []
    assert user_only["total"] == 0

    builtin = await skill_tools.skill_search(query="视频制作 默认流程", category="workflow", scope="builtin")
    assert builtin["ok"] is True
    assert any(item["name"] == "video_production" for item in builtin["skills"])


@pytest.mark.asyncio
async def test_markdown_skill_search_matches_body_and_prioritizes_user_skills(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    prompt_dir = skills_root / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "storyboard_video_prompt.md").write_text(
        "把分镜图和剧情改写成图生视频提示词，重点写动作、镜头、节奏和衔接。\n",
        encoding="utf-8",
    )

    found = await skill_tools.skill_search(query="图生视频 衔接", category="prompt")

    assert found["total"] >= 1
    assert found["skills"][0]["name"] == "storyboard_video_prompt"
    assert found["skills"][0]["scope"] == "user"
    assert found["skills"][0]["priority"] == 0

    mixed_query = await skill_tools.skill_search(query="video production storyboard 视频 分镜 提示词", category="prompt")

    assert mixed_query["total"] >= 1
    assert mixed_query["skills"][0]["name"] == "storyboard_video_prompt"
    assert mixed_query["skills"][0]["scope"] == "user"
    assert mixed_query["skills"][0]["match"]["mode"] == "query_partial"

    builtin_query = await skill_tools.skill_search(query="分镜 宫格", category="prompt", scope="builtin")
    assert builtin_query["scope_filter"] == "builtin"
    assert all(item["scope"] == "builtin" for item in builtin_query["skills"])
    assert any(item["name"] == "shot_grid_prompt" for item in builtin_query["skills"])


@pytest.mark.asyncio
async def test_builtin_video_prompt_modules_are_discoverable(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    expected = {
        "写剧本": "script_writing",
        "人物提示词": "character_prompt",
        "场景提示词": "scene_prompt",
        "分镜 宫格": "shot_grid_prompt",
        "视频提示词": "video_prompt",
    }

    for query, name in expected.items():
        found = await skill_tools.skill_search(query=query, category="prompt", scope="builtin")
        assert found["ok"] is True
        assert any(item["name"] == name and item["scope"] == "builtin" for item in found["skills"])

        loaded = await skill_tools.skill_get_skill(name, category="prompt", scope="builtin")
        assert loaded["ok"] is True
        assert loaded["scope"] == "builtin"
        assert loaded["category"] == "prompt"


@pytest.mark.asyncio
async def test_skill_search_supports_batch_module_queries(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    result = await skill_tools.skill_search(
        category="prompt",
        scope="builtin",
        queries=["写剧本", "人物提示词", "分镜 宫格", "视频提示词"],
    )

    assert result["ok"] is True
    assert result["mode"] == "multi_query"
    assert result["queries"] == ["写剧本", "人物提示词", "分镜 宫格", "视频提示词"]
    assert [group["query"] for group in result["groups"]] == result["queries"]
    assert all(group["total"] >= 1 for group in result["groups"])
    names = {item["name"] for item in result["skills"]}
    assert {"script_writing", "character_prompt", "shot_grid_prompt", "video_prompt"} <= names
    assert all(item["scope"] == "builtin" for item in result["skills"])
    assert "primary_skill" in result["hint"]


@pytest.mark.asyncio
async def test_builtin_storyboard_review_skill_is_discoverable(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    found = await skill_tools.skill_search(query="分镜 检查", category="review", scope="builtin")

    assert found["ok"] is True
    assert any(item["name"] == "storyboard_frame_check" for item in found["skills"])

    loaded = skill_tools.load_review_skill_by_key("storyboard_frame_check")
    assert loaded["ok"] is True
    assert loaded["scope"] == "builtin"
    assert "叙事是否清晰" in loaded["content"]


@pytest.mark.asyncio
async def test_skill_scope_rejects_unknown_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(tmp_path / "skills"))

    found = await skill_tools.skill_search(query="video", category="workflow", scope="remote")
    assert found["ok"] is False
    assert found["error_kind"] == "invalid_skill_scope"
    assert found["available_scopes"] == ["user", "builtin"]

    loaded = await skill_tools.skill_get_skill("video_production", category="workflow", scope="remote")
    assert loaded["ok"] is False
    assert loaded["error_kind"] == "invalid_skill_scope"


@pytest.mark.asyncio
async def test_review_skills_are_searched_separately_and_prefer_reviewer(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    review_dir = skills_root / "review"
    prompt_dir = skills_root / "prompts"
    review_dir.mkdir(parents=True)
    prompt_dir.mkdir(parents=True)
    (review_dir / "storyboard_frame_check.md").write_text(
        "---\n"
        "description: 分镜画面合理性检查\n"
        "category: review\n"
        "applies_to: 分镜检查 storyboard review\n"
        "---\n\n"
        "逐格检查叙事、情绪和镜头衔接。\n",
        encoding="utf-8",
    )
    (prompt_dir / "shot_grid_video_prompt.md").write_text(
        "---\n"
        "description: 分镜写视频提示词\n"
        "category: prompt\n"
        "applies_to: 分镜提示词 视频提示词\n"
        "---\n\n"
        "把分镜整理成视频提示词。\n",
        encoding="utf-8",
    )

    review = await skill_tools.skill_search(query="分镜 检查", category="review")
    prompt = await skill_tools.skill_search(query="分镜 检查", category="prompt")

    assert [item["name"] for item in review["skills"]] == ["storyboard_frame_check"]
    assert review["skills"][0]["recommended_tool"] == "agent.review"
    assert prompt["skills"][0]["name"] == "shot_grid_video_prompt"

    loaded_for_self_check = await skill_tools.skill_get_skill("storyboard_frame_check", category="review")
    assert loaded_for_self_check["ok"] is True
    assert loaded_for_self_check["preferred_tool"] == "agent.review"
    assert "逐格检查" in loaded_for_self_check["content"]

    loaded = skill_tools.load_review_skill_by_key("storyboard_frame_check")
    assert loaded["ok"] is True
    assert "逐格检查" in loaded["content"]
