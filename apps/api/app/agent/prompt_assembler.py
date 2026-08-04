"""System prompt assembler — assemble stable Agent Loop instructions.

This module must not parse natural-language business intent. The main Agent
Loop model reads the latest user message and chooses tools itself.

Section trigger types:
  - always      : 每次都加载
  - plan_mode   : 显式 Plan Mode，只读规划
  - workflow_build_mode : 显式 Workflow Build Mode，搭建/修改工作流
  - attachments : 用户带了附件
  - factory     : 动态构造(runtime_context)

Historical business triggers such as create/video/template/introspect are not
loaded automatically. The runtime Skill catalog supplies description-based
selection metadata; this assembler does not infer business intent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import prompts as prompts_pkg

# ────────────────────────────────────────────────────────────────────────────
# Context
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class PromptContext:
    project_id: str | None = None
    user_message: str = ""
    state: dict = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    model_configs: list[dict] | None = None
    user_facts: list[dict] = field(default_factory=list)
    project_facts: list[dict] = field(default_factory=list)
    has_script: bool = False
    has_characters: bool = False
    has_scenes: bool = False
    has_segments: bool = False
    has_recent_failure: bool = False
    project_mode: str | None = None
    collaboration_mode: str = "default"

    def cache_key(self) -> str:
        metadata = self.state.get("metadata")
        title = metadata.get("title") if isinstance(metadata, dict) else None

        payload = {
            "pid": self.project_id,
            "att": len(self.attachments),
            "title": title or "",
            "collaboration_mode": self.collaboration_mode,
            "tool_profile": select_tool_profile(self),
            "runtime": _runtime_state_signature(self.state),
            "skill_catalog": _skill_catalog_revision(),
            "explicit_skills": _explicit_skill_selection_signature(
                self.user_message, self.attachments
            ),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class PromptSectionStat:
    name: str
    trigger: str
    tier: str
    chars: int
    source: str = "static"


@dataclass(frozen=True)
class PromptAssemblyResult:
    system: str
    history: str
    sections: tuple[PromptSectionStat, ...]
    tool_namespaces: tuple[str, ...]
    cache_key: str
    tool_profile: str = "default"
    runtime: str = ""
    skills_context: str = ""
    skill_instructions: tuple[str, ...] = ()
    selected_skill_names: tuple[str, ...] = ()
    skill_warnings: tuple[str, ...] = ()

    def diagnostics(self) -> dict:
        by_trigger: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for section in self.sections:
            by_trigger[section.trigger] = by_trigger.get(section.trigger, 0) + 1
            by_tier[section.tier] = by_tier.get(section.tier, 0) + 1
        return {
            "cache_key": self.cache_key,
            "system_chars": len(self.system or ""),
            "history_chars": len(self.history or ""),
            "runtime_chars": len(self.runtime or ""),
            "skills_context_chars": len(self.skills_context or ""),
            "section_count": len(self.sections),
            "sections_by_trigger": by_trigger,
            "sections_by_tier": by_tier,
            "tool_namespaces": list(self.tool_namespaces),
            "tool_profile": self.tool_profile,
            "selected_skill_names": list(self.selected_skill_names),
            "skill_instruction_count": len(self.skill_instructions),
            "skill_warnings": list(self.skill_warnings),
            "sections": [
                {
                    "name": section.name,
                    "trigger": section.trigger,
                    "tier": section.tier,
                    "chars": section.chars,
                    "source": section.source,
                }
                for section in self.sections
            ],
        }


# Baseline namespaces always loaded for the node-first, task-driven creation path.
_BASELINE_NS = [
    "project",
    "interaction",
    "skills",
    "node",
    "canvas",
    "task",
    "agent",
    "tool",
    "vision",
]
_WORKFLOW_BUILD_NS = ["project", "interaction", "skills", "workflow"]
_DEFAULT_TOOL_PROFILE = "default"
_WORKFLOW_BUILD_TOOL_PROFILE = "workflow_build"
_WORKFLOW_BUILD_SUPPRESSED_SECTIONS = {
    "working_loop",
    "task_loop",
    "core_rules",
    "delete_rule",
    "memory_write",
    "attachment_rule",
}
_PLAN_SUPPRESSED_SECTIONS = {
    "working_loop",
    "task_loop",
    "core_rules",
    "delete_rule",
    "memory_write",
    "attachment_rule",
}
_SUPPORTED_TRIGGERS = {"always", "factory", "plan_mode", "workflow_build_mode", "attachments"}
_RUNTIME_STATE_CACHE_KEYS = (
    "metadata",
)


def _skill_catalog_revision() -> str:
    from app.mcp_tools.skill_tools import skill_catalog_revision

    return skill_catalog_revision()


def _explicit_skill_selection_signature(
    user_message: str, attachments: list[dict]
) -> str:
    from app.mcp_tools.skill_tools import explicit_skill_selection_signature

    return explicit_skill_selection_signature(user_message, attachments)


def _cache_signature(value: object) -> dict[str, object]:
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
        )
    except TypeError:
        text = str(value)
    if not text:
        return {}
    return {
        "len": len(text),
        "sha1": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
    }


def _runtime_state_signature(state: dict) -> dict[str, object]:
    payload: dict[str, object] = {}
    metadata = state.get("metadata")
    if isinstance(metadata, dict):
        payload["metadata"] = {"title": metadata.get("title")}
    for key in _RUNTIME_STATE_CACHE_KEYS:
        if key == "metadata":
            continue
        if key in state:
            payload[key] = state.get(key)
    return _cache_signature(payload)


def trigger_matches(trigger: str, ctx: PromptContext) -> bool:
    """Check if a prompt section should load; never inspect user text."""
    if trigger == "always":
        return True
    if trigger == "factory":
        return False
    if trigger == "plan_mode":
        return ctx.collaboration_mode == "plan"
    if trigger == "workflow_build_mode":
        return ctx.collaboration_mode == "workflow_build"
    if trigger == "attachments":
        return bool(ctx.attachments)
    return False


def select_tool_namespaces(ctx: PromptContext) -> list[str]:
    """Return a stable namespace hint list without parsing user text."""
    if ctx.collaboration_mode == "workflow_build":
        return list(_WORKFLOW_BUILD_NS)
    return list(_BASELINE_NS)


def select_tool_profile(ctx: PromptContext) -> str:
    """Return the stable tool profile for the current collaboration mode."""
    if ctx.collaboration_mode == "workflow_build":
        return _WORKFLOW_BUILD_TOOL_PROFILE
    return _DEFAULT_TOOL_PROFILE


# ────────────────────────────────────────────────────────────────────────────
# 组装 + 缓存
# ────────────────────────────────────────────────────────────────────────────

_CACHE_LIMIT = 64


def assemble_split_result(ctx: PromptContext) -> PromptAssemblyResult:
    """分层拼装并返回可观测诊断信息。"""
    s_blocks: list[str] = []
    h_blocks: list[str] = []
    runtime_text = ""
    stats: list[PromptSectionStat] = []
    from app.mcp_tools.skill_tools import (
        build_explicit_skill_injections,
        render_available_skills_context,
    )

    explicit_skills = build_explicit_skill_injections(ctx.user_message, ctx.attachments)
    skills_context = render_available_skills_context()

    for sec in prompts_pkg.all_sections():
        if sec.trigger == "factory":
            continue
        if (
            ctx.collaboration_mode == "workflow_build"
            and sec.name in _WORKFLOW_BUILD_SUPPRESSED_SECTIONS
        ):
            continue
        if (
            ctx.collaboration_mode == "plan"
            and sec.name in _PLAN_SUPPRESSED_SECTIONS
        ):
            continue
        if not trigger_matches(sec.trigger, ctx):
            continue
        if not sec.prompt:
            continue
        text = sec.prompt.rstrip()
        if sec.tier == "s":
            s_blocks.append(text)
        else:  # 'h' 或 'od' 都进 history
            h_blocks.append(text)
        stats.append(
            PromptSectionStat(
            name=sec.name,
            trigger=sec.trigger,
            tier=sec.tier,
            chars=len(text),
            )
        )

    # factory section 始终进 system(实时数据,每次必更)
    namespaces = tuple(select_tool_namespaces(ctx))
    tool_profile = select_tool_profile(ctx)
    rt_sec = prompts_pkg.get("runtime_context")
    if rt_sec and rt_sec.build:
        text = rt_sec.build(
            state=ctx.state,
            model_configs=ctx.model_configs,
            user_facts=ctx.user_facts,
            project_facts=ctx.project_facts,
            latest_user_message=ctx.user_message,
        )
        if text:
            runtime_text = text.rstrip()
            stats.append(
                PromptSectionStat(
                name=rt_sec.name,
                trigger=rt_sec.trigger,
                tier=rt_sec.tier,
                chars=len(runtime_text),
                source="factory",
                )
            )

    sep = "\n\n---\n\n"
    return PromptAssemblyResult(
        system=sep.join(s_blocks),
        history=sep.join(h_blocks),
        runtime=runtime_text,
        sections=tuple(stats),
        tool_namespaces=namespaces,
        tool_profile=tool_profile,
        cache_key=ctx.cache_key(),
        skills_context=skills_context,
        skill_instructions=explicit_skills["instructions"],
        selected_skill_names=explicit_skills["selected_names"],
        skill_warnings=explicit_skills["warnings"],
    )


_split_cache: dict[str, PromptAssemblyResult] = {}


def get_split_prompt_result(ctx: PromptContext) -> PromptAssemblyResult:
    """返回 prompt 组装结果和诊断元数据,带 cache。"""
    key = "split:" + ctx.cache_key()
    cached = _split_cache.get(key)
    if cached is not None:
        return cached

    if len(_split_cache) >= _CACHE_LIMIT:
        _split_cache.clear()

    result = assemble_split_result(ctx)
    _split_cache[key] = result
    return result


# ────────────────────────────────────────────────────────────────────────────
# 状态衍生
# ────────────────────────────────────────────────────────────────────────────


def derive_status_flags(state: dict) -> dict[str, bool]:
    return {
        "has_script": bool(state.get("episodes")),
        "has_characters": bool(state.get("characters")),
        "has_scenes": bool(state.get("scenes")),
        "has_segments": bool(state.get("segments")),
    }
