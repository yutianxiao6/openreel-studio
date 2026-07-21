import pytest

from app.agent import slash_commands
from app.agent.feature_flags import evaluate_feature_flags, is_feature_enabled_from_env
from app.mcp_tools.registry import registry


def test_feature_flags_use_code_defaults() -> None:
    states = evaluate_feature_flags({}, env={})

    assert states["agent.deferred_tools"]["enabled"] is True
    assert states["agent.deferred_tools"]["source"] == "default"
    assert states["agent.deferred_tools"]["killed"] is False


def test_feature_flags_allow_runtime_overrides() -> None:
    states = evaluate_feature_flags(
        {"feature_flags": {"agent.deferred_tools": False}},
        env={},
    )

    assert states["agent.deferred_tools"]["enabled"] is False
    assert states["agent.deferred_tools"]["source"] == "app_settings.feature_flags"


def test_feature_flags_allow_env_overrides() -> None:
    states = evaluate_feature_flags(
        {"feature_flags": {"agent.deferred_tools": False}},
        env={"DRAMA_FEATURE_AGENT_DEFERRED_TOOLS": "true"},
    )

    assert states["agent.deferred_tools"]["enabled"] is True
    assert states["agent.deferred_tools"]["source"] == "DRAMA_FEATURE_AGENT_DEFERRED_TOOLS"


def test_feature_kill_switch_forces_disabled() -> None:
    states = evaluate_feature_flags(
        {
            "feature_flags": {"agent.deferred_tools": True},
            "kill_switches": {"agent.deferred_tools": True},
        },
        env={},
    )

    assert states["agent.deferred_tools"]["enabled"] is False
    assert states["agent.deferred_tools"]["killed"] is True
    assert states["agent.deferred_tools"]["kill_source"] == "app_settings.kill_switches"


def test_feature_env_kill_switch_forces_disabled() -> None:
    states = evaluate_feature_flags(
        {"feature_flags": {"agent.deferred_tools": True}},
        env={"DRAMA_KILL_AGENT_DEFERRED_TOOLS": "1"},
    )

    assert states["agent.deferred_tools"]["enabled"] is False
    assert states["agent.deferred_tools"]["killed"] is True
    assert states["agent.deferred_tools"]["kill_source"] == "DRAMA_KILL_AGENT_DEFERRED_TOOLS"


def test_env_feature_enabled_helper_matches_kill_switch() -> None:
    assert is_feature_enabled_from_env("agent.deferred_tools", env={}) is True
    assert is_feature_enabled_from_env(
        "agent.deferred_tools",
        env={"DRAMA_KILL_AGENT_DEFERRED_TOOLS": "1"},
    ) is False


def test_feature_global_kill_switch_forces_all_disabled() -> None:
    states = evaluate_feature_flags({}, env={"DRAMA_FEATURE_KILL_ALL": "yes"})

    assert all(not state["enabled"] for state in states.values())
    assert all(state["kill_source"] == "DRAMA_FEATURE_KILL_ALL" for state in states.values())


@pytest.mark.asyncio
async def test_feature_tools_are_registered() -> None:
    list_tool = registry.get("feature.list")
    check_tool = registry.get("feature.is_enabled")

    assert list_tool is not None
    assert check_tool is not None

    unknown = await check_tool.handler(name="missing.flag")
    assert unknown["ok"] is False
    assert unknown["enabled"] is False
    assert "agent.deferred_tools" in unknown["known"]


@pytest.mark.asyncio
async def test_doctor_snapshot_includes_feature_flag_summary(monkeypatch) -> None:
    async def fake_project_get_state(project_id: str):
        return {"project_mode": "single_node"}

    async def fake_node_summary(project_id: str):
        return {"total": 0, "by_type": {}, "by_status": {}}

    async def fake_feature_states():
        return {
            "agent.trace": {
                "name": "agent.trace",
                "enabled": True,
                "default": True,
                "source": "default",
                "killed": False,
                "kill_source": None,
                "owner": "agent",
                "description": "Trace runs.",
            },
            "agent.deferred_tools": {
                "name": "agent.deferred_tools",
                "enabled": False,
                "default": True,
                "source": "default",
                "killed": True,
                "kill_source": "DRAMA_KILL_AGENT_DEFERRED_TOOLS",
                "owner": "agent",
                "description": "Deferred tools.",
            },
        }

    monkeypatch.setattr(slash_commands, "project_get_state", fake_project_get_state)
    monkeypatch.setattr(slash_commands, "_node_summary", fake_node_summary)
    monkeypatch.setattr(slash_commands, "get_feature_states", fake_feature_states)

    snapshot = await slash_commands.build_doctor_snapshot("project-1")

    assert snapshot["ok"] is True
    feature_flags = snapshot["feature_flags"]
    assert feature_flags["total"] == 2
    assert feature_flags["enabled"] == 1
    assert feature_flags["disabled"] == 1
    assert feature_flags["killed"] == 1
    assert feature_flags["killed_names"] == ["agent.deferred_tools"]
    assert feature_flags["owners"]["agent"]["killed"] == 1
    assert "功能开关：1/2 开启，1 个 kill switch 生效" in snapshot["text"]
