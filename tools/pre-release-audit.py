#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Pre-release audit: scan for personal information, secrets, and portability issues.

Run from repo root:
    uv run --script .claude/skills/agents-live/scripts/pre-release-audit.py

Exit code 0 = pass, 1 = findings reported.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# The adapter-resolution check imports the packaged registry; never let
# that import write __pycache__ into the tree being audited.
sys.dont_write_bytecode = True

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directories included in a release (relative to repo root). Covers
# both shapes: the life checkout (skill dir) and the assembled package
# export (src/ + tools/, Phase 4) - nonexistent entries are skipped.
INCLUDED_DIRS = [
    ".claude/skills/agents-live",
    ".agents",
    "Agents",
    "src",
    "tools",
]
INCLUDED_FILES = ["AGENTS.md", "README.md", "pyproject.toml"]
MACHINE_NAMES_FILE = ".agents-live-machine-names"

# Directories/files that are always excluded even inside included dirs
EXCLUDED_PATTERNS = {
    "Agents/logs",
    "Agents/data",
    ".git",
    "__pycache__",
    "node_modules",
}

# File extensions to scan
TEXT_EXTENSIONS = {
    ".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml",
    ".txt", ".cfg", ".ini", ".env",
}

# Patterns that indicate potential secrets
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("API key",        re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("Bearer token",   re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.]{20,}")),
    ("Private key",    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----")),
    ("AWS key",        re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token",   re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}")),
    ("Generic secret", re.compile(r"(?i)(secret|password|passwd|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]

# Patterns that indicate hardcoded personal paths
PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Home directory",  re.compile(r"/home/[a-z][a-z0-9_-]+/(?!runner/)")),
    ("Windows user",    re.compile(r"C:\\Users\\[A-Za-z][A-Za-z0-9_-]+\\")),
    ("macOS user",      re.compile(r"/Users/[A-Za-z][A-Za-z0-9_-]+/")),
    # Tilde forms bypass the absolute-path patterns above; the
    # maintainer's personal project checkout must never ship.
    ("Personal repo path", re.compile(r"\brepos/life\b")),
]

# Patterns that indicate personal information (names, emails)
PERSONAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Email address", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
]

# Known-safe patterns to suppress false positives
SAFE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"example\.com"),
    re.compile(r"user@"),
    re.compile(r"someone@"),
    re.compile(r"noreply@"),
    re.compile(r"\.token-cache\.json"),  # Path pattern, not a token
    re.compile(r"WORKIQ_MAIL_TOKEN_CACHE_PATH"),  # Env var name, not value
    re.compile(r"MS365_MCP_TOKEN_CACHE_PATH"),
    re.compile(r"/home/runner/"),  # CI runner paths are fine
    re.compile(r"~/.config/"),  # Standard XDG paths with tilde are portable
    re.compile(r"Path\.home\(\)"),  # Python dynamic home resolution
    re.compile(r"/home/user/"),  # Generic doc example paths
    re.compile(r"/home/you/"),  # Generic doc example paths
    re.compile(r"alice@|bob@|jane@"),  # Common doc example names
]


def repo_root() -> Path:
    """Resolve the repo root via the shared paths resolver."""
    script_dir = Path(__file__).resolve().parent
    assembled_root = script_dir.parent
    if script_dir.name == "tools" and (assembled_root / "pyproject.toml").is_file():
        return assembled_root
    source_dir = script_dir.parent / "src" / "agents_live"
    sys.path.insert(0, str(source_dir if source_dir.is_dir() else script_dir))
    from paths import resolve_root  # noqa: PLC0415
    return resolve_root()


def is_export_shape(root: Path) -> bool:
    """True when auditing the assembled release output (tools/ beside
    pyproject.toml) rather than the life checkout."""
    return (root / "tools").is_dir() and (root / "pyproject.toml").is_file()


# ---------------------------------------------------------------------------
# Release-only checks (export shape): adapter resolution + doc links
# ---------------------------------------------------------------------------

_FRONTMATTER_RUNTIME = re.compile(r"^runtime:\s*(.+?)\s*$", re.MULTILINE)
_MD_LINK = re.compile(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)")


