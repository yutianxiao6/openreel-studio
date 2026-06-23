"""Skill registry — unified on-demand skill loading.

Two skill sources:
  apps/api/app/skills/<name>/  — Python packages (SKILL.md + __init__.py)
  skills/                       — Markdown files (workflows/ + prompts/)

skill.search → returns matching names + descriptions (lightweight)
skill.get    → returns full content (on-demand, model decides when to read)
"""
from __future__ import annotations

import ast
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
from app.mcp_tools.registry import register

logger = logging.getLogger(__name__)

_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _md_skills_root() -> Path:
    root = Path(os.environ.get("OPENREEL_SKILLS_DIR") or Path(settings.PROJECT_ROOT) / "skills")
    for child in (root, root / "workflows", root / "prompts", root / "review"):
        child.mkdir(parents=True, exist_ok=True)
    return root


# What a skill is ALLOWED to import. Everything else triggers a refusal.
_ALLOWED_IMPORT_PREFIXES = {
    # stdlib (safe subset)
    "__future__",
    "json", "re", "math", "random", "datetime", "typing", "dataclasses",
    "collections", "itertools", "functools", "asyncio", "uuid",
    # project — go through registry, never touch the DB directly
    "app.mcp_tools.registry",
    "app.prompts",
}

# Hard-banned names. Even an allowed module can't reach these via attribute
# access in the source.
_BANNED_NAMES = {
    "eval", "exec", "compile", "__import__",
    "open",            # file ops must go through file.* tools
    "globals", "locals", "vars", "input",
    "subprocess", "os", "sys", "shutil", "pathlib",
    "socket", "ctypes", "importlib",
}

_VALID_NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


# ── Safety scan ─────────────────────────────────────────────────────────


class SkillSafetyError(ValueError):
    pass


def _check_import(node: ast.AST) -> list[str]:
    """Return a list of violations from one Import/ImportFrom node."""
    violations: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            mod = alias.name.split(".", 1)[0]
            full = alias.name
            if not _is_allowed_import(full):
                violations.append(f"import {full!r} is not allowed")
    elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if not _is_allowed_import(mod):
            violations.append(f"from {mod!r} import ... is not allowed")
    return violations


def _is_allowed_import(dotted: str) -> bool:
    if not dotted:
        return False
    if dotted in _ALLOWED_IMPORT_PREFIXES:
        return True
    head = dotted.split(".", 1)[0]
    if head in _ALLOWED_IMPORT_PREFIXES:
        return True
    for prefix in _ALLOWED_IMPORT_PREFIXES:
        if dotted == prefix or dotted.startswith(prefix + "."):
            return True
    return False


