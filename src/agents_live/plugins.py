"""Project-declared plugins, loaded from source against a seam protocol.

A plugin is a Python module or package inside the repository that
declares it. It is imported directly and its exposed objects are handed
to the seam; nothing is installed.

The declaration format already forbade everything installation bought:
:func:`paths.validated_plugins` rejects an absolute path and any path
resolving outside the declaring repository, so a plugin could never come
from a package index. See docs/decisions/plugin-loading.md.

Loading never raises. A plugin that cannot be found, hashed, imported, or
recognized is recorded as a failure and reported by ``doctor``; one bad
plugin must not take down every command, including ``--help``.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .agent import providers as provider_plugins
from .state import ownership

#: What a loaded module may expose, and which seam each attribute feeds.
PROVIDER_ATTRS = ("PROVIDERS", "PROVIDER")
OWNERSHIP_ATTR = "OWNERSHIP_REGISTRY"

#: Modules are registered in ``sys.modules`` because dataclasses,
#: pickling, and ``typing.get_type_hints`` resolve through it. The key is
#: namespaced rather than the file's own name: two repositories may each
#: declare ``plugin.py``, and pytest documents exactly this collision for
#: ``conftest.py`` files that are not inside a package.
_MODULE_PREFIX = "agents_live._plugins"

# PEP 723 inline metadata. Formally a script format; a plugin is imported
# rather than run, so the header is read as an advisory declaration and
# nothing is ever installed from it.
_PEP723 = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class PluginError(RuntimeError):
    """A plugin declaration cannot be safely resolved or loaded."""


@dataclass(frozen=True)
class Plugin:
    """One declaration, resolved against the repository that made it."""

    name: str
    path: Path
    sha256: str | None
    root: Path

    @property
    def module_name(self) -> str:
        """The ``sys.modules`` key this plugin is registered under."""
        stem = re.sub(r"[^A-Za-z0-9_]", "_", self.name)
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:8]
        return f"{_MODULE_PREFIX}.{digest}_{stem}"

    @property
    def entry_file(self) -> Path:
        """The file the import machinery executes for this plugin."""
        return self.path / "__init__.py" if self.path.is_dir() else self.path


@dataclass(frozen=True)
class Loaded:
    """What one declaration contributed, or why it contributed nothing."""

    plugin: Plugin
    ok: bool
    detail: str


def declared(root: Path, *, require_exists: bool = True) -> dict[str, Plugin]:
    """Resolve one repository's declarations without importing them."""
    declarations = paths.validated_plugins(
        root, paths.load_config(root).get("plugins", {}),
        require_exists=require_exists)
    return {
        name: Plugin(name, declaration["path"], declaration["sha256"], root)
        for name, declaration in declarations.items()
    }


def union(roots: list[Path], *, require_exists: bool = True
          ) -> dict[str, Plugin]:
    """Combine declarations across repositories; first declaration wins.

    Two repositories naming the same plugin is not worth refusing over:
    each loads under its own module key, and the seam registry rejects a
    genuine duplicate registration itself.
    """
    result: dict[str, Plugin] = {}
    for root in roots:
        for name, plugin in declared(
                root, require_exists=require_exists).items():
            result.setdefault(name, plugin)
    return result


def _digest(path: Path) -> str:
    """SHA-256 of a file, or of a directory's sorted relative contents."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    running = hashlib.sha256()
    for item in sorted(item for item in path.rglob("*") if item.is_file()):
        running.update(
            str(item.relative_to(path)).replace("\\", "/").encode("utf-8"))
        running.update(item.read_bytes())
    return running.hexdigest()


def integrity_error(plugin: Plugin) -> str | None:
    """Whether the declared digest still matches what is on disk."""
    if plugin.sha256 is None:
        return None
    try:
        actual = _digest(plugin.path)
    except OSError as exc:
        return f"cannot hash {plugin.path}: {exc}"
    if actual.lower() != plugin.sha256.lower():
        return f"sha256 mismatch for {plugin.path}"
    return None


def requirements(source: Path) -> tuple[str, ...]:
    """Distribution names a plugin's PEP 723 header declares, if any."""
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    blocks = [
        match for match in _PEP723.finditer(text)
        if match.group("type") == "script"
    ]
    if len(blocks) != 1:
        return ()
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in blocks[0].group("content").splitlines(keepends=True))
    try:
        document = tomllib.loads(content)
    except (ValueError, TypeError):
        return ()
    declared_requirements = document.get("dependencies")
    if not isinstance(declared_requirements, list):
        return ()
    names = []
    for item in declared_requirements:
        if not isinstance(item, str):
            continue
        match = _REQUIREMENT_NAME.match(item.strip())
        if match:
            names.append(match.group(0))
    return tuple(names)


