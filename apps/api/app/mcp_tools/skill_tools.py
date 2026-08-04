"""Codex-compatible standard skill package catalog and readers.

Every readable skill is a directory package containing ``SKILL.md``. OpenReel
keeps its workflow/prompt/review category as an optional frontmatter extension.
"""

from __future__ import annotations

import os
import re
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import settings
from app.agent.model_context.policy import COLLECTION_OUTPUT_POLICY, DOCUMENT_OUTPUT_POLICY
from app.mcp_tools.file_tools import TEXT_SOURCE_MAX_BYTES, text_content_window
from app.mcp_tools.query_match import match_text, search_blob
from app.mcp_tools.registry import register
from app.skills.loader import (
    SKILL_FILENAME,
    SkillFormatError,
    catalog_revision,
    discover_skill_packages,
    resolve_skill_resource,
)

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
    "general": "general",
}
_SEARCHABLE_CATEGORIES = {"workflow", "prompt", "review", "general"}
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
SKILL_SEARCH_DEFAULT_LIMIT = 8
SKILL_SEARCH_MAX_LIMIT = 50
MAX_EXPLICIT_SKILL_PROMPT_BYTES = 8_000

_COMMON_ENV_VAR_MENTIONS = {
    "PATH",
    "HOME",
    "USER",
    "SHELL",
    "PWD",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "TERM",
    "XDG_CONFIG_HOME",
}


def _user_skills_root() -> Path:
    return Path(os.environ.get("OPENREEL_SKILLS_DIR") or Path(settings.PROJECT_ROOT) / "skills")


def _normalize_skill_category(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return _CATEGORY_ALIASES.get(raw, raw)


def _skill_is_internal(meta: dict[str, Any]) -> bool:
    source = str(meta.get("declared_source") or meta.get("source") or "").strip().lower()
    tool_name = str(meta.get("tool_name") or "").strip().lower()
    return source == "internal_helper" or tool_name.startswith("internal.")


def _markdown_skill_summary(raw: str) -> str:
    if raw.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", raw, re.DOTALL)
        if match:
            raw = raw[match.end() :]
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
        match["score"] = (
            1000 + len(match.get("matched_terms") or []) + len(match.get("matched_patterns") or [])
        )
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
            "question": str(
                field.get("description") or f"请填写{field.get('label') or field.get('id')}。"
            ),
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
        "required_inputs": [
            str(item) for item in summary.get("required_inputs") or [] if str(item or "").strip()
        ],
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
        source_skill = (
            summary.get("source_skill") if isinstance(summary.get("source_skill"), dict) else {}
        )
        source_skill_name = str(source_skill.get("name") or "").strip()
        if template_id == skill_name:
            matches.append((0, summary))
        elif source_skill_name == skill_name:
            matches.append((1, summary))
    matches.sort(
        key=lambda item: (
            item[0],
            0 if str(item[1].get("scope") or "") == "user" else 1,
            str(item[1].get("name") or ""),
            str(item[1].get("id") or ""),
        )
    )
    if not matches:
        return None
    return _workflow_template_direct_payload(deepcopy(matches[0][1]))


def _skill_search_result_item(
    skill: dict[str, Any], match: dict[str, Any], query: str | None
) -> dict[str, Any]:
    item = {
        "name": skill["name"],
        "category": skill["category"],
        "description": str(skill["description"] or "")[:1_024],
        "applies_to": skill["applies_to"],
        "scope": skill.get("scope", ""),
        "source": skill.get("source", ""),
        "source_root": skill.get("source_root", ""),
        "priority": skill.get("priority", 100),
        "path": skill.get("display_path", ""),
        "allow_implicit_invocation": skill.get("allow_implicit_invocation", True),
    }
    if skill.get("summary"):
        item["summary"] = str(skill["summary"] or "")[:1_024]
    if skill.get("interface"):
        item["interface"] = skill["interface"]
    if skill.get("category") == "review":
        item["recommended_tool"] = "agent.review"
        item["usage"] = (
            "检查类 skill；把 name 作为 review_skill_key 传给 agent.review，附上目标节点或来源引用。"
        )
    elif skill.get("category") == "workflow":
        direct = _direct_workflow_template_for_skill(skill)
        item["recommended_tool"] = "agent.run"
        if direct:
            item["direct_template"] = direct
            item["usage"] = "命中可复用模板摘要；交给 workflow_spec 选择器确认后返回可运行引用。"
        else:
            item["usage"] = "摘要交给 workflow_spec 选择器；它会查找并选择可复用模板。"
    elif skill.get("scope") == "user":
        item["usage"] = "本地用户 skill，优先于同名内置指南；使用前通过 skill.get 读完 SKILL.md。"
    else:
        item["usage"] = "内置 skill；使用前通过 skill.get 读完 SKILL.md。"
    if query:
        item["match"] = {
            key: value
            for key, value in match.items()
            if key in {"mode", "matched_terms", "matched_patterns"}
            and value not in (None, "", [], {})
        }
    item["_score"] = _skill_relevance_score(skill, query) or int(match.get("score") or 0)
    return item


def _dedupe_skill_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get("name") or "")
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
    results.sort(
        key=lambda item: (
            int(item.get("priority", 100)),
            -int(item.get("_score", 0)),
            str(item.get("name", "")),
        )
    )
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


