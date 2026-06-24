from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import settings


def default_asset_library_roots() -> dict[str, str]:
    base = Path(settings.PROJECT_ROOT).expanduser().resolve() / "assets"
    return {
        "project_root": str(base / "projects"),
        "shared_root": str(base / "shared"),
    }


def effective_asset_library(config: Any, *, ensure_dirs: bool = False) -> dict[str, Any]:
    lib = dict(config) if isinstance(config, dict) else {}
    defaults = default_asset_library_roots()
    for key, value in defaults.items():
        if not lib.get(key):
            lib[key] = value
    if ensure_dirs:
        for key in ("project_root", "shared_root"):
            Path(str(lib[key])).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    return lib


def asset_library_roots(config: Any) -> list[Path]:
    lib = effective_asset_library(config)
    return [
        Path(str(lib[key])).expanduser().resolve()
        for key in ("project_root", "shared_root")
        if lib.get(key)
    ]
