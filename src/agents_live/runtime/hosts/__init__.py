"""Selection of the one host adapter for this process."""
from __future__ import annotations

import os
import sys

from ..protocols import HostAdapter


def current() -> HostAdapter:
    if sys.platform == "win32":
        from .windows import WindowsHost
        return WindowsHost()
    if os.environ.get("WSL_DISTRO_NAME"):
        from .wsl import WslHost
        return WslHost()
    from .posix import PosixHost
    return PosixHost()
