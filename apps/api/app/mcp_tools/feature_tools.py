"""feature.* tools for inspecting feature flags and kill switches."""
from __future__ import annotations

from typing import Any

from app.agent.feature_flags import FEATURE_FLAGS, get_feature_states


async def feature_list(owner: str | None = None) -> dict[str, Any]:
    states = await get_feature_states()
    items = list(states.values())
    if owner:
        items = [item for item in items if item.get("owner") == owner]
    return {
        "features": items,
        "count": len(items),
        "known": sorted(FEATURE_FLAGS),
    }


async def feature_is_enabled(name: str) -> dict[str, Any]:
    states = await get_feature_states()
    state = states.get(name)
    if state is None:
        return {
            "ok": False,
            "enabled": False,
            "error": f"Unknown feature flag: {name}",
            "known": sorted(FEATURE_FLAGS),
        }
    return {"ok": True, **state}
