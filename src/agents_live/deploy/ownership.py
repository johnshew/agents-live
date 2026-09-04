"""Who owns an installation, and how a command finds out.

Every installation has exactly one upgrade owner (#369). The rule is
easy to state and easy to violate, because two artifacts can answer to
``agents-live`` on one PATH: the copy a package manager installed and
the one Agents Live would manage itself. If both believe they may
replace the runtime, they race, and the loser leaves an installation
that is neither version - the failure #231 records.

Ownership is therefore something a command can *read*, from evidence on
disk, before it changes anything:

===============  ==============================================  ==========
owner            evidence                                        upgrades by
===============  ==============================================  ==========
``agents-live``  the running image sits inside                    activating
                 ``<root>/versions/<generation>/``                a new
                                                                  generation
``unmanaged``    neither: a checkout, an editable install, a      whatever
                 ``uvx`` run, or a distribution channel that      installed
                 has not been taught to record itself             it
===============  ==============================================  ==========

The self-managed generation layout is the only supported installation
channel. Package-manager and checkout executions are unmanaged: they may
inspect state, but they cannot replace or remove an installation.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from . import layout, pointer as pointer_module

SELF = "agents-live"
UNMANAGED = "unmanaged"

#: How each owner is named to an operator.
LABELS = {
    SELF: "self-managed",
    UNMANAGED: "unmanaged",
}


@dataclass(frozen=True)
class Installation:
    """What a running command can say about the installation it is in."""

    owner: str
    executable: Path
    root: Path
    generation: str | None
    active_generation: str | None
    pointer_state: str
    contested: bool
    detail: str

    @property
    def self_managed(self) -> bool:
        """Whether this command runs from a generation it owns."""
        return self.owner == SELF

    @property
    def stale(self) -> bool:
        """Whether a newer generation was activated under this process.

        Normal, not broken: a long-lived watcher keeps executing the
        generation it started with and picks the new one up at its next
        idle version check (#188).
        """
        return bool(
            self.generation and self.active_generation
            and self.generation != self.active_generation)

    def summary(self) -> str:
        """One line for an operator, with no machine-specific paths."""
        return f"{LABELS.get(self.owner, self.owner)}; {self.detail}"


def classify(executable: Path | str, *, root: Path,
             pointer: pointer_module.Pointer | None, pointer_state: str,
             recorded_owner: str | None = None
             ) -> Installation:
    """Decide the owner from evidence, without touching the host.

    The running image decides first, because that is the artifact an
    upgrade would have to replace. A recorded owner that disagrees with
    it is reported as contested rather than believed: a file cannot
    outrank what is executing.
    """
    path = Path(executable)
    generation = layout.generation_of(path, root)
    if generation is not None:
        owner = SELF
    else:
        owner = UNMANAGED
    active = pointer.generation if pointer is not None else None
    contested = (
        (pointer_state == pointer_module.ACTIVE and owner != SELF)
        or bool(recorded_owner and recorded_owner != owner))
    detail = _detail(owner, generation, active, pointer_state, contested)
    return Installation(
        owner=owner,
        executable=path,
        root=root,
        generation=generation,
        active_generation=active,
        pointer_state=pointer_state,
        contested=contested,
        detail=detail,
    )


def _detail(owner: str, generation: str | None, active: str | None,
            pointer_state: str, contested: bool) -> str:
    label = LABELS.get(owner, owner)
    if contested:
        return (
            f"a generation layout is present and active ({active}) while this "
            f"command runs from a {label} installation; two installations can "
            "answer to 'agents-live' on PATH, and only one may upgrade it")
    if owner == SELF:
        if generation != active:
            return (
                f"running generation {generation}; generation {active} is "
                "active and this process will pick it up when it restarts")
        return f"running the active generation {generation}"
    if pointer_state == pointer_module.MISSING:
        return "the generation layout is not in use on this host"
    return f"the generation pointer is {pointer_state}"


def describe(executable: Path | str | None = None,
             root: Path | None = None) -> Installation:
    """Classify the installation this process is running from.

    Reads the filesystem and nothing else: no package manager, no
    process table, no network. Safe to call from ``doctor`` and from any
    command that has to report before it acts.
    """
    target = Path(executable if executable is not None else sys.executable)
    install_root = root or layout.installation_root()
    pointer, state, _ = pointer_module.status(layout.current_path(install_root))
    return classify(
        target,
        root=install_root,
        pointer=pointer,
        pointer_state=state,
        recorded_owner=read_record(install_root),
    )


def read_record(root: Path | None = None) -> str | None:
    """The owner this installation recorded for itself, if it did."""
    try:
        text = layout.ownership_path(root).read_text(encoding="utf-8")
    except OSError:
        return None
    value = text.strip()
    return value or None


def write_record(owner: str, root: Path | None = None) -> None:
    """Record *owner* beside the pointer, atomically."""
    if owner not in (SELF, UNMANAGED):
        raise ValueError(f"'{owner}' is not an installation owner")
    paths.atomic_write_text(layout.ownership_path(root), f"{owner}\n")


def refusal(installation: Installation, *, action: str = "upgrade"
            ) -> str | None:
    """What a non-owning *action* must say, or ``None`` if it may proceed.

    The message names the owning channel, because "you do not own this"
    is only useful next to what does, and it names the command that does
    own it. A refusal whose only remedy is filesystem surgery is one an
    operator meets for the first time on a broken host.
    """
    if installation.contested:
        return (
            f"{action} refused: this host has both a "
            f"{LABELS.get(installation.owner, installation.owner)} "
            "installation and an active generation layout, and either could "
            "replace the runtime. Run the same command through the "
            f"self-managed installation ({layout.command_path(root=installation.root)}), "
            f"which owns it, or retire that installation with "
            f"`{layout.command_path(root=installation.root)} uninstall`")
    if installation.owner == UNMANAGED:
        return (
            f"{action} refused: this runtime was not installed by a channel "
            "that records an owner, so it cannot replace itself safely")
    return None
