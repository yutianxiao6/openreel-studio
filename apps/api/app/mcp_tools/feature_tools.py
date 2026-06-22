"""feature.* tools for inspecting feature flags and kill switches."""
from __future__ import annotations

from typing import Any

from app.agent.feature_flags import FEATURE_FLAGS, get_feature_states
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob


async def feature_list(
    owner: str | None = None,
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    states = await get_feature_states()
    items = list(states.values())
    if owner:
        items = [item for item in items if item.get("owner") == owner]
    if query or regex or pattern:
        items = [
            item
            for item in items
            if match_text(
                search_blob(item),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            ).get("matched")
        ]
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
