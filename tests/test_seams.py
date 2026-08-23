from __future__ import annotations

import ast
import base64
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
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

from agents_live import (
    agent, deploy, obs, paths, plugins, runtime, state,
)
from agents_live.cli import lifecycle, package_index, processor_check, upgrade_handoff
from agents_live.cli.main import main as cli_main
from agents_live.cli.commands import doctor, init, internal, run, status, stop, uninstall, upgrade
from agents_live.state import registry as repos
from agents_live.cli.spec import COMMANDS
from agents_live.legacy import migrate, triggers
from agents_live.agent import providers
from agents_live.agent import port
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
from agents_live.runtime import spawn
from agents_live.runtime.hosts import processes
from agents_live.runtime.hosts.processes import LocalChildRunner, LocalProcesses
from agents_live.runtime.watchloop import run as run_watchloop
from agents_live.runtime.hosts.posix import PosixHost
from agents_live.runtime.hosts.memory import MemoryHost
from agents_live.runtime.hosts.windows import WindowsHost, WindowsProcesses
from agents_live.runtime.hosts import system as hostruntime, task_scheduler
from agents_live.runtime.hosts import windows_watch as winwatch
from agents_live.runtime.hosts import filesystem as watchsource


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

    def skill(
        self,
        name: str,
        metadata: list[str],
        body: str = "Do the work.",
        version: str = "1",
    ) -> Path:
        directory = self.root / "Agents" / name
        directory.mkdir(parents=True)
        text = "\n".join([
            "---",
            f"name: {name}",
            "description: A portable test definition.",
            "metadata:",
            f'  agents-live.schema-version: "{version}"',
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

    def test_status_reports_consecutive_terminal_failures(self) -> None:
        self.skill("failing", ['agents-live.selector: "fake"'])
        identifier = agent.load("failing", root=self.root).identifier
        log = paths.repo_state_dir(self.root) / "logs" / f"{identifier}.jsonl"
        for index in range(3):
            obs.record(log, obs.Event(
                timestamp=f"2026-08-17T00:00:0{index}+00:00",
                event="run",
                status="failed",
                repository=str(self.root),
                agent=identifier,
                run_id=str(index),
                origin="clock",
            ))

        rows = status._rows(self.root)

        self.assertEqual(3, rows[0]["consecutive_failures"])

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
    def test_doctor_scopes_collection_to_selected_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "selected"
            unrelated = Path(temporary) / "unrelated"
            selected.mkdir()
            unrelated.mkdir()
            registry = {"repos": {
                "selected": str(selected),
                "unrelated": str(unrelated),
            }}
            collected = mock.Mock(
                unavailable_repositories=(), broken_definitions=(),
                unknown_metadata=())

            def resolve_root(value=None, **_kwargs):
                return Path(value) if value is not None else selected

            with (
                mock.patch.object(doctor.repos, "load", return_value=registry),
                mock.patch.object(
                    doctor.state, "resolve_root", side_effect=resolve_root),
                mock.patch.object(
                    doctor.runtime, "health", return_value=Health(True, "fresh")),
                mock.patch.object(
                    doctor, "_git_index_check", return_value=None) as git_check,
                mock.patch.object(doctor.state, "load"),
                mock.patch.object(doctor, "_damaged_records", return_value=0),
                mock.patch.object(
                    doctor.lifecycle, "collect", return_value=collected) as collect,
                mock.patch.object(doctor.hostruntime, "id", return_value="posix"),
                mock.patch.object(doctor, "_health_payload", return_value=None),
                mock.patch.object(
                    doctor.update_check, "interactive", return_value=False),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                doctor.main([])

            self.assertEqual(
                [mock.call(selected, "selected")], git_check.call_args_list)
            collect.assert_called_once_with(
                selected_roots=(selected,), persist=False)

    def test_doctor_all_repos_keeps_global_collection(self) -> None:
        collected = mock.Mock(
            unavailable_repositories=(), broken_definitions=(),
            unknown_metadata=())
        with (
            mock.patch.object(
                doctor.repos, "load", return_value={"repos": {}}),
            mock.patch.object(
                doctor.runtime, "health", return_value=Health(True, "fresh")),
            mock.patch.object(
                doctor.lifecycle, "collect", return_value=collected) as collect,
            mock.patch.object(doctor.hostruntime, "id", return_value="posix"),
            mock.patch.object(doctor, "_health_payload", return_value=None),
            mock.patch.object(
                doctor.update_check, "interactive", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            doctor.main(["--all-repos"])

        collect.assert_called_once_with(selected_roots=None, persist=False)

    def test_doctor_reports_which_channel_owns_the_installation(self) -> None:
        """An operator must be able to see the upgrade owner (#369).

        Ownership decides who may replace the runtime, and two channels
        that both believe they may will eventually race. Reporting it is
        additive here: nothing writes a generation layout yet, so an
        ordinary uv-managed host reports its owner and stays green.
        """
        collected = mock.Mock(
            unavailable_repositories=(), broken_definitions=(),
            unknown_metadata=())
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.dict(os.environ, {
                    deploy.layout.ENV_INSTALL_ROOT:
                        str(Path(temporary) / "install"),
                }),
                mock.patch.object(
                    doctor.repos, "load", return_value={"repos": {}}),
                mock.patch.object(
                    doctor.runtime, "health",
                    return_value=Health(True, "fresh")),
                mock.patch.object(
                    doctor.lifecycle, "collect", return_value=collected),
                mock.patch.object(doctor.hostruntime, "id", return_value="posix"),
                mock.patch.object(doctor, "_health_payload", return_value=None),
                mock.patch.object(
                    doctor.update_check, "interactive", return_value=False),
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--all-repos"])

        reported = [line for line in stdout.getvalue().splitlines()
                    if line.startswith("ok: installation:")]
        self.assertEqual(0, code, stdout.getvalue())
        self.assertEqual(1, len(reported), stdout.getvalue())
        self.assertTrue(
            any(label in reported[0]
                for label in deploy.ownership.LABELS.values()),
            reported[0])

    def test_doctor_distinguishes_ownership_modes(self) -> None:
        root = Path("C:/work/selected")
        with mock.patch.object(
                doctor.ownership, "local_only", return_value=True):
            local = doctor._ownership_check(root, "selected")
        self.assertTrue(local["ok"])
        self.assertIn("local-only", local["detail"])

        with (
            mock.patch.object(
                doctor.ownership, "local_only", return_value=False),
            mock.patch.object(
                doctor.ownership, "validate_registry",
                side_effect=ownership.OwnershipUnavailableError("missing")),
        ):
            unavailable = doctor._ownership_check(root, "selected")
        self.assertFalse(unavailable["ok"])
        self.assertIn("registry declared but unavailable", unavailable["detail"])

    def test_doctor_repair_scopes_convergence_to_selected_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary)
            collected = mock.Mock(
                unavailable_repositories=(), broken_definitions=(),
                unknown_metadata=())
            converged = mock.Mock(
                done=(), failed=(), health=Health(True, "fresh"))
            with (
                mock.patch.object(
                    doctor.repos, "load", return_value={"repos": {}}),
                mock.patch.object(
                    doctor.state, "resolve_root", return_value=selected),
                mock.patch.object(
                    doctor.runtime, "health",
                    return_value=Health(True, "fresh")),
                mock.patch.object(
                    doctor.lifecycle, "collect", return_value=collected),
                mock.patch.object(
                    doctor.lifecycle, "converge",
                    return_value=converged) as converge,
                mock.patch.object(doctor.hostruntime, "id", return_value="posix"),
                mock.patch.object(doctor, "_health_payload", return_value=None),
                mock.patch.object(
                    doctor.update_check, "interactive", return_value=False),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                doctor.main(["--repair"])

            converge.assert_called_once_with(
                selected_roots=(selected,), dry_run=False)

    def test_doctor_reports_a_refused_claude_shim_with_native_remediation(self) -> None:
        refused = runtime.hosts.system.ExecutableNotFound(
            "only shims answer to 'claude' on this host's PATH; "
            "claude.cmd is a batch shim")
        with mock.patch.object(
                doctor.hostruntime, "pin_executable", side_effect=refused):
            checks = doctor._provider_cli_checks({"claude"})

        self.assertEqual(1, len(checks))
        self.assertFalse(checks[0]["ok"])
        self.assertIn("only shims", checks[0]["detail"])
        self.assertIn("winget install Anthropic.ClaudeCode", checks[0]["detail"])
        self.assertNotIn("npm", checks[0]["detail"])

    def test_package_index_rejects_a_resolved_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resolver = root / "fake-uv.py"
            resolver.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "output = Path(args[args.index('--output-file') + 1])\n"
                "output.write_text('agents-live==5.5.2\\n')\n",
                encoding="utf-8",
            )
            result = package_index.check(
                "6.3.2", command=(sys.executable, str(resolver)))

        self.assertFalse(result.ok)
        self.assertEqual("5.5.2", result.resolved)
        self.assertIn("agents-live>=6.3.2 is required", result.detail)

    def test_processor_diagnosis_resolves_without_executing_the_processor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "Agents" / "processor-check"
            scripts = skill / "scripts"
            scripts.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: processor-check\n"
                "description: Check processor dependencies.\nmetadata:\n"
                '  agents-live.schema-version: "1"\n'
                '  agents-live.selector: "none"\n'
                '  agents-live.pre-processor: "scripts/prepare.py"\n'
                "---\nCheck dependencies.\n",
                encoding="utf-8",
            )
            executed = root / "processor-executed"
            processor = scripts / "prepare.py"
            task = scripts / "task.py"
            task.write_text(
                "# /// script\n"
                '# requires-python = ">=3.12"\n'
                '# dependencies = ["transitive-package"]\n'
                "# ///\n"
                f"from pathlib import Path\nPath({str(executed)!r}).touch()\n",
                encoding="utf-8",
            )
            processor.write_text(
                "# /// script\n"
                '# requires-python = ">=3.12"\n'
                '# dependencies = ["example-package"]\n'
                "# ///\n"
                "import subprocess\n"
                "subprocess.run([\"uv\", \"run\", \"--script\", "
                "\"Agents/processor-check/scripts/task.py\"])\n"
                f"from pathlib import Path\nPath({str(executed)!r}).touch()\n",
                encoding="utf-8",
            )
            calls = root / "resolver-calls.json"
            resolver = root / "fake-uv.py"
            resolver.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "path = Path(os.environ['CALLS'])\n"
                "with path.open('a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CALLS": str(calls)}):
                result = processor_check.diagnose(
                    root,
                    processor,
                    "ModuleNotFoundError: removed_api",
                    command=(sys.executable, str(resolver)),
                )
            call_args = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
            ]
            processor_executed = executed.exists()

        self.assertIsNotNone(result)
        self.assertIn("compatible version bound", result)
        self.assertFalse(processor_executed)
        self.assertEqual({str(processor.resolve()), str(task.resolve())}, {
            args[2] for args in call_args
        })
        for args in call_args:
            self.assertEqual("lock", args[0])
            self.assertEqual("--script", args[1])
            self.assertEqual(
                ["--dry-run", "--refresh", "--no-cache"], args[3:])

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
            beacon.write_text(json.dumps({
                "status": "healthy",
                "smoketest": {"status": "pass"},
            }), encoding="utf-8")
            fresh = time.time() - 59 * 60
            os.utime(beacon, (fresh, fresh))
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
            stale = time.time() - 61 * 60
            os.utime(beacon, (stale, stale))

            def refresh(_argv):
                beacon.write_text(json.dumps({
                    "status": "healthy",
                    "smoketest": {"status": "pass"},
                }), encoding="utf-8")
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

    def test_quick_explains_fresh_degraded_smoketest_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            beacon.write_text(json.dumps({
                "status": "degraded",
                "smoketest": {"status": "fail", "reason": "old failure"},
            }), encoding="utf-8")
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(doctor.internal, "main") as maintain,
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        self.assertEqual(1, code)
        maintain.assert_not_called()
        self.assertEqual({
            "ok": False,
            "checks": [{
                "check": "automatic maintenance",
                "ok": False,
                "category": "smoketest_failed",
                "detail": "current framework smoketest verdict is failed",
                "remedy": "agents-live smoketest",
                "source": "cached",
            }],
        }, json.loads(stdout.getvalue()))

    def test_quick_explains_cached_consecutive_agent_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            beacon.write_text(json.dumps({
                "status": "degraded",
                "smoketest": {"status": "pass"},
                "agent_failures": [{
                    "repository": "C:/repo",
                    "agent": "sample-123",
                    "consecutive_failures": 3,
                }],
            }), encoding="utf-8")
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(doctor.internal, "main") as maintain,
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        self.assertEqual(1, code)
        maintain.assert_not_called()
        check = json.loads(stdout.getvalue())["checks"][0]
        self.assertEqual("agent_repeated_failures", check["category"])
        self.assertIn("sample-123 has 3 consecutive failures", check["detail"])
        self.assertEqual(
            "agents-live logs --agent sample-123 --errors", check["remedy"])

    def test_quick_explains_unknown_smoketest_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            beacon.write_text(json.dumps({
                "status": "healthy",
                "smoketest": {"status": "error"},
            }), encoding="utf-8")

            stdout = io.StringIO()
            with (
                mock.patch.object(
                    doctor.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(doctor.internal, "main") as maintain,
                contextlib.redirect_stdout(stdout),
            ):
                code = doctor.main(["--quick"])

        self.assertEqual(1, code)
        maintain.assert_not_called()
        check = json.loads(stdout.getvalue())["checks"][0]
        self.assertEqual("smoketest_unknown", check["category"])
        self.assertEqual("agents-live smoketest", check["remedy"])

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
                "detail": "automatic maintenance wrote no fresh valid health record",
                "source": "refresh-failed",
                "category": "health_record_missing",
                "remedy": "agents-live doctor",
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
            "automatic maintenance could not refresh health",
            payload["checks"][0]["detail"],
        )
        self.assertEqual(
            "maintenance_failed", payload["checks"][0]["category"])

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
    def test_ownership_backend_receives_the_repository_root(self) -> None:
        root = Path("C:/work/selected")
        backend = mock.Mock()
        backend.load_owners.return_value = {"sample": "*"}
        backend.registry_file_exists.return_value = True
        backend.remove_owner.return_value = True
        with (
            mock.patch.object(ownership, "mode", return_value="registry"),
            mock.patch.object(ownership, "_backend", return_value=backend),
            mock.patch.object(
                ownership, "_require_backend", return_value=backend),
        ):
            self.assertEqual(
                {"sample": "*"}, ownership.load_owners(root=root))
            self.assertTrue(ownership.registry_file_exists(root=root))
            ownership.set_owner("sample", "*", root=root)
            ownership.remove_owner("sample", root=root)

        backend.load_owners.assert_any_call(
            rate_limit_secs=60, root=root)
        backend.registry_file_exists.assert_called_once_with(root=root)
        backend.set_owner.assert_called_once_with("sample", "*", root=root)
        backend.remove_owner.assert_called_once_with("sample", root=root)

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
            root = Path(temporary) / "repo"
            root.mkdir()
            state.replace(root, {"sample-123"})
            started_before = (
                paths.repo_state_dir(root) / "started.json"
            ).read_bytes()
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
                mock.patch.object(upgrade, "within", return_value=True),
                mock.patch.object(upgrade, "find_uv", return_value="uv.exe"),
                mock.patch.object(
                    upgrade.hostruntime, "locks_running_image", return_value=True),
                mock.patch.object(
                    upgrade.hostruntime, "process_command_lines", return_value=[]),
                mock.patch.object(upgrade.dashboards, "running", return_value=[]),
                mock.patch.object(
                    upgrade, "watchers_on_host",
                    return_value=[
                        (101, "sample-123", str(root)),
                        (102, "sample-123", str(root)),
                    ],
                ),
                mock.patch.object(
                    upgrade.runtime, "current",
                    return_value=mock.Mock(supervisor=supervisor)),
                mock.patch.object(
                    upgrade.adminlog, "operation",
                    return_value=contextlib.nullcontext({})) as operation,
                mock.patch.object(upgrade.adminlog, "record") as record,
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    0, upgrade._handoff_windows_upgrade(None, runtime_only=False))
            defer = supervisor.defer_until_environment_exits
            command = defer.call_args.args[0]
            self.assertEqual(
                ["uv.exe", "tool", "run", "--isolated", "--refresh", "--from",
                 f"agents-live>={upgrade.__version__}"],
                command[:7])
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
            payload = json.loads(pending[0].read_text(encoding="utf-8"))
            self.assertEqual(42, payload["helper"]["pid"])
            self.assertEqual(
                [{"name": "sample-123", "project": str(root)}],
                payload["quiesce_watchers"],
            )
            self.assertTrue(payload["quiesce_active"])
            self.assertEqual(
                started_before,
                (paths.repo_state_dir(root) / "started.json").read_bytes(),
            )
            record.assert_called_once()
            self.assertEqual(
                "quiesce-requested",
                record.call_args.kwargs["upgrade_phase"],
            )
            self.assertEqual(operation_id, record.call_args.kwargs["correlation_id"])

    def test_windows_upgrade_keeps_dashboard_as_fail_closed_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            environment.mkdir(parents=True)
            supervisor = mock.Mock()
            stderr = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}),
                mock.patch.object(
                    upgrade.hostruntime, "id", return_value=upgrade.hostruntime.WINDOWS),
                mock.patch.object(
                    upgrade.hostruntime, "locks_running_image", return_value=True),
                mock.patch.object(
                    upgrade.plugins, "tool_environment", return_value=environment),
                mock.patch.object(upgrade, "within", return_value=True),
                mock.patch.object(upgrade, "find_uv", return_value="uv.exe"),
                mock.patch.object(upgrade, "_running_watchers", return_value=[]),
                mock.patch.object(
                    upgrade.dashboards, "running",
                    return_value=[{"pid": 88, "port": 8231}],
                ),
                mock.patch.object(
                    upgrade.hostruntime, "process_command_lines", return_value=[]),
                mock.patch.object(
                    upgrade, "watchers_on_host", return_value=[]),
                mock.patch.object(
                    upgrade.runtime, "current",
                    return_value=mock.Mock(supervisor=supervisor)),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(
                    1, upgrade._handoff_windows_upgrade(None, runtime_only=False))
            supervisor.defer_until_environment_exits.assert_not_called()
            self.assertIn("dashboard on port 8231", stderr.getvalue())
            self.assertEqual(
                [],
                list((state_home / "agents-live" / "upgrade-handoffs").glob(
                    "*.pending.json")),
            )

    def test_upgrade_disables_quiescence_before_restoring_watchers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            environment.mkdir(parents=True)
            executable = environment / "Scripts" / "python.exe"
            with mock.patch.dict(
                    os.environ, {"XDG_STATE_HOME": str(state_home)}):
                claim, existing = upgrade_handoff.claim(
                    environment, source="candidate.whl", runtime_only=False)
                self.assertIsNone(existing)
                self.assertIsNotNone(claim)
                assert claim is not None
                upgrade_handoff.request_quiescence(
                    claim, [(101, "sample", "C:/repo")])

                self.assertEqual(
                    claim.operation_id,
                    upgrade_handoff.quiesce_operation(executable),
                )
                self.assertEqual(
                    (("sample", "C:/repo"),),
                    upgrade_handoff.begin_restoration(claim.operation_id),
                )
                self.assertIsNone(
                    upgrade_handoff.quiesce_operation(executable))

    def test_quiescence_is_not_visible_before_request_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_home = Path(temporary) / "state"
            environment = Path(temporary) / "tools" / "agents-live"
            environment.mkdir(parents=True)
            executable = environment / "Scripts" / "python.exe"
            recording = threading.Event()
            release_record = threading.Event()
            query_started = threading.Event()
            query_done = threading.Event()
            observed: list[str | None] = []

            def record(*_args, **_kwargs) -> None:
                recording.set()
                self.assertTrue(release_record.wait(timeout=5))

            with (
                mock.patch.dict(
                    os.environ, {"XDG_STATE_HOME": str(state_home)}),
                mock.patch.object(
                    upgrade_handoff.adminlog, "record", side_effect=record),
            ):
                claim, existing = upgrade_handoff.claim(
                    environment, source="candidate.whl", runtime_only=False)
                self.assertIsNone(existing)
                assert claim is not None
                request = threading.Thread(
                    target=upgrade_handoff.request_quiescence,
                    args=(claim, [(101, "sample", "C:/repo")]),
                )

                def query() -> None:
                    query_started.set()
                    observed.append(
                        upgrade_handoff.quiesce_operation(executable))
                    query_done.set()

                watcher = threading.Thread(target=query)
                request.start()
                self.assertTrue(recording.wait(timeout=5))
                watcher.start()
                self.assertTrue(query_started.wait(timeout=5))
                self.assertFalse(query_done.is_set())
                release_record.set()
                request.join(timeout=5)
                watcher.join(timeout=5)

            self.assertFalse(request.is_alive())
            self.assertFalse(watcher.is_alive())
            self.assertEqual([claim.operation_id], observed)

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
                mock.patch.object(upgrade, "within", return_value=True),
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

    def test_upgrade_continuation_fails_when_convergence_has_no_targets(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(upgrade, "_targets", return_value=([], [])),
            mock.patch.object(
                upgrade.lifecycle, "converge",
                side_effect=RuntimeError("metadata convergence failed")),
            mock.patch.object(
                sys, "argv", ["agents-live", "--skills-only"]),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(1, upgrade.main())
        self.assertIn("metadata convergence failed", stderr.getvalue())

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
        metadata = runtime.artifacts.from_rendered(rendered.rendered)
        self.assertIsNotNone(metadata)
        self.assertEqual(subscription.key, metadata.id)
        self.assertEqual("repo:/tmp/example", metadata.scope)
        self.assertEqual("agent:sample", metadata.target)
        self.assertIsNone(metadata.origin)
        self.assertNotIn("--watch-expression", rendered.rendered)
        self.assertIn("--watch-expression", rendered.watcher_argv)
        for obsolete in (
            "--artifact-marker", "--runtime-role", "--subscription-key",
            "--subscription-fingerprint",
        ):
            self.assertNotIn(obsolete, rendered.rendered)
            self.assertNotIn(obsolete, rendered.watcher_argv)

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

    def test_windows_maintenance_artifact_reaches_the_real_cli(self) -> None:
        with mock.patch(
            "agents_live.runtime.hosts.windows.shutil.which",
            return_value="C:/tools/agents-live.exe",
        ):
            rendered = WindowsHost().render(
                lifecycle.maintenance_subscription())
        argv = json.loads(rendered.rendered)["argv"]
        result = mock.Mock(done=(), failed=(), health=Health(True))
        collected = mock.Mock(subscriptions=())
        with (
            mock.patch.object(internal.lifecycle, "converge", return_value=result),
            mock.patch.object(internal.lifecycle, "collect", return_value=collected),
            mock.patch(
                "agents_live.cli.main.state.resolve_root",
                side_effect=ValueError("no project root"),
            ),
        ):
            self.assertEqual(0, cli_main([*argv[1:], "--dry-run"]))

    def test_runtime_metadata_accepts_only_its_canonical_encoding(self) -> None:
        metadata = runtime.artifacts.InvocationMetadata(
            "0123456789abcdef01234567", "runtime:test", "runtime")
        canonical = runtime.artifacts.encode(metadata)
        self.assertEqual(metadata, runtime.artifacts.decode(canonical))

        payload = canonical.removeprefix(runtime.artifacts.PREFIX)
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        parsed = json.loads(raw)
        variants = (
            f" {canonical}",
            canonical + "=",
            runtime.artifacts.PREFIX + base64.urlsafe_b64encode(
                json.dumps(parsed).encode()).decode().rstrip("="),
            runtime.artifacts.PREFIX + base64.urlsafe_b64encode(
                b'{"target":"runtime","scope":"runtime:test",'
                b'"id":"0123456789abcdef01234567"}').decode().rstrip("="),
            runtime.artifacts.PREFIX + payload[:-1] + "+",
        )
        for value in variants:
            with self.subTest(value=value):
                self.assertIsNone(runtime.artifacts.decode(value))

    def test_scheduled_maintenance_records_its_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            beacon = root / "health.ok"
            log = root / "admin.log"
            result = mock.Mock(done=(), failed=(), health=Health(True))
            collected = mock.Mock(subscriptions=())
            metadata = runtime.artifacts.InvocationMetadata(
                "0123456789abcdef01234567", "runtime:test", "runtime")
            with (
                mock.patch.object(
                    internal.lifecycle, "converge", return_value=result),
                mock.patch.object(
                    internal.lifecycle, "collect", return_value=collected),
                mock.patch.object(
                    internal.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    internal.paths, "resolve_root", side_effect=ValueError),
                mock.patch.object(obs.admin, "log_path", return_value=log),
            ):
                self.assertEqual(
                    0,
                    internal.main(["maintain", "--quiet"], metadata=metadata),
                )

            records = [
                item for item in obs.load((log,))
                if item.get("operation") == "maintenance"
            ]
            self.assertEqual(
                ["start", "ok"], [item["status"] for item in records])
            self.assertEqual(records[0]["run_id"], records[1]["run_id"])
            self.assertEqual(0, records[1]["exit_code"])
            self.assertEqual("scheduler", records[1]["source"])
            self.assertEqual(metadata.id, records[1]["subscription_id"])
            self.assertEqual("healthy", records[1]["health"])
            self.assertEqual(0, records[1]["watchers"])
            self.assertEqual(0, records[1]["cron"])

    def test_failed_maintenance_records_a_complete_terminal_event(self) -> None:
        scenarios = (
            (
                "collection unavailable",
                mock.Mock(done=(), failed=(), health=Health(True)),
                lifecycle.CollectionUnavailable("registry unavailable"),
                "unknown",
            ),
            (
                "unhealthy convergence",
                mock.Mock(
                    done=(), failed=(),
                    health=Health(False, "stale", detail=("beacon stale",))),
                mock.Mock(subscriptions=()),
                "unhealthy",
            ),
        )
        required = {
            "exit_code", "convergence_changes", "convergence_failures",
            "health", "watchers", "cron", "repositories", "smoketest",
            "message",
        }
        for label, result, collected, expected_health in scenarios:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / "admin.log"
                with (
                    mock.patch.object(
                        internal.lifecycle, "converge", return_value=result),
                    mock.patch.object(
                        internal.lifecycle, "collect",
                        side_effect=(collected if isinstance(
                            collected, Exception) else None),
                        return_value=(None if isinstance(
                            collected, Exception) else collected)),
                    mock.patch.object(obs.admin, "log_path", return_value=log),
                ):
                    self.assertEqual(1, internal.main(["maintain", "--quiet"]))
                records = [
                    item for item in obs.load((log,))
                    if item.get("operation") == "maintenance"
                ]
                self.assertEqual(["start", "error"], [
                    item["status"] for item in records])
                self.assertEqual(records[0]["run_id"], records[1]["run_id"])
                self.assertTrue(required.issubset(records[1]))
                self.assertEqual(expected_health, records[1]["health"])

    def test_internal_maintain_refreshes_the_host_health_beacon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            beacon = root / "health.ok"
            beacon.write_text(json.dumps({
                "status": "degraded",
                "smoketest": {
                    "status": "fail",
                    "reason": "previous failure",
                },
            }), encoding="utf-8")
            result_path = paths.repo_state_dir(root) / "logs" / \
                "smoketest-framework-result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(json.dumps({
                "status": "pass",
                "duration_s": 0.1,
                "runtime": "fake",
            }), encoding="utf-8")
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
                mock.patch.object(
                    internal.paths, "resolve_root", return_value=root),
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
            self.assertEqual("pass", payload["smoketest"]["status"])
            self.assertNotIn("reason", payload["smoketest"])

            result_path.write_text(json.dumps({
                "status": "fail",
                "duration_s": 0.1,
                "runtime": "fake",
                "reason": "current failure",
            }), encoding="utf-8")
            converge_maintenance.return_value = result
            with (
                mock.patch.object(
                    internal.lifecycle, "converge", converge_maintenance),
                mock.patch.object(
                    internal.lifecycle, "collect", return_value=collected),
                mock.patch.object(
                    internal.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    internal.paths, "resolve_root", return_value=root),
            ):
                self.assertEqual(0, internal.main(["maintain", "--quiet"]))
            payload = json.loads(beacon.read_text(encoding="utf-8"))
            self.assertEqual("degraded", payload["status"])
            self.assertEqual("fail", payload["smoketest"]["status"])
            self.assertEqual("current failure", payload["smoketest"]["reason"])

    def test_consecutive_failures_ignore_skips_and_reset_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "agent.jsonl"
            outcomes = (
                ("first", "success"),
                ("second", "failed"),
                ("second", "skipped"),
                ("first", "failed"),
                ("second", "failed"),
                ("first", "failed"),
            )
            for index, (identifier, status_value) in enumerate(outcomes):
                event_name = "firing" if status_value == "skipped" else "run"
                obs.record(log, obs.Event(
                    timestamp=f"2026-08-17T00:00:0{index}+00:00",
                    event=event_name,
                    status=status_value,
                    repository=str(temporary),
                    agent=identifier,
                    run_id=str(index),
                    origin="clock",
                ))

            streaks = obs.consecutive_failures((log,))

        self.assertEqual({"first": 2, "second": 2}, streaks)

    def test_maintenance_degrades_for_active_consecutive_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            beacon = state_dir / "health.ok"
            log = state_dir / "logs" / "scheduled-id.jsonl"
            for index in range(3):
                obs.record(log, obs.Event(
                    timestamp=f"2026-08-17T00:00:0{index}+00:00",
                    event="run",
                    status="failed",
                    repository=str(root),
                    agent="scheduled-id",
                    run_id=str(index),
                    origin="clock",
                ))
            subscriptions = (
                Subscription.create(
                    scope=f"repo:{root}", target="scheduled-id",
                    kind="schedule", trigger="0 8 * * *"),
            )
            result = mock.Mock(failed=(), health=Health(True, "not-required"))
            collected = mock.Mock(subscriptions=subscriptions)
            with (
                mock.patch.object(internal.lifecycle, "converge", return_value=result),
                mock.patch.object(internal.lifecycle, "collect", return_value=collected),
                mock.patch.object(
                    internal.paths, "health_beacon_path", return_value=beacon),
                mock.patch.object(
                    internal.paths, "repo_state_dir", return_value=state_dir),
                mock.patch.object(internal.paths, "resolve_root", return_value=root),
            ):
                self.assertEqual(0, internal.main(["maintain", "--quiet"]))

            payload = json.loads(beacon.read_text(encoding="utf-8"))

        self.assertEqual("degraded", payload["status"])
        self.assertEqual([{
            "repository": str(root),
            "agent": "scheduled-id",
            "consecutive_failures": 3,
        }], payload["agent_failures"])

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

    def test_uninstall_refusal_preserves_structured_runtime(self) -> None:
        host = MemoryHost()
        subscription = Subscription.create(
            scope="repo:/tmp/example", target="agent:sample",
            kind="watch", trigger="src/** debounce 1s")
        self.assertFalse(converge((subscription,), _host=host).failed)
        with (
            mock.patch.object(uninstall.runtime, "current", return_value=host),
            mock.patch.object(
                uninstall.plugins, "tool_environment",
                return_value=Path("C:/tools/agents-live"),
            ),
            mock.patch.object(
                uninstall, "_stop_own_watchers",
                return_value=[(42, "sample", "/tmp/example")],
            ),
            mock.patch.object(uninstall.preflight, "emit_failure"),
        ):
            self.assertEqual(1, uninstall.main([]))
        self.assertEqual(1, len(host.trigger_store.list()))
        self.assertEqual(1, len(host.supervisor.owned("watcher")))

    def test_uninstall_wsl_cleanup_failure_preserves_structured_runtime(
            self) -> None:
        host = MemoryHost()
        subscription = Subscription.create(
            scope="repo:/tmp/example", target="agent:sample",
            kind="schedule", trigger="0 8 * * *")
        self.assertFalse(converge((subscription,), _host=host).failed)
        with (
            mock.patch.object(uninstall.runtime, "current", return_value=host),
            mock.patch.object(
                uninstall.plugins, "tool_environment",
                return_value=Path("/tools/agents-live"),
            ),
            mock.patch.object(uninstall, "_stop_own_watchers", return_value=[]),
            mock.patch.object(
                uninstall.hostruntime, "id", return_value=uninstall.hostruntime.WSL),
            mock.patch.object(
                uninstall.wsl_liveness, "uninstall",
                side_effect=RuntimeError("blocked"),
            ),
            mock.patch.object(uninstall.preflight, "emit_failure"),
        ):
            self.assertEqual(1, uninstall.main([]))
        self.assertEqual(1, len(host.trigger_store.list()))

    def test_windows_uninstall_handoff_failure_preserves_structured_runtime(
            self) -> None:
        host = MemoryHost()
        subscription = Subscription.create(
            scope="repo:/tmp/example", target="agent:sample",
            kind="schedule", trigger="0 8 * * *")
        self.assertFalse(converge((subscription,), _host=host).failed)
        with (
            mock.patch.object(uninstall.runtime, "current", return_value=host),
            mock.patch.object(
                uninstall.plugins, "tool_environment",
                return_value=Path("C:/tools/agents-live"),
            ),
            mock.patch.object(uninstall, "_stop_own_watchers", return_value=[]),
            mock.patch.object(
                uninstall.hostruntime, "id",
                return_value=uninstall.hostruntime.WINDOWS,
            ),
            mock.patch.object(uninstall, "find_uv", return_value="uv.exe"),
            mock.patch.object(
                uninstall, "_handoff_windows_uninstall", return_value=False),
            mock.patch.object(uninstall.preflight, "emit_failure"),
            mock.patch.object(uninstall.completions, "remove", return_value=[]),
            mock.patch.object(uninstall, "_sweep_triggers"),
        ):
            self.assertEqual(1, uninstall.main([]))
        self.assertEqual(1, len(host.trigger_store.list()))


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

    def _migrate_with_legacy(
        self,
        host: MemoryHost,
        *args: str,
        registry: dict | None = None,
    ) -> int:
        previous = runtime.current()
        runtime.configure(host)
        try:
            with (
                mock.patch.object(
                    lifecycle.repos,
                    "load",
                    return_value=registry or {
                        "repos": {},
                        "default_repo": None,
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return migrate.main(list(args))
        finally:
            runtime.configure(previous)

    def test_internal_migrate_adopts_and_replaces_legacy_trigger(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        spec = agent.load("sample", root=self.root)
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}

        self.assertEqual(0, self._migrate_with_legacy(host))

        self.assertIn(spec.identifier, state.load(self.root).agents)
        self.assertEqual(set(), host.legacy[str(self.root)])
        self.assertTrue(any(
            item.target == f"agent:{spec.identifier}"
            for item in host.trigger_store.list()
        ))

    def test_active_watcher_quiesces_after_dispatch_without_restart(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1ms"',
        ])

        class Source:
            def __init__(self) -> None:
                self.started = False
                self.stopped = False
                self.polls = 0

            def start(self) -> None:
                self.started = True

            def poll(self, _timeout: float | None) -> list[str]:
                self.polls += 1
                return [str(self.root / "src" / "changed.py")] \
                    if self.polls == 1 else []

            def stop(self) -> None:
                self.stopped = True

        source = Source()
        source.root = self.root
        request = {"operation": None}
        dispatched: list[str] = []
        host = mock.Mock()
        host.change_source.return_value = source
        args = mock.Mock()
        args.name = "sample"
        args.watch_expression = None
        args.subscription_key = "watch-key"
        args.subscription_fingerprint = "watch-fingerprint"

        def fire(firing) -> mock.Mock:
            dispatched.append(firing.agent_id)
            request["operation"] = "upgrade-operation"
            return mock.Mock()

        with (
            mock.patch.object(internal.runtime, "current", return_value=host),
            mock.patch.object(internal, "dispatch", side_effect=fire),
            mock.patch.object(
                internal.upgrade_handoff,
                "quiesce_operation",
                side_effect=lambda _executable: request["operation"],
            ),
            mock.patch.object(internal, "_restart_watcher") as restart,
            mock.patch.object(internal.adminlog, "record") as record,
        ):
            self.assertEqual(0, internal._watch(args))

        self.assertEqual(["sample"], dispatched)
        self.assertTrue(source.started)
        self.assertTrue(source.stopped)
        restart.assert_not_called()
        record.assert_called_once()
        self.assertEqual(
            "upgrade-operation", record.call_args.kwargs["correlation_id"])

    def test_idle_watcher_quiesces_before_poll_without_restart(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1s"',
        ])

        source = mock.Mock()
        host = mock.Mock()
        host.change_source.return_value = source
        args = mock.Mock()
        args.name = "sample"
        args.watch_expression = None
        args.subscription_key = "watch-key"
        args.subscription_fingerprint = "watch-fingerprint"
        with (
            mock.patch.object(internal.runtime, "current", return_value=host),
            mock.patch.object(
                internal.upgrade_handoff,
                "quiesce_operation",
                return_value="upgrade-operation",
            ),
            mock.patch.object(internal, "dispatch") as dispatch_run,
            mock.patch.object(internal, "_restart_watcher") as restart,
            mock.patch.object(internal.adminlog, "record") as record,
        ):
            self.assertEqual(0, internal._watch(args))

        source.start.assert_called_once_with()
        source.poll.assert_not_called()
        source.stop.assert_called_once_with()
        dispatch_run.assert_not_called()
        restart.assert_not_called()
        self.assertEqual("quiesced", record.call_args.kwargs["upgrade_phase"])

    def test_watch_records_lifecycle_trigger_and_degradation_events(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1ms"',
        ])
        spec = agent.load("sample", root=self.root)

        class Source:
            def __init__(self, root: Path) -> None:
                self.root = root
                self.started = False
                self.stopped = False
                self.polls = 0
                self._reporter = None

            def set_reporter(self, reporter) -> None:
                self._reporter = reporter

            def start(self) -> None:
                self.started = True

            def poll(self, _timeout: float | None) -> list[str]:
                self.polls += 1
                if self.polls == 1:
                    assert self._reporter is not None
                    self._reporter("queue-drop", {
                        "dropped_directory_count": 1,
                        "rescan_directory_count": 1,
                    })
                    return [str(self.root / "src" / "changed.py")]
                return []

            def stop(self) -> None:
                self.stopped = True

        source = Source(self.root)
        request = {"operation": None}
        host = mock.Mock()
        host.change_source.return_value = source
        args = mock.Mock()
        args.name = "sample"
        args.watch_expression = None

        def fire(firing):
            self.assertEqual(1, len(firing.changed_files))
            self.assertEqual(1, firing.debounce_ms)
            request["operation"] = "upgrade-operation"
            return mock.Mock()

        with (
            mock.patch.object(internal.runtime, "current", return_value=host),
            mock.patch.object(internal, "dispatch", side_effect=fire),
            mock.patch.object(
                internal.upgrade_handoff,
                "quiesce_operation",
                side_effect=lambda _executable: request["operation"],
            ),
            mock.patch.object(internal, "_restart_watcher"),
        ):
            self.assertEqual(0, internal._watch(args))

        self.assertTrue(source.started)
        self.assertTrue(source.stopped)
        records = [
            item for item in obs.load(
                obs.files(paths.repo_state_dir(self.root) / "logs"))
            if item["agent_name"] == spec.identifier and item["phase"] == "watcher"
        ]
        self.assertEqual(4, len(records))
        self.assertEqual("start", records[0]["status"])
        self.assertEqual(1, records[0]["watch_root_count"])
        self.assertEqual(1, records[0]["watch_debounce_ms"])
        self.assertEqual("degraded", records[1]["status"])
        self.assertEqual("queue-drop", records[1]["degradation"])
        self.assertEqual("ok", records[2]["status"])
        self.assertEqual(1, records[2]["matched_path_count"])
        self.assertEqual(1, records[2]["watch_debounce_ms"])
        self.assertEqual("ok", records[3]["status"])
        self.assertEqual("quiesce", records[3]["stop_reason"])

    def test_terminal_watch_failure_records_a_durable_agent_failure(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1s"',
        ])
        spec = agent.load("sample", root=self.root)
        state.record(self.root, spec.identifier)
        source = mock.Mock()
        source.poll.side_effect = watchsource.WatchFailed("watch root is gone")
        host = mock.Mock()
        host.change_source.return_value = source
        args = mock.Mock()
        args.name = "sample"
        args.watch_expression = None
        stderr = io.StringIO()
        with (
            mock.patch.object(internal.runtime, "current", return_value=host),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(1, internal._watch(args))
        self.assertIn("watch root is gone", stderr.getvalue())
        records = [
            item for item in obs.load(
                obs.files(paths.repo_state_dir(self.root) / "logs"))
            if item["agent_name"] == spec.identifier
        ]
        self.assertEqual(["watcher", "watcher", "done"], [
            item["phase"] for item in records
        ])
        self.assertEqual("error", records[1]["status"])
        self.assertEqual("watch_failed", records[1]["error_category"])
        self.assertEqual("watch_failed", records[2]["error_category"])
        rows = status._rows(self.root)
        sample = next(item for item in rows if item["identifier"] == spec.identifier)
        self.assertEqual("started", sample["state"])
        self.assertEqual(1, sample["consecutive_failures"])

    def test_windows_source_reports_overflow_queue_drop_and_truncated_rescan(self) -> None:
        watched = self.root / "src"
        source = winwatch.WindowsEventSource([str(watched)])
        fake_watch = mock.Mock()
        fake_watch.directory = watched
        fake_watch.dropped = threading.Event()
        fake_watch.dropped.set()
        source._watches = [fake_watch]
        reported: list[tuple[str, dict[str, object]]] = []
        source.set_reporter(lambda kind, payload: reported.append((kind, payload)))
        source.events.put(("overflow", str(watched)))
        with mock.patch.object(
            winwatch,
            "rescan",
            return_value=([str(watched / "changed.py")], True),
        ):
            self.assertEqual([str(watched / "changed.py")], source.poll(0))
        self.assertEqual(
            ["overflow", "queue-drop", "truncated-rescan"],
            [kind for kind, _payload in reported],
        )
        self.assertEqual(1, reported[0][1]["overflowed_directory_count"])
        self.assertEqual(1, reported[1][1]["dropped_directory_count"])
        self.assertEqual(winwatch.RESCAN_FILE_LIMIT, reported[2][1]["rescan_file_limit"])

    def test_upgrade_continuation_restores_quiesced_started_watcher(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1s"',
        ])
        spec = agent.load("sample", root=self.root)
        state.replace(self.root, {spec.identifier})
        before = (paths.repo_state_dir(self.root) / "started.json").read_bytes()
        host = MemoryHost()
        previous = runtime.current()
        runtime.configure(host)
        try:
            with (
                mock.patch.object(lifecycle.repos, "load", return_value={
                    "repos": {"sample": str(self.root)},
                    "default_repo": "sample",
                }),
                mock.patch.object(
                    upgrade.upgrade_handoff,
                    "begin_restoration",
                    return_value=((spec.identifier, str(self.root)),),
                ),
                mock.patch.object(
                    upgrade.adminlog,
                    "record",
                    side_effect=lambda _name, **_fields: self.assertEqual(
                        1, len(host.supervisor.owned("watcher"))),
                ) as record,
            ):
                self.assertEqual(
                    0, upgrade._restore_quiesced_watchers("upgrade-operation"))
        finally:
            runtime.configure(previous)

        self.assertEqual(
            before, (paths.repo_state_dir(self.root) / "started.json").read_bytes())
        self.assertEqual(1, len(host.supervisor.owned("watcher")))
        self.assertEqual(
            "upgrade-operation", record.call_args.kwargs["correlation_id"])
        self.assertEqual("restore", record.call_args.kwargs["upgrade_phase"])

    def test_failed_upgrade_restoration_leaves_intent_for_maintenance(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1s"',
        ])
        spec = agent.load("sample", root=self.root)
        state.replace(self.root, {spec.identifier})
        host = MemoryHost()
        previous = runtime.current()
        runtime.configure(host)
        failed = mock.Mock(
            failed=((mock.Mock(key="watch-key"), "blocked"),),
            health=Health(False, detail=("watcher restore blocked",)),
        )
        try:
            with (
                mock.patch.object(lifecycle.repos, "load", return_value={
                    "repos": {"sample": str(self.root)},
                    "default_repo": "sample",
                }),
                mock.patch.object(
                    upgrade.upgrade_handoff,
                    "begin_restoration",
                    return_value=((spec.identifier, str(self.root)),),
                ),
                mock.patch.object(upgrade.adminlog, "record"),
            ):
                with mock.patch.object(
                        upgrade.lifecycle, "converge", return_value=failed):
                    self.assertEqual(
                        1,
                        upgrade._restore_quiesced_watchers(
                            "upgrade-operation"),
                    )
                self.assertIn(spec.identifier, state.load(self.root).agents)
                self.assertFalse(lifecycle.converge().failed)
        finally:
            runtime.configure(previous)

        self.assertEqual(1, len(host.supervisor.owned("watcher")))

    def test_internal_migrate_preserves_legacy_trigger_when_replacement_fails(
            self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}
        with mock.patch.object(
                host.trigger_store, "install", side_effect=RuntimeError("blocked")):
            self.assertEqual(1, self._migrate_with_legacy(host))
        self.assertEqual({"sample"}, host.legacy[str(self.root)])

    def test_internal_migrate_dry_run_preserves_artifacts_and_started_state(
            self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}

        self.assertEqual(0, self._migrate_with_legacy(host, "--dry-run"))

        self.assertEqual({"sample"}, host.legacy[str(self.root)])
        self.assertEqual([], host.trigger_store.list())
        self.assertFalse(
            (paths.repo_state_dir(self.root) / "started.json").exists())

    def test_internal_migrate_starts_modern_watcher_before_retiring_legacy(
            self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1s"',
        ])
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}

        def retire(_pid: int) -> None:
            self.assertEqual(1, len(host.supervisor.owned("watcher")))

        with (
            mock.patch.object(
                migrate, "_legacy_watchers", return_value=[(42, "sample")]),
            mock.patch.object(migrate.hostruntime, "terminate", side_effect=retire) as terminate,
        ):
            self.assertEqual(0, self._migrate_with_legacy(host))
        terminate.assert_called_once_with(42)

    def test_internal_migrate_leaves_other_repositories_untouched(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        with tempfile.TemporaryDirectory() as temporary:
            other = Path(temporary).resolve()
            skill = other / "Agents" / "other"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: other\ndescription: Other repository.\nmetadata:\n"
                '  agents-live.schema-version: "1"\n'
                '  agents-live.selector: "fake"\n'
                '  agents-live.schedule: "0 9 * * *"\n'
                "---\nbody\n",
                encoding="utf-8",
            )
            other_spec = agent.load("other", root=other)
            other_subscription = Subscription.create(
                scope=f"repo:{other}",
                target=f"agent:{other_spec.identifier}",
                kind="schedule",
                trigger="0 9 * * *",
            )
            host = MemoryHost()
            host.trigger_store.install(host.render(other_subscription))
            host.legacy[str(self.root)] = {"sample"}
            host.legacy[str(other)] = {"other"}

            self.assertEqual(
                0,
                self._migrate_with_legacy(host, registry={
                    "repos": {"other": str(other)},
                    "default_repo": "other",
                }),
            )

            self.assertEqual({"other"}, host.legacy[str(other)])
            self.assertTrue(any(
                item.key == other_subscription.key
                for item in host.trigger_store.list()
            ))

    def test_internal_migrate_preserves_unmapped_other_repository_watcher(
            self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        host = MemoryHost()
        other = Subscription.create(
            scope="repo:/other", target="agent:other",
            kind="watch", trigger="docs/** debounce 1s")
        rendered = host.render(other)
        process = host.supervisor.spawn_detached(
            rendered.watcher_argv,
            role="watcher",
            key=rendered.key,
            fingerprint=rendered.fingerprint,
        )
        host.legacy[str(self.root)] = {"sample"}

        self.assertEqual(0, self._migrate_with_legacy(host))

        self.assertTrue(host.supervisor.alive(process))

    def test_internal_migrate_does_not_retire_unmatched_watcher(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.watch: "src/** debounce 1s"',
        ])
        host = MemoryHost()
        host.legacy[str(self.root)] = {"sample"}
        with (
            mock.patch.object(
                migrate,
                "_legacy_watchers",
                return_value=[(42, "sample"), (43, "missing")],
            ),
            mock.patch.object(migrate.hostruntime, "terminate") as terminate,
        ):
            self.assertEqual(0, self._migrate_with_legacy(host))
        terminate.assert_called_once_with(42)

    def test_internal_migrate_converts_task_scheduler_artifact(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])

        class WindowsMigrationHost(MemoryHost):
            def render(self, subscription: Subscription):
                return WindowsHost.render(self, subscription)

        spec = agent.load("sample", root=self.root)
        host = WindowsMigrationHost()
        host.legacy[str(self.root)] = {"sample"}
        with (
            mock.patch.object(
                migrate.hostruntime,
                "native_scheduler",
                return_value=migrate.hostruntime.TASK_SCHEDULER,
            ),
            mock.patch.object(
                task_scheduler.shutil,
                "which",
                return_value="C:/tools/agents-live.exe",
            ),
        ):
            self.assertEqual(0, self._migrate_with_legacy(host))

        installed = next(
            item
            for item in host.trigger_store.list()
            if item.target == f"agent:{spec.identifier}"
        )
        rendered = json.loads(installed.rendered)
        self.assertEqual("0 8 * * *", rendered["schedule"])
        metadata = runtime.artifacts.from_argv(rendered["argv"])
        self.assertIsNotNone(metadata)
        self.assertEqual(installed.key, metadata.id)
        self.assertEqual("clock", metadata.origin)
        self.assertEqual(
            [
                "C:/tools/agents-live.exe",
                "--repo",
                str(self.root),
                "run",
            "--metadata",
            runtime.artifacts.encode(metadata),
                "--name",
                spec.identifier,
                "--quiet",
            ],
            rendered["argv"],
        )
        self.assertEqual(set(), host.legacy[str(self.root)])

    def test_internal_migrate_adopts_old_root_into_current_subscription(
            self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        spec = agent.load("sample", root=self.root)
        old_root = Path("Z:/agents-live-old-root-does-not-exist").resolve()
        old_token = old_root.as_posix()
        old_line = (
            f"0 8 * * * cd {old_token} && agents-live --repo {old_token} "
            "run --name sample"
        )
        host = MemoryHost()
        with (
            mock.patch.object(
                migrate.hostruntime, "native_scheduler",
                return_value=migrate.hostruntime.CRONTAB,
            ),
            mock.patch.object(migrate.crontab, "lines", return_value=[old_line]),
            mock.patch.object(
                migrate.crontab, "lock", return_value=contextlib.nullcontext()),
            mock.patch.object(migrate.crontab, "write") as write,
            mock.patch.object(migrate, "_legacy_watchers", return_value=[]),
        ):
            self.assertEqual(
                0,
                self._migrate_with_legacy(host, "--adopt", str(old_root)),
            )

        write.assert_called_once_with([])
        self.assertIn(spec.identifier, state.load(self.root).agents)
        self.assertTrue(any(
            item.target == f"agent:{spec.identifier}"
            for item in host.trigger_store.list()
        ))

    def test_internal_migrate_adoption_json_is_one_document(self) -> None:
        self.skill("sample", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        old_root = Path("Z:/agents-live-json-old-root").resolve()
        old_token = old_root.as_posix()
        matched = (
            f"0 8 * * * cd {old_token} && agents-live --repo {old_token} "
            "run --name sample"
        )
        unmatched = (
            f"0 9 * * * cd {old_token} && agents-live --repo {old_token} "
            "run --name missing"
        )
        host = MemoryHost()
        previous = runtime.current()
        runtime.configure(host)
        stdout = io.StringIO()
        try:
            with (
                mock.patch.dict(
                    os.environ, {migrate.preflight.JSON_ENV_VAR: "1"}),
                mock.patch.object(
                    lifecycle.repos, "load",
                    return_value={"repos": {}, "default_repo": None}),
                mock.patch.object(
                    migrate.hostruntime, "native_scheduler",
                    return_value=migrate.hostruntime.CRONTAB,
                ),
                mock.patch.object(
                    migrate.crontab, "lines", return_value=[matched, unmatched]),
                mock.patch.object(
                    migrate.crontab, "lock", return_value=contextlib.nullcontext()),
                mock.patch.object(migrate.crontab, "write"),
                mock.patch.object(migrate, "_legacy_watchers", return_value=[]),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    0, migrate.main(["--adopt", str(old_root)]))
        finally:
            runtime.configure(previous)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual([unmatched], payload["unmatched"])

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

    def test_collection_loads_each_repository_ownership_registry(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first_temporary,
            tempfile.TemporaryDirectory() as second_temporary,
        ):
            first = Path(first_temporary).resolve()
            second = Path(second_temporary).resolve()
            for root, name in ((first, "first-agent"), (second, "second-agent")):
                (root / ".agents-live.toml").write_text(
                    'ownership = "registry"\n', encoding="utf-8")
                skill = root / "Agents" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    f"---\nname: {name}\n"
                    "description: Registry-managed definition.\nmetadata:\n"
                    '  agents-live.schema-version: "1"\n'
                    '  agents-live.selector: "fake"\n'
                    '  agents-live.schedule: "0 9 * * *"\n'
                    "---\nbody\n",
                    encoding="utf-8",
                )
            first_spec = agent.load("first-agent", root=first)
            second_spec = agent.load("second-agent", root=second)
            state.replace(first, {first_spec.identifier})
            state.replace(second, {second_spec.identifier})
            host = MemoryHost()
            previous = runtime.current()
            runtime.configure(host)
            loaded: list[Path] = []

            def owners_for(*, root, **_kwargs):
                loaded.append(root)
                owner = (
                    f"other/windows/{'a' * 32}"
                    if root == first else ownership.WILDCARD
                )
                return {"first-agent": owner, "second-agent": owner}

            try:
                with (
                    mock.patch.object(lifecycle.repos, "load", return_value={
                        "repos": {"first": str(first), "second": str(second)},
                        "default_repo": "first",
                    }),
                    mock.patch.object(
                        ownership, "load_owners", side_effect=owners_for),
                ):
                    collected = lifecycle.collect(persist=False)
            finally:
                runtime.configure(previous)

            targets = {item.target for item in collected.subscriptions}
            self.assertEqual({first, second}, set(loaded))
            self.assertNotIn(f"agent:{first_spec.identifier}", targets)
            self.assertIn(f"agent:{second_spec.identifier}", targets)

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

    def test_windows_init_reports_powershell_completion_profile_line(self) -> None:
        project = self.root / "candidate"
        global_root = self.root / "global"
        completion = self.root / "data" / "powershell" / \
            "agents-live-completion.ps1"
        convergence = mock.Mock(failed=())
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["agents-live init"]),
            mock.patch.dict(
                os.environ, {"AGENTS_LIVE_INIT_REPO": str(project)}),
            mock.patch.object(init.paths, "global_root", return_value=global_root),
            mock.patch.object(init.plugins, "converge", return_value=False),
            mock.patch.object(init, "initialize", return_value=True),
            mock.patch.object(init.repos, "ensure_default"),
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch.object(
                init.completions, "update_best_effort",
                return_value=(completion,)),
            mock.patch.object(init.lifecycle, "converge", return_value=convergence),
            mock.patch.object(init.adminlog, "record"),
            contextlib.redirect_stdout(stdout),
        ):
            code = init.main()

        self.assertEqual(0, code)
        output = stdout.getvalue()
        self.assertIn(str(completion), output)
        self.assertIn(f". '{completion}'", output)
        self.assertIn("$PROFILE", output)


