"""Behavioral coverage for decisions the seam suite exercises only in part.

The 6.0 refactor rewrote the modules under test and rewrote the suite in
the same change, so what survived proves the new code agrees with the new
tests. These are the decisions whose absence a released defect then found:
who may run an agent here, what a trigger store does to a table it shares,
which declared plugin is safe to install, and what happens when a selector
names nothing. Each class notes the behavior it holds, not the function it
calls, so a later refactor can move the code without deleting the check.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import importlib
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agents_live import agent, deploy, obs, paths, plugins, runtime, state
from agents_live.agent import port, providers
from agents_live.cli import lifecycle, resolve, upgrade_handoff
from agents_live.cli.commands import (
    doctor,
    install_generation,
    install_release,
    internal,
    ownership as ownership_command,
    start,
    stop,
    uninstall,
    upgrade,
)
from agents_live.obs import qlog
from agents_live.obs.events import append as append_event
from agents_live.agent.values import McpServer, RawOutput, Request, ResolvedSpec
from agents_live.dispatch import Firing, dispatch
from agents_live.runtime import ChildResult, ProcessRef, Subscription
from agents_live.runtime import artifacts
from agents_live.runtime.hosts import crontab as crontasks
from agents_live.runtime.hosts import system as hostruntime
from agents_live.runtime.hosts import windows as windowshost
from agents_live.runtime.hosts.memory import MemoryHost
from agents_live.runtime.hosts.posix import PosixHost, PosixTriggerStore
from agents_live.runtime.hosts.windows import WindowsProcesses
from agents_live.state import ownership
from agents_live.state import registry as repos

REPOSITORY = Path(__file__).resolve().parents[1]

_ISOLATED_HOMES = {
    "XDG_STATE_HOME": "state",
    "XDG_DATA_HOME": "data",
    "XDG_CONFIG_HOME": "config",
}


# The installation root is host-global and is read before anything a test
# arranges: on a developer machine that already runs a self-managed
# installation, the ownership refusal answers first and tests that never
# mention deployment fail. Isolate it for the whole module; classes that
# arrange their own root still override this one.
_INSTALL_ROOT: tempfile.TemporaryDirectory | None = None
_PREVIOUS_INSTALL_ROOT: str | None = None


def setUpModule() -> None:
    global _INSTALL_ROOT, _PREVIOUS_INSTALL_ROOT
    _PREVIOUS_INSTALL_ROOT = os.environ.get(deploy.layout.ENV_INSTALL_ROOT)
    _INSTALL_ROOT = tempfile.TemporaryDirectory()
    os.environ[deploy.layout.ENV_INSTALL_ROOT] = str(
        Path(_INSTALL_ROOT.name) / "install")


def tearDownModule() -> None:
    if _PREVIOUS_INSTALL_ROOT is None:
        os.environ.pop(deploy.layout.ENV_INSTALL_ROOT, None)
    else:
        os.environ[deploy.layout.ENV_INSTALL_ROOT] = _PREVIOUS_INSTALL_ROOT
    if _INSTALL_ROOT is not None:
        _INSTALL_ROOT.cleanup()


PROVIDER_SPEND: dict[str, str | None] = {
    "claude": '{"result": "done", "total_cost_usd": 0.42}',
    "copilot": '{"type": "session.usage_checkpoint", '
               '"data": {"totalNanoAiu": 42000000000}}',
    "fake": None,
}
"""Vendor output that reports spend, or ``None`` for a provider with none."""


class TempRepository(unittest.TestCase):
    """The seam suite's fixture: a temp project with the user homes moved."""

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

    def skill(self, name: str, metadata: list[str], *,
              root: Path | None = None) -> Path:
        directory = (root or self.root) / "Agents" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("\n".join([
            "---",
            f"name: {name}",
            "description: A portable test definition.",
            "metadata:",
            '  agents-live.schema-version: "1"',
            *[f"  {line}" for line in metadata],
            "---",
            "Do the work.",
            "",
        ]), encoding="utf-8")
        return directory


class TestOwnershipEnforcement(TempRepository):
    """Who may run an agent on this host.

    The seam suite reaches these paths with ``ownership.owns`` replaced by
    a stub, which proves the caller branches but never that the matcher
    decides correctly. A matcher that answered True for a value it could
    not parse would run another host's agents here and pass that suite.
    """

    def _registry_project(self, temporary: str, name: str) -> tuple[Path, str]:
        root = Path(temporary).resolve()
        (root / ".agents-live.toml").write_text(
            'ownership = "registry"\n', encoding="utf-8")
        self.skill(name, [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 9 * * *"',
        ], root=root)
        spec = agent.load(name, root=root)
        state.replace(root, {spec.identifier})
        return root, spec.identifier

    def _collect(self, root: Path, owners: dict[str, str]):
        host = MemoryHost()
        previous = runtime.current()
        runtime.configure(host)
        try:
            with (
                mock.patch.object(lifecycle.repos, "load", return_value={
                    "repos": {"registry": str(root)},
                    "default_repo": "registry",
                }),
                mock.patch.object(
                    ownership, "load_owners", return_value=owners),
            ):
                return lifecycle.collect(persist=False)
        finally:
            runtime.configure(previous)

    def test_this_runtime_keeps_the_agents_it_owns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, identifier = self._registry_project(temporary, "mine")
            collected = self._collect(
                root, {"mine": ownership.current_owner_id()})
        self.assertIn(
            f"agent:{identifier}",
            {item.target for item in collected.subscriptions})

    def test_an_unclaimed_agent_keeps_running_here(self) -> None:
        """Absent from the registry is unclaimed, not owned elsewhere.

        Conflating the two stops every agent in a project that has not
        assigned any of them.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root, identifier = self._registry_project(temporary, "unclaimed")
            collected = self._collect(root, {})
        self.assertIn(
            f"agent:{identifier}",
            {item.target for item in collected.subscriptions})

    def test_another_runtimes_agent_is_not_installed_here(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, identifier = self._registry_project(temporary, "theirs")
            other = f"otherhost/wsl/{'a' * 32}"
            self.assertNotEqual(other, ownership.current_owner_id())
            collected = self._collect(root, {"theirs": other})
        self.assertNotIn(
            f"agent:{identifier}",
            {item.target for item in collected.subscriptions})

    def test_an_unmatchable_owner_value_is_treated_as_someone_elses(self) -> None:
        """No uuid means not ours, whatever produced the value.

        A truncated write, a hand edit, and a badly merged entry all read
        the same way, so there is no repair path to maintain: an operator
        resolves them by claiming the agent.
        """
        for value in (
            "just-a-hostname",
            "host/runtime",
            "host/runtime/",
            "host/runtime/not-a-uuid",
            "host/runtime/" + "a" * 31,
            "",
        ):
            with self.subTest(value=value):
                self.assertFalse(ownership.owns(value))
        self.assertTrue(ownership.owns(ownership.WILDCARD))
        self.assertTrue(ownership.owns(ownership.current_owner_id()))

    def test_a_renamed_host_keeps_its_agents(self) -> None:
        """Matching reads the uuid part only.

        Two WSL distros on one machine share a hostname by default, so a
        hostname match would make them one owner; a rename would make one
        owner into two.
        """
        identity = ownership.owner_uuid(ownership.current_owner_id())
        self.assertTrue(identity)
        self.assertTrue(ownership.owns(f"renamed/otherdistro/{identity}"))
        self.assertEqual(
            "renamed/otherdistro",
            ownership.display_owner(f"renamed/otherdistro/{identity}"))

    def test_a_malformed_declaration_abstains_rather_than_going_local(self) -> None:
        """A corrupted config must never silently flip to run-everything-here."""
        (self.root / ".agents-live.toml").write_text(
            'ownership = "loclal"\n', encoding="utf-8")
        with self.assertRaises(ownership.OwnershipUnavailableError) as caught:
            ownership.mode(self.root)
        self.assertIn("registry", str(caught.exception))

    def test_an_absent_declaration_is_local_without_reading_a_registry(self) -> None:
        self.assertEqual("local", ownership.mode(self.root))
        self.assertTrue(ownership.local_only(self.root))
        with mock.patch.object(ownership, "_require_backend") as backend:
            self.assertEqual({}, ownership.load_owners(root=self.root))
        backend.assert_not_called()


class TestCrontabTriggerStore(TempRepository):
    """What the POSIX store does to a table it shares with other writers.

    The store replaces the whole crontab on every mutation, so each of
    these is the difference between converging one entry and erasing the
    user's own.
    """

    def setUp(self) -> None:
        super().setUp()
        self.table: list[str] = [
            "0 3 * * * /home/someone/backup.sh",
            "@daily /usr/bin/unrelated --flag",
        ]
        self.store = PosixTriggerStore()
        patches = (
            mock.patch.object(crontasks, "lines", side_effect=self._lines),
            mock.patch.object(crontasks, "write", side_effect=self._write),
            mock.patch.object(crontasks, "lock", contextlib.nullcontext),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _lines(self) -> list[str] | None:
        return None if self.table is None else list(self.table)

    def _write(self, new_lines) -> None:
        self.table = list(new_lines)

    def _rendered(self, target: str, trigger: str = "0 8 * * *"):
        subscription = Subscription.create(
            scope=f"repo:{self.root}", target=f"agent:{target}",
            kind="schedule", trigger=trigger)
        return PosixHost().render(subscription)

    def test_repeated_installs_converge_on_one_entry(self) -> None:
        rendered = self._rendered("sample")
        for _ in range(3):
            self.store.install(rendered)
        installed = self.store.list()
        self.assertEqual(1, len(installed))
        self.assertEqual(rendered.key, installed[0].key)
        self.assertEqual(3, len(self.table))

    def test_a_changed_trigger_is_a_new_key_the_store_keeps_until_converged(self) -> None:
        """The store keys on the subscription, and the key covers the
        trigger. Retiring the superseded entry is convergence's job, so a
        store that silently replaced by target would hide the drift the
        diff exists to report."""
        first = self._rendered("sample", "0 8 * * *")
        second = self._rendered("sample", "30 9 * * *")
        self.assertNotEqual(first.key, second.key)
        self.store.install(first)
        self.store.install(second)
        self.assertEqual(
            {first.key, second.key}, {item.key for item in self.store.list()})
        self.store.remove(first.key)
        installed = self.store.list()
        self.assertEqual(1, len(installed))
        self.assertIn("30 9 * * *", installed[0].rendered)

    def test_removal_and_clear_take_only_this_tools_entries(self) -> None:
        foreign = list(self.table)
        self.store.install(self._rendered("first"))
        second = self._rendered("second")
        self.store.install(second)
        self.store.remove(second.key)
        self.assertEqual(["agent:first"], [
            item.target for item in self.store.list()])
        self.assertEqual(1, self.store.clear())
        self.assertEqual(foreign, self.table)

    def test_an_unreadable_crontab_never_rewrites_the_table(self) -> None:
        """A read that failed did not see an empty table.

        ``write`` replaces everything, so treating unreadable as empty
        erases every entry the failed read did not return.
        """
        rendered = self._rendered("sample")
        self.table = None
        for operation in (
            lambda: self.store.install(rendered),
            lambda: self.store.remove(rendered.key),
            lambda: self.store.list(),
            lambda: self.store.clear(),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeError):
                    operation()
        self.assertIsNone(self.table)

    def test_an_entry_this_tool_did_not_write_is_not_claimed(self) -> None:
        self.table.append("0 8 * * * cd /elsewhere && agents-live run --name x")
        self.table.append("0 8 * * * agents-live run # agents-live:v1:not-base64")
        self.assertEqual([], self.store.list())

    def test_every_installed_entry_round_trips_its_marker(self) -> None:
        """The marker is how convergence tells its own artifacts apart.

        A rendering change that broke the round trip would make every
        installed trigger invisible, and convergence would install a
        second copy of each.
        """
        for kind, trigger in (
            ("schedule", "0 8 * * *"),
            ("watch", "'docs/**' debounce 1s"),
        ):
            with self.subTest(kind=kind):
                subscription = Subscription.create(
                    scope=f"repo:{self.root}", target="agent:sample",
                    kind=kind, trigger=trigger)
                rendered = PosixHost().render(subscription)
                decoded = artifacts.from_rendered(rendered.rendered)
                self.assertIsNotNone(decoded)
                self.assertEqual(rendered.key, decoded.id)
                self.assertEqual(f"repo:{self.root}", decoded.scope)
                self.assertEqual("agent:sample", decoded.target)
                self.assertEqual(
                    "clock" if kind == "schedule" else None,
                    decoded.origin,
                )


class TestHostMaintenanceEntries(TempRepository):
    """The tool's own host entries, which no repository owns."""

    def test_maintenance_lines_name_no_repository(self) -> None:
        rendered = PosixHost().render(lifecycle.maintenance_subscription())
        self.assertNotIn(" cd ", rendered.rendered)
        self.assertNotIn("--repo", rendered.rendered)
        self.assertIn("internal maintain --metadata", rendered.rendered)
        self.assertIn(" --quiet", rendered.rendered)
        marker = artifacts.from_rendered(rendered.rendered)
        self.assertIsNotNone(marker)
        self.assertEqual("runtime", marker.target)
        self.assertIsNone(marker.origin)

    def test_the_loop_is_reachable_only_through_the_internal_command(self) -> None:
        """`health-check` was a public verb in 5.x. Its absence is the
        contract; a reintroduced public spelling would be installed by
        one release and unmatched by the next."""
        from agents_live.cli.spec import COMMANDS
        names = {command.name for command in COMMANDS}
        self.assertNotIn("health-check", names)
        self.assertIn("internal", names)


class TestPluginDeclarations(unittest.TestCase):
    """Which declared plugin is safe to install.

    Convergence installs what these functions approve, into the tool
    environment that holds the runtime, and it runs unattended.
    """

    def _wheel(self, directory: Path, *, name: str = "example-plugin",
               version: str = "1.0", group: str | None = None) -> Path:
        stem = name.replace("-", "_")
        wheel = directory / f"{stem}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"{stem}-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
            archive.writestr(
                f"{stem}-{version}.dist-info/entry_points.txt",
                f"[{group or providers.ENTRY_POINT_GROUP}]\n"
                f"example = {stem}:PROVIDER\n")
        return wheel

    def _project(self, directory: Path, wheel: Path, *,
                 sha256: str | None = None, name: str = "example-plugin") -> Path:
        digest = f'sha256 = "{sha256}"\n' if sha256 else ""
        (directory / ".agents-live.toml").write_text(
            f"[plugins.{name}]\n"
            f'path = "{wheel.name}"\n{digest}',
            encoding="utf-8")
        return directory

    def test_a_declaration_takes_its_identity_from_the_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wheel = self._wheel(root, version="2.5")
            self._project(root, wheel)
            declared = plugins.declared(root, require_exists=True)
        plugin = declared["example-plugin"]
        self.assertEqual("example-plugin", plugin.name)
        self.assertEqual("2.5", plugin.version)

    def test_a_wheel_that_declares_another_distribution_is_refused(self) -> None:
        """The configured name is what convergence installs and what the
        receipt records; a wheel naming something else installs a
        distribution nobody declared."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wheel = self._wheel(root, name="other-plugin")
            (root / ".agents-live.toml").write_text(
                "[plugins.example-plugin]\n"
                f'path = "{wheel.name}"\n',
                encoding="utf-8")
            with self.assertRaises(plugins.PluginError) as caught:
                plugins.declared(root, require_exists=True)
        self.assertIn("other-plugin", str(caught.exception))

    def test_a_checksum_that_does_not_match_the_wheel_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wheel = self._wheel(root)
            self._project(root, wheel, sha256="0" * 64)
            errors = plugins.validation_errors([root])
        self.assertTrue(errors)
        self.assertTrue(any("sha256" in item for item in errors))

    def test_two_projects_declaring_different_checksums_are_refused(self) -> None:
        """Convergence installs one environment for every project on the
        host, so two declarations of one plugin have to agree."""
        with tempfile.TemporaryDirectory() as first_dir, \
                tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir).resolve()
            second = Path(second_dir).resolve()
            self._project(first, self._wheel(first), sha256="a" * 64)
            self._project(second, self._wheel(second), sha256="b" * 64)
            with self.assertRaises(plugins.PluginError) as caught:
                plugins.union([first, second])
        self.assertIn("conflicting sha256", str(caught.exception))

    def test_a_declaration_without_its_wheel_takes_metadata_from_the_one_present(self) -> None:
        """One checkout can lag another. The union has to resolve to the
        artifact that exists rather than refusing the pair."""
        with tempfile.TemporaryDirectory() as present_dir, \
                tempfile.TemporaryDirectory() as absent_dir:
            present = Path(present_dir).resolve()
            absent = Path(absent_dir).resolve()
            wheel = self._wheel(present, version="3.1")
            self._project(present, wheel)
            (absent / ".agents-live.toml").write_text(
                "[plugins.example-plugin]\n"
                f'path = "{wheel.name}"\n',
                encoding="utf-8")
            union = plugins.union([absent, present])
        self.assertEqual("3.1", union["example-plugin"].version)

    def test_a_retired_group_is_refused_from_the_declaration_side(self) -> None:
        """Detecting it only while validating what is installed leaves the
        plugin permanently pending, so convergence reinstalls it every
        run (#263)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wheel = self._wheel(root, group="agents_live.agents")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self._project(root, wheel, sha256=digest)
            errors = plugins.validation_errors([root])
        self.assertTrue(any("retired" in item for item in errors))


class TestProviderRegistry(unittest.TestCase):
    """What happens when a definition names a provider.

    A selector that parses but resolves to nothing is the shape of #262:
    the repository looked healthy and every run failed at dispatch.
    """

    def test_the_public_providers_are_registered(self) -> None:
        self.assertLessEqual({"claude", "copilot", "fake"},
                             set(providers.names()))

    def test_an_unknown_provider_fails_closed_and_names_what_is_installed(self) -> None:
        with self.assertRaises(ValueError) as caught:
            providers.get("agency-copilot")
        message = str(caught.exception)
        self.assertIn("agency-copilot", message)
        for name in providers.names():
            self.assertIn(name, message)

    def test_registration_validates_the_fields_it_documents(self) -> None:
        class Provider:
            name = "probe-provider"
            models = None
            efforts = frozenset()

            def prepare(self, spec, request):  # pragma: no cover - unused
                raise AssertionError

            def parse(self, raw):  # pragma: no cover - unused
                raise AssertionError

        for attribute, value in (
            ("name", ""),
            ("efforts", ["high"]),
            ("models", {"sonnet"}),
        ):
            with self.subTest(attribute=attribute):
                candidate = Provider()
                setattr(candidate, attribute, value)
                with self.assertRaises(ValueError):
                    providers.register(candidate)
                self.assertNotIn("probe-provider", providers.names())

    def test_a_second_provider_cannot_take_a_registered_name(self) -> None:
        existing = providers.get("fake")
        providers.register(existing)

        class Impostor:
            name = "fake"
            models = None
            efforts = frozenset()

            def prepare(self, spec, request):  # pragma: no cover - unused
                raise AssertionError

            def parse(self, raw):  # pragma: no cover - unused
                raise AssertionError

        with self.assertRaises(ValueError):
            providers.register(Impostor())
        self.assertIs(existing, providers.get("fake"))


class TestDamagedLogsStayReadable(TempRepository):
    """Readers meet records no writer meant to produce.

    A live deployment's log directory held 11,577 lines where two
    appenders interleaved mid-record. Every reader crossing that
    directory has to step over them: a diagnostic that dies on the way to
    the incident is the one moment it had a job.
    """

    DAMAGED = [
        '{"ts":"2026-08-01T00:00:00Z",{"ts":"2026-08-01T00:01:00Z",'
        '"log_schema":5,"agent_name":"torn","phase":"done","status":"ok"}',
        "not-json",
        "[]",
        '{"log_schema":5,"agent_name":"no-timestamp","phase":"done",'
        '"status":"ok"}',
        '{"log_schema":5,"ts":[],"agent_name":"bad-timestamp",'
        '"phase":"done","status":"ok"}',
    ]

    def _logs(self) -> Path:
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mixed.log").write_text("\n".join([
            '{"log_schema":5,"ts":"2026-08-01T00:02:00Z",'
            '"agent_name":"healthy-agent","phase":"done","status":"ok"}',
            *self.DAMAGED,
            '{"log_schema":5,"ts":"2026-08-01T00:03:00Z",'
            '"agent_name":"healthy-agent","phase":"done","status":"error"}',
        ]) + "\n", encoding="utf-8")
        return directory

    def test_a_damaged_record_never_hides_the_records_around_it(self) -> None:
        records = obs.load(obs.files(self._logs()))
        self.assertEqual(
            ["healthy-agent", "healthy-agent"],
            [record["agent_name"] for record in records])

    def test_the_decoder_catches_the_class_not_the_bound_attribute(self) -> None:
        """The flat script dispatches import this module twice, so the
        attribute a handler resolves is not always the class the raising
        copy produced. Catching ``ValueError`` is what makes the guard
        hold under either import."""
        source = (Path(obs.query.__file__).read_text(encoding="utf-8")
                  if hasattr(obs, "query") else "")
        self.assertNotIn("except json.JSONDecodeError", source)

    def test_the_dashboard_reads_the_directory_once_for_every_row(self) -> None:
        """Once per agent turns a 50 MB directory into a gigabyte of
        parsing per refresh, which blocks the event loop long enough for
        the browser to drop the websocket."""
        self._logs()
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
            with (
                mock.patch.object(
                    dashboard, "LOGS_DIR",
                    paths.repo_state_dir(self.root) / "logs"),
                mock.patch.object(
                    dashboard.obs, "load",
                    side_effect=dashboard.obs.load) as load,
            ):
                index = dashboard.last_run_index()
        self.assertEqual(1, load.call_count)
        # last_ok survives a later failure: the two are tracked apart so
        # the table can show when it last worked and when it last broke.
        self.assertEqual(
            ("2026-08-01T00:02:00Z", "2026-08-01T00:03:00Z", "error"),
            index["healthy-agent"])


class TestFailuresAreVisible(TempRepository):
    """A failed run has to reach the surfaces an operator watches.

    Two runs failed on screen while the dashboard header read "errors in
    last hour: none" and `logs --errors` returned nothing. Neither
    surface was broken in a way any run could show: one globbed the wrong
    suffix, the other read one file, and both answered "none", which is
    the one wrong answer these queries must never give.
    """

    IDENTIFIER = "failing-agent-1234567890"

    def _logs(self) -> Path:
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        moment = datetime.now(timezone.utc).isoformat()
        (directory / f"{self.IDENTIFIER}.jsonl").write_text(json.dumps({
            "log_schema": 5, "ts": moment, "agent_name": self.IDENTIFIER,
            "phase": "done", "status": "error", "trigger": "manual",
            "model": "test-model-1", "message": "child exited with status 2",
        }) + "\n", encoding="utf-8")
        (directory / "agents-live.log").write_text(json.dumps({
            "log_schema": 5, "ts": moment, "agent_name": "framework",
            "phase": "done", "status": "ok", "message": "unrelated",
        }) + "\n", encoding="utf-8")
        return directory

    def _dashboard(self):
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        return dashboard

    def test_the_header_counts_a_failure_written_under_an_identifier(self) -> None:
        """Records key on the identifier and the row shows the display
        name. Matching only display names filed every failed run under
        "framework", where no agent's row could show it."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")
        logs = self._logs()
        dashboard = self._dashboard()
        with mock.patch.object(dashboard, "LOGS_DIR", logs):
            errors, models = dashboard._structured_log_snapshot(
                {self.IDENTIFIER: "failing-agent"})
        self.assertEqual({"failing-agent": 1}, errors)
        self.assertEqual({"failing-agent": "test-model-1"}, models)

    def test_the_header_reads_both_log_suffixes(self) -> None:
        """A run's outcome is written to <identifier>.jsonl. A *.log glob
        counted zero with failed runs on the screen."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")
        logs = self._logs()
        (logs / f"{self.IDENTIFIER}.jsonl").rename(
            logs / f"{self.IDENTIFIER}.jsonl.kept")
        dashboard = self._dashboard()
        with mock.patch.object(dashboard, "LOGS_DIR", logs):
            self.assertEqual(
                ({}, {}),
                dashboard._structured_log_snapshot(
                    {self.IDENTIFIER: "failing-agent"}))
        (logs / f"{self.IDENTIFIER}.jsonl.kept").rename(
            logs / f"{self.IDENTIFIER}.jsonl")
        with mock.patch.object(dashboard, "LOGS_DIR", logs):
            errors, _ = dashboard._structured_log_snapshot(
                {self.IDENTIFIER: "failing-agent"})
        self.assertEqual({"failing-agent": 1}, errors)

    def test_asking_for_errors_spans_the_repository_not_one_file(self) -> None:
        """`--errors` with no name is a question about the repository.
        Scoping it to one file answered "none" while failed runs sat in
        per-agent logs. Agents write one file each and nothing writes a
        per-repository log, so any query that names no file now spans
        them all, and the filename that never existed must not return."""
        source = Path(qlog.__file__).read_text(encoding="utf-8")
        decision = source.split("patterns = ", 1)[1].split("\n", 1)[0]
        self.assertIn("span_everything", decision)
        self.assertIn("args.log is None", source)
        self.assertNotIn("agents-live.log", source)


    def test_a_relative_window_narrows_monotonically(self) -> None:
        """The bound is compared as a string, so an unresolved one does
        not fail. `"2026-..." < "30m"` is true and discards everything;
        `< "1h"` is false and discards nothing. The same timeline
        answered "no events found" or "here is everything" depending on
        which word was typed."""
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        (directory / "windowed.jsonl").write_text("\n".join(
            json.dumps({
                "log_schema": 5,
                "ts": (now - timedelta(minutes=minutes)).isoformat(),
                "agent_name": "windowed", "phase": "done", "status": "ok",
            })
            for minutes in (5, 45, 90, 400)
        ) + "\n", encoding="utf-8")
        counts = [
            len(obs.load(obs.files(directory), since=window))
            for window in ("30m", "1h", "2h", "1d")
        ]
        self.assertEqual([1, 2, 3, 4], counts)
        self.assertEqual(counts, sorted(counts))

    def test_an_unreadable_window_is_refused_rather_than_applied(self) -> None:
        with self.assertRaises(ValueError) as caught:
            obs.query.resolve_since("not-a-time")
        self.assertIn("30m", str(caught.exception))

    def test_every_reader_resolves_the_window_the_same_way(self) -> None:
        """Two readers with two parsers is how one accepted `30m` and the
        other compared against the literal string."""
        self.assertIs(qlog._resolve_ts("30m") is None, False)
        for value in ("30m", "2 hours ago", "1d"):
            with self.subTest(value=value):
                self.assertEqual(
                    obs.query.resolve_since(value)[:16],
                    qlog._resolve_ts(value)[:16])

    def test_schema_check_names_where_a_handler_record_is_invalid(self) -> None:
        """A count alone cannot tell a handler author what to fix."""
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / "handler.jsonl"
        log.write_text("\n".join([
            json.dumps({
                "log_schema": 5, "ts": "2026-08-01T00:00:00Z",
                "agent_name": "handler", "phase": "sync",
            }),
            json.dumps({"log_schema": 5, "agent_name": "handler"}),
            json.dumps({
                "log_schema": 4, "ts": "2026-08-01T00:02:00Z",
                "agent_name": "handler",
            }),
            json.dumps({
                "log_schema": "5", "ts": "2026-08-01T00:03:00Z",
                "agent_name": "handler",
            }),
            json.dumps({
                "log_schema": 5, "ts": "2026-08-01T00:04:00",
                "agent_name": "handler",
            }),
            "{not-json",
            json.dumps({
                "spec": 1, "timestamp": "not-a-time", "event": "run",
                "status": "success", "agent": "handler", "run_id": "bad-ts",
                "origin": "manual",
            }),
        ]) + "\n", encoding="utf-8")

        con = qlog.duckdb.connect(":memory:")
        patterns = [str(directory / "*.jsonl")]
        qlog.build_view(con, patterns)
        message = "; ".join(qlog.check_schema(con, patterns))

        self.assertIn("6 JSONL row(s)", message)
        self.assertIn(f"{log}: line 2: missing field(s): ts", message)
        self.assertIn(f"{log}: line 3: invalid field(s): log_schema", message)
        self.assertIn(f"{log}: line 4: invalid field(s): log_schema", message)
        self.assertIn(
            f"{log}: line 5: invalid field(s): ts (UTC offset required)",
            message,
        )

    def test_schema_check_includes_invalid_archive_rows(self) -> None:
        directory = paths.repo_state_dir(self.root) / "logs"
        archive = directory / "archive"
        archive.mkdir(parents=True)
        live = directory / "live.jsonl"
        live.write_text(json.dumps({
            "log_schema": 5, "ts": "2026-08-01T00:00:00Z",
            "agent_name": "handler",
        }) + "\n", encoding="utf-8")
        (archive / "heartbeat.log").write_text(
            "2026-08-01 heartbeat is healthy\n", encoding="utf-8")
        parquet = archive / "2026-08.parquet"
        writer = qlog.duckdb.connect(":memory:")
        writer.sql(
            "CREATE TABLE archived AS SELECT "
            "CAST(NULL AS VARCHAR) AS ts, "
            "'handler'::VARCHAR AS agent_name, 5::INTEGER AS log_schema"
        )
        writer.sql(
            f"COPY archived TO '{parquet}' (FORMAT PARQUET)"
        )

        con = qlog.duckdb.connect(":memory:")
        patterns = [str(directory / "*.jsonl")]
        qlog.build_view(con, patterns, archives=archive)
        message = "; ".join(qlog.check_schema(con, patterns))

        self.assertIn("1 JSONL row(s)", message)


    def test_health_follows_the_newest_run_not_the_last_one_read(self) -> None:
        """An agent whose history spans a rename has two log files, and
        the older sorts last (`-` before `.`), so reading order made a
        stale failure the current health: three successful runs and a
        green one 35 minutes ago still showed red."""
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        identifier = "renamed-agent-abcdef1234"

        def record(moment: str, status: str) -> str:
            return json.dumps({
                "log_schema": 5, "ts": moment, "agent_name": identifier,
                "phase": "done", "status": status,
            })

        # The newer file sorts first, so the stale failure is read last.
        (directory / f"{identifier}.jsonl").write_text(
            record("2026-08-12T04:08:19.249904+00:00", "ok") + "\n",
            encoding="utf-8")
        (directory / "renamed-agent.jsonl").write_text(
            record("2026-08-11T16:06:30.128Z", "error") + "\n",
            encoding="utf-8")
        # The fixture only means something while the newer file is read
        # first, which is what puts the stale failure last.
        self.assertEqual(
            [f"{identifier}.jsonl", "renamed-agent.jsonl"],
            [path.name for path in obs.files(directory)])

        dashboard = self._dashboard()
        with mock.patch.object(dashboard, "LOGS_DIR", directory):
            last_ok, last_err, status = dashboard.last_run_index()[identifier]
        self.assertEqual("ok", status)
        self.assertEqual("2026-08-12T04:08:19.249904+00:00", last_ok)
        self.assertEqual("2026-08-11T16:06:30.128Z", last_err)


