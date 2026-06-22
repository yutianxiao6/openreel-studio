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
