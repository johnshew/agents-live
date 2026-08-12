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

import contextlib
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agents_live import agent, obs, paths, plugins, runtime, state
from agents_live.agent import providers
from agents_live.cli import lifecycle
from agents_live.legacy import health_check
from agents_live.obs import qlog
from agents_live.runtime import Subscription
from agents_live.runtime import artifacts
from agents_live.runtime.hosts import crontab as crontasks
from agents_live.runtime.hosts import system as hostruntime
from agents_live.runtime.hosts.memory import MemoryHost
from agents_live.runtime.hosts.posix import PosixHost, PosixTriggerStore
from agents_live.state import ownership
from agents_live.state import registry as repos

REPOSITORY = Path(__file__).resolve().parents[1]

_ISOLATED_HOMES = {
    "XDG_STATE_HOME": "state",
    "XDG_DATA_HOME": "data",
    "XDG_CONFIG_HOME": "config",
}


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
                self.assertEqual(rendered.key, decoded["key"])
                self.assertEqual(kind, decoded["kind"])
                self.assertEqual("agent:sample", decoded["target"])
                self.assertEqual(rendered.fingerprint, decoded["fingerprint"])


class TestHostMaintenanceEntries(TempRepository):
    """The tool's own host entries, which no repository owns."""

    def test_maintenance_lines_name_no_repository(self) -> None:
        """Host-scoped by construction: it resolves repositories itself.

        A ``cd`` or ``--repo`` here would pin the loop that repairs every
        registered project to whichever project happened to install it.
        """
        with mock.patch.object(
                health_check, "cli_shim_path",
                return_value=Path("/opt/bin/agents-live")):
            lines = health_check.build_health_cron_lines()
        self.assertEqual(len(health_check.HEALTH_SCHEDULES), len(lines))
        for line in lines:
            self.assertNotIn(" cd ", line)
            self.assertNotIn("--repo", line)
            self.assertIn("internal maintain --quiet", line)
            self.assertTrue(health_check.health_cron_line_matches(line))

    def test_the_matcher_ignores_agent_and_foreign_lines(self) -> None:
        """Removal is keyed on this matcher, so a false positive deletes
        an entry this tool did not write."""
        for line in (
            "0 3 * * * /home/someone/backup.sh",
            "0 8 * * * cd /repo && agents-live run --name maintain 2>&1",
            "0 * * * * /usr/bin/maintain --quiet",
            "0 * * * * internal maintain --quiet",
        ):
            with self.subTest(line=line):
                self.assertFalse(health_check.health_cron_line_matches(line))
        self.assertTrue(health_check.health_cron_line_matches(
            "0 * * * * PATH=/bin /opt/bin/agents-live internal maintain "
            "--quiet 2>&1"))

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
        Scoping it to agents-live.log answered "none" while failed runs
        sat in per-agent logs."""
        source = (Path(qlog.__file__).read_text(encoding="utf-8")
                  .split("patterns = ", 1)[1].split("\n", 1)[0])
        self.assertIn("span_everything", source)
        self.assertIn("args.errors and args.name is None",
                      Path(qlog.__file__).read_text(encoding="utf-8"))


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


class TestCrossModuleAgreements(unittest.TestCase):
    """Assertions that two parts of the tree still agree (#216).

    Each holds a fact that no single module can check, and that a defect
    reached production by breaking.
    """

    def _gate_text(self) -> str:
        return (REPOSITORY / "tools" / "release.py").read_text(encoding="utf-8")

    def _workflow_text(self, name: str) -> str:
        return (REPOSITORY / ".github" / "workflows" / name).read_text(
            encoding="utf-8")

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

    def test_the_publish_workflow_runs_the_declared_gates(self) -> None:
        """Restating the gate list in YAML is how a release once shipped
        past a gate the local run kept (#218)."""
        publish = self._workflow_text("publish.yml")
        self.assertIn("tools/release.py --gates", publish)
        self.assertNotIn("tests/test_", publish)

    def test_the_release_gates_pin_the_smoketest_to_this_checkout(self) -> None:
        """Without ``--repo`` the smoketest acts on whatever root
        resolves, which on a configured host is another project."""
        gates = self._gate_text()
        self.assertRegex(
            gates, r'"--repo",\s*str\(ROOT\),\s*"smoketest"')

    def test_the_smoketest_waits_longer_than_an_agent_may_take(self) -> None:
        """A supervisor that gives up before its child can finish reports
        a healthy system as broken."""
        self.assertGreater(
            health_check.SMOKETEST_TIMEOUT_S,
            health_check.SWEEP_TIMEOUT_S)

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
            with mock.patch.object(
                    repos, "cli_base",
                    return_value=["/env/bin/agents-live"]):
                argv = dashboard._command_argv("run", ["--name", "sample"])
        self.assertEqual(
            ["/env/bin/agents-live", "run", "--name", "sample"], argv)
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


if __name__ == "__main__":
    unittest.main()
