"""Video workflow state reminder.

This module only reports coarse project state. Production strategy lives in
node fields, references, and dependencies.
"""
from __future__ import annotations


VideoMode = str

_MODE_LABELS: dict[VideoMode, str] = {
    "video_production": "视频制作",
    "single_node": "单节点创作",
    "custom_flow": "自定义流程",
    "skill": "Skill 流程",
    "skill_freeform": "Skill 流程",
}


def mode_label(mode: VideoMode | None) -> str:
    return _MODE_LABELS.get(mode or "", str(mode or ""))


def build_video_mode_system_reminder(
    state: dict,
    *,
    video_output_disabled: bool = False,
) -> str:
    mode = (state or {}).get("project_mode")
    if mode != "video_production":
        return ""
    label = mode_label(mode)
    if video_output_disabled:
        body = "当前请求按视觉预制作范围执行，输出文本说明和图片素材。"
    else:
        body = "模型按节点字段、references 和 depends_on 组织制作方法。"
    return f"[SYSTEM] 当前项目状态: {label}。{body}"
