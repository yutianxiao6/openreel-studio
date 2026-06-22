import asyncio

import pytest

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


def test_readonly_role_system_prompt_forbids_mutation() -> None:
    preset = agent_tools._resolve_role("debugger", None)
    system = agent_tools._build_subagent_system(
        preset,
        "检查失败节点",
        {"node_id": "node-1"},
    )

    assert "只读子 Agent" in system
    assert "禁止调用任何写入、执行、生成、删除、批准、重置或配置变更工具" in system


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
    system = agent_tools._build_subagent_system(
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

    assert "用户自定义分镜检查" in system
    assert "逐格核对分镜内容" in system
    assert "发现新增剧情时 safe_to_run=false" in system
    assert "用户自定义分镜一致性检查" in system
    assert "不得新增分镜没有的角色" in system
    assert "自定义审查 skill 是本轮主要检查标准" in system
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
    assert "先修原节点再重试" in inputs["review_skill"]["content"]


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