class TestFrameworkRetention(TempRepository):
    def test_maintenance_rotates_queryable_logs_and_skips_active_runs(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            "retention_days = 1\n", encoding="utf-8")
        repos._add(str(self.root))
        state_dir = paths.repo_state_dir(self.root)
        logs = state_dir / "logs"
        log = logs / "retained.jsonl"
        old = datetime.now(timezone.utc) - timedelta(days=2)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        for timestamp, run_id in ((old, "old"), (recent, "recent")):
            obs.record(log, obs.Event(
                timestamp=timestamp.isoformat(),
                event="run",
                status="success",
                repository=str(self.root),
                agent="retained",
                run_id=run_id,
                origin="clock",
            ))
        host_log = paths.host_logs_dir() / "host-retained.jsonl"
        obs.record(host_log, obs.Event(
            timestamp=old.isoformat(),
            event="admin",
            status="success",
            repository="",
            agent="host-retained",
            run_id="host-old",
            origin="maintenance",
        ))

        archive = logs / "archive"
        archive.mkdir()
        expired = archive / "expired.jsonl"
        expired.write_text("{}\n", encoding="utf-8")
        expired_time = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        os.utime(expired, (expired_time, expired_time))

        runs = state_dir / "runs" / "retained"
        inactive = runs / "inactive"
        inactive.mkdir(parents=True)
        inactive_output = inactive / "processor-output.json"
        inactive_output.write_text("old", encoding="utf-8")
        inactive_transcript = runs / "inactive-agent-1.json"
        inactive_transcript.write_text("old", encoding="utf-8")
        active_id = "active"
        active = runs / active_id
        active.mkdir()
        (active / ".active").write_text(
            json.dumps({"pid": os.getpid()}), encoding="ascii")
        active_output = active / "processor-output.json"
        active_output.write_text("in use", encoding="utf-8")
        active_pipeline = runs / f"{active_id}-pipeline.jsonl"
        active_pipeline.write_text("in use", encoding="utf-8")
        for artifact in (
            inactive_output, inactive_transcript, active_output, active_pipeline,
        ):
            os.utime(artifact, (expired_time, expired_time))

        result = mock.Mock(done=(), failed=(), health=runtime.Health(True))
        collected = mock.Mock(subscriptions=())
        with (
            mock.patch.object(
                internal.lifecycle, "converge", return_value=result),
            mock.patch.object(
                internal.lifecycle, "collect", return_value=collected),
        ):
            self.assertEqual(0, internal.main(["maintain", "--quiet"]))

        self.assertFalse(log.exists())
        self.assertFalse(expired.exists())
        self.assertFalse(inactive_output.exists())
        self.assertFalse(inactive_transcript.exists())
        self.assertTrue(active_output.exists())
        self.assertTrue(active_pipeline.exists())
        self.assertEqual(
            {"old", "recent"},
            {str(record["run_id"]) for record in obs.load(obs.files(logs))},
        )

        connection = qlog.duckdb.connect(":memory:")
        qlog.build_view(
            connection, [str(logs / "*.jsonl")], archives=archive)
        self.assertEqual(
            [(2, True)],
            connection.sql(
                "SELECT count(*), bool_and(_archive) FROM log").fetchall(),
        )
        host_connection = qlog.duckdb.connect(":memory:")
        qlog.build_view(
            host_connection, qlog.all_log_globs(), archives=qlog.archive_dirs())
        self.assertEqual(
            [(1, True)],
            host_connection.sql(
                "SELECT count(*), bool_and(_archive) FROM log "
                "WHERE agent_name = 'host-retained'").fetchall(),
        )
        maintenance = [
            record for record in obs.load(obs.files(paths.host_logs_dir()))
            if record.get("operation") == "maintenance"
            and record.get("status") == "ok"
        ][-1]
        self.assertEqual(2, maintenance["rotated_logs"])
        self.assertEqual(1, maintenance["removed_archives"])
        self.assertEqual(2, maintenance["removed_run_artifacts"])


class TestProviderOutputSurvivesItsFooter(unittest.TestCase):
    """A provider CLI's own chatter must not fail a run that produced output.

    A copilot release began printing a session footer after the answer.
    The extractor parsed the whole text, so an agent that had done its
    work, emitted valid JSON, and exited zero was recorded as
    `output_parse_error`. Stripping footers by prefix means chasing every
    release; finding the value does not.
    """

    FOOTER = (
        "\n\nChanges    +0 -0\n"
        "AI Credits 22.7 (1m 5s)\n"
        "Tokens     \u2191 85.0k (40.4k cached) \u2022 \u2193 7.0k (1.2k reasoning)\n"
        "Resume     copilot --resume=785fa91c-24b3-4ae5-a200-8124fd6a6c9c\n"
    )

    def test_a_session_footer_does_not_hide_the_answer(self) -> None:
        payload = {"diagnosisDate": "2026-08-12", "groups": [{"severity": "noise"}]}
        text = json.dumps(payload) + self.FOOTER
        self.assertEqual(payload, port._extract_json(text))

    def test_a_fenced_block_still_wins_over_surrounding_prose(self) -> None:
        text = (
            "Here is the result:\n\n```json\n{\"chosen\": true}\n```\n"
            + self.FOOTER)
        self.assertEqual({"chosen": True}, port._extract_json(text))

    def test_the_last_complete_value_is_the_answer(self) -> None:
        """Preamble can contain braces of its own; the answer is last."""
        text = ('note: {"draft": 1} was superseded\n'
                '{"final": 2}' + self.FOOTER)
        self.assertEqual({"final": 2}, port._extract_json(text))

    def test_output_with_no_json_at_all_still_reports_none(self) -> None:
        self.assertIsNone(port._extract_json("no value here" + self.FOOTER))
        self.assertIsNone(port._extract_json("{unbalanced" + self.FOOTER))


class TestPendingUpgradeIdentity(TempRepository):
    """A durable record outlives the process it names.

    The handoff is single-flight, so a pid that reads as alive when it is
    really a different process refuses every later upgrade instead of
    merely reporting stale information.
    """

    def _reference(self, *, created_at: float = 1000.0) -> ProcessRef:
        return ProcessRef(
            4321, created_at, "powershell.exe", "upgrade", "operation-1")

    def test_a_reused_pid_is_not_the_helper_that_was_started(self) -> None:
        with (
            mock.patch.object(hostruntime, "is_alive", return_value=True),
            mock.patch.object(
                hostruntime, "process_start_time", return_value=5000.0),
        ):
            self.assertFalse(WindowsProcesses().alive(self._reference()))

    def test_the_same_process_still_counts_as_running(self) -> None:
        with (
            mock.patch.object(hostruntime, "is_alive", return_value=True),
            mock.patch.object(
                hostruntime, "process_start_time", return_value=1000.4),
        ):
            self.assertTrue(WindowsProcesses().alive(self._reference()))

    def test_an_unknown_start_time_is_not_treated_as_a_mismatch(self) -> None:
        """Unavailable is not evidence. Refusing on it would abandon a
        live upgrade and let a second one race the same environment."""
        with (
            mock.patch.object(hostruntime, "is_alive", return_value=True),
            mock.patch.object(
                hostruntime, "process_start_time", return_value=None),
        ):
            self.assertTrue(WindowsProcesses().alive(self._reference()))

    def test_a_dead_pid_is_never_running(self) -> None:
        with mock.patch.object(hostruntime, "is_alive", return_value=False):
            self.assertFalse(WindowsProcesses().alive(self._reference()))

    def test_the_start_time_probe_answers_for_this_process(self) -> None:
        """The guard is only as good as the primitive under it."""
        started = hostruntime.process_start_time(os.getpid())
        self.assertIsNotNone(started)
        self.assertLess(abs(time.time() - started), 3600)
        self.assertIsNone(hostruntime.process_start_time(999_999_999))


class TestWindowsDetachedProcess(unittest.TestCase):
    """Detached watchers outlive maintenance without opening a terminal."""

    def test_owned_watchers_use_hidden_process_discovery(self) -> None:
        metadata = artifacts.encode(artifacts.InvocationMetadata(
            "0123456789abcdef01234567",
            "repo:C:/work/sample",
            "agent:sample",
        ))
        command = (
            "C:/tools/agents-live.exe --repo C:/work/sample internal "
            f"watch-loop --metadata {metadata} sample"
        )
        with (
            mock.patch.object(
                hostruntime, "process_command_lines",
                return_value=[(42, command)]),
            mock.patch.object(
                hostruntime, "process_start_time", return_value=123.5),
        ):
            found = WindowsProcesses().owned("watcher")

        self.assertEqual([
            ProcessRef(
                42,
                123.5,
                "agents-live.exe",
                "watcher",
                "0123456789abcdef01234567",
                "agents-live:v2:0123456789abcdef01234567",
            ),
        ], found)

    def test_termination_uses_native_process_policy(self) -> None:
        reference = ProcessRef(
            42, 123.5, "agents-live.exe", "watcher", "key", "fingerprint")
        with (
            mock.patch.object(WindowsProcesses, "alive", return_value=True),
            mock.patch.object(hostruntime, "terminate") as terminate,
        ):
            WindowsProcesses().terminate(reference)

        terminate.assert_called_once_with(42)

    def test_a_detached_watcher_uses_host_spawn_policy(self) -> None:
        process = mock.Mock(pid=42)
        with mock.patch.object(
                hostruntime, "spawn_detached", return_value=process) as spawn:
            WindowsProcesses().spawn_detached(
                ["agents-live.exe", "internal", "watch-loop", "sample"],
                role="watcher",
                key="subscription",
                fingerprint="fingerprint",
            )

        spawn.assert_called_once_with(
            ["agents-live.exe", "internal", "watch-loop", "sample"],
            cwd=None,
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows console behavior")
    def test_a_detached_watcher_owns_a_console_nobody_can_see(self) -> None:
        source = (
            "import ctypes, pathlib, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "pathlib.Path(sys.argv[1]).write_text("
            "f'{kernel32.GetConsoleCP()} {kernel32.GetConsoleWindow()}', "
            "encoding='utf-8')\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "console.txt"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                WindowsProcesses().spawn_detached(
                    [sys.executable, "-c", source, str(result)],
                    role="watcher",
                    key="subscription",
                    fingerprint="fingerprint",
                )
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(result.exists(), "detached child produced no result")
            code_page, console_window = result.read_text(
                encoding="utf-8").split()

        self.assertNotEqual("0", code_page)
        self.assertEqual("0", console_window)


class TestConcurrentAppendersKeepRecordsWhole(TempRepository):
    """Several processes append to one log, and a split record is lost.

    Watchers, scheduled runs, and the maintenance loop share these files.
    A live deployment accumulated 11,577 records spliced into each other,
    and because every reader skips what it cannot decode, the history
    simply went missing.

    The race itself is not reliably reproducible - four writers against a
    text-mode stream pass most of the time - so what is asserted is the
    mechanism that makes it impossible: one record leaves in one write,
    at a position the kernel chooses.
    """

    def _event(self, size: int = 40000):
        return obs.create(
            "done", "ok", repository="/repo", agent="writer",
            run_id="run-1", origin="test", message="x" * size)

    def test_one_record_leaves_in_one_write(self) -> None:
        log = paths.repo_state_dir(self.root) / "logs" / "shared.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        sizes: list[int] = []
        flags: list[int] = []
        real_write, real_open = os.write, os.open

        def counting_write(descriptor, data):
            sizes.append(len(data))
            return real_write(descriptor, data)

        def recording_open(path, opened_flags, *rest):
            flags.append(opened_flags)
            return real_open(path, opened_flags, *rest)

        for size in (10, 40000):
            with self.subTest(size=size):
                sizes.clear()
                flags.clear()
                with (
                    mock.patch.object(obs.events.os, "write", counting_write),
                    mock.patch.object(obs.events.os, "open", recording_open),
                ):
                    obs.record(log, self._event(size))
                self.assertEqual(
                    1, len(sizes),
                    f"record left in {len(sizes)} writes: {sizes}")
                self.assertTrue(flags and flags[0] & os.O_APPEND)

    def test_the_records_written_are_the_records_read_back(self) -> None:
        log = paths.repo_state_dir(self.root) / "logs" / "shared.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            obs.record(log, self._event(4000))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(20, len(lines))
        self.assertEqual(20, len([json.loads(line) for line in lines]))

    def test_lost_history_is_counted_rather_than_skipped_in_silence(self) -> None:
        """A dropped line looks exactly like one that was never written."""
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "torn.jsonl").write_text("\n".join([
            '{"log_schema":5,"ts":"2026-08-01T00:00:00Z","agent_name":"a",'
            '"phase":"done","status":"ok"}',
            '{"ts":"2026-08-01T00:01:00Z",{"ts":"2026-08-01T00:02:00Z"}',
            "",
            "not-json",
        ]) + "\n", encoding="utf-8")
        (directory / "dashboard-transcript.log").write_text(
            "Run completed\nhandler output\n", encoding="utf-8")
        self.assertEqual(2, obs.query.damaged(obs.files(directory)))
        self.assertEqual(1, len(obs.load(obs.files(directory))))


class TestStateSurvivesAConcurrentReader(TempRepository):
    """One process reading a state file must not fail another's write.

    Every durable file this tool keeps - started intent, the dashboard
    registry, health and update records - is written through
    `atomic_write_text`. On POSIX the concluding `rename` succeeds no
    matter who holds the target open, so the primitive read as safe.
    Windows refuses with `ERROR_ACCESS_DENIED` while any process holds
    the destination open without `FILE_SHARE_DELETE`, which every plain
    `open()` omits. So one command merely *reading* a state file made
    another's write fail outright, and the same hold is what antivirus
    and search indexers take on a freshly written file.

    That surfaced as a traceback from `dashboard`, but the registry it
    failed on is the least of the files involved: the same primitive
    records which agents are started.
    """

    def _target(self) -> Path:
        target = paths.repo_state_dir(self.root) / "registry.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n", encoding="utf-8")
        return target

    def test_a_write_waits_out_a_reader_holding_the_destination(self) -> None:
        target = self._target()
        reader = target.open("r", encoding="utf-8")
        timer = threading.Timer(0.2, reader.close)
        timer.start()
        try:
            paths.atomic_write_text(target, "replacement\n")
        finally:
            timer.cancel()
            reader.close()
        self.assertEqual("replacement\n", target.read_text(encoding="utf-8"))

    def test_a_destination_that_never_frees_still_reports_the_failure(self) -> None:
        """Waiting must not become swallowing: a real block still raises."""
        target = self._target()
        with mock.patch.object(
                paths.os, "replace",
                side_effect=PermissionError(13, "held")):
            with self.assertRaises(PermissionError):
                paths.atomic_write_text(target, "replacement\n")
        self.assertEqual("original\n", target.read_text(encoding="utf-8"))
        self.assertEqual(
            [], [entry for entry in target.parent.iterdir()
                 if entry.name.startswith(f".{target.name}.")],
            "a failed write left its temp file behind")

    def test_waiting_is_bounded(self) -> None:
        """An unavailable destination fails in seconds, not never."""
        target = self._target()
        with mock.patch.object(
                paths.os, "replace",
                side_effect=PermissionError(13, "held")):
            started = time.monotonic()
            with self.assertRaises(PermissionError):
                paths.atomic_write_text(target, "replacement\n")
            waited = time.monotonic() - started
        self.assertLess(waited, 30.0, f"waited {waited:.1f}s before failing")


