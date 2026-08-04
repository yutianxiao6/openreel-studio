"""Codex-compatible filesystem skill package discovery and parsing."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
SKILL_METADATA_PATH = Path("agents") / "openai.yaml"
MAX_SCAN_DEPTH = 6
MAX_SKILL_DIRS = 2_000
MAX_SKILL_PACKAGES = 2_000
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 1_024


class SkillFormatError(ValueError):
    """Raised when a SKILL.md does not satisfy the standard metadata contract."""


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _extract_frontmatter(raw: str) -> str:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError("missing YAML frontmatter delimited by ---")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    raise SkillFormatError("missing closing YAML frontmatter delimiter ---")


def _repair_frontmatter_scalars(frontmatter: str) -> str | None:
    """Quote invalid plain scalars such as ``description: Build for AWS: ECS``."""
    repaired: list[str] = []
    changed = False
    block_indent: int | None = None
    for line in frontmatter.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                repaired.append(line)
                continue
            block_indent = None
        if ":" not in line:
            repaired.append(line)
            continue
        key, value = line.split(":", 1)
        if not key.strip() or (value and not value[0].isspace()):
            repaired.append(line)
            continue
        trimmed_start = value.lstrip()
        leading = value[: len(value) - len(trimmed_start)]
        scalar = trimmed_start
        comment = ""
        for index, char in enumerate(trimmed_start):
            if char == "#" and (index == 0 or trimmed_start[index - 1].isspace()):
                comment_start = len(trimmed_start[:index].rstrip())
                scalar = trimmed_start[:comment_start]
                comment = trimmed_start[comment_start:]
                break
        scalar = scalar.rstrip()
        if not scalar:
            repaired.append(line)
            continue
        if scalar[0] in "|>":
            block_indent = indent
            repaired.append(line)
            continue
        if scalar[0] in "'\"":
            repaired.append(line)
            continue
        has_colon_separator = any(
            char == ":" and index + 1 < len(scalar) and scalar[index + 1].isspace()
            for index, char in enumerate(scalar)
        )
        invalid_flow_like = False
        if scalar[0] in "[{@`":
            try:
                yaml.safe_load(scalar)
            except yaml.YAMLError:
                invalid_flow_like = True
        if not has_colon_separator and not invalid_flow_like:
            repaired.append(line)
            continue
        repaired.append(f"{key}:{leading}'{scalar.replace(chr(39), chr(39) * 2)}'{comment}")
        changed = True
    return "\n".join(repaired) if changed else None


def parse_skill_document(raw: str, *, default_name: str) -> dict[str, Any]:
    """Parse the standard SKILL.md metadata while preserving extension fields."""
    frontmatter = _extract_frontmatter(raw)
    try:
        parsed = yaml.safe_load(frontmatter)
    except yaml.YAMLError as original_error:
        repaired = _repair_frontmatter_scalars(frontmatter)
        if repaired is None:
            raise SkillFormatError(f"invalid YAML: {original_error}") from original_error
        try:
            parsed = yaml.safe_load(repaired)
        except yaml.YAMLError as exc:
            raise SkillFormatError(f"invalid YAML: {original_error}") from exc
    if not isinstance(parsed, dict):
        raise SkillFormatError("YAML frontmatter must be an object")

    name = _single_line(parsed.get("name")) or _single_line(default_name) or "skill"
    description = _single_line(parsed.get("description"))
    if len(name) > MAX_SKILL_NAME_CHARS:
        raise SkillFormatError(f"name exceeds {MAX_SKILL_NAME_CHARS} characters")
    if not description:
        raise SkillFormatError("missing field `description`")
    if len(description) > MAX_SKILL_DESCRIPTION_CHARS:
        raise SkillFormatError(f"description exceeds {MAX_SKILL_DESCRIPTION_CHARS} characters")

    nested_metadata = parsed.get("metadata")
    if not isinstance(nested_metadata, dict):
        nested_metadata = {}
    short_description = _single_line(
        nested_metadata.get("short-description") or nested_metadata.get("short_description")
    )
    return {
        "name": name,
        "description": description,
        "short_description": short_description,
        "category": _single_line(parsed.get("category")),
        "applies_to": _single_line(parsed.get("applies_to")),
        "frontmatter": parsed,
    }


def _load_openai_metadata(skill_dir: Path) -> dict[str, Any]:
    metadata_path = skill_dir / SKILL_METADATA_PATH
    if not metadata_path.is_file():
        return {}
    try:
        parsed = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        logger.warning("Ignoring invalid optional skill metadata %s: %s", metadata_path, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _discover_skill_files(root: Path) -> tuple[list[Path], list[dict[str, str]]]:
    if not root.is_dir():
        return [], []

    discovered: list[Path] = []
    errors: list[dict[str, str]] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    seen_directories: set[Path] = set()
    visited_count = 0

    while pending and len(discovered) < MAX_SKILL_PACKAGES:
        directory, depth = pending.pop()
        try:
            canonical = directory.resolve()
        except OSError as exc:
            errors.append({"path": str(directory), "error": f"resolve failed: {exc}"})
            continue
        if canonical in seen_directories:
            continue
        seen_directories.add(canonical)
        visited_count += 1
        if visited_count > MAX_SKILL_DIRS:
            errors.append(
                {
                    "path": str(root),
                    "error": f"skill scan exceeded {MAX_SKILL_DIRS} directories",
                }
            )
            break

        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            errors.append({"path": str(directory), "error": f"scan failed: {exc}"})
            continue
        for child in children:
            if child.name == SKILL_FILENAME and child.is_file():
                discovered.append(child)
                continue
            if depth >= MAX_SCAN_DEPTH or child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                pending.append((child, depth + 1))

    if pending and len(discovered) >= MAX_SKILL_PACKAGES:
        errors.append(
            {
                "path": str(root),
                "error": f"skill scan exceeded {MAX_SKILL_PACKAGES} packages",
            }
        )

    return sorted(discovered, key=lambda item: str(item)), errors


def discover_skill_packages(
    root: Path,
    *,
    scope: str,
    source_root: str,
    priority: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Discover standard directory packages below one configured skill root."""
    skill_files, errors = _discover_skill_files(root)
    packages: list[dict[str, Any]] = []
    for skill_path in skill_files:
        try:
            raw = skill_path.read_text(encoding="utf-8")
            metadata = parse_skill_document(raw, default_name=skill_path.parent.name)
        except (OSError, UnicodeError, SkillFormatError) as exc:
            errors.append({"path": str(skill_path), "error": str(exc)})
            continue

        openai_metadata = _load_openai_metadata(skill_path.parent)
        policy = openai_metadata.get("policy")
        if not isinstance(policy, dict):
            policy = {}
        interface = openai_metadata.get("interface")
        if not isinstance(interface, dict):
            interface = {}

        packages.append(
            {
                **metadata,
                "path": str(skill_path),
                "skill_dir": str(skill_path.parent),
                "root_path": str(root),
                "scope": scope,
                "source_root": source_root,
                "source": "skill_package",
                "priority": priority,
                "interface": interface,
                "policy": policy,
                "allow_implicit_invocation": policy.get("allow_implicit_invocation") is not False,
            }
        )
    return packages, errors


def catalog_revision(roots: list[Path]) -> str:
    """Return a cheap revision that changes when package metadata changes."""
    digest = hashlib.sha256()
    for root in roots:
        digest.update(str(root).encode("utf-8"))
        skill_files, _ = _discover_skill_files(root)
        for skill_path in skill_files:
            candidates = (skill_path, skill_path.parent / SKILL_METADATA_PATH)
            for candidate in candidates:
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                digest.update(str(candidate).encode("utf-8"))
                digest.update(str(stat.st_mtime_ns).encode("ascii"))
                digest.update(str(stat.st_size).encode("ascii"))
    return digest.hexdigest()[:16]


def resolve_skill_resource(skill_dir: Path, resource: str) -> Path:
    """Resolve one relative skill resource without escaping the package directory."""
    raw = str(resource or "").strip().replace("\\", "/")
    if not raw or raw == SKILL_FILENAME:
        raw = SKILL_FILENAME
    relative = Path(raw)
    if relative.is_absolute():
        raise SkillFormatError("resource must be relative to the skill directory")
    base = skill_dir.resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise SkillFormatError("resource escapes the skill directory")
    if not target.is_file():
        raise FileNotFoundError(raw)
    return target
