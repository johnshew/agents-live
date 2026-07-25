"""Host runtime identity: which environment this process is running in.

The first member of the host-runtime seam described in
docs/windows-support.md. Dispatch, locking, process control, and file
change notification still live in their current modules; only identity
is extracted so far, because it was the one member already duplicated
across the tree.

The value answers "which runtime environment is this", not "which
operating system". Linux and WSL are separate environments on purpose:
one physical machine can host both, they own their agents
independently, and only WSL carries the Windows-side heartbeat
integration.
"""
from __future__ import annotations

import sys
from pathlib import Path

LINUX = "linux"
WSL = "wsl"
WINDOWS = "windows"
MACOS = "macos"

PROC_VERSION = Path("/proc/version")


def id() -> str:  # noqa: A001 - the seam member is named `id` by design
    """Return the runtime environment identifier for this process."""
    if sys.platform == "win32":
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    try:
        if "microsoft" in PROC_VERSION.read_text().lower():
            return WSL
    except OSError:
        pass
    return LINUX
