"""Skill registry — categorized on-demand skill loading.

Two skill sources:
  apps/api/app/skills/<name>/  — built-in default skills (SKILL.md + __init__.py)
  skills/                       — user custom skills grouped by category

skill.search(category=...) → returns matching names + descriptions (lightweight)
skill.get                  → returns content; workflow skills help select templates, review skills prefer agent.review
"""
from __future__ import annotations

import ast
import logging
import os
import re
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob
from app.mcp_tools.registry import register

logger = logging.getLogger(__name__)

_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"

_CATEGORY_ALIASES: dict[str, str] = {
    "flow": "workflow",
    "flows": "workflow",
    "workflow": "workflow",
    "workflows": "workflow",
    "process": "workflow",
    "prompt": "prompt",
    "prompts": "prompt",
    "prompting": "prompt",
    "review": "review",
    "reviews": "review",
    "check": "review",
    "checker": "review",
    "audit": "review",
}
_USER_SKILL_CATEGORY_DIRS: dict[str, str] = {
    "workflow": "workflows",
    "prompt": "prompts",
    "review": "review",
}
_SEARCHABLE_CATEGORIES = {"workflow", "prompt", "review"}
_SCOPE_ALIASES: dict[str, str] = {
    "user": "user",
    "custom": "user",
    "local": "user",
    "project": "user",
    "builtin": "builtin",
    "built_in": "builtin",
    "default": "builtin",
    "system": "builtin",
}
_SEARCHABLE_SCOPES = {"user", "builtin"}


def _md_skills_root() -> Path:
    root = Path(os.environ.get("OPENREEL_SKILLS_DIR") or Path(settings.PROJECT_ROOT) / "skills")
    for child in (root, root / "workflows", root / "prompts", root / "review"):
        try:
            child.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Unable to initialize user skill directory %s: %s", child, exc)
    return root


