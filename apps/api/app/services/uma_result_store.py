"""Private archives for complete Universal Model Adapter invocation results.

The archive is diagnostic state, not an Agent/tool result. Public node results
are projected separately by the media service and node visibility layer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from universal_model_adapter import InvocationResult

from app.config import settings


logger = logging.getLogger(__name__)

MAX_UMA_ARCHIVES_PER_PROJECT = 256
MAX_UMA_ARCHIVE_BYTES_PER_PROJECT = 2 * 1024 * 1024 * 1024


def _safe_component(value: str, fallback: str) -> str:
    rendered = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in str(value or fallback)
    )
    return rendered or fallback


def _archive_root(project_id: str) -> Path:
    safe_project = _safe_component(project_id, "project")
    return Path(settings.PROJECT_ROOT) / "data" / "tool_results" / safe_project / "uma_results"


def _output_suffix(output: Any) -> str:
    path = getattr(output, "path", None)
    if path:
        suffix = Path(path).suffix.lower()
        if suffix and len(suffix) <= 12:
            return suffix
    url = str(getattr(output, "url", None) or "").split("?", 1)[0]
    suffix = Path(url).suffix.lower()
    if suffix and len(suffix) <= 12:
        return suffix
    mime_type = str(getattr(output, "mime_type", None) or "").split(";", 1)[0].strip()
    guessed = mimetypes.guess_extension(mime_type) if mime_type else None
    return guessed or ".bin"


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _directory_size(path: Path) -> int:
    total = 0
    for candidate in path.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _prune_archives(root: Path, *, keep: Path) -> None:
    archives = [path for path in root.iterdir() if path.is_dir()]
    archives.sort(
        key=lambda path: (
            path != keep,
            -(path.stat().st_mtime_ns if path.exists() else 0),
        )
    )
    retained_count = 0
    retained_bytes = 0
    for archive in archives:
        size = _directory_size(archive)
        fits = (
            retained_count < MAX_UMA_ARCHIVES_PER_PROJECT
            and retained_bytes + size <= MAX_UMA_ARCHIVE_BYTES_PER_PROJECT
        )
        if archive == keep or fits:
            retained_count += 1
            retained_bytes += size
            continue
        try:
            shutil.rmtree(archive)
        except OSError:
            logger.warning("failed to prune UMA result archive %s", archive, exc_info=True)


def _save_invocation_result(
    *,
    project_id: str,
    result: InvocationResult,
    materialized_outputs: Mapping[int, Mapping[str, Any]] | None,
) -> Path:
    root = _archive_root(project_id)
    archive = root / _safe_component(result.id, "invocation")
    archive.mkdir(parents=True, exist_ok=True)

    payload = result.model_dump(mode="json", exclude={"outputs"})
    normalized_outputs: list[dict[str, Any]] = []
    for index, output in enumerate(result.outputs):
        normalized = output.model_dump(mode="json", exclude={"data"})
        data = getattr(output, "data", None)
        if data is not None:
            output_type = _safe_component(str(getattr(output, "type", "output")), "output")
            filename = f"output_{index:03d}_{output_type}{_output_suffix(output)}"
            artifact_path = archive / filename
            _atomic_write_bytes(artifact_path, data)
            normalized["data_artifact"] = {
                "filename": filename,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        materialized = (materialized_outputs or {}).get(index)
        if materialized is not None:
            normalized["host_materialization"] = dict(materialized)
        normalized_outputs.append(normalized)

    payload["archive_version"] = 1
    payload["outputs"] = normalized_outputs
    result_path = archive / "result.json"
    _atomic_write_json(result_path, payload)
    _prune_archives(root, keep=archive)
    return result_path


async def archive_invocation_result(
    *,
    project_id: str,
    result: InvocationResult,
    materialized_outputs: Mapping[int, Mapping[str, Any]] | None = None,
) -> Path | None:
    """Persist every UMA result field without adding it to model-visible output."""
    try:
        return await asyncio.to_thread(
            _save_invocation_result,
            project_id=project_id,
            result=result,
            materialized_outputs=materialized_outputs,
        )
    except Exception:
        logger.warning(
            "failed to archive UMA invocation project_id=%s invocation_id=%s",
            project_id,
            result.id,
            exc_info=True,
        )
        return None


def load_invocation_result_archive(project_id: str, invocation_id: str) -> dict[str, Any] | None:
    """Read a private archive for diagnostics and tests."""
    path = (
        _archive_root(project_id)
        / _safe_component(invocation_id, "invocation")
        / "result.json"
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
