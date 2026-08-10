"""One-shot converter from 5.x flat definitions to Agent Skills."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
from pathlib import Path

import yaml

from ... import paths
from ...runtime import parse_schedule, parse_watch

_CONFLICTS = {
    "owner", "env", "tools", "user-invocable", "disable-model-invocation",
    "argument-hint",
}
_SUPPORTED = {
    "name", "description", "license", "compatibility", "metadata",
    "allowed-tools", "runtime", "model", "schedule", "watchPath",
    "watchIgnore", "debounce", "mode", "allow-tools", "mcps", "transcript",
    "timeout", "pre-processor", "post-processor", "handler", "output-schema",
    "output-max-bytes", "output-path-roots", "output-provenance",
    *_CONFLICTS,
}


class MigrationError(ValueError):
    pass


def convert(
    path: Path,
    *,
    root: Path,
    dry_run: bool = False,
    bundle: bool = False,
) -> Path:
    source = path.resolve()
    # Migration is run over many files at once, so every failure has to say
    # which one it was.
    try:
        return _convert(source, root=root, dry_run=dry_run, bundle=bundle)
    except MigrationError as exc:
        raise MigrationError(f"{source}: {exc}") from None


def _discovery_roots(root: Path) -> list[Path]:
    """Every directory a definition may live in, matching discovery."""
    configured = paths.validated_agent_directories(
        root, paths.load_config(root).get("agent_directories", []))
    found: list[Path] = []
    for directory in [root / "Agents", *configured]:
        resolved = directory.resolve()
        if resolved not in found and resolved.is_dir():
            found.append(resolved)
    return found


def _unmigrated(path: Path) -> bool:
    """False once the file carries agents-live metadata of its own.

    An unreadable file is left for the converter, which says what is wrong.
    """
    try:
        data, _ = _frontmatter(path.read_text(encoding="utf-8"))
    except (MigrationError, OSError, UnicodeError):
        return True
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return True
    return not any(
        isinstance(key, str) and key.startswith("agents-live.")
        for key in metadata)


def _convert(source: Path, *, root: Path, dry_run: bool, bundle: bool) -> Path:
    home = source.parent
    if home not in _discovery_roots(root) or source.suffix.lower() != ".md":
        raise MigrationError(
            "only a flat <root>/<name>.md definition in a discovery root "
            "can be migrated")
    text = source.read_text(encoding="utf-8")
    data, body = _frontmatter(text)
    conflicts = sorted(key for key in _CONFLICTS if key in data)
    if conflicts:
        raise MigrationError(
            f"needs a manual decision for: {', '.join(conflicts)}. "
            "Host assignment and secret-bearing values are not portable "
            "metadata: move 'owner' to the ownership registry by running "
            "`agents-live start --name <name>` on the owning host, supply "
            "'env' values from the host environment instead, then delete "
            "the field and migrate again.")
    unknown = sorted(set(data) - _SUPPORTED)
    if unknown:
        raise MigrationError(
            "unsupported fields that cannot be migrated safely: "
            f"{', '.join(unknown)}")
    name = source.stem
    declared_name = data.get("name")
    if declared_name not in (None, name):
        raise MigrationError(
            f"frontmatter name '{declared_name}' conflicts with file name '{name}'")
    description = data.get("description")
    if not isinstance(description, str) or not description:
        raise MigrationError("needs a nonempty description before migration")
    if not bundle:
        # Default: rewrite the frontmatter and leave everything else alone.
        # Relocating a processor changes what `__file__` resolves to, which
        # silently breaks any script that derives paths from its own depth.
        metadata, _ = _metadata(data, source, home, root, bundle=False)
        rendered = _render(name, description, data, metadata, body)
        if not dry_run:
            source.write_text(rendered, encoding="utf-8")
        return source
    destination = home / name
    if destination.exists():
        raise MigrationError(f"migration destination already exists: {destination}")
    metadata, copies = _metadata(data, source, destination, root, bundle=True)
    rendered = _render(name, description, data, metadata, body)
    if dry_run:
        return destination / "SKILL.md"
    destination.mkdir(parents=True)
    try:
        for original, relative in copies:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        (destination / "SKILL.md").write_text(rendered, encoding="utf-8")
        source.unlink()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination / "SKILL.md"


def _frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise MigrationError("no exact frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise MigrationError("unterminated frontmatter") from None
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise MigrationError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise MigrationError("frontmatter must be a mapping")
    return data, "\n".join(lines[end + 1:]).strip()


def _metadata(data: dict, source: Path, destination: Path, root: Path, *, bundle: bool):
    existing = data.get("metadata") or {}
    if not isinstance(existing, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in existing.items()):
        raise MigrationError("metadata must contain only string keys and values")
    collision = sorted(
        key for key in existing if key.startswith("agents-live."))
    if collision:
        raise MigrationError(
            "existing agents-live metadata requires a manual decision: "
            + ", ".join(collision))
    metadata: dict[str, str] = {
        **existing,
        "agents-live.schema-version": "1",
    }
    copies: list[tuple[Path, Path]] = []
    runtime = data.get("runtime")
    if not isinstance(runtime, str) or not runtime:
        raise MigrationError("has no runtime to convert to agents-live.selector")
    model = data.get("model")
    selector = runtime + (f"/{model}" if model else "")
    if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?",
            selector):
        raise MigrationError(
            f"runtime '{runtime}' has no selector spelling: a selector is "
            "provider[/model][:effort] and cannot contain spaces. A 5.x "
            "runtime that carried arguments has to be converted by hand to "
            "the provider name its plugin registers under 6.0.")
    metadata["agents-live.selector"] = selector
    schedules = data.get("schedule")
    if schedules not in (None, ""):
        values = schedules if isinstance(schedules, list) else [schedules]
        canonical = [parse_schedule(str(item)).canonical for item in values]
        metadata["agents-live.schedule"] = (
            canonical[0] if len(canonical) == 1
            else json.dumps(canonical, separators=(",", ":")))
    watch_paths = data.get("watchPath")
    if watch_paths not in (None, ""):
        values = watch_paths if isinstance(watch_paths, list) else [watch_paths]
        includes = [_watch_include(str(item), root) for item in values]
        ignores = data.get("watchIgnore") or []
        if isinstance(ignores, str):
            ignores = [ignores]
        excludes = [_watch_exclude(str(item)) for item in ignores]
        debounce = data.get("debounce", 1)
        expression = shlex.join(
            [*includes, *excludes, "debounce", f"{int(debounce)}s"])
        metadata["agents-live.watch"] = parse_watch(expression).canonical
    simple = {
        "mode": "agents-live.mode",
        "output-provenance": "agents-live.output-provenance",
    }
    for old, new in simple.items():
        if old in data:
            metadata[new] = str(data[old])
    arrays = {
        "allow-tools": "agents-live.allow-tools",
        "mcps": "agents-live.mcps",
        "output-path-roots": "agents-live.output-path-roots",
    }
    for old, new in arrays.items():
        if old not in data:
            continue
        value = data[old]
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise MigrationError(f"{old} must be a string or string list")
        metadata[new] = json.dumps(values, separators=(",", ":"))
    for old, new in (
        ("timeout", "agents-live.timeout"),
        ("output-max-bytes", "agents-live.output-max-bytes"),
    ):
        if old in data:
            metadata[new] = str(data[old])
    if "transcript" in data:
        metadata["agents-live.transcript"] = (
            "true" if bool(data["transcript"]) else "false")
    for old, new in (
        ("pre-processor", "agents-live.pre-processor"),
        ("post-processor", "agents-live.post-processor"),
        ("handler", "agents-live.post-processor"),
    ):
        if old not in data:
            continue
        if new in metadata:
            raise MigrationError("handler and post-processor are both declared")
        original = _old_reference(str(data[old]), source, root)
        relative = _reference(original, destination, Path("scripts"), bundle=bundle)
        metadata[new] = relative.as_posix()
        if bundle:
            copies.append((original, relative))
    if "output-schema" in data:
        schema = data["output-schema"]
        if isinstance(schema, dict):
            metadata["agents-live.output-schema"] = json.dumps(
                schema, sort_keys=True, separators=(",", ":"))
        elif isinstance(schema, str):
            original = _old_reference(schema, source, root)
            relative = _reference(
                original, destination, Path("references"), bundle=bundle)
            metadata["agents-live.output-schema"] = relative.as_posix()
            if bundle:
                copies.append((original, relative))
        else:
            raise MigrationError("output-schema must be a mapping or file reference")
    return metadata, copies


def _reference(
    original: Path, skill_root: Path, bundled_into: Path, *, bundle: bool
) -> Path:
    """Where the migrated definition should point at ``original``."""
    if bundle:
        return bundled_into / original.name
    try:
        return original.relative_to(skill_root)
    except ValueError:
        raise MigrationError(
            f"referenced file {original} is outside {skill_root}, so a flat "
            "definition cannot point at it; migrate with --bundle, or move "
            "the file under the discovery root") from None


def _old_reference(value: str, source: Path, root: Path) -> Path:
    original = (
        (root / value).resolve()
        if "/" in value or "\\" in value
        else (source.parent / "handlers" / value).resolve()
    )
    try:
        original.relative_to(root.resolve())
    except ValueError:
        raise MigrationError(f"referenced file escapes the repository: {value}") from None
    if not original.is_file():
        raise MigrationError(f"referenced file does not exist: {value}")
    return original


def _watch_include(value: str, root: Path) -> str:
    normalized = value.replace("\\", "/")
    if any(char in normalized for char in "*?["):
        return normalized
    directory_hint = normalized.endswith("/")
    normalized = normalized.rstrip("/")
    if directory_hint or (root / normalized).is_dir():
        return f"{normalized}/**"
    return normalized


def _watch_exclude(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.endswith("/"):
        return f"!{normalized}**"
    if "/" not in normalized:
        return f"!**/{normalized}"
    return f"!{normalized}"


def _render(name: str, description: str, source: dict,
            metadata: dict[str, str], body: str) -> str:
    lines = ["---", f"name: {json.dumps(name)}",
             f"description: {json.dumps(description)}"]
    for key in ("license", "compatibility", "allowed-tools"):
        value = source.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise MigrationError(f"{key} must be a string")
            lines.append(f"{key}: {json.dumps(value)}")
    lines.append("metadata:")
    for key, value in metadata.items():
        lines.append(f"  {key}: {json.dumps(value)}")
    lines.extend(("---", body, ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert 5.x flat definitions.")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bundle", action="store_true")
    args = parser.parse_args(argv)
    root = paths.resolve_root()
    if args.paths:
        selected = [Path(item) for item in args.paths]
    else:
        # A scan reports what still needs converting; a file that already
        # carries agents-live metadata is not a 5.x definition.
        selected = [
            item
            for directory in _discovery_roots(root)
            for item in sorted(directory.glob("*.md"))
            if item.name != "_index_.md" and _unmigrated(item)
        ]
        if not selected:
            print("No 5.x definitions to migrate.")
            return 0
    failed = False
    for item in selected:
        try:
            destination = convert(
                item, root=root, dry_run=args.dry_run, bundle=args.bundle)
        except (MigrationError, OSError, ValueError) as exc:
            failed = True
            print(str(exc), file=sys.stderr)
        else:
            verb = "Would convert" if args.dry_run else "Converted"
            if destination == Path(item).resolve():
                print(f"{verb} {item}")
            else:
                print(f"{verb} {item} -> {destination}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
