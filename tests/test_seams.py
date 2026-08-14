from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from agents_live import (
    agent, obs, paths, plugins, runtime, state,
)
from agents_live.cli import lifecycle, upgrade_handoff
from agents_live.cli.commands import doctor, init, internal, run, status, stop, uninstall, upgrade
from agents_live.state import registry as repos
from agents_live.cli.spec import COMMANDS
from agents_live.legacy import agent_adapters, health_check, triggers
from agents_live.agent import providers
from agents_live.state import ownership
from agents_live.dispatch import Firing, _RunLock, dispatch
from agents_live.cli.commands import definition_migrate
from agents_live.cli.commands.definition_migrate import MigrationError, convert
from agents_live.runtime import (
    ChildResult,
    Health,
    InstalledTrigger,
    ProcessRef,
    Subscription,
    WatchSyntaxError,
    converge,
    diff,
    parse_schedule,
    parse_watch,
)
from agents_live.runtime.budget import claim as claim_budget
from agents_live.runtime.hosts.processes import LocalChildRunner
from agents_live.runtime.hosts.posix import PosixHost
from agents_live.runtime.hosts.memory import MemoryHost
from agents_live.runtime.hosts.windows import WindowsProcesses
from agents_live.runtime.hosts import system as hostruntime, task_scheduler


# The repository registry lives under the data home, not the state home, so
# isolating only the latter leaves a test writing the developer's own registry.
_ISOLATED_HOMES = {
    "XDG_STATE_HOME": "state",
    "XDG_DATA_HOME": "data",
    "XDG_CONFIG_HOME": "config",
}


class TempRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "Agents").mkdir()
        self.previous_repo = os.environ.get("AGENTS_LIVE_REPO")
        os.environ["AGENTS_LIVE_REPO"] = str(self.root)
        self.previous_root_cache = (
            paths._cached_default_root, paths._cached_default_source)
        paths._cached_default_root = None
        paths._cached_default_source = None
        self.previous_homes = {
            name: os.environ.get(name) for name in _ISOLATED_HOMES}
        for name, directory in _ISOLATED_HOMES.items():
            os.environ[name] = str(self.root / directory)

    def tearDown(self) -> None:
        if self.previous_repo is None:
            os.environ.pop("AGENTS_LIVE_REPO", None)
        else:
            os.environ["AGENTS_LIVE_REPO"] = self.previous_repo
        (paths._cached_default_root,
         paths._cached_default_source) = self.previous_root_cache
        for name, value in self.previous_homes.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def skill(self, name: str, metadata: list[str], body: str = "Do the work.") -> Path:
        directory = self.root / "Agents" / name
        directory.mkdir(parents=True)
        text = "\n".join([
            "---",
            f"name: {name}",
            "description: A portable test definition.",
            "metadata:",
            '  agents-live.schema-version: "1"',
            *[f"  {line}" for line in metadata],
            "---",
            body,
            "",
        ])
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        return directory


