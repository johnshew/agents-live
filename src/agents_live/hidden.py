"""Start a command with no console window.

Windows Task Scheduler runs an action with an interactive token, in the
developer's own session, and a console program started that way opens a
console window - once per fire, on top of whatever they were doing. A
task action therefore names ``pythonw``, which has no console to show,
and ``pythonw`` runs this: it starts the real command with
``CREATE_NO_WINDOW`` and exits with its status.

The indirection is the point. Run under ``pythonw`` directly, the tool
would have no standard streams at all and fail on its first write;
started from here it gets a console of its own that simply is not
drawn, so its output behaves exactly as it does anywhere else.

Nothing but :mod:`agents_live.wintasks` names this module, and only in
the argument string it persists into a task.
"""
from __future__ import annotations

import subprocess
import sys

# The child is a console program that is run without a console window.
CREATE_NO_WINDOW = 0x08000000


def main(argv: list[str] | None = None) -> int:
    """Run ``argv`` hidden and return its exit status."""
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        return 2  # nothing to run: a usage error, not a failed run
    flags = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        return subprocess.run(command, **flags).returncode
    except OSError:
        # There is no console to explain this on, and the caller is a
        # scheduler that reads exit codes: say it failed to start.
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
