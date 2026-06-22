"""Deterministic collaboration-mode helpers for the Agent Loop."""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any


COLLABORATION_MODE_STATE_KEY = "agent_collaboration_mode"
MODE_DEFAULT = "default"
MODE_PLAN = "plan"


_PROPOSED_PLAN_RE = re.compile(
    r"<proposed_plan>\s*(?P<body>.*?)\s*</proposed_plan>",
    re.IGNORECASE | re.DOTALL,
)


def current_collaboration_mode(state: dict[str, Any] | None) -> str:
    if isinstance(state, dict) and str(state.get(COLLABORATION_MODE_STATE_KEY) or "").lower() == MODE_PLAN:
        return MODE_PLAN
    return MODE_DEFAULT


def is_plan_mode(state: dict[str, Any] | None) -> bool:
    return current_collaboration_mode(state) == MODE_PLAN


def collaboration_mode_patch(mode: str) -> dict[str, str]:
    normalized = MODE_PLAN if str(mode or "").lower() == MODE_PLAN else MODE_DEFAULT
    return {COLLABORATION_MODE_STATE_KEY: normalized}


def split_proposed_plan_blocks(text: str) -> tuple[str, str]:
    """Return user-visible text outside tags and markdown from plan blocks."""
    if not text:
        return "", ""
    bodies = [match.group("body").strip() for match in _PROPOSED_PLAN_RE.finditer(text)]
    markdown = "\n\n".join(body for body in bodies if body)
    visible = _PROPOSED_PLAN_RE.sub("", text).strip()
    return visible, markdown


def build_proposed_plan_doc(
    markdown: str,
    *,
    source_request: str = "",
    iteration: int = 1,
) -> dict[str, Any]:
    body = (markdown or "").strip()
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:10] if body else "empty"
    plan_id = f"proposed_plan_{int(time.time() * 1000)}_{digest}"
    title = "计划"
    summary = ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()[:80] or title
            continue
        if title == "计划":
            title = stripped.lstrip("-*0123456789. ").strip()[:80] or title
        if not summary and not stripped.startswith(("-", "*", "#")):
            summary = stripped[:160]
        if title != "计划" and summary:
            break
    return {
        "id": plan_id,
        "kind": "proposed_plan",
        "title": title,
        "summary": summary,
        "sections": [{"type": "markdown", "content": body}],
        "iteration": max(1, int(iteration or 1)),
        "source_request": source_request,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ui_surface": "proposed_plan",
    }


def proposed_plan_markdown(plan: dict[str, Any] | None) -> str:
    if not isinstance(plan, dict):
        return ""
    sections = plan.get("sections")
    if isinstance(sections, list):
        parts: list[str] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            if section.get("type") == "markdown" and isinstance(section.get("content"), str):
                parts.append(section["content"].strip())
        text = "\n\n".join(part for part in parts if part)
        if text:
            return text
    return str(plan.get("summary") or "").strip()
