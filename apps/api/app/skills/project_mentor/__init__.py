"""skill.project_mentor - project-specific mentor and docs navigator."""
from __future__ import annotations

from pathlib import Path
import re

from app.mcp_tools.registry import register


_SKILL_DIR = Path(__file__).resolve().parent
_GUIDE_DIR = _SKILL_DIR / "guides"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "package.json").exists() and (parent / "apps").exists():
            return parent
    return Path(__file__).resolve().parents[5]


_CUSTOM_PROMPT_GUIDE_DIR = _repo_root() / "data" / "prompt_guides"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


_REFERENCES = {
    "overview": [
        "README.md",
        "README.en.md",
        "docs/README.md",
        "docs/README.en.md",
        "apps/api/README.md",
        "apps/api/app/skills/video_production/SKILL.md",
    ],
    "agent_loop": [
        "apps/api/app/agent/orchestrator.py",
        "apps/api/app/agent/lifecycle_hooks.py",
        "apps/api/app/agent/reset_flow.py",
        "apps/api/app/agent/trace_store.py",
        "apps/api/app/agent/video_mode.py",
        "apps/api/app/mcp_tools/registry.py",
    ],
    "video_workflow": [
        "apps/api/app/skills/project_mentor/guides/video_workflow_t2v.md",
        "apps/api/app/skills/project_mentor/guides/video_workflow_storyboard.md",
        "apps/api/app/skills/project_mentor/guides/video_workflow_shot_images.md",
        "apps/api/app/skills/project_mentor/guides/video_workflow_story_template.md",
        "apps/api/app/agent/prompts/clarify.py",
        "apps/api/app/agent/prompts/video_duration.py",
        "apps/api/app/agent/prompts/segment_rule.py",
        "apps/api/app/agent/prompts/video_types.py",
        "apps/api/app/agent/prompts/flow_paths.py",
        "apps/api/app/agent/video_mode.py",
        "apps/api/app/mcp_tools/interaction_tools.py",
    ],
    "video_workflow_t2v": [
        "apps/api/app/skills/project_mentor/guides/video_workflow.md",
        "apps/api/app/skills/video_production/SKILL.md",
        "apps/api/app/mcp_tools/node_universal.py",
    ],
    "video_workflow_storyboard": [
        "apps/api/app/skills/project_mentor/guides/video_workflow.md",
        "apps/api/app/skills/video_production/SKILL.md",
        "apps/api/app/mcp_tools/node_universal.py",
    ],
    "video_workflow_shot_images": [
        "apps/api/app/skills/project_mentor/guides/video_workflow.md",
        "apps/api/app/skills/video_production/SKILL.md",
        "apps/api/app/mcp_tools/node_universal.py",
    ],
    "video_workflow_story_template": [
        "apps/api/app/skills/project_mentor/guides/video_workflow.md",
        "apps/api/app/skills/story_template_method/SKILL.md",
        "apps/api/app/mcp_tools/node_universal.py",
    ],
    "production_audit_guide": [
        "apps/api/app/agent/prompts/audit_rule.py",
        "apps/api/app/agent/prompts/runtime_context.py",
        "apps/api/app/agent/prompt_synthesizer.py",
        "apps/api/app/mcp_tools/node_universal.py",
        "apps/api/app/api/routes_agent_debug.py",
    ],
    "node_repair_guide": [
        "apps/api/app/agent/prompts/repair_rule.py",
        "apps/api/app/agent/prompts/rerun_rule.py",
        "apps/api/app/mcp_tools/node_universal.py",
        "apps/api/app/agent/permission_policy.py",
    ],
    "slash_commands": [
        "apps/api/app/agent/slash_commands.py",
        "apps/api/app/api/routes_chat.py",
    ],
    "debugging": [
        "apps/api/app/agent/trace_store.py",
        "apps/api/app/agent/context_compact.py",
        "apps/api/app/api/routes_agent_debug.py",
    ],
    "prompt_compaction": [
        "apps/api/app/agent/prompts/",
        "apps/api/app/agent/permission_policy.py",
        "apps/api/app/agent/collaboration_mode.py",
        "apps/api/app/agent/blueprint_confirmation.py",
        "apps/api/app/mcp_tools/node_universal.py",
    ],
}

