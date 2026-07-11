"""Cross-platform subprocess launch options for bundled desktop runtimes."""
from __future__ import annotations

import os
import subprocess
from typing import Any


def _is_windows() -> bool:
    return os.name == "nt"


def hidden_window_kwargs() -> dict[str, Any]:
    """Prevent command windows from flashing for media tools on Windows."""
    if not _is_windows():
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}