def ast_safety_scan(source: str) -> list[str]:
    """Return a list of human-readable violations. Empty list = safe to load."""
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            violations.extend(_check_import(node))
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            violations.append(f"use of banned name {node.id!r}")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in _BANNED_NAMES:
                violations.append(f"access to banned module {node.value.id!r}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BANNED_NAMES:
                violations.append(f"call to banned function {func.id!r}")
    return violations


# ── Tools ───────────────────────────────────────────────────────────────


async def skill_list() -> list[dict]:
    """List every skill on disk and whether its tools are loaded."""
    from app.mcp_tools.registry import parse_skill_md, registry

    if not _SKILLS_ROOT.exists():
        return []

    out: list[dict] = []
    for child in sorted(_SKILLS_ROOT.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        skill_md = child / "SKILL.md"
        meta: dict[str, Any] = {}
        if skill_md.exists():
            meta = parse_skill_md(skill_md.read_text(encoding="utf-8"))
        tool_name = meta.get("tool_name") or f"skill.{child.name}"
        loaded = registry.get(tool_name) is not None
        out.append({
            "name": child.name,
            "tool_name": tool_name,
            "description": meta.get("description", ""),
            "when_to_use": meta.get("when_to_use", ""),
            "tags": meta.get("tags", []),
            "loaded": loaded,
        })
    return out


async def skill_get(name: str) -> dict:
    """Return full SKILL.md + Python source of one skill."""
    from app.mcp_tools.registry import parse_skill_md

    sdir = _SKILLS_ROOT / name
    if not sdir.is_dir():
        return {"error": f"Skill not found: {name}"}
    skill_md = sdir / "SKILL.md"
    init_py = sdir / "__init__.py"
    return {
        "name": name,
        "skill_md": skill_md.read_text(encoding="utf-8") if skill_md.exists() else "",
        "source": init_py.read_text(encoding="utf-8") if init_py.exists() else "",
        "metadata": parse_skill_md(skill_md.read_text(encoding="utf-8")) if skill_md.exists() else {},
    }


async def skill_load_content(name: str) -> dict:
    """Layer 2 on-demand loading: return the SKILL.md body wrapped for injection.

    Use this when you need the full instructions for a skill. The body is
    returned in <skill> tags so it can be injected into the conversation
    context without polluting the system prompt.
    """
    from app.mcp_tools.registry import parse_skill_md

    sdir = _SKILLS_ROOT / name
    if not sdir.is_dir():
        return {"error": f"Skill not found: {name}"}
    skill_md = sdir / "SKILL.md"
    if not skill_md.exists():
        return {"error": f"No SKILL.md for skill: {name}"}

    meta = parse_skill_md(skill_md.read_text(encoding="utf-8"))
    body = meta.get("_body", "")
    description = meta.get("description", "")

    return {
        "name": name,
        "description": description,
        "content": f"<skill name=\"{name}\">\n{body}\n</skill>",
    }


async def skill_create(
    name: str,
    description: str,
    when_to_use: str,
    source: str,
    tool_name: str | None = None,
    tags: list[str] | None = None,
    body: str = "",
) -> dict:
    """Create a new skill on disk and hot-reload it.

    `source` is the Python contents of the new skill's __init__.py — must
    pass the AST safety scan (no os/subprocess/eval/etc., only whitelisted
    imports). `body` is the prose part of SKILL.md (everything after the
    frontmatter); the agent should put trigger examples and reasoning
    notes there.
    """
    from app.mcp_tools.registry import registry, reload_skills

    if not _VALID_NAME.match(name):
        return {"error": f"Invalid skill name {name!r}: must match [a-z][a-z0-9_]+"}

    tool_name = tool_name or f"skill.{name}"
    if not tool_name.startswith("skill."):
        return {"error": "tool_name must start with 'skill.'"}

    sdir = _SKILLS_ROOT / name
    if sdir.exists():
        return {"error": f"Skill {name!r} already exists; delete it first or pick a new name"}

    violations = ast_safety_scan(source)
    if violations:
        return {"error": "Skill source failed safety scan", "violations": violations}

    _SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
    pkg_init = _SKILLS_ROOT / "__init__.py"
    if not pkg_init.exists():
        pkg_init.write_text("", encoding="utf-8")

    sdir.mkdir(parents=True)

    frontmatter_lines = [
        "---",
        f"name: {name}",
        f"tool_name: {tool_name}",
        f"description: {description}",
        f"when_to_use: {when_to_use}",
    ]
    if tags:
        frontmatter_lines.append("tags: [" + ", ".join(tags) + "]")
    frontmatter_lines.append(f"created_at: {datetime.utcnow().isoformat()}Z")
    frontmatter_lines.append("source: skill")
    frontmatter_lines.append("---")
    skill_md = "\n".join(frontmatter_lines) + "\n\n" + (body or description) + "\n"
    (sdir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (sdir / "__init__.py").write_text(source, encoding="utf-8")

    loaded = reload_skills()
    return {
        "name": name,
        "tool_name": tool_name,
        "path": str(sdir),
        "loaded_modules": loaded,
        "registered": registry.get(tool_name) is not None,
    }


async def skill_delete(name: str) -> dict:
    """Remove a skill directory and unregister its tools."""
    from app.mcp_tools.registry import parse_skill_md, registry

    sdir = _SKILLS_ROOT / name
    if not sdir.is_dir():
        return {"error": f"Skill not found: {name}"}

    skill_md = sdir / "SKILL.md"
    tool_names: list[str] = []
    if skill_md.exists():
        meta = parse_skill_md(skill_md.read_text(encoding="utf-8"))
        tn = meta.get("tool_name")
        if tn:
            tool_names.append(tn)

    for tn in tool_names:
        registry.unregister(tn)

    shutil.rmtree(sdir)
    return {"name": name, "unregistered": tool_names, "removed": True}


async def skill_reload() -> dict:
    """Force a hot reload of every skill from disk."""
    from app.mcp_tools.registry import reload_skills

    loaded = reload_skills()
    return {"loaded": loaded, "count": len(loaded)}


# ── Unified on-demand skill search / get ──────────────────────────────────────
# Scans both apps/api/app/skills/ (Python packages) and skills/ (markdown)


def _parse_frontmatter(raw: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---", raw, re.DOTALL)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _markdown_skill_summary(raw: str) -> str:
    if raw.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", raw, re.DOTALL)
        if match:
            raw = raw[match.end():]
    text = re.sub(r"\s+", " ", raw).strip()
    return text[:240]


def _match_skill_blob(
    blob: str,
    *,
    query: str | None,
    regex: str | list[str] | None,
    pattern: str | list[str] | None,
    case_sensitive: bool,
) -> dict[str, Any]:
    match = match_text(
        blob,
        query=query,
        regex=regex,
        pattern=pattern,
        case_sensitive=case_sensitive,
    )
    if match.get("matched") or not str(query or "").strip():
        match["score"] = 1000 + len(match.get("matched_terms") or []) + len(match.get("matched_patterns") or [])
        return match

    raw_blob = str(blob or "")
    compare_blob = raw_blob if case_sensitive else raw_blob.lower()
    raw_query = str(query or "").strip()
    compare_query = raw_query if case_sensitive else raw_query.lower()
    terms = [term for term in re.split(r"\s+", compare_query) if term]
    matched_terms = [term for term in terms if term in compare_blob]
    if matched_terms:
        return {
            **match,
            "matched": True,
            "mode": "query_partial",
            "matched_terms": matched_terms,
            "score": len(matched_terms),
        }
    match["score"] = 0
    return match


def _build_unified_index() -> list[dict[str, Any]]:
    """Scan both skill sources and return a unified list."""
    from app.mcp_tools.registry import parse_skill_md
    results: list[dict[str, Any]] = []

    # User markdown skills are local policy/knowledge and take precedence over
    # built-in default skills with the same search terms or name.
    md_skills_root = _md_skills_root()
    if md_skills_root.exists():
        for category in ("workflows", "prompts"):
            cat_dir = md_skills_root / category
            if not cat_dir.is_dir():
                continue
            for fpath in sorted(cat_dir.glob("*.md")):
                name = fpath.stem
                raw = ""
                try:
                    raw = fpath.read_text(encoding="utf-8")
                    fm = _parse_frontmatter(raw)
                except Exception:
                    fm = {}
                results.append({
                    "name": name,
                    "category": fm.get("category", category),
                    "description": fm.get("description", ""),
                    "applies_to": fm.get("applies_to", "all"),
                    "source": "markdown",
                    "scope": "user",
                    "priority": 0,
                    "path": str(fpath),
                    "content": raw,
                    "summary": _markdown_skill_summary(raw),
                })

    # Python-package skills (apps/api/app/skills/<name>/SKILL.md)
    if _SKILLS_ROOT.exists():
        for child in sorted(_SKILLS_ROOT.iterdir()):
            if not child.is_dir() or child.name.startswith("_") or child.name == "__pycache__":
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            meta = parse_skill_md(skill_md.read_text(encoding="utf-8"))
            results.append({
                "name": child.name,
                "category": "builtin",
                "description": meta.get("description", ""),
                "applies_to": meta.get("applies_to", "all"),
                "source": "python_package",
                "scope": "builtin",
                "priority": 10,
                "summary": meta.get("when_to_use", ""),
            })

    return results


@register(
    "skill.search",
    description="搜索可用的制作流程或提示词模板。传关键词或 regex，返回名称+描述列表。选中后 skill.get 读全文。",
    tags=["skill", "read"],
)
async def skill_search(
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    index = _build_unified_index()
    results = []
    for skill in index:
        match = _match_skill_blob(
            search_blob(
                skill.get("name"),
                skill.get("category"),
                skill.get("description"),
                skill.get("applies_to"),
                skill.get("summary"),
                skill.get("content"),
            ),
            query=query,
            regex=regex,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )
        if not match.get("matched"):
            continue
        item = {
            "name": skill["name"],
            "category": skill["category"],
            "description": skill["description"],
            "applies_to": skill["applies_to"],
            "scope": skill.get("scope", ""),
            "source": skill.get("source", ""),
            "priority": skill.get("priority", 100),
        }
        if skill.get("summary"):
            item["summary"] = skill["summary"]
        if skill.get("scope") == "user":
            item["usage"] = "本地用户 skill，优先于内置默认指南；调用 skill.get 读取全文。"
        if query or regex or pattern:
            item["match"] = {
                key: value
                for key, value in match.items()
                if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
            }
        item["_score"] = int(match.get("score") or 0)
        results.append(item)
    results.sort(key=lambda item: (int(item.get("priority", 100)), -int(item.get("_score", 0)), str(item.get("name", ""))))
    for item in results:
        item.pop("_score", None)
    return {"ok": True, "skills": results, "total": len(results)}


@register(
    "skill.get",
    description="读取指定 skill 的完整内容。先 skill.search 找名称，再传入。",
    tags=["skill", "read"],
)
async def skill_get_skill(name: str = "") -> dict[str, Any]:
    if not name:
        return {"ok": False, "error": "请提供 skill 名称", "error_kind": "missing_name"}
    index = _build_unified_index()
    match = next((s for s in index if s["name"] == name), None)
    if not match:
        available = sorted(s["name"] for s in index)
        return {"ok": False, "error": f"未找到: {name}", "error_kind": "not_found", "available": available}

    source = match.get("source", "")
    if source == "python_package":
        from app.mcp_tools.registry import parse_skill_md
        sdir = _SKILLS_ROOT / name
        skill_md = sdir / "SKILL.md"
        init_py = sdir / "__init__.py"
        meta = parse_skill_md(skill_md.read_text(encoding="utf-8")) if skill_md.exists() else {}
        return {
            "ok": True, "name": name, "category": match["category"], "description": match["description"],
            "detail": "full",
            "content": meta.get("_body", ""),
        }
    elif match.get("path"):
        try:
            content = Path(match["path"]).read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"读取失败: {exc}", "error_kind": "read_error"}
        return {
            "ok": True, "name": name, "category": match["category"], "description": match["description"],
            "detail": "full",
            "content": content,
        }
    return {"ok": False, "error": "无法读取 skill 内容", "error_kind": "unknown_source"}
