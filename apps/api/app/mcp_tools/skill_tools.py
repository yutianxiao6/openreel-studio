"""Codex-compatible Skill catalog, explicit selection, and resource tools.

OpenReel owns these packages, so they are exposed to the model as Codex
``orchestrator resource`` Skills. Discovery is metadata-first: the prompt
contains every implicitly invokable Skill's name, description, and locator.
The model then resolves exact handles with ``skills.list`` and reads resources
with ``skills.read``. There is intentionally no semantic Skill search tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from pathlib import Path
from typing import Any

from app.agent.model_context.policy import DOCUMENT_OUTPUT_POLICY, LARGE_COLLECTION_OUTPUT_POLICY
from app.agent.model_context.types import ToolOutput
from app.config import settings
from app.mcp_tools.registry import register
from app.skills.loader import (
    SKILL_FILENAME,
    SkillFormatError,
    catalog_revision,
    discover_skill_packages,
    resolve_skill_resource,
)

_BUILTIN_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
_ORCHESTRATOR_AUTHORITY = {"kind": "orchestrator"}
_MAX_HANDLE_BYTES = 2_048
_MAX_SKILLS_PER_PAGE = 20
_MAX_READ_CONTENT_BYTES = 24_000
_MAX_SKILL_RESOURCE_CONTENT_BYTES = 1024 * 1024
_MAX_CATALOG_DESCRIPTION_CHARS = 1_024
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

_LIST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "authority": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"kind": {"const": "orchestrator"}},
                    "required": ["kind"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"kind": {"const": "executor"}},
                    "required": ["kind"],
                },
            ],
        },
        "cursor": {"type": "string"},
    },
    "required": ["authority"],
}

_READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "authority": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"kind": {"const": "orchestrator"}},
                    "required": ["kind"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"const": "executor"},
                        "id": {"type": "string"},
                    },
                    "required": ["kind", "id"],
                },
            ],
        },
        "package": {"type": "string"},
        "resource": {"type": "string"},
        "cursor": {"type": "string"},
    },
    "required": ["authority", "package", "resource"],
}


def _user_skills_root() -> Path:
    return Path(os.environ.get("OPENREEL_SKILLS_DIR") or Path(settings.PROJECT_ROOT) / "skills")


def _skill_roots() -> list[Path]:
    return [_user_skills_root(), _BUILTIN_SKILLS_ROOT]


def skill_catalog_revision() -> str:
    """Return the revision used to invalidate prompt and sub-agent caches."""
    return catalog_revision(_skill_roots())


def _relative_package_path(root: Path, skill_dir: Path) -> str:
    try:
        relative = skill_dir.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        relative = Path(skill_dir.name)
    return relative.as_posix().strip("/") or skill_dir.name


def _build_unified_index_with_errors() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    roots = (
        (_user_skills_root(), "user", "user_custom", 0),
        (_BUILTIN_SKILLS_ROOT, "builtin", "builtin_default", 10),
    )
    for root, scope, source_root, priority in roots:
        packages, package_errors = discover_skill_packages(
            root,
            scope=scope,
            source_root=source_root,
            priority=priority,
        )
        errors.extend(package_errors)
        for item in packages:
            package = f"{scope}/{_relative_package_path(root, Path(str(item['skill_dir'])))}"
            locator = f"skill://{package}/{SKILL_FILENAME}"
            item.update(
                {
                    "authority": dict(_ORCHESTRATOR_AUTHORITY),
                    "package": package,
                    "main_resource": SKILL_FILENAME,
                    "locator": locator,
                }
            )
            results.append(item)
    results.sort(
        key=lambda item: (
            int(item.get("priority", 100)),
            str(item.get("name") or ""),
            str(item.get("package") or ""),
        )
    )
    return results, errors


def _build_unified_index() -> list[dict[str, Any]]:
    return _build_unified_index_with_errors()[0]


def _validate_authority(value: Any, *, selector: bool) -> tuple[str, str]:
    if not isinstance(value, dict):
        return "", "authority must be an object"
    allowed = {"kind"} if selector else {"kind", "id"}
    if set(value) - allowed:
        return "", "authority contains unknown fields"
    kind = str(value.get("kind") or "").strip()
    if kind not in {"orchestrator", "executor"}:
        return "", "authority.kind must be orchestrator or executor"
    if not selector and kind == "executor" and not _is_bounded_handle(str(value.get("id") or "")):
        return "", "authority.id must be a bounded non-empty handle"
    if not selector and kind == "orchestrator" and "id" in value:
        return "", "orchestrator authority does not accept id"
    return kind, ""


def _is_bounded_handle(value: str) -> bool:
    text = str(value or "")
    return bool(text) and len(text.encode("utf-8")) <= _MAX_HANDLE_BYTES and not any(
        unicodedata.category(char).startswith("C") for char in text
    )


def _fingerprint(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def _pagination_cursor(value: Any, offset: int) -> str:
    return f"{_fingerprint(value)}:{offset}"


def _parse_cursor(cursor: str | None, value: Any, tool_name: str) -> tuple[int, str]:
    if cursor in (None, ""):
        return 0, ""
    fingerprint, separator, raw_offset = str(cursor).partition(":")
    if not separator:
        return 0, f"{tool_name} cursor is invalid"
    if fingerprint != _fingerprint(value):
        return 0, f"{tool_name} cursor is stale; restart from the first page"
    try:
        offset = int(raw_offset)
    except ValueError:
        return 0, f"{tool_name} cursor is invalid"
    if offset < 0:
        return 0, f"{tool_name} cursor is invalid"
    return offset, ""


def _error(message: str, *, kind: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "error_kind": kind}


def _listed_skill(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "authority": dict(_ORCHESTRATOR_AUTHORITY),
        "package": str(item["package"]),
        "name": str(item["name"]),
        "description": str(item["description"])[:_MAX_CATALOG_DESCRIPTION_CHARS],
        "main_resource": str(item["main_resource"]),
    }


@register(
    "skills.list",
    description=(
        "List skills owned by the requested authority. Returns the exact authority, package, and "
        "main_resource values required by skills.read. Pass next_cursor back as cursor to continue."
    ),
    schema=_LIST_SCHEMA,
    tags=["skills", "read"],
    output_policy=LARGE_COLLECTION_OUTPUT_POLICY,
)
async def skills_list(authority: dict[str, Any], cursor: str | None = None) -> ToolOutput | dict[str, Any]:
    kind, authority_error = _validate_authority(authority, selector=True)
    if authority_error:
        return _error(authority_error, kind="invalid_authority")
    if kind == "executor":
        return ToolOutput(
            value={"skills": [], "warnings": [], "next_cursor": None},
            contains_external_context=False,
        )

    index, scan_errors = _build_unified_index_with_errors()
    skills = [
        _listed_skill(item)
        for item in index
        if item.get("allow_implicit_invocation") is not False
    ]
    start, cursor_error = _parse_cursor(cursor, skills, "skills.list")
    if cursor_error:
        return _error(cursor_error, kind="stale_cursor" if "stale" in cursor_error else "invalid_cursor")
    if start > len(skills):
        return _error("skills.list cursor is invalid", kind="invalid_cursor")
    end = min(len(skills), start + _MAX_SKILLS_PER_PAGE)
    warnings = []
    if start == 0:
        warnings = [f"{item.get('path')}: {item.get('error')}" for item in scan_errors[:10]]
    response = {
        "skills": skills[start:end],
        "warnings": warnings,
        "next_cursor": _pagination_cursor(skills, end) if end < len(skills) else None,
    }
    return ToolOutput(value=response, contains_external_context=True)


def _slice_utf8_page(contents: str, start: int) -> tuple[str, int]:
    encoded = contents.encode("utf-8")
    if start > len(encoded):
        raise ValueError("invalid")
    if start < len(encoded):
        try:
            encoded[:start].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid") from exc
    end = min(len(encoded), start + _MAX_READ_CONTENT_BYTES)
    while end > start:
        try:
            page = encoded[start:end].decode("utf-8")
            return page, end
        except UnicodeDecodeError:
            end -= 1
    return "", start


@register(
    "skills.read",
    description=(
        "Read one page from a skill resource. Pass the exact authority and package from skills.list "
        "or an explicitly selected skill's resource_access metadata, plus its main_resource or a "
        "referenced resource beneath that package. Pass next_cursor back as cursor to continue."
    ),
    schema=_READ_SCHEMA,
    tags=["skills", "read"],
    output_policy=DOCUMENT_OUTPUT_POLICY,
)
async def skills_read(
    authority: dict[str, Any],
    package: str,
    resource: str,
    cursor: str | None = None,
) -> ToolOutput | dict[str, Any]:
    kind, authority_error = _validate_authority(authority, selector=False)
    if authority_error:
        return _error(authority_error, kind="invalid_authority")
    if kind != "orchestrator":
        return _error(
            "skill package is not available from the requested authority",
            kind="package_not_available",
        )
    if not _is_bounded_handle(package):
        return _error("package must be a bounded non-empty handle", kind="invalid_package")
    if not _is_bounded_handle(resource):
        return _error("resource must be a bounded non-empty handle", kind="invalid_resource")

    item = next((candidate for candidate in _build_unified_index() if candidate["package"] == package), None)
    if item is None:
        return _error(
            "skill package is not available from the requested authority",
            kind="package_not_available",
        )
    try:
        target = resolve_skill_resource(Path(str(item["skill_dir"])), resource)
        if target.stat().st_size > _MAX_SKILL_RESOURCE_CONTENT_BYTES:
            return _error("skill resource is too large", kind="resource_too_large")
        contents = target.read_text(encoding="utf-8")
    except SkillFormatError as exc:
        return _error(str(exc), kind="invalid_resource")
    except FileNotFoundError:
        return _error("failed to read skill resource", kind="resource_not_found")
    except UnicodeError:
        return _error("failed to read skill resource", kind="binary_resource")
    except OSError:
        return _error("failed to read skill resource", kind="read_error")

    start, cursor_error = _parse_cursor(cursor, contents, "skills.read")
    if cursor_error:
        return _error(cursor_error, kind="stale_cursor" if "stale" in cursor_error else "invalid_cursor")
    try:
        page, end = _slice_utf8_page(contents, start)
    except ValueError:
        return _error("skills.read cursor is invalid", kind="invalid_cursor")
    resource_name = str(target.relative_to(Path(str(item["skill_dir"])).resolve())).replace("\\", "/")
    response = {
        "resource": resource_name,
        "contents": page,
        "next_cursor": _pagination_cursor(contents, end) if end < len(contents.encode("utf-8")) else None,
    }
    return ToolOutput(value=response, contains_external_context=True)


def _catalog_description(item: dict[str, Any]) -> str:
    interface = item.get("interface") if isinstance(item.get("interface"), dict) else {}
    short = str(interface.get("short_description") or item.get("short_description") or "").strip()
    return (short or str(item.get("description") or "").strip())[:_MAX_CATALOG_DESCRIPTION_CHARS]


def _render_catalog_line(item: dict[str, Any], description_chars: int) -> str:
    description = _catalog_description(item)
    if description_chars < len(description):
        description = description[:description_chars]
    locator = str(item["locator"])
    if description:
        return f"- {item['name']}: {description} (orchestrator resource: {locator})"
    return f"- {item['name']}: (orchestrator resource: {locator})"


def _allocate_catalog_lines(items: list[dict[str, Any]], budget: int) -> tuple[list[str], int]:
    full = [_render_catalog_line(item, len(_catalog_description(item))) for item in items]
    if sum(len(line) + 1 for line in full) <= budget:
        return full, 0

    minimum = [_render_catalog_line(item, 0) for item in items]
    minimum_cost = sum(len(line) + 1 for line in minimum)
    if minimum_cost > budget:
        lines: list[str] = []
        used = 0
        for line in minimum:
            cost = len(line) + 1
            if used + cost > budget:
                continue
            lines.append(line)
            used += cost
        return lines, len(items) - len(lines)

    allocations = [0] * len(items)
    remaining = budget - minimum_cost
    descriptions = [_catalog_description(item) for item in items]
    while remaining > 0:
        changed = False
        for index, description in enumerate(descriptions):
            if allocations[index] >= len(description):
                continue
            current = len(_render_catalog_line(items[index], allocations[index]))
            next_allocation = allocations[index] + 1
            next_cost = len(_render_catalog_line(items[index], next_allocation))
            delta = next_cost - current
            if delta > remaining:
                continue
            allocations[index] = next_allocation
            remaining -= delta
            changed = True
        if not changed:
            break
    return [
        _render_catalog_line(item, allocations[index])
        for index, item in enumerate(items)
    ], 0


def render_available_skills_context(max_chars: int = 1_750) -> str:
    """Render Codex-style name/description/source metadata within a hard budget."""
    index, _ = _build_unified_index_with_errors()
    visible = [item for item in index if item.get("allow_implicit_invocation") is not False]
    if not visible:
        return ""
    header = (
        "## Skills\n"
        "A Skill is a set of instructions provided through a `SKILL.md` source. Entries provide "
        "its name, description, and source locator.\n"
        "### Available skills\n"
    )
    available = max(0, int(max_chars) - len(header))
    lines, omitted = _allocate_catalog_lines(visible, available)
    rendered = header + "\n".join(lines)
    if omitted:
        notice = f"\nExceeded skills context budget. {omitted} additional skill(s) were not included."
        if len(rendered) + len(notice) <= max_chars:
            rendered += notice
    return rendered[:max_chars]


def _is_mention_name_char(char: str) -> bool:
    return len(char) == 1 and char.isascii() and (char.isalnum() or char in "_-:")


def _parse_linked_skill_mention(text: str, start: int) -> tuple[str, str, int] | None:
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
    return (text[name_start:name_end], path, path_end + 1) if path else None


def extract_explicit_skill_mentions(
    message: str, attachments: list[dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    """Collect structured path selections first, then Codex ``$`` text mentions."""
    mentions: list[dict[str, str]] = []
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

    text = str(message or "")
    offset = 0
    while offset < len(text):
        linked = _parse_linked_skill_mention(text, offset)
        if linked is not None:
            name, path, next_offset = linked
            if name.upper() not in _COMMON_ENV_VAR_MENTIONS:
                mentions.append({"name": name, "path": path, "kind": "linked"})
            offset = next_offset
            continue
        if text[offset] != "$":
            offset += 1
            continue
        name_start = offset + 1
        if name_start >= len(text) or not _is_mention_name_char(text[name_start]):
            offset += 1
            continue
        name_end = name_start + 1
        while name_end < len(text) and _is_mention_name_char(text[name_end]):
            name_end += 1
        name = text[name_start:name_end]
        if name.upper() not in _COMMON_ENV_VAR_MENTIONS:
            mentions.append({"name": name, "path": "", "kind": "name"})
        offset = name_end

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for mention in mentions:
        key = (mention["name"], mention["path"], mention["kind"])
        if key not in seen:
            seen.add(key)
            deduped.append(mention)
    return deduped


def explicit_skill_selection_signature(
    message: str, attachments: list[dict[str, Any]] | None = None
) -> str:
    mentions = extract_explicit_skill_mentions(message, attachments)
    if not mentions:
        return ""
    raw = repr([(item["name"], item["path"], item["kind"]) for item in mentions])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _normalize_locator(value: str) -> str:
    locator = str(value or "").strip().replace("\\", "/")
    while locator.startswith("./"):
        locator = locator[2:]
    return locator.rstrip("/")


def _locator_keys(item: dict[str, Any]) -> set[str]:
    path = str(item.get("path") or "")
    keys = {
        _normalize_locator(str(item.get("locator") or "")),
        _normalize_locator(path),
        _normalize_locator(str(item.get("package") or "")),
        _normalize_locator(f"{item.get('package')}/{SKILL_FILENAME}"),
    }
    try:
        keys.add(_normalize_locator(str(Path(path).resolve())))
    except OSError:
        pass
    return {key for key in keys if key}


def _resolve_explicit_skill(
    mention: dict[str, str], index: list[dict[str, Any]]
) -> dict[str, Any] | None:
    path = _normalize_locator(mention.get("path", ""))
    if path:
        return next((item for item in index if path in _locator_keys(item)), None)
    if mention.get("kind") == "structured":
        return None
    name = str(mention.get("name") or "")
    candidates = [item for item in index if item.get("name") == name]
    return candidates[0] if len(candidates) == 1 else None


def _take_utf8_bytes(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def build_explicit_skill_injections(
    message: str, attachments: list[dict[str, Any]] | None = None
) -> dict[str, tuple[str, ...]]:
    """Resolve current-turn explicit references and build Codex ``<skill>`` fragments."""
    mentions = extract_explicit_skill_mentions(message, attachments)
    if not mentions:
        return {"instructions": (), "selected_names": (), "warnings": ()}
    index = _build_unified_index()
    selected: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    blocked_plain_names = {
        str(mention.get("name") or "")
        for mention in mentions
        if mention.get("kind") == "structured" and mention.get("name")
    }
    for mention in mentions:
        if mention.get("kind") == "name" and mention.get("name") in blocked_plain_names:
            continue
        item = _resolve_explicit_skill(mention, index)
        if item is None:
            continue
        name = str(item["name"])
        path = str(item["path"])
        if name in seen_names or path in seen_paths:
            continue
        seen_names.add(name)
        seen_paths.add(path)
        selected.append(item)

    instructions: list[str] = []
    selected_names: list[str] = []
    warnings: list[str] = []
    for item in selected:
        name = str(item["name"])
        try:
            raw = Path(str(item["path"])).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            warnings.append(f"Failed to load skill {name} at {item['locator']}: {exc}")
            continue
        contents, truncated = _take_utf8_bytes(raw, MAX_EXPLICIT_SKILL_PROMPT_BYTES)
        instructions.append(
            f"<skill>\n<name>{name}</name>\n<path>{item['locator']}</path>\n{contents}\n</skill>"
        )
        selected_names.append(name)
        if truncated:
            warnings.append(
                f"Skill `{name}` exceeded the {MAX_EXPLICIT_SKILL_PROMPT_BYTES}-byte explicit "
                "prompt limit. Use its locator's exact package with `skills.read` before relying "
                "on omitted instructions."
            )
    return {
        "instructions": tuple(instructions),
        "selected_names": tuple(selected_names),
        "warnings": tuple(warnings),
    }
