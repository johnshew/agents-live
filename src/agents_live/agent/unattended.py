"""Run-scoped provider configuration for unattended sessions."""
from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def provider_environment(
    provider: str,
    scratch: Path,
) -> Iterator[tuple[tuple[str, str], ...]]:
    """Return host configuration that cannot trust repository customizations."""
    if provider != "copilot":
        yield ()
        return

    home = scratch / "copilot-home"
    home.mkdir(mode=0o700)
    settings = home / "settings.json"
    settings.write_text(
        json.dumps({"disableAllHooks": True}, sort_keys=True),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    try:
        yield (("COPILOT_HOME", str(home)),)
    finally:
        shutil.rmtree(home, ignore_errors=True)
