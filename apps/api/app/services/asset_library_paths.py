from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import settings


def default_asset_library_roots() -> dict[str, str]:
    base = Path(settings.PROJECT_ROOT).expanduser().resolve() / "assets"
    return {
        "root": str(base),
        "project_root": str(base),
        "shared_root": str(base),
    }


def effective_asset_library(config: Any, *, ensure_dirs: bool = False) -> dict[str, Any]:
    lib = dict(config) if isinstance(config, dict) else {}
    defaults = default_asset_library_roots()
    legacy_project_root = lib.get("project_root")
    legacy_shared_root = lib.get("shared_root")
    legacy_parent: Path | None = None
    if not (lib.get("root") or lib.get("library_root") or lib.get("asset_root")) and legacy_project_root and legacy_shared_root:
        project_path = Path(str(legacy_project_root)).expanduser()
        shared_path = Path(str(legacy_shared_root)).expanduser()
        if project_path.name == "projects" and shared_path.name == "shared" and project_path.parent == shared_path.parent:
            legacy_parent = shared_path.parent
    root = (
        lib.get("root")
        or lib.get("library_root")
        or lib.get("asset_root")
        or (str(legacy_parent) if legacy_parent is not None else None)
        or lib.get("shared_root")
        or lib.get("project_root")
        or defaults["root"]
    )
    lib["root"] = str(Path(str(root)).expanduser().resolve())
    lib["project_root"] = lib["root"]
    lib["shared_root"] = lib["root"]
    if ensure_dirs:
        Path(str(lib["root"])).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    return lib


def asset_library_roots(config: Any) -> list[Path]:
    lib = effective_asset_library(config)
    return [Path(str(lib["root"])).expanduser().resolve()]
