from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn


def _copy_missing_directory_entries(source_root: Path, target_root: Path) -> None:
    if not source_root.exists() or not source_root.is_dir():
        return
    target_root.mkdir(parents=True, exist_ok=True)
    for item in source_root.iterdir():
        destination = target_root / item.name
        if item.is_dir():
            if destination.exists() and not destination.is_dir():
                continue
            _copy_missing_directory_entries(item, destination)
        elif not destination.exists():
            shutil.copy2(item, destination)


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
            ("config", config_dir),
            ("plugins", plugins_dir),
            ("workflow_templates", workflow_templates_dir),
        ):
            source = bundled_defaults / name
            _copy_missing_directory_entries(source, target)

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


def _run_packaging_smoke() -> None:
    from app.agent.prompt_assembler import PromptContext, assemble_split_result
    from app.agent.workflow_spec_prompt_contract import AUTHORING_SPEC_GUIDE
    from app.config_store.schema import MediaProviderEntry
    from app.services import media_operations, subprocess_utils

    if not AUTHORING_SPEC_GUIDE.strip():
        raise RuntimeError("workflow spec prompt contract is empty")
    workflow_prompt = assemble_split_result(
        PromptContext(
            project_id="packaging-smoke",
            user_message="检查工作流构建提示词",
            state={},
            collaboration_mode="workflow_build",
        )
    )
    if "Workflow Build Mode" not in workflow_prompt.system:
        raise RuntimeError("workflow build prompt was not assembled")
    process_kwargs = subprocess_utils.hidden_window_kwargs()
    if os.name == "nt" and not process_kwargs.get("creationflags"):
        raise RuntimeError("Windows media subprocesses are not configured to hide command windows")
    if not callable(media_operations.split_video_tracks):
        raise RuntimeError("video split operation was not bundled")

    samples = (
        ("image", "image_http_v1", "image_protocol_id", "openai_images_generations"),
        ("video", "video_http_v1", "video_protocol_id", "seedance_2_0"),
        ("audio", "audio_http_v1", "audio_protocol_id", "openai_audio_speech"),
    )
    for kind, api_format, param_name, protocol_id in samples:
        MediaProviderEntry(
            kind=kind,
            name=f"packaging-smoke-{kind}",
            base_url="https://example.test/v1",
            model_name="packaging-smoke-model",
            api_format=api_format,
            params={param_name: protocol_id},
        )

    print("OpenReel desktop packaging smoke passed", flush=True)


def main() -> None:
    _ensure_desktop_env()
    if os.environ.get("OPENREEL_PACKAGING_SMOKE") == "1":
        _run_packaging_smoke()
        return
    from app.main import app

    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
