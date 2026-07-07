"""Section registry — auto-discovered from .py files in this dir.

每个 section 文件需声明:
  NAME    : str            # 唯一名
  TRIGGER : str            # 加载条件,只支持 always/plan_mode/workflow_build_mode/attachments/factory
  ORDER   : int            # 拼接顺序(小在前)
  TIER    : str            # 分层:'s'(system,每次必发) / 'h'(history,首轮注入)
                           # 默认 'h'(节省 token)。未知 trigger 不会自动注入。
  PROMPT  : str            # 静态文本(TRIGGER != "factory")
  build() : Callable       # 工厂函数(TRIGGER == "factory")

新增 section = 新建一个文件,无需改其他代码。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable


@dataclass
class Section:
    name: str
    trigger: str
    order: int
    tier: str = "h"  # 默认进 history,首轮注入后 cache
    prompt: str | None = None
    build: Callable[..., str] | None = None


_sections: dict[str, Section] = {}


def _discover() -> None:
    """扫本包下所有 .py 文件,收集声明了 NAME 的模块。"""
    pkg = __name__
    for mod_info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        if mod_info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg}.{mod_info.name}")
        name = getattr(mod, "NAME", None)
        if not name:
            continue
        _sections[name] = Section(
            name=name,
            trigger=getattr(mod, "TRIGGER", "always"),
            order=getattr(mod, "ORDER", 500),
            tier=getattr(mod, "TIER", "h"),
            prompt=getattr(mod, "PROMPT", None),
            build=getattr(mod, "build", None),
        )


_discover()


def all_sections() -> list[Section]:
    return sorted(_sections.values(), key=lambda s: s.order)


def get(name: str) -> Section | None:
    return _sections.get(name)


def sections_by_trigger(trigger: str) -> list[Section]:
    return [s for s in all_sections() if s.trigger == trigger]
