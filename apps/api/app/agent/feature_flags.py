"""Feature flag registry and kill-switch evaluation.

Defaults live in code so experiments have one documented registry. Runtime
overrides come from config/runtime.jsonc app_settings and environment variables.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Any


@dataclass(frozen=True)
class FeatureFlag:
    name: str
    default: bool
    description: str
    owner: str = "agent"


FEATURE_FLAGS: dict[str, FeatureFlag] = {
    "agent.deferred_tools": FeatureFlag(
        name="agent.deferred_tools",
        default=True,
        description="Expose a stable core tool surface and load low-frequency tools through tool.search/tool.execute.",
    ),
    "agent.context_compaction": FeatureFlag(
        name="agent.context_compaction",
        default=True,
        description="Compact large tool results and long message history before the model context grows too large.",
    ),
    "agent.trace": FeatureFlag(
        name="agent.trace",
        default=True,
        description="Write compact JSONL agent traces for each run.",
    ),
    "agent.slash_commands": FeatureFlag(
        name="agent.slash_commands",
        default=True,
        description="Handle /plan, /reset, /project, and /doctor deterministically before LLM routing.",
    ),
    "agent.chat_event_contracts": FeatureFlag(
        name="agent.chat_event_contracts",
        default=True,
        description="Validate known chat SSE events with typed contracts before streaming.",
    ),
    "agent.subagents_readonly": FeatureFlag(
        name="agent.subagents_readonly",
        default=True,
        description="Restrict subagents to read-only reviewer/debugger/mentor roles.",
    ),
    "agent.debug_api": FeatureFlag(
        name="agent.debug_api",
        default=True,
        description="Expose read-only agent doctor and trace debug endpoints.",
    ),
    "ui.agent_debug_tab": FeatureFlag(
        name="ui.agent_debug_tab",
        default=True,
        description="Show the Agent Debug settings tab in the web app.",
        owner="ui",
    ),
}


def _env_suffix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled", "allow"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", "deny"}:
            return False
    return None


def _dict_setting(app_settings: dict[str, Any], key: str) -> dict[str, Any]:
    value = app_settings.get(key)
    return value if isinstance(value, dict) else {}


def evaluate_feature_flags(
    app_settings: dict[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return normalized feature states keyed by feature name.

    Precedence:
      1. code default
      2. app_settings.feature_flags[name]
      3. DRAMA_FEATURE_<NAME>
      4. kill switches force disabled:
         app_settings.kill_switches[name], DRAMA_KILL_<NAME>,
         DRAMA_FEATURE_KILL_<NAME>, or DRAMA_FEATURE_KILL_ALL
    """
    settings = app_settings or {}
    env_map = env if env is not None else os.environ
    runtime_flags = _dict_setting(settings, "feature_flags")
    runtime_kills = _dict_setting(settings, "kill_switches")
    global_kill = _parse_bool(env_map.get("DRAMA_FEATURE_KILL_ALL")) is True

    states: dict[str, dict[str, Any]] = {}
    for name, flag in sorted(FEATURE_FLAGS.items()):
        enabled = flag.default
        source = "default"
        runtime_value = _parse_bool(runtime_flags.get(name))
        if runtime_value is not None:
            enabled = runtime_value
            source = "app_settings.feature_flags"

        suffix = _env_suffix(name)
        env_key = f"DRAMA_FEATURE_{suffix}"
        env_value = _parse_bool(env_map.get(env_key))
        if env_value is not None:
            enabled = env_value
            source = env_key

        killed = False
        kill_source: str | None = None
        runtime_kill = _parse_bool(runtime_kills.get(name))
        if runtime_kill is True:
            killed = True
            kill_source = "app_settings.kill_switches"

        for key in (f"DRAMA_KILL_{suffix}", f"DRAMA_FEATURE_KILL_{suffix}"):
            if _parse_bool(env_map.get(key)) is True:
                killed = True
                kill_source = key
                break

        if global_kill:
            killed = True
            kill_source = "DRAMA_FEATURE_KILL_ALL"

        if killed:
            enabled = False

        states[name] = {
            "name": name,
            "enabled": enabled,
            "default": flag.default,
            "source": source,
            "killed": killed,
            "kill_source": kill_source,
            "owner": flag.owner,
            "description": flag.description,
        }
    return states


async def get_feature_states() -> dict[str, dict[str, Any]]:
    from app.config_store import get_store

    cfg = await get_store().get_runtime()
    return evaluate_feature_flags(cfg.app_settings)


async def is_feature_enabled(name: str) -> bool:
    states = await get_feature_states()
    state = states.get(name)
    return bool(state and state["enabled"])


def is_feature_enabled_from_env(name: str, *, env: Mapping[str, str] | None = None) -> bool:
    states = evaluate_feature_flags({}, env=env)
    state = states.get(name)
    return bool(state and state["enabled"])
