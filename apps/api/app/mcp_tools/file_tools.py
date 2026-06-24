"""File / storage MCP tools — sandboxed under settings.STORAGE_DIR / project_id.

All paths are resolved relative to the project's storage root and prevented
from escaping it. Used for: user uploads, agent-written notes, exported files.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.mcp_tools.query_match import invalid_regex_response, match_text, search_blob

TEXT_FULL_READ_MAX_BYTES = 200_000
TEXT_WINDOW_DEFAULT_LINES = 200
TEXT_WINDOW_MAX_LINES = 1_000
TEXT_WINDOW_MAX_CHARS = 80_000


def _root() -> Path:
    base = Path(getattr(settings, "STORAGE_DIR", "./data/storage")).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _project_dir(project_id: str) -> Path:
    p = _root() / project_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_path(project_id: str, rel_path: str) -> Path:
    base = _project_dir(project_id).resolve()
    target = (base / rel_path).resolve()
    if base not in target.parents and target != base:
        raise ValueError(f"Path escapes project sandbox: {rel_path}")
    return target


def _workspace_root() -> Path:
    return Path(settings.PROJECT_ROOT).resolve()


def _workspace_safe_path(path: str, *, allow_root: bool = False) -> Path:
    root = _workspace_root()
    raw = str(path or "").strip()
    if not raw:
        if allow_root:
            return root
        raise ValueError("path is required")
    candidate = Path(raw)
    target = candidate.resolve() if candidate.is_absolute() else (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Path escapes workspace: {path}")
    if target == root and not allow_root:
        raise ValueError("workspace root is not a valid file target")
    return target


def _workspace_rel(path: Path) -> str:
    return str(path.resolve().relative_to(_workspace_root())).replace("\\", "/")


def _deny_workspace_mutation(path: Path) -> None:
    rel = _workspace_rel(path) if path.resolve() != _workspace_root() else ""
    parts = [part for part in rel.split("/") if part]
    if not parts:
        raise ValueError("Refusing to mutate workspace root")
    if parts[0] == ".git":
        raise ValueError("Refusing to mutate .git")


def _entry_payload(path: Path) -> dict[str, Any]:
    stat = path.lstat()
    return {
        "path": _workspace_rel(path),
        "name": path.name,
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
        "is_symlink": path.is_symlink(),
        "size": stat.st_size if path.is_file() else None,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _text_total_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def _bounded_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 10_000_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    return max(minimum, min(parsed, maximum))


def _line_limit(value: int, default: int = TEXT_WINDOW_DEFAULT_LINES) -> int:
    return _bounded_int(value, default, maximum=TEXT_WINDOW_MAX_LINES)


def _start_line(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(1, parsed if parsed > 0 else 1)


def _full_text_payload(
    *,
    path_label: str,
    target: Path,
    text: str,
    size: int,
    mode: str = "text",
) -> dict[str, Any]:
    total_lines = _text_total_lines(text)
    return {
        "ok": True,
        "path": path_label,
        "mode": mode,
        "content": text,
        "size": size,
        "total_lines": total_lines,
        "start_line": 1 if total_lines else 0,
        "end_line": total_lines,
        "truncated": False,
        "next_offset": None,
        "mime_type": mimetypes.guess_type(target.name)[0],
    }


def _window_payload_from_lines(
    *,
    path_label: str,
    target: Path,
    lines: list[str],
    size: int,
    offset: int,
    limit: int,
    max_chars: int = TEXT_WINDOW_MAX_CHARS,
    total_lines: int | None = None,
    mode: str = "text",
) -> dict[str, Any]:
    start = _start_line(offset)
    line_limit = _line_limit(limit)
    stop = start + line_limit - 1
    clipped: list[str] = []
    char_count = 0
    truncated_by_chars = False
    end_line = start - 1
    max_chars = _bounded_int(max_chars, TEXT_WINDOW_MAX_CHARS, maximum=TEXT_WINDOW_MAX_CHARS)

    for line_number, raw_line in enumerate(lines, start=1):
        if line_number < start or line_number > stop:
            continue
        line = raw_line.rstrip("\n").rstrip("\r")
        separator_chars = 1 if clipped else 0
        remaining = max_chars - char_count - separator_chars
        if remaining <= 0:
            truncated_by_chars = True
            break
        if len(line) > remaining:
            clipped.append(line[:remaining])
            char_count = max_chars
            truncated_by_chars = True
            end_line = line_number
            break
        clipped.append(line)
        char_count += len(line) + separator_chars
        end_line = line_number

    total = len(lines) if total_lines is None else total_lines
    content = "\n".join(clipped)
    has_more_lines = end_line < total
    next_offset = end_line + 1 if end_line >= start and has_more_lines else None
    return {
        "ok": True,
        "path": path_label,
        "mode": mode,
        "content": content,
        "size": size,
        "total_lines": total,
        "start_line": start if total else 0,
        "end_line": end_line if end_line >= start else min(start - 1, total),
        "limit": line_limit,
        "truncated": bool(start > 1 or has_more_lines or truncated_by_chars),
        "next_offset": next_offset,
        "content_truncated_by_chars": truncated_by_chars,
        "mime_type": mimetypes.guess_type(target.name)[0],
        "hint": (
            "Continue with offset=next_offset and a line limit when more content is needed."
            if next_offset
            else None
        ),
    }


def _read_text_window_from_file(
    *,
    path_label: str,
    target: Path,
    size: int,
    offset: int,
    limit: int,
    max_chars: int = TEXT_WINDOW_MAX_CHARS,
) -> dict[str, Any]:
    # Uploads are capped, but workspace files may be larger. Read line by line so
    # a page can be returned without constructing a full-file string.
    lines: list[str] = []
    total = 0
    start = _start_line(offset)
    line_limit = _line_limit(limit)
    stop = start + line_limit - 1
    char_count = 0
    truncated_by_chars = False
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            total = line_number
            if line_number < start or line_number > stop or truncated_by_chars:
                continue
            line = raw_line.rstrip("\n").rstrip("\r")
            separator_chars = 1 if lines else 0
            remaining = max_chars - char_count - separator_chars
            if remaining <= 0:
                truncated_by_chars = True
                continue
            if len(line) > remaining:
                lines.append(line[:remaining])
                char_count = max_chars
                truncated_by_chars = True
                continue
            lines.append(line)
            char_count += len(line) + separator_chars

    end_line = start + len(lines) - 1
    has_more_lines = end_line < total
    next_offset = end_line + 1 if lines and has_more_lines else None
    return {
        "ok": True,
        "path": path_label,
        "mode": "text",
        "content": "\n".join(lines),
        "size": size,
        "total_lines": total,
        "start_line": start if total else 0,
        "end_line": end_line if lines else min(start - 1, total),
        "limit": line_limit,
        "truncated": bool(start > 1 or has_more_lines or truncated_by_chars),
        "next_offset": next_offset,
        "content_truncated_by_chars": truncated_by_chars,
        "mime_type": mimetypes.guess_type(target.name)[0],
        "hint": (
            "Continue with offset=next_offset and a line limit when more content is needed."
            if next_offset
            else None
        ),
    }


def _read_text_payload(
    *,
    path_label: str,
    target: Path,
    max_bytes: int,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    size = target.stat().st_size
    max_bytes = _bounded_int(max_bytes, TEXT_FULL_READ_MAX_BYTES)
    if offset > 0 or limit > 0 or size > max_bytes:
        return _read_text_window_from_file(
            path_label=path_label,
            target=target,
            size=size,
            offset=offset,
            limit=limit,
        )
    raw = target.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return _full_text_payload(path_label=path_label, target=target, text=text, size=size)


def _as_extract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["text"] = result.pop("content", "")
    return result


_WORKSPACE_SEARCH_SKIP_DIRS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".mypy_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}


def _iter_workspace_files(base: Path, *, recursive: bool, max_files: int) -> list[Path]:
    if base.is_file():
        return [base]
    if not base.exists() or not base.is_dir():
        return []
    files: list[Path] = []
    if recursive:
        for root, dir_names, file_names in os.walk(base):
            dir_names[:] = [name for name in dir_names if name not in _WORKSPACE_SEARCH_SKIP_DIRS]
            for file_name in file_names:
                files.append(Path(root) / file_name)
                if len(files) >= max_files:
                    return files
    else:
        for entry in sorted(base.iterdir()):
            if entry.is_file():
                files.append(entry)
                if len(files) >= max_files:
                    break
    return files


async def workspace_list(
    path: str = "",
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    recursive: bool = False,
    max_entries: int = 200,
) -> dict:
    """List files under the repository workspace."""
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    try:
        base = _workspace_safe_path(path, allow_root=True)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workspace_path_denied"}
    if not base.exists():
        return {"ok": False, "error": "Path not found", "error_kind": "file_not_found", "path": path}
    max_entries = max(1, min(int(max_entries or 200), 2000))
    entries: list[dict[str, Any]] = []
    truncated = False
    if recursive:
        for root, dir_names, file_names in os.walk(base):
            dir_names[:] = [name for name in dir_names if name not in _WORKSPACE_SEARCH_SKIP_DIRS]
            current = Path(root)
            for name in sorted([*dir_names, *file_names]):
                item = current / name
                entries.append(_entry_payload(item))
                if len(entries) >= max_entries:
                    truncated = True
                    break
            if truncated:
                break
    elif base.is_dir():
        for entry in sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            entries.append(_entry_payload(entry))
            if len(entries) >= max_entries:
                truncated = True
                break
    else:
        entries.append(_entry_payload(base))
    if query or regex or pattern:
        entries = [
            entry
            for entry in entries
            if match_text(
                search_blob(entry),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            ).get("matched")
        ]
    return {"ok": True, "root": str(_workspace_root()), "path": _workspace_rel(base), "entries": entries, "truncated": truncated}


async def workspace_read(
    path: str,
    mode: str = "text",
    max_bytes: int = TEXT_FULL_READ_MAX_BYTES,
    offset: int = 0,
    limit: int = 0,
) -> dict:
    """Read a workspace file as text or base64."""
    try:
        target = _workspace_safe_path(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workspace_path_denied"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "File not found", "error_kind": "file_not_found", "path": path}
    max_bytes = max(1, min(int(max_bytes or TEXT_FULL_READ_MAX_BYTES), 10_000_000))
    size = target.stat().st_size
    if mode == "base64" and size > max_bytes:
        return {
            "ok": False,
            "error": f"File too large (> {max_bytes} bytes)",
            "error_kind": "file_too_large",
            "path": _workspace_rel(target),
            "size": size,
        }
    if mode == "base64":
        raw = target.read_bytes()
        return {
            "ok": True,
            "path": _workspace_rel(target),
            "mode": "base64",
            "content_base64": base64.b64encode(raw).decode("ascii"),
            "size": size,
            "mime_type": mimetypes.guess_type(target.name)[0],
        }
    return _read_text_payload(
        path_label=_workspace_rel(target),
        target=target,
        max_bytes=max_bytes,
        offset=offset,
        limit=limit,
    )


async def workspace_search(
    query: str = "",
    path: str = "",
    glob: str = "*",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    recursive: bool = True,
    include_content: bool = True,
    max_results: int = 50,
    max_file_bytes: int = 200_000,
) -> dict:
    """Search workspace file names and optional text content without shell execution."""
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    try:
        base = _workspace_safe_path(path, allow_root=True)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workspace_path_denied"}
    query_text = str(query or "")
    max_results = max(1, min(int(max_results or 50), 500))
    max_file_bytes = max(1, min(int(max_file_bytes or 200_000), 2_000_000))
    matches: list[dict[str, Any]] = []
    files = _iter_workspace_files(base, recursive=bool(recursive), max_files=5000)
    should_search_content = include_content and bool(query_text or regex or pattern)
    for file_path in files:
        rel = _workspace_rel(file_path)
        if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(file_path.name, glob):
            continue
        path_match = match_text(
            rel,
            query=query_text,
            regex=regex,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )
        if path_match.get("matched"):
            matches.append({
                "path": rel,
                "match_type": "path",
                "line_number": None,
                "preview": rel,
                "match": {
                    key: value
                    for key, value in path_match.items()
                    if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
                },
            })
        if should_search_content and file_path.stat().st_size <= max_file_bytes:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(content.splitlines(), start=1):
                line_match = match_text(
                    line,
                    query=query_text,
                    regex=regex,
                    pattern=pattern,
                    case_sensitive=case_sensitive,
                )
                if line_match.get("matched"):
                    matches.append({
                        "path": rel,
                        "match_type": "content",
                        "line_number": line_number,
                        "preview": line.strip()[:240],
                        "match": {
                            key: value
                            for key, value in line_match.items()
                            if key in {"mode", "matched_terms", "matched_patterns"} and value not in (None, "", [], {})
                        },
                    })
                    break
        if len(matches) >= max_results:
            break
    return {
        "ok": True,
        "root": str(_workspace_root()),
        "query": query,
        "regex": regex,
        "pattern": pattern,
        "case_sensitive": case_sensitive,
        "path": _workspace_rel(base),
        "glob": glob,
        "matches": matches,
        "truncated": len(matches) >= max_results,
    }


async def workspace_write(
    path: str,
    content: str,
    overwrite: bool = True,
    append: bool = False,
    create_dirs: bool = True,
) -> dict:
    """Write text to a workspace file without executing commands."""
    try:
        target = _workspace_safe_path(path)
        _deny_workspace_mutation(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workspace_path_denied"}
    existed = target.exists()
    if existed and target.is_dir():
        return {"ok": False, "error": "Target is a directory", "error_kind": "target_is_directory", "path": path}
    if existed and not overwrite and not append:
        return {"ok": False, "error": "File exists and overwrite=false", "error_kind": "file_exists", "path": _workspace_rel(target)}
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        return {"ok": False, "error": "Parent directory does not exist", "error_kind": "parent_not_found", "path": path}
    old_size = target.stat().st_size if existed else 0
    if append:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "path": _workspace_rel(target),
        "created": not existed,
        "appended": bool(append),
        "old_size": old_size,
        "size": target.stat().st_size,
    }


async def workspace_patch(
    path: str,
    old_text: str,
    new_text: str,
    occurrence: int = 1,
) -> dict:
    """Patch a workspace text file by exact text replacement."""
    try:
        target = _workspace_safe_path(path)
        _deny_workspace_mutation(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workspace_path_denied"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "File not found", "error_kind": "file_not_found", "path": path}
    if not old_text:
        return {"ok": False, "error": "old_text is required", "error_kind": "empty_patch_anchor", "path": _workspace_rel(target)}
    content = target.read_text(encoding="utf-8", errors="replace")
    count = content.count(old_text)
    if count == 0:
        return {"ok": False, "error": "old_text not found", "error_kind": "patch_anchor_not_found", "path": _workspace_rel(target)}
    occurrence = int(occurrence or 1)
    if occurrence < 0 or occurrence > count:
        return {"ok": False, "error": f"occurrence out of range; found {count}", "error_kind": "patch_occurrence_out_of_range", "path": _workspace_rel(target), "found": count}
    replaced = count if occurrence == 0 else 1
    if occurrence == 0:
        updated = content.replace(old_text, new_text)
    else:
        parts = content.split(old_text)
        updated = old_text.join(parts[:occurrence]) + new_text + old_text.join(parts[occurrence:])
    target.write_text(updated, encoding="utf-8")
    return {
        "ok": True,
        "path": _workspace_rel(target),
        "found": count,
        "replaced": replaced,
        "size": target.stat().st_size,
    }


async def workspace_delete(
    path: str,
    recursive: bool = False,
    force: bool = False,
) -> dict:
    """Delete a workspace file or directory without executing commands."""
    try:
        target = _workspace_safe_path(path)
        _deny_workspace_mutation(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_kind": "workspace_path_denied"}
    if not target.exists() and not target.is_symlink():
        if force:
            return {"ok": True, "path": path, "deleted": False}
        return {"ok": False, "error": "Path not found", "error_kind": "file_not_found", "path": path}
    rel = _workspace_rel(target)
    if target.is_dir() and not target.is_symlink():
        if not recursive:
            return {"ok": False, "error": "Directory delete requires recursive=true", "error_kind": "recursive_required", "path": rel}
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True, "path": rel, "deleted": True, "recursive": bool(recursive)}


def image_base64_cache_rel_path(source_rel_path: str) -> str:
    raw = str(source_rel_path or "").strip().lstrip("/")
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    suffix = Path(raw).suffix.lower() or ".img"
    return f"_base64_images/{digest}{suffix}.json"


def write_image_base64_cache(
    project_id: str,
    source_rel_path: str,
    *,
    source_path: Path | None = None,
    mime_type: str | None = None,
) -> dict:
    source = source_path or _safe_path(project_id, source_rel_path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    rel_path = image_base64_cache_rel_path(source_rel_path)
    target = _safe_path(project_id, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mime = mime_type or mimetypes.guess_type(source.name)[0] or "image/png"
    payload = {
        "source_rel_path": source_rel_path,
        "mime_type": mime,
        "encoding": "base64",
        "base64": encoded,
        "bytes": source.stat().st_size,
        "base64_chars": len(encoded),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {
        "base64_rel_path": rel_path,
        "base64_size": target.stat().st_size,
        "base64_chars": len(encoded),
        "mime_type": mime,
    }


def read_image_base64_data_url(project_id: str, base64_rel_path: str) -> str:
    target = _safe_path(project_id, base64_rel_path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(base64_rel_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise ValueError("invalid base64 image cache")
    encoded = str(payload.get("base64") or "").strip()
    if not encoded:
        raise ValueError("empty base64 image cache")
    mime = str(payload.get("mime_type") or "image/png").strip() or "image/png"
    return f"data:{mime};base64,{encoded}"


async def list_dir(
    project_id: str = "",
    rel_path: str = "",
    query: str = "",
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> list[dict] | dict:
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    if project_id:
        target = _safe_path(project_id, rel_path)
        base = _project_dir(project_id)
    else:
        import os as _os
        data_root = Path(settings.PROJECT_ROOT).resolve() / "data"
        raw = _os.path.normpath((rel_path or "").strip().lstrip("/"))
        target = (data_root / raw).resolve() if raw else data_root
        if not str(target).startswith(str(data_root)):
            return []
        base = data_root

    if not target.exists() or not target.is_dir():
        return []
    items = []
    for entry in sorted(target.iterdir()):
        items.append(
            {
                "name": entry.name,
                "path": str(entry.relative_to(base)).replace("\\", "/"),
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else None,
            }
        )
    if query or regex or pattern:
        items = [
            item
            for item in items
            if match_text(
                search_blob(item),
                query=query,
                regex=regex,
                pattern=pattern,
                case_sensitive=case_sensitive,
            ).get("matched")
        ]
    return items


async def read_text(
    project_id: str = "",
    rel_path: str = "",
    max_bytes: int = TEXT_FULL_READ_MAX_BYTES,
    offset: int = 0,
    limit: int = 0,
) -> dict:
    """Read a text file. project_id empty → read from data/ ; otherwise project storage."""
    if not rel_path:
        return {"error": "rel_path is required"}

    if project_id:
        target = _safe_path(project_id, rel_path)
    else:
        import os as _os
        data_root = Path(settings.PROJECT_ROOT).resolve() / "data"
        raw = _os.path.normpath(rel_path.strip().lstrip("/"))
        target = (data_root / raw).resolve()
        if not str(target).startswith(str(data_root)):
            return {"error": f"路径超出允许范围: {rel_path}"}

    if not target.exists() or not target.is_file():
        return {"error": "File not found"}
    return _read_text_payload(
        path_label=rel_path,
        target=target,
        max_bytes=max_bytes,
        offset=offset,
        limit=limit,
    )


async def write_text(project_id: str, rel_path: str, content: str) -> dict:
    target = _safe_path(project_id, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": rel_path, "size": target.stat().st_size}


async def save_uploaded_file(
    project_id: str, filename: str, content_b64: str, subdir: str = "uploads"
) -> dict:
    """Decode a base64-encoded blob and save under storage/<project>/<subdir>/."""
    rel_path = f"{subdir}/{filename}"
    target = _safe_path(project_id, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(content_b64)
    target.write_bytes(raw)
    return {
        "path": rel_path,
        "size": len(raw),
        "mime_type": mimetypes.guess_type(filename)[0],
    }


async def delete_file(project_id: str, rel_path: str) -> dict:
    target = _safe_path(project_id, rel_path)
    if not target.exists():
        return {"ok": False, "error": "Not found"}
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True, "path": rel_path}


async def extract_text_from_upload(
    project_id: str,
    rel_path: str,
    offset: int = 0,
    limit: int = 0,
    max_chars: int = TEXT_WINDOW_MAX_CHARS,
) -> dict:
    """Best-effort text extraction for txt / md / docx with optional line paging."""
    max_chars = _bounded_int(max_chars, TEXT_WINDOW_MAX_CHARS, maximum=TEXT_WINDOW_MAX_CHARS)
    target = _safe_path(project_id, rel_path)
    if not target.exists() or not target.is_file():
        return {"error": "File not found"}

    suffix = target.suffix.lower()
    if suffix in {".txt", ".md"}:
        payload = _read_text_payload(
            path_label=rel_path,
            target=target,
            max_bytes=max_chars,
            offset=offset,
            limit=limit,
        )
        return _as_extract_payload(payload)
    if suffix == ".docx":
        try:
            from docx import Document  # type: ignore
        except ImportError:
            return {"error": "python-docx not installed"}
        doc = Document(str(target))
        text = "\n".join(p.text for p in doc.paragraphs)
        lines = text.split("\n") if text else []
        payload = _window_payload_from_lines(
            path_label=rel_path,
            target=target,
            lines=lines,
            size=target.stat().st_size,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        ) if (offset > 0 or limit > 0 or len(text) > max_chars) else _full_text_payload(
            path_label=rel_path,
            target=target,
            text=text,
            size=target.stat().st_size,
        )
        return _as_extract_payload(payload)
    return {"error": f"Unsupported file type: {suffix}"}