class TestDefinitionLoader(TempRepository):
    def test_loads_quoted_namespaced_metadata(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake/echo:high"',
            'agents-live.schedule: "0 8 * JAN MON"',
            "other-client.value: \"preserved\"",
        ])
        spec = agent.load("sample", root=self.root)
        self.assertEqual("sample", spec.name)
        self.assertEqual("fake/echo:high", spec.execution.selector.canonical)
        self.assertIn(("other-client.value", "preserved"), spec.properties.metadata)

    def test_an_unrecognised_execution_key_is_additive_not_fatal(self) -> None:
        """A key added by a later release describes a capability this one lacks.

        Ignoring it runs the rest of the definition; a change to what an
        existing key means raises the schema version instead.
        """
        self.skill("forward", [
            'agents-live.selector: "fake/echo"',
            'agents-live.schedule: "0 8 * * *"',
            'agents-live.sandbox: "strict"',
        ])
        spec = agent.load("forward", root=self.root)
        self.assertEqual("fake/echo", spec.execution.selector.canonical)
        self.assertEqual(("0 8 * * *",), spec.execution.schedules)
        self.assertEqual(("agents-live.sandbox",), spec.unknown_metadata)

        rows = status._rows(self.root)
        self.assertEqual(
            ["agents-live.sandbox"], rows[0]["unknown_metadata"])

    def test_rejects_unquoted_metadata_duplicate_keys_and_aliases(self) -> None:
        bad = (
            ("unquoted", '  agents-live.selector: fake\n'),
            ("duplicate", '  agents-live.selector: "fake"\n  agents-live.selector: "fake"\n'),
            ("alias", '  shared: &value "fake"\n  agents-live.selector: *value\n'),
        )
        for name, fragment in bad:
            with self.subTest(name=name):
                directory = self.root / "Agents" / name
                directory.mkdir()
                (directory / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Invalid fixture.\nmetadata:\n"
                    '  agents-live.schema-version: "1"\n'
                    f"{fragment}---\nbody\n",
                    encoding="utf-8",
                )
                with self.assertRaises(agent.DefinitionError):
                    agent.load(name, root=self.root)

    def test_one_shot_migration_preserves_other_metadata_and_refuses_unknowns(self) -> None:
        legacy = self.root / "Agents" / "legacy.md"
        legacy.write_text(
            "---\n"
            "description: Legacy fixture.\n"
            "runtime: fake\n"
            "metadata:\n"
            "  other-client.value: preserved\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        converted = convert(legacy, root=self.root)
        self.assertEqual("preserved", dict(
            agent.load("legacy", root=self.root).properties.metadata
        )["other-client.value"])
        bad = self.root / "Agents" / "bad.md"
        bad.write_text(
            "---\ndescription: Bad fixture.\nruntime: fake\nmystery: value\n---\n",
            encoding="utf-8",
        )
        with self.assertRaises(MigrationError):
            convert(bad, root=self.root)

    def test_migration_carries_every_field_the_loader_accepts(self) -> None:
        source = self.root / "Agents" / "full.md"
        source.write_text(
            "---\n"
            "description: A fully specified 5.x definition.\n"
            "runtime: fake\n"
            "schedule: 0 8 * * *\n"
            "allow-tools:\n"
            "  - Read\n"
            "  - Write\n"
            "mode: write\n"
            "timeout: 300\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        convert(source, root=self.root)
        config = agent.load("full", root=self.root).execution
        self.assertEqual(("Read", "Write"), config.allow_tools)
        self.assertEqual("write", config.mode)
        self.assertEqual(300, config.timeout)

    def test_migration_preserves_files_and_expands_directories(self) -> None:
        (self.root / "watched.md").write_text("watched\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        source = self.root / "Agents" / "watcher.md"
        source.write_text(
            "---\ndescription: Watch fixture.\nruntime: fake\nwatchPath:\n"
            "  - watched.md\n"
            "  - docs\n"
            "  - src/*.py\n"
            "  - later.md\n"
            "  - 'win\\*.txt'\n"
            "  - 'archive\\'\n"
            "---\nbody\n",
            encoding="utf-8",
        )

        convert(source, root=self.root)
        watch = parse_watch(agent.load("watcher", root=self.root).execution.watch)
        self.assertEqual((
            "archive/**",
            "docs/**",
            "later.md",
            "src/*.py",
            "watched.md",
            "win/*.txt",
        ), watch.includes)
        self.assertTrue(watch.matches("watched.md"))
        self.assertFalse(watch.matches("watched.md/child"))

    def test_migration_failures_name_the_file(self) -> None:
        source = self.root / "Agents" / "assigned.md"
        source.write_text(
            "---\ndescription: Owned.\nruntime: fake\nowner: some-host\n---\nbody\n",
            encoding="utf-8",
        )
        with self.assertRaises(MigrationError) as caught:
            convert(source, root=self.root)
        self.assertIn("assigned.md", str(caught.exception))
        self.assertIn("ownership registry", str(caught.exception))

    def test_migration_leaves_processors_where_they_are(self) -> None:
        """Relocating a processor changes what its own path means.

        5.x handlers commonly derive the repository root from `__file__`
        depth, so moving one into a bundle silently breaks it. The default
        conversion rewrites frontmatter and touches nothing else.
        """
        handlers = self.root / "Agents" / "handlers"
        handlers.mkdir()
        processor = handlers / "report.py"
        processor.write_text("print('ok')\n", encoding="utf-8")
        source = self.root / "Agents" / "reporter.md"
        source.write_text(
            "---\ndescription: Reporter.\nruntime: none\n"
            "post-processor: Agents/handlers/report.py\n---\nbody\n",
            encoding="utf-8",
        )

        self.assertEqual(source.resolve(), convert(source, root=self.root))
        self.assertTrue(source.is_file(), "the definition stays where it was")
        self.assertTrue(processor.is_file(), "the processor is not moved")
        self.assertFalse((self.root / "Agents" / "reporter").exists())

        spec = agent.load("reporter", root=self.root)
        self.assertEqual("handlers/report.py", spec.execution.post_processor)
        self.assertEqual(
            processor.resolve(),
            (spec.skill_root / spec.execution.post_processor).resolve())

    def test_migration_reaches_every_discovery_root(self) -> None:
        """6.0 ships no old-format loader, so migration is the only door.

        A repository with configured roots was told to migrate and then had
        the definitions in those roots left behind, unrunnable.
        """
        (self.root / ".agents-live.toml").write_text(
            'agent_directories = ["Extra/agents"]\n', encoding="utf-8")
        extra = self.root / "Extra" / "agents"
        extra.mkdir(parents=True)
        source = extra / "outlying.md"
        source.write_text(
            "---\ndescription: Outside Agents/.\nruntime: none\n"
            'schedule: "0 8 * * *"\npost-processor: Extra/agents/report.py\n'
            "---\nbody\n",
            encoding="utf-8",
        )
        (extra / "report.py").write_text("print('ok')\n", encoding="utf-8")

        with (
            mock.patch.object(
                definition_migrate.paths, "resolve_root", return_value=self.root),
            contextlib.redirect_stdout(io.StringIO()) as scanned,
        ):
            self.assertEqual(0, definition_migrate.main(["--dry-run"]))
        self.assertIn("Would convert", scanned.getvalue())
        self.assertNotIn("agents-live.", source.read_text(encoding="utf-8"))

        with (
            mock.patch.object(
                definition_migrate.paths, "resolve_root", return_value=self.root),
            contextlib.redirect_stdout(io.StringIO()) as scanned,
        ):
            self.assertEqual(0, definition_migrate.main([]))
        self.assertIn("Converted", scanned.getvalue())
        spec = agent.load("outlying", root=self.root)
        self.assertEqual("report.py", spec.execution.post_processor)

    def test_bundle_migration_is_available_and_carries_processors(self) -> None:
        handlers = self.root / "Agents" / "handlers"
        handlers.mkdir()
        (handlers / "report.py").write_text("print('ok')\n", encoding="utf-8")
        source = self.root / "Agents" / "bundled.md"
        source.write_text(
            "---\ndescription: Bundled.\nruntime: none\n"
            "post-processor: Agents/handlers/report.py\n---\nbody\n",
            encoding="utf-8",
        )

        destination = convert(source, root=self.root, bundle=True)
        self.assertEqual(
            self.root / "Agents" / "bundled" / "SKILL.md", destination)
        self.assertFalse(source.exists())
        spec = agent.load("bundled", root=self.root)
        self.assertEqual("scripts/report.py", spec.execution.post_processor)
        self.assertTrue((spec.skill_root / "scripts" / "report.py").is_file())

    def test_configured_flat_skills_have_path_derived_identifiers(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            'agent_directories = ["foo", "bar"]\n', encoding="utf-8")
        for directory_name in ("foo", "bar"):
            directory = self.root / directory_name
            directory.mkdir()
            (directory / "README.md").write_text(
                "# Supporting documentation\n", encoding="utf-8")
            (directory / "verify-links.md").write_text(
                "---\n"
                "name: verify-links\n"
                "description: Verify links in repository documentation.\n"
                "metadata:\n"
                '  agents-live.schema-version: "1"\n'
                '  agents-live.selector: "fake"\n'
                '  agents-live.schedule: "0 8 * * *"\n'
                "---\n"
                "Verify the links.\n",
                encoding="utf-8",
            )

        specs = [spec for spec in agent.discover(self.root).specs
                 if spec.name == "verify-links"]
        self.assertEqual(2, len(specs))
        self.assertEqual(2, len({spec.identifier for spec in specs}))
        self.assertTrue(all(
            spec.identifier.startswith("verify-links-") for spec in specs))
        for spec in specs:
            self.assertEqual(spec.prompt_path, agent.load(
                spec.identifier, root=self.root).prompt_path)
        with self.assertRaisesRegex(agent.DefinitionError, "ambiguous"):
            agent.load("verify-links", root=self.root)

    def test_rejects_retired_fields_in_flat_and_bundle_formats(self) -> None:
        (self.root / "Agents" / "old.md").write_text(
            "---\ndescription: old\nruntime: none\n---\nold\n", encoding="utf-8")
        with self.assertRaisesRegex(agent.DefinitionError, "retired"):
            agent.load("old", root=self.root)
        self.skill("retired", [
            'agents-live.selector: "fake"',
        ])
        prompt = self.root / "Agents" / "retired" / "SKILL.md"
        prompt.write_text(
            prompt.read_text(encoding="utf-8").replace(
                "metadata:", "runtime: fake\nmetadata:"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(agent.DefinitionError, "retired"):
            agent.load("retired", root=self.root)


class TestDoctor(unittest.TestCase):
    def _run(self, argv: list[str], initial: Health, result,
             *, json_mode: bool = False) -> tuple[int, str]:
        collected = mock.Mock(
            unavailable_repositories=(), broken_definitions=(),
            unknown_metadata=())
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"AGENTS_LIVE_JSON": "1" if json_mode else ""},
            ),
            mock.patch.object(
                doctor.repos, "load", return_value={"repos": {}}),
            mock.patch.object(doctor.runtime, "health", return_value=initial),
            mock.patch.object(
                doctor.lifecycle, "collect", return_value=collected),
            mock.patch.object(
                doctor.lifecycle, "converge", return_value=result),
            mock.patch.object(
                doctor.update_check, "interactive", return_value=False),
            contextlib.redirect_stdout(stdout),
        ):
            code = doctor.main(argv)
        return code, stdout.getvalue()

    def test_quick_uses_fresh_cached_health_and_always_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            beacon.write_text("cached\n", encoding="utf-8")
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": ""}),
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(doctor.internal, "main") as maintain,
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        self.assertEqual(0, code)
        maintain.assert_not_called()
        self.assertEqual({
            "ok": True,
            "checks": [{
                "check": "automatic maintenance",
                "ok": True,
                "detail": "fresh",
                "source": "cached",
            }],
        }, json.loads(stdout.getvalue()))

    def test_quick_refreshes_stale_health_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            beacon.write_text("stale\n", encoding="utf-8")
            stale = time.time() - doctor.HEALTH_STALE_SECONDS - 60
            os.utime(beacon, (stale, stale))

            def refresh(_argv):
                beacon.write_text("fresh\n", encoding="utf-8")
                return 0

            stdout = io.StringIO()
            with (
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    doctor.internal, "main", side_effect=refresh) as maintain,
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        self.assertEqual(0, code)
        maintain.assert_called_once_with(["maintain", "--quiet"])
        self.assertEqual("refreshed", json.loads(
            stdout.getvalue())["checks"][0]["source"])

    def test_quick_fails_when_maintenance_does_not_write_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    doctor.internal, "main", return_value=1) as maintain,
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        self.assertEqual(1, code)
        maintain.assert_called_once_with(["maintain", "--quiet"])
        self.assertEqual({
            "ok": False,
            "checks": [{
                "check": "automatic maintenance",
                "ok": False,
                "detail": "health record remained stale",
                "source": "refresh-failed",
            }],
        }, json.loads(stdout.getvalue()))

    def test_quick_reports_maintenance_errors_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    doctor.internal, "main",
                    side_effect=RuntimeError("maintenance unavailable")),
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            "health refresh failed",
            payload["checks"][0]["detail"],
        )

    def test_quick_cli_is_host_local_and_suppresses_maintenance_output(self) -> None:
        cli_main = importlib.import_module("agents_live.cli.main")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"

            def noisy_failure(_argv):
                print("private maintenance output")
                print("private repository path", file=sys.stderr)
                return 1

            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENTS_LIVE_REPO": "Z:/missing-private-repository"},
                ),
                mock.patch.object(upgrade_handoff, "reconcile"),
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    doctor.internal, "main", side_effect=noisy_failure),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = cli_main.main(["doctor", "--quick"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("", stderr.getvalue())
        self.assertNotIn("private", stdout.getvalue())

    def test_unknown_metadata_reports_both_possible_remedies(self) -> None:
        collected = mock.Mock(
            unavailable_repositories=(), broken_definitions=(),
            unknown_metadata=((Path("Agents/sample/SKILL.md"),
                               ("agents-live.schedul",)),),
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(
                doctor.repos, "load", return_value={"repos": {}}),
            mock.patch.object(
                doctor.runtime, "health", return_value=Health(True, "fresh")),
            mock.patch.object(
                doctor.lifecycle, "collect", return_value=collected),
            mock.patch.object(doctor.update_check, "interactive", return_value=False),
            contextlib.redirect_stdout(stdout),
        ):
            code = doctor.main([])
        self.assertEqual(1, code)
        self.assertIn("agents-live.schedul", stdout.getvalue())
        self.assertIn("typo", stdout.getvalue())
        self.assertIn("newer agents-live runtime", stdout.getvalue())

    def test_unmerged_git_index_fails_health_without_listing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["git", *args], cwd=root, check=check,
                    capture_output=True, text=True)

            git("init", "--quiet")
            git("config", "user.email", "tests@example.invalid")
            git("config", "user.name", "Agents Live Tests")
            conflicted = root / "private-name.txt"
            conflicted.write_text("base\n", encoding="utf-8")
            git("add", conflicted.name)
            git("commit", "--quiet", "-m", "base")
            primary = git("branch", "--show-current").stdout.strip()
            git("switch", "--quiet", "-c", "other")
            conflicted.write_text("ours\n", encoding="utf-8")
            git("commit", "--quiet", "-am", "other")
            git("switch", "--quiet", primary)
            conflicted.write_text("theirs\n", encoding="utf-8")
            git("commit", "--quiet", "-am", "primary")
            self.assertNotEqual(0, git("merge", "other", check=False).returncode)

            check = doctor._git_index_check(root, "sample")

        self.assertEqual({
            "check": "git index sample",
            "ok": False,
            "detail": (
                "1 unmerged path(s); resolve the Git index before running "
                "automated agents"
            ),
        }, check)
        self.assertNotIn("private-name", str(check))

    def test_non_git_repository_has_no_git_index_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(
                doctor._git_index_check(Path(temporary), "sample"))

    def test_repair_reports_post_convergence_health_in_text_and_json(self) -> None:
        stale = Health(False, "stale", detail=("liveness beacon is stale",))
        fresh = Health(True, "fresh")
        result = mock.Mock(done=(object(),), failed=(), health=fresh)

        code, output = self._run(["--repair"], stale, result)
        self.assertEqual(0, code)
        self.assertIn("ok: host runtime: fresh", output)
        self.assertNotIn("liveness beacon is stale", output)

        code, output = self._run(
            ["--repair"], stale, result, json_mode=True)
        payload = json.loads(output)
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("fresh", next(
            item["detail"] for item in payload["checks"]
            if item["check"] == "host runtime"))

    def test_failed_repair_and_dry_run_keep_unhealthy_status(self) -> None:
        stale = Health(False, "stale", detail=("liveness beacon is stale",))
        failed = mock.Mock(
            done=(), failed=((object(), "repair failed"),), health=stale)
        code, output = self._run(["--repair"], stale, failed)
        self.assertEqual(1, code)
        self.assertIn("ERROR: host runtime", output)
        self.assertIn("ERROR: repair", output)

        preview = mock.Mock(done=(object(),), failed=(), health=Health(True, "fresh"))
        code, output = self._run(["--dry-run"], stale, preview)
        self.assertEqual(1, code)
        self.assertIn("ERROR: host runtime", output)
        self.assertIn("ok: repair", output)


class TestReleaseTool(unittest.TestCase):
    def test_unmatched_pull_does_not_claim_changelog_entry_is_missing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        release = runpy.run_path(str(root / "tools" / "release.py"))
        build_notes = release["_release_notes"]
        entry = mock.Mock(
            summary="Fix packaged dashboard startup",
            issues=(), migration=None,
        )
        stderr = io.StringIO()
        with (
            mock.patch.dict(build_notes.__globals__, {
                "_version_notes": lambda _version: "notes",
                "_changelog_entries": lambda _notes, _version: [entry],
                "_previous_tag": lambda _tag: "v6.0.1",
                "_merged_pulls": lambda _base, _tag: {
                    272: ("fix: load qlog from packaged dashboard", ()),
                },
                "_entry_rank": lambda _entry: 1,
            }),
            contextlib.redirect_stderr(stderr),
        ):
            build_notes("6.0.2")
        self.assertIn("could not be associated", stderr.getvalue())
        self.assertNotIn("has no changelog entry", stderr.getvalue())


class TestRuntimeCore(unittest.TestCase):
    def test_framework_smoketest_has_no_external_provider_gate(self) -> None:
        self.assertEqual("fake", health_check._resolve_smoketest_runtime())

    def test_missing_ownership_backend_reports_the_required_entry_point(self) -> None:
        with (
            mock.patch.object(ownership, "mode", return_value="registry"),
            mock.patch.object(ownership, "_backend", return_value=None),
            self.assertRaisesRegex(
                ownership.OwnershipUnavailableError,
                "agents_live.ownership",
            ),
        ):
            ownership.load_owners()

    def test_broken_ownership_backend_reports_as_unavailable(self) -> None:
        entry = mock.Mock()
        entry.name = "registry"
        entry.value = "broken_plugin.registry"
        entry.load.side_effect = ModuleNotFoundError(
            "No module named 'agents_live.ownership'",
            name="agents_live.ownership",
        )
        with (
            mock.patch("importlib.metadata.entry_points", return_value=[entry]),
            mock.patch.object(ownership, "_backend_resolved", False),
            mock.patch.object(ownership, "_backend_cache", None),
            self.assertRaisesRegex(
                ownership.OwnershipUnavailableError,
                "broken_plugin.registry.*agents_live.ownership",
            ),
        ):
            ownership.registry_available()

    def test_cli_entry_exports_utf8_to_subprocess_subcommands(self) -> None:
        # The subprocess-dispatched subcommands (logs, timeline, dashboard)
        # print agent output verbatim, so a legacy console code page turns
        # one emoji in a log line into UnicodeEncodeError. Reconfiguring
        # only the dispatcher's own streams does not reach them; exporting
        # PYTHONUTF8 does.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTHONUTF8", None)
            with mock.patch.object(hostruntime, "_IS_WINDOWS", False):
                hostruntime.use_utf8_io()
            self.assertEqual("1", os.environ.get("PYTHONUTF8"))

    def test_cli_entry_point_uses_the_utf8_helper(self) -> None:
        module = importlib.import_module("agents_live.cli.main")
        with mock.patch.object(hostruntime, "use_utf8_io") as helper:
            with contextlib.suppress(SystemExit):
                module.main(["--version"])
        helper.assert_called_once_with()

    def test_deferred_upgrade_is_single_flight_and_records_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                claim, existing = upgrade_handoff.claim(
                    environment, source="agents-live", runtime_only=False)
                self.assertIsNotNone(claim)
                self.assertIsNone(existing)
                assert claim is not None
                duplicate, existing = upgrade_handoff.claim(
                    environment, source="agents-live", runtime_only=False)
                self.assertIsNone(duplicate)
                self.assertEqual(claim.operation_id, existing)
                claim.result_path.write_text(json.dumps({
                    "schema": 1,
                    "operation_id": claim.operation_id,
                    "status": "terminal",
                    "helper_pid": 123,
                    "exit_code": 0,
                }), encoding="utf-8")
                with mock.patch.object(upgrade_handoff.adminlog, "record") as record:
                    upgrade_handoff.reconcile()
                self.assertFalse(claim.pending_path.exists())
                record.assert_called_once()
                self.assertEqual("ok", record.call_args.kwargs["status"])
                self.assertEqual(
                    claim.operation_id,
                    record.call_args.kwargs["correlation_id"])

    def test_deferred_upgrade_recovers_a_dead_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                claim, _ = upgrade_handoff.claim(
                    environment, source="agents-live", runtime_only=False)
                assert claim is not None
                helper = ProcessRef(
                    321, 1000.0, "powershell.exe", "upgrade",
                    claim.operation_id)
                upgrade_handoff.spawned(claim, helper)
                supervisor = mock.Mock()
                supervisor.alive.return_value = False
                with (
                    mock.patch.object(
                        upgrade_handoff.runtime, "current",
                        return_value=mock.Mock(supervisor=supervisor)),
                    mock.patch.object(upgrade_handoff.adminlog, "record") as record,
                ):
                    upgrade_handoff.reconcile()
                self.assertFalse(claim.pending_path.exists())
                self.assertEqual("error", record.call_args.kwargs["status"])

    def test_deferred_upgrade_adopts_a_started_helper_after_parent_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                claim, _ = upgrade_handoff.claim(
                    environment, source="agents-live", runtime_only=False)
                assert claim is not None
                claim.result_path.write_text(json.dumps({
                    "schema": 1,
                    "operation_id": claim.operation_id,
                    "status": "started",
                    "helper_pid": 321,
                    "helper_started_at": 1000.0,
                }), encoding="utf-8")
                helper = ProcessRef(
                    321, 1000.0, "powershell.exe", "upgrade",
                    claim.operation_id)
                supervisor = mock.Mock()
                supervisor.alive.return_value = True
                with (
                    mock.patch.object(
                        upgrade_handoff.time, "time", return_value=10_000),
                    mock.patch.object(
                        upgrade_handoff.runtime, "current",
                        return_value=mock.Mock(supervisor=supervisor)),
                    mock.patch.object(upgrade_handoff.adminlog, "record") as record,
                ):
                    upgrade_handoff.reconcile()
                    self.assertTrue(claim.pending_path.exists())
                    pending = json.loads(
                        claim.pending_path.read_text(encoding="utf-8"))
                    self.assertEqual({
                        "pid": 321,
                        "created_at": 1000.0,
                        "image": "powershell.exe",
                        "role": "upgrade",
                        "key": claim.operation_id,
                        "fingerprint": "",
                    }, pending["helper"])
                    supervisor.alive.assert_called_once_with(helper)
                    record.assert_not_called()
                    duplicate, existing = upgrade_handoff.claim(
                        environment, source="agents-live", runtime_only=False)
                self.assertIsNone(duplicate)
                self.assertEqual(claim.operation_id, existing)

    def test_deferred_upgrade_recovery_rejects_a_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                claim, _ = upgrade_handoff.claim(
                    environment, source="agents-live", runtime_only=False)
                assert claim is not None
                claim.result_path.write_text(json.dumps({
                    "schema": 1,
                    "operation_id": claim.operation_id,
                    "status": "started",
                    "helper_pid": 321,
                    "helper_started_at": 1000.0,
                }), encoding="utf-8")
                with (
                    mock.patch.object(
                        upgrade_handoff.runtime, "current",
                        return_value=mock.Mock(supervisor=WindowsProcesses())),
                    mock.patch.object(
                        hostruntime, "is_alive", return_value=True),
                    mock.patch.object(
                        hostruntime, "process_start_time", return_value=5000.0),
                    mock.patch.object(upgrade_handoff.adminlog, "record") as record,
                ):
                    upgrade_handoff.reconcile()
                self.assertFalse(claim.pending_path.exists())
                self.assertEqual("error", record.call_args.kwargs["status"])

    def test_windows_deferred_process_writes_a_bounded_result(self) -> None:
        process = mock.Mock(pid=42)
        reference = ProcessRef(
            42, 1000.0, "powershell.exe", "upgrade", "operation-1")
        supervisor = mock.Mock()
        supervisor.adopt.return_value = reference
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            transcript_path = Path(temporary) / "transcript.log"
            with (
                mock.patch.object(hostruntime, "_IS_WINDOWS", True),
                mock.patch.object(
                    hostruntime.shutil, "which", return_value="powershell.exe"),
                mock.patch.object(
                    hostruntime, "spawn_detached", return_value=process) as spawn,
            ):
                result = hostruntime.defer_until_environment_exits(
                    ["uv", "tool", "upgrade", "agents-live"], Path("C:/tool"),
                    supervisor=supervisor,
                    operation_id="operation-1", result_path=result_path,
                    transcript_path=transcript_path, transcript_limit=4096)
        self.assertIs(reference, result)
        supervisor.adopt.assert_called_once_with(
            42, role="upgrade", key="operation-1", image="powershell.exe")
        command = " ".join(spawn.call_args.args[0])
        self.assertIn("status='started'", command)
        self.assertIn("helper_started_at=$helperStartedAt", command)
        self.assertIn("status='terminal'", command)
        self.assertIn("4096", command)
        self.assertIn("exit $code", command)

    def test_windows_deferred_process_bounds_the_wait_for_the_environment(
        self,
    ) -> None:
        """An unbounded wait is a permanent block, not a delay.

        The helper staying alive is what tells the handoff its slot is
        still in use, so one process that never exits refused every later
        upgrade as already queued.
        """
        process = mock.Mock(pid=42)
        supervisor = mock.Mock()
        supervisor.adopt.return_value = ProcessRef(
            42, 1000.0, "powershell.exe", "upgrade", "operation-1")
        with (
            mock.patch.object(hostruntime, "_IS_WINDOWS", True),
            mock.patch.object(
                hostruntime.shutil, "which", return_value="powershell.exe"),
            mock.patch.object(
                hostruntime, "spawn_detached", return_value=process) as spawn,
        ):
            hostruntime.defer_until_environment_exits(
                ["uv", "tool", "upgrade", "agents-live"], Path("C:/tool"),
                supervisor=supervisor, wait_timeout_s=7)
        command = " ".join(spawn.call_args.args[0])
        self.assertIn("AddSeconds(7)", command)
        self.assertIn("$timedOut = $true", command)
        # Expiring must not run the upgrade: the environment is still busy.
        self.assertIn("if ($timedOut) { exit 1 }", command)

    @unittest.skipUnless(os.name == "nt", "native Windows only")
    def test_windows_deferred_process_reports_a_busy_environment(self) -> None:
        """A helper that gives up still reports, so the slot is released."""
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            transcript_path = Path(temporary) / "transcript.log"
            command_path = Path(temporary) / "never-runs.cmd"
            command_path.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
            # Every process runs from somewhere below the drive root, so
            # the environment never frees and the bound is what ends it.
            helper = WindowsProcesses().defer_until_environment_exits(
                [str(command_path)], Path(sys.executable).anchor,
                operation_id="operation-1", result_path=result_path,
                transcript_path=transcript_path, wait_timeout_s=1)
            self.assertIsNotNone(helper)
            result = self._await_terminal(result_path, transcript_path)
            self.assertEqual("terminal", result["status"])
            self.assertEqual(1, result["exit_code"])
            self.assertIn(
                "still in use",
                transcript_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "native Windows only")
    def test_windows_deferred_process_persists_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            transcript_path = Path(temporary) / "transcript.log"
            command_path = Path(temporary) / "command with spaces.cmd"
            command_path.write_text(
                "@echo off\n"
                "echo [%~1][%~2]\n"
                "exit /b 7\n",
                encoding="utf-8")
            helper = WindowsProcesses().defer_until_environment_exits(
                [str(command_path), "value with spaces", "apostrophe's value"],
                Path(temporary) / "unused-environment",
                operation_id="operation-1", result_path=result_path,
                transcript_path=transcript_path)
            self.assertIsNotNone(helper)
            # The helper is detached by design, so its exit does not order
            # the write that follows it. Reading once raced the file and
            # tore down the fixture underneath a live process, which then
            # failed again on a directory that no longer existed.
            result = self._await_terminal(result_path, transcript_path)
            self.assertEqual("operation-1", result["operation_id"])
            self.assertEqual("terminal", result["status"])
            self.assertEqual(7, result["exit_code"])
            self.assertIn(
                "[value with spaces][apostrophe's value]",
                transcript_path.read_text(encoding="utf-8"))

    def _await_terminal(self, result_path: Path, transcript_path: Path,
                        *, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = json.loads(result_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                last = None
            if isinstance(last, dict) and last.get("status") == "terminal":
                return last
            time.sleep(0.2)
        transcript = ""
        with contextlib.suppress(OSError):
            transcript = transcript_path.read_text(encoding="utf-8")[-2000:]
        self.fail(
            f"no terminal result within {timeout:.0f}s; last={last!r}; "
            f"transcript tail:\n{transcript}")

    def test_windows_deferred_process_without_powershell_returns_none(self) -> None:
        with (
            mock.patch.object(hostruntime, "_IS_WINDOWS", True),
            mock.patch.object(hostruntime.shutil, "which", return_value=None),
        ):
            self.assertIsNone(hostruntime.defer_until_environment_exits(
                ["uv", "tool", "upgrade", "agents-live"], Path("C:/tool")))

    def test_windows_upgrade_queues_one_correlated_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            environment.mkdir(parents=True)
            helper = ProcessRef(
                42, 1000.0, "powershell.exe", "upgrade", "operation-1")
            supervisor = mock.Mock()
            supervisor.defer_until_environment_exits.return_value = helper
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}),
                mock.patch.object(
                    upgrade.hostruntime, "id", return_value=upgrade.hostruntime.WINDOWS),
                mock.patch.object(
                    upgrade.plugins, "tool_environment", return_value=environment),
                mock.patch.object(upgrade.triggers, "within", return_value=True),
                mock.patch.object(upgrade, "find_uv", return_value="uv.exe"),
                mock.patch.object(upgrade, "_refuse_while_held", return_value=False),
                mock.patch.object(
                    upgrade.runtime, "current",
                    return_value=mock.Mock(supervisor=supervisor)),
                mock.patch.object(
                    upgrade.adminlog, "operation",
                    return_value=contextlib.nullcontext({})) as operation,
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    0, upgrade._handoff_windows_upgrade(None, runtime_only=False))
            defer = supervisor.defer_until_environment_exits
            command = defer.call_args.args[0]
            self.assertEqual(
                ["uv.exe", "tool", "run", "--refresh", "--from",
                 f"agents-live>={upgrade.__version__}"],
                command[:6])
            self.assertIn("--continuation-environment", command)
            self.assertIn("--upgrade-id", command)
            operation_id = command[command.index("--upgrade-id") + 1]
            self.assertEqual(operation_id, defer.call_args.kwargs["operation_id"])
            self.assertEqual(
                operation_id,
                operation.call_args.kwargs["correlation_id"])
            self.assertIn(operation_id, stdout.getvalue())
            pending = list(
                (state_home / "agents-live" / "upgrade-handoffs").glob(
                    "*.pending.json"))
            self.assertEqual(1, len(pending))
            self.assertEqual(42, json.loads(
                pending[0].read_text(encoding="utf-8"))["helper"]["pid"])

    def test_windows_upgrade_abandons_claim_when_helper_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            environment.mkdir(parents=True)
            stderr = io.StringIO()
            supervisor = mock.Mock()
            supervisor.defer_until_environment_exits.return_value = None
            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}),
                mock.patch.object(
                    upgrade.hostruntime, "id", return_value=upgrade.hostruntime.WINDOWS),
                mock.patch.object(
                    upgrade.plugins, "tool_environment", return_value=environment),
                mock.patch.object(upgrade.triggers, "within", return_value=True),
                mock.patch.object(upgrade, "find_uv", return_value="uv.exe"),
                mock.patch.object(upgrade, "_refuse_while_held", return_value=False),
                mock.patch.object(
                    upgrade.runtime, "current",
                    return_value=mock.Mock(supervisor=supervisor)),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(
                    1, upgrade._handoff_windows_upgrade(None, runtime_only=False))
            pending = list(
                (state_home / "agents-live" / "upgrade-handoffs").glob(
                    "*.pending.json"))
            self.assertEqual([], pending)
            self.assertIn("nothing was changed", stderr.getvalue())

    def test_windows_uninstall_uses_the_supervisor_handoff(self) -> None:
        helper = ProcessRef(
            42, 1000.0, "powershell.exe", "upgrade", "operation-1")
        supervisor = mock.Mock()
        supervisor.defer_until_environment_exits.return_value = helper
        stdout = io.StringIO()
        environment = Path("C:/tools/agents-live")
        with (
            mock.patch.object(
                uninstall.runtime, "current",
                return_value=mock.Mock(supervisor=supervisor)),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertTrue(
                uninstall._handoff_windows_uninstall("uv.exe", environment))
        supervisor.defer_until_environment_exits.assert_called_once_with(
            ["uv.exe", "tool", "uninstall", "agents-live"], environment)
        self.assertIn("after this command exits", stdout.getvalue())

    def test_name_keyed_ownership_rejects_duplicate_identities(self) -> None:
        with self.assertRaisesRegex(
            ownership.OwnershipUnavailableError,
            "cannot distinguish duplicate agent name 'verify-links'",
        ):
            ownership.resolve_owners(
                (("verify-links-111", "verify-links"),
                 ("verify-links-222", "verify-links")),
                {"verify-links": "host/runtime/" + "a" * 32},
            )

    def test_schedule_language_is_portable_and_watch_is_canonical(self) -> None:
        self.assertEqual("0 8 * 1 1", parse_schedule("0 8 * JAN MON").canonical)
        self.assertEqual("@yearly", parse_schedule("@annually").canonical)
        self.assertEqual(
            {5, 15, 25, 35, 45, 55},
            triggers.schedule_fields("5/10 * * * *")[0],
        )
        self.assertEqual("0 0 * * 5-7", parse_schedule("0 0 * * FRI-SUN").canonical)
        self.assertEqual("0 0 * * 0-0", parse_schedule("0 0 * * SUN-SUN").canonical)
        watch = parse_watch("src/** !src/tmp/** docs/** debounce 1000ms")
        self.assertEqual(
            "'docs/**' 'src/**' '!src/tmp/**' debounce 1s", watch.canonical)
        self.assertTrue(watch.matches("src/main.py"))
        self.assertFalse(watch.matches("src/tmp/cache.py"))
        for unsafe in ("../outside/**", "/absolute/**", "C:/absolute/**"):
            with self.subTest(unsafe=unsafe), self.assertRaises(WatchSyntaxError):
                parse_watch(unsafe)

    def test_diff_repairs_drift_and_restarts_only_changed_watchers(self) -> None:
        desired = (
            _rendered("schedule", "a", "one"),
            _rendered("watch", "b", "new"),
        )
        actual = (
            InstalledTrigger("b", "repo:/r", "watch", "old", "old"),
            InstalledTrigger("orphan", "repo:/r", "schedule", "x", "old"),
        )
        watcher = ProcessRef(12, 1, "agents-live", "watcher", "b", "old")
        self.assertEqual(
            [
                "remove-trigger", "install-trigger", "remove-trigger",
                "install-trigger", "stop-watcher", "start-watcher",
            ],
            [item.kind for item in diff(desired, actual, (watcher,))],
        )

    def test_reboot_watcher_carries_process_identity(self) -> None:
        subscription = Subscription.create(
            scope="repo:/tmp/example",
            target="agent:sample",
            kind="watch",
            trigger="'src/**' debounce 1s",
        )
        rendered = PosixHost().render(subscription)
        self.assertIn("--runtime-role watcher", rendered.rendered)
        self.assertIn(
            f"--subscription-key {subscription.key}", rendered.rendered)
        self.assertNotIn("--watch-expression", rendered.rendered)
        self.assertNotIn("--artifact-marker", rendered.rendered)
        self.assertIn("--watch-expression", rendered.watcher_argv)
        self.assertNotIn("--runtime-role", rendered.watcher_argv)

    def test_long_watcher_renders_a_bounded_crontab_line(self) -> None:
        expression = " ".join(
            f"workspace/component-{index}/generated/**"
            for index in range(80)
        ) + " debounce 1s"
        subscription = Subscription.create(
            scope="repo:/tmp/example",
            target="agent:sample",
            kind="watch",
            trigger=expression,
        )
        host = PosixHost()
        rendered = host.render(subscription)
        self.assertLess(len(rendered.rendered), 1000)
        self.assertGreater(len(" ".join(rendered.watcher_argv)), 1000)
        self.assertIsNotNone(runtime.artifacts.from_rendered(rendered.rendered))

    def test_internal_maintain_refreshes_the_host_health_beacon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            beacon = root / "health.ok"
            subscriptions = (
                Subscription.create(
                    scope=f"repo:{root}", target="agent:watcher",
                    kind="watch", trigger="src/** debounce 1s"),
                Subscription.create(
                    scope=f"repo:{root}", target="agent:scheduled",
                    kind="schedule", trigger="0 8 * * *"),
            )
            result = mock.Mock(
                failed=(), health=Health(True, "not-required"))
            collected = mock.Mock(subscriptions=subscriptions)
            converge_maintenance = mock.Mock(return_value=result)
            with (
                mock.patch.object(
                    internal.lifecycle, "converge", converge_maintenance),
                mock.patch.object(
                    internal.lifecycle, "collect", return_value=collected),
                mock.patch.object(
                    internal.paths, "health_beacon_path", return_value=beacon),
            ):
                self.assertEqual(0, internal.main(["maintain", "--quiet"]))
                original = beacon.read_text(encoding="utf-8")
                self.assertEqual(0, internal.main(["maintain", "--dry-run"]))
                self.assertEqual(original, beacon.read_text(encoding="utf-8"))
                converge_maintenance.return_value = mock.Mock(
                    failed=(), health=Health(False, "stale"))
                self.assertEqual(1, internal.main(["maintain", "--quiet"]))
                self.assertEqual(original, beacon.read_text(encoding="utf-8"))

            payload = json.loads(beacon.read_text(encoding="utf-8"))
            self.assertEqual("healthy", payload["status"])
            self.assertEqual(1, payload["watchers"])
            self.assertEqual(1, payload["cron"])
            self.assertEqual({str(root): {"status": "ok"}}, payload["repos"])

    def test_uninstall_clears_structured_triggers_and_watchers(self) -> None:
        host = MemoryHost()
        subscriptions = (
            Subscription.create(
                scope="repo:/tmp/example", target="agent:sample",
                kind="schedule", trigger="0 8 * * *"),
            Subscription.create(
                scope="repo:/tmp/example", target="agent:sample",
                kind="watch", trigger="'src/**' debounce 1s"),
        )
        self.assertFalse(converge(subscriptions, _host=host).failed)
        with mock.patch.object(uninstall.runtime, "current", return_value=host):
            uninstall._sweep_runtime()
        self.assertEqual([], host.trigger_store.list())
        self.assertEqual([], host.supervisor.owned())


def _rendered(kind: str, key: str, fingerprint: str):
    from agents_live.runtime import RenderedSubscription
    return RenderedSubscription(
        key, "repo:/r", kind, fingerprint, "rendered", ("watch",) if kind == "watch" else ())


class TestStartedState(TempRepository):
    def _converge_with_legacy(self, host: MemoryHost):
        previous = runtime.current()
        runtime.configure(host)
        try:
            with mock.patch.object(
                lifecycle.repos,
                "load",
                return_value={
                    "repos": {"sample": str(self.root)},
                    "default_repo": "sample",
                },
            ):
                return lifecycle.converge()
        finally:
            runtime.configure(previous)

    def test_lifecycle_adopts_then_replaces_legacy_trigger(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        spec = agent.load("sample", root=self.root)
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}
        result = self._converge_with_legacy(host)
        self.assertFalse(result.failed)
        self.assertIn(spec.identifier, state.load(self.root).agents)
        self.assertEqual(set(), host.legacy[str(self.root)])
        self.assertTrue(any(item.kind == "remove-legacy" for item in result.done))

    def test_collection_applies_ownership_mode_per_repository(self) -> None:
        self.skill("local-agent", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        with tempfile.TemporaryDirectory() as temporary:
            registry_root = Path(temporary).resolve()
            skill = registry_root / "Agents" / "remote-agent"
            skill.mkdir(parents=True)
            (registry_root / ".agents-live.toml").write_text(
                'ownership = "registry"\n', encoding="utf-8")
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: remote-agent\n"
                "description: Registry-managed definition.\n"
                "metadata:\n"
                '  agents-live.schema-version: "1"\n'
                '  agents-live.selector: "fake"\n'
                '  agents-live.schedule: "0 9 * * *"\n'
                "---\nbody\n",
                encoding="utf-8",
            )
            local = agent.load("local-agent", root=self.root)
            remote = agent.load("remote-agent", root=registry_root)
            state.replace(self.root, {local.identifier})
            state.replace(registry_root, {remote.identifier})
            host = MemoryHost()
            previous = runtime.current()
            runtime.configure(host)
            try:
                with (
                    mock.patch.object(lifecycle.repos, "load", return_value={
                        "repos": {
                            "local": str(self.root),
                            "registry": str(registry_root),
                        },
                        "default_repo": "local",
                    }),
                    mock.patch.object(
                        ownership, "load_owners",
                        return_value={"remote-agent": "other/runtime/uuid"}),
                    mock.patch.object(ownership, "owns", return_value=False),
                ):
                    collected = lifecycle.collect(persist=False)
            finally:
                runtime.configure(previous)
            targets = {item.target for item in collected.subscriptions}
            self.assertIn(f"agent:{local.identifier}", targets)
            self.assertNotIn(f"agent:{remote.identifier}", targets)

    def test_missing_ownership_backend_blocks_only_registry_roots(self) -> None:
        self.skill("local-agent", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        with tempfile.TemporaryDirectory() as temporary:
            registry_root = Path(temporary).resolve()
            skill = registry_root / "Agents" / "registry-agent"
            skill.mkdir(parents=True)
            (registry_root / ".agents-live.toml").write_text(
                'ownership = "registry"\n', encoding="utf-8")
            (skill / "SKILL.md").write_text(
                "---\nname: registry-agent\n"
                "description: Registry-managed definition.\nmetadata:\n"
                '  agents-live.schema-version: "1"\n'
                '  agents-live.selector: "fake"\n'
                '  agents-live.schedule: "0 9 * * *"\n'
                "---\nbody\n",
                encoding="utf-8",
            )
            local = agent.load("local-agent", root=self.root)
            remote = agent.load("registry-agent", root=registry_root)
            state.replace(self.root, {local.identifier})
            state.replace(registry_root, {remote.identifier})
            host = MemoryHost()
            previous = runtime.current()
            runtime.configure(host)
            try:
                with (
                    mock.patch.object(lifecycle.repos, "load", return_value={
                        "repos": {
                            "local": str(self.root),
                            "registry": str(registry_root),
                        },
                        "default_repo": "local",
                    }),
                    mock.patch.object(
                        ownership, "load_owners",
                        side_effect=ownership.OwnershipUnavailableError(
                            "registry backend unavailable")),
                ):
                    collected = lifecycle.collect(persist=False)
            finally:
                runtime.configure(previous)
            targets = {item.target for item in collected.subscriptions}
            self.assertIn(f"agent:{local.identifier}", targets)
            self.assertNotIn(f"agent:{remote.identifier}", targets)
            self.assertIn(f"repo:{registry_root}", collected.protected_scopes)
            self.assertTrue(any(
                str(registry_root) in detail
                for detail in collected.unavailable_repositories))

    def test_failed_replacement_preserves_legacy_trigger(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}
        with mock.patch.object(
            host.trigger_store, "install", side_effect=RuntimeError("blocked"),
        ):
            result = self._converge_with_legacy(host)
        self.assertTrue(result.failed)
        self.assertEqual({"sample"}, host.legacy[str(self.root)])

    def test_upgrading_before_migrating_still_adopts(self) -> None:
        """The documented upgrade order puts a converge before the migration.

        Adoption is the only thing that carries a 5.x trigger onto its
        canonical identifier, and it has one chance: once started state
        exists it is never adopted again.
        """
        legacy_file = self.root / "Agents" / "sample.md"
        legacy_file.write_text(
            "---\nname: sample\ndescription: Not yet migrated.\n"
            'runtime: fake\nschedule: "0 8 * * *"\n---\nbody\n',
            encoding="utf-8")
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}

        self.assertFalse(self._converge_with_legacy(host).failed)
        self.assertEqual(
            {"sample"}, host.legacy[str(self.root)],
            "a definition that will not parse keeps its legacy trigger")

        convert(legacy_file, root=self.root)
        identifier = agent.load("sample", root=self.root).identifier
        self.assertFalse(self._converge_with_legacy(host).failed)
        self.assertIn(identifier, state.load(self.root).agents)

    def test_partial_adoption_is_visible_to_status(self) -> None:
        """A repository can hold one definition that is not converted yet.

        Convergence adopts the legacy triggers it can map and installs their
        subscriptions, so started state has to record what is running.
        """
        self.skill("good", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        (self.root / "Agents" / "stale.md").write_text(
            "---\nname: stale\ndescription: Still 5.x.\n"
            'runtime: fake\nschedule: "0 9 * * *"\n---\nbody\n',
            encoding="utf-8")
        host = MemoryHost()
        host.legacy[str(self.root)] = {"good", "stale"}

        self.assertFalse(self._converge_with_legacy(host).failed)
        identifier = agent.load("good", root=self.root).identifier
        self.assertIn(identifier, state.load(self.root).agents)

    def test_absent_adopts_and_unreadable_abstains(self) -> None:
        snapshot = state.load_or_adopt(self.root, {"one", "two"})
        self.assertEqual(frozenset({"one", "two"}), snapshot.agents)
        path = next((self.root / "state" / "agents-live" / "repos").glob("*/started.json"))
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(state.StartedStateUnavailable):
            state.load_or_adopt(self.root, set())

    def test_preview_adoption_does_not_initialize_state(self) -> None:
        snapshot = state.load_or_adopt(
            self.root, {"existing"}, persist=False)
        self.assertEqual(frozenset({"existing"}), snapshot.agents)
        self.assertFalse(state.load(self.root).initialized)

    def test_one_invalid_definition_does_not_disturb_its_neighbours(self) -> None:
        self.skill("good", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        host = MemoryHost()
        identifier = agent.load("good", root=self.root).identifier
        registry = {"repos": {"sample": str(self.root)}, "default_repo": "sample"}
        previous = runtime.current()
        runtime.configure(host)
        try:
            with mock.patch.object(
                    lifecycle.repos, "load", return_value=registry):
                self.assertFalse(lifecycle.converge(
                    additions={self.root: {identifier}}).failed)
                installed = {item.key for item in host.trigger_store.list()}
                self.assertEqual(2, len(installed))

                (self.root / "Agents" / "broken.md").write_text(
                    "---\ndescription: Invalid.\nruntime: claude\n---\nbody\n",
                    encoding="utf-8")
                collected = lifecycle.collect(persist=False)
                # The repository still resolves, so nothing needs protecting:
                # the healthy agent survives because discovery isolated the
                # failure instead of reporting an empty repository.
                self.assertEqual((), collected.protected_scopes)
                self.assertEqual(
                    ["broken.md"],
                    [Path(path).name for path, _ in collected.broken_definitions])
                self.assertFalse(lifecycle.converge().failed)
                self.assertEqual(
                    installed, {item.key for item in host.trigger_store.list()})
        finally:
            runtime.configure(previous)

    def test_a_started_definition_that_stops_parsing_keeps_its_trigger(self) -> None:
        """Editing a started definition into an invalid state is not a stop.

        Its desired state becomes unknown rather than empty, so the artifact
        is held until the file parses again or the user stops it.
        """
        directory = self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        prompt = directory / "SKILL.md"
        good = prompt.read_text(encoding="utf-8")
        identifier = agent.load("sample", root=self.root).identifier
        registry = {"repos": {"sample": str(self.root)}, "default_repo": "sample"}
        host = MemoryHost()
        previous = runtime.current()
        runtime.configure(host)
        try:
            with mock.patch.object(
                    lifecycle.repos, "load", return_value=registry):
                self.assertFalse(lifecycle.converge(
                    additions={self.root: {identifier}}).failed)
                started = {item.key for item in host.trigger_store.list()}

                prompt.write_text(
                    good.replace(
                        '  agents-live.selector: "fake"\n',
                        '  agents-live.mystery: "x"\n'),
                    encoding="utf-8")
                collected = lifecycle.collect(persist=False)
                self.assertIn(
                    f"agent:{identifier}", collected.protected_targets)
                self.assertFalse(lifecycle.converge().failed)
                self.assertEqual(
                    started, {item.key for item in host.trigger_store.list()})

                # Protection must not outrank an explicit stop.
                self.assertFalse(lifecycle.converge(
                    removals={self.root: {identifier}}).failed)
                remaining = {item.key for item in host.trigger_store.list()}
                self.assertEqual(1, len(remaining), "only maintenance remains")
        finally:
            runtime.configure(previous)

    def test_unreachable_repository_keeps_its_installed_triggers(self) -> None:
        self.skill("good", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        other = self.root / "other"
        (other / "Agents" / "second").mkdir(parents=True)
        (other / "Agents" / "second" / "SKILL.md").write_text(
            "---\nname: second\ndescription: A second repository definition.\n"
            "metadata:\n"
            '  agents-live.schema-version: "1"\n'
            '  agents-live.selector: "fake"\n'
            '  agents-live.schedule: "0 9 * * *"\n'
            "---\nDo the work.\n",
            encoding="utf-8",
        )
        host = MemoryHost()
        here = agent.load("good", root=self.root).identifier
        there = agent.load("second", root=other).identifier
        registry = {
            "repos": {"sample": str(self.root), "other": str(other)},
            "default_repo": "sample",
        }
        previous = runtime.current()
        runtime.configure(host)
        try:
            with mock.patch.object(
                    lifecycle.repos, "load", return_value=registry):
                self.assertFalse(lifecycle.converge(additions={
                    self.root: {here}, other: {there}}).failed)
                installed = {item.key for item in host.trigger_store.list()}
                self.assertEqual(3, len(installed))

                os.rename(other, self.root / "moved-away")
                collected = lifecycle.collect(persist=False)
                self.assertIn(f"repo:{other}", collected.protected_scopes)
                self.assertFalse(lifecycle.converge().failed)
                self.assertEqual(
                    installed, {item.key for item in host.trigger_store.list()})
        finally:
            runtime.configure(previous)

    def test_naming_an_invalid_definition_reports_why(self) -> None:
        (self.root / "Agents" / "broken.md").write_text(
            "---\ndescription: Invalid.\nruntime: claude\n---\nbody\n",
            encoding="utf-8")
        with self.assertRaisesRegex(agent.DefinitionError, "retired"):
            agent.load("broken", root=self.root)

    def test_failed_init_registers_nothing(self) -> None:
        """A host-mutating command that cannot finish must not half-finish.

        Plugin convergence is the step most likely to fail, so it runs
        before the registry is touched rather than after it.
        """
        project = self.root / "candidate"
        (project / "Agents").mkdir(parents=True)
        before = repos.load()
        self.assertEqual({}, before["repos"])
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["agents-live init"]),
            mock.patch.dict(
                os.environ, {"AGENTS_LIVE_INIT_REPO": str(project)}),
            mock.patch.object(
                init.plugins, "converge",
                side_effect=plugins.PluginError("declared wheel is unreachable")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = init.main()
        self.assertEqual(1, code)
        self.assertIn("plugin convergence failed", stderr.getvalue())
        after = repos.load()
        self.assertEqual({}, after["repos"], "a failed init registered a repository")
        self.assertIsNone(after["default_repo"])
        self.assertFalse((project / paths.CONFIG_DOTFILE).exists())


class TestRuntimeProcessPolicy(unittest.TestCase):
    def test_dispatch_budget_counts_atomically_and_recovers_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "budget.json"
            self.assertTrue(claim_budget(path, limit=1, now=10).allowed)
            self.assertFalse(claim_budget(path, limit=1, now=11).allowed)
            lock = path.with_suffix(".json.lock")
            lock.write_text("stale", encoding="ascii")
            os.utime(lock, (0, 0))
            self.assertTrue(claim_budget(path, limit=1, now=100).allowed)

    @unittest.skipIf(os.name == "nt", "the POSIX adapter owns PTY allocation")
    def test_child_runner_honors_pty(self) -> None:
        result = LocalChildRunner().run_child(
            [sys.executable, "-c",
             "import sys; print('pty' if sys.stdout.isatty() else 'pipe')"],
            use_pty=True,
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("pty", result.stdout)

    def test_dead_run_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = _RunLock(root, "sample")
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text(
                json.dumps({"created": 0, "pid": 2**30}),
                encoding="ascii",
            )
            self.assertTrue(lock.acquire())
            lock.release()

    def test_unreadable_run_lock_expires_instead_of_blocking_forever(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = _RunLock(root, "sample")
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text("{ this is not json", encoding="ascii")

            fresh = _RunLock(root, "sample")
            self.assertFalse(fresh.acquire(), "a fresh unreadable lock is held")

            os.utime(lock.path, (0, 0))
            aged = _RunLock(root, "sample")
            self.assertTrue(aged.acquire(), "an aged unreadable lock is taken")
            aged.release()


class RecordingRunner:
    def __init__(self, outputs: list[ChildResult]) -> None:
        self.outputs = outputs
        self.argv: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.environments: list[dict[str, str]] = []
        self.mcp_configs: list[tuple[Path, dict[str, object]]] = []

    def run_child(self, argv, **kwargs):
        self.argv.append(tuple(argv))
        self.inputs.append(kwargs.get("input_text"))
        self.environments.append(dict(kwargs.get("env", {})))
        arguments = tuple(argv)
        for flag in ("--mcp-config", "--additional-mcp-config"):
            if flag in arguments:
                value = arguments[arguments.index(flag) + 1].removeprefix("@")
                path = Path(value)
                if path.name.startswith("agents-live-mcp-"):
                    self.mcp_configs.append((
                        path,
                        json.loads(path.read_text(encoding="utf-8")),
                    ))
        return self.outputs.pop(0)


class TestObservability(unittest.TestCase):
    def test_malformed_timestamp_does_not_hide_valid_timeline_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "mixed.log"
            log.write_text(
                '{"log_schema":5,"agent_name":"broken","phase":"done"}\n'
                '{"log_schema":5,"ts":"","agent_name":"empty"}\n'
                '{"log_schema":5,"ts":42,"agent_name":"number"}\n'
                '{"log_schema":5,"ts":"not-a-time","agent_name":"invalid"}\n'
                '{"log_schema":5,"ts":"2026-08-11T22:00:00","agent_name":"naive"}\n'
                '{"log_schema":5,"ts":"2026-08-11T22:00:00Z",'
                '"agent_name":"valid","phase":"done"}\n',
                encoding="utf-8",
            )

            all_records = obs.load([log])
            filtered_records = obs.load(
                [log], since="2026-08-11T21:00:00Z")

        for records in (all_records, filtered_records):
            self.assertEqual(1, len(records))
            self.assertEqual("valid", records[0]["agent_name"])


class TestAgentPipeline(TempRepository):
    def test_first_manual_run_can_append_to_handler_log(self) -> None:
        directory = self.skill("handler-writer", [
            'agents-live.selector: "none"',
            'agents-live.post-processor: "scripts/process.py"',
        ])
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "import json, os\n"
            "from datetime import datetime, timezone\n"
            "record = {'log_schema': 5, "
            "'ts': datetime.now(timezone.utc).isoformat(), "
            "'agent_name': os.environ['AGENTS_LIVE_AGENT_ID'], "
            "'phase': 'handler', 'status': 'ok'}\n"
            "with open(os.environ['AGENTS_LIVE_LOG_FILE'], 'a', "
            "encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(record) + '\\n')\n"
            "print('done')\n",
            encoding="utf-8",
        )
        spec = agent.load("handler-writer", root=self.root)
        log = (
            paths.repo_state_dir(self.root)
            / "logs"
            / f"{spec.identifier}.jsonl"
        )
        self.assertFalse(log.parent.exists())

        result = dispatch(
            Firing("handler-writer", str(self.root), "manual"),
        )

        self.assertTrue(result.ok, result)
        records = obs.load([log])
        self.assertEqual(
            ["handler", "done"],
            [record["phase"] for record in records],
        )

    def test_processors_receive_stable_identity_and_log_destination(self) -> None:
        directory = self.skill("handler-contract", [
            'agents-live.selector: "none"',
            'agents-live.post-processor: "scripts/process.py"',
        ])
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "print('done')\n", encoding="utf-8"
        )
        spec = agent.load("handler-contract", root=self.root)
        runner = RecordingRunner([
            ChildResult(("post",), 0, "done", ""),
        ])

        result = dispatch(
            Firing("handler-contract", str(self.root), "manual"),
            runner=runner,
        )

        self.assertTrue(result.ok, result)
        environment = runner.environments[0]
        self.assertEqual(spec.name, environment["AGENTS_LIVE_AGENT_NAME"])
        self.assertEqual(spec.identifier, environment["AGENTS_LIVE_AGENT_ID"])
        self.assertEqual(
            str(
                paths.repo_state_dir(self.root)
                / "logs"
                / f"{spec.identifier}.jsonl"
            ),
            environment["AGENTS_LIVE_LOG_FILE"],
        )

    @unittest.skipIf(os.name == "nt", "POSIX shebang execution")
    def test_shell_processors_honor_shebang_and_require_execute_permission(self) -> None:
        directory = self.skill("shell-pipeline", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.sh"',
            'agents-live.post-processor: "scripts/process.sh"',
        ])
        scripts = directory / "scripts"
        scripts.mkdir()
        pre = scripts / "prepare.sh"
        post = scripts / "process.sh"
        pre.write_text(
            "#!/usr/bin/env bash\nset -o pipefail\nprintf 'prepared'\n",
            encoding="utf-8",
        )
        post.write_text(
            "#!/usr/bin/env bash\nset -o pipefail\nprintf 'processed:%s' \"$(cat)\"\n",
            encoding="utf-8",
        )
        pre.chmod(0o755)
        post.chmod(0o755)

        result = dispatch(Firing("shell-pipeline", str(self.root), "manual"))
        self.assertTrue(result.ok, result)
        self.assertEqual("processed:prepared", result.text)

        post.chmod(0o644)
        result = dispatch(Firing("shell-pipeline", str(self.root), "manual"))
        self.assertFalse(result.ok)
        self.assertEqual("agent_invalid", result.category)
        self.assertIn("not executable", result.message)

    def test_six_shapes_are_pure_and_dispatch_uses_the_fake_runner(self) -> None:
        shapes = (
            ("agent-only", "fake", None, None, (False, True, False)),
            ("agent-post", "fake", None, "scripts/post.py", (False, True, True)),
            ("pre-agent", "fake", "scripts/pre.py", None, (True, True, False)),
            ("all", "fake", "scripts/pre.py", "scripts/post.py", (True, True, True)),
            ("scripted", "none", "scripts/pre.py", "scripts/post.py", (True, False, True)),
            ("post-only", "none", None, "scripts/post.py", (False, False, True)),
        )
        for name, selector, pre, post, expected in shapes:
            metadata = [f'agents-live.selector: "{selector}"']
            if pre:
                metadata.append(f'agents-live.pre-processor: "{pre}"')
            if post:
                metadata.append(f'agents-live.post-processor: "{post}"')
            directory = self.skill(name, metadata)
            scripts = directory / "scripts"
            scripts.mkdir(exist_ok=True)
            for path in filter(None, (pre, post)):
                (directory / path).write_text("print('ok')\n", encoding="utf-8")
            spec = agent.load(name, root=self.root)
            actual = agent.shape(spec)
            self.assertEqual(expected, (actual.has_pre, actual.has_agent, actual.has_post))
            outputs = []
            if actual.has_pre:
                outputs.append(ChildResult(("pre",), 0, "pre", ""))
            if actual.has_agent:
                outputs.append(ChildResult(
                    ("fake",), 0,
                    json.dumps({"text": "done", "structured": {"ok": True}}),
                    ""))
            if actual.has_post:
                outputs.append(ChildResult(("post",), 0, "post", ""))
            runner = RecordingRunner(outputs)
            result = dispatch(
                Firing(name, str(self.root), "manual"), runner=runner)
            self.assertTrue(result.ok, result)
            self.assertEqual(sum(expected), len(runner.argv))
            records = obs.load(obs.files(paths.repo_state_dir(self.root) / "logs"))
            identifier = agent.load(name, root=self.root).identifier
            completed = [
                record for record in records
                if record["agent_name"] == identifier
                and record["phase"] == "done"
            ]
            self.assertEqual("ok", completed[-1]["status"])

    def test_pipeline_post_processor_reads_the_resource_not_agent_stdout(self) -> None:
        directory = self.skill("pipeline", [
            'agents-live.selector: "fake"',
            'agents-live.mode: "pipeline"',
            'agents-live.post-processor: "scripts/post.py"',
        ])
        (directory / "scripts").mkdir()
        (directory / "scripts" / "post.py").write_text(
            "print('done')\n", encoding="utf-8")
        runner = RecordingRunner([
            ChildResult(("fake",), 0, json.dumps({"text": "narration"}), ""),
            ChildResult(("post",), 0, "done", ""),
        ])
        result = dispatch(
            Firing("pipeline", str(self.root), "manual"), runner=runner)
        self.assertTrue(result.ok, result)
        self.assertIsNone(runner.inputs[-1])

    def test_a_quiet_run_records_what_the_processor_produced(self) -> None:
        """A scheduled run is quiet and its streams go nowhere.

        Whatever the durable record does not capture is lost, so a
        processor's output and diagnostics have to reach the event.
        """
        directory = self.skill("noisy", [
            'agents-live.selector: "none"',
            'agents-live.schedule: "0 8 * * *"',
            'agents-live.post-processor: "scripts/report.py"',
        ])
        (directory / "scripts").mkdir()
        (directory / "scripts" / "report.py").write_text(
            "print('ok')\n", encoding="utf-8")
        identifier = agent.load("noisy", root=self.root).identifier
        state.replace(self.root, {identifier})
        runner = RecordingRunner([
            ChildResult(("post",), 0, "processed 3 files", "skipped 1 unreadable"),
        ])
        result = dispatch(
            Firing(identifier, str(self.root), "clock"),
            runner=runner,
            now=datetime(2026, 8, 9, 8, 0).astimezone(),
        )
        self.assertTrue(result.ok, result)

        records = obs.load(obs.files(paths.repo_state_dir(self.root) / "logs"))
        done = [r for r in records
                if r["agent_name"] == identifier and r["phase"] == "done"]
        message = str(done[-1]["message"])
        self.assertIn("processed 3 files", message)
        self.assertIn("skipped 1 unreadable", message)

    def test_a_definition_from_the_future_asks_for_an_upgrade(self) -> None:
        """A repository can be synced ahead of the tool that runs it.

        The definition is not malformed, so the run must not report it as
        the user's mistake: the remedy is to upgrade agents-live.
        """
        directory = self.root / "Agents" / "ahead"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            "---\nname: ahead\ndescription: Written for a later release.\n"
            "metadata:\n"
            f'  agents-live.schema-version: "{agent.SCHEMA_VERSION + 1}"\n'
            '  agents-live.selector: "none"\n'
            '  agents-live.schedule: "0 8 * * *"\n'
            "---\nbody\n",
            encoding="utf-8",
        )
        identifier = agent.BrokenDefinition(
            directory / "SKILL.md", "").identifier_in(self.root)
        state.replace(self.root, {identifier})
        result = dispatch(
            Firing(identifier, str(self.root), "clock"),
            runner=RecordingRunner([]),
            now=datetime(2026, 8, 9, 8, 0).astimezone(),
        )
        self.assertFalse(result.ok)
        self.assertEqual("runtime_outdated", result.category)
        self.assertIn("upgrade", result.message.lower())

        records = obs.load(obs.files(paths.repo_state_dir(self.root) / "logs"))
        done = [r for r in records
                if r["agent_name"] == identifier and r["phase"] == "done"]
        self.assertIn("upgrade", str(done[-1]["message"]).lower())

    def test_prepare_errors_become_failed_outcomes(self) -> None:
        self.skill("invalid-provider-options", [
            'agents-live.selector: "copilot:max"',
        ])
        runner = RecordingRunner([])
        result = dispatch(
            Firing("invalid-provider-options", str(self.root), "manual"),
            runner=runner,
        )
        self.assertFalse(result.ok)
        self.assertEqual("agent_invalid", result.category)

    def test_plan_agent_receives_project_mcp_definition(self) -> None:
        (self.root / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "repo-tool": {
                    "type": "stdio",
                    "command": "uv",
                    "args": ["run", "server.py"],
                    "env": {"SAFE_VALUE": "portable"},
                }
            }
        }), encoding="utf-8")
        self.skill("uses-project-mcp", [
            'agents-live.selector: "copilot"',
            'agents-live.mcps: "[\\"repo-tool\\"]"',
        ])
        runner = RecordingRunner([ChildResult(("copilot",), 0, "done", "")])

        result = dispatch(
            Firing("uses-project-mcp", str(self.root), "manual"),
            runner=runner,
        )

        self.assertTrue(result.ok, result)
        argv = runner.argv[-1]
        self.assertEqual(
            ("--output-format", "json"),
            argv[argv.index("--output-format"):argv.index("--output-format") + 2],
        )
        self.assertNotIn("--mcp", argv)
        config_path, payload = runner.mcp_configs[-1]
        self.assertEqual(
            "uv",
            payload["mcpServers"]["repo-tool"]["command"],
        )
        self.assertFalse(config_path.exists())

    def test_project_mcp_config_is_removed_after_cli_failure(self) -> None:
        (self.root / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "repo-tool": {"type": "stdio", "command": "uv"},
            }
        }), encoding="utf-8")
        self.skill("failing-project-mcp", [
            'agents-live.selector: "claude"',
            'agents-live.mcps: "[\\"repo-tool\\"]"',
        ])
        runner = RecordingRunner([
            ChildResult(("claude",), 1, "", "provider failed"),
        ])

        result = dispatch(
            Firing("failing-project-mcp", str(self.root), "manual"),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(1, len(runner.mcp_configs))
        self.assertFalse(runner.mcp_configs[0][0].exists())

    def test_pipeline_rejects_project_mcp_before_provider_start(self) -> None:
        (self.root / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "repo-tool": {"type": "stdio", "command": "uv"},
            }
        }), encoding="utf-8")
        self.skill("isolated-pipeline", [
            'agents-live.selector: "copilot"',
            'agents-live.mode: "pipeline"',
            'agents-live.mcps: "[\\"repo-tool\\"]"',
        ])
        runner = RecordingRunner([])

        result = dispatch(
            Firing("isolated-pipeline", str(self.root), "manual"),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual("agent_invalid", result.category)
        self.assertIn("pipeline mode", result.message)
        self.assertEqual([], runner.argv)

    def test_declared_mcp_without_project_definition_fails_before_cli(self) -> None:
        self.skill("missing-mcp", [
            'agents-live.selector: "copilot"',
            'agents-live.mcps: "[\\"missing\\"]"',
        ])
        runner = RecordingRunner([])

        result = dispatch(
            Firing("missing-mcp", str(self.root), "manual"),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual("agent_invalid", result.category)
        self.assertIn("missing", result.message)
        self.assertEqual([], runner.argv)

    def test_cli_argument_rejection_has_its_own_category(self) -> None:
        for returncode, message in (
            (1, "error: unknown option '--mcp'"),
            (2, "error: unexpected value for --mcp: repo-tool"),
        ):
            with self.subTest(returncode=returncode):
                self.skill(
                    f"bad-cli-flag-{returncode}",
                    ['agents-live.selector: "copilot"'],
                )
                runner = RecordingRunner([ChildResult(
                    ("copilot",), returncode, "", message,
                )])

                result = dispatch(
                    Firing(
                        f"bad-cli-flag-{returncode}",
                        str(self.root),
                        "manual",
                    ),
                    runner=runner,
                )

                self.assertFalse(result.ok)
                self.assertEqual("cli_argument_rejected", result.category)
                self.assertIn(message, result.message)

    def test_run_json_reports_the_outcome_not_only_the_text(self) -> None:
        self.skill("reported", ['agents-live.selector: "copilot:max"'])
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": "1"}),
            mock.patch.object(run.paths, "resolve_root", return_value=self.root),
            contextlib.redirect_stdout(stdout),
        ):
            code = run.main(["--name", "reported"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("failed", payload["status"])
        self.assertEqual("agent_invalid", payload["category"])
        self.assertTrue(payload["message"])

    def test_stop_withdraws_a_definition_whose_file_is_gone(self) -> None:
        directory = self.skill("departed", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        identifier = agent.load("departed", root=self.root).identifier
        state.replace(self.root, {identifier})
        shutil.rmtree(directory)
        host = MemoryHost()
        registry = {"repos": {"sample": str(self.root)}, "default_repo": "sample"}
        previous = runtime.current()
        runtime.configure(host)
        stdout = io.StringIO()
        try:
            with (
                mock.patch.object(stop.paths, "resolve_root", return_value=self.root),
                mock.patch.object(lifecycle.repos, "load", return_value=registry),
                contextlib.redirect_stdout(stdout),
            ):
                code = stop.main(["--name", "departed"])
        finally:
            runtime.configure(previous)
        self.assertEqual(0, code, stdout.getvalue())
        self.assertNotIn(identifier, state.load(self.root).agents)


class TestArchitectureFitness(unittest.TestCase):
    def test_dashboard_project_argument_uses_repo_selection(self) -> None:
        cli_main = importlib.import_module("agents_live.cli.main")
        root = Path(tempfile.gettempdir()).resolve()
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(cli_main.state, "resolve_root", return_value=root),
            mock.patch.object(cli_main.state, "clear_root_cache"),
            mock.patch.object(cli_main.subprocess, "run", return_value=completed) as run,
            mock.patch.object(cli_main.update_check, "interactive", return_value=False),
            mock.patch.object(upgrade_handoff, "reconcile"),
        ):
            code = cli_main.main(["dashboard", "--port", "9000", "."])
            selected = os.environ[state.ENV_VAR]

        self.assertEqual(0, code)
        self.assertEqual(str(root), selected)
        invocation = run.call_args.args[0]
        self.assertEqual(["--port", "9000"], invocation[-2:])
        self.assertNotIn(".", invocation)
        self.assertEqual(
            (None, ["list"]),
            cli_main._consume_project_argument(
                cli_main.COMMAND_BY_NAME["dashboard"], ["list"]),
        )
        self.assertEqual(
            (".", ["--port=9000"]),
            cli_main._consume_project_argument(
                cli_main.COMMAND_BY_NAME["dashboard"], ["--port=9000", "."]),
        )

    def test_dashboard_help_is_not_treated_as_a_project(self) -> None:
        cli_main = importlib.import_module("agents_live.cli.main")
        stdout = io.StringIO()
        with (
            mock.patch.object(cli_main.state, "resolve_root") as resolve_root,
            mock.patch.object(cli_main.subprocess, "run") as run,
            mock.patch.object(cli_main.update_check, "interactive", return_value=False),
            mock.patch.object(upgrade_handoff, "reconcile"),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli_main.main(["dashboard", "help"])

        self.assertEqual(0, code)
        self.assertIn("agents-live dashboard", stdout.getvalue())
        resolve_root.assert_not_called()
        run.assert_not_called()

    def test_dashboard_interrupt_exits_without_a_traceback(self) -> None:
        # Ctrl+C is the documented way to stop a foreground dashboard, so
        # it is the ordinary exit path and must not read like a crash. The
        # delivery of a real interrupt is platform-specific; the handling
        # under test is not (#249).
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            dashboard = importlib.import_module(
                "agents_live.cli.scripts.dashboard")
        with (
            mock.patch.object(dashboard, "__name__", "__main__"),
            mock.patch.object(dashboard.app, "is_started", False),
            mock.patch.object(dashboard, "port_conflict", return_value=None),
            mock.patch.object(dashboard.dashboards, "record"),
            mock.patch.object(dashboard, "build_page"),
            mock.patch.object(
                dashboard.ui, "run", side_effect=KeyboardInterrupt),
            mock.patch.object(
                sys, "argv", ["dashboard.py", "--port", "8231"]),
        ):
            dashboard.main()  # must return rather than propagate

    def test_dashboard_port_conflict_guidance_is_neutral(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            dashboard = importlib.import_module(
                "agents_live.cli.scripts.dashboard")
        conflict = "127.0.0.1:8231 is not available"
        with (
            mock.patch.object(dashboard, "__name__", "__main__"),
            mock.patch.object(dashboard.app, "is_started", False),
            mock.patch.object(
                dashboard, "port_conflict", return_value=conflict),
            mock.patch.object(dashboard.preflight, "emit_failure") as emit,
            mock.patch.object(
                sys, "argv", ["dashboard.py", "--port", "8231"]),
            self.assertRaises(SystemExit),
        ):
            dashboard.main()

        emit.assert_called_once_with(
            "dashboard",
            f"{conflict}; `agents-live dashboard list` shows dashboards "
            "started by this host; if one is listed on this port, "
            "`agents-live dashboard stop --port 8231` stops that recorded "
            "dashboard, but another listener may still hold the port; "
            "otherwise stop the holder with the owning system or retry with "
            "--port <other>",
            code="port_unavailable",
        )

    def test_dashboard_delayed_refresh_registers_before_client_disconnect(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            dashboard = importlib.import_module(
                "agents_live.cli.scripts.dashboard")
            callback = mock.Mock()
            dashboard._timer_after_first_interval(600.0, callback)

        timer_callback = nicegui.ui.timer.call_args.args[1]
        self.assertNotIn("immediate", nicegui.ui.timer.call_args.kwargs)
        timer_callback()
        callback.assert_not_called()
        timer_callback()
        callback.assert_called_once_with()

    def test_compatibility_shim_and_lazy_host_imports_resolve(self) -> None:
        from agents_live import hidden as legacy_hidden
        from agents_live.runtime.hosts import crontab, hidden, wsl_liveness

        self.assertIs(legacy_hidden.main, hidden.main)
        with mock.patch.object(
            crontab.hostruntime,
            "exclusive_lock",
            return_value=mock.MagicMock(),
        ):
            with crontab.lock():
                pass
        self.assertTrue(callable(wsl_liveness.state_dir))

    def test_windows_hidden_actions_accept_current_and_legacy_modules(self) -> None:
        for module in (
            "agents_live.runtime.hosts.hidden",
            "agents_live.hidden",
        ):
            with self.subTest(module=module):
                arguments = task_scheduler.argument_string(
                    ["-P", "-m", module, "agents-live.exe", "run", "sample"])
                self.assertEqual(
                    "agents-live.exe",
                    task_scheduler._action_program("pythonw.exe", arguments),
                )

    def test_legacy_heartbeat_wrapper_uses_internal_liveness_commands(self) -> None:
        wrapper = (
            Path(__file__).parents[1] / "src" / "agents_live" /
            "windows-heartbeat.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"$CLI" internal liveness', wrapper)
        self.assertIn('"$CLI" internal install-liveness', wrapper)
        self.assertNotIn('"$CLI" heartbeat', wrapper)

    def test_plugin_validation_accepts_exactly_the_groups_that_are_read(self) -> None:
        """A seam that validates one group and reads another connects nothing.

        A provider plugin declaring the retired group passed validation and
        was then never discovered, so a selector naming it failed at dispatch
        as an unknown provider.
        """
        self.assertEqual(
            {providers.ENTRY_POINT_GROUP, ownership.ENTRY_POINT_GROUP},
            set(plugins.ENTRY_POINT_GROUPS),
        )
        self.assertNotIn(
            "agents_live.agents", plugins.ENTRY_POINT_GROUPS)
        self.assertIn("agents_live.agents", plugins.RETIRED_ENTRY_POINT_GROUPS)

    def test_retired_plugin_registration_failure_does_not_crash_discovery(self) -> None:
        class Entry:
            value = "retired_plugin:register"

            @staticmethod
            def load():
                def register_plugin() -> None:
                    raise RuntimeError("registration failed")
                return register_plugin

        stderr = io.StringIO()
        with (
            mock.patch(
                "importlib.metadata.entry_points", return_value=[Entry()]),
            mock.patch("importlib.import_module"),
            contextlib.redirect_stderr(stderr),
        ):
            agent_adapters._discover_plugins()
        self.assertIn("retired agents_live.agents group", stderr.getvalue())
        self.assertIn("registration failed", stderr.getvalue())

    def test_upgrade_uses_tool_environment_receipt_outside_active_venv(self) -> None:
        tool_environment = Path("/uv/tools/agents-live")
        operation = mock.MagicMock()
        operation.__enter__.return_value = {}
        with (
            mock.patch.object(upgrade, "find_uv", return_value="uv"),
            mock.patch.object(upgrade.adminlog, "operation", return_value=operation),
            mock.patch.object(upgrade, "_refuse_while_held", return_value=False),
            mock.patch.object(upgrade.plugins, "launcher_stamp", return_value=None),
            mock.patch.object(upgrade, "_running_watchers", return_value=[]),
            mock.patch.object(upgrade.subprocess, "run", return_value=mock.Mock(returncode=0)),
            mock.patch.object(upgrade, "_report_stale_watchers"),
            mock.patch.object(
                upgrade.plugins, "tool_environment", return_value=tool_environment),
            mock.patch.object(upgrade.plugins, "converge") as converge_plugins,
            mock.patch.object(upgrade.plugins, "installed_version", return_value="6.0.1"),
        ):
            self.assertEqual(0, upgrade._upgrade_runtime([]))
        converge_plugins.assert_called_once_with(
            [], trigger="upgrade", pin_primary=False,
            receipt_environment=tool_environment)

    def test_a_retired_plugin_group_is_refused_even_beside_a_current_one(self) -> None:
        """Half-recognising a plugin is worse than refusing it.

        A 5.x distribution typically declares both an adapter and an
        ownership backend. Validating on the half that still exists left
        the other silently undiscovered, surfacing much later as an
        unknown provider at dispatch.
        """
        declaration = plugins.Plugin(
            name="example-plugin", path=Path("."), sha256=None, version=None)

        class Entry:
            def __init__(self, group: str) -> None:
                self.group, self.name = group, "example"

            def load(self):
                return object()

        class Distribution:
            version = "1.0.0"

            def __init__(self, groups):
                self.entry_points = [Entry(group) for group in groups]

        shapes = {
            ("agents_live.agents",): False,
            ("agents_live.agents", "agents_live.ownership"): False,
            ("agents_live.providers", "agents_live.ownership"): True,
            ("console_scripts",): False,
        }
        for groups, expected in shapes.items():
            with self.subTest(groups=groups):
                with mock.patch.object(
                    importlib.metadata, "distribution",
                    return_value=Distribution(groups),
                ):
                    ok, detail = plugins._installed_state(declaration)
                self.assertEqual(expected, ok, detail)
                if "agents_live.agents" in groups:
                    self.assertIn("retired", detail)
                    self.assertIn(providers.ENTRY_POINT_GROUP, detail)

    def test_a_retired_wheel_is_refused_before_it_is_installed(self) -> None:
        """Installing a plugin the release cannot load is worse than refusing.

        The installed one crashes every command that imports the legacy
        adapters, including the upgrade that would replace it, and it stays
        pending forever so convergence reinstalls it on each run.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / "Agents" / "plugins"
            directory.mkdir(parents=True)
            wheel = directory / "example_plugin-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "example_plugin-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: example-plugin\nVersion: 1.0\n")
                archive.writestr(
                    "example_plugin-1.0.dist-info/entry_points.txt",
                    "[agents_live.agents]\nexample = example_plugin:register\n")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            (root / ".agents-live.toml").write_text(
                "[plugins.example-plugin]\n"
                f'path = "Agents/plugins/{wheel.name}"\n'
                f'sha256 = "{digest}"\n',
                encoding="utf-8",
            )

            with self.assertRaises(plugins.PluginError) as caught:
                plugins.converge([root], trigger="test")
        self.assertIn("retired", str(caught.exception))
        self.assertIn(providers.ENTRY_POINT_GROUP, str(caught.exception))

    def test_upgrade_preflight_refuses_a_retired_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            agents = root / "Agents"
            agents.mkdir()
            (agents / "legacy.md").write_text(
                "---\nname: legacy\ndescription: Legacy definition.\n"
                "runtime: claude\nschedule: '0 8 * * *'\n---\nbody\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    upgrade, "_targets", return_value=([("legacy", root)], [])),
                mock.patch.object(upgrade, "_handoff_windows_upgrade") as handoff,
                mock.patch.dict(os.environ, {paths.ENV_VAR: ""}),
                mock.patch.object(sys, "argv", ["agents-live upgrade"]),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(1, upgrade.main())
        handoff.assert_not_called()
        self.assertIn("retired 5.x fields", stderr.getvalue())

    def test_upgrade_preflight_refuses_a_retired_plugin_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wheel = root / "example_plugin-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "example_plugin-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: example-plugin\nVersion: 1.0\n")
                archive.writestr(
                    "example_plugin-1.0.dist-info/entry_points.txt",
                    "[agents_live.agents]\nexample = example_plugin:register\n")
            (root / ".agents-live.toml").write_text(
                "[plugins.example-plugin]\n"
                f'path = "{wheel.name}"\n',
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    upgrade, "_targets", return_value=([("plugin", root)], [])),
                mock.patch.object(upgrade, "_handoff_windows_upgrade") as handoff,
                mock.patch.dict(os.environ, {paths.ENV_VAR: ""}),
                mock.patch.object(sys, "argv", ["agents-live upgrade"]),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(1, upgrade.main())
        handoff.assert_not_called()
        self.assertIn("retired entry-point group", stderr.getvalue())

    def test_plugin_probe_rejects_a_current_entry_point_that_cannot_load(
            self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "example_plugin-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "example_plugin/__init__.py", "import agents_live.ownership\n")
                archive.writestr(
                    "example_plugin-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: example-plugin\nVersion: 1.0\n")
                archive.writestr(
                    "example_plugin-1.0.dist-info/entry_points.txt",
                    "[agents_live.providers]\nexample = example_plugin:PROVIDER\n")
            script = (
                "import sys; "
                f"sys.path.insert(0, {str(wheel)!r}); "
                f"exec({plugins._COMPATIBILITY_PROBE!r})")
            completed = subprocess.run(
                [sys.executable, "-c", script, "example-plugin"],
                capture_output=True, text=True, check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("agents_live.ownership", completed.stderr)

    def test_plugin_probe_uses_provider_registration_invariants(self) -> None:
        source = (
            "class Provider:\n"
            "    models = None\n"
            "    efforts = frozenset()\n"
            "    def prepare(self, spec, request): pass\n"
            "    def parse(self, raw): pass\n"
            "PROVIDER = Provider()\n")
        for provider_name, expected in (
            ("", "provider name must not be empty"),
            ("fake", "provider 'fake' is already registered"),
        ):
            with self.subTest(provider_name=provider_name):
                with tempfile.TemporaryDirectory() as temporary:
                    wheel = Path(temporary) / "probe_plugin-1.0-py3-none-any.whl"
                    with zipfile.ZipFile(wheel, "w") as archive:
                        archive.writestr(
                            "probe_plugin/__init__.py",
                            f"{source}PROVIDER.name = {provider_name!r}\n")
                        archive.writestr(
                            "probe_plugin-1.0.dist-info/METADATA",
                            "Metadata-Version: 2.1\n"
                            "Name: probe-plugin\nVersion: 1.0\n")
                        archive.writestr(
                            "probe_plugin-1.0.dist-info/entry_points.txt",
                            "[agents_live.providers]\n"
                            "probe = probe_plugin:PROVIDER\n")
                    script = (
                        "import sys; "
                        f"sys.path.insert(0, {str(wheel)!r}); "
                        f"exec({plugins._COMPATIBILITY_PROBE!r})")
                    completed = subprocess.run(
                        [sys.executable, "-c", script, "probe-plugin"],
                        capture_output=True, text=True, check=False)
                self.assertNotEqual(0, completed.returncode)
                self.assertIn(expected, completed.stderr)

    def test_upgrade_plugin_probe_uses_the_candidate_runtime(self) -> None:
        plugin = plugins.Plugin(
            name="example-plugin", path=Path("example.whl"),
            sha256=None, version="1.0")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(plugins, "validation_errors", return_value=()),
            mock.patch.object(plugins, "union", return_value={"example": plugin}),
            mock.patch.object(plugins, "find_uv", return_value="uv"),
            mock.patch.object(
                plugins.subprocess, "run", return_value=completed) as run_probe,
        ):
            self.assertEqual((), plugins.compatibility_errors(
                [Path("project")], runtime_requirement="candidate.whl"))
        command = run_probe.call_args.args[0]
        self.assertIn(
            ["--with", "candidate.whl", "--with", "example.whl"],
            [command[index:index + 4] for index in range(len(command) - 3)])

    def test_cli_targets_resolve_from_owned_packages(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        for command in COMMANDS:
            for target in (command, *command.subcommands):
                with self.subTest(command=target.name, module=target.module):
                    if target.dispatch == "in-process":
                        importlib.import_module(f"agents_live.{target.module}")
                    else:
                        self.assertTrue((package / target.module).is_file())

    def test_subprocess_cli_targets_run_without_import_errors(self) -> None:
        """Existing on disk is not the same as being able to start.

        These targets are dispatched as scripts, so a package-relative import
        that only resolves in-process fails at the user, not in the suite.
        """
        package = Path(__file__).parents[1] / "src" / "agents_live"
        targets = sorted({
            target.module
            for command in COMMANDS
            for target in (command, *command.subcommands)
            if target.dispatch == "subprocess"
        })
        self.assertTrue(targets)
        checked = 0
        for module in targets:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, str(package / module), "--help"],
                    capture_output=True, text=True, timeout=180,
                )
                output = result.stdout + result.stderr
                if re.search(
                        r"ModuleNotFoundError: No module named '(?!agents_live)",
                        output):
                    continue  # an optional third-party dependency is absent
                self.assertNotIn(
                    "Traceback (most recent call last)", output,
                    f"{module}: {output}")
                self.assertEqual(0, result.returncode, f"{module}: {output}")
                checked += 1
        self.assertTrue(checked, "no subprocess target could be executed")
        retired_root_modules = {
            "activate.py", "adminlog.py", "completions.py", "crontasks.py", "dashboard.py",
            "dashboards.py", "definition_migrate.py", "doctor.py",
            "headless.py", "health_check.py", "heartbeat.py", "hostruntime.py",
            "init.py", "internal.py", "lifecycle.py", "migrate.py",
            "ownership.py", "pipeline_mcp.py", "pipeline_runtime.py",
            "qlog.py", "repos.py", "run.py", "schedules.py", "smoketest.py",
            "spawn.py", "start.py", "status.py", "stop.py", "timeline.py",
            "triggers.py", "uninstall.py", "update_check.py", "upgrade.py", "watchpolicy.py",
            "watchsource.py", "wintasks.py", "winwatch.py",
        }
        self.assertEqual(
            set(), retired_root_modules & {path.name for path in package.glob("*.py")})

    def test_dashboard_package_startup_and_agent_visibility(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                skill = root / "Agents" / "visible-agent"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "---\n"
                    "name: visible-agent\n"
                    "description: Verify dashboard agent rows.\n"
                    "metadata:\n"
                    '  agents-live.schema-version: "1"\n'
                    '  agents-live.selector: "fake/echo"\n'
                    '  agents-live.schedule: "0 8 * * *"\n'
                    "---\n"
                    "Report dashboard visibility.\n",
                    encoding="utf-8",
                )
                state_home = root / "state"
                with (
                    mock.patch.dict(os.environ, {
                        "AGENTS_LIVE_REPO": str(root),
                        "XDG_STATE_HOME": str(state_home),
                    }),
                    mock.patch.object(dashboard, "REPO_ROOT", root),
                    mock.patch.object(
                        dashboard, "LOGS_DIR",
                        paths.repo_state_dir(root) / "logs"),
                ):
                    paths.clear_cache()
                    try:
                        identifier = agent.load(
                            "visible-agent", root=root).identifier
                        state.replace(root, {identifier})
                        self.assertEqual(
                            ({}, {}),
                            dashboard._structured_log_snapshot(
                                {"visible-agent"}))
                        snapshot = dashboard.api_agents()
                        self.assertEqual(str(root), snapshot["repo"])
                        self.assertEqual(
                            [("visible-agent", "started")],
                            [(row["name"], row["state"])
                             for row in snapshot["agents"]],
                        )
                        self.assertTrue(snapshot["agents"][0]["can_pause"])
                        self.assertFalse(snapshot["agents"][0]["can_activate"])
                        state.replace(root, set())
                        stopped_snapshot = dashboard.api_agents()
                        self.assertFalse(
                            stopped_snapshot["agents"][0]["can_pause"])
                        self.assertTrue(
                            stopped_snapshot["agents"][0]["can_activate"])
                        with (
                            mock.patch.object(
                                dashboard.ownership, "local_only",
                                return_value=False),
                            mock.patch.object(
                                dashboard.ownership, "load_owners",
                                side_effect=ownership.OwnershipUnavailableError(
                                    "registry backend unavailable")),
                        ):
                            unavailable = dashboard.api_agents()["agents"][0]
                        self.assertEqual("Unavailable", unavailable["owner"])
                        self.assertFalse(unavailable["can_activate"])
                        self.assertFalse(unavailable["can_pause"])
                        self.assertFalse(unavailable["can_claim"])
                        state.replace(root, {identifier})
                        self.assertEqual(
                            snapshot["agents"],
                            dashboard._filtered_agent_rows(
                                snapshot["agents"], {
                                    "name": "", "state": "All",
                                    "owner": "All", "runtime": "All",
                                    "failing": False,
                                }),
                        )
                    finally:
                        paths.clear_cache()
            with mock.patch.object(sys, "argv", ["dashboard.py", "--help"]):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as stopped,
                ):
                    runpy.run_path(
                        dashboard.__file__, run_name="__mp_main__")
                self.assertEqual(0, stopped.exception.code)

    def test_ports_do_not_import_each_other_and_cli_stays_on_ports(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        runtime_imports = _imports(package / "runtime")
        agent_imports = _imports(package / "agent")
        self.assertFalse(any(name.startswith("agents_live.agent") for name in runtime_imports))
        self.assertFalse(any(name.startswith("agents_live.runtime") for name in agent_imports))
        allowed = {
            "agents_live.agent", "agents_live.dispatch", "agents_live.obs",
            "agents_live.runtime", "agents_live.state", "agents_live.cli",
            "agents_live.legacy",
        }
        for imported in _imports(package / "cli"):
            if imported.startswith("agents_live."):
                self.assertTrue(
                    any(imported == item or imported.startswith(f"{item}.") for item in allowed),
                    imported,
                )

    def test_platform_detection_is_confined_to_host_adapters_in_new_seams(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        hosts = package / "runtime" / "hosts"
        # Invariant 4 covers the package, not only the two ports: legacy/ is
        # the one exception, and it is removed in 7.0.
        for path in package.rglob("*.py"):
            if hosts in path.parents or (package / "legacy") in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("sys.platform", text, str(path))
            self.assertNotIn("os.name", text, str(path))


def _imports(directory: Path) -> set[str]:
    found = set()
    for path in directory.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = "agents_live." + ".".join(path.relative_to(
            directory.parents[0]).with_suffix("").parts)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = module.split(".")[:-1]
                    base = ".".join(parts[:len(parts) - node.level + 1])
                    name = f"{base}.{node.module}" if node.module else base
                else:
                    name = node.module or ""
                found.add(name)
    return found


if __name__ == "__main__":
    unittest.main()
