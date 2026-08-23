"""The generation lifecycle, as planning rather than as effects.

#369 asks for deployment to be a transactional subsystem with testable
planning and stated failure semantics, and hands the state machine
itself to #334. This module is that state machine's pure half: it takes
facts - what is active, what is being installed, what is running - and
answers with an ordered plan, a recovery for a partial failure, or the
set of generations a collector may remove.

Nothing here touches a filesystem, a process table, or a package index.
Those are effects, and they belong behind the narrow adapters the steps
name. A plan is worth testing precisely because it can be wrong in ways
that are expensive to discover on a host: a step that mutates the active
generation before activation, a collector that removes a generation a
scheduled task is about to launch, or a rollback that has nothing to
roll back to.

The ordered steps, from #369:

1. ``inspect``   the active installation, its owner, and its channel
2. ``resolve``   the target package, version, plugins, and provenance
3. ``stage``     build a complete generation without touching the active
4. ``validate``  package and framework smoke checks on the staged one
5. ``quiesce``   stop only the processes that must stop
6. ``activate``  one atomic pointer write
7. ``restore``   put back the runtime state quiesce took down
8. ``verify``    health, and roll back the pointer if it fails
9. ``collect``   remove generations nothing holds

Steps 1 through 4 never touch what is running. That is the invariant the
whole model buys, and :func:`plan_activation` is where it is enforced.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import layout, ownership

INSPECT = "inspect"
RESOLVE = "resolve"
STAGE = "stage"
VALIDATE = "validate"
QUIESCE = "quiesce"
ACTIVATE = "activate"
RESTORE = "restore"
VERIFY = "verify"
COLLECT = "collect"
ROLLBACK = "rollback"

#: How many superseded generations survive a collection. One is enough
#: to roll back to and cheap enough to keep; older ones are disk cost
#: with no story attached (#334 open question, answered here).
RETAINED_PREVIOUS = 1


@dataclass(frozen=True)
class Step:
    """One lifecycle step, and what it is allowed to disturb."""

    name: str
    detail: str
    #: Whether the step can change what an already-running process
    #: executes. Only activation and collection may, and collection only
    #: after nothing holds the directory it removes.
    touches_active: bool = False
    #: Whether undoing the step is a single further action.
    reversible: bool = True


@dataclass(frozen=True)
class Plan:
    """An ordered lifecycle, or a refusal explaining why there is none."""

    target: str
    current: str | None
    steps: tuple[Step, ...] = ()
    quiesce: tuple[str, ...] = ()
    rollback_to: str | None = None
    refusal: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.refusal is None

    def names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    def index(self, name: str) -> int:
        return self.names().index(name)


@dataclass(frozen=True)
class Recovery:
    """What an interrupted deployment recovers to."""

    action: str
    detail: str
    #: Whether an operator has to do something. A recovery that a later
    #: command performs by itself is not an incident.
    manual: bool = False


def plan_activation(*, target: str, current: str | None,
                    installation: ownership.Installation | None = None,
                    holders: dict[str, tuple[str, ...]] | None = None,
                    ) -> Plan:
    """Plan the installation and activation of *target*.

    *holders* maps a generation name to the processes executing from it.
    Holders of the *active* generation do not block anything: they keep
    running the code they started with, which is why upgrade stops being
    refusable under this model. Holders of the *target* directory do
    block, because staging would rewrite the directory underneath them,
    which is the in-place rewrite this design exists to avoid.
    """
    holding = holders or {}
    try:
        name = layout.generation_name(target)
    except layout.LayoutError as exc:
        return Plan(target=target, current=current, refusal=str(exc))
    if installation is not None:
        refused = ownership.refusal(installation, action="upgrade")
        if refused:
            return Plan(target=name, current=current, refusal=refused)
    blocked = tuple(holding.get(name, ())) if name != current else ()
    if blocked:
        return Plan(
            target=name, current=current,
            refusal=(
                f"generation {name} is already installed and in use by "
                f"{', '.join(blocked)}; installing it again would rewrite a "
                "directory that is executing"))
    notes: list[str] = []
    if name == current:
        notes.append(
            f"generation {name} is already active; activation rewrites the "
            "pointer with the same name, which changes nothing")
    stale = tuple(holding.get(current, ())) if current else ()
    if stale:
        notes.append(
            f"{len(stale)} process(es) keep running generation {current} "
            "until they restart; nothing has to stop for this upgrade")
    steps = (
        Step(INSPECT, "read the active generation, its owner, and its channel"),
        Step(RESOLVE, "resolve the target version, its plugins, and their provenance"),
        Step(STAGE, f"build generation {name} beside the active one",
             reversible=True),
        Step(VALIDATE, f"smoke-check generation {name} before anything points at it"),
        Step(QUIESCE, "stop only what cannot survive the activation"),
        Step(ACTIVATE, f"write the pointer: {current or 'nothing'} -> {name}",
             touches_active=True),
        Step(RESTORE, "restart what quiesce stopped"),
        Step(VERIFY, "verify health, and roll the pointer back if it fails"),
        Step(COLLECT, "remove superseded generations nothing is executing",
             touches_active=True, reversible=False),
    )
    return Plan(
        target=name, current=current, steps=steps,
        quiesce=(), rollback_to=current, notes=tuple(notes))


def collectable(generations: tuple[str, ...] | list[str], *,
                active: str | None, held: dict[str, tuple[str, ...]] | None = None,
                order: tuple[str, ...] | list[str] | None = None,
                retain: int = RETAINED_PREVIOUS) -> tuple[str, ...]:
    """Which generations a collector may remove.

    Three rules, and each one exists because breaking it has a specific
    consequence:

    - never the active generation: the pointer names it, so removing it
      breaks every launcher immediately;
    - never a held generation: a process is executing from it, and on
      Windows the removal would half-finish;
    - never the most recent superseded generations: they are the
      rollback, and rollback is only free while they are on disk.

    *order* is the activation order, newest last, when the caller knows
    it; otherwise the retained set is chosen by sorted name so the
    answer is deterministic rather than arbitrary.
    """
    holding = held or {}
    superseded = [name for name in (order or sorted(generations))
                  if name in set(generations) and name != active]
    keep = set(superseded[-retain:]) if retain > 0 else set()
    return tuple(sorted(
        name for name in superseded
        if name not in keep and not holding.get(name)))


#: What each way of stopping half way through recovers to. The states are
#: the observable ones: a directory that exists, a pointer that does or
#: does not parse, and a pointer that has moved but whose health is not
#: yet verified.
_RECOVERIES = {
    "staging": Recovery(
        "discard",
        "an interrupted stage leaves only a .staging- directory; the active "
        "generation was never touched, so the recovery is to delete it and "
        "stage again"),
    "staged": Recovery(
        "retry",
        "a complete but unvalidated generation is inert: nothing points at "
        "it, so validation can simply run again"),
    "quiesced": Recovery(
        "restore",
        "quiesce stopped some processes and failed on others; the recovery "
        "is to restart what was stopped and report which holders refused, "
        "because a partial quiesce is not a partial upgrade - the pointer "
        "has not moved"),
    "activated": Recovery(
        "verify",
        "the pointer names the new generation and health is unverified; a "
        "concurrent command sees the new generation, which is correct, and "
        "an already-running process keeps the old one until it restarts"),
    "unverified": Recovery(
        "rollback",
        "post-activation verification failed; writing the previous "
        "generation back into the pointer is a complete rollback because "
        "the previous generation was never modified"),
    "pointer-unreadable": Recovery(
        "repair",
        "the pointer exists and does not parse; no generation may be "
        "guessed from the directory listing, so the launcher reports the "
        "damage and `agents-live doctor` names the repair",
        manual=True),
    "pointer-unsupported": Recovery(
        "replace-launcher",
        "the pointer was written by a newer runtime than the launcher "
        "understands; the launcher is the stale artifact and is what gets "
        "replaced, never the pointer",
        manual=True),
    "collecting": Recovery(
        "reconcile",
        "collection is resumable: it removes only generations that are "
        "neither active, retained, nor held, and it re-reads the pointer "
        "under the same lock an activation takes, so it cannot race one"),
}


def recovery(state: str) -> Recovery | None:
    """The recovery for an interrupted *state*, or ``None`` if unknown."""
    return _RECOVERIES.get(state)


def states() -> tuple[str, ...]:
    """Every interruption state this model has an answer for."""
    return tuple(_RECOVERIES)