def _skill_roots() -> list[Path]:
    return [_user_skills_root(), _SKILLS_ROOT]


def skill_catalog_revision() -> str:
    """Return the revision used to invalidate the prompt assembly cache."""
    return catalog_revision(_skill_roots())


def _display_skill_path(path: str) -> str:
    target = Path(path)
    try:
        return str(target.resolve().relative_to(Path(settings.PROJECT_ROOT).resolve())).replace(
            "\\", "/"
        )
    except (OSError, ValueError):
        return str(target).replace("\\", "/")


def _build_unified_index_with_errors() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Scan standard skill packages from project and built-in roots."""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    roots = [
        (_user_skills_root(), "user", "user_custom", 0),
        (_SKILLS_ROOT, "builtin", "builtin_default", 10),
    ]
    for root, scope, source_root, priority in roots:
        packages, package_errors = discover_skill_packages(
            root,
            scope=scope,
            source_root=source_root,
            priority=priority,
        )
        errors.extend(package_errors)
        for package in packages:
            if _skill_is_internal(package):
                continue
            category = _normalize_skill_category(package.get("category") or "general")
            if category not in _SEARCHABLE_CATEGORIES:
                category = "general"
            package["category"] = category
            package["applies_to"] = package.get("applies_to") or "all"
            package["summary"] = package.get("when_to_use") or ""
            package["display_path"] = _display_skill_path(str(package.get("path") or ""))
            results.append(package)
    results.sort(
        key=lambda item: (
            int(item.get("priority", 100)),
            str(item.get("name") or ""),
            str(item.get("path") or ""),
        )
    )
    return results, errors


def _build_unified_index() -> list[dict[str, Any]]:
    return _build_unified_index_with_errors()[0]


def _is_mention_name_char(char: str) -> bool:
    return len(char) == 1 and (char.isascii() and (char.isalnum() or char in "_-:"))


def _parse_linked_skill_mention(
    text: str, start: int
) -> tuple[str, str, int] | None:
    if not text.startswith("[$", start):
        return None
    name_start = start + 2
    if name_start >= len(text) or not _is_mention_name_char(text[name_start]):
        return None
    name_end = name_start + 1
    while name_end < len(text) and _is_mention_name_char(text[name_end]):
        name_end += 1
    if name_end >= len(text) or text[name_end] != "]":
        return None
    path_start = name_end + 1
    while path_start < len(text) and text[path_start].isspace():
        path_start += 1
    if path_start >= len(text) or text[path_start] != "(":
        return None
    path_end = text.find(")", path_start + 1)
    if path_end < 0:
        return None
    path = text[path_start + 1 : path_end].strip()
    if not path:
        return None
    return text[name_start:name_end], path, path_end + 1


def extract_explicit_skill_mentions(
    message: str, attachments: list[dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    """Parse only Codex-style explicit syntax, never natural-language intent."""
    text = str(message or "")
    mentions: list[dict[str, str]] = []
    index = 0
    while index < len(text):
        linked = _parse_linked_skill_mention(text, index)
        if linked is not None:
            name, path, next_index = linked
            if name.upper() not in _COMMON_ENV_VAR_MENTIONS:
                mentions.append({"name": name, "path": path, "kind": "linked"})
            index = next_index
            continue
        if text[index] != "$":
            index += 1
            continue
        name_start = index + 1
        if name_start >= len(text) or not _is_mention_name_char(text[name_start]):
            index += 1
            continue
        name_end = name_start + 1
        while name_end < len(text) and _is_mention_name_char(text[name_end]):
            name_end += 1
        name = text[name_start:name_end]
        if name.upper() not in _COMMON_ENV_VAR_MENTIONS:
            mentions.append({"name": name, "path": "", "kind": "name"})
        index = name_end

    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("kind") or attachment.get("type") or "").strip().lower()
        if kind != "skill":
            continue
        name = str(attachment.get("name") or "").strip()
        path = str(attachment.get("path") or attachment.get("source") or "").strip()
        if name or path:
            mentions.append({"name": name, "path": path, "kind": "structured"})

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for mention in mentions:
        key = (mention["name"], mention["path"], mention["kind"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(mention)
    return deduped


def explicit_skill_selection_signature(
    message: str, attachments: list[dict[str, Any]] | None = None
) -> str:
    """Hash explicit selectors so unrelated natural-language turns share prompt cache."""
    mentions = extract_explicit_skill_mentions(message, attachments)
    if not mentions:
        return ""
    raw = repr([(item["name"], item["path"], item["kind"]) for item in mentions])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _normalize_skill_locator(value: str) -> str:
    locator = str(value or "").strip().replace("\\", "/")
    if locator.startswith("skill://"):
        locator = locator[len("skill://") :]
    while locator.startswith("./"):
        locator = locator[2:]
    return locator.rstrip("/")


def _skill_locator_keys(skill: dict[str, Any]) -> set[str]:
    name = str(skill.get("name") or "")
    path = str(skill.get("path") or "")
    display_path = str(skill.get("display_path") or "")
    keys = {
        _normalize_skill_locator(name),
        _normalize_skill_locator(path),
        _normalize_skill_locator(display_path),
        _normalize_skill_locator(f"{name}/{SKILL_FILENAME}"),
    }
    try:
        keys.add(_normalize_skill_locator(str(Path(path).resolve())))
    except OSError:
        pass
    return {key for key in keys if key}


def _resolve_explicit_skill(
    mention: dict[str, str], index: list[dict[str, Any]]
) -> dict[str, Any] | None:
    path = _normalize_skill_locator(mention.get("path", ""))
    if path:
        for skill in index:
            if path in _skill_locator_keys(skill):
                return skill
        return None
    name = str(mention.get("name") or "")
    candidates = [skill for skill in index if skill.get("name") == name]
    candidates.sort(key=lambda item: (int(item.get("priority", 100)), str(item.get("path") or "")))
    return candidates[0] if candidates else None


def _take_utf8_bytes(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def build_explicit_skill_injections(
    message: str, attachments: list[dict[str, Any]] | None = None
) -> dict[str, tuple[str, ...]]:
    """Resolve explicit current-turn mentions and render Codex-style skill fragments."""
    mentions = extract_explicit_skill_mentions(message, attachments)
    if not mentions:
        return {"instructions": (), "selected_names": (), "warnings": ()}

    index, _ = _build_unified_index_with_errors()
    selected: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_paths: set[str] = set()
    for mention in mentions:
        skill = _resolve_explicit_skill(mention, index)
        if skill is None:
            if mention.get("kind") in {"linked", "structured"}:
                label = mention.get("name") or mention.get("path") or "unknown"
                warnings.append(
                    f"Explicitly selected skill `{label}` is unavailable; say so briefly and continue with the best fallback."
                )
            continue
        path = str(skill.get("path") or "")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        selected.append(skill)

    instructions: list[str] = []
    selected_names: list[str] = []
    for skill in selected:
        name = str(skill.get("name") or "")
        path = str(skill.get("path") or "")
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            warnings.append(
                f"Failed to load explicitly selected skill `{name}`: {exc}. Continue with the best fallback."
            )
            continue
        contents, truncated = _take_utf8_bytes(raw, MAX_EXPLICIT_SKILL_PROMPT_BYTES)
        display_path = str(skill.get("display_path") or path)
        instructions.append(
            f"<skill>\n<name>{name}</name>\n<path>{display_path}</path>\n{contents}\n</skill>"
        )
        selected_names.append(name)
        if truncated:
            warnings.append(
                f"Skill `{name}` exceeded the {MAX_EXPLICIT_SKILL_PROMPT_BYTES}-byte explicit prompt limit. "
                "Use `skill.get` pagination before relying on omitted instructions."
            )

    return {
        "instructions": tuple(instructions),
        "selected_names": tuple(selected_names),
        "warnings": tuple(warnings),
    }


def render_available_skills_context(max_chars: int = 1_750) -> str:
    """Render a bounded Codex-style metadata catalog for runtime context."""
    index, errors = _build_unified_index_with_errors()
    visible = [skill for skill in index if skill.get("allow_implicit_invocation") is not False]
    if not visible:
        return ""

    intro = [
        "## Skills",
        "Available skill metadata; bodies are loaded only after a match.",
        "### Available skills",
    ]
    outro = [
        "### How to use skills",
        "- Trigger: If the user names a skill (`$SkillName` or plain text), or the task clearly matches its description, use it for this turn. Multiple mentions mean use all; do not carry skills across turns unless re-mentioned.",
        "- Explicit selections may already appear in `<skill>` blocks. Otherwise announce the minimal matching set, order, and why; explain any skipped obvious match. Then use `skill.get` and read every `content_page` through EOF before acting.",
        "- Read only needed linked resources with `resource` relative to that package. If a named skill is missing or unreadable, say so briefly and continue with the best fallback.",
    ]
    if errors:
        outro.append(
            f"Skipped {len(errors)} invalid skill package(s); `skill.search` returns details."
        )

    def render_line(skill: dict[str, Any], description_limit: int) -> str:
        description = str(skill.get("description") or "")
        if description_limit <= 0:
            description = ""
        elif len(description) > description_limit:
            description = description[: max(0, description_limit - 3)].rstrip() + "..."
        path = str(skill.get("display_path") or skill.get("path") or "")
        detail = f": {description}" if description else ""
        return f"- {skill.get('name')}{detail} (file: {path})"

    for description_limit in (160, 120, 80, 48, 24, 0):
        lines = [render_line(skill, description_limit) for skill in visible]
        rendered = "\n".join([*intro, *lines, *outro])
        if len(rendered) <= max_chars:
            return rendered

    lines: list[str] = []
    omitted = 0
    for skill in visible:
        line = render_line(skill, 0)
        candidate = "\n".join([*intro, *lines, line, *outro])
        if len(candidate) > max_chars:
            omitted += 1
            continue
        lines.append(line)
    if omitted:
        outro.append(f"{omitted} additional skill(s) omitted by the runtime catalog budget.")
    return "\n".join([*intro, *lines, *outro])[:max_chars]


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
    candidates.sort(
        key=lambda item: (int(item.get("priority", 100)), str(item.get("category") or ""))
    )
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
    if category_filter == {"general"}:
        return "general skill 使用标准 SKILL.md 合同；命中后用 skill.get 读取全部正文。"
    return "skill.search 只返回元数据；命中后用 skill.get 读取全部 SKILL.md。"


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
        payload["path"] = skill.get("display_path") or skill.get("path")
    return payload


def _read_index_skill_content(
    skill: dict[str, Any],
    *,
    resource: str = "",
    limit: int | None = None,
    paged: bool = False,
    content_offset: int = 0,
    content_limit: int | None = None,
) -> dict[str, Any]:
    name = str(skill.get("name") or "")
    if not skill.get("path") or not skill.get("skill_dir"):
        return {"ok": False, "error": "无法读取 skill 内容", "error_kind": "unknown_source"}
    target = resolve_skill_resource(Path(str(skill["skill_dir"])), resource or SKILL_FILENAME)
    if target.stat().st_size > TEXT_SOURCE_MAX_BYTES:
        return {
            "ok": False,
            "error": f"skill resource 超过 {TEXT_SOURCE_MAX_BYTES} bytes",
            "error_kind": "resource_too_large",
            "resource": str(resource or SKILL_FILENAME),
        }
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeError:
        return {
            "ok": False,
            "error": "skill resource 不是 UTF-8 文本",
            "error_kind": "binary_resource",
            "resource": str(resource or SKILL_FILENAME),
        }
    if limit is not None:
        content = content[: max(0, int(limit))]
    summary = _skill_summary_value(skill, content)
    resource_path = str(target.relative_to(Path(str(skill["skill_dir"])).resolve())).replace(
        "\\", "/"
    )
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
        "path": skill.get("display_path") or skill.get("path"),
        "resource": resource_path,
    }
    if paged:
        page = text_content_window(content, offset=content_offset, limit=content_limit)
        page["source"] = resource_path
        page["revision"] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        payload["content_page"] = page
    else:
        payload["content"] = content
    if skill.get("category") == "workflow":
        payload["workflow_template_match_hint"] = _workflow_template_match_hint(skill, summary)
        direct = _direct_workflow_template_for_skill(skill)
        if direct:
            payload["direct_template"] = direct
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
    description="按名称和描述搜索标准 skill 元数据，可用 category/scope 缩小范围。",
    tags=["skill", "read"],
    output_policy=COLLECTION_OUTPUT_POLICY,
)
async def skill_search(
    query: str = "",
    queries: list[str] | None = None,
    category: str = "",
    scope: str = "",
    offset: int = 0,
    limit: int = SKILL_SEARCH_DEFAULT_LIMIT,
) -> dict[str, Any]:
    category_filter = _category_filter_set(category)
    if category_filter == set():
        return {
            "ok": False,
            "error": f"未知 skill category: {category}",
            "error_kind": "invalid_skill_category",
            "available_categories": ["workflow", "prompt", "review", "general"],
        }
    if category_filter is None:
        category_filter = set(_SEARCHABLE_CATEGORIES)
    scope_filter = _scope_filter_value(scope)
    if scope_filter is not None and scope_filter not in _SEARCHABLE_SCOPES:
        return {
            "ok": False,
            "error": f"未知 skill scope: {scope}",
            "error_kind": "invalid_skill_scope",
            "available_scopes": ["user", "builtin"],
        }
    index, scan_errors = _build_unified_index_with_errors()

    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or SKILL_SEARCH_DEFAULT_LIMIT), SKILL_SEARCH_MAX_LIMIT))
    query_list = [str(item or "").strip() for item in (queries or []) if str(item or "").strip()]
    if query_list:
        if query and str(query).strip() not in query_list:
            query_list.insert(0, str(query).strip())
        query_list = query_list[:6]
        groups: list[dict[str, Any]] = []
        merged: list[dict[str, Any]] = []
        for one_query in query_list:
            group_results = _search_index_for_category(
                index,
                category_filter=category_filter,
                scope_filter=scope_filter,
                query=one_query,
                regex=None,
                pattern=None,
                case_sensitive=False,
            )
            public_group = []
            for item in group_results[offset : offset + min(limit, 3)]:
                public_item = dict(item)
                public_item.pop("_score", None)
                public_group.append(public_item)
            groups.append(
                {
                    "query": one_query,
                    "skills": public_group,
                    "total": len(group_results),
                    "returned": len(public_group),
                }
            )
            merged.extend(group_results)
        all_results = _dedupe_skill_items(merged)
        total = len(all_results)
        results = all_results[offset : offset + limit]
        for item in results:
            item.pop("_score", None)
        return {
            "ok": True,
            "mode": "multi_query",
            "skills": results,
            "total": total,
            "returned": len(results),
            "offset": offset,
            "next_offset": offset + len(results) if offset + len(results) < total else None,
            "groups": groups,
            "queries": query_list,
            "scope_filter": scope_filter or "",
            "hint": _skill_search_hint_for_category(category_filter),
            "errors": scan_errors,
        }

    results = _search_index_for_category(
        index,
        category_filter=category_filter,
        scope_filter=scope_filter,
        query=query,
        regex=None,
        pattern=None,
        case_sensitive=False,
    )
    if scope_filter is None:
        results = _dedupe_skill_items(results)
    total = len(results)
    results = results[offset : offset + limit]
    for item in results:
        item.pop("_score", None)
    return {
        "ok": True,
        "skills": results,
        "total": total,
        "returned": len(results),
        "offset": offset,
        "next_offset": offset + len(results) if offset + len(results) < total else None,
        "scope_filter": scope_filter or "",
        "hint": _skill_search_hint_for_category(category_filter),
        "errors": scan_errors,
    }


@register(
    "skill.get",
    description="读取标准 SKILL.md 或其相对文本资源；按 content_page.next_offset 续读到 EOF。",
    tags=["skill", "read"],
    output_policy=DOCUMENT_OUTPUT_POLICY,
)
async def skill_get_skill(
    name: str = "",
    category: str = "",
    scope: str = "",
    detail: str = "",
    resource: str = "",
    content_offset: int = 0,
    content_limit: int | None = None,
) -> dict[str, Any]:
    if not name:
        return {"ok": False, "error": "请提供 skill 名称", "error_kind": "missing_name"}
    category_filter = _category_filter_set(category)
    if category_filter == set():
        return {
            "ok": False,
            "error": f"未知 skill category: {category}",
            "error_kind": "invalid_skill_category",
            "available_categories": ["workflow", "prompt", "review", "general"],
        }
    scope_filter = _scope_filter_value(scope)
    if scope_filter is not None and scope_filter not in _SEARCHABLE_SCOPES:
        return {
            "ok": False,
            "error": f"未知 skill scope: {scope}",
            "error_kind": "invalid_skill_scope",
            "available_scopes": ["user", "builtin"],
        }
    match = _find_index_skill(name, category=category, scope=scope)
    if not match:
        available = sorted(s["name"] for s in _build_unified_index())
        return {
            "ok": False,
            "error": f"未找到: {name}",
            "error_kind": "not_found",
            "available": available,
        }
    try:
        detail_norm = str(detail or "").strip().lower()
        if detail_norm == "summary" and not resource:
            return _read_index_skill_summary(match)
        payload = _read_index_skill_content(
            match,
            resource=resource,
            paged=True,
            content_offset=content_offset,
            content_limit=content_limit,
        )
        if payload.get("ok") and payload.get("category") == "review":
            payload["preferred_tool"] = "agent.review"
            payload["usage"] = "reviewer 会按 review_skill_key 隔离加载；主 Agent 只做最终确认。"
        return payload
    except SkillFormatError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "invalid_resource"}
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": f"skill resource 不存在: {exc}",
            "error_kind": "resource_not_found",
        }
    except OSError as exc:
        return {"ok": False, "error": f"读取失败: {exc}", "error_kind": "read_error"}