_GUIDANCE = {
    "overview": (
        "OpenReel Studio is a monorepo with a Next.js web app, FastAPI API, "
        "SQLite state, SSE chat streams, and a single visible canvas of workflow "
        "nodes. Start from README.md or README.en.md, then use the matching "
        "docs/README language index and skill files for setup and production rules."
    ),
    "agent_loop": (
        "Keep the Agent loop small. Core production tools are project.get_state, "
        "interaction.request_input, skill.search and skill.get, task.create/list/"
        "update/complete, agent.review, node.list/get/create/update/run, canvas.delete, "
        "and tool.search/describe/execute for deferred capabilities. Natural-language "
        "tasks enter the Agent loop; backend preprocessing may clean input and "
        "stale state but must not decide business actions for the model."
    ),
    "video_workflow": (
        "普通图片/视频制作走节点优先流程：先用 skill.search 查内置和用户 workflow，"
        "没有匹配时用 skill.get 读取内置 `video_production` markdown skill，再创建或更新轻量任务和 "
        "text/image/video/audio 节点。默认成片骨架是剧本/规划 text、人物图、场景图、分镜图、video。"
        "主 Agent 规划节点图和依赖；每个节点是独立任务。可复用 workflow 编译时，workflow_spec 在隔离上下文读取相关独立 prompt skill，"
        "把稳定写法写进 V2 逻辑步骤的 prompt；物化后主 Agent 用 workflow.run_step/run_next/run_all 填 inputs 并启动运行，内部 runner 编译私有提示词阶段并调用 node.run。长任务先批量查询 prompt skill 形成 skill_plan，后续同类节点复用。"
        "用 task blocked_by 表达执行依赖，用 parent_node_id 和 fields.references "
        "自动连线表达分组与依赖；references 可用 role 区分 context、visual_reference 和 source_image。复杂阶段产出用 agent.review 做只读检查。只补问阻塞事实，用 interaction.request_input；用户继续自定义时先修订确认；15秒短视频通常不问分集分段，"
        "但仍按剧本、人物图、场景图和分镜图准备。泛化短视频只给时长时，模型可以自行选择一个具体简单概念并写入剧本/规划 text 节点。"
    ),
    "video_workflow_t2v": (
        "文生视频适合快速概念、无参考图或不强求一致性。创建剧本/规划 text 节点记录主题、"
        "风格、时长、画幅和假设，然后读取视频提示词 prompt skill，创建 video 节点写可执行 prompt；需要生成时运行该 video 节点。"
    ),
    "video_workflow_storyboard": (
        "分镜/宫格分镜适合需要镜头节奏、动作调度或视觉连续性的短片。创建剧本/规划 text，"
        "再创建 storyboard image 节点，读取或使用其输出后更新 video prompt 并运行 video 节点。"
    ),
    "video_workflow_shot_images": (
        "单张分镜图流程适合关键镜头需要更高图片质量或强控制。每个关键镜头是 image 节点，"
        "最终 video 节点通过 fields.references 引用这些图片。"
    ),
    "video_workflow_story_template": (
        "故事模板图流程适合复杂动作、空间调度和强美术方向。先读 skill.story_template_method，"
        "创建 story-template image 节点，再用它驱动 video 节点。"
    ),
    "production_audit_guide": (
        "Before declaring work done, read node statuses and outputs, check failed/"
        "pending/running nodes, verify references resolve, confirm generated media "
        "URLs/files exist, and make sure the final answer names only completed or "
        "explicitly blocked work. Use trace/tool result files when behavior is unclear."
    ),
    "node_repair_guide": (
        "Repair the original node first. Read node.get and nearby node.list. For "
        "dependency_missing, missing prompt, missing reference images, or empty "
        "upstream output, fix or run upstream nodes before retrying the target. "
        "Patch local fields with node.update; rerun with node.run. Do not delete "
        "and recreate unless the latest user message asks for replacement."
    ),
    "slash_commands": (
        "Slash commands are deterministic control-plane operations handled before "
        "LLM routing: mode, plan, reset, and doctor."
    ),
    "debugging": (
        "Use SSE events, persisted message metadata, queryable agent trace events, "
        "tool result files, node status summaries, and artifacts before changing prompts."
    ),
    "prompt_compaction": (
        "Keep prompt sections as short constraint indexes. Move examples and "
        "maintenance guidance to skills, validators, tests, or README-facing docs, "
        "and enforce stable behavior with backend state, permission policy, validators, and tests."
    ),
}

_GUIDE_FILE_BY_TOPIC = {
    "video_workflow": "video_workflow.md",
    "video_workflow_t2v": "video_workflow_t2v.md",
    "video_workflow_storyboard": "video_workflow_storyboard.md",
    "video_workflow_shot_images": "video_workflow_shot_images.md",
    "video_workflow_story_template": "video_workflow_story_template.md",
}


def _detail_key(value: str) -> str:
    raw = (value or "summary").strip().lower()
    if raw in {"full", "example", "examples", "detail", "details", "完整", "示例", "完整示例"}:
        return "full"
    return "summary"


def _profile_key(value: str) -> str:
    raw = (value or "default").strip()
    return raw if _PROFILE_RE.match(raw) else "default"


