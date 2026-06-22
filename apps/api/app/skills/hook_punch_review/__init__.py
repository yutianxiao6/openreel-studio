"""Internal hook/punch review helper."""
from __future__ import annotations

async def hook_punch_review(
    project_id: str,
    episode_number: int,
    node_id: str | None = None,
) -> dict:
    from app.mcp_tools import drama_tools

    full = await drama_tools.review_script(
        project_id=project_id,
        episode_number=episode_number,
        node_id=node_id,
    )
    if not isinstance(full, dict) or "review" not in full:
        return full

    review = full.get("review") or {}
    narrowed: dict = {}
    for key in ("hook", "hook_score", "opening", "punch", "climax", "cliffhanger", "score"):
        if key in review:
            narrowed[key] = review[key]
    issues = review.get("issues") or []
    if isinstance(issues, list):
        narrowed["issues"] = [
            i for i in issues
            if any(k in str(i).lower() for k in ("hook", "钩子", "爆点", "climax", "悬念"))
        ]
    return {
        "episode_number": episode_number,
        "narrowed_review": narrowed,
        "full_score": review.get("score"),
    }
