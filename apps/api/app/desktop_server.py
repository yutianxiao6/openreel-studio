from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn


def _default_user_data_dir() -> Path:
    if os.environ.get("OPENREEL_DESKTOP") == "1":
        return Path.cwd()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(root) / "OpenReel Studio"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "openreel-studio"
    return Path.home() / ".local" / "share" / "openreel-studio"


def _ensure_desktop_env() -> None:
    user_data = Path(os.environ.get("OPENREEL_USER_DATA_DIR") or _default_user_data_dir())
    data_dir = user_data / "data"
    storage_dir = user_data / "storage"
    assets_dir = user_data / "assets"
    config_dir = user_data / "config"
    logs_dir = user_data / "logs"
    plugins_dir = user_data / "plugins"
    skills_dir = user_data / "skills"
    workflow_templates_dir = user_data / "workflow_templates"
    for directory in (
        data_dir,
        storage_dir,
        assets_dir,
        config_dir,
        logs_dir,
        plugins_dir,
        skills_dir,
        skills_dir / "workflows",
        skills_dir / "prompts",
        skills_dir / "review",
        workflow_templates_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    bundled_defaults = Path(getattr(sys, "_MEIPASS", "")) / "defaults"
    if bundled_defaults.exists():
        for name, target in (
            ("plugins", plugins_dir),
            ("workflow_templates", workflow_templates_dir),
        ):
            source = bundled_defaults / name
            if not source.exists():
                continue
            for item in source.iterdir():
                destination = target / item.name
                if destination.exists():
                    continue
                if item.is_dir():
                    shutil.copytree(item, destination)
                else:
                    shutil.copy2(item, destination)

    os.environ.setdefault("APP_ENV", "desktop")
    os.environ.setdefault("APP_HOST", "127.0.0.1")
    os.environ.setdefault("PROJECT_ROOT", str(user_data))
    os.environ.setdefault("OPENREEL_SKILLS_DIR", str(skills_dir))
    os.environ.setdefault("STORAGE_PATH", str(storage_dir))
    os.environ.setdefault("STORAGE_DIR", str(storage_dir))
    os.environ.setdefault(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{(data_dir / 'app.db').as_posix()}",
    )


def main() -> None:
    _ensure_desktop_env()
    from app.main import app

    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
