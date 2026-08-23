"""Deployment vocabulary and primitives: installation root, generation
pointer, stable launcher, and the planning rules around them.

This subsystem is the foundation described in #334 (step 1: installation
root and stable launcher) with the vocabulary, failure semantics, and
ownership rules from #369. It is deliberately inert: nothing here is on
the path of an install, an upgrade, or an uninstall today. The runtime is
still uv-managed, ``agents-live upgrade`` still runs ``uv tool upgrade``,
and no command relocates itself into an installation root.

What it does provide is the model those steps need, in a form that can be
tested without a host:

- :mod:`~agents_live.deploy.layout` computes where a self-managed
  installation would keep its generations, its pointer, and its
  launchers, and refuses any generation name that could escape the root.
- :mod:`~agents_live.deploy.pointer` reads and writes the generation
  pointer, and classifies every way it can fail to answer.
- :mod:`~agents_live.deploy.ownership` decides which channel owns the
  installation a command is running from, so a foreign owner can be
  reported before it can be raced.
- :mod:`~agents_live.deploy.generation` stages, validates, and promotes a
  generation before activation performs the single pointer write.
- :mod:`~agents_live.deploy.plan` is the pure generation lifecycle: the
  ordered steps, what may run before activation, what a partial failure
  recovers to, and which generations a collector may remove.

The rationale, the failure-semantics table, and the ownership matrix are
recorded in ``docs/decisions/deployment-generations.md``.
"""
from __future__ import annotations

from . import generation, layout, ownership, plan, pointer

__all__ = ["generation", "layout", "ownership", "plan", "pointer"]
