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

    found = await skill_tools.skill_search(query="custom_flow")
    assert any(item["name"] == "custom_flow" for item in found["skills"])

    loaded = await skill_tools.skill_get_skill("custom_flow")
    assert loaded["ok"] is True
    assert "自定义流程" in loaded["content"]


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

    found = await skill_tools.skill_search(query="bright")
    assert any(item["name"] == "bright_prompt" for item in found["skills"])

    loaded = await skill_tools.skill_get_skill("bright_prompt")
    assert loaded["ok"] is True
    assert "画面明亮" in loaded["content"]
