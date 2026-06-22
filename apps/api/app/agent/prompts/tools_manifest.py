"""工厂 section:按 namespace 输出工具清单。

[已废弃] LLM 通过 tools 字段已能完整看到工具表,这里再列一遍纯属冗余。
保留空 build() 是为了不破坏 prompt_assembler 对 factory section 的查找。
"""
from __future__ import annotations

NAME = "tools_manifest"
TRIGGER = "factory"
ORDER = 800


def build(namespaces: list[str] | None = None, **_: object) -> str:
    return ""
