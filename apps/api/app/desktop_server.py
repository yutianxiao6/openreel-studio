from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from app.main import app


def _default_user_data_dir() -> Path:
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
    config_dir = user_data / "config"
    logs_dir = user_data / "logs"
    for directory in (data_dir, storage_dir, config_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("APP_ENV", "desktop")
    os.environ.setdefault("APP_HOST", "127.0.0.1")
    os.environ.setdefault("PROJECT_ROOT", str(user_data))
    os.environ.setdefault("STORAGE_PATH", str(storage_dir))
    os.environ.setdefault("STORAGE_DIR", str(storage_dir))
    os.environ.setdefault(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{(data_dir / 'app.db').as_posix()}",
    )


def main() -> None:
    _ensure_desktop_env()
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