def _read_prompt_guide(topic: str, profile: str = "default") -> tuple[str, str] | tuple[None, None]:
    filename = _GUIDE_FILE_BY_TOPIC.get(topic)
    if not filename:
        return None, None
    profile = _profile_key(profile)
    if profile != "default":
        profiled = _CUSTOM_PROMPT_GUIDE_DIR / "profiles" / profile / f"{topic}.md"
        if profiled.exists():
            return profiled.read_text(encoding="utf-8"), str(profiled.relative_to(_repo_root()))
    custom = _CUSTOM_PROMPT_GUIDE_DIR / f"{topic}.md"
    if custom.exists():
        return custom.read_text(encoding="utf-8"), str(custom.relative_to(_repo_root()))
    builtin = _GUIDE_DIR / filename
    if builtin.exists():
        return builtin.read_text(encoding="utf-8"), str(builtin.relative_to(_repo_root()))
    return None, None


@register(
    "skill.project_mentor",
    description="Official OpenReel project mentor for node-first production, debugging, repair, and delivery audit",
    tags=["project", "mentor", "video", "production", "guide", "prompt"],
    metadata={"source": "skill"},
    search_hint=(
        "guide video workflow node-first canvas text image video audio node storyboard text-to-video image-to-video prompt writer parent_node_id fields.references visual_reference source_image auto edges "
        "character_image scene_image storyboard_image first_frame_image last_frame_image story_template video_prompt "
        "T2V I2V R2V final video prompt keyframe first frame last frame multi reference @图片 asset style reference production audit node repair "
        "failed node rerun dependency_missing trace debugging 项目规则 视频工作流 提示词写法 制作审查 "
        "分镜 文生视频 图生视频 制作方法 首次澄清 失败节点 原地修复 节点修复 重跑 排障 "
        "最终视频提示词 文生视频 图生视频 首尾帧 多图参考 参考图 风格参考 宫格分镜视频提示词 "
        "视觉资产提示词 人物图 场景图 分镜图 故事模板图 首帧图 尾帧图 提示词修复 "
        "story_template optional method skill.story_template_method"
    ),
    usage_hints=[
        "Default detail='summary' returns fit criteria first; call detail='full' only after the topic matches the current task.",
        "Use profile='default' unless project settings select another prompt-guide profile; custom profiles live under data/prompt_guides/profiles/<profile>/<topic>.md.",
        "Use topic='video_workflow' for complex video planning after the currently blocking facts are known; short clips do not require episode or segment questions.",
        "Use topic='video_workflow_t2v' for pure text-to-video; it does not require generated images.",
        "Use topic='video_workflow_storyboard' for storyboard/grid-board driven video; final video prompts wait for the storyboard image.",
        "Use topic='video_workflow_shot_images' for separate high-quality shot images.",
        "Use topic='video_workflow_story_template' for story-template driven video after reading skill.story_template_method.",
        "Use node fields and skills for prompt methods; ordinary production does not use a prompt template directory.",
        "Use topic='production_audit_guide' before final delivery or when checking video production consistency.",
        "Use topic='node_repair_guide' before complex failed-node repair or rerun recovery.",
        "Use skill.story_template_method before choosing story-template image-to-video production.",
        "Use topic='debugging' when trace or agent behavior is unclear during video production.",
    ],
)
async def project_mentor(
    topic: str = "overview",
    detail: str = "summary",
    profile: str = "default",
) -> dict:
    key = (topic or "overview").strip().lower()
    if key not in _GUIDANCE:
        key = "overview"
    detail_key = _detail_key(detail)
    profile_key = _profile_key(profile)
    has_full = key in _GUIDE_FILE_BY_TOPIC
    result = {
        "topic": key,
        "detail": "summary",
        "profile": profile_key,
        "guidance": _GUIDANCE[key],
        "references_count": len(_REFERENCES[key]),
        "reference_policy": "源码参考仅用于诊断计数；当前 guidance/guide_content 已包含可执行规则，不要把源码路径当作 file.read_text 目标。",
        "available_topics": sorted(_GUIDANCE),
    }
    if has_full:
        result.update({
            "has_full_guide": True,
            "read_order": "先用当前 summary 判断是否匹配任务；匹配后再调用同一 topic 且 detail='full' 读取完整指南。",
            "full_guide_request": {"topic": key, "detail": "full", "profile": profile_key},
            "custom_override_path": f"data/prompt_guides/{key}.md",
            "profile_override_path": f"data/prompt_guides/profiles/{profile_key}/{key}.md",
        })
        if detail_key == "full":
            content, source = _read_prompt_guide(key, profile=profile_key)
            result["detail"] = "full"
            if content:
                result["guide_content"] = content
                result["guide_source"] = source
                result["customized"] = bool(source and source.startswith("data/prompt_guides/"))
                result["profile_customized"] = bool(
                    source and source.startswith(f"data/prompt_guides/profiles/{profile_key}/")
                )
            else:
                result["guide_error"] = "full guide file not found"
    return result
