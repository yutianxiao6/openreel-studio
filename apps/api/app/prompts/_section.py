"""Worker prompt section — 共享数据类 + 自动发现.

仿 app/agent/prompts 的设计。每个 .py 文件声明:
  NAME    : str            # 工具名,如 "drama.generate_storyboard"
  ORDER   : int            # 同名只允许一个;ORDER 仅做未来扩展占位
  PROMPT  : str            # 静态文本(可选,build 不存在时使用)
  build() : Callable[[WorkerContext], str]   # 工厂函数(可选,优先于 PROMPT)

可同一个文件被多个 NAME 共享(用 ALIASES = ["...", "..."] 列出)。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class WorkerContext:
    """Worker LLM 调用前可用的运行时上下文。

    drama_tools.py 的每个生成工具在调 resolve_prompt 时把当前 segment / shot
    的真实参数传进来,build() 函数可据此生成更精确的 prompt。
    """
    project_id: Optional[str] = None
    node_id: Optional[str] = None
    episode_number: Optional[int] = None
    segment_index: Optional[int] = None
    workflow_mode: Optional[str] = None  # grid | frames | story_template
    grid: Optional[str] = None  # 2*2 | 2*3 | 3*3
    duration_seconds: Optional[int] = None
    resolution: Optional[str] = None
    aspect_ratio: Optional[str] = None
    quality: Optional[str] = None
    model: Optional[str] = None
    extras: dict = field(default_factory=dict)


@dataclass
class WorkerSection:
    name: str
    order: int = 500
    prompt: str | None = None
    build: Callable[[WorkerContext], str] | None = None


_sections: dict[str, WorkerSection] = {}


def _discover() -> None:
    pkg = __name__.rsplit(".", 1)[0]  # app.prompts
    pkg_mod = importlib.import_module(pkg)
    for mod_info in pkgutil.iter_modules(pkg_mod.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{pkg}.{mod_info.name}")
        except Exception:
            continue
        name = getattr(mod, "NAME", None)
        aliases = getattr(mod, "ALIASES", None) or []
        if not name and not aliases:
            continue
        section = WorkerSection(
            name=name or aliases[0],
            order=getattr(mod, "ORDER", 500),
            prompt=getattr(mod, "PROMPT", None),
            build=getattr(mod, "build", None),
        )
        if name:
            _sections[name] = section
        for a in aliases:
            _sections[a] = section


_discover()


def get_section(tool_name: str) -> Optional[WorkerSection]:
    return _sections.get(tool_name)


def render(tool_name: str, ctx: WorkerContext) -> str:
    sec = _sections.get(tool_name)
    if not sec:
        return ""
    if sec.build is not None:
        try:
            return sec.build(ctx) or ""
        except Exception:
            return sec.prompt or ""
    return sec.prompt or ""


def all_tool_names() -> list[str]:
    return sorted(_sections.keys())


__all__ = [
    "WorkerContext",
    "WorkerSection",
    "get_section",
    "render",
    "all_tool_names",
]
