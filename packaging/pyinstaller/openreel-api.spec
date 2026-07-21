# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parents[1]
API_DIR = ROOT / "apps" / "api"

datas = [
    (str(API_DIR / "app" / "skills"), "app/skills"),
    (str(API_DIR / "app" / "prompts"), "app/prompts"),
    (str(API_DIR / "app" / "agent" / "prompts"), "app/agent/prompts"),
]
datas += collect_data_files("litellm")
datas += collect_data_files("universal_model_adapter")

for protocol_dir_name in (
    "image_provider_protocols",
    "video_provider_protocols",
    "audio_provider_protocols",
):
    protocol_dir = ROOT / "config" / protocol_dir_name
    if protocol_dir.exists():
        datas.append((str(protocol_dir), f"defaults/config/{protocol_dir_name}"))

KEYFRAME_PLUGIN_DIR = ROOT / "plugins" / "keyframe-extractor"
for plugin_file in ("main.py", "plugin.json"):
    path = KEYFRAME_PLUGIN_DIR / plugin_file
    if path.exists():
        datas.append((str(path), "defaults/plugins/keyframe-extractor"))

mcp_config = API_DIR / "app" / "mcp_servers.json"
if mcp_config.exists():
    datas.append((str(mcp_config), "app"))

hiddenimports = [
    "aiosqlite",
    "fastapi",
    "litellm",
    "mcp",
    "multipart",
    "pydantic",
    "pydantic_settings",
    "sse_starlette",
    "sqlalchemy",
    "sqlmodel",
    "uvicorn",
    "uvicorn.lifespan.on",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.loops.auto",
]
hiddenimports += collect_submodules("tiktoken_ext")
hiddenimports += collect_submodules("app.agent.prompts")
hiddenimports += collect_submodules("app.prompts")
hiddenimports += collect_submodules("universal_model_adapter")
hiddenimports.append("app.agent.workflow_spec_prompt_contract")
hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    [str(API_DIR / "app" / "desktop_server.py")],
    pathex=[str(API_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="openreel-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=sys.platform != "win32",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="openreel-api",
)