class TestRunsRecordWhatTheySpent(TempRepository):
    """The provider meters the work; nothing else can.

    Across 47,810 records on a live host, none carried usage, so both
    cost columns and the totals line had never shown a number. The figure
    was being printed on stdout and discarded.
    """

    FOOTER = (
        "\x1b[32mChanges\x1b[0m    +0 -0\n"
        "AI Credits 22.7 (1m 5s)\n"
        "Tokens     \u2191 85.0k (40.4k cached) \u2022 \u2193 7.0k (1.2k reasoning)\n"
        "Resume     copilot --resume=785fa91c\n"
    )

    def test_the_copilot_footer_is_recorded_as_usage(self) -> None:
        completion = providers.get("copilot").parse(
            RawOutput(0, '{"done": true}\n' + self.FOOTER, ""))
        usage = dict(completion.usage)
        self.assertEqual("22.7", usage["ai_credits"])
        self.assertEqual("0.227", usage["list_cost_usd"])
        self.assertEqual("85.0k", usage["input_tokens"])
        self.assertEqual("40.4k", usage["cached_tokens"])
        self.assertEqual("7.0k", usage["output_tokens"])

    def test_copilot_list_cost_uses_exact_decimal_arithmetic(self) -> None:
        completion = providers.get("copilot").parse(RawOutput(
            0, "AI Credits 57.7\n", "",
        ))
        self.assertEqual("0.577", dict(completion.usage)["list_cost_usd"])

    def test_output_without_a_footer_reports_no_usage(self) -> None:
        completion = providers.get("copilot").parse(
            RawOutput(0, '{"done": true}\n', ""))
        self.assertEqual((), completion.usage)

    def test_copilot_json_stream_preserves_answer_and_exact_cost(self) -> None:
        launch = providers.get("copilot").prepare(
            ResolvedSpec(
                "cost", "prompt", "write", (), (), (), "copilot", None, None),
            Request(),
        )
        self.assertIn("--output-format", launch.argv)
        # A terminal is what wraps a long event across lines, and every
        # figure below arrives on one.
        self.assertFalse(launch.use_pty)
        stream = "\n".join([
            "warning emitted before the JSON stream",
            json.dumps({
                "type": "assistant.message",
                "data": {
                    "phase": "final_answer",
                    "content": '{"done": true}',
                },
            }),
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "Later unqualified message."},
            }),
            json.dumps({
                "type": "session.usage_checkpoint",
                "data": {"totalNanoAiu": 15110175000},
            }),
            json.dumps({
                "type": "result",
                "exitCode": 0,
                "usage": {"sessionDurationMs": 1234},
            }),
        ])
        completion = providers.get("copilot").parse(RawOutput(0, stream, ""))
        self.assertEqual('{"done": true}', completion.text)
        self.assertEqual({
            "ai_credits": "15.110175",
            "list_cost_usd": "0.15110175",
        }, dict(completion.usage))

    def test_copilot_json_accepts_the_last_answer_without_a_phase(self) -> None:
        stream = "\n".join([
            json.dumps({
                "type": "assistant.message",
                "data": {"content": '{"draft": true}'},
            }),
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "  "},
            }),
            json.dumps({
                "type": "assistant.message",
                "data": {"content": '{"ok": true}'},
            }),
            json.dumps({
                "type": "assistant.message",
                "data": {
                    "content": "The task is complete.",
                    "toolRequests": [{"name": "task_complete"}],
                },
            }),
            json.dumps({
                "type": "session.task_complete",
                "data": {"summary": "Completed with valid JSON."},
            }),
            json.dumps({
                "type": "session.usage_checkpoint",
                "data": {"totalNanoAiu": 2500000000},
            }),
        ])
        completion = providers.get("copilot").parse(RawOutput(0, stream, ""))
        self.assertEqual('{"ok": true}', completion.text)
        self.assertEqual("2.5", dict(completion.usage)["ai_credits"])

    def test_copilot_json_uses_final_checkpoint_and_task_summary_fallback(
        self,
    ) -> None:
        stream = "\n".join([
            json.dumps({
                "type": "session.usage_checkpoint",
                "data": {"totalNanoAiu": 1000000000},
            }),
            "{malformed final event",
            json.dumps({
                "type": "assistant.message",
                "data": {
                    "phase": "analysis",
                    "content": "Intermediate reasoning.",
                },
            }),
            json.dumps({
                "type": "session.task_complete",
                "data": {"summary": "Completed from task summary.", "success": True},
            }),
            json.dumps({
                "type": "session.usage_checkpoint",
                "data": {"totalNanoAiu": 2500000000},
            }),
        ])
        completion = providers.get("copilot").parse(RawOutput(0, stream, ""))
        self.assertEqual("Completed from task summary.", completion.text)
        self.assertEqual("2.5", dict(completion.usage)["ai_credits"])

    def test_claude_preserves_provider_reported_list_cost(self) -> None:
        completion = providers.get("claude").parse(RawOutput(
            0,
            json.dumps({
                "result": "done",
                "usage": {"input_tokens": 100},
                "total_cost_usd": 0.42,
            }),
            "",
        ))
        self.assertEqual("0.42", dict(completion.usage)["list_cost_usd"])

    def test_the_dashboard_reads_only_normalized_list_cost(self) -> None:
        dashboard = self._dashboard()
        self.assertEqual(0.227, dashboard._entry_cost_usd(
            {"usage": [["ai_credits", "22.7"], ["list_cost_usd", "0.227"]]}))
        self.assertIsNone(dashboard._entry_cost_usd(
            {"usage": [["ai_credits", "22.7"]]}))
        self.assertIsNone(dashboard._entry_cost_usd({"usage": []}))

    def test_a_recorded_run_reaches_the_column(self) -> None:
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        obs.record(directory / "spender-1234567890.jsonl", obs.create(
            "done", "ok", repository=str(self.root),
            agent="spender-1234567890", run_id="run-1", origin="manual",
            usage=(
                ("ai_credits", "22.7"),
                ("list_cost_usd", "0.227"),
            )))
        dashboard = self._dashboard()
        with mock.patch.object(dashboard, "LOGS_DIR", directory):
            costs = dashboard.cost_index()
        self.assertEqual((0.227, 0.227), costs["spender-1234567890"])
        self.assertEqual(
            ("0.23", "0.23"),
            dashboard.agent_cost("spender-1234567890", costs))

    def test_legacy_display_name_history_reaches_the_canonical_row(self) -> None:
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        obs.record(directory / "legacy-agent.jsonl", obs.create(
            "done", "ok", repository=str(self.root),
            agent="legacy-agent", run_id="run-1", origin="manual",
            usage=(("list_cost_usd", "0.25"),)))
        dashboard = self._dashboard()
        agents = [{
            "name": "legacy-agent",
            "identifier": "legacy-agent-1234567890",
        }]
        with mock.patch.object(dashboard, "LOGS_DIR", directory):
            runs, costs = dashboard._scan(dashboard._history_aliases(agents))
        self.assertEqual(
            "ok", runs["legacy-agent-1234567890"][2])
        self.assertEqual(
            (0.25, 0.25), costs["legacy-agent-1234567890"])

    def test_unchanged_dashboard_logs_are_not_reparsed(self) -> None:
        directory = paths.repo_state_dir(self.root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        obs.record(directory / "cached-agent.jsonl", obs.create(
            "done", "ok", repository=str(self.root),
            agent="cached-agent-1234567890", run_id="run-1",
            origin="manual"))
        dashboard = self._dashboard()
        with (
            mock.patch.object(dashboard, "LOGS_DIR", directory),
            mock.patch.object(
                dashboard.obs, "load",
                side_effect=dashboard.obs.load) as load,
        ):
            dashboard._scan()
            dashboard._scan()
        self.assertEqual(1, load.call_count)

    def test_dashboard_agent_collection_never_pulls_ownership(self) -> None:
        dashboard = self._dashboard()
        with (
            mock.patch.object(dashboard, "REPO_ROOT", self.root),
            mock.patch.object(
                dashboard.agent_view, "repository_agents",
                return_value=()) as repository_agents,
        ):
            self.assertEqual([], dashboard.collect_agents())
        repository_agents.assert_called_once_with(
            self.root, ownership_rate_limit_secs=10**9)

    def test_dashboard_totals_unrounded_list_cost(self) -> None:
        dashboard = self._dashboard()
        rows = [
            {
                "cost_day": "0.04",
                "cost_week": "0.04",
                "cost_day_value": 0.04,
                "cost_week_value": 0.04,
            }
            for _ in range(10)
        ]
        self.assertEqual(("0.40", "0.40"), dashboard._cost_totals(rows))

    def _dashboard(self):
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        return dashboard


class TestDashboardHealthPolicy(unittest.TestCase):
    def test_beacon_is_stale_after_one_hour_of_missed_five_minute_passes(
        self,
    ) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        with tempfile.TemporaryDirectory() as temporary:
            beacon = Path(temporary) / "health.ok"
            beacon.write_text('{"watchers":0,"cron":1}', encoding="utf-8")
            with mock.patch.object(dashboard, "HEALTH_OK_PATH", beacon):
                fresh = time.time() - 59 * 60
                os.utime(beacon, (fresh, fresh))
                self.assertEqual("ok", dashboard.system_health()["level"])

                stale = time.time() - 61 * 60
                os.utime(beacon, (stale, stale))
                health = dashboard.system_health()
        self.assertEqual("down", health["level"])
        self.assertIn("expected every five minutes", health["tip"])
        self.assertIn("unhealthy after one hour", health["tip"])


class TestDashboardActionCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_running_duplicate_action_is_coalesced(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        started = asyncio.Event()
        release = asyncio.Event()
        executions = 0

        async def execute(_request):
            nonlocal executions
            executions += 1
            started.set()
            await release.wait()
            return 0

        dashboard._ACTION_QUEUE.clear()
        dashboard._PENDING_ACTIONS.clear()
        dashboard._ACTION_WORKER = None
        dashboard._ACTION_RUNNING = False
        with mock.patch.object(dashboard, "_execute_action", side_effect=execute):
            first = asyncio.create_task(dashboard.do_action(
                "Run", "run", ["--name", "sample"], agent_name="sample",
                repository="repo", repository_path="/repos/sample"))
            await started.wait()
            second = asyncio.create_task(dashboard.do_action(
                "Run", "run", ["--name", "sample"], agent_name="sample",
                repository="repo", repository_path="/repos/sample"))
            await asyncio.sleep(0)
            release.set()
            self.assertEqual([0, 0], await asyncio.gather(first, second))
        self.assertEqual(1, executions)

    async def test_health_check_finishes_with_modern_maintenance(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        actions: list[tuple[str, str, list[str]]] = []
        markers: list[str] = []

        async def run_action(
            label: str,
            script: str,
            args: list[str],
            **_kwargs,
        ) -> int:
            actions.append((label, script, args))
            return 0

        with (
            mock.patch.object(dashboard, "do_action", side_effect=run_action),
            mock.patch.object(
                dashboard, "_smoketest_result_path", return_value=mock.Mock(
                    unlink=mock.Mock())),
            mock.patch.object(
                dashboard, "_current_smoketest_pass", return_value=True),
            mock.patch.object(dashboard, "system_health", return_value={
                "level": "ok", "tip": "healthy",
            }),
            mock.patch.object(
                dashboard, "_push_log", side_effect=markers.append),
        ):
            await dashboard.health_check()

        self.assertEqual(
            ("Health check", "internal", ["maintain"]),
            actions[-1],
        )
        self.assertNotIn(("Start", "start", ["--all"]), actions)
        self.assertEqual(
            ["Health check dashboard refresh complete"], markers)

    async def test_health_check_stops_after_smoketest_timeout(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        actions: list[tuple[str, str]] = []

        async def run_action(label, script, _args, **_kwargs):
            actions.append((label, script))
            return 124 if label == "Smoketest" else 0

        with (
            mock.patch.object(dashboard, "do_action", side_effect=run_action),
            mock.patch.object(
                dashboard, "_smoketest_result_path", return_value=mock.Mock(
                    unlink=mock.Mock())),
            mock.patch.object(dashboard, "_current_smoketest_pass") as verdict,
            mock.patch.object(dashboard, "_push_log") as push_log,
        ):
            await dashboard.health_check()
        self.assertEqual([
            ("Doctor", "doctor"),
            ("Smoketest", "smoketest"),
        ], actions)
        verdict.assert_not_called()
        push_log.assert_not_called()

    async def test_health_check_does_not_report_failed_maintenance(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        actions: list[str] = []

        async def run_action(label, _script, _args, **_kwargs):
            actions.append(label)
            return 1 if label == "Health check" else 0

        with (
            mock.patch.object(dashboard, "do_action", side_effect=run_action),
            mock.patch.object(
                dashboard, "_smoketest_result_path", return_value=mock.Mock(
                    unlink=mock.Mock())),
            mock.patch.object(
                dashboard, "_current_smoketest_pass", return_value=True),
            mock.patch.object(dashboard, "system_health") as health,
            mock.patch.object(dashboard, "_push_log") as push_log,
        ):
            await dashboard.health_check()
        self.assertEqual(["Doctor", "Smoketest", "Health check"], actions)
        health.assert_not_called()
        push_log.assert_not_called()

    async def test_shutdown_during_action_does_not_unpack_a_missing_result(
            self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        request = dashboard._ActionRequest(
            "Run", "run", ["--name", "sample"], "sample", None,
            asyncio.get_running_loop().create_future())
        with (
            mock.patch.object(
                dashboard.ng_run, "io_bound",
                new=mock.AsyncMock(return_value=None)),
            mock.patch.object(
                dashboard, "output_log", mock.Mock(), create=True),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await dashboard._execute_action(request)

    async def test_skipped_dashboard_run_is_logged_as_failure(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        request = dashboard._ActionRequest(
            "Run", "run", ["--name", "sample"], "sample", None,
            asyncio.get_running_loop().create_future())
        logged = mock.Mock()
        with (
            mock.patch.object(
                dashboard.ng_run, "io_bound",
                new=mock.AsyncMock(return_value=(
                    0,
                    json.dumps({
                        "ok": True,
                        "status": "skipped",
                        "run_id": "abc123",
                    }),
                    "skipped run transcript",
                ))),
            mock.patch.object(dashboard, "_log_action", logged),
            mock.patch.object(dashboard, "_refresh_views"),
            mock.patch.object(
                dashboard, "output_log", mock.Mock(), create=True),
        ):
            code = await dashboard._execute_action(request)
        self.assertEqual(-1, code)
        self.assertEqual("abc123", logged.call_args.kwargs["run_id"])
        self.assertEqual("skipped", logged.call_args.kwargs["run_status"])
        self.assertEqual(-1, logged.call_args.args[3])

    async def test_dashboard_run_parses_stdout_not_stderr(self) -> None:
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        request = dashboard._ActionRequest(
            "Run", "run", ["--name", "sample"], "sample", None,
            asyncio.get_running_loop().create_future())
        logged = mock.Mock()
        stdout = json.dumps({
            "ok": True,
            "status": "success",
            "run_id": "abc123",
        })
        with (
            mock.patch.object(
                dashboard.ng_run, "io_bound",
                new=mock.AsyncMock(return_value=(
                    0, stdout, stdout + "\nlauncher warning\n"))),
            mock.patch.object(dashboard, "_log_action", logged),
            mock.patch.object(dashboard, "_refresh_views"),
            mock.patch.object(
                dashboard, "output_log", mock.Mock(), create=True),
        ):
            code = await dashboard._execute_action(request)
        self.assertEqual(0, code)
        self.assertEqual("abc123", logged.call_args.kwargs["run_id"])
        self.assertIn("launcher warning", logged.call_args.args[4])


class TestDashboardProcessIdentity(unittest.TestCase):
    def test_dashboard_list_includes_clickable_url(self) -> None:
        from agents_live.cli.scripts import dashboards

        entry = {
            "port": 8232,
            "pid": 42,
            "start_token": 100,
            "repo": "C:/repo",
            "started": "2026-08-20T19:02:20+00:00",
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(dashboards, "running", return_value=[entry]),
            mock.patch.object(dashboards, "port_answers", return_value=True),
            mock.patch.object(sys, "argv", ["dashboards.py", "list"]),
            contextlib.redirect_stdout(stdout),
        ):
            code = dashboards.main()

        self.assertEqual(0, code)
        lines = stdout.getvalue().splitlines()
        self.assertIn("URL", lines[0])
        self.assertIn("http://127.0.0.1:8232", lines[1])

    def test_dashboard_stop_rejects_pid_reuse(self) -> None:
        from agents_live.cli.scripts import dashboards

        entry = {
            "port": 8232,
            "pid": 42,
            "start_token": 100,
            "repo": "C:/repo",
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(dashboards, "running", return_value=[entry]),
            mock.patch.object(
                dashboards.hostruntime, "process_start_token",
                return_value=101),
            mock.patch.object(dashboards.hostruntime, "terminate") as terminate,
            mock.patch.object(dashboards, "forget") as forget,
            mock.patch.object(
                sys, "argv", ["dashboards.py", "stop", "--port", "8232"]),
            contextlib.redirect_stdout(stdout),
        ):
            code = dashboards.main()
        self.assertEqual(1, code)
        self.assertIn("process_identity_unknown", stdout.getvalue())
        terminate.assert_not_called()
        forget.assert_not_called()

    def test_dashboard_stop_terminates_matching_process(self) -> None:
        from agents_live.cli.scripts import dashboards

        entry = {
            "port": 8232,
            "pid": 42,
            "start_token": 100,
            "repo": "C:/repo",
        }
        with (
            mock.patch.object(dashboards, "running", return_value=[entry]),
            mock.patch.object(
                dashboards.hostruntime, "process_start_token",
                return_value=100),
            mock.patch.object(dashboards.hostruntime, "terminate") as terminate,
            mock.patch.object(dashboards, "forget") as forget,
            mock.patch.object(dashboards, "port_answers", return_value=False),
            mock.patch.object(
                sys, "argv", ["dashboards.py", "stop", "--port", "8232"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = dashboards.main()
        self.assertEqual(0, code)
        terminate.assert_called_once_with(42)
        forget.assert_called_once_with(8232, 42)


class TestOwnershipMovesInBothDirections(TempRepository):
    """An agent can be claimed here or assigned to another runtime."""

    def _spec(self, *, enabled: bool = True):
        if enabled:
            (self.root / ".agents-live.toml").write_text(
                'ownership = "registry"\n', encoding="utf-8")
        self.skill("movable", [
            'agents-live.selector: "fake"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        return agent.load("movable", root=self.root)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        host = MemoryHost()
        previous = runtime.current()
        runtime.configure(host)
        try:
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
                mock.patch.object(lifecycle.repos, "load", return_value={
                    "repos": {"here": str(self.root)}, "default_repo": "here"}),
            ):
                code = start.main(argv)
        finally:
            runtime.configure(previous)
        return code, out.getvalue(), err.getvalue()

    def test_transfer_refuses_before_enable_without_mutation(self) -> None:
        self._spec(enabled=False)
        with (
            mock.patch.object(start.repos, "ensure_registered") as register,
            mock.patch.object(start.ownership, "current_owner_id") as identity,
            mock.patch.object(start.ownership, "set_owner") as set_owner,
        ):
            code, _, err = self._run(
                ["--name", "movable", "--transfer-here"])
        self.assertEqual(1, code)
        self.assertIn("agents-live ownership enable", err)
        register.assert_not_called()
        identity.assert_not_called()
        set_owner.assert_not_called()
        self.assertFalse((self.root / ".agents-live.toml").exists())

    def test_a_missing_backend_refuses_instead_of_pretending(self) -> None:
        self._spec()
        with mock.patch.object(
                start.ownership, "registry_available", return_value=False):
            code, _, err = self._run(["--name", "movable", "--transfer-here"])
        self.assertEqual(1, code)
        self.assertIn(ownership.ENTRY_POINT_GROUP, err)

    def test_an_identity_that_is_not_one_is_refused(self) -> None:
        self._spec()
        with mock.patch.object(
                start.ownership, "registry_available", return_value=True):
            code, _, err = self._run(
                ["--name", "movable", "--transfer-to", "just-a-hostname"])
        self.assertEqual(2, code)
        self.assertIn("hostname/runtime/uuid", err)

    def test_claiming_assigns_this_runtime_and_starts_it_here(self) -> None:
        spec = self._spec()
        assigned: list[tuple[str, str]] = []
        with (
            mock.patch.object(start.ownership, "registry_available",
                              return_value=True),
            mock.patch.object(start.ownership, "local_only",
                              return_value=False),
            mock.patch.object(start.ownership, "set_owner",
                              side_effect=lambda n, o, **_kwargs: assigned.append((n, o))) as set_owner,
            mock.patch.object(ownership, "load_owners", return_value={}),
        ):
            code, out, _ = self._run(["--name", "movable", "--transfer-here"])
        self.assertEqual(0, code)
        self.assertEqual([("movable", ownership.current_owner_id())], assigned)
        set_owner.assert_called_once_with(
            "movable", ownership.current_owner_id(), root=self.root)
        self.assertIn("Assigned 'movable'", out)
        self.assertIn(spec.identifier, state.load(self.root).agents)

    def test_assigning_elsewhere_withdraws_it_from_this_host(self) -> None:
        spec = self._spec()
        state.replace(self.root, {spec.identifier})
        other = f"otherhost/wsl/{'b' * 32}"
        with (
            mock.patch.object(start.ownership, "registry_available",
                              return_value=True),
            mock.patch.object(start.ownership, "local_only",
                              return_value=False),
            mock.patch.object(start.ownership, "set_owner"),
            mock.patch.object(ownership, "load_owners", return_value={}),
        ):
            code, out, err = self._run(
                ["--name", "movable", "--transfer-to", other])
        self.assertEqual(0, code)
        self.assertIn("otherhost/wsl", out)
        self.assertIn("withdrawn here", err)
        self.assertNotIn(spec.identifier, state.load(self.root).agents)

    def test_transfer_names_one_agent(self) -> None:
        self._spec()
        code, _, err = self._run(["--all", "--transfer-here"])
        self.assertEqual(2, code)
        self.assertIn("--name", err)


class TestOwnershipEnablement(TempRepository):
    def _run(self, command: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = ownership_command.main([command])
        return code, out.getvalue(), err.getvalue()

    def test_installed_backend_does_not_enable_ownership(self) -> None:
        backend = mock.Mock()
        backend.registry_file_exists.return_value = False
        with (
            mock.patch.object(ownership, "_backend", return_value=backend),
            self.assertRaisesRegex(
                ownership.OwnershipUnavailableError,
                "agents-live ownership enable",
            ),
        ):
            ownership.set_owner("sample", "*", root=self.root)
        code, out, _ = self._run("status")
        self.assertEqual(0, code)
        self.assertIn("local-only", out)
        backend.registry_file_exists.assert_not_called()
        backend.set_owner.assert_not_called()
        self.assertFalse((self.root / ".agents-live.toml").exists())

    def test_enable_requires_a_backend_without_rewriting_config(self) -> None:
        config = self.root / ".agents-live.toml"
        original = 'agent_directories = ["Extra"]\n'
        config.write_text(original, encoding="utf-8")
        with mock.patch.object(ownership, "_backend", return_value=None):
            code, _, err = self._run("enable")
        self.assertEqual(1, code)
        self.assertIn(ownership.ENTRY_POINT_GROUP, err)
        self.assertEqual(original, config.read_text(encoding="utf-8"))

    def test_malformed_registry_refuses_before_rewriting_config(self) -> None:
        config = self.root / ".agents-live.toml"
        original = 'agent_directories = ["Extra"]\n'
        config.write_text(original, encoding="utf-8")
        backend = mock.Mock()
        backend.registry_file_exists.return_value = True
        backend.load_owners.side_effect = ownership.OwnershipUnavailableError(
            "owners document malformed")
        with mock.patch.object(ownership, "_backend", return_value=backend):
            code, _, err = self._run("enable")
        self.assertEqual(1, code)
        self.assertIn("malformed", err)
        self.assertEqual(original, config.read_text(encoding="utf-8"))

    def test_status_reports_declared_but_unavailable(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            'ownership = "registry"\n', encoding="utf-8")
        with mock.patch.object(ownership, "_backend", return_value=None):
            code, out, _ = self._run("status")
        self.assertEqual(1, code)
        self.assertIn("registry declared but unavailable", out)

    def test_enable_validates_then_writes_the_declaration(self) -> None:
        backend = mock.Mock()
        backend.registry_file_exists.return_value = False
        with mock.patch.object(ownership, "_backend", return_value=backend):
            code, out, _ = self._run("enable")
        self.assertEqual(0, code)
        self.assertIn("Registry ownership enabled", out)
        self.assertEqual("registry", ownership.mode(self.root))
        backend.registry_file_exists.assert_called_once_with(root=self.root)
        backend.load_owners.assert_not_called()

    def test_existing_registry_is_validated_and_remains_enabled(self) -> None:
        config = self.root / ".agents-live.toml"
        original = 'ownership = "registry"\n'
        config.write_text(original, encoding="utf-8")
        backend = mock.Mock()
        backend.registry_file_exists.return_value = True
        backend.load_owners.return_value = {"sample": "*"}
        with mock.patch.object(ownership, "_backend", return_value=backend):
            code, out, _ = self._run("enable")
        self.assertEqual(0, code)
        self.assertIn("already enabled", out)
        self.assertEqual(original, config.read_text(encoding="utf-8"))
        backend.load_owners.assert_called_once_with(
            rate_limit_secs=0, root=self.root)


class TestRunsAreRecordedUnderOneName(TempRepository):
    """However an agent is named on the command line, it has one history.

    `run --name <display name>` recorded under that name while the
    scheduled form recorded under the identifier, so an agent accumulated
    two log files. Identifier-keyed readers saw only one of them, which
    hid manual runs from the dashboard's history, cost, and health
    columns, and made the older file decide a row's colour.
    """

    def _spec(self):
        self.skill("named", [
            'agents-live.selector: "fake/echo"',
            'agents-live.schedule: "0 8 * * *"',
        ])
        return agent.load("named", root=self.root)

    def _dispatch(self, requested: str):
        runner = mock.Mock()
        runner.run_child.return_value = ChildResult(
            ("fake",), 0, '{"text":"done"}', "")
        return dispatch(
            Firing(requested, str(self.root), "manual"), runner=runner)

    def test_a_run_named_by_display_name_lands_in_the_identifier_log(self) -> None:
        spec = self._spec()
        outcome = self._dispatch("named")
        self.assertTrue(outcome.ok, outcome)
        logs = paths.repo_state_dir(self.root) / "logs"
        self.assertEqual(
            [f"{spec.identifier}.jsonl"],
            sorted(path.name for path in logs.glob("named*.jsonl")))
        records = obs.load(obs.files(logs))
        self.assertEqual(
            {spec.identifier},
            {r["agent_name"] for r in records if r.get("phase") == "done"})

    def test_both_invocation_forms_share_one_history(self) -> None:
        spec = self._spec()
        self._dispatch("named")
        self._dispatch(spec.identifier)
        logs = paths.repo_state_dir(self.root) / "logs"
        self.assertEqual(
            [f"{spec.identifier}.jsonl"],
            sorted(path.name for path in logs.glob("named*.jsonl")))
        done = [r for r in obs.load(obs.files(logs))
                if r.get("phase") == "done"]
        self.assertEqual(2, len(done))


    def test_the_post_processor_is_handed_the_value_not_the_prose(self) -> None:
        """A provider wraps its answer in prose and a session footer, and
        the processor is the reason the value was extracted at all.
        Handing it the surrounding text made it fail on 24,715 bytes that
        do not begin with JSON."""
        self.skill("contracted", [
            'agents-live.selector: "fake/echo"',
            'agents-live.schedule: "0 8 * * *"',
            'agents-live.post-processor: "record.sh"',
        ])
        directory = self.root / "Agents" / "contracted"
        script = directory / "record.sh"
        script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
        script.chmod(0o755)

        answer = {"files": [{"path": "out.md", "content": "line one\nline two"}]}
        wrapped = (
            "Here is the result:\n\n" + json.dumps(answer)
            + "\n\nAI Credits 1.2 (3s)\nResume copilot --resume=abc\n")
        runner = mock.Mock()
        runner.run_child.side_effect = [
            ChildResult(("fake",), 0, json.dumps({"text": wrapped}), ""),
            ChildResult(("sh",), 0, "", ""),
        ]
        outcome = dispatch(
            Firing("contracted", str(self.root), "manual"), runner=runner)
        self.assertTrue(outcome.ok, outcome)
        handed = runner.run_child.call_args_list[-1].kwargs["input_text"]
        self.assertEqual(answer, json.loads(handed))


class TestPromptFitsTheHostCommandLine(unittest.TestCase):
    """A prompt that outgrows the command line must say so plainly.

    POSIX allows a command line into the megabytes, so a definition
    that grew past 32767 characters ran there for months and failed on
    Windows as `WinError 206`, "the filename or extension is too long"
    - naming the one thing that was not wrong.
    """

    def test_a_posix_host_imposes_no_limit(self) -> None:
        with mock.patch.object(hostruntime, "_IS_WINDOWS", False):
            self.assertIsNone(
                hostruntime.command_line_overflow(["copilot", "-p", "x" * 90000]))

    def test_a_windows_host_reports_how_far_over_the_prompt_is(self) -> None:
        with mock.patch.object(hostruntime, "_IS_WINDOWS", True):
            self.assertIsNone(
                hostruntime.command_line_overflow(["copilot", "-p", "hello"]))
            overflow = hostruntime.command_line_overflow(
                ["copilot", "-p", "x" * 40000])
            self.assertIsNotNone(overflow)
            self.assertGreater(overflow, 0)

    def test_the_failure_names_the_prompt_rather_than_a_filename(self) -> None:
        """The remedy is the definition's, so the message must point there.

        This drives the real spawn seam every provider uses, including
        plugin providers, because the legacy agent path is not the one
        that reported the original failure.
        """
        from agents_live.runtime.hosts.processes import LocalChildRunner

        with mock.patch.object(hostruntime, "_IS_WINDOWS", True):
            result = LocalChildRunner().run_child(["copilot", "-p", "x" * 70000])
        self.assertNotEqual(0, result.returncode)
        self.assertIn("prompt too large", result.stderr)
        self.assertIn("70000", result.stderr)

    def test_a_prompt_that_fits_still_reaches_the_child(self) -> None:
        """The guard must not stand between ordinary runs and their work."""
        from agents_live.runtime.hosts.processes import LocalChildRunner

        result = LocalChildRunner().run_child([sys.executable, "-c", "print(42)"])
        self.assertEqual(0, result.returncode)
        self.assertEqual("42", result.stdout.strip())


class TestInstallationGenerations(unittest.TestCase):
    """Where an installation may write, and what it may never guess.

    #334 replaces the in-place `uv tool upgrade` with side-by-side
    generations and a pointer, and #369 supplies the ownership rules
    that decide who may move that pointer. The primitives land before
    the behavior does, so these hold the decisions the later steps
    depend on: a generation name that cannot escape the installation
    root, a pointer that refuses rather than guesses, an owner read from
    what is executing, a plan that never disturbs the active generation
    before the pointer moves, and a collector that keeps the rollback.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "install"
        patched = mock.patch.dict(
            os.environ,
            {deploy.layout.ENV_INSTALL_ROOT: str(self.root)},
        )
        patched.start()
        self.addCleanup(patched.stop)
        path_patch = mock.patch.object(
            install_release, "_expose_command_root")
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def _generation(self, name: str) -> Path:
        directory = deploy.layout.generation_dir(name)
        (directory / "bin").mkdir(parents=True, exist_ok=True)
        return directory

    def _activate_generation(self, name: str) -> deploy.generation.Generation:
        try:
            built = deploy.generation.load(name)
        except deploy.generation.GenerationError:
            built = deploy.generation.build(
                name,
                populate=lambda staging: (staging / "bin").mkdir(parents=True),
                validate=lambda _staging: None,
            )
        deploy.generation.activate(built)
        return built

    def _uv_environment(self) -> Path:
        environment = Path(self.temporary.name) / "uv-tools" / "agents-live"
        (environment / "bin").mkdir(parents=True, exist_ok=True)
        (environment / deploy.ownership.RECEIPT).write_text(
            "[tool]\n", encoding="utf-8")
        return environment / "bin" / "agents-live"

    def _package_wheel(self, version: str) -> Path:
        wheel = (
            Path(self.temporary.name)
            / f"agents_live-{version}-py3-none-any.whl"
        )
        dist_info = f"agents_live-{version}.dist-info"
        files = {
            "agents_live/__init__.py": f'__version__ = "{version}"\n',
            "agents_live/cli/__init__.py": (
                "from agents_live import __version__\n"
                "def main():\n"
                "    import sys\n"
                "    if '--version' in sys.argv:\n"
                "        print(f'agents-live {__version__}')\n"
                "    else:\n"
                "        print('usage: agents-live [options]')\n"
            ),
            "agents_live/cli/__main__.py": (
                "from . import main\n"
                "main()\n"
            ),
            f"{dist_info}/METADATA": (
                "Metadata-Version: 2.1\n"
                "Name: agents-live\n"
                f"Version: {version}\n"
                "Requires-Python: >=3.12\n"
            ),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\n"
                "Generator: agents-live-tests\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            ),
            f"{dist_info}/entry_points.txt": (
                "[console_scripts]\n"
                "agents-live = agents_live.cli:main\n"
                "al = agents_live.cli:main\n"
            ),
        }
        record = "\n".join(f"{name},," for name in files)
        record += f"\n{dist_info}/RECORD,,\n"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr(f"{dist_info}/RECORD", record)
        return wheel

    def test_windows_generation_path_precedes_retired_tool_bins(self) -> None:
        """The stable command must win lookup after uv migration."""
        command_root = Path("C:/Users/example/AppData/Local/agents-live/current/Scripts")
        key = mock.MagicMock()
        key.__enter__.return_value = key
        registry = mock.Mock()
        registry.HKEY_CURRENT_USER = object()
        registry.REG_EXPAND_SZ = 2
        registry.CreateKey.return_value = key
        registry.QueryValueEx.return_value = (
            "C:\\Users\\example\\.local\\bin;"
            f"{command_root};C:\\Windows",
            registry.REG_EXPAND_SZ,
        )
        with (
            mock.patch.object(hostruntime, "_IS_WINDOWS", True),
            mock.patch.dict(sys.modules, {"winreg": registry}),
        ):
            hostruntime.expose_user_path_directory(command_root)
        written = registry.SetValueEx.call_args.args[4].split(";")
        self.assertEqual(str(command_root), written[0])
        self.assertEqual(1, written.count(str(command_root)))

    def test_a_throwaway_install_root_never_touches_the_user_path(self) -> None:
        """A gate that installs into a temp root must leave no residue.

        POSIX contains a profile write by redirecting HOME. The Windows
        environment lives in the registry, which no environment variable
        redirects, so a readiness run left a permanent PATH entry pointing
        at a deleted temporary directory until the contract it already
        declared was actually honored.
        """
        command_root = Path("C:/Temp/throwaway/install/current/Scripts")
        registry = mock.Mock()
        registry.HKEY_CURRENT_USER = object()
        registry.REG_EXPAND_SZ = 2

        with (
            mock.patch.object(hostruntime, "_IS_WINDOWS", True),
            mock.patch.dict(sys.modules, {"winreg": registry}),
            mock.patch.dict(
                os.environ, {hostruntime.ENV_NO_PATH_UPDATE: "1"}),
        ):
            hostruntime.expose_user_path_directory(command_root)
            hostruntime.remove_user_path_directory(command_root)

        registry.CreateKey.assert_not_called()
        registry.OpenKey.assert_not_called()
        registry.SetValueEx.assert_not_called()

    def test_a_generation_name_cannot_escape_the_installation_root(self) -> None:
        """A staged generation writes wherever its name resolves.

        The name arrives from a version string, a release tag, or an
        operator's argument, and it is joined to a path. A name that
        climbs out of `versions/` is only observable afterwards, as a
        write somewhere the installer never meant to touch.
        """
        for name in ("", "   ", "..", "../evil", "a/b", "a\\b", "~",
                     "C:\\windows", ".hidden", "6.5.0/../..", "v 1"):
            with self.subTest(name=name):
                with self.assertRaises(deploy.layout.LayoutError):
                    deploy.layout.generation_dir(name)
        for name in ("6.5.0", "6.6.0rc1", "6.5.0+local.1", "2026.08.23-dev"):
            with self.subTest(name=name):
                directory = deploy.layout.generation_dir(name)
                self.assertEqual(
                    deploy.layout.generations_root(), directory.parent)
                self.assertIn(self.root, directory.parents)

    def test_activation_switches_only_the_stable_current_directory(
            self) -> None:
        """PATH stays fixed while current selects one immutable generation.

        Windows locks a running image, so activation must never rewrite that
        image. Rollback is the same directory-link switch in reverse.
        """
        old = self._activate_generation("6.5.0")
        new = self._activate_generation("6.6.0")
        self.assertEqual("6.6.0", deploy.pointer.read().generation)
        self.assertEqual(new.path.resolve(), deploy.layout.current_path().resolve())
        self.assertFalse((self.root / "current.json").exists())

        deploy.generation.activate(old)
        self.assertEqual("6.5.0", deploy.pointer.read().generation)
        self.assertEqual(old.path.resolve(), deploy.layout.current_path().resolve())

    def test_bake_commits_are_distinct_immutable_generations(self) -> None:
        """Local-version commit suffixes prevent bake install collisions."""
        first = self._activate_generation("6.6.1.dev0+gabc1234")
        second = self._activate_generation("6.6.1.dev0+gdef5678")

        self.assertNotEqual(first.path, second.path)
        self.assertTrue(first.path.is_dir())
        self.assertTrue(second.path.is_dir())
        self.assertEqual(
            "6.6.1.dev0+gdef5678", deploy.pointer.read().generation)
        self.assertEqual(
            second.path.resolve(), deploy.layout.current_path().resolve())

    def test_an_invalid_current_target_is_refused_not_guessed(
            self) -> None:
        """Guessing is how a host runs a generation nobody activated.

        With generations on disk, an implementation that recovered by picking
        the newest directory would look healthy and run something unactivated.
        """
        self._generation("6.5.0")
        self._generation("6.6.0")
        current = deploy.layout.current_path()
        current.mkdir(parents=True)
        with self.assertRaises(deploy.pointer.PointerError) as invalid:
            deploy.pointer.read()
        self.assertEqual(deploy.pointer.INVALID, invalid.exception.reason)

        found, state, detail = deploy.pointer.status()
        self.assertIsNone(found)
        self.assertEqual(deploy.pointer.INVALID, state)
        self.assertNotIn("6.6.0", detail)

    def test_the_running_image_decides_which_channel_owns_the_runtime(
            self) -> None:
        """An upgrade replaces an artifact, so the artifact decides.

        A recorded owner is a claim; what is executing is the fact. The
        uv answer comes from a receipt beside the running image rather
        than from asking uv, because a command that must report before
        it acts cannot afford a subprocess that may hang.
        """
        generation = self._generation("6.5.0")
        deploy.generation._replace_current(generation, root=self.root)

        managed = deploy.ownership.describe(
            executable=generation / "bin" / "agents-live")
        self.assertEqual(deploy.ownership.SELF, managed.owner)
        self.assertEqual("6.5.0", managed.generation)
        self.assertFalse(managed.stale)
        self.assertIsNone(deploy.ownership.refusal(managed))

        uv_installed = deploy.ownership.describe(executable=self._uv_environment())
        self.assertEqual(deploy.ownership.UV, uv_installed.owner)
        self.assertIsNone(uv_installed.generation)

        elsewhere = deploy.ownership.describe(
            executable=Path(self.temporary.name) / "checkout" / "bin" / "python")
        self.assertEqual(deploy.ownership.UNMANAGED, elsewhere.owner)

    def test_a_second_owner_is_reported_before_two_channels_can_race(
            self) -> None:
        """Two artifacts can answer to `agents-live` on one PATH.

        If both believe they may replace the runtime, an operator's
        `uv tool upgrade` rewrites a shim whose generation Agents Live
        owns, and the partial-replacement failure returns through a
        different door (#369).
        """
        self._generation("6.5.0")
        deploy.generation._replace_current(
            deploy.layout.generation_dir("6.5.0"), root=self.root)

        contested = deploy.ownership.describe(executable=self._uv_environment())
        self.assertTrue(contested.contested)
        self.assertIn("only one", contested.detail)
        self.assertIsNotNone(deploy.ownership.refusal(contested))
        plan = deploy.plan.plan_activation(
            target="6.6.0", current="6.5.0", installation=contested)
        self.assertFalse(plan.ok)
        self.assertEqual((), plan.steps)

    def test_nothing_disturbs_the_active_generation_before_the_pointer_moves(
            self) -> None:
        """Staging beside the active generation is the whole point.

        An in-place rewrite is what leaves an installation on neither
        version, so every step that runs before activation must be able
        to fail without an operator noticing, and activation itself must
        be the single reversible act.
        """
        plan = deploy.plan.plan_activation(target="6.6.0", current="6.5.0")
        self.assertTrue(plan.ok)
        activate = plan.index(deploy.plan.ACTIVATE)
        self.assertEqual(
            [], [step.name for step in plan.steps[:activate]
                 if step.touches_active])
        self.assertTrue(plan.steps[activate].reversible)
        self.assertEqual("6.5.0", plan.rollback_to)
        self.assertLess(activate, plan.index(deploy.plan.VERIFY))
        self.assertLess(plan.index(deploy.plan.STAGE), activate)

    def test_failed_validation_leaves_the_active_generation_untouched(
            self) -> None:
        """A broken candidate stays inert and rebuildable beside the active.

        The record, not the directory name, is what makes a generation
        usable. An interrupted build therefore has to leave something that
        no listing reports and the next attempt discards, or immutability
        would refuse to rewrite it and wedge the version permanently.
        """
        self._activate_generation("6.5.0")

        def populate(target: Path) -> None:
            target.mkdir(parents=True)
            (target / "runtime").write_text("candidate", encoding="utf-8")

        def reject(_target: Path) -> None:
            raise RuntimeError("smoke check failed")

        with self.assertRaises(deploy.generation.GenerationError) as failed:
            deploy.generation.build(
                "6.6.0", populate=populate, validate=reject)
        self.assertIn("smoke check failed", str(failed.exception))
        unsealed = deploy.layout.generation_dir("6.6.0")
        self.assertTrue(unsealed.is_dir())
        self.assertFalse(deploy.layout.is_sealed(unsealed))
        self.assertNotIn("6.6.0", deploy.layout.installed_generations())
        with self.assertRaises(deploy.generation.GenerationError):
            deploy.generation.load("6.6.0")
        self.assertEqual("6.5.0", deploy.pointer.read().generation)

        def validate(target: Path) -> None:
            self.assertFalse((target / "obsolete").exists())
            self.assertEqual(
                "candidate",
                (target / "runtime").read_text(encoding="utf-8"),
            )

        (unsealed / "obsolete").write_text(
            "from failed attempt", encoding="utf-8")
        built = deploy.generation.build(
            "6.6.0", populate=populate, validate=validate)
        self.assertEqual(
            deploy.layout.generation_dir("6.6.0"), built.path)
        self.assertTrue(deploy.layout.is_sealed(built.path))
        self.assertIn("6.6.0", deploy.layout.installed_generations())
        self.assertEqual("6.5.0", deploy.pointer.read().generation)

        deploy.generation.activate(built)
        self.assertEqual("6.6.0", deploy.pointer.read().generation)

    def test_a_sealed_generation_is_never_rebuilt(self) -> None:
        """Immutability keys on the record, so a sealed version is refused."""
        built = deploy.generation.build(
            "6.6.0",
            populate=lambda path: path.mkdir(parents=True),
            validate=lambda _path: None,
        )
        self.assertTrue(deploy.layout.is_sealed(built.path))

        with self.assertRaises(deploy.generation.GenerationError) as refused:
            deploy.generation.build(
                "6.6.0",
                populate=lambda path: path.mkdir(parents=True, exist_ok=True),
                validate=lambda _path: None,
            )
        self.assertIn("already installed", str(refused.exception))

    def test_activation_refuses_unvalidated_or_damaged_installation_state(
            self) -> None:
        """Activation cannot skip validation or overwrite an invalid selection."""
        incomplete = self._generation("6.6.0")
        with self.assertRaises(deploy.generation.GenerationError):
            deploy.generation.load("6.6.0")
        self.assertFalse(deploy.layout.current_path().exists())

        shutil.rmtree(incomplete)
        built = deploy.generation.build(
            "6.6.0",
            populate=lambda staging: staging.mkdir(parents=True),
            validate=lambda _staging: None,
        )
        deploy.layout.current_path().mkdir()
        with self.assertRaises(deploy.generation.GenerationError) as refused:
            deploy.generation.activate(built)
        self.assertIn("points outside versions", str(refused.exception))
        self.assertTrue(deploy.layout.current_path().is_dir())

    def test_hidden_install_seam_builds_and_activates_an_exact_wheel(
            self) -> None:
        """The CLI boundary drives real uv staging and staged-CLI validation."""
        version = "9.9.9"
        wheel = self._package_wheel(version)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agents_live.cli",
                "install-generation",
                version,
                "--from",
                str(wheel),
                "--install-root",
                str(self.root),
                "--activate",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            f"built and activated generation {version}", completed.stdout)
        self.assertEqual(version, deploy.pointer.read().generation)
        installed = deploy.generation.load(version)
        self.assertEqual(
            install_generation.local_provenance(wheel),
            installed.provenance,
        )
        interpreter = (
            hostruntime.executable_dir(installed.path)
            / hostruntime.executable_filename(hostruntime.interpreter_name())
        )
        reported = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                "from agents_live import __version__; print(__version__)",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, reported.returncode, reported.stderr)
        self.assertEqual(version, reported.stdout.strip())
        launcher = (
            hostruntime.executable_dir(installed.path)
            / hostruntime.executable_filename("agents-live")
        )
        launched = subprocess.run(
            [str(launcher), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        self.assertEqual(f"agents-live {version}", launched.stdout.strip())

    def test_official_release_metadata_authenticates_exact_wheel_bytes(
            self) -> None:
        """The release API digest, not a configured index, authenticates bytes."""
        version = "9.8.7"
        content = b"exact wheel bytes"
        digest = hashlib.sha256(content).hexdigest()
        artifact_url = (
            "https://github.com/johnshew/agents-live/releases/download/"
            f"v{version}/agents_live-{version}-py3-none-any.whl"
        )
        metadata = json.dumps({
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": f"agents_live-{version}-py3-none-any.whl",
                "state": "uploaded",
                "browser_download_url": artifact_url,
                "digest": f"sha256:{digest}",
                "size": len(content),
            }],
        }).encode()
        requested: list[str] = []

        class Response(io.BytesIO):
            def __init__(self, value: bytes, url: str):
                super().__init__(value)
                self.url = url

            def geturl(self) -> str:
                return self.url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def opener(request, **_kwargs):
            requested.append(request.full_url)
            if request.full_url.startswith(deploy.release_artifact.API_ROOT):
                return Response(metadata, "https://api.github.com/release")
            return Response(
                content,
                "https://release-assets.githubusercontent.com/artifact",
            )

        latest = deploy.release_artifact.resolve(opener=opener)
        artifact = deploy.release_artifact.resolve(version, opener=opener)
        self.assertEqual(artifact, latest)
        with deploy.release_artifact.verified_download(
                artifact, root=self.root, opener=opener) as wheel:
            self.assertEqual(content, wheel.read_bytes())
            self.assertEqual(artifact.name, wheel.name)
        self.assertEqual(
            [
                "https://api.github.com/repos/johnshew/agents-live/releases/latest",
                "https://api.github.com/repos/johnshew/agents-live/releases/"
                f"tags/v{version}",
                artifact_url,
            ],
            requested,
        )
        self.assertEqual([], list(self.root.glob(".artifact-*")))

    def test_explicit_prerelease_authenticates_only_prerelease_metadata(
            self) -> None:
        """A bake is opt-in and cannot be confused with a stable release."""
        version = "6.7.0.dev0+g7b01b2d"
        name = f"agents_live-{version}-py3-none-any.whl"
        artifact_url = (
            "https://github.com/johnshew/agents-live/releases/download/"
            f"v{version}/{name}"
        ).replace("+", "%2B")
        metadata = {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": True,
            "assets": [{
                "name": name,
                "state": "uploaded",
                "browser_download_url": artifact_url,
                "digest": f"sha256:{'0' * 64}",
                "size": 1,
            }],
        }

        class Response(io.BytesIO):
            def __init__(self, prerelease: bool):
                metadata["prerelease"] = prerelease
                super().__init__(json.dumps(metadata).encode())

            def geturl(self) -> str:
                return "https://api.github.com/release"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        artifact = deploy.release_artifact.resolve(
            version, opener=lambda *_args, **_kwargs: Response(True))
        self.assertEqual(version, artifact.version)
        self.assertEqual(artifact_url, artifact.url)
        with self.assertRaises(deploy.release_artifact.ReleaseArtifactError):
            deploy.release_artifact.resolve(
                version, opener=lambda *_args, **_kwargs: Response(False))

    def test_release_download_fails_closed_on_a_checksum_mismatch(self) -> None:
        """Corrupt bytes never reach uv or leave an installable partial artifact."""
        content = b"tampered"
        artifact = deploy.release_artifact.ReleaseArtifact(
            "9.8.7",
            "agents_live-9.8.7-py3-none-any.whl",
            "https://github.com/johnshew/agents-live/releases/download/"
            "v9.8.7/agents_live-9.8.7-py3-none-any.whl",
            "0" * 64,
            len(content),
        )

        class Response(io.BytesIO):
            def geturl(self) -> str:
                return "https://release-assets.githubusercontent.com/artifact"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with self.assertRaises(
                deploy.release_artifact.ReleaseArtifactError) as failed:
            with deploy.release_artifact.verified_download(
                    artifact,
                    root=self.root,
                    opener=lambda *_args, **_kwargs: Response(content)):
                self.fail("unverified bytes were yielded")
        self.assertIn("checksum mismatch", str(failed.exception))
        self.assertEqual([], list(self.root.glob(".artifact-*")))
        self.assertFalse(deploy.layout.current_path().exists())

    def test_verified_release_cli_builds_the_generation_from_a_local_wheel(
            self) -> None:
        """The network seam hands exact bytes to the landed generation builder."""
        version = "9.7.6"
        wheel = self._package_wheel(version)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        artifact = deploy.release_artifact.ReleaseArtifact(
            version,
            wheel.name,
            "https://github.com/johnshew/agents-live/releases/download/"
            f"v{version}/{wheel.name}",
            digest,
            wheel.stat().st_size,
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(
                deploy.release_artifact, "resolve", return_value=artifact),
            mock.patch.object(
                deploy.release_artifact,
                "verified_download",
                return_value=contextlib.nullcontext(wheel),
            ),
            mock.patch.dict(
                os.environ,
                {"UV_INDEX_URL": "http://127.0.0.1:9/simple"},
            ),
            mock.patch.object(
                sys,
                "argv",
                [
                    "agents-live install-release",
                    version,
                    "--install-root",
                    str(self.root),
                    "--activate",
                ],
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = install_release.main()
        self.assertEqual(0, code)
        self.assertIn(f"sha256:{digest}", stdout.getvalue())
        self.assertEqual(version, deploy.pointer.read().generation)
        installed = deploy.generation.load(version)
        self.assertEqual(
            deploy.generation.Provenance("github-release", wheel.name, digest),
            installed.provenance,
        )
        launcher = (
            hostruntime.executable_dir(installed.path)
            / hostruntime.executable_filename("agents-live")
        )
        launched = subprocess.run(
            [str(launcher), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        self.assertEqual(f"agents-live {version}", launched.stdout.strip())
        self.assertIn(
            f"stable command: {deploy.layout.command_path()}", stdout.getvalue())
        self.assertEqual(
            deploy.ownership.SELF, deploy.ownership.read_record())

        with (
            mock.patch.object(
                deploy.release_artifact, "resolve", return_value=artifact),
            mock.patch.object(
                deploy.release_artifact, "verified_download") as download,
            mock.patch.object(
                sys,
                "argv",
                [
                    "agents-live install-release",
                    version,
                    "--install-root",
                    str(self.root),
                    "--activate",
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = install_release.main()
        self.assertEqual(0, code)
        download.assert_not_called()

    def test_verified_release_refuses_a_damaged_existing_generation(self) -> None:
        """Matching provenance cannot authorize a payload that no longer runs."""
        version = "9.7.5"
        wheel = self._package_wheel(version)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        artifact = deploy.release_artifact.ReleaseArtifact(
            version,
            wheel.name,
            "https://github.com/johnshew/agents-live/releases/download/"
            f"v{version}/{wheel.name}",
            digest,
            wheel.stat().st_size,
        )
        provenance = deploy.generation.Provenance(
            "github-release", wheel.name, digest)
        built = install_generation.install(
            version, source=wheel, root=self.root, provenance=provenance)
        self._activate_generation("6.5.0")
        install_generation.executable(built).unlink()

        stderr = io.StringIO()
        with (
            mock.patch.object(
                deploy.release_artifact, "resolve", return_value=artifact),
            mock.patch.object(
                deploy.release_artifact, "verified_download") as download,
            mock.patch.object(
                sys,
                "argv",
                [
                    "agents-live install-release",
                    version,
                    "--install-root",
                    str(self.root),
                    "--activate",
                ],
            ),
            contextlib.redirect_stderr(stderr),
        ):
            code = install_release.main()

        self.assertEqual(1, code)
        self.assertIn("is damaged: missing launcher", stderr.getvalue())
        self.assertEqual("6.5.0", deploy.pointer.read().generation)
        download.assert_not_called()

    def test_failed_path_exposure_rolls_back_active_generation_ownership(
            self) -> None:
        """Activation does not half-land when the host refuses PATH.

        Exposure is the last irreversible-looking step, and a host that
        refuses it would otherwise leave a selected generation recorded
        as self-managed but unreachable by name.
        """
        version = "9.7.6"
        wheel = self._package_wheel(version)
        with (
            mock.patch.object(
                install_release, "_expose_command_root",
                side_effect=OSError("registry refused")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = install_release.main(
                [version, "--activate", "--wheel", str(wheel)])

        self.assertEqual(1, code)
        self.assertEqual(deploy.pointer.MISSING, deploy.pointer.status()[1])
        self.assertIsNone(deploy.ownership.read_record())
        self.assertEqual((version,), deploy.layout.installed_generations())

    def test_processes_on_the_active_generation_do_not_block_an_upgrade(
            self) -> None:
        """Upgrade stops being refusable, which is the operator payoff.

        A watcher holding the active generation keeps executing it and
        hands off at its next idle version check (#188). A process
        holding the *target* directory is different: staging would
        rewrite a directory that is executing, which is the failure this
        model exists to remove.
        """
        running = deploy.plan.plan_activation(
            target="6.6.0", current="6.5.0",
            holders={"6.5.0": ("watcher 'nightly' (pid 4242)",)})
        self.assertTrue(running.ok)
        self.assertEqual((), running.quiesce)
        self.assertTrue(any("6.5.0" in note for note in running.notes))

        reinstalling = deploy.plan.plan_activation(
            target="6.6.0", current="6.5.0",
            holders={"6.6.0": ("dashboard on port 8080 (pid 77)",)})
        self.assertFalse(reinstalling.ok)
        self.assertIn("dashboard on port 8080", reinstalling.refusal)

    def test_the_collector_keeps_the_rollback_and_never_removes_what_runs(
            self) -> None:
        """A collector that races an activation is the expensive bug.

        The active generation is what every launcher resolves to, the
        retained previous one is the rollback, and a held one is
        executing - on Windows its removal would half-finish.
        """
        collectable = deploy.plan.collectable(
            ("6.3.0", "6.4.0", "6.5.0", "6.6.0"),
            active="6.6.0",
            held={"6.3.0": ("watcher 'nightly' (pid 4242)",)},
            order=("6.3.0", "6.4.0", "6.5.0", "6.6.0"))
        self.assertEqual(("6.4.0",), collectable)
        self.assertEqual(
            (), deploy.plan.collectable(("6.6.0",), active="6.6.0"))

    def test_every_way_a_deployment_can_stop_half_way_has_an_answer(
            self) -> None:
        """#369 asks for the failure semantics, not a best effort.

        A state with no stated recovery is one an operator meets for the
        first time on a broken host.
        """
        for state_name in deploy.plan.states():
            with self.subTest(state=state_name):
                found = deploy.plan.recovery(state_name)
                self.assertIsNotNone(found)
                self.assertTrue(found.action and found.detail)
        self.assertEqual("verify", deploy.plan.recovery("activated").action)
        self.assertEqual("rollback", deploy.plan.recovery("unverified").action)
        self.assertEqual(
            "discard", deploy.plan.recovery("staging").action)
        self.assertIsNone(deploy.plan.recovery("invented-state"))

    def test_self_managed_upgrade_switches_generations_through_current(
            self) -> None:
        """Upgrade changes only current and retains the old generation."""
        old = deploy.generation.build(
            "9.7.5",
            populate=lambda staging: staging.mkdir(parents=True),
            validate=lambda _staging: None,
        )
        deploy.generation.activate(old)
        deploy.ownership.write_record(deploy.ownership.SELF)
        wheel = self._package_wheel("9.7.6")
        self.assertEqual(0, upgrade._upgrade_self_managed(wheel))
        self.assertEqual("9.7.6", deploy.pointer.read().generation)
        self.assertEqual(
            ("9.7.5", "9.7.6"), deploy.layout.installed_generations())
        installed = deploy.generation.load("9.7.6")
        self.assertEqual("local-artifact", installed.provenance.channel)
        self.assertEqual(
            hashlib.sha256(wheel.read_bytes()).hexdigest(),
            installed.provenance.sha256,
        )
        self.assertEqual(
            installed.path.resolve(), deploy.layout.current_path().resolve())

        self.assertEqual(0, upgrade._upgrade_self_managed(wheel))
        self.assertEqual(
            ("9.7.5", "9.7.6"), deploy.layout.installed_generations())

    def test_doctor_validates_current_commands_and_generation(self) -> None:
        """Ownership alone is not health when a generated command is gone."""
        wheel = self._package_wheel("9.7.6")
        built = install_generation.install("9.7.6", source=wheel, root=self.root)
        deploy.generation.activate(built)
        deploy.ownership.write_record(deploy.ownership.SELF)
        installation = deploy.ownership.describe(
            executable=hostruntime.executable_dir(built.path)
            / hostruntime.executable_filename("python"))

        with mock.patch.object(
                deploy.ownership, "describe", return_value=installation):
            healthy = doctor._installation_check()
            deploy.layout.command_path("al").unlink()
            damaged = doctor._installation_check()

        self.assertTrue(healthy["ok"])
        self.assertIn("current selection", healthy["detail"])
        self.assertFalse(damaged["ok"])
        self.assertIn("missing stable command", damaged["detail"])

    def test_self_managed_uninstall_removes_the_owned_root(self) -> None:
        """The self-managed channel never delegates removal to uv."""
        marker = self.root / "versions" / "9.7.6" / "installed"
        marker.parent.mkdir(parents=True)
        marker.write_text("yes", encoding="utf-8")

        with (
            mock.patch.object(uninstall, "_remove_command_exposure"),
            mock.patch.object(
                uninstall.hostruntime, "id", return_value=hostruntime.LINUX),
        ):
            self.assertTrue(uninstall._remove_self_managed(self.root))

        self.assertFalse(self.root.exists())

    def test_self_managed_uninstall_sweeps_the_owned_root(self) -> None:
        """Watchers and triggers are addressed to the tree being removed.

        Asking uv where the tool lives answers about an installation that
        is not there, so the sweeps ran against nothing and left live
        watchers and scheduled triggers behind for an operator to find.
        """
        self._activate_generation("9.7.6")
        deploy.ownership.write_record(deploy.ownership.SELF)
        installation = deploy.ownership.describe(
            executable=self.root / "versions" / "9.7.6" / "bin" / "python")
        swept: list[Path | None] = []
        host = mock.Mock()
        host.supervisor.owned.return_value = []
        host.trigger_store.clear.return_value = 0

        with (
            mock.patch.object(
                uninstall.deploy.ownership, "describe",
                return_value=installation),
            mock.patch.object(
                uninstall.plugins, "tool_environment", return_value=None),
            mock.patch.object(
                uninstall, "_stop_own_watchers",
                side_effect=lambda environment: swept.append(environment) or []),
            mock.patch.object(
                uninstall, "_sweep_triggers",
                side_effect=swept.append),
            mock.patch.object(uninstall.runtime, "current", return_value=host),
            mock.patch.object(uninstall.completions, "remove", return_value=[]),
            mock.patch.object(
                uninstall.hostruntime, "id", return_value=hostruntime.LINUX),
            mock.patch.object(
                uninstall, "_remove_self_managed", return_value=True),
        ):
            self.assertEqual(0, uninstall.main([]))

        self.assertEqual([self.root, self.root], swept)


class TestCrossModuleAgreements(unittest.TestCase):
    def test_release_reads_commit_qualified_installed_bake_version(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        installed_version = release["_installed_version"]
        scope = installed_version.__globals__
        completed = mock.Mock(
            returncode=0,
            stdout=("agents-live 6.7.0.dev0+g0d2e0159 "
                    "(channel: bake, commit: 0d2e0159)\n"),
            stderr="",
        )

        with mock.patch.dict(scope, {
            "_installed_run": lambda _argv: completed,
        }):
            self.assertEqual(
                "6.7.0.dev0+g0d2e0159", installed_version())

    """Assertions that two parts of the tree still agree (#216).

    Each holds a fact that no single module can check, and that a defect
    reached production by breaking.
    """

    def _gate_text(self) -> str:
        return (REPOSITORY / "tools" / "release.py").read_text(encoding="utf-8")

    def _workflow_text(self, name: str) -> str:
        return (REPOSITORY / ".github" / "workflows" / name).read_text(
            encoding="utf-8")

    def _launch(self, name: str):
        return providers.get(name).prepare(
            ResolvedSpec(
                "invariant", "prompt", "write", (), (), (), name, None, None),
            Request(),
        )

    def _dashboard(self):
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
        return dashboard

    def _parse(self, name: str, stdout: str):
        return providers.get(name).parse(RawOutput(0, stdout, ""))

    def test_lock_command_excludes_a_contender_and_runs_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "shared.lock"
            result_path = root / "result.txt"
            command = [
                sys.executable,
                "-m",
                "agents_live.cli",
                "lock",
                str(lock_path),
                "--timeout",
                "0",
                "--",
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(result_path)!r}).write_text('ran')"
                ),
                "--repo",
                "opaque-child-argument",
            ]
            with hostruntime.exclusive_lock(lock_path, blocking=False):
                refused = subprocess.run(
                    command, check=False, capture_output=True, text=True)
            completed = subprocess.run(
                command, check=False, capture_output=True, text=True)
            result = result_path.read_text(encoding="utf-8")

        self.assertEqual(75, refused.returncode, refused.stderr)
        self.assertIn("lock is busy", refused.stderr)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("ran", result)

    def test_claude_never_loads_implicit_repository_configuration(self) -> None:
        for mode in ("plan", "write", "pipeline"):
            with self.subTest(mode=mode):
                launch = providers.get("claude").prepare(
                    ResolvedSpec(
                        "isolated", "prompt", mode, (), (), (),
                        "claude", None, None,
                    ),
                    Request(),
                )
                self.assertIn("--bare", launch.argv)
                self.assertEqual(1, launch.argv.count("--strict-mcp-config"))

    def test_copilot_explicitly_disables_prompt_mode_repository_code(self) -> None:
        launch = providers.get("copilot").prepare(
            ResolvedSpec(
                "isolated", "prompt", "write", (), (), (
                    ("COPILOT_ALLOW_ALL", "true"),
                    ("GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS", "true"),
                    ("GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS", "true"),
                    ("GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP", "true"),
                ), "copilot", None, None,
            ),
            Request(),
        )

        environment = dict(launch.env)
        self.assertEqual("false", environment["COPILOT_ALLOW_ALL"])
        self.assertEqual(
            "false", environment["GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS"])
        self.assertEqual(
            "false", environment["GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS"])
        self.assertEqual(
            "false", environment["GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP"])

    def test_a_provider_that_reports_cost_reports_it_under_one_key(self) -> None:
        """Zero spend and unreported spend look identical on screen.

        The dashboard reads one key. Each provider derives it from a
        different vendor field, so a provider that names its own key
        still parses, still runs, and silently reports nothing. The
        parsers run here against real vendor output rather than being
        read, because a key named only in a docstring reports no spend.
        """
        readers = (REPOSITORY / "src" / "agents_live" / "cli" / "scripts"
                   / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn("list_cost_usd", readers)
        self.assertEqual(
            set(providers.names()), set(PROVIDER_SPEND),
            "a new provider must declare whether it reports spend, or it "
            "joins the suite reporting none and nobody notices")
        for name, stdout in PROVIDER_SPEND.items():
            if stdout is None:
                continue  # a provider that reports no spend owes no key
            with self.subTest(provider=name):
                usage = dict(self._parse(name, stdout).usage)
                self.assertIn(
                    "list_cost_usd", usage,
                    f"{name} reports cost under a key the dashboard "
                    "does not read, so its spend shows as nothing")
                self.assertTrue(
                    Decimal(usage["list_cost_usd"]) > 0,
                    f"{name} parsed vendor output that reported spend and "
                    "produced a figure the dashboard cannot distinguish "
                    "from a free run")

    def test_no_provider_captures_its_output_through_a_posix_only_terminal(
            self) -> None:
        """A PTY is `script -qec`, so Windows ignores the request.

        Cost was captured from a footer the CLI printed only to a
        terminal. Windows allocates none, ignores the flag without
        error, and recorded no spend at all for months.
        """
        for name in providers.names():
            with self.subTest(provider=name):
                self.assertFalse(
                    self._launch(name).use_pty,
                    f"{name} depends on a terminal that exists on POSIX "
                    "and is silently absent on Windows")

    def test_a_provider_that_reports_cost_asks_for_machine_readable_output(
            self) -> None:
        """Scraping a human footer is what made cost platform-specific.

        A structured stream carries the same figures on every host, so
        the request for one is the property worth holding.
        """
        for name, stdout in PROVIDER_SPEND.items():
            if stdout is None:
                continue
            with self.subTest(provider=name):
                self.assertIn(
                    "--output-format", self._launch(name).argv,
                    f"{name} would parse a human-facing footer, which is "
                    "printed on some hosts and not others")

    def test_every_flag_a_provider_emits_is_one_its_cli_accepts(self) -> None:
        """A fake runner accepts any flag; the real CLI does not.

        The provider seam was covered by tests that recorded argv and
        asserted what the code already emitted, so ``--mcp`` -- a flag
        neither ``copilot`` nor ``claude`` has ever had -- was asserted
        into place and every agent declaring ``mcps`` failed at startup
        against the real binary (#296). Ask the installed CLI what it
        accepts instead of asking the code what it sends.
        """
        checked = 0
        for name in providers.names():
            argv = self._launch_with_project_mcp(name).argv
            if argv[0] == sys.executable:
                continue  # an in-tree double, whose flags this repo defines
            executable = shutil.which(argv[0])
            if executable is None:
                continue  # the CLI this provider drives is not installed here
            help_text = subprocess.run(
                [executable, "--help"], capture_output=True, text=True,
                check=False, timeout=120).stdout
            if "--help" not in help_text:
                continue  # the probe itself did not answer; prove nothing
            checked += 1
            for flag in (token for token in argv if token.startswith("--")):
                with self.subTest(provider=name, flag=flag):
                    self.assertRegex(
                        help_text, re.escape(flag) + r"(?![\w-])",
                        f"{name} sends {flag}, which {argv[0]} does not "
                        "accept, so every run of an agent that reaches "
                        "this path fails before the agent starts")
        if not checked:
            self.skipTest("no provider CLI is installed on this host")

    def _launch_with_project_mcp(self, name: str):
        """A launch that exercises the project-MCP path, which is where a
        declared ``mcps`` list turns into flags."""
        return providers.get(name).prepare(
            ResolvedSpec(
                "invariant", "prompt", "write", (),
                (McpServer("repo-tool", {"type": "stdio", "command": "uv"}),),
                (("AGENTS_LIVE_PROJECT_MCP_CONFIG", "config.json"),),
                name, None, None),
            Request(),
        )

    def test_every_test_file_is_run_by_the_gates_and_by_ci(self) -> None:
        """A suite the release does not run is not a gate.

        The gates and the workflow both name test files one at a time, so
        adding a file is silently adding an unrun file unless both lists
        move with it.
        """
        gates = self._gate_text()
        workflow = self._workflow_text("test.yml")
        files = sorted(path.name for path in
                       (REPOSITORY / "tests").glob("test_*.py"))
        self.assertTrue(files)
        for name in files:
            with self.subTest(name=name):
                self.assertIn(f"tests/{name}", gates)
                self.assertIn(f"tests/{name}", workflow)

    def test_publish_uses_release_attached_accepted_artifacts(self) -> None:
        publish = self._workflow_text("publish.yml")
        self.assertIn(
            "github.event_name == 'workflow_dispatch' || "
            "!github.event.release.prerelease",
            publish,
        )
        self.assertIn("gh release download", publish)
        self.assertIn("--pattern '*.whl'", publish)
        self.assertIn("--pattern '*.tar.gz'", publish)
        self.assertIn("sha256sum --check", publish)
        self.assertNotIn("verified-release-dist", publish)
        self.assertNotIn("actions/download-artifact@v4", publish)
        self.assertNotIn("gh release upload", publish)
        self.assertNotIn("tools/release.py --gates", publish)
        self.assertNotIn("tests/test_", publish)

    def test_ci_avoids_duplicate_main_runs_and_keeps_merge_queue_checks(
            self) -> None:
        workflow = self._workflow_text("test.yml")
        self.assertNotRegex(workflow, r"(?m)^  push:")
        self.assertRegex(workflow, r"(?m)^  merge_group:")
        self.assertIn("Documentation-only change", workflow)
        self.assertIn("src/agents_live/skill/SKILL.md", workflow)
        self.assertIn("src/agents_live/skill/templates/*", workflow)

    def test_ci_parallelizes_source_and_exact_wheel_readiness(self) -> None:
        workflow = self._workflow_text("test.yml")
        self.assertRegex(workflow, r"(?m)^  source:")
        self.assertRegex(workflow, r"(?m)^  wheel:")
        self.assertRegex(workflow, r"(?m)^  readiness:")
        self.assertRegex(workflow, r"(?m)^  bootstrap-readiness:")
        self.assertRegex(workflow, r"(?m)^  test:")
        self.assertIn("suite:", workflow)
        self.assertIn("exact-wheel-${{ github.run_id }}", workflow)
        self.assertIn("needs.wheel.outputs.sha256", workflow)
        self.assertIn("sha256sum --check", workflow)
        self.assertIn("--wheel dist/${{ needs.wheel.outputs.name }}", workflow)
        self.assertIn("tools/release.py --build-artifacts", workflow)
        self.assertIn("tools/bootstrap-readiness.py", workflow)
        self.assertIn("bootstrap-readiness]", workflow)

    def test_workflow_actions_use_node24_and_real_cache_inputs(self) -> None:
        test = self._workflow_text("test.yml")
        publish = self._workflow_text("publish.yml")
        for workflow in (test, publish):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn("actions/checkout@v7", workflow)
                self.assertIn("astral-sh/setup-uv@v10.0.1", workflow)
                self.assertIn("cache-dependency-glob: pyproject.toml", workflow)
                self.assertNotIn("actions/checkout@v4", workflow)
                self.assertNotIn("astral-sh/setup-uv@v5", workflow)
        self.assertIn("fetch-depth: 0", test)
        self.assertIn("ref: ${{ inputs.ref || github.sha }}", test)
        self.assertIn("ref: ${{ inputs.ref || github.ref }}", test)
        self.assertIn("ref: ${{ needs.resolve.outputs.sha }}", publish)

    def test_the_release_gates_pin_the_smoketest_to_this_checkout(self) -> None:
        """Without ``--repo`` the smoketest acts on whatever root
        resolves, which on a configured host is another project."""
        gates = self._gate_text()
        self.assertRegex(
            gates, r'"--repo",\s*str\(ROOT\),\s*"smoketest"')

    def test_release_artifacts_build_from_tracked_source_only(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        build = release["_build_release_artifacts"]
        scope = build.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet"], cwd=root, check=True)
            release_files = tuple(root / name for name in (
                "pyproject.toml", "src/__init__.py", "src/VERSION",
                "src/changelog.md"))
            bootstrap_inputs = tuple(root / name for name in (
                "install.ps1", "install.sh"))
            for path in (*release_files, *bootstrap_inputs):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("committed\n", encoding="utf-8")
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."], cwd=root, check=True)
            subprocess.run([
                "git", "-c", "user.name=Test", "-c",
                "user.email=test@example.invalid", "commit", "--quiet",
                "-m", "initial",
            ], cwd=root, check=True)
            release_files[-1].write_text("release\n", encoding="utf-8")
            excluded = root / ".copilot-tracking" / "pr" / "pr.md"
            excluded.parent.mkdir(parents=True)
            excluded.write_text("local only\n", encoding="utf-8")
            (root / ".git" / "info" / "exclude").write_text(
                ".copilot-tracking/\n", encoding="utf-8")
            (root / "dist").mkdir()
            observed: dict[str, bool] = {}

            def run(command: list[str], *, capture: bool = False) -> str:
                if command[:2] == ["git", "archive"]:
                    subprocess.run(command, cwd=root, check=True)
                elif command[:2] == ["uv", "build"]:
                    source = Path(command[-1])
                    observed["tracked"] = (source / "tracked.txt").is_file()
                    observed["excluded"] = (
                        source / ".copilot-tracking" / "pr" / "pr.md").exists()
                    observed["overlay"] = (
                        source / "src" / "changelog.md").read_text(
                            encoding="utf-8") == "release\n"
                else:
                    raise AssertionError(command)
                return ""

            with mock.patch.dict(scope, {
                "ROOT": root,
                "RELEASE_FILES": release_files,
                "BOOTSTRAP_BUILD_INPUTS": bootstrap_inputs,
                "_run": run,
            }):
                build()

            self.assertEqual({
                "tracked": True,
                "excluded": False,
                "overlay": True,
            }, observed)

    def test_release_prepare_revalidates_files_after_gates(self) -> None:
        """A gate-time editor save must not produce a partial release (#227)."""
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        prepare = release["prepare"]
        scope = prepare.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet"], cwd=root, check=True,
                capture_output=True, text=True)
            files = tuple(root / name for name in (
                "pyproject.toml", "__init__.py", "VERSION", "changelog.md"))
            original = {}
            for index, path in enumerate(files):
                content = f"original {index}\n".encode()
                path.write_bytes(content)
                original[path] = content
            commands: list[list[str]] = []
            current_branch = "main"

            def update_versions(_current: str, _target: str) -> None:
                for path in files:
                    path.write_text("release\n", encoding="utf-8")

            def git(*args: str) -> str:
                if args == ("rev-parse", "HEAD"):
                    return "original-head"
                if args == ("branch", "--show-current"):
                    return current_branch
                if args == ("diff", "--name-only"):
                    return "\n".join(
                        path.name for path in files
                        if path.read_bytes() != original[path])
                if args in (
                    ("diff", "--cached", "--name-only"),
                    ("ls-files", "--others", "--exclude-standard"),
                ):
                    return ""
                raise AssertionError(args)

            def run(command: list[str], *, capture: bool = False) -> str:
                nonlocal current_branch
                commands.append(command)
                if command[:3] == ["git", "switch", "-c"]:
                    current_branch = command[3]
                if command == ["gate"]:
                    files[-1].write_bytes(original[files[-1]])
                return ""

            scope.update({
                "ROOT": root,
                "RELEASE_FILES": files,
                "_require_tools": lambda: None,
                "_current_version": lambda: "1.0.0",
                "_next_version": lambda _current, _bump: "1.1.0",
                "_check_bump": lambda _bump: "minor",
                "_print_plan": lambda *_args: None,
                "_check_prepare_state": lambda *_args, **_kwargs: None,
                "_acceptance_path": lambda _version: root / "acceptance.json",
                "_preparation_path": lambda _version: root / "preparation.json",
                "_checkpoint_path": lambda _version: root / "checkpoint.json",
                "_artifact_store_dir": lambda _version: root / "artifacts",
                "_candidate_branch": lambda _version: "release/v1.1.0-candidate",
                "_update_versions": update_versions,
                "_gate_commands": lambda: [["gate"]],
                "_git": git,
                "_run": run,
                "subprocess": mock.Mock(run=mock.Mock()),
            })

            with self.assertRaisesRegex(
                    release["ReleaseError"],
                    "version bump changed an unexpected file set"):
                prepare("minor")

            self.assertEqual(original, {
                path: path.read_bytes() for path in files})
            self.assertNotIn(["git", "add"], [command[:2] for command in commands])
            self.assertNotIn(
                ["git", "commit"], [command[:2] for command in commands])
            self.assertIn(
                ["git", "switch", "-c", "release/v1.1.0-candidate"],
                commands)

    def test_release_diff_rejects_staged_and_untracked_files(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        check_release_diff = release["_check_release_diff"]
        scope = check_release_diff.__globals__
        expected = "\n".join(
            path.relative_to(REPOSITORY).as_posix()
            for path in release["RELEASE_FILES"])

        for staged, untracked in (
            ("unexpected.txt", ""),
            ("", "unexpected.txt"),
        ):
            with self.subTest(staged=bool(staged), untracked=bool(untracked)):
                def git(*args: str) -> str:
                    return {
                        ("diff", "--name-only"): expected,
                        ("diff", "--cached", "--name-only"): staged,
                        ("ls-files", "--others", "--exclude-standard"): untracked,
                    }[args]

                scope["_git"] = git
                with self.assertRaisesRegex(
                        release["ReleaseError"],
                        "version bump changed an unexpected file set"):
                    check_release_diff()

    def test_release_index_rejects_an_editor_save_after_git_add(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        check_release_index = release["_check_release_index"]
        scope = check_release_index.__globals__
        expected = "\n".join(
            path.relative_to(REPOSITORY).as_posix()
            for path in release["RELEASE_FILES"])

        def git(*args: str) -> str:
            return {
                ("diff", "--name-only"): "src/agents_live/__init__.py",
                ("diff", "--cached", "--name-only"): expected,
                ("ls-files", "--others", "--exclude-standard"): "",
            }[args]

        scope["_git"] = git
        with self.assertRaisesRegex(
                release["ReleaseError"], "staged release changed before commit"):
            check_release_index()

    def test_release_commit_rejects_post_validation_changes(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        check_release_commit = release["_check_release_commit"]
        scope = check_release_commit.__globals__
        files = release["RELEASE_FILES"]
        validated = {path: b"validated\n" for path in files}
        expected = [
            path.relative_to(REPOSITORY).as_posix() for path in files]
        expected_blobs = {
            path: release["_blob_id"](path, content)
            for path, content in validated.items()
        }

        for extra, changed_file in (
            ("unexpected.txt", None),
            (None, files[0]),
        ):
            with self.subTest(extra=extra, changed_file=changed_file):
                changed = [*expected, *([extra] if extra else [])]

                def git(*args: str) -> str:
                    if args == ("diff", "--name-only", "HEAD^..HEAD"):
                        return "\n".join(changed)
                    _, reference = args
                    relative = reference.removeprefix("HEAD:")
                    path = next(
                        item for item in files
                        if item.relative_to(REPOSITORY).as_posix() == relative)
                    return (
                        "different-blob"
                        if path == changed_file else expected_blobs[path]
                    )

                scope["_git"] = git
                message = (
                    "unexpected file set" if extra else
                    "changed after validation"
                )
                with self.assertRaisesRegex(release["ReleaseError"], message):
                    check_release_commit(validated)

    def test_unpublished_release_requires_the_candidate_branch(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        check = release["_check_publish_state"]
        scope = check.__globals__
        expected_files = "\n".join(
            path.relative_to(REPOSITORY).as_posix()
            for path in release["RELEASE_FILES"])
        branch = "release/v1.2.3-candidate"

        def git(*args: str) -> str:
            return {
                ("status", "--porcelain"): "",
                ("branch", "--show-current"): branch,
                ("rev-parse", "HEAD"): "candidate",
                ("rev-parse", "origin/main"): "base",
                ("rev-list", "--count", "origin/main..HEAD"): "1",
                ("merge-base", "HEAD", "origin/main"): "base",
                ("cat-file", "-t", "v1.2.3"): "tag",
                ("rev-parse", "v1.2.3^{}"): "candidate",
                ("diff", "--name-only", "HEAD^..HEAD"): expected_files,
            }[args]

        with mock.patch.dict(scope, {
            "_git": git,
            "_run": mock.Mock(),
        }):
            self.assertTrue(check("1.2.3"))
            branch = "main"
            with self.assertRaisesRegex(
                    release["ReleaseError"], "must remain on"):
                check("1.2.3")

    def test_publish_rejects_missing_candidate_acceptance_before_gates(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        publish = release["publish"]
        scope = publish.__globals__
        gate_commands = mock.Mock(return_value=[])
        with mock.patch.dict(scope, {
            "_require_tools": lambda: None,
            "_current_version": lambda: "1.2.3",
            "_check_publish_state": lambda _version: True,
            "_check_preparation": lambda _version: {"prepared": True},
            "_check_candidate_acceptance": mock.Mock(
                side_effect=release["ReleaseError"]("accept candidate first")),
            "_gate_commands": gate_commands,
        }), mock.patch.object(
            scope["subprocess"], "run", return_value=mock.Mock(returncode=1)
        ):
            with self.assertRaisesRegex(
                    release["ReleaseError"], "accept candidate first"):
                publish()
        gate_commands.assert_not_called()

    def test_publish_uses_receipts_without_rerunning_gates(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        publish = release["publish"]
        scope = publish.__globals__
        publish_states = mock.Mock(return_value=True)
        preparation = mock.Mock(return_value={
            "prepared": True,
            "commit": "candidate-commit",
            "tag_object": "annotated-tag-object",
            "wheel": "dist/agents_live-1.2.3-py3-none-any.whl",
            "sdist": "dist/agents_live-1.2.3.tar.gz",
            "installers": [
                {"path": f"dist/{name}", "sha256": "digest"}
                for name in release["BOOTSTRAP_ASSETS"]
            ],
        })
        acceptance = mock.Mock(return_value={"accepted": True})
        release_notes = mock.Mock()
        commands: list[list[str]] = []
        with mock.patch.dict(scope, {
            "_require_tools": lambda: None,
            "_current_version": lambda: "1.2.3",
            "_check_publish_state": publish_states,
            "_check_preparation": preparation,
            "_check_candidate_acceptance": acceptance,
            "_release_notes": lambda _version: "notes",
            "_write_artifact_manifest": lambda *_args: Path("SHA256SUMS-1.2.3"),
            "_run": lambda command, **_kwargs: commands.append(command) or "",
            "_write_release_notes": release_notes,
        }), mock.patch.object(
            scope["subprocess"], "run", return_value=mock.Mock(returncode=1)
        ):
            publish()
        publish_states.assert_called_once_with("1.2.3")
        preparation.assert_called_once_with("1.2.3")
        acceptance.assert_called_once_with("1.2.3")
        self.assertEqual(
            [[
                "git", "push", "--atomic", "origin",
                "candidate-commit:refs/heads/main",
                "annotated-tag-object:refs/tags/v1.2.3",
            ]],
            commands,
        )
        release_notes.assert_called_once_with(
            "v1.2.3", "notes", create=True,
            assets=(
                Path("SHA256SUMS-1.2.3"),
                Path("dist/agents_live-1.2.3-py3-none-any.whl"),
                Path("dist/agents_live-1.2.3.tar.gz"),
                                *(Path(f"dist/{name}")
                                    for name in release["BOOTSTRAP_ASSETS"]),
            ),
            resume_draft=False)

    def test_release_artifacts_are_uploaded_before_draft_publication(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        write_notes = release["_write_release_notes"]
        scope = write_notes.__globals__
        commands: list[list[str]] = []
        with mock.patch.dict(scope, {
            "_run": lambda command, **_kwargs: commands.append(command) or "",
        }):
            write_notes(
                "v1.2.3", "notes", create=True,
                assets=(
                    Path("SHA256SUMS-1.2.3"),
                    Path("dist/agents_live-1.2.3-py3-none-any.whl"),
                    Path("dist/agents_live-1.2.3.tar.gz"),
                ))
        self.assertEqual("create", commands[0][2])
        self.assertIn("--draft", commands[0])
        self.assertNotIn("SHA256SUMS-1.2.3", commands[0])
        self.assertEqual([
            "gh", "release", "upload", "v1.2.3",
            "SHA256SUMS-1.2.3",
            str(Path("dist/agents_live-1.2.3-py3-none-any.whl")),
            str(Path("dist/agents_live-1.2.3.tar.gz")), "--clobber",
        ], commands[1])
        self.assertEqual([
            "gh", "release", "edit", "v1.2.3", "--draft=false",
        ], commands[2])

    def test_preparation_receipt_binds_gates_commit_and_artifacts(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        write = release["_write_preparation"]
        check = release["_check_preparation"]
        scope = write.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "dist" / "agents_live-1.2.3-py3-none-any.whl"
            sdist = root / "dist" / "agents_live-1.2.3.tar.gz"
            wheel.parent.mkdir()
            wheel.write_bytes(b"candidate wheel")
            sdist.write_bytes(b"candidate sdist")
            for name in release["BOOTSTRAP_ASSETS"]:
                (wheel.parent / name).write_bytes(name.encode())
            receipt = root / "preparation.json"

            def git(*args: str) -> str:
                return {
                    ("rev-parse", "refs/tags/v1.2.3"): "tag-object",
                    ("rev-parse", "HEAD"): "candidate-commit",
                    ("rev-parse", "HEAD^"): "base-commit",
                }[args]

            with mock.patch.dict(scope, {
                "ROOT": root,
                "_preparation_path": lambda _version: receipt,
                "_candidate_wheel": lambda _version: wheel,
                "_gate_commands": lambda: [["gate", "--exact"]],
                "_evidence_identity": lambda: {
                    "platform": "test-platform",
                    "python_version": "3.12.0",
                    "workflow_sha256": "workflow-digest",
                },
                "_git": git,
            }):
                write("1.2.3", wheel)
                payload = check("1.2.3")
                self.assertEqual("candidate-commit", payload["commit"])
                self.assertEqual([["gate", "--exact"]], payload["gates"])
                for field in (
                    "platform", "python_version", "workflow_sha256"):
                    with self.subTest(field=field):
                        stale = dict(payload)
                        stale[field] = "stale"
                        receipt.write_text(json.dumps(stale), encoding="utf-8")
                        with self.assertRaisesRegex(
                                release["ReleaseError"], f"stale.*{field}"):
                            check("1.2.3")
                payload["wheel_sha256"] = "stale"
                receipt.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(
                        release["ReleaseError"], "stale.*wheel_sha256"):
                    check("1.2.3")

    def test_preparation_preserves_artifacts_outside_mutable_dist(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        preserve = release["_preserve_release_artifacts"]
        candidate_wheel = release["_candidate_wheel"]
        scope = preserve.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "dist" / "agents_live-1.2.3-py3-none-any.whl"
            sdist = root / "dist" / "agents_live-1.2.3.tar.gz"
            wheel.parent.mkdir()
            wheel.write_bytes(b"accepted wheel")
            sdist.write_bytes(b"accepted sdist")
            for name in release["BOOTSTRAP_ASSETS"]:
                (wheel.parent / name).write_bytes(name.encode())
            store = root / ".git" / "release" / "artifacts-1.2.3"
            with mock.patch.dict(scope, {
                "ROOT": root,
                "_artifact_store_dir": lambda _version: store,
            }):
                preserved = preserve("1.2.3", wheel)
                wheel.write_bytes(b"later build")
                sdist.write_bytes(b"later build")
                self.assertEqual(b"accepted wheel", preserved.read_bytes())
                self.assertEqual(
                    b"accepted sdist",
                    (store / "agents_live-1.2.3.tar.gz").read_bytes())
                for name in release["BOOTSTRAP_ASSETS"]:
                    self.assertEqual(name.encode(), (store / name).read_bytes())
                self.assertEqual(preserved, candidate_wheel("1.2.3"))

    def test_candidate_acceptance_receipt_binds_commit_and_wheel(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        check = release["_check_candidate_acceptance"]
        scope = check.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "dist" / "agents_live-1.2.3-py3-none-any.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"candidate wheel")
            receipt = root / "git" / "acceptance-1.2.3.json"
            receipt.parent.mkdir()
            expected = {
                "schema": release["ACCEPTANCE_SCHEMA"],
                "accepted": True,
                "version": "1.2.3",
                "tag": "v1.2.3",
                "tag_object": "annotated-tag-object",
                "commit": "candidate-commit",
                "wheel": "dist/agents_live-1.2.3-py3-none-any.whl",
                "wheel_sha256": release["_sha256"](wheel),
                "platform": "test-platform",
                "python_version": "3.12.0",
                "workflow_sha256": "workflow-digest",
                "operational": True,
                "operational_agent": "sample-123",
                "cost_agent": "cost-agent-456",
            }
            receipt.write_text(json.dumps(expected), encoding="utf-8")
            with mock.patch.dict(scope, {
                "ROOT": root,
                "_acceptance_path": lambda _version: receipt,
                "_candidate_wheel": lambda _version: wheel,
                "_evidence_identity": lambda: {
                    "platform": "test-platform",
                    "python_version": "3.12.0",
                    "workflow_sha256": "workflow-digest",
                },
                "_git": lambda *args: (
                    "annotated-tag-object"
                    if args == ("rev-parse", "refs/tags/v1.2.3")
                    else "candidate-commit"),
            }):
                self.assertEqual(expected, check("1.2.3"))
                for field in (
                    "platform", "python_version", "workflow_sha256"):
                    with self.subTest(field=field):
                        stale = dict(expected)
                        stale[field] = "stale"
                        receipt.write_text(json.dumps(stale), encoding="utf-8")
                        with self.assertRaisesRegex(
                                release["ReleaseError"], f"stale.*{field}"):
                            check("1.2.3")
                receipt.write_text(json.dumps(expected), encoding="utf-8")
                expected["tag_object"] = "stale-tag-object"
                receipt.write_text(json.dumps(expected), encoding="utf-8")
                with self.assertRaisesRegex(
                        release["ReleaseError"], "stale.*tag_object"):
                    check("1.2.3")
                expected["tag_object"] = "annotated-tag-object"
                expected["wheel_sha256"] = "stale"
                receipt.write_text(json.dumps(expected), encoding="utf-8")
                with self.assertRaisesRegex(
                        release["ReleaseError"], "stale.*wheel_sha256"):
                    check("1.2.3")

    def test_candidate_acceptance_reinstalls_and_preserves_live_state(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        accept = release["accept_candidate"]
        scope = accept.__globals__
        status = {
            "ok": True,
            "agents": [{
                "repository": "C:/repo",
                "identifier": "sample-123",
                "state": "started",
                "loadable": True,
                "execution": {"watch": "src/** debounce 1s"},
                "is_owner": True,
                "ownership_available": True,
            }, {
                "repository": "C:/repo",
                "identifier": "remote-789",
                "state": "started",
                "loadable": True,
                "execution": {"watch": "remote/** debounce 1s"},
                "is_owner": False,
                "ownership_available": True,
            }],
        }
        all_status = {
            "ok": True,
            "repos": [
                {"name": "selected", "path": "C:/repo", "ok": True,
                 "result": status},
                {"name": "other", "path": "C:/other", "ok": True,
                 "result": {"ok": True, "agents": [{
                     "repository": "C:/other",
                     "identifier": "other-456",
                     "state": "started",
                     "loadable": True,
                     "execution": {"watch": "docs/** debounce 1s"},
                 }]}},
            ],
        }
        doctor = {"ok": True, "checks": []}
        completed = subprocess.CompletedProcess(
            [], 0,
            "Upgrade queued as abc123; result: C:/result.json; "
            "run `agents-live logs admin` after this process exits\n",
            "",
        )
        events = [
            {"status": "ok", "upgrade_phase": "quiesce-requested",
             "watcher": "sample-123", "root": "C:/repo"},
            {"status": "ok", "upgrade_phase": "quiesced",
             "watcher": "sample-123", "root": "C:/repo"},
            {"status": "ok", "operation": "plugin-converge",
             "message": "plugins already converged"},
            {"status": "ok", "upgrade_phase": "restore",
             "watcher": "sample-123", "root": "C:/repo"},
            {"status": "ok", "message": "deferred Windows upgrade completed"},
        ]
        written = mock.Mock(return_value=Path("acceptance.json"))
        checkpoint = mock.Mock(return_value=Path("checkpoint.json"))
        operational = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            repo_results = iter((status, status, status))
            all_results = iter((
                all_status, doctor,
                all_status, doctor,
                all_status, doctor,
            ))
            fake_os = mock.Mock()
            fake_os.name = "nt"
            with mock.patch.dict(scope, {
                "_require_tools": lambda: None,
                "_current_version": lambda: "1.2.3",
                "_check_publish_state": lambda _version: True,
                "_check_preparation": lambda _version: {"prepared": True},
                "_acceptance_path": lambda _version: root / "acceptance.json",
                "_checkpoint_path": lambda _version: root / "checkpoint.json",
                "_candidate_wheel": lambda _version: wheel,
                "_installed_version": mock.Mock(side_effect=("1.2.3", "1.2.3")),
                "_installed_json": lambda _repo, _command: next(repo_results),
                "_installed_all_json": lambda _command: next(all_results),
                "_installed_run": mock.Mock(return_value=completed),
                "_wait_for_upgrade_result": lambda _path: {
                    "status": "terminal", "operation_id": "abc123", "exit_code": 0},
                "_candidate_events": lambda _operation: events,
                "_run_operational_acceptance": operational,
                "_write_acceptance_checkpoint": checkpoint,
                "_write_candidate_acceptance": written,
                "os": fake_os,
            }):
                accept(root, "sample-123", "cost-agent-456")
            written.assert_called_once_with(
                "1.2.3", root.resolve(), wheel,
                operation_id="abc123",
                watchers=(("C:/repo", "sample-123"),),
                operational_agent="sample-123",
                cost_agent="cost-agent-456")
            self.assertEqual([
                mock.call(
                    root.resolve(), "sample-123", "cost-agent-456",
                    preflight=True),
                mock.call(root.resolve(), "sample-123", "cost-agent-456"),
            ], operational.call_args_list)
            checkpoint.assert_called_once_with(
                "1.2.3", root.resolve(), wheel,
                operation_id="abc123",
                contract=(
                    ("C:/other", "other-456", "started", True),
                    ("C:/repo", "remote-789", "started", True),
                    ("C:/repo", "sample-123", "started", True),
                ),
                watchers=(("C:/repo", "sample-123"),),
                operational_agent="sample-123",
                cost_agent="cost-agent-456")
            self.assertFalse((root / "acceptance.json").exists())
            self.assertFalse((root / "checkpoint.json").exists())

    def test_candidate_status_contract_rejects_malformed_rows(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        contract = release["_status_contract"]
        valid = {
            "repository": "C:/repo",
            "identifier": "sample-123",
            "state": "started",
            "loadable": True,
        }
        for payload in (
            {"ok": True, "agents": [valid, "malformed"]},
            {"ok": True, "repos": [{
                "name": "selected",
                "ok": True,
                "result": {"ok": True, "agents": [valid, 42]},
            }]},
            {"ok": True, "agents": [{
                "repository": "C:/repo",
                "identifier": "sample-123",
                "state": "started",
                "loadable": "yes",
            }]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                        release["ReleaseError"],
                        "non-object agent row|malformed agent row"):
                    contract(payload)

    def test_candidate_resume_reuses_only_a_matching_upgrade_checkpoint(
            self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        accept = release["accept_candidate"]
        scope = accept.__globals__
        status = {"ok": True, "agents": [{
            "repository": "C:/repo",
            "identifier": "sample-123",
            "state": "started",
            "loadable": True,
            "execution": {"watch": "src/** debounce 1s"},
            "is_owner": True,
            "ownership_available": True,
        }]}
        doctor = {"ok": True, "checks": []}
        finish = mock.Mock(return_value=Path("acceptance.json"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            checkpoint = root / "checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            replacement = mock.Mock()
            with mock.patch.dict(scope, {
                "_require_tools": lambda: None,
                "_current_version": lambda: "1.2.3",
                "_check_publish_state": lambda _version: True,
                "_check_preparation": lambda _version: {"prepared": True},
                "_acceptance_path": lambda _version: root / "acceptance.json",
                "_checkpoint_path": lambda _version: checkpoint,
                "_candidate_wheel": lambda _version: wheel,
                "_installed_version": lambda: "1.2.3",
                "_run_operational_acceptance": mock.Mock(),
                "_check_acceptance_checkpoint": lambda *_args: {
                    "operation_id": "abc123",
                    "contract": [["C:/repo", "sample-123", "started", True]],
                    "watchers": [["C:/repo", "sample-123"]],
                },
                "_installed_all_json": mock.Mock(side_effect=(status, doctor)),
                "_installed_json": mock.Mock(return_value=status),
                "_installed_run": replacement,
                "_finish_operational_acceptance": finish,
            }):
                accept(
                    root, "sample-123", "cost-agent-456", resume=True)
            replacement.assert_not_called()
            finish.assert_called_once_with(
                "1.2.3", root.resolve(), wheel,
                operation_id="abc123",
                before_contract=(
                    ("C:/repo", "sample-123", "started", True),),
                watchers=(("C:/repo", "sample-123"),),
                operational_agent="sample-123",
                cost_agent="cost-agent-456")
            self.assertFalse(checkpoint.exists())

    def test_candidate_resume_rejects_watcher_drift(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        accept = release["accept_candidate"]
        scope = accept.__globals__
        status = {"ok": True, "agents": [{
            "repository": "C:/repo", "identifier": "sample-123",
            "state": "started", "loadable": True,
            "execution": {"watch": None},
        }]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            checkpoint = root / "checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            operational = mock.Mock()
            with mock.patch.dict(scope, {
                "_require_tools": lambda: None,
                "_current_version": lambda: "1.2.3",
                "_check_publish_state": lambda _version: True,
                "_check_preparation": lambda _version: {"prepared": True},
                "_acceptance_path": lambda _version: root / "acceptance.json",
                "_checkpoint_path": lambda _version: checkpoint,
                "_candidate_wheel": lambda _version: wheel,
                "_installed_version": lambda: "1.2.3",
                "_run_operational_acceptance": operational,
                "_check_acceptance_checkpoint": lambda *_args: {
                    "operation_id": "abc123",
                    "contract": [["C:/repo", "sample-123", "started", True]],
                    "watchers": [["C:/repo", "sample-123"]],
                },
                "_installed_all_json": mock.Mock(side_effect=(
                    status, {"ok": True})),
                "_installed_json": mock.Mock(return_value=status),
            }):
                with self.assertRaisesRegex(
                        release["ReleaseError"], "watchers changed"):
                    accept(
                        root, "sample-123", "cost-agent-456", resume=True)
            self.assertEqual(1, operational.call_count)

    def test_operational_acceptance_rejects_final_watcher_drift(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        finish = release["_finish_operational_acceptance"]
        scope = finish.__globals__
        status = {"ok": True, "agents": [{
            "repository": "C:/repo", "identifier": "sample-123",
            "state": "started", "loadable": True,
            "execution": {"watch": None},
        }]}
        write = mock.Mock()
        with mock.patch.dict(scope, {
            "_run_operational_acceptance": mock.Mock(),
            "_installed_all_json": mock.Mock(side_effect=(
                status, {"ok": True})),
            "_installed_json": mock.Mock(return_value=status),
            "_write_candidate_acceptance": write,
        }):
            with self.assertRaisesRegex(
                    release["ReleaseError"], "representative watchers"):
                finish(
                    "1.2.3", Path("C:/repo"), Path("candidate.whl"),
                    operation_id="abc123",
                    before_contract=(
                        ("C:/repo", "sample-123", "started", True),),
                    watchers=(("C:/repo", "sample-123"),),
                    operational_agent="sample-123",
                    cost_agent="cost-agent-456")
        write.assert_not_called()

    def test_candidate_checkpoint_rejects_malformed_baselines(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        check = release["_check_acceptance_checkpoint"]
        scope = check.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            checkpoint = root / "checkpoint.json"
            base = {
                "schema": release["CHECKPOINT_SCHEMA"],
                "phase": "upgrade-complete",
                "version": "1.2.3",
                "tag": "v1.2.3",
                "tag_object": "tag-object",
                "commit": "candidate",
                "base_commit": "base",
                "wheel": "candidate.whl",
                "wheel_sha256": release["_sha256"](wheel),
                "sdist": "candidate.tar.gz",
                "sdist_sha256": "sdist-hash",
                "repo": str(root),
                "platform": sys.platform,
                "operational_agent": "sample-123",
                "cost_agent": "cost-agent-456",
                "operation_id": None,
            }
            with mock.patch.dict(scope, {
                "_checkpoint_path": lambda _version: checkpoint,
                "_release_identity": lambda *_args: {
                    key: base[key] for key in (
                        "version", "tag", "tag_object", "commit",
                        "base_commit", "wheel", "wheel_sha256",
                        "sdist", "sdist_sha256")
                },
            }):
                for contract, watchers in (
                    (["not-a-row"], [["C:/repo", "sample-123"]]),
                    ([ ["C:/repo", "sample-123", "started", True] ], [42]),
                ):
                    payload = {**base, "contract": contract, "watchers": watchers}
                    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                            release["ReleaseError"], "malformed"):
                        check(
                            "1.2.3", root, wheel,
                            "sample-123", "cost-agent-456")

    def test_failed_candidate_rerun_invalidates_previous_receipt(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        accept = release["accept_candidate"]
        scope = accept.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "acceptance.json"
            receipt.write_text('{"accepted":true}', encoding="utf-8")
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            with mock.patch.dict(scope, {
                "_require_tools": lambda: None,
                "_current_version": lambda: "1.2.3",
                "_check_publish_state": lambda _version: True,
                "_check_preparation": lambda _version: {"prepared": True},
                "_acceptance_path": lambda _version: receipt,
                "_candidate_wheel": lambda _version: wheel,
                "_installed_version": lambda: "0.0.0",
            }):
                with self.assertRaisesRegex(
                        release["ReleaseError"], "installed tool is 0.0.0"):
                    accept(root, "sample-123", "cost-agent-456")
            self.assertFalse(receipt.exists())

    def test_candidate_rerun_invalidates_receipt_before_state_check(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        accept = release["accept_candidate"]
        scope = accept.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "acceptance.json"
            receipt.write_text('{"accepted":true}', encoding="utf-8")
            with mock.patch.dict(scope, {
                "_require_tools": lambda: None,
                "_current_version": lambda: "1.2.3",
                "_acceptance_path": lambda _version: receipt,
                "_check_publish_state": mock.Mock(
                    side_effect=release["ReleaseError"]("state changed")),
            }):
                with self.assertRaisesRegex(
                        release["ReleaseError"], "state changed"):
                    accept(Path(temporary), "sample-123", "cost-agent-456")
            self.assertFalse(receipt.exists())

    def test_operational_acceptance_uses_uv_script_and_pinned_cli(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        run_operational = release["_run_operational_acceptance"]
        scope = run_operational.__globals__
        commands: list[list[str]] = []
        with mock.patch.dict(scope, {
            "_installed_cli": lambda: "C:/uv/tools/agents-live/agents-live.exe",
            "_run": lambda command, **_kwargs: commands.append(command) or "",
        }):
            run_operational(
                Path("C:/repo"), "sample-123", "cost-agent-456")
            self.assertEqual([
                "uv", "run", "--script", "tools/candidate-operational.py",
                "--cli", "C:/uv/tools/agents-live/agents-live.exe",
                "--repo", str(Path("C:/repo")),
                "--agent", "sample-123",
                "--cost-agent", "cost-agent-456",
            ], commands[0])
            commands.clear()
            run_operational(
                Path("C:/repo"), "sample-123", "cost-agent-456",
                preflight=True)
            self.assertEqual("--preflight", commands[0][-1])

    def test_candidate_preflight_reuses_acceptance_checks(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        preflight = release["candidate_preflight"]
        scope = preflight.__globals__
        operational = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(scope, {
            "_require_tools": mock.Mock(),
            "_run_operational_acceptance": operational,
        }):
            repository = Path(temporary)
            preflight(repository, "sample-123", "cost-agent-456")
        operational.assert_called_once_with(
            repository.resolve(), "sample-123", "cost-agent-456",
            preflight=True)

    def test_candidate_preflight_rejects_missing_repository(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        preflight = release["candidate_preflight"]
        scope = preflight.__globals__
        with mock.patch.dict(scope, {"_require_tools": mock.Mock()}):
            with self.assertRaisesRegex(
                    release["ReleaseError"], "repository does not exist"):
                preflight(Path("missing-repository"), "sample", "cost")

    def test_local_deploy_preserves_dashboard_ports_and_repositories(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        scope = script["_dashboards"].__globals__
        commands = {
            100: "agents-live --repo C:\\Users\\name\\My Repo dashboard --dev",
            200: "agents-live dashboard --all-repos --open",
        }
        with mock.patch.dict(scope, {
            "_dashboard_modes": lambda pid: tuple(
                flag for flag in ("--all-repos", "--dev", "--open")
                if flag in commands[pid]),
        }):
            dashboards = script["_dashboards"](
                "PORT  URL                    PID  ANSWERING  STARTED  REPOSITORY\n"
                "8231  http://127.0.0.1:8231  100  yes        now      C:\\Users\\name\\My Repo\n"
                "8247  200  yes        now      -\n"
            )
        self.assertEqual([
            (8231, 100, "C:\\Users\\name\\My Repo", ("--dev",)),
            (8247, 200, None, ("--all-repos", "--open")),
        ], [
            (item.port, item.pid, item.repository, item.modes)
            for item in dashboards
        ])

    def test_local_deploy_script_starts_in_an_isolated_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    "uv", "run", "--cache-dir", temporary,
                    "--script", "tools/local-deploy.py", "--help",
                ],
                cwd=REPOSITORY,
                env={
                    **os.environ,
                    "VIRTUAL_ENV": "",
                    "PYTHONPATH": "",
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--repo", completed.stdout)

    def test_local_deploy_rejects_an_implicit_version_downgrade(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        deploy = script["deploy"]
        scope = deploy.__globals__
        with mock.patch.dict(scope, {
            "_synchronize": lambda: "abc123",
            "_bake_configuration": lambda: ("bake/v1.2.2-local", "1.2.2"),
        }), mock.patch.dict(scope["RELEASE"], {
            "_installed_version": lambda: "1.2.3",
        }):
            with self.assertRaisesRegex(
                    script["LocalDeployError"], "pass --allow-downgrade"):
                deploy(Path("C:/repo"))

    def test_release_report_includes_standalone_promotion_decisions(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "release-report.py"))
        issue_rows = script["_issue_rows"]
        scope = issue_rows.__globals__

        with mock.patch.dict(scope, {
            "_json": lambda *_args: {
                "number": 395,
                "title": "Publish bootstrap installers",
                "state": "OPEN",
                "url": "https://example.invalid/issues/395",
            },
        }):
            rows, assigned = issue_rows(
                "owner/repository", {"promotion_decision": [395]})

        self.assertEqual({395}, assigned)
        self.assertEqual(1, len(rows))
        self.assertIn("Awaiting promotion decision", rows[0])
        self.assertIn("| open | required |", rows[0])

    def test_local_deploy_synchronizes_the_configured_bake_branch(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        synchronize = script["_synchronize"]
        scope = synchronize.__globals__
        commands = []

        def git(*args):
            responses = {
                ("status", "--porcelain"): "",
                ("branch", "--show-current"): "bake/v6.7.0-local",
                ("rev-parse", "HEAD"): "abc123",
                ("rev-parse", "origin/bake/v6.7.0-local"): "abc123",
            }
            return responses[args]

        with mock.patch.dict(scope, {
            "_git": git,
            "_bake_configuration": lambda: (
                "bake/v6.7.0-local", "6.7.0"),
            "_run": lambda command, **_kwargs: commands.append(command),
        }):
            self.assertEqual("abc123", synchronize())
        self.assertEqual([
            "git", "pull", "--ff-only", "origin", "bake/v6.7.0-local",
        ], commands[0])

    def test_local_deploy_stamps_only_the_archived_bake_source(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            package = source / "src" / "agents_live"
            skill = package / "skill"
            skill.mkdir(parents=True)
            (source / "pyproject.toml").write_text(
                'version = "6.6.0"\n', encoding="utf-8")
            (package / "__init__.py").write_text(
                '__version__ = "6.6.0"\n', encoding="utf-8")
            (skill / "VERSION").write_text("6.6.0\n", encoding="utf-8")

            script["_stamp_bake_version"](
                source, "6.6.0", "6.7.0.dev0+gabc12345")

            self.assertIn(
                'version = "6.7.0.dev0+gabc12345"',
                (source / "pyproject.toml").read_text(encoding="utf-8"))
            self.assertIn(
                '__version__ = "6.7.0.dev0+gabc12345"',
                (package / "__init__.py").read_text(encoding="utf-8"))
            self.assertEqual(
                "6.7.0.dev0+gabc12345\n",
                (skill / "VERSION").read_text(encoding="utf-8"))
        self.assertEqual((6, 7, 0), script["_version_tuple"](
            "6.7.0.dev0+gabc12345"))

    def test_local_deploy_reuses_only_matching_preparation_evidence(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        prepared = script["_prepared_artifact"]
        scope = prepared.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            receipt = root / "preparation.json"
            payload = {
                "schema": script["LOCAL_PREPARATION_SCHEMA"],
                "prepared": True,
                "commit": "abc123",
                "version": "1.2.3",
                "wheel": str(wheel),
                "wheel_sha256": "digest",
                "platform": sys.platform,
                "os_name": os.name,
                "architecture": script["platform"].machine(),
                "gates": [list(command) for command in script["LOCAL_GATES"]],
            }
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.dict(scope, {
                "_state_directory": lambda: root,
            }), mock.patch.dict(scope["RELEASE"], {
                "_sha256": lambda _path: "digest",
            }):
                self.assertEqual(
                    (wheel.resolve(), "digest"),
                    prepared("abc123", "1.2.3"))
                payload["gates"] = [["stale-gate"]]
                receipt.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(prepared("abc123", "1.2.3"))
                payload["gates"] = [
                    list(command) for command in script["LOCAL_GATES"]]
                payload["platform"] = "different-platform"
                receipt.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(prepared("abc123", "1.2.3"))

    def test_local_deploy_builds_from_the_recorded_commit(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        prepare = script["_prepare_artifact"]
        scope = prepare.__globals__
        commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "built" / "agents_live-1.2.3-py3-none-any.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"wheel")

            def run(command, **_kwargs):
                commands.append(command)
                if command[:2] == ["git", "archive"]:
                    output = next(
                        item.removeprefix("--output=") for item in command
                        if item.startswith("--output="))
                    Path(output).write_bytes(b"archive")

            with mock.patch.dict(scope, {
                "_prepared_artifact": lambda *_args: None,
                "_state_directory": lambda: root / "state",
                "_stamp_bake_version": lambda *_args: None,
                "_run": run,
                "_require_unchanged_checkout": mock.Mock(),
            }), mock.patch.object(
                scope["shutil"], "unpack_archive",
                side_effect=lambda _archive, source: Path(source).mkdir(),
            ), mock.patch.object(
                Path, "glob", return_value=iter((wheel,)),
            ), mock.patch.dict(scope["RELEASE"], {
                "_sha256": lambda _path: "digest",
            }):
                artifact, digest = prepare("abc123", "1.2.3")
            self.assertIn("abc123", commands[0])
            self.assertNotIn(str(REPOSITORY), commands[1])
            self.assertEqual("digest", digest)
            self.assertTrue(artifact.is_file())

    def test_local_deploy_rejects_checkout_drift_before_replacement(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        deploy = script["deploy"]
        scope = deploy.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "candidate.whl"
            wheel.write_bytes(b"wheel")
            dashboards = mock.Mock()
            with mock.patch.dict(scope, {
                "_synchronize": lambda: "abc123",
                "_bake_configuration": lambda: (
                    "bake/v1.2.3-local", "1.2.3"),
                "_prepare_artifact": lambda *_args: (wheel, "digest"),
                "_require_unchanged_checkout": mock.Mock(
                    side_effect=script["LocalDeployError"]("checkout changed")),
                "_running_dashboards": dashboards,
            }), mock.patch.dict(scope["RELEASE"], {
                "_installed_version": lambda: "1.2.3",
            }):
                with self.assertRaisesRegex(
                        script["LocalDeployError"], "checkout changed"):
                    deploy(root)
            dashboards.assert_not_called()

    def test_local_deploy_postcheck_uses_release_contract_helpers(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        postcheck = script["_postcheck"]
        scope = postcheck.__globals__
        baseline = (("C:/repo", "sample-123", "started", True),)
        status = {"ok": True, "agents": []}
        contract = mock.Mock(return_value=baseline)
        watcher_contract = mock.Mock(return_value=(
            ("C:/repo", "sample-123"),))
        all_results = {
            "status": status,
            "doctor": {"ok": True},
        }
        with mock.patch.dict(scope, {
            "_direct_url": lambda: Path("wheel.whl"),
        }), mock.patch.dict(scope["RELEASE"], {
            "_installed_all_json": lambda command: all_results[command],
            "_installed_version": lambda: "1.2.3",
            "_status_contract": contract,
            "_status_rows": lambda _payload: [],
            "_started_watchers": watcher_contract,
        }):
            postcheck(
                Path("C:/repo"), Path("wheel.whl"), "1.2.3", baseline,
                (("C:/repo", "sample-123"),), (), (), None)
        contract.assert_called_once_with(status)
        watcher_contract.assert_called_once_with({"agents": []})

    def test_local_deploy_counts_launcher_and_child_as_one_watcher(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        repository = str(Path("C:/repo").resolve())

        logical = script["_logical_watchers"]([
            (101, "sample-123", "C:/repo"),
            (102, "sample-123", "C:/repo"),
            (103, "other-456", "C:/repo"),
        ])

        self.assertEqual((
            (repository, "other-456"),
            (repository, "sample-123"),
        ), logical)
        self.assertLessEqual(set(logical), {
            (repository, "other-456"),
            (repository, "sample-123"),
        })

    def test_local_deploy_verifies_events_for_local_watchers_only(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        verify = script["_verify_upgrade_events"]
        scope = verify.__globals__
        events = [{
            "status": "ok",
            "upgrade_phase": "quiesce-requested",
            "root": "C:/repo",
            "watcher": "local-123",
        }]
        ordered = mock.Mock()
        baseline = (("C:/repo", "local-123"),)
        with mock.patch.dict(scope["RELEASE"], {
            "_candidate_events": lambda _operation: events,
            "_verify_candidate_events": ordered,
        }):
            verify("abc123", baseline)
            ordered.assert_called_once_with(
                events, (("C:/repo", "local-123"),))
            events[0]["watcher"] = "unexpected-789"
            with self.assertRaisesRegex(
                    script["LocalDeployError"], "exact local watcher"):
                verify("abc123", baseline)
            events.clear()
            with self.assertRaisesRegex(
                    script["LocalDeployError"], "exact local watcher"):
                verify("abc123", baseline)

    def test_local_deploy_waits_through_transient_empty_dashboard_rows(
            self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        wait = script["_await_api_rows"]
        scope = wait.__globals__
        responses = iter((None, {"agents": []}, {"agents": [{"name": "ok"}]}))
        with mock.patch.dict(scope, {
            "_api": lambda _port: next(responses),
        }), mock.patch.object(scope["time"], "sleep", return_value=None):
            self.assertEqual(
                [{"name": "ok"}], wait(8231, timeout_s=10)["agents"])

    def test_local_deploy_cleans_the_dashboard_tree_after_readiness_failure(
            self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        start = script["_start_dashboard"]
        scope = start.__globals__
        process = mock.Mock()
        process.poll.return_value = None
        cleanup = mock.Mock()
        dashboard = script["Dashboard"](8231, 100, "C:/repo", ())
        with mock.patch.dict(scope, {
            "_installed_cli": lambda: Path("agents-live"),
            "_await_api_rows": mock.Mock(
                side_effect=script["LocalDeployError"]("not ready")),
            "_terminate_dashboard_tree": cleanup,
            "READY_TIMEOUT_S": 0,
        }), mock.patch.object(
            scope["subprocess"], "Popen", return_value=process,
        ):
            with self.assertRaisesRegex(
                    script["LocalDeployError"], "did not serve"):
                start(dashboard)
        cleanup.assert_called_once_with(process, 8231)

    def test_local_deploy_attempts_every_dashboard_restart(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        restart = script["_restart_dashboards"]
        scope = restart.__globals__
        first = script["Dashboard"](8231, 100, "C:/one", ())
        second = script["Dashboard"](8232, 200, "C:/two", ())
        attempted = []

        def start(dashboard):
            attempted.append(dashboard.port)
            if dashboard == first:
                raise script["LocalDeployError"]("first failed")

        with mock.patch.dict(scope, {
            "_port_answers": lambda _port: False,
            "_start_dashboard": start,
        }):
            with self.assertRaisesRegex(
                    script["LocalDeployError"], "8231.*first failed"):
                restart((first, second))
        self.assertEqual([8231, 8232], attempted)

    def test_local_deploy_rejects_wheel_mutation_around_upgrade(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        upgrade = script["_upgrade"]
        scope = upgrade.__globals__
        digests = iter(("digest", "changed"))
        with mock.patch.dict(scope["RELEASE"], {
            "_sha256": lambda _wheel: next(digests),
        }), mock.patch.dict(scope, {
            "_upgrade_once": mock.Mock(return_value="operation"),
        }):
            with self.assertRaisesRegex(
                    script["LocalDeployError"], "changed during replacement"):
                upgrade(Path("C:/repo"), Path("wheel.whl"), "digest")

    def test_local_deploy_accepts_synchronous_self_managed_windows_upgrade(
            self) -> None:
        """Generation switching does not need the uv replacement helper."""
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        upgrade_once = script["_upgrade_once"]
        scope = upgrade_once.__globals__
        windows = mock.Mock()
        windows.name = "nt"
        completed = subprocess.CompletedProcess(
            [], 0, "Activated self-managed generation 6.7.0\n", "")
        with (
            mock.patch.dict(scope, {
                "os": windows,
                "_installed_cli": lambda: Path("generation/agents-live.exe"),
                "_installed_run": lambda *_args: completed,
            }),
            mock.patch.object(
                scope["deployment"].layout,
                "generation_of",
                return_value="6.7.0",
            ),
        ):
            self.assertIsNone(
                upgrade_once(Path("C:/repo"), Path("candidate.whl")))

    def test_local_deploy_retries_one_failed_windows_upgrade(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "local-deploy.py"))
        upgrade = script["_upgrade"]
        scope = upgrade.__globals__
        attempts = mock.Mock(side_effect=(
            script["LocalDeployError"]("launcher held"),
            "operation-2",
        ))
        windows = mock.Mock()
        windows.name = "nt"
        with mock.patch.dict(scope, {
            "os": windows,
            "_upgrade_once": attempts,
        }), mock.patch.dict(scope["RELEASE"], {
            "_sha256": lambda _wheel: "digest",
        }):
            self.assertEqual(
                "operation-2",
                upgrade(Path("C:/repo"), Path("wheel.whl"), "digest"))
        self.assertEqual(2, attempts.call_count)

    def test_operational_preflight_checks_browser_dashboard_and_agents(
            self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        preflight = script["_preflight"]
        scope = preflight.__globals__
        browser = mock.Mock()
        playwright = mock.Mock()
        playwright.chromium.launch.return_value = browser
        manager = mock.MagicMock()
        manager.__enter__.return_value = playwright
        sync_api = mock.Mock(sync_playwright=mock.Mock(return_value=manager))
        status = {"ok": True, "agents": [
            {
                "identifier": "sample-123", "loadable": True,
                "state": "started", "execution": {"watch": "src/**"},
                "is_owner": True, "ownership_available": True,
            },
            {"identifier": "cost-agent-456", "loadable": True},
        ]}
        with (
            mock.patch.dict(sys.modules, {"playwright.sync_api": sync_api}),
            mock.patch.dict(scope, {
                "_run": mock.Mock(return_value=mock.Mock(
                    stdout="No dashboard started by this host is running.")),
                "_json": mock.Mock(return_value=status),
                "_browser_executable": lambda: Path("browser.exe"),
                "_resident_watcher_ids": lambda _repo: {"sample-123"},
            }),
        ):
            preflight(
                Path("agents-live.exe"), Path("C:/repo"),
                "sample-123", "cost-agent-456")
            with self.assertRaisesRegex(
                    script["OperationalError"], "must be distinct"):
                preflight(
                    Path("agents-live.exe"), Path("C:/repo"),
                    "sample-123", "sample-123")
        playwright.chromium.launch.assert_called_once_with(
            executable_path=str(Path("browser.exe")), headless=True)
        browser.close.assert_called_once_with()

    def test_operational_preflight_rejects_intent_only_watchers(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        preflight = script["_preflight"]
        scope = preflight.__globals__
        status = {"ok": True, "agents": [
            {
                "identifier": "watcher-123", "loadable": True,
                "state": "started", "execution": {"watch": "src/**"},
                "is_owner": True, "ownership_available": True,
            },
            {"identifier": "cost-agent-456", "loadable": True},
        ]}
        manager = mock.MagicMock()
        manager.__enter__.return_value = mock.Mock()
        sync_api = mock.Mock(
            sync_playwright=mock.Mock(return_value=manager))
        with mock.patch.dict(
                sys.modules, {"playwright.sync_api": sync_api}), \
                mock.patch.dict(scope, {
                    "_run": mock.Mock(return_value=mock.Mock(
                        stdout="No dashboard started by this host is running.")),
                    "_json": mock.Mock(return_value=status),
                    "_resident_watcher_ids": lambda _repo: set(),
                    "_browser_executable": lambda: Path("browser.exe"),
                }):
            with self.assertRaisesRegex(
                    script["OperationalError"],
                    "started watcher processes are not resident.*watcher-123"):
                preflight(
                    Path("agents-live.exe"), Path("C:/repo"),
                    "watcher-123", "cost-agent-456")

    def test_operational_preflight_ignores_remote_started_watchers(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        preflight = script["_preflight"]
        scope = preflight.__globals__
        browser = mock.Mock()
        playwright = mock.Mock()
        playwright.chromium.launch.return_value = browser
        manager = mock.MagicMock()
        manager.__enter__.return_value = playwright
        sync_api = mock.Mock(sync_playwright=mock.Mock(return_value=manager))
        status = {"ok": True, "agents": [
            {
                "identifier": "remote-watcher-123", "loadable": True,
                "state": "started", "execution": {"watch": "src/**"},
                "is_owner": False, "ownership_available": True,
            },
            {"identifier": "sample-123", "loadable": True},
            {"identifier": "cost-agent-456", "loadable": True},
        ]}
        with (
            mock.patch.dict(sys.modules, {"playwright.sync_api": sync_api}),
            mock.patch.dict(scope, {
                "_run": mock.Mock(return_value=mock.Mock(
                    stdout="No dashboard started by this host is running.")),
                "_json": mock.Mock(return_value=status),
                "_browser_executable": lambda: Path("browser.exe"),
                "_resident_watcher_ids": lambda _repo: set(),
            }),
        ):
            preflight(
                Path("agents-live.exe"), Path("C:/repo"),
                "sample-123", "cost-agent-456")
        browser.close.assert_called_once_with()

    def test_windows_process_query_uses_a_real_tab_delimiter(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        scope = script["_process_command_lines"].__globals__
        windows = mock.Mock()
        windows.name = "nt"
        completed = mock.Mock(
            returncode=0,
            stdout="42\tC:\\tools\\agents-live.exe watch-loop sample-123\n",
        )
        run = mock.Mock(return_value=completed)
        with mock.patch.dict(scope, {
            "os": windows,
            "subprocess": mock.Mock(run=run, TimeoutExpired=subprocess.TimeoutExpired),
        }), mock.patch.object(scope["shutil"], "which", return_value="powershell"):
            self.assertEqual(
                ("C:\\tools\\agents-live.exe watch-loop sample-123",),
                script["_process_command_lines"]())
        command = run.call_args.args[0][-1]
        self.assertIn("[char]9", command)
        self.assertNotIn("`t", command)

    def test_resident_watcher_matching_uses_the_exact_quoted_repository(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        resident = script["_resident_watcher_ids"]
        scope = resident.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            repository = parent / "repo with space"
            other = parent / "repo with space-copy"
            repository.mkdir()
            other.mkdir()
            commands = (
                f'agents-live --repo "{repository}" status',
                f'not-agents-live --repo "{repository}" diagnostic watch-loop '
                'fake-123 --runtime-role watcher',
                f'agents-live --repo "{other}" internal watch-loop sample-123 '
                '--runtime-role watcher',
                f'agents-live --repo "{repository}" internal watch-loop sample-123 '
                '--runtime-role watcher',
            )
            with mock.patch.dict(scope, {
                "_process_command_lines": lambda: commands,
            }):
                self.assertEqual({"sample-123"}, resident(repository))
                self.assertEqual(set(), resident(parent / "repo"))

    def test_resident_watcher_matching_reads_the_v2_metadata_target(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        resident = script["_resident_watcher_ids"]
        scope = resident.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            metadata = artifacts.encode(artifacts.InvocationMetadata(
                "0123456789abcdef01234567",
                f"repo:{repository}",
                "agent:sample-123",
            ))
            command = (
                f'agents-live --repo "{repository}" internal watch-loop '
                f'--metadata {metadata} sample-123'
            )
            with mock.patch.dict(scope, {
                "_process_command_lines": lambda: (command,),
            }):
                self.assertEqual({"sample-123"}, resident(repository))

    def test_posix_process_query_preserves_nul_delimited_argv(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        argv = [
            "agents-live", "--repo", "/tmp/repo with space",
            "internal", "watch-loop", "sample-123",
            "--runtime-role", "watcher",
        ]
        rendered = script["_decode_posix_command_line"](
            b"\0".join(item.encode() for item in argv) + b"\0")
        self.assertEqual(argv, shlex.split(rendered))

    def test_operational_cost_probe_requires_positive_list_cost(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        verify = script["_verify_cost_capture"]
        scope = verify.__globals__
        run = {"ok": True, "status": "success", "run_id": "abc123"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "Agents").mkdir()
            homes = {
                name: str(root / directory)
                for name, directory in _ISOLATED_HOMES.items()
            }
            environment = {
                **os.environ,
                **homes,
                "AGENTS_LIVE_REPO": str(root),
            }
            with mock.patch.dict(os.environ, environment):
                directory = paths.repo_state_dir(root) / "logs"
                directory.mkdir(parents=True, exist_ok=True)
                log = directory / "cost-agent-456.jsonl"
                obs.record(log, obs.create(
                    "done", "ok", repository=str(root),
                    agent="cost-agent-456", run_id="abc123",
                    origin="manual", usage=(
                        ("ai_credits", "25"),
                        ("list_cost_usd", "0.25"),
                    )))
                append_event(log, (json.dumps({
                    "spec": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "event": "run",
                    "status": "success",
                    "repository": str(root),
                    "agent": "cost-agent-456",
                    "run_id": "other-run",
                    "origin": "manual",
                    "usage": ['["ai_credits","1"]'],
                }) + "\n").encode("utf-8"))
                archive = directory / "archive"
                archive.mkdir()
                writer = qlog.duckdb.connect(":memory:")
                writer.sql(
                    "CREATE TABLE archived AS SELECT "
                    "TIMESTAMPTZ '2026-08-16T00:00:00Z' AS ts, "
                    "'other-agent'::VARCHAR AS agent_name, "
                    "'done'::VARCHAR AS phase, 'ok'::VARCHAR AS status, "
                    "'manual'::VARCHAR AS trigger, 1::INTEGER AS log_schema, "
                    "'other-run'::VARCHAR AS run_id, "
                    "['[\"ai_credits\",\"1\"]']::VARCHAR[] AS usage"
                )
                writer.sql(
                    f"COPY archived TO '{archive / '2026-08.parquet'}' "
                    "(FORMAT PARQUET)"
                )
            completed = subprocess.run(
                [
                    sys.executable, "-m", "agents_live.cli", "--json",
                    "--repo", str(root), "logs", "--all", "--sql",
                    "select * from log where run_id = 'abc123' limit 20",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            positive = json.loads(completed.stdout)
            self.assertEqual([
                '["ai_credits","25"]',
                '["list_cost_usd","0.25"]',
            ], positive["records"][0]["usage"])
            positive["records"][0]["usage"].insert(0, "{malformed")
            with mock.patch.dict(scope, {
                "_json": mock.Mock(side_effect=(run, positive)),
            }):
                self.assertEqual(
                    ("abc123", 0.25),
                    verify(
                        Path("agents-live"), root,
                        "cost-agent-456"))
        numeric = {"ok": True, "records": [{
            "agent_name": "cost-agent-456", "run_id": "abc123",
            "status": "ok", "usage": [["list_cost_usd", 0.5]],
        }]}
        with mock.patch.dict(scope, {
            "_json": mock.Mock(side_effect=(run, numeric)),
        }):
            self.assertEqual(
                ("abc123", 0.5),
                verify(
                    Path("agents-live"), Path("C:/repo"),
                    "cost-agent-456"))
        missing = {"ok": True, "records": [{
            "agent_name": "cost-agent-456", "run_id": "abc123",
            "status": "ok", "usage": [],
        }]}
        with mock.patch.dict(scope, {
            "_json": mock.Mock(side_effect=(run, missing)),
        }):
            with self.assertRaisesRegex(
                    script["OperationalError"], "no positive list_cost_usd"):
                verify(Path("agents-live"), Path("C:/repo"), "cost-agent-456")
        for invalid in (
            "true", "0", "-0.1", '"NaN"', '"Infinity"', '"-Infinity"',
        ):
            invalid_cost = {"ok": True, "records": [{
                "agent_name": "cost-agent-456", "run_id": "abc123",
                "status": "ok",
                "usage": [f'["list_cost_usd",{invalid}]'],
            }]}
            with mock.patch.dict(scope, {
                "_json": mock.Mock(side_effect=(run, invalid_cost)),
            }):
                with self.assertRaisesRegex(
                        script["OperationalError"],
                        "no positive list_cost_usd"):
                    verify(
                        Path("agents-live"), Path("C:/repo"),
                        "cost-agent-456")

    def test_operational_dashboard_cost_includes_accepted_run(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        verify = script["_verify_dashboard_cost"]
        before = {"agents": [{
            "identifier": "cost-agent-456",
            "cost_day_value": 10.0,
            "cost_week_value": 20.0,
        }]}
        unchanged = {"agents": [dict(before["agents"][0])]}
        with self.assertRaisesRegex(
                script["OperationalError"],
            "did not equal accepted run cost"):
            verify(
                before, unchanged, "cost-agent-456", 0.25)
        after = {"agents": [{
            "identifier": "cost-agent-456",
            "cost_day_value": 10.25,
            "cost_week_value": 20.25,
        }]}
        verify(before, after, "cost-agent-456", 0.25)
        for daily, weekly in ((10.25, 20.0), (10.0, 20.25)):
            with self.subTest(daily=daily, weekly=weekly):
                one_sided = {"agents": [{
                    "identifier": "cost-agent-456",
                    "cost_day_value": daily,
                    "cost_week_value": weekly,
                }]}
                with self.assertRaisesRegex(
                        script["OperationalError"],
                        "did not equal accepted run cost"):
                    verify(before, one_sided, "cost-agent-456", 0.25)

    def test_operational_cost_attribution_rejects_intervening_run(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        verify = script["_verify_cost_attribution"]
        scope = verify.__globals__
        concurrent = {"ok": True, "records": [
            {"run_id": "abc123"},
            {"run_id": "other456"},
        ]}
        with mock.patch.dict(scope, {
            "_json": mock.Mock(return_value=concurrent),
        }):
            with self.assertRaisesRegex(
                    script["OperationalError"],
                    "expected only abc123"):
                verify(
                    Path("agents-live"), Path("C:/repo"),
                    "cost-agent-456", "abc123",
                    "2026-01-01T00:00:00+00:00")

    def test_operational_runner_restores_baseline_after_dashboard_failure(
            self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        main = script["main"]
        scope = main.__globals__
        row = {
            "identifier": "sample-123",
            "name": "sample",
            "state": "started",
            "loadable": True,
        }
        transitions: list[bool] = []

        def payload(_cli, _repo, command, *_args):
            if command == "status":
                return {"ok": True, "agents": [row]}
            if command == "logs":
                return {"ok": True, "records": [{
                    "agent_name": "sample-123", "phase": "done",
                    "status": "ok", "run_id": "abc123"}]}
            if command == "run":
                return {"ok": True, "status": "success", "run_id": "abc123"}
            return {"ok": True}

        with (
            mock.patch.object(
                sys, "argv", [
                    "candidate-operational.py",
                    "--cli", str(REPOSITORY / "agents-live"),
                    "--repo", str(REPOSITORY),
                    "--agent", "sample-123",
                    "--cost-agent", "cost-agent-456",
                ]),
            mock.patch.dict(scope, {
                "_json": payload,
                "_run": lambda *_args, **_kwargs: mock.Mock(
                    stdout="sample-123 timeline"),
                "_set_started": lambda _cli, _repo, _agent, started: (
                    transitions.append(started)),
                "_dashboard_actions": mock.Mock(
                    side_effect=script["OperationalError"]("dashboard failed")),
                "_verify_cost_capture": mock.Mock(),
            }),
        ):
            with self.assertRaisesRegex(
                    script["OperationalError"], "dashboard failed"):
                main()
        self.assertEqual([False, True, True], transitions)

    def test_operational_runner_restores_baseline_after_interrupt(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        main = script["main"]
        scope = main.__globals__
        row = {
            "identifier": "sample-123",
            "name": "sample",
            "state": "started",
            "loadable": True,
        }
        transitions: list[bool] = []

        def payload(_cli, _repo, command, *_args):
            if command == "status":
                return {"ok": True, "agents": [row]}
            if command == "logs":
                return {"ok": True, "records": [{
                    "agent_name": "sample-123", "phase": "done",
                    "status": "ok", "run_id": "abc123"}]}
            if command == "run":
                return {"ok": True, "status": "success", "run_id": "abc123"}
            return {"ok": True}

        with (
            mock.patch.object(
                sys, "argv", [
                    "candidate-operational.py",
                    "--cli", str(REPOSITORY / "agents-live"),
                    "--repo", str(REPOSITORY),
                    "--agent", "sample-123",
                    "--cost-agent", "cost-agent-456",
                ]),
            mock.patch.dict(scope, {
                "_json": payload,
                "_run": lambda *_args, **_kwargs: mock.Mock(
                    stdout="sample-123 timeline"),
                "_set_started": lambda _cli, _repo, _agent, started: (
                    transitions.append(started)),
                "_dashboard_actions": mock.Mock(
                    side_effect=KeyboardInterrupt()),
                "_verify_cost_capture": mock.Mock(),
            }),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main()
        self.assertEqual([False, True, True], transitions)

    def test_operational_runner_rejects_skipped_run_with_fresh_other_record(
            self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        main = script["main"]
        scope = main.__globals__
        row = {
            "identifier": "sample-123", "name": "sample",
            "state": "started", "loadable": True,
        }

        def payload(_cli, _repo, command, *_args):
            if command == "status":
                return {"ok": True, "agents": [row]}
            if command == "logs":
                return {"ok": True, "records": [{
                    "agent_name": "sample-123", "phase": "done", "status": "ok",
                    "run_id": "concurrent456"}]}
            if command == "run":
                return {"ok": True, "status": "skipped",
                        "run_id": "skipped123", "message": "already-running"}
            return {"ok": True}

        with (
            mock.patch.object(
                sys, "argv", [
                    "candidate-operational.py", "--cli", "agents-live",
                    "--repo", str(REPOSITORY), "--agent", "sample-123",
                    "--cost-agent", "cost-agent-456",
                ]),
            mock.patch.dict(scope, {
                "_json": payload,
                "_run": lambda *_args, **_kwargs: mock.Mock(stdout=""),
                "_set_started": mock.Mock(),
                "_dashboard_actions": mock.Mock(),
                "_verify_cost_capture": mock.Mock(),
            }),
        ):
            with self.assertRaisesRegex(
                    script["OperationalError"], "explicit run.*was skipped"):
                main()

    def test_operational_dashboard_run_requires_successful_terminal_event(
            self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        await_run = script["_await_dashboard_run"]
        scope = await_run.__globals__
        skipped = {"ok": True, "records": [{
            "agent_name": "sample-123",
            "run_id": "abc123",
            "status": "error",
        }]}
        with mock.patch.dict(scope, {
            "_json": mock.Mock(return_value=skipped),
        }):
            with self.assertRaisesRegex(
                    script["OperationalError"],
                    "dashboard Run was not successful"):
                await_run(
                    Path("agents-live"), Path("C:/repo"),
                    "sample-123", "2026-01-01T00:00:00+00:00")

        action = {"ok": True, "records": [{
            "agent_name": "sample-123",
            "run_id": "abc123",
            "status": "ok",
        }]}
        terminal = {"ok": True, "records": [{
            "agent_name": "sample-123",
            "run_id": "abc123",
            "phase": "done",
            "status": "ok",
        }]}
        with mock.patch.dict(scope, {
            "_json": mock.Mock(side_effect=(action, terminal)),
        }):
            self.assertEqual(
                "abc123",
                await_run(
                    Path("agents-live"), Path("C:/repo"),
                    "sample-123", "2026-01-01T00:00:00+00:00"))

    def test_dashboard_process_cleanup_runs_after_browser_close_error(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        dashboard_actions = script["_dashboard_actions"]
        scope = dashboard_actions.__globals__
        process = mock.Mock()
        process.pid = 42
        process.poll.return_value = None
        browser = mock.Mock()
        browser.close.side_effect = RuntimeError("browser close failed")
        playwright = mock.Mock()
        playwright.chromium.launch.return_value = browser
        manager = mock.MagicMock()
        manager.__enter__.return_value = playwright
        manager.__exit__.return_value = False
        sync_api = mock.Mock(sync_playwright=mock.Mock(return_value=manager))
        terminated: list[list[str]] = []
        dashboard_lists = iter((
            "No dashboard started by this host is running.",
            "PORT PID\n8232 42",
            "No dashboard started by this host is running.",
        ))
        with (
            mock.patch.dict(sys.modules, {"playwright.sync_api": sync_api}),
            mock.patch.dict(scope, {
                "_free_port": lambda: 8232,
                "_await_api": lambda *_args, **kwargs: (
                    kwargs.get("observe", lambda: None)()
                    or {"agents": [{
                        "identifier": "cost-agent-456",
                        "cost_day_value": 0.25,
                        "cost_week_value": 0.25,
                    }]}),
                "_registered_dashboard_pid": lambda *_args: 42,
                "_api": lambda _port: {"agents": [{
                    "identifier": "cost-agent-456",
                    "cost_day_value": 0.25,
                    "cost_week_value": 0.25,
                }]},
                "_verify_cost_capture": lambda *_args: ("abc123", 0.25),
                "_await_dashboard_cost": lambda *_args: None,
                "_verify_cost_attribution": lambda *_args: None,
                "_run": lambda *_args, **_kwargs: mock.Mock(
                    stdout=next(dashboard_lists)),
                "_port_answers": lambda _port: False,
                "_browser_executable": lambda: Path("browser.exe"),
                "subprocess": mock.Mock(
                    Popen=mock.Mock(return_value=process),
                    run=mock.Mock(side_effect=lambda command, **_kwargs: (
                        terminated.append(command) or mock.Mock(returncode=0)))),
                "os": mock.Mock(name="nt"),
            }),
        ):
            scope["os"].name = "nt"
            with self.assertRaisesRegex(RuntimeError, "browser close failed"):
                dashboard_actions(
                    Path("agents-live.exe"), Path("C:/repo"),
                    "sample-123", "sample", True,
                    "cost-agent-456")
        self.assertIn([
            "agents-live.exe", "--repo", str(Path("C:/repo")),
            "dashboard", "stop", "--port", "8232",
        ], terminated)
        self.assertNotIn(["taskkill", "/T", "/F", "/PID", "42"], terminated)

    def test_dashboard_stop_requires_browser_visible_stopped_state(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        dashboard_actions = script["_dashboard_actions"]
        scope = dashboard_actions.__globals__
        cost_probe = mock.Mock(return_value=("abc123", 0.25))
        for baseline in (True, False):
            with self.subTest(baseline=baseline):
                process = mock.Mock(pid=42)
                process.poll.return_value = None
                process.wait.return_value = 0
                start_button = mock.Mock()
                start_button.wait_for.side_effect = RuntimeError(
                    "start control never appeared")
                action_order: list[str] = []
                run_button = mock.Mock()
                run_button.click.side_effect = lambda: action_order.append("run")
                row = mock.Mock()

                def row_button(_role, *, name):
                    if name == "Register this host's cron/watcher":
                        return start_button
                    if name == "Run this agent once now":
                        return run_button
                    return mock.Mock()

                row.get_by_role.side_effect = row_button
                row.count.return_value = 1
                page = mock.Mock()
                page.get_by_role.return_value.filter.return_value = row
                refresh_lines = mock.Mock()
                refresh_lines.count.return_value = 1
                refresh_lines.nth.return_value.wait_for.side_effect = (
                    lambda **_kwargs: action_order.append("health-ready"))

                def page_text(value, **_kwargs):
                    rendered = (
                        "[14:00:00 MDT] "
                        "Health check dashboard refresh complete"
                    )
                    if isinstance(value, re.Pattern) and value.search(rendered):
                        return refresh_lines
                    return mock.Mock()

                page.get_by_text.side_effect = page_text
                browser = mock.Mock()
                browser.new_page.return_value = page
                playwright = mock.Mock()
                playwright.chromium.launch.return_value = browser
                manager = mock.MagicMock()
                manager.__enter__.return_value = playwright
                sync_api = mock.Mock(
                    sync_playwright=mock.Mock(return_value=manager))
                dashboard_lists = iter((
                    "No dashboard started by this host is running.",
                    "PORT PID\n8232 42",
                    "No dashboard started by this host is running.",
                ))
                with (
                    mock.patch.dict(
                        sys.modules, {"playwright.sync_api": sync_api}),
                    mock.patch.dict(scope, {
                        "_free_port": lambda: 8232,
                        "_await_api": lambda *_args, **kwargs: (
                            kwargs.get("observe", lambda: None)()
                            or {"agents": [{
                                "identifier": "cost-agent-456",
                                "cost_day_value": 0.25,
                                "cost_week_value": 0.25,
                            }]}),
                        "_registered_dashboard_pid": lambda *_args: 42,
                        "_api": lambda _port: {"agents": [{
                            "identifier": "cost-agent-456",
                            "cost_day_value": 0.25,
                            "cost_week_value": 0.25,
                        }]},
                        "_verify_cost_capture": cost_probe,
                        "_await_dashboard_cost": lambda *_args: None,
                        "_verify_cost_attribution": lambda *_args: None,
                        "_run": lambda *_args, **_kwargs: mock.Mock(
                            stdout=next(dashboard_lists)),
                        "_port_answers": lambda _port: False,
                        "_action_count": lambda *_args: 0,
                        "_await_action": lambda *_args: None,
                        "_await_dashboard_run": lambda *_args: "abc123",
                        "_browser_executable": lambda: Path("browser.exe"),
                        "subprocess": mock.Mock(
                            Popen=mock.Mock(return_value=process),
                            run=mock.Mock(
                                return_value=mock.Mock(returncode=0))),
                        "os": mock.Mock(name="nt"),
                    }),
                ):
                    scope["os"].name = "nt"
                    with self.assertRaisesRegex(
                            RuntimeError, "start control never appeared"):
                        dashboard_actions(
                            Path("agents-live.exe"), Path("C:/repo"),
                            "sample-123", "sample", baseline,
                            "cost-agent-456")
                refresh_lines.nth.assert_called_once_with(1)
                self.assertEqual(["health-ready", "run"], action_order[:2])
            cost_probe.assert_not_called()

    def test_dashboard_posix_cleanup_escalates_process_group(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        terminate = script["_terminate_dashboard"]
        scope = terminate.__globals__
        process = mock.Mock(pid=42)
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired("dashboard", 10), 0)
        killpg = mock.Mock(side_effect=(None, None, ProcessLookupError()))
        operating_system = mock.Mock(name="posix")
        operating_system.name = "posix"
        operating_system.getpgid.return_value = 84
        operating_system.killpg = killpg
        posix_signals = mock.Mock(SIGTERM=15, SIGKILL=9)
        with mock.patch.dict(scope, {
            "os": operating_system,
            "signal": posix_signals,
        }):
            terminate(process)
        self.assertEqual([
            mock.call(84, 15),
            mock.call(84, 9),
            mock.call(84, 0),
        ], killpg.call_args_list)
        process.kill.assert_called_once_with()
        self.assertEqual(2, process.wait.call_count)

    def test_dashboard_cleanup_rejects_unconfirmed_survivors(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        terminate = script["_terminate_dashboard"]
        scope = terminate.__globals__
        windows_process = mock.Mock(pid=42)
        windows_process.poll.return_value = None
        windows_os = mock.Mock(name="nt")
        windows_os.name = "nt"
        with mock.patch.dict(scope, {
            "os": windows_os,
            "subprocess": mock.Mock(
                run=mock.Mock(return_value=mock.Mock(returncode=1))),
            "OperationalError": script["OperationalError"],
        }):
            with self.assertRaisesRegex(
                    script["OperationalError"],
                    "could not terminate dashboard process tree"):
                terminate(windows_process)

        posix_process = mock.Mock(pid=42)
        posix_process.poll.return_value = None
        posix_process.wait.side_effect = (
            subprocess.TimeoutExpired("dashboard", 10), 0)
        posix_os = mock.Mock(name="posix")
        posix_os.name = "posix"
        posix_os.getpgid.return_value = 84
        posix_os.killpg.return_value = None
        clock = iter(range(100))
        with mock.patch.dict(scope, {
            "os": posix_os,
            "signal": mock.Mock(SIGTERM=15, SIGKILL=9),
            "subprocess": subprocess,
            "time": mock.Mock(
                monotonic=mock.Mock(side_effect=lambda: next(clock)),
                sleep=mock.Mock()),
            "OperationalError": script["OperationalError"],
        }):
            with self.assertRaisesRegex(
                    script["OperationalError"],
                    "process group 84 survived cleanup"):
                terminate(posix_process)

    def test_dashboard_posix_cleanup_uses_group_after_launcher_exit(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        terminate = script["_terminate_dashboard"]
        scope = terminate.__globals__
        exited = mock.Mock(pid=42)
        exited.poll.return_value = 0

        posix_os = mock.Mock(name="posix")
        posix_os.name = "posix"
        posix_os.killpg.side_effect = (None, ProcessLookupError())
        with mock.patch.dict(scope, {
            "os": posix_os,
            "signal": mock.Mock(SIGTERM=15, SIGKILL=9),
        }):
            terminate(exited, process_group=84)
        self.assertEqual([
            mock.call(84, 15),
            mock.call(84, 0),
        ], posix_os.killpg.call_args_list)

    def test_dashboard_managed_stop_falls_back_to_live_launcher(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        stop = script["_stop_dashboard"]
        scope = stop.__globals__
        process = mock.Mock(pid=42)
        process.poll.return_value = None
        process.wait.return_value = 0
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            return mock.Mock(returncode=1 if len(commands) == 1 else 0)

        windows_os = mock.Mock(name="nt")
        windows_os.name = "nt"
        with mock.patch.dict(scope, {
            "os": windows_os,
            "subprocess": mock.Mock(run=mock.Mock(side_effect=run)),
            "_port_answers": mock.Mock(side_effect=(True, False)),
            "_verify_dashboard_stopped": lambda *_args: None,
        }):
            stop(
                Path("agents-live.exe"), Path("C:/repo"), 8232, process,
                process_group=None, dashboard_pid=84)
        self.assertEqual([
            ["agents-live.exe", "--repo", str(Path("C:/repo")),
             "dashboard", "stop", "--port", "8232"],
            ["taskkill", "/T", "/F", "/PID", "84"],
        ], commands)

    def test_dashboard_fallback_uses_retained_pid_after_launcher_exit(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        stop = script["_stop_dashboard"]
        scope = stop.__globals__
        process = mock.Mock(pid=42)
        process.poll.return_value = 0
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            return mock.Mock(returncode=1 if len(commands) == 1 else 0)

        windows_os = mock.Mock(name="nt")
        windows_os.name = "nt"
        with mock.patch.dict(scope, {
            "os": windows_os,
            "subprocess": mock.Mock(run=mock.Mock(side_effect=run)),
            "_port_answers": mock.Mock(side_effect=(True, False)),
            "_verify_dashboard_stopped": lambda *_args: None,
        }):
            stop(
                Path("agents-live.exe"), Path("C:/repo"), 8232, process,
                process_group=None, dashboard_pid=84)
        self.assertEqual(
            ["taskkill", "/T", "/F", "/PID", "84"], commands[1])

    def test_dashboard_readiness_failure_retains_registered_pid(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        dashboard_actions = script["_dashboard_actions"]
        scope = dashboard_actions.__globals__
        process = mock.Mock(pid=42)
        process.poll.return_value = 0
        taskkill = mock.Mock(return_value=mock.Mock(returncode=0))

        def fail_readiness(_process, _port, *, observe):
            observe()
            observe()
            raise script["OperationalError"]("dashboard failed readiness")

        dashboard_lists = iter((
            "No dashboard started by this host is running.",
            "No dashboard started by this host is running.",
        ))
        windows_os = mock.Mock(name="nt")
        windows_os.name = "nt"
        sync_api = mock.Mock(sync_playwright=mock.Mock())
        registered_pids = iter((None, 84))
        with (
            mock.patch.dict(sys.modules, {"playwright.sync_api": sync_api}),
            mock.patch.dict(scope, {
                "_free_port": lambda: 8232,
                "_await_api": fail_readiness,
                "_registered_dashboard_pid": lambda *_args: next(
                    registered_pids),
                "_run": lambda *_args, **_kwargs: mock.Mock(
                    stdout=next(dashboard_lists)),
                "_port_answers": lambda _port: False,
                "subprocess": mock.Mock(
                    Popen=mock.Mock(return_value=process),
                    run=taskkill,
                    DEVNULL=subprocess.DEVNULL,
                ),
                "os": windows_os,
            }),
        ):
            with self.assertRaisesRegex(
                    script["OperationalError"],
                    "dashboard failed readiness"):
                dashboard_actions(
                    Path("agents-live.exe"), Path("C:/repo"),
                    "sample-123", "sample", True,
                    "cost-agent-456")
        taskkill.assert_called_once_with(
            ["agents-live.exe", "--repo", str(Path("C:/repo")),
             "dashboard", "stop", "--port", "8232"],
            cwd=Path("C:/repo"), capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False)

    def test_candidate_acceptance_parses_dashboard_list_pid(self) -> None:
        script = runpy.run_path(
            str(REPOSITORY / "tools" / "candidate-operational.py"))
        registered_pid = script["_registered_dashboard_pid"]
        scope = registered_pid.__globals__
        from agents_live.cli.scripts import dashboards

        with (
            mock.patch.object(dashboards, "port_answers", return_value=True),
            mock.patch.dict(scope, {
                "_run": lambda *_args: mock.Mock(stdout=dashboards._table([{
                    "port": 8232,
                    "pid": 42,
                    "started": "2026-08-22T16:00:00+00:00",
                    "repo": "C:/repo",
                }])),
            }),
        ):
            self.assertEqual(
                42,
                registered_pid(
                    Path("agents-live.exe"), Path("C:/repo"), 8232))

    def test_candidate_acceptance_prefers_the_active_self_managed_command(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        installed_cli = release["_installed_cli"]
        scope = installed_cli.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_root = root / "self-managed"
            directory = install_root / "current" / (
                "Scripts" if os.name == "nt" else "bin")
            directory.mkdir(parents=True)
            filename = "agents-live.exe" if os.name == "nt" else "agents-live"
            managed = directory / filename
            managed.write_text("managed", encoding="utf-8")
            with mock.patch.dict(
                    scope["os"].environ,
                    {"AGENTS_LIVE_INSTALL_ROOT": str(install_root)}):
                self.assertEqual(str(managed.resolve()), installed_cli())

    def test_candidate_acceptance_falls_back_to_the_uv_tool(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        installed_cli = release["_installed_cli"]
        scope = installed_cli.__globals__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / "agents-live"
            directory = environment / (
                "Scripts" if os.name == "nt" else "bin")
            directory.mkdir(parents=True)
            filename = "agents-live.exe" if os.name == "nt" else "agents-live"
            managed = directory / filename
            managed.write_text("managed", encoding="utf-8")
            with (
                mock.patch.dict(scope["os"].environ, {
                    "AGENTS_LIVE_INSTALL_ROOT": str(root / "absent"),
                }),
                mock.patch.dict(scope, {
                    "_run": lambda *_args, **_kwargs: str(root),
                }),
            ):
                self.assertEqual(str(managed.resolve()), installed_cli())

    def test_candidate_event_order_and_identity_are_exact(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        verify = release["_verify_candidate_events"]
        watchers = (("C:/repo", "sample"),)
        valid = [
            {"status": "ok", "upgrade_phase": "quiesce-requested",
             "watcher": "sample", "root": "C:/repo"},
            {"status": "ok", "upgrade_phase": "quiesced",
             "watcher": "sample", "root": "C:/repo"},
            {"status": "ok", "operation": "plugin-converge"},
            {"status": "ok", "upgrade_phase": "restore",
             "watcher": "sample", "root": "C:/repo"},
            {"status": "ok", "message": "deferred Windows upgrade completed"},
        ]
        verify(valid, watchers)
        with self.assertRaisesRegex(
                release["ReleaseError"], "out of order"):
            verify([valid[0], valid[1], valid[3], valid[2], valid[4]], watchers)
        prefix_only = [dict(item) for item in valid]
        for item in prefix_only:
            if item.get("watcher") == "sample":
                item["watcher"] = "sample-other"
        with self.assertRaisesRegex(
                release["ReleaseError"], "no exact"):
            verify(prefix_only, watchers)
        wrong_root = [dict(item) for item in valid]
        for item in wrong_root:
            if item.get("root") == "C:/repo":
                item["root"] = "C:/other"
        with self.assertRaisesRegex(
                release["ReleaseError"], "no exact"):
            verify(wrong_root, watchers)

    def test_candidate_event_query_decodes_real_duckdb_attributes(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        decode = release["_decode_candidate_event"]
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
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(qlog.__file__).resolve()),
                    "--log",
                    str(path),
                    "--sql",
                    "select run_id, status, message, attributes from log "
                    "where run_id = 'upgrade-operation' order by ts",
                    "--format",
                    "jsonl",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    "AGENTS_LIVE_REPO": str(Path(temporary).resolve()),
                },
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            serialized = [
                json.loads(line) for line in completed.stdout.splitlines()
                if line.strip()
            ]
            self.assertIsInstance(serialized[0]["attributes"], list)
            rows = [decode(row) for row in serialized]
        self.assertEqual(1, len(rows))
        self.assertEqual("upgrade-watchers", rows[0]["operation"])
        self.assertEqual("quiesced", rows[0]["upgrade_phase"])
        self.assertEqual("sample-123", rows[0]["watcher"])
        self.assertEqual("C:/repo", rows[0]["root"])

    def test_release_blob_validation_applies_git_clean_filters(self) -> None:
        release = runpy.run_path(str(REPOSITORY / "tools" / "release.py"))
        blob_id = release["_blob_id"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "core.autocrlf", "true"],
                cwd=root, check=True)
            path = root / "version.txt"
            blob_id.__globals__["ROOT"] = root
            actual = blob_id(path, b"version\r\n")
            expected = subprocess.run(
                ["git", "hash-object", "--stdin"],
                cwd=root, input=b"version\n", capture_output=True, check=True,
            ).stdout.decode("ascii").strip()
            self.assertEqual(expected, actual)

    def test_the_dashboard_resolves_the_cli_the_same_way_the_registry_does(self) -> None:
        """The dashboard runs in an isolated ``uv run --script``
        environment, so a child ``sys.executable -m agents_live.cli``
        cannot import the package the dashboard itself is using (#288).
        One resolver keeps the two callers from drifting apart again."""
        nicegui = mock.MagicMock()
        nicegui.app.get.side_effect = lambda _path: lambda function: function
        nicegui.ui.refreshable.side_effect = lambda function: function
        with mock.patch.dict(sys.modules, {"nicegui": nicegui}):
            from agents_live.cli.scripts import dashboard
            repo_root = Path("/repos/sample")
            with mock.patch.object(
                    repos, "cli_base",
                    return_value=["/env/bin/agents-live"]):
                argv = dashboard._command_argv(
                    "run", ["--name", "sample"],
                    repo_root=repo_root)
        self.assertEqual(
            ["/env/bin/agents-live", "--repo", str(repo_root), "--json",
             "run", "--name", "sample"],
            argv)
        source = Path(dashboard.__file__).read_text(encoding="utf-8")
        body = source.split("def _command_argv", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("[sys.executable", body)

    def test_a_delegated_script_is_handed_the_cli_that_launched_it(self) -> None:
        """The handoff is what keeps an editable source run acting on the
        source instead of on the installed tool."""
        cli_main = importlib.import_module("agents_live.cli.main")
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(cli_main.state, "resolve_root",
                              return_value=REPOSITORY),
            mock.patch.object(cli_main.state, "clear_root_cache"),
            mock.patch.object(cli_main.state, "cli_base",
                              return_value=["/env/bin/agents-live"]),
            mock.patch.object(cli_main.subprocess, "run",
                              return_value=completed) as run,
            mock.patch.object(cli_main.update_check, "interactive",
                              return_value=False),
        ):
            cli_main.main(["dashboard", "--port", "9000"])
        handed = run.call_args.kwargs["env"][state.CLI_ENV_VAR]
        self.assertEqual(["/env/bin/agents-live"], json.loads(handed))

    def test_the_cli_resolver_prefers_a_declared_prefix_then_an_environment(self) -> None:
        with mock.patch.dict(os.environ, {
                state.CLI_ENV_VAR: json.dumps(["/declared/agents-live"])}):
            self.assertEqual(["/declared/agents-live"], repos.cli_base())
        shim = Path("/env/agents-live")
        for broken in ("not json", "[]", '"string"', '[1, 2]'):
            with self.subTest(broken=broken):
                with (
                    mock.patch.dict(os.environ, {state.CLI_ENV_VAR: broken}),
                    mock.patch.object(repos, "_environment_shim",
                                      return_value=shim),
                ):
                    self.assertEqual([str(shim)], repos.cli_base())

    def test_the_environment_shim_only_answers_for_a_real_environment(self) -> None:
        """Walking up from the package would otherwise accept a personal
        ``~/bin/agents-live`` as the environment's entry point."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            package = home / "src" / "agents_live" / "state"
            package.mkdir(parents=True)
            filename = hostruntime.executable_filename("agents-live")
            directory = "Scripts" if filename.endswith(".exe") else "bin"
            stray = home / directory
            stray.mkdir()
            (stray / filename).write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                mock.patch.object(repos, "__file__",
                                  str(package / "registry.py")),
                mock.patch.object(repos.sys, "executable",
                                  str(home / "python")),
            ):
                # Not asserting None: a temp directory can sit under an
                # ancestor that is itself an environment. What must hold
                # is that the stray bin only answers once its own root
                # is marked as one.
                self.assertNotEqual(stray / filename,
                                    repos._environment_shim())
                (home / "pyvenv.cfg").write_text("home = /usr\n",
                                                 encoding="utf-8")
                self.assertEqual(stray / filename,
                                 repos._environment_shim())

    def test_the_dashboard_readiness_gate_asserts_actions_not_only_a_page(self) -> None:
        """A successful GET of ``/`` proves only that NiceGUI bound a
        port; the rows and their action flags are websocket-rendered and
        absent from that response (#279)."""
        gate = (REPOSITORY / "tools" / "dashboard-readiness.py").read_text(
            encoding="utf-8")
        for token in ("/api/agents", "can_pause", "can_activate", "--dev"):
            with self.subTest(token=token):
                self.assertIn(token, gate)
        self.assertIn("tools/dashboard-readiness.py", self._gate_text())


class TestRepositoryDiscoveryRoots(TempRepository):
    """Which files in a repository are Agents Live agents (#388).

    Discovery reaches the project skill directories that Claude and
    Copilot tooling already use, so the rule that keeps a repository
    honest is metadata: a guidance skill in a shared root belongs to its
    own client, and only a definition that opts in with ``agents-live.*``
    is runnable here. The other half of the decision is that the new
    roots are added, never substituted: a declared ``agent_directories``
    list still extends the standard roots, so no repository loses a
    definition it discovers today.
    """

    def definition(self, path: Path, name: str, *,
                   metadata: bool = True, declared: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["---", f"name: {declared or name}",
                 "description: A portable test definition."]
        if metadata:
            lines += ["metadata:",
                      '  agents-live.schema-version: "1"',
                      '  agents-live.selector: "fake"']
        lines += ["---", "Do the work.", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def names(self, root: Path | None = None) -> set[str]:
        return {spec.name for spec in agent.discover(root or self.root).specs}

    def test_client_skill_roots_are_searched_but_only_ours_run(self) -> None:
        """The operator-guidance payload lives in `.claude/skills/`.

        Listing it as a runnable agent would offer Start and Run actions
        for a document, and the same root holds every unrelated skill the
        user's coding agent reads.
        """
        self.definition(
            self.root / ".claude" / "skills" / "reviewer" / "SKILL.md", "reviewer")
        self.definition(
            self.root / ".github" / "skills" / "summarizer" / "SKILL.md", "summarizer")
        self.definition(
            self.root / ".agents" / "skills" / "digest.md", "digest")
        self.definition(
            self.root / ".claude" / "skills" / "agents-live" / "SKILL.md",
            "agents-live", metadata=False)

        self.assertEqual({"reviewer", "summarizer", "digest"}, self.names())
        self.assertEqual(
            (self.root / ".claude" / "skills" / "reviewer" / "SKILL.md").resolve(),
            agent.load("reviewer", root=self.root).prompt_path)

    def test_a_broken_skill_is_reported_only_when_it_claims_to_be_ours(
            self) -> None:
        """Silence and a report are both wrong for the other case.

        A malformed definition that declares execution metadata is one
        the user expects to run, so hiding it strands them. A malformed
        skill that never mentions Agents Live is another tool's file, and
        reporting it turns every foreign edit into our error.
        """
        ours = self.root / ".claude" / "skills" / "ours" / "SKILL.md"
        ours.parent.mkdir(parents=True)
        ours.write_text(
            "---\nname: ours\ndescription: [invalid\nmetadata:\n"
            '  agents-live.selector: "fake"\n---\nDo the work.\n',
            encoding="utf-8",
        )
        self.definition(
            self.root / ".claude" / "skills" / "theirs" / "SKILL.md",
            "theirs", metadata=False, declared="also-mismatched")
        body_mention = (
            self.root / ".claude" / "skills" / "body-mention" / "SKILL.md")
        body_mention.parent.mkdir(parents=True)
        body_mention.write_text(
            "---\nname: body-mention\ndescription: [invalid\n---\n"
            "This guide mentions agents-live.selector in its prose.\n",
            encoding="utf-8",
        )

        discovery = agent.discover(self.root)
        self.assertEqual((), discovery.specs)
        self.assertEqual(
            [(self.root / ".claude" / "skills" / "ours" / "SKILL.md").resolve()],
            [item.path for item in discovery.broken])

    def test_unterminated_owned_client_skill_is_reported_broken(self) -> None:
        ours = self.root / ".github" / "skills" / "ours" / "SKILL.md"
        ours.parent.mkdir(parents=True)
        ours.write_text(
            "---\nname: ours\ndescription: Broken while being edited.\n"
            "metadata:\n  agents-live.selector: fake\n",
            encoding="utf-8",
        )
        foreign = self.root / ".agents" / "skills" / "foreign" / "SKILL.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text(
            "---\nname: foreign\ndescription: Broken foreign skill.\n",
            encoding="utf-8",
        )

        discovery = agent.discover(self.root)

        self.assertEqual((), discovery.specs)
        self.assertEqual([ours.resolve()], [
            item.path for item in discovery.broken
        ])
        self.assertIn("unterminated frontmatter", discovery.broken[0].message)

    def test_declared_directories_extend_the_standard_roots(self) -> None:
        """Configuration adds roots; it never takes one away.

        A repository that named `Email` under 6.x discovers `Agents/`
        too, and upgrading must not silently drop it. Substituting the
        declared list for the standard set would strand every definition
        in `Agents/` the moment the key appears.
        """
        self.definition(self.root / "Agents" / "native" / "SKILL.md", "native")
        self.definition(self.root / "Email" / "digest" / "SKILL.md", "digest")
        config = self.root / ".agents-live.toml"

        config.write_text('agent_directories = ["Email"]\n', encoding="utf-8")
        self.assertEqual({"digest", "native"}, self.names())

        config.write_text(
            'agent_directories = ["Agents", "Email"]\n', encoding="utf-8")
        self.assertEqual({"digest", "native"}, self.names())

    def test_an_empty_directory_list_still_searches_the_standard_roots(
            self) -> None:
        """`[]` keeps meaning "add nothing", as it does today.

        Reading it as "search nothing" is a defensible design, but it
        would stop a repository that ships `agent_directories = []` from
        finding the definitions it runs now, so that reading waits for a
        release that may break behavior.
        """
        self.definition(self.root / "Agents" / "digest" / "SKILL.md", "digest")
        (self.root / ".agents-live.toml").write_text(
            "agent_directories = []\n", encoding="utf-8")

        self.assertEqual({"digest"}, self.names())

    def test_a_declared_client_root_belongs_to_the_repository(self) -> None:
        """Naming `.claude/skills` claims it.

        A repository that already listed that directory in
        `agent_directories` discovered everything in it under 6.x.
        Applying the metadata rule to a root the repository asked for
        would remove definitions it runs today.
        """
        self.definition(
            self.root / ".claude" / "skills" / "guide" / "SKILL.md",
            "guide", metadata=False)

        self.assertEqual(set(), self.names())

        (self.root / ".agents-live.toml").write_text(
            'agent_directories = [".claude/skills"]\n', encoding="utf-8")
        self.assertEqual({"guide"}, self.names())

    def test_one_file_reached_by_two_roots_is_one_candidate(self) -> None:
        self.definition(self.root / "Agents" / "digest" / "SKILL.md", "digest")
        (self.root / ".agents-live.toml").write_text(
            'agent_directories = ["Agents", "./Agents"]\n', encoding="utf-8")

        self.assertEqual(1, len(agent.discover(self.root).specs))
        self.assertEqual("digest", agent.load("digest", root=self.root).name)

    def test_a_new_client_skill_does_not_capture_an_existing_name(self) -> None:
        """6.x adds discovery sources without re-routing commands.

        A repository that already runs `Agents/digest` must keep running
        it after `.claude/skills/digest` becomes visible; turning that
        into an ambiguity error would break working automation on
        upgrade. Two definitions in Agents Live's own roots stay
        ambiguous, because neither is the established answer.
        """
        native = self.definition(
            self.root / "Agents" / "digest" / "SKILL.md", "digest")
        self.definition(
            self.root / ".claude" / "skills" / "digest" / "SKILL.md", "digest")

        self.assertEqual(
            native.resolve(), agent.load("digest", root=self.root).prompt_path)

        self.definition(self.root / "Email" / "digest.md", "digest")
        (self.root / ".agents-live.toml").write_text(
            'agent_directories = ["Agents", "Email"]\n', encoding="utf-8")
        with self.assertRaisesRegex(agent.DefinitionError, "ambiguous"):
            agent.load("digest", root=self.root)


class TestCrossRepositoryResolution(TempRepository):
    """Which repository answers a bare agent name (#388).

    Registration enrolls a repository in this host's managed set, so a
    name that exactly one registered repository defines should not
    require the user to remember where it lives. The decisions here are
    what happens when nothing answers, when two repositories answer, and
    when the user already said which repository to use - the last one
    being what keeps a scheduled invocation inside its own project.
    """

    def repository(self, name: str) -> Path:
        root = self.root / name
        (root / "Agents").mkdir(parents=True)
        repos.ensure_registered(root)
        return root.resolve()

    def definition(self, root: Path, name: str) -> Path:
        directory = root / "Agents" / name
        directory.mkdir(parents=True, exist_ok=True)
        prompt = directory / "SKILL.md"
        prompt.write_text("\n".join([
            "---",
            f"name: {name}",
            "description: A portable test definition.",
            "metadata:",
            '  agents-live.schema-version: "1"',
            '  agents-live.selector: "fake"',
            "---",
            "Do the work.",
            "",
        ]), encoding="utf-8")
        return prompt

    @contextlib.contextmanager
    def unpinned(self):
        """No repository named on the command line or in the environment."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(paths.ENV_VAR, None)
            yield

    def test_a_unique_name_selects_its_repository(self) -> None:
        notes = self.repository("notes")
        life = self.repository("life")
        prompt = self.definition(notes, "email-reviewer")

        with self.unpinned():
            resolution = resolve.resolve("email-reviewer", root=life)

        self.assertTrue(resolution.fallback)
        self.assertEqual(notes, resolution.root)
        self.assertEqual(prompt.resolve(), resolution.spec.prompt_path)

    def test_a_shared_name_refuses_and_qualifies_both_choices(self) -> None:
        notes = self.repository("notes")
        life = self.repository("life")
        self.definition(notes, "git-sync")
        self.definition(life, "git-sync")

        with self.unpinned(), self.assertRaises(resolve.AmbiguousAgent) as raised:
            resolve.resolve("git-sync", root=self.root, action="start")

        message = str(raised.exception)
        self.assertIn("ambiguous across registered repositories", message)
        self.assertIn("life/git-sync-", message)
        self.assertIn("notes/git-sync-", message)
        self.assertIn("--repo <repository> start", message)

    def test_stop_does_not_choose_the_first_registered_repository(self) -> None:
        notes = self.repository("notes")
        life = self.repository("life")
        self.definition(notes, "git-sync")
        self.definition(life, "git-sync")
        error = io.StringIO()

        with (
            self.unpinned(),
            contextlib.redirect_stderr(error),
            mock.patch.object(stop.paths, "resolve_root", return_value=self.root),
            mock.patch.object(stop.lifecycle, "converge") as converge,
        ):
            code = stop.main(["--name", "git-sync", "--dry-run"])

        self.assertEqual(1, code)
        self.assertIn("ambiguous across registered repositories", error.getvalue())
        self.assertIn("life/git-sync-", error.getvalue())
        self.assertIn("notes/git-sync-", error.getvalue())
        converge.assert_not_called()

    def test_stop_keeps_a_local_answer_and_warns_about_a_registered_one(
            self) -> None:
        notes = self.repository("notes")
        life = self.repository("life")
        local = self.definition(notes, "git-sync")
        self.definition(life, "git-sync")
        identifier = agent.load(str(local), root=notes).identifier
        state.replace(notes, {identifier})
        error = io.StringIO()

        with (
            self.unpinned(),
            contextlib.redirect_stderr(error),
            mock.patch.object(stop.paths, "resolve_root", return_value=notes),
            mock.patch.object(stop.lifecycle, "converge") as converge,
        ):
            converge.return_value.failed = ()
            code = stop.main(["--name", "git-sync", "--dry-run"])

        self.assertEqual(0, code)
        self.assertIn("life/git-sync-", error.getvalue())
        converge.assert_called_once_with(
            removals={notes: {identifier}}, dry_run=True)

    def test_stop_reports_a_missing_name_without_a_traceback(self) -> None:
        error = io.StringIO()

        with (
            self.unpinned(),
            contextlib.redirect_stderr(error),
            mock.patch.object(stop.paths, "resolve_root", return_value=self.root),
            mock.patch.object(stop.lifecycle, "converge") as converge,
        ):
            code = stop.main(["--name", "absent", "--dry-run"])

        self.assertEqual(1, code)
        self.assertIn("definition not found: absent", error.getvalue())
        converge.assert_not_called()

    def test_a_missing_name_reports_where_it_looked(self) -> None:
        notes = self.repository("notes")
        with self.unpinned(), self.assertRaises(agent.DefinitionNotFound) as raised:
            resolve.resolve("absent", root=self.root)
        self.assertIn(str(notes), str(raised.exception))

    def test_an_explicit_repository_narrows_the_search(self) -> None:
        """`--repo` is a decision, not a hint.

        It exports the repository environment variable, which is also how
        every persisted invocation pins its project. Searching past it
        would let a scheduled run fire an agent from another repository
        that happens to share the name.
        """
        notes = self.repository("notes")
        life = self.repository("life")
        self.definition(notes, "email-reviewer")

        os.environ[paths.ENV_VAR] = str(life)
        with self.assertRaises(agent.DefinitionNotFound) as raised:
            resolve.resolve("email-reviewer", root=life)
        self.assertNotIn(str(notes), str(raised.exception))

    def test_a_local_answer_survives_and_warns_about_the_other_repository(
            self) -> None:
        notes = self.repository("notes")
        life = self.repository("life")
        here = self.definition(notes, "git-sync")
        self.definition(life, "git-sync")

        with self.unpinned():
            resolution = resolve.resolve("git-sync", root=notes)

        self.assertFalse(resolution.fallback)
        self.assertEqual(here.resolve(), resolution.spec.prompt_path)
        self.assertIn("life/git-sync-", resolution.warning or "")

    def test_selection_does_not_follow_registration_order(self) -> None:
        notes = self.repository("notes")
        life = self.repository("life")
        self.definition(notes, "git-sync")
        self.definition(life, "git-sync")
        registry = repos.config_path()
        forward = registry.read_text(encoding="utf-8")
        reversed_entries = "\n".join([
            "[repos]",
            f'"notes" = {json.dumps(str(notes))}',
            f'"life" = {json.dumps(str(life))}',
            "",
        ])
        self.assertNotEqual(forward, reversed_entries)

        messages = []
        for content in (forward, reversed_entries):
            registry.write_text(content, encoding="utf-8")
            with self.unpinned(), self.assertRaises(
                    resolve.AmbiguousAgent) as raised:
                resolve.resolve("git-sync", root=self.root)
            messages.append(str(raised.exception))

        self.assertEqual(messages[0], messages[1])


if __name__ == "__main__":
    unittest.main()
