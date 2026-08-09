"""Restricted Agent Skills definition loader."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from .. import paths
from .selector import parse_selector
from .values import (
    AgentSpec,
    AgentsLiveConfig,
    BrokenDefinition,
    Discovery,
    SkillProperties,
)

_STANDARD_FIELDS = {
    "name", "description", "license", "compatibility", "metadata", "allowed-tools",
}
_EXECUTION_FIELDS = {
    "agents-live.schema-version",
    "agents-live.schedule",
    "agents-live.watch",
    "agents-live.selector",
    "agents-live.mode",
    "agents-live.allow-tools",
    "agents-live.mcps",
    "agents-live.env",
    "agents-live.transcript",
    "agents-live.timeout",
    "agents-live.pre-processor",
    "agents-live.post-processor",
    "agents-live.output-schema",
    "agents-live.output-max-bytes",
    "agents-live.output-path-roots",
    "agents-live.output-provenance",
}
_RETIRED_FIELDS = {
    "runtime", "model", "schedule", "watchPath", "watchIgnore", "debounce",
    "mode", "allow-tools", "mcps", "env", "transcript", "timeout",
    "pre-processor", "post-processor", "handler", "owner", "output-schema",
    "output-max-bytes", "output-path-roots", "output-provenance", "tools",
    "user-invocable", "disable-model-invocation", "argument-hint",
}
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class DefinitionError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueLoader, node: MappingNode, deep: bool = False):
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise DefinitionError("frontmatter mapping keys must be strings")
        if key in result:
            raise DefinitionError(f"duplicate frontmatter key: {key}")
        if key == "<<":
            raise DefinitionError("YAML merge keys are not supported")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_definition(agent_id: str, *, root: Path) -> AgentSpec:
    repository = Path(root).resolve()
    explicit = Path(agent_id)
    if explicit.is_absolute() or len(explicit.parts) > 1:
        prompt = _explicit_prompt(explicit, repository)
        return _load_prompt(prompt, repository)
    discovery = discover_definitions(repository)
    matches = [
        spec for spec in discovery.specs
        if spec.identifier == agent_id or spec.name == agent_id
    ]
    if not matches:
        # Naming a definition that failed to parse has to report why it
        # failed, not that it is absent.
        for item in discovery.broken:
            if item.name == agent_id:
                raise DefinitionError(item.message)
        raise DefinitionError(f"definition not found: {agent_id}")
    exact = [spec for spec in matches if spec.identifier == agent_id]
    if exact:
        return exact[0]
    if len(matches) > 1:
        choices = ", ".join(spec.identifier for spec in matches)
        raise DefinitionError(
            f"definition name '{agent_id}' is ambiguous; use one of: {choices}")
    return matches[0]


def discover_definitions(root: Path) -> Discovery:
    """Load every definition in the repository, isolating the ones that fail.

    A definition that cannot be parsed is reported rather than raised, so one
    bad file does not make a whole repository look empty to convergence.
    """
    repository = Path(root).resolve()
    try:
        configured = paths.validated_agent_directories(
            repository,
            paths.load_config(repository).get("agent_directories", []),
        )
    except ValueError as exc:
        # Unreadable configuration leaves the discovery roots unknown, so
        # nothing about this repository can be trusted. That stays fatal.
        raise DefinitionError(str(exc)) from exc
    directories = [repository / "Agents", *configured]
    seen_directories: set[Path] = set()
    prompts: list[Path] = []
    broken: list[BrokenDefinition] = []
    for directory in directories:
        resolved = directory.resolve()
        if resolved in seen_directories or not directory.is_dir():
            continue
        seen_directories.add(resolved)
        for item in sorted(directory.glob("*.md")):
            if item.name == "_index_.md":
                continue
            try:
                if _is_flat_definition(item):
                    prompts.append(item)
            except DefinitionError as exc:
                broken.append(BrokenDefinition(item, str(exc)))
        prompts.extend(
            item / "SKILL.md" for item in sorted(directory.iterdir())
            if item.is_dir() and (item / "SKILL.md").is_file())
    specs: list[AgentSpec] = []
    for prompt in prompts:
        try:
            specs.append(_load_prompt(prompt, repository))
        except DefinitionError as exc:
            broken.append(BrokenDefinition(prompt, str(exc)))
    return Discovery(tuple(specs), tuple(broken))


def _is_flat_definition(prompt: Path) -> bool:
    try:
        text = prompt.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if not text.startswith("---\n"):
        return False
    try:
        frontmatter, _body = _extract(text, prompt)
    except DefinitionError:
        if "agents-live." in text:
            raise
        return False
    try:
        candidate = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        if "agents-live." in text:
            _parse(frontmatter, prompt)
        return False
    has_identity = isinstance(candidate, dict) and isinstance(
        candidate.get("name"), str) and isinstance(
        candidate.get("description"), str)
    has_retired_field = isinstance(candidate, dict) and bool(
        candidate.keys() & _RETIRED_FIELDS)
    if not has_identity and not has_retired_field and "agents-live." not in text:
        return False
    _parse(frontmatter, prompt)
    return True


def _load_prompt(prompt: Path, repository: Path) -> AgentSpec:
    prompt = prompt.resolve()
    try:
        prompt.relative_to(repository)
    except ValueError:
        raise DefinitionError(
            f"definition file escapes repository: {prompt}") from None
    if not prompt.is_file():
        raise DefinitionError(f"definition not found: {prompt}")
    skill_root = prompt.parent
    expected_name = skill_root.name if prompt.name == "SKILL.md" else prompt.stem
    try:
        text = prompt.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DefinitionError(f"definition is not readable as UTF-8: {prompt}") from exc
    frontmatter, body = _extract(text, prompt)
    data, metadata_nodes = _parse(frontmatter, prompt)
    # Frontmatter rules are stated per definition; the file they apply to is
    # named once here rather than in every message.
    try:
        properties = _properties(data, metadata_nodes, expected_name)
        execution = _execution(dict(properties.metadata), skill_root)
    except DefinitionError as exc:
        raise DefinitionError(f"{prompt}: {exc}") from None
    return AgentSpec(repository, skill_root, prompt, properties, execution, body)


def _explicit_prompt(candidate: Path, repository: Path) -> Path:
    resolved = candidate.resolve() if candidate.is_absolute() else (
        repository / candidate).resolve()
    try:
        resolved.relative_to(repository)
    except ValueError:
        raise DefinitionError(
            f"definition path escapes repository: {resolved}") from None
    return resolved / "SKILL.md" if resolved.is_dir() else resolved


def _extract(text: str, prompt: Path) -> tuple[str, str]:
    if text.startswith("\ufeff"):
        raise DefinitionError(f"UTF-8 byte-order marks are not supported: {prompt}")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise DefinitionError(f"frontmatter must start with an exact '---' line: {prompt}")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise DefinitionError(f"unterminated frontmatter: {prompt}") from None
    frontmatter = "\n".join(lines[1:end])
    if "\t" in frontmatter:
        raise DefinitionError(f"tabs are not supported in frontmatter: {prompt}")
    return frontmatter, "\n".join(lines[end + 1:]).strip()


def _parse(frontmatter: str, prompt: Path) -> tuple[dict[str, Any], dict[str, ScalarNode]]:
    try:
        for token in yaml.scan(frontmatter):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise DefinitionError(
                    f"anchors, aliases, and explicit tags are not supported: {prompt}")
        root = yaml.compose(frontmatter, Loader=_UniqueLoader)
        data = yaml.load(frontmatter, Loader=_UniqueLoader) or {}
    except DefinitionError:
        raise
    except yaml.YAMLError as exc:
        raise DefinitionError(f"invalid YAML frontmatter in {prompt}: {exc}") from exc
    if not isinstance(root, MappingNode) or not isinstance(data, dict):
        raise DefinitionError(f"frontmatter must be one mapping: {prompt}")
    metadata_nodes: dict[str, ScalarNode] = {}
    for key_node, value_node in root.value:
        if isinstance(key_node, ScalarNode) and key_node.value == "metadata":
            if not isinstance(value_node, MappingNode):
                raise DefinitionError(f"metadata must be a mapping: {prompt}")
            for metadata_key, metadata_value in value_node.value:
                if not isinstance(metadata_key, ScalarNode) or not isinstance(
                        metadata_value, ScalarNode):
                    raise DefinitionError(f"metadata keys and values must be strings: {prompt}")
                metadata_nodes[metadata_key.value] = metadata_value
    return data, metadata_nodes


def _properties(
    data: dict[str, Any],
    metadata_nodes: dict[str, ScalarNode],
    expected_name: str,
) -> SkillProperties:
    unknown = set(data) - _STANDARD_FIELDS
    retired = sorted(unknown & _RETIRED_FIELDS)
    if retired:
        raise DefinitionError(
            f"retired 5.x fields: {', '.join(retired)}; "
            "move execution policy under metadata with agents-live.* keys")
    if unknown:
        raise DefinitionError(f"unknown Agent Skills fields: {', '.join(sorted(unknown))}")
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _NAME.fullmatch(name):
        raise DefinitionError("name must be 1-64 lowercase alphanumeric or hyphen characters")
    if name != expected_name:
        raise DefinitionError(
            f"definition name '{name}' must match source name '{expected_name}'")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise DefinitionError("description must be a string of 1-1024 characters")
    license_name = _optional_string(data, "license")
    compatibility = _optional_string(data, "compatibility")
    if compatibility is not None and len(compatibility) > 500:
        raise DefinitionError("compatibility must not exceed 500 characters")
    allowed_tools = _optional_string(data, "allowed-tools")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise DefinitionError("metadata must be a mapping")
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise DefinitionError("metadata keys and values must be strings")
        node = metadata_nodes.get(key)
        if node is None or node.style not in {"'", '"'}:
            raise DefinitionError(f"metadata value for '{key}' must be quoted")
    return SkillProperties(
        name,
        description,
        license_name,
        compatibility,
        tuple(sorted(metadata.items())),
        allowed_tools,
    )


def _execution(metadata: dict[str, str], skill_root: Path) -> AgentsLiveConfig | None:
    owned = {key: value for key, value in metadata.items()
             if key.startswith("agents-live.")}
    if not owned:
        return None
    unknown = set(owned) - _EXECUTION_FIELDS
    if unknown:
        raise DefinitionError(
            f"unknown agents-live.* fields for schema version "
            f"{owned.get('agents-live.schema-version', '<missing>')}: "
            f"{', '.join(sorted(unknown))}")
    version = owned.get("agents-live.schema-version")
    if version != "1":
        raise DefinitionError(
            "agents-live.schema-version must be quoted \"1\" for this release")
    selector_text = owned.get("agents-live.selector")
    if not selector_text:
        raise DefinitionError("agents-live.selector is required")
    try:
        selector = parse_selector(selector_text)
    except ValueError as exc:
        raise DefinitionError(str(exc)) from exc
    mode = owned.get("agents-live.mode", "plan")
    if mode not in {"plan", "write", "pipeline"}:
        raise DefinitionError("agents-live.mode must be plan, write, or pipeline")
    schedules = _string_or_list(owned.get("agents-live.schedule"), "schedule")
    watch = owned.get("agents-live.watch")
    allow_tools = _string_list(owned.get("agents-live.allow-tools"), "allow-tools")
    mcps = _string_list(owned.get("agents-live.mcps"), "mcps")
    env = _string_map(owned.get("agents-live.env"), "env")
    transcript = _boolean(owned.get("agents-live.transcript"), True, "transcript")
    timeout = _positive_integer(owned.get("agents-live.timeout"), "timeout")
    pre = owned.get("agents-live.pre-processor")
    post = owned.get("agents-live.post-processor")
    _relative_file(pre, skill_root, "pre-processor")
    _relative_file(post, skill_root, "post-processor")
    if selector.provider == "none" and not (pre or post):
        raise DefinitionError("selector none requires a pre-processor or post-processor")
    output_schema = _json_or_path(
        owned.get("agents-live.output-schema"), skill_root, "output-schema")
    output_max = _positive_integer(
        owned.get("agents-live.output-max-bytes"), "output-max-bytes")
    output_roots = _string_list(
        owned.get("agents-live.output-path-roots"), "output-path-roots")
    for item in output_roots:
        _relative(item, skill_root, "output-path-roots")
    provenance = owned.get("agents-live.output-provenance")
    if provenance not in {None, "strict"}:
        raise DefinitionError("agents-live.output-provenance must be strict")
    if mode == "pipeline" and (output_schema or output_roots or provenance):
        raise DefinitionError("stdout output validation is unavailable in pipeline mode")
    return AgentsLiveConfig(
        version, schedules, watch, selector, mode, allow_tools, mcps,
        tuple(sorted(env.items())), transcript, timeout, pre, post,
        output_schema, output_max, output_roots, provenance,
    )


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise DefinitionError(f"{key} must be a string")
    return value


def _json(value: str, field: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise DefinitionError(f"agents-live.{field} must contain valid JSON") from exc


def _string_or_list(value: str | None, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if value.lstrip().startswith("["):
        return _string_list(value, field)
    return (value,)


def _string_list(value: str | None, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    parsed = _json(value, field)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise DefinitionError(f"agents-live.{field} must be a JSON string array")
    return tuple(parsed)


def _string_map(value: str | None, field: str) -> dict[str, str]:
    if value is None:
        return {}
    parsed = _json(value, field)
    if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in parsed.items()):
        raise DefinitionError(f"agents-live.{field} must be a JSON string map")
    return parsed


def _boolean(value: str | None, default: bool, field: str) -> bool:
    if value is None:
        return default
    if value not in {"true", "false"}:
        raise DefinitionError(f"agents-live.{field} must be true or false")
    return value == "true"


def _positive_integer(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise DefinitionError(f"agents-live.{field} must be a positive integer")
    return int(value)


def _relative(value: str, skill_root: Path, field: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise DefinitionError(f"agents-live.{field} must be relative to the skill")
    resolved = (skill_root / path).resolve()
    try:
        resolved.relative_to(skill_root)
    except ValueError:
        raise DefinitionError(f"agents-live.{field} escapes the skill directory") from None
    return resolved


def _relative_file(value: str | None, skill_root: Path, field: str) -> None:
    if value is not None:
        _relative(value, skill_root, field)


def _json_or_path(value: str | None, skill_root: Path, field: str) -> dict | str | None:
    if value is None:
        return None
    if value.lstrip().startswith("{"):
        parsed = _json(value, field)
        if not isinstance(parsed, dict):
            raise DefinitionError(f"agents-live.{field} must be a JSON object")
        return parsed
    _relative(value, skill_root, field)
    return value


def _migration_message(legacy: Path, skill_root: Path) -> str:
    return (
        f"{legacy}: this definition uses the 5.x flat format. 6.0 reads "
        f"{skill_root / 'SKILL.md'} with execution policy under metadata; "
        "run `agents-live migrate definitions` to convert it"
    )
