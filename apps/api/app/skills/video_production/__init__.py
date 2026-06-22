"""skill.video_production - node-first image/video production guide."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.mcp_tools.registry import register


_SKILL_PATH = Path(__file__).with_name("SKILL.md")
_FALLBACK_SUMMARY = (
    "节点优先视频制作 Skill。补全、创建或修复 text/image/video 制作节点前读取；"
    "详细流程以同目录 SKILL.md 为准。"
)
_REFERENCE_POLICY = (
    "guidance/model_summary 已包含模型需要执行的指南正文；skill_path 只作为诊断来源，"
    "不要把它作为 file.read_text 的读取目标。需要重读时再次调用 skill.video_production。"
)
_STORY_TEMPLATE_MARKERS = ("story_template", "story-template", "story template", "故事模板")


def _repo_relative_skill_path() -> str:
    for parent in _SKILL_PATH.parents:
        candidate = parent / "apps" / "api" / "app" / "skills" / "video_production" / "SKILL.md"
        if candidate == _SKILL_PATH:
            return str(_SKILL_PATH.relative_to(parent))
    return "apps/api/app/skills/video_production/SKILL.md"


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    body_start = text.find("\n", end + 4)
    if body_start == -1:
        return ""
    return text[body_start + 1 :]


def _read_skill_markdown() -> str:
    try:
        text = _SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return f"# 视频制作 Skill\n\n{_FALLBACK_SUMMARY}"
    return _strip_frontmatter(text).strip()


def _extract_section(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    start = markdown.find(marker)
    if start == -1:
        return ""
    content_start = markdown.find("\n", start)
    if content_start == -1:
        return ""
    next_heading = markdown.find("\n## ", content_start + 1)
    if next_heading == -1:
        next_heading = len(markdown)
    return markdown[content_start + 1 : next_heading].strip()


def _without_section(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    start = markdown.find(marker)
    if start == -1:
        return markdown
    next_heading = markdown.find("\n## ", start + len(marker))
    if next_heading == -1:
        return markdown[:start].rstrip()
    return (markdown[:start].rstrip() + "\n\n" + markdown[next_heading + 1 :].lstrip()).strip()


def _mentions_story_template(request: str) -> bool:
    normalized = (request or "").strip().lower()
    return any(marker in normalized for marker in _STORY_TEMPLATE_MARKERS)


@register(
    "skill.video_production",
    description=(
        "读取节点优先的视频制作 Skill。补全、创建或修复 text/image/video 制作节点前使用。"
    ),
    tags=["skill", "guide", "video", "image", "production", "node"],
    metadata={
        "source": "skill",
        "is_read_only": True,
        "usage_hints": [
            "补全或创建视频、图片、分镜、关键帧或参考图节点前使用。",
            "普通制作优先读这个 Skill，而不是先走额外草稿对象或提示词模板检索。",
            "用户明确说故事模板/story_template 时，直接读取 skill.story_template_method(detail='full')。",
        ],
    },
    schema={
        "type": "object",
        "properties": {
            "detail": {
                "type": "string",
                "enum": ["summary", "full"],
                "default": "summary",
            },
            "request": {
                "type": "string",
            },
        },
    },
)
async def video_production(detail: str = "summary", request: str = "") -> dict:
    """Return the node-first video production Skill from SKILL.md."""
    normalized_detail = "full" if detail == "full" else "summary"
    full_guide = _read_skill_markdown()
    model_summary = _extract_section(full_guide, "模型摘要") or _FALLBACK_SUMMARY
    full_guidance = _without_section(full_guide, "模型摘要")
    guidance = full_guidance if normalized_detail == "full" else model_summary
    guidance_hash = hashlib.sha256(guidance.encode("utf-8")).hexdigest()[:16]
    skill_path = _repo_relative_skill_path()
    result = {
        "ok": True,
        "skill": "video_production",
        "skill_path": skill_path,
        "detail": normalized_detail,
        "request": request,
        "model_summary": model_summary,
        "guidance": guidance,
        "reference_policy": _REFERENCE_POLICY,
        "guidance_hash": guidance_hash,
        "cache_key": f"skill.video_production:{normalized_detail}:{guidance_hash}",
        "context_fragment": {
            "role": "user",
            "type": "skill",
            "markers": ["<skill>", "</skill>"],
            "name": "video_production",
            "path": skill_path,
            "body_field": "guidance",
            "reuse_policy": (
                "Treat this guidance as the active skill context. Re-read full only when "
                "the user changes process, cached detail is summary, or exact wording is needed."
            ),
        },
        "core_tools": [
            "project.get_state",
            "interaction.request_input",
            "task.create",
            "task.list",
            "task.update",
            "task.complete",
            "node.list",
            "node.get",
            "node.create",
            "node.update",
            "node.run",
        ],
        "node_types": ["text", "image", "video"],
        "next_action": (
            "创建最小完整的一批 text/image/video 节点，或只补问真正阻塞的信息。"
        ),
    }
    if _mentions_story_template(request):
        result["related_skill"] = {
            "tool": "skill.story_template_method",
            "access": "tool.search -> tool.execute",
            "input": {"detail": "full"},
            "reason": "用户明确要求故事模板方法；该方法由独立 skill 承接。",
        }
        result["next_action"] = (
            "用户明确要求故事模板时，直接用 tool.search/tool.execute 读取 "
            "skill.story_template_method(detail='full')；不要重复读取 skill.video_production。"
        )
    return result
