"""Event stream tools — query lifecycle events for debugging and monitoring."""
from __future__ import annotations

from typing import Any

from app.agent.event_stream import event_stream
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
from app.mcp_tools.registry import register


@register("events.tail", description="Get the most recent lifecycle events", tags=["events", "read"])
async def events_tail(project_id: str = "", n: int = 20) -> dict[str, Any]:
    events = event_stream.tail(project_id or None, n=n)
    return {"events": events, "count": len(events)}


@register("events.query", description="Query events by type and optional fuzzy/regex text filter", tags=["events", "read"])
async def events_query(
    project_id: str = "",
    event_type: str = "",
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    events = event_stream.query(
        project_id=project_id or None,
        event_type=event_type or None,
        limit=limit,
    )
    if query or regex or pattern:
        filtered: list[dict[str, Any]] = []
        for event in events:
            match = match_text(
                search_blob(event),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            if match.get("matched"):
                item = dict(event)
                item["match"] = {
                    key: value
                    for key, value in match.items()
                    if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
                }
                filtered.append(item)
        events = filtered
    return {
        "events": events,
        "count": len(events),
        "filters": {
            "project_id": project_id,
            "event_type": event_type,
            "query": query,
            "regex": regex,
            "pattern": pattern,
            "case_sensitive": case_sensitive,
            "limit": limit,
        },
    }
