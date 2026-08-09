from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_live import (
    agent, obs, paths, runtime, state,
)
from agents_live.cli import lifecycle
from agents_live.cli.commands import uninstall
from agents_live.cli.spec import COMMANDS
from agents_live.legacy import health_check, triggers
from agents_live.state import ownership
from agents_live.dispatch import Firing, _RunLock, dispatch
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
from agents_live.runtime.hosts import task_scheduler


class TempRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "Agents").mkdir()
        self.old_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")

    def tearDown(self) -> None:
        if self.old_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old_state
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


class TestRuntimeCore(unittest.TestCase):
    def test_framework_smoketest_has_no_external_provider_gate(self) -> None:
        self.assertEqual("fake", health_check._resolve_smoketest_runtime())

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
        self.assertNotIn("--runtime-role", rendered.watcher_argv)

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


class RecordingRunner:
    def __init__(self, outputs: list[ChildResult]) -> None:
        self.outputs = outputs
        self.argv: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []

    def run_child(self, argv, **kwargs):
        self.argv.append(tuple(argv))
        self.inputs.append(kwargs.get("input_text"))
        return self.outputs.pop(0)


class TestAgentPipeline(TempRepository):
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
            completed = [
                record for record in records
                if record["agent_name"] == name and record["phase"] == "done"
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


class TestArchitectureFitness(unittest.TestCase):
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
        for directory in (package / "runtime", package / "agent", package / "cli"):
            for path in directory.rglob("*.py"):
                if (package / "runtime" / "hosts") in path.parents:
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
