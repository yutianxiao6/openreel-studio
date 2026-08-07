"""Project-scoped local path validation for outbound media inputs."""
from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.db.models import Project
from app.db.session import session_scope
from app.services.asset_library_paths import asset_library_roots


def path_is_within_roots(path: Path, roots: list[Path] | tuple[Path, ...]) -> bool:
    """Return whether a resolved file is contained by one of the allowed roots."""
    target = path.expanduser().resolve()
    return any(target.is_relative_to(root.expanduser().resolve()) for root in roots)


async def project_media_roots(project_id: str) -> tuple[Path, ...]:
    """Return the project storage root and its configured shared asset roots."""
    roots: list[Path] = [(settings.storage_path_resolved / project_id).resolve()]
    library: dict = {}
    try:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
        if project:
            state = json.loads(project.state_json or "{}")
            configured = state.get("asset_library")
            if isinstance(configured, dict):
                library = configured
    except (json.JSONDecodeError, OSError, RuntimeError):
        library = {}
    for root in asset_library_roots(library):
        resolved = root.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def resolve_project_media_file(
    project_id: str,
    value: str | Path,
    *,
    allowed_roots: list[Path] | tuple[Path, ...],
) -> Path | None:
    """Resolve an existing media file without allowing traversal outside its roots."""
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else settings.storage_path_resolved / project_id / raw
    try:
        target = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_file() or not path_is_within_roots(target, allowed_roots):
        return None
    return target
