"""skill.story_template_method - optional story-template image-to-video guide."""
from __future__ import annotations

from pathlib import Path

from app.mcp_tools.registry import register


_SKILL_DIR = Path(__file__).resolve().parent

_SUMMARY = (
    "Story-template is an optional image-to-video method, not the default fallback. "
    "Use it only when the user explicitly asks for story-template production, when "
    "a segment has complex action/blocking or strong art direction, or when "
    "ordinary storyboard/keyframe references would not express space and motion reliably. It "
    "requires a strong image model: high-resolution multi-module boards, readable "
    "action flow, spatial/camera layout, and stable character/scene anchors. "
    "Prompt writing is self-contained in this skill; do not use a separate prompt template library. "
    "If the board is below 2560x1440, blurry, unreadable, or visually inconsistent, "
    "regenerate it before video."
)


def _body() -> str:
    skill_md = _SKILL_DIR / "SKILL.md"
    if not skill_md.exists():
        return ""
    raw = skill_md.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return raw.strip()


@register(
    "skill.story_template_method",
    description=(
        "读取故事模板图生视频方法指南，说明适用条件、图片模型要求、通用 image/video 节点组织和提示词写法。"
        "默认 detail='summary'；确认需要故事模板方法后再读取 detail='full'。该工具不创建节点、生成媒体或批准方案。"
    ),
    tags=["video", "image_to_video", "story_template", "guide"],
    metadata={"source": "skill", "category": "guide"},
    search_hint=(
        "story template story_template image video image_to_video complex action blocking "
        "visual development board 3840x2160 2560x1440 story board action flow camera map art direction "
        "故事模板 图生视频 复杂动作 动作调度 视觉开发板 高分辨率 3840x2160 2560x1440"
    ),
    usage_hints=[
        "Use only when story-template is explicitly requested or ordinary storyboard/keyframe references are insufficient.",
        "Requires a strong image model; not the default fallback video method.",
        "Read full detail before creating the story-template image node.",
    ],
)
async def story_template_method(detail: str = "summary") -> dict:
    detail_key = (detail or "summary").strip().lower()
    result = {
        "topic": "story_template_method",
        "detail": "summary",
        "guidance": _SUMMARY,
        "when_to_use": [
            "user_explicitly_requests_story_template",
            "complex_action_or_blocking",
            "strong_art_direction",
            "ordinary_storyboard_or_keyframes_insufficient",
        ],
        "not_default_fallback": True,
        "node_pattern": [
            {"type": "image", "purpose": "story_template_board"},
            {"type": "video", "purpose": "video_from_story_template_board"},
        ],
        "prompt_source": "skill.story_template_method",
    }
    if detail_key in {"full", "detail", "details", "完整"}:
        result["detail"] = "full"
        result["guide_content"] = _body()
    return result