class TestRuntimeProcessPolicy(unittest.TestCase):
    def test_posix_supervisor_uses_host_spawn_policy(self) -> None:
        process = mock.Mock(pid=42)
        with mock.patch.object(
                processes.system, "spawn_detached", return_value=process) as spawn:
            reference = LocalProcesses().spawn_detached(
                ["agents-live", "internal", "watch-loop", "sample"],
                role="watcher",
                key="subscription",
                fingerprint="fingerprint",
            )

        spawn.assert_called_once_with(
            ["agents-live", "internal", "watch-loop", "sample"],
            cwd=None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(42, reference.pid)

    def test_watcher_enumeration_is_exact_and_installation_scoped(self) -> None:
        environment = Path("C:/tools/agents-live")
        metadata = runtime.artifacts.encode(runtime.artifacts.InvocationMetadata(
            "0123456789abcdef01234567",
            "repo:C:/work/one",
            "agent:metadata-sample",
        ))
        rows = [
            (
                101,
                "C:/tools/agents-live/agents-live.exe --repo C:/work/one "
                "internal watch-loop sample",
            ),
            (
                102,
                "C:/other/agents-live.exe --repo C:/work/two internal "
                "watch-loop other",
            ),
            (103, "python worker.py --name sample"),
            (
                104,
                "C:/tools/agents-live/agents-live.exe --repo C:/work/one "
                f"internal watch-loop --metadata {metadata} metadata-sample",
            ),
        ]
        with mock.patch.object(
                processes.system, "process_command_lines", return_value=rows):
            self.assertEqual(
                [
                    (101, "sample", "C:/work/one"),
                    (104, "metadata-sample", "C:/work/one"),
                ],
                processes.watchers_on_host(under=environment),
            )

    def test_idle_watcher_retires_after_runtime_replacement(self) -> None:
        class QuietSource:
            def __init__(self) -> None:
                self.started = False
                self.stopped = False
                self.timeouts: list[float | None] = []

            def start(self) -> None:
                self.started = True

            def poll(self, timeout: float | None) -> list[str]:
                self.timeouts.append(timeout)
                return []

            def stop(self) -> None:
                self.stopped = True

        source = QuietSource()
        handoffs: list[bool] = []
        with (
            mock.patch.object(internal, "__version__", "6.3.2"),
            mock.patch.object(
                internal.importlib.metadata,
                "version",
                side_effect=("6.3.2", "6.3.3"),
            ),
        ):
            run_watchloop(
                source,
                parse_watch("docs/**"),
                root=Path.cwd(),
                fire=lambda _changed: self.fail("quiet watcher dispatched"),
                should_continue=internal._runtime_is_current,
                on_retire=lambda: handoffs.append(source.stopped),
                idle_check_s=60,
            )

        self.assertTrue(source.started)
        self.assertTrue(source.stopped)
        self.assertEqual([60], source.timeouts)
        self.assertEqual([True], handoffs)

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
    def test_logs_read_what_the_repository_wrote(self) -> None:
        """Agents write one file each, so there is no single log to default to.

        Naming one meant a bare query answered from a file nothing
        creates, which reports emptiness rather than absence.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "Agents").mkdir()
            (root / ".agents-live.toml").write_text("", encoding="utf-8")
            logs = (
                root / "state" / "agents-live" / "repos"
                / paths.repo_state_key(root) / "logs"
            )
            logs.mkdir(parents=True)
            (logs / "note-index-b53040b14b.jsonl").write_text(
                '{"log_schema":5,"ts":"2026-08-11T22:00:00Z",'
                '"agent_name":"note-index-b53040b14b","phase":"done",'
                '"status":"ok"}\n',
                encoding="utf-8",
            )
            self.assertFalse((logs / "agents-live.log").exists())
            environment = {
                **os.environ,
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            for arguments in ((), ("--agent", "note-index"), ("note-index",)):
                with self.subTest(arguments=arguments):
                    completed = subprocess.run(
                        [
                            sys.executable, "-m", "agents_live.cli", "--repo",
                            str(root), "logs", *arguments, "--limit", "5",
                            "--format", "jsonl",
                        ],
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    self.assertEqual(
                        0, completed.returncode,
                        completed.stdout + completed.stderr,
                    )
                    records = [
                        json.loads(line)
                        for line in completed.stdout.splitlines()
                        if line.strip()
                    ]
                    self.assertEqual(
                        ["note-index-b53040b14b"],
                        [record["agent_name"] for record in records],
                    )

    def test_every_reader_agrees_on_a_written_run_status(self) -> None:
        """One written value must not come to mean two things by entry point.

        `qlog.build_view` rewrites `success` in SQL and `query.normalize`
        rewrites it in Python. They are separate code paths that have to
        stay in step, so the Python half is pinned here. A schema-5
        record is a processor's own row and passes through untouched,
        which is why the contract reserves run-outcome statuses.
        """
        run_record = obs.query.normalize({
            "spec": 1,
            "timestamp": "2026-08-11T22:00:00Z",
            "event": "run",
            "status": "success",
            "agent": "sample",
            "run_id": "abc",
            "origin": "manual",
        })
        processor_record = obs.query.normalize({
            "log_schema": 5,
            "ts": "2026-08-11T22:00:00Z",
            "agent_name": "sample",
            "phase": "collect",
            "status": "swept",
        })

        self.assertIsNotNone(run_record)
        self.assertEqual("ok", run_record["status"])
        self.assertEqual("done", run_record["phase"])
        self.assertIsNotNone(processor_record)
        self.assertEqual("swept", processor_record["status"])

    def test_upgrade_watcher_fields_survive_admin_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admin.log"
            with mock.patch.object(obs.admin, "log_path", return_value=path):
                obs.admin.record(
                    "upgrade-watchers",
                    status="ok",
                    correlation_id="upgrade-operation",
                    upgrade_phase="quiesced",
                    watcher="sample-123",
                    root="C:/repo",
                    message="watcher quiesced",
                )
            records = obs.load((path,))
        self.assertEqual(1, len(records))
        self.assertEqual("upgrade-operation", records[0]["run_id"])
        self.assertEqual("upgrade-watchers", records[0]["operation"])
        self.assertEqual("quiesced", records[0]["upgrade_phase"])
        self.assertEqual("sample-123", records[0]["watcher"])
        self.assertEqual("C:/repo", records[0]["root"])

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


class TestProcessorContractVersion2(TempRepository):
    """The schema-2 child contract, and what version 1 still does instead."""

    def _echo_environment(self, directory: Path, name: str = "process.py") -> Path:
        (directory / "scripts").mkdir(exist_ok=True)
        script = directory / "scripts" / name
        script.write_text(
            "import json, os\n"
            "print(json.dumps({key: value for key, value in os.environ.items()\n"
            "                  if key.startswith('AGENTS_LIVE_')}))\n",
            encoding="utf-8",
        )
        return script

    def test_a_pipeline_post_processor_reads_the_result_from_stdin(self) -> None:
        """A class 0 post-processor survives the move to pipeline mode.

        Version 1 handed it nothing here, so this also proves the change is
        additive for a program that ignores stdin.
        """
        directory = self.skill("published", [
            'agents-live.selector: "fake/echo"',
            'agents-live.mode: "pipeline"',
            'agents-live.result-path: "/output/verdict"',
            'agents-live.post-processor: "scripts/process.py"',
        ], body=(
            "Publish the verdict.\n\n"
            "```put /output/verdict\n"
            '{"action": "update"}\n'
            "```\n"
        ), version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "import sys\nprint('received:' + sys.stdin.read().strip())\n",
            encoding="utf-8",
        )

        result = dispatch(Firing("published", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        self.assertIn('"action": "update"', result.text)
        self.assertTrue(result.text.startswith("received:"), result.text)

    def test_an_unpassable_change_set_fails_before_anything_spawns(self) -> None:
        """Trimming the list would let a processor skip work silently.

        Refusing is louder and keeps the promise that what a processor
        reads is every path that changed.
        """
        directory = self.skill("many-files", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "raise SystemExit('nothing should spawn')\n", encoding="utf-8")
        crowd = tuple(f"docs/{index:06d}-{'p' * 60}.md" for index in range(4000))

        result = dispatch(Firing(
            "many-files", str(self.root), "manual", changed_files=crowd))

        self.assertFalse(result.ok)
        self.assertEqual("invocation_input_overflow", result.category)
        self.assertIn("4000 changed files", result.message)
        self.assertIn("agents-live.watch", result.message)

    def test_unpassable_instructions_fail_before_anything_spawns(self) -> None:
        """The model and the processors must not see different text."""
        directory = self.skill("long-brief", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "raise SystemExit('nothing should spawn')\n", encoding="utf-8")

        result = dispatch(Firing(
            "long-brief", str(self.root), "manual",
            instructions="x" * (port.ENVIRONMENT_VALUE_MAX_BYTES + 1)))

        self.assertFalse(result.ok)
        self.assertEqual("invocation_input_overflow", result.category)
        self.assertIn("instructions need", result.message)

    def test_multibyte_instructions_are_bounded_by_encoded_size(self) -> None:
        directory = self.skill("wide-brief", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "raise SystemExit('nothing should spawn')\n", encoding="utf-8")

        result = dispatch(Firing(
            "wide-brief", str(self.root), "manual",
            instructions="\U0001f642" * (32 * 1024 // 4 + 1)))

        self.assertFalse(result.ok)
        self.assertEqual("invocation_input_overflow", result.category)
        self.assertIn("bytes", result.message)

    def test_direct_dispatch_options_share_the_environment_bound(self) -> None:
        directory = self.skill("many-options", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "raise SystemExit('nothing should spawn')\n", encoding="utf-8")

        result = dispatch(Firing(
            "many-options", str(self.root), "manual",
            options=(("brief", "x" * (32 * 1024)),)))

        self.assertFalse(result.ok)
        self.assertEqual("invocation_input_overflow", result.category)
        self.assertIn("options need", result.message)

    def test_options_reach_the_post_processor_and_keep_an_empty_value(self) -> None:
        """An empty value is a value; not supplying one is not."""
        directory = self.skill("post-options", [
            'agents-live.selector: "none"',
            'agents-live.post-processor: "scripts/process.py"',
        ], version="2")
        self._echo_environment(directory)

        result = dispatch(Firing(
            "post-options", str(self.root), "manual",
            options=(("account", ""), ("dry-run", True))))

        self.assertTrue(result.ok, result)
        environment = json.loads(result.text)
        self.assertEqual("post", environment["AGENTS_LIVE_ROLE"])
        options = json.loads(environment["AGENTS_LIVE_OPTIONS"])
        self.assertEqual({"account": "", "dry-run": True}, options)
        self.assertIn("account", options)
        self.assertNotIn("never-supplied", options)

    def test_a_change_set_that_fits_arrives_whole(self) -> None:
        directory = self.skill("some-files", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        self._echo_environment(directory)
        crowd = tuple(f"docs/{index:04d}.md" for index in range(300))

        result = dispatch(Firing(
            "some-files", str(self.root), "manual", changed_files=crowd))

        self.assertTrue(result.ok, result)
        environment = json.loads(result.text)
        self.assertEqual(
            list(crowd), json.loads(environment["AGENTS_LIVE_CHANGED_FILES"]))

    def test_watch_firings_record_matched_path_count_and_debounce(self) -> None:
        self.skill("watch-observed", ['agents-live.selector: "fake/echo"'], version="2")
        spec = agent.load("watch-observed", root=self.root)
        state.record(self.root, spec.identifier)

        result = dispatch(Firing(
            spec.identifier,
            str(self.root),
            "watch",
            changed_files=("docs/a.md", "docs/b.md", "docs/c.md"),
            debounce_ms=1200,
        ))

        self.assertTrue(result.ok, result)
        log = paths.repo_state_dir(self.root) / "logs" / f"{spec.identifier}.jsonl"
        records = [
            item for item in obs.load((log,))
            if item["phase"] == "done" and item["run_id"] == result.run_id
        ]
        self.assertEqual(1, len(records))
        self.assertEqual(3, records[0]["matched_path_count"])
        self.assertEqual(1200, records[0]["watch_debounce_ms"])

    def test_an_option_value_survives_spaces_quotes_and_semicolons(self) -> None:
        directory = self.skill("hostile", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        self._echo_environment(directory)
        awkward = 'a b "c" ; rm -rf / && echo $HOME \'x\''

        result = dispatch(Firing(
            "hostile", str(self.root), "manual",
            options=(("account", awkward),)))

        self.assertTrue(result.ok, result)
        environment = json.loads(result.text)
        self.assertEqual(
            awkward, json.loads(environment["AGENTS_LIVE_OPTIONS"])["account"])

    def test_a_processor_receives_no_argument_it_was_not_given(self) -> None:
        """Agents Live appends nothing, which is what lets a class 0
        processor keep a strict argument parser."""
        directory = self.skill("bare-argv", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/argv.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        script = directory / "scripts" / "argv.py"
        script.write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )

        result = dispatch(Firing(
            "bare-argv", str(self.root), "manual",
            instructions="do the thing",
            options=(("dry-run", True), ("account", "team-inbox")),
        ))

        self.assertTrue(result.ok, result)
        self.assertEqual([], json.loads(result.text))

    def test_instructions_and_option_values_stay_out_of_the_event_log(self) -> None:
        directory = self.skill("discreet", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "print('done')\n", encoding="utf-8")
        spec = agent.load("discreet", root=self.root)

        result = dispatch(Firing(
            "discreet", str(self.root), "manual",
            instructions="SECRETINSTRUCTION",
            options=(("token", "SECRETOPTIONVALUE"),),
        ))

        self.assertTrue(result.ok, result)
        log = paths.repo_state_dir(self.root) / "logs" / f"{spec.identifier}.jsonl"
        recorded = log.read_text(encoding="utf-8")
        self.assertNotIn("SECRETINSTRUCTION", recorded)
        self.assertNotIn("SECRETOPTIONVALUE", recorded)

    @unittest.skipIf(shutil.which("pwsh") is None, "PowerShell not installed")
    def test_the_contract_is_language_neutral(self) -> None:
        """A .ps1 processor reads the same run context a .py one does."""
        directory = self.skill("powershell", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.ps1"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.ps1").write_text(
            "Write-Output $env:AGENTS_LIVE_OPTIONS\n", encoding="utf-8")

        result = dispatch(Firing(
            "powershell", str(self.root), "manual",
            options=(("account", "team-inbox"),)))

        self.assertTrue(result.ok, result)
        self.assertEqual({"account": "team-inbox"}, json.loads(result.text))

    def test_run_context_reaches_a_processor_in_the_environment(self) -> None:
        directory = self.skill("context", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        self._echo_environment(directory)
        spec = agent.load("context", root=self.root)

        result = dispatch(Firing(
            "context", str(self.root), "manual",
            changed_files=("docs/a.md",),
            instructions="Focus on authentication",
            options=(("dry-run", True), ("account", "team-inbox")),
        ))

        self.assertTrue(result.ok, result)
        environment = json.loads(result.text)
        self.assertEqual("2", environment["AGENTS_LIVE_CONTRACT"])
        self.assertEqual("pre", environment["AGENTS_LIVE_ROLE"])
        self.assertEqual("manual", environment["AGENTS_LIVE_ORIGIN"])
        self.assertEqual("1", environment["AGENTS_LIVE_ATTEMPT"])
        self.assertEqual(spec.identifier, environment["AGENTS_LIVE_AGENT_ID"])
        self.assertEqual(str(self.root), environment["AGENTS_LIVE_REPO_ROOT"])
        self.assertEqual(
            "Focus on authentication", environment["AGENTS_LIVE_INSTRUCTIONS"])
        self.assertEqual(
            ["docs/a.md"], json.loads(environment["AGENTS_LIVE_CHANGED_FILES"]))
        self.assertEqual(
            {"dry-run": True, "account": "team-inbox"},
            json.loads(environment["AGENTS_LIVE_OPTIONS"]),
        )
        self.assertEqual(result.run_id, environment["AGENTS_LIVE_RUN_ID"])
        # The version 1 name is gone, so a processor cannot read the shared
        # agent log by accident.
        self.assertNotIn("AGENTS_LIVE_LOG_FILE", environment)

    def test_version_1_keeps_its_own_environment(self) -> None:
        directory = self.skill("legacy-context", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ])
        self._echo_environment(directory)
        spec = agent.load("legacy-context", root=self.root)

        result = dispatch(Firing(
            "legacy-context", str(self.root), "manual",
            options=(("dry-run", True),)))

        self.assertTrue(result.ok, result)
        environment = json.loads(result.text)
        self.assertEqual(
            str(paths.repo_state_dir(self.root) / "logs"
                / f"{spec.identifier}.jsonl"),
            environment["AGENTS_LIVE_LOG_FILE"],
        )
        for name in (
            "AGENTS_LIVE_CONTRACT", "AGENTS_LIVE_OPTIONS", "AGENTS_LIVE_LOG",
            "AGENTS_LIVE_CONTROL", "AGENTS_LIVE_OUTPUT",
        ):
            self.assertNotIn(name, environment)

    def test_empty_changed_files_and_options_are_still_present(self) -> None:
        directory = self.skill("defaults", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        self._echo_environment(directory)

        result = dispatch(Firing("defaults", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        environment = json.loads(result.text)
        self.assertEqual([], json.loads(environment["AGENTS_LIVE_CHANGED_FILES"]))
        self.assertEqual({}, json.loads(environment["AGENTS_LIVE_OPTIONS"]))
        self.assertEqual("", environment["AGENTS_LIVE_INSTRUCTIONS"])

    def test_control_file_skips_the_run_and_version_1_uses_stdout(self) -> None:
        directory = self.skill("skipper", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
            'agents-live.post-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "import json, os, pathlib\n"
            "pathlib.Path(os.environ['AGENTS_LIVE_CONTROL']).write_text(\n"
            "    json.dumps({'skip': True, 'message': 'nothing to do'}))\n"
            "print('{\"skip\": false}')\n",
            encoding="utf-8",
        )
        (directory / "scripts" / "process.py").write_text(
            "raise SystemExit('the post-processor must not run')\n",
            encoding="utf-8",
        )

        result = dispatch(Firing("skipper", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        self.assertEqual("skipped", result.status)
        self.assertEqual("nothing to do", result.message)

    def test_only_a_boolean_true_control_value_skips_the_run(self) -> None:
        directory = self.skill("not-skipped", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
            'agents-live.post-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "import json, os, pathlib\n"
            "pathlib.Path(os.environ['AGENTS_LIVE_CONTROL']).write_text(\n"
            "    json.dumps({'skip': 'false'}))\n",
            encoding="utf-8",
        )
        (directory / "scripts" / "process.py").write_text(
            "print('post-processor ran')\n", encoding="utf-8")

        result = dispatch(Firing("not-skipped", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        self.assertEqual("success", result.status)
        self.assertEqual("post-processor ran", result.text)

    def test_version_2_ignores_a_skip_object_on_stdout(self) -> None:
        directory = self.skill("noisy", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
            'agents-live.post-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "print('{\"skip\": true}')\n", encoding="utf-8")
        (directory / "scripts" / "process.py").write_text(
            "import sys\nprint('ran:' + sys.stdin.read().strip())\n",
            encoding="utf-8",
        )

        result = dispatch(Firing("noisy", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        self.assertEqual("success", result.status)
        self.assertEqual('ran:{"skip": true}', result.text)

    def test_version_1_still_skips_on_a_stdout_object(self) -> None:
        directory = self.skill("legacy-skipper", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
            'agents-live.post-processor: "scripts/process.py"',
        ])
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "print('{\"skip\": true}')\n", encoding="utf-8")
        (directory / "scripts" / "process.py").write_text(
            "raise SystemExit('the post-processor must not run')\n",
            encoding="utf-8",
        )

        result = dispatch(Firing("legacy-skipper", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        self.assertEqual("skipped", result.status)

    def test_an_oversized_result_may_travel_by_file(self) -> None:
        directory = self.skill("by-file", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "import os, pathlib\n"
            "pathlib.Path(os.environ['AGENTS_LIVE_OUTPUT']).write_text('the value')\n"
            "print('diagnostics, not the result')\n",
            encoding="utf-8",
        )

        result = dispatch(Firing("by-file", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        self.assertEqual("the value", result.text)
        self.assertIn("diagnostics, not the result", result.message)

    def test_unused_channels_leave_no_files_behind(self) -> None:
        directory = self.skill("tidy", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "print('done')\n", encoding="utf-8")

        result = dispatch(Firing("tidy", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        runs = paths.repo_state_dir(self.root) / "runs" / "tidy"
        self.assertEqual([], sorted(runs.glob("*")) if runs.exists() else [])

    def test_a_log_sink_is_named_per_step_and_left_alone(self) -> None:
        directory = self.skill("logger", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
            'agents-live.post-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.py").write_text(
            "import json, os, pathlib\n"
            "path = pathlib.Path(os.environ['AGENTS_LIVE_LOG'])\n"
            "path.write_text(json.dumps({'message': 'pre wrote this'}) + '\\n')\n"
            "print(str(path))\n",
            encoding="utf-8",
        )
        (directory / "scripts" / "process.py").write_text(
            "import os, sys\n"
            "print(os.environ['AGENTS_LIVE_LOG'])\n",
            encoding="utf-8",
        )

        result = dispatch(Firing("logger", str(self.root), "manual"))

        self.assertTrue(result.ok, result)
        post_sink = Path(result.text)
        self.assertTrue(post_sink.name.startswith("post-"))
        # The post-processor never wrote, so its sink does not exist, and
        # the pre-processor's survives untouched.
        self.assertFalse(post_sink.exists())
        pre_sink = post_sink.parent / "pre-log.jsonl"
        self.assertEqual(
            {"message": "pre wrote this"},
            json.loads(pre_sink.read_text(encoding="utf-8").strip()),
        )


class TestInvocationInput(TempRepository):
    def _instructions_seen_by(self, name: str, argv: list[str]) -> dict:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run.main(["--name", name, *argv])
        self.assertEqual(0, code, buffer.getvalue())
        return json.loads(buffer.getvalue())

    def _echo_agent(self, name: str) -> None:
        directory = self.skill(name, [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ], version="2")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "import json, os\n"
            "print(json.dumps({key: value for key, value in os.environ.items()\n"
            "                  if key.startswith('AGENTS_LIVE_')}))\n",
            encoding="utf-8",
        )

    def test_prompt_file_and_prompt_agree_including_unicode(self) -> None:
        self._echo_agent("from-file")
        text = "Focus on café, naïve, and 日本語 encoding"
        source = self.root / "instructions.md"
        source.write_text(text, encoding="utf-8")

        from_flag = self._instructions_seen_by("from-file", ["-p", text])
        from_file = self._instructions_seen_by(
            "from-file", ["--prompt-file", str(source)])

        self.assertEqual(text, from_flag["AGENTS_LIVE_INSTRUCTIONS"])
        self.assertEqual(text, from_file["AGENTS_LIVE_INSTRUCTIONS"])

    def test_prompt_file_dash_reads_stdin(self) -> None:
        self._echo_agent("from-stdin")
        text = "Instructions from a pipe, with é"

        with mock.patch.object(sys, "stdin", io.StringIO(text)):
            seen = self._instructions_seen_by("from-stdin", ["--prompt-file", "-"])

        self.assertEqual(text, seen["AGENTS_LIVE_INSTRUCTIONS"])

    def test_bad_invocation_input_fails_before_dispatch(self) -> None:
        self._echo_agent("picky")
        source = self.root / "empty.md"
        source.write_text("   \n", encoding="utf-8")
        cases = {
            "mutually exclusive": ["-p", "a", "--prompt-file", str(source)],
            "must not be empty": ["-p", "   "],
            "unreadable": ["--prompt-file", str(self.root / "absent.md")],
            "more than once": ["-o", "a=1", "-o", "a=2"],
        }
        for expected, argv in cases.items():
            with self.subTest(expected):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    code = run.main(["--name", "picky", *argv])
                self.assertEqual(1, code)
                self.assertIn(expected, errors.getvalue())

    def test_schema_1_does_not_apply_the_processor_environment_bound(self) -> None:
        directory = self.skill("legacy-brief", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/process.py"',
        ])
        (directory / "scripts").mkdir()
        (directory / "scripts" / "process.py").write_text(
            "print('ran')\n", encoding="utf-8")

        code = run.main([
            "--name", "legacy-brief",
            "-p", "x" * (port.ENVIRONMENT_VALUE_MAX_BYTES + 1),
            "--quiet",
        ])

        self.assertEqual(0, code)

    def test_schema_2_instruction_overflow_keeps_its_dispatch_category(self) -> None:
        self._echo_agent("picky-brief")
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": "1"}),
            contextlib.redirect_stdout(output),
        ):
            code = run.main([
                "--name", "picky-brief",
                "-p", "x" * (port.ENVIRONMENT_VALUE_MAX_BYTES + 1),
            ])

        self.assertEqual(1, code)
        self.assertEqual(
            "invocation_input_overflow",
            json.loads(output.getvalue())["category"],
        )

    def test_schema_2_option_overflow_keeps_its_dispatch_category(self) -> None:
        self._echo_agent("picky-options")
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": "1"}),
            contextlib.redirect_stdout(output),
        ):
            code = run.main([
                "--name", "picky-options",
                "-o", f"payload={'x' * port.ENVIRONMENT_VALUE_MAX_BYTES}",
            ])

        self.assertEqual(1, code)
        self.assertEqual(
            "invocation_input_overflow",
            json.loads(output.getvalue())["category"],
        )

    def test_instructions_follow_the_definition_body_in_the_prompt(self) -> None:
        self.skill("composed", [
            'agents-live.selector: "fake/echo"',
        ], body="Authoritative body.", version="2")
        spec = agent.load("composed", root=self.root)
        launch = agent.prepare(
            spec,
            agent.Step.AGENT,
            agent.StepContext(
                agent.Request(
                    text="Focus on authentication",
                    changed_files=("docs/a.md",),
                ),
                pre=agent.StepResult(agent.Step.PRE, True, text="gathered"),
            ),
        )

        prompt = launch.argv[-1]
        self.assertLess(prompt.index("Authoritative body."), prompt.index("Files changed:"))
        self.assertLess(
            prompt.index("Files changed:"), prompt.index("Invocation instructions:"))
        self.assertLess(
            prompt.index("Invocation instructions:"),
            prompt.index("Pre-processor context:"),
        )

    def test_option_grammar_splits_on_the_first_equals(self) -> None:
        self.assertEqual(
            (("dry-run", True), ("account", "a=b"), ("empty", "")),
            run._options(["dry-run", "account=a=b", "empty="]),
        )

    def test_a_repeated_option_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            run._options(["account=one", "account=two"])
        self.assertIn("more than once", str(caught.exception))

    def test_installed_triggers_carry_no_invocation_input(self) -> None:
        """An ad hoc option or prompt must not replay on every later firing.

        A trigger's argv is built from the agent name alone, so capture is
        impossible by construction rather than by discipline. Proven here so
        that stops being an accident.
        """
        argv = spawn._run_invocation(self.root, "agent-name")
        self.assertIsNotNone(argv)
        self.assertNotIn("-o", argv)
        self.assertNotIn("--option", argv)
        self.assertNotIn("-p", argv)
        self.assertNotIn("--prompt", argv)
        self.assertIn("--name", argv)


class TestProviderPromptDelivery(TempRepository):
    def test_claude_sends_the_prompt_on_stdin_not_the_command_line(self) -> None:
        self.skill("large-prompt", [
            'agents-live.selector: "claude"',
        ], body="x" * 50000, version="2")
        spec = agent.load("large-prompt", root=self.root)

        launch = agent.prepare(
            spec, agent.Step.AGENT, agent.StepContext(agent.Request()))

        self.assertNotIn("x" * 50000, launch.argv)
        self.assertIn("x" * 50000, launch.input_text)
        self.assertEqual(("claude", "-p", "--output-format", "json"), launch.argv[:4])
        self.assertIsNone(hostruntime.command_line_overflow(launch.argv))


class TestAgentPipeline(TempRepository):
    def test_processor_crash_gets_reactive_dependency_diagnosis(self) -> None:
        directory = self.skill("diagnosed", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
        ])
        scripts = directory / "scripts"
        scripts.mkdir()
        processor = scripts / "prepare.py"
        processor.write_text(
            "# /// script\n"
            '# requires-python = ">=3.12"\n'
            '# dependencies = ["example-package"]\n'
            "# ///\n",
            encoding="utf-8",
        )
        runner = RecordingRunner([
            ChildResult(("uv", "run", str(processor)), 1, "", "ModuleNotFoundError: api"),
        ])
        with mock.patch.object(
            processor_check,
            "diagnose",
            return_value=(
                "fresh dependency resolution succeeded; the failed import "
                "indicates an incompatible dependency API"
            ),
        ) as diagnose:
            result = dispatch(
                Firing("diagnosed", str(self.root), "manual"), runner=runner)

        self.assertFalse(result.ok)
        diagnose.assert_called_once()
        self.assertIn("incompatible dependency API", result.message)

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

    def test_pipeline_stdio_bridge_completes_a_real_mcp_handshake(self) -> None:
        """Exercise the real subprocess, not a mock of it.

        Three separate bridge-crash bugs (agents-live#205, #240, #321) each
        reached a live agent before being caught, and each was diagnosed only
        by manually reconstructing the ``uv run --script`` invocation outside
        the test suite - nothing here actually ran the bridge as a real
        subprocess against a live server. This spawns it exactly as
        ``pipeline/runtime.py`` configures copilot to, over real stdio, and
        confirms a genuine MCP ``initialize`` round-trip succeeds.
        """
        from agents_live.runtime.spawn import find_uv
        from agents_live.pipeline.runtime import pipeline_runtime

        uv = find_uv()
        if uv is None:
            self.skipTest("uv not found on PATH")
        with pipeline_runtime(None) as env:
            config = json.loads(
                Path(env["PIPELINE_MCP_COPILOT_CONFIG"]).read_text(
                    encoding="utf-8"))
            bridge = config["mcpServers"]["pipeline"]
            child_env = {**os.environ, **bridge["env"]}
            proc = subprocess.Popen(
                [uv, *bridge["args"]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=child_env, text=True,
            )
            request = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
            try:
                # communicate() writes stdin, closes it, and reads both
                # streams to EOF; the bridge's read loop exits on stdin
                # EOF, so the process terminates once this returns.
                stdout, stderr = proc.communicate(
                    input=json.dumps(request) + "\n", timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
                self.fail(f"bridge did not respond in time: {stderr}")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        first_line = next(
            (line for line in stdout.splitlines() if line.strip()), "")
        self.assertTrue(first_line, f"no stdout from bridge; stderr: {stderr}")
        response = json.loads(first_line)
        self.assertEqual(1, response.get("id"))
        self.assertIn("result", response, f"bridge error: {response}")
        self.assertEqual(
            "pipeline-stdio-bridge",
            response["result"].get("serverInfo", {}).get("name"))

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

    def test_pipeline_definition_put_fences_seed_the_resource(self) -> None:
        self.skill(
            "seeded-pipeline",
            [
                'agents-live.selector: "fake"',
                'agents-live.mode: "pipeline"',
            ],
            body=(
                "Publish a result.\n\n"
                "```put /output/result/$schema\n"
                '{"type":"object","required":["ok"]}\n'
                "```"
            ),
        )
        runner = RecordingRunner([
            ChildResult(
                ("fake",), 0,
                json.dumps({"text": "done", "structured": {"ok": True}}),
                "",
            ),
        ])

        result = dispatch(
            Firing("seeded-pipeline", str(self.root), "manual"),
            runner=runner,
        )

        self.assertTrue(result.ok, result)
        pipeline_logs = list(
            (paths.repo_state_dir(self.root) / "runs" / "seeded-pipeline").glob(
                "*-pipeline.jsonl"))
        self.assertEqual(1, len(pipeline_logs))
        records = obs.load(pipeline_logs)
        seeded = [
            record for record in records
            if record.get("op") == "seed"
            and record.get("path") == "/output/result/$schema"
        ]
        self.assertEqual(1, len(seeded))

    def test_pipeline_declared_result_is_returned_before_resource_shutdown(self) -> None:
        self.skill(
            "result-pipeline",
            [
                'agents-live.selector: "fake"',
                'agents-live.mode: "pipeline"',
                'agents-live.result-path: "/output/result"',
            ],
            body=(
                "Publish a result.\n\n"
                "```put /output/result\n"
                '{"summary":"done"}\n'
                "```"
            ),
        )
        runner = RecordingRunner([
            ChildResult(
                ("fake",), 0,
                json.dumps({
                    "text": "narration",
                    "structured": {"ignored": True},
                }),
                "",
            ),
        ])

        result = dispatch(
            Firing("result-pipeline", str(self.root), "manual"),
            runner=runner,
        )

        self.assertTrue(result.ok, result)
        self.assertEqual({"summary": "done"}, result.structured)
        self.assertEqual("published", result.result_status)

    def test_pipeline_declared_result_distinguishes_missing_and_null(self) -> None:
        cases = (
            ("missing-result", "Publish nothing.", None, "not_published"),
            (
                "null-result",
                "Publish null.\n\n```put /output/result\nnull\n```",
                None,
                "published",
            ),
        )
        for name, body, expected, status in cases:
            with self.subTest(name=name):
                self.skill(
                    name,
                    [
                        'agents-live.selector: "fake"',
                        'agents-live.mode: "pipeline"',
                        'agents-live.result-path: "/output/result"',
                    ],
                    body=body,
                )
                runner = RecordingRunner([ChildResult(
                    ("fake",), 0,
                    json.dumps({
                        "text": "narration",
                        "structured": {"ignored": True},
                    }),
                    "",
                )])

                result = dispatch(
                    Firing(name, str(self.root), "manual"), runner=runner)

                self.assertTrue(result.ok, result)
                self.assertEqual(expected, result.structured)
                self.assertEqual(status, result.result_status)

    def test_result_path_requires_an_absolute_pipeline_path(self) -> None:
        cases = (
            (
                "non-pipeline-result",
                ['agents-live.selector: "fake"',
                 'agents-live.result-path: "/output/result"'],
                "only available in pipeline mode",
            ),
            (
                "relative-result",
                ['agents-live.selector: "fake"',
                 'agents-live.mode: "pipeline"',
                 'agents-live.result-path: "output/result"'],
                "must start with '/'",
            ),
        )
        for name, metadata, message in cases:
            with self.subTest(name=name):
                self.skill(name, metadata)
                with self.assertRaisesRegex(agent.DefinitionError, message):
                    agent.load(name, root=self.root)

    def test_transcript_records_the_argv_the_child_was_launched_with(self) -> None:
        """The transcript is the only place the launched command survives.

        A provider's constructed argv is otherwise visible only through
        whatever the child chooses to print to its own stderr/stdout, if
        anything. Persisting it lets a provider-boundary failure (e.g. a
        wrapper CLI dropping or mishandling a flag) be diagnosed from the
        run record, not re-derived from provider source and incidental logs.
        """
        self.skill("recorded", ['agents-live.selector: "fake"'])
        argv = ("fake", "--additional-mcp-config", "@/tmp/pipeline-cfg.json")
        runner = RecordingRunner([
            ChildResult(argv, 0, json.dumps({"text": "ok"}), ""),
        ])
        result = dispatch(
            Firing("recorded", str(self.root), "manual"), runner=runner)
        self.assertTrue(result.ok, result)
        self.assertIsNotNone(result.transcript)
        transcript = json.loads(Path(result.transcript).read_text(encoding="utf-8"))
        self.assertEqual(list(argv), transcript["argv"])

    def test_post_processor_preserves_agent_usage_and_transcript(self) -> None:
        self.skill("telemetry", [
            'agents-live.selector: "fake"',
            'agents-live.post-processor: "scripts/post.py"',
        ])
        spec = agent.load("telemetry", root=self.root)
        result = agent.outcome(spec, {
            agent.Step.AGENT: agent.StepResult(
                agent.Step.AGENT,
                True,
                text="provider output",
                usage=(("premium_requests", "1"),),
                transcript="provider-transcript.json",
            ),
            agent.Step.POST: agent.StepResult(
                agent.Step.POST,
                True,
                text="post-processor output",
            ),
        })

        self.assertEqual("post-processor output", result.text)
        self.assertEqual((("premium_requests", "1"),), result.usage)
        self.assertEqual("provider-transcript.json", result.transcript)

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

    def test_copilot_pipeline_exposes_only_the_pipeline_tool(self) -> None:
        self.skill("isolated-copilot", [
            'agents-live.selector: "copilot"',
            'agents-live.mode: "pipeline"',
        ])
        runner = RecordingRunner([
            ChildResult(("copilot",), 0, "done", ""),
        ])

        result = dispatch(
            Firing("isolated-copilot", str(self.root), "manual"),
            runner=runner,
        )

        self.assertTrue(result.ok, result)
        argv = runner.argv[-1]
        self.assertIn("--disable-builtin-mcps", argv)
        self.assertEqual(
            ("--available-tools", "pipeline", "task_complete"),
            argv[argv.index("--available-tools"):argv.index("--available-tools") + 3],
        )
        allowed = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--allow-tool"
        ]
        self.assertEqual(["pipeline", "task_complete"], allowed)

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
        self.assertNotIn("result_status", payload)

    def test_run_json_serializes_declared_pipeline_result_states(self) -> None:
        cases = (
            ({"summary": "done"}, "published"),
            (None, "not_published"),
            (None, "published"),
        )
        for structured, result_status in cases:
            with self.subTest(
                    structured=structured, result_status=result_status):
                outcome = agent.Outcome(
                    True,
                    "success",
                    text="done",
                    structured=structured,
                    run_id="run-123",
                    result_status=result_status,
                )
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": "1"}),
                    mock.patch.object(
                        run.paths, "resolve_root", return_value=self.root),
                    mock.patch.object(run, "dispatch", return_value=outcome),
                    contextlib.redirect_stdout(stdout),
                ):
                    code = run.main(["--name", "pipeline"])
                payload = json.loads(stdout.getvalue())
                self.assertEqual(0, code)
                self.assertEqual(structured, payload["structured"])
                self.assertEqual(result_status, payload["result_status"])

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


class TestDashboardRepositorySurface(TempRepository):
    def _dashboard_module(self):
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.app.post.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            module = importlib.import_module("agents_live.cli.scripts.dashboard")
            return importlib.reload(module)

    def test_dashboard_repository_settings_mutate_the_registry(self) -> None:
        dashboard = self._dashboard_module()
        other = (self.root / "other").resolve()
        (other / "Agents").mkdir(parents=True)

        added = dashboard._repository_mutation(
            {"action": "add", "path": str(other)})
        self.assertTrue(added["ok"], added)
        self.assertEqual({"other": str(other)}, repos.load()["repos"])

        selected = dashboard._repository_mutation(
            {"action": "set-default", "repo": "other"})
        self.assertTrue(selected["ok"], selected)
        self.assertEqual("other", repos.load()["default_repo"])

        cleared = dashboard._repository_mutation({"action": "clear-default"})
        self.assertTrue(cleared["ok"], cleared)
        self.assertIsNone(repos.load()["default_repo"])

        removed = dashboard._repository_mutation(
            {"action": "remove", "repo": "other"})
        self.assertTrue(removed["ok"], removed)
        self.assertEqual({}, repos.load()["repos"])
        self.assertTrue(other.is_dir(), "unregistering must not delete files")

    def test_dashboard_all_repo_groups_use_the_shared_informational_rows(self) -> None:
        dashboard = self._dashboard_module()
        other = (self.root / "other").resolve()
        (other / "Agents").mkdir(parents=True)
        self.skill("zeta", ['agents-live.selector: "fake/echo"'])
        self.skill("alpha", [
            'agents-live.selector: "none"',
            'agents-live.pre-processor: "scripts/prepare.py"',
        ])
        (self.root / "Agents" / "alpha" / "scripts").mkdir()
        (self.root / "Agents" / "alpha" / "scripts" / "prepare.py").write_text(
            "print('ok')\n", encoding="utf-8")
        (other / "Agents" / "beta").mkdir(parents=True)
        (other / "Agents" / "beta" / "SKILL.md").write_text(
            "---\nname: beta\ndescription: Other repo.\nmetadata:\n"
            '  agents-live.schema-version: "1"\n'
            '  agents-live.selector: "fake/echo"\n'
            "---\nbody\n",
            encoding="utf-8",
        )
        repos._add(str(self.root))
        repos._add(str(other))
        dashboard.STATE["all_repos"].update({
            "repo": "All", "grouped": True,
            "sort_by": "state", "descending": False,
        })

        groups = dashboard.all_repo_groups()
        self.assertEqual(["other", self.root.name], [group["name"] for group in groups])
        local_group = next(group for group in groups if group["name"] == self.root.name)
        self.assertEqual(["alpha", "zeta"], [row["name"] for row in local_group["rows"]])
        self.assertEqual("handler", local_group["rows"][0]["agent"])
        self.assertEqual("fake", local_group["rows"][1]["agent"])
        self.assertEqual(
            [column["name"] for column in dashboard._AGENT_COLUMNS
             if column["name"] != "actions"],
            [column["name"] for column in dashboard._AGGREGATE_COLUMNS],
        )
        self.assertEqual(groups, dashboard.all_repo_groups())
        dashboard.STATE["all_repos"]["descending"] = True
        local_group = next(
            group for group in dashboard.all_repo_groups()
            if group["name"] == self.root.name)
        self.assertEqual(["alpha", "zeta"], [
            row["name"] for row in local_group["rows"]])

    def test_dashboard_all_repo_groups_keep_unavailable_paths_visible(self) -> None:
        dashboard = self._dashboard_module()
        path = repos.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        missing = self.root / "missing"
        path.write_text(
            f"[repos]\nmissing = {json.dumps(str(missing))}\n",
            encoding="utf-8",
        )

        groups = dashboard.all_repo_groups()
        self.assertEqual(1, len(groups))
        self.assertEqual("missing", groups[0]["name"])
        self.assertFalse(groups[0]["available"])
        self.assertIn("not an existing directory", groups[0]["error"])
        self.assertEqual([], groups[0]["rows"])

    def test_dashboard_ungrouped_rows_have_repository_qualified_keys(self) -> None:
        dashboard = self._dashboard_module()
        groups = [
            {"name": "first", "rows": [{"identifier": "daily-123"}]},
            {"name": "second", "rows": [{"identifier": "daily-123"}]},
        ]

        rows = dashboard._ungrouped_agent_rows(groups)

        self.assertEqual(["first", "second"], [
            row["repository"] for row in rows])
        self.assertEqual(2, len({
            row["repository_identifier"] for row in rows}))

    def test_dashboard_host_service_shows_failed_idle_and_running_states(self) -> None:
        dashboard = self._dashboard_module()
        host = MemoryHost()
        subscription = lifecycle.maintenance_subscription()
        host.trigger_store.install(host.render(subscription))
        previous = runtime.current()
        runtime.configure(host)
        try:
            paths.health_beacon_path().parent.mkdir(parents=True, exist_ok=True)
            paths.health_beacon_path().write_text(json.dumps({
                "status": "degraded",
                "ts": datetime.now(timezone.utc).isoformat(),
                "smoketest": {
                    "status": "fail",
                    "reason": "timeout after 360s",
                    "duration_s": 360,
                },
            }), encoding="utf-8")
            obs.admin.record(
                "maintenance", status="error", duration_s=361,
                message="maintenance completed: degraded")

            service = dashboard.host_service_status()
            self.assertTrue(service["installed"])
            self.assertEqual("failed", service["state"])
            self.assertEqual("Failed, idle", service["label"])
            self.assertEqual("timeout after 360s", service["smoketest"]["reason"])
            self.assertEqual(360, service["duration_s"])
            self.assertTrue(service["can_run"])

            dashboard.STATE["health_check_running"] = True
            running = dashboard.host_service_status()
            self.assertEqual("running", running["state"])
            self.assertFalse(running["can_run"])
        finally:
            dashboard.STATE["health_check_running"] = False
            runtime.configure(previous)



class TestArchitectureFitness(unittest.TestCase):
    def test_long_lived_process_creation_stays_with_host_owners(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        allowed = {
            "runtime/hosts/filesystem.py",
            "runtime/hosts/system.py",
        }
        found: set[str] = set()
        for path in package.rglob("*.py"):
            relative = path.relative_to(package).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr == "Popen"
                ):
                    found.add(relative)

        self.assertEqual(allowed, found)

    def test_doctor_repair_can_be_previewed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "Agents").mkdir()
            (root / ".agents-live.toml").write_text("", encoding="utf-8")
            environment = {
                **os.environ,
                "AGENTS_LIVE_REPO": str(root),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            completed = subprocess.run(
                [
                    sys.executable, "-m", "agents_live.cli", "--repo",
                    str(root), "doctor", "--repair", "--dry-run",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        output = completed.stdout + completed.stderr
        self.assertNotEqual(2, completed.returncode, output)
        self.assertIn("repair:", output)
        self.assertNotIn("mutually exclusive", output)

    def test_current_code_cannot_add_an_untracked_legacy_dependency(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        allowed = {
            ("cli/commands/uninstall.py", "agents_live.legacy.migrate"),
            ("cli/commands/upgrade.py", "agents_live.legacy.migrate"),
            ("runtime/hosts/crontab.py", "agents_live.legacy.triggers"),
            ("runtime/hosts/posix.py", "agents_live.legacy.artifacts"),
            ("runtime/hosts/task_scheduler.py", "agents_live.legacy.triggers"),
            ("runtime/hosts/windows.py", "agents_live.legacy.artifacts"),
        }
        found: set[tuple[str, str]] = set()
        for path in package.rglob("*.py"):
            relative = path.relative_to(package).as_posix()
            if relative.startswith("legacy/"):
                continue
            module = "agents_live." + ".".join(
                path.relative_to(package).with_suffix("").parts)
            package_name = module.rpartition(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    imported = (
                        importlib.util.resolve_name(
                            "." * node.level + (node.module or ""),
                            package_name,
                        )
                        if node.level else (node.module or "")
                    )
                    if imported == "agents_live.legacy":
                        found.update(
                            (relative, f"{imported}.{alias.name}")
                            for alias in node.names
                        )
                    elif imported.startswith("agents_live.legacy."):
                        found.update(
                            (relative, f"{imported}.{alias.name}")
                            for alias in node.names
                        )
                elif isinstance(node, ast.Import):
                    found.update(
                        (relative, alias.name)
                        for alias in node.names
                        if alias.name.startswith("agents_live.legacy")
                    )
        self.assertEqual(allowed, found)

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

    def test_dashboard_next_port_announces_and_serves_first_available(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            dashboard = importlib.import_module(
                "agents_live.cli.scripts.dashboard")
        stdout = io.StringIO()
        with (
            mock.patch.object(dashboard, "__name__", "__main__"),
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(dashboard.app, "is_started", False),
            mock.patch.object(
                dashboard, "port_conflict",
                side_effect=["occupied", "occupied", None]),
            mock.patch.object(dashboard.dashboards, "record") as record,
            mock.patch.object(dashboard.atexit, "register"),
            mock.patch.object(dashboard, "build_page"),
            mock.patch.object(dashboard.ui, "run") as run,
            mock.patch.object(
                sys, "argv", ["dashboard.py", "--port", "next"]),
            contextlib.redirect_stdout(stdout),
        ):
            dashboard.main()

        self.assertIn("Dashboard URL: http://127.0.0.1:8233", stdout.getvalue())
        record.assert_called_once_with(8233, os.getpid(), dashboard.REPO_ROOT)
        self.assertEqual(8233, run.call_args.kwargs["port"])

    def test_dashboard_next_port_exhaustion_fails_before_recording(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            dashboard = importlib.import_module(
                "agents_live.cli.scripts.dashboard")
        with (
            mock.patch.object(dashboard, "__name__", "__main__"),
            mock.patch.object(dashboard.app, "is_started", False),
            mock.patch.object(dashboard, "DEFAULT_PORT", 65534),
            mock.patch.object(
                dashboard, "port_conflict", return_value="occupied"),
            mock.patch.object(dashboard.preflight, "emit_failure") as emit,
            mock.patch.object(dashboard.dashboards, "record") as record,
            mock.patch.object(dashboard.ui, "run") as run,
            mock.patch.object(
                sys, "argv", ["dashboard.py", "--port", "next"]),
            self.assertRaises(SystemExit) as raised,
        ):
            dashboard.main()

        self.assertEqual(1, raised.exception.code)
        emit.assert_called_once_with(
            "dashboard",
            "no available port from 65534 through 65535",
            code="port_unavailable",
        )
        record.assert_not_called()
        run.assert_not_called()

    def test_dashboard_next_port_is_reused_by_reload_child(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            dashboard = importlib.import_module(
                "agents_live.cli.scripts.dashboard")
        with (
            mock.patch.object(dashboard, "__name__", "__mp_main__"),
            mock.patch.dict(
                os.environ, {dashboard.SELECTED_PORT_ENV: "8233"}),
            mock.patch.object(dashboard.dashboards, "record") as record,
            mock.patch.object(dashboard, "build_page"),
            mock.patch.object(dashboard.ui, "run") as run,
            mock.patch.object(
                sys, "argv", ["dashboard.py", "--port", "next", "--dev"]),
        ):
            dashboard.main()

        record.assert_not_called()
        self.assertEqual(8233, run.call_args.kwargs["port"])

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
            receipt_environment=tool_environment, correlation_id=None)

    def test_upgrade_records_noop_plugin_convergence_under_correlation(self) -> None:
        with mock.patch.object(plugins.adminlog, "record") as record:
            self.assertFalse(plugins.converge(
                [], trigger="upgrade", correlation_id="upgrade-operation"))
        record.assert_called_once_with(
            "plugin-converge",
            status="ok",
            correlation_id="upgrade-operation",
            trigger="upgrade",
            changed=False,
            pending=[],
            message="plugins already converged",
        )

    def test_plugin_convergence_refuses_the_active_tool_environment(self) -> None:
        declaration = plugins.Plugin(
            name="example", path=Path("example.whl"),
            sha256=None, version="1.0")
        environment = Path("C:/uv/tools/agents-live")
        with (
            mock.patch.object(plugins, "union", return_value={
                "example": declaration}),
            mock.patch.object(
                plugins, "_installed_state", return_value=(False, "missing")),
            mock.patch.object(plugins, "validation_errors", return_value=[]),
            mock.patch.object(
                plugins.hostruntime, "locks_running_image", return_value=True),
            mock.patch.object(
                plugins, "tool_environment", return_value=environment),
            mock.patch.object(
                plugins.sys, "executable",
                str(environment / "Scripts" / "python.exe")),
            mock.patch.object(plugins, "_receipt_requirements") as receipt,
            mock.patch.object(plugins.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(
                    plugins.PluginError, "active tool environment"):
                plugins.converge([Path("C:/repo")], trigger="init")
        receipt.assert_not_called()
        run.assert_not_called()

    def test_plugin_convergence_refuses_an_environment_held_by_another_process(
            self) -> None:
        declaration = plugins.Plugin(
            name="example", path=Path("example.whl"),
            sha256=None, version="1.0")
        environment = Path("C:/uv/tools/agents-live")
        held_executable = environment / "Scripts" / "python.exe"
        with (
            mock.patch.object(plugins, "union", return_value={
                "example": declaration}),
            mock.patch.object(
                plugins, "_installed_state", return_value=(False, "missing")),
            mock.patch.object(plugins, "validation_errors", return_value=[]),
            mock.patch.object(
                plugins.hostruntime, "locks_running_image", return_value=True),
            mock.patch.object(
                plugins, "tool_environment", return_value=environment),
            mock.patch.object(
                plugins.sys, "executable", "C:/uv/cache/python.exe"),
            mock.patch.object(
                plugins.hostruntime, "process_command_lines",
                return_value=[(42, "held command")]),
            mock.patch.object(
                plugins.hostruntime, "split_command_line",
                return_value=[str(held_executable), "internal", "watch-loop"]),
            mock.patch.object(plugins, "_receipt_requirements") as receipt,
            mock.patch.object(plugins.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(
                    plugins.PluginError, "active tool environment"):
                plugins.converge([Path("C:/repo")], trigger="init")
        receipt.assert_not_called()
        run.assert_not_called()

    def test_plugin_convergence_refuses_unverifiable_windows_idleness(self) -> None:
        declaration = plugins.Plugin(
            name="example", path=Path("example.whl"),
            sha256=None, version="1.0")
        environment = Path("C:/uv/tools/agents-live")
        for target, processes, message in (
            (None, [(1, "external helper")], "cannot identify"),
            (environment, [], "cannot verify"),
        ):
            with self.subTest(message=message):
                with (
                    mock.patch.object(plugins, "union", return_value={
                        "example": declaration}),
                    mock.patch.object(
                        plugins, "_installed_state",
                        return_value=(False, "missing")),
                    mock.patch.object(
                        plugins, "validation_errors", return_value=[]),
                    mock.patch.object(
                        plugins.hostruntime, "locks_running_image",
                        return_value=True),
                    mock.patch.object(
                        plugins, "tool_environment", return_value=target),
                    mock.patch.object(
                        plugins.sys, "executable", "C:/uv/cache/python.exe"),
                    mock.patch.object(
                        plugins.hostruntime, "process_command_lines",
                        return_value=processes),
                    mock.patch.object(plugins, "_receipt_requirements") as receipt,
                    mock.patch.object(plugins.subprocess, "run") as run,
                ):
                    with self.assertRaisesRegex(plugins.PluginError, message):
                        plugins.converge([Path("C:/repo")], trigger="init")
                receipt.assert_not_called()
                run.assert_not_called()

    def test_upgrade_correlates_pending_plugin_convergence(self) -> None:
        declaration = plugins.Plugin(
            name="example", path=Path("example.whl"),
            sha256=None, version="1.0")
        primary = plugins.ReceiptRequirement("agents-live==6.3.4")
        operation = mock.MagicMock()
        operation.__enter__.return_value = {}
        with (
            mock.patch.object(plugins, "union", return_value={
                "example": declaration}),
            mock.patch.object(
                plugins, "_installed_state", return_value=(False, "missing")),
            mock.patch.object(plugins, "validation_errors", return_value=[]),
            mock.patch.object(
                plugins.hostruntime, "locks_running_image", return_value=False),
            mock.patch.object(
                plugins, "_receipt_requirements", return_value=(primary, {})),
            mock.patch.object(plugins, "find_uv", return_value="uv"),
            mock.patch.object(
                plugins.subprocess, "run", return_value=mock.Mock(returncode=0)),
            mock.patch.object(plugins, "launcher_stamp", return_value=None),
            mock.patch.object(plugins, "installed_version", return_value="6.3.5"),
            mock.patch.object(
                plugins.adminlog, "operation", return_value=operation,
            ) as operation_call,
        ):
            self.assertTrue(plugins.converge(
                [Path("/repo")], trigger="upgrade",
                correlation_id="upgrade-operation"))
        self.assertEqual(
            "upgrade-operation",
            operation_call.call_args.kwargs["correlation_id"],
        )
        self.assertTrue(operation.__enter__.return_value["changed"])

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
                mock.patch.dict(os.environ, {
                    paths.ENV_VAR: "",
                    "UV_DEFAULT_INDEX": "https://pypi.org/simple",
                }),
                mock.patch.object(sys, "argv", ["agents-live upgrade"]),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(1, upgrade.main())
        handoff.assert_not_called()
        self.assertIn("retired 5.x fields", stderr.getvalue())

    def test_upgrade_refuses_a_stale_configured_index_before_mutation(self) -> None:
        refused = package_index.IndexCheck(
            False,
            "6.3.2",
            "5.5.2",
            "configured index resolved 5.5.2; agents-live>=6.3.2 is required",
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(upgrade.package_index, "configured", return_value=True),
            mock.patch.object(upgrade.package_index, "check", return_value=refused),
            mock.patch.object(upgrade.update_check, "cached_result", return_value=None),
            mock.patch.object(upgrade, "_targets") as targets,
            mock.patch.object(sys, "argv", ["agents-live upgrade"]),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(1, upgrade.main())

        targets.assert_not_called()
        self.assertIn("agents-live>=6.3.2 is required", stderr.getvalue())

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
                mock.patch.dict(os.environ, {
                    paths.ENV_VAR: "",
                    "UV_DEFAULT_INDEX": "https://pypi.org/simple",
                }),
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

    def test_pipeline_package_does_not_shadow_the_mcp_dependency(self) -> None:
        """A same-named sibling module breaks a script run standalone.

        ``stdio_bridge.py`` is executed via ``uv run --script``, which puts
        the script's own directory first on ``sys.path``. A sibling module
        in that directory sharing a name with a dependency the script
        imports would shadow the real package there - this happened once
        (agents-live#317) when the pipeline server module was named
        ``mcp.py``, so ``import mcp`` inside the bridge resolved to that
        plain module instead of the third-party SDK and failed with
        ``ModuleNotFoundError: No module named 'mcp.client'; 'mcp' is not
        a package``. Reproduce the exact shadowing mechanism rather than
        asserting one filename, so a differently named future collision
        is still caught.
        """
        pipeline_dir = (
            Path(__file__).parents[1] / "src" / "agents_live" / "pipeline")
        self.assertTrue(pipeline_dir.is_dir())
        completed = subprocess.run(
            [
                sys.executable, "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import mcp; import mcp.client.session",
                str(pipeline_dir),
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(
            0, completed.returncode,
            f"a pipeline/ sibling module shadows the mcp dependency: "
            f"{completed.stderr}")

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
                        self.assertIn(
                            "agents-live ownership enable",
                            stopped_snapshot["agents"][0]["claim_tip"])
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

    def test_dashboard_abbreviates_windows_timezones_and_timestamps_refresh(self) -> None:
        class MountainTime(tzinfo):
            def utcoffset(self, moment):
                return self.dst(moment) + timedelta(hours=-7)

            def dst(self, moment):
                return timedelta(hours=1 if 3 <= moment.month <= 10 else 0)

            def tzname(self, moment):
                return (
                    "Mountain Daylight Time"
                    if self.dst(moment) else "Mountain Standard Time"
                )

        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard

        summer = datetime(2026, 7, 1, 12, 0, tzinfo=MountainTime())
        winter = datetime(2026, 1, 1, 12, 0, tzinfo=MountainTime())
        self.assertEqual("12:00:00 MDT", dashboard._local_time(summer))
        self.assertEqual("12:00:00 MST", dashboard._local_time(winter))

        output = mock.Mock()
        with (
            mock.patch.object(dashboard, "output_log", output, create=True),
            mock.patch.object(dashboard, "_refresh_summary", return_value="summary"),
            mock.patch.object(dashboard.agent_grid, "refresh", create=True),
            mock.patch.object(dashboard.header_actions, "refresh", create=True),
            mock.patch.object(dashboard.host_service_panel, "refresh", create=True),
            mock.patch.object(
                dashboard.hostruntime, "enumeration_pass",
                return_value=contextlib.nullcontext()),
        ):
            dashboard._refresh_views()
        rendered = output.push.call_args.args[0]
        self.assertRegex(rendered, r"^\[\d{2}:\d{2}:\d{2} [A-Z]+\] summary$")

    def test_ports_do_not_import_each_other_and_cli_stays_on_ports(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        runtime_imports = _imports(package / "runtime")
        agent_imports = _imports(package / "agent")
        self.assertFalse(any(name.startswith("agents_live.agent") for name in runtime_imports))
        self.assertFalse(any(name.startswith("agents_live.runtime") for name in agent_imports))
        allowed = {
            "agents_live.agent", "agents_live.dispatch", "agents_live.obs",
            "agents_live.runtime", "agents_live.state", "agents_live.cli",
            "agents_live.legacy", "agents_live.deploy",
        }
        for imported in _imports(package / "cli"):
            if imported.startswith("agents_live."):
                self.assertTrue(
                    any(imported == item or imported.startswith(f"{item}.") for item in allowed),
                    imported,
                )

    def test_deployment_primitives_stay_below_the_commands_that_use_them(
            self) -> None:
        """`deploy/` is planning and paths, not composition (#369).

        Deployment is the one place a CLI command still owns real
        composition, and the point of extracting these primitives is
        that they can be tested without a host. A dependency on the
        command layer would put the composition back, and a subprocess
        in the detection path would put a package manager back into a
        report that has to answer while a runtime is being replaced.
        """
        package = Path(__file__).parents[1] / "src" / "agents_live"
        imported = _imports(package / "deploy")
        for forbidden in ("agents_live.cli", "agents_live.agent",
                          "agents_live.dispatch", "agents_live.pipeline",
                          "subprocess"):
            self.assertFalse(
                any(name == forbidden or name.startswith(f"{forbidden}.")
                    for name in imported),
                f"deploy/ imports {forbidden}")

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