def missing_requirements(plugin: Plugin) -> tuple[str, ...]:
    """Declared distributions this runtime cannot supply.

    Names only. A plugin runs inside the Agents Live runtime and may use
    only that runtime's dependencies, so the useful answer is "absent",
    not "slightly wrong version". Nothing is installed to satisfy this.
    """
    absent = []
    for name in requirements(plugin.entry_file):
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            absent.append(name)
        except (OSError, ValueError):
            continue
    return tuple(absent)


def _import(plugin: Plugin):
    """Import a plugin's module under its namespaced key."""
    entry = plugin.entry_file
    if not entry.is_file():
        raise PluginError(f"{plugin.path} is not a module or package")
    existing = sys.modules.get(plugin.module_name)
    if existing is not None:
        return existing
    locations = [str(plugin.path)] if plugin.path.is_dir() else None
    spec = importlib.util.spec_from_file_location(
        plugin.module_name, entry, submodule_search_locations=locations)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot import {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[plugin.module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[plugin.module_name]
        raise
    return module


def _attach(module) -> str:
    """Hand a loaded module's exposed objects to the seams they name."""
    attached = []
    providers = None
    for attribute in PROVIDER_ATTRS:
        if hasattr(module, attribute):
            providers = getattr(module, attribute)
            break
    if providers is not None:
        candidates = (
            providers if isinstance(providers, (list, tuple)) else [providers])
        for candidate in candidates:
            provider_plugins.register(candidate)
            attached.append(f"provider {candidate.name}")
    registry = getattr(module, OWNERSHIP_ATTR, None)
    if registry is not None:
        ownership.use_backend(registry)
        attached.append("ownership registry")
    if not attached:
        raise PluginError(
            f"exposes none of {', '.join((*PROVIDER_ATTRS, OWNERSHIP_ATTR))}")
    return ", ".join(attached)


def load(roots: list[Path]) -> tuple[Loaded, ...]:
    """Load every declared plugin, recording failures rather than raising.

    Idempotent: a module already in ``sys.modules`` under its namespaced
    key is reused, and the seam registries accept the same object twice.
    Repositories are resolved independently so one malformed declaration
    cannot hide healthy plugins from another repository.
    """
    results: list[Loaded] = []
    declarations: list[Plugin] = []
    for root in roots:
        try:
            declarations.extend(
                declared(root, require_exists=False).values())
        except (OSError, ValueError, PluginError) as exc:
            results.append(Loaded(
                Plugin("<declarations>", Path(), None, root),
                False, str(exc)))
    for plugin in declarations:
        if not plugin.entry_file.is_file():
            results.append(Loaded(
                plugin, False, f"{plugin.path} is not a module or package"))
            continue
        broken = integrity_error(plugin)
        if broken:
            results.append(Loaded(plugin, False, broken))
            continue
        absent = missing_requirements(plugin)
        if absent:
            results.append(Loaded(
                plugin, False,
                f"declares {', '.join(absent)}, which this runtime does not "
                "provide"))
            continue
        try:
            detail = _attach(_import(plugin))
        except Exception as exc:
            results.append(Loaded(plugin, False, f"{type(exc).__name__}: {exc}"))
            continue
        results.append(Loaded(plugin, True, detail))
    return tuple(results)


def checks(root: Path, *, require_exists: bool = True
           ) -> list[tuple[str, bool, str]]:
    """Per-plugin health for one repository, for ``doctor``."""
    return [(item.plugin.name, item.ok, item.detail) for item in load([root])]


def validation_errors(roots: list[Path]) -> tuple[str, ...]:
    """Declarations that cannot load, as operator-readable messages."""
    return tuple(
        f"plugin {item.plugin.name!r}: {item.detail}"
        for item in load(roots) if not item.ok)


def compatibility_errors(roots: list[Path], *, runtime_requirement: str
                         ) -> tuple[str, ...]:
    """Declared plugins that cannot load under this runtime.

    Source plugins load in this interpreter, so the candidate runtime is
    the one already running and there is nothing to probe in a
    subprocess.
    """
    return validation_errors(roots)


def installed_version() -> str:
    """The agents-live version present in this environment right now.

    Read back from installed metadata rather than assumed: after a
    generation switch the running process still holds the old code, so
    ``__version__`` reports the version that started the command, not the
    one that is now selected.
    """
    try:
        return importlib.metadata.version("agents-live")
    except importlib.metadata.PackageNotFoundError:
        from . import __version__

        return __version__