def _frontmatter_block(text: str) -> str | None:
    """Return the leading YAML frontmatter block, or None."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    return text[4:end] if end != -1 else None


def _declared_runtime(text: str) -> str | None:
    """Extract the `runtime:` value from a file's frontmatter, stripping
    inline YAML comments and quotes (stdlib-only; the templates keep
    frontmatter values simple scalars)."""
    block = _frontmatter_block(text)
    if block is None:
        return None
    match = _FRONTMATTER_RUNTIME.search(block)
    if match is None:
        return None
    value = match.group(1).split(" #")[0].strip()
    return value.strip("'\"") or None


def _exported_adapter_runtimes(root: Path) -> set[str]:
    """Runtimes resolvable in the released package: the packaged registry
    minus adapters marked private (deployment-only plugins never ship)."""
    sys.path.insert(0, str(root / "src" / "agents_live"))
    import agent_adapters  # noqa: PLC0415
    public = {n for n in agent_adapters.names()
              if not agent_adapters.get(n).private}
    return public | {"none"}


def check_agent_adapters(root: Path, files: list[Path]) -> list[str]:
    """Release gate: every exported agent or template that declares a
    `runtime:` must resolve through the exported adapter registry
    (cross-review 2026-07-12 High: a release must not ship
    agency-dependent agents - ship public-adapter prompts or omit)."""
    findings: list[str] = []
    allowed = _exported_adapter_runtimes(root)
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        try:
            runtime = _declared_runtime(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if runtime is not None and runtime not in allowed:
            findings.append(
                f"  {path.relative_to(root)}: runtime '{runtime}' does not "
                f"resolve in the exported adapter registry "
                f"({', '.join(sorted(allowed))})")
    return findings


def check_doc_links(root: Path, files: list[Path]) -> list[str]:
    """Every relative .md link in the export must resolve inside the
    export - a link to a doc that did not ship is a dangling reference
    in the published package."""
    findings: list[str] = []
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_fence = False
        for line_num, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in _MD_LINK.finditer(line):
                target = match.group(1)
                if target.startswith(("http://", "https://", "/", "skill://")):
                    continue
                resolved = (path.parent / target.replace("%20", " ")).resolve()
                if not resolved.is_file():
                    findings.append(
                        f"  {path.relative_to(root)}:{line_num}: dangling "
                        f"doc link '{target}' (target not in the release)")
    return findings


def should_scan(path: Path, root: Path) -> bool:
    """Return True if the file should be scanned."""
    relative = path.relative_to(root)
    rel_str = str(relative)

    # Skip excluded directories
    for excluded in EXCLUDED_PATTERNS:
        if rel_str.startswith(excluded) or f"/{excluded}/" in f"/{rel_str}/":
            return False

    # Skip hidden files/dirs (except .claude, .agents)
    parts = relative.parts
    for part in parts[:-1]:  # Check parent dirs
        if part.startswith(".") and part not in {".claude", ".agents"}:
            return False

    # Only scan text files
    return path.suffix.lower() in TEXT_EXTENSIONS


def collect_files(root: Path) -> list[Path]:
    """Collect all files to scan."""
    files: list[Path] = []

    for dir_path in INCLUDED_DIRS:
        full = root / dir_path
        if full.is_dir():
            for path in sorted(full.rglob("*")):
                if path.is_file() and should_scan(path, root):
                    files.append(path)

    for file_name in INCLUDED_FILES:
        full = root / file_name
        if full.is_file():
            files.append(full)

    return files


def load_machine_names(root: Path) -> list[str]:
    """Load optional local machine names, ignoring blank and comment lines."""
    path = root / MACHINE_NAMES_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def is_safe_match(line: str) -> bool:
    """Return True if the match is a known false positive."""
    return any(pat.search(line) for pat in SAFE_PATTERNS)


def scan_file(
    path: Path, root: Path, machine_names: list[str] | None = None,
) -> list[str]:
    """Scan a single file and return a list of findings."""
    findings: list[str] = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    relative = path.relative_to(root)
    lines = content.splitlines()
    names = machine_names or []

    for line_num, line in enumerate(lines, 1):
        if path.suffix.lower() == ".md" and "—" in line:
            findings.append(f"  {relative}:{line_num}: Em dash in shipped Markdown")

        folded = line.casefold()
        for name in names:
            if name.casefold() in folded:
                findings.append(
                    f"  {relative}:{line_num}: Known machine name from "
                    f"{MACHINE_NAMES_FILE} ({name!r})")

        if is_safe_match(line):
            continue

        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"  {relative}:{line_num}: {label}")

        for label, pattern in PATH_PATTERNS:
            if pattern.search(line):
                findings.append(f"  {relative}:{line_num}: Hardcoded {label}")

        for label, pattern in PERSONAL_PATTERNS:
            if pattern.search(line):
                findings.append(f"  {relative}:{line_num}: {label}")

    return findings


def main() -> int:
    root = repo_root()
    files = collect_files(root)
    machine_names = load_machine_names(root)

    if not files:
        print("No files found to scan.", file=sys.stderr)
        return 1

    all_findings: list[str] = []
    for path in files:
        all_findings.extend(scan_file(path, root, machine_names))

    # Release-shape-only checks: they validate THE ASSEMBLED EXPORT
    # (adapter resolution against the packaged registry, links against
    # the shipped file set); the life checkout resolves both trivially.
    release_checks = ""
    if is_export_shape(root):
        all_findings.extend(check_agent_adapters(root, files))
        all_findings.extend(check_doc_links(root, files))
        release_checks = " (incl. adapter-resolution + doc-link checks)"

    print(f"Pre-release audit: scanned {len(files)} files{release_checks}")
    print()

    if all_findings:
        print(f"⚠️  {len(all_findings)} finding(s):")
        print()
        for finding in all_findings:
            print(finding)
        print()
        print("Review each finding. If it is a false positive, add a safe")
        print("pattern to SAFE_PATTERNS in this script.")
        return 1

    print("✅ No issues found. Ready for release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