def _normalize_skill_category(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return _CATEGORY_ALIASES.get(raw, raw)


def _skill_is_internal(meta: dict[str, Any]) -> bool:
    source = str(meta.get("source") or "").strip().lower()
    tool_name = str(meta.get("tool_name") or "").strip().lower()
    return source == "internal_helper" or tool_name.startswith("internal.")


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


def _skill_search_blob(skill: dict[str, Any]) -> str:
    applies_to = str(skill.get("applies_to") or "").strip()
    fields = [
        skill.get("name"),
        skill.get("category"),
        skill.get("description"),
        applies_to,
    ]
    if not str(skill.get("description") or "").strip() and applies_to.lower() in {"", "all"}:
        fields.append(skill.get("summary"))
    return search_blob(*fields)


def _skill_relevance_score(skill: dict[str, Any], query: str | None) -> int:
    terms = [term for term in re.split(r"\s+", str(query or "").strip().lower()) if term]
    if not terms:
        return 0
    weighted_fields = [
        (str(skill.get("name") or "").lower(), 100),
        (str(skill.get("applies_to") or "").lower(), 70),
        (str(skill.get("description") or "").lower(), 35),
    ]
    applies_to = str(skill.get("applies_to") or "").strip().lower()
    if not str(skill.get("description") or "").strip() and applies_to in {"", "all"}:
        weighted_fields.append((str(skill.get("summary") or "").lower(), 15))
    score = 0
    for term in terms:
        for text, weight in weighted_fields:
            if term in text:
                score += weight
    compact_query = "".join(terms)
    if compact_query:
        for text, weight in weighted_fields:
            if compact_query in text.replace(" ", ""):
                score += weight * 2
    return score


def _workflow_template_direct_payload(summary: dict[str, Any]) -> dict[str, Any]:
    from app.agent import canvas_workflow_templates

    input_fields = canvas_workflow_templates.template_input_field_summaries(summary)
    missing_inputs = [
        str(field.get("id") or "")
        for field in input_fields
        if field.get("required") and field.get("missing")
    ]
    input_questions = [
        {
            "id": str(field.get("id") or ""),
            "header": str(field.get("label") or field.get("id") or "")[:80],
            "question": str(field.get("description") or f"请填写{field.get('label') or field.get('id')}。"),
        }
        for field in input_fields
        if field.get("required") and field.get("missing") and str(field.get("id") or "").strip()
    ][:6]
    payload = {
        "name": str(summary.get("name") or ""),
        "scope": str(summary.get("scope") or ""),
        "source": str(summary.get("source") or ""),
        "description": str(summary.get("description") or "")[:220],
        "inputs": [str(item) for item in summary.get("inputs") or [] if str(item or "").strip()],
        "required_inputs": [str(item) for item in summary.get("required_inputs") or [] if str(item or "").strip()],
        "missing_inputs": missing_inputs,
        "input_fields": input_fields[:8],
        "input_questions": input_questions,
        "selector": "workflow_spec",
        "next_action": "交给 workflow_spec 选择器确认模板匹配和输入定义后返回可运行模板引用。",
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _direct_workflow_template_for_skill(skill: dict[str, Any]) -> dict[str, Any] | None:
    if skill.get("category") != "workflow":
        return None
    skill_name = str(skill.get("name") or "").strip()
    if not skill_name:
        return None
    try:
        from app.agent import canvas_workflow_templates

        summaries = canvas_workflow_templates.list_template_summaries()
    except Exception:
        return None
    matches: list[tuple[int, dict[str, Any]]] = []
    for summary in summaries:
        template_id = str(summary.get("id") or "").strip()
        source_skill = summary.get("source_skill") if isinstance(summary.get("source_skill"), dict) else {}
        source_skill_name = str(source_skill.get("name") or "").strip()
        if template_id == skill_name:
            matches.append((0, summary))
        elif source_skill_name == skill_name:
            matches.append((1, summary))
    matches.sort(key=lambda item: (
        item[0],
        0 if str(item[1].get("scope") or "") == "user" else 1,
        str(item[1].get("name") or ""),
        str(item[1].get("id") or ""),
    ))
    if not matches:
        return None
    return _workflow_template_direct_payload(deepcopy(matches[0][1]))


def _skill_search_result_item(skill: dict[str, Any], match: dict[str, Any], query: str | None) -> dict[str, Any]:
    item = {
        "name": skill["name"],
        "category": skill["category"],
        "description": skill["description"],
        "applies_to": skill["applies_to"],
        "scope": skill.get("scope", ""),
        "source": skill.get("source", ""),
        "source_root": skill.get("source_root", ""),
        "priority": skill.get("priority", 100),
    }
    if skill.get("summary"):
        item["summary"] = skill["summary"]
    if skill.get("category") == "review":
        item["recommended_tool"] = "agent.review"
        item["usage"] = "检查类 skill；把 name 作为 review_skill_key 传给 agent.review，附上目标节点或来源引用。"
    elif skill.get("category") == "workflow":
        direct = _direct_workflow_template_for_skill(skill)
        item["recommended_tool"] = "agent.run"
        if direct:
            item["direct_template"] = direct
            item["usage"] = "命中可复用模板摘要；交给 workflow_spec 选择器确认后返回可运行引用。"
        else:
            item["usage"] = "摘要交给 workflow_spec 选择器；它会查找并选择可复用模板。"
    elif skill.get("scope") == "user":
        item["usage"] = "本地用户 skill，优先于内置默认指南；graph workflow 的 prompt skill 写入 step prompt_template，standalone 才读取单份正文。"
    else:
        item["usage"] = "内置默认 skill；用户 skill 没有匹配项时作为 fallback。"
    if query:
        item["match"] = {
            key: value
            for key, value in match.items()
            if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
        }
    item["_score"] = _skill_relevance_score(skill, query) or int(match.get("score") or 0)
    return item


def _dedupe_skill_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (
            str(item.get("category") or ""),
            str(item.get("name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _search_index_for_category(
    index: list[dict[str, Any]],
    *,
    category_filter: set[str],
    scope_filter: str | None,
    query: str,
    regex: str | list[str] | None,
    pattern: str | list[str] | None,
    case_sensitive: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for skill in index:
        if skill.get("category") not in category_filter:
            continue
        if scope_filter is not None and skill.get("scope") != scope_filter:
            continue
        match = _match_skill_blob(
            _skill_search_blob(skill),
            query=query,
            regex=regex,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )
        if not match.get("matched"):
            continue
        results.append(_skill_search_result_item(skill, match, query))
    results.sort(key=lambda item: (int(item.get("priority", 100)), -int(item.get("_score", 0)), str(item.get("name", ""))))
    return results


def _category_filter_set(category: str = "", kind: str = "") -> set[str] | None:
    raw = _normalize_skill_category(category or kind)
    if not raw:
        return None
    if raw not in _SEARCHABLE_CATEGORIES:
        return set()
    return {raw}


def _scope_filter_value(scope: str = "") -> str | None:
    raw = str(scope or "").strip().lower().replace("-", "_")
    if not raw:
        return None
    return _SCOPE_ALIASES.get(raw, raw)


def _build_unified_index() -> list[dict[str, Any]]:
    """Scan both skill sources and return a unified list."""
    from app.mcp_tools.registry import parse_skill_md
    results: list[dict[str, Any]] = []

    # User markdown skills are local policy/knowledge and take precedence over
    # built-in default skills with the same search terms or name.
    md_skills_root = _md_skills_root()
    if md_skills_root.exists():
        for dir_category, dirname in _USER_SKILL_CATEGORY_DIRS.items():
            cat_dir = md_skills_root / dirname
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
                category = _normalize_skill_category(fm.get("category") or dir_category)
                if category not in _SEARCHABLE_CATEGORIES:
                    category = dir_category
                results.append({
                    "name": name,
                    "category": category,
                    "dir_category": dir_category,
                    "description": fm.get("description", ""),
                    "applies_to": fm.get("applies_to", "all"),
                    "source": "markdown",
                    "scope": "user",
                    "source_root": "user_custom",
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
            raw = skill_md.read_text(encoding="utf-8")
            meta = parse_skill_md(raw)
            if _skill_is_internal(meta):
                continue
            category = _normalize_skill_category(meta.get("category") or "")
            if category not in _SEARCHABLE_CATEGORIES:
                continue
            results.append({
                "name": child.name,
                "category": category,
                "description": meta.get("description", ""),
                "applies_to": meta.get("applies_to", "all"),
                "source": "python_package",
                "scope": "builtin",
                "source_root": "builtin_default",
                "priority": 10,
                "summary": meta.get("when_to_use", ""),
            })

    return results


def _find_index_skill(
    name: str,
    *,
    category: str = "",
    kind: str = "",
    scope: str = "",
) -> dict[str, Any] | None:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return None
    category_filter = _category_filter_set(category, kind)
    scope_filter = _scope_filter_value(scope)
    if scope_filter is not None and scope_filter not in _SEARCHABLE_SCOPES:
        return None
    candidates = []
    for skill in _build_unified_index():
        if skill.get("name") != normalized_name:
            continue
        if category_filter is not None:
            if skill.get("category") not in category_filter:
                continue
        if scope_filter is not None and skill.get("scope") != scope_filter:
            continue
        candidates.append(skill)
    candidates.sort(key=lambda item: (int(item.get("priority", 100)), str(item.get("category") or "")))
    return candidates[0] if candidates else None


def _skill_summary_value(skill: dict[str, Any], content: str = "") -> str:
    summary = str(skill.get("summary") or "").strip()
    if not summary and content:
        summary = _markdown_skill_summary(content)
    if not summary:
        summary = str(skill.get("description") or "").strip()
    return summary


def _workflow_template_match_hint(skill: dict[str, Any], summary: str) -> dict[str, Any]:
    skill_summary = str(skill.get("description") or summary or "").strip()
    payload: dict[str, Any] = {
        "skill_name": str(skill.get("name") or ""),
        "skill_summary": skill_summary,
        "limit": 5,
        "hint": "交给 workflow_spec 选择器使用；没有 direct_template 时用这些摘要字段查找内置和用户 workflow 模板候选。",
    }
    direct = _direct_workflow_template_for_skill(skill)
    if direct:
        payload["direct_template"] = direct
    return payload


def _skill_search_hint_for_category(category_filter: set[str] | None) -> str:
    if category_filter == {"workflow"}:
        return (
            "workflow skill 返回摘要；默认工作流请求交给 workflow_spec 选择器 "
            "选择现有模板。direct_template 只作候选摘要。"
            "standalone 才读取 prompt 正文。"
        )
    if category_filter == {"prompt"}:
        return "prompt skill 返回提示词写法摘要；workflow 图内使用时写入 step primary_skill 或 prompt_template。"
    if category_filter == {"review"}:
        return "review skill 返回检查标准摘要；正式检查把 name 作为 review_skill_key 传给 agent.review。"
    return "skill 返回摘要；按 category 选择 workflow、prompt 或 review 的后续工具路径。"


def _read_index_skill_summary(skill: dict[str, Any]) -> dict[str, Any]:
    name = str(skill.get("name") or "")
    summary = _skill_summary_value(skill)
    payload: dict[str, Any] = {
        "ok": True,
        "name": name,
        "category": skill.get("category", ""),
        "description": skill.get("description", ""),
        "scope": skill.get("scope", ""),
        "source": skill.get("source", ""),
        "source_root": skill.get("source_root", ""),
        "detail": "summary",
        "summary": summary,
    }
    if skill.get("category") == "workflow":
        payload["workflow_template_match_hint"] = _workflow_template_match_hint(skill, summary)
        direct = _direct_workflow_template_for_skill(skill)
        if direct:
            payload["direct_template"] = direct
        payload["content_available"] = True
    if skill.get("path"):
        payload["path"] = skill.get("path")
    return payload


def _read_index_skill_content(skill: dict[str, Any], *, limit: int | None = None) -> dict[str, Any]:
    name = str(skill.get("name") or "")
    source = skill.get("source", "")
    if source == "python_package":
        from app.mcp_tools.registry import parse_skill_md
        sdir = _SKILLS_ROOT / name
        skill_md = sdir / "SKILL.md"
        meta = parse_skill_md(skill_md.read_text(encoding="utf-8")) if skill_md.exists() else {}
        content = str(meta.get("_body", ""))
    elif skill.get("path"):
        content = Path(skill["path"]).read_text(encoding="utf-8")
    else:
        return {"ok": False, "error": "无法读取 skill 内容", "error_kind": "unknown_source"}
    if limit is not None:
        content = content[: max(0, int(limit))]
    summary = _skill_summary_value(skill, content)
    payload = {
        "ok": True,
        "name": name,
        "category": skill.get("category", ""),
        "description": skill.get("description", ""),
        "scope": skill.get("scope", ""),
        "source": skill.get("source", ""),
        "source_root": skill.get("source_root", ""),
        "detail": "full",
        "summary": summary,
        "content": content,
    }
    if skill.get("category") == "workflow":
        payload["workflow_template_match_hint"] = _workflow_template_match_hint(skill, summary)
        direct = _direct_workflow_template_for_skill(skill)
        if direct:
            payload["direct_template"] = direct
    if skill.get("path"):
        payload["path"] = skill.get("path")
    return payload


def load_review_skill_by_key(key: str) -> dict[str, Any]:
    normalized = str(key or "").strip().lower().replace(" ", "_")
    if not re.match(r"^[a-z0-9][a-z0-9_-]{1,80}$", normalized):
        return {"ok": False, "error": "invalid_review_skill_key", "key": key}
    skill = _find_index_skill(normalized, category="review")
    if not skill:
        return {"ok": False, "error": "review_skill_not_found", "key": normalized}
    payload = _read_index_skill_content(skill, limit=8000)
    if payload.get("ok"):
        payload["key"] = normalized
        payload["chars"] = len(str(payload.get("content") or ""))
    return payload


@register(
    "skill.search",
    description="按 category/scope 搜索 skill 索引；review 类返回 name 后交给 agent.review 使用。",
    tags=["skill", "read"],
)
async def skill_search(
    query: str = "",
    queries: list[str] | None = None,
    category: str = "",
    kind: str = "",
    scope: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    category_filter = _category_filter_set(category, kind)
    if category_filter == set():
        return {
            "ok": False,
            "error": f"未知 skill category: {category or kind}",
            "error_kind": "invalid_skill_category",
            "available_categories": ["workflow", "prompt", "review"],
        }
    scope_filter = _scope_filter_value(scope)
    if scope_filter is not None and scope_filter not in _SEARCHABLE_SCOPES:
        return {
            "ok": False,
            "error": f"未知 skill scope: {scope}",
            "error_kind": "invalid_skill_scope",
            "available_scopes": ["user", "builtin"],
        }
    index = _build_unified_index()
    if category_filter is None:
        matched_by_category: dict[str, list[dict[str, Any]]] = {}
        for skill in index:
            if scope_filter is not None and skill.get("scope") != scope_filter:
                continue
            match = _match_skill_blob(
                _skill_search_blob(skill),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            if not match.get("matched"):
                continue
            skill = dict(skill)
            skill["_rank_score"] = _skill_relevance_score(skill, query)
            cat = str(skill.get("category") or "")
            matched_by_category.setdefault(cat, []).append(skill)
        categories: list[dict[str, Any]] = []
        for cat, skills in sorted(matched_by_category.items()):
            skills.sort(key=lambda item: (int(item.get("priority", 100)), -int(item.get("_rank_score", 0)), str(item.get("name", ""))))
            categories.append({
                "category": cat,
                "count": len(skills),
                "top": [
                    {
                        "name": item.get("name"),
                        "description": item.get("description", ""),
                        "scope": item.get("scope", ""),
                        "source_root": item.get("source_root", ""),
                    }
                    for item in skills[:3]
                ],
            })
        return {
            "ok": True,
            "needs_category": True,
            "skills": [],
            "total": sum(item["count"] for item in categories),
            "categories": categories,
            "hint": (
                "请重新调用 skill.search 并指定 category='workflow'、'prompt' 或 'review'。"
                "review 类检查把 name 作为 review_skill_key 传给 agent.review。"
            ),
            "available_categories": ["workflow", "prompt", "review"],
            "scope_filter": scope_filter or "",
        }

    query_list = [str(item or "").strip() for item in (queries or []) if str(item or "").strip()]
    if query_list:
        if query and str(query).strip() not in query_list:
            query_list.insert(0, str(query).strip())
        query_list = query_list[:12]
        groups: list[dict[str, Any]] = []
        merged: list[dict[str, Any]] = []
        for one_query in query_list:
            group_results = _search_index_for_category(
                index,
                category_filter=category_filter,
                scope_filter=scope_filter,
                query=one_query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            )
            public_group = []
            for item in group_results:
                public_item = dict(item)
                public_item.pop("_score", None)
                public_group.append(public_item)
            groups.append({
                "query": one_query,
                "skills": public_group,
                "total": len(public_group),
            })
            merged.extend(group_results)
        results = _dedupe_skill_items(merged)
        for item in results:
            item.pop("_score", None)
        return {
            "ok": True,
            "mode": "multi_query",
            "skills": results,
            "total": len(results),
            "groups": groups,
            "queries": query_list,
            "scope_filter": scope_filter or "",
            "hint": _skill_search_hint_for_category(category_filter),
        }

    results = _search_index_for_category(
        index,
        category_filter=category_filter,
        scope_filter=scope_filter,
        query=query,
        regex=regex,
        pattern=pattern,
        case_sensitive=case_sensitive,
    )
    if scope_filter is None:
        results = _dedupe_skill_items(results)
    for item in results:
        item.pop("_score", None)
    return {"ok": True, "skills": results, "total": len(results), "scope_filter": scope_filter or ""}


@register(
    "skill.get",
    description="读取 skill 摘要或全文；workflow 默认返回摘要，detail='full' 才返回全文。",
    tags=["skill", "read"],
)
async def skill_get_skill(
    name: str = "",
    category: str = "",
    kind: str = "",
    scope: str = "",
    detail: str = "",
) -> dict[str, Any]:
    if not name:
        return {"ok": False, "error": "请提供 skill 名称", "error_kind": "missing_name"}
    scope_filter = _scope_filter_value(scope)
    if scope_filter is not None and scope_filter not in _SEARCHABLE_SCOPES:
        return {
            "ok": False,
            "error": f"未知 skill scope: {scope}",
            "error_kind": "invalid_skill_scope",
            "available_scopes": ["user", "builtin"],
        }
    match = _find_index_skill(name, category=category, kind=kind, scope=scope)
    if not match:
        available = sorted(s["name"] for s in _build_unified_index())
        return {"ok": False, "error": f"未找到: {name}", "error_kind": "not_found", "available": available}
    try:
        detail_norm = str(detail or "").strip().lower()
        if match.get("category") == "workflow" and detail_norm not in {"full", "content"}:
            return _read_index_skill_summary(match)
        payload = _read_index_skill_content(match)
        if payload.get("ok") and payload.get("category") == "review":
            payload["preferred_tool"] = "agent.review"
            payload["usage"] = "reviewer 会按 review_skill_key 隔离加载；主 Agent 只做最终确认。"
        return payload
    except OSError as exc:
        return {"ok": False, "error": f"读取失败: {exc}", "error_kind": "read_error"}
